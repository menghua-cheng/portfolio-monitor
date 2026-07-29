"""Switchable entry/exit signal rules for the backtest (feature: 可切換訊號).

The backtest engine used to hard-code one signal family — the degree-N MA-cross
cascade. This module turns that into a *registry*: a rule is a named function
that, given a bar frame and a direction, returns a per-bar boolean
"condition holds here" mask. The engine takes the **rising edge** of that mask
as the trigger, so every rule fires exactly on the bar the condition becomes
true and never re-fires while it stays true.

A strategy is then just a pair of rules — `(entry rule, exit rule)` — and the
grid the backtest sweeps is the cartesian product of the requested entry and
exit rules. Entry rules are evaluated in the "up" direction, exit rules in
"down", so one rule family covers both sides.

Rule families (spec syntax as accepted on the CLI):

    degree2              cascade: fastest 2 adjacent MA pairs both crossed up
                         within the lookback window, in any order (ADR-0001)
    multi2               雙重突破／跌破: *any* 2 distinct adjacent pairs crossed the
                         same direction within the window — the daily report's own
                         多重突破 counting, deliberately looser than degree (ADR-0006)
    cross:sma20/sma60    one MA pair: short above (up) / below (down) long
    price:sma20          adjusted close above (up) / below (down) an MA
    align                full MA stacking: 多頭排列 (up) / 空頭排列 (down)
    slope:sma240         an MA's own slope over `slope_lookback` bars

Numbers may be written bare (`cross:20/60`, `price:20`) and default to `sma`.
`multi2/3/4` also answer to `double`/`triple`/`quad`. Group tokens expand to
several rules: `degrees`, `multis`, `crosses`, `prices`, `slopes`, `all`.

This module is pure: DataFrames in, numpy masks out. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .signals import _cross_state

# --------------------------------------------------------------------------- #
# Specs and context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleSpec:
    """One signal rule: a registry `kind` plus its parameters.

    Frozen and hashable so specs can key dicts and sit in tuples on a
    BacktestSpec. `label` is the canonical round-trippable text form — what the
    CLI accepts and what the report prints.
    """
    kind: str
    params: tuple[tuple[str, object], ...] = ()

    @classmethod
    def of(cls, kind: str, **params) -> "RuleSpec":
        return cls(kind, tuple(sorted(params.items())))

    @property
    def p(self) -> dict:
        return dict(self.params)

    @property
    def label(self) -> str:
        p = self.p
        if self.kind in ("degree", "multi"):
            return f"{self.kind}{p['n']}"
        if self.kind == "cross":
            return f"cross:{p['short']}/{p['long']}"
        if self.kind == "price":
            return f"price:{p['ma']}"
        if self.kind == "slope":
            return f"slope:{p['ma']}"
        return self.kind

    def __str__(self) -> str:      # so f-strings and logs read naturally
        return self.label


@dataclass
class RuleContext:
    """Everything a rule needs beyond the bar frame itself.

    `pairs` is the adjacent-MA ladder in fast→slow order, which is what gives
    "degree" its meaning: degree N = the fastest N pairs. It is derived from the
    active MA period ladder, so a weekly/monthly backtest with a different
    ladder still has a coherent degree notion.

    **Scope invariant:** a RuleContext is created per backtest run and used with
    exactly one bar frame. `memo` relies on that — it caches per-(pair, direction)
    cross dates and window masks, which is what stops a 16-cell grid from
    recomputing the same four pair masks sixteen times over. Reuse one context
    across different frames and you will get another frame's masks; build a fresh
    one instead (it is cheap), or call `reset_memo()`.
    """
    pairs: list[tuple[str, str]] = field(default_factory=list)
    ma_cols: list[str] = field(default_factory=list)
    window_days: int = 30
    slope_lookback: int = 10
    flat_threshold_pct: float = 0.5
    memo: dict = field(default_factory=dict, repr=False, compare=False)

    def reset_memo(self) -> None:
        self.memo.clear()


def adjacent_pairs(periods, kind: str = "sma") -> list[tuple[str, str]]:
    """Adjacent (fast, slow) MA column pairs for a period ladder.

    [5, 20, 60] -> [("sma5","sma20"), ("sma20","sma60")]. Sorted ascending so a
    caller may pass the ladder in any order and still get fast→slow pairs.
    """
    cols = [f"{kind}{p}" for p in sorted(int(p) for p in periods)]
    return list(zip(cols[:-1], cols[1:]))


# --------------------------------------------------------------------------- #
# Rule implementations — each returns a per-bar boolean "condition holds" mask
# --------------------------------------------------------------------------- #
def _pair_cross_dates(df: pd.DataFrame, short: str, long: str, direction: str) -> list:
    """Dates on which `short` crossed `direction` through `long`."""
    if short not in df.columns or long not in df.columns:
        return []
    state = _cross_state(df[short], df[long])
    out = []
    for i in range(1, len(state)):
        prev, curr = state.iloc[i - 1], state.iloc[i]
        if pd.notna(prev) and pd.notna(curr) and prev != curr:
            if ("up" if curr > prev else "down") == direction:
                out.append(df["date"].iloc[i])
    return out


_NS_PER_DAY = 86_400_000_000_000


def _in_trailing_window(cross_dates: list, bar_dates: np.ndarray,
                        window_days: int) -> np.ndarray:
    """Per-bar: did one of `cross_dates` land 0..window_days calendar days
    on or before this bar?

    Vectorized with searchsorted over the sorted cross dates: for each bar we ask
    how many crosses fall in the half-open window and test the count. The obvious
    per-bar Python loop is the same answer but O(bars x crosses) with a Timestamp
    conversion inside, and this is the hottest path in the whole backtest — the
    grid asks for it once per (pair, direction) and the bar count runs to
    thousands.
    """
    out = np.zeros(len(bar_dates), dtype=bool)
    if not cross_dates or len(bar_dates) == 0:
        return out
    cd = np.sort(pd.to_datetime(pd.Series(list(cross_dates))).to_numpy()
                 .astype("datetime64[ns]").astype("int64"))
    bars = pd.to_datetime(pd.Series(bar_dates)).to_numpy().astype("datetime64[ns]").astype("int64")
    lo = bars - window_days * _NS_PER_DAY
    # crosses in (lo - epsilon, bars]: right-search the upper bound, left-search
    # the lower, and a non-empty span means at least one cross is in range.
    hi_idx = np.searchsorted(cd, bars, side="right")
    lo_idx = np.searchsorted(cd, lo, side="left")
    return hi_idx > lo_idx


def _pair_window_mask(df: pd.DataFrame, short: str, long: str, direction: str,
                      ctx: RuleContext) -> np.ndarray:
    """Memoized per-(pair, direction) "crossed within the window" mask.

    Every degree and multi cell in a grid needs the same handful of these, so
    computing them once per run instead of once per cell is most of the backtest's
    cost. See the scope invariant on RuleContext.
    """
    key = (short, long, direction, ctx.window_days)
    hit = ctx.memo.get(key)
    if hit is None:
        bar_dates = ctx.memo.get("_bars")
        if bar_dates is None:
            bar_dates = df["date"].to_numpy()
            ctx.memo["_bars"] = bar_dates
        dates = _pair_cross_dates(df, short, long, direction)
        hit = _in_trailing_window(dates, bar_dates, ctx.window_days)
        ctx.memo[key] = hit
    return hit


def _rule_degree(df: pd.DataFrame, direction: str, ctx: RuleContext, *, n: int) -> np.ndarray:
    """Degree-N cascade (ADR-0001): every one of the fastest N adjacent pairs has
    crossed `direction` within the trailing window, in any order."""
    n = int(n)
    if n < 1 or n > len(ctx.pairs):
        return np.zeros(len(df), dtype=bool)
    confirmed = np.ones(len(df), dtype=bool)
    for short, long in ctx.pairs[:n]:
        confirmed = confirmed & _pair_window_mask(df, short, long, direction, ctx)
    return confirmed


def _rule_multi(df: pd.DataFrame, direction: str, ctx: RuleContext, *, n: int) -> np.ndarray:
    """雙重／三重／四重突破・跌破: *any* N distinct adjacent pairs crossed
    `direction` within the trailing window.

    This is the counting the daily report's trend summary already shows
    (`signals.summarize_trend` tags 雙重/三重/四重突破), and it is deliberately
    **looser than degree**: degree N demands the fastest N pairs specifically,
    cumulative from the short end, whereas multi N accepts any N pairs. A
    breakout that starts at the slow end of the stack counts here and does not
    count as a degree cross at all (ADR-0001, ADR-0006).
    """
    n = int(n)
    if n < 1 or not ctx.pairs:
        return np.zeros(len(df), dtype=bool)
    counts = np.zeros(len(df), dtype=int)
    for short, long in ctx.pairs:
        counts += _pair_window_mask(df, short, long, direction, ctx).astype(int)
    return counts >= n


def _rule_cross(df: pd.DataFrame, direction: str, ctx: RuleContext, *,
                short: str, long: str) -> np.ndarray:
    """One MA pair's *state*: short above long (up) or below (down). The engine's
    rising edge of this mask is the crossing itself (a golden/death cross)."""
    if short not in df.columns or long not in df.columns:
        return np.zeros(len(df), dtype=bool)
    state = _cross_state(df[short], df[long]).to_numpy()
    return (state > 0) if direction == "up" else (state < 0)


def _rule_price(df: pd.DataFrame, direction: str, ctx: RuleContext, *, ma: str) -> np.ndarray:
    """Adjusted close above (up) / below (down) one MA.

    Reads the `adj_c` column the engine's `_prepare` adds; falls back to raw
    `close` so the rule still works on a plain price frame.
    """
    price_col = "adj_c" if "adj_c" in df.columns else "close"
    if ma not in df.columns or price_col not in df.columns:
        return np.zeros(len(df), dtype=bool)
    price = df[price_col].to_numpy(dtype=float)
    line = df[ma].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        above = price >= line
    warm = ~np.isnan(line)
    return (above & warm) if direction == "up" else (~above & warm)


def _rule_align(df: pd.DataFrame, direction: str, ctx: RuleContext) -> np.ndarray:
    """Full MA stacking: 多頭排列 (fast>…>slow, up) or 空頭排列 (the reverse, down).
    Bars where any MA in the ladder is unwarmed are False."""
    cols = [c for c in ctx.ma_cols if c in df.columns]
    if len(cols) < 2:
        return np.zeros(len(df), dtype=bool)
    vals = df[cols].to_numpy(dtype=float)          # columns already fast→slow
    warm = ~np.isnan(vals).any(axis=1)
    with np.errstate(invalid="ignore"):
        diffs = np.diff(vals, axis=1)
        ordered = (diffs < 0).all(axis=1) if direction == "up" else (diffs > 0).all(axis=1)
    return ordered & warm


def _rule_slope(df: pd.DataFrame, direction: str, ctx: RuleContext, *, ma: str) -> np.ndarray:
    """An MA's own slope over `slope_lookback` bars, in percent, compared against
    `flat_threshold_pct`. "Buy when the yearly line turns up" is slope:sma240."""
    if ma not in df.columns:
        return np.zeros(len(df), dtype=bool)
    s = df[ma].to_numpy(dtype=float)
    lb = max(1, int(ctx.slope_lookback))
    out = np.zeros(len(df), dtype=bool)
    if len(s) <= lb:
        return out
    now, then = s[lb:], s[:-lb]
    with np.errstate(invalid="ignore", divide="ignore"):
        pct = (now - then) / np.abs(then) * 100.0
    ok = np.isfinite(pct)
    hit = (pct > ctx.flat_threshold_pct) if direction == "up" else (pct < -ctx.flat_threshold_pct)
    out[lb:] = hit & ok
    return out


_REGISTRY = {
    "degree": _rule_degree,
    "multi": _rule_multi,
    "cross": _rule_cross,
    "price": _rule_price,
    "align": _rule_align,
    "slope": _rule_slope,
}

RULE_HELP = {
    "degree": "degreeN — fastest N adjacent MA pairs all crossed within the window",
    "multi":  "multiN — ANY N distinct adjacent pairs crossed within the window "
              "(雙重/三重/四重突破・跌破; aliases double/triple/quad)",
    "cross":  "cross:SHORT/LONG — one MA pair crossing (e.g. cross:sma20/sma60)",
    "price":  "price:MA — adjusted close crossing one MA (e.g. price:sma20)",
    "align":  "align — full MA stacking flips to 多頭排列 / 空頭排列",
    "slope":  "slope:MA — that MA's own slope turns up / down",
}

# Bilingual display names for the multi-break degrees, matching the report's tags.
MULTI_NAMES = {
    2: ("Double breakout / breakdown", "雙重突破／跌破"),
    3: ("Triple breakout / breakdown", "三重突破／跌破"),
    4: ("Quadruple breakout / breakdown", "四重突破／跌破"),
}
# Word aliases so `--entry double` works as well as `--entry multi2`.
_MULTI_ALIASES = {"double": 2, "dual": 2, "triple": 3,
                  "quad": 4, "quadruple": 4}


def confirm(df: pd.DataFrame, spec: RuleSpec, direction: str,
            ctx: RuleContext) -> np.ndarray:
    """Per-bar mask for one rule in one direction. Raises on an unknown kind."""
    fn = _REGISTRY.get(spec.kind)
    if fn is None:
        raise ValueError(f"unknown rule kind: {spec.kind!r}")
    return fn(df, direction, ctx, **spec.p)


# --------------------------------------------------------------------------- #
# Parsing / expansion
# --------------------------------------------------------------------------- #
def _ma_name(token: str) -> str:
    """`20` -> `sma20`; `ema20` -> `ema20`. Bare numbers default to SMA."""
    t = token.strip().lower()
    if not t:
        raise ValueError("empty moving-average name")
    if t.isdigit():
        return f"sma{t}"
    if t.startswith(("sma", "ema")) and t[3:].isdigit():
        return t
    raise ValueError(f"not a moving-average name: {token!r}")


def parse_rule(text: str) -> RuleSpec:
    """Parse one rule spec. Accepts `degree2`/`degree:2`, `cross:20/60`,
    `price:sma20`, `slope:240`, `align`."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty rule spec")
    if t == "align":
        return RuleSpec.of("align")
    if t in _MULTI_ALIASES:
        return RuleSpec.of("multi", n=_MULTI_ALIASES[t])
    for counted in ("degree", "multi"):
        if t.startswith(counted):
            arg = t[len(counted):].lstrip(":")
            if not arg.isdigit():
                raise ValueError(f"{counted} needs a number, got {text!r}")
            return RuleSpec.of(counted, n=int(arg))
    kind, _, arg = t.partition(":")
    if kind not in _REGISTRY:
        raise ValueError(f"unknown rule {kind!r}; known: {', '.join(sorted(_REGISTRY))}, "
                         f"plus the groups degrees, crosses, prices, slopes, all")
    if not arg:
        raise ValueError(f"rule {kind!r} needs an argument — {RULE_HELP[kind]}")
    if kind == "cross":
        short, _, long = arg.partition("/")
        if not long:
            raise ValueError(f"cross needs SHORT/LONG, got {text!r}")
        return RuleSpec.of("cross", short=_ma_name(short), long=_ma_name(long))
    if kind in ("price", "slope"):
        return RuleSpec.of(kind, ma=_ma_name(arg))
    raise ValueError(f"unknown rule {kind!r}; known: {', '.join(sorted(_REGISTRY))}")


