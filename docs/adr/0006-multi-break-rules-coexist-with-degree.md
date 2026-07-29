# 雙重/三重/四重突破 is its own rule family, coexisting with degree

The backtest now has **two** ways to say "several MA pairs crossed together", and they
mean different things on purpose:

| | `degreeN` | `multiN` |
|---|---|---|
| requires | the **fastest N** adjacent pairs, cumulative from the short end | **any N** distinct adjacent pairs |
| origin | ADR-0001, built for the backtest | `signals.summarize_trend`, what the daily report already tags |
| example | 周/月 then 月/季 then 季/半年 | 周/月 plus 季/半年, skipping 月/季 |

The obvious tidier alternatives were both rejected:

- **Replace degree with multi.** Rejected: the cascade is the stricter, more meaningful
  signal — a trend that has propagated *outward along the stack* is not the same event as
  two unrelated pairs crossing in the same month. ADR-0001 chose the cascade deliberately
  and that reasoning still holds.
- **Replace multi with degree** (i.e. don't add it). Rejected: the report has shown
  雙重/三重/四重突破 tags since Session 2, and the user's question was precisely "can I
  backtest the thing the report is showing me?". Without `multi`, the answer was no — the
  report's headline tag was the one signal the backtest could not evaluate.

So both exist, and the naming keeps them apart: **degree** is the cascade, **multi** is
the count. `multi` also answers to `double`/`triple`/`quad`, matching the report's wording.

## Consequences

- `degreeN` and `multiN` are **not** orderable by strictness in general. Every degree-N
  cross is a multi-N cross, but not the reverse, so `multi` fires more often and earlier.
  A grid mixing both (`--entry degrees,multis`) will contain pairs whose results are
  identical for tickers whose crosses happen to cascade from the fast end, and wildly
  different otherwise. That is information, not redundancy.
- `multi1` is legal but pointless (it means "any pair crossed at all"), so the `multis`
  group token starts at 2 — matching the report, which only ever tags 雙重 and up.
- Adding this family cost one rule function and one parser case, which is the payoff of
  the registry from ADR-0005. It required no engine change at all.
