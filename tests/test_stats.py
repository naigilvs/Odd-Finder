"""Tests for the stats model in stats.py, using made-up games (no internet needed)."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stats  # noqa: E402


def season_games(seed=1, n=600):
    """A made-up season: team strength decides who usually wins, home teams get a small edge."""
    rng = random.Random(seed)
    strength = {"Strong Sharks": 8, "Good Geese": 3, "Okay Otters": 0, "Weak Wolves": -6}
    teams = list(strength)
    games = []
    for i in range(n):
        h, a = rng.sample(teams, 2)
        margin = strength[h] - strength[a] + 3 + rng.gauss(0, 10)
        base = 110 + rng.gauss(0, 8)
        hs, as_ = round(base + margin / 2), round(base - margin / 2)
        if hs == as_:
            hs += 1
        games.append({"id": str(i), "date": f"2026-01-{1 + i % 28:02d}T{i % 24:02d}:00Z",
                      "season": 2026 if i < n // 2 else 2027, "home": h, "away": a,
                      "hs": hs, "as": as_, "neutral": False})
    return games


def built_league():
    """A LeagueStats with a model trained on the made-up season, without downloading anything."""
    lg = stats.LeagueStats("basketball_nba")
    games = season_games()
    lg.store.games = lambda: games
    lg.store.update = lambda: games
    lg._build()
    assert lg.status == "ready", lg.error
    return lg


class Helpers(unittest.TestCase):
    def test_prob_over_half_line(self):
        self.assertAlmostEqual(stats.prob_over(100, 10, 100.0001), 0.5, places=3)
        self.assertGreater(stats.prob_over(110, 10, 100.5), 0.8)

    def test_prob_over_whole_line_leaves_out_pushes(self):
        self.assertAlmostEqual(stats.prob_over(10, 3, 10), 0.5)

    def test_names_match_across_feeds(self):
        self.assertEqual(stats.normalize("Montréal Canadiens"), "montreal canadiens")
        self.assertEqual(stats.normalize("LA Clippers"), "los angeles clippers")
        self.assertEqual(stats.normalize("St. Louis Blues"), stats.normalize("St Louis Blues"))


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lg = built_league()

    def test_stronger_team_is_favored(self):
        m = self.lg.model
        self.assertGreater(m.win_prob(m.elo_diff("Strong Sharks", "Weak Wolves")), 0.75)
        self.assertLess(m.win_prob(m.elo_diff("Weak Wolves", "Strong Sharks")), 0.4)

    def test_home_advantage(self):
        m = self.lg.model
        self.assertGreater(m.elo_diff("Okay Otters", "Good Geese"), m.elo_diff("Okay Otters", "Good Geese", neutral=True))

    def test_accuracy_report(self):
        r = self.lg.report
        self.assertGreater(r["games"], 100)
        self.assertGreater(r["skill"], 0)          # better than guessing the home team

    def predict(self, **kw):
        item = dict(sport_key="basketball_nba", home="Strong Sharks", away="Weak Wolves", **kw)
        return self.lg.predict(item)

    def test_moneyline_sides_add_up(self):
        h = self.predict(market="h2h", outcome="Strong Sharks")["prob"]
        a = self.predict(market="h2h", outcome="Weak Wolves")["prob"]
        self.assertAlmostEqual(h + a, 1, places=3)

    def test_bigger_spread_is_harder_to_cover(self):
        main = self.predict(market="spreads", outcome="Strong Sharks", point=-3.5)["prob"]
        alt = self.predict(market="alternate_spreads", outcome="Strong Sharks", point=-15.5)["prob"]
        self.assertGreater(main, alt)

    def test_over_and_under_add_up(self):
        o = self.predict(market="totals", outcome="Over", point=220.5)["prob"]
        u = self.predict(market="totals", outcome="Under", point=220.5)["prob"]
        self.assertAlmostEqual(o + u, 1, places=3)

    def test_team_totals(self):
        low = self.predict(market="team_totals", outcome="Over", point=100.5, team="Strong Sharks")
        high = self.predict(market="team_totals", outcome="Over", point=125.5, team="Strong Sharks")
        self.assertGreater(low["prob"], high["prob"])
        self.assertIn("expected_team", low)

    def test_uncovered_bets_say_why(self):
        self.assertIn("quarter", self.predict(market="spreads_h1", outcome="Strong Sharks", point=-1.5)["reason"])
        self.assertIn("reason", self.predict(market="player_points", outcome="Over", point=20.5))
        self.assertIn("reason", self.lg.predict(dict(market="h2h", outcome="X", home="Unknown FC", away="Nobody")))


class SeasonCarryOver(unittest.TestCase):
    def test_first_game_of_a_new_season_uses_the_carried_over_rating(self):
        m = stats.Model(stats.LEAGUES["basketball_nba"])
        m.elo.update({"Good": 1700.0, "Bad": 1300.0})
        m.season_of.update({"Good": 2026, "Bad": 2026})
        m.latest_season = 2026
        full = m.elo_diff("Good", "Bad", neutral=True)
        m.latest_season = 2027                                   # new season started elsewhere
        self.assertAlmostEqual(m.elo_diff("Good", "Bad", neutral=True), full * m.carry)


class Soccer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(3)
        strength = {"Strong FC": 2.2, "Mid Town": 1.3, "Weak Rovers": 0.7, "Average United": 1.3}
        cls.m = stats.SoccerModel(half_life=20, draw=0.1)
        teams = list(strength)
        for i in range(400):
            h, a = rng.sample(teams, 2)
            hs = sum(1 for _ in range(10) if rng.random() < strength[h] * 1.1 / 10)
            as_ = sum(1 for _ in range(10) if rng.random() < strength[a] * 0.9 / 10)
            cls.m.play({"home": h, "away": a, "season": 2026, "hs": hs, "as": as_})

    def test_chances_add_up_and_favor_the_stronger_team(self):
        home, draw, away = self.m.outcome_probs(self.m.scores(*self.m.expected("Strong FC", "Weak Rovers")))
        self.assertAlmostEqual(home + draw + away, 1)
        self.assertGreater(home, 0.6)
        self.assertGreater(home, away)

    def test_half_goal_spread_equals_winning(self):
        grid = self.m.scores(*self.m.expected("Mid Town", "Weak Rovers"))
        self.assertAlmostEqual(stats.cover_prob(grid, -0.5, home_side=True), self.m.outcome_probs(grid)[0])

    def test_totals(self):
        grid = self.m.scores(1.5, 1.2)
        self.assertGreater(stats.over_prob(grid, 1.5), stats.over_prob(grid, 3.5))
        self.assertAlmostEqual(stats.over_prob(grid, 2.5) + (1 - stats.over_prob(grid, 2.5)), 1)

    def test_draw_boost_raises_draws(self):
        plain = stats.SoccerModel(draw=0.0).scores(1.4, 1.1)
        boosted = stats.SoccerModel(draw=0.3).scores(1.4, 1.1)
        self.assertGreater(stats.SoccerModel.outcome_probs(boosted)[1], stats.SoccerModel.outcome_probs(plain)[1])

    def test_quarter_goal_lines_are_recognized(self):
        self.assertTrue(stats._quarter_line(-0.25))
        self.assertTrue(stats._quarter_line(0.75))
        self.assertFalse(stats._quarter_line(-1.5))
        self.assertFalse(stats._quarter_line(2.0))


class ResultMatching(unittest.TestCase):
    """Nickname-only matches must never mix up e.g. Manchester United and Newcastle United."""

    def setUp(self):
        self.lg = stats._leagues["soccer_epl"]
        self.saved = self.lg.store.games
        self.lg.store.games = lambda: [
            {"id": "1", "date": "2026-11-22T15:00Z", "season": 2026, "home": "Manchester United", "away": "Arsenal FC", "hs": 2, "as": 0, "neutral": False},
            {"id": "2", "date": "2026-11-22T15:00Z", "season": 2026, "home": "Newcastle United", "away": "Arsenal Reserves", "hs": 0, "as": 3, "neutral": False},
        ]

    def tearDown(self):
        self.lg.store.games = self.saved

    def test_exact_names_win(self):
        self.assertEqual(stats.find_final_score("soccer_epl", "Manchester United", "Arsenal FC", "2026-11-22T15:00:00Z"), (2, 0))

    def test_ambiguous_nicknames_give_no_answer(self):
        self.assertIsNone(stats.find_final_score("soccer_epl", "Man United", "Arsenal", "2026-11-22T15:00:00Z"))


if __name__ == "__main__":
    unittest.main()
