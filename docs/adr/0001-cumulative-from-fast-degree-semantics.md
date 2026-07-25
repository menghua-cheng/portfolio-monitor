# Backtest degree = cumulative-from-fast cascade (distinct from the report's counting)

The backtest defines an **N-fold cross** (degree N) as *the fastest N adjacent MA
pairs have all crossed the same direction within the 30-calendar-day window, in any
order* — an ordered cascade outward from the shortest MAs (1=周/月, 2=+月/季, 3=+季/半年,
4=+半年/年). The daily report's `summarize_trend` instead tags 雙重/三重/四重突破 by counting
*any* distinct pairs that crossed, regardless of which. We deliberately kept these two
definitions different: the report is a loose "how much is moving" indicator, whereas the
backtest needs a strict, monotonic strength ladder so that higher degree reliably means
"the trend has propagated further along the MA stack." A future reader will otherwise
assume the two 雙重突破 definitions are the same and try to unify them.

## Considered options

- **Any N distinct pairs** (mirror `summarize_trend`): consistent with the report, but
  degree-1 could fire on any pair, making the 1→4 ladder non-monotonic in strength.
- **Cumulative-from-fast, any order** (chosen): a clean strength axis; degree N strictly
  contains degree N-1's pairs.
- **Strict cascade order** (fast must cross before slow): rarer and arguably "cleaner,"
  but far fewer signals over a ~2-year history and more complex to detect.
