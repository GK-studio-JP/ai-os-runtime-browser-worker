from __future__ import annotations

import json
import urllib.request
from typing import Any

BOARD = "GK-studio-JP/ai-bulletin-board"
API = "https://api.github.com"


class DreamSourceError(RuntimeError):
    pass


def github_json(url: str, *, token: str) -> Any:
    token = str(token or "").strip()
    if not token:
        raise DreamSourceError("GITHUB_TOKEN is required for authenticated Dream source reads")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aios-nightly-dream-source/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise DreamSourceError(f"authenticated GitHub read failed for {url}: {exc}") from exc


def paginated(url: str, *, token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        separator = "&" if "?" in url else "?"
        value = github_json(
            f"{url}{separator}per_page=100&page={page}",
            token=token,
        )
        if not isinstance(value, list):
            raise DreamSourceError("GitHub paginated response must be a list")
        if not all(isinstance(item, dict) for item in value):
            raise DreamSourceError("GitHub paginated response contains a non-object row")
        out.extend(value)
        if len(value) < 100:
            return out
    raise DreamSourceError("GitHub pagination exceeded 100 pages")


def _validate_history(issue: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    for field in (
        "number",
        "created_at",
        "updated_at",
        "closed_at",
        "author_association",
        "body",
    ):
        if field not in issue:
            raise DreamSourceError(f"canonical issue field missing: {field}")
    for comment in comments:
        for field in (
            "id",
            "created_at",
            "updated_at",
            "author_association",
            "body",
            "user",
        ):
            if field not in comment:
                raise DreamSourceError(f"canonical comment field missing: {field}")


def acquire_histories(
    *,
    token: str,
    repository: str = BOARD,
) -> dict[str, Any]:
    issues = paginated(
        f"{API}/repos/{repository}/issues?state=all&sort=updated&direction=asc",
        token=token,
    )
    histories: list[dict[str, Any]] = []
    for listed in issues:
        if "pull_request" in listed:
            continue
        number = listed.get("number")
        if not isinstance(number, int):
            raise DreamSourceError("GitHub issue row is missing numeric number")
        issue = github_json(
            f"{API}/repos/{repository}/issues/{number}",
            token=token,
        )
        comments = paginated(
            f"{API}/repos/{repository}/issues/{number}/comments",
            token=token,
        )
        if not isinstance(issue, dict):
            raise DreamSourceError(f"GitHub issue #{number} response must be an object")
        _validate_history(issue, comments)
        histories.append({"issue": issue, "comments": comments})
    return {
        "schema": "aios-dream-histories:v1",
        "repository": repository,
        "histories": histories,
    }


def history_tuples(snapshot: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    if snapshot.get("schema") != "aios-dream-histories:v1":
        raise DreamSourceError("unsupported Dream histories snapshot")
    if snapshot.get("repository") != BOARD:
        raise DreamSourceError("Dream histories repository mismatch")
    rows = snapshot.get("histories")
    if not isinstance(rows, list):
        raise DreamSourceError("Dream histories must be a list")
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DreamSourceError("Dream history row must be an object")
        issue = row.get("issue")
        comments = row.get("comments")
        if not isinstance(issue, dict) or not isinstance(comments, list):
            raise DreamSourceError("Dream history row requires issue and comments")
        number = issue.get("number")
        if not isinstance(number, int) or number in seen:
            raise DreamSourceError("Dream history issue number is missing or duplicated")
        seen.add(number)
        out.append((issue, comments))
    return out