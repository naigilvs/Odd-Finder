/*
 * Odd Finder chat: a chatbot (no AI) that answers from the bot's own numbers.
 * The server reads the question and looks up players, games, stats, free market odds,
 * injuries and form (/api/chat); this file builds the answer with the same calculations the
 * rest of the page uses (chance of success, confidence, value, suggested stake).
 * Uses helpers from app.js, which loads first.
 */
const CHAT = { last: null, lastBet: null, ctx: null, busy: false, started: false, lists: {}, nextList: 1 };
const CHAT_KEY = 'oddfinder.chat';
const CHAT_MAX_MESSAGES = 80;

const TERM_TEXT = {
  push: 'A <b>push</b> is a tie with the line, like a spread of −3 when the team wins by exactly 3. You get your stake back: no win, no loss.',
  vig: 'The <b>vig</b> (juice, margin) is the sportsbook\'s cut. At −110 on both sides you risk $110 to win $100, so the book keeps about 4.5% over many bets. The bot removes it to find the fair chance.',
  value: '<b>Value</b> (expected value) is how much a bet is worth over many tries: chance of winning × payout − 1. +3% means about +$3 per $100 in the long run. It isn\'t a promise for any single bet.',
  kelly: '<b>Kelly</b> sizes a bet by its edge: (chance × decimal odds − 1) ÷ (decimal odds − 1) of your bankroll. Full Kelly is aggressive, so the bot suggests a fraction (quarter Kelly by default) and caps it.',
  parlay: 'A <b>parlay</b> joins several bets into one: all legs must win. The odds multiply, so it pays a lot, but the chances multiply too and the sportsbook\'s cut stacks up on every leg.',
  spread: 'A <b>spread</b> evens out two teams. Knicks −4.5 must win by 5 or more; the other side at +4.5 wins if it loses by 4 or less, or wins.',
  moneyline: 'A <b>moneyline</b> is a bet on who wins the game, no points involved. −150 is the favorite (risk $150 to win $100); +130 is the underdog (risk $100 to win $130).',
  total: 'A <b>total</b> (over/under) is a bet on the combined score. Over 224.5 wins if both teams score 225 or more together.',
  clv: '<b>Closing line value</b> compares the price you got with the market\'s fair price right before the game. Beating the close regularly is the best early sign of real value. See it in Bot results.',
  units: 'A <b>unit</b> is a fixed slice of your bankroll, usually 1%. Results in units compare fairly however much you bet. Set yours in My wagers → Bankroll settings.',
  odds: '<b>American odds</b>: +150 means a $100 bet wins $150; −150 means you bet $150 to win $100. Plus is the underdog, minus the favorite.',
  implied: 'The <b>implied probability</b> is the chance a price suggests: −150 → 60%, +150 → 40%. It includes the sportsbook\'s cut, so both sides add up to more than 100%.',
  confidence: '<b>Confidence</b> is how much to trust the chance of success: how many sportsbooks it\'s compared with, whether sharp books are included, whether stats agree, and injury news. It isn\'t the chance of winning.',
  chance: '<b>Chance of success</b> is the bot\'s estimate that the bet wins: 75% the market\'s fair price (sportsbooks with their cut removed) and 25% the stats models, when stats cover the bet.',
  fair: 'The <b>fair price</b> is what the odds would be with no sportsbook cut, from the bet\'s chance of success. If the real odds pay more than the fair price, the bet has value.',
  prop: 'A <b>player prop</b> is a bet on one player\'s stat, like LeBron over 24.5 points.',
  line: 'The <b>line</b> is the number in a bet: −4.5 in a spread, 224.5 in a total, 24.5 in a player prop.',
  favorite: 'The <b>favorite</b> is expected to win (minus odds); the <b>underdog</b> isn\'t (plus odds). Underdogs pay more but win less often.',
  sharp: '<b>Sharp sportsbooks</b> like Pinnacle take big bets from pros and move their prices fast, so their prices are the most accurate. The bot weights them most.',
  bankroll_term: 'Your <b>bankroll</b> is the money set aside only for betting. Sizing bets as a small share of it keeps a losing streak from wiping you out.',
  alt: 'An <b>alternate line</b> is the same bet at another number, like −7.5 instead of −4.5, at different odds.',
  b2b: 'A <b>back-to-back</b> is two games on consecutive days. Tired teams can play worse, but in the bot\'s tests rest didn\'t make predictions more accurate.',
};

const CHIPS_START = ['What should I bet on tonight?', 'Safest bets tonight', 'Check a bet', 'How am I doing?', 'Is the bot working?'];
const CHIPS_AFTER_BET = ['Why?', 'How much should I bet?', 'What about the other side?', 'Save it', 'Full details'];

// ---- the panel
function openChat(full) {
  renderAiToggle();
  $('#chatPanel').hidden = false;
  $('#chatPanel').classList.toggle('full', !!full);
  $('#chatFab').hidden = true;
  if (!CHAT.started) {
    CHAT.started = true;
    botSay(`Hi! I answer from the bot's own numbers: scans, stats models, injuries and your records. Ask me about a bet, like <i>Jokic over 11.5 rebounds −120</i> or <i>Knicks −4.5</i>, or tap a suggestion.`, CHIPS_START);
  }
  $('#chatInput').focus();
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
}
function closeChat() {
  $('#chatPanel').hidden = true;
  $('#chatFab').hidden = false;
}

function addMsg(who, html) {
  const div = document.createElement('div');
  div.className = `cmsg ${who}`;
  div.innerHTML = html;
  $('#chatLog').appendChild(div);
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
  if (!who.includes('typing')) saveChat();
  return div;
}
function botSay(html, chips) {
  const div = addMsg('bot', html);
  setChips(chips || []);
  saveChat();
  return div;
}

// The conversation is kept in this browser until you start a new chat (✎).
function saveChat() {
  try {
    const log = $('#chatLog');
    while (log.querySelectorAll('.cmsg:not(.typing)').length > CHAT_MAX_MESSAGES) log.querySelector('.cmsg:not(.typing)').remove();
    const used = new Set([...log.querySelectorAll('.cpick')].map(b => b.dataset.l));
    Object.keys(CHAT.lists).forEach(id => { if (!used.has(id)) delete CHAT.lists[id]; });
    const html = [...log.querySelectorAll('.cmsg:not(.typing)')].map(m => m.outerHTML.replace(/<div class="aitake pending">.*?<\/div>/g, '')).join('');
    localStorage.setItem(CHAT_KEY, JSON.stringify({
      html, chips: [...$('#chatChips').querySelectorAll('.chip')].map(c => c.dataset.send ? { label: c.textContent, send: c.dataset.send } : c.textContent),
      ctx: CHAT.ctx, last: CHAT.last, lastBet: CHAT.lastBet, list: CHAT.list, lists: CHAT.lists, nextList: CHAT.nextList,
    }));
  } catch (e) { /* storage full or off: the chat still works, it just isn't kept */ }
}
function restoreChat() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CHAT_KEY) || 'null'); } catch (e) { saved = null; }
  if (!saved || !saved.html) return;
  $('#chatLog').innerHTML = saved.html;
  setChips(saved.chips || CHIPS_START);
  Object.assign(CHAT, { ctx: saved.ctx || null, last: saved.last || null, lastBet: saved.lastBet || null,
    list: saved.list || null, lists: saved.lists || {}, nextList: saved.nextList || 1, started: true });
}
function newChat() {
  localStorage.removeItem(CHAT_KEY);
  $('#chatLog').innerHTML = '';
  Object.assign(CHAT, { last: null, lastBet: null, ctx: null, list: null, lists: {}, nextList: 1, lastAsk: null, unknowns: 0, started: false });
  openChat($('#chatPanel').classList.contains('full'));
}
// A chip is text to send, or {label, send} when the button shows less than it sends.
function setChips(list) {
  $('#chatChips').innerHTML = list.map(c => typeof c === 'string'
    ? `<button type="button" class="chip">${esc(c)}</button>`
    : `<button type="button" class="chip" data-send="${esc(c.send)}">${esc(c.label)}</button>`).join('');
}

async function chatSend(text) {
  text = text.trim();
  if (!text || CHAT.busy) return;
  addMsg('me', esc(text));
  $('#chatInput').value = '';
  if (text.toLowerCase() === 'check a bet') {
    return botSay('Type the bet with its odds, for example <i>LeBron over 24.5 points −115</i>, <i>Celtics ML +120</i> or <i>Chiefs Bills over 45.5</i>. No odds? I\'ll use FanDuel\'s when I can.');
  }
  if (text === 'Full details') return CHAT.last ? openBet(CHAT.last) : botSay('Check a bet first, then I can open its full details.');
  CHAT.busy = true;
  const wait = addMsg('bot typing', '…');
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, context: CHAT.ctx }) });
    const p = await r.json();
    wait.remove();
    if (!r.ok || p.error) throw new Error(p.error || r.statusText);
    const before = document.querySelectorAll('#chatLog .cmsg.bot').length;
    await answer(p, text);
    remember(p);
    // the chatbot answered with its numbers; the AI adds a short take to that reply
    const msgs = document.querySelectorAll('#chatLog .cmsg.bot');
    const asksBack = (p.bets || []).some(b => b.problem || b.kind === 'choose') || (p.intent === 'about' && (p.bets || []).some(b => b.yesno));
    if (aiOn() && !NO_AI_TAKE.includes(p.intent) && !asksBack && msgs.length > before) aiAssist(text, msgs[msgs.length - 1]);
  } catch (e) {
    wait.remove();
    botSay(`Sorry, something went wrong: ${esc(e.message)}`);
  }
  CHAT.busy = false;
}

