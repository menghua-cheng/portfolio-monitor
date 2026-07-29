"""Daily pipeline orchestration (feature: 每日更新 + 日報 + email).

Flow per run:
    sync watchlist -> for each ticker: fetch -> validate -> store prices ->
    compute+store indicators -> detect+store signals -> render chart
    -> assemble HTML report -> save locally -> email (or dry-run) -> audit row.

CLI:
    python -m portfolio_monitor.pipeline              # run, dry-run email (.eml)
    python -m portfolio_monitor.pipeline --send       # run and actually send email
    python -m portfolio_monitor.pipeline --no-email   # run, skip email entirely
    python -m portfolio_monitor.pipeline --tickers AAPL MSFT
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from . import backtest, charts, config, db, email_sender, indicators, report, signals

log = logging.getLogger("portfolio_monitor.pipeline")

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = _REPO_ROOT / "reports"


def _process_ticker(conn, cfg: config.Config, symbol: str, name: str,
                    chart_dir: Path):
    """Fetch->store->indicators->signals->chart for one ticker.

    The returned view carries a self-contained data-URI chart (so the locally
    saved HTML always renders); the chart_path is returned separately for the
    email's inline (cid) attachment. Returns (view, note, chart_path) or raises.
    """
    from . import cache, fetch  # local imports so tests can stub the network layer

    # Cache-first: only the tail is downloaded, and a split is detected and
    # rescaled rather than appended onto a stale basis (ADR-0007). `window_years`
    # keeps the report on its usual window even when the cache runs far deeper.
    df, cc, sync = cache.load_history(
        conn, symbol, years=cfg.history_years,
        tolerance_pct=cfg.crosscheck_tolerance_pct,
        window_years=cfg.history_years,
        overlap_days=cfg.cache_overlap_days)
    problems = fetch.validate_history(df)
    if problems:
        raise RuntimeError(f"{symbol}: data failed validation: {problems}")

    ind = indicators.compute_indicators(df[["date", "close"]])
    db.upsert_indicators(conn, indicators.to_indicator_rows(symbol, ind))

    detected = signals.detect_signals(df, ind, cfg.slope_lookback,
                                      cfg.flat_threshold_pct)
    for s in detected:
        db.upsert_signal(conn, symbol, s.date, s.signal_type, s.detail)

    # Granular MA-cross events (5日線突破月線 …) + 雙重趨勢訊號 annotations.
    cross_events = signals.detect_cross_events(df, ind, cfg.double_window_days)
    for e in cross_events:
        detail = f"{e.label} | {e.note}" if e.note else e.label
        db.upsert_signal(conn, symbol, e.date, e.signal_type, detail)

    state = signals.current_trend_state(df, ind, cfg.slope_lookback,
                                         cfg.flat_threshold_pct)

    # Always-present trend picture (MA alignment + multi-breakout + recent crosses).
    trend = signals.summarize_trend(df, ind, cfg.recent_window_days)

    # Signal backtest: best (entry N, exit M) strategy vs buy-and-hold, computed
    # ephemerally from the in-scope DataFrames (ADR-0003), hindsight-labeled.
    bt = backtest.run_backtest(df, ind, symbol, window_days=cfg.recent_window_days,
                               cost_bps=cfg.backtest_cost_bps,
                               starting_cash=cfg.backtest_starting_cash)
    backtest_view = report.build_backtest_view(bt)

    chart_path = charts.render_chart(symbol, df, ind, chart_dir)   # PNG failsafe
    chart_html = charts.render_interactive_html(symbol, df, ind)   # interactive
    src = report.chart_src_for(symbol, chart_path, "datauri")
    view = report.build_ticker_view(symbol, name, df, ind, state,
                                     [s.signal_type for s in detected], src,
                                     cross_events=cross_events, chart_html=chart_html,
                                     trend=trend, backtest_view=backtest_view)
    return view, f"{cc.note} [cache: {sync.action}, {sync.note}]", chart_path


def run(email_mode: str = "dryrun", only: list[str] | None = None) -> int:
    """Execute the daily pipeline. email_mode in {"send","dryrun","none"}.

    Returns process exit code (0 ok, 1 failure).
    """
    run_date = date.today().isoformat()
    cfg = config.load_config()
    config.sync_securities()

    tickers = [(t.symbol, t.name) for t in cfg.tickers
               if not only or t.symbol in {s.upper() for s in only}]
    if not tickers:
        log.error("No tickers to process.")
        return 1

    chart_dir = REPORTS_DIR / "charts"
    ctx = report.ReportContext(report_date=run_date)
    chart_paths: dict[str, Path] = {}   # symbol -> png path, for email attachment
    notes: list[str] = []
    failures: list[str] = []
    rows_updated = 0

    with db.connect() as conn:
        for symbol, name in tickers:
            try:
                view, note, chart_path = _process_ticker(conn, cfg, symbol,
                                                          name, chart_dir)
                ctx.tickers.append(view)
                chart_paths[symbol.upper()] = chart_path
                notes.append(f"{symbol}: {note}")
                rows_updated += 1
                log.info("%s processed (%s)", symbol, note)
            except Exception as exc:  # isolate per-ticker failures
                failures.append(f"{symbol}: {exc}")
                log.error("%s FAILED: %s", symbol, exc)

        if not ctx.tickers:
            db.record_run(conn, run_date, "failed", 0, "; ".join(failures))
            log.error("All tickers failed; aborting report.")
            return 1

        keyed = any("tiingo" in n.lower() for n in notes)
        ctx.data_source_note = ("cross-check: Tiingo" if keyed
                                else "cross-check: internal validation only")
        ctx.data_source_note_zh = ("交叉驗證: Tiingo" if keyed
                                   else "交叉驗證: 僅內部檢核")

        # Local HTML: self-contained + interactive (plotly.js inlined once, PNG
        # data-URIs baked in as the <noscript> failsafe).
        ctx.plotly_js = charts.plotly_js_script()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"{run_date}.html"
        report_path.write_text(report.render_report(ctx), encoding="utf-8")
        log.info("Report written: %s", report_path)

        # Performance + signal tracker: a second, standalone artifact built from
        # the same cached tables. Isolated so a tracker failure can never cost the
        # user their daily report.
        try:
            from . import tracker_cli, tracker_report
            rep = tracker_cli.build(conn, cfg, lookback_days=cfg.tracker_lookback_days,
                                    index_days=cfg.tracker_index_days)
            tracker_path = tracker_cli.write_html(tracker_report.build_view(rep))
            log.info("Tracker written: %s", tracker_path)
        except Exception as exc:
            log.error("Tracker report failed (daily report unaffected): %s", exc)

        status_note = "; ".join(notes + (["FAILURES: " + "; ".join(failures)]
                                         if failures else []))
        _deliver(cfg, ctx, chart_paths, run_date, email_mode)
        db.record_run(conn, run_date, "ok" if not failures else "partial",
                      rows_updated, status_note)

    return 0  # partial success still exits 0; total failure returned above


def _deliver(cfg: config.Config, ctx: report.ReportContext,
             chart_paths: dict[str, Path], run_date: str,
             email_mode: str) -> None:
    if email_mode == "none":
        log.info("Email skipped (--no-email).")
        return

    # Send-mode with no SMTP credentials: skip email entirely (no .eml, no send).
    if email_mode == "send" and not cfg.email.configured:
        log.info("Email skipped: SMTP not configured (SMTP_USER/SMTP_APP_PASSWORD/"
                 "REPORT_RECIPIENT missing in .env).")
        return

    # Re-render the report with cid: chart references for inline email images.
    # Email clients strip JavaScript, so use the static PNG (not the interactive
    # chart) and drop the inlined plotly.js. The local HTML file keeps both.
    ctx.plotly_js = ""
    images: list[email_sender.InlineImage] = []
    for view in ctx.tickers:
        path = chart_paths.get(view.symbol.upper())
        if path is None:
            continue
        view.chart_html = None
        view.chart_src = f"cid:{view.symbol.upper()}"
        images.append(email_sender.InlineImage(cid=view.symbol.upper(), path=path))
    # Email can't run the switcher's JS, so render a single fixed language (EN).
    email_html = report.render_report(ctx, lang_mode="en")

    subject = f"US Portfolio Report — {run_date}"
    # In dry-run without a configured recipient we still build an .eml for
    # inspection; fall back to the sender, then a neutral placeholder.
    recipient = cfg.email.recipient or cfg.email.user or "report@example.com"
    msg = email_sender.build_message(
        subject=subject, html=email_html, sender=cfg.email.user or recipient,
        recipient=recipient, cc=cfg.email.cc, images=images)

    if email_mode == "dryrun":
        out = email_sender.save_eml(msg, REPORTS_DIR / f"{run_date}.eml")
        log.info("Dry-run: email written to %s (not sent).", out)
        return

    # email_mode == "send" (SMTP is configured — the unconfigured case returned above)
    recipients = [recipient, *cfg.email.cc]
    email_sender.send_via_gmail(
        msg, host=cfg.email.host, port=cfg.email.port,
        user=cfg.email.user, app_password=cfg.email.app_password,
        recipients=recipients)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portfolio_monitor.pipeline",
                                     description="Run the daily portfolio pipeline.")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--send", action="store_true", help="Actually send the email.")
    g.add_argument("--no-email", action="store_true", help="Skip email entirely.")
    parser.add_argument("--tickers", nargs="*", help="Limit to these symbols.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    mode = "send" if args.send else ("none" if args.no_email else "dryrun")
    try:
        return run(email_mode=mode, only=args.tickers)
    except Exception as exc:
        log.exception("Pipeline crashed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
