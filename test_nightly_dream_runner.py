import unittest

import nightly_dream_runner as mod


class NightlyDreamRunnerTests(unittest.TestCase):
    def test_cycle_id_is_stable(self):
        first = mod.cycle_id(
            "2026-10-02T00:00:00+09:00",
            "2026-10-02T11:40:20+09:00",
        )
        second = mod.cycle_id(
            "2026-10-02T00:00:00+09:00",
            "2026-10-02T11:40:20+09:00",
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_extracts_run_metadata_from_fenced_json(self):
        issue = {
            "body": """text
```json
{
  "schema": "aios-dream-run:v1",
  "cycle_id": "sha256:abc",
  "window_start": "2026-10-02T00:00:00+09:00",
  "window_end": "2026-10-02T01:00:00+09:00"
}
```
"""
        }
        meta = mod._run_meta(issue)
        self.assertEqual(meta["cycle_id"], "sha256:abc")

    def test_cycle_state_parser_ignores_non_cycle_json(self):
        comments = [
            {"body": '```json\n{"schema":"other:v1"}\n```'},
            {
                "body": '```json\n'
                '{"schema":"aios-dream-cycle:v1","status":"completed","cycle_id":"x"}'
                '\n```'
            },
        ]
        states = mod._cycle_states(comments)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["cycle_id"], "x")

    def test_run_body_uses_dedicated_process(self):
        body = mod._run_body(
            {
                "schema": "aios-dream-run:v1",
                "cycle_id": "sha256:x",
                "window_start": "2026-10-02T00:00:00+09:00",
                "window_end": "2026-10-02T01:00:00+09:00",
            }
        )
        self.assertIn(mod.PROCESS, body)
        self.assertIn("gemini", body.lower())


if __name__ == "__main__":
    unittest.main()
