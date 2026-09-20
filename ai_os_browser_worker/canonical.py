from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from ai_os_browser_worker.dispatch import BOARD, github
from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.relay import Relay
from ai_os_context.replay import replay as canonical_replay


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
