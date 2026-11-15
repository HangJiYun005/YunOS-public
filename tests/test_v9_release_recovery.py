from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.v9_release_recovery import (
    RecoveryError,
    audit_tree,
    build_package,
    verify_package,
)


class V9ReleaseRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="v9-recovery-test-")
        self.root = Path(self.temporary.name)
        self.databases: dict[str, Path] = {}
        for label in ("main", "profile", "semantic", "runtime"):
            path = self.root / f"{label}.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO records(value) VALUES(?)", (label,)
                )
                connection.commit()
            self.databases[label] = path
        self.assets = self.root / "assets-source"
        self.assets.mkdir()
        (self.assets / "one.bin").write_bytes(b"one")
        nested = self.assets / "nested"
        nested.mkdir()
        (nested / "two.bin").write_bytes(b"two")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_build_and_verify_preproduction_package(self) -> None:
        output = self.root / "package"
        manifest = build_package(
            phase="preproduction",
            output=output,
            project_root=self.root,
            database_paths=self.databases,
            asset_root=self.assets,
        )
        self.assertEqual(manifest["database_count"], 4)
        self.assertTrue(manifest["restore_rehearsal"]["passed"])
        self.assertEqual(manifest["assets"]["backup_audit"], audit_tree(self.assets))
        report = verify_package(output)
        self.assertEqual(report["status"], "verified")
        self.assertTrue(all(report["database_checks"].values()))

    def test_manifest_tamper_fails_closed(self) -> None:
        output = self.root / "package"
        build_package(
            phase="preproduction",
            output=output,
            project_root=self.root,
            database_paths=self.databases,
            asset_root=self.assets,
        )
        path = output / "release_backup_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["phase"] = "final"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            RecoveryError, "recovery_manifest_digest_mismatch"
        ):
            verify_package(output)

    def test_existing_output_is_rejected(self) -> None:
        output = self.root / "package"
        output.mkdir()
        with self.assertRaisesRegex(RecoveryError, "recovery_output_exists"):
            build_package(
                phase="preproduction",
                output=output,
                project_root=self.root,
                database_paths=self.databases,
                asset_root=self.assets,
            )


if __name__ == "__main__":
    unittest.main()
