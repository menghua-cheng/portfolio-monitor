# The interactive explorer duplicates the engine in JavaScript, guarded by a parity test

The explorer is **one static HTML file** that recomputes backtests in the browser: no web
service, no Python at view time, no network. Changing the window, interval, MA ladder or
rules re-runs the grid locally in a few milliseconds.

That requires a backtest engine in the browser, and there is no way to have one without
either shipping a Python runtime or writing a second engine. Options considered:

- **Precompute every grid in Python, embed the results, let JS filter and chart.** No
  duplication, tiny file — and not a backtester. You could re-sort results but not change
  the window, the interval or the rules, which is the entire point.
- **sql.js (SQLite compiled to WASM) with the `.db` embedded.** Superficially appealing
  because the data already lives in SQLite. Rejected: the only "query" needed is "give me
  this ticker's bars", which a JSON array answers, and the WASM blob is ~1MB (~1.4MB
  base64'd). Worse, it does not remove the need for a strategy engine in the browser — it
  solves the easy half of the problem at high cost.
- **Pyodide (CPython in WASM), reusing `backtest.py` verbatim.** The only option with *no*
  duplication. Rejected: ~10MB+ of runtime plus pandas/numpy, several seconds of startup,
  and it fails the "just a static file you can email yourself" test the request was built
  around.
- **A JavaScript port of the engine** (chosen). Instant startup, works from `file://`
  offline; 0.22MB with the lean chart renderer, 4.85MB with plotly.js inlined for zoom.

## The cost, and how it is paid

**Two engines can drift**, and a drifted explorer does not crash — it quietly reports
different numbers than the CLI. Comments and care do not prevent this, so it is guarded
structurally, twice:

1. **A parity test** (`tests/test_explorer.py`). Both engines are driven from one list of
   specs (`explorer.PARITY_SPECS`) over one *rounded* price payload — the same numbers the
   browser sees, since comparing against unrounded data would leave a real divergence
   invisible. Every metric of every grid cell must agree to 1e-9. The matrix deliberately
   covers all three intervals, both MA families, a custom ladder, every rule family, an
   explicit window, a clamped start, both out-of-range windows, and non-default
   cost/lookback/threshold. A separate test asserts the matrix still mentions every rule
   family, so coverage cannot silently shrink.
2. **A self-check in every generated page.** The file embeds Python's own results for its
   default spec and re-runs them on load, showing a green or red banner. A file opened
   months after the Python side moved on still states whether it can be trusted.

The parity test needs `node`, which is not a project dependency, so it **skips** when node
is absent. That is a real gap: on a machine without node, `pytest` passes with the drift
guard switched off. Accepted rather than solved (adding a JS runtime as a hard dependency
costs more than it saves for a personal tool), and the page-level self-check exists partly
to cover it.

## Consequences

- **`engine.js` is not free to diverge from `backtest.py` + `rules.py`.** Adding a rule
  family, changing a fill rule, or altering the warm-up means editing both. The parity test
  is the reminder; treat a parity failure as "the port is stale", not as a flaky test.
- Some semantics had to be reproduced by hand and are the likeliest place for a future bug:
  pandas weeks run Mon..Sun; SMA is null until `period` observations; EMA with
  `adjust=False` recurses from `x[0]` but is likewise null until `period`; a bar keeps the
  last real trading date in its bucket. `test_js_helpers_match_pandas_semantics` pins each
  one directly rather than only through the grid.
- **Charts have two renderers, and plotly.js is the default** (amended 2026-07-30; the
  first cut shipped SVG-only and argued that zoom was not worth the weight — the user asked
  for zoom, which settles it).
  - `plotly` — the bundle inlined, giving drag/scroll zoom, pan, range buttons, unified
    hover and PNG export. Equity and price are two subplots on a **shared x-axis**
    (`xaxis.matches: 'x2'`), so zooming either one zooms both. That coupling is the real
    reason to pay the weight: the question you ask of a backtest is "what was price doing
    during that drawdown?", and two independently-zoomable panels cannot answer it.
  - `svg` (`--svg-charts`) — the hand-rolled fallback: crosshair and tooltip, no zoom, but
    the whole file stays ~220KB.
  The measured cost is **4.85MB vs 0.22MB** — 22x. Only the full plotly bundle (4.63MB)
  ships with the Python package; the ~1MB `plotly.js-basic` partial bundle is npm-only, so
  using it would mean a JS build step or a vendored blob. Not worth it here, but that is the
  lever if the size ever becomes the problem.
  One `ui.js` serves both builds: the plotly path is guarded by a runtime
  `window.Plotly` check, so the lean build degrades rather than breaking.
- The equity curve is computed on demand for the selected cell, never for the whole grid, so
  a 484-cell sweep does not build 484 curves. Python has no public curve API, so parity
  cannot cover it; instead the curve is tested against the metrics it must agree with
  (its own total return, trade count and buy-and-hold).
- **Self-containment is asserted by behaviour, not by string matching.** Once a 4.6MB
  third-party bundle is inlined, "the file contains no `fetch(`" stops being achievable —
  plotly carries map/topojson code paths that scatter traces never touch. So the static scan
  is scoped to code we wrote (the bundle is subtracted first), and the real claim is tested
  by loading both builds in headless Chrome with **all DNS blackholed**
  (`--host-resolver-rules=MAP * ~NOTFOUND`) and requiring the self-check banner to go green.
  A separate Chrome-gated test performs an actual zoom and asserts both axes moved to the
  same range, so the shared-axis coupling cannot silently regress.
- Embedded history is trimmable (`--html-years`) because a 14-year cache should not force a
  14-year page. Hindsight selection (ADR-0004) is *worse* here than on the CLI, since the
  UI makes sweeping `all × all` a single click, so the in-sample caveat and the "N beat buy
  &amp; hold" count are always on screen.
