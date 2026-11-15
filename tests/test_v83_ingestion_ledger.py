from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_core import (
    BatchState,
    bind_batch_approval,
    build_source_manifest,
    create_import_batch,
    transition_batch,
)
from memory.ingestion_ledger import (
    IngestionLedgerError,
    SQLiteIngestionLedger,
    validate_prototype_ledger_path,
)


TS1 = "2026-07-26T00:00:00.000000Z"
TS2 = "2026-07-26T00:01:00.000000Z"
TS3 = "2026-07-26T00:02:00.000000Z"
TS4 = "2026-07-26T00:03:00.000000Z"


def make_manifest(content: bytes = b"archive", *, adapter_version: str = "adapter_v1"):
    return build_source_manifest(
        source_type="chatgpt",
        source_version="export_v1",
        adapter_name="fixture_adapter",
        adapter_version=adapter_version,
        archive_sha256=hashlib.sha256(content).hexdigest(),
        archive_size=len(content),
        archive_kind="zip",
        discovered_at=TS1,
        scanned_at=TS2,
    )


def make_batch(item, *, identifier: str | None = None):
    kwargs = {}
    if identifier is not None:
        kwargs["batch_id_factory"] = lambda: identifier
    return create_import_batch(
        item,
        batch_mode="full",
        plan={"action": "fixture_insert", "count": 2},
        timestamp=TS1,
        **kwargs,
    )


