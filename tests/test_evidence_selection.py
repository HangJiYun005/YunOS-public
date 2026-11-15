from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
from http import HTTPStatus
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.evidence_selection import (
    EvidenceSelectionService,
    EvidenceSelectionValidationError,
    _candidate_identity,
    _digest,
    _duplicate_equivalence_key,
    _excerpt_commitment,
    _identity_payload,
    _materialize_payload,
    _render_candidate,
    _v70_evidence_identity,
    build_selection_from_memory_package,
    canonical_selection_json,
    normalize_policy,
    render_evidence_selection,
    validate_evidence_selection_source_backed,
    validate_evidence_selection_standalone,
)
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import (
    MemoryPackageService,
    _digest as memory_package_digest,
    _fixed_canonical_size as fixed_memory_package_size,
    _identity_payload as memory_package_identity_payload,
    validate_memory_package_standalone,
)
from tools.evidence_selection_preview import main as preview_main, parse_args
from tools.local_web import AyunOSHandler, REAL_DB_PATH


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SeededGraphService:
    def query(self, query) -> dict:
        seed = query.conversation_id
        target = "c2" if seed == "c1" else "c1"
        return {
            "status": "ok", "reason_code": "query_complete",
            "nodes": [
                {"node_id": f"n-{seed}", "conversation_id": seed},
                {"node_id": f"n-{target}", "conversation_id": target},
            ],
            "relations": {"formal": [{
                "relation_id": f"r-{seed}", "relation_class": "formal",
                "relation_type": "semantic_related", "evidence_identity": "evidence_" + "a" * 64,
            }], "deterministic": [], "candidates": [], "unavailable": []},
            "paths": [{
                "path_id": f"p-{seed}", "node_ids": [f"n-{seed}", f"n-{target}"],
                "relation_ids": [f"r-{seed}"],
            }],
        }


