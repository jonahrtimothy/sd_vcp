"""
Fundamentals scraper: screener.in (Section 5 quality filter).

screener.in has no official free API, so this parses the public company
page's HTML directly. Confirmed against the real live site (Aug 2026):
company pages expose a "Quarterly Results" table (Sales / Net Profit /
EPS per quarter, with `data-date-key` quarter-end dates on each header
cell) and a "Peer comparison" section header that carries the sector/
industry classification as plain link text.

MVP scope (deliberately): the earnings-growth-trend half of Section 5's
fundamental filter (accelerating/decelerating/declining quarterly EPS
growth). "Sector relative strength" (comparing the stock's sector index
performance to the broader index) is NOT covered here -- that needs
sector-index price history, a separate data source not wired in yet.
Flagged as a follow-up, not silently skipped.

Fails loudly (ScraperError) on structural changes rather than silently
returning wrong/empty data, matching the pattern established in
nse_scraper.py. Raw HTML is always saved to data/raw/ for audit.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

RAW_DIR = Path(__file__).parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

GROWTH_ROWS = {"Sales", "Net Profit", "EPS in Rs"}
# Banks/NBFCs/insurers report "Revenue" instead of "Sales" as their
# top-line row (confirmed on a real page: HDFCBANK) -- same meaning for
# our growth-trend purposes, normalized to the "Sales" key downstream.
ROW_ALIASES = {"Revenue": "Sales"}
# Step 15.4: OPM % sits in the same Quarterly Results table for
# manufacturers/most companies (confirmed real: RELIANCE), but banks
# report "Financing Margin %" instead -- a different, non-comparable
# metric (net interest margin, not operating margin), so it's captured
# opportunistically and never required, unlike GROWTH_ROWS above.
OPTIONAL_QUARTER_ROWS = {"OPM %"}
MIN_REQUEST_GAP_SECONDS = 2.0  # be a light touch on screener.in -- this is a personal, low-frequency, weekly-cached scrape, not a crawler

_last_request_time = 0.0


class ScraperError(Exception):
    """Raised when screener.in's page structure doesn't match what this
    parser expects -- should stop and get attention, not silently return
    wrong data."""


def _throttled_get(url: str) -> str:
    global _last_request_time
    wait = MIN_REQUEST_GAP_SECONDS - (time.time() - _last_request_time)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        status = e.code
        body = ""
    _last_request_time = time.time()
    if status == 404:
        raise ScraperError(f"404 for {url} -- symbol may not exist on screener.in under this ticker")
    if status != 200:
        raise ScraperError(f"Unexpected HTTP status {status} for {url}")
    return body


def _extract_section(html: str, section_id: str) -> Optional[str]:
    m = re.search(rf'<section[^>]*id="{section_id}".*?</section>', html, re.S)
    return m.group(0) if m else None


def _clean_text(html_fragment: str) -> str:
    text = re.sub(r"<[^>]*>", "", html_fragment)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(raw: str) -> Optional[float]:
    raw = raw.strip().replace(",", "").rstrip("%")
    if raw in ("", "-", "NA"):
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    try:
        val = float(raw)
    except ValueError:
        return None
    return -val if negative else val


def _parse_quarters_table(quarters_section: str) -> dict:
    """Returns {row_label: [values across quarters, oldest to newest]} for
    Sales / Net Profit / EPS in Rs, plus the quarter-end date labels."""
    header_dates = re.findall(r'data-date-key="([\d-]+)"', quarters_section)

    tbody_m = re.search(r"<tbody>.*?</tbody>", quarters_section, re.S)
    if not tbody_m:
        raise ScraperError(
            "No <tbody> found in the Quarterly Results section -- "
            "screener.in's page structure may have changed."
        )

    rows_out = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_m.group(0), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if not cells:
            continue
        label = _clean_text(cells[0]).rstrip("+").strip()
        label = ROW_ALIASES.get(label, label)
        if label not in GROWTH_ROWS and label not in OPTIONAL_QUARTER_ROWS:
            continue
        values = [_parse_number(_clean_text(c)) for c in cells[1:]]
        rows_out[label] = values

    missing = GROWTH_ROWS - set(rows_out.keys())
    if missing:
        raise ScraperError(
            f"Missing expected row(s) in Quarterly Results: {missing}. "
            f"screener.in's page structure may have changed."
        )

    return {"dates": header_dates, **rows_out}


def _parse_sector(html: str) -> tuple:
    peers = _extract_section(html, "peers")
    if not peers:
        return None, None
    sector_m = re.search(r'title="Sector">([^<]+)</a>', peers)
    industry_m = re.search(r'title="Industry">([^<]+)</a>', peers)
    sector = sector_m.group(1).replace("&amp;", "&") if sector_m else None
    industry = industry_m.group(1).replace("&amp;", "&") if industry_m else None
    return sector, industry


def _yoy_growth_trend(values: list, min_quarters_required: int) -> dict:
    """Compares each quarter's value to the SAME quarter a year earlier (4
    columns back) to get a YoY growth rate, then compares the latest YoY
    growth rate to the prior quarter's YoY growth rate to judge whether
    growth is accelerating, decelerating, or declining. Shared core used
    by both EPS (earnings_trend) and Sales (sales_trend, Step 15.4) --
    same logic, different input series."""
    clean = [v for v in values if v is not None]
    if len(values) < min_quarters_required or len(clean) < min_quarters_required:
        return {"latest_yoy": None, "prior_yoy": None, "trend": "insufficient_data"}

    def yoy(idx: int) -> Optional[float]:
        if idx < 4 or idx >= len(values):
            return None
        cur, prior_year = values[idx], values[idx - 4]
        if cur is None or prior_year is None or prior_year == 0:
            return None
        return (cur - prior_year) / abs(prior_year) * 100

    latest_yoy = yoy(len(values) - 1)
    prior_yoy = yoy(len(values) - 2)

    if latest_yoy is None:
        trend = "insufficient_data"
    elif latest_yoy < 0:
        trend = "declining"
    elif prior_yoy is not None and latest_yoy > prior_yoy:
        trend = "accelerating"
    else:
        trend = "decelerating"

    return {
        "latest_yoy": round(latest_yoy, 1) if latest_yoy is not None else None,
        "prior_yoy": round(prior_yoy, 1) if prior_yoy is not None else None,
        "trend": trend,
    }


def _classify_earnings_trend(eps_values: list, min_quarters_required: int) -> dict:
    r = _yoy_growth_trend(eps_values, min_quarters_required)
    return {
        "eps_yoy_growth_pct": r["latest_yoy"],
        "eps_yoy_growth_pct_prior": r["prior_yoy"],
        "earnings_trend": r["trend"],
    }


def _classify_sales_trend(sales_values: list, min_quarters_required: int) -> str:
    """Step 15.4: same accelerating/decelerating/declining/insufficient_data
    classification as earnings_trend, applied to the Sales row instead of
    EPS -- per the handoff doc's explicit instruction to classify sales
    'the same way earnings_trend already works'."""
    return _yoy_growth_trend(sales_values, min_quarters_required)["trend"]


MARGIN_STABLE_BAND_PP = 1.0  # OPM% move smaller than this (percentage points) YoY counts as "stable", not noise


def _classify_margin_trend(opm_values: list) -> str:
    """Margin is already a ratio (not a growth quantity like Sales/EPS), so
    this compares the latest quarter's OPM% LEVEL to OPM% one year earlier
    (4 columns back) directly, rather than reusing the YoY-of-YoY growth
    logic above -- a rate-of-change-of-a-rate doesn't map onto "expanding/
    contracting margin" the way it does for Sales/EPS growth."""
    clean = [v for v in opm_values if v is not None]
    if len(opm_values) < 5 or len(clean) < 5:
        return "insufficient_data"
    cur, prior_year = opm_values[-1], opm_values[-5]
    if cur is None or prior_year is None:
        return "insufficient_data"
    delta_pp = cur - prior_year
    if delta_pp > MARGIN_STABLE_BAND_PP:
        return "expanding"
    if delta_pp < -MARGIN_STABLE_BAND_PP:
        return "contracting"
    return "stable"