// The bet being talked about, sent with the next message so follow-ups work
// ("the under", "at -105", "his assists", "is he playing").
function remember(p) {
  const bets = (p.bets || []).filter(b => (b.kind === 'team' || b.kind === 'player') && !b.error);
  const b = p.intent === 'compare' ? bets[bets.length - 1] : bets[0];
  if (!b) return;
  CHAT.ctx = { kind: b.kind, sport_key: b.sport_key, player: b.player || undefined, team: b.team || undefined,
    opponent: b.opponent || undefined, market: b.market || undefined, outcome: b.outcome || undefined,
    point: b.point != null ? b.point : undefined, odds: b.odds || undefined, event_id: b.event_id || undefined };
}

// ---- answers
async function answer(p, text) {
  switch (p.intent) {
    case 'help': return botSay(helpText(), CHIPS_START);
    case 'thanks': return botSay('Anytime! Ask me about another bet whenever you like.', CHIPS_START);
    case 'talk': return botSay('Sure! I\'m a chatbot for your bets: I answer from the bot\'s own numbers, not opinions. Ask me to check a bet, find the best picks, or follow up with things like <i>what about the under</i>, <i>at −105</i> or <i>is he playing</i>.', CHIPS_START);
    case 'checkhelp': return botSay('Type the bet with its odds, for example <i>LeBron over 24.5 points −115</i>, <i>Celtics ML +120</i> or <i>Chiefs Bills over 45.5</i>. No odds? I\'ll use FanDuel\'s when I can.');
    case 'why': return botSay(whyAnswer(), CHIPS_AFTER_BET);
    case 'save': return saveAnswer();
    case 'term': return botSay(TERM_TEXT[p.term] || 'I don\'t have an explanation for that yet.', ['Best bets tonight', 'Check a bet']);
    case 'record': return botSay(recordAnswer(), ['Bankroll', 'Is the bot working?']);
    case 'bankroll': return botSay(bankrollAnswer(), ['How am I doing?', 'Best bets tonight']);
    case 'picks': return botSay(picksAnswer(), ['Best bets tonight', 'How am I doing?']);
    case 'bot': return botSay(await botAnswer(), ['Best bets tonight', "What's CLV?"]);
    case 'best': return bestAnswer(p);
    case 'more': return moreAnswer();
    case 'stats': return botSay(statsAnswer(p), statsChips(p));
    case 'schedule': return botSay(scheduleAnswer(p), ['What should I bet on tonight?', 'Safest bets tonight']);
    case 'result': return botSay(resultAnswer(p), ['Who plays tonight?', 'What should I bet on tonight?']);
    case 'winner': return winnerAnswer(p.bets);
    case 'faq': return botSay(FAQ_TEXT[p.topic] || helpText(), ['What should I bet on tonight?', 'Check a bet']);
    case 'smalltalk': {
      const t = SMALLTALK_TEXT[p.topic];
      return botSay((typeof t === 'function' ? t() : t) || 'Ha. Ask me about a bet whenever you like.', CHIPS_START);
    }
    case 'choose': {
      const names = p.bets[0].choices;
      return botSay(names.some(n => n.includes('(')) ? 'Which player did you mean?' : 'Which team did you mean?',
        names.map(n => ({ label: n, send: `${n.replace(/ \(.*\)$/, '')} ${text}`.slice(0, 160) })));
    }
    case 'stake':
      if (p.bets && p.bets[0] && p.bets[0].market) return checkAnswer(p.bets[0], true);
      return CHAT.last ? botSay(stakeLine(CHAT.last), CHIPS_AFTER_BET)
        : botSay('Which bet? Check one first, like <i>Knicks −4.5 −110</i>, then ask how much to bet.');
    case 'check': return checkAnswer(p.bets[0]);
    case 'compare': return compareAnswer(p.bets);
    case 'injury': return botSay(injuryAnswer(p.bets[0]), ['Best bets tonight']);
    case 'form': {
      const b = p.bets[0];
      const ask = b.followup && b.kind === 'player' && b.point == null ? '<br>Tell me the line to check a bet on it, e.g. <i>over 6.5 −115</i>.' : '';
      return botSay((formAnswer(b, true) || `I don't have recent games for ${esc(b.player || b.team || 'that')}.`) + ask, ['Best bets tonight']);
    }
    case 'about': return botSay(aboutAnswer(p.bets[0]), ['Best bets tonight']);
    default:
      if (state.cfg && state.cfg.ai) {
        const ai = await aiAnswer(text);
        if (ai) return ai;
      }
      CHAT.unknowns = (CHAT.unknowns || 0) + 1;
      return botSay(CHAT.unknowns === 1
        ? `I'm not sure what you mean, sorry: I'm a chatbot for betting questions, so I can't answer everything. I've saved it so I can learn it. Did you mean one of these? Or try a bet like <i>Knicks −4.5</i>.`
        : `Sorry, I didn't get that either (saved so I can learn it). Here's what I can do:<br>${helpText()}`, p.suggestions || CHIPS_START);
  }
}

// ---- AI (Gemini) for questions the chatbot doesn't understand
// What the page knows, as short lines for the AI's fact sheet: the bet being discussed,
// tonight's best and likeliest bets, and your record. Only betting data, nothing personal.
function aiFacts(question) {
  const lines = [];
  const r = CHAT.last;
  if (r) {
    const parts = [`Bet being discussed: ${betLabel(r)}${r.decimal ? ` at ${r.american}` : ''} (${r.event}, starts ${r.start}).`];
    try {
      parts.push(`Chance of success ${pct(chance(r), 0)}${r.ev != null ? `, value ${signed(r.ev)}` : ''}, ${r.stats_only ? 'stats only, no market price' : `${confidence(r).label} confidence`}.`);
      if (r.decimal) parts.push(`Verdict: ${yesNo(r, ...verdictOf(r)).slice(1).join(': ')}.`);
    } catch (e) { /* partial facts are fine */ }
    const inj = (INJ.known[statKey(r)] || {}).player;
    if (inj) parts.push(`${inj.name} is listed ${inj.status} on the injury report.`);
    lines.push(parts.join(' '));
  }
  const now = new Date().toISOString();
  const rows = scannedRows().filter(x => x.start > now);
  if (rows.length) {
    const value = Calc.pickSides(rows, 'value', chance).filter(x => x.ev > 0).sort((a, b) => b.ev - a.ev).slice(0, 5);
    const likely = Calc.pickSides(rows, 'likely', chance).sort((a, b) => chance(b) - chance(a)).slice(0, 3);
    const fmt = x => `${betLabel(x)} ${x.american} (${x.event}): ${pct(chance(x), 0)} chance, value ${signed(x.ev)}, ${confidence(x).label} confidence`;
    lines.push(value.length ? `Best-value bets in the scans: ${value.map(fmt).join('; ')}.` : 'No bets with positive value in the current scans.');
    lines.push(`Most likely to win in the scans: ${likely.map(fmt).join('; ')}.`);
    lines.push(rows.every(x => x.books_used.length <= 2) ? 'Scans use free odds: FanDuel compared with DraftKings and BetMGM only.' : 'Scans compare FanDuel with several sportsbooks.');
  } else {
    lines.push('No odds scanned right now.');
  }
  // your own record and bankroll only when the question is about you
  const aboutMe = /\b(i|i'?m|me|my|mine|myself|bankroll|record|wagers?|balance)\b/i.test(question || '');
  if (aboutMe && WAGERS.list.length) {
    const s = Calc.wagerSummary(WAGERS.list);
    lines.push(`User's logged wagers: ${s.won}-${s.lost}${s.push ? `-${s.push}` : ''}, net ${signedDollars(s.net)} on ${dollars(s.wagered)} wagered, ${s.pending} pending.`);
  }
  if (aboutMe && bankrollSet()) lines.push(`User's bankroll balance: ${dollars(Calc.balance(WAGERS.bankroll, WAGERS.list))}.`);
  return lines;
}

function chatHistory() {
  return [...document.querySelectorAll('#chatLog .cmsg:not(.typing)')].slice(-7, -1)
    .map(m => ({ who: m.classList.contains('me') ? 'user' : 'bot', text: m.innerText.replace(/\s+/g, ' ').slice(0, 400) }));
}

// ---- AI takes on every reply (Gemini), with an on/off switch in the chat header
const NO_AI_TAKE = ['unknown', 'help', 'thanks', 'checkhelp', 'more', 'save', 'choose', 'smalltalk', 'talk', 'term', 'faq'];
const aiOn = () => !!(state.cfg && state.cfg.ai) && localStorage.getItem('oddfinder.chatAi') !== '0';
function renderAiToggle() {
  const b = $('#chatAi');
  b.hidden = !(state.cfg && state.cfg.ai);
  b.textContent = aiOn() ? '✨ AI on' : 'AI off';
  b.setAttribute('aria-pressed', String(aiOn()));
  b.title = aiOn() ? 'AI takes on replies (Gemini). Click to turn off and save your daily AI answers.' : 'Click to add AI takes to replies (Gemini).';
}
const aiLeft = n => n != null ? ` · ${n} left today` : '';

async function aiAssist(question, el) {
  const botAnswer = el.innerText.replace(/\s+/g, ' ').slice(0, 1500);    // before the placeholder below
  const take = document.createElement('div');
  take.className = 'aitake pending';
  take.innerHTML = '<span class="ailabel">✨ AI is adding a take…</span>';
  el.appendChild(take);
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
  try {
    const r = await fetch('/api/ai', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'assist', message: question, facts: aiFacts(question),
        bot_answer: botAnswer, history: chatHistory() }) });
    const j = await r.json();
    if (!r.ok || j.error || !j.answer) {
      if (j && /limit/.test(j.error || '')) { take.className = 'aitake'; take.innerHTML = `<span class="ailabel">✨ ${esc(j.error)}</span>`; }
      else take.remove();
    } else {
      take.className = 'aitake';
      take.innerHTML = `<span class="ailabel">✨ AI take (Gemini) · can be wrong${aiLeft(j.left)}</span>${esc(j.answer).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>')}`;
    }
  } catch (e) { take.remove(); }
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
  saveChat();
}

