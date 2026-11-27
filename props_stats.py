"""
Player props: stats second opinion (NBA, NHL, NFL)
--------------------------------------------------
Predicts a player's stat for a prop from past box scores:

    expected stat = expected playing time x rate per unit of playing time x opponent factor

  * playing time is minutes (NBA), ice time (NHL), or one unit per game (NFL, where
    box scores don't record playing time); the expected time and the rate are weighted
    averages of the player's recent games, newer games counting more. Games a player
    didn't play are left out, since props are void when a player doesn't play.
  * opponent factor: how much of that stat the opponent allows compared with an
    average team (its strength is tuned on last season)

The chance of going over a line comes from how far real results landed from the
prediction in past games, so it reflects how swingy each stat really is.

Honest gate, rerun every time the model updates: tune on last season, test on this
season at lines like the sportsbooks' (the player's recent average). A stat is used
only if it beats both a coin flip and the player's recent hit rate over that line.

Box scores come from ESPN's free game summaries and are saved in data/, so each game
is only downloaded once. The same box scores grade props automatically after games.
"""
import bisect
import gzip
import json
import math
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import stats

MIN_GAMES = 5                   # a player needs this many recent games before we predict
REFRESH_SECONDS = 6 * 3600


# --------------------------------------------------------------------------- reading box scores

def _num(x):
    return float(str(x).split("-")[0].split("/")[0])


def _toi(x):
    """'13:42' -> 13.7 minutes."""
    m, _, s = str(x).partition(":")
    return float(m) + (float(s) / 60 if s else 0)


def parse_nba(data):
    players = []
    for team in data.get("boxscore", {}).get("players", []):
        tname = team["team"]["displayName"]
        block = (team.get("statistics") or [{}])[0]
        labels = block.get("labels", [])
        idx = {k: labels.index(k) for k in ("MIN", "PTS", "REB", "AST", "3PT") if k in labels}
        if len(idx) < 5:
            continue
        for a in block.get("athletes", []):
            s = a.get("stats") or []
            if a.get("didNotPlay") or len(s) < len(labels):
                continue
            try:
                mins = float(s[idx["MIN"]])
                if mins > 0:
                    players.append({"name": a["athlete"]["displayName"], "team": tname, "min": mins,
                                    "pts": _num(s[idx["PTS"]]), "reb": _num(s[idx["REB"]]),
                                    "ast": _num(s[idx["AST"]]), "3pm": _num(s[idx["3PT"]])})
            except (ValueError, KeyError, IndexError):
                continue
    return players


def parse_nhl(data):
    players = {}
    for team in data.get("boxscore", {}).get("players", []):
        tname = team["team"]["displayName"]
        for block in team.get("statistics", []):
            if block.get("name") == "goalies":
                continue
            labels = block.get("labels", [])
            # ESPN's "S" column is shots on goal; its "SOG" column is shootout goals.
            if not all(k in labels for k in ("TOI", "G", "A", "S")):
                continue
            ix = {k: labels.index(k) for k in ("TOI", "G", "A", "S")}
            for a in block.get("athletes", []):
                s = a.get("stats") or []
                if a.get("didNotPlay") or len(s) < len(labels):
                    continue
                try:
                    toi = _toi(s[ix["TOI"]])
                    if toi > 0:
                        players[a["athlete"]["displayName"]] = {
                            "name": a["athlete"]["displayName"], "team": tname, "min": toi,
                            "pts": _num(s[ix["G"]]) + _num(s[ix["A"]]), "sog": _num(s[ix["S"]])}
                except (ValueError, KeyError, IndexError):
                    continue
    return list(players.values())


