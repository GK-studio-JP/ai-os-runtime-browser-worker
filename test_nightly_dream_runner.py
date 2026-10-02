import json
import unittest
from unittest.mock import patch

import nightly_dream_runner as runner
from datetime import datetime, timezone

from ai_os_browser_worker.nightly_dream import (
    CYCLE_MARKER,
    cycle_id,
    cycle_state_body,
    latest_success,
    oldest_open_run,
    resolve_window,
    run_issue_body,
    run_metadata,
)


def protocol_result(issue_number: int) -> dict:
    return {
        "id": 9001,
        "created_at": "2026-10-02T03:00:00Z",
        "updated_at": "2026-10-02T03:00:00Z",
        "author_association": "OWNER",
        "user": {"login": "GK-studio-JP"},
        "body": (
            "<!-- ai-bb:v1 -->\n```json\n"
            + json.dumps(
                {
                    "type": "RESULT",
                    "agent_id": "dream-test",
                    "task": f"#{issue_number}",
                    "idempotency_key": "result",
                    "summary": "done",
                    "next_action": None,
                    "artifacts": [],
                }
            )
            + "\n```"
        ),
    }


class NightlyDreamLogicTests(unittest.TestCase):
    def test_cycle_id_is_stable(self):
        start = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(cycle_id(start, end), cycle_id(start, end))
        self.assertTrue(cycle_id(start, end).startswith("sha256:"))

    def test_run_issue_body_round_trips_metadata(self):
        start = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
        cycle = cycle_id(start, end)
        body = run_issue_body(
            cycle=cycle,
            window_start=start,
            window_end=end,
            previous_success_issue=53,
        )
        meta = run_metadata({"body": body})
        self.assertEqual(meta["cycle_id"], cycle)
        self.assertEqual(meta["previous_success_issue"], 53)
        self.assertEqual(meta["contract_version"], 2)

    def test_latest_success_requires_cycle_state_and_result(self):
        issue = {
            "number": 53,
            "state": "closed",
            "created_at": "2026-10-02T01:00:00Z",
            "body": run_issue_body(
                cycle="sha256:" + "a" * 64,
                window_start=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc),
                previous_success_issue=None,
            ),
        }
        state = {
            "schema": "aios-dream-cycle:v1",
            "cycle_id": "sha256:" + "a" * 64,
            "status": "completed",
            "window_start": "2026-10-02T00:00:00Z",
            "window_end": "2026-10-02T02:00:00Z",
            "bundle_fingerprint": "sha256:" + "b" * 64,
            "counts": {},
            "deferred": [],
            "memory_prs": [],
            "project_prs": [],
        }
        comments = [
            {
                "body": CYCLE_MARKER + "\n```json\n" + json.dumps(state) + "\n```"
            },
            protocol_result(53),
        ]
        found = latest_success([(issue, comments)])
        self.assertIsNotNone(found)
        self.assertEqual(found[0]["number"], 53)

    def test_resolve_window_uses_latest_success(self):
        start = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)
        cycle = cycle_id(start, end)
        issue = {
            "number": 53,
            "state": "closed",
            "created_at": "2026-10-02T01:00:00Z",
            "body": run_issue_body(
                cycle=cycle,
                window_start=start,
                window_end=end,
                previous_success_issue=None,
            ),
        }
        comments = [
            {
                "body": cycle_state_body(
                    cycle=cycle,
                    window_start=start,
                    window_end=end,
                    bundle_fingerprint="sha256:" + "b" * 64,
                    counts={},
                    deferred=[],
                )
            },
            protocol_result(53),
        ]
        window_start, window_end, previous = resolve_window(
            [(issue, comments)],
            datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(window_start, end)
        self.assertEqual(window_end, datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(previous, 53)

    def test_oldest_open_run_wins(self):
        def issue(number: int, created: str):
            return {
                "number": number,
                "state": "open",
                "created_at": created,
                "body": run_issue_body(
                    cycle="sha256:" + str(number % 10) * 64,
                    window_start=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
                    window_end=datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc),
                    previous_success_issue=None,
                ),
            }

        found = oldest_open_run(
            [
                (issue(60, "2026-10-02T05:00:00Z"), []),
                (issue(59, "2026-10-02T04:00:00Z"), []),
            ]
        )
        self.assertEqual(found[0]["number"], 59)

    def test_auto_source_parser_does_not_require_histories_file(self):
        args = runner.parser().parse_args(["--auto-source"])
        self.assertTrue(args.auto_source)
        self.assertIsNone(args.histories_file)

    def test_validated_history_rejects_incomplete_comment_page(self):
        issue = {
            "number": 52,
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T01:00:00Z",
            "comments": 2,
        }
        one_comment = {
            "id": 1,
            "created_at": "2026-10-02T00:30:00Z",
            "updated_at": "2026-10-02T00:30:00Z",
            "author_association": "OWNER",
            "body": "x",
            "user": {"login": "GK-studio-JP"},
        }
        with patch.object(runner, "_live_comments", return_value=[one_comment]):
            with self.assertRaisesRegex(
                runner.LauncherError,
                "declares 2 comments but 1 were fetched",
            ):
                runner._validated_history(issue)



if __name__ == "__main__":
    unittest.main()
