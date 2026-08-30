"""
Sanity test: build a synthetic OHLCV series shaped like a textbook bullish VCP
(the same shape as the Minervini-style chart discussed), run zone + VCP
detection, and print what the engine finds. This validates the detection
logic works before pointing it at real NSE data.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np
import pandas as pd
from zones import detect_zones, zones_to_dataframe
from vcp import detect_vcp, check_trigger


def build_synthetic_vcp():
    """
    Mirrors the pattern discussed: run-up 100->400, then 3 contractions of
    shrinking depth (-25%, -10%, -4%) with volume drying up, then a
    breakout on volume expansion.
    """
    np.random.seed(42)
    bars = []

    def add_leg(start_price, end_price, n_bars, vol_base, vol_trend=0, noise=0.5):
        prices = np.linspace(start_price, end_price, n_bars)
        for p in prices:
            o = p + np.random.uniform(-noise, noise)
            c = p + np.random.uniform(-noise, noise)
            h = max(o, c) + abs(np.random.uniform(0, noise))
            l = min(o, c) - abs(np.random.uniform(0, noise))
            v = max(10, vol_base + vol_trend * len(bars) + np.random.uniform(-10, 10))
            bars.append([o, h, l, c, v])

    # run-up 100 -> 400
    add_leg(100, 400, 40, vol_base=150)
    # contraction 1: 400 -> 300 (-25%), volume still elevated
    add_leg(400, 300, 12, vol_base=120)
    # bounce back near high
    add_leg(300, 390, 10, vol_base=100)
    # contraction 2: 390 -> 351 (-10%), volume lower
    add_leg(390, 351, 8, vol_base=70)
    add_leg(351, 385, 8, vol_base=60)
    # contraction 3: 385 -> 370 (-4%), volume dried up
    add_leg(385, 370, 6, vol_base=35)
    add_leg(370, 388, 5, vol_base=30)
    # breakout on volume expansion
    add_leg(388, 460, 8, vol_base=250)

    df = pd.DataFrame(bars, columns=["open", "high", "low", "close", "volume"])
    df["date"] = pd.date_range("2026-01-01", periods=len(df), freq="B")
    return df


if __name__ == "__main__":
    df = build_synthetic_vcp()
    print(f"Synthetic series: {len(df)} bars, price {df['close'].min():.0f}-{df['close'].max():.0f}\n")

    # --- Zone detection ---
    zones = detect_zones(df, lookback=3, min_move_pct=8, max_base_bars=20)
    zdf = zones_to_dataframe(zones)
    print("=== Detected Zones ===")
    if not zdf.empty:
        print(zdf[["kind", "start_idx", "end_idx", "zone_low", "zone_high", "origin_move_pct", "fresh"]].to_string(index=False))
    else:
        print("No zones detected — check thresholds.")
    print()

    # --- VCP detection on the base region (bars ~40 to ~89, the contraction zone) ---
    base_region = df.iloc[38:90].reset_index(drop=True)
    setup = detect_vcp(base_region, direction="bullish", contraction_ratio_threshold=0.8)
    print("=== VCP Detection (base region) ===")
    if setup:
        print(setup.summary())
        print("Contractions:")
        for i, c in enumerate(setup.contractions, 1):
            print(f"  C{i}: depth={c.depth_pct}%  avg_vol={c.avg_volume:.1f}")

        # check trigger against the FULL df (breakout happens after this base region in original index space)
        full_setup_view = df.iloc[38:].reset_index(drop=True)
        # re-run detection against the same slice offsets, then check trigger on the extended slice
        setup2 = detect_vcp(df.iloc[38:90].reset_index(drop=True), direction="bullish", contraction_ratio_threshold=0.8)
        extended = df.iloc[38:].reset_index(drop=True)
        setup2 = check_trigger(extended, setup2, volume_multiple=1.5)
        print(f"\nTrigger check status: {setup2.status}")
    else:
        print("No VCP setup detected — check thresholds.")
