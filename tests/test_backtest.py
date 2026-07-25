"""Backtest engine tests.

Synthetic frames give us deterministic MA crossings and fills. Prices carry
open/close/adj_close so the adjusted-open fill (ADR-0002) is exercised. Slow MA
pairs are held constant so only the pair(s) under test can cross.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import backtest  # noqa: E402
from portfolio_monitor.backtest import StrategyResult  # noqa: E402


def _mk(sma5, sma20, sma60, sma120, sma240, open_=None, close=None, adj_close=None):
    n = len(sma5)
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    open_ = list(open_ if open_ is not None else [100.0] * n)
    close = list(close if close is not None else [100.0] * n)
    adj_close = list(adj_close if adj_close is not None else close)
    prices = pd.DataFrame({"date": dates, "open": open_, "high": open_, "low": open_,
                           "close": close, "adj_close": adj_close,
                           "volume": [0] * n, "source": ["t"] * n})
    ind = pd.DataFrame({"date": dates, "sma5": sma5, "sma20": sma20, "sma60": sma60,
                        "sma120": sma120, "sma240": sma240})
    return prices, ind


def _res(bt, n, m):
    return next(r for r in bt.all_results if r.entry_degree == n and r.exit_degree == m)


# --- degree detection -------------------------------------------------------
def test_degree1_confirmed_from_cross_bar():
    _, ind = _mk(sma5=[8, 8, 12, 12], sma20=[10] * 4, sma60=[55] * 4,
                 sma120=[60] * 4, sma240=[50] * 4)
    conf = backtest._degree_confirmed(ind, 1, "up", 30)
    assert list(conf) == [False, False, True, True]


def test_degree2_needs_both_pairs():
    # sma5×sma20 up at bar2; sma20×sma60 up at bar4. Degree-2 completes at bar4.
    _, ind = _mk(sma5=[8, 8, 12, 12, 45, 45, 45, 45],
                 sma20=[10, 10, 10, 10, 40, 40, 40, 40],
                 sma60=[30] * 8, sma120=[100] * 8, sma240=[50] * 8)
    conf2 = backtest._degree_confirmed(ind, 2, "up", 30)
    assert conf2[2] == False and conf2[3] == False   # only one pair crossed yet
    assert conf2[4] == True                          # both now within window
    conf1 = backtest._degree_confirmed(ind, 1, "up", 30)
    assert conf1[2] == True                          # degree-1 fires earlier


def test_degree_window_expiry():
    # Two pairs cross far apart: bar1 and bar6 (5 days) — a 3-day window never
    # sees both at once, so degree-2 is never confirmed.
    _, ind = _mk(sma5=[8, 13, 13, 13, 13, 13, 13],
                 sma20=[10, 10, 10, 10, 10, 10, 10],
                 sma60=[30, 30, 30, 30, 30, 30, 30],
                 sma120=[100] * 7, sma240=[50] * 7)
    # only sma5×sma20 ever crosses here, so degree-2 (needs sma20×sma60) is dead
    conf2 = backtest._degree_confirmed(ind, 2, "up", 3)
    assert not conf2.any()


# --- price adjustment -------------------------------------------------------
def test_adjusted_open_formula():
    prices, ind = _mk(sma5=[1, 1], sma20=[1, 1], sma60=[1, 1], sma120=[1, 1],
                      sma240=[1, 1], open_=[100, 100], close=[200, 200],
                      adj_close=[100, 100])
    df = backtest._prepare(prices, ind)
    # adj_open = open × adj_close / close = 100 × 100/200 = 50
    assert df["adj_open"].iloc[0] == 50.0


# --- full round trip --------------------------------------------------------
def test_single_round_trip_return_net_of_cost():
    # up-cross bar2 -> buy open[3]=100; down-cross bar5 -> sell open[6]=110.
    prices, ind = _mk(sma5=[8, 8, 12, 12, 12, 7, 7, 7], sma20=[10] * 8,
                      sma60=[55] * 8, sma120=[60] * 8, sma240=[50] * 8,
                      open_=[100, 100, 100, 100, 100, 100, 110, 100])
    bt = backtest.run_backtest(prices, ind, "TST")
    r = _res(bt, 1, 1)
    assert r.num_trades == 1          # regression: NOT re-entered on the stale up-cross
    assert not r.has_open_trade
    assert r.win_rate == 1.0
    # 110×(1-5bps) / (100×(1+5bps)) - 1
    expected = (110 * (1 - 5e-4)) / (100 * (1 + 5e-4)) - 1
    assert abs(r.total_return - expected) < 1e-6


def test_open_position_marked_to_market():
    # degree-2 entry at bar4 -> buy open[5]; no down-cross -> still open at end.
    prices, ind = _mk(sma5=[8, 8, 12, 12, 45, 45, 45, 45],
                      sma20=[10, 10, 10, 10, 40, 40, 40, 40],
                      sma60=[30] * 8, sma120=[100] * 8, sma240=[50] * 8,
                      open_=[100] * 8, close=[100] * 8,
                      adj_close=[100, 100, 100, 100, 100, 100, 100, 120])
    bt = backtest.run_backtest(prices, ind, "TST")
    r = _res(bt, 2, 1)
    assert r.num_trades == 1 and r.has_open_trade
    expected = 120 / (100 * (1 + 5e-4)) - 1     # MTM at final adj_close vs entry fill
    assert abs(r.total_return - expected) < 1e-6


# --- buy-and-hold -----------------------------------------------------------
def test_buy_hold_matches_adjusted_ratio():
    prices, ind = _mk(sma5=[1] * 5, sma20=[1] * 5, sma60=[1] * 5, sma120=[1] * 5,
                      sma240=[50] * 5, adj_close=[100, 105, 110, 115, 125])
    bt = backtest.run_backtest(prices, ind, "TST")
    assert abs(bt.buy_hold_return - (125 / 100 - 1)) < 1e-9


# --- best selection ---------------------------------------------------------
def test_pick_best_excludes_zero_trade_and_breaks_ties():
    a = StrategyResult(1, 1, 0.5, 0.20, 0.40, 5, 0.6, False)   # higher DD
    b = StrategyResult(2, 2, 0.5, 0.20, 0.10, 3, 0.7, False)   # same CAGR, lower DD
    z = StrategyResult(3, 3, 9.9, 9.9, 0.0, 0, 0.0, False)     # never traded
    best = backtest._pick_best([a, b, z])
    assert best is b


def test_no_trades_gives_none_best():
    # constant, monotonic MAs -> nothing ever crosses.
    prices, ind = _mk(sma5=[5] * 4, sma20=[10] * 4, sma60=[20] * 4,
                      sma120=[30] * 4, sma240=[40] * 4)
    bt = backtest.run_backtest(prices, ind, "TST")
    assert bt.best is None
    assert all(r.num_trades == 0 for r in bt.all_results)


# --- insufficient history ---------------------------------------------------
def test_insufficient_history_when_sma240_all_nan():
    prices, ind = _mk(sma5=[8, 12], sma20=[10, 10], sma60=[55, 55],
                      sma120=[60, 60], sma240=[None, None])
    bt = backtest.run_backtest(prices, ind, "TST")
    assert bt.best is None and bt.window_start is None
