"""Build and verify YunOS v9 production recovery packages.

The package builder performs read-only SQLite online backups, captures source
DB/WAL/SHM gates, copies the ingestion asset tree, and verifies every database
again from an independent temporary restore directory.  It never opens a
production database for writing.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.operational_state_models import canonical_digest, canonical_json  # noqa: E402
from memory.operational_state_store import OperationalStateStore  # noqa: E402


CONTRACT_VERSION = "yunos-v9-release-recovery-1"
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


class RecoveryError(RuntimeError):
    """A fail-closed recovery-package validation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256": None}
    return {
        "exists": True,
        "size": path.stat().st_size,
        "sha256": _hash_file(path),
    }


def _sqlite_file_gates(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "db": _file_gate(path),
        "wal": _file_gate(Path(str(path) + "-wal")),
        "shm": _file_gate(Path(str(path) + "-shm")),
    }


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
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


def _schema_digest(connection: sqlite3.Connection) -> str:
    rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    return canonical_digest(rows)


def audit_database(label: str, path: Path) -> dict[str, Any]:
    """Return a content-safe logical and physical audit of one SQLite DB."""
    path = path.resolve(strict=True)
    before = _sqlite_file_gates(path)
    uri = path.as_uri() + "?mode=ro&immutable=1"
    started = time.perf_counter()
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        schema_digest = _schema_digest(connection)
        tables: dict[str, dict[str, Any]] = {}
        for table in _table_names(connection):
            count, content_digest = _table_content_digest(connection, table)
            tables[table] = {
                "row_count": count,
                "content_digest": content_digest,
            }
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    after = _sqlite_file_gates(path)
    logical = {
        "schema_digest": schema_digest,
        "user_version": user_version,
        "tables": tables,
    }
    return {
        "label": label,
        "file_gates": before,
        "source_file_gates_unchanged": before == after,
        "schema_digest": schema_digest,
        "user_version": user_version,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
        "tables": tables,
        "logical_digest": canonical_digest(logical),
        "audit_seconds": round(time.perf_counter() - started, 6),
    }


def _iter_tree_files(root: Path) -> Iterable[Path]:
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        names[:] = sorted(names)
        for name in sorted(filenames):
            path = directory_path / name
            if path.is_symlink():
                raise RecoveryError("asset_symlink_not_supported")
            yield path


