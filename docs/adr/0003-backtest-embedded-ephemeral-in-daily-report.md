# Backtest is embedded in the daily report and computed ephemerally

The backtest runs as a **section of the existing daily report**, recomputed on every
pipeline run from the price/indicator DataFrames already built in
`pipeline._process_ticker`, and is **not persisted** to the database. The obvious
alternative — a standalone `python -m portfolio_monitor.backtest` command emitting its own
HTML — was rejected in favor of a single daily artifact the user already reads. Recompute
cost is negligible at this scale (a handful of tickers, ~500 bars, 16 strategies = tens of
milliseconds), and results are fully reproducible from the stored `prices`, so a
`backtest_results` table would only duplicate derivable data. The consequence a future
reader must understand: there is intentionally no backtest CLI and no backtest table — do
not add one expecting it was an oversight.

Because it lives in a "what happened today" report, only a **compact slice** is shown per
ticker (the single best strategy by CAGR vs buy-and-hold), never the full 16×6 grid.

## Consequences

- The backtest cannot answer "how did the best strategy shift over time" without re-adding
  persistence — an accepted limitation.
- The section inherits the report's bilingual (EN/中文) rendering obligation.
