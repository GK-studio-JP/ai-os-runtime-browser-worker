from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

from ai_os_browser_worker.navigation_policy import LauncherError, refresh_element_args
from ai_os_browser_worker.relay import Relay
from browser_worker_launcher import GEMINI, ask_gemini, reduce_observation

START_URL = "https://github.com/GK-studio-JP/ai-bulletin-board/issues/52"
ALLOWED_REPOSITORIES = {
    "GK-studio-JP/ai-bulletin-board",
    "GK-studio-JP/ai-os-projects",
    "GK-studio-JP/ai-os-memory",
}
DENIED_CONTROL_TERMS = (
    "delete",
    "archive",
    "merge pull request",
    "revert",
    "settings",
    "danger zone",
)
MAX_PROMPT_CHARS = 24000
MAX_FEEDBACK_CHARS = 4000


def _allowed_github_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LauncherError("Nightly Dream navigation URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise LauncherError("Nightly Dream operator may navigate only to HTTPS github.com URLs")

    path = parsed.path or "/"
    allowed = any(
        path == f"/{repo}" or path.startswith(f"/{repo}/")
        for repo in ALLOWED_REPOSITORIES
    )
    if not allowed:
        raise LauncherError(f"Nightly Dream navigation is outside the allowed repositories: {value}")
    return value


def _element(page: dict[str, Any], element_id: str) -> dict[str, Any] | None:
    return next(
        (row for row in page.get("elements") or [] if str(row.get("id") or "") == element_id),
        None,
    )


def _require_current_element(page: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    element_id = str(args.get("elementId") or args.get("id") or "")
    generation = page.get("generation")
    if generation is None or not element_id.startswith(f"g{generation}-"):
        raise LauncherError(
            f"Nightly Dream operator requires current-generation elementId; "
            f"got {element_id!r} for generation {generation!r}"
        )
    element = _element(page, element_id)
    if element is None:
        raise LauncherError("Nightly Dream operator element is missing from the current observation")
    return element


def _descriptor(element: dict[str, Any]) -> str:
    return str(element.get("label") or element.get("text") or "").strip().lower()


def _safe_branch_selected(page: dict[str, Any]) -> bool:
    for element in page.get("elements") or []:
        if element.get("role") != "radio":
            continue
        label = _descriptor(element)
        states = element.get("states") if isinstance(element.get("states"), dict) else {}
        if "create a new branch for this commit" in label and states.get("checked") is True:
            return True
    return False


def validate_operator_action(
    command: dict[str, Any],
    page: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    action = str(command.get("action") or "")
    raw_args = command.get("args")
    if action not in {"goto", "getPage", "click", "fill", "scroll", "setViewport"}:
        raise LauncherError(f"Nightly Dream operator action is not allowed: {action!r}")
    if not isinstance(raw_args, dict):
        raise LauncherError("Nightly Dream operator action args must be an object")

    args = dict(raw_args)
    if action == "goto":
        args["url"] = _allowed_github_url(str(args.get("url") or ""))
        return action, args

    if action in {"click", "fill"}:
        element = _require_current_element(page, args)
    else:
        return action, args

    current_url = _allowed_github_url(str(page.get("url") or ""))

    if action == "fill":
        if element.get("role") != "textbox":
            raise LauncherError("Nightly Dream fill is limited to current-generation textboxes")
        text = args.get("text")
        if not isinstance(text, str):
            value = args.get("value")
            if not isinstance(value, str):
                raise LauncherError("Nightly Dream fill requires text")
            args["text"] = value
        args.pop("value", None)
        return action, args

    role = str(element.get("role") or "")
    descriptor = _descriptor(element)
    if role == "link":
        attrs = element.get("attributes") if isinstance(element.get("attributes"), dict) else {}
        href = str(attrs.get("href") or element.get("href") or "")
        if not href:
            raise LauncherError("Nightly Dream link click is missing href")
        _allowed_github_url(urllib.parse.urljoin(current_url, href))
        return action, args

    if role not in {"button", "radio"}:
        raise LauncherError("Nightly Dream click is limited to links, buttons, and radios")
    if any(term in descriptor for term in DENIED_CONTROL_TERMS):
        raise LauncherError(f"Nightly Dream dangerous control is denied: {descriptor!r}")
    if role == "radio" and descriptor.startswith("commit directly to"):
        raise LauncherError("Nightly Dream direct commit to a protected branch is denied")
    if descriptor in {"commit changes", "commit changes...", "propose changes"}:
        if not _safe_branch_selected(page):
            raise LauncherError("Nightly Dream repository mutation requires a new-branch target")
    return action, args


def operator_prompt(
    objective: str,
    observation: dict[str, Any],
    *,
    step: int,
    feedback: str,
) -> str:
    return f"""You are the AIOS Nightly Dream operator. You, Gemini, are the work agent.
A scheduled GPT only launched this run and handed you this objective plus Browser Agent access.
The Python runtime is only a safety gate and Browser Agent executor; it does not decide the work for you.

OBJECTIVE:
{objective}

Allowed GitHub repositories:
- GK-studio-JP/ai-bulletin-board
- GK-studio-JP/ai-os-projects
- GK-studio-JP/ai-os-memory

Operate one browser step at a time. Read the visible page, decide the next action, and return exactly one JSON object.
Use only:
{{"kind":"browser_action","action":"goto|getPage|click|fill|scroll|setViewport","args":{{...}},"reason":"..."}}
or
{{"kind":"finish","summary":"...","artifacts":["https://..."],"evidence":[{{"kind":"visited_url","value":"https://..."}}],"reason":"done"}}
or
{{"kind":"wait","reason":"..."}}

Rules:
- You own the reasoning, navigation, issue/comment work, verification, and completion decision.
- Use Browser Agent only. Do not ask the scheduled GPT to perform work.
- For click/fill, use a current-generation elementId from OBSERVATION.
- Never merge a pull request, delete/archive resources, use repository Settings/Danger Zone, or commit directly to a protected branch.
- Repository source changes must use a new branch and pull request.
- Keep Nightly Dream canonical coordination on ai-bulletin-board.
- Follow the current Nightly Dream runbook/contract by browsing ai-os-projects before production mutations.
- Do not expose secrets, cookies, tokens, or transient private browser data.
- If blocked by an unavailable prerequisite, return wait with the concrete reason.

STEP={step}
FEEDBACK={feedback[:MAX_FEEDBACK_CHARS]}
OBSERVATION={json.dumps(observation, ensure_ascii=False, separators=(",", ":"))}
"""


def run_operator(args: argparse.Namespace) -> dict[str, Any]:
    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")

    objective = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not objective:
        raise LauncherError("Nightly Dream operator prompt is empty")
    if len(objective) > MAX_PROMPT_CHARS:
        raise LauncherError("Nightly Dream operator prompt is too large")

    relay = Relay(base, key, args.session_id)
    relay.ready()
    relay.command("start", {})
    relay.command("goto", {"url": START_URL})
    opened = relay.command("newPage", {"url": GEMINI})
    gemini_index = int(opened.get("pageIndex", 1)) if isinstance(opened, dict) else 1

    feedback = "Browser Agent is ready. Begin by reading the current Nightly Dream runbook and contract."
    for step in range(1, args.max_steps + 1):
        relay.command("switchPage", {"index": 0})
        page = relay.command("getPage", {})
        observation = reduce_observation(page, max_text=5000, max_elements=80)
        command = ask_gemini(
            relay,
            gemini_index,
            operator_prompt(objective, observation, step=step, feedback=feedback),
        )

        kind = str(command.get("kind") or "")
        if kind == "wait":
            return {
                "status": "waiting",
                "step": step,
                "reason": str(command.get("reason") or "").strip(),
            }
        if kind == "finish":
            summary = str(command.get("summary") or "").strip()
            if not summary:
                raise LauncherError("Gemini finish requires a non-empty summary")
            return {
                "status": "finished",
                "step": step,
                "summary": summary,
                "artifacts": command.get("artifacts") or [],
                "evidence": command.get("evidence") or [],
            }
        if kind != "browser_action":
            feedback = "Return exactly one browser_action, finish, or wait JSON object."
            continue

        try:
            action, action_args = validate_operator_action(command, page)
            relay.command("switchPage", {"index": 0})
            if action in {"click", "fill"}:
                fresh = relay.command("getPage", {})
                action_args = refresh_element_args(action, action_args, page, fresh)
            result = relay.command(action, action_args)
            feedback = (
                "Executed Browser Agent action successfully: "
                + json.dumps(
                    {"action": action, "result": result},
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )[:MAX_FEEDBACK_CHARS]
            )
        except LauncherError as exc:
            feedback = f"Action rejected by deterministic safety gate: {exc}"

    return {
        "status": "waiting",
        "step": args.max_steps,
        "reason": "Nightly Dream Gemini operator reached the step limit",
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aios-nightly-dream-gemini-operator")
    root.add_argument("--prompt-file", required=True)
    root.add_argument("--session-id", default="gcp-browser-1")
    root.add_argument("--max-steps", type=int, default=60)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        result = run_operator(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
