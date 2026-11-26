"""Tests for closing prices and closing line value (picks.py) and the free closing scan (app.py)."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import picks  # noqa: E402
import rundown  # noqa: E402

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
START = (NOW + timedelta(hours=6)).isoformat()


def bet(**kw):
    b = {"event_id": "g1", "sport_key": "americanfootball_nfl", "sport": "NFL", "event": "Away Team @ Home Team",
         "home_team": "Home Team", "away_team": "Away Team", "start": START, "market": "h2h", "selection": "Home Team",
         "outcome": "Home Team", "decimal": 2.1, "american": "+110", "fair_prob": 0.5, "chance": 0.5, "ev": 0.05}
    b.update(kw)
    return b


class ClosingPrices(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.track = picks.TrackStore(Path(self.dir.name) / "track.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_scan_in_the_last_hour_is_the_close(self):
        self.track.log([bet()], now=NOW)                                     # 6 hours before: not the close
        self.assertNotIn("close_fair", self.track.all()[0])
        self.track.log([bet(decimal=2.0, fair_prob=0.53)], now=NOW + timedelta(hours=5, minutes=20))
        b = self.track.all()[0]
        self.assertEqual((b["close_fair"], b["close_source"], b["open_decimal"]), (0.53, "scan", 2.1))
        self.assertAlmostEqual(picks.clv(b), 2.1 * 0.53 - 1)                 # first price vs the closing fair chance

    def test_no_clv_when_first_seen_right_before_the_close(self):
        self.track.log([bet()], now=NOW + timedelta(hours=5, minutes=30))
        b = self.track.all()[0]
        self.assertIn("close_fair", b)
        self.assertIsNone(picks.clv(b))
        self.assertIsNone(picks.clv(bet()))                                   # no prices at all

    def test_closing_scan_rows_and_what_is_due(self):
        self.track.log([bet(), bet(market="totals", selection="Over 44.5", outcome="Over", point=44.5),
                        bet(market="player_points", selection="P Over 20.5", player="P")], now=NOW)
        soon = NOW + timedelta(hours=5, minutes=50)
        due = self.track.due_for_close(soon)
        self.assertEqual({b["market"] for b in due}, {"h2h", "totals"})       # props aren't on the free plan
        self.assertEqual(self.track.due_for_close(NOW), [])                    # too early
        rows = [{"event_id": "g1", "market": "h2h", "selection": "Home Team", "fair_prob": 0.55, "decimal": 2.0},
                {"event_id": "g1", "market": "totals", "selection": "Over 45.5", "fair_prob": 0.5}]   # line moved
        self.assertEqual(self.track.record_closes(rows, soon), 1)
        home = next(b for b in self.track.all() if b["market"] == "h2h")
        self.assertEqual((home["close_fair"], home["close_source"]), (0.55, "closing scan"))
        self.assertEqual(self.track.record_closes(rows, NOW + timedelta(hours=7)), 0)   # game started: too late

    def test_summary_shows_clv_and_likely_ranges(self):
        bets = []
        for i in range(30):
            b = bet(event_id=f"g{i}", status="won" if i % 2 else "lost", open_decimal=2.1, open_fair_prob=0.5,
                    first_seen=NOW.isoformat(), close_at=(NOW + timedelta(hours=5)).isoformat(),
                    close_fair=0.5 + (0.03 if i % 3 else -0.01), start=START)
            bets.append(b)
        s = picks.track_summary(bets, view="all")
        first = s["clv"][0]
        self.assertEqual((first["name"], first["bets"], first["beat"]), ("Good value when first seen", 30, 30))
        self.assertGreater(first["lo"], 0)                                     # clearly beating the close
        a = s["all"]
        self.assertLess(a["profit_lo"], a["profit"])
        self.assertGreater(a["profit_hi"], a["profit"])
        self.assertLess(a["profit_lo"], 0)                                     # 15-15 at +110: could be luck
        one = picks.track_summary([bets[0]], view="all")
        self.assertIsNone(one["all"]["profit_lo"])                             # one bet: no range


class ClosingScan(unittest.TestCase):
    def test_free_closing_scan_records_matched_games(self):
        tmp = tempfile.TemporaryDirectory()
        track = picks.TrackStore(Path(tmp.name) / "track.json")
        track.log([bet(event_id="oddsapi1")], now=NOW)
        ev = {"id": "rd_x", "sport_key": "americanfootball_nfl", "sport_title": "NFL", "commence_time": START,
              "home_team": "Home Team", "away_team": "Away Team", "bookmakers": [
                  {"key": "fanduel", "title": "FanDuel", "markets": [{"key": "h2h", "outcomes": [
                      {"name": "Home Team", "price": 2.0}, {"name": "Away Team", "price": 1.8}]}]},
                  {"key": "draftkings", "title": "DraftKings", "markets": [{"key": "h2h", "outcomes": [
                      {"name": "Home Team", "price": 1.9}, {"name": "Away Team", "price": 1.9}]}]}]}
        with mock.patch.object(app, "TRACK", track), mock.patch.object(app, "RUNDOWN_KEY", "k"), \
                mock.patch.object(app, "DEMO", False), \
                mock.patch.object(rundown, "fetch_day", return_value=[ev]) as fd:
            self.assertEqual(app.closing_scan_once(NOW + timedelta(hours=5, minutes=50)), 1)
            self.assertEqual(app.closing_scan_once(NOW), 0)                    # nothing due: no request
        self.assertEqual(fd.call_count, 1)
        b = track.all()[0]
        self.assertEqual((b["close_fair"], b["close_source"]), (0.5, "closing scan"))   # DraftKings 50/50, no margin
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
