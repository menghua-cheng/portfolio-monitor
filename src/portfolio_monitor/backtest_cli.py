"""Backtest explorer CLI (feature: 回測探索).

The daily report shows one hindsight-best strategy per ticker over whatever
history happens to be on disk. This command is the *exploratory* counterpart
(ADR-0005): pick the window, pick the time scale, pick which signals buy and
sell, and see how each watchlist ticker would have done.

    portfolio-monitor-backtest                                   # all tickers, defaults
    portfolio-monitor-backtest AAPL --start 2021-01-01 --end 2023-12-31
    portfolio-monitor-backtest AAPL --interval weekly --years 12 --refresh
    portfolio-monitor-backtest AAPL --entry cross:sma20/sma60 --exit price:sma20
    portfolio-monitor-backtest AAPL --entry all --exit all --top 15
    portfolio-monitor-backtest --list-rules

Prices come from the local SQLite `prices` table by default (no network). Pass
`--refresh` to fetch fresh history first — needed when the window you ask for
predates what the daily pipeline has stored (it keeps `history_years`, 2 by
default). Indicators are always recomputed here at the requested interval and
ladder, so nothing depends on the stored indicator rows.

Results are in-sample by construction: sweeping a grid and reading off the best
row is hindsight selection (ADR-0004). Treat the top line as a ceiling, and
compare strategies against the buy-and-hold row rather than in absolute terms.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import date, timedelta

import pandas as pd

from . import backtest, bars, config, db, rules
from .backtest import BacktestSpec, StrategyResult, TickerBacktest

log = logging.getLogger("portfolio_monitor.backtest_cli")

_PRICE_COLS = ["date", "open", "high", "low", "close", "adj_close", "volume", "source"]

_SORTS = {
    "cagr": lambda r: (-r.cagr, r.max_drawdown, r.num_trades),
    "return": lambda r: (-r.total_return, r.max_drawdown, r.num_trades),
    "drawdown": lambda r: (r.max_drawdown, -r.cagr),
    "trades": lambda r: (r.num_trades, -r.cagr),
    "winrate": lambda r: (-r.win_rate, -r.cagr),
}


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _rows_to_frame(rows) -> pd.DataFrame:
    """SQLite price rows -> the tidy frame the engine expects."""
    if not rows:
        return pd.DataFrame(columns=_PRICE_COLS)
    df = pd.DataFrame([dict(r) for r in rows])
    for c in _PRICE_COLS:
        if c not in df.columns:
            df[c] = None
    return df[_PRICE_COLS].sort_values("date").reset_index(drop=True)


def _needed_years(spec_start: str | None, interval: str, ladder, extra: float = 1.0) -> int:
    """History to fetch: the span the user wants to trade, plus the ladder's
    warm-up, plus a small margin. Used as the `--years` default so a distant
    `--start` doesn't silently trade on a half-warm ladder."""
    warmup = bars.min_history_years(interval, ladder)
    span = 0.0
    if spec_start:
        span = max(0.0, (date.today() - pd.Timestamp(spec_start).date()).days / 365.25)
    return max(2, int(round(span + warmup + extra)))


def _load_prices(conn, symbol: str, years: int, refresh: bool) -> tuple[pd.DataFrame, str]:
    """Price history for one symbol, always served from the local cache.

    `--refresh` syncs first. That sync is *incremental* when the cache already
    reaches `years` back — only the tail downloads — and a full fetch when it
    doesn't, which is what makes asking for a deeper window actually deepen the
    cache. Returns (frame, source-note).
    """
    if refresh:
        from . import cache  # local import keeps yfinance off the fast path
        lo, _hi, _n = db.price_span(conn, symbol)
        want = (date.today() - timedelta(days=int(years * 365.25))).isoformat()
        force = lo is None or lo > want          # cache doesn't go deep enough yet
        res = cache.sync_prices(conn, symbol, years=years, force=force)
        note = f"{res.action}: {res.note}"
    else:
        note = "cached"
    return _rows_to_frame(db.get_prices(conn, symbol)), note


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _pct(x: float, sign: bool = True) -> str:
    return f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%"


def _row(r: StrategyResult) -> list[str]:
    return [r.entry_label, r.exit_label, _pct(r.total_return), _pct(r.cagr),
            f"-{r.max_drawdown * 100:.1f}%", str(r.num_trades),
            _pct(r.win_rate, sign=False) + (" *" if r.has_open_trade else "")]


_HEAD = ["entry", "exit", "return", "CAGR", "maxDD", "trades", "win%"]


