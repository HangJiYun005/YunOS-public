from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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
from memory.operational_state_models import canonical_digest


T1 = "2026-08-01T00:00:00.000000Z"


def sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class SecuredWritePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.artifacts = root / "artifacts"
        self.outside = root / "outside"
        self.project.mkdir()
        self.artifacts.mkdir()
        self.outside.mkdir()
        self.broker = CognitiveLocalBroker(
            project_root=self.project,
            artifact_root=self.artifacts,
        )

    @staticmethod
    def profile(capability_id: str):
        return next(
            item
            for item in bounded_execution_capability_profiles(
                {capability_id: "available"}
            )
            if item.capability_id == capability_id
        )

    def proposal(self, capability_id: str, arguments: dict, *, suffix: str):
        profile = self.profile(capability_id)
        typed_arguments = profile.validate_input(arguments)
        return _tool_proposal_from_verified_record(
            {
                "protocol_version": TOOL_PROTOCOL_VERSION,
                "proposal_id": f"toolproposal:{suffix}",
                "run_id": f"run:{suffix}",
                "objective_id": f"objective:{suffix}",
                "objective_digest": sha(f"objective:{suffix}"),
                "plan_id": f"plan:{suffix}",
                "plan_version": 1,
                "plan_digest": sha(f"plan:{suffix}"),
                "plan_node_id": f"node:{suffix}",
                "capability_id": capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": typed_arguments,
                "argument_digest": canonical_digest(typed_arguments),
                "idempotency_key": f"idempotency:{suffix}",
                "reason": "Verify a handle-secured bounded write",
                "expected_observation": "Only the bound file may change",
                "risk_class": profile.risk_class,
                "authorization_id": f"authorization:{suffix}",
                "authorization_digest": sha(f"authorization:{suffix}"),
                "status": "proposed",
                "proposed_at": T1,
                "runtime_execution_reference": None,
                "runtime_receipt_digest": None,
                "proposal_digest": "",
            }
        )

    def _race_hook(self, parent: Path, moved: Path):
        observation = {"swap_completed": False, "swap_blocked": False}

        def swap_parent(_binding) -> None:
            try:
                parent.rename(moved)
            except OSError:
                # Windows directory handles intentionally omit FILE_SHARE_DELETE.
                observation["swap_blocked"] = True
                raise CognitiveLocalBrokerError(
                    "tool_secured_path_identity_changed"
                )
            parent.symlink_to(self.outside, target_is_directory=True)
            observation["swap_completed"] = True

        return observation, swap_parent

    @staticmethod
    def _restore_parent(parent: Path, moved: Path) -> None:
        if parent.is_symlink():
            parent.unlink()
        if moved.exists():
            moved.rename(parent)

    def test_normal_patch_create_and_rollback_use_secured_handles(self) -> None:
        parent = self.project / "normal"
        parent.mkdir()
        target = parent / "target.txt"
        target.write_bytes(b"before\n")
        patch_proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "normal/target.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="normal-patch",
        )
        patched = self.broker.patch_file(
            proposal=patch_proposal,
            execution_key="execution:normal-patch",
            binding=resolve_project_path(
                project_root=self.project,
                relative_path="normal/target.txt",
                access_mode="write_file",
            ),
        )
        self.assertEqual(b"after\n", target.read_bytes())

        rolled_back = self.broker.rollback_patch(
            proposal=patch_proposal,
            backup_reference=patched["backup_reference"],
            expected_current_sha256=sha("after\n"),
        )
        self.assertTrue(rolled_back["verification_passed"])
        self.assertEqual(b"before\n", target.read_bytes())

        create_proposal = self.proposal(
            "project.file.create",
            {
                "relative_path": "normal/created.txt",
                "content": "created\n",
                "expected_after_sha256": sha("created\n"),
            },
            suffix="normal-create",
        )
        created = self.broker.create_file(
            proposal=create_proposal,
            execution_key="execution:normal-create",
            binding=resolve_project_path(
                project_root=self.project,
                relative_path="normal/created.txt",
                access_mode="create_file",
            ),
        )
        self.assertTrue(created["verification_passed"])
        self.assertEqual(b"created\n", (parent / "created.txt").read_bytes())

    def test_parent_swap_is_blocked_for_patch(self) -> None:
        parent = self.project / "race-patch"
        moved = self.project / "race-patch-moved"
        parent.mkdir()
        (parent / "sentinel.txt").write_bytes(b"inside\n")
        outside_sentinel = self.outside / "sentinel.txt"
        outside_sentinel.write_bytes(b"outside\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "race-patch/sentinel.txt",
                "expected_before_sha256": sha("inside\n"),
                "replacement_content": "changed\n",
                "expected_after_sha256": sha("changed\n"),
            },
            suffix="race-patch",
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-patch/sentinel.txt",
            access_mode="write_file",
        )
        observation, race = self._race_hook(parent, moved)
        try:
            with patch(
                "memory.cognitive_local_broker._write_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    self.broker.patch_file(
                        proposal=proposal,
                        execution_key="execution:race-patch",
                        binding=binding,
                    )
            self.assertFalse(rejected.exception.effect_may_have_occurred)
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"],
                msg=rejected.exception.reason_code,
            )
            self.assertEqual(b"outside\n", outside_sentinel.read_bytes())
        finally:
            self._restore_parent(parent, moved)
        self.assertEqual(b"inside\n", (parent / "sentinel.txt").read_bytes())

    def test_parent_swap_is_blocked_for_create(self) -> None:
        parent = self.project / "race-create"
        moved = self.project / "race-create-moved"
        parent.mkdir()
        outside_sentinel = self.outside / "created.txt"
        outside_sentinel.write_bytes(b"outside\n")
        proposal = self.proposal(
            "project.file.create",
            {
                "relative_path": "race-create/created.txt",
                "content": "created\n",
                "expected_after_sha256": sha("created\n"),
            },
            suffix="race-create",
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-create/created.txt",
            access_mode="create_file",
        )
        observation, race = self._race_hook(parent, moved)
        try:
            with patch(
                "memory.cognitive_local_broker._write_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    self.broker.create_file(
                        proposal=proposal,
                        execution_key="execution:race-create",
                        binding=binding,
                    )
            self.assertFalse(rejected.exception.effect_may_have_occurred)
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"],
                msg=rejected.exception.reason_code,
            )
            self.assertEqual(b"outside\n", outside_sentinel.read_bytes())
        finally:
            self._restore_parent(parent, moved)
        self.assertFalse((parent / "created.txt").exists())

    def test_parent_swap_is_blocked_for_rollback(self) -> None:
        parent = self.project / "race-rollback"
        moved = self.project / "race-rollback-moved"
        parent.mkdir()
        target = parent / "sentinel.txt"
        target.write_bytes(b"before\n")
        outside_sentinel = self.outside / "sentinel.txt"
        outside_sentinel.write_bytes(b"outside\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "race-rollback/sentinel.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="race-rollback",
        )
        patched = self.broker.patch_file(
            proposal=proposal,
            execution_key="execution:race-rollback",
            binding=resolve_project_path(
                project_root=self.project,
                relative_path="race-rollback/sentinel.txt",
                access_mode="write_file",
            ),
        )
        observation, race = self._race_hook(parent, moved)
        try:
            with patch(
                "memory.cognitive_local_broker._write_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    self.broker.rollback_patch(
                        proposal=proposal,
                        backup_reference=patched["backup_reference"],
                        expected_current_sha256=sha("after\n"),
                    )
            self.assertFalse(rejected.exception.effect_may_have_occurred)
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"],
                msg=rejected.exception.reason_code,
            )
            self.assertEqual(b"outside\n", outside_sentinel.read_bytes())
        finally:
            self._restore_parent(parent, moved)
        self.assertEqual(b"after\n", (parent / "sentinel.txt").read_bytes())

    def test_post_write_identity_failure_marks_effect_as_uncertain(self) -> None:
        parent = self.project / "uncertain"
        parent.mkdir()
        target = parent / "target.txt"
        target.write_bytes(b"before\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "uncertain/target.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="uncertain-patch",
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="uncertain/target.txt",
            access_mode="write_file",
        )
        with patch(
            "memory.cognitive_local_broker._verify_secured_file",
            side_effect=CognitiveLocalBrokerError(
                "tool_secured_write_verification_failed"
            ),
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as uncertain:
                self.broker.patch_file(
                    proposal=proposal,
                    execution_key="execution:uncertain-patch",
                    binding=binding,
                )
        self.assertTrue(uncertain.exception.effect_may_have_occurred)
        self.assertEqual(b"after\n", target.read_bytes())

    def test_read_file_parent_swap_never_returns_outside_content(self) -> None:
        parent = self.project / "race-read"
        moved = self.project / "race-read-moved"
        parent.mkdir()
        target = parent / "sentinel.txt"
        target.write_bytes(b"inside\n")
        outside_target = self.outside / "sentinel.txt"
        outside_target.write_bytes(b"outside-secret\n")
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-read/sentinel.txt",
            access_mode="read_file",
        )
        observation, race = self._race_hook(parent, moved)
        result = None
        try:
            with patch(
                "memory.cognitive_local_broker._read_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    result = self.broker.read_file(binding)
            self.assertEqual(
                "tool_secured_path_identity_changed",
                rejected.exception.reason_code,
            )
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"]
            )
            self.assertIsNone(result)
            self.assertEqual(b"outside-secret\n", outside_target.read_bytes())
        finally:
            self._restore_parent(parent, moved)
        self.assertEqual(b"inside\n", target.read_bytes())

    def test_list_directory_parent_swap_never_returns_outside_entries(self) -> None:
        parent = self.project / "race-list"
        moved = self.project / "race-list-moved"
        parent.mkdir()
        (parent / "inside.txt").write_bytes(b"inside\n")
        (self.outside / "outside-secret.txt").write_bytes(b"outside\n")
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-list",
            access_mode="read_directory",
        )
        observation, race = self._race_hook(parent, moved)
        result = None
        try:
            with patch(
                "memory.cognitive_local_broker._read_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    result = self.broker.list_directory(binding, maximum_entries=10)
            self.assertEqual(
                "tool_secured_path_identity_changed",
                rejected.exception.reason_code,
            )
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"]
            )
            self.assertIsNone(result)
        finally:
            self._restore_parent(parent, moved)

    def test_search_parent_swap_never_returns_outside_matches(self) -> None:
        parent = self.project / "race-search"
        moved = self.project / "race-search-moved"
        parent.mkdir()
        (parent / "inside.txt").write_bytes(b"inside\n")
        (self.outside / "outside.txt").write_bytes(b"outside-secret\n")
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-search",
            access_mode="read_directory",
        )
        observation, race = self._race_hook(parent, moved)
        result = None
        try:
            with patch(
                "memory.cognitive_local_broker._read_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    result = self.broker.search_text(
                        binding,
                        search_text="outside-secret",
                        maximum_results=10,
                    )
            self.assertEqual(
                "tool_secured_path_identity_changed",
                rejected.exception.reason_code,
            )
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"]
            )
            self.assertIsNone(result)
        finally:
            self._restore_parent(parent, moved)

    def test_sqlite_parent_swap_never_queries_outside_database(self) -> None:
        parent = self.project / "race-sqlite"
        moved = self.project / "race-sqlite-moved"
        parent.mkdir()
        database = parent / "state.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE inside_state(value TEXT)")
            connection.commit()
        outside_database = self.outside / "state.db"
        with closing(sqlite3.connect(outside_database)) as connection:
            connection.execute("CREATE TABLE outside_secret(value TEXT)")
            connection.commit()
        inside_before = database.read_bytes()
        outside_before = outside_database.read_bytes()
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="race-sqlite/state.db",
            access_mode="read_file",
        )
        observation, race = self._race_hook(parent, moved)
        result = None
        try:
            with patch(
                "memory.cognitive_local_broker._read_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    result = self.broker.inspect_database(binding)
            self.assertEqual(
                "tool_secured_path_identity_changed",
                rejected.exception.reason_code,
            )
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"]
            )
            self.assertIsNone(result)
            self.assertEqual(outside_before, outside_database.read_bytes())
        finally:
            self._restore_parent(parent, moved)
        self.assertEqual(inside_before, database.read_bytes())

    def test_real_junction_or_symlink_project_root_is_denied(self) -> None:
        target = self.outside / "linked-project"
        target.mkdir()
        (target / "sentinel.txt").write_bytes(b"outside\n")
        link = Path(self.temporary.name) / "linked-root"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaises(CognitivePathPolicyError) as denied:
                resolve_project_path(
                    project_root=link,
                    relative_path="sentinel.txt",
                    access_mode="read_file",
                )
            self.assertEqual("tool_path_reparse_denied", denied.exception.reason_code)
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_local_broker_artifact_root_identity_drift_blocks_project_write(self) -> None:
        parent = self.project / "artifact-drift"
        parent.mkdir()
        target = parent / "target.txt"
        target.write_bytes(b"before\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "artifact-drift/target.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="artifact-drift",
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="artifact-drift/target.txt",
            access_mode="write_file",
        )
        moved = Path(self.temporary.name) / "artifacts-original"
        self.artifacts.rename(moved)
        self.artifacts.mkdir()

        with self.assertRaises(CognitiveLocalBrokerError) as denied:
            self.broker.patch_file(
                proposal=proposal,
                execution_key="execution:artifact-drift",
                binding=binding,
            )
        self.assertEqual("tool_artifact_root_changed", denied.exception.reason_code)
        self.assertEqual(b"before\n", target.read_bytes())
        self.assertEqual([], list(self.artifacts.iterdir()))


if __name__ == "__main__":
    unittest.main()
