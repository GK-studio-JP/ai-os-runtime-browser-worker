from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

RUN_MARKER = "<!-- aios-dream-run:v1 -->"
CYCLE_MARKER = "<!-- aios-dream-cycle:v1 -->"
BOOTSTRAP_WATERMARK = "2026-10-02T00:00:00+09:00"
SETTLE_DELAY_SECONDS = 3600


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cycle_id(window_start: datetime, window_end: datetime) -> str:
    material = f"aios-nightly-dream:v1|{iso(window_start)}|{iso(window_end)}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _json_after_marker(body: str, marker: str) -> dict[str, Any] | None:
    if marker not in body:
        return None
    tail = body.split(marker, 1)[1]
    match = re.search(r"```json\s*(\{.*?\})\s*```", tail, re.S | re.I)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _fenced_json_objects(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in re.finditer(r"```json\s*(\{.*?\})\s*```", body, re.S | re.I):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def run_metadata(issue: dict[str, Any]) -> dict[str, Any] | None:
    body = str(issue.get("body") or "")
    value = _json_after_marker(body, RUN_MARKER)
    if value is not None and value.get("schema") == "aios-dream-run:v1":
        return value
    # Production Run #53 predates the explicit marker. Preserve its successful
    # watermark by accepting only an exact fenced Dream-run schema as fallback.
    for candidate in _fenced_json_objects(body):
        if candidate.get("schema") == "aios-dream-run:v1":
            return candidate
    return None


def cycle_states(comments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        value = _json_after_marker(body, CYCLE_MARKER)
        if value is not None and value.get("schema") == "aios-dream-cycle:v1":
            out.append(value)
            continue
        # The first production cycle state was emitted before CYCLE_MARKER was
        # introduced. Accept only the exact fenced cycle schema as compatibility.
        for candidate in _fenced_json_objects(body):
            if candidate.get("schema") == "aios-dream-cycle:v1":
                out.append(candidate)
                break
    return out


def has_result(comments: Iterable[dict[str, Any]], issue_number: int) -> bool:
    marker = "<!-- ai-bb:v1 -->"
    for comment in comments:
        body = str(comment.get("body") or "")
        if marker not in body:
            continue
        tail = body.split(marker, 1)[1]
        match = re.search(r"```json\s*(\{.*?\})\s*```", tail, re.S | re.I)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("type") == "RESULT"
            and value.get("task") == f"#{issue_number}"
            and value.get("next_action") is None
        ):
            return True
    return False


def latest_success(
    histories: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
    for issue, comments in histories:
        meta = run_metadata(issue)
        if meta is None:
            continue
        number = issue.get("number")
        if not isinstance(number, int) or not has_result(comments, number):
            continue
        for state in cycle_states(comments):
            if (
                state.get("status") == "completed"
                and state.get("cycle_id") == meta.get("cycle_id")
                and state.get("window_end") == meta.get("window_end")
            ):
                try:
                    end = parse_iso(str(meta["window_end"]))
                except Exception:
                    continue
                rows.append((end, issue, meta))
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])
    _end, issue, meta = rows[-1]
    return issue, meta


def resolve_window(
    histories: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
    automation_start: datetime,
    *,
    settle_delay_seconds: int = SETTLE_DELAY_SECONDS,
) -> tuple[datetime, datetime, int | None]:
    previous = latest_success(histories)
    if previous is None:
        start = parse_iso(BOOTSTRAP_WATERMARK)
        previous_issue = None
    else:
        issue, meta = previous
        start = parse_iso(str(meta["window_end"]))
        previous_issue = int(issue["number"])
    end = utc(automation_start) - timedelta(seconds=settle_delay_seconds)
    if start >= end:
        raise ValueError("Dream window is empty or negative")
    return start, end, previous_issue


