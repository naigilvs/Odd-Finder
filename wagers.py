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
MAX_LEGS = 15


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
    out = {"date": day.isoformat(), "note": note, "wagered": wagered, "result": result, "won": won}
    out.update(_link(data))                    # optional link to a saved pick in My picks
    # A parlay: one wager on several legs (each typed in or linked to a saved pick).
    if data.get("legs") is not None:
        legs = data["legs"]
        if not isinstance(legs, list) or not 2 <= len(legs) <= MAX_LEGS:
            raise ValueError(f"A parlay needs 2 to {MAX_LEGS} legs.")
        out["legs"] = []
        for i, leg in enumerate(legs, 1):
            if not isinstance(leg, dict) or not str(leg.get("note") or "").strip():
                raise ValueError(f"Parlay leg {i}: choose a pick or type what it is.")
            out["legs"].append(dict({"note": str(leg["note"]).strip()[:120]}, **_link(leg)))
        out.pop("pick_id", None)
        out.pop("decimal", None)
        if not out["note"]:
            out["note"] = f"{len(legs)}-leg parlay"
        odds = [leg.get("decimal") for leg in out["legs"]]
        if all(odds):                             # combined odds, when every leg has them
            combined = 1.0
            for d in odds:
                combined *= d
            out["decimal"] = round(combined, 4)
    return out


def _link(data):
    """An optional link to a saved pick (its id), with the pick's decimal odds."""
    pick_id = data.get("pick_id")
    if not pick_id:
        return {}
    if not isinstance(pick_id, str) or len(pick_id) > 40:
        raise ValueError("That pick link isn't valid.")
    out = {"pick_id": pick_id}
    try:
        odds = float(data.get("decimal") or 0)
    except (TypeError, ValueError):
        odds = 0
    if 1 < odds < 1000:
        out["decimal"] = round(odds, 4)
    return out


# Bankroll settings: what you started with and how carefully to size bets. Every limit is
# optional (0 = off) and only warns; nothing stops you from logging a wager.
BANKROLL_DEFAULTS = {
    "start": 0.0,           # bankroll before the wagers logged here (0 = not set up)
    "unit_pct": 1.0,        # one unit = this % of the bankroll
    "kelly": 0.25,          # fraction of Kelly used for suggested stakes (quarter Kelly)
    "cap_pct": 2.0,         # suggested stakes never above this % of the bankroll
    "max_stake": 0.0,       # warn when one wager is above this many dollars
    "daily_loss": 0.0,      # warn when the day's losses reach this many dollars
    "exposure_pct": 10.0,   # warn when pending bets exceed this % of the bankroll
}
BANKROLL_LIMITS = {"start": (0, MAX_AMOUNT), "unit_pct": (0.1, 10), "kelly": (0.05, 1), "cap_pct": (0.1, 25),
                   "max_stake": (0, MAX_AMOUNT), "daily_loss": (0, MAX_AMOUNT), "exposure_pct": (0, 100)}
BANKROLL_NAMES = {"start": "starting bankroll", "unit_pct": "unit size", "kelly": "Kelly fraction",
                  "cap_pct": "stake cap", "max_stake": "maximum stake", "daily_loss": "daily loss limit",
                  "exposure_pct": "pending limit"}


def clean_bankroll(data):
    if not isinstance(data, dict):
        raise ValueError("Those bankroll settings are missing.")
    out = {}
    for key, default in BANKROLL_DEFAULTS.items():
        try:
            x = float(data.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"Enter a number for the {BANKROLL_NAMES[key]}.")
        lo, hi = BANKROLL_LIMITS[key]
        if x != x or not lo <= x <= hi:
            raise ValueError(f"Enter a {BANKROLL_NAMES[key]} between {lo:g} and {hi:,g}.")
        out[key] = round(x, 4)
    return out


