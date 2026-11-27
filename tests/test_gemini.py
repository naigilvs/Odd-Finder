"""Tests for the AI fallback (gemini.py, app.ai_answer) with a fake Gemini, so nothing is sent."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import gemini  # noqa: E402


def reply(text, thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return {"candidates": [{"content": {"parts": parts}}]}


class Ask(unittest.TestCase):
    def tearDown(self):
        self.usage.stop()
        self.tmp.cleanup()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.usage = mock.patch.object(gemini, "USAGE_FILE", Path(self.tmp.name) / "ai_usage.json")
        self.usage.start()
        gemini._calls.clear()
        gemini._model["name"] = None
        gemini._resting.clear()

    def test_sends_facts_question_and_history(self):
        seen = {}

        def post(key, model, body, timeout):
            seen.update(key=key, model=model, body=body)
            return reply("Lean yes. It has value.", thought="(hidden reasoning)")
        out = gemini.ask("k", "should I?", "Bet being discussed: X", [("user", "hi"), ("bot", "hello")], post=post)
        self.assertEqual(out, "Lean yes. It has value.")                     # the model's hidden thinking is dropped
        self.assertEqual(seen["model"], gemini.MODELS[0])
        contents = seen["body"]["contents"]
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertIn("FACTS", contents[-1]["parts"][0]["text"])
        self.assertIn("Bet being discussed: X", contents[-1]["parts"][0]["text"])
        self.assertIn("Never invent", seen["body"]["systemInstruction"]["parts"][0]["text"])

    def test_falls_back_to_the_next_model_and_remembers_it(self):
        calls = []

        def post(key, model, body, timeout):
            calls.append(model)
            if model == gemini.MODELS[0]:
                raise urllib.error.HTTPError("u", 404, "gone", {}, None)
            return reply("ok")
        self.assertEqual(gemini.ask("k", "q", "f", post=post), "ok")
        self.assertEqual(calls, gemini.MODELS[:2])
        gemini.ask("k", "q", "f", post=post)
        self.assertEqual(calls[-1], gemini.MODELS[1])                       # starts with the one that worked

    def test_a_model_at_its_limit_hands_over_to_the_next(self):
        calls = []

        def post(key, model, body, timeout):
            calls.append(model)
            if model == gemini.MODELS[0]:
                raise urllib.error.HTTPError("u", 429, "limit", {}, None)
            return reply("ok")
        self.assertEqual(gemini.ask("k", "q", "f", post=post, now=5000), "ok")
        self.assertEqual(gemini.ask("k", "q", "f", post=post, now=5100), "ok")
        self.assertEqual(calls, [gemini.MODELS[0], gemini.MODELS[1], gemini.MODELS[1]])   # resting one skipped
        gemini.ask("k", "q", "f", post=post, now=5000 + gemini.REST_AFTER_LIMIT + 1)
        self.assertEqual(calls[-2:], [gemini.MODELS[1]] * 1 + calls[-1:])

    def test_errors_and_limits(self):
        def limited(*a):
            raise urllib.error.HTTPError("u", 429, "limit", {}, None)

        def bad_key(*a):
            raise urllib.error.HTTPError("u", 403, "no", {}, None)
        with self.assertRaisesRegex(gemini.GeminiError, "limit is used up"):
            gemini.ask("k", "q", "f", post=limited)                         # every model at its limit
        self.assertEqual(set(gemini._resting), set(gemini.MODELS))
        with self.assertRaisesRegex(gemini.GeminiError, "limit is used up"):
            gemini.ask("k", "q", "f", post=lambda *a: reply("x"))           # still resting: not even tried
        gemini._resting.clear()
        with self.assertRaisesRegex(gemini.GeminiError, "didn't accept"):
            gemini.ask("k", "q", "f", post=bad_key)
        with self.assertRaisesRegex(gemini.GeminiError, "No Gemini key"):
            gemini.ask("", "q", "f")
        gemini._calls.clear()
        for _ in range(gemini.PER_MINUTE):
            gemini.ask("k", "q", "f", post=lambda *a: reply("x"), now=1000)
        with self.assertRaisesRegex(gemini.GeminiError, "a minute"):
            gemini.ask("k", "q", "f", post=lambda *a: reply("x"), now=1001)


class Endpoint(unittest.TestCase):
    def test_ai_answer_uses_facts_from_the_page(self):
        with mock.patch.object(app, "GEMINI_KEY", "k"), \
                mock.patch.object(app.gemini, "ask", return_value="Not really.") as ask, \
                mock.patch.object(app, "ai_facts", wraps=lambda m, f: "\n".join(f)):
            out = app.ai_answer({"message": "is this good?", "facts": ["Bet being discussed: Knicks -4.5"],
                                 "history": [{"who": "user", "text": "hi"}, "junk"]})
        self.assertEqual(out["answer"], "Not really.")
        self.assertIn("left", out)
        self.assertIn("Knicks -4.5", ask.call_args[0][2])
        self.assertEqual(ask.call_args[0][3], [("user", "hi")])
        with mock.patch.object(app, "GEMINI_KEY", None):
            self.assertIn("error", app.ai_answer({"message": "x"}))

    def test_assist_mode_adds_the_bots_answer_and_short_take_rules(self):
        with mock.patch.object(app, "GEMINI_KEY", "k"), \
                mock.patch.object(app.gemini, "ask", return_value="Fair price, no edge.") as ask, \
                mock.patch.object(app, "ai_facts", wraps=lambda m, f: "facts"):
            app.ai_answer({"mode": "assist", "message": "knicks -4.5", "bot_answer": "Not really — no edge at this price."})
        self.assertIn("The bot's answer (already shown to the user): Not really", ask.call_args[0][2])
        self.assertTrue(ask.call_args[1]["assist"])

    def test_remaining_counts_down_survives_restarts_and_resets_at_midnight_pacific(self):
        import tempfile
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gemini, "USAGE_FILE", Path(d) / "u.json"):
            gemini._calls.clear()
            gemini._resting.clear()
            noon = datetime(2026, 11, 27, 12, 0, tzinfo=gemini.PACIFIC).timestamp()
            self.assertEqual(gemini.remaining(now=noon), gemini.PER_DAY)
            gemini.ask("k", "q", "f", post=lambda *a: reply("x"), now=noon)
            self.assertEqual(gemini.remaining(now=noon), gemini.PER_DAY - 1)
            gemini._calls[:] = gemini._load_usage()                                  # as after a restart
            self.assertEqual(gemini.remaining(now=noon + 60), gemini.PER_DAY - 1)
            just_after_midnight = datetime(2026, 11, 28, 0, 1, tzinfo=gemini.PACIFIC).timestamp()
            self.assertEqual(gemini.remaining(now=just_after_midnight), gemini.PER_DAY)   # Google's reset
            gemini._calls.clear()


if __name__ == "__main__":
    unittest.main()
