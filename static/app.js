// Every request to the bot carries this marker. Other websites can't add it to requests
// they make, so the bot refuses commands that don't come from this page.
const _fetch = window.fetch.bind(window);
window.fetch = (url, opts = {}) => _fetch(url, { ...opts, headers: { ...(opts.headers || {}), 'X-Odd-Finder': '1' } });

const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pct = (x, d = 1) => (x * 100).toFixed(d) + '%';
const signed = (x, d = 1) => (x > 0 ? '+' : '') + (x * 100).toFixed(d) + '%';
const when = iso => new Date(iso).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
const ago = t => { const m = Math.round((Date.now() - t) / 60000); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };

const ICONS = { 'American Football': '🏈', 'Basketball': '🏀', 'Baseball': '⚾', 'Ice Hockey': '🏒', 'Soccer': '⚽',
  'Tennis': '🎾', 'Mixed Martial Arts': '🥊', 'Boxing': '🥊', 'Golf': '⛳', 'Cricket': '🏏', 'Rugby League': '🏉',
  'Rugby Union': '🏉', 'Aussie Rules': '🏉', 'Lacrosse': '🥍', 'Handball': '🤾', 'Politics': '🗳️' };
const TAB_NAMES = { 'American Football': 'Football', 'Ice Hockey': 'Hockey', 'Mixed Martial Arts': 'MMA' };
const TAB_ORDER = ['American Football', 'Basketball', 'Ice Hockey', 'Baseball', 'Soccer', 'Mixed Martial Arts', 'Boxing', 'Tennis', 'Golf'];
const POPULAR = ['americanfootball_nfl', 'americanfootball_ncaaf', 'basketball_nba', 'basketball_ncaab', 'icehockey_nhl', 'baseball_mlb'];
const CREDITS_PER_LEAGUE = 3;

const state = {
  cfg: null, book: null, sports: [], groupOf: {}, tab: 'all',
  picked: new Set(),          // leagues ticked for scanning
  results: {},                // sport_key -> { rows, at }
};

async function getJSON(url) {
  const r = await fetch(url);
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

/* ---------------- start screen ---------------- */
function showStart() {
  $('#app').hidden = true;
  $('#start').hidden = false;
  const live = state.cfg.books.map(b =>
    `<button class="ticket live" data-book="${b.key}"><b>${esc(b.name)}</b><small>Ready to scan</small></button>`);
  const soon = state.cfg.coming_soon.map(n =>
    `<button class="ticket" disabled><b>${esc(n)}</b><small>Coming soon</small></button>`);
  $('#bookList').innerHTML = live.concat(soon).join('');
  $('#startNote').textContent = state.cfg.demo ? 'You’re in demo mode, so odds are made up until you add an API key.' : '';
  document.querySelectorAll('.ticket.live').forEach(b => b.onclick = () => pickBook(b.dataset.book));
}

async function pickBook(key) {
  state.book = state.cfg.books.find(b => b.key === key);
  localStorage.setItem('oddfinder.book', key);
  await loadSavedScans();
  // Leagues you've scanned stay visible after a restart: tick any that have saved results,
  // unless you unticked them yourself (then a note says they're hidden).
  const unticked = new Set(JSON.parse(localStorage.getItem('oddfinder.unticked') || '[]'));
  Object.keys(state.results).forEach(k => { if (!unticked.has(k)) state.picked.add(k); });
  $('#start').hidden = true;
  $('#app').hidden = false;
  $('#bookName').textContent = state.book.name;
  $('#demoBar').hidden = !state.cfg.demo;
  // Old servers don't send `outdated`; a page newer than its server is the same problem.
  $('#restartBar').hidden = !(state.cfg.outdated || !('outdated' in state.cfg));
  renderAll();
}

const storeKey = () => `oddfinder.results.${state.cfg.demo ? 'demo.' : ''}${state.book.key}`;

// Your last scans are saved by the bot (data/scans.json), so they come back after a
// restart with no size limit. Scans an older version saved in the browser are handed
// to the bot (each league keeps its newest copy), then removed from the browser.
async function loadSavedScans() {
  const fromBrowser = k => { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } };
  const oldResults = fromBrowser(storeKey()), oldMore = fromBrowser(moreKey());
  let saved = null;
  try {
    if (oldResults || oldMore) {
      saved = await (await fetch('/api/saved-scans', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'import', book: state.book.key, results: oldResults || {}, more: oldMore || [] }) })).json();
      if (saved && saved.results) { localStorage.removeItem(storeKey()); localStorage.removeItem(moreKey()); }
      else saved = null;
    }
    if (!saved) saved = await getJSON(`/api/saved-scans?book=${encodeURIComponent(state.book.key)}`);
  } catch (e) { saved = null; /* older bot: use the browser copy as before */ }
  state.results = saved ? saved.results : (oldResults || {});
  more.rows = saved ? saved.more : (oldMore || []);
  saveMore();                                     // drop games that have started
}

