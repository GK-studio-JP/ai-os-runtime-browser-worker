import unittest

from browser_worker_launcher import (
    EDITOR_CONTENT_MAX_CHARS,
    LauncherError,
    _require_complete_editor_observation,
    reduce_observation,
)


def editor_page(text: str) -> dict:
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

    def test_oversized_editor_source_is_denied_fail_closed(self):
        source = "z" * (EDITOR_CONTENT_MAX_CHARS + 1)
        page = editor_page(source)
        reduced = reduce_observation(page)

        self.assertEqual(
            len(reduced["elements"][0]["text"]),
            EDITOR_CONTENT_MAX_CHARS,
        )
        self.assertTrue(reduced["elements"][0]["editorContentTruncated"])
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
