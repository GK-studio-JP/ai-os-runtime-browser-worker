from __future__ import annotations

from typing import Any

from ai_os_browser_worker.dream_triage import (
    deterministic_triage,
    normalize_triage_result,
    triage_prompt,
)
from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import GEMINI, ask_gemini


def run_triage(
    relay: Relay,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    deterministic = deterministic_triage(capsule)
    if deterministic is not None:
        return deterministic

    opened = relay.command("newPage", {})
    gemini_index = (
        int(opened.get("pageIndex", 1))
        if isinstance(opened, dict)
        else 1
    )
    raw = ask_gemini(
        relay,
        gemini_index,
        triage_prompt(capsule),
    )
    return normalize_triage_result(capsule, raw)
