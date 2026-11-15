"""Build, verify, and restore YunOS v10 release recovery packages.

The builder opens the five source SQLite databases read-only and uses SQLite's
online backup API.  WAL/SHM files are copied only as forensic evidence; restore
always uses the consistent database images under ``databases/``.  Provider
credentials and opaque secret references are deliberately excluded.

All writable work is first materialized in a tool-owned staging directory.
Existing package or restore destinations are never overwritten, and cleanup is
limited to staging directories carrying the nonce written by this process.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_provider import (  # noqa: E402
    CognitiveProviderError,
    ProviderProfile,
)
from memory.operational_state_models import (  # noqa: E402
    OperationalStateError,
    canonical_digest,
    canonical_json,
)
from memory.operational_state_store import (  # noqa: E402
    OperationalStateStore,
    schema_fingerprint,
)


CONTRACT_VERSION = "yunos-v10-release-recovery-1"
RELEASE_NAME = "YunOS v10.0 — Bounded Cognitive Agency"
RECOVERY_PHASES = ("preproduction", "final")
DEFAULT_DATABASES = {
    "main": Path("data/ayunos_chatgpt.db"),
    "profile": Path("data/profile/approved_profile.db"),
    "semantic": Path("data/semantic/ayunos_semantic.db"),
    "runtime": Path("data/runtime/ayunos_runtime.db"),
    "operational": Path("data/operational/ayunos_operational_state.db"),
}
BACKUP_FILENAMES = {
    "main": "ayunos_chatgpt.db",
    "profile": "approved_profile.db",
    "semantic": "ayunos_semantic.db",
    "runtime": "ayunos_runtime.db",
    "operational": "ayunos_operational_state.db",
}
DEFAULT_ASSET_ROOT = Path("data/ingestion_assets")
DEFAULT_RELEASE_REPORT_ROOT = Path("docs/version_reports")
PRODUCTION_WEB_PORTS = frozenset({8765, 8766})
_REPARSE_ATTRIBUTE = 0x400
_MAX_JSON_BYTES = 16 * 1024 * 1024
_OWNER_MARKER = ".yunos-v10-recovery-owned.json"
OWNERSHIP_MARKER_VERSION = "yunos-v10-recovery-ownership-marker-1"
_SOURCE_BINDINGS = frozenset({"fixed_project_root", "test_fixture"})
_SEALED_MARKER_FIELDS = frozenset(
    {
        "marker_schema_version",
        "contract_version",
        "purpose",
        "state",
        "nonce",
        "phase",
        "manifest_digest",
        "backup_set_digest",
        "ownership_identity_digest",
        "runtime_digest",
        "marker_digest",
    }
)
_SENSITIVE_FIELD = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|authorization|"
    r"credential|password|private[_-]?key|secret|token[_-]?value)(?:$|[_-])"
)
_SECRET_LITERAL_PATTERNS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/=-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)
_PROVIDER_TEMPLATE_FIELDS = (
    "contract_version",
    "provider_id",
    "profile_version",
    "display_name",
    "adapter_kind",
    "adapter_version",
    "endpoint",
    "model",
    "capability_flags",
    "tool_calling_support",
    "structured_output_support",
    "context_limit_tokens",
    "timeout_ms",
    "reasoning_options",
    "cost_metadata",
    "enabled",
    "health_state",
    "health_reason",
    "profile_digest",
)
_SAFE_SECRET_METADATA_FIELDS = frozenset(
    {
        "secret_material_included",
        "secret_references_included",
        "requires_local_secret_configuration",
    }
)


class RecoveryError(RuntimeError):
    """A fail-closed package or restore validation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _validate_sha256(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RecoveryError(reason)
    return value


def _validate_phase(value: Any) -> str:
    if value not in RECOVERY_PHASES:
        raise RecoveryError("invalid_recovery_phase")
    return str(value)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        _reject_reparse(path)
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256": None}
    _reject_hardlink_file(path)
    before = path.stat()
    result = {
        "exists": True,
        "size": before.st_size,
        "sha256": _hash_file(path),
    }
    after = path.stat()
    if (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_nlink),
        int(before.st_size),
        int(before.st_mtime_ns),
    ) != (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_nlink),
        int(after.st_size),
        int(after.st_mtime_ns),
    ):
        raise RecoveryError("file_changed_during_hash")
    return result


def _sqlite_file_gates(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "db": _file_gate(path),
        "wal": _file_gate(Path(str(path) + "-wal")),
        "shm": _file_gate(Path(str(path) + "-shm")),
    }


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        raise RecoveryError("path_identity_unverifiable") from exc
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _reject_reparse(path: Path) -> None:
    if _is_reparse(path):
        raise RecoveryError("reparse_path_forbidden")


def _reject_hardlink_file(path: Path) -> None:
    """Reject aliases whose content identity extends outside the named scope."""

    try:
        if path.is_file() and int(path.stat().st_nlink) != 1:
            raise RecoveryError("hardlink_path_forbidden")
    except OSError as exc:
        raise RecoveryError("path_identity_unverifiable") from exc


def _reject_unsafe_path_shape(path: Path) -> None:
    raw = os.fspath(path)
    windows = raw.replace("/", "\\")
    if (
        not raw
        or "\x00" in raw
        or windows.startswith("\\\\")
        or windows.startswith("\\?\\")
        or windows.startswith("\\.\\")
    ):
        raise RecoveryError("unsafe_path_shape")
    parts = Path(raw).parts
    for part in parts[1:]:
        if ":" in part:
            raise RecoveryError("alternate_data_stream_forbidden")


def _reject_reparse_chain(path: Path) -> None:
    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        if current.exists() or current.is_symlink():
            _reject_reparse(current)
        parent = current.parent
        if parent == current:
            break
        current = parent


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_existing_root(path: Path, *, directory: bool) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    _reject_unsafe_path_shape(lexical)
    if not lexical.exists():
        raise RecoveryError("required_path_missing")
    _reject_reparse_chain(lexical)
    resolved = lexical.resolve(strict=True)
    if directory and not resolved.is_dir():
        raise RecoveryError("required_directory_missing")
    if not directory and not resolved.is_file():
        raise RecoveryError("required_file_missing")
    if not directory:
        _reject_hardlink_file(resolved)
    return resolved


def _safe_descendant(root: Path, relative: Path, *, directory: bool) -> Path:
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise RecoveryError("relative_path_required")
    current = root
    for component in relative.parts:
        if component in {"", ".", ".."} or ":" in component:
            raise RecoveryError("unsafe_relative_path")
        current = current / component
        if not current.exists():
            raise RecoveryError("required_path_missing")
        _reject_reparse(current)
    resolved = current.resolve(strict=True)
    if not _is_within(resolved, root):
        raise RecoveryError("path_escape_detected")
    if directory and not resolved.is_dir():
        raise RecoveryError("required_directory_missing")
    if not directory and not resolved.is_file():
        raise RecoveryError("required_file_missing")
    if not directory:
        _reject_hardlink_file(resolved)
    return resolved


def _directory_identity(path: Path) -> tuple[int, int]:
    """Return the stable filesystem identity used to guard one parent directory."""

    resolved = _safe_existing_root(path, directory=True)
    details = resolved.stat()
    return int(details.st_dev), int(details.st_ino)


def _assert_directory_identity(
    path: Path, expected: tuple[int, int]
) -> None:
    """Fail closed if a destination parent was replaced during materialization."""

    if _directory_identity(path) != expected:
        raise RecoveryError("destination_parent_identity_changed")


def _assert_owned_target_identity(
    path: Path, expected: tuple[int, int]
) -> None:
    """Require the materialized target to remain the owned staging object."""

    try:
        actual = _directory_identity(path)
    except (OSError, RecoveryError):
        raise RecoveryError("destination_target_identity_changed") from None
    if actual != expected:
        raise RecoveryError("destination_target_identity_changed")


def _safe_new_destination(
    path: Path,
) -> tuple[Path, Path, tuple[int, int]]:
    lexical = Path(os.path.abspath(os.fspath(path)))
    _reject_unsafe_path_shape(lexical)
    if lexical.exists() or lexical.is_symlink():
        raise RecoveryError("destination_exists")
    parent = lexical.parent
    if not parent.is_dir():
        raise RecoveryError("destination_parent_missing")
    _reject_reparse_chain(parent)
    resolved_parent = parent.resolve(strict=True)
    canonical_target = resolved_parent / lexical.name
    if canonical_target.exists() or canonical_target.is_symlink():
        raise RecoveryError("destination_exists")
    parent_identity = _directory_identity(resolved_parent)
    return canonical_target, resolved_parent, parent_identity


def _create_owned_staging(
    parent: Path, purpose: str
) -> tuple[Path, str, tuple[int, int]]:
    nonce = uuid.uuid4().hex
    staging = Path(
        tempfile.mkdtemp(prefix=f".yunos-v10-{purpose}-", dir=parent)
    )
    marker = {
        "marker_schema_version": OWNERSHIP_MARKER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "purpose": purpose,
        "state": "staging",
        "nonce": nonce,
    }
    (staging / _OWNER_MARKER).write_text(
        canonical_json(marker) + "\n", encoding="utf-8"
    )
    details = staging.stat()
    return staging, nonce, (int(details.st_dev), int(details.st_ino))


def _ownership_identity_digest(*, purpose: str, nonce: str) -> str:
    if purpose not in {"package", "restore"} or re.fullmatch(
        r"[0-9a-f]{32}", nonce
    ) is None:
        raise RecoveryError("ownership_marker_identity_invalid")
    return canonical_digest(
        {
            "marker_schema_version": OWNERSHIP_MARKER_VERSION,
            "contract_version": CONTRACT_VERSION,
            "purpose": purpose,
            "nonce": nonce,
        }
    )


