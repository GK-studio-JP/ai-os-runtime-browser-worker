import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from browser_worker_launcher import (
    LauncherError,
    _task_payload_from_issue,
    _validate_model_action,
    action_allowed_before_claim,
    canonical_claim_present,
    canonical_result_present,
    canonical_task_completed,
    extract_model_command,
    issue_number_from_dispatch,
    plan_from_file,
    reduce_observation,
    refresh_element_args,
    resolve_plan,
    validate_finish_evidence,
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
        "body": "<!-- ai-bb:v1 -->\n" + json.dumps(payload),
    }


class ProtocolTests(unittest.TestCase):
    def test_claim_and_result_helpers(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [canonical_comment(1, t0, "CLAIM", "a")]
        self.assertTrue(
            canonical_claim_present(
                comments,
                task="#1",
                agent_id="a",
                now=t0 + timedelta(minutes=1),
            )
        )
        self.assertFalse(
            canonical_result_present(
                comments,
                task="#1",
                agent_id="a",
                now=t0 + timedelta(minutes=1),
            )
        )
        comments.append(canonical_comment(2, t0 + timedelta(minutes=2), "RESULT", "a"))
        self.assertTrue(
            canonical_result_present(
                comments,
                task="#1",
                agent_id="a",
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

    def test_overlapping_claim_does_not_steal_live_lease(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=7), "CLAIM", "b"),
        ]
        now = t0 + timedelta(minutes=8)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", now=now))
        self.assertFalse(canonical_claim_present(comments, task="#1", agent_id="b", now=now))
        self.assertFalse(canonical_task_completed(comments, task="#1", now=now))

    def test_expired_lease_allows_reclaim(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=16), "CLAIM", "b"),
        ]
        now = t0 + timedelta(minutes=17)
        self.assertFalse(canonical_claim_present(comments, task="#1", agent_id="a", now=now))
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="b", now=now))

    def test_loser_result_does_not_complete_task(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=7), "CLAIM", "b"),
            canonical_comment(3, t0 + timedelta(minutes=8), "RESULT", "b"),
        ]
        now = t0 + timedelta(minutes=9)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", now=now))
        self.assertFalse(canonical_result_present(comments, task="#1", agent_id="b", now=now))
        self.assertFalse(canonical_task_completed(comments, task="#1", now=now))

    def test_heartbeat_extends_live_owner(self):
        t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        comments = [
            canonical_comment(1, t0, "CLAIM", "a"),
            canonical_comment(2, t0 + timedelta(minutes=10), "HEARTBEAT", "a"),
        ]
        now = t0 + timedelta(minutes=20)
        self.assertTrue(canonical_claim_present(comments, task="#1", agent_id="a", now=now))

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
                now=t0 + timedelta(minutes=1),
            )

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

    def test_task_payload_parses_canonical_task_marker(self):
        body = (
            "<!-- ai-os-task:v1 -->\n"
            "```json\n"
            '{"process":"PROC-RUNTIME-BROWSER-WORKER","objective":"verify"}\n'
            "```\n"
        )
        self.assertEqual(_task_payload_from_issue(body)["objective"], "verify")


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
