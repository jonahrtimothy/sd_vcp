"""
NSE data scraper: participant-wise OI, cash FII/DII, and delivery %.

Fetches daily NSE data using a Playwright browser context to establish the
session cookies NSE requires (a plain requests.get() without a real browser
session gets blocked). Persists results to the local SQLite database.

Usage: python nse_scraper.py <mode> [date]
  mode: oi | fiidii | delivery
  date: YYYY-MM-DD (optional, defaults to today; not used for fiidii)
"""

from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
import sys
import time

import pandas as pd
from playwright.sync_api import sync_playwright

RAW_DIR = Path(__file__).parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

EXPECTED_PARTICIPANTS = {"Client", "DII", "FII", "Pro", "TOTAL"}
EXPECTED_MIN_COLUMNS = 14  # 13 numeric columns + the participant label column


class ScraperError(Exception):
    """Raised when the scraper gets a response but it doesn't look like
    valid data — this should stop the run and get your attention, not be
    silently swallowed."""


def _build_url(for_date: date) -> str:
    return (
        "https://nsearchives.nseindia.com/content/nsccl/"
        f"fao_participant_oi_{for_date.strftime('%d%m%Y')}.csv"
    )


def fetch_participant_oi(for_date: date | None = None, headless: bool = False) -> pd.DataFrame:
    """
    Fetch and parse the participant-wise OI report for `for_date`
    (defaults to today). Returns a tidy DataFrame:
    columns = [date, participant, instrument, side, contracts]
    """
    for_date = for_date or date.today()
    url = _build_url(for_date)
    print(f"Fetching: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print("Warming up session on nseindia.com ...")
        page.goto("https://www.nseindia.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        response = context.request.get(url)
        status = response.status
        raw_text = response.text()

        browser.close()

    if status == 404:
        raise ScraperError(
            f"Got 404 for {url}. Either today's report isn't published yet "
            f"(check timing — NSE publishes ~5-6 PM IST, later on expiry days), "
            f"or NSE has changed this URL pattern (they've done this before). "
            f"If this persists on a normal trading day well after 6:30 PM IST, "
            f"the URL pattern likely needs updating — check nseindia.com's "
            f"current report page manually."
        )
    if status != 200:
        raise ScraperError(f"Unexpected HTTP status {status} for {url}")

    raw_path = RAW_DIR / f"fao_participant_oi_{for_date.strftime('%Y%m%d')}.csv"
    raw_path.write_text(raw_text, encoding="utf-8")
    print(f"Saved raw file: {raw_path}")

    return _parse_and_validate(raw_text, for_date)


def _parse_and_validate(raw_text: str, for_date: date) -> pd.DataFrame:
    df = pd.read_csv(StringIO(raw_text), skiprows=1)
    df.columns = [c.strip() for c in df.columns]

    if df.shape[1] < EXPECTED_MIN_COLUMNS:
        raise ScraperError(
            f"Parsed only {df.shape[1]} columns, expected at least "
            f"{EXPECTED_MIN_COLUMNS}. NSE may have changed the file format — "
            f"inspect the raw file in data/raw/ before trusting this data."
        )

    label_col = df.columns[0]
    participants_found = set(df[label_col].astype(str).str.strip())
    missing = EXPECTED_PARTICIPANTS - participants_found
    if missing:
        raise ScraperError(
            f"Missing expected participant rows: {missing}. "
            f"Found: {participants_found}. File format may have changed — "
            f"inspect the raw file in data/raw/ before trusting this data."
        )

    print(f"Validated OK — {len(df)} rows, {df.shape[1]} columns, "
          f"participants found: {sorted(participants_found)}")

    tidy_rows = []
    instrument_map = {
        "Future Index Long": ("index_fut", "long"),
        "Future Index Short": ("index_fut", "short"),
        "Future Stock Long": ("stock_fut", "long"),
        "Future Stock Short": ("stock_fut", "short"),
        "Option Index Call Long": ("index_opt_call", "long"),
        "Option Index Put Long": ("index_opt_put", "long"),
        "Option Index Call Short": ("index_opt_call", "short"),
        "Option Index Put Short": ("index_opt_put", "short"),
        "Option Stock Call Long": ("stock_opt_call", "long"),
        "Option Stock Put Long": ("stock_opt_put", "long"),
        "Option Stock Call Short": ("stock_opt_call", "short"),
        "Option Stock Put Short": ("stock_opt_put", "short"),
    }

    for _, row in df.iterrows():
        participant = str(row[label_col]).strip()
        if participant == "TOTAL":
            continue
        for col, (instrument, side) in instrument_map.items():
            if col not in df.columns:
                continue
            tidy_rows.append(
                {
                    "date": for_date.isoformat(),
                    "participant": participant,
                    "instrument": instrument,
                    "side": side,
                    "contracts": row[col],
                }
            )

    return pd.DataFrame(tidy_rows)


def fetch_cash_fii_dii(headless: bool = False) -> pd.DataFrame:
    """
    Fetch cash-market FII/DII trading activity via NSE's live JSON API.
    NOTE: unlike participant OI (a per-date historical archive), this
    endpoint only returns the most recent ~1-2 trading days — it is NOT a
    historical backfill source. Call this daily and accumulate results
    locally in the DB if you want history.
    """
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    print(f"Fetching: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page = context.new_page()
        print("Warming up session on nseindia.com ...")
        page.goto("https://www.nseindia.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        response = context.request.get(url)
        status = response.status
        raw_json = response.json() if status == 200 else None

        browser.close()

    if status != 200:
        raise ScraperError(f"Unexpected HTTP status {status} for {url}")
    if not raw_json or not isinstance(raw_json, list):
        raise ScraperError(
            f"Response didn't look like the expected JSON list. "
            f"NSE may have changed this endpoint's shape. Raw response: {raw_json}"
        )

    df = pd.DataFrame(raw_json)
    expected_cols = {"category", "date", "buyValue", "sellValue", "netValue"}
    missing = expected_cols - set(df.columns)
    if missing:
        raise ScraperError(
            f"Missing expected columns: {missing}. Found: {list(df.columns)}. "
            f"NSE may have changed this endpoint's field names."
        )

    raw_path = RAW_DIR / f"cash_fii_dii_{date.today().strftime('%Y%m%d')}.json"
    raw_path.write_text(str(raw_json), encoding="utf-8")
    print(f"Saved raw file: {raw_path}")
    print(f"Validated OK — {len(df)} rows, categories: {sorted(df['category'].unique())}")

    return df[["category", "date", "buyValue", "sellValue", "netValue"]]


def fetch_delivery_data(for_date: date | None = None, headless: bool = False) -> pd.DataFrame:
    """
    Fetch the daily security-wise delivery position (the "MTO" file) and
    return delivery % per symbol, filtered to equity (EQ series) only —
    the raw file also contains bonds, G-secs, SME listings, etc.
    """
    for_date = for_date or date.today()
    date_str = for_date.strftime("%d%m%Y")
    urls_to_try = [
        f"https://nsearchives.nseindia.com/archives/equities/mto/MTO_{date_str}.DAT",
        f"https://www1.nseindia.com/archives/equities/mto/MTO_{date_str}.DAT",
    ]

    raw_text, used_url, status = None, None, None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page = context.new_page()
        print("Warming up session on nseindia.com ...")
        page.goto("https://www.nseindia.com", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        last_error = None
        for url in urls_to_try:
            print(f"Trying: {url}")
            try:
                response = context.request.get(url)
            except Exception as e:
                # A network/TLS-level failure on one URL (seen in practice:
                # the legacy www1.nseindia.com fallback threw an SSL alert)
                # must not abort the whole fetch -- still try the remaining
                # URL(s) before giving up.
                last_error = e
                status = None
                continue
            if response.status == 200:
                raw_text, used_url, status = response.text(), url, 200
                break
            status = response.status

        browser.close()

    if raw_text is None:
        raise ScraperError(
            f"Could not fetch delivery data from any known URL pattern for "
            f"{for_date}. Last status: {status}. Last error: {last_error}. "
            f"Either the date has no trading data, or NSE changed the file "
            f"location/format again — check nseindia.com's current delivery "
            f"position report page manually."
        )

    raw_path = RAW_DIR / f"mto_{for_date.strftime('%Y%m%d')}.dat"
    raw_path.write_text(raw_text, encoding="utf-8")
    print(f"Saved raw file: {raw_path} (from {used_url})")

    lines = [l for l in raw_text.strip().split("\n") if l.strip()]
    rows = [l.split(",") if "," in l else l.split("|") for l in lines]
    rows = [r for r in rows if len(r) >= 7]

    if len(rows) < 50:
        raise ScraperError(
            f"Only parsed {len(rows)} rows — expected hundreds. File format "
            f"may have changed. Inspect the raw file at {raw_path} before trusting this."
        )

    ncols = len(rows[0])
    base_cols = ["record_type", "sr_no", "symbol", "series", "traded_qty", "deliverable_qty", "delivery_pct"]
    columns = base_cols + [f"extra_{i}" for i in range(ncols - 7)] if ncols > 7 else base_cols

    df = pd.DataFrame(rows, columns=columns)
    df["traded_qty"] = pd.to_numeric(df["traded_qty"], errors="coerce")
    df["deliverable_qty"] = pd.to_numeric(df["deliverable_qty"], errors="coerce")
    df["delivery_pct"] = pd.to_numeric(df["delivery_pct"], errors="coerce")
    df["date"] = for_date.isoformat()

    print(f"Validated OK — {len(df)} securities parsed")
    df = df[df["series"] == "EQ"].reset_index(drop=True)
    print(f"Filtered to {len(df)} equity-only rows (series == 'EQ')")
    return df[["date", "symbol", "series", "traded_qty", "deliverable_qty", "delivery_pct"]]


def _trading_weekdays_since(last_date: date, today: date) -> list:
    """Weekdays strictly after `last_date`, up to and including `today`.
    Weekends are skipped outright (never worth a request); actual market
    holidays among the remaining weekdays still get requested and are
    handled as expected 404s by the backfill loops below."""
    days = []
    d = last_date + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def backfill_participant_oi(db_module, days: int = 15, headless: bool = False) -> dict:
    """
    Catches up participant OI for every weekday missing from the DB in the
    last `days` calendar days -- safe after any gap, since this report is a
    per-date NSE archive (unlike cash FII/DII's live-only endpoint, this
    genuinely can be backfilled any time later).

    A 404 on a PAST date is treated as "market holiday, no report" (not an
    error) -- for a date that's already gone by, a real report would
    already be published if one existed. If EVERY weekday in the range
    404s, that's statistically implausible as "all holidays" and is
    surfaced loudly instead, since it more likely means the URL pattern
    broke. Returns {date_iso: 'ok (N rows)' | 'no data (holiday?)' | 'ERROR: ...'}.
    """
    existing_dates = set(db_module.get_participant_oi(symbol="MARKET")["date"])
    today = date.today()
    lookback_start = today - timedelta(days=days)
    candidates = [
        d for d in _trading_weekdays_since(lookback_start, today)
        if d.isoformat() not in existing_dates
    ]

    results = {}
    ok_count = 0
    for d in candidates:
        try:
            df = fetch_participant_oi(d, headless=headless)
            n = db_module.upsert_participant_oi(df)
            results[d.isoformat()] = f"ok ({n} rows)"
            ok_count += 1
        except ScraperError as e:
            is_404 = "Got 404" in str(e)
            if not is_404:
                results[d.isoformat()] = f"ERROR: {e}"
            elif d == today:
                # today's 404 is genuinely ambiguous (not yet published vs.
                # pattern broke) -- NOT assumed a holiday like a past date
                results[d.isoformat()] = "not yet published today (or pattern changed) -- check after ~6:30pm IST"
            else:
                results[d.isoformat()] = "no data (holiday?)"
        except Exception as e:
            # A network/browser-level failure (e.g. an SSL error on one
            # request) for a single date must not abort the whole backfill
            # run -- log it and move on to the next date (same "continue
            # past a single failure" principle as scanner.py's watchlist loop).
            results[d.isoformat()] = f"ERROR (unexpected): {e}"

    weekday_attempts = len([d for d in candidates if d != today])
    ok_past = len([d for d in candidates if d != today and results.get(d.isoformat(), "").startswith("ok")])
    if weekday_attempts >= 3 and ok_past == 0:
        print(
            f"WARNING: 0 of {weekday_attempts} PAST weekday backfill attempts succeeded. "
            f"This many holidays in a row is unlikely -- the URL pattern may have "
            f"changed. Inspect data/raw/ and nseindia.com's report page manually."
        )
    return results


def backfill_delivery_data(db_module, days: int = 15, headless: bool = False) -> dict:
    """Same catch-up logic as backfill_participant_oi, for delivery%."""
    existing_dates = set(db_module.get_delivery_pct("RELIANCE")["date"])  # any liquid symbol works as a date probe
    today = date.today()
    lookback_start = today - timedelta(days=days)
    candidates = [
        d for d in _trading_weekdays_since(lookback_start, today)
        if d.isoformat() not in existing_dates
    ]

    results = {}
    for d in candidates:
        try:
            df = fetch_delivery_data(d, headless=headless)
            n = db_module.upsert_delivery_pct(df)
            results[d.isoformat()] = f"ok ({n} rows)"
        except ScraperError as e:
            is_no_file = "Could not fetch delivery data" in str(e)
            if not is_no_file:
                results[d.isoformat()] = f"ERROR: {e}"
            elif d == today:
                results[d.isoformat()] = "not yet published today (or pattern changed) -- check after ~6:30pm IST"
            else:
                results[d.isoformat()] = "no data (holiday?)"
        except Exception as e:
            # Same defense as backfill_participant_oi: a single date's
            # unexpected failure (browser crash, etc.) must not abort the
            # whole backfill run.
            results[d.isoformat()] = f"ERROR (unexpected): {e}"

    weekday_attempts = len([d for d in candidates if d != today])
    ok_past = len([d for d in candidates if d != today and results.get(d.isoformat(), "").startswith("ok")])
    if weekday_attempts >= 3 and ok_past == 0:
        print(
            f"WARNING: 0 of {weekday_attempts} PAST weekday backfill attempts succeeded. "
            f"This many holidays in a row is unlikely -- the URL pattern may have "
            f"changed. Inspect data/raw/ and nseindia.com's report page manually."
        )
    return results


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent.parent))  # allow `import db`
    import db

    target_date = date.today()
    mode = "oi"
    if len(sys.argv) > 1:
        mode = sys.argv[1]
    if len(sys.argv) > 2 and mode in ("oi", "delivery"):
        # only oi/delivery take a specific YYYY-MM-DD date; the
        # backfill-* modes take an integer day-count instead (parsed
        # separately, below, in their own branches)
        target_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()

    db.init_db()

    try:
        if mode == "oi":
            result = fetch_participant_oi(target_date, headless=False)
            n = db.upsert_participant_oi(result)
            print(f"\nSaved {n} rows to database (table: participant_oi)")
        elif mode == "fiidii":
            result = fetch_cash_fii_dii(headless=False)
            n = db.upsert_cash_fii_dii(result)
            print(f"\nSaved {n} rows to database (table: cash_fii_dii)")
        elif mode == "delivery":
            result = fetch_delivery_data(target_date, headless=False)
            n = db.upsert_delivery_pct(result)
            print(f"\nSaved {n} rows to database (table: delivery_pct)")
        elif mode == "backfill-oi":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 15
            summary = backfill_participant_oi(db, days=days)
            for d, status in sorted(summary.items()):
                print(f"  {d}: {status}")
            sys.exit(0)
        elif mode == "backfill-delivery":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 15
            summary = backfill_delivery_data(db, days=days)
            for d, status in sorted(summary.items()):
                print(f"  {d}: {status}")
            sys.exit(0)
        else:
            print(f"Unknown mode '{mode}'. Use: oi | fiidii | delivery | backfill-oi [days] | backfill-delivery [days]")
            sys.exit(1)

        print("\n=== Parsed result (first 20 rows) ===")
        print(result.head(20).to_string(index=False))
        print(f"\nTotal rows: {len(result)}")
    except ScraperError as e:
        print(f"\nSCRAPER ERROR: {e}")
        sys.exit(1)