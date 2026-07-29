# The backtest explorer is a separate CLI; the report keeps its fixed embedded view

**Supersedes the "no CLI" clause of [ADR-0003](0003-backtest-embedded-ephemeral-in-daily-report.md).**
Everything else in ADR-0003 still holds: the daily report's backtest stays embedded,
ephemeral, and unpersisted.

Making the backtest *explorable* — choosable date window, choosable bar interval,
choosable entry/exit signals — needs an interactive, re-runnable surface. Three shapes
were available:

1. **Parameterize the daily report.** Rejected: the report answers "what happened
   today" for a fixed configuration. A knob-laden report is a worse report, the knobs
   would have to be set in `settings.yaml` (edit-file-then-rerun is a bad exploration
   loop), and a cron job has no user to turn them.
2. **A notebook.** Rejected: no notebook dependency in this project, and the logic
   would drift out of the tested package into an untested cell.
3. **A second console script, `portfolio-monitor-backtest`** (chosen). Flags map
   one-to-one onto the exploration axes, it reuses the same tested engine as the
   report, and the report is untouched.

So the backtest now has **two callers over one engine**:

| | daily report | explorer CLI |
|---|---|---|
| entry point | `backtest.run_backtest` | `backtest.run_spec` |
| window | all history on hand | `--start` / `--end` |
| bars | daily | `--interval daily\|weekly\|monthly` |
| signals | the 4x4 degree grid | any `--entry` x `--exit` rule product |
| indicators | the pipeline's frame | recomputed for the chosen interval + ladder |
| output | one compact bilingual block | ranked grid table or JSON |

## Consequences

- **Signals became a registry** (`rules.py`) instead of one hard-coded cascade. Adding
  a signal family means adding a rule function and its parser case; the engine, the
  grid sweep, and both callers pick it up for free. A rule returns a per-bar "condition
  holds" mask and the engine takes its **rising edge**, so every family inherits the
  fire-once-on-completion semantics the degree cascade already had.
- **MA periods count in bars, so each interval carries its own ladder** (`bars.py`).
  Weekly `sma20` is five months, not one, so reusing the daily ladder at a coarser
  interval would silently change what every line means. The slowest line also sets the
  warm-up: a weekly `104` needs ~2 years of data *before* the first tradable bar, which
  is why the CLI computes a `--years` default and warns when a window is barely warm.
- **Data window and trade window are now distinct.** MAs are computed over all loaded
  history and only then clipped to `--start`, so a chosen start trades on a warm ladder.
  A `--start` earlier than the first all-warm bar is clamped, and says so.
- The explorer reads the stored `prices` table by default (no network). Windows older
  than the daily pipeline's `history_years` need `--refresh`, which also upserts — so
  exploring deep history permanently widens the local cache. Accepted: the writes are
  idempotent and additive, and the daily report backtests the frame it just fetched,
  not the table.
- Hindsight selection (ADR-0004) gets *worse*, not better, with a bigger grid:
  `--entry all --exit all` is 225 cells. The CLI therefore prints the in-sample caveat
  and the "N of M cells beat buy-and-hold" count on every run rather than only
  reporting a winner.
