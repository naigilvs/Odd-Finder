"""Tests for the injury report (injuries.py), using a small made-up ESPN response."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import injuries  # noqa: E402

FEED = {"timestamp": "2026-11-26T04:35:27Z", "injuries": [
    {"displayName": "Atlanta Hawks", "injuries": [
        {"status": "Out", "shortComment": "Hip; won't play Sunday.", "date": "2026-11-21T22:12Z",
         "athlete": {"displayName": "Jock Landale", "position": {"abbreviation": "C"}}, "details": {"returnDate": "2026-11-27"}},
        {"status": "Day-To-Day", "shortComment": "Ankle.", "athlete": {"displayName": "Jaren Jackson Jr."}},
        {"status": "Active", "athlete": {"displayName": "Healthy Guy"}},        # recovered: left out
    ]},
    {"displayName": "Boston Celtics", "injuries": [
        {"status": "Doubtful", "athlete": {"displayName": "Jock Landale"}},     # same name, other team
        {"status": "Injured Reserve", "athlete": {"displayName": "Zed Long"}},
    ]},
]}


class InjuryReport(unittest.TestCase):
    def setUp(self):
        self.rep = injuries.parse(FEED)

    def test_parse_keeps_only_injured_players_with_levels(self):
        names = [(p["name"], p["team"], p["level"]) for p in self.rep["players"]]
        self.assertEqual(names, [("Jock Landale", "Atlanta Hawks", "out"), ("Jaren Jackson Jr.", "Atlanta Hawks", "question"),
                                 ("Jock Landale", "Boston Celtics", "doubtful"), ("Zed Long", "Boston Celtics", "out")])
        self.assertEqual(self.rep["players"][0]["returns"], "2026-11-27")

    def test_player_matched_by_name_and_team(self):
        p = injuries.find_player(self.rep, "Jaren Jackson Jr", "Atlanta Hawks", "Miami Heat")    # no period: same player
        self.assertEqual(p["level"], "question")
        self.assertEqual(injuries.find_player(self.rep, "Jock Landale", "Celtics", "Heat")["level"], "doubtful")
        self.assertIsNone(injuries.find_player(self.rep, "Jock Landale", "Miami Heat", "Chicago Bulls"))   # not in this game

    def test_lookup_props_and_game_bets(self):
        res = injuries.lookup([
            {"key": "a", "sport_key": "basketball_nba", "player": "Jock Landale", "home": "Atlanta Hawks", "away": "Miami Heat"},
            {"key": "b", "sport_key": "basketball_nba", "player": "Nobody", "home": "Atlanta Hawks", "away": "Miami Heat"},
            {"key": "c", "sport_key": "basketball_nba", "home": "Boston Celtics", "away": "Atlanta Hawks"},
            {"key": "d", "sport_key": "soccer_epl", "player": "X", "home": "Arsenal", "away": "Chelsea"},   # no report
            {"key": 5, "sport_key": "basketball_nba"},
        ], get_report=lambda s: self.rep)
        inj = res["injuries"]
        self.assertEqual(inj["a"]["player"]["status"], "Out")
        self.assertEqual(inj["b"], {})
        self.assertEqual(inj["c"]["teams"], ["basketball_nba|Boston Celtics", "basketball_nba|Atlanta Hawks"])
        self.assertEqual([p["name"] for p in res["teams"]["basketball_nba|Boston Celtics"]], ["Zed Long", "Jock Landale"])  # out first
        self.assertNotIn("d", inj)
        self.assertEqual(set(inj), {"a", "b", "c"})
        self.assertEqual(res["updated"], {"basketball_nba": "2026-11-26T04:35:27Z"})

    def test_report_is_cached_and_survives_a_download_failure(self):
        injuries._cache.clear()
        with mock.patch.object(injuries, "_download", return_value=FEED) as dl:
            injuries.report("basketball_nba", now=1000)
            injuries.report("basketball_nba", now=1000 + 60)
            self.assertEqual(dl.call_count, 1)                                   # second ask uses the cache
        with mock.patch.object(injuries, "_download", side_effect=OSError("offline")):
            rep = injuries.report("basketball_nba", now=1000 + injuries.REFRESH_SECONDS + 1)
        self.assertEqual(len(rep["players"]), 4)                                 # last good report kept
        self.assertIsNone(injuries.report("soccer_epl"))
        injuries._cache.clear()


if __name__ == "__main__":
    unittest.main()
