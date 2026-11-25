// Tests for the page's calculations in static/calc.js. Run: ./run_tests.sh (needs Node.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Calc = require('../static/calc.js');

const bet = (over = {}) => ({
  event_id: 'g1', market: 'spreads', outcome: 'Lakers', point: -3.5, selection: 'Lakers -3.5',
  market_label: 'Point spread', fair_prob: 0.55, book_prob: 0.5, ev: 0.05, books_used: ['A', 'B', 'C', 'D', 'E', 'F', 'G'],
  sharp: true, player: null, team_total_for: null, estimated: false, ...over,
});

test('chance of success blends market 75% and stats 25%', () => {
  assert.equal(Calc.chance(bet(), { prob: 0.65 }), 0.75 * 0.55 + 0.25 * 0.65);
  assert.equal(Calc.chance(bet(), undefined), 0.55);
  assert.equal(Calc.chance(bet(), { reason: 'not covered' }), 0.55);
});

test('market and stats agree only when both beat the price', () => {
  assert.equal(Calc.agrees(bet(), { prob: 0.56 }), true);
  assert.equal(Calc.agrees(bet(), { prob: 0.45 }), false);
  assert.equal(Calc.agrees(bet({ ev: -0.01 }), { prob: 0.56 }), false);
});

test('confidence levels', () => {
  assert.equal(Calc.confidence(bet(), { prob: 0.56 }).level, 'high');          // 7 books + sharp + agree
  assert.equal(Calc.confidence(bet({ books_used: ['A', 'B', 'C', 'D'] })).level, 'medium');
  assert.equal(Calc.confidence(bet({ books_used: ['A'], sharp: false })).level, 'low');
  assert.equal(Calc.confidence(bet(), { prob: 0.3 }).level, 'medium');          // stats disagree
  const guessed = Calc.confidence(bet({ estimated: true, player: 'P', books_used: ['A', 'B', 'C', 'D', 'E'] }));
  assert.equal(guessed.level, 'low');
  assert.match(guessed.why, /estimated/);
});

test('bet labels name the stat once', () => {
  assert.equal(Calc.betLabel(bet({ player: 'J. Smith', selection: 'J. Smith Over 6.5', market_label: 'Player assists' })),
    'J. Smith Over 6.5 assists');
  assert.equal(Calc.betLabel(bet({ player: 'J. Smith', selection: 'J. Smith Over 6.5 assists', labeled: true })),
    'J. Smith Over 6.5 assists');
  assert.equal(Calc.betLabel(bet()), 'Lakers -3.5');
});

test('american odds', () => {
  assert.equal(Calc.americanOdds(2.5), '+150');
  assert.equal(Calc.americanOdds(1.5), '-200');
});

test('best line per bet keeps the top value and counts the rest', () => {
  const rows = [
    bet({ market: 'alternate_spreads', point: -1.5, selection: 'Lakers -1.5', ev: -0.02 }),
    bet({ market: 'alternate_spreads', point: -6.5, selection: 'Lakers -6.5', ev: 0.04 }),
    bet({ market: 'alternate_spreads', point: -9.5, selection: 'Lakers -9.5', ev: 0.01 }),
    bet({ market: 'alternate_spreads', outcome: 'Celtics', point: 6.5, selection: 'Celtics +6.5', ev: -0.05 }),
    bet({ market: 'spreads_h1', point: -1.5, selection: 'Lakers -1.5, 1st half', ev: 0.0 }),
  ];
  const best = Calc.bestLines(rows);
  assert.equal(best.length, 3);                       // Lakers spread, Celtics spread, 1st half spread
  const lakers = best.find(r => r.selection === 'Lakers -6.5');
  assert.ok(lakers);
  assert.equal(lakers.others.length, 2);
  assert.deepEqual(lakers.others.map(r => r.selection), ['Lakers -9.5', 'Lakers -1.5']);
});

test('player props group by player and side', () => {
  const p = (who, side, pt, ev) => bet({ market: 'player_points', player: who, outcome: side, point: pt, selection: `${who} ${side} ${pt}`, ev });
  const best = Calc.bestLines([p('A', 'Over', 20.5, 0.01), p('A', 'Over', 24.5, 0.03), p('A', 'Under', 20.5, 0), p('B', 'Over', 20.5, 0)]);
  assert.equal(best.length, 3);
});

test('saved picks: profit and summary', () => {
  const p = (status, decimal, chance, confidence, stake) => ({ status, decimal, chance, confidence, stake });
  assert.equal(Calc.pickProfit(p('won', 2.5), 100), 150);
  assert.equal(Calc.pickProfit(p('lost', 2.5), 100), -100);
  assert.equal(Calc.pickProfit(p('push', 2.5), 100), 0);
  const s = Calc.summarize([
    p('won', 2.0, 0.6, 'High', 50), p('lost', 1.8, 0.6, 'High'), p('won', 3.0, 0.3, 'Low'),
    p('push', 1.9, 0.5, 'Medium'), p('pending', 2.0, 0.5, 'High'),
  ]);
  assert.equal(s.total, 5);
  assert.equal(s.pending, 1);
  assert.deepEqual([s.wins, s.losses, s.pushes], [2, 1, 1]);
  assert.equal(s.winRate, 2 / 3);
  assert.ok(Math.abs(s.expectedRate - 0.5) < 1e-9);         // (0.6 + 0.6 + 0.3) / 3
  assert.equal(s.flatProfit, 100 - 100 + 200 + 0);
  assert.equal(s.realProfit, 50);
  assert.equal(s.byConf.High.picks, 2);
  assert.equal(s.byConf.High.winRate, 0.5);
  assert.equal(s.byConf.Medium.picks, 0);                    // pushes don't count as decided
});

test('my wagers: net and totals', () => {
  const w = (result, wagered, won = 0) => ({ result, wagered, won });
  assert.equal(Calc.wagerNet(w('won', 50, 45.45)), 45.45);
  assert.equal(Calc.wagerNet(w('lost', 20)), -20);
  assert.equal(Calc.wagerNet(w('push', 30)), 0);
  const s = Calc.wagerSummary([w('won', 50, 45.45), w('lost', 20), w('push', 30), w('lost', 10)]);
  assert.deepEqual([s.count, s.won, s.lost, s.push], [4, 1, 2, 1]);
  assert.equal(s.wagered, 110);
  assert.equal(s.net, 15.45);
  assert.ok(Math.abs(s.roi - 15.45 / 110) < 1e-9);
  assert.equal(Calc.wagerSummary([]).roi, null);
});
