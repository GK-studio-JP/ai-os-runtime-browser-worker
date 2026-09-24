import unittest

from browser_worker_launcher import (
    EDITOR_CONTENT_MAX_CHARS,
    LauncherError,
    _require_complete_editor_observation,
    reduce_observation,
)


def editor_page(text: str, *, value: str | None = None) -> dict:
    return {
        "url": (
            "https://github.com/GK-studio-JP/ai-os-context/"
            "edit/main/.github/workflows/snapshot.yml"
        ),
        "generation": 80,
        "pageText": "Editing snapshot.yml file contents",
        "elements": [
            {
                "id": "g80-e183",
                "role": "textbox",
                "label": (
                    "Editing snapshot.yml file contents "
                    "Use Control + Shift + m to toggle the tab key moving focus."
                ),
                "editable": True,
                "text": text,
                "value": value,
            }
        ],
    }


class EditorSourceFidelityTests(unittest.TestCase):
    def test_complete_editor_source_survives_reduction(self):
        source = "x" * 3074
        page = editor_page(source)
        reduced = reduce_observation(page)

        self.assertEqual(reduced["elements"][0]["text"], source)
        self.assertNotIn("editorContentTruncated", reduced["elements"][0])
        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "replacement"},
            page,
            reduced,
        )

    def test_exact_multiline_editor_value_wins_over_lossy_text(self):
        source = (
            "name: projection snapshot\n"
            "on:\n"
            "  workflow_dispatch:\n"
            "jobs:\n"
            "  snapshot:\n"
            "    runs-on: ubuntu-latest\n"
        )
        page = editor_page(
            "name: projection snapshot on: workflow_dispatch: jobs: snapshot:",
            value=source,
        )
        reduced = reduce_observation(page)
        editor = reduced["elements"][0]

        self.assertEqual(editor["value"], source)
        self.assertNotIn("text", editor)
        self.assertNotIn("editorContentTruncated", editor)
        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "replacement"},
            page,
            reduced,
        )

    def test_ordinary_long_textbox_still_truncates_to_120(self):
        page = {
            "generation": 8,
            "pageText": "notes",
            "elements": [
                {
                    "id": "g8-e1",
                    "role": "textbox",
                    "label": "Notes",
                    "text": "a" * 500,
                }
            ],
        }

        reduced = reduce_observation(page)
        self.assertEqual(reduced["elements"][0]["text"], "a" * 120)

    def test_oversized_editor_value_sentinel_is_denied_fail_closed(self):
        source = "z" * (EDITOR_CONTENT_MAX_CHARS + 1)
        page = editor_page("z" * 300, value=source)
        reduced = reduce_observation(page)
        editor = reduced["elements"][0]

        self.assertEqual(len(editor["value"]), EDITOR_CONTENT_MAX_CHARS)
        self.assertNotIn("text", editor)
        self.assertTrue(editor["editorContentTruncated"])
        with self.assertRaisesRegex(
            LauncherError,
            "file editor source is incomplete in model observation",
        ):
            _require_complete_editor_observation(
                "fill",
                {"elementId": "g80-e183", "text": "replacement"},
                page,
                reduced,
            )

    def test_non_editor_fill_is_unaffected(self):
        page = {
            "generation": 8,
            "elements": [
                {
                    "id": "g8-e1",
                    "role": "textbox",
                    "label": "Commit message",
                    "text": "old",
                }
            ],
        }
        reduced = reduce_observation(page)

        _require_complete_editor_observation(
            "fill",
            {"elementId": "g8-e1", "text": "new"},
            page,
            reduced,
        )


if __name__ == "__main__":
    unittest.main()
