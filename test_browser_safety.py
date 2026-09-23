import copy
import json
import unittest
from unittest.mock import patch

from ai_os_browser_worker.relay import Relay, RelayCommandError
from ai_os_browser_worker.safety import (
    LoopGuard,
    browser_state_fingerprint,
    loop_guard_args,
    make_tool_receipt,
    validate_tool_receipt,
)
from browser_worker_launcher import run_worker


REPO_URL = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker"
ISSUE_URL = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/7"


def semantic_page(generation: int) -> dict:
    return {
        "url": REPO_URL + "/tree/main",
        "generation": generation,
        "pageText": "stable task page",
        "elements": [
            {
                "id": f"g{generation}-e1",
                "role": "link",
                "label": "WORKER.md",
                "text": "WORKER.md",
                "attributes": {
                    "href": "/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
                },
            }
        ],
    }


class LoopGuardTests(unittest.TestCase):
    def test_semantic_click_survives_generation_changes_and_caps_on_fifth(self):
        guard = LoopGuard()
        decisions = []
        normalized = []

        for generation in range(1, 6):
            page = semantic_page(generation)
            args = loop_guard_args(
                "click",
                {"elementId": f"g{generation}-e1"},
                page,
            )
            normalized.append(args)
            decisions.append(
                guard.observe_tool_call(
                    "run-1",
                    "click",
                    args,
                    state_fingerprint=browser_state_fingerprint(page),
                )
            )

        self.assertTrue(all(item == normalized[0] for item in normalized))
        self.assertEqual(decisions[0].action, "continue")
        self.assertEqual(decisions[1].action, "continue")
        self.assertEqual(decisions[2].action, "warn")
        self.assertEqual(decisions[2].reason_code, "identical_call_warning")
        self.assertEqual(decisions[3].action, "continue")
        self.assertTrue(decisions[4].stop)
        self.assertEqual(decisions[4].reason_code, "identical_call_hard_limit")
        self.assertEqual(decisions[4].count, 5)

    def test_browser_state_fingerprint_excludes_generation(self):
        first = semantic_page(1)
        second = semantic_page(99)
        second["elements"][0]["id"] = "g99-e42"
        self.assertEqual(
            browser_state_fingerprint(first),
            browser_state_fingerprint(second),
        )

    def test_browser_state_fingerprint_changes_with_semantics(self):
        base = semantic_page(1)
        baseline = browser_state_fingerprint(base)

        changed_url = copy.deepcopy(base)
        changed_url["url"] = REPO_URL + "/blob/main/README.md"
        self.assertNotEqual(baseline, browser_state_fingerprint(changed_url))

        changed_text = copy.deepcopy(base)
        changed_text["pageText"] = "different task page"
        self.assertNotEqual(baseline, browser_state_fingerprint(changed_text))

        changed_element = copy.deepcopy(base)
        changed_element["elements"][0]["label"] = "README.md"
        self.assertNotEqual(baseline, browser_state_fingerprint(changed_element))