def _table(rows: list[list[str]], head: list[str]) -> str:
    widths = [max([len(head[i])] + [len(r[i]) for r in rows]) for i in range(len(head))]
    def line(cells):
        return "  ".join(c.ljust(widths[i]) if i < 2 else c.rjust(widths[i])
                         for i, c in enumerate(cells))
    out = [line(head), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _render(bt: TickerBacktest, name: str, sort_key: str, top: int) -> str:
    ladder = "/".join(str(p) for p in bt.ma_periods)
    title = f"{bt.symbol}" + (f" — {name}" if name else "")
    head = [f"\n{'=' * 72}", title, f"{'=' * 72}"]

    if bt.window_start is None:
        head.append(f"  no result: {bt.note or 'insufficient history'}")
        return "\n".join(head)

    head.append(f"  window   {bt.window_start} → {bt.window_end}  "
                f"({bt.num_bars} {bt.interval} bars)")
    head.append(f"  MA lines {ladder}   data from {bt.data_start}")
    if bt.note:
        head.append(f"  note     {bt.note}")
    if bt.buy_hold_return is not None:
        head.append(f"  buy&hold {_pct(bt.buy_hold_return)} total, "
                    f"{_pct(bt.buy_hold_cagr)} CAGR")

    traded = [r for r in bt.all_results if r.num_trades > 0]
    if not traded:
        head.append(f"  no strategy in the {len(bt.all_results)}-cell grid ever traded "
                    f"in this window")
        return "\n".join(head)

    ranked = sorted(traded, key=_SORTS[sort_key])
    shown = ranked[:top] if top > 0 else ranked
    body = _table([_row(r) for r in shown], _HEAD)
    foot = [f"  {len(traded)} of {len(bt.all_results)} grid cells traded; "
            f"showing {len(shown)} by {sort_key}."]
    if any(r.has_open_trade for r in shown):
        foot.append("  * position still open at window end (marked to market).")
    beat = sum(1 for r in traded if bt.buy_hold_return is not None
               and r.total_return > bt.buy_hold_return)
    foot.append(f"  {beat}/{len(traded)} traded cells beat buy-and-hold. "
                f"In-sample — hindsight-selected, not a forward recommendation.")
    return "\n".join(head + ["", body, ""] + foot)


def _as_dict(bt: TickerBacktest, name: str) -> dict:
    return {
        "symbol": bt.symbol, "name": name, "interval": bt.interval,
        "ma_periods": list(bt.ma_periods), "window_start": bt.window_start,
        "window_end": bt.window_end, "num_bars": bt.num_bars,
        "data_start": bt.data_start, "note": bt.note,
        "buy_hold_return": bt.buy_hold_return, "buy_hold_cagr": bt.buy_hold_cagr,
        "results": [
            {"entry": r.entry_label, "exit": r.exit_label,
             "total_return": r.total_return, "cagr": r.cagr,
             "max_drawdown": r.max_drawdown, "num_trades": r.num_trades,
             "win_rate": r.win_rate, "has_open_trade": r.has_open_trade}
            for r in bt.all_results
        ],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="portfolio-monitor-backtest",
        description="Explore MA-signal backtests over a chosen window, time scale "
                    "and signal pair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Rules:\n  " + "\n  ".join(rules.RULE_HELP.values()) +
               "\n\nGroups: degrees, multis, crosses, prices, slopes, all")
    p.add_argument("symbols", nargs="*", metavar="SYMBOL",
                   help="Tickers to test; default is the whole watchlist.")
    p.add_argument("--start", help="First tradable date (ISO). Clamped later if the "
                                   "MA ladder is not warm yet.")
    p.add_argument("--end", help="Last bar to include (ISO).")
    p.add_argument("--interval", default="daily",
                   help="Bar time scale: daily | weekly | monthly (default daily).")
    p.add_argument("--ma-periods", help="MA ladder in bars, e.g. 5,20,60,120,240. "
                                       "Default is the interval's ladder.")
    p.add_argument("--ma-kind", default="sma", choices=("sma", "ema"),
                   help="Which moving-average family the rules read.")
    p.add_argument("--entry", default="degrees",
                   help="Entry rules to sweep (comma-separated). Default: degrees.")
    p.add_argument("--exit", dest="exit_", default="degrees",
                   help="Exit rules to sweep (comma-separated). Default: degrees.")
    p.add_argument("--window-days", type=int, help="Cascade lookback in calendar days.")
    p.add_argument("--cost-bps", type=float, help="Per-side trading cost, basis points.")
    p.add_argument("--years", type=int,
                   help="History to fetch with --refresh (default: enough to cover "
                        "--start plus the ladder's warm-up).")
    p.add_argument("--refresh", action="store_true",
                   help="Fetch fresh history before testing (needed for windows older "
                        "than the stored history).")
    p.add_argument("--sort", default="cagr", choices=tuple(_SORTS),
                   help="Rank the grid by this metric (default cagr).")
    p.add_argument("--top", type=int, default=10,
                   help="Show this many grid rows per ticker; 0 = all.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of tables.")
    p.add_argument("--html", nargs="?", const="", metavar="PATH",
                   help="Write a standalone interactive HTML explorer instead of a "
                        "table. Recomputes in the browser — no server, no network. "
                        "Optional PATH; defaults to reports/backtest-explorer-<date>.html.")
    p.add_argument("--html-years", type=int, metavar="N",
                   help="Trim the history embedded in --html to N years, to keep the "
                        "file portable (default: everything cached).")
    p.add_argument("--svg-charts", action="store_true",
                   help="Draw the --html charts as plain SVG instead of inlining "
                        "plotly.js: no zoom or pan, but ~220KB instead of ~4.9MB.")
    p.add_argument("--list-rules", action="store_true",
                   help="Print the available signal rules and exit.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _spec_from_args(args, cfg: config.Config) -> BacktestSpec:
    """Build the spec: CLI flags override settings.yaml, which overrides defaults."""
    interval = bars.normalize_interval(args.interval)
    ladder = (tuple(int(x) for x in args.ma_periods.replace(" ", "").split(",") if x)
              if args.ma_periods else bars.default_ladder(interval))
    base = BacktestSpec(
        start=args.start, end=args.end, interval=interval, ma_periods=ladder,
        ma_kind=args.ma_kind,
        window_days=args.window_days if args.window_days is not None
        else cfg.recent_window_days,
        cost_bps=args.cost_bps if args.cost_bps is not None else cfg.backtest_cost_bps,
        starting_cash=cfg.backtest_starting_cash,
        slope_lookback=cfg.slope_lookback,
        flat_threshold_pct=cfg.flat_threshold_pct,
    )
    ctx = base.context()
    return dataclasses.replace(base,
                               entries=tuple(rules.parse_rules(args.entry, ctx)),
                               exits=tuple(rules.parse_rules(args.exit_, ctx)))


def _cli(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.list_rules:
        print("Signal rules (entry rules read 'up', exit rules read 'down'):")
        for kind in sorted(rules.RULE_HELP):
            print(f"  {rules.RULE_HELP[kind]}")
        print("\nGroup tokens: degrees, multis, crosses, prices, slopes, all")
        print("Default ladders per interval:")
        for iv, ladder in bars.DEFAULT_LADDERS.items():
            print(f"  {iv:8s} {', '.join(str(p) for p in ladder)}")
        return 0

    cfg = config.load_config()
    try:
        spec = _spec_from_args(args, cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.html is not None:
        from pathlib import Path as _Path

        from . import explorer
        out = _Path(args.html) if args.html else None
        syms = [s.upper() for s in args.symbols] or None
        with db.connect() as conn:
            try:
                path, size = explorer.write_html(
                    conn, cfg, out=out, symbols=syms, max_years=args.html_years,
                    charts="svg" if args.svg_charts else "plotly")
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        mode = "SVG charts" if args.svg_charts else "plotly.js inlined, zoom/pan"
        print(f"Interactive explorer: {path}  ({size / 1024 / 1024:.2f} MB, "
              f"self-contained, {mode})")
        print("Open it in a browser — it recomputes locally, no server needed.")
        return 0

    wanted = {s.upper() for s in args.symbols}
    tickers = [(t.symbol, t.name) for t in cfg.tickers
               if not wanted or t.symbol in wanted]
    # Allow testing a symbol that isn't on the watchlist, as long as we can fetch it.
    for s in sorted(wanted - {sym for sym, _ in tickers}):
        tickers.append((s, ""))
    if not tickers:
        print("error: no tickers (watchlist is empty)", file=sys.stderr)
        return 1

    years = args.years or _needed_years(spec.start, spec.interval, spec.ladder)
    payload = []
    exit_code = 0
    with db.connect() as conn:
        for symbol, name in tickers:
            prices, src = _load_prices(conn, symbol, years, args.refresh)
            if prices.empty:
                msg = (f"{symbol}: no price history stored — run the daily pipeline "
                       f"or pass --refresh")
                print(msg, file=sys.stderr)
                exit_code = 1
                continue
            bt = backtest.run_spec(prices, symbol, spec)
            if spec.start and bt.data_start and spec.start < str(bt.data_start) \
                    and not args.refresh:
                bt.note = "; ".join(filter(None, [
                    bt.note,
                    f"stored history starts {bt.data_start}; --refresh --years "
                    f"{years} to reach {spec.start}"]))
            log.info("%s: %s (%s)", symbol, src, bt.note or "ok")
            if args.json:
                payload.append(_as_dict(bt, name))
            else:
                print(_render(bt, name, args.sort, args.top))
    if args.json:
        print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(_cli())
