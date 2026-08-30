# S&D + VCP Studio — Project Context (handoff document for Claude Code)

**Last updated**: milestone — Step 10 DONE, a big one: Phases 5 and 6 are
now both complete, plus the India VIX gap is closed and fundamentals
filtering is real (not a placeholder). Concretely: (1) `screener_scraper.py`
+ `fundamentals.py` implement Section 5's earnings-growth quality filter
against real screener.in data, cached weekly; (2) India VIX now comes from
Kite (not NSE) specifically because Kite's date-range API backfills
cleanly across any gap; (3) `kite_ohlcv.py` and `nse_scraper.py` both
gained backfill functions so a gap of any length (laptop off for days)
catches up automatically rather than needing a fixed lookback window --
except cash FII/DII, which has no confirmed historical endpoint and is
therefore the one genuinely unrecoverable gap (see "cloud archiver" idea
below); (4) `scanner.py` (Phase 5) orchestrates the whole pipeline across
the watchlist; (5) the Streamlit dashboard (Phase 6, all 4 pages, dark
theme with color-coded conviction/direction/stage badges and a candlestick
chart with shaded zones + VCP markers) is built and was validated by
actually running it in a real browser against the real DB. Real-data
validation highlight: RELIANCE's technically strong VCP (score=80) now
gets correctly EXCLUDED before the technical scan even runs, because its
real earnings trend is declining -- Section 5's "fundamentals decide
eligibility, technicals decide timing" rule working as designed, not just
described. HDFCBANK produced two distinct real verdicts in the same run
(bearish/Stage-4-aligned -> High conviction; bullish/Stage-4-conflicting ->
Low), extending the Stage-gate validation to a second real stock beyond
RELIANCE.

**Follow-up, same day**: Jonah gave the go-ahead on Task Scheduler and git,
both now done. Windows Task Scheduler task `SD_VCP_Studio_DailyRefresh` is
registered (weekdays 6:45 PM, `StartWhenAvailable` so a missed day catches
up automatically once the laptop is next on). The project is now a git
repo, pushed to `https://github.com/jonahrtimothy/sd_vcp` (public) --
reviewed the staged diff for secrets before pushing (none found; `.env`/
`.kite_token_cache`/`venv/`/`data/` all correctly gitignored). The
dashboard also gained a data-freshness panel (last saved date per source,
color-coded by age) so a Task Scheduler gap is visible at a glance. One
real bug found and fixed from Jonah actually running the new backfill CLI:
`nse_scraper.py`'s argument parsing crashed on `backfill-oi 15` (tried to
parse `"15"` as a date) -- fixed, but still needs Jonah's re-confirmation
since NSE remains unreachable from this session.

**Still being discussed, not yet built**: a genuinely SEPARATE small
cloud-hosted project (its own repo, explicitly not merged into this
codebase) that runs a daily GitHub Actions job to archive cash FII/DII
data -- the one data source here that can't backfill gaps through NSE's
live-only endpoint. Needs a repo name/visibility decision, and either `gh`
CLI or Jonah creating the empty repo manually. See SYSTEM_BUILD_PROMPT.md
Section 2b for the full design.

**One real limitation hit this session**: nse_scraper.py's new backfill
functions could not be validated against live NSE data from within Claude
Code's own session -- nseindia.com blocked/timed out every connection
attempt, while the same session reached Kite and screener.in successfully.
This matches the limitation already documented in
`trading_strategy_system_prompt.md` Section 11 (NSE blocks cloud/
datacenter IPs) and is not a code bug -- but it does mean the backfill
code needs one real validation run on Jonah's own machine before being
fully trusted, even though it reuses the already-validated single-shot
fetch functions.

## What this project is
A personal trading analysis system for Jonah, combining supply/demand zone
theory + VCP (Minervini methodology) + India-specific confluence data
(FII/DII, participant OI, delivery%), for swing trading + futures only (no
options — "F&O-eligible" anywhere in these docs is a stock UNIVERSE
liquidity/eligibility filter only, never a signal to trade options), India-
first with a market-agnostic core designed to scale globally later. Local
project path: `C:\Jonah\sd_vcp`.

