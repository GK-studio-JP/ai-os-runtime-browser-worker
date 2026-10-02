from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_os_browser_worker.dream_triage import deterministic_triage
from ai_os_browser_worker.relay import Relay
from ai_os_context.dream import normalize_dream_report
from ai_os_context.protocol import extract_event_payload
from ai_os_context.replay import replay
from browser_worker_launcher import (
    GEMINI,
    append_issue_comment,
    ask_gemini,
    protocol_event_body,
)
from dream_triage_runner import _CurrentPageRelay, run_triage

BOARD = "GK-studio-JP/ai-bulletin-board"
CONTROL_ISSUE = 52
BOOTSTRAP_WATERMARK = "2026-10-02T00:00:00+09:00"
SETTLE_DELAY_SECONDS = 3600
DREAM_TITLE_PREFIX = "[AIOS][aios-nightly-dream-run] "
JST = ZoneInfo("Asia/Tokyo")
JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


class DreamExecutionError(RuntimeError):
    pass


def _token() -> str:
    value = os.environ.get("GITHUB_TOKEN", "").strip()
    if not value:
        raise DreamExecutionError("GITHUB_TOKEN is required for authenticated canonical reads")
    return value


def _api(path: str, *, token: str | None = None) -> Any:
    target = path if path.startswith("https://") else f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aios-nightly-dream-worker/1",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token or _token()}",
    }
    request = urllib.request.Request(target, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise DreamExecutionError(f"authenticated GitHub read failed: {target}: {exc}") from exc


def _page(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 101):
        sep = "&" if "?" in path else "?"
        value = _api(f"{path}{sep}per_page=100&page={page}")
        if not isinstance(value, list):
            raise DreamExecutionError("GitHub paginated response must be a list")
        rows.extend(value)
        if len(value) < 100:
            return rows
    raise DreamExecutionError("GitHub pagination exceeded 100 pages")

def _board_path(suffix: str) -> str:
    return f"/repos/{BOARD}{suffix}"


def _issue(number: int) -> dict[str, Any]:
    value = _api(_board_path(f"/issues/{number}"))
    if not isinstance(value, dict):
        raise DreamExecutionError(f"issue #{number} response is not an object")
    return value


def _comments(number: int) -> list[dict[str, Any]]:
    return _page(_board_path(f"/issues/{number}/comments?"))


def _json_blocks(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in JSON_BLOCK_RE.findall(text or ""):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _dream_metadata(issue: dict[str, Any]) -> dict[str, Any] | None:
    for value in _json_blocks(str(issue.get("body") or "")):
        if value.get("schema") == "aios-dream-run:v1":
            return value
    return None


def _cycle_state(comments: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any] | None:
    matches = []
    for comment in comments:
        for value in _json_blocks(str(comment.get("body") or "")):
            if (
                value.get("schema") == "aios-dream-cycle:v1"
                and value.get("status") == "completed"
                and value.get("cycle_id") == metadata.get("cycle_id")
                and value.get("window_end") == metadata.get("window_end")
            ):
                matches.append(value)
    return matches[-1] if matches else None


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dream_runs(now: datetime) -> list[dict[str, Any]]:
    rows = _page(_board_path("/issues?state=all&sort=created&direction=asc&"))
    out = []
    for issue in rows:
        if "pull_request" in issue or not str(issue.get("title") or "").startswith(DREAM_TITLE_PREFIX):
            continue
        metadata = _dream_metadata(issue)
        if metadata is None:
            continue
        comments = _comments(int(issue["number"]))
        state = replay(issue, comments, now)
        cycle = _cycle_state(comments, metadata)
        out.append({
            "issue": issue,
            "comments": comments,
            "metadata": metadata,
            "replay": state,
            "cycle": cycle,
        })
    return out


def _latest_success(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    successful = [
        row for row in runs
        if row["replay"].state == "completed" and row["cycle"] is not None
    ]
    if not successful:
        return None
    return max(successful, key=lambda row: _parse_iso(row["metadata"]["window_end"]))


def _oldest_incomplete(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    incomplete = [row for row in runs if row["replay"].state != "completed"]
    if not incomplete:
        return None
    return min(incomplete, key=lambda row: _parse_iso(row["metadata"]["window_start"]))


def _find(page: dict[str, Any], *, label: str | None = None, prefix: str | None = None) -> dict[str, Any] | None:
    for element in page.get("elements") or []:
        if label is not None and element.get("label") == label:
            return element
        text = str(element.get("text") or element.get("label") or "")
        if prefix is not None and text.startswith(prefix):
            return element
    return None

def _observe(relay: Relay, *, deep: bool = False) -> dict[str, Any]:
    args = {"mode": "deep", "maxElements": 200, "maxFrames": 2} if deep else {
        "mode": "light", "maxElements": 120, "maxFrames": 1
    }
    return relay.command("getPage", args)


def _create_issue_ui(relay: Relay, title: str, body: str) -> int:
    relay.command("goto", {"url": f"https://github.com/{BOARD}/issues/new"})
    time.sleep(1)
    page = _observe(relay, deep=True)
    title_box = _find(page, label="Add a title")
    if not title_box:
        raise DreamExecutionError("Dream Run title textbox unavailable")
    relay.command("fill", {"elementId": title_box["id"], "text": title})

    page = _observe(relay, deep=True)
    body_box = _find(page, label="Markdown value")
    if not body_box:
        raise DreamExecutionError("Dream Run body textbox unavailable")
    relay.command("fill", {"elementId": body_box["id"], "text": body})

    page = _observe(relay, deep=True)
    create = _find(page, prefix="Create (")
    if not create:
        raise DreamExecutionError("Dream Run Create button unavailable")
    relay.command("click", {"elementId": create["id"]})
    page = _observe(relay)
    match = re.search(r"/issues/(\d+)(?:$|[/?#])", str(page.get("url") or ""))
    if not match:
        raise DreamExecutionError("Dream Run issue creation was not verified")
    return int(match.group(1))


def _append_event(relay: Relay, issue_no: int, event: dict[str, Any]) -> None:
    body = protocol_event_body(
        str(event["type"]),
        agent_id=str(event["agent_id"]),
        task=str(event["task"]),
        summary=str(event.get("summary") or ""),
        next_action=event.get("next_action"),
        artifacts=[str(item) for item in event.get("artifacts") or []],
        idempotency_key=str(event["idempotency_key"]),
    )
    append_issue_comment(relay, f"https://github.com/{BOARD}/issues/{issue_no}", body)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = _comments(issue_no)
        for row in rows:
            payload = extract_event_payload(str(row.get("body") or ""))
            if isinstance(payload, dict) and payload.get("idempotency_key") == event["idempotency_key"]:
                return
        time.sleep(0.75)
    raise DreamExecutionError(f"canonical {event['type']} was not verified on #{issue_no}")


def _append_cycle_state(relay: Relay, issue_no: int, state: dict[str, Any]) -> None:
    body = "```json\n" + json.dumps(state, ensure_ascii=False, indent=2) + "\n```"
    append_issue_comment(relay, f"https://github.com/{BOARD}/issues/{issue_no}", body)


def _control_state(now: datetime):
    issue = _issue(CONTROL_ISSUE)
    comments = _comments(CONTROL_ISSUE)
    return replay(issue, comments, now)


def _acquire_control(relay: Relay, agent_id: str, attempt: str, now: datetime) -> bool:
    state = _control_state(now)
    if not state.history_safe:
        raise DreamExecutionError(f"Control #52 history unsafe: {state.history_unsafe_reason}")
    if state.state == "claimed":
        if state.owner == agent_id:
            return True
        return False
    _append_event(relay, CONTROL_ISSUE, {
        "type": "CLAIM",
        "agent_id": agent_id,
        "task": "#52",
        "idempotency_key": f"{attempt}:control:claim",
        "summary": "Acquire Nightly Dream control before canonical cycle work.",
        "next_action": "Reconstruct and execute the oldest eligible Dream cycle.",
        "artifacts": [],
    })
    state = _control_state(datetime.now(timezone.utc))
    if state.state != "claimed" or state.owner != agent_id:
        raise DreamExecutionError("Control #52 CLAIM was not the live winning lease")
    return True


def _ensure_control(relay: Relay, agent_id: str, attempt: str) -> None:
    state = _control_state(datetime.now(timezone.utc))
    if state.state != "claimed" or state.owner != agent_id:
        raise DreamExecutionError("Control #52 ownership was lost")
    if state.lease_status == "expiring":
        _append_event(relay, CONTROL_ISSUE, {
            "type": "HEARTBEAT",
            "agent_id": agent_id,
            "task": "#52",
            "idempotency_key": f"{attempt}:control:heartbeat:{int(time.time())}",
            "summary": "Renew Nightly Dream control during active cycle.",
            "next_action": "Continue the current Dream cycle.",
            "artifacts": [],
        })


def _release_control(relay: Relay, agent_id: str, attempt: str, summary: str) -> None:
    state = _control_state(datetime.now(timezone.utc))
    if state.state != "claimed" or state.owner != agent_id:
        return
    _append_event(relay, CONTROL_ISSUE, {
        "type": "RELEASE",
        "agent_id": agent_id,
        "task": "#52",
        "idempotency_key": f"{attempt}:control:release",
        "summary": summary,
        "next_action": None,
        "artifacts": [],
    })

def _cycle_id(window_start: str, window_end: str) -> str:
    raw = f"aios-nightly-dream:v1|{window_start}|{window_end}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _run_body(metadata: dict[str, Any]) -> str:
    task = {
        "process": "PROC-AIOS",
        "repository": "GK-studio-JP/ai-os-projects",
        "objective": "Execute AIOS Nightly Dream production cycle",
        "priority": 100,
        "contracts": ["aios-dream-cycle:v1"],
        "context_refs": [
            "projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md",
            "projects/aios-nightly-dream/DREAM_CONTRACT.md",
            "GK-studio-JP/ai-bulletin-board#52",
        ],
        "acceptance": [
            "Acquire and verify Control #52 lease",
            "Reconstruct authenticated canonical source window",
            "Run Gemini triage and deep Dream for eligible work",
            "Validate the deterministic Dream report and publish gate",
            "Persist cycle summary and canonical RESULT only on safe completion",
        ],
        "blocked_by": [],
        "capabilities": [],
    }
    return (
        "<!-- ai-os-task:v1 -->\n```json\n"
        + json.dumps(task, ensure_ascii=False, indent=2)
        + "\n```\n\n```json\n"
        + json.dumps(metadata, ensure_ascii=False, indent=2)
        + "\n```\n\nProject: AIOS Nightly Dream\nMode: production\n"
    )


def _claim_run(relay: Relay, issue_no: int, agent_id: str, attempt: str) -> None:
    _append_event(relay, issue_no, {
        "type": "CLAIM",
        "agent_id": agent_id,
        "task": f"#{issue_no}",
        "idempotency_key": f"{attempt}:run:{issue_no}:claim",
        "summary": "Claim the selected Nightly Dream run under Control #52.",
        "next_action": "Reconstruct the fixed source window and execute Dream.",
        "artifacts": [f"issue:#{CONTROL_ISSUE}"],
    })
    state = replay(_issue(issue_no), _comments(issue_no), datetime.now(timezone.utc))
    if state.state != "claimed" or state.owner != agent_id:
        raise DreamExecutionError(f"Dream Run #{issue_no} CLAIM was not verified")


def _carryover(latest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not latest or not isinstance(latest.get("cycle"), dict):
        return []
    value = latest["cycle"].get("deferred")
    return [dict(item) for item in value] if isinstance(value, list) else []


def _task_numbers_from_carryover(rows: list[dict[str, Any]]) -> set[int]:
    out: set[int] = set()
    for row in rows:
        for task in row.get("source_tasks") or []:
            match = re.fullmatch(r"#(\d+)", str(task))
            if match:
                out.add(int(match.group(1)))
    return out


def _validate_raw_history(issue: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    for key in ("number", "created_at", "updated_at", "author_association", "body"):
        if key not in issue:
            raise DreamExecutionError(f"canonical issue field missing: {key}")
    for comment in comments:
        for key in ("id", "created_at", "updated_at", "author_association", "body", "user"):
            if key not in comment:
                raise DreamExecutionError(f"canonical comment field missing: {key}")


def _materialize_bundle(
    *,
    window_start: str,
    window_end: str,
    carryover: list[dict[str, Any]],
    workdir: Path,
) -> dict[str, Any]:
    since = urllib.parse.quote(_parse_iso(window_start).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), safe="")
    changed = _page(_board_path(f"/issues?state=all&since={since}&sort=updated&direction=asc&"))
    numbers = {
        int(row["number"]) for row in changed
        if "pull_request" not in row
    }
    numbers.update(_task_numbers_from_carryover(carryover))
    histories = []
    for number in sorted(numbers):
        issue = _issue(number)
        comments = _comments(number)
        _validate_raw_history(issue, comments)
        histories.append({"issue": issue, "comments": comments})
    snapshot = {
        "schema": "aios-dream-histories:v1",
        "repository": BOARD,
        "histories": histories,
    }
    snapshot_path = workdir / "histories.json"
    bundle_path = workdir / "bundle.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "ai_os_context.cli", "dream-bundle-files",
        "--histories-file", str(snapshot_path),
        "--window-start", window_start,
        "--window-end", window_end,
        "--settle-delay-seconds", str(SETTLE_DELAY_SECONDS),
        "--output", str(bundle_path),
    ], check=True)
    return json.loads(bundle_path.read_text(encoding="utf-8"))

def _deferred_entry(task: dict[str, Any], reason: str, detail: str, cycle_id: str) -> dict[str, Any]:
    return {
        "candidate_id": f"task:{task['task']}",
        "source_tasks": [task["task"]],
        "source_fingerprints": [task["source_fingerprint"]],
        "reason": reason,
        "detail": detail[:500],
        "last_evaluated_cycle": cycle_id,
        "evidence_refs": list(task.get("source_refs") or [])[:8],
    }


def _existing_memory_context(root: Path, tasks: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    index_path = root / "index" / "documents.json"
    if not index_path.exists():
        return []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    terms: set[str] = set()
    for task in tasks:
        text = f"{task.get('triage_capsule', {}).get('objective', '')} {task.get('triage_capsule', {}).get('result_summary', '')}".lower()
        terms.update(re.findall(r"[a-z0-9_-]{4,}", text))
    scored = []
    for row in index.get("documents", []):
        haystack = " ".join([
            str(row.get("id") or ""),
            str(row.get("title") or ""),
            " ".join(row.get("keywords") or []),
            str(row.get("path") or ""),
        ]).lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    out = []
    for _score, row in scored[:limit]:
        path = root / str(row.get("path") or "")
        content = path.read_text(encoding="utf-8")[:6000] if path.exists() else ""
        out.append({
            "id": row.get("id"),
            "title": row.get("title"),
            "path": row.get("path"),
            "content": content,
        })
    return out


def _deep_prompt(bundle: dict[str, Any], tasks: list[dict[str, Any]], memory: list[dict[str, Any]]) -> str:
    compact_tasks = [{
        "task": row["task"],
        "source_fingerprint": row["source_fingerprint"],
        "final_state": row["final_state"],
        "objective": row["triage_capsule"]["objective"],
        "result_summary": row["triage_capsule"]["result_summary"],
        "corrections": row["triage_capsule"]["corrections"],
        "source_refs": row["source_refs"],
    } for row in tasks]
    required = {
        "schema": "aios-dream-report:v1",
        "authoritative": False,
        "bundle_fingerprint": bundle["fingerprint"],
        "proposals": [{
            "schema": "aios-dream-proposal:v1",
            "proposal_id": "stable-id",
            "kind": "lesson",
            "decision": "noop",
            "scope": "event_only",
            "source_tasks": ["#123"],
            "summary": "concise operational knowledge",
            "evidence": [{"task": "#123", "ref": "exact source_refs value"}],
        }],
    }
    return (
        "You are the deep synthesis stage for AIOS Nightly Dream. "
        "Use only DEEP_TASKS and EXISTING_MEMORY below. Do not browse or call tools. "
        "Reconstruct final state, reject superseded conclusions, compare reusable operational knowledge, "
        "avoid psychology and secrets, and do not treat salience as evidence. "
        "Patterns require at least three independent source tasks. Choose exactly one allowed enum value for each kind, decision, and scope; REPORT_SHAPE uses concrete example values, not pipe-delimited placeholders. "
        "Return exactly one JSON object with kind=finish and report matching REPORT_SHAPE. "
        f"REPORT_SHAPE={json.dumps(required, ensure_ascii=False, separators=(',', ':'))} "
        f"DEEP_TASKS={json.dumps(compact_tasks, ensure_ascii=False, separators=(',', ':'))} "
        f"EXISTING_MEMORY={json.dumps(memory, ensure_ascii=False, separators=(',', ':'))}"
    )


def _deep_report(
    relay: Relay,
    bundle: dict[str, Any],
    deep_tasks: list[dict[str, Any]],
    memory_root: Path,
) -> tuple[dict[str, Any], str | None]:
    if not deep_tasks:
        raw_report = {
            "schema": "aios-dream-report:v1",
            "authoritative": False,
            "bundle_fingerprint": bundle["fingerprint"],
            "proposals": [],
        }
        return normalize_dream_report(bundle, raw_report), None
    memory = _existing_memory_context(memory_root, deep_tasks)
    started = relay.command("start", {})
    current_url = str(started.get("url") or "") if isinstance(started, dict) else ""
    try:
        raw = ask_gemini(
            _CurrentPageRelay(relay, current_url=current_url),
            0,
            _deep_prompt(bundle, deep_tasks, memory),
        )
        report = raw.get("report") if isinstance(raw, dict) and raw.get("kind") == "finish" else None
        if not isinstance(report, dict):
            raise DreamExecutionError("Gemini deep Dream returned no report")
        return normalize_dream_report(bundle, report), None
    except Exception as exc:
        empty = {
            "schema": "aios-dream-report:v1",
            "authoritative": False,
            "bundle_fingerprint": bundle["fingerprint"],
            "proposals": [],
        }
        return normalize_dream_report(bundle, empty), str(exc)

def _close_issue_best_effort(relay: Relay, issue_no: int) -> None:
    try:
        relay.command("goto", {"url": f"https://github.com/{BOARD}/issues/{issue_no}"})
        page = _observe(relay, deep=True)
        close = _find(page, prefix="Close issue")
        if close:
            relay.command("click", {"elementId": close["id"]})
            _observe(relay)
    except Exception as exc:
        print(f"warning: Dream Run close was not verified: {exc}", file=sys.stderr)


def execute(relay: Relay, *, workdir: Path, memory_root: Path) -> int:
    trigger = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID") or str(int(trigger.timestamp()))
    agent_id = f"gemini:nightly-dream:{run_id}"
    attempt = f"aios-dream:{run_id}"
    acquired = False
    dream_issue_no: int | None = None
    try:
        relay.ready()
        if not _acquire_control(relay, agent_id, attempt, trigger):
            print("AIOS_DREAM_SKIPPED live-control-owner")
            return 0
        acquired = True
        _ensure_control(relay, agent_id, attempt)

        runs = _dream_runs(trigger)
        latest = _latest_success(runs)
        incomplete = _oldest_incomplete(runs)
        carryover = _carryover(latest)

        if incomplete is not None:
            metadata = incomplete["metadata"]
            dream_issue_no = int(incomplete["issue"]["number"])
        else:
            window_start = (
                str(latest["metadata"]["window_end"])
                if latest is not None
                else BOOTSTRAP_WATERMARK
            )
            window_end_dt = trigger.astimezone(JST) - timedelta(seconds=SETTLE_DELAY_SECONDS)
            if window_end_dt <= _parse_iso(window_start):
                print("AIOS_DREAM_SKIPPED no-positive-window")
                return 0
            window_end = window_end_dt.isoformat(timespec="seconds")
            metadata = {
                "schema": "aios-dream-run:v1",
                "cycle_id": _cycle_id(window_start, window_end),
                "contract_version": 1,
                "window_start": window_start,
                "window_end": window_end,
                "settle_delay_seconds": SETTLE_DELAY_SECONDS,
                "previous_success_issue": int(latest["issue"]["number"]) if latest else 0,
                "previous_success_window_end": str(latest["metadata"]["window_end"]) if latest else None,
            }
            title = DREAM_TITLE_PREFIX + metadata["cycle_id"]
            dream_issue_no = _create_issue_ui(relay, title, _run_body(metadata))

        _claim_run(relay, dream_issue_no, agent_id, attempt)
        _ensure_control(relay, agent_id, attempt)

        bundle = _materialize_bundle(
            window_start=str(metadata["window_start"]),
            window_end=str(metadata["window_end"]),
            carryover=carryover,
            workdir=workdir,
        )
        _ensure_control(relay, agent_id, attempt)

        counts = {"skip": 0, "defer": 0, "deep": 0}
        deep_tasks: list[dict[str, Any]] = []
        deferred = [dict(row) for row in carryover]
        seen_deferred = {
            tuple(row.get("source_tasks") or [])
            for row in deferred
        }
        for task in bundle.get("tasks", []):
            # A selected task has new canonical evidence in this source window.
            # Drop stale single-task carryover for it before re-evaluation.
            deferred = [
                row for row in deferred
                if row.get("source_tasks") != [task["task"]]
            ]
            seen_deferred = {
                tuple(row.get("source_tasks") or [])
                for row in deferred
            }
            capsule = task["triage_capsule"]
            deterministic = deterministic_triage(capsule)
            if deterministic is not None:
                verdict = deterministic
            else:
                try:
                    verdict = run_triage(relay, capsule)
                except Exception as exc:
                    verdict = None
                    key = (task["task"],)
                    if key not in seen_deferred:
                        deferred.append(_deferred_entry(
                            task, "gemini_unavailable", str(exc), metadata["cycle_id"]
                        ))
                        seen_deferred.add(key)
                    counts["defer"] += 1
                    continue
            decision = verdict["decision"]
            counts[decision] += 1
            if decision == "deep":
                deep_tasks.append(task)
            elif decision == "defer":
                key = (task["task"],)
                if key not in seen_deferred:
                    deferred.append(_deferred_entry(
                        task,
                        str(verdict.get("reasons", ["defer"])[0]).split(":", 1)[0],
                        "; ".join(verdict.get("reasons") or []),
                        metadata["cycle_id"],
                    ))
                    seen_deferred.add(key)
            _ensure_control(relay, agent_id, attempt)

        report, deep_error = _deep_report(relay, bundle, deep_tasks, memory_root)
        if deep_error:
            for task in deep_tasks:
                key = (task["task"],)
                if key not in seen_deferred:
                    deferred.append(_deferred_entry(
                        task, "gemini_deep_unavailable", deep_error, metadata["cycle_id"]
                    ))
                    seen_deferred.add(key)

        proposal_counts = {key: 0 for key in ("promote", "noop", "defer", "reject", "supersede")}
        for proposal in report.get("proposals", []):
            proposal_counts[proposal["decision"]] += 1
            if proposal["decision"] in {"promote", "supersede"}:
                key = tuple(proposal["source_tasks"])
                deferred.append({
                    "candidate_id": proposal["proposal_id"],
                    "source_tasks": proposal["source_tasks"],
                    "source_fingerprints": [
                        task["source_fingerprint"] for task in bundle.get("tasks", [])
                        if task["task"] in proposal["source_tasks"]
                    ],
                    "reason": "publish_pending",
                    "detail": "Proposal passed Phase 2 validation; Phase 3 publication is deferred until the separated publish executor is enabled.",
                    "last_evaluated_cycle": metadata["cycle_id"],
                    "evidence_refs": [item["ref"] for item in proposal["evidence"]],
                })

        cycle_state = {
            "schema": "aios-dream-cycle:v1",
            "cycle_id": metadata["cycle_id"],
            "status": "completed",
            "window_start": metadata["window_start"],
            "window_end": metadata["window_end"],
            "bundle_fingerprint": bundle["fingerprint"],
            "counts": {
                "selected": int(bundle.get("counts", {}).get("selected", 0)),
                "triage_skip": counts["skip"],
                "triage_defer": counts["defer"],
                "triage_deep": counts["deep"],
                **proposal_counts,
            },
            "deferred": deferred,
            "memory_prs": [],
            "project_prs": [],
        }
        _append_cycle_state(relay, dream_issue_no, cycle_state)
        _append_event(relay, dream_issue_no, {
            "type": "RESULT",
            "agent_id": agent_id,
            "task": f"#{dream_issue_no}",
            "idempotency_key": f"{metadata['cycle_id']}:result:v1",
            "summary": (
                f"Nightly Dream completed from authenticated raw GitHub history. "
                f"selected={cycle_state['counts']['selected']} "
                f"deep={counts['deep']} deferred={len(deferred)}."
            ),
            "next_action": None,
            "artifacts": [f"bundle:{bundle['fingerprint']}"],
        })
        final = replay(_issue(dream_issue_no), _comments(dream_issue_no), datetime.now(timezone.utc))
        if final.state != "completed":
            raise DreamExecutionError("Dream Run RESULT did not produce canonical completed state")
        _close_issue_best_effort(relay, dream_issue_no)
        print(f"AIOS_DREAM_COMPLETED issue=#{dream_issue_no} cycle={metadata['cycle_id']}")
        return 0
    except Exception as exc:
        if dream_issue_no is not None:
            try:
                state = replay(_issue(dream_issue_no), _comments(dream_issue_no), datetime.now(timezone.utc))
                if state.state == "claimed" and state.owner == agent_id:
                    _append_event(relay, dream_issue_no, {
                        "type": "HANDOFF",
                        "agent_id": agent_id,
                        "task": f"#{dream_issue_no}",
                        "idempotency_key": f"{attempt}:handoff",
                        "summary": f"Nightly Dream stopped safely: {str(exc)[:700]}",
                        "next_action": "Reclaim and resume this same Dream cycle after correcting the failure.",
                        "artifacts": [],
                    })
                    _append_event(relay, dream_issue_no, {
                        "type": "RELEASE",
                        "agent_id": agent_id,
                        "task": f"#{dream_issue_no}",
                        "idempotency_key": f"{attempt}:run:release",
                        "summary": "Release incomplete Dream Run after HANDOFF.",
                        "next_action": "Resume this same cycle on the next eligible run.",
                        "artifacts": [],
                    })
            except Exception as handoff_exc:
                print(f"warning: Dream HANDOFF failed: {handoff_exc}", file=sys.stderr)
        raise
    finally:
        if acquired:
            try:
                _release_control(relay, agent_id, attempt, "Release Nightly Dream control after cycle attempt.")
            except Exception as release_exc:
                print(f"warning: Control #52 RELEASE failed: {release_exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--workdir", default="/tmp/aios-nightly-dream")
    parser.add_argument("--memory-root", default="memory")
    args = parser.parse_args(argv)
    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise DreamExecutionError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    return execute(
        Relay(base, key, args.session_id),
        workdir=workdir,
        memory_root=Path(args.memory_root),
    )


if __name__ == "__main__":
    raise SystemExit(main())