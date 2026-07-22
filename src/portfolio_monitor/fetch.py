"""Fetch daily OHLCV history from Yahoo Finance (primary), with a best-effort
independent cross-check for data-quality validation.

Cross-check source, in priority order:
  1. Tiingo  — used when TIINGO_API_KEY is set (free key, generous limits).
  2. Stooq   — key-free, but often blocked by an anti-bot challenge; best-effort.
If neither is reachable, the fetch still succeeds on yfinance alone and relies
on the internal sanity validator (validate_history).

Returns a tidy pandas DataFrame with columns:
    date, open, high, low, close, adj_close, volume, source
indexed 0..N, sorted ascending by date.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

log = logging.getLogger(__name__)

# Flag day-over-day close moves above this as suspicious (helps catch bad ticks
# / unadjusted split rows). Real single-day moves rarely exceed this.
_MAX_DAILY_MOVE_PCT = 50.0
# Consider the series stale if the last bar is older than this many calendar days.
_STALENESS_DAYS = 7

_COLUMNS = ["date", "open", "high", "low", "close", "adj_close", "volume", "source"]


@dataclass
class CrossCheck:
    ticker: str
    yf_close: float | None
    ref_close: float | None
    ref_source: str | None
    diff_pct: float | None
    within_tolerance: bool
    note: str


# --------------------------------------------------------------------------- #
# Individual sources
# --------------------------------------------------------------------------- #
def fetch_yfinance(ticker: str, years: int) -> pd.DataFrame:
    """Fetch from Yahoo Finance via yfinance. Returns empty DataFrame on failure."""
    import yfinance as yf

    start = (date.today() - timedelta(days=int(years * 365.25) + 10)).isoformat()
    try:
        raw = yf.download(ticker, start=start, auto_adjust=False,
                          progress=False, threads=False)
    except Exception as exc:  # network / endpoint change
        log.warning("yfinance download failed for %s: %s", ticker, exc)
        return pd.DataFrame(columns=_COLUMNS)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=_COLUMNS)

    # yfinance may return a MultiIndex column frame when given one ticker.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = raw.reset_index()
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d")
    out["open"] = raw["Open"].astype(float)
    out["high"] = raw["High"].astype(float)
    out["low"] = raw["Low"].astype(float)
    out["close"] = raw["Close"].astype(float)
    out["adj_close"] = raw.get("Adj Close", raw["Close"]).astype(float)
    out["volume"] = raw["Volume"].fillna(0).astype("int64")
    out["source"] = "yfinance"
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def fetch_stooq(ticker: str, years: int) -> pd.DataFrame:
    """Fetch daily EOD from Stooq via pandas-datareader. Returns empty on failure."""
    from pandas_datareader import data as pdr

    start = date.today() - timedelta(days=int(years * 365.25) + 10)
    try:
        raw = pdr.DataReader(ticker, "stooq", start=start)
    except Exception as exc:
        # Known-dead paths (unimplemented reader / anti-bot wall) are expected;
        # log at debug to avoid noise on every run.
        log.debug("stooq download unavailable for %s: %s", ticker, exc)
        return pd.DataFrame(columns=_COLUMNS)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=_COLUMNS)

    raw = raw.sort_index().reset_index()  # Stooq returns most-recent-first
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d")
    out["open"] = raw["Open"].astype(float)
    out["high"] = raw["High"].astype(float)
    out["low"] = raw["Low"].astype(float)
    out["close"] = raw["Close"].astype(float)
    # Stooq has no adjusted close; reuse close.
    out["adj_close"] = raw["Close"].astype(float)
    out["volume"] = raw.get("Volume", pd.Series([0] * len(raw))).fillna(0).astype("int64")
    out["source"] = "stooq"
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def fetch_tiingo(ticker: str, years: int, api_key: str | None = None) -> pd.DataFrame:
    """Fetch daily EOD from Tiingo (independent cross-check source).

    Requires a free API key (TIINGO_API_KEY). Returns empty DataFrame if no key
    is configured or the request fails.
    """
    api_key = api_key or os.getenv("TIINGO_API_KEY", "")
    if not api_key:
        return pd.DataFrame(columns=_COLUMNS)

    start = (date.today() - timedelta(days=int(years * 365.25) + 10)).isoformat()
    url = (f"https://api.tiingo.com/tiingo/daily/{ticker.lower()}/prices"
           f"?startDate={start}&token={api_key}")
    try:
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("tiingo download failed for %s: %s", ticker, exc)
        return pd.DataFrame(columns=_COLUMNS)

    if not payload:
        return pd.DataFrame(columns=_COLUMNS)

    raw = pd.DataFrame(payload)
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d")
    out["open"] = raw["open"].astype(float)
    out["high"] = raw["high"].astype(float)
    out["low"] = raw["low"].astype(float)
    out["close"] = raw["close"].astype(float)
    out["adj_close"] = raw.get("adjClose", raw["close"]).astype(float)
    out["volume"] = raw["volume"].fillna(0).astype("int64")
    out["source"] = "tiingo"
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def fetch_reference(ticker: str, years: int) -> pd.DataFrame:
    """Best-effort independent source for cross-checking: Tiingo first (if keyed),
    else Stooq. Returns empty DataFrame if none is reachable."""
    ref = fetch_tiingo(ticker, years)
    if not ref.empty:
        return ref
    return fetch_stooq(ticker, years)


# --------------------------------------------------------------------------- #
# Orchestration + validation
# --------------------------------------------------------------------------- #
def _latest_close(df: pd.DataFrame) -> float | None:
    if df.empty:
        return None
    return float(df.sort_values("date").iloc[-1]["close"])


def cross_check(ticker: str, yf_df: pd.DataFrame, ref_df: pd.DataFrame,
                tolerance_pct: float) -> CrossCheck:
    """Compare the latest overlapping close of the primary (yfinance) and an
    independent reference source."""
    yf_c = _latest_close(yf_df)
    if yf_df.empty or ref_df.empty:
        ref_c = _latest_close(ref_df)
        ref_src = ref_df["source"].iloc[-1] if not ref_df.empty else None
        return CrossCheck(ticker, yf_c, ref_c, ref_src, None, True,
                          "no independent reference available; cross-check skipped")
    # Align on the most recent date both sources share.
    merged = yf_df[["date", "close"]].merge(
        ref_df[["date", "close"]], on="date", suffixes=("_yf", "_ref"))
    if merged.empty:
        return CrossCheck(ticker, yf_c, _latest_close(ref_df),
                          ref_df["source"].iloc[-1], None, True,
                          "sources share no common date; cross-check skipped")
    row = merged.sort_values("date").iloc[-1]
    yf_v, ref_v = float(row["close_yf"]), float(row["close_ref"])
    diff_pct = abs(yf_v - ref_v) / yf_v * 100.0 if yf_v else None
    within = diff_pct is not None and diff_pct <= tolerance_pct
    ref_src = ref_df["source"].iloc[-1]
    note = (f"OK vs {ref_src} on {row['date']}" if within
            else f"DISCREPANCY {diff_pct:.2f}% vs {ref_src} on {row['date']} "
                 f"(> {tolerance_pct}% tolerance)")
    return CrossCheck(ticker, yf_v, ref_v, ref_src, diff_pct, within, note)


def validate_history(df: pd.DataFrame) -> list[str]:
    """Return a list of data-quality problems (empty list = clean)."""
    problems: list[str] = []
    if df.empty:
        return ["no rows returned"]
    if df[["open", "high", "low", "close"]].isna().any().any():
        problems.append("NaN present in OHLC columns")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        problems.append("non-positive price present")
    dates = pd.to_datetime(df["date"])
    if not dates.is_monotonic_increasing:
        problems.append("dates not monotonically increasing")
    if dates.duplicated().any():
        problems.append("duplicate dates present")
    bad_hl = int((df["high"] < df["low"]).sum())
    if bad_hl:
        problems.append(f"{bad_hl} rows with high < low")
    # Staleness: last bar should be recent (accounts for weekends/holidays).
    last = dates.max()
    if (pd.Timestamp(date.today()) - last).days > _STALENESS_DAYS:
        problems.append(f"stale: last bar {last.date()} older than {_STALENESS_DAYS}d")
    # Extreme day-over-day close moves (bad tick / unadjusted split).
    pct = df.sort_values("date")["close"].pct_change().abs() * 100.0
    n_extreme = int((pct > _MAX_DAILY_MOVE_PCT).sum())
    if n_extreme:
        problems.append(f"{n_extreme} extreme daily moves > {_MAX_DAILY_MOVE_PCT}%")
    return problems


def fetch_history(ticker: str, years: int = 2,
                  tolerance_pct: float = 1.0) -> tuple[pd.DataFrame, CrossCheck]:
    """Fetch a ticker's history from yfinance, cross-checked against an independent
    reference source when one is reachable.

    Raises RuntimeError if the primary source is invalid and no valid fallback
    is available.
    """
    yf_df = fetch_yfinance(ticker, years)
    ref_df = fetch_reference(ticker, years)

    cc = cross_check(ticker, yf_df, ref_df, tolerance_pct)

    primary_problems = validate_history(yf_df)
    if not primary_problems:
        chosen = yf_df
        if cc.diff_pct is not None and not cc.within_tolerance:
            log.warning("%s cross-check: %s", ticker, cc.note)
    else:
        log.warning("%s yfinance invalid (%s); trying reference source",
                    ticker, "; ".join(primary_problems))
        fallback_problems = validate_history(ref_df)
        if fallback_problems:
            raise RuntimeError(
                f"{ticker}: primary source invalid and no valid fallback. "
                f"yfinance: {primary_problems}; reference: {fallback_problems}"
            )
        chosen = ref_df

    return chosen.sort_values("date").reset_index(drop=True), cc


def to_price_rows(ticker: str, df: pd.DataFrame) -> list[dict]:
    """Convert a fetched DataFrame into db.upsert_prices row dicts."""
    rows = []
    for _, r in df.iterrows():
        rows.append(dict(
            ticker=ticker.upper(), date=r["date"],
            open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
            close=float(r["close"]), adj_close=float(r["adj_close"]),
            volume=int(r["volume"]), source=r["source"],
        ))
    return rows
