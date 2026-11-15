from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch

from memory.ingestion_core import (
    AssetStageFingerprint,
    DatabaseFingerprint,
    build_source_manifest,
    create_import_batch,
)
from memory.ingestion_planning import (
    NORMALIZATION_PROTOCOL_VERSION,
    PLANNING_POLICY_VERSION,
    ExistingRecord,
    IngestionPlanningError,
    make_asset_entity,
    make_provenance,
    make_record_locator,
    make_source_identity,
    normalize_attachment,
    normalize_conversation,
    normalize_message,
    plan_import,
)


TS0 = "2026-07-25T23:59:00.000000Z"
TS1 = "2026-07-26T00:00:00.000000Z"
TS2 = "2026-07-26T00:01:00.000000Z"
TS3 = "2026-07-26T00:02:00.000000Z"
TS4 = "2026-07-26T00:03:00.000000Z"
ZERO = "0" * 64
ONE = "1" * 64
TWO = "2" * 64
THREE = "3" * 64


class PlanningFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = build_source_manifest(
            source_type="chatgpt",
            source_version="export_v1",
            adapter_name="fixture_adapter",
            adapter_version="adapter_v1",
            archive_sha256=hashlib.sha256(b"archive").hexdigest(),
            archive_size=7,
            archive_kind="zip",
            discovered_at=TS1,
            scanned_at=TS2,
        )
        self.batch = create_import_batch(
            self.manifest,
            batch_mode="full",
            plan={"phase": "normalization_fixture"},
            timestamp=TS1,
        )
        self.database = DatabaseFingerprint(ZERO, 10, ONE, TWO)
        self.asset_stage = AssetStageFingerprint(0, 0, ONE, TWO)

    def provenance(self, kind: str, source_id: str, ordinal: int = 0):
        return make_provenance(
            record_kind=kind,
            source_type=self.manifest.source_type,
            manifest_id=self.manifest.manifest_id,
            source_record_id=source_id,
            batch_id=self.batch.batch_id,
            adapter_version=self.manifest.adapter_version,
            parsed_at=TS2,
            locator=make_record_locator(
                "json_record",
                ordinal,
                private_locator_material={"source_id": source_id},
            ),
        )

    def conversation(self, source_id: str = "conversation-1", **overrides):
        values = {
            "title": "private title",
            "created_at": TS1,
            "updated_at": TS2,
            "source_status": {},
            "message_source_ids": ["message-1"],
        }
        values.update(overrides)
        return normalize_conversation(
            self.provenance("conversation", source_id),
            **values,
        )

    def message(
        self,
        source_id: str = "message-1",
        conversation_id: str = "conversation-1",
        **overrides,
    ):
        values = {
            "source_conversation_id": conversation_id,
            "source_node_id": "node-" + source_id,
            "role": "user",
            "author": "private author",
            "content_type": "text",
            "text": "private message text",
            "status": "finished",
            "weight": 1,
            "created_at": TS3,
            "updated_at": TS3,
            "parent_source_message_id": None,
            "child_source_message_ids": (),
            "metadata": {},
            "missing_reasons": {"parent_source_message_id": "root_message"},
        }
        values.update(overrides)
        return normalize_message(
            self.provenance("message", source_id),
            **values,
        )

    def attachment(self, source_id: str = "attachment-1", **overrides):
        values = {
            "source_conversation_id": "conversation-1",
            "source_message_id": "message-1",
            "original_filename": "private-name.png",
            "safe_display_filename": "image.png",
            "declared_mime": "image/png",
            "detected_mime": "image/png",
            "declared_size": 100,
            "actual_size": 100,
            "content_sha256": ONE,
            "archive_member_locator": make_record_locator(
                "archive_member",
                3,
                private_locator_material="private/assets/name.png",
            ),
            "entity_state": "entity_available",
            "unavailable_reason": None,
            "missing_reasons": {},
        }
        values.update(overrides)
        return normalize_attachment(
            self.provenance("attachment", source_id),
            **values,
        )

    def plan(
        self,
        *,
        conversations=None,
        messages=None,
        attachments=None,
        assets=None,
        existing=None,
        created_at=TS4,
        complete=True,
        database=None,
        policy=PLANNING_POLICY_VERSION,
        manifest=None,
        batch=None,
    ):
        return plan_import(
            manifest=manifest or self.manifest,
            batch=batch or self.batch,
            conversations=list(conversations if conversations is not None else [self.conversation()]),
            messages=list(messages if messages is not None else [self.message()]),
            attachments=list(attachments or []),
            asset_entities=list(assets or []),
            existing_records=list(existing or []),
            production_fingerprint=database or self.database,
            asset_stage_fingerprint=self.asset_stage,
            created_at=created_at,
            source_snapshot_complete=complete,
            policy_version=policy,
        )

    def existing_from(self, record, production_id: str):
        descriptor = record.plan_descriptor()
        return ExistingRecord(
            record_kind=descriptor["record_kind"],
            source_type=self.manifest.source_type,
            source_identity=descriptor["source_identity"],
            production_identity=production_id,
            content_hash=descriptor["content_hash"],
            structure_hash=descriptor["structure_hash"],
            field_hashes=descriptor.get("field_hashes", {}),
            parent_source_identity=descriptor.get("source_conversation_identity"),
            occurred_at=descriptor.get("occurred_at"),
            metadata_digest=descriptor.get("metadata_digest"),
            content_sha256=descriptor.get("content_sha256"),
            actual_size=descriptor.get("actual_size"),
        )


