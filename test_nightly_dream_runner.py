import json
import os
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from unittest.mock import patch

import nightly_dream_runner as runner

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

    def test_legacy_markerless_run_and_cycle_advance_watermark(self):
        cycle = "sha256:" + "c" * 64
        issue = {
            "number": 53,
            "state": "closed",
            "created_at": "2026-10-02T03:40:48Z",
            "body": (
                "<!-- ai-os-task:v1 -->\n```json\n"
                + json.dumps({"process": "PROC-AIOS"})
                + "\n```\n\n"
                + "```json\n"
                + json.dumps(
                    {
                        "schema": "aios-dream-run:v1",
                        "cycle_id": cycle,
                        "contract_version": 1,
                        "window_start": "2026-10-01T15:00:00Z",
                        "window_end": "2026-10-02T02:40:20Z",
                        "settle_delay_seconds": 3600,
                        "previous_success_issue": 0,
                        "previous_success_window_end": None,
                    }
                )
                + "\n```"
            ),
        }
        comments = [
            {
                "body": "```json\n"
                + json.dumps(
                    {
                        "schema": "aios-dream-cycle:v1",
                        "cycle_id": cycle,
                        "status": "completed",
                        "window_start": "2026-10-01T15:00:00Z",
                        "window_end": "2026-10-02T02:40:20Z",
                        "bundle_fingerprint": "sha256:" + "d" * 64,
                        "counts": {},
                        "deferred": [],
                        "memory_prs": [],
                        "project_prs": [],
                    }
                )
                + "\n```"
            },
            protocol_result(53),
        ]
        found = latest_success([(issue, comments)])
        self.assertIsNotNone(found)
        self.assertEqual(found[0]["number"], 53)
        start, end, previous = resolve_window(
            [(issue, comments)],
            datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(start, datetime(2026, 10, 2, 2, 40, 20, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(previous, 53)

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


class NightlyDreamTriggerProbeTests(unittest.TestCase):
    def test_trigger_probe_only_reaches_browser_agent_and_gemini(self):
        calls = []

        class FakeRelay:
            def __init__(self, base, key, session_id):
                calls.append(("init", base, key, session_id))

            def ready(self):
                calls.append(("ready",))
                return {"ready": True}

            def command(self, command, args):
                calls.append((command, args))
                if command == "goto":
                    return {"url": "https://gemini.google.com/app"}
                return {"url": "https://example.invalid/"}

        args = Namespace(session_id="gcp-browser-1", probe_id="unit-probe")
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://relay.example",
                "SUPABASE_SECRET_KEY": "test-secret",
            },
            clear=False,
        ), patch.object(runner, "Relay", FakeRelay), patch.object(
            runner,
            "_acquire_production_histories",
            side_effect=AssertionError("trigger probe must not acquire Dream source"),
        ):
            result = runner.trigger_probe(args)

        self.assertEqual(result["status"], "trigger_ready")
        self.assertEqual(result["probe_id"], "unit-probe")
        self.assertEqual(result["gemini_url"], "https://gemini.google.com/app")
        self.assertEqual(
            calls,
            [
                ("init", "https://relay.example", "test-secret", "gcp-browser-1"),
                ("ready",),
                ("start", {}),
                ("goto", {"url": "https://gemini.google.com/app"}),
            ],
        )



class NightlyDreamTriggerProbeTests(unittest.TestCase):
    def test_trigger_probe_only_reaches_browser_agent_and_gemini(self):
        calls = []

        class FakeRelay:
            def __init__(self, base, key, session_id):
                calls.append(("init", base, key, session_id))

            def ready(self):
                calls.append(("ready",))
                return {"status": "ready"}

            def command(self, name, payload):
                calls.append((name, payload))
                if name == "goto":
                    return {"url": "https://gemini.google.com/app"}
                return {"url": "https://example.invalid/"}

        args = Namespace(session_id="probe-session", probe_id="probe-1")
        with (
            patch.dict(
                os.environ,
                {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "secret"},
                clear=False,
            ),
            patch.object(runner, "Relay", FakeRelay),
            patch.object(
                runner,
                "_acquire_production_histories",
                side_effect=AssertionError("trigger probe must not acquire Dream source"),
            ),
        ):
            result = runner.trigger_probe(args)

        self.assertEqual(result["status"], "trigger_ready")
        self.assertEqual(result["probe_id"], "probe-1")
        self.assertEqual(
            calls,
            [
                ("init", "https://example.supabase.co", "secret", "probe-session"),
                ("ready",),
                ("start", {}),
                ("goto", {"url": runner.GEMINI_URL}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
