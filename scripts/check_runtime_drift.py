from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "ai-os-runtime-source-lock:v1"
CANONICAL_REPOSITORY = "GK-studio-JP/ai-os-runtime"
REQUIRED_PATHS = frozenset(
    {
        "runtime.py",
        "driver_runner.py",
        "execution_budget.py",
        "runtime_middleware.py",
        "test_runtime.py",
        "test_execution_budget.py",
        "test_runtime_middleware.py",
    }
)
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class DriftError(RuntimeError):
    pass


def git_blob_oid(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise DriftError("file path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ".."}:
        raise DriftError(f"unsafe file path: {value!r}")
    return path.as_posix()


def _blob(value: Any, *, field: str, path: str) -> str:
    if not isinstance(value, str) or HEX40.fullmatch(value) is None:
        raise DriftError(f"{path}: {field} must be a lowercase 40-hex Git blob id")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError(f"cannot read manifest {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise DriftError("manifest root must be an object")

    expected_root_keys = {
        "schema",
        "canonical_repository",
        "canonical_commit",
        "files",
    }
    if set(value) != expected_root_keys:
        raise DriftError(
            "manifest root must contain exactly "
            f"{sorted(expected_root_keys)}"
        )

    if value.get("schema") != SCHEMA:
        raise DriftError(f"unsupported manifest schema: {value.get('schema')!r}")
    if value.get("canonical_repository") != CANONICAL_REPOSITORY:
        raise DriftError(
            f"canonical_repository must be {CANONICAL_REPOSITORY!r}"
        )

    commit = value.get("canonical_commit")
    if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
        raise DriftError("canonical_commit must be a lowercase immutable 40-hex SHA")

    rows = value.get("files")
    if not isinstance(rows, list):
        raise DriftError("files must be a list")

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DriftError("each file entry must be an object")

        file_path = _safe_path(row.get("path"))
        if file_path in seen:
            raise DriftError(f"duplicate file entry: {file_path}")
        seen.add(file_path)

        mode = row.get("mode")
        canonical_blob = _blob(
            row.get("canonical_blob"),
            field="canonical_blob",
            path=file_path,
        )
        local_blob = _blob(
            row.get("local_blob"),
            field="local_blob",
            path=file_path,
        )

        if mode == "identical":
            expected_keys = {"path", "mode", "canonical_blob", "local_blob"}
            if set(row) != expected_keys:
                raise DriftError(
                    f"{file_path}: identical entries must contain exactly "
                    f"{sorted(expected_keys)}"
                )
            if canonical_blob != local_blob:
                raise DriftError(
                    f"{file_path}: identical entry must pin the same canonical and local blob"
                )
        elif mode == "forked":
            expected_keys = {
                "path",
                "mode",
                "canonical_blob",
                "local_blob",
                "rationale",
            }
            if set(row) != expected_keys:
                raise DriftError(
                    f"{file_path}: forked entries must contain exactly "
                    f"{sorted(expected_keys)}"
                )
            rationale = row.get("rationale")
            if not isinstance(rationale, str) or not rationale.strip():
                raise DriftError(f"{file_path}: forked entry requires a rationale")
            if canonical_blob == local_blob:
                raise DriftError(
                    f"{file_path}: forked entry must differ; use identical mode instead"
                )
        else:
            raise DriftError(f"{file_path}: unsupported mode {mode!r}")

    missing = sorted(REQUIRED_PATHS - seen)
    extra = sorted(seen - REQUIRED_PATHS)
    if missing or extra:
        raise DriftError(
            f"manifest file set mismatch: missing={missing!r} extra={extra!r}"
        )

    return value


def _read_file(root: Path, relative: str, *, side: str) -> bytes:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        if path.is_symlink():
            raise DriftError(f"{relative}: {side} file must not be a symlink")
        if not path.is_file():
            raise DriftError(f"{relative}: {side} file is missing")
        return path.read_bytes()
    except OSError as exc:
        raise DriftError(f"{relative}: cannot read {side} file: {exc}") from exc


def verify_manifest(
    manifest: dict[str, Any],
    *,
    canonical_root: Path,
    local_root: Path,
) -> None:
    for row in manifest["files"]:
        relative = row["path"]
        canonical_data = _read_file(canonical_root, relative, side="canonical")
        local_data = _read_file(local_root, relative, side="local")

        canonical_oid = git_blob_oid(canonical_data)
        local_oid = git_blob_oid(local_data)

        if canonical_oid != row["canonical_blob"]:
            raise DriftError(
                f"{relative}: canonical drift: expected {row['canonical_blob']} "
                f"got {canonical_oid}"
            )
        if local_oid != row["local_blob"]:
            raise DriftError(
                f"{relative}: local drift: expected {row['local_blob']} got {local_oid}"
            )

        if row["mode"] == "identical" and canonical_data != local_data:
            raise DriftError(f"{relative}: identical file content differs")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Fail closed when Browser Worker Runtime copies drift from a pinned Runtime commit."
    )
    root.add_argument("--manifest", default="runtime-source.json")
    root.add_argument("--canonical-root", required=True)
    root.add_argument("--local-root", default=".")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        manifest = load_manifest(Path(args.manifest))
        verify_manifest(
            manifest,
            canonical_root=Path(args.canonical_root),
            local_root=Path(args.local_root),
        )
    except DriftError as exc:
        print(f"RUNTIME_DRIFT_GUARD_ERROR: {exc}")
        return 1

    print(
        "RUNTIME_DRIFT_GUARD_OK",
        manifest["canonical_repository"],
        manifest["canonical_commit"],
        len(manifest["files"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
