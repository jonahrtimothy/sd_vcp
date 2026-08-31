"""
Supply/Demand zone detection from OHLCV data.

A zone is identified from swing structure: a sharp, impulsive move away from
a small consolidation/base area leaves that base as a zone. Demand zones form
below rally-base-rally structures; supply zones form above drop-base-drop
structures.

**Precision boundaries (Aug 31 2026, Step 15.0)**: a zone's boundary is NOT
the high-to-low range of the whole base -- that reads as a wide, imprecise
box (confirmed as a real problem on real HDFCBANK data: the old zone_low/
zone_high spanned a multi-week range). The correct boundary is built from
the origin candle(s) at the edge of the base, immediately before the
impulsive move -- the same construct ICT traders call an "order block":
  - distal line (true invalidation boundary): the extreme wick of the
    origin candle(s) -- high for supply, low for demand.
  - proximal line (first-reaction boundary): the real-body edge of the
    same candle(s) -- max(open,close) for supply, min(open,close) for demand.
zone_low/zone_high remain min/max(distal, proximal) so existing chart-shading
code keeps working unchanged, but the range itself is now tight, not the
whole base.

Input: a pandas DataFrame with columns ['date','open','high','low','close','volume'],
sorted ascending by date, with a DatetimeIndex or a 'date' column.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional
import pandas as pd
import numpy as np


@dataclass
class Zone:
    kind: Literal["demand", "supply"]
    start_idx: int
    end_idx: int
    distal_price: float             # true invalidation boundary (extreme wick of origin candle(s))
    proximal_price: float           # first-reaction boundary (real-body edge of origin candle(s))
    zone_low: float                 # min(distal_price, proximal_price) -- for chart shading / legacy reads
    zone_high: float                # max(distal_price, proximal_price)
    origin_move_pct: float          # size of the impulsive move that created this zone (0.0 for VCP-anchored zones)
    tests: int = 0                  # number of times price has touched the proximal line since formation
    fresh: bool = True              # untested since formation
    broken: bool = False            # price has CLOSED through the distal line since formation (invalidated, not just tested)
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


def _origin_cluster_bounds(df: pd.DataFrame, edge_idx: int, kind: str, max_cluster: int = 3):
    """
    Walks backward from `edge_idx` (the base's last candle, immediately
    before the impulsive move) building a small cluster of candles whose
    real bodies overlap -- the same "clustered candidates" judgment call
    the pre-existing zone-merge logic already makes, just applied here to
    find a tight origin instead of merging separate zone detections. Capped
    at `max_cluster` candles so this can't balloon back into a whole-base
    range (the exact problem this precision-boundary rework fixes).

    Returns (distal_price, proximal_price, cluster_start_idx).
    """
    n = len(df)
    edge_idx = max(0, min(edge_idx, n - 1))
    idxs = [edge_idx]
    cur_body_low = min(df.at[edge_idx, "open"], df.at[edge_idx, "close"])
    cur_body_high = max(df.at[edge_idx, "open"], df.at[edge_idx, "close"])

    i = edge_idx - 1
    while i >= 0 and len(idxs) < max_cluster:
        b_low = min(df.at[i, "open"], df.at[i, "close"])
        b_high = max(df.at[i, "open"], df.at[i, "close"])
        if b_low <= cur_body_high and b_high >= cur_body_low:
            idxs.append(i)
            cur_body_low = min(cur_body_low, b_low)
            cur_body_high = max(cur_body_high, b_high)
            i -= 1
        else:
            break

    cluster = df.iloc[min(idxs) : max(idxs) + 1]
    if kind == "supply":
        distal = float(cluster["high"].max())
        proximal = float(cluster[["open", "close"]].max(axis=1).max())
    else:
        distal = float(cluster["low"].min())
        proximal = float(cluster[["open", "close"]].min(axis=1).min())
    return distal, proximal, min(idxs)


def zone_from_vcp_contraction(df: pd.DataFrame, direction: str, setup) -> Zone:
    """
    Builds the zone tied to an ACTIVE VCP setup directly from vcp.py's own
    final, tightest contraction -- not an independently-detected origin
    (Section 3, "VCP-anchored zones", Aug 31 2026). This is what keeps the
    VCP's stop-loss and the zone's distal line as the SAME number rather
    than two separately-computed values that could drift apart: callers
    should read `.distal_price` off the returned Zone as the setup's
    stop-loss, not compute it independently.

    direction='bullish' -> a demand zone (support the setup needs to hold
    above). direction='bearish' -> a supply zone (resistance it needs to
    hold below).
    """
    df = df.reset_index(drop=True)
    final = setup.contractions[-1]
    kind = "demand" if direction == "bullish" else "supply"

    cluster = df.iloc[final.start_idx : final.end_idx + 1]
    if kind == "supply":
        distal = float(cluster["high"].max())
        proximal = float(cluster[["open", "close"]].max(axis=1).max())
    else:
        distal = float(cluster["low"].min())
        proximal = float(cluster[["open", "close"]].min(axis=1).min())

    zone = Zone(
        kind=kind, start_idx=final.start_idx, end_idx=final.end_idx,
        distal_price=distal, proximal_price=proximal,
        zone_low=min(distal, proximal), zone_high=max(distal, proximal),
        origin_move_pct=0.0,  # not an impulsive-move zone -- origin is a VCP contraction, not a base breakout
    )
    _mark_tests(df, [zone])
    return zone


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
      the base region becomes a zone candidate, with its distal/proximal
      boundary computed from the origin candle(s) right at the base's edge
      (see `_origin_cluster_bounds`), not the whole base's range.
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
            distal, proximal, origin_start = _origin_cluster_bounds(marked, b, kind="demand")
            zones.append(
                Zone(
                    kind="demand",
                    start_idx=origin_start,
                    end_idx=b,
                    distal_price=distal,
                    proximal_price=proximal,
                    zone_low=min(distal, proximal),
                    zone_high=max(distal, proximal),
                    origin_move_pct=round(move_up_pct, 2),
                )
            )
        elif move_down_pct >= min_move_pct and move_down_pct > move_up_pct:
            distal, proximal, origin_start = _origin_cluster_bounds(marked, b, kind="supply")
            zones.append(
                Zone(
                    kind="supply",
                    start_idx=origin_start,
                    end_idx=b,
                    distal_price=distal,
                    proximal_price=proximal,
                    zone_low=min(distal, proximal),
                    zone_high=max(distal, proximal),
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

    When a group merges, the candidate with the strongest origin_move_pct
    is kept AS-IS (its distal/proximal lines are authoritative) rather than
    blending min/max across the group -- blending would recreate the wide,
    imprecise box this precision-boundary rework (Step 15.0) exists to fix.
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
        best = max(group, key=lambda z: z.origin_move_pct)
        merged.append(
            Zone(
                kind=best.kind,
                start_idx=best.start_idx,
                end_idx=best.end_idx,
                distal_price=best.distal_price,
                proximal_price=best.proximal_price,
                zone_low=best.zone_low,
                zone_high=best.zone_high,
                origin_move_pct=best.origin_move_pct,
            )
        )
    return merged


def _mark_tests(df: pd.DataFrame, zones: List[Zone]) -> None:
    """For each zone, count how many times price has touched the proximal
    line (tested) since it formed, and whether a CLOSE has gone through the
    distal line (broken -- invalidated, a meaningfully different state from
    merely tested). Updates zone.tests/fresh/broken in place."""
    for z in zones:
        after = df.loc[z.end_idx + 1 :]
        touched = after[(after["low"] <= z.zone_high) & (after["high"] >= z.zone_low)]
        z.tests = len(touched)
        z.fresh = z.tests == 0

        if z.kind == "supply":
            broken_rows = after[after["close"] > z.distal_price]
        else:
            broken_rows = after[after["close"] < z.distal_price]
        z.broken = len(broken_rows) > 0


def zones_to_dataframe(zones: List[Zone]) -> pd.DataFrame:
    return pd.DataFrame([z.as_dict() for z in zones])
