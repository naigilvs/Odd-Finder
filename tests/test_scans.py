"""Tests for saved scans (scans.py)."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scans  # noqa: E402

SOON = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
GONE = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def row(sport="basketball_nba", event="g1", start=SOON, sel="A"):
    return {"sport_key": sport, "event_id": event, "start": start, "selection": sel}


class ScanStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "scans.json"
        self.store = scans.ScanStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kept_after_a_restart_with_no_size_limit(self):
        rows = [row(sel=f"bet {i}") for i in range(20000)]          # far more than a browser could keep
        self.store.save_main("fanduel", ["basketball_nba"], rows)
        again = scans.ScanStore(self.path).load("fanduel")
        self.assertEqual(len(again["results"]["basketball_nba"]["rows"]), 20000)

    def test_rescanning_a_league_replaces_only_that_league(self):
        self.store.save_main("fanduel", ["basketball_nba", "icehockey_nhl"],
                             [row(sel="old nba"), row("icehockey_nhl", sel="nhl")])
        self.store.save_main("fanduel", ["basketball_nba"], [row(sel="new nba")])
        r = self.store.load("fanduel")["results"]
        self.assertEqual([x["selection"] for x in r["basketball_nba"]["rows"]], ["new nba"])
        self.assertEqual([x["selection"] for x in r["icehockey_nhl"]["rows"]], ["nhl"])

    def test_league_with_no_games_is_remembered_as_scanned(self):
        self.store.save_main("fanduel", ["baseball_mlb"], [])
        self.assertEqual(self.store.load("fanduel")["results"]["baseball_mlb"]["rows"], [])

    def test_started_games_are_dropped(self):
        self.store.save_main("fanduel", ["basketball_nba"], [row(sel="later"), row(start=GONE, sel="started")])
        rows = self.store.load("fanduel")["results"]["basketball_nba"]["rows"]
        self.assertEqual([x["selection"] for x in rows], ["later"])

    def test_more_bets_replace_by_game_and_can_be_cleared(self):
        self.store.save_more("fanduel", ["g1", "g2"], [row(event="g1", sel="a"), row(event="g2", sel="b")])
        self.store.save_more("fanduel", ["g1"], [row(event="g1", sel="a2")])
        more = self.store.load("fanduel")["more"]
        self.assertEqual(sorted(x["selection"] for x in more), ["a2", "b"])
        self.assertTrue(all("scanned_at" in x for x in more))
        self.store.clear_more("fanduel")
        self.assertEqual(self.store.load("fanduel")["more"], [])

    def test_each_sportsbook_is_kept_apart(self):
        self.store.save_main("fanduel", ["basketball_nba"], [row()])
        self.assertEqual(self.store.load("draftkings"), {"results": {}, "more": []})

    def test_broken_file_starts_fresh(self):
        self.path.write_text("not json")
        self.assertEqual(scans.ScanStore(self.path).load("fanduel"), {"results": {}, "more": []})



class BrowserImport(unittest.TestCase):
    """Scans an older version saved in the browser are merged into the bot's copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = scans.ScanStore(Path(self.tmp.name) / "scans.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_copy_of_each_league_wins(self):
        self.store.save_main("fanduel", ["basketball_nba"], [row(sel="bot nba")])
        bot_at = self.store.load("fanduel")["results"]["basketball_nba"]["at"]
        merged = self.store.import_browser("fanduel", {
            "basketball_nba": {"at": bot_at - 1000, "rows": [row(sel="older browser nba")]},    # older: ignored
            "soccer_epl": {"at": 5, "rows": [row("soccer_epl", sel="browser epl")]},             # bot has none: added
            "icehockey_nhl": {"at": bot_at + 1000, "rows": [row("icehockey_nhl", sel="newer")]},
        }, [row(event="g9", sel="browser more")])
        self.assertEqual([x["selection"] for x in merged["results"]["basketball_nba"]["rows"]], ["bot nba"])
        self.assertEqual([x["selection"] for x in merged["results"]["soccer_epl"]["rows"]], ["browser epl"])
        self.assertEqual([x["selection"] for x in merged["results"]["icehockey_nhl"]["rows"]], ["newer"])
        self.assertEqual([x["selection"] for x in merged["more"]], ["browser more"])
        # and it's saved, so it survives a restart
        again = scans.ScanStore(self.store.path).load("fanduel")
        self.assertIn("soccer_epl", again["results"])

    def test_started_games_and_junk_are_dropped(self):
        merged = self.store.import_browser("fanduel", {
            "basketball_nba": {"at": 1, "rows": [row(start=GONE), "junk", row(sel="ok")]},
            "bad": "not a league",
        }, ["junk", row(event="g2", start=GONE)])
        self.assertEqual([x["selection"] for x in merged["results"]["basketball_nba"]["rows"]], ["ok"])
        self.assertNotIn("bad", merged["results"])
        self.assertEqual(merged["more"], [])


if __name__ == "__main__":
    unittest.main()
