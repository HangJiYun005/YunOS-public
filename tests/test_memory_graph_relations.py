from __future__ import annotations

import hashlib
import json
import unittest

from memory.memory_graph import build_memory_graph


def summary(
    source_id: str,
    conversation_id: str,
    *,
    version: int | None = 1,
    text: str | None = None,
    memory_space: str = "whale",
    source_table: str = "summary_versions",
    snapshot: str | None = None,
) -> dict:
    body = text or f"summary {source_id}"
    return {
        "source_table": source_table,
        "source_id": source_id,
        "conversation_id": conversation_id,
        "summary_version": version,
        "summary_text": body,
        "summary_text_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "source_snapshot_hash": snapshot or hashlib.sha256(f"{conversation_id}:{version}".encode()).hexdigest(),
        "memory_space": memory_space,
    }


def embedding(
    record: dict,
    vector: list[float],
    *,
    record_id: int,
    provider: str = "test_provider",
    model: str = "test_model",
) -> dict:
    metadata = {
        "summary_source": (
            "memory_sidecar" if record["source_table"] == "summary_versions" else "legacy"
        ),
        "summary_id": record["source_id"],
        "conversation_id": record["conversation_id"],
        "summary_version": record["summary_version"],
        "summary_text_hash": record["summary_text_hash"],
        "source_snapshot_hash": record["source_snapshot_hash"],
        "memory_space": record["memory_space"],
        "semantic_target_kind": "summary_embedding",
        "summary_type": "conversation_summary",
    }
    return {
        "id": record_id,
        "source_table": record["source_table"],
        "source_id": record["source_id"],
        "memory_space": record["memory_space"],
        "source_platform": None,
        "embedding_provider": provider,
        "embedding_model": model,
        "model_revision": "revision-1",
        "model_fingerprint": hashlib.sha256(f"{provider}:{model}".encode()).hexdigest(),
        "embedding_protocol": "normalized_test_v1",
        "embedding_normalized": 1,
        "embedding_dim": len(vector),
        "embedding_json": json.dumps(vector),
        "content_hash": record["summary_text_hash"],
        "content_preview": record["summary_text"],
        "created_at": f"2026-01-0{record_id}T00:00:00Z",
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


class MemoryGraphRelationsTest(unittest.TestCase):
    def test_belongs_to_edges_are_deterministic_and_stable(self):
        records = [summary("s1", "c1"), summary("s2", "c2")]
        first = build_memory_graph(records, [], min_score=0.8)
        second = build_memory_graph(list(reversed(records)), [], min_score=0.8)
        first_ids = {
            edge.edge_id for edge in first.edges
            if edge.relation_type.startswith("belongs_to_")
        }
        second_ids = {
            edge.edge_id for edge in second.edges
            if edge.relation_type.startswith("belongs_to_")
        }
        self.assertEqual(4, len(first_ids))
        self.assertEqual(first_ids, second_ids)
        self.assertTrue(all(edge.status == "deterministic" for edge in first.edges))

    def test_one_conversation_can_link_summaries_in_different_memory_spaces(self):
        whale = summary("whale-summary", "c1", memory_space="whale", version=1)
        general = summary("general-summary", "c1", memory_space="general", version=2)
        first = build_memory_graph([whale, general], [])
        second = build_memory_graph([general, whale], [])
        conversations = [node for node in first.nodes if node.node_type == "conversation"]
        spaces = [node for node in first.nodes if node.node_type == "memory_space"]
        memberships = [edge for edge in first.edges if edge.relation_type == "belongs_to_memory_space"]
        self.assertEqual(1, len(conversations))
        self.assertIsNone(conversations[0].memory_space)
        self.assertEqual({"whale", "general"}, {node.memory_space for node in spaces})
        self.assertEqual(2, len(memberships))
        self.assertEqual(
            [node.public_dict() for node in first.nodes if node.node_type == "conversation"],
            [node.public_dict() for node in second.nodes if node.node_type == "conversation"],
        )

    def test_newer_version_only_connects_consecutive_versions_in_same_conversation(self):
        records = [
            summary("c1-v1", "c1", version=1),
            summary("c1-v2", "c1", version=2),
            summary("c1-v4", "c1", version=4),
            summary("c2-v1", "c2", version=1),
        ]
        build = build_memory_graph(records, [])
        version_edges = [edge for edge in build.edges if edge.relation_type == "newer_version_of"]
        self.assertEqual(1, len(version_edges))
        node_by_id = {node.node_id: node for node in build.nodes}
        self.assertEqual(2, node_by_id[version_edges[0].source_node_id].summary_version)
        self.assertEqual(1, node_by_id[version_edges[0].target_node_id].summary_version)
        self.assertIn("summary_version_gap", build.unavailable_counts())

    def test_semantic_related_is_one_undirected_candidate_per_pair(self):
        left = summary("left", "c1", text="same evidence")
        right = summary("right", "c2", text="same evidence")
        embeddings = [embedding(left, [1.0, 0.0], record_id=1), embedding(right, [1.0, 0.0], record_id=2)]
        first = build_memory_graph([left, right], embeddings, min_score=0.8)
        second = build_memory_graph([right, left], list(reversed(embeddings)), min_score=0.8)
        semantic = [edge for edge in first.edges if edge.relation_type == "semantic_related"]
        reversed_semantic = [edge for edge in second.edges if edge.relation_type == "semantic_related"]
        self.assertEqual(1, len(semantic))
        self.assertEqual("undirected", semantic[0].direction)
        self.assertEqual("candidate", semantic[0].status)
        self.assertEqual(semantic[0].edge_id, reversed_semantic[0].edge_id)

    def test_semantic_evidence_ignores_row_and_query_order(self):
        left = summary("left", "c1", text="same evidence")
        right = summary("right", "c2", text="same evidence")
        first_embeddings = [
            embedding(left, [1.0, 0.0], record_id=1),
            embedding(right, [1.0, 0.0], record_id=2),
        ]
        second_embeddings = [
            embedding(right, [1.0, 0.0], record_id=200),
            embedding(left, [1.0, 0.0], record_id=100),
        ]
        for record in second_embeddings:
            metadata = json.loads(record["metadata_json"])
            record["metadata_json"] = json.dumps(metadata, sort_keys=False, indent=2)
        first = build_memory_graph([left, right], first_embeddings)
        second = build_memory_graph([right, left], second_embeddings)
        first_edge = next(edge for edge in first.edges if edge.relation_type == "semantic_related")
        second_edge = next(edge for edge in second.edges if edge.relation_type == "semantic_related")
        self.assertEqual(first_edge.evidence_identity, second_edge.evidence_identity)
        self.assertEqual(first_edge.edge_id, second_edge.edge_id)

    def test_low_similarity_does_not_create_candidate(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        build = build_memory_graph(
            [left, right],
            [embedding(left, [1.0, 0.0], record_id=1), embedding(right, [0.0, 1.0], record_id=2)],
            min_score=0.5,
        )
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
        self.assertEqual(1, build.unavailable_counts()["semantic_score_below_threshold"])

    def test_missing_and_incompatible_embeddings_are_explicitly_unavailable(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        missing = build_memory_graph([left, right], [])
        self.assertEqual(2, missing.unavailable_counts()["embedding_missing"])

        incompatible = build_memory_graph(
            [left, right],
            [
                embedding(left, [1.0, 0.0], record_id=1, provider="provider-a"),
                embedding(right, [1.0, 0.0], record_id=2, provider="provider-b"),
            ],
        )
        self.assertEqual(1, incompatible.unavailable_counts()["embedding_identity_incompatible"])
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in incompatible.edges))

    def test_legacy_embedding_does_not_override_current_compatible_embedding(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        legacy = embedding(left, [0.0, 1.0], record_id=1, provider="legacy_unversioned")
        current_left = embedding(left, [1.0, 0.0], record_id=2)
        current_right = embedding(right, [1.0, 0.0], record_id=3)
        build = build_memory_graph([left, right], [legacy, current_left, current_right])
        semantic = [edge for edge in build.edges if edge.relation_type == "semantic_related"]
        self.assertEqual(1, len(semantic))
        self.assertEqual(1.0, semantic[0].score)
        self.assertIn("provider=test_provider", semantic[0].generator_identity)

    def test_generator_threshold_change_changes_candidate_edge_id(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        embeddings = [embedding(left, [1.0, 0.0], record_id=1), embedding(right, [1.0, 0.0], record_id=2)]
        low = build_memory_graph([left, right], embeddings, min_score=0.7)
        high = build_memory_graph([left, right], embeddings, min_score=0.9)
        low_edge = next(edge for edge in low.edges if edge.relation_type == "semantic_related")
        high_edge = next(edge for edge in high.edges if edge.relation_type == "semantic_related")
        self.assertNotEqual(low_edge.generator_config_hash, high_edge.generator_config_hash)
        self.assertNotEqual(low_edge.edge_id, high_edge.edge_id)

    def test_exact_metadata_fields_are_required_for_semantic_candidates(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        right_embedding = embedding(right, [1.0, 0.0], record_id=2)
        mutations = {
            "summary_id": "wrong-summary",
            "conversation_id": "wrong-conversation",
            "summary_version": 999,
            "summary_text_hash": hashlib.sha256(b"wrong-text").hexdigest(),
            "source_snapshot_hash": hashlib.sha256(b"wrong-snapshot").hexdigest(),
            "memory_space": "general",
            "summary_source": "legacy",
            "semantic_target_kind": "wrong-target",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                left_embedding = embedding(left, [1.0, 0.0], record_id=1)
                metadata = json.loads(left_embedding["metadata_json"])
                metadata[field] = value
                left_embedding["metadata_json"] = json.dumps(metadata, sort_keys=True)
                build = build_memory_graph([left, right], [left_embedding, right_embedding])
                self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
                self.assertEqual(1, build.unavailable_counts()["embedding_metadata_mismatch"])

    def test_missing_corrupt_and_wrong_type_metadata_are_unavailable(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        right_embedding = embedding(right, [1.0, 0.0], record_id=2)
        base = embedding(left, [1.0, 0.0], record_id=1)
        missing_field = json.loads(base["metadata_json"])
        missing_field.pop("conversation_id")
        wrong_type = json.loads(base["metadata_json"])
        wrong_type["summary_version"] = "1"
        cases = (
            (None, "embedding_metadata_missing"),
            ("{}", "embedding_metadata_missing"),
            (json.dumps(missing_field), "embedding_metadata_missing"),
            ("{bad json", "embedding_metadata_invalid"),
            (json.dumps([]), "embedding_metadata_invalid"),
            (json.dumps(wrong_type), "embedding_metadata_invalid"),
        )
        for raw, reason in cases:
            with self.subTest(raw=raw, reason=reason):
                left_embedding = dict(base)
                left_embedding["metadata_json"] = raw
                build = build_memory_graph([left, right], [left_embedding, right_embedding])
                self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
                self.assertEqual(1, build.unavailable_counts()[reason])

    def test_top_level_source_space_and_content_mismatches_fail_closed(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        right_embedding = embedding(right, [1.0, 0.0], record_id=2)
        cases = {
            "source_table": "memory_summaries",
            "source_id": "wrong-source",
            "memory_space": "general",
            "content_hash": hashlib.sha256(b"wrong-content").hexdigest(),
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                left_embedding = embedding(left, [1.0, 0.0], record_id=1)
                left_embedding[field] = value
                build = build_memory_graph([left, right], [left_embedding, right_embedding])
                self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
                self.assertGreater(sum(build.unavailable_counts().values()), 0)

    def test_top_level_identity_and_provider_type_errors_fail_closed(self):
        left = summary("1", "c1")
        right = summary("right", "c2")
        right_embedding = embedding(right, [1.0, 0.0], record_id=2)

        numeric_source = embedding(left, [1.0, 0.0], record_id=1)
        numeric_source["source_id"] = 1
        source_build = build_memory_graph([left, right], [numeric_source, right_embedding])
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in source_build.edges))
        self.assertEqual(1, source_build.unavailable_counts()["embedding_metadata_invalid"])

        invalid_provider = embedding(left, [1.0, 0.0], record_id=1)
        invalid_provider["embedding_provider"] = 123
        provider_build = build_memory_graph([left, right], [invalid_provider, right_embedding])
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in provider_build.edges))
        self.assertEqual(1, provider_build.unavailable_counts()["embedding_identity_incompatible"])

    def test_one_valid_and_one_mismatched_embedding_produces_no_edge(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        left_embedding = embedding(left, [1.0, 0.0], record_id=1)
        right_embedding = embedding(right, [1.0, 0.0], record_id=2)
        metadata = json.loads(right_embedding["metadata_json"])
        metadata["conversation_id"] = "wrong"
        right_embedding["metadata_json"] = json.dumps(metadata)
        build = build_memory_graph([left, right], [left_embedding, right_embedding])
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
        self.assertEqual(1, build.unavailable_counts()["embedding_metadata_mismatch"])

    def test_provider_and_dimension_incompatibility_remain_unavailable(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        provider_mismatch = build_memory_graph(
            [left, right],
            [
                embedding(left, [1.0, 0.0], record_id=1, provider="provider-a"),
                embedding(right, [1.0, 0.0], record_id=2, provider="provider-b"),
            ],
        )
        self.assertEqual(1, provider_mismatch.unavailable_counts()["embedding_identity_incompatible"])

        dimension_mismatch = build_memory_graph(
            [left, right],
            [
                embedding(left, [1.0, 0.0], record_id=1),
                embedding(right, [1.0, 0.0, 0.0], record_id=2),
            ],
        )
        self.assertEqual(1, dimension_mismatch.unavailable_counts()["embedding_identity_incompatible"])
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in dimension_mismatch.edges))

    def test_legacy_and_sidecar_numeric_ids_do_not_cross_bind(self):
        sidecar = summary("1", "c1", source_table="summary_versions", version=1)
        legacy = summary("1", "c1", source_table="memory_summaries", version=None)
        build = build_memory_graph(
            [sidecar, legacy],
            [embedding(legacy, [1.0, 0.0], record_id=1)],
        )
        self.assertFalse(any(edge.relation_type == "semantic_related" for edge in build.edges))
        self.assertEqual(1, build.unavailable_counts()["embedding_missing"])

    def test_verified_metadata_digest_and_node_change_replace_edge_identity(self):
        left = summary("left", "c1")
        right = summary("right", "c2")
        first = build_memory_graph(
            [left, right],
            [embedding(left, [1.0, 0.0], record_id=1), embedding(right, [1.0, 0.0], record_id=2)],
        )
        changed_left = summary(
            "left",
            "c1",
            snapshot=hashlib.sha256(b"changed-snapshot").hexdigest(),
        )
        second = build_memory_graph(
            [changed_left, right],
            [
                embedding(changed_left, [1.0, 0.0], record_id=99),
                embedding(right, [1.0, 0.0], record_id=100),
            ],
        )
        first_edge = next(edge for edge in first.edges if edge.relation_type == "semantic_related")
        second_edge = next(edge for edge in second.edges if edge.relation_type == "semantic_related")
        self.assertNotEqual(first_edge.evidence_identity, second_edge.evidence_identity)
        self.assertNotEqual(first_edge.edge_id, second_edge.edge_id)


if __name__ == "__main__":
    unittest.main()
