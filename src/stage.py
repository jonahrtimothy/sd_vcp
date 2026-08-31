"""
Stage 1-4 trend classification (Weinstein/Minervini-style), per Section 2
of the strategy system prompt.

Uses 50/150/200-day SMA structure and the 200-day SMA's slope to classify
where an instrument sits in its market cycle:
  Stage 1: Basing / accumulation (sideways, MAs flat/converging)
  Stage 2: Advancing / markup (price > rising MAs, stacked bullishly)
  Stage 3: Topping / distribution (MAs flattening after an advance)
  Stage 4: Declining / markdown (price < falling MAs, stacked bearishly)

Requires at least ~210 days of OHLCV data (200-day SMA + a slope window) —
returns 'insufficient_data' if the input is shorter than that.
"""

from dataclasses import dataclass
from typing import Literal, Optional
import pandas as pd
import numpy as np

Stage = Literal["Stage 1", "Stage 2", "Stage 3", "Stage 4", "insufficient_data"]

MIN_BARS_REQUIRED = 210
SLOPE_WINDOW = 10          # bars used to measure SMA200 slope direction
FLAT_SLOPE_THRESHOLD = 0.05  # % change over the slope window considered "flat"


@dataclass
class StageResult:
    stage: Stage
    price: float
    sma50: float
    sma150: float
    sma200: float
    sma200_slope_pct: float   # % change in SMA200 over SLOPE_WINDOW bars
    reason: str                # human-readable explanation of why this stage was chosen

    def summary(self) -> str:
        if self.stage == "insufficient_data":
            return f"insufficient_data — {self.reason}"
        return (
            f"{self.stage} | price={self.price:.2f} "
            f"SMA50={self.sma50:.2f} SMA150={self.sma150:.2f} SMA200={self.sma200:.2f} "
            f"SMA200_slope={self.sma200_slope_pct:+.2f}% | {self.reason}"
        )


def classify_stage(df: pd.DataFrame) -> StageResult:
    """
    df must have a 'close' column, sorted ascending by date (oldest first).
    Classifies based on the LAST row (most recent close) relative to its
    moving average structure.
    """
    df = df.reset_index(drop=True)

    if len(df) < MIN_BARS_REQUIRED:
        return StageResult(
            stage="insufficient_data",
            price=float(df["close"].iloc[-1]) if len(df) else 0.0,
            sma50=0.0, sma150=0.0, sma200=0.0, sma200_slope_pct=0.0,
            reason=f"need >= {MIN_BARS_REQUIRED} bars, got {len(df)}",
        )

    sma50 = df["close"].rolling(50).mean()
    sma150 = df["close"].rolling(150).mean()
    sma200 = df["close"].rolling(200).mean()

    price = float(df["close"].iloc[-1])
    s50 = float(sma50.iloc[-1])
    s150 = float(sma150.iloc[-1])
    s200 = float(sma200.iloc[-1])
    s200_prev = float(sma200.iloc[-1 - SLOPE_WINDOW])
    s200_slope_pct = (s200 - s200_prev) / s200_prev * 100 if s200_prev else 0.0

    rising = s200_slope_pct > FLAT_SLOPE_THRESHOLD
    falling = s200_slope_pct < -FLAT_SLOPE_THRESHOLD
    flat = not rising and not falling

    stacked_bullish = price > s50 > s150 > s200
    stacked_bearish = price < s50 < s150 < s200

    if stacked_bullish and rising:
        stage: Stage = "Stage 2"
        reason = "price above rising, bullishly-stacked MAs (advancing/markup)"
    elif stacked_bearish and falling:
        stage = "Stage 4"
        reason = "price below falling, bearishly-stacked MAs (declining/markdown)"
    elif flat or (not stacked_bullish and not stacked_bearish and rising):
        # ambiguous/converging MA structure — distinguish Stage 1 (base after
        # a decline) from Stage 3 (top after an advance) using recent price
        # position relative to SMA150 as a rough proxy for prior trend direction
        if price >= s150:
            stage = "Stage 3"
            reason = "MAs flattening/converging with price still elevated (topping/distribution)"
        else:
            stage = "Stage 1"
            reason = "MAs flattening/converging with price still depressed (basing/accumulation)"
    elif rising:
        # rising SMA200 but not cleanly stacked — treat as early/late Stage 2
        stage = "Stage 2"
        reason = "SMA200 rising but MA stack not fully aligned (transitional advance)"
    else:  # falling but not cleanly stacked
        stage = "Stage 4"
        reason = "SMA200 falling but MA stack not fully aligned (transitional decline)"

    return StageResult(
        stage=stage, price=price, sma50=s50, sma150=s150, sma200=s200,
        sma200_slope_pct=s200_slope_pct, reason=reason,
    )


