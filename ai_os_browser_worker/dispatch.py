from __future__ import annotations

import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from ai_os_browser_worker.navigation_policy import LauncherError

PROCESS = "PROC-RUNTIME-BROWSER-WORKER"
BOARD = "GK-studio-JP/ai-bulletin-board"
SCHEDULER = "GK-studio-JP/ai-os-scheduler"
WORKFLOW = "browser-worker-plan.yml"
ARTIFACT = "ai-os-browser-worker-dispatch"


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
    data = github(
        f"/repos/{SCHEDULER}/actions/runs/{run_id}/artifacts?per_page=100",
        token=token,
    )
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
            raise LauncherError(
                "scheduler artifact must contain exactly one browser-worker-plan.json"
            )
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
        raise LauncherError(
            "choose only one of --scheduler-run-id, --plan-file, or --plan-url"
        )
    if plan_file:
        return plan_from_file(plan_file)
    if plan_url:
        return plan_from_url(plan_url)
    run_id = scheduler_run_id or latest_scheduler_run(token)
    return plan_from_run(run_id, token)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    if (
        plan.get("schema") != "ai-os-dispatch-plan:v1"
        or plan.get("authoritative") is not False
    ):
        raise LauncherError("invalid dispatch plan boundary")
    if plan.get("filters", {}).get("process") != PROCESS:
        raise LauncherError("dispatch plan process mismatch")
    rows = plan.get("dispatches")
    if (
        not isinstance(rows, list)
        or plan.get("dispatch_count") != len(rows)
        or len(rows) > 1
    ):
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
        raise LauncherError(
            "dispatch issue URL does not match canonical board task pointer"
        )
    return dispatch


def issue_number_from_dispatch(dispatch: dict[str, Any]) -> int:
    return int(str(dispatch["task"])[1:])