def _sealed_owner_marker(
    *,
    purpose: str,
    nonce: str,
    phase: str,
    manifest_digest: str,
    backup_set_digest: str,
    runtime_digest: str | None,
) -> dict[str, Any]:
    if purpose == "package" and runtime_digest is not None:
        raise RecoveryError("ownership_marker_runtime_binding_invalid")
    if purpose == "restore":
        _validate_sha256(
            runtime_digest, "ownership_marker_runtime_binding_invalid"
        )
    marker: dict[str, Any] = {
        "marker_schema_version": OWNERSHIP_MARKER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "purpose": purpose,
        "state": "sealed",
        "nonce": nonce,
        "phase": _validate_phase(phase),
        "manifest_digest": _validate_sha256(
            manifest_digest, "ownership_marker_manifest_binding_invalid"
        ),
        "backup_set_digest": _validate_sha256(
            backup_set_digest, "ownership_marker_backup_binding_invalid"
        ),
        "ownership_identity_digest": _ownership_identity_digest(
            purpose=purpose, nonce=nonce
        ),
        "runtime_digest": runtime_digest,
    }
    marker["marker_digest"] = canonical_digest(marker)
    return marker


def _write_sealed_owner_marker(
    root: Path,
    *,
    purpose: str,
    nonce: str,
    phase: str,
    manifest_digest: str,
    backup_set_digest: str,
    runtime_digest: str | None,
) -> dict[str, Any]:
    marker_path = root / _OWNER_MARKER
    try:
        existing = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RecoveryError("ownership_staging_marker_invalid") from None
    if existing != {
        "marker_schema_version": OWNERSHIP_MARKER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "purpose": purpose,
        "state": "staging",
        "nonce": nonce,
    }:
        raise RecoveryError("ownership_staging_marker_invalid")
    marker = _sealed_owner_marker(
        purpose=purpose,
        nonce=nonce,
        phase=phase,
        manifest_digest=manifest_digest,
        backup_set_digest=backup_set_digest,
        runtime_digest=runtime_digest,
    )
    marker_path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
    return marker


def _validate_sealed_owner_marker(
    root: Path,
    *,
    purpose: str,
    phase: str,
    manifest_digest: str,
    backup_set_digest: str,
    ownership_identity_digest: str,
    runtime_digest: str | None,
) -> dict[str, Any]:
    marker_path = _safe_descendant(root, Path(_OWNER_MARKER), directory=False)
    marker = _read_json(marker_path)
    if set(marker) != _SEALED_MARKER_FIELDS:
        raise RecoveryError("ownership_marker_shape_invalid")
    nonce = marker.get("nonce")
    if not isinstance(nonce, str):
        raise RecoveryError("ownership_marker_identity_invalid")
    expected = _sealed_owner_marker(
        purpose=purpose,
        nonce=nonce,
        phase=phase,
        manifest_digest=manifest_digest,
        backup_set_digest=backup_set_digest,
        runtime_digest=runtime_digest,
    )
    if (
        marker != expected
        or marker.get("ownership_identity_digest")
        != ownership_identity_digest
    ):
        raise RecoveryError("ownership_marker_binding_mismatch")
    if _contains_secret_literal(marker):
        raise RecoveryError("ownership_marker_secret_detected")
    return marker


def _cleanup_owned_staging(
    path: Path,
    nonce: str,
    identity: tuple[int, int],
) -> None:
    marker_path = path / _OWNER_MARKER
    try:
        _reject_reparse(path)
        details = path.stat()
        if (int(details.st_dev), int(details.st_ino)) != identity:
            return
        _reject_reparse(marker_path)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, RecoveryError):
        return
    if (
        marker.get("contract_version") == CONTRACT_VERSION
        and marker.get("nonce") == nonce
        and path.name.startswith(".yunos-v10-")
    ):
        shutil.rmtree(path)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _typed_value(value: Any) -> bytes:
    if value is None:
        return b"n:"
    if isinstance(value, bytes):
        return b"b:" + hashlib.sha256(value).hexdigest().encode("ascii")
    if isinstance(value, str):
        return b"s:" + value.encode("utf-8")
    if isinstance(value, int):
        return b"i:" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f:" + value.hex().encode("ascii")
    return b"x:" + repr(value).encode("utf-8")


def _table_content_digest(
    connection: sqlite3.Connection, table: str
) -> tuple[int, str]:
    row_hashes: list[str] = []
    count = 0
    for row in connection.execute("SELECT * FROM " + _quote(table)):
        digest = hashlib.sha256()
        for value in row:
            encoded = _typed_value(value)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        row_hashes.append(digest.hexdigest())
        count += 1
    row_hashes.sort()
    return count, hashlib.sha256(
        "\n".join(row_hashes).encode("ascii")
    ).hexdigest()


def _schema_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
        )
    ]


def audit_database(label: str, path: Path) -> dict[str, Any]:
    """Audit a SQLite database with WAL-aware read-only semantics."""
    path = _safe_existing_root(path, directory=False)
    started = time.perf_counter()
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_key_violations = list(
            connection.execute("PRAGMA foreign_key_check")
        )
        schema_digest = canonical_digest(_schema_rows(connection))
        tables: dict[str, dict[str, Any]] = {}
        for table in _table_names(connection):
            count, content_digest = _table_content_digest(connection, table)
            tables[table] = {
                "row_count": count,
                "content_digest": content_digest,
            }
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    logical = {
        "schema_digest": schema_digest,
        "user_version": user_version,
        "tables": tables,
    }
    return {
        "label": label,
        "file_gate": _file_gate(path),
        "schema_digest": schema_digest,
        "user_version": user_version,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_key_violations),
        "tables": tables,
        "logical_digest": canonical_digest(logical),
        "audit_seconds": round(time.perf_counter() - started, 6),
    }


