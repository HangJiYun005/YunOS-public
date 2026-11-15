from __future__ import annotations

from copy import deepcopy
import hashlib
from http import HTTPStatus
import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.approved_profile_control import open_production_profile_admin, profile_sidecar_status
from memory.evidence_selection import EvidenceSelectionService
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import MemoryPackageService
from memory.profile_enablement import (
    GATE_B1_AUTHORIZATION_VERSION,
    GATE_B2_AUTHORIZATION_VERSION,
    PROPOSAL_VERSION,
    GateB1ProposalCapability,
    GateB2DecisionCapability,
    ProfileEnablementError,
    ProfileEnablementIdempotencyConflict,
    ProfileEnablementRevisionConflict,
    ProfileEnablementService,
    _ProfileEnablementCore,
    active_profile_enablement_projection,
    build_profile_enablement_review,
    initialize_profile_enablement_sidecar,
    profile_gate_authority_state,
    profile_enablement_sidecar_status,
)
from memory.temporal_memory_candidates import _TemporalCandidateCore
from tools.local_web import AyunOSHandler, REAL_DB_PATH


class ProfileEnablementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.sidecar = self.root / "profile-d1.db"
        with sqlite3.connect(self.source) as conn:
            conn.executescript("""
                CREATE TABLE conversations(
                    conversation_id TEXT PRIMARY KEY, title TEXT, create_time REAL,
                    create_time_iso TEXT, update_time REAL, update_time_iso TEXT
                );
                CREATE TABLE messages(
                    message_id TEXT, conversation_id TEXT, conversation_title TEXT,
                    ordinal INTEGER, role TEXT, create_time REAL, create_time_iso TEXT,
                    text TEXT, memory_space TEXT
                );
            """)
            rows = [
                ("c1", "m1", "memory Preference: I prefer tea."),
                ("c2", "m2", "memory Fact: version one is deployed."),
                ("c3", "m3", "memory Open question: which route?"),
            ]
            for ordinal, (conversation_id, message_id, text) in enumerate(rows, start=1):
                observed = 1735689600.0 + ordinal * 3600
                iso = f"2025-01-01T0{ordinal}:00:00+00:00"
                conn.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                    (conversation_id, f"Candidate {ordinal}", observed, iso, observed, iso),
                )
                conn.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (message_id, conversation_id, f"Candidate {ordinal}", ordinal, "user", observed, iso, text, "general"),
                )
            conn.commit()
        retriever = GraphEnhancedRetrievalService(
            self.source,
            self.root / "semantic-missing.db",
            self.root / "knowledge-missing.db",
            semantic_searcher=lambda *args, **kwargs: {"results": []},
        )
        memory_service = MemoryPackageService(self.source, retriever=retriever)
        selection = EvidenceSelectionService(self.source, memory_package_service=memory_service)
        self.temporal = _TemporalCandidateCore(self.source, selection_service=selection)
        self.query = RetrievalQuery("memory")
        self.package = self.temporal.build(self.query, budget_profile="Large")
        self.core = _ProfileEnablementCore(
            self.temporal.build, self.sidecar,
            anchor_validator=lambda conversation_id, message_id: (conversation_id, message_id) == ("c1", "m1"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self) -> None:
        result = initialize_profile_enablement_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )
        self.assertEqual("initialized", result["status"])

    def propose(self, suffix: str = "one", **overrides):
        values = {
            "original_text": "I prefer compact laptops.",
            "proposal_type": "preference",
            "source_anchors": [{"conversation_id": "c1", "message_id": "m1"}],
            "expected_revision": active_profile_enablement_projection(self.sidecar)["revision"],
            "request_id": f"proposal-request-{suffix}",
            "mutation_id": f"proposal-mutation-{suffix}",
        }
        values.update(overrides)
        authority = self.core.authorize_b1(**values)
        return self.core.propose(authority=authority, **values)

    def decide_proposal(self, **values):
        payload = {
            "subject_origin": "direct_user_proposal",
            "subject_id": values.pop("proposal_id"),
            "query_text": "",
            "budget_profile": "Medium",
            "package_identity": None,
            **values,
        }
        authority = self.core.authorize_b2(**payload)
        return self.core.decide(authority=authority, **payload)

    def decide_candidate(self, query, **values):
        payload = {
            "subject_origin": "v7.2_candidate",
            "subject_id": values.pop("candidate_id"),
            "query_text": query.query,
            "budget_profile": values.pop("budget_profile", "Medium"),
            "package_identity": values.pop("package_identity"),
            **values,
        }
        authority = self.core.authorize_b2(**payload)
        return self.core.decide(authority=authority, **payload)

    def test_direct_proposal_is_distinct_append_only_and_never_auto_approved(self):
        self.initialize()
        before = active_profile_enablement_projection(self.sidecar)
        receipt = self.propose()
        proposal = receipt["proposal"]
        self.assertEqual(PROPOSAL_VERSION, proposal["proposal_version"])
        self.assertEqual("direct_user_proposal", proposal["origin"])
        self.assertEqual("local_user", proposal["proposed_by"])
        self.assertEqual("preference", proposal["proposal_type"])
        self.assertEqual("I prefer compact laptops.", proposal["original_text"])
        self.assertEqual([{"conversation_id": "c1", "message_id": "m1"}], proposal["source_anchors"])
        self.assertTrue(receipt["decision_required"])
        self.assertFalse(receipt["active_profile_changed"])
        after = active_profile_enablement_projection(self.sidecar)
        self.assertEqual(before, after)
        self.assertEqual(receipt, self.propose())

        review = build_profile_enablement_review(self.package, sidecar_path=self.sidecar)
        self.assertEqual(1, len(review["direct_proposals"]))
        entry = review["direct_proposals"][0]
        self.assertEqual("direct_user_proposal", entry["origin"])
        self.assertFalse(entry["active"])
        self.assertEqual([], review["decision_history"])

    def test_manual_decisions_share_revision_and_preserve_original_edit_final_and_history(self):
        self.initialize()
        proposal = self.propose()["proposal"]
        approved = self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="approve", expected_revision=0,
            request_id="decision-request-approve", mutation_id="decision-mutation-approve",
        )
        self.assertEqual(1, approved["revision"])
        item = approved["active_approved_profile"]["items"][0]
        self.assertEqual("I prefer compact laptops.", item["statement"])
        self.assertIs(item["model_visible"], True)
        self.assertEqual(approved, self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="approve", expected_revision=0,
            request_id="decision-request-approve", mutation_id="decision-mutation-approve",
        ))

        edited = self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="edit_and_approve", expected_revision=1,
            request_id="decision-request-edit", mutation_id="decision-mutation-edit",
            edited_statement="I prefer compact, lightweight laptops.", reason="User refined wording.",
        )
        self.assertEqual("I prefer compact, lightweight laptops.", edited["active_approved_profile"]["items"][0]["statement"])
        candidate = next(item for item in self.package["candidates"] if item["candidate_type"] == "fact")
        rejected = self.decide_candidate(
            self.query, budget_profile="Large", package_identity=self.package["package_identity"],
            candidate_id=candidate["candidate_id"], action="reject", expected_revision=2,
            request_id="decision-request-reject", mutation_id="decision-mutation-reject",
        )
        self.assertEqual(3, rejected["revision"])
        deferred = self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="defer", expected_revision=3,
            request_id="decision-request-defer", mutation_id="decision-mutation-defer",
        )
        self.assertEqual([], deferred["active_approved_profile"]["items"])
        review = self.core.review(self.query, budget_profile="Large")
        self.assertEqual(4, len(review["decision_history"]))
        self.assertEqual({"manual"}, {item["decision_source"] for item in review["decision_history"]})
        first, second = review["decision_history"][:2]
        self.assertEqual("I prefer compact laptops.", first["original_text"])
        self.assertEqual("I prefer compact laptops.", first["final_text"])
        self.assertEqual("I prefer compact laptops.", second["original_text"])
        self.assertEqual("I prefer compact, lightweight laptops.", second["edited_statement"])
        self.assertEqual("I prefer compact, lightweight laptops.", second["final_text"])

    def test_candidate_write_rebuilds_source_and_rejects_drift(self):
        self.initialize()
        candidate = next(item for item in self.package["candidates"] if item["candidate_type"] == "preference")
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE messages SET text='source drifted' WHERE message_id='m1'")
            conn.commit()
        with self.assertRaisesRegex(ProfileEnablementError, "stale"):
            self.decide_candidate(
                self.query, budget_profile="Large", package_identity=self.package["package_identity"],
                candidate_id=candidate["candidate_id"], action="approve", expected_revision=0,
                request_id="candidate-request-drift", mutation_id="candidate-mutation-drift",
            )
        self.assertEqual(0, active_profile_enablement_projection(self.sidecar)["revision"])

    def test_revision_concurrency_id_conflicts_and_append_only_boundaries(self):
        self.initialize()
        first = self.propose("first")["proposal"]
        second = self.propose(
            "second", original_text="I prefer quiet keyboards.", source_anchors=[],
        )["proposal"]
        gate = threading.Barrier(2)

        def decide(proposal, index):
            gate.wait()
            try:
                result = self.decide_proposal(
                    proposal_id=proposal["proposal_id"], action="approve", expected_revision=0,
                    request_id=f"tab-request-{index}", mutation_id=f"tab-mutation-{index}",
                )
                return "persisted", result["revision"]
            except ProfileEnablementRevisionConflict as exc:
                return "conflict", exc.current_revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: decide(*args), [(first, 1), (second, 2)]))
        self.assertEqual(["conflict", "persisted"], sorted(item[0] for item in outcomes))
        with self.assertRaises(ProfileEnablementIdempotencyConflict):
            self.propose(
                "conflict",
                original_text="Different payload", proposal_type="fact", source_anchors=[],
                expected_revision=1,
                request_id="proposal-request-first", mutation_id="proposal-mutation-other",
            )
        with self.assertRaises(ProfileEnablementIdempotencyConflict):
            self.decide_proposal(
                proposal_id=first["proposal_id"], action="reject", expected_revision=1,
                request_id="proposal-request-first", mutation_id="decision-cross-kind",
            )
        with sqlite3.connect(self.sidecar) as conn:
            for table in (
                "direct_profile_proposals", "direct_profile_proposal_receipts",
                "profile_enablement_decisions", "profile_enablement_receipts",
            ):
                with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(f"DELETE FROM {table}")

    def test_direct_decision_read_rebinds_to_immutable_proposal_snapshot(self):
        self.initialize()
        proposal = self.propose(source_anchors=[])["proposal"]
        self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="approve", expected_revision=0,
            request_id="binding-decision-request", mutation_id="binding-decision-mutation",
        )
        with sqlite3.connect(self.sidecar) as conn:
            conn.executescript("""
                DROP TRIGGER profile_enablement_decisions_no_update;
                DROP TRIGGER profile_enablement_decisions_no_delete;
                UPDATE profile_enablement_decisions
                SET subject_snapshot_json=json_set(subject_snapshot_json, '$.original_text', 'different')
                WHERE sequence=1;
                CREATE TRIGGER profile_enablement_decisions_no_update
                BEFORE UPDATE ON profile_enablement_decisions
                BEGIN SELECT RAISE(ABORT, 'profile_enablement_decisions is append-only'); END;
                CREATE TRIGGER profile_enablement_decisions_no_delete
                BEFORE DELETE ON profile_enablement_decisions
                BEGIN SELECT RAISE(ABORT, 'profile_enablement_decisions is append-only'); END;
            """)
            conn.commit()
        with self.assertRaises(ProfileEnablementError):
            active_profile_enablement_projection(self.sidecar)

    def test_invalid_anchor_privacy_type_and_non_boolean_initialization_fail_closed(self):
        self.initialize()
        with self.assertRaisesRegex(ProfileEnablementError, "controlled user message"):
            self.propose("anchor", source_anchors=[{"conversation_id": "c1", "message_id": "forged"}])
        with self.assertRaisesRegex(ProfileEnablementError, "proposal_type"):
            self.propose("type", proposal_type="open_question", source_anchors=[])
        with self.assertRaisesRegex(ProfileEnablementError, "privacy boundary"):
            self.propose("path", original_text=r"Use C:\\Users\\ExampleUser\\private.txt", source_anchors=[])
        target = self.root / "production-like.db"
        with patch("memory.profile_enablement.PRODUCTION_PROFILE_SIDECAR", target):
            result = initialize_profile_enablement_sidecar(
                target, execute=1, allow_production_profile_sidecar=True,
                confirm_production_profile_sidecar_init=True,
            )
        self.assertEqual("confirmation_type_invalid", result["reason_code"])
        self.assertFalse(target.exists())

    def test_new_schema_is_not_silently_accepted_by_sealed_v8a_and_no_injection_surface_exists(self):
        self.initialize()
        self.assertEqual("invalid", profile_sidecar_status(self.sidecar)["status"])
        self.assertNotIn("source_db", inspect.signature(ProfileEnablementService).parameters)
        self.assertNotIn("sidecar_path", inspect.signature(ProfileEnablementService).parameters)
        with patch("memory.profile_enablement.PRODUCTION_PROFILE_SIDECAR", self.sidecar):
            with self.assertRaisesRegex(ProfileEnablementError, "test profile core"):
                _ProfileEnablementCore(self.temporal.build, self.sidecar)

    def test_source_database_is_never_written(self):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.initialize()
        proposal = self.propose(source_anchors=[])["proposal"]
        self.decide_proposal(
            proposal_id=proposal["proposal_id"], action="approve", expected_revision=0,
            request_id="decision-request-source", mutation_id="decision-mutation-source",
        )
        self.assertEqual(source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_gate_b1_and_b2_are_distinct_one_shot_authorizations(self):
        self.initialize()
        b1_payload = {
            "original_text": "I prefer compact laptops.",
            "proposal_type": "preference",
            "source_anchors": [],
            "expected_revision": 0,
            "request_id": "gate-b1-request-one",
            "mutation_id": "gate-b1-mutation-one",
        }
        b1 = self.core.authorize_b1(**b1_payload)
        self.assertEqual("issued", profile_gate_authority_state(b1))
        with self.assertRaisesRegex(ProfileEnablementError, "Gate B2"):
            self.core.decide(
                authority=b1,
                subject_origin="direct_user_proposal", subject_id="dpp_" + "0" * 64,
                action="approve", expected_revision=0,
                request_id="gate-b2-request-wrong", mutation_id="gate-b2-mutation-wrong",
                query_text="", budget_profile="Medium", package_identity=None,
            )
        self.assertEqual("issued", profile_gate_authority_state(b1))

        receipt = self.core.propose(authority=b1, **b1_payload)
        proposal = receipt["proposal"]
        self.assertEqual(("B1", GATE_B1_AUTHORIZATION_VERSION), (
            receipt["gate"], proposal["authorization_version"],
        ))
        self.assertEqual({
            "original_text": b1_payload["original_text"],
            "proposal_type": b1_payload["proposal_type"],
            "source_anchors": b1_payload["source_anchors"],
            "proposed_by": "local_user",
            "expected_revision": b1_payload["expected_revision"],
            "request_id": b1_payload["request_id"],
            "mutation_id": b1_payload["mutation_id"],
        }, proposal["authorization"]["binding"])
        self.assertRegex(proposal["proposal_id"], r"dpp_[0-9a-f]{64}")
        self.assertEqual("consumed", profile_gate_authority_state(b1))
        self.assertEqual([], active_profile_enablement_projection(self.sidecar)["items"])
        with self.assertRaisesRegex(ProfileEnablementError, "consumed"):
            self.core.propose(authority=b1, **b1_payload)

        replay_authority = self.core.authorize_b1(**b1_payload)
        self.assertEqual(receipt, self.core.propose(authority=replay_authority, **b1_payload))

        b2_payload = {
            "subject_origin": "direct_user_proposal",
            "subject_id": proposal["proposal_id"],
            "action": "approve",
            "expected_revision": 0,
            "request_id": "gate-b2-request-one",
            "mutation_id": "gate-b2-mutation-one",
            "query_text": "", "budget_profile": "Medium", "package_identity": None,
        }
        b2 = self.core.authorize_b2(**b2_payload)
        with self.assertRaisesRegex(ProfileEnablementError, "Gate B1"):
            self.core.propose(authority=b2, **{
                **b1_payload,
                "request_id": "gate-b1-request-wrong",
                "mutation_id": "gate-b1-mutation-wrong",
            })
        self.assertEqual("issued", profile_gate_authority_state(b2))
        decision = self.core.decide(authority=b2, **b2_payload)
        self.assertEqual(("B2", GATE_B2_AUTHORIZATION_VERSION), (
            decision["gate"], decision["decision"]["authorization_version"],
        ))
        self.assertEqual(b2_payload["subject_id"], decision["decision"]["authorization"]["binding"]["subject_id"])
        self.assertEqual("consumed", profile_gate_authority_state(b2))

    def test_gate_b1_exact_binding_mismatch_expires_authority(self):
        self.initialize()
        base = {
            "original_text": "I prefer compact laptops.",
            "proposal_type": "preference",
            "source_anchors": [{"conversation_id": "c1", "message_id": "m1"}],
            "expected_revision": 0,
            "request_id": "binding-b1-request",
            "mutation_id": "binding-b1-mutation",
        }
        changes = (
            {"original_text": "I prefer large laptops."},
            {"proposal_type": "fact"},
            {"source_anchors": []},
            {"expected_revision": 1},
            {"request_id": "binding-b1-request-other"},
            {"mutation_id": "binding-b1-mutation-other"},
        )
        for index, change in enumerate(changes):
            authorized = {
                **base,
                "request_id": f"binding-b1-request-{index}",
                "mutation_id": f"binding-b1-mutation-{index}",
            }
            authority = self.core.authorize_b1(**authorized)
            with self.subTest(change=change), self.assertRaisesRegex(
                ProfileEnablementError, "binding mismatch",
            ):
                self.core.propose(authority=authority, **{**authorized, **change})
            self.assertEqual("expired", profile_gate_authority_state(authority))
        self.assertEqual([], active_profile_enablement_projection(self.sidecar)["items"])

    def test_gate_b2_binding_mismatch_expires_and_concurrent_consume_is_single_winner(self):
        self.initialize()
        proposal = self.propose(source_anchors=[])["proposal"]
        base = {
            "subject_origin": "direct_user_proposal", "subject_id": proposal["proposal_id"],
            "action": "edit_and_approve", "expected_revision": 0,
            "request_id": "binding-request-base", "mutation_id": "binding-mutation-base",
            "edited_statement": "I prefer small laptops.",
            "query_text": "", "budget_profile": "Medium", "package_identity": None,
        }
        changes = (
            {"subject_id": "dpp_" + "0" * 64},
            {"action": "reject", "edited_statement": ""},
            {"expected_revision": 1},
            {"edited_statement": "I prefer large laptops."},
        )
        for index, change in enumerate(changes):
            authorized = {**base, "request_id": f"binding-request-{index}",
                          "mutation_id": f"binding-mutation-{index}"}
            authority = self.core.authorize_b2(**authorized)
            asserted = {**authorized, **change}
            with self.subTest(change=change), self.assertRaisesRegex(
                ProfileEnablementError, "binding mismatch",
            ):
                self.core.decide(authority=authority, **asserted)
            self.assertEqual("expired", profile_gate_authority_state(authority))

        concurrent = {
            "subject_origin": "direct_user_proposal", "subject_id": proposal["proposal_id"],
            "action": "approve", "expected_revision": 0,
            "request_id": "concurrent-b2-request", "mutation_id": "concurrent-b2-mutation",
            "query_text": "", "budget_profile": "Medium", "package_identity": None,
        }
        authority = self.core.authorize_b2(**concurrent)
        gate = threading.Barrier(2)

        def execute():
            gate.wait()
            try:
                return self.core.decide(authority=authority, **concurrent)["status"]
            except ProfileEnablementError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: execute(), range(2)))
        self.assertEqual(1, results.count("persisted"))
        self.assertEqual(1, sum("consumed" in item for item in results))
        self.assertEqual("consumed", profile_gate_authority_state(authority))

    def test_profile_admin_api_exposes_d1_review_proposal_and_origin_specific_decision(self):
        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(
            readonly=True, profile_admin=False, profile_admin_session=None,
            db_mode="real", db_path=REAL_DB_PATH,
        )
        handler.send_json = Mock()
        handler.send_error_json = Mock()
        with patch.dict(AyunOSHandler.api_profile_enablement_control.__globals__, {
            "build_production_profile_enablement_review": Mock(return_value={"mutation_available": True}),
        }):
            handler.api_profile_enablement_control({"q": ["memory"], "budget": ["Medium"]})
        review = handler.send_json.call_args.args[0]
        self.assertFalse(review["mutation_available"])
        self.assertEqual("readonly_preview", review["write_mode"])

        proposal_payload = {
            "original_text": "I prefer compact laptops.",
            "proposal_type": "preference",
            "source_anchors": [],
            "expected_revision": 0,
            "request_id": "api-proposal-request",
            "mutation_id": "api-proposal-mutation",
        }
        handler.send_json.reset_mock()
        handler.api_profile_enablement_proposal(proposal_payload)
        denied, status = handler.send_json.call_args.args
        self.assertEqual(("profile_admin_required", HTTPStatus.FORBIDDEN), (
            denied["reason_code"], status,
        ))

        service = Mock()
        service.propose.return_value = {"status": "proposal_recorded"}
        service.decide.return_value = {"status": "persisted"}
        session = open_production_profile_admin()
        handler.app = Mock(
            readonly=True, profile_admin=True, profile_admin_session=session,
            db_mode="real", db_path=REAL_DB_PATH,
        )
        handler.send_json.reset_mock()
        with patch.dict(AyunOSHandler.api_profile_enablement_proposal.__globals__, {
            "ProfileEnablementService": Mock(return_value=service),
            "open_production_gate_b1": Mock(return_value=Mock(spec=GateB1ProposalCapability)),
        }):
            handler.api_profile_enablement_proposal(proposal_payload)
        handler.send_json.assert_called_once_with({"status": "proposal_recorded"})
        service.propose.assert_called_once()
        self.assertNotIn("proposed_by", proposal_payload)

        decision_payload = {
            "subject_origin": "direct_user_proposal",
            "subject_id": "dpp_" + "a" * 64,
            "action": "approve",
            "expected_revision": 0,
            "request_id": "api-decision-request",
            "mutation_id": "api-decision-mutation",
        }
        handler.send_json.reset_mock()
        with patch.dict(AyunOSHandler.api_profile_enablement_decision.__globals__, {
            "ProfileEnablementService": Mock(return_value=service),
            "open_production_gate_b2": Mock(return_value=Mock(spec=GateB2DecisionCapability)),
        }):
            handler.api_profile_enablement_decision(decision_payload)
        handler.send_json.assert_called_once_with({"status": "persisted"})
        service.decide.assert_called_once()

        handler.send_error_json.reset_mock()
        handler.api_profile_enablement_decision({**decision_payload, "sidecar_path": "x.db"})
        handler.send_error_json.assert_called_once_with(
            "Profile enablement decision fields mismatch", HTTPStatus.BAD_REQUEST,
        )
        do_post = inspect.getsource(AyunOSHandler.do_POST)
        self.assertIn('"/api/profile-enablement-proposal"', do_post)
        self.assertIn('"/api/profile-enablement-decision"', do_post)

    def test_web_profile_admin_contains_direct_proposal_and_d1_decision_controls(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        for marker in (
            't("profile.directProposal"',
            "v8.0-D1 直接用户提议",
            "/api/profile-enablement-control",
            "/api/profile-enablement-proposal",
            "/api/profile-enablement-decision",
            "function submitDirectProfileProposal",
            "function submitProfileEnablementDecision",
            "subject_origin",
            "expected_revision: review.control_revision",
        ):
            self.assertIn(marker, app)


if __name__ == "__main__":
    unittest.main()
