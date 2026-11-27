#!/usr/bin/env python3
"""
Betting Value Finder
--------------------
Pulls the current odds from a chosen sportsbook (FanDuel for now), estimates the
"fair" win probability of every bet from the consensus of other sportsbooks with
the bookmaker margin (vig) removed, and ranks every bet by expected value:

    EV = fair_probability * decimal_odds - 1

A positive EV means the sportsbook is paying more than the bet's estimated
probability says it should. Odds come from The Odds API (the-odds-api.com).

Run:  python3 app.py            (needs an API key, see README.md)
      python3 app.py --demo     (made-up sample data, no key needed)
"""
import argparse
import json
import math
import re
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import backup
import chatbot
import injuries
import picks
import props_stats
import rundown
import scans
import wagers
import stats

ROOT = Path(__file__).resolve().parent
API_BASE = "https://api.the-odds-api.com/v4"

# Sportsbooks the user can analyze. Add more here later, e.g. "draftkings": "DraftKings".
TARGET_BOOKS = {"fanduel": "FanDuel"}
# Shown on the start screen as not yet available.
COMING_SOON_BOOKS = ["DraftKings", "BetMGM", "Caesars", "Fanatics"]

# Books used to build the "fair probability" consensus. The Odds API bills every
# 10 bookmakers as one region, so this list (plus the target book) costs 1 region.
REFERENCE_BOOKS = [
    "pinnacle", "betfair_ex_eu", "draftkings", "betmgm", "williamhill_us",
    "betrivers", "bovada", "betonlineag", "lowvig",
]
# Low-margin "sharp" books are better at pricing, so their opinion counts more.
SHARP_WEIGHTS = {"pinnacle": 3.0, "betfair_ex_eu": 2.0, "lowvig": 1.5}

FEATURED_MARKETS = ["h2h", "spreads", "totals"]
PROP_MARKETS = {
    "basketball_nba": ["player_points", "player_rebounds", "player_assists", "player_threes"],
    "basketball_ncaab": ["player_points", "player_rebounds", "player_assists"],
    "americanfootball_nfl": ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
    "americanfootball_ncaaf": ["player_pass_yds", "player_rush_yds", "player_reception_yds"],
    "baseball_mlb": ["batter_hits", "batter_total_bases", "pitcher_strikeouts"],
    "icehockey_nhl": ["player_points", "player_shots_on_goal"],
}
MARKET_LABELS = {
    "h2h": "Moneyline", "spreads": "Point spread", "totals": "Total (O/U)",
    "player_points": "Player points", "player_rebounds": "Player rebounds",
    "player_assists": "Player assists", "player_threes": "Player 3-pointers",
    "player_pass_yds": "Passing yards", "player_rush_yds": "Rushing yards",
    "player_reception_yds": "Receiving yards", "player_receptions": "Receptions",
    "batter_hits": "Batter hits", "batter_total_bases": "Total bases",
    "pitcher_strikeouts": "Pitcher strikeouts", "player_shots_on_goal": "Shots on goal",
    "player_anytime_td": "Anytime touchdown", "player_double_double": "Double-double",
    "player_triple_double": "Triple-double", "player_goal_scorer_anytime": "Anytime goal",
    "batter_home_runs": "Home runs",
}
PROP_EVENTS_PER_SPORT = 8      # caps API usage when props are turned on

# ---- "More bets": loaded one game at a time (about 1 credit per bet type per game)
MORE_MAX_GAMES = 10
PERIOD_MARKETS = {   # by sport family (first part of the sport key)
    "basketball": ["h2h_h1", "spreads_h1", "totals_h1", "h2h_q1", "spreads_q1", "totals_q1"],
    "americanfootball": ["h2h_h1", "spreads_h1", "totals_h1", "h2h_q1", "spreads_q1", "totals_q1"],
    "icehockey": ["h2h_p1", "spreads_p1", "totals_p1"],
    "baseball": ["h2h_1st_5_innings", "spreads_1st_5_innings", "totals_1st_5_innings"],
}
YES_NO_MARKETS = {
    "americanfootball_nfl": ["player_anytime_td"], "americanfootball_ncaaf": ["player_anytime_td"],
    "basketball_nba": ["player_double_double", "player_triple_double"],
    "icehockey_nhl": ["player_goal_scorer_anytime"], "baseball_mlb": ["batter_home_runs"],
}
# When books only offer the "Yes" side, the margin can't be removed exactly, so
# assume this much margin instead (and mark the bet as low confidence).
ONE_SIDED_MARGIN = 0.10


def more_groups(sport):
    """Bet-type groups the More bets section can load for a league."""
    family = sport.split("_")[0]
    groups = [
        {"key": "alt", "label": "Alternate lines", "hint": "Spreads and totals at other numbers",
         "markets": ["alternate_spreads", "alternate_totals"]},
        {"key": "team", "label": "Team totals", "hint": "One team's score over or under",
         "markets": ["team_totals", "alternate_team_totals"]},
        {"key": "periods", "label": "Halves, quarters and periods", "hint": "Moneyline, spread and total for part of the game",
         "markets": PERIOD_MARKETS.get(family, [])},
        {"key": "props", "label": "Player props (over/under)", "hint": "Points, yards, strikeouts and more",
         "markets": PROP_MARKETS.get(sport, [])},
        {"key": "yesno", "label": "Yes/no player props", "hint": "Anytime touchdown, double-double, home run…",
         "markets": YES_NO_MARKETS.get(sport, [])},
    ]
    return [g for g in groups if g["markets"]]


PERIOD_NAMES = {"_q1": "1st quarter", "_q2": "2nd quarter", "_q3": "3rd quarter", "_q4": "4th quarter",
                "_h1": "1st half", "_h2": "2nd half", "_p1": "1st period", "_p2": "2nd period",
                "_p3": "3rd period", "_1st_5_innings": "1st 5 innings", "_1st_1_innings": "1st inning"}


def split_market(key):
    """'alternate_spreads' -> ('spreads', True, None); 'totals_h1' -> ('totals', False, '1st half')."""
    alt = key.startswith("alternate_")
    base = key[len("alternate_"):] if alt else key
    for suffix, name in PERIOD_NAMES.items():
        if base.endswith(suffix):
            return base[:-len(suffix)], alt, name
    return base, alt, None


def market_label(key):
    if key in MARKET_LABELS:
        return MARKET_LABELS[key]
    base, alt, period = split_market(key)
    names = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total", "team_totals": "Team total"}
    label = names.get(base) or MARKET_LABELS.get(base) or base.split("_", 1)[-1].replace("_", " ").capitalize()
    if alt:
        label = "Alt " + label.lower()
    if period:
        label = f"{period[0].upper()}{period[1:]} {label.lower()}"
    return label


def is_player_market(key):
    return key.startswith(("player_", "batter_", "pitcher_"))
CACHE_SECONDS = 300
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")   # league keys and game ids from the browser

