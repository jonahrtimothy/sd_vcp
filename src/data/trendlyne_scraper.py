"""
Cash FII/DII flow scraper: trendlyne.com.

Why this exists alongside nse_scraper.py's fetch_cash_fii_dii(): NSE's own
live JSON endpoint only ever returns the most recent ~1-2 trading days --
not a historical backfill source, and a real unrecoverable gap if sd_vcp
misses several days (see PROJECT_CONTEXT.md). Investigated three
alternatives for this specific gap (Aug 2026):
  - BSE India: reachable via plain HTTP, but its FII/DII report page is
    protected by Akamai bot-detection that blocks headless-browser
    fingerprints (same wall as NSE) -- not usable.
  - NiftyTrader: no obviously embedded historical data found in a quick
    check -- not pursued further once Trendlyne worked.
  - Trendlyne: WORKS. Its public "latest" FII/DII page embeds a genuine
    ~1-trading-month rolling window of real daily data directly in
    server-rendered HTML (a `data-jsondata` attribute on
    `<table id="cash-table-main-pastmonth">`) -- reachable via a plain
    HTTP GET, no Playwright/browser needed at all, and confirmed to match
    NSE's own real published figures exactly for a cross-checked date
    (28 Aug 2026: FII net -Rs5039.8cr, DII net +Rs5183.9cr, matching
    nse_scraper.py's independently-fetched real data for the same date).

Because this single page always carries ~a month of trailing history,
fetching it regularly effectively CANNOT lose data to a gap shorter than
that window -- this closes the "unrecoverable gap" problem directly,
without needing NSE, Playwright, or any cloud-archiver workaround.
"""

from __future__ import annotations

import html as html_module
import json
import re
import urllib.request

import pandas as pd

URL = "https://trendlyne.com/macro-data/fii-dii/latest/"
TABLE_ID = "cash-table-main-pastmonth"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_SUMMARY_ROW_LABELS = {"Last 30 Days", "Last 2 Weeks", "Last 1 Week"}


class ScraperError(Exception):
    """Raised when the page doesn't look like what this parser expects --
    should stop and get attention, not be silently swallowed."""


def fetch_cash_fii_dii_history() -> pd.DataFrame:
    """
    Fetch ~1 trading month of real daily cash FII/DII flow.
    Returns a tidy DataFrame with columns [category, date, buyValue,
    sellValue, netValue] -- the exact shape db.upsert_cash_fii_dii()
    already expects (same column names nse_scraper.fetch_cash_fii_dii()
    produces), so it plugs in with zero schema changes.
    """
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        raise ScraperError(f"Could not reach {URL}: {e!r}")

    if status != 200:
        raise ScraperError(f"Unexpected HTTP status {status} for {URL}")

    match = re.search(
        rf'<table id="{TABLE_ID}"[^>]*data-jsondata="([^"]+)"', body
    )
    if not match:
        raise ScraperError(
            f"Could not find table#{TABLE_ID} with a data-jsondata attribute "
            f"on {URL} -- Trendlyne may have changed their page structure."
        )

    try:
        payload = json.loads(html_module.unescape(match.group(1)))
    except json.JSONDecodeError as e:
        raise ScraperError(f"data-jsondata attribute wasn't valid JSON: {e}")

    expected_headers = [
        "date", "FII Gross Purchase", "FII Gross Sales",
        "FII Net Purchase / Sales", "DII Net Purchase / Sales",
        "DII Gross Sales", "DII Gross Purchase", "details",
    ]
    if payload.get("headers") != expected_headers:
        raise ScraperError(
            f"Unexpected column headers: {payload.get('headers')}. "
            f"Trendlyne may have changed their table structure -- "
            f"expected: {expected_headers}"
        )

    rows = []
    for row in payload.get("data", []):
        date_label = row[0]
        if date_label in _SUMMARY_ROW_LABELS:
            continue  # skip "Last 30 Days" / "Last 2 Weeks" / "Last 1 Week" rollups
        (
            _, fii_buy, fii_sell, fii_net,
            dii_net, dii_sell, dii_buy, _,
        ) = row
        rows.append({"category": "FII/FPI", "date": date_label, "buyValue": fii_buy, "sellValue": fii_sell, "netValue": fii_net})
        rows.append({"category": "DII", "date": date_label, "buyValue": dii_buy, "sellValue": dii_sell, "netValue": dii_net})

    if not rows:
        raise ScraperError("Parsed 0 dated rows -- table structure may have changed.")

    print(f"Parsed {len(rows)} rows ({len(rows)//2} trading days) from Trendlyne")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import db

    try:
        result = fetch_cash_fii_dii_history()
        db.init_db()
        n = db.upsert_cash_fii_dii(result)
        print(f"\nSaved {n} rows to database (table: cash_fii_dii)")
        print("\n=== Most recent 10 rows ===")
        print(result.sort_values("date", ascending=False).head(10).to_string(index=False))
    except ScraperError as e:
        print(f"\nSCRAPER ERROR: {e}")
        sys.exit(1)
