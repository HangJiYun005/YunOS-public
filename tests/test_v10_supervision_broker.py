from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory.cognitive_local_broker as local_broker_module
import memory.cognitive_command_policy as command_policy
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
)
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    _tool_proposal_from_verified_record,
)
from memory.cognitive_local_broker import (
    CognitiveLocalBroker,
    CognitiveLocalBrokerError,
)
from memory.cognitive_path_policy import (
    CognitivePathPolicyError,
    resolve_project_path,
)
from memory.cognitive_supervision import ProcessSupervisor
from memory.operational_state_models import canonical_digest


T1 = "2026-08-01T00:00:00.000000Z"


def sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class SupervisionAndLocalBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.artifacts = root / "artifacts"
        self.project.mkdir()
        self.artifacts.mkdir()
        self.broker = CognitiveLocalBroker(
            project_root=self.project,
            artifact_root=self.artifacts,
        )

    @staticmethod
    def _assert_process_gone(pid: int) -> None:
        if os.name != "nt":
            with unittest.TestCase().assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            return
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            wait_result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            # WAIT_OBJECT_0 proves that this process object is signalled/exited.
            unittest.TestCase().assertEqual(0, wait_result)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def profile(self, capability_id: str):
        return next(
            item
            for item in bounded_execution_capability_profiles(
                {capability_id: "available"}
            )
            if item.capability_id == capability_id
        )

    def proposal(self, capability_id: str, arguments: dict):
        profile = self.profile(capability_id)
        typed_arguments = profile.validate_input(arguments)
        return _tool_proposal_from_verified_record(
            {
                "protocol_version": TOOL_PROTOCOL_VERSION,
                "proposal_id": f"toolproposal:{capability_id}",
                "run_id": "run:supervision-broker",
                "objective_id": "objective:supervision-broker",
                "objective_digest": sha("objective"),
                "plan_id": "plan:supervision-broker",
                "plan_version": 1,
                "plan_digest": sha("plan"),
                "plan_node_id": f"node:{capability_id}",
                "capability_id": capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": typed_arguments,
                "argument_digest": canonical_digest(typed_arguments),
                "idempotency_key": f"idempotency:{capability_id}",
                "reason": "Exercise one bounded local operation",
                "expected_observation": "A deterministic local observation",
                "risk_class": profile.risk_class,
                "authorization_id": "authorization:supervision-broker",
                "authorization_digest": sha("authorization"),
                "status": "proposed",
                "proposed_at": T1,
                "runtime_execution_reference": None,
                "runtime_receipt_digest": None,
                "proposal_digest": "",
            }
        )

    def resolved_test_only_command(self, test_id: str):
        """Build a sealed supervisor fixture without registering Python."""

        profile = self.profile("process.template.run")
        executable = command_policy._executable_identity(sys.executable)
        return command_policy.ResolvedCommand(
            template_id="git.diff.check.v1",
            capability_id=profile.capability_id,
            capability_version=profile.version,
            capability_digest=profile.capability_digest,
            executable_identity=executable,
            argv=(
                executable["absolute_path"],
                "-B",
                "-m",
                "unittest",
                test_id,
                "-q",
            ),
            working_directory=str(self.project),
            environment=command_policy._minimal_environment(),
            stdin_mode="disabled",
            shell=False,
            template_digest="f" * 64,
            _factory_seal=command_policy._RESOLVED_COMMAND_FACTORY_SEAL,
        )

    def test_short_stall_clock_terminates_owned_process_without_residue(self) -> None:
        (self.project / "test_supervised_stall.py").write_text(
            "import time\n"
            "import unittest\n\n"
            "class SlowTest(unittest.TestCase):\n"
            "    def test_wait(self):\n"
            "        time.sleep(30)\n",
            encoding="utf-8",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=40,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )
        result = supervisor.run(
            self.resolved_test_only_command(
                "test_supervised_stall.SlowTest.test_wait"
            ),
            timeout_ms=5_000,
            output_limit_bytes=4_096,
        )

        self.assertTrue(result.suspected_stall)
        self.assertIn("suspected_stall", result.reason_codes)
        self.assertFalse(result.termination["residual_process"])
        self.assertLess(result.duration_ms, 2_000)
        self.assertGreaterEqual(len(result.observations), 1)
        self.assertEqual("stdin_disabled", result.observations[0]["input_wait"])
        self._assert_process_gone(result.pid)

    def test_output_is_bounded_and_artifact_names_are_windows_safe(self) -> None:
        (self.project / "test_supervised_output.py").write_text(
            "import unittest\n\n"
            "class OutputTest(unittest.TestCase):\n"
            "    def test_output(self):\n"
            "        print('x' * 5000, flush=True)\n",
            encoding="utf-8",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=5_000,
            poll_ms=5,
            termination_grace_ms=100,
            artifact_hard_limit_bytes=1_024,
            trusted_clock=lambda: T1,
        )
        result = supervisor.run(
            self.resolved_test_only_command(
                "test_supervised_output.OutputTest.test_output"
            ),
            timeout_ms=5_000,
            output_limit_bytes=1_024,
        )

        self.assertTrue(result.truncated)
        self.assertIn("tool_output_limit_exceeded", result.reason_codes)
        self.assertIn("tool_output_artifact_limit_exceeded", result.reason_codes)
        self.assertLessEqual(
            len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")),
            1_024,
        )
        self.assertEqual(2, len(result.artifact_references))
        for reference in result.artifact_references:
            artifact = Path(reference)
            self.assertEqual(self.artifacts.resolve(), artifact.parent)
            self.assertRegex(
                artifact.name,
                r"^[0-9a-f]{64}\.(?:stdout|stderr)\.log$",
            )
            self.assertNotIn(":", artifact.name)
            self.assertLessEqual(artifact.stat().st_size, 1_024)
        self._assert_process_gone(result.pid)

    def test_secret_output_is_redacted_from_result_and_both_artifacts(self) -> None:
        secret = "api_key=abcdefghijklmnop"
        (self.project / "test_supervised_secret.py").write_text(
            "import sys\n"
            "import unittest\n\n"
            "class SecretTest(unittest.TestCase):\n"
            "    def test_secret(self):\n"
            "        print('safe output', flush=True)\n"
            f"        sys.stderr.write('{secret}\\n')\n",
            encoding="utf-8",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=5_000,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )
        result = supervisor.run(
            self.resolved_test_only_command(
                "test_supervised_secret.SecretTest.test_secret"
            ),
            timeout_ms=5_000,
            output_limit_bytes=4_096,
        )

        self.assertIn("tool_output_secret_detected", result.reason_codes)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertEqual(2, len(result.artifact_references))
        for reference in result.artifact_references:
            persisted = Path(reference).read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertEqual("[REDACTED: secret material detected]\n", persisted)

    def test_native_read_list_search_and_sqlite_are_readonly(self) -> None:
        (self.project / "notes.txt").write_bytes(b"first\nneedle here\n")
        folder = self.project / "folder"
        folder.mkdir()
        (folder / "one.txt").write_bytes(b"one\n")
        database = self.project / "sample.db"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA user_version=17")
            connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        database_before = database.read_bytes()
        sidecars_before = {
            suffix: (database.parent / f"{database.name}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        }

        read_binding = resolve_project_path(
            project_root=self.project,
            relative_path="notes.txt",
            access_mode="read_file",
        )
        listing_binding = resolve_project_path(
            project_root=self.project,
            relative_path="folder",
            access_mode="read_directory",
        )
        search_binding = resolve_project_path(
            project_root=self.project,
            relative_path="notes.txt",
            access_mode="read_file",
        )
        database_binding = resolve_project_path(
            project_root=self.project,
            relative_path="sample.db",
            access_mode="read_file",
        )

        read = self.broker.read_file(read_binding)
        listing = self.broker.list_directory(
            listing_binding,
            maximum_entries=10,
        )
        search = self.broker.search_text(
            search_binding,
            search_text="needle",
            maximum_results=10,
        )
        inspection = self.broker.inspect_database(database_binding)

        self.assertEqual("first\nneedle here\n", read["content"])
        self.assertEqual(["folder/one.txt"], listing["entries"])
        self.assertEqual(2, search["matches"][0]["line"])
        self.assertEqual("ok", inspection["integrity"])
        self.assertEqual(17, inspection["user_version"])
        self.assertEqual(["sample"], inspection["tables"])
        self.assertEqual("stable_readonly_snapshot", inspection["freshness"])
        self.assertEqual([], inspection["sidecars"])
        self.assertEqual(["-wal", "-shm"], inspection["absent_sidecars"])
        self.assertRegex(inspection["database_identity_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(inspection["snapshot_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(database_before, database.read_bytes())
        self.assertEqual(
            sidecars_before,
            {
                suffix: (database.parent / f"{database.name}{suffix}").exists()
                for suffix in ("-wal", "-shm", "-journal")
            },
        )

    def test_sqlite_wal_is_visible_and_sidecar_change_fails_closed(self) -> None:
        database = self.project / "active.db"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        self.assertEqual("wal", writer.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE current_state(id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO current_state(value) VALUES('from-wal')")
        writer.commit()
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="active.db",
            access_mode="read_file",
        )

        inspection = self.broker.inspect_database(binding)
        self.assertEqual(["current_state"], inspection["tables"])
        self.assertEqual("stable_readonly_snapshot", inspection["freshness"])
        self.assertEqual(
            ["shm", "wal"],
            sorted(item["kind"] for item in inspection["sidecars"]),
        )
        self.assertEqual([], inspection["absent_sidecars"])
        for sidecar in inspection["sidecars"]:
            self.assertGreater(sidecar["size"], 0)
            self.assertRegex(sidecar["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(sidecar["identity_digest"], r"^[0-9a-f]{64}$")

        original_capture = self.broker._capture_sqlite_set
        calls = 0

        def mutate_before_second_capture(active_binding):
            nonlocal calls
            calls += 1
            if calls == 2:
                writer.execute("INSERT INTO current_state(value) VALUES('changed')")
                writer.commit()
            return original_capture(active_binding)

        with patch.object(
            self.broker,
            "_capture_sqlite_set",
            side_effect=mutate_before_second_capture,
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as changed:
                self.broker.inspect_database(binding)
        self.assertEqual(
            "tool_database_snapshot_changed",
            changed.exception.reason_code,
        )

    def test_patch_backup_storage_key_and_complete_rollback(self) -> None:
        target = self.project / "bounded.txt"
        target.write_bytes(b"before\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "bounded.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="bounded.txt",
            access_mode="write_file",
        )
        patched = self.broker.patch_file(
            proposal=proposal,
            execution_key="execution:patch",
            binding=binding,
        )

        self.assertEqual(b"after\n", target.read_bytes())
        reference = patched["backup_reference"]
        storage_key = canonical_digest({"backup_reference": reference})
        self.assertRegex(storage_key, r"^[0-9a-f]{64}$")
        record_path = self.artifacts / f"{storage_key}.json"
        before_path = self.artifacts / f"{storage_key}.before.bin"
        self.assertTrue(record_path.is_file())
        self.assertTrue(before_path.is_file())
        self.assertNotIn(":", record_path.name)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(storage_key, record["artifact_storage_key"])
        self.assertEqual(before_path.name, record["before_artifact"])

        rolled_back = self.broker.rollback_patch(
            proposal=proposal,
            backup_reference=reference,
            expected_current_sha256=sha("after\n"),
        )
        self.assertTrue(rolled_back["verification_passed"])
        self.assertEqual(b"before\n", target.read_bytes())

    def test_create_file_is_exclusive_and_records_safe_backup_key(self) -> None:
        proposal = self.proposal(
            "project.file.create",
            {
                "relative_path": "created.txt",
                "content": "created\n",
                "expected_after_sha256": sha("created\n"),
            },
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="created.txt",
            access_mode="create_file",
        )
        created = self.broker.create_file(
            proposal=proposal,
            execution_key="execution:create",
            binding=binding,
        )

        self.assertEqual(b"created\n", (self.project / "created.txt").read_bytes())
        storage_key = canonical_digest(
            {"backup_reference": created["backup_reference"]}
        )
        record_path = self.artifacts / f"{storage_key}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(storage_key, record["artifact_storage_key"])
        self.assertIsNone(record["before_artifact"])
        with self.assertRaisesRegex(
            CognitivePathPolicyError,
            "tool_create_target_exists",
        ):
            resolve_project_path(
                project_root=self.project,
                relative_path="created.txt",
                access_mode="create_file",
            )

    def test_out_of_scope_and_illegal_paths_fail_closed_without_effect(self) -> None:
        outside = self.project.parent / "outside.txt"
        cases = (
            "../outside.txt",
            str(outside),
            "C:relative.txt",
            "name.txt:stream",
            ".git/config",
            "folder/../outside.txt",
        )
        for value in cases:
            with self.subTest(relative_path=value):
                with self.assertRaises(CognitivePathPolicyError):
                    resolve_project_path(
                        project_root=self.project,
                        relative_path=value,
                        access_mode="create_file",
                    )
        self.assertFalse(outside.exists())

        other_project = self.project.parent / "other"
        other_project.mkdir()
        (other_project / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        foreign_binding = resolve_project_path(
            project_root=other_project,
            relative_path="foreign.txt",
            access_mode="read_file",
        )
        with self.assertRaisesRegex(
            CognitiveLocalBrokerError,
            "tool_broker_path_binding_mismatch",
        ):
            self.broker.read_file(foreign_binding)
        self.assertEqual(
            "foreign\n",
            (other_project / "foreign.txt").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