_ANNUAL_YEAR_RE = re.compile(r"^[A-Za-z]{3} \d{4}$")


def _parse_annual_section_rows(html: str, section_id: str, wanted_labels: set) -> Optional[dict]:
    """Generic parser for screener.in's annual (FY-end, year-keyed)
    sections -- profit-loss and ratios, both used by the Step 15.4
    earnings-quality check below. Unlike _parse_quarters_table, this never
    raises on a missing row: both sections are opportunistic/optional data
    (e.g. banks have no Debtor Days / Inventory Days ratios at all --
    confirmed real: HDFCBANK's ratios section only has ROE %), so a
    missing row degrades to "not computed" rather than blocking the whole
    fundamentals fetch.

    Drops a trailing non-"Mon YYYY" column if present (screener.in's
    profit-loss section appends a 13th "TTM" column after the FY columns;
    ratios does not -- confirmed real on RELIANCE. Dropping it keeps the
    two sections' value lists aligned by fiscal year so latest/prior-year
    comparisons across sections line up correctly)."""
    section = _extract_section(html, section_id)
    if not section:
        return None
    thead_m = re.search(r"<thead>.*?</thead>", section, re.S)
    tbody_m = re.search(r"<tbody>.*?</tbody>", section, re.S)
    if not thead_m or not tbody_m:
        return None
    headers = [_clean_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_m.group(0), re.S)][1:]
    trim = bool(headers) and not _ANNUAL_YEAR_RE.match(headers[-1])

    rows_out = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_m.group(0), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if not cells:
            continue
        label = _clean_text(cells[0]).rstrip("+").strip()
        if label not in wanted_labels:
            continue
        values = [_parse_number(_clean_text(c)) for c in cells[1:]]
        if trim and values:
            values = values[:-1]
        rows_out[label] = values

    return rows_out or None


