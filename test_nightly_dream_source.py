from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from ai_os_browser_worker.nightly_dream_source import (
    DreamSourceError,
    _validate_history,
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
    def test_github_json_allows_public_read_without_auth_header(self):
        observed = {}

        def fake_urlopen(request, timeout):
            observed["auth"] = request.get_header("Authorization")
            observed["timeout"] = timeout
            return Response({"ok": True})

        with patch("urllib.request.urlopen", fake_urlopen):
            value = github_json("https://api.github.com/repos/x/y", token=None)
        self.assertEqual(value, {"ok": True})
        self.assertIsNone(observed["auth"])
        self.assertEqual(observed["timeout"], 60)

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

    def test_validate_history_rejects_incomplete_comments(self):
        issue = {
            "number": 52,
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T01:00:00Z",
            "closed_at": None,
            "author_association": "OWNER",
            "body": "control",
            "comments": 2,
        }
        comment = {
            "id": 1,
            "created_at": "2026-10-02T00:30:00Z",
            "updated_at": "2026-10-02T00:30:00Z",
            "author_association": "OWNER",
            "body": "x",
            "user": {"login": "GK-studio-JP"},
        }
        with self.assertRaisesRegex(DreamSourceError, "declares 2 comments but 1"):
            _validate_history(issue, [comment])

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