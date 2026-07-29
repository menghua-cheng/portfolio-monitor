# The tracker is equal-weight, and its hit rate is explicitly not a backtest

Two decisions in the performance/signal tracker look like shortcuts and are not.

## The portfolio is equal-weight, rebalanced per horizon

`config/portfolio.csv` is a **watchlist**: `symbol,name`, no share counts, no cost basis.
So there is no position-weighted return to compute. Rather than invent weights, the tracker
reports the equal-weight mean of the per-ticker returns for each horizon, and **prints how
many tickers each horizon averaged**.

That count is not decoration. A ticker added last month cannot answer "1 year", so it is
excluded from that horizon rather than assumed flat — and an average over a membership that
changes per column is misleading unless the membership is visible. Same rule for the
equal-weight index sparkline: only tickers covering the whole window take part, and the
constituent count is printed next to it.

Rejected: adding share counts to the watchlist. That turns a monitor into a
position tracker — a different, larger tool, and one that would need cost basis, currency
and corporate-action handling to be worth anything.

## The signal hit rate is a tracking measure, not a strategy result

Hit rate asks: did an up-signal get followed by a gain, and a down-signal by a fall,
measured on adjusted closes from the signal bar to **the latest bar**?

That last part is what makes it *not* a backtest. Every signal is measured to the same
right-hand edge, so a signal from 80 days ago gets an 80-day runway and one from yesterday
gets a day. There are no entries, no exits, no position sizing, no costs, and no notion of
being flat. It cannot be compounded or compared to buy-and-hold.

It is kept anyway because it answers a question the backtest does not: *has this ticker's
signal flow been pointing the right way lately?* The backtest answers "would trading this
rule have made money over a window", which is a different question and already has its own
command.

Rejected: measuring each signal over a fixed forward horizon (e.g. +20 days) to make the
runways comparable. More rigorous, but it makes the most recent signals — the ones the user
actually cares about today — unscoreable, which defeats the point of a daily tracker.
Rejected too: dropping hit rate entirely; a list of signals with no accountability is what
the daily report already shows.

## Consequences

- Signals whose type makes **no directional claim** are listed but never scored
  (`correct is None`), so the denominator is "directional signals", printed alongside the
  total. Scoring a neutral state label would be inventing an opinion.
- The mismatch between hit rate and the backtest is expected, not a bug. Both the HTML and
  the terminal output carry the caveat inline — it must be un-missable next to the number,
  not buried in docs.
- Nothing is persisted. Performance and hit rate are fully derivable from the cached
  `prices` and `signals` tables, so a `tracker_daily` table would only duplicate derivable
  data — the same reasoning as ADR-0003. The consequence is the same too: the tracker
  cannot answer "what did the hit rate look like last month" without re-adding persistence.
- The tracker runs at the end of the daily pipeline inside a `try/except`. It is a second
  artifact, and a failure there must never cost the user the daily report.
