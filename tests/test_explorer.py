"""Interactive explorer tests — above all, Python/JavaScript engine parity.

`static/engine.js` is a second implementation of the backtest engine so the
standalone HTML can recompute without a server. Two engines drift; the parity test
here is the mechanism that stops it. It drives BOTH engines from one list of specs
(`explorer.PARITY_SPECS`) over one rounded price payload, and demands agreement to
1e-9 on every metric of every grid cell.

The parity test needs `node`, which is not a project dependency, so it skips when
node is absent — and a separate test asserts the skip is loud rather than silent.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import backtest, config, explorer  # noqa: E402

_RUNNER = Path(__file__).resolve().parent / "js" / "parity_runner.js"
_NODE = shutil.which("node") or shutil.which("nodejs")
needs_node = pytest.mark.skipif(_NODE is None, reason="node not installed")

TOL = 1e-9


# --------------------------------------------------------------------------- #
# Synthetic history: long enough to warm a 240-bar line and a monthly ladder,
# with real swings so every rule family actually fires.
# --------------------------------------------------------------------------- #
def _synthetic_bars(n: int = 2600) -> dict:
    import math
    dates = pd.bdate_range("2014-01-01", periods=n).strftime("%Y-%m-%d")
    close, opens, highs, lows, adj = [], [], [], [], []
    for i in range(n):
        # trend + two cycles of different lengths + a sawtooth, so MAs cross often
        v = (100.0 + i * 0.05
             + 18 * math.sin(i / 47.0) + 9 * math.sin(i / 11.0)
             + 4 * ((i % 23) / 23.0))
        close.append(round(v, 4))
        opens.append(round(v * 0.997, 4))
        highs.append(round(v * 1.01, 4))
        lows.append(round(v * 0.99, 4))
        adj.append(round(v * 0.93, 4))     # a real split/dividend factor != 1
    return explorer.bars_payload(pd.DataFrame({
        "date": list(dates), "open": opens, "high": highs, "low": lows,
        "close": close, "adj_close": adj, "volume": [1] * n, "source": ["t"] * n}))


@pytest.fixture(scope="module")
def bars():
    return _synthetic_bars()


@pytest.fixture(scope="module")
def fixture_path(bars, tmp_path_factory):
    fx = explorer.parity_fixture(bars)
    p = tmp_path_factory.mktemp("parity") / "fixture.json"
    p.write_text(json.dumps(fx), encoding="utf-8")
    return p, fx


def _run_node(path: Path) -> dict:
    proc = subprocess.run([_NODE, str(_RUNNER), str(path)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[-4000:]}"
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- #
# Payload shaping (pure, no node needed)
# --------------------------------------------------------------------------- #
def test_bars_payload_is_column_oriented_and_rounded():
    df = pd.DataFrame({"date": ["2024-01-02", "2024-01-01"],
                       "open": [1.123456, 2.0], "high": [1.2, 2.2], "low": [1.0, 2.0],
                       "close": [1.15, 2.1], "adj_close": [1.1, 2.05],
                       "volume": [1, 1], "source": ["t", "t"]})
    p = explorer.bars_payload(df)
    assert p["date"] == ["2024-01-01", "2024-01-02"]          # sorted ascending
    assert p["open"][1] == round(1.123456, 4)
    assert set(p) == {"date", "open", "high", "low", "close", "adjClose"}


def test_bars_payload_of_empty_frame_is_empty_columns():
    p = explorer.bars_payload(pd.DataFrame(columns=["date", "open", "high", "low",
                                                    "close", "adj_close"]))
    assert p["date"] == [] and p["adjClose"] == []


def test_payload_round_trips_back_to_a_frame(bars):
    df = explorer.payload_to_frame(bars)
    assert list(df["date"]) == list(bars["date"])
    assert list(df["adj_close"]) == list(bars["adjClose"])
    # and the engine accepts it
    bt = backtest.run_spec(df, "T", explorer.spec_from_js({}))
    assert bt.window_start is not None


def test_spec_from_js_maps_every_axis():
    spec = explorer.spec_from_js({
        "interval": "weekly", "maPeriods": [4, 13], "maKind": "ema",
        "entries": "degree1", "exits": "multi2", "start": "2020-01-01",
        "end": "2024-01-01", "windowDays": 7, "costBps": 25, "slopeLookback": 3,
        "flatThresholdPct": 2.0})
    assert spec.interval == "weekly" and spec.ladder == (4, 13)
    assert spec.ma_kind == "ema" and spec.window_days == 7 and spec.cost_bps == 25
    assert [s.label for s in spec.entries] == ["degree1"]
    assert [s.label for s in spec.exits] == ["multi2"]


def test_spec_from_js_blank_dates_become_none():
    spec = explorer.spec_from_js({"start": "", "end": ""})
    assert spec.start is None and spec.end is None


def test_result_payload_is_sorted_and_complete():
    bars_ = _synthetic_bars(700)
    bt = backtest.run_spec(explorer.payload_to_frame(bars_), "T",
                           explorer.spec_from_js({"entries": "degrees", "exits": "degrees"}))
    p = explorer.result_payload(bt)
    assert len(p["results"]) == 16
    assert p["results"] == sorted(p["results"], key=lambda r: (r["entry"], r["exit"]))
    assert set(p["results"][0]) == {"entry", "exit", "totalReturn", "cagr",
                                    "maxDrawdown", "numTrades", "winRate", "hasOpenTrade"}


def test_parity_fixture_covers_the_declared_spec_matrix(bars):
    fx = explorer.parity_fixture(bars, specs=explorer.PARITY_SPECS[:3])
    assert len(fx["cases"]) == 3
    assert fx["bars"] is bars
    assert all("expected" in c for c in fx["cases"])


def test_parity_matrix_exercises_every_rule_family_and_interval():
    """If a family or interval leaves the matrix, drift there becomes invisible."""
    blob = json.dumps(explorer.PARITY_SPECS)
    for token in ("degree", "multi", "cross:", "price:", "align", "slope:"):
        assert token in blob, f"rule family {token!r} missing from PARITY_SPECS"
    for iv in ("weekly", "monthly"):
        assert iv in blob, f"interval {iv!r} missing from PARITY_SPECS"
    assert "ema" in blob and "maPeriods" in blob


# --------------------------------------------------------------------------- #
# THE parity test
# --------------------------------------------------------------------------- #
@needs_node
def test_js_engine_matches_python_engine_on_every_spec(fixture_path):
    path, fx = fixture_path
    got = _run_node(path)
    assert len(got["cases"]) == len(fx["cases"])

    mismatches = []
    for i, (want_case, got_case) in enumerate(zip(fx["cases"], got["cases"])):
        spec, want, actual = want_case["spec"], want_case["expected"], got_case["actual"]
        if "error" in actual:
            mismatches.append(f"[{i}] {spec}: JS raised {actual['error']}")
            continue
        for key in ("windowStart", "windowEnd", "numBars", "dataStart", "interval"):
            if want[key] != actual[key]:
                mismatches.append(f"[{i}] {spec}: {key} py={want[key]!r} js={actual[key]!r}")
        if list(want["maPeriods"]) != list(actual["maPeriods"]):
            mismatches.append(f"[{i}] {spec}: maPeriods {want['maPeriods']} vs {actual['maPeriods']}")
        for key in ("buyHoldReturn", "buyHoldCagr"):
            a, b = want[key], actual[key]
            if (a is None) != (b is None) or (a is not None and abs(a - b) > TOL):
                mismatches.append(f"[{i}] {spec}: {key} py={a} js={b}")
        if len(want["results"]) != len(actual["results"]):
            mismatches.append(f"[{i}] {spec}: {len(want['results'])} cells vs {len(actual['results'])}")
            continue
        for pr, jr in zip(want["results"], actual["results"]):
            if (pr["entry"], pr["exit"]) != (jr["entry"], jr["exit"]):
                mismatches.append(f"[{i}] {spec}: cell order {pr['entry']}/{pr['exit']} "
                                  f"vs {jr['entry']}/{jr['exit']}")
                continue
            for key in ("totalReturn", "cagr", "maxDrawdown", "winRate"):
                if abs(pr[key] - jr[key]) > TOL:
                    mismatches.append(f"[{i}] {spec} {pr['entry']}x{pr['exit']}: "
                                      f"{key} py={pr[key]!r} js={jr[key]!r}")
            for key in ("numTrades", "hasOpenTrade"):
                if pr[key] != jr[key]:
                    mismatches.append(f"[{i}] {spec} {pr['entry']}x{pr['exit']}: "
                                      f"{key} py={pr[key]!r} js={jr[key]!r}")
    assert not mismatches, ("Python and JS engines disagree — the HTML explorer "
                            f"would lie:\n" + "\n".join(mismatches[:40]))


@needs_node
def test_parity_cases_are_not_all_degenerate(fixture_path):
    """Guards against the parity test passing because every grid came back empty.

    Three of the specs are deliberately degenerate (end before history, start after
    it) and correctly produce no cells; the rest must be real grids."""
    _path, fx = fixture_path
    sizes = [len(c["expected"]["results"]) for c in fx["cases"]]
    real = [n for n in sizes if n]
    assert len(real) >= len(sizes) - 3
    assert max(sizes) >= 16 and min(real) >= 1


@needs_node
def test_js_helpers_match_pandas_semantics(tmp_path):
    """MA and resampling semantics, checked directly rather than only through the
    grid — these are where a subtle port bug hides."""
    from portfolio_monitor import bars as bars_mod, indicators
    n = 40
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    close = [float(100 + i) for i in range(n)]
    df = pd.DataFrame({"date": list(dates), "open": close, "high": close, "low": close,
                       "close": close, "adj_close": close, "volume": [1] * n,
                       "source": ["t"] * n})
    ind = indicators.compute_indicators(df[["date", "close"]], periods=[3, 5])
    weekly = bars_mod.resample_bars(df, "weekly")
    probe = tmp_path / "probe.js"
    probe.write_text(f"""
