from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from execution_budget import (
    execution_budget_from_invocation,
    normalize_execution_budget,
    requires_execution_usage,
    validate_result_against_budget,
)

BOOT_SCHEMA = "ai-os-worker-boot:v1"
CAPSULE_SCHEMA = "ai-os-context-capsule:v1"
DISPATCH_SCHEMA = "ai-os-dispatch:v1"
PREFLIGHT_SCHEMA = "ai-os-runtime-preflight:v1"
INVOCATION_SCHEMA = "ai-os-worker-invocation:v1"
RESULT_SCHEMA = "ai-os-worker-result:v1"
OUTCOME_SCHEMA = "ai-os-runtime-outcome:v1"
GATE_SCHEMA = "ai-os-runtime-gate:v1"

RESULT_STATUSES = {"progress", "completed", "blocked", "page_fault", "failed"}


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str | None, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_key(*parts: Any) -> str:
    text = "|".join("" if p is None else str(p) for p in parts)
    return "runtime:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def validate_boot(boot: dict[str, Any], capsule: dict[str, Any] | None) -> dict[str, Any] | None:
    if boot.get("schema") != BOOT_SCHEMA:
        raise ValueError(f"unsupported boot schema: {boot.get('schema')!r}")
    if boot.get("authoritative") is not False:
        raise ValueError("worker boot must be explicitly non-authoritative")
    if boot.get("persist_required") is not True:
        raise ValueError("worker boot must require persistence")

    dispatch_count = boot.get("dispatch_count")
    dispatch = boot.get("dispatch")
    if dispatch_count == 0:
        if dispatch is not None:
            raise ValueError("idle boot cannot contain a dispatch")
        return None
    if dispatch_count != 1 or not isinstance(dispatch, dict):
        raise ValueError("runtime v0.1 requires exactly one dispatch or an idle boot")
    if dispatch.get("schema") != DISPATCH_SCHEMA:
        raise ValueError(f"unsupported dispatch schema: {dispatch.get('schema')!r}")
    if dispatch.get("authoritative") is not False:
        raise ValueError("dispatch must be non-authoritative")
    if capsule is None:
        raise ValueError("selected dispatch requires a Context Capsule")
    if capsule.get("schema") != CAPSULE_SCHEMA:
        raise ValueError(f"unsupported capsule schema: {capsule.get('schema')!r}")
    if capsule.get("authoritative") is not False:
        raise ValueError("capsule must be non-authoritative")
    cap_digest = capsule.get("content_digest")
    recomputed_digest = _fingerprint({k: v for k, v in capsule.items() if k != "content_digest"})
    if not cap_digest or cap_digest != recomputed_digest:
        raise ValueError("capsule content digest mismatch")

    task = dispatch.get("task")
    if not task or capsule.get("task", {}).get("id") != task:
        raise ValueError("dispatch task does not match capsule task")
    process = dispatch.get("process")
    if not process or capsule.get("identity", {}).get("process") != process:
        raise ValueError("dispatch process does not match capsule process")

    dispatch_ctx = dispatch.get("context")
    boot_ctx = boot.get("capsule")
    if not isinstance(dispatch_ctx, dict) or not isinstance(boot_ctx, dict):
        raise ValueError("boot and dispatch must carry capsule references")
    cap_fp = capsule.get("fingerprint")
    if not cap_fp or dispatch_ctx.get("fingerprint") != cap_fp or boot_ctx.get("fingerprint") != cap_fp:
        raise ValueError("capsule fingerprint mismatch")
    if (
        dispatch_ctx.get("content_digest") != cap_digest
        or boot_ctx.get("content_digest") != cap_digest
    ):
        raise ValueError("capsule content digest reference mismatch")
    return dispatch


def _claim_proposal(task: str, worker_id: str, next_action: str) -> dict[str, Any]:
    return {
        "schema": "ai-bb-event-proposal:v1",
        "authoritative": False,
        "persist_required": True,
        "event": {
            "type": "CLAIM",
            "agent_id": worker_id,
            "task": task,
            "idempotency_key": _event_key("CLAIM", task, worker_id),
            "summary": "Runtime preflight requests task ownership before Worker execution.",
            "next_action": next_action,
            "artifacts": [],
        },
    }


