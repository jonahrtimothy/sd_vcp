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
from stage import classify_stage
from vcp import detect_vcp
from zones import detect_zones
from charts import build_price_chart
from style import conviction_badge, direction_badge, stage_badge


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

    header_cols = st.columns([1.4, 1.1, 1.1, 1.1, 1.1, 1])
    for c, label in zip(header_cols, ["Symbol", "Direction", "Stage", "Score", "Conviction", ""]):
        c.markdown(f"**{label}**")

    for _, row in scan_df.iterrows():
        cols = st.columns([1.4, 1.1, 1.1, 1.1, 1.1, 1])
        cols[0].markdown(f"**{row['symbol']}**")
        cols[1].markdown(direction_badge(row["direction"]), unsafe_allow_html=True)
        cols[2].markdown(stage_badge(row["stage"]), unsafe_allow_html=True)
        cols[3].markdown(f"{row['confluence_score']:.0f}")
        cols[4].markdown(conviction_badge(row["conviction"]), unsafe_allow_html=True)
        if cols[5].button("Inspect", key=f"inspect_{row['id']}"):
            st.session_state["selected_symbol"] = row["symbol"]
            st.session_state["nav"] = "Symbol Detail"
            st.rerun()


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
    n_watchlist = len(cfg["watchlist"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Watchlist size", n_watchlist)
    m2.metric("🟢 High conviction", int(n_high))
    m3.metric("🟡 Medium conviction", int(n_medium))
    m4.metric("🔴 Low conviction", int(n_low))

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

    # selectable symbols = watchlist ∪ anything that already has OHLCV history saved
    conn_symbols = []
    try:
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn_symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol")]
        conn.close()
    except Exception:
        pass
    options = sorted(set(cfg["watchlist"]) | set(conn_symbols))
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

    fig = build_price_chart(df, zones, setup, title=f"{symbol} -- {df['date'].max()}")
    st.plotly_chart(fig, use_container_width=True)

    col_left, col_right = st.columns([1.3, 1])

    with col_left:
        st.subheader("Setup output (Section 9 format)")
        if setup is None:
            st.info(f"No {direction} VCP structure currently detected for {symbol}.")
        elif stage_result is None:
            st.info("Stage classification unavailable (insufficient history) -- confluence verdict withheld.")
        else:
            confluence = compute_confluence(stage_result, zones, setup, symbol=symbol, as_of_date=df["date"].max())
            st.markdown(f"**1. Instrument / Stage:** {symbol} | {stage_badge(stage_result.stage)}", unsafe_allow_html=True)
            st.caption(stage_result.reason)
            st.markdown(f"**2. VCP:** {len(setup.contractions)} contractions, ratio_ok={setup.contraction_ratio_ok}, vol_decay_ok={setup.volume_decay_ok}, quality_score={setup.quality_score:.0f}")
            st.markdown(f"**3. Entry trigger:** {'Close above' if direction == 'bullish' else 'Close below'} **{setup.trigger_level:.2f}** on volume expansion (>= config threshold)")
            base_low = df["low"].iloc[setup.base_start_idx:setup.base_end_idx + 1].min()
            base_high = df["high"].iloc[setup.base_start_idx:setup.base_end_idx + 1].max()
            stop = base_low if direction == "bullish" else base_high
            st.markdown(f"**4. Stop-loss:** {stop:.2f} (opposite side of the base -- setup invalidated if closed beyond this)")
            risk = abs(setup.trigger_level - stop)
            target = setup.trigger_level + 2 * risk if direction == "bullish" else setup.trigger_level - 2 * risk
            st.markdown(f"**5. Target (2R):** {target:.2f} | Risk-reward: 1:2 (illustrative -- size to your own max-risk-per-trade rule)")
            if confluence:
                st.markdown(f"**6. Composite conviction:** {conviction_badge(confluence.conviction)} (weighted score {confluence.weighted_score})", unsafe_allow_html=True)
                st.caption(confluence.notes)
            st.markdown(f"**7. Invalidation:** price closes back inside the base after the trigger, or the pattern widens instead of tightening")
            st.markdown("**8. Data caveats:**")
            if confluence:
                for note in confluence.data_notes:
                    st.caption(f"- {note}")

    with col_right:
        st.subheader("Confluence data")
        if setup is not None and stage_result is not None:
            confluence = compute_confluence(stage_result, zones, setup, symbol=symbol, as_of_date=df["date"].max())
            if confluence:
                c1, c2, c3 = st.columns(3)
                c1.metric("FII/DII", f"{confluence.fii_dii_bonus:+.0f}")
                c2.metric("OI buildup", f"{confluence.oi_buildup_bonus:+.0f}")
                c3.metric("Delivery%", f"{confluence.delivery_bonus:+.0f}")

        fund_row = db.get_fundamentals(symbol)
        st.markdown("**Fundamentals (Section 5)**")
        if fund_row:
            trend = fund_row.get("earnings_trend", "unknown")
            trend_color = {"accelerating": "#22c55e", "decelerating": "#f59e0b", "declining": "#ef4444"}.get(trend, "#94a3b8")
            st.markdown(f"Sector: {fund_row.get('sector') or 'unknown'}")
            st.markdown(f"Industry: {fund_row.get('industry') or 'unknown'}")
            from style import badge
            st.markdown(badge(trend, trend_color), unsafe_allow_html=True)
            if fund_row.get("eps_yoy_growth_pct") is not None:
                st.caption(f"EPS YoY: {fund_row['eps_yoy_growth_pct']}% (prior quarter: {fund_row.get('eps_yoy_growth_pct_prior')}%)")
        else:
            st.caption("Not yet scraped -- runs automatically on the next scan.")


def page_watchlist(cfg: dict):
    st.title("📋 Watchlist Management")
    st.caption("Edits here rewrite the `watchlist:` block in config.yaml directly (comments elsewhere in the file are preserved).")

    current = cfg["watchlist"]
    st.write(f"Currently tracking **{len(current)}** symbols.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Add a symbol")
        new_symbol = st.text_input("NSE trading symbol (e.g. RELIANCE)").strip().upper()
        if st.button("Add") and new_symbol:
            if new_symbol in current:
                st.warning(f"{new_symbol} is already in the watchlist.")
            else:
                update_watchlist(current + [new_symbol])
                st.success(f"Added {new_symbol}. Reloading...")
                st.rerun()

    with col2:
        st.subheader("Remove a symbol")
        to_remove = st.selectbox("Symbol", [""] + current)
        if st.button("Remove") and to_remove:
            update_watchlist([s for s in current if s != to_remove])
            st.success(f"Removed {to_remove}. Reloading...")
            st.rerun()

    st.divider()
    st.subheader("Current watchlist")
    st.dataframe(pd.DataFrame({"symbol": current}), use_container_width=True, hide_index=True)


def page_scan_history():
    st.title("🕘 Scan History")

    dates = db.get_scan_dates()
    if not dates:
        st.info("No scan history yet.")
        return

    chosen = st.selectbox("Scan date", dates)
    scan_df = db.get_scan_results(scan_date=chosen)
    render_scan_table(scan_df)


def main():
    cfg = load_config()

    if "nav" not in st.session_state:
        st.session_state["nav"] = "Daily Scan"

    st.sidebar.title("S&D + VCP Studio")
    st.sidebar.caption("India-first supply/demand + VCP swing research tool. Analytical output only -- never a buy/sell instruction.")
    nav = st.sidebar.radio(
        "Navigate",
        ["Daily Scan", "Symbol Detail", "Watchlist Management", "Scan History"],
        key="nav",
    )

    if nav == "Daily Scan":
        page_daily_scan(cfg)
    elif nav == "Symbol Detail":
        page_symbol_detail(cfg)
    elif nav == "Watchlist Management":
        page_watchlist(cfg)
    elif nav == "Scan History":
        page_scan_history()


if __name__ == "__main__":
    main()
