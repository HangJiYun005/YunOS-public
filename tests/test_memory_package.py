from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr
import hashlib
import io
from http import HTTPStatus
import json
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
    project_retrieval_explanation,
    validate_retrieval_envelope_state,
)
from memory.memory_package import (
    CLASSIFICATION_KEYS,
    MemoryPackageService,
    MemoryPackageValidationError,
    _digest,
    _fixed_canonical_size,
    _identity_payload,
    canonical_json,
    render_memory_package,
    validate_memory_package_source_backed,
    validate_memory_package_standalone,
)
from tools.local_web import AyunOSHandler, REAL_DB_PATH
from tools.memory_package_preview import parse_args
from tools.migrate_semantic_memory import CREATE_MEMORY_EMBEDDINGS_SQL


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FixedRetriever:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def search(self, query: RetrievalQuery) -> dict:
        return deepcopy(self.payload)


class SeededGraphService:
    def __init__(self) -> None:
        self.queries = []

    def query(self, query) -> dict:
        self.queries.append(query)
        seed = query.conversation_id
        target = "c2" if seed == "c1" else "c1"
        suffix = seed.removeprefix("c") or seed
        relation = {
            "relation_id": f"r{suffix}", "relation_class": "formal",
            "relation_type": "semantic_related", "evidence_identity": "evidence_" + "a" * 64,
        }
        return {
            "status": "ok", "reason_code": "query_complete",
            "nodes": [
                {"node_id": f"n-{seed}", "conversation_id": seed},
                {"node_id": f"n-{target}", "conversation_id": target},
            ],
            "relations": {
                "formal": [relation], "deterministic": [],
                "candidates": [], "unavailable": [],
            },
            "paths": [{
                "path_id": f"p-{seed}",
                "node_ids": [f"n-{seed}", f"n-{target}"],
                "relation_ids": [f"r{suffix}"],
            }],
        }


class MemoryPackageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.semantic = self.root / "semantic-missing.db"
        self.knowledge = self.root / "knowledge-missing.db"
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
        conn.executemany("INSERT INTO conversations VALUES(?,?,?,?,?,?)", [
            ("c1", "Alpha plan", 1735689600.0, "2025-01-01T00:00:00+00:00", 1735776000.0, "2025-01-02T00:00:00+00:00"),
            ("c2", "Other plan", 1735862400.0, "2025-01-03T00:00:00+00:00", 1735862400.0, "2025-01-03T00:00:00+00:00"),
        ])
        conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", [
            ("m1", "c1", "Alpha plan", 1, "user", 1735689600.0, "2025-01-01T00:00:00+00:00", "alpha first evidence", "general"),
            ("m2", "c1", "Alpha plan", 2, "assistant", 1735776000.0, "2025-01-02T00:00:00+00:00", "alpha second evidence", "general"),
            ("m3", "c2", "Other plan", 1, "user", 1735862400.0, "2025-01-03T00:00:00+00:00", "unrelated", "general"),
        ])
        conn.commit()
        conn.close()
        self.retriever = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_searcher=lambda *args, **kwargs: {"results": []},
        )
        self.service = MemoryPackageService(self.source, retriever=self.retriever)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, **kwargs) -> dict:
        return self.service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "semantic", "graph"), limit=10, **kwargs,
        ))

    def reseal(self, package: dict, *, evidence: bool = False) -> None:
        if evidence:
            for item in package["evidence"]:
                item["evidence_id"] = "ev_" + _digest({
                    key: value for key, value in item.items() if key != "evidence_id"
                })
            package["classifications"]["unclassified_evidence"] = [
                item["evidence_id"] for item in package["evidence"]
            ]
        package["size"]["evidence_excerpt_chars"] = sum(
            len(item["excerpt"]) for item in package["evidence"]
        )
        package["package_identity"] = "mp_" + _digest(_identity_payload(package))
        package["size"]["canonical_json_chars"] = _fixed_canonical_size(package)

    def graph_package_service(self, *, semantic_results: list[dict] | None = None) -> MemoryPackageService:
        semantic = self.root / "semantic-graph.db"
        sqlite3.connect(semantic).close()
        retriever = GraphEnhancedRetrievalService(
            self.source, semantic, self.knowledge,
            semantic_provider="deterministic_test",
            semantic_searcher=lambda *args, **kwargs: {
                "results": deepcopy(semantic_results or []),
            },
            graph_service=SeededGraphService(),
        )
        return MemoryPackageService(self.source, retriever=retriever)

    def refresh_package_state(self, package: dict) -> None:
        capabilities = package["capabilities"]
        missing = sorted(name for name, state in capabilities.items() if state["status"] == "unavailable")
        package["missing_capabilities"] = missing
        package["degradation"] = {
            "status": "degraded" if missing else "complete",
            "reason_codes": sorted(capabilities[name]["reason_code"] for name in missing),
        }
        package["warnings"] = [{
            "code": f"{name}_unavailable",
            "message": f"{name} capability unavailable: {capabilities[name]['reason_code']}",
        } for name in missing]
        if package["evidence"]:
            package["status"] = "partial" if missing else "ok"
            package["reason_code"] = "partial_capability_result" if missing else "package_complete"
        else:
            package["status"] = "empty"
            package["reason_code"] = "no_evidence"

    def retain_hit_type(self, package: dict, hit_type: str) -> None:
        item = deepcopy(next(item for item in package["evidence"] if item["hit"]["type"] == hit_type))
        item["result_rank"] = 1
        package["evidence"] = [item]
        package["retrieval"].update(result_count=1, total_ranked=1, next_cursor=None)

    def assert_validation_levels_reject(self, package: dict) -> None:
        self.reseal(package, evidence=True)
        with self.assertRaises(MemoryPackageValidationError):
            validate_memory_package_standalone(package)
        with self.assertRaises(MemoryPackageValidationError):
            validate_memory_package_source_backed(package, trusted_source_db=self.source)

    def rewrite_graph_details(self, package: dict, **updates) -> None:
        graph = next(item for item in package["evidence"] if item["hit"]["type"] == "graph")
        graph["details"].update(updates)
        graph["explanation"]["text"] = project_retrieval_explanation(
            "graph", graph["hit"]["score"], graph["details"],
        )

    def test_canonical_json_renderer_and_identity_are_repeatable(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(render_memory_package(first), render_memory_package(second))
        self.assertEqual(first["package_identity"], second["package_identity"])
        self.assertEqual(first["size"]["canonical_json_chars"], len(canonical_json(first)))
        round_trip = json.loads(canonical_json(first))
        validate_memory_package_source_backed(round_trip, trusted_source_db=self.source)
        budgeted = self.service.build(
            RetrievalQuery("alpha", hit_types=("keyword", "semantic", "graph"), limit=10),
            future_token_budget=4096,
        )
        self.assertNotEqual(first["package_identity"], budgeted["package_identity"])
        self.assertEqual(4096, budgeted["size"]["future_token_budget"])
        self.assertFalse(budgeted["size"]["token_selection_applied"])

    def test_evidence_keeps_independent_source_anchor_and_is_unclassified(self):
        package = self.build()
        self.assertEqual(["m2", "m1"], [
            item["source"]["message_anchor"]["message_id"] for item in package["evidence"]
        ])
        self.assertEqual(["c1", "c1"], [
            item["source"]["conversation"]["conversation_id"] for item in package["evidence"]
        ])
        self.assertEqual([], package["classifications"]["facts"])
        self.assertEqual(
            [item["evidence_id"] for item in package["evidence"]],
            package["classifications"]["unclassified_evidence"],
        )
        self.assertTrue(all(item["classification"] == "unclassified_evidence" for item in package["evidence"]))
        self.assertEqual(set(CLASSIFICATION_KEYS), set(package["classifications"]))

    def test_v64_time_and_degradation_states_propagate_exactly(self):
        conn = sqlite3.connect(self.semantic)
        conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
        conn.commit()
        conn.close()
        package = self.build(start_time="2025-01-01", end_time="2025-01-02")
        self.assertEqual("2025-01-01T00:00:00Z", package["request"]["time"]["start_time"])
        self.assertEqual("2025-01-02T23:59:59.999999Z", package["request"]["time"]["end_time"])
        self.assertEqual(
            {"status": "unavailable", "reason_code": "semantic_time_evidence_unprovable"},
            package["retrieval"]["availability"]["semantic_sidecar"],
        )
        self.assertEqual(
            package["retrieval"]["availability"]["semantic_sidecar"],
            package["capabilities"]["semantic"],
        )
        self.assertEqual(
            package["retrieval"]["availability"]["knowledge_sidecar"],
            package["capabilities"]["graph"],
        )
        self.assertEqual("partial", package["status"])
        self.assertEqual("degraded", package["degradation"]["status"])
        self.assertEqual(["graph", "semantic"], package["missing_capabilities"])

    def test_empty_and_source_unavailable_are_explicit(self):
        empty = self.service.build(RetrievalQuery("absent", hit_types=("keyword",)))
        self.assertEqual(("empty", "no_evidence"), (empty["status"], empty["reason_code"]))
        missing = self.root / "missing.db"
        service = MemoryPackageService(
            missing,
            retriever=GraphEnhancedRetrievalService(missing, self.semantic, self.knowledge),
        )
        unavailable = service.build(RetrievalQuery("alpha", hit_types=("keyword",)))
        self.assertEqual("unavailable", unavailable["status"])
        self.assertEqual("source_database_missing", unavailable["reason_code"])
        self.assertEqual([], unavailable["evidence"])

    def test_malformed_package_version_anchor_and_classification_fail_closed(self):
        package = self.build()
        cases = []
        malformed = deepcopy(package)
        malformed["unexpected"] = True
        cases.append(malformed)
        wrong_version = deepcopy(package)
        wrong_version["protocol_version"] = "v7-memory-package-0"
        cases.append(wrong_version)
        forged_anchor = deepcopy(package)
        forged_anchor["evidence"][0]["source"]["message_anchor"]["message_id"] = "m3"
        cases.append(forged_anchor)
        fake_fact = deepcopy(package)
        fake_fact["classifications"]["facts"].append(package["evidence"][0]["evidence_id"])
        cases.append(fake_fact)
        for candidate in cases:
            with self.subTest(candidate=list(candidate)):
                with self.assertRaises(MemoryPackageValidationError):
                    validate_memory_package_source_backed(candidate, trusted_source_db=self.source)

    def test_resealed_invalid_time_budget_retrieval_status_and_reason_fail_closed(self):
        package = self.build()
        cases = []
        invalid_time = deepcopy(package)
        invalid_time["request"]["time"]["start_time"] = "not-iso"
        cases.append(invalid_time)
        reversed_time = deepcopy(package)
        reversed_time["request"]["time"].update(
            start_time="2026-01-02T00:00:00Z", end_time="2026-01-01T00:00:00Z",
        )
        cases.append(reversed_time)
        invalid_mode = deepcopy(package)
        invalid_mode["request"]["scope"]["result_mode"] = "raw"
        cases.append(invalid_mode)
        for name, value in (
            ("context_window", 6), ("limit", 51), ("graph_depth", 3),
            ("relation_budget", 61), ("path_budget", 31),
        ):
            invalid_parameter = deepcopy(package)
            invalid_parameter["request"]["retrieval_parameters"][name] = value
            cases.append(invalid_parameter)
        invalid_budget = deepcopy(package)
        invalid_budget["request"]["retrieval_parameters"]["context_budget"] = 999999
        cases.append(invalid_budget)
        invalid_status = deepcopy(package)
        invalid_status["retrieval"]["status"] = "fabricated"
        cases.append(invalid_status)
        invalid_reason = deepcopy(package)
        invalid_reason["retrieval"]["reason_code"] = "fabricated_reason"
        cases.append(invalid_reason)
        invalid_availability = deepcopy(package)
        invalid_availability["retrieval"]["availability"]["semantic_sidecar"] = {
            "status": "available", "reason_code": "semantic_sidecar_missing",
        }
        invalid_availability["capabilities"]["semantic"] = deepcopy(
            invalid_availability["retrieval"]["availability"]["semantic_sidecar"]
        )
        cases.append(invalid_availability)
        for candidate in cases:
            self.reseal(candidate)
            with self.subTest(candidate=candidate["retrieval"]):
                with self.assertRaises(MemoryPackageValidationError):
                    validate_memory_package_standalone(candidate)

    def test_shared_envelope_state_rejects_impossible_combinations_and_forged_counts(self):
        package = self.build()

        unavailable_with_evidence = deepcopy(package)
        unavailable_with_evidence["retrieval"].update(
            status="unavailable", reason_code="source_database_missing",
        )
        self.reseal(unavailable_with_evidence)
        with self.assertRaisesRegex(MemoryPackageValidationError, "envelope state"):
            validate_memory_package_standalone(unavailable_with_evidence)

        semantic_not_requested = deepcopy(package)
        disabled = {"status": "not_requested", "reason_code": "hit_type_disabled"}
        semantic_not_requested["retrieval"]["availability"]["semantic_sidecar"] = deepcopy(disabled)
        semantic_not_requested["capabilities"]["semantic"] = deepcopy(disabled)
        semantic_not_requested["missing_capabilities"] = ["graph"]
        graph_reason = semantic_not_requested["capabilities"]["graph"]["reason_code"]
        semantic_not_requested["degradation"] = {
            "status": "degraded", "reason_codes": [graph_reason],
        }
        semantic_not_requested["warnings"] = [{
            "code": "graph_unavailable",
            "message": f"graph capability unavailable: {graph_reason}",
        }]
        self.reseal(semantic_not_requested)
        with self.assertRaisesRegex(MemoryPackageValidationError, "envelope state"):
            validate_memory_package_standalone(semantic_not_requested)

        forged_counts = deepcopy(package)
        forged_counts["retrieval"]["result_count"] += 7
        forged_counts["retrieval"]["total_ranked"] += 7
        self.reseal(forged_counts)
        with self.assertRaisesRegex(MemoryPackageValidationError, "envelope state"):
            validate_memory_package_standalone(forged_counts)

    def test_graph_direct_seed_state_matrix_is_shared_and_fail_closed(self):
        keyword_service = self.graph_package_service()
        keyword_package = keyword_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=10,
        ))
        keyword_graph = next(
            item for item in keyword_package["evidence"] if item["hit"]["type"] == "graph"
        )
        self.assertEqual(["keyword"], keyword_graph["details"]["seed_hit_types"])
        self.assertEqual(("c1", "m2"), (
            keyword_graph["details"]["path_start_conversation_id"],
            keyword_graph["details"]["path_start_anchor_message_id"],
        ))
        validate_memory_package_standalone(keyword_package)
        validate_memory_package_source_backed(keyword_package, trusted_source_db=self.source)

        semantic_results = [{
            "rank": 1, "score": 0.91, "source_id": "s1",
            "source_table": "memory_summaries", "memory_space": "general",
            "content_preview": "semantic seed", "summary_type": "conversation_summary",
            "conversation_id": "c1",
        }]
        semantic_service = self.graph_package_service(semantic_results=semantic_results)
        semantic_package = semantic_service.build(RetrievalQuery(
            "semantic-only", hit_types=("semantic", "graph"), limit=10,
        ))
        semantic_graph = next(
            item for item in semantic_package["evidence"] if item["hit"]["type"] == "graph"
        )
        self.assertEqual(["semantic"], semantic_graph["details"]["seed_hit_types"])
        validate_memory_package_standalone(semantic_package)
        validate_memory_package_source_backed(semantic_package, trusted_source_db=self.source)

        joint_service = self.graph_package_service(semantic_results=semantic_results)
        joint_package = joint_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "semantic", "graph"), limit=10,
        ))
        joint_graph = next(
            item for item in joint_package["evidence"] if item["hit"]["type"] == "graph"
        )
        self.assertEqual(["keyword", "semantic"], joint_graph["details"]["seed_hit_types"])
        validate_memory_package_standalone(joint_package)
        validate_memory_package_source_backed(joint_package, trusted_source_db=self.source)

        empty_package = keyword_service.build(RetrievalQuery(
            "definitely absent", hit_types=("keyword", "semantic", "graph"), limit=10,
        ))
        self.assertEqual(("empty", 0, 0, "no_direct_seed"), (
            empty_package["status"], empty_package["retrieval"]["result_count"],
            empty_package["retrieval"]["total_ranked"],
            empty_package["retrieval"]["availability"]["knowledge_sidecar"]["reason_code"],
        ))
        validate_memory_package_standalone(empty_package)
        validate_memory_package_source_backed(empty_package, trusted_source_db=self.source)

        with self.assertRaisesRegex(RetrievalValidationError, "graph_requires_direct_channel"):
            RetrievalQuery("alpha", hit_types=("graph",)).normalized()
        with self.assertRaisesRegex(RetrievalValidationError, "graph_requires_direct_channel"):
            keyword_service.build(RetrievalQuery("alpha", hit_types=("graph",), limit=10))

        graph_only = deepcopy(keyword_package)
        graph_only["request"]["scope"]["hit_types"] = ["graph"]

        no_seed_cases = []
        for source_package, hit_type in (
            (keyword_package, "keyword"),
            (semantic_package, "semantic"),
            (keyword_package, "graph"),
        ):
            candidate = deepcopy(source_package)
            self.retain_hit_type(candidate, hit_type)
            no_seed = {"status": "unavailable", "reason_code": "no_direct_seed"}
            candidate["retrieval"]["availability"]["knowledge_sidecar"] = deepcopy(no_seed)
            candidate["capabilities"]["graph"] = deepcopy(no_seed)
            self.refresh_package_state(candidate)
            no_seed_cases.append((f"no_direct_seed_with_{hit_type}_evidence", candidate))

        disabled = {"status": "not_requested", "reason_code": "hit_type_disabled"}
        no_possible_seed = deepcopy(empty_package)
        no_possible_seed["request"]["scope"]["hit_types"] = ["semantic", "graph"]
        no_possible_seed["retrieval"]["availability"]["semantic_sidecar"] = {
            "status": "unavailable", "reason_code": "semantic_sidecar_missing",
        }
        no_possible_seed["capabilities"]["keyword"] = deepcopy(disabled)
        no_possible_seed["capabilities"]["semantic"] = deepcopy(
            no_possible_seed["retrieval"]["availability"]["semantic_sidecar"]
        )
        no_possible_seed["retrieval"]["availability"]["knowledge_sidecar"] = {
            "status": "available", "reason_code": "graph_query_complete",
        }
        no_possible_seed["capabilities"]["graph"] = deepcopy(
            no_possible_seed["retrieval"]["availability"]["knowledge_sidecar"]
        )
        self.refresh_package_state(no_possible_seed)

        cases = [
            ("graph_only_request", graph_only),
            *no_seed_cases,
            ("graph_available_without_possible_direct_seed", no_possible_seed),
        ]

        subset = deepcopy(joint_package)
        self.rewrite_graph_details(subset, seed_hit_types=["keyword"])
        cases.append(("seed_hit_types_subset", subset))

        superset = deepcopy(joint_package)
        self.rewrite_graph_details(superset, seed_hit_types=["keyword", "semantic", "graph"])
        cases.append(("seed_hit_types_superset", superset))

        cross_conversation = deepcopy(joint_package)
        self.rewrite_graph_details(
            cross_conversation,
            seed_conversation_id="c2", path_start_conversation_id="c2",
        )
        cases.append(("seed_message_cross_conversation", cross_conversation))

        missing_message = deepcopy(joint_package)
        self.rewrite_graph_details(
            missing_message,
            seed_anchor_message_id="m404", path_start_anchor_message_id="m404",
        )
        cases.append(("seed_message_missing", missing_message))

        directless_message = deepcopy(joint_package)
        self.rewrite_graph_details(
            directless_message,
            seed_conversation_id="c2", seed_anchor_message_id="m3",
            path_start_conversation_id="c2", path_start_anchor_message_id="m3",
        )
        cases.append(("seed_message_has_no_direct_evidence", directless_message))
        cases.append(("seed_points_to_graph_only_evidence", deepcopy(directless_message)))

        path_mismatch = deepcopy(joint_package)
        self.rewrite_graph_details(path_mismatch, path_start_anchor_message_id="m1")
        cases.append(("graph_path_start_mismatch", path_mismatch))

        for name, candidate in cases:
            with self.subTest(name=name):
                self.assert_validation_levels_reject(candidate)

        forged_source = deepcopy(joint_package)
        for item in forged_source["evidence"]:
            if (
                item["hit"]["type"] in {"keyword", "semantic"}
                and item["source"]["conversation"]["conversation_id"] == "c1"
                and item["source"]["message_anchor"]["message_id"] == "m2"
            ):
                item["source"]["message_anchor"]["message_id"] = "m404"
                item["source"]["reference_uri"] = "ayunos://conversation/c1?message=m404"
        self.rewrite_graph_details(
            forged_source,
            seed_anchor_message_id="m404", path_start_anchor_message_id="m404",
        )
        self.reseal(forged_source, evidence=True)
        validate_memory_package_standalone(forged_source)
        with self.assertRaises(MemoryPackageValidationError):
            validate_memory_package_source_backed(forged_source, trusted_source_db=self.source)

        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE messages SET text='alpha third seed' WHERE message_id='m3'")
        conn.commit()
        conn.close()
        multi_seed = self.graph_package_service().build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=10,
        ))
        graph_seed_keys = {
            (item["details"]["seed_conversation_id"], item["details"]["seed_anchor_message_id"])
            for item in multi_seed["evidence"] if item["hit"]["type"] == "graph"
        }
        self.assertEqual({("c1", "m2"), ("c2", "m3")}, graph_seed_keys)
        validate_memory_package_standalone(multi_seed)
        validate_memory_package_source_backed(multi_seed, trusted_source_db=self.source)

        degraded = self.service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=10,
        ))
        self.assertTrue(any(item["hit"]["type"] == "keyword" for item in degraded["evidence"]))
        self.assertFalse(any(item["hit"]["type"] == "graph" for item in degraded["evidence"]))
        self.assertEqual(
            "knowledge_sidecar_missing",
            degraded["retrieval"]["availability"]["knowledge_sidecar"]["reason_code"],
        )
        validate_memory_package_standalone(degraded)
        validate_memory_package_source_backed(degraded, trusted_source_db=self.source)

    def test_graph_reason_precedence_is_shared_by_producer_and_both_validators(self):
        semantic_and_knowledge_missing = self.service.build(RetrievalQuery(
            "semantic-only", hit_types=("semantic", "graph"), limit=10,
        ))

        semantic_db = self.root / "semantic-model-unavailable.db"
        sqlite3.connect(semantic_db).close()
        model_graph = SeededGraphService()
        model_unavailable_service = MemoryPackageService(
            self.source,
            retriever=GraphEnhancedRetrievalService(
                self.source, semantic_db, self.knowledge,
                semantic_provider="deterministic_test",
                semantic_searcher=Mock(
                    side_effect=RuntimeError(
                        "Unable to load the configured local embedding model"
                    )
                ),
                graph_service=model_graph,
            ),
        )
        semantic_model_unavailable = model_unavailable_service.build(RetrievalQuery(
            "semantic-only", hit_types=("semantic", "graph"), limit=10,
        ))

        keyword_zero_hits = self.service.build(RetrievalQuery(
            "definitely absent", hit_types=("keyword", "graph"), limit=10,
        ))
        keyword_seed_knowledge_missing = self.service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=10,
        ))
        graph_available_service = self.graph_package_service()
        keyword_seed_graph_available = graph_available_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=10,
        ))

        matrix = (
            (
                "semantic_and_knowledge_missing",
                semantic_and_knowledge_missing,
                "direct_channel_unavailable",
            ),
            (
                "semantic_model_unavailable",
                semantic_model_unavailable,
                "direct_channel_unavailable",
            ),
            ("keyword_zero_hits", keyword_zero_hits, "no_direct_seed"),
            (
                "keyword_seed_knowledge_missing",
                keyword_seed_knowledge_missing,
                "knowledge_sidecar_missing",
            ),
            (
                "keyword_seed_graph_available",
                keyword_seed_graph_available,
                "graph_query_complete",
            ),
        )
        for name, package, expected_reason in matrix:
            with self.subTest(name=name):
                self.assertEqual(
                    expected_reason,
                    package["retrieval"]["availability"]["knowledge_sidecar"]["reason_code"],
                )
                validate_memory_package_standalone(package)
                validate_memory_package_source_backed(
                    package, trusted_source_db=self.source,
                )

        self.assertEqual((0, 0, 0), (
            semantic_and_knowledge_missing["retrieval"]["result_count"],
            semantic_and_knowledge_missing["retrieval"]["total_ranked"],
            len(semantic_and_knowledge_missing["evidence"]),
        ))
        self.assertEqual([], model_graph.queries)
        self.assertEqual((0, 0, 0), (
            keyword_zero_hits["retrieval"]["result_count"],
            keyword_zero_hits["retrieval"]["total_ranked"],
            len(keyword_zero_hits["evidence"]),
        ))
        self.assertTrue(graph_available_service.retriever.graph_service.queries)

        for name, package in (
            ("unexecutable_direct_as_knowledge_missing", semantic_and_knowledge_missing),
            ("zero_seed_as_knowledge_missing", keyword_zero_hits),
        ):
            candidate = deepcopy(package)
            wrong = {"status": "unavailable", "reason_code": "knowledge_sidecar_missing"}
            candidate["retrieval"]["availability"]["knowledge_sidecar"] = deepcopy(wrong)
            candidate["capabilities"]["graph"] = deepcopy(wrong)
            self.refresh_package_state(candidate)
            with self.subTest(name=name):
                self.assert_validation_levels_reject(candidate)

        for wrong_reason in ("direct_channel_unavailable", "no_direct_seed"):
            candidate = deepcopy(keyword_seed_knowledge_missing)
            wrong = {"status": "unavailable", "reason_code": wrong_reason}
            candidate["retrieval"]["availability"]["knowledge_sidecar"] = deepcopy(wrong)
            candidate["capabilities"]["graph"] = deepcopy(wrong)
            self.refresh_package_state(candidate)
            with self.subTest(name=f"legal_seed_as_{wrong_reason}"):
                self.assert_validation_levels_reject(candidate)

    def test_result_count_is_current_page_and_total_ranked_is_full_query_set(self):
        first = self.service.build(RetrievalQuery("alpha", hit_types=("keyword",), limit=1))
        self.assertEqual((1, 2, [1]), (
            first["retrieval"]["result_count"], first["retrieval"]["total_ranked"],
            sorted({item["result_rank"] for item in first["evidence"]}),
        ))
        self.assertIsNotNone(first["retrieval"]["next_cursor"])

        second = self.service.build(RetrievalQuery(
            "alpha", hit_types=("keyword",), limit=1,
            cursor=first["retrieval"]["next_cursor"],
        ))
        self.assertEqual((1, 2, [2], None), (
            second["retrieval"]["result_count"], second["retrieval"]["total_ranked"],
            sorted({item["result_rank"] for item in second["evidence"]}),
            second["retrieval"]["next_cursor"],
        ))

    def test_v64_producer_and_v7_validator_call_the_same_envelope_state_machine(self):
        with patch(
            "memory.graph_enhanced_retrieval.validate_retrieval_envelope_state",
            wraps=validate_retrieval_envelope_state,
        ) as v64_call, patch(
            "memory.memory_package.validate_retrieval_envelope_state",
            wraps=validate_retrieval_envelope_state,
        ) as v7_call:
            self.build()
        self.assertGreaterEqual(v64_call.call_count, 1)
        self.assertGreaterEqual(v7_call.call_count, 2)

    def test_uri_duplicate_extra_missing_and_classification_reference_fail_closed(self):
        package = self.build()
        invalid_uri = deepcopy(package)
        invalid_uri["evidence"][0]["source"]["reference_uri"] = "ayunos://wrong"
        duplicate = deepcopy(package)
        duplicate["evidence"].append(deepcopy(duplicate["evidence"][0]))
        duplicate["classifications"]["unclassified_evidence"].append(
            duplicate["evidence"][0]["evidence_id"]
        )
        dangerous_extra = deepcopy(package)
        dangerous_extra["evidence"][0]["raw_metadata"] = {"path": "hidden"}
        missing = deepcopy(package)
        missing["evidence"][0].pop("explanation")
        bad_reference = deepcopy(package)
        bad_reference["classifications"]["unclassified_evidence"] = ["ev_missing"]
        promoted = deepcopy(package)
        promoted["classifications"]["facts"] = [package["evidence"][0]["evidence_id"]]
        for candidate in (invalid_uri, duplicate, dangerous_extra, missing, bad_reference, promoted):
            with self.assertRaises(MemoryPackageValidationError):
                validate_memory_package_standalone(candidate)

    def test_absolute_paths_are_deterministically_redacted_before_identity(self):
        windows = "C:\\Program Files\\(T)\\a b.txt"
        unc = "\\\\srv\\Shared Folder\\(T)\\c d.txt"
        posix = "/home/private folder/(t)/e f.txt"
        file_uri = "file:///var/private folder/(t)/g h.txt"
        raw = (
            f'win="{windows}", unc=({unc}); '
            f'posix=<{posix}>; uri=\'{file_uri}\'.'
        )
        conn = sqlite3.connect(self.source)
        conn.execute(
            "UPDATE messages SET conversation_title=?,text=? WHERE message_id='m1'",
            ("path title " + raw, "pathprobe " + raw),
        )
        conn.commit()
        conn.close()
        first = self.service.build(RetrievalQuery("pathprobe", hit_types=("keyword",), limit=5))
        second = self.service.build(RetrievalQuery("pathprobe", hit_types=("keyword",), limit=5))
        json_text = canonical_json(first)
        rendered = render_memory_package(first)
        self.assertEqual(first["package_identity"], second["package_identity"])
        self.assertTrue(first["privacy"]["absolute_path_redaction_applied"])
        self.assertGreaterEqual(first["privacy"]["redaction_count"], 8)
        self.assertIn("[absolute-path]", json_text)
        self.assertIn("absolute_path_redaction_applied=true", rendered)
        self.assertIn('win=\\"[absolute-path]\\", unc=([absolute-path]);', json_text)
        self.assertIn("posix=<[absolute-path]>; uri='[absolute-path]'.", rendered)
        for secret in (windows, unc, posix, file_uri):
            self.assertNotIn(secret, json_text)
            self.assertNotIn(secret, rendered)
        for leaked_suffix in (
            "Files\\(T)", "Folder\\(T)", "folder/(t)", "a b.txt", "g h.txt",
        ):
            self.assertNotIn(leaked_suffix, json_text)
            self.assertNotIn(leaked_suffix, rendered)
        validate_memory_package_source_backed(first, trusted_source_db=self.source)
        bad_count = deepcopy(first)
        bad_count["privacy"]["redaction_count"] += 1
        self.reseal(bad_count)
        with self.assertRaisesRegex(MemoryPackageValidationError, "redaction count"):
            validate_memory_package_standalone(bad_count)
        forged = deepcopy(first)
        forged["evidence"][0]["excerpt"] = "leak C:\\Private\\again.txt"
        self.reseal(forged, evidence=True)
        with self.assertRaises(MemoryPackageValidationError):
            validate_memory_package_source_backed(forged, trusted_source_db=self.source)
        with self.assertRaisesRegex(MemoryPackageValidationError, "privacy contract"):
            self.service.build(RetrievalQuery("C:\\Private\\query.txt", hit_types=("keyword",)))

    def test_standalone_accepts_self_consistent_repackage_but_source_backed_rebuild_rejects_it(self):
        conn = sqlite3.connect(self.source)
        conn.execute(
            "UPDATE messages SET conversation_title='Clean title',text=? WHERE message_id='m1'",
            ("singlepath C:\\Private Folder\\secret.txt",),
        )
        conn.commit()
        conn.close()
        package = self.service.build(RetrievalQuery("singlepath", hit_types=("keyword",), limit=5))
        self.assertEqual(1, package["privacy"]["redaction_count"])

        repackaged = deepcopy(package)
        repackaged["evidence"][0]["excerpt"] = repackaged["evidence"][0]["excerpt"].replace(
            "[absolute-path]", "[removed]",
        )
        repackaged["privacy"] = {
            "absolute_path_redaction_applied": False,
            "redaction_count": 0,
            "replacement": "[absolute-path]",
        }
        self.reseal(repackaged, evidence=True)

        validate_memory_package_standalone(repackaged)
        with self.assertRaisesRegex(MemoryPackageValidationError, "excerpt projection"):
            validate_memory_package_source_backed(
                repackaged, trusted_source_db=self.source,
            )

    def test_forged_v64_anchor_and_evidence_mismatch_fail_before_return(self):
        retrieval = self.retriever.search(RetrievalQuery("alpha", hit_types=("keyword",), limit=10))
        forged = deepcopy(retrieval)
        forged["results"][0]["evidence"][0]["source"]["conversation_id"] = "c2"
        with self.assertRaisesRegex(MemoryPackageValidationError, "does not belong"):
            MemoryPackageService(self.source, retriever=FixedRetriever(forged)).build(
                RetrievalQuery("alpha", hit_types=("keyword",), limit=10)
            )
        mismatch = deepcopy(retrieval)
        mismatch["results"][0]["evidence"][0]["source"]["anchor_message_id"] = "m1"
        with self.assertRaisesRegex(MemoryPackageValidationError, "fields mismatch"):
            MemoryPackageService(self.source, retriever=FixedRetriever(mismatch)).build(
                RetrievalQuery("alpha", hit_types=("keyword",), limit=10)
            )
        unrequested = deepcopy(retrieval)
        unrequested_evidence = unrequested["results"][0]["evidence"][0]
        unrequested_evidence["hit_type"] = "semantic"
        unrequested_evidence["score"] = 0.5
        unrequested_evidence["reason"] = "injected semantic provenance"
        unrequested_evidence.pop("terms", None)
        unrequested["results"][0]["hit_types"] = ["semantic"]
        with self.assertRaisesRegex(MemoryPackageValidationError, "not request-bound"):
            MemoryPackageService(self.source, retriever=FixedRetriever(unrequested)).build(
                RetrievalQuery("alpha", hit_types=("keyword",), limit=10)
            )

    def test_core_does_not_change_source_and_output_excludes_private_payloads(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        package = self.build(context_window=2)
        after = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        encoded = canonical_json(package).lower()
        for forbidden in ("embedding", "metadata", "prompt", str(self.source).lower(), '"context"'):
            self.assertNotIn(forbidden, encoded)
        self.assertFalse(self.semantic.exists())
        self.assertFalse(self.knowledge.exists())

    def test_cli_and_api_expose_no_database_path_and_api_reuses_core(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["alpha", "--db", "other.db"])
        handler = object.__new__(AyunOSHandler)
        sent = []
        handler.send_json = lambda payload, status=None: sent.append((payload, status))
        handler.send_error_json = lambda message, status=None: self.fail(message)
        expected = self.service.build(RetrievalQuery("alpha", hit_types=("keyword",), limit=2))
        handler.app = Mock(readonly=False, db_mode="test", db_path=self.source)
        handler.api_memory_package_preview({"q": ["alpha"]})
        unavailable, status = sent.pop()
        self.assertEqual(("production_readonly_required", HTTPStatus.SERVICE_UNAVAILABLE), (
            unavailable["reason_code"], status,
        ))
        handler.app = Mock(readonly=True, db_mode="real", db_path=self.source)
        handler.api_memory_package_preview({"q": ["alpha"]})
        unavailable, status = sent.pop()
        self.assertEqual(("production_readonly_required", HTTPStatus.SERVICE_UNAVAILABLE), (
            unavailable["reason_code"], status,
        ))
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH.resolve())
        with patch("tools.local_web.build_production_memory_package", return_value=expected) as build:
            handler.api_memory_package_preview({"q": ["alpha"], "hit_types": ["keyword"], "limit": ["2"]})
        self.assertEqual((expected, None), sent[0])
        request = build.call_args.args[0]
        self.assertEqual(("alpha", ("keyword",), 2), (request.query, request.hit_types, request.limit))
        text_payloads = []
        handler.send_text = lambda value, status=None: text_payloads.append(value)
        with patch("tools.local_web.build_production_memory_package", return_value=expected):
            handler.api_memory_package_preview({
                "q": ["alpha"], "hit_types": ["keyword"], "limit": ["2"], "format": ["text"],
            })
        self.assertIn(expected["package_identity"], text_payloads[0])
        errors = []
        handler.send_error_json = lambda message, status=None: errors.append(message)
        handler.api_memory_package_preview({"q": ["alpha"], "db": ["other.db"]})
        self.assertEqual(["Unsupported memory package parameter"], errors)
        with patch(
            "tools.local_web.build_production_memory_package",
            side_effect=lambda request, **kwargs: request.normalized(),
        ):
            handler.api_memory_package_preview({"q": ["alpha"], "hit_types": ["graph"]})
        self.assertEqual(
            ["Unsupported memory package parameter", "Invalid memory package query"], errors,
        )

    def test_cli_json_and_text_are_stable_and_readonly(self):
        command = [sys.executable, "tools/memory_package_preview.py", "unlikely-v7-cli-token", "--hit-types", "keyword"]
        first = subprocess.run(command + ["--json"], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        second = subprocess.run(command + ["--json"], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        text = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual((0, 0, 0), (first.returncode, second.returncode, text.returncode))
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["readonly"])
        self.assertFalse(payload["persisted"])
        self.assertIn(payload["package_identity"], text.stdout)
        graph_only = subprocess.run(
            [sys.executable, "tools/memory_package_preview.py", "alpha", "--hit-types", "graph", "--json"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(2, graph_only.returncode)
        invalid = json.loads(graph_only.stdout)
        self.assertEqual("request_or_package_validation_failed", invalid["reason_code"])
        self.assertEqual("graph_requires_direct_channel", invalid["error"])


if __name__ == "__main__":
    unittest.main()
