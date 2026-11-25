"""
Player props: stats second opinion (NBA)
----------------------------------------
Predicts a player's points, rebounds, assists or 3-pointers from past box scores:

    expected stat = expected minutes x per-minute rate x opponent factor

  * expected minutes and per-minute rates: weighted averages of the player's recent
    games (newer games count more; games he didn't play are left out, since props
    are void when a player doesn't play)
  * opponent factor: how much of that stat the opponent allows compared with an
    average team (its strength is tuned on last season)

The chance of going over a line comes from how far real results landed from the
prediction in past games, so it reflects how swingy each stat really is.
Box scores come from ESPN's free game summaries and are saved in data/, so each
game is only downloaded once.
"""
import bisect
import gzip
import json
import math
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import stats

STATS = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "3pm"}
LABELS = {"pts": "points", "reb": "rebounds", "ast": "assists", "3pm": "3-pointers"}
SPORT = "basketball_nba"
MIN_GAMES = 5                   # a player needs this many recent games before we predict
REFRESH_SECONDS = 6 * 3600


def summary_url(event_id):
    return f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}"


def fetch_box(event_id):
    """One game's box score: [{name, team, min, pts, reb, ast, 3pm}] for players who played."""
    req = urllib.request.Request(summary_url(event_id), headers={"Accept-Encoding": "gzip", "User-Agent": "odd-finder/1.0"})
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
    data = json.loads(body)
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
                if mins <= 0:
                    continue
                players.append({"name": a["athlete"]["displayName"], "team": tname, "min": mins,
                                "pts": float(s[idx["PTS"]]), "reb": float(s[idx["REB"]]), "ast": float(s[idx["AST"]]),
                                "3pm": float(s[idx["3PT"]].split("-")[0])})
            except (ValueError, KeyError, IndexError):
                continue
    return players


class BoxScoreStore:
    def __init__(self, sport=SPORT):
        self.file = stats.DATA_DIR / f"players_{sport}.json"
        try:
            self.games = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.games = {}          # event id -> {"date", "season", "home", "away", "players": [...]}

    def update(self, results):
        """Download box scores for finished games we don't have yet."""
        todo = [g for g in results if g["id"] not in self.games]

        def one(g):
            try:
                return g, fetch_box(g["id"])
            except Exception:
                return g, None
        stats.DATA_DIR.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, (g, players) in enumerate(pool.map(one, todo), 1):
                if players:
                    self.games[g["id"]] = {"date": g["date"], "season": g["season"], "home": g["home"],
                                           "away": g["away"], "players": players}
                if i % 100 == 0:
                    self.file.write_text(json.dumps(self.games, separators=(",", ":")))
        self.file.write_text(json.dumps(self.games, separators=(",", ":")))
        return sorted(self.games.values(), key=lambda g: g["date"])


# --------------------------------------------------------------------------- the model

class PropModel:
    """Walks through games in order, predicting each player's stats before learning from them."""

    def __init__(self, half_life=10, opp_weight=1.0, opp_half_life=20):
        self.a = 1 - 0.5 ** (1 / half_life)
        self.oa = 1 - 0.5 ** (1 / opp_half_life)
        self.w = opp_weight
        self.players = {}        # name -> {"n", "min", "rate": {stat: per-minute}, "team", "last"}
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
            for stat in LABELS:
                mu = self.expect(pl["name"], opp, stat)
                if mu is not None:
                    self.log.append((g["season"], stat, mu, pl[stat], pl["name"], g["date"]))
        # learn: players
        for pl in g["players"]:
            p = self.players.get(pl["name"])
            if p is None:
                p = self.players[pl["name"]] = {"n": 0, "min": pl["min"],
                                                "rate": {s: pl[s] / pl["min"] for s in LABELS}}
            else:
                p["min"] += self.a * (pl["min"] - p["min"])
                for s in LABELS:
                    p["rate"][s] += self.a * (pl[s] / pl["min"] - p["rate"][s])
            p["n"] += 1
            p["team"], p["last"] = pl["team"], g["date"]
        # learn: what each team allowed, and the league average
        for team, opp in teams.items():
            given = {s: sum(pl[s] for pl in g["players"] if pl["team"] == opp) for s in LABELS}
            al = self.allowed.setdefault(team, dict(given))
            for s in LABELS:
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


def loss(entries, zs_by_stat):
    """Average log loss of over/under predictions at lines like the books set (recent average, .5)."""
    total, n = 0.0, 0
    for _, stat, mu, actual, line in entries:
        p = prob_over(mu, line, zs_by_stat[stat])
        y = 1.0 if actual > line else 0.0
        p = min(max(p, 1e-4), 1 - 1e-4)
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
        n += 1
    return total / max(n, 1)


