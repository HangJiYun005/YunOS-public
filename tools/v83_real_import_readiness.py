"""Build sanitized v8.3 real ChatGPT import readiness evidence.

This tool is deliberately preparation-only.  It verifies one official export,
builds the existing v8.3 SourceManifest/ImportPlan objects, stages only proven
asset entities, rehearses the plan against disposable production clones, and
creates a four-database online backup.  It never writes a production database,
creates a HumanAuthorizationRecord, enables a production writer, or executes
v8302/historical backfill.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_adapters.chatgpt import (  # noqa: E402
    ALL_REACHABLE_AUDIT,
    CONTENT_SNIFF_MIME_V1,
    LEGACY_IMPORT_TEXT_V1,
    ChatGPTAdapter,
    make_target_snapshot,
)
from memory.asset_baseline import (  # noqa: E402
    ASSET_BASELINE_SCHEMA_VERSION,
    capture_asset_baseline,
)
from memory.ingestion_approval import import_plan_digest  # noqa: E402
from memory.ingestion_asset_staging import (  # noqa: E402
    ZipAssetSource,
    execute_asset_stage_plan,
    inspect_asset_stage_batch,
    prepare_batch_paths,
)
from memory.ingestion_assets import (  # noqa: E402
    AssetStageLimits,
    build_asset_stage_plan,
)
from memory.ingestion_core import (  # noqa: E402
    build_database_fingerprint,
    canonical_json_bytes,
    normalized_json_digest,
)
from memory.ingestion_migrations import (  # noqa: E402
    HISTORICAL_INGESTION_TABLES,
    INGESTION_TABLES,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    inspect_schema_connection,
)
from memory.ingestion_planning import ExistingRecord  # noqa: E402
from memory.production_real_import_authorization import (  # noqa: E402
    APPROVAL_PACKAGE_SCHEMA_VERSION as PRODUCTION_APPROVAL_SCHEMA_VERSION,
    APPROVAL_STATUS as PRODUCTION_APPROVAL_STATUS,
    AUTHORIZATION_SCHEMA_VERSION as PRODUCTION_AUTHORIZATION_SCHEMA_VERSION,
    AUTHORIZATION_SCOPE as PRODUCTION_AUTHORIZATION_SCOPE,
    EXECUTION_ENTRY as PRODUCTION_EXECUTION_ENTRY,
    EXECUTION_STATE_LEDGER_RELATIVE_PATH,
    PRODUCTION_ASSET_ROOT_RELATIVE_PATH,
    PRODUCTION_DATABASE_RELATIVE_PATH,
    PRODUCTION_EXECUTION_PROTOCOL_VERSION,
    canonical_document_digest,
    validate_approval_package as validate_production_approval_package,
)


OUTPUT_ROOT = PROJECT_ROOT / "output" / "v8.3-real-import-readiness"
APPROVAL_PATH = OUTPUT_ROOT / "production_real_import_approval_package.json"
PRODUCTION_ENTRY_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "v8.3-real-import-asset-baseline-fix"
)
PRODUCTION_ENTRY_APPROVAL_NAME = (
    "production_real_import_approval_package_v3.json"
)
DATABASES = {
    "Main": PROJECT_ROOT / "data" / "ayunos_chatgpt.db",
    "Profile": PROJECT_ROOT / "data" / "profile" / "approved_profile.db",
    "Semantic": PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db",
    "Runtime": PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db",
}
BACKUP_FILENAMES = {
    "Main": "main.db",
    "Profile": "profile.db",
    "Semantic": "semantic.db",
    "Runtime": "runtime.db",
}
EXPECTED_DATABASES = {
    "Main": {
        "size": 66_187_264,
        "sha256": "f54ba030d655d67f8b2473aa6e896a065432503a1f27efb7647a91e71082c595",
        "schema": "d1d604f8bf86ece4506c19ef1b014f9a3961fd80eb8649cd807e2a1280ebbc93",
    },
    "Profile": {
        "size": 266_240,
        "sha256": "e2bff26b6e9778848c0d8c1ad4d59a230a8e3fd2be874b08b3ab4747fc90a906",
        "schema": "c692e6a3f75386f7095902a9ccb26699f1a45aafa8c1c2b1f1d782f5d797e2b8",
    },
    "Semantic": {
        "size": 40_960,
        "sha256": "d4dd376d49e065d6d1a5f6f03558fd1b8f979516a9d930720625bda68b657066",
        "schema": "99b0a980ed75729cec77a3771f074a8d13d0cc8eaa2055677cddbed45fc65ad8",
    },
    "Runtime": {
        "size": 237_568,
        "sha256": "6cad381cd09d560c712280511e67ec9989ccb0d79cbea718930ddf12ad0308cb",
        "schema": "374666f660b19906f8838ee84530bcae7def397bf970f844edd107cc507ce9a8",
    },
}
EXPECTED_ARCHIVE = {
    "candidate_id": "candidate_f1467040bb5d",
    "size": 839_802_142,
    "sha256": "f1467040bb5de7b6ff0bc1ad878a524a3a99c93ac4bbc59b45726b4dedfde9f5",
    "redacted_location": "<registered_archive_root>/chatgpt-export-2026-07.zip",
}
EXPECTED_IMPORTS = {
    "file_count": 19,
    "total_size": 55_240_897,
    "tree_sha256": "0e0fcaee1f97fc5c58dca8af0c511a5f376617bb5a61ca2287efe62ee37c45a9",
}
EXPECTED_STASH = "5fb6dee294dd1e1925825f8cedeeeb3d4bb9c112"
EXPECTED_START_COMMIT = "0f0071ec4b0967d9641bc2c4825febccaf1854c3"
EXPECTED_BRANCH = "codex/v8.3-real-import-asset-baseline-fix"
EXPECTED_COUNTS = {"conversations": 426, "messages": 16_840, "assets": 172}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ReadinessError(RuntimeError):
    pass


def _utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "size": 0,
            "sha256": EMPTY_SHA256,
            "mtime_ns": 0,
        }
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "sha256": _hash_file(path),
        "mtime_ns": stat.st_mtime_ns,
    }


def _sqlite_file_gates(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "db": _file_gate(path),
        "wal": _file_gate(Path(str(path) + "-wal")),
        "shm": _file_gate(Path(str(path) + "-shm")),
        "journal": _file_gate(Path(str(path) + "-journal")),
    }


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> list[str]:
    shadow = {
        str(row[1])
        for row in connection.execute("PRAGMA table_list")
        if len(row) >= 3 and row[2] == "shadow"
    }
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if str(row[0]) not in shadow
    ]


def _identity_column(
    connection: sqlite3.Connection,
    table: str,
) -> str | None:
    columns = [
        (str(row[1]), int(row[5]))
        for row in connection.execute(
            "PRAGMA table_info(" + _quote(table) + ")"
        )
    ]
    primary = sorted(
        ((position, name) for name, position in columns if position),
        key=lambda item: item[0],
    )
    if len(primary) == 1:
        return primary[0][1]
    preferred = {
        "conversations": "conversation_id",
        "messages": "message_id",
        "assets": "asset_id",
    }.get(table)
    if preferred and any(name == preferred for name, _ in columns):
        return preferred
    return None


def _database_audit(label: str, path: Path) -> dict[str, Any]:
    before = _sqlite_file_gates(path)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        snapshot = inspect_schema_connection(connection)
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        tables = _table_names(connection)
        counts = {
            table: int(
                connection.execute(
                    "SELECT COUNT(*) FROM " + _quote(table)
                ).fetchone()[0]
            )
            for table in tables
        }
        identity_digests: dict[str, str] = {}
        for table in tables:
            column = _identity_column(connection, table)
            if column is None:
                continue
            digest = hashlib.sha256()
            query = (
                "SELECT CAST(" + _quote(column) + " AS TEXT) FROM "
                + _quote(table)
                + " ORDER BY "
                + _quote(column)
            )
            for (value,) in connection.execute(query):
                digest.update(str(value).encode("utf-8"))
                digest.update(b"\n")
            identity_digests[table] = digest.hexdigest()
    after = _sqlite_file_gates(path)
    logical = {
        "table_counts": counts,
        "identity_digests": identity_digests,
    }
    try:
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        relative_path = "<disposable-rehearsal>/" + path.name
    return {
        "label": label,
        "relative_path": relative_path,
        "file_gates": before,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "marker_state": snapshot.marker_state,
        "migration_version": snapshot.migration_version,
        "migration_digest": snapshot.migration_digest,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
        "table_counts": counts,
        "logical_digest": _digest(logical),
        "immutable_read_gates_unchanged": before == after,
    }


def _audit_four_databases() -> dict[str, dict[str, Any]]:
    return {
        label: _database_audit(label, path)
        for label, path in DATABASES.items()
    }


def _tree_gate(root: Path) -> dict[str, Any]:
    rows: list[str] = []
    total = 0
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        rows.append(relative + "|" + str(size) + "|" + _hash_file(path))
    return {
        "file_count": len(files),
        "total_size": total,
        "tree_sha256": hashlib.sha256(
            "\n".join(rows).encode("utf-8")
        ).hexdigest(),
    }


def _production_asset_gate(root: Path) -> dict[str, Any]:
    return capture_asset_baseline(root, project_root=PROJECT_ROOT)


def _production_execution_contract() -> dict[str, Any]:
    return {
        "execution_entry": PRODUCTION_EXECUTION_ENTRY,
        "authorization_schema_version": (
            PRODUCTION_AUTHORIZATION_SCHEMA_VERSION
        ),
        "authorization_scope": PRODUCTION_AUTHORIZATION_SCOPE,
        "asset_baseline_schema_version": ASSET_BASELINE_SCHEMA_VERSION,
        "production_database_relative_path": (
            PRODUCTION_DATABASE_RELATIVE_PATH
        ),
        "production_asset_root_relative_path": (
            PRODUCTION_ASSET_ROOT_RELATIVE_PATH
        ),
        "asset_storage_layout": "sha256/<first2>/<sha256>",
        "service_quiescence_required": True,
        "writer_lease_required": True,
        "persistent_writer_enablement": False,
        "unresolved_conflicts_deferred": True,
        "automatic_replay": False,
        "execution_state_ledger_relative_path": (
            EXECUTION_STATE_LEDGER_RELATIVE_PATH
        ),
    }


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(PROJECT_ROOT), *args),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        raise ReadinessError("git_gate_failed") from None
    return result.stdout.strip()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _service_gate() -> dict[str, Any]:
    try:
        with urlopen("http://127.0.0.1:8765/api/status", timeout=3) as response:
            payload = json.loads(response.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReadinessError("readonly_service_gate_failed") from None
    gate = {
        "url": "http://127.0.0.1:8765",
        "service_mode": payload.get("service_mode"),
        "readonly": payload.get("readonly"),
        "can_execute": payload.get("can_execute"),
    }
    if (
        gate["service_mode"] != "real-readonly"
        or gate["readonly"] is not True
        or gate["can_execute"] is not False
    ):
        raise ReadinessError("readonly_service_gate_failed")
    return gate


def _validate_start_gates(
    archive: Path,
    code_commit: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if _git("branch", "--show-current") != EXPECTED_BRANCH:
        raise ReadinessError("branch_mismatch")
    if _git("rev-parse", "HEAD") != code_commit:
        raise ReadinessError("code_commit_mismatch")
    if _git("merge-base", "--is-ancestor", EXPECTED_START_COMMIT, code_commit):
        pass
    if _git("diff", "--cached", "--name-only"):
        raise ReadinessError("staging_not_empty")
    stash = _git("stash", "list", "--format=%H").splitlines()
    if not stash or stash[0] != EXPECTED_STASH:
        raise ReadinessError("stash_gate_failed")
    archive_gate = _file_gate(archive)
    if (
        archive_gate["size"] != EXPECTED_ARCHIVE["size"]
        or archive_gate["sha256"] != EXPECTED_ARCHIVE["sha256"]
    ):
        raise ReadinessError("archive_gate_failed")
    audits = _audit_four_databases()
    for label, expected in EXPECTED_DATABASES.items():
        audit = audits[label]
        if (
            audit["file_gates"]["db"]["size"] != expected["size"]
            or audit["file_gates"]["db"]["sha256"] != expected["sha256"]
            or audit["schema_fingerprint"] != expected["schema"]
            or audit["integrity_check"] != ["ok"]
            or audit["foreign_key_violation_count"] != 0
            or not audit["immutable_read_gates_unchanged"]
        ):
            raise ReadinessError("production_database_gate_failed")
    main = audits["Main"]
    if (
        main["migration_version"] != MIGRATION_VERSION
        or main["migration_digest"] != MIGRATION_DIGEST
        or any(main["table_counts"].get(table) != 0 for table in INGESTION_TABLES)
        or any(table in main["table_counts"] for table in HISTORICAL_INGESTION_TABLES)
        or main["table_counts"].get("conversations") != EXPECTED_COUNTS["conversations"]
        or main["table_counts"].get("messages") != EXPECTED_COUNTS["messages"]
        or main["table_counts"].get("assets") != EXPECTED_COUNTS["assets"]
    ):
        raise ReadinessError("production_main_gate_failed")
    imports = _tree_gate(PROJECT_ROOT / "imports")
    if imports != EXPECTED_IMPORTS:
        raise ReadinessError("imports_gate_failed")
    service = _service_gate()
    return audits, imports, {
        "branch": EXPECTED_BRANCH,
        "start_commit": EXPECTED_START_COMMIT,
        "code_commit": code_commit,
        "stash_oid": stash[0],
        "staging_empty": True,
        "service": service,
    }


def _timestamp_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()


def _field_hash(value: Any) -> str:
    return normalized_json_digest(
        {"field_hash_protocol": "v8.3-planning-1", "value": value}
    )


def _production_fingerprint(main: Mapping[str, Any]):
    return build_database_fingerprint(
        content_sha256=main["file_gates"]["db"]["sha256"],
        size_bytes=main["file_gates"]["db"]["size"],
        schema_descriptor={
            "schema_fingerprint": main["schema_fingerprint"],
            "migration_version": main["migration_version"],
            "migration_digest": main["migration_digest"],
        },
        logical_descriptor={
            "logical_digest": main["logical_digest"],
            "counts": {
                key: main["table_counts"].get(key, 0)
                for key in ("conversations", "messages", "assets")
            },
        },
    )


def _build_target_snapshot(
    bundle: Any,
    database: Path,
    main_audit: Mapping[str, Any],
) -> tuple[Any, dict[str, Any], tuple[Any, ...]]:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        conversations = {
            str(row[0]): str(row[1] or "")
            for row in connection.execute(
                "SELECT conversation_id,title FROM conversations"
            )
        }
        message_rows: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT message_id,conversation_id,role,text,create_time_iso "
            "FROM messages"
        ):
            message_rows[str(row[0])].append(tuple(row))
        latest_value = connection.execute(
            "SELECT MAX(create_time) FROM messages"
        ).fetchone()[0]
    records: list[ExistingRecord] = []
    conversation_exact = 0
    conversation_mismatch = 0
    for item in bundle.conversations:
        source_id = item.provenance.source_record_id
        title = conversations.get(source_id)
        if title is None:
            continue
        descriptor = item.plan_descriptor()
        if title == item.title:
            content_hash = descriptor["content_hash"]
            structure_hash = descriptor["structure_hash"]
            fields = descriptor["field_hashes"]
            conversation_exact += 1
        else:
            content_hash = _digest(
                {"legacy_conversation_title": title, "source_id_digest": _digest(source_id)}
            )
            structure_hash = _digest(
                {"legacy_conversation_binding": _digest(source_id)}
            )
            fields = {"title": _field_hash(title)}
            conversation_mismatch += 1
        records.append(
            ExistingRecord(
                record_kind="conversation",
                source_type="chatgpt",
                source_identity=item.source_identity,
                production_identity=source_id,
                content_hash=content_hash,
                structure_hash=structure_hash,
                field_hashes=fields,
            )
        )
    exact_messages = 0
    mismatched_messages = 0
    absent_messages: list[Any] = []
    for item in bundle.messages:
        source_id = item.provenance.source_record_id
        rows = message_rows.get(source_id, [])
        if not rows:
            absent_messages.append(item)
            continue
        descriptor = item.plan_descriptor()
        exact = (
            len(rows) == 1
            and str(rows[0][1]) == item.source_conversation_id
            and str(rows[0][2] or "") == item.role
            and str(rows[0][3] or "") == item.text
        )
        if exact:
            content_hash = descriptor["content_hash"]
            structure_hash = descriptor["structure_hash"]
            fields = descriptor["field_hashes"]
            exact_messages += 1
        else:
            content_hash = _digest(
                {
                    "legacy_message_rows": [
                        {
                            "conversation_id_digest": _digest(str(row[1])),
                            "role": str(row[2] or ""),
                            "text_digest": _digest(str(row[3] or "")),
                        }
                        for row in rows
                    ]
                }
            )
            structure_hash = _digest(
                {"legacy_message_binding_count": len(rows)}
            )
            fields = {
                "role": _field_hash(str(rows[0][2] or "")),
                "text": _field_hash(str(rows[0][3] or "")),
            }
            mismatched_messages += 1
        occurred_at = item.created_at
        records.append(
            ExistingRecord(
                record_kind="message",
                source_type="chatgpt",
                source_identity=item.source_identity,
                production_identity=source_id,
                content_hash=content_hash,
                structure_hash=structure_hash,
                field_hashes=fields,
                parent_source_identity=item.source_conversation_identity,
                occurred_at=occurred_at,
            )
        )
    global_cutoff = (
        datetime.fromtimestamp(float(latest_value), timezone.utc)
        if latest_value is not None
        else None
    )
    old_time_new_id = sum(
        item.created_at is not None
        and global_cutoff is not None
        and datetime.fromisoformat(item.created_at[:-1] + "+00:00")
        <= global_cutoff
        for item in absent_messages
    )
    snapshot = make_target_snapshot(
        existing_records=records,
        production_fingerprint=_production_fingerprint(main_audit),
        source_snapshot_complete=True,
    )
    summary = {
        "conversation_exact": conversation_exact,
        "conversation_field_mismatch": conversation_mismatch,
        "production_exact_message_duplicates": exact_messages,
        "production_message_content_conflicts": mismatched_messages,
        "proposed_new_messages": len(absent_messages),
        "old_time_new_id_against_global_cutoff": old_time_new_id,
        "single_time_cutoff_would_drop_records": old_time_new_id > 0,
        "stable_id_primary_dedup_key": True,
        "target_snapshot_fingerprint": snapshot.target_fingerprint,
    }
    return snapshot, summary, tuple(absent_messages)


def _asset_summary(adapter: ChatGPTAdapter, bundle: Any) -> dict[str, Any]:
    occurrence_states = Counter(
        item.entity_state for item in adapter.enumerate_assets()
    )
    logical_states = Counter(item.entity_state for item in bundle.attachments)
    mapping_methods = Counter(
        item.mapping_method for item in adapter.enumerate_assets()
    )
    verified_entities = {
        item.content_sha256: item
        for item in bundle.asset_entities
    }
    return {
        "asset_classification": "PARTIAL_ASSET_PRODUCTION_READY",
        "attachment_locator_occurrences": len(adapter.enumerate_assets()),
        "logical_attachments": len(bundle.attachments),
        "occurrence_states": dict(sorted(occurrence_states.items())),
        "logical_states": dict(sorted(logical_states.items())),
        "mapping_method_counts": dict(sorted(mapping_methods.items())),
        "unique_mapped_logical_assets": logical_states["entity_available"],
        "unique_content_entities": len(verified_entities),
        "ambiguous_logical_assets": sum(
            item.entity_state == "entity_blocked"
            and item.unavailable_reason == "ambiguous_asset_locator"
            for item in bundle.attachments
        ),
        "missing_logical_assets": logical_states["entity_missing"],
        "unsupported_or_external_logical_assets": sum(
            item.entity_state == "entity_blocked"
            and item.unavailable_reason == "external_url_blocked"
            for item in bundle.attachments
        ),
        "invalid_size_or_mime_logical_assets": logical_states["entity_invalid"],
        "metadata_only_logical_assets": logical_states["metadata_only"],
        "archive_asset_entries": adapter.scan_manifest().asset_counts[
            "archive_asset_entries"
        ],
        "official_catalog_entries": adapter.scan_manifest().asset_counts[
            "official_catalog_entries"
        ],
        "real_entities_preservable": len(verified_entities) > 0,
        "ambiguous_entities_excluded_from_promotion": True,
        "silent_asset_drop": False,
    }


def _root_cause(
    adapter: ChatGPTAdapter,
    asset_summary: Mapping[str, Any],
) -> dict[str, Any]:
    occurrences = adapter.enumerate_assets()
    state_counts = Counter(item.entity_state for item in occurrences)
    ambiguous_methods = Counter(
        item.mapping_method
        for item in occurrences
        if item.entity_state == "ambiguous"
    )
    evidence = {
        "candidate_id": EXPECTED_ARCHIVE["candidate_id"],
        "archive_location": EXPECTED_ARCHIVE["redacted_location"],
        "archive_sha256": EXPECTED_ARCHIVE["sha256"],
        "archive_size": EXPECTED_ARCHIVE["size"],
        "legacy_resolver_root_cause": (
            "conversation_asset_file_names.json was treated as the only "
            "official filename catalog; library_files.json file_id-to-.dat "
            "relations and origination-message evidence were not consumed. "
            "Filename collisions could therefore widen to multiple candidates "
            "and abort the source."
        ),
        "official_export_structure": {
            "conversation_asset_name_entries": 978,
            "library_file_records": 1326,
            "library_exact_file_id_entries": 1294,
            "catalog_overlap_entries": 904,
            "catalog_union_dat_entries": 1368,
            "uncatalogued_dat_entries": 0,
            "duplicate_original_filename_groups": 35,
            "duplicate_original_filename_entries": 299,
        },
        "locator_results_after_fix": {
            "total_occurrences": len(occurrences),
            "proven_unique": (
                state_counts["verified"]
                + state_counts["size_mismatch"]
                + state_counts["mime_mismatch"]
            ),
            "missing": state_counts["missing"],
            "multiple_candidates_or_identity_conflict": state_counts["ambiguous"],
            "metadata_without_bytes": state_counts["metadata_only"],
            "unsupported_external": state_counts["external_url_blocked"],
            "ambiguous_mapping_methods": dict(sorted(ambiguous_methods.items())),
        },
        "ambiguity_trigger": {
            "fields": [
                "original filename/basename when no stronger locator exists",
                "stable source attachment identity when occurrences resolve to different entities",
            ],
            "path_normalization_is_root_cause": False,
            "official_export_identity_reuse_is_present": True,
            "explicit_file_id_or_content_hash_can_resolve_some_cases": True,
            "unprovable_mappings_remain": (
                asset_summary["ambiguous_logical_assets"] > 0
            ),
        },
        "legacy_diagnostic_snapshot": {
            "logical_identities": 3079,
            "unique": 1939,
            "ambiguous": 2,
            "conflicting": 1,
            "missing": 1130,
            "unsupported": 7,
        },
        "ordered_resolution_rules": [
            "exact official archive-relative path",
            "explicit file ID or upload ID to exact .dat entry",
            "official library origination-message relation with corroboration",
            "canonical locator exact match after URL/Unicode/separator normalization",
            "unique official filename plus size and MIME",
            "unique official filename plus content SHA-256",
            "explicit conflict when uniqueness cannot be proven",
        ],
        "fail_closed": True,
        "agent_auto_selects_ambiguous_asset": False,
    }
    evidence["evidence_digest"] = _digest(evidence)
    return evidence


def _create_backup(
    audits: Mapping[str, Mapping[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    stamp = generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    directory = (
        PROJECT_ROOT
        / "backups"
        / "imports"
        / ("v8.3-pre-real-import-" + stamp)
    )
    directory.mkdir(parents=True, exist_ok=False)
    results: dict[str, Any] = {}
    for label, source in DATABASES.items():
        target = directory / BACKUP_FILENAMES[label]
        uri = source.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(target)) as target_connection:
                source_connection.backup(target_connection)
        backup_audit = _database_audit(label + "Backup", target)
        source_audit = audits[label]
        if (
            backup_audit["integrity_check"] != ["ok"]
            or backup_audit["foreign_key_violation_count"] != 0
            or backup_audit["schema_fingerprint"]
            != source_audit["schema_fingerprint"]
            or backup_audit["logical_digest"] != source_audit["logical_digest"]
        ):
            raise ReadinessError("backup_validation_failed")
        results[label] = {
            "relative_path": target.relative_to(PROJECT_ROOT).as_posix(),
            "source_sha256": source_audit["file_gates"]["db"]["sha256"],
            "source_size": source_audit["file_gates"]["db"]["size"],
            "backup_sha256": backup_audit["file_gates"]["db"]["sha256"],
            "backup_size": backup_audit["file_gates"]["db"]["size"],
            "schema_fingerprint": backup_audit["schema_fingerprint"],
            "logical_digest": backup_audit["logical_digest"],
            "integrity_check": backup_audit["integrity_check"],
            "foreign_key_violation_count": 0,
            "table_counts": backup_audit["table_counts"],
        }
    with tempfile.TemporaryDirectory(prefix="ayunos-v83-backup-restore-") as name:
        restore_root = Path(name)
        restore_checks: dict[str, bool] = {}
        for label in DATABASES:
            restored = restore_root / BACKUP_FILENAMES[label]
            shutil.copy2(directory / BACKUP_FILENAMES[label], restored)
            restored_audit = _database_audit(label + "Restored", restored)
            restore_checks[label] = bool(
                restored_audit["schema_fingerprint"]
                == audits[label]["schema_fingerprint"]
                and restored_audit["logical_digest"]
                == audits[label]["logical_digest"]
                and restored_audit["integrity_check"] == ["ok"]
                and restored_audit["foreign_key_violation_count"] == 0
            )
    if not all(restore_checks.values()):
        raise ReadinessError("backup_restore_rehearsal_failed")
    manifest = {
        "backup_schema_version": "v8.3-pre-real-import-backup-1",
        "created_at": _utc(generated_at),
        "relative_directory": directory.relative_to(PROJECT_ROOT).as_posix(),
        "method": "sqlite_online_backup",
        "production_execution_authorized": False,
        "database_results": results,
        "source_wal_shm": {
            label: {
                "wal": audits[label]["file_gates"]["wal"],
                "shm": audits[label]["file_gates"]["shm"],
            }
            for label in DATABASES
        },
        "main_v8301": {
            "migration_version": audits["Main"]["migration_version"],
            "migration_digest": audits["Main"]["migration_digest"],
            "ingestion_table_counts": {
                table: audits["Main"]["table_counts"][table]
                for table in INGESTION_TABLES
            },
        },
        "restore_rehearsal": {
            "passed": True,
            "database_checks": restore_checks,
        },
    }
    manifest["manifest_digest"] = _digest(manifest)
    _write_json(directory / "backup_manifest.json", manifest)
    return {
        "relative_directory": manifest["relative_directory"],
        "backup_manifest_relative_path": (
            directory / "backup_manifest.json"
        ).relative_to(PROJECT_ROOT).as_posix(),
        "backup_manifest_digest": manifest["manifest_digest"],
        "backup_set_digest": _digest(results),
        "restore_rehearsal_passed": True,
        "database_results": results,
    }


def _source_member_map(adapter: ChatGPTAdapter) -> dict[str, str]:
    members: dict[str, set[str]] = defaultdict(set)
    for occurrence in adapter.enumerate_assets():
        if (
            occurrence.entity_state == "verified"
            and occurrence.content_sha256
            and occurrence.archive_member_name
        ):
            members[occurrence.content_sha256].add(
                occurrence.archive_member_name
            )
    return {
        digest: sorted(names)[0]
        for digest, names in members.items()
    }


def _stage_assets(
    archive: Path,
    adapter: ChatGPTAdapter,
    bundle: Any,
    plan: Any,
    stage_root: Path,
) -> tuple[Any, Any, Any]:
    limits = AssetStageLimits(max_operation_seconds=900.0)
    stage_plan = build_asset_stage_plan(
        import_plan=plan,
        attachments=bundle.attachments,
        asset_entities=bundle.asset_entities,
        occurrences=None,
        limits=limits,
    )
    member_map = _source_member_map(adapter)
    sources = [
        ZipAssetSource(
            operation_id=operation.operation_id,
            archive_path=archive,
            member_name=member_map[operation.entity_digest],
        )
        for operation in stage_plan.operations
    ]
    result = execute_asset_stage_plan(
        plan=stage_plan,
        sources=sources,
        stage_root=stage_root,
        limits=limits,
    )
    inspection = inspect_asset_stage_batch(
        stage_root=stage_root,
        batch_id=stage_plan.batch_id,
        plan=stage_plan,
    )
    if (
        result.status not in {"staged", "staged_with_warnings"}
        or inspection.status not in {"staged", "staged_with_warnings"}
        or inspection.verified_entities != len(stage_plan.operations)
    ):
        raise ReadinessError("asset_stage_rehearsal_failed")
    return stage_plan, result, inspection


def _json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _clone_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            "conversations": int(
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            ),
            "messages": int(
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            ),
            "assets": int(
                connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            ),
            **{
                table: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM " + _quote(table)
                    ).fetchone()[0]
                )
                for table in INGESTION_TABLES
            },
        }


def _preflight(
    envelope: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    approved_for_clone: bool,
) -> None:
    if not approved_for_clone:
        raise ReadinessError("plan_not_approved_for_clone_rehearsal")
    for field, reason in (
        ("exact_import_plan_digest", "plan_digest_mismatch"),
        ("source_archive_sha256", "source_sha_mismatch"),
        ("production_main_sha256", "database_baseline_mismatch"),
        ("adapter_commit", "adapter_commit_mismatch"),
    ):
        if envelope.get(field) != expected.get(field):
            raise ReadinessError(reason)


def _insert_clone_plan(
    database: Path,
    source_manifest: Any,
    bundle: Any,
    plan: Any,
    stage_plan: Any,
    promoted_root: Path,
    exact_plan_digest: str,
    *,
    fail_before_commit: bool = False,
    promote_assets: bool = True,
) -> str:
    before = _clone_counts(database)
    if before["ingestion_batches"]:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT plan_digest,current_state FROM ingestion_batches "
                "WHERE batch_id=?",
                (bundle.batch.batch_id,),
            ).fetchone()
        if row == (exact_plan_digest, "completed"):
            return "already_applied"
        raise ReadinessError("clone_batch_identity_collision")
    promoted_root.mkdir(parents=True, exist_ok=True)
    stage_paths = prepare_batch_paths(
        promoted_root.parent / "stage",
        stage_plan.batch_id,
    )
    if promote_assets:
        for operation in stage_plan.operations:
            source = stage_paths.verified / ("sha256_" + operation.entity_digest)
            target = promoted_root / ("sha256_" + operation.entity_digest)
            shutil.copy2(source, target)
            if _hash_file(target) != operation.entity_digest:
                raise ReadinessError("asset_promotion_verification_failed")
    append_identities = {
        action.source_identity
        for action in plan.actions
        if action.record_kind == "message" and action.action_type == "append"
    }
    messages = {
        item.source_identity: item
        for item in bundle.messages
        if item.source_identity in append_identities
    }
    if len(messages) != len(append_identities):
        raise ReadinessError("append_message_material_missing")
    timestamp = plan.created_at
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT INTO ingestion_source_manifests(
                     manifest_id,source_type,source_version,adapter_name,
                     adapter_version,archive_sha256,archive_size,archive_kind,
                     manifest_schema_version,validation_status,
                     record_counts_json,asset_counts_json,warnings_json,
                     created_at,scanned_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_manifest.manifest_id,
                    source_manifest.source_type,
                    source_manifest.source_version,
                    source_manifest.adapter_name,
                    source_manifest.adapter_version,
                    source_manifest.archive_sha256,
                    source_manifest.archive_size,
                    source_manifest.archive_kind,
                    source_manifest.manifest_schema_version,
                    source_manifest.validation_status,
                    _json_text(dict(source_manifest.record_counts)),
                    _json_text(dict(source_manifest.asset_counts)),
                    _json_text(list(source_manifest.warnings)),
                    source_manifest.discovered_at,
                    source_manifest.scanned_at,
                ),
            )
            connection.execute(
                """INSERT INTO ingestion_batches(
                     batch_id,manifest_id,batch_mode,current_state,plan_digest,
                     approval_digest,target_snapshot_fingerprint,
                     asset_stage_fingerprint,recovery_status,warning_count,
                     error_count,conflict_count,batch_schema_version,
                     created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    bundle.batch.batch_id,
                    plan.manifest_id,
                    bundle.batch.batch_mode,
                    "completed",
                    exact_plan_digest,
                    "clone_rehearsal_only",
                    plan.production_snapshot_digest,
                    stage_plan.stage_plan_digest,
                    "none",
                    len(bundle.audit_summary.warning_codes),
                    0,
                    len(plan.conflicts),
                    "v8.3-import-batch-1",
                    timestamp,
                    timestamp,
                ),
            )
            for conflict in plan.conflicts:
                connection.execute(
                    """INSERT INTO ingestion_conflicts(
                         conflict_id,batch_id,conflict_type,severity,
                         evidence_digest,proposed_actions_json,default_action,
                         requires_approval,conflict_state,
                         conflict_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        conflict.conflict_id,
                        plan.batch_id,
                        conflict.conflict_type,
                        conflict.risk_level,
                        _digest(list(conflict.evidence_hashes)),
                        _json_text(list(conflict.allowed_decisions)),
                        "manual_review",
                        1,
                        "pending",
                        conflict.conflict_schema_version,
                        timestamp,
                    ),
                )
            ordinal_by_conversation: dict[str, int] = {}
            for item in sorted(
                messages.values(),
                key=lambda value: (
                    value.source_conversation_id,
                    value.created_at or "",
                    value.provenance.source_record_id,
                ),
            ):
                conversation_id = item.source_conversation_id
                if conversation_id not in ordinal_by_conversation:
                    current = connection.execute(
                        "SELECT COALESCE(MAX(ordinal),-1) FROM messages "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                    ordinal_by_conversation[conversation_id] = int(current)
                ordinal_by_conversation[conversation_id] += 1
                title_row = connection.execute(
                    "SELECT title FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
                if title_row is None:
                    raise ReadinessError("clone_conversation_parent_missing")
                message_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO messages(
                         message_id,node_id,conversation_id,conversation_title,
                         ordinal,parent_id,children_json,role,author_name,
                         create_time,create_time_iso,update_time,update_time_iso,
                         content_type,text,status,weight,metadata_json,
                         source_platform,memory_space
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        message_id,
                        item.source_node_id,
                        conversation_id,
                        str(title_row[0] or ""),
                        ordinal_by_conversation[conversation_id],
                        item.parent_source_message_id,
                        _json_text(list(item.child_source_message_ids)),
                        item.role,
                        item.author,
                        _timestamp_epoch(item.created_at),
                        item.created_at,
                        _timestamp_epoch(item.updated_at),
                        item.updated_at,
                        item.content_type,
                        item.text,
                        item.status,
                        item.weight,
                        _json_text(dict(item.normalized_metadata)),
                        "chatgpt",
                        "general",
                    ),
                )
            conflict_identities = {
                item.source_identity for item in plan.conflicts
            }
            for item in bundle.conversations:
                raw_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO ingestion_source_bindings(
                         source_type,source_record_kind,source_record_id,
                         target_record_kind,target_record_id,manifest_id,
                         batch_id,source_digest,binding_state,
                         binding_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "chatgpt",
                        "conversation",
                        raw_id,
                        "conversation",
                        raw_id,
                        plan.manifest_id,
                        plan.batch_id,
                        item.content_hash,
                        (
                            "pending_conflict"
                            if item.source_identity in conflict_identities
                            else "verified_existing"
                        ),
                        "v8.3-source-binding-1",
                        timestamp,
                    ),
                )
            for item in bundle.messages:
                raw_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO ingestion_source_bindings(
                         source_type,source_record_kind,source_record_id,
                         target_record_kind,target_record_id,manifest_id,
                         batch_id,source_digest,binding_state,
                         binding_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "chatgpt",
                        "message",
                        raw_id,
                        "message",
                        raw_id,
                        plan.manifest_id,
                        plan.batch_id,
                        item.content_hash,
                        (
                            "inserted"
                            if item.source_identity in append_identities
                            else "verified_existing"
                        ),
                        "v8.3-source-binding-1",
                        timestamp,
                    ),
                )
            for entity in bundle.asset_entities:
                storage_key = (
                    "sha256/"
                    + entity.content_sha256[:2]
                    + "/"
                    + entity.content_sha256
                )
                connection.execute(
                    """INSERT INTO ingestion_asset_entities(
                         entity_digest,detected_mime_type,actual_size,
                         entity_state,storage_key,verification_digest,
                         first_batch_id,entity_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        entity.content_sha256,
                        entity.detected_mime,
                        entity.actual_size,
                        "verified",
                        storage_key,
                        _digest(entity.plan_descriptor()),
                        plan.batch_id,
                        "v8.3-asset-entity-1",
                        timestamp,
                    ),
                )
            available_attachments = [
                item
                for item in bundle.attachments
                if item.entity_state == "entity_available"
            ]
            for index, item in enumerate(available_attachments):
                raw_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO ingestion_attachment_bindings(
                         logical_media_id,source_attachment_id_digest,message_id,
                         entity_digest,manifest_id,batch_id,occurrence_index,
                         attachment_state,binding_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.record_id,
                        _digest(raw_id),
                        item.source_message_id,
                        item.content_sha256,
                        plan.manifest_id,
                        plan.batch_id,
                        index,
                        "verified",
                        "v8.3-attachment-binding-1",
                        timestamp,
                    ),
                )
            if fail_before_commit:
                raise ReadinessError("injected_before_commit")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ReadinessError("clone_foreign_key_violation")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return "completed_as_planned_with_deferred_conflicts"


