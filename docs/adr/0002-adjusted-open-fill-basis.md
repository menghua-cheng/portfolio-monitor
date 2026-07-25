# Backtest fills at the split/dividend-adjusted next-bar open

Trades fill at the **next bar's open**, adjusted for splits and dividends as
`open × (adj_close / close)` for that bar. Filling on the *next* bar (not the signal
bar) avoids lookahead bias — the Nth cross is only known at the signal bar's close, so we
cannot also transact at that close. Adjusting the open is necessary because the `prices`
table stores a *raw* open but a split/dividend-*adjusted* `adj_close`; mixing the two
across a multi-year hold silently corrupts returns (a single split reads as a ~50% loss).
Since no adjusted open is stored, we derive it with the same day's close→adj_close ratio.

## Considered options

- **Signal-bar close**: simplest, close-only, but optimistic (transacts at the exact
  close that defined the signal).
- **Next-bar open, raw prices**: realistic timing but breaks on any split during the hold
  and ignores dividends.
- **adj_close throughout**: fully consistent and adjusted, but gives up the next-open
  realism.
- **Adjusted next-bar open** (chosen): lookahead-safe *and* split/dividend-consistent, at
  the cost of one derived column.
