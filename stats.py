"""
Stats second opinion
--------------------
Builds an independent estimate of each bet's chance of winning from past game
results, so it can be compared with the betting market's estimate.

Data: final scores from ESPN's public scoreboard feeds (last season + this
season), saved in data/ so each finished day is only downloaded once.

Model, per league:
  * Elo team ratings (with home advantage and margin of victory) -> win chance.
  * Elo difference -> expected winning margin (slope fitted on last season),
    with a normal spread around it -> chance of covering a point spread.
  * Points scored / allowed averages -> expected game total -> over/under chance.
Settings are tuned on last season and tested on this season, so the accuracy
report shows how the model does on games it was not tuned on.
"""
import gzip
import json
import math
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

# Odds API sport key -> ESPN feed and starting model settings.
#   k: how fast ratings move, home: home advantage in Elo points,
#   carry: share of a team's rating kept from one season to the next,
#   half_life: games for points-scored averages to give half weight to old games.
LEAGUES = {
    "americanfootball_nfl": {"name": "NFL", "path": "football/nfl", "by": "week",
                             "k": 20, "home": 48, "carry": 0.67, "half_life": 8},
    "basketball_nba": {"name": "NBA", "path": "basketball/nba", "by": "day",
                       "k": 20, "home": 70, "carry": 0.75, "half_life": 15},
    "icehockey_nhl": {"name": "NHL", "path": "hockey/nhl", "by": "day",
                      "k": 6, "home": 33, "carry": 0.7, "half_life": 15},
    "baseball_mlb": {"name": "MLB", "path": "baseball/mlb", "by": "day",
                     "k": 4, "home": 24, "carry": 0.67, "half_life": 30},
}
REFRESH_SECONDS = 6 * 3600     # rebuild a league's model at most every 6 hours
MIN_TEST_GAMES = 30            # fewer test games than this -> not enough to judge accuracy
Z80 = 1.2816                   # 80% of a normal distribution lies within +-1.2816 sd


# --------------------------------------------------------------------------- downloading

# ESPN starts refusing if asked too much at once, so all leagues share 3 request slots.
_espn_slots = threading.BoundedSemaphore(3)


