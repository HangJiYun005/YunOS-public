from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from memory.context_assembly import make_approved_profile_item
from memory.operational_state_acquisition import (
    AUTHORITATIVE_PROPOSAL,
    CANDIDATE_PROPOSAL,
    ApprovedMemoryAdapter,
    DatabaseObservationAdapter,
    GitObservationAdapter,
    RuntimeOutcomeAdapter,
    StateReconciler,
    UserDeclarationAdapter,
    freshness_for,
    source_diagnostic,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
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
T5 = "2026-07-31T00:05:00Z"


class StateAcquisitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.project_id = "project:yunos"
        self.entity_id = "project:yunos"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def authority(
        self,
        *,
        authority_id: str = "authority:state-acquisition-test",
        issued_by: str = "actor:local-user",
        event_types: tuple[str, ...] = (
            "project_declared",
            "project_observed",
            "project_revoked",
            "project_superseded",
            "outcome_reported",
            "outcome_verified",
            "outcome_accepted",
            "candidate_decision",
            "candidate_expire",
        ),
        write_scope: tuple[str, ...] = ("*",),
        commit_permission: bool = True,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at="2026-08-01T00:00:00Z",
            target_project_id=self.project_id,
            allowed_event_types=event_types,
            read_scope=("repo:metadata", "database:metadata"),
            write_scope=write_scope,
            prohibited_operations=("push", "create-next-node"),
            commit_permission=commit_permission,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )

    def declaration(
        self,
        *,
        action: str = "declare",
        state: str = "active",
        statement: str = "YunOS is the active local project.",
        entity_id: str | None = None,
    ):
        return UserDeclarationAdapter.adapt(
            project_id=self.project_id,
            entity_id=entity_id or self.entity_id,
            entity_type="project",
            action=action,
            state=state,
            statement=statement,
            declared_at=T1,
            user_actor_id="actor:local-user",
        )

    def test_user_declaration_builds_authoritative_traceable_event(self) -> None:
        proposal = self.declaration()
        event = proposal.to_event(authority=self.authority(), recorded_at=T2)
        self.assertEqual(proposal.proposal_kind, AUTHORITATIVE_PROPOSAL)
        self.assertEqual(event.source_kind, "declared")
        self.assertEqual(event.authority_kind, "user")
        self.assertEqual(event.canonical_payload["state"], "active")
        self.assertEqual(
            event.event_id,
            event.expected_event_id(self.authority().authority_digest),
        )

    def test_user_declaration_requires_explicit_supported_action(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError, "unsupported_user_declaration_action"
        ):
            self.declaration(action="infer")

    def _init_git_repo(self) -> Path:
        repository = self.root / "repo"
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(repository)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "user.email",
                "test.invalid",
            ],
            check=True,
        )
        (repository / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "README.md"], check=True
        )
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2026-07-31T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-07-31T00:00:00Z",
            }
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-m", "baseline"],
            check=True,
            capture_output=True,
            env=environment,
        )
        subprocess.run(
            ["git", "-C", str(repository), "tag", "v1.0.0"], check=True
        )
        return repository

    def test_git_observation_covers_head_tag_worktree_staging_untracked(self) -> None:
        repository = self._init_git_repo()
        (repository / "README.md").write_text("changed\n", encoding="utf-8")
        (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "staged.txt"], check=True
        )
        (repository / "untracked.txt").write_text(
            "untracked\n", encoding="utf-8"
        )
        adapter = GitObservationAdapter(
            repository, repository_alias="repo:test"
        )
        first = adapter.observe(project_id=self.project_id, observed_at=T1)
        second = adapter.observe(project_id=self.project_id, observed_at=T1)
        evidence = first["evidence"]
        self.assertEqual(first["evidence_digest"], second["evidence_digest"])
        self.assertEqual(evidence["branch"], "main")
        self.assertEqual(evidence["head_tags"], ["v1.0.0"])
        self.assertFalse(evidence["worktree"]["clean"])
        self.assertEqual(evidence["staging"]["count"], 1)
        self.assertEqual(evidence["untracked"]["count"], 1)
        self.assertNotIn(str(repository), str(first))

    def test_git_observation_proposal_is_deterministic_observation(self) -> None:
        repository = self._init_git_repo()
        proposal = GitObservationAdapter(
            repository, repository_alias="repo:test"
        ).project_proposal(project_id=self.project_id, observed_at=T1)
        self.assertEqual(proposal.authority_kind, "deterministic_observation")
        self.assertEqual(proposal.payload["head_tags"], ["v1.0.0"])
        self.assertEqual(proposal.payload["state"], "active")

    def _init_database(self) -> Path:
        database = self.root / "source.db"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                "PRAGMA foreign_keys=ON;"
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(id INTEGER PRIMARY KEY,"
                "parent_id INTEGER REFERENCES parent(id));"
                "INSERT INTO parent VALUES(1);"
                "INSERT INTO child VALUES(1,1);"
            )
            connection.commit()
        finally:
            connection.close()
        return database

    def test_database_observation_is_readonly_source_backed_and_counted(self) -> None:
        database = self._init_database()
        before = canonical_digest(database.read_bytes().hex())
        observation = DatabaseObservationAdapter(
            database, database_alias="database:test"
        ).observe(
            project_id=self.project_id,
            observed_at=T1,
            count_tables=("parent", "child"),
        )
        after = canonical_digest(database.read_bytes().hex())
        evidence = observation["evidence"]
        self.assertEqual(before, after)
        self.assertEqual(observation["status"], "observed")
        self.assertEqual(evidence["table_counts"], {"child": 1, "parent": 1})
        self.assertEqual(evidence["integrity_check"], "ok")
        self.assertEqual(evidence["foreign_key_violations"], 0)
        self.assertTrue(evidence["readonly_state"]["query_only"])
        self.assertFalse(evidence["path_identity"]["absolute_path_persisted"])
        self.assertNotIn(str(database), str(observation))

    def test_database_observation_rejects_unknown_count_table(self) -> None:
        database = self._init_database()
        with self.assertRaisesRegex(
            OperationalStateError, "database_count_table_missing"
        ):
            DatabaseObservationAdapter(
                database, database_alias="database:test"
            ).observe(
                project_id=self.project_id,
                observed_at=T1,
                count_tables=("missing",),
            )

    def test_runtime_completed_is_not_user_accepted(self) -> None:
        proposal = RuntimeOutcomeAdapter.adapt(
            project_id=self.project_id,
            entity_id="outcome:test-runtime",
            receipt={"run_id": "run:test", "result": "completed"},
            verification={"result": "passed", "checks": 3},
            observed_at=T1,
        )
        self.assertEqual(proposal.payload["state"], "reported")
        self.assertFalse(proposal.payload["accepted"])
        self.assertEqual(
            proposal.reason_code,
            "verified_runtime_outcome_not_user_accepted",
        )

    def test_runtime_acceptance_requires_verified_predecessor_and_user(self) -> None:
        authority = self.authority(
            authority_id="authority:runtime-acceptance"
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "runtime_acceptance_requires_verified_predecessor",
        ):
            RuntimeOutcomeAdapter.adapt(
                project_id=self.project_id,
                entity_id="outcome:test-runtime",
                receipt={"run_id": "run:test", "result": "completed"},
                verification={"result": "passed"},
                observed_at=T2,
                user_acceptance=authority,
            )
        accepted = RuntimeOutcomeAdapter.adapt(
            project_id=self.project_id,
            entity_id="outcome:test-runtime",
            receipt={"run_id": "run:test", "result": "completed"},
            verification={"result": "passed"},
            observed_at=T2,
            user_acceptance=authority,
            previous_state="verified",
            supersedes_event_id="event:verified-runtime",
        )
        self.assertEqual(accepted.payload["state"], "accepted")
        self.assertTrue(accepted.payload["accepted"])

    def test_agent_cannot_accept_runtime_outcome(self) -> None:
        authority = self.authority(
            authority_id="authority:agent-runtime-acceptance",
            issued_by="agent:self",
        )
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_accept_runtime_outcome"
        ):
            RuntimeOutcomeAdapter.adapt(
                project_id=self.project_id,
                entity_id="outcome:test-runtime",
                receipt={"run_id": "run:test", "result": "completed"},
                verification={"result": "passed"},
                observed_at=T2,
                user_acceptance=authority,
                previous_state="verified",
                supersedes_event_id="event:verified-runtime",
            )

    def test_approved_memory_only_admits_user_approved_project_relevant_items(
        self,
    ) -> None:
        relevant = make_approved_profile_item(
            "Keep YunOS normal web access read-only.",
            kind="constraint",
            topics=("yunos", "readonly"),
            approval_id="approval-yunos-readonly",
            approved_at=T0,
        )
        unrelated = make_approved_profile_item(
            "Use spaced repetition for language study.",
            kind="goal",
            topics=("language", "study"),
            approval_id="approval-language-study",
            approved_at=T0,
        )
        proposals = ApprovedMemoryAdapter.adapt(
            project_id=self.project_id,
            approved_items=(relevant, unrelated),
            project_topics=("yunos",),
            observed_at=T1,
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].payload["memory_kind"], "constraint")
        self.assertEqual(
            proposals[0].reason_code,
            "traceable_project_relevant_approved_memory",
        )

    def test_approved_memory_rejects_forged_approval(self) -> None:
        item = make_approved_profile_item(
            "Keep YunOS read-only.",
            kind="constraint",
            topics=("yunos",),
            approval_id="approval-yunos-readonly",
            approved_at=T0,
        )
        item["approval"]["authority"] = "agent"
        with self.assertRaisesRegex(
            OperationalStateError, "approved_memory_not_user_approved"
        ):
            ApprovedMemoryAdapter.adapt(
                project_id=self.project_id,
                approved_items=(item,),
                project_topics=("yunos",),
                observed_at=T1,
            )

    def test_approved_memory_normalizes_traceable_timezone_to_utc(self) -> None:
        item = make_approved_profile_item(
            "Keep YunOS read-only.",
            kind="constraint",
            topics=("yunos",),
            approval_id="approval-yunos-timezone",
            approved_at="2026-07-31T08:00:00+08:00",
        )
        proposal = ApprovedMemoryAdapter.adapt(
            project_id=self.project_id,
            approved_items=(item,),
            project_topics=("yunos",),
            observed_at=T1,
        )[0]
        self.assertEqual(
            proposal.effective_at, "2026-07-31T00:00:00.000000Z"
        )

    def test_reconciliation_rejects_duplicates_and_preserves_conflict(self) -> None:
        first = self.declaration(statement="YunOS is active.", state="active")
        duplicate = self.declaration(
            statement="YunOS is active.", state="active"
        )
        conflict = self.declaration(
            statement="YunOS is paused.", state="paused"
        )
        plan = StateReconciler().plan((first, duplicate, conflict))
        actions = [action["action"] for action in plan["actions"]]
        self.assertIn("reject_duplicate", actions)
        self.assertIn("create_conflict_group", actions)
        self.assertFalse(plan["agent_auto_resolution_allowed"])
        self.assertIn(
            "conflicting_user_statement", plan["manual_resolution_fields"]
        )

    def test_reconciliation_keeps_candidates_and_source_failures_non_authoritative(
        self,
    ) -> None:
        candidate = replace(
            self.declaration(),
            proposal_kind=CANDIDATE_PROPOSAL,
            source_kind="inferred_candidate",
            authority_kind="candidate_only",
            reason_code="model_inference_requires_review",
        )
        unavailable = source_diagnostic(
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type="project",
            source_kind="service",
            source_alias="service:yunos",
            observed_at=T1,
            unavailable=True,
        )
        plan = StateReconciler().plan((candidate, unavailable))
        self.assertEqual(
            [action["action"] for action in plan["actions"]],
            ["create_candidate", "preserve_diagnostic"],
        )
        with self.assertRaisesRegex(
            OperationalStateError, "proposal_is_not_authoritative"
        ):
            candidate.to_event(authority=self.authority(), recorded_at=T2)

    def test_freshness_recalculation_covers_all_source_states(self) -> None:
        self.assertEqual(
            freshness_for(observed_at=T0, as_of=T1), "fresh"
        )
        self.assertEqual(
            freshness_for(
                observed_at=T0,
                as_of="2026-08-01T12:00:00Z",
            ),
            "aging",
        )
        self.assertEqual(
            freshness_for(
                observed_at=T0,
                as_of="2026-08-08T00:00:00Z",
            ),
            "stale",
        )
        self.assertEqual(
            freshness_for(
                observed_at=T0,
                as_of=T5,
                expires_at=T3,
            ),
            "expired",
        )
        self.assertEqual(
            freshness_for(
                observed_at=T0,
                as_of=T1,
                source_available=False,
            ),
            "unverifiable",
        )

    def test_candidate_expiry_is_explicit_append_only_decision(self) -> None:
        database = self.root / "operational.db"
        initialize_operational_database(database, created_at=T0)
        store = OperationalStateStore(database)
        proposal = self.declaration()
        store.create_candidate(
            candidate_id="candidate:expiring",
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type="project",
            candidate_type="focus",
            candidate={"proposal_digest": proposal.proposal_digest},
            confidence_ppm=600000,
            source_anchor=proposal.source_anchor,
            created_at=T1,
            expires_at=T2,
        )
        before = store.expire_candidates(as_of=T1)
        after = store.expire_candidates(as_of=T3)
        self.assertEqual(before["expired_count"], 0)
        self.assertEqual(after["expired_candidate_ids"], ["candidate:expiring"])
        self.assertEqual(
            store.read_status()["counts"]["state_approvals"], 1
        )
        connection = sqlite3.connect(database)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                connection.execute("DELETE FROM state_approvals")
        finally:
            connection.close()

    def test_readonly_observation_cli_preserves_database(self) -> None:
        database = self._init_database()
        before = canonical_digest(database.read_bytes().hex())
        result = subprocess.run(
            [
                sys.executable,
                str(Path.cwd() / "tools" / "observe_operational_state.py"),
                "database",
                "--database",
                str(database),
                "--database-alias",
                "database:test",
                "--project-id",
                self.project_id,
                "--observed-at",
                T1,
                "--count-table",
                "parent",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "observed")
        self.assertEqual(before, canonical_digest(database.read_bytes().hex()))

    def test_reconciliation_against_store_event_shape_is_duplicate_safe(self) -> None:
        database = self.root / "operational.db"
        initialize_operational_database(database, created_at=T0)
        store = OperationalStateStore(database)
        authority = self.authority()
        proposal = self.declaration()
        event = proposal.to_event(authority=authority, recorded_at=T2)
        lease_token = "state-acquisition-lease-token"
        store.acquire_writer_lease(
            owner_id="owner:state-acquisition",
            token=lease_token,
            acquired_at=T1,
            expires_at=T5,
        )
        preview = store.preview_event(authority=authority, event=event)
        store.commit_preview(
            authority=authority,
            preview=preview,
            lease_owner_id="owner:state-acquisition",
            lease_token=lease_token,
        )
        plan = StateReconciler().plan(
            (proposal,), existing_events=store.list_events()
        )
        self.assertEqual(plan["actions"][0]["action"], "reject_duplicate")


if __name__ == "__main__":
    unittest.main()
