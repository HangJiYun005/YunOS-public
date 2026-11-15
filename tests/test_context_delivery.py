from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
from http import HTTPStatus
import inspect
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory import context_assembly as context_assembly_module
from memory.context_assembly import ContextAssemblyValidationError
from memory.context_delivery import (
    DELIVERY_IDENTITY_VERSION,
    PROTOCOL_VERSION,
    ContextDeliveryService,
    ContextDeliveryValidationError,
    _ContextDeliveryCore,
    build_context_delivery_from_assembly,
    canonical_context_delivery_json,
    render_context_delivery,
    validate_context_delivery_against_assembly,
    validate_context_delivery_standalone,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from tests import test_context_assembly as v73_tests
from tools.context_delivery_preview import main as preview_main, parse_args
from tools.local_web import App, AyunOSHandler, REAL_DB_PATH


class ContextDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = v73_tests.ContextAssemblyTest("test_json_text_consistency_and_stable_identities")
        self.fixture.setUp()
        self.core = _ContextDeliveryCore(context_assembly_core=self.fixture.core)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def build(self, *, excluded_item_ids=(), approved_profile=None, query=None, **kwargs):
        return self.core.build(
            query or self.fixture.query,
            budget_profile=kwargs.pop("budget_profile", "Large"),
            excluded_item_ids=excluded_item_ids,
            approved_profile=(
                [self.fixture.approved, self.fixture.unrelated_approved]
                if approved_profile is None else approved_profile
            ),
            **kwargs,
        )

    def test_four_review_layers_and_trust_boundaries_are_explicit(self):
        delivery = self.build()
        self.assertEqual(PROTOCOL_VERSION, delivery["protocol_version"])
        self.assertEqual(DELIVERY_IDENTITY_VERSION, delivery["delivery_identity_version"])
        self.assertEqual(
            {"current_task", "approved_profile", "candidate_memory", "historical_evidence"},
            set(delivery["layers"]),
        )
        self.assertFalse(delivery["layers"]["approved_profile"]["candidate_promotion_allowed"])
        for item in delivery["layers"]["approved_profile"]["items"]:
            self.assertEqual("approved", item["approval"]["status"])
        for item in delivery["layers"]["candidate_memory"]["items"]:
            self.assertEqual(("candidate", "not_approved"), (item["state"], item["approval_status"]))
            self.assertEqual({"source_ids", "evidence_ids", "event_ids"}, set(item["provenance"]))
        for item in delivery["layers"]["candidate_memory"]["relations"]:
            self.assertEqual({"source_ids", "evidence_ids", "event_ids"}, set(item["provenance"]))
        for item in delivery["layers"]["historical_evidence"]["items"]:
            self.assertEqual({"source_ids", "evidence_ids", "event_ids"}, set(item["provenance"]))
            if item["expanded_excerpt"] is not None:
                self.assertEqual(item["provenance"], item["expanded_excerpt_provenance"])
        statuses = {item["trust_status"] for item in delivery["review_items"]}
        self.assertIn("approved", statuses)
        self.assertIn("candidate/not_approved", statuses)
        self.assertIn("historical_evidence", statuses)

    def test_empty_approved_profile_is_visible_and_candidates_are_not_promoted(self):
        delivery = self.build(approved_profile=[])
        layer = delivery["layers"]["approved_profile"]
        self.assertEqual("empty", layer["status"])
        self.assertEqual([], layer["items"])
        self.assertIn("No approved profile items", layer["empty_message"])
        self.assertTrue(delivery["layers"]["candidate_memory"]["items"])
        self.assertNotIn("[approved/", delivery["model_context"])

    def test_temporary_exclusion_rebuilds_only_delivery_and_never_persists(self):
        before_hash = hashlib.sha256(self.fixture.source.read_bytes()).hexdigest()
        base = self.build()
        candidate_id = base["layers"]["candidate_memory"]["items"][0]["candidate_id"]
        adjusted = self.build(excluded_item_ids=[candidate_id])
        self.assertEqual(
            base["assembly_binding"]["assembly_identity"],
            adjusted["assembly_binding"]["assembly_identity"],
        )
        self.assertNotEqual(base["delivery_identity"], adjusted["delivery_identity"])
        self.assertNotIn(f"candidate/not_approved/preference/{candidate_id}", adjusted["model_context"])
        self.assertIn(f"candidate/not_approved/preference/{candidate_id}", adjusted["source_assembly"]["model_context"])
        self.assertEqual([candidate_id], adjusted["controls"]["excluded_item_ids"])
        self.assertEqual("customized", adjusted["review_state"])
        self.assertIn("user_excluded_current_delivery", {
            item["reason"] for item in adjusted["omitted"] if item["item_id"] == candidate_id
        })
        self.assertEqual({
            "readonly": True,
            "persisted": False,
            "sent_to_model": False,
            "external_model_calls": False,
            "temporary_adjustments_only": True,
        }, adjusted["execution"])
        self.assertEqual(before_hash, hashlib.sha256(self.fixture.source.read_bytes()).hexdigest())

    def test_candidate_exclusion_closes_same_source_evidence_expansion_and_relations(self):
        base = self.build()
        candidate = base["layers"]["candidate_memory"]["items"][0]
        source_id = candidate["provenance"]["source_ids"][0]
        statement = candidate["statement"]
        adjusted = self.build(excluded_item_ids=[candidate["candidate_id"]])
        closure = adjusted["controls"]["exclusion_closure"]

        self.assertIn(candidate["candidate_id"], closure["excluded_candidate_ids"])
        self.assertIn(source_id, closure["excluded_evidence_reference_ids"])
        self.assertNotIn(statement, adjusted["model_context"])
        self.assertNotIn(source_id, {
            item["evidence_reference_id"]
            for item in adjusted["layers"]["historical_evidence"]["items"]
        })
        self.assertFalse(any(
            candidate["candidate_id"] in {item["newer_candidate_id"], item["older_candidate_id"]}
            for item in adjusted["layers"]["candidate_memory"]["relations"]
        ))
        evidence_review = next(
            item for item in adjusted["review_items"] if item["item_id"] == source_id
        )
        self.assertEqual("provenance_cascade", evidence_review["exclusion"]["state"])
        self.assertEqual([candidate["candidate_id"]], evidence_review["exclusion"]["caused_by_item_ids"])

    def test_evidence_exclusion_closes_dependent_candidate_and_relation(self):
        base = self.build()
        evidence = base["layers"]["historical_evidence"]["items"][0]
        evidence_id = evidence["evidence_reference_id"]
        candidate = next(
            item for item in base["layers"]["candidate_memory"]["items"]
            if evidence_id in item["provenance"]["source_ids"]
        )
        adjusted = self.build(excluded_item_ids=[evidence_id])
        closure = adjusted["controls"]["exclusion_closure"]

        self.assertIn(candidate["candidate_id"], closure["excluded_candidate_ids"])
        self.assertNotIn(candidate["candidate_id"], {
            item["candidate_id"] for item in adjusted["layers"]["candidate_memory"]["items"]
        })
        self.assertFalse(any(
            candidate["candidate_id"] in {item["newer_candidate_id"], item["older_candidate_id"]}
            for item in adjusted["layers"]["candidate_memory"]["relations"]
        ))
        candidate_review = next(
            item for item in adjusted["review_items"] if item["item_id"] == candidate["candidate_id"]
        )
        self.assertEqual("provenance_cascade", candidate_review["exclusion"]["state"])

    def test_equal_text_from_independent_sources_is_not_globally_removed(self):
        shared_text = "Preference: For MacBook I prefer Pro."
        assembly = self.fixture.build()
        candidates = assembly["candidate_memory"]["items"]
        c1 = next(item for item in candidates if "/c1?" in item["reference_uri"])
        c2 = next(item for item in candidates if "/c2?" in item["reference_uri"])
        c2["statement"] = shared_text
        c2_evidence = next(
            item for item in assembly["historical_evidence"]["items"]
            if item["evidence_reference_id"] == c2["source_candidate_id"]
        )
        c2_evidence["expanded_excerpt"] = shared_text
        assembly["model_context"] = context_assembly_module._render_model_context(
            assembly["task_context"],
            assembly["confirmed_user_profile"],
            assembly["candidate_memory"],
            assembly["historical_evidence"],
        )
        assembly["budget"]["used_characters"] = len(assembly["model_context"])
        assembly["budget"]["remaining_characters"] = (
            assembly["budget"]["total_characters_limit"] - len(assembly["model_context"])
        )
        assembly["budget"]["estimated_tokens_used"] = context_assembly_module.estimate_tokens(
            assembly["model_context"]
        )
        assembly["budget"]["estimated_tokens_remaining"] = (
            assembly["budget"]["estimated_token_limit"]
            - assembly["budget"]["estimated_tokens_used"]
        )
        assembly["transparency"]["usage"] = deepcopy(assembly["budget"])
        assembly["assembly_identity"] = "ctx_" + context_assembly_module._digest(
            context_assembly_module._identity_payload(assembly)
        )
        self.assertEqual(c1["statement"], c2["statement"])

        adjusted = build_context_delivery_from_assembly(
            assembly, excluded_item_ids=[c1["candidate_id"]],
        )
        delivered_c2 = next(
            item for item in adjusted["layers"]["candidate_memory"]["items"]
            if item["candidate_id"] == c2["candidate_id"]
        )
        delivered_c1_review = next(
            item for item in adjusted["review_items"] if item["item_id"] == c1["candidate_id"]
        )
        self.assertFalse(
            set(delivered_c1_review["provenance"]["source_ids"])
            & set(delivered_c2["provenance"]["source_ids"])
        )
        remaining_ids = {
            item["candidate_id"] for item in adjusted["layers"]["candidate_memory"]["items"]
        }
        self.assertNotIn(c1["candidate_id"], remaining_ids)
        self.assertIn(c2["candidate_id"], remaining_ids)
        self.assertIn(shared_text, adjusted["model_context"])

    def test_validator_recomputes_closure_and_proves_excluded_provenance_absent(self):
        base = self.build()
        candidate_id = base["layers"]["candidate_memory"]["items"][0]["candidate_id"]
        adjusted = self.build(excluded_item_ids=[candidate_id])
        closure = adjusted["controls"]["exclusion_closure"]
        payload = adjusted["model_payload_provenance"]
        for kind in ("source_ids", "evidence_ids", "event_ids"):
            self.assertFalse(set(closure["excluded_provenance"][kind]) & set(payload[kind]))

        tampered = deepcopy(adjusted)
        tampered["controls"]["exclusion_closure"]["excluded_candidate_ids"] = []
        with self.assertRaisesRegex(ContextDeliveryValidationError, "closure mismatch"):
            validate_context_delivery_standalone(tampered)

        tampered = deepcopy(adjusted)
        excluded_source = closure["excluded_provenance"]["source_ids"][0]
        tampered["model_payload_provenance"]["source_ids"].append(excluded_source)
        tampered["model_payload_provenance"]["source_ids"].sort()
        with self.assertRaisesRegex(ContextDeliveryValidationError, "model payload provenance mismatch"):
            validate_context_delivery_standalone(tampered)

    def test_budget_and_time_controls_reassemble_through_v73(self):
        large = self.build()
        small = self.build(budget_profile="Small")
        ranged = self.build(query=RetrievalQuery(
            "MacBook", start_time="2025-02-01", end_time="2025-02-28",
            hit_types=("keyword", "semantic"), limit=20,
        ))
        self.assertEqual("Large", large["controls"]["budget_profile"])
        self.assertEqual("Small", small["controls"]["budget_profile"])
        self.assertNotEqual(large["assembly_binding"]["assembly_identity"], small["assembly_binding"]["assembly_identity"])
        self.assertEqual("2025-02-01T00:00:00Z", ranged["controls"]["time_range"]["start_time"])
        self.assertEqual("2025-02-28T23:59:59.999999Z", ranged["controls"]["time_range"]["end_time"])
        self.assertNotEqual(large["assembly_binding"]["assembly_identity"], ranged["assembly_binding"]["assembly_identity"])
        self.assertLessEqual(small["budget"]["used_characters"], small["budget"]["total_characters_limit"])
        self.assertLessEqual(small["budget"]["estimated_tokens_used"], small["budget"]["estimated_token_limit"])

    def test_source_anchors_omissions_and_capability_degradation_are_reviewable(self):
        delivery = self.build()
        anchored = [item for item in delivery["review_items"] if item["source_anchor"]]
        self.assertTrue(anchored)
        self.assertTrue(all(item["source_anchor"]["reference_uri"].startswith("ayunos://") for item in anchored))
        self.assertTrue(any(item["omitted_reasons"] for item in delivery["review_items"]))
        self.assertIn("capabilities", delivery["capability_state"])
        self.assertIn("degradation", delivery["capability_state"])
        self.assertIn("missing_capabilities", delivery["capability_state"])

    def test_identity_json_text_and_tamper_validation(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first["delivery_identity"], second["delivery_identity"])
        self.assertEqual(first, json.loads(canonical_context_delivery_json(first)))
        preview = render_context_delivery(first)
        self.assertIn(first["delivery_identity"], preview)
        self.assertIn(first["assembly_binding"]["assembly_identity"], preview)
        self.assertIn(first["model_context"], preview)
        self.assertIn("persisted: false", preview)
        self.assertIn("sent_to_model: false", preview)
        validate_context_delivery_against_assembly(first, trusted_assembly=first["source_assembly"])
        for mutate in (
            lambda value: value["execution"].update({"sent_to_model": True}),
            lambda value: value["layers"]["candidate_memory"]["items"][0].update({"approval_status": "approved"}),
            lambda value: value.update({"model_context": value["model_context"] + "x"}),
        ):
            tampered = deepcopy(first)
            mutate(tampered)
            with self.assertRaises((ContextDeliveryValidationError, ContextAssemblyValidationError)):
                validate_context_delivery_standalone(tampered)

    def test_unknown_or_noncanonical_exclusions_fail_closed(self):
        with self.assertRaises(ContextDeliveryValidationError):
            self.build(excluded_item_ids=["mc_" + "0" * 64])
        with self.assertRaises(ContextDeliveryValidationError):
            self.build(excluded_item_ids=["current_task"])

    def test_historical_evidence_can_be_temporarily_excluded_by_anchor_identity(self):
        base = self.build()
        evidence = base["layers"]["historical_evidence"]["items"][0]
        item_id = evidence["evidence_reference_id"]
        adjusted = self.build(excluded_item_ids=[item_id])
        self.assertNotIn(item_id, adjusted["layers"]["current_task"]["content"]["current_retrieval_results"]["assembled_reference_ids"])
        self.assertFalse(next(item for item in adjusted["review_items"] if item["item_id"] == item_id)["included"])
        self.assertIn("user_excluded_current_delivery", next(
            item["reason"] for item in adjusted["omitted"] if item["item_id"] == item_id
        ))

    def test_cli_and_get_are_fixed_real_readonly_surfaces(self):
        args = parse_args(["MacBook", "--budget", "large", "--json"])
        self.assertFalse(hasattr(args, "db"))
        self.assertNotIn("source_db", inspect.signature(ContextDeliveryService).parameters)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["MacBook", "--db", "other.db"])
        with patch("tools.context_delivery_preview.build_production_context_delivery", return_value={"x": 1}), patch(
            "tools.context_delivery_preview.canonical_context_delivery_json", return_value="{}",
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, preview_main(["MacBook", "--json"]))
            self.assertEqual("{}", output.getvalue().strip())

        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH)
        handler.send_json = Mock()
        handler.send_text = Mock()
        handler.send_error_json = Mock()
        method_globals = AyunOSHandler.api_context_delivery_preview.__globals__
        with patch.dict(method_globals, {
            "build_production_context_delivery": Mock(return_value={"delivery": 1}),
        }):
            handler.api_context_delivery_preview({"q": ["MacBook"], "exclude": []})
        handler.send_json.assert_called_once_with({"delivery": 1})
        handler.send_error_json.reset_mock()
        handler.api_context_delivery_preview({"q": ["MacBook"], "db": ["x.db"]})
        handler.send_error_json.assert_called_once_with(
            "Unsupported context delivery parameter", HTTPStatus.BAD_REQUEST,
        )
        handler.send_json.reset_mock()
        handler.app = Mock(readonly=False, db_mode="real", db_path=REAL_DB_PATH)
        handler.api_context_delivery_preview({"q": ["MacBook"]})
        payload, status = handler.send_json.call_args.args
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("production_readonly_required", payload["reason_code"])
        self.assertFalse(payload["execution"]["persisted"])
        self.assertFalse(payload["execution"]["sent_to_model"])

    def test_message_context_uses_composite_anchor_and_fails_closed(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "anchors.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE messages(
                        message_id TEXT, conversation_id TEXT, conversation_title TEXT,
                        ordinal INTEGER, role TEXT, create_time REAL,
                        create_time_iso TEXT, text TEXT, source_platform TEXT
                    );
                """)
                conn.executemany(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        ("shared", "c1", "One", 1, "user", 1.0, "2025-01-01T00:00:00Z", "one", "chatgpt"),
                        ("shared", "c2", "Two", 1, "user", 2.0, "2025-01-02T00:00:00Z", "two", "chatgpt"),
                    ],
                )
                conn.commit()
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            handler = object.__new__(AyunOSHandler)
            handler.app = App(db_path.resolve(), real=False, readonly=True)
            handler.send_json = Mock()
            handler.send_error_json = Mock()

            handler.api_message_context({
                "conversation_id": ["c2"], "message_id": ["shared"], "window": ["0"],
            })
            payload = handler.send_json.call_args.args[0]
            self.assertEqual("c2", payload["conversation_id"])
            self.assertEqual("two", payload["messages"][0]["text"])
            self.assertEqual(before_hash, hashlib.sha256(db_path.read_bytes()).hexdigest())

            handler.send_error_json.reset_mock()
            handler.api_message_context({"message_id": ["shared"]})
            handler.send_error_json.assert_called_once_with(
                "Missing conversation id", HTTPStatus.BAD_REQUEST,
            )
            handler.send_error_json.reset_mock()
            handler.api_message_context({"conversation_id": ["c2"]})
            handler.send_error_json.assert_called_once_with(
                "Missing message id", HTTPStatus.BAD_REQUEST,
            )
            handler.send_error_json.reset_mock()
            handler.api_message_context({
                "conversation_id": ["forged"], "message_id": ["shared"],
            })
            handler.send_error_json.assert_called_once_with(
                "Message anchor not found", HTTPStatus.NOT_FOUND,
            )

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    ("shared", "c2", "Two duplicate", 2, "user", 3.0, "2025-01-03T00:00:00Z", "duplicate", "chatgpt"),
                )
                conn.commit()
            after_duplicate_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            handler.send_error_json.reset_mock()
            handler.api_message_context({
                "conversation_id": ["c2"], "message_id": ["shared"],
            })
            handler.send_error_json.assert_called_once_with(
                "Ambiguous message anchor", HTTPStatus.CONFLICT,
            )
            self.assertEqual(after_duplicate_hash, hashlib.sha256(db_path.read_bytes()).hexdigest())

            handler.send_json.reset_mock()
            handler.api_message_context({
                "conversation_id": ["c1"], "message_id": ["shared"], "window": ["0"],
            })
            self.assertEqual("c1", handler.send_json.call_args.args[0]["conversation_id"])
            self.assertEqual(after_duplicate_hash, hashlib.sha256(db_path.read_bytes()).hexdigest())

    def test_web_review_entry_and_controls_are_present(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        app = (root / "web" / "app.js").read_text(encoding="utf-8")
        style = (root / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn('view: "contextReview"', app)
        for marker in (
            "function loadContextReview",
            "function runContextReview",
            "function renderContextReviewResults",
            "function contextReviewItemCard",
            "/api/context-delivery-preview",
            "context-review-start",
            "context-review-end",
            "Small", "Medium", "Large", "Custom",
            "conversation_id: item.conversation_id",
            "provenance_cascade",
            't("context.openSourceMessage"',
            't("context.finalModelContext"',
        ):
            self.assertIn(marker, app)
        self.assertIn(".context-layer", style)
        self.assertIn(".model-context-preview", style)


if __name__ == "__main__":
    unittest.main()
