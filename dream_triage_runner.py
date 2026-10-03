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
    """Keep Dream evaluation on one current page while reusing ask_gemini."""

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


class GeminiEvaluatorSession:
    """Lazily start one Gemini browser session and reuse it for a Dream cycle."""

    def __init__(self, relay: Relay):
        self._relay = relay
        self._page_relay: _CurrentPageRelay | None = None

    @property
    def started(self) -> bool:
        return self._page_relay is not None

    def _current_page(self) -> _CurrentPageRelay:
        if self._page_relay is None:
            started = self._relay.command("start", {})
            current_url = (
                str(started.get("url") or "")
                if isinstance(started, dict)
                else ""
            )
            self._page_relay = _CurrentPageRelay(
                self._relay,
                current_url=current_url,
            )
        return self._page_relay

    def triage(self, capsule: dict[str, Any]) -> dict[str, Any]:
        deterministic = deterministic_triage(capsule)
        if deterministic is not None:
            return deterministic

        raw = ask_gemini(
            self._current_page(),
            0,
            triage_prompt(capsule),
        )
        return normalize_triage_result(capsule, raw)

    def deep(self, prompt_text: str) -> dict[str, Any]:
        return ask_gemini(
            self._current_page(),
            0,
            prompt_text,
        )


def run_triage(
    relay: Relay,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    """Backward-compatible one-shot triage helper."""
    return GeminiEvaluatorSession(relay).triage(capsule)
