# The price cache is incremental, and a detected split rescales stored history

The local SQLite `prices` table is now the **source of truth**. A run reads it, asks
upstream only for bars newer than what it holds, and appends. Previously every run
re-downloaded each ticker's whole history from Yahoo *and* the whole thing again from
Tiingo for the cross-check.

The load-bearing problem is not caching — it is that **upstream silently rewrites the
past**. Yahoo's raw `Close` is split-adjusted retroactively: the day after a 4:1 split,
every historical close it serves is a quarter of what it served the day before.
`Adj Close` moves the same way on every dividend. So "append what's new" is only safe if
something checks that the old and new bars are on the same price basis. Without that
check, a split leaves a 75% one-day crash sitting in the middle of the cache, and every
indicator, signal, backtest and performance number downstream treats it as real.

So each incremental sync **re-fetches a small overlap of bars it already has** (12
calendar days by default, `cache.overlap_days`) and compares:

| overlap comparison | action |
|---|---|
| closes agree | append the new tail (the common case) |
| closes differ by a consistent ratio *r* | **split**: rescale every cached bar before the fetch window by *r*, volume by 1/*r*, then append |
| closes differ inconsistently | refuse to guess: refetch the full window and overwrite, and audit it |
| only `adj_close` differs consistently | **dividend adjustment**: same path, recorded as `adjustment` |

Considered and rejected:

- **Full refetch every run** (the old behaviour): correct, but the cost the user asked to
  remove, and it silently caps history at `history_years`.
- **Full refetch on any mismatch, never rescale**: simpler, but it *destroys deep history*.
  The explorer's `--refresh --years 14` builds a cache far deeper than any single fetch
  window; a refetch-and-replace would truncate it back on the next split. Rescaling
  preserves it. (The inconsistent branch still falls back to this, because guessing a
  factor from noisy data is worse than losing depth.)
- **Store each price basis separately and adjust on read**: the fully general answer, and
  far more machinery than a personal monitor needs.
- **Trust yfinance's `splits` action feed**: one more endpoint to depend on, and it would
  not catch a Tiingo-side or dividend re-basing. Comparing the data we actually store
  against the data we actually receive is source-agnostic and needs no extra call.

Reference sources (Tiingo/Stooq) are cached the same way in a separate `prices_ref` table,
so the daily cross-check compares two *cached* series. `prices` stays one row per
(ticker, date) — putting `source` in its primary key would have made `get_prices` return
duplicate dates and broken every existing caller.

## Consequences

- **The cache mutates stored history.** `db.rescale_prices` rewrites rows in place. Every
  rescale is audited in `corporate_actions` (factor, cutoff, rows touched, reason) and
  logged at WARNING, because a silent bulk price rewrite is otherwise indistinguishable
  from corruption. `portfolio-monitor-cache actions` lists them.
- The rescale cutoff is **the earliest date the fresh fetch supplies**, not the earliest
  date compared. Using the comparison window's start instead leaves the bars between the
  two starts on the old basis — a cliff in the middle of the series. This was a real bug,
  caught by the split test; `test_rebase_cutoff_is_where_fresh_data_begins…` guards it.
- Detection needs an overlap, so it needs the cache to be **non-empty and recent**. A
  first sync, or `--force`, takes the full window with no comparison — nothing to compare
  against. A cache stale by more than `overlap_days` fetches from the overlap start
  anyway, so the comparison window simply widens.
- `REBASE_TOL_PCT` (0.5%) sits between a real EOD correction and the smallest common split
  (5:4 = 20%), so ordinary revisions do not trigger a rescale.
- The cache may now hold **more** history than the report wants. `cache.load_history`
  takes `window_years` to trim what it *returns* without touching what it *stores*, which
  is what keeps the daily report on its usual 2-year window while the explorer sees 14.
- The daily pipeline no longer calls `fetch.fetch_history`. That function still exists and
  still works; it is simply not on the daily path.
