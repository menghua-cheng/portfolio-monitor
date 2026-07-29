"""Daily portfolio performance + signal tracker (feature: 績效與訊號追蹤).

The daily report answers "what happened today, per ticker". This module answers
the two questions that only make sense over time:

1. **Performance** — how is each holding, and the watchlist as a whole, doing over
   1 day / 1 week / 1 month / 3 months / 6 months / 1 year / YTD, and how far is
   each off its 52-week high?
2. **Signal tracking** — every signal the pipeline has recorded, what the price
   has done *since* it fired, and therefore how often this ticker's signals have
   actually pointed the right way.

Both are computed from the cached `prices` and `signals` tables — nothing new is
persisted, because both are fully derivable from what is already stored (the same
reasoning as ADR-0003). Returns use **adjusted** closes, so they are total
returns and a split or dividend never shows up as performance.

The portfolio is treated as **equal-weight, rebalanced at the start of each
horizon**: its return over a horizon is the mean of the per-ticker returns over
that horizon, across the tickers that have data at both ends. The watchlist
carries no share counts, so equal weight is the only honest reading; a ticker
with too little history is excluded from that horizon rather than assumed flat.

This module is pure: rows/frames in, dataclasses out. No I/O, no rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Horizon key -> (calendar days back, en label, zh label). None = year-to-date.
HORIZONS: list[tuple[str, int | None, str, str]] = [
    ("1d", 1, "1D", "單日"),
    ("1w", 7, "1W", "一週"),
    ("1m", 30, "1M", "一月"),
    ("3m", 91, "3M", "三月"),
    ("6m", 182, "6M", "半年"),
    ("1y", 365, "1Y", "一年"),
    ("ytd", None, "YTD", "年初至今"),
]

# Signal families, mapped to the direction they claim. Anything unmapped is
# tracked but never counted toward the hit rate — a state label like "unknown"
# makes no directional claim, so scoring it would invent an opinion.
_UP_SIGNALS = {"GOLDEN_CROSS", "ALIGNED_UP", "LONG_DOWN_SHORT_BREAKOUT"}
_DOWN_SIGNALS = {"DEATH_CROSS", "ALIGNED_DOWN", "LONG_UP_SHORT_DOWN"}

SIGNAL_LABELS_EN = {
    "GOLDEN_CROSS": "Golden cross (monthly above quarterly)",
    "DEATH_CROSS": "Death cross (monthly below quarterly)",
    "ALIGNED_UP": "Aligned up",
    "ALIGNED_DOWN": "Aligned down",
    "LONG_UP_SHORT_DOWN": "Long-term up, short-term pullback",
    "LONG_DOWN_SHORT_BREAKOUT": "Long-term down, short-term breakout",
}
SIGNAL_LABELS_ZH = {
    "GOLDEN_CROSS": "黃金交叉（月線上穿季線）",
    "DEATH_CROSS": "死亡交叉（月線下破季線）",
    "ALIGNED_UP": "多頭同向",
    "ALIGNED_DOWN": "空頭同向",
    "LONG_UP_SHORT_DOWN": "長多短空（回檔）",
    "LONG_DOWN_SHORT_BREAKOUT": "長空短多（反攻）",
}


def signal_direction(signal_type: str) -> str:
    """"up" | "down" | "neutral" — the directional claim a signal type makes."""
    if signal_type.startswith("CROSS_UP_") or signal_type in _UP_SIGNALS:
        return "up"
    if signal_type.startswith("CROSS_DOWN_") or signal_type in _DOWN_SIGNALS:
        return "down"
    return "neutral"


def signal_labels(signal_type: str, detail: str = "") -> tuple[str, str]:
    """(en, zh) display labels. Granular cross signals carry their own bilingual
    text in `detail` (built by signals.detect_cross_events), so prefer that."""
    if signal_type in SIGNAL_LABELS_EN:
        return SIGNAL_LABELS_EN[signal_type], SIGNAL_LABELS_ZH[signal_type]
    if detail:
        # detail is "<zh label> | <zh note>" for cross events; keep the label part.
        zh = detail.split(" | ")[0]
        return _cross_label_en(signal_type), zh
    return signal_type, signal_type


_MA_EN = {"sma5": "Weekly", "sma20": "Monthly", "sma60": "Quarterly",
          "sma120": "Half-year", "sma240": "Yearly"}


def _cross_label_en(signal_type: str) -> str:
    """"CROSS_UP_sma5_sma20" -> "Weekly line breaks above Monthly line"."""
    parts = signal_type.split("_")
    if len(parts) < 4:
        return signal_type
    direction = "breaks above" if parts[1] == "UP" else "breaks below"
    short, long = parts[2], parts[3]
    return f"{_MA_EN.get(short, short)} line {direction} {_MA_EN.get(long, long)} line"


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class TickerPerf:
    symbol: str
    name: str
    close: float | None                      # raw close, what a quote screen shows
    returns: dict[str, float | None] = field(default_factory=dict)  # horizon -> fraction
    high_52w: float | None = None
    off_high_pct: float | None = None        # negative fraction below the 52w high
    first_date: str | None = None
    last_date: str | None = None
    bars: int = 0


@dataclass
class PortfolioPerf:
    returns: dict[str, float | None] = field(default_factory=dict)
    counted: dict[str, int] = field(default_factory=dict)   # tickers per horizon
    best: tuple[str, float] | None = None                   # (symbol, 1d fraction)
    worst: tuple[str, float] | None = None
    index_series: list[tuple[str, float]] = field(default_factory=list)  # (date, =100 at start)
    index_members: int = 0        # tickers that covered the whole index window


@dataclass
class SignalHit:
    ticker: str
    date: str
    signal_type: str
    label_en: str
    label_zh: str
    direction: str                 # up | down | neutral
    days_ago: int
    price_at_signal: float | None
    forward_pct: float | None      # adjusted return from the signal bar to the last bar
    correct: bool | None           # None when the signal makes no directional claim


@dataclass
class SignalScore:
    total: int = 0
    scored: int = 0                # directional signals only
    correct: int = 0
    up_total: int = 0
    up_correct: int = 0
    down_total: int = 0
    down_correct: int = 0

    @property
    def hit_rate(self) -> float | None:
        return self.correct / self.scored if self.scored else None

    @property
    def up_hit_rate(self) -> float | None:
        return self.up_correct / self.up_total if self.up_total else None

    @property
    def down_hit_rate(self) -> float | None:
        return self.down_correct / self.down_total if self.down_total else None


@dataclass
class TrackerReport:
    as_of: str
    lookback_days: int
    tickers: list[TickerPerf] = field(default_factory=list)
    portfolio: PortfolioPerf = field(default_factory=PortfolioPerf)
    signals: list[SignalHit] = field(default_factory=list)
    score: SignalScore = field(default_factory=SignalScore)
    per_ticker_score: dict[str, SignalScore] = field(default_factory=dict)
    note: str = ""


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def _as_of(prices: dict[str, pd.DataFrame]) -> str | None:
    """The newest bar across all tickers — the report's own "today"."""
    dates = [df["date"].iloc[-1] for df in prices.values() if not df.empty]
    return max(dates) if dates else None


