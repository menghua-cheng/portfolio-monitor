# Progress — US Stock Portfolio Monitor

Status legend: `todo` · `in-progress` · `verified`

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 0 | Scaffold & environment | verified | `pip install -r requirements.txt` OK; core imports OK |
| 1 | Config & portfolio list CLI | verified | add/list/remove ticker persists to yaml + DB |
| 2 | Database layer | verified | idempotent upsert unit test passes (5 tests) |
| 3 | Data fetching + cross-check | verified | real fetch of AAPL/MSFT/NVDA: 507 clean rows each, last bar 2026-07-21, internal validator passes; cross-check via Tiingo (activates when TIINGO_API_KEY set) |
| 4 | Indicators (SMA/EMA) | verified | 4 unit tests pass (hand-computed SMA/EMA, year-line null<240, NaN->None); real tickers computed & stored |
| 5 | Trend-transition signals | verified | 7 unit tests: golden/death cross, long-up/short-down, long-down/breakout, current-state label, no-dup on unchanged state |
| 6 | Charts | verified | AAPL.png rendered (958x713, 116KB); candlesticks + 5 MA overlays + volume panel visually confirmed |
| 7 | Daily HTML report | verified | 480KB self-contained HTML: 3 tickers, 3 embedded charts, state chips, MA legend + detail cells, signals, disclaimer (structural check; browser ext unavailable for screenshot) |
| 8 | Email delivery | verified (dry-run) | .eml built: multipart/alternative+related, 3 inline PNG CIDs, cid: placeholders rewritten. Live send pending user App Password (permissioned action) |
| 9 | Pipeline + scheduling | verified | full pipeline runs (3 tickers, DB updated, local HTML self-contained, email dry-run); cron wrapper runs standalone (exit 0); --send fallback + --tickers filter work. Crontab entry documented (not auto-installed). |
| 10 | Docs & close-out | verified | README written; clean-shell run via wrapper OK (exit 0); DB integrity confirmed (507 clean rows/ticker, 0 null OHLC, 240-line warm, last bar 2026-07-21); 16 tests pass |

## ✅ All 10 steps verified — all 7 requested features complete.

Final acceptance (2026-07-22): fetch→indicators→signals→charts→report→email(dry-run)→DB all green
for AAPL/MSFT/NVDA. Live email send is the only step requiring user action (Gmail App Password in
`.env`, then run with `--send`).

## Session 2 (2026-07-22) — enhancements

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 11 | Interactive Plotly charts | verified | render_interactive_html builds a 12-trace figure (candlestick + 5 SMA + 5 EMA + volume), valid JSON parses, unified hover shows date/OHLC/SMA/EMA/volume; plotly.js inlined ONCE per report (self-contained, offline); static PNG kept as `<noscript>` failsafe + email image. Browser screenshot N/A (extension not connected). |
| 12 | Granular MA-cross details + 雙重趨勢訊號 | verified | detect_cross_events covers all adjacent pairs (5/20/60/120/240) → 「月線向上突破季線」etc.; double-signal note like 「14日前 2025-06-26 周線已向上突破月線」. 4 new unit tests; validated on real history (AAPL 23 / MSFT 23 / NVDA 31 events). |
| 13 | --send graceful skip + cron | verified | `--send` with blank SMTP now SKIPS email (no .eml, exit 0) instead of dry-run fallback; email uses PNG (plotly.js stripped). Cron installed: `0 6 * * 2-6 …/run_daily.sh --send`; wrapper run exit 0, email skipped. |
| 14 | Tiingo enablement | pending user | `.env` created with `TIINGO_API_KEY=` (blank). Code already supports it; paste a free key to activate the yfinance-vs-Tiingo cross-check, then re-run to verify. |