def _compute_earnings_quality_flag(html: str, multiplier: float) -> tuple:
    """Step 15.4 earnings-quality red flag: rising Inventory/Trade
    Receivables growing meaningfully faster than Sales is a classic
    earnings-quality warning sign (revenue recognized without matching
    cash conversion).

    Real-data note: screener.in's static Balance Sheet HTML does NOT
    reliably expose raw Inventory/Trade Receivables line items -- they're
    rolled into an expandable "Other Assets +" row loaded via JS, not
    present in the fetched HTML (confirmed on RELIANCE, a case the
    original plan assumed would work). The Ratios section's Debtor Days /
    Inventory Days ARE reliably present for non-financial companies
    (confirmed real: RELIANCE) and absent for banks/NBFCs (confirmed
    real: HDFCBANK only has ROE %) -- exactly the split the earnings-
    quality check needs. Since Debtor Days = Receivables/Sales*365 (approx),
    Receivables ~= Debtor Days * Sales / 365, which reconstructs the exact
    receivables-vs-sales YoY comparison the original plan wanted, from
    data that's actually there. Same reconstruction for Inventory Days.

    Returns (flag, detail) where flag is one of
    "flagged"/"clean"/"not_applicable"/"insufficient_data".
    """
    ratios = _parse_annual_section_rows(html, "ratios", {"Debtor Days", "Inventory Days"})
    if not ratios:
        return "not_applicable", "no Debtor/Inventory Days ratios exposed (bank/NBFC-style financials)"

    pl = _parse_annual_section_rows(html, "profit-loss", {"Sales", "Revenue"})
    sales_annual = None
    if pl:
        sales_annual = pl.get("Sales") or pl.get("Revenue")
    if not sales_annual or len(sales_annual) < 2:
        return "insufficient_data", "fewer than 2 years of annual Sales history"

    s_cur, s_prior = sales_annual[-1], sales_annual[-2]
    if s_cur is None or s_prior is None or s_prior == 0:
        return "insufficient_data", "annual Sales figures unavailable for the last 2 fiscal years"
    sales_yoy = (s_cur - s_prior) / abs(s_prior) * 100

    flagged_items = []
    clean_items = []
    for label, name in (("Debtor Days", "receivables"), ("Inventory Days", "inventory")):
        days = ratios.get(label)
        if not days or len(days) < 2:
            continue
        d_cur, d_prior = days[-1], days[-2]
        if d_cur is None or d_prior is None:
            continue
        proxy_cur = d_cur * s_cur / 365
        proxy_prior = d_prior * s_prior / 365
        if proxy_prior == 0:
            continue
        growth = (proxy_cur - proxy_prior) / abs(proxy_prior) * 100
        note = f"{name} {growth:+.0f}% YoY vs sales {sales_yoy:+.0f}% YoY"
        is_flagged = (
            (sales_yoy > 0 and growth > multiplier * sales_yoy)
            or (sales_yoy <= 0 and growth > 0)
        )
        (flagged_items if is_flagged else clean_items).append(note)

    if not flagged_items and not clean_items:
        return "insufficient_data", "fewer than 2 years of Debtor/Inventory Days history"
    if flagged_items:
        return "flagged", "; ".join(flagged_items)
    return "clean", "; ".join(clean_items)


