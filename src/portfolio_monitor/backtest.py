"""Signal backtest (feature: 回測).

Replays a ticker's own MA-derived signals over history and asks: would trading on
them have made money? See docs/adr/0001-0005 and CONTEXT.md for the model. In
brief:

* A **Strategy** is a pair of rules `(entry rule, exit rule)` drawn from the
  registry in `rules.py`. Entry rules are evaluated "up", exit rules "down", and
  each fires on the bar its condition first becomes true (the rising edge).
  The daily report's grid is the classic one: entry/exit degrees 1..4.
* A **degree-N cross** is the cumulative-from-fast cascade: the fastest N
  adjacent MA pairs have each crossed the same direction within a lookback
  window, in any order (ADR-0001). Up = breakout (buy), down = breakdown (sell).
* Trades are long-only, one position at a time, filled at the **next bar's
  split/dividend-adjusted open** (ADR-0002), compounded all-in/all-out, netted a
  per-side cost. An open position at the window end is marked to market.
* Every strategy and the buy-and-hold benchmark share one **common window** that
  starts at the first bar where the whole MA ladder is warm — optionally pushed
  later by an explicit `start`, and cut short by an explicit `end` (ADR-0005).

Two entry points:

* `run_backtest(prices, indicators, symbol, …)` — the daily report's path:
  daily bars, the report's indicator frame, the 4x4 degree grid.
* `run_spec(prices, symbol, spec)` — the explorer's path: a `BacktestSpec`
  choosing the date window, the bar interval, the MA ladder and which rules to
  sweep. Indicators are recomputed at the requested interval, so this needs only
  a daily price frame.

This module is pure: it takes price/indicator DataFrames and returns dataclasses.
No I/O, no DB, no rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import bars, indicators as indicators_mod, rules
from .rules import RuleContext, RuleSpec

# Degrees the daily report's grid sweeps (1=single … 4=quad). Fixed by the four
# adjacent MA pairs; higher degree = the cascade has propagated further along the
# stack. An explorer run with a shorter ladder gets correspondingly fewer.
DEGREES = (1, 2, 3, 4)


@dataclass
class StrategyResult:
    entry: RuleSpec
    exit: RuleSpec
    total_return: float          # fraction, e.g. 0.25 == +25%
    cagr: float                  # annualized fraction
    max_drawdown: float          # positive fraction, e.g. 0.30 == -30% peak-to-trough
    num_trades: int              # entries taken (realized round-trips + any open one)
    win_rate: float              # fraction of trades with positive P&L (open counted by MTM)
    has_open_trade: bool         # a position was still open at the window end (marked to market)

    # The report renders degree grids, so expose the degree numbers directly.
    # 0 means "this side isn't a degree rule" (e.g. a cross: or price: rule).
    @property
    def entry_degree(self) -> int:
        return int(self.entry.p.get("n", 0)) if self.entry.kind == "degree" else 0

    @property
    def exit_degree(self) -> int:
        return int(self.exit.p.get("n", 0)) if self.exit.kind == "degree" else 0

    @property
    def entry_label(self) -> str:
        return self.entry.label

    @property
    def exit_label(self) -> str:
        return self.exit.label


@dataclass(frozen=True)
class BacktestSpec:
    """What to backtest: the window, the time scale, and which signals to sweep.

    * `start`/`end` — ISO dates bounding the **trade** window. `start` can only
      push the window later than the first all-warm bar, never earlier: MAs need
      their warm-up, so bars before that are data, not tradable history.
    * `interval` — daily / weekly / monthly bars (see bars.py).
    * `ma_periods` — the MA ladder in bars; None takes the interval's default.
    * `ma_kind` — "sma" or "ema"; picks which family the rules read.
    * `entries`/`exits` — rule specs to sweep; the grid is their product. Empty
      means "all degrees available for this ladder".
    """
    start: str | None = None
    end: str | None = None
    interval: str = "daily"
    ma_periods: tuple[int, ...] | None = None
    ma_kind: str = "sma"
    entries: tuple[RuleSpec, ...] = ()
    exits: tuple[RuleSpec, ...] = ()
    window_days: int = 30
    cost_bps: float = 5.0
    starting_cash: float = 10000.0
    slope_lookback: int = 10
    flat_threshold_pct: float = 0.5

    @property
    def ladder(self) -> tuple[int, ...]:
        if self.ma_periods:
            return tuple(sorted({int(p) for p in self.ma_periods}))
        return bars.default_ladder(self.interval)

    def context(self) -> RuleContext:
        ladder = self.ladder
        return RuleContext(
            pairs=rules.adjacent_pairs(ladder, self.ma_kind),
            ma_cols=[f"{self.ma_kind}{p}" for p in ladder],
            window_days=self.window_days,
            slope_lookback=self.slope_lookback,
            flat_threshold_pct=self.flat_threshold_pct,
        )

    def resolved_entries(self, ctx: RuleContext) -> tuple[RuleSpec, ...]:
        return self.entries or _all_degrees(ctx)

    def resolved_exits(self, ctx: RuleContext) -> tuple[RuleSpec, ...]:
        return self.exits or _all_degrees(ctx)


def _all_degrees(ctx: RuleContext) -> tuple[RuleSpec, ...]:
    return tuple(RuleSpec.of("degree", n=n) for n in range(1, len(ctx.pairs) + 1))


@dataclass
class TickerBacktest:
    symbol: str
    window_start: str | None     # ISO date of the first tradable bar, or None if insufficient
    window_end: str | None
    best: StrategyResult | None  # highest CAGR among strategies with >=1 trade; None if none traded
    buy_hold_return: float | None
    buy_hold_cagr: float | None
    all_results: list[StrategyResult] = field(default_factory=list)
    # Explorer context — what was actually run, for honest reporting.
    interval: str = "daily"
    ma_periods: tuple[int, ...] = ()
    num_bars: int = 0            # tradable bars in the window
    data_start: str | None = None  # first bar of loaded history (warm-up included)
    note: str = ""               # human-readable caveat, e.g. clamped start


# --------------------------------------------------------------------------- #
# Price adjustment + window
# --------------------------------------------------------------------------- #
def _prepare(prices: pd.DataFrame, indicators: pd.DataFrame) -> pd.DataFrame:
    """Merge prices+indicators on date, sort ascending, and add adjusted OHLC.

    adj_open = open × (adj_close / close) — the split/dividend factor for the bar
    (ADR-0002). close is validated > 0 upstream, but guard defensively anyway.
    """
    df = prices.merge(indicators, on="date", how="inner").sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    adjc = df["adj_close"].astype(float)
    factor = np.where(close > 0, adjc / close, 1.0)
    df["adj_open"] = df["open"].astype(float).to_numpy() * factor
    df["adj_c"] = adjc.to_numpy()
    return df


def _warm_start(df: pd.DataFrame, ma_cols=("sma240",)) -> int | None:
    """First bar index where every MA in the ladder is warm. The slowest line
    decides, and it is the common start for every strategy and buy-and-hold."""
    present = [c for c in ma_cols if c in df.columns]
    if not present:
        return None
    mask = df[present].notna().all(axis=1).to_numpy()
    if not mask.any():
        return None
    return int(mask.argmax())


def _first_index_on_or_after(df: pd.DataFrame, date_str: str) -> int | None:
    """Index of the first bar dated on or after `date_str`; None if none is."""
    dates = pd.to_datetime(df["date"])
    mask = (dates >= pd.Timestamp(date_str)).to_numpy()
    return int(mask.argmax()) if mask.any() else None


# --------------------------------------------------------------------------- #
# Degree detection (kept as a named helper: it is the report's headline signal)
# --------------------------------------------------------------------------- #
def _degree_confirmed(df: pd.DataFrame, degree: int, direction: str,
                      window_days: int, ctx: RuleContext | None = None) -> np.ndarray:
    """Per-bar boolean: is a degree-N cross confirmed in `direction` at this bar?

    True when *every* one of the fastest-N adjacent pairs has a `direction` cross
    within the trailing window (any order). Defaults to the daily sma ladder.
    """
    if ctx is None:
        ctx = RuleContext(pairs=rules.adjacent_pairs(bars.DEFAULT_LADDERS["daily"]),
                          window_days=window_days)
    else:
        ctx = RuleContext(pairs=ctx.pairs, ma_cols=ctx.ma_cols, window_days=window_days,
                          slope_lookback=ctx.slope_lookback,
                          flat_threshold_pct=ctx.flat_threshold_pct)
    return rules.confirm(df, RuleSpec.of("degree", n=degree), direction, ctx)


# --------------------------------------------------------------------------- #
# Trade engine
# --------------------------------------------------------------------------- #
def _rising_edge(mask: np.ndarray) -> np.ndarray:
    """Bars where `mask` turns True. A condition *completes* on its rising edge,
    not on every bar it stays true — that is what "fires on the bar completing the
    Nth cross" (CONTEXT.md) means, and it avoids re-entering on a stale cross or
    churning in a whipsaw."""
    if len(mask) == 0:
        return mask
    return mask & ~np.concatenate(([False], mask[:-1]))


def _run_one(df: pd.DataFrame, entry: RuleSpec, exit_: RuleSpec, ctx: RuleContext,
             window_start: int, window_end: int, cost_bps: float,
             starting_cash: float) -> StrategyResult:
    """Backtest one (entry rule, exit rule) strategy over [window_start, window_end]."""
    n = window_end + 1
    entry_edge = _rising_edge(rules.confirm(df, entry, "up", ctx))
    exit_edge = _rising_edge(rules.confirm(df, exit_, "down", ctx))
    adj_open = df["adj_open"].to_numpy()
    adj_c = df["adj_c"].to_numpy()
    cost = cost_bps / 1e4

    # Pass 1: decide fills. A completion at bar i fills at bar i+1's open (ADR-0002).
    events: list[tuple[int, str, float]] = []   # (fill_bar, "enter"|"exit", price)
    in_position = False
    for i in range(window_start, n):
        if i + 1 >= n:            # no next bar inside the window to fill against
            break
        if not in_position and entry_edge[i]:
            events.append((i + 1, "enter", adj_open[i + 1] * (1 + cost)))
            in_position = True
        elif in_position and exit_edge[i]:
            events.append((i + 1, "exit", adj_open[i + 1] * (1 - cost)))
            in_position = False

    # Pass 2: build the daily mark-to-market equity curve and tally trades.
    cash = starting_cash
    shares = 0.0
    entry_price: float | None = None
    trade_returns: list[float] = []
    has_open = False
    equity = np.full(n, np.nan)
    ei = 0
    for i in range(window_start, n):
        while ei < len(events) and events[ei][0] == i:
            _, kind, price = events[ei]
            if kind == "enter":
                shares = cash / price
                cash = 0.0
                entry_price = price
            else:  # exit
                cash = shares * price
                trade_returns.append(price / entry_price - 1.0)
                shares = 0.0
                entry_price = None
            ei += 1
        equity[i] = cash + shares * adj_c[i]
    # Position still open at the end → mark to market for the open trade's return.
    if entry_price is not None:
        has_open = True
        trade_returns.append(adj_c[n - 1] / entry_price - 1.0)

    eq = equity[window_start:n]
    eq0, eqN = eq[0], eq[-1]
    total_return = eqN / eq0 - 1.0
    days = (pd.Timestamp(df["date"].iloc[n - 1]) - pd.Timestamp(df["date"].iloc[window_start])).days
    years = days / 365.25
    cagr = (eqN / eq0) ** (1.0 / years) - 1.0 if years > 0 and eqN > 0 else total_return
    running_peak = np.maximum.accumulate(eq)
    max_drawdown = float(np.max((running_peak - eq) / running_peak)) if len(eq) else 0.0

    num_trades = len(trade_returns)
    wins = sum(1 for r in trade_returns if r > 0)
    win_rate = wins / num_trades if num_trades else 0.0
    return StrategyResult(entry, exit_, float(total_return), float(cagr),
                          max_drawdown, num_trades, float(win_rate), has_open)


def _buy_hold(df: pd.DataFrame, window_start: int, window_end: int) -> tuple[float, float]:
    """Buy-and-hold over the same window: buy at the first tradable bar's adjusted
    close, hold to the last bar's adjusted close. No churn, so no trading cost."""
    adj_c = df["adj_c"].to_numpy()
    start_price, end_price = adj_c[window_start], adj_c[window_end]
    total = end_price / start_price - 1.0
    days = (pd.Timestamp(df["date"].iloc[window_end])
            - pd.Timestamp(df["date"].iloc[window_start])).days
    years = days / 365.25
    cagr = (end_price / start_price) ** (1.0 / years) - 1.0 if years > 0 else total
    return float(total), float(cagr)