def process_lease_attempt(
    ledger_path: str,
    batch_id: str,
    manifest_id: str,
    owner: str,
    start_event,
    result_queue,
) -> None:
    start_event.wait(10)
    ledger = SQLiteIngestionLedger(ledger_path)
    decision = ledger.acquire_writer_lease(
        batch_id=batch_id,
        manifest_id=manifest_id,
        target_id="prototype_target",
        owner=owner,
        created_at=TS2,
        expires_at=TS4,
    )
    result_queue.put(decision.status)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "ledger.sqlite"
        self.ledger = SQLiteIngestionLedger(self.path)
        self.ledger.initialize()
        self.manifest = make_manifest()
        self.batch = make_batch(self.manifest)
        self.ledger.add_manifest(self.manifest)
        self.ledger.add_batch(self.batch)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _append(self, current, next_state, prior="", **kwargs):
        updated, event = transition_batch(
            current,
            next_state,
            actor_kind="test_actor",
            reason_code="test_progress",
            occurred_at=kwargs.pop("occurred_at", TS2),
            previous_event_digest=prior,
            **kwargs,
        )
        self.ledger.append_transition(updated, event)
        return updated, event

    def _to_committing(self):
        current = self.batch
        prior = ""
        for state in (
            BatchState.VALIDATING,
            BatchState.PLANNED,
            BatchState.AWAITING_APPROVAL,
        ):
            current, event = self._append(current, state, prior)
            prior = event.event_digest
        current = bind_batch_approval(current, timestamp=TS2)
        current, event = self._append(current, BatchState.APPROVED, prior)
        prior = event.event_digest
        current, event = self._append(
            current,
            BatchState.COMMITTING,
            prior,
            supplied_approval_digest=current.approval_digest,
            current_manifest_id=current.manifest_id,
        )
        return current, event

    def test_path_guard_allows_temp_and_rejects_project_data(self) -> None:
        self.assertEqual(self.path.resolve(), validate_prototype_ledger_path(self.path))
        project_data = Path(__file__).resolve().parents[1] / "data" / "forbidden.sqlite"
        with self.assertRaisesRegex(IngestionLedgerError, "ledger_path_outside_prototype_roots"):
            SQLiteIngestionLedger(project_data)

    def test_manifest_and_batch_repository_round_trip(self) -> None:
        self.assertEqual(self.manifest, self.ledger.get_manifest(self.manifest.manifest_id))
        self.assertEqual(self.batch, self.ledger.get_batch(self.batch.batch_id))
        self.assertEqual(
            [self.manifest],
            self.ledger.find_manifests_by_archive_sha256(self.manifest.archive_sha256),
        )
        self.assertEqual(
            [self.batch],
            self.ledger.list_batches_for_manifest(self.manifest.manifest_id),
        )

    def test_exact_manifest_insert_is_idempotent_but_identity_collision_fails(self) -> None:
        self.ledger.add_manifest(self.manifest)
        changed_metadata = replace(self.manifest, discovered_at=TS3)
        with self.assertRaisesRegex(IngestionLedgerError, "manifest_id_collision"):
            self.ledger.add_manifest(changed_metadata)

    def test_batch_id_collision_fails_without_overwrite(self) -> None:
        with self.assertRaisesRegex(IngestionLedgerError, "batch_id_collision"):
            self.ledger.add_batch(self.batch)
        self.assertEqual(self.batch, self.ledger.get_batch(self.batch.batch_id))

    def test_atomic_transition_updates_snapshot_and_appends_event(self) -> None:
        updated, event = self._append(self.batch, BatchState.VALIDATING)
        self.assertEqual(updated, self.ledger.get_batch(self.batch.batch_id))
        self.assertEqual([event], self.ledger.list_events(self.batch.batch_id))

    def test_stale_transition_is_rejected(self) -> None:
        updated, event = self._append(self.batch, BatchState.VALIDATING)
        stale, stale_event = transition_batch(
            self.batch,
            BatchState.CANCELLED,
            actor_kind="test_actor",
            reason_code="stale",
            occurred_at=TS3,
        )
        with self.assertRaisesRegex(IngestionLedgerError, "stale_or_inconsistent_transition"):
            self.ledger.append_transition(stale, stale_event)
        self.assertEqual(updated, self.ledger.get_batch(self.batch.batch_id))
        self.assertEqual([event], self.ledger.list_events(self.batch.batch_id))

    def test_event_rows_are_sqlite_append_only(self) -> None:
        self._append(self.batch, BatchState.VALIDATING)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE batch_events SET event_digest=?", ("0" * 64,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM batch_events")
        finally:
            connection.close()

    def test_storage_tampering_is_detected_on_read(self) -> None:
        _, event = self._append(self.batch, BatchState.VALIDATING)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER batch_events_no_update")
            payload = event.to_dict()
            payload["private_extra_field"] = "must fail closed"
            connection.execute(
                "UPDATE batch_events SET event_json=? WHERE event_id=?",
                (json.dumps(payload), event.event_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(IngestionLedgerError, "event_record_tampered"):
            self.ledger.list_events(self.batch.batch_id)

    def test_lease_owner_and_target_must_be_privacy_safe_opaque_tokens(self) -> None:
        for owner, target in (
            (r"C:\private\owner", "target"),
            ("owner", r"\\server\share\database"),
            ("owner with spaces", "target"),
        ):
            with self.subTest(owner=owner, target=target):
                with self.assertRaisesRegex(IngestionLedgerError, "invalid_lease_identity"):
                    self.ledger.acquire_writer_lease(
                        batch_id=self.batch.batch_id,
                        manifest_id=self.manifest.manifest_id,
                        target_id=target,
                        owner=owner,
                        created_at=TS2,
                        expires_at=TS4,
                    )

    def test_private_source_values_and_contents_are_absent_from_ledger_bytes(self) -> None:
        private = r"C:\Users\ExampleUser\private-title.zip"
        raw_body = "private conversation body and authorization token"
        updated, event = transition_batch(
            self.batch,
            BatchState.VALIDATING,
            actor_kind="validator",
            reason_code=private,
            payload={"raw": raw_body, "tailnet": "example.invalid"},
            occurred_at=TS2,
        )
        self.ledger.append_transition(updated, event)
        data = self.path.read_bytes()
        for forbidden in (private, raw_body, "example.invalid", "authorization token"):
            self.assertNotIn(forbidden.encode("utf-8"), data)

    def test_same_manifest_writer_conflicts(self) -> None:
        first = self.ledger.acquire_writer_lease(
            batch_id=self.batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="target_one",
            owner="owner_one",
            created_at=TS2,
            expires_at=TS4,
        )
        other_batch = make_batch(self.manifest)
        self.ledger.add_batch(other_batch)
        second = self.ledger.acquire_writer_lease(
            batch_id=other_batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="target_two",
            owner="owner_two",
            created_at=TS2,
            expires_at=TS4,
        )
        self.assertEqual("acquired", first.status)
        self.assertEqual("blocked", second.status)

    def test_same_target_is_mutually_exclusive_across_manifests(self) -> None:
        other_manifest = make_manifest(b"other")
        other_batch = make_batch(other_manifest)
        self.ledger.add_manifest(other_manifest)
        self.ledger.add_batch(other_batch)
        first = self.ledger.acquire_writer_lease(
            batch_id=self.batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="shared_target",
            owner="owner_one",
            created_at=TS2,
            expires_at=TS4,
        )
        second = self.ledger.acquire_writer_lease(
            batch_id=other_batch.batch_id,
            manifest_id=other_manifest.manifest_id,
            target_id="shared_target",
            owner="owner_two",
            created_at=TS2,
            expires_at=TS4,
        )
        self.assertEqual("acquired", first.status)
        self.assertEqual("blocked", second.status)

    def test_different_manifests_and_targets_can_run_in_parallel(self) -> None:
        other_manifest = make_manifest(b"other")
        other_batch = make_batch(other_manifest)
        self.ledger.add_manifest(other_manifest)
        self.ledger.add_batch(other_batch)
        decisions = (
            self.ledger.acquire_writer_lease(
                batch_id=self.batch.batch_id,
                manifest_id=self.manifest.manifest_id,
                target_id="target_one",
                owner="owner_one",
                created_at=TS2,
                expires_at=TS4,
            ),
            self.ledger.acquire_writer_lease(
                batch_id=other_batch.batch_id,
                manifest_id=other_manifest.manifest_id,
                target_id="target_two",
                owner="owner_two",
                created_at=TS2,
                expires_at=TS4,
            ),
        )
        self.assertEqual(["acquired", "acquired"], [item.status for item in decisions])

    def test_expired_lease_requires_recovery_and_is_not_auto_reassigned(self) -> None:
        first = self.ledger.acquire_writer_lease(
            batch_id=self.batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="target",
            owner="owner_one",
            created_at=TS1,
            expires_at=TS2,
        )
        other_batch = make_batch(self.manifest)
        self.ledger.add_batch(other_batch)
        second = self.ledger.acquire_writer_lease(
            batch_id=other_batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="target",
            owner="owner_two",
            created_at=TS3,
            expires_at=TS4,
        )
        self.assertEqual("acquired", first.status)
        self.assertEqual("recovery_required", second.status)
        self.assertEqual(first.lease.lease_group_id, second.lease.lease_group_id)

    def test_owner_checked_release_and_missing_lease(self) -> None:
        acquired = self.ledger.acquire_writer_lease(
            batch_id=self.batch.batch_id,
            manifest_id=self.manifest.manifest_id,
            target_id="target",
            owner="owner_one",
            created_at=TS2,
            expires_at=TS4,
        )
        with self.assertRaisesRegex(IngestionLedgerError, "writer_lease_release_mismatch"):
            self.ledger.release_writer_lease(
                batch_id=self.batch.batch_id,
                lease_group_id=acquired.lease.lease_group_id,
                owner="wrong_owner",
            )
        self.ledger.release_writer_lease(
            batch_id=self.batch.batch_id,
            lease_group_id=acquired.lease.lease_group_id,
            owner="owner_one",
        )
        result = self.ledger.inspect_writer_lease(
            batch_id=self.batch.batch_id,
            lease_group_id=acquired.lease.lease_group_id,
            owner="owner_one",
            at=TS3,
        )
        self.assertEqual("recovery_required", result.status)

    def test_committing_batch_losing_lease_enters_recovery_required(self) -> None:
        committing, _ = self._to_committing()
        recovered = self.ledger.mark_lost_committing_lease(
            batch_id=committing.batch_id,
            occurred_at=TS3,
        )
        self.assertEqual(BatchState.RECOVERY_REQUIRED.value, recovered.current_state)
        self.assertEqual("required", recovered.recovery_status)
        self.assertEqual(recovered, self.ledger.get_batch(committing.batch_id))

    def test_two_processes_competing_have_one_winner(self) -> None:
        other = make_batch(self.manifest)
        self.ledger.add_batch(other)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=process_lease_attempt,
                args=(
                    str(self.path),
                    item.batch_id,
                    self.manifest.manifest_id,
                    f"owner_{index}",
                    start,
                    results,
                ),
            )
            for index, item in enumerate((self.batch, other), start=1)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(20)
            self.assertEqual(0, process.exitcode)
        statuses = sorted(results.get(timeout=5) for _ in processes)
        self.assertEqual(["acquired", "blocked"], statuses)
