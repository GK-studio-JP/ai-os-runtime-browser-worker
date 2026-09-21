import sys
import unittest

from driver_runner import run_driver
from runtime import gate, normalize, preflight, prepare


def capsule(through=10):
    return {
        "schema": "ai-os-context-capsule:v1",
        "authoritative": False,
        "fingerprint": "cap-123",
        "source": {"through_comment_id": through},
        "identity": {"process": "PROC-RUNTIME", "target_repository": "GK-studio-JP/ai-os-runtime"},
        "task": {"id": "#123", "objective": "exercise runtime"},
    }


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
        "context": {"capsule": "capsules/issue-123.json", "fingerprint": "cap-123", "through_comment_id": 10},
        "source": {"repository": "GK-studio-JP/ai-bulletin-board"},
    }
    return {
        "schema": "ai-os-worker-boot:v1",
        "authoritative": False,
        "persist_required": True,
        "source_plan_fingerprint": "sha256:plan",
        "dispatch_count": 1,
        "dispatch": dispatch,
        "capsule": {"path": "capsule.json", "fingerprint": "cap-123", "through_comment_id": 10},
    }


def fresh(state="open", owner=None, through=10, safe=True):
    return {
        "task": "#123",
        "state": state,
        "history_safe": safe,
        "owner": owner,
        "owner_actor": "repo-owner" if owner else None,
        "through_comment_id": through,
    }


def worker_invocation():
    return {
        "schema": "ai-os-worker-invocation:v1",
        "authoritative": False,
        "persist_required": True,
        "fingerprint": "sha256:invocation",
        "worker_id": "worker-1",
        "worker_actor": "repo-owner",
    }


class RuntimeTests(unittest.TestCase):
    def test_open_task_requires_claim(self):
        p = preflight(boot(), capsule(), fresh(), worker_id="worker-1", worker_actor="repo-owner")
        self.assertEqual(p["status"], "CLAIM_REQUIRED")
        self.assertEqual(p["claim_proposal"]["event"]["type"], "CLAIM")
        self.assertEqual(p["claim_proposal"]["event"]["agent_id"], "worker-1")

    def test_stale_context_fails_closed(self):
        p = preflight(boot(), capsule(), fresh(through=11), worker_id="worker-1", worker_actor="repo-owner")
        self.assertEqual(p["status"], "STALE_CONTEXT")
        self.assertEqual(p["reason_code"], "canonical_history_changed")

    def test_matching_live_owner_is_ready(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1", worker_actor="repo-owner")
        self.assertEqual(p["status"], "READY")

    def test_prepare_binds_invocation_to_worker_and_capsule(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1", worker_actor="repo-owner")
        inv = prepare(boot(), capsule(), p, driver="manual")
        self.assertEqual(inv["worker_id"], "worker-1")
        self.assertEqual(inv["input"]["capsule"]["fingerprint"], "cap-123")
        self.assertTrue(inv["fingerprint"].startswith("sha256:"))

    def test_completed_result_becomes_result_proposal(self):
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1", worker_actor="repo-owner")
        inv = prepare(boot(), capsule(), p, driver="manual")
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": inv["fingerprint"],
            "worker_id": "worker-1",
            "worker_actor": "repo-owner",
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
        p = preflight(boot(), capsule(), fresh(state="claimed", owner="worker-1"), worker_id="worker-1", worker_actor="repo-owner")
        inv = prepare(boot(), capsule(), p, driver="manual")
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": inv["fingerprint"],
            "worker_id": "worker-1",
            "worker_actor": "repo-owner",
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

    def test_subprocess_driver_round_trip(self):
        adapter = (
            "import json,sys;"
            "inv=json.load(sys.stdin);"
            "print(json.dumps({"
            "'schema':'ai-os-worker-result:v1',"
            "'invocation_fingerprint':inv['fingerprint'],"
            "'worker_id':inv['worker_id'],"
            "'worker_actor':inv['worker_actor'],"
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
            "'worker_actor':inv['worker_actor'],"
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