/* ---------------- tabs + leagues ---------------- */
function groups() {
  const gs = [...new Set(state.sports.map(s => s.group))];
  return gs.sort((a, b) => (TAB_ORDER.indexOf(a) + 1 || 99) - (TAB_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
}
const leaguesInTab = () => state.sports.filter(s => state.tab === 'all' || s.group === state.tab);
// A league's bets are shown when it's in the open tab and its league button is ticked.
const shown = (k, g = state.tab) => (g === 'all' || state.groupOf[k] === g) && state.picked.has(k);
const rowsInTab = () => Object.entries(state.results)
  .filter(([k]) => shown(k))
  .flatMap(([, v]) => v.rows);

function renderTabs() {
  const count = g => Object.entries(state.results).filter(([k]) => shown(k, g))
    .reduce((n, [, v]) => n + v.rows.filter(r => r.ev > 0).length, 0);
  const tab = (g, label, ico) => {
    const c = count(g);
    return `<button role="tab" data-g="${esc(g)}" aria-selected="${state.tab === g}">
      <span class="ico" aria-hidden="true">${ico}</span>${esc(label)}${c ? `<span class="count" title="Bets with positive value">${c}</span>` : ''}</button>`;
  };
  $('#tabs').innerHTML = tab('all', 'All sports', '🏟️') + groups().map(g => tab(g, TAB_NAMES[g] || g, ICONS[g] || '🏟️')).join('');
  document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { state.tab = b.dataset.g; renderAll(); });
}

function renderLeagues() {
  $('#leagues').innerHTML = leaguesInTab().map(s => `
    <label class="chip"><input type="checkbox" value="${s.key}" ${state.picked.has(s.key) ? 'checked' : ''}><span>${esc(s.title)}</span></label>`).join('');
  document.querySelectorAll('#leagues input').forEach(i => i.onchange = () => {
    i.checked ? state.picked.add(i.value) : state.picked.delete(i.value);
    saveLeagues();
    renderScanButton();
    renderTabs();
    renderResults();
  });
  renderScanButton();
}

function scanTargets() { return leaguesInTab().map(s => s.key).filter(k => state.picked.has(k)); }

function renderScanButton() {
  const n = scanTargets().length;
  const cost = state.cfg.demo ? '' : `<small>about ${n * CREDITS_PER_LEAGUE}${$('#props').checked ? '+' : ''} credits</small>`;
  $('#scan').innerHTML = n ? `Scan ${n} league${n > 1 ? 's' : ''}${cost}` : 'Pick a league';
  $('#scan').disabled = !n;
}

/* ---------------- scanning ---------------- */
async function scan() {
  const sports = scanTargets();
  if (!sports.length) return;
  $('#scan').disabled = true;
  setStatus(`Checking ${state.book.name}'s odds against the market…`);
  try {
    const p = new URLSearchParams({ book: state.book.key, sports: sports.join(','),
      props: $('#props').checked ? 1 : 0, min_books: 1 });
    const res = await getJSON('/api/scan?' + p);
    const at = Date.now();
    STATS.known = {};            // fresh odds: ask for fresh stats too
    clearTimeout(STATS.timer); STATS.timer = null;
    sports.forEach(k => state.results[k] = { rows: [], at });
    res.rows.forEach(r => (state.results[r.sport_key] ||= { rows: [], at }).rows.push(r));
    showCredits(res.quota);
    renderAll(true);
    if (res.errors.length) setStatus(`Some leagues couldn't be loaded: ${res.errors.join('; ')}`, true);
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    renderScanButton();
  }
}

function setStatus(msg, err) { $('#status').textContent = msg; $('#status').className = 'status' + (err ? ' err' : ''); }
function showCredits(q) {
  document.querySelectorAll('.credit-line').forEach(el => {
    if (state.cfg && state.cfg.demo) {
      el.textContent = 'Demo mode uses no credits';
    } else if (q && q.remaining != null) {
      const left = Math.floor(+q.remaining);
      el.innerHTML = `<b>${left.toLocaleString()}</b> API credits left`;
      el.classList.toggle('low', left < 50);
    } else if (!el.innerHTML) {
      el.textContent = 'Credits left will show after a scan';
    }
  });
}

/* ---------------- results ---------------- */
function filtered() {
  const m = $('#fMarket').value, minP = +$('#fProb').value / 100, minEv = +$('#fEv').value / 100;
  const minBooks = +$('#minBooks').value || 1, key = $('#sort').value;
  return rowsInTab()
    .filter(r => (!m || r.market_label === m) && chance(r) >= minP && r.ev >= minEv && r.books_used.length >= minBooks
      && (!$('#fAgree').checked || agrees(r))
      && (!$('#fConf').value || CONF_RANK[confidence(r).level] >= CONF_RANK[$('#fConf').value]))
    .sort((a, b) => key === 'start' ? a.start.localeCompare(b.start)
      : key === 'chance' ? chance(b) - chance(a) : b[key] - a[key]);
}

function renderResults(fresh) {
  const all = rowsInTab();
  const markets = [...new Set(all.map(r => r.market_label))].sort();
  const cur = $('#fMarket').value;
  $('#fMarket').innerHTML = '<option value="">All bet types</option>' + markets.map(m => `<option>${esc(m)}</option>`).join('');
  $('#fMarket').value = markets.includes(cur) ? cur : '';

  const scannedHere = Object.keys(state.results).filter(k => shown(k));
  const list = filtered();
  $('#filters').hidden = !all.length;

  if (!scanTargets().length) {
    $('#empty').innerHTML = `<h3>No leagues selected</h3><p>Tap a league above to show its bets.</p>`;
  } else if (!scannedHere.length) {
    const tabName = state.tab === 'all' ? 'any sport' : (TAB_NAMES[state.tab] || state.tab);
    $('#empty').innerHTML = `<div class="big" aria-hidden="true">${state.tab === 'all' ? '🏟️' : ICONS[state.tab] || '🏟️'}</div>
      <h3>No odds loaded for ${esc(tabName)} yet</h3><p>Pick the leagues above and press Scan to load ${esc(state.book.name)}'s lines.</p>`;
  } else if (!list.length) {
    $('#empty').innerHTML = all.length
      ? `<h3>No bets match these filters</h3><p>Use Show all above, or lower the minimum chance, value or books needed.</p>`
      : `<h3>No upcoming games found</h3><p>${esc(state.book.name)} has no lines for these leagues right now, or not enough other books offer them to compare.</p>`;
  } else {
    $('#empty').innerHTML = '';
  }
  $('#empty').hidden = !$('#empty').innerHTML;

  const latest = Math.max(0, ...scannedHere.map(k => state.results[k].at));
  if (latest && !$('#status').classList.contains('err')) {
    const pos = all.filter(r => r.ev > 0).length;
    setStatus(`${all.length} bets checked, ${pos} with positive value. Updated ${ago(latest)}.`);
  }
  const hidden = Object.keys(state.results).filter(k => (state.tab === 'all' || state.groupOf[k] === state.tab)
    && !state.picked.has(k) && state.results[k].rows.length).length;
  if (hidden && !$('#status').classList.contains('err')) {
    $('#status').textContent += `${$('#status').textContent ? ' ' : ''}${hidden} scanned league${hidden > 1 ? 's are' : ' is'} hidden. Tick ${hidden > 1 ? 'them' : 'it'} above to show ${hidden > 1 ? 'their' : 'its'} bets.`;
  }
  $('#shown').textContent = list.length ? `Showing ${Math.min(list.length, 300)} of ${list.length}` : '';
  // Say when filters hide bets (e.g. player props offered by only 1-2 other books), with a way to show them.
  const hiddenByFilters = all.length - list.length;
  const note = $('#filterNote');
  note.hidden = !hiddenByFilters;
  if (hiddenByFilters) {
    const books = +$('#minBooks').value || 1;
    const fewBooks = all.filter(r => r.books_used.length < books).length;
    note.innerHTML = `${hiddenByFilters} bet${hiddenByFilters > 1 ? 's are' : ' is'} hidden by your filters` +
      (fewBooks ? `, including ${fewBooks} offered by fewer than ${books} other sportsbooks (often player props)` : '') +
      `. <button class="linkbtn" id="showAll">Show all</button>`;
    $('#showAll').onclick = () => {
      $('#fMarket').value = ''; $('#fProb').value = 0; $('#fEv').value = -100; $('#minBooks').value = 1;
      $('#fConf').value = ''; $('#fAgree').checked = false;
      renderResults();
    };
  }

  const top = list[0];
  state.shown = list.slice(0, 300);
  state.topPick = top;
  $('#toppick').hidden = !top || top.ev <= 0 || $('#sort').value !== 'ev';
  if (top && !$('#toppick').hidden) {
    $('#toppick').innerHTML = `
      <div>
        <div class="label">Best value right now</div>
        <div class="who">${avatarFor(top)}<h3>${esc(betLabel(top))}</h3></div>
        <div class="event">${esc(top.event)}, ${esc(top.sport)}, ${when(top.start)}</div>
        ${agrees(top) ? '<em class="agree">Market + stats agree</em>' : ''}${formLine(top)}
        <div class="facts">
          <div><b>${top.american}</b>${esc(state.book.name)} odds</div>
          <div><b>${pct(chance(top))}</b>chance of success</div>
          <div><b>${confidence(top).label}</b>confidence</div>
          <div><b>${pct(top.fair_prob)}</b>market win chance</div>
          <div><b>${statOf(top) && statOf(top).prob != null ? pct(statOf(top).prob) : '–'}</b>stats win chance</div>
          <div><b>${pct(top.book_prob)}</b>chance the price implies</div>
          <div><b>${esc(top.market_label)}</b>bet type</div>
        </div>
      </div>
      <div class="plate"><b>${signed(top.ev)}</b><small>value</small></div>`;
    $('#toppick').classList.toggle('fresh', !!fresh);
  }

  $('#list').innerHTML = list.slice(0, 300).map((r, i) => betRow(r, i, 'main')).join('');
  loadImages();
  loadStats();
  scheduleTrackLog();
}

// One bet in a list. `which` says which list it belongs to, so a click opens the right bet.
function betRow(r, i, which) {
  return `
    <div class="bet" data-list="${which}" data-i="${i}" role="button" tabindex="0" aria-label="Show details for ${esc(betLabel(r))}">
      <div class="rank">${i + 1}${starButton(r)}</div>
      <div class="what">${avatarFor(r)}<div><b>${esc(betLabel(r))}</b><span>${esc(r.event)}</span>${formLine(r)}${r.others && r.others.length ? `<span class="more-lines">+${r.others.length} other line${r.others.length > 1 ? 's' : ''} for this bet</span>` : ''}${agrees(r) ? '<em class="agree">Market + stats agree</em>' : ''}</div></div>
      <div class="type">${esc(r.market_label)}<span class="when">${esc(r.sport)}, ${when(r.start)}</span></div>
      <div class="odds">${r.american}<small>${r.decimal.toFixed(2)}</small></div>
      <div class="probs" title="Compared with ${esc(r.books_used.join(', '))}">
        <div class="chance"><b>${pct(chance(r), 0)}</b> chance of success</div>
        ${confBadge(r)}<br>
        Market <b>${pct(r.fair_prob)}</b>
        <div class="meter"><i style="width:${(r.fair_prob * 100).toFixed(1)}%"></i><em style="left:${(r.book_prob * 100).toFixed(1)}%"></em></div>
        ${statLine(r)}<br>
        Price implies ${pct(r.book_prob)}${r.sharp ? ' <span class="tag-sharp">incl. sharp books</span>' : ''}
      </div>
      <div class="ev ${r.ev > 0 ? 'pos' : 'neg'}">${signed(r.ev)}<small>value</small></div>
    </div>`;
}

const betLabel = Calc.betLabel;

/* ---------------- stats second opinion ---------------- */
const STATS = { known: {}, timer: null, busy: false };
const statKey = r => `${r.event_id}|${r.market}|${r.selection}`;
const statOf = r => STATS.known[statKey(r)];
const nick = n => (n || '').split(' ').slice(-1)[0];
// Calculations live in calc.js; these pass in each bet's stats opinion.
const agrees = r => Calc.agrees(r, statOf(r));
const chance = r => Calc.chance(r, statOf(r));
const confidence = r => Calc.confidence(r, statOf(r));
const CONF_RANK = Calc.CONF_RANK;

function confBadge(r) {
  const c = confidence(r);
  return `<span class="conf ${c.level}" title="${esc(c.label)} confidence: ${esc(c.why)}">${c.label} confidence</span>`;
}

function statLine(r) {
  const s = statOf(r);
  if (!s) return '<span class="muted">Stats loading…</span>';
  if (s.prob == null) return `<span class="muted" title="${esc(s.reason || '')}">Stats not available</span>`;
  return `Stats <b class="${s.prob > r.book_prob ? 'up' : 'down'}">${pct(s.prob)}</b>`;
}

function formLine(r) {
  const s = statOf(r);
  if (!s || !s.form_home || !s.form_away) return '';
  return `<span class="form">Last 10: ${esc(nick(r.away_team))} ${esc(s.form_away)}, ${esc(nick(r.home_team))} ${esc(s.form_home)}</span>`;
}

// Ask the server for the stats opinion on every bet we have. Leagues whose past
// results are still downloading come back as "pending", so ask again shortly.
async function loadStats(fromTimer) {
  if (STATS.busy || (STATS.timer && !fromTimer)) return;
  STATS.timer = null;
  const rows = Object.values(state.results).flatMap(v => v.rows).concat(more.rows)
    .filter(r => !(statKey(r) in STATS.known));
  if (!rows.length) return;
  STATS.busy = true;
  let changed = false;
  try {
    const items = rows.map(r => ({ key: statKey(r), sport_key: r.sport_key, market: r.market, outcome: r.outcome,
      point: r.point, home: r.home_team, away: r.away_team, team: r.team_total_for, player: r.player, fair_prob: r.fair_prob }));
    const res = await (await fetch('/api/stats', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }) })).json();
    changed = Object.keys(res.stats || {}).length > 0;
    Object.assign(STATS.known, res.stats || {});
    if (res.pending) STATS.timer = setTimeout(() => loadStats(true), 5000);
  } catch (e) { /* stats are optional; the market numbers still show */ }
  STATS.busy = false;
  if (changed) { renderTabs(); renderResults(); if (!$('#moreView').hidden) renderMore(); }
}

async function loadAccuracy() {
  const box = $('#accBody');
  box.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const leagues = await getJSON('/api/accuracy');
    box.innerHTML = (state.cfg.demo ? '<p class="muted">Demo mode: the stats shown on bets are made up, but this report comes from real past games.</p>' : '') +
      '<div class="acc-grid">' + leagues.map(lg => {
        if (lg.props) {
          const pr = lg.props;
          if (pr.status !== 'ready') return `<div class="acc"><h4>${esc(lg.league)}</h4><p class="muted">${pr.status === 'error' ? esc(pr.error || '') : 'Downloading box scores…'}</p></div>`;
          const rows = Object.entries(pr.stats).map(([name, r]) => `<tr><td>${esc(name)}</td><td class="n">${r.bets}</td>
            <td class="n">${r.vs_hit_rate != null ? signed(r.vs_hit_rate, 1) : '–'}</td><td class="n">${r.usable ? 'Used' : 'Not used'}</td></tr>`).join('');
          return `<div class="acc"><h4>${esc(lg.league)}</h4>
            <p>Tested on this season's games, each predicted before it was played, at lines like the sportsbooks' (the player's recent average). Compared with simply using the player's hit rate over that line in his last 10 games:</p>
            <table><tr><th>Stat</th><th class="n">Bets</th><th class="n">Better by</th><th class="n"></th></tr>${rows}</table>
            <p class="${Object.values(pr.stats).some(r => r.usable) ? 'ok' : 'warn'}">Only stats that beat both a coin flip and the hit rate are used.</p></div>`;
        }
        const r = lg.report;
        if (!r) {
          const msg = lg.status === 'error' ? `Couldn't load past results: ${esc(lg.error || '')}` : 'Downloading past results…';
          return `<div class="acc"><h4>${esc(lg.league)}</h4><p class="muted">${msg}</p></div>`;
        }
        const used = Object.entries(r.usable || {}).filter(([, ok]) => ok).map(([k]) => k);
        const names = { moneyline: 'moneylines', spread: 'spreads', total: 'totals' };
        const rows = (r.buckets || []).map(b =>
          `<tr><td>${esc(b.range)}</td><td class="n">${b.games}</td><td class="n">${pct(b.actual)}</td></tr>`).join('');
        return `<div class="acc"><h4>${esc(lg.league)}</h4>
          ${r.kind === 'soccer'
            ? (r.picked_winner != null
              ? `<p>Tested on <b>${r.games}</b> matches from this season, each predicted before it was played. Picked the right result (win, draw or loss) <b>${pct(r.picked_winner, 0)}</b> of the time. Compared with simple base rates (how often home teams win, draw, lose), results were <b>${signed(r.skill, 1)}</b> better predicted and over/under 2.5 goals <b>${signed(r.total_skill, 1)}</b>.</p>`
              : `<p>Only <b>${r.games}</b> matches this season to test on, too few to judge, so this league stays on the market opinion.</p>`)
            : `<p>Tested on <b>${r.games}</b> games from this season, each predicted before it was played. Picked the winner <b>${pct(r.picked_winner || 0, 0)}</b> of the time.</p>`}
          ${rows ? `<table><tr><th>When stats said</th><th class="n">Games</th><th class="n">Teams won</th></tr>${rows}</table>` : ''}
          ${r.spread_coverage != null ? `<p>The real margin landed inside the stats' likely range ${pct(r.spread_coverage, 0)} of the time, and the real total ${pct(r.total_coverage, 0)} of the time (aim: 80%).</p>` : ''}
          <p class="${used.length ? 'ok' : 'warn'}">${used.length ? 'Used for ' + used.map(u => names[u]).join(', ') : 'Not used yet: not enough reliable results this season'}</p>
        </div>`;
      }).join('') + '</div>';
    const loading = lg => lg.props ? !['ready', 'error'].includes(lg.props.status) : (!lg.report && lg.status !== 'error');
    if (leagues.some(loading)) setTimeout(() => { if ($('#accBox').open) loadAccuracy(); }, 5000);
  } catch (e) {
    box.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

/* ---------------- logos and player photos ---------------- */
const IMG = { known: {}, timer: null };

function initials(name) {
  const words = name.replace(/[^A-Za-z0-9 .'-]/g, '').split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[words.length - 1][0]).toUpperCase();
}

function avatar(kind, name, group) {
  const key = `${kind}|${name}`, url = IMG.known[key];
  return `<span class="av ${kind}" data-key="${esc(key)}" data-group="${esc(group || '')}" title="${esc(name)}">` +
    (url ? `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">` : '') +
    `<b aria-hidden="true">${esc(initials(name))}</b></span>`;
}

function avatarFor(r) {
  const group = state.groupOf[r.sport_key] || '';
  const fighters = group === 'Mixed Martial Arts' || group === 'Boxing';
  if (r.player) return avatar('player', r.player, group);
  if (r.pick_team) return avatar(fighters ? 'player' : 'team', r.pick_team, group);
  if (r.home_team && r.away_team) {
    const k = fighters ? 'player' : 'team';
    return `<span class="pair">${avatar(k, r.away_team, group)}${avatar(k, r.home_team, group)}</span>`;
  }
  return '';
}

function applyImages() {
  document.querySelectorAll('.av[data-key]').forEach(s => {
    const url = IMG.known[s.dataset.key];
    if (url && !s.querySelector('img')) {
      s.insertAdjacentHTML('afterbegin', `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">`);
    }
  });
}

// Ask the server for logos/photos of everything on screen. New names are looked up
// in the background (about one every 2 seconds), so keep asking until they're done.
async function loadImages() {
  clearTimeout(IMG.timer);
  const items = new Map();
  document.querySelectorAll('.av[data-key]').forEach(s => {
    const key = s.dataset.key;
    if (key in IMG.known || items.has(key)) return;
    const [kind, ...rest] = key.split('|');
    items.set(key, { kind, name: rest.join('|'), group: s.dataset.group });
  });
  if (!items.size) return;
  try {
    const r = await fetch('/api/images', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: [...items.values()] }) });
    const res = await r.json();
    Object.assign(IMG.known, res.images || {});
    applyImages();
    if (res.pending) IMG.timer = setTimeout(loadImages, 5000);
  } catch (e) { /* pictures are optional; initials stay */ }
}

