from __future__ import annotations

import json
import re
from typing import Any

TRIAGE_CAPSULE_SCHEMA = "aios-dream-triage-capsule:v1"
TRIAGE_RESULT_SCHEMA = "aios-dream-triage-result:v1"
TRIAGE_VERSION = 1
DIMENSIONS = (
    "operational_impact",
    "reuse_scope",
    "novelty",
    "recurrence",
    "evidence_strength",
)
WEIGHTS = {
    "operational_impact": 0.30,
    "reuse_scope": 0.25,
    "novelty": 0.15,
    "recurrence": 0.15,
    "evidence_strength": 0.15,
}
FINAL_STATES = {"completed", "open", "claimed", "history_unsafe"}
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


def validate_triage_capsule(capsule: Any) -> dict[str, Any]:
    if not isinstance(capsule, dict):
        raise ValueError("triage capsule must be an object")
    if capsule.get("schema") != TRIAGE_CAPSULE_SCHEMA:
        raise ValueError("unsupported triage capsule schema")
    task = _require_string(capsule.get("task"), "task")
    if not re.fullmatch(r"#\d+", task):
        raise ValueError("task must be a #<number> pointer")
    source_fingerprint = _require_string(
        capsule.get("source_fingerprint"), "source_fingerprint"
    )
    if not FINGERPRINT_RE.fullmatch(source_fingerprint):
        raise ValueError("source_fingerprint must be sha256:<64 lowercase hex>")
    if capsule.get("final_state") not in FINAL_STATES:
        raise ValueError("unsupported final_state")
    _require_string(capsule.get("objective"), "objective")
    _require_string(capsule.get("result_summary", ""), "result_summary", allow_empty=True)
    for field in ("corrections", "artifacts", "verification"):
        if not isinstance(capsule.get(field), list):
            raise ValueError(f"{field} must be a list")
    if capsule["final_state"] == "completed" and not capsule["result_summary"].strip():
        raise ValueError("completed capsule requires result_summary")
    return capsule


def triage_prompt(capsule: dict[str, Any]) -> str:
    validate_triage_capsule(capsule)
    compact = json.dumps(
        capsule,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "You are the salience triage stage for AIOS Nightly Dream. "
        "Use only CAPSULE. Do not browse, use search, or call external tools. "
        "Assess reusable operational knowledge, not user psychology. "
        "Return exactly one JSON object and no Markdown. "
        f"source_fingerprint must be {capsule['source_fingerprint']} and "
        f"triage_version must be {TRIAGE_VERSION}. "
        "Each dimension must be a number from 0.0 to 1.0. "
        "reasons must be 1 to 5 concise evidence-based strings. "
        "Required JSON shape: "
        '{"kind":"finish","schema":"aios-dream-triage-result:v1",'
        '"source_fingerprint":"sha256:...","triage_version":1,'
        '"dimensions":{"operational_impact":0.0,"reuse_scope":0.0,'
        '"novelty":0.0,"recurrence":0.0,"evidence_strength":0.0},'
        '"reasons":["..."]}. '
        f"CAPSULE={compact}"
    )


def _dimension(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"dimension {name} must be numeric")
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"dimension {name} must be within [0, 1]")
    return number


def _decision(score: float) -> str:
    if score < 0.35:
        return "skip"
    if score < 0.65:
        return "defer"
    return "deep"


def _result(
    capsule: dict[str, Any],
    *,
    dimensions: dict[str, float],
    reasons: list[str],
    decision_override: str | None = None,
) -> dict[str, Any]:
    score = round(
        sum(dimensions[name] * WEIGHTS[name] for name in DIMENSIONS),
        4,
    )
    return {
        "schema": TRIAGE_RESULT_SCHEMA,
        "source_fingerprint": capsule["source_fingerprint"],
        "triage_version": TRIAGE_VERSION,
        "salience": score,
        "dimensions": dimensions,
        "decision": decision_override or _decision(score),
        "reasons": reasons,
    }


def deterministic_triage(capsule: dict[str, Any]) -> dict[str, Any] | None:
    validate_triage_capsule(capsule)
    zeros = {name: 0.0 for name in DIMENSIONS}
    if capsule["final_state"] == "history_unsafe":
        return _result(
            capsule,
            dimensions=zeros,
            reasons=["history_unsafe: canonical evidence is unusable"],
            decision_override="skip",
        )
    if capsule["final_state"] != "completed":
        return _result(
            capsule,
            dimensions=zeros,
            reasons=[f"incomplete: final_state={capsule['final_state']}"],
            decision_override="defer",
        )
    if not capsule["verification"]:
        return _result(
            capsule,
            dimensions=zeros,
            reasons=["insufficient_evidence: completed task has no verification"],
            decision_override="defer",
        )
    return None


def normalize_triage_result(
    capsule: dict[str, Any],
    raw: Any,
) -> dict[str, Any]:
    validate_triage_capsule(capsule)
    if not isinstance(raw, dict) or raw.get("kind") != "finish":
        raise ValueError("Gemini triage must return kind=finish")
    if raw.get("schema") != TRIAGE_RESULT_SCHEMA:
        raise ValueError("unsupported Gemini triage result schema")
    if raw.get("source_fingerprint") != capsule["source_fingerprint"]:
        raise ValueError("Gemini triage source_fingerprint mismatch")
    if raw.get("triage_version") != TRIAGE_VERSION:
        raise ValueError("Gemini triage version mismatch")
    values = raw.get("dimensions")
    if not isinstance(values, dict):
        raise ValueError("Gemini triage dimensions must be an object")
    dimensions = {
        name: _dimension(values.get(name), name)
        for name in DIMENSIONS
    }
    reasons = raw.get("reasons")
    if not isinstance(reasons, list) or not 1 <= len(reasons) <= 5:
        raise ValueError("Gemini triage reasons must contain 1 to 5 items")
    cleaned: list[str] = []
    for reason in reasons:
        text = _require_string(reason, "reason").strip()
        if len(text) > 240:
            raise ValueError("Gemini triage reason exceeds 240 characters")
        cleaned.append(text)
    return _result(capsule, dimensions=dimensions, reasons=cleaned)
