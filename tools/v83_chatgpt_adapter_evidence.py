#!/usr/bin/env python3
"""Generate redacted v8.3-C adapter evidence without production writes."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_adapters.chatgpt import (
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    empty_target_snapshot,
    make_target_snapshot,
)
from memory.ingestion_core import build_database_fingerprint
from memory.ingestion_planning import ExistingRecord
from memory.media_index import build_media_index


DB_NAME = "ayunos" + "_chatgpt" + ".db"
PRODUCTION_DB = PROJECT_ROOT / "data" / DB_NAME
OUTPUT_DIR = PROJECT_ROOT / "output" / "v8.3-c-chatgpt-adapter"
EXPECTED_BASELINE = {
    DB_NAME: {
        "size": 65_982_464,
        "sha256": "739a552423ca3e572e8fb76d64cc22825a3870fafbe28cc467fa534372c76690",
        "mtime_utc": "2026-07-25T18:22:24.752357Z",
    },
    DB_NAME + "-wal": {
        "size": 0,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "mtime_utc": "2026-07-25T18:22:24.818079Z",
    },
    DB_NAME + "-shm": {
        "size": 32_768,
        "sha256": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        "mtime_utc": "2026-07-25T18:25:36.750693Z",
    },
}


def file_gate() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in (
        PRODUCTION_DB,
        Path(str(PRODUCTION_DB) + "-wal"),
        Path(str(PRODUCTION_DB) + "-shm"),
    ):
        if not path.exists():
            result[path.name] = {"exists": False}
            continue
        info = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        expected = EXPECTED_BASELINE.get(path.name)
        mtime_utc = (
            __import__("datetime")
            .datetime.fromtimestamp(
                info.st_mtime,
                __import__("datetime").timezone.utc,
            )
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        result[path.name] = {
            "exists": True,
            "size": info.st_size,
            "mtime_utc": mtime_utc,
            "sha256": digest.hexdigest(),
            "baseline_content_match": bool(
                expected
                and expected["size"] == info.st_size
                and expected["sha256"] == digest.hexdigest()
            ),
            "authority_mtime_match": bool(
                expected and expected["mtime_utc"] == mtime_utc
            ),
        }
    return result


def production_compatibility() -> tuple[dict[str, Any], Any]:
    connection = sqlite3.connect(
        f"file:{PRODUCTION_DB.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        tables = [
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        columns = {
            table: [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in ("conversations", "messages")
        }
        counts = {
            "conversations": connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0],
            "messages": connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0],
            "assets": connection.execute(
                "SELECT COUNT(*) FROM assets"
            ).fetchone()[0],
        }
        source_counts = {
            table: {
                (row["source_platform"] or "legacy_unknown"): row["count"]
                for row in connection.execute(
                    f"""SELECT source_platform, COUNT(*) AS count
                        FROM {table}
                        GROUP BY source_platform
                        ORDER BY source_platform"""
                )
            }
            for table in ("conversations", "messages")
        }
        non_claude_counts = {
            "conversations": connection.execute(
                """SELECT COUNT(*) FROM conversations
                   WHERE COALESCE(source_platform, '') <> 'claude'"""
            ).fetchone()[0],
            "messages": connection.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE COALESCE(source_platform, '') <> 'claude'"""
            ).fetchone()[0],
        }
        id_checks = {
            "conversation_ids_missing": connection.execute(
                """SELECT COUNT(*) FROM conversations
                   WHERE conversation_id IS NULL OR TRIM(conversation_id) = ''"""
            ).fetchone()[0],
            "conversation_id_duplicate_groups": connection.execute(
                """SELECT COUNT(*) FROM (
                     SELECT conversation_id FROM conversations
                     GROUP BY conversation_id HAVING COUNT(*) > 1
                   )"""
            ).fetchone()[0],
            "message_ids_missing": connection.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE message_id IS NULL OR TRIM(message_id) = ''"""
            ).fetchone()[0],
            "message_id_duplicate_groups": connection.execute(
                """SELECT COUNT(*) FROM (
                     SELECT message_id FROM messages
                     WHERE message_id IS NOT NULL AND TRIM(message_id) <> ''
                     GROUP BY message_id HAVING COUNT(*) > 1
                   )"""
            ).fetchone()[0],
            "chatgpt_message_binding_candidates": connection.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE source_platform = 'chatgpt'
                     AND message_id IS NOT NULL AND TRIM(message_id) <> ''"""
            ).fetchone()[0],
        }
        schema_descriptor = {
            "tables": tables,
            "columns": columns,
        }
        logical_descriptor = {
            "counts": counts,
            "source_counts": source_counts,
            "id_checks": id_checks,
        }
    finally:
        connection.close()
    content_sha256 = hashlib.sha256(PRODUCTION_DB.read_bytes()).hexdigest()
    production_fingerprint = build_database_fingerprint(
        content_sha256=content_sha256,
        size_bytes=PRODUCTION_DB.stat().st_size,
        schema_descriptor=schema_descriptor,
        logical_descriptor=logical_descriptor,
    )
    return (
        {
            "counts": counts,
            "non_claude_protected_counts": non_claude_counts,
            "source_counts": source_counts,
            "id_checks": id_checks,
            "schema_has_required_columns": {
                table: all(
                    required in columns[table]
                    for required in (
                        "source_platform",
                        "memory_space",
                        "metadata_json",
                    )
                )
                for table in ("conversations", "messages")
            },
            "production_fingerprint": production_fingerprint.to_dict(),
            "production_import_plan_generated": False,
            "production_operation_executed": False,
            "production_schema_migrated": False,
        },
        production_fingerprint,
    )


