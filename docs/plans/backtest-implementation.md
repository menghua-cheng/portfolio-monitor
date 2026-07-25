# Backtest — Implementation Plan

Everything traces to `CONTEXT.md` (Signal, Backtest, N-fold cross, Strategy,
Round-trip trade, Fill, Buy-and-hold) and the four ADRs in `docs/adr/`. Ordered
so each phase is independently testable; the pure engine lands and is fully
tested before any wiring.

## Phase 0 — Verify the one load-bearing assumption
The feature assumes the in-memory `df` in `pipeline._process_ticker` carries
`open`, `close`, and `adj_close` columns (ADR-0002 needs all three for the
adjusted-open fill).
- Read `fetch.py` (`fetch_history` / `to_price_rows`) and confirm the returned
  frame has those columns. If `adj_close` isn't present, the fill basis decision
  must change — stop and flag before coding.

## Phase 1 — Config surface
- `config/settings.yaml`: add a `backtest:` block → `cost_bps: 5`,
  `starting_cash: 10000`.
- `config.py`: add `Config` properties `backtest_cost_bps` (float, default 5)
  and `backtest_starting_cash` (float, default 10000). Window reuses the existing
  `recent_window_days` — no new window knob.

## Phase 2 — Pure engine: `src/portfolio_monitor/backtest.py`
No I/O, no DB, no rendering — takes DataFrames, returns dataclasses.

Dataclasses:
- `StrategyResult(entry_degree, exit_degree, total_return, cagr, max_drawdown,
  num_trades, win_rate, has_open_trade)`
- `TickerBacktest(symbol, window_start, window_end, best: StrategyResult | None,
  buy_hold_return, buy_hold_cagr, all_results: list[StrategyResult])`

Functions (each maps to a decided rule):
- `_adjusted(df)` → adds `adj_open = open × adj_close ÷ close`, `adj_c = adj_close`
  (ADR-0002).
- `_window_start(ind)` → first bar index where all MAs incl. `sma240` are non-null
  (common all-warm start).
- `_degree_confirmed(df, ind, degree, direction, window_days)` → boolean per bar:
  all of the fastest-N pairs (`signals._ADJ_PAIRS[:degree]`, already fast→slow)
  had a `direction` cross within the trailing window, any order (ADR-0001).
  Reuses `signals._pair_cross_events`.
- `_run_one(df, ind, N, M, cfg)` → trade engine: from `window_start`, long-only
  single-position; enter at next bar's adjusted open when degree-N up confirmed
  & flat, exit at next adjusted open when degree-M down confirmed & long;
  `cost_bps` on each fill; build a daily mark-to-market equity curve; open
  position at last bar → MTM at final `adj_close`. Derives total return / CAGR
  (calendar days ÷ 365.25) / max drawdown / trade count / win rate.
- `_buy_hold(df, ind, cfg)` → buy at `window_start` fill basis, hold to last
  `adj_close`, same window.
- `run_backtest(df, ind, cfg)` → compute all 16, pick best by CAGR among
  strategies with ≥1 trade, tie-break lower max-drawdown → fewer trades; if none
  traded → `best=None`.

Edge cases: entry confirmed on the final bar (no next open) → no fill; zero-trade
strategy → excluded from "best"; win rate counts the open MTM trade by unrealized
P&L.

## Phase 3 — Engine tests: `tests/test_backtest.py`
Follow `test_signals.py`'s synthetic-frame style, but frames include `open`,
`close`, `adj_close`. Assert:
- degree-2 entry fires only once both fastest pairs crossed up within the window;
- fill lands on the next bar's open, and a synthetic split (close≠adj_close) is
  corrected by the adjustment;
- a hand-built round-trip yields the expected return net of 5 bps;
- buy-and-hold matches a direct adj_close ratio;
- best-strategy selection + tie-break;
- zero-trade ticker → `best is None`;
- open-at-end → MTM into total return.

## Phase 4 — Report view model + template
- `report.py`: add `BacktestView` dataclass (bilingual best-strategy label e.g.
  `"Entry×2 / Exit×1"`, preformatted metric strings, `bh_return`, window dates,
  a bilingual hindsight disclaimer, and a `no_trades` flag). Add
  `backtest: BacktestView | None = None` to `TickerView`; populate it in
  `build_ticker_view` via a new optional param.
- `templates/report.html.j2`: inject a compact block in the detail card right
  after the `ma_cells` table (after line ~116), using the `t(en, zh)` macro for
  every string. Renders best strategy + its 5 metrics + vs-B&H, the disclaimer,
  or a "no trades / insufficient history" note. No new Plotly chart — renders
  correctly in the email's `lang_mode="en"` path.

## Phase 5 — Pipeline wiring
- `pipeline._process_ticker`: after `ind` is computed, call
  `backtest.run_backtest(df, ind, cfg)`, map to a `BacktestView`, pass into
  `build_ticker_view`. Reuses in-scope DataFrames — no refetch, no new DB table
  (ADR-0003). The per-ticker try/except already isolates failures.

## Phase 6 — End-to-end verification
- Run `PYTHONPATH=src python -m portfolio_monitor.pipeline --no-email --tickers AAPL`
  and open the generated report; confirm the block renders, the EN/中文 switcher
  toggles it, and the disclaimer shows.
- Run `pytest` — expect existing ~20 green plus the new `test_backtest.py`.
- Spot-check one ticker's "best" number by hand against the equity math.

## Risk notes
- `_pair_cross_events` is private — reuse in-package, or promote a public wrapper
  in `signals.py`.
- Warm-up eats history: degree-4 needs `sma240` warm, so a 2-year window leaves
  ~1 year tradable; some (ticker, strategy) cells legitimately have no trades.
- Compute is trivial at this scale, so ephemeral daily recompute is fine.
