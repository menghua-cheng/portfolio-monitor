"""Trend-transition detection (feature 4).

Two independent state families are tracked per ticker:

1. CROSS family — short MA (sma20) vs long MA (sma60):
     GOLDEN_CROSS : sma20 crosses ABOVE sma60
     DEATH_CROSS  : sma20 crosses BELOW sma60

2. TREND family — combination of long-term direction (sma240 slope) and the
   price's short-term posture relative to sma20:
     LONG_UP_SHORT_DOWN      : long-term up, but price fell below sma20 (pullback risk)
     LONG_DOWN_SHORT_BREAKOUT: long-term down, but price broke above sma20 (reversal attempt)
     ALIGNED_UP              : long-term up and price above sma20
     ALIGNED_DOWN            : long-term down and price below sma20

A signal is only emitted when the current state DIFFERS from the previous bar's
state (a genuine transition), so the daily report never repeats a stale signal.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    date: str
    signal_type: str
    detail: str


# "line" names for each MA period, used in the granular cross details.
MA_CH = {"sma5": "周線", "sma20": "月線", "sma60": "季線",
         "sma120": "半年線", "sma240": "年線"}
MA_EN = {"sma5": "Weekly", "sma20": "Monthly", "sma60": "Quarterly",
         "sma120": "Half-year", "sma240": "Yearly"}
# Adjacent MA pairs (short, long) whose crossings we report in the top table.
_ADJ_PAIRS = [("sma5", "sma20"), ("sma20", "sma60"),
              ("sma60", "sma120"), ("sma120", "sma240")]
_DIR_PHRASE = {"up": "向上突破", "down": "向下跌破"}
_DIR_EN = {"up": "breaks above", "down": "breaks below"}
_DIR_EN_PAST = {"up": "broke above", "down": "broke below"}


@dataclass
class CrossEvent:
    """A moving-average crossing that fired on the latest bar.

    `note`/`note_en` carry an optional 雙重趨勢訊號 (dual trend signal) annotation:
    a same-direction crossing on a *different* adjacent pair that happened within
    the lookback window, confirming a broader trend shift.
    """
    date: str
    short: str            # e.g. "sma5"
    long: str             # e.g. "sma20"
    direction: str        # "up" | "down"
    label: str            # zh, e.g. "周線向上突破月線"
    signal_type: str      # e.g. "CROSS_UP_sma5_sma20"
    note: str | None = None       # zh dual-signal annotation
    label_en: str = ""            # en, e.g. "Weekly line breaks above Monthly line"
    note_en: str | None = None    # en dual-signal annotation
    days_ago: int = 0             # calendar days before the latest bar (0 = today)


# Chinese/English numeral prefixes for multi-line breakout tags (2->雙, 3->三, …).
_NUM_ZH = {2: "雙", 3: "三", 4: "四"}
_NUM_EN = {2: "Double", 3: "Triple", 4: "Quadruple"}


@dataclass
class TrendSummary:
    """Always-present trend picture for a ticker (feature: 更多趨勢資訊).

    * alignment  — MA stacking: 多頭排列 / 空頭排列 / 多空交錯.
    * tags       — aggregate multi-line events in the window: 雙重/三重突破 (up)
                   or 雙重/三重跌破 (down). Each tag is an (en, zh) pair.
    * recent     — individual crossings within the window, most recent first.
    """
    alignment_zh: str
    alignment_en: str
    alignment_dir: str          # "up" | "down" | "mixed" (for colouring)
    tags: list                  # list[tuple[str, str]] -> (en, zh)
    recent: list                # list[CrossEvent]


def _slope_direction(series: pd.Series, lookback: int, flat_threshold_pct: float) -> str:
    """Classify a MA's direction over `lookback` bars as up/down/flat."""
    s = series.dropna()
    if len(s) <= lookback:
        return "unknown"
    now = s.iloc[-1]
    then = s.iloc[-1 - lookback]
    if then == 0 or pd.isna(now) or pd.isna(then):
        return "unknown"
    pct = (now - then) / abs(then) * 100.0
    if pct > flat_threshold_pct:
        return "up"
    if pct < -flat_threshold_pct:
        return "down"
    return "flat"


def _cross_state(sma_short: pd.Series, sma_long: pd.Series) -> pd.Series:
    """Per-bar state: +1 when short>=long, -1 when short<long, NaN when unknown."""
    diff = sma_short - sma_long
    state = pd.Series(index=diff.index, dtype="float")
    state[diff >= 0] = 1.0
    state[diff < 0] = -1.0
    state[diff.isna()] = float("nan")
    return state


