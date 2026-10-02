from __future__ import annotations

from typing import Any

from ai_os_browser_worker.dream_triage import (
    deterministic_triage,
    normalize_triage_result,
    triage_prompt,
)
from ai_os_browser_worker.relay import (
    DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
    Relay,
)
from browser_worker_launcher import GEMINI, ask_gemini


class _CurrentPageRelay:
    """Keep Dream triage on the current page while reusing ask_gemini."""

    def __init__(self, relay: Relay, current_url: str = ""):
        self._relay = relay
        self._current_url = str(current_url or "")

    def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: int = DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
    ) -> Any:
        command_args = args or {}
        if action == "switchPage":
            return {"pageIndex": 0}
        if (
            action == "goto"
            and str(command_args.get("url") or "") == GEMINI
            and self._current_url.startswith(GEMINI)
        ):
            return {"url": self._current_url, "reusedCurrentPage": True}
        result = self._relay.command(action, command_args, timeout=timeout)
        if action == "goto" and isinstance(result, dict):
            self._current_url = str(result.get("url") or command_args.get("url") or "")
        return result


def run_triage(
    relay: Relay,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    deterministic = deterministic_triage(capsule)
    if deterministic is not None:
        return deterministic

    # Dream triage owns the Browser Agent page for this bounded model call.
    # Ensure the browser exists, then reuse the current page so this path does
    # not depend on newPage/switchPage. If start already returns Gemini, also
    # avoid navigating to the identical URL again.
    started = relay.command("start", {})
    current_url = str(started.get("url") or "") if isinstance(started, dict) else ""
    raw = ask_gemini(
        _CurrentPageRelay(relay, current_url=current_url),
        0,
        triage_prompt(capsule),
    )
    return normalize_triage_result(capsule, raw)