def _pick_best(results: list[StrategyResult]) -> StrategyResult | None:
    """Highest CAGR among strategies that actually traded; tie-break by lower
    max-drawdown, then fewer trades (defaults A + D). None if nothing traded."""
    traded = [r for r in results if r.num_trades > 0]
    if not traded:
        return None
    return sorted(traded, key=lambda r: (-r.cagr, r.max_drawdown, r.num_trades))[0]


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def _empty(symbol: str, spec: BacktestSpec | None = None, note: str = "") -> TickerBacktest:
    return TickerBacktest(symbol, None, None, None, None, None, [],
                          interval=spec.interval if spec else "daily",
                          ma_periods=spec.ladder if spec else (), note=note)


def _grid(df: pd.DataFrame, symbol: str, spec: BacktestSpec, ctx: RuleContext,
          ws: int, we: int, note: str) -> TickerBacktest:
    """Sweep the entry×exit product over an already-prepared frame and window."""
    results = [
        _run_one(df, e, x, ctx, ws, we, spec.cost_bps, spec.starting_cash)
        for e in spec.resolved_entries(ctx) for x in spec.resolved_exits(ctx)
    ]
    bh_return, bh_cagr = _buy_hold(df, ws, we)
    return TickerBacktest(
        symbol=symbol,
        window_start=str(df["date"].iloc[ws]),
        window_end=str(df["date"].iloc[we]),
        best=_pick_best(results),
        buy_hold_return=bh_return,
        buy_hold_cagr=bh_cagr,
        all_results=results,
        interval=spec.interval,
        ma_periods=spec.ladder,
        num_bars=we - ws + 1,
        data_start=str(df["date"].iloc[0]) if len(df) else None,
        note=note,
    )


