"""Tests for the chatbot (chatbot.py) and its server answer (app.chat_answer), with a small
made-up list of players and teams so they don't depend on downloaded data."""
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import chatbot  # noqa: E402

RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()


def p(name, team, sport="basketball_nba"):
    return chatbot._key(name), {"name": name, "team": team, "date": RECENT, "sport": sport}


PLAYERS = {
    "basketball_nba": dict([p("Nikola Jokic", "Denver Nuggets"), p("LeBron James", "Los Angeles Lakers"),
                            p("Jaylen Brown", "Boston Celtics"), p("Bruce Brown", "Denver Nuggets"),
                            p("Jayson Tatum", "Boston Celtics"), p("Stephen Curry", "Golden State Warriors")]),
    "icehockey_nhl": dict([p("Connor McDavid", "Edmonton Oilers", "icehockey_nhl"), p("Cam York", "Philadelphia Flyers", "icehockey_nhl")]),
    "americanfootball_nfl": dict([p("Josh Allen", "Buffalo Bills", "americanfootball_nfl")]),
}
TEAMS = {
    "basketball_nba": {"Denver Nuggets": RECENT, "Utah Jazz": RECENT, "Boston Celtics": RECENT, "New York Knicks": RECENT,
                       "Los Angeles Lakers": RECENT, "Golden State Warriors": RECENT},
    "icehockey_nhl": {"New York Rangers": RECENT, "Edmonton Oilers": RECENT},
    "baseball_mlb": {"Texas Rangers": OLD},                                     # off-season
    "americanfootball_nfl": {"Buffalo Bills": RECENT, "Kansas City Chiefs": RECENT},
    "soccer_epl": {"Arsenal": RECENT, "Manchester United": RECENT, "Manchester City": RECENT},
}


