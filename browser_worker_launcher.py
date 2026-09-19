from __future__ import annotations

import argparse, io, json, os, re, sys, time, urllib.request, uuid, zipfile
from typing import Any

PROCESS = "PROC-RUNTIME-BROWSER-WORKER"
BOARD = "GK-studio-JP/ai-bulletin-board"
SCHEDULER = "GK-studio-JP/ai-os-scheduler"
WORKFLOW = "browser-worker-plan.yml"
ARTIFACT = "ai-os-browser-worker-dispatch"
GEMINI = "https://gemini.google.com/app"
WORKER_DOC = "https://github.com/GK-studio-JP/ai-os-runtime-browser-worker/blob/main/WORKER.md"
BROWSER_DOC = "https://github.com/kj2whvbzjn-hue/browser-agent/blob/main/BROWSER_AGENT_INSTRUCTIONS.md"
ALLOWED = {"goto", "getPage", "fill", "click", "press", "typeText", "clickText", "scroll", "setViewport"}


class LauncherError(RuntimeError):
    pass


def _request(method: str, url: str, *, token: str | None = None, value: Any = None, headers: dict[str, str] | None = None) -> bytes:
    h = {"User-Agent": "ai-os-browser-worker-launcher", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = None
    if value is not None:
        body = json.dumps(value).encode()
        h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method=method), timeout=60) as r:
            return r.read()
    except Exception as e:
        raise LauncherError(f"HTTP failure for {method} {url}: {e}") from e


def _json(method: str, url: str, **kwargs: Any) -> Any:
    raw = _request(method, url, **kwargs)
    return json.loads(raw.decode()) if raw else None


def github(path: str, *, token: str | None = None) -> Any:
    return _json("GET", f"https://api.github.com{path}", token=token, headers={"Accept": "application/vnd.github+json"})


def latest_scheduler_run(token: str | None) -> int:
    data = github(f"/repos/{SCHEDULER}/actions/workflows/{WORKFLOW}/runs?status=success&per_page=1", token=token)
    runs = data.get("workflow_runs", [])
    if not runs:
        raise LauncherError("no successful Browser Worker scheduler run")
    return int(runs[0]["id"])


def plan_from_run(run_id: int, token: str | None) -> dict[str, Any]:
    data = github(f"/repos/{SCHEDULER}/actions/runs/{run_id}/artifacts?per_page=100", token=token)
    item = next((a for a in data.get("artifacts", []) if a.get("name") == ARTIFACT), None)
    if not item:
        raise LauncherError(f"{ARTIFACT} missing on scheduler run {run_id}")
    raw = _request("GET", item["archive_download_url"], token=token, headers={"Accept": "application/vnd.github+json"})
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.endswith("browser-worker-plan.json")]
        if len(names) != 1:
            raise LauncherError("scheduler artifact must contain exactly one browser-worker-plan.json")
        return json.loads(z.read(names[0]))


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
    d = rows[0]
    if d.get("schema") != "ai-os-dispatch:v1" or d.get("authoritative") is not False or d.get("process") != PROCESS:
        raise LauncherError("invalid dispatch boundary")
    if not re.fullmatch(r"#\d+", str(d.get("task") or "")) or not d.get("source", {}).get("issue_url"):
        raise LauncherError("dispatch task pointer is invalid")
    return d


def issue_number_from_dispatch(dispatch: dict[str, Any]) -> int:
    return int(str(dispatch["task"])[1:])


def _balanced_json_objects(text: str) -> list[str]:
    out = []
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0; quoted = False; escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if quoted:
                if escaped: escaped = False
                elif ch == "\\": escaped = True
                elif ch == '"': quoted = False
                continue
            if ch == '"': quoted = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start:i+1]); break
    return out


def _commands(text: str) -> list[dict[str, Any]]:
    out = []
    for raw in _balanced_json_objects(text):
        try: obj = json.loads(raw)
        except json.JSONDecodeError: continue
        if isinstance(obj, dict) and obj.get("kind") in {"browser_action", "finish", "wait"}: out.append(obj)
    return out


def extract_model_command(page_text: str) -> dict[str, Any]:
    rows = _commands(page_text)
    if not rows: raise LauncherError("Gemini returned no launcher command")
    return rows[-1]


def _protocol_payload(body: str) -> dict[str, Any] | None:
    if "<!-- ai-bb:v1 -->" not in body: return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", body)
    raw = m.group(1) if m else None
    if not raw:
        objs = _balanced_json_objects(body[body.find("{"):]) if "{" in body else []
        raw = objs[0] if objs else None
    try: obj = json.loads(raw) if raw else None
    except json.JSONDecodeError: return None
    return obj if isinstance(obj, dict) else None