def fetch_fundamentals(
    symbol: str, min_quarters_required: int = 6, earnings_quality_multiplier: float = 2.5
) -> dict:
    """
    Fetch and parse screener.in's consolidated company page for `symbol`.
    Falls back to the standalone page if no consolidated figures exist
    (common for companies without subsidiaries).

    Returns a dict matching db.upsert_fundamentals()'s expected keys.
    """
    urls_to_try = [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]

    html, used_url = None, None
    last_error = None
    for url in urls_to_try:
        try:
            html = _throttled_get(url)
            used_url = url
            break
        except ScraperError as e:
            last_error = e
            continue

    if html is None:
        raise last_error or ScraperError(f"Could not fetch a screener.in page for {symbol}")

    raw_path = RAW_DIR / f"screener_{symbol}_{date.today().strftime('%Y%m%d')}.html"
    raw_path.write_text(html, encoding="utf-8")

    # Parsed independently of the quarters table below so a row-naming
    # quirk there (e.g. a company type this parser doesn't recognize yet)
    # doesn't also lose sector/industry, which come from a separate part
    # of the page.
    sector, industry = _parse_sector(html)

    quarters_section = _extract_section(html, "quarters")
    if not quarters_section:
        raise ScraperError(
            f"No 'quarters' section found on {used_url} -- screener.in's "
            f"page structure may have changed. Raw page saved to {raw_path}."
        )

    try:
        parsed = _parse_quarters_table(quarters_section)
    except ScraperError as e:
        print(
            f"WARNING: could not parse quarterly growth rows for {symbol} "
            f"({e}) -- sector/industry still captured, earnings trend left "
            f"as insufficient_data rather than guessed."
        )
        return {
            "symbol": symbol, "sector": sector, "industry": industry,
            "quarters_json": None, "eps_yoy_growth_pct": None,
            "eps_yoy_growth_pct_prior": None, "profit_yoy_growth_pct": None,
            "earnings_trend": "insufficient_data",
            "sales_trend": "insufficient_data", "margin_trend": "insufficient_data",
            "earnings_quality_flag": "insufficient_data", "earnings_quality_detail": None,
        }

    eps_values = parsed["EPS in Rs"]
    profit_values = parsed["Net Profit"]
    sales_values = parsed["Sales"]
    opm_values = parsed.get("OPM %")  # optional -- absent for banks (Financing Margin % instead)
    trend_info = _classify_earnings_trend(eps_values, min_quarters_required)
    sales_trend = _classify_sales_trend(sales_values, min_quarters_required)
    margin_trend = _classify_margin_trend(opm_values) if opm_values else "not_applicable"
    eq_flag, eq_detail = _compute_earnings_quality_flag(html, earnings_quality_multiplier)

    profit_yoy = None
    clean_profit = [v for v in profit_values if v is not None]
    if len(profit_values) >= 5 and len(clean_profit) >= 5:
        cur, prior_year = profit_values[-1], profit_values[-5]
        if cur is not None and prior_year not in (None, 0):
            profit_yoy = round((cur - prior_year) / abs(prior_year) * 100, 1)

    quarters_json = json.dumps({
        "dates": parsed["dates"],
        "sales": parsed["Sales"],
        "net_profit": parsed["Net Profit"],
        "eps": parsed["EPS in Rs"],
        "opm_pct": opm_values,
        "source_url": used_url,
    })

    print(
        f"Parsed {symbol}: sector={sector}, industry={industry}, "
        f"earnings_trend={trend_info['earnings_trend']} "
        f"(latest EPS YoY={trend_info['eps_yoy_growth_pct']}%, "
        f"prior={trend_info['eps_yoy_growth_pct_prior']}%), "
        f"sales_trend={sales_trend}, margin_trend={margin_trend}, "
        f"earnings_quality={eq_flag} ({eq_detail})"
    )

    return {
        "symbol": symbol,
        "sector": sector,
        "industry": industry,
        "quarters_json": quarters_json,
        "eps_yoy_growth_pct": trend_info["eps_yoy_growth_pct"],
        "eps_yoy_growth_pct_prior": trend_info["eps_yoy_growth_pct_prior"],
        "profit_yoy_growth_pct": profit_yoy,
        "earnings_trend": trend_info["earnings_trend"],
        "sales_trend": sales_trend,
        "margin_trend": margin_trend,
        "earnings_quality_flag": eq_flag,
        "earnings_quality_detail": eq_detail,
    }


if __name__ == "__main__":
    import sys

    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "RELIANCE"
    try:
        result = fetch_fundamentals(symbol)
        print("\n=== Result ===")
        for k, v in result.items():
            if k == "quarters_json":
                continue
            print(f"  {k}: {v}")
    except ScraperError as e:
        print(f"\nSCRAPER ERROR: {e}")
        sys.exit(1)
