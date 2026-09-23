import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_os_browser_worker.dispatch import (
    issue_number_from_dispatch,
    plan_from_file,
    resolve_plan,
    validate_plan,
)
from ai_os_browser_worker.evidence import validate_finish_evidence
from ai_os_browser_worker.navigation_policy import (
    _validate_model_action,
    refresh_element_args,
    validate_mutation_receipt,
)
from browser_worker_launcher import (
    GEMINI_MALFORMED_STABLE_POLLS,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    LauncherError,
    _has_new_gemini_response,
    _is_gemini_transient_error,
    ask_gemini,
    _task_payload_from_issue,
    canonical_claim_present,
    canonical_result_present,
    canonical_task_completed,
    ensure_canonical_lease,
    extract_model_command,
    protocol_event_body,
    prompt,
    reduce_observation,
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
    value = {
        "schema": "ai-os-dispatch-plan:v1",
        "authoritative": False,
        "filters": {"process": process},
        "dispatch_count": len(rows),
        "dispatches": rows,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["fingerprint"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return value


def mutation_receipt(
    *,
    task="#7",
    repository="GK-studio-JP/ai-os-runtime-browser-worker",
    process="PROC-RUNTIME-BROWSER-WORKER",
    plan_fingerprint="sha256:plan",
):
    value = {
        "schema": "ai-os-kernel-capability-receipt:v1",
        "authoritative": False,
        "persist_required": True,
        "approved": True,
        "caller": process,
        "operation": "mutate_repository",
        "required_capability": "repository.write.branch",
        "task": task,
        "target_repository": repository,
        "source_plan_fingerprint": plan_fingerprint,
        "constraints": {
            "mode": "branch-pr",
            "allowed_browser_actions": ["fill", "click"],
            "forbid_direct_main_commit": True,
            "require_new_branch": True,
            "require_pull_request": True,
        },
        "errors": [],
    }
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["fingerprint"] = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return value


class GeminiTransientErrorTests(unittest.TestCase):
    def test_recognizes_logged_out_generic_failure(self):
        self.assertTrue(
            _is_gemini_transient_error(
                "Gemini said Sorry, something went wrong. Please try your request again."
            )
        )

    def test_does_not_treat_normal_response_as_transient_failure(self):
        self.assertFalse(
            _is_gemini_transient_error(
                'Gemini said {"kind":"wait","reason":"still working"}'
            )
        )

    def test_response_timeout_is_bounded_and_long_enough(self):
        self.assertGreaterEqual(GEMINI_RESPONSE_TIMEOUT_SECONDS, 90)
        self.assertLessEqual(GEMINI_RESPONSE_TIMEOUT_SECONDS, 180)

    def test_detects_new_gemini_response_marker(self):
        self.assertTrue(
            _has_new_gemini_response(
                "prompt text",
                'prompt text Gemini said {"kind":"browser_action"',
            )
        )
        self.assertFalse(
            _has_new_gemini_response(
                "Gemini said old response",
                "Gemini said old response",
            )
        )

    def test_streaming_partial_response_can_complete_before_malformed_retry(self):
        class FakeRelay:
            def __init__(self):
                self.fills = []
                self.actions = []
                self.pages = [
                    {
                        "pageText": "",
                        "elements": [
                            {"id": "g1-e1", "label": "Enter a prompt for Gemini"},
                        ],
                    },
                    {
                        "pageText": "TASK",
                        "elements": [
                            {"id": "g1-e1", "label": "Enter a prompt for Gemini"},
                            {"id": "g1-e2", "label": "Send message"},
                        ],
                    },
                    {
                        "pageText": (
                            'TASK Gemini said {"kind":"browser_action",'
                            '"action":"fill","args":{"value":"still streaming"'
                        ),
                        "elements": [],
                    },
                    {
                        "pageText": (
                            'TASK Gemini said '
                            '{"kind":"wait","reason":"stream-complete"}'
                        ),
                        "elements": [],
                    },
                ]

            def command(self, action, args):
                self.actions.append((action, args))
                if action == "getPage":
                    return self.pages.pop(0)
                if action == "fill":
                    self.fills.append(args["text"])
                return {}

        relay = FakeRelay()
        with patch("browser_worker_launcher.time.sleep", return_value=None):
            result = ask_gemini(relay, 1, "TASK")

        self.assertEqual(result, {"kind": "wait", "reason": "stream-complete"})
        self.assertEqual(len(relay.fills), 1)
        self.assertEqual(
            sum(1 for action, _ in relay.actions if action == "goto"),
            1,
        )

    def test_malformed_response_retries_once_with_strict_json_instruction(self):
        class FakeRelay:
            def __init__(self):
                self.fills = []
                self.actions = []
                malformed = {
                    "pageText": (
                        'TASK Gemini said {"kind":"browser_action",'
                        '"action":"fill","args":{"value":"unterminated"'
                    ),
                    "elements": [],
                }
                self.pages = [
                    {
                        "pageText": "",
                        "elements": [
                            {"id": "g1-e1", "label": "Enter a prompt for Gemini"},
                        ],
                    },
                    {
                        "pageText": "TASK",
                        "elements": [
                            {"id": "g1-e1", "label": "Enter a prompt for Gemini"},
                            {"id": "g1-e2", "label": "Send message"},
                        ],
                    },
                ] + [
                    dict(malformed)
                    for _ in range(GEMINI_MALFORMED_STABLE_POLLS)
                ] + [
                    {
                        "pageText": "",
                        "elements": [
                            {"id": "g2-e1", "label": "Enter a prompt for Gemini"},
                        ],
                    },
                    {
                        "pageText": "TASK RETRY",
                        "elements": [
                            {"id": "g2-e1", "label": "Enter a prompt for Gemini"},
                            {"id": "g2-e2", "label": "Send message"},
                        ],
                    },
                    {
                        "pageText": (
                            'TASK RETRY Gemini said '
                            '{"kind":"wait","reason":"retry-ok"}'
                        ),
                        "elements": [],
                    },
                ]

            def command(self, action, args):
                self.actions.append((action, args))
                if action == "getPage":
                    return self.pages.pop(0)
                if action == "fill":
                    self.fills.append(args["text"])
                return {}

        relay = FakeRelay()
        original_prompt = "ORIGINAL-PROMPT-SENTINEL " * 300
        with patch("browser_worker_launcher.time.sleep", return_value=None):
            result = ask_gemini(relay, 1, original_prompt)

        self.assertEqual(result, {"kind": "wait", "reason": "retry-ok"})
        self.assertEqual(len(relay.fills), 2)
        self.assertEqual(relay.fills[0], original_prompt)
        self.assertNotIn("ORIGINAL-PROMPT-SENTINEL", relay.fills[1])
        self.assertLess(len(relay.fills[1]), 1000)
        self.assertIn("not valid parseable JSON", relay.fills[1])
        self.assertIn("Escape quotes and backslashes", relay.fills[1])
        self.assertEqual(
            sum(1 for action, _ in relay.actions if action == "goto"),
            1,
            "malformed retry must reuse the existing Gemini conversation",
        )


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

    def test_validate_plan_rejects_tampered_fingerprint(self):
        value = plan([dispatch("#7")])
        value["dispatches"][0]["task"] = "#8"
        with self.assertRaisesRegex(LauncherError, "fingerprint"):
            validate_plan(value)

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
    def setUp(self):
        self.issue = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/7"
        self.payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
        }
        self.repo_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker"

    def validate(self, command, observation, receipt=None):
        return _validate_model_action(
            command,
            observation,
            task_payload=self.payload,
            issue_url=self.issue,
            mutation_receipt=receipt,
        )

    def test_extract_model_command_uses_last_matching_json(self):
        text = (
            'Prompt example {"kind":"wait","reason":"example"}\n'
            'Gemini said {"kind":"browser_action","action":"goto","args":{"url":"https://example.test"},"reason":"open"}'
        )
        command = extract_model_command(text)
        self.assertEqual(command["kind"], "browser_action")
        self.assertEqual(command["action"], "goto")

    def test_navigation_link_requires_current_generation(self):
        observation = {
            "generation": 4,
            "url": self.repo_url,
            "elements": [
                {
                    "id": "g4-e8",
                    "role": "link",
                    "label": "commit",
                    "href": "/GK-studio-JP/ai-os-runtime-browser-worker/commit/" + "a" * 40,
                }
            ],
        }
        command = {"action": "click", "args": {"elementId": "g4-e8"}}
        self.assertEqual(self.validate(command, observation)[0], "click")
        command["args"]["elementId"] = "g3-e8"
        with self.assertRaises(LauncherError):
            self.validate(command, observation)

    def test_disallows_control_plane_tab_actions(self):
        with self.assertRaises(LauncherError):
            self.validate(
                {"action": "switchPage", "args": {"index": 1}},
                {"generation": 1, "url": self.repo_url, "elements": []},
            )

    def test_disallows_model_mutation_actions(self):
        observation = {
            "generation": 4,
            "url": self.repo_url,
            "elements": [{"id": "g4-e8", "role": "textbox", "label": "Edit"}],
        }
        commands = [
            {"action": "fill", "args": {"elementId": "g4-e8", "text": "x"}},
            {"action": "press", "args": {"key": "Enter"}},
            {"action": "typeText", "args": {"text": "x"}},
            {"action": "clickText", "args": {"text": "Delete"}},
        ]
        for command in commands:
            with self.subTest(action=command["action"]):
                with self.assertRaisesRegex(LauncherError, "disallowed model action"):
                    self.validate(command, observation)

    def test_goto_allows_https_task_repository_and_canonical_issue(self):
        observation = {"generation": 4, "url": self.repo_url, "elements": []}
        task_api = (
            "https://api.github.com/repos/GK-studio-JP/"
            "ai-os-runtime-browser-worker/commits/main"
        )
        action, args = self.validate(
            {"action": "goto", "args": {"url": task_api}},
            observation,
        )
        self.assertEqual(action, "goto")
        self.assertEqual(args["url"], task_api)

        action, args = self.validate(
            {"action": "goto", "args": {"url": self.issue}},
            observation,
        )
        self.assertEqual(action, "goto")
        self.assertEqual(args["url"], self.issue)

    def test_goto_allows_explicit_context_repository_read(self):
        self.payload["context_refs"] = [
            "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + "a" * 40,
        ]
        observation = {"generation": 4, "url": self.repo_url, "elements": []}
        context_blob = (
            "https://github.com/GK-studio-JP/ai-os-runtime/blob/main/runtime.py"
        )
        action, args = self.validate(
            {"action": "goto", "args": {"url": context_blob}},
            observation,
        )
        self.assertEqual(action, "goto")
        self.assertEqual(args["url"], context_blob)

    def test_goto_rejects_non_https_private_and_cross_repository_urls(self):
        observation = {"generation": 4, "url": self.repo_url, "elements": []}
        denied = [
            "file:///etc/passwd",
            "http://github.com/GK-studio-JP/ai-os-runtime-browser-worker",
            "https://127.0.0.1/",
            "https://localhost/",
            "https://github.com/other/repo",
            "https://api.github.com/repos/other/repo/commits/main",
            "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker-evil",
        ]
        for url in denied:
            with self.subTest(url=url):
                with self.assertRaises(LauncherError):
                    self.validate(
                        {"action": "goto", "args": {"url": url}},
                        observation,
                    )

    def test_kernel_receipt_binds_dispatch_task_repository_and_plan(self):
        item = dispatch("#7")
        item["target_repository"] = self.payload["repository"]
        receipt = mutation_receipt()
        validated = validate_mutation_receipt(
            receipt,
            dispatch=item,
            plan_fingerprint="sha256:plan",
        )
        self.assertTrue(validated["approved"])

        tampered = dict(receipt)
        tampered["task"] = "#8"
        with self.assertRaisesRegex(LauncherError, "fingerprint"):
            validate_mutation_receipt(
                tampered,
                dispatch=item,
                plan_fingerprint="sha256:plan",
            )

    def test_fill_requires_receipt_and_is_limited_to_repo_editor(self):
        observation = {
            "generation": 4,
            "url": self.repo_url + "/edit/main/README.md",
            "elements": [
                {
                    "id": "g4-e8",
                    "role": "textbox",
                    "label": "Editing README.md file contents",
                }
            ],
        }
        command = {
            "action": "fill",
            "args": {"elementId": "g4-e8", "text": "replacement"},
        }
        with self.assertRaisesRegex(LauncherError, "disallowed model action"):
            self.validate(command, observation)

        action, args = self.validate(command, observation, mutation_receipt())
        self.assertEqual(action, "fill")
        self.assertEqual(args["text"], "replacement")

        outside = dict(observation)
        outside["url"] = self.repo_url
        with self.assertRaisesRegex(LauncherError, "branch/PR pages"):
            self.validate(command, outside, mutation_receipt())

    def test_commit_to_main_is_denied_until_new_branch_selected(self):
        observation = {
            "generation": 4,
            "url": self.repo_url + "/edit/main/README.md",
            "elements": [
                {
                    "id": "g4-e20",
                    "role": "button",
                    "label": "Commit changes",
                },
                {
                    "id": "g4-e21",
                    "role": "radio",
                    "label": "Commit directly to the main branch",
                    "states": {"checked": True},
                },
                {
                    "id": "g4-e22",
                    "role": "radio",
                    "label": "Create a new branch for this commit and start a pull request",
                    "states": {"checked": False},
                },
            ],
        }
        command = {"action": "click", "args": {"elementId": "g4-e20"}}
        with self.assertRaisesRegex(
            LauncherError,
            "Kernel receipt requires a new branch",
        ):
            self.validate(command, observation, mutation_receipt())

        observation["elements"][1]["states"]["checked"] = False
        observation["elements"][2]["states"]["checked"] = True
        self.assertEqual(
            self.validate(command, observation, mutation_receipt())[0],
            "click",
        )

    def test_commit_to_existing_feature_branch_is_denied(self):
        observation = {
            "generation": 4,
            "url": self.repo_url + "/edit/feature/foo/README.md",
            "elements": [
                {
                    "id": "g4-e20",
                    "role": "button",
                    "label": "Commit changes",
                },
                {
                    "id": "g4-e21",
                    "role": "radio",
                    "label": "Commit directly to the feature/foo branch",
                    "states": {"checked": True},
                },
                {
                    "id": "g4-e22",
                    "role": "radio",
                    "label": "Create a new branch for this commit and start a pull request",
                    "states": {"checked": False},
                },
            ],
        }
        command = {"action": "click", "args": {"elementId": "g4-e20"}}
        with self.assertRaisesRegex(
            LauncherError,
            "Kernel receipt requires a new branch",
        ):
            self.validate(command, observation, mutation_receipt())

    def test_merge_control_is_denied_even_with_receipt(self):
        observation = {
            "generation": 4,
            "url": self.repo_url + "/compare/main...feature",
            "elements": [
                {
                    "id": "g4-e30",
                    "role": "button",
                    "label": "Merge pull request",
                }
            ],
        }
        with self.assertRaisesRegex(LauncherError, "dangerous mutation control"):
            self.validate(
                {"action": "click", "args": {"elementId": "g4-e30"}},
                observation,
                mutation_receipt(),
            )

    def test_click_normalizes_alias_and_requires_navigation_link(self):
        observation = {
            "generation": 4,
            "url": self.repo_url,
            "elements": [
                {
                    "id": "g4-e9",
                    "role": "link",
                    "label": "source",
                    "href": "/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md",
                },
                {
                    "id": "g4-e10",
                    "role": "button",
                    "label": "Delete repository",
                },
            ],
        }
        action, args = self.validate(
            {"action": "click", "args": {"id": "g4-e9"}},
            observation,
        )
        self.assertEqual(action, "click")
        self.assertEqual(args, {"elementId": "g4-e9"})

        with self.assertRaisesRegex(LauncherError, "navigation links"):
            self.validate(
                {"action": "click", "args": {"elementId": "g4-e10"}},
                observation,
            )

    def test_click_rejects_cross_repository_link(self):
        observation = {
            "generation": 4,
            "url": self.repo_url,
            "elements": [
                {
                    "id": "g4-e11",
                    "role": "link",
                    "label": "outside",
                    "href": "https://github.com/other/repo",
                }
            ],
        }
        with self.assertRaisesRegex(LauncherError, "outside task/context repositories"):
            self.validate(
                {"action": "click", "args": {"elementId": "g4-e11"}},
                observation,
            )


class ElementRefreshTests(unittest.TestCase):
    def test_remaps_click_to_fresh_generation_by_role_and_label(self):
        observation = {
            "generation": 2,
            "elements": [
                {
                    "id": "g2-e137",
                    "role": "button",
                    "label": "Open commit details",
                }
            ],
        }
        fresh_page = {
            "generation": 6,
            "elements": [
                {
                    "id": "g6-e150",
                    "role": "button",
                    "label": "Open commit details",
                }
            ],
        }
        self.assertEqual(
            refresh_element_args(
                "click",
                {"elementId": "g2-e137"},
                observation,
                fresh_page,
            ),
            {"elementId": "g6-e150"},
        )

    def test_remaps_fill_to_fresh_generation_by_role_and_label(self):
        observation = {
            "generation": 2,
            "elements": [
                {
                    "id": "g2-e20",
                    "role": "textbox",
                    "label": "Commit message",
                }
            ],
        }
        fresh_page = {
            "generation": 5,
            "elements": [
                {
                    "id": "g5-e22",
                    "role": "textbox",
                    "label": "Commit message",
                }
            ],
        }
        self.assertEqual(
            refresh_element_args(
                "fill",
                {"elementId": "g2-e20", "text": "msg"},
                observation,
                fresh_page,
            ),
            {"elementId": "g5-e22", "text": "msg"},
        )

    def test_rejects_ambiguous_fresh_element_match(self):
        observation = {
            "generation": 2,
            "elements": [
                {
                    "id": "g2-e10",
                    "role": "button",
                    "label": "Continue",
                }
            ],
        }
        fresh_page = {
            "generation": 3,
            "elements": [
                {"id": "g3-e10", "role": "button", "label": "Continue"},
                {"id": "g3-e11", "role": "button", "label": "Continue"},
            ],
        }
        with self.assertRaisesRegex(LauncherError, "2 matches"):
            refresh_element_args(
                "click",
                {"elementId": "g2-e10"},
                observation,
                fresh_page,
            )


def canonical_comment(
    comment_id: int,
    created_at: datetime,
    event_type: str,
    agent_id: str,
    *,
    task: str = "#1",
    updated_at: datetime | None = None,
    actor: str = "repo-owner",
):
    next_action = "continue" if event_type in {"CLAIM", "HEARTBEAT"} else None
    payload = {
        "type": event_type,
        "agent_id": agent_id,
        "task": task,
        "idempotency_key": f"{agent_id}:{task}:{event_type.lower()}:{comment_id}",
        "summary": f"{event_type.lower()} event",
        "next_action": next_action,
        "artifacts": [],
    }

    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "id": comment_id,
        "created_at": stamp(created_at),
        "updated_at": stamp(updated_at or created_at),
        "body": "<!-- ai-bb:v1 -->\n```json\n" + json.dumps(payload) + "\n```",
        "user": {"login": actor},
        "author_association": "OWNER",
    }


class ProtocolTests(unittest.TestCase):
    def test_protocol_event_body_uses_fenced_json(self):
        body = protocol_event_body(
            "CLAIM",
            agent_id="a",
            task="#1",
            summary="claimed",
            next_action="continue",
            artifacts=[],
        )
        self.assertIn("<!-- ai-bb:v1 -->\n```json\n", body)
        self.assertTrue(body.endswith("\n```"))

    def test_claim_and_result_helpers(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [canonical_comment(1, t0, "CLAIM", "a")]
        self.assertTrue(
            canonical_claim_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                now=t0 + timedelta(minutes=1),
            )
        )
        self.assertFalse(
            canonical_result_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                now=t0 + timedelta(minutes=1),
            )
        )
        comments.append(canonical_comment(2, t0 + timedelta(minutes=2), "RESULT", "a"))
        self.assertTrue(
            canonical_result_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                now=t0 + timedelta(minutes=3),
            )
        )
        self.assertTrue(
            canonical_task_completed(
                comments,
                task="#1",
                now=t0 + timedelta(minutes=3),
            )
        )

    def test_claim_helper_rejects_same_agent_from_different_actor(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [canonical_comment(1, t0, "CLAIM", "a", actor="owner-a")]
        self.assertTrue(
            canonical_claim_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="owner-a",
                now=t0 + timedelta(minutes=1),
            )
        )
        self.assertFalse(
            canonical_claim_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="owner-b",
                now=t0 + timedelta(minutes=1),
            )
        )

    def test_overlapping_claim_does_not_steal_live_lease(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=7), "CLAIM", "b"),
        ]
        now = t0 + timedelta(minutes=8)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", owner_actor="repo-owner", now=now))
        self.assertFalse(canonical_claim_present(comments, task="#1", agent_id="b", owner_actor="repo-owner", now=now))
        self.assertFalse(canonical_task_completed(comments, task="#1", now=now))

    def test_expired_lease_allows_reclaim(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=16), "CLAIM", "b"),
        ]
        now = t0 + timedelta(minutes=17)
        self.assertFalse(canonical_claim_present(comments, task="#1", agent_id="a", owner_actor="repo-owner", now=now))
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="b", owner_actor="repo-owner", now=now))

    def test_loser_result_does_not_complete_task(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=7), "CLAIM", "b"),
            canonical_comment(3, t0 + timedelta(minutes=8), "RESULT", "b"),
        ]
        now = t0 + timedelta(minutes=9)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", owner_actor="repo-owner", now=now))
        self.assertFalse(canonical_result_present(comments, task="#1", agent_id="b", owner_actor="repo-owner", now=now))
        self.assertFalse(canonical_task_completed(comments, task="#1", now=now))

    def test_heartbeat_extends_live_owner(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=10), "HEARTBEAT", "a"),
        ]
        now = t0 + timedelta(minutes=20)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", owner_actor="repo-owner", now=now))

    def test_ensure_canonical_lease_renews_expiring_owner_without_replacing_task_page(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        before = [canonical_comment(1, t0, "CLAIM", "a")]
        after = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=11), "HEARTBEAT", "a"),
        ]
        now = t0 + timedelta(minutes=11)

        class FakeRelay:
            def __init__(self):
                self.calls = []

            def command(self, action, args):
                self.calls.append((action, args))
                if action == "newPage":
                    return {"pageIndex": 3}
                return {}

        relay = FakeRelay()
        with (
            patch("browser_worker_launcher.comments", side_effect=[before, after]),
            patch("browser_worker_launcher.append_issue_comment") as append_mock,
            patch(
                "browser_worker_launcher.wait_for_protocol_event",
                side_effect=lambda token, issue_no, predicate: predicate(after),
            ),
        ):
            state = ensure_canonical_lease(
                token="token",
                issue_no=1,
                issue_url="https://github.com/GK-studio-JP/ai-bulletin-board/issues/1",
                relay=relay,
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                phase="task work",
                now=now,
            )

        self.assertEqual(state.owner, "a")
        self.assertEqual(state.lease_status, "active")
        self.assertEqual(
            relay.calls,
            [
                (
                    "newPage",
                    {"url": "https://github.com/GK-studio-JP/ai-bulletin-board/issues/1"},
                ),
                ("switchPage", {"index": 0}),
            ],
        )
        self.assertEqual(append_mock.call_args.kwargs["page_index"], 3)
        body = append_mock.call_args.args[2]
        payload = json.loads(body.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual(payload["type"], "HEARTBEAT")
        self.assertEqual(payload["agent_id"], "a")
        self.assertTrue(payload["idempotency_key"].startswith("a:#1:heartbeat:"))
        self.assertNotEqual(payload["idempotency_key"], "a:#1:heartbeat")

    def test_ensure_canonical_lease_skips_heartbeat_when_active(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        rows = [canonical_comment(1, t0, "CLAIM", "a")]
        with (
            patch("browser_worker_launcher.comments", return_value=rows),
            patch("browser_worker_launcher.append_issue_comment") as append_mock,
        ):
            state = ensure_canonical_lease(
                token=None,
                issue_no=1,
                issue_url="https://github.com/GK-studio-JP/ai-bulletin-board/issues/1",
                relay=object(),
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                phase="task work",
                now=t0 + timedelta(minutes=1),
            )
        self.assertEqual(state.lease_status, "active")
        append_mock.assert_not_called()

    def test_ensure_canonical_lease_fails_for_non_owner(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        rows = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=7), "CLAIM", "b"),
        ]
        with (
            patch("browser_worker_launcher.comments", return_value=rows),
            patch("browser_worker_launcher.append_issue_comment") as append_mock,
        ):
            with self.assertRaisesRegex(LauncherError, "ownership was lost"):
                ensure_canonical_lease(
                    token=None,
                    issue_no=1,
                    issue_url="https://github.com/GK-studio-JP/ai-bulletin-board/issues/1",
                    relay=object(),
                    task="#1",
                    agent_id="b",
                    owner_actor="repo-owner",
                    phase="task work",
                    now=t0 + timedelta(minutes=8),
                )
        append_mock.assert_not_called()

    def test_ensure_canonical_lease_fails_closed_when_heartbeat_is_not_verified(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        before = [canonical_comment(1, t0, "CLAIM", "a")]

        class FakeRelay:
            def __init__(self):
                self.calls = []

            def command(self, action, args):
                self.calls.append((action, args))
                if action == "newPage":
                    return {"pageIndex": 4}
                return {}

        relay = FakeRelay()
        with (
            patch("browser_worker_launcher.comments", return_value=before),
            patch("browser_worker_launcher.append_issue_comment") as append_mock,
            patch("browser_worker_launcher.wait_for_protocol_event", return_value=False),
        ):
            with self.assertRaisesRegex(LauncherError, "HEARTBEAT was not verified"):
                ensure_canonical_lease(
                    token=None,
                    issue_no=1,
                    issue_url="https://github.com/GK-studio-JP/ai-bulletin-board/issues/1",
                    relay=relay,
                    task="#1",
                    agent_id="a",
                    owner_actor="repo-owner",
                    phase="RESULT submission",
                    now=t0 + timedelta(minutes=11),
                )
        self.assertEqual(append_mock.call_args.kwargs["page_index"], 4)
        self.assertEqual(relay.calls[-1], ("switchPage", {"index": 0}))

    def test_edited_protocol_history_fails_closed(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(
                1,
                t0,
                "CLAIM",
                "a",
                updated_at=t0 + timedelta(seconds=1),
            )
        ]
        with self.assertRaisesRegex(LauncherError, "canonical history is unsafe"):
            canonical_claim_present(
                comments,
                task="#1",
                agent_id="a",
                owner_actor="repo-owner",
                now=t0 + timedelta(minutes=1),
            )


class FinishEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.issue = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/11"
        self.sha = "a" * 40
        self.file_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/"
            "blob/main/browser_worker_launcher.py"
        )
        self.task_payload = {
            "process": "PROC-RUNTIME-BROWSER-WORKER",
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": (
                "Verify the current main commit SHA and confirm "
                "browser_worker_launcher.py exists."
            ),
            "acceptance": [
                "RESULT artifacts include the current main commit SHA.",
                "RESULT summary confirms browser_worker_launcher.py exists.",
            ],
        }

    def test_rejects_finish_without_observed_task_evidence(self):
        command = {
            "kind": "finish",
            "summary": "Confirmed browser_worker_launcher.py exists.",
            "artifacts": [self.sha, self.file_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": self.file_url},
            ],
        }
        ledger = [{"url": self.issue, "pageText": self.sha + " browser_worker_launcher.py"}]
        rejection = validate_finish_evidence(
            command, ledger, self.task_payload, self.issue
        )
        self.assertIn("no non-canonical task page", rejection)

    def test_accepts_observed_sha_and_visited_file(self):
        commit_url = (
            "https://api.github.com/repos/GK-studio-JP/"
            "ai-os-runtime-browser-worker/commits/main"
        )
        ledger = [
            {"url": commit_url, "pageText": json.dumps({"sha": self.sha})},
            {"url": self.file_url, "pageText": "browser_worker_launcher.py source"},
        ]
        command = {
            "kind": "finish",
            "summary": "Confirmed browser_worker_launcher.py exists on main.",
            "artifacts": [self.sha, self.file_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": self.file_url},
            ],
        }
        self.assertIsNone(
            validate_finish_evidence(command, ledger, self.task_payload, self.issue)
        )

    def test_rejects_unobserved_artifact_sha(self):
        observed_sha = "b" * 40
        ledger = [
            {
                "url": "https://api.github.com/repos/GK-studio-JP/"
                "ai-os-runtime-browser-worker/commits/main",
                "pageText": json.dumps({"sha": observed_sha}),
            },
            {"url": self.file_url, "pageText": "browser_worker_launcher.py source"},
        ]
        command = {
            "kind": "finish",
            "summary": "Confirmed browser_worker_launcher.py exists on main.",
            "artifacts": [self.sha, self.file_url],
            "evidence": [
                {"kind": "observed_text", "value": "browser_worker_launcher.py"},
                {"kind": "visited_url", "value": self.file_url},
            ],
        }
        rejection = validate_finish_evidence(
            command, ledger, self.task_payload, self.issue
        )
        self.assertIn("does not match the current main HEAD", rejection)

    def test_rejects_old_commit_detail_for_current_main(self):
        commit_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/commit/"
            + self.sha
        )
        ledger = [
            {"url": commit_url, "pageText": self.sha},
            {"url": self.file_url, "pageText": "browser_worker_launcher.py source"},
        ]
        command = {
            "kind": "finish",
            "summary": "Confirmed browser_worker_launcher.py exists on main.",
            "artifacts": [self.sha, self.file_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": self.file_url},
            ],
        }
        rejection = validate_finish_evidence(
            command, ledger, self.task_payload, self.issue
        )
        self.assertIn("/commits/main", rejection)
        self.assertIn("do not prove the current main HEAD", rejection)

    def test_rejects_missing_file_visit(self):
        commit_url = (
            "https://api.github.com/repos/GK-studio-JP/"
            "ai-os-runtime-browser-worker/commits/main"
        )
        ledger = [
            {"url": commit_url, "pageText": json.dumps({"sha": self.sha})},
        ]
        command = {
            "kind": "finish",
            "summary": "Confirmed browser_worker_launcher.py exists on main.",
            "artifacts": [self.sha, self.file_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
            ],
        }
        rejection = validate_finish_evidence(
            command, ledger, self.task_payload, self.issue
        )
        self.assertIn("file existence was not verified", rejection)

    def test_rejects_change_task_without_implementation_artifact(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a deterministic Runtime drift guard.",
            "acceptance": [
                "The guard runs in GitHub Actions and records workflow evidence.",
            ],
            "contracts": [
                "Canonical runtime source: GK-studio-JP/ai-os-runtime@" + self.sha,
            ],
        }
        canonical_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + self.sha
        )
        ledger = [
            {"url": canonical_url, "pageText": self.sha},
            {
                "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker",
                "pageText": "repository",
            },
        ]
        command = {
            "kind": "finish",
            "summary": "Inspected the repository.",
            "artifacts": [self.sha],
            "evidence": [{"kind": "extracted_fact", "value": self.sha}],
        }
        rejection = validate_finish_evidence(
            command, ledger, task_payload, self.issue
        )
        self.assertIn("implementation task requires", rejection)

    def test_accepts_change_task_with_pr_and_workflow_evidence(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a deterministic Runtime drift guard.",
            "acceptance": [
                "The guard runs in GitHub Actions and records workflow evidence.",
            ],
            "contracts": [
                "Canonical runtime source: GK-studio-JP/ai-os-runtime@" + self.sha,
            ],
        }
        canonical_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + self.sha
        )
        pr_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/pull/123"
        )
        run_url = (
            "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/actions/runs/456"
        )
        ledger = [
            {"url": canonical_url, "pageText": self.sha},
            {"url": pr_url, "pageText": "Pull request 123"},
            {"url": run_url, "pageText": "workflow success"},
        ]
        command = {
            "kind": "finish",
            "summary": "Added and verified the deterministic Runtime drift guard.",
            "artifacts": [self.sha, pr_url, run_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": pr_url},
                {"kind": "visited_url", "value": run_url},
            ],
        }
        self.assertIsNone(
            validate_finish_evidence(
                command,
                ledger,
                task_payload,
                self.issue,
                source_mutation_performed=True,
                require_new_pr=True,
                preexisting_pull_urls=set(),
                preexisting_workflow_urls=set(),
            )
        )

    def test_rejects_finish_without_source_mutation_for_branch_pr_task(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a deterministic Runtime drift guard.",
            "acceptance": ["The guard runs in GitHub Actions with workflow evidence."],
            "contracts": [
                "Canonical runtime source: GK-studio-JP/ai-os-runtime@" + self.sha,
            ],
        }
        pr_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/pull/123"
        run_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/actions/runs/456"
        ledger = [
            {"url": "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + self.sha, "pageText": self.sha},
            {"url": pr_url, "pageText": "Pull request 123"},
            {"url": run_url, "pageText": "workflow success"},
        ]
        command = {
            "kind": "finish",
            "summary": "Added and verified the deterministic Runtime drift guard.",
            "artifacts": [self.sha, pr_url, run_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": pr_url},
                {"kind": "visited_url", "value": run_url},
            ],
        }
        rejection = validate_finish_evidence(
            command,
            ledger,
            task_payload,
            self.issue,
            source_mutation_performed=False,
            require_new_pr=True,
        )
        self.assertIn("source-file mutation", rejection)

    def test_rejects_preexisting_pull_request_evidence(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a deterministic Runtime drift guard.",
            "acceptance": ["The guard runs in GitHub Actions with workflow evidence."],
            "contracts": [
                "Canonical runtime source: GK-studio-JP/ai-os-runtime@" + self.sha,
            ],
        }
        pr_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/pull/15"
        run_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/actions/runs/456"
        ledger = [
            {"url": "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + self.sha, "pageText": self.sha},
            {"url": pr_url, "pageText": "unrelated pre-existing pull request"},
            {"url": run_url, "pageText": "workflow success"},
        ]
        command = {
            "kind": "finish",
            "summary": "Added and verified the deterministic Runtime drift guard.",
            "artifacts": [self.sha, pr_url, run_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": pr_url},
                {"kind": "visited_url", "value": run_url},
            ],
        }
        rejection = validate_finish_evidence(
            command,
            ledger,
            task_payload,
            self.issue,
            source_mutation_performed=True,
            require_new_pr=True,
            preexisting_pull_urls={pr_url},
        )
        self.assertIn("predated this worker run", rejection)

    def test_rejects_preexisting_workflow_evidence(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a deterministic Runtime drift guard.",
            "acceptance": ["The guard runs in GitHub Actions with workflow evidence."],
            "contracts": [
                "Canonical runtime source: GK-studio-JP/ai-os-runtime@" + self.sha,
            ],
        }
        pr_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/pull/123"
        run_url = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/actions/runs/456"
        ledger = [
            {"url": "https://github.com/GK-studio-JP/ai-os-runtime/commit/" + self.sha, "pageText": self.sha},
            {"url": pr_url, "pageText": "Pull request 123"},
            {"url": run_url, "pageText": "workflow success"},
        ]
        command = {
            "kind": "finish",
            "summary": "Added and verified the deterministic Runtime drift guard.",
            "artifacts": [self.sha, pr_url, run_url],
            "evidence": [
                {"kind": "extracted_fact", "value": self.sha},
                {"kind": "visited_url", "value": pr_url},
                {"kind": "visited_url", "value": run_url},
            ],
        }
        rejection = validate_finish_evidence(
            command,
            ledger,
            task_payload,
            self.issue,
            source_mutation_performed=True,
            require_new_pr=True,
            preexisting_pull_urls=set(),
            preexisting_workflow_urls={run_url},
        )
        self.assertIn("workflow evidence reused", rejection)

    def test_task_payload_parses_canonical_task_marker(self):
        body = (
            "<!-- ai-os-task:v1 -->\n"
            "```json\n"
            '{"process":"PROC-RUNTIME-BROWSER-WORKER","objective":"verify"}\n'
            "```\n"
        )
        self.assertEqual(_task_payload_from_issue(body)["objective"], "verify")


class ObservationTests(unittest.TestCase):
    def test_reduce_observation_preserves_link_href_for_policy(self):
        page = {
            "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker",
            "generation": 8,
            "pageText": "source",
            "elements": [
                {
                    "id": "g8-e1",
                    "role": "link",
                    "text": "WORKER.md",
                    "attributes": {
                        "href": "/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
                    },
                }
            ],
        }
        reduced = reduce_observation(page)
        self.assertEqual(
            reduced["elements"][0]["href"],
            "/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md",
        )

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

    def test_reduce_observation_prioritizes_editor_controls(self):
        page = {
            "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/edit/main/runtime.py",
            "generation": 8,
            "pageText": "source",
            "elements": [
                *[
                    {
                        "id": f"g8-e{i}",
                        "role": "link",
                        "text": f"link-{i}",
                        "attributes": {"href": f"/path/{i}"},
                    }
                    for i in range(20)
                ],
                {
                    "id": "g8-e99",
                    "role": "textbox",
                    "label": "Editing runtime.py file contents",
                    "editable": True,
                    "text": "code",
                },
                {
                    "id": "g8-e100",
                    "role": "radio",
                    "label": "Create a new branch for this commit",
                    "states": {"checked": False},
                },
            ],
        }
        reduced = reduce_observation(page, max_elements=4)
        self.assertEqual(reduced["elements"][0]["id"], "g8-e99")
        self.assertEqual(reduced["elements"][1]["id"], "g8-e100")

    def test_prompt_is_compact_and_carries_mutation_contract(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Self-improvement test: add a deterministic guard.",
            "acceptance": ["The guard runs in GitHub Actions with workflow evidence."],
            "contracts": ["Canonical runtime source: GK-studio-JP/ai-os-runtime@abc123"],
            "context_refs": ["https://github.com/GK-studio-JP/ai-os-runtime/commit/abc123"],
        }
        text = prompt(
            "browser-chat-gemini-test",
            "#16",
            "https://github.com/GK-studio-JP/ai-bulletin-board/issues/16",
            {
                "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker",
                "generation": 8,
                "pageText": "repository",
                "elements": [],
                "dialogs": [],
            },
            1,
            "ready",
            True,
            task_payload,
            [],
            True,
        )
        self.assertLess(len(text), 7000)
        self.assertIn('"contracts":["Canonical runtime source:', text)
        self.assertIn("perform the smallest authorized branch/PR mutation", text)
        self.assertIn('"source_mutation_performed":false', text)
        self.assertIn("do not browse /pulls first", text)
        self.assertIn("exact observed 40-character SHA alone", text)

    def test_prompt_marks_new_file_editor_progress_after_filename(self):
        task_payload = {
            "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            "objective": "Add a drift guard.",
            "acceptance": ["Create a branch and pull request."],
            "contracts": [],
            "context_refs": [],
        }
        text = prompt(
            "browser-chat-gemini-test",
            "#16",
            "https://github.com/GK-studio-JP/ai-bulletin-board/issues/16",
            {
                "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/new/main",
                "generation": 8,
                "pageText": "Creating a new file",
                "elements": [
                    {
                        "id": "g8-e198",
                        "role": "textbox",
                        "label": (
                            "Editing test_contract_drift.py file contents "
                            "Use Control + Shift + m to toggle the tab key moving focus."
                        ),
                    }
                ],
                "dialogs": [],
            },
            2,
            "ready",
            True,
            task_payload,
            [],
            True,
            source_mutation_performed=True,
        )
        self.assertIn(
            '"editor_progress":{"file_name":"test_contract_drift.py",'
            '"next_required":"fill:file_contents"}',
            text,
        )
        self.assertIn("Do not fill file_name again", text)


if __name__ == "__main__":
    unittest.main()
