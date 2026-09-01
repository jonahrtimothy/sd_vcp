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

from stage import classify_stage, MIN_BARS_REQUIRED


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


def _all_contractions(df: pd.DataFrame, direction: Literal["bullish", "bearish"], lookback: int = 2) -> List["Contraction"]:
    """Full chronological contraction list across the whole of `df` --
    factored out of detect_vcp() (Step 15.5) so the historical base-staging
    scan below can walk ALL contractions in a long history, not just the
    most recent few detect_vcp() keeps for a live candidate."""
    df = df.reset_index(drop=True)
    piv_high, piv_low = _pivot_lows_highs(df, lookback=lookback)
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

    return contractions


def _evaluate_contractions(contractions: List["Contraction"], ratio_threshold: float) -> dict:
    """Shared tolerant ratio/volume-decay scoring -- factored out of
    detect_vcp() (Step 15.5) so it can be reused for a live candidate AND
    for each historically-mined base."""
    ratio_checks = []
    for i in range(1, len(contractions)):
        prev_depth = contractions[i - 1].depth_pct
        cur_depth = contractions[i].depth_pct
        if prev_depth <= 0:
            ratio_checks.append(False)
            continue
        ratio_checks.append(cur_depth <= ratio_threshold * prev_depth)
    ratio_fraction = sum(ratio_checks) / len(ratio_checks) if ratio_checks else 0.0

    vols = [c.avg_volume for c in contractions]
    vol_checks = [vols[i] <= vols[i - 1] for i in range(1, len(vols))]
    vol_fraction = sum(vol_checks) / len(vol_checks) if vol_checks else 0.0

    return {
        "ratio_fraction": ratio_fraction,
        "ratio_ok": ratio_fraction >= 0.6,
        "vol_fraction": vol_fraction,
        "vol_ok": vol_fraction >= 0.6,
    }


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

    contractions = _all_contractions(df, direction, lookback=2)
    contractions = contractions[-max_contractions:]
    if len(contractions) < 2:
        return None

    # rule: each contraction depth should be <= threshold * previous depth.
    # Uses TOLERANT/fractional scoring rather than strict all-pass: real
    # market data rarely shrinks perfectly at every single step, and one
    # noisy mid-pattern reversal shouldn't zero out an otherwise legitimate
    # setup. contraction_ratio_ok is True when a majority (>=60%) of steps
    # pass; the underlying fraction feeds the score continuously.
    evaluated = _evaluate_contractions(contractions, contraction_ratio_threshold)
    ratio_fraction, contraction_ratio_ok = evaluated["ratio_fraction"], evaluated["ratio_ok"]
    vol_fraction, volume_decay_ok = evaluated["vol_fraction"], evaluated["vol_ok"]

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


# --- VCP base staging (Step 15.5, Sep 1 2026 addition, Section 4/7) ---
# "Which base are we on since the uptrend started" -- a 1st or 2nd base
# breakout is statistically much more reliable than a 4th+ base (Minervini's
# own base-counting heuristic). Bullish/Stage 2 only: this is fundamentally
# a Stage-2-uptrend concept in the original methodology -- a "base" is a
# pause WITHIN an established advance before continuing higher. There's no
# equivalent standard concept for Stage 4 declines, so bearish setups
# report "not_applicable" here rather than a fabricated mirror.
#
# Design choice made per the Step 15 handoff's own instruction to "pick
# whichever is less invasive... and document the choice": rather than
# persisting a new Stage-transition log table that only starts accumulating
# history from whenever this ships, this DERIVES the transition (and every
# base since it) directly from the OHLCV history already cached per symbol
# -- immediately useful across the whole existing universe rather than
# needing weeks of live scans to accumulate before it means anything, and
# needs no schema change.

