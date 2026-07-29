"""Signal-rule registry tests.

Each rule is checked in both directions on a tiny hand-built frame, so the mask
it returns can be asserted bar by bar. Parsing and group expansion are covered
separately — they are what the CLI actually exposes.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import rules  # noqa: E402
from portfolio_monitor.rules import RuleContext, RuleSpec  # noqa: E402

LADDER = (5, 20, 60, 120, 240)


def _ctx(**kw):
    base = dict(pairs=rules.adjacent_pairs(LADDER),
                ma_cols=[f"sma{p}" for p in LADDER],
                window_days=30, slope_lookback=2, flat_threshold_pct=0.5)
    base.update(kw)
    return RuleContext(**base)


def _frame(n=6, **cols):
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    df = pd.DataFrame({"date": dates, "adj_c": [100.0] * n})
    for p in LADDER:
        df[f"sma{p}"] = [float(p)] * n          # descending-in-period = bearish stack
    for k, v in cols.items():
        df[k] = list(v)
    return df


# --- adjacent pairs ---------------------------------------------------------
def test_adjacent_pairs_are_fast_to_slow():
    assert rules.adjacent_pairs([60, 5, 20]) == [("sma5", "sma20"), ("sma20", "sma60")]


def test_adjacent_pairs_honour_ma_kind():
    assert rules.adjacent_pairs([5, 20], "ema") == [("ema5", "ema20")]


# --- cross ------------------------------------------------------------------
def test_cross_state_both_directions():
    df = _frame(4, sma5=[8, 8, 30, 30], sma20=[20] * 4)
    spec = RuleSpec.of("cross", short="sma5", long="sma20")
    assert list(rules.confirm(df, spec, "up", _ctx())) == [False, False, True, True]
    assert list(rules.confirm(df, spec, "down", _ctx())) == [True, True, False, False]


def test_cross_on_missing_column_is_all_false():
    df = _frame(3)
    spec = RuleSpec.of("cross", short="sma7", long="sma20")
    assert not rules.confirm(df, spec, "up", _ctx()).any()


# --- price ------------------------------------------------------------------
def test_price_vs_ma():
    df = _frame(4, adj_c=[10.0, 30.0, 30.0, 10.0], sma20=[20.0] * 4)
    spec = RuleSpec.of("price", ma="sma20")
    assert list(rules.confirm(df, spec, "up", _ctx())) == [False, True, True, False]
    assert list(rules.confirm(df, spec, "down", _ctx())) == [True, False, False, True]


def test_price_unwarmed_ma_is_false_in_both_directions():
    df = _frame(2, adj_c=[10.0, 10.0], sma20=[None, 20.0])
    spec = RuleSpec.of("price", ma="sma20")
    assert list(rules.confirm(df, spec, "up", _ctx())) == [False, False]
    assert list(rules.confirm(df, spec, "down", _ctx())) == [False, True]


# --- align ------------------------------------------------------------------
def test_align_bullish_and_bearish_stacks():
    df = _frame(2)                       # sma5=5 < sma20=20 < … = bearish
    assert list(rules.confirm(df, RuleSpec.of("align"), "down", _ctx())) == [True, True]
    assert not rules.confirm(df, RuleSpec.of("align"), "up", _ctx()).any()

    df2 = _frame(2)
    for i, p in enumerate(LADDER):       # reverse the stack -> bullish
        df2[f"sma{p}"] = [float(100 - i * 10)] * 2
    assert list(rules.confirm(df2, RuleSpec.of("align"), "up", _ctx())) == [True, True]


def test_align_false_where_any_ma_unwarmed():
    df = _frame(2)
    df["sma240"] = [None, 240.0]
    assert list(rules.confirm(df, RuleSpec.of("align"), "down", _ctx())) == [False, True]


# --- slope ------------------------------------------------------------------
def test_slope_up_down_and_flat():
    # lookback 2: bar2 vs bar0 rises 20%, bar3 vs bar1 falls, bar4 vs bar2 flat.
    df = _frame(5, sma240=[100.0, 120.0, 120.0, 100.0, 120.0])
    spec = RuleSpec.of("slope", ma="sma240")
    up = list(rules.confirm(df, spec, "up", _ctx()))
    down = list(rules.confirm(df, spec, "down", _ctx()))
    assert up == [False, False, True, False, False]      # bar4 is flat vs bar2
    assert down == [False, False, False, True, False]


def test_slope_shorter_than_lookback_is_all_false():
    df = _frame(2, sma240=[100.0, 200.0])
    spec = RuleSpec.of("slope", ma="sma240")
    assert not rules.confirm(df, spec, "up", _ctx(slope_lookback=5)).any()


# --- degree -----------------------------------------------------------------
def test_degree_beyond_ladder_is_all_false():
    df = _frame(3, sma5=[8, 30, 30], sma20=[20] * 3)
    ctx = _ctx(pairs=rules.adjacent_pairs([5, 20]))     # only one pair exists
    assert not rules.confirm(df, RuleSpec.of("degree", n=2), "up", ctx).any()
    assert rules.confirm(df, RuleSpec.of("degree", n=1), "up", ctx)[1]


# --- multi (雙重・三重・四重突破／跌破) --------------------------------------
def _two_pair_frame():
    """sma5×sma20 crosses up at bar2; sma60×sma120 crosses up at bar4.
    Those are pairs 1 and 3 of the ladder — NOT cumulative from the fast end."""
    df = _frame(6)
    df["sma5"] = [8, 8, 30, 30, 30, 30]
    df["sma20"] = [20] * 6
    df["sma60"] = [50, 50, 50, 50, 130, 130]
    df["sma120"] = [120] * 6
    df["sma240"] = [240] * 6
    return df


def test_multi2_counts_any_two_pairs_where_degree2_needs_the_fastest_two():
    df = _two_pair_frame()
    multi2 = rules.confirm(df, RuleSpec.of("multi", n=2), "up", _ctx())
    degree2 = rules.confirm(df, RuleSpec.of("degree", n=2), "up", _ctx())
    assert list(multi2) == [False, False, False, False, True, True]
    assert not degree2.any()          # sma20×sma60 never crossed, so no cascade


def test_multi_counts_up_and_down_independently():
    df = _two_pair_frame()
    assert not rules.confirm(df, RuleSpec.of("multi", n=2), "down", _ctx()).any()


def test_multi1_fires_on_the_first_pair_alone():
    df = _two_pair_frame()
    m1 = rules.confirm(df, RuleSpec.of("multi", n=1), "up", _ctx())
    assert list(m1) == [False, False, True, True, True, True]


def test_multi3_needs_a_third_pair():
    df = _two_pair_frame()
    assert not rules.confirm(df, RuleSpec.of("multi", n=3), "up", _ctx()).any()
    # Give sma120 an up-cross through sma240 at bar5 -> a third pair, so multi3 fires
    # there (and only there) while degree3 still needs sma20×sma60, which never crossed.
    df.loc[5, "sma120"] = 300.0
    assert list(rules.confirm(df, RuleSpec.of("multi", n=3), "up", _ctx())) == \
        [False, False, False, False, False, True]
    assert not rules.confirm(df, RuleSpec.of("degree", n=3), "up", _ctx()).any()


def test_multi_respects_the_trailing_window():
    # Pairs cross 4 days apart; a 2-day window never sees both at once.
    df = _two_pair_frame()
    assert not rules.confirm(df, RuleSpec.of("multi", n=2), "up",
                             _ctx(window_days=1)).any()


def test_multi_with_no_pairs_is_all_false():
    df = _two_pair_frame()
    assert not rules.confirm(df, RuleSpec.of("multi", n=2), "up", _ctx(pairs=[])).any()


def test_multi_names_cover_the_report_tags():
    assert rules.MULTI_NAMES[2][1] == "雙重突破／跌破"
    assert rules.MULTI_NAMES[4][0].startswith("Quadruple")


# --- parsing ----------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("degree2", RuleSpec.of("degree", n=2)),
    ("degree:3", RuleSpec.of("degree", n=3)),
    ("multi2", RuleSpec.of("multi", n=2)),
    ("multi:4", RuleSpec.of("multi", n=4)),
    ("align", RuleSpec.of("align")),
    ("cross:20/60", RuleSpec.of("cross", short="sma20", long="sma60")),
    ("cross:ema20/ema60", RuleSpec.of("cross", short="ema20", long="ema60")),
    ("price:sma20", RuleSpec.of("price", ma="sma20")),
    ("slope:240", RuleSpec.of("slope", ma="sma240")),
    ("  DEGREE1  ", RuleSpec.of("degree", n=1)),
])
def test_parse_rule_round_trip(text, expected):
    spec = rules.parse_rule(text)
    assert spec == expected
    assert rules.parse_rule(spec.label) == expected      # label re-parses


@pytest.mark.parametrize("bad", ["", "degree", "degreeX", "multi", "multiX",
                                 "cross:20", "cross:x/y",
                                 "price", "price:foo", "nonsense:1", "wat"])
def test_parse_rule_rejects_garbage(bad):
    with pytest.raises(ValueError):
        rules.parse_rule(bad)


def test_parse_rules_expands_groups_against_the_ladder():
    ctx = _ctx()
    degrees = rules.parse_rules("degrees", ctx)
    assert [s.label for s in degrees] == ["degree1", "degree2", "degree3", "degree4"]
    crosses = rules.parse_rules("crosses", ctx)
    assert crosses[0].label == "cross:sma5/sma20" and len(crosses) == 4
    multis = rules.parse_rules("multis", ctx)
    assert [s.label for s in multis] == ["multi2", "multi3", "multi4"]
    # degrees + multis(2..4) + crosses + prices + slopes + align
    assert len(rules.parse_rules("all", ctx)) == 4 + 3 + 4 + 5 + 5 + 1


def test_parse_rules_short_ladder_yields_fewer_degrees():
    ctx = _ctx(pairs=rules.adjacent_pairs([4, 13, 26]), ma_cols=["sma4", "sma13", "sma26"])
    assert [s.label for s in rules.parse_rules("degrees", ctx)] == ["degree1", "degree2"]


def test_parse_rules_dedupes_and_keeps_order():
    ctx = _ctx()
    got = rules.parse_rules("degree2, degrees, degree2, align", ctx)
    labels = [s.label for s in got]
    assert labels[0] == "degree2"
    assert labels.count("degree2") == 1
    assert labels[-1] == "align"


def test_parse_rules_rejects_empty_list():
    with pytest.raises(ValueError):
        rules.parse_rules("  , ", _ctx())


def test_price_rule_falls_back_to_raw_close_without_adj_c():
    df = _frame(2, sma20=[20.0, 20.0])
    df = df.drop(columns=["adj_c"])
    df["close"] = [10.0, 30.0]
    spec = RuleSpec.of("price", ma="sma20")
    assert list(rules.confirm(df, spec, "up", _ctx())) == [False, True]


@pytest.mark.parametrize("alias,n", [("double", 2), ("dual", 2), ("triple", 3),
                                     ("quad", 4), ("quadruple", 4), ("QUAD", 4)])
def test_multi_word_aliases(alias, n):
    assert rules.parse_rule(alias) == RuleSpec.of("multi", n=n)


# --- memoization (performance invariant) ------------------------------------
def test_window_masks_are_computed_once_per_pair_and_direction(monkeypatch):
    """A grid asks many cells for the same per-pair masks. Computing them per cell
    instead of per run cost ~14x on the daily report's 16-cell grid, so this is a
    behaviour test, not a micro-optimisation detail."""
    df = _two_pair_frame()
    ctx = _ctx()
    calls = []
    real = rules._pair_cross_dates
    monkeypatch.setattr(rules, "_pair_cross_dates",
                        lambda d, s, l, dr: calls.append((s, l, dr)) or real(d, s, l, dr))
    # A 4x4 degree grid plus 3 multis, both directions — 19 rule evaluations.
    for n in (1, 2, 3, 4):
        for direction in ("up", "down"):
            rules.confirm(df, RuleSpec.of("degree", n=n), direction, ctx)
    for n in (2, 3, 4):
        for direction in ("up", "down"):
            rules.confirm(df, RuleSpec.of("multi", n=n), direction, ctx)
    # 4 pairs x 2 directions = 8 distinct masks, no matter how many cells asked.
    assert len(calls) == 8
    assert len(set(calls)) == 8


def test_reset_memo_forces_recomputation():
    df = _two_pair_frame()
    ctx = _ctx()
    first = rules.confirm(df, RuleSpec.of("degree", n=1), "up", ctx)
    assert ctx.memo                      # something was cached
    ctx.reset_memo()
    assert not ctx.memo
    assert list(rules.confirm(df, RuleSpec.of("degree", n=1), "up", ctx)) == list(first)


def test_trailing_window_is_inclusive_at_both_ends():
    dates = pd.date_range("2024-01-01", periods=10, freq="D").strftime("%Y-%m-%d")
    bars = pd.Series(dates).to_numpy()
    mask = rules._in_trailing_window([dates[2]], bars, window_days=3)
    # the cross bar itself, and exactly 3 calendar days after it
    assert list(mask) == [False, False, True, True, True, True, False, False, False, False]


def test_trailing_window_with_no_crosses_or_no_bars():
    bars = pd.Series(["2024-01-01", "2024-01-02"]).to_numpy()
    assert not rules._in_trailing_window([], bars, 30).any()
    assert len(rules._in_trailing_window(["2024-01-01"], pd.Series([], dtype=object).to_numpy(), 30)) == 0