| 15 | Bilingual report + language switcher | verified | Report is now a full UTF-8 `<!doctype html>` doc with a top-right EN/中文 switcher (default en_US, choice remembered via localStorage). Every user-facing string carries en+zh via a `t(en,zh)` Jinja macro + CSS/JS toggle (33 balanced en/zh spans). Email renders single-language EN (0 i18n spans, no switcher/JS). Tiingo cross-check LIVE (OK vs tiingo). |
| 16 | FIX: charts + note follow the switcher | verified | Bug: chart labels & data-source note were baked bilingual so both langs always showed. Fix: figure rendered English-only (0 CJK in figure JSON, legend names neutral SMA5/EMA5); per-chart `__pmChartI18n` registry + `applyChartLang()` relayouts titles/axes/vol-hover on toggle; `data_source_note` made bilingual. Holistic scan: 0 stray CJK outside toggle-spans/registry (only the 中文 button + plotly.js calendar internals remain). **Runtime-verified in headless Google Chrome (Playwright): EN → 34 en-spans visible / 0 zh-spans / chart labels English / 0 stray CJK; 中文 → 0 en-spans / 34 zh-spans / chart labels 價格·量·成交量·收盤與均線; toggle back to EN restores fully.** |

| 17 | Always-present trend summary in table | verified | Table was blank ("—") on days with no fresh cross. Added `signals.summarize_trend`: MA alignment (多頭/空頭/多空交錯 via `_ma_alignment`), multi-line breakout/breakdown tags (雙重/三重突破・跌破, counts distinct same-dir adjacent-pair crosses in `recent_window_days`=30), and `detect_recent_cross_events` recent-crossings list with days-ago. 4 new tests. Real data: AAPL 多頭排列; MSFT 多空交錯 + 雙重突破 (季線突破半年線 14d + 周線突破月線 15d); NVDA recent crosses. Runtime-reverified in Chrome (47/0 span split, charts follow). |

| 18 | Chart cross markers w/ hover detail | verified | Each MA crossing in the visible window is marked on the interactive chart: ▲ green triangle (突破/breakout) / ▼ red triangle (跌破/breakdown) at the crossing MA level (`charts._cross_markers`). Hover pops up the detail msg; language-switchable via the registry + `Plotly.restyle` on marker `text`. Runtime-verified in headless Chrome: real `Plotly.Fx.hover` popup shows "Weekly line breaks above Monthly line · 2026-07-06" (EN) ⇄ "周線向上突破月線 · 2026-07-06" (ZH); switcher still isolates languages (47/0 spans, 0 stray CJK, charts+markers follow). |

| 19 | Highlight multi-breakout clusters on chart | verified | `charts._breakout_clusters` groups same-direction crosses within window_days (bounded from first cross → tight bursts) spanning ≥2 distinct pairs → 雙重/三重/四重突破・跌破. Each cluster drawn as a shaded band (opacity 0.06+0.05·degree, dotted border) + bold ★ label trace (`meta:'clusterUp'/'clusterDown'`, top for up / bottom for down), language-switchable via registry+restyle. Real data: AAPL 3×雙重突破 + 三重跌破; NVDA 2×三重突破. Runtime-verified in Chrome: ★ labels "★ Double breakout"⇄"★ 雙重突破", switcher still isolates (PASS). **Hover popup lists the constituent crossings** (short on-chart `text` label + detailed `hovertext`): e.g. 三重向上突破 → 周線突破月線 / 月線突破季線 / 季線突破半年線; language-switchable (restyles both text+hovertext). |

24 tests pass. New dep: `plotly>=5.20` (in requirements.txt + venv).

## Session 3 (2026-07-26) — signal backtest

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 20 | Signal backtest (best strategy vs buy-and-hold) | verified | New pure engine `backtest.py`: 4×4 grid of `(entry degree N, exit degree M)` strategies over a cumulative-from-fast degree cascade (edge-triggered), long-only, filled at next bar's split/dividend-adjusted open, compounded all-in/all-out, 5bps/side cost, open position marked to market; common all-warm window; best by CAGR (hindsight-labeled) vs buy-and-hold. Rendered as a compact bilingual block per ticker (no new chart), ephemeral (no new DB table). 10 new unit tests; **live run AAPL — best Entry×2/Exit×4 +58.7% vs buy-and-hold +66.3% over 2025-06-27→2026-07-24** (report block renders EN/中文). Design captured in `CONTEXT.md` + `docs/adr/0001-0004`. |

34 tests pass. Design docs added: `CONTEXT.md` (glossary), `docs/adr/` (4 ADRs),
`docs/plans/backtest-implementation.md`.

