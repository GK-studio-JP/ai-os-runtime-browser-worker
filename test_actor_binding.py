import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from browser_worker_launcher import canonical_claim_present, canonical_result_present, ensure_canonical_lease, LauncherError
from test_browser_worker_launcher import canonical_comment


class ActorBindingTests(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.now = self.t0 + timedelta(minutes=2)
        self.claim = canonical_comment(1, self.t0, "CLAIM", "same-agent")

    def test_same_agent_different_trusted_actor_cannot_claim_or_complete(self):
        self.assertFalse(canonical_claim_present([self.claim], task="#1", agent_id="same-agent",
                                                actor_login="other-member", now=self.now))
        result = canonical_comment(2, self.t0 + timedelta(minutes=1), "RESULT", "same-agent")
        rows = [self.claim, result]
        self.assertTrue(canonical_result_present(rows, task="#1", agent_id="same-agent",
                                                 actor_login="repo-owner", now=self.now))
        self.assertFalse(canonical_result_present(rows, task="#1", agent_id="same-agent",
                                                  actor_login="other-member", now=self.now))
        result["user"]["login"] = "other-member"
        result["author_association"] = "MEMBER"
        self.assertFalse(canonical_result_present(rows, task="#1", agent_id="same-agent",
                                                  actor_login="other-member", now=self.now))

    def test_foreign_actor_lease_cannot_be_renewed(self):
        with patch("browser_worker_launcher.comments", return_value=[self.claim]), \
             patch("browser_worker_launcher.append_issue_comment") as append:
            with self.assertRaisesRegex(LauncherError, "ownership was lost"):
                ensure_canonical_lease(token=None, issue_no=1, issue_url="unused", relay=object(),
                                       task="#1", agent_id="same-agent", actor_login="other-member",
                                       phase="execution", now=self.now)
            append.assert_not_called()

    def test_missing_github_author_is_not_a_claim(self):
        del self.claim["user"]
        self.assertFalse(canonical_claim_present([self.claim], task="#1", agent_id="same-agent",
                                                actor_login="repo-owner", now=self.now))
