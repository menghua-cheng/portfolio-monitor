"""Incremental price-cache tests.

The detection maths (`detect_rebase`) is pure and tested directly. The sync paths
are tested against a real temp SQLite DB with the network stubbed, because the
behaviour that matters — "only pull new bars, and rescale rather than append when
a split rewrote history" — is exactly the interaction between fetch and storage.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import cache, db  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conn(tmp_path):
    with db.connect(tmp_path / "t.db") as c:
        yield c


def _frame(dates, closes, opens=None, volume=100, source="yfinance", adj=None):
    n = len(dates)
    o = list(opens if opens is not None else closes)
    a = list(adj if adj is not None else closes)
    return pd.DataFrame({"date": list(dates), "open": o, "high": o, "low": o,
                         "close": list(closes), "adj_close": a,
                         "volume": [volume] * n, "source": [source] * n})


def _bdates(start, n):
    return list(pd.date_range(start, periods=n, freq="B").strftime("%Y-%m-%d"))


def _seed(conn, ticker, dates, closes, volume=100):
    db.upsert_prices(conn, [
        dict(ticker=ticker, date=d, open=c, high=c, low=c, close=c, adj_close=c,
             volume=volume, source="yfinance")
        for d, c in zip(dates, closes)])


def _stub_yf(monkeypatch, frame_or_fn):
    """Point cache's fetch layer at a canned frame (or a callable of the args)."""
    calls = []

    def fake(ticker, years, start=None):
        calls.append({"ticker": ticker, "years": years, "start": start})
        out = frame_or_fn(ticker, years, start) if callable(frame_or_fn) else frame_or_fn
        return out.copy()

    monkeypatch.setattr(cache.fetch, "fetch_yfinance", fake)
    return calls


# --------------------------------------------------------------------------- #
# detect_rebase (pure)
# --------------------------------------------------------------------------- #
def test_matching_overlap_is_not_a_rebase():
    d = _bdates("2024-01-01", 5)
    v = cache.detect_rebase(_frame(d, [10, 11, 12, 13, 14]),
                            _frame(d, [10, 11, 12, 13, 14]))
    assert not v.rebased and "matches" in v.note


def test_tiny_revision_is_not_a_rebase():
    d = _bdates("2024-01-01", 4)
    # A 0.1% EOD correction must not trigger a rescale of the whole history.
    v = cache.detect_rebase(_frame(d, [100, 100, 100, 100]),
                            _frame(d, [100.1, 100.1, 100.1, 100.1]))
    assert not v.rebased


def test_four_for_one_split_is_detected_with_its_factor():
    d = _bdates("2024-01-01", 5)
    stored = _frame(d, [400, 404, 408, 412, 416])
    fresh = _frame(d, [100, 101, 102, 103, 104])          # every close /4
    v = cache.detect_rebase(stored, fresh)
    assert v.rebased and v.kind == "split"
    assert v.factor == pytest.approx(0.25)
    assert v.effective_from == d[0]      # the cutoff is where fresh data begins
    assert "4:1 split" in v.note


def test_reverse_split_is_described_as_such():
    d = _bdates("2024-01-01", 4)
    v = cache.detect_rebase(_frame(d, [1, 1, 1, 1]), _frame(d, [10, 10, 10, 10]))
    assert v.kind == "split" and v.factor == pytest.approx(10.0)
    assert "reverse split" in v.note


def test_inconsistent_drift_refuses_to_guess_a_factor():
    d = _bdates("2024-01-01", 4)
    stored = _frame(d, [100, 100, 100, 100])
    fresh = _frame(d, [50, 70, 90, 25])                   # no single ratio fits
    v = cache.detect_rebase(stored, fresh)
    assert v.rebased and v.kind == "inconsistent" and v.factor is None


def test_dividend_only_shift_is_an_adjustment_not_a_split():
    d = _bdates("2024-01-01", 4)
    stored = _frame(d, [100, 100, 100, 100], adj=[100, 100, 100, 100])
    fresh = _frame(d, [100, 100, 100, 100], adj=[99, 99, 99, 99])
    v = cache.detect_rebase(stored, fresh)
    assert v.rebased and v.kind == "adjustment"
    assert v.factor == pytest.approx(0.99)


