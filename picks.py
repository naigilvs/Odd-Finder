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
import uuid
from datetime import datetime, timedelta, timezone

import stats

PICKS_FILE = stats.DATA_DIR / "picks.json"
GRADE_AFTER = timedelta(hours=3)            # a game should be over this long after it starts
GIVE_UP_AFTER = timedelta(days=3)           # no result by then: postponed or cancelled, so you mark it
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


# NBA, NHL and NFL player props are graded from the player's real stat in the box score.
import props_stats


def prop_label(sport, market):
    cfg = props_stats.SPORTS.get(sport)
    return cfg["labels"][cfg["markets"][market]] if cfg and market in cfg["markets"] else market


def is_gradable_prop(pick):
    return (props_stats.handles(pick) and bool(pick.get("player"))
            and pick.get("outcome") in ("Over", "Under") and pick.get("point") is not None)


def auto_gradable(pick):
    if pick.get("player"):
        return is_gradable_prop(pick)
    m = base_market(pick.get("market") or "")
    point = pick.get("point")
    quarter = point is not None and abs(point * 2 - round(point * 2)) > 1e-9   # -0.25 / +0.75: split results
    league = stats.LEAGUES.get(pick.get("sport_key"))
    return (league is not None and not league.get("cup") and m in AUTO_MARKETS
            and not m.endswith(PERIOD_ENDINGS) and not quarter)


def grade_prop(pick, value):
    """Won, lost or push for an over/under prop, from the player's real stat."""
    diff = value - pick["point"] if pick["outcome"] == "Over" else pick["point"] - value
    return "won" if diff > 0 else "lost" if diff < 0 else "push"