def espn_get(path, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{ESPN}/{path}/scoreboard" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "odd-finder/1.0"})
    last_err = None
    for attempt in range(4):
        try:
            with _espn_slots:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
            return json.loads(body)
        except Exception as e:  # network hiccup or too many requests: wait and retry
            last_err = e
            time.sleep(3 * 2 ** attempt)
    raise last_err


def parse_games(feed):
    """Pull finished regular-season and playoff games out of one scoreboard response."""
    games = []
    for e in feed.get("events", []):
        season = e.get("season", {})
        if season.get("type") not in (2, 3):          # skip preseason / all-star etc.
            continue
        comp = (e.get("competitions") or [{}])[0]
        if not comp.get("status", e.get("status", {})).get("type", {}).get("completed"):
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue

        def score(c):
            s = c.get("score")
            return float(s.get("value") if isinstance(s, dict) else s)

        try:
            hs, as_ = score(sides["home"]), score(sides["away"])
        except (TypeError, ValueError):
            continue
        games.append({
            "id": e["id"], "date": e["date"], "season": season.get("year"),
            "home": sides["home"]["team"]["displayName"], "away": sides["away"]["team"]["displayName"],
            "hs": hs, "as": as_, "neutral": bool(comp.get("neutralSite")),
        })
    return games


def ymd(s):
    return s[:10].replace("-", "")


class ResultsStore:
    """Downloads and saves finished games for one league."""

    def __init__(self, sport_key):
        self.key = sport_key
        self.cfg = LEAGUES[sport_key]
        self.file = DATA_DIR / f"results_{sport_key}.json"
        try:
            self.saved = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.saved = {"units": {}}

    def _units(self):
        """Every request needed for last season and this season: (unit id, params, end date)."""
        today = datetime.now(timezone.utc).date()
        now_feed = espn_get(self.cfg["path"])
        league = now_feed["leagues"][0]
        start = datetime.fromisoformat(league["calendarStartDate"].replace("Z", "+00:00")).date()
        prev_feed = espn_get(self.cfg["path"], dates=(start - timedelta(days=120)).strftime("%Y%m%d"))
        units = []
        for feed in (prev_feed, now_feed):
            lg = feed["leagues"][0]
            cal = lg.get("calendar", [])
            if self.cfg["by"] == "week":
                year = lg["season"]["year"]
                for block in cal:
                    stype = int(block.get("value", 0))
                    if stype not in (2, 3):
                        continue
                    for wk in block.get("entries", []):
                        begins = datetime.fromisoformat(wk["startDate"].replace("Z", "+00:00")).date()
                        ends = datetime.fromisoformat(wk["endDate"].replace("Z", "+00:00")).date()
                        if begins <= today:
                            units.append((f"{year}-{stype}-{wk['value']}",
                                          {"dates": year, "seasontype": stype, "week": wk["value"]}, ends))
            else:
                if cal and isinstance(cal[0], str) and len(cal) > 60:
                    days = sorted({date.fromisoformat(d[:10]) for d in cal})
                else:  # calendar not listed day by day: walk the whole season
                    s = datetime.fromisoformat(lg["calendarStartDate"].replace("Z", "+00:00")).date()
                    e = datetime.fromisoformat(lg["calendarEndDate"].replace("Z", "+00:00")).date()
                    days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
                for d in days:
                    if d <= today:
                        units.append((d.strftime("%Y%m%d"), {"dates": d.strftime("%Y%m%d")}, d))
        return units

    def update(self):
        """Download anything new. Finished days/weeks are kept and never downloaded again."""
        today = datetime.now(timezone.utc).date()
        todo = [u for u in self._units()
                if not self.saved["units"].get(u[0], {}).get("final")]

        self.failed = 0

        def fetch(unit):
            uid, params, ends = unit
            try:
                games = parse_games(espn_get(self.cfg["path"], **params))
            except Exception as e:
                self.failed += 1
                self.last_error = f"{type(e).__name__}: {e}"
                return uid, None
            # A unit is final once it's more than 2 days in the past (late games, stat fixes).
            return uid, {"final": ends < today - timedelta(days=2), "games": games}

        DATA_DIR.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, (uid, result) in enumerate(pool.map(fetch, todo), 1):
                if result is not None:
                    self.saved["units"][uid] = result
                if i % 50 == 0:   # save progress so an interrupted first download isn't lost
                    self.file.write_text(json.dumps(self.saved))
        self.file.write_text(json.dumps(self.saved))
        return self.games()

    def games(self):
        seen, out = set(), []
        for u in self.saved["units"].values():
            for g in u["games"]:
                if g["id"] not in seen:
                    seen.add(g["id"])
                    out.append(g)
        out.sort(key=lambda g: g["date"])
        return out


# --------------------------------------------------------------------------- the model

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_over(mu, sd, line):
    """Chance a whole-number result beats `line` (pushes on whole lines left out)."""
    if line == math.floor(line):
        over = 1 - norm_cdf((line + 0.5 - mu) / sd)
        under = norm_cdf((line - 0.5 - mu) / sd)
        return over / (over + under) if over + under > 0 else 0.5
    return 1 - norm_cdf((line - mu) / sd)


class Model:
    """Walks through every game in date order, predicting each one before learning from it."""

    def __init__(self, cfg, k_mult=1.0, home_mult=1.0):
        self.k = cfg["k"] * k_mult
        self.home = cfg["home"] * home_mult
        self.carry = cfg["carry"]
        self.alpha = 1 - 0.5 ** (1 / cfg["half_life"])   # weight of the newest game
        self.elo, self.season_of = {}, {}
        self.latest_season = 0                           # newest season seen so far
        self.scored, self.allowed = {}, {}               # team -> weighted avg points
        self.league_pts, self.home_edge = None, 0.0      # avg points per team-game, home - away
        self.recent = {}                                 # team -> last results ("W"/"L"/"T")
        self.log = []                                    # predictions made before each game

    # ---- ratings
    def _rating(self, team, season):
        r = self.elo.get(team, 1500.0)
        if team in self.season_of and self.season_of[team] != season:
            r = 1500 + (r - 1500) * self.carry           # new season: pull toward average
        self.season_of[team] = season
        return r

    def current(self, team):
        """A team's rating now. One that hasn't played yet this season still gets the
        between-seasons pull toward average, as it will when it plays."""
        r = self.elo.get(team, 1500.0)
        if team in self.season_of and self.season_of[team] < self.latest_season:
            r = 1500 + (r - 1500) * self.carry
        return r

    def elo_diff(self, home, away, neutral=False):
        return self.current(home) - self.current(away) + (0 if neutral else self.home)

    @staticmethod
    def win_prob(diff):
        return 1 / (1 + 10 ** (-diff / 400))

    # ---- scoring
    def expected_points(self, team, opp, at_home):
        if self.league_pts is None:
            return None
        lp = self.league_pts
        off = self.scored.get(team, lp)
        dfn = self.allowed.get(opp, lp)
        base = off * dfn / lp if lp else lp
        return base + (self.home_edge / 2 if at_home else -self.home_edge / 2)

    def form(self, team, n=10):
        res = self.recent.get(team, [])[-n:]
        if not res:
            return None
        w, l, t = res.count("W"), res.count("L"), res.count("T")
        return f"{w}-{l}" + (f"-{t}" if t else "")

    def play(self, g):
        h, a, season = g["home"], g["away"], g["season"]
        self.latest_season = max(self.latest_season, season)
        rh, ra = self._rating(h, season), self._rating(a, season)
        self.elo[h], self.elo[a] = rh, ra
        diff = rh - ra + (0 if g["neutral"] else self.home)
        p = self.win_prob(diff)
        eh, ea = self.expected_points(h, a, not g["neutral"]), self.expected_points(a, h, False)
        self.log.append({"season": season, "date": g["date"], "p": p, "diff": diff,
                         "margin": g["hs"] - g["as"], "total": g["hs"] + g["as"],
                         "exp_total": (eh + ea) if eh is not None else None,
                         "exp_h": eh, "exp_a": ea, "hs": g["hs"], "as": g["as"],
                         "games_h": len(self.recent.get(h, [])), "games_a": len(self.recent.get(a, []))})

        # learn from the result
        margin = g["hs"] - g["as"]
        result = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        if margin:
            winner_diff = diff if margin > 0 else -diff
            mult = math.log(abs(margin) + 1) * 2.2 / (winner_diff * 0.001 + 2.2)
        else:
            mult = 1.0
        change = self.k * mult * (result - p)
        self.elo[h], self.elo[a] = rh + change, ra - change

        a_ = self.alpha
        for team, pts_for, pts_against in ((h, g["hs"], g["as"]), (a, g["as"], g["hs"])):
            if team not in self.scored:
                self.scored[team], self.allowed[team] = pts_for, pts_against
            else:
                self.scored[team] += a_ * (pts_for - self.scored[team])
                self.allowed[team] += a_ * (pts_against - self.allowed[team])
        avg = (g["hs"] + g["as"]) / 2
        self.league_pts = avg if self.league_pts is None else self.league_pts + a_ / 4 * (avg - self.league_pts)
        if not g["neutral"]:
            self.home_edge += a_ / 4 * ((g["hs"] - g["as"]) - self.home_edge)
        for team, r in ((h, result), (a, 1 - result)):
            self.recent.setdefault(team, []).append("W" if r == 1 else "L" if r == 0 else "T")
            self.recent[team] = self.recent[team][-10:]


def log_loss(entries):
    total = 0.0
    for e in entries:
        y = 1.0 if e["margin"] > 0 else 0.0 if e["margin"] < 0 else 0.5
        p = min(max(e["p"], 1e-6), 1 - 1e-6)
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return total / max(len(entries), 1)


def settled(entries, min_games=5):
    """Leave out each team's first few games, before the model knows them."""
    return [e for e in entries if e["games_h"] >= min_games and e["games_a"] >= min_games]


class LeagueStats:
    """Downloads results, tunes and tests the model, and answers questions about upcoming games."""

    def __init__(self, sport_key):
        self.key = sport_key
        self.cfg = LEAGUES[sport_key]
        self.store = ResultsStore(sport_key)
        self.lock = threading.Lock()
        self.model = None
        self.status, self.error, self.built_at = "not started", None, 0
        self.report = None
        self.teams = {}            # normalized name -> ESPN name

    def refresh_if_stale(self):
        with self.lock:
            if self.status == "loading" or time.time() - self.built_at < REFRESH_SECONDS:
                return
            self.status = "loading" if self.model is None else self.status
            self.built_at = time.time()  # stops repeat starts while this one runs
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        try:
            games = self.store.update()
            if len(games) < 50:
                raise RuntimeError("Not enough finished games found yet.")
            seasons = sorted({g["season"] for g in games})
            test_season = seasons[-1]
            # Tune on last season: pick the settings that predicted it best.
            best = None
            for km in (0.5, 1.0, 1.5):
                for hm in (0.5, 1.0, 1.5):
                    m = Model(self.cfg, km, hm)
                    for g in games:
                        if g["season"] < test_season:
                            m.play(g)
                    score = log_loss(settled(m.log))
                    if best is None or score < best[0]:
                        best = (score, km, hm)
            _, km, hm = best if best and len(seasons) > 1 else (0, 1.0, 1.0)
            model = Model(self.cfg, km, hm)
            for g in games:
                model.play(g)
            train = settled([e for e in model.log if e["season"] < test_season]) or settled(model.log)
            test = settled([e for e in model.log if e["season"] == test_season])
            # Fit: points of margin per Elo point, and how much results vary.
            sxy = sum(e["diff"] * e["margin"] for e in train)
            sxx = sum(e["diff"] ** 2 for e in train) or 1
            model.margin_per_elo = sxy / sxx
            res = [e["margin"] - model.margin_per_elo * e["diff"] for e in train]
            model.margin_sd = max(math.sqrt(sum(r * r for r in res) / max(len(res), 1)), 1.0)
            tot = [e["total"] - e["exp_total"] for e in train if e["exp_total"] is not None]
            model.total_bias = sum(tot) / max(len(tot), 1)
            model.total_sd = max(math.sqrt(sum((t - model.total_bias) ** 2 for t in tot) / max(len(tot), 1)), 1.0)
            # One team's score (for team totals): how far it lands from its expected points.
            team_res = [x for e in train if e["exp_h"] is not None
                        for x in (e["hs"] - e["exp_h"], e["as"] - e["exp_a"])]
            model.team_bias = sum(team_res) / max(len(team_res), 1)
            model.team_sd = max(math.sqrt(sum((x - model.team_bias) ** 2 for x in team_res) / max(len(team_res), 1)), 0.5)
            report = self._accuracy(model, train, test, test_season)
            teams = {normalize(t): t for t in model.elo}
            with self.lock:
                self.model, self.report, self.teams = model, report, teams
                self.status, self.error = "ready", None
        except Exception as e:
            with self.lock:
                self.status = "ready" if self.model else "error"
                self.error = str(e)
                self.built_at = time.time() - REFRESH_SECONDS + 600   # try again in 10 minutes

    def _accuracy(self, model, train, test, test_season):
        """How well the model did on this season's games, each predicted before it was played."""
        out = {"league": self.cfg["name"], "season": test_season, "games": len(test)}
        if not test:
            out["usable"] = {"moneyline": False, "spread": False, "total": False}
            return out
        home_rate = sum(1 for e in train if e["margin"] > 0) / max(len(train), 1)
        y = [1.0 if e["margin"] > 0 else 0.0 if e["margin"] < 0 else 0.5 for e in test]
        brier = sum((e["p"] - yy) ** 2 for e, yy in zip(test, y)) / len(test)
        base = sum((home_rate - yy) ** 2 for yy in y) / len(test)
        picked = sum(1 for e, yy in zip(test, y) if (e["p"] >= 0.5) == (yy == 1.0) and yy != 0.5) / len(test)
        buckets = []
        for lo, hi in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)):
            sel = [(max(e["p"], 1 - e["p"]), (yy == 1.0) if e["p"] >= 0.5 else (yy == 0.0))
                   for e, yy in zip(test, y) if lo <= max(e["p"], 1 - e["p"]) < hi and yy != 0.5]
            if sel:
                buckets.append({"range": f"{round(lo * 100)}–{min(round(hi * 100), 100)}%",
                                "games": len(sel),
                                "predicted": round(sum(s[0] for s in sel) / len(sel), 3),
                                "actual": round(sum(1 for s in sel if s[1]) / len(sel), 3)})
        margin_in = [abs(e["margin"] - model.margin_per_elo * e["diff"]) <= Z80 * model.margin_sd for e in test]
        totals = [e for e in test if e["exp_total"] is not None]
        total_in = [abs(e["total"] - (e["exp_total"] + model.total_bias)) <= Z80 * model.total_sd for e in totals]
        skill = 1 - brier / base if base else 0
        m_cov = sum(margin_in) / len(margin_in)
        t_cov = sum(total_in) / len(total_in) if total_in else 0
        enough = len(test) >= MIN_TEST_GAMES
        out.update({
            "picked_winner": round(picked, 3),
            "skill": round(skill, 3),
            "buckets": buckets,
            "spread_coverage": round(m_cov, 3),
            "total_coverage": round(t_cov, 3),
            "usable": {"moneyline": enough and skill > 0,
                       "spread": enough and 0.70 <= m_cov <= 0.90,
                       "total": enough and bool(total_in) and 0.70 <= t_cov <= 0.90},
        })
        return out

    # ---- answering questions
    def match(self, name):
        if not name:
            return None
        n = normalize(name)
        n = ALIASES.get(n, n)
        if n in self.teams:
            return self.teams[n]
        # Fall back to the nickname ("Lakers") if exactly one team has it.
        last = n.split()[-1]
        hits = [t for k, t in self.teams.items() if k.split()[-1] == last]
        return hits[0] if len(hits) == 1 else None

    def predict(self, item):
        """item: market, outcome, point, home, away -> {prob, form...} or {reason}."""
        m, report = self.model, self.report
        home, away = self.match(item.get("home")), self.match(item.get("away"))
        if not home or not away:
            return {"reason": "Team not found in past results"}
        usable = report["usable"]
        market, outcome, point = item.get("market") or "", item.get("outcome"), item.get("point")
        if market.startswith("alternate_"):      # alternate lines: same model, different number
            market = market[len("alternate_"):]
        diff = m.elo_diff(home, away)
        out = {"form_home": m.form(home), "form_away": m.form(away)}
        if market == "h2h":
            if not usable["moneyline"]:
                return dict(out, reason="Stats weren't accurate enough for this league's moneylines")
            p_home = m.win_prob(diff)
            if outcome == item.get("home"):
                out["prob"] = p_home
            elif outcome == item.get("away"):
                out["prob"] = 1 - p_home
            else:
                return dict(out, reason="Draws aren't covered")
        elif market == "spreads" and point is not None:
            if not usable["spread"]:
                return dict(out, reason="Stats weren't accurate enough for this league's spreads")
            mu = m.margin_per_elo * diff                       # expected home margin
            if outcome == item.get("home"):
                out["prob"] = prob_over(mu, m.margin_sd, -point)
            else:
                out["prob"] = prob_over(-mu, m.margin_sd, -point)
        elif market == "totals" and point is not None:
            if not usable["total"]:
                return dict(out, reason="Stats weren't accurate enough for this league's totals")
            eh, ea = m.expected_points(home, away, True), m.expected_points(away, home, False)
            if eh is None:
                return dict(out, reason="Not enough scoring data")
            mu = eh + ea + m.total_bias
            p_over = prob_over(mu, m.total_sd, point)
            out["prob"] = p_over if outcome == "Over" else 1 - p_over
            out["expected_total"] = round(mu, 1)
        elif market == "team_totals" and point is not None:
            if not usable["total"]:
                return dict(out, reason="Stats weren't accurate enough for this league's totals")
            team = self.match(item.get("team"))
            if team not in (home, away):
                return dict(out, reason="Team not found in past results")
            opp = away if team == home else home
            exp = m.expected_points(team, opp, team == home)
            if exp is None:
                return dict(out, reason="Not enough scoring data")
            mu = exp + m.team_bias
            p_over = prob_over(mu, m.team_sd, point)
            out["prob"] = p_over if outcome == "Over" else 1 - p_over
            out["expected_team"] = round(mu, 1)
        elif any(market.endswith(s) for s in ("_q1", "_q2", "_q3", "_q4", "_h1", "_h2", "_p1", "_p2", "_p3", "_innings")):
            return dict(out, reason="Stats don't cover quarter, half or period bets yet")
        else:
            return dict(out, reason="Stats don't cover this bet type yet")
        out["prob"] = round(out["prob"], 4)
        return out