def _value_on_or_before(df: pd.DataFrame, cutoff: str, col: str = "adj_close"):
    """The last value at or before `cutoff`, or None if the series starts later.

    Signals and horizon boundaries land on calendar dates that are often not
    trading days, so every lookup snaps backwards to the most recent real bar.
    """
    mask = (df["date"] <= cutoff).to_numpy()
    if not mask.any():
        return None
    idx = int(np.flatnonzero(mask)[-1])
    val = df[col].to_numpy(dtype=float)[idx]
    return None if pd.isna(val) else float(val)


def _horizon_start(as_of: str, days: int | None) -> str:
    if days is None:                                  # YTD
        return f"{pd.Timestamp(as_of).year}-01-01"
    return (pd.Timestamp(as_of) - pd.Timedelta(days=days)).date().isoformat()


def _return_over(df: pd.DataFrame, as_of: str, days: int | None) -> float | None:
    """Adjusted total return from the last bar before the horizon start to `as_of`.

    Anchoring on the last bar *before* the start (rather than the first bar after
    it) is what makes 1d mean "since the previous close" and YTD mean "since last
    year's final close" — the conventional readings.
    """
    end = _value_on_or_before(df, as_of)
    if end is None or end <= 0:
        return None
    start_cut = _horizon_start(as_of, days)
    prior = df[df["date"] < start_cut]
    if prior.empty:
        return None                                   # not enough history: excluded
    base = float(prior["adj_close"].iloc[-1])
    if base <= 0:
        return None
    return end / base - 1.0


