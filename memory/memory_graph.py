"""Evidence-backed, read-only construction of normalized memory graph candidates."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

from memory.embedding_provider import cosine_similarity
from memory.graph_identity import (
    GraphEdge,
    GraphIdentityError,
    GraphNode,
    canonical_hash,
    make_conversation_node,
    make_edge,
    make_evidence_identity,
    make_generator_config_hash,
    make_memory_space_node,
    make_summary_node,
)


GRAPH_SCHEMA_VERSION = "v6.0"
DEFAULT_MIN_SCORE = 0.80
DETERMINISTIC_GENERATOR_IDENTITY = "yunos_memory_graph_deterministic_v1"
SEMANTIC_PAIR_POLICY = "one_lexical_compatible_identity_per_unordered_pair_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _EmbeddingMetadataMissing(ValueError):
    pass


class _EmbeddingMetadataInvalid(ValueError):
    pass


class _EmbeddingMetadataMismatch(ValueError):
    pass


@dataclass(frozen=True)
class GraphUnavailable:
    relation_type: str
    status: str
    reason: str
    evidence_identity: str
    source_node_id: str | None = None
    target_node_id: str | None = None
    score: float | None = None
    threshold: float | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryGraphBuild:
    schema_version: str
    generator_config_hash: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    unavailable: tuple[GraphUnavailable, ...]

    def node_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(node.node_type for node in self.nodes).items()))

    def edge_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(edge.relation_type for edge in self.edges).items()))

    def unavailable_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.unavailable).items()))


def build_memory_graph(
    summary_records: list[dict[str, Any]],
    embedding_records: list[dict[str, Any]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
) -> MemoryGraphBuild:
    threshold = _threshold(min_score)
    overall_config = make_generator_config_hash({
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "deterministic_generator": DETERMINISTIC_GENERATOR_IDENTITY,
        "semantic_algorithm": "cosine_similarity",
        "semantic_pair_policy": SEMANTIC_PAIR_POLICY,
        "min_score": format(threshold, ".12f"),
    })
    unavailable: list[GraphUnavailable] = []
    summary_nodes: dict[str, GraphNode] = {}
    for record in summary_records:
        try:
            node = make_summary_node(record)
        except GraphIdentityError as exc:
            evidence = make_evidence_identity({
                "source_table": str(record.get("source_table") or ""),
                "source_id": str(record.get("source_id") or ""),
                "reason": str(exc),
            })
            unavailable.append(GraphUnavailable(
                relation_type="semantic_related",
                status="unavailable",
                reason="summary_identity_unavailable",
                evidence_identity=evidence,
                threshold=threshold,
            ))
            continue
        summary_nodes.setdefault(node.node_id, node)

    nodes: dict[str, GraphNode] = dict(summary_nodes)
    deterministic_edges: dict[str, GraphEdge] = {}
    deterministic_config = make_generator_config_hash({
        "generator": DETERMINISTIC_GENERATOR_IDENTITY,
        "relations": [
            "belongs_to_conversation",
            "belongs_to_memory_space",
            "newer_version_of",
        ],
        "newer_version_rule": "same_conversation_consecutive_sidecar_versions",
    })

    for summary in sorted(summary_nodes.values(), key=lambda item: item.node_id):
        conversation = make_conversation_node(summary.conversation_id or "", summary.memory_space)
        memory_space = make_memory_space_node(summary.memory_space or "")
        nodes.setdefault(conversation.node_id, conversation)
        nodes.setdefault(memory_space.node_id, memory_space)
        for relation_type, target in (
            ("belongs_to_conversation", conversation),
            ("belongs_to_memory_space", memory_space),
        ):
            evidence = make_evidence_identity({
                "relation_type": relation_type,
                "summary_node_id": summary.node_id,
                "target_node_id": target.node_id,
            })
            edge = make_edge(
                relation_type,
                summary.node_id,
                target.node_id,
                direction="directed",
                generator_identity=DETERMINISTIC_GENERATOR_IDENTITY,
                generator_config_hash=deterministic_config,
                evidence_identity=evidence,
                status="deterministic",
            )
            deterministic_edges.setdefault(edge.edge_id, edge)

    by_conversation: dict[str, list[GraphNode]] = {}
    for node in summary_nodes.values():
        if node.source_table == "summary_versions" and node.summary_version is not None:
            by_conversation.setdefault(str(node.conversation_id), []).append(node)
    for versions in by_conversation.values():
        ordered = sorted(versions, key=lambda item: (int(item.summary_version or 0), item.node_id))
        for older, newer in zip(ordered, ordered[1:]):
            if int(newer.summary_version or 0) != int(older.summary_version or 0) + 1:
                unavailable.append(_unavailable(
                    "newer_version_of",
                    "summary_version_gap",
                    newer.node_id,
                    older.node_id,
                ))
                continue
            evidence = make_evidence_identity({
                "conversation_id": newer.conversation_id,
                "newer_node_id": newer.node_id,
                "older_node_id": older.node_id,
                "newer_version": newer.summary_version,
                "older_version": older.summary_version,
            })
            edge = make_edge(
                "newer_version_of",
                newer.node_id,
                older.node_id,
                direction="directed",
                generator_identity=DETERMINISTIC_GENERATOR_IDENTITY,
                generator_config_hash=deterministic_config,
                evidence_identity=evidence,
                status="deterministic",
            )
            deterministic_edges.setdefault(edge.edge_id, edge)

    semantic_edges, semantic_unavailable = _semantic_candidates(
        sorted(summary_nodes.values(), key=lambda item: item.node_id),
        embedding_records,
        threshold,
    )
    unavailable.extend(semantic_unavailable)
    edges = list(deterministic_edges.values()) + semantic_edges
    return MemoryGraphBuild(
        schema_version=GRAPH_SCHEMA_VERSION,
        generator_config_hash=overall_config,
        nodes=tuple(sorted(nodes.values(), key=_node_sort_key)),
        edges=tuple(sorted(edges, key=lambda item: (item.relation_type, item.edge_id))),
        unavailable=tuple(sorted(unavailable, key=_unavailable_sort_key)),
    )


def _semantic_candidates(
    summary_nodes: list[GraphNode],
    embedding_records: list[dict[str, Any]],
    threshold: float,
) -> tuple[list[GraphEdge], list[GraphUnavailable]]:
    by_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in embedding_records:
        key = (str(record.get("source_table") or ""), str(record.get("source_id") or ""))
        by_source.setdefault(key, []).append(record)

    evidence_by_node: dict[str, dict[str, dict[str, Any]]] = {}
    unavailable: list[GraphUnavailable] = []
    for node in summary_nodes:
        matches = by_source.get((node.source_table, node.source_id), [])
        valid, reason = _compatible_node_embeddings(node, matches)
        evidence_by_node[node.node_id] = valid
        if not valid:
            unavailable.append(GraphUnavailable(
                relation_type="semantic_related",
                status="unavailable",
                reason=reason,
                evidence_identity=make_evidence_identity({
                    "node_id": node.node_id,
                    "reason": reason,
                    "matching_embedding_count": len(matches),
                }),
                source_node_id=node.node_id,
                threshold=threshold,
            ))

    edges: dict[str, GraphEdge] = {}
    for left, right in combinations(summary_nodes, 2):
        left_records = evidence_by_node[left.node_id]
        right_records = evidence_by_node[right.node_id]
        if not left_records or not right_records:
            continue
        common = sorted(set(left_records) & set(right_records))
        if not common:
            unavailable.append(GraphUnavailable(
                relation_type="semantic_related",
                status="unavailable",
                reason="embedding_identity_incompatible",
                evidence_identity=make_evidence_identity({
                    "source_node_id": min(left.node_id, right.node_id),
                    "target_node_id": max(left.node_id, right.node_id),
                    "left_identities": sorted(left_records),
                    "right_identities": sorted(right_records),
                }),
                source_node_id=min(left.node_id, right.node_id),
                target_node_id=max(left.node_id, right.node_id),
                threshold=threshold,
            ))
            continue

        identity_key = common[0]
        left_evidence = left_records[identity_key]
        right_evidence = right_records[identity_key]
        score = float(format(cosine_similarity(left_evidence["vector"], right_evidence["vector"]), ".12f"))
        if score < threshold:
            unavailable.append(GraphUnavailable(
                relation_type="semantic_related",
                status="unavailable",
                reason="semantic_score_below_threshold",
                evidence_identity=make_evidence_identity({
                    "left_evidence": left_evidence["evidence_identity"],
                    "right_evidence": right_evidence["evidence_identity"],
                    "score": format(score, ".12f"),
                    "threshold": format(threshold, ".12f"),
                }),
                source_node_id=min(left.node_id, right.node_id),
                target_node_id=max(left.node_id, right.node_id),
                score=score,
                threshold=threshold,
            ))
            continue

        identity = left_evidence["identity"]
        generator_identity = _semantic_generator_identity(identity)
        generator_config = make_generator_config_hash({
            "generator_identity": generator_identity,
            "algorithm": "cosine_similarity",
            "pair_policy": SEMANTIC_PAIR_POLICY,
            "min_score": format(threshold, ".12f"),
            "embedding_identity": identity,
        })
        evidence = make_evidence_identity({
            "source_node_id": min(left.node_id, right.node_id),
            "target_node_id": max(left.node_id, right.node_id),
            "left_evidence": left_evidence["evidence_identity"],
            "right_evidence": right_evidence["evidence_identity"],
        })
        edge = make_edge(
            "semantic_related",
            left.node_id,
            right.node_id,
            direction="undirected",
            generator_identity=generator_identity,
            generator_config_hash=generator_config,
            evidence_identity=evidence,
            score=score,
            threshold=threshold,
            status="candidate",
        )
        edges.setdefault(edge.edge_id, edge)
    return list(edges.values()), unavailable


def _compatible_node_embeddings(
    node: GraphNode,
    records: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    if not records:
        return {}, "embedding_missing"
    current_hash_records = [record for record in records if record.get("content_hash") == node.summary_text_hash]
    if not current_hash_records:
        return {}, "embedding_content_mismatch"

    valid: dict[str, dict[str, Any]] = {}
    saw_metadata_missing = False
    saw_metadata_invalid = False
    saw_metadata_mismatch = False
    saw_identity_error = False
    saw_vector_error = False
    ordered = sorted(current_hash_records, key=_embedding_order_key)
    for record in ordered:
        try:
            verified_metadata = _verified_embedding_metadata(node, record)
        except _EmbeddingMetadataMissing:
            saw_metadata_missing = True
            continue
        except _EmbeddingMetadataInvalid:
            saw_metadata_invalid = True
            continue
        except _EmbeddingMetadataMismatch:
            saw_metadata_mismatch = True
            continue
        try:
            identity = _embedding_identity(record)
        except (TypeError, ValueError):
            saw_identity_error = True
            continue
        try:
            vector = _embedding_vector(record, int(identity["embedding_dim"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            saw_vector_error = True
            continue
        identity_key = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        vector_json = json.dumps(vector, ensure_ascii=True, separators=(",", ":"))
        metadata_identity = (
            "metadata_"
            + canonical_hash("verified_embedding_metadata", verified_metadata)
        )
        record_evidence = make_evidence_identity({
            "source_table": verified_metadata["source_table"],
            "source_id": verified_metadata["source_id"],
            "content_hash": record.get("content_hash"),
            "embedding_identity": identity,
            "verified_metadata_identity": metadata_identity,
            "embedding_hash": hashlib.sha256(vector_json.encode("utf-8")).hexdigest(),
        })
        valid.setdefault(identity_key, {
            "identity": identity,
            "vector": vector,
            "evidence_identity": record_evidence,
            "verified_metadata_identity": metadata_identity,
        })
    if valid:
        return valid, ""
    if saw_metadata_mismatch:
        return {}, "embedding_metadata_mismatch"
    if saw_metadata_invalid:
        return {}, "embedding_metadata_invalid"
    if saw_metadata_missing:
        return {}, "embedding_metadata_missing"
    if saw_identity_error:
        return {}, "embedding_identity_incompatible"
    if saw_vector_error:
        return {}, "embedding_vector_invalid"
    return {}, "embedding_unavailable"


def _verified_embedding_metadata(
    node: GraphNode,
    record: dict[str, Any],
) -> dict[str, Any]:
    raw = record.get("metadata_json")
    if raw is None or raw == "":
        raise _EmbeddingMetadataMissing("embedding metadata is missing")
    if not isinstance(raw, str):
        raise _EmbeddingMetadataInvalid("embedding metadata is invalid")
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _EmbeddingMetadataInvalid("embedding metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise _EmbeddingMetadataInvalid("embedding metadata is invalid")
    if not metadata:
        raise _EmbeddingMetadataMissing("embedding metadata is missing")

    required = {
        "summary_source",
        "summary_id",
        "conversation_id",
        "summary_version",
        "summary_text_hash",
        "source_snapshot_hash",
        "memory_space",
        "semantic_target_kind",
        "summary_type",
    }
    if not required.issubset(metadata):
        raise _EmbeddingMetadataMissing("embedding metadata is incomplete")

    if not all(
        isinstance(record.get(key), str)
        for key in ("source_table", "source_id", "memory_space")
    ):
        raise _EmbeddingMetadataInvalid("embedding source identity type is invalid")
    source_table = record["source_table"].strip()
    source_id = record["source_id"].strip()
    summary_source = _metadata_text(metadata, "summary_source")
    summary_id = _metadata_text(metadata, "summary_id")
    conversation_id = _metadata_text(metadata, "conversation_id")
    summary_text_hash = _metadata_hash(metadata, "summary_text_hash")
    source_snapshot_hash = _metadata_hash(metadata, "source_snapshot_hash")
    memory_space = _metadata_text(metadata, "memory_space")
    semantic_target_kind = _metadata_text(metadata, "semantic_target_kind")
    summary_type = _metadata_text(metadata, "summary_type")

    raw_version = metadata["summary_version"]
    if node.summary_version is None:
        if raw_version is not None:
            raise _EmbeddingMetadataMismatch("embedding summary version does not match")
        summary_version = None
    else:
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise _EmbeddingMetadataInvalid("embedding summary version is invalid")
        summary_version = raw_version

    expected_summary_source = (
        "memory_sidecar" if node.source_table == "summary_versions" else "legacy"
    )
    top_level_source_table = record["source_table"].strip()
    top_level_source_id = record["source_id"].strip()
    top_level_memory_space = record["memory_space"].strip()
    expected = {
        "source_table": node.source_table,
        "source_id": node.source_id,
        "summary_source": expected_summary_source,
        "summary_id": node.source_id,
        "conversation_id": str(node.conversation_id or ""),
        "summary_version": node.summary_version,
        "summary_text_hash": node.summary_text_hash,
        "source_snapshot_hash": node.source_snapshot_hash,
        "memory_space": str(node.memory_space or ""),
        "semantic_target_kind": "summary_embedding",
        "summary_type": "conversation_summary",
    }
    normalized = {
        "source_table": source_table,
        "source_id": source_id,
        "summary_source": summary_source,
        "summary_id": summary_id,
        "conversation_id": conversation_id,
        "summary_version": summary_version,
        "summary_text_hash": summary_text_hash,
        "source_snapshot_hash": source_snapshot_hash,
        "memory_space": memory_space,
        "semantic_target_kind": semantic_target_kind,
        "summary_type": summary_type,
    }
    if (
        top_level_source_table != node.source_table
        or top_level_source_id != node.source_id
        or top_level_memory_space != str(node.memory_space or "")
        or any(normalized[key] != value for key, value in expected.items())
    ):
        raise _EmbeddingMetadataMismatch("embedding metadata does not match summary identity")
    return normalized


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise _EmbeddingMetadataInvalid("embedding metadata field type is invalid")
    clean = value.strip()
    if not clean:
        raise _EmbeddingMetadataInvalid("embedding metadata field is empty")
    return clean


def _metadata_hash(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise _EmbeddingMetadataInvalid("embedding metadata hash type is invalid")
    clean = value.strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise _EmbeddingMetadataInvalid("embedding metadata hash is invalid")
    return clean


def _embedding_identity(record: dict[str, Any]) -> dict[str, Any]:
    raw_fields = {
        key: record.get(key)
        for key in (
            "embedding_provider",
            "embedding_model",
            "model_revision",
            "model_fingerprint",
            "embedding_protocol",
        )
    }
    if not all(isinstance(value, str) for value in raw_fields.values()):
        raise ValueError("embedding provider identity is invalid")
    provider = raw_fields["embedding_provider"].strip()
    if not provider or provider == "legacy_unversioned":
        raise ValueError("embedding provider identity is unavailable")
    model = raw_fields["embedding_model"].strip()
    revision = raw_fields["model_revision"].strip()
    fingerprint = raw_fields["model_fingerprint"].strip()
    protocol = raw_fields["embedding_protocol"].strip()
    raw_dimension = record.get("embedding_dim")
    raw_normalized = record.get("embedding_normalized")
    if (
        isinstance(raw_dimension, bool)
        or not isinstance(raw_dimension, int)
        or isinstance(raw_normalized, bool)
        or not isinstance(raw_normalized, int)
    ):
        raise ValueError("embedding provider identity is invalid")
    dimension = raw_dimension
    normalized = raw_normalized
    if not all((model, revision, fingerprint, protocol)) or dimension <= 0 or normalized not in {0, 1}:
        raise ValueError("embedding identity is incomplete")
    return {
        "embedding_provider": provider,
        "embedding_model": model,
        "model_revision": revision,
        "model_fingerprint": fingerprint,
        "embedding_protocol": protocol,
        "embedding_normalized": normalized,
        "embedding_dim": dimension,
    }


def _embedding_vector(record: dict[str, Any], expected_dim: int) -> list[float]:
    value = json.loads(str(record.get("embedding_json") or ""))
    if not isinstance(value, list):
        raise TypeError("embedding vector is not a list")
    vector = [float(item) for item in value]
    if len(vector) != expected_dim or any(not math.isfinite(item) for item in vector):
        raise ValueError("embedding vector is incompatible")
    return vector


def _semantic_generator_identity(identity: dict[str, Any]) -> str:
    return (
        "semantic_cosine_v1"
        f"|provider={identity['embedding_provider']}"
        f"|model={identity['embedding_model']}"
        f"|revision={identity['model_revision']}"
        f"|fingerprint={identity['model_fingerprint']}"
        f"|protocol={identity['embedding_protocol']}"
        f"|dim={identity['embedding_dim']}"
        f"|normalized={identity['embedding_normalized']}"
    )


def _unavailable(
    relation_type: str,
    reason: str,
    source_node_id: str,
    target_node_id: str | None,
) -> GraphUnavailable:
    return GraphUnavailable(
        relation_type=relation_type,
        status="unavailable",
        reason=reason,
        evidence_identity=make_evidence_identity({
            "relation_type": relation_type,
            "reason": reason,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
        }),
        source_node_id=source_node_id,
        target_node_id=target_node_id,
    )


def _threshold(value: float) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_score must be between -1 and 1") from exc
    if not math.isfinite(threshold) or threshold < -1.0 or threshold > 1.0:
        raise ValueError("min_score must be between -1 and 1")
    return float(format(threshold, ".12f"))


def _embedding_order_key(record: dict[str, Any]) -> str:
    return canonical_hash(
        "embedding_record_order",
        {
            "source_table": record.get("source_table"),
            "source_id": record.get("source_id"),
            "memory_space": record.get("memory_space"),
            "content_hash": record.get("content_hash"),
            "provider": record.get("embedding_provider"),
            "model": record.get("embedding_model"),
            "revision": record.get("model_revision"),
            "fingerprint": record.get("model_fingerprint"),
            "protocol": record.get("embedding_protocol"),
            "dimension": record.get("embedding_dim"),
            "normalized": record.get("embedding_normalized"),
            "embedding_json": record.get("embedding_json"),
            "metadata_json": record.get("metadata_json"),
        },
    )


def _node_sort_key(node: GraphNode) -> tuple[str, str, str, int, str]:
    order = {"memory_space": "0", "conversation": "1", "summary": "2"}
    return (
        order.get(node.node_type, "9"),
        str(node.memory_space or ""),
        str(node.conversation_id or ""),
        -1 if node.summary_version is None else node.summary_version,
        node.node_id,
    )


def _unavailable_sort_key(item: GraphUnavailable) -> tuple[str, str, str, str]:
    return (
        item.relation_type,
        item.reason,
        str(item.source_node_id or ""),
        str(item.target_node_id or ""),
    )
