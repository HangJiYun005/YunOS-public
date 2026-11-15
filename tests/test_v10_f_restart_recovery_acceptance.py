from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from memory.operational_state_models import canonical_digest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL = PROJECT_ROOT / "tools" / "v10_f_restart_recovery_acceptance.py"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class V10FRestartRecoveryAcceptanceTest(unittest.TestCase):
    """True subprocess restart coverage over a disposable Operational DB."""

    def test_new_process_recovers_checkpoint_once_and_rejects_expired_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            output_root = Path(temporary) / "restart-recovery"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(TOOL),
                    "execute",
                    "--output-root",
                    str(output_root),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=150,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(
                "YUNOS_V10_F_RESTART_RECOVERY_GO",
                summary["conclusion"],
            )
            report_path = Path(summary["report_path"])
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "v10-f-cross-process-restart-recovery-2",
                report["acceptance_version"],
            )
            report_basis = dict(report)
            report_digest = report_basis.pop("report_digest")
            self.assertEqual(report_digest, canonical_digest(report_basis))
            self.assertTrue(report["fresh_python_process"])
            self.assertEqual(73, report["old_process_exit_code"])
            self.assertEqual(0, report["real_provider_calls"])
            self.assertEqual(0, report["production_database_writes"])
            self.assertTrue(all(report["checks"].values()))

            detail_path = report_path.parent / "new-process-recovery.json"
            self.assertEqual(report["detail_file_sha256"], sha256_file(detail_path))
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            detail_basis = dict(detail)
            detail_digest = detail_basis.pop("result_digest")
            self.assertEqual(detail_digest, canonical_digest(detail_basis))
            self.assertEqual(report["detail_digest"], detail_digest)
            self.assertEqual(
                str(
                    (
                        PROJECT_ROOT
                        / "data"
                        / "operational"
                        / "ayunos_operational_state.db"
                    ).resolve()
                ),
                detail["database_boundary"]["production_database"],
            )
            self.assertFalse(detail["database_boundary"]["production_database_used"])
            database = report_path.parent / report["disposable_database"]["relative_path"]
            self.assertTrue(database.is_file())
            self.assertEqual(
                report["disposable_database"]["sha256"], sha256_file(database)
            )
            self.assertEqual(
                report["disposable_database"]["size"], database.stat().st_size
            )
            boundary = report["production_boundary"]
            self.assertEqual(boundary["files_before"], boundary["files_after"])
            self.assertTrue(
                all(delta == 0 for delta in boundary["runtime_count_delta"].values())
            )
            self.assertEqual(sha256_file(TOOL), report["source_identity"]["tool_sha256"])
            self.assertEqual(
                sha256_file(Path(__file__).resolve()),
                report["source_identity"]["test_sha256"],
            )
            manifest_path = (
                report_path.parent
                / report["supporting_evidence"]["manifest_relative_path"]
            )
            self.assertEqual(
                report["supporting_evidence"]["manifest_sha256"],
                sha256_file(manifest_path),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_basis = dict(manifest)
            manifest_digest = manifest_basis.pop("manifest_digest")
            self.assertEqual(manifest_digest, canonical_digest(manifest_basis))
            self.assertEqual(
                report["supporting_evidence"]["manifest_digest"], manifest_digest
            )
            self.assertEqual(
                report["supporting_evidence"]["file_count"], len(manifest["files"])
            )
            for item in manifest["files"]:
                evidence = report_path.parent / item["name"]
                self.assertTrue(evidence.is_file())
                self.assertEqual(item["size"], evidence.stat().st_size)
                self.assertEqual(item["sha256"], sha256_file(evidence))
            interrupted = detail["interrupted_run"]
            self.assertEqual("verifying", interrupted["status_before"])
            self.assertEqual("waiting_acceptance", interrupted["status_after"])
            self.assertEqual(1, interrupted["tool_executions_before"])
            self.assertEqual(1, interrupted["tool_executions_after"])
            self.assertEqual(0, interrupted["verifications_before"])
            self.assertEqual(1, interrupted["verifications_after"])
            self.assertEqual(0, interrupted["recovery_model_sends"])
            self.assertEqual(0, interrupted["recovery_gateway_execute_calls"])
            self.assertEqual(0, interrupted["replay_gateway_execute_calls"])

            expired = detail["expired_authorization_run"]
            self.assertEqual(
                "authorization_expired_or_not_yet_valid",
                expired["reason_code"],
            )
            self.assertEqual(expired["status_before"], expired["status_after"])
            self.assertTrue(expired["run_digest_unchanged"])
            self.assertTrue(expired["checkpoint_digest_unchanged"])
            self.assertEqual(0, expired["model_sends"])
            self.assertEqual(0, expired["tool_executions_after"])

    def test_internal_child_mode_rejects_unowned_nonempty_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="yunos-v10f-restart-recovery-",
            ignore_cleanup_errors=True,
        ) as temporary:
            root = Path(temporary)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("must remain unchanged\n", encoding="utf-8")
            before = sha256_file(sentinel)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(TOOL),
                    "_seed",
                    "--scenario-root",
                    str(root),
                    "--owner-token",
                    "not-an-owner-token",
                    "--owner-parent-pid",
                    str(os.getpid()),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual(before, sha256_file(sentinel))
            self.assertEqual([sentinel.name], sorted(path.name for path in root.iterdir()))

    def test_acceptance_source_has_no_production_or_real_provider_entry(self) -> None:
        source = TOOL.read_text(encoding="utf-8")
        self.assertNotIn("YUNOS_OPERATIONAL_STATE_PRODUCTION_WRITE", source)
        self.assertNotIn("create_production_agent_runtime_source", source)
        self.assertNotIn("ProductionAgentRuntimeTurnSource", source)
        self.assertNotIn("shell.execute\": \"available", source)
        self.assertNotIn("network.request\": \"available", source)
        self.assertIn('"project.file.read": "available"', source)


if __name__ == "__main__":
    unittest.main()
