"""
Backups to iCloud Drive
-----------------------
Saved picks and the bot's track record are copied to an "Odd Finder backups"
folder in iCloud Drive, so they survive if the data folder is deleted:

    Odd Finder backups/latest/          newest copy of each file
    Odd Finder backups/daily/YYYY-MM-DD the copy from each day, kept for 30 days

If a file is missing when the bot starts, it's restored from the latest backup.
Older copies stay in daily/ for 30 days, e.g. to undo a Clear history by mistake.
Your API key (config.json) is never backed up. To use another folder (Dropbox,
Google Drive...), set "backup_folder" in config.json.
"""
import json
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
FILES = ["picks.json", "track.json"]
KEEP_DAYS = 30
CHECK_EVERY = 300            # seconds between checks for changes


def default_folder():
    return ICLOUD / "Odd Finder backups" if ICLOUD.is_dir() else None


def _readable(path):
    """True if the file exists and is valid saved data (an empty list counts: e.g. after Clear history)."""
    try:
        return isinstance(json.loads(path.read_text()), list)
    except (OSError, ValueError):
        return False


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)             # never leaves a half-copied file


class Backup:
    def __init__(self, data_dir, folder):
        self.data_dir = Path(data_dir)
        self.folder = Path(folder) if folder else None
        self.last = None          # time of the last successful backup
        self.error = None
        self._seen = {}           # file -> modification time already backed up
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return self.folder is not None

    def restore_missing(self):
        """Bring back any data file that's missing locally but has a backup."""
        restored = []
        if not self.enabled:
            return restored
        for name in FILES:
            local, saved = self.data_dir / name, self.folder / "latest" / name
            if not local.exists() and _readable(saved):
                _copy(saved, local)
                restored.append(name)
        return restored

    def run(self, today=None):
        """Copy changed files to the backup folder. Returns the names backed up."""
        if not self.enabled:
            return []
        today = today or date.today()
        done = []
        with self.lock:
            try:
                for name in FILES:
                    src = self.data_dir / name
                    if not src.exists():
                        continue
                    mtime = src.stat().st_mtime
                    if self._seen.get(name) == mtime:
                        continue                         # unchanged since the last backup
                    if not _readable(src):
                        continue                         # never replace a good backup with a broken file
                    _copy(src, self.folder / "latest" / name)
                    _copy(src, self.folder / "daily" / today.isoformat() / name)
                    self._seen[name] = mtime
                    done.append(name)
                self._prune(today)
                if done:
                    self.last = datetime.now(timezone.utc).isoformat()
                self.error = None
            except OSError as e:
                self.error = f"Couldn't back up: {e.strerror or e}"
        return done

    def _prune(self, today):
        daily = self.folder / "daily"
        if not daily.is_dir():
            return
        cutoff = today - timedelta(days=KEEP_DAYS)
        for d in daily.iterdir():
            try:
                if d.is_dir() and date.fromisoformat(d.name) < cutoff:
                    shutil.rmtree(d)
            except ValueError:
                continue                                 # not one of ours: leave it alone

    def start(self):
        """Back up now, then check for changes every few minutes in the background."""
        def loop():
            while True:
                self.run()
                time.sleep(CHECK_EVERY)
        if self.enabled:
            threading.Thread(target=loop, daemon=True).start()

    def status(self):
        return {"enabled": self.enabled, "folder": str(self.folder) if self.folder else None,
                "where": "iCloud Drive" if self.folder and ICLOUD in self.folder.parents else (self.folder.name if self.folder else None),
                "last": self.last, "error": self.error}
