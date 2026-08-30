"""
First real-data test of the core detection engine.

Pulls OHLCV for a symbol from the database (populated by kite_ohlcv.py) and
runs it through zones.py, vcp.py, stage.py, and confluence.py — the full
pipeline from raw price data to one final conviction verdict.

Usage: python test_real_detection.py [SYMBOL]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import db

sys.path.insert(0, str(Path(__file__).parent.parent))
from zones import detect_zones, zones_to_dataframe
from vcp import detect_vcp, check_trigger
from stage import classify_stage
from confluence import compute_confluence

if __name__ == "__main__":
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "RELIANCE"

    df = db.get_ohlcv(symbol)
    if df.empty:
        print(f"No OHLCV data found for {symbol} in the database. "
              f"Run: python src\\data\\kite_ohlcv.py {symbol} 100")
        sys.exit(1)

    print(f"Loaded {len(df)} real trading days for {symbol} "
          f"({df['date'].min()} to {df['date'].max()})\n")

    zones = detect_zones(df, lookback=3, min_move_pct=5, max_base_bars=20)
    zdf = zones_to_dataframe(zones)

    print("=== Detected Zones (real data) ===")
    if not zdf.empty:
        print(zdf[["kind", "start_idx", "end_idx", "zone_low", "zone_high",
                    "origin_move_pct", "fresh"]].to_string(index=False))
    else:
        print("No zones detected — the window may be too short, or "
              "no qualifying moves occurred. Try a longer lookback "
              "(more days_back in kite_ohlcv.py) or lower min_move_pct.")
    print()

    print("=== VCP scan across the data (real data) ===")
    setup = detect_vcp(df, direction="bullish", contraction_ratio_threshold=0.85)
    if setup:
        print(setup.summary())
        for i, c in enumerate(setup.contractions, 1):
            print(f"  C{i}: depth={c.depth_pct}%  avg_vol={c.avg_volume:.0f}")
        setup = check_trigger(df, setup)
        print(f"Trigger status: {setup.status}")
    else:
        print("No bullish VCP structure found in this window (need at "
              "least 2 contractions). This may be genuinely accurate — "
              "not every stock/window has a VCP forming — or the window "
              "may be too short. Try a longer days_back.")
    print()

    print("=== Stage Classification (real data) ===")
    stage_result = classify_stage(df)
    print(stage_result.summary())

    print("\n=== FINAL VERDICT (confluence of Stage + Zones + VCP + real OI/delivery%/FII-DII) ===")
    confluence = compute_confluence(
        stage_result, zones, setup,
        symbol=symbol, as_of_date=df["date"].max(),
    )
    if confluence:
        print(confluence.summary())
    else:
        print("No VCP setup was found, so no confluence verdict to compute.")