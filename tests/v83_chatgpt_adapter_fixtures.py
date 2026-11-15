from __future__ import annotations

import json
from pathlib import Path
import zipfile
from typing import Any, Mapping

from memory.ingestion_planning import ExistingRecord


TS = "2026-07-28T00:00:00.000000Z"
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-png"


def message(
    message_id: str | None,
    role: str,
    parts: list[Any],
    *,
    created: float | None = 1.0,
    updated: float | None = 1.0,
    status: str = "finished_successfully",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": role},
        "create_time": created,
        "update_time": updated,
        "content": {"content_type": "text", "parts": parts},
        "status": status,
        "metadata": dict(metadata or {}),
    }


def node(
    node_id: str,
    raw_message: Mapping[str, Any] | None,
    *,
    parent: str | None,
    children: list[str],
) -> tuple[str, dict[str, Any]]:
    return (
        node_id,
        {
            "id": node_id,
            "parent": parent,
            "children": children,
            "message": dict(raw_message) if raw_message is not None else None,
        },
    )


def simple_conversation(
    *,
    conversation_id: str = "conversation-1",
    title: str = "Fixture",
    user_text: str = "hello",
    assistant_parts: list[Any] | None = None,
) -> dict[str, Any]:
    mapping = dict(
        [
            node(
                "message-1",
                message("message-1", "user", [user_text], created=1, updated=1),
                parent=None,
                children=["message-2"],
            ),
            node(
                "message-2",
                message(
                    "message-2",
                    "assistant",
                    assistant_parts if assistant_parts is not None else ["world"],
                    created=2,
                    updated=2,
                ),
                parent="message-1",
                children=[],
            ),
        ]
    )
    return {
        "id": conversation_id,
        "conversation_id": conversation_id,
        "title": title,
        "create_time": 1,
        "update_time": 2,
        "current_node": "message-2",
        "mapping": mapping,
    }


def branched_conversation() -> dict[str, Any]:
    conversation = simple_conversation()
    conversation["mapping"]["message-1"]["children"] = [
        "message-2",
        "message-branch",
    ]
    conversation["mapping"]["message-branch"] = {
        "id": "message-branch",
        "parent": "message-1",
        "children": [],
        "message": message(
            "message-branch",
            "assistant",
            ["excluded branch"],
            created=1.5,
            updated=1.5,
        ),
    }
    return conversation


def write_json_source(
    root: Path,
    conversations: list[dict[str, Any]],
    *,
    name: str = "conversations.json",
) -> Path:
    path = root / name
    path.write_text(
        json.dumps(conversations, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_directory_source(
    root: Path,
    conversations: list[dict[str, Any]],
    *,
    assets: Mapping[str, bytes] | None = None,
    asset_names: Mapping[str, str] | None = None,
) -> Path:
    source = root / "export"
    source.mkdir()
    write_json_source(source, conversations)
    for name, payload in (assets or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    if asset_names is not None:
        (source / "conversation_asset_file_names.json").write_text(
            json.dumps(dict(asset_names), ensure_ascii=False),
            encoding="utf-8",
        )
    return source


def write_zip_source(
    root: Path,
    conversations: list[dict[str, Any]],
    *,
    assets: Mapping[str, bytes] | None = None,
    asset_names: Mapping[str, str] | None = None,
    reverse_entries: bool = False,
) -> Path:
    path = root / ("export-reversed.zip" if reverse_entries else "export.zip")
    rows: list[tuple[str, bytes]] = [
        (
            "conversations.json",
            json.dumps(conversations, ensure_ascii=False).encode("utf-8"),
        )
    ]
    rows.extend((name, payload) for name, payload in (assets or {}).items())
    if asset_names is not None:
        rows.append(
            (
                "conversation_asset_file_names.json",
                json.dumps(dict(asset_names), ensure_ascii=False).encode("utf-8"),
            )
        )
    if reverse_entries:
        rows.reverse()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in rows:
            archive.writestr(name, payload)
    return path


def existing_from_record(record: Any, production_identity: str) -> ExistingRecord:
    descriptor = record.plan_descriptor()
    return ExistingRecord(
        record_kind=descriptor["record_kind"],
        source_type=record.provenance.source_type,
        source_identity=descriptor["source_identity"],
        production_identity=production_identity,
        content_hash=descriptor["content_hash"],
        structure_hash=descriptor["structure_hash"],
        field_hashes=descriptor.get("field_hashes", {}),
        parent_source_identity=descriptor.get(
            "source_conversation_identity"
        )
        if descriptor["record_kind"] == "message"
        else descriptor.get("source_message_identity"),
        occurred_at=descriptor.get("occurred_at"),
        metadata_digest=descriptor.get("metadata_digest"),
        content_sha256=descriptor.get("content_sha256"),
        actual_size=descriptor.get("actual_size"),
    )
