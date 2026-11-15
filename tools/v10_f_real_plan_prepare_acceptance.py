"""Fixed one-shot v10-F production acceptance for real Plan and Prepare.

The production entry is intentionally non-injectable.  It reads the existing
v9 Project Resume Package for ``project:ayun-english-core`` and appends only
bounded cognitive planning records to the schema-10001 Operational State DB.
It never opens the external project repository, invokes a Provider, proposes
or executes a tool, creates a Delegation/Outcome/Approval, or accepts its own
result.  Tests use the private target seam with disposable databases only.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_execution_models import (  # noqa: E402
    CognitiveRun,
    _create_cognitive_checkpoint,
    _create_cognitive_run,
)
from memory.cognitive_execution_store import (  # noqa: E402
    CognitiveExecutionStore,
    _empty_model_budget_state_payload,
)
from memory.cognitive_objective import (  # noqa: E402
    AcceptancePolicy,
    AuthorityRequest,
    ObjectiveContract,
    ObjectiveSourceAnchor,
    build_objective_contract,
)
from memory.cognitive_orchestration import (  # noqa: E402
    ContextBudget,
    ContextItem,
    ContextSource,
    assemble_cognitive_context,
)
from memory.cognitive_planning import (  # noqa: E402
    PlanEdge,
    PlanGraph,
    PlanNode,
    build_plan_graph,
    revise_plan,
)
from memory.cognitive_planning_store import CognitivePlanningStore  # noqa: E402
from memory.cognitive_provider_store import CognitiveProviderStore  # noqa: E402
from memory.cognitive_state_migration import (  # noqa: E402
    verify_cognitive_state_schema,
)
from memory.cognitive_state_store import CognitiveStateStore  # noqa: E402
from memory.db_path_policy import is_reparse_point  # noqa: E402
from memory.operational_state_context import (  # noqa: E402
    RESUME_PACKAGE_VERSION,
    OperationalStateContextService,
)
from memory.operational_state_models import (  # noqa: E402
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
)
from memory.operational_state_store import (  # noqa: E402
    PRODUCTION_WRITE_ENV,
    OperationalStateStore,
)


ACCEPTANCE_VERSION = "v10-f-real-plan-prepare-acceptance-1"
PROJECT_ID = "project:ayun-english-core"
EXPECTED_BASELINE = "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53"
EXPECTED_PROJECT_VERSION = "v0.1.1"
EXPECTED_EXTERNAL_POLICY = "read_only_observation_only"
USER_ACTOR_ID = "user:yunos"
SYSTEM_ACTOR_ID = "system:yunos-v10-f-plan-prepare"
PLANNER_ACTOR_ID = "agent:yunos-v10-f-planner"
USER_AUTHORIZATION_SHA256 = (
    "ff968831ddaf4e694673272ede56dc7857286b77c1cfddbd908559385282ae11"
)
PERSISTED_PROVIDER_ID = "deepseek"
PERSISTED_PROVIDER_VERSION = 1

PRODUCTION_OPERATIONAL_DB = (
    PROJECT_ROOT / "data" / "operational" / "ayunos_operational_state.db"
).absolute()
PRODUCTION_RUNTIME_DB = (
    PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
).absolute()
PRODUCTION_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "v10-f-production-adoption"
    / "real-plan-prepare-acceptance"
).absolute()
SENTRY_NAME = "one-shot-plan-prepare-sentry.json"
RESULT_NAME = "real-plan-prepare-acceptance.json"
FAILURE_NAME = "real-plan-prepare-failure.json"

_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{2,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_VERSION = re.compile(r"(?i)(?:v10-g|v10\.1|v11)\b")
_FORBIDDEN_EXTERNAL_TEXT = "e:\\ayunprojects"

_OPERATIONAL_TABLES = (
    "authorization_artifacts",
    "capability_registry",
    "cognitive_run_checkpoints",
    "cognitive_runs",
    "cognitive_schema",
    "context_bindings",
    "delegation_contracts",
    "objective_contracts",
    "operational_schema",
    "outcome_records",
    "plan_edges",
    "plan_graphs",
    "plan_nodes",
    "plan_revisions",
    "provider_registry",
    "state_approvals",
    "state_candidates",
    "state_conflicts",
    "state_entities",
    "state_events",
    "state_lease_events",
    "state_projections",
    "state_receipts",
    "state_snapshots",
    "state_source_anchors",
    "state_verifications",
    "state_writer_leases",
    "tool_executions",
    "tool_proposals",
)
EXPECTED_OPERATIONAL_DELTA = {name: 0 for name in _OPERATIONAL_TABLES}
EXPECTED_OPERATIONAL_DELTA.update(
    {
        "cognitive_run_checkpoints": 4,
        "cognitive_runs": 2,
        "context_bindings": 2,
        "objective_contracts": 2,
        "plan_edges": 8,
        "plan_graphs": 4,
        "plan_nodes": 12,
        "plan_revisions": 4,
        "state_candidates": 6,
        "state_entities": 8,
        "state_events": 8,
        "state_lease_events": 2,
        "state_projections": 8,
        "state_receipts": 8,
        "state_source_anchors": 5,
        "state_verifications": 2,
    }
)

_PROTECTED_ZERO_DELTA_TABLES = (
    "authorization_artifacts",
    "capability_registry",
    "delegation_contracts",
    "outcome_records",
    "provider_registry",
    "state_approvals",
    "tool_executions",
    "tool_proposals",
)
_RUNTIME_TABLES = (
    "runtime_schema",
    "runtime_conversations",
    "runtime_request_artifacts",
    "runtime_model_runs",
    "runtime_messages",
    "runtime_errors",
    "runtime_run_events",
    "runtime_receipts",
)


class PlanPrepareAcceptanceError(RuntimeError):
    """Sanitized, stable acceptance failure."""

    def __init__(self, reason_code: str) -> None:
        safe = (
            reason_code
            if isinstance(reason_code, str) and _SAFE_REASON.fullmatch(reason_code)
            else "real_plan_prepare_acceptance_failed"
        )
        self.reason_code = safe
        super().__init__(safe)


def _fail(reason_code: str) -> None:
    raise PlanPrepareAcceptanceError(reason_code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _stamp(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256": None}
    stat = path.stat()
    return {"exists": True, "size": stat.st_size, "sha256": _sha256_file(path)}


def _reject_reparse_chain(path: Path, reason_code: str) -> None:
    current = path.absolute()
    while True:
        try:
            if (current.exists() or current.is_symlink()) and is_reparse_point(current):
                _fail(reason_code)
        except OSError:
            _fail(reason_code)
        if current.parent == current:
            return
        current = current.parent


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _path_is_within(candidate: Path, root: Path) -> bool:
    key = _path_key(candidate)
    boundary = _path_key(root).rstrip("\\/")
    return key == boundary or key.startswith(boundary + os.sep)


def _fixed_shared_production_paths() -> tuple[Path, Path, Path]:
    """Rebuild the three formal identities from this file, never globals."""

    root = Path(_fixed_shared_production_paths.__code__.co_filename).resolve(
        strict=True
    ).parents[1]
    return (
        (root / "data" / "operational" / "ayunos_operational_state.db").absolute(),
        (root / "data" / "runtime" / "ayunos_runtime.db").absolute(),
        (
            root
            / "output"
            / "v10-f-production-adoption"
            / "real-plan-prepare-acceptance"
        ).absolute(),
    )


def _shared_path_lexical_gate(value: Any) -> Path:
    if type(value) is not type(Path()):
        _fail("invalid_acceptance_target")
    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        _fail("shared_target_path_invalid")
    windows_text = raw.replace("/", "\\")
    folded = windows_text.casefold()
    if (
        folded.startswith("\\\\?\\")
        or folded.startswith("\\\\.\\")
        or folded.startswith("\\??\\")
        or folded.startswith("\\\\")
    ):
        _fail("shared_target_windows_namespace_forbidden")
    drive, tail = os.path.splitdrive(raw)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
        _fail("shared_target_drive_forbidden")
    if ":" in tail:
        _fail("shared_target_ads_forbidden")
    if ".." in Path(raw).parts:
        _fail("shared_target_traversal_forbidden")
    return Path(os.path.abspath(raw))


def _same_file_fail_closed(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        _fail("shared_target_identity_unverifiable")


def _preflight_shared_temp_path(value: Any) -> Path:
    target = _shared_path_lexical_gate(value)
    for formal in _fixed_shared_production_paths():
        if _path_key(target) == _path_key(formal):
            _fail("shared_core_production_path_forbidden")
    temp_root = Path(os.path.abspath(tempfile.gettempdir()))
    target_drive, _ = os.path.splitdrive(str(target))
    temp_drive, _ = os.path.splitdrive(str(temp_root))
    if target_drive.casefold() != temp_drive.casefold():
        _fail("shared_target_drive_forbidden")
    try:
        within_temp = os.path.commonpath(
            (_path_key(target), _path_key(temp_root))
        ) == _path_key(temp_root)
    except ValueError:
        within_temp = False
    if target == temp_root or not within_temp:
        _fail("shared_target_outside_system_temp")
    return target


def _validate_shared_temp_path(value: Any, *, kind: str) -> Path:
    target = _preflight_shared_temp_path(value)
    temp_root = Path(os.path.abspath(tempfile.gettempdir()))
    if not target.parent.is_dir():
        _fail("shared_target_parent_missing")
    _reject_reparse_chain(temp_root, "shared_target_reparse_forbidden")
    _reject_reparse_chain(target.parent, "shared_target_reparse_forbidden")
    if target.exists() or target.is_symlink():
        _reject_reparse_chain(target, "shared_target_reparse_forbidden")
    real_root = os.path.normcase(os.path.realpath(temp_root))
    real_target = os.path.normcase(os.path.realpath(target))
    try:
        real_within_temp = os.path.commonpath(
            (real_target, real_root)
        ) == real_root
    except ValueError:
        real_within_temp = False
    if real_target == real_root or not real_within_temp:
        _fail("shared_target_alias_escape_forbidden")
    for formal in _fixed_shared_production_paths():
        if (
            _path_key(target) == _path_key(formal)
            or os.path.normcase(os.path.realpath(target))
            == os.path.normcase(os.path.realpath(formal))
            or _same_file_fail_closed(target, formal)
            or (
                target.name.casefold() == formal.name.casefold()
                and _same_file_fail_closed(target.parent, formal.parent)
            )
        ):
            _fail("shared_core_production_path_forbidden")
    if kind == "database":
        if not target.is_file():
            _fail("shared_target_database_missing")
        try:
            if target.stat().st_nlink != 1:
                _fail("shared_target_database_alias_forbidden")
        except OSError:
            _fail("shared_target_identity_unverifiable")
    elif kind == "output":
        if target.exists() and not target.is_dir():
            _fail("shared_target_output_not_directory")
    else:
        _fail("invalid_acceptance_target")
    return target


def _validate_shared_target_fields(
    target: Any,
) -> tuple[Path, Path, Path]:
    if type(target) is not _AcceptanceTarget:
        _fail("exact_acceptance_target_required")
    try:
        production = target.production
        operational_value = target.operational_db
        runtime_value = target.runtime_db
        output_value = target.output_root
    except AttributeError:
        _fail("invalid_acceptance_target")
    if production is not False:
        _fail("shared_core_production_target_forbidden")
    raw_values = (operational_value, runtime_value, output_value)
    if any(type(value) is not type(Path()) for value in raw_values):
        _fail("invalid_acceptance_target")
    if any(
        _FORBIDDEN_EXTERNAL_TEXT in _path_key(path) for path in raw_values
    ):
        _fail("external_project_path_forbidden")
    preflight_values = tuple(
        _preflight_shared_temp_path(value) for value in raw_values
    )
    operational_db = _validate_shared_temp_path(
        preflight_values[0], kind="database"
    )
    runtime_db = _validate_shared_temp_path(preflight_values[1], kind="database")
    output_root = _validate_shared_temp_path(preflight_values[2], kind="output")
    if len({_path_key(path) for path in (operational_db, runtime_db, output_root)}) != 3:
        _fail("acceptance_path_overlap")
    if _path_is_within(operational_db, output_root) or _path_is_within(
        runtime_db, output_root
    ):
        _fail("acceptance_path_overlap")
    return operational_db, runtime_db, output_root


def _rebuild_shared_target(target: Any) -> _AcceptanceTarget:
    if type(target) is not _AcceptanceTarget:
        _fail("exact_acceptance_target_required")
    operational_db, runtime_db, output_root = _validate_shared_target_fields(target)
    return _AcceptanceTarget(
        operational_db=operational_db,
        runtime_db=runtime_db,
        output_root=output_root,
        production=False,
    )


@dataclass(frozen=True, slots=True)
class _AcceptanceTarget:
    operational_db: Path
    runtime_db: Path
    output_root: Path
    production: bool = False

    def __post_init__(self) -> None:
        operational_db, runtime_db, output_root = _validate_shared_target_fields(self)
        object.__setattr__(self, "operational_db", operational_db)
        object.__setattr__(self, "runtime_db", runtime_db)
        object.__setattr__(self, "output_root", output_root)


@dataclass(frozen=True, slots=True)
class _PinnedOutput:
    path: Path
    device: int
    inode: int
    anchor_path: Path
    anchor_device: int
    anchor_inode: int
    anchor_descriptor: int


@dataclass(frozen=True)
class _Contracts:
    objectives: Mapping[str, ObjectiveContract]
    plans: Mapping[str, tuple[PlanGraph, PlanGraph]]
    contexts: Mapping[str, Any]
    anchors: Mapping[str, ObjectiveSourceAnchor]
    authority: AuthorityArtifact
    run_ids: Mapping[str, str]
    times: Mapping[str, str]


def _rebuild_pinned_output(output: Any) -> _PinnedOutput:
    if type(output) is not _PinnedOutput:
        _fail("exact_pinned_output_required")
    try:
        raw_values = (
            output.path,
            output.device,
            output.inode,
            output.anchor_path,
            output.anchor_device,
            output.anchor_inode,
            output.anchor_descriptor,
        )
    except AttributeError:
        _fail("invalid_pinned_output")
    path, device, inode, anchor_path, anchor_device, anchor_inode, descriptor = (
        raw_values
    )
    if type(path) is not type(Path()) or type(anchor_path) is not type(Path()):
        _fail("invalid_pinned_output")
    if any(
        type(value) is not int or value < 0
        for value in (device, inode, anchor_device, anchor_inode, descriptor)
    ):
        _fail("invalid_pinned_output")
    path = _validate_shared_temp_path(path, kind="output")
    anchor_path = _validate_shared_temp_path(anchor_path, kind="database")
    if anchor_path != path / ".output-root-anchor":
        _fail("output_anchor_identity_changed")
    try:
        path_stat = path.stat()
        anchor_stat = anchor_path.stat()
        descriptor_stat = os.fstat(descriptor)
    except OSError:
        _fail("output_root_identity_changed")
    if not path.is_dir() or (path_stat.st_dev, path_stat.st_ino) != (
        device,
        inode,
    ):
        _fail("output_root_identity_changed")
    if (
        not anchor_path.is_file()
        or (anchor_stat.st_dev, anchor_stat.st_ino)
        != (anchor_device, anchor_inode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino)
        != (anchor_device, anchor_inode)
    ):
        _fail("output_anchor_identity_changed")
    return _PinnedOutput(
        path=path,
        device=device,
        inode=inode,
        anchor_path=anchor_path,
        anchor_device=anchor_device,
        anchor_inode=anchor_inode,
        anchor_descriptor=descriptor,
    )


def _pin_new_output_root(target: _AcceptanceTarget) -> _PinnedOutput:
    target = _rebuild_shared_target(target)
    _reject_reparse_chain(target.output_root.parent, "output_parent_reparse_forbidden")
    target.output_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.output_root.mkdir(exist_ok=False)
    except FileExistsError:
        _fail("plan_prepare_output_root_exists_do_not_rerun")
    _reject_reparse_chain(target.output_root, "output_root_reparse_forbidden")
    stat = target.output_root.stat()
    anchor_path = target.output_root / ".output-root-anchor"
    descriptor = os.open(
        anchor_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(descriptor, b"YunOS v10-F Plan/Prepare output anchor\n")
        os.fsync(descriptor)
        anchor_stat = os.fstat(descriptor)
        path_stat = anchor_path.stat()
        if (anchor_stat.st_dev, anchor_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            _fail("output_anchor_identity_changed")
        return _PinnedOutput(
            target.output_root,
            stat.st_dev,
            stat.st_ino,
            anchor_path,
            anchor_stat.st_dev,
            anchor_stat.st_ino,
            descriptor,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _assert_output_identity(output: _PinnedOutput) -> None:
    output = _rebuild_pinned_output(output)
    try:
        stat = output.path.stat()
        anchor_stat = output.anchor_path.stat()
        descriptor_stat = os.fstat(output.anchor_descriptor)
    except OSError:
        _fail("output_root_identity_changed")
    if not output.path.is_dir() or (stat.st_dev, stat.st_ino) != (
        output.device,
        output.inode,
    ):
        _fail("output_root_identity_changed")
    if (
        output.anchor_path.parent != output.path
        or not output.anchor_path.is_file()
        or (anchor_stat.st_dev, anchor_stat.st_ino)
        != (output.anchor_device, output.anchor_inode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino)
        != (output.anchor_device, output.anchor_inode)
    ):
        _fail("output_anchor_identity_changed")
    _reject_reparse_chain(output.path, "output_root_reparse_forbidden")


def _write_exclusive(output: _PinnedOutput, name: str, payload: Mapping[str, Any]) -> None:
    output = _rebuild_pinned_output(output)
    _assert_output_identity(output)
    if type(name) is not str or Path(name).name != name or name in {".", ".."}:
        _fail("invalid_output_name")
    path = output.path / name
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    _assert_output_identity(output)


def _write_result_exclusive(
    output: _PinnedOutput, payload: Mapping[str, Any]
) -> None:
    output = _rebuild_pinned_output(output)
    _write_exclusive(output, RESULT_NAME, payload)


def _release_output_anchor(output: _PinnedOutput) -> None:
    output = _rebuild_pinned_output(output)
    _assert_output_identity(output)
    os.close(output.anchor_descriptor)


def _database_rows_digest(connection: sqlite3.Connection, table: str) -> str:
    quoted = table.replace('"', '""')
    columns = [
        row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')
    ]
    rows = [
        {column: row[column] for column in columns}
        for row in connection.execute(f'SELECT * FROM "{quoted}" ORDER BY rowid')
    ]
    return canonical_digest(rows)


def _operational_snapshot(target: _AcceptanceTarget) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    path = target.operational_db
    if not path.is_file():
        _fail("operational_database_missing")
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            _fail("operational_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("operational_foreign_key_check_failed")
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        if tables != tuple(sorted(_OPERATIONAL_TABLES)):
            _fail("operational_schema_table_set_mismatch")
        counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for table in tables
        }
        protected = {
            table: _database_rows_digest(connection, table)
            for table in _PROTECTED_ZERO_DELTA_TABLES
        }
        writer_rows = counts["state_writer_leases"]
    return {
        "counts": counts,
        "protected_digests": protected,
        "writer_leases": writer_rows,
        "snapshot_digest": canonical_digest(
            {"counts": counts, "protected_digests": protected}
        ),
    }


def _runtime_snapshot(target: _AcceptanceTarget) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    path = target.runtime_db
    if not path.is_file():
        _fail("runtime_database_missing")
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            _fail("runtime_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("runtime_foreign_key_check_failed")
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        if tables != tuple(sorted(_RUNTIME_TABLES)):
            _fail("runtime_schema_table_set_mismatch")
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
        digests = {table: _database_rows_digest(connection, table) for table in tables}
    files = {
        "database": _file_gate(path),
        "wal": _file_gate(Path(str(path) + "-wal")),
        "shm": _file_gate(Path(str(path) + "-shm")),
    }
    return {
        "counts": counts,
        "table_digests": digests,
        "files": files,
        "snapshot_digest": canonical_digest(
            {"counts": counts, "table_digests": digests, "files": files}
        ),
    }


def _project_and_resume_gate(
    target: _AcceptanceTarget, *, as_of: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = _rebuild_shared_target(target)
    verify_cognitive_state_schema(target.operational_db)
    store = OperationalStateStore(target.operational_db)
    projection = store.get_projection(PROJECT_ID)
    if projection is None:
        _fail("ayun_english_project_missing")
    fields = projection.get("fields", {})
    if (
        projection.get("entity_type") != "project"
        or projection.get("state") != "active"
        or fields.get("authoritative_baseline") != EXPECTED_BASELINE
        or fields.get("current_version") != EXPECTED_PROJECT_VERSION
        or fields.get("external_repository_policy") != EXPECTED_EXTERNAL_POLICY
        or fields.get("source_repository_modified_by_yunos") is not False
    ):
        _fail("ayun_english_project_boundary_mismatch")
    resume = OperationalStateContextService(target.operational_db).resume_package(
        PROJECT_ID, budget_profile="Large", as_of=as_of
    )
    if (
        resume.get("package_version") != RESUME_PACKAGE_VERSION
        or resume.get("project_id") != PROJECT_ID
        or resume.get("source_state") != "live"
        or resume.get("cache_control") != "no-store"
        or resume.get("cache_stale") is not False
        or resume.get("authoritative_baseline") != EXPECTED_BASELINE
        or resume.get("current_version") != EXPECTED_PROJECT_VERSION
        or not _SHA256.fullmatch(str(resume.get("package_digest", "")))
    ):
        _fail("ayun_english_resume_package_mismatch")
    return projection, resume


def _provider_gate(target: _AcceptanceTarget) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    try:
        profile = CognitiveProviderStore(target.operational_db).get_provider(
            PERSISTED_PROVIDER_ID, PERSISTED_PROVIDER_VERSION
        )
    except OperationalStateError:
        _fail("persisted_provider_profile_missing_or_invalid")
    if (
        profile.get("provider_id") != PERSISTED_PROVIDER_ID
        or profile.get("profile_version") != PERSISTED_PROVIDER_VERSION
        or not _SHA256.fullmatch(str(profile.get("profile_digest", "")))
        or profile.get("manual_selection_only") is not True
        or profile.get("grants_authority") is not False
        or profile.get("execution_allowed") is not False
    ):
        _fail("persisted_provider_profile_boundary_mismatch")
    return profile


def _objective_anchor(mode: str, resume: Mapping[str, Any], observed_at: str) -> SourceAnchor:
    evidence = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "mode": mode,
        "project_id": PROJECT_ID,
        "resume_package_digest": resume["package_digest"],
        "authoritative_baseline": EXPECTED_BASELINE,
        "current_version": EXPECTED_PROJECT_VERSION,
        "external_repository_policy": EXPECTED_EXTERNAL_POLICY,
        "user_authorization_sha256": USER_AUTHORIZATION_SHA256,
        "external_repository_access_allowed": False,
        "provider_calls_allowed": False,
        "tool_execution_allowed": False,
    }
    return ObjectiveSourceAnchor(
        anchor_id=f"anchor:user:v10-f-english-{mode}",
        source_kind="user_declaration",
        locator=f"acceptance:v10-f-real-{mode}",
        evidence_digest=canonical_digest(evidence),
        observed_at=observed_at,
        privacy_scope="private_metadata",
    )


def _build_objective(mode: str, anchor: SourceAnchor) -> ObjectiveContract:
    if mode not in {"plan", "prepare"}:
        _fail("invalid_plan_prepare_mode")
    read_root = "operational://project:ayun-english-core"
    goal = (
        "基于真实 v9 Resume Package 形成可验证方案，不执行项目变更。"
        if mode == "plan"
        else "基于真实 v9 Resume Package 形成变更预览、授权需求、验收与回滚草案。"
    )
    return build_objective_contract(
        objective_id=f"objective:v10-f-english-{mode}-acceptance",
        user_utterance=(
            "为 Ayun English Core 生成真实规划。"
            if mode == "plan"
            else "为 Ayun English Core 生成真实准备预览。"
        ),
        interpreted_goal=goal,
        project_id=PROJECT_ID,
        source_anchors=(anchor,),
        success_criteria=(
            "目标绑定真实项目基线和 Resume Package digest。",
            "Plan Graph 具有依赖、验证和确定性 revision。",
            "不访问或修改外部项目仓库。",
            "系统验证与用户接受保持分离。",
        ),
        prohibited_outcomes=(
            "不得读取或写入外部 Ayun English 项目仓库。",
            "不得调用 Provider、工具、网络或 Runtime。",
            "不得创建 Delegation、Outcome、Approval 或 Authorization Artifact。",
            "不得更新项目完成状态或声称用户已接受。",
        ),
        agency_level=mode,
        agency_selected_by="user",
        maximum_authorized_agency_level=mode,
        agency_selection_source_anchor_id=anchor.anchor_id,
        cognitive_stance="continue",
        risk_tolerance="low",
        resource_constraints=(
            "只读 v9 Operational State Resume Package。",
            "只允许 schema-10001 Operational State 加法式认知记录。",
            "模型调用、工具提案、工具执行、网络和外部仓库访问均为零。",
        ),
        assumptions=(
            "生产迁移已完成且 Provider Registry 已由此前节点安装。",
            "Ayun English Core 外部仓库继续保持 read_only_observation_only。",
        ),
        acceptance_policy=AcceptancePolicy(
            mode="explicit_user",
            automatic_allowed=False,
            requires_user_event=True,
            deterministic_criteria=(
                "system verification passed",
                "no provider or tool execution",
            ),
            maximum_risk="low",
        ),
        authority_request=AuthorityRequest(
            requested=False,
            read_roots=(read_root,),
            write_roots=(),
            capabilities=(),
            commands=(),
            risk_ceiling="low",
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            network_permission=False,
            database_permission="readonly",
        ),
    )


def _node(
    *,
    node_id: str,
    objective: ObjectiveContract,
    title: str,
    purpose: str,
    dependencies: Sequence[str] = (),
    node_kind: str = "action",
    authority_requirement: str = "none",
    read_resume: bool = False,
) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        objective_id=objective.objective_id,
        title=title,
        purpose=purpose,
        dependencies=tuple(dependencies),
        node_kind=node_kind,
        capability_requirements=(),
        read_scope=(
            ("operational://project:ayun-english-core/resume",)
            if read_resume
            else ()
        ),
        write_scope=(),
        risk="low",
        reversible=True,
        authority_requirement=authority_requirement,
        success_criteria=(f"{title} 产生确定性且可审计的结果。",),
        verification=(f"独立查询验证 {title} 的 digest、边界和状态。",),
        rollback=("该节点不产生项目写入；丢弃未接受草案即可。",),
        timeout_ms=30_000,
        state="proposed",
        evidence=(objective.source_anchors[0],),
    )


def _build_plan(mode: str, objective: ObjectiveContract, times: Mapping[str, str]) -> tuple[PlanGraph, PlanGraph]:
    first_id = f"node:v10f-{mode}-resume"
    second_id = f"node:v10f-{mode}-artifact"
    third_id = f"node:v10f-{mode}-verification"
    nodes = (
        _node(
            node_id=first_id,
            objective=objective,
            title="绑定真实 Resume Package",
            purpose="仅读取 Operational State 中已存在的项目恢复包和项目投影。",
            authority_requirement="read",
            read_resume=True,
        ),
        _node(
            node_id=second_id,
            objective=objective,
            title=("形成可验证 Plan Graph" if mode == "plan" else "形成准备预览"),
            purpose=(
                "生成含依赖、风险、授权需求、成功标准和验证方法的规划图。"
                if mode == "plan"
                else "只生成 proposed changes、authority needs、acceptance 和 rollback 草案。"
            ),
            dependencies=(first_id,),
            authority_requirement=("none" if mode == "plan" else "prepare"),
        ),
        _node(
            node_id=third_id,
            objective=objective,
            title="验证边界并等待接受",
            purpose="验证无 Provider、无工具、无外部仓库访问、无项目完成更新。",
            dependencies=(second_id,),
            node_kind="verification",
        ),
    )
    edges = (
        PlanEdge(
            edge_id=f"edge:v10f-{mode}-resume-to-artifact",
            from_node_id=first_id,
            to_node_id=second_id,
        ),
        PlanEdge(
            edge_id=f"edge:v10f-{mode}-artifact-to-verification",
            from_node_id=second_id,
            to_node_id=third_id,
        ),
    )
    initial = build_plan_graph(
        objective=objective,
        nodes=nodes,
        edges=edges,
        change_reason="建立真实项目的 bounded plan/prepare 初始图。",
        created_at=times[f"{mode}_plan_v1"],
        created_by="agent",
        plan_id=f"plan:v10-f-english-{mode}",
    )
    revised_nodes = tuple(
        replace(
            item,
            purpose=(
                item.purpose
                + " Revision 明确记录：外部项目仓库仍不可访问，任何未来 act 均需新 Objective 与独立 Authorization Artifact。"
            ),
        )
        if item.node_id == third_id
        else item
        for item in initial.nodes
    )
    revised = revise_plan(
        initial,
        objective=objective,
        nodes=revised_nodes,
        change_reason="补充外部仓库禁入和未来 act 必须重新授权的明确边界。",
        created_at=times[f"{mode}_plan_v2"],
        created_by="agent",
    )
    return initial, revised


def _build_context(objective: ObjectiveContract, resume: Mapping[str, Any]) -> Any:
    content = {
        "project_id": PROJECT_ID,
        "authoritative_baseline": resume["authoritative_baseline"],
        "current_version": resume["current_version"],
        "external_repository_policy": EXPECTED_EXTERNAL_POLICY,
        "project_state": resume["project_state"],
        "resume_package_digest": resume["package_digest"],
        "source_state": resume["source_state"],
        "cache_control": resume["cache_control"],
    }
    source = ContextSource(
        source_system="v9_resume",
        source_reference=f"resume-package:{PROJECT_ID}",
        source_digest=resume["package_digest"],
        purpose="绑定真实 v9 Project Resume Package 的最小必要字段。",
        items=(
            ContextItem(
                item_id=f"context-item:{objective.objective_id}",
                content=content,
                priority=0,
                trust_class="untrusted_operational_state",
            ),
        ),
        maximum_items=1,
        maximum_bytes=8_192,
        freshness="fresh",
        source_state="live",
        cache_control="no-store",
        cache_classification="private_operational_state_live",
        cached_at=None,
        cache_stale=False,
        as_of=resume["as_of"],
        required=True,
        priority=0,
    )
    return assemble_cognitive_context(
        objective_id=objective.objective_id,
        objective_digest=objective.canonical_digest,
        sources=(source,),
        budget=ContextBudget(
            maximum_items=1,
            maximum_bytes=12_000,
            maximum_estimated_tokens=3_000,
        ),
    )


def _plan_event_entity(graph: PlanGraph) -> str:
    return make_identity(
        "workitem", {"plan_id": graph.plan_id, "version": graph.version}
    )


def _run_identity(mode: str, objective: ObjectiveContract, plan: PlanGraph) -> str:
    return make_identity(
        "cognitiverun",
        {
            "acceptance_version": ACCEPTANCE_VERSION,
            "mode": mode,
            "objective_digest": objective.canonical_digest,
            "plan_digest": plan.plan_digest,
            "provider_calls": 0,
            "tool_executions": 0,
        },
    )


def _build_contracts(resume: Mapping[str, Any], base: datetime) -> _Contracts:
    times = {
        "plan_anchor": _stamp(base, 1),
        "prepare_anchor": _stamp(base, 2),
        "plan_objective": _stamp(base, 3),
        "prepare_objective": _stamp(base, 4),
        "plan_plan_v1": _stamp(base, 5),
        "plan_plan_v2": _stamp(base, 6),
        "prepare_plan_v1": _stamp(base, 7),
        "prepare_plan_v2": _stamp(base, 8),
        "context": _stamp(base, 9),
        "plan_run": _stamp(base, 10),
        "prepare_run": _stamp(base, 11),
        "plan_verifying": _stamp(base, 12),
        "prepare_verifying": _stamp(base, 13),
        "verification": _stamp(base, 14),
        "candidates": _stamp(base, 15),
        "plan_waiting": _stamp(base, 16),
        "prepare_waiting": _stamp(base, 17),
        "release": _stamp(base, 20),
        "recovery": _stamp(base, 30),
    }
    anchors = {
        mode: _objective_anchor(mode, resume, times[f"{mode}_anchor"])
        for mode in ("plan", "prepare")
    }
    objectives = {
        mode: _build_objective(mode, anchors[mode])
        for mode in ("plan", "prepare")
    }
    plans = {
        mode: _build_plan(mode, objectives[mode], times)
        for mode in ("plan", "prepare")
    }
    contexts = {
        mode: _build_context(objectives[mode], resume)
        for mode in ("plan", "prepare")
    }
    run_ids = {
        mode: _run_identity(mode, objectives[mode], plans[mode][1])
        for mode in ("plan", "prepare")
    }
    write_scope = [
        f"decision:v10-f-english-{mode}-objective"
        for mode in ("plan", "prepare")
    ]
    write_scope.extend(
        _plan_event_entity(graph)
        for mode in ("plan", "prepare")
        for graph in plans[mode]
    )
    write_scope.extend(run_ids.values())
    authority = AuthorityArtifact(
        authority_id="authority:yunos-v10-f-real-plan-prepare",
        issued_by=USER_ACTOR_ID,
        issued_at=_stamp(base, -60),
        expires_at=_stamp(base, 900),
        target_project_id=PROJECT_ID,
        allowed_event_types=(
            "candidate_create",
            "cognitive_run_created",
            "cognitive_run_update",
            "decision_approved",
            "plan_created",
            "plan_revised",
            "verification_record",
        ),
        read_scope=("operational://project:ayun-english-core",),
        write_scope=tuple(sorted(write_scope)),
        prohibited_operations=(
            "accept_result",
            "create_delegation",
            "create_next_version",
            "create_outcome_record",
            "external_project_access",
            "merge",
            "network",
            "provider_call",
            "push",
            "tag",
            "tool_execution",
            "tool_proposal",
        ),
        commit_permission=True,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
    )
    contracts = _Contracts(
        objectives=objectives,
        plans=plans,
        contexts=contexts,
        anchors=anchors,
        authority=authority,
        run_ids=run_ids,
        times=times,
    )
    _assert_contract_boundaries(contracts)
    return contracts


def _assert_contract_boundaries(contracts: _Contracts) -> None:
    for mode in ("plan", "prepare"):
        objective = contracts.objectives[mode]
        if (
            objective.project_id != PROJECT_ID
            or objective.agency_level != mode
            or objective.cognitive_stance != "continue"
            or objective.agency_selection.selected_level != mode
            or objective.agency_selection.maximum_authorized_level != mode
            or objective.agency_selection.selected_by != "user"
            or objective.clarification_required
            or objective.acceptance_policy.automatic_allowed
            or not objective.acceptance_policy.requires_user_event
        ):
            _fail("objective_hidden_agency_elevation")
        request = objective.authority_request
        if (
            request.requested
            or request.write_roots
            or request.capabilities
            or request.commands
            or request.commit_permission
            or request.merge_permission
            or request.tag_permission
            or request.push_permission
            or request.network_permission
            or request.database_permission != "readonly"
        ):
            _fail("objective_authority_overreach")
        history = contracts.plans[mode]
        if len(history) != 2 or history[0].version != 1 or history[1].version != 2:
            _fail("plan_revision_history_invalid")
        for graph in history:
            if (
                graph.objective_id != objective.objective_id
                or graph.objective_digest != objective.canonical_digest
                or len(graph.nodes) != 3
                or len(graph.edges) != 2
            ):
                _fail("plan_objective_binding_mismatch")
            for node in graph.nodes:
                if node.write_scope or node.capability_requirements:
                    _fail("plan_write_or_tool_overreach")
                if any(
                    not scope.startswith("operational://project:ayun-english-core")
                    for scope in node.read_scope
                ):
                    _fail("plan_external_read_overreach")
        serialized = canonical_json(
            {
                "objective": objective.to_dict(),
                "plans": [graph.to_dict() for graph in history],
            }
        )
        if _FORBIDDEN_EXTERNAL_TEXT in serialized.casefold():
            _fail("external_project_path_forbidden")
        if _FORBIDDEN_VERSION.search(serialized):
            _fail("next_version_creation_forbidden")


def _objective_event(
    mode: str, objective: ObjectiveContract, authority: AuthorityArtifact, at: str
) -> Any:
    return build_state_event(
        project_id=PROJECT_ID,
        entity_id=f"decision:v10-f-english-{mode}-objective",
        entity_type="decision",
        event_type="decision_approved",
        source_kind="declared",
        source_anchor=objective.source_anchors[0],
        authority_kind="user",
        actor_kind="user",
        actor_id=USER_ACTOR_ID,
        observed_at=at,
        effective_at=at,
        recorded_at=at,
        expires_at=None,
        supersedes_event_id=None,
        caused_by_event_id=None,
        idempotency_key=f"idempotency:v10-f-english-{mode}-objective",
        conflict_group_id=None,
        payload={
            "state": "approved",
            "objective_id": objective.objective_id,
            "objective_version": objective.version,
            "objective_digest": objective.canonical_digest,
            "agency_level": mode,
            "execution_authorized": False,
        },
        privacy_scope="private_metadata",
        authority_digest=authority.authority_digest,
    )


def _plan_event(
    graph: PlanGraph,
    *,
    anchor: SourceAnchor,
    authority: AuthorityArtifact,
    caused_by_event_id: str,
) -> Any:
    at = graph.revisions[-1].created_at
    return build_state_event(
        project_id=PROJECT_ID,
        entity_id=_plan_event_entity(graph),
        entity_type="work_item",
        event_type="plan_created" if graph.version == 1 else "plan_revised",
        source_kind="declared",
        source_anchor=anchor,
        authority_kind="user",
        actor_kind="agent",
        actor_id=PLANNER_ACTOR_ID,
        observed_at=at,
        effective_at=at,
        recorded_at=at,
        expires_at=None,
        supersedes_event_id=None,
        caused_by_event_id=caused_by_event_id,
        idempotency_key=make_identity(
            "idempotency", {"plan_digest": graph.plan_digest, "version": graph.version}
        ),
        conflict_group_id=None,
        payload={
            "state": "proposed",
            "plan_id": graph.plan_id,
            "plan_version": graph.version,
            "plan_digest": graph.plan_digest,
            "objective_id": graph.objective_id,
            "objective_version": graph.objective_version,
            "objective_digest": graph.objective_digest,
            "authority_impact": graph.revisions[-1].authority_impact,
        },
        privacy_scope="private_metadata",
        authority_digest=authority.authority_digest,
    )


def _persist_context_binding(
    *,
    target: _AcceptanceTarget,
    binding: Any,
    objective: ObjectiveContract,
    anchor: SourceAnchor,
    authority: AuthorityArtifact,
    owner: str,
    token: str,
    created_at: str,
) -> None:
    target = _rebuild_shared_target(target)
    store = OperationalStateStore(target.operational_db)
    with closing(store._connect(write=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            store._assert_lease(connection, owner_id=owner, token=token, at=created_at)
            authority.authorize_event(
                project_id=PROJECT_ID,
                entity_id=f"decision:v10-f-english-{objective.agency_level}-objective",
                event_type="decision_approved",
                at=created_at,
            )
            objective_row = connection.execute(
                "SELECT canonical_digest FROM objective_contracts "
                "WHERE objective_id=? AND version=?",
                (objective.objective_id, objective.version),
            ).fetchone()
            anchor_row = connection.execute(
                "SELECT 1 FROM state_source_anchors WHERE anchor_id=?", (anchor.anchor_id,)
            ).fetchone()
            if (
                objective_row is None
                or objective_row["canonical_digest"] != objective.canonical_digest
                or anchor_row is None
            ):
                _fail("context_binding_source_missing")
            connection.execute(
                "INSERT INTO context_bindings("
                "binding_id,objective_id,objective_version,source_system,"
                "source_reference,source_digest,maximum_items,maximum_bytes,"
                "selected_items,canonical_bytes,freshness,contains_secret,"
                "canonical_binding,binding_digest,source_anchor_id,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding.binding_id,
                    objective.objective_id,
                    objective.version,
                    binding.source_system,
                    binding.source_reference,
                    binding.source_digest,
                    binding.maximum_items,
                    binding.maximum_bytes,
                    binding.selected_items,
                    binding.canonical_bytes,
                    binding.freshness,
                    0,
                    canonical_json(binding.to_dict()),
                    binding.binding_digest,
                    anchor.anchor_id,
                    created_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _create_nonexecuting_run(
    *,
    target: _AcceptanceTarget,
    mode: str,
    objective: ObjectiveContract,
    plan: PlanGraph,
    provider: Mapping[str, Any],
    authority: AuthorityArtifact,
    owner: str,
    token: str,
    at: str,
) -> CognitiveRun:
    target = _rebuild_shared_target(target)
    run_id = _run_identity(mode, objective, plan)
    checkpoint = _create_cognitive_checkpoint(
        run_id=run_id,
        checkpoint_sequence=1,
        safe_to_resume=True,
        run_status="verifying",
        authorization_digest=None,
        objective_digest=objective.canonical_digest,
        plan_digest=plan.plan_digest,
        provider_profile_digest=provider["profile_digest"],
        current_node_ids=(),
        completed_node_ids=tuple(node.node_id for node in plan.nodes),
        model_budget_state=_empty_model_budget_state_payload(),
        runtime_recovery_bindings=(),
        proposal_ids=(),
        execution_ids=(),
        idempotency_keys=(),
        last_completed_execution_id=None,
        unsafe_reason_codes=(),
        steps_used=0,
        started_at=at,
        last_activity_at=at,
        ended_at=None,
        failure_code=None,
        recorded_at=at,
    )
    run = _create_cognitive_run(
        run_id=run_id,
        objective_id=objective.objective_id,
        objective_version=objective.version,
        objective_digest=objective.canonical_digest,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_digest=plan.plan_digest,
        provider_id=provider["provider_id"],
        provider_profile_version=provider["profile_version"],
        provider_profile_digest=provider["profile_digest"],
        authorization_id=None,
        authorization_digest=None,
        status="verifying",
        checkpoint_sequence=1,
        last_safe_checkpoint=checkpoint.checkpoint_digest,
        model_calls_used=0,
        tokens_used=0,
        cost_used_microunits=0,
        steps_used=0,
        started_at=at,
        last_activity_at=at,
        ended_at=None,
        failure_code=None,
    )
    execution = CognitiveExecutionStore(
        target.operational_db,
        trusted_clock=lambda: at,
    )
    event = execution._resource_event(
        source_authority=authority,
        project_id=PROJECT_ID,
        entity_id=run_id,
        event_type="cognitive_run_created",
        actor_kind="system",
        actor_id=SYSTEM_ACTOR_ID,
        at=at,
        expires_at=None,
        payload={
            "run_id": run_id,
            "run_digest": run.run_digest,
            "agency_level": mode,
            "authorization_id": None,
            "provider_calls": 0,
            "tool_proposals": 0,
            "tool_executions": 0,
            "acceptance_state": "not_accepted",
        },
        idempotency_key=f"idempotency:v10-f-english-{mode}-run",
        authority_kind="deterministic_observation",
        source_kind="observed",
    )
    preview = execution.operational.preview_event(authority=authority, event=event)
    with closing(execution.operational._connect(write=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            execution.operational._commit_preview_on_connection(
                connection,
                authority=authority,
                preview=preview,
                lease_owner_id=owner,
                lease_token=token,
            )
            execution._insert_run(connection, run)
            execution._insert_checkpoint(connection, checkpoint)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return run


def _transition_run(
    *,
    target: _AcceptanceTarget,
    run: CognitiveRun,
    next_status: str,
    authority: AuthorityArtifact,
    owner: str,
    token: str,
    at: str,
    failure_code: str | None = None,
) -> CognitiveRun:
    target = _rebuild_shared_target(target)
    return CognitiveExecutionStore(
        target.operational_db,
        trusted_clock=lambda: at,
    ).transition_run(
        run_id=run.run_id,
        expected_run_digest=run.run_digest,
        next_status=next_status,
        source_authority=authority,
        lease_owner_id=owner,
        lease_token=token,
        failure_code=failure_code,
    )


def _candidate_anchor(contracts: _Contracts) -> SourceAnchor:
    evidence = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "run_ids": sorted(contracts.run_ids.values()),
        "source": "system_verified_plan_prepare",
        "acceptance": "pending_user_event",
    }
    return SourceAnchor(
        anchor_id="anchor:database:v10-f-plan-prepare-candidates",
        source_kind="database",
        locator="operational-state:v10-f-plan-prepare-candidates",
        evidence_digest=canonical_digest(evidence),
        observed_at=contracts.times["candidates"],
        privacy_scope="private_metadata",
    )


def _prepare_preview(contracts: _Contracts) -> dict[str, Any]:
    prepare = contracts.objectives["prepare"]
    plan = contracts.plans["prepare"][1]
    payload = {
        "artifact_version": "v10-f-prepare-preview-1",
        "objective_id": prepare.objective_id,
        "objective_digest": prepare.canonical_digest,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_digest": plan.plan_digest,
        "artifact_only": True,
        "proposed_changes": [
            "在未来独立 act 任务中审查并完善 book-aware migration 的验证说明。",
            "在未来独立 act 任务中增加确定性的 localStorage migration 回归测试。",
        ],
        "authority_needs": {
            "new_act_objective_required": True,
            "authorization_artifact_required": True,
            "external_repository_read_scope_required": True,
            "external_repository_write_scope_required": True,
            "currently_granted": False,
        },
        "acceptance_conditions": [
            "独立 act 授权明确限定仓库根目录和允许能力。",
            "diff、测试和回滚证据均通过系统验证。",
            "用户 Acceptance Event 明确发生。",
        ],
        "rollback_conditions": [
            "只回滚该未来任务拥有的变更。",
            "授权过期、越权、测试失败或证据丢失时 fail-closed。",
        ],
        "denied_now": [
            "external_repository_access",
            "provider_call",
            "tool_proposal",
            "tool_execution",
            "network",
            "delegation",
            "project_completion_update",
            "self_acceptance",
        ],
        "project_read_performed": False,
        "project_write_performed": False,
        "provider_calls": 0,
        "tool_proposals": 0,
        "tool_executions": 0,
        "acceptance_state": "system_verified_not_user_accepted",
    }
    return {**payload, "preview_digest": canonical_digest(payload)}


def _record_verifications_and_candidates(
    *,
    target: _AcceptanceTarget,
    contracts: _Contracts,
    runs: Mapping[str, CognitiveRun],
    owner: str,
    token: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = _rebuild_shared_target(target)
    store = OperationalStateStore(target.operational_db)
    verification_results: dict[str, Any] = {}
    for mode in ("plan", "prepare"):
        objective = contracts.objectives[mode]
        plan = contracts.plans[mode][1]
        binding = contracts.contexts[mode].bindings[0]
        evidence = {
            "objective_digest": objective.canonical_digest,
            "plan_digest": plan.plan_digest,
            "context_binding_digest": binding.binding_digest,
            "resume_package_digest": binding.source_digest,
            "provider_calls": 0,
            "tool_proposals": 0,
            "tool_executions": 0,
            "external_repository_accesses": 0,
            "project_completion_updates": 0,
            "acceptance_events": 0,
        }
        verification_results[mode] = store.record_verification(
            verification_id=make_identity(
                "verification", {"run_id": runs[mode].run_id, "evidence": evidence}
            ),
            entity_id=runs[mode].run_id,
            event_id=None,
            verifier_kind="deterministic_system",
            result="passed",
            evidence_digest=canonical_digest(evidence),
            details={
                **evidence,
                "result_state": "system_verified_not_user_accepted",
                "user_accepted": False,
            },
            recorded_at=contracts.times["verification"],
            authority=contracts.authority,
            lease_owner_id=owner,
            lease_token=token,
        )

    preview = _prepare_preview(contracts)
    candidate_anchor = _candidate_anchor(contracts)
    definitions: list[tuple[str, str, CognitiveRun, dict[str, Any]]] = [
        (
            "prepare_preview",
            "prepare_preview",
            runs["prepare"],
            preview,
        )
    ]
    for mode in ("plan", "prepare"):
        definitions.append(
            (
                f"{mode}_outcome",
                "outcome_candidate",
                runs[mode],
                {
                    "candidate_version": "v10-f-outcome-candidate-1",
                    "mode": mode,
                    "run_id": runs[mode].run_id,
                    "system_verified": True,
                    "user_accepted": False,
                    "acceptance_state": "system_verified_not_user_accepted",
                    "project_completion_update_allowed": False,
                },
            )
        )
    definitions.extend(
        (
            (
                "fact",
                "fact_record",
                runs["plan"],
                {
                    "candidate_version": "v10-f-learning-candidate-1",
                    "kind": "fact",
                    "fact": "真实 v9 Resume Package 的基线、版本和只读外部仓库策略已验证。",
                    "long_term_preference": False,
                    "status": "pending",
                },
            ),
            (
                "observation",
                "observation",
                runs["prepare"],
                {
                    "candidate_version": "v10-f-learning-candidate-1",
                    "kind": "observation",
                    "observation": "Plan/Prepare 可在零 Provider、零工具和零项目访问下形成可审计草案。",
                    "stable_preference": False,
                    "status": "pending",
                },
            ),
            (
                "workflow",
                "workflow_candidate",
                runs["prepare"],
                {
                    "candidate_version": "v10-f-learning-candidate-1",
                    "kind": "workflow_candidate",
                    "workflow": "Resume gate → Objective → Plan revision → Preview → Verification → Wait acceptance",
                    "auto_adopt_allowed": False,
                    "user_approval_required": True,
                    "status": "pending",
                },
            ),
        )
    )
    candidate_results: list[dict[str, Any]] = []
    for label, candidate_type, run, payload in definitions:
        candidate_results.append(
            store.create_candidate(
                candidate_id=make_identity(
                    "candidate",
                    {
                        "acceptance_version": ACCEPTANCE_VERSION,
                        "label": label,
                        "run_id": run.run_id,
                        "candidate_digest": canonical_digest(payload),
                    },
                ),
                project_id=PROJECT_ID,
                entity_id=run.run_id,
                entity_type="resource",
                candidate_type=candidate_type,
                candidate=payload,
                confidence_ppm=1_000_000,
                source_anchor=candidate_anchor,
                created_at=contracts.times["candidates"],
                expires_at=None,
                authority=contracts.authority,
                lease_owner_id=owner,
                lease_token=token,
                enforce_authority=True,
                strict_replay_binding=True,
            )
        )
    return verification_results, candidate_results


def _persist_acceptance(
    *,
    target: _AcceptanceTarget,
    contracts: _Contracts,
    provider: Mapping[str, Any],
    owner: str,
    token: str,
) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    objective_results: dict[str, Any] = {}
    plan_results: dict[str, list[dict[str, Any]]] = {}
    for mode in ("plan", "prepare"):
        objective = contracts.objectives[mode]
        at = contracts.times[f"{mode}_objective"]
        objective_results[mode] = CognitiveStateStore(
            target.operational_db,
            trusted_clock=lambda value=at: value,
        ).commit_objective(
            contract=objective.to_dict(),
            authority=contracts.authority,
            event=_objective_event(mode, objective, contracts.authority, at),
            lease_owner_id=owner,
            lease_token=token,
            created_at=at,
        )
        caused_by = objective_results[mode]["event_id"]
        plan_results[mode] = []
        for graph in contracts.plans[mode]:
            at = graph.revisions[-1].created_at
            result = CognitivePlanningStore(
                target.operational_db,
                trusted_clock=lambda value=at: value,
            ).commit_plan(
                graph=graph,
                authority=contracts.authority,
                event=_plan_event(
                    graph,
                    anchor=contracts.anchors[mode],
                    authority=contracts.authority,
                    caused_by_event_id=caused_by,
                ),
                lease_owner_id=owner,
                lease_token=token,
            )
            plan_results[mode].append(result)
            caused_by = result["event_id"]
        _persist_context_binding(
            target=target,
            binding=contracts.contexts[mode].bindings[0],
            objective=objective,
            anchor=contracts.anchors[mode],
            authority=contracts.authority,
            owner=owner,
            token=token,
            created_at=contracts.times["context"],
        )

    runs: dict[str, CognitiveRun] = {}
    for mode in ("plan", "prepare"):
        runs[mode] = _create_nonexecuting_run(
            target=target,
            mode=mode,
            objective=contracts.objectives[mode],
            plan=contracts.plans[mode][1],
            provider=provider,
            authority=contracts.authority,
            owner=owner,
            token=token,
            at=contracts.times[f"{mode}_run"],
        )
    verifications, candidates = _record_verifications_and_candidates(
        target=target,
        contracts=contracts,
        runs=runs,
        owner=owner,
        token=token,
    )
    for mode in ("plan", "prepare"):
        runs[mode] = _transition_run(
            target=target,
            run=runs[mode],
            next_status="waiting_acceptance",
            authority=contracts.authority,
            owner=owner,
            token=token,
            at=contracts.times[f"{mode}_waiting"],
        )
    return {
        "objective_results": objective_results,
        "plan_results": plan_results,
        "runs": runs,
        "verifications": verifications,
        "candidates": candidates,
        "prepare_preview": _prepare_preview(contracts),
    }


def _operational_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
    return {
        table: after["counts"][table] - before["counts"][table]
        for table in _OPERATIONAL_TABLES
    }


def _audit_persisted_result(
    *,
    target: _AcceptanceTarget,
    contracts: _Contracts,
    persisted: Mapping[str, Any],
    project_before: Mapping[str, Any],
    operational_before: Mapping[str, Any],
    operational_after: Mapping[str, Any],
    runtime_before: Mapping[str, Any],
    runtime_after: Mapping[str, Any],
) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    delta = _operational_delta(operational_before, operational_after)
    if delta != EXPECTED_OPERATIONAL_DELTA:
        _fail("operational_delta_mismatch")
    if operational_after["writer_leases"] != 0:
        _fail("writer_lease_not_zero")
    for table in _PROTECTED_ZERO_DELTA_TABLES:
        if operational_before["protected_digests"][table] != operational_after[
            "protected_digests"
        ][table]:
            _fail("protected_operational_table_changed")
    if runtime_before != runtime_after:
        _fail("runtime_delta_not_zero")
    after_project = OperationalStateStore(target.operational_db).get_projection(PROJECT_ID)
    if (
        after_project is None
        or after_project.get("projection_digest")
        != project_before.get("projection_digest")
        or after_project.get("state") != project_before.get("state")
        or after_project.get("fields") != project_before.get("fields")
    ):
        _fail("project_projection_changed")
    uri = f"file:{target.operational_db.resolve(strict=True).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        run_rows = connection.execute(
            "SELECT run_id,status,authorization_id,model_calls_used,tokens_used,"
            "cost_used_microunits,steps_used FROM cognitive_runs "
            "WHERE run_id IN (?,?) ORDER BY run_id",
            tuple(sorted(contracts.run_ids.values())),
        ).fetchall()
        if len(run_rows) != 2 or any(
            row["status"] != "waiting_acceptance"
            or row["authorization_id"] is not None
            or row["model_calls_used"] != 0
            or row["tokens_used"] != 0
            or row["cost_used_microunits"] != 0
            or row["steps_used"] != 0
            for row in run_rows
        ):
            _fail("cognitive_run_acceptance_boundary_mismatch")
        candidates = connection.execute(
            "SELECT candidate_type,status FROM state_candidates WHERE entity_id IN (?,?) "
            "ORDER BY candidate_type,candidate_id",
            tuple(sorted(contracts.run_ids.values())),
        ).fetchall()
        expected_types = sorted(
            [
                "prepare_preview",
                "outcome_candidate",
                "outcome_candidate",
                "fact_record",
                "observation",
                "workflow_candidate",
            ]
        )
        if (
            [row["candidate_type"] for row in candidates] != expected_types
            or any(row["status"] != "pending" for row in candidates)
        ):
            _fail("pending_candidate_boundary_mismatch")
        acceptance_events = connection.execute(
            "SELECT COUNT(*) FROM state_events WHERE project_id=? "
            "AND event_type='outcome_accepted'",
            (PROJECT_ID,),
        ).fetchone()[0]
        if acceptance_events:
            _fail("self_acceptance_detected")
        context_rows = connection.execute(
            "SELECT source_system,source_digest,contains_secret FROM context_bindings "
            "WHERE objective_id IN (?,?) ORDER BY objective_id",
            tuple(sorted(item.objective_id for item in contracts.objectives.values())),
        ).fetchall()
        if len(context_rows) != 2 or any(
            row["source_system"] != "v9_resume"
            or row["source_digest"]
            != contracts.contexts["plan"].bindings[0].source_digest
            or row["contains_secret"] != 0
            for row in context_rows
        ):
            _fail("context_binding_audit_failed")
    return {
        "operational_delta": delta,
        "runtime_delta": {table: 0 for table in _RUNTIME_TABLES},
        "provider_registry_delta": 0,
        "tool_proposal_delta": 0,
        "tool_execution_delta": 0,
        "delegation_delta": 0,
        "outcome_record_delta": 0,
        "approval_delta": 0,
        "project_completion_update_delta": 0,
        "external_repository_accesses": 0,
        "writer_lease_final": 0,
        "run_states": {
            mode: persisted["runs"][mode].status for mode in ("plan", "prepare")
        },
        "acceptance_state": "system_verified_not_user_accepted",
        "project_projection_digest_unchanged": True,
    }


def _failure_reconcile(
    *,
    target: _AcceptanceTarget,
    contracts: _Contracts,
    reason_code: str,
) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    store = OperationalStateStore(target.operational_db)
    owner = "writer:v10-f-plan-prepare-reconcile"
    token = "v10-f-plan-prepare-reconcile-token-0001"
    acquired = False
    states: dict[str, str] = {}
    errors: list[str] = []
    try:
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=contracts.times["recovery"],
            expires_at=_stamp(
                datetime.fromisoformat(
                    contracts.times["recovery"].replace("Z", "+00:00")
                ),
                60,
            ),
        )
        acquired = True
        for mode, run_id in contracts.run_ids.items():
            try:
                execution = CognitiveExecutionStore(
                    target.operational_db,
                    trusted_clock=lambda: contracts.times["recovery"],
                )
                run = execution.get_run(run_id)
                if run.status in {"planning", "verifying"}:
                    run = execution.transition_run(
                        run_id=run.run_id,
                        expected_run_digest=run.run_digest,
                        next_status="recovery_required",
                        source_authority=contracts.authority,
                        lease_owner_id=owner,
                        lease_token=token,
                        failure_code=reason_code,
                    )
                elif run.status == "waiting_acceptance":
                    run = execution.transition_run(
                        run_id=run.run_id,
                        expected_run_digest=run.run_digest,
                        next_status="failed",
                        source_authority=contracts.authority,
                        lease_owner_id=owner,
                        lease_token=token,
                        failure_code=reason_code,
                    )
                states[mode] = run.status
            except OperationalStateError as exc:
                if exc.reason_code == "cognitive_run_not_found":
                    states[mode] = "not_created"
                else:
                    errors.append(exc.reason_code)
    except (OperationalStateError, PlanPrepareAcceptanceError) as exc:
        errors.append(getattr(exc, "reason_code", "reconcile_failed"))
    finally:
        if acquired:
            try:
                store.release_writer_lease(
                    owner_id=owner,
                    token=token,
                    released_at=_stamp(
                        datetime.fromisoformat(
                            contracts.times["recovery"].replace("Z", "+00:00")
                        ),
                        1,
                    ),
                )
            except OperationalStateError as exc:
                errors.append(exc.reason_code)
    try:
        lease_zero = store.read_status()["lease"] is None
    except OperationalStateError:
        lease_zero = False
    return {
        "attempted": True,
        "run_states": states,
        "writer_lease_zero": lease_zero,
        "errors": sorted(set(errors)),
        "recovery_required": bool(errors) or not lease_zero,
    }


def _execute_acceptance(
    *,
    target: _AcceptanceTarget,
    confirm_user_authorization_sha256: str,
    clock: Callable[[], str],
) -> dict[str, Any]:
    target = _rebuild_shared_target(target)
    if confirm_user_authorization_sha256 != USER_AUTHORIZATION_SHA256:
        _fail("user_authorization_confirmation_mismatch")
    _reject_reparse_chain(target.operational_db, "operational_database_reparse_forbidden")
    _reject_reparse_chain(target.runtime_db, "runtime_database_reparse_forbidden")
    base_text = normalize_timestamp(clock())
    base = datetime.fromisoformat(base_text.replace("Z", "+00:00"))
    project_before, resume = _project_and_resume_gate(
        target,
        as_of=base_text,
    )
    provider = _provider_gate(target)
    operational_before = _operational_snapshot(target)
    runtime_before = _runtime_snapshot(target)
    if operational_before["writer_leases"] != 0:
        _fail("writer_lease_not_zero_before_acceptance")
    contracts = _build_contracts(resume, base)
    output = _pin_new_output_root(target)
    sentry = {
        "version": "v10-f-real-plan-prepare-one-shot-sentry-1",
        "acceptance_version": ACCEPTANCE_VERSION,
        "created_at": base_text,
        "project_id": PROJECT_ID,
        "user_authorization_sha256": USER_AUTHORIZATION_SHA256,
        "resume_package_digest": resume["package_digest"],
        "project_projection_digest": project_before["projection_digest"],
        "operational_before_digest": operational_before["snapshot_digest"],
        "runtime_before_digest": runtime_before["snapshot_digest"],
        "provider_profile_digest": provider["profile_digest"],
        "provider_calls_allowed": False,
        "tool_execution_allowed": False,
        "external_repository_access_allowed": False,
        "do_not_rerun": True,
    }
    sentry["sentry_digest"] = canonical_digest(sentry)
    try:
        _write_exclusive(output, SENTRY_NAME, sentry)
    except BaseException:
        try:
            _release_output_anchor(output)
        except BaseException:
            pass
        raise

    store = OperationalStateStore(target.operational_db)
    owner = "writer:v10-f-real-plan-prepare"
    token = "v10-f-real-plan-prepare-one-shot-token-0001"
    lease_acquired = False
    persisted: dict[str, Any] | None = None
    failure: BaseException | None = None
    try:
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=base_text,
            expires_at=_stamp(base, 120),
        )
        lease_acquired = True
        persisted = _persist_acceptance(
            target=target,
            contracts=contracts,
            provider=provider,
            owner=owner,
            token=token,
        )
    except BaseException as exc:
        failure = exc
    finally:
        if lease_acquired:
            try:
                store.release_writer_lease(
                    owner_id=owner,
                    token=token,
                    released_at=contracts.times["release"],
                )
            except BaseException as release_error:
                if failure is None:
                    failure = release_error
    if failure is not None:
        reason = getattr(failure, "reason_code", "plan_prepare_persistence_failed")
        recovery = _failure_reconcile(
            target=target, contracts=contracts, reason_code=str(reason)
        )
        failure_payload = {
            "version": ACCEPTANCE_VERSION,
            "status": "NO_GO_DO_NOT_RERUN",
            "reason_code": (
                reason if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason) else "plan_prepare_persistence_failed"
            ),
            "failed_at": _utc_now(),
            "recovery": recovery,
            "provider_calls": 0,
            "tool_executions": 0,
            "external_repository_accesses": 0,
            "sanitized": True,
            "do_not_rerun": True,
        }
        try:
            _write_exclusive(output, FAILURE_NAME, failure_payload)
        except BaseException:
            pass
        try:
            _release_output_anchor(output)
        except BaseException:
            pass
        raise failure
    if persisted is None:
        _fail("plan_prepare_persisted_result_missing")

    try:
        operational_after = _operational_snapshot(target)
        runtime_after = _runtime_snapshot(target)
        audit = _audit_persisted_result(
            target=target,
            contracts=contracts,
            persisted=persisted,
            project_before=project_before,
            operational_before=operational_before,
            operational_after=operational_after,
            runtime_before=runtime_before,
            runtime_after=runtime_after,
        )
    except BaseException as exc:
        reason = getattr(exc, "reason_code", "post_persistence_audit_failed")
        recovery = _failure_reconcile(
            target=target, contracts=contracts, reason_code=str(reason)
        )
        failure_payload = {
            "version": ACCEPTANCE_VERSION,
            "status": "NO_GO_DO_NOT_RERUN",
            "reason_code": (
                reason
                if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason)
                else "post_persistence_audit_failed"
            ),
            "failed_at": _utc_now(),
            "recovery": recovery,
            "provider_calls": 0,
            "tool_executions": 0,
            "external_repository_accesses": 0,
            "sanitized": True,
            "do_not_rerun": True,
        }
        try:
            _write_exclusive(output, FAILURE_NAME, failure_payload)
        except BaseException:
            pass
        try:
            _release_output_anchor(output)
        except BaseException:
            pass
        raise
    result = {
        "version": ACCEPTANCE_VERSION,
        "status": "YUNOS_V10_F_REAL_PLAN_PREPARE_ACCEPTANCE_GO",
        "completed_at": _utc_now(),
        "project": {
            "project_id": PROJECT_ID,
            "authoritative_baseline": EXPECTED_BASELINE,
            "current_version": EXPECTED_PROJECT_VERSION,
            "external_repository_policy": EXPECTED_EXTERNAL_POLICY,
            "resume_package_digest": resume["package_digest"],
            "external_repository_accesses": 0,
            "project_completion_updates": 0,
        },
        "objectives": {
            mode: {
                "objective_id": contracts.objectives[mode].objective_id,
                "objective_digest": contracts.objectives[mode].canonical_digest,
                "agency_level": mode,
                "cognitive_stance": "continue",
                "authority_requested": False,
            }
            for mode in ("plan", "prepare")
        },
        "plans": {
            mode: {
                "plan_id": contracts.plans[mode][1].plan_id,
                "version": 2,
                "plan_digest": contracts.plans[mode][1].plan_digest,
                "revision_digest": contracts.plans[mode][1].revisions[-1].revision_digest,
                "node_count": 3,
                "edge_count": 2,
                "write_scope": [],
                "capabilities": [],
            }
            for mode in ("plan", "prepare")
        },
        "context_bindings": {
            mode: contracts.contexts[mode].bindings[0].to_dict()
            for mode in ("plan", "prepare")
        },
        "authorization_boundary": {
            "authorization_artifacts_created": 0,
            "execution_authorized": False,
            "merge": False,
            "tag": False,
            "push": False,
            "network": False,
            "external_project_access": False,
            "user_acceptance_required": True,
        },
        "prepare_preview": persisted["prepare_preview"],
        "runs": {
            mode: {
                "run_id": persisted["runs"][mode].run_id,
                "status": persisted["runs"][mode].status,
                "authorization_id": None,
                "model_calls": 0,
                "tool_proposals": 0,
                "tool_executions": 0,
                "acceptance_state": "system_verified_not_user_accepted",
            }
            for mode in ("plan", "prepare")
        },
        "verifications": persisted["verifications"],
        "learning_candidates": {
            "count": 3,
            "types": ["fact_record", "observation", "workflow_candidate"],
            "status": "pending",
            "long_term_profile_updated": False,
        },
        "provider": {
            "provider_id": PERSISTED_PROVIDER_ID,
            "profile_version": PERSISTED_PROVIDER_VERSION,
            "profile_digest": provider["profile_digest"],
            "binding_purpose": "schema_required_persistence_reference_only",
            "calls": 0,
            "runtime_delta": 0,
            "hidden_switch": False,
            "secret_material_read": False,
        },
        "audit": audit,
        "one_shot": {
            "sentry": SENTRY_NAME,
            "do_not_rerun": True,
            "failure_reconcile_available": True,
        },
    }
    result["report_digest"] = canonical_digest(result)
    try:
        _write_result_exclusive(output, result)
    except BaseException as exc:
        reason = getattr(exc, "reason_code", "result_artifact_write_failed")
        recovery = _failure_reconcile(
            target=target, contracts=contracts, reason_code=str(reason)
        )
        failure_payload = {
            "version": ACCEPTANCE_VERSION,
            "status": "NO_GO_DO_NOT_RERUN",
            "reason_code": (
                reason if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason) else "result_artifact_write_failed"
            ),
            "failed_at": _utc_now(),
            "recovery": recovery,
            "provider_calls": 0,
            "tool_executions": 0,
            "external_repository_accesses": 0,
            "sanitized": True,
            "do_not_rerun": True,
        }
        try:
            _write_exclusive(output, FAILURE_NAME, failure_payload)
        except BaseException:
            pass
        try:
            _release_output_anchor(output)
        except BaseException:
            pass
        raise
    _release_output_anchor(output)
    return result


@dataclass(frozen=True, slots=True)
class _FixedProductionExecutor:
    def run(self, *, confirm_user_authorization_sha256: str) -> dict[str, Any]:
        if type(self) is not _FixedProductionExecutor:
            _fail("exact_production_executor_required")
        if (
            confirm_user_authorization_sha256
            != "ff968831ddaf4e694673272ede56dc7857286b77c1cfddbd908559385282ae11"
        ):
            _fail("user_authorization_confirmation_mismatch")

        # These imports and all authority-bearing objects are constructed inside
        # this sole production method.  The public entry has no target, clock,
        # Store, source, output, or builder injection seam.
        from contextlib import closing as production_closing
        from datetime import datetime as ProductionDateTime
        from datetime import timedelta as ProductionTimedelta
        from datetime import timezone as ProductionTimezone
        import hashlib as production_hashlib
        import json as production_json
        import os as production_os
        from pathlib import Path as ProductionPath
        import re as production_re
        import sqlite3 as production_sqlite3

        from memory.cognitive_execution_models import (
            _create_cognitive_checkpoint as production_create_checkpoint,
            _create_cognitive_run as production_create_run,
        )
        from memory.cognitive_execution_store import (
            CognitiveExecutionStore as ProductionExecutionStore,
            _empty_model_budget_state_payload as production_empty_budget,
        )
        from memory.cognitive_planning_store import (
            CognitivePlanningStore as ProductionPlanningStore,
        )
        from memory.cognitive_provider_store import (
            CognitiveProviderStore as ProductionProviderStore,
        )
        from memory.cognitive_state_migration import (
            verify_cognitive_state_schema as production_verify_schema,
        )
        from memory.cognitive_state_store import (
            CognitiveStateStore as ProductionStateStore,
        )
        from memory.db_path_policy import (
            is_reparse_point as production_is_reparse_point,
        )
        from memory.operational_state_context import (
            RESUME_PACKAGE_VERSION as PRODUCTION_RESUME_PACKAGE_VERSION,
            OperationalStateContextService as ProductionContextService,
        )
        from memory.operational_state_models import (
            OperationalStateError as ProductionOperationalStateError,
            canonical_digest as production_canonical_digest,
            canonical_json as production_canonical_json,
            make_identity as production_make_identity,
            normalize_timestamp as production_normalize_timestamp,
        )
        from memory.operational_state_store import (
            OperationalStateStore as ProductionOperationalStore,
        )

        if (
            production_os.environ.get(
                "YUNOS_OPERATIONAL_STATE_PRODUCTION_WRITE", ""
            ).strip()
            != "1"
        ):
            _fail("production_operational_write_environment_gate_required")

        production_root = ProductionPath(
            type(self).run.__code__.co_filename
        ).resolve().parents[1]
        target = (
            (
                production_root
                / "data"
                / "operational"
                / "ayunos_operational_state.db"
            ).absolute(),
            (
                production_root / "data" / "runtime" / "ayunos_runtime.db"
            ).absolute(),
            (
                production_root
                / "output"
                / "v10-f-production-adoption"
                / "real-plan-prepare-acceptance"
            ).absolute(),
        )
        operational_db, runtime_db, output_root = target
        if any(
            "e:\\ayunprojects" in production_os.path.normcase(str(path))
            for path in target
        ):
            _fail("external_project_path_forbidden")
        if operational_db == runtime_db or output_root in (
            operational_db,
            runtime_db,
        ):
            _fail("production_acceptance_path_mismatch")

        for candidate, reason_code in (
            (operational_db, "operational_database_reparse_forbidden"),
            (runtime_db, "runtime_database_reparse_forbidden"),
        ):
            current = candidate.absolute()
            while True:
                try:
                    if (
                        current.exists() or current.is_symlink()
                    ) and production_is_reparse_point(current):
                        _fail(reason_code)
                except OSError:
                    _fail(reason_code)
                if current.parent == current:
                    break
                current = current.parent

        base_text = production_normalize_timestamp(
            ProductionDateTime.now(ProductionTimezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        base = ProductionDateTime.fromisoformat(base_text.replace("Z", "+00:00"))

        production_verify_schema(operational_db)
        production_store = ProductionOperationalStore(
            operational_db,
            allow_production_write=True,
        )
        project_before = production_store.get_projection(
            "project:ayun-english-core"
        )
        if project_before is None:
            _fail("ayun_english_project_missing")
        project_fields = project_before.get("fields", {})
        if (
            project_before.get("entity_type") != "project"
            or project_before.get("state") != "active"
            or project_fields.get("authoritative_baseline")
            != "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53"
            or project_fields.get("current_version") != "v0.1.1"
            or project_fields.get("external_repository_policy")
            != "read_only_observation_only"
            or project_fields.get("source_repository_modified_by_yunos") is not False
        ):
            _fail("ayun_english_project_boundary_mismatch")
        resume = ProductionContextService(operational_db).resume_package(
            "project:ayun-english-core",
            budget_profile="Large",
            as_of=base_text,
        )
        if (
            resume.get("package_version") != PRODUCTION_RESUME_PACKAGE_VERSION
            or resume.get("project_id") != "project:ayun-english-core"
            or resume.get("source_state") != "live"
            or resume.get("cache_control") != "no-store"
            or resume.get("cache_stale") is not False
            or resume.get("authoritative_baseline")
            != "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53"
            or resume.get("current_version") != "v0.1.1"
            or not production_re.fullmatch(
                r"[0-9a-f]{64}", str(resume.get("package_digest", ""))
            )
        ):
            _fail("ayun_english_resume_package_mismatch")

        try:
            provider = ProductionProviderStore(
                operational_db,
                allow_production_write=True,
            ).get_provider("deepseek", 1)
        except ProductionOperationalStateError:
            _fail("persisted_provider_profile_missing_or_invalid")
        if not isinstance(provider, dict):
            _fail("persisted_provider_profile_missing_or_invalid")
        if (
            provider.get("provider_id") != "deepseek"
            or provider.get("profile_version") != 1
            or not production_re.fullmatch(
                r"[0-9a-f]{64}", str(provider.get("profile_digest", ""))
            )
            or provider.get("manual_selection_only") is not True
            or provider.get("grants_authority") is not False
            or provider.get("execution_allowed") is not False
        ):
            _fail("persisted_provider_profile_boundary_mismatch")

        operational_tables = (
            "authorization_artifacts",
            "capability_registry",
            "cognitive_run_checkpoints",
            "cognitive_runs",
            "cognitive_schema",
            "context_bindings",
            "delegation_contracts",
            "objective_contracts",
            "operational_schema",
            "outcome_records",
            "plan_edges",
            "plan_graphs",
            "plan_nodes",
            "plan_revisions",
            "provider_registry",
            "state_approvals",
            "state_candidates",
            "state_conflicts",
            "state_entities",
            "state_events",
            "state_lease_events",
            "state_projections",
            "state_receipts",
            "state_snapshots",
            "state_source_anchors",
            "state_verifications",
            "state_writer_leases",
            "tool_executions",
            "tool_proposals",
        )
        protected_tables = (
            "authorization_artifacts",
            "capability_registry",
            "delegation_contracts",
            "outcome_records",
            "provider_registry",
            "state_approvals",
            "tool_executions",
            "tool_proposals",
        )
        runtime_tables = (
            "runtime_schema",
            "runtime_conversations",
            "runtime_request_artifacts",
            "runtime_model_runs",
            "runtime_messages",
            "runtime_errors",
            "runtime_run_events",
            "runtime_receipts",
        )

        if not operational_db.is_file():
            _fail("operational_database_missing")
        operational_uri = (
            f"file:{operational_db.resolve(strict=True).as_posix()}?mode=ro"
        )
        with production_closing(
            production_sqlite3.connect(operational_uri, uri=True)
        ) as connection:
            connection.row_factory = production_sqlite3.Row
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                _fail("operational_integrity_check_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                _fail("operational_foreign_key_check_failed")
            actual_tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            if actual_tables != tuple(sorted(operational_tables)):
                _fail("operational_schema_table_set_mismatch")
            operational_before_counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in operational_tables
            }
            operational_before_protected: dict[str, str] = {}
            for table in protected_tables:
                columns = [
                    row[1]
                    for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    )
                ]
                rows = [
                    {column: row[column] for column in columns}
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                operational_before_protected[table] = (
                    production_canonical_digest(rows)
                )
        operational_before = {
            "counts": operational_before_counts,
            "protected_digests": operational_before_protected,
            "writer_leases": operational_before_counts["state_writer_leases"],
        }
        operational_before["snapshot_digest"] = production_canonical_digest(
            {
                "counts": operational_before_counts,
                "protected_digests": operational_before_protected,
            }
        )
        if operational_before["writer_leases"] != 0:
            _fail("writer_lease_not_zero_before_acceptance")

        if not runtime_db.is_file():
            _fail("runtime_database_missing")
        runtime_uri = f"file:{runtime_db.resolve(strict=True).as_posix()}?mode=ro"
        with production_closing(
            production_sqlite3.connect(runtime_uri, uri=True)
        ) as connection:
            connection.row_factory = production_sqlite3.Row
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                _fail("runtime_integrity_check_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                _fail("runtime_foreign_key_check_failed")
            actual_runtime_tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            if actual_runtime_tables != tuple(sorted(runtime_tables)):
                _fail("runtime_schema_table_set_mismatch")
            runtime_before_counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in runtime_tables
            }
            runtime_before_digests: dict[str, str] = {}
            for table in runtime_tables:
                columns = [
                    row[1]
                    for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    )
                ]
                rows = [
                    {column: row[column] for column in columns}
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                runtime_before_digests[table] = production_canonical_digest(rows)
        runtime_before_files: dict[str, dict[str, Any]] = {}
        for label, path in (
            ("database", runtime_db),
            ("wal", ProductionPath(str(runtime_db) + "-wal")),
            ("shm", ProductionPath(str(runtime_db) + "-shm")),
        ):
            if not path.is_file():
                runtime_before_files[label] = {
                    "exists": False,
                    "size": 0,
                    "sha256": None,
                }
            else:
                file_hash = production_hashlib.sha256()
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        file_hash.update(chunk)
                runtime_before_files[label] = {
                    "exists": True,
                    "size": path.stat().st_size,
                    "sha256": file_hash.hexdigest(),
                }
        runtime_before = {
            "counts": runtime_before_counts,
            "table_digests": runtime_before_digests,
            "files": runtime_before_files,
        }
        runtime_before["snapshot_digest"] = production_canonical_digest(
            runtime_before
        )

        contracts = _build_contracts(resume, base)

        output_parent = output_root.parent
        current = output_parent.absolute()
        while True:
            try:
                if (
                    current.exists() or current.is_symlink()
                ) and production_is_reparse_point(current):
                    _fail("output_parent_reparse_forbidden")
            except OSError:
                _fail("output_parent_reparse_forbidden")
            if current.parent == current:
                break
            current = current.parent
        output_parent.mkdir(parents=True, exist_ok=True)
        try:
            output_root.mkdir(exist_ok=False)
        except FileExistsError:
            _fail("plan_prepare_output_root_exists_do_not_rerun")
        current = output_root.absolute()
        while True:
            try:
                if (
                    current.exists() or current.is_symlink()
                ) and production_is_reparse_point(current):
                    _fail("output_root_reparse_forbidden")
            except OSError:
                _fail("output_root_reparse_forbidden")
            if current.parent == current:
                break
            current = current.parent
        output_stat = output_root.stat()
        output_identity = (output_stat.st_dev, output_stat.st_ino)
        output_anchor_path = output_root / ".output-root-anchor"
        output_anchor_descriptor = production_os.open(
            output_anchor_path,
            production_os.O_RDWR | production_os.O_CREAT | production_os.O_EXCL,
            0o600,
        )
        try:
            production_os.write(
                output_anchor_descriptor,
                b"YunOS v10-F Plan/Prepare production output anchor\n",
            )
            production_os.fsync(output_anchor_descriptor)
            output_anchor_stat = production_os.fstat(output_anchor_descriptor)
            output_anchor_identity = (
                output_anchor_stat.st_dev,
                output_anchor_stat.st_ino,
            )
            output_anchor_path_stat = output_anchor_path.stat()
            if output_anchor_identity != (
                output_anchor_path_stat.st_dev,
                output_anchor_path_stat.st_ino,
            ):
                _fail("output_anchor_identity_changed")
        except BaseException:
            production_os.close(output_anchor_descriptor)
            raise

        sentry = {
            "version": "v10-f-real-plan-prepare-one-shot-sentry-1",
            "acceptance_version": "v10-f-real-plan-prepare-acceptance-1",
            "created_at": base_text,
            "project_id": "project:ayun-english-core",
            "user_authorization_sha256": (
                "ff968831ddaf4e694673272ede56dc7857286b77c1cfddbd908559385282ae11"
            ),
            "resume_package_digest": resume["package_digest"],
            "project_projection_digest": project_before["projection_digest"],
            "operational_before_digest": operational_before["snapshot_digest"],
            "runtime_before_digest": runtime_before["snapshot_digest"],
            "provider_profile_digest": provider["profile_digest"],
            "provider_calls_allowed": False,
            "tool_execution_allowed": False,
            "external_repository_access_allowed": False,
            "do_not_rerun": True,
        }
        sentry["sentry_digest"] = production_canonical_digest(sentry)
        try:
            publication_stat = output_root.stat()
            publication_anchor_stat = output_anchor_path.stat()
            publication_descriptor_stat = production_os.fstat(
                output_anchor_descriptor
            )
            if (
                (publication_stat.st_dev, publication_stat.st_ino)
                != output_identity
                or (
                    publication_anchor_stat.st_dev,
                    publication_anchor_stat.st_ino,
                )
                != output_anchor_identity
                or (
                    publication_descriptor_stat.st_dev,
                    publication_descriptor_stat.st_ino,
                )
                != output_anchor_identity
            ):
                _fail("output_anchor_identity_changed")
            sentry_path = output_root / "one-shot-plan-prepare-sentry.json"
            sentry_descriptor = production_os.open(
                sentry_path,
                production_os.O_WRONLY
                | production_os.O_CREAT
                | production_os.O_EXCL,
                0o600,
            )
            with production_os.fdopen(sentry_descriptor, "wb") as handle:
                handle.write(
                    (
                        production_json.dumps(
                            sentry,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
                handle.flush()
                production_os.fsync(handle.fileno())
            publication_stat = output_root.stat()
            publication_anchor_stat = output_anchor_path.stat()
            publication_descriptor_stat = production_os.fstat(
                output_anchor_descriptor
            )
            if (
                (publication_stat.st_dev, publication_stat.st_ino)
                != output_identity
                or (
                    publication_anchor_stat.st_dev,
                    publication_anchor_stat.st_ino,
                )
                != output_anchor_identity
                or (
                    publication_descriptor_stat.st_dev,
                    publication_descriptor_stat.st_ino,
                )
                != output_anchor_identity
            ):
                _fail("output_anchor_identity_changed")
        except BaseException:
            production_os.close(output_anchor_descriptor)
            raise

        owner = "writer:v10-f-real-plan-prepare"
        token = "v10-f-real-plan-prepare-one-shot-token-0001"
        lease_acquired = False
        persisted: dict[str, Any] | None = None
        failure: BaseException | None = None
        try:
            production_store.acquire_writer_lease(
                owner_id=owner,
                token=token,
                acquired_at=base_text,
                expires_at=(
                    (base + ProductionTimedelta(seconds=120))
                    .astimezone(ProductionTimezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                ),
            )
            lease_acquired = True
            objective_results: dict[str, Any] = {}
            plan_results: dict[str, list[dict[str, Any]]] = {}
            for mode in ("plan", "prepare"):
                objective = contracts.objectives[mode]
                at = contracts.times[f"{mode}_objective"]
                objective_results[mode] = ProductionStateStore(
                    operational_db,
                    trusted_clock=iter((at,)).__next__,
                    allow_production_write=True,
                ).commit_objective(
                    contract=objective.to_dict(),
                    authority=contracts.authority,
                    event=_objective_event(mode, objective, contracts.authority, at),
                    lease_owner_id=owner,
                    lease_token=token,
                    created_at=at,
                )
                caused_by = objective_results[mode]["event_id"]
                plan_results[mode] = []
                for graph in contracts.plans[mode]:
                    at = graph.revisions[-1].created_at
                    plan_result = ProductionPlanningStore(
                        operational_db,
                        trusted_clock=iter((at,)).__next__,
                        allow_production_write=True,
                    ).commit_plan(
                        graph=graph,
                        authority=contracts.authority,
                        event=_plan_event(
                            graph,
                            anchor=contracts.anchors[mode],
                            authority=contracts.authority,
                            caused_by_event_id=caused_by,
                        ),
                        lease_owner_id=owner,
                        lease_token=token,
                    )
                    plan_results[mode].append(plan_result)
                    caused_by = plan_result["event_id"]
                binding = contracts.contexts[mode].bindings[0]
                anchor = contracts.anchors[mode]
                context_at = contracts.times["context"]
                with production_closing(
                    production_store._connect(write=True)
                ) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        production_store._assert_lease(
                            connection,
                            owner_id=owner,
                            token=token,
                            at=context_at,
                        )
                        contracts.authority.authorize_event(
                            project_id="project:ayun-english-core",
                            entity_id=(
                                f"decision:v10-f-english-{objective.agency_level}-objective"
                            ),
                            event_type="decision_approved",
                            at=context_at,
                        )
                        objective_row = connection.execute(
                            "SELECT canonical_digest FROM objective_contracts "
                            "WHERE objective_id=? AND version=?",
                            (objective.objective_id, objective.version),
                        ).fetchone()
                        anchor_row = connection.execute(
                            "SELECT 1 FROM state_source_anchors WHERE anchor_id=?",
                            (anchor.anchor_id,),
                        ).fetchone()
                        if (
                            objective_row is None
                            or objective_row["canonical_digest"]
                            != objective.canonical_digest
                            or anchor_row is None
                        ):
                            _fail("context_binding_source_missing")
                        connection.execute(
                            "INSERT INTO context_bindings("
                            "binding_id,objective_id,objective_version,source_system,"
                            "source_reference,source_digest,maximum_items,maximum_bytes,"
                            "selected_items,canonical_bytes,freshness,contains_secret,"
                            "canonical_binding,binding_digest,source_anchor_id,created_at"
                            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                binding.binding_id,
                                objective.objective_id,
                                objective.version,
                                binding.source_system,
                                binding.source_reference,
                                binding.source_digest,
                                binding.maximum_items,
                                binding.maximum_bytes,
                                binding.selected_items,
                                binding.canonical_bytes,
                                binding.freshness,
                                0,
                                production_canonical_json(binding.to_dict()),
                                binding.binding_digest,
                                anchor.anchor_id,
                                context_at,
                            ),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise

            runs: dict[str, CognitiveRun] = {}
            for mode in ("plan", "prepare"):
                objective = contracts.objectives[mode]
                plan = contracts.plans[mode][1]
                at = contracts.times[f"{mode}_run"]
                run_id = _run_identity(mode, objective, plan)
                checkpoint = production_create_checkpoint(
                    run_id=run_id,
                    checkpoint_sequence=1,
                    safe_to_resume=True,
                    run_status="verifying",
                    authorization_digest=None,
                    objective_digest=objective.canonical_digest,
                    plan_digest=plan.plan_digest,
                    provider_profile_digest=provider["profile_digest"],
                    current_node_ids=(),
                    completed_node_ids=tuple(node.node_id for node in plan.nodes),
                    model_budget_state=production_empty_budget(),
                    runtime_recovery_bindings=(),
                    proposal_ids=(),
                    execution_ids=(),
                    idempotency_keys=(),
                    last_completed_execution_id=None,
                    unsafe_reason_codes=(),
                    steps_used=0,
                    started_at=at,
                    last_activity_at=at,
                    ended_at=None,
                    failure_code=None,
                    recorded_at=at,
                )
                run = production_create_run(
                    run_id=run_id,
                    objective_id=objective.objective_id,
                    objective_version=objective.version,
                    objective_digest=objective.canonical_digest,
                    plan_id=plan.plan_id,
                    plan_version=plan.version,
                    plan_digest=plan.plan_digest,
                    provider_id="deepseek",
                    provider_profile_version=1,
                    provider_profile_digest=provider["profile_digest"],
                    authorization_id=None,
                    authorization_digest=None,
                    status="verifying",
                    checkpoint_sequence=1,
                    last_safe_checkpoint=checkpoint.checkpoint_digest,
                    model_calls_used=0,
                    tokens_used=0,
                    cost_used_microunits=0,
                    steps_used=0,
                    started_at=at,
                    last_activity_at=at,
                    ended_at=None,
                    failure_code=None,
                )
                execution = ProductionExecutionStore(
                    operational_db,
                    trusted_clock=iter((at,)).__next__,
                    allow_production_write=True,
                )
                event = execution._resource_event(
                    source_authority=contracts.authority,
                    project_id="project:ayun-english-core",
                    entity_id=run_id,
                    event_type="cognitive_run_created",
                    actor_kind="system",
                    actor_id="system:yunos-v10-f-plan-prepare",
                    at=at,
                    expires_at=None,
                    payload={
                        "run_id": run_id,
                        "run_digest": run.run_digest,
                        "agency_level": mode,
                        "authorization_id": None,
                        "provider_calls": 0,
                        "tool_proposals": 0,
                        "tool_executions": 0,
                        "acceptance_state": "not_accepted",
                    },
                    idempotency_key=f"idempotency:v10-f-english-{mode}-run",
                    authority_kind="deterministic_observation",
                    source_kind="observed",
                )
                preview = execution.operational.preview_event(
                    authority=contracts.authority,
                    event=event,
                )
                with production_closing(
                    execution.operational._connect(write=True)
                ) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        execution.operational._commit_preview_on_connection(
                            connection,
                            authority=contracts.authority,
                            preview=preview,
                            lease_owner_id=owner,
                            lease_token=token,
                        )
                        execution._insert_run(connection, run)
                        execution._insert_checkpoint(connection, checkpoint)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                runs[mode] = run

            verification_results: dict[str, Any] = {}
            for mode in ("plan", "prepare"):
                objective = contracts.objectives[mode]
                plan = contracts.plans[mode][1]
                binding = contracts.contexts[mode].bindings[0]
                evidence = {
                    "objective_digest": objective.canonical_digest,
                    "plan_digest": plan.plan_digest,
                    "context_binding_digest": binding.binding_digest,
                    "resume_package_digest": binding.source_digest,
                    "provider_calls": 0,
                    "tool_proposals": 0,
                    "tool_executions": 0,
                    "external_repository_accesses": 0,
                    "project_completion_updates": 0,
                    "acceptance_events": 0,
                }
                verification_results[mode] = production_store.record_verification(
                    verification_id=production_make_identity(
                        "verification",
                        {"run_id": runs[mode].run_id, "evidence": evidence},
                    ),
                    entity_id=runs[mode].run_id,
                    event_id=None,
                    verifier_kind="deterministic_system",
                    result="passed",
                    evidence_digest=production_canonical_digest(evidence),
                    details={
                        **evidence,
                        "result_state": "system_verified_not_user_accepted",
                        "user_accepted": False,
                    },
                    recorded_at=contracts.times["verification"],
                    authority=contracts.authority,
                    lease_owner_id=owner,
                    lease_token=token,
                )

            prepare_preview = _prepare_preview(contracts)
            candidate_anchor = _candidate_anchor(contracts)
            definitions: list[
                tuple[str, str, CognitiveRun, dict[str, Any]]
            ] = [
                (
                    "prepare_preview",
                    "prepare_preview",
                    runs["prepare"],
                    prepare_preview,
                )
            ]
            for mode in ("plan", "prepare"):
                definitions.append(
                    (
                        f"{mode}_outcome",
                        "outcome_candidate",
                        runs[mode],
                        {
                            "candidate_version": "v10-f-outcome-candidate-1",
                            "mode": mode,
                            "run_id": runs[mode].run_id,
                            "system_verified": True,
                            "user_accepted": False,
                            "acceptance_state": (
                                "system_verified_not_user_accepted"
                            ),
                            "project_completion_update_allowed": False,
                        },
                    )
                )
            definitions.extend(
                (
                    (
                        "fact",
                        "fact_record",
                        runs["plan"],
                        {
                            "candidate_version": "v10-f-learning-candidate-1",
                            "kind": "fact",
                            "fact": (
                                "真实 v9 Resume Package 的基线、版本和只读外部仓库策略已验证。"
                            ),
                            "long_term_preference": False,
                            "status": "pending",
                        },
                    ),
                    (
                        "observation",
                        "observation",
                        runs["prepare"],
                        {
                            "candidate_version": "v10-f-learning-candidate-1",
                            "kind": "observation",
                            "observation": (
                                "Plan/Prepare 可在零 Provider、零工具和零项目访问下形成可审计草案。"
                            ),
                            "stable_preference": False,
                            "status": "pending",
                        },
                    ),
                    (
                        "workflow",
                        "workflow_candidate",
                        runs["prepare"],
                        {
                            "candidate_version": "v10-f-learning-candidate-1",
                            "kind": "workflow_candidate",
                            "workflow": (
                                "Resume gate -> Objective -> Plan revision -> Preview -> Verification -> Wait acceptance"
                            ),
                            "auto_adopt_allowed": False,
                            "user_approval_required": True,
                            "status": "pending",
                        },
                    ),
                )
            )
            candidate_results: list[dict[str, Any]] = []
            for label, candidate_type, run, payload in definitions:
                candidate_results.append(
                    production_store.create_candidate(
                        candidate_id=production_make_identity(
                            "candidate",
                            {
                                "acceptance_version": (
                                    "v10-f-real-plan-prepare-acceptance-1"
                                ),
                                "label": label,
                                "run_id": run.run_id,
                                "candidate_digest": production_canonical_digest(
                                    payload
                                ),
                            },
                        ),
                        project_id="project:ayun-english-core",
                        entity_id=run.run_id,
                        entity_type="resource",
                        candidate_type=candidate_type,
                        candidate=payload,
                        confidence_ppm=1_000_000,
                        source_anchor=candidate_anchor,
                        created_at=contracts.times["candidates"],
                        expires_at=None,
                        authority=contracts.authority,
                        lease_owner_id=owner,
                        lease_token=token,
                        enforce_authority=True,
                        strict_replay_binding=True,
                    )
                )

            for mode in ("plan", "prepare"):
                at = contracts.times[f"{mode}_waiting"]
                runs[mode] = ProductionExecutionStore(
                    operational_db,
                    trusted_clock=iter((at,)).__next__,
                    allow_production_write=True,
                ).transition_run(
                    run_id=runs[mode].run_id,
                    expected_run_digest=runs[mode].run_digest,
                    next_status="waiting_acceptance",
                    source_authority=contracts.authority,
                    lease_owner_id=owner,
                    lease_token=token,
                )
            persisted = {
                "objective_results": objective_results,
                "plan_results": plan_results,
                "runs": runs,
                "verifications": verification_results,
                "candidates": candidate_results,
                "prepare_preview": prepare_preview,
            }
        except BaseException as exc:
            failure = exc
        finally:
            if lease_acquired:
                try:
                    production_store.release_writer_lease(
                        owner_id=owner,
                        token=token,
                        released_at=contracts.times["release"],
                    )
                except BaseException as release_error:
                    if failure is None:
                        failure = release_error

        result: dict[str, Any] | None = None
        if failure is None:
            try:
                if persisted is None:
                    _fail("plan_prepare_persisted_result_missing")
                with production_closing(
                    production_sqlite3.connect(operational_uri, uri=True)
                ) as connection:
                    connection.row_factory = production_sqlite3.Row
                    if (
                        connection.execute("PRAGMA integrity_check").fetchone()[0]
                        != "ok"
                    ):
                        _fail("operational_integrity_check_failed")
                    if connection.execute("PRAGMA foreign_key_check").fetchall():
                        _fail("operational_foreign_key_check_failed")
                    operational_after_counts = {
                        table: int(
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0]
                        )
                        for table in operational_tables
                    }
                    operational_after_protected: dict[str, str] = {}
                    for table in protected_tables:
                        columns = [
                            row[1]
                            for row in connection.execute(
                                f'PRAGMA table_info("{table}")'
                            )
                        ]
                        rows = [
                            {column: row[column] for column in columns}
                            for row in connection.execute(
                                f'SELECT * FROM "{table}" ORDER BY rowid'
                            )
                        ]
                        operational_after_protected[table] = (
                            production_canonical_digest(rows)
                        )
                operational_after = {
                    "counts": operational_after_counts,
                    "protected_digests": operational_after_protected,
                    "writer_leases": operational_after_counts[
                        "state_writer_leases"
                    ],
                }
                operational_after["snapshot_digest"] = (
                    production_canonical_digest(
                        {
                            "counts": operational_after_counts,
                            "protected_digests": operational_after_protected,
                        }
                    )
                )

                with production_closing(
                    production_sqlite3.connect(runtime_uri, uri=True)
                ) as connection:
                    connection.row_factory = production_sqlite3.Row
                    if (
                        connection.execute("PRAGMA integrity_check").fetchone()[0]
                        != "ok"
                    ):
                        _fail("runtime_integrity_check_failed")
                    if connection.execute("PRAGMA foreign_key_check").fetchall():
                        _fail("runtime_foreign_key_check_failed")
                    runtime_after_counts = {
                        table: int(
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0]
                        )
                        for table in runtime_tables
                    }
                    runtime_after_digests: dict[str, str] = {}
                    for table in runtime_tables:
                        columns = [
                            row[1]
                            for row in connection.execute(
                                f'PRAGMA table_info("{table}")'
                            )
                        ]
                        rows = [
                            {column: row[column] for column in columns}
                            for row in connection.execute(
                                f'SELECT * FROM "{table}" ORDER BY rowid'
                            )
                        ]
                        runtime_after_digests[table] = (
                            production_canonical_digest(rows)
                        )
                runtime_after_files: dict[str, dict[str, Any]] = {}
                for label, path in (
                    ("database", runtime_db),
                    ("wal", ProductionPath(str(runtime_db) + "-wal")),
                    ("shm", ProductionPath(str(runtime_db) + "-shm")),
                ):
                    if not path.is_file():
                        runtime_after_files[label] = {
                            "exists": False,
                            "size": 0,
                            "sha256": None,
                        }
                    else:
                        file_hash = production_hashlib.sha256()
                        with path.open("rb") as handle:
                            while True:
                                chunk = handle.read(1024 * 1024)
                                if not chunk:
                                    break
                                file_hash.update(chunk)
                        runtime_after_files[label] = {
                            "exists": True,
                            "size": path.stat().st_size,
                            "sha256": file_hash.hexdigest(),
                        }
                runtime_after = {
                    "counts": runtime_after_counts,
                    "table_digests": runtime_after_digests,
                    "files": runtime_after_files,
                }
                runtime_after["snapshot_digest"] = production_canonical_digest(
                    runtime_after
                )

                expected_delta = {table: 0 for table in operational_tables}
                expected_delta.update(
                    {
                        "cognitive_run_checkpoints": 4,
                        "cognitive_runs": 2,
                        "context_bindings": 2,
                        "objective_contracts": 2,
                        "plan_edges": 8,
                        "plan_graphs": 4,
                        "plan_nodes": 12,
                        "plan_revisions": 4,
                        "state_candidates": 6,
                        "state_entities": 8,
                        "state_events": 8,
                        "state_lease_events": 2,
                        "state_projections": 8,
                        "state_receipts": 8,
                        "state_source_anchors": 5,
                        "state_verifications": 2,
                    }
                )
                delta = {
                    table: operational_after_counts[table]
                    - operational_before_counts[table]
                    for table in operational_tables
                }
                if delta != expected_delta:
                    _fail("operational_delta_mismatch")
                if operational_after["writer_leases"] != 0:
                    _fail("writer_lease_not_zero")
                for table in protected_tables:
                    if (
                        operational_before_protected[table]
                        != operational_after_protected[table]
                    ):
                        _fail("protected_operational_table_changed")
                if runtime_before != runtime_after:
                    _fail("runtime_delta_not_zero")
                project_after = production_store.get_projection(
                    "project:ayun-english-core"
                )
                if (
                    project_after is None
                    or project_after.get("projection_digest")
                    != project_before.get("projection_digest")
                    or project_after.get("state") != project_before.get("state")
                    or project_after.get("fields") != project_before.get("fields")
                ):
                    _fail("project_projection_changed")
                with production_closing(
                    production_sqlite3.connect(operational_uri, uri=True)
                ) as connection:
                    connection.row_factory = production_sqlite3.Row
                    run_rows = connection.execute(
                        "SELECT run_id,status,authorization_id,model_calls_used,"
                        "tokens_used,cost_used_microunits,steps_used "
                        "FROM cognitive_runs WHERE run_id IN (?,?) ORDER BY run_id",
                        tuple(sorted(contracts.run_ids.values())),
                    ).fetchall()
                    if len(run_rows) != 2 or any(
                        row["status"] != "waiting_acceptance"
                        or row["authorization_id"] is not None
                        or row["model_calls_used"] != 0
                        or row["tokens_used"] != 0
                        or row["cost_used_microunits"] != 0
                        or row["steps_used"] != 0
                        for row in run_rows
                    ):
                        _fail("cognitive_run_acceptance_boundary_mismatch")
                    candidates = connection.execute(
                        "SELECT candidate_type,status FROM state_candidates "
                        "WHERE entity_id IN (?,?) ORDER BY candidate_type,candidate_id",
                        tuple(sorted(contracts.run_ids.values())),
                    ).fetchall()
                    if (
                        [row["candidate_type"] for row in candidates]
                        != sorted(
                            [
                                "prepare_preview",
                                "outcome_candidate",
                                "outcome_candidate",
                                "fact_record",
                                "observation",
                                "workflow_candidate",
                            ]
                        )
                        or any(row["status"] != "pending" for row in candidates)
                    ):
                        _fail("pending_candidate_boundary_mismatch")
                    acceptance_events = connection.execute(
                        "SELECT COUNT(*) FROM state_events WHERE project_id=? "
                        "AND event_type='outcome_accepted'",
                        ("project:ayun-english-core",),
                    ).fetchone()[0]
                    if acceptance_events:
                        _fail("self_acceptance_detected")
                    context_rows = connection.execute(
                        "SELECT source_system,source_digest,contains_secret "
                        "FROM context_bindings WHERE objective_id IN (?,?) "
                        "ORDER BY objective_id",
                        tuple(
                            sorted(
                                item.objective_id
                                for item in contracts.objectives.values()
                            )
                        ),
                    ).fetchall()
                    if len(context_rows) != 2 or any(
                        row["source_system"] != "v9_resume"
                        or row["source_digest"]
                        != contracts.contexts["plan"].bindings[0].source_digest
                        or row["contains_secret"] != 0
                        for row in context_rows
                    ):
                        _fail("context_binding_audit_failed")
                audit = {
                    "operational_delta": delta,
                    "runtime_delta": {table: 0 for table in runtime_tables},
                    "provider_registry_delta": 0,
                    "tool_proposal_delta": 0,
                    "tool_execution_delta": 0,
                    "delegation_delta": 0,
                    "outcome_record_delta": 0,
                    "approval_delta": 0,
                    "project_completion_update_delta": 0,
                    "external_repository_accesses": 0,
                    "writer_lease_final": 0,
                    "run_states": {
                        mode: persisted["runs"][mode].status
                        for mode in ("plan", "prepare")
                    },
                    "acceptance_state": (
                        "system_verified_not_user_accepted"
                    ),
                    "project_projection_digest_unchanged": True,
                }
                result = {
                    "version": "v10-f-real-plan-prepare-acceptance-1",
                    "status": "YUNOS_V10_F_REAL_PLAN_PREPARE_ACCEPTANCE_GO",
                    "completed_at": (
                        ProductionDateTime.now(ProductionTimezone.utc)
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z")
                    ),
                    "project": {
                        "project_id": "project:ayun-english-core",
                        "authoritative_baseline": (
                            "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53"
                        ),
                        "current_version": "v0.1.1",
                        "external_repository_policy": (
                            "read_only_observation_only"
                        ),
                        "resume_package_digest": resume["package_digest"],
                        "external_repository_accesses": 0,
                        "project_completion_updates": 0,
                    },
                    "objectives": {
                        mode: {
                            "objective_id": contracts.objectives[
                                mode
                            ].objective_id,
                            "objective_digest": contracts.objectives[
                                mode
                            ].canonical_digest,
                            "agency_level": mode,
                            "cognitive_stance": "continue",
                            "authority_requested": False,
                        }
                        for mode in ("plan", "prepare")
                    },
                    "plans": {
                        mode: {
                            "plan_id": contracts.plans[mode][1].plan_id,
                            "version": 2,
                            "plan_digest": contracts.plans[mode][1].plan_digest,
                            "revision_digest": contracts.plans[mode][
                                1
                            ].revisions[-1].revision_digest,
                            "node_count": 3,
                            "edge_count": 2,
                            "write_scope": [],
                            "capabilities": [],
                        }
                        for mode in ("plan", "prepare")
                    },
                    "context_bindings": {
                        mode: contracts.contexts[mode].bindings[0].to_dict()
                        for mode in ("plan", "prepare")
                    },
                    "authorization_boundary": {
                        "authorization_artifacts_created": 0,
                        "execution_authorized": False,
                        "merge": False,
                        "tag": False,
                        "push": False,
                        "network": False,
                        "external_project_access": False,
                        "user_acceptance_required": True,
                    },
                    "prepare_preview": persisted["prepare_preview"],
                    "runs": {
                        mode: {
                            "run_id": persisted["runs"][mode].run_id,
                            "status": persisted["runs"][mode].status,
                            "authorization_id": None,
                            "model_calls": 0,
                            "tool_proposals": 0,
                            "tool_executions": 0,
                            "acceptance_state": (
                                "system_verified_not_user_accepted"
                            ),
                        }
                        for mode in ("plan", "prepare")
                    },
                    "verifications": persisted["verifications"],
                    "learning_candidates": {
                        "count": 3,
                        "types": [
                            "fact_record",
                            "observation",
                            "workflow_candidate",
                        ],
                        "status": "pending",
                        "long_term_profile_updated": False,
                    },
                    "provider": {
                        "provider_id": "deepseek",
                        "profile_version": 1,
                        "profile_digest": provider["profile_digest"],
                        "binding_purpose": (
                            "schema_required_persistence_reference_only"
                        ),
                        "calls": 0,
                        "runtime_delta": 0,
                        "hidden_switch": False,
                        "secret_material_read": False,
                    },
                    "audit": audit,
                    "one_shot": {
                        "sentry": "one-shot-plan-prepare-sentry.json",
                        "do_not_rerun": True,
                        "failure_reconcile_available": True,
                    },
                }
                result["report_digest"] = production_canonical_digest(result)

                publication_stat = output_root.stat()
                publication_anchor_stat = output_anchor_path.stat()
                publication_descriptor_stat = production_os.fstat(
                    output_anchor_descriptor
                )
                if (
                    (publication_stat.st_dev, publication_stat.st_ino)
                    != output_identity
                    or (
                        publication_anchor_stat.st_dev,
                        publication_anchor_stat.st_ino,
                    )
                    != output_anchor_identity
                    or (
                        publication_descriptor_stat.st_dev,
                        publication_descriptor_stat.st_ino,
                    )
                    != output_anchor_identity
                ):
                    _fail("output_anchor_identity_changed")
                result_path = output_root / "real-plan-prepare-acceptance.json"
                result_descriptor = production_os.open(
                    result_path,
                    production_os.O_WRONLY
                    | production_os.O_CREAT
                    | production_os.O_EXCL,
                    0o600,
                )
                with production_os.fdopen(result_descriptor, "wb") as handle:
                    handle.write(
                        (
                            production_json.dumps(
                                result,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    handle.flush()
                    production_os.fsync(handle.fileno())
                publication_stat = output_root.stat()
                publication_anchor_stat = output_anchor_path.stat()
                publication_descriptor_stat = production_os.fstat(
                    output_anchor_descriptor
                )
                if (
                    (publication_stat.st_dev, publication_stat.st_ino)
                    != output_identity
                    or (
                        publication_anchor_stat.st_dev,
                        publication_anchor_stat.st_ino,
                    )
                    != output_anchor_identity
                    or (
                        publication_descriptor_stat.st_dev,
                        publication_descriptor_stat.st_ino,
                    )
                    != output_anchor_identity
                ):
                    _fail("output_anchor_identity_changed")
            except BaseException as exc:
                failure = exc

        if failure is not None:
            reason = getattr(
                failure,
                "reason_code",
                "plan_prepare_persistence_or_publication_failed",
            )
            reconcile_store = ProductionOperationalStore(
                operational_db,
                allow_production_write=True,
            )
            reconcile_owner = "writer:v10-f-plan-prepare-reconcile"
            reconcile_token = "v10-f-plan-prepare-reconcile-token-0001"
            reconcile_acquired = False
            states: dict[str, str] = {}
            reconcile_errors: list[str] = []
            try:
                reconcile_store.acquire_writer_lease(
                    owner_id=reconcile_owner,
                    token=reconcile_token,
                    acquired_at=contracts.times["recovery"],
                    expires_at=(
                        ProductionDateTime.fromisoformat(
                            contracts.times["recovery"].replace("Z", "+00:00")
                        )
                        + ProductionTimedelta(seconds=60)
                    )
                    .astimezone(ProductionTimezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                )
                reconcile_acquired = True
                for mode, run_id in contracts.run_ids.items():
                    try:
                        recovery_at = contracts.times["recovery"]
                        recovery_execution = ProductionExecutionStore(
                            operational_db,
                            trusted_clock=iter((recovery_at,)).__next__,
                            allow_production_write=True,
                        )
                        run = recovery_execution.get_run(run_id)
                        if run.status in {"planning", "verifying"}:
                            run = recovery_execution.transition_run(
                                run_id=run.run_id,
                                expected_run_digest=run.run_digest,
                                next_status="recovery_required",
                                source_authority=contracts.authority,
                                lease_owner_id=reconcile_owner,
                                lease_token=reconcile_token,
                                failure_code=str(reason),
                            )
                        elif run.status == "waiting_acceptance":
                            run = recovery_execution.transition_run(
                                run_id=run.run_id,
                                expected_run_digest=run.run_digest,
                                next_status="failed",
                                source_authority=contracts.authority,
                                lease_owner_id=reconcile_owner,
                                lease_token=reconcile_token,
                                failure_code=str(reason),
                            )
                        states[mode] = run.status
                    except ProductionOperationalStateError as exc:
                        if exc.reason_code == "cognitive_run_not_found":
                            states[mode] = "not_created"
                        else:
                            reconcile_errors.append(exc.reason_code)
            except (ProductionOperationalStateError, PlanPrepareAcceptanceError) as exc:
                reconcile_errors.append(
                    getattr(exc, "reason_code", "reconcile_failed")
                )
            finally:
                if reconcile_acquired:
                    try:
                        release_at = (
                            ProductionDateTime.fromisoformat(
                                contracts.times["recovery"].replace(
                                    "Z", "+00:00"
                                )
                            )
                            + ProductionTimedelta(seconds=1)
                        )
                        reconcile_store.release_writer_lease(
                            owner_id=reconcile_owner,
                            token=reconcile_token,
                            released_at=(
                                release_at.astimezone(ProductionTimezone.utc)
                                .isoformat(timespec="microseconds")
                                .replace("+00:00", "Z")
                            ),
                        )
                    except ProductionOperationalStateError as exc:
                        reconcile_errors.append(exc.reason_code)
            try:
                lease_zero = reconcile_store.read_status()["lease"] is None
            except ProductionOperationalStateError:
                lease_zero = False
            recovery = {
                "attempted": True,
                "run_states": states,
                "writer_lease_zero": lease_zero,
                "errors": sorted(set(reconcile_errors)),
                "recovery_required": bool(reconcile_errors) or not lease_zero,
            }
            safe_reason = (
                reason
                if isinstance(reason, str)
                and production_re.fullmatch(r"[a-z][a-z0-9_]{2,127}", reason)
                else "plan_prepare_persistence_or_publication_failed"
            )
            failure_payload = {
                "version": "v10-f-real-plan-prepare-acceptance-1",
                "status": "NO_GO_DO_NOT_RERUN",
                "reason_code": safe_reason,
                "failed_at": (
                    ProductionDateTime.now(ProductionTimezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                ),
                "recovery": recovery,
                "provider_calls": 0,
                "tool_executions": 0,
                "external_repository_accesses": 0,
                "sanitized": True,
                "do_not_rerun": True,
            }
            try:
                publication_stat = output_root.stat()
                publication_anchor_stat = output_anchor_path.stat()
                publication_descriptor_stat = production_os.fstat(
                    output_anchor_descriptor
                )
                if (
                    (publication_stat.st_dev, publication_stat.st_ino)
                    != output_identity
                    or (
                        publication_anchor_stat.st_dev,
                        publication_anchor_stat.st_ino,
                    )
                    != output_anchor_identity
                    or (
                        publication_descriptor_stat.st_dev,
                        publication_descriptor_stat.st_ino,
                    )
                    != output_anchor_identity
                ):
                    _fail("output_anchor_identity_changed")
                failure_path = output_root / "real-plan-prepare-failure.json"
                failure_descriptor = production_os.open(
                    failure_path,
                    production_os.O_WRONLY
                    | production_os.O_CREAT
                    | production_os.O_EXCL,
                    0o600,
                )
                with production_os.fdopen(failure_descriptor, "wb") as handle:
                    handle.write(
                        (
                            production_json.dumps(
                                failure_payload,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    handle.flush()
                    production_os.fsync(handle.fileno())
            except BaseException:
                pass
            try:
                production_os.close(output_anchor_descriptor)
            except OSError:
                pass
            raise failure

        if result is None:
            try:
                production_os.close(output_anchor_descriptor)
            except OSError:
                pass
            _fail("plan_prepare_result_missing")
        production_os.close(output_anchor_descriptor)
        return result


def run_production_acceptance(
    *, confirm_user_authorization_sha256: str
) -> dict[str, Any]:
    """Non-injectable production entry.  Every path and identity is code-owned."""

    return _FixedProductionExecutor().run(
        confirm_user_authorization_sha256=confirm_user_authorization_sha256
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot YunOS v10-F real Plan/Prepare acceptance. Production DB, "
            "project, output, Provider reference and transport boundaries are fixed."
        )
    )
    parser.add_argument("command", choices=("execute",))
    parser.add_argument(
        "--confirm-user-authorization-sha256",
        choices=(USER_AUTHORIZATION_SHA256,),
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command != "execute":
        _fail("execute_command_required")
    try:
        result = run_production_acceptance(
            confirm_user_authorization_sha256=(
                arguments.confirm_user_authorization_sha256
            )
        )
    except (PlanPrepareAcceptanceError, OperationalStateError, OSError, sqlite3.Error) as exc:
        reason = getattr(exc, "reason_code", "real_plan_prepare_acceptance_failed")
        print(
            canonical_json(
                {
                    "version": ACCEPTANCE_VERSION,
                    "status": "NO_GO",
                    "reason_code": reason,
                    "provider_calls": 0,
                    "tool_executions": 0,
                    "external_repository_accesses": 0,
                }
            )
        )
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
