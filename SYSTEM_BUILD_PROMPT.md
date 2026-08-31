# S&D + VCP Studio — System Build Prompt

This is the implementation spec for building the real system, kept separate
from `trading_strategy_system_prompt.md` (the analytical spec: what counts as
a valid zone, VCP, confluence signal, risk rule). This document is updated at
every build milestone — treat it as the living engineering source of truth.

**Local project path**: `C:\Jonah\sd_vcp` — this document and all code are
designed/authored in chat, packaged as a zip each milestone, and extracted
into that folder on your machine.

**Build environment note**: design and code authoring happens in chat. Actual
execution against live data (NSE via Playwright, Kite Connect API) must run
on your machine using plain Python — this is not exclusive to Claude Code;
any local Python setup works. Claude Code is an optional convenience for
faster iterative debugging later, not a requirement.

**Hard requirement: no synthetic/mock data anywhere in the delivered system.**
Every module must be validated against real market data pulled from a real
source before being considered done. The existing `tests/test_detection.py`
using synthetic data was a logic-validation step only — replace/supplement it
with tests against real historical data as soon as a data source is wired in.

---

## 1. Current state (what already exists) — updated through Step 10, all validated on REAL data (except where noted)

Project folder `sd_vcp_studio/` (local path `C:\Jonah\sd_vcp`) contains:

**Core detection engine** (`src/`):
- `zones.py` — supply/demand zone detection from OHLCV. Includes a proper
  union-find merge (price overlap AND time proximity required — a naive
  price-only merge was tried first and found to chain unrelated zones
  together transitively on longer histories; fixed and validated on 475
  days of real RELIANCE data: went from 2 meaningless mega-zones spanning
  400+ bars each down to 5 differentiated real zones spanning 54-262 bars).
  **Step 15.0 (Aug 31/Sep 1 2026, Minervini/SEPA alignment pass)**: zone
  boundaries rebuilt as precise distal/proximal lines (ICT "order block"
  equivalent) from the origin candle(s) at the base edge, replacing the old
  whole-base high/low range. `Zone` gained `distal_price`/`proximal_price`
  (true invalidation boundary vs. first-reaction boundary) and `broken`
  (a CLOSE through distal, distinct from merely `tests`/touching proximal);
  `zone_low`/`zone_high` are now `min`/`max` of the two new fields so
  existing chart-shading code needed no changes. New
  `zone_from_vcp_contraction(df, direction, setup)` builds the zone tied to
  an ACTIVE VCP setup directly from `vcp.py`'s own final, tightest
  contraction, not an independently-detected origin -- this unifies
  stop-loss and zone-invalidation into one number instead of two separately
  computed ones that could drift apart; `scanner.py` and the dashboard both
  now use this for the setup's backing zone and its stop-loss. Validated on
  real HDFCBANK data: zones went from wide multi-week boxes (e.g. the old
  "supply (tested x98)" spanning a whole base) to tight 1-3 point bands;
  the active bearish setup's stop-loss tightened from Rs843.00 (old
  whole-base high) to Rs734.40 (new distal line off the final contraction)
  -- risk per share dropped from Rs127.90 to Rs19.30, a ~7x tighter,
  more realistic risk definition. Could not pinpoint the exact hand-drawn
  856.80/817.00 example from the handoff doc (that discussion happened
  outside this session, no date/context available to locate it) -- flagged
  honestly rather than claimed as matched; the mechanism itself is
  confirmed working correctly against real data by the broader validation
  above.
- `vcp.py` — VCP contraction detection + scoring (0-100) + breakout trigger
  check. Uses TOLERANT/fractional scoring for the contraction-ratio and
  volume-decay rules (a majority of steps must pass, not every single
  one) — the original strict all-pass version was found too brittle on
  real data (one noisy mid-pattern step zeroed an otherwise legitimate
  setup; real RELIANCE data went score=40→80 after this fix).
- `stage.py` — Stage 1-4 trend classification via 50/150/200-day SMA
  structure and SMA200 slope. Requires >=210 bars of history. Validated
  on synthetic uptrend/downtrend/flat cases and real RELIANCE data
  (correctly classified Stage 4 given real falling MA structure).
  **Step 15.3 (new)**: `classify_trend_template()` completes Minervini's
  full 8-point Trend Template (items 6-8: >=25% above the 52-week low,
  within 25% of the 52-week high, RS Rating >=70), surfaced ALONGSIDE
  `classify_stage()` rather than folded into it -- a stock can be Stage 2
  by MA structure alone yet still fail the full template (Minervini's own
  "broken leader" caution). All 8 checks are individually exposed on
  `TrendTemplateResult` so a failure is legible, not collapsed into one
  boolean. Requires >=252 bars (a full trading year); reports
  `insufficient_data` honestly otherwise rather than guessing.
- `rs_rating.py` (NEW, Step 15.3) — approximates IBD's RS Rating: a
  weighted blend of trailing quarterly returns (`config.yaml`'s
  `rs_rating.quarter_weights`, default 0.4/0.2/0.2/0.2, heaviest on the
  most recent ~63-day quarter), percentile-ranked 1-99 against the whole
  scanned universe for that scan run. Explicitly documented as NOT the
  real published IBD metric. `scanner.py`'s `run_scan()` computes it once
  per scan across all symbols with enough history and persists via
  `db.update_rs_ratings()` before the per-symbol confluence loop.
  Validated on the live 210-stock F&O universe: 210/210 computed, rating
  distribution min=1/max=99/median=50 (a sane percentile spread).
