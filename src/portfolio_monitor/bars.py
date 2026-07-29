"""Bar time-scale conversion (feature: 可切換週期).

The daily report always works on daily bars. The backtest explorer can also
trade **weekly** or **monthly** bars: daily OHLCV is bucketed by ISO week or
calendar month and aggregated (open=first, high=max, low=min, close/adj_close=
last, volume=sum). A bar keeps the *last real trading date* in its bucket rather
than the period end, so calendar-day windows and printed dates stay honest and
the final bar is never dated in the future.

Because MA periods are counted in **bars**, the same ladder means different
things per interval — `sma20` is a month of daily bars but five months of weekly
ones. So each interval carries its own default ladder chosen to keep the
familiar 月/季/半年/年線 meanings:

    daily    5, 20, 60, 120, 240   week, month, quarter, half-year, year
    weekly   4, 13, 26, 52, 104    month, quarter, half-year, year, 2 years
    monthly  3, 6, 12, 24, 60      quarter, half-year, year, 2 years, 5 years

Override with an explicit ladder when you want something else. Note that the
slowest line sets the warm-up cost: 104 weekly bars needs ~2 years of history
*before* the first tradable bar, so widen `--years` when you go coarse.
"""
from __future__ import annotations

import pandas as pd

INTERVALS = ("daily", "weekly", "monthly")

DEFAULT_LADDERS: dict[str, tuple[int, ...]] = {
    "daily": (5, 20, 60, 120, 240),
    "weekly": (4, 13, 26, 52, 104),
    "monthly": (3, 6, 12, 24, 60),
}

# Approximate trading bars per year, used to translate a ladder's warm-up cost
# into the calendar history a run needs.
BARS_PER_YEAR = {"daily": 252, "weekly": 52, "monthly": 12}

_AGG = {
    "date": ("date", "last"),
    "open": ("open", "first"),
    "high": ("high", "max"),
    "low": ("low", "min"),
    "close": ("close", "last"),
    "adj_close": ("adj_close", "last"),
    "volume": ("volume", "sum"),
}


def normalize_interval(interval: str) -> str:
    """Accept a few natural spellings (`d`, `day`, `w`, `week`, `m`, `month`)."""
    t = (interval or "daily").strip().lower()
    aliases = {"d": "daily", "day": "daily", "1d": "daily",
               "w": "weekly", "week": "weekly", "1w": "weekly",
               "m": "monthly", "month": "monthly", "1mo": "monthly", "mo": "monthly"}
    t = aliases.get(t, t)
    if t not in INTERVALS:
        raise ValueError(f"unknown interval {interval!r}; choose from {', '.join(INTERVALS)}")
    return t


def default_ladder(interval: str) -> tuple[int, ...]:
    return DEFAULT_LADDERS[normalize_interval(interval)]


def resample_bars(prices: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Aggregate a daily price frame to `interval` bars.

    `prices` needs date/open/high/low/close/adj_close/volume, ascending by date.
    Returns the same columns (plus `source`, carried through when present),
    reindexed 0..N. `daily` is a sorted pass-through.
    """
    interval = normalize_interval(interval)
    df = prices.sort_values("date").reset_index(drop=True)
    if interval == "daily" or df.empty:
        return df

    key = pd.to_datetime(df["date"]).dt.to_period("W" if interval == "weekly" else "M")
    agg = dict(_AGG)
    if "source" in df.columns:
        agg["source"] = ("source", "last")
    out = df.groupby(key, sort=True).agg(**agg).reset_index(drop=True)
    return out


def min_history_years(interval: str, ladder) -> float:
    """Calendar years of data the slowest line needs just to warm up. Callers add
    the span they actually want to trade on top of this."""
    interval = normalize_interval(interval)
    periods = [int(p) for p in ladder]
    return (max(periods) if periods else 0) / BARS_PER_YEAR[interval]
