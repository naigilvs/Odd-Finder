"""Tests for backups (backup.py), using temporary folders instead of iCloud Drive."""
import json
import os
import sys
import tempfile
import unittest
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

    def test_backs_up_only_the_latest_copy(self):
        self.write("picks.json", [{"id": 1}])
        self.write("config.json", [{"key": "secret"}])                 # never backed up
        self.assertEqual(self.b.run(), ["picks.json"])
        self.assertEqual(json.loads((self.cloud / "latest" / "picks.json").read_text()), [{"id": 1}])
        self.assertEqual([p.name for p in self.cloud.iterdir()], ["latest"])   # no daily copies
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

    def test_old_daily_copies_are_tidied_away(self):
        self.write("picks.json", [{"id": 1}])
        old = self.cloud / "daily" / "2026-11-20"
        other = self.cloud / "daily" / "my notes"
        for d in (old, other):
            d.mkdir(parents=True)
        self.b.run()
        self.assertFalse(old.exists())
        self.assertTrue(other.exists())                                  # not ours: left alone
        other.rmdir()
        self.b._seen.clear()
        self.b.run()
        self.assertFalse((self.cloud / "daily").exists())                # empty folder removed too

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



class GitHubBackups(unittest.TestCase):
    """Uses a local bare repo in place of GitHub (no internet needed)."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(self.remote)], check=True)
        self.data = root / "data"
        self.data.mkdir()
        self.g = backup.GitBackup(self.data, str(self.remote))
        os.environ.setdefault("GIT_AUTHOR_NAME", "Test")
        os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
        os.environ.setdefault("GIT_COMMITTER_NAME", "Test")
        os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")

    def tearDown(self):
        self.tmp.cleanup()

    def commits(self):
        import subprocess
        r = subprocess.run(["git", "--git-dir", str(self.remote), "log", "--oneline", "main"], capture_output=True, text=True)
        return [l for l in r.stdout.splitlines() if l]

    def files_in_remote(self):
        import subprocess
        r = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "--name-only", "main"], capture_output=True, text=True)
        return sorted(r.stdout.split())

    def test_pushes_only_when_something_changed(self):
        (self.data / "picks.json").write_text('[{"id": 1}]')
        (self.data / "config.json").write_text('{"odds_api_key": "secret"}')
        self.assertTrue(self.g.run())
        self.assertFalse(self.g.run())                       # nothing new
        (self.data / "picks.json").write_text('[{"id": 1}, {"id": 2}]')
        self.assertTrue(self.g.run())
        self.assertEqual(len(self.commits()), 2)             # every version is kept
        self.assertEqual(self.files_in_remote(), ["picks.json"])
        self.assertIsNone(self.g.error)

    def test_restores_on_a_fresh_computer(self):
        (self.data / "picks.json").write_text('[{"id": 7}]')
        (self.data / "track.json").write_text('[]')
        self.g.run()
        fresh = Path(self.tmp.name) / "fresh-data"
        fresh.mkdir()
        restored = backup.GitBackup(fresh, str(self.remote)).restore_missing()
        self.assertEqual(sorted(restored), ["picks.json", "track.json"])
        self.assertEqual(json.loads((fresh / "picks.json").read_text()), [{"id": 7}])

    def test_bad_link_reports_an_error_without_crashing(self):
        bad = backup.GitBackup(self.data, str(Path(self.tmp.name) / "no-such-repo.git"))
        (self.data / "picks.json").write_text('[]')
        self.assertFalse(bad.run())
        self.assertIn("GitHub backup failed", bad.error)
        self.assertEqual(bad.restore_missing(), [])

    def test_off_without_a_link(self):
        off = backup.GitBackup(self.data, None)
        self.assertFalse(off.enabled)
        self.assertFalse(off.run())


if __name__ == "__main__":
    unittest.main()
