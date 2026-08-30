"""
Fundamental quality filter (Section 5 of the strategy prompt): a minimum
quality bar applied BEFORE the technical scan narrows the universe, not a
deep valuation exercise.

Data source: screener.in via data/screener_scraper.py. Cached locally
(fundamentals table) and only re-scraped once the cache is older than
`cache_max_age_days` (config.yaml, default 7) -- earnings growth and
sector classification don't change day to day, so there's no reason to
hit screener.in on every daily scan run.

Scope note: only the earnings-growth-trend half of Section 5 is
implemented. "Sector relative strength" (is the stock's sector currently
outperforming the broader index) needs sector-index price history, a
data source not wired in yet -- flagged, not silently assumed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import db
from config import load_config
from data.screener_scraper import fetch_fundamentals, ScraperError

EarningsTrend = Literal["accelerating", "decelerating", "declining", "insufficient_data"]


@dataclass
class FundamentalsResult:
    symbol: str
    sector: Optional[str]
    industry: Optional[str]
    earnings_trend: EarningsTrend
    eps_yoy_growth_pct: Optional[float]
    eligible: bool
    notes: str

    def summary(self) -> str:
        return (
            f"{self.symbol}: sector={self.sector or 'unknown'} "
            f"earnings_trend={self.earnings_trend} "
            f"(EPS YoY={self.eps_yoy_growth_pct}%) -> "
            f"{'ELIGIBLE' if self.eligible else 'EXCLUDED'} | {self.notes}"
        )


def _cache_is_fresh(fetched_at: Optional[str], max_age_days: int) -> bool:
    if not fetched_at:
        return False
    age_days = (datetime.now() - datetime.fromisoformat(fetched_at)).days
    return age_days <= max_age_days


def apply_fundamental_filter(symbol: str, force_refresh: bool = False) -> FundamentalsResult:
    """
    Section 5: "fundamentals decide eligibility, technicals decide timing."
    A 'declining' earnings trend excludes the symbol from the technical
    scan. 'insufficient_data' does NOT exclude -- we can't fabricate a
    verdict from missing data, so it passes through flagged as unverified
    rather than silently assumed clean.
    """
    cfg = load_config().get("fundamentals", {})
    max_age_days = cfg.get("cache_max_age_days", 7)
    min_quarters = cfg.get("min_quarters_required", 6)

    stale_note = ""
    cached = db.get_fundamentals(symbol)
    if cached and not force_refresh and _cache_is_fresh(cached.get("fetched_at"), max_age_days):
        record = cached
    else:
        try:
            record = fetch_fundamentals(symbol, min_quarters_required=min_quarters)
            db.upsert_fundamentals(record)
        except ScraperError as e:
            if not cached:
                return FundamentalsResult(
                    symbol=symbol, sector=None, industry=None,
                    earnings_trend="insufficient_data", eps_yoy_growth_pct=None,
                    eligible=True,
                    notes=f"fundamentals unavailable ({e}) -- passed through unverified, not excluded",
                )
            record = cached
            stale_note = f" (re-scrape failed: {e} -- using stale cache from {cached.get('fetched_at')})"

    trend = record.get("earnings_trend", "insufficient_data")
    eligible = trend != "declining"

    if trend == "declining":
        notes = "excluded: declining YoY EPS growth (Section 5 quality bar)"
    elif trend == "insufficient_data":
        notes = "insufficient earnings history to judge trend -- passed through unverified, not excluded"
    else:
        notes = f"{trend} YoY EPS growth"
    notes += stale_note

    return FundamentalsResult(
        symbol=symbol,
        sector=record.get("sector"),
        industry=record.get("industry"),
        earnings_trend=trend,
        eps_yoy_growth_pct=record.get("eps_yoy_growth_pct"),
        eligible=eligible,
        notes=notes,
    )


if __name__ == "__main__":
    import sys

    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "RELIANCE"
    db.init_db()
    result = apply_fundamental_filter(symbol)
    print(result.summary())
