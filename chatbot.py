"""
Chatbot: understands typed questions about bets, without AI
-----------------------------------------------------------
It recognizes a fixed set of question types and pulls the details out of the text:

    "Jokic over 11.5 rebounds -120"     check a bet (player, stat, Over/Under, line, odds)
    "Knicks -4.5" / "Lakers ML +130"     check a bet (team, spread or moneyline, odds)
    "Celtics Knicks over 224.5"          check a bet (a game's total)
    "Tatum o27.5 pts or Brown o22.5"     compare two bets
    "best NBA props tonight"             best bets from your scans
    "how am I doing" / "bankroll"        your wagers and bankroll
    "is the bot working"                 Bot results
    "what's a push"                      explain a betting term

Players and teams are matched against the box scores and results already saved in data/,
forgiving short names and typos. Answers are put together by the page from the bot's own
numbers; nothing here makes anything up. Questions it doesn't understand are saved (on this
computer only) in data/chat_unknown.json, so new question types can be added later.
"""
import difflib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import props_stats
import stats

UNKNOWN_FILE = stats.DATA_DIR / "chat_unknown.json"
INDEX_SECONDS = 3600            # rebuild the name lists at most hourly

# Stat words -> the market per sport. NBA and NHL both have "points".
STATS = [
    (r"\b(points?|pts|pt)\b", {"basketball_nba": "player_points", "icehockey_nhl": "player_points"}),
    (r"\b(rebounds?|rebs?|boards)\b", {"basketball_nba": "player_rebounds"}),
    (r"\b(assists?|asts?|dimes)\b", {"basketball_nba": "player_assists"}),
    (r"\b(threes?|3s|3pm|3 ?pointers?|three ?pointers?|treys?)\b", {"basketball_nba": "player_threes"}),
    (r"\b(shots?( on goal)?|sog)\b", {"icehockey_nhl": "player_shots_on_goal"}),
    (r"\b(pass(ing)? ?(yards|yds)|pass yd)\b", {"americanfootball_nfl": "player_pass_yds"}),
    (r"\b(rush(ing)? ?(yards|yds)|rush yd)\b", {"americanfootball_nfl": "player_rush_yds"}),
    (r"\b(rec(eiving)? ?(yards|yds)|rec yd)\b", {"americanfootball_nfl": "player_reception_yds"}),
    (r"\b(receptions?|catches|recs)\b", {"americanfootball_nfl": "player_receptions"}),
]
SPORT_WORDS = {
    "basketball_nba": r"\b(nba|basketball)\b", "americanfootball_nfl": r"\b(nfl|football)\b",
    "icehockey_nhl": r"\b(nhl|hockey)\b", "baseball_mlb": r"\b(mlb|baseball)\b",
    "soccer_epl": r"\b(epl|premier league)\b", "soccer_spain_la_liga": r"\bla ?liga\b",
    "soccer_italy_serie_a": r"\bserie a\b", "soccer_germany_bundesliga": r"\bbundesliga\b",
    "soccer_france_ligue_one": r"\bligue ?1\b",
}
SOCCER = [k for k in SPORT_WORDS if k.startswith("soccer_")]

TERMS = {
    "push": "push", "pushes": "push", "vig": "vig", "juice": "vig", "margin": "vig", "hold": "vig",
    "ev": "value", "+ev": "value", "expected value": "value", "value": "value", "edge": "value",
    "kelly": "kelly", "parlay": "parlay", "parlays": "parlay", "spread": "spread", "point spread": "spread",
    "moneyline": "moneyline", "money line": "moneyline", "ml": "moneyline", "over under": "total", "over/under": "total",
    "total": "total", "totals": "total", "clv": "clv", "closing line value": "clv", "closing line": "clv",
    "unit": "units", "units": "units", "american odds": "odds", "odds": "odds", "plus money": "odds",
    "implied probability": "implied", "implied": "implied", "confidence": "confidence",
    "chance of success": "chance", "fair price": "fair", "fair odds": "fair", "prop": "prop", "props": "prop",
    "player prop": "prop", "line": "line", "favorite": "favorite", "underdog": "favorite", "dog": "favorite",
    "sharp": "sharp", "sharp book": "sharp", "pinnacle": "sharp", "bankroll": "bankroll_term",
    "alt line": "alt", "alternate line": "alt", "back to back": "b2b", "back-to-back": "b2b",
}

