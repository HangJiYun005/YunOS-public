from __future__ import annotations

from copy import deepcopy
import inspect
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.approved_profile_control import (
    ApprovedProfileControlError,
    ApprovedProfileControlService,
    ApprovedProfileIdempotencyConflict,
    ApprovedProfileRevisionConflict,
    _ApprovedProfileControlCore,
    _record_test_approved_profile_decision,
    active_approved_profile_projection,
    build_approved_profile_review,
    initialize_approved_profile_sidecar,
    open_production_profile_admin,
    profile_sidecar_status,
)
from memory.evidence_selection import EvidenceSelectionService
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import MemoryPackageService
from memory.temporal_memory_candidates import _TemporalCandidateCore
from tools.init_approved_profile_sidecar import parse_args as parse_init_args
from tools.local_web import AyunOSHandler, REAL_DB_PATH


class ApprovedProfileControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.sidecar = self.root / "approved-profile.db"
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
        selection_service = EvidenceSelectionService(self.source, memory_package_service=memory_service)
        service = _TemporalCandidateCore(self.source, selection_service=selection_service)
        self.temporal_service = service
        self.package = service.build(
            RetrievalQuery("memory", hit_types=("keyword",), limit=20), budget_profile="Large"
        )
        self.control = _ApprovedProfileControlCore(service.build, self.sidecar)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(self, candidate_type: str) -> dict:
        return next(item for item in self.package["candidates"] if item["candidate_type"] == candidate_type)

    def initialize(self) -> None:
        result = initialize_approved_profile_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )
        self.assertEqual("initialized", result["status"])

    def decide(
        self, candidate: dict, action: str, revision: int, suffix: str,
        *, edited_statement: str = "", reason: str = "",
    ) -> dict:
        return _record_test_approved_profile_decision(
            self.package,
            sidecar_path=self.sidecar,
            package_identity=self.package["package_identity"],
            candidate_id=candidate["candidate_id"],
            action=action,
            expected_revision=revision,
            request_id=f"request-{suffix}",
            mutation_id=f"mutation-{suffix}",
            edited_statement=edited_statement,
            reason=reason,
        )

    def test_missing_sidecar_review_is_non_creating_and_still_exposes_source_evidence(self):
        self.assertFalse(self.sidecar.exists())
        review = build_approved_profile_review(self.package, sidecar_path=self.sidecar)
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(review["mutation_available"])
        self.assertEqual("sidecar_not_initialized", review["sidecar"]["reason_code"])
        self.assertEqual(0, review["control_revision"])
        preference = next(
            item for item in review["candidates"]
            if item["candidate"]["candidate_type"] == "preference"
        )
        evidence = preference["evidence"]
        self.assertEqual("user", evidence["source_anchor"]["role"])
        self.assertTrue(evidence["source_anchor"]["reference_uri"].startswith("ayunos://conversation/"))
        self.assertTrue(evidence["evidence_ids"])
        self.assertEqual(64, len(evidence["source_excerpt_sha256"]))
        self.assertTrue(review["execution"]["source_readonly"])
        self.assertFalse(review["execution"]["external_model_calls"])

    def test_explicit_initialization_is_dry_run_by_default_and_test_gated(self):
        dry_run = initialize_approved_profile_sidecar(self.sidecar, test_mode=True)
        self.assertEqual("would_initialize", dry_run["status"])
        self.assertFalse(self.sidecar.exists())
        refused = initialize_approved_profile_sidecar(self.sidecar, execute=True)
        self.assertEqual(("refused", "test_mode_required"), (refused["status"], refused["reason_code"]))
        self.assertFalse(self.sidecar.exists())
        self.initialize()
        self.assertEqual("ready", profile_sidecar_status(self.sidecar)["status"])
        self.assertEqual("already_initialized", initialize_approved_profile_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )["status"])
        unsafe = initialize_approved_profile_sidecar(
            Path(__file__).resolve().parents[1] / "unsafe-profile.db", test_mode=True,
        )
        self.assertEqual(("refused", "sidecar_path_invalid"), (unsafe["status"], unsafe["reason_code"]))

    def test_production_initialization_requires_three_strict_booleans(self):
        for index, values in enumerate((
            {"execute": 1, "allow_production_profile_sidecar": True, "confirm_production_profile_sidecar_init": True},
            {"execute": True, "allow_production_profile_sidecar": "yes", "confirm_production_profile_sidecar_init": True},
            {"execute": True, "allow_production_profile_sidecar": True, "confirm_production_profile_sidecar_init": 1},
        )):
            target = self.root / f"production-{index}.db"
            with patch("memory.approved_profile_control.PRODUCTION_PROFILE_SIDECAR", target):
                result = initialize_approved_profile_sidecar(target, **values)
            self.assertEqual(("refused", "confirmation_type_invalid"), (
                result["status"], result["reason_code"],
            ))
            self.assertFalse(target.exists())
        target = self.root / "production-partial.db"
        with patch("memory.approved_profile_control.PRODUCTION_PROFILE_SIDECAR", target):
            partial = initialize_approved_profile_sidecar(
                target, execute=True, allow_production_profile_sidecar=True,
                confirm_production_profile_sidecar_init=False,
            )
        self.assertEqual(("refused", "production_confirmation_required"), (
            partial["status"], partial["reason_code"],
        ))
        self.assertFalse(target.exists())

    def test_execute_rebuilds_source_backed_package_and_rejects_prepare_drift(self):
        self.initialize()
        query = RetrievalQuery("memory", hit_types=("keyword",), limit=20)
        prepared = self.control.review(query, budget_profile="Large")
        candidate = next(item for item in prepared["candidates"] if item["promoted_kind"] is not None)
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE messages SET text='source changed after prepare' WHERE message_id='m1'")
            conn.commit()
        with self.assertRaisesRegex(ApprovedProfileControlError, "package identity is stale"):
            self.control.decide(
                query, budget_profile="Large",
                package_identity=prepared["source"]["package_identity"],
                candidate_id=candidate["candidate"]["candidate_id"], action="approve",
                expected_revision=0, request_id="request-source-drift",
                mutation_id="mutation-source-drift",
            )
        self.assertEqual(0, active_approved_profile_projection(self.sidecar)["decision_count"])

    def test_forged_candidate_content_fails_standalone_before_test_insert(self):
        self.initialize()
        for index, mutate in enumerate((
            lambda package: package["candidates"][0].__setitem__("statement", "forged profile statement"),
            lambda package: package["events"][0]["source"]["message_anchor"].__setitem__("message_id", "forged-message"),
            lambda package: package.__setitem__("package_identity", "tmc_" + "0" * 64),
            lambda package: package["source_projection"].__setitem__("package_identity", "msp_" + "0" * 64),
        )):
            forged = deepcopy(self.package)
            mutate(forged)
            with self.subTest(index=index), self.assertRaises(ValueError):
                _record_test_approved_profile_decision(
                    forged, sidecar_path=self.sidecar,
                    package_identity=forged["package_identity"],
                    candidate_id=forged["candidates"][0]["candidate_id"], action="approve",
                    expected_revision=0, request_id=f"request-forged-{index}",
                    mutation_id=f"mutation-forged-{index}",
                )

    def test_concurrent_same_revision_allows_one_append_and_one_conflict(self):
        self.initialize()
        candidates = [self.candidate("preference"), self.candidate("fact")]
        gate = threading.Barrier(2)

        def decide(index: int) -> tuple[str, int]:
            gate.wait()
            try:
                result = _record_test_approved_profile_decision(
                    self.package, sidecar_path=self.sidecar,
                    package_identity=self.package["package_identity"],
                    candidate_id=candidates[index]["candidate_id"], action="approve",
                    expected_revision=0, request_id=f"request-tab-{index}",
                    mutation_id=f"mutation-tab-{index}",
                )
                return "persisted", result["revision"]
            except ApprovedProfileRevisionConflict as exc:
                return "conflict", exc.current_revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(decide, (0, 1)))
        self.assertEqual(["conflict", "persisted"], sorted(item[0] for item in outcomes))
        self.assertEqual([1, 1], sorted(item[1] for item in outcomes))
        with sqlite3.connect(self.sidecar) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM approved_profile_decisions").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM approved_profile_receipts").fetchone()[0])

    def test_complete_append_only_decision_loop_projection_revision_and_idempotency(self):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.initialize()
        preference = self.candidate("preference")
        fact = self.candidate("fact")

        approved = self.decide(preference, "approve", 0, "approve")
        self.assertEqual(1, approved["revision"])
        self.assertEqual(preference["statement"], approved["active_approved_profile"]["items"][0]["statement"])

        retry = self.decide(preference, "approve", 0, "approve")
        self.assertEqual(approved, retry)
        with self.assertRaises(ApprovedProfileRevisionConflict) as stale:
            self.decide(fact, "approve", 0, "stale")
        self.assertEqual(1, stale.exception.current_revision)

        edited = self.decide(
            preference, "edit_and_approve", 1, "edit",
            edited_statement="I prefer green tea.", reason="User corrected the approved wording.",
        )
        self.assertEqual(2, edited["revision"])
        self.assertEqual("I prefer green tea.", edited["active_approved_profile"]["items"][0]["statement"])

        rejected = self.decide(preference, "reject", 2, "reject", reason="No longer accurate.")
        self.assertEqual((3, []), (rejected["revision"], rejected["active_approved_profile"]["items"]))
        deferred = self.decide(fact, "defer", 3, "defer")
        self.assertEqual((4, []), (deferred["revision"], deferred["active_approved_profile"]["items"]))
        final = self.decide(fact, "approve", 4, "fact")
        self.assertEqual(5, final["revision"])
        self.assertEqual("fact", final["active_approved_profile"]["items"][0]["kind"])

        projection = active_approved_profile_projection(self.sidecar)
        self.assertEqual((5, 5, 1), (projection["revision"], projection["decision_count"], len(projection["items"])))
        self.assertEqual(fact["candidate_id"], projection["bindings"][0]["candidate_id"])
        self.assertTrue(projection["model_visible"])
        self.assertIs(projection["items"][0]["model_visible"], True)
        delayed_retry = self.decide(preference, "approve", 0, "approve")
        self.assertEqual(approved, delayed_retry)
        review = build_approved_profile_review(self.package, sidecar_path=self.sidecar)
        self.assertEqual(5, len(review["decision_history"]))
        self.assertEqual({"manual"}, {item["decision_source"] for item in review["decision_history"]})
        with sqlite3.connect(self.sidecar) as conn:
            self.assertEqual(5, conn.execute("SELECT COUNT(*) FROM approved_profile_decisions").fetchone()[0])
            self.assertEqual(5, conn.execute("SELECT COUNT(*) FROM approved_profile_receipts").fetchone()[0])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("UPDATE approved_profile_decisions SET reason='changed' WHERE sequence=1")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("DELETE FROM approved_profile_decisions WHERE sequence=1")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("UPDATE approved_profile_receipts SET receipt_json='{}'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("DELETE FROM approved_profile_receipts")
        self.assertEqual(source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_request_and_mutation_ids_fail_closed_on_reuse(self):
        self.initialize()
        preference = self.candidate("preference")
        self.decide(preference, "approve", 0, "same")
        with self.assertRaises(ApprovedProfileIdempotencyConflict):
            _record_test_approved_profile_decision(
                self.package,
                sidecar_path=self.sidecar,
                package_identity=self.package["package_identity"],
                candidate_id=preference["candidate_id"],
                action="reject",
                expected_revision=1,
                request_id="request-same",
                mutation_id="mutation-other",
            )
        with self.assertRaises(ApprovedProfileIdempotencyConflict):
            _record_test_approved_profile_decision(
                self.package,
                sidecar_path=self.sidecar,
                package_identity=self.package["package_identity"],
                candidate_id=preference["candidate_id"],
                action="reject",
                expected_revision=1,
                request_id="request-other",
                mutation_id="mutation-same",
            )
        with sqlite3.connect(self.sidecar) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM approved_profile_decisions").fetchone()[0])

    def test_coordinated_offline_row_tamper_fails_record_identity_validation(self):
        self.initialize()
        self.decide(self.candidate("fact"), "approve", 0, "tamper")
        with sqlite3.connect(self.sidecar) as conn:
            conn.executescript("""
                DROP TRIGGER approved_profile_decisions_no_update;
                DROP TRIGGER approved_profile_decisions_no_delete;
                UPDATE approved_profile_decisions SET reason='offline tamper' WHERE sequence=1;
                CREATE TRIGGER approved_profile_decisions_no_update
                BEFORE UPDATE ON approved_profile_decisions
                BEGIN SELECT RAISE(ABORT, 'approved profile decisions are append-only'); END;
                CREATE TRIGGER approved_profile_decisions_no_delete
                BEFORE DELETE ON approved_profile_decisions
                BEGIN SELECT RAISE(ABORT, 'approved profile decisions are append-only'); END;
            """)
            conn.commit()
        with self.assertRaisesRegex(ApprovedProfileControlError, "identity mismatch"):
            active_approved_profile_projection(self.sidecar)

    def test_open_question_and_absolute_path_edits_cannot_be_approved(self):
        self.initialize()
        open_question = self.candidate("open_question")
        preference = self.candidate("preference")
        review = build_approved_profile_review(self.package, sidecar_path=self.sidecar)
        item = next(entry for entry in review["candidates"] if entry["candidate"]["candidate_id"] == open_question["candidate_id"])
        self.assertEqual(["reject", "defer"], item["allowed_actions"])
        with self.assertRaisesRegex(ApprovedProfileControlError, "open_question"):
            self.decide(open_question, "approve", 0, "question")
        with self.assertRaisesRegex(ApprovedProfileControlError, "privacy boundary"):
            self.decide(
                preference, "edit_and_approve", 0, "path",
                edited_statement=r"Use C:\\Users\\ExampleUser\\secret.txt",
            )
        self.assertEqual(0, active_approved_profile_projection(self.sidecar)["revision"])

    def test_production_service_and_initializer_expose_no_source_or_sidecar_injection(self):
        self.assertNotIn("source_db", inspect.signature(ApprovedProfileControlService).parameters)
        self.assertNotIn("sidecar_path", inspect.signature(ApprovedProfileControlService).parameters)
        with self.assertRaisesRegex(ApprovedProfileControlError, "profile-admin session"):
            ApprovedProfileControlService().decide(
                RetrievalQuery("memory"), profile_admin=None, budget_profile="Small",
                package_identity=self.package["package_identity"],
                candidate_id=self.candidate("fact")["candidate_id"], action="approve",
                expected_revision=0, request_id="request-no-admin",
                mutation_id="mutation-no-admin",
            )
        args = parse_init_args([])
        self.assertFalse(args.execute)
        self.assertIsNone(args.test_sidecar)

    def test_readonly_preview_rejects_mutation_and_profile_admin_allows_it(self):
        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(
            readonly=True, profile_admin=False, profile_admin_session=None,
            db_mode="real", db_path=REAL_DB_PATH,
        )
        handler.send_json = Mock()
        handler.send_error_json = Mock()
        globals_ = AyunOSHandler.api_approved_profile_control.__globals__
        with patch.dict(globals_, {
            "build_production_approved_profile_review": Mock(return_value={"mutation_available": True}),
        }):
            handler.api_approved_profile_control({"q": ["memory"], "budget": ["Medium"]})
        preview = handler.send_json.call_args.args[0]
        self.assertFalse(preview["mutation_available"])
        self.assertEqual("readonly_preview", preview["write_mode"])
        handler.send_json.reset_mock()
        handler.api_approved_profile_control({"q": ["memory"], "db": ["other.db"]})
        handler.send_error_json.assert_called_once_with(
            "Unsupported approved profile control parameter", HTTPStatus.BAD_REQUEST,
        )

        handler.send_error_json.reset_mock()
        payload = {
            "q": "memory",
            "budget": "Medium",
            "package_identity": self.package["package_identity"],
            "candidate_id": self.candidate("fact")["candidate_id"],
            "action": "defer",
            "expected_revision": 0,
            "request_id": "request-api-1",
            "mutation_id": "mutation-api-1",
        }
        record = Mock(return_value={"status": "persisted"})
        with patch.dict(AyunOSHandler.api_approved_profile_decision.__globals__, {
            "record_production_approved_profile_decision": record,
        }):
            handler.api_approved_profile_decision(payload)
        denied, status = handler.send_json.call_args.args
        self.assertEqual(("profile_admin_required", HTTPStatus.FORBIDDEN), (
            denied["reason_code"], status,
        ))
        record.assert_not_called()

        handler.send_json.reset_mock()
        admin_session = open_production_profile_admin()
        handler.app = Mock(
            readonly=True, profile_admin=True, profile_admin_session=admin_session,
            db_mode="real", db_path=REAL_DB_PATH,
        )
        with patch.dict(AyunOSHandler.api_approved_profile_decision.__globals__, {
            "record_production_approved_profile_decision": record,
        }):
            handler.api_approved_profile_decision(payload)
        handler.send_json.assert_called_once_with({"status": "persisted"})
        record.assert_called_once()

        handler.send_json.reset_mock()
        handler.app = Mock(readonly=False, profile_admin=False, db_mode="real", db_path=REAL_DB_PATH)
        handler.api_approved_profile_control({"q": ["memory"]})
        unavailable, status = handler.send_json.call_args.args
        self.assertEqual(("production_readonly_required", HTTPStatus.SERVICE_UNAVAILABLE), (
            unavailable["reason_code"], status,
        ))
        do_post_source = inspect.getsource(AyunOSHandler.do_POST)
        self.assertNotIn('parsed.path != "/api/approved-profile-decision"', do_post_source)
        self.assertIn("self.app.profile_admin", do_post_source)
        self.assertIn("profile_admin_mutation", do_post_source)

    def test_web_control_page_contains_minimal_decision_and_revision_controls(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        app = (root / "web" / "app.js").read_text(encoding="utf-8")
        style = (root / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn('view: "approvedProfiles"', app)
        for marker in (
            "function loadApprovedProfiles",
            "function runApprovedProfileReview",
            "function submitApprovedProfileDecision",
            "/api/approved-profile-control",
            "/api/approved-profile-decision",
            "expected_revision: review.control_revision",
            't("profile.viewSourceEvidence"',
            "edit_and_approve",
            't("profile.decisionHistory"',
            "legacy.tools.profile.decisionSubjectMeta",
            "legacy.tools.profile.approvalMeta",
        ):
            self.assertIn(marker, app)
        self.assertIn(".profile-control-panel", style)


if __name__ == "__main__":
    unittest.main()
