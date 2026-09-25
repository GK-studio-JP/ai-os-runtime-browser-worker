import unittest

from ai_os_browser_worker.evidence import (
    _requires_repository_change,
    validate_finish_evidence,
)


class FinishRepositoryChangeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue_url = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/19"
        self.task_payload = {
            "repository": "GK-studio-JP/ai-os-context",
            "objective": (
                "Close the cross-repository projection path in "
                ".github/workflows/snapshot.yml."
            ),
            "acceptance": [
                "workflow_dispatch cannot select an arbitrary repository.",
                (
                    "SOURCE_REPOSITORY is fixed or allowlisted to "
                    "GK-studio-JP/ai-bulletin-board with explicit fail-closed validation."
                ),
                "Create a new branch and PR; never commit directly to an existing branch.",
                "RESULT artifacts include PR, immutable commit SHA, and workflow/test evidence.",
            ],
            "contracts": [
                "Live canonical repository is GK-studio-JP/ai-bulletin-board.",
            ],
        }

    def test_acceptance_marks_close_objective_as_repository_change(self) -> None:
        self.assertTrue(_requires_repository_change(self.task_payload))

    def test_finish_without_source_mutation_is_rejected(self) -> None:
        readme_url = "https://github.com/GK-studio-JP/ai-os-context/blob/main/README.md"
        command = {
            "kind": "finish",
            "summary": "Inspected snapshot workflow and README.",
            "artifacts": ["https://github.com/GK-studio-JP/ai-os-context/pulls"],
            "evidence": [{"kind": "visited_url", "value": readme_url}],
        }
        ledger = [{"url": readme_url, "pageText": "README"}]

        rejection = validate_finish_evidence(
            command,
            ledger,
            self.task_payload,
            self.issue_url,
            source_mutation_performed=False,
            require_new_pr=True,
        )

        self.assertIn("source-file mutation", rejection)


if __name__ == "__main__":
    unittest.main()