def test_rebase_cutoff_is_where_fresh_data_begins_not_where_comparison_does():
    # Stored covers 10 bars; upstream only re-serves the last 4, all rebased.
    # The cutoff must be the first *fresh* date, so the un-overwritten bars
    # before it get rescaled rather than stranded on the old basis.
    d = _bdates("2024-01-01", 10)
    stored = _frame(d, [400.0] * 10)
    fresh = _frame(d[6:], [100.0] * 4)
    v = cache.detect_rebase(stored, fresh)
    assert v.kind == "split" and v.effective_from == d[6]


def test_no_shared_dates_is_not_a_rebase():
    v = cache.detect_rebase(_frame(_bdates("2024-01-01", 2), [10, 11]),
                            _frame(_bdates("2025-01-01", 2), [10, 11]))
    assert not v.rebased and "no shared dates" in v.note


@pytest.mark.parametrize("factor,expected", [
    (0.25, "4:1 split"), (0.1, "10:1 split"), (3.0, "1:3 reverse split"),
    (0.777, "non-standard"),
])
def test_split_phrase(factor, expected):
    assert expected in cache._split_phrase(factor)


# --------------------------------------------------------------------------- #
# rescale_prices (storage)
# --------------------------------------------------------------------------- #
def test_rescale_only_touches_rows_before_the_cutoff(conn):
    d = _bdates("2024-01-01", 4)
    _seed(conn, "TST", d, [400, 400, 100, 100], volume=100)
    changed = db.rescale_prices(conn, "TST", 0.25, before_date=d[2])
    assert changed == 2
    got = [(r["date"], r["close"], r["volume"]) for r in db.get_prices(conn, "TST")]
    assert got[0] == (d[0], 100.0, 400)       # price /4, volume ×4
    assert got[2] == (d[2], 100.0, 100)       # untouched


def test_rescale_is_a_noop_for_a_unit_factor(conn):
    d = _bdates("2024-01-01", 2)
    _seed(conn, "TST", d, [100, 100])
    assert db.rescale_prices(conn, "TST", 1.0, d[1]) == 0
    assert db.rescale_prices(conn, "TST", -2.0, d[1]) == 0


# --------------------------------------------------------------------------- #
# sync_prices
# --------------------------------------------------------------------------- #
def test_first_sync_is_a_full_fetch(conn, monkeypatch):
    d = _bdates("2024-01-01", 6)
    calls = _stub_yf(monkeypatch, _frame(d, [10, 11, 12, 13, 14, 15]))
    res = cache.sync_prices(conn, "TST", years=2)
    assert res.action == "full" and res.rows_written == 6 and res.rows_cached == 6
    assert calls[0]["start"] is None          # no start = years-back default
    assert res.first_date == d[0] and res.last_date == d[-1]


def test_second_sync_only_asks_for_the_tail(conn, monkeypatch):
    d = _bdates("2024-01-01", 10)
    _seed(conn, "TST", d[:8], [10] * 8)
    calls = _stub_yf(monkeypatch, _frame(d[6:], [10, 10, 20, 21]))
    res = cache.sync_prices(conn, "TST", years=2)
    assert res.action == "incremental" and "2 new bar(s)" in res.note
    assert calls[0]["start"] is not None      # asked from an overlap start
    assert pd.Timestamp(calls[0]["start"]) < pd.Timestamp(d[7])
    assert res.rows_cached == 10              # appended, nothing lost


def test_sync_with_nothing_new_reports_up_to_date(conn, monkeypatch):
    d = _bdates("2024-01-01", 6)
    _seed(conn, "TST", d, [10] * 6)
    _stub_yf(monkeypatch, _frame(d[3:], [10, 10, 10]))
    res = cache.sync_prices(conn, "TST", years=2)
    assert res.action == "up-to-date" and res.rows_cached == 6


def test_split_rescales_cached_history_instead_of_appending_a_cliff(conn, monkeypatch):
    # 20 cached bars at 400; upstream now serves the overlap at 100 (4:1 split)
    # plus one genuinely new bar.
    d = _bdates("2024-01-01", 21)
    _seed(conn, "TST", d[:20], [400.0] * 20, volume=100)
    _stub_yf(monkeypatch, _frame(d[14:], [100.0] * 7))
    res = cache.sync_prices(conn, "TST", years=2)

    assert res.rebase.rebased and res.rebase.kind == "split"
    closes = [r["close"] for r in db.get_prices(conn, "TST")]
    assert closes == [100.0] * 21             # one basis throughout, no 75% cliff
    pct = pd.Series(closes).pct_change().abs().max() * 100
    assert pct < 1.0                          # the cliff the rescale exists to prevent
    actions = db.list_corporate_actions(conn, "TST")
    assert len(actions) == 1 and actions[0]["kind"] == "split"
    assert actions[0]["rows_rescaled"] > 0