def clean_money(data):
    """One deposit or withdrawal from the page: {date, kind: "deposit"/"withdraw", amount, note}."""
    if not isinstance(data, dict):
        raise ValueError("That deposit or withdrawal is missing details.")
    if data.get("kind") not in ("deposit", "withdraw"):
        raise ValueError("Choose add money or withdraw.")
    amount = _money(data.get("amount"), "amount")
    if amount <= 0:
        raise ValueError("Enter how much money.")
    try:
        day = date.fromisoformat(str(data.get("date") or date.today().isoformat()))
    except ValueError:
        raise ValueError("Enter a valid date.")
    return {"date": day.isoformat(), "kind": data["kind"], "amount": amount if data["kind"] == "deposit" else -amount,
            "note": str(data.get("note") or "").strip()[:80]}


def _clean_moves(items):
    out = []
    for m in items if isinstance(items, list) else []:
        try:
            c = clean_money({"kind": m.get("kind"), "amount": abs(float(m.get("amount", 0))), "date": m.get("date"), "note": m.get("note")})
            c.update(id=str(m.get("id") or uuid.uuid4().hex[:12])[:20], created=str(m.get("created") or "")[:40])
            out.append(c)
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def net(w):
    return w["won"] if w["result"] == "won" else -w["wagered"] if w["result"] == "lost" else 0.0


class WagerStore:
    def __init__(self, path=WAGERS_FILE):
        self.path = path
        self.lock = threading.Lock()
        # bankroll settings sit next to the wagers: bankroll.json (bankroll_demo.json in demo mode)
        self.bankroll_path = path.parent / path.name.replace("wagers", "bankroll")
        try:
            raw = json.loads(self.bankroll_path.read_text())
            self.bankroll = clean_bankroll(raw)
            self.bankroll["moves"] = _clean_moves(raw.get("moves"))       # money added or withdrawn
        except (OSError, ValueError, AttributeError):
            self.bankroll = dict(BANKROLL_DEFAULTS, moves=[])
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

    def add_many(self, items):
        """Several wagers at once: all are checked first, and none are saved if any is wrong."""
        if not isinstance(items, list) or not items:
            raise ValueError("Add at least one wager.")
        if len(items) > 50:
            raise ValueError("Add at most 50 wagers at a time.")
        cleaned = []
        for i, data in enumerate(items, 1):
            try:
                cleaned.append(clean(data))
            except ValueError as e:
                raise ValueError(f"Wager {i}: {e}" if len(items) > 1 else str(e))
        now = datetime.now(timezone.utc)
        for i, w in enumerate(cleaned):
            # keep the order they were entered in (each a microsecond apart)
            w.update({"id": uuid.uuid4().hex[:12], "created": now.replace(microsecond=(now.microsecond + i) % 1_000_000).isoformat()})
        with self.lock:
            self.wagers.extend(cleaned)
            self._write()
        return cleaned

    def update(self, wid, data):
        w = clean(data)
        with self.lock:
            old = next((x for x in self.wagers if x["id"] == wid), None)
            if not old:
                raise KeyError("That wager wasn't found.")
            for key in ("pick_id", "decimal", "legs"):   # the edit decides links and legs
                old.pop(key, None)
            old.update(w)
            self._write()
            return old

    def _write_bankroll(self):
        tmp = self.bankroll_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.bankroll, indent=1))
        tmp.replace(self.bankroll_path)

    def set_bankroll(self, data):
        settings = clean_bankroll(data)
        with self.lock:
            settings["moves"] = self.bankroll.get("moves", [])           # settings don't touch deposits
            self.bankroll = settings
            self._write_bankroll()
        return settings

    def add_money(self, data):
        """Add money to the bankroll, or withdraw some."""
        move = clean_money(data)
        move.update(id=uuid.uuid4().hex[:12], created=datetime.now(timezone.utc).isoformat())
        with self.lock:
            self.bankroll.setdefault("moves", []).append(move)
            self._write_bankroll()
        return move

    def delete_money(self, mid):
        with self.lock:
            before = len(self.bankroll.get("moves", []))
            self.bankroll["moves"] = [m for m in self.bankroll.get("moves", []) if m["id"] != mid]
            if len(self.bankroll["moves"]) != before:
                self._write_bankroll()

    def delete(self, wid):
        with self.lock:
            before = len(self.wagers)
            self.wagers = [w for w in self.wagers if w["id"] != wid]
            if len(self.wagers) != before:
                self._write()
