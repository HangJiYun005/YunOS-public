from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_core import (
    AssetStageFingerprint,
    DatabaseFingerprint,
    build_source_manifest,
    create_import_batch,
)
from memory.ingestion_planning import (
    NORMALIZED_MODEL_TYPES,
    AssetEntity,
    ImportConflict,
    ImportDecision,
    ImportVerification,
    IngestionPlanningError,
    NormalizedAttachment,
    NormalizedConversation,
    NormalizedMessage,
    make_asset_entity,
    make_import_conflict,
    make_import_decision,
    make_import_verification,
    make_provenance,
    make_record_locator,
    make_source_identity,
    normalize_attachment,
    normalize_conversation,
    normalize_message,
)


TS1 = "2026-07-26T00:00:00.000000Z"
TS2 = "2026-07-26T00:01:00.000000Z"
TS3 = "2026-07-26T00:02:00.000000Z"
ZERO = "0" * 64
ONE = "1" * 64
TWO = "2" * 64


def fixture_manifest(source_type: str = "chatgpt"):
    return build_source_manifest(
        source_type=source_type,
        source_version="export_v1",
        adapter_name="fixture_adapter",
        adapter_version="adapter_v1",
        archive_sha256=hashlib.sha256(b"archive").hexdigest(),
        archive_size=7,
        archive_kind="zip",
        discovered_at=TS1,
        scanned_at=TS2,
    )


def fixture_batch(manifest=None):
    item = manifest or fixture_manifest()
    return create_import_batch(
        item,
        batch_mode="full",
        plan={"phase": "normalization_fixture"},
        timestamp=TS1,
    )


def provenance(kind: str, source_id: str, manifest=None, batch=None, ordinal: int = 0):
    item = manifest or fixture_manifest()
    work = batch or fixture_batch(item)
    return make_provenance(
        record_kind=kind,
        source_type=item.source_type,
        manifest_id=item.manifest_id,
        source_record_id=source_id,
        batch_id=work.batch_id,
        adapter_version=item.adapter_version,
        parsed_at=TS2,
        locator=make_record_locator(
            "json_record",
            ordinal,
            private_locator_material={"private_member": "private/export.json", "id": source_id},
        ),
    )


