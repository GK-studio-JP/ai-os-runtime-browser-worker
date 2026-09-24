from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_os_browser_worker.dispatch import (
    BOARD,
    PROCESS,
    github,
    issue_number_from_dispatch,
    resolve_plan,
    validate_plan,
)
from ai_os_browser_worker.evidence import (
    SHA40_RE,
    _main_head_evidence_url,
    _requires_current_main_sha,
    validate_finish_evidence,
)
from ai_os_browser_worker.navigation_policy import (
    LauncherError,
    _task_repository,
    _validate_model_action,
    refresh_element_args,
    validate_mutation_receipt,
)
from ai_os_browser_worker.relay import Relay, RelayCommandError
from ai_os_browser_worker.safety import (
    LoopGuard,
    browser_state_fingerprint,
    loop_guard_args,
    receipt_fingerprint,
)
from ai_os_context.protocol import extract_task_envelope
from ai_os_context.replay import replay as canonical_replay

GEMINI = "https://gemini.google.com/app"
WORKER_DOC = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
BROWSER_DOC = "https://github.com/GK-studio-JP/browser-agent/blob/main/BROWSER_AGENT_INSTRUCTIONS.md"
GEMINI_TRANSIENT_ERRORS = (
    "I encountered an error doing what you asked",
    "I'm having a hard time fulfilling your request",
    "Sorry, something went wrong. Please try your request again.",
)
GEMINI_RESPONSE_TIMEOUT_SECONDS = 90
GEMINI_RETRY_SETTLE_TIMEOUT_SECONDS = 10
GEMINI_RETRY_SETTLE_POLL_SECONDS = 0.5
GEMINI_MALFORMED_STABLE_POLLS = 3
AI_OS_CANONICAL_OWNER_ACTOR_ENV = "AI_OS_CANONICAL_OWNER_ACTOR"


def canonical_owner_actor() -> str:
    actor = os.environ.get(AI_OS_CANONICAL_OWNER_ACTOR_ENV, "").strip()
    if not actor:
        raise LauncherError(f"{AI_OS_CANONICAL_OWNER_ACTOR_ENV} is required")
    return actor


def _is_gemini_transient_error(text: str) -> bool:
    return any(marker in text for marker in GEMINI_TRANSIENT_ERRORS)


def _balanced_json_object_spans(text: str) -> list[tuple[int, str]]:
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
                    out.append((start, text[start : index + 1]))
                    break
    return out


def _balanced_json_objects(text: str) -> list[str]:
    return [raw for _start, raw in _balanced_json_object_spans(text)]


def _escaped_json_commands(text: str) -> list[tuple[int, dict[str, Any]]]:
    out = []
    covered_until = -1
    for match in re.finditer(r'\{\s*\\\"kind\\\"', text):
        start = match.start()
        if start <= covered_until:
            continue

        cursor = start
        while True:
            end = text.find("}", cursor + 1)
            if end < 0:
                break
            raw = text[start : end + 1]
            try:
                decoded = json.loads('"' + raw + '"')
                obj = json.loads(decoded)
            except (json.JSONDecodeError, TypeError):
                cursor = end
                continue
            if isinstance(obj, dict) and obj.get("kind") in {"browser_action", "finish", "wait"}:
                out.append((start, obj))
                covered_until = end
                break
            cursor = end
    return out


