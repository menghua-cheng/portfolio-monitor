"""Incremental price cache (feature: 價格快取).

Every run used to re-download each ticker's whole history from Yahoo (and all of
it again from Tiingo for the cross-check). This module makes the local SQLite
tables the source of truth and downloads only what is missing: look at the last
cached bar, ask upstream for the tail, append it.

The thing that makes this non-trivial is that **a stock split silently rewrites
history upstream**. Yahoo's raw `close` is split-adjusted retroactively, so after
a 4:1 split every historical close it serves is a quarter of what it served
yesterday. Blindly appending new bars onto old ones would leave a 75% one-day
"crash" in the cache that every indicator, signal and backtest would then treat
as real. Dividends do the same to `adj_close` (only) on a smaller scale.

So each incremental sync re-fetches a small **overlap** of already-cached bars
and compares them against what is stored:

* closes agree            -> append the new tail only (the common case).
* closes differ by a
  consistent ratio r       -> a split. Record it, rescale every cached bar older
                              than the overlap by r (volume by 1/r), then append.
* closes differ
  inconsistently           -> not a clean rebase (bad ticks, a source change).
                              Refuse to guess: refetch the full window and
                              overwrite, and say so in the note.
* only adj_close differs   -> a dividend adjustment. Same rescale path, applied
                              to the adjusted basis, recorded as `adjustment`.

Reference sources (Tiingo/Stooq) are cached the same way in `prices_ref`, so the
daily cross-check compares two *cached* series instead of re-downloading both.

Everything here is I/O — DB reads/writes and network fetches. The detection
maths is factored into pure helpers (`detect_rebase`, `_ratios`) so it is
testable without either.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import db, fetch

log = logging.getLogger("portfolio_monitor.cache")

PRIMARY_SOURCE = "yfinance"

# How many calendar days of already-cached bars to re-fetch and compare. Wide
# enough to survive a long weekend or a holiday week, narrow enough to stay cheap.
OVERLAP_DAYS = 12
# A shared date whose close moved by more than this is a rebase, not a revision.
# Real EOD corrections are far smaller; the smallest common split (5:4) is 20%.
REBASE_TOL_PCT = 0.5
# Ratios across the overlap must agree this closely to be called a clean rebase.
RATIO_CONSISTENCY_PCT = 0.5
# How far behind the primary series a cached reference source may sit and still be
# considered caught up. Tiingo publishes a day later than Yahoo, so by a calendar
# test the reference is *never* current and would be re-fetched on every run —
# including several runs of the same day. One bar of lag is the steady state.
REFERENCE_MAX_LAG_DAYS = 1

_PRICE_COLS = ["date", "open", "high", "low", "close", "adj_close", "volume", "source"]


@dataclass
class RebaseVerdict:
    """What comparing the overlap says about the cached price basis."""
    rebased: bool
    kind: str | None = None        # "split" | "adjustment" | "inconsistent"
    factor: float | None = None    # new/old, e.g. 0.25 for a 4:1 split
    effective_from: str | None = None
    note: str = ""


@dataclass
class SyncResult:
    ticker: str
    source: str
    action: str                    # full | incremental | up-to-date | failed
    rows_written: int = 0
    first_date: str | None = None
    last_date: str | None = None
    rows_cached: int = 0
    rebase: RebaseVerdict | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.action != "failed"


# --------------------------------------------------------------------------- #
# Pure helpers: frames <-> rows, rebase detection
# --------------------------------------------------------------------------- #
def rows_to_frame(rows, cols=None) -> pd.DataFrame:
    """SQLite rows -> the tidy ascending frame the rest of the package expects."""
    cols = cols or _PRICE_COLS
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows])
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols].sort_values("date").reset_index(drop=True)


def to_ref_rows(ticker: str, df: pd.DataFrame) -> list[dict]:
    """Fetched reference frame -> db.upsert_prices_ref row dicts."""
    out = []
    for _, r in df.iterrows():
        out.append(dict(ticker=ticker.upper(), source=str(r["source"]), date=r["date"],
                        open=float(r["open"]), high=float(r["high"]),
                        low=float(r["low"]), close=float(r["close"]),
                        adj_close=float(r["adj_close"]), volume=int(r["volume"])))
    return out


def _ratios(stored: pd.DataFrame, fresh: pd.DataFrame, col: str) -> np.ndarray:
    """fresh/stored per shared date for one column, NaNs and zeros dropped."""
    merged = stored[["date", col]].merge(fresh[["date", col]], on="date",
                                         suffixes=("_old", "_new"))
    if merged.empty:
        return np.array([])
    old = merged[f"{col}_old"].to_numpy(dtype=float)
    new = merged[f"{col}_new"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = new / old
    return r[np.isfinite(r) & (old > 0)]


def detect_rebase(stored: pd.DataFrame, fresh: pd.DataFrame) -> RebaseVerdict:
    """Compare overlapping bars and decide whether the cached basis is stale.

    Pure: both frames need `date`, `close` and `adj_close`. `stored` is what the
    cache holds for the overlap window; `fresh` is what upstream just served for
    the same dates.
    """
    close_r = _ratios(stored, fresh, "close")
    if close_r.size == 0:
        return RebaseVerdict(False, note="no shared dates to compare")

    # The cutoff is the earliest date the *fresh* fetch supplies, not the earliest
    # date we compared. Everything from there on gets overwritten by the upsert;
    # everything strictly before it is what must be rescaled. Using the comparison
    # window's start instead would leave the bars between the two starts stranded
    # on the old basis — a silent price cliff in the middle of the series.
    cutoff = str(fresh["date"].min())

    def _spread(r):
        med = float(np.median(r))
        if med <= 0:
            return med, float("inf")
        return med, float(np.max(np.abs(r - med)) / med * 100.0)

    med, spread = _spread(close_r)
    if abs(med - 1.0) * 100.0 > REBASE_TOL_PCT:
        if spread > RATIO_CONSISTENCY_PCT:
            return RebaseVerdict(True, "inconsistent", None, cutoff,
                                 f"overlap closes disagree by ~{abs(med - 1) * 100:.1f}% "
                                 f"but inconsistently (spread {spread:.1f}%) — "
                                 f"not a clean split")
        return RebaseVerdict(True, "split", med, cutoff,
                             f"closes rebased ×{med:.6g} "
                             f"({_split_phrase(med)}) across the overlap")

    # Closes agree, so no split. A dividend still moves adj_close retroactively.
    adj_r = _ratios(stored, fresh, "adj_close")
    if adj_r.size:
        amed, aspread = _spread(adj_r)
        if abs(amed - 1.0) * 100.0 > REBASE_TOL_PCT and aspread <= RATIO_CONSISTENCY_PCT:
            return RebaseVerdict(True, "adjustment", amed, cutoff,
                                 f"adjusted closes rebased ×{amed:.6g} "
                                 f"(dividend); raw closes unchanged")
    return RebaseVerdict(False, note="overlap matches cache")


def _split_phrase(factor: float) -> str:
    """Describe a ratio as a split when it is close to a simple one (4:1, 1:10…)."""
    if factor <= 0:
        return "unknown"
    for num in range(2, 21):
        if abs(factor - 1.0 / num) / (1.0 / num) < 0.02:
            return f"~{num}:1 split"
        if abs(factor - float(num)) / float(num) < 0.02:
            return f"~1:{num} reverse split"
    return "non-standard ratio"


# --------------------------------------------------------------------------- #
# Sync: primary series
# --------------------------------------------------------------------------- #
def _overlap_start(last_date: str, overlap_days: int) -> str:
    return (pd.Timestamp(last_date) - pd.Timedelta(days=overlap_days)).date().isoformat()


def last_expected_bar(today: str | None = None) -> str:
    """The newest bar that could exist: the most recent weekday on or before today.

    Deliberately ignores market holidays. Being wrong on a holiday costs one fetch
    that returns nothing new; hard-coding a holiday calendar would cost a
    dependency and go stale.
    """
    d = pd.Timestamp(today) if today else pd.Timestamp(date.today())
    while d.weekday() >= 5:                      # Sat/Sun
        d -= pd.Timedelta(days=1)
    return d.date().isoformat()


def is_current(last_date: str | None, today: str | None = None) -> bool:
    """Is a cached series already as new as any bar could be?

    This is the check that makes a same-day re-run free. Trimming the *payload* of
    an incremental fetch barely helps — per-request latency dominates, so asking
    for 12 days costs nearly what asking for 2 years costs. The only real saving is
    not asking at all.
    """
    return bool(last_date) and str(last_date) >= last_expected_bar(today)


def _apply_rebase(conn, ticker: str, verdict: RebaseVerdict) -> int:
    """Rescale the cached bars that predate the compared overlap, and audit it."""
    rescaled = 0
    if verdict.kind == "split" and verdict.factor and verdict.effective_from:
        rescaled = db.rescale_prices(conn, ticker, verdict.factor, verdict.effective_from)
    db.record_corporate_action(conn, ticker, date.today().isoformat(),
                               verdict.kind or "unknown", verdict.factor,
                               verdict.effective_from, rescaled, verdict.note)
    log.warning("%s: %s — %s (%d cached rows rescaled)", ticker, verdict.kind,
                verdict.note, rescaled)
    return rescaled


def sync_prices(conn, ticker: str, years: int = 2, overlap_days: int = OVERLAP_DAYS,
                force: bool = False, today: str | None = None) -> SyncResult:
    """Bring the cached primary (yfinance) series for one ticker up to date.

    `years` is the depth to reach on a first/forced fetch; an incremental sync
    ignores it and asks only for the tail. Never shrinks the cache: history
    deeper than `years` stays.
    """
    ticker = ticker.upper()
    last = db.latest_price_date(conn, ticker)

    # Nothing could have been published since the last cached bar -> no request at
    # all. This is where the real speed-up lives (see `is_current`).
    if not force and is_current(last, today):
        lo, hi, n = db.price_span(conn, ticker)
        note = f"cache current through {hi}"
        db.record_sync(conn, ticker, PRIMARY_SOURCE, lo, hi, n, "up-to-date", note)
        return SyncResult(ticker, PRIMARY_SOURCE, "up-to-date", 0, lo, hi, n,
                          RebaseVerdict(False, note="not checked; cache current"), note)

    full = force or last is None

    if full:
        fresh = fetch.fetch_yfinance(ticker, years)
        action = "full"
        verdict = RebaseVerdict(False, note="full fetch")
    else:
        fresh = fetch.fetch_yfinance(ticker, years, start=_overlap_start(last, overlap_days))
        action = "incremental"
        stored = rows_to_frame(db.get_prices(conn, ticker,
                                            start=_overlap_start(last, overlap_days)))
        verdict = detect_rebase(stored, fresh)
        if verdict.rebased and verdict.kind == "inconsistent":
            # Can't rescale safely — take the whole window fresh and overwrite.
            fresh = fetch.fetch_yfinance(ticker, years)
            action = "full"
            db.record_corporate_action(conn, ticker, date.today().isoformat(),
                                       "inconsistent", None, verdict.effective_from,
                                       0, verdict.note)
            log.warning("%s: %s — refetching %dy", ticker, verdict.note, years)
        elif verdict.rebased:
            _apply_rebase(conn, ticker, verdict)

    if fresh.empty:
        lo, hi, n = db.price_span(conn, ticker)
        note = "fetch returned no rows; cache left as-is"
        db.record_sync(conn, ticker, PRIMARY_SOURCE, lo, hi, n, "failed", note)
        return SyncResult(ticker, PRIMARY_SOURCE, "failed", 0, lo, hi, n, verdict, note)

    written = db.upsert_prices(conn, fetch.to_price_rows(ticker, fresh))
    lo, hi, n = db.price_span(conn, ticker)
    new_bars = 0 if last is None else int((pd.to_datetime(fresh["date"]) >
                                           pd.Timestamp(last)).sum())
    if action == "incremental" and new_bars == 0 and not verdict.rebased:
        action = "up-to-date"
    note = verdict.note if verdict.rebased else f"{new_bars} new bar(s)"
    db.record_sync(conn, ticker, PRIMARY_SOURCE, lo, hi, n, action, note)
    return SyncResult(ticker, PRIMARY_SOURCE, action, written, lo, hi, n, verdict, note)


def sync_reference(conn, ticker: str, years: int = 2,
                   overlap_days: int = OVERLAP_DAYS,
                   force: bool = False, today: str | None = None) -> SyncResult:
    """Cache the independent cross-check series (Tiingo, else Stooq) the same way.

    Best-effort by design: with no `TIINGO_API_KEY` and Stooq blocked this simply
    reports `failed` and the caller falls back to internal validation only.
    """
    ticker = ticker.upper()
    source = "tiingo" if _tiingo_keyed() else "stooq"
    last = db.latest_ref_date(conn, ticker, source)
    if not force and _reference_caught_up(conn, ticker, source, last, today):
        cached = db.get_prices_ref(conn, ticker, source)
        note = f"cache current through {last}"
        db.record_sync(conn, ticker, source, cached[0]["date"] if cached else None,
                       last, len(cached), "up-to-date", note)
        return SyncResult(ticker, source, "up-to-date", 0,
                          cached[0]["date"] if cached else None, last,
                          len(cached), note=note)
    start = None if (force or last is None) else _overlap_start(last, overlap_days)
    fresh = fetch.fetch_reference(ticker, years, start=start)
    if fresh.empty:
        note = "no reference source reachable"
        db.record_sync(conn, ticker, source, None, None, 0, "failed", note)
        return SyncResult(ticker, source, "failed", 0, note=note)

    source = str(fresh["source"].iloc[0])       # may differ from the guess above
    written = db.upsert_prices_ref(conn, to_ref_rows(ticker, fresh))
    cached = db.get_prices_ref(conn, ticker, source)
    lo = cached[0]["date"] if cached else None
    hi = cached[-1]["date"] if cached else None
    action = "full" if start is None else "incremental"
    db.record_sync(conn, ticker, source, lo, hi, len(cached), action, "")
    return SyncResult(ticker, source, action, written, lo, hi, len(cached))


def _reference_caught_up(conn, ticker: str, source: str, last: str | None,
                         today: str | None = None) -> bool:
    """Is the cached reference series fresh enough to skip its fetch?

    Two conditions, both needed. It must already be within `REFERENCE_MAX_LAG_DAYS`
    of the primary series — that is what keeps the cross-check comparing a recent
    shared date rather than a stale one. And it must have been synced *today*, so a
    permanently-lagging source is still pulled once per day and cannot silently
    drift further behind. Together these make the second and later runs of a day
    free without weakening the cross-check at all.
    """
    if not last:
        return False
    if is_current(last, today):
        return True
    primary_last = db.latest_price_date(conn, ticker)
    if not primary_last:
        return False
    lag = (pd.Timestamp(primary_last) - pd.Timestamp(last)).days
    if lag > REFERENCE_MAX_LAG_DAYS:
        return False
    rows = [r for r in db.get_sync(conn, ticker) if r["source"] == source]
    synced_at = rows[0]["last_synced_at"] if rows else None
    if not synced_at:
        return False
    # `last_synced_at` is written by SQLite's datetime('now'), which is **UTC**, so
    # the comparison has to be UTC too. Comparing it against a local `date.today()`
    # is wrong by a day for part of every day in any non-UTC timezone — silently,
    # since the only symptom is fetching more (or less) often than intended. Note
    # this is a wall-clock question, deliberately independent of `today`, which
    # carries the *market* date.
    return str(synced_at)[:10] >= datetime.now(timezone.utc).date().isoformat()


def _tiingo_keyed() -> bool:
    import os
    return bool(os.getenv("TIINGO_API_KEY", ""))


# --------------------------------------------------------------------------- #
# The pipeline's entry point
# --------------------------------------------------------------------------- #
def load_history(conn, ticker: str, years: int = 2, refresh: bool = True,
                 force: bool = False, tolerance_pct: float = 1.0,
                 window_years: int | None = None,
                 overlap_days: int = OVERLAP_DAYS, today: str | None = None,
                 ) -> tuple[pd.DataFrame, fetch.CrossCheck, SyncResult]:
    """Cache-first history for one ticker, plus a cross-check over cached series.

    This replaces `fetch.fetch_history` in the daily pipeline. With `refresh` it
    syncs both series first (cheap: only the tail moves), then serves the frame
    from SQLite. `window_years` trims what is *returned* without touching what is
    *stored*, so a deep cache still yields the report's usual window.
    """
    ticker = ticker.upper()
    sync = SyncResult(ticker, PRIMARY_SOURCE, "up-to-date", note="refresh skipped")
    if refresh:
        sync = sync_prices(conn, ticker, years=years, force=force,
                           overlap_days=overlap_days, today=today)
        sync_reference(conn, ticker, years=years, force=force,
                       overlap_days=overlap_days, today=today)

    start = None
    if window_years:
        start = (date.today() - timedelta(days=int(window_years * 365.25) + 10)).isoformat()
    df = rows_to_frame(db.get_prices(conn, ticker, start=start))

    ref = rows_to_frame(db.get_prices_ref(conn, ticker, start=start),
                        cols=["date", "open", "high", "low", "close", "adj_close",
                              "volume", "source"])
    cc = fetch.cross_check(ticker, df, ref, tolerance_pct)
    return df, cc, sync


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _status_lines(conn) -> list[str]:
    rows = db.get_sync(conn)
    if not rows:
        return ["cache is empty — run `portfolio-monitor-cache sync`"]
    head = ["ticker", "source", "first", "last", "rows", "last sync", "action", "note"]
    body = [[r["ticker"], r["source"], r["first_date"] or "—", r["last_date"] or "—",
             str(r["rows"] or 0), (r["last_synced_at"] or "—")[:16],
             r["action"] or "—", r["note"] or ""] for r in rows]
    widths = [max([len(head[i])] + [len(b[i]) for b in body]) for i in range(len(head))]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(head)),
           "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(b)) for b in body]
    return out


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from . import config

    p = argparse.ArgumentParser(
        prog="portfolio-monitor-cache",
        description="Inspect and refresh the local price cache. Syncs are "
                    "incremental: only bars newer than the cache are downloaded, "
                    "and a detected split rescales the stored history.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show what the cache holds per ticker and source.")
    p_sync = sub.add_parser("sync", help="Pull new bars for the watchlist (or given symbols).")
    p_sync.add_argument("symbols", nargs="*", metavar="SYMBOL")
    p_sync.add_argument("--years", type=int, help="Depth for a first/forced fetch "
                                                  "(default: settings history_years).")
    p_sync.add_argument("--force", action="store_true",
                        help="Refetch the whole window instead of just the tail.")
    p_sync.add_argument("--no-reference", action="store_true",
                        help="Skip the Tiingo/Stooq cross-check series.")
    sub.add_parser("actions", help="List detected splits / price re-basings.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load_config()

    with db.connect() as conn:
        if args.cmd == "status":
            print("\n".join(_status_lines(conn)))
            return 0

        if args.cmd == "actions":
            rows = db.list_corporate_actions(conn)
            if not rows:
                print("No price re-basings detected yet.")
                return 0
            for r in rows:
                factor = f"×{r['factor']:.6g}" if r["factor"] is not None else "—"
                print(f"{r['detected_on']}  {r['ticker']:6s} {r['kind']:12s} {factor:>12s} "
                      f"from {r['effective_from'] or '—'}  "
                      f"{r['rows_rescaled'] or 0} rows  {r['note'] or ''}")
            return 0

        wanted = {s.upper() for s in args.symbols}
        symbols = [t.symbol for t in cfg.tickers if not wanted or t.symbol in wanted]
        symbols += sorted(wanted - set(symbols))
        if not symbols:
            print("error: no tickers (watchlist is empty)")
            return 1

        years = args.years or cfg.history_years
        failed = 0
        for sym in symbols:
            res = sync_prices(conn, sym, years=years, force=args.force)
            line = (f"{sym:6s} {res.action:12s} {res.rows_written:5d} rows synced  "
                    f"{res.first_date} → {res.last_date} ({res.rows_cached} cached)"
                    f"  {res.note}")
            if not args.no_reference:
                ref = sync_reference(conn, sym, years=years, force=args.force)
                line += f"  | ref {ref.source}: {ref.action}"
            print(line)
            failed += 0 if res.ok else 1
        return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
