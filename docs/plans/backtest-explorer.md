# Backtest explorer — implementation plan

Goal: make the existing backtest answer "how would this ticker have done if I
changed **when**, **at what time scale**, and **on which signals**?" — without
disturbing the daily report, which keeps its one fixed compact view.

Traces to `CONTEXT.md` (Signal rule, Strategy, Bar interval, MA ladder, Warm-up vs
trade window) and ADR-0005. Ordered so each layer is pure and independently
testable before anything user-facing exists.

## The three axes, and what each one actually costs

| Axis | Naive reading | What it really requires |
|------|---------------|-------------------------|
| window | slice the frame to `[start, end]` | slice the **trade** window only — MAs must warm on data *before* `start`, or the first months of every run are half-warm |
| time scale | pass `interval` to the fetcher | resample locally, and give each interval **its own MA ladder** — periods count in bars, so weekly `sma20` is five months |
| signals | add an `if` for each new signal | a **rule registry**: the engine must stop knowing what a signal is |

Axis 3 is the one that decides the design. If the engine keeps calling a
degree-cascade function directly, every new signal family edits the engine. So
the engine takes a *mask* and a rule produces one.

## Phase 1 — `rules.py` (pure)
Signal registry. A rule is `(frame, direction, ctx) -> per-bar bool mask` meaning
"the condition holds at this bar". The engine takes the **rising edge**, which is
what makes every family inherit the degree cascade's fire-once-on-completion
semantics for free.

- `RuleSpec(kind, params)` — frozen, hashable, with a round-trippable `label`
  (`parse_rule(spec.label) == spec` is a test).
- `RuleContext(pairs, ma_cols, window_days, slope_lookback, flat_threshold_pct)` —
  the ladder-derived facts a rule needs. `pairs` is what gives *degree* meaning,
  so a shorter ladder simply has fewer degrees; nothing special-cases that.
- Families: `degree` (the existing cascade, moved here), `cross`, `price`,
  `align`, `slope`. Each must be correct in **both** directions and must return
  False on unwarmed bars — an unwarmed MA is not a "price is below it" signal.
- `parse_rule` / `parse_rules`, with group tokens (`degrees`, `crosses`, `prices`,
  `slopes`, `all`) expanded against the active ladder. Reject malformed specs with
  a message naming the valid forms; check the *kind* before complaining about a
  missing argument, or `--entry bogus` says "bogus needs an argument".

## Phase 2 — `bars.py` (pure)
Time scale. `resample_bars(prices, interval)` buckets daily rows by ISO week /
calendar month: open=first, high=max, low=min, close/adj_close=last, volume=sum.

- A bar's date is the **last real trading date in its bucket**, not the period
  end — otherwise the newest bar is dated in the future and calendar-day windows
  (the degree lookback) silently shift.
- `DEFAULT_LADDERS` per interval, chosen to preserve the 月/季/半年/年線 meanings.
- `min_history_years(interval, ladder)` — turns the slowest rung into the calendar
  history a run needs, which the CLI uses for warnings and its `--years` default.

## Phase 3 — engine parameterization (`backtest.py`)
- `indicators.compute_indicators(prices, periods=None)` gains the ladder argument.
- `BacktestSpec` carries the axes; `spec.context()` derives the `RuleContext` so
  ladder → pairs → degrees is computed in exactly one place.
- `run_spec(prices, symbol, spec)`: resample → compute MAs on **all** loaded
  history → `_prepare` → clip. Order matters: clipping before computing is the
  bug this ordering exists to prevent.
- Window resolution: `ws = max(first_all_warm_bar, first bar >= start)`; a `start`
  earlier than the warm bar is *clamped with a note*, never silently honoured.
  `end` snaps to the last bar on or before it.
- Degenerate cases return a no-result carrying a **reason** (`history too short to
  warm N bars`, `window too short`, `start after the last bar`), and a window
  shorter than the slowest rung gets a "widen the window" hint — an annualized
  figure over 5 bars is noise and should say so.
- `StrategyResult` holds two `RuleSpec`s; `entry_degree`/`exit_degree` become
  properties so `report.build_backtest_view` needs no change.
- `run_backtest` stays as the report's entry point, expressed over the same
  internals.

## Phase 4 — `backtest_cli.py`
Third console script, `portfolio-monitor-backtest`. Flags map 1:1 onto the axes.

- Prices from the stored `prices` table by default (no network). `--refresh`
  fetches and upserts, with `--years` defaulting to requested span + warm-up +
  margin — a distant `--start` against 2 years of stored rows must not quietly
  produce a tiny window.
- Output: ranked grid table (`--sort`, `--top`) or `--json`; `--list-rules` for
  discovery.
- Every run prints the buy-and-hold row, the "N of M cells beat buy-and-hold"
  count, and the in-sample caveat. A 225-cell sweep makes ADR-0004's hindsight
  problem worse, so the honesty scales with the grid rather than staying a
  footnote.

## Phase 5 — docs
README (What it does + an "Exploring backtests" section + Layout), PROGRESS.md
(steps, gates, test count, decisions-log entry), CONTEXT.md glossary additions,
ADR-0005, and a scoped supersession note on ADR-0003 / this plan's predecessor.
