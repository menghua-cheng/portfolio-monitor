"""Compute moving averages (SMA + EMA) for each configured period.

Periods (trading days) map to the Chinese-convention MA "lines":
    5=周線, 20=月線, 60=季線, 120=半年線, 240=年線

SMA = simple rolling mean; a value is null until `period` bars exist.
EMA = exponentially-weighted mean (span=period); we also require `period` bars
      of warm-up before emitting a value so short and long EMAs are comparable.
"""
from __future__ import annotations

import pandas as pd

# period label -> trading-day window. Kept in sync with config ma_periods.
PERIODS = {5: "5", 20: "20", 60: "60", 120: "120", 240: "240"}


def compute_indicators(prices: pd.DataFrame, periods=None) -> pd.DataFrame:
    """Given a price DataFrame (must contain 'date' and 'close', ascending by
    date), return a DataFrame with columns: date, sma5.., ema5.. (NaN until warm).

    `periods` overrides the default daily ladder — the backtest explorer passes a
    coarser one for weekly/monthly bars, where MA periods count in bars, not days.
    """
    periods = sorted({int(p) for p in (periods if periods is not None else PERIODS)})
    if prices.empty:
        return pd.DataFrame(columns=["date", *[f"sma{p}" for p in periods],
                                     *[f"ema{p}" for p in periods]])

    df = prices.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)

    out = pd.DataFrame({"date": df["date"].values})
    for period in periods:
        # min_periods=period => null until we have a full window.
        out[f"sma{period}"] = close.rolling(window=period, min_periods=period).mean().values
        ema = close.ewm(span=period, adjust=False, min_periods=period).mean()
        out[f"ema{period}"] = ema.values
    return out


def to_indicator_rows(ticker: str, indicators: pd.DataFrame) -> list[dict]:
    """Convert an indicators DataFrame into db.upsert_indicators row dicts.

    NaN (unwarmed) values become None so they store as SQL NULL.
    """
    ticker = ticker.upper()
    cols = [f"sma{p}" for p in PERIODS] + [f"ema{p}" for p in PERIODS]
    rows = []
    for _, r in indicators.iterrows():
        row = {"ticker": ticker, "date": r["date"]}
        for c in cols:
            val = r[c]
            row[c] = None if pd.isna(val) else float(val)
        rows.append(row)
    return rows
