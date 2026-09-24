import unittest

from ai_os_browser_worker.navigation_policy import LauncherError, _validate_model_action
from browser_worker_launcher import reduce_observation


class FullPageValidationTests(unittest.TestCase):
    def test_full_page_resolves_commit_button_omitted_by_reduction(self):
        full_page = {
            "url": (
                "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/"
                "edit/main/browser_worker_launcher.py"
            ),
            "generation": 270,
            "pageText": "Commit changes...",
            "elements": [
                *[
                    {
                        "id": f"g270-e{index}",
                        "role": "textbox",
                        "label": f"token-{index}",
                    }
                    for index in range(1, 16)
                ],
                {
                    "id": "g270-e187",
                    "role": "button",
                    "label": "Commit changes...",
                    "text": "Commit changes...",
                },
            ],
        }
        reduced = reduce_observation(full_page, max_elements=14)
        self.assertFalse(
            any(
                element.get("label") == "Commit changes..."
                for element in reduced["elements"]
            )
        )
        command = {
            "action": "click",
            "args": {"target": "Commit changes..."},
        }

        with self.assertRaisesRegex(LauncherError, "resolved to 0"):
            _validate_model_action(
                command,
                reduced,
                task_payload={
                    "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
                },
                mutation_receipt={},
            )

        action, args = _validate_model_action(
            command,
            full_page,
            task_payload={
                "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
            },
            mutation_receipt={},
        )
        self.assertEqual(action, "click")
        self.assertEqual(args["elementId"], "g270-e187")

    def test_full_page_duplicate_commit_buttons_fail_closed(self):
        full_page = {
            "url": (
                "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/"
                "edit/main/browser_worker_launcher.py"
            ),
            "generation": 270,
            "elements": [
                {
                    "id": "g270-e187",
                    "role": "button",
                    "label": "Commit changes...",
                },
                {
                    "id": "g270-e188",
                    "role": "button",
                    "label": "Commit changes...",
                },
            ],
        }

        with self.assertRaisesRegex(LauncherError, "resolved to 2"):
            _validate_model_action(
                {
                    "action": "click",
                    "args": {"target": "Commit changes..."},
                },
                full_page,
                task_payload={
                    "repository": "GK-studio-JP/ai-os-runtime-browser-worker",
                },
                mutation_receipt={},
            )


if __name__ == "__main__":
    unittest.main()