function renderAll(fresh) {
  if (!$('#status').classList.contains('err')) setStatus('');
  renderTabs();
  renderLeagues();
  renderResults(fresh);
}

/* ---------------- boot ---------------- */
async function init() {
  state.cfg = await getJSON('/api/config');
  await loadPicks();
  showCredits(state.cfg.quota);
  try {
    state.sports = await getJSON('/api/sports');
    getJSON('/api/config').then(c => showCredits(c.quota)).catch(() => {});  // sports call reports credits
  } catch (e) {
    state.sports = [];
    setStatus(e.message, true);
  }
  state.sports.forEach(s => state.groupOf[s.key] = s.group);
  const remembered = loadLeagues();
  state.sports.forEach(s => {
    if (remembered ? remembered.includes(s.key) : (state.cfg.demo || POPULAR.includes(s.key))) state.picked.add(s.key);
  });
  const saved = localStorage.getItem('oddfinder.book');
  if (saved && state.cfg.books.some(b => b.key === saved)) pickBook(saved); else showStart();
}

$('#scan').onclick = () => { setStatus(''); scan(); };
$('#props').onchange = renderScanButton;
$('#changeBook').onclick = showStart;
$('#home').onclick = e => { e.preventDefault(); showPage('find'); state.tab = 'all'; renderAll(); };
['#fMarket', '#fProb', '#fEv', '#sort', '#minBooks'].forEach(s => $(s).addEventListener('input', () => renderResults()));
$('#fAgree').addEventListener('change', () => renderResults());
$('#fConf').addEventListener('change', () => renderResults());
$('#accBox').addEventListener('toggle', () => { if ($('#accBox').open) loadAccuracy(); });

/* ---------------- bet details pop-up ---------------- */
const americanOdds = Calc.americanOdds;
const money = x => '$' + x.toFixed(2).replace(/\.00$/, '');
const unitFor = k => /hockey|soccer/.test(k) ? 'goals' : /baseball/.test(k) ? 'runs' : 'points';
const daysAway = iso => (new Date(iso) - Date.now()) / 86400000;

// Turn a bet's numbers into a plain-English verdict, reasons and risks.
function explain(r) {
  const book = state.book.name, s = statOf(r), hasStats = !!(s && s.prob != null);
  const c = confidence(r), ch = chance(r);
  const good = [], risk = [];

  // 1. Price vs. chance: where the value comes from
  const gap = (r.fair_prob - r.book_prob) * 100;
  if (r.ev > 0) {
    good.push(`${book}'s odds of ${r.american} are priced as if this has a ${pct(r.book_prob)} chance. The rest of the market puts it at ${pct(r.fair_prob)}, ${gap.toFixed(1)} points higher. That gap is the value: about ${money(r.ev * 100)} profit per $100 bet, on average over many bets like this.`);
  } else {
    risk.push(`${book}'s odds of ${r.american} are priced as if this has a ${pct(r.book_prob)} chance, but the market only gives it ${pct(r.fair_prob)}. Over many bets like this you'd expect to lose about ${money(-r.ev * 100)} per $100.`);
  }

  // 2. How the price compares with other sportsbooks
  const cmp = r.compare || [];
  if (cmp.length) {
    const beaten = cmp.filter(o => o.price < r.decimal).length;
    const better = cmp.filter(o => o.price > r.decimal);
    if (!better.length) good.push(`${book} has the best price of all ${cmp.length + 1} sportsbooks compared.`);
    else if (beaten >= cmp.length / 2) good.push(`${book} pays more than ${beaten} of the ${cmp.length} other sportsbooks on this bet.`);
    else risk.push(`${better.length} of ${cmp.length} other sportsbooks pay more for this same bet (best: ${better[0].book} at ${better[0].american}).`);
    const pin = cmp.find(o => o.book === 'Pinnacle');
    if (pin && pin.price < r.decimal) good.push(`Pinnacle, the sportsbook known for the sharpest prices, only offers ${pin.american}, less than ${book}'s ${r.american}.`);
  }

  // 3. The stats second opinion
  if (hasStats) {
    if (s.prob > r.book_prob) good.push(`Past results agree: the stats give it a ${pct(s.prob)} chance, also above the ${pct(r.book_prob)} the price implies.`);
    else risk.push(`Past results disagree: the stats give it only ${pct(s.prob)}, below the ${pct(r.book_prob)} the price implies.`);
    if (s.expected_stat != null && r.point != null) {
      const leans = (r.outcome === 'Over') === (s.expected_stat > r.point);
      (leans ? good : risk).push(`From ${r.player}'s recent games, minutes and the opponent's defense, the stats expect about ${s.expected_stat} ${s.stat_name}, against a line of ${r.point}.`);
    }
    if (s.expected_total != null && r.point != null) {
      const unit = unitFor(r.sport_key);
      const leans = (r.outcome === 'Over') === (s.expected_total > r.point);
      (leans ? good : risk).push(`The stats expect about ${s.expected_total} total ${unit}, against a line of ${r.point}.`);
    }
    if (s.form_home && s.form_away && r.pick_team) {
      const mine = r.pick_team === r.home_team ? s.form_home : s.form_away;
      const theirs = r.pick_team === r.home_team ? s.form_away : s.form_home;
      const opp = r.pick_team === r.home_team ? r.away_team : r.home_team;
      const wins = x => +x.split('-')[0];
      const line = `${r.pick_team} are ${mine} in their last 10 games; ${opp} are ${theirs}.`;
      if (wins(mine) >= 6 && wins(mine) > wins(theirs)) good.push(line);
      else if (wins(mine) <= 4 && wins(mine) < wins(theirs)) risk.push(line);
    }
  } else {
    risk.push(`There's no stats second opinion for this bet${s && s.reason ? ` (${s.reason.toLowerCase()})` : ''}, so it rests on the market alone.`);
  }

  // 4. Confidence
  if (c.level === 'high') good.push(`High confidence: ${c.why}.`);
  if (c.level === 'low') risk.push(`Low confidence: ${c.why}.`);

  // 5. Things that are always true
  risk.push(`Even at a ${pct(ch, 0)} chance of success, this bet loses about ${100 - Math.round(ch * 100)} times in 100. Value only pays off over many bets.`);
  if (r.player) risk.push('Player props are offered by fewer sportsbooks and can swing on late lineup or injury news.');
  if (r.estimated) risk.push('Some sportsbooks only offer the "Yes" side of this bet and no other players to measure their margin from, so the bot guessed it (about 10%). Treat the chance as rough.');
  else if (r.margin_measured) good.push('Sportsbooks only offer "Yes" here, but their margin was measured from the Yes/No lines they offer other players in this game, so the chance is more reliable than a guess.');
  const d = daysAway(r.start);
  if (d > 3) risk.push(`The game is ${Math.round(d)} days away, so injuries and lineup news could still move the line.`);
  const at = r.scanned_at || (state.results[r.sport_key] && state.results[r.sport_key].at);
  risk.push(`Odds move. These were checked ${at ? ago(at) : 'earlier'}, so confirm the price in ${book} before you bet.`);

  let verdict;
  if (r.ev > 0.03 && c.level !== 'low' && (!hasStats || s.prob > r.book_prob)) verdict = ['good', 'Strong value', `${book} is paying noticeably more than this bet's chances are worth.`];
  else if (r.ev > 0 && c.level === 'low') verdict = ['ok', 'Possible value, rough estimate', `${book} looks like it's paying more than this bet's chances are worth, but the estimate is rough.`];
  else if (r.ev > 0) verdict = ['good', 'Value pick', `${book} is paying a little more than this bet's chances are worth.`];
  else if (r.ev > -0.02) verdict = ['ok', 'About a fair price', `${book}'s odds are close to what the bet's chances are worth, so there's no real edge.`];
  else verdict = ['bad', 'Not a value bet', `${book} is paying less than this bet's chances are worth.`];
  return { verdict, good, risk };
}

