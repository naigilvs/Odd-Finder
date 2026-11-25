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
