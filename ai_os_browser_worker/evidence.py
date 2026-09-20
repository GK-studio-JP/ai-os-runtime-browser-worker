from __future__ import annotations

import re
from typing import Any

from ai_os_browser_worker.navigation_policy import _task_repository

EVIDENCE_KINDS = {"visited_url", "observed_text", "immutable_artifact", "extracted_fact"}
SHA40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
GEMINI = "https://gemini.google.com/app"


def _task_requirement_text(task_payload: dict[str, Any]) -> str:
    acceptance = task_payload.get("acceptance")
    rows = acceptance if isinstance(acceptance, list) else []
    return "\n".join(
        [str(task_payload.get("objective") or ""), *[str(item) for item in rows]]
    )


def _requires_current_main_sha(task_payload: dict[str, Any]) -> bool:
    requirement = _task_requirement_text(task_payload).lower()
    return "current main" in requirement and ("sha" in requirement or "commit" in requirement)


def _main_head_evidence_url(repository: str) -> str:
    return f"https://api.github.com/repos/{repository}/commits/main"


def _top_level_sha(page_text: str) -> str | None:
    match = re.match(r'\s*\{\s*"sha"\s*:\s*"([0-9a-f]{40})"', page_text)
    return match.group(1) if match else None


def _noncanonical_evidence_pages(
    ledger: list[dict[str, Any]], issue_url: str
) -> list[dict[str, Any]]:
    issue_prefix = issue_url.rstrip("/")
    return [
        row
        for row in ledger
        if (url := str(row.get("url") or ""))
        and not url.rstrip("/").startswith(issue_prefix)
        and not url.startswith(GEMINI)
    ]


def validate_finish_evidence(
    command: dict[str, Any],
    ledger: list[dict[str, Any]],
    task_payload: dict[str, Any],
    issue_url: str,
) -> str | None:
    summary = str(command.get("summary") or "").strip()
    artifacts = command.get("artifacts")
    evidence = command.get("evidence")
    if not summary:
        return "finish rejected: summary must be non-empty."
    if not isinstance(artifacts, list) or not all(
        isinstance(item, str) and item.strip() for item in artifacts
    ):
        return "finish rejected: artifacts must be a list of non-empty strings."
    if not isinstance(evidence, list) or not evidence:
        return "finish rejected: structured evidence is required."
    if not all(
        isinstance(item, dict)
        and item.get("kind") in EVIDENCE_KINDS
        and isinstance(item.get("value"), str)
        and item.get("value", "").strip()
        for item in evidence
    ):
        return (
            "finish rejected: evidence entries require kind "
            "visited_url|observed_text|immutable_artifact|extracted_fact and a non-empty value."
        )

    pages = _noncanonical_evidence_pages(ledger, issue_url)
    if not pages:
        return "finish rejected: no non-canonical task page was observed."

    def observed(value: str) -> bool:
        return any(
            value in str(row.get("pageText") or "") or value in str(row.get("url") or "")
            for row in pages
        )

    def visited(value: str) -> bool:
        needle = value.rstrip("/")
        return any(str(row.get("url") or "").rstrip("/") == needle for row in pages)

    for item in evidence:
        kind = str(item["kind"])
        value = str(item["value"]).strip()
        if kind == "visited_url" and not visited(value):
            return f"finish rejected: visited_url evidence was not observed: {value}"
        if kind in {"observed_text", "extracted_fact"} and not observed(value):
            return f"finish rejected: {kind} was not observed in the task browser: {value}"
        if kind == "immutable_artifact":
            supported = visited(value) if value.startswith(("http://", "https://")) else observed(value)
            if not supported:
                return f"finish rejected: immutable_artifact was not observed: {value}"

    requirement_text = _task_requirement_text(task_payload)
    requirement_lower = requirement_text.lower()
    artifact_text = "\n".join(str(item).strip() for item in artifacts)

    needs_current_main_sha = _requires_current_main_sha(task_payload)
    needs_sha = "commit sha" in requirement_lower or needs_current_main_sha
    if needs_sha:
        shas = SHA40_RE.findall(artifact_text)
        if not shas:
            return "finish rejected: acceptance requires a 40-character commit SHA in RESULT artifacts."

        if needs_current_main_sha:
            repository = _task_repository(task_payload)
            if not repository:
                return "finish rejected: current-main verification requires task repository in owner/repo form."
            expected_url = _main_head_evidence_url(repository)
            main_rows = [
                row
                for row in pages
                if str(row.get("url") or "").rstrip("/") == expected_url.rstrip("/")
            ]
            if not main_rows:
                return (
                    "finish rejected: current main SHA must be observed by visiting "
                    f"{expected_url}; commit detail pages do not prove the current main HEAD."
                )
            main_sha = next(
                (
                    sha
                    for row in reversed(main_rows)
                    if (sha := _top_level_sha(str(row.get("pageText") or "")))
                ),
                None,
            )
            if not main_sha:
                return (
                    "finish rejected: the commits/main page did not expose a valid "
                    'top-level 40-character "sha".'
                )
            if main_sha not in shas:
                return (
                    "finish rejected: RESULT artifact SHA does not match the current main HEAD "
                    f"observed at {expected_url}."
                )
            if not any(
                item.get("kind") == "extracted_fact"
                and str(item.get("value") or "").strip() == main_sha
                for item in evidence
            ):
                return (
                    "finish rejected: structured evidence must include the exact current main "
                    "HEAD SHA as extracted_fact."
                )
        else:
            supported_shas = [sha for sha in shas if observed(sha)]
            if not supported_shas:
                return "finish rejected: artifact commit SHA was not observed on a non-canonical task page."
            if not any(
                any(sha in str(item.get("value") or "") for sha in supported_shas)
                for item in evidence
            ):
                return "finish rejected: the observed commit SHA must also appear in structured evidence."

    filenames = sorted(set(re.findall(r"\b[A-Za-z0-9_.-]+\.py\b", requirement_text)))
    for filename in filenames:
        if filename not in summary:
            return f"finish rejected: RESULT summary must name the verified file {filename}."
        if not any(filename in str(row.get("url") or "") for row in pages):
            return f"finish rejected: file existence was not verified by visiting {filename}."
        if not any(
            filename in str(item.get("value") or "")
            for item in evidence
            if item.get("kind") in {"visited_url", "immutable_artifact"}
        ):
            return f"finish rejected: structured evidence must include the visited {filename} artifact."

    return None