function betDetails(r) {
  const book = state.book.name, s = statOf(r), hasStats = !!(s && s.prob != null);
  const c = confidence(r), ex = explain(r);
  const [tone, title, line] = ex.verdict;

  // price comparison: this book, every other book, and the fair (no-margin) price
  const cmp = r.compare || [];
  const fairDec = 1 / r.fair_prob;
  const rowsCmp = [{ book, price: r.decimal, american: r.american, cls: 'fd', note: ' (this bet)' },
    ...cmp.map(o => ({ ...o, cls: '', note: o.sharp ? ' <small>sharp</small>' : '' })),
    { book: 'Fair price', price: fairDec, american: americanOdds(fairDec), cls: 'fair', note: ' <small>no margin</small>' }]
    .sort((a, b) => b.price - a.price);
  const maxP = Math.max(...rowsCmp.map(o => o.price)), minP = Math.min(...rowsCmp.map(o => o.price));
  const width = p => maxP === minP ? 100 : 15 + 85 * (p - minP) / (maxP - minP);

  const form = s && s.form_home ? `<p>Last 10 games: ${esc(r.away_team)} ${esc(s.form_away)}, ${esc(r.home_team)} ${esc(s.form_home)}.</p>` : '';
  return `
    <button class="close" aria-label="Close">×</button>
    <div class="bm-head">${avatarFor(r)}<div>
      <h2 id="bmTitle">${esc(betLabel(r))}</h2>
      <div class="sub">${esc(r.event)}, ${esc(r.sport)}, ${when(r.start)}. ${esc(r.market_label)}.</div>
    </div></div>
    <div class="bm-save">
      ${savedPick(r)
        ? `<button class="btn-save on" id="bmSave">★ Saved to My picks</button><span class="muted">Click to remove it.</span>`
        : `<button class="btn-save" id="bmSave">☆ Save pick</button>
           <label>Stake (optional) $<input id="bmStake" type="number" min="0" step="1" placeholder="0"></label>`}
    </div>
    <div class="bm-verdict ${tone}"><b>${title}.</b>${esc(line)}</div>
    <div class="bm-nums">
      <div class="hl"><b>${pct(chance(r), 0)}</b>chance of success</div>
      <div><b>${c.label}</b>confidence</div>
      <div><b>${signed(r.ev)}</b>value</div>
      <div><b>${r.american}</b>${esc(book)} odds</div>
      <div><b>${money((r.decimal - 1) * 100)}</b>profit on a $100 bet</div>
    </div>

    <h3>${ex.verdict[0] === 'good' ? 'Why it could be a good pick' : 'What it has going for it'}</h3>
    ${ex.good.length ? `<ul class="good">${ex.good.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : '<p>Nothing stands out in its favor.</p>'}

    <h3>Things to watch out for</h3>
    <ul class="risk">${ex.risk.map(t => `<li>${esc(t)}</li>`).join('')}</ul>

    <h3>How ${esc(book)}'s price compares</h3>
    ${cmp.length ? `<p>Higher odds pay more. The fair price is what the odds would be with every book's margin removed.</p>
      <div class="cmp">${rowsCmp.map(o => `
        <span class="${o.cls}">${esc(o.book)}${o.note}</span><span class="o ${o.cls}">${o.american}</span>
        <span class="bar ${o.cls}"><i style="width:${width(o.price).toFixed(0)}%"></i></span>`).join('')}</div>`
      : '<p>Scan again to see each sportsbook\'s price for this bet.</p>'}

    ${r.others && r.others.length ? `
      <h3>Other lines for this bet</h3>
      <p>The same bet at other numbers. ${r.others.every(o => o.ev <= r.ev)
        ? 'This one is listed because it has the best value.'
        : 'You\'re viewing one of the other lines; the list shows the one with the best value.'} Click a line to see its details.</p>
      <div class="lines">${[r, ...r.others].map((o, i) => `
        <button class="line${i === 0 ? ' this' : ''}" data-line="${i}">
          <span>${esc(betLabel(o))}${i === 0 ? ' <small>(this one)</small>' : ''}</span>
          <span class="o">${o.american}</span>
          <span>${pct(chance(o), 0)}<small> chance</small></span>
          <span class="${o.ev > 0 ? 'up' : 'down'}">${signed(o.ev)}<small> value</small></span>
        </button>`).join('')}</div>` : ''}

    <h3>The numbers behind it</h3>
    <ul>
      <li>Market win chance: <b>${pct(r.fair_prob)}</b>, from ${r.books_used.length} other sportsbooks with their margins removed.</li>
      <li>Stats win chance: <b>${hasStats ? pct(s.prob) : 'not available'}</b>${hasStats ? ', from this season\'s and last season\'s results' : ''}.</li>
      <li>Chance of success: <b>${pct(chance(r))}</b>${hasStats ? ' (75% market, 25% stats)' : ' (market only)'}.</li>
      <li>Chance ${esc(book)}'s price implies: <b>${pct(r.book_prob)}</b>, margin included.</li>
      <li>Confidence: <b>${c.label}</b>, because of: ${esc(c.why)}.</li>
    </ul>
    ${form}`;
}

function openBet(r) {
  if (!r) return;
  $('#bmBody').innerHTML = betDetails(r);
  $('#betModal').showModal();
  $('#bmBody .close').onclick = () => $('#betModal').close();
  $('#bmSave').onclick = async () => {
    const stake = $('#bmStake') ? +$('#bmStake').value || null : null;
    await toggleSave(r, stake);
    const top = $('#betModal').scrollTop;
    openBet(r);                                   // redraw with the new saved state
    $('#betModal').scrollTop = top;
  };
  const lines = r.others ? [r, ...r.others] : [];
  document.querySelectorAll('#bmBody .line').forEach(b => b.onclick = () => {
    const pick = lines[+b.dataset.line];
    if (pick !== r) openBet(Object.assign({}, pick, { others: lines.filter(x => x !== pick) }));
  });
  $('#betModal').scrollTop = 0;
  loadImages();
}

const betFromRow = el => (el.dataset.list === 'more' ? more.shown : state.shown)[+el.dataset.i];
['#list', '#mList'].forEach(id => {
  $(id).addEventListener('click', e => {
    const b = e.target.closest('.bet');
    if (!b) return;
    if (e.target.closest('.star')) { toggleSave(betFromRow(b)); return; }   // the star saves, it doesn't open
    openBet(betFromRow(b));
  });
  $(id).addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('bet')) { e.preventDefault(); openBet(betFromRow(e.target)); }
  });
});

/* ---------------- More bets ---------------- */
const more = { rows: [], shown: [], games: [], groups: [], loadedLeague: null };
const moreKey = () => `oddfinder.more.${state.cfg.demo ? 'demo.' : ''}${state.book.key}`;
const MORE_MAX_GAMES = 10;

function showView(view) {
  document.querySelectorAll('.viewswitch button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.view === view)));
  $('#mainView').hidden = view !== 'main';
  $('#moreView').hidden = view !== 'more';
  if (view === 'more') enterMore();
}

function enterMore() {
  if (!$('#mLeague').options.length) {
    const byGroup = {};
    state.sports.forEach(s => (byGroup[s.group] = byGroup[s.group] || []).push(s));
    $('#mLeague').innerHTML = groups().map(g => `<optgroup label="${esc(TAB_NAMES[g] || g)}">` +
      byGroup[g].map(s => `<option value="${s.key}">${esc(s.title)}</option>`).join('') + '</optgroup>').join('');
    const first = POPULAR.find(k => state.sports.some(s => s.key === k));
    if (first) $('#mLeague').value = first;
  }
  if (more.loadedLeague !== $('#mLeague').value) loadMoreLeague();
  renderMore();
}

// Upcoming games and available bet types for a league. Free: no credits used.
async function loadMoreLeague() {
  const sport = $('#mLeague').value;
  if (!sport) return;
  more.loadedLeague = sport;
  $('#mGames').innerHTML = '<span class="muted">Loading games…</span>';
  $('#mTypes').innerHTML = '';
  try {
    const res = await getJSON('/api/games?sport=' + encodeURIComponent(sport));
    if (more.loadedLeague !== sport) return;          // user switched league meanwhile
    more.games = res.games;
    more.groups = res.groups;
    showCredits(res.quota);
    $('#mGames').innerHTML = res.games.length ? res.games.map(g => `
      <label><input type="checkbox" value="${esc(g.id)}"><span>${esc(g.away)} @ ${esc(g.home)}<small>${when(g.start)}</small></span></label>`).join('')
      : '<span class="muted">No upcoming games in this league right now.</span>';
    $('#mTypes').innerHTML = res.groups.length ? res.groups.map(g => `
      <label><input type="checkbox" value="${esc(g.key)}" ${g.key === 'alt' || g.key === 'team' ? 'checked' : ''}>
        <span>${esc(g.label)}<small>${esc(g.hint)}. <span class="cost">${g.markets.length} bet type${g.markets.length > 1 ? 's' : ''}</span></small></span></label>`).join('')
      : '<span class="muted">No extra bet types for this league.</span>';
    document.querySelectorAll('#mGames input, #mTypes input').forEach(i => i.onchange = renderMoreScan);
  } catch (e) {
    $('#mGames').innerHTML = `<span class="status err">${esc(e.message)}</span>`;
  }
  renderMoreScan();
}

function moreSelection() {
  const games = [...document.querySelectorAll('#mGames input:checked')].map(i => i.value);
  const picked = [...document.querySelectorAll('#mTypes input:checked')].map(i => i.value);
  const markets = more.groups.filter(g => picked.includes(g.key)).flatMap(g => g.markets);
  return { games, markets };
}

function renderMoreScan() {
  const { games, markets } = moreSelection();
  const btn = $('#mScan');
  const tooMany = games.length > MORE_MAX_GAMES;
  btn.disabled = !games.length || !markets.length || tooMany;
  const cost = state.cfg.demo ? '' : `<small>up to ${games.length * markets.length} credits</small>`;
  btn.innerHTML = !games.length ? 'Pick games' : !markets.length ? 'Pick bet types'
    : tooMany ? `Pick ${MORE_MAX_GAMES} games or fewer`
    : `Scan ${games.length} game${games.length > 1 ? 's' : ''}${cost}`;
}

async function moreScan() {
  const { games, markets } = moreSelection();
  if (!games.length || !markets.length) return;
  $('#mScan').disabled = true;
  setMoreStatus(`Loading ${state.book.name}'s odds for ${games.length} game${games.length > 1 ? 's' : ''}…`);
  try {
    const p = new URLSearchParams({ book: state.book.key, sport: $('#mLeague').value,
      events: games.join(','), markets: markets.join(',') });
    const res = await getJSON('/api/more-scan?' + p);
    const at = Date.now();
    // Replace earlier results for these games; keep the rest.
    more.rows = more.rows.filter(r => !games.includes(r.event_id))
      .concat(res.rows.map(r => ({ ...r, scanned_at: at })));
    res.rows.forEach(r => delete STATS.known[statKey(r)]);   // fresh odds: fresh stats
    saveMore();
    showCredits(res.quota);
    setMoreStatus(`${res.count} bets loaded for ${games.length} game${games.length > 1 ? 's' : ''}.` +
      (res.errors.length ? ` Some games couldn't be loaded: ${res.errors.join('; ')}` : ''), !!res.errors.length && !res.count);
    renderMore();
  } catch (e) {
    setMoreStatus(e.message, true);
  } finally {
    renderMoreScan();
  }
}

function saveMore() {
  // The bot saves More bets scans itself; here we just drop games that have started.
  more.rows = more.rows.filter(r => new Date(r.start) > Date.now());
}

function setMoreStatus(msg, err) { $('#mStatus').textContent = msg; $('#mStatus').className = 'status' + (err ? ' err' : ''); }

function fillSelect(sel, items, allLabel) {   // items: [[value, label]]
  const cur = sel.value;
  sel.innerHTML = `<option value="">${allLabel}</option>` + items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  sel.value = items.some(([v]) => v === cur) ? cur : '';
}