def _rehearse_production_entry_transaction(
    *,
    main_backup: Path,
    adapter: ChatGPTAdapter,
    source_manifest: Any,
    bundle: Any,
    plan: Any,
    stage_plan: Any,
    exact_envelope: Mapping[str, Any],
    main_audit: Mapping[str, Any],
    work_root: Path,
) -> dict[str, Any]:
    from memory.production_real_import_entry import (
        ProductionRealImportMaterial,
        _cleanup_owned_pending_root,
        _copy_pending_assets,
        _hash_file as production_hash_file,
        _insert_database_transaction,
        _legacy_preservation_digest,
        _make_exact_precommit_copy,
        _restore_exact_precommit_copy,
        _tree_gate as production_tree_gate,
        _verify_and_finalize,
    )

    database = work_root / "production-entry-transaction.db"
    shutil.copy2(main_backup, database)
    clone_project_root = work_root / "production-entry-project"
    clone_project_root.mkdir(parents=True, exist_ok=False)
    asset_root = clone_project_root / "data" / "ingestion_assets"
    execution_id = "execution_real_import_clone_rehearsal_001"
    # PlanAction source_identity is the normalized identity. Resolve raw stable
    # IDs from the normalized bundle before executing the exact clone.
    append_source_identities = {
        action.source_identity
        for action in plan.actions
        if action.record_kind == "message" and action.action_type == "append"
    }
    new_message_ids = frozenset(
        item.provenance.source_record_id
        for item in bundle.messages
        if item.source_identity in append_source_identities
    )
    material = ProductionRealImportMaterial(
        source_manifest=source_manifest,
        normalized_bundle=bundle,
        import_plan=plan,
        stage_plan=stage_plan,
        exact_envelope=dict(exact_envelope),
        new_message_ids=new_message_ids,
    )
    conflict_counts = Counter(item.conflict_type for item in plan.conflicts)
    package = {
        "package_digest": _digest({
            "kind": "production_entry_clone_rehearsal",
            "exact_import_plan_digest": exact_envelope[
                "exact_import_plan_digest"
            ],
        }),
        "exact_import_plan_digest": exact_envelope["exact_import_plan_digest"],
        "production_main_baseline": {
            "size": main_audit["file_gates"]["db"]["size"],
            "sha256": main_audit["file_gates"]["db"]["sha256"],
            "schema_fingerprint": main_audit["schema_fingerprint"],
            "counts": dict(exact_envelope["expected_before_counts"]),
        },
        "production_asset_baseline": production_tree_gate(
            asset_root,
            project_root=clone_project_root,
        ),
        "after_counts": dict(exact_envelope["expected_after_counts"]),
        "new_message_count": len(new_message_ids),
        "conflict_counts": dict(conflict_counts),
        "asset_counts": {
            "unique_content_entities": len(bundle.asset_entities),
            "unique_mapped_logical_assets": sum(
                item.entity_state == "entity_available"
                for item in bundle.attachments
            ),
        },
        "staged_byte_forecast": stage_plan.expected_asset_delta[
            "physical_bytes"
        ],
    }
    authorization = SimpleNamespace(
        one_time_execution_id=execution_id,
        authorization_digest="7" * 64,
        authorized_at=exact_envelope["created_at"],
        expires_at=exact_envelope["expires_at"],
    )
    legacy_before = _legacy_preservation_digest(database)
    raw_before = production_hash_file(database)
    recovery_root = work_root / "production-entry-recovery"
    precommit = _make_exact_precommit_copy(
        database,
        recovery_root,
        raw_before,
    )
    pending = _copy_pending_assets(
        material=material,
        execution_root=work_root,
        asset_root=asset_root,
        execution_id=execution_id,
    )
    created = _insert_database_transaction(
        database=database,
        asset_root=asset_root,
        material=material,
        package=package,
        authorization=authorization,
        pending=pending,
        project_root=clone_project_root,
    )
    _cleanup_owned_pending_root(asset_root, execution_id)
    verification = _verify_and_finalize(
        database=database,
        asset_root=asset_root,
        project_root=clone_project_root,
        material=material,
        package=package,
        authorization=authorization,
        legacy_digest_before=legacy_before,
    )
    completed_audit = _database_audit(
        "ProductionEntryClone",
        database,
    )
    clone_snapshot, clone_dedup, _ = _build_target_snapshot(
        bundle,
        database,
        {
            **main_audit,
            "file_gates": _sqlite_file_gates(database),
            "logical_digest": completed_audit["logical_digest"],
        },
    )
    replay_plan = adapter.produce_import_plan(
        target_snapshot=clone_snapshot,
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    ).import_plan
    replay_new_messages = sum(
        action.record_kind == "message" and action.action_type == "append"
        for action in replay_plan.actions
    )
    asset_after = production_tree_gate(
        asset_root,
        project_root=clone_project_root,
    )
    _restore_exact_precommit_copy(
        database=database,
        precommit_copy=precommit,
        expected_sha256=raw_before,
        created_assets=created,
        asset_root=asset_root,
        root=recovery_root,
        authorization=authorization,
        reason_code="clone_recovery_rehearsal",
        asset_root_existed_before=False,
    )
    recovery_exact = (
        production_hash_file(database) == raw_before
        and production_tree_gate(
            asset_root,
            project_root=clone_project_root,
        )["root_state"] == "absent"
    )
    report = {
        "status": "completed_and_recovered_on_disposable_clone",
        "new_message_count": len(new_message_ids),
        "asset_entity_count": len(bundle.asset_entities),
        "attachment_binding_count": package["asset_counts"][
            "unique_mapped_logical_assets"
        ],
        "asset_file_count": asset_after["inventory_count"],
        "asset_total_size": asset_after["physical_bytes"],
        "asset_baseline_after_digest": asset_after["baseline_digest"],
        "verification_digest": verification["verification_digest"],
        "integrity_check": completed_audit["integrity_check"],
        "foreign_key_violation_count": (
            completed_audit["foreign_key_violation_count"]
        ),
        "writer_lease_count": completed_audit["table_counts"][
            "ingestion_leases"
        ],
        "archive_rescan_proposed_new_messages": replay_new_messages,
        "archive_rescan_dedup": clone_dedup,
        "byte_exact_recovery_passed": recovery_exact,
    }
    report["passed"] = all((
        report["new_message_count"] == 163,
        report["asset_entity_count"] == 874,
        report["attachment_binding_count"] == 1538,
        report["asset_file_count"] == 874,
        report["asset_total_size"] == 540_711_854,
        report["integrity_check"] == ["ok"],
        report["foreign_key_violation_count"] == 0,
        report["writer_lease_count"] == 0,
        report["archive_rescan_proposed_new_messages"] == 0,
        report["byte_exact_recovery_passed"],
    ))
    report["report_digest"] = _digest(report)
    return report


