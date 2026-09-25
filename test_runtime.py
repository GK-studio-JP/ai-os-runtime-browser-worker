import hashlib
import json
import sys
import unittest

from driver_runner import run_driver
from runtime import gate, normalize, preflight, prepare


def _digest(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def capsule(
    through=10,
    source_fp="sha256:source",
    task_spec_fp="sha256:task-spec",
    state="open",
    event_count=0,
):
    value = {
        "schema": "ai-os-context-capsule:v1",
        "authoritative": False,
        "fingerprint": "cap-123",
        "source": {
            "canonical_through_comment_id": through,
            "through_comment_id": through,
            "task_spec_fingerprint": task_spec_fp,
            "source_fingerprint": source_fp,
        },
        "identity": {
            "process": "PROC-RUNTIME",
            "target_repository": "GK-studio-JP/ai-os-runtime",
        },
        "task": {
            "id": "#123",
            "objective": "exercise runtime",
            "state": state,
        },
        "replay": {"canonical_event_count": event_count},
    }
    value["content_digest"] = _digest(value)
    return value


def boot():
    dispatch = {
        "schema": "ai-os-dispatch:v1",
        "authoritative": False,
        "task": "#123",
        "title": "runtime task",
        "process": "PROC-RUNTIME",
        "target_repository": "GK-studio-JP/ai-os-runtime",
        "priority": 100,
        "capabilities": [],
        "next_action": "exercise runtime",
        "context": {"capsule": "capsules/issue-123.json", "fingerprint": "cap-123", "content_digest": capsule()["content_digest"], "through_comment_id": 10},
        "source": {"repository": "GK-studio-JP/ai-bulletin-board"},
    }
    return {
        "schema": "ai-os-worker-boot:v1",
        "authoritative": False,
        "persist_required": True,
        "source_plan_fingerprint": "sha256:plan",
        "dispatch_count": 1,
        "dispatch": dispatch,
        "capsule": {"path": "capsule.json", "fingerprint": "cap-123", "content_digest": capsule()["content_digest"], "through_comment_id": 10},
    }


def fresh(
    state="open",
    owner=None,
    through=10,
    safe=True,
    source_fp="sha256:source",
    task_spec_fp="sha256:task-spec",
    event_count=0,
    latest_owner_event=None,
):
    return {
        "task": "#123",
        "state": state,
        "history_safe": safe,
        "owner": owner,
        "canonical_through_comment_id": through,
        "through_comment_id": through,
        "canonical_event_count": event_count,
        "latest_owner_event": latest_owner_event,
        "task_spec_fingerprint": task_spec_fp,
        "source_fingerprint": source_fp,
    }


def worker_invocation():
    return {
        "schema": "ai-os-worker-invocation:v1",
        "authoritative": False,
        "persist_required": True,
        "fingerprint": "sha256:invocation",
        "worker_id": "worker-1",
    }


class RuntimeTests(unittest.TestCase):
    def test_open_task_requires_claim(self):
        p = preflight(boot(), capsule(), fresh(), worker_id="worker-1")
        self.assertEqual(p["status"], "CLAIM_REQUIRED")
        self.assertEqual(p["claim_proposal"]["event"]["type"], "CLAIM")
        self.assertEqual(p["claim_proposal"]["event"]["agent_id"], "worker-1")

    def test_stale_context_fails_closed(self):
        p = preflight(boot(), capsule(), fresh(through=11), worker_id="worker-1")
        self.assertEqual(p["status"], "STALE_CONTEXT")
        self.assertEqual(p["reason_code"], "canonical_history_changed")

    def test_issue_body_change_fails_closed(self):
        p = preflight(
            boot(),
            capsule(),
            fresh(source_fp="sha256:changed"),
            worker_id="worker-1",
        )
        self.assertEqual(p["status"], "STALE_CONTEXT")
        self.assertEqual(p["reason_code"], "canonical_source_changed")

    def test_matching_live_owner_is_ready(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1")
        self.assertEqual(p["status"], "READY")

    def test_claim_append_replay_reaches_ready_and_prepare(self):
        first = preflight(boot(), capsule(), fresh(), worker_id="worker-1")
        self.assertEqual(first["status"], "CLAIM_REQUIRED")

        after_claim = fresh(
            state="claimed",
            owner="worker-1",
            through=11,
            source_fp="sha256:source-after-claim",
            event_count=1,
            latest_owner_event={
                "type": "CLAIM",
                "agent_id": "worker-1",
                "ref": "comment:11",
            },
        )
        second = preflight(boot(), capsule(), after_claim, worker_id="worker-1")
        self.assertEqual(second["status"], "READY")
        self.assertEqual(second["reason_code"], "ownership_confirmed_after_claim")
        self.assertEqual(second["canonical_through_comment_id"], 11)
        self.assertEqual(
            second["canonical_source_fingerprint"],
            "sha256:source-after-claim",
        )

        invocation = prepare(boot(), capsule(), second, driver="manual")
        self.assertEqual(invocation["worker_id"], "worker-1")
        self.assertEqual(invocation["canonical_through_comment_id"], 11)

    def test_claim_transition_rejects_extra_canonical_history(self):
        after_claim_and_extra = fresh(
            state="claimed",
            owner="worker-1",
            through=12,
            source_fp="sha256:source-after-extra",
            event_count=2,
            latest_owner_event={
                "type": "CLAIM",
                "agent_id": "worker-1",
                "ref": "comment:12",
            },
        )
        p = preflight(
            boot(),
            capsule(),
            after_claim_and_extra,
            worker_id="worker-1",
        )
        self.assertEqual(p["status"], "STALE_CONTEXT")
        self.assertEqual(p["reason_code"], "canonical_history_changed")

    def test_claim_transition_rejects_task_spec_change(self):
        after_claim = fresh(
            state="claimed",
            owner="worker-1",
            through=11,
            source_fp="sha256:source-after-claim",
            task_spec_fp="sha256:changed-task-spec",
            event_count=1,
            latest_owner_event={
                "type": "CLAIM",
                "agent_id": "worker-1",
                "ref": "comment:11",
            },
        )
        p = preflight(boot(), capsule(), after_claim, worker_id="worker-1")
        self.assertEqual(p["status"], "STALE_CONTEXT")
        self.assertEqual(p["reason_code"], "task_spec_changed")

    def test_tampered_capsule_content_fails_closed(self):
        value = capsule()
        value["task"]["objective"] = "tampered"
        with self.assertRaisesRegex(ValueError, "content digest"):
            preflight(boot(), value, fresh(), worker_id="worker-1")

    def test_mismatched_capsule_digest_reference_fails_closed(self):
        value = boot()
        value["dispatch"]["context"]["content_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "digest reference"):
            preflight(value, capsule(), fresh(), worker_id="worker-1")

    def test_prepare_binds_invocation_to_worker_and_capsule(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1")
        inv = prepare(boot(), capsule(), p, driver="manual")
        self.assertEqual(inv["worker_id"], "worker-1")
        self.assertEqual(inv["input"]["capsule"]["fingerprint"], "cap-123")
        self.assertTrue(inv["fingerprint"].startswith("sha256:"))

    def test_completed_result_becomes_result_proposal(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1")
        inv = prepare(boot(), capsule(), p, driver="manual")
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": inv["fingerprint"],
            "worker_id": "worker-1",
            "status": "completed",
            "summary": "done",
            "next_action": None,
            "artifacts": ["commit:abc"],
            "requests": [],
        }
        out = normalize(inv, result)
        self.assertEqual(out["event_proposal"]["event"]["type"], "RESULT")
        self.assertIsNone(out["event_proposal"]["event"]["next_action"])

    def test_gate_requires_same_live_owner_and_history(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1")
        inv = prepare(boot(), capsule(), p, driver="manual")
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": inv["fingerprint"],
            "worker_id": "worker-1",
            "status": "progress",
            "summary": "checkpoint",
            "next_action": "continue",
            "artifacts": [],
            "requests": [],
        }
        out = normalize(inv, result)

        good = gate(boot(), fresh(state="claimed", owner="worker-1"), out)
        self.assertTrue(good["eligible_for_persistence"])

        stolen = gate(boot(), fresh(state="claimed", owner="worker-2"), out)
        self.assertFalse(stolen["eligible_for_persistence"])
        self.assertEqual(stolen["reason_code"], "ownership_changed")

        changed = gate(boot(), fresh(state="claimed", owner="worker-1", through=11), out)
        self.assertFalse(changed["eligible_for_persistence"])
        self.assertEqual(changed["reason_code"], "canonical_history_changed")

        source_changed = gate(
            boot(),
            fresh(state="claimed", owner="worker-1", source_fp="sha256:changed"),
            out,
        )
        self.assertFalse(source_changed["eligible_for_persistence"])
        self.assertEqual(source_changed["reason_code"], "canonical_source_changed")

    def test_subprocess_driver_round_trip(self):
        adapter = (
            "import json,sys;"
            "inv=json.load(sys.stdin);"
            "print(json.dumps({"
            "'schema':'ai-os-worker-result:v1',"
            "'invocation_fingerprint':inv['fingerprint'],"
            "'worker_id':inv['worker_id'],"
            "'status':'completed',"
            "'summary':'driver ok',"
            "'next_action':None,"
            "'artifacts':[],"
            "'requests':[]"
            "}))"
        )
        result = run_driver(
            worker_invocation(),
            [sys.executable, "-c", adapter],
            timeout_seconds=5,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "driver ok")

    def test_subprocess_driver_rejects_identity_mismatch(self):
        adapter = (
            "import json,sys;"
            "inv=json.load(sys.stdin);"
            "print(json.dumps({"
            "'schema':'ai-os-worker-result:v1',"
            "'invocation_fingerprint':inv['fingerprint'],"
            "'worker_id':'worker-2',"
            "'status':'completed',"
            "'summary':'wrong identity',"
            "'next_action':None,"
            "'artifacts':[],"
            "'requests':[]"
            "}))"
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            run_driver(
                worker_invocation(),
                [sys.executable, "-c", adapter],
                timeout_seconds=5,
            )

    def test_subprocess_driver_rejects_excessive_stdout(self):
        adapter = "import sys;sys.stdout.write('x' * 2048)"
        with self.assertRaisesRegex(ValueError, "stdout exceeds"):
            run_driver(
                worker_invocation(),
                [sys.executable, "-c", adapter],
                timeout_seconds=5,
                max_output_bytes=1024,
            )

    def test_subprocess_driver_does_not_echo_stderr(self):
        adapter = "import sys;sys.stderr.write('TOP_SECRET');raise SystemExit(7)"
        with self.assertRaises(RuntimeError) as ctx:
            run_driver(
                worker_invocation(),
                [sys.executable, "-c", adapter],
                timeout_seconds=5,
            )
        self.assertNotIn("TOP_SECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
