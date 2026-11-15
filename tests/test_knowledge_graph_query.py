from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.graph_identity import canonical_hash, make_edge
from memory.knowledge_graph_query import (
    KnowledgeGraphQuery,
    KnowledgeGraphQueryError,
    KnowledgeGraphQueryService,
)
from memory.knowledge_sidecar import (
    PRODUCTION_KNOWLEDGE_SIDECAR,
    TestKnowledgeGraphRepository,
    initialize_test_knowledge_sidecar,
)
from memory.memory_graph import build_memory_graph
from tests.test_memory_graph_identity import summary_record
from tools.query_knowledge_graph import parse_args
from tools.local_web import AyunOSHandler
from memory.controlled_knowledge_persistence import PERSISTENCE_PROTOCOL_IDENTITY
from memory.knowledge_sidecar import load_knowledge_store_identity
from memory.memory_graph import MemoryGraphBuild
from memory.relation_review import discover_relation_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeGraphQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "knowledge.db"
        initialize_test_knowledge_sidecar(self.db)
        records = [
            summary_record(source_id="1", conversation_id="c1", memory_space="whale"),
            summary_record(source_id="2", conversation_id="c2", memory_space="general"),
        ]
        base = build_memory_graph(records, [])
        self.nodes = {node.node_id: node for node in base.nodes}
        self.summary_nodes = sorted(
            (node for node in base.nodes if node.node_type == "summary"),
            key=lambda node: node.node_id,
        )
        source, target = self.summary_nodes
        candidate_edge = make_edge(
            "semantic_related", source.node_id, target.node_id,
            direction="undirected", generator_identity="deterministic_test",
            generator_config_hash="config_" + "a" * 64,
            evidence_identity="evidence_" + "b" * 64,
            status="candidate", score=0.91, threshold=0.8,
        )
        self.build = MemoryGraphBuild(
            schema_version=base.schema_version,
            generator_config_hash=base.generator_config_hash,
            nodes=base.nodes,
            edges=tuple((*base.edges, candidate_edge)),
            unavailable=base.unavailable,
        )
        TestKnowledgeGraphRepository(self.db).append_build(
            self.build, generated_at="2026-01-01T00:00:00Z",
        )
        discovery = discover_relation_candidates(self.build, created_at="2026-01-01T00:00:01Z")
        candidate = next(item for item in discovery.candidates if item.relation_type == "semantic_related")
        self.candidate = candidate
        self._insert_receipt_backed_formal(candidate)
        self.service = KnowledgeGraphQueryService.for_test(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _tamper(self, triggers: dict[str, str], statements: list[tuple[str, tuple]]) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            for trigger in triggers:
                conn.execute(f"DROP TRIGGER {trigger}")
            for sql, params in statements:
                conn.execute(sql, params)
            for sql in triggers.values():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _insert_receipt_backed_formal(self, candidate) -> None:
        conn = sqlite3.connect(self.db)
        try:
            store = load_knowledge_store_identity(conn)
            plan_id = "plan_" + "4" * 64
            plan_payload_hash = "5" * 64
            decision_id = "decision_" + "6" * 64
            decision_payload_hash = "7" * 64
            authority_store_id = "authority_" + "8" * 64
            authority_generation = "generation_" + "9" * 64
            authority_mutation_seq = 1
            authority_mutation_digest = "c" * 64
            backup_identity = "backup_" + "e" * 64
            anchor_binding = {
                "anchor_enabled": False,
                "anchor_satisfied": False,
                "anchor_identity": None,
                "anchor_assertion_digest": None,
                "anchor_reason_digest": canonical_hash(
                    "external_anchor_reason", {"reason": "external_anchor_not_configured"},
                ),
            }
            store_binding = {
                "knowledge_store_id": store["knowledge_store_id"],
                "knowledge_generation": store["knowledge_generation"],
                "knowledge_initialization_seal": store["initialization_seal"],
                "knowledge_schema_fingerprint": store["schema_fingerprint"],
                "knowledge_metadata_digest": store["metadata_digest"],
            }
            persistence_payload = {
                "protocol": PERSISTENCE_PROTOCOL_IDENTITY,
                "candidate_id": candidate.candidate_id,
                "candidate_payload_hash": candidate.payload_hash,
                "review_plan_id": plan_id,
                "plan_payload_hash": plan_payload_hash,
                "decision_id": decision_id,
                "decision_payload_hash": decision_payload_hash,
                "authority_store_id": authority_store_id,
                "authority_generation": authority_generation,
                "authority_mutation_seq": authority_mutation_seq,
                "authority_mutation_digest": authority_mutation_digest,
                **store_binding,
                **anchor_binding,
            }
            persistence_id = "persistence_" + canonical_hash(
                "accepted_relation_persistence", persistence_payload,
            )
            transaction_identity = "transaction_" + canonical_hash(
                "knowledge_persistence_transaction",
                {
                    "persistence_id": persistence_id,
                    "backup_identity": backup_identity,
                    "authority_mutation_seq": authority_mutation_seq,
                    "authority_mutation_digest": authority_mutation_digest,
                    **store_binding,
                    **anchor_binding,
                },
            )
            receipt_id = "receipt_" + canonical_hash(
                "knowledge_persistence_receipt", {"transaction_identity": transaction_identity},
            )
            persisted_at = "2026-01-01T00:00:03Z"
            values = (
                persistence_id, 3, 1, store["knowledge_store_id"], store["knowledge_generation"],
                store["metadata_digest"], candidate.candidate_id, candidate.payload_hash,
                plan_id, plan_payload_hash, decision_id, decision_payload_hash,
                authority_store_id, authority_generation, "a" * 64, "chain_" + "b" * 64,
                candidate.relation_type, candidate.source_node_id, candidate.source_node_payload_hash,
                candidate.target_node_id, candidate.target_node_payload_hash, candidate.direction,
                candidate.generator_identity, candidate.generator_config_hash, candidate.evidence_digest,
                candidate.graph_build_identity, candidate.score, candidate.threshold,
                "2026-01-01T00:00:02Z", persisted_at, "active",
            )
            conn.execute(
                "INSERT INTO accepted_relation_versions VALUES(" + ",".join("?" for _ in values) + ")",
                values,
            )
            receipt_values = (
                receipt_id, persistence_id, store["knowledge_store_id"], store["knowledge_generation"],
                store["metadata_digest"], transaction_identity, backup_identity,
                authority_mutation_seq, authority_mutation_digest, 1, 0, 0, None, None,
                anchor_binding["anchor_reason_digest"], 0, persisted_at,
            )
            conn.execute(
                "INSERT INTO persistence_receipts VALUES(" + ",".join("?" for _ in receipt_values) + ")",
                receipt_values,
            )
            conn.commit()
            self.persistence_id = persistence_id
            self.receipt_id = receipt_id
            self.transaction_identity = transaction_identity
        finally:
            conn.close()

    def test_formal_candidate_and_deterministic_relations_are_strictly_separated(self):
        default = self.service.query(KnowledgeGraphQuery(limit=100, max_depth=3))
        self.assertEqual(1, len(default["relations"]["formal"]))
        self.assertGreaterEqual(len(default["relations"]["deterministic"]), 1)
        self.assertEqual([], default["relations"]["candidates"])
        self.assertEqual([], default["relations"]["unavailable"])
        formal = default["relations"]["formal"][0]
        self.assertEqual(1, formal["relation_version"])
        self.assertEqual(self.candidate.evidence_digest, formal["evidence_identity"])
        self.assertEqual(self.receipt_id, formal["receipt_id"])
        self.assertEqual(self.transaction_identity, formal["transaction_identity"])
        self.assertEqual("accepted_relation_version", formal["explanation"]["basis_code"])

        included = self.service.query(KnowledgeGraphQuery(
            limit=100, max_depth=3, include_candidates=True,
        ))
        returned_node_ids = {node["node_id"] for node in included["nodes"]}
        self.assertTrue(all(
            {item["source_node_id"], item["target_node_id"]} <= returned_node_ids
            for group in included["relations"].values() for item in group
        ))
        self.assertEqual(1, len(included["relations"]["formal"]))
        self.assertEqual(1, len(included["relations"]["candidates"]))
        candidate = included["relations"]["candidates"][0]
        self.assertEqual("candidate", candidate["status"])
        self.assertEqual("semantic_candidate", candidate["explanation"]["basis_code"])
        self.assertNotEqual(formal["relation_id"], candidate["relation_id"])
        self.assertNotEqual(formal["evidence_identity"], candidate["evidence_identity"])

    def test_nonexistent_explicit_filters_return_empty_results(self):
        requests = (
            KnowledgeGraphQuery(conversation_id="missing-conversation", include_candidates=True),
            KnowledgeGraphQuery(node_id="node_" + "f" * 64, include_candidates=True),
            KnowledgeGraphQuery(memory_space="missing-space", include_candidates=True),
        )
        for request in requests:
            with self.subTest(query=request.public_dict()):
                payload = self.service.query(request)
                self.assertEqual([], payload["nodes"])
                self.assertEqual([], payload["paths"])
                self.assertEqual([], payload["related_memories"])
                self.assertTrue(all(not group for group in payload["relations"].values()))
                self.assertEqual(0, sum(
                    payload["counts"][key] for key in (
                        "matched_nodes", "formal_relations", "deterministic_relations",
                        "candidate_relations", "unavailable_relations", "paths", "related_memories",
                    )
                ))

    def test_query_is_stable_bounded_and_paths_do_not_repeat_nodes(self):
        request = KnowledgeGraphQuery(limit=100, max_depth=3, include_candidates=True)
        first = self.service.query(request)
        second = self.service.query(request)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first["paths"]), 100)
        for path in first["paths"]:
            self.assertLessEqual(path["depth"], 3)
            self.assertEqual(len(path["node_ids"]), len(set(path["node_ids"])))

    def test_limit_is_relation_path_budget_with_complete_endpoint_closure(self):
        payload = self.service.query(KnowledgeGraphQuery(
            limit=1, max_depth=3, include_candidates=True,
        ))
        traversable_count = sum(
            len(payload["relations"][group]) for group in ("formal", "deterministic", "candidates")
        )
        self.assertLessEqual(traversable_count, 1)
        self.assertLessEqual(len(payload["relations"]["unavailable"]), 1)
        self.assertLessEqual(len(payload["paths"]), 1)
        self.assertLessEqual(len(payload["related_memories"]), 1)
        node_ids = {node["node_id"] for node in payload["nodes"]}
        self.assertTrue(all(
            {relation["source_node_id"], relation["target_node_id"]} <= node_ids
            for group in payload["relations"].values() for relation in group
        ))

    def test_conversation_space_relation_node_and_depth_filters_apply(self):
        by_conversation = self.service.query(KnowledgeGraphQuery(
            conversation_id="c1", max_depth=2, limit=100,
        ))
        self.assertTrue(by_conversation["nodes"])
        self.assertTrue(any(node["conversation_id"] == "c1" for node in by_conversation["nodes"]))
        self.assertTrue(by_conversation["related_memories"])

        by_space = self.service.query(KnowledgeGraphQuery(
            memory_space="whale", max_depth=1, limit=100,
        ))
        self.assertTrue(any(node["memory_space"] == "whale" for node in by_space["nodes"]))

        semantic = self.service.query(KnowledgeGraphQuery(
            relation_type="semantic_related", include_candidates=True, max_depth=2, limit=100,
        ))
        for group in semantic["relations"].values():
            self.assertTrue(all(item["relation_type"] == "semantic_related" for item in group))

        node_id = self.summary_nodes[0].node_id
        depth_zero = self.service.query(KnowledgeGraphQuery(node_id=node_id, max_depth=0))
        self.assertEqual([node_id], [node["node_id"] for node in depth_zero["nodes"]])
        self.assertEqual([], depth_zero["paths"])
        self.assertTrue(all(not group for group in depth_zero["relations"].values()))

    def test_output_is_redacted_and_contains_no_path_body_embedding_or_metadata(self):
        payload = self.service.query(KnowledgeGraphQuery(
            include_candidates=True, max_depth=3, limit=100,
        ))
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            str(self.root), '"summary_text":', "embedding_json", "metadata_json", "prompt",
            "TOP_SECRET", "accepted_at", "persisted_at",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(all(
            relation["explanation"]["redacted"]
            for group in payload["relations"].values() for relation in group
        ))

    def test_formal_node_payload_mismatch_fails_closed(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DROP TRIGGER accepted_relation_versions_no_update")
            conn.execute(
                "UPDATE accepted_relation_versions SET source_node_payload_hash=?",
                ("0" * 64,),
            )
            conn.execute(
                "CREATE TRIGGER accepted_relation_versions_no_update "
                "BEFORE UPDATE ON accepted_relation_versions "
                "BEGIN SELECT RAISE(ABORT, 'accepted_relation_versions is append-only'); END"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(Exception, "candidate identity or payload mismatch|payload binding mismatch"):
            self.service.query()

    def test_missing_receipt_cannot_become_formal(self):
        self._tamper(
            {"persistence_receipts_no_delete": (
                "CREATE TRIGGER persistence_receipts_no_delete BEFORE DELETE ON persistence_receipts "
                "BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END"
            )},
            [("DELETE FROM persistence_receipts", ())],
        )
        with self.assertRaisesRegex(Exception, "receipt is missing"):
            self.service.query()

    def test_wrong_receipt_identity_cannot_become_formal(self):
        self._tamper(
            {"persistence_receipts_no_update": (
                "CREATE TRIGGER persistence_receipts_no_update BEFORE UPDATE ON persistence_receipts "
                "BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END"
            )},
            [("UPDATE persistence_receipts SET receipt_id=?", ("receipt_" + "0" * 64,))],
        )
        with self.assertRaisesRegex(Exception, "receipt identity mismatch"):
            self.service.query()

    def test_cross_store_receipt_cannot_become_formal(self):
        self._tamper(
            {"persistence_receipts_no_update": (
                "CREATE TRIGGER persistence_receipts_no_update BEFORE UPDATE ON persistence_receipts "
                "BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END"
            )},
            [("UPDATE persistence_receipts SET knowledge_store_id=?", ("knowledge_" + "0" * 64,))],
        )
        with self.assertRaisesRegex(Exception, "foreign keys are invalid|store identity mismatch"):
            self.service.query()

    def test_transaction_identity_mismatch_cannot_become_formal(self):
        self._tamper(
            {"persistence_receipts_no_update": (
                "CREATE TRIGGER persistence_receipts_no_update BEFORE UPDATE ON persistence_receipts "
                "BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END"
            )},
            [("UPDATE persistence_receipts SET transaction_identity=?", ("transaction_" + "0" * 64,))],
        )
        with self.assertRaisesRegex(Exception, "transaction identity mismatch"):
            self.service.query(KnowledgeGraphQuery(relation_type="belongs_to_conversation"))

    def test_persistence_and_decision_binding_mismatch_cannot_become_formal(self):
        self._tamper(
            {
                "accepted_relation_versions_no_update": (
                    "CREATE TRIGGER accepted_relation_versions_no_update BEFORE UPDATE ON accepted_relation_versions "
                    "BEGIN SELECT RAISE(ABORT, 'accepted_relation_versions is append-only'); END"
                ),
            },
            [("UPDATE accepted_relation_versions SET decision_payload_hash=?", ("0" * 64,))],
        )
        with self.assertRaisesRegex(Exception, "persistence identity mismatch"):
            self.service.query()

    def test_persistence_id_mismatch_cannot_become_formal(self):
        wrong = "persistence_" + "0" * 64
        self._tamper(
            {
                "accepted_relation_versions_no_update": (
                    "CREATE TRIGGER accepted_relation_versions_no_update BEFORE UPDATE ON accepted_relation_versions "
                    "BEGIN SELECT RAISE(ABORT, 'accepted_relation_versions is append-only'); END"
                ),
                "persistence_receipts_no_update": (
                    "CREATE TRIGGER persistence_receipts_no_update BEFORE UPDATE ON persistence_receipts "
                    "BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END"
                ),
            },
            [
                ("UPDATE persistence_receipts SET persistence_id=?", (wrong,)),
                ("UPDATE accepted_relation_versions SET persistence_id=?", (wrong,)),
            ],
        )
        with self.assertRaisesRegex(Exception, "persistence identity mismatch"):
            self.service.query()

    def test_real_v62_gateway_receipt_remains_queryable(self):
        from tests.test_controlled_knowledge_persistence import ControlledKnowledgePersistenceTests

        case = ControlledKnowledgePersistenceTests(methodName="test_preview_confirmation_and_idempotency")
        case.setUp()
        try:
            persisted = case.persist()
            payload = KnowledgeGraphQueryService.for_test(case.db).query(
                KnowledgeGraphQuery(relation_type="semantic_related", limit=10),
            )
            self.assertEqual(1, payload["counts"]["formal_relations"])
            formal = payload["relations"]["formal"][0]
            self.assertEqual(persisted["persistence_id"], formal["relation_id"])
            self.assertEqual(persisted["receipt_id"], formal["receipt_id"])
        finally:
            case.tearDown()

    def test_graph_edge_evidence_cannot_be_cross_bound_to_unchanged_edge_id(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DROP TRIGGER graph_edges_no_update")
            conn.execute(
                "UPDATE graph_edges SET evidence_identity=? WHERE status='candidate'",
                ("evidence_wrong_" + "f" * 64,),
            )
            conn.execute(
                "CREATE TRIGGER graph_edges_no_update BEFORE UPDATE ON graph_edges "
                "BEGIN SELECT RAISE(ABORT, 'graph_edges is append-only'); END"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(Exception, "edge identity mismatch"):
            self.service.query(KnowledgeGraphQuery(include_candidates=True))

    def test_empty_initialized_store_reports_no_formal_relations(self):
        empty = self.root / "empty.db"
        initialize_test_knowledge_sidecar(empty)
        payload = KnowledgeGraphQueryService.for_test(empty).query()
        self.assertEqual("ok", payload["status"])
        self.assertEqual("unavailable", payload["availability"]["formal_relations"])
        self.assertEqual("no_formal_relations", payload["availability"]["formal_relation_reason"])
        self.assertEqual(0, payload["counts"]["formal_relations"])

    def test_stored_unavailable_relation_is_isolated_and_never_traversed(self):
        source = self.summary_nodes[0]
        target = next(node for node in self.build.nodes if node.node_type == "conversation")
        unavailable = make_edge(
            "semantic_related", source.node_id, target.node_id,
            direction="undirected", generator_identity="deterministic_test",
            generator_config_hash="config_" + "f" * 64,
            evidence_identity="evidence_unavailable_" + "0" * 64,
            status="unavailable", score=None, threshold=0.8,
        )
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    unavailable.edge_id, unavailable.edge_schema_version, unavailable.relation_type,
                    unavailable.source_node_id, unavailable.target_node_id, unavailable.direction,
                    unavailable.generator_identity, unavailable.generator_config_hash,
                    unavailable.evidence_identity, unavailable.score, unavailable.threshold,
                    unavailable.status, "2026-01-01T00:00:04Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        payload = self.service.query(KnowledgeGraphQuery(include_candidates=True, max_depth=3, limit=100))
        self.assertEqual(1, payload["counts"]["unavailable_relations"])
        relation = payload["relations"]["unavailable"][0]
        self.assertEqual("unavailable", relation["relation_class"])
        self.assertEqual("unavailable_relation", relation["explanation"]["basis_code"])
        self.assertTrue(all(
            relation["relation_id"] not in path["relation_ids"] for path in payload["paths"]
        ))

    def test_production_absence_is_stable_and_does_not_initialize(self):
        before = PRODUCTION_KNOWLEDGE_SIDECAR.exists()
        if before:
            self.skipTest("production knowledge sidecar is initialized")
        first = KnowledgeGraphQueryService().query()
        second = KnowledgeGraphQueryService().query()
        self.assertEqual(first, second)
        self.assertEqual("unavailable", first["status"])
        self.assertEqual("knowledge_sidecar_not_initialized", first["reason_code"])
        self.assertFalse(PRODUCTION_KNOWLEDGE_SIDECAR.exists())

    def test_invalid_limits_and_write_style_cli_flags_are_refused(self):
        for query in (KnowledgeGraphQuery(limit=0), KnowledgeGraphQuery(max_depth=4)):
            with self.assertRaises(KnowledgeGraphQueryError):
                query.normalized()
        for forbidden in ("--db", "--write", "--execute", "--accept", "--authority"):
            with self.subTest(flag=forbidden), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args([forbidden, "value"])

    def test_cli_subprocess_is_readonly_and_machine_safe(self):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "query_knowledge_graph.py"), "--json"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["readonly"])
        self.assertFalse(payload["executed"])
        self.assertNotIn(str(PROJECT_ROOT), completed.stdout)

    def test_api_contract_forwards_only_bounded_readonly_query_fields(self):
        handler = object.__new__(AyunOSHandler)
        sent: list[dict] = []
        handler.send_json = lambda payload, status=None: sent.append(payload)
        with patch("tools.local_web.query_production_knowledge_graph", return_value={"status": "ok"}) as query:
            handler.api_knowledge_graph_query({
                "conversation_id": ["c1"], "memory_space": ["whale"],
                "relation_type": ["semantic_related"], "node_id": [self.summary_nodes[0].node_id],
                "max_depth": ["3"], "limit": ["10"], "include_candidates": ["true"],
            })
        self.assertEqual([{"status": "ok"}], sent)
        request = query.call_args.args[0]
        self.assertEqual(("c1", "whale", "semantic_related", 3, 10, True), (
            request.conversation_id, request.memory_space, request.relation_type,
            request.max_depth, request.limit, request.include_candidates,
        ))

    def test_api_rejects_unknown_or_unbounded_parameters(self):
        handler = object.__new__(AyunOSHandler)
        errors: list[tuple[str, object]] = []
        handler.send_error_json = lambda message, status=None: errors.append((message, status))
        handler.send_json = lambda payload, status=None: self.fail("invalid query was accepted")
        handler.api_knowledge_graph_query({"db": ["secret.db"]})
        handler.api_knowledge_graph_query({"max_depth": ["99"]})
        handler.api_knowledge_graph_query({"include_candidates": ["maybe"]})
        self.assertEqual(3, len(errors))


if __name__ == "__main__":
    unittest.main()
