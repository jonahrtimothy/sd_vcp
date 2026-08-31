"""
Relative Strength (RS) Rating -- Step 15.3, strategy prompt Section 7
(Aug 31 2026 addition), closing the 8th Trend Template criterion
(Section 2, item 8).

A stock's own trailing price performance, blended across four ~63-trading-
day quarters weighted toward the most recent quarter, then percentile-
ranked 1-99 against the FULL SCANNED UNIVERSE (not a fixed historical
benchmark) -- recomputed every scan run so the ranking stays relative to
current conditions rather than going stale.

**IMPORTANT LIMITATION, stated explicitly per the handoff doc's own
requirement**: IBD's real RS Rating formula is proprietary and not
published in exact form. This is a transparent, documented APPROXIMATION
using the commonly-cited public description of their general methodology
(heaviest weight on the most recent quarter, declining weight further
back) -- it is NOT the real published IBD RS Rating, and a number from
this module should never be presented or mistaken as one. Weights are
tunable in config.yaml's `rs_rating:` block, not hardcoded here.
"""

from typing import Optional

import pandas as pd

QUARTER_LENGTH_DAYS = 63  # ~1 trading quarter


def _quarter_return(df: pd.DataFrame, quarters_back: int) -> Optional[float]:
    """% return over the quarter `quarters_back` periods before the most
    recent close (0 = most recent quarter). None if there isn't enough
    history for that specific quarter -- never interpolated/guessed."""
    end_idx = len(df) - 1 - quarters_back * QUARTER_LENGTH_DAYS
    start_idx = end_idx - QUARTER_LENGTH_DAYS
    if start_idx < 0 or end_idx >= len(df) or end_idx < 0:
        return None
    start_close = df["close"].iloc[start_idx]
    end_close = df["close"].iloc[end_idx]
    if not start_close:
        return None
    return (end_close - start_close) / start_close * 100


def compute_weighted_return(df: pd.DataFrame, weights: list) -> Optional[float]:
    """
    Weighted blend of the last len(weights) quarters' returns (weights[0]
    = most recent quarter). Returns None if ANY of the required quarters
    lacks enough history -- a partial/incomplete blend is not computed,
    matching this project's "insufficient data reports honestly" pattern.
    """
    df = df.reset_index(drop=True)
    quarter_returns = [_quarter_return(df, q) for q in range(len(weights))]
    if any(r is None for r in quarter_returns):
        return None
    return sum(w * r for w, r in zip(weights, quarter_returns))


def compute_rs_ratings(ohlcv_by_symbol: dict, weights: list) -> dict:
    """
    ohlcv_by_symbol: {symbol: ohlcv_df} for the full scanned universe.
    Returns {symbol: rs_rating} (1-99 scale, 99 = strongest) for every
    symbol with enough history to compute a full weighted return. Symbols
    without enough history are simply absent from the result -- never
    given a guessed/default rating.
    """
    weighted_returns = {}
    for symbol, df in ohlcv_by_symbol.items():
        wr = compute_weighted_return(df, weights)
        if wr is not None:
            weighted_returns[symbol] = wr

    if not weighted_returns:
        return {}

    series = pd.Series(weighted_returns)
    percentile = series.rank(pct=True)  # 0.0 (weakest) - 1.0 (strongest) within this universe
    rs_ratings = (percentile * 98 + 1).round().astype(int)  # map to a 1-99 scale
    return rs_ratings.to_dict()