## The two living spec documents (both in the project folder — READ THESE FIRST)
1. **`trading_strategy_system_prompt.md`** — the ANALYTICAL spec: Stage
   filter, zone rules, VCP scoring rules, fundamental filter, confluence
   scoring (including the Stage-as-hard-gate weighting rule), risk
   framework, output format. This defines WHAT counts as a valid setup.
2. **`SYSTEM_BUILD_PROMPT.md`** — the ENGINEERING spec: data sources (Kite
   Connect for OHLCV — implemented; NSE Playwright scraping for
   OI/delivery%/FII-DII — implemented), database schema, module
   interfaces, config, dashboard design, hosting/scheduling decision
   (local-first, Section 13), testing/logging/security standards, build
   phase status (Section 14 — phases 1-4 mostly done, 5-7 not started).
   This defines HOW to build it, and its Section 1 has a full accurate
   inventory of what code currently exists and what each module does.

## Decisions locked in so far
- Market: India equities/futures, swing timeframe (days-weeks), NO options
- Broker API: Zerodha Kite Connect, "Connect" tier (₹500/month). Account
  set up, authenticated, AND actively fetching real historical data
  successfully (confirmed Aug 31 2026) — app named "SD VCP Studio",
  redirect URL http://127.0.0.1:8000/kite/callback (a placeholder, not a
  real running server — used only to capture request_token from the URL
  bar during the manual daily login step)
- NSE data (OI/delivery%/FII-DII): free, via Playwright browser automation
  — requires a real browser session (cookies), can't be plain HTTP
  requests. India VIX scraper NOT yet built (remaining gap).
- Storage: local SQLite, working, real data persisting
- Dashboard: Streamlit (planned, not built)
- Hosting/scheduling: LOCAL-FIRST decision made (Option A) — runs on
  Jonah's own PC, Windows Task Scheduler for the evening data-refresh
  automation, manual Kite login each trading morning (2FA can't be fully
  automated without a TOTP-secret-in-code tradeoff, deliberately not
  taken by default). Cloud hosting deferred until/unless remote access
  becomes a real need. Full reasoning in SYSTEM_BUILD_PROMPT.md Section 13.
- Implementation now moves to Claude Code — design/strategy discussion
  continues in chat; Claude Code should read both spec docs above before
  making changes, and update SYSTEM_BUILD_PROMPT.md's Section 1 (current
  state) and Section 14 (phase status) as milestones are completed, same
  discipline as this file has followed throughout
- No synthetic data allowed in the delivered/final system — synthetic data
  was used only to validate the core detection logic works

## What's already built and tested (in `src/`)
- `zones.py` — supply/demand zone detection from OHLCV. **Merge bug FIXED
  and validated against real data**: 7 overlapping candidate zones on real
  RELIANCE data correctly collapsed to 2 clean zones after the fix.
- `vcp.py` — VCP contraction detection, scoring (0-100), breakout trigger
  check. **Scoring logic upgraded from strict all-pass to tolerant/
  fractional** after real data showed the strict version was too brittle
  (a single noisy mid-pattern step zeroed the whole score). Validated:
  RELIANCE real 4-contraction pattern went from score=40 (ratio_ok=False,
  vol_decay_ok=False) to score=80 (both True) after the fix — matched a
  hand-calculated prediction exactly before the code change was made.
- `stage.py` — Stage 1-4 trend classification (MA-structure based, Section
  2 of strategy prompt). Validated against synthetic uptrend/downtrend/flat
  data (all 4 cases pass) AND real RELIANCE data (correctly classified
  Stage 4 given real falling MA structure). Requires >=210 bars of history.
