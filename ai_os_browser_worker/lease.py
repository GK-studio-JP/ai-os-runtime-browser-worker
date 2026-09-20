from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ai_os_browser_worker.canonical import (
    _canonical_replay_state,
    append_issue_comment,
    comments,
    protocol_event_body,
    wait_for_protocol_event,
)
from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.relay import Relay


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
        raise LauncherError(
            f"canonical lease ownership was lost after HEARTBEAT before {phase}"
        )
    if refreshed.lease_expires_at == previous_expiry:
        raise LauncherError(
            f"canonical HEARTBEAT did not extend the lease before {phase}"
        )

    print(
        f"LEASE_RENEWED task={task} agent_id={agent_id} "
        f"lease_expires_at={refreshed.lease_expires_at}"
    )
    return refreshed