class BasicPlanningTest(PlanningFixture):
    def test_new_conversation_and_message_plan_insert_and_append(self) -> None:
        plan = self.plan()
        actions = {(item.record_kind, item.action_type) for item in plan.actions}
        self.assertIn(("conversation", "insert"), actions)
        self.assertIn(("message", "append"), actions)
        self.assertFalse(plan.approval_required)
        self.assertEqual(2, plan.counts["automatic_decisions"])

    def test_plan_is_order_and_created_time_stable(self) -> None:
        conversation = self.conversation()
        message = self.message()
        first = self.plan(
            conversations=[conversation],
            messages=[message],
            created_at=TS3,
        )
        second = self.plan(
            conversations=list(reversed([conversation])),
            messages=list(reversed([message])),
            created_at=TS4,
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.normalized_records_digest, second.normalized_records_digest)

    def test_same_source_content_and_structure_is_no_change(self) -> None:
        conversation = self.conversation()
        message = self.message()
        plan = self.plan(
            conversations=[conversation],
            messages=[message],
            existing=[
                self.existing_from(conversation, "production-conversation"),
                self.existing_from(message, "production-message"),
            ],
        )
        self.assertTrue(all(item.action_type == "no_change" for item in plan.actions))
        self.assertFalse(plan.approval_required)

    def test_plan_counts_match_actions_conflicts_and_decisions(self) -> None:
        plan = self.plan()
        self.assertEqual(len(plan.actions), plan.counts["actions"])
        self.assertEqual(len(plan.conflicts), plan.counts["conflicts"])
        self.assertEqual(
            len(plan.automatic_decisions),
            plan.counts["automatic_decisions"],
        )
        for action_type in (
            "insert", "append", "no_change", "conflict", "tombstone_candidate",
        ):
            self.assertEqual(
                sum(item.action_type == action_type for item in plan.actions),
                plan.counts[action_type],
            )

    def test_plan_binds_policy_and_database_snapshot(self) -> None:
        first = self.plan()
        policy_changed = self.plan(policy="v8.3-b-deterministic-planning-2")
        database_changed = self.plan(
            database=DatabaseFingerprint(THREE, 10, ONE, TWO),
        )
        self.assertNotEqual(first.plan_id, policy_changed.plan_id)
        self.assertNotEqual(first.plan_id, database_changed.plan_id)

    def test_plan_commit_prerequisites_are_explicit(self) -> None:
        plan = self.plan()
        self.assertEqual(
            {
                "approval_digest_matches_plan",
                "asset_stage_revalidated",
                "database_snapshot_revalidated",
                "single_writer_lease_active",
            },
            set(plan.commit_prerequisites),
        )


