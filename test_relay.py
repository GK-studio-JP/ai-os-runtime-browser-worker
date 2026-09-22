import unittest
from unittest.mock import patch

from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.relay import Relay, _transient_relay_error


class ScriptedRelay(Relay):
    def __init__(self, responses):
        super().__init__("https://relay.test", "key", "session")
        self.responses = list(responses)
        self.calls = []

    def rest(self, method, path, value=None, prefer=None):
        self.calls.append((method, path, value, prefer))
        if method == "POST":
            return [{}]
        if not self.responses:
            raise AssertionError("unexpected relay poll")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RelayRetryTests(unittest.TestCase):
    @patch("ai_os_browser_worker.relay.time.sleep", return_value=None)
    def test_get_page_retries_transient_poll_failure(self, _sleep):
        relay = ScriptedRelay([
            LauncherError("HTTP failure: Supabase 502 Bad Gateway"),
            [{"status": "done", "result": {"generation": 9}}],
        ])

        result = relay.command("getPage", {})

        self.assertEqual(result, {"generation": 9})
        posts = [call for call in relay.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)

    @patch("ai_os_browser_worker.relay.time.sleep", return_value=None)
    def test_get_page_retries_transient_command_error(self, _sleep):
        relay = ScriptedRelay([
            [{"status": "error", "error": "Supabase 503 Service Unavailable"}],
            [{"status": "done", "result": {"url": "https://example.test"}}],
        ])

        result = relay.command("getPage", {})

        self.assertEqual(result["url"], "https://example.test")
        posts = [call for call in relay.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)

    def test_read_operation_timeout_is_transient(self):
        self.assertTrue(
            _transient_relay_error(
                "HTTP failure for GET relay: The read operation timed out"
            )
        )

    @patch("ai_os_browser_worker.relay.time.sleep", return_value=None)
    def test_command_with_receipt_retries_transient_poll_without_reposting(self, _sleep):
        relay = ScriptedRelay([
            LauncherError("HTTP failure for GET relay: The read operation timed out"),
            [{"status": "done", "result": {"generation": 9}}],
        ])

        result, receipt = relay.command_with_receipt(
            "fill",
            {"elementId": "g1-e1", "text": "x"},
            run_id="run-1",
            step=1,
        )

        self.assertEqual(result, {"generation": 9})
        self.assertEqual(receipt["status"], "success")
        posts = [call for call in relay.calls if call[0] == "POST"]
        gets = [call for call in relay.calls if call[0] == "GET"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(gets), 2)

    @patch("ai_os_browser_worker.relay.time.sleep", return_value=None)
    def test_mutating_fill_does_not_retry_transient_error(self, _sleep):
        relay = ScriptedRelay([
            [{"status": "error", "error": "Supabase 502 Bad Gateway"}],
        ])

        with self.assertRaisesRegex(LauncherError, "Browser Agent fill failed"):
            relay.command("fill", {"elementId": "g1-e1", "text": "x"})

        posts = [call for call in relay.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
