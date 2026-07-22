# Progress — US Stock Portfolio Monitor

Status legend: `todo` · `in-progress` · `verified`

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 0 | Scaffold & environment | verified | `pip install -r requirements.txt` OK; core imports OK |
| 1 | Config & portfolio list CLI | verified | add/list/remove ticker persists to yaml + DB |
| 2 | Database layer | verified | idempotent upsert unit test passes (5 tests) |
| 3 | Data fetching + cross-check | verified | real fetch of AAPL/MSFT/NVDA: 507 clean rows each, last bar 2026-07-21, internal validator passes; cross-check via Tiingo (activates when TIINGO_API_KEY set) |
| 4 | Indicators (SMA/EMA) | verified | 4 unit tests pass (hand-computed SMA/EMA, year-line null<240, NaN->None); real tickers computed & stored |
| 5 | Trend-transition signals | verified | 7 unit tests: golden/death cross, long-up/short-down, long-down/breakout, current-state label, no-dup on unchanged state |
| 6 | Charts | verified | AAPL.png rendered (958x713, 116KB); candlesticks + 5 MA overlays + volume panel visually confirmed |
| 7 | Daily HTML report | verified | 480KB self-contained HTML: 3 tickers, 3 embedded charts, state chips, MA legend + detail cells, signals, disclaimer (structural check; browser ext unavailable for screenshot) |
| 8 | Email delivery | verified (dry-run) | .eml built: multipart/alternative+related, 3 inline PNG CIDs, cid: placeholders rewritten. Live send pending user App Password (permissioned action) |
| 9 | Pipeline + scheduling | verified | full pipeline runs (3 tickers, DB updated, local HTML self-contained, email dry-run); cron wrapper runs standalone (exit 0); --send fallback + --tickers filter work. Crontab entry documented (not auto-installed). |
| 10 | Docs & close-out | verified | README written; clean-shell run via wrapper OK (exit 0); DB integrity confirmed (507 clean rows/ticker, 0 null OHLC, 240-line warm, last bar 2026-07-21); 16 tests pass |

## ✅ All 10 steps verified — all 7 requested features complete.

Final acceptance (2026-07-22): fetch→indicators→signals→charts→report→email(dry-run)→DB all green
for AAPL/MSFT/NVDA. Live email send is the only step requiring user action (Gmail App Password in
`.env`, then run with `--send`).

## Session 2 (2026-07-22) — enhancements

| Step | Feature | Status | Verification gate |
|------|---------|--------|-------------------|
| 11 | Interactive Plotly charts | verified | render_interactive_html builds a 12-trace figure (candlestick + 5 SMA + 5 EMA + volume), valid JSON parses, unified hover shows date/OHLC/SMA/EMA/volume; plotly.js inlined ONCE per report (self-contained, offline); static PNG kept as `<noscript>` failsafe + email image. Browser screenshot N/A (extension not connected). |
| 12 | Granular MA-cross details + 雙重趨勢訊號 | verified | detect_cross_events covers all adjacent pairs (5/20/60/120/240) → 「月線向上突破季線」etc.; double-signal note like 「14日前 2025-06-26 周線已向上突破月線」. 4 new unit tests; validated on real history (AAPL 23 / MSFT 23 / NVDA 31 events). |
| 13 | --send graceful skip + cron | verified | `--send` with blank SMTP now SKIPS email (no .eml, exit 0) instead of dry-run fallback; email uses PNG (plotly.js stripped). Cron installed: `0 6 * * 2-6 …/run_daily.sh --send`; wrapper run exit 0, email skipped. |
| 14 | Tiingo enablement | pending user | `.env` created with `TIINGO_API_KEY=` (blank). Code already supports it; paste a free key to activate the yfinance-vs-Tiingo cross-check, then re-run to verify. |