def parse_nfl(data):
    """Passing, rushing and receiving combined per player; a missing category counts as 0."""
    wanted = {"passing": {"YDS": "passyds"}, "rushing": {"YDS": "rushyds"},
              "receiving": {"YDS": "recyds", "REC": "rec"}}
    players = {}
    for team in data.get("boxscore", {}).get("players", []):
        tname = team["team"]["displayName"]
        for block in team.get("statistics", []):
            fields = wanted.get(block.get("name"))
            labels = block.get("labels", [])
            if not fields or not all(k in labels for k in fields):
                continue
            for a in block.get("athletes", []):
                s = a.get("stats") or []
                if len(s) < len(labels):
                    continue
                name = a["athlete"]["displayName"]
                p = players.setdefault(name, {"name": name, "team": tname, "min": 1.0,
                                              "passyds": 0.0, "rushyds": 0.0, "recyds": 0.0, "rec": 0.0})
                try:
                    for label, key in fields.items():
                        p[key] = _num(s[labels.index(label)])
                except (ValueError, IndexError):
                    continue
    return list(players.values())


SPORTS = {
    "basketball_nba": {"path": "basketball/nba", "parse": parse_nba,
                       "markets": {"player_points": "pts", "player_rebounds": "reb",
                                   "player_assists": "ast", "player_threes": "3pm"},
                       "labels": {"pts": "points", "reb": "rebounds", "ast": "assists", "3pm": "3-pointers"},
                       "min_line": {"pts": 4.5, "reb": 1.5, "ast": 1.5, "3pm": 0.5}},
    "icehockey_nhl": {"path": "hockey/nhl", "parse": parse_nhl,
                      "markets": {"player_points": "pts", "player_shots_on_goal": "sog"},
                      "labels": {"pts": "points", "sog": "shots on goal"},
                      "min_line": {"pts": 0.5, "sog": 1.5}},
    "americanfootball_nfl": {"path": "football/nfl", "parse": parse_nfl,
                             "markets": {"player_pass_yds": "passyds", "player_rush_yds": "rushyds",
                                         "player_reception_yds": "recyds", "player_receptions": "rec"},
                             "labels": {"passyds": "passing yards", "rushyds": "rushing yards",
                                        "recyds": "receiving yards", "rec": "receptions"},
                             "min_line": {"passyds": 150.5, "rushyds": 20.5, "recyds": 15.5, "rec": 1.5}},
}


def fetch_box(sport, event_id):
    """One finished game's box score, as a list of players who played."""
    cfg = SPORTS[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}/summary?event={event_id}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "odd-finder/1.0"})
    for attempt in range(4):
        try:
            with stats._espn_slots:
                with urllib.request.urlopen(req, timeout=25) as r:
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * 2 ** attempt)
    return cfg["parse"](json.loads(body))


class BoxScoreStore:
    def __init__(self, sport):
        self.sport = sport
        self.file = stats.DATA_DIR / f"players_{sport}.json"
        try:
            self.games = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.games = {}          # event id -> {"date", "season", "home", "away", "players": [...]}

    def save(self):
        stats.DATA_DIR.mkdir(exist_ok=True)
        self.file.write_text(json.dumps(self.games, separators=(",", ":")))

    def update(self, results):
        """Download box scores for finished games we don't have yet."""
        todo = [g for g in results if g["id"] not in self.games]

        def one(g):
            try:
                return g, fetch_box(self.sport, g["id"])
            except Exception:
                return g, None
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, (g, players) in enumerate(pool.map(one, todo), 1):
                if players:
                    self.games[g["id"]] = {"date": g["date"], "season": g["season"], "home": g["home"],
                                           "away": g["away"], "players": players}
                if i % 100 == 0:
                    self.save()
        self.save()
        return sorted(self.games.values(), key=lambda g: g["date"])


# --------------------------------------------------------------------------- the model

