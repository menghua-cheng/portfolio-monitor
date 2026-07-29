"""Tracker CLI: daily portfolio performance + signal tracker (feature: 績效追蹤).

    portfolio-monitor-tracker                    # terminal table, cached data
    portfolio-monitor-tracker --html             # also write reports/tracker-<date>.html
    portfolio-monitor-tracker --sync             # pull new bars first
    portfolio-monitor-tracker --days 180 --json

Reads the cached `prices` and `signals` tables, so a run costs nothing in network
terms unless `--sync` is passed. The daily pipeline generates the same HTML at the
end of its run, so this command is for looking at a different window (or a
different lookback) between runs.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import cache, config, db, tracker, tracker_report

log = logging.getLogger("portfolio_monitor.tracker_cli")

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = _REPO_ROOT / "reports"


def load_inputs(conn, cfg, history_days: int | None = None):
    """Cached prices + watchlist names + signal rows, ready for tracker.build_report.

    `history_days` bounds the price read: the tracker never needs more than its
    longest horizon plus the 52-week high window, and the cache may hold years
    more than that.
    """
    names = {t.symbol: t.name for t in cfg.tickers}
    symbols = list(names) or db.tickers_with_prices(conn)
    start = None
    if history_days:
        start = (date.today() - timedelta(days=history_days)).isoformat()
    prices = {s: cache.rows_to_frame(db.get_prices(conn, s, start=start)) for s in symbols}
    signals = conn.execute(
        "SELECT ticker, date, signal_type, detail FROM signals ORDER BY date DESC"
    ).fetchall()
    return prices, names, signals


def build(conn, cfg, lookback_days: int = 90, index_days: int = 180,
          as_of: str | None = None) -> tracker.TrackerReport:
    """The whole tracker for the current watchlist, from cached data."""
    # 52-week high needs a year; the 1y horizon needs a year plus a bar before it.
    history_days = max(400, lookback_days + 30, index_days + 30)
    prices, names, signals = load_inputs(conn, cfg, history_days)
    return tracker.build_report(prices, names, signals, lookback_days=lookback_days,
                                index_days=index_days, as_of=as_of)


def write_html(view: tracker_report.TrackerView, out: Path | None = None) -> Path:
    """Render and save the bilingual tracker report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = out or (REPORTS_DIR / f"tracker-{view.as_of or date.today().isoformat()}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tracker_report.render_html(view), encoding="utf-8")
    return path


def _as_json(rep: tracker.TrackerReport) -> str:
    payload = {
        "as_of": rep.as_of, "lookback_days": rep.lookback_days, "note": rep.note,
        "tickers": [asdict(p) for p in rep.tickers],
        "portfolio": asdict(rep.portfolio),
        "signals": [asdict(s) for s in rep.signals],
        "score": {**asdict(rep.score), "hit_rate": rep.score.hit_rate},
        "per_ticker_score": {k: {**asdict(v), "hit_rate": v.hit_rate}
                             for k, v in rep.per_ticker_score.items()},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="portfolio-monitor-tracker",
        description="Daily portfolio performance and signal tracker, from the "
                    "local price cache.")
    p.add_argument("--days", type=int, default=90,
                   help="Lookback for the signal tracker, in days (default 90).")
    p.add_argument("--index-days", type=int, default=180,
                   help="Window for the equal-weight index sparkline (default 180).")
    p.add_argument("--as-of", help="Treat this ISO date as 'today' (defaults to the "
                                   "newest cached bar).")
    p.add_argument("--sync", action="store_true",
                   help="Pull new bars into the cache before building the report.")
    p.add_argument("--html", action="store_true",
                   help="Also write the bilingual HTML report.")
    p.add_argument("--out", type=Path, help="Path for the HTML report (implies --html).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p.add_argument("--max-signals", type=int, default=25,
                   help="Signal rows to print in the terminal table (default 25).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _cli(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load_config()

    with db.connect() as conn:
        if args.sync:
            for t in cfg.tickers:
                res = cache.sync_prices(conn, t.symbol, years=cfg.history_years)
                log.info("%s: %s (%s)", t.symbol, res.action, res.note)
        rep = build(conn, cfg, lookback_days=args.days,
                    index_days=args.index_days, as_of=args.as_of)

    if args.json:
        print(_as_json(rep))
    else:
        view = tracker_report.build_view(rep)
        print(tracker_report.render_text(view, max_signals=args.max_signals))
        if args.html or args.out:
            print(f"\nHTML: {write_html(view, args.out)}")
        return 0 if rep.as_of else 1

    if args.html or args.out:
        print(f"HTML: {write_html(tracker_report.build_view(rep), args.out)}",
              file=sys.stderr)
    return 0 if rep.as_of else 1


if __name__ == "__main__":
    sys.exit(_cli())
