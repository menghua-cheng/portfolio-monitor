"""Signal backtest (feature: 回測).

Replays a ticker's own MA-cross signals over history and asks: would trading on
them have made money? See docs/adr/0001-0004 and CONTEXT.md for the model. In
brief:

* A **Strategy** is a pair `(entry degree N, exit degree M)`, N,M in 1..4.
* A **degree-N cross** is the cumulative-from-fast cascade: the fastest N
  adjacent MA pairs have each crossed the same direction within a lookback
  window, in any order (ADR-0001). Up = breakout (buy), down = breakdown (sell).
* Trades are long-only, one position at a time, filled at the **next bar's
  split/dividend-adjusted open** (ADR-0002), compounded all-in/all-out, netted a
  per-side cost. An open position at the window end is marked to market.
* Every strategy and the buy-and-hold benchmark share one **common window** that
  starts at the first bar where all MAs (incl. sma240) are warm.

This module is pure: it takes price/indicator DataFrames and returns dataclasses.
No I/O, no DB, no rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .signals import _ADJ_PAIRS, _pair_cross_events

# Degrees the backtest sweeps (1=single … 4=quad). Fixed by the four adjacent
# MA pairs; higher degree = the cascade has propagated further along the stack.
DEGREES = (1, 2, 3, 4)


@dataclass
class StrategyResult:
    entry_degree: int
    exit_degree: int
    total_return: float          # fraction, e.g. 0.25 == +25%
    cagr: float                  # annualized fraction
    max_drawdown: float          # positive fraction, e.g. 0.30 == -30% peak-to-trough
    num_trades: int              # entries taken (realized round-trips + any open one)
    win_rate: float              # fraction of trades with positive P&L (open counted by MTM)
    has_open_trade: bool         # a position was still open at the window end (marked to market)


@dataclass
class TickerBacktest:
    symbol: str
    window_start: str | None     # ISO date of the first all-warm bar, or None if insufficient
    window_end: str | None
    best: StrategyResult | None  # highest CAGR among strategies with >=1 trade; None if none traded
    buy_hold_return: float | None
    buy_hold_cagr: float | None
    all_results: list[StrategyResult] = field(default_factory=list)


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


def _window_start(df: pd.DataFrame) -> int | None:
    """First bar index where all MAs are warm. sma240 is the slowest, so its
    first non-null bar is the common start for every strategy and buy-and-hold."""
    if "sma240" not in df.columns:
        return None
    mask = df["sma240"].notna().to_numpy()
    if not mask.any():
        return None
    return int(mask.argmax())


# --------------------------------------------------------------------------- #
# Degree detection
# --------------------------------------------------------------------------- #
def _pair_in_window(cross_dates: list, bar_dates: np.ndarray, window_days: int) -> np.ndarray:
    """Per-bar boolean: did a cross of this pair land in the trailing window
    (0..window_days calendar days on or before the bar)?"""
    out = np.zeros(len(bar_dates), dtype=bool)
    if not cross_dates:
        return out
    cd = np.array([pd.Timestamp(d).value for d in cross_dates], dtype="int64")
    ns_per_day = 86_400_000_000_000
    for i, d in enumerate(bar_dates):
        diff_days = (pd.Timestamp(d).value - cd) / ns_per_day
        out[i] = bool(np.any((diff_days >= 0) & (diff_days <= window_days)))
    return out


def _degree_confirmed(df: pd.DataFrame, degree: int, direction: str,
                      window_days: int) -> np.ndarray:
    """Per-bar boolean: is a degree-N cross confirmed in `direction` at this bar?

    True when *every* one of the fastest-N adjacent pairs has a `direction` cross
    within the trailing window (any order). ANDs the per-pair window masks.
    """
    bar_dates = pd.to_datetime(df["date"]).to_numpy()
    confirmed = np.ones(len(df), dtype=bool)
    for short, long in _ADJ_PAIRS[:degree]:
        cross_dates = [edate for _, edate, d in _pair_cross_events(df, short, long)
                       if d == direction]
        confirmed &= _pair_in_window(cross_dates, bar_dates, window_days)
    return confirmed


# --------------------------------------------------------------------------- #
# Trade engine
# --------------------------------------------------------------------------- #
def _run_one(df: pd.DataFrame, entry_degree: int, exit_degree: int,
             window_start: int, window_days: int, cost_bps: float,
             starting_cash: float) -> StrategyResult:
    """Backtest one (entry N, exit M) strategy over [window_start, last bar]."""
    n = len(df)
    entry_conf = _degree_confirmed(df, entry_degree, "up", window_days)
    exit_conf = _degree_confirmed(df, exit_degree, "down", window_days)
    # A degree-N cross *completes* (the count first reaches N) on the rising edge
    # of the confirmation, not on every bar it stays within the window. Firing on
    # the edge is what "fires on the bar completing the Nth cross" (CONTEXT.md)
    # means, and it avoids re-entering on a stale cross or churning in a whipsaw.
    entry_edge = entry_conf & ~np.concatenate(([False], entry_conf[:-1]))
    exit_edge = exit_conf & ~np.concatenate(([False], exit_conf[:-1]))
    adj_open = df["adj_open"].to_numpy()
    adj_c = df["adj_c"].to_numpy()
    cost = cost_bps / 1e4

    # Pass 1: decide fills. A completion at bar i fills at bar i+1's open (ADR-0002).
    events: list[tuple[int, str, float]] = []   # (fill_bar, "enter"|"exit", price)
    in_position = False
    for i in range(window_start, n):
        if i + 1 >= n:            # no next bar to fill against
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

    eq = equity[window_start:]
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
    return StrategyResult(entry_degree, exit_degree, float(total_return), float(cagr),
                          max_drawdown, num_trades, float(win_rate), has_open)


def _buy_hold(df: pd.DataFrame, window_start: int) -> tuple[float, float]:
    """Buy-and-hold over the same window: buy at the first all-warm bar's adjusted
    close, hold to the last bar's adjusted close. No churn, so no trading cost."""
    adj_c = df["adj_c"].to_numpy()
    start_price, end_price = adj_c[window_start], adj_c[len(df) - 1]
    total = end_price / start_price - 1.0
    days = (pd.Timestamp(df["date"].iloc[len(df) - 1])
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
# Public entry point
# --------------------------------------------------------------------------- #
def run_backtest(prices: pd.DataFrame, indicators: pd.DataFrame,
                 symbol: str, window_days: int = 30, cost_bps: float = 5.0,
                 starting_cash: float = 10000.0) -> TickerBacktest:
    """Run the full 4×4 strategy grid for one ticker and pick the best by CAGR.

    Returns a TickerBacktest with best=None when the history is too short for any
    MA-warm bar or no strategy ever traded.
    """
    if prices.empty or indicators.empty:
        return TickerBacktest(symbol, None, None, None, None, None, [])
    df = _prepare(prices, indicators)
    ws = _window_start(df)
    if ws is None or ws >= len(df) - 1:
        return TickerBacktest(symbol, None, None, None, None, None, [])

    results = [
        _run_one(df, n, m, ws, window_days, cost_bps, starting_cash)
        for n in DEGREES for m in DEGREES
    ]
    bh_return, bh_cagr = _buy_hold(df, ws)
    return TickerBacktest(
        symbol=symbol,
        window_start=str(df["date"].iloc[ws]),
        window_end=str(df["date"].iloc[len(df) - 1]),
        best=_pick_best(results),
        buy_hold_return=bh_return,
        buy_hold_cagr=bh_cagr,
        all_results=results,
    )
