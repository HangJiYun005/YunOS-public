from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.graph_enhanced_retrieval import (
    GraphEnhancedRetrievalService,
    RetrievalQuery,
    RetrievalValidationError,
    validate_retrieval_availability_state,
)
from memory.semantic_memory import SemanticRetrievalError
from tools.graph_enhanced_search import parse_args
from tools.local_web import AyunOSHandler
from tools.migrate_semantic_memory import CREATE_MEMORY_EMBEDDINGS_SQL


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeGraphService:
    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {
            "status": "ok", "reason_code": "query_complete", "nodes": [], "paths": [],
            "relations": {"formal": [], "deterministic": [], "candidates": [], "unavailable": []},
        }
        self.error = error
        self.queries = []

    def query(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.payload


class GraphEnhancedRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.semantic = self.root / "semantic.db"
        self.knowledge = self.root / "knowledge.db"
        self._create_source()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_source(self) -> None:
        conn = sqlite3.connect(self.source)
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
        conversations = [
            ("c1", "Mars research", 1735689600.0, "2025-01-01T00:00:00+00:00", 1735862400.0, "2025-01-03T00:00:00+00:00"),
            ("c2", "Victorian fiction", 1735776000.0, "2025-01-02T00:00:00+00:00", 1735948800.0, "2025-01-04T00:00:00+00:00"),
            ("c3", "Old device notes", 1704067200.0, "2024-01-01T00:00:00+00:00", 1704067200.0, "2024-01-01T00:00:00+00:00"),
        ]
        messages = [
            ("m1", "c1", "Mars research", 1, "user", 1735689600.0, "2025-01-01T00:00:00+00:00", "alpha launch notes", "general"),
            ("m2", "c1", "Mars research", 2, "assistant", 1735776000.0, "2025-01-02T00:00:00+00:00", "alpha trajectory details", "general"),
            ("m3", "c1", "Mars research", 3, "assistant", 1735862400.0, "2025-01-03T00:00:00+00:00", "landing conclusion", "general"),
            ("m4", "c2", "Victorian fiction", 1, "user", 1735776000.0, "2025-01-02T00:00:00+00:00", "an unseen presence in a classic story", "general"),
            ("m5", "c2", "Victorian fiction", 2, "assistant", 1735948800.0, "2025-01-04T00:00:00+00:00", "literary conclusion", "general"),
            ("m6", "c3", "Old device notes", 1, "user", 1704067200.0, "2024-01-01T00:00:00+00:00", "alpha computer hardware", "general"),
        ]
        conn.executemany("INSERT INTO conversations VALUES(?,?,?,?,?,?)", conversations)
        conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", messages)
        conn.commit(); conn.close()

    def _semantic_payload(self, conversation_id: str = "c2", score: float = 0.91) -> dict:
        return {
            "results": [{
                "rank": 1, "score": score, "source_id": "s1",
                "source_table": "memory_summaries", "memory_space": "general",
                "content_preview": "A supernatural literary discussion.",
                "summary_type": "conversation_summary", "conversation_id": conversation_id,
            }]
        }

    def _service(self, *, semantic=None, graph=None, semantic_exists=True, knowledge_exists=True):
        if semantic_exists and not self.semantic.exists():
            conn = sqlite3.connect(self.semantic)
            try:
                conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
                conn.commit()
            finally:
                conn.close()
        if knowledge_exists:
            self.knowledge.touch()
        return GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_provider="deterministic_test",
            semantic_searcher=semantic or (lambda *args, **kwargs: {"results": []}),
            graph_service=graph,
        )

    def _graph_payload(self, *, relation_class="formal", target="c2", include_candidate=False):
        relation = {
            "relation_id": "r1", "relation_class": relation_class,
            "relation_type": "semantic_related", "evidence_identity": "evidence_" + "a" * 64,
        }
        relations = {"formal": [], "deterministic": [], "candidates": [], "unavailable": []}
        relations["candidates" if include_candidate else relation_class].append(relation)
        return {
            "status": "ok", "reason_code": "query_complete",
            "nodes": [
                {"node_id": "n1", "conversation_id": "c1"},
                {"node_id": "n2", "conversation_id": target},
            ],
            "relations": relations,
            "paths": [{"path_id": "p1", "node_ids": ["n1", "n2"], "relation_ids": ["r1"]}],
        }

    def test_default_query_searches_all_time(self):
        payload = self._service(graph=FakeGraphService()).search(RetrievalQuery(
            "alpha", hit_types=("keyword",), limit=20,
        ))
        self.assertEqual(["m2", "m1", "m6"], [item["anchor_message_id"] for item in payload["results"]])
        self.assertIsNone(payload["query"]["start_time"])
        self.assertIsNone(payload["query"]["end_time"])

    def test_start_end_and_bilateral_boundaries_are_inclusive(self):
        service = self._service(graph=FakeGraphService())
        start = service.search(RetrievalQuery("alpha", start_time="2025-01-02", hit_types=("keyword",)))
        end = service.search(RetrievalQuery("alpha", end_time="2025-01-01", hit_types=("keyword",)))
        both = service.search(RetrievalQuery(
            "alpha", start_time="2025-01-01T00:00:00Z", end_time="2025-01-02T00:00:00Z",
            hit_types=("keyword",),
        ))
        self.assertEqual(["m2"], [item["anchor_message_id"] for item in start["results"]])
        self.assertEqual(["m1", "m6"], [item["anchor_message_id"] for item in end["results"]])
        self.assertEqual(["m2", "m1"], [item["anchor_message_id"] for item in both["results"]])

    def test_semantic_recovers_content_without_keyword_and_uses_stable_anchor(self):
        semantic = Mock(return_value=self._semantic_payload())
        payload = self._service(semantic=semantic, graph=FakeGraphService()).search(RetrievalQuery(
            "ghostly visitor", hit_types=("keyword", "semantic"),
        ))
        self.assertEqual(1, payload["result_count"])
        item = payload["results"][0]
        self.assertEqual("c2", item["conversation_id"])
        self.assertEqual("m5", item["anchor_message_id"])
        self.assertEqual(["semantic"], item["hit_types"])
        self.assertIn("semantically similar", item["hit_reason"])
        self.assertEqual("v6.4-retrieval-2", payload["protocol_version"])
        self.assertEqual("m5", item["evidence"][0]["source"]["anchor_message_id"])
        semantic.assert_called_once()

    def test_bounded_time_disables_unprovable_summary_semantic_and_graph_seed(self):
        semantic = Mock(return_value=self._semantic_payload("c2"))
        graph = FakeGraphService(self._graph_payload())
        payload = self._service(semantic=semantic, graph=graph).search(RetrievalQuery(
            "ghostly visitor", start_time="2025-01-02", end_time="2025-01-02",
            hit_types=("keyword", "semantic", "graph"),
        ))
        self.assertEqual([], payload["results"])
        semantic.assert_not_called()
        self.assertEqual([], graph.queries)
        self.assertEqual(
            "semantic_time_evidence_unprovable",
            payload["availability"]["semantic_sidecar"]["reason_code"],
        )
        self.assertEqual(
            "no_direct_seed", payload["availability"]["knowledge_sidecar"]["reason_code"],
        )

    def test_graph_expansion_is_seeded_explained_and_candidate_excluded(self):
        graph = FakeGraphService(self._graph_payload())
        payload = self._service(graph=graph).search(RetrievalQuery(
            "alpha", start_time="2025-01-01", hit_types=("keyword", "graph"), graph_depth=1,
        ))
        graph_items = [item for item in payload["results"] if item["hit_types"] == ["graph"]]
        self.assertEqual(1, len(graph_items))
        evidence = graph_items[0]["evidence"][0]
        self.assertEqual("c1", evidence["seed_conversation_id"])
        self.assertEqual(["keyword"], evidence["seed_hit_types"])
        self.assertEqual(("c1", "m2"), (
            evidence["path_start_conversation_id"],
            evidence["path_start_anchor_message_id"],
        ))
        self.assertEqual("formal", evidence["relations"][0]["relation_class"])
        self.assertEqual(graph_items[0]["conversation_id"], evidence["source"]["conversation_id"])
        self.assertEqual(graph_items[0]["anchor_message_id"], evidence["source"]["anchor_message_id"])
        self.assertFalse(graph.queries[0].include_candidates)

        candidate = FakeGraphService(self._graph_payload(relation_class="candidate", include_candidate=True))
        excluded = self._service(graph=candidate).search(RetrievalQuery(
            "alpha", start_time="2025-01-01", hit_types=("keyword", "graph"),
        ))
        self.assertFalse(any(item["hit_types"] == ["graph"] for item in excluded["results"]))

    def test_time_filter_cannot_be_bypassed_by_graph(self):
        graph = FakeGraphService(self._graph_payload(target="c3"))
        payload = self._service(graph=graph).search(RetrievalQuery(
            "alpha", start_time="2025-01-01", hit_types=("keyword", "graph"),
        ))
        self.assertNotIn("c3", {item["conversation_id"] for item in payload["results"]})

    def test_context_window_cannot_leak_messages_outside_time_filter(self):
        payload = self._service(graph=FakeGraphService()).search(RetrievalQuery(
            "alpha", start_time="2025-01-02", end_time="2025-01-02",
            hit_types=("keyword",), context_window=5,
        ))
        context_ids = {
            context["message_id"] for item in payload["results"] for context in item["context"]
        }
        self.assertEqual({"m2"}, context_ids)

    def test_no_seed_fails_closed_without_graph_query(self):
        graph = FakeGraphService(self._graph_payload())
        payload = self._service(graph=graph).search(RetrievalQuery(
            "definitely absent", hit_types=("keyword", "graph"),
        ))
        self.assertEqual([], graph.queries)
        self.assertEqual("no_direct_seed", payload["availability"]["knowledge_sidecar"]["reason_code"])

    def test_graph_only_is_rejected_before_retrieval(self):
        graph = FakeGraphService(self._graph_payload())
        service = self._service(graph=graph)
        with self.assertRaisesRegex(RetrievalValidationError, "graph_requires_direct_channel"):
            service.search(RetrievalQuery("alpha", hit_types=("graph",)))
        self.assertEqual([], graph.queries)

    def test_unexecutable_direct_channel_is_not_reported_as_no_direct_seed(self):
        graph = FakeGraphService(self._graph_payload())
        payload = self._service(
            semantic=lambda *args, **kwargs: {"results": []},
            graph=graph, semantic_exists=False,
        ).search(RetrievalQuery("alpha", hit_types=("semantic", "graph")))
        self.assertEqual(
            "direct_channel_unavailable",
            payload["availability"]["knowledge_sidecar"]["reason_code"],
        )
        self.assertEqual((0, 0, []), (
            payload["result_count"], payload["total_ranked"], payload["results"],
        ))
        self.assertEqual([], graph.queries)

    def test_paged_graph_evidence_carries_its_exact_direct_seed_proof(self):
        service = self._service(graph=FakeGraphService(self._graph_payload()))
        cursor = None
        graph_page = None
        for _ in range(10):
            payload = service.search(RetrievalQuery(
                "alpha", hit_types=("keyword", "graph"), limit=1, cursor=cursor,
            ))
            graph_evidence = [
                item
                for result in payload["results"]
                for item in result["evidence"]
                if item["hit_type"] == "graph"
            ]
            if graph_evidence:
                graph_page = payload
                break
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        self.assertIsNotNone(graph_page)
        direct = {
            (item["source"]["conversation_id"], item["source"]["anchor_message_id"], item["hit_type"])
            for result in graph_page["results"]
            for item in result["evidence"]
            if item["hit_type"] in {"keyword", "semantic"}
        }
        for item in graph_evidence:
            self.assertEqual(
                {(item["seed_conversation_id"], item["seed_anchor_message_id"], hit_type)
                 for hit_type in item["seed_hit_types"]},
                {key for key in direct if key[:2] == (
                    item["seed_conversation_id"], item["seed_anchor_message_id"],
                )},
            )

    def test_semantic_direct_seed_reaches_graph_and_zero_direct_hits_do_not(self):
        graph = FakeGraphService(self._graph_payload())
        semantic = Mock(return_value=self._semantic_payload("c1"))
        seeded = self._service(semantic=semantic, graph=graph).search(RetrievalQuery(
            "semantic-only", hit_types=("semantic", "graph"),
        ))
        graph_evidence = [
            evidence
            for result in seeded["results"]
            for evidence in result["evidence"]
            if evidence["hit_type"] == "graph"
        ]
        self.assertTrue(graph_evidence)
        self.assertTrue(all(item["seed_hit_types"] == ["semantic"] for item in graph_evidence))

        no_hits_graph = FakeGraphService(self._graph_payload())
        empty = self._service(
            semantic=lambda *args, **kwargs: {"results": []}, graph=no_hits_graph,
        ).search(RetrievalQuery(
            "definitely absent", hit_types=("keyword", "semantic", "graph"),
        ))
        self.assertEqual([], no_hits_graph.queries)
        self.assertEqual((0, 0, "no_direct_seed"), (
            empty["result_count"], empty["total_ranked"],
            empty["availability"]["knowledge_sidecar"]["reason_code"],
        ))

    def test_multi_evidence_merges_by_conversation_and_direct_stays_ahead_of_graph(self):
        semantic = Mock(return_value=self._semantic_payload("c1", 0.8))
        graph = FakeGraphService(self._graph_payload())
        service = self._service(semantic=semantic, graph=graph)
        for mode in ("snippets", "conversation_anchor"):
            with self.subTest(mode=mode):
                payload = service.search(RetrievalQuery(
                    "alpha", result_mode=mode, hit_types=("keyword", "semantic", "graph"),
                ))
                c1 = next(
                    item for item in payload["results"]
                    if item["conversation_id"] == "c1" and "semantic" in item["hit_types"]
                )
                self.assertEqual(["keyword", "semantic"], c1["hit_types"])
                self.assertEqual("keyword", c1["primary_hit_type"])
                self.assertEqual("m2", c1["anchor_message_id"])
                self.assertIn("alpha", c1["snippet"].lower())
                keyword_evidence = [item for item in c1["evidence"] if item["hit_type"] == "keyword"]
                semantic_evidence = next(item for item in c1["evidence"] if item["hit_type"] == "semantic")
                self.assertIn("m2", {
                    item["source"]["anchor_message_id"] for item in keyword_evidence
                })
                self.assertEqual("m3", semantic_evidence["source"]["anchor_message_id"])
                self.assertTrue(all(
                    item["source"]["conversation_id"] == "c1" for item in keyword_evidence
                ))
                self.assertEqual("c1", semantic_evidence["source"]["conversation_id"])
                if mode == "conversation_anchor":
                    self.assertEqual(1, len([
                        item for item in payload["results"] if item["conversation_id"] == "c1"
                    ]))
                self.assertLess(c1["sorting_basis"]["tier"], next(
                    item for item in payload["results"] if item["conversation_id"] == "c2"
                )["sorting_basis"]["tier"])

    def test_two_result_modes_context_and_anchor_uri(self):
        service = self._service(graph=FakeGraphService())
        snippets = service.search(RetrievalQuery("alpha", hit_types=("keyword",), context_window=1))
        anchor = service.search(RetrievalQuery(
            "alpha", hit_types=("keyword",), result_mode="conversation_anchor",
        ))
        self.assertTrue(snippets["results"][0]["context"])
        self.assertTrue(all(not item["context"] for item in anchor["results"]))
        self.assertTrue(all(item["reference_uri"].endswith(item["anchor_message_id"]) for item in anchor["results"]))

    def test_stable_sort_cursor_and_high_frequency_conversation_cap(self):
        service = self._service(graph=FakeGraphService())
        first = service.search(RetrievalQuery("alpha", hit_types=("keyword",), limit=1))
        again = service.search(RetrievalQuery("alpha", hit_types=("keyword",), limit=1))
        self.assertEqual(first["results"], again["results"])
        second = service.search(RetrievalQuery(
            "alpha", hit_types=("keyword",), limit=1, cursor=first["next_cursor"],
        ))
        self.assertNotEqual(first["results"][0]["anchor_message_id"], second["results"][0]["anchor_message_id"])

    def test_budget_edges_and_two_hop_limit(self):
        graph = FakeGraphService(self._graph_payload())
        self._service(graph=graph).search(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), graph_depth=2,
            relation_budget=1, path_budget=1, context_budget=0,
        ))
        self.assertEqual(2, graph.queries[0].max_depth)
        self.assertEqual(1, graph.queries[0].limit)
        with self.assertRaises(RetrievalValidationError):
            RetrievalQuery("x", graph_depth=3).normalized()

    def test_context_character_budget_is_absolute_including_clip_marker(self):
        service = self._service(graph=FakeGraphService())
        for budget in (0, 1, 2, 3, 20):
            with self.subTest(budget=budget):
                payload = service.search(RetrievalQuery(
                    "alpha", hit_types=("keyword",), limit=1,
                    context_window=5, context_budget=budget,
                ))
                actual = sum(
                    len(context["snippet"])
                    for item in payload["results"] for context in item["context"]
                )
                self.assertLessEqual(actual, budget)
                self.assertEqual(budget, payload["budgets"]["max_context_chars"])

    def test_missing_and_corrupt_sidecars_degrade_without_creation(self):
        missing_semantic = self.root / "missing-semantic.db"
        missing_knowledge = self.root / "missing-knowledge.db"
        service = GraphEnhancedRetrievalService(self.source, missing_semantic, missing_knowledge)
        payload = service.search(RetrievalQuery("alpha"))
        self.assertEqual("semantic_sidecar_missing", payload["availability"]["semantic_sidecar"]["reason_code"])
        self.assertEqual("knowledge_sidecar_missing", payload["availability"]["knowledge_sidecar"]["reason_code"])
        self.assertFalse(missing_semantic.exists())
        self.assertFalse(missing_knowledge.exists())

        corrupt = Mock(side_effect=SemanticRetrievalError("bad vector"))
        degraded = self._service(semantic=corrupt, graph=FakeGraphService()).search(RetrievalQuery("alpha"))
        self.assertEqual("semantic_sidecar_invalid", degraded["availability"]["semantic_sidecar"]["reason_code"])
        self.assertGreater(degraded["result_count"], 0)

    def test_semantic_dependency_model_corrupt_and_invalid_reasons_are_distinct(self):
        cases = (
            (
                RuntimeError("Local embedding provider requires sentence-transformers; install it before use"),
                "semantic_dependency_unavailable",
            ),
            (
                RuntimeError("Unable to load the configured local embedding model"),
                "semantic_model_unavailable",
            ),
            (sqlite3.DatabaseError("database disk image is malformed"), "semantic_sidecar_corrupt"),
            (SemanticRetrievalError("Stored embedding vector is invalid"), "semantic_sidecar_invalid"),
        )
        for error, reason in cases:
            with self.subTest(reason=reason):
                semantic = Mock(side_effect=error)
                payload = self._service(
                    semantic=semantic, graph=FakeGraphService(),
                ).search(RetrievalQuery("alpha"))
                self.assertEqual(reason, payload["availability"]["semantic_sidecar"]["reason_code"])
                self.assertGreater(payload["result_count"], 0)

    def test_malformed_semantic_sqlite_is_reported_corrupt_without_creation(self):
        self.semantic.write_bytes(b"not-a-sqlite-database")
        before = self.semantic.read_bytes()
        payload = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_provider="deterministic_test", graph_service=FakeGraphService(),
        ).search(RetrievalQuery("alpha"))
        self.assertEqual(
            "semantic_sidecar_corrupt",
            payload["availability"]["semantic_sidecar"]["reason_code"],
        )
        self.assertEqual(before, self.semantic.read_bytes())
        self.assertFalse(self.knowledge.exists())

        bounded = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_provider="deterministic_test", graph_service=FakeGraphService(),
        ).search(RetrievalQuery("alpha", start_time="2025-01-01"))
        self.assertEqual(
            "semantic_sidecar_corrupt",
            bounded["availability"]["semantic_sidecar"]["reason_code"],
        )

    def test_invalid_time_hit_type_cursor_and_empty_query(self):
        cases = (
            RetrievalQuery(""), RetrievalQuery("x", start_time="bad"),
            RetrievalQuery("x", start_time="2025-02-01", end_time="2025-01-01"),
            RetrievalQuery("x", hit_types=("candidate",)),
            RetrievalQuery("x", hit_types=("graph",)), RetrievalQuery("x", cursor="bad"),
        )
        service = self._service(graph=FakeGraphService())
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(RetrievalValidationError):
                    service.search(case)

    def test_cli_validation_and_privacy_readonly_surface(self):
        args = parse_args(["alpha", "--start-time", "2025-01-01", "--graph-depth", "2"])
        self.assertEqual("alpha", args.query)
        self.assertEqual(2, args.graph_depth)
        module = (PROJECT_ROOT / "memory" / "graph_enhanced_retrieval.py").read_text(encoding="utf-8")
        for forbidden in ("CREATE TABLE", "INSERT INTO", "UPDATE ", "DELETE FROM", "mode=rwc"):
            self.assertNotIn(forbidden, module)
        self.assertIn("mode=ro", module)

    def test_api_forwards_reserved_search_time_anchor_budget_and_cursor_fields(self):
        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(db_path=self.source)
        sent = []
        handler.send_json = lambda payload, status=None: sent.append(payload)
        search = Mock(return_value={"status": "ok"})
        with patch("tools.local_web.GraphEnhancedRetrievalService") as service:
            service.return_value.search = search
            handler.api_graph_enhanced_retrieval({
                "q": ["alpha"], "start_time": ["2025-01-01"],
                "end_time": ["2025-01-02"], "result_mode": ["conversation_anchor"],
                "context_window": ["2"], "limit": ["7"], "cursor": ["cursor"],
                "hit_types": ["keyword,semantic,graph"], "graph_depth": ["2"],
                "relation_budget": ["9"], "path_budget": ["4"],
                "context_budget": ["1000"], "memory_space": ["general"],
            })
        self.assertEqual([{"status": "ok"}], sent)
        request = search.call_args.args[0]
        self.assertEqual(("alpha", "2025-01-01", "2025-01-02", "conversation_anchor", 2, 7), (
            request.query, request.start_time, request.end_time, request.result_mode,
            request.context_window, request.limit,
        ))

    def test_api_rejects_unknown_and_invalid_parameters(self):
        handler = object.__new__(AyunOSHandler)
        errors = []
        handler.send_error_json = lambda message, status=None: errors.append(message)
        handler.send_json = lambda payload, status=None: self.fail("invalid request accepted")
        handler.api_graph_enhanced_retrieval({"db": ["production.db"], "q": ["x"]})
        handler.api_graph_enhanced_retrieval({"q": ["x"], "graph_depth": ["3"]})
        handler.api_graph_enhanced_retrieval({"q": ["x"], "hit_types": ["candidate"]})
        handler.api_graph_enhanced_retrieval({"q": ["x"], "hit_types": ["graph"]})
        self.assertEqual(4, len(errors))

    def test_cli_smoke_reports_the_current_real_sidecar_state(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TRANSFORMERS_VERBOSITY": "error"}
        proc = subprocess.run(
            [sys.executable, "tools/graph_enhanced_search.py", "unlikely-v64-smoke-token", "--json"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
            env=env,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["readonly"])
        self.assertEqual("unavailable", payload["availability"]["knowledge_sidecar"]["status"])
        validate_retrieval_availability_state(
            "semantic_sidecar", payload["availability"]["semantic_sidecar"],
        )


if __name__ == "__main__":
    unittest.main()
