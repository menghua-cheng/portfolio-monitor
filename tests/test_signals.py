"""Verify trend-transition detection with synthetic, hand-crafted indicator series.

We feed detect_signals() precomputed sma columns so the crossing / trend-flip
bar is deterministic, then assert exactly the expected signal fires (and that an
unchanged state fires nothing).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import signals  # noqa: E402


def _frames(close, sma20, sma60, sma240):
    n = len(close)
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    prices = pd.DataFrame({"date": dates, "close": close})
    ind = pd.DataFrame({"date": dates, "sma20": sma20, "sma60": sma60,
                        "sma240": sma240})
    return prices, ind


def _types(sigs):
    return {s.signal_type for s in sigs}


def test_golden_cross():
    # sma20 goes from below to above sma60 on the last bar.
    prices, ind = _frames(close=[10, 10], sma20=[9.0, 13.0],
                          sma60=[12.0, 12.0], sma240=[np.nan, np.nan])
    assert "GOLDEN_CROSS" in _types(signals.detect_signals(prices, ind))


def test_death_cross():
    prices, ind = _frames(close=[10, 10], sma20=[13.0, 9.0],
                          sma60=[12.0, 12.0], sma240=[np.nan, np.nan])
    assert "DEATH_CROSS" in _types(signals.detect_signals(prices, ind))


def test_no_cross_when_state_unchanged():
    # short stays above long across both bars -> no cross signal.
    prices, ind = _frames(close=[10, 10], sma20=[13.0, 14.0],
                          sma60=[12.0, 12.0], sma240=[np.nan, np.nan])
    assert "GOLDEN_CROSS" not in _types(signals.detect_signals(prices, ind))
    assert "DEATH_CROSS" not in _types(signals.detect_signals(prices, ind))


def test_long_up_short_down():
    n = 15
    # sma240 steadily rising (long-term UP); price above sma20 until the final
    # bar, where it drops below -> LONG_UP_SHORT_DOWN transition.
    sma240 = list(np.linspace(100, 130, n))          # rising
    sma20 = [50.0] * n
    close = [60.0] * (n - 1) + [40.0]                # above, then below sma20
    sma60 = [50.0] * n
    prices, ind = _frames(close, sma20, sma60, sma240)
    sigs = _types(signals.detect_signals(prices, ind, slope_lookback=10))
    assert "LONG_UP_SHORT_DOWN" in sigs


def test_long_down_short_breakout():
    n = 15
    sma240 = list(np.linspace(130, 100, n))          # falling (long-term DOWN)
    sma20 = [50.0] * n
    close = [40.0] * (n - 1) + [60.0]                # below, then breaks above sma20
    sma60 = [50.0] * n
    prices, ind = _frames(close, sma20, sma60, sma240)
    sigs = _types(signals.detect_signals(prices, ind, slope_lookback=10))
    assert "LONG_DOWN_SHORT_BREAKOUT" in sigs


def test_no_trend_signal_when_state_unchanged():
    n = 15
    sma240 = list(np.linspace(100, 130, n))          # long-term up
    sma20 = [50.0] * n
    close = [60.0] * n                               # always above sma20 -> ALIGNED_UP throughout
    sma60 = [50.0] * n
    prices, ind = _frames(close, sma20, sma60, sma240)
    sigs = _types(signals.detect_signals(prices, ind, slope_lookback=10))
    assert "LONG_UP_SHORT_DOWN" not in sigs
    assert "ALIGNED_UP" not in sigs  # no transition -> no emission


def test_current_trend_state_label():
    n = 15
    sma240 = list(np.linspace(100, 130, n))
    sma20 = [50.0] * n
    close = [60.0] * n
    sma60 = [50.0] * n
    prices, ind = _frames(close, sma20, sma60, sma240)
    assert signals.current_trend_state(prices, ind, slope_lookback=10) == "ALIGNED_UP"


# --- Granular MA-cross events (detect_cross_events) --------------------------
def _full_frames(close, sma5, sma20, sma60, sma120, sma240):
    n = len(close)
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    prices = pd.DataFrame({"date": dates, "close": close})
    ind = pd.DataFrame({"date": dates, "sma5": sma5, "sma20": sma20,
                        "sma60": sma60, "sma120": sma120, "sma240": sma240})
    return prices, ind


def test_cross_event_5_over_20_up():
    # 周線(sma5) crosses ABOVE 月線(sma20) on the last bar; no other pair crosses.
    prices, ind = _full_frames(
        close=[10, 10], sma5=[8.0, 13.0], sma20=[10.0, 10.0],
        sma60=[20.0, 20.0], sma120=[30.0, 30.0], sma240=[40.0, 40.0])
    events = signals.detect_cross_events(prices, ind)
    assert len(events) == 1
    ev = events[0]
    assert ev.signal_type == "CROSS_UP_sma5_sma20"
    assert ev.label == "周線向上突破月線"
    assert ev.note is None


def test_cross_event_down_break():
    # 周線 crosses BELOW 月線 on the last bar -> 向下跌破.
    prices, ind = _full_frames(
        close=[10, 10], sma5=[13.0, 8.0], sma20=[10.0, 10.0],
        sma60=[20.0, 20.0], sma120=[30.0, 30.0], sma240=[40.0, 40.0])
    events = signals.detect_cross_events(prices, ind)
    assert len(events) == 1
    assert events[0].signal_type == "CROSS_DOWN_sma5_sma20"
    assert events[0].label == "周線向下跌破月線"


def test_double_trend_signal_annotation():
    # sma5>sma20 up at bar 2; sma20>sma60 up at the last bar (4) -> the last
    # event is annotated as a 雙重趨勢訊號 referencing the earlier周線 cross.
    prices, ind = _full_frames(
        close=[10] * 5,
        sma5=[8, 8, 13, 13, 13],
        sma20=[10, 10, 10, 10, 10],
        sma60=[12, 12, 12, 12, 9],
        sma120=[20] * 5,
        sma240=[30] * 5)
    events = signals.detect_cross_events(prices, ind, double_window_days=30)
    assert len(events) == 1
    ev = events[0]
    assert ev.signal_type == "CROSS_UP_sma20_sma60"
    assert ev.label == "月線向上突破季線"
    assert ev.note is not None
    assert "雙重趨勢訊號" in ev.note
    assert "周線" in ev.note and "2日前" in ev.note


def test_no_cross_event_when_flat():
    prices, ind = _full_frames(
        close=[10, 10], sma5=[13.0, 14.0], sma20=[10.0, 10.0],
        sma60=[9.0, 9.0], sma120=[8.0, 8.0], sma240=[7.0, 7.0])
    assert signals.detect_cross_events(prices, ind) == []


# --- Always-present trend summary (summarize_trend / alignment / multi-breakout) ---
def test_ma_alignment_bullish():
    prices, ind = _full_frames(
        close=[10, 10], sma5=[15.0, 15.0], sma20=[14.0, 14.0], sma60=[13.0, 13.0],
        sma120=[12.0, 12.0], sma240=[11.0, 11.0])
    ts = signals.summarize_trend(prices, ind)
    assert ts.alignment_dir == "up" and ts.alignment_zh == "多頭排列"


def test_ma_alignment_bearish():
    prices, ind = _full_frames(
        close=[10, 10], sma5=[11.0, 11.0], sma20=[12.0, 12.0], sma60=[13.0, 13.0],
        sma120=[14.0, 14.0], sma240=[15.0, 15.0])
    ts = signals.summarize_trend(prices, ind)
    assert ts.alignment_dir == "down" and ts.alignment_zh == "空頭排列"


def test_double_breakout_tag():
    # Two DIFFERENT pairs cross UP within the window -> 雙重突破.
    prices, ind = _full_frames(
        close=[10] * 5,
        sma5=[8, 8, 13, 13, 13],          # crosses above sma20 at bar 2
        sma20=[10, 10, 10, 10, 10],
        sma60=[12, 12, 12, 12, 9],         # sma20 crosses above sma60 at bar 4
        sma120=[20] * 5,
        sma240=[30] * 5)
    ts = signals.summarize_trend(prices, ind, window_days=30)
    zh_tags = [zh for _, zh in ts.tags]
    assert "雙重突破" in zh_tags
    # recent list carries both crossings with days_ago
    assert len(ts.recent) == 2 and ts.recent[0].days_ago <= ts.recent[1].days_ago


def test_recent_events_respect_window():
    prices, ind = _full_frames(
        close=[10] * 5,
        sma5=[8, 13, 13, 13, 13],          # single cross very early (bar 1)
        sma20=[10, 10, 10, 10, 10],
        sma60=[12] * 5, sma120=[20] * 5, sma240=[30] * 5)
    # bar 1 is 3 days before the last bar (index 4); window of 1 day excludes it.
    assert signals.detect_recent_cross_events(prices, ind, window_days=1) == []
    assert len(signals.detect_recent_cross_events(prices, ind, window_days=10)) == 1
