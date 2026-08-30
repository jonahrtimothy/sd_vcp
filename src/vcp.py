"""
Volatility Contraction Pattern (VCP) detection.

Implements the rules from the strategy system prompt (Section 4):
- 2+ (ideally 3+) successive pullbacks within a base
- each contraction shallower than the last (target: <= ~70% of prior depth)
- volume decaying through the contractions
- breakout/breakdown trigger = close beyond the final contraction's
  high/low on volume expansion

NOTE: contraction ratio / volume decay use TOLERANT/fractional scoring
(a majority of steps must pass, not every single one) rather than strict
all-pass — real market data rarely shrinks perfectly at every step, and a
single noisy mid-pattern reversal shouldn't zero out an otherwise
legitimate setup. See _score_vcp.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional
import pandas as pd
import numpy as np


@dataclass
class Contraction:
    start_idx: int
    end_idx: int
    depth_pct: float       # % pullback of this contraction
    avg_volume: float


@dataclass
class VCPSetup:
    direction: Literal["bullish", "bearish"]
    base_start_idx: int
    base_end_idx: int
    contractions: List[Contraction]
    contraction_ratio_ok: bool     # majority (>=60%) of steps shrink cleanly
    volume_decay_ok: bool          # majority (>=60%) of steps show volume decay
    trigger_level: float           # breakout/breakdown level to watch
    quality_score: float           # 0-100 composite score
    status: Literal["forming", "triggered", "failed"] = "forming"

    def summary(self) -> str:
        return (
            f"{self.direction.upper()} VCP | {len(self.contractions)} contractions | "
            f"ratio_ok={self.contraction_ratio_ok} vol_decay_ok={self.volume_decay_ok} | "
            f"trigger={self.trigger_level:.2f} | score={self.quality_score:.0f} | {self.status}"
        )


def _pivot_lows_highs(df: pd.DataFrame, lookback: int = 2):
    """Simple local pivot detection within a base window."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    piv_high = np.zeros(n, dtype=bool)
    piv_low = np.zeros(n, dtype=bool)
    for i in range(lookback, n - lookback):
        if highs[i] == highs[i - lookback : i + lookback + 1].max():
            piv_high[i] = True
        if lows[i] == lows[i - lookback : i + lookback + 1].min():
            piv_low[i] = True
    return piv_high, piv_low


