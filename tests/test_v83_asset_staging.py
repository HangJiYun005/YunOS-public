from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from memory.ingestion_assets import AssetStageLimits, build_asset_stage_plan
from memory.ingestion_asset_staging import (
    AssetStagingError,
    ZipAssetSource,
    execute_asset_stage_plan,
    prepare_batch_paths,
    read_stage_ledger_events,
)
from tests.test_v83_asset_stage_plan import (
    asset_part,
    build_fixture,
)
from tests.v83_chatgpt_adapter_fixtures import PNG


class AssetStagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_root = self.root / "stage"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(
        self,
        *,
        payload: bytes = PNG,
        member_name: str = "assets/image.png",
        mime: str = "image/png",
    ):
        source, result, plan = build_fixture(
            self.root / ("fixture-" + str(len(list(self.root.glob("fixture-*"))))),
            {member_name: payload},
            parts=[asset_part("asset-1", member_name, payload, mime)],
        )
        runtime_source = ZipAssetSource(
            operation_id=plan.operations[0].operation_id,
            archive_path=source,
            member_name=member_name,
        )
        return source, result, plan, runtime_source

    def test_small_zip_entity_is_streamed_and_verified(self) -> None:
        _, _, plan, source = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual("staged", result.status)
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        self.assertEqual(PNG, target.read_bytes())

    def test_large_entity_uses_bounded_chunks(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + (b"streaming-payload-" * 200_000)
        _, _, plan, source = self.fixture(payload=payload)
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
            limits=AssetStageLimits(
                chunk_size=4096,
                max_entity_bytes=len(payload) + 1,
                max_batch_physical_bytes=len(payload) + 1,
                max_batch_logical_bytes=len(payload) + 1,
                max_compression_ratio=1_000_000,
                min_free_disk_bytes=1,
            ),
        )
        self.assertEqual("staged", result.status)
        self.assertEqual(len(payload), result.bytes_processed)

    def test_empty_entity_is_supported(self) -> None:
        source, _, plan = build_fixture(
            self.root / "empty-fixture",
            {"empty.bin": b""},
            parts=[asset_part(
                "empty-asset",
                "empty.bin",
                b"",
                "application/octet-stream",
            )],
        )
        runtime = ZipAssetSource(
            plan.operations[0].operation_id,
            source,
            "empty.bin",
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        self.assertEqual("staged", result.status)
        self.assertEqual(0, result.bytes_processed)

    def test_duplicate_entity_is_written_once(self) -> None:
        parts = [
            asset_part("asset-a", "a.png", PNG, "image/png"),
            asset_part("asset-b", "b.png", PNG, "image/png"),
        ]
        source, _, plan = build_fixture(
            self.root / "duplicate-fixture",
            {"a.png": PNG, "b.png": PNG},
            parts=parts,
        )
        self.assertEqual(1, len(plan.operations))
        runtime = ZipAssetSource(
            plan.operations[0].operation_id,
            source,
            "a.png",
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("staged", result.status)
        self.assertEqual(1, len(list(paths.verified.iterdir())))
        self.assertEqual(2, len(plan.occurrence_bindings))

    def test_size_mismatch_is_quarantined(self) -> None:
        archive, _, plan, source = self.fixture()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(source.member_name, PNG + b"x")
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("failed", result.status)
        self.assertIn("asset_size_mismatch", result.reason_codes)
        self.assertEqual(1, result.entities_quarantined)
        self.assertEqual(1, len(list(paths.quarantine.iterdir())))
        self.assertEqual([], list(paths.verified.iterdir()))

    def test_sha256_mismatch_is_quarantined(self) -> None:
        archive, _, plan, source = self.fixture()
        changed = PNG[:-1] + bytes([PNG[-1] ^ 1])
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(source.member_name, changed)
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual("failed", result.status)
        self.assertIn("asset_digest_mismatch", result.reason_codes)

    def test_mime_mismatch_is_quarantined(self) -> None:
        payload = b"plain text with a misleading extension"
        _, _, plan, source = self.fixture(
            payload=payload,
            member_name="misleading.png",
            mime="image/png",
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual("failed", result.status)
        self.assertIn("asset_mime_mismatch", result.reason_codes)

    def test_atomic_move_leaves_no_incoming_file(self) -> None:
        _, _, plan, source = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("staged", result.status)
        self.assertEqual([], list(paths.incoming.iterdir()))

    def test_existing_same_digest_is_reused_after_revalidation(self) -> None:
        _, _, plan, source = self.fixture()
        first = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        second = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual("staged", first.status)
        self.assertEqual("staged", second.status)
        self.assertEqual(1, second.entities_reused)

    def test_existing_digest_name_with_different_content_fails_closed(self) -> None:
        _, _, plan, source = self.fixture()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        target.write_bytes(b"corrupt")
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual("failed", result.status)
        self.assertIn("verified_entity_collision", result.reason_codes)

    def test_private_source_filename_never_becomes_physical_filename(self) -> None:
        private_name = "private-customer-name.png"
        _, _, plan, source = self.fixture(member_name=private_name)
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        disk_names = [item.name for item in paths.batch.rglob("*")]
        self.assertEqual("staged", result.status)
        self.assertFalse(any(private_name in name for name in disk_names))
        self.assertTrue(any(name.startswith("sha256_") for name in disk_names))

    def test_ledger_contains_only_sanitized_event_fields(self) -> None:
        _, _, plan, source = self.fixture(member_name="private-name.png")
        execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        events = read_stage_ledger_events(self.stage_root, plan.batch_id)
        encoded = str(events)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("private-name.png", encoded)
        self.assertTrue(all("event_digest" in event for event in events))

    def test_cancel_before_first_read_returns_cancelled(self) -> None:
        _, _, plan, source = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
            cancel_check=lambda: True,
        )
        self.assertEqual("cancelled", result.status)
        self.assertEqual(0, result.entities_quarantined)
        self.assertIn("cancelled", result.reason_codes)

    def test_cancel_during_stream_quarantines_partial_file(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + (b"x" * 100_000)
        _, _, plan, source = self.fixture(payload=payload)
        calls = {"count": 0}

        def cancel() -> bool:
            calls["count"] += 1
            return calls["count"] > 3

        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
            limits=AssetStageLimits(
                chunk_size=1024,
                max_compression_ratio=1_000_000,
                min_free_disk_bytes=1,
            ),
            cancel_check=cancel,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("cancelled", result.status)
        self.assertEqual(1, result.entities_quarantined)
        self.assertEqual(1, len(list(paths.quarantine.iterdir())))
        self.assertEqual([], list(paths.verified.iterdir()))

    def test_timeout_stops_operation(self) -> None:
        _, _, plan, source = self.fixture()
        with patch(
            "memory.ingestion_asset_staging.time.monotonic",
            side_effect=[0.0, 10.0],
        ):
            result = execute_asset_stage_plan(
                plan=plan,
                sources=(source,),
                stage_root=self.stage_root,
                limits=AssetStageLimits(max_operation_seconds=1),
            )
        self.assertEqual("failed", result.status)
        self.assertIn("asset_operation_timeout", result.reason_codes)

    def test_insufficient_disk_space_stops_before_write(self) -> None:
        _, _, plan, source = self.fixture()
        disk = type("Disk", (), {"free": 0})()
        with patch(
            "memory.ingestion_asset_staging.shutil.disk_usage",
            return_value=disk,
        ):
            result = execute_asset_stage_plan(
                plan=plan,
                sources=(source,),
                stage_root=self.stage_root,
            )
        self.assertEqual("failed", result.status)
        self.assertIn("insufficient_disk_space", result.reason_codes)

    def test_zip_entry_limit_is_enforced_at_execution(self) -> None:
        _, _, plan, source = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
            limits=AssetStageLimits(max_zip_entries=1),
        )
        self.assertEqual("failed", result.status)
        self.assertIn("zip_entry_limit_exceeded", result.reason_codes)

    def test_compression_ratio_limit_is_enforced_at_execution(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + (b"0" * 100_000)
        _, _, plan, source = self.fixture(payload=payload)
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
            limits=AssetStageLimits(max_compression_ratio=1),
        )
        self.assertEqual("failed", result.status)
        self.assertIn("zip_compression_ratio_exceeded", result.reason_codes)

    def test_unplanned_runtime_source_is_rejected(self) -> None:
        _, _, plan, source = self.fixture()
        extra = ZipAssetSource(
            "astageop_" + "f" * 64,
            source.archive_path,
            source.member_name,
        )
        with self.assertRaisesRegex(AssetStagingError, "unplanned_asset_source"):
            execute_asset_stage_plan(
                plan=plan,
                sources=(source, extra),
                stage_root=self.stage_root,
            )

    def test_missing_runtime_source_is_rejected(self) -> None:
        _, _, plan, _ = self.fixture()
        with self.assertRaisesRegex(
            AssetStagingError, "planned_asset_source_missing"
        ):
            execute_asset_stage_plan(
                plan=plan,
                sources=(),
                stage_root=self.stage_root,
            )

    def test_zero_operation_plan_creates_no_entity_file(self) -> None:
        parts = [{"type": "file", "id": "meta", "name": "meta.txt"}]
        _, _, plan = build_fixture(
            self.root / "metadata-fixture",
            {},
            parts=parts,
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(),
            stage_root=self.stage_root,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("staged", result.status)
        self.assertEqual([], list(paths.verified.iterdir()))

    def test_result_binds_stage_fingerprint(self) -> None:
        _, _, plan, source = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(source,),
            stage_root=self.stage_root,
        )
        self.assertEqual(plan.asset_stage_fingerprint, result.asset_stage_fingerprint)
        self.assertRegex(result.verification_digest, r"^[0-9a-f]{64}$")

    def test_dependency_dag_is_executed_in_topological_order(self) -> None:
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\xff\xd8\xffsecond"
        archive, adapter_result, _ = build_fixture(
            self.root / "dependency-fixture",
            {"a.png": first, "b.jpg": second},
            parts=[
                asset_part("asset-a", "a.png", first, "image/png"),
                asset_part("asset-b", "b.jpg", second, "image/jpeg"),
            ],
        )
        digests = sorted(
            item.content_sha256 for item in adapter_result.bundle.asset_entities
        )
        plan = build_asset_stage_plan(
            import_plan=adapter_result.import_plan,
            attachments=adapter_result.bundle.attachments,
            asset_entities=adapter_result.bundle.asset_entities,
            dependencies_by_entity={digests[1]: (digests[0],)},
        )
        members = {
            hashlib.sha256(first).hexdigest(): "a.png",
            hashlib.sha256(second).hexdigest(): "b.jpg",
        }
        sources = tuple(
            ZipAssetSource(
                operation.operation_id,
                archive,
                members[operation.entity_digest],
            )
            for operation in plan.operations
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=sources,
            stage_root=self.stage_root,
        )
        self.assertEqual("staged", result.status)


if __name__ == "__main__":
    unittest.main()
