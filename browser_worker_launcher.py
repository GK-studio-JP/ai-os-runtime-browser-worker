from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

PROCESS = "PROC-RUNTIME-BROWSER-WORKER"
BOARD = "GK-studio-JP/ai-bulletin-board"
SCHEDULER = "GK-studio-JP/ai-os-scheduler"
WORKFLOW = "browser-worker-plan.yml"
ARTIFACT = "ai-os-browser-worker-dispatch"
GEMINI = "https://gemini.google.com/app"
WORKER_DOC = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
BROWSER_DOC = "https://github.com/GK-studio-JP/browser-agent/blob/main/BROWSER_AGENT_INSTRUCTIONS.md"
ALLOWED = {"goto", "getPage", "fill", "click", "press", "typeText", "clickText", "scroll", "setViewport"}
MUTATING = {"fill", "click", "press", "typeText", "clickText"}


class LauncherError(RuntimeError):
    pass


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


def github(path: str, *, token: str | None = None) -> Any:
    return _json(
        "GET",
        f"https://api.github.com{path}",
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    )


def latest_scheduler_run(token: str | None) -> int:
    data = github(
        f"/repos/{SCHEDULER}/actions/workflows/{WORKFLOW}/runs?status=success&per_page=1",
        token=token,
    )
    runs = data.get("workflow_runs", [])
    if not runs:
        raise LauncherError("no successful Browser Worker scheduler run")
    return int(runs[0]["id"])


def plan_from_run(run_id: int, token: str | None) -> dict[str, Any]:
    data = github(f"/repos/{SCHEDULER}/actions/runs/{run_id}/artifacts?per_page=100", token=token)
    item = next((a for a in data.get("artifacts", []) if a.get("name") == ARTIFACT), None)
    if not item:
        raise LauncherError(f"{ARTIFACT} missing on scheduler run {run_id}")
    raw = _request(
        "GET",
        item["archive_download_url"],
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.endswith("browser-worker-plan.json")]
        if len(names) != 1:
            raise LauncherError("scheduler artifact must contain exactly one browser-worker-plan.json")
        return json.loads(archive.read(names[0]))