async function aiAnswer(text) {
  const wait = addMsg('bot typing', 'Thinking…');
  try {
    const r = await fetch('/api/ai', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, facts: aiFacts(text), history: chatHistory() }) });
    const j = await r.json();
    wait.remove();
    if (!r.ok || j.error || !j.answer) {
      if (j && j.error) botSay(`<span class="muted small">AI unavailable: ${esc(j.error)}</span>`);
      return null;
    }
    const html = esc(j.answer).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>');
    return botSay(`<span class="ailabel">✨ AI answer (Gemini) · can be wrong; the numbers come from the bot${aiLeft(j.left)}</span>${html}`, CHIPS_START);
  } catch (e) {
    wait.remove();
    return null;
  }
}

const FAQ_TEXT = {
  losing: 'Sorry, that\'s rough. Losing streaks happen to everyone, even with good bets, so <b>don\'t chase losses</b> with bigger bets. Take a break, keep bets small (1–2% of your bankroll), and set a daily loss limit in <b>My wagers → Bankroll settings</b>; I\'ll warn you when you hit it. If betting stops being fun or you feel you can\'t stop, call or text <b>1-800-GAMBLER</b> (free, 24/7).',
  how_work: 'I compare FanDuel\'s price on every bet with other sportsbooks (their prices with the margin removed: the "market"), and add the bot\'s stats models (team ratings, player stats, injuries). A bet is <b>good value</b> when FanDuel pays more than its chance of winning is worth. Every bet is tracked and graded afterwards, so you can see how the bot does in <b>Bot results</b>.',
  win: 'Honest tips: <b>1)</b> bet only when the price is better than the chance (value), not on hunches; <b>2)</b> keep each bet small, 1–2% of your bankroll; <b>3)</b> shop for the best price; <b>4)</b> track every bet (My wagers); <b>5)</b> avoid big parlays; <b>6)</b> judge yourself over hundreds of bets, not one night. Even good bettors lose often: the edge only shows over time.',
  parlays: 'Parlays pay big but the sportsbook\'s margin stacks up on every leg, so on average they lose more than single bets. A 2–3 leg parlay of real value picks can be fine for fun; long parlays are mostly lottery tickets. Same-game legs that go together (a QB and his receiver) aren\'t priced as independent, so be careful there.',
  bankroll_size: 'Only money you can afford to lose completely. Pick an amount for the season, then bet 1–2% of it per bet (that\'s your unit). Set it in <b>My wagers → Set up bankroll</b> and I\'ll suggest a stake for every bet.',
  worth: 'For most people betting loses money over time, because the sportsbook takes a cut on every bet. You can do better than average by only betting real value, keeping bets small and tracking results, but there\'s no guaranteed profit. Treat it as entertainment with a budget.',
  live: 'I only check bets <b>before</b> the game starts: live odds move too fast to compare reliably, so I don\'t judge in-game bets.',
  other_books: 'Right now I check <b>FanDuel</b>\'s prices against the other books. Other sportsbooks show up in each bet\'s price comparison, but picks are for FanDuel. Adding another sportsbook as the main one is possible later.',
  props_vs_lines: 'Player props are where sportsbooks make more mistakes, but they\'re also thinner markets with bigger margins. Game lines are sharper (harder to beat) but more reliable. The bot shows both; value is what matters, not the bet type.',
  props_out: 'FanDuel usually posts player props the day before or the morning of a game, closer to tip-off for some players. Props come from The Odds API (about 4 credits a game), so I don\'t fetch them on my own: tick <b>Player props</b> and press Scan in Find bets, or open <b>More bets</b> for one game. If a scan finds none, they aren\'t posted yet: try again closer to game time.',
  news: 'I don\'t have news headlines yet, but I do have ESPN\'s injury reports (NBA, NHL, NFL): ask <i>is Embiid playing</i> or <i>is anyone hurt on the Knicks</i>. For a player\'s recent games, ask <i>LeBron last 10</i>.',
  units_size: 'A unit is usually 1% of your bankroll (1–2% is common). With $500, one unit is $5. Set it in <b>My wagers → Bankroll settings</b>.',
};
const SMALLTALK_TEXT = {
  how: 'Doing good, thanks! Ready to look at some bets. Want the best ones for tonight?',
  who: 'I\'m the Odd Finder chat: I answer from the bot\'s own numbers (odds, stats, injuries, your records). I\'m not an AI, so I stick to betting questions.',
  laugh: '😄 Anything you want me to check?',
  bye: 'See you! Good luck, and bet responsibly.',
  ai: () => state.cfg && state.cfg.ai
    ? `Yes: I'm connected to Google's Gemini (3.5 Flash-Lite). I answer from the bot's own numbers first, and with <b>✨ AI on</b> (top of this chat) Gemini adds a short take; questions I don't understand go to Gemini directly. It's ${aiOn() ? 'on' : 'off'} right now.`
    : 'No AI is connected right now, so I answer as a chatbot from the bot\'s own numbers. Adding a Gemini key to config.json connects one.',
  morning: 'Hey! Want to see what\'s good tonight?',
  smart: 'I try! I only know what the bot\'s numbers say, so I\'m best at bet questions. Tell me what I got wrong and I\'ll get better at it.',
};

// ---- past stats from saved games: season averages, game logs, head-to-head
const STAT_SHORT = { pts: 'PTS', reb: 'REB', ast: 'AST', '3pm': '3PM', sog: 'SOG', passyds: 'Pass yds', rushyds: 'Rush yds',
  recyds: 'Rec yds', rec: 'Rec' };
// games are stored in UTC ("2026-11-27T01:20Z"); show the local date (Thanksgiving night, not the next day)
const fmtDay = d => new Date(d.length > 10 ? d : d + 'T12:00:00').toLocaleDateString([], { month: 'short', day: 'numeric', year: '2-digit' });
const num = x => x == null ? '–' : (Math.round(x * 10) / 10).toString();

function table(head, rows) {
  return `<div class="ctable-wrap"><table class="ctable"><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') + '</table></div>';
}

// Columns worth showing: stats the player actually has (no receiving yards for a QB), and
// minutes only where they're real (NFL box scores don't have them; NHL's are time on ice).
function shownKeys(st) {
  return st.keys.filter(k => st.seasons.some(x => x.avg[k] > 0));
}
const minLabel = st => st.sport === 'icehockey_nhl' ? 'TOI' : st.sport === 'americanfootball_nfl' ? null : 'Min';

function playerLog(st, games) {
  const keys = shownKeys(st), ml = minLabel(st);
  return table(['Date', 'Opp', ...(ml ? [ml] : []), ...keys.map(k => STAT_SHORT[k] || k)],
    games.map(g => [fmtDay(g.date), `${g.home ? 'vs' : '@'} ${esc(shortTeam(g.opp))}`, ...(ml ? [num(g.min)] : []), ...keys.map(k => num(g[k]))]));
}

function statsAnswer(p) {
  const b = (p.bets || [])[0] || {}, st = p.stats_data;
  const who = b.player || b.team || 'that';
  if (!st) {
    return b.kind === 'player'
      ? `I don't have saved games for ${esc(who)}. Player stats cover the NBA (points, rebounds, assists, 3-pointers), NHL (points, shots on goal) and NFL (passing, rushing and receiving yards, receptions), last season and this one.`
      : `I don't have saved results for ${esc(who)}.`;
  }
  if (b.kind === 'player') {
    const keys = shownKeys(st), lab = k => STAT_SHORT[k] || k, ml = minLabel(st);
    const parts = [`<b>${esc(b.player)}</b> <span class="muted small">${esc(st.team)}</span>`];
    if (p.n === 1) {                                        // last game
      const g = st.log[0];
      parts.push(`Last game, ${fmtDay(g.date)} ${g.home ? 'vs' : '@'} ${esc(g.opp)}: ${ml ? `${num(g.min)} ${ml.toLowerCase()}, ` : ''}` + keys.map(k => `<b>${num(g[k])}</b> ${esc(st.labels[k])}`).join(', ') + '.');
    }
    if (st.vs) {
      const v = st.vs;
      parts.push(v.games.length
        ? `<b>vs ${esc(v.opponent)}</b>: ${v.games.length} game${v.games.length === 1 ? '' : 's'} in the last two seasons, averaging ` + keys.map(k => `${num(v.avg[k])} ${esc(st.labels[k])}`).join(', ') + '.' + playerLog(st, v.games)
        : `No games against ${esc(v.opponent)} in the last two seasons.`);
    }
    if (st.best) {
      parts.push(`<b>Best games by ${esc(st.labels[st.best.stat])}</b> (last two seasons):` + playerLog(st, st.best.games));
    }
    if (!st.vs && !st.best && p.n !== 1) {
      parts.push('<b>Per game, by season</b>' + table(['Season', 'G', ...(ml ? [ml] : []), ...keys.map(lab)],
        st.seasons.map(x => [esc(x.season), x.games, ...(ml ? [num(x.min)] : []), ...keys.map(k => num(x.avg[k]))])));
      if (st.seasons[0]) parts.push(`<span class="muted small">Highs ${esc(st.seasons[0].season)}: ` + keys.map(k => `${num(st.seasons[0].high[k])} ${esc(st.labels[k])}`).join(', ') + '</span>');
      const n = p.log ? st.log.length : Math.min(5, st.log.length);
      parts.push(`<b>Last ${n} game${n === 1 ? '' : 's'}</b>` + playerLog(st, st.log.slice(0, n)));
    }
    return parts.join('<br>');
  }
  // team
  const parts = [`<b>${esc(b.team)}</b>`];
  parts.push(table(['Season', 'Record', 'Home', 'Away', 'Scored', 'Allowed'],
    st.seasons.map(x => [esc(x.season), esc(x.record), esc(x.home), esc(x.away), num(x.for), num(x.against)])));
  const n = +st.streak.slice(1), word = { W: ['won', 'wins'], L: ['lost', 'losses'], D: ['drew', 'draws'] }[st.streak[0]];
  const streak = n === 1 ? `${word[0]} their last game` : `${n} ${word[1]} in a row`;
  parts.push(`<span class="muted small">Current streak: ${streak}.</span>`);
  const games = gs => table(['Date', 'Opp', 'Score', ''], gs.map(g => [fmtDay(g.date), `${g.home ? 'vs' : '@'} ${esc(shortTeam(g.opp))}`,
    `${num(g.us)}-${num(g.them)}`, `<b class="${g.result === 'W' ? 'up' : g.result === 'L' ? 'down' : ''}">${g.result}</b>`]));
  if (st.h2h) {
    const h = st.h2h;
    parts.push(h.games.length
      ? `<b>vs ${esc(h.opponent)}</b> (last two seasons): ${esc(h.record)}, average score ${num(h.for)}-${num(h.against)}.` + games(h.games)
      : `No games against ${esc(h.opponent)} in the last two seasons.`);
  } else {
    parts.push(`<b>Last ${st.log.length} games</b>` + games(st.log));
  }
  return parts.join('<br>');
}

