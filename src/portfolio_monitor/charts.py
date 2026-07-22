"""Render a candlestick trend chart per ticker (feature 5: 趨勢圖).

Uses mplfinance with a non-interactive Agg backend so it runs headless under
cron. Overlays the 5/20/60/120/240-day SMA lines plus a volume panel, and
zooms to the most recent `display_days` bars for readability while the MAs are
still computed from the full history.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless; must precede pyplot import

import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

# MA period -> line colour (consistent across every chart / the report legend).
_MA_COLORS = {
    "sma5": "#e15759",    # 周線  red
    "sma20": "#f28e2b",   # 月線  orange
    "sma60": "#4e79a7",   # 季線  blue
    "sma120": "#59a14f",  # 半年線 green
    "sma240": "#9c755f",  # 年線  brown
}
_MA_LABELS = {
    "sma5": "5 (周)", "sma20": "20 (月)", "sma60": "60 (季)",
    "sma120": "120 (半年)", "sma240": "240 (年)",
}
# English legend labels, parallel to _MA_LABELS (for the bilingual report legend).
_MA_LABELS_EN = {
    "sma5": "5 (W)", "sma20": "20 (M)", "sma60": "60 (Q)",
    "sma120": "120 (H)", "sma240": "240 (Y)",
}


def render_chart(ticker: str, prices: pd.DataFrame, indicators: pd.DataFrame,
                 out_dir: str | Path, display_days: int = 180) -> Path:
    """Write a PNG chart for `ticker` and return its path.

    `prices` needs date, open, high, low, close, volume. `indicators` needs
    date + sma columns. Both ascending by date.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}.png"

    df = prices.merge(indicators, on="date", how="left").sort_values("date")
    df = df.copy()
    df["dt"] = pd.to_datetime(df["date"])
    df = df.set_index("dt")

    # OHLCV frame mplfinance expects (capitalised columns).
    ohlc = df[["open", "high", "low", "close", "volume"]].rename(
        columns={"open": "Open", "high": "High", "low": "Low",
                 "close": "Close", "volume": "Volume"})

    view = ohlc.tail(display_days)
    addplots = []
    for col, color in _MA_COLORS.items():
        if col in df.columns and df[col].notna().any():
            series = df[col].tail(display_days)
            if series.notna().any():
                addplots.append(mpf.make_addplot(series, color=color, width=1.0))

    style = mpf.make_mpf_style(base_mpf_style="yahoo", gridstyle=":",
                               facecolor="white")
    title = f"\n{ticker.upper()} — last {min(display_days, len(ohlc))} sessions"
    mpf.plot(
        view, type="candle", style=style, addplot=addplots,
        volume=True, figsize=(11, 6.5), title=title,
        savefig=dict(fname=str(out_path), dpi=110, bbox_inches="tight"),
        warn_too_much_data=len(view) + 1,
    )
    return out_path


def ma_legend() -> list[tuple[str, str, str]]:
    """(en label, zh label, color) triples for the bilingual report legend."""
    return [(_MA_LABELS_EN[k], _MA_LABELS[k], _MA_COLORS[k]) for k in _MA_COLORS]


# --------------------------------------------------------------------------- #
# Interactive chart (feature: 互動式趨勢圖) — hover shows date/OHLC/SMA/EMA/volume.
# The static PNG above stays the failsafe (used for email + <noscript>).
# --------------------------------------------------------------------------- #
def plotly_js_script() -> str:
    """The plotly.js library wrapped in a <script> tag, to be inlined ONCE per
    report so the interactive charts render fully offline (self-contained)."""
    from plotly.offline import get_plotlyjs

    return f"<script>{get_plotlyjs()}</script>"