## Session 4 (2026-07-26) — auto company-name lookup

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 21 | Auto-fetch company name on `config add` | verified | `config add SYMBOL` with no name now resolves it via `fetch.fetch_company_name` — Tiingo metadata first (reliable, uses `TIINGO_API_KEY`), yfinance `.info` as best-effort fallback (its quoteSummary endpoint currently 401s), blank if neither resolves. Explicit name still wins; an existing name is preserved on re-add. `add_ticker` loads `.env` itself since the config CLI doesn't call `load_config()`. 8 new tests (lookup precedence + add wiring, isolated onto tmp csv/db). **Live: `config add GOOGL` → "Alphabet Inc - Class A"** (watchlist restored after). |

42 tests pass.

## Session 5 (2026-07-28) — uv packaging + console scripts

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 22 | `pyproject.toml` + console scripts (uv-native) | verified | Added `pyproject.toml` (hatchling, src layout, deps migrated from requirements.txt, pytest in a `dev` dependency-group) with console scripts `portfolio-monitor` → `pipeline:_cli` and `portfolio-monitor-config` → `config:_cli`; committed `uv.lock`. `uv sync` installs the project editable, so **no `PYTHONPATH` is needed anymore** — fixes the recurring `ModuleNotFoundError: No module named 'portfolio_monitor'` / `attempted relative import` errors when running from a fresh shell. Verified with `PYTHONPATH` unset: `uv run portfolio-monitor-config list`, `uv run python -m portfolio_monitor.config list`, `uv run portfolio-monitor --help`, and `uv run pytest -q` (42 pass). README updated to the console-script flow (plain-venv/`requirements.txt` kept as a fallback). `scripts/run_daily.sh` left as-is (its `PYTHONPATH=src` form stays compatible with both install methods). |

42 tests pass.

## Session 6 (2026-07-29) — multi-ticker add/remove

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 23 | `config add/remove` accept multiple symbols | verified | `add`/`remove` now take one-or-more symbols (`add NVDA AAPL TSM`), each auto-name-resolved on add. Custom names moved from a positional arg to `--name` (single-symbol only; errors with multiple) — the old `add SYM "Name"` positional form is removed. `remove` reports per-symbol presence. 5 new tests (multi-add, multi-remove, `--name` guard, single+`--name`, remove presence). **Live: `add GOOGL TSM` → Alphabet + Taiwan Semiconductor; `remove GOOGL TSM` restored the watchlist.** 47 tests pass. README updated. |

47 tests pass.

## Session 7 (2026-07-30) — backtest explorer (window · time scale · switchable signals)

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 24 | Switchable signal rules (`rules.py`) | verified | Signals extracted from the engine into a registry: `degreeN`, `cross:S/L`, `price:MA`, `align`, `slope:MA`, each returning a per-bar "condition holds" mask whose **rising edge** the engine triggers on — so every family inherits the fire-once-on-completion semantics the degree cascade had. `RuleSpec` is frozen/hashable with a round-trippable `label` (`parse_rule(spec.label) == spec`, asserted). Group tokens `degrees/crosses/prices/slopes/all` expand against the active ladder, dedupe, preserve order. 25 new tests (both directions per rule, unwarmed-MA handling, degree-beyond-ladder, parser rejects 9 malformed forms). |
| 25 | Bar time scale (`bars.py`) | verified | Daily→weekly/monthly aggregation (open=first, high=max, low=min, close/adj_close=last, volume=sum) bucketed by ISO week / calendar month, each bar dated with the **last real trading date in its bucket** (never a synthetic period end, so the final bar is never in the future). Per-interval MA ladders — daily 5/20/60/120/240, weekly 4/13/26/52/104, monthly 3/6/12/24/60 — because periods count in bars; `min_history_years` turns the slowest rung into the calendar history a run needs. 14 new tests. |
| 26 | Windowed/parameterized engine (`backtest.run_spec`) | verified | New `BacktestSpec` (start, end, interval, ma_periods, ma_kind, entries, exits, cost, window_days) alongside the report's unchanged `run_backtest`. **Data window and trade window separated**: MAs are computed over all loaded history, then clipped — a `--start` inside the data trades on a warm ladder, a `--start` before the first all-warm bar is clamped with a note. `--end` snaps to the last bar on or before it. Barely-warm windows carry a "widen the window" hint; too-short/out-of-range windows return a clean no-result with a reason. `StrategyResult` now holds two `RuleSpec`s and exposes `entry_degree`/`exit_degree` (0 for non-degree rules), so `report.build_backtest_view` is untouched. 17 new tests. |
| 27 | Explorer CLI (`portfolio-monitor-backtest`) | verified | Third console script. Flags map 1:1 onto the three axes (`--start/--end`, `--interval/--ma-periods/--ma-kind`, `--entry/--exit`), plus `--sort cagr\|return\|drawdown\|trades\|winrate`, `--top`, `--json`, `--list-rules`, `--refresh/--years` (default years = requested span + ladder warm-up + margin). Reads stored `prices` by default (no network); `--refresh` fetches and upserts. Every run prints the buy-and-hold row and an "N of M cells beat buy-and-hold" + in-sample caveat (ADR-0004). 23 new tests (flag→spec, history estimate, row loading, rendering, ranking, exit codes). **Live on AAPL:** daily default grid 2025-06-26→2026-07-28 (273 bars) best degree1×degree4 +69.0% vs B&H +69.9%; sub-window `--start 2025-09-01 --end 2026-03-31` (146 bars) best −2.6% vs B&H +10.7%; mixed rules `price:sma20`×`cross:sma20/sma60` +66.2% (9/9 cells traded); `--interval weekly --refresh --years 14` → 630 weekly bars 2014-07-11→2026-07-29, best +1194% vs B&H +1533%; `--interval monthly` → 110 monthly bars from 2017-06-30, best +578.9% vs B&H +920.5%. Malformed rules/intervals exit 2 with an actionable message; a ticker with no stored rows exits 1. |