- `confluence.py` — combines Stage + Zones + VCP into one Low/Medium/High
  conviction verdict. Stage is a HARD GATE/multiplier (not equal-weighted):
  a conflicting Stage caps conviction at Low regardless of VCP score —
  this is a confirmed capital-preservation design decision (see strategy
  prompt Section 7). Validated on real data: an 80-score bullish VCP got
  correctly hard-capped to Low conviction due to a Stage 4 conflict.
  **Step 9 (new)**: now also pulls the real participant_oi/delivery_pct/
  cash_fii_dii data sitting in the DB and factors it into the weighted
  score as three additional bonus/penalty inputs (`compute_confluence`
  gained optional `symbol`/`as_of_date` params, backward compatible when
  omitted): (1) cash FII/FPI net flow direction vs. setup direction
  (DII cash flow tracked in the data but not scored — DII derivatives
  activity is structurally minimal per the futures-only nuance, so only
  FII cash flow is used as the primary institutional read), (2) FII+Pro
  stock-futures OI buildup trend (participant OI is a market-wide breadth
  report, not per-symbol — this is a breadth signal, not stock-specific),
  (3) per-symbol delivery% trend (rising = supports genuine participation,
  falling = more likely intraday churn, applied symmetrically to both
  bullish and bearish setups — an engineering interpretation of the
  strategy prompt's bullish-framed example, flagged for Jonah to confirm).
  Each signal requires >=2 real days of history to compute a trend and
  explicitly reports "insufficient data" rather than guessing when fewer
  are available — validated on real RELIANCE data (Aug 31 2026): with only
  a single day of participant_oi/delivery_pct in the DB so far, OI and
  delivery correctly report "insufficient history" (0 contribution) while
  the 2-row cash_fii_dii data correctly registered a real FII net-sell
  signal conflicting with a bullish setup. Thresholds/bonus magnitudes are
  module-level constants in confluence.py (not yet in a config file — see
  Section 5, no config.yaml exists in this project yet) and will need
  calibration once more daily scrapes accumulate real trend history.
- `db.py` — SQLite schema (8 tables) + upsert/read functions for ohlcv,
  participant_oi, cash_fii_dii, delivery_pct (india_vix, zones, vcp_setups,
  scan_results tables exist in schema, not yet populated by code). All
  upserts are idempotent (INSERT OR REPLACE, confirmed via re-run test).
- `test_real_detection.py` — the primary end-to-end validation script;
  pulls real OHLCV from the DB and runs the full zones→vcp→stage→confluence
  pipeline, printing the final verdict. Run as: `python src\test_real_detection.py <SYMBOL>`
- `tests/test_detection.py` — original synthetic-data sanity check (kept
  for fast pure-logic testing, not a substitute for real-data validation)

**Data layer** (`src/data/`):
- `trendlyne_scraper.py` (NEW, Step 11) — `fetch_cash_fii_dii_history()`:
  the real fix for the cash FII/DII gap (see Section 2b for the full
  investigation). Plain `urllib` GET against
  `trendlyne.com/macro-data/fii-dii/latest/`, parses the JSON embedded in
  `<table id="cash-table-main-pastmonth" data-jsondata="...">`. Returns
  ~23 real trading days per fetch in the exact column shape
  `db.upsert_cash_fii_dii()` already expects. No Playwright needed --
  validated as NOT blocked by the bot-detection that stopped both NSE and
  BSE. This is now the PRIMARY source `scanner.py` uses; `nse_scraper.py`'s
  `fetch_cash_fii_dii()` (below) remains as an occasional NSE cross-check,
  per the strategy prompt's own "cross-check against NSE as source of
  truth" guidance -- it's just no longer the default daily path since it
  can't backfill.
- `nse_scraper.py` — three working, validated scrapers, all persisting to
  the DB automatically: `fetch_participant_oi()` (direct archive CSV),
  `fetch_cash_fii_dii()` (live JSON API, only returns ~1-2 recent days —
  not a historical backfill source, superseded as the default path by
  `trendlyne_scraper.py` above), `fetch_delivery_data()` (MTO file,
  filtered to `series == "EQ"` — the raw file also contains bonds/G-secs/
  SME listings that must be excluded). Run as:
  `python src\data\nse_scraper.py <oi|fiidii|delivery> [YYYY-MM-DD]`
  **Step 10 (new)**: added `backfill_participant_oi()`/`backfill_delivery_data()`
  -- loop every missing weekday since the last saved date (skip weekends
  outright), calling the existing single-shot functions per date. A 404 on
  a PAST date is treated as "market holiday" (not an error, since a real
  report for a bygone date would already be published if one existed);
  today's 404 keeps the original ambiguous message (not yet published vs.
  pattern broke) since that distinction genuinely can't be resolved for
  the current day. If every weekday in the backfill window 404s, that's
  surfaced loudly as a likely URL-pattern break rather than silently
  assumed to be a holiday streak. Run as:
  `python src\data\nse_scraper.py backfill-oi [days]` / `backfill-delivery [days]`.
  **NOT validated against live NSE data from within Claude Code's own
  session** -- nseindia.com blocked/timed-out every connection attempt
  from that session's network path (both a raw Playwright `page.goto` and
  with `--disable-http2`), while the exact same session reached both
  Kite's API and screener.in successfully. This matches the limitation
  already flagged in `trading_strategy_system_prompt.md` Section 11 ("NSE
  requires a live browser session; not reachable from a sandboxed build
  environment") -- NSE is known to hard-block cloud/datacenter IP ranges.
  **Needs a real validation run on Jonah's own machine** before being
  considered fully proven, even though the code reuses the already-validated
  single-shot fetch functions and the date-looping logic is straightforward.
- `kite_auth.py` — Kite Connect login flow (login URL → manual 2FA login →
  request_token capture from redirect URL → access_token exchange →
  local caching in `.kite_token_cache`, gitignored). Validated end-to-end
  against the real Zerodha API (confirmed real profile() response). The
  access_token expires ~daily (end of trading day) and MUST be
  regenerated each trading morning via `login` then `exchange` — this is
  Kite's design (2FA-gated), not automatable without a real TOTP-automation
  tradeoff (see Section 8 below on scheduling).
- `kite_ohlcv.py` — resolves symbol→instrument_token via Kite's cached
  instruments master list, fetches daily historical candles, persists to
  the ohlcv table. Validated: RELIANCE, 700-day request → 475 real trading
  days fetched and saved.
  **Step 10 (new)**: `backfill_ohlcv(symbol)` -- fetches only what's missing
  since the last saved date (or a bootstrap window on first run), so a gap
  of any length (laptop off for days/weeks) catches up in one Kite API
  call rather than depending on a fixed lookback. `fetch_india_vix()`/
  `backfill_india_vix()` -- India VIX (instrument "INDIA VIX", segment
  INDICES, token 264969) fetched via the SAME Kite historical-data path,
  chosen deliberately over an NSE Playwright scrape specifically because
  Kite's date-range API backfills cleanly, closing the VIX gap noted in
  Section 2c/Phase 3. Both validated against the real live Kite session
  (Aug 31 2026): VIX bootstrap fetched 203 real days; a real bug was
  caught and fixed here -- 300 calendar days only yielded 203 TRADING
  days (short of stage.py's 210-bar minimum) due to weekends/holidays, so
  `ohlcv_bootstrap_days` is now 400 in config.yaml. Also fixed:
  `fetch_historical_ohlcv`/`fetch_india_vix` originally raised on an empty
  result unconditionally; backfill now passes `raise_on_empty=False` since
  an empty range (weekend, or today not yet closed) is a normal "nothing
  new yet" outcome for backfill, not a scraper failure -- caught via real
  testing (RELIANCE's Aug 29-31 gap crashed before this fix).
  **Step 15.2 (new)**: `get_fno_universe()` -- the full NSE F&O-eligible
  stock universe (~210 names), sourced live from the same NFO instruments
  master already cached daily for lot-size lookups, replacing the
  hand-picked 40-name watchlist. Index futures (NIFTY, BANKNIFTY,
  FINNIFTY, MIDCPNIFTY, etc.) are filtered out using a real, robust
  discriminator confirmed against live data: a genuine stock underlying
  also has a matching NSE cash-equity listing; an index never does.
- `screener_scraper.py` (NEW) — fundamentals scraper for Section 5's
  earnings-growth quality filter. screener.in has no official API; this
  parses the public company page's HTML directly (confirmed against real
  live pages, Aug 2026): the "Quarterly Results" table's Sales/Net
  Profit/EPS rows (matched to `data-date-key` quarter-end dates) and the
  "Peer comparison" section's sector/industry link text. Falls back from
  `/company/<SYM>/consolidated/` to `/company/<SYM>/` for companies
  without consolidated financials. Computes YoY EPS growth (vs. the same
  quarter 4 columns back) and classifies accelerating/decelerating/
  declining by comparing the latest YoY rate to the prior quarter's.
  Validated against 4 real symbols: RELIANCE (declining, -22.4% EPS YoY),
  TCS (decelerating, +4.6%), PAGEIND (insufficient_data -- some quarters
  had unparseable cells, correctly NOT fabricated into a verdict), and a
  fake ticker (correctly raised ScraperError on 404). Throttled to one
  request per 2s minimum -- light touch, matches the weekly-cache design
  below, not a crawler. Also provides the sector/industry classification
  `confluence.py`'s sector-relative-strength signal uses (Step 13, see
  below) -- parsed independently of the quarterly-growth table so a
  row-naming quirk in one doesn't lose the other (real bug found and
  fixed on HDFCBANK, a bank -- see Step 13 detail).
