from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    COGNITIVE_SCHEMA_MARKER,
    _COGNITIVE_DDL,
    _COGNITIVE_OBJECTS,
    _parse_sql_statements,
    apply_cognitive_state_migration,
    verify_cognitive_state_schema,
)
from memory.cognitive_objective import build_objective_contract
from memory.cognitive_state_store import CognitiveStateStore
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
    schema_fingerprint,
)


T0 = "2026-07-31T00:00:00.000000Z"
T1 = "2026-07-31T00:01:00.000000Z"
T2 = "2026-07-31T00:02:00.000000Z"
T3 = "2026-07-31T00:03:00.000000Z"
T4 = "2026-07-31T00:04:00.000000Z"
T5 = "2026-07-31T01:00:00.000000Z"
T6 = "2026-07-31T02:00:00.000000Z"
T_FUTURE = "2099-01-01T00:00:00.000000Z"


class CognitiveObjectiveStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        self.operational = OperationalStateStore(self.database)
        self.anchor = SourceAnchor(
            anchor_id="anchor:v10-objective-user",
            source_kind="user_declaration",
            locator="user-declaration:v10-objective",
            evidence_digest="a" * 64,
            observed_at=T1,
            privacy_scope="private_content",
        )
        self.owner_id = "owner:v10-objective-test"
        self.lease_token = "v10-objective-test-lease-token"
        self.trusted_time = T3

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _seal_contract(payload: dict) -> dict:
        contract = dict(payload)
        contract["canonical_digest"] = canonical_digest(contract)
        return contract

    def contract(
        self,
        *,
        objective_id: str = "objective:test-one",
        version: int = 1,
        agency_level: str = "plan",
        clarification_required: bool = False,
        interpreted_goal: str = "Verify one bounded objective.",
        source_anchor: SourceAnchor | None = None,
    ) -> dict:
        anchor = source_anchor or self.anchor
        agency_selection = {
            "selected_level": agency_level,
            "selected_by": "user",
            "maximum_authorized_level": agency_level,
            "source_anchor_id": anchor.anchor_id,
            "source_anchor_digest": anchor.evidence_digest,
        }
        agency_selection["selection_digest"] = canonical_digest(
            agency_selection
        )
        return self._seal_contract(
            {
                "objective_id": objective_id,
                "version": version,
                "project_id": "project:yunos",
                "user_utterance": "Please verify one bounded objective.",
                "interpreted_goal": interpreted_goal,
                "agency_level": agency_level,
                "agency_selection": agency_selection,
                "cognitive_stance": "continue",
                "success_criteria": ["The objective is deterministically stored."],
                "prohibited_outcomes": ["No production writes."],
                "risk_tolerance": "low",
                "time_constraints": [],
                "resource_constraints": [],
                "assumptions": [],
                "ambiguities": (
                    ["The target must be clarified."]
                    if clarification_required
                    else []
                ),
                "clarification_required": clarification_required,
                "context_requirements": [],
                "acceptance_policy": {
                    "mode": "explicit_user",
                    "automatic_allowed": False,
                    "requires_user_event": True,
                    "deterministic_criteria": [],
                    "maximum_risk": "low",
                    "require_system_verification": True,
                    "require_reversible": True,
                    "require_no_conflicts": True,
                    "require_no_overreach": True,
                    "require_no_unresolved_warnings": True,
                    "preauthorization_reference": None,
                    "preauthorization_digest": None,
                },
                "authority_request": {
                    "requested": False,
                    "read_roots": [],
                    "write_roots": [],
                    "capabilities": [],
                    "commands": [],
                    "risk_ceiling": "low",
                    "commit_permission": False,
                    "merge_permission": False,
                    "tag_permission": False,
                    "push_permission": False,
                    "network_permission": False,
                    "database_permission": "readonly",
                },
                "source_anchors": [anchor.to_dict()],
            }
        )

    def authority(
        self,
        *,
        authority_id: str = "authority:v10-objective-user",
        issued_by: str = "user:yunos",
        expires_at: str = T5,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at=expires_at,
            target_project_id="project:yunos",
            allowed_event_types=(
                "decision_approved",
                "candidate_create",
                "candidate_decision",
            ),
            read_scope=("project:yunos",),
            write_scope=("*",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "start_next_version",
            ),
            commit_permission=True,
        )

    def event(
        self,
        contract: dict,
        authority: AuthorityArtifact,
        *,
        suffix: str = "one",
        source_anchor: SourceAnchor | None = None,
    ):
        anchor = source_anchor or self.anchor
        return build_state_event(
            project_id=contract["project_id"],
            entity_id=f"decision:objective-{suffix}",
            entity_type="decision",
            event_type="decision_approved",
            source_kind="declared",
            source_anchor=anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id="user:yunos",
            observed_at=T1,
            effective_at=T2,
            recorded_at=T3,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key=f"idempotency:objective-{suffix}",
            conflict_group_id=None,
            payload={
                "state": "approved",
                "objective_id": contract["objective_id"],
                "objective_version": contract["version"],
                "objective_digest": contract["canonical_digest"],
            },
            privacy_scope="private_content",
            authority_digest=authority.authority_digest,
        )

    def migrate(self) -> CognitiveStateStore:
        apply_cognitive_state_migration(self.database, created_at=T1)
        return CognitiveStateStore(
            self.database,
            trusted_clock=lambda: self.trusted_time,
        )

    def acquire(self) -> None:
        self.operational.acquire_writer_lease(
            owner_id=self.owner_id,
            token=self.lease_token,
            acquired_at=T1,
            expires_at=T5,
        )

    def commit(
        self,
        store: CognitiveStateStore,
        contract: dict,
        *,
        suffix: str = "one",
        approval_id: str | None = None,
    ) -> dict:
        authority = self.authority()
        return store.commit_objective(
            contract=contract,
            authority=authority,
            event=self.event(contract, authority, suffix=suffix),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
            approval_id=approval_id,
            created_at=T3,
        )

    def test_migration_preserves_9001_and_adds_verified_10001(self) -> None:
        result = apply_cognitive_state_migration(self.database, created_at=T1)
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(COGNITIVE_SCHEMA_MARKER, result["marker"])
        self.assertEqual(COGNITIVE_SCHEMA_GENERATION, result["schema_generation"])

        old_status = OperationalStateStore(self.database).read_status()
        new_status = CognitiveStateStore(self.database).read_status()
        self.assertEqual(9001, old_status["schema_version"])
        self.assertEqual(COGNITIVE_SCHEMA_MARKER, new_status["marker"])
        self.assertEqual(COGNITIVE_SCHEMA_GENERATION, new_status["schema_generation"])
        self.assertEqual(old_status["schema_digest"], new_status["schema_digest"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(old_status["schema_digest"], schema_fingerprint(connection))
            names = {name for _object_type, name in _COGNITIVE_OBJECTS}
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                )
                if row[1] in names
            }
            self.assertEqual(45, len(objects))
            self.assertEqual(_COGNITIVE_OBJECTS, objects)

    def test_migration_is_idempotent_and_rejects_active_writer(self) -> None:
        self.acquire()
        with self.assertRaisesRegex(
            OperationalStateError, "cognitive_migration_active_writer_lease"
        ):
            apply_cognitive_state_migration(self.database, created_at=T2)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )

        self.operational.release_writer_lease(
            owner_id=self.owner_id,
            token=self.lease_token,
            released_at=T2,
        )
        first = apply_cognitive_state_migration(self.database, created_at=T2)
        second = apply_cognitive_state_migration(self.database, created_at=T3)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["schema_digest"], second["schema_digest"])

    def test_migration_failure_rolls_back_and_production_is_forbidden(self) -> None:
        before = self.operational.read_status()["schema_digest"]
        with patch(
            "memory.cognitive_state_migration._COGNITIVE_DDL",
            (_COGNITIVE_DDL[0], "CREATE TABLE intentionally_broken("),
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                apply_cognitive_state_migration(self.database, created_at=T2)
        self.assertEqual(before, self.operational.read_status()["schema_digest"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )
        with patch(
            "memory.cognitive_state_migration.is_production_operational_path",
            return_value=True,
        ):
            with self.assertRaisesRegex(
                OperationalStateError,
                "cognitive_production_migration_forbidden",
            ):
                apply_cognitive_state_migration(self.database, created_at=T2)

    def test_migration_parser_handles_trigger_bodies_and_rejects_truncation(
        self,
    ) -> None:
        statements = _parse_sql_statements(
            """
            -- A semicolon in a literal must not split a statement.
            CREATE TABLE parser_probe(value TEXT);
            CREATE TRIGGER parser_probe_guard
            BEFORE INSERT ON parser_probe
            BEGIN
                SELECT CASE
                    WHEN NEW.value=';'
                    THEN RAISE(ABORT,'semi;colon')
                END;
            END;
            """
        )
        self.assertEqual(2, len(statements))
        with closing(sqlite3.connect(":memory:")) as connection:
            for statement in statements:
                connection.execute(statement)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "semi;colon"):
                connection.execute(
                    "INSERT INTO parser_probe(value) VALUES(';')"
                )
        with self.assertRaisesRegex(
            OperationalStateError,
            "cognitive_schema_resource_incomplete",
        ):
            _parse_sql_statements("CREATE TABLE truncated(")

    def test_partial_schema_and_schema_digest_drift_fail_closed(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(_COGNITIVE_DDL[0])
            connection.commit()
        with self.assertRaisesRegex(
            OperationalStateError,
            "cognitive_partial_schema_detected",
        ):
            apply_cognitive_state_migration(self.database, created_at=T2)
        with closing(sqlite3.connect(self.database)) as connection:
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                )
                if row[1] in {
                    name for _object_type, name in _COGNITIVE_OBJECTS
                }
            }
            self.assertEqual({("table", "cognitive_schema")}, objects)

        other_database = self.root / "operational-drift.db"
        initialize_operational_database(other_database, created_at=T0)
        apply_cognitive_state_migration(other_database, created_at=T1)
        with closing(sqlite3.connect(other_database)) as connection:
            connection.execute(
                "CREATE INDEX objective_contracts_unexpected "
                "ON objective_contracts(project_id)"
            )
            drifted_digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? "
                "WHERE singleton=1",
                (drifted_digest,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            OperationalStateError,
            "cognitive_schema_drift",
        ):
            verify_cognitive_state_schema(other_database)

    def test_unexpected_resource_effect_rolls_back_the_whole_transaction(
        self,
    ) -> None:
        before = self.operational.read_status()["schema_digest"]
        unexpected = "CREATE TABLE cognitive_unexpected(value TEXT)"
        with patch(
            "memory.cognitive_state_migration._COGNITIVE_DDL",
            _COGNITIVE_DDL + (unexpected,),
        ):
            with self.assertRaisesRegex(
                OperationalStateError,
                "cognitive_schema_resource_effect_mismatch",
            ):
                apply_cognitive_state_migration(self.database, created_at=T2)
        self.assertEqual(before, self.operational.read_status()["schema_digest"])
        with closing(sqlite3.connect(self.database)) as connection:
            objects = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='cognitive_unexpected' "
                    "OR name='cognitive_schema'"
                )
            }
            self.assertEqual(set(), objects)

    def test_objective_rows_are_append_only(self) -> None:
        store = self.migrate()
        self.acquire()
        self.commit(store, self.contract())
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append_only"):
                connection.execute(
                    "UPDATE objective_contracts SET project_id='project:other'"
                )
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append_only"):
                connection.execute("DELETE FROM objective_contracts")

    def test_atomic_commit_receipt_idempotency_and_restart(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        first = self.commit(store, contract)
        second = self.commit(store, contract)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        original_authority = self.authority()
        original_event = self.event(contract, original_authority)
        forged_authority = self.authority(
            authority_id="authority:v10-objective-forged-replay",
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "event_id_mismatch",
        ):
            store.commit_objective(
                contract=contract,
                authority=forged_authority,
                event=original_event,
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
                created_at=T3,
            )
        self.assertEqual(first["receipt_digest"], second["receipt_digest"])

        restarted = CognitiveStateStore(self.database)
        record = restarted.get_objective(contract["objective_id"], 1)
        self.assertEqual(contract, record["contract"])
        self.assertEqual(contract["canonical_digest"], record["canonical_digest"])
        self.assertEqual(record, restarted.get_objective(contract["objective_id"]))
        self.assertEqual(
            [record],
            restarted.list_objectives(project_id="project:yunos", limit=1),
        )
        status = restarted.read_status()
        self.assertEqual(1, status["objective_count"])
        self.assertEqual(1, status["operational_counts"]["state_events"])
        self.assertEqual(1, status["operational_counts"]["state_receipts"])

    def test_missing_approval_rolls_back_event_and_receipt(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        before = OperationalStateStore(self.database).read_status()["counts"]
        with self.assertRaisesRegex(
            OperationalStateError, "objective_approval_not_found"
        ):
            self.commit(
                store,
                contract,
                approval_id="approval:missing-objective-candidate",
            )
        after = OperationalStateStore(self.database).read_status()["counts"]
        self.assertEqual(before["state_events"], after["state_events"])
        self.assertEqual(before["state_receipts"], after["state_receipts"])
        self.assertIsNone(store.get_objective(contract["objective_id"], 1))

    def test_version_requires_predecessor_and_collision_fails_closed(self) -> None:
        store = self.migrate()
        self.acquire()
        version_two = self.contract(version=2)
        with self.assertRaisesRegex(
            OperationalStateError, "objective_version_predecessor_missing"
        ):
            self.commit(store, version_two, suffix="v2")

        version_one = self.contract()
        self.commit(store, version_one)
        changed = self.contract(interpreted_goal="A conflicting interpretation.")
        before = OperationalStateStore(self.database).read_status()["counts"]
        with self.assertRaisesRegex(
            OperationalStateError, "objective_version_collision"
        ):
            self.commit(store, changed, suffix="collision")
        after = OperationalStateStore(self.database).read_status()["counts"]
        self.assertEqual(before["state_events"], after["state_events"])
        self.assertEqual(before["state_receipts"], after["state_receipts"])

    def test_contract_digest_anchor_and_event_are_verified(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        forged = dict(contract)
        forged["interpreted_goal"] = "Forged after sealing."
        with self.assertRaisesRegex(
            OperationalStateError, "objective_digest_mismatch"
        ):
            self.commit(store, forged, suffix="forged")

        extra = self._seal_contract(
            {
                key: value
                for key, value in contract.items()
                if key != "canonical_digest"
            }
            | {"unexpected_field": "must remain closed"}
        )
        with self.assertRaisesRegex(
            OperationalStateError, "invalid_objective_contract"
        ):
            self.commit(store, extra, suffix="extra-field")

        other_anchor = SourceAnchor(
            anchor_id="anchor:v10-objective-other",
            source_kind="user_declaration",
            locator="user-declaration:v10-objective-other",
            evidence_digest="b" * 64,
            observed_at=T1,
            privacy_scope="private_content",
        )
        authority = self.authority()
        with self.assertRaisesRegex(
            OperationalStateError, "objective_source_anchor_mismatch"
        ):
            store.commit_objective(
                contract=contract,
                authority=authority,
                event=self.event(
                    contract,
                    authority,
                    suffix="anchor-mismatch",
                    source_anchor=other_anchor,
                ),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
                created_at=T3,
            )

        wrong_event = self.event(contract, authority, suffix="wrong-event")
        wrong_payload = dict(wrong_event.to_dict())
        wrong_payload["canonical_payload"] = dict(
            wrong_payload["canonical_payload"],
            objective_version=99,
        )
        wrong_payload["payload_digest"] = canonical_digest(
            wrong_payload["canonical_payload"]
        )
        with self.assertRaisesRegex(
            OperationalStateError, "objective_event_binding_mismatch"
        ):
            store.commit_objective(
                contract=contract,
                authority=authority,
                event=type(wrong_event).from_dict(wrong_payload),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
                created_at=T3,
            )

    def test_clarification_contract_is_candidate_only_and_agent_cannot_approve(
        self,
    ) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(clarification_required=True)
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertEqual("pending", candidate["status"])
        listed = store.list_objective_candidates(limit=1)
        self.assertEqual(candidate["candidate_id"], listed[0]["candidate_id"])
        self.assertEqual(
            contract["canonical_digest"],
            listed[0]["candidate"]["canonical_digest"],
        )
        self.assertIsNone(store.get_objective(contract["objective_id"], 1))

        agent = self.authority(
            authority_id="authority:v10-objective-agent",
            issued_by="agent:v10-objective",
        )
        with self.assertRaisesRegex(
            OperationalStateError, "agent_cannot_approve_candidate"
        ):
            store.decide_objective_candidate(
                candidate_id=candidate["candidate_id"],
                decision="approve",
                authority=agent,
                recorded_at=T3,
            )

        user = self.authority()
        with self.assertRaisesRegex(
            OperationalStateError, "objective_candidate_digest_mismatch"
        ):
            store.decide_objective_candidate(
                candidate_id=candidate["candidate_id"],
                decision="approve",
                authority=user,
                recorded_at=T3,
                expected_candidate_digest="0" * 64,
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )
        approved = store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="approve",
            authority=user,
            recorded_at=T3,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertEqual("approved", approved["status"])

        with self.assertRaisesRegex(
            OperationalStateError, "objective_clarification_required"
        ):
            self.commit(
                store,
                contract,
                suffix="clarification",
                approval_id=approved["approval_id"],
            )

    def test_resolved_contract_requires_its_own_exact_approval(self) -> None:
        store = self.migrate()
        self.acquire()
        candidate_contract = self.contract(clarification_required=True)
        ambiguous_candidate = store.create_objective_candidate(
            contract=candidate_contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        ambiguous_approval = store.decide_objective_candidate(
            candidate_id=ambiguous_candidate["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=ambiguous_candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        resolved = self.contract(
            interpreted_goal="Verify the clarified bounded objective."
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_approval_contract_mismatch",
        ):
            self.commit(
                store,
                resolved,
                suffix="ambiguous-approval",
                approval_id=ambiguous_approval["approval_id"],
            )

        resolved_candidate = store.create_objective_candidate(
            contract=resolved,
            source_anchor=self.anchor,
            confidence_ppm=1_000_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        resolved_approval = store.decide_objective_candidate(
            candidate_id=resolved_candidate["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=resolved_candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        result = self.commit(
            store,
            resolved,
            suffix="resolved",
            approval_id=resolved_approval["approval_id"],
        )
        self.assertEqual(resolved_approval["approval_id"], result["approval_id"])
        record = store.get_objective(resolved["objective_id"], 1)
        self.assertEqual(resolved_approval["approval_id"], record["approval_id"])

    def test_approval_cannot_be_rebound_to_another_objective(self) -> None:
        store = self.migrate()
        self.acquire()
        other = self.contract(
            objective_id="objective:other",
            clarification_required=True,
        )
        candidate = store.create_objective_candidate(
            contract=other,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        approved = store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        contract = self.contract()
        before = self.operational.read_status()["counts"]
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_approval_contract_mismatch",
        ):
            self.commit(
                store,
                contract,
                suffix="approval-rebind",
                approval_id=approved["approval_id"],
            )
        after = self.operational.read_status()["counts"]
        self.assertEqual(before["state_events"], after["state_events"])
        self.assertEqual(before["state_receipts"], after["state_receipts"])

    def test_revoked_approval_keeps_committed_objective_history_readable(
        self,
    ) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=1_000_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        approved = store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T2,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.commit(
            store,
            contract,
            suffix="revoke-history",
            approval_id=approved["approval_id"],
        )
        revoked = store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="revoke",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertEqual("rejected", revoked["status"])
        record = store.get_objective(contract["objective_id"], 1)
        self.assertEqual(approved["approval_id"], record["approval_id"])
        self.assertEqual("rejected", record["approval_current_status"])

    def test_revoked_approval_cannot_authorize_new_objective_commit(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=1_000_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        approved = store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T2,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        store.decide_objective_candidate(
            candidate_id=candidate["candidate_id"],
            decision="revoke",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=candidate["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_approval_not_current",
        ):
            self.commit(
                store,
                contract,
                suffix="revoked-before-commit",
                approval_id=approved["approval_id"],
            )

    def test_consult_contract_cannot_be_authoritatively_persisted(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(agency_level="consult")
        with self.assertRaisesRegex(
            OperationalStateError,
            "consult_objective_persistence_denied",
        ):
            self.commit(store, contract, suffix="consult")
        self.assertIsNone(store.get_objective(contract["objective_id"], 1))

    def test_consult_contract_cannot_create_candidate_state(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(agency_level="consult")
        before = self.operational.read_status()["counts"]
        with closing(self.operational._connect()) as connection:
            anchors_before = connection.execute(
                "SELECT COUNT(*) FROM state_source_anchors"
            ).fetchone()[0]
        with self.assertRaisesRegex(
            OperationalStateError,
            "consult_objective_candidate_persistence_denied",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T2,
                expires_at=T5,
                authority=self.authority(),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )
        after = self.operational.read_status()["counts"]
        self.assertEqual(before["state_candidates"], after["state_candidates"])
        with closing(self.operational._connect()) as connection:
            anchors_after = connection.execute(
                "SELECT COUNT(*) FROM state_source_anchors"
            ).fetchone()[0]
        self.assertEqual(anchors_before, anchors_after)

    def test_shared_system_approval_cannot_authorize_objective_commit(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=900_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        system_authority = self.authority(
            authority_id="authority:v10-objective-system",
            issued_by="system:auto",
        )
        approval = self.operational.decide_candidate(
            candidate_id=candidate["candidate_id"],
            decision="approve",
            authority=system_authority,
            recorded_at=T3,
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
            enforce_authority=True,
            enforce_not_expired=True,
            expected_candidate_digest=candidate["candidate_digest"],
        )
        before = self.operational.read_status()["counts"]
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_approval_authority_mismatch",
        ):
            self.commit(
                store,
                contract,
                suffix="system-approval",
                approval_id=approval["approval_id"],
            )
        after = self.operational.read_status()["counts"]
        self.assertEqual(before["state_events"], after["state_events"])
        self.assertEqual(before["state_receipts"], after["state_receipts"])

    def test_system_authority_cannot_commit_objective_directly(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        authority = self.authority(
            authority_id="authority:v10-objective-system-commit",
            issued_by="system:auto",
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_commit_requires_user_authority",
        ):
            store.commit_objective(
                contract=contract,
                authority=authority,
                event=self.event(
                    contract,
                    authority,
                    suffix="system-commit",
                ),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
                created_at=T3,
            )

    def test_objective_event_actor_must_equal_authority_issuer(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        authority = self.authority()
        event_payload = self.event(
            contract,
            authority,
            suffix="actor-mismatch",
        ).to_dict()
        event_payload["actor_id"] = "user:another"
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_event_actor_authority_mismatch",
        ):
            store.commit_objective(
                contract=contract,
                authority=authority,
                event=type(self.event(contract, authority)).from_dict(
                    event_payload
                ),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
                created_at=T3,
            )

    def test_objective_event_recorded_at_uses_trusted_clock(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract()
        authority = self.authority()
        event_payload = self.event(
            contract,
            authority,
            suffix="trusted-recorded-at",
        ).to_dict()
        event_payload["recorded_at"] = T6
        event = type(self.event(contract, authority)).from_dict(event_payload)
        result = store.commit_objective(
            contract=contract,
            authority=authority,
            event=event,
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
            created_at=T1,
        )
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT recorded_at FROM state_events WHERE event_id=?",
                (result["event_id"],),
            ).fetchone()
            objective = connection.execute(
                "SELECT created_at FROM objective_contracts "
                "WHERE objective_id=? AND version=1",
                (contract["objective_id"],),
            ).fetchone()
        self.assertEqual(T3, row["recorded_at"])
        self.assertEqual(T3, objective["created_at"])

    def test_historical_request_time_cannot_revive_expired_authority_or_lease(
        self,
    ) -> None:
        store = self.migrate()
        self.acquire()
        self.trusted_time = T6
        contract = self.contract(clarification_required=True)
        with self.assertRaisesRegex(
            OperationalStateError,
            "authority_expired_or_not_yet_valid",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T1,
                expires_at=None,
                authority=self.authority(),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "writer_lease_expired",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T1,
                expires_at=None,
                authority=self.authority(
                    authority_id="authority:v10-objective-long-lived",
                    expires_at=T_FUTURE,
                ),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )

    def test_candidate_write_requires_authority_and_writer_lease(self) -> None:
        store = self.migrate()
        contract = self.contract(clarification_required=True)
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_candidate_authority_required",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T2,
                expires_at=T5,
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_candidate_writer_lease_required",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T2,
                expires_at=T5,
                authority=self.authority(),
            )
        self.acquire()
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_candidate_create_requires_user_authority",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T2,
                expires_at=T5,
                authority=self.authority(
                    authority_id="authority:v10-objective-agent-create",
                    issued_by="agent:v10-objective",
                ),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )

    def test_candidate_identity_binds_expiry_confidence_and_replay_state(
        self,
    ) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(clarification_required=True)
        first = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        changed_confidence = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=600_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        changed_expiry = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T_FUTURE,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertEqual(
            3,
            len(
                {
                    first["candidate_id"],
                    changed_confidence["candidate_id"],
                    changed_expiry["candidate_id"],
                }
            ),
        )
        self.assertFalse(changed_confidence["idempotent_replay"])
        self.assertFalse(changed_expiry["idempotent_replay"])
        replay = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["candidate_id"], replay["candidate_id"])

    def test_v10_reuses_opaque_v9_source_anchor_without_identity_collision(
        self,
    ) -> None:
        legacy_anchor = SourceAnchor(
            anchor_id="anchor:v9-opaque-locator",
            source_kind="user_declaration",
            locator="  locator:cafe\u0301  ",
            evidence_digest="b" * 64,
            observed_at=T1,
            privacy_scope="private_content",
        )
        self.operational.create_candidate(
            candidate_id="candidate:v9-opaque-anchor",
            project_id="project:yunos",
            entity_id="decision:v9-opaque-anchor",
            entity_type="decision",
            candidate_type="legacy_candidate",
            candidate={"legacy": True},
            confidence_ppm=500_000,
            source_anchor=legacy_anchor,
            created_at=T1,
            expires_at=T5,
        )
        store = self.migrate()
        self.acquire()
        contract = build_objective_contract(
            user_utterance="Create one bounded plan.",
            interpreted_goal="Create one bounded plan without execution.",
            project_id="project:yunos",
            source_anchors=(legacy_anchor,),
            success_criteria=("The plan remains read-only.",),
            agency_level="plan",
            agency_selected_by="user",
            maximum_authorized_agency_level="plan",
        )
        candidate = store.create_objective_candidate(
            contract=contract.to_dict(),
            source_anchor=contract.source_anchors[0],
            confidence_ppm=500_000,
            created_at=T2,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertEqual("pending", candidate["status"])
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT canonical_json FROM state_source_anchors "
                "WHERE anchor_id=?",
                (legacy_anchor.anchor_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("  locator:café  ", row["canonical_json"])

    def test_candidate_full_validation_occurs_inside_decision_transaction(
        self,
    ) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(clarification_required=True)
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute(
                "UPDATE state_candidates SET entity_type='task' "
                "WHERE candidate_id=?",
                (candidate["candidate_id"],),
            )
            connection.commit()
        with self.assertRaisesRegex(
            OperationalStateError,
            "objective_candidate_record_invalid",
        ):
            store.decide_objective_candidate(
                candidate_id=candidate["candidate_id"],
                decision="approve",
                authority=self.authority(),
                recorded_at=T2,
                expected_candidate_digest=candidate["candidate_digest"],
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM state_candidates WHERE candidate_id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            approvals = connection.execute(
                "SELECT COUNT(*) FROM state_approvals WHERE candidate_id=?",
                (candidate["candidate_id"],),
            ).fetchone()[0]
        self.assertEqual("pending", row["status"])
        self.assertEqual(0, approvals)

    def test_candidate_expiry_is_atomic_and_replay_keeps_decision(self) -> None:
        store = self.migrate()
        self.acquire()
        contract = self.contract(clarification_required=True)
        self.trusted_time = T1
        candidate = store.create_objective_candidate(
            contract=contract,
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T2,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "candidate_expired",
        ):
            self.trusted_time = T2
            store.decide_objective_candidate(
                candidate_id=candidate["candidate_id"],
                decision="approve",
                authority=self.authority(),
                recorded_at=T2,
                expected_candidate_digest=candidate["candidate_digest"],
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "candidate_expired",
        ):
            store.create_objective_candidate(
                contract=contract,
                source_anchor=self.anchor,
                confidence_ppm=500_000,
                created_at=T1,
                expires_at=T2,
                authority=self.authority(),
                lease_owner_id=self.owner_id,
                lease_token=self.lease_token,
            )

        self.trusted_time = T3
        fresh = store.create_objective_candidate(
            contract=self.contract(
                objective_id="objective:fresh-candidate",
                clarification_required=True,
            ),
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        approved = store.decide_objective_candidate(
            candidate_id=fresh["candidate_id"],
            decision="approve",
            authority=self.authority(),
            recorded_at=T3,
            expected_candidate_digest=fresh["candidate_digest"],
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        replay = store.create_objective_candidate(
            contract=self.contract(
                objective_id="objective:fresh-candidate",
                clarification_required=True,
            ),
            source_anchor=self.anchor,
            confidence_ppm=500_000,
            created_at=T1,
            expires_at=T5,
            authority=self.authority(),
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual("approved", replay["status"])
        self.assertEqual("approved", approved["status"])


if __name__ == "__main__":
    unittest.main()
