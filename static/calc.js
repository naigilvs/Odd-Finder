/*
 * Odd Finder calculations: pure functions with no page or browser code, so they
 * can be tested on their own (tests/test_calc.mjs). `s` is a bet's stats opinion
 * ({prob, ...} or {reason}) or undefined.
 */
(function (root) {
  // Chance of success: the market's estimate, blended 75/25 with the stats one when there is one.
  // The market gets more weight because it tested as more accurate.
  const MARKET_WEIGHT = 0.75;

  function chance(r, s) {
    return s && s.prob != null ? MARKET_WEIGHT * r.fair_prob + (1 - MARKET_WEIGHT) * s.prob : r.fair_prob;
  }

  // Both opinions think the bet's chance is higher than the sportsbook's price implies.
  function agrees(r, s) {
    return !!(s && s.prob != null && r.ev > 0 && s.prob > r.book_prob);
  }

  // Confidence: how much to trust the chance of success, from points for and against.
  function confidence(r, s) {
    let score = 0;
    const why = [];
    const n = r.books_used.length;
    if (n >= 7) { score += 2; why.push(`compared with ${n} sportsbooks`); }
    else if (n >= 4) { score += 1; why.push(`compared with ${n} sportsbooks`); }
    else why.push(`only ${n} sportsbook${n === 1 ? '' : 's'} to compare with`);
    if (r.sharp) { score += 1; why.push('includes sharp books like Pinnacle'); }
    if (s && s.prob != null) {
      const sameSide = (s.prob > r.book_prob) === (r.fair_prob > r.book_prob);
      if (sameSide && Math.abs(s.prob - r.fair_prob) <= 0.1) { score += 1; why.push('market and stats agree'); }
      else { score -= 1; why.push('market and stats disagree'); }
    }
    if (r.player) { score -= 1; why.push('player props are thinner markets'); }
    if (r.estimated) { score -= 2; why.push('books only offer the "Yes" side, so their margin had to be estimated'); }
    const level = score >= 3 ? 'high' : score >= 1 ? 'medium' : 'low';
    return { level, label: level[0].toUpperCase() + level.slice(1), why: why.join(', ') };
  }
  const CONF_RANK = { low: 0, medium: 1, high: 2 };

  // Player props name the stat: "J. Carter Under 6.5 assists". Newer scans already include it (`labeled`).
  function betLabel(r) {
    if (r.labeled || !r.player) return r.selection;
    const stat = r.market_label.replace(/^(Player|Batter|Pitcher)\s+/i, '').toLowerCase();
    return `${r.selection} ${stat}`;
  }

  const americanOdds = d => d >= 2 ? '+' + Math.round((d - 1) * 100) : String(Math.round(-100 / (d - 1)));

  // Which bet a line belongs to, ignoring the number: "Lakers spread", "Over total, 1st half",
  // "J. Smith Over points". Alternate and main lines of the same bet share a group.
  function lineGroup(r) {
    // The side is the team (spreads, moneylines) or Over/Under (totals, team totals, props).
    const base = r.market.replace(/^alternate_/, '');
    return [r.event_id, base, r.player || '', r.team_total_for || '', r.outcome].join('|');
  }

  // Keep the best-value line of each group; the others ride along on it as `others`.
  function bestLines(rows) {
    const groups = new Map();
    for (const r of rows) {
      const k = lineGroup(r);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    return [...groups.values()].map(g => {
      const sorted = g.slice().sort((a, b) => b.ev - a.ev);
      return Object.assign({}, sorted[0], { others: sorted.slice(1) });
    });
  }

  // ---- saved picks
  // Profit on one pick at a given stake (a push returns the stake: 0 profit).
  function pickProfit(p, stake) {
    if (p.status === 'won') return stake * (p.decimal - 1);
    if (p.status === 'lost') return -stake;
    return 0;
  }

  // Record, win rate vs. what the bot expected, and profit, overall and by confidence level.
  function summarize(picks) {
    const settled = picks.filter(p => p.status === 'won' || p.status === 'lost' || p.status === 'push');
    const decided = settled.filter(p => p.status !== 'push');
    const wins = decided.filter(p => p.status === 'won').length;
    const expectedWins = decided.reduce((a, p) => a + (p.chance != null ? p.chance : p.fair_prob || 0), 0);
    const staked = settled.filter(p => p.stake > 0);
    const byConf = {};
    for (const level of ['High', 'Medium', 'Low']) {
      const g = decided.filter(p => p.confidence === level);
      byConf[level] = {
        picks: g.length,
        wins: g.filter(p => p.status === 'won').length,
        winRate: g.length ? g.filter(p => p.status === 'won').length / g.length : null,
        expectedRate: g.length ? g.reduce((a, p) => a + (p.chance != null ? p.chance : 0), 0) / g.length : null,
      };
    }
    return {
      total: picks.length,
      pending: picks.length - settled.length,
      wins, losses: decided.length - wins, pushes: settled.length - decided.length,
      winRate: decided.length ? wins / decided.length : null,
      expectedRate: decided.length ? expectedWins / decided.length : null,
      flatProfit: settled.reduce((a, p) => a + pickProfit(p, 100), 0),     // if every pick had been $100
      flatPicks: settled.length,
      realProfit: staked.reduce((a, p) => a + pickProfit(p, p.stake), 0),
      realRisked: staked.reduce((a, p) => a + p.stake, 0),
      stakedPicks: staked.length,
      byConf,
    };
  }

  // ---- my wagers: what you actually bet, and won or lost
  // Profit on a winning bet from its decimal odds: $50 at 2.50 (+150) wins $75.
  function winFromOdds(wagered, decimal) {
    return Math.round(wagered * (decimal - 1) * 100) / 100;
  }
  function wagerNet(w) {
    return w.result === 'won' ? w.won : w.result === 'lost' ? -w.wagered : 0;
  }
  // Pending bets (result not set yet) are kept out of profit and record: shown as money at stake.
  function wagerSummary(list) {
    const s = { count: 0, wagered: 0, net: 0, won: 0, lost: 0, push: 0, pending: 0, atStake: 0 };
    for (const w of list) {
      if (w.result === 'pending') { s.pending += 1; s.atStake += w.wagered; continue; }
      s.count += 1;
      s.wagered += w.wagered;
      s.net += wagerNet(w);
      s[w.result] += 1;
    }
    s.atStake = Math.round(s.atStake * 100) / 100;
    s.wagered = Math.round(s.wagered * 100) / 100;
    s.net = Math.round(s.net * 100) / 100;
    s.roi = s.wagered ? s.net / s.wagered : null;
    return s;
  }

  const api = { MARKET_WEIGHT, chance, agrees, confidence, CONF_RANK, betLabel, americanOdds, lineGroup, bestLines,
                pickProfit, summarize, winFromOdds, wagerNet, wagerSummary };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Calc = api;
})(this);