138 tests pass (47 → 138). New design doc: `docs/adr/0005-backtest-explorer-is-a-separate-cli.md`
(supersedes the "no CLI" clause of ADR-0003); `CONTEXT.md` gained *Signal rule*, *Bar interval*,
*MA ladder*, *Warm-up vs trade window*, and a generalized *Strategy*.

## Session 8 (2026-07-30) — multi-break rules · incremental price cache · tracker

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 28 | `multiN` rules — 雙重/三重/四重突破・跌破 | verified | New rule family: **any** N distinct adjacent pairs crossed the same direction within the window, i.e. exactly what `summarize_trend` already tags — deliberately looser than `degreeN`, which demands the fastest N cumulatively (ADR-0006). Aliases `double`/`triple`/`quad`, group token `multis` (starts at 2, since multi1 is a vacuous restatement). Cost: one rule function + one parser case, no engine change — the ADR-0005 registry paying off. 12 new tests, including the discriminating case where pairs 1 and 3 cross (multi2 fires at the second cross; degree2 never fires because 月/季 never crossed). **Live AAPL 13y daily:** `degree1 × multi4` +2554% vs buy-and-hold +2548% — the only cell of 15 traded to beat B&H. |
| 29 | Incremental price cache + split detection | verified | SQLite is now the source of truth (`cache.py`, ADR-0007). New tables: `prices_ref` (cached Tiingo/Stooq series), `price_sync` (per ticker+source bookkeeping), `corporate_actions` (rebase audit), plus an index on `prices(date)`. Each sync re-fetches a ~12-day overlap and compares stored vs fresh closes: agree → append tail; consistent ratio → **split**, rescale all older cached bars by the factor (volume inversely) then append; inconsistent → refuse to guess, refetch the window, audit it; adj_close-only drift → dividend adjustment. `fetch.*` gained a `start=` override; `get_prices` gained start/end bounds so `load_history(window_years=…)` trims what is *returned* without shrinking what is *stored*. New `portfolio-monitor-cache <status\|sync\|actions>`. 31 new tests. **A real bug was caught by the split test:** the rescale cutoff was the *comparison window's* start, leaving bars between it and the fetch start stranded on the old basis — fixed to the first fresh date, regression-guarded. **Live:** pipeline run 1 = 1 new bar each for MSFT/NVDA, AAPL up-to-date; run 2 = all three `up-to-date, 0 new bar(s)`; AAPL's 14y/3526-row cache preserved while the report still reads its 2y window. |
| 30 | Performance + signal tracker | verified | `tracker.py` (pure) + `tracker_report.py` + `templates/tracker.html.j2` + `portfolio-monitor-tracker`. Per-ticker adjusted total return over 1D/1W/1M/3M/6M/1Y/YTD (anchored on the last bar *before* each boundary, so 1D = since the previous close and YTD = since last year's final close), distance off the 52-week high, equal-weight PORTFOLIO row with the per-horizon ticker count printed, dependency-free inline-SVG index sparkline, every recorded signal with its forward adjusted return and a directional verdict, and hit rate overall + per ticker (ADR-0008). Horizons a ticker cannot answer are excluded, never assumed flat. **A second real bug was caught by the index test:** `dropna()` after normalizing intersected dates instead of excluding short constituents, so a late-listing ticker both diluted the index and truncated its window while each series stayed based outside it — fixed by filtering constituents to the window before normalizing, and `index_members` is now reported. 49 new tests. Runs at the end of the daily pipeline inside try/except so it can never cost the user their report. |
| 31 | Tracker report bilingual verification | verified | **Runtime-verified in headless Google Chrome**: EN view renders all three sections + sparkline with correct up/down colouring; ZH view renders 投資組合績效與訊號追蹤 / 績效（調整後總報酬）/ 等權重指數 / 訊號與其後價格表現 / 訊號命中率. Span audit on the switcher build: 59 en-spans / 59 zh-spans balanced, **0 stray CJK** outside zh spans. Two English leaks found in the ZH screenshot (`today`, `3 of 3 tickers`) and a third (the `1D 3 · 1W 3 …` horizon counts) were made bilingual, with `test_zh_view_has_no_english_leaks_in_the_variable_strings` guarding them. |

| 32 | Measured and closed the two real hot spots | verified | Profiling the pipeline per stage (rather than assuming) showed the incremental fetch alone was only **1.3x** faster than a full refetch — per-request latency dominates, so asking for 12 days costs nearly what 2 years costs. Two fixes followed. (a) `cache.is_current` / `last_expected_bar`: skip the request entirely when the cache already holds the newest bar that could exist (most recent weekday), and `_reference_caught_up` for the reference source, which lags a day and so is *never* "current" by a calendar test — it skips only when within 1 bar of the primary **and** already synced today. Data step 8.1s → 0.05s on a cache-current re-run. (b) The backtest was rebuilding the same four per-pair window masks for all 16 grid cells, in a per-bar Python loop: memoized per run on `RuleContext` (with the scope invariant documented) and vectorized with `searchsorted` → **3.14s → 0.22s (14x)**; a 49-cell grid over 3287 daily bars now runs in 0.41s. Pipeline wall clock ~13s → 7.9s (~4s once the day's bar is cached). 8 new tests, including a call-counting test asserting 8 distinct masks regardless of cell count, so a refactor cannot silently undo the 14x. **A third real bug fixed here:** `_reference_caught_up` compared SQLite's `datetime('now')` (UTC) against a local `date.today()` — wrong by a day for ~8h daily in GMT+8, silently. Now UTC on both sides, regression-tested. |

251 tests pass (138 → 251). New design docs: `docs/adr/0006` (multi vs degree),
`docs/adr/0007` (cache + split rescale), `docs/adr/0008` (equal-weight + hit-rate-is-not-a-backtest),
`docs/plans/cache-and-tracker.md`. New settings: `tracker.lookback_days`,
`tracker.index_days`, `cache.overlap_days`. New console scripts:
`portfolio-monitor-cache`, `portfolio-monitor-tracker`.

## Session 9 (2026-07-30) — interactive standalone explorer

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 33 | JS engine port (`static/engine.js`) | verified | Second implementation of `backtest.py` + `rules.py` so a static HTML file can recompute without a server (ADR-0009). Covers all three intervals, both MA families, all six rule families, the warm-up/clamp/end-snap window logic, next-adjusted-open fills, and the full metric set. Reproduces the pandas semantics that would otherwise silently diverge: Mon..Sun weeks, SMA null until `period`, EMA `adjust=False` recursing from x[0] but null until `period`, bars keeping the last real date in their bucket. Pair window masks are memoized as in Python, so a 484-cell grid stays fast. |
| 34 | Python/JS parity harness | verified | `explorer.PARITY_SPECS` (14 specs) drives BOTH engines over one **rounded** payload — the exact numbers the browser sees, since comparing unrounded data would hide a real divergence — and `tests/js/parity_runner.js` runs the JS side under node. Every metric of every cell must agree to **1e-9**. Matrix covers all intervals, ema, a custom ladder, every rule family, an explicit window, a clamped start, both out-of-range windows, and non-default cost/window/lookback/threshold; a meta-test asserts the matrix still mentions every family so coverage can't silently shrink. **Passed on the first run, all 14 specs.** Node is not a project dependency, so the test skips without it — an accepted gap, documented in ADR-0009 and partly covered by the page self-check. |
| 35 | Standalone HTML explorer | verified | `portfolio-monitor-backtest --html [PATH] [--html-years N]`. One file, no server, no network, opens from `file://`. Controls for ticker/interval/ladder/MA family/start/end/entry/exit/window/cost/ranking, five presets, click-a-row equity curve vs buy & hold with entry+exit fills, price chart with the MA ladder, hand-rolled SVG charts with hover crosshair (plotly would have added ~3.7MB to a file whose value is portability). A `test_render_html_is_self_contained` check asserts **no** external loader — script src, link, img, iframe, @import, fetch, XHR, WebSocket, dynamic import — and that the only absolute URLs are XML namespaces. **Live: 3 tickers × 6 years = 221 KB.** |
| 36 | Page self-check | verified | Every generated page embeds Python's results for its default spec and re-runs them on load, showing a green or red banner, so a file opened after the Python side moves on still states whether it can be trusted. **Runtime-verified in headless Chrome: banner green, "16 cells verified", grid computed in 4 ms.** |
| 37 | Runtime interaction + cross-check | verified | Drove the real controls in headless Chrome (interval→weekly, entry `multis,degree1`, exit `cross:sma4/sma13`, start 2021-06-01, cost 30bps) and dumped the resulting DOM: ladder auto-switched to 4/13/26/52/104, start **clamped to 2022-07-22 (MA warm-up)** exactly as Python does, 4 cells / 2 traded, equity chart 2 paths, price chart 6 paths, self-check still green. Then re-ran that identical spec through the **Python** engine on the identical embedded payload: window `2022-07-22 → 2026-07-29 (211 weekly bars)`, B&H `+125.6% (+22.4% CAGR)`, `degree1 × cross:sma4/sma13` `+62.7% / CAGR +12.9% / DD -24.5% / 9 trades / 56% win` — **identical to the browser on every figure, including the clamp note**. Equity-curve path (not parity-coverable, since Python exposes no curve) tested against the metrics it must agree with. |

276 tests pass (251 → 276). New design doc: `docs/adr/0009-interactive-explorer-duplicates-the-engine-in-js.md`.
New assets: `src/portfolio_monitor/static/{engine.js,ui.js}`, `templates/explorer.html.j2`,
`tests/js/parity_runner.js`. CONTEXT.md gained *Explorer* and *Parity*.

## Notes / decisions log
- 2026-07-22: Project scaffolded. Using **Python 3.11** venv (system python3.14 lacks ensurepip and
  sudo is unavailable; 3.11 also has better prebuilt wheels for the data stack).
- Data source: yfinance (primary). **Stooq is now blocked** (anti-bot PoW wall + pandas-datareader
  0.11.1 stooq reader unimplemented), so cross-check moved to **Tiingo** (free key, set `TIINGO_API_KEY`).
  Without a key: single-source yfinance + strong internal validation (NaN, non-positive, monotonic
  dates, dup dates, high<low, staleness >7d, extreme daily move >50%).
- 2026-07-26: Backtest added. Degree = cumulative-from-fast cascade, deliberately stricter than the
  report's `summarize_trend` "any distinct pairs" counting (ADR-0001). Fills use the split/dividend-
  adjusted next-bar open `open×adj_close÷close` to stay lookahead-safe and split-consistent (ADR-0002).
  Embedded in the daily report and computed ephemerally, no CLI and no `backtest_results` table by
  design (ADR-0003). The "best" strategy is in-sample/hindsight-selected and labeled as such rather
  than split train/test, since ~2y history barely covers the sma240 warm-up (ADR-0004).
- 2026-07-30 (session 9): Interactive explorer as a single static HTML file (ADR-0009). The
  decision that matters is accepting a **second engine implementation** in JavaScript. The
  alternatives each failed the brief: precomputing grids isn't a backtester (you can re-sort
  but not re-parameterize); sql.js solves the easy half (data access) for ~1.4MB and still
  needs a strategy engine; Pyodide avoids duplication entirely but costs 10MB+ and multi-second
  startup, failing the "static file you can email yourself" test. So duplication was the price,
  and it is paid with a parity test over a 14-spec matrix at 1e-9 plus an on-load self-check
  banner in every page — because a drifted engine does not crash, it quietly disagrees with the
  CLI. Known gap, accepted and documented: the parity test needs node, which is not a project
  dependency, so it skips on a machine without node. Charts are hand-rolled SVG for the same
  reason the file exists at all — plotly.js would have added ~3.7MB to an artifact whose whole
  value is portability, and the interactivity that matters is changing parameters, not zooming.
- 2026-07-30 (session 8): Three things, one theme — make the stored data do the work.
  (a) **`multiN` joins `degreeN` rather than replacing it** (ADR-0006): the report has tagged
  雙重/三重/四重突破 since Session 2 and it was the one signal the backtest couldn't evaluate,
  but the cascade is the stricter and more meaningful event, so both exist under names that
  keep them apart. (b) **The cache is incremental, and a split rescales stored history**
  (ADR-0007) — the hard part isn't caching, it's that Yahoo silently rewrites the past, so
  each sync re-checks an overlap of bars it already holds. Rescaling (rather than
  refetch-and-replace) is what preserves a 14-year cache the explorer built; the
  inconsistent case still falls back to a full refetch, because guessing a factor from
  noisy data is worse than losing depth. Every rescale is audited and logged at WARNING —
  a silent bulk price rewrite is otherwise indistinguishable from corruption.
  (c) **The tracker is equal-weight and its hit rate is explicitly not a backtest**
  (ADR-0008): no share counts exist to weight by, and every signal is measured to the same
  right-hand edge, so the caveat is printed inline next to the number rather than buried.
  Nothing new is persisted for the tracker — it is all derivable from `prices` + `signals`,
  the same reasoning as ADR-0003.
  Three genuine bugs surfaced from writing discriminating tests and from *measuring* rather
  than assuming — none from review: the split rescale cutoff (stranded bars mid-series), the
  index `dropna()` (a late-listing ticker diluting *and* truncating the index), and a
  UTC-vs-local date comparison in the sync-recency check (silently wrong for ~8h a day in
  GMT+8). All three are regression-guarded.
  The performance lesson worth keeping: **profiling contradicted the intuition twice.** The
  incremental fetch — the thing the request was about — bought only 1.3x, because per-request
  latency dominates payload; the win came from not making the request at all. And the
  *backtest* turned out to be as large a cost as the fetching (35% of the run), for a reason
  nothing to do with I/O: identical per-pair masks recomputed once per grid cell.
- 2026-07-30: Backtest made explorable along three axes without touching the daily report
  (ADR-0005, which supersedes only the "no CLI" clause of ADR-0003 — embedded + ephemeral still
  holds). Key choices: (a) signals became a **registry of rules** whose per-bar mask the engine
  rising-edge-triggers, so one abstraction covers degree cascades, MA-pair crosses, price-vs-MA,
  alignment and slope, and adding a family costs one function + one parser case; (b) **MA periods
  count in bars, so each interval owns its ladder** — reusing the daily 5/20/60/120/240 on weekly
  bars would silently redefine every line; (c) **data window ≠ trade window** — MAs warm over all
  loaded history and only then get clipped, so `--start` never trades a half-warm ladder, and a
  start inside the warm-up is clamped out loud. Rejected: parameterizing the daily report via
  `settings.yaml` (edit-file-then-rerun is a poor exploration loop, and cron has no user to turn
  knobs) and a notebook (untested code outside the package). Caveat carried forward: a bigger grid
  makes ADR-0004's hindsight selection *worse*, so the CLI always prints buy-and-hold and the
  beat-count rather than just a winner.
- 2026-07-28: Adopted `pyproject.toml` (hatchling, src layout) with `uv.lock` and two console scripts
  so `uv sync` installs the package and no `PYTHONPATH` is needed. `requirements.txt` kept as a
  pip/venv fallback (dependency lists may drift; pyproject is the source of truth for uv users).
