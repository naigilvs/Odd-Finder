"""
Saved scans
-----------
The results of your last scan of each league (and your More bets scans) are kept
in data/scans.json, so they're still there after the bot or browser restarts,
with no size limit. Games that have already started are dropped when loading.
"""
import json
import threading
import time
from datetime import datetime, timezone


def _upcoming(rows, now):
    out = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["start"].replace("Z", "+00:00")) > now:
                out.append(r)
        except (KeyError, ValueError, AttributeError):
            continue
    return out


class ScanStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                raise ValueError
        except (OSError, ValueError):
            self.data = {}          # book -> {"leagues": {sport: {"at": ms, "rows": [...]}}, "more": [...]}

    def _write(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")))
        tmp.replace(self.path)

    def _book(self, book):
        return self.data.setdefault(book, {"leagues": {}, "more": []})

    def save_main(self, book, sports, rows):
        """A main-lines scan of `sports`: replaces those leagues' saved results."""
        at = int(time.time() * 1000)
        with self.lock:
            leagues = self._book(book)["leagues"]
            for s in sports:
                leagues[s] = {"at": at, "rows": []}
            for r in rows:
                leagues.setdefault(r.get("sport_key"), {"at": at, "rows": []})["rows"].append(r)
            self._prune(book)
            self._write()

    def save_more(self, book, event_ids, rows):
        """A More bets scan: replaces the saved bets for those games, keeps the rest."""
        at = int(time.time() * 1000)
        with self.lock:
            b = self._book(book)
            b["more"] = [r for r in b["more"] if r.get("event_id") not in set(event_ids)] + \
                        [dict(r, scanned_at=at) for r in rows]
            self._prune(book)
            self._write()

    def import_browser(self, book, results, more):
        """Scans an older version saved in the browser: keep each league's newer copy,
        and add More bets for games the bot doesn't have yet."""
        with self.lock:
            b = self._book(book)
            for sport, saved in (results or {}).items():
                if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list):
                    continue
                mine = b["leagues"].get(sport)
                if not mine or (saved.get("at") or 0) > (mine.get("at") or 0):
                    b["leagues"][sport] = {"at": saved.get("at") or 0, "rows": [r for r in saved["rows"] if isinstance(r, dict)]}
            have = {r.get("event_id") for r in b["more"]}
            b["more"] += [r for r in (more or []) if isinstance(r, dict) and r.get("event_id") not in have]
            self._prune(book)
            self._write()
            return {"results": b["leagues"], "more": b["more"]}

    def clear_more(self, book):
        with self.lock:
            self._book(book)["more"] = []
            self._write()

    def _prune(self, book):
        """Drop games that have started, and leagues left with nothing."""
        now = datetime.now(timezone.utc)
        b = self._book(book)
        for s in list(b["leagues"]):
            b["leagues"][s]["rows"] = _upcoming(b["leagues"][s]["rows"], now)
        b["more"] = _upcoming(b["more"], now)

    def load(self, book):
        with self.lock:
            if book in self.data:
                before = json.dumps(self.data[book], separators=(",", ":"))
                self._prune(book)
                if json.dumps(self.data[book], separators=(",", ":")) != before:
                    self._write()
            b = self.data.get(book, {"leagues": {}, "more": []})
            return {"results": b["leagues"], "more": b["more"]}
