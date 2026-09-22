"""Authority-neutral runtime middleware inspired by DeerFlow's harness hooks.

Middleware may inspect a private snapshot, emit external audit data, or stop
execution by raising MiddlewareBlocked. It never receives the live invocation
or result object, so it cannot grant authority or silently rewrite the runtime
contract.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any


class MiddlewareBlocked(RuntimeError):
    """Fail-closed signal raised when middleware denies compute execution."""

    def __init__(self, middleware: str, phase: str, reason: str) -> None:
        self.middleware = middleware
        self.phase = phase
        self.reason = reason
        super().__init__(f"runtime middleware {middleware!r} blocked {phase}: {reason}")


class RuntimeMiddleware:
    """Base class for non-authoritative Runtime middleware."""

    name = "runtime-middleware"

    def before_compute(self, invocation: Mapping[str, Any]) -> None:
        """Inspect an invocation snapshot before the compute driver runs."""

    def after_compute(
        self,
        invocation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        """Inspect invocation/result snapshots after driver validation."""


class MiddlewareChain:
    """Deterministic middleware chain with mutation isolation and fail-closed errors."""

    def __init__(self, middlewares: Sequence[RuntimeMiddleware] = ()) -> None:
        self._middlewares = tuple(middlewares)

    @property
    def middlewares(self) -> tuple[RuntimeMiddleware, ...]:
        return self._middlewares

    @staticmethod
    def _snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(dict(value))

    @staticmethod
    def _name(middleware: RuntimeMiddleware) -> str:
        value = str(getattr(middleware, "name", "") or "").strip()
        return value or middleware.__class__.__name__

    def before_compute(self, invocation: Mapping[str, Any]) -> None:
        for middleware in self._middlewares:
            name = self._name(middleware)
            try:
                middleware.before_compute(self._snapshot(invocation))
            except MiddlewareBlocked:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"runtime middleware {name!r} failed during before_compute"
                ) from exc

    def after_compute(
        self,
        invocation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        for middleware in reversed(self._middlewares):
            name = self._name(middleware)
            try:
                middleware.after_compute(
                    self._snapshot(invocation),
                    self._snapshot(result),
                )
            except MiddlewareBlocked:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"runtime middleware {name!r} failed during after_compute"
                ) from exc
