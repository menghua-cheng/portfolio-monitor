# Portfolio Monitor

A personal US-stock monitor that fetches prices, computes moving-average
indicators, detects trend/cross signals, and delivers a daily bilingual report.

## Language

**Signal**:
A dated, per-ticker event emitted only on a *transition* — either a CROSS-family
event (short MA crossing a long MA) or a TREND-family state change. Signals
describe what is happening now; they are not trade instructions until a backtest
interprets them as such.

**Backtest**:
A historical evaluation that replays past prices and asks whether trading on a
ticker's own Signals would have made money. Per-ticker (not portfolio-wide) and
strategy-validation only — it is not a parameter optimizer or a live trading
simulator.
_Avoid_: Simulation, optimizer (those imply a different, larger scope).

**N-fold cross** (degree):
A *cumulative-from-fast* cascade: degree N means the fastest N adjacent MA pairs
have all crossed in the same direction within a lookback window. Ordered
outward from the shortest MAs — degree 1 = 周/月 (sma5×sma20), 2 = +月/季
(sma20×sma60), 3 = +季/半年 (sma60×sma120), 4 = +半年/年 (sma120×sma240, all
pairs). Upward = breakout, downward = breakdown. Higher degree = the trend has
propagated further along the MA stack, so a stronger/rarer signal. Note this is
*stricter* than the report's `summarize_trend`, which counts any distinct pairs
regardless of which.

**Multi-break** (雙重／三重／四重突破・跌破):
*Any* N distinct adjacent MA pairs crossing the same direction within a lookback
window, regardless of which pairs. Contrast **N-fold cross (degree)**, which
demands the fastest N pairs specifically, cumulative from the short end: every
degree-N cross is a multi-N cross, but not the reverse, so a Multi-break fires
more often and earlier. This is the counting the daily report's trend tags use.
_Avoid_: calling either one "N crosses" — the distinction is the whole point.

**Signal rule**:
One named, parameterized condition a Backtest can buy or sell on — an N-fold
cross, a single MA pair crossing, price crossing an MA, the MA stack flipping
alignment, or one MA's slope turning. A rule is always read *directionally*:
upward for entries, downward for exits, so one rule covers both sides. It fires
on the bar its condition becomes true and not again while it stays true.

**Strategy**:
A pair `(entry Signal rule, exit Signal rule)`: buy when the entry rule fires
upward, sell when the exit rule fires downward. The two are chosen
independently, so entry and exit strength need not match. A Backtest compares
strategies across the grid formed by the requested entry × exit rules — the
daily report's grid is the 4×4 of entry/exit degrees.

**Bar interval** (time scale):
The unit of one bar: daily, weekly, or monthly. Coarser bars are aggregated from
daily ones (open=first, high=max, low=min, close=last, volume=sum) and keep the
last real trading date in the bucket.

**MA ladder**:
The ordered set of MA periods a Backtest runs on, counted in **bars**, fast to
slow. Because the count is in bars, each Bar interval needs its own ladder to
keep the familiar 月/季/半年/年線 meanings — daily 5/20/60/120/240, weekly
4/13/26/52/104, monthly 3/6/12/24/60. Adjacent rungs of the ladder are the pairs
that give *degree* its meaning, so a shorter ladder has fewer degrees.

**Price cache**:
The local SQLite price tables, treated as the source of truth. A run reads them
and asks upstream only for bars newer than what they hold. Deepening the cache is
permanent; what a caller *reads* is a window over it, which is why the daily
report can stay on two years while the explorer sees fourteen.

**Re-basing** (split / adjustment):
Upstream silently rewriting historical prices — a split divides every past raw
close, a dividend shifts every past adjusted close. The Price cache detects it by
re-fetching an **overlap** of bars it already holds and comparing them, then
rescales the stored history so the series keeps one price basis. Without this a
split appears as a real one-day crash.
_Avoid_: "correction" (that means a small upstream revision, which must NOT
trigger a rescale).

**Tracker**:
The daily performance-and-signal artifact: horizon returns per ticker and for the
equal-weight portfolio, plus every recorded Signal with what price did after it.
Distinct from a Backtest — it has no entries, exits, costs or positions.

**Hit rate**:
The fraction of *directional* Signals whose claim matched the subsequent adjusted
price move, measured from the signal bar to the latest bar. A Tracker measure, not
a strategy result: every Signal is measured to the same right-hand edge, so older
ones get a longer runway.
_Avoid_: win rate (that is the Backtest's per-Round-trip measure — different thing).

**Equal-weight portfolio**:
How the Tracker aggregates the watchlist: the mean of the per-ticker returns for a
horizon, over the tickers that have data at both ends, with that count reported.
The watchlist carries no share counts, so there is no position-weighted return to
compute.

**Warm-up** vs **trade window**:
The Warm-up is the leading stretch of history consumed before the slowest MA in
the ladder has a value; the trade window is what remains and is the only part a
Backtest trades or benchmarks. A requested start date can only push the trade
window later, never into the Warm-up.

**Round-trip trade**:
One entry paired with its matching exit for a single ticker under a single
Strategy. The unit a Backtest scores. Long-only, one open position at a time:
while a position is open, further entry signals are ignored until the exit fires.

**Fill**:
The price at which a Round-trip's entry or exit executes: the *next* bar's open
after the signal bar, adjusted for splits/dividends (open × adj_close ÷ close).
Filling on the next bar (not the signal bar) is what keeps the Backtest free of
lookahead bias.

**Buy-and-hold**:
The benchmark a Backtest is judged against: buying at the first bar of the common
(all-MAs-warm) window and holding to the last, on the same adjusted-price basis.
"Did my signals make money?" is only meaningful relative to this.
_Avoid_: Baseline, passive (reserve "Buy-and-hold" as the one name).