class PropModel:
    """Walks through games in order, predicting each player's stats before learning from them."""

    def __init__(self, stat_keys, half_life=10, opp_weight=1.0, opp_half_life=20):
        self.keys = list(stat_keys)
        self.a = 1 - 0.5 ** (1 / half_life)
        self.oa = 1 - 0.5 ** (1 / opp_half_life)
        self.w = opp_weight
        self.players = {}        # name -> {"n", "min", "rate": {stat: per unit of playing time}, "team", "last"}
        self.allowed = {}        # team -> {stat: avg allowed per game}
        self.league = {}         # stat -> avg per team-game
        self.log = []            # (season, stat, mu, actual, player, date)

    def expect(self, name, opp, stat):
        p = self.players.get(name)
        if not p or p["n"] < MIN_GAMES:
            return None
        mu = p["min"] * p["rate"][stat]
        lg, al = self.league.get(stat), self.allowed.get(opp, {}).get(stat)
        if lg and al:
            mu *= (al / lg) ** self.w
        return mu

    def play(self, g):
        teams = {g["home"]: g["away"], g["away"]: g["home"]}
        for pl in g["players"]:
            opp = teams.get(pl["team"])
            for stat in self.keys:
                mu = self.expect(pl["name"], opp, stat)
                if mu is not None:
                    self.log.append((g["season"], stat, mu, pl[stat], pl["name"], g["date"]))
        for pl in g["players"]:
            p = self.players.get(pl["name"])
            if p is None:
                p = self.players[pl["name"]] = {"n": 0, "min": pl["min"],
                                                "rate": {s: pl[s] / pl["min"] for s in self.keys}}
            else:
                p["min"] += self.a * (pl["min"] - p["min"])
                for s in self.keys:
                    p["rate"][s] += self.a * (pl[s] / pl["min"] - p["rate"][s])
            p["n"] += 1
            p["team"], p["last"] = pl["team"], g["date"]
        for team, opp in teams.items():
            given = {s: sum(pl[s] for pl in g["players"] if pl["team"] == opp) for s in self.keys}
            al = self.allowed.setdefault(team, dict(given))
            for s in self.keys:
                al[s] += self.oa * (given[s] - al[s])
                self.league[s] = given[s] if s not in self.league else self.league[s] + self.oa / 10 * (given[s] - self.league[s])


def z_of(mu, x):
    return (x - mu) / math.sqrt(mu + 1)


def prob_over(mu, line, zs):
    """Share of past results that would have gone over `line`, given expected `mu`.
    zs: sorted standardized misses from past games for this stat."""
    if not zs:
        return None
    cut = z_of(mu, line)
    over = len(zs) - bisect.bisect_right(zs, cut)
    return (over + 0.5) / (len(zs) + 1)


def _ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def loss(entries, zs_by_stat):
    """Average log loss of over/under predictions at lines like the books set."""
    total = sum(_ll(prob_over(mu, line, zs_by_stat[stat]), 1.0 if actual > line else 0.0)
                for _, stat, mu, actual, line in entries)
    return total / max(len(entries), 1)


def recent(games, keys):
    """For each player-game, that player's previous 10 results for each stat."""
    hist, out = {}, {}
    for g in games:
        for pl in g["players"]:
            h = hist.setdefault(pl["name"], {s: [] for s in keys})
            for s in keys:
                out[(g["date"], pl["name"], s)] = list(h[s][-10:])
                h[s].append(pl[s])
    return out


def book_line(past):
    """A line like the sportsbooks': the recent average, ending in .5."""
    return math.floor(sum(past) / len(past)) + 0.5


def evaluate(games, keys, half_life, opp_weight, min_line=None):
    """Run the model through all games; return it and its predictions at book-like lines
    (only lines a sportsbook would offer: at least `min_line` for that stat)."""
    min_line = min_line or {}
    m = PropModel(keys, half_life, opp_weight)
    for g in games:
        m.play(g)
    hist = recent(games, keys)
    entries = []
    for season, stat, mu, actual, name, date in m.log:
        past = hist.get((date, name, stat))
        if past and len(past) >= MIN_GAMES and book_line(past) >= min_line.get(stat, 0):
            entries.append((season, stat, mu, actual, book_line(past)))
    return m, entries