def detect_signals(prices: pd.DataFrame, indicators: pd.DataFrame,
                   slope_lookback: int = 10, flat_threshold_pct: float = 0.5) -> list[Signal]:
    """Return the list of transition signals that fire on the LAST bar.

    `prices` needs columns date, close. `indicators` needs date + sma columns.
    Both must be ascending by date and aligned on date.
    """
    if prices.empty or indicators.empty:
        return []

    df = prices[["date", "close"]].merge(indicators, on="date", how="inner")
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 2:
        return []

    signals: list[Signal] = []
    last_date = df["date"].iloc[-1]

    # --- CROSS family: sma20 vs sma60 ---------------------------------------
    cross = _cross_state(df["sma20"], df["sma60"])
    prev, curr = cross.iloc[-2], cross.iloc[-1]
    if pd.notna(prev) and pd.notna(curr) and prev != curr:
        if curr > prev:  # -1 -> +1
            signals.append(Signal(last_date, "GOLDEN_CROSS",
                                  "sma20 crossed above sma60"))
        else:            # +1 -> -1
            signals.append(Signal(last_date, "DEATH_CROSS",
                                  "sma20 crossed below sma60"))

    # --- TREND family: long-term slope + price vs sma20 ---------------------
    trend_state = _trend_state_series(df, slope_lookback, flat_threshold_pct)
    prev_t, curr_t = trend_state.iloc[-2], trend_state.iloc[-1]
    if curr_t not in ("unknown",) and prev_t != curr_t:
        detail = f"transition {prev_t} -> {curr_t}"
        signals.append(Signal(last_date, curr_t, detail))

    return signals


def _trend_state_series(df: pd.DataFrame, slope_lookback: int,
                        flat_threshold_pct: float) -> pd.Series:
    """Compute the TREND-family state label for every bar (vectorized enough for
    daily use; recomputes slope per bar via a rolling window)."""
    close = df["close"].astype(float)
    sma20 = df["sma20"]
    sma240 = df["sma240"]

    states = []
    for i in range(len(df)):
        long_dir = _slope_direction(sma240.iloc[: i + 1], slope_lookback, flat_threshold_pct)
        price = close.iloc[i]
        s20 = sma20.iloc[i]
        if long_dir == "unknown" or pd.isna(s20):
            states.append("unknown")
            continue
        price_above = price >= s20
        if long_dir == "up":
            states.append("ALIGNED_UP" if price_above else "LONG_UP_SHORT_DOWN")
        elif long_dir == "down":
            states.append("LONG_DOWN_SHORT_BREAKOUT" if price_above else "ALIGNED_DOWN")
        else:  # flat long-term trend -> classify by price posture only
            states.append("ALIGNED_UP" if price_above else "ALIGNED_DOWN")
    return pd.Series(states, index=df.index)


def _pair_cross_events(df: pd.DataFrame, short: str, long: str) -> list[tuple[int, str, str]]:
    """Every crossing of one adjacent MA pair over the full history.

    Returns (bar_index, date, direction) tuples in date order, where direction
    is "up" (short crosses above long) or "down" (short crosses below long).
    """
    if short not in df.columns or long not in df.columns:
        return []
    state = _cross_state(df[short], df[long])
    events: list[tuple[int, str, str]] = []
    for i in range(1, len(state)):
        prev, curr = state.iloc[i - 1], state.iloc[i]
        if pd.notna(prev) and pd.notna(curr) and prev != curr:
            events.append((i, df["date"].iloc[i], "up" if curr > prev else "down"))
    return events


def detect_cross_events(prices: pd.DataFrame, indicators: pd.DataFrame,
                        double_window_days: int = 30) -> list[CrossEvent]:
    """Granular MA-cross signals that fire on the LATEST bar (feature: 更多訊號細節).

    Scans every adjacent MA pair (周/月/季/半年/年線) for a crossing on the last
    bar, e.g. 「5日線突破月線」or「月線突破季線」. Each such crossing is annotated as
    a 雙重趨勢訊號 when a *same-direction* crossing on another pair occurred within
    `double_window_days` calendar days — e.g. 「n日前 2026-06-29 周線已向上突破月線」.
    """
    if prices.empty or indicators.empty:
        return []
    df = prices[["date", "close"]].merge(indicators, on="date", how="inner")
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 2:
        return []

    last_idx = len(df) - 1
    last_date = df["date"].iloc[last_idx]

    all_events: list[tuple[int, str, str, str, str]] = []   # idx, date, dir, short, long
    today_events: list[tuple[int, str, str, str, str]] = []
    for short, long in _ADJ_PAIRS:
        for idx, edate, direction in _pair_cross_events(df, short, long):
            rec = (idx, edate, direction, short, long)
            all_events.append(rec)
            if idx == last_idx:
                today_events.append(rec)

    results: list[CrossEvent] = []
    for idx, edate, direction, short, long in today_events:
        label = f"{MA_CH[short]}{_DIR_PHRASE[direction]}{MA_CH[long]}"
        label_en = f"{MA_EN[short]} line {_DIR_EN[direction]} {MA_EN[long]} line"
        sig_type = f"CROSS_{'UP' if direction == 'up' else 'DOWN'}_{short}_{long}"

        # Most recent same-direction crossing on a DIFFERENT pair -> double signal.
        related = [e for e in all_events
                   if e[2] == direction and (e[3], e[4]) != (short, long)]
        related.sort(key=lambda e: e[0])
        note = note_en = None
        for r_idx, r_date, r_dir, r_short, r_long in reversed(related):
            days_ago = (pd.Timestamp(last_date) - pd.Timestamp(r_date)).days
            if days_ago > double_window_days:
                continue
            if days_ago == 0:
                note = (f"本日 {MA_CH[r_short]}亦{_DIR_PHRASE[r_dir]}"
                        f"{MA_CH[r_long]},為雙重趨勢訊號")
                note_en = (f"today {MA_EN[r_short]} line also {_DIR_EN[r_dir]} "
                           f"{MA_EN[r_long]} line — dual trend signal")
            else:
                note = (f"{days_ago}日前 {r_date} {MA_CH[r_short]}已{_DIR_PHRASE[r_dir]}"
                        f"{MA_CH[r_long]},為雙重趨勢訊號")
                note_en = (f"{days_ago}d ago ({r_date}) {MA_EN[r_short]} line already "
                           f"{_DIR_EN_PAST[r_dir]} {MA_EN[r_long]} line — dual trend signal")
            break
        results.append(CrossEvent(edate, short, long, direction, label,
                                  sig_type, note, label_en=label_en, note_en=note_en))
    return results


