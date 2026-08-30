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
from typing import Literal
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