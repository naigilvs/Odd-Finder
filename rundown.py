"""
TheRundown (free plan): main-line odds without using Odds API credits
--------------------------------------------------------------------
TheRundown's free plan gives full-game moneyline, spread and total odds from three
sportsbooks (FanDuel, DraftKings, BetMGM), up to 5 minutes old, with 20,000 "data
points" a day (about 20 per game). No player props or alternate lines.

Its games are converted into the same shape as The Odds API's, so the rest of the bot
values them the same way. Each game is matched to The Odds API's game list (a free
call, no credits) by teams and start time, so a game keeps the same id, team names and
logos whichever source scanned it, and player props can still be added from The Odds API.

The key goes in config.json as "therundown_key". It is only sent to therundown.io, in the
X-TheRundown-Key header.
"""
import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import stats

BASE = "https://therundown.io/api/v2"
CACHE_SECONDS = 5 * 60

# Odds API league key -> TheRundown sport id (leagues the free plan covers that the bot uses)
SPORT_IDS = {
    "americanfootball_nfl": 2, "americanfootball_ncaaf": 1, "basketball_nba": 4, "basketball_ncaab": 5,
    "basketball_wnba": 8, "icehockey_nhl": 6, "baseball_mlb": 3, "mma_mixed_martial_arts": 7,
    "soccer_usa_mls": 10, "soccer_epl": 11, "soccer_france_ligue_one": 12, "soccer_germany_bundesliga": 13,
    "soccer_spain_la_liga": 14, "soccer_italy_serie_a": 15, "soccer_uefa_champs_league": 16,
    "soccer_uefa_europa_league": 33, "soccer_mexico_ligamx": 34,
}
# Days ahead to load: football is weekly, soccer plays across the weekend, the rest most days.
DAYS_AHEAD = {"americanfootball_nfl": 7, "americanfootball_ncaaf": 7}
DAYS_AHEAD.update({k: 5 for k in SPORT_IDS if k.startswith("soccer_")})
DEFAULT_DAYS = 3

# TheRundown sportsbook id -> (Odds API key, name)
BOOKS = {23: ("fanduel", "FanDuel"), 19: ("draftkings", "DraftKings"), 22: ("betmgm", "BetMGM")}
MARKETS = {1: "h2h", 2: "spreads", 3: "totals"}

# Names that differ from The Odds API's, used when a game can't be matched to its list.
NAME_FIXES = {"LA Clippers": "Los Angeles Clippers", "Portland Trailblazers": "Portland Trail Blazers"}

_rate_lock = threading.Lock()          # free plan: 1 request a second
_last_request = [0.0]
_cache = {}
_cache_lock = threading.Lock()
usage = {"remaining": None, "limit": None, "reset": None}


class RundownError(Exception):
    pass


def _get(key, path, **params):
    url = f"{BASE}/{path}?" + "&".join(f"{k}={v}" for k, v in params.items())
    with _cache_lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    req = urllib.request.Request(url, headers={"X-TheRundown-Key": key, "Accept-Encoding": "gzip",
                                               "User-Agent": "odd-finder/1.0"})
    for attempt in range(3):
        with _rate_lock:                               # space requests 1.1 s apart, across threads
            wait = _last_request[0] + 1.1 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                if resp.headers.get("x-datapoints-remaining") is not None:
                    usage.update(remaining=int(resp.headers["x-datapoints-remaining"]),
                                 limit=int(resp.headers.get("x-datapoints-limit") or 0) or None,
                                 reset=resp.headers.get("x-datapoints-reset"))
                data = json.loads(body)
                break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2)                          # too fast: wait and try again
                continue
            if e.code == 429:
                raise RundownError("TheRundown's free daily limit is used up; it resets at midnight UTC.")
            if e.code in (401, 403):
                raise RundownError("TheRundown didn't accept the key in config.json.")
            raise RundownError(f"TheRundown said HTTP {e.code}.")
        except (urllib.error.URLError, OSError) as e:
            raise RundownError(f"Could not reach TheRundown: {getattr(e, 'reason', e)}")
    with _cache_lock:
        _cache[url] = (time.time(), data)
    return data


