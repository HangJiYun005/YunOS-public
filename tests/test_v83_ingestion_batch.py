from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from memory.ingestion_core import (
    APPROVAL_POLICY_VERSION,
    AssetFingerprintItem,
    AssetStageFingerprint,
    BatchEvent,
    BatchMode,
    BatchState,
    DatabaseFingerprint,
    ImportBatch,
    IngestionCoreError,
    LEGAL_TRANSITIONS,
    ReplayDecisionCode,
    bind_batch_approval,
    build_asset_stage_fingerprint,
    build_database_fingerprint,
    build_source_manifest,
    create_import_batch,
    decide_source_replay,
    make_approval_digest,
    make_plan_digest,
    replay_batch_events,
    transition_batch,
    verify_batch_snapshot,
)


TS1 = "2026-07-26T00:00:00.000000Z"
TS2 = "2026-07-26T00:01:00.000000Z"
TS3 = "2026-07-26T00:02:00.000000Z"
ZERO = "0" * 64
ONE = "1" * 64
TWO = "2" * 64


def manifest(**overrides):
    values = {
        "source_type": "chatgpt",
        "source_version": "export_v1",
        "adapter_name": "fixture_adapter",
        "adapter_version": "adapter_v1",
        "archive_sha256": hashlib.sha256(b"archive").hexdigest(),
        "archive_size": 7,
        "archive_kind": "zip",
        "discovered_at": TS1,
        "scanned_at": TS2,
    }
    values.update(overrides)
    return build_source_manifest(**values)


def db_fingerprint() -> DatabaseFingerprint:
    return DatabaseFingerprint(ZERO, 10, ONE, TWO)


def asset_fingerprint() -> AssetStageFingerprint:
    return AssetStageFingerprint(2, 20, ONE, TWO)


def batch(**overrides) -> ImportBatch:
    values = {
        "batch_mode": BatchMode.FULL.value,
        "plan": {"actions": [{"kind": "insert", "count": 3}], "created_at": TS1},
        "database_before_fingerprint": db_fingerprint(),
        "asset_stage_fingerprint": asset_fingerprint(),
        "timestamp": TS1,
    }
    values.update(overrides)
    return create_import_batch(manifest(), **values)


def at_state(source: ImportBatch, state: BatchState, **overrides) -> ImportBatch:
    values = {
        "current_state": state.value,
        "event_sequence": 0,
        "updated_at": TS2,
    }
    values.update(overrides)
    return replace(source, **values)


