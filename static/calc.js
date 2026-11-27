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
  // `inj` is the bet's injury news ({player} for a prop whose player is on the report).
  function confidence(r, s, inj) {
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
    const p = inj && inj.player;
    if (p) {
      // Out: the prices are probably stale. Doubtful / questionable: the player may not play.
      score -= p.level === 'out' ? 10 : p.level === 'doubtful' ? 3 : 2;
      why.push(`${p.name} is listed ${p.status.toLowerCase()} on the injury report`);
    }
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

  // ---- one side per bet
  // Every side and line of one bet share a key: LeBron Over and Under 24.5 (and 26.5),
  // Knicks -4.5 and Celtics +4.5, all three outcomes of a soccer moneyline.
  function betGroup(r) {
    const base = r.market.replace(/^alternate_/, '');
    return [r.event_id, base, r.player || '', r.team_total_for || ''].join('|');
  }

  // One row per bet: the best-value side ("value": value first, chance to break ties) or the
  // side most likely to win ("likely": chance first). Each kept row gets `agree`: true when
  // both ways choose it. `chanceOf` is the page's chance of success (market + stats).
  function pickSides(rows, mode, chanceOf) {
    const groups = new Map();
    for (const r of rows) {
      const k = betGroup(r);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    const best = (g, first, second) => g.reduce((a, b) => (first(b) > first(a) || (first(b) === first(a) && second(b) > second(a))) ? b : a);
    const ev = r => r.ev, ch = r => chanceOf(r);
    return [...groups.values()].map(g => {
      const byValue = best(g, ev, ch), byChance = best(g, ch, ev);
      const keep = mode === 'likely' ? byChance : byValue;
      return Object.assign({}, keep, { agree_sides: byValue === byChance });
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
  // ---- parlays
  // Combined decimal odds: the legs' odds multiplied. Pushed legs drop out (as sportsbooks
  // do); null if any other leg's odds aren't known. Three -110 legs: ~6.96, about +596.
  function parlayDecimal(legs) {
    let d = 1;
    for (const leg of legs) {
      if (leg.status === 'push') continue;
      if (!(leg.decimal > 1)) return null;
      d *= leg.decimal;
    }
    return Math.round(d * 10000) / 10000;
  }
  // The parlay's result from its legs' results: any loss loses it; it's decided only once
  // every leg is won or pushed; all pushes return the stake.
  function parlayResult(statuses) {
    if (statuses.includes('lost')) return 'lost';
    if (statuses.some(s => s !== 'won' && s !== 'push')) return null;
    return statuses.every(s => s === 'push') ? 'push' : 'won';
  }

  function wagerNet(w) {
    return w.result === 'won' ? w.won : w.result === 'lost' ? -w.wagered : 0;
  }
  // Pending bets (result not set yet) are kept out of profit and record: shown as money at stake.
  function wagerSummary(list) {
    const r2 = x => Math.round(x * 100) / 100;
    const s = { count: 0, wagered: 0, net: 0, won: 0, lost: 0, push: 0, pending: 0, atStake: 0,
                totalWon: 0, totalLost: 0, biggestWin: null, biggestLoss: null, streak: null };
    for (const w of list) {
      if (w.result === 'pending') { s.pending += 1; s.atStake += w.wagered; continue; }
      s.count += 1;
      s.wagered += w.wagered;
      s.net += wagerNet(w);
      s[w.result] += 1;
      if (w.result === 'won') {
        s.totalWon += w.won;
        if (!s.biggestWin || w.won > s.biggestWin.won) s.biggestWin = w;
      }
      if (w.result === 'lost') {
        s.totalLost += w.wagered;
        if (!s.biggestLoss || w.wagered > s.biggestLoss.wagered) s.biggestLoss = w;
      }
    }
    for (const k of ['atStake', 'wagered', 'net', 'totalWon', 'totalLost']) s[k] = r2(s[k]);
    s.roi = s.wagered ? s.net / s.wagered : null;
    s.winRate = s.won + s.lost ? s.won / (s.won + s.lost) : null;          // pushes don't count
    s.avgBet = s.count ? r2(s.wagered / s.count) : null;
    // Current streak: the latest settled results in a row (pushes skipped), e.g. won 3.
    const settled = list.filter(w => w.result === 'won' || w.result === 'lost')
      .sort((a, b) => (b.date + (b.created || '')).localeCompare(a.date + (a.created || '')));
    if (settled.length) {
      let n = 0;
      while (n < settled.length && settled[n].result === settled[0].result) n++;
      s.streak = { result: settled[0].result, length: n };
    }
    return s;
  }

  // ---- bankroll
  // Balance: the starting bankroll plus the net of every settled wager.
  function balance(settings, list) {
    return Math.round((settings.start + list.reduce((a, w) => a + wagerNet(w), 0)) * 100) / 100;
  }
  // Balance after each day with settled wagers, oldest first.
  function balanceHistory(settings, list) {
    const byDay = new Map();
    for (const w of list) if (w.result !== 'pending') byDay.set(w.date, (byDay.get(w.date) || 0) + wagerNet(w));
    let b = settings.start;
    return [...byDay.keys()].sort().map(date => {
      b += byDay.get(date);
      return { date, balance: Math.round(b * 100) / 100 };
    });
  }
  // Kelly: the share of the bankroll that grows it fastest if `chanceOf` is exactly right:
  // (chance x decimal - 1) / (decimal - 1). Chances are estimates, so we use a fraction of it
  // (quarter Kelly by default), half that again for low-confidence bets, and never more than
  // the cap. No edge -> $0.
  function kellyStake(chanceOf, decimal, bankroll, settings, level) {
    const full = decimal > 1 ? (chanceOf * decimal - 1) / (decimal - 1) : 0;
    if (!(full > 0) || !(bankroll > 0)) return { stake: 0, full: Math.max(full, 0), share: 0, capped: false };
    const raw = full * settings.kelly * (level === 'low' ? 0.5 : 1);
    const share = Math.min(raw, settings.cap_pct / 100);
    const amount = bankroll * share;
    const stake = amount >= 10 ? Math.round(amount) : Math.round(amount * 100) / 100;
    return { stake, full, share, capped: raw > share };
  }
  // Warnings for new wagers (they never block): above the maximum stake, at the daily loss
  // limit, or too much waiting on pending bets. `existing` excludes a wager being edited.
  function bankrollWarnings(fresh, existing, settings, today) {
    const out = [];
    const r2 = x => Math.round(x * 100) / 100;
    const bal = balance(settings, existing);
    if (settings.max_stake > 0) {
      const big = fresh.filter(w => w.wagered > settings.max_stake);
      if (big.length) out.push(`${big.length > 1 ? `${big.length} of these wagers are` : 'This wager is'} above your $${settings.max_stake} maximum stake.`);
    }
    if (settings.daily_loss > 0 && fresh.some(w => w.date === today)) {
      const day = existing.concat(fresh).filter(w => w.date === today && w.result !== 'pending')
        .reduce((a, w) => a + wagerNet(w), 0);
      if (-day >= settings.daily_loss) out.push(`You're down $${r2(-day)} today, at or past your $${settings.daily_loss} daily loss limit.`);
    }
    if (settings.exposure_pct > 0 && settings.start > 0 && fresh.some(w => w.result === 'pending')) {
      const pending = existing.concat(fresh).filter(w => w.result === 'pending').reduce((a, w) => a + w.wagered, 0);
      const limit = Math.max(bal, 0) * settings.exposure_pct / 100;
      if (pending > limit) out.push(`You'd have $${r2(pending)} waiting on pending bets, more than ${settings.exposure_pct}% of your $${bal} bankroll.`);
    }
    return out;
  }

  const api = { MARKET_WEIGHT, chance, agrees, confidence, CONF_RANK, betLabel, americanOdds, lineGroup, bestLines, betGroup, pickSides,
                pickProfit, summarize, winFromOdds, parlayDecimal, parlayResult, wagerNet, wagerSummary,
                balance, balanceHistory, kellyStake, bankrollWarnings };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Calc = api;
})(this);
