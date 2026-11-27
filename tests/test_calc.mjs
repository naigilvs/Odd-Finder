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

test('my wagers: pending bets are kept out of profit and record', () => {
  const w = (result, wagered, won = 0) => ({ result, wagered, won });
  const s = Calc.wagerSummary([w('won', 50, 45.45), w('lost', 20), w('pending', 40), w('pending', 10)]);
  assert.deepEqual([s.count, s.won, s.lost, s.pending], [2, 1, 1, 2]);
  assert.equal(s.wagered, 70);
  assert.equal(s.net, 25.45);
  assert.equal(s.atStake, 50);
  assert.equal(Calc.wagerNet(w('pending', 40)), 0);
});

test('amount won from the odds', () => {
  assert.equal(Calc.winFromOdds(50, 2.5), 75);       // +150
  assert.equal(Calc.winFromOdds(50, 1.909), 45.45);  // -110
});

test('my wagers: extra stats', () => {
  const w = (result, wagered, won, date, note) => ({ result, wagered, won: won || 0, date, note, created: date });
  const s = Calc.wagerSummary([
    w('won', 50, 45, '2026-11-01', 'A'), w('lost', 30, 0, '2026-11-02', 'B'), w('won', 20, 60, '2026-11-03', 'C'),
    w('push', 10, 0, '2026-11-04', 'D'), w('won', 10, 9, '2026-11-05', 'E'), w('pending', 25, 0, '2026-11-06', 'F'),
  ]);
  assert.equal(s.totalWon, 114);
  assert.equal(s.totalLost, 30);
  assert.equal(s.net, 84);                               // total won - total lost
  assert.equal(s.winRate, 3 / 4);                        // pushes don't count
  assert.equal(s.avgBet, 24);                            // 120 on 5 settled bets
  assert.equal(s.biggestWin.note, 'C');
  assert.equal(s.biggestLoss.note, 'B');
  assert.deepEqual(s.streak, { result: 'won', length: 2 });   // E and C (push D skipped)
  assert.equal(Calc.wagerSummary([]).streak, null);
});

test('parlays: combined odds and result', () => {
  const leg = (decimal, status = 'pending') => ({ decimal, status });
  assert.equal(Calc.parlayDecimal([leg(1.909), leg(1.909), leg(1.909)]), 6.9569);   // about +596
  assert.equal(Calc.winFromOdds(10, Calc.parlayDecimal([leg(1.909), leg(1.909), leg(1.909)])), 59.57);
  assert.equal(Calc.parlayDecimal([leg(1.909), leg(1.909, 'push'), leg(1.909)]), 3.6443);  // pushed leg drops out
  assert.equal(Calc.parlayDecimal([leg(2.0), {}]), null);                             // a leg with no odds
  assert.equal(Calc.parlayResult(['won', 'lost', 'pending']), 'lost');
  assert.equal(Calc.parlayResult(['won', 'pending']), null);
  assert.equal(Calc.parlayResult(['won', 'push']), 'won');
  assert.equal(Calc.parlayResult(['push', 'push']), 'push');
});

test('injury news lowers confidence', () => {
  const inj = (level, status) => ({ player: { name: 'J. Smith', level, status } });
  const prop = bet({ player: 'J. Smith', sharp: true });                   // 7 books + sharp - prop = medium
  assert.equal(Calc.confidence(prop).level, 'medium');
  assert.equal(Calc.confidence(prop, undefined, inj('question', 'Questionable')).level, 'low');
  const out = Calc.confidence(bet(), { prob: 0.56 }, inj('out', 'Out'));    // even a high one drops to low
  assert.equal(out.level, 'low');
  assert.match(out.why, /listed out on the injury report/);
  assert.equal(Calc.confidence(bet(), { prob: 0.56 }, {}).level, 'high');   // not on the report
});

