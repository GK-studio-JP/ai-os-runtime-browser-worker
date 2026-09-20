from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

RECEIPT_SCHEMA = "ai-os-tool-receipt:v1"
RECEIPT_STATUSES = {"success", "error", "partial_success"}

_RECEIPT_KEYS = {
    "schema", "authoritative", "acceptance", "run_id", "step", "action_id",
    "tool", "status", "args_sha256", "output_sha256", "output_bytes", "created_at",
}


@dataclass(frozen=True)
class LoopDecision:
    action: Literal["continue", "warn", "stop"]
    reason_code: str
    tool: str
    count: int
    threshold: int

    @property
    def stop(self) -> bool:
        return self.action == "stop"


class LoopGuard:
    """Deterministic, run-scoped loop detection for Browser Agent actions.

    Only stable hashes and tool names are retained. Raw arguments are normalized
    transiently and are never retained in loop history.
    """

    SALIENT_FIELDS = ("path", "url", "query", "command", "pattern", "glob", "cmd")

    def __init__(
        self,
        *,
        warning_limit: int = 3,
        hard_limit: int = 5,
        window: int = 20,
        tool_frequency_warning_limit: int = 30,
        tool_frequency_hard_limit: int = 50,
        tool_freq_overrides: dict[str, tuple[int, int]] | None = None,
        max_runs: int = 128,
    ) -> None:
        self._validate_pair("identical-call", warning_limit, hard_limit)
        self._validate_pair(
            "tool-frequency",
            tool_frequency_warning_limit,
            tool_frequency_hard_limit,
        )
        if not isinstance(window, int) or isinstance(window, bool) or window < 1:
            raise ValueError("window must be a positive integer")
        if not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1:
            raise ValueError("max_runs must be a positive integer")

        overrides = dict(tool_freq_overrides or {})
        for tool, limits in overrides.items():
            if not isinstance(tool, str) or not tool.strip():
                raise ValueError("tool frequency override names must be non-empty strings")
            if not isinstance(limits, tuple) or len(limits) != 2:
                raise ValueError("tool frequency overrides must be (warning, hard) tuples")
            self._validate_pair(f"tool-frequency override for {tool}", limits[0], limits[1])

        self.warning_limit = warning_limit
        self.hard_limit = hard_limit
        self.window = window
        self.tool_frequency_warning_limit = tool_frequency_warning_limit
        self.tool_frequency_hard_limit = tool_frequency_hard_limit
        self.tool_freq_overrides = overrides
        self.max_runs = max_runs
        override_hard = max((hard for _, hard in overrides.values()), default=0)
        self.tool_window = max(window, tool_frequency_hard_limit, override_hard)

        self._run_lru: OrderedDict[str, None] = OrderedDict()
        self._keys: dict[str, deque[str]] = {}
        self._tools: dict[str, deque[str]] = {}
        self._key_warned: dict[str, set[str]] = {}
        self._tool_warned: dict[str, set[str]] = {}

    @staticmethod
    def _validate_pair(name: str, warning: int, hard: int) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (warning, hard)
        ):
            raise ValueError(f"{name} limits must be positive integers")
        if warning >= hard:
            raise ValueError(f"{name} warning limit must be lower than hard limit")

    def _touch_run(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if run_id in self._run_lru:
            self._run_lru.move_to_end(run_id)
            return

        self._run_lru[run_id] = None
        self._keys[run_id] = deque(maxlen=self.window)
        self._tools[run_id] = deque(maxlen=self.tool_window)
        self._key_warned[run_id] = set()
        self._tool_warned[run_id] = set()

        while len(self._run_lru) > self.max_runs:
            stale, _ = self._run_lru.popitem(last=False)
            self._keys.pop(stale, None)
            self._tools.pop(stale, None)
            self._key_warned.pop(stale, None)
            self._tool_warned.pop(stale, None)

    @staticmethod
    def _as_mapping(args: Any) -> Any:
        if isinstance(args, str):
            try:
                return json.loads(args)
            except (TypeError, ValueError, json.JSONDecodeError):
                return args
        return args

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _normalized_key_material(self, tool: str, args: Any) -> Any:
        value = self._as_mapping(args)
        if tool == "read_file" and isinstance(value, dict):
            return {
                "path": value.get("path"),
                "start": value.get("start"),
                "end": value.get("end") if value.get("end") is not None else "end",
            }
        if tool in {"write_file", "str_replace"}:
            return value
        if isinstance(value, dict):
            salient = {field: value[field] for field in self.SALIENT_FIELDS if field in value}
            return salient or value
        return value

    def _call_key(self, tool: str, args: Any, state_fingerprint: str | None) -> str:
        material = {
            "tool": tool,
            "key": self._normalized_key_material(tool, args),
            "state_fingerprint": state_fingerprint,
        }
        return hashlib.sha256(self._canonical(material).encode("utf-8")).hexdigest()

    def reset(self, run_id: str) -> None:
        self._run_lru.pop(run_id, None)
        self._keys.pop(run_id, None)
        self._tools.pop(run_id, None)
        self._key_warned.pop(run_id, None)
        self._tool_warned.pop(run_id, None)

    def _tool_limits(self, tool: str) -> tuple[int, int]:
        return self.tool_freq_overrides.get(
            tool,
            (self.tool_frequency_warning_limit, self.tool_frequency_hard_limit),
        )

    def observe_tool_call(
        self,
        run_id: str,
        tool: str,
        args: Any,
        *,
        state_fingerprint: str | None = None,
    ) -> LoopDecision:
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool is required")
        if state_fingerprint is not None and not isinstance(state_fingerprint, str):
            raise ValueError("state_fingerprint must be a string or None")

        self._touch_run(run_id)
        key = self._call_key(tool, args, state_fingerprint)
        key_history = self._keys[run_id]
        tool_history = self._tools[run_id]
        key_history.append(key)
        tool_history.append(tool)

        key_count = Counter(key_history)[key]
        tool_count = Counter(tool_history)[tool]
        tool_warning, tool_hard = self._tool_limits(tool)

        if key_count >= self.hard_limit:
            return LoopDecision("stop", "identical_call_hard_limit", tool, key_count, self.hard_limit)
        if tool_count >= tool_hard:
            return LoopDecision("stop", "tool_frequency_hard_limit", tool, tool_count, tool_hard)

        key_warned = self._key_warned[run_id]
        tool_warned = self._tool_warned[run_id]
        if key_count < self.warning_limit:
            key_warned.discard(key)
        if tool_count < tool_warning:
            tool_warned.discard(tool)

        if key_count >= self.warning_limit and key not in key_warned:
            key_warned.add(key)
            return LoopDecision("warn", "identical_call_warning", tool, key_count, self.warning_limit)
        if tool_count >= tool_warning and tool not in tool_warned:
            tool_warned.add(tool)
            return LoopDecision("warn", "tool_frequency_warning", tool, tool_count, tool_warning)

        return LoopDecision("continue", "within_limits", tool, max(key_count, tool_count), 0)


def _canonical_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def make_tool_receipt(
    *,
    run_id: str,
    step: int,
    tool: str,
    status: str,
    args: Any,
    output: Any,
    action_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    if not isinstance(step, int) or isinstance(step, bool) or step < 1:
        raise ValueError("step must be a positive integer")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("tool is required")
    if status not in RECEIPT_STATUSES:
        raise ValueError(f"unsupported receipt status: {status!r}")
    if action_id is not None and (
        not isinstance(action_id, str) or not action_id.strip()
    ):
        raise ValueError("action_id must be null or a non-empty string")

    output_bytes = _canonical_bytes(output)
    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "authoritative": False,
        "acceptance": False,
        "run_id": run_id,
        "step": step,
        "action_id": action_id,
        "tool": tool,
        "status": status,
        "args_sha256": _digest(args),
        "output_sha256": "sha256:" + hashlib.sha256(output_bytes).hexdigest(),
        "output_bytes": len(output_bytes),
        "created_at": timestamp,
    }
    validate_tool_receipt(receipt)
    return receipt


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value[7:]
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)