def _validate_audit_match(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> None:
    if expected.get("integrity_check") != ["ok"]:
        raise RecoveryError("source_integrity_failed")
    if actual.get("integrity_check") != ["ok"]:
        raise RecoveryError("restored_integrity_failed")
    if expected.get("foreign_key_violation_count") != 0:
        raise RecoveryError("source_foreign_key_failed")
    if actual.get("foreign_key_violation_count") != 0:
        raise RecoveryError("restored_foreign_key_failed")
    for key in ("schema_digest", "logical_digest", "user_version"):
        if expected.get(key) != actual.get(key):
            raise RecoveryError("restored_" + key + "_mismatch")


def _online_backup(source: Path, target: Path) -> None:
    uri = source.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        source_connection.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_connection.execute("PRAGMA journal_mode=DELETE")
            target_connection.commit()


def _copy_sidecars(source: Path, target: Path) -> dict[str, Any]:
    target.mkdir(parents=True)
    result: dict[str, Any] = {}
    for suffix, key in (("-wal", "wal"), ("-shm", "shm")):
        source_path = Path(str(source) + suffix)
        if source_path.is_symlink():
            raise RecoveryError("reparse_path_forbidden")
        before = _file_gate(source_path)
        if before["exists"]:
            _reject_reparse(source_path)
            destination = target / (source.name + suffix)
            shutil.copy2(source_path, destination)
            packaged = _file_gate(destination)
            after = _file_gate(source_path)
            if before != after or packaged != before:
                raise RecoveryError("sqlite_sidecar_changed_during_copy")
            relative = destination.name
        else:
            packaged = {"exists": False, "size": 0, "sha256": None}
            relative = None
        result[key] = {
            "source": before,
            "packaged": packaged,
            "packaged_filename": relative,
        }
    return result


def _iter_tree_files(root: Path) -> Iterable[Path]:
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        _reject_reparse(directory_path)
        checked_names: list[str] = []
        for name in sorted(names):
            child = directory_path / name
            _reject_reparse(child)
            if not child.is_dir():
                raise RecoveryError("asset_tree_entry_invalid")
            checked_names.append(name)
        names[:] = checked_names
        for name in sorted(filenames):
            child = directory_path / name
            _reject_reparse(child)
            if not child.is_file():
                raise RecoveryError("asset_tree_entry_invalid")
            _reject_hardlink_file(child)
            yield child


def audit_tree(root: Path) -> dict[str, Any]:
    root = _safe_existing_root(root, directory=True)
    rows: list[str] = []
    total = 0
    for path in _iter_tree_files(root):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        rows.append(relative + "|" + str(size) + "|" + _hash_file(path))
    return {
        "file_count": len(rows),
        "total_size": total,
        "tree_sha256": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(),
    }


def _release_report_sources(project_root: Path) -> tuple[Path, list[Path]]:
    report_root = _safe_descendant(
        project_root, DEFAULT_RELEASE_REPORT_ROOT, directory=True
    )
    reports = [
        path
        for path in _iter_tree_files(report_root)
        if path.suffix.lower() == ".md"
        and path.name.lower().startswith("yunos_v10")
    ]
    reports.sort(key=lambda path: path.relative_to(report_root).as_posix())
    if not reports:
        raise RecoveryError("v10_release_reports_missing")
    for path in reports:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RecoveryError("v10_release_report_invalid") from exc
        if _contains_secret_literal(text):
            raise RecoveryError("v10_release_report_secret_detected")
    return report_root, reports


def _audit_release_report_sources(
    source_root: Path, sources: Iterable[Path]
) -> tuple[dict[str, Any], list[str]]:
    rows: list[str] = []
    files: list[str] = []
    total_size = 0
    for source in sources:
        relative = source.relative_to(source_root).as_posix()
        gate = _file_gate(source)
        if gate["exists"] is not True:
            raise RecoveryError("v10_release_report_changed_during_audit")
        total_size += int(gate["size"])
        files.append(relative)
        rows.append(relative + "|" + str(gate["size"]) + "|" + str(gate["sha256"]))
    return (
        {
            "file_count": len(rows),
            "total_size": total_size,
            "tree_sha256": hashlib.sha256(
                "\n".join(rows).encode("utf-8")
            ).hexdigest(),
        },
        files,
    )


def _copy_release_reports(
    source_root: Path, sources: Iterable[Path], destination: Path
) -> dict[str, Any]:
    sources = list(sources)
    destination.mkdir()
    for source in sources:
        relative = source.relative_to(source_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        gate = _file_gate(source)
        if _file_gate(target) != gate:
            raise RecoveryError("v10_release_report_copy_mismatch")
    source_audit, source_files = _audit_release_report_sources(
        source_root, sources
    )
    packaged_audit = audit_tree(destination)
    if source_audit != packaged_audit:
        raise RecoveryError("v10_release_report_copy_mismatch")
    return {
        "included": True,
        "source_relative_path": DEFAULT_RELEASE_REPORT_ROOT.as_posix(),
        "backup_relative_path": "release_reports",
        "source_audit": source_audit,
        "backup_audit": packaged_audit,
        "files": source_files,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    _reject_reparse(path)
    _reject_hardlink_file(path)
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise RecoveryError("recovery_json_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery_json_invalid") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery_json_invalid")
    return value


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rows_as_dicts(
    connection: sqlite3.Connection, query: str
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query).fetchall()]


def _cognitive_summary(database: Path, *, phase: str) -> dict[str, Any]:
    selected_phase = _validate_phase(phase)
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        core_required = (
            "operational_schema",
            "state_projections",
            "delegation_contracts",
            "outcome_records",
        )
        if not all(_table_exists(connection, table) for table in core_required):
            raise RecoveryError("operational_core_schema_missing")
        operational_row = connection.execute(
            "SELECT marker,schema_version,schema_digest,writer_default_enabled "
            "FROM operational_schema WHERE singleton=1"
        ).fetchone()
        if (
            operational_row is None
            or operational_row[0] != "yunos-operational-state-9001"
            or operational_row[1] != 9001
            or operational_row[3] != 0
            or operational_row[2] != schema_fingerprint(connection)
        ):
            raise RecoveryError("operational_core_schema_marker_mismatch")
        cognitive_exists = _table_exists(connection, "cognitive_schema")
        if selected_phase == "preproduction":
            if cognitive_exists:
                raise RecoveryError("recovery_phase_schema_mismatch")
            availability = "not_migrated"
            schema_marker = None
            schema_generation = None
            cognitive_schema_digest = None
            objectives = None
            plans = None
            runs = None
        else:
            cognitive_required = (
                "cognitive_schema",
                "objective_contracts",
                "plan_graphs",
                "plan_nodes",
                "plan_edges",
                "provider_registry",
                "cognitive_runs",
            )
            if not all(
                _table_exists(connection, table) for table in cognitive_required
            ):
                raise RecoveryError("v10_cognitive_schema_missing")
            schema_row = connection.execute(
                "SELECT marker,schema_generation,schema_digest,contract_version,"
                "writer_default_enabled FROM cognitive_schema WHERE singleton=1"
            ).fetchone()
            if (
                schema_row is None
                or schema_row[0] != "yunos-bounded-cognitive-agency-10001"
                or schema_row[1] != 10001
                or schema_row[3] != 1
                or schema_row[4] != 0
                or schema_row[2] != operational_row[2]
            ):
                raise RecoveryError("v10_cognitive_schema_marker_mismatch")
            availability = "available"
            schema_marker = schema_row[0]
            schema_generation = schema_row[1]
            cognitive_schema_digest = schema_row[2]
            objectives = _rows_as_dicts(
                connection,
                "SELECT objective_id,version,project_id,agency_level,"
                "cognitive_stance,clarification_required,canonical_digest,"
                "created_at FROM objective_contracts "
                "ORDER BY objective_id,version",
            )
            plans = _rows_as_dicts(
                connection,
                "SELECT g.plan_id,g.version,g.objective_id,g.objective_version,"
                "g.objective_digest,g.plan_digest,g.created_at,"
                "(SELECT COUNT(*) FROM plan_nodes n WHERE n.plan_id=g.plan_id "
                "AND n.plan_version=g.version) AS node_count,"
                "(SELECT COUNT(*) FROM plan_edges e WHERE e.plan_id=g.plan_id "
                "AND e.plan_version=g.version) AS edge_count "
                "FROM plan_graphs g ORDER BY g.plan_id,g.version",
            )
            runs = _rows_as_dicts(
                connection,
                "SELECT run_id,objective_id,objective_version,plan_id,"
                "plan_version,provider_id,provider_profile_version,"
                "authorization_id,status,checkpoint_sequence,"
                "last_safe_checkpoint_digest,model_calls_used,tokens_used,"
                "cost_used_microunits,steps_used,started_at,last_activity_at,"
                "ended_at,failure_code,current_projection_digest "
                "FROM cognitive_runs ORDER BY run_id",
            )
        projections = _rows_as_dicts(
            connection,
            "SELECT entity_id,project_id,entity_type,state,freshness,"
            "current_event_id,projection_digest,rebuilt_through_sequence,"
            "updated_at FROM state_projections ORDER BY entity_id",
        )
        delegations = _rows_as_dicts(
            connection,
            "SELECT delegation_id,project_id,contract_digest,authority_digest,"
            "valid_from,expires_at,created_event_id FROM delegation_contracts "
            "ORDER BY delegation_id",
        )
        outcomes = _rows_as_dicts(
            connection,
            "SELECT outcome_id,delegation_id,runtime_run_id,receipt_digest,"
            "verification_digest,status,outcome_digest,current_event_id "
            "FROM outcome_records ORDER BY outcome_id",
        )
    basis = {
        "phase": selected_phase,
        "availability": availability,
        "core_schema_marker": operational_row[0],
        "core_schema_version": operational_row[1],
        "core_schema_digest": operational_row[2],
        "schema_marker": schema_marker,
        "schema_generation": schema_generation,
        "schema_digest": cognitive_schema_digest,
        "objectives": objectives,
        "plans": plans,
        "runs": runs,
        "operational_projections": projections,
        "delegations": delegations,
        "outcomes": outcomes,
    }
    return {
        **basis,
        "counts": {
            "objectives": None if objectives is None else len(objectives),
            "plans": None if plans is None else len(plans),
            "runs": None if runs is None else len(runs),
            "operational_projections": len(projections),
            "delegations": len(delegations),
            "outcomes": len(outcomes),
        },
        "summary_digest": canonical_digest(basis),
        "private_payloads_included": False,
    }


def _contains_secret_literal(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                _SENSITIVE_FIELD.search(str(key)) is not None
                and str(key) not in _SAFE_SECRET_METADATA_FIELDS
            )
            or _contains_secret_literal(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_literal(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_LITERAL_PATTERNS)
    return False


def _provider_template(database: Path, *, phase: str) -> dict[str, Any]:
    selected_phase = _validate_phase(phase)
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    profiles: list[dict[str, Any]] = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if not _table_exists(connection, "provider_registry"):
            if selected_phase == "final":
                raise RecoveryError("v10_provider_registry_missing")
            rows = []
        elif selected_phase == "preproduction":
            raise RecoveryError("recovery_phase_schema_mismatch")
        else:
            rows = connection.execute(
                "SELECT * FROM provider_registry "
                "ORDER BY provider_id,profile_version"
            ).fetchall()
        for row in rows:
            raw = row["canonical_profile"]
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RecoveryError("provider_profile_corrupt") from exc
            if not isinstance(payload, dict) or canonical_json(payload) != raw:
                raise RecoveryError("provider_profile_corrupt")
            try:
                profile = ProviderProfile.from_record(payload)
            except CognitiveProviderError as exc:
                raise RecoveryError("provider_profile_corrupt") from exc
            expected_reference = (
                None
                if profile.secret_reference is None
                else profile.secret_reference.reference_id
            )
            if (
                row["provider_id"] != profile.provider_id
                or row["profile_version"] != profile.profile_version
                or row["display_name"] != profile.display_name
                or row["endpoint"] != profile.endpoint
                or row["model"] != profile.model
                or row["enabled"] != int(profile.enabled)
                or row["manual_selection_only"] != 1
                or row["secret_reference"] != expected_reference
                or row["health_state"] != profile.health_state
                or row["profile_digest"] != profile.profile_digest
            ):
                raise RecoveryError("provider_profile_tampered")
            safe_payload = profile.to_dict()
            safe = {
                key: safe_payload[key]
                for key in _PROVIDER_TEMPLATE_FIELDS
                if key in safe_payload
            }
            safe["requires_local_secret_configuration"] = (
                expected_reference is not None
            )
            if _contains_secret_literal(safe):
                raise RecoveryError("provider_template_secret_detected")
            profiles.append(safe)
    template: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "phase": selected_phase,
        "availability": (
            "not_migrated" if selected_phase == "preproduction" else "available"
        ),
        "selection_policy": "manual_only",
        "hidden_fallback": False,
        "secret_material_included": False,
        "secret_references_included": False,
        "profiles": profiles,
    }
    template["template_digest"] = canonical_digest(template)
    return template


def _operational_rebuild_rehearsal(database: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ayunos-v10-projection-rebuild-") as name:
        copied = Path(name) / BACKUP_FILENAMES["operational"]
        shutil.copy2(database, copied)
        store = OperationalStateStore(copied)
        before = store.list_projections()
        with closing(sqlite3.connect(copied)) as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM state_snapshots "
                "ORDER BY through_sequence DESC,created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise RecoveryError("operational_snapshot_missing")
        snapshot_id = str(row[0])
        verified = store.verify_snapshot(snapshot_id)
        rebuilt = store.rebuild_projections(snapshot_id=snapshot_id)
        after = store.list_projections()
        if before != after:
            raise RecoveryError("operational_projection_rebuild_mismatch")
        return {
            "snapshot_id": snapshot_id,
            "snapshot_verified": verified["status"] == "verified",
            "projection_rebuild_status": rebuilt["status"],
            "projection_count": len(after),
            "projection_digests_match": True,
        }


def _operational_quiescence(database: Path, *, phase: str) -> dict[str, Any]:
    selected_phase = _validate_phase(phase)
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        operational = connection.execute(
            "SELECT writer_default_enabled FROM operational_schema "
            "WHERE singleton=1"
        ).fetchone()
        cognitive_exists = _table_exists(connection, "cognitive_schema")
        if cognitive_exists:
            cognitive = connection.execute(
                "SELECT writer_default_enabled FROM cognitive_schema "
                "WHERE singleton=1"
            ).fetchone()
        else:
            cognitive = None
        lease_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM state_writer_leases"
            ).fetchone()[0]
        )
    result = {
        "operational_writer_default_enabled": (
            None if operational is None else bool(operational[0])
        ),
        "cognitive_writer_default_enabled": (
            None if cognitive is None else bool(cognitive[0])
        ),
        "active_writer_lease_count": lease_count,
    }
    expected = {
        "operational_writer_default_enabled": False,
        "cognitive_writer_default_enabled": (
            None if selected_phase == "preproduction" else False
        ),
        "active_writer_lease_count": 0,
    }
    if cognitive_exists != (selected_phase == "final"):
        raise RecoveryError("recovery_phase_schema_mismatch")
    if result != expected:
        raise RecoveryError("operational_source_not_quiescent")
    return result


def _restore_rehearsal(
    database_directory: Path,
    audits: Mapping[str, Mapping[str, Any]],
    cognitive: Mapping[str, Any],
    *,
    port: int,
    phase: str,
) -> dict[str, Any]:
    _validate_restore_port(port)
    with tempfile.TemporaryDirectory(prefix="ayunos-v10-restore-rehearsal-") as name:
        root = Path(name)
        checks: dict[str, Any] = {}
        for label, filename in BACKUP_FILENAMES.items():
            restored = root / filename
            shutil.copy2(database_directory / filename, restored)
            audit = audit_database(label + "-rehearsal", restored)
            _validate_audit_match(audits[label], audit)
            checks[label] = {
                "schema_digest_match": True,
                "logical_digest_match": True,
                "integrity_check": audit["integrity_check"],
                "foreign_key_violation_count": audit[
                    "foreign_key_violation_count"
                ],
            }
        restored_operational = root / BACKUP_FILENAMES["operational"]
        restored_cognitive = _cognitive_summary(
            restored_operational, phase=phase
        )
        if restored_cognitive != cognitive:
            raise RecoveryError("restored_cognitive_summary_mismatch")
        rebuild = _operational_rebuild_rehearsal(restored_operational)
    return {
        "passed": True,
        "isolated_temporary_path": True,
        "production_database_connected": False,
        "requested_loopback_origin": f"http://127.0.0.1:{port}",
        "database_results": checks,
        "cognitive_summary_match": True,
        "operational_projection_rebuild": rebuild,
    }


def _validate_restore_port(port: int) -> None:
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1024 <= port <= 65535
        or port in PRODUCTION_WEB_PORTS
    ):
        raise RecoveryError("independent_restore_port_required")