def decimal_odds(american):
    """-110 -> 1.9091, +150 -> 2.5. None for missing or placeholder prices."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return round(1 + a / 100, 4)
    if a <= -100:
        return round(1 + 100 / -a, 4)
    return None


def _team_name(t):
    name = f"{t.get('name', '')} {t.get('mascot', '')}".strip()
    return NAME_FIXES.get(name, name)


def convert(ev, sport_key, sport_title):
    """One TheRundown event -> an Odds API-style event ({id, home_team, bookmakers: [...]}) or None."""
    teams = ev.get("teams") or []
    home = next((t for t in teams if t.get("is_home")), None)
    away = next((t for t in teams if t.get("is_away")), None)
    if not home or not away or not ev.get("event_date"):
        return None
    names = {t["team_id"]: _team_name(t) for t in teams if "team_id" in t}
    books = {}
    for m in ev.get("markets") or []:
        mkey = MARKETS.get(m.get("market_id"))
        if not mkey or m.get("period_id", 0) != 0:
            continue
        for part in m.get("participants") or []:
            pname = names.get(part.get("id"), NAME_FIXES.get(part.get("name"), part.get("name")))
            if mkey == "totals":
                pname = part.get("name")                       # Over / Under
            for line in part.get("lines") or []:
                for bid, pr in (line.get("prices") or {}).items():
                    book = BOOKS.get(int(bid)) if str(bid).isdigit() else None
                    price = decimal_odds(pr.get("price"))
                    if not book or not price or pr.get("is_main_line") is False:
                        continue
                    out = {"name": pname, "price": price}
                    if mkey != "h2h":
                        try:
                            out["point"] = float(line.get("value"))
                        except (TypeError, ValueError):
                            continue
                    b = books.setdefault(book[0], {"key": book[0], "title": book[1], "markets": {},
                                                   "last_update": pr.get("updated_at")})
                    b["markets"].setdefault(mkey, []).append(out)
                    if (pr.get("updated_at") or "") > (b["last_update"] or ""):
                        b["last_update"] = pr["updated_at"]
    bookmakers = [{"key": b["key"], "title": b["title"], "last_update": b["last_update"],
                   "markets": [{"key": k, "outcomes": v} for k, v in b["markets"].items()]} for b in books.values()]
    return {"id": f"rd_{ev['event_id']}", "sport_key": sport_key, "sport_title": sport_title,
            "commence_time": ev["event_date"].replace("+00:00", "Z"),
            "home_team": _team_name(home), "away_team": _team_name(away), "bookmakers": bookmakers,
            "source": "therundown"}


SAME_WORD = {"state": "st", "saint": "st", "univ": "", "university": ""}


def _name(n):
    """A team name for matching: "Iowa State Cyclones" and "Iowa St Cyclones" are the same."""
    words = [SAME_WORD.get(w, w) for w in stats.normalize(n or "").split()]
    n = " ".join(w for w in words if w)
    return stats.ALIASES.get(n, n)


def _exact(a, b):
    """Same team (full name, not just the nickname)."""
    na, nb = _name(a), _name(b)
    return bool(na) and na == nb


def match_ids(events, odds_api_games):
    """Give each converted game The Odds API's id and team names when the same game is on its
    list: start within 3 hours, and one team's full name matches (or, in pro leagues, both
    teams' nicknames). Nicknames alone aren't enough in college sports, where many schools
    are "Wildcats" or "Tigers" and play at the same time. The best match wins; each game once."""
    used = set()
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        best, best_score = None, 0
        for g in odds_api_games:
            if g["id"] in used:
                continue
            gs = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if abs((gs - start).total_seconds()) > 3 * 3600:
                continue
            h, a = g.get("home_team") or "", g.get("away_team") or ""
            # home and away can be listed the other way round for neutral-site games
            exact = max(_exact(ev["home_team"], h) + _exact(ev["away_team"], a),
                        _exact(ev["home_team"], a) + _exact(ev["away_team"], h))
            nick = max(stats._same_team(ev["home_team"], h) + stats._same_team(ev["away_team"], a),
                       stats._same_team(ev["home_team"], a) + stats._same_team(ev["away_team"], h))
            score = 2 * exact + nick
            college = "ncaa" in (ev.get("sport_key") or "")             # nicknames repeat across schools
            if (exact >= 1 or (nick == 2 and not college)) and score > best_score:
                best, best_score = g, score
        if best:
            used.add(best["id"])
            ev.update(id=best["id"], home_team=best.get("home_team"), away_team=best.get("away_team"),
                      commence_time=best["commence_time"], sport_title=best.get("sport_title") or ev["sport_title"])
    return events


def fetch_events(key, sport_key, sport_title, now=None):
    """Upcoming games in one league for the next few days, Odds API-style."""
    sid = SPORT_IDS.get(sport_key)
    if sid is None:
        return None                                    # not on TheRundown's free plan
    now = now or datetime.now(timezone.utc)
    local_today = (now - timedelta(minutes=_offset())).date()
    events = []
    for d in range(DAYS_AHEAD.get(sport_key, DEFAULT_DAYS)):
        events += fetch_day(key, sport_key, sport_title, (local_today + timedelta(days=d)).isoformat())
    return events


def _offset():
    """Minutes this computer's clock is behind UTC (TheRundown's `offset`: which day a game is on)."""
    return -int(datetime.now().astimezone().utcoffset().total_seconds() // 60)


def local_day(iso):
    """The local date (YYYY-MM-DD) of an ISO start time, as TheRundown files games by day."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t - timedelta(minutes=_offset())).date().isoformat()


def fetch_day(key, sport_key, sport_title, day):
    """One league's games on one local day (YYYY-MM-DD), Odds API-style; [] if not covered."""
    sid = SPORT_IDS.get(sport_key)
    if sid is None:
        return []
    data = _get(key, f"sports/{sid}/events/{day}", market_ids="1,2,3", main_line="true", offset=_offset())
    out = []
    for ev in data.get("events") or []:
        c = convert(ev, sport_key, sport_title)
        if c and c["bookmakers"]:
            out.append(c)
    return out
