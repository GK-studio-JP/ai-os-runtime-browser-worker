import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_runtime_drift import (
    CANONICAL_REPOSITORY,
    DriftError,
    REQUIRED_PATHS,
    SCHEMA,
    git_blob_oid,
    load_manifest,
    verify_manifest,
)


PINNED_COMMIT = "d17f7024ebd2015a1595ed531406dd594d04741b"


class RuntimeDriftGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.local_root = root / "local"
        self.canonical_root = root / "canonical"
        self.local_root.mkdir()
        self.canonical_root.mkdir()

        self.canonical_bytes = {}
        self.local_bytes = {}
        for relative in REQUIRED_PATHS:
            canonical = f"canonical:{relative}\n".encode()
            local = canonical
            if relative == "driver_runner.py":
                local = b"worker-specific BrokenPipeError compatibility fork\n"
            self.canonical_bytes[relative] = canonical
            self.local_bytes[relative] = local
            (self.canonical_root / relative).write_bytes(canonical)
            (self.local_root / relative).write_bytes(local)

        self.manifest_path = self.local_root / "runtime-source.json"
        self._write_manifest(self._manifest())

    def tearDown(self):
        self.temp.cleanup()

    def _manifest(self):
        rows = []
        for relative in sorted(REQUIRED_PATHS):
            canonical_blob = git_blob_oid(self.canonical_bytes[relative])
            local_blob = git_blob_oid(self.local_bytes[relative])
            if relative == "driver_runner.py":
                rows.append(
                    {
                        "path": relative,
                        "mode": "forked",
                        "canonical_blob": canonical_blob,
                        "local_blob": local_blob,
                        "rationale": "Pinned Browser Worker compatibility fork.",
                    }
                )
            else:
                rows.append(
                    {
                        "path": relative,
                        "mode": "identical",
                        "canonical_blob": canonical_blob,
                        "local_blob": local_blob,
                    }
                )
        return {
            "schema": SCHEMA,
            "canonical_repository": CANONICAL_REPOSITORY,
            "canonical_commit": PINNED_COMMIT,
            "files": rows,
        }

    def _write_manifest(self, value):
        self.manifest_path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _verify(self):
        manifest = load_manifest(self.manifest_path)
        verify_manifest(
            manifest,
            canonical_root=self.canonical_root,
            local_root=self.local_root,
        )

    def test_valid_identical_and_forked_files_pass(self):
        self._verify()

    def test_local_identical_drift_fails(self):
        (self.local_root / "runtime.py").write_text(
            "local drift\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DriftError, "runtime.py: local drift"):
            self._verify()

    def test_canonical_drift_fails(self):
        (self.canonical_root / "runtime.py").write_text(
            "canonical drift\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DriftError, "runtime.py: canonical drift"):
            self._verify()

    def test_forked_local_drift_fails(self):
        (self.local_root / "driver_runner.py").write_text(
            "changed compatibility fork\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DriftError, "driver_runner.py: local drift"):
            self._verify()

    def test_manifest_cannot_drop_contract_file(self):
        value = self._manifest()
        value["files"] = [
            row for row in value["files"] if row["path"] != "runtime.py"
        ]
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "manifest file set mismatch"):
            load_manifest(self.manifest_path)

    def test_manifest_rejects_duplicate_file(self):
        value = self._manifest()
        value["files"].append(dict(value["files"][0]))
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "duplicate file entry"):
            load_manifest(self.manifest_path)

    def test_manifest_rejects_unsafe_path(self):
        value = self._manifest()
        value["files"][0]["path"] = "../runtime.py"
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "unsafe file path"):
            load_manifest(self.manifest_path)

    def test_forked_entry_requires_rationale(self):
        value = self._manifest()
        row = next(
            row for row in value["files"] if row["path"] == "driver_runner.py"
        )
        row["rationale"] = "   "
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "requires a rationale"):
            load_manifest(self.manifest_path)

    def test_manifest_requires_immutable_commit(self):
        value = self._manifest()
        value["canonical_commit"] = "main"
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "immutable 40-hex SHA"):
            load_manifest(self.manifest_path)

    def test_manifest_requires_canonical_repository(self):
        value = self._manifest()
        value["canonical_repository"] = "other/repository"
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "canonical_repository"):
            load_manifest(self.manifest_path)


    def test_forked_canonical_drift_fails(self):
        (self.canonical_root / "driver_runner.py").write_text(
            "changed canonical driver runner\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DriftError, "driver_runner.py: canonical drift"):
            self._verify()

    def test_manifest_rejects_unknown_mode(self):
        value = self._manifest()
        row = next(row for row in value["files"] if row["path"] == "runtime.py")
        row["mode"] = "copied"
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "unsupported mode"):
            load_manifest(self.manifest_path)

    def test_missing_canonical_file_fails(self):
        (self.canonical_root / "runtime.py").unlink()
        with self.assertRaisesRegex(DriftError, "runtime.py: canonical file is missing"):
            self._verify()

    def test_missing_local_file_fails(self):
        (self.local_root / "runtime.py").unlink()
        with self.assertRaisesRegex(DriftError, "runtime.py: local file is missing"):
            self._verify()

    def test_manifest_rejects_unknown_root_field(self):
        value = self._manifest()
        value["unexpected"] = True
        self._write_manifest(value)
        with self.assertRaisesRegex(DriftError, "manifest root must contain exactly"):
            load_manifest(self.manifest_path)

    def test_symlinked_contract_file_fails(self):
        local_path = self.local_root / "runtime.py"
        local_path.unlink()
        local_path.symlink_to(self.canonical_root / "runtime.py")
        with self.assertRaisesRegex(DriftError, "runtime.py: local file must not be a symlink"):
            self._verify()


if __name__ == "__main__":
    unittest.main()
