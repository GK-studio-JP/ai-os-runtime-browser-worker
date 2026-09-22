import unittest

from ai_os_browser_worker.navigation_policy import (
    LauncherError,
    _semantic_element_args,
)


class SemanticMutationTargetTests(unittest.TestCase):
    def setUp(self):
        self.new_file_page = {
            "generation": 7,
            "url": "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/new/main",
            "elements": [
                {"id": "g7-e10", "role": "textbox", "label": "File name"},
                {
                    "id": "g7-e20",
                    "role": "textbox",
                    "label": (
                        "Editing file contents Use Control + Shift + m to toggle "
                        "the tab key moving focus."
                    ),
                },
                {
                    "id": "g7-e21",
                    "role": "textbox",
                    "label": "Enter file contents here",
                },
                {
                    "id": "g7-e30",
                    "role": "radio",
                    "label": "Create a new branch for this commit and start a pull request",
                },
                {
                    "id": "g7-e31",
                    "role": "button",
                    "label": "Propose changes",
                    "text": "Propose changes",
                },
            ],
        }

    def test_resolves_explicit_file_name_field(self):
        args = _semantic_element_args(
            "fill",
            {"field": "file_name", "text": "runtime_contract_guard.py"},
            self.new_file_page,
        )
        self.assertEqual(
            args,
            {"elementId": "g7-e10", "text": "runtime_contract_guard.py"},
        )

    def test_resolves_explicit_file_contents_field(self):
        args = _semantic_element_args(
            "fill",
            {"field": "file_contents", "text": "print('guard')\n"},
            self.new_file_page,
        )
        self.assertEqual(
            args,
            {"elementId": "g7-e20", "text": "print('guard')\n"},
        )

    def test_infers_filename_from_new_file_fill(self):
        args = _semantic_element_args(
            "fill",
            {"text": "runtime_contract_guard.py"},
            self.new_file_page,
        )
        self.assertEqual(args["elementId"], "g7-e10")

    def test_infers_source_contents_after_filename(self):
        args = _semantic_element_args(
            "fill",
            {"text": "from __future__ import annotations\n\ndef check():\n    return True\n"},
            self.new_file_page,
        )
        self.assertEqual(args["elementId"], "g7-e20")

    def test_resolves_exact_mutation_control_label(self):
        args = _semantic_element_args(
            "click",
            {"label": "Propose changes"},
            self.new_file_page,
        )
        self.assertEqual(args, {"elementId": "g7-e31"})

    def test_rejects_missing_click_target(self):
        with self.assertRaisesRegex(
            LauncherError, "without elementId requires label or target"
        ):
            _semantic_element_args("click", {}, self.new_file_page)

    def test_rejects_ambiguous_exact_label(self):
        page = dict(self.new_file_page)
        page["elements"] = [
            *self.new_file_page["elements"],
            {
                "id": "g7-e32",
                "role": "button",
                "label": "Propose changes",
                "text": "Propose changes",
            },
        ]
        with self.assertRaisesRegex(LauncherError, "resolved to 2"):
            _semantic_element_args(
                "click",
                {"target": "Propose changes"},
                page,
            )


if __name__ == "__main__":
    unittest.main()
