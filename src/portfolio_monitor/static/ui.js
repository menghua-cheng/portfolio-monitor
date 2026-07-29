/* Interactive explorer UI (feature: 互動回測).
 *
 * Controls -> spec -> PM.runSpec -> table + charts, all client-side. Charts are
 * hand-rolled SVG rather than a charting library: inlining plotly.js would add
 * ~3.7MB to a file whose whole point is being portable, and the interactivity that
 * matters in a backtester is changing the parameters, not zooming the axes. Hover
 * readout is a crosshair plus one absolutely-positioned tooltip.
 *
 * Every number shown is computed here from the embedded bars; nothing is
 * precomputed except the self-check block, whose only job is to prove this engine
 * still agrees with the Python one that generated the file.
 */
'use strict';
(function () {
  var DATA = window.__PM_DATA__;
  var PM = window.PM;
  var UP = '#0a8f52', DOWN = '#c0392b', MUTED = '#66707a', ACCENT = '#1d6fb8';

  var $ = function (id) { return document.getElementById(id); };
  var run = null, curve = null, selected = null;

  function pct(x, digits) {
    if (x === null || x === undefined || !isFinite(x)) return '—';
    return (x >= 0 ? '+' : '') + (x * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  }
  function plainPct(x) {
    return (x === null || !isFinite(x)) ? '—' : (x * 100).toFixed(0) + '%';
  }
  function fg(x) { return x > 0 ? UP : (x < 0 ? DOWN : MUTED); }

  // --- spec from the controls ---------------------------------------------
  function currentSpec() {
    var ladderRaw = $('maPeriods').value.trim();
    var ladder = ladderRaw
      ? ladderRaw.split(',').map(function (s) { return parseInt(s, 10); }).filter(function (v) { return v > 0; })
      : null;
    return {
      interval: $('interval').value,
      maPeriods: ladder,
      maKind: $('maKind').value,
      entries: $('entries').value,
      exits: $('exits').value,
      start: $('start').value || '',
      end: $('end').value || '',
      windowDays: parseInt($('windowDays').value, 10),
      costBps: parseFloat($('costBps').value),
      startingCash: DATA.defaults.startingCash,
      slopeLookback: DATA.defaults.slopeLookback,
      flatThresholdPct: DATA.defaults.flatThresholdPct
    };
  }

  function currentBars() {
    var sym = $('symbol').value;
    for (var i = 0; i < DATA.tickers.length; i++) if (DATA.tickers[i].symbol === sym) return DATA.tickers[i];
    return DATA.tickers[0];
  }

  // --- SVG charting -------------------------------------------------------
  function svgEl(name, attrs) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (var k in attrs) if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]);
    return e;
  }

  /* series: [{name, color, values, dash}] over a shared `dates` axis.
     `logScale` matters for equity curves spanning orders of magnitude. */
  function lineChart(host, dates, series, opts) {
    opts = opts || {};
    host.innerHTML = '';
    var visible = series.filter(function (s) { return s.values && s.values.length; });
    if (!visible.length || dates.length < 2) {
      host.innerHTML = '<div class="muted pad">Not enough data to plot.</div>';
      return;
    }
    var W = host.clientWidth || 860, H = opts.height || 240;
    var padL = 54, padR = 10, padT = 10, padB = 22;
    var lo = Infinity, hi = -Infinity;
    visible.forEach(function (s) {
      s.values.forEach(function (v) {
        if (v === null || !isFinite(v)) return;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
    });
    if (!isFinite(lo)) { host.innerHTML = '<div class="muted pad">No values.</div>'; return; }
    var useLog = opts.log && lo > 0 && hi / lo > 20;
    var tf = useLog ? Math.log : function (v) { return v; };
    var tlo = tf(lo), thi = tf(hi), span = (thi - tlo) || 1;

    function x(i) { return padL + i * (W - padL - padR) / (dates.length - 1); }
    function y(v) { return padT + (thi - tf(v)) * (H - padT - padB) / span; }

    var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', height: H,
                             preserveAspectRatio: 'none' });
    // gridlines + y labels
    var ticks = 4;
    for (var t = 0; t <= ticks; t++) {
      var vv = useLog ? Math.exp(tlo + span * t / ticks) : lo + (hi - lo) * t / ticks;
      var yy = y(vv);
      svg.appendChild(svgEl('line', { x1: padL, y1: yy, x2: W - padR, y2: yy,
                                      stroke: '#eef1f4', 'stroke-width': 1 }));
      var lbl = svgEl('text', { x: 4, y: yy + 3, fill: MUTED, 'font-size': 10 });
      lbl.textContent = opts.fmtY ? opts.fmtY(vv) : vv.toFixed(0);
      svg.appendChild(lbl);
    }
    // x labels: first, middle, last
    [0, Math.floor(dates.length / 2), dates.length - 1].forEach(function (i) {
      var lbl = svgEl('text', { x: Math.min(x(i), W - 60), y: H - 6, fill: MUTED, 'font-size': 10 });
      lbl.textContent = dates[i];
      svg.appendChild(lbl);
    });
    // series
    visible.forEach(function (s) {
      var d = '', started = false;
      for (var i = 0; i < s.values.length; i++) {
        var v = s.values[i];
        if (v === null || !isFinite(v)) { started = false; continue; }
        d += (started ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1) + ' ';
        started = true;
      }
      var p = svgEl('path', { d: d, fill: 'none', stroke: s.color,
                              'stroke-width': s.width || 1.6, 'stroke-linejoin': 'round' });
      if (s.dash) p.setAttribute('stroke-dasharray', s.dash);
      svg.appendChild(p);
    });
    // trade markers
    (opts.markers || []).forEach(function (m) {
      if (m.i < 0 || m.i >= dates.length) return;
      var yy = y(m.value);
      svg.appendChild(svgEl('circle', { cx: x(m.i), cy: yy, r: 3.4, fill: m.color,
                                        stroke: '#fff', 'stroke-width': 1 }));
    });
    // hover crosshair
    var cross = svgEl('line', { y1: padT, y2: H - padB, stroke: '#9aa5b1',
                                'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0 });
    svg.appendChild(cross);
    var tip = host.parentNode.querySelector('.tip');
    svg.addEventListener('mousemove', function (ev) {
      var r = svg.getBoundingClientRect();
      var rel = (ev.clientX - r.left) / r.width * W;
      var i = Math.round((rel - padL) / (W - padL - padR) * (dates.length - 1));
      if (i < 0) i = 0; if (i >= dates.length) i = dates.length - 1;
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
      cross.setAttribute('opacity', 1);
      if (tip) {
        var lines = ['<b>' + dates[i] + '</b>'];
        visible.forEach(function (s) {
          var v = s.values[i];
          lines.push('<span style="color:' + s.color + '">■</span> ' + s.name + ': ' +
                     (v === null || !isFinite(v) ? '—' : (opts.fmtTip ? opts.fmtTip(v) : v.toFixed(2))));
        });
        tip.innerHTML = lines.join('<br>');
        tip.style.opacity = 1;
        tip.style.left = Math.min(ev.clientX - r.left + 12, r.width - 170) + 'px';
        tip.style.top = '8px';
      }
    });
    svg.addEventListener('mouseleave', function () {
      cross.setAttribute('opacity', 0);
      if (tip) tip.style.opacity = 0;
    });
    host.appendChild(svg);
  }

  // --- rendering ----------------------------------------------------------
  function renderSummary(tk) {
    var el = $('summary');
    if (!run.windowStart) {
      el.innerHTML = '<span class="bad">No result: ' + (run.note || 'insufficient history') + '</span>';
      return;
    }
    var traded = run.results.filter(function (r) { return r.numTrades > 0; });
    var beat = traded.filter(function (r) { return r.totalReturn > run.buyHoldReturn; }).length;
    var parts = [
      '<b>' + tk.symbol + '</b>' + (tk.name ? ' · ' + tk.name : ''),
      run.windowStart + ' → ' + run.windowEnd + ' (' + run.numBars + ' ' + run.interval + ' bars)',
      'MA ' + run.maPeriods.join('/'),
      'buy &amp; hold <b style="color:' + fg(run.buyHoldReturn) + '">' + pct(run.buyHoldReturn) +
        '</b> (' + pct(run.buyHoldCagr) + ' CAGR)',
      traded.length + '/' + run.results.length + ' cells traded, <b>' + beat + '</b> beat buy &amp; hold'
    ];
    el.innerHTML = parts.join(' &nbsp;·&nbsp; ') +
      (run.note ? '<div class="warn">' + run.note + '</div>' : '');
  }

  var SORTS = {
    cagr: function (a, b) { return (b.cagr - a.cagr) || (a.maxDrawdown - b.maxDrawdown); },
    'return': function (a, b) { return b.totalReturn - a.totalReturn; },
    drawdown: function (a, b) { return (a.maxDrawdown - b.maxDrawdown) || (b.cagr - a.cagr); },
    trades: function (a, b) { return a.numTrades - b.numTrades; },
    winrate: function (a, b) { return b.winRate - a.winRate; }
  };

  function renderGrid() {
    var host = $('grid');
    var traded = run.results.filter(function (r) { return r.numTrades > 0; });
    if (!traded.length) {
      host.innerHTML = '<div class="muted pad">No cell in this ' + run.results.length +
        '-cell grid ever traded in this window.</div>';
      return;
    }
    traded.sort(SORTS[$('sort').value]);
    var top = parseInt($('top').value, 10);
    var shown = top > 0 ? traded.slice(0, top) : traded;
    var head = ['entry', 'exit', 'return', 'CAGR', 'max DD', 'trades', 'win %'];
    var html = '<table><tr>' + head.map(function (h, i) {
      return '<th class="' + (i < 2 ? 'l' : '') + '">' + h + '</th>';
    }).join('') + '</tr>';
    shown.forEach(function (r) {
      var key = r.entry + '|' + r.exit;
      var beats = r.totalReturn > run.buyHoldReturn;
      html += '<tr data-key="' + key + '" class="row' + (key === selected ? ' sel' : '') + '">' +
        '<td class="l mono">' + r.entry + '</td><td class="l mono">' + r.exit + '</td>' +
        '<td style="color:' + fg(r.totalReturn) + '">' + pct(r.totalReturn) +
          (beats ? ' <span title="beat buy &amp; hold">★</span>' : '') + '</td>' +
        '<td style="color:' + fg(r.cagr) + '">' + pct(r.cagr) + '</td>' +
        '<td style="color:' + DOWN + '">-' + (r.maxDrawdown * 100).toFixed(1) + '%</td>' +
        '<td>' + r.numTrades + '</td>' +
        '<td>' + plainPct(r.winRate) + (r.hasOpenTrade ? ' *' : '') + '</td></tr>';
    });
    html += '</table>';
    html += '<div class="muted pad">Showing ' + shown.length + ' of ' + traded.length +
      ' traded cells by ' + $('sort').value + '. ★ beat buy &amp; hold. ' +
      '* position still open at the window end (marked to market). ' +
      'Click a row for its equity curve.</div>';
    host.innerHTML = html;
    Array.prototype.forEach.call(host.querySelectorAll('tr.row'), function (tr) {
      tr.addEventListener('click', function () { select(tr.getAttribute('data-key')); });
    });
  }

  function select(key) {
    selected = key;
    var bits = key.split('|');
    curve = PM.curveFor(run, bits[0], bits[1]);
    renderGrid();
    renderCharts();
  }

  function renderCharts() {
    if (!curve) { $('equity').innerHTML = ''; $('price').innerHTML = ''; $('cellInfo').innerHTML = ''; return; }
    var r = curve.result;
    $('cellInfo').innerHTML = '<b class="mono">' + r.entry + '</b> → <b class="mono">' + r.exit +
      '</b> &nbsp;·&nbsp; return <b style="color:' + fg(r.totalReturn) + '">' + pct(r.totalReturn) +
      '</b> &nbsp;·&nbsp; CAGR ' + pct(r.cagr) + ' &nbsp;·&nbsp; max DD <span style="color:' + DOWN +
      '">-' + (r.maxDrawdown * 100).toFixed(1) + '%</span> &nbsp;·&nbsp; ' + r.numTrades +
      ' trades, ' + plainPct(r.winRate) + ' win';

    var markers = [];
    curve.trades.forEach(function (t) {
      markers.push({ i: t.enter, value: curve.equity[t.enter], color: UP });
      if (t.exit !== undefined) markers.push({ i: t.exit, value: curve.equity[t.exit], color: DOWN });
    });
    lineChart($('equity'), curve.dates, [
      { name: 'strategy', color: ACCENT, values: curve.equity, width: 1.8 },
      { name: 'buy & hold', color: MUTED, values: curve.buyHold, dash: '4 3', width: 1.2 }
    ], { height: 230, log: true, markers: markers,
         fmtY: function (v) { return v >= 1000 ? (v / 1000).toFixed(0) + 'k' : v.toFixed(0); },
         fmtTip: function (v) { return v.toFixed(0); } });

    var priceSeries = [{ name: 'adj close', color: '#1f2933', values: curve.close, width: 1.4 }];
    var palette = ['#e8833a', '#1d6fb8', '#8e44ad', '#16a085', '#c0392b'];
    Object.keys(curve.ma).forEach(function (k, i) {
      priceSeries.push({ name: k, color: palette[i % palette.length], values: curve.ma[k], width: 1 });
    });
    var pm = [];
    curve.trades.forEach(function (t) {
      pm.push({ i: t.enter, value: curve.close[t.enter], color: UP });
      if (t.exit !== undefined) pm.push({ i: t.exit, value: curve.close[t.exit], color: DOWN });
    });
    lineChart($('price'), curve.dates, priceSeries, { height: 230, log: true, markers: pm });
  }

  // --- run ----------------------------------------------------------------
  function compute() {
    var tk = currentBars();
    $('status').textContent = 'computing…';
    // Yield once so the status paints before a large grid blocks the thread.
    requestAnimationFrame(function () {
      var t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      try {
        run = PM.runSpec(tk.bars, currentSpec());
      } catch (e) {
        $('summary').innerHTML = '<span class="bad">' + (e.message || e) + '</span>';
        $('grid').innerHTML = ''; $('status').textContent = '';
        curve = null; renderCharts();
        return;
      }
      var ms = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
      renderSummary(tk);
      selected = null; curve = null;
      renderGrid();
      var traded = run.results.filter(function (r) { return r.numTrades > 0; });
      if (traded.length) {
        traded.sort(SORTS[$('sort').value]);
        select(traded[0].entry + '|' + traded[0].exit);
      } else { renderCharts(); }
      $('status').textContent = run.results.length + ' cells in ' + ms.toFixed(0) + ' ms';
      $('dataSpan').textContent = tk.span;
    });
  }

  // --- self-check ---------------------------------------------------------
  /* Recompute the spec the generator recorded and compare against Python's answer.
     This is what keeps a file honest after the Python side moves on. */
  function selfCheck() {
    var sc = DATA.selfcheck, banner = $('selfcheck');
    if (!sc) return;
    var tk = null;
    for (var i = 0; i < DATA.tickers.length; i++) if (DATA.tickers[i].symbol === sc.symbol) tk = DATA.tickers[i];
    if (!tk) return;
    var got, diffs = [];
    try { got = PM.runSpec(tk.bars, sc.spec); }
    catch (e) { diffs.push('engine threw: ' + (e.message || e)); }
    if (got) {
      var want = sc.expected;
      if (got.windowStart !== want.windowStart || got.windowEnd !== want.windowEnd) {
        diffs.push('window ' + got.windowStart + '→' + got.windowEnd +
                   ' vs expected ' + want.windowStart + '→' + want.windowEnd);
      }
      if (got.results.length !== want.results.length) {
        diffs.push('grid size ' + got.results.length + ' vs ' + want.results.length);
      } else {
        var byKey = {};
        got.results.forEach(function (r) { byKey[r.entry + '|' + r.exit] = r; });
        want.results.forEach(function (w) {
          var g = byKey[w.entry + '|' + w.exit];
          if (!g) { diffs.push('missing cell ' + w.entry + '×' + w.exit); return; }
          ['totalReturn', 'cagr', 'maxDrawdown', 'winRate'].forEach(function (k) {
            if (Math.abs(g[k] - w[k]) > 1e-9) {
              diffs.push(w.entry + '×' + w.exit + ' ' + k + ': ' + g[k] + ' vs ' + w[k]);
            }
          });
          if (g.numTrades !== w.numTrades) {
            diffs.push(w.entry + '×' + w.exit + ' trades ' + g.numTrades + ' vs ' + w.numTrades);
          }
        });
      }
    }
    if (diffs.length) {
      banner.className = 'banner bad-banner';
      banner.innerHTML = '<b>Engine mismatch.</b> This page\'s in-browser engine disagrees ' +
        'with the Python engine that generated it, so the numbers below may be wrong. ' +
        'Regenerate the file. First differences: ' + diffs.slice(0, 4).join('; ');
    } else {
      banner.className = 'banner ok-banner';
      banner.innerHTML = 'Engine self-check passed — the in-browser results match the ' +
        'Python engine that generated this file (' + DATA.selfcheck.expected.results.length +
        ' cells verified).';
    }
  }

  // --- wiring -------------------------------------------------------------
  function init() {
    var d = DATA.defaults;
    DATA.tickers.forEach(function (t) {
      var o = document.createElement('option');
      o.value = t.symbol;
      o.textContent = t.symbol + (t.name ? ' — ' + t.name : '');
      $('symbol').appendChild(o);
    });
    $('symbol').value = d.symbol;
    $('interval').value = d.interval;
    $('maKind').value = d.maKind;
    $('entries').value = d.entries;
    $('exits').value = d.exits;
    $('windowDays').value = d.windowDays;
    $('costBps').value = d.costBps;
    $('sort').value = d.sort;

    // Changing the interval swaps the ladder placeholder, since periods count in bars.
    function syncLadderHint() {
      var lad = PM.DEFAULT_LADDERS[$('interval').value];
      $('maPeriods').placeholder = lad.join(',') + '  (default for ' + $('interval').value + ')';
    }
    $('interval').addEventListener('change', syncLadderHint);
    syncLadderHint();

    ['symbol', 'interval', 'maKind', 'maPeriods', 'entries', 'exits', 'start', 'end',
     'windowDays', 'costBps'].forEach(function (id) {
      $(id).addEventListener('change', compute);
    });
    ['sort', 'top'].forEach(function (id) {
      $(id).addEventListener('change', function () { renderGrid(); });
    });
    $('runBtn').addEventListener('click', compute);
    Array.prototype.forEach.call(document.querySelectorAll('[data-preset]'), function (b) {
      b.addEventListener('click', function () {
        var p = JSON.parse(b.getAttribute('data-preset'));
        Object.keys(p).forEach(function (k) { if ($(k)) $(k).value = p[k]; });
        syncLadderHint();
        compute();
      });
    });
    window.addEventListener('resize', function () { renderCharts(); });

    selfCheck();
    compute();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}());