# --- 8-point Trend Template (Step 15.3, Aug 31 2026 addition, Section 2) ---
# Items 1-5 (MA structure) are already covered by classify_stage() above;
# this adds items 6-8 (52-week range, RS Rating) as a Trend Template
# pass/fail check ALONGSIDE Stage, not a replacement -- a stock can be
# Stage 2 by MA structure alone yet fail the Trend Template (Minervini's
# own "broken leader" caution).

TREND_TEMPLATE_MIN_BARS = 252    # a full trading year, for the 52-week range check
TREND_TEMPLATE_SLOPE_WINDOW = 21  # ~1 calendar month of trading days
RS_RATING_BULLISH_MIN = 70        # Section 2 item 8: "no less than 70"
RS_RATING_BEARISH_MAX = 100 - RS_RATING_BULLISH_MIN  # mirrored threshold for Stage 4/short candidates
PCT_ABOVE_52W_LOW_MIN = 25.0       # Section 2 item 6
PCT_BELOW_52W_HIGH_MAX = 25.0      # Section 2 item 7


@dataclass
class TrendTemplateResult:
    # individual checks, so Section 9 output can show exactly which one
    # failed rather than collapsing everything into one opaque boolean
    price_above_ma50: bool
    price_above_ma150_ma200: bool
    ma150_above_ma200: bool
    ma50_above_ma150_ma200: bool
    ma200_trending_up_1m: bool
    pct_above_52w_low: float
    pct_above_52w_low_ok: bool       # >= 25% above the 52-week low
    pct_below_52w_high: float
    pct_below_52w_high_ok: bool      # within 25% of the 52-week high
    rs_rating: Optional[int]
    rs_rating_ok: bool                # >= 70 (bullish) -- see clean_stage4 for the mirrored bearish check
    clean_stage2: bool                 # ALL 8 checks pass
    clean_stage4: bool                 # mirrored bearish read: near 52w low + weak RS (MA-structure mirror comes from classify_stage's own Stage 4 read, not duplicated here)
    reason: str

    def summary(self) -> str:
        if self.clean_stage2:
            return f"Clean Trend Template pass (all 8 checks) -- RS Rating {self.rs_rating}"
        return f"Trend Template: {self.reason}"