# Team logos and player photos come from TheSportsDB's free API (key "123").
# The free plan allows ~30 requests a minute, so names are looked up one at a
# time in the background and saved to images.json; each is only looked up once.
SPORTSDB = "https://www.thesportsdb.com/api/v1/json/123"
IMAGE_CACHE_FILE = ROOT / "images.json"
SPORTSDB_SPORT = {"American Football": "American Football", "Basketball": "Basketball",
                  "Ice Hockey": "Ice Hockey", "Baseball": "Baseball", "Soccer": "Soccer",
                  "Mixed Martial Arts": "Fighting", "Boxing": "Fighting"}
RETRY_MISSING_IMAGE_AFTER = 7 * 24 * 3600   # try again for names not found a week ago
SPORTSDB_GAP_SECONDS = 2.5

DEMO = False
API_KEY = None
RUNDOWN_KEY = None     # TheRundown's free key (config.json "therundown_key"): main lines without credits
_cache = {}
_cache_lock = threading.Lock()
_quota = {"remaining": None, "used": None}


# --------------------------------------------------------------------------- odds API

class ApiError(Exception):
    pass


def claim_data(lock_file, port):
    """Only one live copy of the bot may use the data folder at a time; two would overwrite
    each other's saved picks and track record. Returns None once this copy has claimed it,
    or the other copy's port if one is already running."""
    import atexit
    import os
    import socket
    try:
        other = json.loads(lock_file.read_text())
        pid, other_port = int(other["pid"]), int(other["port"])
        if pid != os.getpid():
            try:
                os.kill(pid, 0)                         # is that process still alive?
                alive = True
            except PermissionError:
                alive = True
            except OSError:
                alive = False
            if alive:
                with socket.socket() as sock:           # ...and is it really serving?
                    sock.settimeout(1)
                    if sock.connect_ex(("127.0.0.1", other_port)) == 0:
                        return other_port
    except (OSError, ValueError, KeyError, TypeError):
        pass                                            # no lock, or a leftover one: take over
    lock_file.parent.mkdir(exist_ok=True)
    lock_file.write_text(json.dumps({"pid": os.getpid(), "port": port}))

    def release():
        try:
            if json.loads(lock_file.read_text()).get("pid") == os.getpid():
                lock_file.unlink()
        except (OSError, ValueError):
            pass
    atexit.register(release)
    return None


def load_config():
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def backup_soon():
    """Back up shortly after picks or results change (the backup itself skips unchanged files)."""
    if BACKUP and time.time() - getattr(backup_soon, "last", 0) > 60:
        backup_soon.last = time.time()
        threading.Timer(5, BACKUP.run).start()


def load_api_key():
    key = os.environ.get("ODDS_API_KEY")
    cfg = ROOT / "config.json"
    if not key and cfg.exists():
        key = json.loads(cfg.read_text()).get("odds_api_key")
    return (key or "").strip() or None


def api_get(path, **params):
    params["apiKey"] = API_KEY
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    cache_key = url
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "value-finder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
            if resp.headers.get("x-requests-remaining") is not None:
                _quota["remaining"] = resp.headers.get("x-requests-remaining")
                _quota["used"] = resp.headers.get("x-requests-used")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", str(e))
        except Exception:
            msg = str(e)
        raise ApiError(f"The Odds API said: {msg} (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach The Odds API: {e.reason}")
    with _cache_lock:
        _cache[cache_key] = (time.time(), data)
    return data


def list_sports():
    if DEMO:
        return [{"key": k, "title": t, "group": g} for k, t, g in DEMO_SPORTS]
    sports = api_get("/sports")  # free call
    return [
        {"key": s["key"], "title": s["title"], "group": s["group"]}
        for s in sports
        if s.get("active") and not s.get("has_outrights")
    ]


def fetch_events(sport, target, include_props):
    """Return events for one sport, each with odds from the target + reference books."""
    if DEMO:
        return demo_events(sport, target, include_props)
    books = ",".join([target] + [b for b in REFERENCE_BOOKS if b != target][:9])
    events = api_get(
        f"/sports/{sport}/odds", bookmakers=books,
        markets=",".join(FEATURED_MARKETS), oddsFormat="decimal",
    )
    if include_props:
        add_props(sport, target, events)
    return events


def add_props(sport, target, events):
    """Player props for the next 48 hours' games, from The Odds API (these cost credits)."""
    props = PROP_MARKETS.get(sport)
    if not props:
        return
    books = books_param(target)
    soon = datetime.now(timezone.utc) + timedelta(hours=48)
    upcoming = [e for e in events if parse_time(e["commence_time"]) < soon
                and not e["id"].startswith("rd_")][:PROP_EVENTS_PER_SPORT]   # rd_: not on The Odds API's list
    for ev in upcoming:
        try:
            extra = api_get(
                f"/sports/{sport}/events/{ev['id']}/odds", bookmakers=books,
                markets=",".join(props), oddsFormat="decimal",
            )
        except ApiError:
            continue
        merge_bookmakers(ev, extra.get("bookmakers", []))


def sport_title(sport):
    try:
        return next((s["title"] for s in list_sports() if s["key"] == sport), sport)   # free call, cached
    except ApiError:
        return stats.LEAGUES.get(sport, {}).get("name", sport)


def fetch_events_free(sport, target, include_props):
    """Main lines from TheRundown's free plan (no credits), matched to The Odds API's game list
    (also free) so ids and names stay the same. Props, if asked for, still come from The Odds API."""
    events = rundown.fetch_events(RUNDOWN_KEY, sport, sport_title(sport))
    if events is None:
        raise ApiError("not on TheRundown's free plan. Choose The Odds API above to scan it")
    try:
        rundown.match_ids(events, api_get(f"/sports/{sport}/events"))
    except ApiError:
        pass                                   # no match list: games keep TheRundown's ids
    if include_props:
        add_props(sport, target, events)
    return events


def books_param(target):
    return ",".join([target] + [b for b in REFERENCE_BOOKS if b != target][:9])


def list_games(sport):
    """Upcoming games in one league. Free: The Odds API's events list costs no credits."""
    now = datetime.now(timezone.utc)
    events = demo_events(sport, "fanduel", False) if DEMO else api_get(f"/sports/{sport}/events")
    return [{"id": e["id"], "home": e.get("home_team"), "away": e.get("away_team"), "start": e["commence_time"]}
            for e in events if parse_time(e["commence_time"]) > now]