function statsChips(p) {
  const b = (p.bets || [])[0] || {};
  const name = b.player || b.team || '';
  if (!name) return CHIPS_START;
  return b.kind === 'player'
    ? [`${name} last 10 games`, `${name} best games`, `Is ${name} playing?`]
    : [`${name} last 10`, `When do the ${nick(name)} play`, `Best bet in the ${nick(name)} game`];
}

function scheduleAnswer(p) {
  const games = p.games || [];
  const scope = p.scope || {};
  if (!games.length) return scope.team ? `I couldn't find an upcoming ${esc(scope.team)} game.` : `No games found ${p.window === 'tomorrow' ? 'tomorrow' : 'for the rest of today'} in those leagues.`;
  const rows = scannedRows();
  const lean = g => {
    const ml = rows.filter(r => r.event_id === g.event_id && r.market === 'h2h');
    if (!ml.length) return '';
    const fav = ml.reduce((a, b) => chance(b) > chance(a) ? b : a);
    return ` · bot likes <b>${esc(shortTeam(fav.outcome))}</b> ${pct(chance(fav), 0)}`;
  };
  const byLeague = {};
  games.forEach(g => (byLeague[g.sport] ||= []).push(g));
  return (scope.team ? `<b>${esc(scope.team)}</b>, next games:` : `<b>Games ${p.window === 'tomorrow' ? 'tomorrow' : 'today'}</b>:`) +
    Object.entries(byLeague).map(([lg, gs]) => `<br><b>${esc(lg)}</b><ul>${gs.map(g => `<li>${esc(g.away)} @ ${esc(g.home)}, ${when(g.start)}${lean(g)}</li>`).join('')}</ul>`).join('') +
    `<span class="muted small">Ask <i>best bet in the ${esc(nick((games[0] || {}).home || ''))} game</i> for picks.</span>`;
}

function resultAnswer(p) {
  const b = (p.bets || [])[0] || {};
  if (b.kind === 'player') {
    const f = Object.values(b.forms || {}).filter(Boolean);
    if (!f.length) return `I don't have ${esc(b.player)}'s recent games.`;
    const g = f[0].recent[0];
    return `<b>${esc(b.player)}</b>, last game (${esc(g.date)}, ${g.home ? 'vs' : '@'} ${esc(shortTeam(g.opp))}): ` +
      f.map(x => `${x.recent[0].value} ${esc(x.stat)}`).join(', ') + '.';
  }
  const r = p.result;
  if (!r) return `I don't have a recent result for ${esc(b.team || 'that team')}.`;
  return `<b>${esc(r.team)}</b> ${r.tie ? 'tied' : r.won ? 'won' : 'lost'} ${r.us}-${r.them} ${r.home ? 'vs' : 'at'} ${esc(r.opponent)} (${new Date(r.date).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}).` +
    (b.event ? ` Next: ${esc(b.event)}, ${when(b.start)}.` : '');
}

function winnerAnswer(bets) {
  const ok = (bets || []).filter(b => !b.error && !b.problem).map(b => ({ b, ...chatRow(b) })).filter(x => x.row);
  if (ok.length < 2) {
    const b = (bets || [])[0] || {};
    return botSay(b.error ? esc(b.error) : `I couldn't get both teams' chances for that game yet. Try again in a minute, or scan its league in Find bets.`, ['Who plays tonight?']);
  }
  const [a, c] = ok;
  const fav = chance(a.row) >= chance(c.row) ? a : c, dog = fav === a ? c : a;
  const val = x => x.row.ev != null ? x.row.ev : -1;
  const better = val(a) >= val(c) ? a : c;
  CHAT.last = fav.row;
  const price = x => x.row.decimal ? ` at ${esc(x.row.american)}` : '';
  botSay(`<b>The bot likes ${esc(fav.b.team)}</b> to win ${esc(fav.row.event)}: <b>${pct(chance(fav.row), 0)}</b> vs ${pct(chance(dog.row), 0)} for ${esc(dog.b.team)}.` +
    `<br>${better === fav ? `It's also the better value${price(fav)} (${signed(fav.row.ev)}).` : `But the better value is <b>${esc(better.b.team)}</b>${price(better)} (${signed(better.row.ev)}): FanDuel's price on ${esc(fav.b.team)}${price(fav)} is ${fav.row.ev < 0 ? 'too steep' : 'thinner'}.`}` +
    `<br><span class="muted small">${pct(1 - chance(fav.row), 0)} of the time the other team wins: nothing is certain.</span>`, ['Why?', 'How much should I bet?', 'What about the other side?']);
}

function helpText() {
  return `<ul class="chelp">
    <li><b>Check a bet:</b> <i>Jokic over 11.5 rebounds −120</i>, <i>Knicks −4.5</i>, <i>Lakers ML +130</i>, <i>Chiefs Bills over 45.5</i></li>
    <li><b>Compare:</b> <i>Knicks −4.5 or Knicks Celtics over 224.5</i></li>
    <li><b>Best bets:</b> <i>best NBA props tonight</i>, <i>best bets here</i> (the list on your screen)</li>
    <li><b>Players and teams:</b> <i>Jalen Brunson</i>, <i>McDavid last 10 shots</i>, <i>is Embiid playing</i></li>
    <li><b>You:</b> <i>how am I doing</i>, <i>bankroll</i>, <i>my picks</i>, <i>how much should I bet</i></li>
    <li><b>The bot:</b> <i>is the bot working</i>; <b>terms:</b> <i>what's a push</i>, <i>what is Kelly</i></li></ul>`;
}

// Every scanned bet the page has (main lines and More bets).
const scannedRows = () => Object.values(state.results).flatMap(v => v.rows).concat(more.rows);

function findScanned(bet) {
  return scannedRows().find(r => r.event_id === bet.event_id && r.selection === bet.selection
    && (r.market === bet.market || r.market === 'alternate_' + bet.market));
}

// The bet as a row like the scan's, so chance, confidence, value and the pop-up all work.
function chatRow(bet) {
  const scanned = findScanned(bet);
  const s = bet.stats && bet.stats.prob != null ? bet.stats : null;
  let row, based;
  if (scanned) { row = { ...scanned }; based = ['full', 'Full check: from your scan'] }
  else if (bet.row) { row = { ...bet.row }; based = ['full', 'Full check: free market odds (FanDuel vs DraftKings and BetMGM)'] }
  else if (s) {
    row = { event_id: bet.event_id, sport_key: bet.sport_key, sport: bet.sport, event: bet.event, home_team: bet.home,
      away_team: bet.away, start: bet.start, market: bet.market, market_label: bet.market_label, selection: bet.selection,
      outcome: bet.outcome, point: bet.point, player: bet.player || null, labeled: true, books_used: [], compare: [],
      sharp: false, fair_prob: s.prob, stats_only: true, decimal: null };
    based = ['stats', bet.player ? 'Stats only: no market price for this prop without a scan, so it\'s rougher' : 'Stats only: no market price found, so it\'s rougher'];
  } else return { row: null };
  if (bet.decimal) {                                 // your odds, not FanDuel's
    Object.assign(row, { decimal: bet.decimal, american: (bet.odds > 0 ? '+' : '') + bet.odds,
      book_prob: Math.round(10000 / bet.decimal) / 10000, ev: Math.round((row.fair_prob * bet.decimal - 1) * 10000) / 10000, user_odds: true });
  } else if (row.stats_only) {
    row.ev = null;
  }
  const k = statKey(row);
  if (bet.stats && !(k in STATS.known)) STATS.known[k] = bet.stats;
  if (bet.injury) { INJ.known[k] = bet.injury; Object.assign(INJ.teams, bet.injury_teams || {}); Object.assign(INJ.updated, bet.injury_updated || {}); }
  return { row, based };
}

// A plain answer first: Yes / Lean yes / Not really / No, from the verdict and any injury.
function yesNo(row, tone, title) {
  const inj = (INJ.known[statKey(row)] || {}).player;
  if (inj && inj.level === 'out') return ['bad', 'No', `${inj.name} is listed out`];
  if (!row.decimal) return ['ok', 'Can\'t say yet', 'I need the odds'];
  if (title === 'Strong value') return ['good', 'Yes', 'good value at this price'];
  if (title === 'Value pick') return ['good', 'Yes, a small edge', 'slightly good value'];
  if (/Possible value|Slight value/.test(title)) return ['ok', 'Lean yes', 'some value, but a rough estimate'];
  if (title === 'About a fair price') return ['ok', 'Not really', 'no edge at this price'];
  return ['bad', 'No', 'the price pays less than the bet is worth'];
}

