from __future__ import annotations

from typing import Any

from ai_os_browser_worker.dream_triage import (
    deterministic_triage,
    normalize_triage_result,
    triage_prompt,
)
from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import ask_gemini


class _CurrentPageRelay:
    """Keep Dream triage on the current page while reusing ask_gemini."""

    def __init__(self, relay: Relay):
        self._relay = relay

    def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: int = 75,
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
    # Avoid newPage/switchPage: their eager Light observation can hit the relay
    # deadman on Gemini even when navigation itself succeeded.
    relay.command("start", {}, timeout=125)
    raw = ask_gemini(
        _CurrentPageRelay(relay),
        0,
        triage_prompt(capsule),
    )
    return normalize_triage_result(capsule, raw)