def run_backtest(prices: pd.DataFrame, indicators: pd.DataFrame,
                 symbol: str, window_days: int = 30, cost_bps: float = 5.0,
                 starting_cash: float = 10000.0) -> TickerBacktest:
    """Run the daily report's degree grid for one ticker and pick the best by CAGR.

    Daily bars, the caller's indicator frame, entry/exit degrees 1..4. Returns a
    TickerBacktest with best=None when the history is too short for any MA-warm
    bar or no strategy ever traded.
    """
    spec = BacktestSpec(interval="daily", window_days=window_days,
                        cost_bps=cost_bps, starting_cash=starting_cash)
    if prices.empty or indicators.empty:
        return _empty(symbol, spec)
    df = _prepare(prices, indicators)
    ctx = spec.context()
    ws = _warm_start(df, ctx.ma_cols)
    if ws is None or ws >= len(df) - 1:
        return _empty(symbol, spec)
    return _grid(df, symbol, spec, ctx, ws, len(df) - 1, "")


def run_spec(prices: pd.DataFrame, symbol: str, spec: BacktestSpec) -> TickerBacktest:
    """Run an explorer backtest: resample to `spec.interval`, recompute the MA
    ladder on those bars, clip to [spec.start, spec.end], and sweep the grid.

    MAs are always computed over the **full** loaded history and only then clipped,
    so a `start` inside the data trades with a properly warmed ladder rather than
    restarting the warm-up at the requested date.
    """
    if prices.empty:
        return _empty(symbol, spec, "no price history")

    bar_df = bars.resample_bars(prices, spec.interval)
    ladder = spec.ladder
    ind = indicators_mod.compute_indicators(bar_df[["date", "close"]], periods=ladder)
    df = _prepare(bar_df, ind)
    ctx = spec.context()

    missing = [c for c in ctx.ma_cols if c not in df.columns]
    if missing:
        return _empty(symbol, spec, f"missing indicator columns: {', '.join(missing)}")

    ws = _warm_start(df, ctx.ma_cols)
    if ws is None:
        need = bars.min_history_years(spec.interval, ladder)
        return _empty(symbol, spec,
                      f"history too short to warm {max(ladder)} {spec.interval} bars "
                      f"(~{need:.1f}y needed before the first tradable bar)")

    notes: list[str] = []
    if spec.start:
        want = _first_index_on_or_after(df, spec.start)
        if want is None:
            return _empty(symbol, spec, f"start {spec.start} is after the last bar")
        if want > ws:
            ws = want
        elif want < ws:
            notes.append(f"start clamped to {df['date'].iloc[ws]} (MA warm-up)")

    we = len(df) - 1
    if spec.end:
        dates = pd.to_datetime(df["date"])
        mask = (dates <= pd.Timestamp(spec.end)).to_numpy()
        if not mask.any():
            return _empty(symbol, spec, f"end {spec.end} is before the first bar")
        we = int(np.max(np.flatnonzero(mask)))

    if we <= ws:
        return _empty(symbol, spec,
                      f"window too short: only {max(0, we - ws + 1)} tradable "
                      f"{spec.interval} bar(s) after MA warm-up")

    # A window shorter than the slowest line means the ladder only just warmed up:
    # the signals have barely any room to fire and annualized figures are noise.
    tradable = we - ws + 1
    if tradable < max(ladder):
        need = bars.min_history_years(spec.interval, ladder)
        notes.append(f"only {tradable} tradable {spec.interval} bars after warm-up "
                     f"(the {max(ladder)}-bar line alone needs ~{need:.1f}y of data) — "
                     f"widen the window or --refresh --years")

    return _grid(df, symbol, spec, ctx, ws, we, "; ".join(notes))