def _find_player_stat(*args, **kw):
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
    drop_unknown = False          # a prop that can't be graded automatically: you mark it

    def __init__(self, path=PICKS_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.picks = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.picks = []
        for p in self.picks:                  # picks saved before a bet type became automatic
            if p.get("status") == "pending" and not p.get("final"):
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

    def _give_up(self, p, note="Result not found (postponed or cancelled?): mark it yourself"):
        """Stop checking a pick automatically: you mark it (the track record just leaves it out)."""
        with self.lock:
            if self.drop_unknown:
                if p in self.picks:
                    self.picks.remove(p)
            else:
                p["auto"] = False
                p["final"] = note
        return 1

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
            todo = [p for p in self.picks if p["status"] == "pending" and p.get("auto") and auto_gradable(p)
                    and datetime.fromisoformat(p["start"].replace("Z", "+00:00")) + GRADE_AFTER < now]
        graded = 0
        refreshed = set()
        for p in todo:
            sport = p["sport_key"]
            fresh = refresh and sport not in refreshed
            refreshed.add(sport)
            stale = datetime.fromisoformat(p["start"].replace("Z", "+00:00")) + GIVE_UP_AFTER < now
            if p.get("player"):                       # player prop: the player's real stat
                found = find_stat(sport, p["market"], p["player"], p["home_team"], p["away_team"], p["start"], refresh=fresh)
                if found is None:
                    if stale:
                        graded += self._give_up(p)
                    continue
                how, value = found
                if how == "unknown":                  # can't tell from the box score
                    graded += self._give_up(p, "Couldn't tell from the box score: mark it yourself")
                    continue
                if how == "did_not_play":
                    update = {"status": "push", "final": f"{p['player']} didn't play: bet void, stake returned"}
                else:
                    update = {"status": grade_prop(p, value),
                              "final": f"{p['player']}: {_num(value)} {prop_label(sport, p['market'])}"}
            else:
                score = find(sport, p["home_team"], p["away_team"], p["start"], refresh=fresh)
                if score is None:
                    if stale:
                        graded += self._give_up(p)
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

    drop_unknown = True           # the track record leaves out bets it can't grade

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
                near_start = (starts - now).total_seconds() <= CLOSE_WINDOW
                if old:
                    if old["status"] == "pending":
                        old.update(bet)
                        old["last_seen"] = now.isoformat()
                        if near_start:
                            _set_close(old, bet["fair_prob"], bet["decimal"], now, "scan")
                        updated += 1
                    continue
                bet.update({"id": uuid.uuid4().hex[:12], "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                            "status": "pending", "auto": True})
                # The price when first seen: later scans update the current price, so the two
                # show how the line moved (and, near the start, how the closing price compares).
                bet.update({f"open_{k}": bet[k] for k in ("american", "decimal", "fair_prob", "point") if k in bet})
                if near_start and "fair_prob" in bet:
                    _set_close(bet, bet["fair_prob"], bet["decimal"], now, "scan")
                self.picks.append(bet)
                index[_key(bet)] = bet
                added += 1
            if added or updated:
                self._write()
        return added, updated

    def record_closes(self, rows, now=None):
        """Closing prices from a scan just before the start: each row is a valued bet
        ({event_id, market, selection, fair_prob, decimal}). Returns how many bets got one."""
        now = now or datetime.now(timezone.utc)
        done = 0
        with self.lock:
            index = {_key(b): b for b in self.picks if b["status"] == "pending"}
            for r in rows:
                b = index.get((r.get("event_id"), r.get("market"), r.get("selection")))
                if not b or r.get("fair_prob") is None:
                    continue
                if datetime.fromisoformat(b["start"].replace("Z", "+00:00")) <= now:
                    continue                                   # already started: too late
                _set_close(b, r["fair_prob"], r.get("decimal"), now, "closing scan")
                done += 1
            if done:
                self._write()
        return done

    def due_for_close(self, now=None, window=None):
        """Pending main-line bets whose game starts within `window` seconds (default: the
        closing-scan window)."""
        now = now or datetime.now(timezone.utc)
        window = window or CLOSING_SCAN_MINUTES * 60
        with self.lock:
            out = []
            for b in self.picks:
                if b["status"] != "pending" or b["market"] not in ("h2h", "spreads", "totals"):
                    continue
                left = (datetime.fromisoformat(b["start"].replace("Z", "+00:00")) - now).total_seconds()
                if 0 < left <= window:
                    out.append(dict(b))
            return out

    def clear(self):
        with self.lock:
            self.picks = []
            self._write()


# Closing line value (CLV): did the price when the bot first saw a bet beat the market's
# final fair price just before the start? Bettors who beat the close usually profit in the
# long run, and CLV shows it in hundreds of bets, where win/loss takes thousands.
CLOSE_WINDOW = 60 * 60              # a scan this close to the start counts as the closing price
CLOSING_SCAN_MINUTES = 15           # free closing scans look at games starting this soon
MIN_LEAD = 60 * 60                  # first seen at least this long before the close, or no CLV


def _set_close(b, fair, decimal, now, source):
    b.update(close_fair=round(float(fair), 4), close_at=now.isoformat(), close_source=source)
    if decimal:
        b["close_decimal"] = float(decimal)


def clv(b):
    """The first price's value measured with the closing fair chance: +0.03 means the bet was
    priced 3% better than the market finally said it was worth. None without both prices."""
    if b.get("close_fair") is None or not b.get("open_decimal") or not b.get("first_seen") or not b.get("close_at"):
        return None
    first = datetime.fromisoformat(b["first_seen"].replace("Z", "+00:00"))
    close = datetime.fromisoformat(b["close_at"].replace("Z", "+00:00"))
    if (close - first).total_seconds() < MIN_LEAD:
        return None                     # first seen right before the close: nothing to measure
    return b["open_decimal"] * b["close_fair"] - 1


def first_value(b):
    """Value when the bot first saw the bet (its first price and the market then)."""
    if b.get("open_decimal") and b.get("open_fair_prob") is not None:
        return b["open_decimal"] * b["open_fair_prob"] - 1
    return b.get("ev", 0)


def _range(values, z=1.96):
    """Likely range (about 95%) for the average of `values`, or None with fewer than 2."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    sd = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5
    half = z * sd / n ** 0.5
    return mean - half, mean + half


def _clv_block(bets, name):
    values = [c for c in (clv(b) for b in bets) if c is not None]
    r = _range(values)
    return {"name": name, "bets": len(values), "beat": sum(1 for v in values if v > 0),
            "avg": sum(values) / len(values) if values else None,
            "lo": r[0] if r else None, "hi": r[1] if r else None}


# The page's numbers for a bet (static/calc.js: chance, confidence, agrees), so bets the
# bot scans on its own are logged exactly as if the page had scanned them.
MARKET_WEIGHT = 0.75


def page_numbers(r, s=None, inj=None):
    """chance, confidence label, agree and stats_prob for a scanned row, given its stats
    opinion `s` ({prob} or {reason} or None) and injury news `inj` ({player} or {})."""
    has = bool(s and s.get("prob") is not None)
    chance = MARKET_WEIGHT * r["fair_prob"] + (1 - MARKET_WEIGHT) * s["prob"] if has else r["fair_prob"]
    score, n = 0, len(r.get("books_used") or [])
    score += 2 if n >= 7 else 1 if n >= 4 else 0
    score += 1 if r.get("sharp") else 0
    if has:
        same = (s["prob"] > r["book_prob"]) == (r["fair_prob"] > r["book_prob"])
        score += 1 if same and abs(s["prob"] - r["fair_prob"]) <= 0.1 else -1
    score -= 1 if r.get("player") else 0
    score -= 2 if r.get("estimated") else 0
    pl = (inj or {}).get("player")
    if pl:
        score -= 10 if pl["level"] == "out" else 3 if pl["level"] == "doubtful" else 2
    level = "High" if score >= 3 else "Medium" if score >= 1 else "Low"
    return {"chance": chance, "confidence": level, "stats_prob": s["prob"] if has else None,
            "agree": bool(has and r["ev"] > 0 and s["prob"] > r["book_prob"])}


CHANCE_BUCKETS = [(0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]


def _block(bets):
    """Record and $100-a-bet profit for a group of finished bets."""
    decided = [b for b in bets if b["status"] != "push"]
    wins = sum(1 for b in decided if b["status"] == "won")
    expected = sum(b.get("chance", b.get("fair_prob", 0)) for b in decided)
    each = [100 * (b["decimal"] - 1) if b["status"] == "won" else -100 if b["status"] == "lost" else 0 for b in bets]
    profit = sum(each)
    r = _range(each)                     # likely range of the total: n x the average's range
    return {"bets": len(bets), "wins": wins, "losses": len(decided) - wins, "pushes": len(bets) - len(decided),
            "profit_lo": round(r[0] * len(each), 2) if r else None, "profit_hi": round(r[1] * len(each), 2) if r else None,
            "win_rate": wins / len(decided) if decided else None,
            "expected": expected / len(decided) if decided else None,
            "profit": round(profit, 2), "roi": profit / (100 * len(bets)) if bets else None}


def side_key(b):
    """The sides of one bet share a key: Over and Under 24.5, Lakers -3.5 and Celtics +3.5,
    or all three outcomes of a soccer moneyline."""
    point = b.get("point")
    return (b["event_id"], b["market"], b.get("player") or "", b.get("team_total_for") or "",
            abs(point) if point is not None else None)


def bot_sides(bets, by="value"):
    """The bot's side of each bet: the one with the best value (the one ranked higher in Find
    bets), the higher chance of success if values tie. A bet is one game's total, spread or
    moneyline, or one player's stat: if the line moved between scans (Over 223.5, later Under
    224.5) it is still one bet, so the bot gets one pick, not one per line."""
    groups = {}
    for b in bets:
        groups.setdefault(side_key(b)[:4], []).append(b)          # every line and side of the bet
    chance = lambda b: b.get("chance", b.get("fair_prob", 0))
    if by == "chance":                         # the side the bot thinks is most likely to win
        return [max(g, key=lambda b: (chance(b), b.get("ev", 0))) for g in groups.values()]
    return [max(g, key=lambda b: (b.get("ev", 0), chance(b))) for g in groups.values()]


def side_check(bets):
    """In finished bets where one side won and another lost: how often each opinion chose the
    winning side. Value (the bot's side) can favor the less likely side at a good price, so its
    profit matters more than how often it's right."""
    groups = {}
    for b in bets:
        groups.setdefault(side_key(b), []).append(b)
    judges = [("Bot's pick (better value)", lambda b: b.get("ev", 0)),
              ("Bot's favorite (higher chance of success)", lambda b: b.get("chance", b.get("fair_prob", 0))),
              ("Market's side (higher fair chance)", lambda b: b.get("fair_prob", 0)),
              ("Stats' side", lambda b: b.get("stats_prob")),
              ("Sportsbook favorite", lambda b: b.get("book_prob", 0))]
    out = []
    for name, f in judges:
        right = total = 0
        profit = 0.0
        for g in groups.values():
            statuses = {b["status"] for b in g}
            if "won" not in statuses or "lost" not in statuses:
                continue                                    # unfinished, pushed or only one side logged
            if any(f(b) is None for b in g):
                continue                                    # e.g. no stats opinion for this bet
            top = max(g, key=f)
            if sum(1 for b in g if f(b) == f(top)) > 1:
                continue                                    # no clear side
            total += 1
            right += top["status"] == "won"
            profit += 100 * (top["decimal"] - 1) if top["status"] == "won" else -100
        if total:
            out.append({"name": name, "bets": total, "right": right, "rate": right / total, "profit": round(profit, 2)})
    return out


def track_summary(bets, since=None, view="side"):
    """How the bot's predictions turned out, for bets starting on or after `since` (ISO time).
    view: "likely" counts only the side the bot thinks is most likely to win, "side" only the
    side with the better value, "value" only good-value bets, "all" every side logged."""
    sel = [b for b in bets if not since or b["start"] >= since]
    check = side_check([b for b in sel if b["status"] in ("won", "lost", "push")])
    if view == "likely":
        sel = bot_sides(sel, by="chance")
    elif view == "agree":                      # the bot's pick and its favorite are the same side
        favs = {id(b) for b in bot_sides(sel, by="chance")}
        sel = [b for b in bot_sides(sel) if id(b) in favs]
    elif view == "side":
        sel = bot_sides(sel)
    elif view == "value":
        sel = [b for b in sel if b.get("ev", 0) > 0]
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
        "view": view if view in ("likely", "side", "value", "agree") else "all", "side_check": check,
        "clv": [b for b in (_clv_block([b for b in sel if first_value(b) > 0], "Good value when first seen"),
                            _clv_block(sel, {"likely": "Bot's favorites", "side": "Bot's picks", "agree": "Pick and favorite agree",
                                             "value": "Value bets",
                                             "all": "Both sides of every bet"}.get(view, "Both sides of every bet")))],
        "tracked": len(sel), "pending": len(sel) - len(done), "settled": len(done),
        "all": _block(done),
        "value": _block([b for b in done if b.get("ev", 0) > 0]),
        "agree": _block([b for b in done if b.get("agree")]),
        "calibration": calibration,
        "by_confidence": [r for lvl in ("High", "Medium", "Low") for r in group(lambda b: b.get("confidence") or "–") if r["name"] == lvl],
        "by_league": group(lambda b: b.get("sport") or b["sport_key"]),
        "by_type": group(lambda b: base_market(b["market"]),
                         lambda k: {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total", "team_totals": "Team total",
                                    **{m: "Player " + cfg["labels"][st] for cfg in props_stats.SPORTS.values()
                                       for m, st in cfg["markets"].items()}}.get(k, k)),
        "recent": [{k: b.get(k) for k in ("label", "selection", "event", "sport", "start", "american", "chance",
                                           "confidence", "ev", "status", "final", "market_label")} for b in recent],
    }
