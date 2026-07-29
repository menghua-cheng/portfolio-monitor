# portfolio-monitor

A daily monitor for a US-stock watchlist. It fetches OHLCV history, computes
SMA/EMA moving-average lines (5/20/60/120/240 trading days = week/month/quarter/
half-year/year), detects trend transitions, renders interactive charts, builds an
HTML report, optionally emails it, and stores everything in a local SQLite
database. Notes are bilingual (English / 繁體中文).

I run it from cron after the US close and read the report in a browser.

## Screenshots

The daily report. The summary table shows each ticker's trend state, MA
alignment, and any multi-line breakout tags, with the recent crossings listed
underneath:

![Report summary](docs/screenshots/overview.png)

The interactive chart: moving-average lines, a marker at every cross (up-triangle
for a breakout, down-triangle for a breakdown), a shaded band over multi-line
clusters, and a hover popup naming each constituent crossing and its date:

![Interactive chart with breakout detail](docs/screenshots/chart-hover.png)

The report is bilingual; the top-right control switches between English and
繁體中文, and the charts follow:

![Chinese view](docs/screenshots/overview-zh.png)

## What it does

1. Watchlist kept in a CSV (`config/portfolio.csv`), managed by a small CLI;
   adding a symbol auto-fills the company name (Tiingo, then yfinance).
2. Daily OHLCV + volume fetch from Yahoo Finance (via `yfinance`), with an
   optional independent Tiingo cross-check and a set of internal data-quality
   checks (NaN, non-positive prices, non-monotonic/duplicate dates, high < low,
   staleness, implausible daily moves).
3. Moving averages: SMA and EMA for each period.
4. Signals:
   - Golden/death cross and long/short trend-state transitions, emitted only on
     the bar where the state actually changes.
   - Per-adjacent-pair cross detail (e.g. "月線向上突破季線"), with a dual-trend
     note when a related same-direction cross fired recently.
   - An always-present trend summary per ticker: MA alignment
     (多頭排列 / 空頭排列 / 多空交錯), multi-line breakout/breakdown tags
     (雙重・三重・四重突破／跌破) over a lookback window, and a recent-crossings
     list with days-ago.
5. Report: a self-contained UTF-8 HTML file with interactive Plotly charts
   (hover shows date/OHLC/SMA/EMA/volume) and a top-right English / 中文 switcher
   (default English). Each crossing in view is marked on the chart (▲ breakout /
   ▼ breakdown) with a hover popup naming the lines and the date; multi-line
   clusters get a shaded highlight band and a star label whose popup lists the
   constituent crossings. A static PNG is kept as a no-JS fallback and for email.
6. Signal backtest: replays each ticker's own MA-cross signals over history and
   reports, per ticker, the best of 16 strategies vs buy-and-hold. A strategy is
   an `(entry degree N, exit degree M)` pair, where degree N means the fastest N
   adjacent MA pairs have all crossed the same direction within the lookback
   window (1=single … 4=quad). Trades are long-only, filled at the next bar's
   split/dividend-adjusted open, compounded, and netted a small per-side cost;
   the best strategy is chosen by CAGR and clearly labeled as hindsight-selected,
   not a forward recommendation. Shown as a compact bilingual block under each
   ticker in the report. See `docs/adr/` for the design rationale.