- `fundamentals.py` (NEW) — Section 5's quality filter, applied BEFORE the
  technical scan per the strategy prompt's intended order. Caches
  screener_scraper.py's output in the new `fundamentals` DB table and only
  re-scrapes once the cache exceeds `cache_max_age_days` (config.yaml,
  default 7) -- earnings data doesn't change day to day. A `declining`
  earnings trend excludes the symbol from the technical scan;
  `insufficient_data` passes through flagged as unverified rather than
  being excluded (can't fabricate a verdict from missing data) or silently
  assumed clean. Validated end-to-end via scanner.py: RELIANCE's
  currently-strong VCP pattern (score=80) now gets correctly excluded
  before the technical stage ever runs, due to its declining EPS trend.
- `scanner.py` (NEW, Phase 5) — orchestrates the full pipeline across the
  watchlist. `refresh_all_data(cfg)`: backfills OHLCV/VIX per symbol
  (Kite), backfills participant_oi/delivery_pct (NSE, per-date archives),
  fetches cash_fii_dii live-only (can't backfill gaps -- see Section 2b
  update below). `run_scan(cfg)`: fundamentals filter first, then for each
  of bullish/bearish: zones + VCP + stage + confluence, persisted to
  zones/vcp_setups/scan_results. Continues past a single symbol's failure
  (logs and skips) rather than crashing the run. Logs to both console and
  `data/scanner.log`. Validated end-to-end on real data (RELIANCE, TCS,
  HDFCBANK): HDFCBANK correctly produced two distinct real verdicts --
  bearish/Stage 4 aligned -> High conviction (score=88), bullish/Stage 4
  conflicting -> Low (score=19) -- extending the Stage-gate validation
  beyond RELIANCE to a second real stock. `scripts/refresh_data.py` and
  `scripts/run_scan.py` are thin CLI wrappers matching Section 6's
  intended daily flow.
- `src/dashboard/app.py` (NEW, Phase 6) — Streamlit dashboard, all 4 pages
  from Section 7 built: Daily Scan (KPI tiles + color-coded conviction/
  direction/stage badges, sorted by conviction then score), Symbol Detail
  (Plotly candlestick with shaded supply/demand zones -- solid border if
  fresh, dotted if tested -- VCP base region + trigger line + contraction
  markers, plus the full Section 9 output structure rendered as text with
  real computed entry/stop/target/R:R, and a confluence-data + fundamentals
  panel), Watchlist Management (add/remove symbols, rewrites just the
  `watchlist:` block in config.yaml via `config.update_watchlist()` --
  preserves every comment elsewhere in the file, confirmed via a real
  add/restore round-trip diff), Scan History (browse any past scan_date).
  Dark theme with a green/amber/red palette (`.streamlit/config.toml`,
  `src/dashboard/style.py`). A "Refresh data + re-run scan now" button on
  the Daily Scan page calls scanner.py's functions directly. **Validated
  by actually running it** (`streamlit run src/dashboard/app.py`) and
  driving it in a real browser: all 4 pages render correctly against the
  real DB, including the candlestick+zones+VCP chart and the full Section
  9 text output with real numbers.
- `config.yaml` / `src/config.py` (NEW) — centralizes the watchlist and
  every threshold that was previously hardcoded (detection thresholds,
  confluence bonus magnitudes, fundamentals cache age), per Section 15's
  own standard. `confluence.py` now reads its thresholds from here
  (falls back to the old hardcoded defaults if a key is missing).
- `db.py` — gained: `get_participant_oi()`, `upsert_india_vix()`/
  `get_india_vix()`, `upsert_fundamentals()`/`get_fundamentals()`,
  `upsert_zones()`/`upsert_vcp_setup()`/`upsert_scan_result()` (delete-then-
  insert per scan_date, since these tables are re-derived each scan run,
  not append-only) and their `get_*_for_scan()`/`get_scan_results()`/
  `get_latest_scan_date()`/`get_scan_dates()` read-side counterparts, plus
  a new `fundamentals` table. `scan_results` gained a `direction` column
  (a symbol can have both a bullish and bearish scan_result row); a
  one-time migration drops and recreates that table if the column is
  missing (safe -- it's empty in any DB from before this column existed).
  **Step 15.0/15.3 (new)**: `zones` gained `distal_price`/`proximal_price`/
  `broken` columns (ALTER TABLE migration, preserved 244 real existing
  rows); `fundamentals` gained `rs_rating` (same migration pattern). New
  `update_rs_ratings()`. Real bug found and fixed: `upsert_fundamentals()`
  was `INSERT OR REPLACE`, which silently nulled `rs_rating` on every
  weekly earnings re-scrape since that column isn't in the function's own
  INSERT list -- switched to `INSERT ... ON CONFLICT DO UPDATE` on an
  explicit column list instead, verified via a real round-trip test that
  `rs_rating` now survives a re-scrape.

**Config/security**:
- `.env` (gitignored) — holds `KITE_API_KEY`, `KITE_API_SECRET`
- `.gitignore` — excludes `.env`, `.kite_token_cache`, `venv/`, `data/raw/`,
  `data/*.db`, `data/*.log`, `src/data/data/`, `*.log`,
  `.streamlit/secrets.toml`, `__pycache__/`
- `requirements.txt` — pandas, numpy, playwright, kiteconnect,
  python-dotenv, pyyaml, streamlit, plotly

**Known open items** (see Section "What's NOT built yet" further down):
Windows Task Scheduler automation not set up yet (needs Jonah's explicit
go-ahead before creating a standing scheduled task); the cash FII/DII
cloud-archiver idea (see Section 2b) -- RESOLVED since, see Step 11/13
below (Trendlyne replaced the need for it); "sector relative strength"
half of the fundamentals filter (Section 5) -- RESOLVED, see Step 13.

## 2. Data sources — the decision and the setup

### 2a. Price/OHLCV history + live quotes — Kite Connect (IMPLEMENTED & VALIDATED against real live data)
- **Plan required: "Connect" tier, ₹500/month.** Confirmed from zerodha.com/products/api: the free "Personal" tier only covers order/GTT/alerts management and portfolio/margin computation — it does NOT include historical candle data or WebSocket streaming, both of which we need. The paid Connect tier includes "full suite of APIs with realtime WebSocket streaming and historical candle data."
- Requires an active Zerodha trading/demat account regardless of tier.
- Signup: developers.kite.trade/signup → create an app → get `api_key` and `api_secret`

**Kite Connect auth flow design** (must be built before any data fetch works):
1. Construct login URL: `https://kite.trade/connect/login?api_key=<api_key>&v=3`
2. Open this URL in a real browser (you log in manually — 2FA required, this step is NOT automatable without your credentials/TOTP device)
3. After login, Kite redirects to your registered redirect URL with a `request_token` query param — capture this
4. Exchange it server-side: POST to `https://api.kite.trade/session/token` with `api_key`, `request_token`, and a checksum (`SHA-256(api_key + request_token + api_secret)`) → response contains `access_token`
5. `access_token` is valid until ~end of trading day (expires daily, typically around 6 AM IST next day) — must be regenerated each morning before use
6. Store `access_token` in a local gitignored token cache file (not `.env`, since it's regenerated daily — separate from the static `api_key`/`api_secret`)
7. All subsequent API calls use header `Authorization: token <api_key>:<access_token>`

**Historical data endpoint design**:
- `GET https://api.kite.trade/instruments/historical/<instrument_token>/<interval>` with `from`/`to` date params
- `interval` for our swing-trading use case: `day` (daily candles) — no need for minute-level data given the strategy's swing/multi-week timeframe
- `instrument_token` is NOT the stock symbol — Kite requires mapping symbol → instrument_token via their instruments master list (`GET https://api.kite.trade/instruments` — a large CSV, download once daily/weekly and cache locally, then look up tokens from it)
- Rate limits apply (check current Kite Connect docs for exact limits at build time — these are occasionally revised) — build in throttling/backoff, don't hammer the API across the full watchlist without delay

### 2b. Participant-wise OI, delivery %, cash FII/DII flows — NSE via Playwright

**Confirmed direct-URL pattern (verified via research, Aug 2026)**:
Participant-wise OI is published as a direct static CSV, not only through the
clickable report page:
```
https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
```
This is faster/simpler than clicking through the JS-rendered report page —
use Playwright's browser context to establish session cookies (visit
nseindia.com homepage first), then use `context.request.get()` to fetch this
URL directly with those cookies attached, rather than simulating clicks.

**Important resilience note**: NSE changed their CM bhavcopy file location/
format in July 2024 (old `.csv.zip` discontinued, replaced by a new "UDiFF"
format) — confirming these direct URL patterns DO break over time without
notice. The scraper must fail loudly and clearly (not silently return
garbage/empty data) when a URL pattern stops working, so it's obvious
something needs fixing rather than the system quietly running on stale or
missing data.

**Design flow** (this is the part that needed fleshing out):
1. Launch a Playwright browser context (headed while developing so you can see what's happening; headless once stable)
2. Navigate to `https://www.nseindia.com` first (homepage) — establishes session cookies required before any nseindia.com/nsearchives.nseindia.com request will succeed
3. Wait for page load + a short randomized delay (NSE's bot detection is sensitive to instant/robotic navigation patterns)
4. Use `context.request.get(direct_csv_url)` to fetch the participant OI CSV directly, reusing the warmed-up session cookies
5. If this returns 404/unexpected content: fall back to navigating the actual report page (Products → Derivatives → Equity Derivatives → Current Day's Reports → F&O Daily Reports) and locating the current download link dynamically via `page.wait_for_selector()` — this is the resilient fallback for when NSE changes the direct URL pattern
6. Parse the downloaded CSV into the schema (Section 3 below) — expected shape: a title row (skip), a header row, then 5 data rows (Client, DII, FII, Pro, TOTAL) across 14 columns (Future Index Long/Short, Future Stock Long/Short, Option Index/Stock Call/Put Long/Short, Total Long/Short)
7. Validate parsed shape before accepting it (right number of rows/columns, expected participant labels present) — if validation fails, treat as a scraper break, not valid data
8. **Timing**: reports publish ~5-6 PM IST, later on expiry days — schedule after 6:30 PM IST with a retry loop (e.g., every 30 min, up to 3 times)
9. **Failure handling**: distinguish "report not yet published" (retry later) from "URL pattern broke / site structure changed" (log loudly, stop retrying, needs human attention) — these are different failure modes
10. Same approach (direct URL where known + Playwright fallback) applies to delivery% and cash FII/DII — these will be built in subsequent steps once participant OI is proven working

**Real limitation, discovered and then SOLVED (Aug 2026)**: NSE's own live
JSON API (`nseindia.com/api/fiidiiTradeReact`) only ever returns the most
recent ~1-2 trading days -- not a historical backfill source, unlike
participant OI/delivery% (per-date archives). A cloud-hosted GitHub
Actions archiver was investigated as a fix (see the now-closed
`nse-fii-dii-archiver` side-repo) but its own smoke test proved GitHub
Actions runners get blocked by NSE's cloud-IP bot detection too --
confirmed via a real Actions run, not assumed.

**What actually fixed it**: `src/data/trendlyne_scraper.py`. Investigated
three alternatives empirically (reachability tested from the same session
NSE blocked): BSE India is reachable via plain HTTP but its FII/DII page
is behind Akamai bot-detection that blocks headless-browser fingerprints
(same wall as NSE); NiftyTrader showed no obviously embedded historical
data in a quick check; **Trendlyne worked** -- its public
`trendlyne.com/macro-data/fii-dii/latest/` page embeds a genuine ~1-
trading-month rolling window of real daily FII/DII cash flow directly in
server-rendered HTML (`<table id="cash-table-main-pastmonth"
data-jsondata="...">`, a JSON blob right in the HTML attribute) -- no
Playwright, no browser fingerprint, no bot-blocking observed, reachable
via a plain `urllib` GET. Confirmed to match NSE's own real published
figures exactly for a cross-checked date (28 Aug 2026: FII net
-Rs5,039.8cr, DII net +Rs5,183.9cr, matching nse_scraper.py's
independently-fetched real data for the same date bit-for-bit). Since a
single fetch always carries ~a month of trailing history, this closes the
gap directly -- even a multi-week miss self-heals on the next fetch, no
cloud archiver needed. `scanner.py`'s `refresh_all_data()` now calls this
instead of `nse_scraper.fetch_cash_fii_dii()` (which remains in the
codebase, still valid, usable as an occasional NSE cross-check per the
strategy prompt's own "third-party aggregators ... cross-check against
NSE as source of truth" guidance -- just no longer the primary daily path
since it can't backfill). `confluence.py`'s date parsing was made
format-agnostic (`_fii_dii_flow_signal` now tries both Trendlyne's ISO
`YYYY-MM-DD` and NSE's `DD-Mon-YYYY`) since the table now legitimately
mixes both. Validated end-to-end: HDFCBANK's real scan now shows all
three real confluence signals (FII/DII flow, OI buildup, delivery% trend)
computing genuine values instead of "insufficient data," using the ~23
days of real history from this fetch plus the multi-day participant_oi/
delivery_pct backfill Jonah ran -- and they tell a coherent story (Stage 4
+ FII net selling + OI building short + falling delivery% all agree
bearish -> High conviction=89 on the bearish setup; bullish setup
hard-capped Low=8 since everything conflicts).

### 2c. India VIX, sector indices
~~Same NSE Playwright flow, or available via the Kite Connect instrument
list if using that API.~~ **DONE (Step 10)**: built via the Kite Connect
instrument list path specifically (not NSE Playwright) -- see
`kite_ohlcv.py`'s `fetch_india_vix()`/`backfill_india_vix()` in Section 1
above. Chosen over NSE specifically because Kite's date-range API
backfills cleanly across any gap, which an NSE Playwright scrape of VIX
would not (same live-only limitation as cash FII/DII above). Sector
indices (for the "sector relative strength" half of the fundamentals
filter, Section 5 of the strategy prompt) are now wired in too -- same
Kite-INDICES pattern, see Step 13.

## 3. Data Schema (SQLite)

Claude Code should implement (adjust field names/types as needed, but keep this shape):

```sql
-- Raw price history, one row per symbol per day
CREATE TABLE ohlcv (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,          -- ISO date
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    source TEXT,                  -- e.g. 'kite_connect'
    fetched_at TEXT,
    PRIMARY KEY (symbol, date)
);

-- Participant-wise OI, one row per symbol/index per day per participant type
CREATE TABLE participant_oi (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    participant TEXT NOT NULL,    -- 'FII' | 'DII' | 'Client' | 'Pro'
    instrument TEXT NOT NULL,     -- 'index_fut' | 'stock_fut' | 'index_opt' | 'stock_opt'
    long_contracts INTEGER,
    short_contracts INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date, participant, instrument)
);

-- Cash market FII/DII net flows (market-wide, not per-symbol)
CREATE TABLE cash_fii_dii (
    date TEXT NOT NULL PRIMARY KEY,
    fii_net_cr REAL,
    dii_net_cr REAL,
    fetched_at TEXT
);

-- Delivery %
CREATE TABLE delivery_pct (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    traded_qty INTEGER,
    delivery_qty INTEGER,
    delivery_pct REAL,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date)
);

-- India VIX
CREATE TABLE india_vix (
    date TEXT NOT NULL PRIMARY KEY,
    close REAL,
    fetched_at TEXT
);

-- Detected zones (output of zones.py, persisted per scan run)
CREATE TABLE zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    kind TEXT,                    -- 'demand' | 'supply'
    zone_low REAL, zone_high REAL,
    origin_move_pct REAL,
    fresh INTEGER,                -- 0/1
    tests INTEGER
);

-- Detected VCP setups (output of vcp.py, persisted per scan run)
CREATE TABLE vcp_setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    direction TEXT,                -- 'bullish' | 'bearish'
    contraction_count INTEGER,
    contraction_ratio_ok INTEGER,
    volume_decay_ok INTEGER,
    trigger_level REAL,
    quality_score REAL,
    status TEXT                    -- 'forming' | 'triggered' | 'failed'
);

-- Composite scan results (what the dashboard reads)
CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    stage TEXT,                    -- Stage 1-4 classification
    zone_id INTEGER,
    vcp_id INTEGER,
    confluence_score REAL,
    conviction TEXT,               -- 'Low' | 'Medium' | 'High'
    notes TEXT,
    FOREIGN KEY (zone_id) REFERENCES zones(id),
    FOREIGN KEY (vcp_id) REFERENCES vcp_setups(id)
);
```

Rationale: raw data tables are append-only historical records (never overwritten, so you can always re-derive analysis); `zones`/`vcp_setups`/`scan_results` are re-computed each scan run and tied to `scan_date` so you can look back at what the system flagged on any past day, not just today.

## 4. Module Interface Specs

Keep these as separate, independently testable modules — this is what makes the "swap the confluence layer for another market later" plan actually work:

- `src/data/broker_client.py` — `fetch_ohlcv(symbol, start_date, end_date) -> pd.DataFrame`, handles auth/token refresh internally, raises a clear exception on auth failure (don't silently return empty data)
- `src/data/nse_scraper.py` — `fetch_participant_oi(date) -> pd.DataFrame`, `fetch_cash_fii_dii(date) -> dict`, `fetch_delivery_pct(date) -> pd.DataFrame`, `fetch_india_vix(date) -> float`. Each should raise a distinguishable "not yet published" exception vs. a genuine failure.
- `src/db.py` — thin wrapper around SQLite: `upsert_ohlcv(df)`, `get_ohlcv(symbol, start, end)`, equivalent for each table above
- `src/zones.py`, `src/vcp.py` — already exist, keep signatures as-is after the zone-merge fix
- `src/stage.py` — new module: `classify_stage(ohlcv_df) -> Literal['Stage 1','Stage 2','Stage 3','Stage 4']` per Section 2 of the strategy prompt (MA-based)
- `src/fundamentals.py` — DONE (Step 10): earnings-growth half of Section 5's filter, backed by `src/data/screener_scraper.py` (screener.in, weekly-cached). "Sector relative strength" half also DONE (Step 13) -- lives in `confluence.py` as a toggleable scoring input rather than a `fundamentals.py` eligibility filter (softer/more debatable signal than earnings growth, per discussion with Jonah), backed by `src/sector_mapping.py` + Kite sector-index OHLCV.
- `src/confluence.py` — implements Section 7 scoring: takes zone + VCP + participant OI + delivery% + FII/DII + VIX, returns a composite score and conviction label
- `src/scanner.py` — orchestrates: for each symbol in watchlist, call the above in sequence, write to `scan_results`
- `src/dashboard/app.py` — Streamlit entrypoint

## 5. Configuration
`config.yaml` (or `.toml`) at project root, NOT hardcoded in modules:
```yaml
watchlist:
  - RELIANCE
  - TCS
  # ... 30-50 liquid stocks with an active futures segment (a UNIVERSE
  # eligibility/liquidity filter only — futures are traded, options are not)
detection:
  zone_lookback: 3
  zone_min_move_pct: 8.0
  zone_max_base_bars: 15
  vcp_contraction_ratio_threshold: 0.75
  vcp_volume_multiple_trigger: 1.5
data:
  broker: kite_connect   # or the chosen alternative
  db_path: data/studio.db
```

## 6. Environment Setup (for Claude Code to script)
1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt` (add: `playwright`, `streamlit`, `sqlalchemy` or raw `sqlite3`, the broker SDK e.g. `kiteconnect`, `pyyaml`, `pytest`)
3. `playwright install chromium` (downloads the browser Playwright drives)
4. Copy `.env.example` → `.env`, fill in broker API key/secret (never commit `.env` — add to `.gitignore`)
5. `python3 scripts/init_db.py` — creates the SQLite schema above
6. Daily use: `python3 scripts/refresh_data.py` (fetch latest OHLCV + NSE data) → `python3 scripts/run_scan.py` (run detection across watchlist) → `streamlit run src/dashboard/app.py` (view results)

## 7. Dashboard — page-by-page spec
- **Page 1: Daily Scan** — table of `scan_results` for the latest `scan_date`, sortable by `confluence_score`/`conviction`, columns: symbol, direction, stage, VCP quality score, conviction, contraction count. Clicking a row goes to Page 2 for that symbol.
- **Page 2: Symbol Detail** — price chart (candlestick) with the detected zone(s) shaded and VCP contraction points marked, a data panel showing current participant OI/delivery%/FII-DII alongside their recent trend, and the full Section 9 output structure (entry trigger, stop, target, R:R, invalidation) rendered as text.
- **Page 3: Watchlist Management** — add/remove symbols from `config.yaml`'s watchlist via the UI rather than hand-editing the file.
- **Page 4: Scan History** — browse past `scan_date` results, useful for reviewing "what did the system flag last week."

## 8. End Product — what a normal day looks like
1. After market close (~6-7 PM IST, once NSE reports are published), you run `refresh_data.py` (or it's scheduled via cron/Task Scheduler)
2. `run_scan.py` executes automatically after refresh, populating `scan_results`
3. You open the Streamlit dashboard, review the ranked shortlist on Page 1
4. For any high-conviction setup, click into Page 2 to visually confirm the zone/VCP looks right on the chart and check confluence data
5. If it holds up, you place the trade manually in your broker app — the system never places trades itself
6. Next day, you can look back at Page 4 to see how yesterday's flagged setups are developing

## 9. Testing Strategy
- `pytest` unit tests for `zones.py`/`vcp.py` logic (synthetic data is fine here — it's testing pure logic, not data connectivity)
- Integration tests for `broker_client.py`/`nse_scraper.py` that hit real endpoints (these will need your real credentials to run — mark them clearly as requiring live credentials, e.g. `@pytest.mark.live_data`, so they can be skipped in contexts without access)
- A manual validation checklist for Phase 2 (comparing detected zones/VCPs against real charts) — this should be a documented, repeatable process, not a one-off

## 10. Logging & Error Handling
- Use Python's `logging` module, not print statements — log to both console and a rotating file
- Every data-fetch failure should be logged with enough context to debug (which symbol, which date, which source, what the actual error was) — never fail silently
- The scanner should continue past a single symbol's failure (log it, skip it) rather than crashing the entire run

## 11. Security
- `.env` for all secrets, in `.gitignore` from the first commit
- If this project is ever pushed to a remote repo (even private), double-check no API keys/tokens are in commit history
- Kite Connect's daily access token should be stored only in the local `.env` or a local token cache file (also gitignored), refreshed each trading day — never logged in plaintext

## 12. Future Roadmap (not MVP, but keep the door open)
- Swapping in a non-India confluence module (Section "Implementation Architecture" of the strategy prompt) for US/global markets — the `confluence.py` interface should be generic enough that this is a new file, not a rewrite
- Backtest harness (Phase 7)
- Possibly alerting (e.g., notify when a watchlist symbol's VCP triggers) — explicitly out of scope for MVP, don't build until the core loop is proven reliable

## 13. Hosting & Scheduling (decision made — Option A, local-first)

**Decision**: run locally on Jonah's own PC for now, not cloud-hosted. Reasoning: Kite Connect's daily re-authentication (login → 2FA → request_token → access_token exchange) cannot be fully automated without either (a) manual login each trading morning, or (b) scripting the TOTP 2FA code via a library like `pyotp`, which means storing a TOTP secret in code — a deliberate security tradeoff to be decided explicitly later, not defaulted into. This constraint exists regardless of hosting location, so local-first avoids adding cloud security/maintenance complexity for a problem hosting doesn't solve anyway.

**Local architecture**:
- Windows Task Scheduler runs the data-refresh + scan pipeline automatically each evening (~6:30-7 PM IST, after NSE publishes reports)
- Jonah does the Kite login/exchange manually each trading morning (~30 seconds, existing flow in `kite_auth.py`)
- Dashboard runs via `streamlit run`, accessible at `localhost` in the browser
- Limitation (accepted): only accessible while the PC is on; not accessible remotely/from phone

**Future option (not now)**: cloud VM hosting (AWS/DigitalOcean, a few $/month) with cron instead of Task Scheduler, for remote/phone access — revisit only if local-only access becomes a real limitation in practice, not preemptively. Still hits the same Kite daily-login constraint.

## 14. Build Phases — STATUS

**Phase 1 — Core engine** ✅ DONE (zones.py, vcp.py, stage.py all built, tested against synthetic AND real data, two real bugs found via real data and fixed: zone-merge time-proximity, VCP tolerant scoring)

**Phase 2 — Real OHLCV data connection** ✅ DONE (kite_auth.py + kite_ohlcv.py, validated against real Kite API — RELIANCE 475 real trading days fetched and persisted)

**Phase 3 — NSE Playwright automation** ✅ DONE (nse_scraper.py — participant OI, cash FII/DII, delivery% all validated against real live NSE data and persisting to DB). India VIX built via Kite instead (Step 10, see Section 2c) — closes what was this phase's remaining gap. Step 10 also added `backfill_participant_oi()`/`backfill_delivery_data()`, written and logically sound but **not yet validated against live NSE data from within Claude Code's own session** (nseindia.com blocked that session's network path entirely — a known real limitation, not a code bug) — needs one real validation run on Jonah's own machine.

**Phase 4 — Confluence scoring module** ✅ DONE, including the real-data wiring (Step 9): Stage+Zones+VCP scoring (Stage as hard gate/multiplier, confirmed capital-preservation design) PLUS real participant_oi/delivery_pct/cash_fii_dii wired in as additional scoring inputs, validated on real RELIANCE and HDFCBANK data.

**Phase 5 — Universe scanner** ✅ DONE (Step 10): `scanner.py` orchestrates fundamentals -> zones -> VCP -> stage -> confluence across the whole watchlist, persisting to zones/vcp_setups/scan_results; `scripts/refresh_data.py` + `scripts/run_scan.py` are the daily-use entrypoints. Validated end-to-end on real data (RELIANCE correctly excluded by the new fundamentals filter; HDFCBANK produced two distinct real bullish/bearish verdicts).

**Phase 6 — Studio dashboard** ✅ DONE (Step 10, extended Step 12): 5 pages now (Daily Scan, Symbol Detail, Position Calculator, Watchlist Management, Scan History) in `src/dashboard/app.py`, validated by actually running `streamlit run` and driving it in a real browser against the real DB (see Section 1 above for detail).

**Phase 7 — Backtest harness** ❌ NOT STARTED (correctly deferred — not needed yet).

**Step 15 — Minervini/SEPA alignment pass** 🔄 IN PROGRESS (started Sep 1 2026, decided with Jonah in chat, engineering translation handed off via `CLAUDE_CODE_BUILD_PROMPT_step15.md`). 8 sub-steps (15.0-15.8), tracked individually as they land:
- **15.0 — Precision zone boundaries** ✅ DONE, see Section 1 above.
- **15.1 — Position Calculator dual mode** ✅ DONE. New Vehicle toggle (Futures/Cash equity) on `page_calculator()`, using the Step 12 pending-override session-state pattern. Futures mode unchanged (lot-size fetch, lot rounding); Cash mode is a plain share count from risk%/stop distance, no lot rounding, no margin math -- lot-size UI hidden entirely in Cash mode. `PROJECT_CONTEXT.md`'s "futures only" summary line corrected. Real bug found and fixed while validating: `capital`/`risk_pct` had no stable `key=`, so a rerun triggered by the UNRELATED "Fetch from Kite" button was silently wiping capital the user had already typed -- same root cause/fix as the Step 12 lot-size bug (give every widget whose value must survive a same-page rerun a real `key=`). Validated live: HDFCBANK futures (1 lot=650 shares, Rs12,545 risk, capped by lot granularity) vs. cash (777 shares, Rs14,996 risk, no lot-rounding artifact) at the identical entry/stop/capital -- materially different in exactly the expected way.
- **15.2 — Full NSE F&O universe** ✅ DONE. `kite_ohlcv.get_fno_universe()` (see Section 1 above) replaces the 40-name watchlist; `scanner.get_scan_symbols()` computes `get_fno_universe() minus config.yaml's watchlist` (the recommended-default exclude-list repurposing from the handoff doc, applied and logged here rather than silently assumed) and caches the universe list to `data/fno_universe_cache.json` so `run_scan()` doesn't need a live Kite session just to enumerate symbols -- only `refresh_all_data()` (which already needs Kite per-symbol) re-fetches it live. Dashboard: "Watchlist Management" renamed "Excluded Symbols" with flipped Add/Remove semantics (Include -> Exclude); Daily Scan's KPI row now shows "Scanned universe" instead of "Watchlist size"; Symbol Detail's symbol selector now just lists whatever has OHLCV data, since there's no more hand-picked list to union in. `config.yaml`'s `watchlist:` cleared to `[]` on this transition (keeping the old 40-name INCLUDE list would have flipped to EXCLUDING exactly those stocks). Real bug found and fixed: `config.update_watchlist()`'s regex didn't match the new inline `watchlist: []` form, and would have written a bare `watchlist:\n` for an empty list -- which YAML parses as `null`, not `[]`, crashing anything that iterates `cfg["watchlist"]`. Progress logging (`[N/total] symbol: ...`, periodic elapsed-time lines) added to both `refresh_all_data()` and `run_scan()` per the doc's requirement.

  **Real, full end-to-end validation (Sep 1 2026)**: ran the actual full universe live, not simulated. `get_fno_universe()` returned 210 real stocks (6 index-future underlyings like NIFTY/BANKNIFTY correctly filtered out via the cash-equity-listing discriminator). `refresh_data.py`: 210 symbols' OHLCV backfilled in **211 seconds**, zero failures. `run_scan.py`: 210 symbols scanned (including full screener.in fundamentals scrapes for ~170 symbols never seen before) in **382 seconds**, zero failures. **Total end-to-end: ~10 minutes** -- comfortably fits the 6:45 PM Task Scheduler slot with a large margin before any reasonable time Jonah would check results; not a concern, but measured and reported as the doc required rather than assumed. DB counts scaled as expected: `fundamentals` 39->210 (full universe), latest `scan_date` went from ~58 scan_results rows (old 40-watchlist) to **278** (73 High / 111 Medium / 94 Low). Confirmed visually in a real browser: genuinely new symbols never seen in this project before (PATANJALI, TATACONSUM, VBL) showing up as real High-conviction setups.
- **15.3 — 8-point Trend Template completion + new RS Rating module** ✅ DONE, see Section 1 above (`stage.py`'s `classify_trend_template()` and new `rs_rating.py`).
- 15.4 — Fundamentals: sales/margin acceleration + earnings-quality flags — not started.
- 15.5 — VCP base staging + (Time)(Depth)(Ticks) notation — not started.
- 15.6 — Confluence: wire in RS Rating — not started (depends on 15.3).
- 15.7 — Risk framework: R:R floor, portfolio concentration + new trade-log table, exit plan, gap disaster plan — not started (depends on 15.0, done).
- 15.8 — Documentation hygiene — ongoing alongside each sub-step, same discipline as every prior step.

**Beyond the original 7 phases**: Position Calculator (Step 12) and Nifty-alignment + sector-relative-strength confluence signals (Step 13) -- both DONE, see Section 1 above. MVP scope explicitly locked to equities-only; commodities and index-as-tradeable-symbol support deferred to a later phase (Jonah's decision, Aug 31 2026).

**Windows Task Scheduler automation (Section 13)** ✅ DONE (Aug 31 2026, with Jonah's explicit go-ahead): task `SD_VCP_Studio_DailyRefresh` registered, runs `scripts\run_daily_refresh.ps1` (which calls `refresh_data.py` then `run_scan.py`, logging to `data/task_scheduler.log`) weekdays at 6:45 PM local time, `StartWhenAvailable=True` so a missed trigger (laptop off) runs as soon as the machine is next available -- confirmed via `Get-ScheduledTask`/`Get-ScheduledTaskInfo` (next run correctly showed 2026-08-31 18:45 +05:30, DaysOfWeek=Mon-Fri).

**Dashboard data-freshness panel** ✅ DONE (Aug 31 2026): `db.get_data_freshness()` + a new panel on the Daily Scan page show the last saved date per raw source (ohlcv/india_vix/participant_oi/delivery_pct/cash_fii_dii) with age-based color coding, so a gap (Task Scheduler missed a few days) is visible at a glance rather than only discoverable by noticing odd scan results. The existing "refresh everything" button's caption was also rewritten to explicitly state what it backfills and the one exception (cash FII/DII). Validated by running the dashboard again in a real browser.

**Real bug fixed, found by the user actually running the code**: `nse_scraper.py`'s CLI unconditionally tried to parse `sys.argv[2]` as a `YYYY-MM-DD` date regardless of mode, so `backfill-oi 15` crashed trying to parse `"15"` as a date. Fixed to only parse a date for the `oi`/`delivery` modes; `backfill-*` modes parse their second arg as an integer day-count instead. Not yet re-confirmed against live NSE data (same network-block limitation as before) -- Jonah re-running this on his own machine is still the outstanding validation step.

**Git**: repo initialized and pushed to `https://github.com/jonahrtimothy/sd_vcp` (public), Aug 31 2026. Reviewed staged diff for secrets before pushing -- only variable names/docstrings referencing `api_key`/`access_token` etc., no actual credential values (`.env`, `.kite_token_cache`, `venv/`, and everything under `data/` are all gitignored and were confirmed absent from the diff).

**Cash FII/DII gap** ✅ RESOLVED (Aug 31 2026) -- not via the cloud archiver (its smoke test proved GitHub Actions also gets blocked by NSE), but via `trendlyne_scraper.py`, a real alternate source that carries ~a month of trailing history per fetch. See Section 2b for the full story. The `nse-fii-dii-archiver` side-repo is kept only as a documented dead-end (its README explains why), not under active development.

**Nifty trend alignment + sector relative strength: DONE (Step 13, Aug 31 2026)**, closing the last two open confluence/fundamentals gaps:
- `confluence.py` gained `_nifty_alignment_signal()` -- reuses `stage.py` completely as-is on NIFTY 50's own OHLCV (no new analytical logic, same engine just pointed at the index; the index's OHLCV lives in the SAME `ohlcv` table as stocks, keyed by (symbol, date) already, so no schema change) and applies the same aligned/neutral/conflicting logic already used for the stock's own Stage.
- `confluence.py` gained `_sector_strength_signal()` -- compares the stock's sector index return vs. NIFTY 50's return over a trailing window (config: `sector_rs_lookback_days`, default 20), classifies outperforming/underperforming/in-line, and scores alignment with the setup direction the same way. Sector name -> Kite index mapping lives in new `src/sector_mapping.py` (deliberately incomplete -- only sectors with a clear, confirmed-real Kite INDICES instrument are mapped; an unmapped sector honestly reports "not computed," never a guessed loose match).
- Both are TOGGLEABLE independently of each other: Nifty alignment always runs (it's cheap and unambiguous); sector strength specifically has an on/off switch (`fundamentals.sector_strength_enabled` in config.yaml, default true) exposed as a dashboard checkbox on Watchlist Management, per Jonah's explicit request, since it's a softer/more debatable signal than the earnings-growth filter. The config is re-read fresh on every call (not cached at import time like the other confluence thresholds) so toggling the checkbox takes effect immediately, no dashboard restart needed -- confirmed via a real toggle-and-verify test (checkbox off -> config.yaml correctly shows `false`, back on -> `true`, comments preserved throughout).
- `scanner.py`'s `refresh_all_data()` now also backfills NIFTY 50 plus every sector index in the mapping table (via the same `backfill_ohlcv(..., segment="INDICES")` path used for VIX) -- fetches all of them unconditionally rather than only whichever sectors happen to be in the current watchlist, avoiding a chicken-and-egg problem (which sectors are needed isn't known until fundamentals.py's cache is populated, which happens in `run_scan`, which runs after `refresh_all_data`).
- Both signals were validated as gracefully honest, NOT validated end-to-end with real index data yet: the Kite access_token expired partway through this session's testing (normal daily expiry, not a bug) before the new NIFTY 50/sector-index fetch could be exercised live. Real scan output confirms the correct honest fallback behavior in the meantime (`"insufficient NIFTY 50 history (0 bars, need >=210) -- Nifty alignment not computed"`, `"insufficient NIFTY FIN SERVICE/NIFTY 50 history..."`) -- Jonah re-running `kite_auth.py login`/`exchange` and then `refresh_data.py` is the outstanding step to get real values flowing.

**Real bug found and fixed as a side effect of this work**: testing the sector mapping on HDFCBANK (a real watchlist symbol) surfaced that `screener_scraper.py`'s quarterly-growth parser only recognized a "Sales" row label, but banks/NBFCs/insurers report "Revenue" instead on screener.in -- meaning every financial-sector stock was silently failing fundamentals scraping AND losing its sector/industry info entirely (the sector parse never ran because the exception from the missing "Sales" row fired first). Fixed two ways: (1) `ROW_ALIASES = {"Revenue": "Sales"}` normalizes the label so financial-sector earnings growth now parses correctly (confirmed real: HDFCBANK now shows `accelerating`, EPS YoY=17.9%, sector="Financial Services" -- previously `insufficient_data`/`sector=unknown`); (2) sector/industry parsing was moved to run independently of the quarterly-growth table, with quarters-table failures now degrading gracefully (sector/industry still captured, earnings_trend left honestly as `insufficient_data`) instead of losing everything on any row-naming quirk.

**Real gap found via user walkthrough, fixed (Step 14, Aug 31 2026)**: `vcp.py`'s `check_trigger()` -- which checks whether price has actually closed beyond the trigger level on volume expansion -- existed and was tested from the start, but the production pipeline (`scanner.py`) never called it. Every setup on the Daily Scan was silently labeled as if still "forming," even when real price action had already confirmed the trigger (or invalidated the pattern entirely) days earlier. Found by walking through HDFCBANK's actual recent candles with Jonah: Aug 27 closed at 711.00 (below the 715.10 bearish trigger) on 46.5M volume (~2-3x the recent average) -- a real, valid trigger by the strategy's own rule -- while HDFCBANK's *bullish* setup, mirror-image, had been invalidated by that same move (price broke the wrong way for it).

Fixed:
- `scanner.py`'s `_scan_one_direction()` now calls `check_trigger()` after `detect_vcp()`. A `"failed"` status is NOT persisted as a scan_result at all -- per Section 4's own rule ("the setup is invalidated -- exit or stand aside"), an invalidated pattern isn't a valid opportunity, so surfacing it as one would contradict the strategy. Real effect on a live scan: 6 setups (HDFCBANK-bullish, KOTAKBANK, TITAN, JSWSTEEL, NTPC, ONGC) that were previously showing up as valid High/Medium-conviction opportunities got correctly filtered out once their patterns were checked against what price had actually done since.
- `db.get_scan_results()` now LEFT JOINs `vcp_setups` to expose `trigger_status` alongside the score/conviction.
- Dashboard: a new "Trigger" column (forming/TRIGGERED badge) on the Daily Scan and Scan History tables; Symbol Detail shows a prominent green "ALREADY TRIGGERED" or red "ALREADY FAILED" banner (with the same not-a-live-signal framing throughout this conversation) alongside the trigger level, computed live via `check_trigger()` on whatever OHLCV is currently loaded.
- Validated end-to-end on real data: HDFCBANK bearish correctly shows "TRIGGERED" (confirmed against the real Aug 27 close+volume), HDFCBANK bullish correctly shows "FAILED" with the red banner.

**Scope decisions locked in with Jonah (Aug 31 2026)**: MVP = equities only -- commodities (gold, crude, etc.) and other global-market parameters explicitly deferred to a later phase, not part of this build; USDINR/crude dropped from the near-term plan entirely on this basis (they'd only ever have been a confluence INPUT for equity setups, never tradeable instruments themselves, and even that is deferred now). Indexes (NIFTY 50, BANK NIFTY) as tradeable watchlist symbols also deferred, same reasoning -- though the detection ENGINE already works on index OHLCV as-is (proven by the Nifty-trend-alignment feature below, which runs stage.py on NIFTY 50's own price data); what's still needed for full index support is fundamentals.py skipping screener.in for index symbols, the OI-buildup signal reading `index_fut` instead of `stock_fut`, and delivery% reporting "not applicable" rather than an always-empty lookup -- none hard, just not built.

**Position Calculator (NEW, Step 12)**: `src/dashboard/app.py`'s `page_calculator()`. Entry/stop/target1/target2/lot-size/capital/max-risk% -> suggested lots and quantity (risk-based sizing, Section 8: "Position size = f(stop distance, max risk per trade)", rounded to real lot multiples not a theoretical share count), actual risk amount, R:R and potential P&L per target. Lot size can be auto-fetched from Kite's real F&O instrument list (`kite_ohlcv.get_lot_size()`, NEW -- confirmed against real data: HDFCBANK=650, RELIANCE=500, TCS=225) rather than typed in blind. A "Send to Position Calculator" button on Symbol Detail prefills entry/stop/target1(2R)/target2(3R) from the currently computed setup. Nothing here is persisted anywhere (no file, no DB) -- capital figures are personal and this repo is public, so this is a deliberate non-persistence choice, not an oversight.

Two real Streamlit bugs found and fixed while building/testing this live in a real browser (not just written blind):
1. `st.session_state["nav"]` (and later the calculator's `*_w` keys) cannot be reassigned directly from code that runs AFTER the widget owning that key has already been instantiated in the same script pass -- Streamlit raises `StreamlitAPIException`. Fixed via a "pending override" pattern: button handlers set a plain transport key (`_nav_override`, `calc_prefill_pending`, `_calc_lot_fetch_pending`) and call `st.rerun()`; the actual widget-bound key gets updated at the TOP of the next run, before that widget is created. This is now the standard pattern for any future cross-page/cross-widget navigation in this dashboard -- don't reassign a widget's own `key` directly from a button handler.
2. The lot-size field's fetched value (e.g. 650) silently reverted to the widget's bare default (1) on the very next rerun (e.g. typing into the Capital field), because the field had no stable `key` and its `value=` was being recomputed from a plain variable, not session_state -- meaning every position-size calculation after the first keystroke elsewhere on the page was silently wrong (computed against lot_size=1). Confirmed via real math check (capital=500,000 gave Lots=39 instead of the correct 0) before being caught and fixed. Root cause and fix are the same as #1: give every prefillable widget a stable session-state-backed `key`, never mix `value=` with programmatic reassignment.

### Remaining phase detail:

**Phase 7 — Backtest harness** (later, once the above is stable and observed running for a few weeks):
- Historical replay of the detection + confluence + risk rules against past data to evaluate the contraction-ratio and volume-decay thresholds empirically, not just illustratively

## 15. Non-negotiable engineering standards
- **No hardcoded credentials** — use a `.env` file (gitignored) for API keys/secrets, never commit them
- **No synthetic data standing in for real data** in any delivered/"done" module — synthetic data is fine only for fast unit-test sanity checks of pure logic
- Every data-fetching function must handle and clearly surface failures (stale data, auth expiry, missing report) rather than silently returning empty/wrong results
- Git version control from the start; meaningful commit messages per phase
- Config (watchlist, thresholds like `min_move_pct`, `contraction_ratio_threshold`) should live in a config file, not be hardcoded inside detection logic — you'll want to tune these against real results

## 16. Reference
See `trading_strategy_system_prompt.md` (same project) for the full analytical ruleset — Stage filter, zone rules, VCP scoring rules, fundamental filter, confluence scoring, risk framework, output format. This build brief implements that spec; it does not redefine the strategy.
