from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from memory.operational_state_delegation import (
    DelegationContract,
    DelegationService,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


T0 = "2026-07-31T00:00:00Z"
T1 = "2026-07-31T00:01:00Z"
T2 = "2026-07-31T00:02:00Z"
T3 = "2026-07-31T00:03:00Z"
T4 = "2026-07-31T00:04:00Z"
T5 = "2026-07-31T00:05:00Z"
T6 = "2026-07-31T00:06:00Z"
T7 = "2026-07-31T00:07:00Z"
T8 = "2026-07-31T00:08:00Z"
T9 = "2026-07-31T00:09:00Z"


class DelegationOutcomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.database = self.root / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        self.store = OperationalStateStore(self.database)
        self.project_id = "project:yunos"
        self.work_item_id = "work-item:v9-production-adoption"
        self.delegation_id = "delegation:v9-safe-test"
        self.owner = "owner:v9-delegation-test"
        self.token = "v9-delegation-writer-token"
        self.anchor = SourceAnchor(
            anchor_id="anchor:v9-delegation-test",
            source_kind="user_declaration",
            locator="user-declaration:v9-delegation-test",
            evidence_digest=hashlib.sha256(b"delegation-test").hexdigest(),
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        self.authority = self.make_authority()
        self.contract = self.make_contract()
        self.service = DelegationService(
            self.store, source_anchor=self.anchor
        )
        self.store.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T0,
            expires_at="2026-08-01T00:00:00Z",
        )
        self.seed_work_item()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_authority(
        self,
        *,
        authority_id: str = "authority:v9-delegation-test",
        issued_by: str = "actor:local-user",
        merge_permission: bool = False,
        tag_permission: bool = False,
        push_permission: bool = False,
        next_node_permission: bool = False,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at="2026-08-02T00:00:00Z",
            target_project_id=self.project_id,
            allowed_event_types=(
                "work_item_declared",
                "work_item_completed",
                "delegation_proposed",
                "delegation_authorized",
                "delegation_queued",
                "delegation_running",
                "delegation_waiting",
                "delegation_completed",
                "delegation_failed",
                "delegation_cancelled",
                "delegation_accepted",
                "delegation_rejected",
                "delegation_overreach",
                "outcome_reported",
                "outcome_verified",
                "outcome_accepted",
                "outcome_rejected",
                "candidate_decision",
                "candidate_create",
                "delegation_contract_register",
                "outcome_projection_update",
            ),
            read_scope=("*",),
            write_scope=("*",),
            prohibited_operations=("push", "create-next-node"),
            commit_permission=True,
            merge_permission=merge_permission,
            tag_permission=tag_permission,
            push_permission=push_permission,
            next_node_permission=next_node_permission,
        )

    def make_contract(
        self,
        *,
        delegation_id: str | None = None,
        authority: AuthorityArtifact | None = None,
        merge_permission: bool = False,
        tag_permission: bool = False,
        push_permission: bool = False,
        next_node_permission: bool = False,
    ) -> DelegationContract:
        selected = authority or self.authority
        return DelegationContract(
            delegation_id=delegation_id or self.delegation_id,
            project_id=self.project_id,
            work_item_id=self.work_item_id,
            intent="Execute the explicitly authorized safe v9 test task.",
            read_scope=("repo:yunos",),
            write_scope=(self.work_item_id,),
            prohibited_actions=(
                "merge",
                "tag",
                "push",
                "declare_project_complete",
                "declare_project_sealed",
                "resolve_conflict",
                "start_next_version",
                "create_long_term_plan",
                "update_user_profile",
            ),
            tool_scope=("tool:unit-test",),
            risk="low",
            valid_from=T1,
            expires_at="2026-08-01T00:00:00Z",
            acceptance_criteria=(
                "All scoped tests pass.",
                "No production data is modified.",
            ),
            rollback="Discard the disposable test database.",
            version_scope=("v9-D",),
            commit_permission=True,
            merge_permission=merge_permission,
            tag_permission=tag_permission,
            push_permission=push_permission,
            next_node_permission=next_node_permission,
            authority_digest=selected.authority_digest,
        )

    def seed_work_item(self) -> None:
        event = build_state_event(
            project_id=self.project_id,
            entity_id=self.work_item_id,
            entity_type="work_item",
            event_type="work_item_declared",
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id="actor:local-user",
            observed_at=T1,
            effective_at=T1,
            recorded_at=T1,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key="idempotency:v9-delegation-work-item",
            conflict_group_id=None,
            payload={
                "state": "in_progress",
                "title": "Adopt v9 production state safely",
            },
            privacy_scope="private_metadata",
            authority_digest=self.authority.authority_digest,
        )
        preview = self.store.preview_event(
            authority=self.authority, event=event
        )
        self.store.commit_preview(
            authority=self.authority,
            preview=preview,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def create_delegation(self):
        return self.service.create(
            contract=self.contract,
            authority=self.authority,
            user_actor_id="actor:local-user",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def transition(self, next_state: str, at: str):
        return self.service.transition(
            delegation_id=self.delegation_id,
            next_state=next_state,
            authority=self.authority,
            actor_kind=(
                "user" if next_state == "authorized" else "agent"
            ),
            actor_id=(
                "actor:local-user"
                if next_state == "authorized"
                else "agent:v9-test"
            ),
            at=at,
            lease_owner_id=self.owner,
            lease_token=self.token,
            reason_code=f"test_{next_state}",
        )

    def running_delegation(self) -> None:
        self.create_delegation()
        self.transition("authorized", T2)
        self.transition("queued", T3)
        self.transition("running", T4)

    def test_contract_is_deterministic_bound_and_append_only(self) -> None:
        created = self.create_delegation()
        stored = self.store.get_delegation_contract(self.delegation_id)
        self.assertEqual(
            created["contract_digest"], self.contract.contract_digest
        )
        self.assertEqual(
            DelegationContract.from_dict(stored["contract"]),
            self.contract,
        )
        self.assertEqual(
            stored["authority_digest"], self.authority.authority_digest
        )
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                connection.execute(
                    "UPDATE delegation_contracts SET project_id='project:x'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                connection.execute("DELETE FROM delegation_contracts")
        finally:
            connection.close()

    def test_agent_cannot_authorize_or_expand_contract(self) -> None:
        agent_authority = self.make_authority(
            authority_id="authority:agent-self",
            issued_by="agent:self",
        )
        agent_contract = self.make_contract(
            authority=agent_authority,
            delegation_id="delegation:agent-self",
        )
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_authorize_delegation"
        ):
            agent_contract.validate_against_authority(agent_authority)
        excessive = self.make_contract(merge_permission=True)
        with self.assertRaisesRegex(
            OperationalStateError, "delegation_exceeds_merge_authority"
        ):
            excessive.validate_against_authority(self.authority)
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_propose_delegation"
        ):
            self.service.create(
                contract=self.contract,
                authority=self.authority,
                user_actor_id="agent:self",
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

    def test_delegation_lifecycle_to_running(self) -> None:
        self.running_delegation()
        projection = self.store.get_projection(self.delegation_id)
        self.assertEqual(projection["state"], "running")
        self.assertEqual(projection["fields"]["work_item_id"], self.work_item_id)
        self.assertEqual(projection["event_count"], 4)

    def test_action_validator_covers_scope_version_and_privileged_overreach(
        self,
    ) -> None:
        allowed = self.contract.validate_action(
            {"action": "write", "target": self.work_item_id}, at=T2
        )
        self.assertEqual(allowed, ())
        cases = {
            "read_scope_expansion": {
                "action": "read",
                "target": "repo:private-other",
            },
            "write_scope_expansion": {
                "action": "write",
                "target": "work-item:other",
            },
            "tool_scope_expansion": {
                "action": "use_tool",
                "tool": "tool:network",
            },
            "next_node_not_authorized": {"action": "create_node"},
            "version_scope_expansion": {
                "action": "change_version",
                "version": "v9-E",
            },
            "merge_not_authorized": {"action": "merge"},
            "tag_not_authorized": {"action": "tag"},
            "push_not_authorized": {"action": "push"},
            "agent_declare_project_complete_forbidden": {
                "action": "declare_project_complete"
            },
            "agent_declare_project_sealed_forbidden": {
                "action": "declare_project_sealed"
            },
            "agent_resolve_conflict_forbidden": {
                "action": "resolve_conflict"
            },
            "agent_start_next_version_forbidden": {
                "action": "start_next_version"
            },
            "agent_create_long_term_plan_forbidden": {
                "action": "create_long_term_plan"
            },
            "agent_update_user_profile_forbidden": {
                "action": "update_user_profile"
            },
        }
        for expected, action in cases.items():
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    self.contract.validate_action(action, at=T2),
                )

    def test_overreach_fails_closed_records_event_and_moves_waiting(self) -> None:
        self.running_delegation()
        result = self.service.enforce_action(
            delegation_id=self.delegation_id,
            proposed_action={"action": "push", "target": "origin"},
            authority=self.authority,
            agent_id="agent:v9-test",
            at=T5,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(result["status"], "blocked_waiting_for_controller")
        self.assertFalse(result["operation_executed"])
        self.assertIn("push_not_authorized", result["violations"])
        projection = self.store.get_projection(self.delegation_id)
        self.assertEqual(projection["state"], "waiting")
        self.assertFalse(
            projection["fields"]["project_completion_updated"]
        )
        events = self.store.list_events(entity_id=self.delegation_id)
        self.assertEqual(
            events[-1]["event"]["event_type"], "delegation_overreach"
        )
        self.assertEqual(
            events[-1]["event"]["canonical_payload"][
                "proposed_action"
            ]["action"],
            "push",
        )

    def test_completed_verified_accepted_candidate_then_projection_update(
        self,
    ) -> None:
        self.running_delegation()
        receipt_digest = hashlib.sha256(b"runtime-receipt").hexdigest()
        execution = self.service.record_execution_result(
            delegation_id=self.delegation_id,
            outcome_id="outcome:v9-safe-test",
            runtime_run_id="runtime-run:v9-safe-test",
            result="completed",
            receipt_digest=receipt_digest,
            artifact_references=("artifact:test-report",),
            authority=self.authority,
            runtime_actor_id="runtime:v8",
            at=T5,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertFalse(execution["work_item_completion_updated"])
        self.assertEqual(
            self.store.get_projection(self.work_item_id)["state"],
            "in_progress",
        )
        self.assertEqual(
            self.store.get_projection(self.delegation_id)["state"],
            "completed",
        )
        verification_digest = hashlib.sha256(
            b"verification-evidence"
        ).hexdigest()
        verified = self.service.verify_outcome(
            outcome_id="outcome:v9-safe-test",
            verification_digest=verification_digest,
            passed=True,
            authority=self.authority,
            verifier_id="runtime:verifier",
            at=T6,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(verified["status"], "verified")
        self.assertFalse(verified["work_item_completion_updated"])
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_accept_outcome"
        ):
            self.service.accept_outcome(
                outcome_id="outcome:v9-safe-test",
                authority=self.authority,
                user_actor_id="agent:self-go",
                at=T7,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        accepted = self.service.accept_outcome(
            outcome_id="outcome:v9-safe-test",
            authority=self.authority,
            user_actor_id="actor:local-user",
            at=T7,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(
            accepted["delegation_transition"]["state"], "accepted"
        )
        self.assertFalse(accepted["work_item_completion_updated"])
        self.assertEqual(
            self.store.get_projection(self.work_item_id)["state"],
            "in_progress",
        )
        candidate_id = accepted["state_candidate"]["candidate_id"]
        candidate = next(
            item
            for item in self.store.list_candidates()
            if item["candidate_id"] == candidate_id
        )
        self.assertEqual(candidate["status"], "pending")
        applied = self.service.approve_and_apply_completion_candidate(
            candidate_id=candidate_id,
            authority=self.authority,
            user_actor_id="actor:local-user",
            at=T8,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(applied["status"], "projection_updated")
        self.assertEqual(
            self.store.get_projection(self.work_item_id)["state"],
            "completed",
        )

    def test_failed_result_and_failed_verification_remain_audited(self) -> None:
        self.running_delegation()
        result = self.service.record_execution_result(
            delegation_id=self.delegation_id,
            outcome_id="outcome:v9-failed-test",
            runtime_run_id="runtime-run:v9-failed-test",
            result="failed",
            receipt_digest=hashlib.sha256(b"failed-receipt").hexdigest(),
            artifact_references=("artifact:failure-log",),
            authority=self.authority,
            runtime_actor_id="runtime:v8",
            at=T5,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(
            result["delegation_transition"]["state"], "failed"
        )
        rejected = self.service.verify_outcome(
            outcome_id="outcome:v9-failed-test",
            verification_digest=hashlib.sha256(
                b"failed-verification"
            ).hexdigest(),
            passed=False,
            authority=self.authority,
            verifier_id="runtime:verifier",
            at=T6,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(
            rejected["delegation_transition"]["state"], "rejected"
        )
        outcome = self.store.list_outcomes(
            delegation_id=self.delegation_id
        )[0]
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(
            outcome["artifact_references"], ["artifact:failure-log"]
        )
        self.assertEqual(
            self.store.get_projection(self.work_item_id)["state"],
            "in_progress",
        )

    def test_outcome_ledger_stores_only_runtime_and_artifact_references(self) -> None:
        self.running_delegation()
        self.service.record_execution_result(
            delegation_id=self.delegation_id,
            outcome_id="outcome:v9-reference-test",
            runtime_run_id="runtime-run:v9-reference-test",
            result="completed",
            receipt_digest=hashlib.sha256(b"reference-receipt").hexdigest(),
            artifact_references=(
                "artifact:report-digest",
                "artifact:test-log-digest",
            ),
            authority=self.authority,
            runtime_actor_id="runtime:v8",
            at=T5,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        row = self.store.list_outcomes()[0]
        self.assertEqual(
            set(row),
            {
                "outcome_id",
                "delegation_id",
                "runtime_run_id",
                "receipt_digest",
                "verification_digest",
                "artifact_references",
                "status",
                "outcome",
                "outcome_digest",
                "current_event_id",
            },
        )
        self.assertEqual(
            row["outcome_digest"], canonical_digest(row["outcome"])
        )
        self.assertNotIn("artifact_content", row["outcome"])

    def test_tampered_contract_and_outcome_fail_closed(self) -> None:
        self.running_delegation()
        self.service.record_execution_result(
            delegation_id=self.delegation_id,
            outcome_id="outcome:v9-tamper-test",
            runtime_run_id="runtime-run:v9-tamper-test",
            result="completed",
            receipt_digest=hashlib.sha256(b"tamper-receipt").hexdigest(),
            artifact_references=("artifact:test",),
            authority=self.authority,
            runtime_actor_id="runtime:v8",
            at=T5,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE outcome_records SET canonical_outcome='{}'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            OperationalStateError, "outcome_record_tampered"
        ):
            self.store.list_outcomes()


if __name__ == "__main__":
    unittest.main()
