import unittest
from unittest.mock import patch

from ai_os_browser_worker.dream_triage import (
    deterministic_triage,
    normalize_triage_result,
    triage_prompt,
)
from ai_os_browser_worker.relay import DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS
from browser_worker_launcher import GEMINI

from dream_triage_runner import GeminiEvaluatorSession, _CurrentPageRelay, run_triage

FP = "sha256:" + "a" * 64


def capsule(**changes):
    value = {
        "schema": "aios-dream-triage-capsule:v1",
        "task": "#46",
        "source_fingerprint": FP,
        "final_state": "completed",
        "objective": "Preserve verified recovery lessons.",
        "result_summary": "Page-level recovery preserved the session.",
        "corrections": ["Earlier restart guidance was superseded."],
        "artifacts": ["artifact-1"],
        "verification": ["tests passed"],
    }
    value.update(changes)
    return value


def model_result(fingerprint=FP):
    return {
        "kind": "finish",
        "schema": "aios-dream-triage-result:v1",
        "source_fingerprint": fingerprint,
        "triage_version": 1,
        "dimensions": {
            "operational_impact": 0.9,
            "reuse_scope": 0.8,
            "novelty": 0.6,
            "recurrence": 0.7,
            "evidence_strength": 1.0,
        },
        "reasons": ["verified"],
    }


class DreamTriageTests(unittest.TestCase):
    def test_unsettled_and_unsafe_do_not_need_model(self):
        self.assertEqual(
            deterministic_triage(
                capsule(final_state="open", result_summary="", verification=[])
            )["decision"],
            "defer",
        )
        self.assertEqual(
            deterministic_triage(
                capsule(final_state="history_unsafe", result_summary="", verification=[])
            )["decision"],
            "skip",
        )

    def test_verified_completed_task_requires_model(self):
        self.assertIsNone(deterministic_triage(capsule()))
        self.assertIn("superseded", triage_prompt(capsule()))

    def test_model_score_and_decision_are_recomputed(self):
        raw = model_result()
        raw["salience"] = 0.01
        raw["decision"] = "skip"
        result = normalize_triage_result(capsule(), raw)
        self.assertEqual(result["salience"], 0.815)
        self.assertEqual(result["decision"], "deep")

    def test_bad_fingerprint_and_score_fail_closed(self):
        raw = model_result("sha256:" + "b" * 64)
        with self.assertRaises(ValueError):
            normalize_triage_result(capsule(), raw)
        raw["source_fingerprint"] = FP
        raw["dimensions"]["novelty"] = 1.1
        with self.assertRaises(ValueError):
            normalize_triage_result(capsule(), raw)

    def test_runner_reuses_existing_gemini_page(self):
        class Relay:
            def __init__(self):
                self.calls = []

            def command(
                self,
                action,
                args=None,
                timeout=DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
            ):
                self.calls.append((action, args or {}, timeout))
                return {"url": GEMINI}

        relay = Relay()
        with patch("dream_triage_runner.ask_gemini", return_value=model_result()) as ask:
            result = run_triage(relay, capsule())
        self.assertEqual(
            relay.calls,
            [("start", {}, DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS)],
        )
        wrapped = ask.call_args.args[0]
        self.assertIsInstance(wrapped, _CurrentPageRelay)
        self.assertEqual(wrapped.command("switchPage", {"index": 0}), {"pageIndex": 0})
        self.assertTrue(wrapped.command("goto", {"url": GEMINI})["reusedCurrentPage"])
        self.assertEqual(len(relay.calls), 1)
        self.assertEqual(result["decision"], "deep")

    def test_session_does_not_start_for_deterministic_defer(self):
        class Relay:
            def command(self, action, args):
                raise AssertionError("Gemini/browser should not start")

        session = GeminiEvaluatorSession(Relay())
        with patch("dream_triage_runner.ask_gemini") as ask:
            result = session.triage(
                capsule(final_state="claimed", result_summary="", verification=[])
            )
        ask.assert_not_called()
        self.assertFalse(session.started)
        self.assertEqual(result["decision"], "defer")

    def test_session_reuses_one_start_across_multiple_triage_calls(self):
        class Relay:
            def __init__(self):
                self.calls = []

            def command(
                self,
                action,
                args=None,
                timeout=DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
            ):
                self.calls.append((action, args or {}, timeout))
                return {"url": GEMINI}

        fp2 = "sha256:" + "b" * 64
        relay = Relay()
        session = GeminiEvaluatorSession(relay)
        with patch(
            "dream_triage_runner.ask_gemini",
            side_effect=[model_result(FP), model_result(fp2)],
        ) as ask:
            one = session.triage(capsule())
            two = session.triage(capsule(task="#47", source_fingerprint=fp2))

        self.assertTrue(session.started)
        self.assertEqual(
            relay.calls,
            [("start", {}, DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS)],
        )
        self.assertEqual(ask.call_count, 2)
        self.assertIs(ask.call_args_list[0].args[0], ask.call_args_list[1].args[0])
        self.assertEqual(one["decision"], "deep")
        self.assertEqual(two["decision"], "deep")

    def test_session_reuses_same_page_for_deep_after_triage(self):
        class Relay:
            def __init__(self):
                self.calls = []

            def command(
                self,
                action,
                args=None,
                timeout=DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
            ):
                self.calls.append((action, args or {}, timeout))
                return {"url": GEMINI}

        relay = Relay()
        session = GeminiEvaluatorSession(relay)
        with patch(
            "dream_triage_runner.ask_gemini",
            side_effect=[model_result(), {"kind": "finish", "schema": "deep-test"}],
        ) as ask:
            session.triage(capsule())
            deep = session.deep("deep prompt")

        self.assertEqual(
            relay.calls,
            [("start", {}, DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS)],
        )
        self.assertEqual(ask.call_count, 2)
        self.assertIs(ask.call_args_list[0].args[0], ask.call_args_list[1].args[0])
        self.assertEqual(deep["schema"], "deep-test")

    def test_current_page_relay_allows_first_gemini_navigation_from_blank(self):
        class Relay:
            def __init__(self):
                self.calls = []

            def command(
                self,
                action,
                args=None,
                timeout=DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
            ):
                self.calls.append((action, args or {}, timeout))
                return {"url": str((args or {}).get("url") or "about:blank")}

        relay = Relay()
        current = _CurrentPageRelay(relay, current_url="about:blank")
        current.command("goto", {"url": GEMINI}, timeout=12)
        self.assertEqual(relay.calls, [("goto", {"url": GEMINI}, 12)])
        self.assertTrue(current.command("goto", {"url": GEMINI})["reusedCurrentPage"])
        self.assertEqual(len(relay.calls), 1)

    def test_runner_defers_without_gemini(self):
        class Relay:
            def command(self, action, args):
                raise AssertionError("Gemini should not be called")

        with patch("dream_triage_runner.ask_gemini") as ask:
            result = run_triage(
                Relay(),
                capsule(final_state="claimed", result_summary="", verification=[]),
            )
        ask.assert_not_called()
        self.assertEqual(result["decision"], "defer")


if __name__ == "__main__":
    unittest.main()