- `confluence.py` — combines Stage + Zones + VCP + real participant
  OI/delivery%/cash FII-DII into one conviction verdict (Low/Medium/High).
  Stage acts as a hard gate/multiplier, not an equal-weighted input: a
  conflicting Stage caps conviction at Low no matter the VCP score
  (capital-preservation design, explicit user requirement). Validated on
  real RELIANCE data: VCP score=80 (would look like a strong buy alone)
  correctly got hard-capped to Low conviction, due to Stage 4 conflict —
  confirmed working as designed. Step 9: the real confluence data
  (participant OI, delivery%, cash FII/DII) is now wired in too, each
  requiring >=2 real days of history to compute a trend and honestly
  reporting "insufficient data" rather than guessing when fewer are
  available.
- `tests/test_detection.py` — original synthetic validation script (still
  useful as a fast pure-logic sanity check).
- `src/test_real_detection.py` — runs zones.py/vcp.py against real DB data
  for any symbol; this is now the primary validation script going forward.
- `src/data/nse_scraper.py` — DONE and validated against REAL
  live NSE data (not synthetic). Three working functions, all tested:
  - `fetch_participant_oi()` — direct archive CSV, confirmed working (48
    rows: 4 participants × 12 instrument/side combos)
  - `fetch_cash_fii_dii()` — live JSON API (`nseindia.com/api/fiidiiTradeReact`),
    confirmed working (real Aug 28 2026 data: DII net buy ₹5,183.93cr,
    FII/FPI net sell ₹5,039.8cr — only returns ~1-2 recent days, not
    historical backfill)
  - `fetch_delivery_data()` — MTO file, confirmed working (3,336 raw rows
    across ALL security types — bonds/G-secs/SME/etc — filtered down to
    2,629 equity-only rows via `series == "EQ"`)
  - All three: Playwright-warmed session, clear ScraperError on
    failure/format-change, raw file always saved to `data/raw/` for audit
- Zone/VCP modules have NOT been run against real market data yet (only
  synthetic) — near-term next step once OHLCV (Kite) is wired in.
- `src/db.py` — DONE and validated with real persisted data.**
  SQLite schema (8 tables per SYSTEM_BUILD_PROMPT.md Section 3), upsert
  functions (idempotent — re-running doesn't duplicate, confirmed via
  INSERT OR REPLACE test) for participant_oi/cash_fii_dii/delivery_pct,
  read-helpers for verification. Wired into nse_scraper.py's __main__ so
  every scrape run now persists automatically. Confirmed real counts:
  participant_oi=48, cash_fii_dii=2, delivery_pct=2629. Database lives at
  `C:\Jonah\sd_vcp\data\studio.db`. Tables for ohlcv/zones/vcp_setups/
  scan_results/india_vix exist in schema but are empty — not built yet.
- `src/data/kite_auth.py` — DONE and validated against the real
  Zerodha API.** Implements login_url generation, request_token→access_token
  exchange, local token caching (`.kite_token_cache`, gitignored), and a
  `test` command that confirmed a real authenticated profile() call
  (logged in as the real account holder). Access token must be regenerated
  daily (expires ~end of trading day) via `login` + `exchange` — this is
  Kite's design, not something to automate around (2FA-gated).
- `src/data/kite_ohlcv.py` — DONE and validated against real
  live data.** Downloads/caches Kite's instruments master list (refreshed
  daily), resolves symbol -> instrument_token, fetches daily OHLCV candles
  via `kite.historical_data()`, persists to the ohlcv table. Confirmed
  working: RELIANCE, 100-day lookback, 68 real trading-day candles fetched
  and saved, prices in a plausible real range (~1260-1370).
