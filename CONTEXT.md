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

**Strategy**:
A pair `(entry degree N, exit degree M)`: buy when an N-fold *upward* cross is
confirmed, sell when an M-fold *downward* cross is confirmed. N and M are chosen
independently, so entry and exit strength need not match. A Backtest compares
strategies across the 4×4 grid of degrees.

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
