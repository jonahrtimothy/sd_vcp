"""
Local SQLite storage for the S&D + VCP Studio.

Implements the schema from SYSTEM_BUILD_PROMPT.md Section 3.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "studio.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS participant_oi (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    participant TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date, participant, instrument, side)
);

CREATE TABLE IF NOT EXISTS cash_fii_dii (
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    buy_value REAL,
    sell_value REAL,
    net_value REAL,
    fetched_at TEXT,
    PRIMARY KEY (date, category)
);

CREATE TABLE IF NOT EXISTS delivery_pct (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    series TEXT,
    traded_qty INTEGER,
    deliverable_qty INTEGER,
    delivery_pct REAL,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS india_vix (
    date TEXT NOT NULL PRIMARY KEY,
    close REAL,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    kind TEXT,
    distal_price REAL, proximal_price REAL,
    zone_low REAL, zone_high REAL,
    origin_move_pct REAL,
    fresh INTEGER,
    tests INTEGER,
    broken INTEGER
);

CREATE TABLE IF NOT EXISTS vcp_setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    direction TEXT,
    contraction_count INTEGER,
    contraction_ratio_ok INTEGER,
    volume_decay_ok INTEGER,
    trigger_level REAL,
    quality_score REAL,
    status TEXT
);

CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    direction TEXT,
    stage TEXT,
    zone_id INTEGER,
    vcp_id INTEGER,
    confluence_score REAL,
    conviction TEXT,
    notes TEXT
);

-- Fundamental quality filter (Section 5), scraped from screener.in.
-- One row per symbol, overwritten on each re-scrape (not scan-dated like
-- zones/vcp_setups -- fundamentals are cached/refreshed on their own
-- weekly cadence, independent of the daily technical scan).
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT NOT NULL PRIMARY KEY,
    sector TEXT,
    industry TEXT,
    quarters_json TEXT,
    eps_yoy_growth_pct REAL,
    eps_yoy_growth_pct_prior REAL,
    profit_yoy_growth_pct REAL,
    earnings_trend TEXT,
    fetched_at TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _migrate_scan_results_add_direction(conn: sqlite3.Connection) -> None:
    """scan_results gained a `direction` column after the table already
    existed in some databases. scan_results is a re-computed-per-scan table
    (not an append-only historical record), so it's safe to drop and let
    the next scan repopulate it rather than write a real ALTER TABLE
    migration for a table that's meant to be rebuilt anyway."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(scan_results)")]
    if cols and "direction" not in cols:
        conn.execute("DROP TABLE scan_results")
        conn.commit()


def _migrate_zones_add_precision_columns(conn: sqlite3.Connection) -> None:
    """zones gained distal_price/proximal_price/broken columns (Step 15.0,
    precision zone boundaries). Unlike scan_results, this table already
    holds real persisted zone data worth keeping, so this is a real
    ALTER TABLE ADD COLUMN migration, not a drop/recreate."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(zones)")]
    if not cols:
        return  # table doesn't exist yet -- SCHEMA below creates it fresh with all columns
    if "distal_price" not in cols:
        conn.execute("ALTER TABLE zones ADD COLUMN distal_price REAL")
    if "proximal_price" not in cols:
        conn.execute("ALTER TABLE zones ADD COLUMN proximal_price REAL")
    if "broken" not in cols:
        conn.execute("ALTER TABLE zones ADD COLUMN broken INTEGER")
    conn.commit()


def init_db() -> None:
    """Create all tables if they don't already exist. Safe to run repeatedly."""
    conn = get_connection()
    try:
        _migrate_scan_results_add_direction(conn)
        _migrate_zones_add_precision_columns(conn)
        conn.executescript(SCHEMA)
        conn.commit()
        print(f"Database ready at: {DB_PATH}")
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat()