def synthetic_conversations() -> list[dict[str, Any]]:
    first = {
        "id": "synthetic-conversation-a",
        "conversation_id": "synthetic-conversation-a",
        "title": "redacted-fixture-a",
        "create_time": 1,
        "update_time": 2,
        "current_node": "a2",
        "mapping": {
            "a1": {
                "id": "a1",
                "parent": None,
                "children": ["a2", "a-branch"],
                "message": {
                    "id": "a1",
                    "author": {"role": "user"},
                    "create_time": 1,
                    "update_time": 1,
                    "content": {"content_type": "text", "parts": ["fixture"]},
                    "status": "finished_successfully",
                },
            },
            "a2": {
                "id": "a2",
                "parent": "a1",
                "children": [],
                "message": {
                    "id": "a2",
                    "author": {"role": "assistant"},
                    "create_time": 2,
                    "update_time": 2,
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [
                            {
                                "type": "image_asset_pointer",
                                "id": "synthetic-asset-a",
                                "asset_pointer": "assets/fixture.png",
                                "name": "fixture.png",
                                "mime_type": "image/png",
                                "size_bytes": 19,
                            }
                        ],
                    },
                    "status": "finished_successfully",
                },
            },
            "a-branch": {
                "id": "a-branch",
                "parent": "a1",
                "children": [],
                "message": {
                    "id": "a-branch",
                    "author": {"role": "assistant"},
                    "create_time": 1.5,
                    "update_time": 1.5,
                    "content": {
                        "content_type": "text",
                        "parts": ["audit-only-branch"],
                    },
                    "status": "finished_successfully",
                },
            },
        },
    }
    second = {
        "id": "synthetic-conversation-old-time-new-id",
        "conversation_id": "synthetic-conversation-old-time-new-id",
        "title": "redacted-fixture-b",
        "create_time": -100,
        "update_time": -50,
        "current_node": "b1",
        "mapping": {
            "b1": {
                "id": "b1",
                "parent": None,
                "children": [],
                "message": {
                    "id": "b1",
                    "author": {"role": "user"},
                    "create_time": -100,
                    "update_time": -100,
                    "content": {"content_type": "text", "parts": ["old-time"]},
                    "status": "finished_successfully",
                },
            }
        },
    }
    return [first, second]