def run_more_scan(target, sport, event_ids, markets):
    """More bets: load the chosen bet types for the chosen games, one game at a time."""
    now = datetime.now(timezone.utc)
    rows, errors = [], []

    def fetch(eid, mkts):
        return api_get(f"/sports/{sport}/events/{eid}/odds", bookmakers=books_param(target),
                       markets=",".join(mkts), oddsFormat="decimal")

    def one(eid):
        if DEMO:
            try:
                return demo_more_event(sport, eid, markets), None
            except ApiError as e:
                return None, str(e)
        try:
            return fetch(eid, markets), None
        except ApiError as e:
            if "HTTP 422" not in str(e) or len(markets) == 1:
                return None, str(e)
        # A bet type this league doesn't support can reject the whole request (no credits
        # are charged for that), so try each bet type on its own and keep what works.
        event, skipped = None, []
        for m in markets:
            try:
                part = fetch(eid, [m])
            except ApiError:
                skipped.append(market_label(m))
                continue
            if event is None:
                event = part
            else:
                merge_bookmakers(event, part.get("bookmakers", []))
        note = f"Not offered for this game: {', '.join(skipped)}" if skipped else None
        return event, (note if event is None else None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for ev, err in pool.map(one, event_ids):
            if err:
                errors.append(err)
            elif ev and ev.get("bookmakers") and parse_time(ev["commence_time"]) > now:
                rows.extend(analyze_event(ev, target, 1))
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return {"book": TARGET_BOOKS[target], "demo": DEMO, "generated": now.isoformat(),
            "count": len(rows), "rows": rows, "errors": errors, "quota": _quota}


def merge_bookmakers(event, extra_books):
    existing = {b["key"]: b for b in event["bookmakers"]}
    for b in extra_books:
        if b["key"] in existing:
            existing[b["key"]]["markets"].extend(b["markets"])
        else:
            event["bookmakers"].append(b)


# --------------------------------------------------------------------------- logos and photos

class ImageFinder:
    """Finds team logos and player photos, one lookup at a time, and remembers them."""

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.cache = json.loads(IMAGE_CACHE_FILE.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.queue, self.queued = [], set()
        threading.Thread(target=self._work, daemon=True).start()

    def get(self, items):
        """Return the images already known; queue the rest. -> (images, pending count)"""
        found, pending = {}, 0
        with self.lock:
            for it in items[:500]:
                kind, name = it.get("kind"), (it.get("name") or "").strip()
                if kind not in ("team", "player") or not name:
                    continue
                key = f"{kind}|{name}"
                hit = self.cache.get(key)
                if hit and (hit["url"] or time.time() - hit["at"] < RETRY_MISSING_IMAGE_AFTER):
                    found[key] = hit["url"]
                    continue
                if key not in self.queued:
                    self.queued.add(key)
                    self.queue.append((key, kind, name, it.get("group") or ""))
                pending += 1
        return found, pending

    def _work(self):
        while True:
            with self.lock:
                job = self.queue.pop(0) if self.queue else None
            if not job:
                time.sleep(0.5)
                continue
            key, kind, name, group = job
            try:
                url = self._lookup(kind, name, group)
            except Exception as e:
                with self.lock:            # network trouble or rate limit: try again later
                    self.queue.append(job)
                rate_limited = isinstance(e, urllib.error.HTTPError) and e.code == 429
                time.sleep(60 if rate_limited else SPORTSDB_GAP_SECONDS * 5)
                continue
            with self.lock:
                self.cache[key] = {"url": url, "at": time.time()}
                self.queued.discard(key)
                IMAGE_CACHE_FILE.write_text(json.dumps(self.cache, indent=0))
            time.sleep(SPORTSDB_GAP_SECONDS)

    @staticmethod
    def _search(endpoint, **params):
        url = f"{SPORTSDB}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "odd-finder/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)

    def _lookup(self, kind, name, group):
        sport = SPORTSDB_SPORT.get(group)
        if kind == "team":
            results = self._search("searchteams.php", t=name).get("teams") or []
            if not results and name.lower().endswith((" fc", " cf", " sc")):
                results = self._search("searchteams.php", t=name[:-3]).get("teams") or []
            fields = ("strBadge", "strLogo")
        else:
            results = self._search("searchplayers.php", p=name).get("player") or []
            fields = ("strCutout", "strThumb")
        if sport:  # don't show a logo from a different sport with the same name
            results = [r for r in results if r.get("strSport") == sport]
        for r in results:
            for f in fields:
                if r.get(f):
                    return r[f] + "/tiny"
        return None


IMAGES = None
PICKS = picks.PickStore()       # saved picks, in data/picks.json
WAGERS = wagers.WagerStore()    # the bets you actually placed, in data/wagers.json
TRACK = picks.TrackStore()      # every gradable bet the bot showed, in data/track.json
SCANS = scans.ScanStore(stats.DATA_DIR / "scans.json")   # your last scans, kept across restarts
BACKUP = None                   # copies picks and track record to iCloud Drive (not in demo mode)
GITBACKUP = None                # and commits them to a private GitHub repo, if one is set


# --------------------------------------------------------------------------- analysis

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def line_key(market, outcome, home):
    """Identify one betting line so the same line can be matched across books.
    Both sides of a line share a key: home -3.5 and away +3.5, or Over/Under 220.5."""
    point = outcome.get("point")
    if point is None:
        lk = None
    elif outcome["name"] in ("Over", "Under"):
        lk = round(point, 2)
    else:  # spread: express it from the home team's side
        lk = round(point if outcome["name"] == home else -point, 2)
    return (market, outcome.get("description") or "", lk)


def book_lines(bookmaker, home):
    lines = {}
    for m in bookmaker.get("markets", []):
        for o in m.get("outcomes", []):
            if o.get("price", 0) > 1:
                lines.setdefault(line_key(m["key"], o, home), {})[o["name"]] = o
    return lines


def devig(outcomes):
    """Turn one book's prices for every side of a line into fair probabilities."""
    inv = {n: 1.0 / o["price"] for n, o in outcomes.items()}
    total = sum(inv.values())
    return {n: v / total for n, v in inv.items()}


def american(decimal):
    if decimal >= 2:
        return f"+{round((decimal - 1) * 100)}"
    return f"{round(-100 / (decimal - 1))}"


def describe(market, outcome):
    """Full name of the bet, e.g. 'Lakers -3.5', 'J. Smith Over 24.5 points', 'Lakers Over 112.5, 1st half'."""
    name, point, desc = outcome["name"], outcome.get("point"), outcome.get("description")
    base, alt, period = split_market(market)
    if base == "team_totals":
        text = f"{desc} {name} {point:g}" if point is not None else f"{desc} {name}"
    elif desc:  # player prop
        stat = market_label(market)
        stat = stat.split(" ", 1)[1] if stat.startswith(("Player ", "Batter ", "Pitcher ")) else stat
        if point is None:
            text = f"{desc} {stat.lower()}: {name}"
        elif is_player_market(market) and market in YES_NO_MARKETS.get("baseball_mlb", []) and point == 0.5:
            text = f"{desc} {stat.lower()}: {'Yes' if name == 'Over' else 'No'}"
        else:
            text = f"{desc} {name} {point:g} {stat.lower()}"
    elif base == "spreads" and point is not None:
        text = f"{name} {point:+g}"
    elif base == "totals" and point is not None:
        text = f"{name} {point:g}"
    else:
        text = name
    return f"{text}, {period}" if period else text


def yes_no_margin(blines, market):
    """A book's average margin on the two-sided (Yes/No, Over/Under) lines it offers for
    one bet type in this game, or None if it only offers one side everywhere."""
    holds = [sum(1.0 / o["price"] for o in line.values()) - 1
             for k, line in blines.items() if k[0] == market and len(line) == 2]
    holds = [h for h in holds if 0 < h < 0.5]          # ignore broken or extreme lines
    return sum(holds) / len(holds) if holds else None


def analyze_event(event, target, min_books):
    home = event.get("home_team")
    tbook = next((b for b in event["bookmakers"] if b["key"] == target), None)
    if not tbook:
        return []
    others = [(b["key"], b["title"], book_lines(b, home))
              for b in event["bookmakers"] if b["key"] != target]
    rows = []
    for key, outs in book_lines(tbook, home).items():
        names = set(outs)
        one_sided = len(outs) == 1          # e.g. anytime touchdown: only "Yes" is offered
        weighted = {n: 0.0 for n in names}
        total_w, used, estimated, measured = 0.0, [], False, False
        compare = {n: [] for n in names}    # each other book's price for each side
        for bkey, btitle, blines in others:
            line = blines.get(key)
            if not line:
                continue
            if one_sided:
                (n,) = names
                if n not in line:
                    continue
                if len(line) >= 2:
                    probs = {n: devig(line)[n]}
                else:
                    # Only "Yes" here. Use this book's real margin on the same bet type for
                    # players where it offers both sides; guess only if there are none.
                    margin = yes_no_margin(blines, key[0])
                    if margin is None:
                        margin, estimated = ONE_SIDED_MARGIN, True
                    else:
                        measured = True
                    probs = {n: (1.0 / line[n]["price"]) / (1 + margin)}
            elif set(line) == names:
                probs = devig(line)
            else:
                continue
            w = SHARP_WEIGHTS.get(bkey, 1.0)
            for n, p in probs.items():
                weighted[n] += w * p
                compare[n].append({"book": btitle, "price": line[n]["price"],
                                   "american": american(line[n]["price"]), "sharp": bkey in SHARP_WEIGHTS})
            total_w += w
            used.append(btitle)
        if len(used) < min_books:
            continue
        hold = None if one_sided else sum(1.0 / o["price"] for o in outs.values()) - 1
        market = key[0]
        base = split_market(market)[0]
        teams = (home, event.get("away_team"))
        for n, o in outs.items():
            fair = weighted[n] / total_w
            price = o["price"]
            desc = o.get("description")
            rows.append({
                "event_id": event["id"],
                "sport_key": event.get("sport_key"),
                "sport": event.get("sport_title", event.get("sport_key")),
                "event": f"{event.get('away_team')} @ {home}" if home else event.get("sport_title"),
                "home_team": home,
                "away_team": event.get("away_team"),
                "pick_team": n if n in teams else (desc if base == "team_totals" else None),
                "player": desc if desc and is_player_market(market) else None,
                "team_total_for": desc if base == "team_totals" else None,
                "outcome": n,
                "point": o.get("point"),
                "start": event["commence_time"],
                "market": market,
                "market_label": market_label(market),
                "selection": describe(market, o),
                "labeled": True,             # selection already names the stat/period
                "estimated": estimated,      # margin guessed for at least one book
                "margin_measured": measured and not estimated,
                "decimal": price,
                "american": american(price),
                "book_prob": round(1.0 / price, 4),
                "fair_prob": round(fair, 4),
                "edge": round(fair - 1.0 / price, 4),
                "ev": round(fair * price - 1, 4),
                "hold": round(hold, 4) if hold is not None else None,
                "books_used": used,
                "compare": sorted(compare[n], key=lambda c: -c["price"]),
                "sharp": any(b in ("Pinnacle", "Betfair") for b in used),
            })
    return rows


def closing_scan_once(now=None):
    """Closing prices for tracked moneylines, spreads and totals whose games start within 15
    minutes, from TheRundown's free odds (no credits). Returns how many bets got one."""
    if not RUNDOWN_KEY or DEMO:
        return 0
    by_sport = {}
    for b in TRACK.due_for_close(now):
        if b.get("sport_key") in rundown.SPORT_IDS:
            by_sport.setdefault(b["sport_key"], []).append(b)
    done = 0
    for sport, bets in by_sport.items():
        games = {b["event_id"]: {"id": b["event_id"], "home_team": b.get("home_team") or "",
                                 "away_team": b.get("away_team") or "", "commence_time": b["start"]} for b in bets}
        events = []
        for day in sorted({rundown.local_day(b["start"]) for b in bets}):
            try:
                events += rundown.fetch_day(RUNDOWN_KEY, sport, bets[0].get("sport") or sport, day)
            except rundown.RundownError:
                continue
        rundown.match_ids(events, list(games.values()))
        rows = [r for ev in events if ev["id"] in games for r in analyze_event(ev, "fanduel", 1)]
        done += TRACK.record_closes(rows, now)
    return done


def closing_scans():
    """Background: every 5 minutes, record closing prices for games about to start."""
    while True:
        try:
            closing_scan_once()
        except Exception as e:  # never let a hiccup stop the loop
            print(f"Closing-price check skipped: {e}")
        time.sleep(300)


# --------------------------------------------------------------------------- chatbot

def upcoming_games(sport, started_hours=0):
    """Upcoming games in one league as Odds API-style events: its free game list, or
    TheRundown's free schedule if that fails. Games that started more than `started_hours`
    ago are left out (0: only games not started yet)."""
    now = datetime.now(timezone.utc) - timedelta(hours=started_hours)
    try:
        games = api_get(f"/sports/{sport}/events")
    except ApiError:
        games = []
        if RUNDOWN_KEY and sport in rundown.SPORT_IDS:
            try:
                games = rundown.fetch_events(RUNDOWN_KEY, sport, sport_title(sport))
            except rundown.RundownError:
                games = []
    return sorted((g for g in games if parse_time(g["commence_time"]) > now), key=lambda g: g["commence_time"])


def chat_bet(bet):
    """Fill in one bet from the chat: its next game, the stats chance, the free market price
    (moneylines, spreads, totals), injury news and recent form. The page does the rest with
    the same calculations as everywhere else."""
    sport, kind = bet.get("sport_key"), bet.get("kind")
    if kind not in ("team", "player") or not sport:
        return bet
    team, opp = bet.get("team"), bet.get("opponent")
    game = next((g for g in upcoming_games(sport)
                 if (stats._same_team(team, g["home_team"]) and (not opp or stats._same_team(opp, g["away_team"])))
                 or (stats._same_team(team, g["away_team"]) and (not opp or stats._same_team(opp, g["home_team"])))), None)
    if kind == "player":
        markets = [bet["market"]] if bet.get("market") else list(props_stats.SPORTS.get(sport, {}).get("markets", {}))
        forms = {m: chatbot.player_form(sport, bet["player"], m, bet.get("point") if m == bet.get("market") else None) for m in markets}
        bet["forms"] = {m: f for m, f in forms.items() if f and (f["last10"]["avg"] > 0 or m == bet.get("market"))}
    else:
        bet["forms"] = {t: chatbot.team_form(sport, t) for t in (team, opp) if t}
    if not game:
        live = next((g for g in upcoming_games(sport, started_hours=5)
                     if stats._same_team(team, g["home_team"]) or stats._same_team(team, g["away_team"])), None)
        if live and parse_time(live["commence_time"]) <= datetime.now(timezone.utc):
            bet["error"] = (f"{live['away_team']} @ {live['home_team']} has already started. The bot only checks bets "
                            f"before the start, because live odds move too fast.")
        else:
            bet["error"] = f"I couldn't find an upcoming {team} game in the schedule."
        return bet
    home, away = game["home_team"], game["away_team"]
    bet.update(event_id=game["id"], home=home, away=away, start=game["commence_time"], event=f"{away} @ {home}",
               sport=game.get("sport_title") or sport_title(sport))
    if kind == "team" and not opp:
        bet["opponent"] = away if stats._same_team(team, home) else home
    inj = injuries.lookup([{"key": "chat", "sport_key": sport, "player": bet.get("player"), "home": home, "away": away}])
    bet["injury"], bet["injury_teams"], bet["injury_updated"] = inj["injuries"].get("chat"), inj["teams"], inj["updated"]
    market = bet.get("market")
    if not market or bet.get("problem"):
        return bet
    if market in ("h2h", "spreads"):
        bet["outcome"] = home if stats._same_team(team, home) else away     # the game's own spelling
    selection = describe(market, {"name": bet["outcome"], "point": bet.get("point"), "description": bet.get("player")})
    bet.update(selection=selection, market_label=market_label(market))
    found, pending = stats.predict_many([{"key": "chat", "sport_key": sport, "market": market, "outcome": bet["outcome"],
                                          "point": bet.get("point"), "home": home, "away": away, "player": bet.get("player")}])
    bet["stats"], bet["stats_pending"] = found.get("chat"), bool(pending)
    if market in ("h2h", "spreads", "totals") and RUNDOWN_KEY and not DEMO and sport in rundown.SPORT_IDS:
        try:
            evs = rundown.fetch_day(RUNDOWN_KEY, sport, bet["sport"], rundown.local_day(game["commence_time"]))
        except rundown.RundownError:
            evs = []
        rundown.match_ids(evs, [game])
        ev = next((e for e in evs if e["id"] == game["id"]), None)
        rows = analyze_event(ev, "fanduel", 1) if ev else []
        row = next((r for r in rows if r["market"] == market and r["selection"] == selection), None)
        if row:
            bet["row"] = dict(row, scanned_at=None, source="TheRundown (free)")
        else:
            alt = next((r for r in rows if r["market"] == market and r["outcome"] == bet["outcome"]), None)
            if alt:
                bet["fd_line"] = {"selection": alt["selection"], "american": alt["american"], "point": alt["point"]}
    return bet


def chat_answer(message, context=None):
    """What the chat message asks, with the data the page needs to answer it. `context` is the
    bet talked about last (for follow-ups like "the under" or "is he playing")."""
    p = chatbot.parse(message, context)
    if p["intent"] == "unknown":
        chatbot.log_unknown(message)
    for b in p.get("bets", []):
        try:
            chat_bet(b)
        except Exception as e:   # one lookup failing shouldn't break the reply
            b["error"] = f"Something went wrong looking that up: {e}"
    return p


def run_scan(target, sports, include_props, min_books, source="odds_api"):
    now = datetime.now(timezone.utc)
    rows, errors = [], []
    free = source == "therundown" and RUNDOWN_KEY and not DEMO

    def one(sport):
        try:
            return (fetch_events_free if free else fetch_events)(sport, target, include_props), None
        except (ApiError, rundown.RundownError) as e:
            return [], f"{sport}: {e}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        for events, err in pool.map(one, sports):
            if err:
                errors.append(err)
            for ev in events:
                if parse_time(ev["commence_time"]) <= now:
                    continue  # skip games already underway; live odds move too fast
                rows.extend(analyze_event(ev, target, min_books))
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return {
        "book": TARGET_BOOKS[target], "demo": DEMO, "generated": now.isoformat(),
        "count": len(rows), "rows": rows, "errors": errors, "quota": _quota,
        "source": "therundown" if free else "odds_api", "rundown_usage": rundown.usage,
    }


# --------------------------------------------------------------------------- demo data
# Entirely made-up teams and odds so the app can be tried without an API key.

DEMO_SPORTS = [
    ("basketball_nba", "NBA", "Basketball"),
    ("basketball_ncaab", "NCAAB", "Basketball"),
    ("americanfootball_nfl", "NFL", "American Football"),
    ("americanfootball_ncaaf", "NCAAF", "American Football"),
    ("icehockey_nhl", "NHL", "Ice Hockey"),
    ("soccer_epl", "EPL", "Soccer"),
    ("soccer_uefa_champs_league", "UEFA Champions League", "Soccer"),
    ("mma_mixed_martial_arts", "MMA", "Mixed Martial Arts"),
]
DEMO_TEAMS = ["Harbor Hawks", "Ridge Rams", "Metro Comets", "Valley Vipers", "Coast Cougars",
              "Summit Owls", "River Foxes", "Prairie Bison", "Canyon Kings", "Lake Lynx"]
DEMO_PLAYERS = ["J. Carter", "M. Alvarez", "D. Okafor", "T. Nguyen", "R. Silva", "K. Brooks"]
DEMO_BOOKS = [("fanduel", "FanDuel"), ("pinnacle", "Pinnacle"), ("draftkings", "DraftKings"),
              ("betmgm", "BetMGM"), ("williamhill_us", "Caesars"), ("betrivers", "BetRivers")]


# Realistic made-up numbers per league: (spread low, high, total low, high).
DEMO_LINES = {
    "basketball_nba": (1, 12, 212, 238), "basketball_ncaab": (1, 15, 130, 158),
    "americanfootball_nfl": (1, 10, 37, 52), "americanfootball_ncaaf": (1, 21, 42, 64),
    "icehockey_nhl": (1, 2, 5, 7), "baseball_mlb": (1, 2, 7, 10),
    "soccer_epl": (0, 2, 2, 4), "soccer_uefa_champs_league": (0, 2, 2, 4),
    "mma_mixed_martial_arts": (1, 4, 1, 3), "default": (1, 8, 40, 60),
}
# Typical player prop lines: (low, high).
DEMO_PROP_LINES = {
    "player_points": (8, 30), "player_rebounds": (3, 12), "player_assists": (2, 10), "player_threes": (1, 5),
    "player_pass_yds": (180, 300), "player_rush_yds": (30, 95), "player_reception_yds": (25, 85),
    "player_receptions": (2, 8), "batter_hits": (0, 2), "batter_total_bases": (1, 3),
    "pitcher_strikeouts": (4, 8), "player_shots_on_goal": (1, 5),
}


# Same stat name, different sport: NHL "points" are goals + assists, so far smaller.
DEMO_PROP_LINES_BY_SPORT = {("icehockey", "player_points"): (0, 2)}


def demo_prop_line(rng, market, sport=""):
    lo, hi = DEMO_PROP_LINES_BY_SPORT.get((sport.split("_")[0], market)) or DEMO_PROP_LINES.get(market, (3, 20))
    return math.floor(rng.uniform(lo, hi)) + 0.5


def demo_events(sport, target, include_props):
    rng = random.Random(sport)
    soccer = sport.startswith("soccer")
    events = []
    for i in range(5):
        home, away = rng.sample(DEMO_TEAMS, 2)
        start = (datetime.now(timezone.utc) + timedelta(hours=3 + i * 7)).isoformat()
        p_home = rng.uniform(0.3, 0.7)
        lo_s, hi_s, lo_t, hi_t = DEMO_LINES.get(sport, DEMO_LINES["default"])
        spread = math.floor(rng.uniform(lo_s, hi_s)) + 0.5
        total = math.floor(rng.uniform(lo_t, hi_t)) + 0.5
        prop_market = (PROP_MARKETS.get(sport) or ["player_points"])[0]
        prop_lines = [(rng.choice(DEMO_PLAYERS), prop_market, demo_prop_line(rng, prop_market, sport))
                      for _ in range(2)]
        books = []
        for key, title in DEMO_BOOKS:
            margin = 0.025 if key == "pinnacle" else 0.045
            noise = lambda: rng.uniform(-0.035, 0.035)

            def price(p):
                p = min(max(p + noise(), 0.03), 0.97)
                return round(1 / (p * (1 + margin)), 2)

            if soccer:
                pd = 0.26
                h2h = [{"name": home, "price": price(p_home * (1 - pd))},
                       {"name": away, "price": price((1 - p_home) * (1 - pd))},
                       {"name": "Draw", "price": price(pd)}]
            else:
                h2h = [{"name": home, "price": price(p_home)}, {"name": away, "price": price(1 - p_home)}]
            markets = [
                {"key": "h2h", "outcomes": h2h},
                {"key": "spreads", "outcomes": [
                    {"name": home, "point": -spread, "price": price(0.5)},
                    {"name": away, "point": spread, "price": price(0.5)}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total, "price": price(0.5)},
                    {"name": "Under", "point": total, "price": price(0.5)}]},
            ]
            if include_props and sport in PROP_MARKETS:
                for player, mk, pt in prop_lines:
                    markets.append({"key": mk, "outcomes": [
                        {"name": "Over", "description": player, "point": pt, "price": price(0.5)},
                        {"name": "Under", "description": player, "point": pt, "price": price(0.5)}]})
            books.append({"key": key, "title": title, "markets": markets})
        events.append({"id": f"{sport}-{i}", "sport_key": sport,
                       "sport_title": dict((k, t) for k, t, _ in DEMO_SPORTS).get(sport, sport),
                       "commence_time": start, "home_team": home, "away_team": away,
                       "bookmakers": books})
    return events