function verdictOf(row) {
  if (!row.decimal) return ['ok', 'Need the odds', 'Tell me the odds (e.g. −115) and I\'ll say whether the price is worth it.'];
  if (row.stats_only) {                    // no market price: the stats' opinion alone, so softer words
    return row.ev > 0.03 ? ['ok', 'Possible value, stats only', 'The stats think this price pays more than the bet is worth, but there\'s no market price to confirm it.']
      : row.ev > 0 ? ['ok', 'Slight value by the stats', 'A small edge by the stats alone: rough, so treat it as a lean.']
      : row.ev > -0.02 ? ['ok', 'About a fair price', 'By the stats, this price is close to what the bet is worth.']
      : ['bad', 'Not worth it at this price', 'By the stats, this price pays less than the bet is worth.'];
  }
  return explain(row).verdict;
}

// Reasons for a stats-only check, without the market words the full check uses.
function statsOnlyReasons(row, bet) {
  const s = bet.stats || {}, good = [], risk = [];
  if (s.expected_stat != null) good.push(`From ${bet.player}'s recent games, minutes and the opponent's defense, the stats expect about ${s.expected_stat} ${s.stat_name}, against a line of ${row.point}.`);
  if (s.form_home && s.form_away) good.push(`Last 10 games: ${bet.away} ${s.form_away}, ${bet.home} ${s.form_home}.`);
  if (row.decimal) risk.push(`At ${row.american} the price implies a ${pct(1 / row.decimal)} chance; the stats give it ${pct(row.fair_prob)}.`);
  risk.push('No market price to compare with, so this is the stats\' opinion alone: rougher than a full check.' +
    (bet.player ? ' Scan this game\'s player props for a full check.' : ''));
  return { good, risk };
}

function checkAnswer(bet, stakeToo) {
  if (!bet || bet.problem) return botSay(bet && bet.problem ? esc(bet.problem) : 'Tell me the bet, like <i>Knicks −4.5 −110</i>.');
  if (bet.error) return botSay(`${esc(bet.error)}${formAnswer(bet) ? `<br>${formAnswer(bet)}` : ''}`, ['Best bets tonight']);
  const { row, based } = chatRow(bet);
  if (!row) {
    const why = bet.stats_pending ? 'The stats models are still loading (the first start takes a minute or two). Ask me again shortly.'
      : bet.stats && bet.stats.reason ? `I can't judge this one: ${esc(bet.stats.reason.toLowerCase())}, and there's no market price for it without a scan.`
      : 'I can\'t judge this one: no market price and no stats cover it.';
    return botSay(`<b>${esc(bet.selection || bet.player || bet.team)}</b><br>${why}${formAnswer(bet) ? `<br>${formAnswer(bet)}` : ''}`, ['Best bets tonight']);
  }
  CHAT.last = row;
  CHAT.lastBet = bet;
  const ch = chance(row), [tone, title, line] = verdictOf(row);
  const c = row.stats_only ? { label: 'Low' } : confidence(row);
  const ex = row.stats_only ? statsOnlyReasons(row, bet) : row.decimal ? explain(row) : { good: [], risk: [] };
  const fair = americanOdds(1 / ch);
  const odds = row.decimal ? `${row.american}${row.user_odds ? ' (your odds)' : ` (${esc(state.book ? state.book.name : 'FanDuel')})`}` : '–';
  const extra = [];
  if (bet.fd_line && !findScanned(bet)) extra.push(`FanDuel's current line is <b>${esc(bet.fd_line.selection)} ${esc(bet.fd_line.american)}</b>; you asked about a different number.`);
  const f = formAnswer(bet);
  if (f) extra.push(f);
  const inj = (INJ.known[statKey(row)] || {}).player;
  if (inj) extra.push(`<span class="inj ${inj.level}">⚠ ${esc(inj.name)}: ${esc(inj.status)}</span> ${esc(inj.comment || '')}`);
  else if (!bet.player && bet.injury_teams) extra.push(teamInjuries(bet));
  if (row.decimal) extra.push(stakeSuggestion(row).text);
  const yn = yesNo(row, tone, title);
  const html = `
    <div class="cbet">
      <b>${esc(betLabel(row))}</b><span class="muted small">${esc(row.event)}, ${when(row.start)}</span>
      <div class="cyes ${yn[0]}"><b>${esc(yn[1])}</b> — ${esc(yn[2])}.</div>
      <p class="plain">${esc(plainSummary(row))}</p>
      <div class="cnums">
        <div><b>${pct(ch, 0)}</b>chance</div>
        <div><b>${row.ev != null ? signed(row.ev) : '–'}</b>value</div>
        <div><b>${odds}</b>odds</div>
        <div><b>${fair}</b>fair odds</div>
        <div><b>${c.label}</b>confidence</div>
      </div>
      <span class="cbased ${based[0]}">${esc(based[1])}</span>
      ${ex.good.length ? `<ul class="good">${ex.good.slice(0, 2).map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      ${ex.risk.length ? `<ul class="risk">${ex.risk.slice(0, 3).map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      ${extra.map(x => `<p>${x}</p>`).join('')}
    </div>`;
  const div = botSay(html, CHIPS_AFTER_BET);
  if (stakeToo && row.decimal) botSay(stakeLine(row), CHIPS_AFTER_BET);
  return div;
}

// "Why?": every reason for and against the last bet checked.
function whyAnswer() {
  const row = CHAT.last;
  if (!row) return 'Check a bet first, then ask me why.';
  if (!row.decimal) return 'I need the odds to say whether it\'s worth it: tell me the price, like <i>−115</i>.';
  const ex = row.stats_only ? statsOnlyReasons(row, CHAT.lastBet || {}) : explain(row);
  const c = row.stats_only ? null : confidence(row);
  return `<b>${esc(betLabel(row))}</b>` +
    (ex.good.length ? `<br>For it:<ul class="good">${ex.good.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : '<br>Nothing stands out in its favor.') +
    (ex.risk.length ? `Against it:<ul class="risk">${ex.risk.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : '') +
    (c ? `<span class="muted small">${c.label} confidence because of: ${esc(c.why)}.</span>` : '');
}

async function saveAnswer() {
  const row = CHAT.last;
  if (!row) return botSay('Check a bet first, then say <i>save it</i>.');
  if (savedPick(row)) return botSay(`${esc(betLabel(row))} is already in My picks.`, CHIPS_AFTER_BET);
  if (!row.decimal) return botSay('I need the odds before saving it: tell me the price, like <i>−115</i>.');
  try {
    await toggleSave(row, null);
    botSay(`Saved <b>${esc(betLabel(row))}</b> ${esc(row.american)} to My picks. It's graded automatically after the game; tap ★ there to remove it.`, CHIPS_AFTER_BET);
  } catch (e) {
    botSay(`I couldn't save it: ${esc(e.message)}`);
  }
}

function stakeLine(row) {
  if (!row.decimal) return 'I need the odds to size a bet: tell me the price, like <i>−115</i>.';
  return `${stakeSuggestion(row).text} <span class="muted">Even a good bet loses often, so keep each bet small.</span>`;
}

function compareAnswer(bets) {
  const parts = bets.map(b => ({ b, ...(b.error || b.problem ? { row: null } : chatRow(b)) }));
  parts.forEach(x => checkAnswer(x.b));
  const ok = parts.filter(x => x.row);
  if (ok.length < 2) return;
  const [a, b] = ok;
  const val = x => x.row.ev != null ? x.row.ev : -1;
  const better = val(a) === val(b) ? (chance(a.row) >= chance(b.row) ? a : b) : val(a) > val(b) ? a : b;
  const likelier = chance(a.row) >= chance(b.row) ? a : b;
  botSay(`<b>Comparing:</b> ${esc(betLabel(better.row))} has the better value${better === likelier ? ' and the better chance'
    : `, while ${esc(betLabel(likelier.row))} is more likely to win but pays less for it`}. The bot ranks by value: over many bets, that's what pays.`, CHIPS_AFTER_BET);
}

function teamInjuries(bet) {
  const lists = Object.entries(bet.injury_teams || {}).map(([k, ps]) => [k.split('|').slice(1).join('|'), ps.filter(p => p.level !== 'question' || ps.length < 6)]);
  const txt = lists.map(([team, ps]) => ps.length
    ? `<b>${esc(team)}</b>: ${ps.slice(0, 5).map(p => `${esc(p.name)} (${esc(p.status)})`).join(', ')}${ps.length > 5 ? ` and ${ps.length - 5} more` : ''}`
    : `<b>${esc(team)}</b>: nobody listed`).join('<br>');
  return txt ? `Injury report:<br>${txt}` : '';
}

// A team's short name, unless its nickname is generic ("City", "United", "Town"): then its full name.
const GENERIC_NICK = ['City', 'United', 'Town', 'Albion', 'FC', 'Athletic', 'Rovers', 'Wanderers', 'County', 'Rangers', 'Sporting'];
const shortTeam = n => GENERIC_NICK.includes(nick(n)) ? n : nick(n);

function formAnswer(bet, wantAll) {
  const forms = bet && bet.forms;
  if (!forms) return '';
  if (bet.kind === 'player') {
    const list = Object.entries(forms).filter(([m, f]) => f && (wantAll || m === bet.market));
    if (!list.length) return '';
    return list.map(([, f]) => {
      const ov = blk => f.line != null && blk.over != null ? `, over ${f.line} in ${blk.over} of ${blk.games}` : '';
      return `<b>${esc(bet.player)}, ${esc(f.stat)}:</b> last 5 avg ${f.last5.avg}${ov(f.last5)}; last 10 avg ${f.last10.avg}${ov(f.last10)}; last 20 avg ${f.last20.avg}${ov(f.last20)}.` +
        (f.home_avg != null && f.away_avg != null ? ` Home ${f.home_avg}, away ${f.away_avg}.` : '') +
        (wantAll ? `<br><span class="muted small">Recent: ${f.recent.map(g => `${g.value} ${g.home ? 'vs' : '@'} ${esc(shortTeam(g.opp))}`).join(', ')}</span>` : '');
    }).join('<br>');
  }
  return Object.entries(forms).filter(([, f]) => f).map(([team, f]) =>
    `<b>${esc(team)}</b> last ${f.games.length}: ${f.record}, scoring ${f.for} and allowing ${f.against} on average.` +
    (wantAll ? `<br><span class="muted small">${f.games.map(g => `${g.result} ${g.score} ${g.home ? 'vs' : '@'} ${esc(shortTeam(g.opp))}`).join(', ')}</span>` : '')).join('<br>');
}

function injuryAnswer(bet) {
  if (!bet || bet.error) return esc(bet && bet.error || 'Who? Try a player or team name.');
  if (bet.kind === 'player') {
    const p = bet.injury && bet.injury.player;
    if (!['basketball_nba', 'icehockey_nhl', 'americanfootball_nfl'].includes(bet.sport_key)) return 'Injury reports cover the NBA, NHL and NFL only.';
    return p ? `<span class="inj ${p.level}">⚠ ${esc(p.name)}: ${esc(p.status)}</span> ${esc(p.comment || '')}${p.returns ? ` Expected back around ${esc(p.returns)}.` : ''}`
      : `${esc(bet.player)} isn't on ESPN's injury report${bet.event ? ` for ${esc(bet.event)}, ${when(bet.start)}` : ''}.`;
  }
  return teamInjuries(bet) || 'No injury report for that league.';
}

function aboutAnswer(bet) {
  if (!bet) return helpText();
  const parts = [];
  if (bet.yesno) parts.push(`<b>Can't say yet</b> — which bet on ${esc(bet.player || bet.team)}? Tell me the stat and line, like <i>${esc(bet.player || '')} over 20.5 points −115</i>, or ask <i>best bet for ${esc(bet.player || '')}</i>.`);
  if (bet.event) parts.push(`Next game: <b>${esc(bet.event)}</b>, ${when(bet.start)}.`);
  else if (bet.error) parts.push(esc(bet.error));
  const inj = injuryAnswer(bet);
  if (inj && !bet.error) parts.push(inj);
  const f = formAnswer(bet, false);
  if (f) parts.push(f);
  parts.push(`Ask about a bet, like <i>${esc((bet.player || bet.team || '').split(' ').slice(-1)[0])} ${bet.kind === 'player' ? 'over 20.5 points' : '−4.5'}</i>.`);
  return parts.join('<br>');
}

// ---- lists of bets: best value, most likely to win, both agree
const FRESH_MINUTES = 45;              // older scans are refreshed (free) before answering
const POPULAR_LEAGUES = ['basketball_nba', 'icehockey_nhl', 'americanfootball_nfl', 'soccer_epl'];
const LEAGUE_NAME = { basketball_nba: 'NBA', icehockey_nhl: 'NHL', americanfootball_nfl: 'NFL', baseball_mlb: 'MLB',
  basketball_ncaab: 'college basketball', americanfootball_ncaaf: 'college football', soccer_epl: 'EPL' };
const leagueName = k => LEAGUE_NAME[k] || ((state.sports.find(s => s.key === k) || {}).title) || k;

// Free main-line odds for these leagues (TheRundown), added to the page like a normal scan.
async function chatScan(sports) {
  const p = new URLSearchParams({ book: state.book.key, sports: sports.join(','), props: 0, min_books: 1, source: 'therundown' });
  const res = await getJSON('/api/scan?' + p);
  const at = Date.now();
  const failed = new Set(res.failed || []);                   // a failed league keeps its last odds
  sports.filter(k => !failed.has(k)).forEach(k => state.results[k] = { rows: [], at });
  res.rows.filter(r => !failed.has(r.sport_key)).forEach(r => (state.results[r.sport_key] ||= { rows: [], at }).rows.push(r));
  state.source = res.source;
  showCredits(res.quota, res.rundown_usage);
  renderAll(false);
  scheduleTrackLog();
  return res;
}

// Stats opinions (and injury news) for these bets, waiting briefly while models load.
async function ensureStats(rows) {
  for (let tries = 0; tries < 4; tries++) {
    const need = rows.filter(r => !(statKey(r) in STATS.known));
    if (!need.length) break;
    const items = need.map(r => ({ key: statKey(r), sport_key: r.sport_key, market: r.market, outcome: r.outcome,
      point: r.point, home: r.home_team, away: r.away_team, team: r.team_total_for, player: r.player, fair_prob: r.fair_prob }));
    try {
      const res = await (await fetch('/api/stats', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }) })).json();
      Object.assign(STATS.known, res.stats || {});
      if (!res.pending) break;
    } catch (e) { break; }
    await new Promise(r => setTimeout(r, 2500));
  }
  try { await loadInjuries(); } catch (e) { /* optional */ }
}

