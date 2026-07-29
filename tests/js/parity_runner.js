/* Parity runner: feed the JS engine the fixture the Python engine produced.
 *
 * Usage: node parity_runner.js <fixture.json>
 * Prints one JSON object: {cases: [{spec, actual}]} — the caller compares it
 * against the fixture's `expected` blocks. Kept dumb on purpose; all judgement
 * about what "equal" means lives in the pytest that calls this.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const enginePath = path.resolve(__dirname, '../../src/portfolio_monitor/static/engine.js');
const PM = require(enginePath);

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function normalize(run) {
  return {
    windowStart: run.windowStart,
    windowEnd: run.windowEnd,
    numBars: run.numBars,
    dataStart: run.dataStart,
    buyHoldReturn: run.buyHoldReturn,
    buyHoldCagr: run.buyHoldCagr,
    interval: run.interval,
    maPeriods: run.maPeriods,
    results: run.results
      .map(r => ({
        entry: r.entry, exit: r.exit,
        totalReturn: r.totalReturn, cagr: r.cagr, maxDrawdown: r.maxDrawdown,
        numTrades: r.numTrades, winRate: r.winRate, hasOpenTrade: r.hasOpenTrade
      }))
      .sort((a, b) => (a.entry < b.entry ? -1 : a.entry > b.entry ? 1
        : a.exit < b.exit ? -1 : a.exit > b.exit ? 1 : 0))
  };
}

const out = { cases: [] };
for (const c of fixture.cases) {
  let actual;
  try {
    actual = normalize(PM.runSpec(fixture.bars, c.spec));
  } catch (e) {
    actual = { error: String(e && e.message || e) };
  }
  out.cases.push({ spec: c.spec, actual });
}
process.stdout.write(JSON.stringify(out));