class ProvenanceAndIdentityTest(unittest.TestCase):
    def test_nine_model_contract_is_complete(self) -> None:
        self.assertEqual(9, len(NORMALIZED_MODEL_TYPES))
        self.assertIn(NormalizedConversation, NORMALIZED_MODEL_TYPES)
        self.assertIn(NormalizedMessage, NORMALIZED_MODEL_TYPES)
        self.assertIn(NormalizedAttachment, NORMALIZED_MODEL_TYPES)
        self.assertIn(AssetEntity, NORMALIZED_MODEL_TYPES)
        self.assertIn(ImportConflict, NORMALIZED_MODEL_TYPES)
        self.assertIn(ImportDecision, NORMALIZED_MODEL_TYPES)
        self.assertIn(ImportVerification, NORMALIZED_MODEL_TYPES)

    def test_source_identity_is_stable_across_manifests_but_separates_sources(self) -> None:
        first = make_source_identity("message", "chatgpt", "message-1")
        second = make_source_identity("message", "chatgpt", "message-1")
        other = make_source_identity("message", "deepseek", "message-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_private_locator_material_is_hashed_not_persisted(self) -> None:
        locator = make_record_locator(
            "json_record",
            4,
            private_locator_material={
                "path": r"C:\Users\ExampleUser\private-export.json",
                "title": "private conversation",
            },
        )
        encoded = json.dumps(locator.to_dict())
        self.assertNotIn("someone", encoded)
        self.assertNotIn("private conversation", encoded)
        self.assertEqual(64, len(locator.locator_fingerprint))

    def test_provenance_repr_and_descriptor_hide_raw_source_id(self) -> None:
        value = provenance("message", "private-source-message-id")
        self.assertNotIn("private-source-message-id", repr(value))
        self.assertNotIn(
            "private-source-message-id",
            json.dumps(value.plan_descriptor()),
        )
        self.assertIn("source_message_", value.source_identity)


class NormalizedConversationTest(unittest.TestCase):
    def test_conversation_identity_and_hash_are_deterministic(self) -> None:
        source = provenance("conversation", "conversation-1")
        first = normalize_conversation(
            source,
            title="e\u0301 private title",
            created_at=TS1,
            updated_at=TS2,
            source_status={"archived": False, "flags": ["a", "b"]},
            message_source_ids=["m1", "m2"],
        )
        second = normalize_conversation(
            source,
            title="\u00e9 private title",
            created_at=TS1,
            updated_at=TS2,
            source_status={"flags": ["a", "b"], "archived": False},
            message_source_ids=["m1", "m2"],
        )
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.message_identity_digest, second.message_identity_digest)

    def test_message_order_changes_structure_not_content(self) -> None:
        source = provenance("conversation", "conversation-1")
        one = normalize_conversation(
            source,
            title="title",
            created_at=TS1,
            updated_at=TS2,
            source_status={},
            message_source_ids=["m1", "m2"],
        )
        two = normalize_conversation(
            source,
            title="title",
            created_at=TS1,
            updated_at=TS2,
            source_status={},
            message_source_ids=["m2", "m1"],
        )
        self.assertEqual(one.content_hash, two.content_hash)
        self.assertNotEqual(one.message_identity_digest, two.message_identity_digest)

    def test_missing_times_require_explicit_reasons(self) -> None:
        source = provenance("conversation", "conversation-1")
        with self.assertRaisesRegex(IngestionPlanningError, "missing_created_at_reason"):
            normalize_conversation(
                source,
                title="title",
                created_at=None,
                updated_at=TS2,
                source_status={},
                message_source_ids=[],
            )
        result = normalize_conversation(
            source,
            title="title",
            created_at=None,
            updated_at=None,
            source_status={},
            message_source_ids=[],
            missing_reasons={
                "created_at": "source_value_absent",
                "updated_at": "source_value_absent",
            },
        )
        self.assertIsNone(result.created_at)
        self.assertEqual("source_value_absent", result.missing_reasons["created_at"])

    def test_plan_descriptor_hides_title_and_production_id(self) -> None:
        result = normalize_conversation(
            provenance("conversation", "private-conversation-id"),
            title="private title body",
            created_at=TS1,
            updated_at=TS2,
            source_status={},
            message_source_ids=[],
            production_conversation_id="production-private-id",
        )
        encoded = json.dumps(result.plan_descriptor())
        self.assertNotIn("private title body", encoded)
        self.assertNotIn("private-conversation-id", encoded)
        self.assertNotIn("production-private-id", encoded)

    def test_frozen_conversation_and_nested_metadata_cannot_mutate(self) -> None:
        result = normalize_conversation(
            provenance("conversation", "conversation-1"),
            title="title",
            created_at=TS1,
            updated_at=TS2,
            source_status={"nested": {"enabled": True}},
            message_source_ids=[],
        )
        with self.assertRaises(FrozenInstanceError):
            result.title = "changed"
        with self.assertRaises(TypeError):
            result.source_status["nested"]["enabled"] = False


