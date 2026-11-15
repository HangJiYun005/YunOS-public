from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.db_path_policy import DEFAULT_OPERATIONAL_STATE_PATH
from memory.operational_state_models import (
    AppendPreview,
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
)
from memory.operational_state_store import (
    OperationalStateStore,
    PRODUCTION_WRITE_ENV,
    default_lease_interval,
    initialize_operational_database,
)


T0 = "2026-07-31T00:00:00Z"
T1 = "2026-07-31T00:01:00Z"
T2 = "2026-07-31T00:02:00Z"
T3 = "2026-07-31T00:03:00Z"
T4 = "2026-07-31T00:04:00Z"
T5 = "2026-07-31T00:05:00Z"


class OperationalStateCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.db = self.root / "operational.db"
        initialize_operational_database(self.db, created_at=T0)
        self.store = OperationalStateStore(self.db)
        self.project_id = "project:yunos"
        self.entity_id = "project:yunos"
        self.owner = "owner:v9-test"
        self.token = "v9-test-lease-token-0001"
        self.authority = self.make_authority()
        self.anchor = self.make_anchor()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_anchor(
        self,
        *,
        anchor_id: str = "anchor:user:v9-test",
        source_kind: str = "user_declaration",
        observed_at: str = T0,
    ) -> SourceAnchor:
        return SourceAnchor(
            anchor_id=anchor_id,
            source_kind=source_kind,
            locator="task-authority:v9-test",
            evidence_digest=hashlib.sha256(b"v9-test-authority").hexdigest(),
            observed_at=observed_at,
            privacy_scope="private_metadata",
        )

    def make_authority(
        self,
        *,
        authority_id: str = "authority:v9-test",
        issued_by: str = "actor:local-user",
        event_types: tuple[str, ...] = (
            "project_declared",
            "project_paused",
            "project_resumed",
            "project_observed",
            "candidate_create",
            "candidate_decision",
        ),
        write_scope: tuple[str, ...] | None = None,
        commit_permission: bool = True,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at="2026-08-01T00:00:00Z",
            target_project_id=self.project_id,
            allowed_event_types=event_types,
            read_scope=("repo:metadata",),
            write_scope=write_scope or (self.entity_id,),
            prohibited_operations=("push", "create-next-node"),
            commit_permission=commit_permission,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )

    def make_event(
        self,
        *,
        event_type: str = "project_declared",
        state: str = "active",
        source_kind: str = "declared",
        authority_kind: str = "user",
        actor_kind: str = "user",
        actor_id: str = "actor:local-user",
        observed_at: str = T1,
        effective_at: str = T1,
        recorded_at: str = T1,
        idempotency_key: str = "idempotency:v9-project-seed",
        supersedes_event_id: str | None = None,
        payload_extra: dict[str, object] | None = None,
        authority: AuthorityArtifact | None = None,
        anchor: SourceAnchor | None = None,
    ):
        selected_authority = authority or self.authority
        payload = {
            "state": state,
            "name": "YunOS",
            "purpose": "Synthetic operational state test project",
        }
        if payload_extra:
            payload.update(payload_extra)
        return build_state_event(
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type="project",
            event_type=event_type,
            source_kind=source_kind,
            source_anchor=anchor or self.anchor,
            authority_kind=authority_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
            observed_at=observed_at,
            effective_at=effective_at,
            recorded_at=recorded_at,
            expires_at=None,
            supersedes_event_id=supersedes_event_id,
            caused_by_event_id=None,
            idempotency_key=idempotency_key,
            conflict_group_id=None,
            payload=payload,
            privacy_scope="private_metadata",
            authority_digest=selected_authority.authority_digest,
        )

    def acquire(self, *, acquired_at: str = T0, expires_at: str = T5) -> None:
        self.store.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )

    def commit(self, event, *, authority: AuthorityArtifact | None = None):
        selected_authority = authority or self.authority
        preview = self.store.preview_event(
            authority=selected_authority, event=event
        )
        receipt = self.store.commit_preview(
            authority=selected_authority,
            preview=preview,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        return preview, receipt

    def seed_project(self):
        self.acquire()
        return self.commit(self.make_event())

    def test_initial_schema_status_and_integrity(self) -> None:
        status = self.store.read_status()
        report = self.store.integrity_report()
        self.assertEqual(status["schema_version"], 9001)
        self.assertEqual(status["projection_version"], 1)
        self.assertFalse(status["writer_default_enabled"])
        self.assertFalse(status["writer_enabled_for_process"])
        self.assertEqual(report.integrity_check, "ok")
        self.assertEqual(report.foreign_key_violations, 0)
        self.assertFalse(report.schema_drift)

    def test_production_initialization_is_disabled_by_default(self) -> None:
        if DEFAULT_OPERATIONAL_STATE_PATH.exists():
            self.skipTest("production operational database already exists")
        with patch.dict(os.environ, {PRODUCTION_WRITE_ENV: ""}, clear=False):
            with self.assertRaisesRegex(
                OperationalStateError, "production_writer_disabled"
            ):
                initialize_operational_database(
                    DEFAULT_OPERATIONAL_STATE_PATH,
                    created_at=T0,
                    allow_production=True,
                )
        self.assertFalse(DEFAULT_OPERATIONAL_STATE_PATH.exists())

    def test_non_temporary_database_target_is_rejected(self) -> None:
        target = Path.cwd() / "data" / "forbidden-operational-test.db"
        with self.assertRaisesRegex(
            OperationalStateError, "unsafe_operational_database_target"
        ):
            initialize_operational_database(target, created_at=T0)
        self.assertFalse(target.exists())

    def test_preview_commit_and_receipt(self) -> None:
        self.acquire()
        event = self.make_event()
        preview, receipt = self.commit(event)
        projection = self.store.get_projection(self.entity_id)
        self.assertEqual(projection["state"], "active")
        self.assertEqual(projection["event_count"], 1)
        self.assertEqual(receipt.event_id, event.event_id)
        self.assertEqual(receipt.preview_digest, preview.preview_digest)
        self.assertFalse(receipt.idempotent_replay)
        self.assertEqual(
            receipt.projection_digest, projection["projection_digest"]
        )

    def test_commit_requires_active_matching_lease(self) -> None:
        event = self.make_event()
        preview = self.store.preview_event(
            authority=self.authority, event=event
        )
        with self.assertRaisesRegex(
            OperationalStateError, "writer_lease_required"
        ):
            self.store.commit_preview(
                authority=self.authority,
                preview=preview,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

    def test_idempotent_replay_returns_original_receipt(self) -> None:
        self.acquire()
        event = self.make_event()
        preview, first = self.commit(event)
        second = self.store.commit_preview(
            authority=self.authority,
            preview=preview,
            lease_owner_id="owner:irrelevant-for-readonly-replay",
            lease_token="different-token-no-mutation",
        )
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.receipt_digest, second.receipt_digest)
        self.assertEqual(len(self.store.list_events()), 1)

    def test_idempotency_key_collision_is_rejected(self) -> None:
        self.acquire()
        first = self.make_event()
        self.commit(first)
        collision = self.make_event(
            event_type="project_paused",
            state="paused",
            recorded_at=T2,
            effective_at=T2,
            idempotency_key=first.idempotency_key,
        )
        preview = AppendPreview.create(
            collision,
            self.authority.authority_digest,
            self.store.get_projection(self.entity_id)["projection_digest"],
        )
        with self.assertRaisesRegex(
            OperationalStateError, "idempotency_key_collision"
        ):
            self.store.commit_preview(
                authority=self.authority,
                preview=preview,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

    def test_state_events_are_append_only(self) -> None:
        self.seed_project()
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                conn.execute("UPDATE state_events SET event_type='tampered'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                conn.execute("DELETE FROM state_events")
        finally:
            conn.close()

    def test_invalid_state_transition_is_rejected(self) -> None:
        self.seed_project()
        archived = self.make_event(
            event_type="project_paused",
            state="archived",
            effective_at=T2,
            recorded_at=T2,
            idempotency_key="idempotency:invalid-archive",
        )
        with self.assertRaisesRegex(
            OperationalStateError, "invalid_state_transition"
        ):
            self.store.preview_event(
                authority=self.authority, event=archived
            )

    def test_unauthorized_event_and_scope_are_rejected(self) -> None:
        event = self.make_event()
        denied = self.make_authority(
            authority_id="authority:denied",
            event_types=("project_paused",),
            commit_permission=False,
        )
        with self.assertRaises(OperationalStateError):
            self.store.preview_event(authority=denied, event=event)

    def test_event_id_is_bound_to_authority_and_event_content(self) -> None:
        event = self.make_event()
        tampered = event.to_dict()
        tampered["event_id"] = "event:tampered-but-valid"
        with self.assertRaisesRegex(
            OperationalStateError, "event_id_mismatch"
        ):
            self.store.preview_event(
                authority=self.authority,
                event=type(event).from_dict(tampered),
            )

        other_authority = self.make_authority(
            authority_id="authority:other-valid"
        )
        with self.assertRaisesRegex(
            OperationalStateError, "event_id_mismatch"
        ):
            self.store.preview_event(
                authority=other_authority,
                event=event,
            )

    def test_agent_cannot_self_authorize_event(self) -> None:
        authority = self.make_authority(
            authority_id="authority:agent-self",
            issued_by="agent:v9-test",
        )
        event = self.make_event(
            actor_kind="agent",
            actor_id="agent:v9-test",
            authority=authority,
        )
        with self.assertRaisesRegex(
            OperationalStateError, "agent_self_authorization"
        ):
            self.store.preview_event(authority=authority, event=event)

    def test_out_of_order_lower_authority_event_is_preserved_not_projected(self) -> None:
        self.seed_project()
        observed_authority = self.make_authority(
            authority_id="authority:git-observation",
            event_types=("project_observed",),
        )
        observed_anchor = self.make_anchor(
            anchor_id="anchor:git:test",
            source_kind="git",
            observed_at=T3,
        )
        older = self.make_event(
            event_type="project_observed",
            state="proposed",
            source_kind="observed",
            authority_kind="deterministic_observation",
            actor_kind="system",
            actor_id="system:git-adapter",
            observed_at=T3,
            effective_at=T0,
            recorded_at=T3,
            idempotency_key="idempotency:older-git-observation",
            authority=observed_authority,
            anchor=observed_anchor,
        )
        self.commit(older, authority=observed_authority)
        projection = self.store.get_projection(self.entity_id)
        self.assertEqual(projection["state"], "active")
        self.assertEqual(projection["event_count"], 2)
        self.assertEqual(len(self.store.list_events()), 2)

    def test_supersession_preserves_history_and_changes_projection(self) -> None:
        _, first_receipt = self.seed_project()
        paused = self.make_event(
            event_type="project_paused",
            state="paused",
            effective_at=T2,
            recorded_at=T2,
            idempotency_key="idempotency:pause-project",
            supersedes_event_id=first_receipt.event_id,
        )
        self.commit(paused)
        projection = self.store.get_projection(self.entity_id)
        self.assertEqual(projection["state"], "paused")
        self.assertEqual(projection["event_count"], 2)
        self.assertEqual(projection["active_event_count"], 1)
        self.assertEqual(len(self.store.list_events()), 2)

    def test_stale_preview_is_rejected(self) -> None:
        self.acquire()
        first = self.make_event()
        stale = self.store.preview_event(
            authority=self.authority, event=first
        )
        self.commit(first)
        another = self.make_event(
            event_type="project_paused",
            state="paused",
            effective_at=T2,
            recorded_at=T2,
            idempotency_key="idempotency:pause-after-stale",
        )
        mismatched = AppendPreview.create(
            another, self.authority.authority_digest, None
        )
        with self.assertRaisesRegex(
            OperationalStateError, "stale_append_preview"
        ):
            self.store.commit_preview(
                authority=self.authority,
                preview=mismatched,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assertEqual(stale.previous_projection_digest, None)

    def test_writer_lease_contention_release_and_audit(self) -> None:
        self.acquire()
        with self.assertRaisesRegex(
            OperationalStateError, "writer_lease_conflict"
        ):
            self.store.acquire_writer_lease(
                owner_id="owner:other",
                token="other-lease-token-0002",
                acquired_at=T1,
                expires_at=T5,
            )
        self.store.release_writer_lease(
            owner_id=self.owner,
            token=self.token,
            released_at=T1,
        )
        conn = sqlite3.connect(self.db)
        try:
            actions = [
                row[0]
                for row in conn.execute(
                    "SELECT action FROM state_lease_events ORDER BY effective_at"
                )
            ]
        finally:
            conn.close()
        self.assertEqual(actions, ["acquired", "released"])

    def test_expired_lease_requires_explicit_recovery(self) -> None:
        self.store.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T0,
            expires_at=T1,
        )
        with self.assertRaisesRegex(
            OperationalStateError, "expired_lease_requires_recovery"
        ):
            self.store.acquire_writer_lease(
                owner_id="owner:other",
                token="other-lease-token-0002",
                acquired_at=T2,
                expires_at=T5,
            )
        recovered = self.store.recover_expired_writer_lease(
            recovered_at=T2,
            authority_digest=self.authority.authority_digest,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertIsNone(self.store.read_status()["lease"])

    def test_active_lease_cannot_be_recovered(self) -> None:
        self.acquire()
        with self.assertRaisesRegex(
            OperationalStateError, "active_lease_cannot_be_recovered"
        ):
            self.store.recover_expired_writer_lease(
                recovered_at=T1,
                authority_digest=self.authority.authority_digest,
            )

    def test_projection_rebuild_is_deterministic(self) -> None:
        self.seed_project()
        before = self.store.get_projection(self.entity_id)
        first = self.store.rebuild_projections()
        after = self.store.get_projection(self.entity_id)
        second = self.store.rebuild_projections()
        self.assertEqual(before["projection_digest"], after["projection_digest"])
        self.assertEqual(first["projection_count"], 1)
        self.assertEqual(second["projection_count"], 1)

    def test_projection_corruption_is_detected_and_rebuild_recovers(self) -> None:
        self.seed_project()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE state_projections SET canonical_projection='{}'"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            OperationalStateError, "projection_record_tampered"
        ):
            self.store.get_projection(self.entity_id)
        with self.assertRaisesRegex(
            OperationalStateError, "projection_record_tampered"
        ):
            self.store.list_projections()
        self.store.rebuild_projections()
        self.assertEqual(
            self.store.get_projection(self.entity_id)["state"], "active"
        )

    def test_projection_list_uses_one_verified_connection(self) -> None:
        self.seed_project()
        expected = self.store.get_projection(self.entity_id)
        with patch.object(
            self.store, "_connect", wraps=self.store._connect
        ) as connect:
            actual = self.store.list_projections(
                entity_type="project",
                project_id=self.project_id,
                freshness="fresh",
            )
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(actual, [expected])

    def test_projection_list_rejects_invalid_row_identity(self) -> None:
        self.seed_project()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE state_projections SET entity_id='invalid identity'"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            OperationalStateError, "invalid_identity"
        ):
            self.store.list_projections()

    def test_snapshot_verify_and_snapshot_rebuild(self) -> None:
        self.seed_project()
        snapshot = self.store.create_snapshot(created_at=T2)
        verified = self.store.verify_snapshot(snapshot["snapshot_id"])
        before = self.store.get_projection(self.entity_id)["projection_digest"]
        result = self.store.rebuild_projections(
            snapshot_id=snapshot["snapshot_id"]
        )
        after = self.store.get_projection(self.entity_id)["projection_digest"]
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(result["base_sequence"], 1)
        self.assertEqual(before, after)

    def test_candidate_is_not_projected_and_requires_external_approval(self) -> None:
        candidate = self.store.create_candidate(
            candidate_id="candidate:v9-next-step",
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type="project",
            candidate_type="next_step",
            candidate={"proposal": "synthetic next step"},
            confidence_ppm=750000,
            source_anchor=self.anchor,
            created_at=T1,
            expires_at=T5,
        )
        self.assertEqual(candidate["status"], "pending")
        self.assertIsNone(self.store.get_projection(self.entity_id))
        approval = self.store.decide_candidate(
            candidate_id="candidate:v9-next-step",
            decision="approve",
            authority=self.authority,
            recorded_at=T2,
        )
        self.assertEqual(approval["status"], "approved")
        self.assertIsNone(self.store.get_projection(self.entity_id))

    def test_agent_cannot_approve_candidate(self) -> None:
        self.store.create_candidate(
            candidate_id="candidate:agent-test",
            project_id=self.project_id,
            entity_id=None,
            entity_type="project",
            candidate_type="focus",
            candidate={"proposal": "synthetic"},
            confidence_ppm=500000,
            source_anchor=self.anchor,
            created_at=T1,
            expires_at=None,
        )
        agent_authority = self.make_authority(
            authority_id="authority:agent-approval",
            issued_by="agent:v9-test",
        )
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_approve_candidate"
        ):
            self.store.decide_candidate(
                candidate_id="candidate:agent-test",
                decision="approve",
                authority=agent_authority,
                recorded_at=T2,
            )

    def test_candidate_decision_requires_explicit_commit_authority(self) -> None:
        self.store.create_candidate(
            candidate_id="candidate:no-commit",
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type="project",
            candidate_type="focus",
            candidate={"proposal": "synthetic"},
            confidence_ppm=500000,
            source_anchor=self.anchor,
            created_at=T1,
            expires_at=None,
        )
        denied = self.make_authority(
            authority_id="authority:no-candidate-commit",
            commit_permission=False,
        )
        with self.assertRaisesRegex(
            OperationalStateError, "event_commit_not_authorized"
        ):
            self.store.decide_candidate(
                candidate_id="candidate:no-commit",
                decision="approve",
                authority=denied,
                recorded_at=T2,
            )

    def test_production_auxiliary_write_requires_authority_and_lease(self) -> None:
        self.store.production = True
        self.store.allow_production_write = True
        with patch.dict(os.environ, {PRODUCTION_WRITE_ENV: "1"}):
            with self.assertRaisesRegex(
                OperationalStateError, "production_authority_required"
            ):
                self.store.create_candidate(
                    candidate_id="candidate:production-guard",
                    project_id=self.project_id,
                    entity_id=self.entity_id,
                    entity_type="project",
                    candidate_type="focus",
                    candidate={"proposal": "synthetic"},
                    confidence_ppm=500000,
                    source_anchor=self.anchor,
                    created_at=T1,
                    expires_at=None,
                )
            with self.assertRaisesRegex(
                OperationalStateError, "production_writer_lease_required"
            ):
                self.store.create_candidate(
                    candidate_id="candidate:production-guard",
                    project_id=self.project_id,
                    entity_id=self.entity_id,
                    entity_type="project",
                    candidate_type="focus",
                    candidate={"proposal": "synthetic"},
                    confidence_ppm=500000,
                    source_anchor=self.anchor,
                    created_at=T1,
                    expires_at=None,
                    authority=self.authority,
                )
        self.store.production = False
        self.assertEqual(
            self.store.read_status()["counts"]["state_candidates"], 0
        )

    def test_conflict_marks_projection_conflicted_without_deleting_events(self) -> None:
        self.seed_project()
        second = self.make_event(
            event_type="project_observed",
            state="active",
            effective_at=T2,
            recorded_at=T2,
            idempotency_key="idempotency:conflict-observation",
        )
        _, receipt = self.commit(second)
        events = self.store.list_events()
        result = self.store.create_conflict(
            conflict_group_id="conflict:v9-project-state",
            project_id=self.project_id,
            entity_id=self.entity_id,
            field_name="state",
            member_event_ids=[
                events[0]["event"]["event_id"],
                receipt.event_id,
            ],
            created_at=T3,
        )
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            self.store.get_projection(self.entity_id)["freshness"],
            "conflicted",
        )
        self.assertEqual(len(self.store.list_events()), 2)

    def test_verification_is_append_only(self) -> None:
        _, receipt = self.seed_project()
        result = self.store.record_verification(
            verification_id="verification:v9-project",
            entity_id=self.entity_id,
            event_id=receipt.event_id,
            verifier_kind="test",
            result="passed",
            evidence_digest=hashlib.sha256(b"test-evidence").hexdigest(),
            details={"checks": 3},
            recorded_at=T2,
        )
        self.assertEqual(result["result"], "passed")
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                conn.execute("DELETE FROM state_verifications")
        finally:
            conn.close()

    def test_freshness_can_change_without_changing_event_history(self) -> None:
        self.seed_project()
        before_count = len(self.store.list_events())
        stale = self.store.set_freshness(
            entity_id=self.entity_id, freshness="stale"
        )
        self.assertEqual(stale["freshness"], "stale")
        self.assertEqual(len(self.store.list_events()), before_count)

    def test_backup_and_restore_preserve_events_projection_and_schema(self) -> None:
        self.seed_project()
        backup = self.root / "backup.db"
        restored_path = self.root / "restored.db"
        report = self.store.backup_to(backup)
        restored = OperationalStateStore.restore_from_backup(
            backup, restored_path
        )
        self.assertEqual(report["integrity_check"], "ok")
        self.assertEqual(
            restored.get_projection(self.entity_id)["projection_digest"],
            self.store.get_projection(self.entity_id)["projection_digest"],
        )
        self.assertEqual(restored.list_events(), self.store.list_events())
        self.assertEqual(
            restored.integrity_report().schema_digest,
            self.store.integrity_report().schema_digest,
        )

    def test_schema_drift_is_fail_closed(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE unauthorized_schema_drift(value TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            OperationalStateError, "operational_schema_drift"
        ):
            self.store.read_status()

    def test_invalid_clock_and_privacy_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError, "timestamp_must_be_utc"
        ):
            self.make_anchor(observed_at="2026-07-31T08:00:00+08:00")
        with self.assertRaisesRegex(
            OperationalStateError, "invalid_privacy_scope"
        ):
            SourceAnchor(
                anchor_id="anchor:invalid-privacy",
                source_kind="test",
                locator="synthetic",
                evidence_digest="0" * 64,
                observed_at=T0,
                privacy_scope="secret",
            )

    def test_inferred_candidate_cannot_be_built_as_state_event(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError, "candidate_cannot_be_authoritative_event"
        ):
            self.make_event(
                source_kind="inferred_candidate",
                authority_kind="candidate_only",
            )

    def test_default_lease_interval_is_utc_and_bounded(self) -> None:
        acquired, expires = default_lease_interval(
            acquired_at=T0, seconds=120
        )
        self.assertEqual(acquired, "2026-07-31T00:00:00.000000Z")
        self.assertEqual(expires, "2026-07-31T00:02:00.000000Z")

    def test_state_admin_status_cli_is_read_only(self) -> None:
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = subprocess.run(
            [
                sys.executable,
                str(Path.cwd() / "tools" / "state_admin.py"),
                "status",
                "--db",
                str(self.db),
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertEqual(before, after)

    def test_state_admin_requires_preview_confirmation(self) -> None:
        parser_result = subprocess.run(
            [
                sys.executable,
                str(Path.cwd() / "tools" / "state_admin.py"),
                "commit",
                "--help",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(parser_result.returncode, 0)
        self.assertIn("--confirm-preview-digest", parser_result.stdout)
        self.assertIn("--lease-token-file", parser_result.stdout)

    def test_authority_digest_and_projection_are_deterministic(self) -> None:
        recreated = self.make_authority()
        self.assertEqual(
            recreated.authority_digest, self.authority.authority_digest
        )
        self.seed_project()
        first = self.store.get_projection(self.entity_id)
        self.store.rebuild_projections()
        second = self.store.get_projection(self.entity_id)
        self.assertEqual(
            canonical_digest(first), canonical_digest(second)
        )


if __name__ == "__main__":
    unittest.main()
