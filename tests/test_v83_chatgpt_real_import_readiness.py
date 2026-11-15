from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from memory.ingestion_adapters.chatgpt import (
    ALL_REACHABLE_AUDIT,
    CONTENT_SNIFF_MIME_V1,
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    LEGACY_IMPORT_TEXT_V1,
    empty_target_snapshot,
    make_target_snapshot,
)
from memory.ingestion_assets import build_asset_stage_plan
from memory.ingestion_planning import ExistingRecord, make_source_identity
from tests.v83_chatgpt_adapter_fixtures import PNG, simple_conversation


class ChatGPTRealImportReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source(
        self,
        part: dict[str, object],
        *,
        assets: dict[str, bytes],
        names: dict[str, str] | None = None,
        library: list[dict[str, object]] | None = None,
    ) -> Path:
        path = self.root / "official-export.zip"
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "conversations.json",
                json.dumps(
                    [simple_conversation(assistant_parts=[part])],
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            for name, payload in assets.items():
                archive.writestr(name, payload)
            if names is not None:
                archive.writestr(
                    "conversation_asset_file_names.json",
                    json.dumps(names).encode("utf-8"),
                )
            if library is not None:
                archive.writestr(
                    "library_files.json",
                    json.dumps(library).encode("utf-8"),
                )
        return path

    def adapter(self, source: Path) -> ChatGPTAdapter:
        return ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )

    def test_url_decode_and_separator_normalization_keep_full_path_exact(
        self,
    ) -> None:
        source = self.source(
            {
                "type": "image_asset_pointer",
                "asset_pointer": "sandbox:/folder/image%20one.png",
                "name": "image one.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG),
            },
            assets={"folder/image one.png": PNG},
        )
        occurrence = self.adapter(source).enumerate_assets()[0]
        self.assertEqual("verified", occurrence.entity_state)
        self.assertEqual(
            "explicit_archive_relative_path",
            occurrence.mapping_method,
        )
        self.assertEqual(1, occurrence.resolution_candidate_count)

    def test_official_file_id_relation_is_preferred_over_duplicate_name(
        self,
    ) -> None:
        source = self.source(
            {
                "type": "image_asset_pointer",
                "asset_pointer": "sediment://file-selected",
                "name": "same.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG),
            },
            assets={
                "file-selected.dat": PNG,
                "file-other.dat": PNG + b"other",
            },
            names={
                "file-selected.dat": "same.png",
                "file-other.dat": "same.png",
            },
            library=[
                {
                    "file_id": "file-selected",
                    "file_name": "same.png",
                    "file_size_bytes": len(PNG),
                    "mime_type": "image/png",
                    "origination_message_id": "message-2",
                }
            ],
        )
        occurrence = self.adapter(source).enumerate_assets()[0]
        self.assertEqual("verified", occurrence.entity_state)
        self.assertEqual(
            "explicit_file_or_upload_id",
            occurrence.mapping_method,
        )

    def test_duplicate_basename_size_mime_can_disambiguate(self) -> None:
        selected = PNG
        other = PNG + b"different-size"
        source = self.source(
            {
                "type": "file",
                "name": "same.png",
                "mime_type": "image/png",
                "size_bytes": len(selected),
            },
            assets={"file-a.dat": selected, "file-b.dat": other},
            names={"file-a.dat": "same.png", "file-b.dat": "same.png"},
        )
        occurrence = self.adapter(source).enumerate_assets()[0]
        self.assertEqual("verified", occurrence.entity_state)
        self.assertEqual(
            "unique_basename_size_mime_or_hash",
            occurrence.mapping_method,
        )

    def test_hash_assisted_mapping_is_exact(self) -> None:
        selected = PNG + b"a"
        other = PNG + b"b"
        self.assertEqual(len(selected), len(other))
        source = self.source(
            {
                "type": "file",
                "name": "same.png",
                "mime_type": "image/png",
                "size_bytes": len(selected),
                "sha256": hashlib.sha256(selected).hexdigest(),
            },
            assets={"file-a.dat": selected, "file-b.dat": other},
            names={"file-a.dat": "same.png", "file-b.dat": "same.png"},
        )
        occurrence = self.adapter(source).enumerate_assets()[0]
        self.assertEqual("verified", occurrence.entity_state)
        self.assertEqual(hashlib.sha256(selected).hexdigest(), occurrence.content_sha256)

    def test_unprovable_duplicate_name_is_explicit_conflict_not_text_loss(
        self,
    ) -> None:
        source = self.source(
            {
                "type": "file",
                "text": "visible text remains normalized",
                "name": "same.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG),
            },
            assets={"file-a.dat": PNG, "file-b.dat": PNG},
            names={"file-a.dat": "same.png", "file-b.dat": "same.png"},
        )
        adapter = self.adapter(source)
        result = adapter.produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="incremental",
        )
        self.assertIn("visible text remains normalized", result.bundle.messages[1].text)
        self.assertEqual("entity_blocked", result.bundle.attachments[0].entity_state)
        self.assertIn(
            "asset_locator_ambiguous",
            {item.conflict_type for item in result.import_plan.conflicts},
        )
        self.assertFalse(result.bundle.asset_entities)

    def test_real_locator_outputs_are_deterministic(self) -> None:
        source = self.source(
            {
                "type": "image_asset_pointer",
                "asset_pointer": "sediment://file-stable",
                "name": "stable.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG),
            },
            assets={"file-stable.dat": PNG},
            names={"file-stable.dat": "stable.png"},
            library=[
                {
                    "file_id": "file-stable",
                    "file_name": "stable.png",
                    "file_size_bytes": len(PNG),
                    "mime_type": "image/png",
                    "origination_message_id": "message-2",
                }
            ],
        )
        self.assertTrue(self.adapter(source).verify_scan_determinism())

    def test_same_content_uses_content_sniffed_mime_not_filename_extension(
        self,
    ) -> None:
        payload = b"same bytes without a recognized magic signature"
        source = self.root / "mime-conflict.zip"
        conversation = simple_conversation(
            assistant_parts=[
                {
                    "type": "file",
                    "asset_pointer": "sediment://file-a",
                    "name": "same.txt",
                    "mime_type": "text/plain",
                    "size_bytes": len(payload),
                },
                {
                    "type": "file",
                    "asset_pointer": "sediment://file-b",
                    "name": "same.json",
                    "mime_type": "application/json",
                    "size_bytes": len(payload),
                },
            ]
        )
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "conversations.json",
                json.dumps([conversation]).encode("utf-8"),
            )
            archive.writestr("file-a.dat", payload)
            archive.writestr("file-b.dat", payload)
        adapter = ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
            asset_mime_policy=CONTENT_SNIFF_MIME_V1,
        )
        occurrences = adapter.enumerate_assets()
        self.assertEqual(
            {"text/plain"},
            {item.detected_mime_type for item in occurrences},
        )
        result = adapter.produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="incremental",
            branch_policy=ALL_REACHABLE_AUDIT,
        )
        stage_plan = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
        )
        self.assertEqual(1, len(stage_plan.operations))

    def test_missing_and_external_attachments_remain_explicit(self) -> None:
        source = self.source(
            {
                "type": "file",
                "asset_pointer": "https://example.invalid/not-fetched.bin",
                "name": "not-fetched.bin",
            },
            assets={},
        )
        result = self.adapter(source).produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="incremental",
        )
        attachment = result.bundle.attachments[0]
        self.assertEqual("entity_blocked", attachment.entity_state)
        self.assertEqual("external_url_blocked", attachment.unavailable_reason)
        self.assertFalse(result.bundle.asset_entities)

    def test_legacy_text_policy_determinism_keeps_policy_binding(self) -> None:
        source = self.source(
            {"type": "text", "text": "stable"},
            assets={},
        )
        adapter = ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
            text_policy=LEGACY_IMPORT_TEXT_V1,
        )
        self.assertTrue(adapter.verify_scan_determinism())

    def test_stable_id_dedup_does_not_apply_a_global_time_cutoff(self) -> None:
        source = self.source(
            {"type": "text", "text": "stable"},
            assets={},
        )
        adapter = self.adapter(source)
        bundle = adapter.build_normalized_bundle(batch_mode="incremental")
        conversation = bundle.conversations[0]
        descriptor = conversation.plan_descriptor()
        unrelated_parent = make_source_identity(
            "conversation",
            "chatgpt",
            "unrelated-conversation",
        )
        existing = (
            ExistingRecord(
                record_kind="conversation",
                source_type="chatgpt",
                source_identity=conversation.source_identity,
                production_identity=conversation.provenance.source_record_id,
                content_hash=descriptor["content_hash"],
                structure_hash=descriptor["structure_hash"],
                field_hashes=descriptor["field_hashes"],
            ),
            ExistingRecord(
                record_kind="message",
                source_type="chatgpt",
                source_identity=make_source_identity(
                    "message",
                    "chatgpt",
                    "unrelated-future-message",
                ),
                production_identity="unrelated-future-message",
                content_hash="1" * 64,
                structure_hash="2" * 64,
                field_hashes={"text": "3" * 64},
                parent_source_identity=unrelated_parent,
                occurred_at="2099-01-01T00:00:00.000000Z",
            ),
        )
        target = make_target_snapshot(
            existing_records=existing,
            production_fingerprint=empty_target_snapshot().production_fingerprint,
            source_snapshot_complete=True,
        )
        plan = adapter.produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
        ).import_plan
        self.assertEqual(
            2,
            sum(
                action.record_kind == "message"
                and action.action_type == "append"
                for action in plan.actions
            ),
        )
        self.assertNotIn(
            "old_time_new_id",
            {item.conflict_type for item in plan.conflicts},
        )

    def test_existing_stable_message_id_is_no_change_not_duplicate_write(
        self,
    ) -> None:
        source = self.source(
            {"type": "text", "text": "stable"},
            assets={},
        )
        adapter = self.adapter(source)
        bundle = adapter.build_normalized_bundle(batch_mode="incremental")
        conversation = bundle.conversations[0]
        message = bundle.messages[0]
        conversation_descriptor = conversation.plan_descriptor()
        message_descriptor = message.plan_descriptor()
        target = make_target_snapshot(
            existing_records=(
                ExistingRecord(
                    record_kind="conversation",
                    source_type="chatgpt",
                    source_identity=conversation.source_identity,
                    production_identity=conversation.provenance.source_record_id,
                    content_hash=conversation_descriptor["content_hash"],
                    structure_hash=conversation_descriptor["structure_hash"],
                    field_hashes=conversation_descriptor["field_hashes"],
                ),
                ExistingRecord(
                    record_kind="message",
                    source_type="chatgpt",
                    source_identity=message.source_identity,
                    production_identity=message.provenance.source_record_id,
                    content_hash=message_descriptor["content_hash"],
                    structure_hash=message_descriptor["structure_hash"],
                    field_hashes=message_descriptor["field_hashes"],
                    parent_source_identity=message.source_conversation_identity,
                    occurred_at=message.created_at,
                ),
            ),
            production_fingerprint=empty_target_snapshot().production_fingerprint,
            source_snapshot_complete=True,
        )
        plan = adapter.produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
        ).import_plan
        action_by_identity = {
            item.source_identity: item for item in plan.actions
        }
        self.assertEqual(
            "no_change",
            action_by_identity[message.source_identity].action_type,
        )


if __name__ == "__main__":
    unittest.main()
