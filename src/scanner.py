"""
Universe scanner (Phase 5): orchestrates the full pipeline -- data refresh
(with gap backfill) then fundamentals -> zones -> VCP -> stage -> confluence
-- across the full NSE F&O universe (Step 15.2: sourced live from Kite's
own instruments master, ~210 stocks, minus config.yaml's `watchlist` which
is now an EXCLUDE list, not the scanned universe itself), persisting
results so the dashboard has something to read.

Two entrypoints, matching SYSTEM_BUILD_PROMPT.md Section 6's intended daily
flow (thin CLI wrappers live in scripts/refresh_data.py and scripts/run_scan.py):
  refresh_all_data(cfg) -- catches up OHLCV/VIX (Kite, full backfill) and
      participant_oi/delivery_pct (NSE archives, backfillable) since
      whatever was last saved; cash FII/DII is fetched live-only (NSE has
      no confirmed historical endpoint for it -- see PROJECT_CONTEXT.md).
  run_scan(cfg) -- for each universe symbol: fundamentals filter first
      (Section 5 -- decides eligibility before technicals run at all),
      then zones + VCP (both directions) + stage + confluence, persisted
      to zones/vcp_setups/scan_results.

Both continue past a single symbol's failure (log it, skip it) rather than
crashing the whole run (SYSTEM_BUILD_PROMPT.md Section 10).
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

import db
from config import load_config
from confluence import compute_confluence
from fundamentals import apply_fundamental_filter
from stage import classify_stage
from vcp import detect_vcp, check_trigger
from zones import detect_zones, zone_from_vcp_contraction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent.parent / "data" / "scanner.log"),
    ],
)
log = logging.getLogger("scanner")

UNIVERSE_CACHE_PATH = Path(__file__).parent.parent / "data" / "fno_universe_cache.json"


def get_scan_symbols(cfg: dict, require_live: bool = False) -> list:
    """
    The scan universe (Step 15.2): Kite's live F&O universe minus
    config.yaml's `watchlist` (repurposed as an exclude list -- see the
    comment in config.yaml for why). Cached to disk after a successful
    live fetch so run_scan() can operate on the last-known universe
    without needing a live, non-expired Kite session just to enumerate
    symbols -- the scan itself only reads OHLCV already in the DB.
    `require_live=True` (used by refresh_all_data, which already needs
    Kite per-symbol anyway) always re-fetches, catching newly-added F&O names.
    """
    excludes = set(cfg.get("watchlist", []) or [])

    if not require_live and UNIVERSE_CACHE_PATH.exists():
        cached = json.loads(UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
        return [s for s in cached["universe"] if s not in excludes]

    from data.kite_ohlcv import get_fno_universe
    universe = get_fno_universe()
    UNIVERSE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE_PATH.write_text(
        json.dumps({"universe": universe, "fetched_at": datetime.now().isoformat()}),
        encoding="utf-8",
    )
    return [s for s in universe if s not in excludes]


def refresh_all_data(cfg: dict) -> None:
    from data.kite_ohlcv import backfill_ohlcv, backfill_india_vix
    from data.nse_scraper import backfill_participant_oi, backfill_delivery_data
    from data.trendlyne_scraper import fetch_cash_fii_dii_history

    symbols = get_scan_symbols(cfg, require_live=True)
    bootstrap_days = cfg["detection"].get("ohlcv_bootstrap_days", 400)

    log.info(f"=== Refreshing OHLCV for {len(symbols)} F&O universe symbols (Kite, per-symbol backfill) ===")
    t0 = time.time()
    for i, symbol in enumerate(symbols, 1):
        try:
            result = backfill_ohlcv(symbol, bootstrap_days=bootstrap_days)
            if not result.empty:
                db.upsert_ohlcv(result)
                log.info(f"[{i}/{len(symbols)}] {symbol}: +{len(result)} OHLCV rows")
            elif i % 20 == 0 or i == len(symbols):
                log.info(f"[{i}/{len(symbols)}] ... {time.time() - t0:.0f}s elapsed")
        except Exception as e:
            log.error(f"[{i}/{len(symbols)}] {symbol}: OHLCV backfill failed -- {e}")
    log.info(f"OHLCV refresh done: {len(symbols)} symbols in {time.time() - t0:.0f}s")

    log.info("=== Refreshing India VIX (Kite, backfill) ===")
    try:
        vix = backfill_india_vix(bootstrap_days=bootstrap_days)
        if not vix.empty:
            db.upsert_india_vix(vix)
            log.info(f"India VIX: +{len(vix)} rows")
    except Exception as e:
        log.error(f"India VIX backfill failed -- {e}")

    log.info("=== Refreshing NIFTY 50 + sector indices (Kite, backfill) -- for Nifty-alignment and sector-RS confluence signals ===")
    from sector_mapping import SECTOR_TO_NIFTY_INDEX
    index_symbols = {"NIFTY 50"} | set(SECTOR_TO_NIFTY_INDEX.values())
    for index_symbol in sorted(index_symbols):
        try:
            result = backfill_ohlcv(index_symbol, bootstrap_days=bootstrap_days, segment="INDICES")
            if not result.empty:
                db.upsert_ohlcv(result)
                log.info(f"{index_symbol}: +{len(result)} OHLCV rows")
        except Exception as e:
            log.error(f"{index_symbol}: index backfill failed -- {e}")

    log.info("=== Refreshing participant OI (NSE, backfill) ===")
    try:
        oi_summary = backfill_participant_oi(db, days=15)
        for d, status in sorted(oi_summary.items()):
            log.info(f"  participant_oi {d}: {status}")
    except Exception as e:
        log.error(f"participant OI backfill failed -- {e}")

    log.info("=== Refreshing delivery% (NSE, backfill) ===")
    try:
        delivery_summary = backfill_delivery_data(db, days=15)
        for d, status in sorted(delivery_summary.items()):
            log.info(f"  delivery_pct {d}: {status}")
    except Exception as e:
        log.error(f"delivery% backfill failed -- {e}")

    log.info("=== Refreshing cash FII/DII (Trendlyne -- ~1 trading month per fetch, real backfill) ===")
    try:
        fii_dii = fetch_cash_fii_dii_history()
        n = db.upsert_cash_fii_dii(fii_dii)
        log.info(f"cash_fii_dii: +{n} rows")
    except Exception as e:
        log.error(f"cash FII/DII fetch failed -- {e}")


def _scan_one_direction(symbol: str, df, direction: str, cfg: dict, scan_date: str) -> None:
    det = cfg["detection"]
    zones = detect_zones(
        df, lookback=det["zone_lookback"],
        min_move_pct=det["zone_min_move_pct"],
        max_base_bars=det["zone_max_base_bars"],
    )
    setup = detect_vcp(
        df, direction=direction,
        contraction_ratio_threshold=det["vcp_contraction_ratio_threshold"],
    )
    if setup is None:
        return

    setup = check_trigger(df, setup, volume_multiple=det["vcp_volume_multiple_trigger"])
    if setup.status == "failed":
        # Strategy prompt Section 4: "the setup is invalidated -- exit or
        # stand aside." A failed pattern is no longer a valid opportunity,
        # so it's deliberately not persisted as one -- surfacing it as an
        # actionable scan_result would contradict the strategy's own rule.
        log.info(f"{symbol} [{direction}]: VCP pattern failed/invalidated -- not persisted as an opportunity")
        return

    # Step 15.0: the zone backing THIS active setup is anchored to the VCP's
    # own final contraction (not independently detected) so the stop-loss
    # and the zone's distal line can never drift apart. Put it first so it's
    # preferred as the backing zone below over any broader/historical zone.
    vcp_zone = zone_from_vcp_contraction(df, direction, setup)
    all_zones = [vcp_zone] + zones

    stage_result = classify_stage(df)
    confluence = compute_confluence(stage_result, all_zones, setup, symbol=symbol, as_of_date=scan_date)
    if confluence is None:
        return

    zone_ids = db.upsert_zones(symbol, scan_date, all_zones)
    vcp_id = db.upsert_vcp_setup(symbol, scan_date, setup)

    backing_zone_id = None
    matching_kind = "demand" if direction == "bullish" else "supply"
    for z, zid in zip(all_zones, zone_ids):
        if z.kind == matching_kind:
            backing_zone_id = zid
            if z.fresh:
                break  # prefer a fresh matching zone if one exists

    all_notes = "; ".join([confluence.notes] + confluence.data_notes)
    db.upsert_scan_result(
        symbol=symbol, scan_date=scan_date, direction=direction,
        stage=stage_result.stage, confluence_score=confluence.weighted_score,
        conviction=confluence.conviction, notes=all_notes,
        zone_id=backing_zone_id, vcp_id=vcp_id,
    )
    log.info(f"{symbol} [{direction}]: {confluence.conviction} (score={confluence.weighted_score}) -- {stage_result.stage} -- trigger:{setup.status}")


def run_scan(cfg: dict) -> None:
    symbols = get_scan_symbols(cfg, require_live=False)
    min_bars = cfg["detection"].get("stage_min_bars", 210)

    log.info(f"=== Scanning {len(symbols)} F&O universe symbols ===")
    t0 = time.time()

    # Load once, reused both for RS Rating's cross-symbol batch computation
    # (Step 15.3) and the per-symbol pipeline below -- avoids reading each
    # symbol's OHLCV from the DB twice.
    ohlcv_by_symbol = {s: db.get_ohlcv(s) for s in symbols}

    from rs_rating import compute_rs_ratings
    rs_weights = cfg.get("rs_rating", {}).get("quarter_weights", [0.4, 0.2, 0.2, 0.2])
    rs_ratings = compute_rs_ratings(ohlcv_by_symbol, rs_weights)
    if rs_ratings:
        n_saved = db.update_rs_ratings(rs_ratings)
        log.info(f"RS Rating computed for {n_saved}/{len(symbols)} symbols (rest lack enough history for a full 4-quarter blend)")
    else:
        log.warning("RS Rating: no symbols had enough history to compute -- all Trend Template RS checks will report unavailable")

    skipped_no_history = 0
    for i, symbol in enumerate(symbols, 1):
        try:
            df = ohlcv_by_symbol[symbol]
            if len(df) < min_bars:
                skipped_no_history += 1
                if skipped_no_history <= 5 or i == len(symbols):
                    log.warning(f"[{i}/{len(symbols)}] {symbol}: only {len(df)} OHLCV bars (< {min_bars} needed) -- skipping")
                continue

            fund = apply_fundamental_filter(symbol)
            if not fund.eligible:
                log.info(f"[{i}/{len(symbols)}] {symbol}: excluded by fundamentals filter -- {fund.notes}")
                continue

            scan_date = df["date"].max()
            for direction in ("bullish", "bearish"):
                _scan_one_direction(symbol, df, direction, cfg, scan_date)

            if i % 20 == 0:
                log.info(f"[{i}/{len(symbols)}] ... {time.time() - t0:.0f}s elapsed")

        except Exception as e:
            log.error(f"[{i}/{len(symbols)}] {symbol}: scan failed -- {e}", exc_info=True)
            continue

    if skipped_no_history > 5:
        log.warning(f"{skipped_no_history} symbols total skipped for insufficient OHLCV history -- run refresh_data.py to backfill them")
    log.info(f"Scan complete: {len(symbols)} symbols in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    cfg = load_config()
    db.init_db()

    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "refresh":
        refresh_all_data(cfg)
    elif mode == "scan":
        run_scan(cfg)
    elif mode == "all":
        refresh_all_data(cfg)
        run_scan(cfg)
    else:
        print("Usage: python scanner.py [refresh|scan|all]")
        sys.exit(1)
