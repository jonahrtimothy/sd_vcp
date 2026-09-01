"""
Risk framework additions (Step 15.7, Sep 1 2026, Section 8/9): a zone-based
exit plan (suggested profit-taking + trailing-stop reference), the reward:risk
floor check, and the gap-risk disaster-plan note.

All FLAG-ONLY / informational -- nothing here excludes a setup or blocks an
action. Consistent with this system's "trade execution stays manual"
philosophy (Section 12/15.7 decision, confirmed with Jonah): a below-floor
R:R gets a badge, not a rejection.
"""

from dataclasses import dataclass
from typing import List, Literal, Optional

from zones import Zone

GAP_RISK_NOTE = (
    "Gap-risk disaster plan: if price gaps beyond the stop-loss (news, an "
    "overnight/weekend gap, or a circuit filter), exit at the earliest "
    "available price once the market reopens rather than waiting for a "
    "recovery -- the stop-loss level assumes a normal intraday fill, not "
    "a gap through it."
)


@dataclass
class ExitPlan:
    target_zone: Optional[Zone]           # nearest FRESH zone beyond entry, suggested first profit-taking level
    trail_zone: Optional[Zone]            # nearest FRESH zone between stop and entry, suggested trailing-stop reference
    reward_risk_ratio: Optional[float]    # None if no qualifying target zone was found
    rr_floor_ok: Optional[bool]           # None mirrors reward_risk_ratio's unavailability

    def summary(self) -> str:
        if self.reward_risk_ratio is None:
            return "R:R unavailable -- no qualifying fresh target zone found beyond entry"
        status = "OK" if self.rr_floor_ok else "BELOW FLOOR"
        return f"R:R 1:{self.reward_risk_ratio:.1f} ({status})"


def suggest_exit_plan(
    direction: Literal["bullish", "bearish"],
    entry_price: float,
    stop_price: float,
    zones: List[Zone],
    min_rr: float = 2.0,
) -> ExitPlan:
    """
    Target: nearest FRESH zone of the opposite kind to the setup's backing
    zone (supply for bullish, demand for bearish) beyond entry -- the next
    real supply/demand imbalance price would have to clear, used as a
    realistic first profit-taking level (via the zone's near/proximal edge,
    not its far/distal extreme).

    Trail: nearest FRESH zone of the setup's OWN backing kind (demand for
    bullish, supply for bearish) that sits strictly BETWEEN the current
    stop and entry -- a closer support/resistance than the initial stop
    that price could trail into as it advances, without being read as the
    initial stop-loss.

    Both reuse zones.py's already-detected zones -- new derived output,
    not new detection logic, per the handoff's own framing.
    """
    target_kind = "supply" if direction == "bullish" else "demand"
    trail_kind = "demand" if direction == "bullish" else "supply"

    if direction == "bullish":
        target_candidates = [z for z in zones if z.kind == target_kind and z.fresh and z.proximal_price > entry_price]
        trail_candidates = [z for z in zones if z.kind == trail_kind and z.fresh and stop_price < z.proximal_price < entry_price]
    else:
        target_candidates = [z for z in zones if z.kind == target_kind and z.fresh and z.proximal_price < entry_price]
        trail_candidates = [z for z in zones if z.kind == trail_kind and z.fresh and entry_price < z.proximal_price < stop_price]

    target_zone = min(target_candidates, key=lambda z: abs(z.proximal_price - entry_price)) if target_candidates else None
    trail_zone = min(trail_candidates, key=lambda z: abs(z.proximal_price - entry_price)) if trail_candidates else None

    risk = abs(entry_price - stop_price)
    reward_risk_ratio = None
    rr_floor_ok = None
    if target_zone is not None and risk > 0:
        reward = abs(target_zone.proximal_price - entry_price)
        reward_risk_ratio = round(reward / risk, 2)
        rr_floor_ok = reward_risk_ratio >= min_rr

    return ExitPlan(
        target_zone=target_zone, trail_zone=trail_zone,
        reward_risk_ratio=reward_risk_ratio, rr_floor_ok=rr_floor_ok,
    )
