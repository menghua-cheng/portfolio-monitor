"""Verify SMA/EMA computation against hand-computed values and warm-up nulls."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import indicators  # noqa: E402


def _prices(closes):
    dates = pd.date_range("2020-01-01", periods=len(closes), freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "close": closes})


def test_sma5_hand_computed():
    # closes 1..10; 5-day SMA at index 4 = mean(1..5)=3, at index 9 = mean(6..10)=8
    df = _prices(list(range(1, 11)))
    ind = indicators.compute_indicators(df)
    assert pd.isna(ind["sma5"].iloc[3]), "null before 5 bars"
    assert ind["sma5"].iloc[4] == 3.0
    assert ind["sma5"].iloc[9] == 8.0


def test_ema_matches_manual_recursion():
    closes = [10, 11, 12, 13, 14, 15, 16]
    df = _prices(closes)
    ind = indicators.compute_indicators(df)
    # EMA span=5 => alpha = 2/(5+1) = 1/3, adjust=False, seeded at the 5th bar.
    span = 5
    alpha = 2 / (span + 1)
    s = pd.Series(closes)
    manual = s.ewm(span=span, adjust=False, min_periods=span).mean()
    got = ind["ema5"]
    assert pd.isna(got.iloc[3]) and pd.isna(manual.iloc[3])
    for i in range(4, len(closes)):
        assert abs(got.iloc[i] - manual.iloc[i]) < 1e-9
    # sanity: alpha used correctly -> next = prev + alpha*(price-prev)
    prev = manual.iloc[4]
    expected5 = prev + alpha * (closes[5] - prev)
    assert abs(got.iloc[5] - expected5) < 1e-9


def test_year_line_null_until_240_bars():
    df = _prices([100.0] * 239)
    ind = indicators.compute_indicators(df)
    assert ind["sma240"].isna().all(), "240-day line must be null with <240 bars"

    df2 = _prices([100.0] * 240)
    ind2 = indicators.compute_indicators(df2)
    assert not pd.isna(ind2["sma240"].iloc[239])
    assert ind2["sma240"].iloc[239] == 100.0


def test_to_indicator_rows_nan_to_none():
    df = _prices(list(range(1, 7)))
    ind = indicators.compute_indicators(df)
    rows = indicators.to_indicator_rows("aapl", ind)
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["sma5"] is None            # unwarmed -> None
    assert rows[4]["sma5"] == 3.0             # warmed
