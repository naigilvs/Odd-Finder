"""Tests for saved picks and automatic grading (picks.py). No internet needed."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import picks  # noqa: E402
import stats  # noqa: E402

PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def pick(**kw):
    base = {"event_id": "g1", "sport_key": "basketball_nba", "sport": "NBA", "event": "Away Team @ Home Team",
            "home_team": "Home Team", "away_team": "Away Team", "start": PAST, "market": "h2h",
            "market_label": "Moneyline", "selection": "Home Team", "outcome": "Home Team", "american": "+120",
            "decimal": 2.2, "chance": 0.5, "confidence": "High", "point": None}
    base.update(kw)
    return base


class Grading(unittest.TestCase):
    def test_moneyline(self):
        self.assertEqual(picks.grade(pick(), 110, 100), "won")
        self.assertEqual(picks.grade(pick(), 100, 110), "lost")
        self.assertEqual(picks.grade(pick(outcome="Away Team"), 100, 110), "won")
        self.assertEqual(picks.grade(pick(), 20, 20), "push")          # NFL tie on a 2-way moneyline

    def test_spread(self):
        sp = dict(market="spreads", outcome="Home Team")
        self.assertEqual(picks.grade(pick(point=-3.5, **sp), 110, 105), "won")
        self.assertEqual(picks.grade(pick(point=-5.5, **sp), 110, 105), "lost")
        self.assertEqual(picks.grade(pick(point=-5, **sp), 110, 105), "push")
        self.assertEqual(picks.grade(pick(market="alternate_spreads", outcome="Away Team", point=7.5), 110, 105), "won")

    def test_totals(self):
        self.assertEqual(picks.grade(pick(market="totals", outcome="Over", point=210.5), 110, 105), "won")
        self.assertEqual(picks.grade(pick(market="totals", outcome="Under", point=210.5), 110, 105), "lost")
        self.assertEqual(picks.grade(pick(market="alternate_totals", outcome="Under", point=215), 110, 105), "push")

    def test_team_totals(self):
        tt = dict(market="team_totals", outcome="Over", point=107.5)
        self.assertEqual(picks.grade(pick(team_total_for="Home Team", **tt), 110, 105), "won")
        self.assertEqual(picks.grade(pick(team_total_for="Away Team", **tt), 110, 105), "lost")

    def test_what_can_be_checked_automatically(self):
        self.assertTrue(picks.auto_gradable(pick()))
        self.assertTrue(picks.auto_gradable(pick(market="alternate_totals")))
        self.assertFalse(picks.auto_gradable(pick(market="spreads_h1")))                 # part of a game
        self.assertFalse(picks.auto_gradable(pick(market="player_points", player="P")))   # player prop
        self.assertFalse(picks.auto_gradable(pick(sport_key="soccer_netherlands_eredivisie")))   # league not covered


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = picks.PickStore(Path(self.dir.name) / "picks.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_save_keeps_one_copy_and_survives_restart(self):
        a = self.store.save(pick(), stake=25)
        b = self.store.save(pick())
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["stake"], 25)
        self.assertTrue(a["auto"])
        again = picks.PickStore(self.store.path)
        self.assertEqual(len(again.all()), 1)

    def test_save_ignores_unknown_fields_and_needs_details(self):
        p = self.store.save(dict(pick(), evil="<script>"))
        self.assertNotIn("evil", p)
        with self.assertRaises(ValueError):
            self.store.save({"selection": "Something"})

    def test_update_and_delete(self):
        p = self.store.save(pick(market="player_points", player="P", selection="P Over 20.5"))
        self.store.update(p["id"], {"stake": 10, "status": "won"})
        got = self.store.all()[0]
        self.assertEqual((got["stake"], got["status"], got["graded_by"]), (10, "won", "you"))
        self.store.update(p["id"], {"status": "pending", "stake": None})
        got = self.store.all()[0]
        self.assertEqual((got["stake"], got["status"]), (None, "pending"))
        self.store.delete(p["id"])
        self.assertEqual(self.store.all(), [])

    def test_grading_pending_picks(self):
        done = self.store.save(pick())
        upcoming = self.store.save(pick(event_id="g2", start=FUTURE))
        prop = self.store.save(pick(event_id="g3", market="player_points", player="P", selection="P Over 20.5"))
        calls = []

        def fake_find(sport, home, away, start, refresh=False):
            calls.append((sport, home, away))
            return (110, 104)

        graded = self.store.grade_pending(refresh=False, find=fake_find)
        by_id = {p["id"]: p for p in self.store.all()}
        self.assertEqual(graded, 1)
        self.assertEqual(by_id[done["id"]]["status"], "won")
        self.assertEqual(by_id[done["id"]]["final"], "Away Team 104, Home Team 110")
        self.assertEqual(by_id[upcoming["id"]]["status"], "pending")    # not played yet
        self.assertEqual(by_id[prop["id"]]["status"], "pending")        # marked by hand
        self.assertEqual(len(calls), 1)

    def test_unfinished_games_stay_pending(self):
        self.store.save(pick())
        self.assertEqual(self.store.grade_pending(refresh=False, find=lambda *a, **k: None), 0)
        self.assertEqual(self.store.all()[0]["status"], "pending")


class FinalScores(unittest.TestCase):
    def setUp(self):
        self.lg = stats._leagues["basketball_nba"]
        self.saved = self.lg.store.games
        self.lg.store.games = lambda: [
            {"id": "1", "date": "2026-11-20T00:30Z", "season": 2027, "home": "LA Clippers", "away": "Boston Celtics",
             "hs": 101.0, "as": 99.0, "neutral": False},
        ]

    def tearDown(self):
        self.lg.store.games = self.saved

    def test_matches_names_from_both_feeds(self):
        score = stats.find_final_score("basketball_nba", "Los Angeles Clippers", "Boston Celtics", "2026-11-20T00:40:00Z")
        self.assertEqual(score, (101.0, 99.0))

    def test_home_and_away_listed_the_other_way(self):
        score = stats.find_final_score("basketball_nba", "Boston Celtics", "Los Angeles Clippers", "2026-11-20T00:40:00Z")
        self.assertEqual(score, (99.0, 101.0))

    def test_wrong_day_or_league(self):
        self.assertIsNone(stats.find_final_score("basketball_nba", "Los Angeles Clippers", "Boston Celtics", "2026-11-23T00:40:00Z"))
        self.assertIsNone(stats.find_final_score("soccer_epl", "A", "B", "2026-11-20T00:40:00Z"))



class TrackRecord(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.track = picks.TrackStore(Path(self.dir.name) / "track.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_logs_gradable_bets_once_and_updates_them(self):
        a, u = self.track.log([pick(start=FUTURE, decimal=2.2, agree=True),
                               pick(start=FUTURE, market="player_points", player="P", selection="P Over 20.5"),  # not gradable
                               pick(start=PAST, event_id="old"),                                               # already started
                               "not a bet", {"selection": "missing fields"}])
        self.assertEqual((a, u), (1, 0))
        a, u = self.track.log([pick(start=FUTURE, decimal=2.4)])            # rescan with a new price
        self.assertEqual((a, u), (0, 1))
        bets = self.track.all()
        self.assertEqual(len(bets), 1)
        self.assertEqual(bets[0]["decimal"], 2.4)
        self.assertFalse(bets[0]["agree"])                                  # latest scan wins
        self.assertEqual(picks.TrackStore(self.track.path).all()[0]["decimal"], 2.4)   # saved to disk

    def test_keeps_the_starting_price_when_the_line_moves(self):
        self.track.log([pick(start=FUTURE, decimal=2.2, american="+120")])
        self.track.log([pick(start=FUTURE, decimal=2.0, american="+100", open_decimal=9.9)])   # page can't overwrite it
        b = self.track.all()[0]
        self.assertEqual((b["open_decimal"], b["open_american"]), (2.2, "+120"))
        self.assertEqual((b["decimal"], b["american"]), (2.0, "+100"))

    def test_finished_bets_are_not_changed_by_rescans(self):
        self.track.log([pick(start=FUTURE)])
        self.track.picks[0]["status"] = "won"
        self.assertEqual(self.track.log([pick(start=FUTURE, decimal=5.0)]), (0, 0))
        self.assertEqual(self.track.all()[0]["decimal"], 2.2)

    def test_grading_and_clearing(self):
        self.track.log([pick(start=FUTURE)])
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.assertEqual(self.track.grade_pending(refresh=False, find=lambda *a, **k: (100, 90), now=later), 1)
        self.assertEqual(self.track.all()[0]["status"], "won")
        self.track.clear()
        self.assertEqual(self.track.all(), [])

    def test_summary(self):
        def done(status, chance, ev, conf="High", agree=False, decimal=2.0, start="2026-11-20T00:00:00+00:00", **kw):
            return dict(pick(start=start, decimal=decimal, chance=chance, ev=ev, confidence=conf, **kw),
                        status=status, agree=agree)
        bets = [done("won", 0.55, 0.05, agree=True), done("lost", 0.65, 0.02), done("won", 0.35, -0.03, conf="Low", decimal=3.0),
                done("push", 0.5, 0.01), dict(pick(start=FUTURE), status="pending"),
                done("won", 0.6, 0.1, start="2025-01-01T00:00:00+00:00")]
        s = picks.track_summary(bets, since="2026-01-01T00:00:00+00:00", view="all")
        self.assertEqual((s["tracked"], s["pending"], s["settled"]), (5, 1, 4))
        self.assertEqual((s["all"]["wins"], s["all"]["losses"], s["all"]["pushes"]), (2, 1, 1))
        self.assertAlmostEqual(s["all"]["win_rate"], 2 / 3)
        self.assertAlmostEqual(s["all"]["expected"], (0.55 + 0.65 + 0.35) / 3)
        self.assertEqual(s["all"]["profit"], 100 - 100 + 200 + 0)
        self.assertEqual((s["value"]["bets"], s["value"]["profit"]), (3, 0))      # positive-value bets only
        self.assertEqual((s["agree"]["bets"], s["agree"]["profit"]), (1, 100))
        self.assertEqual([r["name"] for r in s["by_confidence"]], ["High", "Low"])
        self.assertEqual([r["name"] for r in s["calibration"]], ["30–40%", "50–60%", "60–70%"])
        self.assertEqual(s["by_type"][0]["name"], "Moneyline")
        self.assertEqual(len(picks.track_summary(bets, view="all")["recent"]), 5)              # all time includes 2025


def prop(**kw):
    base = dict(market="player_points", market_label="Player points", player="Devin Booker",
                selection="Devin Booker Over 20.5 points", outcome="Over", point=20.5)
    base.update(kw)
    return pick(**base)


class PropGrading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = picks.PickStore(Path(self.dir.name) / "picks.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_which_props_are_automatic(self):
        self.assertTrue(picks.auto_gradable(prop()))
        self.assertTrue(picks.auto_gradable(prop(market="player_threes")))
        self.assertFalse(picks.auto_gradable(prop(sport_key="americanfootball_nfl")))   # NFL props: by hand
        self.assertFalse(picks.auto_gradable(prop(market="player_double_double", outcome="Yes", point=None)))

    def test_over_under_and_push(self):
        self.assertEqual(picks.grade_prop(prop(), 27), "won")
        self.assertEqual(picks.grade_prop(prop(), 18), "lost")
        self.assertEqual(picks.grade_prop(prop(outcome="Under"), 18), "won")
        self.assertEqual(picks.grade_prop(prop(point=20.0), 20), "push")

    def test_graded_from_the_box_score(self):
        played = self.store.save(prop())
        sat = self.store.save(prop(event_id="g2", player="Bench Guy", selection="Bench Guy Over 5.5 points"))
        upcoming = self.store.save(prop(event_id="g3", start=FUTURE, selection="Later Over 9.5 points"))
        calls = []

        def fake_stat(sport, market, player, home, away, start, refresh=False):
            calls.append((market, player))
            return ("played", 27.0) if player == "Devin Booker" else ("did_not_play", None)

        graded = self.store.grade_pending(refresh=False, find=lambda *a, **k: None, find_stat=fake_stat)
        by_id = {p["id"]: p for p in self.store.all()}
        self.assertEqual(graded, 2)
        self.assertEqual((by_id[played["id"]]["status"], by_id[played["id"]]["final"]), ("won", "Devin Booker: 27 points"))
        self.assertEqual(by_id[sat["id"]]["status"], "push")                       # void: stake returned
        self.assertIn("didn't play", by_id[sat["id"]]["final"])
        self.assertEqual(by_id[upcoming["id"]]["status"], "pending")
        self.assertEqual(len(calls), 2)

    def test_props_saved_before_become_automatic(self):
        old = dict(prop(), id="old1", status="pending", auto=False, stake=None)
        self.store.path.write_text(json.dumps([old]))
        reloaded = picks.PickStore(self.store.path)
        self.assertTrue(reloaded.all()[0]["auto"])

    def test_track_record_logs_nba_props(self):
        track = picks.TrackStore(Path(self.dir.name) / "track.json")
        added, _ = track.log([prop(start=FUTURE), prop(start=FUTURE, sport_key="icehockey_nhl", event_id="n1"),
                              prop(start=FUTURE, sport_key="baseball_mlb", market="batter_hits", event_id="m1")])
        self.assertEqual(added, 2)                      # NBA and NHL props; MLB props aren't covered
        s = picks.track_summary([dict(track.all()[0], status="won")])           # the NBA one
        self.assertEqual(s["by_type"][0]["name"], "Player points")

    def test_nfl_player_missing_from_box_score_is_left_to_you(self):
        nfl = dict(sport_key="americanfootball_nfl", market="player_receptions", player="Quiet Receiver",
                   selection="Quiet Receiver Under 2.5 receptions", outcome="Under", point=2.5)
        saved = self.store.save(prop(**nfl))
        self.assertTrue(saved["auto"])
        unknown = lambda *a, **k: ("unknown", None)
        self.store.grade_pending(refresh=False, find=lambda *a, **k: None, find_stat=unknown)
        p = self.store.all()[0]
        self.assertEqual((p["status"], p["auto"]), ("pending", False))           # Won/Lost buttons for you
        track = picks.TrackStore(Path(self.dir.name) / "track.json")
        track.log([prop(start=FUTURE, **nfl)])
        track.grade_pending(refresh=False, find=lambda *a, **k: None, find_stat=unknown,
                            now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(days=3))
        self.assertEqual(track.all(), [])                                         # left out of the record


class PostponedGames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_no_result_after_3_days_is_handed_to_you(self):
        store = picks.PickStore(Path(self.dir.name) / "picks.json")
        long_ago = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        store.save(pick(start=long_ago))
        store.save(pick(event_id="g2"))                                   # 1 day ago: keep waiting
        nothing = lambda *a, **k: None
        self.assertEqual(store.grade_pending(refresh=False, find=nothing, find_stat=nothing), 1)
        by = {p["event_id"]: p for p in store.all()}
        self.assertEqual((by["g1"]["status"], by["g1"]["auto"]), ("pending", False))
        self.assertIn("postponed or cancelled", by["g1"]["final"])
        self.assertTrue(by["g2"]["auto"])
        self.assertEqual(store.grade_pending(refresh=False, find=nothing, find_stat=nothing), 0)   # not re-counted
        self.assertFalse(picks.PickStore(store.path).all()[0]["auto"])                            # stays yours after a restart

    def test_track_record_leaves_them_out(self):
        track = picks.TrackStore(Path(self.dir.name) / "track.json")
        track.log([pick(start=FUTURE)])
        later = datetime.now(timezone.utc) + timedelta(days=5)
        track.grade_pending(refresh=False, find=lambda *a, **k: None, find_stat=lambda *a, **k: None, now=later)
        self.assertEqual(track.all(), [])


class SoccerGrading(unittest.TestCase):
    def test_what_is_graded_automatically(self):
        epl = pick(sport_key="soccer_epl", outcome="Draw")
        self.assertTrue(picks.auto_gradable(epl))
        self.assertEqual(picks.grade(epl, 1, 1), "won")
        self.assertFalse(picks.auto_gradable(pick(sport_key="soccer_epl", market="spreads", point=-0.25)))   # split result
        self.assertFalse(picks.auto_gradable(pick(sport_key="soccer_uefa_champs_league")))                 # extra time


class BotSide(unittest.TestCase):
    """Bot results counts the bot's side of each bet: the side with the better value."""

    @staticmethod
    def side(sel, status, ev, fair, point=None, market="totals", event="g1", player=None, decimal=1.91, stats_prob=None):
        b = {"event_id": event, "market": market, "selection": sel, "point": point, "player": player, "status": status,
             "ev": ev, "fair_prob": fair, "chance": fair, "book_prob": 0.52, "decimal": decimal, "start": PAST,
             "sport": "NBA", "sport_key": "basketball_nba"}
        if stats_prob is not None:
            b["stats_prob"] = stats_prob
        return b

    def bets(self):
        s = self.side
        return [
            s("Over 220.5", "lost", 0.02, 0.53, 220.5, stats_prob=0.40), s("Under 220.5", "won", -0.06, 0.47, 220.5, stats_prob=0.60),
            s("Lakers -3.5", "won", -0.01, 0.51, -3.5, "spreads"), s("Celtics +3.5", "lost", -0.03, 0.49, 3.5, "spreads"),
            s("P Over 20.5", "won", -0.02, 0.50, 20.5, "player_points", player="P"),
            s("P Under 20.5", "lost", -0.04, 0.50, 20.5, "player_points", player="P"),
            s("Q Over 8.5", "won", -0.02, 0.50, 8.5, "player_points", player="Q"),        # other player: own bet
            s("Home", "pending", 0.01, 0.40, None, "h2h", "g2"), s("Away", "pending", -0.05, 0.35, None, "h2h", "g2"),
            s("Draw", "pending", -0.04, 0.25, None, "h2h", "g2"),                          # 3-way soccer moneyline
        ]

    def test_one_side_per_bet_with_the_best_value(self):
        chosen = {b["selection"] for b in picks.bot_sides(self.bets())}
        self.assertEqual(chosen, {"Over 220.5", "Lakers -3.5", "P Over 20.5", "Q Over 8.5", "Home"})

    def test_a_moved_line_is_still_one_bet(self):
        s = self.side
        moved = [s("Over 223.5", "lost", -0.02, 0.49, 223.5), s("Under 223.5", "won", -0.05, 0.51, 223.5),
                 s("Over 224.5", "lost", -0.06, 0.47, 224.5), s("Under 224.5", "won", -0.04, 0.53, 224.5)]
        self.assertEqual([b["selection"] for b in picks.bot_sides(moved)], ["Over 223.5"])
        self.assertEqual(picks.side_check(moved)[0]["bets"], 2)                      # two lines, each a pair

    def test_most_likely_side_can_differ_from_the_better_value_side(self):
        s = self.side
        bets = [s("Dog", "lost", 0.03, 0.40, None, "h2h", "g9"), s("Fav", "won", -0.04, 0.60, None, "h2h", "g9")]
        self.assertEqual([b["selection"] for b in picks.bot_sides(bets, by="chance")], ["Fav"])
        self.assertEqual([b["selection"] for b in picks.bot_sides(bets)], ["Dog"])
        self.assertEqual(picks.track_summary(bets)["view"], "side")                  # the default stays the value side
        likely = picks.track_summary(bets, view="likely")
        self.assertEqual((likely["view"], likely["all"]["wins"], likely["all"]["losses"]), ("likely", 1, 0))
        self.assertEqual(picks.track_summary(bets, view="side")["all"]["losses"], 1)
        self.assertEqual(picks.track_summary(bets, view="agree")["tracked"], 0)        # pick and favorite differ
        both = bets + [s("Home", "won", 0.02, 0.6, None, "h2h", "g8"), s("Away", "lost", -0.05, 0.4, None, "h2h", "g8")]
        agree = picks.track_summary(both, view="agree")
        self.assertEqual((agree["view"], agree["tracked"], agree["all"]["wins"]), ("agree", 1, 1))

    def test_views(self):
        side = picks.track_summary(self.bets(), view="side")
        self.assertEqual((side["view"], side["tracked"], side["settled"], side["pending"]), ("side", 5, 4, 1))
        self.assertEqual((side["all"]["wins"], side["all"]["losses"]), (3, 1))
        value = picks.track_summary(self.bets(), view="value")
        self.assertEqual((value["tracked"], value["settled"]), (2, 1))                 # Over 220.5 and Home
        every = picks.track_summary(self.bets(), view="all")
        self.assertEqual((every["view"], every["tracked"]), ("all", 10))
        self.assertEqual(picks.track_summary(self.bets(), view="nonsense")["view"], "all")

    def test_which_side_won(self):
        check = {r["name"]: r for r in picks.side_check([b for b in self.bets() if b["status"] != "pending"])}
        bot = check["Bot's pick (better value)"]
        self.assertEqual((bot["bets"], bot["right"]), (3, 2))                          # total lost, spread and prop won
        self.assertAlmostEqual(bot["profit"], 2 * 91 - 100)
        market = check["Market's side (higher fair chance)"]
        self.assertEqual((market["bets"], market["right"]), (2, 1))                    # the prop's 50/50 has no clear side
        self.assertEqual(check["Stats' side"]["bets"], 1)                              # only the total has stats
        self.assertEqual(check["Stats' side"]["right"], 1)


if __name__ == "__main__":
    unittest.main()