def tune_and_test(games, keys, min_line=None):
    """Tune on last season, test on this season. Returns what's needed to predict, plus which
    stats beat both a coin flip and the player's recent hit rate."""
    test_season = max(g["season"] for g in games)
    best = None
    for hl in (5, 10, 20):
        for ow in (0.0, 0.5, 1.0):
            m, entries = evaluate(games, keys, hl, ow, min_line)
            zs = {s: sorted(z_of(mu, a) for se, st, mu, a, _, _ in m.log if st == s and se < test_season) for s in keys}
            score = loss([e for e in entries if e[0] < test_season], zs)
            if best is None or score < best[0]:
                best = (score, hl, ow, m, zs)
    _, hl, ow, m, zs_train = best
    hist = recent(games, keys)
    report = {}
    for stat in keys:
        rows = {"train": [], "test": []}
        for season, st, mu, actual, name, date in m.log:
            past = hist.get((date, name, st))
            if st != stat or not past or len(past) < MIN_GAMES or not zs_train[stat]:
                continue
            line = book_line(past)
            if line < (min_line or {}).get(stat, 0):
                continue
            p_hit = (sum(1 for x in past if x > line) + 0.5) / (len(past) + 1)
            rows["test" if season == test_season else "train"].append(
                (prob_over(mu, line, zs_train[stat]), p_hit, 1.0 if actual > line else 0.0))
        ks = [x / 20 for x in range(6, 21)]
        k = min(ks, key=lambda k: sum(_ll(0.5 + k * (p - 0.5), y) for p, _, y in rows["train"])) if rows["train"] else 1.0
        t = rows["test"]
        if len(t) < 200:
            report[stat] = {"usable": False, "bets": len(t), "k": k}
            continue
        model = sum(_ll(0.5 + k * (p - 0.5), y) for p, _, y in t) / len(t)
        hitr = sum(_ll(h, y) for _, h, y in t) / len(t)
        coin = math.log(2)
        report[stat] = {"usable": model < hitr and model < coin, "bets": len(t), "k": k,
                        "vs_hit_rate": round((hitr - model) / hitr, 4), "vs_coin": round((coin - model) / coin, 4)}
    zs_all = {s: sorted(z_of(mu, a) for _, st, mu, a, _, _ in m.log if st == s) for s in keys}
    return {"model": m, "zs": zs_all, "report": report, "season": test_season, "half_life": hl, "opp_weight": ow}


# --------------------------------------------------------------------------- live predictions

def name_key(name):
    """'Jaren Jackson Jr.' and 'Jaren Jackson Jr' match; accents and suffixes don't matter."""
    words = stats.normalize(name or "").split()
    while words and words[-1] in ("jr", "sr", "ii", "iii", "iv"):
        words.pop()
    return " ".join(words)