def parse_rules(text: str, ctx: RuleContext) -> list[RuleSpec]:
    """Parse a comma-separated rule list, expanding the group tokens
    `degrees`, `crosses`, `prices`, `slopes` and `all` against the active ladder.
    Order is preserved and duplicates are dropped."""
    out: list[RuleSpec] = []

    def add(spec: RuleSpec) -> None:
        if spec not in out:
            out.append(spec)

    for raw in text.split(","):
        tok = raw.strip().lower()
        if not tok:
            continue
        if tok in ("degrees", "all"):
            for n in range(1, len(ctx.pairs) + 1):
                add(RuleSpec.of("degree", n=n))
        if tok in ("multis", "all"):
            # multi1 == "any single pair crossed", which is a strictly weaker
            # restatement of the ladder having any cross at all; the report only
            # ever tags 雙重 and up, so the group starts at 2.
            for n in range(2, len(ctx.pairs) + 1):
                add(RuleSpec.of("multi", n=n))
        if tok in ("crosses", "all"):
            for short, long in ctx.pairs:
                add(RuleSpec.of("cross", short=short, long=long))
        if tok in ("prices", "all"):
            for ma in ctx.ma_cols:
                add(RuleSpec.of("price", ma=ma))
        if tok in ("slopes", "all"):
            for ma in ctx.ma_cols:
                add(RuleSpec.of("slope", ma=ma))
        if tok == "all":
            add(RuleSpec.of("align"))
        if tok in ("degrees", "multis", "crosses", "prices", "slopes", "all"):
            continue
        add(parse_rule(tok))
    if not out:
        raise ValueError("no rules parsed")
    return out