class ConversationAndMessageConflictTest(PlanningFixture):
    def test_same_message_id_content_change_is_high_risk_conflict(self) -> None:
        before = self.message(text="before")
        after = self.message(text="after")
        plan = self.plan(
            messages=[after],
            existing=[self.existing_from(before, "production-message")],
        )
        self.assertIn("message_content_changed", {item.conflict_type for item in plan.conflicts})
        self.assertTrue(plan.approval_required)
        self.assertFalse(any(item.action_type == "append" for item in plan.actions if item.record_kind == "message"))

    def test_same_message_content_relationship_change_is_conflict(self) -> None:
        before = self.message()
        existing = self.existing_from(before, "production-message")
        existing = replace(existing, structure_hash=ZERO)
        plan = self.plan(messages=[before], existing=[existing])
        self.assertIn("relationship_changed", {item.conflict_type for item in plan.conflicts})

    def test_conversation_title_or_time_change_is_explicit_conflict(self) -> None:
        before = self.conversation(title="before")
        after = self.conversation(title="after", updated_at=TS3)
        plan = self.plan(
            conversations=[after],
            existing=[self.existing_from(before, "production-conversation")],
        )
        self.assertIn("conversation_fields_changed", {item.conflict_type for item in plan.conflicts})

    def test_old_time_new_id_is_conflict_not_silently_skipped(self) -> None:
        conversation = self.conversation()
        old_existing_message = self.message("existing-message", created_at=TS3)
        existing = self.existing_from(old_existing_message, "production-existing-message")
        new_old_message = self.message("new-old-message", created_at=TS2, updated_at=TS2)
        plan = self.plan(
            conversations=[conversation],
            messages=[new_old_message],
            existing=[existing],
        )
        self.assertIn("old_time_new_id", {item.conflict_type for item in plan.conflicts})
        self.assertTrue(plan.approval_required)

    def test_newer_message_is_append(self) -> None:
        existing_message = self.message("existing-message", created_at=TS2)
        existing = self.existing_from(existing_message, "production-existing-message")
        new_message = self.message("new-message", created_at=TS4, updated_at=TS4)
        plan = self.plan(
            messages=[existing_message, new_message],
            existing=[existing],
        )
        action = next(
            item for item in plan.actions
            if item.source_identity == new_message.source_identity
        )
        self.assertEqual("append", action.action_type)

    def test_same_text_different_id_is_preserved_with_similarity_evidence(self) -> None:
        existing_message = self.message("existing-id", text="same body")
        existing = self.existing_from(existing_message, "production-existing")
        incoming = self.message(
            "incoming-id",
            text="same body",
            created_at=TS4,
            updated_at=TS4,
        )
        plan = self.plan(
            messages=[existing_message, incoming],
            existing=[existing],
        )
        action = next(
            item for item in plan.actions
            if item.source_identity == incoming.source_identity
        )
        self.assertEqual("append", action.action_type)
        self.assertIn("similar_content_different_identity", action.evidence_codes)
        self.assertFalse(plan.conflicts)

    def test_similar_conversation_creates_link_candidate_without_auto_merge(self) -> None:
        existing_conversation = self.conversation("existing-conversation")
        existing = self.existing_from(
            existing_conversation,
            "production-existing-conversation",
        )
        incoming = self.conversation("incoming-conversation")
        plan = self.plan(
            conversations=[existing_conversation, incoming],
            messages=[],
            existing=[existing],
        )
        incoming_actions = {
            item.action_type
            for item in plan.actions
            if item.source_identity == incoming.source_identity
        }
        self.assertEqual({"insert", "link_candidate"}, incoming_actions)
        self.assertNotIn("merge", json.dumps(plan.to_dict()))

    def test_duplicate_message_id_across_conversations_is_relationship_conflict(self) -> None:
        conversations = [
            self.conversation("conversation-1"),
            self.conversation("conversation-2"),
        ]
        messages = [
            self.message("duplicate-id", "conversation-1"),
            self.message("duplicate-id", "conversation-2"),
        ]
        plan = self.plan(conversations=conversations, messages=messages)
        self.assertIn(
            "duplicate_source_id_cross_parent",
            {item.conflict_type for item in plan.conflicts},
        )
        self.assertEqual(
            0,
            sum(item.action_type == "append" for item in plan.actions),
        )

    def test_missing_conversation_or_message_parent_is_conflict(self) -> None:
        missing_conversation = self.message(
            "orphan",
            "absent-conversation",
        )
        plan = self.plan(conversations=[], messages=[missing_conversation])
        self.assertIn("missing_conversation_parent", {item.conflict_type for item in plan.conflicts})

        child = self.message(
            "child",
            parent_source_message_id="absent-parent",
            missing_reasons={},
        )
        plan = self.plan(messages=[child])
        self.assertIn("missing_message_parent", {item.conflict_type for item in plan.conflicts})

    def test_complete_snapshot_creates_tombstone_candidate_without_delete(self) -> None:
        absent = self.message("absent-message")
        existing = self.existing_from(absent, "production-absent")
        plan = self.plan(existing=[existing], complete=True)
        self.assertIn(
            "source_record_missing_tombstone_candidate",
            {item.conflict_type for item in plan.conflicts},
        )
        tombstone = next(
            item for item in plan.actions
            if item.source_identity == absent.source_identity
        )
        self.assertEqual("tombstone_candidate", tombstone.action_type)
        self.assertNotIn("delete", json.dumps(plan.to_dict()))

    def test_incomplete_snapshot_never_infers_deletion(self) -> None:
        absent = self.message("absent-message")
        existing = self.existing_from(absent, "production-absent")
        plan = self.plan(existing=[existing], complete=False)
        self.assertNotIn(
            "source_record_missing_tombstone_candidate",
            {item.conflict_type for item in plan.conflicts},
        )


