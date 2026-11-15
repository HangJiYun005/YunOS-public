from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from memory.context_assembly import make_approved_profile_item
from memory.context_delivery import PROTOCOL_VERSION as SEALED_V74_PROTOCOL_VERSION
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.production_delivery import (
    ASSEMBLY_PROTOCOL_VERSION,
    PROFILE_POLICY_VERSION,
    PROTOCOL_VERSION,
    ProductionDeliveryService,
    ProductionDeliveryValidationError,
    _ProductionDeliveryCore,
    _digest,
    production_profile_binding,
    validate_production_delivery_standalone,
)
from memory.runtime_enablement import (
    build_runtime_enablement_artifact,
    validate_runtime_enablement_artifact,
)
from memory.runtime_protocol_contracts import profile_projection_identity
from memory.runtime_thin_slice import (
    build_runtime_request_artifact,
    validate_runtime_request_artifact,
)
from tests import test_context_delivery as v74_tests


QUESTION = (
    "Based on my prior YunOS history about choosing a MacBook, what preferences "
    "or exclusions are actually supported, and which points remain uncertain? "
    "Cite the relevant conversation and message anchors."
)


class ProductionDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.source_db = Path(self.temp.name) / "source.db"
        with sqlite3.connect(self.source_db) as conn:
            conn.executescript("""
                CREATE TABLE messages(
                    message_id TEXT, conversation_id TEXT, conversation_title TEXT,
                    ordinal INTEGER, role TEXT, create_time REAL,
                    create_time_iso TEXT, text TEXT, memory_space TEXT DEFAULT 'general'
                );
                CREATE VIRTUAL TABLE messages_fts USING fts5(
                    conversation_title, role, text,
                    content='messages', content_rowid='rowid'
                );
            """)
            rows = [
                ("m1", "c1", "MacBook plan", 1, "user", 1.0, "2026-01-01T00:00:00+00:00", "A MacBook would be a long-term, always-carried computer with battery life for travel and libraries.", "general"),
                ("m2", "c1", "MacBook plan", 2, "user", 2.0, "2026-02-01T00:00:00+00:00", "I require 120Hz; 60Hz is unacceptable unless I try it first.", "general"),
                ("m3", "c1", "MacBook plan", 3, "user", 3.0, "2026-03-01T00:00:00+00:00", "Experience and comfort matter more than chasing the lowest price.", "general"),
                ("m4", "c1", "MacBook plan", 4, "user", 4.0, "2026-04-01T00:00:00+00:00", "Target configuration: MacBook Pro M5, 24G memory, 1TB SSD, new.", "general"),
                ("m5", "c1", "MacBook plan", 5, "user", 5.0, "2026-05-01T00:00:00+00:00", "After September, buy only if the final price drops below 15,000 RMB.", "general"),
                ("m6", "c1", "MacBook plan", 6, "user", 6.0, "2026-06-01T00:00:00+00:00", "After September, maybe buy Air first; later I may switch to Pro, so the purchase plan changed.", "general"),
                ("m7", "c1", "MacBook plan", 7, "assistant", 7.0, "2026-06-01T00:01:00+00:00", "A current price claim that must not outrank direct user evidence.", "general"),
            ]
            conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

        self.v74 = v74_tests.ContextDeliveryTest(
            "test_four_review_layers_and_trust_boundaries_are_explicit"
        )
        self.v74.setUp()
        self.sealed = self.v74.build(
            approved_profile=[],
            query=RetrievalQuery("MacBook", hit_types=("keyword", "semantic", "graph"), limit=20),
        )
        self.items = []
        for index in range(4):
            item = make_approved_profile_item(
                f"Approved profile item {index}; intentionally unrelated to the MacBook query.",
                kind=("preference", "goal", "decision", "constraint")[index],
                topics=[f"unrelated-{index}"],
                approval_id=f"approval-{index}",
                approved_at=f"2026-01-0{index + 1}T00:00:00+00:00",
            )
            item["model_visible"] = True
            self.items.append(item)
        rejected = deepcopy(self.items[0])
        rejected["profile_item_id"] = "upi_" + "e" * 64
        rejected["approval"]["status"] = "rejected"
        pending = deepcopy(self.items[1])
        pending["profile_item_id"] = "upi_" + "d" * 64
        pending["approval"]["status"] = "pending"
        malformed = deepcopy(self.items[2])
        malformed["profile_item_id"] = "upi_" + "c" * 64
        malformed["statement"] = ""
        self.projection = {
            "protocol_version": "v8.0-d1-active-approved-profile-1",
            "projection_identity": "app_" + "a" * 64,
            "status": "ok",
            "reason_code": "active_approved_profile_available",
            "revision": 8,
            "decision_count": 8,
            "items": [*self.items, rejected, pending, malformed],
            "bindings": [],
            "model_visible": True,
            "readonly": True,
        }
        self.projection["projection_identity"] = profile_projection_identity(self.projection)
        self.core = _ProductionDeliveryCore(
            source_db=self.source_db,
            projection_loader=lambda: deepcopy(self.projection),
            sealed_builder=lambda *_args, **_kwargs: deepcopy(self.sealed),
        )

    def tearDown(self) -> None:
        self.v74.tearDown()
        self.temp.cleanup()

    def build(self):
        return self.core.build(RetrievalQuery(QUESTION), budget_profile="Large")

    def test_all_active_model_visible_items_are_included_without_relevance_pruning(self):
        delivery = self.build()
        self.assertEqual(PROTOCOL_VERSION, delivery["protocol_version"])
        self.assertEqual(ASSEMBLY_PROTOCOL_VERSION, delivery["source_assembly"]["protocol_version"])
        self.assertEqual(PROFILE_POLICY_VERSION, delivery["controls"]["profile_inclusion_policy"])
        self.assertEqual(4, delivery["counts"]["approved_profile_included"])
        self.assertEqual(7, delivery["counts"]["approved_profile_available"])
        self.assertEqual(3, delivery["counts"]["approved_profile_excluded"])
        self.assertEqual(
            sorted(item["profile_item_id"] for item in self.items),
            [item["profile_item_id"] for item in delivery["layers"]["approved_profile"]["items"]],
        )
        self.assertNotIn("rejected", {item["approval"]["status"] for item in delivery["layers"]["approved_profile"]["items"]})
        self.assertNotIn("pending", {item["approval"]["status"] for item in delivery["layers"]["approved_profile"]["items"]})

    def test_lexical_fallback_is_source_backed_diverse_and_explicitly_degraded(self):
        delivery = self.build()
        evidence = delivery["layers"]["historical_evidence"]["items"]
        combined = " ".join(item["excerpt"] for item in evidence).casefold()
        self.assertGreater(len(evidence), 0)
        self.assertIn("60hz", combined)
        self.assertIn("24g", combined)
        self.assertIn("15,000", combined)
        self.assertTrue(all(item["source_anchor"]["role"] == "user" for item in evidence))
        self.assertTrue(all(item["source_anchor"]["conversation_id"] and item["source_anchor"]["message_id"] for item in evidence))
        self.assertEqual("active", delivery["capability_state"]["lexical_fallback"]["status"])
        self.assertEqual("degraded", delivery["capability_state"]["degradation"]["status"])
        self.assertTrue(any(item["freshness"]["status"] == "potentially_stale" for item in evidence))
        self.assertTrue(any(item["temporal"]["potentially_conflicting"] for item in evidence))
        reasons = {item["reason_code"] for item in delivery["uncertainties"]}
        self.assertIn("unsupported_product_conclusion", reasons)
        self.assertIn("volatile_price_or_promotion", reasons)

    def test_delivery_assembly_and_runtime_bindings_cover_exact_contents(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first["delivery_identity"], second["delivery_identity"])
        self.assertEqual(first["assembly_binding"], second["assembly_binding"])
        binding = production_profile_binding(first)
        expected_ids = [item["profile_item_id"] for item in first["layers"]["approved_profile"]["items"]]
        self.assertEqual(expected_ids, binding["profile_item_ids"])

        d1 = build_runtime_enablement_artifact(first, approved_profile_binding=binding)
        validate_runtime_enablement_artifact(d1, trusted_delivery=first)
        thin_binding = {key: value for key, value in binding.items() if key != "projection_protocol_version"}
        bc = build_runtime_request_artifact(first, approved_profile_binding=thin_binding)
        validate_runtime_request_artifact(bc, trusted_delivery=first)
        self.assertEqual(first["delivery_identity"], d1["delivery_binding"]["delivery_identity"])
        self.assertEqual(first["delivery_identity"], bc["delivery_binding"]["delivery_identity"])
        expected_evidence = [
            item["evidence_reference_id"]
            for item in first["layers"]["historical_evidence"]["items"]
        ]
        self.assertEqual(expected_evidence, d1["delivery_binding"]["evidence_reference_ids"])
        self.assertEqual(expected_evidence, bc["delivery_binding"]["evidence_reference_ids"])

    def test_tampering_and_profile_exclusion_fail_closed(self):
        delivery = self.build()
        tampered = deepcopy(delivery)
        tampered["layers"]["historical_evidence"]["items"][0]["source_anchor"]["message_id"] = "forged"
        with self.assertRaises(ProductionDeliveryValidationError):
            validate_production_delivery_standalone(tampered)
        with self.assertRaisesRegex(ProductionDeliveryValidationError, "cannot be excluded"):
            self.core.build(
                RetrievalQuery(QUESTION), budget_profile="Large",
                excluded_item_ids=[self.items[0]["profile_item_id"]],
            )

        tampered = deepcopy(delivery)
        tampered["source_assembly"]["historical_evidence_bindings"] = []
        tampered["source_assembly"]["assembly_identity"] = "pca_" + _digest({
            key: value for key, value in tampered["source_assembly"].items()
            if key != "assembly_identity"
        })
        tampered["assembly_binding"]["assembly_identity"] = tampered["source_assembly"]["assembly_identity"]
        tampered["delivery_identity"] = "pdv_" + _digest({
            key: value for key, value in tampered.items() if key != "delivery_identity"
        })
        with self.assertRaisesRegex(ProductionDeliveryValidationError, "assembly evidence binding"):
            validate_production_delivery_standalone(tampered)

    def test_production_surface_is_fixed_and_v74_protocol_is_unchanged(self):
        self.assertEqual("v7.4-transparent-context-delivery-1", SEALED_V74_PROTOCOL_VERSION)
        self.assertEqual(0, len(inspect.signature(ProductionDeliveryService).parameters))


if __name__ == "__main__":
    unittest.main()
