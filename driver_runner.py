from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from execution_budget import ExecutionBudgetMiddleware, effective_timeout_seconds
from runtime_middleware import MiddlewareBlocked, MiddlewareChain

INVOCATION_SCHEMA = "ai-os-worker-invocation:v1"
RESULT_SCHEMA = "ai-os-worker-result:v1"
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


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
    if not invocation.get("worker_id"):
        raise ValueError("worker invocation worker_id is required")


def _run_bounded_subprocess(
    argv: list[str],
    invocation: dict[str, Any],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
) -> tuple[int, bytes]:
    payload = json.dumps(invocation, ensure_ascii=False).encode("utf-8")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("driver subprocess pipes are unavailable")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout_seconds

    try:
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except BrokenPipeError:
            # The child may exit before consuming stdin; return-code handling below
            # owns that failure without exposing child stderr.
            pass

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(argv, timeout_seconds)

            events = selector.select(timeout=remaining)
            if not events:
                continue

            chunk = os.read(
                process.stdout.fileno(),
                min(65536, max_output_bytes - total + 1),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > max_output_bytes:
                process.kill()
                process.wait()
                raise ValueError("driver stdout exceeds max_output_bytes")
            chunks.append(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0 and process.poll() is None:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        try:
            returncode = process.wait(timeout=max(remaining, 0.001))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()

    return returncode, b"".join(chunks)


def run_driver(
    invocation: dict[str, Any],
    command: Sequence[str],
    *,
    timeout_seconds: int = 300,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    middleware: MiddlewareChain | None = None,
) -> dict[str, Any]:
    validate_invocation(invocation)
    argv = [str(part) for part in command]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("driver command is required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or max_output_bytes < 1
    ):
        raise ValueError("max_output_bytes must be positive")

    custom_middlewares = middleware.middlewares if middleware else ()
    chain = MiddlewareChain(
        (ExecutionBudgetMiddleware(), *custom_middlewares)
    )
    chain.before_compute(invocation)
    effective_timeout, budget_deadline = effective_timeout_seconds(
        invocation,
        timeout_seconds,
    )

    try:
        returncode, stdout_bytes = _run_bounded_subprocess(
            argv,
            invocation,
            timeout_seconds=effective_timeout,
            max_output_bytes=max_output_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        if budget_deadline:
            raise MiddlewareBlocked(
                "execution-budget",
                "compute",
                "deadline_seconds exceeded",
            ) from exc
        raise

    if returncode != 0:
        raise RuntimeError(f"driver exited with status {returncode}")

    try:
        stdout = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("driver stdout must be UTF-8 JSON") from exc

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("driver stdout must be one JSON object") from exc
    if not isinstance(result, dict):
        raise ValueError("driver stdout must decode to one JSON object")
    if result.get("schema") != RESULT_SCHEMA:
        raise ValueError(f"unsupported Worker result schema: {result.get('schema')!r}")
    if result.get("invocation_fingerprint") != invocation.get("fingerprint"):
        raise ValueError("Worker result does not match invocation fingerprint")
    if result.get("worker_id") != invocation.get("worker_id"):
        raise ValueError("Worker result identity does not match invocation")

    chain.after_compute(invocation, result)
    return result

def command_run(args: argparse.Namespace) -> None:
    invocation = _read(args.invocation)
    result = run_driver(
        invocation,
        args.driver_command,
        timeout_seconds=args.timeout_seconds,
        max_output_bytes=args.max_output_bytes,
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
    p.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
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
