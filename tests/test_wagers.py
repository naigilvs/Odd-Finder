"""Tests for the My wagers log (wagers.py)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wagers  # noqa: E402


class WagerLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = wagers.WagerStore(Path(self.tmp.name) / "wagers.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_survive_a_restart(self):
        self.store.add({"date": "2026-11-20", "note": "Lakers -3.5", "wagered": 50, "result": "won", "won": 45.45})
        self.store.add({"date": "2026-11-21", "wagered": 20, "result": "lost"})
        again = wagers.WagerStore(self.store.path).all()
        self.assertEqual([w["date"] for w in again], ["2026-11-21", "2026-11-20"])        # newest first
        self.assertEqual([wagers.net(w) for w in again], [-20, 45.45])

    def test_push_and_lost_ignore_any_amount_won(self):
        w = self.store.add({"date": "2026-11-20", "wagered": 30, "result": "push", "won": 99})
        self.assertEqual((w["won"], wagers.net(w)), (0.0, 0.0))

    def test_bad_input_is_refused_with_a_clear_message(self):
        bad = [({"wagered": 0, "result": "lost"}, "wagered"),
               ({"wagered": -5, "result": "lost"}, "between"),
               ({"wagered": "abc", "result": "lost"}, "number"),
               ({"wagered": 10, "result": "maybe"}, "still pending"),
               ({"wagered": 10, "result": "won"}, "how much you won"),
               ({"wagered": 10, "result": "lost", "date": "2026-13-45"}, "date"),
               ({"wagered": float("nan"), "result": "lost"}, "between"),
               ("not a dict", "missing")]
        for data, words in bad:
            with self.assertRaises(ValueError) as e:
                self.store.add(data)
            self.assertIn(words, str(e.exception))
        self.assertEqual(self.store.all(), [])

    def test_note_is_trimmed_and_date_defaults_to_today(self):
        w = self.store.add({"note": "  " + "x" * 300, "wagered": 5, "result": "lost"})
        self.assertEqual(len(w["note"]), 120)
        self.assertEqual(len(w["date"]), 10)

    def test_update_and_delete(self):
        w = self.store.add({"date": "2026-11-20", "wagered": 10, "result": "lost"})
        self.store.update(w["id"], {"date": "2026-11-20", "wagered": 10, "result": "won", "won": 9.09})
        self.assertEqual(self.store.all()[0]["result"], "won")
        with self.assertRaises(KeyError):
            self.store.update("nope", {"wagered": 1, "result": "lost"})
        self.store.delete(w["id"])
        self.assertEqual(self.store.all(), [])

    def test_pending_now_result_later(self):
        w = self.store.add({"date": "2026-11-25", "note": "Chiefs ML", "wagered": 40, "result": "pending", "won": 12})
        self.assertEqual((w["result"], w["won"], wagers.net(w)), ("pending", 0.0, 0.0))      # not counted yet
        self.store.update(w["id"], {"date": "2026-11-25", "note": "Chiefs ML", "wagered": 40, "result": "won", "won": 36.36})
        got = wagers.WagerStore(self.store.path).all()[0]                                    # saved
        self.assertEqual((got["result"], got["won"], wagers.net(got)), ("won", 36.36, 36.36))

    def test_any_wager_can_be_edited_later(self):
        w = self.store.add({"date": "2026-11-20", "note": "typo", "wagered": 100, "result": "won", "won": 90})
        self.store.update(w["id"], {"date": "2026-11-19", "note": "Lakers -3.5", "wagered": 10, "result": "lost"})
        got = self.store.all()[0]
        self.assertEqual((got["date"], got["note"], got["wagered"], got["result"], got["won"]),
                         ("2026-11-19", "Lakers -3.5", 10, "lost", 0.0))
        self.assertEqual(got["id"], w["id"])                                                  # same entry, not a copy
        self.assertEqual(len(self.store.all()), 1)

    def test_linked_to_a_saved_pick(self):
        w = self.store.add({"date": "2026-11-25", "note": "Lakers -3.5", "wagered": 50, "result": "pending",
                            "pick_id": "abc123", "decimal": 2.5})
        self.assertEqual((w["pick_id"], w["decimal"]), ("abc123", 2.5))
        with self.assertRaises(ValueError):
            self.store.add({"wagered": 5, "result": "lost", "pick_id": {"not": "text"}})
        self.store.update(w["id"], {"date": "2026-11-25", "note": "my own note", "wagered": 50, "result": "lost"})
        got = self.store.all()[0]
        self.assertNotIn("pick_id", got)                    # edited without the link: unlinked
        self.assertNotIn("decimal", got)

    def test_several_at_once(self):
        added = self.store.add_many([
            {"date": "2026-11-25", "note": "A", "wagered": 10, "result": "pending"},
            {"date": "2026-11-25", "note": "B", "wagered": 20, "result": "won", "won": 18},
            {"date": "2026-11-25", "note": "C", "wagered": 30, "result": "lost"}])
        self.assertEqual([w["note"] for w in added], ["A", "B", "C"])
        self.assertEqual(len({w["id"] for w in added}), 3)
        newest_first = [w["note"] for w in wagers.WagerStore(self.store.path).all()]
        self.assertEqual(newest_first, ["C", "B", "A"])                        # entry order kept

    def test_several_at_once_is_all_or_none(self):
        with self.assertRaises(ValueError) as e:
            self.store.add_many([{"wagered": 10, "result": "lost"}, {"wagered": 0, "result": "lost"}])
        self.assertIn("Wager 2", str(e.exception))
        self.assertEqual(self.store.all(), [])                                 # nothing saved
        for bad in ([], "nope", [{"wagered": 1, "result": "lost"}] * 51):
            with self.assertRaises(ValueError):
                self.store.add_many(bad)

    def test_parlay_one_wager_on_several_legs(self):
        w = self.store.add({"date": "2026-11-22", "wagered": 10, "result": "pending", "legs": [
            {"note": "Lakers -3.5", "pick_id": "a1", "decimal": 1.909},
            {"note": "Over 220.5", "pick_id": "b2", "decimal": 1.909},
            {"note": "Celtics ML", "pick_id": "c3", "decimal": 1.909}]})
        self.assertEqual(w["note"], "3-leg parlay")                   # default name
        self.assertAlmostEqual(w["decimal"], 1.909 ** 3, places=3)    # combined odds, about +596
        self.assertNotIn("pick_id", w)
        self.assertEqual([l["pick_id"] for l in w["legs"]], ["a1", "b2", "c3"])
        # a typed-in leg has no odds, so there are no combined odds
        w2 = self.store.add({"wagered": 5, "result": "lost", "note": "Sunday",
                             "legs": [{"note": "Lakers -3.5", "pick_id": "a1", "decimal": 1.909}, {"note": "Typed leg"}]})
        self.assertNotIn("decimal", w2)
        self.assertEqual(w2["note"], "Sunday")
        self.assertEqual(wagers.net(w2), -5)

    def test_parlay_bad_legs_are_refused(self):
        bad = [([{"note": "Only one"}], "2 to 15"),
               ([{"note": f"L{i}"} for i in range(16)], "2 to 15"),
               ("legs", "2 to 15"),
               ([{"note": "A"}, {"note": "  "}], "leg 2"),
               ([{"note": "A"}, "B"], "leg 2"),
               ([{"note": "A"}, {"note": "B", "pick_id": 5}], "pick link")]
        for legs, words in bad:
            with self.assertRaises(ValueError) as e:
                self.store.add({"wagered": 10, "result": "lost", "legs": legs})
            self.assertIn(words, str(e.exception))

    def test_editing_a_parlay_into_a_single_bet_drops_its_legs(self):
        w = self.store.add({"wagered": 10, "result": "pending",
                            "legs": [{"note": "A", "pick_id": "a1", "decimal": 2.0}, {"note": "B", "pick_id": "b2", "decimal": 2.0}]})
        self.assertEqual(w["decimal"], 4.0)
        single = self.store.update(w["id"], {"wagered": 10, "result": "lost", "note": "Just A"})
        self.assertNotIn("legs", single)
        self.assertNotIn("decimal", single)

    def test_bankroll_settings_saved_next_to_wagers(self):
        self.assertEqual(self.store.bankroll, wagers.BANKROLL_DEFAULTS)            # not set up yet
        saved = self.store.set_bankroll({"start": "500", "unit_pct": 1, "kelly": 0.25, "cap_pct": 2, "max_stake": 25})
        self.assertEqual(saved["start"], 500.0)
        self.assertEqual(saved["daily_loss"], 0.0)                                  # missing -> default (off)
        again = wagers.WagerStore(self.store.path)
        self.assertEqual(again.bankroll["max_stake"], 25.0)
        self.assertEqual(again.bankroll_path.name, "bankroll.json")
        demo = wagers.WagerStore(Path(self.tmp.name) / "wagers_demo.json")
        self.assertEqual(demo.bankroll_path.name, "bankroll_demo.json")

    def test_bad_bankroll_settings_are_refused(self):
        for data, words in [({"start": -5}, "starting bankroll"), ({"kelly": 3}, "Kelly fraction"),
                            ({"cap_pct": "abc"}, "number"), ({"exposure_pct": 150}, "pending limit"), ("x", "missing")]:
            with self.assertRaises(ValueError) as e:
                self.store.set_bankroll(data)
            self.assertIn(words, str(e.exception))
        self.assertEqual(self.store.bankroll, wagers.BANKROLL_DEFAULTS)            # unchanged


if __name__ == "__main__":
    unittest.main()
