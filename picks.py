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


# NBA player props are graded from the player's real stat in the game's box score.
PROP_SPORT = "basketball_nba"
PROP_MARKETS = {"player_points": "points", "player_rebounds": "rebounds",
                "player_assists": "assists", "player_threes": "3-pointers"}


def is_gradable_prop(pick):
    return (pick.get("sport_key") == PROP_SPORT and pick.get("market") in PROP_MARKETS
            and bool(pick.get("player")) and pick.get("outcome") in ("Over", "Under") and pick.get("point") is not None)


def auto_gradable(pick):
    if pick.get("player"):
        return is_gradable_prop(pick)
    m = base_market(pick.get("market") or "")
    return (pick.get("sport_key") in stats.LEAGUES and m in AUTO_MARKETS
            and not m.endswith(PERIOD_ENDINGS))


def grade_prop(pick, value):
    """Won, lost or push for an over/under prop, from the player's real stat."""
    diff = value - pick["point"] if pick["outcome"] == "Over" else pick["point"] - value
    return "won" if diff > 0 else "lost" if diff < 0 else "push"


def _find_player_stat(*args, **kw):
    import props_stats                         # imported when needed: it loads player stats
    return props_stats.find_player_stat(*args, **kw)


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


def clean(data):
    """Keep only the known fields of a bet, with the right types."""
    bet = {f: str(data[f])[:300] for f in TEXT_FIELDS if isinstance(data.get(f), (str, int, float))}
    bet.update({f: float(data[f]) for f in NUMBER_FIELDS
                if isinstance(data.get(f), (int, float)) and not isinstance(data.get(f), bool)})
    if not all(bet.get(f) for f in ("event_id", "market", "selection", "start")) or "decimal" not in bet:
        raise ValueError("That bet is missing details, so it can't be saved.")
    return bet


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
        for p in self.picks:                  # picks saved before a bet type became automatic
            if p.get("status") == "pending":
                p["auto"] = auto_gradable(p)

    def _write(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.picks, indent=1))
        tmp.replace(self.path)          # never leaves a half-written file

    def all(self):
        with self.lock:
            return list(self.picks)

    def save(self, data, stake=None):
        pick = clean(data)
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

    def grade_pending(self, refresh=True, find=stats.find_final_score, find_stat=_find_player_stat, now=None):
        """Grade every finished, automatically gradable pick. Returns how many were graded."""
        now = now or datetime.now(timezone.utc)
        with self.lock:
            todo = [p for p in self.picks if p["status"] == "pending" and auto_gradable(p)
                    and datetime.fromisoformat(p["start"].replace("Z", "+00:00")) + GRADE_AFTER < now]
        graded = 0
        refreshed = set()
        for p in todo:
            sport = p["sport_key"]
            fresh = refresh and sport not in refreshed
            refreshed.add(sport)
            if p.get("player"):                       # NBA prop: the player's real stat
                found = find_stat(p["market"], p["player"], p["home_team"], p["away_team"], p["start"], refresh=fresh)
                if found is None:
                    continue
                how, value = found
                if how == "did_not_play":
                    update = {"status": "push", "final": f"{p['player']} didn't play: bet void, stake returned"}
                else:
                    update = {"status": grade_prop(p, value),
                              "final": f"{p['player']}: {_num(value)} {PROP_MARKETS[p['market']]}"}
            else:
                score = find(sport, p["home_team"], p["away_team"], p["start"], refresh=fresh)
                if score is None:
                    continue
                outcome = grade(p, *score)
                if outcome is None:
                    continue
                update = {"status": outcome,
                          "final": f"{p['away_team']} {_num(score[1])}, {p['home_team']} {_num(score[0])}"}
            with self.lock:
                p.update(update, graded_by="auto", graded_at=now.isoformat())
                graded += 1
        if graded:
            with self.lock:
                self._write()
        return graded


# --------------------------------------------------------------------------- bot track record

TRACK_FILE = stats.DATA_DIR / "track.json"


def _key(b):
    return (b["event_id"], b["market"], b["selection"])


