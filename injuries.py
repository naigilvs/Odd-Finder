"""
Injury reports
--------------
ESPN's free injury report for the NBA, NHL and NFL (ESPN has none for soccer).
Each player listed gets a level:

    out       Out, Injured Reserve, Suspension: very unlikely to play
    doubtful  Doubtful
    question  Questionable, Day-To-Day: may or may not play

Players listed as Active (the NFL report includes players who have recovered) are left
out. Reports are downloaded at most every 20 minutes per league and kept in memory only.
They are flags for you to read, not part of the chance of success: they haven't been
tested against past results.
"""
import gzip
import json
import threading
import time
import urllib.request

import props_stats
import stats

PATHS = {"basketball_nba": "basketball/nba", "icehockey_nhl": "hockey/nhl", "americanfootball_nfl": "football/nfl"}
REFRESH_SECONDS = 20 * 60
LEVELS = {"Out": "out", "Injured Reserve": "out", "Suspension": "out", "Doubtful": "doubtful",
          "Questionable": "question", "Day-To-Day": "question"}

_cache = {}                    # sport -> {"at": time, "report": {...}}
_lock = threading.Lock()


def _download(path):
    url = f"{stats.ESPN}/{path}/injuries"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "odd-finder/1.0"})
    with stats._espn_slots:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    return json.loads(body)


def parse(feed):
    """ESPN's report -> {"updated", "players": [{name, key, team, status, level, comment, date, returns}]}."""
    players = []
    for team in feed.get("injuries") or []:
        for i in team.get("injuries") or []:
            level = LEVELS.get(i.get("status"))
            athlete = i.get("athlete") or {}
            name = athlete.get("displayName")
            if not level or not name:
                continue
            details = i.get("details") or {}
            players.append({
                "name": name, "key": props_stats.name_key(name), "team": team.get("displayName") or "",
                "status": i["status"], "level": level, "comment": (i.get("shortComment") or "")[:300],
                "date": i.get("date") or "", "returns": details.get("returnDate") or "",
                "position": (athlete.get("position") or {}).get("abbreviation") or "",
            })
    return {"updated": feed.get("timestamp") or "", "players": players}


def report(sport, now=None):
    """The league's current report (cached), or None if it isn't covered or can't be downloaded."""
    path = PATHS.get(sport)
    if not path:
        return None
    now = now or time.time()
    with _lock:
        hit = _cache.get(sport)
        if hit and now - hit["at"] < REFRESH_SECONDS:
            return hit["report"]
    try:
        rep = parse(_download(path))
    except Exception:
        return hit["report"] if hit else None          # keep the last good report if ESPN hiccups
    with _lock:
        _cache[sport] = {"at": now, "report": rep}
    return rep


def find_player(rep, player, home, away):
    """The injury entry for a player in this game, matched by name and team."""
    key = props_stats.name_key(player)
    for p in rep["players"]:
        if p["key"] == key and any(t and stats._same_team(p["team"], t) for t in (home, away)):
            return p
    return None


def team_list(rep, team):
    """Everyone on a team's report, the most serious first."""
    order = {"out": 0, "doubtful": 1, "question": 2}
    return sorted((p for p in rep["players"] if team and stats._same_team(p["team"], team)),
                  key=lambda p: (order[p["level"]], p["name"]))


def lookup(items, get_report=report):
    """For each bet {key, sport_key, player, home, away}: a player prop gets its player's
    entry ({"player": ...} or {} if not listed); a game bet gets {"teams": ["sport|home",
    "sport|away"]}, keys into `teams`, where each team's list is sent once. Bets in
    leagues without a report get nothing."""
    out, teams, updated, reports = {}, {}, {}, {}
    for it in items[:3000]:
        sport, key = it.get("sport_key"), it.get("key")
        if not isinstance(key, str) or sport not in PATHS:
            continue
        if sport not in reports:
            reports[sport] = get_report(sport)
        rep = reports[sport]
        if not rep:
            continue
        updated[sport] = rep["updated"]
        home, away = str(it.get("home") or ""), str(it.get("away") or "")
        if it.get("player"):
            p = find_player(rep, str(it["player"]), home, away)
            out[key] = {"player": p} if p else {}
        else:
            out[key] = {"teams": [f"{sport}|{home}", f"{sport}|{away}"]}
            for t in (home, away):
                teams.setdefault(f"{sport}|{t}", team_list(rep, t))
    return {"injuries": out, "teams": teams, "updated": updated}
