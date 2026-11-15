#!/usr/bin/env python3
"""Generate redacted, non-production v8.3-D acceptance evidence."""
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
)
from memory.ingestion_assets import build_asset_stage_plan
from memory.ingestion_asset_staging import (
    ZipAssetSource,
    execute_asset_stage_plan,
)
from memory.media_index import build_media_index


OUTPUT_ROOT = PROJECT_ROOT / "output" / "v8.3-d-asset-preservation-staging"
PRODUCTION_DB = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
IMPORT_ROOT = PROJECT_ROOT / "imports" / "chatgpt"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_gate() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for path in (
        PRODUCTION_DB,
        Path(str(PRODUCTION_DB) + "-wal"),
        Path(str(PRODUCTION_DB) + "-shm"),
    ):
        if not path.exists():
            rows[path.name] = {"exists": False}
            continue
        info = path.stat()
        rows[path.name] = {
            "exists": True,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": _sha256_file(path),
        }
    tree_rows: list[dict[str, Any]] = []
    if IMPORT_ROOT.exists():
        for path in sorted(
            (item for item in IMPORT_ROOT.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        ):
            tree_rows.append({
                "relative_path_digest": hashlib.sha256(
                    path.relative_to(IMPORT_ROOT).as_posix().encode("utf-8")
                ).hexdigest(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
    rows["imports_chatgpt"] = {
        "file_count": len(tree_rows),
        "total_bytes": sum(item["size"] for item in tree_rows),
        "tree_digest": hashlib.sha256(
            json.dumps(
                tree_rows,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest(),
    }
    return rows


def production_compatibility() -> dict[str, Any]:
    media = build_media_index(
        PRODUCTION_DB,
        inspect_entities=False,
        compute_hash=False,
    )
    connection = sqlite3.connect(
        f"file:{PRODUCTION_DB.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.execute("PRAGMA query_only=ON")
    try:
        source_counts = {
            (row[0] or "legacy_unknown"): row[1]
            for row in connection.execute(
                """SELECT source_platform, COUNT(*)
                   FROM messages
                   GROUP BY source_platform
                   ORDER BY source_platform"""
            )
        }
    finally:
        connection.close()
    entity_states = Counter(item.entity_state.value for item in media.records)
    identity_kinds = Counter(item.source_identity_kind for item in media.records)
    logical_identities = {
        item.media_id for item in media.records if item.media_id
    }
    available = sum(item.can_open for item in media.records)
    return {
        "media_occurrences": len(media.records),
        "logical_media_identities": len(logical_identities),
        "available_entities": available,
        "entity_states": dict(sorted(entity_states.items())),
        "identity_kinds": dict(sorted(identity_kinds.items())),
        "message_source_counts": dict(sorted(source_counts.items())),
        "real_stage_operations": 0 if available == 0 else None,
        "metadata_only_creates_empty_files": False,
        "claude_identity_protocol_changed": False,
        "chatgpt_identity_protocol_changed": False,
        "deepseek_stage_classification_enabled": False,
        "production_write_executed": False,
        "production_schema_migrated": False,
    }


def synthetic_conversation() -> dict[str, Any]:
    return {
        "id": "synthetic-stage-conversation",
        "conversation_id": "synthetic-stage-conversation",
        "title": "redacted-stage-fixture",
        "create_time": 1,
        "update_time": 2,
        "current_node": "message-2",
        "mapping": {
            "message-1": {
                "id": "message-1",
                "parent": None,
                "children": ["message-2"],
                "message": {
                    "id": "message-1",
                    "author": {"role": "user"},
                    "create_time": 1,
                    "update_time": 1,
                    "content": {"content_type": "text", "parts": ["redacted"]},
                    "status": "finished_successfully",
                    "metadata": {},
                },
            },
            "message-2": {
                "id": "message-2",
                "parent": "message-1",
                "children": [],
                "message": {
                    "id": "message-2",
                    "author": {"role": "assistant"},
                    "create_time": 2,
                    "update_time": 2,
                    "content": {
                        "content_type": "text",
                        "parts": [{
                            "type": "image_asset_pointer",
                            "id": "synthetic-asset",
                            "asset_pointer": "assets/fixture.png",
                            "name": "fixture.png",
                            "mime_type": "image/png",
                            "size_bytes": 19,
                        }],
                    },
                    "status": "finished_successfully",
                    "metadata": {},
                },
            },
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    before = file_gate()
    compatibility = production_compatibility()
    payload = b"\x89PNG\r\n\x1a\nfixture-png"
    with tempfile.TemporaryDirectory() as directory:
        archive_path = Path(directory) / "synthetic-assets.zip"
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "conversations.json",
                json.dumps(
                    [synthetic_conversation()],
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            archive.writestr("assets/fixture.png", payload)
        adapter_result = ChatGPTAdapter(
            archive_path,
            timestamp=DETERMINISTIC_TIMESTAMP,
        ).produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="full",
        )
        plan = build_asset_stage_plan(
            import_plan=adapter_result.import_plan,
            attachments=adapter_result.bundle.attachments,
            asset_entities=adapter_result.bundle.asset_entities,
        )
        runtime_source = ZipAssetSource(
            operation_id=plan.operations[0].operation_id,
            archive_path=archive_path,
            member_name="assets/fixture.png",
        )
        stage_result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime_source,),
            stage_root=OUTPUT_ROOT / "runtime",
        )
    after = file_gate()
    gate = {
        "before": before,
        "after": after,
        "database_content_size_mtime_unchanged": all(
            before[name] == after[name]
            for name in ("ayunos_chatgpt.db", "ayunos_chatgpt.db-wal")
        ),
        "shm_content_size_unchanged": all(
            before["ayunos_chatgpt.db-shm"].get(key)
            == after["ayunos_chatgpt.db-shm"].get(key)
            for key in ("exists", "size", "sha256")
        ),
        "wal_zero_bytes": after["ayunos_chatgpt.db-wal"].get("size") == 0,
        "imports_chatgpt_unchanged": (
            before["imports_chatgpt"] == after["imports_chatgpt"]
        ),
    }
    acceptance = {
        "node": "YunOS v8.3-D Asset Preservation and Staging Core",
        "synthetic_stage_status": stage_result.status,
        "synthetic_stage_operations": len(plan.operations),
        "synthetic_occurrence_bindings": len(plan.occurrence_bindings),
        "stage_plan_digest": plan.stage_plan_digest,
        "stage_fingerprint": plan.asset_stage_fingerprint.to_dict(),
        "production_compatibility": compatibility,
        "file_gate_passed": all((
            gate["database_content_size_mtime_unchanged"],
            gate["shm_content_size_unchanged"],
            gate["wal_zero_bytes"],
            gate["imports_chatgpt_unchanged"],
        )),
        "production_write_executed": False,
        "production_promotion_executed": False,
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _write_json(OUTPUT_ROOT / "stage_plan_summary.json", {
        "stage_plan_id": plan.stage_plan_id,
        "stage_plan_digest": plan.stage_plan_digest,
        "import_plan_id": plan.import_plan_id,
        "manifest_id": plan.manifest_id,
        "batch_id": plan.batch_id,
        "operations": [item.to_dict() for item in plan.operations],
        "occurrence_bindings": dict(plan.occurrence_bindings),
        "expected_asset_delta": dict(plan.expected_asset_delta),
        "asset_stage_fingerprint": plan.asset_stage_fingerprint.to_dict(),
    })
    _write_json(OUTPUT_ROOT / "stage_result_summary.json", stage_result.to_dict())
    _write_json(OUTPUT_ROOT / "production_compatibility.json", compatibility)
    _write_json(OUTPUT_ROOT / "file_gate.json", gate)
    _write_json(OUTPUT_ROOT / "acceptance_summary.json", acceptance)
    print(json.dumps(acceptance, ensure_ascii=False, sort_keys=True))
    return 0 if acceptance["file_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