class EvidenceSelectionTest(unittest.TestCase):
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
            ("c1", "Alpha plan", 1.0, "2025-01-01T00:00:00+00:00", 20.0, "2025-01-20T00:00:00+00:00"),
            ("c2", "Other plan", 2.0, "2025-02-01T00:00:00+00:00", 20.0, "2025-02-20T00:00:00+00:00"),
            ("c3", "Third plan", 3.0, "2025-03-01T00:00:00+00:00", 20.0, "2025-03-20T00:00:00+00:00"),
        ])
        rows = []
        stamp = 1735689600.0
        for conv in ("c1", "c2", "c3"):
            for index in range(1, 7):
                rows.append((
                    f"{conv}-m{index}", conv, f"{conv} title", index,
                    "user" if index % 2 else "assistant", stamp + len(rows),
                    f"2025-01-{len(rows) + 1:02d}T00:00:00+00:00",
                    f"alpha evidence unique {conv} {index} " + ("x" * (40 + index)), "general",
                ))
        conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        self.retriever = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_searcher=lambda *args, **kwargs: {"results": []},
        )
        self.memory_service = MemoryPackageService(self.source, retriever=self.retriever)
        self.service = EvidenceSelectionService(
            self.source, memory_package_service=self.memory_service,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def query(self, **kwargs) -> RetrievalQuery:
        return RetrievalQuery("alpha", hit_types=("keyword",), limit=50, **kwargs)

    def reseal_identity(self, package: dict) -> None:
        package["package_identity"] = "msp_" + _digest(_identity_payload(package))

    def reseal_source_identity(self, package: dict) -> None:
        package["package_identity"] = "mp_" + memory_package_digest(
            memory_package_identity_payload(package)
        )
        package["size"]["canonical_json_chars"] = fixed_memory_package_size(package)
        validate_memory_package_standalone(package)

    def graph_source_package(self) -> dict:
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE messages SET text=replace(text, 'alpha', 'other') WHERE conversation_id='c2'")
        conn.commit()
        conn.close()
        sqlite3.connect(self.knowledge).close()
        graph_memory_service = MemoryPackageService(
            self.source,
            retriever=GraphEnhancedRetrievalService(
                self.source, self.semantic, self.knowledge,
                semantic_searcher=lambda *args, **kwargs: {"results": []},
                graph_service=SeededGraphService(),
            ),
        )
        return graph_memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=50,
        ))

    def build_graph_selection(self, **policy_values) -> tuple[dict, dict]:
        source_package = self.graph_source_package()
        package = build_selection_from_memory_package(
            source_package,
            policy=normalize_policy("custom", **policy_values),
        )
        return source_package, package

    def forge_selected_excerpt(self, package: dict, excerpt: str) -> dict:
        forged = deepcopy(package)
        self.assertEqual((1, 1), (len(forged["candidate_ledger"]), len(forged["selected"])))
        ledger = forged["candidate_ledger"][0]
        selected = forged["selected"][0]
        ledger["excerpt_commitment"] = _excerpt_commitment(excerpt)
        for hit in ledger["hit_provenance"]:
            hit["evidence_id"] = _v70_evidence_identity(ledger, hit, excerpt)
        ledger["evidence_ids"] = [hit["evidence_id"] for hit in ledger["hit_provenance"]]
        forged["source_projection"]["evidence_order"] = list(ledger["evidence_ids"])
        ledger["candidate_id"] = _candidate_identity(ledger)
        ledger["duplicate_equivalence"]["key"] = _duplicate_equivalence_key(ledger)
        ledger["duplicate_equivalence"]["representative_candidate_id"] = ledger["candidate_id"]
        candidate = {
            "candidate_id": ledger["candidate_id"], "source": ledger["source"],
            "excerpt": excerpt, "hit_provenance": ledger["hit_provenance"],
            "duplicate_sources": [],
        }
        chars = len(_render_candidate(candidate))
        ledger["render_cost"] = {
            "payload_chars": chars, "estimated_tokens": math.ceil(chars / 4),
        }
        selected.update({
            "candidate_id": ledger["candidate_id"], "source": deepcopy(ledger["source"]),
            "excerpt": excerpt, "evidence_ids": deepcopy(ledger["evidence_ids"]),
            "hit_provenance": deepcopy(ledger["hit_provenance"]), "duplicate_sources": [],
            "priority": deepcopy(ledger["priority"]), "payload_chars": chars,
            "estimated_tokens": math.ceil(chars / 4),
        })
        forged["model_payload"] = _materialize_payload(forged["selected"])
        used = len(forged["model_payload"])
        estimated = math.ceil(used / 4)
        forged["budget"].update({
            "used_characters": used,
            "remaining_characters": forged["budget"]["character_budget"] - used,
            "estimated_tokens_used": estimated,
            "estimated_tokens_remaining": forged["budget"]["estimated_token_budget"] - estimated,
        })
        self.reseal_identity(forged)
        return forged

    def test_same_anchor_hits_merge_without_losing_hit_provenance(self):
        sqlite3.connect(self.semantic).close()
        semantic_retriever = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_provider="deterministic_test",
            semantic_searcher=lambda *args, **kwargs: {"results": [{
                "rank": 1, "score": 0.91, "source_id": "s1",
                "source_table": "memory_summaries", "memory_space": "general",
                "content_preview": "semantic seed", "summary_type": "conversation_summary",
                "conversation_id": "c1",
            }]},
        )
        memory_service = MemoryPackageService(self.source, retriever=semantic_retriever)
        source_package = memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "semantic"), limit=50,
        ))
        package = build_selection_from_memory_package(source_package, policy=normalize_policy("large"))
        merged = [item for item in package["selected"] if {
            hit["hit_type"] for hit in item["hit_provenance"]
        } == {"keyword", "semantic"}]
        self.assertTrue(merged)
        item = merged[0]
        self.assertEqual("c1", item["source"]["conversation"]["conversation_id"])
        self.assertEqual(2, len(item["hit_provenance"]))
        self.assertEqual(1, package["model_payload"].count(f"[{item['candidate_id']}]"))
        validate_evidence_selection_source_backed(
            package, trusted_source_package=source_package, trusted_source_db=self.source,
        )

    def test_exact_duplicate_is_stable_and_preserves_all_anchors(self):
        conn = sqlite3.connect(self.source)
        duplicate_text = "alpha exact duplicate evidence"
        conn.execute("UPDATE messages SET text=? WHERE message_id IN ('c1-m6','c2-m6')", (duplicate_text,))
        conn.commit()
        conn.close()
        first = self.service.build(self.query(), budget_profile="large")
        second = self.service.build(self.query(), budget_profile="large")
        self.assertEqual(first, second)
        duplicate = next(item for item in first["omitted"] if item["reason"] == "duplicate")
        representative = next(item for item in first["selected"] if item["candidate_id"] == duplicate["duplicate_of"])
        self.assertEqual(1, len(representative["duplicate_sources"]))
        self.assertEqual(1, len(representative["hit_provenance"]))
        self.assertEqual(1, len(representative["duplicate_sources"][0]["hit_provenance"]))
        self.assertEqual(1, first["model_payload"].count(duplicate_text))
        self.assertNotIn("excerpt", duplicate)
        self.assertTrue(all(
            set(hit) == {
                "evidence_id", "result_rank", "hit_type", "score", "basis_code",
                "explanation", "details",
            }
            for hit in duplicate["hit_provenance"]
        ))

    def test_named_budgets_are_hard_monotonic_and_reproducible(self):
        packages = {
            name: self.service.build(self.query(), budget_profile=name)
            for name in ("small", "medium", "large")
        }
        counts = []
        for name, package in packages.items():
            with self.subTest(name=name):
                self.assertLessEqual(package["budget"]["used_characters"], package["budget"]["character_budget"])
                self.assertLessEqual(package["budget"]["estimated_tokens_used"], package["budget"]["estimated_token_budget"])
                self.assertEqual(package, self.service.build(self.query(), budget_profile=name))
                counts.append(package["counts"]["selected"])
        self.assertEqual(counts, sorted(counts))

    def test_custom_budget_is_bounded_and_character_limit_is_absolute(self):
        package = self.service.build(
            self.query(), budget_profile="custom", character_budget=700,
            estimated_token_budget=400, per_conversation_cap=3, max_selected=10,
        )
        self.assertEqual(len(package["model_payload"]), package["budget"]["used_characters"])
        self.assertLessEqual(len(package["model_payload"]), 700)
        invalid = (
            ("custom", dict(character_budget=255, estimated_token_budget=400, per_conversation_cap=3, max_selected=10)),
            ("custom", dict(character_budget=700, estimated_token_budget=63, per_conversation_cap=3, max_selected=10)),
            ("small", dict(character_budget=700)),
        )
        for name, kwargs in invalid:
            with self.assertRaises(EvidenceSelectionValidationError):
                normalize_policy(name, **kwargs)

    def test_conversation_cap_and_round_robin_preserve_diversity(self):
        package = self.service.build(
            self.query(),
            budget_profile="custom", character_budget=20_000,
            estimated_token_budget=8_000, per_conversation_cap=2, max_selected=50,
        )
        counts = {}
        for item in package["selected"]:
            conversation_id = item["source"]["conversation"]["conversation_id"]
            counts[conversation_id] = counts.get(conversation_id, 0) + 1
        self.assertGreaterEqual(len(counts), 2)
        self.assertLessEqual(max(counts.values()), 2)
        self.assertTrue(any(item["reason"] == "conversation_cap" for item in package["omitted"]))

    def test_graph_only_never_displaces_direct_evidence(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE messages SET text=replace(text, 'alpha', 'other') WHERE conversation_id='c2'")
        conn.commit()
        conn.close()
        sqlite3.connect(self.knowledge).close()
        graph_memory_service = MemoryPackageService(
            self.source,
            retriever=GraphEnhancedRetrievalService(
                self.source, self.semantic, self.knowledge,
                semantic_searcher=lambda *args, **kwargs: {"results": []},
                graph_service=SeededGraphService(),
            ),
        )
        source_package = graph_memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "graph"), limit=50,
        ))
        package = build_selection_from_memory_package(
            source_package,
            policy=normalize_policy(
                "custom", character_budget=700, estimated_token_budget=400,
                per_conversation_cap=2, max_selected=10,
            ),
        )
        priorities = [item["priority"]["class"] for item in package["selected"]]
        self.assertNotIn("graph_only", priorities)
        self.assertTrue(any(
            item["priority"]["class"] == "graph_only" and item["reason"] in {"lower_priority", "budget_exceeded"}
            for item in package["omitted"]
        ))
        graph_omitted = next(
            item for item in package["omitted"] if item["priority"]["class"] == "graph_only"
        )
        self.assertTrue(any("seed_hit_types" in hit["details"] for hit in graph_omitted["hit_provenance"]))

    def test_direct_conversation_cap_makes_tier_incomplete_and_blocks_graph_fill(self):
        _, package = self.build_graph_selection(
            character_budget=20_000, estimated_token_budget=10_000,
            per_conversation_cap=1, max_selected=100,
        )
        self.assertTrue(any(
            item["priority"]["class"] == "direct" and item["reason"] == "conversation_cap"
            for item in package["omitted"]
        ))
        self.assertFalse(any(item["priority"]["class"] == "graph_only" for item in package["selected"]))
        self.assertTrue(all(
            item["reason"] == "lower_priority"
            for item in package["omitted"] if item["priority"]["class"] == "graph_only"
        ))

    def test_direct_max_items_makes_tier_incomplete_and_blocks_graph_fill(self):
        _, package = self.build_graph_selection(
            character_budget=20_000, estimated_token_budget=10_000,
            per_conversation_cap=20, max_selected=2,
        )
        self.assertTrue(any(
            item["priority"]["class"] == "direct" and item["reason"] == "lower_priority"
            for item in package["omitted"]
        ))
        self.assertFalse(any(item["priority"]["class"] == "graph_only" for item in package["selected"]))
        self.assertTrue(all(
            item["reason"] == "lower_priority"
            for item in package["omitted"] if item["priority"]["class"] == "graph_only"
        ))

    def test_direct_budget_exceeded_makes_tier_incomplete_and_blocks_graph_fill(self):
        _, package = self.build_graph_selection(
            character_budget=600, estimated_token_budget=1_000,
            per_conversation_cap=20, max_selected=100,
        )
        self.assertTrue(any(
            item["priority"]["class"] == "direct" and item["reason"] == "budget_exceeded"
            for item in package["omitted"]
        ))
        self.assertFalse(any(item["priority"]["class"] == "graph_only" for item in package["selected"]))
        self.assertTrue(all(
            item["reason"] == "lower_priority"
            for item in package["omitted"] if item["priority"]["class"] == "graph_only"
        ))

    def test_graph_can_use_remainder_only_after_every_direct_candidate_is_selected(self):
        _, package = self.build_graph_selection(
            character_budget=20_000, estimated_token_budget=10_000,
            per_conversation_cap=20, max_selected=100,
        )
        self.assertFalse(any(
            item["priority"]["class"] == "direct" for item in package["omitted"]
        ))
        self.assertTrue(any(
            item["priority"]["class"] == "graph_only" for item in package["selected"]
        ))

    def test_validator_recomputes_selection_omissions_counts_and_budget_after_rehash(self):
        original = self.service.build(self.query(), budget_profile="small")
        mutations = []
        removed = deepcopy(original)
        removed["selected"] = removed["selected"][1:]
        mutations.append(removed)
        omitted = deepcopy(original)
        omitted["omitted"][0]["reason"] = "lower_priority"
        mutations.append(omitted)
        counted = deepcopy(original)
        counted["counts"]["selected"] += 1
        mutations.append(counted)
        budgeted = deepcopy(original)
        budgeted["budget"]["used_characters"] += 1
        mutations.append(budgeted)
        for candidate in mutations:
            self.reseal_identity(candidate)
            with self.assertRaises(EvidenceSelectionValidationError):
                validate_evidence_selection_standalone(candidate)

    def test_ledger_rejects_coordinated_omitted_anchor_reason_and_cost_reseal(self):
        original = self.service.build(
            self.query(), budget_profile="custom", character_budget=20_000,
            estimated_token_budget=10_000, per_conversation_cap=1, max_selected=100,
        )
        forged = deepcopy(original)
        omitted = next(item for item in forged["omitted"] if item["reason"] == "conversation_cap")
        omitted["source_anchor"].update({
            "conversation_id": "forged-conversation", "message_id": "forged-message",
            "reference_uri": "ayunos://conversation/forged-conversation?message=forged-message",
        })
        omitted["reason"] = "budget_exceeded"
        omitted["payload_chars"] += 4
        omitted["estimated_tokens"] = math.ceil(omitted["payload_chars"] / 4)
        forged["counts"]["omission_reasons"]["conversation_cap"] -= 1
        forged["counts"]["omission_reasons"]["budget_exceeded"] += 1
        self.reseal_identity(forged)
        with self.assertRaises(EvidenceSelectionValidationError):
            validate_evidence_selection_standalone(forged)

    def test_selected_path_reseal_is_rejected_after_all_public_identities_change(self):
        source_package = self.memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword",), limit=1,
        ))
        original = build_selection_from_memory_package(
            source_package,
            policy=normalize_policy(
                "custom", character_budget=5_000, estimated_token_budget=2_000,
                per_conversation_cap=2, max_selected=2,
            ),
        )
        for path in (
            r"alpha C:\Users\ExampleUser\raw.txt",
            r"alpha \\server\share\raw.txt",
            "alpha /home/secret/raw.txt",
            "alpha file:///C:/Users/ExampleUser/raw.txt",
        ):
            with self.subTest(path=path):
                forged = self.forge_selected_excerpt(original, path)
                with self.assertRaises(EvidenceSelectionValidationError) as raised:
                    validate_evidence_selection_standalone(forged)
                self.assertIn("v7.0 privacy boundary", str(raised.exception))
                self.assertNotIn(path, str(raised.exception))

    def test_standalone_rejects_internally_inconsistent_capability_projection(self):
        forged = self.service.build(
            RetrievalQuery("alpha", hit_types=("keyword", "semantic"), limit=20),
            budget_profile="medium",
        )
        self.assertEqual("partial", forged["source_projection"]["status"])
        available = {"status": "available", "reason_code": "semantic_query_complete"}
        forged["source_projection"].update({
            "status": "ok", "reason_code": "package_complete",
            "capabilities": {**forged["source_projection"]["capabilities"], "semantic": available},
            "degradation": {"status": "complete", "reason_codes": []},
            "warnings": [], "missing_capabilities": [],
        })
        forged.update({
            "status": "ok", "reason_code": "selection_complete",
            "capabilities": deepcopy(forged["source_projection"]["capabilities"]),
            "degradation": {"status": "complete", "reason_codes": []},
            "warnings": [], "missing_capabilities": [],
        })
        self.reseal_identity(forged)
        with self.assertRaises(EvidenceSelectionValidationError):
            validate_evidence_selection_standalone(forged)

    def test_standalone_accepts_fully_coordinated_body_repackage_but_source_backed_rejects(self):
        source_package = self.memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword",), limit=1,
        ))
        original = build_selection_from_memory_package(
            source_package,
            policy=normalize_policy(
                "custom", character_budget=5_000, estimated_token_budget=2_000,
                per_conversation_cap=2, max_selected=2,
            ),
        )
        excerpt = "alpha fully coordinated harmless replacement"
        repackaged = self.forge_selected_excerpt(original, excerpt)
        repackaged_source = deepcopy(source_package)
        repackaged_source["evidence"][0]["excerpt"] = excerpt
        repackaged_source["evidence"][0]["evidence_id"] = (
            repackaged["candidate_ledger"][0]["evidence_ids"][0]
        )
        repackaged_source["classifications"]["unclassified_evidence"] = [
            repackaged_source["evidence"][0]["evidence_id"]
        ]
        repackaged_source["size"]["evidence_excerpt_chars"] = len(excerpt)
        self.reseal_source_identity(repackaged_source)
        repackaged["source_projection"]["package_identity"] = repackaged_source["package_identity"]
        self.reseal_identity(repackaged)

        validate_evidence_selection_standalone(repackaged)
        with self.assertRaises(EvidenceSelectionValidationError):
            validate_evidence_selection_source_backed(
                repackaged,
                trusted_source_package=source_package,
                trusted_source_db=self.source,
            )

    def test_standalone_accepts_fully_coordinated_source_state_repackage_but_source_backed_rejects(self):
        source_package = self.memory_service.build(RetrievalQuery(
            "alpha", hit_types=("keyword", "semantic"), limit=20,
        ))
        self.assertEqual(("partial", "degraded"), (
            source_package["status"], source_package["degradation"]["status"],
        ))
        repackaged_source = deepcopy(source_package)
        available = {"status": "available", "reason_code": "semantic_query_complete"}
        repackaged_source["retrieval"]["availability"]["semantic_sidecar"] = deepcopy(available)
        repackaged_source["capabilities"]["semantic"] = deepcopy(available)
        repackaged_source["missing_capabilities"] = []
        repackaged_source["degradation"] = {"status": "complete", "reason_codes": []}
        repackaged_source["warnings"] = []
        repackaged_source["status"] = "ok"
        repackaged_source["reason_code"] = "package_complete"
        self.reseal_source_identity(repackaged_source)
        repackaged = build_selection_from_memory_package(
            repackaged_source, policy=normalize_policy("medium"),
        )

        validate_evidence_selection_standalone(repackaged)
        self.assertEqual(("ok", "complete"), (
            repackaged["status"], repackaged["degradation"]["status"],
        ))
        with self.assertRaises(EvidenceSelectionValidationError):
            validate_evidence_selection_source_backed(
                repackaged,
                trusted_source_package=source_package,
                trusted_source_db=self.source,
            )

    def test_unicode_nfc_duplicate_equivalence_and_render_cost_recompute_from_ledger(self):
        decomposed = "alpha Cafe\u0301\r\nline 😀 e\u0301"
        composed = "alpha Café\nline 😀 é"
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE messages SET text=? WHERE message_id='c1-m6'", (decomposed,))
        conn.execute("UPDATE messages SET text=? WHERE message_id='c2-m6'", (composed,))
        conn.execute("UPDATE messages SET text=? WHERE message_id='c3-m5'", (composed,))
        conn.commit()
        conn.close()
        package = self.service.build(
            self.query(), budget_profile="custom", character_budget=100_000,
            estimated_token_budget=25_000, per_conversation_cap=20, max_selected=100,
        )
        assistant_entries = [
            item for item in package["candidate_ledger"]
            if item["source"]["message_anchor"]["message_id"] in {"c1-m6", "c2-m6"}
        ]
        self.assertEqual(2, len(assistant_entries))
        self.assertNotEqual(
            assistant_entries[0]["excerpt_commitment"]["sha256"],
            assistant_entries[1]["excerpt_commitment"]["sha256"],
        )
        self.assertEqual(
            assistant_entries[0]["excerpt_commitment"]["normalized_sha256"],
            assistant_entries[1]["excerpt_commitment"]["normalized_sha256"],
        )
        self.assertEqual(
            assistant_entries[0]["duplicate_equivalence"]["key"],
            assistant_entries[1]["duplicate_equivalence"]["key"],
        )
        user_entry = next(
            item for item in package["candidate_ledger"]
            if item["source"]["message_anchor"]["message_id"] == "c3-m5"
        )
        self.assertNotEqual(
            assistant_entries[0]["duplicate_equivalence"]["key"],
            user_entry["duplicate_equivalence"]["key"],
        )
        representative_id = assistant_entries[0]["duplicate_equivalence"]["representative_candidate_id"]
        representative = next(item for item in package["selected"] if item["candidate_id"] == representative_id)
        self.assertEqual(representative["payload_chars"], len(_render_candidate(representative)))
        self.assertEqual(representative["estimated_tokens"], math.ceil(representative["payload_chars"] / 4))
        validate_evidence_selection_standalone(package)

    def test_standalone_accepts_source_identity_rebind_but_source_backed_rejects(self):
        source_package = self.memory_service.build(self.query())
        package = build_selection_from_memory_package(source_package, policy=normalize_policy("medium"))
        validate_evidence_selection_standalone(package)
        validate_evidence_selection_source_backed(
            package, trusted_source_package=source_package, trusted_source_db=self.source,
        )
        rebound = deepcopy(package)
        rebound["source_projection"]["package_identity"] = "mp_" + "f" * 64
        self.reseal_identity(rebound)
        validate_evidence_selection_standalone(rebound)
        with self.assertRaises(EvidenceSelectionValidationError):
            validate_evidence_selection_source_backed(
                rebound, trusted_source_package=source_package, trusted_source_db=self.source,
            )

    def test_service_and_production_surfaces_stop_when_source_backed_validation_fails(self):
        with patch(
            "memory.evidence_selection.validate_evidence_selection_source_backed",
            side_effect=EvidenceSelectionValidationError("source-backed validation blocked"),
        ) as source_backed:
            with self.assertRaises(EvidenceSelectionValidationError):
                self.service.build(self.query(), budget_profile="small")
        source_backed.assert_called_once()

        with patch(
            "tools.evidence_selection_preview.build_production_evidence_selection",
            side_effect=EvidenceSelectionValidationError("source-backed validation blocked"),
        ) as production_build, redirect_stdout(io.StringIO()) as stdout:
            result = preview_main(["alpha", "--hit-types", "keyword", "--budget", "small"])
        self.assertEqual(2, result)
        self.assertEqual("unavailable", json.loads(stdout.getvalue())["status"])
        production_build.assert_called_once()

        handler = object.__new__(AyunOSHandler)
        sent = []
        errors = []
        handler.send_json = lambda payload, status=None: sent.append((payload, status))
        handler.send_text = lambda payload, status=None: sent.append((payload, status))
        handler.send_error_json = lambda message, status=None: errors.append((message, status))
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH.resolve())
        with patch(
            "tools.local_web.build_production_evidence_selection",
            side_effect=EvidenceSelectionValidationError("source-backed validation blocked"),
        ) as production_build:
            handler.api_evidence_selection_preview({"q": ["alpha"], "format": ["text"]})
        production_build.assert_called_once()
        self.assertEqual([], sent)
        self.assertEqual([("Invalid evidence selection query", HTTPStatus.BAD_REQUEST)], errors)

    def test_empty_degraded_and_unavailable_states_propagate(self):
        empty = self.service.build(RetrievalQuery("absent", hit_types=("keyword",)))
        self.assertEqual(("empty", []), (empty["status"], empty["selected"]))
        degraded = self.service.build(RetrievalQuery("alpha", hit_types=("keyword", "semantic"), limit=20))
        self.assertEqual(("partial", "degraded"), (degraded["status"], degraded["degradation"]["status"]))
        missing = self.root / "missing.db"
        missing_memory = MemoryPackageService(
            missing,
            retriever=GraphEnhancedRetrievalService(missing, self.semantic, self.knowledge),
        )
        unavailable = EvidenceSelectionService(missing, memory_package_service=missing_memory).build(
            RetrievalQuery("alpha", hit_types=("keyword",)),
        )
        self.assertEqual(("unavailable", []), (unavailable["status"], unavailable["selected"]))

    def test_json_text_preview_and_identity_are_stable(self):
        first = self.service.build(self.query(), budget_profile="medium")
        second = self.service.build(self.query(), budget_profile="medium")
        self.assertEqual("v7.1-evidence-selection-2", first["protocol_version"])
        self.assertEqual("v7.1-selection-policy-2", first["selection_policy"]["policy_version"])
        self.assertEqual(first["package_identity"], second["package_identity"])
        self.assertEqual(first, json.loads(canonical_selection_json(first)))
        rendered = render_evidence_selection(first)
        self.assertIn(first["model_payload"], rendered)
        self.assertIn(first["package_identity"], rendered)

    def test_cli_and_get_preview_accept_no_database_path(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["alpha", "--db", "other.db"])
        handler = object.__new__(AyunOSHandler)
        sent = []
        errors = []
        handler.send_json = lambda payload, status=None: sent.append((payload, status))
        handler.send_text = lambda payload, status=None: sent.append((payload, status))
        handler.send_error_json = lambda message, status=None: errors.append(message)
        handler.app = Mock(readonly=False, db_mode="test", db_path=self.source)
        handler.api_evidence_selection_preview({"q": ["alpha"]})
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, sent.pop()[1])
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH.resolve())
        expected = self.service.build(self.query(), budget_profile="small")
        with patch("tools.local_web.build_production_evidence_selection", return_value=expected) as build:
            handler.api_evidence_selection_preview({
                "q": ["alpha"], "hit_types": ["keyword"], "limit": ["50"], "budget": ["small"],
            })
        self.assertEqual(expected, sent.pop()[0])
        self.assertEqual("small", build.call_args.kwargs["budget_profile"])
        with patch("tools.local_web.build_production_evidence_selection", return_value=expected):
            handler.api_evidence_selection_preview({"q": ["alpha"], "format": ["text"]})
        self.assertEqual(render_evidence_selection(expected), sent.pop()[0])
        handler.api_evidence_selection_preview({"q": ["alpha"], "db": ["other.db"]})
        self.assertEqual(["Unsupported evidence selection parameter"], errors)

    def test_cli_json_is_stable_and_readonly(self):
        command = [
            sys.executable, "tools/evidence_selection_preview.py", "unlikely-v71-cli-token",
            "--hit-types", "keyword", "--budget", "small", "--json",
        ]
        first = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        second = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual((0, 0), (first.returncode, second.returncode))
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["readonly"])
        self.assertFalse(payload["persisted"])


if __name__ == "__main__":
    unittest.main()