| 15 | Bilingual report + language switcher | verified | Report is now a full UTF-8 `<!doctype html>` doc with a top-right EN/中文 switcher (default en_US, choice remembered via localStorage). Every user-facing string carries en+zh via a `t(en,zh)` Jinja macro + CSS/JS toggle (33 balanced en/zh spans). Email renders single-language EN (0 i18n spans, no switcher/JS). Tiingo cross-check LIVE (OK vs tiingo). |
| 16 | FIX: charts + note follow the switcher | verified | Bug: chart labels & data-source note were baked bilingual so both langs always showed. Fix: figure rendered English-only (0 CJK in figure JSON, legend names neutral SMA5/EMA5); per-chart `__pmChartI18n` registry + `applyChartLang()` relayouts titles/axes/vol-hover on toggle; `data_source_note` made bilingual. Holistic scan: 0 stray CJK outside toggle-spans/registry (only the 中文 button + plotly.js calendar internals remain). **Runtime-verified in headless Google Chrome (Playwright): EN → 34 en-spans visible / 0 zh-spans / chart labels English / 0 stray CJK; 中文 → 0 en-spans / 34 zh-spans / chart labels 價格·量·成交量·收盤與均線; toggle back to EN restores fully.** |

| 17 | Always-present trend summary in table | verified | Table was blank ("—") on days with no fresh cross. Added `signals.summarize_trend`: MA alignment (多頭/空頭/多空交錯 via `_ma_alignment`), multi-line breakout/breakdown tags (雙重/三重突破・跌破, counts distinct same-dir adjacent-pair crosses in `recent_window_days`=30), and `detect_recent_cross_events` recent-crossings list with days-ago. 4 new tests. Real data: AAPL 多頭排列; MSFT 多空交錯 + 雙重突破 (季線突破半年線 14d + 周線突破月線 15d); NVDA recent crosses. Runtime-reverified in Chrome (47/0 span split, charts follow). |

| 18 | Chart cross markers w/ hover detail | verified | Each MA crossing in the visible window is marked on the interactive chart: ▲ green triangle (突破/breakout) / ▼ red triangle (跌破/breakdown) at the crossing MA level (`charts._cross_markers`). Hover pops up the detail msg; language-switchable via the registry + `Plotly.restyle` on marker `text`. Runtime-verified in headless Chrome: real `Plotly.Fx.hover` popup shows "Weekly line breaks above Monthly line · 2026-07-06" (EN) ⇄ "周線向上突破月線 · 2026-07-06" (ZH); switcher still isolates languages (47/0 spans, 0 stray CJK, charts+markers follow). |

| 19 | Highlight multi-breakout clusters on chart | verified | `charts._breakout_clusters` groups same-direction crosses within window_days (bounded from first cross → tight bursts) spanning ≥2 distinct pairs → 雙重/三重/四重突破・跌破. Each cluster drawn as a shaded band (opacity 0.06+0.05·degree, dotted border) + bold ★ label trace (`meta:'clusterUp'/'clusterDown'`, top for up / bottom for down), language-switchable via registry+restyle. Real data: AAPL 3×雙重突破 + 三重跌破; NVDA 2×三重突破. Runtime-verified in Chrome: ★ labels "★ Double breakout"⇄"★ 雙重突破", switcher still isolates (PASS). **Hover popup lists the constituent crossings** (short on-chart `text` label + detailed `hovertext`): e.g. 三重向上突破 → 周線突破月線 / 月線突破季線 / 季線突破半年線; language-switchable (restyles both text+hovertext). |

24 tests pass. New dep: `plotly>=5.20` (in requirements.txt + venv).

## Notes / decisions log
- 2026-07-22: Project scaffolded. Using **Python 3.11** venv (system python3.14 lacks ensurepip and
  sudo is unavailable; 3.11 also has better prebuilt wheels for the data stack).
- Data source: yfinance (primary). **Stooq is now blocked** (anti-bot PoW wall + pandas-datareader
  0.11.1 stooq reader unimplemented), so cross-check moved to **Tiingo** (free key, set `TIINGO_API_KEY`).
  Without a key: single-source yfinance + strong internal validation (NaN, non-positive, monotonic
  dates, dup dates, high<low, staleness >7d, extreme daily move >50%).
