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
from sector_mapping import get_sector_index
from stage import StageResult, classify_stage
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
BONUS_NIFTY_ALIGNMENT = _cfg.get("bonus_nifty_alignment", 6.0)
SECTOR_RS_LOOKBACK_DAYS = _cfg.get("sector_rs_lookback_days", 20)
SECTOR_RS_THRESHOLD_PP = _cfg.get("sector_rs_threshold_pp", 2.0)
BONUS_SECTOR_RS = _cfg.get("bonus_sector_rs", 6.0)

NIFTY_SYMBOL = "NIFTY 50"


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
    nifty_alignment_bonus: float
    sector_rs_bonus: float
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
            f"delivery={self.delivery_bonus:+.0f} nifty={self.nifty_alignment_bonus:+.0f} "
            f"sector_rs={self.sector_rs_bonus:+.0f} "
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
    # cash_fii_dii mixes date formats depending on source: NSE's own live
    # endpoint gives 'DD-Mon-YYYY' (e.g. '28-Aug-2026'), trendlyne_scraper.py
    # gives ISO 'YYYY-MM-DD'. Try both rather than assuming one.
    parsed = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    still_unparsed = parsed.isna()
    if still_unparsed.any():
        parsed[still_unparsed] = pd.to_datetime(
            df.loc[still_unparsed, "date"], format="%d-%b-%Y", errors="coerce"
        )
    df["_date"] = parsed
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


def _nifty_alignment_signal(direction: str, as_of_date: str) -> tuple:
    """Does the setup direction agree with NIFTY 50's OWN Stage
    classification? Reuses stage.py as-is on the index's own OHLCV --
    no new analytical logic, just the same engine pointed at the market
    instead of the stock."""
    nifty_df = db.get_ohlcv(NIFTY_SYMBOL, end_date=as_of_date)
    if len(nifty_df) < 210:
        return 0.0, f"insufficient NIFTY 50 history ({len(nifty_df)} bars, need >=210) -- Nifty alignment not computed"

    nifty_stage = classify_stage(nifty_df)
    if nifty_stage.stage == "insufficient_data":
        return 0.0, "NIFTY 50 stage classification unavailable"

    alignment, _ = _classify_alignment(direction, nifty_stage.stage)
    label = f"NIFTY 50 is {nifty_stage.stage}"

    if alignment == "aligned":
        return BONUS_NIFTY_ALIGNMENT, f"{label} - aligned with {direction} setup"
    if alignment == "conflicting":
        return -BONUS_NIFTY_ALIGNMENT, f"{label} - conflicts with {direction} setup"
    return 0.0, f"{label} - neutral relative to {direction} setup"


def _sector_strength_signal(direction: str, symbol: Optional[str], as_of_date: str) -> tuple:
    """Section 5: "is the stock's sector itself currently in favor
    (outperforming the broader index) or out of favor". Toggleable via
    config.yaml's fundamentals.sector_strength_enabled (dashboard
    checkbox) -- re-read fresh each call (not cached at import time like
    the other thresholds above) so toggling takes effect immediately
    without restarting the dashboard."""
    if not load_config().get("fundamentals", {}).get("sector_strength_enabled", True):
        return 0.0, "sector relative strength check disabled (config)"

    if not symbol:
        return 0.0, "no symbol provided, sector relative strength skipped"

    fund = db.get_fundamentals(symbol)
    sector = fund.get("sector") if fund else None
    if not sector:
        return 0.0, f"no cached sector for {symbol} -- relative strength not computed"

    sector_index = get_sector_index(sector)
    if not sector_index:
        return 0.0, f"sector '{sector}' has no mapped NIFTY index -- relative strength not computed"

    as_of = _parse_iso(as_of_date)
    lookback_start = (as_of - timedelta(days=SECTOR_RS_LOOKBACK_DAYS * 2)).isoformat()  # *2: calendar days to comfortably cover N trading days

    sector_df = db.get_ohlcv(sector_index, start_date=lookback_start, end_date=as_of_date)
    nifty_df = db.get_ohlcv(NIFTY_SYMBOL, start_date=lookback_start, end_date=as_of_date)
    if len(sector_df) < SECTOR_RS_LOOKBACK_DAYS or len(nifty_df) < SECTOR_RS_LOOKBACK_DAYS:
        return 0.0, (
            f"insufficient {sector_index}/NIFTY 50 history to compute "
            f"{SECTOR_RS_LOOKBACK_DAYS}-day relative strength "
            f"(have {len(sector_df)}/{len(nifty_df)} days)"
        )

    sector_return = (sector_df["close"].iloc[-1] - sector_df["close"].iloc[-SECTOR_RS_LOOKBACK_DAYS]) / sector_df["close"].iloc[-SECTOR_RS_LOOKBACK_DAYS] * 100
    nifty_return = (nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-SECTOR_RS_LOOKBACK_DAYS]) / nifty_df["close"].iloc[-SECTOR_RS_LOOKBACK_DAYS] * 100
    diff = sector_return - nifty_return

    label = f"{sector_index} {SECTOR_RS_LOOKBACK_DAYS}d return {sector_return:+.1f}% vs NIFTY 50 {nifty_return:+.1f}%"

    if diff >= SECTOR_RS_THRESHOLD_PP:
        rs_state = "outperforming"
    elif diff <= -SECTOR_RS_THRESHOLD_PP:
        rs_state = "underperforming"
    else:
        return 0.0, f"{label} - roughly in line, no clear sector edge"

    # bullish setup wants an in-favor (outperforming) sector; bearish wants
    # an out-of-favor (underperforming) one -- same aligned/conflicting
    # logic as Stage, just applied to sector vs. market instead of stock vs. MAs
    is_aligned = (direction == "bullish" and rs_state == "outperforming") or (direction == "bearish" and rs_state == "underperforming")
    if is_aligned:
        return BONUS_SECTOR_RS, f"{label} - {rs_state} sector aligned with {direction} setup"
    return -BONUS_SECTOR_RS, f"{label} - {rs_state} sector conflicts with {direction} setup"


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
    nifty_bonus, nifty_note = _nifty_alignment_signal(direction, as_of_date)
    sector_rs_bonus, sector_rs_note = _sector_strength_signal(direction, symbol, as_of_date)
    data_notes = [fii_dii_note, oi_note, delivery_note, nifty_note, sector_rs_note]

    weighted = (
        raw_score * multiplier + zone_bonus
        + fii_dii_bonus + oi_bonus + delivery_bonus
        + nifty_bonus + sector_rs_bonus
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
        nifty_alignment_bonus=nifty_bonus,
        sector_rs_bonus=sector_rs_bonus,
        weighted_score=round(weighted, 1),
        conviction=conviction,
        notes=notes,
        data_notes=data_notes,
    )