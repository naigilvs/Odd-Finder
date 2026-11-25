"""Tests for the odds valuation in app.py. Run: ./run_tests.sh (no internet or API key needed)."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402

FUTURE = "2099-01-01T00:00:00Z"


def event(books, home="Home Team", away="Away Team"):
    """books: {key: {market: [outcome, ...]}}; the first book listed is the one analyzed."""
    return {"id": "ev1", "sport_key": "basketball_nba", "sport_title": "NBA", "commence_time": FUTURE,
            "home_team": home, "away_team": away,
            "bookmakers": [{"key": k, "title": k.title(), "markets": [{"key": m, "outcomes": o} for m, o in mk.items()]}
                           for k, mk in books.items()]}


class Basics(unittest.TestCase):
    def test_american_odds(self):
        self.assertEqual(app.american(2.5), "+150")
        self.assertEqual(app.american(1.5), "-200")
        self.assertEqual(app.american(2.0), "+100")

    def test_devig_removes_margin(self):
        fair = app.devig({"A": {"price": 1.909}, "B": {"price": 1.909}})
        self.assertAlmostEqual(fair["A"], 0.5)
        self.assertAlmostEqual(sum(fair.values()), 1.0)

    def test_both_sides_of_a_spread_share_a_line(self):
        home = app.line_key("spreads", {"name": "H", "point": -3.5}, "H")
        away = app.line_key("spreads", {"name": "A", "point": 3.5}, "H")
        other = app.line_key("spreads", {"name": "H", "point": -4.5}, "H")
        self.assertEqual(home, away)
        self.assertNotEqual(home, other)

    def test_market_labels(self):
        self.assertEqual(app.market_label("alternate_spreads"), "Alt spread")
        self.assertEqual(app.market_label("spreads_h1"), "1st half spread")
        self.assertEqual(app.market_label("totals_1st_5_innings"), "1st 5 innings total")
        self.assertEqual(app.market_label("alternate_team_totals"), "Alt team total")
        self.assertEqual(app.market_label("player_anytime_td"), "Anytime touchdown")

    def test_bet_names(self):
        self.assertEqual(app.describe("spreads", {"name": "Lakers", "point": -3.5}), "Lakers -3.5")
        self.assertEqual(app.describe("totals_h1", {"name": "Over", "point": 110.5}), "Over 110.5, 1st half")
        self.assertEqual(app.describe("team_totals", {"name": "Under", "point": 112.5, "description": "Lakers"}),
                         "Lakers Under 112.5")
        self.assertEqual(app.describe("player_assists", {"name": "Over", "point": 6.5, "description": "J. Smith"}),
                         "J. Smith Over 6.5 assists")
        self.assertEqual(app.describe("player_anytime_td", {"name": "Yes", "description": "J. Smith"}),
                         "J. Smith anytime touchdown: Yes")
        self.assertEqual(app.describe("batter_home_runs", {"name": "Over", "point": 0.5, "description": "A. Judge"}),
                         "A. Judge home runs: Yes")

    def test_more_bets_groups_per_league(self):
        nba = {g["key"] for g in app.more_groups("basketball_nba")}
        self.assertEqual(nba, {"alt", "team", "periods", "props", "yesno"})
        soccer = {g["key"] for g in app.more_groups("soccer_epl")}
        self.assertEqual(soccer, {"alt", "team"})


class Valuation(unittest.TestCase):
    def test_value_uses_other_books_without_their_margin(self):
        ev = event({
            "fanduel": {"h2h": [{"name": "Home Team", "price": 2.2}, {"name": "Away Team", "price": 1.7}]},
            "pinnacle": {"h2h": [{"name": "Home Team", "price": 2.0}, {"name": "Away Team", "price": 1.9}]},
        })
        rows = {r["outcome"]: r for r in app.analyze_event(ev, "fanduel", 1)}
        fair_home = (1 / 2.0) / (1 / 2.0 + 1 / 1.9)
        self.assertAlmostEqual(rows["Home Team"]["fair_prob"], round(fair_home, 4))
        self.assertAlmostEqual(rows["Home Team"]["ev"], round(fair_home * 2.2 - 1, 4))
        self.assertAlmostEqual(rows["Home Team"]["book_prob"], round(1 / 2.2, 4))
        self.assertEqual(rows["Home Team"]["compare"][0]["book"], "Pinnacle")

    def test_lines_only_match_the_same_number(self):
        ev = event({
            "fanduel": {"spreads": [{"name": "Home Team", "point": -3.5, "price": 1.9}, {"name": "Away Team", "point": 3.5, "price": 1.9}]},
            "pinnacle": {"spreads": [{"name": "Home Team", "point": -4.5, "price": 1.9}, {"name": "Away Team", "point": 4.5, "price": 1.9}]},
        })
        self.assertEqual(app.analyze_event(ev, "fanduel", 1), [])    # nothing to compare with

    def test_min_books(self):
        ev = event({
            "fanduel": {"h2h": [{"name": "Home Team", "price": 2.0}, {"name": "Away Team", "price": 1.8}]},
            "pinnacle": {"h2h": [{"name": "Home Team", "price": 2.0}, {"name": "Away Team", "price": 1.9}]},
        })
        self.assertEqual(len(app.analyze_event(ev, "fanduel", 1)), 2)
        self.assertEqual(app.analyze_event(ev, "fanduel", 2), [])

    def test_yes_only_props_measure_margin_when_possible(self):
        dd = "player_double_double"
        ev = event({
            "fanduel": {dd: [{"name": "Yes", "description": "P1", "price": 3.0}]},
            # DraftKings offers Yes/No for P2, so its margin on this bet type can be measured.
            "draftkings": {dd: [{"name": "Yes", "description": "P1", "price": 2.8},
                                {"name": "Yes", "description": "P2", "price": 1.8},
                                {"name": "No", "description": "P2", "price": 2.1}]},
        })
        r = [r for r in app.analyze_event(ev, "fanduel", 1) if r["player"] == "P1"][0]
        margin = 1 / 1.8 + 1 / 2.1 - 1
        self.assertAlmostEqual(r["fair_prob"], round((1 / 2.8) / (1 + margin), 4))
        self.assertTrue(r["margin_measured"])
        self.assertFalse(r["estimated"])

    def test_yes_only_props_fall_back_to_a_guess(self):
        dd = "player_double_double"
        ev = event({"fanduel": {dd: [{"name": "Yes", "description": "P1", "price": 3.0}]},
                    "betmgm": {dd: [{"name": "Yes", "description": "P1", "price": 2.7}]}})
        r = app.analyze_event(ev, "fanduel", 1)[0]
        self.assertAlmostEqual(r["fair_prob"], round((1 / 2.7) / (1 + app.ONE_SIDED_MARGIN), 4))
        self.assertTrue(r["estimated"])

    def test_yes_no_margin_ignores_broken_lines(self):
        lines = {("m", "a", None): {"Yes": {"price": 1.9}, "No": {"price": 1.9}},      # 5.3% margin
                 ("m", "b", None): {"Yes": {"price": 1.01}, "No": {"price": 1.01}}}    # ~98%: ignored
        self.assertAlmostEqual(app.yes_no_margin(lines, "m"), 2 / 1.9 - 1)
        self.assertIsNone(app.yes_no_margin(lines, "other"))

    def test_team_totals_are_teams_not_players(self):
        ev = event({
            "fanduel": {"team_totals": [{"name": "Over", "description": "Home Team", "point": 110.5, "price": 1.9},
                                        {"name": "Under", "description": "Home Team", "point": 110.5, "price": 1.9}]},
            "pinnacle": {"team_totals": [{"name": "Over", "description": "Home Team", "point": 110.5, "price": 1.95},
                                         {"name": "Under", "description": "Home Team", "point": 110.5, "price": 1.85}]},
        })
        r = app.analyze_event(ev, "fanduel", 1)[0]
        self.assertIsNone(r["player"])
        self.assertEqual(r["team_total_for"], "Home Team")
        self.assertEqual(r["pick_team"], "Home Team")


class Server(unittest.TestCase):
    """The More bets endpoints, against the demo data (no internet)."""

    @classmethod
    def setUpClass(cls):
        app.DEMO, app.API_KEY = True, None
        cls.tmp = tempfile.TemporaryDirectory()
        app.PICKS = app.picks.PickStore(Path(cls.tmp.name) / "picks.json")   # never touch real picks
        app.TRACK = app.picks.TrackStore(Path(cls.tmp.name) / "track.json")
        app.WAGERS = app.wagers.WagerStore(Path(cls.tmp.name) / "wagers.json")
        app.SCANS = app.scans.ScanStore(Path(cls.tmp.name) / "scans.json")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Odd-Finder": "1"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_saving_picks(self):
        bet = {"event_id": "e9", "sport_key": "basketball_nba", "sport": "NBA", "event": "A @ H", "home_team": "H",
               "away_team": "A", "start": FUTURE, "market": "h2h", "market_label": "Moneyline", "selection": "H",
               "outcome": "H", "american": "+110", "decimal": 2.1}
        status, body = self.post("/api/picks", {"action": "save", "pick": bet, "stake": 20})
        self.assertEqual(status, 200)
        pid = body["pick"]["id"]
        self.assertEqual(len(self.post("/api/picks", {"action": "list"})[1]["picks"]), 1)
        self.assertEqual(self.post("/api/picks", {"action": "update", "id": pid, "status": "won"})[1]["picks"][0]["status"], "won")
        self.assertEqual(self.post("/api/picks", {"action": "update", "id": "nope", "status": "won"})[0], 400)
        self.assertEqual(self.post("/api/picks", {"action": "save", "pick": {"selection": "x"}})[0], 400)
        self.assertEqual(self.post("/api/picks", {"action": "delete", "id": pid})[1]["picks"], [])

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers={"X-Odd-Finder": "1", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_games_list(self):
        status, body = self.get("/api/games?sport=basketball_nba")
        self.assertEqual(status, 200)
        self.assertTrue(body["games"])
        self.assertTrue(body["groups"])

    def test_bad_league_is_refused(self):
        self.assertEqual(self.get("/api/games?sport=../../etc")[0], 400)

    def test_more_scan(self):
        games = self.get("/api/games?sport=basketball_nba")[1]["games"]
        status, body = self.get(f"/api/more-scan?book=fanduel&sport=basketball_nba&events={games[0]['id']}"
                                f"&markets=alternate_spreads,team_totals,not_a_market")
        self.assertEqual(status, 200)
        self.assertTrue(body["rows"])
        self.assertEqual({r["market"] for r in body["rows"]}, {"alternate_spreads", "team_totals"})

    def test_more_scan_limits(self):
        many = ",".join(f"g{i}" for i in range(app.MORE_MAX_GAMES + 1))
        self.assertEqual(self.get(f"/api/more-scan?book=fanduel&sport=basketball_nba&events={many}&markets=team_totals")[0], 400)
        self.assertEqual(self.get("/api/more-scan?book=fanduel&sport=basketball_nba&events=g1&markets=not_a_market")[0], 400)

    def test_track_record(self):
        bet = {"event_id": "t1", "sport_key": "basketball_nba", "sport": "NBA", "event": "A @ H", "home_team": "H",
               "away_team": "A", "start": FUTURE, "market": "spreads", "market_label": "Point spread", "selection": "H -2.5",
               "outcome": "H", "point": -2.5, "american": "-110", "decimal": 1.91, "chance": 0.55, "ev": 0.02}
        self.assertEqual(self.post("/api/track", {"action": "log", "bets": [bet]})[1], {"added": 1, "updated": 0})
        summary = self.post("/api/track", {"action": "summary"})[1]["summary"]
        self.assertEqual((summary["tracked"], summary["pending"]), (1, 1))
        self.assertEqual(self.post("/api/track", {"action": "clear"})[1]["summary"]["tracked"], 0)
        self.assertEqual(self.post("/api/track", {"action": "explode"})[0], 400)

    def test_scans_are_saved_by_the_bot(self):
        self.post("/api/saved-scans", {"action": "clear_more", "book": "fanduel"})   # other tests share this file
        self.assertEqual(self.get("/api/saved-scans?book=fanduel")[1]["more"], [])
        status, body = self.get("/api/scan?book=fanduel&sports=basketball_nba&min_books=1")
        self.assertEqual(status, 200)
        saved = self.get("/api/saved-scans?book=fanduel")[1]
        self.assertEqual(len(saved["results"]["basketball_nba"]["rows"]), body["count"])
        games = self.get("/api/games?sport=basketball_nba")[1]["games"]
        self.get(f"/api/more-scan?book=fanduel&sport=basketball_nba&events={games[0]['id']}&markets=team_totals")
        self.assertTrue(self.get("/api/saved-scans?book=fanduel")[1]["more"])
        self.assertEqual(self.post("/api/saved-scans", {"action": "clear_more", "book": "fanduel"})[0], 200)
        self.assertEqual(self.get("/api/saved-scans?book=fanduel")[1]["more"], [])
        self.assertEqual(self.get("/api/saved-scans?book=nope")[0], 400)

    def test_other_websites_are_refused(self):
        self.assertEqual(self.get("/api/config", {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.get("/api/config", {"Host": f"evil.example:{self.port}"})[0], 403)   # DNS rebinding
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/scan?book=fanduel&sports=basketball_nba")
        with self.assertRaises(urllib.error.HTTPError) as no_marker:                               # e.g. an <img> tag
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(no_marker.exception.code, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/track", data=b'{"action":"clear"}',
                                     headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as post:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(post.exception.code, 403)

    def test_own_page_still_loads_and_its_origin_is_allowed(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.get("/api/config", {"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_bad_requests_get_an_answer(self):
        status, body = self.get("/api/scan?book=fanduel&sports=basketball_nba&min_books=abc")
        self.assertEqual(status, 400)
        self.assertIn("Bad request", body["error"])

    def test_only_one_live_copy_uses_the_data(self):
        import socket
        lock = Path(self.tmp.name) / "running.lock"
        self.assertIsNone(app.claim_data(lock, 1234))                     # first copy claims it
        self.assertIsNone(app.claim_data(lock, 1234))                     # same copy again: fine
        lock.write_text(json.dumps({"pid": 1, "port": self.port}))        # another live copy (pid 1 always exists)
        self.assertEqual(app.claim_data(lock, 5555), self.port)
        with socket.socket() as s:                                         # a leftover lock for a port nobody serves
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        lock.write_text(json.dumps({"pid": 1, "port": dead_port}))
        self.assertIsNone(app.claim_data(lock, 5555))

    def test_wagers(self):
        status, body = self.post("/api/wagers", {"action": "add", "wager": {"date": "2026-11-20", "wagered": 25, "result": "won", "won": 30}})
        self.assertEqual(status, 200)
        wid = body["wagers"][0]["id"]
        self.assertEqual(self.post("/api/wagers", {"action": "add", "wager": {"wagered": 0, "result": "lost"}})[0], 400)
        self.assertEqual(len(self.post("/api/wagers", {"action": "list"})[1]["wagers"]), 1)
        self.assertEqual(self.post("/api/wagers", {"action": "delete", "id": wid})[1]["wagers"], [])

    def test_config_reports_freshness(self):
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(body["outdated"])


if __name__ == "__main__":
    unittest.main()
