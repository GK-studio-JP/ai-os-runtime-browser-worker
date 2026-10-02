from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ai_os_browser_worker.nightly_dream import (
    iso,
    latest_success,
    oldest_open_run,
    parse_iso,
    resolve_window,
    run_metadata,
)

BOARD = "GK-studio-JP/ai-bulletin-board"
API = "https://api.github.com"
CONTROL_ISSUE = 52


class DreamSourceError(RuntimeError):
    pass


def github_json(url: str, *, token: str | None = None) -> Any:
    token = str(token or "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aios-nightly-dream-source/2",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        mode = "authenticated" if token else "public"
        raise DreamSourceError(f"{mode} GitHub read failed for {url}: {exc}") from exc


def paginated(url: str, *, token: str | None = None) -> list[dict[str, Any]]:
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
    number = issue.get("number")
    if not isinstance(number, int):
        raise DreamSourceError("canonical issue number is missing")
    for field in (
        "created_at",
        "updated_at",
        "closed_at",
        "author_association",
        "body",
    ):
        if field not in issue:
            raise DreamSourceError(f"canonical issue field missing: {field}")
    declared = issue.get("comments")
    if isinstance(declared, int) and declared != len(comments):
        raise DreamSourceError(
            f"issue #{number} declares {declared} comments but {len(comments)} were fetched"
        )
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
        user = comment.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise DreamSourceError("canonical comment actor login is missing")


def _list_issues(
    *,
    token: str | None,
    repository: str,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "state": "all",
        "sort": "updated",
        "direction": "asc",
    }
    if since is not None:
        params["since"] = iso(since)
    rows = paginated(
        f"{API}/repos/{repository}/issues?{urllib.parse.urlencode(params)}",
        token=token,
    )
    return [row for row in rows if "pull_request" not in row]


def _history(
    issue: dict[str, Any],
    *,
    token: str | None,
    repository: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    number = issue.get("number")
    if not isinstance(number, int):
        raise DreamSourceError("GitHub issue row is missing numeric number")
    comments = paginated(
        f"{API}/repos/{repository}/issues/{number}/comments",
        token=token,
    )
    _validate_history(issue, comments)
    return issue, comments


def acquire_histories(
    *,
    token: str | None = None,
    repository: str = BOARD,
    automation_start: datetime | None = None,
) -> dict[str, Any]:
    at = automation_start or datetime.now(timezone.utc)
    issues = _list_issues(token=token, repository=repository)
    by_number = {
        int(issue["number"]): issue
        for issue in issues
        if isinstance(issue.get("number"), int)
    }
    control = by_number.get(CONTROL_ISSUE)
    if control is None:
        raise DreamSourceError("Control Issue #52 is missing")

    histories: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    def include(issue: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        number = int(issue["number"])
        if number not in histories:
            histories[number] = _history(
                issue,
                token=token,
                repository=repository,
            )
        return histories[number]

    include(control)

    dream_issues = [issue for issue in issues if run_metadata(issue) is not None]
    for issue in dream_issues:
        if str(issue.get("state") or "").lower() == "open":
            include(issue)

    closed_candidates: list[tuple[datetime, dict[str, Any]]] = []
    for issue in dream_issues:
        if str(issue.get("state") or "").lower() != "closed":
            continue
        meta = run_metadata(issue)
        if meta is None:
            continue
        try:
            window_end = parse_iso(str(meta["window_end"]))
        except Exception:
            continue
        closed_candidates.append((window_end, issue))
    closed_candidates.sort(key=lambda item: item[0], reverse=True)
    for _window_end, issue in closed_candidates:
        history = include(issue)
        if latest_success([history]) is not None:
            break

    seed = list(histories.values())
    resumed = oldest_open_run(seed)
    if resumed is not None:
        _issue, meta = resumed
        window_start = parse_iso(str(meta["window_start"]))
    else:
        window_start, _window_end, _previous = resolve_window(seed, at)

    for issue in _list_issues(
        token=token,
        repository=repository,
        since=window_start,
    ):
        include(issue)

    return {
        "schema": "aios-dream-histories:v1",
        "repository": repository,
        "histories": [
            {"issue": issue, "comments": comments}
            for _number, (issue, comments) in sorted(histories.items())
        ],
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