def recent_lines(games):
    """For each player-game, a line like the books set: the player's last-10-game average, rounded to .5."""
    hist, out = {}, {}
    for g in games:
        for pl in g["players"]:
            h = hist.setdefault(pl["name"], {s: [] for s in LABELS})
            for s in LABELS:
                past = h[s][-10:]
                if len(past) >= MIN_GAMES:
                    out[(g["date"], pl["name"], s)] = math.floor(sum(past) / len(past)) + 0.5
                h[s].append(pl[s])
    return out


def evaluate(games, half_life, opp_weight):
    """Run the model through all games; return it and its predictions at book-like lines."""
    m = PropModel(half_life, opp_weight)
    for g in games:
        m.play(g)
    lines = recent_lines(games)
    entries = []
    for season, stat, mu, actual, name, date in m.log:
        line = lines.get((date, name, stat))
        if line is not None:
            entries.append((season, stat, mu, actual, line))
    return m, entries


# --------------------------------------------------------------------------- live predictions

def name_key(name):
    """'Jaren Jackson Jr.' and 'Jaren Jackson Jr' match; accents and suffixes don't matter."""
    words = stats.normalize(name or "").split()
    while words and words[-1] in ("jr", "sr", "ii", "iii", "iv"):
        words.pop()
    return " ".join(words)


def tune_and_test(games):
    """Tune on last season, test on this season. Returns everything needed to predict,
    plus which stats beat both a coin flip and the player's recent hit rate."""
    test_season = max(g["season"] for g in games)
    best = None
    for hl in (5, 10, 20):
        for ow in (0.0, 0.5, 1.0):
            m, entries = evaluate(games, hl, ow)
            zs = {s: sorted(z_of(mu, a) for se, st, mu, a, _, _ in m.log if st == s and se < test_season) for s in LABELS}
            score = loss([e for e in entries if e[0] < test_season], zs)
            if best is None or score < best[0]:
                best = (score, hl, ow, m, entries, zs)
    _, hl, ow, m, entries, zs_train = best
    hits = _hit_rates(games)
    report = {}
    for stat in LABELS:
        rows = {"train": [], "test": []}
        for season, st, mu, actual, name, date in m.log:
            past = hits.get((date, name, st))
            if st != stat or not past or len(past) < MIN_GAMES:
                continue
            line = math.floor(sum(past) / len(past)) + 0.5
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
    zs_all = {s: sorted(z_of(mu, a) for _, st, mu, a, _, _ in m.log if st == s) for s in LABELS}
    return {"model": m, "zs": zs_all, "report": report, "season": test_season, "half_life": hl, "opp_weight": ow}


def _ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _hit_rates(games):
    hist, out = {}, {}
    for g in games:
        for pl in g["players"]:
            h = hist.setdefault(pl["name"], {s: [] for s in LABELS})
            for s in LABELS:
                out[(g["date"], pl["name"], s)] = list(h[s][-10:])
                h[s].append(pl[s])
    return out


class LiveProps:
    """Keeps the props model up to date in the background and answers questions about upcoming props."""

    def __init__(self):
        self.lock = threading.Lock()
        self.store = BoxScoreStore()
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
            league = stats._leagues[SPORT]
            for _ in range(120):                       # wait for the team results to download first
                if league.status in ("ready", "error"):
                    break
                league.refresh_if_stale()
                time.sleep(5)
            games = self.store.update(league.store.games())
            if len(games) < 300:
                raise RuntimeError("Not enough box scores yet.")
            state = tune_and_test(games)
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
        stat = STATS.get(item.get("market"))
        if not stat:
            return None
        if self.state is None:
            return "pending" if self.status != "error" else {"reason": f"Couldn't load player stats: {self.error}"}
        rep = self.state["report"].get(stat, {})
        if not rep.get("usable"):
            return {"reason": f"Stats weren't more accurate than a player's recent hit rate for {LABELS[stat]}"}
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
        return {"prob": round(prob, 4), "expected_stat": round(mu, 1), "stat_name": LABELS[stat]}

    def accuracy(self):
        self.refresh_if_stale()
        if not self.state:
            return {"status": self.status, "error": self.error}
        return {"status": "ready", "season": self.state["season"],
                "stats": {LABELS[s]: r for s, r in self.state["report"].items()}}


LIVE = LiveProps()
