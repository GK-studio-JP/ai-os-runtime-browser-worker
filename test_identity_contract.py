import unittest

from runtime import gate, normalize, preflight, prepare
from driver_runner import validate_invocation
from test_runtime import boot, capsule, fresh, worker_invocation


class IdentityContractTests(unittest.TestCase):
    def test_same_agent_different_actor_and_missing_actor_fail_closed(self):
        state = fresh(state="claimed", owner="worker-1")
        for actor in ("other-member", None, ""):
            with self.subTest(actor=actor):
                state["owner_actor"] = actor
                result = preflight(boot(), capsule(), state, worker_id="worker-1", worker_actor="repo-owner")
                self.assertNotEqual(result["status"], "READY")
        with self.assertRaises(ValueError):
            preflight(boot(), capsule(), state, worker_id="worker-1", worker_actor="")

    def test_actor_bound_from_preflight_through_postflight(self):
        state = fresh(state="claimed", owner="worker-1")
        check = preflight(boot(), capsule(), state, worker_id="worker-1", worker_actor="repo-owner")
        inv = prepare(boot(), capsule(), check, driver="manual")
        self.assertEqual(inv["worker_actor"], "repo-owner")
        result = dict(schema="ai-os-worker-result:v1", invocation_fingerprint=inv["fingerprint"],
                      worker_id="worker-1", worker_actor="repo-owner", status="completed",
                      summary="done", next_action=None, artifacts=[], requests=[])
        outcome = normalize(inv, result)
        self.assertTrue(gate(boot(), state, outcome)["eligible_for_persistence"])
        for actor in ("other-member", None, ""):
            with self.subTest(actor=actor):
                changed = {**state, "owner_actor": actor}
                self.assertFalse(gate(boot(), changed, outcome)["eligible_for_persistence"])
                with self.assertRaisesRegex(ValueError, "identity"):
                    normalize(inv, {**result, "worker_actor": actor})
        legacy = dict(outcome)
        del legacy["worker_actor"]
        self.assertFalse(gate(boot(), state, legacy)["eligible_for_persistence"])

    def test_driver_rejects_legacy_invocation_before_execution(self):
        inv = worker_invocation()
        del inv["worker_actor"]
        with self.assertRaisesRegex(ValueError, "worker_actor"):
            validate_invocation(inv)