def preflight(
    boot: dict[str, Any],
    capsule: dict[str, Any] | None,
    fresh: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    dispatch = validate_boot(boot, capsule)
    if not worker_id.strip():
        raise ValueError("worker_id is required")

    base = {
        "schema": PREFLIGHT_SCHEMA,
        "authoritative": False,
        "persist_required": True,
        "worker_id": worker_id,
        "source_plan_fingerprint": boot.get("source_plan_fingerprint"),
        "status": "IDLE",
        "reason_code": "no_dispatch",
        "task": None,
        "canonical_through_comment_id": fresh.get("through_comment_id"),
        "canonical_source_fingerprint": fresh.get("source_fingerprint"),
        "claim_proposal": None,
    }
    if dispatch is None:
        return {**base, "fingerprint": _fingerprint(base)}

    task = str(dispatch["task"])
    result = {**base, "task": task}
    if fresh.get("task") != task:
        result.update(status="STOP", reason_code="task_mismatch")
    elif fresh.get("history_safe") is not True or fresh.get("state") == "history_unsafe":
        result.update(status="STOP", reason_code="history_unsafe")
    else:
        capsule_source = capsule.get("source", {}) if capsule else {}
        capsule_through = capsule_source.get("through_comment_id")
        capsule_source_fingerprint = capsule_source.get("source_fingerprint")
        fresh_through = fresh.get("through_comment_id")
        fresh_source_fingerprint = fresh.get("source_fingerprint")
        if capsule_through != fresh_through:
            result.update(status="STALE_CONTEXT", reason_code="canonical_history_changed")
        elif (
            not capsule_source_fingerprint
            or not fresh_source_fingerprint
            or capsule_source_fingerprint != fresh_source_fingerprint
        ):
            result.update(status="STALE_CONTEXT", reason_code="canonical_source_changed")
        elif fresh.get("state") == "completed":
            result.update(status="STOP", reason_code="already_completed")
        elif fresh.get("state") == "open":
            next_action = dispatch.get("next_action") or capsule.get("task", {}).get("objective") or "Execute selected task."
            result.update(
                status="CLAIM_REQUIRED",
                reason_code="task_unclaimed",
                claim_proposal=_claim_proposal(task, worker_id, str(next_action)),
            )
        elif fresh.get("state") == "claimed":
            owner = fresh.get("owner")
            if owner == worker_id:
                result.update(status="READY", reason_code="ownership_confirmed")
            else:
                result.update(status="WAIT", reason_code="owned_by_other_worker")
        else:
            result.update(status="STOP", reason_code="unsupported_canonical_state")

    result["fingerprint"] = _fingerprint({k: v for k, v in result.items() if k != "fingerprint"})
    return result


def prepare(
    boot: dict[str, Any],
    capsule: dict[str, Any],
    preflight_result: dict[str, Any],
    *,
    driver: str,
) -> dict[str, Any]:
    dispatch = validate_boot(boot, capsule)
    if dispatch is None:
        raise ValueError("cannot prepare an invocation for an idle boot")
    if preflight_result.get("schema") != PREFLIGHT_SCHEMA:
        raise ValueError("unsupported preflight schema")
    if preflight_result.get("status") != "READY":
        raise ValueError("preflight must be READY before Worker invocation")
    if preflight_result.get("task") != dispatch.get("task"):
        raise ValueError("preflight task does not match dispatch")
    worker_id = preflight_result.get("worker_id")
    if not worker_id:
        raise ValueError("preflight worker_id is required")

    execution_budget = None
    dispatch_budget = dispatch.get("execution_budget")
    if dispatch_budget is not None:
        execution_budget = normalize_execution_budget(dispatch_budget)

    result_required = [
        "schema",
        "invocation_fingerprint",
        "worker_id",
        "status",
        "summary",
        "next_action",
        "artifacts",
        "requests",
    ]
    instructions = [
        "Use only the selected task and Context Capsule as default context.",
        "Page in referenced sources when required; do not guess missing facts.",
        "Do not mutate canonical coordination state directly.",
        "Return one structured ai-os-worker-result:v1 object.",
    ]
    if execution_budget is not None and requires_execution_usage(execution_budget):
        result_required.append("execution_usage")
        instructions.append(
            "Report ai-os-execution-usage:v1 for every active step, tool-call, "
            "or token limit. Usage is non-authoritative execution accounting."
        )

    invocation = {
        "schema": INVOCATION_SCHEMA,
        "authoritative": False,
        "persist_required": True,
        "driver": driver,
        "worker_id": worker_id,
        "task": dispatch["task"],
        "process": dispatch["process"],
        "target_repository": dispatch.get("target_repository"),
        "canonical_through_comment_id": preflight_result.get("canonical_through_comment_id"),
        "canonical_source_fingerprint": preflight_result.get("canonical_source_fingerprint"),
        "source_plan_fingerprint": boot.get("source_plan_fingerprint"),
        "preflight_fingerprint": preflight_result.get("fingerprint"),
        "input": {
            "dispatch": dispatch,
            "capsule": capsule,
        },
        "result_contract": {
            "schema": RESULT_SCHEMA,
            "statuses": sorted(RESULT_STATUSES),
            "required": result_required,
        },
        "instructions": instructions,
    }
    if execution_budget is not None:
        invocation["execution_budget"] = execution_budget
    invocation["fingerprint"] = _fingerprint(invocation)
    return invocation


def normalize(invocation: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if invocation.get("schema") != INVOCATION_SCHEMA:
        raise ValueError("unsupported invocation schema")
    if result.get("schema") != RESULT_SCHEMA:
        raise ValueError("unsupported Worker result schema")
    if result.get("invocation_fingerprint") != invocation.get("fingerprint"):
        raise ValueError("Worker result does not match invocation fingerprint")
    if result.get("worker_id") != invocation.get("worker_id"):
        raise ValueError("Worker result identity does not match invocation")

    execution_budget = execution_budget_from_invocation(invocation)
    execution_usage = validate_result_against_budget(invocation, result)

    status = result.get("status")
    if status not in RESULT_STATUSES:
        raise ValueError(f"unsupported Worker result status: {status!r}")
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Worker result summary is required")
    next_action = result.get("next_action")
    if status == "completed":
        if next_action is not None:
            raise ValueError("completed result must use next_action=null")
    elif not isinstance(next_action, str) or not next_action.strip():
        raise ValueError(f"{status} result requires next_action")
    artifacts = result.get("artifacts")
    requests = result.get("requests")
    if not isinstance(artifacts, list) or any(not isinstance(x, str) for x in artifacts):
        raise ValueError("Worker result artifacts must be strings")
    if not isinstance(requests, list) or any(not isinstance(x, str) for x in requests):
        raise ValueError("Worker result requests must be strings")

    event_type = {
        "completed": "RESULT",
        "progress": "PROGRESS",
        "page_fault": "PROGRESS",
        "blocked": "HANDOFF",
        "failed": "HANDOFF",
    }[status]
    event = {
        "type": event_type,
        "agent_id": invocation["worker_id"],
        "task": invocation["task"],
        "idempotency_key": _event_key(
            event_type,
            invocation.get("fingerprint"),
            status,
            summary,
            next_action,
            ",".join(artifacts),
        ),
        "summary": summary,
        "next_action": None if event_type == "RESULT" else next_action,
        "artifacts": artifacts,
    }
    outcome = {
        "schema": OUTCOME_SCHEMA,
        "authoritative": False,
        "persist_required": True,
        "worker_id": invocation["worker_id"],
        "task": invocation["task"],
        "status": status,
        "summary": summary,
        "next_action": next_action,
        "artifacts": artifacts,
        "requests": requests,
        "invocation_fingerprint": invocation.get("fingerprint"),
        "canonical_through_comment_id": invocation.get("canonical_through_comment_id"),
        "canonical_source_fingerprint": invocation.get("canonical_source_fingerprint"),
        "event_proposal": {
            "schema": "ai-bb-event-proposal:v1",
            "authoritative": False,
            "persist_required": True,
            "event": event,
        },
    }
    if execution_budget is not None:
        outcome["execution_budget"] = execution_budget
    if execution_usage is not None:
        outcome["execution_usage"] = execution_usage
    outcome["fingerprint"] = _fingerprint(outcome)
    return outcome


def gate(boot: dict[str, Any], fresh: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    if boot.get("schema") != BOOT_SCHEMA:
        raise ValueError("unsupported boot schema")
    if outcome.get("schema") != OUTCOME_SCHEMA:
        raise ValueError("unsupported runtime outcome schema")
    task = outcome.get("task")
    worker_id = outcome.get("worker_id")
    eligible = True
    reason = "fresh_owner_confirmed"

    if fresh.get("task") != task:
        eligible, reason = False, "task_mismatch"
    elif fresh.get("history_safe") is not True or fresh.get("state") == "history_unsafe":
        eligible, reason = False, "history_unsafe"
    elif fresh.get("state") != "claimed":
        eligible, reason = False, "task_not_claimed"
    elif fresh.get("owner") != worker_id:
        eligible, reason = False, "ownership_changed"
    elif fresh.get("through_comment_id") != outcome.get("canonical_through_comment_id"):
        eligible, reason = False, "canonical_history_changed"
    elif fresh.get("source_fingerprint") != outcome.get("canonical_source_fingerprint"):
        eligible, reason = False, "canonical_source_changed"

    result = {
        "schema": GATE_SCHEMA,
        "authoritative": False,
        "persist_required": True,
        "eligible_for_persistence": eligible,
        "reason_code": reason,
        "task": task,
        "worker_id": worker_id,
        "source_plan_fingerprint": boot.get("source_plan_fingerprint"),
        "outcome_fingerprint": outcome.get("fingerprint"),
        "canonical_through_comment_id": fresh.get("through_comment_id"),
        "canonical_source_fingerprint": fresh.get("source_fingerprint"),
        "event_proposal": outcome.get("event_proposal") if eligible else None,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def command_preflight(args: argparse.Namespace) -> None:
    capsule = _read(args.capsule) if args.capsule else None
    _write(args.output, preflight(_read(args.boot), capsule, _read(args.fresh), worker_id=args.worker_id))


def command_prepare(args: argparse.Namespace) -> None:
    _write(
        args.output,
        prepare(_read(args.boot), _read(args.capsule), _read(args.preflight), driver=args.driver),
    )


def command_normalize(args: argparse.Namespace) -> None:
    _write(args.output, normalize(_read(args.invocation), _read(args.result)))


def command_gate(args: argparse.Namespace) -> None:
    _write(args.output, gate(_read(args.boot), _read(args.fresh), _read(args.outcome)))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aios-runtime")
    sub = root.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight", help="gate a Worker boot against fresh canonical replay")
    p.add_argument("--boot", required=True)
    p.add_argument("--capsule")
    p.add_argument("--fresh", required=True)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--output")
    p.set_defaults(func=command_preflight)

    p = sub.add_parser("prepare", help="build one provider-neutral Worker invocation")
    p.add_argument("--boot", required=True)
    p.add_argument("--capsule", required=True)
    p.add_argument("--preflight", required=True)
    p.add_argument("--driver", default="manual")
    p.add_argument("--output")
    p.set_defaults(func=command_prepare)

    p = sub.add_parser("normalize", help="normalize one structured Worker result")
    p.add_argument("--invocation", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--output")
    p.set_defaults(func=command_normalize)

    p = sub.add_parser("gate", help="postflight gate before canonical persistence")
    p.add_argument("--boot", required=True)
    p.add_argument("--fresh", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--output")
    p.set_defaults(func=command_gate)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
