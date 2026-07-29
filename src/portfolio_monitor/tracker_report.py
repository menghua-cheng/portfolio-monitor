"""Rendering for the performance + signal tracker (HTML and terminal).

Mirrors `report.py`'s conventions: all numbers are preformatted here so the Jinja
template stays dumb, and every user-facing string carries both English and
繁體中文 for the same EN/中文 switcher the daily report uses.

The portfolio index is drawn as an **inline SVG sparkline** rather than a Plotly
figure: the tracker is a small, self-contained artifact that must also survive
being read in an email client, where no JavaScript runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .tracker import HORIZONS, SignalScore, TrackerReport

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

UP_FG, DOWN_FG, FLAT_FG = "#0a8f52", "#c0392b", "#66707a"


def _pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def _pct_plain(x: float | None, digits: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _fg(x: float | None) -> str:
    if x is None:
        return FLAT_FG
    return UP_FG if x > 0 else (DOWN_FG if x < 0 else FLAT_FG)


@dataclass
class Cell:
    text: str
    fg: str = FLAT_FG


@dataclass
class PerfRow:
    symbol: str
    name: str
    close: str
    cells: list[Cell] = field(default_factory=list)     # one per HORIZONS entry
    off_high: Cell = field(default_factory=lambda: Cell("—"))
    coverage: str = ""                                   # e.g. "512 bars from 2024-07-12"


@dataclass
class SignalRow:
    ticker: str
    date: str
    days_ago: str
    days_ago_zh: str
    label_en: str
    label_zh: str
    dir_en: str
    dir_zh: str
    dir_fg: str
    price: str
    forward: Cell
    verdict_en: str
    verdict_zh: str
    verdict_fg: str


@dataclass
class ScoreView:
    scope_en: str
    scope_zh: str
    hit_rate: str
    detail_en: str
    detail_zh: str
    fg: str


@dataclass
class TrackerView:
    as_of: str
    lookback_days: int
    horizons: list[tuple[str, str]]        # (en, zh) headers
    rows: list[PerfRow] = field(default_factory=list)
    portfolio: PerfRow | None = None
    portfolio_counts: str = ""
    best: str = ""
    worst: str = ""
    portfolio_counts_zh: str = ""
    sparkline: str = ""                     # inline SVG
    spark_range: str = ""
    spark_range_zh: str = ""
    signals: list[SignalRow] = field(default_factory=list)
    scores: list[ScoreView] = field(default_factory=list)
    note: str = ""
    empty: bool = False


def sparkline_svg(series: list[tuple[str, float]], width: int = 780,
                  height: int = 64) -> str:
    """A dependency-free SVG line of the equal-weight index.

    Returns "" for a series too short to draw. The 100 baseline is drawn so the
    reader can see at a glance whether the window is up or down overall.
    """
    if len(series) < 2:
        return ""
    vals = [v for _d, v in series]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 6
    inner_h = height - 2 * pad

    def x(i):
        return pad + i * (width - 2 * pad) / (len(vals) - 1)

    def y(v):
        return pad + (hi - v) * inner_h / span

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    up = vals[-1] >= vals[0]
    stroke = UP_FG if up else DOWN_FG
    base = ""
    if lo <= 100.0 <= hi:
        by = y(100.0)
        base = (f'<line x1="{pad}" y1="{by:.1f}" x2="{width - pad}" y2="{by:.1f}" '
                f'stroke="#c9d2da" stroke-width="1" stroke-dasharray="3 3"/>')
    area = (f"{pad},{height - pad} " + pts + f" {width - pad},{height - pad}")
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" preserveAspectRatio="none">'
            f'<polygon points="{area}" fill="{stroke}" opacity="0.08"/>'
            f'{base}'
            f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
            f'stroke-width="1.8" stroke-linejoin="round"/></svg>')


def _score_view(scope_en: str, scope_zh: str, s: SignalScore) -> ScoreView:
    rate = s.hit_rate
    return ScoreView(
        scope_en=scope_en, scope_zh=scope_zh,
        hit_rate=_pct_plain(rate) if rate is not None else "—",
        detail_en=(f"{s.correct}/{s.scored} directional of {s.total} signals · "
                   f"up {_pct_plain(s.up_hit_rate)} ({s.up_total}) · "
                   f"down {_pct_plain(s.down_hit_rate)} ({s.down_total})"),
        detail_zh=(f"{s.total} 個訊號中 {s.scored} 個具方向性，命中 {s.correct} 個 · "
                   f"看多 {_pct_plain(s.up_hit_rate)}（{s.up_total}）· "
                   f"看空 {_pct_plain(s.down_hit_rate)}（{s.down_total}）"),
        fg=UP_FG if (rate is not None and rate >= 0.5) else
           (DOWN_FG if rate is not None else FLAT_FG),
    )


def build_view(rep: TrackerReport) -> TrackerView:
    """Map the computed tracker onto display-ready strings."""
    view = TrackerView(as_of=rep.as_of, lookback_days=rep.lookback_days,
                       horizons=[(en, zh) for _k, _d, en, zh in HORIZONS],
                       note=rep.note)
    if not rep.as_of:
        view.empty = True
        return view

    for p in rep.tickers:
        view.rows.append(PerfRow(
            symbol=p.symbol, name=p.name,
            close="—" if p.close is None else f"{p.close:.2f}",
            cells=[Cell(_pct(p.returns.get(k)), _fg(p.returns.get(k)))
                   for k, _d, _en, _zh in HORIZONS],
            off_high=Cell(_pct(p.off_high_pct), _fg(p.off_high_pct)),
            coverage=f"{p.bars} bars from {p.first_date}" if p.first_date else "",
        ))

    pf = rep.portfolio
    view.portfolio = PerfRow(
        symbol="PORTFOLIO", name="", close="",
        cells=[Cell(_pct(pf.returns.get(k)), _fg(pf.returns.get(k)))
               for k, _d, _en, _zh in HORIZONS],
        off_high=Cell(""),
    )
    view.portfolio_counts = " · ".join(
        f"{en} {pf.counted.get(k, 0)}" for k, _d, en, _zh in HORIZONS)
    view.portfolio_counts_zh = " · ".join(
        f"{zh} {pf.counted.get(k, 0)}" for k, _d, _en, zh in HORIZONS)
    if pf.best:
        view.best = f"{pf.best[0]} {_pct(pf.best[1])}"
    if pf.worst:
        view.worst = f"{pf.worst[0]} {_pct(pf.worst[1])}"
    view.sparkline = sparkline_svg(pf.index_series)
    if pf.index_series:
        first, last = pf.index_series[0], pf.index_series[-1]
        change = f"{last[1] / first[1] - 1:+.1%}"
        view.spark_range = (f"{first[0]} → {last[0]}  ({change}, "
                            f"{pf.index_members} of {len(rep.tickers)} tickers)")
        view.spark_range_zh = (f"{first[0]} → {last[0]}  ({change}，"
                               f"{len(rep.tickers)} 檔中納入 {pf.index_members} 檔)")

    dir_words = {"up": ("Bullish", "看多", UP_FG),
                 "down": ("Bearish", "看空", DOWN_FG),
                 "neutral": ("Neutral", "中性", FLAT_FG)}
    for h in rep.signals:
        d_en, d_zh, d_fg = dir_words[h.direction]
        if h.correct is None:
            v_en, v_zh, v_fg = "—", "—", FLAT_FG
        elif h.correct:
            v_en, v_zh, v_fg = "on side", "方向正確", UP_FG
        else:
            v_en, v_zh, v_fg = "against", "方向相反", DOWN_FG
        view.signals.append(SignalRow(
            ticker=h.ticker, date=h.date,
            days_ago="today" if h.days_ago == 0 else f"{h.days_ago}d",
            days_ago_zh="本日" if h.days_ago == 0 else f"{h.days_ago}日前",
            label_en=h.label_en, label_zh=h.label_zh,
            dir_en=d_en, dir_zh=d_zh, dir_fg=d_fg,
            price="—" if h.price_at_signal is None else f"{h.price_at_signal:.2f}",
            forward=Cell(_pct(h.forward_pct), _fg(h.forward_pct)),
            verdict_en=v_en, verdict_zh=v_zh, verdict_fg=v_fg))

    view.scores = [_score_view("All tickers", "全部標的", rep.score)]
    for sym, s in sorted(rep.per_ticker_score.items()):
        view.scores.append(_score_view(sym, sym, s))
    return view


def render_html(view: TrackerView, lang_mode: str = "switch") -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html", "xml"]))
    return env.get_template("tracker.html.j2").render(v=view, lang_mode=lang_mode)


# --------------------------------------------------------------------------- #
# Terminal rendering
# --------------------------------------------------------------------------- #
def _table(head: list[str], rows: list[list[str]], left: int = 2) -> str:
    if not rows:
        return ""
    widths = [max([len(head[i])] + [len(r[i]) for r in rows]) for i in range(len(head))]

    def line(cells):
        return "  ".join(c.ljust(widths[i]) if i < left else c.rjust(widths[i])
                         for i, c in enumerate(cells))

    return "\n".join([line(head), "  ".join("-" * w for w in widths)]
                     + [line(r) for r in rows])


def render_text(view: TrackerView, max_signals: int = 25) -> str:
    if view.empty:
        return f"No data: {view.note}"
    out = [f"Portfolio performance & signal tracker — {view.as_of}",
           "=" * 72]
    head = ["ticker", "name", "close"] + [en for en, _zh in view.horizons] + ["off 52wH"]
    rows = [[r.symbol, (r.name or "")[:22], r.close]
            + [c.text for c in r.cells] + [r.off_high.text] for r in view.rows]
    if view.portfolio:
        rows.append(["—", "", ""] + ["" for _ in view.horizons] + [""])
        rows.append([view.portfolio.symbol, "equal-weight", ""]
                    + [c.text for c in view.portfolio.cells] + [""])
    out += [_table(head, rows, left=3), ""]
    out.append(f"  tickers counted per horizon: {view.portfolio_counts}")
    if view.best:
        out.append(f"  best today {view.best} · worst today {view.worst}")
    if view.spark_range:
        out.append(f"  equal-weight index {view.spark_range}")
    if view.note:
        out.append(f"  note: {view.note}")

    out += ["", f"Signals in the last {view.lookback_days} days", "-" * 72]
    if not view.signals:
        out.append("  none recorded")
    else:
        shown = view.signals[:max_signals]
        srows = [[s.ticker, s.label_en[:44], s.date, s.days_ago, s.dir_en,
                  s.price, s.forward.text, s.verdict_en] for s in shown]
        out.append(_table(["ticker", "signal", "date", "age", "claim",
                           "price", "since", "verdict"], srows, left=2))
        if len(view.signals) > len(shown):
            out.append(f"  … {len(view.signals) - len(shown)} more "
                       f"(use --max-signals or --json)")

    out += ["", "Signal hit rate", "-" * 72]
    for s in view.scores:
        out.append(f"  {s.scope_en:12s} {s.hit_rate:>6s}   {s.detail_en}")
    out.append("")
    out.append("  Hit rate tracks direction only — no entries, exits or costs, and "
               "every signal is")
    out.append("  measured to the latest bar, so older ones get a longer runway. "
               "Use the backtest")
    out.append("  (`portfolio-monitor-backtest`) for tradability.")
    return "\n".join(out)