7. Backtest explorer (`portfolio-monitor-backtest`): the same engine, driven from
   the command line, so you can change the **window** (`--start` / `--end`), the
   **time scale** (`--interval daily|weekly|monthly`), and **which signals buy and
   sell** (`--entry` / `--exit`, from a rule registry: N-fold cascades, the
   雙重/三重/四重突破・跌破 multi-break counts the report tags, single MA pair
   crosses, price-vs-MA, stack alignment, MA slope). Prints the grid ranked by any
   metric, with buy-and-hold for the same window. See
   [Exploring backtests](#exploring-backtests).
8. Interactive explorer: `portfolio-monitor-backtest --html` writes **one static HTML
   file** that recomputes backtests in your browser — change the window, interval,
   MA ladder or rules and the grid re-runs locally in milliseconds. No server, no
   network, works from `file://`. See [Interactive explorer](#interactive-explorer).
9. Performance & signal tracker (`portfolio-monitor-tracker`, also written by every
   daily run): per-ticker and equal-weight-portfolio total returns over
   1D/1W/1M/3M/6M/1Y/YTD, distance off the 52-week high, an equal-weight index
   sparkline, and every recorded signal with what price did *since* it fired plus a
   directional hit rate. See [Performance & signal tracker](#performance--signal-tracker).
10. Incremental price cache: SQLite is the source of truth, so a run downloads only
   bars newer than what it holds instead of re-pulling all history from Yahoo and
   Tiingo every time. A **stock split** silently rewrites historical closes
   upstream, so each sync re-checks a small overlap and rescales the stored history
   rather than appending a fake 75% crash onto it. See
   [Price cache](#price-cache).
11. Email: Gmail SMTP with the chart inlined as a PNG. Dry-run by default (writes
   an `.eml` for inspection). With `--send`, if SMTP is not configured the email
   step is skipped rather than failing, so a cron job stays green.
12. Storage: SQLite at `data/portfolio.db`, all writes idempotent.

## Requirements

- Python 3.11, in a local venv at `.venv`. Managed with [uv](https://docs.astral.sh/uv/) (recommended).

## Setup

```bash
uv sync                                                 # creates .venv, installs the project

cp config/portfolio.example.csv config/portfolio.csv    # then edit your holdings
cp .env.example .env                                     # optional, for email/Tiingo
```

`uv sync` installs the package itself (from `pyproject.toml`), so there is **no
`PYTHONPATH` to set** — the commands below run from anywhere in the repo.

```bash
uv run portfolio-monitor --help
```

`config/portfolio.csv` and `.env` are gitignored. Until you create
`portfolio.csv`, the loader falls back to `portfolio.example.csv`.

<details><summary>…without uv (plain venv + pip)</summary>

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -e .          # or: pip install -r requirements.txt
./.venv/bin/portfolio-monitor --help
```

If you install with `requirements.txt` instead of `-e .`, the package isn't
installed, so run modules with `PYTHONPATH=src ./.venv/bin/python -m …` or use
`scripts/run_daily.sh`, which sets `PYTHONPATH` itself.
</details>

## Configuration

Program settings are in `config/settings.yaml`. The watchlist is
`config/portfolio.csv` (`symbol,name` per row).

| Key | Meaning |
|-----|---------|
| `history_years` | How much history the daily report fetches and works on |
| `ma_periods` | The MA ladder for the daily report (week…year) |
| `signals.slope_lookback`, `signals.flat_threshold_pct` | Long-term trend slope classification |
| `signals.double_window_days` | Window for tagging a second cross as a dual-trend signal |
| `signals.recent_window_days` | Window for the trend summary and the backtest cascade |
| `backtest.cost_bps` | Per-side trading cost applied to every backtest fill |
| `backtest.starting_cash` | Notional capital for the equity curve (metrics are scale-free) |
| `tracker.lookback_days` | How far back the tracker scores recorded signals (default 90) |
| `tracker.index_days` | Window for the tracker's equal-weight index sparkline (default 180) |
| `cache.overlap_days` | Cached bars an incremental sync re-checks for a split/dividend re-basing (default 12) |
| `data.crosscheck_tolerance_pct` | Allowed yfinance-vs-Tiingo close difference |

The backtest explorer reads the same `backtest` settings as defaults; its window,
interval, MA ladder and signal rules are per-run flags rather than settings, so
exploring never means editing a file. Per-interval MA ladders live in
`src/portfolio_monitor/bars.py` and are overridable with `--ma-periods`.

Secrets go in `.env` (see `.env.example`):

| Variable | Purpose |
|----------|---------|
| `SMTP_USER` | Gmail address that sends the report |
| `SMTP_APP_PASSWORD` | Google App Password (not the login password) |
| `REPORT_RECIPIENT` | Where the report is delivered |
| `REPORT_CC` | Optional, comma-separated |
| `TIINGO_API_KEY` | Optional free key; enables the yfinance-vs-Tiingo cross-check |

### Gmail App Password

Enable 2-Step Verification, then create an App Password under
Google Account -> Security -> App passwords ("Mail"), and put the 16-character
value in `SMTP_APP_PASSWORD`. Leave `SMTP_USER`/`SMTP_APP_PASSWORD` blank to keep
the pipeline in dry-run.

### Tiingo key (optional)

Register at <https://www.tiingo.com/>, copy the API token into `TIINGO_API_KEY`.
When set, each fetch is validated against Tiingo's close within
`crosscheck_tolerance_pct`. Without it, the tool runs on yfinance alone plus the
internal checks.

## Managing the watchlist

```bash
uv run portfolio-monitor-config list
uv run portfolio-monitor-config add NVDA AAPL TSM        # one or more; names auto-looked-up
uv run portfolio-monitor-config add NVDA --name "My label"  # single symbol, custom name
uv run portfolio-monitor-config remove NVDA AAPL         # one or more
uv run portfolio-monitor-config sync   # reconcile the DB to the CSV
```

`add` and `remove` accept multiple symbols. When you `add` without `--name`, the
company name is fetched automatically — Tiingo metadata first (needs
`TIINGO_API_KEY`), then yfinance as a fallback; if neither resolves a name, the
symbol is still added with a blank name. `--name` sets a custom name and is only
valid with a single symbol.

## Running

```bash
uv run portfolio-monitor --verbose      # dry-run email
uv run portfolio-monitor --send         # send (needs .env)
uv run portfolio-monitor --no-email     # HTML only
uv run portfolio-monitor --tickers AAPL MSFT
```

Outputs:

- `reports/<date>.html` — self-contained interactive report.
- `reports/tracker-<date>.html` — performance + signal tracker.
- `reports/<date>.eml` — the email message (dry-run) for inspection.
- `reports/charts/<TICKER>.png` — per-ticker static charts.
- `data/portfolio.db` — prices, indicators, signals, run audit.

## Exploring backtests

The daily report shows one hindsight-best strategy per ticker over whatever
history is on hand. `portfolio-monitor-backtest` is the exploratory counterpart:
same engine, but you choose the window, the time scale, and the signals.

```bash
uv run portfolio-monitor-backtest                     # whole watchlist, defaults
uv run portfolio-monitor-backtest AAPL --top 5        # one ticker, top 5 rows
uv run portfolio-monitor-backtest --list-rules        # available signals + ladders
```

**1 — window.** `--start` / `--end` bound the *tradable* window (ISO dates):

```bash
uv run portfolio-monitor-backtest AAPL --start 2025-09-01 --end 2026-03-31
```

Moving averages are computed over all loaded history and only then clipped, so a
chosen start trades on a warm ladder rather than restarting the warm-up. A
`--start` earlier than the first all-warm bar is clamped, and the output says so.
Windows older than the stored history need `--refresh` (see below).

**2 — time scale.** `--interval daily|weekly|monthly` aggregates daily bars
(open=first, high=max, low=min, close=last, volume=sum). MA periods count in
*bars*, so each interval carries its own ladder — weekly `sma20` would be five
months, not one:

| interval | default MA ladder | meaning |
|----------|-------------------|---------|
| `daily` | 5, 20, 60, 120, 240 | week, month, quarter, half-year, year |
| `weekly` | 4, 13, 26, 52, 104 | month, quarter, half-year, year, 2 years |
| `monthly` | 3, 6, 12, 24, 60 | quarter, half-year, year, 2 years, 5 years |

Override with `--ma-periods 10,50,200` (any length; degrees follow the number of
adjacent pairs). The slowest line sets the warm-up cost, so coarse intervals need
much more history than the pipeline stores — fetch it with `--refresh`:

```bash
uv run portfolio-monitor-backtest AAPL --interval weekly --refresh --years 14
uv run portfolio-monitor-backtest AAPL --interval monthly --top 4
```

`--refresh` fetches and upserts into `data/portfolio.db`, so the deeper history
stays cached for later runs. Without it the explorer reads stored rows only and
never touches the network.

**3 — signals.** `--entry` and `--exit` take a comma-separated list of rules; the
grid swept is their product. Entry rules are read upward, exit rules downward:

| rule | fires when |
|------|-----------|
| `degreeN` | the fastest N adjacent MA pairs have all crossed within the lookback window |
| `multiN` | **any** N distinct adjacent pairs have crossed within the window — the 雙重/三重/四重突破・跌破 the report tags. Aliases: `double`, `triple`, `quad` |
| `cross:sma20/sma60` | that MA pair crosses (a golden / death cross) |
| `price:sma20` | the adjusted close crosses that MA |
| `align` | the whole stack flips to 多頭排列 / 空頭排列 |
| `slope:sma240` | that MA's own slope turns up / down |

`degreeN` and `multiN` are close cousins worth keeping straight: degree demands the
fastest N pairs *cumulatively from the short end*, multi accepts any N pairs. Every
degree-N cross is a multi-N cross but not the reverse, so multi fires more often and
earlier — see `docs/adr/0006`.

Numbers may be bare (`cross:20/60`), and `--ma-kind ema` points the rules at the
EMA family. Group tokens expand against the active ladder: `degrees`, `multis`,
`crosses`, `prices`, `slopes`, `all`.

```bash
# buy on a golden cross, sell when price loses the monthly line
uv run portfolio-monitor-backtest AAPL --entry cross:sma20/sma60 --exit price:sma20

# sweep everything and rank by drawdown instead of CAGR
uv run portfolio-monitor-backtest AAPL --entry all --exit all --sort drawdown --top 15

# backtest the report's own 雙重/三重/四重突破 tags
uv run portfolio-monitor-backtest AAPL --entry multis --exit multis
```

Other flags: `--sort cagr|return|drawdown|trades|winrate`, `--top N` (`0` = all),
`--cost-bps`, `--window-days`, `--json` for machine-readable output.

> Results are **in-sample by construction**: sweeping a grid and reading off the
> best row is hindsight selection, and a bigger grid makes that worse, not better.
> Every run prints the buy-and-hold row and how many cells beat it — read those,
> not the top line alone. See `docs/adr/0004` and `docs/adr/0005`.

## Interactive explorer

One static HTML file that runs the backtester in your browser:

```bash
uv run portfolio-monitor-backtest --html                      # reports/backtest-explorer-<date>.html
uv run portfolio-monitor-backtest --html ~/bt.html --html-years 6
uv run portfolio-monitor-backtest AAPL MSFT --html            # embed only these tickers
uv run portfolio-monitor-backtest --html --svg-charts         # 0.22 MB, no zoom
```

Open it from disk — no server, no network, nothing to install. The price history is
embedded as JSON and the engine is inlined, so every control recomputes locally:
ticker, bar interval, MA ladder, MA family, start/end dates, entry and exit rules,
cascade window, per-side cost, ranking and row count. Click any grid row for its
equity curve (vs buy & hold, with ▲ entry / ▼ exit fills marked) and the price chart
with the MA ladder. A 16-cell grid over 1,200 daily bars recomputes in ~3 ms.

**Charts zoom.** plotly.js is inlined, so you get drag-zoom, scroll-zoom, pan,
6m/1y/3y/all range buttons, unified hover and PNG export. Equity and price sit on a
**shared x-axis** — zoom either panel and both follow, which is what you want when
asking "what was price doing during that drawdown?".

| Build | Size (3 tickers × 6y) | Charts |
|-------|----------------------|--------|
| default | ~4.9 MB | plotly.js: zoom, pan, range buttons, unified hover |
| `--svg-charts` | ~0.22 MB | hand-drawn SVG: hover crosshair only, no zoom |

`--html-years N` trims the embedded history, since a 14-year cache shouldn't force a
14-year file. Use `--svg-charts` when portability matters more than zoom (emailing it
to yourself); only the full 4.63 MB plotly bundle ships with the Python package, so
there is no middle size available without a JS build step.

**How it can be wrong, and what stops it.** The browser engine is a *second*
implementation of the Python one (a JS port — see `docs/adr/0009` for why not
sql.js or Pyodide). Two engines can drift, and a drifted page doesn't crash, it just
disagrees with the CLI. So:

- `tests/test_explorer.py` drives **both** engines from one spec matrix over the same
  rounded payload and requires agreement to 1e-9 on every metric of every cell —
  across all three intervals, both MA families, every rule family and the edge-case
  windows. It needs `node`; without node it skips, which means the guard is off.
- Both builds are loaded in headless Chrome with **all DNS blackholed** to prove they
  need no network, and a further test performs a real zoom and asserts both axes
  landed on the same range. Those skip without Chrome.
- Every generated page embeds Python's results for its default spec and re-checks
  them on load, showing a green or red banner at the top. If you ever see the red
  one, regenerate the file and trust the CLI instead.

> Hindsight selection is *worse* here than on the CLI, because sweeping `all × all`
> is one click. The in-sample caveat and the "beat buy & hold" count stay on screen
> for that reason.

## Performance & signal tracker

Every daily run writes `reports/tracker-<date>.html` alongside the main report. The
same thing on demand, in the terminal:

```bash
uv run portfolio-monitor-tracker                  # table, from the cache (no network)
uv run portfolio-monitor-tracker --html           # also write the HTML report
uv run portfolio-monitor-tracker --days 180       # score signals over 180 days
uv run portfolio-monitor-tracker --sync           # pull new bars first
uv run portfolio-monitor-tracker --json           # machine-readable
```

Three sections:

1. **Performance** — per-ticker total return (adjusted, so splits and dividends are
   never mistaken for performance) over 1D/1W/1M/3M/6M/1Y/YTD, plus distance off the
   52-week high. The `PORTFOLIO` row is the **equal-weight** mean: the watchlist has
   no share counts, so equal weight is the only honest reading. A ticker without
   enough history for a horizon is excluded from it rather than assumed flat, which
   is why the tickers-counted line under the table matters.
2. **Equal-weight index** — an inline SVG sparkline of the watchlist normalized to
   100, built only from tickers covering the whole window.
3. **Signals and hit rate** — every recorded signal in the lookback window, what the
   adjusted price has done since it fired, and whether that matched the signal's
   direction. Signals that make no directional claim are listed but not scored.

> The hit rate is a **tracking** measure, not a strategy result: no entries, exits,
> position sizing or costs, and every signal is measured to the latest bar, so older
> signals get a longer runway. It answers "has this ticker's signal flow been
> pointing the right way lately". For tradability, use the backtest. See
> `docs/adr/0008`.

## Price cache

`data/portfolio.db` is the source of truth for prices. A run asks Yahoo (and Tiingo)
only for bars newer than what it already holds, so a daily run downloads a handful of
rows instead of years of history.

```bash
uv run portfolio-monitor-cache status      # what's cached, per ticker and source
uv run portfolio-monitor-cache sync        # pull new bars for the watchlist
uv run portfolio-monitor-cache sync AAPL --years 15 --force   # deepen the cache
uv run portfolio-monitor-cache actions     # splits / re-basings that were detected
```

**Splits are the reason this isn't trivial.** Yahoo's raw close is split-adjusted
retroactively, so the day after a 4:1 split every historical close it serves is a
quarter of yesterday's. Appending new bars onto old ones would leave a 75% one-day
"crash" in the cache that every indicator, signal, backtest and performance figure
downstream would treat as real.

So each incremental sync re-fetches a small overlap of bars it already has
(`cache.overlap_days`, default 12 calendar days) and compares them:

| Overlap comparison | What happens |
|---|---|
| closes agree | append the new tail — the common case |
| closes differ by a consistent ratio | **split**: the stored history is rescaled by that ratio (volume inversely), then the tail is appended |
| closes differ inconsistently | no guessing: the full window is refetched and overwritten |
| only adjusted closes differ | **dividend adjustment**: same rescale path |

Every rescale is recorded in the `corporate_actions` table with its factor, cutoff and
row count, and logged at WARNING — a silent bulk price rewrite would otherwise be
indistinguishable from corruption. Reference (Tiingo/Stooq) history is cached
separately in `prices_ref`, so the daily cross-check compares two cached series.

The cache is never shrunk: `portfolio-monitor-backtest --refresh --years 14` deepens
it permanently, while the daily report keeps reading only its `history_years` window.
See `docs/adr/0007`.

### What it actually saved

Measured on the 3-ticker watchlist. Two things dominated a run, and trimming the
*payload* of a fetch was not one of them:

| Step | Before | After |
|------|--------|-------|
| Data (both sources, per run) | 8.1s | 3.8s while today's bar may still appear; **0.05s** once it is cached |
| Backtest (16-cell grid × 3 tickers) | 3.1s | **0.22s** |
| Whole pipeline, wall clock | ~13s | ~7.9s, ~4s once the day's bar is cached |

- **Fetch latency, not payload, was the data cost.** Asking Yahoo for 12 days costs
  nearly what asking for 2 years costs, so an incremental fetch alone was only ~1.3x
  faster. The real saving is *not asking at all* when the cache already holds the
  newest bar that could exist — which is what makes a second run of the day free.
- **The backtest was recomputing the same work per grid cell.** All 16 cells need the
  same four per-MA-pair "crossed within the window" masks; they were being rebuilt for
  every cell, in a per-bar Python loop. Memoizing them per run and vectorizing the
  window test with `searchsorted` gave ~14x. A 49-cell grid over 13 years of daily
  bars now runs in 0.4s.

## Scheduling

US markets close 16:00 ET; end-of-day data is reliably available a few hours
later. A cron entry running Tue–Sat covers Mon–Fri closes:

```cron
0 6 * * 2-6 /path/to/portfolio-monitor/scripts/run_daily.sh --send
```

Logs go to `logs/daily-<date>.log`; a non-zero exit triggers cron's own mail.
With `--send`, the email step is skipped while SMTP is unconfigured, so it is
safe to install `--send` up front — it starts sending once a Gmail App Password
is set.

## Tests

```bash
uv run pytest -q
```

## Data sources and disclaimer

Primary data is Yahoo Finance via `yfinance`, which is unofficial and can break.
The optional cross-check is Tiingo. This is for personal monitoring only and is
not investment advice.

## Layout

```
pyproject.toml               project metadata, deps, console scripts
uv.lock                      pinned dependency lockfile (uv sync)
config/settings.yaml         program settings (tracked)
config/portfolio.example.csv example watchlist (tracked)
config/portfolio.csv         your watchlist (gitignored)
src/portfolio_monitor/       config, db, fetch, cache, indicators, signals, charts,
                             bars, rules, backtest, backtest_cli, explorer,
                             tracker, tracker_report, tracker_cli, report,
                             email_sender, pipeline
src/portfolio_monitor/static/  engine.js (JS port of the backtest engine), ui.js
templates/report.html.j2     daily report template
templates/tracker.html.j2    tracker report template
templates/explorer.html.j2   interactive explorer template
tests/js/parity_runner.js    drives engine.js under node for the parity test
CONTEXT.md                   domain glossary (ubiquitous language)
docs/adr/                    architecture decision records
docs/plans/                  implementation plans
scripts/run_daily.sh         cron wrapper
tests/                       pytest suite
data/ reports/ logs/         generated (gitignored)
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