# Old or alternate team names the odds feed may use -> ESPN's current name (normalized).
ALIASES = {"utah hockey club": "utah mammoth", "oakland athletics": "athletics"}


def normalize(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and").replace(".", "")
    s = "".join(ch if ch.isalnum() or ch == " " else " " for ch in s)
    s = " ".join(s.split())
    if s.startswith("la "):
        s = "los angeles " + s[3:]
    return s


# --------------------------------------------------------------------------- public API

_leagues = {k: LeagueStats(k) for k in LEAGUES}


def warm_up():
    """Start loading every league in the background (called when the app starts)."""
    for lg in _leagues.values():
        lg.refresh_if_stale()
    import props_stats
    for live in props_stats.LIVE.values():
        live.refresh_if_stale()


_last_results_refresh = {}


def _same_team(a, b):
    """Team names from the odds feed and ESPN: equal, or the same nickname ("Lakers")."""
    a, b = ALIASES.get(normalize(a), normalize(a)), ALIASES.get(normalize(b), normalize(b))
    return a == b or (a and b and a.split()[-1] == b.split()[-1])


def find_final_game(sport_key, home, away, start_iso, refresh=False):
    """The finished game matching the odds feed's home and away teams and start time, as
    (game, flipped) where flipped means ESPN lists home and away the other way round;
    or None if it isn't finished or the league isn't covered. `refresh` downloads any new
    results first (at most every 15 minutes per league)."""
    lg = _leagues.get(sport_key)
    if not lg or not home or not away:
        return None
    if refresh and time.time() - _last_results_refresh.get(sport_key, 0) > 900:
        _last_results_refresh[sport_key] = time.time()
        try:
            lg.store.update()
        except Exception:
            pass
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    for g in reversed(lg.store.games()):
        when = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        if abs((when - start).total_seconds()) > 18 * 3600:
            continue
        if _same_team(home, g["home"]) and _same_team(away, g["away"]):
            return g, False
        if _same_team(home, g["away"]) and _same_team(away, g["home"]):   # listed the other way round
            return g, True
    return None


def find_final_score(sport_key, home, away, start_iso, refresh=False):
    """Final score of one game as (home points, away points), using the odds feed's home and
    away teams, or None if it isn't finished or the league isn't covered."""
    found = find_final_game(sport_key, home, away, start_iso, refresh)
    if not found:
        return None
    g, flipped = found
    return (g["as"], g["hs"]) if flipped else (g["hs"], g["as"])


def predict_many(items):
    """items: [{key, sport_key, market, outcome, point, home, away}] -> ({key: result}, pending)"""
    import props_stats                            # here, not at the top: props_stats imports this file
    results, pending = {}, 0
    for it in items[:2000]:
        if props_stats.handles(it):
            res = props_stats.LIVE[it["sport_key"]].predict(it)
            if res == "pending":
                pending += 1
            elif res:
                results[it["key"]] = res
            continue
        lg = _leagues.get(it.get("sport_key"))
        if not lg:
            results[it["key"]] = {"reason": "Stats aren't available for this league yet"}
            continue
        lg.refresh_if_stale()
        if lg.model is None:
            if lg.status == "error":
                results[it["key"]] = {"reason": f"Couldn't load past results: {lg.error}"}
            else:
                pending += 1
            continue
        try:
            results[it["key"]] = lg.predict(it)
        except Exception as e:
            results[it["key"]] = {"reason": f"Stats error: {e}"}
    return results, pending


def accuracy():
    import props_stats
    out = [{"sport_key": f"props_{k}", "league": f"{props_stats.LEAGUE_NAMES[k]} player props", "props": live.accuracy()}
           for k, live in props_stats.LIVE.items()]
    for key, lg in _leagues.items():
        lg.refresh_if_stale()
        out.append({"sport_key": key, "league": lg.cfg["name"], "status": lg.status,
                    "error": lg.error, "report": lg.report})
    return out
