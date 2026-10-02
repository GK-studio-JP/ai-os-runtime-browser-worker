from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_os_browser_worker.dispatch import BOARD, github
from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import (
    ask_gemini,
    append_issue_comment,
    protocol_event_body,
)
from dream_triage_runner import run_triage
from ai_os_context.replay import replay as canonical_replay

CONTROL_ISSUE = 52
SETTLE_DELAY_SECONDS = 3600
BOOTSTRAP_WATERMARK = "2026-10-02T00:00:00+09:00"
RUN_TITLE_PREFIX = "[AIOS][aios-nightly-dream-run] "
PROCESS = "PROC-AIOS-NIGHTLY-DREAM"
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(JST).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_objects(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S | re.I)
    marker = (text or "").split("<!-- ai-bb:v1 -->", 1)
    if len(marker) == 2 and not candidates:
        candidates.append(marker[1].strip())
    for raw in candidates:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _run_meta(issue: dict[str, Any]) -> dict[str, Any] | None:
    for value in _json_objects(str(issue.get("body") or "")):
        if value.get("schema") == "aios-dream-run:v1":
            return value
    return None


def _cycle_states(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for comment in comments:
        for value in _json_objects(str(comment.get("body") or "")):
            if value.get("schema") == "aios-dream-cycle:v1":
                out.append(value)
    return out


def cycle_id(window_start: str, window_end: str) -> str:
    raw = f"aios-nightly-dream:v1|{window_start}|{window_end}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue(number: int) -> dict[str, Any]:
    value = github(f"/repos/{BOARD}/issues/{number}")
    if not isinstance(value, dict):
        raise LauncherError(f"invalid GitHub issue payload for #{number}")
    return value


def _comments(number: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        rows = github(f"/repos/{BOARD}/issues/{number}/comments?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise LauncherError(f"invalid GitHub comments payload for #{number}")
        out.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < 100:
            issue = _issue(number)
            expected = issue.get("comments")
            if isinstance(expected, int) and expected != len(out):
                raise LauncherError(
                    f"source-inconsistent: #{number} expected {expected} comments, got {len(out)}"
                )
            return out
    raise LauncherError(f"source-inconsistent: comment pagination exceeded for #{number}")


def _all_issues(*, since: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        query = f"state=all&per_page=100&page={page}"
        if since:
            query += "&since=" + urllib.parse.quote(since, safe=":+")
        rows = github(f"/repos/{BOARD}/issues?{query}")
        if not isinstance(rows, list):
            raise LauncherError("invalid GitHub issue-list payload")
        rows = [row for row in rows if isinstance(row, dict) and "pull_request" not in row]
        out.extend(rows)
        if len(rows) < 100:
            return out
    raise LauncherError("source-inconsistent: issue pagination exceeded")


def _validate_history(issue: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    for key in ("number", "created_at", "updated_at", "body"):
        if issue.get(key) is None:
            raise LauncherError(f"source-inconsistent: issue #{issue.get('number')} missing {key}")
    for comment in comments:
        for key in ("id", "created_at", "body", "user", "author_association"):
            if comment.get(key) is None:
                raise LauncherError(
                    f"source-inconsistent: issue #{issue.get('number')} comment missing {key}"
                )


def _find_element(
    page: dict[str, Any],
    *,
    role: str | None = None,
    texts: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    for element in page.get("elements") or []:
        if role and element.get("role") != role:
            continue
        text = str(element.get("text") or "").strip().lower()
        label = str(element.get("label") or "").strip().lower()
        if texts and not any(value.lower() in text for value in texts):
            continue
        if labels and not any(value.lower() in label for value in labels):
            continue
        if element.get("id"):
            return element
    return None


def _create_issue(relay: Relay, title: str, body: str) -> int:
    url = f"https://github.com/{BOARD}/issues/new"
    relay.command("goto", {"url": url, "mode": "light", "maxElements": 160, "maxFrames": 1})
    page = relay.command("getPage", {"mode": "light", "maxElements": 160, "maxFrames": 1})
    title_box = _find_element(page, role="textbox", labels=("title", "add a title"))
    textboxes = [
        item for item in page.get("elements") or []
        if item.get("role") == "textbox" and item.get("id")
        and "search" not in str(item.get("label") or "").lower()
    ]
    if title_box is None and textboxes:
        title_box = textboxes[0]
    if title_box is None:
        raise LauncherError("Dream Run issue title box unavailable")
    relay.command("fill", {"elementId": title_box["id"], "text": title})

    page = relay.command("getPage", {"mode": "light", "maxElements": 180, "maxFrames": 1})
    body_box = _find_element(
        page,
        role="textbox",
        labels=("description", "comment", "markdown", "body"),
    )
    if body_box is None:
        textboxes = [
            item for item in page.get("elements") or []
            if item.get("role") == "textbox" and item.get("id")
            and item.get("id") != title_box.get("id")
            and "search" not in str(item.get("label") or "").lower()
        ]
        if textboxes:
            body_box = textboxes[-1]
    if body_box is None:
        raise LauncherError("Dream Run issue body box unavailable")
    relay.command("fill", {"elementId": body_box["id"], "text": body})

    page = relay.command("getPage", {"mode": "light", "maxElements": 180, "maxFrames": 1})
    submit = _find_element(
        page,
        role="button",
        texts=("submit new issue", "create issue"),
    )
    if submit is None:
        raise LauncherError("Dream Run issue submit button unavailable")
    relay.command("click", {"elementId": submit["id"]})
    page = relay.command("getPage", {"mode": "light", "maxElements": 120, "maxFrames": 1})
    match = re.search(r"/issues/(\d+)(?:$|[?#])", str(page.get("url") or ""))
    if not match:
        raise LauncherError("Dream Run issue creation was not observed")
    return int(match.group(1))


def _close_issue(relay: Relay, number: int) -> None:
    url = f"https://github.com/{BOARD}/issues/{number}"
    relay.command("goto", {"url": url, "mode": "light", "maxElements": 180, "maxFrames": 1})
    page = relay.command("getPage", {"mode": "light", "maxElements": 180, "maxFrames": 1})
    button = _find_element(page, role="button", texts=("close issue",))
    if button is not None:
        relay.command("click", {"elementId": button["id"]})
    else:
        relay.command("clickText", {"text": "Close issue", "exact": False})
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if _issue(number).get("state") == "closed":
            return
        time.sleep(1)
    raise LauncherError(f"Dream Run #{number} did not close")


def _wait_owner(number: int, agent_id: str, timeout: int = 20) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        issue = _issue(number)
        comments = _comments(number)
        state = canonical_replay(issue, comments, datetime.now(timezone.utc))
        if state.state == "claimed" and state.owner == agent_id:
            return state
        time.sleep(1)
    raise LauncherError(f"canonical CLAIM was not verified for #{number}")


def _append_event(
    relay: Relay,
    number: int,
    event_type: str,
    *,
    agent_id: str,
    summary: str,
    next_action: str | None,
    artifacts: list[str],
    idempotency_key: str,
) -> None:
    body = protocol_event_body(
        event_type,
        agent_id=agent_id,
        task=f"#{number}",
        summary=summary,
        next_action=next_action,
        artifacts=artifacts,
        idempotency_key=idempotency_key,
    )
    append_issue_comment(relay, f"https://github.com/{BOARD}/issues/{number}", body)
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if any(idempotency_key in str(row.get("body") or "") for row in _comments(number)):
            return
        time.sleep(1)
    raise LauncherError(f"{event_type} write was not verified for #{number}")


def _append_json_comment(relay: Relay, number: int, value: dict[str, Any]) -> None:
    body = "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"
    append_issue_comment(relay, f"https://github.com/{BOARD}/issues/{number}", body)
    marker = str(value.get("cycle_id") or value.get("schema") or "")
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if any(marker and marker in str(row.get("body") or "") for row in _comments(number)):
            return
        time.sleep(1)
    raise LauncherError(f"cycle-state write was not verified for #{number}")


def _successful_runs(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for issue in issues:
        if not str(issue.get("title") or "").startswith(RUN_TITLE_PREFIX):
            continue
        meta = _run_meta(issue)
        if not meta:
            continue
        comments = _comments(int(issue["number"]))
        states = [
            row for row in _cycle_states(comments)
            if row.get("status") == "completed"
            and row.get("cycle_id") == meta.get("cycle_id")
            and row.get("window_end") == meta.get("window_end")
        ]
        replay = canonical_replay(issue, comments, datetime.now(timezone.utc))
        if states and replay.latest_result is not None:
            out.append({"issue": issue, "meta": meta, "state": states[-1]})
    out.sort(key=lambda row: _parse_iso(row["meta"]["window_end"]))
    return out


def _run_body(meta: dict[str, Any]) -> str:
    task = {
        "process": PROCESS,
        "repository": "GK-studio-JP/ai-os-projects",
        "objective": "Execute one dedicated Gemini Nightly Dream production cycle",
        "priority": 100,
        "contracts": ["aios-dream-cycle:v1"],
        "context_refs": [
            "projects/aios-nightly-dream/AUTOMATION_RUNBOOK.md",
            "projects/aios-nightly-dream/DREAM_CONTRACT.md",
            "GK-studio-JP/ai-bulletin-board#52",
        ],
        "acceptance": [
            "Scheduled GPT only triggers this dedicated runner",
            "Gemini performs salience and deep Dream reasoning",
            "Deterministic AIOS validation gates all canonical mutations",
            "Cycle state and RESULT are persisted only after safe completion",
        ],
        "blocked_by": [],
        "capabilities": [],
    }
    return (
        "<!-- ai-os-task:v1 -->\n```json\n"
        + json.dumps(task, ensure_ascii=False, indent=2)
        + "\n```\n\n```json\n"
        + json.dumps(meta, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def _memory_context(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from ai_os_context.memory import MemoryUnavailable, search_global_memory
    except ImportError as exc:
        raise LauncherError("current ai-os-context Memory API is required") from exc
    out: dict[str, Any] = {}
    for row in tasks:
        capsule = row.get("triage_capsule") or {}
        query = " ".join(
            str(capsule.get(key) or "")
            for key in ("objective", "result_summary")
        ).strip()
        try:
            result = search_global_memory(query, limit=5)
            out[row["task"]] = result.get("results", [])
        except (MemoryUnavailable, ValueError):
            out[row["task"]] = []
    return out


def _deep_prompt(
    bundle: dict[str, Any],
    deep_tasks: list[dict[str, Any]],
    triage: dict[str, Any],
    memory: dict[str, Any],
) -> str:
    payload = {
        "bundle_fingerprint": bundle["fingerprint"],
        "tasks": deep_tasks,
        "triage": triage,
        "existing_memory": memory,
    }
    return (
        "You are the dedicated Gemini deep-analysis worker for AIOS Nightly Dream. "
        "Use only INPUT and do not browse or call external tools. Reconstruct final state, "
        "discard superseded conclusions, compare reusable operational lessons, reconcile "
        "against existing_memory, and be conservative. Return exactly one JSON object. "
        "It must have kind=finish, schema=aios-dream-report:v1, authoritative=false, "
        "bundle_fingerprint equal INPUT.bundle_fingerprint, and proposals as a list. "
        "Each proposal must have schema=aios-dream-proposal:v1, a stable proposal_id, "
        "kind fact|lesson|pattern, decision promote|noop|defer|reject|supersede, "
        "scope global|project|event_only, source_tasks from INPUT only, a concise summary, "
        "and evidence entries {task,ref} where ref is present in that task's source_refs. "
        "Promote/supersede requires evidence; promoted pattern requires at least 3 tasks. "
        "Never infer psychology or persist secrets. "
        f"INPUT={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _empty_report(bundle: dict[str, Any], normalize_dream_report) -> dict[str, Any]:
    return normalize_dream_report(
        bundle,
        {
            "schema": "aios-dream-report:v1",
            "authoritative": False,
            "bundle_fingerprint": bundle["fingerprint"],
            "proposals": [],
        },
    )


def _publish_request_prompt(
    bundle: dict[str, Any],
    report: dict[str, Any],
    proposal: dict[str, Any],
    documents: dict[str, Any],
) -> str:
    payload = {
        "bundle_fingerprint": bundle["fingerprint"],
        "proposal": proposal,
        "documents": documents.get("documents", []),
    }
    return (
        "You are the Gemini Memory drafting stage for AIOS Nightly Dream. "
        "Return exactly one JSON object with kind=finish plus an "
        "aios-memory-publish-request:v1. It must be authoritative=false and use the "
        "provided bundle_fingerprint and proposal_id. Choose create or update, a document_id, "
        "title, an allowed path under knowledge/lessons/ or knowledge/troubleshooting/, "
        "type lesson|troubleshooting, Markdown content with an H1, priority 0..100, and "
        "tools/repositories/environments/keywords string arrays. Include supersedes_document_id "
        "only for a supersede proposal. Do not include secrets. "
        f"INPUT={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _memory_documents_projection() -> dict[str, Any]:
    base = str(os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = str(os.environ.get("SUPABASE_SECRET_KEY") or "")
    if not base or not key:
        raise LauncherError("Memory projection is not configured")
    url = (
        base
        + "/rest/v1/aios_memory_chunks?"
        + urllib.parse.urlencode({
            "select": "document_id,title,source_path,type,status",
            "scope": "eq.global",
            "limit": "10000",
        })
    )
    request = urllib.request.Request(
        url,
        headers={"apikey": key, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if not isinstance(rows, list):
        raise LauncherError("Memory projection returned an invalid document list")
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        doc_id = str(row.get("document_id") or "").strip()
        path = str(row.get("source_path") or "").strip()
        if not doc_id or not path:
            continue
        documents.setdefault(
            doc_id,
            {
                "id": doc_id,
                "title": str(row.get("title") or doc_id),
                "path": path,
                "scope": "global",
                "type": str(row.get("type") or ""),
                "status": str(row.get("status") or "active"),
            },
        )
    return {
        "schema": "ai-os-memory-documents:v1",
        "documents": sorted(documents.values(), key=lambda row: row["id"]),
    }


_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]{12,}\\b"),
    re.compile(r"\\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sb_secret_[A-Za-z0-9_]{20,})\\b"),
    re.compile(r"(?i)\\b(?:password|passwd|cookie|api[_-]?key|service[_-]?role[_-]?key)\\s*[:=]\\s*[^\\s\\`]{8,}"),
)


def _validate_publish_plan(
    *,
    bundle: dict[str, Any],
    report: dict[str, Any],
    request: dict[str, Any],
    documents: dict[str, Any],
) -> dict[str, Any]:
    request = {key: value for key, value in request.items() if key != "kind"}
    if request.get("schema") != "aios-memory-publish-request:v1":
        raise ValueError("unsupported Memory publish request")
    if request.get("authoritative") is not False:
        raise ValueError("Memory publish request must be non-authoritative")
    if request.get("bundle_fingerprint") != bundle.get("fingerprint"):
        raise ValueError("Memory publish request bundle mismatch")

    proposal_id = str(request.get("proposal_id") or "").strip()
    matches = [
        row for row in report.get("proposals", [])
        if isinstance(row, dict) and row.get("proposal_id") == proposal_id
    ]
    if len(matches) != 1:
        raise ValueError("publish request must reference exactly one proposal")
    proposal = matches[0]
    if proposal.get("decision") not in {"promote", "supersede"}:
        raise ValueError("proposal is not publishable")
    if proposal.get("scope") != "global":
        raise ValueError("global Memory gate only accepts global proposals")

    source_rows = {
        row["task"]: row
        for row in bundle.get("tasks", [])
        if isinstance(row, dict) and isinstance(row.get("task"), str)
    }
    source_tasks = proposal.get("source_tasks")
    if not isinstance(source_tasks, list) or not source_tasks:
        raise ValueError("source_tasks are required")
    for task in source_tasks:
        row = source_rows.get(task)
        if not row or row.get("history_safe") is not True or row.get("final_state") != "completed":
            raise ValueError("publishable proposal requires completed safe source tasks")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("publishable proposal requires evidence")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("invalid evidence")
        task = item.get("task")
        ref = item.get("ref")
        if task not in source_tasks or ref not in source_rows[task].get("source_refs", []):
            raise ValueError("evidence is not bound to the source bundle")

    doc_type = str(request.get("type") or "").strip()
    if doc_type not in {"lesson", "troubleshooting"}:
        raise ValueError("unsupported Memory document type")
    doc_id = str(request.get("document_id") or "").strip()
    expected_prefix = "lesson." if doc_type == "lesson" else "troubleshooting."
    if not doc_id.startswith(expected_prefix):
        raise ValueError("document_id prefix does not match type")
    path = str(request.get("path") or "").strip()
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
        or not path.endswith(".md")
        or not path.startswith(("knowledge/lessons/", "knowledge/troubleshooting/"))
    ):
        raise ValueError("Memory path is outside the Dream allowlist")
    content = str(request.get("content") or "")
    if len(content) > 30000 or not re.search(r"(?m)^#\\s+\\S", content):
        raise ValueError("Memory content is invalid")
    if any(pattern.search(content) for pattern in _SENSITIVE_PATTERNS):
        raise ValueError("Memory content contains a prohibited value pattern")

    docs = {
        row.get("id"): row
        for row in documents.get("documents", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    paths = {
        row.get("path"): row.get("id")
        for row in documents.get("documents", [])
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    operation = str(request.get("operation") or "")
    if operation not in {"create", "update"}:
        raise ValueError("operation must be create or update")
    existing = docs.get(doc_id)
    if operation == "create":
        if existing is not None or path in paths:
            raise ValueError("create target already exists")
    else:
        if existing is None or existing.get("path") != path:
            raise ValueError("update target does not match canonical document")

    supersede = None
    supersede_id = request.get("supersedes_document_id")
    if proposal.get("decision") == "supersede":
        supersede_id = str(supersede_id or "").strip()
        target = docs.get(supersede_id)
        if not supersede_id or not target or target.get("status", "active") != "active":
            raise ValueError("supersede target must be an active canonical document")
        if supersede_id == doc_id:
            raise ValueError("document cannot supersede itself")
        supersede = {"document_id": supersede_id, "status": "superseded"}
    elif supersede_id is not None:
        raise ValueError("supersedes_document_id requires supersede decision")

    priority = request.get("priority", 70)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        raise ValueError("priority must be 0..100")
    metadata = {
        "id": doc_id,
        "title": str(request.get("title") or "").strip(),
        "path": path,
        "scope": "global",
        "type": doc_type,
        "tools": request.get("tools") if isinstance(request.get("tools"), list) else [],
        "repositories": request.get("repositories") if isinstance(request.get("repositories"), list) else [],
        "environments": request.get("environments") if isinstance(request.get("environments"), list) else [],
        "keywords": request.get("keywords") if isinstance(request.get("keywords"), list) else [],
        "priority": priority,
        "status": "active",
    }
    if not metadata["title"]:
        raise ValueError("Memory title is required")
    material = {
        "bundle_fingerprint": bundle["fingerprint"],
        "proposal_id": proposal_id,
        "operation": operation,
        "metadata": metadata,
        "content": content,
        "supersede": supersede,
        "evidence": evidence,
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan_id = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema": "aios-memory-publish-plan:v1",
        "authoritative": False,
        "plan_id": plan_id,
        "bundle_fingerprint": bundle["fingerprint"],
        "proposal_id": proposal_id,
        "decision": proposal["decision"],
        "write_mode": "branch_pr_only",
        "target": metadata,
        "file": {"path": path, "content": content},
        "index_mutations": {
            "upsert": metadata,
            "supersede": [supersede] if supersede else [],
        },
        "direct_canonical_write_allowed": False,
    }

def _write_status(path: str | None, value: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_cycle(args: argparse.Namespace) -> int:
    try:
        from ai_os_context.dream import build_dream_bundle, normalize_dream_report
    except ImportError as exc:
        raise LauncherError("current ai-os-context Dream API is required") from exc
    now = _now_jst()
    agent_id = f"gemini:nightly-dream:{now.strftime('%Y%m%dT%H%M%S%z')}:{uuid.uuid4().hex[:8]}"
    relay = Relay(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"], args.session_id)
    relay.ready(timeout=60)
    relay.command("start", {})

    control_claimed = False
    run_number: int | None = None
    try:
        control_issue = _issue(CONTROL_ISSUE)
        control_comments = _comments(CONTROL_ISSUE)
        control_state = canonical_replay(control_issue, control_comments, datetime.now(timezone.utc))
        if control_state.state == "claimed":
            _write_status(args.status_file, {
                "schema": "aios-nightly-dream-run-status:v1",
                "status": "busy",
                "observed_at": _iso(now),
                "owner": control_state.owner,
            })
            return 0

        _append_event(
            relay,
            CONTROL_ISSUE,
            "CLAIM",
            agent_id=agent_id,
            summary="Dedicated Gemini Nightly Dream runner is acquiring the production control lease.",
            next_action="Resolve or create the fixed Dream Run, reconstruct its source window, and execute Gemini Dream analysis.",
            artifacts=[],
            idempotency_key=f"{agent_id}:control:claim",
        )
        _wait_owner(CONTROL_ISSUE, agent_id)
        control_claimed = True

        all_issues = _all_issues()
        runs = [
            issue for issue in all_issues
            if str(issue.get("title") or "").startswith(RUN_TITLE_PREFIX)
        ]
        incomplete = sorted(
            [issue for issue in runs if issue.get("state") == "open" and _run_meta(issue)],
            key=lambda row: int(row["number"]),
        )
        successful = _successful_runs(runs)

        if incomplete:
            run_issue = incomplete[0]
            meta = _run_meta(run_issue)
            assert meta is not None
            run_number = int(run_issue["number"])
        else:
            previous = successful[-1] if successful else None
            window_start = (
                str(previous["meta"]["window_end"])
                if previous
                else BOOTSTRAP_WATERMARK
            )
            window_end = _iso(now - timedelta(seconds=SETTLE_DELAY_SECONDS))
            if _parse_iso(window_end) <= _parse_iso(window_start):
                raise LauncherError("Dream window is not yet positive")
            cid = cycle_id(window_start, window_end)
            meta = {
                "schema": "aios-dream-run:v1",
                "cycle_id": cid,
                "contract_version": 2,
                "window_start": window_start,
                "window_end": window_end,
                "settle_delay_seconds": SETTLE_DELAY_SECONDS,
                "previous_success_issue": int(previous["issue"]["number"]) if previous else 0,
                "previous_success_window_end": str(previous["meta"]["window_end"]) if previous else None,
                "executor": "gemini_dedicated_runner",
            }
            run_number = _create_issue(relay, RUN_TITLE_PREFIX + cid, _run_body(meta))

        _append_event(
            relay,
            run_number,
            "CLAIM",
            agent_id=agent_id,
            summary="Dedicated Gemini Nightly Dream runner is claiming this fixed cycle.",
            next_action="Reconstruct canonical source histories and run deterministic triage.",
            artifacts=[f"issue:#{CONTROL_ISSUE}"],
            idempotency_key=f"{agent_id}:run:{run_number}:claim",
        )
        _wait_owner(run_number, agent_id)

        window_start = str(meta["window_start"])
        window_end = str(meta["window_end"])
        source_issues = _all_issues(since=window_start)
        histories: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for issue in source_issues:
            comments = _comments(int(issue["number"]))
            _validate_history(issue, comments)
            histories.append((issue, comments))

        bundle = build_dream_bundle(
            BOARD,
            histories,
            window_start=_parse_iso(window_start),
            window_end=_parse_iso(window_end),
            generated_at=now,
            settle_delay_seconds=SETTLE_DELAY_SECONDS,
        )

        _append_event(
            relay,
            CONTROL_ISSUE,
            "HEARTBEAT",
            agent_id=agent_id,
            summary="Dedicated Dream source reconstruction completed; Gemini triage is starting.",
            next_action="Run bounded Gemini triage and deep analysis, then persist the cycle state.",
            artifacts=[f"bundle:{bundle['fingerprint']}", f"issue:#{run_number}"],
            idempotency_key=f"{agent_id}:control:heartbeat:source",
        )

        triage: dict[str, Any] = {}
        deep_tasks: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for row in bundle.get("tasks", []):
            capsule = row["triage_capsule"]
            try:
                result = run_triage(relay, capsule)
            except Exception as exc:
                result = {
                    "schema": "aios-dream-triage-result:v1",
                    "source_fingerprint": capsule["source_fingerprint"],
                    "triage_version": 1,
                    "salience": 0.0,
                    "dimensions": {
                        "operational_impact": 0.0,
                        "reuse_scope": 0.0,
                        "novelty": 0.0,
                        "recurrence": 0.0,
                        "evidence_strength": 0.0,
                    },
                    "decision": "defer",
                    "reasons": [f"gemini_unavailable: {type(exc).__name__}"],
                }
            triage[row["task"]] = result
            if result["decision"] == "deep":
                deep_tasks.append(row)
            elif result["decision"] == "defer":
                deferred.append({
                    "candidate_id": f"task:{row['task']}",
                    "source_tasks": [row["task"]],
                    "source_fingerprints": [row["source_fingerprint"]],
                    "reason": "gemini_unavailable"
                    if any(str(reason).startswith("gemini_unavailable") for reason in result["reasons"])
                    else "triage_defer",
                    "detail": "; ".join(result["reasons"]),
                    "last_evaluated_cycle": meta["cycle_id"],
                    "evidence_refs": row.get("source_refs", [])[:8],
                })

        if deep_tasks:
            memory = _memory_context(deep_tasks)
            raw_report = ask_gemini(relay, 0, _deep_prompt(bundle, deep_tasks, triage, memory))
            report = normalize_dream_report(
                bundle,
                {key: value for key, value in raw_report.items() if key != "kind"},
            )
        else:
            report = _empty_report(bundle, normalize_dream_report)

        documents = _memory_documents_projection()
        publish_plans: list[dict[str, Any]] = []
        for proposal in report.get("proposals", []):
            if proposal["decision"] not in {"promote", "supersede"}:
                continue
            if proposal["scope"] == "global":
                try:
                    raw_request = ask_gemini(
                        relay,
                        0,
                        _publish_request_prompt(bundle, report, proposal, documents),
                    )
                    plan = _validate_publish_plan(
                        bundle=bundle,
                        report=report,
                        request=raw_request,
                        documents=documents,
                    )
                    publish_plans.append(plan)
                    deferred.append({
                        "candidate_id": proposal["proposal_id"],
                        "source_tasks": proposal["source_tasks"],
                        "source_fingerprints": [
                            next(row["source_fingerprint"] for row in bundle["tasks"] if row["task"] == task)
                            for task in proposal["source_tasks"]
                        ],
                        "reason": "publish_pending",
                        "detail": f"Validated publish plan {plan['plan_id']} is awaiting the dedicated PR mutation stage.",
                        "last_evaluated_cycle": meta["cycle_id"],
                        "evidence_refs": [
                            item["ref"] for item in proposal.get("evidence", [])
                        ],
                    })
                except Exception as exc:
                    deferred.append({
                        "candidate_id": proposal["proposal_id"],
                        "source_tasks": proposal["source_tasks"],
                        "source_fingerprints": [],
                        "reason": "publish_gate_rejected",
                        "detail": type(exc).__name__,
                        "last_evaluated_cycle": meta["cycle_id"],
                        "evidence_refs": [item["ref"] for item in proposal.get("evidence", [])],
                    })
            elif proposal["scope"] == "project":
                deferred.append({
                    "candidate_id": proposal["proposal_id"],
                    "source_tasks": proposal["source_tasks"],
                    "source_fingerprints": [],
                    "reason": "project_publish_pending",
                    "detail": "Project publish is deferred to the dedicated project PR stage.",
                    "last_evaluated_cycle": meta["cycle_id"],
                    "evidence_refs": [item["ref"] for item in proposal.get("evidence", [])],
                })

        decision_counts = {key: 0 for key in ("promote", "noop", "defer", "reject", "supersede")}
        for proposal in report.get("proposals", []):
            decision_counts[proposal["decision"]] += 1
        triage_counts = {key: 0 for key in ("skip", "defer", "deep")}
        for result in triage.values():
            triage_counts[result["decision"]] += 1

        state = {
            "schema": "aios-dream-cycle:v1",
            "cycle_id": meta["cycle_id"],
            "status": "completed",
            "window_start": window_start,
            "window_end": window_end,
            "bundle_fingerprint": bundle["fingerprint"],
            "executor": "gemini_dedicated_runner",
            "counts": {
                "selected": len(bundle.get("tasks", [])),
                "triage_skip": triage_counts["skip"],
                "triage_defer": triage_counts["defer"],
                "triage_deep": triage_counts["deep"],
                **decision_counts,
            },
            "deferred": deferred,
            "memory_prs": [],
            "project_prs": [],
            "validated_publish_plans": [
                {"plan_id": plan["plan_id"], "target": plan["target"]["path"]}
                for plan in publish_plans
            ],
        }
        _append_json_comment(relay, run_number, state)
        _append_event(
            relay,
            run_number,
            "RESULT",
            agent_id=agent_id,
            summary=(
                f"Dedicated Gemini Nightly Dream cycle completed for {window_start}..{window_end}. "
                f"selected={state['counts']['selected']} deep={state['counts']['triage_deep']} "
                f"proposals={sum(decision_counts.values())} deferred={len(deferred)}. "
                "Scheduled GPT did not perform canonical GitHub writes."
            ),
            next_action=None,
            artifacts=[f"bundle:{bundle['fingerprint']}"]
            + [f"publish-plan:{plan['plan_id']}" for plan in publish_plans],
            idempotency_key=f"aios-nightly-dream:{meta['cycle_id']}:result:v2",
        )
        _close_issue(relay, run_number)
        _append_event(
            relay,
            CONTROL_ISSUE,
            "RELEASE",
            agent_id=agent_id,
            summary=f"Dedicated Gemini Nightly Dream cycle #{run_number} completed and released the control lease.",
            next_action="A later scheduled trigger may start the next settled Dream window.",
            artifacts=[f"issue:#{run_number}", f"bundle:{bundle['fingerprint']}"],
            idempotency_key=f"{agent_id}:control:release",
        )
        control_claimed = False
        _write_status(args.status_file, {
            "schema": "aios-nightly-dream-run-status:v1",
            "status": "completed",
            "completed_at": _iso(_now_jst()),
            "run_issue": run_number,
            "cycle_id": meta["cycle_id"],
            "bundle_fingerprint": bundle["fingerprint"],
            "counts": state["counts"],
        })
        return 0
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if run_number is not None:
            try:
                _append_event(
                    relay,
                    run_number,
                    "HANDOFF",
                    agent_id=agent_id,
                    summary=f"Dedicated Gemini Nightly Dream stopped safely: {detail[:500]}",
                    next_action="Resume the same cycle after correcting the failing boundary; do not advance the watermark.",
                    artifacts=[],
                    idempotency_key=f"{agent_id}:run:{run_number}:handoff",
                )
            except Exception:
                pass
        if control_claimed:
            try:
                _append_event(
                    relay,
                    CONTROL_ISSUE,
                    "RELEASE",
                    agent_id=agent_id,
                    summary="Dedicated Gemini Nightly Dream released control after a safely recorded failure.",
                    next_action="Resume the same incomplete cycle on the next trigger.",
                    artifacts=[f"issue:#{run_number}"] if run_number else [],
                    idempotency_key=f"{agent_id}:control:release:failure",
                )
                control_claimed = False
            except Exception:
                pass
        _write_status(args.status_file, {
            "schema": "aios-nightly-dream-run-status:v1",
            "status": "failed",
            "failed_at": _iso(_now_jst()),
            "run_issue": run_number,
            "error": detail[:1000],
        })
        raise


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aios-nightly-dream-runner")
    root.add_argument("--session-id", required=True)
    root.add_argument("--context-root", required=True)
    root.add_argument("--memory-root", required=True)
    root.add_argument("--status-file")
    root.add_argument("--trigger-file")
    return root


def main() -> int:
    args = parser().parse_args()
    return run_cycle(args)


if __name__ == "__main__":
    raise SystemExit(main())