def _commands(text: str) -> list[dict[str, Any]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    for start, raw in _balanced_json_object_spans(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("kind") in {"browser_action", "finish", "wait"}:
            rows.append((start, obj))

    rows.extend(_escaped_json_commands(text))
    rows.sort(key=lambda row: row[0])
    return [obj for _start, obj in rows]


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
    owner_actor: str,
    now: datetime | None = None,
) -> bool:
    state = _canonical_replay_state(comments, task=task, now=now)
    return (
        state.state == "claimed"
        and state.owner == agent_id
        and state.owner_actor == owner_actor
    )


def canonical_result_present(
    comments: list[dict[str, Any]],
    *,
    task: str,
    agent_id: str,
    owner_actor: str,
    now: datetime | None = None,
) -> bool:
    state = _canonical_replay_state(comments, task=task, now=now)
    return (
        state.state == "completed"
        and state.latest_result is not None
        and state.latest_result.agent_id == agent_id
        and state.latest_result.actor_login == owner_actor
    )


def _task_payload_from_issue(body: str) -> dict[str, Any]:
    value = extract_task_envelope(body)
    if isinstance(value, dict) and value.get("process") == PROCESS:
        return value
    return {}


def _record_tool_receipt(
    receipts: list[dict[str, Any]],
    receipt: dict[str, Any],
) -> None:
    receipts.append(receipt)
    if len(receipts) > 80:
        del receipts[:-80]
    print("TOOL_RECEIPT " + json.dumps(receipt, sort_keys=True))


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


def _source_edit_url(url: str, repository: str | None) -> bool:
    if not repository:
        return False
    prefix = f"https://github.com/{repository}/"
    if not str(url or "").startswith(prefix):
        return False
    suffix = str(url)[len(prefix):]
    return suffix.startswith("edit/") or suffix.startswith("new/")


def _initial_artifact_snapshots(
    token: str | None,
    repository: str | None,
) -> tuple[set[str], set[str]]:
    if not repository:
        return set(), set()

    pull_rows = github(
        f"/repos/{repository}/pulls?state=all&per_page=100",
        token=token,
    )
    pull_urls = {
        str(row.get("html_url") or "").rstrip("/")
        for row in (pull_rows if isinstance(pull_rows, list) else [])
        if isinstance(row, dict) and row.get("html_url")
    }

    run_data = github(
        f"/repos/{repository}/actions/runs?per_page=100",
        token=token,
    )
    workflow_rows = (
        run_data.get("workflow_runs")
        if isinstance(run_data, dict)
        else []
    )
    workflow_urls = {
        str(row.get("html_url") or "").rstrip("/")
        for row in (workflow_rows if isinstance(workflow_rows, list) else [])
        if isinstance(row, dict) and row.get("html_url")
    }
    return pull_urls, workflow_urls


def reduce_observation(
    page: dict[str, Any],
    *,
    max_text: int = 700,
    max_elements: int = 14,
) -> dict[str, Any]:
    keys = (
        "id",
        "role",
        "text",
        "label",
        "value",
    )
    useful_roles = {"button", "link", "textbox", "combobox", "checkbox", "radio"}
    useful = [
        element
        for element in (page.get("elements") or [])
        if element.get("editable") or element.get("role") in useful_roles
    ]
    priority_roles = {"textbox", "radio", "checkbox", "combobox"}
    priority = [
        element
        for element in useful
        if element.get("editable") or element.get("role") in priority_roles
    ]
    secondary = [
        element
        for element in useful
        if element not in priority and element.get("role") == "button"
    ]
    navigation = [
        element
        for element in useful
        if element not in priority and element not in secondary
    ]
    selected = (priority + secondary + navigation)[:max_elements]
    elements = []
    for element in selected:
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
            if isinstance(value, str) and len(value) > 120:
                compact[key] = value[:120]
        elements.append(compact)
    return {
        "url": page.get("url"),
        "title": page.get("title"),
        "generation": page.get("generation"),
        "pageText": str(page.get("pageText") or "")[:max_text],
        "elements": elements,
        "dialogs": page.get("dialogs") or [],
    }


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


def _has_new_gemini_response(baseline_text: str, current_text: str) -> bool:
    return current_text.count("Gemini said") > baseline_text.count("Gemini said")


def _settle_gemini_before_retry(
    relay: Relay,
    page: dict[str, Any],
) -> dict[str, Any]:
    stop = _find(page, label="Stop response")
    if not stop:
        return page

    relay.command("click", {"elementId": stop["id"]})
    end = time.monotonic() + GEMINI_RETRY_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < end:
        time.sleep(GEMINI_RETRY_SETTLE_POLL_SECONDS)
        page = relay.command("getPage", {})
        if not _find(page, label="Stop response"):
            return page
    raise LauncherError("Gemini previous response did not stop before retry")


def _wait_for_gemini_retry_send(
    relay: Relay,
    page: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    send = _find(page, label="Send message")
    if send:
        return page, send

    end = time.monotonic() + GEMINI_RETRY_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < end:
        time.sleep(GEMINI_RETRY_SETTLE_POLL_SECONDS)
        page = relay.command("getPage", {})
        send = _find(page, label="Send message")
        if send:
            return page, send
    raise LauncherError("Gemini retry input did not become sendable")


def ask_gemini(relay: Relay, gemini_index: int, prompt_text: str) -> dict[str, Any]:
    attempt_prompt = prompt_text
    relay.command("switchPage", {"index": gemini_index})
    relay.command("goto", {"url": GEMINI})
    for attempt in range(2):
        relay.command("switchPage", {"index": gemini_index})
        page = relay.command("getPage", {})
        if dismiss := _find(page, text="Not now"):
            relay.command("click", {"elementId": dismiss["id"]})
            page = relay.command("getPage", {})
        if attempt > 0:
            page = _settle_gemini_before_retry(relay, page)
        box = _find(page, label="Enter a prompt for Gemini")
        if not box:
            raise LauncherError("Gemini prompt box unavailable")
        relay.command("fill", {"elementId": box["id"], "text": attempt_prompt})
        page = relay.command("getPage", {})
        baseline_text = str(page.get("pageText") or "")
        baseline = len(_commands(baseline_text))
        send = _find(page, label="Send message")
        if not send:
            if attempt == 0:
                raise LauncherError("Gemini send button unavailable")
            page, send = _wait_for_gemini_retry_send(relay, page)
        relay.command("click", {"elementId": send["id"]})

        end = time.monotonic() + GEMINI_RESPONSE_TIMEOUT_SECONDS
        retry_reason: str | None = None
        malformed_snapshot: str | None = None
        malformed_stable_polls = 0
        while time.monotonic() < end:
            time.sleep(1.5)
            page = relay.command("getPage", {})
            text = str(page.get("pageText") or "")
            stopped = not _find(page, label="Stop response")
            commands = _commands(text)
            if stopped and len(commands) > baseline:
                return commands[-1]
            if stopped and _is_gemini_transient_error(text):
                retry_reason = "transient"
                break
            if stopped and _has_new_gemini_response(baseline_text, text):
                response_fragment = text.rsplit("Gemini said", 1)[-1]
                if response_fragment != malformed_snapshot:
                    malformed_snapshot = response_fragment
                    malformed_stable_polls = 1
                    continue
                malformed_stable_polls += 1
                if malformed_stable_polls < GEMINI_MALFORMED_STABLE_POLLS:
                    continue
                retry_reason = "malformed"
                break

        if retry_reason is None:
            retry_reason = "timeout"

        if attempt == 0:
            if retry_reason == "malformed":
                retry_instruction = (
                    "\nRETRY: Your previous response was visible but was not valid parseable JSON. "
                    "Return exactly one valid JSON object matching the requested launcher schema. "
                    "For action=fill with field=file_contents, args.value must be valid JSON: "
                    "encode each source literal double quote as \\u0022, each source literal backslash as \\u005c, "
                    "and each line break as \\n; never emit a raw source double quote inside args.value. "
                    "Use valid JSON escapes in every other JSON string. "
                    "Do not use Markdown fences, Gemini web search, or external tools."
                )
            elif retry_reason == "transient":
                retry_instruction = (
                    "\nRETRY: Do not use Gemini web search or external tools. "
                    "Use only TASK, OBSERVED, and OBSERVATION. Return one JSON object."
                )
            else:
                retry_instruction = (
                    "\nRETRY: Your previous request produced no visible Gemini response before the timeout. "
                    "Return exactly one valid JSON object matching the requested launcher schema. "
                    "Do not use Markdown fences, Gemini web search, or external tools."
                )
            attempt_prompt = prompt_text + "\n\n" + retry_instruction.lstrip()
            continue
        if retry_reason == "malformed":
            raise LauncherError("Gemini returned malformed launcher command after retry")
        if retry_reason == "transient":
            raise LauncherError("Gemini logged-out response failed after retry")
        raise LauncherError("Gemini response timed out after retry")

    raise LauncherError("Gemini returned no launcher command")

def _editor_progress(observation: dict[str, Any]) -> dict[str, str] | None:
    url = str(observation.get("url") or "")
    if "/new/" not in url:
        return None
    for element in observation.get("elements") or []:
        if element.get("role") != "textbox":
            continue
        label = str(element.get("label") or "")
        match = re.match(r"Editing (.+?) file contents(?:\s|$)", label)
        if match:
            return {
                "file_name": match.group(1),
                "next_required": "fill:file_contents",
            }
    return None


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
    mutation_enabled: bool,
    source_mutation_performed: bool = False,
) -> str:
    claim_state = "verified" if claimed else "not-verified"
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
        "contracts": task_payload.get("contracts"),
        "context_refs": task_payload.get("context_refs"),
        "current_main_evidence_url": current_main_evidence_url,
        "source_mutation_performed": source_mutation_performed,
        "mutation_entry_hint": (
            f"https://github.com/{repository}/edit/main/<existing-path> or "
            f"https://github.com/{repository}/new/main"
            if repository and mutation_enabled
            else None
        ),
        "editor_progress": _editor_progress(observation),
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
        "visited_urls": evidence_urls[-5:],
        "observed_full_shas": evidence_shas[-5:],
    }
    if mutation_enabled:
        mutation_policy = (
            "Kernel branch/PR receipt active: fill/click only in the task repo. "
            "Never commit to main or merge. First source change from main must use a new branch and PR."
        )
        action_contract = (
            '{"kind":"browser_action","action":"goto|getPage|click|fill|scroll|setViewport",'
            '"args":{...},"reason":"..."}'
        )
    else:
        mutation_policy = "No mutation receipt: read-only navigation only."
        action_contract = (
            '{"kind":"browser_action","action":"goto|getPage|click|scroll|setViewport",'
            '"args":{...},"reason":"..."}'
        )

    return f"""Browser Agent task {task}. Issue={issue}. Run={agent}. CLAIM={claim_state}.
TASK={json.dumps(task_context, ensure_ascii=False, separators=(",", ":"))}
OBSERVED={json.dumps(evidence_context, ensure_ascii=False, separators=(",", ":"))}
POLICY={mutation_policy}
Rules: never expose secrets; evidence must come from observed task pages. Do not use Gemini web search or external tools; all required state is in this prompt.
For click/fill, use the current-generation elementId exactly when possible. Never return click/fill with no target. For GitHub editor fill, if preserving elementId is difficult, args may use field="file_name", field="file_contents", or field="commit_message"; the Runtime resolves only one safe current-generation textbox. For a mutation click, args may use the exact visible label as label/target and the Runtime resolves only one current-generation allowed control.
For action=fill with field="file_contents", args.value must be a valid JSON string: encode every source literal double quote as \\u0022, every source literal backslash as \\u005c, and each line break as \\n; never place a raw source double quote inside args.value.
If objective/acceptance asks to add, implement, fix, update, or change something and observed pages do not already prove it exists, perform the smallest authorized branch/PR mutation before finish. README/repository listings/unrelated or pre-existing PRs are not implementation evidence.
For GitHub implementation, do not browse /pulls first. Navigate directly to the edit URL for an existing path or the new-file URL from TASK.mutation_entry_hint. On a new-file page first fill field="file_name", then fill field="file_contents", select the new-branch radio, Propose changes, then Create pull request.
If TASK.editor_progress.next_required is "fill:file_contents", the file name is already set. Do not fill file_name again; the next mutation must fill field="file_contents" with the complete source text.
For SHA evidence use the exact observed 40-character SHA alone as extracted_fact.value, not a sentence containing it.
If current_main_evidence_url is present, visit it and use its top-level sha.
Return one JSON object only:
{action_contract}
or {{"kind":"finish","summary":"verified result","artifacts":["immutable artifact"],"evidence":[{{"kind":"visited_url","value":"https://..."}},{{"kind":"extracted_fact","value":"observed fact"}}],"reason":"done"}}
or {{"kind":"wait","reason":"..."}}
Step={step}; feedback={feedback}
OBSERVATION={json.dumps(observation, ensure_ascii=False, separators=(",", ":"))}"""

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
    return (
        "<!-- ai-bb:v1 -->\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```"
    )


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
    owner_actor: str,
    phase: str,
    now: datetime | None = None,
) -> Any:
    state = _canonical_replay_state(comments(token, issue_no), task=task, now=now)
    if (
        state.state != "claimed"
        or state.owner != agent_id
        or state.owner_actor != owner_actor
    ):
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
            and refreshed.owner_actor == owner_actor
            and refreshed.lease_expires_at != previous_expiry
            and latest is not None
            and latest.type == "HEARTBEAT"
            and latest.agent_id == agent_id
            and latest.actor_login == owner_actor
        )

    if not wait_for_protocol_event(token, issue_no, renewed):
        raise LauncherError(f"canonical HEARTBEAT was not verified before {phase}")

    refreshed = _canonical_replay_state(comments(token, issue_no), task=task, now=now)
    if (
        refreshed.state != "claimed"
        or refreshed.owner != agent_id
        or refreshed.owner_actor != owner_actor
    ):
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
    mutation_receipt: dict[str, Any] | None = None,
) -> int:
    task = str(dispatch["task"])
    issue_url = str(dispatch["source"]["issue_url"])
    issue_no = issue_number_from_dispatch(dispatch)
    issue_data = github(f"/repos/{BOARD}/issues/{issue_no}", token=token)
    task_payload = _task_payload_from_issue(str(issue_data.get("body") or "")) if isinstance(issue_data, dict) else {}
    repository = _task_repository(task_payload)
    preexisting_pull_urls, preexisting_workflow_urls = _initial_artifact_snapshots(
        token,
        repository,
    )
    initial_comments = comments(token, issue_no)
    if canonical_task_completed(initial_comments, task=task):
        print(f"ALREADY_COMPLETED task={task}")
        return 0

    agent = f"browser-chat-gemini-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    owner_actor = canonical_owner_actor()
    loop_guard = LoopGuard()
    loop_capped_reason: str | None = None
    tool_receipts: list[dict[str, Any]] = []
    source_mutation_performed = False
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
        lambda rows: canonical_claim_present(
            rows,
            task=task,
            agent_id=agent,
            owner_actor=owner_actor,
        ),
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
                owner_actor=owner_actor,
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
                    mutation_receipt is not None,
                    source_mutation_performed,
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
                    source_mutation_performed=source_mutation_performed,
                    require_new_pr=mutation_receipt is not None,
                    preexisting_pull_urls=preexisting_pull_urls,
                    preexisting_workflow_urls=preexisting_workflow_urls,
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
                    owner_actor=owner_actor,
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
                        owner_actor=owner_actor,
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

            if loop_capped_reason:
                print(
                    f"WAIT {task}: browser loop capped ({loop_capped_reason}); "
                    "additional model browser action was not executed"
                )
                return 2

            try:
                action, args = _validate_model_action(
                    model_command,
                    page,
                    task_payload=task_payload,
                    issue_url=issue_url,
                    mutation_receipt=mutation_receipt,
                )
            except LauncherError as exc:
                feedback = str(exc)
                continue

            relay.command("switchPage", {"index": 0})
            guard_page = page
            if action in {"click", "fill"}:
                fresh_page = relay.command("getPage", {})
                _record_page_evidence(ledger, fresh_page)
                try:
                    args = refresh_element_args(action, args, page, fresh_page)
                except LauncherError as exc:
                    feedback = str(exc)
                    continue
                guard_page = fresh_page

            is_mutation = action == "fill"
            if action == "click":
                element_id = str(args.get("elementId") or "")
                clicked = next(
                    (
                        element
                        for element in guard_page.get("elements") or []
                        if element.get("id") == element_id
                    ),
                    None,
                )
                is_mutation = bool(clicked and clicked.get("role") != "link")
            if is_mutation:
                ensure_canonical_lease(
                    token=token,
                    issue_no=issue_no,
                    issue_url=issue_url,
                    relay=relay,
                    task=task,
                    agent_id=agent,
                    owner_actor=owner_actor,
                    phase="browser mutation",
                )

            guarded_args = loop_guard_args(action, args, guard_page)
            try:
                _, receipt = relay.command_with_receipt(
                    action,
                    args,
                    run_id=agent,
                    step=step,
                )
            except RelayCommandError as exc:
                _record_tool_receipt(tool_receipts, exc.receipt)
                feedback = (
                    f"{exc}; execution evidence receipt="
                    f"{receipt_fingerprint(exc.receipt)}."
                )
                continue

            _record_tool_receipt(tool_receipts, receipt)
            if (
                action == "fill"
                and _source_edit_url(str(guard_page.get("url") or ""), repository)
            ):
                source_mutation_performed = True
            page = relay.command("getPage", {})
            _record_page_evidence(ledger, page)
            decision = loop_guard.observe_tool_call(
                agent,
                action,
                guarded_args,
                state_fingerprint=browser_state_fingerprint(page),
            )
            receipt_id = receipt_fingerprint(receipt)
            if decision.stop:
                loop_capped_reason = (
                    f"{decision.reason_code} tool={action} "
                    f"count={decision.count} threshold={decision.threshold}"
                )
                feedback = (
                    f"Executed {action}; browser loop capped after this action: "
                    f"{loop_capped_reason}; receipt={receipt_id}; "
                    f"fresh generation={page.get('generation')} url={page.get('url')}. "
                    "No further model browser actions will execute. "
                    "Return finish if existing observed evidence satisfies acceptance; "
                    "otherwise return wait."
                )
                continue
            if decision.action == "warn":
                feedback = (
                    f"Executed {action}; loop warning={decision.reason_code} "
                    f"count={decision.count}/{decision.threshold}; "
                    f"receipt={receipt_id}; fresh generation={page.get('generation')} "
                    f"url={page.get('url')}."
                )
            else:
                feedback = (
                    f"Executed {action}; receipt={receipt_id}; "
                    f"fresh generation={page.get('generation')} "
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
    parser.add_argument("--mutation-receipt")
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

    mutation_receipt = None
    if args.mutation_receipt:
        try:
            raw_receipt = json.loads(Path(args.mutation_receipt).read_text(encoding="utf-8"))
        except Exception as exc:
            raise LauncherError(f"failed to read Kernel mutation receipt: {exc}") from exc
        mutation_receipt = validate_mutation_receipt(
            raw_receipt,
            dispatch=dispatch,
            plan_fingerprint=str(plan.get("fingerprint") or ""),
        )

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
        mutation_receipt=mutation_receipt,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LauncherError as exc:
        print(f"LAUNCHER_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
