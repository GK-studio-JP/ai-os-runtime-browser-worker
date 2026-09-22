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
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read()
    except Exception as exc:
        raise LauncherError(f"HTTP failure for {method} {url}: {exc}") from exc


def _json(method: str, url: str, **kwargs: Any) -> Any:
    raw = _request(method, url, **kwargs)
    return json.loads(raw.decode()) if raw else None


RETRYABLE_COMMAND_ACTIONS = {"getPage", "switchPage", "goto"}
TRANSIENT_RELAY_ERROR_MARKERS = (
    "supabase 502",
    "502 bad gateway",
    "http error 502",
    "supabase 503",
    "503 service unavailable",
    "http error 503",
    "supabase 504",
    "504 gateway timeout",
    "http error 504",
    "the read operation timed out",
    "read operation timed out",
)


def _transient_relay_error(message: str) -> bool:
    value = str(message or "").lower()
    return any(marker in value for marker in TRANSIENT_RELAY_ERROR_MARKERS)


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
        command_args = args or {}
        max_attempts = 3 if action in RETRYABLE_COMMAND_ACTIONS else 1
        end = time.monotonic() + timeout
        last_transient_error = ""

        for attempt in range(1, max_attempts + 1):
            if time.monotonic() >= end:
                break
            command_id = f"launcher-{uuid.uuid4().hex}"
            try:
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
            except LauncherError as exc:
                if (
                    attempt < max_attempts
                    and _transient_relay_error(str(exc))
                ):
                    last_transient_error = str(exc)
                    time.sleep(0.5)
                    continue
                raise

            retry = False
            while time.monotonic() < end:
                try:
                    rows = self.rest(
                        "GET",
                        f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{command_id}&select=status,result,error&limit=1",
                    ) or []
                except LauncherError as exc:
                    if (
                        attempt < max_attempts
                        and _transient_relay_error(str(exc))
                    ):
                        last_transient_error = str(exc)
                        retry = True
                        break
                    raise

                row = rows[0] if rows else {}
                if row.get("status") == "done":
                    return row.get("result")
                if row.get("status") == "error":
                    error = str(row.get("error") or "")
                    if (
                        attempt < max_attempts
                        and _transient_relay_error(error)
                    ):
                        last_transient_error = error
                        retry = True
                        break
                    raise LauncherError(f"Browser Agent {action} failed: {error}")
                time.sleep(0.5)

            if retry:
                time.sleep(0.5)
                continue
            break

        suffix = (
            f" after transient relay failure: {last_transient_error}"
            if last_transient_error
            else ""
        )
        raise LauncherError(f"Browser Agent {action} timed out{suffix}")

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
        last_transient_error = ""
        while time.monotonic() < end:
            try:
                rows = self.rest(
                    "GET",
                    f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{command_id}&select=status,result,error&limit=1",
                ) or []
            except LauncherError as exc:
                if _transient_relay_error(str(exc)):
                    last_transient_error = str(exc)
                    time.sleep(0.5)
                    continue
                raise
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

        timeout_error = "timeout"
        if last_transient_error:
            timeout_error += f" after transient relay failure: {last_transient_error}"
        receipt = make_tool_receipt(
            run_id=run_id,
            step=step,
            action_id=command_id,
            tool=action,
            status="error",
            args=command_args,
            output={"error": timeout_error},
        )
        raise RelayCommandError(
            f"Browser Agent {action} timed out"
            + (
                f" after transient relay failure: {last_transient_error}"
                if last_transient_error
                else ""
            ),
            receipt,
        )
