"""Assemble the daily HTML report (feature 5).

Builds a per-ticker context (latest close, 1-day change, MA values, trend-state
label, today's signals, chart image) and renders templates/report.html.j2.

Chart images are referenced two ways:
  * mode="datauri"  -> base64-embedded, producing a self-contained HTML file.
  * mode="cid"      -> `cid:<ticker>` references for inline email attachment.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts
from .backtest import TickerBacktest
from .signals import CrossEvent, TrendSummary

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _REPO_ROOT / "templates"

# Trend-state chips: (en label, zh label, fg colour, bg colour).
STATE_LABELS = {
    "ALIGNED_UP": ("Uptrend", "趨勢向上", "#0a8f52", "#e6f6ee"),
    "ALIGNED_DOWN": ("Downtrend", "趨勢向下", "#c0392b", "#fbeae7"),
    "LONG_UP_SHORT_DOWN": ("Pullback", "長多短空", "#b26a00", "#fdf0dc"),
    "LONG_DOWN_SHORT_BREAKOUT": ("Breakout", "長空短多突破", "#1d6fb8", "#e4f0fb"),
    "unknown": ("Insufficient data", "資料不足", "#66707a", "#f0f2f4"),
}
# Trend-family signal labels: signal_type -> (en, zh).
SIGNAL_LABELS = {
    "GOLDEN_CROSS": ("Golden Cross (SMA20↑SMA60)", "黃金交叉 (SMA20↑SMA60)"),
    "DEATH_CROSS": ("Death Cross (SMA20↓SMA60)", "死亡交叉 (SMA20↓SMA60)"),
    "LONG_UP_SHORT_DOWN": ("Long-up / Short-down", "長多短空"),
    "LONG_DOWN_SHORT_BREAKOUT": ("Long-down / Short-breakout", "長空短多突破"),
    "ALIGNED_UP": ("Turned Uptrend", "轉為趨勢向上"),
    "ALIGNED_DOWN": ("Turned Downtrend", "轉為趨勢向下"),
}

# MA detail cells: (key, en label, zh label).
_MA_DISPLAY = [("sma5", "SMA5 W", "SMA5 周"), ("sma20", "SMA20 M", "SMA20 月"),
               ("sma60", "SMA60 Q", "SMA60 季"), ("sma120", "SMA120 H", "SMA120 半年"),
               ("sma240", "SMA240 Y", "SMA240 年")]


@dataclass
class SignalLine:
    """One line in the report's signal cell: a bilingual headline plus an
    optional bilingual sub-note (the 雙重趨勢訊號 / dual-trend annotation)."""
    text_en: str
    text_zh: str
    note_en: str | None = None
    note_zh: str | None = None


@dataclass
class BacktestView:
    """Display-ready backtest summary for one ticker: the single best strategy by
    CAGR vs buy-and-hold, all strings preformatted so the template stays dumb.
    Rendered under each ticker; labeled as hindsight-selected (ADR-0004)."""
    no_trades: bool
    window: str = ""
    entry_degree: int = 0
    exit_degree: int = 0
    total_return_pct: str = ""
    cagr_pct: str = ""
    max_drawdown_pct: str = ""
    num_trades: int = 0
    win_rate_pct: str = ""
    has_open_trade: bool = False
    buy_hold_pct: str = ""


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def build_backtest_view(bt: TickerBacktest | None) -> BacktestView:
    """Map an engine TickerBacktest to a display view. Insufficient history or a
    ticker no strategy ever traded both collapse to the muted no_trades state."""
    if bt is None or bt.window_start is None or bt.best is None:
        return BacktestView(no_trades=True)
    b = bt.best
    dd_val = b.max_drawdown * 100
    dd = f"-{dd_val:.1f}%" if dd_val >= 0.05 else "0.0%"
    return BacktestView(
        no_trades=False,
        window=f"{bt.window_start} → {bt.window_end}",
        entry_degree=b.entry_degree, exit_degree=b.exit_degree,
        total_return_pct=_pct(b.total_return), cagr_pct=_pct(b.cagr),
        max_drawdown_pct=dd, num_trades=b.num_trades,
        win_rate_pct=f"{b.win_rate * 100:.0f}%", has_open_trade=b.has_open_trade,
        buy_hold_pct=_pct(bt.buy_hold_return if bt.buy_hold_return is not None else 0.0),
    )


@dataclass
class TickerView:
    symbol: str
    name: str
    close: float
    change_pct: float
    state_label_en: str
    state_label_zh: str
    state_fg: str
    state_bg: str
    signals: list[SignalLine]
    ma_cells: list[tuple[str, str, str]]   # (en label, zh label, value)
    chart_src: str
    chart_html: str | None = None   # interactive fragment for the browser report
    # --- always-present trend summary (MA alignment + multi-breakout + recent) ---
    alignment_en: str = ""
    alignment_zh: str = ""
    alignment_fg: str = "#66707a"
    alignment_bg: str = "#f0f2f4"
    trend_tags: list[SignalLine] = field(default_factory=list)
    recent_crosses: list[SignalLine] = field(default_factory=list)
    # --- signal backtest (best strategy vs buy-and-hold; hindsight-selected) ---
    backtest: "BacktestView | None" = None


@dataclass
class ReportContext:
    report_date: str
    tickers: list[TickerView] = field(default_factory=list)
    ma_legend: list[tuple[str, str]] = field(default_factory=charts.ma_legend)
    data_source: str = "Yahoo Finance (yfinance)"
    data_source_note: str = ""       # en (cross-check status)
    data_source_note_zh: str = ""    # zh_TW equivalent
    plotly_js: str = ""   # inlined plotly.js <script>, present only for the browser report


def _one_day_change_pct(prices: pd.DataFrame) -> float:
    s = prices.sort_values("date")["close"].astype(float)
    if len(s) < 2 or s.iloc[-2] == 0:
        return 0.0
    return (s.iloc[-1] - s.iloc[-2]) / s.iloc[-2] * 100.0


def _fmt(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{float(v):.2f}"


# Alignment chip colours by direction (matches trend-state palette).
_ALIGN_COLORS = {
    "up": ("#0a8f52", "#e6f6ee"),
    "down": ("#c0392b", "#fbeae7"),
    "mixed": ("#66707a", "#f0f2f4"),
}


def build_ticker_view(symbol: str, name: str, prices: pd.DataFrame,
                      indicators: pd.DataFrame, state: str,
                      today_signal_types: list[str], chart_src: str,
                      cross_events: list[CrossEvent] | None = None,
                      chart_html: str | None = None,
                      trend: TrendSummary | None = None,
                      backtest_view: "BacktestView | None" = None) -> TickerView:
    prices = prices.sort_values("date")
    last_close = float(prices["close"].iloc[-1])
    last_ind = indicators.sort_values("date").iloc[-1] if not indicators.empty else {}
    ma_cells = [(en, zh, _fmt(last_ind[key] if key in last_ind else None))
                for key, en, zh in _MA_DISPLAY]
    label_en, label_zh, fg, bg = STATE_LABELS.get(state, STATE_LABELS["unknown"])

    lines: list[SignalLine] = []
    # Granular MA-cross events first (周/月/季/半年/年線 突破/跌破 + 雙重趨勢訊號 note).
    for ev in cross_events or []:
        lines.append(SignalLine(text_en=ev.label_en, text_zh=ev.label,
                                note_en=ev.note_en, note_zh=ev.note))
    # Then the trend-family transitions. The generic SMA20/60 golden/death cross
    # is already surfaced richer by the cross events above, so skip it here.
    for s in today_signal_types:
        if s in ("GOLDEN_CROSS", "DEATH_CROSS"):
            continue
        en, zh = SIGNAL_LABELS.get(s, (s, s))
        lines.append(SignalLine(text_en=en, text_zh=zh))

    # Always-present trend summary: MA alignment + multi-breakout tags + recent.
    align_en = align_zh = ""
    align_fg, align_bg = _ALIGN_COLORS["mixed"]
    trend_tags: list[SignalLine] = []
    recent_lines: list[SignalLine] = []
    if trend is not None:
        align_en, align_zh = trend.alignment_en, trend.alignment_zh
        align_fg, align_bg = _ALIGN_COLORS.get(trend.alignment_dir, _ALIGN_COLORS["mixed"])
        trend_tags = [SignalLine(text_en=en, text_zh=zh) for en, zh in trend.tags]
        for e in trend.recent[:5]:
            if e.days_ago == 0:
                suf_en, suf_zh = "(today)", "(本日)"
            else:
                suf_en, suf_zh = f"({e.days_ago}d ago)", f"({e.days_ago}日前)"
            recent_lines.append(SignalLine(text_en=f"{e.label_en} {suf_en}",
                                           text_zh=f"{e.label} {suf_zh}"))

    return TickerView(
        symbol=symbol, name=name, close=last_close,
        change_pct=_one_day_change_pct(prices),
        state_label_en=label_en, state_label_zh=label_zh, state_fg=fg, state_bg=bg,
        signals=lines, ma_cells=ma_cells, chart_src=chart_src,
        chart_html=chart_html,
        alignment_en=align_en, alignment_zh=align_zh,
        alignment_fg=align_fg, alignment_bg=align_bg,
        trend_tags=trend_tags, recent_crosses=recent_lines,
        backtest=backtest_view,
    )


def _png_to_datauri(path: Path) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def chart_src_for(symbol: str, chart_path: Path, mode: str) -> str:
    if mode == "cid":
        return f"cid:{symbol.upper()}"
    return _png_to_datauri(chart_path)


def render_report(context: ReportContext, lang_mode: str = "switch") -> str:
    """Render the report HTML.

    lang_mode:
      "switch" -> bilingual browser report: both languages emitted, a top-right
                  EN/中文 switcher toggles them via CSS/JS (default EN).
      "en" / "zh" -> a single fixed language (used for email, which can't run JS).
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.j2")
    return template.render(
        report_date=context.report_date,
        tickers=context.tickers,
        ma_legend=context.ma_legend,
        data_source=context.data_source,
        data_source_note=context.data_source_note,
        data_source_note_zh=context.data_source_note_zh,
        plotly_js=context.plotly_js,
        lang_mode=lang_mode,
    )
