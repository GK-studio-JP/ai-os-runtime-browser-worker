from __future__ import annotations

import unittest

from nightly_dream_runner import (
    DreamBootstrapError,
    dispatch_fixed_workflow,
    request_dispatch,
)


class FakeRelay:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.calls = []

    def command(self, action, args=None, timeout=None):
        self.calls.append((action, args or {}))
        if action == "start":
            return {"url": "https://gemini.google.com/app"}
        if action == "getPage":
            if not self.pages:
                raise AssertionError("unexpected getPage")
            return self.pages.pop(0)
        return {"ok": True}


class NightlyDreamBootstrapTests(unittest.TestCase):
    def test_request_dispatch_requires_exact_gemini_decision(self):
        relay = FakeRelay()
        result = request_dispatch(
            relay,
            ask_model=lambda *_args, **_kwargs: {
                "kind": "finish",
                "action": "dispatch_nightly_dream",
                "reason": "scheduled trigger accepted",
            },
        )
        self.assertEqual(result["action"], "dispatch_nightly_dream")

    def test_request_dispatch_rejects_other_action(self):
        relay = FakeRelay()
        with self.assertRaises(DreamBootstrapError):
            request_dispatch(
                relay,
                ask_model=lambda *_args, **_kwargs: {
                    "kind": "finish",
                    "action": "write_github_directly",
                },
            )

    def test_dispatch_uses_fixed_workflow_and_verifies(self):
        relay = FakeRelay(
            pages=[
                {
                    "elements": [
                        {"id": "g1-e1", "role": "button", "text": "Run workflow"}
                    ]
                },
                {
                    "elements": [
                        {"id": "g2-e1", "role": "button", "text": "Run workflow"},
                        {"id": "g2-e2", "role": "button", "text": "Run workflow"},
                    ]
                },
                {
                    "pageText": "Workflow run was successfully requested.",
                    "elements": [],
                },
            ]
        )
        dispatch_fixed_workflow(relay)
        actions = [name for name, _ in relay.calls]
        self.assertEqual(
            actions,
            ["goto", "getPage", "click", "getPage", "click", "getPage"],
        )
        self.assertEqual(relay.calls[2][1]["elementId"], "g1-e1")
        self.assertEqual(relay.calls[4][1]["elementId"], "g2-e2")


if __name__ == "__main__":
    unittest.main()