class Reading(unittest.TestCase):
    def setUp(self):
        self.saved = dict(chatbot._index)
        chatbot._index.update(at=time.time(), players=PLAYERS, teams=TEAMS)

    def tearDown(self):
        chatbot._index.update(self.saved)

    def bet(self, text):
        out = chatbot.parse(text)
        return out["intent"], (out.get("bets") or [{}])[0]

    def test_player_props(self):
        intent, b = self.bet("Jokic over 11.5 rebounds -120")
        self.assertEqual((intent, b["player"], b["market"], b["outcome"], b["point"], b["odds"]),
                         ("check", "Nikola Jokic", "player_rebounds", "Over", 11.5, -120))
        self.assertAlmostEqual(b["decimal"], 1.8333, places=3)
        self.assertEqual(self.bet("lebron o24.5 pts −115")[1]["player"], "LeBron James")      # first name, unicode minus
        self.assertEqual(self.bet("steph u4.5 threes +105")[1]["outcome"], "Under")            # nickname
        self.assertEqual(self.bet("jokich over 25.5 points")[1]["player"], "Nikola Jokic")    # typo
        self.assertEqual(self.bet("mcdavid over 3.5 shots")[1]["market"], "player_shots_on_goal")
        self.assertEqual(self.bet("josh allen over 245.5 passing yards")[1]["market"], "player_pass_yds")

    def test_team_bets(self):
        self.assertEqual(self.bet("Knicks -4.5")[1]["market"], "spreads")
        self.assertEqual(self.bet("Knicks -4.5")[1]["point"], -4.5)
        intent, b = self.bet("Lakers ML +130")
        self.assertEqual((b["market"], b["odds"]), ("h2h", 130))
        intent, b = self.bet("celtics knicks over 224.5")
        self.assertEqual((b["market"], b["team"], b["opponent"], b["point"]), ("totals", "Boston Celtics", "New York Knicks", 224.5))
        self.assertEqual(self.bet("rangers ml")[1]["team"], "New York Rangers")               # in season beats MLB
        self.assertEqual(self.bet("arsenal ml")[1]["sport_key"], "soccer_epl")
        self.assertEqual(self.bet("manchester ml")[1].get("team"), None)                     # two Manchester teams

    def test_questions(self):
        cases = {"best nba props tonight": "best", "how am i doing": "record", "bankroll": "bankroll",
                 "my picks": "picks", "is the bot working": "bot", "how much should i bet": "stake",
                 "what's a push": "term", "what does +150 mean": "term", "is lebron playing": "injury",
                 "mcdavid last 10": "stats", "Jayson Tatum": "about", "hello": "help", "asdf qwer": "unknown",
                 "Tatum over 27.5 points or Brown over 22.5 points": "compare", "over 224.5": "check",
                 # questions people actually asked that it first didn't understand
                 "can u talk to me": "talk", "any nba?": "best", "nba picks": "best", "what about nfl": "best",
                 "give me nfl picks": "best", "why": "why", "check a bet": "checkhelp",
                 "any suggestions?": "best", "news": "faq", "are the props out?": "faq"}
        for text, intent in cases.items():
            self.assertEqual(chatbot.parse(text)["intent"], intent, text)
        self.assertEqual(chatbot.parse("best nba props")["sports"], ["basketball_nba"])
        self.assertEqual(chatbot.parse("what is kelly")["term"], "kelly")
        self.assertIn("Which game", chatbot.parse("over 224.5")["bets"][0]["problem"])

    def test_ways_of_asking_for_picks(self):
        def ask(text):
            out = chatbot.parse(text)
            return out["intent"], out.get("mode"), out.get("one"), out.get("window"), out["sports"]
        self.assertEqual(ask("what should i bet on tonight"), ("best", "value", True, "today", []))
        self.assertEqual(ask("best nba picks"), ("best", "value", False, None, ["basketball_nba"]))
        self.assertEqual(ask("safest bets tonight")[:2], ("best", "likely"))
        self.assertEqual(ask("any locks?")[:2], ("best", "likely"))
        self.assertEqual(ask("nba favorites")[1], "likely")
        self.assertEqual(ask("picks where both agree")[1], "agree")
        self.assertEqual(ask("closest to fair price")[1], "closest")
        self.assertEqual(ask("what do u recommend")[0], "best")
        self.assertEqual(ask("give me a pick")[2], True)
        self.assertEqual(ask("any good bets tomorrow")[3], "tomorrow")
        for text in ("more", "show more", "what else", "next"):
            self.assertEqual(chatbot.parse(text)["intent"], "more", text)
        # a recommendation question still works mid-conversation about another bet
        ctx = {"kind": "player", "sport_key": "basketball_nba", "player": "Nikola Jokic", "team": "Denver Nuggets"}
        self.assertEqual(chatbot.parse("what should i bet on", ctx)["intent"], "best")

    def test_scoped_requests_yes_no_and_new_topics(self):
        def ask(text):
            out = chatbot.parse(text)
            return out["intent"], out
        intent, out = ask("best bet in the knicks game")
        self.assertEqual((intent, out["scope"]["team"]), ("best", "New York Knicks"))
        intent, out = ask("best bet for lebron")
        self.assertEqual((intent, out["scope"]["player"]), ("best", "LeBron James"))
        self.assertEqual(ask("what should i bet on in the knicks game")[1]["scope"]["team"], "New York Knicks")
        self.assertIsNone(ask("best nba bet")[1]["scope"])
        intent, out = ask("should i bet the knicks")
        self.assertEqual((intent, out["bets"][0]["market"], out["bets"][0]["yesno"]), ("check", "h2h", True))
        self.assertTrue(ask("would you take knicks -4.5")[1]["bets"][0]["yesno"])
        self.assertEqual(ask("should i bet on lebron")[0], "about")                  # which prop? asks
        self.assertEqual(ask("when do the lakers play")[0], "schedule")
        self.assertEqual(ask("who plays tonight")[0], "schedule")
        self.assertEqual(ask("did the lakers win")[0], "result")
        self.assertEqual(ask("lakers to win +130")[0], "check")                      # a bet, not a result
        self.assertEqual(ask("who will win celtics vs knicks")[0], "winner")
        for text, topic in (("any tips?", "win"), ("how does the bot work", "how_work"), ("are parlays worth it", "parlays"),
                            ("i keep losing", "losing"), ("is betting worth it", "worth")):
            self.assertEqual((ask(text)[0], ask(text)[1]["topic"]), ("faq", topic), text)
        for text in ("how are you", "who are you", "lol", "bye"):
            self.assertEqual(ask(text)[0], "smalltalk", text)
        intent, out = ask("the weather today")
        self.assertEqual(intent, "unknown")
        self.assertTrue(out["suggestions"])

    def test_city_words_and_ambiguous_team_names(self):
        out = chatbot.parse("new york ml")                                       # not the player Cam York
        self.assertEqual((out["intent"], out["bets"][0]["choices"]), ("choose", ["New York Knicks", "New York Rangers"]))
        self.assertEqual(chatbot.parse("cam york over 0.5 points")["bets"][0]["player"], "Cam York")
        out = chatbot.parse("manchester ml")
        self.assertEqual(out["bets"][0]["choices"], ["Manchester City", "Manchester United"])
        self.assertEqual(chatbot.parse("manchester city ml")["bets"][0]["team"], "Manchester City")
        self.assertEqual(chatbot.parse("jaquars ml")["intent"], "check") if "Jacksonville Jaguars" in TEAMS.get("americanfootball_nfl", {}) else None
        self.assertEqual(chatbot.parse("nugets -3.5")["bets"][0]["team"], "Denver Nuggets")      # typo
        self.assertEqual(chatbot.parse("are u connected to gemini")["topic"], "ai")

    def test_shared_last_names(self):
        out = chatbot.parse("brown over 12.5 points")
        self.assertEqual(out["intent"], "choose")
        self.assertEqual(len(out["bets"][0]["choices"]), 2)
        # next to a Celtics player, "Brown" is Jaylen Brown
        cmp = chatbot.parse("Tatum over 27.5 points or Brown over 22.5 points")["bets"]
        self.assertEqual([b["player"] for b in cmp], ["Jayson Tatum", "Jaylen Brown"])

    def test_follow_ups_use_the_last_bet(self):
        jokic = {"kind": "player", "sport_key": "basketball_nba", "player": "Nikola Jokic", "team": "Denver Nuggets",
                 "market": "player_rebounds", "outcome": "Over", "point": 11.5, "odds": -120, "event_id": "g1"}
        knicks = {"kind": "team", "sport_key": "basketball_nba", "team": "New York Knicks", "opponent": "Boston Celtics",
                  "market": "spreads", "outcome": "New York Knicks", "point": -4.5, "odds": -110}

        def f(text, ctx):
            out = chatbot.parse(text, ctx)
            b = (out.get("bets") or [{}])[0]
            return out["intent"], b.get("outcome"), b.get("point"), b.get("odds"), b.get("market")
        self.assertEqual(f("what about the under", jokic), ("check", "Under", 11.5, None, "player_rebounds"))  # new side: old odds dropped
        self.assertEqual(f("and at -105", jokic), ("check", "Over", 11.5, -105, "player_rebounds"))
        self.assertEqual(f("what if the line was 12.5", jokic)[:3], ("check", "Over", 12.5))
        self.assertEqual(f("his assists over 9.5", jokic), ("check", "Over", 9.5, None, "player_assists"))
        self.assertEqual(f("his assists", jokic)[0], "form")                                   # no line: show his assists
        self.assertEqual(f("is he playing", jokic)[0], "injury")
        self.assertEqual(f("last 10", jokic)[0], "form")
        self.assertEqual(f("how much should i bet on the under", jokic)[:2], ("stake", "Under"))
        self.assertEqual(f("the other side", knicks), ("check", "Boston Celtics", 4.5, None, "spreads"))
        self.assertEqual(f("what about 3.5", knicks)[:3], ("check", "New York Knicks", -3.5))
        self.assertEqual(f("ml +120", knicks), ("check", "New York Knicks", None, 120, "h2h"))
        self.assertEqual(f("over 224.5", knicks)[1:], ("Over", 224.5, None, "totals"))
        for text, intent in (("why", "why"), ("save it", "save"), ("best bets in this game", "best"), ("thanks", "thanks")):
            self.assertEqual(chatbot.parse(text, jokic)["intent"], intent, text)
        cmp = chatbot.parse("compare that with jokic over 25.5 points", jokic)
        self.assertEqual((cmp["intent"], [b["market"] for b in cmp["bets"]]), ("compare", ["player_rebounds", "player_points"]))
        # without a last bet, follow-ups aren't guessed
        self.assertEqual(chatbot.parse("what about the under", None)["intent"], "unknown")
        self.assertEqual(chatbot.parse("why", None)["intent"], "why")                           # the page asks for a bet
        self.assertIsNone(chatbot.clean_ctx({"kind": "player", "sport_key": "x"}))             # no player or team
        self.assertNotIn("evil", chatbot.clean_ctx(dict(jokic, evil="<script>")))

    def test_unknown_questions_are_kept(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chatbot, "UNKNOWN_FILE", Path(d) / "u.json"):
            for i in range(3):
                chatbot.log_unknown(f"question {i}")
            self.assertIn("question 2", chatbot.UNKNOWN_FILE.read_text())


