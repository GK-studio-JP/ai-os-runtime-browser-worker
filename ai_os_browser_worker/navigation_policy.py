from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any

READ_ONLY_ACTIONS = {"goto", "getPage", "click", "scroll", "setViewport"}
MUTATION_ACTIONS = {"fill"}
MUTATION_RECEIPT_SCHEMA = "ai-os-kernel-capability-receipt:v1"
ALLOWED_MUTATION_BUTTONS = {
    "commit changes...",
    "commit changes",
    "propose changes",
    "create pull request",
}
DENIED_MUTATION_TERMS = (
    "delete",
    "archive",
    "merge pull request",
    "revert",
    "close pull request",
    "settings",
    "danger zone",
)


class LauncherError(RuntimeError):
    pass


def _task_repository(task_payload: dict[str, Any]) -> str | None:
    repository = str(task_payload.get("repository") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return None
    return repository


def _context_repositories(task_payload: dict[str, Any]) -> set[str]:
    repositories: set[str] = set()
    refs = task_payload.get("context_refs")
    if not isinstance(refs, list):
        return repositories

    for raw in refs:
        if not isinstance(raw, str):
            continue
        parsed = urllib.parse.urlparse(raw.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        host = parsed.hostname.lower()
        parts = [part for part in (parsed.path or "").split("/") if part]
        candidate: str | None = None
        if host == "github.com" and len(parts) >= 2:
            candidate = f"{parts[0]}/{parts[1]}"
        elif host == "api.github.com" and len(parts) >= 3 and parts[0] == "repos":
            candidate = f"{parts[1]}/{parts[2]}"
        elif host == "raw.githubusercontent.com" and len(parts) >= 2:
            candidate = f"{parts[0]}/{parts[1]}"

        if candidate and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate):
            repositories.add(candidate)
    return repositories


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
    repositories = {repository, *_context_repositories(task_payload)}
    host = parsed.hostname.lower()
    path = parsed.path or "/"

    allowed = False
    for allowed_repository in repositories:
        owner, repo = allowed_repository.split("/", 1)
        if host == "github.com":
            prefix = f"/{owner}/{repo}"
            allowed = path == prefix or path.startswith(prefix + "/")
        elif host == "api.github.com":
            prefix = f"/repos/{owner}/{repo}"
            allowed = path == prefix or path.startswith(prefix + "/")
        elif host == "raw.githubusercontent.com":
            prefix = f"/{owner}/{repo}/"
            allowed = path.startswith(prefix)
        if allowed:
            break

    if not allowed:
        raise LauncherError(
            f"browser navigation outside task/context repositories is denied: {value}"
        )
    return value


def _fingerprint(value: dict[str, Any]) -> str:
    material = {key: item for key, item in value.items() if key != "fingerprint"}
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_mutation_receipt(
    receipt: dict[str, Any],
    *,
    dispatch: dict[str, Any],
    plan_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("schema") != MUTATION_RECEIPT_SCHEMA:
        raise LauncherError("invalid Kernel mutation receipt schema")
    if receipt.get("fingerprint") != _fingerprint(receipt):
        raise LauncherError("Kernel mutation receipt fingerprint mismatch")
    if receipt.get("approved") is not True or receipt.get("authoritative") is not False:
        raise LauncherError("Kernel mutation receipt is not approved")
    if receipt.get("caller") != dispatch.get("process"):
        raise LauncherError("Kernel mutation receipt caller mismatch")
    if receipt.get("task") != dispatch.get("task"):
        raise LauncherError("Kernel mutation receipt task mismatch")
    if receipt.get("target_repository") != dispatch.get("target_repository"):
        raise LauncherError("Kernel mutation receipt repository mismatch")
    if receipt.get("source_plan_fingerprint") != plan_fingerprint:
        raise LauncherError("Kernel mutation receipt dispatch fingerprint mismatch")
    if receipt.get("required_capability") != "repository.write.branch":
        raise LauncherError("Kernel mutation receipt capability mismatch")

    constraints = receipt.get("constraints")
    if not isinstance(constraints, dict):
        raise LauncherError("Kernel mutation receipt constraints are missing")
    if (
        constraints.get("mode") != "branch-pr"
        or constraints.get("forbid_direct_main_commit") is not True
        or constraints.get("require_new_branch") is not True
        or constraints.get("require_pull_request") is not True
    ):
        raise LauncherError("Kernel mutation receipt branch/PR constraints are invalid")
    allowed = constraints.get("allowed_browser_actions")
    if not isinstance(allowed, list) or not {"fill", "click"}.issubset(set(allowed)):
        raise LauncherError("Kernel mutation receipt browser action scope is incomplete")
    return receipt


def _mutation_page_allowed(url: str, repository: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        return False
    prefix = f"/{repository}"
    path = parsed.path or "/"
    if not (path == prefix or path.startswith(prefix + "/")):
        return False
    suffix = path[len(prefix):]
    return (
        suffix.startswith("/edit/")
        or suffix.startswith("/new/")
        or suffix.startswith("/compare/")
        or suffix.startswith("/pull/new/")
    )


def _checked(element: dict[str, Any]) -> bool:
    states = element.get("states")
    return isinstance(states, dict) and states.get("checked") is True


def _require_safe_commit_target(
    observation: dict[str, Any],
    *,
    task_payload: dict[str, Any],
) -> None:
    repository = _task_repository(task_payload)
    if not repository:
        raise LauncherError("branch mutation requires task repository")

    radios = [
        element
        for element in observation.get("elements") or []
        if element.get("role") == "radio"
    ]
    new_branch = next(
        (
            element
            for element in radios
            if "create a new branch for this commit" in str(element.get("label") or "").lower()
        ),
        None,
    )
    direct = next(
        (
            element
            for element in radios
            if str(element.get("label") or "").lower().startswith("commit directly to")
        ),
        None,
    )
    if new_branch and _checked(new_branch):
        return
    if direct and _checked(direct):
        raise LauncherError(
            "direct commit is forbidden; Kernel receipt requires a new branch"
        )
    raise LauncherError("repository mutation requires an explicit safe branch target")


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


def _semantic_element_args(
    action: str,
    args: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    args = dict(args)
    if action == "fill" and "text" not in args and isinstance(args.get("value"), str):
        args["text"] = args["value"]
    args.pop("value", None)

    if args.get("elementId") or args.get("id"):
        return args

    generation = observation.get("generation")
    if generation is None:
        raise LauncherError("semantic element resolution requires a current generation")

    roles = {"textbox"} if action == "fill" else {"link", "button", "radio"}
    candidates = [
        element
        for element in observation.get("elements") or []
        if element.get("role") in roles
        and str(element.get("id") or "").startswith(f"g{generation}-")
    ]

    field = str(args.get("field") or "").strip().lower().replace("-", "_")
    target = str(args.get("label") or args.get("target") or "").strip()

    if action == "fill" and field:
        if field in {"file_name", "filename"}:
            candidates = [
                element
                for element in candidates
                if str(element.get("label") or "").strip().lower() == "file name"
            ]
        elif field in {"file_contents", "contents", "content", "source"}:
            candidates = [
                element
                for element in candidates
                if str(element.get("label") or "").lower().startswith(
                    "editing file contents"
                )
            ]
        elif field in {"commit_message", "message"}:
            candidates = [
                element
                for element in candidates
                if str(element.get("label") or "").strip().lower() == "commit message"
            ]
        else:
            raise LauncherError(f"unsupported semantic fill field: {field!r}")
    elif target:
        lowered = target.lower()
        candidates = [
            element
            for element in candidates
            if str(element.get("label") or "").strip().lower() == lowered
            or str(element.get("text") or "").strip().lower() == lowered
        ]
    elif action == "fill":
        text = args.get("text")
        if not isinstance(text, str):
            raise LauncherError("model fill requires text")
        parsed = urllib.parse.urlparse(str(observation.get("url") or ""))
        path = parsed.path or ""
        if "/new/" in path and re.fullmatch(
            r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+",
            text.strip(),
        ):
            candidates = [
                element
                for element in candidates
                if str(element.get("label") or "").strip().lower() == "file name"
            ]
        elif "/new/" in path or "/edit/" in path:
            candidates = [
                element
                for element in candidates
                if str(element.get("label") or "").lower().startswith(
                    "editing file contents"
                )
            ]
        else:
            raise LauncherError(
                "model fill without elementId requires a supported semantic field"
            )
    else:
        raise LauncherError(
            f"model {action} without elementId requires label or target"
        )

    if len(candidates) != 1:
        descriptor = field or target or "inferred target"
        raise LauncherError(
            f"semantic {action} target {descriptor!r} resolved to "
            f"{len(candidates)} current-generation elements"
        )

    resolved = dict(args)
    resolved["elementId"] = candidates[0]["id"]
    resolved.pop("field", None)
    resolved.pop("label", None)
    resolved.pop("target", None)
    resolved.pop("value", None)
    return resolved


def _validate_model_action(
    command: dict[str, Any],
    observation: dict[str, Any],
    *,
    task_payload: dict[str, Any] | None = None,
    issue_url: str = "",
    mutation_receipt: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    action = str(command.get("action") or "")
    raw_args = command.get("args")
    allowed = set(READ_ONLY_ACTIONS)
    if mutation_receipt is not None:
        allowed.update(MUTATION_ACTIONS)
    if action not in allowed or not isinstance(raw_args, dict):
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

    element: dict[str, Any] | None = None
    if action in {"click", "fill"}:
        args = _semantic_element_args(action, args, observation)
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
        if not element:
            raise LauncherError("model action element is missing from current observation")

    if action == "fill":
        if mutation_receipt is None:
            raise LauncherError("model fill requires a Kernel mutation receipt")
        if element is None or element.get("role") != "textbox":
            raise LauncherError("model fill is limited to current-generation textboxes")
        repository = _task_repository(payload)
        if not repository or not _mutation_page_allowed(
            str(observation.get("url") or ""),
            repository,
        ):
            raise LauncherError("model fill is limited to task-repository branch/PR pages")
        text = args.get("text")
        if not isinstance(text, str):
            raise LauncherError("model fill requires text")
        return action, args

    if action == "click":
        if element is not None and element.get("role") == "link":
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

        if mutation_receipt is None:
            raise LauncherError(
                "model click is limited to current-generation navigation links"
            )

        repository = _task_repository(payload)
        if not repository or not _mutation_page_allowed(
            str(observation.get("url") or ""),
            repository,
        ):
            raise LauncherError("model mutation click is limited to task-repository branch/PR pages")
        if element is None or element.get("role") not in {"button", "radio"}:
            raise LauncherError("model mutation click requires an approved button or radio")

        descriptor = str(element.get("label") or element.get("text") or "").strip().lower()
        if any(term in descriptor for term in DENIED_MUTATION_TERMS):
            raise LauncherError(f"dangerous mutation control is denied: {descriptor!r}")

        if element.get("role") == "radio":
            if "create a new branch for this commit" not in descriptor:
                raise LauncherError("only the new-branch commit radio may be selected")
            return action, args

        if descriptor not in ALLOWED_MUTATION_BUTTONS:
            raise LauncherError(f"mutation button is outside the receipt allowlist: {descriptor!r}")
        if descriptor in {"commit changes", "propose changes"}:
            _require_safe_commit_target(observation, task_payload=payload)
        return action, args

    return action, args


def refresh_element_args(
    action: str,
    args: dict[str, Any],
    observation: dict[str, Any],
    fresh_page: dict[str, Any],
) -> dict[str, Any]:
    if action not in {"click", "fill"}:
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
