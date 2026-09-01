"""
S&D + VCP Studio dashboard (Phase 6). Streamlit entrypoint.

Run: streamlit run src/dashboard/app.py

Four pages (SYSTEM_BUILD_PROMPT.md Section 7):
  1. Daily Scan -- ranked scan_results table for the latest scan_date
  2. Symbol Detail -- candlestick chart w/ zones + VCP + Section 9 output
  3. Watchlist Management -- add/remove symbols in config.yaml
  4. Scan History -- browse any past scan_date
"""

import sys
from datetime import date, datetime
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR / "dashboard"))
sys.path.insert(0, str(SRC_DIR / "data"))

import pandas as pd
import streamlit as st

import db
from config import load_config, update_watchlist
from confluence import compute_confluence
from stage import classify_stage, classify_trend_template
from vcp import detect_vcp, check_trigger
from zones import detect_zones, zone_from_vcp_contraction
from risk import suggest_exit_plan, GAP_RISK_NOTE
from charts import build_price_chart
from style import conviction_badge, direction_badge, stage_badge, trigger_badge


def _parse_any_date(raw: str):
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def render_freshness_panel():
    freshness = db.get_data_freshness()
    cols = st.columns(len(freshness))
    today = date.today()
    for col, (source, raw_date) in zip(cols, freshness.items()):
        parsed = _parse_any_date(raw_date)
        if parsed is None:
            col.metric(source, "no data", delta="never fetched", delta_color="off")
            continue
        age_days = (today - parsed).days
        if age_days <= 3:
            color = "normal"
        elif age_days <= 7:
            color = "off"
        else:
            color = "inverse"
        col.metric(source, str(parsed), delta=f"{age_days}d ago", delta_color=color)

st.set_page_config(page_title="S&D + VCP Studio", layout="wide", page_icon="📈")
db.init_db()


def _conviction_sort_key(s: str) -> int:
    return {"High": 0, "Medium": 1, "Low": 2}.get(s, 3)


def render_scan_table(scan_df: pd.DataFrame):
    if scan_df.empty:
        st.info("No scan results yet. Run `python scripts/run_scan.py` after `refresh_data.py`.")
        return

    scan_df = scan_df.copy()
    scan_df["_sort"] = scan_df["conviction"].map(_conviction_sort_key)
    scan_df = scan_df.sort_values(["_sort", "confluence_score"], ascending=[True, False])

    header_cols = st.columns([1.3, 1, 1, 1.2, 1, 1.1, 1])
    for c, label in zip(header_cols, ["Symbol", "Direction", "Stage", "Trigger", "Score", "Conviction", ""]):
        c.markdown(f"**{label}**")

    for _, row in scan_df.iterrows():
        cols = st.columns([1.3, 1, 1, 1.2, 1, 1.1, 1])
        cols[0].markdown(f"**{row['symbol']}**")
        cols[1].markdown(direction_badge(row["direction"]), unsafe_allow_html=True)
        cols[2].markdown(stage_badge(row["stage"]), unsafe_allow_html=True)
        cols[3].markdown(trigger_badge(row.get("trigger_status", "forming")), unsafe_allow_html=True)
        cols[4].markdown(f"{row['confluence_score']:.0f}")
        cols[5].markdown(conviction_badge(row["conviction"]), unsafe_allow_html=True)
        if cols[6].button("Inspect", key=f"inspect_{row['id']}"):
            st.session_state["selected_symbol"] = row["symbol"]
            st.session_state["_nav_override"] = "Symbol Detail"
            st.rerun()


