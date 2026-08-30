"""
Runs the full detection + confluence pipeline (fundamentals -> zones ->
VCP -> stage -> confluence) across the whole watchlist, using whatever
data is currently in the DB. Run refresh_data.py first if you want today's
data included.

Usage: python scripts/run_scan.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import load_config
import db
from scanner import run_scan

if __name__ == "__main__":
    db.init_db()
    run_scan(load_config())