def detect_recent_cross_events(prices: pd.DataFrame, indicators: pd.DataFrame,
                               window_days: int = 30) -> list[CrossEvent]:
    """All MA crossings within the last `window_days` calendar days (not just the
    latest bar), most-recent first, each carrying `days_ago`. Feeds the always-on
    trend summary so the report shows recent 突破/跌破 even on a quiet day."""
    if prices.empty or indicators.empty:
        return []
    df = prices[["date", "close"]].merge(indicators, on="date", how="inner")
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 2:
        return []

    last_date = df["date"].iloc[-1]
    out: list[CrossEvent] = []
    for short, long in _ADJ_PAIRS:
        for idx, edate, direction in _pair_cross_events(df, short, long):
            days_ago = (pd.Timestamp(last_date) - pd.Timestamp(edate)).days
            if days_ago < 0 or days_ago > window_days:
                continue
            label = f"{MA_CH[short]}{_DIR_PHRASE[direction]}{MA_CH[long]}"
            label_en = f"{MA_EN[short]} line {_DIR_EN[direction]} {MA_EN[long]} line"
            sig = f"CROSS_{'UP' if direction == 'up' else 'DOWN'}_{short}_{long}"
            out.append(CrossEvent(edate, short, long, direction, label, sig,
                                  label_en=label_en, days_ago=days_ago))
    out.sort(key=lambda e: e.days_ago)
    return out


def _ma_alignment(indicators: pd.DataFrame) -> tuple[str, str, str]:
    """Classify the latest MA stacking as bullish / bearish / mixed.

    Returns (zh, en, direction). Bullish (多頭排列) = sma5>sma20>sma60>sma120>sma240;
    bearish (空頭排列) = the reverse; otherwise 多空交錯 (mixed)."""
    if indicators.empty:
        return ("資料不足", "Insufficient data", "mixed")
    last = indicators.sort_values("date").iloc[-1]
    vals = [last.get(f"sma{p}") for p in (5, 20, 60, 120, 240)]
    if any(v is None or pd.isna(v) for v in vals):
        return ("資料不足", "Insufficient data", "mixed")
    vals = [float(v) for v in vals]
    if all(vals[i] > vals[i + 1] for i in range(4)):
        return ("多頭排列", "Bullish alignment", "up")
    if all(vals[i] < vals[i + 1] for i in range(4)):
        return ("空頭排列", "Bearish alignment", "down")
    return ("多空交錯", "Mixed alignment", "mixed")


def summarize_trend(prices: pd.DataFrame, indicators: pd.DataFrame,
                    window_days: int = 30) -> TrendSummary:
    """Build the always-present trend summary: MA alignment + multi-line
    breakout/breakdown tags (雙重/三重突破・跌破) + the recent-crossings list."""
    recent = detect_recent_cross_events(prices, indicators, window_days)
    up_pairs = {(e.short, e.long) for e in recent if e.direction == "up"}
    down_pairs = {(e.short, e.long) for e in recent if e.direction == "down"}
    tags: list[tuple[str, str]] = []
    if len(up_pairs) >= 2:
        n = min(len(up_pairs), 4)
        tags.append((f"{_NUM_EN[n]} breakout", f"{_NUM_ZH[n]}重突破"))
    if len(down_pairs) >= 2:
        n = min(len(down_pairs), 4)
        tags.append((f"{_NUM_EN[n]} breakdown", f"{_NUM_ZH[n]}重跌破"))
    az, ae, ad = _ma_alignment(indicators)
    return TrendSummary(az, ae, ad, tags, recent)


def current_trend_state(prices: pd.DataFrame, indicators: pd.DataFrame,
                        slope_lookback: int = 10,
                        flat_threshold_pct: float = 0.5) -> str:
    """The TREND-family label on the latest bar (for the report summary)."""
    if prices.empty or indicators.empty:
        return "unknown"
    df = prices[["date", "close"]].merge(indicators, on="date", how="inner")
    df = df.sort_values("date").reset_index(drop=True)
    if df.empty:
        return "unknown"
    return _trend_state_series(df, slope_lookback, flat_threshold_pct).iloc[-1]
