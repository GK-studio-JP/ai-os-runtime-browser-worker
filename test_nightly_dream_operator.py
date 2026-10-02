import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import nightly_dream_operator as operator
from ai_os_browser_worker.navigation_policy import LauncherError


class NightlyDreamOperatorPolicyTests(unittest.TestCase):
    def test_allows_navigation_inside_nightly_dream_repositories(self):
        page = {
            "url": operator.START_URL,
            "generation": 1,
            "elements": [],
        }
        action, args = operator.validate_operator_action(
            {
                "kind": "browser_action",
                "action": "goto",
                "args": {
                    "url": "https://github.com/GK-studio-JP/ai-os-projects/blob/main/projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md"
                },
            },
            page,
        )
        self.assertEqual(action, "goto")
        self.assertIn("ai-os-projects", args["url"])

    def test_denies_navigation_outside_nightly_dream_repositories(self):
        with self.assertRaises(LauncherError):
            operator.validate_operator_action(
                {
                    "kind": "browser_action",
                    "action": "goto",
                    "args": {"url": "https://github.com/openai/openai-python"},
                },
                {
                    "url": operator.START_URL,
                    "generation": 1,
                    "elements": [],
                },
            )

    def test_denies_direct_commit_radio(self):
        page = {
            "url": "https://github.com/GK-studio-JP/ai-os-memory/edit/main/README.md",
            "generation": 7,
            "elements": [
                {
                    "id": "g7-direct",
                    "role": "radio",
                    "label": "Commit directly to the main branch",
                }
            ],
        }
        with self.assertRaisesRegex(LauncherError, "direct commit"):
            operator.validate_operator_action(
                {
                    "kind": "browser_action",
                    "action": "click",
                    "args": {"elementId": "g7-direct"},
                },
                page,
            )

    def test_allows_issue_comment_fill(self):
        page = {
            "url": operator.START_URL,
            "generation": 3,
            "elements": [
                {
                    "id": "g3-comment",
                    "role": "textbox",
                    "label": "Add a comment",
                }
            ],
        }
        action, args = operator.validate_operator_action(
            {
                "kind": "browser_action",
                "action": "fill",
                "args": {"elementId": "g3-comment", "text": "hello"},
            },
            page,
        )
        self.assertEqual(action, "fill")
        self.assertEqual(args["text"], "hello")


class NightlyDreamOperatorLoopTests(unittest.TestCase):
    def test_gemini_drives_browser_action_then_finish(self):
        calls = []

        class FakeRelay:
            def __init__(self, base, key, session_id):
                calls.append(("init", base, key, session_id))
                self.session = session_id

            def ready(self):
                calls.append(("ready",))
                return {"ready": True}

            def command(self, action, args, timeout=None):
                calls.append((action, dict(args), timeout))
                if action == "start":
                    return {"url": "about:blank"}
                if action == "goto":
                    return {"url": args["url"]}
                if action == "newPage":
                    return {"pageIndex": 1, "url": args["url"]}
                if action == "getPage":
                    return {
                        "url": operator.START_URL,
                        "title": "Dream Control",
                        "generation": 4,
                        "pageText": "Dream Control",
                        "elements": [],
                    }
                return {"ok": True}

        commands = iter(
            [
                {
                    "kind": "browser_action",
                    "action": "goto",
                    "args": {
                        "url": "https://github.com/GK-studio-JP/ai-os-projects/blob/main/projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md"
                    },
                    "reason": "read contract",
                },
                {
                    "kind": "finish",
                    "summary": "Gemini completed the bounded operator test.",
                    "artifacts": [],
                    "evidence": [],
                    "reason": "done",
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            prompt_file.write_text("Perform the Nightly Dream test.", encoding="utf-8")
            args = Namespace(
                prompt_file=str(prompt_file),
                session_id="gcp-browser-1",
                max_steps=4,
            )
            with patch.dict(
                os.environ,
                {
                    "SUPABASE_URL": "https://relay.example",
                    "SUPABASE_SECRET_KEY": "secret",
                },
                clear=False,
            ), patch.object(operator, "Relay", FakeRelay), patch.object(
                operator, "ask_gemini", side_effect=lambda *a, **k: next(commands)
            ):
                result = operator.run_operator(args)

        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["step"], 2)
        self.assertTrue(
            any(
                row[0] == "getPage"
                and row[1].get("observationTimeoutMs")
                == operator.WORK_PAGE_OBSERVATION_TIMEOUT_MS
                for row in calls
                if len(row) >= 2 and isinstance(row[1], dict)
            )
        )
        self.assertTrue(
            any(
                row[0] == "goto"
                and row[1].get("url", "").startswith(
                    "https://github.com/GK-studio-JP/ai-os-projects/"
                )
                for row in calls
                if len(row) >= 2 and isinstance(row[1], dict)
            )
        )
        self.assertTrue(
            any(
                row[0] == "newPage"
                and row[1].get("url") == operator.GEMINI
                and row[1].get("pageCreateTimeoutMs") == 60000
                for row in calls
                if len(row) >= 2 and isinstance(row[1], dict)
            )
        )


if __name__ == "__main__":
    unittest.main()