class ToolReceiptTests(unittest.TestCase):
    def test_receipt_is_execution_evidence_without_raw_payload(self):
        receipt = make_tool_receipt(
            run_id="run-1",
            step=1,
            action_id="action-1",
            tool="goto",
            status="success",
            args={"url": "https://example.test/raw-secret"},
            output={"pageText": "raw-result-secret"},
            created_at="2026-09-20T00:00:00Z",
        )

        validate_tool_receipt(receipt)
        self.assertIs(receipt["authoritative"], False)
        self.assertIs(receipt["acceptance"], False)
        self.assertNotIn("args", receipt)
        self.assertNotIn("output", receipt)
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("raw-secret", serialized)
        self.assertNotIn("raw-result-secret", serialized)

    def test_relay_success_creates_runtime_owned_receipt(self):
        relay = Relay("https://supabase.example.test", "key", "session-1")
        with patch.object(
            relay,
            "rest",
            side_effect=[
                [],
                [{"status": "done", "result": {"ok": True}}],
            ],
        ):
            result, receipt = relay.command_with_receipt(
                "getPage",
                {},
                run_id="run-1",
                step=1,
                timeout=1,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(receipt["tool"], "getPage")
        self.assertEqual(receipt["status"], "success")
        self.assertIs(receipt["authoritative"], False)
        self.assertIs(receipt["acceptance"], False)

    def test_relay_error_attaches_error_receipt(self):
        relay = Relay("https://supabase.example.test", "key", "session-1")
        with patch.object(
            relay,
            "rest",
            side_effect=[
                [],
                [{"status": "error", "error": "boom"}],
            ],
        ):
            with self.assertRaises(RelayCommandError) as raised:
                relay.command_with_receipt(
                    "click",
                    {"elementId": "g1-e1"},
                    run_id="run-1",
                    step=2,
                    timeout=1,
                )

        receipt = raised.exception.receipt
        self.assertEqual(receipt["tool"], "click")
        self.assertEqual(receipt["status"], "error")
        self.assertIs(receipt["authoritative"], False)
        self.assertIs(receipt["acceptance"], False)
        self.assertNotIn("args", receipt)
        self.assertNotIn("output", receipt)


class FakeRelay:
    def __init__(self):
        self.session = "launcher-test-session"
        self.commands = []
        self.receipted_actions = []
        self.generation = 0

    def ready(self):
        return None

    def _page(self):
        self.generation += 1
        return {
            "url": REPO_URL,
            "title": "repo",
            "generation": self.generation,
            "pageText": "stable page",
            "elements": [],
        }

    def command(self, action, args):
        self.commands.append((action, dict(args)))
        if action == "newPage":
            return {"pageIndex": 1}
        if action == "getPage":
            return self._page()
        return {}

    def command_with_receipt(self, action, args, *, run_id, step, timeout=75):
        self.receipted_actions.append((action, dict(args)))
        result = {"ok": True}
        receipt = make_tool_receipt(
            run_id=run_id,
            step=step,
            action_id=f"action-{len(self.receipted_actions)}",
            tool=action,
            status="success",
            args=args,
            output=result,
            created_at=f"2026-09-20T00:00:{step:02d}Z",
        )
        return result, receipt


def dispatch() -> dict:
    return {
        "schema": "ai-os-dispatch:v1",
        "authoritative": False,
        "task": "#7",
        "title": "test",
        "process": "PROC-RUNTIME-BROWSER-WORKER",
        "source": {
            "repository": "GK-studio-JP/ai-bulletin-board",
            "issue_url": ISSUE_URL,
        },
    }


def browser_action() -> dict:
    return {
        "kind": "browser_action",
        "action": "getPage",
        "args": {},
        "reason": "verify",
    }


class LauncherLoopCapTests(unittest.TestCase):
    def run_sequence(self, sequence):
        relay = FakeRelay()
        with (
            patch("browser_worker_launcher.github", return_value={"body": ""}),
            patch("browser_worker_launcher.comments", return_value=[]),
            patch("browser_worker_launcher.canonical_task_completed", return_value=False),
            patch("browser_worker_launcher.canonical_owner_actor", return_value="repo-owner"),
            patch("browser_worker_launcher.append_issue_comment"),
            patch("browser_worker_launcher.wait_for_protocol_event", return_value=True),
            patch("browser_worker_launcher.ensure_canonical_lease"),
            patch(
                "browser_worker_launcher._validate_model_action",
                return_value=("getPage", {}),
            ),
            patch("browser_worker_launcher.ask_gemini", side_effect=sequence),
            patch("browser_worker_launcher.validate_finish_evidence", return_value=None),
            patch("browser_worker_launcher.time.sleep"),
        ):
            code = run_worker(
                dispatch(),
                token=None,
                relay=relay,
                max_steps=len(sequence),
            )
        return code, relay

    def test_fifth_identical_action_executes_and_sixth_is_blocked(self):
        sequence = [browser_action() for _ in range(6)]
        code, relay = self.run_sequence(sequence)

        self.assertEqual(code, 2)
        self.assertEqual(len(relay.receipted_actions), 5)
        self.assertEqual(
            [action for action, _ in relay.receipted_actions],
            ["getPage"] * 5,
        )
        self.assertGreater(
            sum(1 for action, _ in relay.commands if action == "getPage"),
            len(relay.receipted_actions),
        )

    def test_finish_remains_allowed_after_loop_cap(self):
        sequence = [browser_action() for _ in range(5)] + [
            {
                "kind": "finish",
                "summary": "acceptance already verified",
                "artifacts": ["immutable-artifact"],
                "evidence": [],
                "reason": "done",
            }
        ]
        code, relay = self.run_sequence(sequence)

        self.assertEqual(code, 0)
        self.assertEqual(len(relay.receipted_actions), 5)

    def test_wait_remains_allowed_after_loop_cap(self):
        sequence = [browser_action() for _ in range(5)] + [
            {"kind": "wait", "reason": "need handoff"}
        ]
        code, relay = self.run_sequence(sequence)

        self.assertEqual(code, 2)
        self.assertEqual(len(relay.receipted_actions), 5)


if __name__ == "__main__":
    unittest.main()
