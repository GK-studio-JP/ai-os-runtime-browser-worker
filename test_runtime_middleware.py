import sys
import unittest

from driver_runner import run_driver
from runtime_middleware import MiddlewareBlocked, MiddlewareChain, RuntimeMiddleware


def invocation():
    return {
        "schema": "ai-os-worker-invocation:v1",
        "authoritative": False,
        "persist_required": True,
        "fingerprint": "sha256:invocation",
        "worker_id": "worker-1",
    }


def successful_adapter():
    return (
        "import json,sys;"
        "inv=json.load(sys.stdin);"
        "print(json.dumps({"
        "'schema':'ai-os-worker-result:v1',"
        "'invocation_fingerprint':inv['fingerprint'],"
        "'worker_id':inv['worker_id'],"
        "'status':'completed',"
        "'summary':'ok'"
        "}))"
    )


class RecordingMiddleware(RuntimeMiddleware):
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def before_compute(self, invocation):
        self.events.append(f"before:{self.name}")
        invocation["worker_id"] = "mutated-snapshot"

    def after_compute(self, invocation, result):
        self.events.append(f"after:{self.name}")
        invocation["worker_id"] = "mutated-snapshot"
        result["summary"] = "mutated-snapshot"


class BlockingMiddleware(RuntimeMiddleware):
    name = "blocker"

    def before_compute(self, invocation):
        raise MiddlewareBlocked(self.name, "before_compute", "policy denied execution")


class RuntimeMiddlewareTests(unittest.TestCase):
    def test_chain_order_and_snapshot_mutation_isolation(self):
        events = []
        inv = invocation()
        chain = MiddlewareChain(
            [
                RecordingMiddleware("first", events),
                RecordingMiddleware("second", events),
            ]
        )

        result = run_driver(
            inv,
            [sys.executable, "-c", successful_adapter()],
            timeout_seconds=5,
            middleware=chain,
        )

        self.assertEqual(
            events,
            [
                "before:first",
                "before:second",
                "after:second",
                "after:first",
            ],
        )
        self.assertEqual(inv["worker_id"], "worker-1")
        self.assertEqual(result["worker_id"], "worker-1")
        self.assertEqual(result["summary"], "ok")

    def test_block_happens_before_subprocess_execution(self):
        chain = MiddlewareChain([BlockingMiddleware()])

        with self.assertRaisesRegex(MiddlewareBlocked, "policy denied execution"):
            run_driver(
                invocation(),
                [sys.executable, "-c", "raise SystemExit(99)"],
                timeout_seconds=5,
                middleware=chain,
            )

    def test_unexpected_middleware_error_fails_closed(self):
        class BrokenMiddleware(RuntimeMiddleware):
            name = "broken"

            def before_compute(self, invocation):
                raise ValueError("boom")

        with self.assertRaisesRegex(RuntimeError, "broken"):
            run_driver(
                invocation(),
                [sys.executable, "-c", successful_adapter()],
                timeout_seconds=5,
                middleware=MiddlewareChain([BrokenMiddleware()]),
            )


if __name__ == "__main__":
    unittest.main()