function inWindow(r, window) {
  const t = new Date(r.start), now = new Date();
  if (window === 'today') { const end = new Date(now); end.setDate(end.getDate() + 1); end.setHours(5, 0, 0, 0); return t < end; }
  if (window === 'tomorrow') {
    const a = new Date(now); a.setDate(a.getDate() + 1); a.setHours(5, 0, 0, 0);
    const b = new Date(a); b.setDate(b.getDate() + 1);
    return t >= a && t < b;
  }
  return true;
}

// ---- plain-language summaries: what the numbers mean, without percentages
function chanceWords(c, verb = 'win') {
  return c >= 0.75 ? `very likely to ${verb}` : c >= 0.6 ? `more likely to ${verb} than not` : c >= 0.53 ? 'a slight favorite'
    : c >= 0.47 ? 'basically a coin flip' : c >= 0.4 ? 'a slight underdog' : c >= 0.25 ? 'an underdog' : 'a long shot';
}
function priceWords(r) {
  if (r.ev == null) return null;
  return r.ev > 0.03 ? 'FanDuel is paying more than it\'s worth: a good price'
    : r.ev > 0 ? 'FanDuel\'s price is a little generous'
    : r.ev > -0.02 ? 'the price is about fair, so there\'s no real edge'
    : 'FanDuel\'s price is too low for those chances, so it isn\'t worth it';
}
// "Jokic", "Jackson" (not "Jr."): sports talk uses last names
function firstName(r) {
  const w = (r.player || '').split(' ').filter(x => !/^(jr\.?|sr\.?|ii|iii|iv)$/i.test(x));
  return w[w.length - 1] || r.player;
}

// One or two plain sentences: what's expected, whether the price is worth it, how much to trust it.
function plainSummary(r, short) {
  const s = statOf(r), parts = [];
  const name = r.player ? firstName(r) : null;
  if (r.player && s && s.expected_stat != null && r.point != null) {
    const diff = s.expected_stat - r.point;
    const vs = Math.abs(diff) < 0.3 ? `right around the ${r.point} line` : diff > 0 ? `more than the ${r.point} line` : `less than the ${r.point} line`;
    const lean = r.outcome === 'Over' ? (diff > 0 ? ', which favors the over' : diff < 0 ? ', which works against the over' : '')
      : r.outcome === 'Under' ? (diff < 0 ? ', which favors the under' : diff > 0 ? ', which works against the under' : '') : '';
    parts.push(`${name} is expected to get about ${s.expected_stat} ${s.stat_name}, ${vs}${lean}.`);
  } else {
    const verb = /spreads/.test(r.market) ? 'cover' : /totals|player_/.test(r.market) ? 'hit' : 'win';
    parts.push(`The bot thinks ${betLabel(r)} is ${chanceWords(chance(r), verb)}.`);   // "Knicks -4.5 is more likely to cover than not"
  }
  const price = priceWords(r);
  if (price) parts.push(price.charAt(0).toUpperCase() + price.slice(1) + '.');
  else parts.push('Tell me the odds and I\'ll say whether the price is worth it.');
  if (short) return parts.join(' ');
  const inj = (injOf(r) || {}).player;
  if (inj) parts.push(inj.level === 'out' ? `Heads-up: ${inj.name} is listed out.` : `Heads-up: ${inj.name} is ${inj.status.toLowerCase()}, so check before betting.`);
  if (r.stats_only) parts.push('There\'s no market price for this one, so it\'s based on stats alone: a rough read.');
  else {
    const c = confidence(r);
    parts.push(c.level === 'high' ? 'The numbers line up well, so this is a fairly confident read.'
      : c.level === 'low' ? 'Treat it as a rough estimate: there isn\'t much to compare it with.' : '');
  }
  return parts.filter(Boolean).join(' ');
}

// Why a bet is on the list, in one line: what the stats expect vs the line, the fair price,
// and what keeps confidence down.
function pickReason(r) {
  const s = statOf(r), bits = [];
  if (s && s.expected_stat != null && r.point != null) {
    const dir = s.expected_stat > r.point ? 'over' : s.expected_stat < r.point ? 'under' : 'right at';
    bits.push(`stats expect ${s.expected_stat} ${s.stat_name}, ${dir} the ${r.point} line`);
  } else if (s && s.prob != null) {
    bits.push(`stats give it ${pct(s.prob, 0)}`);
  }
  if (r.fair_prob) bits.push(`fair price ${americanOdds(1 / r.fair_prob)} vs FanDuel ${r.american}`);
  const inj = (injOf(r) || {}).player;
  if (inj) bits.push(`⚠ ${inj.name} ${inj.status.toLowerCase()}`);
  const c = confidence(r);
  if (c.level === 'low') {
    const why = /only \d+ sportsbook|thinner markets|estimated|disagree|injury/.exec(c.why);
    bits.push(`low confidence${why ? `: ${why[0].replace('player props are thinner markets', 'thin prop market').replace(/only (\d+) sportsbooks?.*/, 'only $1 books to compare')}` : ''}`);
  }
  return bits.join(' · ');
}

