from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from ai_os_browser_worker.nightly_dream_source import (
    DreamSourceError,
    github_json,
    history_tuples,
)


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class NightlyDreamSourceTests(unittest.TestCase):
    def test_github_json_requires_token(self):
        with self.assertRaises(DreamSourceError):
            github_json("https://api.github.com/repos/x/y", token="")

    def test_github_json_sends_bearer_auth(self):
        observed = {}

        def fake_urlopen(request, timeout):
            observed["auth"] = request.get_header("Authorization")
            observed["timeout"] = timeout
            return Response({"ok": True})

        with patch("urllib.request.urlopen", fake_urlopen):
            value = github_json("https://api.github.com/repos/x/y", token="secret")
        self.assertEqual(value, {"ok": True})
        self.assertEqual(observed["auth"], "Bearer secret")
        self.assertEqual(observed["timeout"], 60)

    def test_history_tuples_rejects_duplicate_issue(self):
        issue = {"number": 1}
        snapshot = {
            "schema": "aios-dream-histories:v1",
            "repository": "GK-studio-JP/ai-bulletin-board",
            "histories": [
                {"issue": issue, "comments": []},
                {"issue": issue, "comments": []},
            ],
        }
        with self.assertRaises(DreamSourceError):
            history_tuples(snapshot)


if __name__ == "__main__":
    unittest.main()