def audit_tree(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
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


def _online_backup(source: Path, target: Path) -> None:
    uri = source.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        source_connection.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_connection.execute("PRAGMA journal_mode=DELETE")
            target_connection.commit()


def _copy_sqlite_sidecars(
    source: Path, target_directory: Path
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    target_directory.mkdir(parents=True, exist_ok=True)
    for suffix, key in (("-wal", "wal"), ("-shm", "shm")):
        source_sidecar = Path(str(source) + suffix)
        before = _file_gate(source_sidecar)
        if before["exists"]:
            target = target_directory / (source.name + suffix)
            shutil.copy2(source_sidecar, target)
            copied = _file_gate(target)
            after = _file_gate(source_sidecar)
            if before != after or copied != before:
                raise RecoveryError("sqlite_sidecar_changed_during_copy")
        else:
            copied = {"exists": False, "size": 0, "sha256": None}
        results[key] = {"source": before, "packaged": copied}
    return results


def _validate_audit_match(
    source: Mapping[str, Any], restored: Mapping[str, Any]
) -> None:
    if source["integrity_check"] != ["ok"]:
        raise RecoveryError("source_integrity_failed")
    if restored["integrity_check"] != ["ok"]:
        raise RecoveryError("restored_integrity_failed")
    if source["foreign_key_violation_count"] != 0:
        raise RecoveryError("source_foreign_key_failed")
    if restored["foreign_key_violation_count"] != 0:
        raise RecoveryError("restored_foreign_key_failed")
    for key in ("schema_digest", "logical_digest", "user_version"):
        if source[key] != restored[key]:
            raise RecoveryError("restored_" + key + "_mismatch")


def _latest_snapshot_id(database: Path) -> str | None:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT snapshot_id FROM state_snapshots "
            "ORDER BY through_sequence DESC, created_at DESC LIMIT 1"
        ).fetchone()
    return None if row is None else str(row[0])


def _verify_operational_rebuild(database: Path) -> dict[str, Any]:
    store = OperationalStateStore(database)
    before = store.list_projections()
    snapshot_id = _latest_snapshot_id(database)
    if snapshot_id is None:
        raise RecoveryError("operational_snapshot_missing")
    snapshot = store.verify_snapshot(snapshot_id)
    rebuilt = store.rebuild_projections(snapshot_id=snapshot_id)
    after = store.list_projections()
    if before != after:
        raise RecoveryError("operational_projection_rebuild_mismatch")
    return {
        "snapshot_id": snapshot_id,
        "snapshot_verified": snapshot["status"] == "verified",
        "projection_count": len(after),
        "projection_rebuild_status": rebuilt["status"],
        "projection_digests_match": True,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _manifest_without_digest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(manifest)
    result.pop("manifest_digest", None)
    return result


def _source_alias(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return "<disposable-rehearsal>/" + path.name


def build_package(
    *,
    phase: str,
    output: Path,
    project_root: Path = PROJECT_ROOT,
    database_paths: Mapping[str, Path] | None = None,
    asset_root: Path | None = None,
    include_assets: bool = True,
) -> dict[str, Any]:
    if phase not in {"preproduction", "final"}:
        raise RecoveryError("invalid_recovery_phase")
    expected_labels = (
        ("main", "profile", "semantic", "runtime")
        if phase == "preproduction"
        else ("main", "profile", "semantic", "runtime", "operational")
    )
    project_root = project_root.resolve(strict=True)
    configured = database_paths or DEFAULT_DATABASES
    sources = {
        label: (
            configured[label]
            if configured[label].is_absolute()
            else project_root / configured[label]
        )
        for label in expected_labels
    }
    assets = asset_root or project_root / "data/ingestion_assets"
    if output.exists():
        raise RecoveryError("recovery_output_exists")
    output.mkdir(parents=True)
    database_directory = output / "databases"
    sidecar_directory = output / "sqlite_sidecars"
    database_directory.mkdir()
    sidecar_directory.mkdir()
    started = time.perf_counter()
    database_results: dict[str, Any] = {}
    for label, source in sources.items():
        source_audit = audit_database(label, source)
        backup = database_directory / BACKUP_FILENAMES[label]
        _online_backup(source, backup)
        backup_audit = audit_database(label + "-backup", backup)
        _validate_audit_match(source_audit, backup_audit)
        sidecars = _copy_sqlite_sidecars(source, sidecar_directory / label)
        database_results[label] = {
            "source_relative_path": _source_alias(source, project_root),
            "backup_relative_path": backup.relative_to(output).as_posix(),
            "source_audit": source_audit,
            "backup_audit": backup_audit,
            "sqlite_sidecars": sidecars,
        }
    asset_result: dict[str, Any]
    if include_assets:
        source_asset_audit = audit_tree(assets)
        packaged_assets = output / "assets"
        shutil.copytree(assets, packaged_assets)
        packaged_asset_audit = audit_tree(packaged_assets)
        if source_asset_audit != packaged_asset_audit:
            raise RecoveryError("asset_backup_mismatch")
        asset_result = {
            "included": True,
            "source_relative_path": assets.relative_to(project_root).as_posix(),
            "backup_relative_path": "assets",
            "source_audit": source_asset_audit,
            "backup_audit": packaged_asset_audit,
        }
    else:
        asset_result = {"included": False}

    with tempfile.TemporaryDirectory(prefix="ayunos-v9-restore-") as name:
        restore_root = Path(name)
        restore_results: dict[str, Any] = {}
        operational_rebuild: dict[str, Any] | None = None
        for label in expected_labels:
            source_audit = database_results[label]["source_audit"]
            restored = restore_root / BACKUP_FILENAMES[label]
            shutil.copy2(
                database_directory / BACKUP_FILENAMES[label],
                restored,
            )
            restored_audit = audit_database(label + "-restored", restored)
            _validate_audit_match(source_audit, restored_audit)
            restore_results[label] = {
                "integrity_check": restored_audit["integrity_check"],
                "foreign_key_violation_count": restored_audit[
                    "foreign_key_violation_count"
                ],
                "schema_digest_match": (
                    restored_audit["schema_digest"]
                    == source_audit["schema_digest"]
                ),
                "logical_digest_match": (
                    restored_audit["logical_digest"]
                    == source_audit["logical_digest"]
                ),
            }
            if label == "operational":
                operational_rebuild = _verify_operational_rebuild(restored)

    backup_set_basis = {
        "databases": {
            label: {
                "backup_sha256": result["backup_audit"]["file_gates"]["db"][
                    "sha256"
                ],
                "schema_digest": result["backup_audit"]["schema_digest"],
                "logical_digest": result["backup_audit"]["logical_digest"],
                "sidecars": result["sqlite_sidecars"],
            }
            for label, result in database_results.items()
        },
        "assets": asset_result,
    }
    manifest: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "phase": phase,
        "created_at": _utc_now(),
        "method": "sqlite_online_backup_plus_asset_tree_copy",
        "production_databases_opened_read_only": True,
        "production_writer_enabled": False,
        "database_count": len(expected_labels),
        "database_results": database_results,
        "assets": asset_result,
        "restore_rehearsal": {
            "passed": True,
            "database_results": restore_results,
            "operational_projection_rebuild": operational_rebuild,
        },
        "backup_set_digest": canonical_digest(backup_set_basis),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    _write_json(output / "release_backup_manifest.json", manifest)
    schema_manifest = {
        "contract_version": CONTRACT_VERSION,
        "phase": phase,
        "databases": {
            label: {
                "schema_digest": result["backup_audit"]["schema_digest"],
                "user_version": result["backup_audit"]["user_version"],
                "table_names": sorted(result["backup_audit"]["tables"]),
            }
            for label, result in database_results.items()
        },
    }
    schema_manifest["schema_manifest_digest"] = canonical_digest(schema_manifest)
    _write_json(output / "schema_manifest.json", schema_manifest)
    (output / "RECOVERY.md").write_text(
        "# YunOS v9 Recovery Package\n\n"
        "Restore only to an isolated directory first. Validate the manifest, "
        "all SQLite integrity and foreign-key checks, schema digests, logical "
        "digests, the asset tree digest, and the operational snapshot rebuild "
        "before any human-approved production cutover. The packaged WAL/SHM "
        "files are forensic evidence; recovery uses the consistent online "
        "backup database files under `databases/`.\n",
        encoding="utf-8",
    )
    return manifest


def verify_package(package: Path) -> dict[str, Any]:
    package = package.resolve(strict=True)
    manifest_path = package / "release_backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = canonical_digest(_manifest_without_digest(manifest))
    if manifest.get("manifest_digest") != expected:
        raise RecoveryError("recovery_manifest_digest_mismatch")
    database_checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="ayunos-v9-package-verify-") as name:
        restore_root = Path(name)
        operational_rebuild = None
        for label, result in manifest["database_results"].items():
            source = package / result["backup_relative_path"]
            audit = audit_database(label + "-package", source)
            backup_audit = result["backup_audit"]
            _validate_audit_match(backup_audit, audit)
            restored = restore_root / BACKUP_FILENAMES[label]
            shutil.copy2(source, restored)
            restore_audit = audit_database(label + "-verify-restore", restored)
            _validate_audit_match(backup_audit, restore_audit)
            database_checks[label] = True
            if label == "operational":
                operational_rebuild = _verify_operational_rebuild(restored)
    asset_check = True
    if manifest["assets"]["included"]:
        asset_check = (
            audit_tree(package / manifest["assets"]["backup_relative_path"])
            == manifest["assets"]["backup_audit"]
        )
    if not asset_check or not all(database_checks.values()):
        raise RecoveryError("recovery_package_verification_failed")
    report = {
        "contract_version": CONTRACT_VERSION,
        "status": "verified",
        "phase": manifest["phase"],
        "manifest_digest": manifest["manifest_digest"],
        "backup_set_digest": manifest["backup_set_digest"],
        "database_checks": database_checks,
        "asset_check": asset_check,
        "operational_projection_rebuild": operational_rebuild,
    }
    return {**report, "report_digest": canonical_digest(report)}


def _parse_database_overrides(values: list[str]) -> dict[str, Path]:
    result = dict(DEFAULT_DATABASES)
    for value in values:
        if "=" not in value:
            raise RecoveryError("invalid_database_override")
        label, raw_path = value.split("=", 1)
        if label not in DEFAULT_DATABASES or not raw_path:
            raise RecoveryError("invalid_database_override")
        result[label] = Path(raw_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--phase", choices=("preproduction", "final"), required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    build.add_argument("--database", action="append", default=[])
    build.add_argument("--asset-root", type=Path)
    build.add_argument("--without-assets", action="store_true")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--package", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = build_package(
                phase=args.phase,
                output=args.output,
                project_root=args.project_root,
                database_paths=_parse_database_overrides(args.database),
                asset_root=args.asset_root,
                include_assets=not args.without_assets,
            )
        else:
            report = verify_package(args.package)
    except (OSError, sqlite3.Error, RecoveryError) as exc:
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