CODE_FILES = [ROOT / "app.py", ROOT / "stats.py", ROOT / "picks.py", ROOT / "backup.py", ROOT / "scans.py", ROOT / "props_stats.py", ROOT / "wagers.py", ROOT / "injuries.py", ROOT / "rundown.py", ROOT / "chatbot.py"]
_started_with = {f: f.stat().st_mtime for f in CODE_FILES if f.exists()}


def code_changed_since_start():
    """True when app.py or stats.py was edited after this server started (it needs a restart)."""
    return any(f.exists() and f.stat().st_mtime != t for f, t in _started_with.items())


def demo_more_event(sport, eid, markets):
    """Made-up odds for the More bets section: every chosen bet type for one demo game."""
    ev = next((e for e in demo_events(sport, "fanduel", False) if e["id"] == eid), None)
    if not ev:
        raise ApiError("That game isn't available any more.")
    rng = random.Random(eid + "|" + ",".join(sorted(markets)))
    home, away = ev["home_team"], ev["away_team"]
    fd = {m["key"]: m for m in ev["bookmakers"][0]["markets"]}
    h2h = {o["name"]: o["price"] for o in fd["h2h"]["outcomes"]}
    p_home = (1 / h2h[home]) / sum(1 / p for p in h2h.values())
    spread = abs(fd["spreads"]["outcomes"][0]["point"])
    total = fd["totals"]["outcomes"][0]["point"]
    players = rng.sample(DEMO_PLAYERS, 3)
    half = lambda x: math.floor(x) + 0.5          # always a half point: no pushes

    # Each line's outcomes with their "true" chance; prices per book are made from these below.
    spec = []
    for m in markets:
        base, alt, period = split_market(m)
        scale = {None: 1, "1st half": 0.5, "1st quarter": 0.25, "1st period": 0.33, "1st 5 innings": 0.55}.get(period, 0.5)
        if base == "h2h":
            spec.append((m, [{"name": home, "p": p_home}, {"name": away, "p": 1 - p_home}]))
        elif base == "spreads":
            unit = max(1.0, total * 0.015)         # one "step" of points for this sport
            for pt in ([spread - unit, spread + unit, spread + 2 * unit] if alt else [spread * scale]):
                pt = half(max(pt, 0.5))
                p = min(max(0.5 + (spread * scale - pt) * 0.12 / unit, 0.05), 0.95)
                spec.append((m, [{"name": home, "point": -pt, "p": p}, {"name": away, "point": pt, "p": 1 - p}]))
        elif base == "totals":
            for pt in ([total * scale - total * 0.06, total * scale + total * 0.06] if alt else [total * scale]):
                pt = half(pt)
                p = min(max(0.5 + (total * scale - pt) / (total * 0.25), 0.05), 0.95)
                spec.append((m, [{"name": "Over", "point": pt, "p": p}, {"name": "Under", "point": pt, "p": 1 - p}]))
        elif base == "team_totals":
            for team, share in ((home, p_home), (away, 1 - p_home)):
                exp = total * (0.45 + share * 0.1)
                for pt in ([exp - total * 0.05, exp + total * 0.05] if alt else [exp]):
                    pt = half(pt)
                    p = min(max(0.5 + (exp - pt) / (total * 0.2), 0.05), 0.95)
                    spec.append((m, [{"name": "Over", "point": pt, "description": team, "p": p},
                                     {"name": "Under", "point": pt, "description": team, "p": 1 - p}]))
        elif m == "batter_home_runs":
            for pl in players:
                p = rng.uniform(0.08, 0.3)
                spec.append((m, [{"name": "Over", "point": 0.5, "description": pl, "p": p},
                                 {"name": "Under", "point": 0.5, "description": pl, "p": 1 - p}]))
        elif any(m in v for v in YES_NO_MARKETS.values()):
            for pl in players:   # yes-only market, like real anytime-touchdown odds
                spec.append((m, [{"name": "Yes", "description": pl, "p": rng.uniform(0.12, 0.55)}]))
        elif is_player_market(m):
            for pl in players[:2]:
                pt = demo_prop_line(rng, m, sport)
                spec.append((m, [{"name": "Over", "point": pt, "description": pl, "p": 0.5},
                                 {"name": "Under", "point": pt, "description": pl, "p": 0.5}]))

    books = []
    for key, title in DEMO_BOOKS:
        margin = 0.025 if key == "pinnacle" else 0.05
        by_market = {}
        for m, outs in spec:
            for o in outs:
                p = min(max(o["p"] + rng.uniform(-0.035, 0.035), 0.03), 0.97)
                out = {k: v for k, v in o.items() if k != "p"}
                out["price"] = round(1 / (p * (1 + margin)), 2)
                by_market.setdefault(m, []).append(out)
        books.append({"key": key, "title": title,
                      "markets": [{"key": m, "outcomes": outs} for m, outs in by_market.items()]})
    return dict(ev, bookmakers=books)