def test_split_also_rescales_volume_inversely(conn, monkeypatch):
    d = _bdates("2024-01-01", 21)
    _seed(conn, "TST", d[:20], [400.0] * 20, volume=1000)
    _stub_yf(monkeypatch, _frame(d[14:], [100.0] * 7, volume=4000))
    cache.sync_prices(conn, "TST", years=2)
    vols = [r["volume"] for r in db.get_prices(conn, "TST")]
    assert vols[0] == 4000                    # 1000 / 0.25


def test_inconsistent_overlap_triggers_a_full_refetch(conn, monkeypatch):
    d = _bdates("2024-01-01", 12)
    _seed(conn, "TST", d[:10], [100.0] * 10)

    def serve(ticker, years, start=None):
        if start is None:                     # the full refetch
            return _frame(d, [7.0] * 12)
        return _frame(d[6:10], [50, 70, 90, 25])   # the garbled overlap

    _stub_yf(monkeypatch, serve)
    res = cache.sync_prices(conn, "TST", years=2)
    assert res.action == "full"
    assert [r["close"] for r in db.get_prices(conn, "TST")] == [7.0] * 12
    kinds = [r["kind"] for r in db.list_corporate_actions(conn, "TST")]
    assert "inconsistent" in kinds            # audited, not silently swallowed


def test_force_skips_the_incremental_path(conn, monkeypatch):
    d = _bdates("2024-01-01", 6)
    _seed(conn, "TST", d, [10] * 6)
    calls = _stub_yf(monkeypatch, _frame(d, [10] * 6))
    res = cache.sync_prices(conn, "TST", years=5, force=True)
    assert res.action == "full" and calls[0]["start"] is None


def test_failed_fetch_leaves_the_cache_intact(conn, monkeypatch):
    d = _bdates("2024-01-01", 6)
    _seed(conn, "TST", d, [10] * 6)
    _stub_yf(monkeypatch, _frame([], []))
    res = cache.sync_prices(conn, "TST", years=2)
    assert res.action == "failed" and not res.ok
    assert res.rows_cached == 6               # nothing dropped
    assert db.get_sync(conn, "TST")[0]["action"] == "failed"


def test_sync_records_bookkeeping(conn, monkeypatch):
    d = _bdates("2024-01-01", 4)
    _stub_yf(monkeypatch, _frame(d, [10, 11, 12, 13]))
    cache.sync_prices(conn, "TST", years=2)
    row = [r for r in db.get_sync(conn, "TST") if r["source"] == "yfinance"][0]
    assert row["first_date"] == d[0] and row["last_date"] == d[-1]
    assert row["rows"] == 4 and row["last_synced_at"]


# --------------------------------------------------------------------------- #
# sync_reference + load_history
# --------------------------------------------------------------------------- #
def test_reference_series_is_cached_separately(conn, monkeypatch):
    d = _bdates("2024-01-01", 4)
    monkeypatch.setattr(cache.fetch, "fetch_reference",
                        lambda t, y, start=None: _frame(d, [10, 11, 12, 13], source="tiingo"))
    res = cache.sync_reference(conn, "TST", years=2)
    assert res.source == "tiingo" and res.rows_written == 4
    assert len(db.get_prices_ref(conn, "TST", "tiingo")) == 4
    assert db.get_prices(conn, "TST") == []       # canonical table untouched


def test_unreachable_reference_is_not_fatal(conn, monkeypatch):
    monkeypatch.setattr(cache.fetch, "fetch_reference",
                        lambda t, y, start=None: _frame([], []))
    res = cache.sync_reference(conn, "TST", years=2)
    assert res.action == "failed" and "no reference source" in res.note


