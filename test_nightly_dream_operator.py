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
    @staticmethod
    def _work_page(generation=4, url=operator.START_URL, **extra):
        return {
            "url": url,
            "title": "Dream Control",
            "generation": generation,
            "pageText": "Dream Control",
            "elements": [],
            **extra,
        }

    def test_gemini_drives_nonzero_active_work_page_then_finish(self):
        calls = []
        work_page_index = 1
        gemini_page_index = 2

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
                    if args["url"] == operator.START_URL:
                        return NightlyDreamOperatorLoopTests._work_page(generation=3)
                    return {
                        **NightlyDreamOperatorLoopTests._work_page(generation=7),
                        "url": args["url"],
                    }
                if action == "listPages":
                    return {
                        "pages": [
                            {
                                "index": 0,
                                "url": "https://github.com/GK-studio-JP/ai-bulletin-board/issues/53",
                                "active": False,
                            },
                            {
                                "index": work_page_index,
                                "url": operator.START_URL,
                                "active": True,
                            },
                        ]
                    }
                if action == "newPage":
                    return {"pageIndex": gemini_page_index, "url": args["url"]}
                if action == "switchPage":
                    if args.get("index") != work_page_index:
                        raise AssertionError(f"wrong work page index: {args}")
                    if (
                        args.get("observationTimeoutMs")
                        != operator.WORK_PAGE_OBSERVATION_TIMEOUT_MS
                    ):
                        raise AssertionError(f"missing long observation timeout: {args}")
                    return {
                        "pageIndex": work_page_index,
                        "page": NightlyDreamOperatorLoopTests._work_page(generation=4),
                    }
                if action == "getPage":
                    return NightlyDreamOperatorLoopTests._work_page(generation=5)
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
        self.assertIn(("listPages", {}, None), calls)
        self.assertTrue(
            any(
                row[0] == "switchPage"
                and row[1].get("index") == work_page_index
                and row[1].get("observationTimeoutMs")
                == operator.WORK_PAGE_OBSERVATION_TIMEOUT_MS
                for row in calls
                if len(row) >= 2 and isinstance(row[1], dict)
            )
        )
        self.assertFalse(
            any(
                row[0] == "switchPage" and row[1].get("index") == 0
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

    def test_deferred_switch_without_cache_uses_long_get_page(self):
        get_page_args = []

        class FakeRelay:
            def __init__(self, base, key, session_id):
                self.session = session_id

            def ready(self):
                return {"ready": True}

            def command(self, action, args, timeout=None):
                if action == "start":
                    return {"url": "about:blank"}
                if action == "goto":
                    return {"url": args["url"]}
                if action == "listPages":
                    return {
                        "pages": [
                            {"index": 3, "url": operator.START_URL, "active": True},
                        ]
                    }
                if action == "newPage":
                    return {"pageIndex": 4, "url": args["url"]}
                if action == "switchPage":
                    return {
                        "pageIndex": args["index"],
                        "page": NightlyDreamOperatorLoopTests._work_page(
                            generation=5,
                            observationStatus="deferred",
                        ),
                    }
                if action == "getPage":
                    get_page_args.append(dict(args))
                    return NightlyDreamOperatorLoopTests._work_page(generation=6)
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            prompt_file.write_text("Perform the Nightly Dream test.", encoding="utf-8")
            args = Namespace(
                prompt_file=str(prompt_file),
                session_id="gcp-browser-1",
                max_steps=1,
            )
            with patch.dict(
                os.environ,
                {
                    "SUPABASE_URL": "https://relay.example",
                    "SUPABASE_SECRET_KEY": "secret",
                },
                clear=False,
            ), patch.object(operator, "Relay", FakeRelay), patch.object(
                operator,
                "ask_gemini",
                return_value={
                    "kind": "finish",
                    "summary": "Recovered with one bounded long observation.",
                    "artifacts": [],
                    "evidence": [],
                    "reason": "done",
                },
            ):
                result = operator.run_operator(args)

        self.assertEqual(result["status"], "finished")
        self.assertTrue(get_page_args)
        self.assertTrue(
            all(
                row.get("observationTimeoutMs")
                == operator.WORK_PAGE_OBSERVATION_TIMEOUT_MS
                for row in get_page_args
            )
        )

    def test_reuses_action_observation_when_switch_observation_is_deferred(self):
        runbook_url = (
            "https://github.com/GK-studio-JP/ai-os-projects/blob/main/"
            "projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md"
        )
        get_page_calls = 0
        goto_count = 0
        switch_count = 0

        class FakeRelay:
            def __init__(self, base, key, session_id):
                self.session = session_id

            def ready(self):
                return {"ready": True}

            def command(self, action, args, timeout=None):
                nonlocal get_page_calls, goto_count, switch_count
                if action == "start":
                    return {"url": "about:blank"}
                if action == "goto":
                    goto_count += 1
                    if goto_count == 1:
                        return NightlyDreamOperatorLoopTests._work_page(generation=1)
                    return NightlyDreamOperatorLoopTests._work_page(
                        generation=9,
                        url=args["url"],
                    )
                if action == "listPages":
                    return {
                        "pages": [
                            {"index": 5, "url": operator.START_URL, "active": True},
                        ]
                    }
                if action == "newPage":
                    return {"pageIndex": 6, "url": args["url"]}
                if action == "switchPage":
                    switch_count += 1
                    if switch_count <= 2:
                        return {
                            "pageIndex": 5,
                            "page": NightlyDreamOperatorLoopTests._work_page(
                                generation=10 + switch_count,
                                url=operator.START_URL,
                            ),
                        }
                    return {
                        "pageIndex": 5,
                        "page": NightlyDreamOperatorLoopTests._work_page(
                            generation=13,
                            url=runbook_url,
                            observationStatus="deferred",
                        ),
                    }
                if action == "getPage":
                    get_page_calls += 1
                    raise AssertionError("cached action observation should avoid getPage")
                return {"ok": True}

        commands = iter(
            [
                {
                    "kind": "browser_action",
                    "action": "goto",
                    "args": {"url": runbook_url},
                    "reason": "read contract",
                },
                {
                    "kind": "finish",
                    "summary": "Reused the action observation.",
                    "artifacts": [],
                    "evidence": [],
                    "reason": "done",
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            prompt_file.write_text("Perform the bounded operator test.", encoding="utf-8")
            args = Namespace(
                prompt_file=str(prompt_file),
                session_id="gcp-browser-1",
                max_steps=2,
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
        self.assertEqual(get_page_calls, 0)

    def test_requires_exactly_one_active_work_page(self):
        with self.assertRaisesRegex(LauncherError, "exactly one active work page"):
            operator._active_work_page_index(
                {
                    "pages": [
                        {"index": 0, "url": operator.START_URL, "active": True},
                        {"index": 1, "url": operator.START_URL, "active": True},
                    ]
                }
            )



if __name__ == "__main__":
    unittest.main()