WORDS_NOT_NAMES = set("""over under the and or vs at to on in of for a an is it this that what whats how much should
i me my bet bets take tonight today tomorrow game games win wins ml moneyline money line points point pts rebounds
assists threes shots goal yards rush rec receiving passing pass receptions catches odds plus minus even pick team total
props prop best top good value check hit will going go does do anyone any who which with more less than about think
yes no ok okay thanks please can you tell show last form injury injured playing play status news vs versus spread
half quarter first second period""".split())

# Common nicknames -> full names (first names that are unique are matched on their own).
NICKNAMES = {"steph": "stephen curry", "kd": "kevin durant", "ad": "anthony davis", "sga": "shai gilgeous-alexander",
             "giannis": "giannis antetokounmpo", "luka": "luka doncic", "bron": "lebron james", "lbj": "lebron james",
             "ant": "anthony edwards", "wemby": "victor wembanyama", "jt": "jayson tatum", "cmc": "christian mccaffrey",
             "jjettas": "justin jefferson", "ovi": "alex ovechkin", "mackinnon": "nathan mackinnon"}

_lock = threading.Lock()
_index = {"at": 0, "players": {}, "teams": {}}


def norm(text):
    """Lower case, plain letters, unicode minus to '-', and keep + - . for numbers."""
    t = stats.unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = text.replace("−", "-").replace("–", "-") if not t else t
    t = re.sub(r"[−–—]", "-", t)
    t = re.sub(r"[^a-z0-9+\-.'/ ]", " ", t)
    return " ".join(t.split())


def _key(name):
    return props_stats.name_key(name)


