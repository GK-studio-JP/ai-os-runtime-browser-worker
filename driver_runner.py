from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence

INVOCATION_SCHEMA = "ai-os-worker-invocation:v1"
RESULT_SCHEMA = "ai-os-worker-result:v1"


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str | None, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def validate_invocation(invocation: dict[str, Any]) -> None:
    if invocation.get("schema") != INVOCATION_SCHEMA:
        raise ValueError(f"unsupported invocation schema: {invocation.get('schema')!r}")
    if invocation.get("authoritative") is not False:
        raise ValueError("worker invocation must be explicitly non-authoritative")
    if invocation.get("persist_required") is not True:
        raise ValueError("worker invocation must require persistence")
    if not invocation.get("fingerprint"):
        raise ValueError("worker invocation fingerprint is required")
    if not isinstance(invocation.get("worker_actor"), str) or not invocation["worker_actor"].strip():
        raise ValueError("worker invocation worker_actor is required")
    if not invocation.get("worker_id"):
        raise ValueError("worker invocation worker_id is required")


def run_driver(
    invocation: dict[str, Any],
    command: Sequence[str],
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    validate_invocation(invocation)
    argv = [str(part) for part in command]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("driver command is required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    completed = subprocess.run(
        argv,
        input=json.dumps(invocation, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"driver exited with status {completed.returncode}")

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("driver stdout must be one JSON object") from exc
    if not isinstance(result, dict):
        raise ValueError("driver stdout must decode to one JSON object")
    if result.get("schema") != RESULT_SCHEMA:
        raise ValueError(f"unsupported Worker result schema: {result.get('schema')!r}")
    if result.get("invocation_fingerprint") != invocation.get("fingerprint"):
        raise ValueError("Worker result does not match invocation fingerprint")
    if result.get("worker_actor") != invocation.get("worker_actor"):
        raise ValueError("Worker result actor identity does not match invocation")
    if result.get("worker_id") != invocation.get("worker_id"):
        raise ValueError("Worker result identity does not match invocation")
    return result


def command_run(args: argparse.Namespace) -> None:
    invocation = _read(args.invocation)
    result = run_driver(
        invocation,
        args.driver_command,
        timeout_seconds=args.timeout_seconds,
    )
    _write(args.output, result)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aios-driver-runner")
    sub = root.add_subparsers(dest="command_name", required=True)

    p = sub.add_parser(
        "run",
        help="deliver one Worker invocation to a replaceable subprocess driver",
    )
    p.add_argument("--invocation", required=True)
    p.add_argument("--output")
    p.add_argument("--timeout-seconds", type=int, default=300)
    p.add_argument(
        "driver_command",
        nargs=argparse.REMAINDER,
        help="driver argv after --; invocation JSON is provided on stdin",
    )
    p.set_defaults(func=command_run)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