def ticker_performance(symbol: str, name: str, df: pd.DataFrame,
                       as_of: str) -> TickerPerf:
    """Horizon returns, 52-week high and distance off it, for one ticker."""
    if df.empty:
        return TickerPerf(symbol, name, None)
    df = df.sort_values("date").reset_index(drop=True)
    returns = {key: _return_over(df, as_of, days) for key, days, _, _ in HORIZONS}

    year_ago = (pd.Timestamp(as_of) - pd.Timedelta(days=365)).date().isoformat()
    window = df[df["date"] >= year_ago]
    high = float(window["adj_close"].astype(float).max()) if not window.empty else None
    last_adj = _value_on_or_before(df, as_of)
    off_high = (last_adj / high - 1.0) if (high and last_adj and high > 0) else None

    return TickerPerf(
        symbol=symbol, name=name,
        close=float(df["close"].iloc[-1]), returns=returns,
        high_52w=high, off_high_pct=off_high,
        first_date=str(df["date"].iloc[0]), last_date=str(df["date"].iloc[-1]),
        bars=len(df),
    )


def portfolio_performance(perfs: list[TickerPerf], prices: dict[str, pd.DataFrame],
                          as_of: str, index_days: int = 180) -> PortfolioPerf:
    """Equal-weight portfolio returns per horizon, plus a normalized index series.

    Each horizon averages only the tickers that have data at both ends, and
    reports how many that was — an average over a shifting membership is a lie if
    the membership isn't shown.
    """
    out = PortfolioPerf()
    for key, _days, _en, _zh in HORIZONS:
        vals = [p.returns.get(key) for p in perfs]
        vals = [v for v in vals if v is not None]
        out.returns[key] = sum(vals) / len(vals) if vals else None
        out.counted[key] = len(vals)

    day = [(p.symbol, p.returns.get("1d")) for p in perfs if p.returns.get("1d") is not None]
    if day:
        out.best = max(day, key=lambda x: x[1])
        out.worst = min(day, key=lambda x: x[1])

    out.index_series, out.index_members = _index_series(prices, as_of, index_days)
    return out


def _index_series(prices: dict[str, pd.DataFrame], as_of: str,
                  days: int) -> tuple[list[tuple[str, float]], int]:
    """Equal-weight index (=100 at the window start) over the last `days`.

    Only tickers that cover the **whole** window take part. That filter has to
    happen before normalizing: intersecting dates afterwards instead would keep a
    late-listing constituent, shrink the index window to whatever every ticker
    happens to share, and leave each series normalized against a base outside the
    surviving window — an index that is neither the requested window nor
    consistently based. Returns (series, constituent count).
    """
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=days)).date().isoformat()
    windows = {}
    for sym, df in prices.items():
        if df.empty:
            continue
        w = df[(df["date"] >= start) & (df["date"] <= as_of)]
        if len(w) >= 2 and float(w["adj_close"].iloc[0]) > 0:
            windows[sym] = w
    if not windows:
        return [], 0

    # The window the index actually covers is the earliest start any ticker
    # reaches; tickers that start later are dropped, not allowed to truncate it.
    target_start = min(str(w["date"].iloc[0]) for w in windows.values())
    series = []
    for w in windows.values():
        if str(w["date"].iloc[0]) != target_start:
            continue
        base = float(w["adj_close"].iloc[0])
        series.append(pd.Series((w["adj_close"].astype(float) / base).to_numpy(),
                                index=w["date"].to_numpy()))
    if not series:
        return [], 0
    frame = pd.concat(series, axis=1).dropna()   # align on shared trading days
    if frame.empty:
        return [], 0
    idx = frame.mean(axis=1) * 100.0
    return [(str(d), float(v)) for d, v in idx.items()], len(series)


