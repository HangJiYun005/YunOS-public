"""Deterministic, path-free identities for YunOS memory graph records."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any


NODE_SCHEMA_VERSION = 1
EDGE_SCHEMA_VERSION = 1
NODE_TYPES = {"conversation", "summary", "memory_space"}
RELATION_TYPES = {
    "belongs_to_conversation",
    "belongs_to_memory_space",
    "newer_version_of",
    "semantic_related",
}
EDGE_STATUSES = {"deterministic", "candidate", "unavailable", "manual_review"}
DIRECTIONS = {"directed", "undirected"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GraphIdentityError(ValueError):
    """Raised when graph evidence is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class GraphNode:
    node_schema_version: int
    node_id: str
    node_type: str
    source_table: str
    source_id: str
    conversation_id: str | None
    summary_version: int | None
    summary_text_hash: str | None
    source_snapshot_hash: str | None
    memory_space: str | None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphEdge:
    edge_schema_version: int
    edge_id: str
    relation_type: str
    source_node_id: str
    target_node_id: str
    direction: str
    generator_identity: str
    generator_config_hash: str
    evidence_identity: str
    score: float | None
    threshold: float | None
    status: str

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_hash(namespace: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"namespace": namespace, "payload": payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_summary_node(record: dict[str, Any]) -> GraphNode:
    source_table = _required(record, "source_table")
    if source_table not in {"summary_versions", "memory_summaries"}:
        raise GraphIdentityError("summary source table is unsupported")
    source_id = _required(record, "source_id")
    conversation_id = _required(record, "conversation_id")
    memory_space = _required(record, "memory_space")
    summary_hash = _sha256(record.get("summary_text_hash"), "summary text hash")
    snapshot_hash = _sha256(record.get("source_snapshot_hash"), "source snapshot hash")
    text = record.get("summary_text")
    if not isinstance(text, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != summary_hash:
        raise GraphIdentityError("summary text hash verification failed")

    raw_version = record.get("summary_version")
    if source_table == "summary_versions":
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise GraphIdentityError("sidecar summary version is invalid") from exc
        if version < 1:
            raise GraphIdentityError("sidecar summary version is invalid")
    else:
        if raw_version is not None:
            raise GraphIdentityError("legacy summary version must be absent")
        version = None

    identity = {
        "node_schema_version": NODE_SCHEMA_VERSION,
        "node_type": "summary",
        "source_table": source_table,
        "source_id": source_id,
        "conversation_id": conversation_id,
        "summary_version": version,
        "summary_text_hash": summary_hash,
        "source_snapshot_hash": snapshot_hash,
        "memory_space": memory_space,
    }
    return GraphNode(node_id=f"node_{canonical_hash('graph_node', identity)}", **identity)


def make_conversation_node(conversation_id: str, memory_space: str | None) -> GraphNode:
    conversation_key = str(conversation_id or "").strip()
    if not conversation_key:
        raise GraphIdentityError("conversation identity is missing")
    identity = {
        "node_schema_version": NODE_SCHEMA_VERSION,
        "node_type": "conversation",
        "source_table": "conversations",
        "source_id": conversation_key,
        "conversation_id": conversation_key,
        "summary_version": None,
        "summary_text_hash": None,
        "source_snapshot_hash": None,
        # Scope membership belongs to summary nodes and their explicit edges.
        # Keeping mutable scope state off the conversation payload guarantees
        # that one conversation ID always maps to one immutable node record.
        "memory_space": None,
    }
    stable = {
        "node_schema_version": NODE_SCHEMA_VERSION,
        "node_type": "conversation",
        "source_table": "conversations",
        "source_id": conversation_key,
        "conversation_id": conversation_key,
    }
    return GraphNode(node_id=f"node_{canonical_hash('graph_node', stable)}", **identity)


def make_memory_space_node(memory_space: str) -> GraphNode:
    space = str(memory_space or "").strip()
    if not space:
        raise GraphIdentityError("memory space identity is missing")
    identity = {
        "node_schema_version": NODE_SCHEMA_VERSION,
        "node_type": "memory_space",
        "source_table": "memory_spaces",
        "source_id": space,
        "conversation_id": None,
        "summary_version": None,
        "summary_text_hash": None,
        "source_snapshot_hash": None,
        "memory_space": space,
    }
    return GraphNode(node_id=f"node_{canonical_hash('graph_node', identity)}", **identity)


def make_edge(
    relation_type: str,
    source_node_id: str,
    target_node_id: str,
    *,
    direction: str,
    generator_identity: str,
    generator_config_hash: str,
    evidence_identity: str,
    status: str,
    score: float | None = None,
    threshold: float | None = None,
) -> GraphEdge:
    if relation_type not in RELATION_TYPES:
        raise GraphIdentityError("relation type is unsupported")
    if direction not in DIRECTIONS:
        raise GraphIdentityError("edge direction is unsupported")
    if status not in EDGE_STATUSES:
        raise GraphIdentityError("edge status is unsupported")
    if not source_node_id or not target_node_id or source_node_id == target_node_id:
        raise GraphIdentityError("edge endpoints are invalid")
    if not generator_identity or not generator_config_hash or not evidence_identity:
        raise GraphIdentityError("edge generator or evidence identity is missing")
    if direction == "undirected" and target_node_id < source_node_id:
        source_node_id, target_node_id = target_node_id, source_node_id

    clean_score = _clean_score(score, "score")
    clean_threshold = _clean_score(threshold, "threshold")
    identity = {
        "edge_schema_version": EDGE_SCHEMA_VERSION,
        "relation_type": relation_type,
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "direction": direction,
        "generator_identity": generator_identity,
        "generator_config_hash": generator_config_hash,
        "evidence_identity": evidence_identity,
        "score": None if clean_score is None else format(clean_score, ".12f"),
        "threshold": None if clean_threshold is None else format(clean_threshold, ".12f"),
        "status": status,
    }
    return GraphEdge(
        edge_schema_version=EDGE_SCHEMA_VERSION,
        edge_id=f"edge_{canonical_hash('graph_edge', identity)}",
        relation_type=relation_type,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        direction=direction,
        generator_identity=generator_identity,
        generator_config_hash=generator_config_hash,
        evidence_identity=evidence_identity,
        score=clean_score,
        threshold=clean_threshold,
        status=status,
    )


def make_generator_config_hash(config: dict[str, Any]) -> str:
    return f"config_{canonical_hash('graph_generator_config', config)}"


def make_evidence_identity(evidence: dict[str, Any]) -> str:
    return f"evidence_{canonical_hash('graph_evidence', evidence)}"


def _required(record: dict[str, Any], key: str) -> str:
    value = str(record.get(key) or "").strip()
    if not value:
        raise GraphIdentityError(f"{key.replace('_', ' ')} is missing")
    return value


def _sha256(value: Any, label: str) -> str:
    clean = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise GraphIdentityError(f"{label} is invalid")
    return clean


def _clean_score(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    try:
        clean = float(value)
    except (TypeError, ValueError) as exc:
        raise GraphIdentityError(f"edge {label} is invalid") from exc
    if not math.isfinite(clean) or clean < -1.0 or clean > 1.0:
        raise GraphIdentityError(f"edge {label} is invalid")
    return float(format(clean, ".12f"))
