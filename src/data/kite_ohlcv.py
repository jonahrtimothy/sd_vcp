"""
Kite Connect historical OHLCV fetch.

Requires kite_auth.py's flow to have been run already today (a valid
cached access_token). Kite needs an instrument_token (not the plain
symbol) to fetch historical candles, so this module first downloads and
caches Kite's full instruments list, looks up the token for the symbol
you ask for, then fetches daily candles.

Usage:
    python kite_ohlcv.py <SYMBOL> <days_back>
    e.g. python kite_ohlcv.py RELIANCE 100
"""

import sys
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from kite_auth import get_authenticated_kite

sys.path.insert(0, str(Path(__file__).parent.parent))
import db

INSTRUMENTS_CACHE = Path(__file__).parent / "data" / "kite_instruments.csv"

VIX_TRADINGSYMBOL = "INDIA VIX"
VIX_SEGMENT = "INDICES"


def get_instrument_token(symbol: str, exchange: str = "NSE", segment: str | None = None) -> int:
    """
    Look up the instrument_token for a symbol. Downloads and caches Kite's
    full instruments list (large CSV, thousands of rows) if not already
    cached locally today.

    `segment` defaults to matching `exchange` (correct for cash equities,
    where Kite tags segment == "NSE") -- pass an explicit segment (e.g.
    "INDICES" for India VIX) when the instrument isn't a plain equity.
    """
    segment = segment or exchange
    kite = get_authenticated_kite()

    refresh_needed = True
    if INSTRUMENTS_CACHE.exists():
        cache_age_hours = (
            datetime.now().timestamp() - INSTRUMENTS_CACHE.stat().st_mtime
        ) / 3600
        refresh_needed = cache_age_hours > 24  # refresh once a day is plenty

    if refresh_needed:
        print("Refreshing instruments master list from Kite (once-daily)...")
        instruments = kite.instruments(exchange)
        df = pd.DataFrame(instruments)
        INSTRUMENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(INSTRUMENTS_CACHE, index=False)
    else:
        df = pd.read_csv(INSTRUMENTS_CACHE)

    match = df[(df["tradingsymbol"] == symbol) & (df["segment"] == segment)]
    if match.empty:
        match = df[df["tradingsymbol"] == symbol]

    if match.empty:
        raise ValueError(
            f"Symbol '{symbol}' not found in {exchange} instruments list. "
            f"Check spelling/exchange/segment, or the instruments cache may "
            f"be stale (delete {INSTRUMENTS_CACHE} to force a refresh)."
        )

    token = int(match.iloc[0]["instrument_token"])
    print(f"Resolved {symbol} -> instrument_token {token}")
    return token