def demo_injury_report(sport):
    """Made-up injury report for demo mode: two demo players and one per demo team."""
    if sport not in injuries.PATHS:
        return None
    teams = {e["home_team"] for e in demo_events(sport, "fanduel", False)}
    made_up = [(DEMO_PLAYERS[0], "Questionable", "question", "Ankle soreness; a game-time decision (demo)."),
               (DEMO_PLAYERS[1], "Out", "out", "Won't play tonight (demo).")]
    players = [{"name": n, "key": props_stats.name_key(n), "team": t, "status": st, "level": lv, "comment": c,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "returns": "", "position": ""}
               for n, st, lv, c in made_up for t in teams]
    players += [{"name": f"Bench Player {i}", "key": f"bench player {i}", "team": t, "status": "Day-To-Day",
                 "level": "question", "comment": "Illness (demo).", "date": "", "returns": "", "position": ""}
                for i, t in enumerate(sorted(teams), 1)]
    return {"updated": datetime.now(timezone.utc).isoformat(), "players": players}


def demo_stats(items):
    """Made-up stats for demo mode: the market's chance nudged up or down a little."""
    out = {}
    for it in items:
        key = it.get("key", "")
        market = (it.get("market") or "").replace("alternate_", "", 1)
        if market not in ("h2h", "spreads", "totals", "team_totals") or it.get("outcome") == "Draw":
            out[key] = {"reason": "Stats don't cover this bet type yet"}
            continue
        rng = random.Random(key)
        fair = float(it.get("fair_prob") or 0.5)
        out[key] = {"prob": round(min(max(fair + rng.uniform(-0.06, 0.06), 0.02), 0.98), 4),
                    "form_home": f"{rng.randint(3, 8)}-{rng.randint(2, 7)}",
                    "form_away": f"{rng.randint(3, 8)}-{rng.randint(2, 7)}"}
    return out


