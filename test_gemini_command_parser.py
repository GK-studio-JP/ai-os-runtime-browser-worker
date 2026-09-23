import unittest

from browser_worker_launcher import _commands


class GeminiCommandParsingTests(unittest.TestCase):
    def test_normal_json_launcher_command_is_unchanged(self):
        text = 'Gemini said {"kind":"wait","reason":"normal"}'
        self.assertEqual(
            _commands(text),
            [{"kind": "wait", "reason": "normal"}],
        )

    def test_one_layer_escaped_browser_action_is_accepted(self):
        text = (
            r'Gemini said { \"kind\": \"browser_action\", '
            r'\"action\": \"click\", '
            r'\"args\": { \"elementId\": \"g1-e2\" }, '
            r'\"reason\": \"continue\" }'
        )
        self.assertEqual(
            _commands(text),
            [
                {
                    "kind": "browser_action",
                    "action": "click",
                    "args": {"elementId": "g1-e2"},
                    "reason": "continue",
                }
            ],
        )

    def test_one_layer_escaped_payload_preserves_escapes_and_nested_braces(self):
        text = (
            r'Gemini said { \"kind\": \"browser_action\", '
            r'\"action\": \"fill\", '
            r'\"args\": { \"field\": \"file_contents\", '
            r'\"value\": \"line1\\nTARGET=\\\"quoted\\\"\\n'
            r'path=C:\\\\temp\\nexpr={{ outer: { inner: 1 } }}\" }, '
            r'\"reason\": \"edit\" }'
        )
        commands = _commands(text)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["kind"], "browser_action")
        self.assertEqual(commands[0]["action"], "fill")
        self.assertEqual(
            commands[0]["args"]["value"],
            'line1\nTARGET="quoted"\npath=C:\\temp\nexpr={{ outer: { inner: 1 } }}',
        )

    def test_mixed_normal_and_escaped_commands_preserve_page_order(self):
        text = (
            'Gemini said {"kind":"wait","reason":"first"} '
            r'Gemini said { \"kind\": \"finish\", \"summary\": \"second\" }'
        )
        self.assertEqual(
            _commands(text),
            [
                {"kind": "wait", "reason": "first"},
                {"kind": "finish", "summary": "second"},
            ],
        )

    def test_doubly_escaped_launcher_command_is_rejected(self):
        one_layer = r'{ \"kind\": \"wait\", \"reason\": \"one\" }'
        double_layer = one_layer.replace(r'\"', r'\\\"')
        self.assertEqual(_commands("Gemini said " + double_layer), [])

    def test_malformed_escaped_launcher_command_is_rejected(self):
        text = (
            r'Gemini said { \"kind\": \"browser_action\", '
            r'\"action\": \"fill\", \"args\": { \"value\": \"unterminated\" }'
        )
        self.assertEqual(_commands(text), [])


if __name__ == "__main__":
    unittest.main()
