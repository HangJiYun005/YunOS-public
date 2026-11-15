from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_adapters.chatgpt import (
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    empty_target_snapshot,
)
from memory.ingestion_core import normalized_json_digest
from memory.ingestion_assets import (
    AssetOccurrence,
    AssetPlanningError,
    AssetStageLimits,
    build_asset_stage_plan,
    occurrence_from_normalized_attachment,
)
from memory.ingestion_planning import ImportPlan, _make_action, make_asset_entity
from tests.v83_chatgpt_adapter_fixtures import (
    PNG,
    simple_conversation,
    write_zip_source,
)


def asset_part(
    identity: str,
    member_name: str,
    payload: bytes,
    mime: str,
) -> dict[str, object]:
    return {
        "type": "image_asset_pointer" if mime.startswith("image/") else "file",
        "id": identity,
        "asset_pointer": member_name,
        "name": Path(member_name).name,
        "mime_type": mime,
        "size_bytes": len(payload),
    }


def build_fixture(
    root: Path,
    assets: dict[str, bytes] | None = None,
    *,
    parts: list[object] | None = None,
    batch_mode: str = "full",
):
    root.mkdir(parents=True, exist_ok=True)
    payloads = assets if assets is not None else {"assets/image.png": PNG}
    content_parts = parts
    if content_parts is None:
        name, payload = next(iter(payloads.items()))
        content_parts = [asset_part("asset-1", name, payload, "image/png")]
    source = write_zip_source(
        root,
        [simple_conversation(assistant_parts=content_parts)],
        assets=payloads,
    )
    result = ChatGPTAdapter(
        source,
        timestamp=DETERMINISTIC_TIMESTAMP,
    ).produce_import_plan(
        target_snapshot=empty_target_snapshot(),
        batch_mode=batch_mode,
    )
    plan = build_asset_stage_plan(
        import_plan=result.import_plan,
        attachments=result.bundle.attachments,
        asset_entities=result.bundle.asset_entities,
    )
    return source, result, plan


class AssetStagePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_single_verified_entity_creates_one_operation(self) -> None:
        _, result, plan = build_fixture(self.root)
        self.assertEqual(1, len(plan.operations))
        self.assertEqual(1, plan.expected_asset_delta["entity_count"])
        self.assertEqual(len(PNG), plan.expected_asset_delta["physical_bytes"])
        self.assertEqual(result.import_plan.plan_id, plan.import_plan_id)

    def test_operation_identity_uses_verified_sha256(self) -> None:
        _, _, plan = build_fixture(self.root)
        operation = plan.operations[0]
        self.assertEqual(hashlib.sha256(PNG).hexdigest(), operation.entity_digest)
        self.assertRegex(operation.operation_id, r"^astageop_[0-9a-f]{64}$")

    def test_input_order_does_not_change_stage_plan(self) -> None:
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\xff\xd8\xffsecond"
        assets = {"a.png": first, "b.jpg": second}
        parts = [
            asset_part("asset-a", "a.png", first, "image/png"),
            asset_part("asset-b", "b.jpg", second, "image/jpeg"),
        ]
        _, result, original = build_fixture(self.root, assets, parts=parts)
        reversed_plan = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=tuple(reversed(result.bundle.attachments)),
            asset_entities=tuple(reversed(result.bundle.asset_entities)),
        )
        self.assertEqual(original.stage_plan_digest, reversed_plan.stage_plan_digest)
        self.assertEqual(
            [item.operation_id for item in original.operations],
            [item.operation_id for item in reversed_plan.operations],
        )

    def test_occurrence_order_does_not_change_entity_plan(self) -> None:
        _, result, _ = build_fixture(self.root)
        base = occurrence_from_normalized_attachment(result.bundle.attachments[0])
        second = replace(base, occurrence_id="occurrence_" + "2" * 64)
        first = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
            occurrences=(base, second),
        )
        reversed_plan = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
            occurrences=(second, base),
        )
        self.assertEqual(first.stage_plan_digest, reversed_plan.stage_plan_digest)
        self.assertEqual(1, len(first.operations))
        self.assertEqual(2, len(first.occurrence_bindings))

    def test_path_locator_change_does_not_change_logical_digest(self) -> None:
        _, result, _ = build_fixture(self.root)
        occurrence = occurrence_from_normalized_attachment(
            result.bundle.attachments[0]
        )
        first = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
            occurrences=(occurrence,),
        )
        moved = replace(occurrence, locator_fingerprint="a" * 64)
        second = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
            occurrences=(moved,),
        )
        self.assertEqual(first.stage_plan_digest, second.stage_plan_digest)

    def test_manifest_and_batch_are_bound_to_plan(self) -> None:
        source, _, first = build_fixture(self.root)
        result = ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        ).produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="incremental",
        )
        second = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
        )
        self.assertNotEqual(first.batch_id, second.batch_id)
        self.assertNotEqual(first.stage_plan_digest, second.stage_plan_digest)
        self.assertEqual(first.manifest_id, second.manifest_id)

    def test_entity_digest_change_changes_plan(self) -> None:
        _, _, first = build_fixture(self.root / "one")
        other = b"\x89PNG\r\n\x1a\nother"
        _, _, second = build_fixture(
            self.root / "two",
            {"assets/image.png": other},
        )
        self.assertNotEqual(
            first.operations[0].entity_digest,
            second.operations[0].entity_digest,
        )
        self.assertNotEqual(first.stage_plan_digest, second.stage_plan_digest)

    def test_repeated_occurrences_share_one_physical_entity(self) -> None:
        _, result, _ = build_fixture(self.root)
        base = occurrence_from_normalized_attachment(result.bundle.attachments[0])
        occurrences = tuple(
            replace(base, occurrence_id="occurrence_" + f"{index:064x}")
            for index in range(1, 4)
        )
        plan = build_asset_stage_plan(
            import_plan=result.import_plan,
            attachments=result.bundle.attachments,
            asset_entities=result.bundle.asset_entities,
            occurrences=occurrences,
        )
        self.assertEqual(1, plan.expected_asset_delta["entity_count"])
        self.assertEqual(3, plan.expected_asset_delta["occurrence_count"])
        self.assertEqual(len(PNG), plan.expected_asset_delta["physical_bytes"])
        self.assertEqual(len(PNG) * 3, plan.expected_asset_delta["logical_bytes"])

    def test_two_logical_attachments_can_share_one_entity_digest(self) -> None:
        parts = [
            asset_part("asset-a", "a.png", PNG, "image/png"),
            asset_part("asset-b", "b.png", PNG, "image/png"),
        ]
        _, _, plan = build_fixture(
            self.root,
            {"a.png": PNG, "b.png": PNG},
            parts=parts,
        )
        self.assertEqual(1, len(plan.operations))
        self.assertEqual(2, len(plan.occurrence_bindings))

    def test_metadata_only_does_not_create_stage_operation(self) -> None:
        parts = [{"type": "file", "id": "meta", "name": "note.txt"}]
        _, result, plan = build_fixture(self.root, {}, parts=parts)
        self.assertEqual("metadata_only", result.bundle.attachments[0].entity_state)
        self.assertEqual(0, len(plan.operations))

    def test_missing_does_not_create_stage_operation(self) -> None:
        parts = [{
            "type": "file",
            "id": "missing",
            "asset_pointer": "missing.bin",
            "name": "missing.bin",
        }]
        _, result, plan = build_fixture(self.root, {}, parts=parts)
        self.assertEqual("entity_missing", result.bundle.attachments[0].entity_state)
        self.assertEqual(0, len(plan.operations))

    def test_external_url_blocked_does_not_create_stage_operation(self) -> None:
        parts = [{
            "type": "file",
            "id": "external",
            "url": "https://example.invalid/private",
            "name": "remote.bin",
        }]
        _, result, plan = build_fixture(self.root, {}, parts=parts)
        self.assertEqual(
            "entity_blocked",
            result.bundle.attachments[0].entity_state,
        )
        self.assertEqual(0, len(plan.operations))

    def test_unplanned_entity_is_rejected(self) -> None:
        _, result, _ = build_fixture(self.root)
        extra = make_asset_entity(
            content_sha256="a" * 64,
            actual_size=1,
            detected_mime="application/octet-stream",
            storage_root_id="stage",
            relative_entity_path="sha256_aa",
            first_seen_batch_id=result.import_plan.batch_id,
            integrity_state="verified",
            created_at=DETERMINISTIC_TIMESTAMP,
        )
        with self.assertRaisesRegex(AssetPlanningError, "unplanned_normalized_entity"):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=(*result.bundle.asset_entities, extra),
            )

    def test_same_digest_with_conflicting_size_fails_closed(self) -> None:
        _, result, _ = build_fixture(self.root)
        original = result.bundle.asset_entities[0]
        conflicting = make_asset_entity(
            content_sha256=original.content_sha256,
            actual_size=original.actual_size + 1,
            detected_mime=original.detected_mime,
            storage_root_id="stage",
            relative_entity_path="sha256_conflict",
            first_seen_batch_id=result.import_plan.batch_id,
            integrity_state="verified",
            created_at=DETERMINISTIC_TIMESTAMP,
        )
        extra_action = _make_action(
            action_type="insert",
            record_kind="asset",
            source_identity=conflicting.asset_entity_id,
        )
        actions = tuple((*result.import_plan.actions, extra_action))
        identity = {
            **result.import_plan.identity_payload(),
            "actions": [
                item.to_dict()
                for item in sorted(actions, key=lambda item: item.action_id)
            ],
        }
        changed_plan = ImportPlan(
            plan_id="plan_" + normalized_json_digest(identity),
            batch_id=result.import_plan.batch_id,
            manifest_id=result.import_plan.manifest_id,
            archive_sha256=result.import_plan.archive_sha256,
            adapter_version=result.import_plan.adapter_version,
            normalization_protocol=result.import_plan.normalization_protocol,
            policy_version=result.import_plan.policy_version,
            production_snapshot_digest=(
                result.import_plan.production_snapshot_digest
            ),
            normalized_records_digest=(
                result.import_plan.normalized_records_digest
            ),
            asset_inventory_digest=result.import_plan.asset_inventory_digest,
            actions=actions,
            conflicts=result.import_plan.conflicts,
            automatic_decisions=result.import_plan.automatic_decisions,
            counts=result.import_plan.counts,
            approval_required=result.import_plan.approval_required,
            commit_prerequisites=result.import_plan.commit_prerequisites,
            source_snapshot_complete=result.import_plan.source_snapshot_complete,
            created_at=result.import_plan.created_at,
        )
        with self.assertRaisesRegex(AssetPlanningError, "entity_digest_conflict"):
            build_asset_stage_plan(
                import_plan=changed_plan,
                attachments=result.bundle.attachments,
                asset_entities=(original, conflicting),
            )

    def test_missing_normalized_entity_is_rejected(self) -> None:
        _, result, _ = build_fixture(self.root)
        with self.assertRaisesRegex(AssetPlanningError, "normalized_entity_missing"):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=(),
            )

    def test_single_entity_limit_is_checked_during_planning(self) -> None:
        _, result, _ = build_fixture(self.root)
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_entity_size_limit_exceeded"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                limits=AssetStageLimits(max_entity_bytes=1),
            )

    def test_physical_batch_limit_is_checked_during_planning(self) -> None:
        _, result, _ = build_fixture(self.root)
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_batch_physical_limit_exceeded"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                limits=AssetStageLimits(max_batch_physical_bytes=1),
            )

    def test_logical_batch_limit_counts_all_occurrences(self) -> None:
        _, result, _ = build_fixture(self.root)
        base = occurrence_from_normalized_attachment(result.bundle.attachments[0])
        second = replace(base, occurrence_id="occurrence_" + "2" * 64)
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_batch_logical_limit_exceeded"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                occurrences=(base, second),
                limits=AssetStageLimits(
                    max_batch_logical_bytes=len(PNG),
                ),
            )

    def test_occurrence_limit_is_checked(self) -> None:
        _, result, _ = build_fixture(self.root)
        base = occurrence_from_normalized_attachment(result.bundle.attachments[0])
        second = replace(base, occurrence_id="occurrence_" + "2" * 64)
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_occurrence_limit_exceeded"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                occurrences=(base, second),
                limits=AssetStageLimits(max_occurrences=1),
            )

    def test_entity_count_limit_is_checked(self) -> None:
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\xff\xd8\xffsecond"
        _, result, _ = build_fixture(
            self.root,
            {"a.png": first, "b.jpg": second},
            parts=[
                asset_part("asset-a", "a.png", first, "image/png"),
                asset_part("asset-b", "b.jpg", second, "image/jpeg"),
            ],
        )
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_entity_limit_exceeded"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                limits=AssetStageLimits(max_entities=1),
            )

    def test_dependency_cycle_is_rejected(self) -> None:
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\xff\xd8\xffsecond"
        assets = {"a.png": first, "b.jpg": second}
        parts = [
            asset_part("asset-a", "a.png", first, "image/png"),
            asset_part("asset-b", "b.jpg", second, "image/jpeg"),
        ]
        _, result, _ = build_fixture(self.root, assets, parts=parts)
        digests = [item.content_sha256 for item in result.bundle.asset_entities]
        with self.assertRaisesRegex(
            AssetPlanningError, "asset_stage_dependency_cycle"
        ):
            build_asset_stage_plan(
                import_plan=result.import_plan,
                attachments=result.bundle.attachments,
                asset_entities=result.bundle.asset_entities,
                dependencies_by_entity={
                    digests[0]: (digests[1],),
                    digests[1]: (digests[0],),
                },
            )

    def test_occurrence_descriptor_contains_no_original_filename(self) -> None:
        _, _, plan = build_fixture(self.root)
        serialized = str(plan.to_dict())
        self.assertNotIn("image.png", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_stage_fingerprint_matches_physical_entities(self) -> None:
        _, _, plan = build_fixture(self.root)
        self.assertEqual(1, plan.asset_stage_fingerprint.item_count)
        self.assertEqual(len(PNG), plan.asset_stage_fingerprint.total_size_bytes)


if __name__ == "__main__":
    unittest.main()
