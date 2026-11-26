"""Tests for TheRundown's free odds (rundown.py) and the free scan path in app.py, with made-up responses."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import rundown  # noqa: E402


def price(american, main=True):
    return {"price": american, "is_main_line": main, "updated_at": "2026-11-26T15:00:00Z"}


def event(eid="abc", start="2026-11-26T21:30:00Z", home=("Dallas", "Cowboys", 77), away=("Philadelphia", "Eagles", 79)):
    h, a = f"{home[0]} {home[1]}".strip(), f"{away[0]} {away[1]}".strip()
    return {"event_id": eid, "event_date": start, "teams": [
        {"team_id": away[2], "name": away[0], "mascot": away[1], "is_away": True, "is_home": False},
        {"team_id": home[2], "name": home[0], "mascot": home[1], "is_away": False, "is_home": True}],
        "markets": [
            {"market_id": 1, "period_id": 0, "participants": [
                {"id": home[2], "name": h, "lines": [{"prices": {"23": price(-480), "19": price(-479), "22": price(-475)}}]},
                {"id": away[2], "name": a, "lines": [{"prices": {"23": price(370), "19": price(369), "22": price(360)}}]}]},
            {"market_id": 2, "period_id": 0, "participants": [
                {"id": home[2], "name": h, "lines": [{"value": "-8.5", "prices": {"23": price(-112), "19": price(-111)}}]},
                {"id": away[2], "name": a, "lines": [{"value": "+8.5", "prices": {"23": price(-108), "19": price(-108)}}]}]},
            {"market_id": 3, "period_id": 0, "participants": [
                {"id": 9, "name": "Over", "lines": [{"value": "47.5", "prices": {"23": price(-110), "22": price(-105)}},
                                                    {"value": "48.5", "prices": {"19": price(-110)}}]},
                {"id": 10, "name": "Under", "lines": [{"value": "47.5", "prices": {"23": price(-110), "22": price(-115)}},
                                                     {"value": "48.5", "prices": {"19": price(-110)}}]}]},
            {"market_id": 1, "period_id": 1, "participants": []},                  # 1st half: ignored
            {"market_id": 29, "period_id": 0, "participants": []},                 # a prop: not on Free
        ]}


class Convert(unittest.TestCase):
    def test_american_to_decimal(self):
        self.assertEqual(rundown.decimal_odds(-110), 1.9091)
        self.assertEqual(rundown.decimal_odds(150), 2.5)
        self.assertIsNone(rundown.decimal_odds(0))           # placeholder
        self.assertIsNone(rundown.decimal_odds("x"))

    def test_event_becomes_odds_api_shape(self):
        ev = rundown.convert(event(), "americanfootball_nfl", "NFL")
        self.assertEqual((ev["id"], ev["home_team"], ev["away_team"]), ("rd_abc", "Dallas Cowboys", "Philadelphia Eagles"))
        fd = next(b for b in ev["bookmakers"] if b["key"] == "fanduel")
        markets = {m["key"]: m["outcomes"] for m in fd["markets"]}
        self.assertEqual(set(markets), {"h2h", "spreads", "totals"})
        self.assertEqual(sorted((o["name"], o.get("point")) for o in markets["spreads"]),
                         [("Dallas Cowboys", -8.5), ("Philadelphia Eagles", 8.5)])
        self.assertEqual({o["name"] for o in markets["totals"]}, {"Over", "Under"})
        self.assertEqual({b["title"] for b in ev["bookmakers"]}, {"FanDuel", "DraftKings", "BetMGM"})

    def test_bot_values_it_like_any_game(self):
        rows = app.analyze_event(rundown.convert(event(), "americanfootball_nfl", "NFL"), "fanduel", 1)
        by = {r["selection"]: r for r in rows}
        self.assertEqual(by["Dallas Cowboys"]["books_used"], ["DraftKings", "BetMGM"])
        self.assertEqual(by["Over 47.5"]["books_used"], ["BetMGM"])            # DraftKings is on 48.5
        self.assertNotIn("Over 48.5", by)                                      # FanDuel doesn't offer it
        self.assertIn("Dallas Cowboys -8.5", by)

    def test_name_fixes_and_soccer_names(self):
        ev = rundown.convert(event(home=("LA", "Clippers", 1), away=("Coventry City", "", 2)), "basketball_nba", "NBA")
        self.assertEqual(ev["home_team"], "Los Angeles Clippers")
        self.assertEqual(ev["away_team"], "Coventry City")


class MatchAndFetch(unittest.TestCase):
    def test_games_get_the_odds_api_id_and_names(self):
        evs = [rundown.convert(event(), "americanfootball_nfl", "NFL"),
               rundown.convert(event(eid="zzz", start="2026-11-30T01:00:00Z", home=("Nobody", "Here", 5), away=("No", "One", 6)),
                               "americanfootball_nfl", "NFL")]
        games = [{"id": "oddsapi1", "home_team": "Dallas Cowboys", "away_team": "Philadelphia Eagles",
                  "commence_time": "2026-11-26T21:25:00Z", "sport_title": "NFL"},
                 {"id": "other", "home_team": "Dallas Cowboys", "away_team": "Philadelphia Eagles",
                  "commence_time": "2026-12-20T21:25:00Z"}]                     # same teams, weeks later
        rundown.match_ids(evs, games)
        self.assertEqual((evs[0]["id"], evs[0]["commence_time"]), ("oddsapi1", "2026-11-26T21:25:00Z"))
        self.assertEqual(evs[1]["id"], "rd_zzz")                               # no match: keeps its own id

    def test_fetch_loads_each_day_and_skips_unsupported_leagues(self):
        calls = []

        def fake_get(key, path, **params):
            calls.append(path)
            return {"events": [event(eid=path[-10:])]} if path.endswith("2026-11-26") else {"events": []}
        now = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
        with mock.patch.object(rundown, "_get", side_effect=fake_get):
            evs = rundown.fetch_events("k", "americanfootball_nfl", "NFL", now=now)
            self.assertIsNone(rundown.fetch_events("k", "tennis_atp_paris", "ATP", now=now))
        self.assertEqual(len(calls), 7)                                        # NFL: a week ahead
        self.assertTrue(calls[0].startswith("sports/2/events/"))
        self.assertEqual(len(evs), 1)

    def test_free_scan_path(self):
        ev = rundown.convert(event(start="2099-01-01T00:00:00Z"), "americanfootball_nfl", "NFL")
        with mock.patch.object(app, "RUNDOWN_KEY", "k"), mock.patch.object(app, "DEMO", False), \
                mock.patch.object(rundown, "fetch_events", side_effect=lambda k, sp, t: [ev] if sp in rundown.SPORT_IDS else None) as fe, \
                mock.patch.object(app, "api_get", side_effect=app.ApiError("no credits")) as ag:
            res = app.run_scan("fanduel", ["americanfootball_nfl", "tennis_atp_paris"], False, 1, "therundown")
        self.assertEqual(res["source"], "therundown")
        self.assertTrue(res["rows"])
        self.assertTrue(all(r["event_id"] == "rd_abc" for r in res["rows"]))   # unmatched when the list fails
        self.assertEqual(fe.call_count, 2)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("not on TheRundown's free plan", res["errors"][0])     # tennis isn't free
        self.assertTrue(ag.called)                                             # only the free lists were tried


if __name__ == "__main__":
    unittest.main()
