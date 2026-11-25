"""
Backups to iCloud Drive
-----------------------
Saved picks and the bot's track record are copied to an "Odd Finder backups"
folder in iCloud Drive within minutes of changing, so they survive if the data
folder is deleted. Only the newest copy is kept (Odd Finder backups/latest/):
the GitHub backup below keeps every older version, so iCloud stays tiny.

If a file is missing when the bot starts, it's restored from the latest backup.
Your API key (config.json) is never backed up. To use another folder (Dropbox,
Google Drive...), set "backup_folder" in config.json.
"""
import json
import shutil
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
FILES = ["picks.json", "track.json", "wagers.json"]
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

    def run(self):
        """Copy changed files to the backup folder. Returns the names backed up."""
        if not self.enabled:
            return []
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
                    self._seen[name] = mtime
                    done.append(name)
                self._remove_daily()
                if done:
                    self.last = datetime.now(timezone.utc).isoformat()
                self.error = None
            except OSError as e:
                self.error = f"Couldn't back up: {e.strerror or e}"
        return done

    def _remove_daily(self):
        """Earlier versions kept a copy per day in daily/. GitHub keeps history now, so
        remove those dated copies (anything else in the folder is left alone)."""
        daily = self.folder / "daily"
        if not daily.is_dir():
            return
        for d in daily.iterdir():
            try:
                date.fromisoformat(d.name)
            except ValueError:
                continue                                 # not one of ours: leave it alone
            if d.is_dir():
                shutil.rmtree(d)
        if not any(daily.iterdir()):
            daily.rmdir()

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


# --------------------------------------------------------------------------- GitHub backup

import os
import subprocess

GIT_EVERY = 3600             # seconds between GitHub backups


class GitBackup:
    """Commits picks and the track record to a private GitHub repo and pushes, so every
    version is kept. The local copy of that repo lives in data/github-backup."""

    def __init__(self, data_dir, repo_url, workdir=None):
        self.data_dir = Path(data_dir)
        self.url = repo_url
        self.dir = Path(workdir) if workdir else self.data_dir / "github-backup"
        self.last = None
        self.error = None
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.url)

    def _git(self, *args, cwd=None, check=True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")          # never stop to ask for a password
        r = subprocess.run(["git", *args], cwd=cwd or self.dir, env=env, capture_output=True, text=True, timeout=120)
        if check and r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git failed")
        return r

    def _ready(self):
        """Make sure data/github-backup is a copy of the backup repo (download it if needed)."""
        if (self.dir / ".git").is_dir():
            return
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        r = self._git("clone", self.url, str(self.dir), cwd=self.dir.parent, check=False)
        if r.returncode != 0:
            raise RuntimeError("Couldn't reach the GitHub backup repo. Check the link and that you're signed in to GitHub.")
        if self._git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:     # empty repo
            self._git("checkout", "-b", "main", check=False)

    def restore_missing(self):
        restored = []
        if not self.enabled:
            return restored
        try:
            with self.lock:
                self._ready()
                self._git("pull", "--ff-only", "origin", "main", check=False)
                for name in FILES:
                    local, saved = self.data_dir / name, self.dir / name
                    if not local.exists() and _readable(saved):
                        _copy(saved, local)
                        restored.append(name)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            self.error = str(e)
        return restored

    def run(self):
        """Commit and push any changes. Returns True if a new backup was pushed."""
        if not self.enabled:
            return False
        with self.lock:
            try:
                self._ready()
                for name in FILES:
                    src = self.data_dir / name
                    if _readable(src):
                        _copy(src, self.dir / name)
                if not self._git("status", "--porcelain").stdout.strip():
                    self.error = None
                    return False                                   # nothing changed
                self._git("add", *[n for n in FILES if (self.dir / n).exists()])
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._git("commit", "-m", f"Backup {stamp}")
                self._git("push", "-u", "origin", "HEAD:main")
                self.last = datetime.now(timezone.utc).isoformat()
                self.error = None
                return True
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                self.error = f"GitHub backup failed: {e}"
                return False

    def start(self):
        def loop():
            while True:
                self.run()
                time.sleep(GIT_EVERY)
        if self.enabled:
            threading.Thread(target=loop, daemon=True).start()

    def status(self):
        return {"enabled": self.enabled, "where": "GitHub", "last": self.last, "error": self.error}