def classify_trend_template(df: pd.DataFrame, rs_rating: Optional[int] = None) -> TrendTemplateResult:
    """
    df: OHLCV, ascending, >= TREND_TEMPLATE_MIN_BARS (252, ~1 trading year)
    for a real 52-week range read -- returns an honest insufficient-data
    result otherwise, never a guessed one.

    rs_rating: 1-99, computed separately across the whole scanned universe
    (rs_rating.py) since it's inherently a cross-symbol comparison, not
    something a single symbol's OHLCV can produce alone. None if not yet
    computed/available -- the RS check then honestly fails rather than
    being assumed passing.

    Recomputes 50/150/200-day SMAs directly from `df` rather than reusing
    classify_stage()'s StageResult, since this needs its OWN 1-month slope
    window (TREND_TEMPLATE_SLOPE_WINDOW=21) distinct from classify_stage's
    faster-reacting SLOPE_WINDOW=10 -- the two serve different purposes
    (Stage classification vs. this specific Trend Template checkpoint) and
    deliberately are not forced to share one number.
    """
    df = df.reset_index(drop=True)

    if len(df) < TREND_TEMPLATE_MIN_BARS:
        return TrendTemplateResult(
            price_above_ma50=False, price_above_ma150_ma200=False, ma150_above_ma200=False,
            ma50_above_ma150_ma200=False, ma200_trending_up_1m=False,
            pct_above_52w_low=0.0, pct_above_52w_low_ok=False,
            pct_below_52w_high=0.0, pct_below_52w_high_ok=False,
            rs_rating=rs_rating, rs_rating_ok=False,
            clean_stage2=False, clean_stage4=False,
            reason=f"insufficient_data -- need >= {TREND_TEMPLATE_MIN_BARS} bars (1 trading year), got {len(df)}",
        )

    sma50 = df["close"].rolling(50).mean()
    sma150 = df["close"].rolling(150).mean()
    sma200 = df["close"].rolling(200).mean()

    price = float(df["close"].iloc[-1])
    s50, s150, s200 = float(sma50.iloc[-1]), float(sma150.iloc[-1]), float(sma200.iloc[-1])
    s200_prev_month = float(sma200.iloc[-1 - TREND_TEMPLATE_SLOPE_WINDOW])
    ma200_trending_up_1m = s200 > s200_prev_month

    low_52w = float(df["low"].iloc[-TREND_TEMPLATE_MIN_BARS:].min())
    high_52w = float(df["high"].iloc[-TREND_TEMPLATE_MIN_BARS:].max())
    pct_above_52w_low = (price - low_52w) / low_52w * 100 if low_52w else 0.0
    pct_below_52w_high = (high_52w - price) / high_52w * 100 if high_52w else 0.0

    checks = {
        "price_above_ma50": price > s50,
        "price_above_ma150_ma200": price > s150 and price > s200,
        "ma150_above_ma200": s150 > s200,
        "ma50_above_ma150_ma200": s50 > s150 and s50 > s200,
        "ma200_trending_up_1m": ma200_trending_up_1m,
        "pct_above_52w_low_ok": pct_above_52w_low >= PCT_ABOVE_52W_LOW_MIN,
        "pct_below_52w_high_ok": pct_below_52w_high <= PCT_BELOW_52W_HIGH_MAX,
        "rs_rating_ok": rs_rating is not None and rs_rating >= RS_RATING_BULLISH_MIN,
    }
    clean_stage2 = all(checks.values())

    # mirrored bearish read: near the 52-week low + weak RS (the MA-
    # structure mirror is classify_stage()'s own Stage 4 read, not
    # duplicated here -- this just adds the two checks Stage 4 alone
    # doesn't cover)
    clean_stage4 = (
        pct_above_52w_low <= PCT_ABOVE_52W_LOW_MIN
        and rs_rating is not None and rs_rating <= RS_RATING_BEARISH_MAX
    )

    if clean_stage2:
        reason = "clean Stage 2 -- all 8 Trend Template checks pass"
    else:
        failed = [k for k, v in checks.items() if not v]
        reason = f"MA structure only partially clean -- failed: {', '.join(failed)}"

    return TrendTemplateResult(
        price_above_ma50=checks["price_above_ma50"],
        price_above_ma150_ma200=checks["price_above_ma150_ma200"],
        ma150_above_ma200=checks["ma150_above_ma200"],
        ma50_above_ma150_ma200=checks["ma50_above_ma150_ma200"],
        ma200_trending_up_1m=checks["ma200_trending_up_1m"],
        pct_above_52w_low=round(pct_above_52w_low, 1),
        pct_above_52w_low_ok=checks["pct_above_52w_low_ok"],
        pct_below_52w_high=round(pct_below_52w_high, 1),
        pct_below_52w_high_ok=checks["pct_below_52w_high_ok"],
        rs_rating=rs_rating,
        rs_rating_ok=checks["rs_rating_ok"],
        clean_stage2=clean_stage2,
        clean_stage4=clean_stage4,
        reason=reason,
    )