class AttachmentAndAssetPlanningTest(PlanningFixture):
    def test_same_entity_hash_and_size_shares_entity_preserving_attachment(self) -> None:
        attachment = self.attachment()
        existing_asset = ExistingRecord(
            record_kind="asset",
            source_type="chatgpt",
            source_identity="asset_" + ZERO,
            production_identity="production-asset",
            content_hash=ZERO,
            structure_hash=ONE,
            field_hashes={},
            content_sha256=ONE,
            actual_size=100,
        )
        plan = self.plan(
            attachments=[attachment],
            existing=[existing_asset],
        )
        action = next(item for item in plan.actions if item.record_kind == "attachment")
        self.assertEqual("share_entity", action.action_type)
        self.assertEqual(existing_asset.production_identity_digest, action.production_identity_digest)

    def test_same_sha_different_size_is_critical_collision(self) -> None:
        attachment = self.attachment(actual_size=101)
        existing_asset = ExistingRecord(
            record_kind="asset",
            source_type="chatgpt",
            source_identity="asset_" + ZERO,
            production_identity="production-asset",
            content_hash=ZERO,
            structure_hash=ONE,
            field_hashes={},
            content_sha256=ONE,
            actual_size=100,
        )
        plan = self.plan(attachments=[attachment], existing=[existing_asset])
        conflict = next(
            item for item in plan.conflicts
            if item.conflict_type == "hash_collision_or_corruption"
        )
        self.assertEqual("critical", conflict.risk_level)
        self.assertTrue(plan.approval_required)

    def test_same_metadata_different_content_is_asset_conflict(self) -> None:
        before = self.attachment(content_sha256=ONE)
        existing = self.existing_from(before, "production-attachment")
        incoming = self.attachment("attachment-2", content_sha256=TWO)
        plan = self.plan(attachments=[incoming], existing=[existing])
        self.assertIn(
            "asset_metadata_content_mismatch",
            {item.conflict_type for item in plan.conflicts},
        )

    def test_metadata_only_is_explicit_and_requires_approval(self) -> None:
        attachment = self.attachment(
            detected_mime=None,
            actual_size=None,
            content_sha256=None,
            entity_state="metadata_only",
            unavailable_reason="source_entity_absent",
            missing_reasons={
                "detected_mime": "source_entity_absent",
                "actual_size": "source_entity_absent",
                "content_sha256": "source_entity_absent",
            },
        )
        plan = self.plan(attachments=[attachment])
        action = next(item for item in plan.actions if item.record_kind == "attachment")
        self.assertEqual("metadata_only", action.action_type)
        self.assertTrue(action.requires_approval)
        self.assertTrue(plan.approval_required)

    def test_new_asset_entity_is_insert_without_filesystem_access(self) -> None:
        asset = make_asset_entity(
            content_sha256=ONE,
            actual_size=100,
            detected_mime="image/png",
            storage_root_id="asset_root",
            relative_entity_path="batch/entity.bin",
            first_seen_batch_id=self.batch.batch_id,
            integrity_state="verified",
            created_at=TS2,
        )
        plan = self.plan(assets=[asset])
        action = next(item for item in plan.actions if item.record_kind == "asset")
        self.assertEqual("insert", action.action_type)

    def test_asset_entity_same_sha_different_size_is_critical_conflict(self) -> None:
        asset = make_asset_entity(
            content_sha256=ONE,
            actual_size=101,
            detected_mime="image/png",
            storage_root_id="asset_root",
            relative_entity_path="batch/entity.bin",
            first_seen_batch_id=self.batch.batch_id,
            integrity_state="verified",
            created_at=TS2,
        )
        existing = ExistingRecord(
            record_kind="asset",
            source_type="chatgpt",
            source_identity="asset_" + ZERO,
            production_identity="production-asset",
            content_hash=ZERO,
            structure_hash=ONE,
            field_hashes={},
            content_sha256=ONE,
            actual_size=100,
        )
        plan = self.plan(assets=[asset], existing=[existing])
        conflict = next(
            item for item in plan.conflicts
            if item.conflict_type == "hash_collision_or_corruption"
        )
        self.assertEqual("critical", conflict.risk_level)

    def test_attachment_relationship_missing_is_conflict(self) -> None:
        attachment = self.attachment(
            source_message_id="absent-message",
        )
        plan = self.plan(attachments=[attachment])
        self.assertIn(
            "attachment_relationship_missing",
            {item.conflict_type for item in plan.conflicts},
        )


