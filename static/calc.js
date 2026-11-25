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

  const api = { MARKET_WEIGHT, chance, agrees, confidence, CONF_RANK, betLabel, americanOdds, lineGroup, bestLines };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Calc = api;
})(this);