def _cross_markers(ticker: str, prices: pd.DataFrame, indicators: pd.DataFrame,
                   visible_dates: set) -> tuple[dict, dict]:
    """Build ▲/▼ marker data for every MA crossing whose date is on-screen.

    Returns (up, down) dicts each with x (dates), y (crossing level = the short
    MA's value on the cross bar), and en/zh hover texts. The zh texts are also
    consumed by the language switcher (registered alongside the chart)."""
    from . import signals

    events = signals.detect_recent_cross_events(prices, indicators, window_days=10 ** 6)
    ind = indicators.set_index("date")
    up = {"x": [], "y": [], "en": [], "zh": []}
    dn = {"x": [], "y": [], "en": [], "zh": []}
    for e in events:
        if e.date not in visible_dates:
            continue
        y = ind[e.short].get(e.date) if e.short in ind.columns else None
        if y is None or pd.isna(y):
            continue
        bucket = up if e.direction == "up" else dn
        bucket["x"].append(e.date)
        bucket["y"].append(float(y))
        bucket["en"].append(f"{e.label_en}  ·  {e.date}")
        bucket["zh"].append(f"{e.label}  ·  {e.date}")
    return up, dn


def _breakout_clusters(prices: pd.DataFrame, indicators: pd.DataFrame,
                       visible_dates: set, window_days: int = 30) -> list[dict]:
    """Find multi-line breakout/breakdown clusters inside the visible window.

    Groups same-direction crossings that occur within `window_days` of each other;
    a group spanning >=2 DISTINCT adjacent pairs is a 雙重/三重/四重突破 (up) or
    跌破 (down). Returns dicts with x0/x1 (span), xc (centre date), degree, and
    bilingual labels — used to draw a strong highlight band + ★ label."""
    from . import signals

    events = [e for e in signals.detect_recent_cross_events(prices, indicators, 10 ** 6)
              if e.date in visible_dates]
    clusters: list[dict] = []
    for direction in ("up", "down"):
        up = direction == "up"
        evs = sorted((e for e in events if e.direction == direction), key=lambda e: e.date)
        group: list = []

        def flush(g):
            # Distinct pairs -> the date each one crossed (last occurrence wins).
            pair_date = {}
            for e in g:
                pair_date[(e.short, e.long)] = e.date
            pairs = sorted(pair_date, key=lambda pl: int(pl[0][3:]))
            if len(pairs) < 2:
                return
            n = min(len(pairs), 4)
            word_zh, word_en = ("突破", "breakout") if up else ("跌破", "breakdown")
            head_zh = f"{signals._NUM_ZH[n]}重{'向上突破' if up else '向下跌破'}"
            head_en = f"{signals._NUM_EN[n]} {word_en}"
            # Constituent crossings — which line broke which, and on what date.
            items_zh = [f"{signals.MA_CH[s]}{'向上突破' if up else '向下跌破'}{signals.MA_CH[l]}"
                        f" ({pair_date[(s, l)]})" for s, l in pairs]
            items_en = [f"{signals.MA_EN[s]} {'breaks above' if up else 'breaks below'} "
                        f"{signals.MA_EN[l]} ({pair_date[(s, l)]})" for s, l in pairs]
            dates = sorted(e.date for e in g)
            clusters.append(dict(
                direction=direction, x0=dates[0], x1=dates[-1],
                xc=dates[len(dates) // 2], degree=n,
                label_zh=f"{signals._NUM_ZH[n]}重{word_zh}",
                label_en=head_en,
                hover_zh=head_zh + "：<br>" + "<br>".join("· " + it for it in items_zh),
                hover_en=head_en + ":<br>" + "<br>".join("· " + it for it in items_en)))

        for e in evs:
            # Bound the cluster to `window_days` from its FIRST cross so a
            # highlight marks a tight burst, not a months-long chain.
            if group and (pd.Timestamp(e.date) - pd.Timestamp(group[0].date)).days > window_days:
                flush(group)
                group = []
            group.append(e)
        flush(group)
    return clusters


def render_interactive_html(ticker: str, prices: pd.DataFrame,
                            indicators: pd.DataFrame,
                            display_days: int = 180) -> str:
    """Return a self-contained interactive candlestick chart as an HTML fragment.

    Hovering shows a unified tooltip with the date, OHLC, every SMA/EMA line and
    volume. The plotly.js library itself is NOT included here — inline it once
    via `plotly_js_script()` — so multiple charts share a single copy.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    df = prices.merge(indicators, on="date", how="left").sort_values("date")
    df = df.tail(display_days)
    x = pd.to_datetime(df["date"])

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[0.74, 0.26],
        subplot_titles=(f"{ticker.upper()} — Price & MA (last {len(df)} sessions)",
                        "Volume"),
    )

    fig.add_trace(go.Candlestick(
        x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="OHLC", increasing_line_color="#d1495b", decreasing_line_color="#2a9d8f",
        showlegend=False), row=1, col=1)

    # SMA solid + matching EMA dashed for each period; unified hover lists them all.
    for col, color in _MA_COLORS.items():
        period = col[3:]
        if col in df.columns and df[col].notna().any():
            # Neutral legend name (no CJK) so the chart legend reads the same in
            # both languages; the 周/月/季… mapping lives in the report's MA legend.
            fig.add_trace(go.Scatter(
                x=x, y=df[col], mode="lines", name=f"SMA{period}",
                line=dict(color=color, width=1.4),
                hovertemplate="%{y:.2f}<extra>SMA" + period + "</extra>"), row=1, col=1)
        ecol = f"ema{period}"
        if ecol in df.columns and df[ecol].notna().any():
            fig.add_trace(go.Scatter(
                x=x, y=df[ecol], mode="lines", name=f"EMA{period}",
                line=dict(color=color, width=1.0, dash="dot"),
                visible="legendonly",
                hovertemplate="%{y:.2f}<extra>EMA" + period + "</extra>"), row=1, col=1)

    # Mark each MA crossing inside the visible window with a ▲ (breakout) / ▼
    # (breakdown) glyph at the crossing level; hovering pops up the detail message.
    up_mk, dn_mk = _cross_markers(ticker, prices, indicators, set(df["date"]))
    if up_mk["x"]:
        fig.add_trace(go.Scatter(
            x=up_mk["x"], y=up_mk["y"], mode="markers", name="Breakout",
            marker=dict(symbol="triangle-up", size=12, color="#0a8f52",
                        line=dict(width=1, color="#ffffff")),
            text=up_mk["en"], hovertemplate="%{text}<extra></extra>",
            showlegend=False, meta="markUp"), row=1, col=1)
    if dn_mk["x"]:
        fig.add_trace(go.Scatter(
            x=dn_mk["x"], y=dn_mk["y"], mode="markers", name="Breakdown",
            marker=dict(symbol="triangle-down", size=12, color="#c0392b",
                        line=dict(width=1, color="#ffffff")),
            text=dn_mk["en"], hovertemplate="%{text}<extra></extra>",
            showlegend=False, meta="markDown"), row=1, col=1)

    # Strong highlight for multi-line breakout/breakdown clusters (雙重/三重/四重).
    clusters = _breakout_clusters(prices, indicators, set(df["date"]))
    vis_high, vis_low = float(df["high"].max()), float(df["low"].min())
    empty = lambda: {"x": [], "lab_en": [], "lab_zh": [], "hov_en": [], "hov_zh": []}
    clu = {"up": empty(), "down": empty()}
    for cl in clusters:
        up = cl["direction"] == "up"
        color = "#0a8f52" if up else "#c0392b"
        pad = pd.Timedelta(days=2)
        # Full-height shaded band across the cluster span (stronger the higher the degree).
        fig.add_vrect(
            x0=(pd.Timestamp(cl["x0"]) - pad).strftime("%Y-%m-%d"),
            x1=(pd.Timestamp(cl["x1"]) + pad).strftime("%Y-%m-%d"),
            fillcolor=color, opacity=0.06 + 0.05 * cl["degree"],
            line_width=1.2, line_color=color, line_dash="dot", layer="below")
        b = clu["up"] if up else clu["down"]
        b["x"].append(cl["xc"])
        b["lab_en"].append("★ " + cl["label_en"])       # short on-chart label
        b["lab_zh"].append("★ " + cl["label_zh"])
        b["hov_en"].append(cl["hover_en"])              # detailed popup (which lines broke)
        b["hov_zh"].append(cl["hover_zh"])
    if clu["up"]["x"]:
        fig.add_trace(go.Scatter(
            x=clu["up"]["x"], y=[vis_high] * len(clu["up"]["x"]),
            mode="markers+text", text=clu["up"]["lab_en"], hovertext=clu["up"]["hov_en"],
            textposition="top center", textfont=dict(size=12, color="#0a8f52"),
            name="MultiBreakout",
            marker=dict(symbol="star", size=16, color="#0a8f52",
                        line=dict(width=1, color="#ffffff")),
            hovertemplate="%{hovertext}<extra></extra>", showlegend=False,
            meta="clusterUp"), row=1, col=1)
    if clu["down"]["x"]:
        fig.add_trace(go.Scatter(
            x=clu["down"]["x"], y=[vis_low] * len(clu["down"]["x"]),
            mode="markers+text", text=clu["down"]["lab_en"],
            hovertext=clu["down"]["hov_en"],
            textposition="bottom center", textfont=dict(size=12, color="#c0392b"),
            name="MultiBreakdown",
            marker=dict(symbol="star", size=16, color="#c0392b",
                        line=dict(width=1, color="#ffffff")),
            hovertemplate="%{hovertext}<extra></extra>", showlegend=False,
            meta="clusterDown"), row=1, col=1)

    up_down = df["close"] >= df["open"]
    fig.add_trace(go.Bar(
        x=x, y=df["volume"], name="Volume", showlegend=False,
        marker_color=["#d1495b" if u else "#2a9d8f" for u in up_down],
        hovertemplate="%{y:,}<extra>Volume</extra>"), row=2, col=1)

    fig.update_layout(
        template="plotly_white", hovermode="x unified",
        height=640, margin=dict(l=40, r=20, t=48, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11)),
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    div_id = f"chart-{ticker.upper()}"
    frag = fig.to_html(include_plotlyjs=False, full_html=False, div_id=div_id,
                       config={"displayModeBar": True, "responsive": True})

    # The chart is drawn with English labels (default); register the zh_TW strings
    # so the report's language switcher can relayout it via Plotly (see template JS).
    n = len(df)
    i18n = {
        "en": {"t0": f"{ticker.upper()} — Price & MA (last {n} sessions)",
               "t1": "Volume", "y0": "Price", "y1": "Volume", "vhover": "Volume",
               "markUp": up_mk["en"], "markDown": dn_mk["en"],
               "clusterUp": clu["up"]["lab_en"], "clusterDown": clu["down"]["lab_en"],
               "clusterUpHover": clu["up"]["hov_en"], "clusterDownHover": clu["down"]["hov_en"]},
        "zh": {"t0": f"{ticker.upper()} — 收盤與均線 (最近 {n} 筆)",
               "t1": "成交量", "y0": "價格", "y1": "量", "vhover": "成交量",
               "markUp": up_mk["zh"], "markDown": dn_mk["zh"],
               "clusterUp": clu["up"]["lab_zh"], "clusterDown": clu["down"]["lab_zh"],
               "clusterUpHover": clu["up"]["hov_zh"], "clusterDownHover": clu["down"]["hov_zh"]},
    }
    reg = (f'<script>window.__pmChartI18n=(window.__pmChartI18n||{{}});'
           f'window.__pmChartI18n["{div_id}"]={json.dumps(i18n, ensure_ascii=False)};</script>')
    return frag + reg