def oldest_open_run(
    histories: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
    for issue, _comments in histories:
        if str(issue.get("state") or "").lower() != "open":
            continue
        meta = run_metadata(issue)
        if meta is None:
            continue
        created = str(issue.get("created_at") or "")
        try:
            created_at = parse_iso(created)
        except Exception:
            created_at = datetime.max.replace(tzinfo=timezone.utc)
        rows.append((created_at, issue, meta))
    if not rows:
        return None
    rows.sort(key=lambda item: (item[0], int(item[1].get("number") or 0)))
    _created, issue, meta = rows[0]
    return issue, meta


def run_issue_body(
    *,
    cycle: str,
    window_start: datetime,
    window_end: datetime,
    previous_success_issue: int | None,
    settle_delay_seconds: int = SETTLE_DELAY_SECONDS,
) -> str:
    task = {
        "process": "PROC-RUNTIME-BROWSER-WORKER",
        "repository": "GK-studio-JP/ai-bulletin-board",
        "objective": "Execute one AIOS Nightly Dream cycle through the dedicated Gemini runner.",
        "priority": 50,
        "contracts": ["aios-dream-cycle:v1"],
        "context_refs": [
            "GK-studio-JP/ai-os-projects/projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md",
            "GK-studio-JP/ai-os-projects/projects/aios-nightly-dream/DREAM_CONTRACT.md",
        ],
        "acceptance": [
            "Gemini performs Dream triage and deep synthesis.",
            "Deterministic validation gates all canonical publication decisions.",
            "Cycle state and RESULT are persisted before watermark advancement.",
        ],
        "blocked_by": [],
        "capabilities": ["browser-agent", "gemini-web", "github-ui"],
    }
    meta = {
        "schema": "aios-dream-run:v1",
        "cycle_id": cycle,
        "contract_version": 2,
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "settle_delay_seconds": settle_delay_seconds,
        "previous_success_issue": previous_success_issue or 0,
        "previous_success_window_end": iso(window_start) if previous_success_issue else None,
    }
    return (
        "<!-- ai-os-task:v1 -->\n```json\n"
        + json.dumps(task, ensure_ascii=False, indent=2)
        + "\n```\n\n"
        + RUN_MARKER
        + "\n```json\n"
        + json.dumps(meta, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def cycle_state_body(
    *,
    cycle: str,
    window_start: datetime,
    window_end: datetime,
    bundle_fingerprint: str,
    counts: dict[str, int],
    deferred: list[dict[str, Any]],
    memory_prs: list[str] | None = None,
    project_prs: list[str] | None = None,
) -> str:
    value = {
        "schema": "aios-dream-cycle:v1",
        "cycle_id": cycle,
        "status": "completed",
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "bundle_fingerprint": bundle_fingerprint,
        "counts": counts,
        "deferred": deferred,
        "memory_prs": memory_prs or [],
        "project_prs": project_prs or [],
    }
    return CYCLE_MARKER + "\n```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def deep_prompt(
    bundle: dict[str, Any],
    triage: list[dict[str, Any]],
    *,
    existing_memory: list[dict[str, Any]] | None = None,
) -> str:
    deep_tasks = {
        row["task"]: row
        for row in bundle.get("tasks", [])
        if isinstance(row, dict)
        and any(
            t.get("task") == row.get("task") and t.get("decision") == "deep"
            for t in triage
        )
    }
    payload = {
        "bundle_fingerprint": bundle.get("fingerprint"),
        "tasks": list(deep_tasks.values()),
        "triage": [row for row in triage if row.get("decision") == "deep"],
        "existing_memory": existing_memory or [],
    }
    return (
        "You are the deep synthesis stage of AIOS Nightly Dream. "
        "Use only INPUT; do not browse or call external tools. "
        "Return exactly one JSON object and no Markdown. "
        "Treat GitHub source evidence as authoritative and Gemini triage scores only as routing input. "
        "Do not infer psychology. Reconcile duplicate, update, contradiction, and reusable operational knowledge. "
        "Patterns require at least 3 independent source tasks. "
        "Every promote or supersede proposal must cite evidence refs present in its source task. "
        "Required shape: "
        '{"kind":"finish","schema":"aios-dream-report:v1","authoritative":false,'
        '"bundle_fingerprint":"sha256:...","proposals":['
        '{"schema":"aios-dream-proposal:v1","proposal_id":"...",'
        '"kind":"fact|lesson|pattern","decision":"promote|noop|defer|reject|supersede",'
        '"scope":"global|project|event_only","source_tasks":["#1"],'
        '"summary":"...","evidence":[{"task":"#1","ref":"comment:123"}]}]}. '
        "If there is no durable proposal, return an empty proposals array. "
        f"INPUT={canonical_json(payload)}"
    )
