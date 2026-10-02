from __future__ import annotations

import argparse
import os
from typing import Any, Callable

from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import ask_gemini
from dream_triage_runner import _CurrentPageRelay

WORKFLOW_URL = (
    "https://github.com/GK-studio-JP/browser-agent/"
    "actions/workflows/nightly-dream-worker.yml"
)


class DreamBootstrapError(RuntimeError):
    pass


def bootstrap_prompt() -> str:
    return """You are the AIOS Nightly Dream bootstrap worker.
The scheduled GPT has only started you; it has no Dream or GitHub-write role.
Return exactly one JSON object and no Markdown:
{"kind":"finish","action":"dispatch_nightly_dream","reason":"..."}
Do not browse or use external tools. The deterministic bootstrap code will
dispatch the fixed Nightly Dream workflow after validating this response."""


def request_dispatch(
    relay: Relay,
    *,
    ask_model: Callable[..., dict[str, Any]] = ask_gemini,
) -> dict[str, Any]:
    started = relay.command("start", {})
    current_url = (
        str(started.get("url") or "")
        if isinstance(started, dict)
        else ""
    )
    raw = ask_model(
        _CurrentPageRelay(relay, current_url=current_url),
        0,
        bootstrap_prompt(),
    )
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != "finish"
        or raw.get("action") != "dispatch_nightly_dream"
    ):
        raise DreamBootstrapError("Gemini did not authorize fixed Dream dispatch")
    return raw


def _run_workflow_buttons(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        element
        for element in page.get("elements") or []
        if element.get("role") == "button"
        and (
            element.get("text") == "Run workflow"
            or element.get("label") == "Run workflow"
        )
    ]


def dispatch_fixed_workflow(relay: Relay) -> None:
    relay.command(
        "goto",
        {
            "url": WORKFLOW_URL,
            "mode": "light",
            "maxElements": 100,
            "maxFrames": 1,
        },
    )
    page = relay.command(
        "getPage",
        {"mode": "light", "maxElements": 100, "maxFrames": 1},
    )
    buttons = _run_workflow_buttons(page)
    if not buttons:
        raise DreamBootstrapError("GitHub Run workflow button unavailable")
    relay.command("click", {"elementId": buttons[0]["id"]})

    page = relay.command(
        "getPage",
        {"mode": "light", "maxElements": 120, "maxFrames": 1},
    )
    buttons = _run_workflow_buttons(page)
    if not buttons:
        raise DreamBootstrapError("GitHub Run workflow confirmation unavailable")
    relay.command("click", {"elementId": buttons[-1]["id"]})

    page = relay.command(
        "getPage",
        {"mode": "light", "maxElements": 80, "maxFrames": 1},
    )
    text = str(page.get("pageText") or "")
    if (
        "Workflow run was successfully requested" not in text
        and "queued" not in text.lower()
        and "in progress" not in text.lower()
    ):
        raise DreamBootstrapError(
            "Dream workflow dispatch was not visibly verified"
        )


def run_bootstrap(relay: Relay) -> None:
    request_dispatch(relay)
    dispatch_fixed_workflow(relay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)

    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise DreamBootstrapError(
            "SUPABASE_URL and SUPABASE_SECRET_KEY are required"
        )
    relay = Relay(base, key, args.session_id)
    relay.ready()
    run_bootstrap(relay)
    print("AIOS_DREAM_BOOTSTRAP_DISPATCHED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