class NormalizedMessageTest(unittest.TestCase):
    def _message(self, **overrides):
        values = {
            "source_conversation_id": "conversation-1",
            "source_node_id": "node-1",
            "role": "user",
            "author": "private author",
            "content_type": "text",
            "text": "private message body",
            "status": "finished",
            "weight": 1.0,
            "created_at": TS1,
            "updated_at": TS2,
            "parent_source_message_id": None,
            "child_source_message_ids": (),
            "metadata": {"model": "fixture", "nested": {"x": 1}},
            "missing_reasons": {"parent_source_message_id": "root_message"},
        }
        values.update(overrides)
        return normalize_message(provenance("message", "message-1"), **values)

    def test_text_change_changes_content_hash_not_record_identity(self) -> None:
        first = self._message(text="one")
        second = self._message(text="two")
        self.assertEqual(first.record_id, second.record_id)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_parent_change_changes_structure_hash_not_content_hash(self) -> None:
        first = self._message()
        second = self._message(
            parent_source_message_id="message-parent",
            missing_reasons={},
        )
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.structure_hash, second.structure_hash)

    def test_descriptor_hides_text_author_raw_ids_and_metadata_values(self) -> None:
        value = self._message(
            text="secret message text",
            author="private person",
            metadata={"private_path": r"C:\private\source.json"},
            production_message_id="production-private-message",
        )
        encoded = json.dumps(value.plan_descriptor())
        for forbidden in (
            "secret message text",
            "private person",
            "message-1",
            "conversation-1",
            "production-private-message",
            r"C:\private",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_non_finite_weight_and_oversized_metadata_fail_closed(self) -> None:
        with self.assertRaisesRegex(IngestionPlanningError, "invalid_message_weight"):
            self._message(weight=math.nan)
        with self.assertRaisesRegex(IngestionPlanningError, "metadata_size_limit_exceeded"):
            self._message(metadata={"payload": "x" * 1_000_001})

    def test_optional_node_and_times_require_reasons(self) -> None:
        with self.assertRaisesRegex(IngestionPlanningError, "missing_source_node_id_reason"):
            self._message(source_node_id=None)
        result = self._message(
            source_node_id=None,
            created_at=None,
            updated_at=None,
            missing_reasons={
                "source_node_id": "source_value_absent",
                "created_at": "source_value_absent",
                "updated_at": "source_value_absent",
                "parent_source_message_id": "root_message",
            },
        )
        self.assertIsNone(result.source_node_id)


class NormalizedAttachmentAndAssetTest(unittest.TestCase):
    def _attachment(self, **overrides):
        values = {
            "source_conversation_id": "conversation-1",
            "source_message_id": "message-1",
            "original_filename": "private-original-name.png",
            "safe_display_filename": "image.png",
            "declared_mime": "image/png",
            "detected_mime": "image/png",
            "declared_size": 100,
            "actual_size": 100,
            "content_sha256": ONE,
            "archive_member_locator": make_record_locator(
                "archive_member",
                1,
                private_locator_material="private/assets/original.png",
            ),
            "entity_state": "entity_available",
            "unavailable_reason": None,
            "missing_reasons": {},
        }
        values.update(overrides)
        return normalize_attachment(
            provenance("attachment", "attachment-1"),
            **values,
        )

    def test_available_attachment_requires_hash_size_and_mime(self) -> None:
        self.assertEqual("entity_available", self._attachment().entity_state)
        with self.assertRaisesRegex(IngestionPlanningError, "available_entity_evidence_incomplete"):
            self._attachment(content_sha256=None)

    def test_metadata_only_requires_reason_and_explicit_missing_reasons(self) -> None:
        with self.assertRaisesRegex(IngestionPlanningError, "unavailable_entity_reason_missing"):
            self._attachment(
                entity_state="metadata_only",
                content_sha256=None,
                actual_size=None,
                detected_mime=None,
                unavailable_reason=None,
                missing_reasons={
                    "content_sha256": "source_entity_absent",
                    "actual_size": "source_entity_absent",
                    "detected_mime": "source_entity_absent",
                },
            )
        result = self._attachment(
            entity_state="metadata_only",
            content_sha256=None,
            actual_size=None,
            detected_mime=None,
            unavailable_reason="source_entity_absent",
            missing_reasons={
                "content_sha256": "source_entity_absent",
                "actual_size": "source_entity_absent",
                "detected_mime": "source_entity_absent",
            },
        )
        self.assertEqual("source_entity_absent", result.unavailable_reason)

    def test_attachment_descriptor_hides_filenames_and_raw_relationship_ids(self) -> None:
        encoded = json.dumps(self._attachment().plan_descriptor())
        for forbidden in (
            "private-original-name.png",
            "attachment-1",
            "conversation-1",
            "message-1",
            "private/assets",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_asset_identity_is_content_based_not_path_based(self) -> None:
        first = make_asset_entity(
            content_sha256=ONE,
            actual_size=100,
            detected_mime="image/png",
            storage_root_id="asset_root",
            relative_entity_path="batch-one/entity.bin",
            first_seen_batch_id=fixture_batch().batch_id,
            integrity_state="verified",
            created_at=TS2,
        )
        second = make_asset_entity(
            content_sha256=ONE,
            actual_size=100,
            detected_mime="image/png",
            storage_root_id="other_root",
            relative_entity_path="batch-two/entity.bin",
            first_seen_batch_id=first.first_seen_batch_id,
            integrity_state="verified",
            created_at=TS3,
        )
        self.assertEqual(first.asset_entity_id, second.asset_entity_id)

    def test_asset_rejects_absolute_traversal_and_device_paths(self) -> None:
        for path in ("/absolute/a.bin", "../escape.bin", "C:/device.bin"):
            with self.subTest(path=path), self.assertRaisesRegex(
                IngestionPlanningError, "invalid_relative_entity_path"
            ):
                make_asset_entity(
                    content_sha256=ONE,
                    actual_size=1,
                    detected_mime="application/octet-stream",
                    storage_root_id="asset_root",
                    relative_entity_path=path,
                    first_seen_batch_id=fixture_batch().batch_id,
                    integrity_state="verified",
                    created_at=TS2,
                )


class ConflictDecisionVerificationTest(unittest.TestCase):
    def test_conflict_identity_is_order_stable(self) -> None:
        values = dict(
            batch_id=fixture_batch().batch_id,
            record_kind="message",
            source_identity=make_source_identity("message", "chatgpt", "message-1"),
            production_identity_digest=ZERO,
            conflict_type="message_content_changed",
            risk_level="high",
        )
        first = make_import_conflict(
            **values,
            evidence_hashes=(ONE, TWO),
            allowed_decisions=("manual_review", "keep_existing"),
        )
        second = make_import_conflict(
            **values,
            evidence_hashes=(TWO, ONE),
            allowed_decisions=("keep_existing", "manual_review"),
        )
        self.assertEqual(first.conflict_id, second.conflict_id)

    def test_decision_identity_excludes_time_but_binds_policy_and_evidence(self) -> None:
        values = dict(
            conflict_id=None,
            plan_scope="action_" + ZERO,
            decision="insert",
            actor_kind="policy_engine",
            policy_version="policy_v1",
            evidence_hash=ONE,
        )
        first = make_import_decision(**values, decided_at=TS1)
        second = make_import_decision(**values, decided_at=TS2)
        changed = make_import_decision(
            **{**values, "evidence_hash": TWO},
            decided_at=TS1,
        )
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertNotEqual(first.decision_id, changed.decision_id)

    def test_verification_binds_snapshots_counts_checks_and_result(self) -> None:
        database = DatabaseFingerprint(ZERO, 10, ONE, TWO)
        assets = AssetStageFingerprint(1, 5, ONE, TWO)
        first = make_import_verification(
            batch_id=fixture_batch().batch_id,
            phase="precommit",
            database_snapshot=database,
            asset_snapshot=assets,
            expected_counts={"messages": 2},
            actual_counts={"messages": 2},
            integrity_check="ok",
            foreign_key_check="ok",
            index_checks={"fts": "ok"},
            source_binding_checks={"messages": "ok"},
            completed_state_check="not_applicable",
            result="passed",
            verified_at=TS2,
        )
        second = replace(first, verified_at=TS3)
        self.assertEqual(first.verification_id, second.verification_id)
        self.assertIn("database_snapshot", first.to_dict())

    def test_invalid_conflict_decision_and_verification_values_fail_closed(self) -> None:
        with self.assertRaises(IngestionPlanningError):
            make_import_conflict(
                batch_id=fixture_batch().batch_id,
                record_kind="message",
                source_identity=make_source_identity("message", "chatgpt", "m1"),
                production_identity_digest=None,
                conflict_type="content_changed",
                evidence_hashes=(ONE,),
                risk_level="silent",
                allowed_decisions=("manual_review",),
            )
        with self.assertRaises(IngestionPlanningError):
            make_import_decision(
                conflict_id=None,
                plan_scope="action_" + ZERO,
                decision="silent_overwrite",
                actor_kind="policy_engine",
                policy_version="policy_v1",
                evidence_hash=ONE,
                decided_at=TS1,
            )
        with self.assertRaises(IngestionPlanningError):
            make_import_verification(
                batch_id=fixture_batch().batch_id,
                phase="unknown",
                database_snapshot=None,
                asset_snapshot=None,
                expected_counts={},
                actual_counts={},
                integrity_check="ok",
                foreign_key_check="ok",
                index_checks={},
                source_binding_checks={},
                completed_state_check="ok",
                result="passed",
                verified_at=TS1,
            )