const PM = require({str(_RUNNER.parent.parent.parent / 'src/portfolio_monitor/static/engine.js')!r});
const bars = {json.dumps(explorer.bars_payload(df))};
const out = {{
  sma3: PM.sma(bars.close, 3), sma5: PM.sma(bars.close, 5),
  ema3: PM.ema(bars.close, 3),
  weeklyDates: PM.resample(bars, 'weekly').date,
  weeklyClose: PM.resample(bars, 'weekly').close,
}};
process.stdout.write(JSON.stringify(out));
""", encoding="utf-8")
    got = json.loads(subprocess.run([_NODE, str(probe)], capture_output=True,
                                    text=True, timeout=120).stdout)

    def close_enough(js_list, py_series):
        for a, b in zip(js_list, py_series):
            if a is None or pd.isna(b):
                assert a is None and pd.isna(b)
            else:
                assert abs(a - float(b)) < 1e-9

    close_enough(got["sma3"], ind["sma3"])
    close_enough(got["sma5"], ind["sma5"])
    close_enough(got["ema3"], ind["ema3"])
    assert got["weeklyDates"] == list(weekly["date"])
    close_enough(got["weeklyClose"], weekly["close"])


def test_parity_runner_and_engine_files_exist():
    """A missing asset must fail here, not silently produce a broken page."""
    assert _RUNNER.exists()
    assert (Path(explorer.__file__).parent / "static" / "engine.js").exists()
    assert (Path(explorer.__file__).parent / "static" / "ui.js").exists()


def test_node_absence_is_reported_not_hidden():
    """Documents that parity coverage depends on node being present."""
    if _NODE is None:
        pytest.skip("node not installed — parity tests skipped (this is the point)")
    assert _NODE


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #
def _cfg(tickers):
    return config.Config(
        tickers=[config.Ticker(symbol=s, name=n) for s, n in tickers],
        settings={}, email=config.EmailConfig(host="", port=0, user="",
                                              app_password="", recipient=""))


@pytest.fixture()
def seeded(tmp_path, bars):
    from portfolio_monitor import db
    conn_cm = db.connect(tmp_path / "t.db")
    conn = conn_cm.__enter__()
    rows = []
    for i, d in enumerate(bars["date"]):
        rows.append(dict(ticker="TST", date=d, open=bars["open"][i], high=bars["high"][i],
                         low=bars["low"][i], close=bars["close"][i],
                         adj_close=bars["adjClose"][i], volume=1, source="t"))
    db.upsert_prices(conn, rows)
    yield conn
    conn_cm.__exit__(None, None, None)


def test_build_data_embeds_one_payload_per_ticker(seeded, bars):
    data = explorer.build_data(seeded, _cfg([("TST", "Test Co")]))
    assert [t["symbol"] for t in data.tickers] == ["TST"]
    assert data.tickers[0]["name"] == "Test Co"
    assert len(data.tickers[0]["bars"]["date"]) == len(bars["date"])
    assert "bars)" in data.tickers[0]["span"]


def test_build_data_can_trim_embedded_history(seeded):
    full = explorer.build_data(seeded, _cfg([("TST", "")]))
    trimmed = explorer.build_data(seeded, _cfg([("TST", "")]), max_years=3)
    assert len(trimmed.tickers[0]["bars"]["date"]) < len(full.tickers[0]["bars"]["date"])


def test_build_data_carries_a_selfcheck_block(seeded):
    data = explorer.build_data(seeded, _cfg([("TST", "")]))
    assert data.selfcheck["symbol"] == "TST"
    assert data.selfcheck["expected"]["results"]
    assert data.selfcheck["spec"]["entries"] == "degrees"


def test_build_data_without_prices_is_an_error(tmp_path):
    from portfolio_monitor import db
    with db.connect(tmp_path / "empty.db") as conn:
        with pytest.raises(RuntimeError, match="no cached prices"):
            explorer.build_data(conn, _cfg([("NONE", "")]))


def test_render_html_is_self_contained(seeded):
    """No resource load of any kind — the file must work from file:// while offline.

    Checks for things that actually *fetch* (src/href attributes, link tags, network
    APIs, dynamic import), not for the string "http", since the SVG namespace URI
    `http://www.w3.org/2000/svg` is an identifier and fetches nothing.
    """
    html = explorer.render_html(explorer.build_data(seeded, _cfg([("TST", "")])))
    assert html.startswith("<!doctype html>")
    loaders = [r"<script[^>]+\bsrc\s*=", r"<link\b", r"<img\b", r"<iframe\b",
               r"@import\b", r"\bfetch\s*\(", r"XMLHttpRequest", r"WebSocket",
               r"\bimport\s*\(", r"url\s*\(\s*[\'\"]?https?:"]
    for pat in loaders:
        assert not re.search(pat, html, re.I), f"external loader {pat!r} in a standalone file"
    # the only absolute URLs allowed are XML namespace identifiers
    for url in re.findall(r"https?://[^\s\'\")]+", html):
        assert url.startswith("http://www.w3.org/"), f"unexpected absolute URL: {url}"
    assert "PM.runSpec" in html and "__PM_DATA__" in html


def test_render_html_inlines_both_assets(seeded):
    html = explorer.render_html(explorer.build_data(seeded, _cfg([("TST", "")])))
    assert "Backtest engine, JavaScript port" in html      # engine.js docstring
    assert "renderGrid" in html                            # ui.js


def test_write_html_reports_its_size(seeded, tmp_path):
    out = tmp_path / "x.html"
    path, size = explorer.write_html(seeded, _cfg([("TST", "")]), out=out)
    assert path == out and size > 10_000
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


# --------------------------------------------------------------------------- #
# Equity curve path — not covered by parity, since the Python engine does not
# expose a curve. Checked instead against the metrics it must be consistent with.
# --------------------------------------------------------------------------- #
@needs_node
def test_js_equity_curve_is_consistent_with_its_own_metrics(bars, tmp_path):
    fixture = tmp_path / "curve_fixture.json"
    fixture.write_text(json.dumps({"bars": bars}), encoding="utf-8")
    engine = Path(explorer.__file__).parent / "static" / "engine.js"
    probe = tmp_path / "curve.js"
    probe.write_text(f"""