def render_open_positions_panel(cfg: dict):
    """Step 15.7: open position count + total open risk from the manual
    trade log, with a soft/hard color-coded concentration flag -- mirrors
    render_freshness_panel()'s age-based color coding (Step 13). FLAG ONLY:
    never blocks opening a new position, matches this system's
    "trade execution stays manual" philosophy."""
    st.subheader("Open positions (manual trade log)")
    open_df = db.get_trade_log(status="open")
    risk_cfg = cfg.get("risk", {})
    soft_min = risk_cfg.get("concentration_soft_min", 4)
    soft_max = risk_cfg.get("concentration_soft_max", 6)

    n_open = len(open_df)
    total_open_risk = float((open_df["qty"] * (open_df["entry_price"] - open_df["stop"]).abs()).sum()) if n_open else 0.0

    if n_open < soft_min:
        color = "normal"
    elif n_open <= soft_max:
        color = "off"
    else:
        color = "inverse"

    c1, c2 = st.columns(2)
    c1.metric(
        "Open positions", n_open,
        delta=f"soft flag at {soft_min}-{soft_max}" if n_open >= soft_min else None,
        delta_color=color,
    )
    c2.metric("Total open risk (Rs)", f"{total_open_risk:,.0f}")

    if n_open:
        with st.expander(f"{n_open} open position(s) -- click to view / close", expanded=(n_open >= soft_min)):
            for _, row in open_df.iterrows():
                rc1, rc2, rc3, rc4, rc5, rc6 = st.columns([1.2, 1, 1, 1, 1, 1])
                rc1.markdown(f"**{row['symbol']}**")
                rc2.caption(row["vehicle"])
                rc3.caption(f"entry {row['entry_price']:.2f}")
                rc4.caption(f"stop {row['stop']:.2f}")
                rc5.caption(f"qty {row['qty']:.0f}")
                if rc6.button("Close", key=f"close_trade_{row['id']}"):
                    db.close_trade(int(row["id"]), date.today().isoformat())
                    st.rerun()
    else:
        st.caption("No open positions logged. Log a trade from the Position Calculator page once you actually take one.")