class PlanningSafetyAndPrivacyTest(PlanningFixture):
    def test_plan_contains_no_raw_ids_text_filenames_paths_or_production_ids(self) -> None:
        conversation = self.conversation("private-conversation-id", title="secret title")
        message = self.message(
            "private-message-id",
            "private-conversation-id",
            text="secret message body",
            author="private person",
            metadata={"path": r"C:\Users\ExampleUser\private.json"},
        )
        attachment = self.attachment(
            "private-attachment-id",
            source_conversation_id="private-conversation-id",
            source_message_id="private-message-id",
            original_filename="secret-original-name.png",
        )
        existing = self.existing_from(message, "private-production-message-id")
        plan = self.plan(
            conversations=[conversation],
            messages=[message],
            attachments=[attachment],
            existing=[existing],
        )
        encoded = json.dumps(plan.to_dict())
        for forbidden in (
            "private-conversation-id",
            "private-message-id",
            "private-attachment-id",
            "secret title",
            "secret message body",
            "private person",
            "secret-original-name.png",
            "private-production-message-id",
            "someone",
            r"C:\Users",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_ambiguous_existing_binding_fails_closed(self) -> None:
        message = self.message()
        existing = self.existing_from(message, "production-one")
        duplicate = replace(existing, production_identity="production-two")
        with self.assertRaisesRegex(IngestionPlanningError, "ambiguous_existing_binding"):
            self.plan(existing=[existing, duplicate])

    def test_record_provenance_mismatch_fails_closed(self) -> None:
        other_manifest = build_source_manifest(
            source_type="chatgpt",
            source_version="export_v1",
            adapter_name="fixture_adapter",
            adapter_version="adapter_v1",
            archive_sha256=THREE,
            archive_size=1,
            archive_kind="zip",
            discovered_at=TS1,
            scanned_at=TS2,
        )
        bad = replace(
            self.message().provenance,
            manifest_id=other_manifest.manifest_id,
        )
        bad_message = replace(self.message(), provenance=bad)
        with self.assertRaisesRegex(IngestionPlanningError, "record_provenance_binding_mismatch"):
            self.plan(messages=[bad_message])

    def test_batch_manifest_mismatch_fails_closed(self) -> None:
        other_manifest = build_source_manifest(
            source_type="chatgpt",
            source_version="export_v1",
            adapter_name="fixture_adapter",
            adapter_version="adapter_v2",
            archive_sha256=THREE,
            archive_size=1,
            archive_kind="zip",
            discovered_at=TS1,
            scanned_at=TS2,
        )
        with self.assertRaisesRegex(IngestionPlanningError, "batch_manifest_binding_mismatch"):
            self.plan(manifest=other_manifest)

    def test_normalization_protocol_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(IngestionPlanningError, "normalization_protocol_mismatch"):
            plan_import(
                manifest=self.manifest,
                batch=self.batch,
                conversations=[],
                messages=[],
                attachments=[],
                asset_entities=[],
                existing_records=[],
                production_fingerprint=self.database,
                asset_stage_fingerprint=self.asset_stage,
                created_at=TS4,
                normalization_protocol="v8.3-b-unknown-normalizer",
            )

    def test_record_limit_is_enforced_before_planning(self) -> None:
        records = [self.conversation(f"conversation-{index}") for index in range(3)]
        with patch("memory.ingestion_planning.MAX_RECORDS_PER_PLAN", 2):
            with self.assertRaisesRegex(IngestionPlanningError, "plan_record_limit_exceeded"):
                self.plan(conversations=records, messages=[])

    def test_conflicts_never_receive_automatic_decisions(self) -> None:
        before = self.message(text="before")
        after = self.message(text="after")
        plan = self.plan(
            messages=[after],
            existing=[self.existing_from(before, "production-message")],
        )
        conflict_ids = {item.conflict_id for item in plan.conflicts}
        self.assertTrue(conflict_ids)
        self.assertTrue(all(item.conflict_id is None for item in plan.automatic_decisions))
        self.assertFalse(
            conflict_ids & {item.conflict_id for item in plan.automatic_decisions}
        )