function renderMore() {
  const rows = more.rows;
  $('#mFilters').hidden = !rows.length;
  const uniq = (arr) => [...new Map(arr.map(x => [x[0], x])).values()];
  fillSelect($('#mfLeague'), uniq(rows.map(r => [r.sport_key, r.sport])).sort((a, b) => a[1].localeCompare(b[1])), 'All leagues');
  const lg = $('#mfLeague').value;
  const inLeague = rows.filter(r => !lg || r.sport_key === lg);
  fillSelect($('#mfGame'), uniq(inLeague.map(r => [r.event_id, `${r.event}, ${when(r.start)}`])), 'All games');
  const game = $('#mfGame').value;
  const inGame = inLeague.filter(r => !game || r.event_id === game);
  fillSelect($('#mfType'), uniq(inGame.map(r => [r.market_label, r.market_label])).sort((a, b) => a[1].localeCompare(b[1])), 'All bet types');
  const names = [...new Set(inGame.flatMap(r => [r.player, r.team_total_for, r.home_team, r.away_team]).filter(Boolean))].sort();
  $('#mWhoList').innerHTML = names.map(n => `<option value="${esc(n)}">`).join('');

  const type = $('#mfType').value, who = $('#mfWho').value.trim().toLowerCase();
  const conf = $('#mfConf').value, key = $('#mfSort').value;
  let list = inGame
    .filter(r => (!type || r.market_label === type)
      && (!who || [r.selection, r.player, r.team_total_for, r.pick_team].some(x => x && x.toLowerCase().includes(who)))
      && (!conf || CONF_RANK[confidence(r).level] >= CONF_RANK[conf]));
  // Alternate lines: show each bet's best-value line, with the others listed in its pop-up.
  if ($('#mfBest').checked) list = Calc.bestLines(list);
  list.sort((a, b) => key === 'start' ? a.start.localeCompare(b.start)
      : key === 'chance' ? chance(b) - chance(a) : b.ev - a.ev);

  more.shown = list.slice(0, 300);
  $('#mShown').textContent = list.length ? `Showing ${more.shown.length} of ${list.length}` : '';
  $('#mList').innerHTML = more.shown.map((r, i) => betRow(r, i, 'more')).join('');
  $('#mEmpty').innerHTML = !rows.length
    ? '<h3>No extra bets loaded yet</h3><p>Pick a league, the games and the bet types above, then press Scan.</p>'
    : !list.length ? '<h3>No bets match these filters</h3><p>Try another player, game or bet type.</p>' : '';
  $('#mEmpty').hidden = !$('#mEmpty').innerHTML;
  loadImages();
  loadStats();
  scheduleTrackLog();
}

document.querySelectorAll('.viewswitch button').forEach(b => b.onclick = () => showView(b.dataset.view));
$('#mLeague').addEventListener('change', loadMoreLeague);
$('#mAllGames').onclick = () => {
  const boxes = [...document.querySelectorAll('#mGames input')];
  const allOn = boxes.length && boxes.slice(0, MORE_MAX_GAMES).every(i => i.checked);
  boxes.forEach((i, n) => i.checked = !allOn && n < MORE_MAX_GAMES);
  renderMoreScan();
};
$('#mScan').onclick = moreScan;
$('#mClear').onclick = async () => {
  more.rows = [];
  try {
    await fetch('/api/saved-scans', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'clear_more', book: state.book.key }) });
  } catch (e) { /* cleared on screen anyway */ }
  setMoreStatus(''); renderMore();
};
['#mfLeague', '#mfGame', '#mfType', '#mfConf', '#mfSort', '#mfBest'].forEach(s => $(s).addEventListener('change', renderMore));
$('#mfWho').addEventListener('input', renderMore);
$('#toppick').addEventListener('click', () => openBet(state.topPick));
$('#toppick').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openBet(state.topPick); } });
// click outside the pop-up closes it (Esc closes it too, built in)
$('#betModal').addEventListener('click', e => { if (e.target === $('#betModal')) $('#betModal').close(); });

/* ---------------- My picks ---------------- */
const PICKS = { list: [], checking: false, tab: 'open' };
const pickKey = r => `${r.event_id}|${r.market}|${r.selection}`;
const savedPick = r => r && PICKS.list.find(p => pickKey(p) === pickKey(r));
const dollars = x => (x < 0 ? '−$' : '$') +
  Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).replace(/\.00$/, '');
const signedDollars = x => (x > 0 ? '+' : '') + dollars(x);

function starButton(r) {
  const on = !!savedPick(r);
  return `<button class="star${on ? ' on' : ''}" aria-label="${on ? 'Remove from My picks' : 'Save to My picks'}"
    title="${on ? 'Saved. Click to remove' : 'Save pick'}">${on ? '★' : '☆'}</button>`;
}

function toast(msg) {
  let t = $('#toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
  t.textContent = msg;
  t.className = 'show';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.className = '', 2600);
}

async function picksApi(body) {
  const r = await fetch('/api/picks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  if (j.picks) PICKS.list = j.picks;
  const pending = PICKS.list.filter(p => p.status === 'pending').length;
  $('#sideCount').textContent = pending ? `${pending} open` : '';
  $('#menuCount').textContent = pending || '';
  return j;
}

async function loadPicks() { try { await picksApi({ action: 'list' }); } catch (e) { /* picks are optional */ } }

// Save a bet to My picks, or remove it if it's already saved.
async function toggleSave(r, stake) {
  if (!r) return;
  const existing = savedPick(r);
  try {
    if (existing) {
      await picksApi({ action: 'delete', id: existing.id });
      toast('Removed from My picks');
    } else {
      const s = statOf(r);
      const pick = {
        event_id: r.event_id, sport_key: r.sport_key, sport: r.sport, event: r.event, home_team: r.home_team,
        away_team: r.away_team, start: r.start, market: r.market, market_label: r.market_label, selection: r.selection,
        label: betLabel(r), outcome: r.outcome, team_total_for: r.team_total_for, player: r.player, american: r.american,
        book: state.book.name, confidence: confidence(r).label, point: r.point, decimal: r.decimal, chance: chance(r),
        fair_prob: r.fair_prob, stats_prob: s && s.prob != null ? s.prob : undefined, book_prob: r.book_prob, ev: r.ev,
      };
      await picksApi({ action: 'save', pick, stake });
      toast('Saved to My picks');
    }
  } catch (e) {
    toast(e.message);
  }
  refreshStars();
  if (!$('#picksView').hidden) renderPicks();
}

function refreshStars() {
  document.querySelectorAll('.bet[data-list]').forEach(el => {
    const r = betFromRow(el), star = el.querySelector('.star');
    if (r && star) star.outerHTML = starButton(r);
  });
}

async function checkResults() {
  if (PICKS.checking || !PICKS.list.some(p => p.status === 'pending')) return;
  PICKS.checking = true;
  $('#pCheck').disabled = true;
  $('#pCheck').textContent = 'Checking…';
  try {
    const j = await picksApi({ action: 'grade' });
    $('#pChecked').textContent = j.graded ? `${j.graded} result${j.graded > 1 ? 's' : ''} updated just now` : 'Checked just now: no new results';
  } catch (e) {
    $('#pChecked').textContent = e.message;
  }
  PICKS.checking = false;
  $('#pCheck').disabled = false;
  $('#pCheck').textContent = 'Check results';
  renderPicks();
}

function pickStatus(p) {
  const started = new Date(p.start) <= Date.now();
  if (p.status !== 'pending') {
    const word = { won: 'Won', lost: 'Lost', push: 'Push' }[p.status];
    return `<span class="badge ${p.status}">${word}</span>${p.final ? `<span class="final">${esc(p.final)}</span>` : ''}
      <span class="muted small">${p.graded_by === 'auto' ? 'Checked automatically' : 'Marked by you'}</span>
      <button class="linkbtn" data-act="pending" data-id="${p.id}">Undo</button>`;
  }
  if (!started) return `<span class="badge pending">Upcoming</span><span class="muted small">Starts ${when(p.start)}</span>`;
  if (p.auto) return `<span class="badge pending">Waiting for result</span><span class="muted small">Checked automatically after the game</span>`;
  return `<span class="badge pending">Mark the result</span>
    <span class="marks">
      <button data-act="won" data-id="${p.id}">Won</button>
      <button data-act="lost" data-id="${p.id}">Lost</button>
      <button data-act="push" data-id="${p.id}">Push</button>
    </span>`;
}

function renderPicks() {
  const all = PICKS.list;
  const s = Calc.summarize(all);
  const tone = x => x > 0 ? 'up' : x < 0 ? 'down' : '';
  $('#pSummary').innerHTML = all.length ? `
    <div><b>${s.total}</b>picks saved${s.pending ? `, ${s.pending} waiting` : ''}</div>
    <div><b>${s.wins}-${s.losses}${s.pushes ? `-${s.pushes}` : ''}</b>record (won-lost${s.pushes ? '-push' : ''})</div>
    <div><b>${s.winRate != null ? pct(s.winRate, 0) : '–'}</b>${s.expectedRate != null ? `won, bot expected ${pct(s.expectedRate, 0)}` : 'win rate, once picks finish'}</div>
    <div><b class="${tone(s.flatProfit)}">${s.flatPicks ? signedDollars(s.flatProfit) : '–'}</b>if you'd bet $100 on each</div>
    <div><b class="${tone(s.realProfit)}">${s.stakedPicks ? signedDollars(s.realProfit) : '–'}</b>${s.stakedPicks ? `your profit on ${dollars(s.realRisked)} staked` : 'your profit: add a stake to track it'}</div>` : '';

  const levels = Object.entries(s.byConf).filter(([, v]) => v.picks);
  $('#pByConf').innerHTML = levels.length ? `
    <h3>By confidence</h3>
    <p class="muted">If the bot is well calibrated, each level wins about as often as it expected. A few dozen finished picks are needed before this means much.</p>
    <table><tr><th>Confidence</th><th class="n">Finished</th><th class="n">Won</th><th class="n">Bot expected</th></tr>
    ${levels.map(([lvl, v]) => `<tr><td>${lvl}</td><td class="n">${v.picks}</td><td class="n">${pct(v.winRate, 0)}</td><td class="n">${pct(v.expectedRate, 0)}</td></tr>`).join('')}
    </table>` : '';

  const open = all.filter(p => p.status === 'pending');
  const done = all.filter(p => p.status !== 'pending');
  $('#pOpenN').textContent = open.length || '';
  $('#pDoneN').textContent = done.length || '';
  document.querySelectorAll('#pTabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === PICKS.tab)));
  const list = PICKS.tab === 'open'
    ? open.sort((a, b) => a.start.localeCompare(b.start))          // soonest first
    : done.sort((a, b) => b.start.localeCompare(a.start));         // most recent first
  $('#pList').innerHTML = list.map(p => {
    const profit = p.status !== 'pending' && p.stake ? Calc.pickProfit(p, p.stake) : null;
    return `
    <div class="pick ${p.status}">
      <div class="pmain">
        <b>${esc(p.label || p.selection)}</b>
        <span>${esc(p.event)}, ${esc(p.sport)}, ${when(p.start)}. ${esc(p.market_label)}.</span>
        <span class="muted small">Saved at ${esc(p.american)} on ${esc(p.book || 'FanDuel')}: ${pct(p.chance || p.fair_prob || 0, 0)} chance of success, ${esc(p.confidence || '–')} confidence, ${signed(p.ev || 0)} value</span>
      </div>
      <div class="pres">${pickStatus(p)}</div>
      <div class="pact">
        <label>Stake $<input type="number" min="0" step="1" value="${p.stake || ''}" placeholder="0" data-stake="${p.id}"></label>
        ${profit != null ? `<span class="${tone(profit)}">${signedDollars(profit)}</span>` : ''}
        <button class="linkbtn remove" data-act="delete" data-id="${p.id}" aria-label="Remove this pick">Remove</button>
      </div>
    </div>`;
  }).join('');
  $('#pEmpty').innerHTML = !all.length
    ? '<div class="big" aria-hidden="true">☆</div><h3>No saved picks yet</h3><p>Click the ☆ next to any bet, or Save pick in its details, to keep track of it here.</p>'
    : list.length ? ''
    : PICKS.tab === 'open' ? '<h3>No unsettled picks</h3><p>Every pick you saved has a result. Find more bets to save.</p>'
    : '<h3>No settled picks yet</h3><p>Picks move here once their game is over and the result is in.</p>';
  $('#pEmpty').hidden = !$('#pEmpty').innerHTML;
}

$('#pList').addEventListener('click', async e => {
  const b = e.target.closest('button[data-act]');
  if (!b) return;
  const { act, id } = b.dataset;
  try {
    if (act === 'delete') {
      if (!confirm('Remove this pick from My picks? Its result will be deleted too.')) return;
      await picksApi({ action: 'delete', id });
    } else {
      await picksApi({ action: 'update', id, status: act });
    }
  } catch (err) { toast(err.message); }
  renderPicks();
  refreshStars();
});
$('#pList').addEventListener('change', async e => {
  const id = e.target.dataset.stake;
  if (!id) return;
  try { await picksApi({ action: 'update', id, stake: +e.target.value || null }); } catch (err) { toast(err.message); }
  renderPicks();
});
$('#pCheck').onclick = () => checkResults();
document.querySelectorAll('#pTabs button').forEach(b => b.onclick = () => { PICKS.tab = b.dataset.tab; renderPicks(); });

/* ---------------- Bot track record: log every gradable bet ---------------- */
// Moneylines, spreads, totals and team totals in these leagues can be checked automatically
// after the game, so every one the bot shows is logged (once per bet; rescans update it).
const TRACK_LEAGUES = ['americanfootball_nfl', 'basketball_nba', 'icehockey_nhl', 'baseball_mlb',
  'soccer_epl', 'soccer_spain_la_liga', 'soccer_italy_serie_a', 'soccer_germany_bundesliga', 'soccer_france_ligue_one'];
const TRACK_MARKETS = ['h2h', 'spreads', 'totals', 'team_totals'];
const TRACK = { sent: {}, timer: null };

// Player props are checked against the player's real stats after the game.
const TRACK_PROPS = {
  basketball_nba: ['player_points', 'player_rebounds', 'player_assists', 'player_threes'],
  icehockey_nhl: ['player_points', 'player_shots_on_goal'],
  americanfootball_nfl: ['player_pass_yds', 'player_rush_yds', 'player_reception_yds', 'player_receptions'],
};

function trackable(r) {
  if (new Date(r.start) <= Date.now()) return false;
  if (r.player) return (TRACK_PROPS[r.sport_key] || []).includes(r.market)
    && (r.outcome === 'Over' || r.outcome === 'Under') && r.point != null;
  return TRACK_LEAGUES.includes(r.sport_key) && TRACK_MARKETS.includes(r.market.replace(/^alternate_/, ''));
}

function scheduleTrackLog() {
  clearTimeout(TRACK.timer);
  TRACK.timer = setTimeout(logTrack, 2000);   // wait for stats to settle, then log in one go
}

async function logTrack() {
  const rows = Object.values(state.results).flatMap(v => v.rows).concat(more.rows).filter(trackable);
  const bets = [];
  for (const r of rows) {
    const s = statOf(r), c = chance(r), sig = `${r.decimal}|${c.toFixed(4)}|${s ? 1 : 0}`;
    if (TRACK.sent[statKey(r)] === sig) continue;          // already logged as it is now
    TRACK.sent[statKey(r)] = sig;
    bets.push({
      event_id: r.event_id, sport_key: r.sport_key, sport: r.sport, event: r.event, home_team: r.home_team,
      away_team: r.away_team, start: r.start, market: r.market, market_label: r.market_label, selection: r.selection,
      label: betLabel(r), outcome: r.outcome, team_total_for: r.team_total_for, player: r.player, american: r.american, point: r.point,
      decimal: r.decimal, chance: c, fair_prob: r.fair_prob, stats_prob: s && s.prob != null ? s.prob : undefined,
      book_prob: r.book_prob, ev: r.ev, confidence: confidence(r).label, agree: agrees(r), book: state.book.name,
    });
  }
  if (!bets.length) return;
  try {
    await fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'log', bets }) });
  } catch (e) { bets.forEach(b => delete TRACK.sent[`${b.event_id}|${b.market}|${b.selection}`]); }   // retry next time
}