def _build_index():
    """Players (from saved box scores) and teams (from saved results) by sport, with the
    words people use for them. Only recent seasons count, so retired names don't match."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=400)).isoformat()
    players = {}
    for sport, live in props_stats.LIVE.items():
        latest = {}
        for g in live.store.games.values():
            if g["date"] < recent:
                continue
            for pl in g["players"]:
                k = _key(pl["name"])
                if k not in latest or g["date"] >= latest[k]["date"]:
                    latest[k] = {"name": pl["name"], "team": pl["team"], "date": g["date"], "sport": sport}
        players[sport] = latest
    teams = {}
    for sport, lg in stats._leagues.items():
        if sport == "soccer_uefa_champs_league":
            continue
        last = {}
        for g in lg.store.games():
            if g["date"] < recent:
                continue
            for t in (g["home"], g["away"]):
                last[t] = max(last.get(t, ""), g["date"])
        teams[sport] = last
    with _lock:
        _index.update(at=time.time(), players=players, teams=teams)


def _get_index():
    with _lock:
        fresh = time.time() - _index["at"] < INDEX_SECONDS and _index["players"]
    if not fresh:
        _build_index()
    with _lock:
        return _index["players"], _index["teams"]


def _in_season(teams_by_sport, days=12):
    """Leagues with games in the last `days` days (so "Rangers" means the NHL team in November)."""
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {s for s, ts in teams_by_sport.items() if any(d >= cut for d in ts.values())}


def team_aliases(name, sport):
    n = stats.normalize(name)
    words = n.split()
    out = {n}
    for fc in ("fc ", "afc ", " fc", " afc", "cf "):
        if fc in f" {n} ":
            out.add(" ".join(w for w in words if w not in ("fc", "afc", "cf")))
    if not sport.startswith("soccer_"):
        out.add(words[-1])                                # nickname: "lakers"
        if len(words) >= 3:
            out.add(" ".join(words[-2:]))                 # "trail blazers", "red wings", "maple leafs"
            out.add(" ".join(words[:-2]) if words[-2] in ("trail", "red", "maple", "blue", "golden", "white") else " ".join(words[:-1]))
        else:
            out.add(" ".join(words[:-1]))                 # city: "boston"
    else:
        for w in words:
            if len(w) >= 5 and w not in ("united", "city", "real", "athletic", "sporting", "olympique", "borussia", "atletico", "inter", "racing"):
                out.add(w)                                # "tottenham", "villa", "liverpool"
    return {a for a in out if a}


def find_teams(text, sports=None):
    """Teams named in the text: [(sport, team)], longest names first, without overlaps.
    Ambiguous words (a city with two teams) are left out."""
    _, teams = _get_index()
    live = _in_season(teams)
    alias = {}
    for sport, ts in teams.items():
        if sports and sport not in sports:
            continue
        for team in ts:
            for a in team_aliases(team, sport):
                alias.setdefault(a, set()).add((sport, team))
    padded = f" {text} "
    found, used = [], []
    for a in sorted(alias, key=len, reverse=True):
        i = padded.find(f" {a} ")
        if i < 0 or any(s <= i < e or s < i + len(a) + 1 <= e for s, e in used):
            continue
        options = alias[a]
        if len(options) > 1:
            in_season = {o for o in options if o[0] in live}
            options = in_season if len(in_season) == 1 else options
        if len(options) != 1:
            continue
        (sport, team), = options
        if (sport, team) not in found:
            found.append((sport, team))
        used.append((i, i + len(a) + 1))
    return found


def find_player(text, sports=None, prefer_teams=()):
    """The player named in the text: ({"name", "team", "sport"}, None), or (None, [choices])
    when a last name fits several players, or (None, None). `prefer_teams` settles a shared
    last name ("Brown" next to a Celtics player means Jaylen Brown)."""
    players, teams = _get_index()
    live = _in_season(teams)
    pool = {}
    for sport, ps in players.items():
        if sports and sport not in sports:
            continue
        for k, p in ps.items():
            pool.setdefault(k, []).append(p)
    words = [w for w in re.findall(r"[a-z][a-z'.-]*", text) if w not in WORDS_NOT_NAMES]
    raw = re.findall(r"[a-z][a-z'.-]*", text)
    for w in raw:                                        # nicknames: "steph", "giannis", "sga"
        full = NICKNAMES.get(w)
        if full and _key(full) in pool:
            return sorted(pool[_key(full)], key=lambda p: p["date"], reverse=True)[0], None

    def pick(options):
        options = sorted(options, key=lambda p: (p["sport"] in live, p["date"]), reverse=True)
        return options[0]
    # 1. full name (or first + last) in the text
    for n in (4, 3, 2):
        for i in range(len(raw) - n + 1):
            k = _key(" ".join(raw[i:i + n]))
            if k in pool:
                return pick(pool[k]), None
    # 2. a close spelling of a full name ("jokich nikola", "luka doncich")
    names = list(pool)
    for n in (3, 2):
        for i in range(len(raw) - n + 1):
            chunk = " ".join(raw[i:i + n])
            if len(chunk) < 7:
                continue
            m = difflib.get_close_matches(chunk, names, n=1, cutoff=0.86)
            if m:
                return pick(pool[m[0]]), None
    # 3. last name only ("jokic", "mcdavid"), or a close spelling of one
    by_last = {}
    for k, ps in pool.items():
        by_last.setdefault(k.split()[-1], []).extend(ps)
    for w in words:
        if len(w) < 3:
            continue
        hits = by_last.get(w)
        if not hits and len(w) >= 5:
            m = difflib.get_close_matches(w, list(by_last), n=1, cutoff=0.88)
            hits = by_last.get(m[0]) if m else None
        if hits:
            in_season = [p for p in hits if p["sport"] in live]
            hits = in_season or hits
            if len(hits) > 1 and prefer_teams:
                same = [p for p in hits if any(stats._same_team(p["team"], t) for t in prefer_teams)]
                hits = same if len(same) == 1 else hits
            if len(hits) == 1:
                return hits[0], None
            return None, sorted(hits, key=lambda p: p["date"], reverse=True)[:6]
    # 4. a first name only one current player has ("lebron", "jokic" is above)
    by_first = {}
    for k, ps in pool.items():
        for p in ps:
            if p["sport"] in live and len(k.split()) > 1:
                by_first.setdefault(k.split()[0], []).append(p)
    for w in words:
        hits = by_first.get(w)
        if hits and len(hits) == 1 and len(w) >= 4:
            return hits[0], None
    return None, None


# --------------------------------------------------------------------------- reading a bet

ODDS = re.compile(r"(?<![\d.])([+-]\d{3,4})(?![\d.])")
SIDE_LINE = re.compile(r"\b(over|under|o|u)\s*(\d+(?:\.\d+)?)")
SPREAD = re.compile(r"(?<![\w.])([+-]\d{1,2}(?:\.5|\.0)?)(?![\d.])")


def read_odds(text):
    m = ODDS.search(text)
    if m:
        return int(m.group(1))
    if re.search(r"\b(even|evens|ev money|even money)\b", text):
        return 100
    return None


def american_to_decimal(a):
    return round(1 + a / 100, 4) if a > 0 else round(1 + 100 / -a, 4)


def read_bet(text, sports=None, prefer_teams=()):
    """One bet from a piece of text, or {"problem": ...} when it can't be understood."""
    odds = read_odds(text)
    rest = ODDS.sub(" ", text)
    m = SIDE_LINE.search(rest)
    side, line = (("Over" if m.group(1) in ("over", "o") else "Under"), float(m.group(2))) if m else (None, None)
    stat_markets = next((mk for pat, mk in STATS if re.search(pat, rest)), None)
    bet = {"odds": odds, "decimal": american_to_decimal(odds) if odds else None}
    # a player prop?
    player, choices = find_player(rest, sports or (list(stat_markets) if stat_markets else None), prefer_teams)
    if choices:
        return dict(bet, kind="choose", choices=[f"{p['name']} ({p['team']})" for p in choices])
    if player and (stat_markets or side):
        market = (stat_markets or {}).get(player["sport"])
        if not market:
            return dict(bet, kind="player", player=player["name"], team=player["team"], sport_key=player["sport"],
                        problem=f"I can check {player['name']}'s points, rebounds, assists and the like: say which stat, e.g. \"{player['name'].split()[-1]} over 20.5 points\".")
        if side is None:
            return dict(bet, kind="player", player=player["name"], team=player["team"], sport_key=player["sport"],
                        market=market, problem="Say over or under and the line, e.g. \"over 24.5\".")
        return dict(bet, kind="player", player=player["name"], team=player["team"], sport_key=player["sport"],
                    market=market, outcome=side, point=line)
    teams = find_teams(rest, sports)
    if not teams:
        if player:
            return dict(bet, kind="player", player=player["name"], team=player["team"], sport_key=player["sport"])
        if side:
            return dict(bet, kind="unclear", problem=f"Which game or player is {side.lower()} {line:g} for? "
                        "For example \"Celtics Knicks over 224.5\" or \"Tatum over 27.5 points\".")
        return dict(bet, kind=None)
    sport, team = teams[0]
    other = next((t for s, t in teams[1:] if s == sport), None)
    base = dict(bet, kind="team", sport_key=sport, team=team, opponent=other)
    if side:
        return dict(base, market="totals", outcome=side, point=line)
    sp = SPREAD.search(rest)
    if sp:
        return dict(base, market="spreads", outcome=team, point=float(sp.group(1)))
    if re.search(r"\bpk\b|pick ?em", rest):
        return dict(base, market="spreads", outcome=team, point=0.0)
    if re.search(r"\b(ml|moneyline|money line|to win|win|wins|beat|beats)\b", rest) or odds:
        return dict(base, market="h2h", outcome=team)
    return base                                              # just a team: form, injuries, news