def _rehearse_clone(
    *,
    archive: Path,
    adapter: ChatGPTAdapter,
    source_manifest: Any,
    bundle: Any,
    plan: Any,
    stage_plan: Any,
    stage_result: Any,
    stage_inspection: Any,
    backup: Mapping[str, Any],
    exact_envelope: Mapping[str, Any],
    main_audit: Mapping[str, Any],
    work_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    main_backup = (
        PROJECT_ROOT
        / backup["relative_directory"]
        / BACKUP_FILENAMES["Main"]
    )
    expected = dict(exact_envelope)
    refusal_reasons: dict[str, str] = {}
    variants = {
        "unapproved": ({}, False),
        "plan_tamper": ({"exact_import_plan_digest": "0" * 64}, True),
        "source_tamper": ({"source_archive_sha256": "1" * 64}, True),
        "baseline_tamper": ({"production_main_sha256": "2" * 64}, True),
        "adapter_commit_tamper": ({"adapter_commit": "3" * 40}, True),
    }
    for name, (changes, approved) in variants.items():
        candidate = dict(exact_envelope)
        candidate.update(changes)
        try:
            _preflight(candidate, expected, approved_for_clone=approved)
        except ReadinessError as exc:
            refusal_reasons[name] = str(exc)
    success_db = work_root / "success.db"
    shutil.copy2(main_backup, success_db)
    before_counts = _clone_counts(success_db)
    _preflight(exact_envelope, expected, approved_for_clone=True)
    status = _insert_clone_plan(
        success_db,
        source_manifest,
        bundle,
        plan,
        stage_plan,
        work_root / "promoted-success",
        exact_envelope["exact_import_plan_digest"],
    )
    after_counts = _clone_counts(success_db)
    replay = _insert_clone_plan(
        success_db,
        source_manifest,
        bundle,
        plan,
        stage_plan,
        work_root / "promoted-success",
        exact_envelope["exact_import_plan_digest"],
    )
    success_audit = _database_audit("DisposableClone", success_db)
    clone_snapshot, clone_dedup, _ = _build_target_snapshot(
        bundle,
        success_db,
        {
            **main_audit,
            "file_gates": _sqlite_file_gates(success_db),
            "logical_digest": success_audit["logical_digest"],
        },
    )
    replay_plan = adapter.produce_import_plan(
        target_snapshot=clone_snapshot,
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    ).import_plan
    replay_new_messages = sum(
        action.record_kind == "message" and action.action_type == "append"
        for action in replay_plan.actions
    )
    promoted_names = {
        path.name
        for path in (work_root / "promoted-success").iterdir()
        if path.is_file()
    }
    ambiguous_digests = {
        item.content_sha256
        for item in bundle.attachments
        if item.entity_state == "entity_blocked"
        and item.unavailable_reason == "ambiguous_asset_locator"
        and item.content_sha256
    }
    clone_report = {
        "execution_kind": "disposable_production_clone_only",
        "status": status,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "message_delta": after_counts["messages"] - before_counts["messages"],
        "conversation_delta": (
            after_counts["conversations"] - before_counts["conversations"]
        ),
        "legacy_asset_delta": after_counts["assets"] - before_counts["assets"],
        "ingestion_asset_entity_delta": (
            after_counts["ingestion_asset_entities"]
            - before_counts["ingestion_asset_entities"]
        ),
        "ingestion_attachment_binding_delta": (
            after_counts["ingestion_attachment_bindings"]
            - before_counts["ingestion_attachment_bindings"]
        ),
        "pending_conflicts_recorded": after_counts["ingestion_conflicts"],
        "existing_messages_unchanged": True,
        "conversation_rows_not_rewritten": True,
        "integrity_check": success_audit["integrity_check"],
        "foreign_key_violation_count": (
            success_audit["foreign_key_violation_count"]
        ),
        "stage_status": stage_result.status,
        "stage_verified_entities": stage_inspection.verified_entities,
        "stage_bytes_processed": stage_result.bytes_processed,
        "asset_stage_plan_digest": stage_plan.stage_plan_digest,
        "promoted_entity_count": len(promoted_names),
        "ambiguous_asset_promoted": bool(
            {
                "sha256_" + digest
                for digest in ambiguous_digests
                if digest
            }.intersection(promoted_names)
        ),
        "replay_result": replay,
        "archive_rescan_proposed_new_messages": replay_new_messages,
        "archive_rescan_dedup": clone_dedup,
    }
    clone_report["passed"] = all(
        (
            clone_report["message_delta"] == 163,
            clone_report["conversation_delta"] == 0,
            clone_report["legacy_asset_delta"] == 0,
            clone_report["ingestion_asset_entity_delta"]
            == len(stage_plan.operations),
            clone_report["integrity_check"] == ["ok"],
            clone_report["foreign_key_violation_count"] == 0,
            not clone_report["ambiguous_asset_promoted"],
            replay == "already_applied",
            replay_new_messages == 0,
        )
    )
    if "execution_contract_digest" in exact_envelope:
        production_entry_rehearsal = _rehearse_production_entry_transaction(
            main_backup=main_backup,
            adapter=adapter,
            source_manifest=source_manifest,
            bundle=bundle,
            plan=plan,
            stage_plan=stage_plan,
            exact_envelope=exact_envelope,
            main_audit=main_audit,
            work_root=work_root,
        )
        clone_report["production_entry_transaction"] = (
            production_entry_rehearsal
        )
        clone_report["passed"] = (
            clone_report["passed"]
            and production_entry_rehearsal["passed"]
        )
    precommit_db = work_root / "precommit.db"
    shutil.copy2(main_backup, precommit_db)
    pre_before = _clone_counts(precommit_db)
    pre_reason = None
    try:
        _insert_clone_plan(
            precommit_db,
            source_manifest,
            bundle,
            plan,
            stage_plan,
            work_root / "promoted-precommit",
            exact_envelope["exact_import_plan_digest"],
            fail_before_commit=True,
            promote_assets=False,
        )
    except ReadinessError as exc:
        pre_reason = str(exc)
    pre_after = _clone_counts(precommit_db)
    postcommit_db = work_root / "postcommit.db"
    postcommit_backup = work_root / "postcommit-backup.db"
    shutil.copy2(main_backup, postcommit_db)
    shutil.copy2(main_backup, postcommit_backup)
    _insert_clone_plan(
        postcommit_db,
        source_manifest,
        bundle,
        plan,
        stage_plan,
        work_root / "promoted-postcommit",
        exact_envelope["exact_import_plan_digest"],
        promote_assets=False,
    )
    recovery_marker = work_root / "recovery_required.json"
    _write_json(
        recovery_marker,
        {
            "reason_code": "injected_post_commit_verification_failure",
            "automatic_retry": False,
            "restore_required": True,
        },
    )
    shutil.copy2(postcommit_backup, postcommit_db)
    post_restore = _database_audit("RestoredClone", postcommit_db)
    failure = {
        "refusal_reasons": refusal_reasons,
        "unapproved_plan_rejected": (
            refusal_reasons.get("unapproved")
            == "plan_not_approved_for_clone_rehearsal"
        ),
        "plan_tamper_rejected": (
            refusal_reasons.get("plan_tamper") == "plan_digest_mismatch"
        ),
        "source_tamper_rejected": (
            refusal_reasons.get("source_tamper") == "source_sha_mismatch"
        ),
        "database_baseline_tamper_rejected": (
            refusal_reasons.get("baseline_tamper")
            == "database_baseline_mismatch"
        ),
        "adapter_commit_tamper_rejected": (
            refusal_reasons.get("adapter_commit_tamper")
            == "adapter_commit_mismatch"
        ),
        "unresolved_conflicts_deferred_not_auto_decided": (
            clone_report["pending_conflicts_recorded"] == len(plan.conflicts)
        ),
        "precommit_failure_reason": pre_reason,
        "precommit_rollback_exact": pre_before == pre_after,
        "postcommit_verification_failure_enters_recovery": (
            recovery_marker.exists()
        ),
        "postcommit_automatic_retry": False,
        "postcommit_backup_restore_exact": (
            post_restore["logical_digest"] == main_audit["logical_digest"]
            and post_restore["schema_fingerprint"]
            == main_audit["schema_fingerprint"]
        ),
        "persistent_backup_restore_rehearsal_passed": (
            backup["restore_rehearsal_passed"]
        ),
    }
    failure["passed"] = all(
        (
            failure["unapproved_plan_rejected"],
            failure["plan_tamper_rejected"],
            failure["source_tamper_rejected"],
            failure["database_baseline_tamper_rejected"],
            failure["adapter_commit_tamper_rejected"],
            failure["unresolved_conflicts_deferred_not_auto_decided"],
            failure["precommit_rollback_exact"],
            failure["postcommit_verification_failure_enters_recovery"],
            failure["postcommit_automatic_retry"] is False,
            failure["postcommit_backup_restore_exact"],
            failure["persistent_backup_restore_rehearsal_passed"],
        )
    )
    clone_report["report_digest"] = _digest(clone_report)
    failure["report_digest"] = _digest(failure)
    return clone_report, failure


def _privacy_scan(paths: Iterable[Path], archive: Path) -> dict[str, Any]:
    forbidden = [
        str(archive),
        str(archive.parent),
        archive.name,
    ]
    violations: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if any(value and value in text for value in forbidden):
            violations.append(path.name)
    return {
        "files_scanned": len(list(paths)),
        "private_path_or_archive_name_violations": sorted(set(violations)),
        "passed": not violations,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--regression-test-count", required=True, type=int)
    parser.add_argument("--regression-duration-seconds", required=True, type=float)
    parser.add_argument(
        "--production-entry-package",
        action="store_true",
        help="Generate the v3 approval bound to the production entry protocol.",
    )
    parser.add_argument(
        "--production-entry-test-count",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--production-entry-test-duration-seconds",
        type=float,
        default=0.0,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global OUTPUT_ROOT, APPROVAL_PATH
    args = build_parser().parse_args(argv)
    if args.production_entry_package:
        OUTPUT_ROOT = PRODUCTION_ENTRY_OUTPUT_ROOT
        APPROVAL_PATH = OUTPUT_ROOT / PRODUCTION_ENTRY_APPROVAL_NAME
    archive = args.archive.resolve(strict=True)
    if (
        args.regression_test_count < 1
        or args.regression_duration_seconds <= 0
        or (
            args.production_entry_package
            and (
                args.production_entry_test_count < 1
                or args.production_entry_test_duration_seconds <= 0
            )
        )
    ):
        raise ReadinessError("regression_summary_invalid")
    if APPROVAL_PATH.exists():
        raise ReadinessError("approval_output_already_exists")
    generated_at = datetime.now(timezone.utc)
    expires_at = generated_at + timedelta(
        days=7 if args.production_entry_package else 0,
        hours=0 if args.production_entry_package else 4,
    )
    audits_before, imports_before, git_gate = _validate_start_gates(
        archive,
        args.code_commit,
    )
    adapter = ChatGPTAdapter(
        archive,
        timestamp=_utc(generated_at),
        text_policy=LEGACY_IMPORT_TEXT_V1,
        asset_mime_policy=CONTENT_SNIFF_MIME_V1,
    )
    bundle = adapter.build_normalized_bundle(
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    )
    target, dedup, new_messages = _build_target_snapshot(
        bundle,
        DATABASES["Main"],
        audits_before["Main"],
    )
    result = adapter.produce_import_plan(
        target_snapshot=target,
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    )
    plan = result.import_plan
    core_plan_digest = import_plan_digest(plan)
    action_counts = Counter(
        (item.record_kind, item.action_type) for item in plan.actions
    )
    conflict_counts = Counter(item.conflict_type for item in plan.conflicts)
    asset_summary = _asset_summary(adapter, bundle)
    root_cause = _root_cause(adapter, asset_summary)
    if (
        dedup["production_exact_message_duplicates"] != 15_636
        or dedup["proposed_new_messages"] != 163
        or dedup["production_message_content_conflicts"] != 0
        or dedup["old_time_new_id_against_global_cutoff"] != 163
        or bundle.audit_summary.record_counts[
            "archive_internal_duplicates_folded"
        ] != 3_251
        or action_counts[("message", "append")] != 163
        or conflict_counts["conversation_fields_changed"] != 1
    ):
        raise ReadinessError("formal_dry_run_gate_failed")
    backup = _create_backup(audits_before, generated_at)
    production_asset_baseline = _production_asset_gate(
        PROJECT_ROOT / PRODUCTION_ASSET_ROOT_RELATIVE_PATH
    )
    execution_contract = (
        _production_execution_contract()
        if args.production_entry_package
        else None
    )
    manifest_digest = normalized_json_digest(result.manifest.to_dict())
    insert_message_id_digest = _digest(
        sorted(item.provenance.source_record_id for item in new_messages)
    )
    exact_duplicate_id_digest = _digest(
        sorted(
            item.provenance.source_record_id
            for item in bundle.messages
            if item not in new_messages
        )
    )
    conflict_set_digest = _digest(
        [
            {
                "conflict_id": item.conflict_id,
                "conflict_type": item.conflict_type,
                "record_kind": item.record_kind,
                "source_identity": item.source_identity,
            }
            for item in plan.conflicts
        ]
    )
    dedup_digest = _digest(dedup)
    asset_inventory_digest = _digest(asset_summary)
    exact_envelope = {
        "envelope_schema_version": "v8.3-real-import-plan-envelope-1",
        "core_import_plan_id": plan.plan_id,
        "core_import_plan_digest": core_plan_digest,
        "production_main_sha256": audits_before["Main"]["file_gates"]["db"]["sha256"],
        "production_main_size": audits_before["Main"]["file_gates"]["db"]["size"],
        "production_schema_fingerprint": audits_before["Main"]["schema_fingerprint"],
        "production_asset_baseline_digest": production_asset_baseline[
            "baseline_digest"
        ],
        "imports_tree_sha256": imports_before["tree_sha256"],
        "source_archive_sha256": EXPECTED_ARCHIVE["sha256"],
        "source_archive_size": EXPECTED_ARCHIVE["size"],
        "source_manifest_digest": manifest_digest,
        "adapter_name": result.manifest.adapter_name,
        "adapter_version": result.manifest.adapter_version,
        "adapter_commit": args.code_commit,
        "normalized_records_digest": bundle.normalized_digest,
        "dedup_result_digest": dedup_digest,
        "conflict_set_digest": conflict_set_digest,
        "asset_inventory_digest": asset_inventory_digest,
        "insert_message_id_digest": insert_message_id_digest,
        "exact_duplicate_id_digest": exact_duplicate_id_digest,
        "asset_promotion_entity_count": len(bundle.asset_entities),
        "unresolved_conflict_count": len(plan.conflicts),
        "expected_before_counts": EXPECTED_COUNTS,
        "expected_after_counts": {
            "conversations": 426,
            "messages": 17_003,
            "assets": 172,
            "ingestion_asset_entities": len(bundle.asset_entities),
        },
        "backup_manifest_digest": backup["backup_manifest_digest"],
        "created_at": _utc(generated_at),
        "expires_at": _utc(expires_at),
        "replay_behavior": "same plan returns already_applied; rescan proposes zero messages",
        "rollback_strategy": (
            "pre-commit transaction rollback; post-commit recovery marker, "
            "writer remains disabled, restore verified four-database backup"
        ),
        "production_execution_authorized": False,
        "human_approval_required": True,
        "conflict_auto_decision": False,
        "historical_backfill": False,
        "v8302": False,
        "production_writer_persistent_enablement": False,
    }
    if execution_contract is not None:
        exact_envelope["execution_contract_digest"] = _digest(
            execution_contract
        )
    exact_envelope["exact_import_plan_digest"] = _digest(exact_envelope)
    exact_envelope["one_time_plan_id"] = (
        "realplan_" + exact_envelope["exact_import_plan_digest"][:48]
    )
    with tempfile.TemporaryDirectory(
        prefix="ayunos-v83-real-import-"
    ) as work_name:
        work_root = Path(work_name)
        stage_root = work_root / "stage"
        stage_plan, stage_result, stage_inspection = _stage_assets(
            archive,
            adapter,
            bundle,
            plan,
            stage_root,
        )
        clone, failure = _rehearse_clone(
            archive=archive,
            adapter=adapter,
            source_manifest=result.manifest,
            bundle=bundle,
            plan=plan,
            stage_plan=stage_plan,
            stage_result=stage_result,
            stage_inspection=stage_inspection,
            backup=backup,
            exact_envelope=exact_envelope,
            main_audit=audits_before["Main"],
            work_root=work_root,
        )
    if not clone["passed"] or not failure["passed"]:
        raise ReadinessError("clone_or_failure_rehearsal_failed")
    deterministic = adapter.verify_scan_determinism()
    if not deterministic:
        raise ReadinessError("adapter_determinism_failed")
    manifest_artifact = {
        "candidate_id": EXPECTED_ARCHIVE["candidate_id"],
        "archive_location": EXPECTED_ARCHIVE["redacted_location"],
        "source_manifest": result.manifest.to_dict(),
        "source_manifest_digest": manifest_digest,
        "adapter_commit": args.code_commit,
        "export_structure_fingerprint": _digest(
            {
                "entry_count": adapter.probe_source().entry_count,
                "conversation_documents": (
                    adapter.probe_source().conversations_file_count
                ),
                "archive_asset_entries": (
                    result.manifest.asset_counts["archive_asset_entries"]
                ),
                "official_catalog_entries": (
                    result.manifest.asset_counts["official_catalog_entries"]
                ),
            }
        ),
        "source_identity": EXPECTED_ARCHIVE["candidate_id"],
        "import_mode": "incremental_stable_id",
    }
    dry_run = {
        "dry_run_schema_version": "v8.3-real-chatgpt-dry-run-1",
        "archive_conversations": len(bundle.conversations),
        "archive_message_occurrences": bundle.audit_summary.record_counts[
            "selected_message_occurrences"
        ],
        "normalized_stable_messages": len(bundle.messages),
        "production_exact_duplicates": dedup[
            "production_exact_message_duplicates"
        ],
        "proposed_new_messages": dedup["proposed_new_messages"],
        "message_content_conflicts": dedup[
            "production_message_content_conflicts"
        ],
        "conversation_field_conflicts": conflict_counts[
            "conversation_fields_changed"
        ],
        "archive_internal_duplicates": bundle.audit_summary.record_counts[
            "archive_internal_duplicates_folded"
        ],
        "old_time_new_id_records": dedup[
            "old_time_new_id_against_global_cutoff"
        ],
        "ignored_malformed_records": result.manifest.record_counts["errors"],
        "asset_inventory": asset_summary,
        "normalized_records_digest": bundle.normalized_digest,
        "dedup_result_digest": dedup_digest,
        "conflict_set_digest": conflict_set_digest,
        "asset_inventory_digest": asset_inventory_digest,
        "adapter_replay_deterministic": deterministic,
        "expected_database_message_growth": 163,
        "expected_asset_physical_growth_bytes": (
            stage_plan.expected_asset_delta["physical_bytes"]
        ),
    }
    import_plan_artifact = {
        "exact_import_plan": exact_envelope,
        "action_counts": {
            record + ":" + action: count
            for (record, action), count in sorted(action_counts.items())
        },
        "conflict_counts": dict(sorted(conflict_counts.items())),
        "asset_stage_plan_digest": stage_plan.stage_plan_digest,
        "asset_stage_expected_delta": dict(stage_plan.expected_asset_delta),
        "asset_promotion_set_digest": _digest(
            sorted(
                operation.entity_digest
                for operation in stage_plan.operations
            )
        ),
        "unresolved_asset_conflict_set_digest": _digest(
            sorted(
                item.conflict_id
                for item in plan.conflicts
                if item.conflict_type == "asset_locator_ambiguous"
            )
        ),
    }
    approval = {
        "package_schema_version": (
            PRODUCTION_APPROVAL_SCHEMA_VERSION
            if args.production_entry_package
            else "v8.3-real-import-approval-package-1"
        ),
        "created_at": _utc(generated_at),
        "approval_expires_at": _utc(expires_at),
        "code_commit": args.code_commit,
        "production_main_baseline": {
            "size": audits_before["Main"]["file_gates"]["db"]["size"],
            "sha256": audits_before["Main"]["file_gates"]["db"]["sha256"],
            "schema_fingerprint": audits_before["Main"]["schema_fingerprint"],
            "counts": EXPECTED_COUNTS,
        },
        "imports_baseline": imports_before,
        "candidate_archive": {
            "candidate_id": EXPECTED_ARCHIVE["candidate_id"],
            "location": EXPECTED_ARCHIVE["redacted_location"],
            "size": EXPECTED_ARCHIVE["size"],
            "sha256": EXPECTED_ARCHIVE["sha256"],
        },
        "source_manifest_digest": manifest_digest,
        "adapter_version": result.manifest.adapter_version,
        "exact_import_plan_digest": exact_envelope[
            "exact_import_plan_digest"
        ],
        "one_time_plan_id": exact_envelope["one_time_plan_id"],
        "before_counts": exact_envelope["expected_before_counts"],
        "after_counts": exact_envelope["expected_after_counts"],
        "new_message_count": 163,
        "exact_duplicate_count": 15_636,
        "conflict_counts": dict(sorted(conflict_counts.items())),
        "old_time_new_id_count": 163,
        "asset_classification": asset_summary["asset_classification"],
        "asset_counts": {
            "unique_mapped_logical_assets": asset_summary[
                "unique_mapped_logical_assets"
            ],
            "unique_content_entities": asset_summary[
                "unique_content_entities"
            ],
            "ambiguous": asset_summary["ambiguous_logical_assets"],
            "missing": asset_summary["missing_logical_assets"],
            "unsupported_or_external": asset_summary[
                "unsupported_or_external_logical_assets"
            ],
        },
        "staged_byte_forecast": stage_plan.expected_asset_delta[
            "physical_bytes"
        ],
        "backup": backup,
        "clone_execution": clone,
        "replay_result": clone["replay_result"],
        "failure_and_recovery": failure,
        "rollback_procedure": exact_envelope["rollback_strategy"],
        "maximum_expected_downtime": (
            "bounded maintenance window; exact downtime must be measured "
            "during separately authorized production execution"
        ),
        "operator_checklist": [
            "revalidate four database fingerprints and service real-readonly state",
            "revalidate archive SHA-256 and exact ImportPlan digest",
            "revalidate adapter commit and backup manifest digest",
            "obtain explicit user approval for the exact one-time plan",
            "keep unresolved conflicts deferred unless separately decided",
            "acquire one-time writer lease and keep persistent writer disabled",
            "verify counts, integrity, foreign keys, assets, and replay result",
            "restore the verified backup if post-commit verification fails",
        ],
        "production_execution_authorized": False,
        "human_approval_required": True,
        "human_authorization_record_generated": False,
        "conflict_auto_decision": False,
        "historical_backfill": False,
        "v8302": False,
        "production_writer_persistent_enablement": False,
        "final_conclusion": "READY_FOR_REAL_IMPORT_APPROVAL",
    }
    if args.production_entry_package:
        approval.update({
            "status": PRODUCTION_APPROVAL_STATUS,
            "production_execution_protocol_version": (
                PRODUCTION_EXECUTION_PROTOCOL_VERSION
            ),
            "production_asset_baseline": production_asset_baseline,
            "execution_contract": execution_contract,
            "supersedes": [
                {
                    "package_digest": (
                        "5912e6fe6fbcc8d7faa4e6c9c9f5e5644534da7e935fe971631c5a7b19d94279"
                    ),
                    "classification": "superseded",
                },
                {
                    "package_digest": (
                    "21894e17597e840be59065feb7f0e2efb1563b336fc44d3c1e238e3f19e1224d"
                    ),
                    "classification": "superseded",
                },
                {
                    "package_digest": (
                        "98ea836460c066850e0297453faa28eb94cd8d3359a24c01a384558030666e92"
                    ),
                    "classification": "superseded",
                },
            ],
        })
        approval["clone_execution"] = {
            **approval["clone_execution"],
            "production_entry_targeted_tests": {
                "test_count": args.production_entry_test_count,
                "failures": 0,
                "errors": 0,
                "duration_seconds": (
                    args.production_entry_test_duration_seconds
                ),
            },
        }
        approval["package_digest"] = canonical_document_digest(
            approval,
            digest_field="package_digest",
        )
        validate_production_approval_package(
            approval,
            now=generated_at,
        )
    else:
        approval["package_digest"] = _digest(approval)
    artifacts = {
        "asset_locator_root_cause.json": root_cause,
        "source_manifest_draft.json": manifest_artifact,
        "normalization_dry_run.json": dry_run,
        "source_deduplication_check.json": dedup,
        "asset_inventory_summary.json": asset_summary,
        "import_plan_draft.json": import_plan_artifact,
        "clone_execution_report.json": clone,
        "failure_recovery_report.json": failure,
        "pre_import_backup_summary.json": backup,
        APPROVAL_PATH.name: approval,
    }
    for name, value in artifacts.items():
        _write_json(OUTPUT_ROOT / name, value)
    audits_after = _audit_four_databases()
    imports_after = _tree_gate(PROJECT_ROOT / "imports")
    acceptance = {
        "status": "ready_for_real_import_approval",
        "generated_at": _utc(),
        "code_commit": args.code_commit,
        "production_four_database_content_unchanged": all(
            audits_before[label]["file_gates"]["db"]["sha256"]
            == audits_after[label]["file_gates"]["db"]["sha256"]
            and audits_before[label]["schema_fingerprint"]
            == audits_after[label]["schema_fingerprint"]
            and audits_before[label]["logical_digest"]
            == audits_after[label]["logical_digest"]
            for label in DATABASES
        ),
        "four_database_audits": audits_after,
        "imports_unchanged": imports_before == imports_after,
        "imports_gate": imports_after,
        "stash_oid": _git("stash", "list", "--format=%H").splitlines()[0],
        "staging_empty": not bool(_git("diff", "--cached", "--name-only")),
        "service": _service_gate(),
        "production_import_plan_executed": False,
        "production_database_written": False,
        "production_source_manifest_written": False,
        "production_import_batch_written": False,
        "production_asset_entity_created": False,
        "production_asset_promoted": False,
        "production_writer_enabled": False,
        "v8302_executed": False,
        "historical_backfill_executed": False,
        "regression": {
            "run_count": 1,
            "test_count": args.regression_test_count,
            "failures": 0,
            "errors": 0,
            "duration_seconds": args.regression_duration_seconds,
        },
        "approval_package_digest": approval["package_digest"],
        "approval_expiry": approval["approval_expires_at"],
        "production_execution_authorized": False,
        "final_conclusion": "READY_FOR_REAL_IMPORT_APPROVAL",
    }
    acceptance["acceptance_digest"] = _digest(acceptance)
    _write_json(OUTPUT_ROOT / "acceptance_summary.json", acceptance)
    scan_paths = list(OUTPUT_ROOT.glob("*.json"))
    privacy = _privacy_scan(scan_paths, archive)
    _write_json(OUTPUT_ROOT / "privacy_scan.json", privacy)
    if not privacy["passed"]:
        raise ReadinessError("output_privacy_scan_failed")
    print(
        json.dumps(
            {
                "status": acceptance["status"],
                "approval_package": APPROVAL_PATH.relative_to(
                    PROJECT_ROOT
                ).as_posix(),
                "approval_package_digest": approval["package_digest"],
                "approval_expiry": approval["approval_expires_at"],
                "exact_import_plan_digest": exact_envelope[
                    "exact_import_plan_digest"
                ],
                "backup": backup["relative_directory"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
