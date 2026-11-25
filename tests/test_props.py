"""Tests for the NBA player props model (props_stats.py), with made-up box scores."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import props_stats as P  # noqa: E402

KEYS = P.SPORTS["basketball_nba"]["labels"]


def season(seed=4, n_games=500):
    """Two made-up seasons: each player has a true scoring rate; one team's defense gives up a lot."""
    rng = random.Random(seed)
    teams = ["Alpha", "Bravo", "Charlie", "Delta"]
    roster = {t: [f"{t} Player {i}" for i in range(5)] for t in teams}
    rate = {p: rng.uniform(0.3, 0.9) for t in teams for p in roster[t]}
    leaky = {"Delta": 1.25}
    games = []
    for i in range(n_games):
        h, a = rng.sample(teams, 2)
        players = []
        for team, opp in ((h, a), (a, h)):
            for p in roster[team]:
                mins = max(5.0, rng.gauss(30, 4))
                pts = max(0, round(rng.gauss(mins * rate[p] * leaky.get(opp, 1.0), 4)))
                players.append({"name": p, "team": team, "min": mins, "pts": pts, "reb": rng.randint(0, 10),
                                "ast": rng.randint(0, 8), "3pm": rng.randint(0, 4)})
        games.append({"date": f"2026-{1 + i // 250:02d}-{1 + (i % 250) // 9:02d}T{i % 9:02d}:00Z",
                      "season": 2026 if i < n_games // 2 else 2027, "home": h, "away": a, "players": players})
    return games, rate


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games, cls.rate = season()
        cls.m = P.PropModel(KEYS, half_life=10, opp_weight=1.0)
        for g in cls.games:
            cls.m.play(g)

    def test_better_scorers_are_expected_to_score_more(self):
        players = [p for p in self.m.players if p.startswith("Alpha")]
        best = max(players, key=lambda p: self.rate[p])
        worst = min(players, key=lambda p: self.rate[p])
        self.assertGreater(self.m.expect(best, "Bravo", "pts"), self.m.expect(worst, "Bravo", "pts"))

    def test_leaky_defense_raises_the_expectation(self):
        p = "Alpha Player 0"
        self.assertGreater(self.m.expect(p, "Delta", "pts"), self.m.expect(p, "Bravo", "pts"))

    def test_needs_enough_games(self):
        fresh = P.PropModel(KEYS)
        fresh.play(self.games[0])
        self.assertIsNone(fresh.expect(self.games[0]["players"][0]["name"], "Bravo", "pts"))

    def test_chance_of_going_over(self):
        zs = sorted(P.z_of(mu, a) for _, st, mu, a, _, _ in self.m.log if st == "pts")
        self.assertGreater(P.prob_over(20, 15.5, zs), P.prob_over(20, 25.5, zs))
        self.assertAlmostEqual(P.prob_over(20, 19.99, zs) + (1 - P.prob_over(20, 19.99, zs)), 1)
        self.assertIsNone(P.prob_over(20, 15.5, []))


class Gate(unittest.TestCase):
    def test_tuned_on_last_season_and_tested_on_this_one(self):
        games, _ = season(seed=9, n_games=700)
        state = P.tune_and_test(games, KEYS)
        self.assertEqual(state["season"], 2027)
        pts = state["report"]["pts"]
        self.assertTrue(pts["usable"])                        # real signal in made-up points: should pass
        self.assertGreater(pts["vs_hit_rate"], 0)
        reb = state["report"]["reb"]
        self.assertLess(reb.get("vs_coin", 0), 0.02)          # rebounds are pure noise here: no real gain

    def test_names_match_across_feeds(self):
        self.assertEqual(P.name_key("Jaren Jackson Jr."), P.name_key("Jaren Jackson"))
        self.assertEqual(P.name_key("Nikola Jokić"), P.name_key("Nikola Jokic"))
        self.assertEqual(P.name_key("Gary Payton II"), "gary payton")


class ReadingBoxScores(unittest.TestCase):
    def test_nhl_points_are_goals_plus_assists_and_goalies_are_skipped(self):
        labels = ["TOI", "G", "A", "S", "SOG"]          # ESPN: S = shots on goal, SOG = shootout goals
        data = {"boxscore": {"players": [{"team": {"displayName": "Oilers"}, "statistics": [
            {"name": "forwards", "labels": labels, "athletes": [
                {"athlete": {"displayName": "Connor McDavid"}, "stats": ["21:30", "1", "2", "5", "1"]},
                {"athlete": {"displayName": "Scratched Guy"}, "stats": ["0:00", "0", "0", "0", "0"]}]},
            {"name": "goalies", "labels": ["GA", "SA", "TOI"], "athletes": [
                {"athlete": {"displayName": "A Goalie"}, "stats": ["2", "30", "60:00"]}]}]}]}}
        players = P.parse_nhl(data)
        self.assertEqual(len(players), 1)
        p = players[0]
        self.assertEqual((p["name"], p["pts"], p["sog"]), ("Connor McDavid", 3.0, 5.0))
        self.assertAlmostEqual(p["min"], 21.5)

    def test_nfl_categories_are_combined_per_player(self):
        data = {"boxscore": {"players": [{"team": {"displayName": "Bengals"}, "statistics": [
            {"name": "passing", "labels": ["C/ATT", "YDS"], "athletes": [{"athlete": {"displayName": "Joe Burrow"}, "stats": ["28/40", "300"]}]},
            {"name": "rushing", "labels": ["CAR", "YDS"], "athletes": [
                {"athlete": {"displayName": "Joe Burrow"}, "stats": ["3", "12"]},
                {"athlete": {"displayName": "Chase Brown"}, "stats": ["15", "70"]}]},
            {"name": "receiving", "labels": ["REC", "YDS"], "athletes": [{"athlete": {"displayName": "Chase Brown"}, "stats": ["4", "31"]}]}]}]}}
        by = {p["name"]: p for p in P.parse_nfl(data)}
        self.assertEqual((by["Joe Burrow"]["passyds"], by["Joe Burrow"]["rushyds"], by["Joe Burrow"]["rec"]), (300.0, 12.0, 0.0))
        self.assertEqual((by["Chase Brown"]["rushyds"], by["Chase Brown"]["rec"], by["Chase Brown"]["recyds"]), (70.0, 4.0, 31.0))

    def test_which_props_each_sport_covers(self):
        self.assertTrue(P.handles({"sport_key": "icehockey_nhl", "market": "player_shots_on_goal"}))
        self.assertTrue(P.handles({"sport_key": "americanfootball_nfl", "market": "player_receptions"}))
        self.assertFalse(P.handles({"sport_key": "baseball_mlb", "market": "pitcher_strikeouts"}))


if __name__ == "__main__":
    unittest.main()
