from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory.ingestion_adapters.chatgpt import (
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    empty_target_snapshot,
    make_target_snapshot,
)
from memory.ingestion_core import build_database_fingerprint
from memory.ingestion_planning import ExistingRecord, make_source_identity
from tests.v83_chatgpt_adapter_fixtures import (
    PNG,
    existing_from_record,
    simple_conversation,
    write_json_source,
    write_zip_source,
)


def fingerprint(label: str):
    payload = label.encode("utf-8")
    return build_database_fingerprint(
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        schema_descriptor={"schema": label},
        logical_descriptor={"logical": label},
    )


class ChatGPTAdapterPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, source: Path) -> ChatGPTAdapter:
        return ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )

    def target(
        self,
        records: list[ExistingRecord] | tuple[ExistingRecord, ...] = (),
        *,
        complete: bool = False,
        label: str = "target",
    ):
        return make_target_snapshot(
            existing_records=records,
            production_fingerprint=fingerprint(label),
            source_snapshot_complete=complete,
        )

    def incoming_existing_records(self, source: Path) -> list[ExistingRecord]:
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="incremental")
        records: list[ExistingRecord] = []
        for index, record in enumerate([*bundle.conversations, *bundle.messages]):
            records.append(
                existing_from_record(record, f"production-{index}")
            )
        return records

    def test_full_empty_target_creates_complete_nonexecuted_plan(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        result = self.adapter(source).produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="full",
        )
        self.assertEqual(1, result.import_plan.counts["insert"])
        self.assertEqual(2, result.import_plan.counts["append"])
        self.assertEqual(0, result.import_plan.counts["conflicts"])
        self.assertFalse(result.summary()["operation_executed"])

    def test_full_and_incremental_share_exact_normalized_semantics(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        full = self.adapter(source).build_normalized_bundle(batch_mode="full")
        incremental = self.adapter(source).build_normalized_bundle(
            batch_mode="incremental"
        )
        self.assertEqual(full.normalized_digest, incremental.normalized_digest)
        self.assertEqual(
            [item.content_hash for item in full.messages],
            [item.content_hash for item in incremental.messages],
        )
        self.assertNotEqual(full.batch.batch_id, incremental.batch.batch_id)
        self.assertEqual("full", full.batch.batch_mode)
        self.assertEqual("incremental", incremental.batch.batch_mode)

    def test_exact_duplicate_source_id_and_digest_is_no_change(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        existing = self.incoming_existing_records(source)
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target(existing),
            batch_mode="incremental",
        )
        self.assertEqual(3, result.import_plan.counts["no_change"])
        self.assertEqual(0, result.import_plan.counts["conflicts"])

    def test_same_source_id_changed_content_is_conflict(self) -> None:
        original = write_json_source(
            self.root,
            [simple_conversation(user_text="original")],
            name="original.json",
        )
        existing = self.incoming_existing_records(original)
        changed = write_json_source(
            self.root,
            [simple_conversation(user_text="changed")],
            name="changed.json",
        )
        result = self.adapter(changed).produce_import_plan(
            target_snapshot=self.target(existing),
            batch_mode="incremental",
        )
        self.assertGreater(result.import_plan.counts["conflicts"], 0)
        self.assertIn(
            "message_content_changed",
            {item.conflict_type for item in result.import_plan.conflicts},
        )

    def test_old_time_new_source_id_reaches_planner(self) -> None:
        original_conversation = simple_conversation(conversation_id="existing")
        original = write_json_source(
            self.root,
            [original_conversation],
            name="original.json",
        )
        existing = self.incoming_existing_records(original)
        new_old = simple_conversation(conversation_id="old-time-new-id")
        new_old["create_time"] = -100
        new_old["update_time"] = -50
        source = write_json_source(
            self.root,
            [original_conversation, new_old],
            name="incremental.json",
        )
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target(existing),
            batch_mode="incremental",
        )
        new_conversation_identity = make_source_identity(
            "conversation",
            "chatgpt",
            "old-time-new-id",
        )
        actions = {
            (item.source_identity, item.action_type)
            for item in result.import_plan.actions
        }
        self.assertIn((new_conversation_identity, "insert"), actions)

    def test_different_id_same_text_is_not_silently_deduplicated(self) -> None:
        existing_source = write_json_source(
            self.root,
            [simple_conversation(conversation_id="existing")],
            name="existing.json",
        )
        existing = self.incoming_existing_records(existing_source)
        incoming = simple_conversation(conversation_id="incoming")
        incoming["mapping"]["message-1"]["message"]["id"] = "incoming-message-1"
        incoming["mapping"]["message-1"]["id"] = "incoming-message-1"
        incoming["mapping"]["message-2"]["message"]["id"] = "incoming-message-2"
        incoming["mapping"]["message-2"]["id"] = "incoming-message-2"
        incoming["mapping"] = {
            "incoming-message-1": incoming["mapping"].pop("message-1"),
            "incoming-message-2": incoming["mapping"].pop("message-2"),
        }
        incoming["mapping"]["incoming-message-1"]["children"] = ["incoming-message-2"]
        incoming["mapping"]["incoming-message-2"]["parent"] = "incoming-message-1"
        incoming["current_node"] = "incoming-message-2"
        source = write_json_source(self.root, [incoming], name="incoming.json")
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target(existing),
            batch_mode="incremental",
        )
        incoming_identity = make_source_identity(
            "message", "chatgpt", "incoming-message-1"
        )
        matching = [
            action
            for action in result.import_plan.actions
            if action.source_identity == incoming_identity
        ]
        self.assertEqual(1, len(matching))
        self.assertIn(
            matching[0].action_type,
            {"append", "link_candidate"},
        )

    def test_complete_snapshot_creates_tombstone_candidate_never_delete(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        missing = ExistingRecord(
            record_kind="conversation",
            source_type="chatgpt",
            source_identity=make_source_identity(
                "conversation", "chatgpt", "missing-source"
            ),
            production_identity="production-missing",
            content_hash="1" * 64,
            structure_hash="2" * 64,
            field_hashes={},
        )
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target([missing], complete=True),
            batch_mode="incremental",
        )
        action_types = {action.action_type for action in result.import_plan.actions}
        self.assertIn("tombstone_candidate", action_types)
        self.assertNotIn("delete", action_types)
        self.assertTrue(result.import_plan.approval_required)

    def test_incomplete_snapshot_never_infers_deletion(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        missing = ExistingRecord(
            record_kind="conversation",
            source_type="chatgpt",
            source_identity=make_source_identity(
                "conversation", "chatgpt", "missing-source"
            ),
            production_identity="production-missing",
            content_hash="1" * 64,
            structure_hash="2" * 64,
            field_hashes={},
        )
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target([missing], complete=False),
            batch_mode="incremental",
        )
        self.assertNotIn(
            "tombstone_candidate",
            {action.action_type for action in result.import_plan.actions},
        )

    def test_deepseek_and_legacy_records_are_not_chatgpt_tombstones(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        records = [
            ExistingRecord(
                record_kind="conversation",
                source_type=source_type,
                source_identity=make_source_identity(
                    "conversation", source_type, f"{source_type}-source"
                ),
                production_identity=f"production-{source_type}",
                content_hash="1" * 64,
                structure_hash="2" * 64,
                field_hashes={},
            )
            for source_type in ("deepseek", "legacy_unknown")
        ]
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target(records, complete=True),
            batch_mode="incremental",
        )
        tombstones = [
            action
            for action in result.import_plan.actions
            if action.action_type == "tombstone_candidate"
        ]
        self.assertEqual([], tombstones)

    def test_repeated_same_source_and_target_has_identical_plan_digest(self) -> None:
        source = write_zip_source(self.root, [simple_conversation()])
        target = self.target(label="stable")
        first = self.adapter(source).produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
        )
        second = self.adapter(source).produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
        )
        self.assertEqual(first.import_plan.plan_id, second.import_plan.plan_id)
        self.assertEqual(first.bundle.batch.plan_digest, second.bundle.batch.plan_digest)
        self.assertEqual(first.semantic_plan_digest, second.semantic_plan_digest)
        self.assertEqual(
            [item.action_id for item in first.import_plan.actions],
            [item.action_id for item in second.import_plan.actions],
        )

    def test_normalized_digest_is_stable_across_processes(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        code = (
            "from memory.ingestion_adapters.chatgpt import "
            "ChatGPTAdapter,DETERMINISTIC_TIMESTAMP;"
            f"print(ChatGPTAdapter(r'{source}',timestamp=DETERMINISTIC_TIMESTAMP)"
            ".build_normalized_bundle(batch_mode='full').normalized_digest)"
        )
        env = dict(__import__("os").environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        first = subprocess.check_output(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
        ).strip()
        second = subprocess.check_output(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
        ).strip()
        self.assertEqual(first, second)

    def test_zip_entry_order_does_not_change_semantic_plan(self) -> None:
        part = {
            "type": "image_asset_pointer",
            "id": "asset-1",
            "asset_pointer": "image.png",
            "name": "image.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG),
        }
        conversation = simple_conversation(assistant_parts=[part])
        first_source = write_zip_source(
            self.root,
            [conversation],
            assets={"image.png": PNG},
        )
        second_source = write_zip_source(
            self.root,
            [conversation],
            assets={"image.png": PNG},
            reverse_entries=True,
        )
        target = self.target(label="same")
        first = self.adapter(first_source).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        second = self.adapter(second_source).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        self.assertEqual(first.bundle.normalized_digest, second.bundle.normalized_digest)
        self.assertEqual(first.semantic_plan_digest, second.semantic_plan_digest)
        self.assertNotEqual(
            first.manifest.archive_sha256,
            second.manifest.archive_sha256,
        )

    def test_source_change_changes_manifest_and_plan(self) -> None:
        first_source = write_json_source(
            self.root,
            [simple_conversation(user_text="a")],
            name="a.json",
        )
        second_source = write_json_source(
            self.root,
            [simple_conversation(user_text="b")],
            name="b.json",
        )
        target = self.target()
        first = self.adapter(first_source).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        second = self.adapter(second_source).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        self.assertNotEqual(first.manifest.manifest_id, second.manifest.manifest_id)
        self.assertNotEqual(first.import_plan.plan_id, second.import_plan.plan_id)
        self.assertNotEqual(first.bundle.normalized_digest, second.bundle.normalized_digest)

    def test_target_change_changes_plan_and_target_fingerprint(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        empty = self.target(label="empty")
        changed = self.target(label="changed")
        first = self.adapter(source).produce_import_plan(
            target_snapshot=empty,
            batch_mode="full",
        )
        second = self.adapter(source).produce_import_plan(
            target_snapshot=changed,
            batch_mode="full",
        )
        self.assertNotEqual(
            first.target_snapshot.target_fingerprint,
            second.target_snapshot.target_fingerprint,
        )
        self.assertNotEqual(first.import_plan.plan_id, second.import_plan.plan_id)

    def test_batch_mode_is_bound_into_plan_identity(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        target = self.target()
        full = self.adapter(source).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        incremental = self.adapter(source).produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
        )
        self.assertNotEqual(full.bundle.batch.batch_id, incremental.bundle.batch.batch_id)
        self.assertNotEqual(full.import_plan.plan_id, incremental.import_plan.plan_id)
        self.assertEqual(full.manifest.manifest_id, incremental.manifest.manifest_id)

    def test_adapter_calls_v83_b_planner_and_never_executes_actions(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        from memory.ingestion_adapters import chatgpt as module

        real_plan = module.plan_import
        with patch.object(module, "plan_import", wraps=real_plan) as planner:
            result = self.adapter(source).produce_import_plan(
                target_snapshot=self.target(),
                batch_mode="full",
            )
        planner.assert_called_once()
        self.assertFalse(result.summary()["operation_executed"])
        self.assertTrue(all(hasattr(item, "action_type") for item in result.import_plan.actions))

    def test_verified_asset_is_planned_as_existing_v83_b_asset_record(self) -> None:
        part = {
            "type": "image_asset_pointer",
            "id": "asset-verified",
            "asset_pointer": "image.png",
            "name": "image.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG),
        }
        source = write_zip_source(
            self.root,
            [simple_conversation(assistant_parts=[part])],
            assets={"image.png": PNG},
        )
        result = self.adapter(source).produce_import_plan(
            target_snapshot=self.target(),
            batch_mode="full",
        )
        self.assertEqual(1, len(result.bundle.asset_entities))
        asset_actions = [
            action
            for action in result.import_plan.actions
            if action.record_kind == "asset"
        ]
        self.assertEqual(1, len(asset_actions))
        self.assertEqual("insert", asset_actions[0].action_type)


if __name__ == "__main__":
    unittest.main()
