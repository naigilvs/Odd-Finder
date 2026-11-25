"""
My wagers
---------
A simple log of the bets you actually placed: the date, an optional note, how much
you wagered, and whether it won (and how much), lost, pushed, or is still pending
(set the result later). Every entry can be edited. Kept in data/wagers.json on your
computer and backed up with your picks.

Net result of one wager:  won -> +amount won (profit, not counting the stake)
                          lost -> -amount wagered
                          push -> 0 (stake returned)
                          pending -> not counted yet
"""
import json
import threading
import uuid
from datetime import date, datetime, timezone

import stats

WAGERS_FILE = stats.DATA_DIR / "wagers.json"
RESULTS = ("pending", "won", "lost", "push")
MAX_AMOUNT = 1_000_000


def _money(value, name):
    try:
        x = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a number for {name}.")
    if x != x or x < 0 or x > MAX_AMOUNT:          # NaN, negative or absurd
        raise ValueError(f"Enter a {name} between $0 and ${MAX_AMOUNT:,}.")
    return x


def clean(data):
    """Check one wager from the page and keep only what we store."""
    if not isinstance(data, dict):
        raise ValueError("That wager is missing details.")
    wagered = _money(data.get("wagered"), "amount wagered")
    if wagered <= 0:
        raise ValueError("Enter how much you wagered.")
    result = data.get("result")
    if result not in RESULTS:
        raise ValueError("Choose whether it won, lost, pushed or is still pending.")
    won = _money(data.get("won") or 0, "amount won") if result == "won" else 0.0
    if result == "won" and won <= 0:
        raise ValueError("Enter how much you won (your profit, not counting the stake).")
    try:
        day = date.fromisoformat(str(data.get("date") or date.today().isoformat()))
    except ValueError:
        raise ValueError("Enter a valid date.")
    note = str(data.get("note") or "").strip()[:120]
    return {"date": day.isoformat(), "note": note, "wagered": wagered, "result": result, "won": won}


def net(w):
    return w["won"] if w["result"] == "won" else -w["wagered"] if w["result"] == "lost" else 0.0


class WagerStore:
    def __init__(self, path=WAGERS_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.wagers = json.loads(self.path.read_text())
            if not isinstance(self.wagers, list):
                raise ValueError
        except (OSError, ValueError):
            self.wagers = []

    def _write(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.wagers, indent=1))
        tmp.replace(self.path)          # never leaves a half-written file

    def all(self):
        with self.lock:
            return sorted(self.wagers, key=lambda w: (w["date"], w["created"]), reverse=True)

    def add(self, data):
        w = clean(data)
        w.update({"id": uuid.uuid4().hex[:12], "created": datetime.now(timezone.utc).isoformat()})
        with self.lock:
            self.wagers.append(w)
            self._write()
        return w

    def update(self, wid, data):
        w = clean(data)
        with self.lock:
            old = next((x for x in self.wagers if x["id"] == wid), None)
            if not old:
                raise KeyError("That wager wasn't found.")
            old.update(w)
            self._write()
            return old

    def delete(self, wid):
        with self.lock:
            before = len(self.wagers)
            self.wagers = [w for w in self.wagers if w["id"] != wid]
            if len(self.wagers) != before:
                self._write()
