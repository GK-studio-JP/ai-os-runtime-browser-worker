import sys
import unittest

from driver_runner import run_driver
from execution_budget import (
    BUDGET_SCHEMA,
    USAGE_SCHEMA,
    normalize_execution_budget,
    validate_execution_usage,
)
from runtime import normalize, preflight, prepare
from runtime_middleware import MiddlewareBlocked
from test_runtime import boot, capsule, fresh, worker_invocation


def budget_boot(**limits):
    value = {
        "schema": BUDGET_SCHEMA,
        "authoritative": False,
        **limits,
    }
    value_boot = boot()
    value_boot["dispatch"]["execution_budget"] = value
    return value_boot


def ready_invocation(value_boot):
    state = fresh(state="claimed", owner="worker-1")
    check = preflight(value_boot, capsule(), state, worker_id="worker-1")
    return prepare(value_boot, capsule(), check, driver="manual")


def adapter_with_usage(steps):
    return (
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
        "'requests':[],"
        "'execution_usage':{"
        "'schema':'ai-os-execution-usage:v1',"
        "'authoritative':False,"
        f"'steps':{steps}"
        "}"
        "}))"
    )


class ExecutionBudgetTests(unittest.TestCase):
    def test_budget_schema_is_non_authoritative_and_bounded(self):
        value = normalize_execution_budget(
            {
                "schema": BUDGET_SCHEMA,
                "authoritative": False,
                "deadline_seconds": 30,
                "max_steps": 10,
            }
        )
        self.assertFalse(value["authoritative"])
        self.assertEqual(value["deadline_seconds"], 30)
        self.assertEqual(value["max_steps"], 10)
        self.assertIsNone(value["max_tool_calls"])
        self.assertIsNone(value["max_tokens"])

        with self.assertRaisesRegex(ValueError, "non-authoritative"):
            normalize_execution_budget(
                {
                    "schema": BUDGET_SCHEMA,
                    "authoritative": True,
                    "deadline_seconds": 30,
                }
            )
        with self.assertRaisesRegex(ValueError, "at least one limit"):
            normalize_execution_budget(
                {
                    "schema": BUDGET_SCHEMA,
                    "authoritative": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            normalize_execution_budget(
                {
                    "schema": BUDGET_SCHEMA,
                    "authoritative": False,
                    "max_steps": 0,
                }
            )

    def test_usage_schema_is_non_authoritative(self):
        usage = validate_execution_usage(
            {
                "schema": USAGE_SCHEMA,
                "authoritative": False,
                "steps": 0,
                "tool_calls": 2,
            }
        )
        self.assertEqual(usage["steps"], 0)
        self.assertEqual(usage["tool_calls"], 2)
        self.assertIsNone(usage["tokens"])

        with self.assertRaisesRegex(ValueError, "non-authoritative"):
            validate_execution_usage(
                {
                    "schema": USAGE_SCHEMA,
                    "authoritative": True,
                    "steps": 1,
                }
            )

    def test_prepare_is_backward_compatible_without_budget(self):
        value_boot = boot()
        state = fresh(state="claimed", owner="worker-1")
        check = preflight(value_boot, capsule(), state, worker_id="worker-1")
        invocation = prepare(value_boot, capsule(), check, driver="manual")

        self.assertNotIn("execution_budget", invocation)
        self.assertNotIn(
            "execution_usage",
            invocation["result_contract"]["required"],
        )

    def test_prepare_carries_budget_and_requires_counter_usage(self):
        invocation = ready_invocation(
            budget_boot(
                deadline_seconds=30,
                max_steps=10,
                max_tool_calls=5,
            )
        )

        self.assertFalse(invocation["execution_budget"]["authoritative"])
        self.assertEqual(invocation["execution_budget"]["max_steps"], 10)
        self.assertIn(
            "execution_usage",
            invocation["result_contract"]["required"],
        )

    def test_deadline_only_budget_does_not_require_usage(self):
        invocation = ready_invocation(budget_boot(deadline_seconds=30))
        self.assertNotIn(
            "execution_usage",
            invocation["result_contract"]["required"],
        )

    def test_driver_deadline_fails_closed(self):
        invocation = {
            **worker_invocation(),
            "execution_budget": {
                "schema": BUDGET_SCHEMA,
                "authoritative": False,
                "deadline_seconds": 1,
            },
        }
        adapter = "import time;time.sleep(2)"
        with self.assertRaises(MiddlewareBlocked) as ctx:
            run_driver(
                invocation,
                [sys.executable, "-c", adapter],
                timeout_seconds=5,
            )
        self.assertEqual(ctx.exception.middleware, "execution-budget")
        self.assertEqual(ctx.exception.phase, "compute")
        self.assertIn("deadline_seconds", ctx.exception.reason)

    def test_driver_requires_usage_for_active_counter_budget(self):
        invocation = {
            **worker_invocation(),
            "execution_budget": {
                "schema": BUDGET_SCHEMA,
                "authoritative": False,
                "max_steps": 2,
            },
        }
        adapter = (
            "import json,sys;"
            "inv=json.load(sys.stdin);"
            "print(json.dumps({"
            "'schema':'ai-os-worker-result:v1',"
            "'invocation_fingerprint':inv['fingerprint'],"
            "'worker_id':inv['worker_id']"
            "}))"
        )
        with self.assertRaises(MiddlewareBlocked) as ctx:
            run_driver(
                invocation,
                [sys.executable, "-c", adapter],
                timeout_seconds=5,
            )
        self.assertEqual(ctx.exception.phase, "after_compute")
        self.assertIn("execution_usage", ctx.exception.reason)

    def test_driver_accepts_counter_at_limit(self):
        invocation = {
            **worker_invocation(),
            "execution_budget": {
                "schema": BUDGET_SCHEMA,
                "authoritative": False,
                "max_steps": 2,
            },
        }
        result = run_driver(
            invocation,
            [sys.executable, "-c", adapter_with_usage(2)],
            timeout_seconds=5,
        )
        self.assertEqual(result["execution_usage"]["steps"], 2)

    def test_driver_rejects_counter_over_limit(self):
        invocation = {
            **worker_invocation(),
            "execution_budget": {
                "schema": BUDGET_SCHEMA,
                "authoritative": False,
                "max_steps": 2,
            },
        }
        with self.assertRaises(MiddlewareBlocked) as ctx:
            run_driver(
                invocation,
                [sys.executable, "-c", adapter_with_usage(3)],
                timeout_seconds=5,
            )
        self.assertIn("execution budget exceeded", ctx.exception.reason)

    def test_normalize_cannot_bypass_counter_budget(self):
        invocation = ready_invocation(budget_boot(max_steps=2))
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": invocation["fingerprint"],
            "worker_id": "worker-1",
            "status": "completed",
            "summary": "done",
            "next_action": None,
            "artifacts": [],
            "requests": [],
            "execution_usage": {
                "schema": USAGE_SCHEMA,
                "authoritative": False,
                "steps": 3,
            },
        }
        with self.assertRaisesRegex(ValueError, "execution budget exceeded"):
            normalize(invocation, result)

    def test_outcome_carries_non_authoritative_usage_but_event_does_not(self):
        invocation = ready_invocation(budget_boot(max_steps=2))
        result = {
            "schema": "ai-os-worker-result:v1",
            "invocation_fingerprint": invocation["fingerprint"],
            "worker_id": "worker-1",
            "status": "completed",
            "summary": "done",
            "next_action": None,
            "artifacts": [],
            "requests": [],
            "execution_usage": {
                "schema": USAGE_SCHEMA,
                "authoritative": False,
                "steps": 2,
            },
        }
        outcome = normalize(invocation, result)

        self.assertFalse(outcome["execution_budget"]["authoritative"])
        self.assertFalse(outcome["execution_usage"]["authoritative"])
        event = outcome["event_proposal"]["event"]
        self.assertNotIn("execution_budget", event)
        self.assertNotIn("execution_usage", event)


if __name__ == "__main__":
    unittest.main()
