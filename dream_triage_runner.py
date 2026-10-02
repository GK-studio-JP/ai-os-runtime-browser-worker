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
from browser_worker_launcher import ask_gemini


class _CurrentPageRelay:
    """Keep Dream triage on the current page while reusing ask_gemini."""

    def __init__(self, relay: Relay):
        self._relay = relay

    def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: int = DEFAULT_RELAY_COMMAND_TIMEOUT_SECONDS,
    ) -> Any:
        if action == "switchPage":
            return {"pageIndex": 0}
        return self._relay.command(action, args or {}, timeout=timeout)


def run_triage(
    relay: Relay,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    deterministic = deterministic_triage(capsule)
    if deterministic is not None:
        return deterministic

    # Dream triage owns the Browser Agent page for this bounded model call.
    # Ensure the browser exists, then reuse the current page so this path does
    # not depend on newPage/switchPage creation or eager observations.
    relay.command("start", {})
    raw = ask_gemini(
        _CurrentPageRelay(relay),
        0,
        triage_prompt(capsule),
    )
    return normalize_triage_result(capsule, raw)