def _stage_1_to_2_transition_idx(df: pd.DataFrame, stride: int) -> Optional[int]:
    """Walks classify_stage() forward over `df` at `stride`-day intervals
    (base durations are measured in weeks, so day-level precision isn't
    needed) and returns the index of the LAST Stage 1 -> Stage 2 flip
    found -- i.e. the start of the current advance leg. None if no such
    flip is found in the available history (either the stock has been in
    Stage 2+ for the entire recorded period, or history doesn't reach back
    far enough to see the prior Stage 1)."""
    if len(df) < MIN_BARS_REQUIRED:
        return None

    sample_idxs = list(range(MIN_BARS_REQUIRED - 1, len(df), stride))
    if sample_idxs[-1] != len(df) - 1:
        sample_idxs.append(len(df) - 1)

    stages = [(i, classify_stage(df.iloc[: i + 1]).stage) for i in sample_idxs]

    last_transition = None
    for (prev_i, prev_stage), (cur_i, cur_stage) in zip(stages, stages[1:]):
        if prev_stage == "Stage 1" and cur_stage == "Stage 2":
            last_transition = cur_i
    return last_transition


def _group_into_bases(contractions: List[Contraction], ratio_threshold: float) -> List[List[Contraction]]:
    """Groups a chronological contraction list into distinct bases: a run
    of contractions each shrinking relative to the last one in the current
    group is one base; a contraction that does NOT shrink relative to the
    group's last one closes that group (if it has >=2 contractions, it's a
    qualifying base) and starts a fresh one.

    Deliberately a STRICTER per-step rule than detect_vcp()'s own
    60%-tolerant pass -- detect_vcp evaluates one live candidate a human
    will sanity-check before acting; this mines many historical bases
    unattended, where a stricter bar reduces false-positive base counts."""
    bases: List[List[Contraction]] = []
    current: List[Contraction] = []
    for c in contractions:
        if not current:
            current = [c]
            continue
        prev = current[-1]
        if prev.depth_pct > 0 and c.depth_pct <= ratio_threshold * prev.depth_pct:
            current.append(c)
        else:
            if len(current) >= 2:
                bases.append(current)
            current = [c]
    if len(current) >= 2:
        bases.append(current)
    return bases


def format_base_notation(base_contractions: List[Contraction]) -> str:
    """Renders a qualifying base as (Time)(Depth)(Ticks) text, e.g. "8w22
    over 3T" = an 8-week base, 22% max contraction depth, 3 contractions
    ("ticks"). Shown on Symbol Detail and Section 9 text output."""
    start_idx = base_contractions[0].start_idx
    end_idx = base_contractions[-1].end_idx
    n_bars = end_idx - start_idx + 1
    weeks = max(1, round(n_bars / 5))
    max_depth = max(c.depth_pct for c in base_contractions)
    ticks = len(base_contractions)
    return f"{weeks}w{max_depth:.0f} over {ticks}T"


def count_bases_since_stage2(df: pd.DataFrame, cfg: dict) -> dict:
    """Bullish-only. Returns:
      base_count: int (qualifying bases since the last Stage 1->2
        transition, current forming/active base included) or None
      bases: list of {contractions, notation} dicts, chronological
      transition_idx: the df row position of the Stage 1->2 flip, or None
      reason: populated when base_count is None, explaining why
    """
    stride = cfg.get("base_staging", {}).get("stage_sample_stride_days", 5)
    ratio_threshold = cfg.get("detection", {}).get("vcp_contraction_ratio_threshold", 0.85)

    df = df.reset_index(drop=True)
    transition_idx = _stage_1_to_2_transition_idx(df, stride)
    if transition_idx is None:
        return {
            "base_count": None, "bases": [], "transition_idx": None,
            "reason": "no Stage 1->2 transition found in available history "
                      "(stock may have been in Stage 2+ the whole recorded "
                      "period, or history doesn't reach back far enough)",
        }

    segment = df.iloc[transition_idx:].reset_index(drop=True)
    contractions = _all_contractions(segment, "bullish", lookback=2)
    grouped = _group_into_bases(contractions, ratio_threshold)

    if not grouped:
        return {
            "base_count": None, "bases": [], "transition_idx": transition_idx,
            "reason": "no qualifying (>=2 shrinking contractions) base found "
                      "since the Stage 1->2 transition yet",
        }

    bases = [{"contractions": g, "notation": format_base_notation(g)} for g in grouped]
    return {"base_count": len(bases), "bases": bases, "transition_idx": transition_idx, "reason": None}