def validate_tool_receipt(receipt: dict[str, Any]) -> None:
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be an object")
    if set(receipt) != _RECEIPT_KEYS:
        raise ValueError("receipt keys do not match schema")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("unsupported receipt schema")
    if receipt.get("authoritative") is not False:
        raise ValueError("receipt must be non-authoritative")
    if receipt.get("acceptance") is not False:
        raise ValueError("receipt is execution evidence, not acceptance")
    if not isinstance(receipt.get("run_id"), str) or not receipt["run_id"].strip():
        raise ValueError("run_id is required")
    if (
        not isinstance(receipt.get("step"), int)
        or isinstance(receipt["step"], bool)
        or receipt["step"] < 1
    ):
        raise ValueError("step must be a positive integer")

    action_id = receipt.get("action_id")
    if action_id is not None and (
        not isinstance(action_id, str) or not action_id.strip()
    ):
        raise ValueError("action_id must be null or a non-empty string")

    if not isinstance(receipt.get("tool"), str) or not receipt["tool"].strip():
        raise ValueError("tool is required")
    if receipt.get("status") not in RECEIPT_STATUSES:
        raise ValueError("unsupported receipt status")
    if not _valid_sha256(receipt.get("args_sha256")) or not _valid_sha256(
        receipt.get("output_sha256")
    ):
        raise ValueError("receipt hashes must be full sha256 digests")
    if (
        not isinstance(receipt.get("output_bytes"), int)
        or isinstance(receipt["output_bytes"], bool)
        or receipt["output_bytes"] < 0
    ):
        raise ValueError("output_bytes must be a non-negative integer")

    created_at = receipt.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("created_at is required")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be ISO-8601") from exc


def receipt_fingerprint(receipt: dict[str, Any]) -> str:
    validate_tool_receipt(receipt)
    return _digest(receipt)


def browser_state_fingerprint(page: dict[str, Any]) -> str:
    """Hash stable task-page semantics without retaining page contents.

    Browser Agent generations intentionally do not participate: getPage can
    advance generations even when the page itself did not make progress.
    """

    semantic_elements = []
    for element in (page.get("elements") or [])[:200]:
        semantic_elements.append(
            {
                "role": element.get("role"),
                "label": element.get("label"),
                "text": element.get("text"),
                "href": element.get("href")
                or (element.get("attributes") or {}).get("href"),
            }
        )

    material = {
        "url": str(page.get("url") or "").rstrip("/"),
        "pageText": str(page.get("pageText") or "")[:20000],
        "elements": semantic_elements,
    }
    return _digest(material)


def loop_guard_args(
    action: str,
    args: dict[str, Any],
    page: dict[str, Any],
) -> dict[str, Any]:
    """Normalize generation-bound Browser Agent args for stable loop keys."""

    if action != "click":
        return dict(args)

    element_id = str(args.get("elementId") or "")
    element = next(
        (
            item
            for item in page.get("elements") or []
            if str(item.get("id") or "") == element_id
        ),
        None,
    )
    if not element:
        return dict(args)

    return {
        "role": element.get("role"),
        "label": element.get("label"),
        "text": element.get("text"),
        "href": element.get("href")
        or (element.get("attributes") or {}).get("href"),
    }
