from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_os_context.protocol import extract_task_envelope
from ai_os_context.replay import replay as canonical_replay

PROCESS = "PROC-RUNTIME-BROWSER-WORKER"
BOARD = "GK-studio-JP/ai-bulletin-board"
SCHEDULER = "GK-studio-JP/ai-os-scheduler"
WORKFLOW = "browser-worker-plan.yml"
ARTIFACT = "ai-os-browser-worker-dispatch"
GEMINI = "https://gemini.google.com/app"
WORKER_DOC = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
BROWSER_DOC = "https://github.com/GK-studio-JP/browser-agent/blob/main/BROWSER_AGENT_INSTRUCTIONS.md"
ALLOWED = {"goto", "getPage", "click", "scroll", "setViewport"}
MUTATING: set[str] = set()
EVIDENCE_KINDS = {"visited_url", "observed_text", "immutable_artifact", "extracted_fact"}
SHA40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")


class LauncherError(RuntimeError):
    pass


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    value: Any = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    h = {"User-Agent": "ai-os-runtime-worker", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = None
    if value is not None:
        body = json.dumps(value).encode()
        h["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    except Exception as exc:
        raise LauncherError(f"HTTP failure for {method} {url}: {exc}") from exc


def _json(method: str, url: str, **kwargs: Any) -> Any:
    raw = _request(method, url, **kwargs)
    return json.loads(raw.decode()) if raw else None


def github(path: str, *, token: str | None = None) -> Any:
    return _json(
        "GET",
        f"https://api.github.com{path}",
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    )


def latest_scheduler_run(token: str | None) -> int:
    data = github(
        f"/repos/{SCHEDULER}/actions/workflows/{WORKFLOW}/runs?status=success&per_page=1",
        token=token,
    )
    runs = data.get("workflow_runs", [])
    if not runs:
        raise LauncherError("no successful Browser Worker scheduler run")
    return int(runs[0]["id"])


def plan_from_run(run_id: int, token: str | None) -> dict[str, Any]:
    data = github(f"/repos/{SCHEDULER}/actions/runs/{run_id}/artifacts?per_page=100", token=token)
    item = next((a for a in data.get("artifacts", []) if a.get("name") == ARTIFACT), None)
    if not item:
        raise LauncherError(f"{ARTIFACT} missing on scheduler run {run_id}")
    raw = _request(
        "GET",
        item["archive_download_url"],
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.endswith("browser-worker-plan.json")]
        if len(names) != 1:
            raise LauncherError("scheduler artifact must contain exactly one browser-worker-plan.json")
        return json.loads(archive.read(names[0]))


def plan_from_file(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise LauncherError(f"failed to read dispatch plan file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError("dispatch plan file must contain one JSON object")
    return value


def plan_from_url(url: str) -> dict[str, Any]:
    try:
        value = json.loads(_request("GET", url).decode())
    except Exception as exc:
        raise LauncherError(f"failed to read dispatch plan URL {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError("dispatch plan URL must contain one JSON object")
    return value


def resolve_plan(
    *,
    token: str | None,
    scheduler_run_id: int | None = None,
    plan_file: str | None = None,
    plan_url: str | None = None,
) -> dict[str, Any]:
    selected = sum(bool(v) for v in (scheduler_run_id, plan_file, plan_url))
    if selected > 1:
        raise LauncherError("choose only one of --scheduler-run-id, --plan-file, or --plan-url")
    if plan_file:
        return plan_from_file(plan_file)
    if plan_url:
        return plan_from_url(plan_url)
    run_id = scheduler_run_id or latest_scheduler_run(token)
    return plan_from_run(run_id, token)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    if plan.get("schema") != "ai-os-dispatch-plan:v1" or plan.get("authoritative") is not False:
        raise LauncherError("invalid dispatch plan boundary")
    if plan.get("filters", {}).get("process") != PROCESS:
        raise LauncherError("dispatch plan process mismatch")
    rows = plan.get("dispatches")
    if not isinstance(rows, list) or plan.get("dispatch_count") != len(rows) or len(rows) > 1:
        raise LauncherError("launcher requires a bounded 0-or-1 dispatch plan")
    if not rows:
        return None
    dispatch = rows[0]
    if (
        dispatch.get("schema") != "ai-os-dispatch:v1"
        or dispatch.get("authoritative") is not False
        or dispatch.get("process") != PROCESS
    ):
        raise LauncherError("invalid dispatch boundary")
    task = str(dispatch.get("task") or "")
    issue_url = str(dispatch.get("source", {}).get("issue_url") or "")
    if not re.fullmatch(r"#\d+", task):
        raise LauncherError("dispatch task pointer is invalid")
    expected = f"https://github.com/{BOARD}/issues/{task[1:]}"
    if issue_url.rstrip("/") != expected:
        raise LauncherError("dispatch issue URL does not match canonical board task pointer")
    return dispatch


def issue_number_from_dispatch(dispatch: dict[str, Any]) -> int:
    return int(str(dispatch["task"])[1:])


def _balanced_json_objects(text: str) -> list[str]:
    out = []
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                continue
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start : index + 1])
                    break
    return out


def _commands(text: str) -> list[dict[str, Any]]:
    out = []
    for raw in _balanced_json_objects(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("kind") in {"browser_action", "finish", "wait"}:
            out.append(obj)
    return out


def extract_model_command(page_text: str) -> dict[str, Any]:
    rows = _commands(page_text)
    if not rows:
        raise LauncherError("Gemini returned no launcher command")
    return rows[-1]


def _canonical_replay_state(
    comments: list[dict[str, Any]],
    *,
    task: str,
    now: datetime | None = None,
) -> Any:
    if not re.fullmatch(r"#\d+", task):
        raise LauncherError("canonical task pointer is invalid")
    state = canonical_replay({"number": int(task[1:])}, comments, now=now)
    if not state.history_safe:
        reason = state.history_unsafe_reason or "unknown history defect"
        raise LauncherError(f"canonical history is unsafe: {reason}")
    return state


def canonical_task_completed(
    comments: list[dict[str, Any]],
    *,
    task: str,
    now: datetime | None = None,
) -> bool:
    return _canonical_replay_state(comments, task=task, now=now).state == "completed"


def canonical_claim_present(
    comments: list[dict[str, Any]],
    *,
    task: str,
    agent_id: str,
    now: datetime | None = None,
) -> bool:
    state = _canonical_replay_state(comments, task=task, now=now)
    return state.state == "claimed" and state.owner == agent_id


def canonical_result_present(
    comments: list[dict[str, Any]],
    *,
    task: str,
    agent_id: str,
    now: datetime | None = None,
) -> bool:
    state = _canonical_replay_state(comments, task=task, now=now)
    return (
        state.state == "completed"
        and state.latest_result is not None
        and state.latest_result.agent_id == agent_id
    )


def _task_payload_from_issue(body: str) -> dict[str, Any]:
    value = extract_task_envelope(body)
    if isinstance(value, dict) and value.get("process") == PROCESS:
        return value
    return {}


def _record_page_evidence(ledger: list[dict[str, Any]], page: dict[str, Any]) -> None:
    ledger.append(
        {
            "url": str(page.get("url") or ""),
            "title": str(page.get("title") or ""),
            "generation": page.get("generation"),
            "pageText": str(page.get("pageText") or "")[:12000],
        }
    )
    if len(ledger) > 80:
        del ledger[:-80]


def _task_requirement_text(task_payload: dict[str, Any]) -> str:
    acceptance = task_payload.get("acceptance")
    rows = acceptance if isinstance(acceptance, list) else []
    return "\n".join(
        [str(task_payload.get("objective") or ""), *[str(item) for item in rows]]
    )


def _requires_current_main_sha(task_payload: dict[str, Any]) -> bool:
    requirement = _task_requirement_text(task_payload).lower()
    return "current main" in requirement and ("sha" in requirement or "commit" in requirement)


def _task_repository(task_payload: dict[str, Any]) -> str | None:
    repository = str(task_payload.get("repository") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return None
    return repository


def _main_head_evidence_url(repository: str) -> str:
    return f"https://api.github.com/repos/{repository}/commits/main"


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


def reduce_observation(
    page: dict[str, Any],
    *,
    max_text: int = 1800,
    max_elements: int = 28,
) -> dict[str, Any]:
    keys = (
        "id",
        "role",
        "text",
        "label",
        "value",
    )
    useful_roles = {"button", "textbox", "combobox", "checkbox", "radio"}
    useful = [
        element
        for element in (page.get("elements") or [])
        if element.get("editable") or element.get("role") in useful_roles
    ]
    elements = []
    for element in useful[:max_elements]:
        compact = {
            key: element.get(key)
            for key in keys
            if element.get(key) not in (None, "", [], {})
        }
        attributes = element.get("attributes") if isinstance(element.get("attributes"), dict) else {}
        href = attributes.get("href")
        if isinstance(href, str) and href:
            compact["href"] = href
        for key in ("text", "label", "value"):
            value = compact.get(key)
            if isinstance(value, str) and len(value) > 160:
                compact[key] = value[:160]
        elements.append(compact)
    return {
        "url": page.get("url"),
        "title": page.get("title"),
        "generation": page.get("generation"),
        "pageText": str(page.get("pageText") or "")[:max_text],
        "elements": elements,
        "dialogs": page.get("dialogs") or [],
    }


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
            raise LauncherError(f"stale elementId {element_id!r} for generation {generation!r}")
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


def _element_for_action(observation: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    element_id = str(args.get("elementId") or "")
    if not element_id:
        return None
    return next((e for e in observation.get("elements") or [] if e.get("id") == element_id), None)


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


def action_allowed_before_claim(
    command: dict[str, Any],
    observation: dict[str, Any],
    issue_url: str,
) -> bool:
    action = str(command.get("action") or "")
    args = command.get("args") if isinstance(command.get("args"), dict) else {}
    if action in {"goto", "getPage", "scroll", "setViewport"}:
        return True
    if str(observation.get("url") or "").rstrip("/") != issue_url.rstrip("/"):
        return False
    element = _element_for_action(observation, args)
    if not element:
        return False
    if action in {"fill", "typeText"}:
        label = str(element.get("label") or "")
        return (
            label == "Use Markdown to format your comment"
            or label == "Markdown value"
            or "format your comment" in label
        )
    if action == "click":
        return element.get("role") == "button" and str(element.get("text") or "") == "Comment"
    return False


class Relay:
    def __init__(self, base: str, key: str, session: str):
        self.base = base.rstrip("/")
        self.key = key
        self.session = session
        self.headers = {
            "apikey": key,
            "Content-Type": "application/json",
        }

    def rest(
        self,
        method: str,
        path: str,
        value: Any = None,
        prefer: str | None = None,
    ) -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        return _json(method, f"{self.base}/rest/v1/{path}", value=value, headers=headers)

    def ready(self, timeout: int = 180) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest(
                "GET",
                f"browser_relay_sessions?session_id=eq.{self.session}&select=state,ended_at,last_error",
            ) or []
            if rows and rows[0].get("state") == "ready" and not rows[0].get("ended_at"):
                return
            time.sleep(1)
        raise LauncherError(f"Browser Agent session {self.session} did not become ready")

    def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: int = 75,
    ) -> Any:
        command_id = f"launcher-{uuid.uuid4().hex}"
        self.rest(
            "POST",
            "browser_relay_commands",
            {
                "session_id": self.session,
                "command_id": command_id,
                "action": action,
                "args": args or {},
            },
            "return=representation",
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest(
                "GET",
                f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{command_id}&select=status,result,error&limit=1",
            ) or []
            row = rows[0] if rows else {}
            if row.get("status") == "done":
                return row.get("result")
            if row.get("status") == "error":
                raise LauncherError(f"Browser Agent {action} failed: {row.get('error')}")
            time.sleep(0.5)
        raise LauncherError(f"Browser Agent {action} timed out")


def _find(
    page: dict[str, Any],
    *,
    label: str | None = None,
    text: str | None = None,
) -> dict[str, Any] | None:
    return next(
        (
            element
            for element in page.get("elements") or []
            if (label is not None and element.get("label") == label)
            or (text is not None and element.get("text") == text)
        ),
        None,
    )


def ask_gemini(relay: Relay, gemini_index: int, prompt_text: str) -> dict[str, Any]:
    relay.command("switchPage", {"index": gemini_index})
    relay.command("goto", {"url": GEMINI})
    page = relay.command("getPage", {})
    if dismiss := _find(page, text="Not now"):
        relay.command("click", {"elementId": dismiss["id"]})
        page = relay.command("getPage", {})
    box = _find(page, label="Enter a prompt for Gemini")
    if not box:
        raise LauncherError("Gemini prompt box unavailable")
    relay.command("fill", {"elementId": box["id"], "text": prompt_text})
    page = relay.command("getPage", {})
    baseline = len(_commands(str(page.get("pageText") or "")))
    send = _find(page, label="Send message")
    if not send:
        raise LauncherError("Gemini send button unavailable")
    relay.command("click", {"elementId": send["id"]})
    end = time.monotonic() + 90
    while time.monotonic() < end:
        time.sleep(1.5)
        page = relay.command("getPage", {})
        text = str(page.get("pageText") or "")
        if not _find(page, label="Stop response") and len(_commands(text)) > baseline:
            return _commands(text)[-1]
    raise LauncherError("Gemini response timed out")


def prompt(
    agent: str,
    task: str,
    issue: str,
    observation: dict[str, Any],
    step: int,
    feedback: str,
    claimed: bool,
    task_payload: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> str:
    claim_state = "verified" if claimed else "not verified"
    repository = _task_repository(task_payload)
    current_main_evidence_url = (
        _main_head_evidence_url(repository)
        if repository and _requires_current_main_sha(task_payload)
        else None
    )
    task_context = {
        "repository": task_payload.get("repository"),
        "objective": task_payload.get("objective"),
        "acceptance": task_payload.get("acceptance"),
        "context_refs": task_payload.get("context_refs"),
        "current_main_evidence_url": current_main_evidence_url,
    }
    evidence_urls: list[str] = []
    evidence_shas: list[str] = []
    for row in ledger:
        url = str(row.get("url") or "")
        if url and not url.startswith(GEMINI) and url.rstrip("/") != issue.rstrip("/"):
            if url not in evidence_urls:
                evidence_urls.append(url)
        for sha in SHA40_RE.findall(url + "\n" + str(row.get("pageText") or "")):
            if sha not in evidence_shas:
                evidence_shas.append(sha)
    evidence_context = {
        "visited_urls": evidence_urls[-8:],
        "observed_full_shas": evidence_shas[-8:],
    }
    return f"""Control the Browser Agent for {task}. Canonical Issue: {issue}
Run: {agent}. CLAIM: {claim_state}. The launcher writes CLAIM/RESULT; do not write those comments yourself.
The task definition is supplied below on every turn. After CLAIM is verified, do not return to the canonical Issue merely to reread the task. Continue verification from the current task page.
If TASK.current_main_evidence_url is present, visit that exact URL and use its top-level "sha" as the current main HEAD. A /commit/<sha> detail page alone does not prove current main.
Do only the supplied task, use only current-generation element IDs, and never expose secrets.
TASK:
{json.dumps(task_context, ensure_ascii=False, separators=(",", ":"))}
EVIDENCE ALREADY OBSERVED BY THE LAUNCHER:
{json.dumps(evidence_context, ensure_ascii=False, separators=(",", ":"))}
If the evidence above already proves every acceptance item, return finish now instead of revisiting pages.
Return exactly one JSON object, no prose:
{{"kind":"browser_action","action":"goto|getPage|click|scroll|setViewport","args":{{...}},"reason":"..."}}
Model-driven browser mutation is disabled pending Kernel capability receipts. click is allowed only for current-generation navigation links inside the task repository. For click use args={{"elementId":"gN-eM"}}.
or {{"kind":"finish","summary":"what was verified","artifacts":["immutable artifact"],"evidence":[{{"kind":"visited_url","value":"https://..."}},{{"kind":"extracted_fact","value":"observed fact"}}],"reason":"done"}}
Evidence must come from pages actually observed in the task browser, not from the Issue text or Gemini. If the task asks for a current commit SHA, put the full 40-character SHA in artifacts and evidence. If it asks whether a file exists, actually visit that file before finish.
or {{"kind":"wait","reason":"..."}}
Step {step}. Feedback: {feedback}
OBSERVATION:
{json.dumps(observation, ensure_ascii=False, separators=(",", ":"))}"""


def comments(token: str | None, issue: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        data = github(
            f"/repos/{BOARD}/issues/{issue}/comments?per_page=100&page={page}",
            token=token,
        )
        rows = data if isinstance(data, list) else []
        out.extend(rows)
        if len(rows) < 100:
            return out
    raise LauncherError("canonical comment history exceeded 100 pages")


def protocol_event_body(
    event_type: str,
    *,
    agent_id: str,
    task: str,
    summary: str,
    next_action: str | None,
    artifacts: list[str],
    idempotency_key: str | None = None,
) -> str:
    payload = {
        "type": event_type,
        "agent_id": agent_id,
        "task": task,
        "idempotency_key": idempotency_key or f"{agent_id}:{task}:{event_type.lower()}",
        "summary": summary,
        "next_action": next_action,
        "artifacts": artifacts,
    }
    return "<!-- ai-bb:v1 -->\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def append_issue_comment(
    relay: Relay,
    issue_url: str,
    body: str,
    *,
    page_index: int = 0,
) -> None:
    relay.command("switchPage", {"index": page_index})
    relay.command("goto", {"url": issue_url})
    page = relay.command("getPage", {})
    box = next(
        (
            element
            for element in page.get("elements") or []
            if element.get("role") == "textbox"
            and element.get("label") == "Add a comment"
        ),
        None,
    )
    if not box:
        raise LauncherError("canonical Issue comment box unavailable")
    relay.command("fill", {"elementId": box["id"], "text": body})
    page = relay.command("getPage", {})
    button = next(
        (
            element
            for element in page.get("elements") or []
            if element.get("role") == "button"
            and element.get("text") == "Comment"
        ),
        None,
    )
    if not button:
        raise LauncherError("canonical Issue Comment button unavailable")
    relay.command("click", {"elementId": button["id"]})
    relay.command("getPage", {})


def wait_for_protocol_event(
    token: str | None,
    issue_no: int,
    predicate,
    *,
    timeout: int = 15,
) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(comments(token, issue_no)):
            return True
        time.sleep(0.5)
    return False


def ensure_canonical_lease(
    *,
    token: str | None,
    issue_no: int,
    issue_url: str,
    relay: Relay,
    task: str,
    agent_id: str,
    phase: str,
    now: datetime | None = None,
) -> Any:
    state = _canonical_replay_state(comments(token, issue_no), task=task, now=now)
    if state.state != "claimed" or state.owner != agent_id:
        raise LauncherError(f"canonical lease ownership was lost before {phase}")

    if state.lease_status != "expiring":
        return state

    previous_expiry = state.lease_expires_at
    heartbeat_key = f"{agent_id}:{task}:heartbeat:{uuid.uuid4().hex}"
    heartbeat_body = protocol_event_body(
        "HEARTBEAT",
        agent_id=agent_id,
        task=task,
        summary=f"Renewing canonical lease before {phase}.",
        next_action="Continue the authorized task while retaining canonical ownership.",
        artifacts=[],
        idempotency_key=heartbeat_key,
    )
    opened = relay.command("newPage", {"url": issue_url})
    heartbeat_page_index = (
        int(opened.get("pageIndex", 0)) if isinstance(opened, dict) else 0
    )
    try:
        append_issue_comment(
            relay,
            issue_url,
            heartbeat_body,
            page_index=heartbeat_page_index,
        )
    finally:
        relay.command("switchPage", {"index": 0})

    def renewed(rows: list[dict[str, Any]]) -> bool:
        refreshed = _canonical_replay_state(rows, task=task, now=now)
        latest = refreshed.latest_owner_event
        return (
            refreshed.state == "claimed"
            and refreshed.owner == agent_id
            and refreshed.lease_expires_at != previous_expiry
            and latest is not None
            and latest.type == "HEARTBEAT"
            and latest.agent_id == agent_id
        )

    if not wait_for_protocol_event(token, issue_no, renewed):
        raise LauncherError(f"canonical HEARTBEAT was not verified before {phase}")

    refreshed = _canonical_replay_state(comments(token, issue_no), task=task, now=now)
    if refreshed.state != "claimed" or refreshed.owner != agent_id:
        raise LauncherError(f"canonical lease ownership was lost after HEARTBEAT before {phase}")
    if refreshed.lease_expires_at == previous_expiry:
        raise LauncherError(f"canonical HEARTBEAT did not extend the lease before {phase}")

    print(
        f"LEASE_RENEWED task={task} agent_id={agent_id} "
        f"lease_expires_at={refreshed.lease_expires_at}"
    )
    return refreshed


def run_worker(
    dispatch: dict[str, Any],
    *,
    token: str | None,
    relay: Relay,
    max_steps: int,
) -> int:
    task = str(dispatch["task"])
    issue_url = str(dispatch["source"]["issue_url"])
    issue_no = issue_number_from_dispatch(dispatch)
    issue_data = github(f"/repos/{BOARD}/issues/{issue_no}", token=token)
    task_payload = _task_payload_from_issue(str(issue_data.get("body") or "")) if isinstance(issue_data, dict) else {}
    initial_comments = comments(token, issue_no)
    if canonical_task_completed(initial_comments, task=task):
        print(f"ALREADY_COMPLETED task={task}")
        return 0

    agent = f"browser-chat-gemini-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    relay.ready()
    relay.command("start", {})
    relay.command("goto", {"url": issue_url})
    ledger: list[dict[str, Any]] = []
    initial_page = relay.command("getPage", {})
    _record_page_evidence(ledger, initial_page)

    claim_body = protocol_event_body(
        "CLAIM",
        agent_id=agent,
        task=task,
        summary="Claiming this task after canonical replay.",
        next_action="Read the task and begin the authorized implementation.",
        artifacts=[],
    )
    append_issue_comment(relay, issue_url, claim_body)
    if not wait_for_protocol_event(
        token,
        issue_no,
        lambda rows: canonical_claim_present(rows, task=task, agent_id=agent),
    ):
        raise LauncherError("canonical CLAIM was not verified after submission")

    opened = relay.command("newPage", {"url": GEMINI})
    gemini_index = int(opened.get("pageIndex", 1)) if isinstance(opened, dict) else 1
    time.sleep(2)
    feedback = (
        f"Browser Agent session {relay.session} is ready and canonical CLAIM is verified. "
        "Read the task, perform the work, verify artifacts, then return finish."
    )

    try:
        for step in range(1, max_steps + 1):
            ensure_canonical_lease(
                token=token,
                issue_no=issue_no,
                issue_url=issue_url,
                relay=relay,
                task=task,
                agent_id=agent,
                phase="task work",
            )
            claimed = True

            relay.command("switchPage", {"index": 0})
            page = relay.command("getPage", {})
            _record_page_evidence(ledger, page)
            observation = reduce_observation(page)
            model_command = ask_gemini(
                relay,
                gemini_index,
                prompt(
                    agent,
                    task,
                    issue_url,
                    observation,
                    step,
                    feedback,
                    claimed,
                    task_payload,
                    ledger,
                ),
            )

            if model_command.get("kind") == "wait":
                print(f"WAIT {task}: {model_command.get('reason', '')}")
                return 2

            if model_command.get("kind") == "finish":
                rejection = validate_finish_evidence(
                    model_command,
                    ledger,
                    task_payload,
                    issue_url,
                )
                if rejection:
                    feedback = rejection
                    continue
                ensure_canonical_lease(
                    token=token,
                    issue_no=issue_no,
                    issue_url=issue_url,
                    relay=relay,
                    task=task,
                    agent_id=agent,
                    phase="RESULT submission",
                )
                summary = str(model_command.get("summary") or "").strip()
                artifacts = model_command.get("artifacts")
                result_body = protocol_event_body(
                    "RESULT",
                    agent_id=agent,
                    task=task,
                    summary=summary,
                    next_action=None,
                    artifacts=[item.strip() for item in artifacts],
                )
                append_issue_comment(relay, issue_url, result_body)
                if wait_for_protocol_event(
                    token,
                    issue_no,
                    lambda rows: canonical_result_present(
                        rows,
                        task=task,
                        agent_id=agent,
                    ),
                ):
                    print(
                        f"RESULT_OK task={task} agent_id={agent} session_id={relay.session}"
                    )
                    return 0
                feedback = "RESULT submission was not verified on the canonical Issue; continue."
                continue

            if model_command.get("kind") != "browser_action":
                feedback = "invalid command kind; return one allowed command."
                continue

            try:
                action, args = _validate_model_action(
                    model_command,
                    observation,
                    task_payload=task_payload,
                    issue_url=issue_url,
                )
            except LauncherError as exc:
                feedback = str(exc)
                continue

            if action in MUTATING:
                ensure_canonical_lease(
                    token=token,
                    issue_no=issue_no,
                    issue_url=issue_url,
                    relay=relay,
                    task=task,
                    agent_id=agent,
                    phase="browser mutation",
                )

            relay.command("switchPage", {"index": 0})
            if action in {"click", "fill"}:
                fresh_page = relay.command("getPage", {})
                _record_page_evidence(ledger, fresh_page)
                try:
                    args = refresh_element_args(action, args, observation, fresh_page)
                except LauncherError as exc:
                    feedback = str(exc)
                    continue
            relay.command(action, args)
            page = relay.command("getPage", {})
            _record_page_evidence(ledger, page)
            feedback = (
                f"Executed {action}; fresh generation={page.get('generation')} "
                f"url={page.get('url')}."
            )

        raise LauncherError(f"max worker steps exceeded ({max_steps})")
    finally:
        try:
            relay.command("end", {})
        except Exception as exc:
            print(f"warning: browser end failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler-run-id", type=int)
    parser.add_argument("--plan-file")
    parser.add_argument("--plan-url")
    parser.add_argument("--session-id")
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--print-dispatch", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    plan = resolve_plan(
        token=token,
        scheduler_run_id=args.scheduler_run_id,
        plan_file=args.plan_file,
        plan_url=args.plan_url,
    )
    dispatch = validate_plan(plan)
    if not dispatch:
        print("NO_DISPATCH")
        return 0
    if args.print_dispatch:
        print(json.dumps(dispatch, ensure_ascii=False, indent=2))
        return 0

    if not args.session_id:
        raise LauncherError("--session-id is required for the production launcher")
    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    return run_worker(
        dispatch,
        token=token,
        relay=Relay(base, key, args.session_id),
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LauncherError as exc:
        print(f"LAUNCHER_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