def plan_from_file(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise LauncherError(f"failed to read dispatch plan file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError("dispatch plan file must contain one JSON object")
    return value


def plan_from_url(url: str) -> dict[str, Any]:
    try:
        value = json.loads(_request("GET", url).decode())
    except Exception as exc:
        raise LauncherError(f"failed to read dispatch plan URL {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError("dispatch plan URL must contain one JSON object")
    return value


def resolve_plan(
    *,
    token: str | None,
    scheduler_run_id: int | None = None,
    plan_file: str | None = None,
    plan_url: str | None = None,
) -> dict[str, Any]:
    selected = sum(bool(v) for v in (scheduler_run_id, plan_file, plan_url))
    if selected > 1:
        raise LauncherError("choose only one of --scheduler-run-id, --plan-file, or --plan-url")
    if plan_file:
        return plan_from_file(plan_file)
    if plan_url:
        return plan_from_url(plan_url)
    run_id = scheduler_run_id or latest_scheduler_run(token)
    return plan_from_run(run_id, token)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    if plan.get("schema") != "ai-os-dispatch-plan:v1" or plan.get("authoritative") is not False:
        raise LauncherError("invalid dispatch plan boundary")
    if plan.get("filters", {}).get("process") != PROCESS:
        raise LauncherError("dispatch plan process mismatch")
    rows = plan.get("dispatches")
    if not isinstance(rows, list) or plan.get("dispatch_count") != len(rows) or len(rows) > 1:
        raise LauncherError("launcher requires a bounded 0-or-1 dispatch plan")
    if not rows:
        return None
    dispatch = rows[0]
    if (
        dispatch.get("schema") != "ai-os-dispatch:v1"
        or dispatch.get("authoritative") is not False
        or dispatch.get("process") != PROCESS
    ):
        raise LauncherError("invalid dispatch boundary")
    task = str(dispatch.get("task") or "")
    issue_url = str(dispatch.get("source", {}).get("issue_url") or "")
    if not re.fullmatch(r"#\d+", task):
        raise LauncherError("dispatch task pointer is invalid")
    expected = f"https://github.com/{BOARD}/issues/{task[1:]}"
    if issue_url.rstrip("/") != expected:
        raise LauncherError("dispatch issue URL does not match canonical board task pointer")
    return dispatch


def issue_number_from_dispatch(dispatch: dict[str, Any]) -> int:
    return int(str(dispatch["task"])[1:])


def _balanced_json_objects(text: str) -> list[str]:
    out = []
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                continue
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start : index + 1])
                    break
    return out


def _commands(text: str) -> list[dict[str, Any]]:
    out = []
    for raw in _balanced_json_objects(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("kind") in {"browser_action", "finish", "wait"}:
            out.append(obj)
    return out


def extract_model_command(page_text: str) -> dict[str, Any]:
    rows = _commands(page_text)
    if not rows:
        raise LauncherError("Gemini returned no launcher command")
    return rows[-1]


def _protocol_payload(body: str) -> dict[str, Any] | None:
    if "<!-- ai-bb:v1 -->" not in body:
        return None
    match = re.search(r"\`\`\`(?:json)?\s*(\{[\s\S]*?\})\s*\`\`\`", body)
    raw = match.group(1) if match else None
    if not raw:
        objects = _balanced_json_objects(body[body.find("{") :]) if "{" in body else []
        raw = objects[0] if objects else None
    try:
        obj = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def canonical_task_completed(comments: list[dict[str, Any]], *, task: str) -> bool:
    return any(
        (payload := _protocol_payload(str(comment.get("body") or "")))
        and payload.get("task") == task
        and payload.get("type") == "RESULT"
        for comment in comments
    )


def canonical_claim_present(comments: list[dict[str, Any]], *, task: str, agent_id: str) -> bool:
    return any(
        (payload := _protocol_payload(str(comment.get("body") or "")))
        and payload.get("task") == task
        and payload.get("agent_id") == agent_id
        and payload.get("type") == "CLAIM"
        for comment in comments
    )


def canonical_result_present(comments: list[dict[str, Any]], *, task: str, agent_id: str) -> bool:
    claimed = False
    for comment in comments:
        payload = _protocol_payload(str(comment.get("body") or ""))
        if not payload or payload.get("task") != task or payload.get("agent_id") != agent_id:
            continue
        if payload.get("type") == "CLAIM":
            claimed = True
        if payload.get("type") == "RESULT" and claimed:
            return True
    return False


def reduce_observation(
    page: dict[str, Any],
    *,
    max_text: int = 3000,
    max_elements: int = 40,
) -> dict[str, Any]:
    keys = (
        "id",
        "role",
        "text",
        "label",
        "value",
    )
    useful_roles = {"button", "textbox", "combobox", "checkbox", "radio"}
    useful = [
        element
        for element in (page.get("elements") or [])
        if element.get("editable") or element.get("role") in useful_roles
    ]
    elements = []
    for element in useful[:max_elements]:
        compact = {
            key: element.get(key)
            for key in keys
            if element.get(key) not in (None, "", [], {})
        }
        for key in ("text", "label", "value"):
            value = compact.get(key)
            if isinstance(value, str) and len(value) > 160:
                compact[key] = value[:160]
        elements.append(compact)
    return {
        "url": page.get("url"),
        "title": page.get("title"),
        "generation": page.get("generation"),
        "pageText": str(page.get("pageText") or "")[:max_text],
        "elements": elements,
        "dialogs": page.get("dialogs") or [],
    }


def _validate_model_action(
    command: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    action = str(command.get("action") or "")
    args = command.get("args")
    if action not in ALLOWED or not isinstance(args, dict):
        raise LauncherError(f"disallowed model action: {action!r}")
    if action in {"fill", "click"}:
        element_id = str(args.get("elementId") or "")
        generation = observation.get("generation")
        if generation is None or not element_id.startswith(f"g{generation}-"):
            raise LauncherError(f"stale elementId {element_id!r} for generation {generation!r}")
    return action, args


def _element_for_action(observation: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    element_id = str(args.get("elementId") or "")
    if not element_id:
        return None
    return next((e for e in observation.get("elements") or [] if e.get("id") == element_id), None)


def action_allowed_before_claim(
    command: dict[str, Any],
    observation: dict[str, Any],
    issue_url: str,
) -> bool:
    action = str(command.get("action") or "")
    args = command.get("args") if isinstance(command.get("args"), dict) else {}
    if action in {"goto", "getPage", "scroll", "setViewport"}:
        return True
    if str(observation.get("url") or "").rstrip("/") != issue_url.rstrip("/"):
        return False
    element = _element_for_action(observation, args)
    if not element:
        return False
    if action in {"fill", "typeText"}:
        label = str(element.get("label") or "")
        return (
            label == "Use Markdown to format your comment"
            or label == "Markdown value"
            or "format your comment" in label
        )
    if action == "click":
        return element.get("role") == "button" and str(element.get("text") or "") == "Comment"
    return False


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


def _find(
    page: dict[str, Any],
    *,
    label: str | None = None,
    text: str | None = None,
) -> dict[str, Any] | None:
    return next(
        (
            element
            for element in page.get("elements") or []
            if (label is not None and element.get("label") == label)
            or (text is not None and element.get("text") == text)
        ),
        None,
    )


def ask_gemini(relay: Relay, gemini_index: int, prompt_text: str) -> dict[str, Any]:
    relay.command("switchPage", {"index": gemini_index})
    relay.command("goto", {"url": GEMINI})
    page = relay.command("getPage", {})
    if dismiss := _find(page, text="Not now"):
        relay.command("click", {"elementId": dismiss["id"]})
        page = relay.command("getPage", {})
    box = _find(page, label="Enter a prompt for Gemini")
    if not box:
        raise LauncherError("Gemini prompt box unavailable")
    relay.command("fill", {"elementId": box["id"], "text": prompt_text})
    page = relay.command("getPage", {})
    baseline = len(_commands(str(page.get("pageText") or "")))
    send = _find(page, label="Send message")
    if not send:
        raise LauncherError("Gemini send button unavailable")
    relay.command("click", {"elementId": send["id"]})
    end = time.monotonic() + 90
    while time.monotonic() < end:
        time.sleep(1.5)
        page = relay.command("getPage", {})
        text = str(page.get("pageText") or "")
        if not _find(page, label="Stop response") and len(_commands(text)) > baseline:
            return _commands(text)[-1]
    raise LauncherError("Gemini response timed out")


def prompt(
    agent: str,
    task: str,
    issue: str,
    observation: dict[str, Any],
    step: int,
    feedback: str,
    claimed: bool,
) -> str:
    claim_state = "CLAIM VERIFIED FOR THIS RUN" if claimed else "CLAIM NOT YET VERIFIED"
    return f"""You are the reasoning engine for an AI OS Browser Chat Worker.
Run identity: {agent}
Routed task pointer: {task}
Canonical Issue: {issue}
Worker protocol: {WORKER_DOC}
Browser Agent instructions: {BROWSER_DOC}
Canonical claim state: {claim_state}
The task body is NOT injected here. Read it from the canonical Issue through Browser Agent observations.
Follow WORKER.md: canonical replay -> CLAIM -> replay ownership -> work -> replay -> RESULT.
Work only on {task}. Use only current-generation element IDs. Never put secrets/cookies/credentials in public Issues.
Before CLAIM is verified, the launcher only permits read-only navigation plus writing/submitting the CLAIM comment on the canonical Issue.
Return exactly ONE JSON object and no prose.
Allowed shapes:
{{"kind":"browser_action","action":"goto|getPage|fill|click|press|typeText|clickText|scroll|setViewport","args":{{...}},"reason":"..."}}
{{"kind":"finish","reason":"RESULT was appended and verified."}}
{{"kind":"wait","reason":"..."}}
Step: {step}
Previous launcher feedback: {feedback}
CURRENT WORK-TAB OBSERVATION:
{json.dumps(observation, ensure_ascii=False, separators=(",", ":"))}"""


def comments(token: str | None, issue: int) -> list[dict[str, Any]]:
    data = github(f"/repos/{BOARD}/issues/{issue}/comments?per_page=100", token=token)
    return data if isinstance(data, list) else []


def run_worker(
    dispatch: dict[str, Any],
    *,
    token: str | None,
    relay: Relay,
    max_steps: int,
) -> int:
    task = str(dispatch["task"])
    issue_url = str(dispatch["source"]["issue_url"])
    issue_no = issue_number_from_dispatch(dispatch)
    initial_comments = comments(token, issue_no)
    if canonical_task_completed(initial_comments, task=task):
        print(f"ALREADY_COMPLETED task={task}")
        return 0

    agent = f"browser-chat-gemini-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    relay.ready()
    relay.command("start", {})
    relay.command("goto", {"url": issue_url})
    relay.command("getPage", {})
    opened = relay.command("newPage", {"url": GEMINI})
    gemini_index = int(opened.get("pageIndex", 1)) if isinstance(opened, dict) else 1
    time.sleep(2)
    feedback = f"Browser Agent session {relay.session} is ready. Read the operating documents and canonical Issue."

    try:
        for step in range(1, max_steps + 1):
            current_comments = comments(token, issue_no)
            claimed = canonical_claim_present(current_comments, task=task, agent_id=agent)

            relay.command("switchPage", {"index": 0})
            page = relay.command("getPage", {})
            observation = reduce_observation(page)
            model_command = ask_gemini(
                relay,
                gemini_index,
                prompt(agent, task, issue_url, observation, step, feedback, claimed),
            )

            if model_command.get("kind") == "wait":
                print(f"WAIT {task}: {model_command.get('reason', '')}")
                return 2

            if model_command.get("kind") == "finish":
                if canonical_result_present(
                    comments(token, issue_no),
                    task=task,
                    agent_id=agent,
                ):
                    print(
                        f"RESULT_OK task={task} agent_id={agent} session_id={relay.session}"
                    )
                    return 0
                feedback = (
                    "finish rejected: canonical CLAIM followed by RESULT for this run is not present; continue."
                )
                continue

            if model_command.get("kind") != "browser_action":
                feedback = "invalid command kind; return one allowed command."
                continue

            try:
                action, args = _validate_model_action(model_command, observation)
            except LauncherError as exc:
                feedback = str(exc)
                continue

            if not claimed and action in MUTATING:
                if not action_allowed_before_claim(model_command, observation, issue_url):
                    feedback = (
                        "mutation rejected before canonical CLAIM verification. "
                        "Only the canonical Issue comment box and Comment button may mutate before CLAIM."
                    )
                    continue

            relay.command("switchPage", {"index": 0})
            relay.command(action, args)
            page = relay.command("getPage", {})
            feedback = (
                f"Executed {action}; fresh generation={page.get('generation')} "
                f"url={page.get('url')}."
            )

        raise LauncherError(f"max worker steps exceeded ({max_steps})")
    finally:
        try:
            relay.command("end", {})
        except Exception as exc:
            print(f"warning: browser end failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler-run-id", type=int)
    parser.add_argument("--plan-file")
    parser.add_argument("--plan-url")
    parser.add_argument("--session-id")
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--print-dispatch", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    plan = resolve_plan(
        token=token,
        scheduler_run_id=args.scheduler_run_id,
        plan_file=args.plan_file,
        plan_url=args.plan_url,
    )
    dispatch = validate_plan(plan)
    if not dispatch:
        print("NO_DISPATCH")
        return 0
    if args.print_dispatch:
        print(json.dumps(dispatch, ensure_ascii=False, indent=2))
        return 0

    if not args.session_id:
        raise LauncherError("--session-id is required for the production launcher")
    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    return run_worker(
        dispatch,
        token=token,
        relay=Relay(base, key, args.session_id),
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LauncherError as exc:
        print(f"LAUNCHER_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
