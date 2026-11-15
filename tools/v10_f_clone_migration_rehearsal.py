from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping
import uuid


MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from memory.cognitive_production_migration import (
    CognitiveProductionMigrationError,
    _apply_fixed_cognitive_ddl,
    _assert_core_only,
    _assert_publication_pin,
    _build_plan,
    _configure_connection,
    _core_data_digest,
    _core_schema_snapshot_digest,
    _existing_cognitive_objects,
    _open_publication_pin,
    _reconcile_database,
    _table_content_digest,
)
from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    _COGNITIVE_OBJECT_NAMES,
    _COGNITIVE_OBJECTS,
    _verify_cognitive_connection,
)
from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
)
from memory.operational_state_store import (
    SCHEMA_VERSION,
    OperationalStateStore,
    schema_fingerprint,
)
from tools.v10_release_recovery import (
    DEFAULT_ASSET_ROOT,
    DEFAULT_DATABASES,
    RecoveryError,
    _file_gate,
    _safe_descendant,
    _safe_existing_root,
    _sqlite_file_gates,
    audit_tree,
)


CONTRACT_VERSION = "yunos-v10-f-clone-migration-rehearsal-1"
OWNERSHIP_VERSION = "yunos-v10-f-clone-rehearsal-ownership-1"
AUTHORIZATION_ENV = "YUNOS_V10_F_CLONE_MIGRATION_REHEARSAL"
AUTHORIZATION_ENV_VALUE = "1"
USER_AUTHORIZATION_SHA256 = (
    "ff968831ddaf4e694673272ede56dc7857286b77c1cfddbd908559385282ae11"
)
OUTPUT_NAME = "v10-f-clone-migration-rehearsal"
SENTRY_NAME = "v10-f-clone-migration-rehearsal-one-shot-sentry.json"
FAILURE_NAME = "v10-f-clone-migration-rehearsal-failure.json"
REPORT_NAME = "clone-migration-rehearsal.json"
OWNER_MARKER_NAME = ".yunos-v10-f-clone-rehearsal-owned.json"
SOURCE_SNAPSHOT_NAME = "bound-operational-backup.db"
SUCCESS_CLONE_NAME = "migration-success.db"
PRECOMMIT_CLONE_NAME = "fault-precommit.db"
AFTERCOMMIT_CLONE_NAME = "fault-aftercommit.db"
RESTORE_CLONE_NAME = "restored-from-bound-backup.db"
EXPECTED_COGNITIVE_OBJECT_COUNT = 45
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_REPARSE_ATTRIBUTE = 0x400


class CloneMigrationRehearsalError(RuntimeError):
    pass


class _InjectedCloneFault(RuntimeError):
    pass


@dataclass(frozen=True)
class CloneMigrationRehearsalConfirmation:
    production: bool
    confirm_one_shot: bool
    confirm_readonly_production: bool
    confirm_disposable_clones: bool
    confirm_no_service_stop: bool
    confirm_no_git_or_stash_mutation: bool
    confirm_head: str
    user_authorization_sha256: str

    def validate(self, *, production: bool) -> None:
        if type(self) is not CloneMigrationRehearsalConfirmation:
            raise CloneMigrationRehearsalError("exact_confirmation_required")
        if self.production is not production:
            raise CloneMigrationRehearsalError("confirmation_mode_mismatch")
        if not all(
            value is True
            for value in (
                self.confirm_one_shot,
                self.confirm_readonly_production,
                self.confirm_disposable_clones,
                self.confirm_no_service_stop,
                self.confirm_no_git_or_stash_mutation,
            )
        ):
            raise CloneMigrationRehearsalError("confirmation_incomplete")
        if _HEAD.fullmatch(self.confirm_head or "") is None:
            raise CloneMigrationRehearsalError("confirmed_head_invalid")
        if self.user_authorization_sha256 != USER_AUTHORIZATION_SHA256:
            raise CloneMigrationRehearsalError(
                "user_authorization_digest_mismatch"
            )

    @property
    def confirmation_digest(self) -> str:
        return canonical_digest(
            {
                "production": self.production,
                "confirm_one_shot": self.confirm_one_shot,
                "confirm_readonly_production": (
                    self.confirm_readonly_production
                ),
                "confirm_disposable_clones": self.confirm_disposable_clones,
                "confirm_no_service_stop": self.confirm_no_service_stop,
                "confirm_no_git_or_stash_mutation": (
                    self.confirm_no_git_or_stash_mutation
                ),
                "confirm_head": self.confirm_head,
                "user_authorization_sha256": (
                    self.user_authorization_sha256
                ),
            }
        )