def fetch_historical_ohlcv(
    symbol: str,
    days_back: int = 100,
    exchange: str = "NSE",
    from_date: date_cls | None = None,
    to_date: date_cls | None = None,
    raise_on_empty: bool = True,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV candles for `symbol`. Either pass `days_back` (fetch
    the last N days from today), or explicit `from_date`/`to_date` for a
    precise range (used by backfill_ohlcv to fetch exactly what's missing).
    Returns a DataFrame: columns = [symbol, date, open, high, low, close, volume].

    `raise_on_empty=False` (used by backfill_ohlcv) treats zero candles as
    a normal "nothing new yet" outcome (e.g. the range is only a weekend,
    or today's candle hasn't closed yet) rather than an error -- an empty
    range is expected here, not a scraper failure.
    """
    kite = get_authenticated_kite()
    token = get_instrument_token(symbol, exchange)

    to_date = to_date or datetime.now().date()
    from_date = from_date or (to_date - timedelta(days=days_back))

    print(f"Fetching {symbol} daily candles: {from_date} to {to_date}")
    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_date,
        to_date=to_date,
        interval="day",
    )

    if not candles:
        if not raise_on_empty:
            print(f"No new {symbol} candles in {from_date} to {to_date} (likely a weekend/holiday gap, or today hasn't closed yet)")
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume", "source"])
        raise ValueError(
            f"No historical data returned for {symbol} ({from_date} to "
            f"{to_date}). Check the date range covers actual trading days, "
            f"and that the symbol is correct and actively traded."
        )

    df = pd.DataFrame(candles)
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    df["source"] = "kite_connect"

    print(f"Fetched {len(df)} daily candles for {symbol}")
    return df[["symbol", "date", "open", "high", "low", "close", "volume", "source"]]


def backfill_ohlcv(symbol: str, bootstrap_days: int = 400) -> pd.DataFrame:
    """
    Fetch exactly the OHLCV history missing since the last saved date for
    `symbol` -- safe to call after any gap (laptop off for days/weeks): it
    always catches up completely in one call, rather than relying on a
    fixed days_back window. On a symbol with no history at all yet, falls
    back to `bootstrap_days` (default 400 calendar days, comfortably above
    stage.py's 210-TRADING-day minimum once weekends/holidays are factored
    in).

    Returns an empty DataFrame (no API call made) if already up to date.
    """
    existing = db.get_ohlcv(symbol)
    today = datetime.now().date()

    if existing.empty:
        from_date = today - timedelta(days=bootstrap_days)
    else:
        last_date = datetime.strptime(existing["date"].max(), "%Y-%m-%d").date()
        from_date = last_date + timedelta(days=1)

    if from_date > today:
        print(f"{symbol}: already up to date (last saved date covers through today)")
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume", "source"])

    return fetch_historical_ohlcv(symbol, from_date=from_date, to_date=today, raise_on_empty=False)


def fetch_india_vix(
    days_back: int = 100,
    from_date: date_cls | None = None,
    to_date: date_cls | None = None,
    raise_on_empty: bool = True,
) -> pd.DataFrame:
    """
    India VIX daily close via Kite's historical data API (instrument
    "INDIA VIX", segment INDICES) -- chosen over an NSE Playwright scrape
    specifically because Kite's date-range API backfills cleanly across
    any gap, unlike NSE's live-only endpoints. Returns columns [date, close].
    """
    kite = get_authenticated_kite()
    token = get_instrument_token(VIX_TRADINGSYMBOL, exchange="NSE", segment=VIX_SEGMENT)

    to_date = to_date or datetime.now().date()
    from_date = from_date or (to_date - timedelta(days=days_back))

    print(f"Fetching India VIX daily candles: {from_date} to {to_date}")
    candles = kite.historical_data(
        instrument_token=token, from_date=from_date, to_date=to_date, interval="day",
    )
    if not candles:
        if not raise_on_empty:
            print(f"No new India VIX candles in {from_date} to {to_date} (likely a weekend/holiday gap, or today hasn't closed yet)")
            return pd.DataFrame(columns=["date", "close"])
        raise ValueError(f"No India VIX data returned for {from_date} to {to_date}.")

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    print(f"Fetched {len(df)} India VIX daily candles")
    return df[["date", "close"]]


def backfill_india_vix(bootstrap_days: int = 400) -> pd.DataFrame:
    """Same catch-up logic as backfill_ohlcv, applied to India VIX."""
    existing = db.get_india_vix()
    today = datetime.now().date()

    if existing.empty:
        from_date = today - timedelta(days=bootstrap_days)
    else:
        last_date = datetime.strptime(existing["date"].max(), "%Y-%m-%d").date()
        from_date = last_date + timedelta(days=1)

    if from_date > today:
        print("India VIX: already up to date")
        return pd.DataFrame(columns=["date", "close"])

    return fetch_india_vix(from_date=from_date, to_date=today, raise_on_empty=False)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python kite_ohlcv.py <SYMBOL> [days_back]   (fixed-window fetch)")
        print("  python kite_ohlcv.py backfill <SYMBOL>      (catch up since last saved date)")
        print("  python kite_ohlcv.py vix [days_back]        (fixed-window India VIX fetch)")
        print("  python kite_ohlcv.py vix-backfill           (catch up India VIX since last saved date)")
        sys.exit(1)

    db.init_db()
    mode = sys.argv[1]

    try:
        if mode == "backfill":
            if len(sys.argv) < 3:
                print("Usage: python kite_ohlcv.py backfill <SYMBOL>")
                sys.exit(1)
            symbol = sys.argv[2].upper()
            result = backfill_ohlcv(symbol)
            n = db.upsert_ohlcv(result) if not result.empty else 0
            print(f"Saved {n} new rows to database (table: ohlcv)" if n else "No new rows needed.")

        elif mode == "vix":
            days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 100
            result = fetch_india_vix(days_back)
            n = db.upsert_india_vix(result)
            print(f"Saved {n} rows to database (table: india_vix)")
            print(result.tail(10).to_string(index=False))

        elif mode == "vix-backfill":
            result = backfill_india_vix()
            n = db.upsert_india_vix(result) if not result.empty else 0
            print(f"Saved {n} new rows to database (table: india_vix)" if n else "No new rows needed.")

        else:
            symbol = mode.upper()
            days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 100
            result = fetch_historical_ohlcv(symbol, days_back)
            n = db.upsert_ohlcv(result)
            print(f"Saved {n} rows to database (table: ohlcv)")
            print("\n=== Fetched data (first 10 rows) ===")
            print(result.head(10).to_string(index=False))
            print(f"\nTotal rows: {len(result)}")
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)