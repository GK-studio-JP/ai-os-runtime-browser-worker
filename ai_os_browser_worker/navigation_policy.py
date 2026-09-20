from __future__ import annotations

import re
import urllib.parse
from typing import Any

ALLOWED = {"goto", "getPage", "click", "scroll", "setViewport"}


class LauncherError(RuntimeError):
    pass


def _task_repository(task_payload: dict[str, Any]) -> str | None:
    repository = str(task_payload.get("repository") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return None
    return repository


def _allowed_navigation_url(
    url: str,
    *,
    task_payload: dict[str, Any],
    issue_url: str,
) -> str:
    value = str(url or "").strip()
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LauncherError("browser navigation URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise LauncherError("browser navigation requires an HTTPS task-scoped URL")

    if issue_url and value.rstrip("/") == issue_url.rstrip("/"):
        return value

    repository = _task_repository(task_payload)
    if not repository:
        raise LauncherError("browser navigation requires task repository in owner/repo form")
    owner, repo = repository.split("/", 1)
    host = parsed.hostname.lower()
    path = parsed.path or "/"

    allowed = False
    if host == "github.com":
        prefix = f"/{owner}/{repo}"
        allowed = path == prefix or path.startswith(prefix + "/")
    elif host == "api.github.com":
        prefix = f"/repos/{owner}/{repo}"
        allowed = path == prefix or path.startswith(prefix + "/")
    elif host == "raw.githubusercontent.com":
        prefix = f"/{owner}/{repo}/"
        allowed = path.startswith(prefix)

    if not allowed:
        raise LauncherError(
            f"browser navigation outside task repository is denied: {value}"
        )
    return value


def _element_for_action(
    observation: dict[str, Any], args: dict[str, Any]
) -> dict[str, Any] | None:
    element_id = str(args.get("elementId") or "")
    if not element_id:
        return None
    return next(
        (e for e in observation.get("elements") or [] if e.get("id") == element_id),
        None,
    )


def _validate_model_action(
    command: dict[str, Any],
    observation: dict[str, Any],
    *,
    task_payload: dict[str, Any] | None = None,
    issue_url: str = "",
) -> tuple[str, dict[str, Any]]:
    action = str(command.get("action") or "")
    raw_args = command.get("args")
    if action not in ALLOWED or not isinstance(raw_args, dict):
        raise LauncherError(f"disallowed model action: {action!r}")

    args = dict(raw_args)
    payload = task_payload or {}

    if action == "goto":
        args["url"] = _allowed_navigation_url(
            str(args.get("url") or ""),
            task_payload=payload,
            issue_url=issue_url,
        )
        return action, args

    if action == "click":
        if "elementId" not in args and "id" in args:
            args["elementId"] = args["id"]
        args.pop("id", None)
        element_id = str(args.get("elementId") or "")
        generation = observation.get("generation")
        if generation is None or not element_id.startswith(f"g{generation}-"):
            raise LauncherError(
                f"stale elementId {element_id!r} for generation {generation!r}"
            )
        element = _element_for_action(observation, args)
        if not element or element.get("role") != "link":
            raise LauncherError(
                "model click is limited to current-generation navigation links"
            )
        href = str(element.get("href") or "")
        if not href:
            raise LauncherError("model click navigation link is missing href")
        target = urllib.parse.urljoin(str(observation.get("url") or ""), href)
        _allowed_navigation_url(
            target,
            task_payload=payload,
            issue_url=issue_url,
        )
        return action, args

    return action, args


def refresh_element_args(
    action: str,
    args: dict[str, Any],
    observation: dict[str, Any],
    fresh_page: dict[str, Any],
) -> dict[str, Any]:
    if action != "click":
        return dict(args)

    source = _element_for_action(observation, args)
    if not source:
        raise LauncherError(
            f"cannot refresh elementId {args.get('elementId')!r}: source element missing"
        )

    fresh_generation = fresh_page.get("generation")
    if fresh_generation is None:
        raise LauncherError("cannot refresh elementId without a fresh page generation")

    source_role = source.get("role")
    source_label = str(source.get("label") or "")
    source_text = str(source.get("text") or "")
    candidates = []
    for element in fresh_page.get("elements") or []:
        element_id = str(element.get("id") or "")
        if not element_id.startswith(f"g{fresh_generation}-"):
            continue
        if source_role and element.get("role") != source_role:
            continue
        if source_label:
            if str(element.get("label") or "") != source_label:
                continue
        elif source_text:
            if str(element.get("text") or "") != source_text:
                continue
        else:
            continue
        candidates.append(element)

    if len(candidates) != 1:
        descriptor = source_label or source_text or str(source.get("id") or "")
        raise LauncherError(
            f"cannot refresh elementId for {descriptor!r}: {len(candidates)} matches on fresh page"
        )

    refreshed = dict(args)
    refreshed["elementId"] = candidates[0]["id"]
    return refreshed