class ServerAnswer(unittest.TestCase):
    def setUp(self):
        self.saved = dict(chatbot._index)
        chatbot._index.update(at=time.time(), players=PLAYERS, teams=TEAMS)
        start = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        self.games = [{"id": "g1", "home_team": "Utah Jazz", "away_team": "Denver Nuggets", "commence_time": start, "sport_title": "NBA"}]

    def tearDown(self):
        chatbot._index.update(self.saved)

    def run_chat(self, text, predict=None):
        predict = predict or (lambda items: ({"chat": {"prob": 0.6, "expected_stat": 12.4, "stat_name": "rebounds"}}, 0))
        with mock.patch.object(app, "upcoming_games", return_value=self.games), \
                mock.patch.object(app.stats, "predict_many", side_effect=predict), \
                mock.patch.object(app.injuries, "lookup", return_value={"injuries": {}, "teams": {}, "updated": {}}), \
                mock.patch.object(chatbot, "player_form", return_value=None), \
                mock.patch.object(chatbot, "team_form", return_value=None), \
                mock.patch.object(app, "RUNDOWN_KEY", None):
            return app.chat_answer(text)

    def test_a_prop_gets_its_game_and_stats(self):
        b = self.run_chat("jokic over 11.5 rebounds -120")["bets"][0]
        self.assertEqual((b["event_id"], b["event"], b["selection"]), ("g1", "Denver Nuggets @ Utah Jazz", "Nikola Jokic Over 11.5 rebounds"))
        self.assertEqual(b["stats"]["prob"], 0.6)

    def test_team_bet_uses_the_games_spelling(self):
        b = self.run_chat("nuggets -4.5", lambda items: ({"chat": {"prob": 0.55}}, 0))["bets"][0]
        self.assertEqual((b["outcome"], b["selection"], b["opponent"]), ("Denver Nuggets", "Denver Nuggets -4.5", "Utah Jazz"))

    def test_teams_that_dont_play_each_other(self):
        b = self.run_chat("who will win nuggets vs knicks")["bets"][0]
        self.assertIn("don't play each other soon", b["error"])

    def test_schedule_and_result(self):
        with mock.patch.object(app, "upcoming_games", return_value=self.games):
            out = app.chat_answer("when do the nuggets play")
        self.assertEqual([g["event_id"] for g in out["games"]], ["g1"])
        game = {"id": "x", "date": "2026-11-20T02:00Z", "home": "Utah Jazz", "away": "Denver Nuggets", "hs": 99, "as": 120}
        fake = mock.MagicMock()
        fake.store.games.return_value = [game]
        with mock.patch.dict(app.stats._leagues, {"basketball_nba": fake}):
            r = app.chat_result({"kind": "team", "sport_key": "basketball_nba", "team": "Denver Nuggets"})
        self.assertEqual((r["won"], r["us"], r["them"], r["home"]), (True, 120, 99, False))

    def test_no_game_found(self):
        b = self.run_chat("celtics ml")["bets"][0]
        self.assertIn("couldn't find an upcoming Boston Celtics game", b["error"])