def canonical_task_completed(comments: list[dict[str, Any]], *, task: str) -> bool:
    return any((p := _protocol_payload(str(c.get("body") or ""))) and p.get("task") == task and p.get("type") == "RESULT" for c in comments)


def canonical_result_present(comments: list[dict[str, Any]], *, task: str, agent_id: str) -> bool:
    claimed = False
    for c in comments:
        p = _protocol_payload(str(c.get("body") or ""))
        if not p or p.get("task") != task or p.get("agent_id") != agent_id: continue
        if p.get("type") == "CLAIM": claimed = True
        if p.get("type") == "RESULT" and claimed: return True
    return False


def reduce_observation(page: dict[str, Any], *, max_text: int = 12000, max_elements: int = 80) -> dict[str, Any]:
    keys = ("id", "role", "text", "label", "accessibleName", "value", "disabled", "editable", "context", "states", "inViewport", "occluded")
    elems = [{k: e.get(k) for k in keys if e.get(k) not in (None, "", [], {})} for e in (page.get("elements") or [])[:max_elements]]
    return {"url": page.get("url"), "title": page.get("title"), "generation": page.get("generation"), "pageText": str(page.get("pageText") or "")[:max_text], "elements": elems, "dialogs": page.get("dialogs") or []}


def _validate_model_action(command: dict[str, Any], observation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action, args = str(command.get("action") or ""), command.get("args")
    if action not in ALLOWED or not isinstance(args, dict): raise LauncherError(f"disallowed model action: {action!r}")
    if action in {"fill", "click"}:
        eid, gen = str(args.get("elementId") or ""), observation.get("generation")
        if gen is None or not eid.startswith(f"g{gen}-"): raise LauncherError(f"stale elementId {eid!r} for generation {gen!r}")
    return action, args


class Relay:
    def __init__(self, base: str, key: str, session: str):
        self.base, self.key, self.session = base.rstrip("/"), key, session
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def rest(self, method: str, path: str, value: Any = None, prefer: str | None = None) -> Any:
        h = dict(self.headers)
        if prefer: h["Prefer"] = prefer
        return _json(method, f"{self.base}/rest/v1/{path}", value=value, headers=h)

    def ready(self, timeout: int = 180) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest("GET", f"browser_relay_sessions?session_id=eq.{self.session}&select=state,ended_at,last_error") or []
            if rows and rows[0].get("state") == "ready" and not rows[0].get("ended_at"): return
            time.sleep(1)
        raise LauncherError(f"Browser Agent session {self.session} did not become ready")

    def command(self, action: str, args: dict[str, Any] | None = None, timeout: int = 75) -> Any:
        cid = f"launcher-{uuid.uuid4().hex}"
        self.rest("POST", "browser_relay_commands", {"session_id": self.session, "command_id": cid, "action": action, "args": args or {}}, "return=representation")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.rest("GET", f"browser_relay_commands?session_id=eq.{self.session}&command_id=eq.{cid}&select=status,result,error&limit=1") or []
            row = rows[0] if rows else {}
            if row.get("status") == "done": return row.get("result")
            if row.get("status") == "error": raise LauncherError(f"Browser Agent {action} failed: {row.get('error')}")
            time.sleep(.5)
        raise LauncherError(f"Browser Agent {action} timed out")


def _find(page: dict[str, Any], *, label: str | None = None, text: str | None = None) -> dict[str, Any] | None:
    return next((e for e in page.get("elements") or [] if (label is not None and e.get("label") == label) or (text is not None and e.get("text") == text)), None)


def ask_gemini(relay: Relay, gemini_index: int, prompt: str) -> dict[str, Any]:
    relay.command("switchPage", {"index": gemini_index}); page = relay.command("getPage", {})
    if dismiss := _find(page, text="Not now"):
        relay.command("click", {"elementId": dismiss["id"]}); page = relay.command("getPage", {})
    box = _find(page, label="Enter a prompt for Gemini")
    if not box: raise LauncherError("Gemini prompt box unavailable")
    relay.command("fill", {"elementId": box["id"], "text": prompt}); page = relay.command("getPage", {})
    baseline = len(_commands(str(page.get("pageText") or "")))
    send = _find(page, label="Send message")
    if not send: raise LauncherError("Gemini send button unavailable")
    relay.command("click", {"elementId": send["id"]})
    end = time.monotonic() + 75
    while time.monotonic() < end:
        time.sleep(1.5); page = relay.command("getPage", {}); text = str(page.get("pageText") or "")
        if not _find(page, label="Stop response") and len(_commands(text)) > baseline: return _commands(text)[-1]
    raise LauncherError("Gemini response timed out")


def prompt(agent: str, task: str, issue: str, obs: dict[str, Any], step: int, feedback: str) -> str:
    return f'''You are the reasoning engine for an AI OS Browser Chat Worker.
Run identity: {agent}
Routed task pointer: {task}
Canonical Issue: {issue}
Worker protocol: {WORKER_DOC}
Browser Agent instructions: {BROWSER_DOC}
The task body is NOT injected here. Read it from the canonical Issue through Browser Agent observations.
Follow WORKER.md: canonical replay -> CLAIM -> replay ownership -> work -> replay -> RESULT. Work only on {task}. Use only current-generation element IDs. Never put secrets/cookies/credentials in public Issues. Return exactly ONE JSON object and no prose.
Allowed shapes:
{{"kind":"browser_action","action":"goto|getPage|fill|click|press|typeText|clickText|scroll|setViewport","args":{{...}},"reason":"..."}}
{{"kind":"finish","reason":"RESULT was appended and verified."}}
{{"kind":"wait","reason":"..."}}
Step: {step}
Previous launcher feedback: {feedback}
CURRENT WORK-TAB OBSERVATION:
{json.dumps(obs, ensure_ascii=False, separators=(",", ":"))}'''


def comments(token: str | None, issue: int) -> list[dict[str, Any]]:
    data = github(f"/repos/{BOARD}/issues/{issue}/comments?per_page=100", token=token)
    return data if isinstance(data, list) else []


def run_worker(dispatch: dict[str, Any], *, token: str | None, relay: Relay, max_steps: int) -> int:
    task, issue_url = str(dispatch["task"]), dispatch["source"]["issue_url"]
    issue_no = issue_number_from_dispatch(dispatch)
    if canonical_task_completed(comments(token, issue_no), task=task):
        print(f"ALREADY_COMPLETED task={task}"); return 0
    agent = f"browser-chat-gemini-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    relay.ready(); relay.command("start", {}); relay.command("goto", {"url": issue_url}); relay.command("getPage", {})
    opened = relay.command("newPage", {"url": GEMINI}); gemini_index = int(opened.get("pageIndex", 1)) if isinstance(opened, dict) else 1
    time.sleep(2); feedback = f"Browser Agent session {relay.session} is ready. Read the operating documents and canonical Issue."
    try:
        for step in range(1, max_steps + 1):
            relay.command("switchPage", {"index": 0}); page = relay.command("getPage", {}); obs = reduce_observation(page)
            cmd = ask_gemini(relay, gemini_index, prompt(agent, task, issue_url, obs, step, feedback))
            if cmd.get("kind") == "wait": print(f"WAIT {task}: {cmd.get('reason','')}"); return 2
            if cmd.get("kind") == "finish":
                if canonical_result_present(comments(token, issue_no), task=task, agent_id=agent):
                    print(f"RESULT_OK task={task} agent_id={agent} session_id={relay.session}"); return 0
                feedback = "finish rejected: canonical CLAIM followed by RESULT for this run is not present; continue."
                continue
            if cmd.get("kind") != "browser_action": feedback = "invalid command kind; return one allowed command."; continue
            try: action, args = _validate_model_action(cmd, obs)
            except LauncherError as e: feedback = str(e); continue
            relay.command("switchPage", {"index": 0}); relay.command(action, args); page = relay.command("getPage", {})
            feedback = f"Executed {action}; fresh generation={page.get('generation')} url={page.get('url')}."
        raise LauncherError(f"max worker steps exceeded ({max_steps})")
    finally:
        try: relay.command("end", {})
        except Exception as e: print(f"warning: browser end failed: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("--scheduler-run-id", type=int); p.add_argument("--session-id"); p.add_argument("--max-steps", type=int, default=80); p.add_argument("--print-dispatch", action="store_true")
    a = p.parse_args(argv); token = os.environ.get("GITHUB_TOKEN"); run_id = a.scheduler_run_id or latest_scheduler_run(token); plan = plan_from_run(run_id, token); dispatch = validate_plan(plan)
    if not dispatch: print("NO_DISPATCH"); return 0
    if a.print_dispatch: print(json.dumps(dispatch, ensure_ascii=False, indent=2)); return 0
    if not a.session_id: raise LauncherError("--session-id is required for the production launcher")
    base, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
    if not base or not key: raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    return run_worker(dispatch, token=token, relay=Relay(base, key, a.session_id), max_steps=a.max_steps)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except LauncherError as e: print(f"LAUNCHER_ERROR: {e}", file=sys.stderr); raise SystemExit(1)
