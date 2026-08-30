"""
Confluence scoring: combines Stage classification + Zone quality + VCP
score into ONE composite conviction verdict (Section 7/9 of the strategy
prompt).

Design principle (capital preservation first): Stage acts as a GATE/
MULTIPLIER on the VCP score, not an equally-weighted input averaged in.
A technically clean VCP pattern that conflicts with the Stage trend gets
heavily discounted AND has its conviction hard-capped at "Low" — a good
pattern score can never override a bad trend context into a confident
recommendation. This mirrors the strategy prompt's own rule: "Never
present a setup as high conviction on chart pattern alone if confluence
data contradicts it."
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Literal, Optional

import pandas as pd

import db
from config import load_config
from stage import StageResult
from vcp import VCPSetup
from zones import Zone

Alignment = Literal["aligned", "neutral", "conflicting", "unknown"]
Conviction = Literal["Low", "Medium", "High"]

MULTIPLIER_ALIGNED = 1.0
MULTIPLIER_NEUTRAL = 0.65
MULTIPLIER_CONFLICTING = 0.3
MULTIPLIER_UNKNOWN = 0.5

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40

# --- Real confluence data layer (strategy prompt Section 7) ---
# Thresholds/magnitudes now live in config.yaml (`confluence:` section) so
# they can be tuned against real results without touching code. Defaults
# below are used only if a key is missing from config.yaml.
_cfg = load_config().get("confluence", {})
FII_DII_NET_THRESHOLD_CR = _cfg.get("fii_dii_net_threshold_cr", 1000.0)
OI_BUILDUP_LOOKBACK_DAYS = _cfg.get("oi_buildup_lookback_days", 5)
DELIVERY_LOOKBACK_DAYS = _cfg.get("delivery_lookback_days", 5)
DELIVERY_TREND_THRESHOLD_PP = _cfg.get("delivery_trend_threshold_pp", 3.0)
DATA_STALENESS_DAYS = _cfg.get("data_staleness_days", 3)

BONUS_FII_DII = _cfg.get("bonus_fii_dii", 8.0)
BONUS_OI_BUILDUP = _cfg.get("bonus_oi_buildup", 6.0)
BONUS_DELIVERY_TREND = _cfg.get("bonus_delivery_trend", 5.0)


@dataclass
class ConfluenceResult:
    direction: Literal["bullish", "bearish"]
    stage: str
    alignment: Alignment
    multiplier: float
    raw_vcp_score: float
    zone_bonus: float
    fii_dii_bonus: float
    oi_buildup_bonus: float
    delivery_bonus: float
    weighted_score: float
    conviction: Conviction
    notes: str
    data_notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        header = (
            f"{self.direction.upper()} setup | Stage={self.stage} "
            f"alignment={self.alignment} (x{self.multiplier}) | "
            f"raw_vcp={self.raw_vcp_score:.0f} zone={self.zone_bonus:+.0f} "
            f"fii_dii={self.fii_dii_bonus:+.0f} oi={self.oi_buildup_bonus:+.0f} "
            f"delivery={self.delivery_bonus:+.0f} "
            f"-> weighted={self.weighted_score:.1f} | "
            f"CONVICTION: {self.conviction} | {self.notes}"
        )
        if self.data_notes:
            header += "\n  " + "\n  ".join(f"- {n}" for n in self.data_notes)
        return header


def _classify_alignment(direction: str, stage: str) -> tuple:
    if stage == "insufficient_data":
        return "unknown", MULTIPLIER_UNKNOWN

    if direction == "bullish":
        if stage == "Stage 2":
            return "aligned", MULTIPLIER_ALIGNED
        if stage == "Stage 4":
            return "conflicting", MULTIPLIER_CONFLICTING
        return "neutral", MULTIPLIER_NEUTRAL
    else:
        if stage == "Stage 4":
            return "aligned", MULTIPLIER_ALIGNED
        if stage == "Stage 2":
            return "conflicting", MULTIPLIER_CONFLICTING
        return "neutral", MULTIPLIER_NEUTRAL


def _zone_bonus(direction: str, zones: List[Zone]) -> tuple:
    matching_kind = "demand" if direction == "bullish" else "supply"
    matching = [z for z in zones if z.kind == matching_kind]

    if not matching:
        return -10.0, f"no {matching_kind} zone found to back this setup"

    fresh_matching = [z for z in matching if z.fresh]
    if fresh_matching:
        return 5.0, f"backed by a fresh {matching_kind} zone"
    return 0.0, f"backed by a {matching_kind} zone, but it's already been tested (not fresh)"


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _fii_dii_flow_signal(direction: str, as_of_date: str) -> tuple:
    """Market-wide cash FII/DII net flow (Section 7). DII derivatives
    activity is structurally minimal (strategy prompt Section 7 futures-only
    nuance) so this reads cash-market FII/FPI net flow as the primary signal.
    """
    df = db.get_cash_fii_dii()
    if df.empty:
        return 0.0, "no cash FII/DII data available"

    df = df.copy()
    df["_date"] = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["_date"])
    as_of = _parse_iso(as_of_date)
    df = df[df["_date"] <= as_of]
    if df.empty:
        return 0.0, "no cash FII/DII data on or before evaluation date"

    latest_date = df["_date"].max()
    age_days = (as_of - latest_date).days
    row = df[(df["_date"] == latest_date) & (df["category"] == "FII/FPI")]
    if row.empty:
        return 0.0, "no FII/FPI row found in cash_fii_dii data"

    fii_net = float(row.iloc[0]["net_value"])
    label = (
        f"FII cash net {'buy' if fii_net >= 0 else 'sell'} "
        f"Rs{abs(fii_net):,.0f}cr on {latest_date.date()}"
    )
    if age_days > DATA_STALENESS_DAYS:
        return 0.0, f"{label} ({age_days}d old, too stale to use as a live signal)"

    signed = fii_net if direction == "bullish" else -fii_net
    if signed > FII_DII_NET_THRESHOLD_CR:
        return BONUS_FII_DII, f"{label} - aligned with {direction} setup"
    if signed < -FII_DII_NET_THRESHOLD_CR:
        return -BONUS_FII_DII, f"{label} - conflicts with {direction} setup"
    return 0.0, f"{label} - below Rs{FII_DII_NET_THRESHOLD_CR:,.0f}cr threshold, neutral"


def _participant_oi_signal(direction: str, as_of_date: str) -> tuple:
    """FII+Pro stock-futures OI buildup direction (Section 7). Participant
    OI is a market-wide report, not per-symbol, so this is a breadth signal,
    not stock-specific. DII is excluded per the futures-only nuance (DIIs
    are regulated out of speculative derivatives activity).
    """
    df = db.get_participant_oi(symbol="MARKET", instrument="stock_fut")
    if df.empty:
        return 0.0, "no participant OI data available"

    df = df[df["participant"].isin(["FII", "Pro"])].copy()
    df["_date"] = pd.to_datetime(df["date"], errors="coerce")
    as_of = _parse_iso(as_of_date)
    df = df[df["_date"] <= as_of]
    if df.empty:
        return 0.0, "no participant OI data on or before evaluation date"

    lookback_start = as_of - timedelta(days=OI_BUILDUP_LOOKBACK_DAYS)
    window = df[df["_date"] >= lookback_start]
    dates_available = sorted(window["_date"].unique())
    if len(dates_available) < 2:
        latest = df["_date"].max()
        age_days = (as_of - latest).days
        return 0.0, (
            f"only {len(dates_available)} day(s) of FII+Pro stock-fut OI in the "
            f"last {OI_BUILDUP_LOOKBACK_DAYS}d - need >=2 to assess buildup trend "
            f"(latest available: {pd.Timestamp(latest).date()}, {age_days}d old)"
        )

    def net_oi(d):
        day = window[window["_date"] == d]
        longs = day[day["side"] == "long"]["contracts"].sum()
        shorts = day[day["side"] == "short"]["contracts"].sum()
        return longs - shorts

    earliest, latest = dates_available[0], dates_available[-1]
    delta = net_oi(latest) - net_oi(earliest)
    label = (
        f"FII+Pro stock-fut net OI moved {delta:+,.0f} contracts "
        f"({pd.Timestamp(earliest).date()}->{pd.Timestamp(latest).date()})"
    )

    signed = delta if direction == "bullish" else -delta
    if signed > 0:
        return BONUS_OI_BUILDUP, f"{label} - buildup aligned with {direction} setup"
    if signed < 0:
        return -BONUS_OI_BUILDUP, f"{label} - buildup conflicts with {direction} setup"
    return 0.0, f"{label} - flat, no clear buildup"


def _delivery_trend_signal(symbol: Optional[str], as_of_date: str) -> tuple:
    """Per-symbol delivery% trend (Section 7): rising delivery% into a move
    suggests real participation rather than intraday churn, symmetric for
    both bullish and bearish moves — falling delivery% weakens conviction
    either way rather than favoring one direction over the other. (This
    symmetric reading is an engineering interpretation of the strategy
    prompt's bullish-framed example; worth confirming with Jonah.)
    """
    if not symbol:
        return 0.0, "no symbol provided, delivery% trend skipped"

    as_of = _parse_iso(as_of_date)
    lookback_start = (as_of - timedelta(days=DELIVERY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    df = db.get_delivery_pct(symbol, start_date=lookback_start, end_date=as_of_date)
    if len(df) < 2:
        if df.empty:
            return 0.0, f"no delivery% data for {symbol} available"
        return 0.0, (
            f"only 1 day of delivery% for {symbol} in the last "
            f"{DELIVERY_LOOKBACK_DAYS}d ({df.iloc[-1]['date']}: "
            f"{df.iloc[-1]['delivery_pct']:.1f}%) - need >=2 to assess trend"
        )

    first_pct = float(df.iloc[0]["delivery_pct"])
    last_pct = float(df.iloc[-1]["delivery_pct"])
    trend = last_pct - first_pct
    label = (
        f"{symbol} delivery% moved {first_pct:.1f}%->{last_pct:.1f}% "
        f"({df.iloc[0]['date']}->{df.iloc[-1]['date']})"
    )

    if trend >= DELIVERY_TREND_THRESHOLD_PP:
        return BONUS_DELIVERY_TREND, f"{label} - rising, supports genuine participation behind the move"
    if trend <= -DELIVERY_TREND_THRESHOLD_PP:
        return -BONUS_DELIVERY_TREND, f"{label} - falling, suggests more intraday churn than real accumulation"
    return 0.0, f"{label} - roughly flat, no clear signal"


def compute_confluence(
    stage_result: StageResult,
    zones: List[Zone],
    vcp_setup: Optional[VCPSetup],
    symbol: Optional[str] = None,
    as_of_date: Optional[str] = None,
) -> Optional[ConfluenceResult]:
    if vcp_setup is None:
        return None

    direction = vcp_setup.direction
    alignment, multiplier = _classify_alignment(direction, stage_result.stage)
    raw_score = vcp_setup.quality_score

    zone_bonus, zone_note = _zone_bonus(direction, zones)

    if as_of_date is None:
        as_of_date = date.today().isoformat()

    fii_dii_bonus, fii_dii_note = _fii_dii_flow_signal(direction, as_of_date)
    oi_bonus, oi_note = _participant_oi_signal(direction, as_of_date)
    delivery_bonus, delivery_note = _delivery_trend_signal(symbol, as_of_date)
    data_notes = [fii_dii_note, oi_note, delivery_note]

    weighted = (
        raw_score * multiplier + zone_bonus
        + fii_dii_bonus + oi_bonus + delivery_bonus
    )
    weighted = max(0.0, min(100.0, weighted))

    if alignment == "conflicting":
        conviction: Conviction = "Low"
        notes = (
            f"Stage conflicts with setup direction ({zone_note}) - "
            f"conviction hard-capped at Low regardless of score, per strategy rule "
            f"'never present as high conviction if confluence data contradicts it'"
        )
    elif weighted >= HIGH_THRESHOLD:
        conviction = "High"
        notes = zone_note
    elif weighted >= MEDIUM_THRESHOLD:
        conviction = "Medium"
        notes = zone_note
    else:
        conviction = "Low"
        notes = zone_note

    return ConfluenceResult(
        direction=direction,
        stage=stage_result.stage,
        alignment=alignment,
        multiplier=multiplier,
        raw_vcp_score=raw_score,
        zone_bonus=zone_bonus,
        fii_dii_bonus=fii_dii_bonus,
        oi_buildup_bonus=oi_bonus,
        delivery_bonus=delivery_bonus,
        weighted_score=round(weighted, 1),
        conviction=conviction,
        notes=notes,
        data_notes=data_notes,
    )