/* ---------------- Sidebar menu and pages ---------------- */
const PAGES = { find: '#findPage', picks: '#picksView', wagers: '#wagersView', bot: '#botView' };

function openMenu() {
  $('#sidebar').hidden = false;
  $('#scrim').hidden = false;
  $('#menuBtn').setAttribute('aria-expanded', 'true');
  requestAnimationFrame(() => document.body.classList.add('menu-open'));
  $('#sidebar .side-item[aria-current="page"]').focus();
  showBackup();
}

// Backup status at the bottom of the menu.
async function showBackup() {
  try {
    const cfg = await getJSON('/api/config');
    const b = cfg.backup || {}, g = cfg.github_backup || {};
    const line = (x, icon) => !x.enabled ? null
      : x.error ? `<span class="warn">${esc(x.error)}</span>`
      : x.last ? `${icon} Backed up to ${x.where} ${ago(new Date(x.last).getTime())}`
      : `${icon} Backing up to ${x.where} automatically`;
    const lines = b.demo ? ['Backups are off in demo mode.']
      : [line(b, '☁') || '<span class="warn">iCloud Drive backup is off: iCloud Drive wasn\'t found.</span>', line(g, '⎇')].filter(Boolean);
    $('#sideBackup').innerHTML = lines.map(l => `<span>${l}</span>`).join('');
  } catch (e) { /* status is optional */ }
}
function closeMenu() {
  document.body.classList.remove('menu-open');
  $('#menuBtn').setAttribute('aria-expanded', 'false');
  $('#scrim').hidden = true;
  setTimeout(() => { if (!document.body.classList.contains('menu-open')) $('#sidebar').hidden = true; }, 200);
}

function showPage(page) {
  Object.entries(PAGES).forEach(([k, sel]) => $(sel).hidden = k !== page);
  document.querySelectorAll('.side-item').forEach(b =>
    b.dataset.page === page ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current'));
  closeMenu();
  window.scrollTo(0, 0);
  if (page === 'picks') { renderPicks(); checkResults(); }
  if (page === 'bot') loadBot(true);
  if (page === 'wagers') loadWagers();
}

$('#menuBtn').onclick = openMenu;
$('#sideClose').onclick = closeMenu;
$('#scrim').onclick = closeMenu;
$('#sideBook').onclick = () => { closeMenu(); showStart(); };
document.querySelectorAll('.side-item').forEach(b => b.onclick = () => showPage(b.dataset.page));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sidebar').hidden) closeMenu(); });

/* ---------------- Bot results page ---------------- */
const BOT = { busy: false };

function botSince() {
  const v = $('#bRange').value;
  if (!v) return null;
  if (v === 'season') {                              // most recent August 1st
    const now = new Date();
    const year = now.getMonth() >= 7 ? now.getFullYear() : now.getFullYear() - 1;
    return new Date(Date.UTC(year, 7, 1)).toISOString();
  }
  return new Date(Date.now() - +v * 86400000).toISOString();
}

async function loadBot(checkFirst) {
  if (BOT.busy) return;
  BOT.busy = true;
  if (checkFirst) { $('#bCheck').disabled = true; $('#bCheck').textContent = 'Checking…'; }
  try {
    const r = await fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: checkFirst ? 'grade' : 'summary', since: botSince() }) });
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || r.statusText);
    if (checkFirst) $('#bChecked').textContent = j.graded ? `${j.graded} result${j.graded > 1 ? 's' : ''} updated just now` : 'Checked just now: no new results';
    renderBot(j.summary);
  } catch (e) {
    $('#bChecked').textContent = e.message;
  }
  BOT.busy = false;
  $('#bCheck').disabled = false;
  $('#bCheck').textContent = 'Check results';
}

function renderBot(s) {
  const tone = x => x > 0 ? 'up' : x < 0 ? 'down' : '';
  const rec = b => `${b.wins}-${b.losses}${b.pushes ? `-${b.pushes}` : ''}`;
  const rate = b => b.win_rate != null ? pct(b.win_rate, 0) : '–';
  const card = (big, cls, text) => `<div><b class="${cls || ''}">${big}</b>${text}</div>`;
  const a = s.all, v = s.value, g = s.agree;
  $('#bSummary').innerHTML = s.tracked ? [
    card(s.tracked, '', `bets tracked, ${s.pending} waiting for a result`),
    card(rate(a), '', a.expected != null ? `of finished bets won (${rec(a)}); the bot expected ${pct(a.expected, 0)}` : 'win rate, once games finish'),
    card(v.bets ? signedDollars(v.profit) : '–', tone(v.profit),
      v.bets ? `betting $100 on each good-value bet (${rec(v)}, ${signed(v.roi, 1)} return)` : 'on good-value bets, once games finish'),
    card(g.bets ? signedDollars(g.profit) : '–', tone(g.profit),
      g.bets ? `on "market + stats agree" bets (${rec(g)})` : 'on bets where market and stats agree'),
    card(a.bets ? signedDollars(a.profit) : '–', tone(a.profit),
      a.bets ? 'betting $100 on every bet, good value or not' : 'on every bet, for comparison'),
  ].join('') : '';

  const table = (title, note, rows, first) => rows.length ? `
    <section class="bcard"><h3>${title}</h3>${note ? `<p>${note}</p>` : ''}
      <table><tr><th>${first}</th><th class="n">Bets</th><th class="n">Won</th><th class="n">Expected</th><th class="n">$100 each</th></tr>
      ${rows.map(r => `<tr><td>${esc(r.name)}</td><td class="n">${r.bets}</td><td class="n">${rate(r)}</td>
        <td class="n">${r.expected != null ? pct(r.expected, 0) : '–'}</td><td class="n ${tone(r.profit)}">${signedDollars(r.profit)}</td></tr>`).join('')}
      </table></section>` : '';
  $('#bTables').innerHTML =
    table('When the bot said…', 'If its chances are right, each row wins about as often as expected. Give it a few hundred bets before reading much into it.', s.calibration, 'Chance of success') +
    table('By confidence', 'High confidence should be the most reliable.', s.by_confidence, 'Confidence') +
    table('By league', '', s.by_league, 'League') +
    table('By bet type', '', s.by_type, 'Bet type');

  $('#bRecentTitle').hidden = !s.recent.length;
  $('#bRecent').innerHTML = s.recent.map(b => `
    <div class="pick ${b.status}">
      <div class="pmain"><b>${esc(b.label || b.selection)}</b>
        <span>${esc(b.event)}, ${esc(b.sport)}, ${when(b.start)}. ${esc(b.market_label)}.</span>
        <span class="muted small">${esc(b.american)}: ${pct(b.chance || 0, 0)} chance of success, ${esc(b.confidence || '–')} confidence, ${signed(b.ev || 0)} value</span></div>
      <div class="pres"><span class="badge ${b.status}">${{ won: 'Won', lost: 'Lost', push: 'Push' }[b.status]}</span>${b.final ? `<span class="final">${esc(b.final)}</span>` : ''}</div>
    </div>`).join('');
  $('#bEmpty').innerHTML = s.tracked ? (s.settled ? '' : '<h3>No finished games yet</h3><p>Results appear here once the games the bot logged are over. Check back after they\'ve been played.</p>')
    : '<div class="big" aria-hidden="true">📈</div><h3>Nothing tracked for this period yet</h3><p>Scan the NFL, NBA, NHL or MLB and every moneyline, spread, total and team total the bot shows is logged here automatically.</p>';
  $('#bEmpty').hidden = !$('#bEmpty').innerHTML;
}

