"""Standalone interactive backtest explorer: one static HTML file (feature: 互動回測).

Builds a self-contained page that recomputes backtests **in the browser**. No web
service, no Python at view time, no network: the watchlist's price history is
embedded as compact JSON and the engine is a JavaScript port of `backtest.py` +
`rules.py` (`static/engine.js`), inlined into the file.

Why a JS port rather than sql.js/WASM: the only "query" the page needs is "give me
this ticker's bars", which a JSON array answers. Shipping SQLite-compiled-to-WASM
would add ~1.4MB of base64 for SQL we don't use — and it would not remove the need
for a strategy engine in the browser, which is the actual work.

The cost of that choice is **two engines that can drift**. It is guarded twice:

* `parity_fixture()` exports the Python engine's own answers for a matrix of specs;
  `tests/test_explorer.py` runs `engine.js` under node against it and asserts
  agreement to 1e-9. Change either side and that test fails.
* Every generated page embeds Python's results for its default spec and re-checks
  them on load, showing a banner if the two disagree. A file opened months later
  still tells the truth about itself.

This module is I/O at the edges (reads the cache, writes a file); the payload
shaping is pure and tested.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import backtest, bars as bars_mod, cache, db, rules
from .backtest import BacktestSpec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _REPO_ROOT / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
REPORTS_DIR = _REPO_ROOT / "reports"

# Price precision in the embedded payload. 4dp on adjusted prices is far finer than
# any real quote and keeps the file small; the parity test asserts the engines agree
# on exactly this payload, so rounding can never cause a Python/JS divergence.
_ROUND = 4


# --------------------------------------------------------------------------- #
# Payload shaping (pure)
# --------------------------------------------------------------------------- #
def bars_payload(df: pd.DataFrame) -> dict:
    """A price frame -> the column-oriented object `engine.js` consumes.

    Column-oriented rather than row-oriented because it is ~30% smaller (no
    repeated keys) and it is what the engine wants anyway.
    """
    if df.empty:
        return {"date": [], "open": [], "high": [], "low": [], "close": [], "adjClose": []}
    d = df.sort_values("date")
    return {
        "date": [str(x) for x in d["date"]],
        "open": [round(float(x), _ROUND) for x in d["open"]],
        "high": [round(float(x), _ROUND) for x in d["high"]],
        "low": [round(float(x), _ROUND) for x in d["low"]],
        "close": [round(float(x), _ROUND) for x in d["close"]],
        "adjClose": [round(float(x), _ROUND) for x in d["adj_close"]],
    }


def payload_to_frame(payload: dict) -> pd.DataFrame:
    """Inverse of `bars_payload`, so the Python engine can be run against exactly
    the rounded numbers the browser sees. Parity is meaningless otherwise."""
    n = len(payload["date"])
    return pd.DataFrame({
        "date": list(payload["date"]),
        "open": list(payload["open"]), "high": list(payload["high"]),
        "low": list(payload["low"]), "close": list(payload["close"]),
        "adj_close": list(payload["adjClose"]),
        "volume": [0] * n, "source": ["cache"] * n,
    })


def result_payload(bt: backtest.TickerBacktest) -> dict:
    """A TickerBacktest -> the same shape `engine.js` returns, for comparison."""
    return {
        "windowStart": bt.window_start, "windowEnd": bt.window_end,
        "numBars": bt.num_bars, "dataStart": bt.data_start,
        "buyHoldReturn": bt.buy_hold_return, "buyHoldCagr": bt.buy_hold_cagr,
        "interval": bt.interval, "maPeriods": list(bt.ma_periods),
        "results": sorted(
            [{"entry": r.entry_label, "exit": r.exit_label,
              "totalReturn": r.total_return, "cagr": r.cagr,
              "maxDrawdown": r.max_drawdown, "numTrades": r.num_trades,
              "winRate": r.win_rate, "hasOpenTrade": r.has_open_trade}
             for r in bt.all_results],
            key=lambda r: (r["entry"], r["exit"])),
    }


def spec_from_js(js: dict, cfg=None) -> BacktestSpec:
    """Build a Python BacktestSpec from the JS-side spec object, so both engines
    are driven from one description."""
    interval = bars_mod.normalize_interval(js.get("interval", "daily"))
    ladder = tuple(int(x) for x in (js.get("maPeriods") or bars_mod.default_ladder(interval)))
    base = BacktestSpec(
        start=js.get("start") or None, end=js.get("end") or None,
        interval=interval, ma_periods=ladder, ma_kind=js.get("maKind", "sma"),
        window_days=int(js.get("windowDays", 30)),
        cost_bps=float(js.get("costBps", 5.0)),
        starting_cash=float(js.get("startingCash", 10000.0)),
        slope_lookback=int(js.get("slopeLookback", 10)),
        flat_threshold_pct=float(js.get("flatThresholdPct", 0.5)),
    )
    ctx = base.context()
    import dataclasses
    return dataclasses.replace(
        base,
        entries=tuple(rules.parse_rules(js.get("entries", "degrees"), ctx)),
        exits=tuple(rules.parse_rules(js.get("exits", "degrees"), ctx)),
    )


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #
# The specs the parity test sweeps. Chosen to exercise every branch that could
# diverge: all three intervals, both MA families, custom ladders, every rule
# family, an explicit window, a clamped start, and a non-default cost/lookback.
PARITY_SPECS: list[dict] = [
    {"entries": "degrees", "exits": "degrees"},
    {"entries": "multis", "exits": "multis"},
    {"entries": "all", "exits": "degree1"},
    {"entries": "degree1,multi2,cross:sma20/sma60,price:sma20,align,slope:sma240",
     "exits": "degree4,multi4,cross:sma5/sma20,price:sma60,align,slope:sma20"},
    {"interval": "weekly", "entries": "degrees", "exits": "multis"},
    {"interval": "monthly", "entries": "all", "exits": "all"},
    {"maKind": "ema", "entries": "crosses", "exits": "prices"},
    {"maPeriods": [10, 50, 200], "entries": "degrees,multis", "exits": "degrees"},
    {"start": "2020-01-01", "end": "2024-06-30", "entries": "degrees", "exits": "degrees"},
    {"start": "1990-01-01", "entries": "degree1", "exits": "degree1"},   # clamped
    {"end": "1990-01-01", "entries": "degree1", "exits": "degree1"},     # before history
    {"start": "2099-01-01", "entries": "degree1", "exits": "degree1"},   # after history
    {"costBps": 25, "windowDays": 7, "slopeLookback": 3, "flatThresholdPct": 2.0,
     "entries": "all", "exits": "all"},
    {"interval": "weekly", "maPeriods": [4, 13], "entries": "degrees,multis",
     "exits": "crosses,slopes"},
]


def parity_fixture(bars: dict, specs: list[dict] | None = None) -> dict:
    """Python's answers for each spec, against the exact rounded payload.

    The fixture is the contract between the two engines: `cases[i].expected` is
    what `engine.js` must reproduce for `cases[i].spec`.
    """
    specs = specs if specs is not None else PARITY_SPECS
    df = payload_to_frame(bars)
    cases = []
    for js in specs:
        bt = backtest.run_spec(df, "PARITY", spec_from_js(js))
        cases.append({"spec": js, "expected": result_payload(bt)})
    return {"bars": bars, "cases": cases}


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #
@dataclass
class ExplorerData:
    generated: str
    tickers: list[dict]          # [{symbol, name, bars, span}]
    defaults: dict               # the initial spec the page opens on
    selfcheck: dict              # {symbol, spec, expected} for the on-load parity check


def _asset(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def build_data(conn, cfg, symbols: list[str] | None = None,
               max_years: int | None = None) -> ExplorerData:
    """Collect the embedded payload: one bars object per watchlist ticker.

    `max_years` trims how much history is embedded — the whole point of the file is
    that it is portable, so a 14-year cache does not have to become a 14-year page.
    """
    names = {t.symbol: t.name for t in cfg.tickers}
    wanted = [s.upper() for s in (symbols or list(names) or db.tickers_with_prices(conn))]
    start = None
    if max_years:
        start = (date.today() - timedelta(days=int(max_years * 365.25))).isoformat()

    payloads = []
    for sym in wanted:
        df = cache.rows_to_frame(db.get_prices(conn, sym, start=start))
        if df.empty:
            continue
        bp = bars_payload(df)
        payloads.append({"symbol": sym, "name": names.get(sym, ""), "bars": bp,
                         "span": f"{bp['date'][0]} → {bp['date'][-1]} ({len(bp['date'])} bars)"})
    if not payloads:
        raise RuntimeError("no cached prices to embed — run the pipeline or "
                           "`portfolio-monitor-cache sync` first")

    defaults = {
        "symbol": payloads[0]["symbol"],
        "interval": "daily", "maPeriods": None, "maKind": "sma",
        "entries": "degrees", "exits": "degrees",
        "start": "", "end": "",
        "windowDays": cfg.recent_window_days,
        "costBps": cfg.backtest_cost_bps,
        "startingCash": cfg.backtest_starting_cash,
        "slopeLookback": cfg.slope_lookback,
        "flatThresholdPct": cfg.flat_threshold_pct,
        "sort": "cagr",
    }
    check_spec = {k: v for k, v in defaults.items() if k not in ("symbol", "sort")}
    expected = result_payload(backtest.run_spec(
        payload_to_frame(payloads[0]["bars"]), payloads[0]["symbol"],
        spec_from_js(check_spec)))
    return ExplorerData(
        generated=date.today().isoformat(), tickers=payloads, defaults=defaults,
        selfcheck={"symbol": payloads[0]["symbol"], "spec": check_spec, "expected": expected},
    )


def render_html(data: ExplorerData) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html", "xml"]))
    return env.get_template("explorer.html.j2").render(
        generated=data.generated,
        engine_js=_asset("engine.js"),
        ui_js=_asset("ui.js"),
        payload_json=json.dumps(
            {"tickers": data.tickers, "defaults": data.defaults, "selfcheck": data.selfcheck},
            separators=(",", ":"), allow_nan=False),
    )


def write_html(conn, cfg, out: Path | None = None, symbols: list[str] | None = None,
               max_years: int | None = None) -> tuple[Path, int]:
    """Build and save the explorer. Returns (path, bytes)."""
    data = build_data(conn, cfg, symbols=symbols, max_years=max_years)
    path = out or (REPORTS_DIR / f"backtest-explorer-{data.generated}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(data)
    path.write_text(html, encoding="utf-8")
    return path, len(html.encode("utf-8"))