function pickItem(r, i, mode, listId, number) {
  const poor = mode === 'likely' && r.ev < 0 ? ` <span class="poorprice">⚠ Poor price</span>` : '';
  const solid = r.ev > 0 && confidence(r).level !== 'low' ? ' <span class="solid" title="Positive value and medium or high confidence">✓</span>' : '';
  return `<button type="button" class="cpick" data-l="${listId}" data-i="${i}"><b>${number ? `${number}. ` : ''}${esc(betLabel(r))}</b> ${esc(r.american)}${solid}${poor}
    <span class="plain small">${esc(plainSummary(r, true))}</span>
    <span class="muted small">${esc(r.event)}, ${when(r.start)} · ${pct(chance(r), 0)} chance · <span class="${r.ev > 0 ? 'up' : 'down'}">${signed(r.ev)}</span> value · ${confidence(r).label}</span>
    <span class="why small">${esc(pickReason(r))}</span></button>`;
}

// Show part of a list, remembering the rest for "show more".
function keepList(rows) {
  const id = CHAT.nextList++;
  CHAT.lists[id] = rows;
  return id;
}
function showList(head, rows, mode, foot, chips, size) {
  size = size || 5;
  CHAT.list = { rows, mode, offset: size, size };
  const page = rows.slice(0, size), id = keepList(page);
  const more = rows.length > size ? `<br><span class="muted small">${rows.length - size} more: say <i>show more</i>.</span>` : '';
  botSay(`${head}<div class="cpicks">${page.map((r, i) => pickItem(r, i, mode, id, i + 1)).join('')}</div>${foot || ''}${more}`,
    chips || (rows.length > size ? ['Show more', 'Safest bets tonight', 'Check a bet'] : ['Safest bets tonight', 'Check a bet']));
}

function moreAnswer() {
  const L = CHAT.list;
  if (!L || L.offset >= L.rows.length) return botSay('That\'s everything in the last list. Ask for another league, or <i>safest bets tonight</i>.', CHIPS_START);
  const size = L.size || 5, start = L.offset;
  const page = L.rows.slice(start, start + size), id = keepList(page);
  L.offset += size;
  const left = L.rows.length - L.offset;
  botSay(`<div class="cpicks">${page.map((r, i) => pickItem(r, i, L.mode, id, start + i + 1)).join('')}</div>${left > 0 ? `<span class="muted small">${left} more: say <i>show more</i>.</span>` : '<span class="muted small">That\'s all of them.</span>'}`,
    left > 0 ? ['Show more', 'Check a bet'] : ['Safest bets tonight', 'Check a bet']);
}

const nameKey = n => (n || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z ]/g, '').replace(/ (jr|sr|ii|iii|iv)$/, '').trim();
const sameTeam = (a, b) => !!a && !!b && (a.toLowerCase() === b.toLowerCase() || a.toLowerCase().split(' ').pop() === b.toLowerCase().split(' ').pop());

async function bestAnswer(p) {
  const now = new Date().toISOString();
  // which leagues: the ones asked for, else what's scanned, else popular ones in season
  const scanned = Object.keys(state.results).filter(k => state.results[k].rows.some(r => r.start > now));
  let leagues = p.sports && p.sports.length ? p.sports : scanned.length ? scanned
    : POPULAR_LEAGUES.filter(k => !state.cfg.rundown_sports || state.cfg.rundown_sports.includes(k));
  if (p.event_id) leagues = [...new Set(scannedRows().filter(r => r.event_id === p.event_id).map(r => r.sport_key))];
  const scope = p.scope && (p.scope.team || p.scope.player) ? p.scope : null;
  if (scope && scope.sport_key) leagues = [scope.sport_key];
  // refresh old or missing main lines for free, so answers use today's prices
  const pointsLow = state.rundownUsage && state.rundownUsage.remaining != null && state.rundownUsage.remaining < 3000;
  const asked = new Set((p.sports || []).concat(scope && scope.sport_key ? [scope.sport_key] : []));
  const stale = (p.props || (scope && scope.player) || pointsLow) ? []
    : leagues.filter(k => (!state.results[k] || Date.now() - state.results[k].at > FRESH_MINUTES * 60000)
        && (asked.has(k) || !/ncaa/.test(k)));                 // college leagues cost the most points: only when asked
  let refreshed = false;
  if (stale.length && state.cfg.rundown && !state.cfg.demo && !p.event_id) {
    const wait = addMsg('bot typing', `Getting fresh ${stale.map(leagueName).join(', ')} odds (free)…`);
    try {
      const sup = stale.filter(k => (state.cfg.rundown_sports || []).includes(k));
      if (sup.length) { await chatScan(sup); refreshed = true; }
    } catch (e) { /* use what's there */ }
    wait.remove();
  }
  let rows = (p.here && state.shown && state.shown.length ? state.shown : scannedRows())
    .filter(r => r.start > now && leagues.includes(r.sport_key) && inWindow(r, p.window));
  if (p.event_id) rows = rows.filter(r => r.event_id === p.event_id);
  let what = `${leagues.length && leagues.length <= 3 ? leagues.map(leagueName).join(', ') + ' ' : ''}${p.props ? 'player props' : 'bets'}${p.window === 'today' ? ' tonight' : p.window === 'tomorrow' ? ' tomorrow' : ''}`;
  if (scope && scope.player) {
    const k = nameKey(scope.player);
    rows = rows.filter(r => r.player && nameKey(r.player) === k);
    what = `${scope.player} props`;
    if (!rows.length) {
      const f = formAnswer(Object.assign({ kind: 'player' }, scope), true);
      return botSay(`I don't have ${esc(scope.player)}'s props scanned${scope.event ? ` for ${esc(scope.event)}, ${when(scope.start)}` : ''}, so I can't say which one is best yet. Props come from The Odds API (about 4 credits for the game): open <b>More bets</b>, pick that game and its player props, then ask again.` +
        (f ? `<br>${f}` : '') + `<br>Or give me a line to check now, like <i>${esc(scope.player)} over 20.5 points −115</i>.`, ['Safest bets tonight', 'Check a bet']);
    }
  } else if (scope && scope.team) {
    rows = rows.filter(r => scope.event_id
      ? r.event_id === scope.event_id || (sameTeam(r.home_team, scope.home) && sameTeam(r.away_team, scope.away))
      : sameTeam(r.home_team, scope.team) || sameTeam(r.away_team, scope.team));
    what = scope.event ? `bets in ${scope.event}` : `${scope.team} bets`;
    if (!rows.length) return botSay(scope.error ? esc(scope.error) : `I don't have odds for ${esc(scope.event || scope.team + "'s next game")} yet. Scan its league in Find bets, then ask again.`, ['Who plays tonight?', 'Check a bet']);
  }
  if (p.props) {
    rows = rows.filter(r => r.player);
    if (!rows.length) {
      return botSay(`I don't have scanned ${what} yet. Player props come from The Odds API (about 4 credits a game), so I don't fetch them on my own: tick <b>Player props</b> and press Scan in Find bets, or use <b>More bets</b> for one game. Meanwhile I can show game lines.`,
        [`Best ${leagues.map(leagueName)[0] || ''} game lines`.replace('  ', ' '), 'Safest bets tonight']);
    }
  }
  if (!rows.length) {
    return botSay(state.cfg.rundown
      ? `I couldn't find any upcoming ${what}. ${p.window ? 'Try without "tonight" or "tomorrow", or ' : 'Try '}another league.`
      : `I don't have scanned ${what}. Press Scan in Find bets, then ask me again.`, CHIPS_START);
  }
  await ensureStats(rows);
  const mode = p.mode || 'value';
  const picked = Calc.pickSides(rows, mode === 'likely' ? 'likely' : 'value', chance);
  const latest = Math.max(...leagues.map(k => (state.results[k] || {}).at || 0));
  const fresh = refreshed ? 'Fresh odds just now' : latest ? `Odds checked ${ago(latest)}` : '';
  const freeOnly = picked.every(r => r.books_used.length <= 2);
  const foot = `<span class="muted small">${fresh}${freeOnly ? ' · free odds: FanDuel compared with DraftKings and BetMGM only, so confidence is lower' : ''}. Tap a bet for full details. Even good bets lose often.</span>`;

  // the same question again, with nothing new: say so and show something different
  const key = JSON.stringify([leagues, p.props, p.window, mode, p.event_id || '', p.one]);
  const again = CHAT.lastAsk && CHAT.lastAsk.key === key && !refreshed && Date.now() - CHAT.lastAsk.at < 15 * 60000;
  CHAT.lastAsk = { key, at: Date.now() };
  const same = again ? `<span class="muted small">Same as when you asked a moment ago: no new odds since then.</span><br>` : '';

  if (mode === 'closest') {
    return showList(`${same}<b>Closest to a fair price</b> (${what}): the smallest disadvantage first. ${picked.some(r => r.ev > 0) ? 'The top ones have real value.' : 'None has value right now.'}`,
      picked.sort((a, b) => b.ev - a.ev), 'value', foot);
  }
  if (mode === 'likely') {
    const list = picked.sort((a, b) => chance(b) - chance(a));
    return showList(`${same}<b>Most likely to win</b> (${what}): the side the bot thinks will win, highest chance first. <span class="muted small">⚠ Poor price means likely, but FanDuel charges more than it's worth.</span>`, list, 'likely', foot,
      ['Show more', 'Best value instead', 'Check a bet'], p.count);
  }
  if (mode === 'agree') {
    const list = picked.filter(r => r.agree_sides).sort((a, b) => b.ev - a.ev);
    if (!list.length) return botSay(`No ${what} where the best-value side is also the likelier side right now.`, ['Safest bets tonight', 'Best value instead']);
    return showList(`${same}<b>Best value and most likely agree</b> (${what}): the side that's both the better deal and the likelier winner, best value first.`, list, 'agree', foot);
  }
  const good = picked.filter(r => r.ev > 0).sort((a, b) => b.ev - a.ev);
  const solid = good.filter(qualifies);
  if (p.one && good.length) {
    const top = solid[0] || good[0];
    CHAT.last = top;
    CHAT.ctx = { kind: top.player ? 'player' : 'team', sport_key: top.sport_key, player: top.player || undefined,
      team: top.pick_team || top.home_team, opponent: top.pick_team ? (top.pick_team === top.home_team ? top.away_team : top.home_team) : top.away_team,
      market: top.market.replace(/^alternate_/, ''), outcome: top.outcome, point: top.point != null ? top.point : undefined, event_id: top.event_id };
    const others = good.filter(r => r !== top);
    showList(`${same}If you bet one thing, I'd take the <b>first one below</b>: ${esc(explain(top).verdict[2])} ${confidence(top).label} confidence.${others.length ? ' The others are the next best options.' : ''}`,
      [top, ...others], 'value', foot, ['Why?', 'How much should I bet?', 'Show more']);
    return;
  }
  const size = p.count || 5;
  if (good.length) {
    // solid picks (positive value, medium+ confidence) first, then the low-confidence ones, each best value first
    const ranked = solid.concat(good.filter(r => !solid.includes(r)));
    const head = solid.length === good.length
      ? `<b>${good.length} ${esc(what)} beat FanDuel's price right now</b>, all with medium or high confidence. Best first:`
      : solid.length
        ? `<b>${good.length} ${esc(what)} beat FanDuel's price right now.</b> The ${solid.length === 1 ? 'first one is' : `first ${solid.length} are`} the most solid (✓: medium or high confidence); the rest are leans with low confidence. Best first:`
        : `<b>${good.length} ${esc(what)} beat FanDuel's price right now</b>, all with low confidence${freeOnly ? ' (only 2 books to compare with on the free odds)' : ' (thin markets or few books to compare)'}, so treat them as leans. Best first:`;
    return showList(`${same}${head}`, ranked, 'value', foot, null, size);
  }
  // nothing has value: explain, then show something useful instead of the same list again
  const likely = picked.slice().sort((a, b) => chance(b) - chance(a));
  const why = `<b>Nothing in ${esc(what)} beats the market right now</b> (${picked.length} checked). That's normal: FanDuel's prices are usually fair, and value tends to appear closer to game time or in player props${freeOnly ? '. On the free odds I can only compare with DraftKings and BetMGM, which makes edges harder to spot' : ''}.`;
  if (again) {
    const near = picked.slice().sort((a, b) => b.ev - a.ev);
    return showList(`Still nothing new since I last checked (${fresh.toLowerCase()}). Here are the <b>closest to a fair price</b> instead: the smallest disadvantage, not value.`, near, 'value', foot,
      ['Safest bets tonight', 'Show more', 'Check a bet']);
  }
  showList(`${why}<br>If you still want to bet, here's what the bot thinks is <b>most likely to win</b>:`, likely, 'likely', foot,
    ['Show more', 'Closest to fair price', 'Check a bet']);
}

