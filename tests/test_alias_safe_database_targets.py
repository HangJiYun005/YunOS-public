from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import memory.db_path_policy as policy_module
from memory.db_path_policy import (
    DEFAULT_APPROVED_PROFILE_SIDECAR_PATH,
    DEFAULT_OPERATIONAL_STATE_PATH,
    DEFAULT_RUNTIME_SIDECAR_PATH,
    KnowledgeSidecarPathError,
    PROTECTED_PRODUCTION_DATABASE_TARGETS,
    _reject_protected_database_alias,
    authorize_alias_safe_test_database_target,
    connect_alias_safe_test_database,
    create_alias_safe_test_database_connection,
    validate_opened_test_database_connection,
    validate_knowledge_test_db_target,
)
from memory.profile_authorization_fidelity import (
    AuthorizationFidelityError,
    _AuthorizationFidelityTestCore,
)
from memory.profile_enablement import (
    ProfileEnablementError,
    _ProfileEnablementCore,
)
from memory.operational_state_models import OperationalStateError
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_names(path: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(path)
    try:
        return tuple(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master ORDER BY type,name"
            )
        )
    finally:
        conn.close()


class AliasSafeDatabaseTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.production_dir = self.root / "simulated-production"
        self.test_dir = self.root / "test-targets"
        self.production_dir.mkdir()
        self.test_dir.mkdir()
        self.production = self.production_dir / "approved_profile.db"
        conn = sqlite3.connect(self.production)
        try:
            conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO sentinel VALUES('untouched')")
            conn.commit()
        finally:
            conn.close()
        self.production_hash = _sha256(self.production)
        self.production_names = _sqlite_names(self.production)

    def tearDown(self) -> None:
        self.assertEqual(_sha256(self.production), self.production_hash)
        self.assertEqual(_sqlite_names(self.production), self.production_names)
        self.temp.cleanup()

    def test_fixed_registry_contains_all_nine_production_targets(self) -> None:
        relative = os.path.relpath(DEFAULT_APPROVED_PROFILE_SIDECAR_PATH, Path.cwd())
        case_variant = Path(str(DEFAULT_APPROVED_PROFILE_SIDECAR_PATH).swapcase())
        self.assertEqual(len(PROTECTED_PRODUCTION_DATABASE_TARGETS), 9)
        for candidate in (
            DEFAULT_APPROVED_PROFILE_SIDECAR_PATH,
            DEFAULT_OPERATIONAL_STATE_PATH,
            DEFAULT_RUNTIME_SIDECAR_PATH,
            relative,
            case_variant,
            Path(relative).parent / "unused" / ".." / Path(relative).name,
        ):
            with self.assertRaises(KnowledgeSidecarPathError):
                validate_knowledge_test_db_target(candidate)

    def test_hardlink_alias_is_rejected_without_skip(self) -> None:
        alias = self.test_dir / "hardlink.db"
        os.link(self.production, alias)
        self.assertTrue(os.path.samefile(self.production, alias))
        with self.assertRaises(KnowledgeSidecarPathError):
            _reject_protected_database_alias(alias, self.production)

    def test_directory_symlink_alias_is_rejected_when_supported(self) -> None:
        alias_dir = self.root / "directory-alias"
        try:
            alias_dir.symlink_to(self.production_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaises(KnowledgeSidecarPathError):
            _reject_protected_database_alias(alias_dir / self.production.name, self.production)

    def test_mocked_reparse_parent_is_rejected_without_skip(self) -> None:
        target = self.test_dir / "reparse-parent.db"
        original = policy_module.is_reparse_point

        def fake_reparse(path: str | Path) -> bool:
            return Path(path) == self.test_dir or original(path)

        with patch.object(policy_module, "is_reparse_point", side_effect=fake_reparse):
            with self.assertRaises(KnowledgeSidecarPathError):
                authorize_alias_safe_test_database_target(target)

    def test_hardlink_replacement_between_validation_and_open_is_rejected(self) -> None:
        target = self.test_dir / "race.db"
        conn = sqlite3.connect(target)
        try:
            conn.execute("CREATE TABLE harmless(value TEXT)")
            conn.commit()
        finally:
            conn.close()
        real_connect = sqlite3.connect
        calls = 0

        def replace_then_connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            target.unlink()
            os.link(self.production, target)
            return real_connect(*args, **kwargs)

        with patch.object(policy_module.sqlite3, "connect", side_effect=replace_then_connect):
            with self.assertRaises(KnowledgeSidecarPathError):
                connect_alias_safe_test_database(target)
        self.assertEqual(calls, 1)

    def test_operational_store_revalidates_identity_at_every_open(self) -> None:
        target = self.test_dir / "operational-race.db"
        initialize_operational_database(
            target,
            created_at="2026-07-31T00:00:00Z",
        )
        store = OperationalStateStore(target)
        real_connect = sqlite3.connect

        def replace_then_connect(*args, **kwargs):
            target.unlink()
            os.link(self.production, target)
            return real_connect(*args, **kwargs)

        with patch.object(
            policy_module.sqlite3,
            "connect",
            side_effect=replace_then_connect,
        ):
            with self.assertRaisesRegex(
                OperationalStateError,
                "unsafe_operational_database_target",
            ):
                store.read_status()

    def test_opened_database_must_match_authorized_file_identity(self) -> None:
        first = self.test_dir / "first.db"
        second = self.test_dir / "second.db"
        for path in (first, second):
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE harmless(value TEXT)")
                conn.commit()
            finally:
                conn.close()
        authorization = authorize_alias_safe_test_database_target(first, require_exists=True)
        conn = sqlite3.connect(second)
        try:
            with self.assertRaises(KnowledgeSidecarPathError):
                validate_opened_test_database_connection(conn, authorization)
        finally:
            conn.close()

    def test_normal_temporary_database_can_be_created_and_written(self) -> None:
        target = self.test_dir / "normal.db"
        with create_alias_safe_test_database_connection(target) as conn:
            conn.execute("CREATE TABLE sample(value TEXT NOT NULL)")
            conn.execute("INSERT INTO sample VALUES('created')")
            conn.commit()
        conn = connect_alias_safe_test_database(target)
        try:
            self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "created")
        finally:
            conn.close()

    def test_failed_creation_removes_only_the_exclusively_created_file(self) -> None:
        target = self.test_dir / "failed-create.db"
        with patch.object(
            policy_module.sqlite3,
            "connect",
            side_effect=sqlite3.OperationalError("forced open failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                with create_alias_safe_test_database_connection(target):
                    self.fail("connection must not be yielded")
        self.assertFalse(target.exists())

    def test_identity_resolution_errors_fail_closed(self) -> None:
        target = self.test_dir / "identity-errors.db"
        conn = sqlite3.connect(target)
        conn.close()
        with patch.object(policy_module.os.path, "samefile", side_effect=OSError("denied")):
            with self.assertRaises(KnowledgeSidecarPathError):
                authorize_alias_safe_test_database_target(target, require_exists=True)
        with patch.object(policy_module.os.path, "realpath", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                authorize_alias_safe_test_database_target(target, require_exists=True)

        real_stat = os.stat

        def fail_target_stat(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        with patch.object(policy_module.os, "stat", side_effect=fail_target_stat):
            with self.assertRaises(KnowledgeSidecarPathError):
                authorize_alias_safe_test_database_target(target, require_exists=True)

    def test_profile_test_cores_reject_the_fixed_production_target(self) -> None:
        from memory.approved_profile_control import (
            ApprovedProfileControlError,
            _ApprovedProfileControlCore,
        )

        with self.assertRaises(ApprovedProfileControlError):
            _ApprovedProfileControlCore(
                lambda *_args, **_kwargs: {}, DEFAULT_APPROVED_PROFILE_SIDECAR_PATH,
            )
        with self.assertRaises(ProfileEnablementError):
            _ProfileEnablementCore(
                lambda *_args, **_kwargs: {}, DEFAULT_APPROVED_PROFILE_SIDECAR_PATH,
            )
        with self.assertRaises(AuthorizationFidelityError):
            _AuthorizationFidelityTestCore(DEFAULT_APPROVED_PROFILE_SIDECAR_PATH)

    def test_fixed_validator_has_no_policy_or_production_root_override(self) -> None:
        blocked = {"policy", "protected_targets", "production_root", "resolver"}
        for function in (
            authorize_alias_safe_test_database_target,
            validate_knowledge_test_db_target,
            connect_alias_safe_test_database,
            create_alias_safe_test_database_connection,
        ):
            self.assertTrue(blocked.isdisjoint(inspect.signature(function).parameters))
        source = Path(policy_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_PRODUCTION_TARGET_POLICY", source)
        self.assertNotIn("_AliasSafeTargetPolicy", source)
        self.assertNotIn("os.environ", source)

    def test_production_modules_do_not_import_a_tests_only_policy(self) -> None:
        modules = (
            "memory/approved_profile_control.py",
            "memory/profile_enablement.py",
            "memory/profile_authorization_fidelity.py",
        )
        root = Path(__file__).resolve().parents[1]
        for relative in modules:
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("tests.", source)
            self.assertNotIn("_AliasSafeTargetPolicy", source)
            self.assertNotIn("_PRODUCTION_TARGET_POLICY", source)


if __name__ == "__main__":
    unittest.main()