def _inventory(root: Path, *, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    excluded = set(exclude)
    rows: list[dict[str, Any]] = []
    for path in _iter_tree_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in excluded or relative == _OWNER_MARKER:
            continue
        rows.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _hash_file(path),
            }
        )
    rows.sort(key=lambda item: item["path"])
    return rows


def _backup_set_basis(
    inventory: list[dict[str, Any]],
    database_results: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Any],
    cognitive: Mapping[str, Any],
    provider_template: Mapping[str, Any],
    release_reports: Mapping[str, Any],
    source_provenance: Mapping[str, Any],
    service_quiescence_digest: str,
    phase: str,
    ownership_marker_identity_digest: str,
) -> dict[str, Any]:
    return {
        "phase": _validate_phase(phase),
        "artifact_inventory": inventory,
        "database_logical_digests": {
            label: result["backup_audit"]["logical_digest"]
            for label, result in sorted(database_results.items())
        },
        "asset_tree_sha256": assets["backup_audit"]["tree_sha256"],
        "cognitive_summary_digest": cognitive["summary_digest"],
        "provider_template_digest": provider_template["template_digest"],
        "release_reports_tree_sha256": release_reports["backup_audit"][
            "tree_sha256"
        ],
        "source_provenance_digest": source_provenance["provenance_digest"],
        "service_quiescence_digest": service_quiescence_digest,
        "ownership_marker_identity_digest": _validate_sha256(
            ownership_marker_identity_digest,
            "ownership_marker_identity_invalid",
        ),
    }


def _database_provenance_entry(
    relative: Path,
    file_gates: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_relative_path": relative.as_posix(),
        "stable_file_gates": dict(file_gates),
        "schema_digest": audit["schema_digest"],
        "logical_digest": audit["logical_digest"],
        "user_version": audit["user_version"],
    }


def _validate_source_provenance_shape(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "contract_version",
        "source_binding",
        "root_identity_sha256",
        "databases",
        "assets",
        "release_reports",
        "provenance_digest",
    }:
        raise RecoveryError("source_provenance_invalid")
    if (
        value.get("contract_version") != CONTRACT_VERSION
        or value.get("source_binding") not in _SOURCE_BINDINGS
    ):
        raise RecoveryError("source_provenance_invalid")
    _validate_sha256(
        value.get("root_identity_sha256"), "source_provenance_invalid"
    )
    _validate_sha256(
        value.get("provenance_digest"), "source_provenance_invalid"
    )
    databases = value.get("databases")
    if not isinstance(databases, Mapping) or set(databases) != set(DEFAULT_DATABASES):
        raise RecoveryError("source_provenance_invalid")
    for label, relative in DEFAULT_DATABASES.items():
        entry = databases[label]
        if not isinstance(entry, Mapping) or set(entry) != {
            "source_relative_path",
            "stable_file_gates",
            "schema_digest",
            "logical_digest",
            "user_version",
        }:
            raise RecoveryError("source_provenance_invalid")
        if entry.get("source_relative_path") != relative.as_posix():
            raise RecoveryError("source_provenance_invalid")
        gates = entry.get("stable_file_gates")
        if not isinstance(gates, Mapping) or set(gates) != {"db", "wal", "shm"}:
            raise RecoveryError("source_provenance_invalid")
        _validate_sha256(entry.get("schema_digest"), "source_provenance_invalid")
        _validate_sha256(entry.get("logical_digest"), "source_provenance_invalid")
        if not isinstance(entry.get("user_version"), int):
            raise RecoveryError("source_provenance_invalid")
    assets = value.get("assets")
    if (
        not isinstance(assets, Mapping)
        or set(assets) != {"source_relative_path", "audit"}
        or assets.get("source_relative_path") != DEFAULT_ASSET_ROOT.as_posix()
        or not isinstance(assets.get("audit"), Mapping)
    ):
        raise RecoveryError("source_provenance_invalid")
    reports = value.get("release_reports")
    if (
        not isinstance(reports, Mapping)
        or set(reports) != {"source_relative_path", "audit", "files"}
        or reports.get("source_relative_path")
        != DEFAULT_RELEASE_REPORT_ROOT.as_posix()
        or not isinstance(reports.get("audit"), Mapping)
        or not isinstance(reports.get("files"), list)
    ):
        raise RecoveryError("source_provenance_invalid")
    if value.get("provenance_digest") != canonical_digest(
        _without_digest(value, "provenance_digest")
    ):
        raise RecoveryError("source_provenance_digest_mismatch")


def _inspect_project_source_provenance(
    project_root: Path, *, source_binding: str
) -> dict[str, Any]:
    if source_binding not in _SOURCE_BINDINGS:
        raise RecoveryError("source_provenance_invalid")
    root = _safe_existing_root(project_root, directory=True)
    sources = {
        label: _safe_descendant(root, relative, directory=False)
        for label, relative in DEFAULT_DATABASES.items()
    }
    gates_before = {
        label: _sqlite_file_gates(source)
        for label, source in sources.items()
    }
    audits = {
        label: audit_database(label + "-source-provenance", source)
        for label, source in sources.items()
    }
    gates_after = {
        label: _sqlite_file_gates(source)
        for label, source in sources.items()
    }
    if gates_before != gates_after:
        raise RecoveryError("source_database_changed_during_final_audit")
    databases: dict[str, Any] = {}
    for label, relative in DEFAULT_DATABASES.items():
        databases[label] = _database_provenance_entry(
            relative, gates_after[label], audits[label]
        )
    assets_source = _safe_descendant(root, DEFAULT_ASSET_ROOT, directory=True)
    asset_audit = audit_tree(assets_source)
    if audit_tree(assets_source) != asset_audit:
        raise RecoveryError("asset_source_changed_during_final_audit")
    report_root, report_sources = _release_report_sources(root)
    report_audit, report_files = _audit_release_report_sources(
        report_root, report_sources
    )
    final_report_root, final_report_sources = _release_report_sources(root)
    final_report_audit, final_report_files = _audit_release_report_sources(
        final_report_root, final_report_sources
    )
    if (
        final_report_root != report_root
        or final_report_audit != report_audit
        or final_report_files != report_files
    ):
        raise RecoveryError("release_report_source_changed_during_final_audit")
    seal_gates = {
        label: _sqlite_file_gates(source)
        for label, source in sources.items()
    }
    if seal_gates != gates_after:
        raise RecoveryError("source_database_changed_during_final_audit")
    if audit_tree(assets_source) != asset_audit:
        raise RecoveryError("asset_source_changed_during_final_audit")
    seal_report_root, seal_report_sources = _release_report_sources(root)
    seal_report_audit, seal_report_files = _audit_release_report_sources(
        seal_report_root, seal_report_sources
    )
    if (
        seal_report_root != report_root
        or seal_report_audit != report_audit
        or seal_report_files != report_files
    ):
        raise RecoveryError("release_report_source_changed_during_final_audit")
    normalized_root = os.path.normcase(os.fspath(root)).casefold()
    provenance: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "source_binding": source_binding,
        "root_identity_sha256": hashlib.sha256(
            normalized_root.encode("utf-8")
        ).hexdigest(),
        "databases": databases,
        "assets": {
            "source_relative_path": DEFAULT_ASSET_ROOT.as_posix(),
            "audit": asset_audit,
        },
        "release_reports": {
            "source_relative_path": DEFAULT_RELEASE_REPORT_ROOT.as_posix(),
            "audit": report_audit,
            "files": report_files,
        },
    }
    provenance["provenance_digest"] = canonical_digest(provenance)
    _validate_source_provenance_shape(provenance)
    return provenance