class BatchModelTest(unittest.TestCase):
    def test_batch_ids_are_unique_and_manifest_is_bound(self) -> None:
        first = batch()
        second = batch()
        self.assertNotEqual(first.batch_id, second.batch_id)
        self.assertEqual(manifest().manifest_id, first.manifest_id)
        self.assertEqual("chatgpt", first.source_type)

    def test_every_required_batch_mode_is_accepted(self) -> None:
        for mode in BatchMode:
            with self.subTest(mode=mode.value):
                self.assertEqual(mode.value, batch(batch_mode=mode.value).batch_mode)

    def test_invalid_batch_mode_and_id_are_rejected(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "invalid_batch_mode"):
            batch(batch_mode="unsafe_write")
        with self.assertRaisesRegex(IngestionCoreError, "invalid_batch_id"):
            batch(batch_id_factory=lambda: "batch_collision")

    def test_invalid_fingerprint_and_recovery_status_are_rejected(self) -> None:
        source = batch()
        with self.assertRaisesRegex(IngestionCoreError, "invalid_batch_fingerprint"):
            replace(source, database_before_fingerprint="not-a-fingerprint")
        with self.assertRaisesRegex(IngestionCoreError, "invalid_recovery_status"):
            replace(source, recovery_status="silently_safe")

    def test_plan_digest_excludes_paths_and_timestamps(self) -> None:
        first = make_plan_digest({
            "created_at": TS1,
            "path": r"C:\private\one.zip",
            "actions": [{"count": 2, "kind": "insert"}],
        })
        second = make_plan_digest({
            "created_at": TS3,
            "path": r"D:\other\two.zip",
            "actions": [{"kind": "insert", "count": 2}],
        })
        self.assertEqual(first, second)

    def test_plan_digest_changes_for_semantic_input_and_normalizes_unicode(self) -> None:
        self.assertNotEqual(
            make_plan_digest({"count": 1}),
            make_plan_digest({"count": 2}),
        )
        self.assertEqual(
            make_plan_digest({"label": "e\u0301"}),
            make_plan_digest({"label": "\u00e9"}),
        )

    def test_plan_digest_matches_in_a_separate_process(self) -> None:
        value = {"actions": [{"kind": "insert", "count": 2}], "created_at": TS1}
        expected = make_plan_digest(value)
        script = (
            "from memory.ingestion_core import make_plan_digest;"
            "print(make_plan_digest({'created_at':'different',"
            "'actions':[{'count':2,'kind':'insert'}]}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(expected, result.stdout.strip())

    def test_approval_digest_binds_manifest_plan_database_assets_and_policy(self) -> None:
        value = make_approval_digest(
            manifest_id=manifest().manifest_id,
            plan_digest=make_plan_digest({"count": 1}),
            database_before_fingerprint=db_fingerprint(),
            asset_stage_fingerprint=asset_fingerprint(),
        )
        self.assertEqual(64, len(value))
        changed = make_approval_digest(
            manifest_id=manifest(archive_sha256=hashlib.sha256(b"changed").hexdigest()).manifest_id,
            plan_digest=make_plan_digest({"count": 1}),
            database_before_fingerprint=db_fingerprint(),
            asset_stage_fingerprint=asset_fingerprint(),
        )
        self.assertNotEqual(value, changed)
        self.assertIn("manifest", APPROVAL_POLICY_VERSION)

    def test_database_fingerprint_is_path_free_and_descriptor_order_stable(self) -> None:
        first = build_database_fingerprint(
            content_sha256=ZERO,
            size_bytes=10,
            schema_descriptor={"tables": ["messages"], "version": 0},
            logical_descriptor={"messages": 4, "conversations": 2},
        )
        second = build_database_fingerprint(
            content_sha256=ZERO,
            size_bytes=10,
            schema_descriptor={"version": 0, "tables": ["messages"]},
            logical_descriptor={"conversations": 2, "messages": 4},
        )
        self.assertEqual(first, second)
        self.assertNotIn("path", json.dumps(first.to_dict()))

    def test_asset_fingerprint_is_order_stable_and_rejects_duplicate_keys(self) -> None:
        items = (
            AssetFingerprintItem(ZERO, ONE, 10),
            AssetFingerprintItem(TWO, ZERO, 20),
        )
        self.assertEqual(
            build_asset_stage_fingerprint(items),
            build_asset_stage_fingerprint(reversed(items)),
        )
        with self.assertRaisesRegex(IngestionCoreError, "duplicate_asset_logical_key"):
            build_asset_stage_fingerprint((items[0], items[0]))

    def test_approval_can_only_bind_while_awaiting(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "approval_not_awaiting"):
            bind_batch_approval(batch())
        awaiting = at_state(batch(), BatchState.AWAITING_APPROVAL)
        approved = bind_batch_approval(awaiting, timestamp=TS2)
        self.assertEqual(64, len(approved.approval_digest))

    def test_serialized_report_is_json_safe_and_path_free(self) -> None:
        encoded = json.dumps(batch().to_dict(), sort_keys=True)
        self.assertNotIn("C:\\\\", encoded)
        self.assertNotIn("private", encoded)
        self.assertIn("batch_schema_version", encoded)


class StateMachineTest(unittest.TestCase):
    def test_every_declared_legal_transition_is_accepted(self) -> None:
        base = batch()
        for previous, targets in LEGAL_TRANSITIONS.items():
            for target in targets:
                source = at_state(base, previous)
                kwargs = {}
                if target is BatchState.COMPLETED_WITH_WARNINGS:
                    source = replace(source, warning_count=1)
                if target is BatchState.COMMITTING:
                    expected = make_approval_digest(
                        manifest_id=source.manifest_id,
                        plan_digest=source.plan_digest,
                        database_before_fingerprint=source.database_before_fingerprint,
                        asset_stage_fingerprint=source.asset_stage_fingerprint,
                    )
                    source = replace(source, approval_digest=expected)
                    kwargs = {
                        "supplied_approval_digest": expected,
                        "current_manifest_id": source.manifest_id,
                    }
                with self.subTest(previous=previous.value, target=target.value):
                    updated, event = transition_batch(
                        source,
                        target,
                        actor_kind="test_actor",
                        reason_code="test_transition",
                        occurred_at=TS2,
                        **kwargs,
                    )
                    self.assertEqual(target.value, updated.current_state)
                    self.assertEqual(previous.value, event.previous_state)

    def test_every_undeclared_transition_is_rejected(self) -> None:
        base = batch()
        states = set(BatchState)
        for previous in BatchState:
            illegal = states - set(LEGAL_TRANSITIONS[previous]) - {previous}
            for target in illegal:
                with self.subTest(previous=previous.value, target=target.value):
                    with self.assertRaisesRegex(IngestionCoreError, "illegal_state_transition"):
                        transition_batch(
                            at_state(base, previous),
                            target,
                            actor_kind="test_actor",
                            reason_code="invalid_transition",
                            occurred_at=TS2,
                        )

    def test_explicitly_forbidden_shortcuts_remain_closed(self) -> None:
        pairs = (
            (BatchState.DISCOVERED, BatchState.COMPLETED),
            (BatchState.PLANNED, BatchState.COMMITTING),
            (BatchState.FAILED, BatchState.COMPLETED),
            (BatchState.RECOVERY_REQUIRED, BatchState.COMPLETED),
            (BatchState.COMPLETED, BatchState.COMMITTING),
            (BatchState.CANCELLED, BatchState.COMMITTING),
        )
        for previous, target in pairs:
            with self.subTest(previous=previous.value, target=target.value):
                with self.assertRaisesRegex(IngestionCoreError, "illegal_state_transition"):
                    transition_batch(
                        at_state(batch(), previous),
                        target,
                        actor_kind="test_actor",
                        reason_code="blocked",
                        occurred_at=TS2,
                    )

    def test_committing_requires_current_manifest_and_exact_approval_digest(self) -> None:
        source = at_state(batch(), BatchState.APPROVED)
        expected = make_approval_digest(
            manifest_id=source.manifest_id,
            plan_digest=source.plan_digest,
            database_before_fingerprint=source.database_before_fingerprint,
            asset_stage_fingerprint=source.asset_stage_fingerprint,
        )
        source = replace(source, approval_digest=expected)
        for supplied, current in (
            ("", source.manifest_id),
            (ZERO, source.manifest_id),
            (expected, manifest(archive_sha256=ZERO).manifest_id),
        ):
            with self.subTest(supplied=supplied[:8], current=current[:12]):
                with self.assertRaises(IngestionCoreError):
                    transition_batch(
                        source,
                        BatchState.COMMITTING,
                        actor_kind="writer",
                        reason_code="commit_requested",
                        occurred_at=TS2,
                        supplied_approval_digest=supplied,
                        current_manifest_id=current,
                    )

    def test_completed_with_warnings_requires_warning_count(self) -> None:
        source = at_state(batch(), BatchState.VERIFYING)
        with self.assertRaisesRegex(IngestionCoreError, "completed_warning_count_missing"):
            transition_batch(
                source,
                BatchState.COMPLETED_WITH_WARNINGS,
                actor_kind="verifier",
                reason_code="warnings_found",
                occurred_at=TS2,
            )

    def test_recovery_and_rollback_status_are_explicit(self) -> None:
        source = at_state(batch(), BatchState.COMMITTING)
        recovery, event = transition_batch(
            source,
            BatchState.RECOVERY_REQUIRED,
            actor_kind="lease_monitor",
            reason_code="writer_lease_lost",
            occurred_at=TS2,
        )
        self.assertEqual("required", recovery.recovery_status)
        rolling, event2 = transition_batch(
            recovery,
            BatchState.ROLLING_BACK,
            actor_kind="recovery_worker",
            reason_code="rollback_started",
            occurred_at=TS3,
            previous_event_digest=event.event_digest,
        )
        self.assertEqual("rolling_back", rolling.recovery_status)
        rolled, _ = transition_batch(
            rolling,
            BatchState.ROLLED_BACK,
            actor_kind="recovery_worker",
            reason_code="rollback_verified",
            occurred_at="2026-07-26T00:03:00.000000Z",
            previous_event_digest=event2.event_digest,
        )
        self.assertEqual("rolled_back", rolled.recovery_status)

    def test_malicious_reason_and_payload_are_not_persisted(self) -> None:
        raw = r"C:\Users\ExampleUser\source.zip bearer secret-message-body"
        updated, event = transition_batch(
            batch(),
            BatchState.VALIDATING,
            actor_kind="validator",
            reason_code=raw,
            payload={"raw_message": raw},
            occurred_at=TS2,
        )
        encoded = json.dumps(event.to_dict())
        self.assertEqual("redacted_error_detail", event.reason_code)
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret-message-body", encoded)
        self.assertEqual(1, updated.event_sequence)


class EventReplayTest(unittest.TestCase):
    def _events(self):
        current = batch()
        events = []
        prior = ""
        for state in (
            BatchState.VALIDATING,
            BatchState.PLANNED,
            BatchState.AWAITING_APPROVAL,
        ):
            current, event = transition_batch(
                current,
                state,
                actor_kind="test_actor",
                reason_code="progress",
                occurred_at=TS2,
                previous_event_digest=prior,
            )
            events.append(event)
            prior = event.event_digest
        return current, events

    def test_sequence_and_replay_rebuild_snapshot(self) -> None:
        current, events = self._events()
        replay = replay_batch_events(current.batch_id, events)
        self.assertEqual(BatchState.AWAITING_APPROVAL.value, replay.current_state)
        self.assertEqual([1, 2, 3], [event.sequence for event in events])
        verify_batch_snapshot(current, events)

    def test_event_gap_is_rejected(self) -> None:
        current, events = self._events()
        with self.assertRaisesRegex(IngestionCoreError, "event_sequence_gap"):
            replay_batch_events(current.batch_id, [events[0], events[2]])

    def test_event_chain_tampering_is_rejected(self) -> None:
        current, events = self._events()
        raw = events[1].to_dict()
        raw["reason_code"] = "tampered"
        with self.assertRaisesRegex(IngestionCoreError, "event_digest_mismatch"):
            BatchEvent.from_dict(raw)
        chain = events[2].to_dict()
        chain["previous_event_digest"] = ZERO
        with self.assertRaisesRegex(IngestionCoreError, "event_digest_mismatch"):
            BatchEvent.from_dict(chain)

    def test_snapshot_mismatch_is_rejected(self) -> None:
        current, events = self._events()
        stale = replace(current, current_state=BatchState.PLANNED.value)
        with self.assertRaisesRegex(IngestionCoreError, "batch_snapshot_mismatch"):
            verify_batch_snapshot(stale, events)


class ReplayDecisionTest(unittest.TestCase):
    def test_new_archive_is_rescan_allowed(self) -> None:
        result = decide_source_replay(manifest(), [], [])
        self.assertEqual(ReplayDecisionCode.RESCAN_ALLOWED, result.code)

    def test_completed_is_exact_duplicate(self) -> None:
        item = manifest()
        completed = at_state(batch(), BatchState.COMPLETED)
        result = decide_source_replay(item, [item], [completed])
        self.assertEqual(ReplayDecisionCode.EXACT_DUPLICATE, result.code)

    def test_committing_is_recovery_required(self) -> None:
        item = manifest()
        committing = at_state(batch(), BatchState.COMMITTING)
        result = decide_source_replay(item, [item], [committing])
        self.assertEqual(ReplayDecisionCode.RECOVERY_REQUIRED, result.code)

    def test_failed_is_retry_allowed(self) -> None:
        item = manifest()
        failed = at_state(batch(), BatchState.FAILED)
        result = decide_source_replay(item, [item], [failed])
        self.assertEqual(ReplayDecisionCode.RETRY_ALLOWED, result.code)

    def test_planned_or_approved_requires_approval(self) -> None:
        item = manifest()
        for state in (BatchState.PLANNED, BatchState.AWAITING_APPROVAL, BatchState.APPROVED):
            with self.subTest(state=state.value):
                result = decide_source_replay(item, [item], [at_state(batch(), state)])
                self.assertEqual(ReplayDecisionCode.APPROVAL_REQUIRED, result.code)

    def test_active_scan_is_blocked(self) -> None:
        item = manifest()
        result = decide_source_replay(
            item, [item], [at_state(batch(), BatchState.VALIDATING)]
        )
        self.assertEqual(ReplayDecisionCode.BLOCKED, result.code)

    def test_same_archive_with_adapter_or_schema_change_is_incompatible(self) -> None:
        old = manifest()
        candidates = (
            manifest(adapter_version="adapter_v2"),
            manifest(manifest_schema_version="v8.3-a-source-manifest-2"),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate.manifest_id):
                result = decide_source_replay(candidate, [old], [])
                self.assertEqual(ReplayDecisionCode.INCOMPATIBLE_MANIFEST, result.code)

    def test_cancelled_or_rolled_back_can_rescan(self) -> None:
        item = manifest()
        for state in (BatchState.CANCELLED, BatchState.ROLLED_BACK):
            with self.subTest(state=state.value):
                result = decide_source_replay(item, [item], [at_state(batch(), state)])
                self.assertEqual(ReplayDecisionCode.RESCAN_ALLOWED, result.code)