def detect_vcp(
    df: pd.DataFrame,
    direction: Literal["bullish", "bearish"] = "bullish",
    max_contractions: int = 4,
    contraction_ratio_threshold: float = 0.75,
    volume_lookback: int = 20,
) -> Optional[VCPSetup]:
    """
    Scan the most recent `window` of df (should already be sliced to a
    candidate base region, e.g. output from a prior swing-high-to-now slice)
    for a VCP structure.

    direction='bullish': looking for a base under a resistance ceiling,
        with successive pullbacks (from local highs to local lows) shrinking.
    direction='bearish': looking for a base above a support floor,
        with successive bounces (from local lows to local highs) shrinking.
    """
    df = df.reset_index(drop=True)
    if len(df) < 10:
        return None

    piv_high, piv_low = _pivot_lows_highs(df, lookback=2)
    n = len(df)

    contractions: List[Contraction] = []

    if direction == "bullish":
        high_idxs = [i for i in range(n) if piv_high[i]]
        for hi in high_idxs:
            later_lows = [i for i in range(hi, n) if piv_low[i]]
            if not later_lows:
                continue
            lo = later_lows[0]
            depth_pct = (df.loc[hi, "high"] - df.loc[lo, "low"]) / df.loc[hi, "high"] * 100
            avg_vol = df.loc[hi:lo, "volume"].mean()
            contractions.append(Contraction(hi, lo, round(depth_pct, 2), avg_vol))
    else:
        low_idxs = [i for i in range(n) if piv_low[i]]
        for lo in low_idxs:
            later_highs = [i for i in range(lo, n) if piv_high[i]]
            if not later_highs:
                continue
            hi = later_highs[0]
            depth_pct = (df.loc[hi, "high"] - df.loc[lo, "low"]) / df.loc[lo, "low"] * 100
            avg_vol = df.loc[lo:hi, "volume"].mean()
            contractions.append(Contraction(lo, hi, round(depth_pct, 2), avg_vol))

    contractions = contractions[-max_contractions:]
    if len(contractions) < 2:
        return None

    # rule: each contraction depth should be <= threshold * previous depth.
    # Uses TOLERANT/fractional scoring rather than strict all-pass: real
    # market data rarely shrinks perfectly at every single step, and one
    # noisy mid-pattern reversal shouldn't zero out an otherwise legitimate
    # setup. contraction_ratio_ok is True when a majority (>=60%) of steps
    # pass; the underlying fraction feeds the score continuously.
    ratio_checks = []
    for i in range(1, len(contractions)):
        prev_depth = contractions[i - 1].depth_pct
        cur_depth = contractions[i].depth_pct
        if prev_depth <= 0:
            ratio_checks.append(False)
            continue
        ratio_checks.append(cur_depth <= contraction_ratio_threshold * prev_depth)
    ratio_fraction = sum(ratio_checks) / len(ratio_checks) if ratio_checks else 0.0
    contraction_ratio_ok = ratio_fraction >= 0.6

    # same tolerant approach for volume decay
    vols = [c.avg_volume for c in contractions]
    vol_checks = [vols[i] <= vols[i - 1] for i in range(1, len(vols))]
    vol_fraction = sum(vol_checks) / len(vol_checks) if vol_checks else 0.0
    volume_decay_ok = vol_fraction >= 0.6

    base_start_idx = contractions[0].start_idx
    base_end_idx = contractions[-1].end_idx

    if direction == "bullish":
        trigger_level = df.loc[base_start_idx : base_end_idx, "high"].max()
    else:
        trigger_level = df.loc[base_start_idx : base_end_idx, "low"].min()

    quality_score = _score_vcp(contractions, ratio_fraction, vol_fraction)

    return VCPSetup(
        direction=direction,
        base_start_idx=base_start_idx,
        base_end_idx=base_end_idx,
        contractions=contractions,
        contraction_ratio_ok=contraction_ratio_ok,
        volume_decay_ok=volume_decay_ok,
        trigger_level=round(float(trigger_level), 2),
        quality_score=quality_score,
    )


def _score_vcp(contractions: List[Contraction], ratio_fraction: float, vol_fraction: float) -> float:
    """0-100 composite quality score. Transparent, tunable weighting.
    ratio_fraction/vol_fraction are 0.0-1.0 (fraction of steps that passed),
    giving partial credit rather than an all-or-nothing boolean — a single
    noisy step no longer zeroes out an otherwise legitimate setup."""
    score = 0.0
    score += min(len(contractions), 4) / 4 * 30          # more contractions (up to 4) = better, 30 pts max
    score += ratio_fraction * 30                          # clean shrinking ratio = up to 30 pts
    score += vol_fraction * 30                            # volume decay = up to 30 pts
    tightest = contractions[-1].depth_pct
    if tightest <= 5:
        score += 10
    elif tightest <= 10:
        score += 5
    return round(min(score, 100), 1)


def check_trigger(
    df: pd.DataFrame, setup: VCPSetup, volume_multiple: float = 1.5, volume_lookback: int = 20
) -> VCPSetup:
    """
    Check the bars AFTER the base for a valid breakout/breakdown trigger:
    close beyond trigger_level on volume >= volume_multiple * rolling average volume.
    Mutates and returns setup.status.
    """
    df = df.reset_index(drop=True)
    avg_vol = df["volume"].rolling(volume_lookback).mean()
    after = df.loc[setup.base_end_idx + 1 :]

    for i in after.index:
        vol_ok = df.loc[i, "volume"] >= volume_multiple * avg_vol.loc[i] if not np.isnan(avg_vol.loc[i]) else False
        if setup.direction == "bullish":
            if df.loc[i, "close"] > setup.trigger_level and vol_ok:
                setup.status = "triggered"
                return setup
            if df.loc[i, "close"] < df.loc[setup.base_start_idx : setup.base_end_idx, "low"].min():
                setup.status = "failed"
                return setup
        else:
            if df.loc[i, "close"] < setup.trigger_level and vol_ok:
                setup.status = "triggered"
                return setup
            if df.loc[i, "close"] > df.loc[setup.base_start_idx : setup.base_end_idx, "high"].max():
                setup.status = "failed"
                return setup

    return setup