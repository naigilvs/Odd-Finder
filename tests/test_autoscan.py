"""Tests for automatic scans (app.py) and the page's numbers in Python (picks.page_numbers)."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
import picks  # noqa: E402


def row(**over):
    r = {"event_id": "g1", "sport_key": "basketball_nba", "sport": "NBA", "event": "A @ B", "home_team": "B", "away_team": "A",
         "start": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(), "market": "h2h", "market_label": "Moneyline",
         "selection": "B", "outcome": "B", "decimal": 2.1, "american": "+110", "fair_prob": 0.55, "book_prob": 0.5,
         "ev": 0.05, "books_used": ["A", "B", "C", "D", "E", "F", "G"], "sharp": True, "player": None}
    r.update(over)
    return r


CASES = [  # (row changes, stats, injury)
    ({}, {"prob": 0.56}, None),
    ({"books_used": ["A", "B", "C", "D"]}, None, None),
    ({"books_used": ["A"], "sharp": False}, None, None),
    ({}, {"prob": 0.3}, None),
    ({"estimated": True, "player": "P", "books_used": ["A", "B", "C", "D", "E"]}, None, None),
    ({"player": "P"}, None, {"player": {"name": "P", "level": "question", "status": "Questionable"}}),
    ({}, {"prob": 0.56}, {"player": {"name": "P", "level": "out", "status": "Out"}}),
    ({"ev": -0.02}, {"prob": 0.6}, None),
]


class PageNumbers(unittest.TestCase):
    def test_same_as_the_page(self):
        """The Python copy gives the same chance, confidence and agree as static/calc.js."""
        cases = [(row(**c), s, i) for c, s, i in CASES]
        script = ("const C = require(process.argv[1]); const cases = JSON.parse(process.argv[2]);"
                  "console.log(JSON.stringify(cases.map(([r, s, i]) => ({ chance: C.chance(r, s || undefined),"
                  " confidence: C.confidence(r, s || undefined, i || undefined).label, agree: C.agrees(r, s || undefined) }))));")
        try:
            out = subprocess.run(["node", "-e", script, str(ROOT / "static/calc.js"), json.dumps(cases)],
                                 capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            self.skipTest("Node.js isn't installed")
        page = json.loads(out.stdout)
        for (r, s, i), js in zip(cases, page):
            py = picks.page_numbers(r, s, i)
            self.assertAlmostEqual(py["chance"], js["chance"])
            self.assertEqual((py["confidence"], py["agree"]), (js["confidence"], js["agree"]), (r, s, i))


class AutoScans(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = app.AutoScanSettings(Path(self.dir.name) / "autoscan.json")
        self.track = picks.TrackStore(Path(self.dir.name) / "track.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_settings_from_the_page(self):
        self.assertTrue(self.settings.get()["enabled"])                                   # on by default
        out = self.settings.set_from_page({"enabled": False, "every_hours": 6, "sports": ["icehockey_nhl", "tennis_x", 5]})
        self.assertEqual((out["enabled"], out["every_hours"], out["sports"]), (False, 6, ["icehockey_nhl"]))
        self.assertEqual(self.settings.set_from_page({"every_hours": 7})["every_hours"], 6)  # not an option: unchanged
        with self.assertRaises(ValueError):
            self.settings.set_from_page({"sports": ["tennis_x"]})
        self.assertEqual(app.AutoScanSettings(self.settings.path).get()["every_hours"], 6)  # saved

    def test_scans_when_due_and_logs_bets(self):
        noon = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)                         # midday in the US
        scanned = {"rows": [row()], "errors": []}
        saved = mock.MagicMock()
        with mock.patch.object(app, "AUTOSCAN", self.settings), mock.patch.object(app, "RUNDOWN_KEY", "k"), \
                mock.patch.object(app, "DEMO", False), mock.patch.object(app, "TRACK", self.track), \
                mock.patch.object(app, "SCANS", saved), mock.patch.object(app.rundown, "usage", {"remaining": 15000}), \
                mock.patch.object(app, "run_scan", return_value=scanned) as rs, \
                mock.patch.object(app.stats, "predict_many", return_value=({"g1|h2h|B": {"prob": 0.56}}, 0)), \
                mock.patch.object(app.injuries, "lookup", return_value={"injuries": {}, "teams": {}, "updated": {}}), \
                mock.patch.object(app, "datetime", wraps=datetime) as dt:
            dt.now.return_value = noon
            local_noon = noon.astimezone().hour not in app.QUIET_HOURS
            first = app.autoscan_once(noon)
            again = app.autoscan_once(noon + timedelta(hours=1))                          # not due yet
            later = app.autoscan_once(noon + timedelta(hours=3, minutes=5))
        if not local_noon:
            self.skipTest("this computer's time zone puts the test hour in quiet hours")
        self.assertEqual((first, again, later), ("scanned", "not due", "scanned"))
        self.assertEqual(rs.call_args[0][4], "therundown")                                 # free odds only
        self.assertFalse(rs.call_args[0][2])                                               # no props (credits)
        saved.save_main.assert_called()
        bet = self.track.all()[0]
        self.assertAlmostEqual(bet["chance"], 0.75 * 0.55 + 0.25 * 0.56)
        self.assertEqual((bet["confidence"], bet["agree"]), ("High", True))
        self.assertEqual(self.settings.get()["last_count"], 1)

    def test_skips_when_off_quiet_or_low_on_points(self):
        with mock.patch.object(app, "AUTOSCAN", self.settings), mock.patch.object(app, "RUNDOWN_KEY", "k"), \
                mock.patch.object(app, "DEMO", False), mock.patch.object(app, "run_scan") as rs:
            self.settings.update(enabled=False)
            self.assertEqual(app.autoscan_once(), "off")
            self.settings.update(enabled=True)
            with mock.patch.object(app.rundown, "usage", {"remaining": 100}):
                quiet = datetime.now().astimezone().hour in app.QUIET_HOURS
                self.assertEqual(app.autoscan_once(), "quiet hours" if quiet else "low points")
            rs.assert_not_called()
        with mock.patch.object(app, "AUTOSCAN", self.settings), mock.patch.object(app, "RUNDOWN_KEY", None):
            self.assertEqual(app.autoscan_once(), "off")                                    # no free key


if __name__ == "__main__":
    unittest.main()