def upsert_participant_oi(df: pd.DataFrame, symbol: str = "MARKET") -> int:
    """
    df must have columns: date, participant, instrument, side, contracts.
    `symbol` is 'MARKET' since participant OI is a market-wide report, not
    per-stock (kept as a column for schema consistency / future per-symbol use).
    """
    conn = get_connection()
    try:
        rows = [
            (symbol, r["date"], r["participant"], r["instrument"], r["side"], int(r["contracts"]), _now())
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO participant_oi
               (symbol, date, participant, instrument, side, contracts, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def upsert_cash_fii_dii(df: pd.DataFrame) -> int:
    """df must have columns: category, date, buyValue, sellValue, netValue."""
    conn = get_connection()
    try:
        rows = [
            (r["date"], r["category"], float(r["buyValue"]), float(r["sellValue"]), float(r["netValue"]), _now())
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO cash_fii_dii
               (date, category, buy_value, sell_value, net_value, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def upsert_delivery_pct(df: pd.DataFrame) -> int:
    """df must have columns: date, symbol, series, traded_qty, deliverable_qty, delivery_pct."""
    conn = get_connection()
    try:
        rows = [
            (
                r["symbol"], r["date"], r["series"],
                None if pd.isna(r["traded_qty"]) else int(r["traded_qty"]),
                None if pd.isna(r["deliverable_qty"]) else int(r["deliverable_qty"]),
                None if pd.isna(r["delivery_pct"]) else float(r["delivery_pct"]),
                _now(),
            )
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO delivery_pct
               (symbol, date, series, traded_qty, deliverable_qty, delivery_pct, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def upsert_ohlcv(df: pd.DataFrame) -> int:
    """df must have columns: symbol, date, open, high, low, close, volume, source."""
    conn = get_connection()
    try:
        rows = [
            (
                r["symbol"], r["date"], float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), int(r["volume"]),
                r.get("source", "unknown"), _now(),
            )
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO ohlcv
               (symbol, date, open, high, low, close, volume, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_ohlcv(symbol: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT * FROM ohlcv WHERE symbol = ?"
        params = [symbol]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_delivery_pct(symbol: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT * FROM delivery_pct WHERE symbol = ?"
        params = [symbol]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_participant_oi(
    symbol: str = "MARKET",
    participant: str = None,
    instrument: str = None,
    start_date: str = None,
    end_date: str = None,
) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT * FROM participant_oi WHERE symbol = ?"
        params = [symbol]
        if participant:
            query += " AND participant = ?"
            params.append(participant)
        if instrument:
            query += " AND instrument = ?"
            params.append(instrument)
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_cash_fii_dii(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT * FROM cash_fii_dii WHERE 1=1"
        params = []
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def upsert_india_vix(df: pd.DataFrame) -> int:
    """df must have columns: date, close."""
    conn = get_connection()
    try:
        rows = [(r["date"], float(r["close"]), _now()) for _, r in df.iterrows()]
        conn.executemany(
            "INSERT OR REPLACE INTO india_vix (date, close, fetched_at) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_india_vix(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT * FROM india_vix WHERE 1=1"
        params = []
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def upsert_fundamentals(record: dict) -> None:
    """record keys: symbol, sector, industry, quarters_json,
    eps_yoy_growth_pct, eps_yoy_growth_pct_prior, profit_yoy_growth_pct,
    earnings_trend. One row per symbol -- overwritten on each re-scrape."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals
               (symbol, sector, industry, quarters_json, eps_yoy_growth_pct,
                eps_yoy_growth_pct_prior, profit_yoy_growth_pct, earnings_trend, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["symbol"], record.get("sector"), record.get("industry"),
                record.get("quarters_json"), record.get("eps_yoy_growth_pct"),
                record.get("eps_yoy_growth_pct_prior"), record.get("profit_yoy_growth_pct"),
                record.get("earnings_trend"), _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_fundamentals(symbol: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM fundamentals WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        cols = [c[0] for c in conn.execute("SELECT * FROM fundamentals WHERE symbol = ?", (symbol,)).description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def upsert_zones(symbol: str, scan_date: str, zones: list) -> list:
    """Replaces all zones previously stored for this (symbol, scan_date) --
    zones/vcp_setups/scan_results are re-derived per scan run, not
    append-only. Returns the DB row ids in the same order as `zones`."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM zones WHERE symbol = ? AND scan_date = ?", (symbol, scan_date))
        ids = []
        for z in zones:
            cur = conn.execute(
                """INSERT INTO zones
                   (symbol, scan_date, kind, distal_price, proximal_price,
                    zone_low, zone_high, origin_move_pct, fresh, tests, broken)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, scan_date, z.kind, z.distal_price, z.proximal_price,
                 z.zone_low, z.zone_high, z.origin_move_pct, int(z.fresh), z.tests, int(z.broken)),
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def upsert_vcp_setup(symbol: str, scan_date: str, setup) -> int:
    """Replaces any previously stored VCP setup for this
    (symbol, scan_date, direction). Returns the new row's DB id."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM vcp_setups WHERE symbol = ? AND scan_date = ? AND direction = ?",
            (symbol, scan_date, setup.direction),
        )
        cur = conn.execute(
            """INSERT INTO vcp_setups
               (symbol, scan_date, direction, contraction_count, contraction_ratio_ok,
                volume_decay_ok, trigger_level, quality_score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, scan_date, setup.direction, len(setup.contractions),
                int(setup.contraction_ratio_ok), int(setup.volume_decay_ok),
                setup.trigger_level, setup.quality_score, setup.status,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def upsert_scan_result(
    symbol: str, scan_date: str, direction: str, stage: str,
    confluence_score: float, conviction: str, notes: str,
    zone_id: int = None, vcp_id: int = None,
) -> int:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM scan_results WHERE symbol = ? AND scan_date = ? AND direction = ?",
            (symbol, scan_date, direction),
        )
        cur = conn.execute(
            """INSERT INTO scan_results
               (symbol, scan_date, direction, stage, zone_id, vcp_id,
                confluence_score, conviction, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, scan_date, direction, stage, zone_id, vcp_id,
             confluence_score, conviction, notes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_scan_results(scan_date: str = None, symbol: str = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT sr.*, vs.status AS trigger_status
            FROM scan_results sr
            LEFT JOIN vcp_setups vs ON sr.vcp_id = vs.id
            WHERE 1=1
        """
        params = []
        if scan_date:
            query += " AND sr.scan_date = ?"
            params.append(scan_date)
        if symbol:
            query += " AND sr.symbol = ?"
            params.append(symbol)
        query += " ORDER BY sr.confluence_score DESC"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_zones_for_scan(symbol: str, scan_date: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(
            "SELECT * FROM zones WHERE symbol = ? AND scan_date = ?",
            conn, params=[symbol, scan_date],
        )
    finally:
        conn.close()


def get_vcp_setups_for_scan(symbol: str, scan_date: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(
            "SELECT * FROM vcp_setups WHERE symbol = ? AND scan_date = ?",
            conn, params=[symbol, scan_date],
        )
    finally:
        conn.close()


def get_latest_scan_date() -> str | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT MAX(scan_date) FROM scan_results").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_scan_dates() -> list:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT scan_date FROM scan_results ORDER BY scan_date DESC").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_data_freshness() -> dict:
    """Latest saved date per raw data source -- lets the dashboard show at
    a glance whether a source has fallen behind (e.g. Task Scheduler
    didn't run for a few days), rather than that only being discoverable
    by noticing missing/odd scan results."""
    conn = get_connection()
    try:
        return {
            "ohlcv (any symbol)": conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0],
            "india_vix": conn.execute("SELECT MAX(date) FROM india_vix").fetchone()[0],
            "participant_oi": conn.execute("SELECT MAX(date) FROM participant_oi").fetchone()[0],
            "delivery_pct": conn.execute("SELECT MAX(date) FROM delivery_pct").fetchone()[0],
            "cash_fii_dii": conn.execute(
                "SELECT MAX(date) FROM cash_fii_dii"
            ).fetchone()[0],  # note: dates here are 'DD-Mon-YYYY' (NSE's own format), not ISO
        }
    finally:
        conn.close()


def table_counts() -> dict:
    """Quick summary of row counts per table — useful for sanity checks."""
    conn = get_connection()
    try:
        tables = ["ohlcv", "participant_oi", "cash_fii_dii", "delivery_pct",
                  "india_vix", "zones", "vcp_setups", "scan_results", "fundamentals"]
        counts = {}
        for t in tables:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return counts
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("\nTable row counts:")
    for table, count in table_counts().items():
        print(f"  {table}: {count}")