class TrackStore(PickStore):
    """Every bet the bot showed that can be graded automatically, logged before the game
    and checked after it. Each bet is kept once (a rescan updates it to the latest price)."""

    def __init__(self, path=TRACK_FILE):
        super().__init__(path)

    def _write(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.picks, separators=(",", ":")))   # compact: this file grows
        tmp.replace(self.path)

    def log(self, bets, now=None):
        now = now or datetime.now(timezone.utc)
        added = updated = 0
        with self.lock:
            index = {_key(b): b for b in self.picks}
            for raw in bets[:3000]:
                if not isinstance(raw, dict):
                    continue
                try:
                    bet = clean(raw)
                    starts = datetime.fromisoformat(bet["start"].replace("Z", "+00:00"))
                except (ValueError, KeyError):
                    continue
                if not auto_gradable(bet) or starts <= now:      # only bets seen before the game
                    continue
                bet["agree"] = raw.get("agree") is True
                old = index.get(_key(bet))
                if old:
                    if old["status"] == "pending":
                        old.update(bet)
                        old["last_seen"] = now.isoformat()
                        updated += 1
                    continue
                bet.update({"id": uuid.uuid4().hex[:12], "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                            "status": "pending", "auto": True})
                self.picks.append(bet)
                index[_key(bet)] = bet
                added += 1
            if added or updated:
                self._write()
        return added, updated

    def clear(self):
        with self.lock:
            self.picks = []
            self._write()


CHANCE_BUCKETS = [(0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]


def _block(bets):
    """Record and $100-a-bet profit for a group of finished bets."""
    decided = [b for b in bets if b["status"] != "push"]
    wins = sum(1 for b in decided if b["status"] == "won")
    expected = sum(b.get("chance", b.get("fair_prob", 0)) for b in decided)
    profit = sum(100 * (b["decimal"] - 1) if b["status"] == "won" else -100 if b["status"] == "lost" else 0 for b in bets)
    return {"bets": len(bets), "wins": wins, "losses": len(decided) - wins, "pushes": len(bets) - len(decided),
            "win_rate": wins / len(decided) if decided else None,
            "expected": expected / len(decided) if decided else None,
            "profit": round(profit, 2), "roi": profit / (100 * len(bets)) if bets else None}


def track_summary(bets, since=None):
    """How the bot's predictions turned out, for bets starting on or after `since` (ISO time)."""
    sel = [b for b in bets if not since or b["start"] >= since]
    done = [b for b in sel if b["status"] in ("won", "lost", "push")]

    def group(key_fn, label_fn=lambda k: k):
        groups = {}
        for b in done:
            groups.setdefault(key_fn(b), []).append(b)
        rows = [dict(_block(v), name=label_fn(k)) for k, v in groups.items()]
        return sorted(rows, key=lambda r: -r["bets"])

    calibration = []
    for lo, hi in CHANCE_BUCKETS:
        g = [b for b in done if lo <= b.get("chance", b.get("fair_prob", 0)) < hi]
        if g:
            calibration.append(dict(_block(g), name=f"{round(lo * 100)}–{min(round(hi * 100), 100)}%"))
    recent = sorted(done, key=lambda b: b["start"], reverse=True)[:60]
    return {
        "tracked": len(sel), "pending": len(sel) - len(done), "settled": len(done),
        "all": _block(done),
        "value": _block([b for b in done if b.get("ev", 0) > 0]),
        "agree": _block([b for b in done if b.get("agree")]),
        "calibration": calibration,
        "by_confidence": [r for lvl in ("High", "Medium", "Low") for r in group(lambda b: b.get("confidence") or "–") if r["name"] == lvl],
        "by_league": group(lambda b: b.get("sport") or b["sport_key"]),
        "by_type": group(lambda b: base_market(b["market"]),
                         lambda k: {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total", "team_totals": "Team total",
                                    **{m: "Player " + n for m, n in PROP_MARKETS.items()}}.get(k, k)),
        "recent": [{k: b.get(k) for k in ("label", "selection", "event", "sport", "start", "american", "chance",
                                           "confidence", "ev", "status", "final", "market_label")} for b in recent],
    }