- `db.py` gained `upsert_ohlcv()`/`get_ohlcv()` to support the above.
- **Step 10 additions** (full detail in SYSTEM_BUILD_PROMPT.md Section 1):
  `screener_scraper.py` + `fundamentals.py` (Section 5 earnings-growth
  filter, real screener.in data, weekly cache), `kite_ohlcv.py` gained
  `backfill_ohlcv()`/`fetch_india_vix()`/`backfill_india_vix()`,
  `nse_scraper.py` gained `backfill_participant_oi()`/
  `backfill_delivery_data()` (NOT yet validated live -- NSE blocked this
  session's network), `scanner.py` (Phase 5 orchestration),
  `src/dashboard/app.py` (Phase 6, all 4 pages, validated by actually
  running it), `config.yaml`/`src/config.py` (centralized tunables).

## Environment setup notes (from real troubleshooting)
- Project lives at `C:\Jonah\sd_vcp`, Python venv at `C:\Jonah\sd_vcp\venv`
- PowerShell activation: `.\venv\Scripts\Activate.ps1` (not the bare
  `activate` — that's the cmd.exe form)
- If PowerShell blocks script execution: `Set-ExecutionPolicy -Scope Process
  -ExecutionPolicy Bypass` (session-only, safe, run once per terminal)
- Zip-extraction-and-merge was unreliable (older zips missing newer files
  caused confusion) — direct file creation + paste in VS Code proved more
  reliable for this project and is now the standard workflow going forward
- Installed and confirmed working: pandas, numpy, playwright (+ chromium
  browser via `playwright install chromium`)

## What's designed but NOT built yet
- "Sector relative strength" (the other half of Section 5's fundamentals
  filter) — needs a sector-index price data source, not decided yet
- Windows Task Scheduler automation (Section 13) — code is ready
  (`scripts/refresh_data.py` then `scripts/run_scan.py`), needs Jonah's
  explicit go-ahead on timing before a standing scheduled task gets created
- The cash FII/DII cloud-archiver (separate repo, Section 2b) — needs a
  repo name/visibility decision from Jonah
- Backtest harness (Phase 7) — correctly deferred until the above has run
  for a few weeks

## Immediate next step
Phases 1-6 are all now complete and validated on real data (Phase 4's
gaps closed, Phase 5/6 built this session). What's left is genuinely just
the three items above, all of which are either external-service decisions
or things needing Jonah's explicit go-ahead rather than more engineering:
(1) confirm Task Scheduler timing and let Claude Code set it up, (2) decide
the cloud-archiver repo's name/visibility, (3) run `nse_scraper.py`'s new
backfill functions once on Jonah's own machine to get the real-NSE
validation this session's network couldn't provide. Also worth noting: the
OI-buildup and delivery%-trend confluence signals will stay at
"insufficient data" until the daily refresh has run for a few more days
and accumulated multi-day history in the DB — this is expected, not a bug.

## Handoff to Claude Code
This file, along with `trading_strategy_system_prompt.md` and
`SYSTEM_BUILD_PROMPT.md`, are all present in `C:\Jonah\sd_vcp` alongside
the actual working code. When starting a Claude Code session on this
project:
1. Read all three `.md` files first, before making any changes — they
   contain the full analytical spec, engineering spec, and current status
2. Follow the "Non-negotiable engineering standards" in
   SYSTEM_BUILD_PROMPT.md Section 15 (no synthetic data in the delivered
   system, no hardcoded secrets, real validation before moving to the
   next phase)
3. Update SYSTEM_BUILD_PROMPT.md's Section 1 (current state) and Section
   14 (phase status) after completing each milestone — same discipline
   maintained throughout this project so far, don't let it lapse
4. For strategy/analytical questions (is this rule right, should this
   threshold change, does this match the intended behavior) — bring those
   back to the chat conversation with Jonah rather than deciding
   unilaterally; Claude Code's job is implementation, chat is where
   strategy decisions get made
5. Real validation discipline established throughout this project: every
   module gets tested against real data (not just synthetic) before being
   considered done, and real data test failures have twice surfaced
   genuine design bugs (zone-merge time-proximity, VCP scoring
   brittleness) that synthetic data alone would never have revealed —
   keep testing against real data at every step, don't skip it because
   synthetic tests pass
