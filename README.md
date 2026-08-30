# S&D + VCP Studio

Personal quant research tool implementing the strategy from the system prompt:
Supply/Demand zones + VCP (Minervini-style) scoring + confluence data, for
Indian swing/futures trading, designed to generalize globally.

## Structure
- `src/zones.py` — rule-based supply/demand zone detection from OHLCV
- `src/vcp.py` — VCP contraction detection, scoring, and breakout trigger check
- `tests/test_detection.py` — validates both modules against a synthetic
  textbook VCP pattern (run this first: `python3 tests/test_detection.py`)
- `data/` — where downloaded NSE/broker data will live (not yet wired up)
- `notebooks/` — for ad-hoc exploration

## Status
- [x] Core zone detection algorithm — working, needs zone-merging refinement
      (currently returns overlapping candidate zones rather than one clean zone)
- [x] Core VCP detection + scoring + trigger-check algorithm — working,
      validated against synthetic data
- [ ] Real OHLCV data ingestion (broker API or NSE historical archive)
- [ ] NSE Playwright automation (participant-wise OI, delivery %, FII/DII) —
      must run locally, NSE requires a live browser session + isn't reachable
      from the current build environment
- [ ] Universe scanner (run detection across 50-100 stocks daily)
- [ ] Streamlit/Dash "studio" dashboard
- [ ] Backtest harness

## Next steps
1. Fix zone-merging so overlapping candidate zones consolidate into one
2. Wire in real OHLCV (broker API recommended over scraping NSE for price data)
3. Playwright script for NSE participant-wise OI / delivery % (runs on your machine)
4. Universe scanner loop
5. Dashboard
# sd_vcp
