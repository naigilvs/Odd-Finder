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
}
PROP_EVENTS_PER_SPORT = 8      # caps API usage when props are turned on
CACHE_SECONDS = 300

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
_cache = {}
_cache_lock = threading.Lock()
_quota = {"remaining": None, "used": None}


# --------------------------------------------------------------------------- odds API

class ApiError(Exception):
    pass


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
    props = PROP_MARKETS.get(sport)
    if include_props and props:
        soon = datetime.now(timezone.utc) + timedelta(hours=48)
        upcoming = [e for e in events if parse_time(e["commence_time"]) < soon][:PROP_EVENTS_PER_SPORT]
        by_id = {e["id"]: e for e in events}
        for ev in upcoming:
            try:
                extra = api_get(
                    f"/sports/{sport}/events/{ev['id']}/odds", bookmakers=books,
                    markets=",".join(props), oddsFormat="decimal",
                )
            except ApiError:
                continue
            merge_bookmakers(by_id[ev["id"]], extra.get("bookmakers", []))
    return events


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
    name, point, desc = outcome["name"], outcome.get("point"), outcome.get("description")
    if desc:  # player prop
        return f"{desc} {name} {point:g}" if point is not None else f"{desc} {name}"
    if market == "spreads":
        return f"{name} {point:+g}"
    if market == "totals":
        return f"{name} {point:g}"
    return name


def analyze_event(event, target, min_books):
    home = event.get("home_team")
    tbook = next((b for b in event["bookmakers"] if b["key"] == target), None)
    if not tbook:
        return []
    others = [(b["key"], b["title"], book_lines(b, home))
              for b in event["bookmakers"] if b["key"] != target]
    rows = []
    for key, outs in book_lines(tbook, home).items():
        if len(outs) < 2:
            continue
        names = set(outs)
        weighted = {n: 0.0 for n in names}
        total_w, used = 0.0, []
        for bkey, btitle, blines in others:
            line = blines.get(key)
            if not line or set(line) != names:
                continue
            w = SHARP_WEIGHTS.get(bkey, 1.0)
            for n, p in devig(line).items():
                weighted[n] += w * p
            total_w += w
            used.append(btitle)
        if len(used) < min_books:
            continue
        hold = sum(1.0 / o["price"] for o in outs.values()) - 1
        market = key[0]
        for n, o in outs.items():
            fair = weighted[n] / total_w
            price = o["price"]
            rows.append({
                "event_id": event["id"],
                "sport_key": event.get("sport_key"),
                "sport": event.get("sport_title", event.get("sport_key")),
                "event": f"{event.get('away_team')} @ {home}" if home else event.get("sport_title"),
                "home_team": home,
                "away_team": event.get("away_team"),
                "pick_team": n if n in (home, event.get("away_team")) else None,
                "player": o.get("description"),
                "outcome": n,
                "point": o.get("point"),
                "start": event["commence_time"],
                "market": market,
                "market_label": MARKET_LABELS.get(market, market.replace("_", " ").title()),
                "selection": describe(market, o),
                "decimal": price,
                "american": american(price),
                "book_prob": round(1.0 / price, 4),
                "fair_prob": round(fair, 4),
                "edge": round(fair - 1.0 / price, 4),
                "ev": round(fair * price - 1, 4),
                "hold": round(hold, 4),
                "books_used": used,
                "sharp": any(b in ("Pinnacle", "Betfair") for b in used),
            })
    return rows


def run_scan(target, sports, include_props, min_books):
    now = datetime.now(timezone.utc)
    rows, errors = [], []

    def one(sport):
        try:
            return fetch_events(sport, target, include_props), None
        except ApiError as e:
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


def demo_events(sport, target, include_props):
    rng = random.Random(sport)
    soccer = sport.startswith("soccer")
    events = []
    for i in range(5):
        home, away = rng.sample(DEMO_TEAMS, 2)
        start = (datetime.now(timezone.utc) + timedelta(hours=3 + i * 7)).isoformat()
        p_home = rng.uniform(0.3, 0.7)
        spread = round(rng.uniform(1, 9) * 2) / 2
        total = round(rng.uniform(40, 230) * 2) / 2
        prop_lines = [(rng.choice(DEMO_PLAYERS), "player_points", round(rng.uniform(8, 30)) + 0.5)
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
                       "sport_title": dict((k, t) for k, t, _ in DEMO_SPORTS)[sport],
                       "commence_time": start, "home_team": home, "away_team": away,
                       "bookmakers": books})
    return events


def demo_stats(items):
    """Made-up stats for demo mode: the market's chance nudged up or down a little."""
    out = {}
    for it in items:
        key = it.get("key", "")
        if it.get("market") not in ("h2h", "spreads", "totals") or it.get("outcome") == "Draw":
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

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/images", "/api/stats"):
            return self.send_json({"error": "Not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            return self.send_json({"error": "Request too large"}, 413)
        try:
            items = json.loads(self.rfile.read(length) or b"{}").get("items", [])
        except ValueError:
            return self.send_json({"error": "Bad JSON"}, 400)
        items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        if path == "/api/images":
            found, pending = IMAGES.get(items)
            return self.send_json({"images": found, "pending": pending})
        if DEMO:
            return self.send_json({"stats": demo_stats(items), "pending": 0})
        found, pending = stats.predict_many(items)
        self.send_json({"stats": found, "pending": pending})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/config":
                self.send_json({"books": [{"key": k, "name": v} for k, v in TARGET_BOOKS.items()],
                                "coming_soon": COMING_SOON_BOOKS,
                                "demo": DEMO, "has_key": bool(API_KEY), "quota": _quota,
                                "prop_sports": list(PROP_MARKETS)})
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
                self.send_json(run_scan(target, sports, props, min_books))
            else:
                self.send_json({"error": "Not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 502)


def main():
    global DEMO, API_KEY, IMAGES
    ap = argparse.ArgumentParser(description="Betting value finder")
    ap.add_argument("--demo", action="store_true", help="use made-up sample data")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    API_KEY = load_api_key()
    DEMO = args.demo or not API_KEY
    IMAGES = ImageFinder()
    stats.warm_up()   # start downloading past results in the background
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
