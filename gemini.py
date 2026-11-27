"""
Gemini: an AI for chat questions the chatbot doesn't understand
---------------------------------------------------------------
Only questions the chatbot can't answer come here, together with a short fact sheet the bot
builds from its own numbers (the bet being discussed, tonight's top picks, a player's or
team's next game, form and injuries, your record). The AI is told to use only those facts,
to say when it doesn't know, and never to promise wins. Its answers are labeled in the chat.

The key goes in config.json as "gemini_key" (Google AI Studio). Free keys: Google may use
what's sent to improve its products, so nothing personal is sent: no names, emails or keys.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import stats

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Free-key limits per model (AI Studio, Nov 2026): 3.5 Flash-Lite 15 a minute / 50 a day,
# 3.1 Flash-Lite 15 / 5, 3.8 Flash 5 / 20 but over 40 s per answer (too slow for chat).
# 3.5 Flash-Lite first (~5 s an answer); when a model runs out, the next one is tried.
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
TIMEOUT = 25
PER_MINUTE, PER_DAY = 10, 55           # the bot's own cap, just inside the models' daily total
REST_AFTER_LIMIT = 30 * 60             # a model that says "limit reached" is skipped this long

SYSTEM = """You are the AI helper inside Odd Finder, a personal sports-betting analysis tool for FanDuel.
Answer the user's question in plain, friendly language, in at most about 120 words.
Rules:
- Use ONLY numbers, odds, chances, injuries and results from the FACTS section. Never invent statistics, odds, injuries, scores or news. If the facts don't cover something, say you don't know it and suggest what to ask the bot instead (for example "check a bet like 'Knicks -4.5 -110'" or "ask 'best NBA bets tonight'").
- "Chance of success" is the bot's estimated chance a bet wins. "Value" is how much a bet is worth at its price (positive = the price pays more than the chance is worth). A good bet has positive value, not just a high chance.
- Never promise or imply a bet will win. When you talk about placing a bet, a short reminder that even good bets lose often is enough; mention stake size (1-2% of a bankroll) only when the question is about how much to bet.
- Only if the user says or clearly shows they're losing control of their gambling (chasing losses, can't stop, lost too much), be kind, suggest a break and mention 1-800-GAMBLER. Otherwise don't mention it.
- You can't place bets, browse the internet or see live scores; don't pretend to.
- Only when the question asks whether to make a bet, start with Yes, No, Lean yes or Not really, then explain briefly. For other questions, just answer.
- Answer the question asked first; don't repeat facts that aren't relevant to it.
- For general questions (how hedging works, bankroll strategy, what a term means), explain the idea from general betting knowledge without numbers; don't bring up the facts' picks unless the user asks for picks or they directly answer the question.
Plain text only: no markdown headings or tables; short paragraphs or a few "-" bullet lines are fine."""


# Assist mode: the chatbot has already answered with exact numbers; the AI adds a short take.
ASSIST = """
ASSIST MODE: the bot has already shown the user its own answer, with the exact numbers (it's in the facts as
"The bot's answer"). Write a short follow-up of 1-3 sentences (under 60 words): the takeaway in plain, natural
words, or one useful extra point. Agree with the bot's verdict and numbers; never contradict them or invent new
ones. Don't repeat every number, don't start with "Yes"/"No" (the bot already did), and don't greet the user."""


def remaining(now=None):
    """AI answers left today under the bot's own cap."""
    now = now or time.time()
    with _lock:
        return max(PER_DAY - len([t for t in _calls if t >= _day_start(now)]), 0)


class GeminiError(Exception):
    pass


USAGE_FILE = stats.DATA_DIR / "ai_usage.json"   # when AI questions were asked, so restarts don't reset the count
PACIFIC = ZoneInfo("America/Los_Angeles")       # Google resets free daily limits at midnight Pacific time

_lock = threading.Lock()
_calls = []                            # times of recent calls, for the limits above


def _load_usage():
    try:
        return [float(t) for t in json.loads(USAGE_FILE.read_text()) if isinstance(t, (int, float))]
    except (OSError, ValueError, TypeError):
        return []


def _save_usage():
    try:
        USAGE_FILE.parent.mkdir(exist_ok=True)
        USAGE_FILE.write_text(json.dumps(_calls[-500:]))
    except OSError:
        pass


def _day_start(now):
    """Unix time of the last midnight Pacific before `now`."""
    d = datetime.fromtimestamp(now, PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


_calls.extend(_load_usage())
_model = {"name": None}
_resting = {}                          # model -> time it may be tried again


def _allowed(now):
    with _lock:
        _calls[:] = [t for t in _calls if now - t < 86400]
        if len([t for t in _calls if now - t < 60]) >= PER_MINUTE:
            raise GeminiError("The AI is getting a lot of questions; try again in a minute.")
        if len([t for t in _calls if t >= _day_start(now)]) >= PER_DAY:
            raise GeminiError("The AI has answered its daily limit of questions; it resets at midnight Pacific time.")
        _calls.append(now)
        _save_usage()


def _post(key, model, body, timeout):
    req = urllib.request.Request(f"{BASE}/{model}:generateContent", data=json.dumps(body).encode(),
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask(key, question, facts, history=(), model=None, post=_post, now=None, assist=False):
    """The AI's answer to `question`, given the bot's `facts` (text) and the last few chat
    lines `history` ([(who, text)], who = "user" or "bot"). Raises GeminiError."""
    if not key:
        raise GeminiError("No Gemini key set.")
    contents = []
    for who, text in list(history)[-6:]:
        contents.append({"role": "user" if who == "user" else "model", "parts": [{"text": str(text)[:600]}]})
    contents.append({"role": "user", "parts": [{"text": f"FACTS (from the bot, as of now):\n{facts[:6000]}\n\nQUESTION: {question[:500]}"}]})
    body = {"systemInstruction": {"parts": [{"text": SYSTEM + (ASSIST if assist else "")}]}, "contents": contents,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700}}
    t = now or time.time()
    order = [model] if model else ([_model["name"]] if _model["name"] else []) + [m for m in MODELS if m != _model["name"]]
    tried = [m for m in order if _resting.get(m, 0) <= t]
    if not tried:
        raise GeminiError("The AI's free limit is used up for now; it resets later (Google resets daily limits at midnight Pacific time).")
    _allowed(t)                                         # counts toward the bot's cap only when a model is asked
    last = None
    for m in tried:
        try:
            out = post(key, m, body, TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 429:                          # this model's limit: rest it, try the next one
                _resting[m] = t + REST_AFTER_LIMIT
                last = "limit reached"
                continue
            if e.code in (401, 403):
                raise GeminiError("Google didn't accept the Gemini key in config.json.")
            last = f"HTTP {e.code}"
            continue                                       # e.g. a model that isn't available: try the next
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            continue
        parts = ((out.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if text:
            _model["name"] = m
            return text
        last = "an empty answer"
    if last == "limit reached":
        raise GeminiError("The AI's free limit is used up for now; it resets later (Google resets daily limits at midnight Pacific time).")
    raise GeminiError(f"The AI didn't answer ({last}).")