class LiveProps:
    """Keeps one sport's props model up to date in the background and answers questions."""

    def __init__(self, sport):
        self.sport = sport
        self.cfg = SPORTS[sport]
        self.lock = threading.Lock()
        self.store = BoxScoreStore(sport)
        self.state = None
        self.status, self.error, self.built_at = "not started", None, 0
        self.names = {}

    def refresh_if_stale(self):
        with self.lock:
            if self.status == "loading" or time.time() - self.built_at < REFRESH_SECONDS:
                return
            self.status = "loading" if self.state is None else self.status
            self.built_at = time.time()
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        try:
            league = stats._leagues[self.sport]
            for _ in range(120):                       # wait for the team results to download first
                if league.status in ("ready", "error"):
                    break
                league.refresh_if_stale()
                time.sleep(5)
            games = self.store.update(league.store.games())
            if len(games) < 100:
                raise RuntimeError("Not enough box scores yet.")
            state = tune_and_test(games, self.cfg["labels"], self.cfg.get("min_line"))
            names = {name_key(n): n for n in state["model"].players}
            with self.lock:
                self.state, self.names = state, names
                self.status, self.error = "ready", None
        except Exception as e:
            with self.lock:
                self.status = "ready" if self.state else "error"
                self.error = str(e)
                self.built_at = time.time() - REFRESH_SECONDS + 600

    def predict(self, item):
        self.refresh_if_stale()
        stat = self.cfg["markets"].get((item.get("market") or "").replace("alternate_", "", 1))
        if not stat:
            return None
        label = self.cfg["labels"][stat]
        if self.state is None:
            return "pending" if self.status != "error" else {"reason": f"Couldn't load player stats: {self.error}"}
        rep = self.state["report"].get(stat, {})
        if not rep.get("usable"):
            return {"reason": f"Stats weren't more accurate than a player's recent hit rate for {label}"}
        m = self.state["model"]
        name = self.names.get(name_key(item.get("player")))
        if not name:
            return {"reason": "Player not found in this season's box scores"}
        p = m.players[name]
        if p["n"] < MIN_GAMES:
            return {"reason": "Not enough recent games for this player"}
        home, away = item.get("home"), item.get("away")
        if stats._same_team(p["team"], home):
            opp = next((t for t in m.allowed if stats._same_team(t, away)), None)
        elif stats._same_team(p["team"], away):
            opp = next((t for t in m.allowed if stats._same_team(t, home)), None)
        else:
            return {"reason": "Player's current team doesn't match this game"}
        mu = m.expect(name, opp, stat)
        point = item.get("point")
        if mu is None or point is None:
            return {"reason": "Not enough data for this prop"}
        p_over = prob_over(mu, point, self.state["zs"][stat])
        p_over = 0.5 + rep["k"] * (p_over - 0.5)
        prob = p_over if item.get("outcome") == "Over" else 1 - p_over
        return {"prob": round(prob, 4), "expected_stat": round(mu, 1), "stat_name": label}

    def accuracy(self):
        self.refresh_if_stale()
        if not self.state:
            return {"status": self.status, "error": self.error}
        return {"status": "ready", "season": self.state["season"],
                "stats": {self.cfg["labels"][s]: r for s, r in self.state["report"].items()}}


LIVE = {sport: LiveProps(sport) for sport in SPORTS}
LEAGUE_NAMES = {"basketball_nba": "NBA", "icehockey_nhl": "NHL", "americanfootball_nfl": "NFL"}


def handles(item):
    sport = item.get("sport_key")
    market = (item.get("market") or "").replace("alternate_", "", 1)
    return sport in SPORTS and market in SPORTS[sport]["markets"]


def find_player_stat(sport, market, player, home, away, start_iso, refresh=False):
    """A player's real stat in a finished game, for grading props.
    Returns ("played", value), ("did_not_play", None), or None if the game isn't over yet."""
    cfg = SPORTS.get(sport)
    stat = cfg and cfg["markets"].get(market)
    if not stat:
        return None
    found = stats.find_final_game(sport, home, away, start_iso, refresh)
    if not found:
        return None
    game = found[0]
    live = LIVE[sport]
    with live.lock:
        box = live.store.games.get(game["id"])
    if box is None:                                   # not downloaded yet: fetch just this game
        try:
            players = fetch_box(sport, game["id"])
        except Exception:
            return None
        if not players:
            return None
        box = {"date": game["date"], "season": game["season"], "home": game["home"],
               "away": game["away"], "players": players}
        with live.lock:
            live.store.games[game["id"]] = box
    want = name_key(player)
    for pl in box["players"]:
        if name_key(pl["name"]) == want:
            return ("played", pl[stat])
    # NBA and NHL box scores list everyone who played. NFL ones only list players who
    # recorded a stat, so a missing player may have played without a catch: can't tell.
    return ("unknown", None) if sport == "americanfootball_nfl" else ("did_not_play", None)