# --------------------------------------------------------------------------- follow-ups

CTX_KEYS = {"kind": str, "sport_key": str, "player": str, "team": str, "opponent": str, "market": str,
            "outcome": str, "point": (int, float), "odds": int, "event_id": str}
PRONOUN = re.compile(r"\b(he|him|his|they|them|their|it|that|this|same|instead|then|also|and)\b")
BARE_LINE = re.compile(r"(?:^|\b(?:line (?:was|is|of|at)|at|to|of|about|if it was|if it were))\s*([+-]?\d{1,3}\.5)(?!\d)")
FLIP = re.compile(r"\b(other side|opposite|flip( it)?|the other team|fade (it|that))\b")


def clean_ctx(ctx):
    """The bet being talked about, as the page sends it back: only known fields, right types."""
    if not isinstance(ctx, dict) or ctx.get("kind") not in ("team", "player"):
        return None
    out = {k: ctx[k] for k, t in CTX_KEYS.items() if isinstance(ctx.get(k), t) and not isinstance(ctx.get(k), bool)}
    return out if out.get("sport_key") and (out.get("player") or out.get("team")) else None


def follow_up(text, ctx):
    """A follow-up about the last bet ("the under", "at -105", "what about 12.5", "his assists",
    "is he playing"): that bet with the changes in the text, or None if the text isn't one."""
    if not ctx:
        return None
    odds = read_odds(text)
    rest = ODDS.sub(" ", text)
    m = SIDE_LINE.search(rest)
    side_only = re.search(r"\b(the )?(under|over)\b(?!\s*\d)", rest)
    bare = None if m else BARE_LINE.search(rest)
    stat_markets = next((mk for pat, mk in STATS if re.search(pat, rest)), None)
    spread = SPREAD.search(rest) if ctx["kind"] == "team" else None
    ml = ctx["kind"] == "team" and re.search(r"\b(ml|moneyline|money line|to win)\b", rest)
    flip = FLIP.search(rest)
    asks = re.search(r"\blast \d+\b|\bform\b|\binjur|\bhurt\b|\bplaying\b|\bstatus\b|\bhealthy\b|\brecent(ly)?\b", rest)
    if not any((odds, m, side_only, bare, stat_markets, spread, ml, flip, asks, PRONOUN.search(rest))):
        return None
    b = {k: ctx.get(k) for k in ("kind", "sport_key", "player", "team", "opponent", "market", "outcome", "point")}
    same_bet = True
    if stat_markets and ctx["kind"] == "player":
        market = stat_markets.get(ctx["sport_key"])
        if not market:
            return dict(b, odds=None, decimal=None, problem="That stat isn't one I can check for this player's sport.")
        if market != ctx.get("market"):
            b.update(market=market, point=None, outcome=b.get("outcome") if b.get("outcome") in ("Over", "Under") else "Over")
            same_bet = False
    if ml:
        b.update(market="h2h", point=None, outcome=b.get("team"))
        same_bet = same_bet and ctx.get("market") == "h2h"
    if flip or (side_only and b.get("outcome") in ("Over", "Under") and not m):
        if b.get("market") in ("spreads", "h2h") and b.get("opponent"):
            b.update(team=ctx.get("opponent"), opponent=ctx.get("team"), outcome=ctx.get("opponent"))
            if b.get("point") is not None:
                b["point"] = -b["point"]
        elif side_only:
            b["outcome"] = "Over" if side_only.group(2) == "over" else "Under"
        else:
            b["outcome"] = "Under" if b.get("outcome") == "Over" else "Over"
        same_bet = same_bet and b["outcome"] == ctx.get("outcome")
    if m:
        b.update(outcome="Over" if m.group(1) in ("over", "o") else "Under", point=float(m.group(2)))
        if b.get("market") in ("spreads", "h2h") and ctx["kind"] == "team":
            b["market"] = "totals"                      # "over 224.5" after a team bet: that game's total
        same_bet = False
    elif spread and b.get("market") != "totals":
        b.update(market="spreads", outcome=b.get("team"), point=float(spread.group(1)))
        same_bet = False
    elif bare:
        val = float(bare.group(1))
        if b.get("market") == "spreads" and not bare.group(1).startswith(("+", "-")) and (ctx.get("point") or 0) < 0:
            val = -val                                  # "what about 3.5" after Knicks -4.5 means -3.5
        b["point"] = val
        same_bet = False
    b["odds"] = odds if odds else (ctx.get("odds") if same_bet else None)
    b["decimal"] = american_to_decimal(b["odds"]) if b["odds"] else None
    if b.get("kind") == "player" and b.get("market") and b.get("point") is None:
        b["problem"] = "Say the line for that, e.g. \"over 6.5\"."
    if b.get("market") is None:
        b.pop("market")
    b["followup"] = True
    return b


