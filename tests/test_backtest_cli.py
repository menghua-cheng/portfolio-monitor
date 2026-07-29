"""Backtest explorer CLI tests.

The engine is tested in test_backtest.py; here we check the layer the user
actually touches: flag -> BacktestSpec translation, the history estimate that
`--refresh` uses, row loading, and that rendering stays honest about hindsight
selection. No network, no DB.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import backtest_cli, config  # noqa: E402
from portfolio_monitor.backtest import StrategyResult, TickerBacktest  # noqa: E402


def _cfg():
    return config.Config(tickers=[], settings={}, email=config.EmailConfig(
        host="", port=0, user="", app_password="", recipient=""))


def _args(argv):
    return backtest_cli._build_parser().parse_args(argv)


def _spec(argv):
    return backtest_cli._spec_from_args(_args(argv), _cfg())


# --- flag -> spec -----------------------------------------------------------
def test_defaults_are_daily_degrees_over_the_daily_ladder():
    spec = _spec([])
    assert spec.interval == "daily" and spec.ladder == (5, 20, 60, 120, 240)
    assert [s.label for s in spec.entries] == ["degree1", "degree2", "degree3", "degree4"]
    assert spec.entries == spec.exits
    assert spec.start is None and spec.end is None


def test_window_flags_reach_the_spec():
    spec = _spec(["--start", "2020-01-01", "--end", "2021-06-30"])
    assert spec.start == "2020-01-01" and spec.end == "2021-06-30"


def test_interval_switch_brings_its_own_ladder_and_degree_count():
    spec = _spec(["--interval", "monthly"])
    assert spec.interval == "monthly" and spec.ladder == (3, 6, 12, 24, 60)
    assert len(spec.entries) == 4          # 5 MAs -> 4 adjacent pairs -> 4 degrees


def test_explicit_ladder_overrides_the_interval_default():
    spec = _spec(["--interval", "weekly", "--ma-periods", "10, 50,200"])
    assert spec.ladder == (10, 50, 200)
    assert [s.label for s in spec.entries] == ["degree1", "degree2"]


def test_rule_switching_and_group_expansion():
    spec = _spec(["--entry", "cross:20/60,align", "--exit", "prices"])
    assert [s.label for s in spec.entries] == ["cross:sma20/sma60", "align"]
    assert [s.label for s in spec.exits] == [
        "price:sma5", "price:sma20", "price:sma60", "price:sma120", "price:sma240"]


def test_ema_kind_expands_groups_over_ema_columns():
    spec = _spec(["--ma-kind", "ema", "--entry", "crosses"])
    assert spec.entries[0].label == "cross:ema5/ema20"


def test_bad_rule_is_a_value_error_the_cli_can_report():
    with pytest.raises(ValueError):
        _spec(["--entry", "nonsense"])


def test_bad_interval_is_a_value_error():
    with pytest.raises(ValueError):
        _spec(["--interval", "hourly"])


def test_cost_and_window_flags_override_settings():
    spec = _spec(["--cost-bps", "25", "--window-days", "7"])
    assert spec.cost_bps == 25.0 and spec.window_days == 7


# --- history estimate -------------------------------------------------------
def test_needed_years_covers_span_plus_warmup():
    # Daily sma240 warms in ~1y; a 2020 start is ~6.5y back as of 2026.
    got = backtest_cli._needed_years("2020-01-01", "daily", (5, 240))
    assert got >= 8


def test_needed_years_has_a_floor_of_two():
    assert backtest_cli._needed_years(None, "daily", (5, 20)) == 2


def test_needed_years_scales_with_a_coarse_ladder():
    weekly = backtest_cli._needed_years(None, "weekly", (4, 104))
    assert weekly >= 3           # 104 weekly bars alone is ~2y of warm-up


# --- row loading ------------------------------------------------------------
def test_rows_to_frame_sorts_and_keeps_the_engine_columns():
    rows = [{"ticker": "T", "date": "2024-01-02", "open": 2, "high": 2, "low": 2,
             "close": 2, "adj_close": 2, "volume": 1, "source": "db"},
            {"ticker": "T", "date": "2024-01-01", "open": 1, "high": 1, "low": 1,
             "close": 1, "adj_close": 1, "volume": 1, "source": "db"}]
    df = backtest_cli._rows_to_frame(rows)
    assert list(df["date"]) == ["2024-01-01", "2024-01-02"]
    assert list(df.columns) == backtest_cli._PRICE_COLS


def test_rows_to_frame_of_nothing_is_an_empty_typed_frame():
    df = backtest_cli._rows_to_frame([])
    assert df.empty and list(df.columns) == backtest_cli._PRICE_COLS


# --- rendering --------------------------------------------------------------
def _result(entry="degree1", exit_="degree2", **kw):
    from portfolio_monitor import rules
    vals = dict(total_return=0.5, cagr=0.2, max_drawdown=0.1, num_trades=3,
                win_rate=0.66, has_open_trade=False)
    vals.update(kw)
    return StrategyResult(rules.parse_rule(entry), rules.parse_rule(exit_), **vals)


def _bt(results, **kw):
    base = dict(symbol="TST", window_start="2024-01-01", window_end="2024-12-31",
                best=results[0] if results else None, buy_hold_return=0.4,
                buy_hold_cagr=0.4, all_results=results, interval="daily",
                ma_periods=(5, 20), num_bars=250, data_start="2023-01-01")
    base.update(kw)
    return TickerBacktest(**base)


def test_render_shows_window_ladder_and_buy_hold():
    out = backtest_cli._render(_bt([_result()]), "Test Co", "cagr", 10)
    assert "TST — Test Co" in out
    assert "2024-01-01 → 2024-12-31" in out and "250 daily bars" in out
    assert "5/20" in out and "buy&hold +40.0%" in out


def test_render_always_labels_results_as_in_sample():
    out = backtest_cli._render(_bt([_result()]), "", "cagr", 10)
    assert "hindsight-selected" in out and "beat buy-and-hold" in out


def test_render_ranks_by_the_requested_metric_and_honours_top():
    rows = [_result("degree1", "degree1", cagr=0.1, max_drawdown=0.5),
            _result("degree2", "degree2", cagr=0.9, max_drawdown=0.9),
            _result("degree3", "degree3", cagr=0.5, max_drawdown=0.1)]
    by_cagr = backtest_cli._render(_bt(rows), "", "cagr", 1)
    assert "degree2" in by_cagr and "degree1" not in by_cagr.split("entry")[-1]
    by_dd = backtest_cli._render(_bt(rows), "", "drawdown", 1)
    assert "degree3" in by_dd.split("entry")[-1]


def test_render_marks_open_positions():
    out = backtest_cli._render(_bt([_result(has_open_trade=True)]), "", "cagr", 10)
    assert "*" in out and "still open at window end" in out


def test_render_skips_zero_trade_cells():
    out = backtest_cli._render(_bt([_result(num_trades=0)], best=None), "", "cagr", 10)
    assert "ever traded" in out


def test_render_reports_the_no_result_note():
    bt = _bt([], window_start=None, window_end=None, best=None, note="history too short")
    out = backtest_cli._render(bt, "", "cagr", 10)
    assert "no result: history too short" in out


def test_render_surfaces_a_caveat_note():
    out = backtest_cli._render(_bt([_result()], note="start clamped to 2024-01-01"),
                               "", "cagr", 10)
    assert "start clamped" in out


def test_as_dict_is_json_shaped():
    d = backtest_cli._as_dict(_bt([_result()]), "Test Co")
    assert d["symbol"] == "TST" and d["ma_periods"] == [5, 20]
    assert d["results"][0]["entry"] == "degree1"
    assert d["results"][0]["exit"] == "degree2"


def test_list_rules_exits_cleanly(capsys):
    assert backtest_cli._cli(["--list-rules"]) == 0
    out = capsys.readouterr().out
    assert "degreeN" in out and "cross:SHORT/LONG" in out
    assert "weekly" in out          # per-interval ladders are documented here


def test_sort_choices_all_have_a_key():
    parser = backtest_cli._build_parser()
    choices = next(a for a in parser._actions if a.dest == "sort").choices
    assert set(choices) == set(backtest_cli._SORTS)


def test_table_handles_an_empty_row_list():
    assert backtest_cli._table([], backtest_cli._HEAD).startswith("entry")


# --- the --refresh path (network stubbed; this path had no coverage and crashed) ---
def _conn(tmp_path):
    from portfolio_monitor import db
    return db.connect(tmp_path / "t.db")


def _seed(conn, ticker, dates, close=100.0):
    from portfolio_monitor import db
    db.upsert_prices(conn, [
        dict(ticker=ticker, date=d, open=close, high=close, low=close, close=close,
             adj_close=close, volume=1, source="yfinance") for d in dates])


def test_load_prices_without_refresh_reads_the_cache(tmp_path):
    import pandas as pd
    with _conn(tmp_path) as conn:
        _seed(conn, "TST", pd.bdate_range("2026-01-01", periods=5).strftime("%Y-%m-%d"))
        df, note = backtest_cli._load_prices(conn, "TST", years=2, refresh=False)
    assert len(df) == 5 and note == "cached"


def test_load_prices_with_refresh_syncs_incrementally_when_deep_enough(tmp_path, monkeypatch):
    import pandas as pd
    from portfolio_monitor import cache
    calls = []
    monkeypatch.setattr(cache, "sync_prices",
                        lambda conn, sym, years=2, force=False, **k:
                        calls.append({"sym": sym, "years": years, "force": force})
                        or cache.SyncResult(sym, "yfinance", "up-to-date", note="ok"))
    with _conn(tmp_path) as conn:
        _seed(conn, "TST", pd.bdate_range("2016-01-01", periods=5).strftime("%Y-%m-%d"))
        df, note = backtest_cli._load_prices(conn, "TST", years=2, refresh=True)
    assert calls == [{"sym": "TST", "years": 2, "force": False}]   # deep enough already
    assert note.startswith("up-to-date") and len(df) == 5


def test_load_prices_with_refresh_forces_a_full_fetch_when_too_shallow(tmp_path, monkeypatch):
    import pandas as pd
    from portfolio_monitor import cache
    calls = []
    monkeypatch.setattr(cache, "sync_prices",
                        lambda conn, sym, years=2, force=False, **k:
                        calls.append(force)
                        or cache.SyncResult(sym, "yfinance", "full", note="ok"))
    with _conn(tmp_path) as conn:
        _seed(conn, "TST", pd.bdate_range("2026-01-01", periods=5).strftime("%Y-%m-%d"))
        backtest_cli._load_prices(conn, "TST", years=10, refresh=True)
    assert calls == [True]        # 2026 cache can't answer a 10-year window


def test_load_prices_with_refresh_forces_when_the_cache_is_empty(tmp_path, monkeypatch):
    from portfolio_monitor import cache
    calls = []
    monkeypatch.setattr(cache, "sync_prices",
                        lambda conn, sym, years=2, force=False, **k:
                        calls.append(force)
                        or cache.SyncResult(sym, "yfinance", "full", note="ok"))
    with _conn(tmp_path) as conn:
        backtest_cli._load_prices(conn, "NEW", years=2, refresh=True)
    assert calls == [True]