const fs = require('fs');
const PM = require({str(engine)!r});
const bars = JSON.parse(fs.readFileSync({str(fixture)!r}, 'utf8')).bars;
const run = PM.runSpec(bars, {{entries: 'degrees', exits: 'degrees', costBps: 5}});
const out = [];
for (const r of run.results) {{
  if (!r.numTrades) continue;
  const c = PM.curveFor(run, r.entry, r.exit);
  out.push({{
    entry: r.entry, exit: r.exit,
    gridReturn: r.totalReturn,
    curveReturn: c.equity[c.equity.length - 1] / c.equity[0] - 1,
    bars: c.dates.length, equityLen: c.equity.length, bhLen: c.buyHold.length,
    closeLen: c.close.length, maKeys: Object.keys(c.ma).length,
    trades: c.trades.length, gridTrades: r.numTrades,
    bhReturn: c.buyHold[c.buyHold.length - 1] / c.buyHold[0] - 1,
    monotonicDates: c.dates.every((d, i) => i === 0 || d > c.dates[i - 1]),
    finiteEquity: c.equity.every(v => Number.isFinite(v))
  }});
}}
process.stdout.write(JSON.stringify({{runBars: run.numBars, bh: run.buyHoldReturn, cells: out}}));
""", encoding="utf-8")
    got = json.loads(subprocess.run([_NODE, str(probe)], capture_output=True,
                                    text=True, timeout=180).stdout)
    assert got["cells"], "no traded cell produced a curve"
    for c in got["cells"]:
        label = f"{c['entry']}x{c['exit']}"
        # The curve must reproduce the metric the grid reported for the same cell.
        assert abs(c["gridReturn"] - c["curveReturn"]) < 1e-9, label
        # A closed trade contributes one entry+exit pair; an open one has no exit.
        assert c["trades"] == c["gridTrades"], label
        assert c["bars"] == got["runBars"] == c["equityLen"] == c["bhLen"] == c["closeLen"], label
        assert c["maKeys"] == 5, label
        assert abs(c["bhReturn"] - got["bh"]) < 1e-9, label
        assert c["monotonicDates"] and c["finiteEquity"], label