@dataclass(frozen=True)
class _BoundPaths:
    project_root: Path
    output_parent: Path
    output: Path
    sentry: Path
    failure: Path
    databases: Mapping[str, Path]
    assets: Path
    production: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _reason(exc: BaseException) -> str:
    if isinstance(
        exc,
        (
            CloneMigrationRehearsalError,
            CognitiveProductionMigrationError,
            OperationalStateError,
            RecoveryError,
        ),
    ):
        value = str(exc).strip()
        if re.fullmatch(r"[a-z0-9_\-]+", value):
            return value
    return "clone_migration_rehearsal_internal_error"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value)
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise OSError("short_write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        raise CloneMigrationRehearsalError("one_shot_artifact_exists") from None
    except OSError:
        raise CloneMigrationRehearsalError("exclusive_record_write_failed") from None


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_exclusive_json(path, value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("too_large")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise CloneMigrationRehearsalError("control_document_invalid") from None
    if not isinstance(value, dict):
        raise CloneMigrationRehearsalError("control_document_invalid")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        raise CloneMigrationRehearsalError("path_identity_unverifiable") from None
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _directory_identity(path: Path) -> tuple[int, int]:
    resolved = _safe_existing_root(path, directory=True)
    details = resolved.stat()
    return int(details.st_dev), int(details.st_ino)


def _assert_directory_identity(path: Path, expected: tuple[int, int]) -> None:
    if _directory_identity(path) != expected:
        raise CloneMigrationRehearsalError("output_parent_identity_changed")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_execution_root(project_root: Path, *, production: bool) -> Path:
    selected = Path(project_root).resolve(strict=True)
    fixed_production = Path(__file__).resolve().parents[1]
    if production:
        if selected != fixed_production:
            raise CloneMigrationRehearsalError("production_root_not_fixed")
        return selected
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        selected == fixed_production
        or not _is_within(selected, temporary_root)
        or _is_reparse(selected)
    ):
        raise CloneMigrationRehearsalError("test_root_not_disposable")
    return selected


def _bound_paths(project_root: Path, *, production: bool) -> _BoundPaths:
    root = _safe_existing_root(project_root, directory=True)
    output_parent = _safe_descendant(root, Path("output"), directory=True)
    databases = {
        label: _safe_descendant(root, relative, directory=False)
        for label, relative in DEFAULT_DATABASES.items()
    }
    assets = _safe_descendant(root, DEFAULT_ASSET_ROOT, directory=True)
    output = output_parent / OUTPUT_NAME
    sentry = output_parent / SENTRY_NAME
    failure = output_parent / FAILURE_NAME
    for candidate in (output, sentry, failure):
        if candidate.exists() or candidate.is_symlink():
            raise CloneMigrationRehearsalError("one_shot_artifact_exists")
    return _BoundPaths(
        project_root=root,
        output_parent=output_parent,
        output=output,
        sentry=sentry,
        failure=failure,
        databases=databases,
        assets=assets,
        production=production,
    )


_GIT_ALLOWLIST = frozenset(
    {
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("rev-parse", "-q", "--verify", "refs/stash"),
    }
)


def _git_readonly(root: Path, arguments: tuple[str, ...]) -> str:
    if arguments not in _GIT_ALLOWLIST:
        raise CloneMigrationRehearsalError("git_command_not_allowlisted")
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        shell=False,
    )
    if completed.returncode not in ({0, 1} if "refs/stash" in arguments else {0}):
        raise CloneMigrationRehearsalError("git_read_failed")
    return completed.stdout.strip()


def _capture_git_state(root: Path) -> dict[str, Any]:
    head = _git_readonly(root, ("rev-parse", "HEAD"))
    if _HEAD.fullmatch(head) is None:
        raise CloneMigrationRehearsalError("git_head_invalid")
    status = _git_readonly(
        root, ("status", "--porcelain=v1", "--untracked-files=all")
    )
    stash = _git_readonly(root, ("rev-parse", "-q", "--verify", "refs/stash"))
    lines = tuple(line for line in status.splitlines() if line)
    return {
        "head": head,
        "tracked_worktree_clean": not any(
            not line.startswith("??") and len(line) > 1 and line[1] != " "
            for line in lines
        ),
        "staging_empty": not any(
            not line.startswith("??") and line[0] != " " for line in lines
        ),
        "untracked_empty": not any(line.startswith("??") for line in lines),
        "worktree_clean": not lines,
        "stash_ref": stash or None,
    }


