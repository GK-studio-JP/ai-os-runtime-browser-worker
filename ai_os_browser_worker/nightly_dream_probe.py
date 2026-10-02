from __future__ import annotations

from argparse import Namespace
from typing import Any

from ai_os_browser_worker.navigation_policy import LauncherError
from ai_os_browser_worker.relay import Relay

GEMINI_URL = "https://gemini.google.com/app"


def trigger_probe(args: Namespace) -> dict[str, Any]:
    base = __import__("os").environ.get("SUPABASE_URL")
    key = __import__("os").environ.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        raise LauncherError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")

    relay = Relay(base, key, args.session_id)
    relay.ready()
    relay.command("start", {})
    page = relay.command("goto", {"url": GEMINI_URL})
    current = str(page.get("url") or "") if isinstance(page, dict) else ""
    if not current.startswith("https://gemini.google.com/"):
        page = relay.command("getPage", {})
        current = str(page.get("url") or "") if isinstance(page, dict) else ""
    if not current.startswith("https://gemini.google.com/"):
        raise LauncherError(f"Gemini trigger probe did not reach Gemini; url={current!r}")

    return {
        "status": "trigger_ready",
        "probe_id": args.probe_id,
        "session_id": args.session_id,
        "gemini_url": current,
    }
