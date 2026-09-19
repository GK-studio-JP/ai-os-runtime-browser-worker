import unittest

from browser_worker_launcher import (
    LauncherError,
    _protocol_payload,
    _validate_model_action,
    canonical_result_present,
    canonical_task_completed,
    extract_model_command,
    issue_number_from_dispatch,
    reduce_observation,
    validate_plan,
)


def dispatch(task="#7"):
    return {
        "schema": "ai-os-dispatch:v1",
        "authoritative": False,
        "task": task,
        "title": "test",
        "process": "PROC-RUNTIME-BROWSER-WORKER",
        "source": {
            "repository": "GK-studio-JP/ai-bulletin-board",
            "issue_url": f"https://github.com/GK-studio-JP/ai-bulletin-board/issues/{task[1:]}",
        },
    }


def plan(rows):
    return {
        "schema": "ai-os-dispatch-plan:v1",
        "authoritative": False,
        "filters": {"process": "PROC-RUNTIME-BROWSER-WORKER"},
        "dispatch_count": len(rows),
        "dispatches": rows,
    }


class LauncherTests(unittest.TestCase):
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
        bad = plan([dispatch()])
        bad["filters"]["process"] = "PROC-OTHER"
        with self.assertRaises(LauncherError):
            validate_plan(bad)

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
            _validate_model_action({"action": "switchPage", "args": {"index": 1}}, {"generation": 1})

    def test_protocol_payload_parses_fenced_json(self):
        body = '''<!-- ai-bb:v1 -->
```json
{"type":"CLAIM","agent_id":"a","task":"#1"}
```'''
        self.assertEqual(_protocol_payload(body)["type"], "CLAIM")

    def test_canonical_task_completed_accepts_any_result_for_task(self):
        comments = [{"body": '<!-- ai-bb:v1 -->\n```json\n{"type":"RESULT","agent_id":"other","task":"#9"}\n```'}]
        self.assertTrue(canonical_task_completed(comments, task="#9"))
        self.assertFalse(canonical_task_completed(comments, task="#8"))

    def test_canonical_result_requires_claim_then_result_same_agent(self):
        comments = [
            {"body": '<!-- ai-bb:v1 -->\n```json\n{"type":"RESULT","agent_id":"a","task":"#1"}\n```'},
            {"body": '<!-- ai-bb:v1 -->\n```json\n{"type":"CLAIM","agent_id":"a","task":"#1"}\n```'},
        ]
        self.assertFalse(canonical_result_present(comments, task="#1", agent_id="a"))
        comments.append({"body": '<!-- ai-bb:v1 -->\n```json\n{"type":"RESULT","agent_id":"a","task":"#1"}\n```'})
        self.assertTrue(canonical_result_present(comments, task="#1", agent_id="a"))

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
