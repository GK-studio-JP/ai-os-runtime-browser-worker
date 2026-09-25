import base64
import unittest
from unittest.mock import patch

from browser_worker_launcher import (
    EDITOR_CONTENT_MAX_CHARS,
    LauncherError,
    _enrich_authoritative_editor_source,
    _github_edit_target,
    _require_complete_editor_observation,
    _require_stable_editor_source,
    reduce_observation,
)


def editor_page(
    text: str,
    *,
    value: str | None = None,
    authoritative: bool = True,
    url: str = (
        "https://github.com/GK-studio-JP/ai-os-context/"
        "edit/main/.github/workflows/snapshot.yml"
    ),
) -> dict:
    editor = {
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
    if authoritative:
        editor["editorSourceKind"] = "github_contents_api"
        editor["editorSourceSha"] = "a" * 40
    return {
        "url": url,
        "generation": 80,
        "pageText": "Editing snapshot.yml file contents",
        "elements": [editor],
    }


def enrichment_page() -> dict:
    page = editor_page(
        "name: projection snapshot on: workflow_dispatch:",
        value="partial decorated DOM source",
        authoritative=False,
    )
    page["elements"].append(
        {
            "id": "g80-e90",
            "role": "button",
            "label": "main branch",
            "text": "main",
        }
    )
    return page


class EditorSourceFidelityTests(unittest.TestCase):
    def test_complete_authoritative_editor_value_survives_reduction(self):
        source = "x" * 3074
        page = editor_page("x" * 300, value=source)
        reduced = reduce_observation(page)
        editor = reduced["elements"][0]

        self.assertEqual(editor["value"], source)
        self.assertEqual(editor["editorSourceKind"], "github_contents_api")
        self.assertNotIn("text", editor)
        self.assertNotIn("editorContentTruncated", editor)
        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "replacement"},
            page,
            reduced,
        )

    def test_blank_authoritative_editor_value_is_preserved(self):
        page = editor_page("lossy fallback", value="")
        reduced = reduce_observation(page)
        editor = reduced["elements"][0]

        self.assertEqual(editor["value"], "")
        self.assertNotIn("text", editor)
        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "replacement"},
            page,
            reduced,
        )

    def test_missing_exact_editor_value_is_denied_fail_closed(self):
        page = editor_page("lossy 300-character-style text", value=None)
        reduced = reduce_observation(page)

        with self.assertRaisesRegex(
            LauncherError,
            "file editor exact source is unavailable",
        ):
            _require_complete_editor_observation(
                "fill",
                {"elementId": "g80-e183", "text": "replacement"},
                page,
                reduced,
            )

    def test_dom_only_existing_editor_value_is_not_authoritative(self):
        page = editor_page(
            "lossy text",
            value="partial decorated contenteditable source",
            authoritative=False,
        )
        reduced = reduce_observation(page)

        with self.assertRaisesRegex(
            LauncherError,
            "file editor authoritative source is unavailable",
        ):
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

    def test_contents_api_enrichment_overrides_virtualized_dom_source(self):
        source = (
            "name: projection snapshot\n\n"
            "on:\n"
            "  workflow_dispatch:\n"
            "jobs:\n"
            "  snapshot:\n"
            "    runs-on: ubuntu-latest\n"
        )
        response = {
            "type": "file",
            "encoding": "base64",
            "path": ".github/workflows/snapshot.yml",
            "size": len(source.encode("utf-8")),
            "sha": "b" * 40,
            "content": base64.b64encode(source.encode("utf-8")).decode("ascii"),
        }
        with patch(
            "browser_worker_launcher.github",
            return_value=response,
        ) as github_mock:
            enriched = _enrich_authoritative_editor_source(
                enrichment_page(),
                token="token",
                repository="GK-studio-JP/ai-os-context",
            )

        editor = enriched["elements"][0]
        self.assertEqual(editor["value"], source)
        self.assertEqual(editor["editorSourceKind"], "github_contents_api")
        self.assertEqual(editor["editorSourceSha"], "b" * 40)
        self.assertEqual(editor["editorSourceRef"], "main")
        self.assertEqual(
            editor["editorSourcePath"],
            ".github/workflows/snapshot.yml",
        )
        github_mock.assert_called_once_with(
            "/repos/GK-studio-JP/ai-os-context/contents/"
            ".github/workflows/snapshot.yml?ref=main",
            token="token",
        )

        reduced = reduce_observation(enriched)
        self.assertEqual(reduced["elements"][0]["value"], source)
        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "replacement"},
            enriched,
            reduced,
        )

    def test_contents_api_enrichment_does_not_cross_task_repository(self):
        page = enrichment_page()
        page["url"] = (
            "https://github.com/GK-studio-JP/other-repo/"
            "edit/main/.github/workflows/snapshot.yml"
        )
        with patch("browser_worker_launcher.github") as github_mock:
            enriched = _enrich_authoritative_editor_source(
                page,
                token="token",
                repository="GK-studio-JP/ai-os-context",
            )

        self.assertIs(enriched, page)
        github_mock.assert_not_called()

    def test_contents_api_enrichment_requires_verified_ref_label(self):
        page = enrichment_page()
        page["elements"][-1]["label"] = "other branch"
        with self.assertRaisesRegex(
            LauncherError,
            "GitHub edit ref could not be verified",
        ):
            _enrich_authoritative_editor_source(
                page,
                token="token",
                repository="GK-studio-JP/ai-os-context",
            )

    def test_contents_api_enrichment_denies_oversized_file(self):
        response = {
            "type": "file",
            "encoding": "base64",
            "path": ".github/workflows/snapshot.yml",
            "size": EDITOR_CONTENT_MAX_CHARS + 1,
            "sha": "b" * 40,
            "content": "",
        }
        with patch(
            "browser_worker_launcher.github",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                LauncherError,
                "exceeds the exact-source Browser Worker limit",
            ):
                _enrich_authoritative_editor_source(
                    enrichment_page(),
                    token="token",
                    repository="GK-studio-JP/ai-os-context",
                )

    def test_authoritative_blob_sha_must_stay_stable_before_fill(self):
        original = editor_page("lossy", value="source")
        fresh = editor_page("lossy", value="source")
        fresh["generation"] = 81
        fresh["elements"][0]["id"] = "g81-e183"
        fresh["elements"][0]["editorSourceSha"] = "c" * 40

        with self.assertRaisesRegex(
            LauncherError,
            "authoritative source changed before mutation",
        ):
            _require_stable_editor_source(
                "fill",
                {"elementId": "g80-e183"},
                original,
                {"elementId": "g81-e183"},
                fresh,
            )

    def test_new_file_blank_editor_does_not_require_contents_api_source(self):
        page = editor_page(
            "lossy fallback",
            value="",
            authoritative=False,
            url="https://github.com/GK-studio-JP/ai-os-context/new/main",
        )
        reduced = reduce_observation(page)

        _require_complete_editor_observation(
            "fill",
            {"elementId": "g80-e183", "text": "new source"},
            page,
            reduced,
        )

    def test_unsafe_encoded_edit_path_is_denied(self):
        with self.assertRaisesRegex(
            LauncherError,
            "unsupported path segments",
        ):
            _github_edit_target(
                "https://github.com/GK-studio-JP/ai-os-context/"
                "edit/main/.github/%2E%2E/secret.yml"
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