# --------------------------------------------------------------------------- #
# Signal tracking
# --------------------------------------------------------------------------- #
def track_signals(signal_rows, prices: dict[str, pd.DataFrame], as_of: str,
                  lookback_days: int = 90) -> tuple[list[SignalHit], SignalScore,
                                                    dict[str, SignalScore]]:
    """Score every recorded signal in the lookback window by what price did after it.

    "Correct" means an up-signal was followed by a gain, or a down-signal by a
    fall, measured on adjusted closes from the signal bar to the latest bar. This
    is a *tracking* measure, not a backtest: it has no entries, exits, costs or
    position sizing, and every signal is measured to the same right-hand edge, so
    older signals get a longer runway. Read it as "has this ticker's signal flow
    been pointing the right way lately", and use the backtest for tradability.
    """
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=lookback_days)).date().isoformat()
    hits: list[SignalHit] = []
    overall = SignalScore()
    per: dict[str, SignalScore] = {}

    for row in signal_rows:
        ticker = str(row["ticker"]).upper()
        sdate = str(row["date"])
        if sdate < start or sdate > as_of:
            continue
        df = prices.get(ticker)
        stype = str(row["signal_type"])
        detail = str(row["detail"] or "")
        direction = signal_direction(stype)
        label_en, label_zh = signal_labels(stype, detail)

        price_at = forward = None
        correct: bool | None = None
        if df is not None and not df.empty:
            at = _value_on_or_before(df, sdate)
            end = _value_on_or_before(df, as_of)
            raw_at = _value_on_or_before(df, sdate, col="close")
            price_at = raw_at
            if at and end and at > 0:
                forward = end / at - 1.0
                if direction == "up":
                    correct = forward > 0
                elif direction == "down":
                    correct = forward < 0

        hits.append(SignalHit(
            ticker=ticker, date=sdate, signal_type=stype,
            label_en=label_en, label_zh=label_zh, direction=direction,
            days_ago=(pd.Timestamp(as_of) - pd.Timestamp(sdate)).days,
            price_at_signal=price_at, forward_pct=forward, correct=correct))

        score = per.setdefault(ticker, SignalScore())
        for s in (overall, score):
            s.total += 1
            if correct is None:
                continue
            s.scored += 1
            s.correct += int(correct)
            if direction == "up":
                s.up_total += 1
                s.up_correct += int(correct)
            else:
                s.down_total += 1
                s.down_correct += int(correct)

    hits.sort(key=lambda h: (h.days_ago, h.ticker, h.signal_type))
    return hits, overall, per


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_report(prices: dict[str, pd.DataFrame], names: dict[str, str],
                 signal_rows, lookback_days: int = 90,
                 index_days: int = 180, as_of: str | None = None) -> TrackerReport:
    """Assemble the whole tracker from cached prices, watchlist names and signals."""
    prices = {k.upper(): (v.sort_values("date").reset_index(drop=True) if not v.empty else v)
              for k, v in prices.items()}
    as_of = as_of or _as_of(prices)
    if as_of is None:
        return TrackerReport(as_of="", lookback_days=lookback_days,
                             note="no cached prices — run the pipeline or "
                                  "`portfolio-monitor-cache sync`")

    perfs = [ticker_performance(sym, names.get(sym, ""), df, as_of)
             for sym, df in sorted(prices.items())]
    portfolio = portfolio_performance(perfs, prices, as_of, index_days)
    hits, score, per = track_signals(signal_rows, prices, as_of, lookback_days)

    stale = [p.symbol for p in perfs if p.last_date and p.last_date < as_of]
    note = f"stale (no bar on {as_of}): {', '.join(stale)}" if stale else ""
    return TrackerReport(as_of=as_of, lookback_days=lookback_days, tickers=perfs,
                         portfolio=portfolio, signals=hits, score=score,
                         per_ticker_score=per, note=note)