function recordAnswer() {
  if (!WAGERS.list.length) return 'You haven\'t logged any wagers yet. Add them in <b>My wagers</b> and I can tell you how you\'re doing.';
  const all = Calc.wagerSummary(WAGERS.list);
  const d = new Date(); d.setDate(d.getDate() - 29);
  const recent = Calc.wagerSummary(WAGERS.list.filter(w => w.date >= d.toISOString().slice(0, 10)));
  const line = (s, name) => s.count ? `<b>${name}:</b> ${s.won}-${s.lost}${s.push ? `-${s.push}` : ''}, <span class="${s.net >= 0 ? 'up' : 'down'}">${signedDollars(s.net)}</span> on ${dollars(s.wagered)} wagered (${signed(s.roi, 1)} return)` : `<b>${name}:</b> no settled bets`;
  return [line(recent, 'Last 30 days'), line(all, 'All time'),
    all.streak ? `Current streak: ${all.streak.result === 'won' ? 'won' : 'lost'} ${all.streak.length}.` : '',
    all.pending ? `${all.pending} pending, ${dollars(all.atStake)} at stake.` : ''].filter(Boolean).join('<br>');
}

function bankrollAnswer() {
  if (!bankrollSet()) return 'Your bankroll isn\'t set up yet. Open <b>My wagers → Set up bankroll</b> to get a balance, units and suggested stakes.';
  const b = WAGERS.bankroll, bal = Calc.balance(b, WAGERS.list), u = unitSize(b);
  const pending = WAGERS.list.filter(w => w.result === 'pending').reduce((a, w) => a + w.wagered, 0);
  return `Balance <b>${dollars(bal)}</b> (started at ${dollars(b.start)}, ${bal - b.start >= 0 ? '+' : '−'}${Math.abs((bal - b.start) / u).toFixed(1)} units). ` +
    `${dollars(pending)} is waiting on pending bets. Suggested stakes use ${KELLY_WORD[b.kelly] || `${b.kelly}× Kelly`}, never more than ${b.cap_pct}% of your balance (${dollars(Math.round(bal * b.cap_pct) / 100)} right now).`;
}

function picksAnswer() {
  const open = PICKS.list.filter(p => p.status === 'pending');
  if (!PICKS.list.length) return 'You haven\'t saved any picks yet. Tap ☆ on a bet in Find bets to save it.';
  const s = Calc.summarize(PICKS.list);
  return `${open.length} unsettled pick${open.length === 1 ? '' : 's'}${open.length ? `: ${open.slice(0, 5).map(p => esc(p.label || p.selection)).join(', ')}${open.length > 5 ? '…' : ''}` : ''}.<br>` +
    (s.wins + s.losses ? `Settled: ${s.wins}-${s.losses}${s.pushes ? `-${s.pushes}` : ''} (${pct(s.winRate, 0)}; the bot expected ${pct(s.expectedRate, 0)}).` : 'None settled yet.');
}

async function botAnswer() {
  try {
    const r = await fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'summary', since: botSince(), view: 'side' }) });
    const s = (await r.json()).summary;
    const a = s.all, v = s.value, clv = (s.clv || []).find(x => x.bets);
    const luck = b => b.profit_lo == null ? 'too few to tell' : b.profit_lo > 0 ? 'more than luck' : b.profit_hi < 0 ? 'losing by more than luck' : 'too early to tell';
    return [`<b>Bot's picks (better-value side):</b> ${a.wins}-${a.losses} (${a.win_rate != null ? pct(a.win_rate, 0) : '–'}); it expected ${a.expected != null ? pct(a.expected, 0) : '–'}, so its chances are ${a.expected != null && Math.abs(a.win_rate - a.expected) < 0.04 ? 'about right' : 'off so far'}.`,
      v.bets ? `<b>Good-value bets:</b> ${v.wins}-${v.losses}, <span class="${v.profit >= 0 ? 'up' : 'down'}">${signedDollars(v.profit)}</span> at $100 each: ${luck(v)}.` : 'No good-value bets have finished yet.',
      clv ? `<b>Beat the closing price:</b> ${pct(clv.beat / clv.bets, 0)} of ${clv.bets} (${signed(clv.avg, 1)} average): ${clv.lo == null ? 'too few to tell' : clv.lo > 0 ? 'a real sign of value' : clv.hi < 0 ? 'losing to the close' : 'too early to tell'}.` : 'Closing-price results are still building up.',
      `<span class="muted small">Period: ${esc($('#bRange').selectedOptions[0].textContent.toLowerCase())}. Full details in Bot results.</span>`].join('<br>');
  } catch (e) {
    return 'I couldn\'t load Bot results just now.';
  }
}

// ---- events
restoreChat();
$('#chatAi').onclick = () => { localStorage.setItem('oddfinder.chatAi', aiOn() ? '0' : '1'); renderAiToggle(); };
$('#chatFab').onclick = () => openChat(false);
$('#chatNew').onclick = newChat;
$('#chatClose').onclick = closeChat;
$('#chatFull').onclick = () => $('#chatPanel').classList.toggle('full');
$('#chatForm').addEventListener('submit', e => { e.preventDefault(); chatSend($('#chatInput').value); });
$('#chatChips').addEventListener('click', e => {
  const c = e.target.closest('.chip');
  if (!c) return;
  if (c.dataset.send) return chatSend(c.dataset.send);
  if (c.textContent === 'Bankroll') return chatSend('bankroll');
  if (c.textContent === 'Best value instead') return chatSend('best value bets');
  chatSend(c.textContent);
});
$('#chatLog').addEventListener('click', e => {
  const b = e.target.closest('.cpick');
  const rows = b && CHAT.lists[b.dataset.l];
  if (rows && rows[+b.dataset.i]) openBet(rows[+b.dataset.i]);
});
document.querySelector('.side-item[data-chat]').onclick = () => { closeMenu(); openChat(window.innerWidth > 700); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#chatPanel').hidden && !$('#betModal').open) closeChat(); });

// "Ask about this bet" in a bet's pop-up: open the chat with that bet already checked.
function askAboutBet(r) {
  $('#betModal').close();
  openChat(false);
  CHAT.last = r;
  CHAT.ctx = { kind: r.player ? 'player' : 'team', sport_key: r.sport_key, player: r.player || undefined,
    team: r.pick_team || undefined, opponent: r.pick_team ? (r.pick_team === r.home_team ? r.away_team : r.home_team) : undefined,
    market: r.market.replace(/^alternate_/, ''), outcome: r.outcome, point: r.point != null ? r.point : undefined, event_id: r.event_id };
  if (!r.player && !r.pick_team) Object.assign(CHAT.ctx, { team: r.home_team, opponent: r.away_team });
  addMsg('me', esc(`Check ${betLabel(r)} ${r.american}`));
  const bet = { kind: r.player ? 'player' : 'team', sport_key: r.sport_key, event_id: r.event_id, selection: r.selection,
    market: r.market.replace(/^alternate_/, ''), player: r.player, team: r.pick_team, outcome: r.outcome, point: r.point,
    event: r.event, start: r.start, home: r.home_team, away: r.away_team };
  checkAnswer(bet);
}
