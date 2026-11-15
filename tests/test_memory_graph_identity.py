from __future__ import annotations

import hashlib
import unittest

from memory.graph_identity import (
    make_conversation_node,
    make_edge,
    make_evidence_identity,
    make_generator_config_hash,
    make_memory_space_node,
    make_summary_node,
)


def summary_record(
    *,
    source_table: str = "summary_versions",
    source_id: str = "1",
    conversation_id: str = "c1",
    version: int | None = 1,
    text: str = "stable summary",
    snapshot: str | None = None,
    memory_space: str = "whale",
) -> dict:
    return {
        "source_table": source_table,
        "source_id": source_id,
        "conversation_id": conversation_id,
        "summary_version": version,
        "summary_text": text,
        "summary_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_snapshot_hash": snapshot or hashlib.sha256(b"snapshot").hexdigest(),
        "memory_space": memory_space,
    }


class MemoryGraphIdentityTest(unittest.TestCase):
    def test_sidecar_and_legacy_numeric_ids_do_not_collide(self):
        sidecar = make_summary_node(summary_record())
        legacy = make_summary_node(summary_record(
            source_table="memory_summaries",
            version=None,
        ))
        self.assertNotEqual(sidecar.node_id, legacy.node_id)

    def test_same_summary_identity_is_stable(self):
        first = make_summary_node(summary_record())
        second = make_summary_node(dict(summary_record()))
        self.assertEqual(first.node_id, second.node_id)

    def test_version_text_and_snapshot_changes_change_identity(self):
        base = make_summary_node(summary_record())
        changed_version = make_summary_node(summary_record(version=2))
        changed_text = make_summary_node(summary_record(text="changed summary"))
        changed_snapshot = make_summary_node(summary_record(
            snapshot=hashlib.sha256(b"new snapshot").hexdigest()
        ))
        self.assertEqual(4, len({
            base.node_id,
            changed_version.node_id,
            changed_text.node_id,
            changed_snapshot.node_id,
        }))

    def test_conversation_and_memory_space_nodes_are_stable(self):
        conversation_a = make_conversation_node("c1", "whale")
        conversation_b = make_conversation_node("c1", "general")
        conversation_c = make_conversation_node("c2", "whale")
        space_a = make_memory_space_node("whale")
        space_b = make_memory_space_node("whale")
        self.assertEqual(conversation_a.node_id, conversation_b.node_id)
        self.assertEqual(conversation_a.public_dict(), conversation_b.public_dict())
        self.assertIsNone(conversation_a.memory_space)
        self.assertNotEqual(conversation_a.node_id, conversation_c.node_id)
        self.assertEqual(space_a.node_id, space_b.node_id)

    def test_undirected_edge_normalizes_endpoint_order(self):
        config = make_generator_config_hash({"threshold": 0.8})
        evidence = make_evidence_identity({"left": "a", "right": "b"})
        forward = make_edge(
            "semantic_related", "node_a", "node_b",
            direction="undirected", generator_identity="semantic-v1",
            generator_config_hash=config, evidence_identity=evidence,
            score=0.9, threshold=0.8, status="candidate",
        )
        reverse = make_edge(
            "semantic_related", "node_b", "node_a",
            direction="undirected", generator_identity="semantic-v1",
            generator_config_hash=config, evidence_identity=evidence,
            score=0.9, threshold=0.8, status="candidate",
        )
        self.assertEqual(forward.edge_id, reverse.edge_id)
        self.assertEqual(("node_a", "node_b"), (forward.source_node_id, forward.target_node_id))

    def test_generator_config_change_changes_edge_identity(self):
        evidence = make_evidence_identity({"left": "a", "right": "b"})
        first = make_edge(
            "semantic_related", "node_a", "node_b", direction="undirected",
            generator_identity="semantic-v1",
            generator_config_hash=make_generator_config_hash({"threshold": 0.8}),
            evidence_identity=evidence, score=0.95, threshold=0.8, status="candidate",
        )
        second = make_edge(
            "semantic_related", "node_a", "node_b", direction="undirected",
            generator_identity="semantic-v1",
            generator_config_hash=make_generator_config_hash({"threshold": 0.9}),
            evidence_identity=evidence, score=0.95, threshold=0.9, status="candidate",
        )
        self.assertNotEqual(first.edge_id, second.edge_id)


if __name__ == "__main__":
    unittest.main()
