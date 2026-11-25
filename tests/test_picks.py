"""Tests for saved picks and automatic grading (picks.py). No internet needed."""
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
        self.assertFalse(picks.auto_gradable(pick(sport_key="soccer_epl")))              # league not covered


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


if __name__ == "__main__":
    unittest.main()
