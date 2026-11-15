from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from memory.ingestion_asset_staging import (
    AssetStagingError,
    AssetStagingInterrupted,
    ZipAssetSource,
    execute_asset_stage_plan,
    inspect_asset_stage_batch,
    prepare_batch_paths,
    recover_asset_stage_batch,
    read_stage_ledger_events,
    rollback_asset_stage_batch,
)
from tests.test_v83_asset_stage_plan import build_fixture
from tests.v83_chatgpt_adapter_fixtures import PNG


class AssetStagingRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_root = self.root / "stage"
        self.fixture_index = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(self, *, batch_mode: str = "full"):
        self.fixture_index += 1
        source, _, plan = build_fixture(
            self.root / f"fixture-{self.fixture_index}",
            batch_mode=batch_mode,
        )
        runtime = ZipAssetSource(
            plan.operations[0].operation_id,
            source,
            "assets/image.png",
        )
        return source, plan, runtime

    def stage(self):
        _, plan, runtime = self.fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        return plan, runtime, result

    def test_completed_batch_inspection_revalidates_verified_bytes(self) -> None:
        plan, _, result = self.stage()
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("staged", result.status)
        self.assertEqual("staged", report.status)
        self.assertEqual(1, report.verified_entities)

    def test_incoming_residual_is_never_considered_available(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        (paths.incoming / "unknown.part").write_bytes(b"partial")
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertEqual(1, report.incoming_residuals)
        self.assertIn("incoming_residual", report.warning_codes)

    def test_ledger_without_verified_file_requires_recovery(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        (paths.verified / ("sha256_" + plan.operations[0].entity_digest)).unlink()
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertEqual(1, report.missing_entities)

    def test_verified_file_without_ledger_requires_recovery(self) -> None:
        _, plan, _ = self.fixture()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        target.write_bytes(PNG)
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertEqual(1, report.verified_entities)
        self.assertTrue(report.ledger_valid)

    def test_changed_verified_digest_requires_recovery(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        target.write_bytes(b"tampered")
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertIn("verified_entity_mismatch", report.warning_codes)

    def test_crash_after_incoming_close_leaves_nonusable_residual(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_incoming_close":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted) as captured:
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertIsNotNone(captured.exception.lease_token)
        self.assertEqual("recovery_required", report.status)
        self.assertEqual(1, report.incoming_residuals)
        self.assertEqual([], list(paths.verified.iterdir()))

    def test_crash_after_atomic_move_requires_ledger_reconciliation(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_atomic_move":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted):
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertEqual(1, report.verified_entities)
        self.assertTrue(report.lease_present)

    def test_exact_lease_authority_can_complete_verified_crash_recovery(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_atomic_move":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted) as captured:
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        token = captured.exception.lease_token
        self.assertIsNotNone(token)
        report = recover_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
            lease_token=token or "",
        )
        self.assertEqual("staged", report.status)
        self.assertFalse(report.lease_present)

    def test_wrong_lease_authority_cannot_take_over_crashed_batch(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_incoming_close":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted):
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        with self.assertRaisesRegex(AssetStagingError, "lease_authority_mismatch"):
            recover_asset_stage_batch(
                stage_root=self.stage_root,
                batch_id=plan.batch_id,
                plan=plan,
                lease_token="lease_" + "0" * 32,
            )

    def test_lease_is_not_auto_taken_over_during_inspection(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_incoming_close":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted):
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        first = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        second = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertTrue(first.lease_present)
        self.assertTrue(second.lease_present)
        self.assertIn("lease_present_no_auto_takeover", second.warning_codes)

    def test_corrupt_ledger_chain_is_detected(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        ledger = paths.ledger / "events.jsonl"
        events = ledger.read_text(encoding="utf-8").splitlines()
        changed = json.loads(events[0])
        changed["bytes_processed"] = 999
        events[0] = json.dumps(changed, sort_keys=True)
        ledger.write_text("\n".join(events) + "\n", encoding="utf-8")
        report = inspect_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("recovery_required", report.status)
        self.assertFalse(report.ledger_valid)

    def test_ledger_write_failure_marks_recovery_required(self) -> None:
        _, plan, runtime = self.fixture()

        def fail(point: str) -> None:
            if point == "before_ledger_write":
                raise AssetStagingError("stage_ledger_write_failed")

        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
            fault_injector=fail,
        )
        self.assertEqual("recovery_required", result.status)
        self.assertIn("stage_ledger_write_failed", result.reason_codes)

    def test_atomic_move_failure_never_creates_verified_entity(self) -> None:
        _, plan, runtime = self.fixture()

        def fail(point: str) -> None:
            if point == "before_atomic_move":
                raise OSError("simulated")

        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
            fault_injector=fail,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("failed", result.status)
        self.assertEqual([], list(paths.verified.iterdir()))
        self.assertEqual(1, len(list(paths.quarantine.iterdir())))

    def test_concurrent_writer_is_rejected_by_batch_lease(self) -> None:
        _, plan, runtime = self.fixture()
        entered = threading.Event()
        release = threading.Event()
        first_result: list[object] = []

        def block(point: str) -> None:
            if point == "during_stream_write":
                entered.set()
                release.wait(5)

        def run_first() -> None:
            first_result.append(execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=block,
            ))

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaisesRegex(AssetStagingError, "batch_lease_conflict"):
                execute_asset_stage_plan(
                    plan=plan,
                    sources=(runtime,),
                    stage_root=self.stage_root,
                )
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual("staged", first_result[0].status)

    def test_safe_rollback_removes_only_owned_verified_entity(self) -> None:
        plan, _, _ = self.stage()
        result = rollback_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        self.assertEqual("rolled_back", result.status)
        self.assertEqual([], list(paths.verified.iterdir()))
        self.assertEqual("rolled_back", read_stage_ledger_events(paths)[-1]["next_state"])

    def test_rollback_preserves_unknown_file(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        unknown = paths.verified / "user-file"
        unknown.write_text("preserve", encoding="utf-8")
        result = rollback_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual("preserve", unknown.read_text(encoding="utf-8"))
        self.assertIn("unknown_file_preserved", result.warning_codes)

    def test_rollback_preserves_tampered_expected_filename(self) -> None:
        plan, _, _ = self.stage()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        target.write_bytes(b"user-replacement")
        result = rollback_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
        )
        self.assertEqual(b"user-replacement", target.read_bytes())
        self.assertIn("unknown_file_preserved", result.warning_codes)

    def test_rollback_of_one_batch_does_not_touch_other_batch(self) -> None:
        source_one, plan_one, runtime_one = self.fixture(batch_mode="full")
        source_two, plan_two, runtime_two = self.fixture(batch_mode="incremental")
        self.assertEqual(
            plan_one.operations[0].entity_digest,
            plan_two.operations[0].entity_digest,
        )
        execute_asset_stage_plan(
            plan=plan_one,
            sources=(runtime_one,),
            stage_root=self.stage_root,
        )
        execute_asset_stage_plan(
            plan=plan_two,
            sources=(runtime_two,),
            stage_root=self.stage_root,
        )
        second_paths = prepare_batch_paths(self.stage_root, plan_two.batch_id)
        second_target = second_paths.verified / (
            "sha256_" + plan_two.operations[0].entity_digest
        )
        rollback_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan_one.batch_id,
            plan=plan_one,
        )
        self.assertTrue(source_one.exists())
        self.assertTrue(source_two.exists())
        self.assertEqual(PNG, second_target.read_bytes())

    def test_incomplete_crash_recovery_quarantines_owned_incoming_only(self) -> None:
        _, plan, runtime = self.fixture()

        def crash(point: str) -> None:
            if point == "after_incoming_close":
                raise AssetStagingInterrupted(point)

        with self.assertRaises(AssetStagingInterrupted) as captured:
            execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
                fault_injector=crash,
            )
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        unknown = paths.incoming / "user-file"
        unknown.write_bytes(b"preserve")
        report = recover_asset_stage_batch(
            stage_root=self.stage_root,
            batch_id=plan.batch_id,
            plan=plan,
            lease_token=captured.exception.lease_token or "",
        )
        self.assertEqual("recovery_required", report.status)
        self.assertTrue(unknown.exists())
        self.assertTrue(any(paths.quarantine.iterdir()))


if __name__ == "__main__":
    unittest.main()