def test_load_history_serves_from_cache_and_cross_checks(conn, monkeypatch):
    d = _bdates("2024-01-01", 5)
    _stub_yf(monkeypatch, _frame(d, [10, 11, 12, 13, 14]))
    monkeypatch.setattr(cache.fetch, "fetch_reference",
                        lambda t, y, start=None: _frame(d, [10, 11, 12, 13, 14],
                                                        source="tiingo"))
    df, cc, sync = cache.load_history(conn, "TST", years=2, tolerance_pct=1.0)
    assert list(df["date"]) == d and sync.action == "full"
    assert cc.within_tolerance and cc.ref_source == "tiingo"


def test_load_history_can_trim_the_returned_window_without_shrinking_the_cache(conn, monkeypatch):
    old = _bdates("2015-01-01", 5)
    recent = _bdates("2026-06-01", 5)
    _seed(conn, "TST", old + recent, [10.0] * 10)
    df, _cc, sync = cache.load_history(conn, "TST", refresh=False, window_years=1)
    assert list(df["date"]) == recent            # only the window comes back
    assert db.price_span(conn, "TST")[2] == 10   # all 10 rows still cached
    assert sync.action == "up-to-date"


def test_load_history_without_refresh_makes_no_network_call(conn, monkeypatch):
    d = _bdates("2024-01-01", 3)
    _seed(conn, "TST", d, [10.0] * 3)

    def boom(*a, **k):
        raise AssertionError("network must not be touched when refresh=False")

    monkeypatch.setattr(cache.fetch, "fetch_yfinance", boom)
    monkeypatch.setattr(cache.fetch, "fetch_reference", boom)
    df, _cc, sync = cache.load_history(conn, "TST", refresh=False)
    assert len(df) == 3 and sync.action == "up-to-date"


# --------------------------------------------------------------------------- #
# get_prices bounds / helpers
# --------------------------------------------------------------------------- #
def test_get_prices_respects_start_and_end(conn):
    d = _bdates("2024-01-01", 6)
    _seed(conn, "TST", d, [10] * 6)
    assert len(db.get_prices(conn, "TST", start=d[2])) == 4
    assert len(db.get_prices(conn, "TST", end=d[2])) == 3
    assert len(db.get_prices(conn, "TST", start=d[1], end=d[3])) == 3


def test_price_span_and_ticker_listing(conn):
    _seed(conn, "AAA", _bdates("2024-01-01", 3), [1, 2, 3])
    _seed(conn, "BBB", _bdates("2024-02-01", 2), [1, 2])
    assert db.price_span(conn, "AAA")[2] == 3
    assert db.price_span(conn, "ZZZ") == (None, None, 0)
    assert db.tickers_with_prices(conn) == ["AAA", "BBB"]


