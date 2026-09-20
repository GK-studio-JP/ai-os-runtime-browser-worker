from __future__ import annotations

import json
import time
import urllib.request
import uuid
from typing import Any

from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.safety import make_tool_receipt


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    value: Any = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    h = {"User-Agent": "ai-os-runtime-worker", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = None
    if value is not None:
        body = json.dumps(value).encode()
        h["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    except Exception as exc:
        raise LauncherError(f"HTTP failure for {method} {url}: {exc}") from exc


def _json(method: str, url: str, **kwargs: Any) -> Any:
    raw = _request(method, url, **kwargs)
    return json.loads(raw.decode()) if raw else None


class RelayCommandError(LauncherError):
    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt


class Relay:
    def __init__(self, base: str, key: str, session: str):
        self.base = base.rstrip("/")
        self.key = key
        self.session = session
        self.headers = {
            "apikey": key,
            "Content-Type": "application/json",
        }

    def rest(
        self,
        method: str,
        path: str,
        value: Any = None,
        prefer: str | None = None,
    ) -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        return _json(method, f"{self.base}/rest/v1/{path}", value=value, headers=headers)

    def ready(self, timeout: int = 180) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest(
                "GET",
                f"browser_relay_sessions?session_id=eq.{self.session}&select=state,ended_at,last_error",
            ) or []
            if rows and rows[0].get("state") == "ready" and not rows[0].get("ended_at"):
                return
            time.sleep(1)
        raise LauncherError(f"Browser Agent session {self.session} did not become ready")

    def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: int = 75,
    ) -> Any:
        command_id = f"launcher-{uuid.uuid4().hex}"
        self.rest(
            "POST",
            "browser_relay_commands",
            {
                "session_id": self.session,
                "command_id": command_id,
                "action": action,
                "args": args or {},
            },
            "return=representation",
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest(
                "GET",
                f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{command_id}&select=status,result,error&limit=1",
            ) or []
            row = rows[0] if rows else {}
            if row.get("status") == "done":
                return row.get("result")
            if row.get("status") == "error":
                raise LauncherError(f"Browser Agent {action} failed: {row.get('error')}")
            time.sleep(0.5)
        raise LauncherError(f"Browser Agent {action} timed out")

    def command_with_receipt(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        run_id: str,
        step: int,
        timeout: int = 75,
    ) -> tuple[Any, dict[str, Any]]:
        """Execute one Browser Agent command and derive runtime-owned evidence."""

        command_id = f"launcher-{uuid.uuid4().hex}"
        command_args = args or {}
        self.rest(
            "POST",
            "browser_relay_commands",
            {
                "session_id": self.session,
                "command_id": command_id,
                "action": action,
                "args": command_args,
            },
            "return=representation",
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest(
                "GET",
                f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{command_id}&select=status,result,error&limit=1",
            ) or []
            row = rows[0] if rows else {}
            if row.get("status") == "done":
                result = row.get("result")
                receipt = make_tool_receipt(
                    run_id=run_id,
                    step=step,
                    action_id=command_id,
                    tool=action,
                    status="success",
                    args=command_args,
                    output=result,
                )
                return result, receipt
            if row.get("status") == "error":
                error = str(row.get("error") or "")
                receipt = make_tool_receipt(
                    run_id=run_id,
                    step=step,
                    action_id=command_id,
                    tool=action,
                    status="error",
                    args=command_args,
                    output={"error": error},
                )
                raise RelayCommandError(
                    f"Browser Agent {action} failed: {error}",
                    receipt,
                )
            time.sleep(0.5)

        receipt = make_tool_receipt(
            run_id=run_id,
            step=step,
            action_id=command_id,
            tool=action,
            status="error",
            args=command_args,
            output={"error": "timeout"},
        )
        raise RelayCommandError(
            f"Browser Agent {action} timed out",
            receipt,
        )
