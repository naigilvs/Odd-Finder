"""Tests for backups (backup.py), using temporary folders instead of iCloud Drive."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backup  # noqa: E402


class Backups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.cloud = root / "data", root / "cloud"
        self.data.mkdir()
        self.b = backup.Backup(self.data, self.cloud)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, items):
        (self.data / name).write_text(json.dumps(items))

    def test_backs_up_latest_and_a_daily_copy(self):
        self.write("picks.json", [{"id": 1}])
        self.write("config.json", [{"key": "secret"}])                 # never backed up
        day = date(2026, 11, 25)
        self.assertEqual(self.b.run(today=day), ["picks.json"])
        self.assertEqual(json.loads((self.cloud / "latest" / "picks.json").read_text()), [{"id": 1}])
        self.assertTrue((self.cloud / "daily" / "2026-11-25" / "picks.json").exists())
        self.assertFalse(list(self.cloud.rglob("config.json")))
        self.assertIsNotNone(self.b.status()["last"])

    def test_unchanged_files_are_skipped(self):
        self.write("track.json", [])
        self.assertEqual(self.b.run(), ["track.json"])
        self.assertEqual(self.b.run(), [])
        self.write("track.json", [{"id": 2}])
        st = (self.data / "track.json").stat()
        os.utime(self.data / "track.json", (st.st_atime, st.st_mtime + 5))
        self.assertEqual(self.b.run(), ["track.json"])

    def test_broken_files_never_replace_a_good_backup(self):
        self.write("picks.json", [{"id": 1}])
        self.b.run()
        (self.data / "picks.json").write_text("{not json")
        os.utime(self.data / "picks.json", None)
        self.b._seen.clear()
        self.assertEqual(self.b.run(), [])
        self.assertEqual(json.loads((self.cloud / "latest" / "picks.json").read_text()), [{"id": 1}])

    def test_daily_copies_older_than_30_days_are_removed(self):
        self.write("picks.json", [{"id": 1}])
        today = date(2026, 11, 25)
        old = self.cloud / "daily" / (today - timedelta(days=31)).isoformat()
        keep = self.cloud / "daily" / (today - timedelta(days=29)).isoformat()
        other = self.cloud / "daily" / "my notes"
        for d in (old, keep, other):
            d.mkdir(parents=True)
        self.b.run(today=today)
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())
        self.assertTrue(other.exists())                                  # not ours: left alone

    def test_restores_missing_files_only(self):
        self.write("picks.json", [{"id": 1}])
        self.write("track.json", [{"id": 9}])
        self.b.run()
        (self.data / "picks.json").unlink()                             # deleted by accident
        self.write("track.json", [])                                     # cleared on purpose
        self.assertEqual(self.b.restore_missing(), ["picks.json"])
        self.assertEqual(json.loads((self.data / "picks.json").read_text()), [{"id": 1}])
        self.assertEqual(json.loads((self.data / "track.json").read_text()), [])

    def test_off_without_a_folder(self):
        off = backup.Backup(self.data, None)
        self.write("picks.json", [{"id": 1}])
        self.assertFalse(off.enabled)
        self.assertEqual(off.run(), [])
        self.assertEqual(off.restore_missing(), [])


if __name__ == "__main__":
    unittest.main()
