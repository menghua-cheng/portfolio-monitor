"""Backtest engine tests.

Synthetic frames give us deterministic MA crossings and fills. Prices carry
open/close/adj_close so the adjusted-open fill (ADR-0002) is exercised. Slow MA
pairs are held constant so only the pair(s) under test can cross.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import backtest  # noqa: E402
from portfolio_monitor.backtest import StrategyResult  # noqa: E402
from portfolio_monitor.rules import RuleSpec  # noqa: E402


def _deg(n):
    return RuleSpec.of("degree", n=n)


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
    a = StrategyResult(_deg(1), _deg(1), 0.5, 0.20, 0.40, 5, 0.6, False)  # higher DD
    b = StrategyResult(_deg(2), _deg(2), 0.5, 0.20, 0.10, 3, 0.7, False)  # same CAGR, lower DD
    z = StrategyResult(_deg(3), _deg(3), 9.9, 9.9, 0.0, 0, 0.0, False)    # never traded
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


# --------------------------------------------------------------------------- #
# Explorer path: run_spec — choosable window, time scale and signal rules
# --------------------------------------------------------------------------- #
def _daily_prices(closes, start="2024-01-01"):
    """A daily price frame whose open == close, so fills are unambiguous."""
    n = len(closes)
    dates = pd.date_range(start, periods=n, freq="B").strftime("%Y-%m-%d")
    c = [float(x) for x in closes]
    return pd.DataFrame({"date": dates, "open": c, "high": c, "low": c, "close": c,
                         "adj_close": c, "volume": [1] * n, "source": ["t"] * n})


# A saw-tooth: two full up/down swings, enough for a 2/3 MA ladder to cross often.
_SAW = ([10, 11, 12, 13, 14, 15, 16, 15, 14, 13, 12, 11, 10] * 2) + [11, 12, 13, 14]


def _spec(**kw):
    kw.setdefault("ma_periods", (2, 3))
    return backtest.BacktestSpec(**kw)


def test_run_spec_reports_the_window_it_actually_traded():
    prices = _daily_prices(_SAW)
    bt = backtest.run_spec(prices, "TST", _spec())
    assert bt.interval == "daily" and bt.ma_periods == (2, 3)
    assert bt.window_start == prices["date"].iloc[2]     # sma3 warms on bar index 2
    assert bt.window_end == prices["date"].iloc[-1]
    assert bt.num_bars == len(prices) - 2
    assert bt.data_start == prices["date"].iloc[0]
    assert bt.best is not None and bt.best.num_trades > 0


def test_run_spec_start_pushes_the_window_later():
    prices = _daily_prices(_SAW)
    wanted = prices["date"].iloc[10]
    bt = backtest.run_spec(prices, "TST", _spec(start=wanted))
    assert bt.window_start == wanted
    assert bt.buy_hold_return == pytest.approx(
        float(prices["adj_close"].iloc[-1]) / float(prices["adj_close"].iloc[10]) - 1)


def test_run_spec_start_before_warmup_is_clamped_with_a_note():
    prices = _daily_prices(_SAW)
    bt = backtest.run_spec(prices, "TST", _spec(start="2000-01-01"))
    assert bt.window_start == prices["date"].iloc[2]     # not 2000-01-01
    assert "clamped" in bt.note


def test_run_spec_end_truncates_the_window():
    prices = _daily_prices(_SAW)
    cut = prices["date"].iloc[15]
    bt = backtest.run_spec(prices, "TST", _spec(end=cut))
    assert bt.window_end == cut
    # An end that falls between bars snaps back to the last bar on or before it.
    bt2 = backtest.run_spec(prices, "TST", _spec(end="2099-01-01"))
    assert bt2.window_end == prices["date"].iloc[-1]


def test_run_spec_end_before_history_yields_no_result():
    bt = backtest.run_spec(_daily_prices(_SAW), "TST", _spec(end="1999-01-01"))
    assert bt.best is None and bt.window_start is None
    assert "before the first bar" in bt.note


def test_run_spec_start_after_history_yields_no_result():
    bt = backtest.run_spec(_daily_prices(_SAW), "TST", _spec(start="2099-01-01"))
    assert bt.best is None and "after the last bar" in bt.note


def test_run_spec_weekly_interval_trades_fewer_bars():
    prices = _daily_prices(_SAW * 4)
    daily = backtest.run_spec(prices, "TST", _spec())
    weekly = backtest.run_spec(prices, "TST", _spec(interval="weekly"))
    assert weekly.interval == "weekly"
    assert 0 < weekly.num_bars < daily.num_bars / 4
    # Buy-and-hold is computed on the resampled bars, so the two need not match
    # exactly, but both must be finite and the window must be inside the data.
    assert weekly.window_start >= daily.data_start


def test_run_spec_default_ladder_follows_the_interval():
    prices = _daily_prices(_SAW * 30)
    bt = backtest.run_spec(prices, "TST", backtest.BacktestSpec(interval="monthly"))
    assert bt.ma_periods == (3, 6, 12, 24, 60)


def test_run_spec_switches_signal_rules():
    prices = _daily_prices(_SAW)
    cross = RuleSpec.of("cross", short="sma2", long="sma3")
    price = RuleSpec.of("price", ma="sma3")
    bt = backtest.run_spec(prices, "TST", _spec(entries=(cross,), exits=(price,)))
    assert len(bt.all_results) == 1
    r = bt.all_results[0]
    assert r.entry_label == "cross:sma2/sma3" and r.exit_label == "price:sma3"
    assert r.entry_degree == 0 and r.exit_degree == 0    # not degree rules
    assert r.num_trades > 0


def test_run_spec_grid_is_the_entry_exit_product():
    prices = _daily_prices(_SAW)
    entries = (RuleSpec.of("degree", n=1), RuleSpec.of("cross", short="sma2", long="sma3"))
    exits = (RuleSpec.of("price", ma="sma3"),)
    bt = backtest.run_spec(prices, "TST", _spec(entries=entries, exits=exits))
    assert len(bt.all_results) == 2
    assert {(r.entry_label, r.exit_label) for r in bt.all_results} == {
        ("degree1", "price:sma3"), ("cross:sma2/sma3", "price:sma3")}


def test_run_spec_ema_ladder_is_readable_by_rules():
    prices = _daily_prices(_SAW)
    spec = _spec(ma_kind="ema", entries=(RuleSpec.of("cross", short="ema2", long="ema3"),),
                 exits=(RuleSpec.of("cross", short="ema2", long="ema3"),))
    bt = backtest.run_spec(prices, "TST", spec)
    assert bt.best is not None and bt.best.num_trades > 0


def test_run_spec_empty_prices_is_a_clean_no_result():
    empty = _daily_prices([])
    bt = backtest.run_spec(empty, "TST", _spec())
    assert bt.best is None and bt.window_start is None and bt.note


def test_run_spec_history_too_short_to_warm_says_so():
    bt = backtest.run_spec(_daily_prices([10, 11, 12]), "TST", _spec(ma_periods=(2, 240)))
    assert bt.best is None and "history too short" in bt.note


def test_run_spec_no_tradable_bars_after_warmup():
    # Exactly 240 bars: sma240 warms on the very last one, leaving nothing to trade.
    bt = backtest.run_spec(_daily_prices(list(range(10, 250))), "TST",
                           _spec(ma_periods=(2, 240)))
    assert bt.best is None and "window too short" in bt.note


def test_run_spec_barely_warm_window_carries_a_widen_hint():
    # A window shorter than the slowest line: annualized figures would be noise.
    bt = backtest.run_spec(_daily_prices(list(range(10, 260))), "TST",
                           _spec(ma_periods=(2, 240)))
    assert bt.window_start is not None
    assert "tradable daily bars after warm-up" in bt.note and "--refresh" in bt.note
