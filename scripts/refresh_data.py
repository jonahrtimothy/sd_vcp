"""
Daily data refresh: catches up OHLCV, India VIX, participant OI, delivery%,
and cash FII/DII for the whole watchlist -- safe to run after any gap
(laptop off for days), since OHLCV/VIX/OI/delivery% all backfill from
whatever was last saved rather than assuming a fixed lookback window.

Requires a fresh Kite access_token (run kite_auth.py's login/exchange flow
first if you haven't today).

Usage: python scripts/refresh_data.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import load_config
import db
from scanner import refresh_all_data

if __name__ == "__main__":
    db.init_db()
    refresh_all_data(load_config())