def _validate_source_provenance_binding(
    source_provenance: Mapping[str, Any],
    database_results: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Any],
    release_reports: Mapping[str, Any],
) -> None:
    _validate_source_provenance_shape(source_provenance)
    databases = source_provenance["databases"]
    for label, relative in DEFAULT_DATABASES.items():
        result = database_results[label]
        expected = _database_provenance_entry(
            relative,
            result["stable_source_file_gates"],
            result["source_audit"],
        )
        if databases[label] != expected:
            raise RecoveryError("database_source_provenance_mismatch")
    if source_provenance["assets"] != {
        "source_relative_path": DEFAULT_ASSET_ROOT.as_posix(),
        "audit": assets["source_audit"],
    }:
        raise RecoveryError("asset_source_provenance_mismatch")
    if source_provenance["release_reports"] != {
        "source_relative_path": DEFAULT_RELEASE_REPORT_ROOT.as_posix(),
        "audit": release_reports["source_audit"],
        "files": release_reports["files"],
    }:
        raise RecoveryError("release_report_source_provenance_mismatch")


def _fixed_provenance_api_factory(module_file: str):
    """Seal fixed-root provenance APIs in a closure, not a mutable global."""

    production_root = Path(module_file).resolve().parents[1]

    def inspect_fixed_production_provenance() -> dict[str, Any]:
        """Read-only audit of fixed production DBs, assets, and reports."""

        return _inspect_project_source_provenance(
            production_root, source_binding="fixed_project_root"
        )

    def validate_fixed_production_provenance(
        recorded: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Re-audit sealed production sources against package provenance."""

        _validate_source_provenance_shape(recorded)
        if recorded.get("source_binding") != "fixed_project_root":
            raise RecoveryError("fixed_production_source_binding_required")
        current = _inspect_project_source_provenance(
            production_root, source_binding="fixed_project_root"
        )
        if current != recorded:
            raise RecoveryError("fixed_production_provenance_mismatch")
        report: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status": "matched",
            "source_binding": "fixed_project_root",
            "provenance_digest": current["provenance_digest"],
        }
        report["report_digest"] = canonical_digest(report)
        return report

    return (
        inspect_fixed_production_provenance,
        validate_fixed_production_provenance,
    )


(
    inspect_fixed_production_provenance,
    validate_fixed_production_provenance,
) = _fixed_provenance_api_factory(__file__)


def _build_package(
    *,
    phase: str,
    output: Path,
    project_root: Path,
    source_binding: str,
    rehearsal_port: int = 18765,
    service_quiescence_digest: str,
) -> dict[str, Any]:
    """Create one final five-database v10 package at a new destination."""
    selected_phase = _validate_phase(phase)
    _validate_restore_port(rehearsal_port)
    quiescence_digest = _validate_sha256(
        service_quiescence_digest,
        "service_quiescence_digest_required",
    )
    root = _safe_existing_root(project_root, directory=True)
    if source_binding == "fixed_project_root" and root != _safe_existing_root(
        Path(__file__).resolve().parents[1], directory=True
    ):
        raise RecoveryError("fixed_production_project_root_required")
    if source_binding not in _SOURCE_BINDINGS:
        raise RecoveryError("source_provenance_invalid")
    sources = {
        label: _safe_descendant(root, relative, directory=False)
        for label, relative in DEFAULT_DATABASES.items()
    }
    assets_source = _safe_descendant(root, DEFAULT_ASSET_ROOT, directory=True)
    report_root, report_sources = _release_report_sources(root)
    destination, parent, parent_identity = _safe_new_destination(output)
    data_root = _safe_descendant(root, Path("data"), directory=True)
    if (
        _is_within(destination, data_root)
        or _is_within(destination, assets_source)
        or _is_within(destination, report_root)
    ):
        raise RecoveryError("package_destination_overlaps_source")
    staging, nonce, staging_identity = _create_owned_staging(parent, "package")
    succeeded = False
    try:
        quiescence_before = _operational_quiescence(
            sources["operational"], phase=selected_phase
        )
        database_directory = staging / "databases"
        sidecar_directory = staging / "sqlite_sidecars"
        database_directory.mkdir()
        sidecar_directory.mkdir()
        started = time.perf_counter()
        database_results: dict[str, Any] = {}
        for label, source in sources.items():
            initial_gates = _sqlite_file_gates(source)
            source_before = audit_database(label + "-source-before", source)
            stable_gates_before = _sqlite_file_gates(source)
            backup = database_directory / BACKUP_FILENAMES[label]
            _online_backup(source, backup)
            source_after = audit_database(label + "-source-after", source)
            stable_gates_after = _sqlite_file_gates(source)
            if stable_gates_before != stable_gates_after:
                raise RecoveryError("source_database_changed_during_backup")
            _validate_audit_match(source_before, source_after)
            backup_audit = audit_database(label + "-backup", backup)
            _validate_audit_match(source_after, backup_audit)
            sidecars = _copy_sidecars(
                source, sidecar_directory / label
            )
            if _sqlite_file_gates(source) != stable_gates_after:
                raise RecoveryError("source_database_changed_during_sidecar_copy")
            database_results[label] = {
                "source_relative_path": DEFAULT_DATABASES[label].as_posix(),
                "backup_relative_path": (
                    Path("databases") / BACKUP_FILENAMES[label]
                ).as_posix(),
                "initial_file_gates": initial_gates,
                "stable_source_file_gates": stable_gates_after,
                "source_audit": source_after,
                "backup_audit": backup_audit,
                "sqlite_sidecars": sidecars,
            }

        source_asset_audit = audit_tree(assets_source)
        packaged_assets = staging / "assets"
        shutil.copytree(assets_source, packaged_assets, symlinks=True)
        packaged_asset_audit = audit_tree(packaged_assets)
        if (
            source_asset_audit != audit_tree(assets_source)
            or source_asset_audit != packaged_asset_audit
        ):
            raise RecoveryError("asset_backup_mismatch")
        assets = {
            "included": True,
            "source_relative_path": DEFAULT_ASSET_ROOT.as_posix(),
            "backup_relative_path": "assets",
            "source_audit": source_asset_audit,
            "backup_audit": packaged_asset_audit,
        }
        release_reports = _copy_release_reports(
            report_root, report_sources, staging / "release_reports"
        )
        quiescence_after = _operational_quiescence(
            sources["operational"], phase=selected_phase
        )
        if quiescence_after != quiescence_before:
            raise RecoveryError("operational_quiescence_changed_during_backup")

        source_provenance = _inspect_project_source_provenance(
            root, source_binding=source_binding
        )
        _validate_source_provenance_binding(
            source_provenance,
            database_results,
            assets,
            release_reports,
        )

        operational_backup = database_directory / BACKUP_FILENAMES["operational"]
        cognitive = _cognitive_summary(
            operational_backup, phase=selected_phase
        )
        provider_template = _provider_template(
            operational_backup, phase=selected_phase
        )
        _write_json(staging / "provider_configuration_template.json", provider_template)

        schema_manifest: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "database_count": 5,
            "databases": {
                label: {
                    "schema_digest": result["backup_audit"]["schema_digest"],
                    "user_version": result["backup_audit"]["user_version"],
                    "tables": sorted(result["backup_audit"]["tables"]),
                }
                for label, result in database_results.items()
            },
        }
        schema_manifest["schema_manifest_digest"] = canonical_digest(
            schema_manifest
        )
        _write_json(staging / "schema_manifest.json", schema_manifest)

        logical_summaries: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "databases": {
                label: {
                    "logical_digest": result["backup_audit"]["logical_digest"],
                    "table_summaries": result["backup_audit"]["tables"],
                }
                for label, result in database_results.items()
            },
            "assets": packaged_asset_audit,
            "release_reports": release_reports["backup_audit"],
            "cognitive": cognitive,
        }
        logical_summaries["logical_summaries_digest"] = canonical_digest(
            logical_summaries
        )
        _write_json(staging / "logical_summaries.json", logical_summaries)

        recovery_guide = (
            "# YunOS v10 恢复指南\n\n"
            "本包必须先恢复到新的隔离目录，并使用未被生产服务占用的 loopback "
            "端口进行只读验收。`databases/` 中的五个 SQLite online backup 是恢复"
            "来源；`sqlite_sidecars/` 仅为 DB/WAL/SHM 取证证据，不得覆盖恢复库。\n\n"
            "恢复前运行 `python tools/v10_release_recovery.py verify --package "
            "<package> --expected-phase <preproduction|final>`。恢复时运行 "
            "`python tools/v10_release_recovery.py restore --package <package> "
            "--destination <new-directory> --port <isolated-port> "
            "--expected-phase <preproduction|final>`。"
            "工具拒绝现有目标、绝对清单路径、路径穿越以及 symlink/junction/reparse "
            "point。恢复完成后仅依据 `restore_runtime.json` 中列出的隔离数据库"
            "启动 real-readonly 服务；不得连接生产数据库。Provider 模板不含密钥"
            "或 secret reference，凭据必须在恢复环境中另行人工配置。"
            "`release_reports/` 只保存 v10.0—v10-F 节点、迁移和真实验收报告；"
            "最终封板 sidecar 位于 `docs/releases/`。\n"
        )
        (staging / "RECOVERY.md").write_text(recovery_guide, encoding="utf-8")

        rehearsal = _restore_rehearsal(
            database_directory,
            {label: result["backup_audit"] for label, result in database_results.items()},
            cognitive,
            port=rehearsal_port,
            phase=selected_phase,
        )
        inventory = _inventory(staging)
        ownership_identity_digest = _ownership_identity_digest(
            purpose="package", nonce=nonce
        )
        basis = _backup_set_basis(
            inventory,
            database_results,
            assets,
            cognitive,
            provider_template,
            release_reports,
            source_provenance,
            quiescence_digest,
            selected_phase,
            ownership_identity_digest,
        )
        manifest: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "release_name": RELEASE_NAME,
            "phase": selected_phase,
            "created_at": _utc_now(),
            "method": "sqlite_online_backup_plus_verified_asset_copy",
            "database_count": 5,
            "production_databases_opened_read_only": True,
            "production_write_performed": False,
            "database_results": database_results,
            "assets": assets,
            "release_reports": release_reports,
            "source_provenance": source_provenance,
            "service_quiescence": {
                "evidence_digest": quiescence_digest,
                "state": quiescence_after,
                "services_stopped_evidence_supplied_by_caller": True,
            },
            "schema_manifest_relative_path": "schema_manifest.json",
            "logical_summaries_relative_path": "logical_summaries.json",
            "provider_template_relative_path": "provider_configuration_template.json",
            "recovery_guide_relative_path": "RECOVERY.md",
            "artifact_inventory": inventory,
            "restore_rehearsal": rehearsal,
            "ownership_marker_identity_digest": ownership_identity_digest,
            "backup_set_digest": canonical_digest(basis),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
        manifest["manifest_digest"] = canonical_digest(manifest)
        _write_json(staging / "release_manifest.json", manifest)
        _write_sealed_owner_marker(
            staging,
            purpose="package",
            nonce=nonce,
            phase=selected_phase,
            manifest_digest=manifest["manifest_digest"],
            backup_set_digest=manifest["backup_set_digest"],
            runtime_digest=None,
        )
        verify_package(staging, expected_phase=selected_phase)
        if (
            _inspect_project_source_provenance(
                root, source_binding=source_binding
            )
            != source_provenance
        ):
            raise RecoveryError("source_provenance_changed_before_package_seal")
        _assert_directory_identity(parent, parent_identity)
        if destination.exists() or destination.is_symlink():
            raise RecoveryError("destination_exists")
        staging.rename(destination)
        _assert_owned_target_identity(destination, staging_identity)
        _assert_directory_identity(parent, parent_identity)
        if _safe_existing_root(destination, directory=True).parent != parent:
            raise RecoveryError("destination_parent_identity_changed")
        _validate_sealed_owner_marker(
            destination,
            purpose="package",
            phase=selected_phase,
            manifest_digest=manifest["manifest_digest"],
            backup_set_digest=manifest["backup_set_digest"],
            ownership_identity_digest=ownership_identity_digest,
            runtime_digest=None,
        )
        succeeded = True
        return manifest
    finally:
        # Only a failed, still-unpublished staging directory is removable.
        # After rename, the final path is immutable evidence even if a later
        # identity or sealed-marker validation fails.
        if not succeeded and staging.exists():
            _cleanup_owned_staging(staging, nonce, staging_identity)


def _package_builder_api_factory(module_file: str):
    """Bind production and test builders to an immutable closure root."""

    production_root = Path(module_file).resolve().parents[1]

    def build_package(
        *,
        phase: str,
        output: Path,
        rehearsal_port: int = 18765,
        service_quiescence_digest: str,
    ) -> dict[str, Any]:
        """Build only from the factory-sealed production project root."""

        return _build_package(
            phase=phase,
            output=output,
            project_root=production_root,
            source_binding="fixed_project_root",
            rehearsal_port=rehearsal_port,
            service_quiescence_digest=service_quiescence_digest,
        )

    def _build_package_for_test(
        *,
        phase: str,
        output: Path,
        _test_project_root: Path,
        rehearsal_port: int = 18765,
        service_quiescence_digest: str,
    ) -> dict[str, Any]:
        """Private fixture builder which can never target production."""

        try:
            selected_root = _test_project_root.resolve(strict=True)
        except OSError:
            raise RecoveryError("test_project_root_invalid") from None
        if selected_root == production_root:
            raise RecoveryError("test_project_root_is_production")
        return _build_package(
            phase=phase,
            output=output,
            project_root=selected_root,
            source_binding="test_fixture",
            rehearsal_port=rehearsal_port,
            service_quiescence_digest=service_quiescence_digest,
        )

    return build_package, _build_package_for_test


build_package, _build_package_for_test = _package_builder_api_factory(__file__)


def _safe_manifest_path(package: Path, raw: Any, *, directory: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise RecoveryError("manifest_relative_path_invalid")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RecoveryError("manifest_relative_path_invalid")
    if any(":" in part for part in pure.parts):
        raise RecoveryError("manifest_relative_path_invalid")
    current = package
    for part in pure.parts:
        current = current / part
        if not current.exists():
            raise RecoveryError("manifest_artifact_missing")
        _reject_reparse(current)
    resolved = current.resolve(strict=True)
    if not _is_within(resolved, package):
        raise RecoveryError("manifest_path_escape_detected")
    if directory and not resolved.is_dir():
        raise RecoveryError("manifest_artifact_type_mismatch")
    if not directory and not resolved.is_file():
        raise RecoveryError("manifest_artifact_type_mismatch")
    if not directory:
        _reject_hardlink_file(resolved)
    return resolved


def _without_digest(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    return result


def _validate_auxiliary_digest(
    value: Mapping[str, Any], field: str, reason: str
) -> None:
    if value.get(field) != canonical_digest(_without_digest(value, field)):
        raise RecoveryError(reason)


def _verify_inventory(package: Path, expected: Any) -> list[dict[str, Any]]:
    if not isinstance(expected, list):
        raise RecoveryError("artifact_inventory_invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in expected:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise RecoveryError("artifact_inventory_invalid")
        path = _safe_manifest_path(package, item["path"])
        if item["path"] in seen:
            raise RecoveryError("artifact_inventory_duplicate")
        seen.add(item["path"])
        actual = {
            "path": item["path"],
            "size": path.stat().st_size,
            "sha256": _hash_file(path),
        }
        if actual != item:
            raise RecoveryError("artifact_digest_mismatch")
        normalized.append(actual)
    normalized.sort(key=lambda item: item["path"])
    actual_inventory = _inventory(package, exclude=("release_manifest.json",))
    if normalized != actual_inventory:
        raise RecoveryError("artifact_inventory_set_mismatch")
    return normalized


def verify_package(
    package: Path,
    *,
    expected_phase: str,
) -> dict[str, Any]:
    """Verify package paths, hashes, SQLite logic, v10 state, and rebuild."""
    selected_phase = _validate_phase(expected_phase)
    root = _safe_existing_root(package, directory=True)
    manifest = _read_json(_safe_descendant(root, Path("release_manifest.json"), directory=False))
    expected_manifest_fields = {
        "contract_version",
        "release_name",
        "phase",
        "created_at",
        "method",
        "database_count",
        "production_databases_opened_read_only",
        "production_write_performed",
        "database_results",
        "assets",
        "release_reports",
        "source_provenance",
        "service_quiescence",
        "schema_manifest_relative_path",
        "logical_summaries_relative_path",
        "provider_template_relative_path",
        "recovery_guide_relative_path",
        "artifact_inventory",
        "restore_rehearsal",
        "ownership_marker_identity_digest",
        "backup_set_digest",
        "elapsed_seconds",
        "manifest_digest",
    }
    if set(manifest) != expected_manifest_fields:
        raise RecoveryError("recovery_manifest_shape_invalid")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise RecoveryError("recovery_contract_version_mismatch")
    if manifest.get("database_count") != 5:
        raise RecoveryError("recovery_database_count_mismatch")
    if manifest.get("phase") != selected_phase:
        raise RecoveryError("recovery_phase_mismatch")
    if manifest.get("production_write_performed") is not False:
        raise RecoveryError("recovery_write_boundary_invalid")
    service_quiescence = manifest.get("service_quiescence")
    if (
        not isinstance(service_quiescence, dict)
        or set(service_quiescence)
        != {
            "evidence_digest",
            "state",
            "services_stopped_evidence_supplied_by_caller",
        }
        or service_quiescence.get(
            "services_stopped_evidence_supplied_by_caller"
        )
        is not True
        or service_quiescence.get("state")
        != {
            "operational_writer_default_enabled": False,
            "cognitive_writer_default_enabled": (
                None if selected_phase == "preproduction" else False
            ),
            "active_writer_lease_count": 0,
        }
    ):
        raise RecoveryError("service_quiescence_manifest_invalid")
    _validate_sha256(
        service_quiescence.get("evidence_digest"),
        "service_quiescence_manifest_invalid",
    )
    if (
        manifest.get("release_name") != RELEASE_NAME
        or manifest.get("method")
        != "sqlite_online_backup_plus_verified_asset_copy"
        or manifest.get("production_databases_opened_read_only") is not True
        or manifest.get("schema_manifest_relative_path") != "schema_manifest.json"
        or manifest.get("logical_summaries_relative_path")
        != "logical_summaries.json"
        or manifest.get("provider_template_relative_path")
        != "provider_configuration_template.json"
        or manifest.get("recovery_guide_relative_path") != "RECOVERY.md"
    ):
        raise RecoveryError("recovery_manifest_boundary_invalid")
    if manifest.get("manifest_digest") != canonical_digest(
        _without_digest(manifest, "manifest_digest")
    ):
        raise RecoveryError("recovery_manifest_digest_mismatch")
    ownership_identity_digest = _validate_sha256(
        manifest.get("ownership_marker_identity_digest"),
        "ownership_marker_identity_invalid",
    )
    _validate_sealed_owner_marker(
        root,
        purpose="package",
        phase=selected_phase,
        manifest_digest=manifest["manifest_digest"],
        backup_set_digest=manifest["backup_set_digest"],
        ownership_identity_digest=ownership_identity_digest,
        runtime_digest=None,
    )
    inventory = _verify_inventory(root, manifest.get("artifact_inventory"))

    database_results = manifest.get("database_results")
    if not isinstance(database_results, dict) or set(database_results) != set(DEFAULT_DATABASES):
        raise RecoveryError("recovery_database_manifest_invalid")
    database_checks: dict[str, Any] = {}
    package_databases: dict[str, Path] = {}
    actual_audits: dict[str, dict[str, Any]] = {}
    allowed_artifacts = {
        "RECOVERY.md",
        "schema_manifest.json",
        "logical_summaries.json",
        "provider_configuration_template.json",
    }
    for label, filename in BACKUP_FILENAMES.items():
        result = database_results[label]
        if not isinstance(result, dict) or set(result) != {
            "source_relative_path",
            "backup_relative_path",
            "initial_file_gates",
            "stable_source_file_gates",
            "source_audit",
            "backup_audit",
            "sqlite_sidecars",
        }:
            raise RecoveryError("recovery_database_manifest_invalid")
        expected_relative = (Path("databases") / filename).as_posix()
        if (
            result.get("source_relative_path")
            != DEFAULT_DATABASES[label].as_posix()
            or result.get("backup_relative_path") != expected_relative
        ):
            raise RecoveryError("recovery_database_path_mismatch")
        backup_audit = result.get("backup_audit")
        source_audit = result.get("source_audit")
        if not isinstance(backup_audit, dict) or not isinstance(source_audit, dict):
            raise RecoveryError("recovery_database_manifest_invalid")
        path = _safe_manifest_path(root, expected_relative)
        audit = audit_database(label + "-package-verify", path)
        _validate_audit_match(backup_audit, audit)
        _validate_audit_match(source_audit, backup_audit)
        package_databases[label] = path
        actual_audits[label] = audit
        allowed_artifacts.add(expected_relative)
        database_checks[label] = {
            "integrity_check": audit["integrity_check"],
            "foreign_key_violation_count": audit["foreign_key_violation_count"],
            "schema_digest_match": True,
            "logical_digest_match": True,
            "file_sha256_match": (
                audit["file_gate"]["sha256"]
                == backup_audit.get("file_gate", {}).get("sha256")
            ),
        }
        if not database_checks[label]["file_sha256_match"]:
            raise RecoveryError("database_backup_hash_mismatch")
        sidecars = result.get("sqlite_sidecars")
        if not isinstance(sidecars, dict) or set(sidecars) != {"wal", "shm"}:
            raise RecoveryError("sqlite_sidecar_manifest_invalid")
        for key, suffix in (("wal", "-wal"), ("shm", "-shm")):
            evidence = sidecars[key]
            if not isinstance(evidence, dict) or set(evidence) != {
                "source",
                "packaged",
                "packaged_filename",
            }:
                raise RecoveryError("sqlite_sidecar_manifest_invalid")
            packaged_gate = evidence["packaged"]
            if not isinstance(packaged_gate, dict) or not isinstance(
                evidence["source"], dict
            ):
                raise RecoveryError("sqlite_sidecar_manifest_invalid")
            expected_filename = filename + suffix
            if packaged_gate.get("exists"):
                if evidence.get("packaged_filename") != expected_filename:
                    raise RecoveryError("sqlite_sidecar_path_mismatch")
                relative = (
                    Path("sqlite_sidecars") / label / expected_filename
                ).as_posix()
                sidecar_path = _safe_manifest_path(root, relative)
                if _file_gate(sidecar_path) != packaged_gate:
                    raise RecoveryError("sqlite_sidecar_digest_mismatch")
                allowed_artifacts.add(relative)
            elif (
                evidence.get("packaged_filename") is not None
                or packaged_gate
                != {"exists": False, "size": 0, "sha256": None}
            ):
                raise RecoveryError("sqlite_sidecar_manifest_invalid")

    assets = manifest.get("assets")
    if not isinstance(assets, dict) or assets.get("backup_relative_path") != "assets":
        raise RecoveryError("asset_manifest_invalid")
    asset_path = _safe_manifest_path(root, "assets", directory=True)
    asset_audit = audit_tree(asset_path)
    if asset_audit != assets.get("backup_audit"):
        raise RecoveryError("asset_backup_mismatch")

    release_reports = manifest.get("release_reports")
    if (
        not isinstance(release_reports, dict)
        or set(release_reports)
        != {
            "included",
            "source_relative_path",
            "backup_relative_path",
            "source_audit",
            "backup_audit",
            "files",
        }
        or release_reports.get("included") is not True
        or release_reports.get("source_relative_path")
        != DEFAULT_RELEASE_REPORT_ROOT.as_posix()
        or release_reports.get("backup_relative_path") != "release_reports"
        or not isinstance(release_reports.get("files"), list)
    ):
        raise RecoveryError("release_report_manifest_invalid")
    report_path = _safe_manifest_path(
        root, "release_reports", directory=True
    )
    report_audit = audit_tree(report_path)
    if (
        report_audit != release_reports.get("source_audit")
        or report_audit != release_reports.get("backup_audit")
    ):
        raise RecoveryError("release_report_backup_mismatch")
    actual_report_files = [
        path.relative_to(report_path).as_posix()
        for path in _iter_tree_files(report_path)
    ]
    if actual_report_files != release_reports["files"]:
        raise RecoveryError("release_report_file_set_mismatch")
    for path in _iter_tree_files(report_path):
        try:
            report_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RecoveryError("v10_release_report_invalid") from exc
        if _contains_secret_literal(report_text):
            raise RecoveryError("v10_release_report_secret_detected")

    source_provenance = manifest.get("source_provenance")
    if not isinstance(source_provenance, Mapping):
        raise RecoveryError("source_provenance_invalid")
    _validate_source_provenance_binding(
        source_provenance,
        database_results,
        assets,
        release_reports,
    )

    for artifact in inventory:
        relative = artifact["path"]
        if (
            relative not in allowed_artifacts
            and not relative.startswith("assets/")
            and not relative.startswith("release_reports/")
        ):
            raise RecoveryError("unexpected_package_artifact")

    schema_manifest = _read_json(
        _safe_manifest_path(root, manifest.get("schema_manifest_relative_path"))
    )
    _validate_auxiliary_digest(
        schema_manifest,
        "schema_manifest_digest",
        "schema_manifest_digest_mismatch",
    )
    expected_schema_manifest: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "database_count": 5,
        "databases": {
            label: {
                "schema_digest": audit["schema_digest"],
                "user_version": audit["user_version"],
                "tables": sorted(audit["tables"]),
            }
            for label, audit in actual_audits.items()
        },
    }
    expected_schema_manifest["schema_manifest_digest"] = canonical_digest(
        expected_schema_manifest
    )
    if schema_manifest != expected_schema_manifest:
        raise RecoveryError("schema_manifest_content_mismatch")
    logical = _read_json(
        _safe_manifest_path(root, manifest.get("logical_summaries_relative_path"))
    )
    _validate_auxiliary_digest(
        logical,
        "logical_summaries_digest",
        "logical_summaries_digest_mismatch",
    )
    cognitive = _cognitive_summary(
        package_databases["operational"], phase=selected_phase
    )
    if _operational_quiescence(
        package_databases["operational"], phase=selected_phase
    ) != (
        service_quiescence["state"]
    ):
        raise RecoveryError("restored_operational_quiescence_mismatch")
    expected_logical: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "databases": {
            label: {
                "logical_digest": audit["logical_digest"],
                "table_summaries": audit["tables"],
            }
            for label, audit in actual_audits.items()
        },
        "assets": asset_audit,
        "release_reports": report_audit,
        "cognitive": cognitive,
    }
    expected_logical["logical_summaries_digest"] = canonical_digest(
        expected_logical
    )
    if logical != expected_logical:
        raise RecoveryError("logical_summaries_content_mismatch")

    provider = _read_json(
        _safe_manifest_path(root, manifest.get("provider_template_relative_path"))
    )
    _validate_auxiliary_digest(
        provider, "template_digest", "provider_template_digest_mismatch"
    )
    if (
        provider.get("secret_material_included") is not False
        or provider.get("secret_references_included") is not False
        or _contains_secret_literal(
            {key: value for key, value in provider.items() if key != "template_digest"}
        )
    ):
        raise RecoveryError("provider_template_secret_detected")
    expected_provider = _provider_template(
        package_databases["operational"], phase=selected_phase
    )
    if provider != expected_provider:
        raise RecoveryError("provider_template_mismatch")

    basis = _backup_set_basis(
        inventory,
        database_results,
        assets,
        cognitive,
        provider,
        manifest["release_reports"],
        source_provenance,
        manifest["service_quiescence"]["evidence_digest"],
        selected_phase,
        ownership_identity_digest,
    )
    if manifest.get("backup_set_digest") != canonical_digest(basis):
        raise RecoveryError("backup_set_digest_mismatch")
    rebuild = _operational_rebuild_rehearsal(package_databases["operational"])
    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "verified",
        "phase": selected_phase,
        "manifest_digest": manifest["manifest_digest"],
        "backup_set_digest": manifest["backup_set_digest"],
        "database_checks": database_checks,
        "asset_check": True,
        "release_report_check": True,
        "release_report_count": report_audit["file_count"],
        "release_report_tree_sha256": report_audit["tree_sha256"],
        "source_binding": source_provenance["source_binding"],
        "source_provenance_digest": source_provenance["provenance_digest"],
        "cognitive_summary_digest": cognitive["summary_digest"],
        "provider_secret_material_included": False,
        "service_quiescence_digest": service_quiescence["evidence_digest"],
        "operational_projection_rebuild": rebuild,
    }
    report["report_digest"] = canonical_digest(report)
    return report


def restore_package(
    *,
    package: Path,
    destination: Path,
    port: int,
    expected_phase: str,
) -> dict[str, Any]:
    """Restore a verified package to one new isolated directory."""
    selected_phase = _validate_phase(expected_phase)
    _validate_restore_port(port)
    package_root = _safe_existing_root(package, directory=True)
    manifest_path = _safe_manifest_path(package_root, "release_manifest.json")
    manifest_gate_before = _file_gate(manifest_path)
    verification = verify_package(
        package_root, expected_phase=selected_phase
    )
    if _file_gate(manifest_path) != manifest_gate_before:
        raise RecoveryError("package_manifest_changed_after_verification")
    manifest = _read_json(manifest_path)
    if (
        manifest.get("manifest_digest") != verification.get("manifest_digest")
        or manifest.get("backup_set_digest") != verification.get("backup_set_digest")
    ):
        raise RecoveryError("package_manifest_verification_mismatch")
    target, parent, parent_identity = _safe_new_destination(destination)
    if _is_within(target, package_root):
        raise RecoveryError("restore_destination_overlaps_package")
    production_data = (
        Path(__file__).resolve().parents[1] / "data"
    ).resolve(strict=True)
    if _is_within(target, production_data):
        raise RecoveryError("independent_restore_path_required")
    staging, nonce, staging_identity = _create_owned_staging(parent, "restore")
    ownership_identity_digest = _ownership_identity_digest(
        purpose="restore", nonce=nonce
    )
    succeeded = False
    try:
        database_checks: dict[str, Any] = {}
        restored_paths: dict[str, str] = {}
        for label, source_relative in DEFAULT_DATABASES.items():
            destination_path = staging / source_relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            source = _safe_manifest_path(
                package_root,
                manifest["database_results"][label]["backup_relative_path"],
            )
            expected_gate = manifest["database_results"][label]["backup_audit"].get(
                "file_gate"
            )
            if _file_gate(source) != expected_gate:
                raise RecoveryError("package_database_changed_before_restore")
            shutil.copy2(source, destination_path)
            if _file_gate(source) != expected_gate:
                raise RecoveryError("package_database_changed_during_restore")
            if _file_gate(destination_path) != expected_gate:
                raise RecoveryError("restored_database_file_gate_mismatch")
            audit = audit_database(label + "-isolated-restore", destination_path)
            _validate_audit_match(
                manifest["database_results"][label]["backup_audit"], audit
            )
            restored_paths[label] = source_relative.as_posix()
            database_checks[label] = True

        packaged_assets = _safe_manifest_path(
            package_root, manifest["assets"]["backup_relative_path"], directory=True
        )
        expected_asset_audit = manifest["assets"]["backup_audit"]
        if audit_tree(packaged_assets) != expected_asset_audit:
            raise RecoveryError("package_asset_changed_before_restore")
        restored_assets = staging / DEFAULT_ASSET_ROOT
        restored_assets.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(packaged_assets, restored_assets)
        if audit_tree(packaged_assets) != expected_asset_audit:
            raise RecoveryError("package_asset_changed_during_restore")
        if audit_tree(restored_assets) != expected_asset_audit:
            raise RecoveryError("restored_asset_mismatch")

        metadata = staging / "recovery_metadata"
        metadata.mkdir()
        inventory_gates = {
            item["path"]: {
                "exists": True,
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in manifest["artifact_inventory"]
        }
        for name in (
            "release_manifest.json",
            "schema_manifest.json",
            "logical_summaries.json",
            "provider_configuration_template.json",
            "RECOVERY.md",
        ):
            source = _safe_manifest_path(package_root, name)
            expected_gate = (
                manifest_gate_before if name == "release_manifest.json" else inventory_gates.get(name)
            )
            if expected_gate is None or _file_gate(source) != expected_gate:
                raise RecoveryError("package_metadata_changed_before_restore")
            shutil.copy2(source, metadata / name)
            if _file_gate(source) != expected_gate:
                raise RecoveryError("package_metadata_changed_during_restore")
            if _file_gate(metadata / name) != expected_gate:
                raise RecoveryError("restored_metadata_file_gate_mismatch")
        packaged_reports = _safe_manifest_path(
            package_root,
            manifest["release_reports"]["backup_relative_path"],
            directory=True,
        )
        expected_report_audit = manifest["release_reports"]["backup_audit"]
        if audit_tree(packaged_reports) != expected_report_audit:
            raise RecoveryError("package_release_report_changed_before_restore")
        shutil.copytree(packaged_reports, metadata / "release_reports")
        if audit_tree(packaged_reports) != expected_report_audit:
            raise RecoveryError("package_release_report_changed_during_restore")
        if audit_tree(metadata / "release_reports") != expected_report_audit:
            raise RecoveryError("restored_release_report_mismatch")

        operational = staging / DEFAULT_DATABASES["operational"]
        cognitive = _cognitive_summary(operational, phase=selected_phase)
        expected_logical = _read_json(metadata / "logical_summaries.json")
        if cognitive != expected_logical.get("cognitive"):
            raise RecoveryError("restored_cognitive_summary_mismatch")
        rebuild = _operational_rebuild_rehearsal(operational)
        runtime: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "phase": selected_phase,
            "mode": "real-readonly",
            "requested_loopback_origin": f"http://127.0.0.1:{port}",
            "production_database_connected": False,
            "write_routes_enabled": False,
            "restored_database_paths": restored_paths,
            "asset_path": DEFAULT_ASSET_ROOT.as_posix(),
            "provider_template_path": (
                "recovery_metadata/provider_configuration_template.json"
            ),
            "release_reports_path": "recovery_metadata/release_reports",
            "ready_for_isolated_readonly_start": True,
            "ownership_marker_identity_digest": ownership_identity_digest,
            "source_service_quiescence_digest": manifest[
                "service_quiescence"
            ]["evidence_digest"],
        }
        runtime["runtime_digest"] = canonical_digest(runtime)
        _write_json(staging / "restore_runtime.json", runtime)
        _write_sealed_owner_marker(
            staging,
            purpose="restore",
            nonce=nonce,
            phase=selected_phase,
            manifest_digest=manifest["manifest_digest"],
            backup_set_digest=manifest["backup_set_digest"],
            runtime_digest=runtime["runtime_digest"],
        )
        _assert_directory_identity(parent, parent_identity)
        if target.exists() or target.is_symlink():
            raise RecoveryError("destination_exists")
        staging.rename(target)
        _assert_owned_target_identity(target, staging_identity)
        _assert_directory_identity(parent, parent_identity)
        if _safe_existing_root(target, directory=True).parent != parent:
            raise RecoveryError("destination_parent_identity_changed")
        _validate_sealed_owner_marker(
            target,
            purpose="restore",
            phase=selected_phase,
            manifest_digest=manifest["manifest_digest"],
            backup_set_digest=manifest["backup_set_digest"],
            ownership_identity_digest=ownership_identity_digest,
            runtime_digest=runtime["runtime_digest"],
        )
        succeeded = True
        report: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status": "restored",
            "phase": selected_phase,
            "destination": str(target),
            "requested_loopback_origin": runtime["requested_loopback_origin"],
            "production_database_connected": False,
            "database_checks": database_checks,
            "asset_check": True,
            "release_report_check": True,
            "cognitive_summary_match": True,
            "operational_projection_rebuild": rebuild,
            "source_package_verification_digest": verification["report_digest"],
            "backup_set_digest": manifest["backup_set_digest"],
            "source_service_quiescence_digest": manifest[
                "service_quiescence"
            ]["evidence_digest"],
        }
        report["report_digest"] = canonical_digest(report)
        return report
    finally:
        if not succeeded and staging.exists():
            _cleanup_owned_staging(staging, nonce, staging_identity)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--phase", choices=RECOVERY_PHASES, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--rehearsal-port", type=int, default=18765)
    build.add_argument("--service-quiescence-digest", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--package", type=Path, required=True)
    verify.add_argument(
        "--expected-phase", choices=RECOVERY_PHASES, required=True
    )
    restore = subparsers.add_parser("restore")
    restore.add_argument("--package", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--port", type=int, required=True)
    restore.add_argument(
        "--expected-phase", choices=RECOVERY_PHASES, required=True
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = build_package(
                phase=args.phase,
                output=args.output,
                rehearsal_port=args.rehearsal_port,
                service_quiescence_digest=args.service_quiescence_digest,
            )
        elif args.command == "verify":
            report = verify_package(
                args.package, expected_phase=args.expected_phase
            )
        else:
            report = restore_package(
                package=args.package,
                destination=args.destination,
                port=args.port,
                expected_phase=args.expected_phase,
            )
    except (OSError, sqlite3.Error, OperationalStateError, RecoveryError) as exc:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "blocked",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