def page_daily_scan(cfg: dict):
    st.title("📊 Daily Scan")

    latest_date = db.get_latest_scan_date()
    if not latest_date:
        st.warning("No scans have been run yet.")
        st.code("python scripts/refresh_data.py\npython scripts/run_scan.py", language="bash")
        return

    st.caption(f"Showing scan_date = **{latest_date}** (most recent). This describes setups as of that day's close -- watch for the entry trigger the next time the market is open, not an instant \"buy now\" signal.")

    scan_df = db.get_scan_results(scan_date=latest_date)

    n_high = (scan_df["conviction"] == "High").sum()
    n_medium = (scan_df["conviction"] == "Medium").sum()
    n_low = (scan_df["conviction"] == "Low").sum()

    universe_size = "?"
    try:
        from scanner import UNIVERSE_CACHE_PATH
        import json as _json
        if UNIVERSE_CACHE_PATH.exists():
            cached = _json.loads(UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
            universe_size = len(cached["universe"]) - len(cfg.get("watchlist", []))
    except Exception:
        pass

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scanned universe", universe_size)
    m2.metric("🟢 High conviction", int(n_high))
    m3.metric("🟡 Medium conviction", int(n_medium))
    m4.metric("🔴 Low conviction", int(n_low))

    st.divider()
    render_open_positions_panel(cfg)

    st.divider()
    render_scan_table(scan_df)

    st.divider()
    st.subheader("Data freshness")
    st.caption("Last saved date per source -- if the scheduled evening refresh has been missed for a few days (laptop off, etc.), it'll show up here as a gap rather than silently going stale.")
    render_freshness_panel()

    st.divider()
    st.caption(
        "This backfills EVERYTHING since whatever was last saved -- OHLCV, India VIX, "
        "participant OI, delivery%, and cash FII/DII -- then re-runs the full scan. "
        "Safe to click any time, including after being away for days; it always catches "
        "up rather than assuming a fixed window. Cash FII/DII now comes from Trendlyne "
        "(~1 trading month of real history per fetch) rather than NSE's live-only "
        "endpoint, so even a multi-week gap in that source self-heals -- see "
        "PROJECT_CONTEXT.md for why."
    )
    if st.button("🔄 Backfill everything + re-run scan now", type="primary"):
        with st.spinner("Refreshing data (Kite + NSE) and re-running the scan... this can take a few minutes."):
            try:
                from scanner import refresh_all_data, run_scan
                refresh_all_data(cfg)
                run_scan(cfg)
                st.success("Done. Reloading...")
                st.rerun()
            except Exception as e:
                st.error(f"Refresh/scan failed: {e}")


def page_symbol_detail(cfg: dict):
    st.title("🔍 Symbol Detail")

    # selectable symbols = anything that already has OHLCV history saved
    # (Step 15.2: no more hand-picked watchlist to union in -- the universe
    # is the full F&O list now, so "has data" is the only relevant filter)
    conn_symbols = []
    try:
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn_symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol")]
        conn.close()
    except Exception:
        pass
    options = sorted(conn_symbols)
    if not options:
        st.info("No symbols with OHLCV data yet. Run refresh_data.py first.")
        return

    default_idx = 0
    if st.session_state.get("selected_symbol") in options:
        default_idx = options.index(st.session_state["selected_symbol"])

    symbol = st.selectbox("Symbol", options, index=default_idx)
    st.session_state["selected_symbol"] = symbol

    df = db.get_ohlcv(symbol)
    min_bars = cfg["detection"].get("stage_min_bars", 210)
    if len(df) < min_bars:
        st.warning(f"Only {len(df)} OHLCV bars saved for {symbol} (need >= {min_bars} for a Stage classification). Run refresh_data.py to backfill more history.")
        if df.empty:
            return

    det = cfg["detection"]
    zones = detect_zones(df, lookback=det["zone_lookback"], min_move_pct=det["zone_min_move_pct"], max_base_bars=det["zone_max_base_bars"])
    stage_result = classify_stage(df) if len(df) >= min_bars else None

    direction = st.radio("Direction", ["bullish", "bearish"], horizontal=True)
    setup = detect_vcp(df, direction=direction, contraction_ratio_threshold=det["vcp_contraction_ratio_threshold"])
    vcp_zone = None
    if setup is not None:
        setup = check_trigger(df, setup, volume_multiple=det["vcp_volume_multiple_trigger"])
        # Step 15.0: the zone backing the active setup is anchored to VCP's
        # own final contraction, not independently detected -- stop-loss
        # below reads its distal_price so it can never drift from the chart.
        vcp_zone = zone_from_vcp_contraction(df, direction, setup)
    display_zones = ([vcp_zone] + zones) if vcp_zone else zones

    fig = build_price_chart(df, display_zones, setup, title=f"{symbol} -- {df['date'].max()}")
    st.plotly_chart(fig, use_container_width=True)

    col_left, col_right = st.columns([1.3, 1])

    with col_left:
        st.subheader("Setup output (Section 9 format)")
        if setup is None:
            st.info(f"No {direction} VCP structure currently detected for {symbol}.")
        elif stage_result is None:
            st.info("Stage classification unavailable (insufficient history) -- confluence verdict withheld.")
        else:
            confluence = compute_confluence(stage_result, display_zones, setup, symbol=symbol, as_of_date=df["date"].max(), ohlcv_df=df)

            if setup.status == "failed":
                st.error(
                    "This VCP pattern has ALREADY FAILED -- price closed back inside the base (or "
                    "the pattern widened) since it formed. Per the strategy's own invalidation rule, "
                    "this is 'exit or stand aside,' not a live opportunity -- numbers below are shown "
                    "for reference only, not as something to act on."
                )
            elif setup.status == "triggered":
                st.success(
                    "This setup has ALREADY TRIGGERED -- price has closed beyond the entry level on "
                    "volume expansion, as of the data currently loaded. This describes what already "
                    "happened, not a live signal to act on right now."
                )

            st.markdown(f"**1. Instrument / Stage:** {symbol} | {stage_badge(stage_result.stage)}", unsafe_allow_html=True)
            st.caption(stage_result.reason)
            fund_row_for_rs = db.get_fundamentals(symbol)
            rs_rating = fund_row_for_rs.get("rs_rating") if fund_row_for_rs else None
            tt = classify_trend_template(df, rs_rating)
            if tt.clean_stage2:
                st.caption(f"Trend Template: ✅ clean pass, all 8 checks (RS Rating {tt.rs_rating})")
            else:
                st.caption(
                    f"Trend Template: MA structure alone isn't a clean Stage 2 -- "
                    f"52w low {tt.pct_above_52w_low:+.0f}% (need >=25%), "
                    f"52w high {tt.pct_below_52w_high:.0f}% below (need <=25%), "
                    f"RS Rating {tt.rs_rating if tt.rs_rating is not None else 'unavailable'} (need >=70). "
                    f"Minervini's own 'broken leader' caution: don't treat a stock as recovered just "
                    f"because its moving averages turned up, if it's still far from its highs and weak vs. the market."
                )
            base_suffix = ""
            if confluence and confluence.base_count is not None:
                base_suffix = f" | base #{confluence.base_count} since last Stage 1->2 (x{confluence.base_multiplier} confidence)"
            st.markdown(f"**2. VCP:** {len(setup.contractions)} contractions, ratio_ok={setup.contraction_ratio_ok}, vol_decay_ok={setup.volume_decay_ok}, quality_score={setup.quality_score:.0f}{base_suffix}")
            st.markdown(
                f"**3. Entry trigger:** {'Close above' if direction == 'bullish' else 'Close below'} "
                f"**{setup.trigger_level:.2f}** on volume expansion (>= config threshold) "
                f"{trigger_badge(setup.status)}",
                unsafe_allow_html=True,
            )
            stop = vcp_zone.distal_price
            exit_plan = suggest_exit_plan(
                direction, float(setup.trigger_level), float(stop), display_zones,
                min_rr=cfg.get("risk", {}).get("min_reward_risk_ratio", 2.0),
            )
            if exit_plan.trail_zone is not None:
                trail_note = f" Trailing-stop reference once price advances: {exit_plan.trail_zone.proximal_price:.2f} ({exit_plan.trail_zone.kind} zone)."
            else:
                trail_note = ""
            st.markdown(f"**4. Stop-loss:** {stop:.2f} (the VCP-anchored zone's distal line -- final contraction's extreme wick; setup invalidated if closed beyond this)")
            if trail_note:
                st.caption(trail_note.strip())
            risk_amt = abs(setup.trigger_level - stop)
            target1 = setup.trigger_level + 2 * risk_amt if direction == "bullish" else setup.trigger_level - 2 * risk_amt
            target2 = setup.trigger_level + 3 * risk_amt if direction == "bullish" else setup.trigger_level - 3 * risk_amt
            st.markdown(f"**5. Targets:** T1 (2R) = {target1:.2f}, T2 (3R) = {target2:.2f} | illustrative R-multiples -- size to your own max-risk-per-trade rule")
            if exit_plan.target_zone is not None:
                rr_color = "#22c55e" if exit_plan.rr_floor_ok else "#ef4444"
                from style import badge
                st.markdown(
                    f"Zone-based target: **{exit_plan.target_zone.proximal_price:.2f}** "
                    f"({exit_plan.target_zone.kind} zone) -- {badge(exit_plan.summary(), rr_color)}",
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"Zone-based target: {exit_plan.summary()}")
            st.caption("Sizing sent to the calculator defaults to Futures -- switch to Cash equity there if that's the vehicle for this trade.")
            if st.button("Send to Position Calculator"):
                st.session_state["calc_symbol"] = symbol
                st.session_state["calc_direction"] = direction
                st.session_state["calc_vehicle"] = "Futures"
                st.session_state["calc_entry"] = float(setup.trigger_level)
                st.session_state["calc_stop"] = float(stop)
                st.session_state["calc_target1"] = float(target1)
                st.session_state["calc_target2"] = float(target2)
                st.session_state["calc_prefill_pending"] = True
                st.session_state["_nav_override"] = "Position Calculator"
                st.rerun()
            if confluence:
                st.markdown(f"**6. Composite conviction:** {conviction_badge(confluence.conviction)} (weighted score {confluence.weighted_score})", unsafe_allow_html=True)
                st.caption(confluence.notes)
            st.markdown(f"**7. Invalidation:** price closes back inside the base after the trigger, or the pattern widens instead of tightening")
            st.caption(GAP_RISK_NOTE)
            st.markdown("**8. Data caveats:**")
            if confluence:
                for note in confluence.data_notes:
                    st.caption(f"- {note}")

    with col_right:
        st.subheader("Confluence data")
        if setup is not None and stage_result is not None:
            confluence = compute_confluence(stage_result, display_zones, setup, symbol=symbol, as_of_date=df["date"].max(), ohlcv_df=df)
            if confluence:
                c1, c2, c3 = st.columns(3)
                c1.metric("FII/DII", f"{confluence.fii_dii_bonus:+.0f}")
                c2.metric("OI buildup", f"{confluence.oi_buildup_bonus:+.0f}")
                c3.metric("Delivery%", f"{confluence.delivery_bonus:+.0f}")
                c4, c5, c6 = st.columns(3)
                c4.metric("Nifty alignment", f"{confluence.nifty_alignment_bonus:+.0f}")
                c5.metric("Sector RS", f"{confluence.sector_rs_bonus:+.0f}")
                c6.metric("RS Rating", f"{confluence.rs_rating_bonus:+.0f}")

        fund_row = db.get_fundamentals(symbol)
        st.markdown("**Fundamentals (Section 5)**")
        if fund_row:
            from style import badge
            trend = fund_row.get("earnings_trend", "unknown")
            trend_color = {"accelerating": "#22c55e", "decelerating": "#f59e0b", "declining": "#ef4444"}.get(trend, "#94a3b8")
            st.markdown(f"Sector: {fund_row.get('sector') or 'unknown'}")
            st.markdown(f"Industry: {fund_row.get('industry') or 'unknown'}")
            st.markdown(badge(f"earnings {trend}", trend_color), unsafe_allow_html=True)
            if fund_row.get("eps_yoy_growth_pct") is not None:
                st.caption(f"EPS YoY: {fund_row['eps_yoy_growth_pct']}% (prior quarter: {fund_row.get('eps_yoy_growth_pct_prior')}%)")

            # Step 15.4: sales/margin acceleration + earnings-quality --
            # additive, flag-only info (never excludes a symbol), shown as
            # a warning badge only when it actually conflicts/trips, per
            # the severity decision in the Step 15 handoff.
            sales_trend = fund_row.get("sales_trend")
            margin_trend = fund_row.get("margin_trend")
            eq_flag = fund_row.get("earnings_quality_flag")
            eq_detail = fund_row.get("earnings_quality_detail")

            badge_row = []
            if sales_trend and sales_trend not in ("insufficient_data",):
                sales_color = {"accelerating": "#22c55e", "decelerating": "#f59e0b", "declining": "#ef4444"}.get(sales_trend, "#94a3b8")
                badge_row.append(badge(f"sales {sales_trend}", sales_color))
            if margin_trend and margin_trend not in ("insufficient_data", "not_applicable"):
                margin_color = {"expanding": "#22c55e", "stable": "#94a3b8", "contracting": "#ef4444"}.get(margin_trend, "#94a3b8")
                badge_row.append(badge(f"margin {margin_trend}", margin_color))
            if badge_row:
                st.markdown(" ".join(badge_row), unsafe_allow_html=True)

            conflicting = sales_trend in ("declining",) and trend in ("accelerating", "decelerating")
            if eq_flag == "flagged" or conflicting:
                warn_bits = []
                if eq_flag == "flagged":
                    warn_bits.append(f"earnings-quality: {eq_detail}")
                if conflicting:
                    warn_bits.append(f"sales trend ({sales_trend}) conflicts with earnings trend ({trend})")
                st.markdown(badge("⚠ quality warning", "#ef4444"), unsafe_allow_html=True)
                st.caption(" -- ".join(warn_bits))
        else:
            st.caption("Not yet scraped -- runs automatically on the next scan.")


def page_watchlist(cfg: dict):
    st.title("🚫 Excluded Symbols")
    st.caption(
        "Since Step 15.2, the scanned universe is the full NSE F&O-eligible list "
        "(~210 stocks, sourced live from Kite -- self-updating as NSE adds/removes "
        "names), not a hand-picked watchlist. This page is now an EXCLUDE list: "
        "symbols to skip even though they're F&O-eligible. Edits rewrite the "
        "`watchlist:` block in config.yaml directly (comments elsewhere preserved)."
    )

    excluded = cfg["watchlist"]

    try:
        from scanner import UNIVERSE_CACHE_PATH
        import json as _json
        if UNIVERSE_CACHE_PATH.exists():
            cached = _json.loads(UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
            universe_size = len(cached["universe"])
            m1, m2, m3 = st.columns(3)
            m1.metric("Full F&O universe", universe_size)
            m2.metric("Excluded", len(excluded))
            m3.metric("Currently scanning", universe_size - len([s for s in excluded if s in cached["universe"]]))
        else:
            st.info("F&O universe not fetched yet -- run `refresh_data.py` once to populate it.")
    except Exception:
        pass

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Add an exclusion")
        new_symbol = st.text_input("NSE trading symbol (e.g. RELIANCE)").strip().upper()
        if st.button("Exclude") and new_symbol:
            if new_symbol in excluded:
                st.warning(f"{new_symbol} is already excluded.")
            else:
                update_watchlist(excluded + [new_symbol])
                st.success(f"Excluded {new_symbol}. Reloading...")
                st.rerun()

    with col2:
        st.subheader("Remove an exclusion")
        to_remove = st.selectbox("Symbol", [""] + excluded)
        if st.button("Un-exclude") and to_remove:
            update_watchlist([s for s in excluded if s != to_remove])
            st.success(f"{to_remove} will be scanned again. Reloading...")
            st.rerun()

    st.divider()
    st.subheader("Currently excluded")
    if excluded:
        st.dataframe(pd.DataFrame({"symbol": excluded}), use_container_width=True, hide_index=True)
    else:
        st.caption("Nothing excluded -- the full F&O universe is being scanned.")

    st.divider()
    st.subheader("⚙️ Confluence settings")
    sector_enabled = cfg.get("fundamentals", {}).get("sector_strength_enabled", True)
    new_val = st.checkbox(
        "Include sector relative strength in confluence scoring",
        value=sector_enabled,
        help=(
            "Section 5: is the stock's sector currently outperforming or "
            "underperforming NIFTY 50? On by default, but kept toggleable "
            "since it's a softer, more debatable signal than the earnings-"
            "growth filter. Turning it off makes this input contribute 0 "
            "to every score (not hidden, just not scored) -- takes effect "
            "on the next scan/calculation, no restart needed."
        ),
    )
    if new_val != sector_enabled:
        from config import set_sector_strength_enabled
        set_sector_strength_enabled(new_val)
        st.success(f"Sector relative strength scoring turned {'ON' if new_val else 'OFF'}.")
        st.rerun()

    rs_enabled = cfg.get("rs_rating", {}).get("enabled", True)
    new_rs_val = st.checkbox(
        "Include RS Rating in confluence scoring",
        value=rs_enabled,
        help=(
            "Step 15.6: RS Rating (Step 15.3, an approximation of IBD's "
            "proprietary metric -- see src/rs_rating.py) as a confluence "
            "signal. On by default, but kept toggleable in case the RS "
            "Rating formula itself is still being calibrated and you'd "
            "rather it not affect scores yet. Turning it off makes this "
            "input contribute 0 to every score (not hidden, just not "
            "scored) -- takes effect on the next scan/calculation, no "
            "restart needed."
        ),
    )
    if new_rs_val != rs_enabled:
        from config import set_rs_rating_enabled
        set_rs_rating_enabled(new_rs_val)
        st.success(f"RS Rating scoring turned {'ON' if new_rs_val else 'OFF'}.")
        st.rerun()


def page_scan_history():
    st.title("🕘 Scan History")

    dates = db.get_scan_dates()
    if not dates:
        st.info("No scan history yet.")
        return

    chosen = st.selectbox("Scan date", dates)
    scan_df = db.get_scan_results(scan_date=chosen)
    render_scan_table(scan_df)


def page_calculator(cfg: dict):
    st.title("🧮 Position Calculator")
    st.caption(
        "Position-sizing planning tool only -- not investment advice, and nothing "
        "you type here is saved anywhere (no file, no database, no persistence "
        "across sessions). Notional value shown is illustrative (quantity x price), "
        "NOT your broker's actual SPAN+exposure margin requirement -- always check "
        "your broker platform before placing a trade."
    )

    # Any session_state write to a *_w key below MUST happen here, before the
    # widget owning that key is instantiated further down -- same rule as
    # the _nav_override fix: a key already bound to a widget instantiated
    # in this run can't be reassigned directly (Streamlit raises
    # StreamlitAPIException). This block handles both the one-time prefill
    # from "Send to Position Calculator" (Symbol Detail) and the "Fetch
    # from Kite" button's lot-size update, which triggers its own rerun.
    if st.session_state.pop("calc_prefill_pending", False):
        st.session_state["calc_symbol_w"] = st.session_state.pop("calc_symbol", "")
        st.session_state["calc_direction_w"] = st.session_state.pop("calc_direction", "bullish")
        # Default Futures on arrival from Symbol Detail -- matches the
        # existing pre-Step-15.1 behavior; Jonah switches manually for a
        # cash idea rather than the tool trying to infer intent (Step 15.1).
        st.session_state["calc_vehicle_w"] = st.session_state.pop("calc_vehicle", "Futures")
        st.session_state["calc_entry_w"] = float(st.session_state.pop("calc_entry", 0.0))
        st.session_state["calc_stop_w"] = float(st.session_state.pop("calc_stop", 0.0))
        st.session_state["calc_target1_w"] = float(st.session_state.pop("calc_target1", 0.0))
        st.session_state["calc_target2_w"] = float(st.session_state.pop("calc_target2", 0.0))
        symbol_for_lot = st.session_state.get("calc_symbol_w", "")
        if symbol_for_lot:
            try:
                from data.kite_ohlcv import get_lot_size
                fetched = get_lot_size(symbol_for_lot.upper())
                if fetched:
                    st.session_state["calc_lot_size_w"] = fetched
                    st.session_state["_calc_lot_msg"] = ("success", f"Fetched real lot size for {symbol_for_lot.upper()} from Kite: {fetched}")
                else:
                    st.session_state["_calc_lot_msg"] = ("warning", f"Could not find a Kite F&O lot size for {symbol_for_lot.upper()} -- enter it manually.")
            except Exception as e:
                st.session_state["_calc_lot_msg"] = ("warning", f"Lot size lookup failed ({e}) -- enter it manually.")

    if "_calc_lot_fetch_pending" in st.session_state:
        st.session_state["calc_lot_size_w"] = st.session_state.pop("_calc_lot_fetch_pending")

    # One-time defaults for capital/risk_pct -- these are never programmatically
    # set elsewhere (pure user input), but still need a stable key: without one,
    # a rerun triggered by an UNRELATED button (e.g. "Fetch from Kite") was
    # found to silently reset them to their bare defaults, wiping out capital
    # the user had already typed. Same root cause as the lot-size bug found
    # and fixed in Step 12 -- any widget whose value must survive a
    # same-page rerun triggered by other code needs a real key.
    if "calc_capital_w" not in st.session_state:
        st.session_state["calc_capital_w"] = 0.0
    if "calc_risk_pct_w" not in st.session_state:
        st.session_state["calc_risk_pct_w"] = 1.0

    lot_msg = st.session_state.pop("_calc_lot_msg", None)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Trade setup")
        vehicle = st.radio(
            "Vehicle", ["Futures", "Cash equity"], horizontal=True, key="calc_vehicle_w",
            help="Futures: sized in lot multiples, real lot size fetchable from Kite. "
                 "Cash equity: plain share count, no lot rounding, no leverage/margin math.",
        )
        symbol = st.text_input("Symbol (optional, for lot-size lookup)", key="calc_symbol_w")
        direction = st.radio("Direction", ["bullish", "bearish"], horizontal=True, key="calc_direction_w")
        entry = st.number_input("Entry price", min_value=0.0, format="%.2f", key="calc_entry_w")
        stop = st.number_input("Stop-loss price", min_value=0.0, format="%.2f", key="calc_stop_w")
        target1 = st.number_input("Target 1", min_value=0.0, format="%.2f", key="calc_target1_w")
        target2 = st.number_input("Target 2", min_value=0.0, format="%.2f", key="calc_target2_w")

        lot_size = 1
        if vehicle == "Futures":
            lot_col1, lot_col2 = st.columns([2, 1])
            lot_size = lot_col1.number_input("Lot size", min_value=1, step=1, key="calc_lot_size_w")
            if lot_msg:
                level, text = lot_msg
                getattr(st, level)(text)
            if lot_col2.button("Fetch from Kite", help="Look up the real stock-futures lot size for the symbol above"):
                if not symbol:
                    st.warning("Enter a symbol first.")
                else:
                    try:
                        from data.kite_ohlcv import get_lot_size
                        fetched = get_lot_size(symbol.upper())
                        if fetched:
                            st.session_state["_calc_lot_fetch_pending"] = fetched
                            st.rerun()
                        else:
                            st.warning(f"No F&O lot size found for {symbol.upper()}.")
                    except Exception as e:
                        st.warning(f"Lookup failed: {e}")
        else:
            st.caption("Cash equity mode: plain share count from risk % and stop distance -- no lot rounding, no margin/leverage.")

    with col2:
        st.subheader("Risk & capital")
        capital = st.number_input("Capital available (Rs)", min_value=0.0, step=10000.0, format="%.2f", key="calc_capital_w")
        risk_pct = st.number_input("Max risk per trade (%)", min_value=0.01, max_value=100.0, step=0.25, key="calc_risk_pct_w")

        risk_per_share = abs(entry - stop)

        st.divider()
        st.subheader("Suggested position size")

        if vehicle == "Futures":
            risk_per_lot = risk_per_share * lot_size
            if risk_per_lot <= 0 or capital <= 0:
                st.info("Enter entry, stop, lot size, and capital to compute a suggested position size.")
                max_lots = 0
                suggested_qty = 0
                actual_risk_amount = 0.0
            else:
                max_risk_amount = capital * risk_pct / 100
                max_lots = int(max_risk_amount // risk_per_lot)
                suggested_qty = max_lots * lot_size
                actual_risk_amount = max_lots * risk_per_lot

                m1, m2, m3 = st.columns(3)
                m1.metric("Lots", max_lots)
                m2.metric("Quantity", suggested_qty)
                m3.metric("Actual risk (Rs)", f"{actual_risk_amount:,.0f}")

                notional = suggested_qty * entry
                st.caption(f"Illustrative notional value: Rs{notional:,.0f} (quantity x entry price -- not your real margin requirement)")
        else:
            if risk_per_share <= 0 or capital <= 0:
                st.info("Enter entry, stop, and capital to compute a suggested position size.")
                suggested_qty = 0
                actual_risk_amount = 0.0
            else:
                max_risk_amount = capital * risk_pct / 100
                suggested_qty = int(max_risk_amount // risk_per_share)
                actual_risk_amount = suggested_qty * risk_per_share

                m1, m2 = st.columns(2)
                m1.metric("Shares", suggested_qty)
                m2.metric("Actual risk (Rs)", f"{actual_risk_amount:,.0f}")

                capital_required = suggested_qty * entry
                st.caption(f"Capital required: Rs{capital_required:,.0f} (quantity x entry price, full payment -- cash equity has no leverage/margin in this calculation)")

    st.divider()
    st.subheader("Targets")
    if risk_per_share <= 0:
        st.info("Enter a valid entry and stop-loss (different prices) to compute risk-reward.")
    else:
        t1_col, t2_col = st.columns(2)
        for col, target, label in [(t1_col, target1, "Target 1"), (t2_col, target2, "Target 2")]:
            with col:
                if target <= 0:
                    st.caption(f"{label}: not set")
                    continue
                rr = abs(target - entry) / risk_per_share
                pnl = suggested_qty * abs(target - entry)
                st.metric(f"{label} R:R", f"1:{rr:.1f}")
                st.caption(f"Potential P&L at {label} (Rs{target:.2f}, full sized position): Rs{pnl:,.0f}")

    st.divider()
    st.subheader("📝 Log this trade")
    st.caption(
        "Step 15.7: the ONLY record of which setups you actually took vs. what the "
        "scanner surfaced -- purely manual, no broker sync, no automation. Backs the "
        "portfolio-concentration flag on the Daily Scan page. Logs the position size "
        "computed above (quantity), not a separate entry."
    )
    entry_date = st.date_input("Entry date", value=date.today(), key="calc_log_date_w")
    if st.button("Log this trade"):
        if not symbol:
            st.warning("Enter a symbol above before logging.")
        elif risk_per_share <= 0 or suggested_qty <= 0:
            st.warning("Enter a valid entry, stop-loss, and capital (a real suggested quantity > 0) before logging.")
        else:
            db.add_trade_log_entry({
                "symbol": symbol.upper(), "vehicle": vehicle,
                "entry_date": entry_date.isoformat(), "entry_price": entry,
                "stop": stop, "qty": suggested_qty,
            })
            st.success(f"Logged: {symbol.upper()} {vehicle}, qty {suggested_qty} @ {entry:.2f}, stop {stop:.2f}. See the Daily Scan page for your open positions.")


def main():
    cfg = load_config()

    if "nav" not in st.session_state:
        st.session_state["nav"] = "Daily Scan"
    if "_nav_override" in st.session_state:
        # Cross-page nav (e.g. an "Inspect"/"Send to Calculator" button) must
        # land here BEFORE the radio widget below is instantiated -- once a
        # widget owns a session_state key for a run, that key can't be
        # reassigned directly from within the same run.
        st.session_state["nav"] = st.session_state.pop("_nav_override")

    st.sidebar.title("S&D + VCP Studio")
    st.sidebar.caption("India-first supply/demand + VCP swing research tool. Analytical output only -- never a buy/sell instruction.")
    nav = st.sidebar.radio(
        "Navigate",
        ["Daily Scan", "Symbol Detail", "Position Calculator", "Excluded Symbols", "Scan History"],
        key="nav",
    )

    if nav == "Daily Scan":
        page_daily_scan(cfg)
    elif nav == "Position Calculator":
        page_calculator(cfg)
    elif nav == "Symbol Detail":
        page_symbol_detail(cfg)
    elif nav == "Excluded Symbols":
        page_watchlist(cfg)
    elif nav == "Scan History":
        page_scan_history()


if __name__ == "__main__":
    main()