# --------------------------------------------------------------------------- what is being asked

def _has(text, *patterns):
    return any(re.search(p, text) for p in patterns)


def parse(message, context=None):
    """What the message asks for, and the details in it. `context` is the bet talked about
    last, so follow-ups like "the under" or "is he playing" work."""
    ctx = clean_ctx(context)
    text = norm(message or "")[:400]
    out = {"text": text, "sports": [s for s, p in SPORT_WORDS.items() if re.search(p, text)]}
    if re.search(r"\bsoccer\b", text):
        out["sports"] = sorted(set(out["sports"]) | set(SOCCER))
    out["props"] = True if re.search(r"\bprops?\b", text) else None
    if not text:
        return dict(out, intent="help")
    if re.fullmatch(r"(check|check a bet|check bet|check my bet|check a pick)\??", text):
        return dict(out, intent="checkhelp")
    if _has(text, r"\b(can|could|will) (you|u) (talk|chat|speak)\b", r"\btalk to me\b", r"\bare (you|u) (there|real|a bot|an ai)\b"):
        return dict(out, intent="talk")
    # a betting term?
    m = re.search(r"\b(what('?s| is| are| does)|explain|define|meaning of|mean)\b", text)
    if m and ODDS.search(text) and not find_teams(text) and not find_player(text)[0]:
        return dict(out, intent="term", term="odds")              # "what does +150 mean"
    if m:
        for phrase in sorted(TERMS, key=len, reverse=True):
            if re.search(rf"(?<![\w+]){re.escape(phrase)}(?!\w)", text) and not SIDE_LINE.search(text):
                return dict(out, intent="term", term=TERMS[phrase])
    if _has(text, r"\bhow (am i|are my|is my)\b", r"\bmy (record|wagers?|bets|results|profit)\b", r"how much (have i|did i) (won|lost|made)"):
        return dict(out, intent="record")
    if _has(text, r"\bbankroll\b", r"\bbalance\b", r"\bmy units\b"):
        return dict(out, intent="bankroll")
    if _has(text, r"\bmy (saved )?picks\b", r"\bsaved picks\b"):
        return dict(out, intent="picks")
    if _has(text, r"\bbot\b.*\b(work|working|good|accurate|results|record|doing)\b", r"\b(is|how) (the )?bot\b",
            r"\bbot results\b", r"\bclv\b", r"\btrack record\b", r"\bhow accurate\b"):
        return dict(out, intent="bot")
    stake = _has(text, r"how much (should|to|do) i (bet|wager|put)", r"\b(stake|bet size|how much on)\b")
    if _has(text, r"^(why|how come|why not|explain( that| it| this)?|reasons?|what makes (it|that))\b\??$"):
        return dict(out, intent="why")                           # the page answers about the last bet, or asks for one
    if ctx:
        if _has(text, r"\b(save|add|keep) (it|that|this)\b", r"\bsave (the )?(bet|pick)\b"):
            return dict(out, intent="save")
        if _has(text, r"\b(this|that|same) game\b") and _has(text, r"\b(best|other|else|any|more|value|bets?|props?)\b"):
            return dict(out, intent="best", event_id=ctx.get("event_id"), here=False)
        cm = re.search(r"\bcompare (?:it|that|this)?\s*(?:with|to|and|vs\.?|against)\s+(.+)", text)
        if cm:
            second = read_bet(cm.group(1), out["sports"] or None, [t for t in (ctx.get("team"), ctx.get("opponent")) if t])
            first = follow_up("that", ctx)
            if second.get("market"):
                return dict(out, intent="compare", bets=[first, second])
    # bets in the text (two joined by "or" / "vs" with lines: compare them)
    parts = [p for p in re.split(r"\s+(?:or|vs\.?|versus)\s+", text) if p.strip()]
    if len(parts) == 2 and all(SIDE_LINE.search(p) or SPREAD.search(p) or ODDS.search(p) or re.search(r"\bml\b", p) for p in parts):
        first = read_bet(parts[0], out["sports"] or None)
        second = read_bet(parts[1], out["sports"] or None, [t for t in (first.get("team"), first.get("opponent")) if t])
        bets = [first, second]
        if all(b.get("market") for b in bets):
            return dict(out, intent="compare", bets=bets)
        if any(b.get("kind") == "choose" for b in bets):
            return dict(out, intent="choose", bets=[b for b in bets if b.get("kind") == "choose"])
    bet = read_bet(text, out["sports"] or None)
    if bet.get("kind") in (None, "unclear"):
        bet = follow_up(text, ctx) or bet
    if bet.get("kind") == "choose":
        return dict(out, intent="choose", bets=[bet])
    if _has(text, r"\binjur", r"\bhurt\b", r"\bis \w+( \w+)? (playing|out|healthy|active)\b", r"\bstatus\b", r"\bplaying (tonight|today)\b"):
        if bet.get("kind"):
            return dict(out, intent="injury", bets=[bet])
    if _has(text, r"\blast \d+\b", r"\bform\b", r"\bhow (has|have|is) \w+( \w+)? (been|doing|playing)\b", r"\brecent(ly)?\b", r"\blog\b"):
        if bet.get("kind"):
            return dict(out, intent="form", bets=[bet])
    if bet.get("followup") and bet.get("kind") == "player" and bet.get("market") and bet.get("point") is None:
        return dict(out, intent="form", bets=[bet])              # "his assists": show them, ask for a line
    if bet.get("market") or bet.get("problem"):
        return dict(out, intent="stake" if stake else "check", bets=[bet])
    if stake:
        return dict(out, intent="stake")
    if _has(text, r"\b(best|top|good|value|recommend|suggest|should i bet|what to bet|any (bets|picks|value))\b", r"\bpicks? (for )?(tonight|today|tomorrow)\b", r"\bbets? (tonight|today|tomorrow)\b",
            r"(?<!my )\bpicks?\b", r"\bany\b.*\b(bets?|props?|games?)\b") or (out["sports"] and not bet.get("kind")):
        return dict(out, intent="best", here=bool(re.search(r"\b(here|these|on screen|this list)\b", text)))
    if bet.get("kind") in ("team", "player"):
        return dict(out, intent="about", bets=[bet])        # just a name: form and injuries
    if _has(text, r"^(hi|hey|hello|yo|sup|help)\b", r"what can you do", r"\bhelp\b"):
        return dict(out, intent="help")
    if _has(text, r"^(thanks|thank you|thx|ty|cheers|cool|nice|great|ok|okay|got it|perfect)\b"):
        return dict(out, intent="thanks")
    return dict(out, intent="unknown")