if __name__ == "__main__":
    unittest.main()


class PastStats(unittest.TestCase):
    """Player and team stats from saved games, with made-up games."""

    def setUp(self):
        self.saved = dict(chatbot._index)
        chatbot._index.update(at=time.time(), players=PLAYERS, teams=TEAMS)
        g = lambda i, date, season, home, away, pts, reb: (f"g{i}", {
            "date": date, "season": season, "home": home, "away": away,
            "players": [{"name": "Nikola Jokic", "team": "Denver Nuggets", "min": 35, "pts": pts, "reb": reb, "ast": 9, "3pm": 1}]})
        self.box = dict([g(1, "2026-11-20T02:00Z", 2027, "Denver Nuggets", "Boston Celtics", 30, 12),
                         g(2, "2026-11-22T02:00Z", 2027, "Utah Jazz", "Denver Nuggets", 20, 15),
                         g(3, "2026-03-01T02:00Z", 2026, "Boston Celtics", "Denver Nuggets", 40, 10)])
        self.results = [{"id": "r1", "date": "2026-11-20T02:00Z", "season": 2027, "home": "Denver Nuggets", "away": "Boston Celtics", "hs": 120, "as": 110},
                        {"id": "r2", "date": "2026-11-22T02:00Z", "season": 2027, "home": "Utah Jazz", "away": "Denver Nuggets", "hs": 115, "as": 101},
                        {"id": "r3", "date": "2026-03-01T02:00Z", "season": 2026, "home": "Boston Celtics", "away": "Denver Nuggets", "hs": 99, "as": 104}]

    def tearDown(self):
        chatbot._index.update(self.saved)

    def test_player_stats(self):
        with mock.patch.object(chatbot.props_stats.LIVE["basketball_nba"].store, "games", self.box):
            st = chatbot.player_stats("basketball_nba", "Nikola Jokic", opponent="Boston Celtics", n=2, best_stat="pts")
        self.assertEqual([s["season"] for s in st["seasons"]], ["2026-27", "2025-26"])
        self.assertEqual((st["seasons"][0]["games"], st["seasons"][0]["avg"]["pts"], st["seasons"][0]["high"]["reb"]), (2, 25.0, 15))
        self.assertEqual([g["date"][:10] for g in st["log"]], ["2026-11-22", "2026-11-20"])      # newest first, each game
        self.assertEqual((st["log"][0]["opp"], st["log"][0]["home"], st["log"][0]["reb"]), ("Utah Jazz", False, 15))
        self.assertEqual((len(st["vs"]["games"]), st["vs"]["avg"]["pts"]), (2, 35.0))            # both Celtics games
        self.assertEqual(st["best"]["games"][0]["pts"], 40)

    def test_team_stats(self):
        fake = mock.MagicMock()
        fake.store.games.return_value = self.results
        with mock.patch.dict(chatbot.stats._leagues, {"basketball_nba": fake}):
            st = chatbot.team_stats("basketball_nba", "Denver Nuggets", opponent="Boston Celtics", n=5)
        self.assertEqual([(s["season"], s["record"], s["home"], s["away"]) for s in st["seasons"]],
                         [("2026-27", "1-1", "1-0", "0-1"), ("2025-26", "1-0", "0-0", "1-0")])
        self.assertEqual((st["log"][0]["result"], st["streak"]), ("L", "L1"))
        self.assertEqual((st["h2h"]["record"], len(st["h2h"]["games"])), ("2-0", 2))

    def test_stats_questions(self):
        def ask(text):
            out = chatbot.parse(text)
            b = (out.get("bets") or [{}])[0]
            return out["intent"], b.get("player") or b.get("team"), b.get("opponent"), out.get("n"), out.get("best")
        self.assertEqual(ask("jokic stats")[:2], ("stats", "Nikola Jokic"))
        self.assertEqual(ask("jokic last 5 games")[3], 5)
        self.assertEqual(ask("jokic last night")[3], 1)
        self.assertEqual(ask("jokic vs celtics")[:3], ("stats", "Nikola Jokic", "Boston Celtics"))
        self.assertEqual(ask("jokic best rebounding games")[4], "reb")
        self.assertEqual(ask("knicks vs celtics history")[:3], ("stats", "New York Knicks", "Boston Celtics"))
        self.assertEqual(ask("celtics vs knicks history")[1], "Boston Celtics")                  # typed order
        self.assertEqual(ask("nuggets record")[:2], ("stats", "Denver Nuggets"))
        self.assertEqual(ask("jokic over 11.5 rebounds")[0], "check")                             # a bet, not stats
        self.assertEqual(ask("did the nuggets win")[0], "result")
