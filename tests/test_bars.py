"""Bar time-scale conversion tests.

The invariants that matter: OHLCV aggregates the way a bar should, a bar's date
is a real trading date from inside its bucket (never a synthetic period end), and
each interval keeps its own MA ladder.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import bars  # noqa: E402


def _daily(dates, open_, high, low, close, adj_close=None, volume=None):
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": close,
        "adj_close": adj_close if adj_close is not None else close,
        "volume": volume if volume is not None else [1] * len(dates),
        "source": ["t"] * len(dates),
    })


# --- interval normalization -------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("daily", "daily"), ("D", "daily"), ("day", "daily"),
    ("weekly", "weekly"), ("w", "weekly"),
    ("monthly", "monthly"), ("mo", "monthly"), ("1mo", "monthly"),
])
def test_normalize_interval_aliases(text, expected):
    assert bars.normalize_interval(text) == expected


def test_normalize_interval_rejects_unknown():
    with pytest.raises(ValueError):
        bars.normalize_interval("hourly")


# --- resampling -------------------------------------------------------------
def test_daily_is_a_sorted_passthrough():
    df = _daily(["2024-01-03", "2024-01-02"], [1, 2], [1, 2], [1, 2], [1, 2])
    out = bars.resample_bars(df, "daily")
    assert list(out["date"]) == ["2024-01-02", "2024-01-03"]


def test_weekly_aggregates_ohlcv_per_iso_week():
    # Mon-Wed of one week, then Mon of the next.
    df = _daily(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-08"],
                open_=[10, 11, 12, 20], high=[15, 16, 14, 22],
                low=[9, 8, 11, 19], close=[11, 12, 13, 21], volume=[1, 2, 3, 4])
    out = bars.resample_bars(df, "weekly")
    assert len(out) == 2
    first = out.iloc[0]
    assert first["open"] == 10 and first["close"] == 13
    assert first["high"] == 16 and first["low"] == 8
    assert first["volume"] == 6
    assert first["date"] == "2024-01-03"       # last real trading date in the bucket
    assert out.iloc[1]["date"] == "2024-01-08"


def test_monthly_aggregates_per_calendar_month():
    df = _daily(["2024-01-05", "2024-01-31", "2024-02-01"],
                open_=[10, 11, 20], high=[12, 13, 21], low=[9, 10, 19],
                close=[11, 12, 20])
    out = bars.resample_bars(df, "monthly")
    assert list(out["date"]) == ["2024-01-31", "2024-02-01"]
    assert out.iloc[0]["open"] == 10 and out.iloc[0]["close"] == 12


def test_resample_carries_adjusted_close_as_the_bucket_last():
    df = _daily(["2024-01-01", "2024-01-02"], [1, 1], [1, 1], [1, 1], [10, 20],
                adj_close=[5, 9])
    out = bars.resample_bars(df, "weekly")
    assert out.iloc[0]["adj_close"] == 9


def test_resample_empty_frame_is_empty():
    df = _daily([], [], [], [], [])
    assert bars.resample_bars(df, "weekly").empty


def test_resample_tolerates_a_missing_source_column():
    df = _daily(["2024-01-01"], [1], [1], [1], [1]).drop(columns=["source"])
    out = bars.resample_bars(df, "weekly")
    assert len(out) == 1 and "source" not in out.columns


# --- ladders / warm-up ------------------------------------------------------
def test_each_interval_has_its_own_ladder():
    assert bars.default_ladder("daily") == (5, 20, 60, 120, 240)
    assert bars.default_ladder("w") == (4, 13, 26, 52, 104)
    assert bars.default_ladder("monthly") == (3, 6, 12, 24, 60)


def test_min_history_years_tracks_the_slowest_line():
    assert bars.min_history_years("daily", (5, 240)) == pytest.approx(240 / 252)
    assert bars.min_history_years("weekly", (4, 104)) == pytest.approx(2.0)
    assert bars.min_history_years("monthly", (3, 60)) == pytest.approx(5.0)


def test_min_history_years_of_empty_ladder_is_zero():
    assert bars.min_history_years("daily", []) == 0
