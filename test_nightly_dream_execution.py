from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import nightly_dream_execution as dream


def comment(comment_id, body, when="2026-10-02T05:00:00Z"):
    return {
        "id": comment_id,
        "created_at": when,
        "updated_at": when,
        "author_association": "OWNER",
        "user": {"login": "GK-studio-JP"},
        "body": body,
    }


def event_body(payload):
    return "<!-- ai-bb:v1 -->\n```json\n" + json.dumps(payload) + "\n```"


class NightlyDreamExecutionTests(unittest.TestCase):
    def test_run_body_round_trips_metadata(self):
        metadata = {
            "schema": "aios-dream-run:v1",
            "cycle_id": "sha256:" + "a" * 64,
            "contract_version": 1,
            "window_start": "2026-10-02T00:00:00+09:00",
            "window_end": "2026-10-02T01:00:00+09:00",
            "settle_delay_seconds": 3600,
            "previous_success_issue": 53,
            "previous_success_window_end": "2026-10-02T00:00:00+09:00",
        }
        body = dream._run_body(metadata)
        self.assertEqual(dream._dream_metadata({"body": body}), metadata)

    def test_cycle_id_is_stable(self):
        first = dream._cycle_id("a", "b")
        second = dream._cycle_id("a", "b")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")

    def test_cycle_state_requires_matching_cycle_and_window(self):
        metadata = {
            "cycle_id": "sha256:" + "b" * 64,
            "window_end": "2026-10-02T02:00:00+09:00",
        }
        matching = {
            "schema": "aios-dream-cycle:v1",
            "cycle_id": metadata["cycle_id"],
            "status": "completed",
            "window_end": metadata["window_end"],
        }
        wrong = {**matching, "window_end": "2026-10-02T03:00:00+09:00"}
        rows = [
            comment(1, "```json\n" + json.dumps(wrong) + "\n```"),
            comment(2, "```json\n" + json.dumps(matching) + "\n```"),
        ]
        self.assertEqual(dream._cycle_state(rows, metadata), matching)

    def test_deferred_entry_preserves_canonical_provenance(self):
        task = {
            "task": "#77",
            "source_fingerprint": "sha256:" + "c" * 64,
            "source_refs": ["issue:#77", "comment:123"],
        }
        value = dream._deferred_entry(task, "gemini_unavailable", "offline", "cycle")
        self.assertEqual(value["source_tasks"], ["#77"])
        self.assertEqual(value["source_fingerprints"], [task["source_fingerprint"]])
        self.assertEqual(value["evidence_refs"], task["source_refs"])


if __name__ == "__main__":
    unittest.main()