def _validate_git_state(
    state: Mapping[str, Any], confirmation: CloneMigrationRehearsalConfirmation
) -> None:
    if state.get("head") != confirmation.confirm_head:
        raise CloneMigrationRehearsalError("confirmed_head_mismatch")
    if not all(
        state.get(key) is True
        for key in (
            "tracked_worktree_clean",
            "staging_empty",
            "untracked_empty",
            "worktree_clean",
        )
    ):
        raise CloneMigrationRehearsalError("git_worktree_not_clean")


def _capture_data_boundary(paths: _BoundPaths) -> dict[str, Any]:
    try:
        database_gates = {
            label: _sqlite_file_gates(path)
            for label, path in sorted(paths.databases.items())
        }
        assets = audit_tree(paths.assets)
    except RecoveryError as exc:
        raise CloneMigrationRehearsalError(str(exc)) from None
    return {
        "databases": database_gates,
        "assets": assets,
        "boundary_digest": canonical_digest(
            {"databases": database_gates, "assets": assets}
        ),
    }


@contextmanager
def _readonly_source_pin(path: Path) -> Iterator[Any]:
    pin = _open_publication_pin(path)
    try:
        yield pin
        _assert_publication_pin(pin)
    finally:
        if os.name != "nt":
            try:
                import fcntl

                fcntl.flock(pin.descriptor, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        try:
            os.close(pin.descriptor)
        except OSError:
            pass


def _open_immutable_source(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        _configure_connection(connection)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error:
        raise CloneMigrationRehearsalError(
            "production_operational_readonly_open_failed"
        ) from None


def _source_state(connection: sqlite3.Connection) -> dict[str, Any]:
    _assert_core_only(connection)
    marker = connection.execute(
        "SELECT marker,schema_version,schema_digest,writer_default_enabled "
        "FROM operational_schema WHERE singleton=1"
    ).fetchone()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    writers = int(
        connection.execute("SELECT COUNT(*) FROM state_writer_leases").fetchone()[0]
    )
    if (
        marker is None
        or int(marker["schema_version"]) != SCHEMA_VERSION
        or int(marker["writer_default_enabled"]) != 0
        or integrity != "ok"
        or foreign_keys != 0
        or writers != 0
    ):
        raise CloneMigrationRehearsalError("source_operational_gate_failed")
    digest = schema_fingerprint(connection)
    if marker["schema_digest"] != digest:
        raise CloneMigrationRehearsalError("source_operational_schema_drift")
    return {
        "schema_version": int(marker["schema_version"]),
        "schema_digest": digest,
        "core_schema_digest": _core_schema_snapshot_digest(connection),
        "core_data_digest": _core_data_digest(connection),
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "writer_lease_count": writers,
        "cognitive_object_count": len(_existing_cognitive_objects(connection)),
    }


def _snapshot_operational_source(
    source: Path, destination: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _sqlite_file_gates(source)
    if (
        before["db"]["exists"] is not True
        or before["db"]["size"] < 1
        or (before["wal"]["exists"] and before["wal"]["size"] != 0)
    ):
        raise CloneMigrationRehearsalError("production_operational_not_quiescent")
    with _readonly_source_pin(source) as pin:
        if (pin.size, pin.sha256) != (
            before["db"]["size"],
            before["db"]["sha256"],
        ):
            raise CloneMigrationRehearsalError("source_pin_binding_mismatch")
        with closing(_open_immutable_source(source)) as source_connection:
            source_state = _source_state(source_connection)
            with closing(sqlite3.connect(destination)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.commit()
        _assert_publication_pin(pin)
    after = _sqlite_file_gates(source)
    if after != before:
        raise CloneMigrationRehearsalError(
            "production_operational_changed_during_snapshot"
        )
    snapshot_gate = _file_gate(destination)
    with closing(_open_immutable_source(destination)) as snapshot_connection:
        snapshot_state = _source_state(snapshot_connection)
    if snapshot_state != source_state:
        raise CloneMigrationRehearsalError("snapshot_logical_binding_mismatch")
    binding: dict[str, Any] = {
        "binding_version": CONTRACT_VERSION,
        "source_relative_path": DEFAULT_DATABASES["operational"].as_posix(),
        "source_sqlite_gates": before,
        "source_state": source_state,
        "backup_relative_path": SOURCE_SNAPSHOT_NAME,
        "backup_file_gate": snapshot_gate,
        "backup_state": snapshot_state,
        "production_open_mode": "sqlite_uri_mode_ro_immutable_1_query_only",
    }
    binding["binding_digest"] = canonical_digest(binding)
    return binding, _build_plan(destination, production=True)


def _copy_bound_backup(
    backup: Path,
    target: Path,
    binding: Mapping[str, Any],
) -> None:
    if _file_gate(backup) != binding["backup_file_gate"]:
        raise CloneMigrationRehearsalError("bound_backup_changed")
    if target.exists() or target.is_symlink():
        raise CloneMigrationRehearsalError("clone_target_exists")
    shutil.copyfile(backup, target)
    if _file_gate(target) != binding["backup_file_gate"]:
        raise CloneMigrationRehearsalError("bound_backup_copy_mismatch")


def _open_clone_write(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=rw", uri=True)
    _configure_connection(connection)
    return connection


def _verify_source_clone(path: Path) -> dict[str, Any]:
    with closing(_open_immutable_source(path)) as connection:
        return _source_state(connection)


def _verify_migrated_clone(path: Path) -> dict[str, Any]:
    with closing(_open_immutable_source(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        writers = int(
            connection.execute("SELECT COUNT(*) FROM state_writer_leases").fetchone()[0]
        )
        verified = _verify_cognitive_connection(connection)
        object_count = len(_existing_cognitive_objects(connection))
        if (
            integrity != "ok"
            or foreign_keys != 0
            or writers != 0
            or verified["operational_schema_version"] != SCHEMA_VERSION
            or verified["schema_generation"] != COGNITIVE_SCHEMA_GENERATION
            or object_count != EXPECTED_COGNITIVE_OBJECT_COUNT
            or object_count != len(_COGNITIVE_OBJECTS)
        ):
            raise CloneMigrationRehearsalError("migrated_clone_verification_failed")
        return {
            **verified,
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "writer_lease_count": writers,
            "cognitive_object_count": object_count,
        }


def _apply_once(
    path: Path,
    *,
    plan: Mapping[str, Any],
    created_at: str,
    fault: str | None,
) -> dict[str, Any]:
    if fault not in {None, "precommit", "aftercommit"}:
        raise CloneMigrationRehearsalError("fault_mode_invalid")
    apply_count = 0
    committed = False
    with closing(_open_clone_write(path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            apply_count += 1
            _apply_fixed_cognitive_ddl(
                connection,
                plan=plan,
                created_at=created_at,
            )
            if fault == "precommit":
                raise _InjectedCloneFault("precommit")
            connection.commit()
            committed = True
            if fault == "aftercommit":
                raise _InjectedCloneFault("aftercommit")
        except _InjectedCloneFault:
            if not committed:
                connection.rollback()
        except Exception:
            if not committed:
                connection.rollback()
            raise
    reconciliation = _reconcile_database(
        path,
        authority=plan,
        production=True,
    )
    expected = "rolled_back" if fault == "precommit" else "completed"
    if reconciliation["state"] != expected or apply_count != 1:
        raise CloneMigrationRehearsalError("fault_reconciliation_failed")
    verification = (
        _verify_source_clone(path)
        if expected == "rolled_back"
        else _verify_migrated_clone(path)
    )
    return {
        "fault": fault,
        "commit_reached": committed,
        "ddl_apply_count": apply_count,
        "reconcile_replayed_ddl": False,
        "reconciliation_state": reconciliation["state"],
        "reconciliation_reason": reconciliation["reason_code"],
        "verification": verification,
    }


def _core_logical_summary(path: Path) -> dict[str, Any]:
    with closing(_open_immutable_source(path)) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if str(row[0]) not in _COGNITIVE_OBJECT_NAMES
        ]
        table_summaries = {
            table: _table_content_digest(connection, table) for table in tables
        }
        value = {
            "core_schema_digest": _core_schema_snapshot_digest(connection),
            "core_data_digest": _core_data_digest(connection),
            "table_count": len(tables),
            "tables": table_summaries,
        }
    return {**value, "summary_digest": canonical_digest(value)}


def _context_summary(path: Path, *, as_of: str) -> dict[str, Any]:
    # OperationalStateStore intentionally rejects arbitrary non-production,
    # non-temp paths.  Evidence clones live under fixed output, so evaluate the
    # v9 packages through an exact, owned system-temp mirror and prove the
    # retained source did not change during that read.
    source_before = _file_gate(path)
    with tempfile.TemporaryDirectory(
        prefix="yunos-v10-f-context-mirror-",
        ignore_cleanup_errors=False,
    ) as temporary:
        mirror = Path(temporary) / "operational-context.db"
        shutil.copyfile(path, mirror)
        if _file_gate(mirror) != source_before:
            raise CloneMigrationRehearsalError("context_mirror_binding_mismatch")
        service = OperationalStateContextService(mirror)
        projects = service.project_list()
        project_ids = sorted(
            str(item["entity_id"]) for item in projects["items"]
        )
        if not project_ids:
            raise CloneMigrationRehearsalError("resume_project_required")
        now_package = service.now_package(budget_profile="Medium", as_of=as_of)
        resumes = {
            project_id: service.resume_package(
                project_id,
                budget_profile="Medium",
                as_of=as_of,
            )["package_digest"]
            for project_id in project_ids
        }
    if _file_gate(path) != source_before:
        raise CloneMigrationRehearsalError("context_source_changed_during_read")
    value = {
        "project_count": len(project_ids),
        "project_list_digest": projects["list_digest"],
        "now_package_digest": now_package["package_digest"],
        "resume_package_digests": resumes,
    }
    return {
        **value,
        "context_digest": canonical_digest(value),
        "evaluation_boundary": "sha256_bound_owned_temp_mirror",
        "source_file_sha256": source_before["sha256"],
        "ephemeral_mirror_cleaned": True,
    }


def _staging_marker(nonce: str) -> dict[str, Any]:
    value = {
        "marker_version": OWNERSHIP_VERSION,
        "contract_version": CONTRACT_VERSION,
        "purpose": "clone_migration_rehearsal",
        "state": "staging",
        "nonce": nonce,
    }
    value["ownership_identity_digest"] = canonical_digest(
        {key: value[key] for key in ("marker_version", "contract_version", "purpose", "nonce")}
    )
    return value


def _sealed_marker(nonce: str, report_digest: str) -> dict[str, Any]:
    if _SHA256.fullmatch(report_digest or "") is None:
        raise CloneMigrationRehearsalError("report_digest_invalid")
    value = _staging_marker(nonce)
    value["state"] = "sealed"
    value["report_digest"] = report_digest
    value["marker_digest"] = canonical_digest(value)
    return value


def _create_owned_staging(parent: Path) -> tuple[Path, str, tuple[int, int]]:
    nonce = uuid.uuid4().hex
    staging = Path(
        tempfile.mkdtemp(prefix=".v10-f-clone-rehearsal-", dir=parent)
    )
    marker = _staging_marker(nonce)
    _write_new_json(staging / OWNER_MARKER_NAME, marker)
    details = staging.stat()
    return staging, nonce, (int(details.st_dev), int(details.st_ino))


def _validate_owned_directory(
    path: Path, nonce: str, identity: tuple[int, int], *, sealed_digest: str | None
) -> None:
    if _is_reparse(path):
        raise CloneMigrationRehearsalError("owned_directory_reparse_forbidden")
    details = path.stat()
    if (int(details.st_dev), int(details.st_ino)) != identity:
        raise CloneMigrationRehearsalError("owned_directory_identity_changed")
    marker_path = path / OWNER_MARKER_NAME
    if _is_reparse(marker_path) or int(marker_path.stat().st_nlink) != 1:
        raise CloneMigrationRehearsalError("ownership_marker_alias_forbidden")
    actual = _read_json(marker_path)
    expected = (
        _staging_marker(nonce)
        if sealed_digest is None
        else _sealed_marker(nonce, sealed_digest)
    )
    if actual != expected:
        raise CloneMigrationRehearsalError("ownership_marker_invalid")


def _cleanup_owned_staging(
    staging: Path, nonce: str, identity: tuple[int, int]
) -> bool:
    try:
        if not staging.name.startswith(".v10-f-clone-rehearsal-"):
            return False
        _validate_owned_directory(staging, nonce, identity, sealed_digest=None)
    except (OSError, CloneMigrationRehearsalError):
        try:
            marker = _read_json(staging / OWNER_MARKER_NAME)
            details = staging.stat()
            if (
                marker.get("nonce") != nonce
                or marker.get("contract_version") != CONTRACT_VERSION
                or (int(details.st_dev), int(details.st_ino)) != identity
            ):
                return False
        except (OSError, CloneMigrationRehearsalError):
            return False
    shutil.rmtree(staging)
    return True


def _claim_sentry(
    paths: _BoundPaths,
    confirmation: CloneMigrationRehearsalConfirmation,
    *,
    recorded_at: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sentry_version": CONTRACT_VERSION,
        "state": "claimed",
        "recorded_at": recorded_at,
        "confirmation_digest": confirmation.confirmation_digest,
        "confirmed_head": confirmation.confirm_head,
        "user_authorization_sha256": confirmation.user_authorization_sha256,
        "production_operational_relative_path": DEFAULT_DATABASES[
            "operational"
        ].as_posix(),
        "fixed_output_relative_path": f"output/{OUTPUT_NAME}",
        "one_shot": True,
    }
    record["record_digest"] = canonical_digest(record)
    _write_exclusive_json(paths.sentry, record)
    return record


def _materialize_rehearsal(
    paths: _BoundPaths,
    confirmation: CloneMigrationRehearsalConfirmation,
    *,
    sentry: Mapping[str, Any],
    git_before: Mapping[str, Any],
    recorded_at: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    parent_identity = _directory_identity(paths.output_parent)
    data_before = _capture_data_boundary(paths)
    staging, nonce, staging_identity = _create_owned_staging(paths.output_parent)
    published = False
    try:
        backup = staging / SOURCE_SNAPSHOT_NAME
        binding, plan = _snapshot_operational_source(
            paths.databases["operational"], backup
        )
        baseline_logical = _core_logical_summary(backup)
        baseline_context = _context_summary(backup, as_of=recorded_at)

        success_clone = staging / SUCCESS_CLONE_NAME
        _copy_bound_backup(backup, success_clone, binding)
        success = _apply_once(
            success_clone,
            plan=plan,
            created_at=recorded_at,
            fault=None,
        )
        success_logical = _core_logical_summary(success_clone)
        success_context = _context_summary(success_clone, as_of=recorded_at)

        precommit_clone = staging / PRECOMMIT_CLONE_NAME
        _copy_bound_backup(backup, precommit_clone, binding)
        precommit = _apply_once(
            precommit_clone,
            plan=plan,
            created_at=recorded_at,
            fault="precommit",
        )
        precommit_logical = _core_logical_summary(precommit_clone)
        precommit_context = _context_summary(precommit_clone, as_of=recorded_at)

        aftercommit_clone = staging / AFTERCOMMIT_CLONE_NAME
        _copy_bound_backup(backup, aftercommit_clone, binding)
        aftercommit = _apply_once(
            aftercommit_clone,
            plan=plan,
            created_at=recorded_at,
            fault="aftercommit",
        )
        aftercommit_logical = _core_logical_summary(aftercommit_clone)
        aftercommit_context = _context_summary(aftercommit_clone, as_of=recorded_at)

        restore_clone = staging / RESTORE_CLONE_NAME
        _copy_bound_backup(backup, restore_clone, binding)
        restored = _verify_source_clone(restore_clone)
        restore_logical = _core_logical_summary(restore_clone)
        restore_context = _context_summary(restore_clone, as_of=recorded_at)

        comparisons = {
            "success_core_schema_unchanged": (
                success_logical["core_schema_digest"]
                == baseline_logical["core_schema_digest"]
            ),
            "success_core_data_unchanged": (
                success_logical["summary_digest"]
                == baseline_logical["summary_digest"]
            ),
            "success_now_resume_unchanged": (
                success_context["context_digest"]
                == baseline_context["context_digest"]
            ),
            "precommit_core_unchanged": (
                precommit_logical["summary_digest"]
                == baseline_logical["summary_digest"]
            ),
            "precommit_now_resume_unchanged": (
                precommit_context["context_digest"]
                == baseline_context["context_digest"]
            ),
            "aftercommit_core_unchanged": (
                aftercommit_logical["summary_digest"]
                == baseline_logical["summary_digest"]
            ),
            "aftercommit_now_resume_unchanged": (
                aftercommit_context["context_digest"]
                == baseline_context["context_digest"]
            ),
            "restore_core_unchanged": (
                restore_logical["summary_digest"]
                == baseline_logical["summary_digest"]
            ),
            "restore_now_resume_unchanged": (
                restore_context["context_digest"]
                == baseline_context["context_digest"]
            ),
        }
        if not all(comparisons.values()):
            raise CloneMigrationRehearsalError("v9_semantics_changed")

        data_after = _capture_data_boundary(paths)
        if data_after != data_before:
            raise CloneMigrationRehearsalError("production_data_boundary_changed")
        git_after = _capture_git_state(paths.project_root)
        if git_after != git_before:
            raise CloneMigrationRehearsalError("git_or_stash_state_changed")

        report: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status": "clone_migration_rehearsal_go",
            "recorded_at": recorded_at,
            "production_mode": paths.production,
            "one_shot_sentry_digest": sentry["record_digest"],
            "confirmation_digest": confirmation.confirmation_digest,
            "source_open_mode": "readonly_immutable_query_only",
            "production_write_performed": False,
            "service_stop_performed": False,
            "git_mutation_performed": False,
            "stash_mutation_performed": False,
            "fixed_output_relative_path": f"output/{OUTPUT_NAME}",
            "migration": {
                "source_core_schema_version": SCHEMA_VERSION,
                "target_cognitive_schema_generation": (
                    COGNITIVE_SCHEMA_GENERATION
                ),
                "source_schema_digest": plan["source_schema_digest"],
                "target_schema_digest": plan["target_schema_digest"],
                "expected_cognitive_object_count": (
                    EXPECTED_COGNITIVE_OBJECT_COUNT
                ),
                "plan_digest": plan["plan_digest"],
            },
            "bound_backup": binding,
            "baseline": {
                "logical_summary": baseline_logical,
                "context_summary": baseline_context,
            },
            "success": {
                "relative_path": SUCCESS_CLONE_NAME,
                "migration": success,
                "logical_summary": success_logical,
                "context_summary": success_context,
            },
            "fault_injection": {
                "precommit": {
                    "relative_path": PRECOMMIT_CLONE_NAME,
                    "result": precommit,
                    "logical_summary": precommit_logical,
                    "context_summary": precommit_context,
                },
                "aftercommit": {
                    "relative_path": AFTERCOMMIT_CLONE_NAME,
                    "result": aftercommit,
                    "logical_summary": aftercommit_logical,
                    "context_summary": aftercommit_context,
                },
            },
            "restore": {
                "relative_path": RESTORE_CLONE_NAME,
                "bound_backup_revalidated": True,
                "rollback_policy": (
                    "no_reverse_ddl_restore_bound_backup_to_new_owned_clone"
                ),
                "aftercommit_clone_retained": True,
                "source_schema_restored": restored,
                "logical_summary": restore_logical,
                "context_summary": restore_context,
            },
            "comparisons": comparisons,
            "production_boundary": {
                "before": data_before,
                "after": data_after,
                "other_four_databases_unchanged": all(
                    data_before["databases"][label]
                    == data_after["databases"][label]
                    for label in DEFAULT_DATABASES
                    if label != "operational"
                ),
                "operational_database_unchanged": (
                    data_before["databases"]["operational"]
                    == data_after["databases"]["operational"]
                ),
                "assets_unchanged": data_before["assets"] == data_after["assets"],
            },
            "git_boundary": {"before": git_before, "after": git_after},
            "retention": {
                "retained_for_evidence": True,
                "cleanup_performed": False,
                "owned_clone_relative_paths": [
                    SOURCE_SNAPSHOT_NAME,
                    SUCCESS_CLONE_NAME,
                    PRECOMMIT_CLONE_NAME,
                    AFTERCOMMIT_CLONE_NAME,
                    RESTORE_CLONE_NAME,
                ],
            },
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
        if not all(
            (
                report["production_boundary"]["other_four_databases_unchanged"],
                report["production_boundary"]["operational_database_unchanged"],
                report["production_boundary"]["assets_unchanged"],
            )
        ):
            raise CloneMigrationRehearsalError("production_boundary_not_proven")
        report["report_digest"] = canonical_digest(report)
        _write_new_json(staging / REPORT_NAME, report)
        marker_path = staging / OWNER_MARKER_NAME
        marker_path.write_bytes(_canonical_bytes(_sealed_marker(nonce, report["report_digest"])))
        _validate_owned_directory(
            staging,
            nonce,
            staging_identity,
            sealed_digest=report["report_digest"],
        )
        _assert_directory_identity(paths.output_parent, parent_identity)
        if paths.output.exists() or paths.output.is_symlink():
            raise CloneMigrationRehearsalError("fixed_output_exists")
        staging.rename(paths.output)
        published = True
        _assert_directory_identity(paths.output_parent, parent_identity)
        _validate_owned_directory(
            paths.output,
            nonce,
            staging_identity,
            sealed_digest=report["report_digest"],
        )
        return report
    finally:
        if not published and staging.exists():
            _cleanup_owned_staging(staging, nonce, staging_identity)


def _run_bound_rehearsal(
    project_root: Path,
    *,
    confirmation: CloneMigrationRehearsalConfirmation,
    production: bool,
) -> dict[str, Any]:
    selected_root = _validate_execution_root(project_root, production=production)
    confirmation.validate(production=production)
    if os.environ.get(AUTHORIZATION_ENV, "") != AUTHORIZATION_ENV_VALUE:
        raise CloneMigrationRehearsalError("authorization_environment_gate_closed")
    paths = _bound_paths(selected_root, production=production)
    git_before = _capture_git_state(paths.project_root)
    _validate_git_state(git_before, confirmation)
    recorded_at = normalize_timestamp(_utc_now())
    sentry = _claim_sentry(
        paths,
        confirmation,
        recorded_at=recorded_at,
    )
    try:
        return _materialize_rehearsal(
            paths,
            confirmation,
            sentry=sentry,
            git_before=git_before,
            recorded_at=recorded_at,
        )
    except Exception as exc:
        failure: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status": "clone_migration_rehearsal_failed",
            "reason_code": _reason(exc),
            "recorded_at": _utc_now(),
            "one_shot_sentry_digest": sentry["record_digest"],
            "fixed_output_retained": paths.output.exists(),
            "staging_cleanup_policy": "owned_staging_only",
            "production_write_performed": False,
            "service_stop_performed": False,
            "git_mutation_performed": False,
            "stash_mutation_performed": False,
        }
        failure["failure_digest"] = canonical_digest(failure)
        try:
            _write_exclusive_json(paths.failure, failure)
        except CloneMigrationRehearsalError:
            raise CloneMigrationRehearsalError(
                "failure_record_write_failed"
            ) from None
        raise CloneMigrationRehearsalError(failure["reason_code"]) from None


def _install_production_api(module_file: str):
    fixed_root = Path(module_file).resolve().parents[1]
    bound_executor = _run_bound_rehearsal

    def run_production_clone_migration_rehearsal(
        *, confirmation: CloneMigrationRehearsalConfirmation
    ) -> dict[str, Any]:
        return bound_executor(
            fixed_root,
            confirmation=confirmation,
            production=True,
        )

    return run_production_clone_migration_rehearsal


run_production_clone_migration_rehearsal = _install_production_api(__file__)
del _install_production_api


def _run_clone_migration_rehearsal_for_test(
    *,
    _test_project_root: Path,
    confirmation: CloneMigrationRehearsalConfirmation,
) -> dict[str, Any]:
    selected = _validate_execution_root(_test_project_root, production=False)
    return _run_bound_rehearsal(
        selected,
        confirmation=confirmation,
        production=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-one-shot", action="store_true")
    parser.add_argument("--confirm-readonly-production", action="store_true")
    parser.add_argument("--confirm-disposable-clones", action="store_true")
    parser.add_argument("--confirm-no-service-stop", action="store_true")
    parser.add_argument("--confirm-no-git-or-stash-mutation", action="store_true")
    parser.add_argument("--confirm-head")
    parser.add_argument(
        "--confirm-user-authorization-sha256",
        choices=(USER_AUTHORIZATION_SHA256,),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    preview = {
        "contract_version": CONTRACT_VERSION,
        "mode": "execute" if args.execute else "dry_run",
        "fixed_output_relative_path": f"output/{OUTPUT_NAME}",
        "fixed_sentry_relative_path": f"output/{SENTRY_NAME}",
        "production_operational_relative_path": DEFAULT_DATABASES[
            "operational"
        ].as_posix(),
        "required_environment": (
            f"{AUTHORIZATION_ENV}={AUTHORIZATION_ENV_VALUE}"
        ),
        "user_authorization_sha256": USER_AUTHORIZATION_SHA256,
        "path_override_supported": False,
        "database_override_supported": False,
        "dependency_injection_supported": False,
        "production_write_allowed": False,
        "service_stop_allowed": False,
        "git_or_stash_mutation_allowed": False,
    }
    if not args.execute:
        print(canonical_json(preview))
        return 0
    try:
        confirmation = CloneMigrationRehearsalConfirmation(
            production=True,
            confirm_one_shot=args.confirm_one_shot,
            confirm_readonly_production=args.confirm_readonly_production,
            confirm_disposable_clones=args.confirm_disposable_clones,
            confirm_no_service_stop=args.confirm_no_service_stop,
            confirm_no_git_or_stash_mutation=(
                args.confirm_no_git_or_stash_mutation
            ),
            confirm_head=args.confirm_head or "",
            user_authorization_sha256=(
                args.confirm_user_authorization_sha256 or ""
            ),
        )
        result = run_production_clone_migration_rehearsal(
            confirmation=confirmation
        )
    except Exception as exc:
        print(
            canonical_json(
                {
                    **preview,
                    "status": "refused",
                    "reason_code": _reason(exc),
                }
            )
        )
        return 1
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