# --------------------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- safety checks for every request -------------------------------------------
    # Only this computer's own page may control the bot. Other websites you visit could
    # otherwise make your browser send it commands (spend credits, delete picks), and a
    # trick called DNS rebinding could reach it under another site name.
    def _blocked(self, needs_marker):
        port = self.server.server_address[1]
        own = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
        if self.headers.get("Host", "") not in own:
            return "Requests must use the bot's own address (http://localhost:%d)." % port
        origin = self.headers.get("Origin")
        if origin and origin.replace("http://", "", 1) not in own:
            return "Requests from other websites aren't allowed."
        if needs_marker and self.headers.get("X-Odd-Finder") != "1":
            return "Requests must come from the Odd Finder page."
        return None

    def _safely(self, handler, needs_marker):
        why = self._blocked(needs_marker)
        if why:
            return self.send_json({"error": why}, 403)
        try:
            handler()
        except (ValueError, TypeError) as e:          # e.g. a malformed number in the request
            self.send_json({"error": f"Bad request: {e}"}, 400)
        except BrokenPipeError:
            pass                                         # the page went away before the reply
        except Exception as e:                           # never leave the page without an answer
            self.send_json({"error": f"Something went wrong: {e}"}, 500)

    def do_POST(self):
        self._safely(self._post, needs_marker=True)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        self._safely(self._get, needs_marker=path.startswith("/api/"))

    def _post(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/images", "/api/stats", "/api/picks", "/api/track", "/api/saved-scans", "/api/wagers", "/api/injuries", "/api/chat"):
            return self.send_json({"error": "Not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20_000_000:          # room for a full browser copy of saved scans
            return self.send_json({"error": "Request too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json({"error": "Bad JSON"}, 400)
        if path == "/api/picks":
            return self.handle_picks(body)
        if path == "/api/track":
            return self.handle_track(body)
        if path == "/api/wagers":
            return self.handle_wagers(body)
        if path == "/api/saved-scans":
            book, action = body.get("book"), body.get("action")
            if book not in TARGET_BOOKS:
                return self.send_json({"error": "Unknown sportsbook"}, 400)
            if action == "clear_more":
                SCANS.clear_more(book)
                return self.send_json({"ok": True})
            if action == "import":
                results = body.get("results") if isinstance(body.get("results"), dict) else {}
                more = body.get("more") if isinstance(body.get("more"), list) else []
                return self.send_json(SCANS.import_browser(book, results, more))
            return self.send_json({"error": "Unknown action"}, 400)
        items = body.get("items", [])
        items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        if path == "/api/images":
            found, pending = IMAGES.get(items)
            return self.send_json({"images": found, "pending": pending})
        if path == "/api/chat":
            return self.send_json(chat_answer(str(body.get("message") or "")[:500], body.get("context")))
        if path == "/api/injuries":
            return self.send_json(injuries.lookup(items, demo_injury_report if DEMO else injuries.report))
        if DEMO:
            return self.send_json({"stats": demo_stats(items), "pending": 0})
        found, pending = stats.predict_many(items)
        self.send_json({"stats": found, "pending": pending})

    def handle_wagers(self, body):
        """My wagers: list, add, update, delete."""
        action = body.get("action", "list")
        try:
            if action == "add":
                WAGERS.add(body.get("wager"))
            elif action == "add_many":
                WAGERS.add_many(body.get("wagers"))
            elif action == "update":
                WAGERS.update(str(body.get("id")), body.get("wager"))
            elif action == "delete":
                WAGERS.delete(str(body.get("id")))
            elif action == "bankroll":
                WAGERS.set_bankroll(body.get("bankroll"))
            elif action != "list":
                return self.send_json({"error": "Unknown action"}, 400)
            if action != "list":
                backup_soon()
            self.send_json({"wagers": WAGERS.all(), "bankroll": WAGERS.bankroll})
        except (KeyError, ValueError) as e:
            self.send_json({"error": str(e).strip("'\"")}, 400)

    def handle_track(self, body):
        """Bot track record: log bets, summarize, check results, clear."""
        action = body.get("action", "summary")
        since = body.get("since") if isinstance(body.get("since"), str) else None
        view = body.get("view") if body.get("view") in ("likely", "side", "value", "agree", "all") else "side"
        if action in ("log", "grade", "clear"):
            backup_soon()
        if action == "log":
            bets = body.get("bets") if isinstance(body.get("bets"), list) else []
            added, updated = TRACK.log(bets)
            return self.send_json({"added": added, "updated": updated})
        if action == "grade":
            graded = 0 if DEMO else TRACK.grade_pending(refresh=True)
            return self.send_json({"graded": graded, "summary": picks.track_summary(TRACK.all(), since, view)})
        if action == "clear":
            TRACK.clear()
        elif action != "summary":
            return self.send_json({"error": "Unknown action"}, 400)
        self.send_json({"summary": picks.track_summary(TRACK.all(), since, view)})

    def handle_picks(self, body):
        """Saved picks: list, save, update (stake / manual result), delete, grade."""
        action = body.get("action", "list")
        try:
            if action != "list":
                backup_soon()
            if action == "save":
                pick = PICKS.save(body.get("pick") or {}, body.get("stake"))
                return self.send_json({"pick": pick, "picks": PICKS.all()})
            if action == "update":
                PICKS.update(str(body.get("id")), {k: body[k] for k in ("stake", "status") if k in body})
            elif action == "delete":
                PICKS.delete(str(body.get("id")))
            elif action == "grade":
                graded = 0 if DEMO else PICKS.grade_pending(refresh=True)
                return self.send_json({"picks": PICKS.all(), "graded": graded})
            elif action != "list":
                return self.send_json({"error": "Unknown action"}, 400)
            self.send_json({"picks": PICKS.all()})
        except (KeyError, ValueError) as e:
            self.send_json({"error": str(e).strip("'\"")}, 400)

    def _get(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")   # always get the latest page
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path.startswith("/static/") and url.path.count("/") == 2:
                f = ROOT / "static" / url.path.split("/")[-1]
                types = {".svg": "image/svg+xml", ".png": "image/png", ".css": "text/css", ".js": "text/javascript"}
                if f.suffix not in types or not f.is_file():
                    return self.send_json({"error": "Not found"}, 404)
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", types[f.suffix])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/config":
                self.send_json({"books": [{"key": k, "name": v} for k, v in TARGET_BOOKS.items()],
                                "coming_soon": COMING_SOON_BOOKS,
                                "demo": DEMO, "has_key": bool(API_KEY), "quota": _quota,
                                "outdated": code_changed_since_start(),
                                "backup": BACKUP.status() if BACKUP else {"enabled": False, "demo": DEMO},
                                "github_backup": GITBACKUP.status() if GITBACKUP else {"enabled": False},
                                "prop_sports": list(PROP_MARKETS),
                                "rundown": bool(RUNDOWN_KEY) and not DEMO, "rundown_usage": rundown.usage,
                                "rundown_sports": list(rundown.SPORT_IDS)})
            elif url.path == "/api/accuracy":
                self.send_json(stats.accuracy())
            elif url.path == "/api/sports":
                self.send_json(list_sports())
            elif url.path == "/api/scan":
                target = q.get("book", ["fanduel"])[0]
                if target not in TARGET_BOOKS:
                    return self.send_json({"error": f"Unknown sportsbook: {target}"}, 400)
                sports = [s for s in q.get("sports", [""])[0].split(",") if s]
                if not sports:
                    return self.send_json({"error": "Pick at least one sport."}, 400)
                props = q.get("props", ["0"])[0] == "1"
                min_books = max(1, int(q.get("min_books", ["2"])[0]))
                source = q.get("source", ["odds_api"])[0]
                result = run_scan(target, sports, props, min_books, source)
                SCANS.save_main(target, sports, result["rows"])
                self.send_json(result)
            elif url.path == "/api/saved-scans":
                target = q.get("book", ["fanduel"])[0]
                if target not in TARGET_BOOKS:
                    return self.send_json({"error": f"Unknown sportsbook: {target}"}, 400)
                self.send_json(SCANS.load(target))
            elif url.path == "/api/games":
                sport = q.get("sport", [""])[0]
                if not SAFE_ID.match(sport):
                    return self.send_json({"error": "Pick a league."}, 400)
                self.send_json({"games": list_games(sport), "groups": more_groups(sport), "quota": _quota})
            elif url.path == "/api/more-scan":
                target = q.get("book", ["fanduel"])[0]
                sport = q.get("sport", [""])[0]
                if target not in TARGET_BOOKS or not SAFE_ID.match(sport):
                    return self.send_json({"error": "Pick a sportsbook and a league."}, 400)
                events = [e for e in q.get("events", [""])[0].split(",") if SAFE_ID.match(e)]
                allowed = {m for g in more_groups(sport) for m in g["markets"]}
                markets = [m for m in q.get("markets", [""])[0].split(",") if m in allowed]
                if not events or not markets:
                    return self.send_json({"error": "Pick at least one game and one bet type."}, 400)
                if len(events) > MORE_MAX_GAMES:
                    return self.send_json({"error": f"Pick at most {MORE_MAX_GAMES} games at a time."}, 400)
                result = run_more_scan(target, sport, events, markets)
                SCANS.save_more(target, events, result["rows"])
                self.send_json(result)
            else:
                self.send_json({"error": "Not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 502)


def main():
    global DEMO, API_KEY, RUNDOWN_KEY, IMAGES, PICKS, TRACK, BACKUP, GITBACKUP, SCANS, WAGERS
    ap = argparse.ArgumentParser(description="Betting value finder")
    ap.add_argument("--demo", action="store_true", help="use made-up sample data")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    API_KEY = load_api_key()
    RUNDOWN_KEY = (os.environ.get("THERUNDOWN_KEY") or load_config().get("therundown_key") or "").strip() or None
    DEMO = args.demo or not API_KEY
    IMAGES = ImageFinder()
    if DEMO:   # keep made-up demo picks and results apart from real ones
        PICKS = picks.PickStore(stats.DATA_DIR / "picks_demo.json")
        WAGERS = wagers.WagerStore(stats.DATA_DIR / "wagers_demo.json")
        TRACK = picks.TrackStore(stats.DATA_DIR / "track_demo.json")
        SCANS = scans.ScanStore(stats.DATA_DIR / "scans_demo.json")
    else:      # back up real picks and results, and restore them if they were deleted
        other = claim_data(stats.DATA_DIR / "running.lock", args.port)
        if other:
            raise SystemExit(f"Odd Finder is already running at http://localhost:{other}\n"
                             f"Use that one, or stop it first (Ctrl+C in its Terminal window). Running two\n"
                             f"copies at once could make them overwrite each other's saved picks and results.")
        cfg = load_config()
        folder = cfg.get("backup_folder")
        BACKUP = backup.Backup(stats.DATA_DIR, Path(folder).expanduser() if folder else backup.default_folder())
        GITBACKUP = backup.GitBackup(stats.DATA_DIR, cfg.get("github_backup"))
        restored = []
        for b in (BACKUP, GITBACKUP):          # iCloud first, then GitHub for anything still missing
            got = b.restore_missing()
            if got:
                print(f"Restored from your backup on {b.status()['where']}: {', '.join(got)}")
            restored += got
        if restored:
            PICKS, TRACK = picks.PickStore(), picks.TrackStore()     # load the restored files
        BACKUP.start()
        GITBACKUP.start()
        if not BACKUP.enabled:
            print("Backups are off: iCloud Drive wasn't found. Set \"backup_folder\" in config.json to choose a folder.")
    stats.warm_up()   # start downloading past results in the background
    if RUNDOWN_KEY and not DEMO:
        threading.Thread(target=closing_scans, daemon=True).start()   # free closing prices for CLV
    if DEMO and not args.demo:
        print("No API key found (ODDS_API_KEY or config.json), so running with DEMO data.")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if e.errno == 48:
            raise SystemExit(
                f"Port {args.port} is already in use, probably by another copy of this app.\n"
                f"Close that one (Ctrl+C in its Terminal window), or run on another port:\n"
                f"  python3 app.py --port {args.port + 1}")
        raise
    print(f"Value finder running at http://localhost:{args.port}  ({'DEMO data' if DEMO else 'live odds'})")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
