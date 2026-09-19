import json
import tempfile
import unittest
from pathlib import Path

from browser_worker_launcher import (
    LauncherError,
    _protocol_payload,
    _validate_model_action,
    action_allowed_before_claim,
    canonical_claim_present,
    canonical_result_present,
    canonical_task_completed,
    extract_model_command,
    issue_number_from_dispatch,
    plan_from_file,
    reduce_observation,
    resolve_plan,
    validate_plan,
)


def dispatch(task="#7", process="PROC-RUNTIME-BROWSER-WORKER"):
    return {
        "schema": "ai-os-dispatch:v1",
        "authoritative": False,
        "task": task,
        "title": "secret task title should not be copied",
        "process": process,
        "source": {
            "repository": "GK-studio-JP/ai-bulletin-board",
            "issue_url": f"https://github.com/GK-studio-JP/ai-bulletin-board/issues/{task[1:]}",
        },
    }


def plan(rows, process="PROC-RUNTIME-BROWSER-WORKER"):
    return {
        "schema": "ai-os-dispatch-plan:v1",
        "authoritative": False,
        "filters": {"process": process},
        "dispatch_count": len(rows),
        "dispatches": rows,
    }


class PlanTests(unittest.TestCase):
    def test_validate_plan_accepts_single_dispatch(self):
        item = validate_plan(plan([dispatch("#12")]))
        self.assertEqual(item["task"], "#12")
        self.assertEqual(issue_number_from_dispatch(item), 12)

    def test_validate_plan_accepts_empty_plan(self):
        self.assertIsNone(validate_plan(plan([])))

    def test_validate_plan_rejects_multiple_dispatches(self):
        with self.assertRaises(LauncherError):
            validate_plan(plan([dispatch("#1"), dispatch("#2")]))

    def test_validate_plan_rejects_wrong_process(self):
        with self.assertRaises(LauncherError):
            validate_plan(plan([dispatch()], process="PROC-OTHER"))

    def test_validate_plan_rejects_noncanonical_issue_url(self):
        value = dispatch("#7")
        value["source"]["issue_url"] = "https://github.com/other/repo/issues/7"
        with self.assertRaises(LauncherError):
            validate_plan(plan([value]))

    def test_plan_file_and_resolve_plan(self):
        value = plan([dispatch("#9")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(plan_from_file(str(path))["dispatch_count"], 1)
            resolved = resolve_plan(token=None, plan_file=str(path))
            self.assertEqual(resolved["dispatches"][0]["task"], "#9")

    def test_resolve_plan_rejects_multiple_sources(self):
        with self.assertRaises(LauncherError):
            resolve_plan(token=None, scheduler_run_id=1, plan_file="x.json")


class ModelCommandTests(unittest.TestCase):
    def test_extract_model_command_uses_last_matching_json(self):
        text = (
            'Prompt example {"kind":"wait","reason":"example"}\n'
            'Gemini said {"kind":"browser_action","action":"goto","args":{"url":"https://example.test"},"reason":"open"}'
        )
        command = extract_model_command(text)
        self.assertEqual(command["kind"], "browser_action")
        self.assertEqual(command["action"], "goto")

    def test_element_action_requires_current_generation(self):
        observation = {"generation": 4}
        command = {"action": "click", "args": {"elementId": "g4-e8"}}
        self.assertEqual(_validate_model_action(command, observation)[0], "click")
        command["args"]["elementId"] = "g3-e8"
        with self.assertRaises(LauncherError):
            _validate_model_action(command, observation)

    def test_disallows_control_plane_tab_actions(self):
        with self.assertRaises(LauncherError):
            _validate_model_action(
                {"action": "switchPage", "args": {"index": 1}},
                {"generation": 1},
            )


    def test_normalizes_fill_and_click_alias_args(self):
        observation = {"generation": 4}

        action, args = _validate_model_action(
            {"action": "fill", "args": {"id": "g4-e8", "value": "hello"}},
            observation,
        )
        self.assertEqual(action, "fill")
        self.assertEqual(args, {"elementId": "g4-e8", "text": "hello"})

        action, args = _validate_model_action(
            {"action": "click", "args": {"id": "g4-e9"}},
            observation,
        )
        self.assertEqual(action, "click")
        self.assertEqual(args, {"elementId": "g4-e9"})


class ProtocolTests(unittest.TestCase):
    def test_protocol_payload_parses_fenced_json(self):
        body = """<!-- ai-bb:v1 -->
```json
{"type":"CLAIM","agent_id":"a","task":"#1"}
```"""
        self.assertEqual(_protocol_payload(body)["type"], "CLAIM")

    def test_claim_and_result_helpers(self):
        comments = [
            {
                "body": '<!-- ai-bb:v1 -->\n```json\n{"type":"CLAIM","agent_id":"a","task":"#1"}\n```'
            }
        ]
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a"))
        self.assertFalse(canonical_result_present(comments, task="#1", agent_id="a"))
        comments.append(
            {
                "body": '<!-- ai-bb:v1 -->\n```json\n{"type":"RESULT","agent_id":"a","task":"#1"}\n```'
            }
        )
        self.assertTrue(canonical_result_present(comments, task="#1", agent_id="a"))
        self.assertTrue(canonical_task_completed(comments, task="#1"))

    def test_preclaim_gate_allows_only_comment_mutation(self):
        issue = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/7"
        observation = {
            "url": issue,
            "generation": 4,
            "elements": [
                {
                    "id": "g4-e1",
                    "role": "textbox",
                    "label": "Use Markdown to format your comment",
                },
                {"id": "g4-e2", "role": "button", "text": "Comment"},
                {"id": "g4-e3", "role": "button", "text": "Close issue"},
            ],
        }
        self.assertTrue(
            action_allowed_before_claim(
                {"action": "fill", "args": {"elementId": "g4-e1"}},
                observation,
                issue,
            )
        )
        self.assertTrue(
            action_allowed_before_claim(
                {"action": "click", "args": {"elementId": "g4-e2"}},
                observation,
                issue,
            )
        )
        self.assertFalse(
            action_allowed_before_claim(
                {"action": "click", "args": {"elementId": "g4-e3"}},
                observation,
                issue,
            )
        )
        self.assertTrue(
            action_allowed_before_claim(
                {"action": "goto", "args": {"url": "https://example.test"}},
                observation,
                issue,
            )
        )


class ObservationTests(unittest.TestCase):
    def test_reduce_observation_bounds_text_and_elements(self):
        page = {
            "url": "https://example.test",
            "generation": 8,
            "pageText": "x" * 100,
            "elements": [
                {"id": f"g8-e{i}", "role": "button", "text": str(i), "extra": "ignore"}
                for i in range(5)
            ],
        }
        reduced = reduce_observation(page, max_text=10, max_elements=2)
        self.assertEqual(reduced["pageText"], "x" * 10)
        self.assertEqual(len(reduced["elements"]), 2)
        self.assertNotIn("extra", reduced["elements"][0])


if __name__ == "__main__":
    unittest.main()