def test_status_lines_report_empty_and_populated_caches(conn, monkeypatch):
    assert "cache is empty" in cache._status_lines(conn)[0]
    d = _bdates("2024-01-01", 3)
    _stub_yf(monkeypatch, _frame(d, [10, 11, 12]))
    cache.sync_prices(conn, "TST", years=2)
    lines = cache._status_lines(conn)
    assert lines[0].startswith("ticker") and any("TST" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# "cache is current" short circuit — the actual speed-up
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("today,expected", [
    ("2026-07-30", "2026-07-30"),   # Thursday -> itself
    ("2026-08-01", "2026-07-31"),   # Saturday -> Friday
    ("2026-08-02", "2026-07-31"),   # Sunday   -> Friday
])
def test_last_expected_bar_walks_back_over_weekends(today, expected):
    assert cache.last_expected_bar(today) == expected


def test_is_current_compares_against_the_last_weekday():
    assert cache.is_current("2026-07-31", today="2026-08-02")     # Fri bar, Sunday run
    assert not cache.is_current("2026-07-30", today="2026-07-31")
    assert not cache.is_current(None, today="2026-07-31")


def test_current_cache_makes_no_network_call_at_all(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)                 # ends 2026-07-29 (a Wednesday)
    _seed(conn, "TST", d, [10.0] * 21)

    def boom(*a, **k):
        raise AssertionError("a current cache must not hit the network")

    monkeypatch.setattr(cache.fetch, "fetch_yfinance", boom)
    res = cache.sync_prices(conn, "TST", years=2, today=d[-1])
    assert res.action == "up-to-date" and res.rows_written == 0
    assert "cache current through" in res.note
    assert res.rows_cached == 21


def test_force_still_fetches_a_current_cache(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d, [10.0] * 21)
    calls = _stub_yf(monkeypatch, _frame(d, [10.0] * 21))
    res = cache.sync_prices(conn, "TST", years=2, force=True, today=d[-1])
    assert res.action == "full" and len(calls) == 1


def test_a_stale_cache_still_syncs(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d[:-3], [10.0] * 18)
    calls = _stub_yf(monkeypatch, _frame(d[-8:], [10.0] * 8))
    res = cache.sync_prices(conn, "TST", years=2, today=d[-1])
    assert res.action == "incremental" and len(calls) == 1


def test_current_reference_cache_is_also_skipped(conn, monkeypatch):
    d = _bdates("2026-07-01", 5)
    db.upsert_prices_ref(conn, [
        dict(ticker="TST", source="stooq", date=x, open=1, high=1, low=1,
             close=1, adj_close=1, volume=1) for x in d])

    def boom(*a, **k):
        raise AssertionError("a current reference cache must not hit the network")

    monkeypatch.setattr(cache.fetch, "fetch_reference", boom)
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    res = cache.sync_reference(conn, "TST", years=2, today=d[-1])
    assert res.action == "up-to-date" and res.rows_cached == 5


def test_reference_one_bar_behind_is_refetched_when_not_yet_synced_today(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d, [10.0] * 21)            # primary through d[-1]
    db.upsert_prices_ref(conn, [
        dict(ticker="TST", source="stooq", date=x, open=1, high=1, low=1,
             close=1, adj_close=1, volume=1) for x in d[:-1]])   # ref one bar behind
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(cache.fetch, "fetch_reference",
                        lambda t, y, start=None: calls.append(start) or
                        _frame(d, [1.0] * 21, source="stooq"))
    res = cache.sync_reference(conn, "TST", years=2, today=d[-1])
    assert res.action == "incremental" and len(calls) == 1


def test_reference_within_lag_and_already_synced_today_is_skipped(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d, [10.0] * 21)
    db.upsert_prices_ref(conn, [
        dict(ticker="TST", source="stooq", date=x, open=1, high=1, low=1,
             close=1, adj_close=1, volume=1) for x in d[:-1]])
    db.record_sync(conn, "TST", "stooq", d[0], d[-2], 20, "incremental", "")
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("already synced today and within lag — must not refetch")

    monkeypatch.setattr(cache.fetch, "fetch_reference", boom)
    # record_sync stamped "now" in UTC; the recency check reads the same clock, so
    # this is the same-day branch regardless of the runner's timezone.
    res = cache.sync_reference(conn, "TST", years=2, today=d[-1])
    assert res.action == "up-to-date"


def test_reference_far_behind_is_always_refetched(conn, monkeypatch):
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d, [10.0] * 21)
    db.upsert_prices_ref(conn, [
        dict(ticker="TST", source="stooq", date=x, open=1, high=1, low=1,
             close=1, adj_close=1, volume=1) for x in d[:5]])   # weeks behind
    db.record_sync(conn, "TST", "stooq", d[0], d[4], 5, "incremental", "")
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(cache.fetch, "fetch_reference",
                        lambda t, y, start=None: calls.append(start) or
                        _frame(d, [1.0] * 21, source="stooq"))
    cache.sync_reference(conn, "TST", years=2)
    assert len(calls) == 1        # lag > REFERENCE_MAX_LAG_DAYS wins over "synced today"


def test_sync_recency_uses_utc_like_the_stored_stamp(conn, monkeypatch):
    """Regression: `last_synced_at` is SQLite datetime('now') = UTC. Comparing it
    against a local date is off by a day for part of every day outside UTC."""
    d = _bdates("2026-07-01", 21)
    _seed(conn, "TST", d, [10.0] * 21)
    db.upsert_prices_ref(conn, [
        dict(ticker="TST", source="stooq", date=x, open=1, high=1, low=1,
             close=1, adj_close=1, volume=1) for x in d[:-1]])
    db.record_sync(conn, "TST", "stooq", d[0], d[-2], 20, "incremental", "")
    stamp = db.get_sync(conn, "TST")[0]["last_synced_at"]
    from datetime import datetime, timezone
    assert str(stamp)[:10] == datetime.now(timezone.utc).date().isoformat()
    assert cache._reference_caught_up(conn, "TST", "stooq", d[-2], today=d[-1])
