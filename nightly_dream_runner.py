from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_os_browser_worker.dream_triage import deterministic_triage
from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.nightly_dream import (
    cycle_id,
    cycle_state_body,
    deep_prompt,
    iso,
    latest_success,
    oldest_open_run,
    parse_iso,
    resolve_window,
    run_issue_body,
    run_metadata,
)
from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import ask_gemini
from dream_triage_runner import _CurrentPageRelay, run_triage
from ai_os_context.dream import build_dream_bundle, normalize_dream_report
from ai_os_context.memory import MemoryUnavailable, search_global_memory
from ai_os_context.replay import replay

BOARD = "GK-studio-JP/ai-bulletin-board"
CONTROL_ISSUE = 52
CONTROL_URL = f"https://github.com/{BOARD}/issues/{CONTROL_ISSUE}"
GITHUB_API = "https://api.github.com"
DREAM_TITLE_PREFIX = "[AIOS][aios-nightly-dream-run] "


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_histories(path: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "aios-dream-histories:v1":
        raise LauncherError("unsupported Dream histories snapshot")
    if value.get("repository") != BOARD:
        raise LauncherError("Dream histories repository mismatch")
    rows = value.get("histories")
    if not isinstance(rows, list):
        raise LauncherError("Dream histories must be a list")
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise LauncherError("Dream history row must be an object")
        issue = row.get("issue")
        comments = row.get("comments")
        if not isinstance(issue, dict) or not isinstance(comments, list):
            raise LauncherError("Dream history row requires issue and comments")
        number = issue.get("number")
        if not isinstance(number, int) or number in seen:
            raise LauncherError("Dream history issue number is missing or duplicated")
        seen.add(number)
        out.append((issue, comments))
    return out


def _history(
    histories: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    issue_number: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for issue, comments in histories:
        if issue.get("number") == issue_number:
            return issue, comments
    raise LauncherError(f"snapshot is missing required issue #{issue_number}")


def _github_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "aios-nightly-dream-gemini-runner",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise LauncherError(f"GitHub verification read failed: {exc}") from exc


def _live_issue(issue_number: int) -> dict[str, Any]:
    value = _github_json(f"{GITHUB_API}/repos/{BOARD}/issues/{issue_number}")
    if not isinstance(value, dict):
        raise LauncherError("GitHub issue verification returned invalid JSON")
    return value


def _live_comments(issue_number: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        value = _github_json(
            f"{GITHUB_API}/repos/{BOARD}/issues/{issue_number}/comments"
            f"?per_page=100&page={page}"
        )
        if not isinstance(value, list):
            raise LauncherError("GitHub comments verification returned invalid JSON")
        out.extend(value)
        if len(value) < 100:
            return out
    raise LauncherError("GitHub comments exceeded 100 pages")


def _list_issues(*, since: datetime | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 101):
        query: dict[str, str | int] = {
            "state": "all",
            "per_page": 100,
            "page": page,
        }
        if since is not None:
            query["since"] = iso(since)
        value = _github_json(
            f"{GITHUB_API}/repos/{BOARD}/issues?"
            + urllib.parse.urlencode(query)
        )
        if not isinstance(value, list):
            raise LauncherError("GitHub issue discovery returned invalid JSON")
        out.extend(
            row
            for row in value
            if isinstance(row, dict) and "pull_request" not in row
        )
        if len(value) < 100:
            return out
    raise LauncherError("GitHub issue discovery exceeded 100 pages")


def _validated_history(
    issue: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    number = issue.get("number")
    if not isinstance(number, int):
        raise LauncherError("GitHub issue is missing a numeric number")
    for field in ("created_at", "updated_at"):
        if not isinstance(issue.get(field), str):
            raise LauncherError(
                f"source-inconsistent: issue #{number} is missing {field}"
            )
    comments = _live_comments(number)
    declared = issue.get("comments")
    if isinstance(declared, int) and declared != len(comments):
        raise LauncherError(
            f"source-inconsistent: issue #{number} declares {declared} comments "
            f"but {len(comments)} were fetched"
        )
    for comment in comments:
        for field in ("id", "created_at", "updated_at", "author_association", "body"):
            if comment.get(field) is None:
                raise LauncherError(
                    f"source-inconsistent: issue #{number} comment is missing {field}"
                )
        user = comment.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise LauncherError(
                f"source-inconsistent: issue #{number} comment is missing actor login"
            )
    return issue, comments


def _auto_histories(
    automation_start: datetime,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    issues = _list_issues()
    by_number = {
        int(issue["number"]): issue
        for issue in issues
        if isinstance(issue.get("number"), int)
    }
    control = by_number.get(CONTROL_ISSUE)
    if control is None:
        raise LauncherError("source-inconsistent: Control Issue #52 is missing")

    histories: dict[
        int,
        tuple[dict[str, Any], list[dict[str, Any]]],
    ] = {}

    def include(issue: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        number = int(issue["number"])
        if number not in histories:
            histories[number] = _validated_history(issue)
        return histories[number]

    include(control)

    dream_issues = [
        issue for issue in issues
        if run_metadata(issue) is not None
    ]
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
            end = parse_iso(str(meta["window_end"]))
        except Exception:
            continue
        closed_candidates.append((end, issue))
    closed_candidates.sort(key=lambda item: item[0], reverse=True)
    for _end, issue in closed_candidates:
        history = include(issue)
        if latest_success([history]) is not None:
            break

    seed = list(histories.values())
    resumed = oldest_open_run(seed)
    if resumed is not None:
        _issue, meta = resumed
        window_start = parse_iso(str(meta["window_start"]))
    else:
        window_start, _window_end, _previous = resolve_window(
            seed,
            automation_start,
        )

    for issue in _list_issues(since=window_start):
        include(issue)

    return [
        histories[number]
        for number in sorted(histories)
    ]


def _wait_replay(
    issue_number: int,
    predicate: Callable[[Any], bool],
    *,
    timeout: int = 20,
) -> Any:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        issue = _live_issue(issue_number)
        comments = _live_comments(issue_number)
        last = replay(issue, comments, _now())
        if predicate(last):
            return last
        time.sleep(0.75)
    raise LauncherError(
        f"canonical replay verification timed out for #{issue_number}; "
        f"last_state={getattr(last, 'state', None)!r}"
    )


def _find(page: dict[str, Any], *, role: str | None = None, text: str | None = None, label: str | None = None) -> dict[str, Any] | None:
    for element in page.get("elements") or []:
        if role is not None and element.get("role") != role:
            continue
        if text is not None and str(element.get("text") or "").strip() != text:
            continue
        if label is not None and str(element.get("label") or "").strip() != label:
            continue
        return element
    return None


def _append_comment(relay: Relay, issue_url: str, body: str) -> None:
    relay.command("goto", {"url": issue_url})
    page = relay.command("getPage", {})
    box = _find(page, role="textbox", label="Add a comment")
    if not box:
        raise LauncherError("canonical Issue comment box unavailable")
    relay.command("fill", {"elementId": box["id"], "text": body})
    page = relay.command("getPage", {})
    button = _find(page, role="button", text="Comment")
    if not button:
        raise LauncherError("canonical Issue Comment button unavailable")
    relay.command("click", {"elementId": button["id"]})
    relay.command("getPage", {})


def _protocol_body(
    event_type: str,
    *,
    agent_id: str,
    task: str,
    summary: str,
    next_action: str | None,
    artifacts: list[str] | None = None,
    key: str | None = None,
) -> str:
    payload = {
        "type": event_type,
        "agent_id": agent_id,
        "task": task,
        "idempotency_key": key or f"{agent_id}:{task}:{event_type.lower()}:{uuid.uuid4().hex}",
        "summary": summary,
        "next_action": next_action,
        "artifacts": artifacts or [],
    }
    return (
        "<!-- ai-bb:v1 -->\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```"
    )


def _create_run_issue(
    relay: Relay,
    *,
    title: str,
    body: str,
) -> tuple[int, str]:
    url = (
        f"https://github.com/{BOARD}/issues/new?"
        + urllib.parse.urlencode({"title": title, "body": body})
    )
    relay.command("goto", {"url": url})
    page = relay.command("getPage", {})
    button = (
        _find(page, role="button", text="Submit new issue")
        or _find(page, role="button", text="Create issue")
        or _find(page, role="button", text="Submit")
    )
    if not button:
        raise LauncherError("new Dream Run submit button unavailable")
    relay.command("click", {"elementId": button["id"]})
    page = relay.command("getPage", {})
    current = str(page.get("url") or "")
    match = re.search(r"/issues/(\d+)(?:$|[?#])", current)
    if not match:
        raise LauncherError("new Dream Run issue URL was not observed")
    number = int(match.group(1))
    return number, f"https://github.com/{BOARD}/issues/{number}"


def _close_issue(relay: Relay, issue_url: str, issue_number: int) -> None:
    relay.command("goto", {"url": issue_url})
    page = relay.command("getPage", {})
    button = (
        _find(page, role="button", text="Close issue")
        or _find(page, role="button", text="Close")
    )
    if not button:
        raise LauncherError("Dream Run close button unavailable")
    relay.command("click", {"elementId": button["id"]})
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if str(_live_issue(issue_number).get("state")) == "closed":
            return
        time.sleep(0.75)
    raise LauncherError("Dream Run did not become closed after close action")


def _gemini_deep(relay: Relay, prompt_text: str) -> dict[str, Any]:
    started = relay.command("start", {})
    current_url = str(started.get("url") or "") if isinstance(started, dict) else ""
    return ask_gemini(_CurrentPageRelay(relay, current_url=current_url), 0, prompt_text)


def _ensure_lease(
    relay: Relay,
    *,
    issue_number: int,
    issue_url: str,
    agent_id: str,
    phase: str,
) -> None:
    state = _wait_replay(
        issue_number,
        lambda value: value.state in {"claimed", "open", "completed", "history_unsafe"},
        timeout=10,
    )
    if not state.history_safe:
        raise LauncherError(f"canonical history became unsafe before {phase}")
    if state.state != "claimed" or state.owner != agent_id:
        raise LauncherError(f"canonical lease ownership was lost before {phase}")
    if state.lease_status != "expiring":
        return
    _append_comment(
        relay,
        issue_url,
        _protocol_body(
            "HEARTBEAT",
            agent_id=agent_id,
            task=f"#{issue_number}",
            summary=f"Renewing Nightly Dream lease before {phase}.",
            next_action=f"Continue the same Nightly Dream cycle through {phase}.",
        ),
    )
    _wait_replay(
        issue_number,
        lambda value: (
            value.state == "claimed"
            and value.owner == agent_id
            and value.lease_status != "expired"
        ),
    )


def _memory_context(bundle: dict[str, Any], triage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries: list[str] = []
    for row in bundle.get("tasks", []):
        if not isinstance(row, dict):
            continue
        if not any(
            item.get("task") == row.get("task") and item.get("decision") == "deep"
            for item in triage
        ):
            continue
        cap = row.get("triage_capsule") or {}
        query = " ".join(
            str(cap.get(key) or "").strip()
            for key in ("objective", "result_summary")
        ).strip()
        if query:
            queries.append(query[:1200])
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries[:8]:
        response = search_global_memory(query, limit=4)
        for item in response.get("results", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("chunk_id") or item.get("document_id") or json.dumps(item, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= 24:
                return results
    return results


def _deferred_from_triage(bundle: dict[str, Any], triage: list[dict[str, Any]], cycle: str) -> list[dict[str, Any]]:
    by_task = {
        row.get("task"): row
        for row in bundle.get("tasks", [])
        if isinstance(row, dict)
    }
    out: list[dict[str, Any]] = []
    for item in triage:
        if item.get("decision") != "defer":
            continue
        task = str(item.get("task") or "")
        source = by_task.get(task) or {}
        out.append({
            "candidate_id": f"triage:{task}:{source.get('source_fingerprint')}",
            "source_tasks": [task],
            "source_fingerprints": [source.get("source_fingerprint")],
            "reason": (item.get("reasons") or ["triage_defer"])[0],
            "last_evaluated_cycle": cycle,
            "evidence_refs": source.get("source_refs") or [],
        })
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    automation_start = parse_iso(args.automation_start) if args.automation_start else _now()
    histories = (
        _load_histories(args.histories_file)
        if args.histories_file
        else _auto_histories(automation_start)
    )
    control_issue, control_comments = _history(histories, CONTROL_ISSUE)
    control_state = replay(control_issue, control_comments, automation_start)
    if not control_state.history_safe:
        raise LauncherError("Dream Control #52 is history_unsafe")
    if control_state.state == "claimed" and control_state.owner:
        raise LauncherError(f"Dream Control #52 is owned by {control_state.owner}")

    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")

    relay = Relay(base, key, args.session_id)
    relay.ready()
    relay.command("start", {})

    agent = f"nightly-dream-gemini-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    control_claimed = False
    run_claimed = False
    run_no: int | None = None
    run_url: str | None = None

    try:
        _append_comment(
            relay,
            CONTROL_URL,
            _protocol_body(
                "CLAIM",
                agent_id=agent,
                task="#52",
                summary="Dedicated Gemini Nightly Dream runner is claiming the production control lease.",
                next_action="Reconstruct the Dream window and execute the cycle.",
            ),
        )
        _wait_replay(CONTROL_ISSUE, lambda state: state.state == "claimed" and state.owner == agent)
        control_claimed = True

        resumed = oldest_open_run(histories)
        if resumed is not None:
            issue, meta = resumed
            run_no = int(issue["number"])
            run_url = str(issue.get("html_url") or f"https://github.com/{BOARD}/issues/{run_no}")
            cycle = str(meta["cycle_id"])
            window_start = parse_iso(str(meta["window_start"]))
            window_end = parse_iso(str(meta["window_end"]))
            previous_success_issue = int(meta.get("previous_success_issue") or 0) or None
        else:
            window_start, window_end, previous_success_issue = resolve_window(
                histories,
                automation_start,
            )
            cycle = cycle_id(window_start, window_end)
            title = DREAM_TITLE_PREFIX + cycle
            body = run_issue_body(
                cycle=cycle,
                window_start=window_start,
                window_end=window_end,
                previous_success_issue=previous_success_issue,
            )
            run_no, run_url = _create_run_issue(relay, title=title, body=body)

        assert run_no is not None and run_url is not None
        _append_comment(
            relay,
            run_url,
            _protocol_body(
                "CLAIM",
                agent_id=agent,
                task=f"#{run_no}",
                summary="Dedicated Gemini Nightly Dream runner is claiming this Dream Run.",
                next_action="Build the canonical source bundle and run Gemini Dream synthesis.",
            ),
        )
        _wait_replay(run_no, lambda state: state.state == "claimed" and state.owner == agent)
        run_claimed = True

        bundle = build_dream_bundle(
            BOARD,
            histories,
            window_start=window_start,
            window_end=window_end,
            generated_at=automation_start,
            settle_delay_seconds=3600,
            max_timeline_events=48,
        )

        triage: list[dict[str, Any]] = []
        for row in bundle.get("tasks", []):
            _ensure_lease(
                relay,
                issue_number=CONTROL_ISSUE,
                issue_url=CONTROL_URL,
                agent_id=agent,
                phase=f"triage {row.get('task')}",
            )
            _ensure_lease(
                relay,
                issue_number=run_no,
                issue_url=run_url,
                agent_id=agent,
                phase=f"triage {row.get('task')}",
            )
            capsule = row.get("triage_capsule")
            deterministic = deterministic_triage(capsule)
            if deterministic is not None:
                result = deterministic
            else:
                result = run_triage(relay, capsule)
            triage.append({"task": row["task"], **result})

        deep_count = sum(1 for row in triage if row.get("decision") == "deep")
        if deep_count:
            try:
                memory = _memory_context(bundle, triage)
            except MemoryUnavailable as exc:
                memory = []
                for item in triage:
                    if item.get("decision") == "deep":
                        item["decision"] = "defer"
                        item["reasons"] = [f"memory_unavailable: {exc}"]
                deep_count = 0
        else:
            memory = []

        if deep_count:
            _ensure_lease(
                relay,
                issue_number=CONTROL_ISSUE,
                issue_url=CONTROL_URL,
                agent_id=agent,
                phase="deep synthesis",
            )
            _ensure_lease(
                relay,
                issue_number=run_no,
                issue_url=run_url,
                agent_id=agent,
                phase="deep synthesis",
            )
            raw_report = _gemini_deep(relay, deep_prompt(bundle, triage, existing_memory=memory))
            report = normalize_dream_report(bundle, raw_report)
        else:
            report = normalize_dream_report(
                bundle,
                {
                    "schema": "aios-dream-report:v1",
                    "authoritative": False,
                    "bundle_fingerprint": bundle["fingerprint"],
                    "proposals": [],
                },
            )

        counts = {
            "selected": len(bundle.get("tasks", [])),
            "triage_skip": sum(1 for row in triage if row.get("decision") == "skip"),
            "triage_defer": sum(1 for row in triage if row.get("decision") == "defer"),
            "triage_deep": sum(1 for row in triage if row.get("decision") == "deep"),
            "promote": 0,
            "noop": 0,
            "defer": 0,
            "reject": 0,
            "supersede": 0,
        }
        deferred = _deferred_from_triage(bundle, triage, cycle)
        for proposal in report.get("proposals", []):
            decision = str(proposal.get("decision") or "")
            if decision in counts:
                counts[decision] += 1
            if decision in {"promote", "supersede"}:
                deferred.append({
                    "candidate_id": proposal.get("proposal_id"),
                    "source_tasks": proposal.get("source_tasks") or [],
                    "source_fingerprints": [
                        row.get("source_fingerprint")
                        for row in bundle.get("tasks", [])
                        if row.get("task") in (proposal.get("source_tasks") or [])
                    ],
                    "reason": "publish_pending",
                    "last_evaluated_cycle": cycle,
                    "evidence_refs": [
                        item.get("ref")
                        for item in proposal.get("evidence", [])
                        if isinstance(item, dict)
                    ],
                })

        _ensure_lease(
            relay,
            issue_number=CONTROL_ISSUE,
            issue_url=CONTROL_URL,
            agent_id=agent,
            phase="cycle persistence",
        )
        _ensure_lease(
            relay,
            issue_number=run_no,
            issue_url=run_url,
            agent_id=agent,
            phase="cycle persistence",
        )

        state_body = cycle_state_body(
            cycle=cycle,
            window_start=window_start,
            window_end=window_end,
            bundle_fingerprint=str(bundle["fingerprint"]),
            counts=counts,
            deferred=deferred,
        )
        _append_comment(relay, run_url, state_body)
        _append_comment(
            relay,
            run_url,
            _protocol_body(
                "RESULT",
                agent_id=agent,
                task=f"#{run_no}",
                summary=(
                    "Nightly Dream cycle completed through the dedicated Gemini runner; "
                    "publishable proposals, if any, remain explicitly deferred as publish_pending."
                ),
                next_action=None,
                artifacts=[f"dream-cycle:{cycle}", f"bundle:{bundle['fingerprint']}"],
                key=f"nightly-dream:{cycle}:result",
            ),
        )
        _wait_replay(run_no, lambda state: state.state == "completed")
        _close_issue(relay, run_url, run_no)
        run_claimed = False

        _append_comment(
            relay,
            CONTROL_URL,
            _protocol_body(
                "RELEASE",
                agent_id=agent,
                task="#52",
                summary=f"Nightly Dream cycle {cycle} completed and control is released.",
                next_action=None,
                artifacts=[f"issue:{run_no}", f"dream-cycle:{cycle}"],
            ),
        )
        _wait_replay(CONTROL_ISSUE, lambda state: state.state == "open")
        control_claimed = False

        return {
            "status": "completed",
            "agent_id": agent,
            "cycle_id": cycle,
            "run_issue": run_no,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "bundle_fingerprint": bundle["fingerprint"],
            "counts": counts,
            "deferred": len(deferred),
        }
    except Exception as exc:
        error = str(exc)
        if run_claimed and run_no is not None and run_url:
            try:
                _append_comment(
                    relay,
                    run_url,
                    _protocol_body(
                        "HANDOFF",
                        agent_id=agent,
                        task=f"#{run_no}",
                        summary=f"Dedicated Gemini Nightly Dream runner stopped safely: {error[:500]}",
                        next_action="Resume the same Dream Run after correcting the reported failure boundary.",
                    ),
                )
                _append_comment(
                    relay,
                    run_url,
                    _protocol_body(
                        "RELEASE",
                        agent_id=agent,
                        task=f"#{run_no}",
                        summary="Releasing Dream Run lease after a resumable failure.",
                        next_action=None,
                    ),
                )
            except Exception:
                pass
        if control_claimed:
            try:
                _append_comment(
                    relay,
                    CONTROL_URL,
                    _protocol_body(
                        "RELEASE",
                        agent_id=agent,
                        task="#52",
                        summary=f"Releasing Dream control after safe failure: {error[:500]}",
                        next_action=None,
                    ),
                )
            except Exception:
                pass
        raise


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aios-nightly-dream-gemini-runner")
    source = root.add_mutually_exclusive_group(required=True)
    source.add_argument("--histories-file")
    source.add_argument("--auto-source", action="store_true")
    root.add_argument("--session-id", default="gcp-browser-1")
    root.add_argument("--automation-start")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
