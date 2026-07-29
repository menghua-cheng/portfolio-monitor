"""Performance + signal tracker tests.

Hand-built price series make every horizon return checkable by arithmetic. The
cases that matter most are the honest-reporting ones: a horizon a ticker has too
little history for must be excluded (not silently treated as flat), and the
portfolio average must say how many tickers it averaged.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import tracker, tracker_report  # noqa: E402

AS_OF = "2026-06-30"


def _series(end: str, n: int, closes, adj=None):
    """A daily frame of `n` business days ENDING on `end`."""
    dates = pd.bdate_range(end=end, periods=n).strftime("%Y-%m-%d")
    c = list(closes) if not isinstance(closes, (int, float)) else [float(closes)] * n
    a = list(adj) if adj is not None else c
    return pd.DataFrame({"date": list(dates), "open": c, "high": c, "low": c,
                         "close": c, "adj_close": a, "volume": [1] * n,
                         "source": ["t"] * n})


def _row(ticker, date_, stype, detail=""):
    return {"ticker": ticker, "date": date_, "signal_type": stype, "detail": detail}


# --------------------------------------------------------------------------- #
# signal direction / labels
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stype,expected", [
    ("CROSS_UP_sma5_sma20", "up"),
    ("CROSS_DOWN_sma20_sma60", "down"),
    ("GOLDEN_CROSS", "up"),
    ("DEATH_CROSS", "down"),
    ("ALIGNED_UP", "up"),
    ("ALIGNED_DOWN", "down"),
    ("LONG_UP_SHORT_DOWN", "down"),
    ("LONG_DOWN_SHORT_BREAKOUT", "up"),
    ("SOMETHING_ELSE", "neutral"),
])
def test_signal_direction(stype, expected):
    assert tracker.signal_direction(stype) == expected


def test_cross_labels_are_bilingual_from_the_stored_detail():
    en, zh = tracker.signal_labels("CROSS_UP_sma5_sma20", "周線向上突破月線 | 註記")
    assert en == "Weekly line breaks above Monthly line"
    assert zh == "周線向上突破月線"          # the note after " | " is dropped


def test_known_state_labels_do_not_need_a_detail():
    en, zh = tracker.signal_labels("GOLDEN_CROSS")
    assert "Golden cross" in en and "黃金交叉" in zh


def test_unknown_signal_falls_back_to_its_type():
    assert tracker.signal_labels("WAT") == ("WAT", "WAT")


# --------------------------------------------------------------------------- #
# horizon returns
# --------------------------------------------------------------------------- #
def test_one_day_return_is_measured_against_the_previous_close():
    df = _series(AS_OF, 5, [100, 100, 100, 100, 110])
    p = tracker.ticker_performance("TST", "Test", df, AS_OF)
    assert p.returns["1d"] == pytest.approx(0.10)
    assert p.close == 110.0


def test_returns_use_adjusted_closes_so_a_split_is_not_performance():
    # Raw close halves (a 2:1 split) while the adjusted series is flat.
    df = _series(AS_OF, 30, [200] * 27 + [100, 100, 100], adj=[100] * 30)
    p = tracker.ticker_performance("TST", "Test", df, AS_OF)
    assert p.returns["1d"] == pytest.approx(0.0)
    assert p.returns["1w"] == pytest.approx(0.0)


def test_horizon_without_enough_history_is_none_not_zero():
    df = _series(AS_OF, 10, 100)                    # ~2 weeks of data
    p = tracker.ticker_performance("TST", "Test", df, AS_OF)
    assert p.returns["1w"] is not None
    assert p.returns["1y"] is None                  # excluded, not "flat"
    assert p.returns["6m"] is None


def test_ytd_is_measured_from_last_year_final_close():
    dates = ["2025-12-30", "2025-12-31", "2026-01-02", AS_OF]
    df = pd.DataFrame({"date": dates, "open": [1] * 4, "high": [1] * 4, "low": [1] * 4,
                       "close": [50, 100, 105, 120], "adj_close": [50, 100, 105, 120],
                       "volume": [1] * 4, "source": ["t"] * 4})
    p = tracker.ticker_performance("TST", "Test", df, AS_OF)
    assert p.returns["ytd"] == pytest.approx(0.20)   # 120/100, not 120/105


def test_off_52w_high_is_zero_at_the_high_and_negative_below():
    at_high = _series(AS_OF, 30, list(range(100, 130)))
    assert tracker.ticker_performance("T", "", at_high, AS_OF).off_high_pct == \
        pytest.approx(0.0)
    below = _series(AS_OF, 30, list(range(130, 100, -1)))
    p = tracker.ticker_performance("T", "", below, AS_OF)
    assert p.off_high_pct == pytest.approx(101 / 130 - 1)


def test_empty_frame_yields_an_empty_perf():
    p = tracker.ticker_performance("TST", "Test", _series(AS_OF, 0, []), AS_OF)
    assert p.close is None and p.bars == 0


def test_a_horizon_boundary_on_a_weekend_snaps_back_to_a_real_bar():
    # 2026-06-30 minus 7d = 2026-06-23 (a Tuesday); build a gap around it so the
    # lookup must snap to the last bar strictly before the cutoff.
    dates = ["2026-06-19", "2026-06-26", AS_OF]
    df = pd.DataFrame({"date": dates, "open": [1] * 3, "high": [1] * 3, "low": [1] * 3,
                       "close": [100, 110, 120], "adj_close": [100, 110, 120],
                       "volume": [1] * 3, "source": ["t"] * 3})
    p = tracker.ticker_performance("T", "", df, AS_OF)
    assert p.returns["1w"] == pytest.approx(0.20)     # from 2026-06-19's 100


# --------------------------------------------------------------------------- #
# portfolio
# --------------------------------------------------------------------------- #
def test_portfolio_is_the_equal_weight_mean_and_reports_its_membership():
    prices = {"A": _series(AS_OF, 5, [100, 100, 100, 100, 110]),
              "B": _series(AS_OF, 5, [100, 100, 100, 100, 90])}
    perfs = [tracker.ticker_performance(s, "", d, AS_OF) for s, d in prices.items()]
    pf = tracker.portfolio_performance(perfs, prices, AS_OF)
    assert pf.returns["1d"] == pytest.approx(0.0)     # +10% and -10%
    assert pf.counted["1d"] == 2
    assert pf.best[0] == "A" and pf.worst[0] == "B"


def test_portfolio_excludes_short_history_tickers_from_long_horizons():
    prices = {"OLD": _series(AS_OF, 400, 100), "NEW": _series(AS_OF, 5, 100)}
    perfs = [tracker.ticker_performance(s, "", d, AS_OF) for s, d in prices.items()]
    pf = tracker.portfolio_performance(perfs, prices, AS_OF)
    assert pf.counted["1d"] == 2
    assert pf.counted["1y"] == 1                      # NEW can't answer 1y
    assert pf.returns["1y"] is not None


def test_portfolio_horizon_with_no_qualifying_ticker_is_none():
    prices = {"NEW": _series(AS_OF, 3, 100)}
    perfs = [tracker.ticker_performance("NEW", "", prices["NEW"], AS_OF)]
    pf = tracker.portfolio_performance(perfs, prices, AS_OF)
    assert pf.returns["1y"] is None and pf.counted["1y"] == 0


def test_index_series_starts_at_100_and_only_uses_full_coverage_tickers():
    prices = {"A": _series(AS_OF, 60, [100] * 59 + [120]),
              "LATE": _series(AS_OF, 3, 100)}          # too short for the window
    perfs = [tracker.ticker_performance(s, "", d, AS_OF) for s, d in prices.items()]
    pf = tracker.portfolio_performance(perfs, prices, AS_OF, index_days=60)
    assert pf.index_series[0][1] == pytest.approx(100.0)
    assert pf.index_series[-1][1] == pytest.approx(120.0)   # LATE didn't dilute it
    assert pf.index_members == 1                            # ...because it was excluded
    assert len(pf.index_series) > 3                         # ...and didn't truncate it


def test_index_series_is_empty_without_a_shared_window():
    prices = {"A": _series(AS_OF, 1, 100)}
    perfs = [tracker.ticker_performance("A", "", prices["A"], AS_OF)]
    pf = tracker.portfolio_performance(perfs, prices, AS_OF, index_days=60)
    assert pf.index_series == [] and pf.index_members == 0


# --------------------------------------------------------------------------- #
# signal tracking
# --------------------------------------------------------------------------- #
def test_up_signal_followed_by_a_gain_is_correct():
    prices = {"A": _series(AS_OF, 5, [100, 100, 100, 100, 120])}
    hits, score, per = track = tracker.track_signals(
        [_row("A", "2026-06-25", "GOLDEN_CROSS")], prices, AS_OF)
    assert len(hits) == 1 and hits[0].correct is True
    assert hits[0].forward_pct > 0
    assert score.hit_rate == 1.0 and per["A"].up_correct == 1
    assert track[1].up_total == 1


def test_up_signal_followed_by_a_fall_is_wrong():
    prices = {"A": _series(AS_OF, 5, [100, 100, 100, 100, 80])}
    hits, score, _ = tracker.track_signals(
        [_row("A", "2026-06-25", "CROSS_UP_sma5_sma20")], prices, AS_OF)
    assert hits[0].correct is False and score.hit_rate == 0.0


def test_down_signal_is_correct_when_price_falls():
    prices = {"A": _series(AS_OF, 5, [100, 100, 100, 100, 80])}
    hits, score, _ = tracker.track_signals(
        [_row("A", "2026-06-25", "DEATH_CROSS")], prices, AS_OF)
    assert hits[0].correct is True and score.down_correct == 1


def test_neutral_signals_are_listed_but_never_scored():
    prices = {"A": _series(AS_OF, 5, [100, 100, 100, 100, 120])}
    hits, score, _ = tracker.track_signals(
        [_row("A", "2026-06-25", "MYSTERY_STATE")], prices, AS_OF)
    assert len(hits) == 1 and hits[0].correct is None
    assert score.total == 1 and score.scored == 0 and score.hit_rate is None


def test_signals_outside_the_lookback_are_ignored():
    prices = {"A": _series(AS_OF, 200, 100)}
    rows = [_row("A", "2026-06-25", "GOLDEN_CROSS"),
            _row("A", "2025-06-25", "GOLDEN_CROSS")]
    hits, score, _ = tracker.track_signals(rows, prices, AS_OF, lookback_days=30)
    assert len(hits) == 1 and hits[0].date == "2026-06-25"
    assert score.total == 1


def test_future_dated_signals_are_ignored():
    prices = {"A": _series(AS_OF, 5, 100)}
    hits, _s, _p = tracker.track_signals(
        [_row("A", "2027-01-01", "GOLDEN_CROSS")], prices, AS_OF)
    assert hits == []


def test_signal_for_an_untracked_ticker_still_lists_without_a_score():
    hits, score, _ = tracker.track_signals(
        [_row("GONE", "2026-06-25", "GOLDEN_CROSS")], {}, AS_OF)
    assert len(hits) == 1 and hits[0].forward_pct is None
    assert score.scored == 0


def test_hits_are_sorted_newest_first():
    prices = {"A": _series(AS_OF, 30, 100)}
    rows = [_row("A", "2026-06-10", "GOLDEN_CROSS"),
            _row("A", "2026-06-29", "DEATH_CROSS")]
    hits, _s, _p = tracker.track_signals(rows, prices, AS_OF)
    assert [h.date for h in hits] == ["2026-06-29", "2026-06-10"]
    assert hits[0].days_ago == 1


def test_price_at_signal_is_the_raw_close():
    prices = {"A": _series(AS_OF, 5, [10, 20, 30, 40, 50], adj=[1, 2, 3, 4, 5])}
    sdate = prices["A"]["date"].iloc[2]
    hits, _s, _p = tracker.track_signals([_row("A", sdate, "GOLDEN_CROSS")],
                                         prices, AS_OF)
    assert hits[0].price_at_signal == 30.0      # what a quote screen showed
    assert hits[0].forward_pct == pytest.approx(5 / 3 - 1)   # adjusted basis


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def test_build_report_pulls_it_all_together():
    prices = {"A": _series(AS_OF, 30, list(range(100, 130))),
              "B": _series(AS_OF, 30, list(range(130, 100, -1)))}
    rep = tracker.build_report(prices, {"A": "Alpha", "B": "Beta"},
                               [_row("A", "2026-06-20", "GOLDEN_CROSS")])
    assert rep.as_of == AS_OF
    assert [t.symbol for t in rep.tickers] == ["A", "B"]
    assert rep.tickers[0].name == "Alpha"
    assert rep.portfolio.counted["1d"] == 2
    assert len(rep.signals) == 1 and rep.score.total == 1


def test_build_report_with_no_prices_says_so():
    rep = tracker.build_report({}, {}, [])
    assert rep.as_of == "" and "no cached prices" in rep.note


def test_build_report_flags_stale_tickers():
    prices = {"FRESH": _series(AS_OF, 10, 100),
              "STALE": _series("2026-06-10", 10, 100)}
    rep = tracker.build_report(prices, {}, [])
    assert rep.as_of == AS_OF and "STALE" in rep.note and "FRESH" not in rep.note


def test_explicit_as_of_overrides_the_newest_bar():
    prices = {"A": _series(AS_OF, 20, list(range(100, 120)))}
    earlier = prices["A"]["date"].iloc[10]
    rep = tracker.build_report(prices, {}, [], as_of=earlier)
    assert rep.as_of == earlier
    assert rep.tickers[0].returns["1d"] is not None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _view():
    prices = {"A": _series(AS_OF, 260, list(range(100, 360))),
              "B": _series(AS_OF, 260, list(range(360, 100, -1)))}
    rep = tracker.build_report(prices, {"A": "Alpha", "B": "Beta"},
                               [_row("A", "2026-06-20", "GOLDEN_CROSS"),
                                _row("B", "2026-06-22", "DEATH_CROSS"),
                                _row("B", "2026-06-24", "ODD_STATE")])
    return rep, tracker_report.build_view(rep)


def test_view_preformats_every_number_and_colour():
    _rep, v = _view()
    assert v.as_of == AS_OF and len(v.rows) == 2
    assert v.rows[0].cells[0].text.endswith("%")
    assert v.rows[0].cells[0].fg == tracker_report.UP_FG        # A rose
    assert v.rows[1].cells[0].fg == tracker_report.DOWN_FG      # B fell
    assert v.portfolio is not None and "1D 2" in v.portfolio_counts


def test_view_marks_neutral_signals_with_no_verdict():
    _rep, v = _view()
    odd = [s for s in v.signals if s.label_en == "ODD_STATE"][0]
    assert odd.verdict_en == "—" and odd.dir_en == "Neutral"


def test_view_scores_lead_with_the_all_ticker_row():
    _rep, v = _view()
    assert v.scores[0].scope_en == "All tickers"
    assert {s.scope_en for s in v.scores[1:]} == {"A", "B"}


def test_sparkline_is_svg_and_degrades_on_a_short_series():
    svg = tracker_report.sparkline_svg([("d1", 100.0), ("d2", 110.0), ("d3", 105.0)])
    assert svg.startswith("<svg") and "polyline" in svg
    assert tracker_report.sparkline_svg([("d1", 100.0)]) == ""
    assert tracker_report.sparkline_svg([]) == ""


def test_sparkline_colours_by_direction():
    up = tracker_report.sparkline_svg([("a", 100.0), ("b", 120.0)])
    down = tracker_report.sparkline_svg([("a", 120.0), ("b", 100.0)])
    assert tracker_report.UP_FG in up and tracker_report.DOWN_FG in down


def test_text_render_covers_the_three_sections_and_the_caveat():
    _rep, v = _view()
    out = tracker_report.render_text(v)
    assert "Portfolio performance & signal tracker" in out
    assert "PORTFOLIO" in out and "equal-weight" in out
    assert "Signals in the last" in out and "Signal hit rate" in out
    assert "no entries, exits or costs" in out


def test_text_render_truncates_long_signal_lists():
    _rep, v = _view()
    out = tracker_report.render_text(v, max_signals=1)
    assert "2 more" in out


def test_text_render_of_an_empty_report():
    v = tracker_report.build_view(tracker.build_report({}, {}, []))
    assert v.empty and "No data" in tracker_report.render_text(v)


def test_html_renders_both_languages_and_the_switcher():
    _rep, v = _view()
    html = tracker_report.render_html(v)
    assert html.startswith("<!doctype html>")
    assert 'class="i18n en"' in html and 'class="i18n zh"' in html
    assert html.count('class="i18n en"') == html.count('class="i18n zh"')
    assert "投資組合績效與訊號追蹤" in html and "<svg" in html
    assert "lang-switch" in html


def test_html_single_language_mode_drops_the_switcher():
    _rep, v = _view()
    en = tracker_report.render_html(v, lang_mode="en")
    assert "i18n" not in en and "lang-switch" not in en
    assert "Portfolio Performance" in en
    zh = tracker_report.render_html(v, lang_mode="zh")
    assert "投資組合績效與訊號追蹤" in zh and "i18n" not in zh


def test_html_of_an_empty_report_is_still_valid():
    v = tracker_report.build_view(tracker.build_report({}, {}, []))
    html = tracker_report.render_html(v)
    assert html.startswith("<!doctype html>") and "no cached prices" in html


def test_zh_view_has_no_english_leaks_in_the_variable_strings():
    _rep, v = _view()
    zh = tracker_report.render_html(v, lang_mode="zh")
    for leak in ("today", "of 3 tickers", "1D ", "YTD "):
        assert leak not in zh, f"English leaked into the zh view: {leak!r}"
    assert "本日" in zh or "日前" in zh
    assert "檔中納入" in zh and "年初至今" in zh