def log_unknown(message):
    """Keep questions the chatbot didn't understand (last 500), on this computer only."""
    try:
        items = json.loads(UNKNOWN_FILE.read_text())
        items = items if isinstance(items, list) else []
    except (OSError, ValueError):
        items = []
    items.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "text": str(message)[:300]})
    stats.DATA_DIR.mkdir(exist_ok=True)
    UNKNOWN_FILE.write_text(json.dumps(items[-500:], indent=1))


# --------------------------------------------------------------------------- recent form

def player_form(sport, player, market, line=None):
    """A player's recent games for one stat: last 5/10/20 values, averages, how often over
    `line`, and home vs away. None if the stat or player isn't in the saved box scores."""
    cfg = props_stats.SPORTS.get(sport)
    stat = cfg and cfg["markets"].get(market)
    if not stat:
        return None
    key = _key(player)
    games = []
    for g in props_stats.LIVE[sport].store.games.values():
        for pl in g["players"]:
            if _key(pl["name"]) == key and pl.get("min", 0) > 0:
                games.append((g["date"], pl[stat], pl["team"] == g["home"], g["home"] if pl["team"] != g["home"] else g["away"]))
    if not games:
        return None
    games.sort(reverse=True)
    vals = [v for _, v, _, _ in games]

    def block(n):
        xs = vals[:n]
        out = {"games": len(xs), "avg": round(sum(xs) / len(xs), 1)}
        if line is not None:
            out["over"] = sum(1 for x in xs if x > line)
        return out
    home = [v for _, v, h, _ in games[:20] if h]
    away = [v for _, v, h, _ in games[:20] if not h]
    return {"stat": cfg["labels"][stat], "line": line, "last5": block(5), "last10": block(10), "last20": block(20),
            "recent": [{"date": d[:10], "value": v, "home": h, "opp": o} for d, v, h, o in games[:10]],
            "home_avg": round(sum(home) / len(home), 1) if home else None,
            "away_avg": round(sum(away) / len(away), 1) if away else None}


def team_form(sport, team):
    """A team's last 10 results: record, average score for and against, and the games."""
    lg = stats._leagues.get(sport)
    if not lg:
        return None
    games = [g for g in lg.store.games() if stats._same_team(g["home"], team) or stats._same_team(g["away"], team)]
    games = sorted(games, key=lambda g: g["date"], reverse=True)[:10]
    if not games:
        return None
    rows, w, l, d = [], 0, 0, 0
    pf = pa = 0.0
    for g in games:
        home = stats._same_team(g["home"], team)
        us, them = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
        res = "W" if us > them else "L" if us < them else "D"
        w, l, d = w + (res == "W"), l + (res == "L"), d + (res == "D")
        pf, pa = pf + us, pa + them
        rows.append({"date": g["date"][:10], "opp": g["away"] if home else g["home"], "home": home,
                     "score": f"{us:g}-{them:g}", "result": res})
    return {"record": f"{w}-{l}" + (f"-{d}" if d else ""), "for": round(pf / len(games), 1),
            "against": round(pa / len(games), 1), "games": rows}