$('#bCheck').onclick = () => loadBot(true);
$('#bRange').addEventListener('change', () => loadBot(false));
$('#bClear').onclick = async () => {
  if (!confirm('Clear the bot\'s whole track record? Every logged bet and result will be deleted. Your saved picks are not affected.')) return;
  await fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'clear' }) });
  TRACK.sent = {};
  loadBot(false);
};

/* ---------------- Remembering ticked leagues ---------------- */
function saveLeagues() {
  try {
    localStorage.setItem('oddfinder.leagues', JSON.stringify([...state.picked]));
    // Leagues you unticked on purpose stay hidden even if they have saved results.
    const unticked = state.sports.map(s => s.key).filter(k => !state.picked.has(k));
    localStorage.setItem('oddfinder.unticked', JSON.stringify(unticked));
  } catch (e) { /* storage full: not important */ }
}
function loadLeagues() {
  try { return JSON.parse(localStorage.getItem('oddfinder.leagues')); } catch (e) { return null; }
}

/* ---------------- My wagers ---------------- */
const WAGERS = { list: [], editing: null };
const todayLocal = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const localDay = iso => { const d = new Date(iso); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const RESULT_WORD = { won: 'Won', lost: 'Lost', push: 'Push', pending: 'Pending' };

async function wagersApi(body) {
  const r = await fetch('/api/wagers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  WAGERS.list = j.wagers || [];
  return j;
}

async function loadWagers() {
  await loadPicks();                               // for "Choose from My picks"
  if (!$('#wRows').children.length) resetForm();
  else $('#wRows').querySelectorAll('.wrow').forEach(fillPickOptions);
  try { await wagersApi({ action: 'list' }); } catch (e) { setWagerStatus(e.message, true); }
  renderWagers();
}

function setWagerStatus(msg, err) { $('#wStatus').textContent = msg; $('#wStatus').className = 'status' + (err ? ' err' : ''); }

// ---- the form: one row per wager ("+ Add another wager" adds rows; editing uses one row)
function makeRow(date) {
  const row = document.createElement('div');
  row.className = 'wrow';
  row.innerHTML = `
    <span class="wnum" aria-hidden="true"></span>
    <label>Date<input type="date" class="f-date" required></label>
    <div class="wnote">
      <label>What you bet on <span class="muted">(optional)</span><select class="f-pick" aria-label="Choose a saved pick from My picks"></select></label>
      <input class="f-note" maxlength="120" placeholder="e.g. Lakers -3.5" autocomplete="off" aria-label="What you bet on">
      <div class="f-legs" hidden></div>
      <div class="f-parlay-tools">
        <button type="button" class="linkbtn f-parlay-on">Make this a parlay</button>
        <button type="button" class="linkbtn f-addleg" hidden>+ Add leg</button>
        <span class="f-odds muted small"></span>
        <button type="button" class="linkbtn f-parlay-off" hidden>Back to a single bet</button>
      </div>
    </div>
    <label>Amount wagered ($)<input class="f-wagered" type="number" min="0.01" step="0.01" inputmode="decimal" required></label>
    <label>Result
      <select class="f-result">
        <option value="pending" selected>Pending (set it later)</option>
        <option value="won">Won</option>
        <option value="lost">Lost</option>
        <option value="push">Push (stake returned)</option>
      </select>
    </label>
    <label class="f-wonwrap">Amount won ($)<input class="f-won" type="number" min="0.01" step="0.01" inputmode="decimal"><small class="f-hint">Your profit, not counting the stake</small></label>
    <button type="button" class="linkbtn remove f-remove" aria-label="Remove this wager from the form">Remove</button>`;
  row.querySelector('.f-date').value = date || todayLocal();
  row.dataset.pickId = '';
  row.dataset.autoWon = '';
  $('#wRows').appendChild(row);
  fillPickOptions(row);
  syncRow(row);
  return row;
}

const f = (row, cls) => row.querySelector('.' + cls);
const rowPick = row => row.dataset.pickId && PICKS.list.find(p => p.id === row.dataset.pickId);

function pickOptions(emptyText) {
  const open = PICKS.list.filter(p => p.status === 'pending').sort((a, b) => a.start.localeCompare(b.start));
  const done = PICKS.list.filter(p => p.status !== 'pending').sort((a, b) => b.start.localeCompare(a.start)).slice(0, 50);
  const opt = p => `<option value="${esc(p.id)}">${esc(p.label || p.selection)} (${esc(p.event)}, ${new Date(p.start).toLocaleDateString([], { month: 'short', day: 'numeric' })}) ${esc(p.american)}</option>`;
  return `<option value="">${PICKS.list.length ? emptyText : 'No saved picks yet: type your own'}</option>` +
    (open.length ? `<optgroup label="Unsettled picks">${open.map(opt).join('')}</optgroup>` : '') +
    (done.length ? `<optgroup label="Settled picks">${done.map(opt).join('')}</optgroup>` : '');
}

function fillPickOptions(row) {
  f(row, 'f-pick').innerHTML = pickOptions('Type your own below, or choose from My picks');
  f(row, 'f-pick').value = rowPick(row) ? row.dataset.pickId : '';
  row.querySelectorAll('.leg').forEach(leg => {
    leg.querySelector('.l-pick').innerHTML = pickOptions('Type this leg, or choose from My picks');
    leg.querySelector('.l-pick').value = legPick(leg) ? leg.dataset.pickId : '';
  });
}

// ---- parlays: one wager on several legs
const isParlay = row => row.dataset.parlay === '1';
const legPick = leg => leg.dataset.pickId && PICKS.list.find(p => p.id === leg.dataset.pickId);

function makeLeg(row, data = {}) {
  const leg = document.createElement('div');
  leg.className = 'leg';
  leg.innerHTML = `<span class="lnum"></span>
    <select class="l-pick" aria-label="Choose this leg from My picks"></select>
    <input class="l-note" maxlength="120" placeholder="e.g. Lakers -3.5" autocomplete="off" aria-label="Parlay leg">
    <button type="button" class="linkbtn remove l-remove" aria-label="Remove this leg">Remove</button>`;
  leg.dataset.pickId = data.pick_id || '';
  leg.querySelector('.l-note').value = data.note || '';
  f(row, 'f-legs').appendChild(leg);
  leg.querySelector('.l-pick').innerHTML = pickOptions('Type this leg, or choose from My picks');
  leg.querySelector('.l-pick').value = legPick(leg) ? leg.dataset.pickId : '';
  numberLegs(row);
  return leg;
}

function numberLegs(row) {
  const legs = [...row.querySelectorAll('.leg')];
  legs.forEach((leg, i) => {
    leg.querySelector('.lnum').textContent = `Leg ${i + 1}`;
    leg.querySelector('.l-remove').hidden = legs.length <= 2;           // a parlay has at least 2 legs
  });
}

function setParlay(row, on, legs) {
  row.dataset.parlay = on ? '1' : '';
  f(row, 'f-legs').hidden = !on;
  f(row, 'f-pick').hidden = on;
  f(row, 'f-addleg').hidden = !on;
  f(row, 'f-parlay-off').hidden = !on;
  f(row, 'f-parlay-on').hidden = on;
  f(row, 'f-odds').textContent = '';
  f(row, 'f-note').placeholder = on ? 'Parlay name (optional), e.g. Sunday 3-leg' : 'e.g. Lakers -3.5';
  f(row, 'f-legs').innerHTML = '';
  if (on) {
    const start = legs || (rowPick(row) ? [{ note: f(row, 'f-note').value, pick_id: row.dataset.pickId }, {}]
                                        : [{ note: f(row, 'f-note').value }, {}]);
    if (!legs) { f(row, 'f-note').value = ''; row.dataset.pickId = ''; f(row, 'f-pick').value = ''; }
    start.forEach(l => makeLeg(row, l));
  }
  row.dataset.autoResult = '';
  syncRow(row);
}

function rowLegs(row) {
  return [...row.querySelectorAll('.leg')].map(leg => {
    const p = legPick(leg);
    return Object.assign({ note: leg.querySelector('.l-note').value.trim() },
      p ? { pick_id: p.id, decimal: p.decimal, status: p.status } : {});
  });
}

function choosePick(row, id) {
  row.dataset.pickId = id || '';
  const p = rowPick(row);
  if (!p) return;
  f(row, 'f-note').value = p.label || p.selection;
  f(row, 'f-date').value = localDay(p.start);
  if (p.stake && !f(row, 'f-wagered').value) f(row, 'f-wagered').value = p.stake;
  if (p.status !== 'pending') f(row, 'f-result').value = p.status;        // already graded
  row.dataset.autoWon = '1';
  syncRow(row);
  setWagerStatus(`Filled in from My picks: ${p.label || p.selection}. Check the amount you wagered.`);
}

// Show "Amount won" only for wins; for a linked pick, work it out from the odds unless you typed your own.
function syncRow(row) {
  let decimal = null, oddsText = null, from = "the pick's odds";
  if (isParlay(row)) {
    const legs = rowLegs(row);
    decimal = Calc.parlayDecimal(legs);
    f(row, 'f-odds').textContent = decimal ? `Combined odds ${Calc.americanOdds(decimal)} (${decimal.toFixed(2)})`
      : 'Combined odds show when every leg is chosen from My picks';
    oddsText = decimal ? Calc.americanOdds(decimal) : null;
    from = 'the combined odds';
    // every leg from My picks and graded? then the parlay's result follows from them
    if (row.dataset.autoResult !== 'off') {
      const r = legs.length >= 2 && legs.every(l => l.status) ? Calc.parlayResult(legs.map(l => l.status)) : null;
      if (r) { f(row, 'f-result').value = r; row.dataset.autoResult = 'auto'; row.dataset.autoWon = '1'; }
      else if (row.dataset.autoResult === 'auto') { f(row, 'f-result').value = 'pending'; row.dataset.autoResult = ''; }
    }
  } else {
    const p = rowPick(row);
    if (p && p.decimal > 1) { decimal = p.decimal; oddsText = p.american; }
  }
  const won = f(row, 'f-result').value === 'won';
  f(row, 'f-wonwrap').hidden = !won;
  const wagered = +f(row, 'f-wagered').value, hint = f(row, 'f-hint');
  if (won && decimal > 1 && wagered > 0 && (row.dataset.autoWon || !f(row, 'f-won').value)) {
    f(row, 'f-won').value = Calc.winFromOdds(wagered, decimal);
    row.dataset.autoWon = '1';
    hint.textContent = `Worked out from ${from} (${oddsText}). Change it if your payout was different.`;
  } else if (!row.dataset.autoWon || !decimal) {
    hint.textContent = 'Your profit, not counting the stake';
  }
}

function readRow(row) {
  const w = { date: f(row, 'f-date').value, note: f(row, 'f-note').value.trim(), wagered: +f(row, 'f-wagered').value,
              result: f(row, 'f-result').value, won: +f(row, 'f-won').value || 0 };
  if (isParlay(row)) {
    w.legs = rowLegs(row).map(({ note, pick_id, decimal }) => Object.assign({ note }, pick_id ? { pick_id, decimal } : {}));
    return w;
  }
  const p = rowPick(row);
  if (p) Object.assign(w, { pick_id: p.id, decimal: p.decimal });
  return w;
}

function rowProblem(w) {
  if (w.legs) {
    if (w.legs.length < 2) return 'a parlay needs at least 2 legs';
    const blank = w.legs.findIndex(l => !l.note);
    if (blank >= 0) return `parlay leg ${blank + 1}: choose a pick or type what it is`;
  }
  if (!(w.wagered > 0)) return 'enter how much you wagered';
  if (w.result === 'won' && !(w.won > 0)) return 'enter how much you won (your profit, not counting the stake)';
  return null;
}

function refreshFormButtons() {
  const rows = [...$('#wRows').querySelectorAll('.wrow')];
  rows.forEach((r, i) => {
    f(r, 'wnum').textContent = rows.length > 1 ? i + 1 : '';
    f(r, 'f-remove').hidden = rows.length < 2;
  });
  const editing = !!WAGERS.editing;
  $('#wMore').hidden = editing;
  $('#wCancel').hidden = !editing;
  $('#wAdd').textContent = editing ? 'Save changes' : rows.length > 1 ? `Add ${rows.length} wagers` : 'Add wager';
  $('#wForm').classList.toggle('editing', editing);
}

function resetForm(date) {
  WAGERS.editing = null;
  $('#wRows').innerHTML = '';
  makeRow(date);
  refreshFormButtons();
}

function startEdit(w) {
  WAGERS.editing = w.id;
  $('#wRows').innerHTML = '';
  const row = makeRow(w.date);
  row.dataset.pickId = w.pick_id || '';
  fillPickOptions(row);
  f(row, 'f-note').value = w.note || '';
  if (w.legs) setParlay(row, true, w.legs);
  f(row, 'f-wagered').value = w.wagered;
  f(row, 'f-result').value = w.result;
  row.dataset.autoResult = 'off';                     // keep the saved result while editing
  f(row, 'f-won').value = w.result === 'won' ? w.won : '';
  syncRow(row);
  refreshFormButtons();
  setWagerStatus(`Editing "${w.note || 'Wager'}". Change anything, then Save changes.`);
  $('#wForm').scrollIntoView({ behavior: 'smooth', block: 'center' });
  (w.result === 'pending' ? f(row, 'f-result') : f(row, 'f-wagered')).focus();
  renderWagers();
}

// ---- the list and the stats
function wagerCutoff() {
  const v = $('#wRange').value, today = todayLocal();
  if (!v) return null;
  if (v === 'month') return today.slice(0, 8) + '01';
  const d = new Date(today + 'T00:00:00');
  d.setDate(d.getDate() - (+v - 1));
  return d.toISOString().slice(0, 10);
}

function renderWagers() {
  const cutoff = wagerCutoff();
  const list = WAGERS.list.filter(w => !cutoff || w.date >= cutoff);
  const s = Calc.wagerSummary(list);
  const tone = x => x > 0 ? 'up' : x < 0 ? 'down' : '';
  const card = (big, cls, text) => `<div><b class="${cls || ''}">${big}</b>${text}</div>`;
  const name = w => esc(w.note || 'Wager');
  $('#wSummary').innerHTML = list.length ? [
    s.count && card(signedDollars(s.net), tone(s.net), s.net >= 0 ? 'net profit' : 'net loss'),
    s.count && card(signedDollars(s.totalWon), s.totalWon ? 'up' : '', `total won on ${s.won} winning bet${s.won === 1 ? '' : 's'}`),
    s.count && card(s.totalLost ? signedDollars(-s.totalLost) : '$0', s.totalLost ? 'down' : '', `total lost on ${s.lost} losing bet${s.lost === 1 ? '' : 's'}`),
    s.count && card(s.winRate != null ? pct(s.winRate, 0) : '–', '', `win rate (${s.won}-${s.lost}${s.push ? `-${s.push}` : ''} won-lost${s.push ? '-push' : ''})`),
    s.count && card(dollars(s.wagered), '', `wagered on ${s.count} settled bet${s.count === 1 ? '' : 's'}`),
    s.count && card(s.roi != null ? signed(s.roi, 1) : '–', tone(s.net), 'return on money wagered'),
    s.count && card(dollars(s.avgBet), '', 'average bet'),
    s.biggestWin && card(signedDollars(s.biggestWin.won), 'up', `biggest win: ${name(s.biggestWin)}`),
    s.biggestLoss && card(signedDollars(-s.biggestLoss.wagered), 'down', `biggest loss: ${name(s.biggestLoss)}`),
    s.streak && card(`${s.streak.result === 'won' ? 'Won' : 'Lost'} ${s.streak.length}`, s.streak.result === 'won' ? 'up' : 'down',
      `current streak (latest ${s.streak.length === 1 ? 'bet' : `${s.streak.length} bets`} in a row)`),
    s.pending && card(dollars(s.atStake), '', `at stake on ${s.pending} pending bet${s.pending === 1 ? '' : 's'}`),
  ].filter(Boolean).join('') : '';
  $('#wList').innerHTML = list.map(w => {
    const n = Calc.wagerNet(w);
    const amount = w.result === 'pending' ? `<span class="muted">${dollars(w.wagered)} at stake</span>` : `<span class="${tone(n)}"><b>${signedDollars(n)}</b></span>`;
    return `
    <div class="pick ${w.result}${WAGERS.editing === w.id ? ' being-edited' : ''}">
      <div class="pmain"><b>${esc(w.note || 'Wager')}</b>${w.pick_id ? ' <span class="tag-pick">From My picks</span>' : ''}${w.legs ? ` <span class="tag-pick">${w.legs.length}-leg parlay${w.decimal ? ` ${Calc.americanOdds(w.decimal)}` : ''}</span>` : ''}
        ${w.legs ? `<ol class="legs">${w.legs.map(l => `<li>${esc(l.note)}</li>`).join('')}</ol>` : ''}
        <span>${new Date(w.date + 'T12:00:00').toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })}</span></div>
      <div class="pres"><span class="badge ${w.result}">${RESULT_WORD[w.result]}</span>
        <span class="muted small">Wagered ${dollars(w.wagered)}${w.result === 'won' ? `, won ${dollars(w.won)}` : ''}</span></div>
      <div class="pact">${amount}
        <button class="linkbtn" data-edit="${w.id}">${w.result === 'pending' ? 'Set result' : 'Edit'}</button>
        <button class="linkbtn remove" data-del="${w.id}" aria-label="Delete this wager">Delete</button></div>
    </div>`;
  }).join('');
  $('#wEmpty').innerHTML = !WAGERS.list.length
    ? '<div class="big" aria-hidden="true">💵</div><h3>No wagers logged yet</h3><p>Add the bets you placed with the form above. Use "+ Add another wager" to log several at once.</p>'
    : !list.length ? '<h3>No wagers in this period</h3><p>Choose a longer period above to see older ones.</p>' : '';
  $('#wEmpty').hidden = !$('#wEmpty').innerHTML;
}

// ---- events
$('#wRows').addEventListener('change', e => {
  const row = e.target.closest('.wrow');
  if (!row) return;
  if (e.target.classList.contains('f-pick')) return choosePick(row, e.target.value);
  if (e.target.classList.contains('l-pick')) {
    const leg = e.target.closest('.leg');
    leg.dataset.pickId = e.target.value || '';
    const p = legPick(leg);
    if (p) leg.querySelector('.l-note').value = p.label || p.selection;
    row.dataset.autoWon = '1';
    return syncRow(row);
  }
  if (e.target.classList.contains('f-result')) row.dataset.autoResult = 'off';     // you chose it: keep it
  syncRow(row);
});
$('#wRows').addEventListener('input', e => {
  const row = e.target.closest('.wrow');
  if (!row) return;
  if (e.target.classList.contains('f-won')) row.dataset.autoWon = '';
  syncRow(row);
});
$('#wRows').addEventListener('click', e => {
  const row = e.target.closest('.wrow');
  if (!row) return;
  const c = e.target.classList;
  if (c.contains('f-remove')) { row.remove(); refreshFormButtons(); }
  else if (c.contains('f-parlay-on')) { setParlay(row, true); row.querySelector('.leg .l-pick').focus(); }
  else if (c.contains('f-parlay-off')) setParlay(row, false);
  else if (c.contains('f-addleg')) { makeLeg(row).querySelector('.l-pick').focus(); syncRow(row); }
  else if (c.contains('l-remove')) { e.target.closest('.leg').remove(); numberLegs(row); syncRow(row); }
});
$('#wMore').addEventListener('click', () => {
  const rows = $('#wRows').querySelectorAll('.wrow');
  const last = rows[rows.length - 1];
  const row = makeRow(last ? f(last, 'f-date').value : todayLocal());      // same day as the one above
  refreshFormButtons();
  f(row, 'f-pick').focus();
});
$('#wRange').addEventListener('change', renderWagers);
$('#wCancel').addEventListener('click', () => { resetForm(); setWagerStatus(''); renderWagers(); });
$('#wForm').addEventListener('submit', async e => {
  e.preventDefault();
  const rows = [...$('#wRows').querySelectorAll('.wrow')];
  const wagers = rows.map(readRow);
  for (let i = 0; i < wagers.length; i++) {
    const problem = rowProblem(wagers[i]);
    if (problem) {
      rows[i].querySelector(problem.startsWith('enter how much you won') ? '.f-won' : '.f-wagered').focus();
      return setWagerStatus(wagers.length > 1 ? `Wager ${i + 1}: ${problem}.` : problem[0].toUpperCase() + problem.slice(1) + '.', true);
    }
  }
  const editing = WAGERS.editing;
  $('#wAdd').disabled = true;
  try {
    if (editing) await wagersApi({ action: 'update', id: editing, wager: wagers[0] });
    else await wagersApi({ action: 'add_many', wagers });
    const one = wagers[0];
    const what = one.result === 'won' ? `won ${dollars(one.won)}` : one.result === 'lost' ? `lost ${dollars(one.wagered)}`
      : one.result === 'push' ? 'push' : `pending, ${dollars(one.wagered)} at stake`;
    const keepDate = rows.length ? f(rows[rows.length - 1], 'f-date').value : todayLocal();
    resetForm(editing ? todayLocal() : keepDate);                        // a new batch keeps its date for the next one
    setWagerStatus(editing ? `Saved: ${what}.` : wagers.length > 1 ? `Added ${wagers.length} wagers.` : `Added: ${what}.`);
    renderWagers();
  } catch (err) { setWagerStatus(err.message, true); }
  $('#wAdd').disabled = false;
});
$('#wList').addEventListener('click', async e => {
  const ed = e.target.closest('button[data-edit]');
  if (ed) { const w = WAGERS.list.find(x => x.id === ed.dataset.edit); if (w) startEdit(w); return; }
  const b = e.target.closest('button[data-del]');
  if (!b || !confirm('Delete this wager? This can\'t be undone.')) return;
  try {
    await wagersApi({ action: 'delete', id: b.dataset.del });
    if (WAGERS.editing === b.dataset.del) resetForm();
    setWagerStatus('Wager deleted.');
  } catch (err) { setWagerStatus(err.message, true); }
  renderWagers();
});

// Start the app (kept last so everything above is defined first).
init().catch(e => { document.body.innerHTML = `<p style="padding:40px">Odd Finder couldn't start: ${esc(e.message)}</p>`; });
