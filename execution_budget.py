"""Non-authoritative execution budget primitives for Runtime compute.

Budgets constrain execution resources but never grant capability, ownership, or
canonical persistence authority. Wall-clock deadlines are Runtime-observed at
the subprocess boundary. Step/tool/token counters are contract-checked when a
budget enables them and therefore require explicit non-authoritative usage data
from the compute adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime_middleware import MiddlewareBlocked, RuntimeMiddleware

BUDGET_SCHEMA = "ai-os-execution-budget:v1"
USAGE_SCHEMA = "ai-os-execution-usage:v1"

_LIMIT_KEYS = (
    "deadline_seconds",
    "max_steps",
    "max_tool_calls",
    "max_tokens",
)
_USAGE_BY_LIMIT = {
    "max_steps": "steps",
    "max_tool_calls": "tool_calls",
    "max_tokens": "tokens",
}


def _positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be null or a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be null or a non-negative integer")
    return value


def normalize_execution_budget(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("execution_budget must be an object")

    allowed = {"schema", "authoritative", *_LIMIT_KEYS}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"execution_budget has unsupported fields: {sorted(unknown)!r}"
        )
    if value.get("schema") != BUDGET_SCHEMA:
        raise ValueError(
            f"unsupported execution budget schema: {value.get('schema')!r}"
        )
    if value.get("authoritative") is not False:
        raise ValueError("execution budget must be explicitly non-authoritative")

    budget = {
        "schema": BUDGET_SCHEMA,
        "authoritative": False,
        "deadline_seconds": _positive_int(
            value.get("deadline_seconds"), "deadline_seconds"
        ),
        "max_steps": _positive_int(value.get("max_steps"), "max_steps"),
        "max_tool_calls": _positive_int(
            value.get("max_tool_calls"), "max_tool_calls"
        ),
        "max_tokens": _positive_int(value.get("max_tokens"), "max_tokens"),
    }
    if all(budget[key] is None for key in _LIMIT_KEYS):
        raise ValueError("execution budget must define at least one limit")
    return budget


def execution_budget_from_invocation(
    invocation: Mapping[str, Any],
) -> dict[str, Any] | None:
    value = invocation.get("execution_budget")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("execution_budget must be an object")
    return normalize_execution_budget(value)


def requires_execution_usage(budget: Mapping[str, Any]) -> bool:
    normalized = normalize_execution_budget(budget)
    return any(normalized[key] is not None for key in _USAGE_BY_LIMIT)


def validate_execution_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("execution_usage must be an object")

    allowed = {"schema", "authoritative", "steps", "tool_calls", "tokens"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"execution_usage has unsupported fields: {sorted(unknown)!r}"
        )
    if value.get("schema") != USAGE_SCHEMA:
        raise ValueError(
            f"unsupported execution usage schema: {value.get('schema')!r}"
        )
    if value.get("authoritative") is not False:
        raise ValueError("execution usage must be explicitly non-authoritative")

    return {
        "schema": USAGE_SCHEMA,
        "authoritative": False,
        "steps": _non_negative_int(value.get("steps"), "steps"),
        "tool_calls": _non_negative_int(
            value.get("tool_calls"), "tool_calls"
        ),
        "tokens": _non_negative_int(value.get("tokens"), "tokens"),
    }


def execution_usage_from_result(
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    value = result.get("execution_usage")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("execution_usage must be an object")
    return validate_execution_usage(value)


def validate_result_against_budget(
    invocation: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    budget = execution_budget_from_invocation(invocation)
    if budget is None:
        return execution_usage_from_result(result)

    active_counters = {
        limit_key: usage_key
        for limit_key, usage_key in _USAGE_BY_LIMIT.items()
        if budget[limit_key] is not None
    }
    usage = execution_usage_from_result(result)
    if active_counters and usage is None:
        raise ValueError(
            "execution_usage is required by the active execution budget"
        )
    if usage is None:
        return None

    for limit_key, usage_key in active_counters.items():
        observed = usage[usage_key]
        if observed is None:
            raise ValueError(
                f"execution_usage.{usage_key} is required by {limit_key}"
            )
        limit = budget[limit_key]
        if observed > limit:
            raise ValueError(
                f"execution budget exceeded: "
                f"{usage_key}={observed} > {limit_key}={limit}"
            )
    return usage


def effective_timeout_seconds(
    invocation: Mapping[str, Any],
    requested_timeout_seconds: int,
) -> tuple[int, bool]:
    if (
        not isinstance(requested_timeout_seconds, int)
        or isinstance(requested_timeout_seconds, bool)
        or requested_timeout_seconds < 1
    ):
        raise ValueError("timeout_seconds must be positive")

    budget = execution_budget_from_invocation(invocation)
    if budget is None or budget["deadline_seconds"] is None:
        return requested_timeout_seconds, False

    deadline = int(budget["deadline_seconds"])
    return min(requested_timeout_seconds, deadline), (
        deadline <= requested_timeout_seconds
    )


class ExecutionBudgetMiddleware(RuntimeMiddleware):
    """Fail closed on malformed or exceeded execution budgets."""

    name = "execution-budget"

    def before_compute(self, invocation: Mapping[str, Any]) -> None:
        if invocation.get("execution_budget") is None:
            return
        try:
            execution_budget_from_invocation(invocation)
        except ValueError as exc:
            raise MiddlewareBlocked(
                self.name, "before_compute", str(exc)
            ) from exc

    def after_compute(
        self,
        invocation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        try:
            validate_result_against_budget(invocation, result)
        except ValueError as exc:
            raise MiddlewareBlocked(
                self.name, "after_compute", str(exc)
            ) from exc