test('bankroll: balance, history and suggested stakes', () => {
  const S = { start: 500, unit_pct: 1, kelly: 0.25, cap_pct: 2, max_stake: 0, daily_loss: 0, exposure_pct: 10 };
  const w = (date, result, wagered, won = 0) => ({ date, result, wagered, won });
  const list = [w('2026-11-02', 'lost', 20), w('2026-11-01', 'won', 50, 45), w('2026-11-02', 'won', 10, 9), w('2026-11-03', 'pending', 30)];
  assert.equal(Calc.balance(S, list), 534);
  assert.deepEqual(Calc.balanceHistory(S, list), [{ date: '2026-11-01', balance: 545 }, { date: '2026-11-02', balance: 534 }]);
  // 55% at -110: full Kelly ~5.5%, quarter ~1.4% -> about $7 on $500
  const k = Calc.kellyStake(0.55, 1.909, 500, S, 'medium');
  assert.equal(k.stake, 6.87);
  assert.ok(Math.abs(k.full - 0.055) < 0.001);
  assert.equal(Calc.kellyStake(0.55, 1.909, 500, S, 'low').stake, 3.43);     // halved for low confidence
  assert.equal(Calc.kellyStake(0.5, 1.909, 500, S).stake, 0);                // no edge, no bet
  const big = Calc.kellyStake(0.7, 2.0, 500, S);
  assert.deepEqual([big.stake, big.capped], [10, true]);                      // capped at 2%
  assert.equal(Calc.kellyStake(0.6, 2.0, 0, S).stake, 0);                    // no bankroll set
});

test('bankroll: limit warnings', () => {
  const S = { start: 500, unit_pct: 1, kelly: 0.25, cap_pct: 2, max_stake: 25, daily_loss: 40, exposure_pct: 10 };
  const w = (result, wagered, date = '2026-11-25') => ({ date, result, wagered, won: result === 'won' ? wagered : 0 });
  assert.deepEqual(Calc.bankrollWarnings([w('lost', 10)], [], S, '2026-11-25'), []);
  assert.match(Calc.bankrollWarnings([w('pending', 30)], [], S, '2026-11-25')[0], /above your \$25 maximum/);
  const day = Calc.bankrollWarnings([w('lost', 15)], [w('lost', 25)], S, '2026-11-25');
  assert.match(day[0], /down \$40 today/);
  assert.equal(Calc.bankrollWarnings([w('lost', 15)], [w('lost', 25, '2026-11-24')], S, '2026-11-25').length, 0);  // yesterday doesn't count
  const exp = Calc.bankrollWarnings([w('pending', 20)], [w('pending', 25), w('pending', 10)], S, '2026-11-25');
  assert.match(exp[0], /\$55 waiting on pending bets, more than 10% of your \$500/);
  assert.deepEqual(Calc.bankrollWarnings([w('pending', 100)], [], { ...S, max_stake: 0, exposure_pct: 0 }, '2026-11-25'), []);  // limits off
});

test('one side per bet: best value or most likely to win', () => {
  const side = (outcome, ev, ch, extra = {}) => bet({ market: 'player_points', player: 'P', outcome, point: 24.5,
    selection: `P ${outcome} 24.5`, ev, fair_prob: ch, ...extra });
  const rows = [side('Over', 0.03, 0.42), side('Under', -0.06, 0.58),                       // value and chance split
                bet({ market: 'h2h', outcome: 'Lakers', selection: 'Lakers', point: null, ev: 0.01, fair_prob: 0.6 }),
                bet({ market: 'h2h', outcome: 'Celtics', selection: 'Celtics', point: null, ev: -0.05, fair_prob: 0.4 }),
                side('Over', 0.01, 0.40, { market: 'alternate_player_points', point: 26.5, selection: 'P Over 26.5' })];
  const ch = r => r.fair_prob;
  const value = Calc.pickSides(rows, 'value', ch), likely = Calc.pickSides(rows, 'likely', ch);
  assert.equal(value.length, 2);                                  // one per bet (alt line joins its bet)
  assert.deepEqual(value.map(r => r.selection).sort(), ['Lakers', 'P Over 24.5']);
  assert.deepEqual(likely.map(r => r.selection).sort(), ['Lakers', 'P Under 24.5']);
  const lakers = value.find(r => r.selection === 'Lakers');
  assert.equal(lakers.agree_sides, true);                         // best value and likelier agree
  assert.equal(value.find(r => r.player).agree_sides, false);
  assert.equal(Calc.betGroup(rows[0]), Calc.betGroup(rows[4]));
});

test('free odds points estimate', () => {
  assert.equal(Calc.freePoints(['basketball_nba'], {}), 30 * 20);
  assert.equal(Calc.freePoints(['basketball_nba'], { basketball_nba: 12 }), 30 * 20);   // the usual slate is the floor
  assert.equal(Calc.freePoints(['basketball_nba'], { basketball_nba: 45 }), 45 * 20);   // a busier slate counts
  assert.ok(Calc.freePoints(['basketball_ncaab'], {}) >= 6000);                         // college costs the most
  assert.equal(Calc.freePoints(['soccer_epl'], {}), 10 * 20);
  assert.equal(Calc.freePoints([], {}), 0);
});
