"""
Saved picks
-----------
Picks you save are kept in data/picks.json (on your computer, not on GitHub).
After a game ends, moneylines, spreads, totals and team totals in the NFL, NBA,
NHL and MLB are graded automatically from the final score. Everything else
(player props, quarters/halves, other sports) you mark won/lost/push yourself.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import stats

PICKS_FILE = stats.DATA_DIR / "picks.json"
GRADE_AFTER = timedelta(hours=3)            # a game should be over this long after it starts
AUTO_MARKETS = {"h2h", "spreads", "totals", "team_totals"}
PERIOD_ENDINGS = ("_q1", "_q2", "_q3", "_q4", "_h1", "_h2", "_p1", "_p2", "_p3", "_innings")
STATUSES = {"pending", "won", "lost", "push"}

# What we keep about a pick when it's saved (anything else sent is ignored).
TEXT_FIELDS = ["event_id", "sport_key", "sport", "event", "home_team", "away_team", "start", "market",
               "market_label", "selection", "label", "outcome", "team_total_for", "player", "american",
               "book", "confidence"]
NUMBER_FIELDS = ["point", "decimal", "chance", "fair_prob", "stats_prob", "book_prob", "ev"]


def base_market(market):
    return market[len("alternate_"):] if market.startswith("alternate_") else market


def auto_gradable(pick):
    m = base_market(pick.get("market") or "")
    return (pick.get("sport_key") in stats.LEAGUES and m in AUTO_MARKETS
            and not m.endswith(PERIOD_ENDINGS) and not pick.get("player"))


def grade(pick, home_score, away_score):
    """Win, loss or push for a pick from the final score, or None if it can't be told."""
    m = base_market(pick["market"])
    home, away, outcome, point = pick["home_team"], pick["away_team"], pick["outcome"], pick.get("point")

    def side_margin():
        if outcome == home:
            return home_score - away_score
        if outcome == away:
            return away_score - home_score
        return None

    def result(x):
        return "won" if x > 0 else "lost" if x < 0 else "push"

    if m == "h2h":
        if outcome == "Draw":
            return "won" if home_score == away_score else "lost"
        margin = side_margin()
        return None if margin is None else result(margin)
    if point is None:
        return None
    if m == "spreads":
        margin = side_margin()
        return None if margin is None else result(margin + point)
    if m in ("totals", "team_totals"):
        if m == "totals":
            score = home_score + away_score
        else:
            team = pick.get("team_total_for")
            score = home_score if team == home else away_score if team == away else None
            if score is None:
                return None
        if outcome == "Over":
            return result(score - point)
        if outcome == "Under":
            return result(point - score)
    return None


def _num(x):
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


class PickStore:
    def __init__(self, path=PICKS_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.picks = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.picks = []

    def _write(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.picks, indent=1))
        tmp.replace(self.path)          # never leaves a half-written file

    def all(self):
        with self.lock:
            return list(self.picks)

    def save(self, data, stake=None):
        pick = {f: str(data[f])[:300] for f in TEXT_FIELDS if isinstance(data.get(f), (str, int, float))}
        pick.update({f: float(data[f]) for f in NUMBER_FIELDS if isinstance(data.get(f), (int, float))})
        if not all(pick.get(f) for f in ("event_id", "market", "selection", "start")) or "decimal" not in pick:
            raise ValueError("That bet is missing details, so it can't be saved.")
        with self.lock:
            for p in self.picks:          # saving the same bet twice keeps the first one
                if (p["event_id"], p["market"], p["selection"]) == (pick["event_id"], pick["market"], pick["selection"]):
                    return p
            pick.update({"id": uuid.uuid4().hex[:12], "saved_at": datetime.now(timezone.utc).isoformat(),
                         "status": "pending", "auto": auto_gradable(pick), "stake": None})
            if isinstance(stake, (int, float)) and stake > 0:
                pick["stake"] = round(float(stake), 2)
            self.picks.append(pick)
            self._write()
            return pick

    def update(self, pid, changes):
        with self.lock:
            p = next((p for p in self.picks if p["id"] == pid), None)
            if not p:
                raise KeyError("That pick wasn't found.")
            if "stake" in changes:
                s = changes["stake"]
                p["stake"] = round(float(s), 2) if isinstance(s, (int, float)) and s > 0 else None
            if changes.get("status") in STATUSES:
                p["status"] = changes["status"]
                p["graded_by"] = None if p["status"] == "pending" else "you"
                p["graded_at"] = None if p["status"] == "pending" else datetime.now(timezone.utc).isoformat()
                if p["status"] == "pending":
                    p.pop("final", None)
            self._write()
            return p

    def delete(self, pid):
        with self.lock:
            before = len(self.picks)
            self.picks = [p for p in self.picks if p["id"] != pid]
            if len(self.picks) != before:
                self._write()

    def grade_pending(self, refresh=True, find=stats.find_final_score, now=None):
        """Grade every finished, automatically gradable pick. Returns how many were graded."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            todo = [p for p in self.picks if p["status"] == "pending" and p.get("auto")
                    and datetime.fromisoformat(p["start"].replace("Z", "+00:00")) + GRADE_AFTER < now]
        graded = 0
        refreshed = set()
        for p in todo:
            sport = p["sport_key"]
            score = find(sport, p["home_team"], p["away_team"], p["start"], refresh=refresh and sport not in refreshed)
            refreshed.add(sport)
            if score is None:
                continue
            outcome = grade(p, *score)
            if outcome is None:
                continue
            with self.lock:
                p.update({"status": outcome, "graded_by": "auto", "graded_at": now.isoformat(),
                          "final": f"{p['away_team']} {_num(score[1])}, {p['home_team']} {_num(score[0])}"})
                graded += 1
        if graded:
            with self.lock:
                self._write()
        return graded
