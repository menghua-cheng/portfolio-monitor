/* Backtest engine, JavaScript port (feature: 互動回測).
 *
 * This is a deliberate second implementation of `backtest.py` + `rules.py`, so the
 * standalone HTML explorer can recompute a grid in the browser when the user
 * changes the window, the interval or the rules — with no server and no Python.
 *
 * TWO ENGINES CAN DRIFT. That is the whole risk of this file, so it is guarded
 * two ways rather than by care:
 *   1. `tests/test_explorer.py` runs this file under node against fixtures the
 *      Python engine produced, over a matrix of specs, and asserts equality to
 *      1e-9. Change either engine and that test fails.
 *   2. Every generated page embeds Python's own results for its default spec and
 *      re-checks them on load, so an artifact opened months later still says so
 *      if the two sides disagree.
 *
 * Parity notes worth knowing before editing:
 *   - Weeks run Mon..Sun (pandas `to_period("W")`); a bar keeps the LAST real
 *     trading date in its bucket, never a synthetic period end.
 *   - SMA is null until `period` observations; EMA (adjust=false) recurses from
 *     x[0] but is likewise null until `period` observations.
 *   - A signal on bar i fills at bar i+1's adjusted open, and the last bar of the
 *     window can never trigger (there is nothing to fill against).
 *
 * Pure: arrays in, plain objects out. No DOM. Loaded both as a browser global
 * (`PM`) and as a node module.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PM = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MS_DAY = 86400000;

  // --- dates ---------------------------------------------------------------
  function dayNum(iso) {                 // "YYYY-MM-DD" -> integer day index
    return Math.floor(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / MS_DAY);
  }
  function isoOf(day) {
    return new Date(day * MS_DAY).toISOString().slice(0, 10);
  }
  function weekdayMon0(day) {            // 1970-01-01 was a Thursday (=3 Mon-based)
    return (((day + 3) % 7) + 7) % 7;
  }

  // --- bar aggregation -----------------------------------------------------
  var INTERVALS = ['daily', 'weekly', 'monthly'];
  var DEFAULT_LADDERS = {
    daily: [5, 20, 60, 120, 240],
    weekly: [4, 13, 26, 52, 104],
    monthly: [3, 6, 12, 24, 60]
  };
  var BARS_PER_YEAR = { daily: 252, weekly: 52, monthly: 12 };

  function bucketKey(iso, interval) {
    if (interval === 'monthly') return iso.slice(0, 7);
    var d = dayNum(iso);
    return String(d - weekdayMon0(d));   // the Monday of the Mon..Sun week
  }

  /* Aggregate daily bars to `interval`. open=first, high=max, low=min,
     close/adjClose=last, and the bar's date is the last real date in the bucket. */
  function resample(bars, interval) {
    if (interval === 'daily') return bars;
    var out = { date: [], open: [], high: [], low: [], close: [], adjClose: [] };
    var key = null;
    for (var i = 0; i < bars.date.length; i++) {
      var k = bucketKey(bars.date[i], interval);
      if (k !== key) {
        key = k;
        out.date.push(bars.date[i]);
        out.open.push(bars.open[i]);
        out.high.push(bars.high[i]);
        out.low.push(bars.low[i]);
        out.close.push(bars.close[i]);
        out.adjClose.push(bars.adjClose[i]);
      } else {
        var j = out.date.length - 1;
        out.date[j] = bars.date[i];
        if (bars.high[i] > out.high[j]) out.high[j] = bars.high[i];
        if (bars.low[i] < out.low[j]) out.low[j] = bars.low[i];
        out.close[j] = bars.close[i];
        out.adjClose[j] = bars.adjClose[i];
      }
    }
    return out;
  }

  function minHistoryYears(interval, ladder) {
    if (!ladder || !ladder.length) return 0;
    return Math.max.apply(null, ladder) / BARS_PER_YEAR[interval];
  }

  // --- moving averages -----------------------------------------------------
  function sma(values, period) {
    var out = new Array(values.length), sum = 0;
    for (var i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      out[i] = i >= period - 1 ? sum / period : null;
    }
    return out;
  }

  function ema(values, period) {
    // adjust=false: recurse from x[0], but null until `period` observations.
    var a = 2 / (period + 1), out = new Array(values.length), prev = null;
    for (var i = 0; i < values.length; i++) {
      prev = i === 0 ? values[0] : (1 - a) * prev + a * values[i];
      out[i] = i >= period - 1 ? prev : null;
    }
    return out;
  }

  function maColumns(bars, ladder, kind) {
    var cols = {}, fn = kind === 'ema' ? ema : sma;
    for (var i = 0; i < ladder.length; i++) cols[kind + ladder[i]] = fn(bars.close, ladder[i]);
    return cols;
  }

  function adjusted(bars) {
    var n = bars.date.length, adjOpen = new Array(n);
    for (var i = 0; i < n; i++) {
      var f = bars.close[i] > 0 ? bars.adjClose[i] / bars.close[i] : 1;
      adjOpen[i] = bars.open[i] * f;
    }
    return { adjOpen: adjOpen, adjClose: bars.adjClose.slice() };
  }

  // --- rules ---------------------------------------------------------------
  function adjacentPairs(ladder, kind) {
    var sorted = ladder.slice().sort(function (a, b) { return a - b; }), out = [];
    for (var i = 0; i < sorted.length - 1; i++) out.push([kind + sorted[i], kind + sorted[i + 1]]);
    return out;
  }

  var MULTI_ALIASES = { double: 2, dual: 2, triple: 3, quad: 4, quadruple: 4 };

  function parseMa(tok) {
    tok = tok.trim().toLowerCase();
    if (/^\d+$/.test(tok)) return 'sma' + tok;
    if (/^(sma|ema)\d+$/.test(tok)) return tok;
    throw new Error('not a moving-average name: ' + tok);
  }

  /* Mirror of rules.parse_rule. Accepts degreeN/degree:N, multiN/double/triple/
     quad, cross:S/L, price:MA, slope:MA, align. */
  function parseRule(text) {
    var t = String(text).trim().toLowerCase();
    if (!t) throw new Error('empty rule');
    if (t === 'align') return { kind: 'align', label: 'align' };
    if (MULTI_ALIASES[t]) return { kind: 'multi', n: MULTI_ALIASES[t], label: 'multi' + MULTI_ALIASES[t] };
    var counted = ['degree', 'multi'];
    for (var c = 0; c < counted.length; c++) {
      if (t.indexOf(counted[c]) === 0) {
        var arg = t.slice(counted[c].length).replace(/^:/, '');
        if (!/^\d+$/.test(arg)) throw new Error(counted[c] + ' needs a number: ' + text);
        return { kind: counted[c], n: +arg, label: counted[c] + arg };
      }
    }
    var bits = t.split(':'), kind = bits[0], arg = bits.slice(1).join(':');
    if (kind === 'cross') {
      var sl = arg.split('/');
      if (sl.length !== 2) throw new Error('cross needs SHORT/LONG: ' + text);
      var s = parseMa(sl[0]), l = parseMa(sl[1]);
      return { kind: 'cross', short: s, long: l, label: 'cross:' + s + '/' + l };
    }
    if (kind === 'price' || kind === 'slope') {
      var ma = parseMa(arg);
      return { kind: kind, ma: ma, label: kind + ':' + ma };
    }
    throw new Error('unknown rule: ' + text);
  }

  function expandRules(text, ctx) {
    var out = [], seen = {};
    function add(r) { if (!seen[r.label]) { seen[r.label] = 1; out.push(r); } }
    String(text).split(',').forEach(function (raw) {
      var tok = raw.trim().toLowerCase();
      if (!tok) return;
      var all = tok === 'all';
      if (tok === 'degrees' || all) for (var n = 1; n <= ctx.pairs.length; n++) add(parseRule('degree' + n));
      if (tok === 'multis' || all) for (var m = 2; m <= ctx.pairs.length; m++) add(parseRule('multi' + m));
      if (tok === 'crosses' || all) ctx.pairs.forEach(function (p) { add(parseRule('cross:' + p[0] + '/' + p[1])); });
      if (tok === 'prices' || all) ctx.maCols.forEach(function (c) { add(parseRule('price:' + c)); });
      if (tok === 'slopes' || all) ctx.maCols.forEach(function (c) { add(parseRule('slope:' + c)); });
      if (all) add(parseRule('align'));
      if (['degrees', 'multis', 'crosses', 'prices', 'slopes', 'all'].indexOf(tok) >= 0) return;
      add(parseRule(tok));
    });
    if (!out.length) throw new Error('no rules parsed');
    return out;
  }

  function crossState(a, b, i) {
    if (a[i] === null || b[i] === null || a[i] === undefined || b[i] === undefined) return null;
    return a[i] - b[i] >= 0 ? 1 : -1;
  }

  function pairCrossDays(ctx, shortName, longName, direction) {
    var a = ctx.ma[shortName], b = ctx.ma[longName], out = [];
    if (!a || !b) return out;
    for (var i = 1; i < ctx.days.length; i++) {
      var prev = crossState(a, b, i - 1), curr = crossState(a, b, i);
      if (prev !== null && curr !== null && prev !== curr) {
        if ((curr > prev ? 'up' : 'down') === direction) out.push(ctx.days[i]);
      }
    }
    return out;
  }

  /* Per-bar: did one of `crossDays` land 0..windowDays before this bar?
     Two-pointer sweep over sorted days — the same answer as the Python
     searchsorted, and the hot path here too. */
  function inTrailingWindow(crossDays, days, windowDays) {
    var out = new Array(days.length).fill(false);
    if (!crossDays.length) return out;
    var lo = 0, hi = 0;
    for (var i = 0; i < days.length; i++) {
      while (hi < crossDays.length && crossDays[hi] <= days[i]) hi++;
      while (lo < crossDays.length && crossDays[lo] < days[i] - windowDays) lo++;
      out[i] = hi > lo;
    }
    return out;
  }

  function pairWindowMask(ctx, shortName, longName, direction) {
    var key = shortName + '|' + longName + '|' + direction;
    if (!ctx.memo[key]) {
      ctx.memo[key] = inTrailingWindow(pairCrossDays(ctx, shortName, longName, direction),
                                       ctx.days, ctx.windowDays);
    }
    return ctx.memo[key];
  }

  function confirm(rule, direction, ctx) {
    var n = ctx.days.length, out = new Array(n).fill(false), i, k;
    if (rule.kind === 'degree') {
      if (rule.n < 1 || rule.n > ctx.pairs.length) return out;
      out = new Array(n).fill(true);
      for (k = 0; k < rule.n; k++) {
        var m = pairWindowMask(ctx, ctx.pairs[k][0], ctx.pairs[k][1], direction);
        for (i = 0; i < n; i++) out[i] = out[i] && m[i];
      }
      return out;
    }
    if (rule.kind === 'multi') {
      if (rule.n < 1 || !ctx.pairs.length) return out;
      var counts = new Array(n).fill(0);
      for (k = 0; k < ctx.pairs.length; k++) {
        var mm = pairWindowMask(ctx, ctx.pairs[k][0], ctx.pairs[k][1], direction);
        for (i = 0; i < n; i++) if (mm[i]) counts[i]++;
      }
      for (i = 0; i < n; i++) out[i] = counts[i] >= rule.n;
      return out;
    }
    if (rule.kind === 'cross') {
      var a = ctx.ma[rule.short], b = ctx.ma[rule.long];
      if (!a || !b) return out;
      for (i = 0; i < n; i++) {
        var st = crossState(a, b, i);
        out[i] = st === null ? false : (direction === 'up' ? st > 0 : st < 0);
      }
      return out;
    }
    if (rule.kind === 'price') {
      var line = ctx.ma[rule.ma];
      if (!line) return out;
      for (i = 0; i < n; i++) {
        if (line[i] === null) { out[i] = false; continue; }
        var above = ctx.adjClose[i] >= line[i];
        out[i] = direction === 'up' ? above : !above;
      }
      return out;
    }
    if (rule.kind === 'align') {
      var cols = ctx.maCols.filter(function (c) { return ctx.ma[c]; });
      if (cols.length < 2) return out;
      for (i = 0; i < n; i++) {
        var ok = true, warm = true;
        for (k = 0; k < cols.length; k++) if (ctx.ma[cols[k]][i] === null) { warm = false; break; }
        if (!warm) { out[i] = false; continue; }
        for (k = 0; k < cols.length - 1; k++) {
          var d = ctx.ma[cols[k + 1]][i] - ctx.ma[cols[k]][i];   // slow - fast
          if (direction === 'up' ? !(d < 0) : !(d > 0)) { ok = false; break; }
        }
        out[i] = ok;
      }
      return out;
    }
    if (rule.kind === 'slope') {
      var s = ctx.ma[rule.ma];
      if (!s) return out;
      var lb = Math.max(1, ctx.slopeLookback);
      for (i = lb; i < n; i++) {
        var now = s[i], then = s[i - lb];
        if (now === null || then === null || then === 0) { out[i] = false; continue; }
        var pct = (now - then) / Math.abs(then) * 100;
        out[i] = direction === 'up' ? pct > ctx.flatThresholdPct : pct < -ctx.flatThresholdPct;
      }
      return out;
    }
    throw new Error('unknown rule kind: ' + rule.kind);
  }

  function risingEdge(mask) {
    var out = new Array(mask.length);
    for (var i = 0; i < mask.length; i++) out[i] = mask[i] && !(i > 0 && mask[i - 1]);
    return out;
  }

  // --- trade engine --------------------------------------------------------
  function runOne(ctx, entry, exit, ws, we, costBps, startingCash, wantCurve) {
    var n = we + 1, cost = costBps / 1e4;
    var entryEdge = risingEdge(confirm(entry, 'up', ctx));
    var exitEdge = risingEdge(confirm(exit, 'down', ctx));
    var events = [], inPos = false, i;
    for (i = ws; i < n; i++) {
      if (i + 1 >= n) break;                       // nothing to fill against
      if (!inPos && entryEdge[i]) { events.push([i + 1, 'enter', ctx.adjOpen[i + 1] * (1 + cost)]); inPos = true; }
      else if (inPos && exitEdge[i]) { events.push([i + 1, 'exit', ctx.adjOpen[i + 1] * (1 - cost)]); inPos = false; }
    }

    var cash = startingCash, shares = 0, entryPrice = null, tradeReturns = [], trades = [];
    var equity = new Array(n).fill(null), ei = 0, hasOpen = false;
    for (i = ws; i < n; i++) {
      while (ei < events.length && events[ei][0] === i) {
        var kind = events[ei][1], price = events[ei][2];
        if (kind === 'enter') { shares = cash / price; cash = 0; entryPrice = price; trades.push({ enter: i, enterPrice: price }); }
        else {
          cash = shares * price;
          tradeReturns.push(price / entryPrice - 1);
          trades[trades.length - 1].exit = i;
          trades[trades.length - 1].exitPrice = price;
          trades[trades.length - 1].ret = price / entryPrice - 1;
          shares = 0; entryPrice = null;
        }
        ei++;
      }
      equity[i] = cash + shares * ctx.adjClose[i];
    }
    if (entryPrice !== null) {
      hasOpen = true;
      var mtm = ctx.adjClose[n - 1] / entryPrice - 1;
      tradeReturns.push(mtm);
      trades[trades.length - 1].ret = mtm;
      trades[trades.length - 1].open = true;
    }

    var eq0 = equity[ws], eqN = equity[n - 1];
    var totalReturn = eqN / eq0 - 1;
    var years = (ctx.days[n - 1] - ctx.days[ws]) / 365.25;
    var cagr = (years > 0 && eqN > 0) ? Math.pow(eqN / eq0, 1 / years) - 1 : totalReturn;
    var peak = -Infinity, maxDd = 0;
    for (i = ws; i < n; i++) {
      if (equity[i] > peak) peak = equity[i];
      var dd = (peak - equity[i]) / peak;
      if (dd > maxDd) maxDd = dd;
    }
    var wins = tradeReturns.filter(function (r) { return r > 0; }).length;
    var res = {
      entry: entry.label, exit: exit.label,
      totalReturn: totalReturn, cagr: cagr, maxDrawdown: maxDd,
      numTrades: tradeReturns.length,
      winRate: tradeReturns.length ? wins / tradeReturns.length : 0,
      hasOpenTrade: hasOpen
    };
    if (wantCurve) { res.equity = equity.slice(ws, n); res.trades = trades; }
    return res;
  }

  function buyHold(ctx, ws, we) {
    var start = ctx.adjClose[ws], end = ctx.adjClose[we];
    var total = end / start - 1;
    var years = (ctx.days[we] - ctx.days[ws]) / 365.25;
    return { totalReturn: total, cagr: years > 0 ? Math.pow(end / start, 1 / years) - 1 : total };
  }

  function pickBest(results) {
    var traded = results.filter(function (r) { return r.numTrades > 0; });
    if (!traded.length) return null;
    traded.sort(function (a, b) {
      return (b.cagr - a.cagr) || (a.maxDrawdown - b.maxDrawdown) || (a.numTrades - b.numTrades);
    });
    return traded[0];
  }

  // --- public entry point --------------------------------------------------
  function buildContext(bars, spec) {
    var interval = spec.interval || 'daily';
    var ladder = (spec.maPeriods && spec.maPeriods.length ? spec.maPeriods.slice() : DEFAULT_LADDERS[interval].slice())
      .map(Number).filter(function (v) { return v > 0; });
    ladder = ladder.filter(function (v, i) { return ladder.indexOf(v) === i; }).sort(function (a, b) { return a - b; });
    var kind = spec.maKind || 'sma';
    var barDf = resample(bars, interval);
    var adj = adjusted(barDf);
    return {
      interval: interval, ladder: ladder, kind: kind, bars: barDf,
      days: barDf.date.map(dayNum),
      ma: maColumns(barDf, ladder, kind),
      maCols: ladder.map(function (p) { return kind + p; }),
      pairs: adjacentPairs(ladder, kind),
      adjOpen: adj.adjOpen, adjClose: adj.adjClose,
      windowDays: spec.windowDays === undefined ? 30 : spec.windowDays,
      slopeLookback: spec.slopeLookback === undefined ? 10 : spec.slopeLookback,
      flatThresholdPct: spec.flatThresholdPct === undefined ? 0.5 : spec.flatThresholdPct,
      memo: {}
    };
  }

  function warmStart(ctx) {
    for (var i = 0; i < ctx.days.length; i++) {
      var warm = true;
      for (var k = 0; k < ctx.maCols.length; k++) {
        var col = ctx.ma[ctx.maCols[k]];
        if (!col || col[i] === null) { warm = false; break; }
      }
      if (warm) return i;
    }
    return -1;
  }

  function empty(note, ctx) {
    return {
      windowStart: null, windowEnd: null, numBars: 0, dataStart: null,
      note: note, buyHoldReturn: null, buyHoldCagr: null, results: [], best: null,
      interval: ctx ? ctx.interval : 'daily', maPeriods: ctx ? ctx.ladder : []
    };
  }

  /* Run the entry x exit grid. `spec.entries`/`spec.exits` are rule-spec strings
     (comma lists and group tokens allowed). Mirrors backtest.run_spec. */
  function runSpec(bars, spec) {
    if (!bars || !bars.date || !bars.date.length) return empty('no price history');
    var ctx = buildContext(bars, spec);
    var n = ctx.days.length;
    var ws = warmStart(ctx);
    if (ws < 0) {
      var need = minHistoryYears(ctx.interval, ctx.ladder);
      return empty('history too short to warm ' + Math.max.apply(null, ctx.ladder) + ' ' +
                   ctx.interval + ' bars (~' + need.toFixed(1) + 'y needed before the first tradable bar)', ctx);
    }
    var notes = [];
    if (spec.start) {
      var want = -1, sd = dayNum(spec.start);
      for (var i = 0; i < n; i++) if (ctx.days[i] >= sd) { want = i; break; }
      if (want < 0) return empty('start ' + spec.start + ' is after the last bar', ctx);
      if (want > ws) ws = want;
      else if (want < ws) notes.push('start clamped to ' + ctx.bars.date[ws] + ' (MA warm-up)');
    }
    var we = n - 1;
    if (spec.end) {
      var ed = dayNum(spec.end), found = -1;
      for (var j = 0; j < n; j++) if (ctx.days[j] <= ed) found = j;
      if (found < 0) return empty('end ' + spec.end + ' is before the first bar', ctx);
      we = found;
    }
    if (we <= ws) {
      return empty('window too short: only ' + Math.max(0, we - ws + 1) + ' tradable ' +
                   ctx.interval + ' bar(s) after MA warm-up', ctx);
    }
    var tradable = we - ws + 1, slowest = Math.max.apply(null, ctx.ladder);
    if (tradable < slowest) {
      notes.push('only ' + tradable + ' tradable ' + ctx.interval + ' bars after warm-up (the ' +
                 slowest + '-bar line alone needs ~' + minHistoryYears(ctx.interval, ctx.ladder).toFixed(1) +
                 'y of data) — widen the window');
    }

    var entries = expandRules(spec.entries === undefined ? 'degrees' : spec.entries, ctx);
    var exits = expandRules(spec.exits === undefined ? 'degrees' : spec.exits, ctx);
    var cost = spec.costBps === undefined ? 5 : spec.costBps;
    var cashAmt = spec.startingCash === undefined ? 10000 : spec.startingCash;
    var results = [];
    for (var e = 0; e < entries.length; e++) {
      for (var x = 0; x < exits.length; x++) {
        results.push(runOne(ctx, entries[e], exits[x], ws, we, cost, cashAmt, false));
      }
    }
    var bh = buyHold(ctx, ws, we);
    return {
      windowStart: ctx.bars.date[ws], windowEnd: ctx.bars.date[we],
      numBars: tradable, dataStart: ctx.bars.date[0], note: notes.join('; '),
      buyHoldReturn: bh.totalReturn, buyHoldCagr: bh.cagr,
      results: results, best: pickBest(results),
      interval: ctx.interval, maPeriods: ctx.ladder,
      _ctx: ctx, _ws: ws, _we: we, _cost: cost, _cash: cashAmt
    };
  }

  /* Equity curve + trade list for one cell of an already-run grid. Kept separate
     so the grid sweep never pays for curves nobody looks at. */
  function curveFor(run, entryLabel, exitLabel) {
    if (!run._ctx) return null;
    var res = runOne(run._ctx, parseRule(entryLabel), parseRule(exitLabel),
                     run._ws, run._we, run._cost, run._cash, true);
    var ctx = run._ctx, dates = ctx.bars.date.slice(run._ws, run._we + 1);
    var bh = [], base = ctx.adjClose[run._ws];
    for (var i = run._ws; i <= run._we; i++) bh.push(run._cash * ctx.adjClose[i] / base);
    return {
      dates: dates, equity: res.equity, buyHold: bh, trades: res.trades,
      close: ctx.adjClose.slice(run._ws, run._we + 1),
      ma: ctx.maCols.reduce(function (acc, c) { acc[c] = ctx.ma[c].slice(run._ws, run._we + 1); return acc; }, {}),
      result: res
    };
  }

  return {
    runSpec: runSpec, curveFor: curveFor, parseRule: parseRule, expandRules: expandRules,
    resample: resample, sma: sma, ema: ema, adjacentPairs: adjacentPairs,
    buildContext: buildContext, inTrailingWindow: inTrailingWindow,
    dayNum: dayNum, isoOf: isoOf, INTERVALS: INTERVALS,
    DEFAULT_LADDERS: DEFAULT_LADDERS, minHistoryYears: minHistoryYears
  };
}));