def existing_from_record(record: Any, ordinal: int) -> ExistingRecord:
    descriptor = record.plan_descriptor()
    return ExistingRecord(
        record_kind=descriptor["record_kind"],
        source_type=record.provenance.source_type,
        source_identity=descriptor["source_identity"],
        production_identity=f"synthetic-production-{ordinal}",
        content_hash=descriptor["content_hash"],
        structure_hash=descriptor["structure_hash"],
        field_hashes=descriptor.get("field_hashes", {}),
        parent_source_identity=descriptor.get("source_conversation_identity"),
        occurred_at=descriptor.get("occurred_at"),
        metadata_digest=descriptor.get("metadata_digest"),
        content_sha256=descriptor.get("content_sha256"),
        actual_size=descriptor.get("actual_size"),
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    before = file_gate()
    compatibility, production_fingerprint = production_compatibility()
    media = build_media_index(
        PRODUCTION_DB,
        inspect_entities=False,
        compute_hash=False,
    )
    compatibility["media"] = {
        "occurrences": len(media.records),
        "logical_identities": len(
            {record.media_id for record in media.records if record.media_id}
        ),
        "identity_kinds": dict(
            sorted(
                Counter(
                    record.source_identity_kind for record in media.records
                ).items()
            )
        ),
        "entity_states": dict(
            sorted(
                Counter(
                    record.entity_state.value for record in media.records
                ).items()
            )
        ),
        "available_entities": sum(record.can_open for record in media.records),
    }
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "synthetic-chatgpt.zip"
        png = b"\x89PNG\r\n\x1a\nfixture-png"
        with zipfile.ZipFile(
            source,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "conversations.json",
                json.dumps(
                    synthetic_conversations(),
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            archive.writestr("assets/fixture.png", png)
        adapter = ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )
        full = adapter.produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="full",
        )
        full_bundle = full.bundle
        existing = [
            existing_from_record(record, ordinal)
            for ordinal, record in enumerate(
                [*full_bundle.conversations, *full_bundle.messages]
            )
            if (
                getattr(record, "provenance", None) is not None
                and (
                    record.provenance.source_record_id
                    == "synthetic-conversation-a"
                    or getattr(record, "source_conversation_id", None)
                    == "synthetic-conversation-a"
                )
            )
        ]
        incremental_target = make_target_snapshot(
            existing_records=existing,
            production_fingerprint=production_fingerprint,
            source_snapshot_complete=False,
        )
        incremental = adapter.produce_import_plan(
            target_snapshot=incremental_target,
            batch_mode="incremental",
        )
        scan_summary = {
            "probe": adapter.probe_source().to_dict(),
            "manifest": adapter.scan_manifest().to_dict(),
            "audit": adapter.validate_source().to_dict(),
            "deterministic_rescan": adapter.verify_scan_determinism(),
            "source_is_synthetic": True,
            "private_content_included": False,
            "absolute_path_included": False,
        }
        normalized_counts = {
            "full": full.bundle.summary(),
            "incremental": incremental.bundle.summary(),
            "shared_normalized_digest": (
                full.bundle.normalized_digest
                == incremental.bundle.normalized_digest
            ),
            "attachment_occurrences": len(
                full.bundle.attachment_occurrences
            ),
            "logical_attachments": len(full.bundle.attachments),
            "asset_entities": len(full.bundle.asset_entities),
        }
    after = file_gate()
    compatibility["file_gate_before"] = before
    compatibility["file_gate_after"] = after
    compatibility["content_and_size_unchanged"] = all(
        before[name].get("sha256") == after[name].get("sha256")
        and before[name].get("size") == after[name].get("size")
        for name in before
    )
    compatibility["mtime_unchanged"] = {
        name: before[name].get("mtime_utc") == after[name].get("mtime_utc")
        for name in before
    }
    compatibility["stash_touched_by_script"] = False
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT_DIR / "adapter_scan_summary.json", scan_summary)
    write_json(OUTPUT_DIR / "normalized_counts.json", normalized_counts)
    write_json(
        OUTPUT_DIR / "synthetic_full_plan_summary.json",
        full.summary(),
    )
    write_json(
        OUTPUT_DIR / "synthetic_incremental_plan_summary.json",
        incremental.summary(),
    )
    write_json(
        OUTPUT_DIR / "compatibility_snapshot_summary.json",
        compatibility,
    )
    print(
        json.dumps(
            {
                "evidence_files": 5,
                "synthetic_full_plan": full.import_plan.plan_id,
                "synthetic_incremental_plan": incremental.import_plan.plan_id,
                "production_content_gate": compatibility[
                    "content_and_size_unchanged"
                ],
                "production_operation_executed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
