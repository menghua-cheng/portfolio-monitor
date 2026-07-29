# Multi-break rules · price cache · tracker — implementation plan

Three requests that look independent and share one theme: **make the stored data do the
work**. Traces to `CONTEXT.md` (Multi-break, Price cache, Re-basing, Tracker, Hit rate,
Equal-weight portfolio) and ADR-0006/0007/0008.

## Phase 1 — `multiN` rules (cheap, do it first)
The report has tagged 雙重/三重/四重突破 since Session 2; it was the one signal the backtest
could not evaluate. Add it as a rule family, do **not** fold it into `degree`.

- `rules._rule_multi`: count how many adjacent pairs have a `direction` cross inside the
  trailing window, fire at `>= n`. Reuses `_pair_cross_dates` + `_in_trailing_window`, so
  it is ~8 lines.
- Parsing: `multiN` / `multi:N`, plus the word aliases `double`/`dual`/`triple`/`quad`.
  Group token `multis` starts at **2** — `multi1` is legal but vacuous, and the report only
  tags 雙重 and up.
- The test that matters is the **discriminating** one: build a frame where pairs 1 and 3
  cross but pair 2 never does, then assert `multi2` fires and `degree2` does not. Anything
  weaker passes even if the two families are accidentally the same function.

## Phase 2 — incremental price cache (`cache.py`)
The speed request. The trap is that upstream rewrites history.

- Schema (`db.py`): `prices_ref` (cached cross-check sources, PK includes `source` —
  keep it out of `prices`, whose one-row-per-date shape every caller depends on),
  `price_sync` (bookkeeping: what we hold, when we looked, what happened),
  `corporate_actions` (rebase audit), index on `prices(date)`.
- `fetch.*` gains `start=` so a sync can ask for the tail instead of years.
- `db.get_prices` gains start/end bounds — the cache may now be far deeper than the
  caller's window.
- `detect_rebase(stored, fresh)` is **pure** and carries the whole decision: compare median
  close ratio over shared dates against `REBASE_TOL_PCT` (0.5%, comfortably between an EOD
  revision and the smallest common split at 20%), and its spread against
  `RATIO_CONSISTENCY_PCT` to distinguish a clean split from noise. Fall through to
  `adj_close` for dividend-only shifts.
- **The cutoff is the first date the fresh fetch supplies, not the first date compared.**
  Rows from the cutoff on get overwritten by the upsert; rows before it need rescaling.
  Using the comparison window's start leaves the bars between the two starts on the old
  basis — a cliff in the middle of the series. This was a real bug; the split test caught it.
- `db.rescale_prices` multiplies prices by the factor and divides volume by it, for rows
  before the cutoff. Audit every call.
- `load_history(…, window_years=…)` is the pipeline's entry point: sync, then serve from
  SQLite, trimming what is *returned* without shrinking what is *stored*.
- `portfolio-monitor-cache <status|sync|actions>` for visibility. A user who cannot see
  what the cache holds cannot trust it.
- Tests stub `fetch.fetch_yfinance` against a **real temp SQLite DB** — the behaviour under
  test is the interaction between fetch and storage, so mocking the storage would test
  nothing. Cover: first full fetch, tail-only second fetch, up-to-date, split rescale
  (assert no >1% day-over-day jump survives), volume inverse scaling, inconsistent →
  refetch, force, failed fetch leaves the cache intact.

## Phase 3 — tracker (`tracker.py` + `tracker_report.py` + template + CLI)
Pure computation, then display, then I/O — same split as the report.

- Horizons anchor on the last bar **before** each boundary, so 1D means "since the previous
  close" and YTD means "since last year's final close". Every lookup snaps backwards to a
  real bar, because boundaries land on weekends.
- Adjusted closes throughout: a split must never appear as performance.
- A horizon a ticker lacks history for is `None` — **excluded, not flat** — and the
  portfolio row prints how many tickers each horizon averaged.
- The index series must filter constituents to the window **before** normalizing. Doing it
  after (an intersecting `dropna()`) keeps a late-listing ticker, truncates the window to
  whatever all tickers share, and leaves each series based outside the surviving window.
  This was the second real bug; `index_members` is now reported so the reader can see it.
- Hit rate scores directional signals only; neutral state labels are listed with no verdict.
  The caveat ships **inline** next to the number in both HTML and terminal output.
- Bilingual: everything through the `t(en, zh)` macro. Preformatted strings that vary
  (`today`/`本日`, the ticker counts) need `_zh` twins — a screenshot of the ZH view is the
  only reliable way to find these, and a leak test locks them in.
- Wire into the pipeline inside `try/except`: a second artifact must never cost the user
  their daily report.

## Phase 4 — measure, then optimise what the profile shows
Do this *after* the feature works, and do not skip it: the intuition was wrong twice here.

- Profile the pipeline **per stage**. The incremental fetch bought only 1.3x on its own —
  per-request latency dominates payload, so a 12-day fetch costs nearly what 2 years costs.
  The saving is *not requesting at all*: `is_current` skips the call when the cache already
  holds the newest bar that could exist (`last_expected_bar`, most recent weekday, holidays
  deliberately ignored). For the reference source, which lags a day and so is never
  calendar-current, skip only when within `REFERENCE_MAX_LAG_DAYS` of the primary **and**
  already synced today — and compare that "today" in **UTC**, matching SQLite's
  `datetime('now')`. Comparing it to a local `date.today()` is silently wrong by a day for
  part of every day outside UTC.
- The profile also showed the **backtest** was 35% of the run, for a reason unrelated to I/O:
  every grid cell rebuilt the same per-(pair, direction) window masks, in a per-bar Python
  loop. Memoize them on `RuleContext` (documenting its single-frame scope invariant) and
  vectorize the window test with `searchsorted` → 14x. Lock it in with a call-counting test,
  not a timing test: assert 8 distinct masks for a 19-cell sweep, so a refactor cannot
  silently undo it.

## Phase 5 — docs
README (What it does, Configuration table, two new sections, Layout), PROGRESS.md (steps,
gates, test count, decisions log), CONTEXT.md glossary, ADR-0006/0007/0008, this plan.
