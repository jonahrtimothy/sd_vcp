"""
Supply/Demand zone detection from OHLCV data.

A zone is identified from swing structure: a sharp, impulsive move away from
a small consolidation/base area leaves that base as a zone. Demand zones form
below rally-base-rally structures; supply zones form above drop-base-drop
structures.

Input: a pandas DataFrame with columns ['date','open','high','low','close','volume'],
sorted ascending by date, with a DatetimeIndex or a 'date' column.
"""

from dataclasses import dataclass, field
from typing import List, Literal
import pandas as pd
import numpy as np


@dataclass
class Zone:
    kind: Literal["demand", "supply"]
    start_idx: int
    end_idx: int
    zone_low: float
    zone_high: float
    origin_move_pct: float          # size of the impulsive move that created this zone
    tests: int = 0                  # number of times price has revisited this zone since formation
    fresh: bool = True              # untested since formation
    stage_at_formation: str = ""    # filled in by stage.py if used together

    def as_dict(self):
        d = self.__dict__.copy()
        return d


def _find_swing_points(df: pd.DataFrame, lookback: int = 3) -> pd.DataFrame:
    """
    Mark local swing highs/lows using a simple fractal rule:
    a swing high is a bar whose high is greater than `lookback` bars on each side;
    symmetric for swing lows. Adds boolean columns 'swing_high' / 'swing_low'.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swing_high = np.zeros(n, dtype=bool)
    swing_low = np.zeros(n, dtype=bool)

    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == lookback:
            swing_high[i] = True
        if lows[i] == window_l.min() and np.argmin(window_l) == lookback:
            swing_low[i] = True

    out = df.copy()
    out["swing_high"] = swing_high
    out["swing_low"] = swing_low
    return out


def detect_zones(
    df: pd.DataFrame,
    lookback: int = 3,
    min_move_pct: float = 8.0,
    max_base_bars: int = 15,
) -> List[Zone]:
    """
    Detect candidate supply/demand zones.

    Logic:
    - Find swing highs/lows.
    - Between consecutive opposite swings, measure the base (consolidation)
      immediately preceding an impulsive move.
    - If the subsequent move away from that base exceeds `min_move_pct`,
      the base region becomes a zone candidate.
      * A base followed by a strong upward impulsive move -> demand zone
      * A base followed by a strong downward impulsive move -> supply zone

    This is intentionally simple/transparent (rule-based, not ML) so it can be
    audited and tuned. Tune `min_move_pct` and `max_base_bars` to the
    instrument's volatility and your timeframe.
    """
    df = df.reset_index(drop=True)
    marked = _find_swing_points(df, lookback=lookback)
    zones: List[Zone] = []

    swing_idxs = marked.index[(marked["swing_high"]) | (marked["swing_low"])].tolist()

    for i in range(len(swing_idxs) - 1):
        a, b = swing_idxs[i], swing_idxs[i + 1]
        if b - a > max_base_bars:
            continue  # too wide to be a tight base

        base_slice = marked.loc[a:b]
        base_low = base_slice["low"].min()
        base_high = base_slice["high"].max()

        # measure the move AWAY from this base (post-base, up to next swing or +lookback*4 bars)
        move_end = min(b + lookback * 4, len(marked) - 1)
        post_slice = marked.loc[b:move_end]
        if post_slice.empty:
            continue

        move_up_pct = (post_slice["high"].max() - base_low) / base_low * 100
        move_down_pct = (base_high - post_slice["low"].min()) / base_high * 100

        if move_up_pct >= min_move_pct and move_up_pct > move_down_pct:
            zones.append(
                Zone(
                    kind="demand",
                    start_idx=a,
                    end_idx=b,
                    zone_low=base_low,
                    zone_high=base_high,
                    origin_move_pct=round(move_up_pct, 2),
                )
            )
        elif move_down_pct >= min_move_pct and move_down_pct > move_up_pct:
            zones.append(
                Zone(
                    kind="supply",
                    start_idx=a,
                    end_idx=b,
                    zone_low=base_low,
                    zone_high=base_high,
                    origin_move_pct=round(move_down_pct, 2),
                )
            )

    zones = _merge_overlapping_zones(zones, max_merge_gap_bars=max_base_bars * 2)
    _mark_tests(df, zones)
    return zones


def _merge_overlapping_zones(zones: List[Zone], max_merge_gap_bars: int = 30) -> List[Zone]:
    """
    Candidate zones from swing-pair scanning often overlap heavily (the
    same real structure gets detected multiple times from different swing
    pairs). Merge same-kind zones into one consolidated zone ONLY when they
    overlap in BOTH price range AND time (their bar-index ranges are within
    `max_merge_gap_bars` of each other).

    Time proximity matters: on longer histories, price can revisit a
    similar level many months later — that's a genuinely separate event
    (a retest, tracked via Zone.tests/fresh), not the same zone. A naive
    price-only merge chains these together transitively (A near B near C
    near D...) into one meaningless zone spanning the whole price history.
    Requiring time proximity too prevents that chaining.
    """
    n = len(zones)
    if n == 0:
        return zones

    # union-find over zone indices, grouped by kind
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def price_overlaps(a: Zone, b: Zone) -> bool:
        return a.zone_low <= b.zone_high and b.zone_low <= a.zone_high

    def time_gap(a: Zone, b: Zone) -> int:
        if a.end_idx < b.start_idx:
            return b.start_idx - a.end_idx
        if b.end_idx < a.start_idx:
            return a.start_idx - b.end_idx
        return 0  # index ranges already overlap in time

    for i in range(n):
        for j in range(i + 1, n):
            if zones[i].kind != zones[j].kind:
                continue
            if price_overlaps(zones[i], zones[j]) and time_gap(zones[i], zones[j]) <= max_merge_gap_bars:
                union(i, j)

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(zones[i])

    merged: List[Zone] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(
            Zone(
                kind=group[0].kind,
                start_idx=min(z.start_idx for z in group),
                end_idx=max(z.end_idx for z in group),
                zone_low=min(z.zone_low for z in group),
                zone_high=max(z.zone_high for z in group),
                origin_move_pct=max(z.origin_move_pct for z in group),
            )
        )
    return merged


def _mark_tests(df: pd.DataFrame, zones: List[Zone]) -> None:
    """For each zone, count how many times price has re-entered the zone
    range AFTER it formed. Updates zone.tests and zone.fresh in place."""
    for z in zones:
        after = df.loc[z.end_idx + 1 :]
        touched = after[(after["low"] <= z.zone_high) & (after["high"] >= z.zone_low)]
        z.tests = len(touched)
        z.fresh = z.tests == 0


def zones_to_dataframe(zones: List[Zone]) -> pd.DataFrame:
    return pd.DataFrame([z.as_dict() for z in zones])