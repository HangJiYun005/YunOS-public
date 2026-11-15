"""Read-only, evidence-bound queries over the YunOS knowledge sidecar."""
from __future__ import annotations

import sqlite3
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from memory.db_path_policy import validate_knowledge_test_db_target, validate_production_knowledge_sidecar
from memory.graph_identity import RELATION_TYPES, canonical_hash, make_edge
from memory.controlled_knowledge_persistence import (
    PERSISTENCE_PROTOCOL_IDENTITY,
    RELATION_VERSION,
)
from memory.knowledge_sidecar import (
    KNOWLEDGE_SCHEMA_FINGERPRINT,
    KNOWLEDGE_SCHEMA_VERSION,
    PRODUCTION_KNOWLEDGE_SIDECAR,
    REQUIRED_TABLES,
    KnowledgeRepositoryError,
    _schema_fingerprint,
    load_knowledge_store_identity,
)
from memory.relation_review import (
    CANDIDATE_SCHEMA_VERSION,
    REVIEWABLE_STATUSES,
    RelationCandidate,
    validate_candidate_identity,
)


QUERY_PROTOCOL_VERSION = "v6.3-query-1"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
DEFAULT_MAX_DEPTH = 1
MAX_PATH_DEPTH = 3
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED64 = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{64}$")


class KnowledgeGraphQueryError(ValueError):
    """Raised when a query is unsafe or violates the v6.3 contract."""


@dataclass(frozen=True)
class KnowledgeGraphQuery:
    conversation_id: str | None = None
    memory_space: str | None = None
    relation_type: str | None = None
    node_id: str | None = None
    max_depth: int = DEFAULT_MAX_DEPTH
    limit: int = DEFAULT_LIMIT
    include_candidates: bool = False

    def normalized(self) -> "KnowledgeGraphQuery":
        conversation_id = _optional_token(self.conversation_id, "conversation_id", 256)
        memory_space = _optional_token(self.memory_space, "memory_space", 128)
        node_id = _optional_token(self.node_id, "node_id", 160)
        relation_type = _optional_token(self.relation_type, "relation_type", 80)
        if relation_type is not None and relation_type not in RELATION_TYPES:
            raise KnowledgeGraphQueryError("relation_type is unsupported")
        if node_id is not None and not node_id.startswith("node_"):
            raise KnowledgeGraphQueryError("node_id is invalid")
        try:
            depth = int(self.max_depth)
            limit = int(self.limit)
        except (TypeError, ValueError) as exc:
            raise KnowledgeGraphQueryError("limit and max_depth must be integers") from exc
        if not 0 <= depth <= MAX_PATH_DEPTH:
            raise KnowledgeGraphQueryError(f"max_depth must be between 0 and {MAX_PATH_DEPTH}")
        if not 1 <= limit <= MAX_LIMIT:
            raise KnowledgeGraphQueryError(f"limit must be between 1 and {MAX_LIMIT}")
        return KnowledgeGraphQuery(
            conversation_id=conversation_id,
            memory_space=memory_space,
            relation_type=relation_type,
            node_id=node_id,
            max_depth=depth,
            limit=limit,
            include_candidates=bool(self.include_candidates),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "memory_space": self.memory_space,
            "relation_type": self.relation_type,
            "node_id": self.node_id,
            "max_depth": self.max_depth,
            "limit": self.limit,
            "include_candidates": self.include_candidates,
        }


class KnowledgeGraphQueryService:
    """Query a sealed sidecar without initializing or mutating it."""

    def __init__(self, path: str | Path = PRODUCTION_KNOWLEDGE_SIDECAR, *, test_mode: bool = False) -> None:
        self.path = (
            validate_knowledge_test_db_target(path)
            if test_mode
            else validate_production_knowledge_sidecar(path)
        )

    @classmethod
    def for_test(cls, path: str | Path) -> "KnowledgeGraphQueryService":
        return cls(path, test_mode=True)

    def query(self, query: KnowledgeGraphQuery | None = None) -> dict[str, Any]:
        request = (query or KnowledgeGraphQuery()).normalized()
        if not self.path.is_file():
            return _unavailable(request, "knowledge_sidecar_not_initialized")

        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN")
            store = _validate_store(conn)
            nodes = _load_nodes(conn)
            store_formal_count = int(conn.execute(
                "SELECT COUNT(*) FROM accepted_relation_versions WHERE status='active'"
            ).fetchone()[0])
            formal = _load_formal_relations(conn, store, nodes, request.relation_type)
            deterministic, candidates, unavailable = _load_graph_relations(conn, request.relation_type)
            conn.rollback()
        except (KnowledgeGraphQueryError, KnowledgeRepositoryError):
            raise
        except sqlite3.Error as exc:
            raise KnowledgeRepositoryError("Knowledge graph query is unavailable") from exc
        finally:
            conn.close()

        selected = [*formal, *deterministic]
        if request.include_candidates:
            selected.extend(candidates)
        selected.sort(key=_relation_sort_key)

        seeds = _seed_node_ids(nodes, request)
        has_explicit_node_filter = any((
            request.node_id, request.conversation_id, request.memory_space,
        ))
        if not has_explicit_node_filter:
            seeds = sorted({
                endpoint for item in selected[: request.limit] for endpoint in _endpoints(item)
            })
        paths = _bounded_paths(selected, seeds, request.max_depth, request.limit)
        visible_relation_ids = {relation_id for path in paths for relation_id in path["relation_ids"]}
        if request.max_depth == 0:
            visible_relation_ids = set()

        visible = [item for item in selected if item["relation_id"] in visible_relation_ids]
        if request.max_depth > 0 and not has_explicit_node_filter:
            visible = selected[: request.limit]
        visible = visible[: request.limit]
        visible_ids = {item["relation_id"] for item in visible}
        visible_paths = [
            path for path in paths
            if all(relation_id in visible_ids for relation_id in path["relation_ids"])
        ][: request.limit]
        node_ids = set(seeds)
        for item in visible:
            node_ids.update(_endpoints(item))
        for path in visible_paths:
            node_ids.update(path["node_ids"])
        unavailable_visible = [] if has_explicit_node_filter and not seeds else [
            item for item in unavailable
            if not node_ids or any(endpoint in node_ids for endpoint in _endpoints(item))
        ][: request.limit]
        for item in unavailable_visible:
            node_ids.update(_endpoints(item))
        # Every returned relation must have both exact versioned endpoint
        # records present. The relation limit already bounds this set.
        public_nodes = [nodes[node_id] for node_id in sorted(node_ids) if node_id in nodes]

        formal_visible = [item for item in visible if item["relation_class"] == "formal"]
        deterministic_visible = [item for item in visible if item["relation_class"] == "deterministic"]
        candidate_visible = [item for item in visible if item["relation_class"] == "candidate"]
        related_memories = _aggregate_related_memories(nodes, visible_paths, request.limit)
        result_identity_payload = {
            "protocol_version": QUERY_PROTOCOL_VERSION,
            "store_metadata_digest": store["metadata_digest"],
            "query": request.public_dict(),
            "nodes": [_node_result_binding(item) for item in public_nodes],
            "relations": [_relation_result_binding(item) for item in visible],
            "unavailable_relations": [_relation_result_binding(item) for item in unavailable_visible],
            "path_ids": [item["path_id"] for item in visible_paths],
        }
        return {
            "protocol_version": QUERY_PROTOCOL_VERSION,
            "status": "ok",
            "reason_code": "query_complete",
            "readonly": True,
            "executed": False,
            "query": request.public_dict(),
            "query_identity": "query_" + canonical_hash("knowledge_graph_query_result", result_identity_payload),
            "store": {
                "knowledge_schema_version": store["knowledge_schema_version"],
                "knowledge_store_id": store["knowledge_store_id"],
                "knowledge_generation": store["knowledge_generation"],
                "metadata_digest": store["metadata_digest"],
            },
            "availability": {
                "knowledge_sidecar": "available",
                "formal_relations": "available" if store_formal_count else "unavailable",
                "formal_relation_reason": "formal_relations_present" if store_formal_count else "no_formal_relations",
            },
            "counts": {
                "matched_nodes": len(public_nodes),
                "formal_relations": len(formal_visible),
                "deterministic_relations": len(deterministic_visible),
                "candidate_relations": len(candidate_visible),
                "unavailable_relations": len(unavailable_visible),
                "paths": len(visible_paths),
                "related_memories": len(related_memories),
            },
            "nodes": public_nodes,
            "relations": {
                "formal": formal_visible,
                "deterministic": deterministic_visible,
                "candidates": candidate_visible,
                "unavailable": unavailable_visible,
            },
            "paths": visible_paths,
            "related_memories": related_memories,
            "truncated": len(selected) > len(visible) or len(paths) > len(visible_paths),
        }


def query_production_knowledge_graph(query: KnowledgeGraphQuery | None = None) -> dict[str, Any]:
    return KnowledgeGraphQueryService().query(query)


def _validate_store(conn: sqlite3.Connection) -> dict[str, Any]:
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise KnowledgeRepositoryError("Knowledge query connection is not read-only")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise KnowledgeRepositoryError("Knowledge sidecar integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise KnowledgeRepositoryError("Knowledge sidecar foreign keys are invalid")
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES.issubset(tables):
        raise KnowledgeRepositoryError("Knowledge sidecar schema is unavailable")
    if conn.execute("PRAGMA user_version").fetchone()[0] != KNOWLEDGE_SCHEMA_VERSION:
        raise KnowledgeRepositoryError("Knowledge sidecar schema version is unsupported")
    if _schema_fingerprint(conn) != KNOWLEDGE_SCHEMA_FINGERPRINT:
        raise KnowledgeRepositoryError("Knowledge sidecar schema fingerprint mismatch")
    return load_knowledge_store_identity(conn)


def _load_nodes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT node_id,node_schema_version,node_type,source_table,source_id,conversation_id,"
        "summary_version,summary_text_hash,source_snapshot_hash,memory_space FROM graph_nodes ORDER BY node_id"
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        _validate_node_identity(item)
        result[item["node_id"]] = item
    return result


def _validate_node_identity(node: dict[str, Any]) -> None:
    payload = {key: node[key] for key in (
        "node_schema_version", "node_type", "source_table", "source_id", "conversation_id",
        "summary_version", "summary_text_hash", "source_snapshot_hash", "memory_space",
    )}
    if node["node_type"] == "conversation":
        identity = {key: payload[key] for key in (
            "node_schema_version", "node_type", "source_table", "source_id", "conversation_id",
        )}
    else:
        identity = payload
    expected = "node_" + canonical_hash("graph_node", identity)
    if node["node_id"] != expected:
        raise KnowledgeRepositoryError("Knowledge graph node identity mismatch")


def _load_formal_relations(
    conn: sqlite3.Connection,
    store: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    relation_type: str | None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT a.persistence_id,a.knowledge_schema_version,a.relation_version,"
        "a.knowledge_store_id,a.knowledge_generation,a.knowledge_metadata_digest,"
        "a.candidate_id,a.candidate_payload_hash,a.review_plan_id,a.plan_payload_hash,"
        "a.decision_id,a.decision_payload_hash,a.authority_store_id,a.authority_generation,"
        "a.authority_metadata_digest,a.review_chain_id,a.relation_type,a.source_node_id,"
        "a.source_node_payload_hash,a.target_node_id,a.target_node_payload_hash,a.direction,"
        "a.generator_identity,a.generator_config_hash,a.evidence_digest,a.graph_build_identity,"
        "a.score,a.threshold,a.accepted_at,a.persisted_at,a.status,"
        "r.receipt_id,r.knowledge_store_id AS receipt_store_id,"
        "r.knowledge_generation AS receipt_generation,"
        "r.knowledge_metadata_digest AS receipt_metadata_digest,r.transaction_identity,"
        "r.backup_identity,r.authority_mutation_seq,r.authority_mutation_digest,"
        "r.external_anchor_required,r.external_anchor_enabled,r.external_anchor_satisfied,"
        "r.external_anchor_identity,r.external_anchor_assertion_digest,"
        "r.external_anchor_reason_digest,r.production_eligible,r.committed_at "
        "FROM accepted_relation_versions a "
        "LEFT JOIN persistence_receipts r ON r.persistence_id=a.persistence_id "
        "WHERE a.status='active'"
    )
    rows = conn.execute(sql + " ORDER BY a.persistence_id").fetchall()
    result = []
    for row in rows:
        validated_candidate = _validate_formal_persistence(row, store)
        source = nodes.get(row["source_node_id"])
        target = nodes.get(row["target_node_id"])
        if source is None or target is None:
            raise KnowledgeRepositoryError("Formal relation endpoint is missing")
        for node, expected in ((source, row["source_node_payload_hash"]), (target, row["target_node_payload_hash"])):
            if canonical_hash("graph_node_payload", node) != expected:
                raise KnowledgeRepositoryError("Formal relation node payload binding mismatch")
        if relation_type is not None and row["relation_type"] != relation_type:
            continue
        result.append({
            "relation_id": row["persistence_id"],
            "relation_class": "formal",
            "relation_version": row["relation_version"],
            "relation_type": row["relation_type"],
            "source_node_id": row["source_node_id"],
            "source_node_payload_hash": row["source_node_payload_hash"],
            "target_node_id": row["target_node_id"],
            "target_node_payload_hash": row["target_node_payload_hash"],
            "direction": row["direction"],
            "generator_identity": row["generator_identity"],
            "generator_config_hash": row["generator_config_hash"],
            "evidence_identity": row["evidence_digest"],
            "graph_build_identity": row["graph_build_identity"],
            "candidate_id": row["candidate_id"],
            "candidate_payload_hash": row["candidate_payload_hash"],
            "candidate_status": validated_candidate.candidate_status,
            "review_plan_id": row["review_plan_id"],
            "plan_payload_hash": row["plan_payload_hash"],
            "decision_id": row["decision_id"],
            "decision_payload_hash": row["decision_payload_hash"],
            "receipt_id": row["receipt_id"],
            "transaction_identity": row["transaction_identity"],
            "authority_mutation_seq": row["authority_mutation_seq"],
            "authority_mutation_digest": row["authority_mutation_digest"],
            "score": row["score"],
            "threshold": row["threshold"],
            "status": "active",
            "explanation": _explanation("formal", row["relation_type"]),
        })
    return result


def _validate_formal_persistence(row: sqlite3.Row, store: dict[str, Any]) -> RelationCandidate:
    if row["receipt_id"] is None:
        raise KnowledgeRepositoryError("Formal relation persistence receipt is missing")
    store_binding = _store_binding(store)
    relation_store = {
        "knowledge_store_id": row["knowledge_store_id"],
        "knowledge_generation": row["knowledge_generation"],
        "knowledge_metadata_digest": row["knowledge_metadata_digest"],
    }
    receipt_store = {
        "knowledge_store_id": row["receipt_store_id"],
        "knowledge_generation": row["receipt_generation"],
        "knowledge_metadata_digest": row["receipt_metadata_digest"],
    }
    expected_store_subset = {
        key: store_binding[key]
        for key in ("knowledge_store_id", "knowledge_generation", "knowledge_metadata_digest")
    }
    if relation_store != expected_store_subset or receipt_store != expected_store_subset:
        raise KnowledgeRepositoryError("Formal relation store identity mismatch")
    if row["knowledge_schema_version"] != KNOWLEDGE_SCHEMA_VERSION or row["relation_version"] != RELATION_VERSION:
        raise KnowledgeRepositoryError("Formal relation version is unsupported")
    if row["committed_at"] != row["persisted_at"]:
        raise KnowledgeRepositoryError("Formal relation commit receipt mismatch")

    candidates = []
    for candidate_status in sorted(REVIEWABLE_STATUSES):
        candidate = RelationCandidate(
            candidate_schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id=row["candidate_id"],
            relation_type=row["relation_type"],
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
            direction=row["direction"],
            source_node_payload_hash=row["source_node_payload_hash"],
            target_node_payload_hash=row["target_node_payload_hash"],
            generator_identity=row["generator_identity"],
            generator_config_hash=row["generator_config_hash"],
            evidence_digest=row["evidence_digest"],
            evidence_kind="semantic_embedding",
            score=row["score"],
            threshold=row["threshold"],
            rationale_codes=("threshold_qualified_semantic_evidence",),
            candidate_status=candidate_status,
            graph_build_identity=row["graph_build_identity"],
            created_at="",
        )
        try:
            validate_candidate_identity(candidate)
        except ValueError:
            continue
        if candidate.payload_hash == row["candidate_payload_hash"]:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise KnowledgeRepositoryError("Formal relation candidate identity or payload mismatch")
    candidate = candidates[0]

    for value, prefix, label in (
        (row["review_plan_id"], "plan", "review plan ID"),
        (row["decision_id"], "decision", "decision ID"),
        (row["authority_store_id"], "authority", "authority store ID"),
        (row["authority_generation"], "generation", "authority generation"),
        (row["review_chain_id"], "chain", "review chain ID"),
        (row["backup_identity"], "backup", "backup identity"),
    ):
        _require_prefixed(value, prefix, label)
    for value, label in (
        (row["plan_payload_hash"], "plan payload hash"),
        (row["decision_payload_hash"], "decision payload hash"),
        (row["authority_metadata_digest"], "authority metadata digest"),
        (row["authority_mutation_digest"], "authority mutation digest"),
        (row["external_anchor_reason_digest"], "anchor reason digest"),
    ):
        _require_hex(value, label)
    if not isinstance(row["authority_mutation_seq"], int) or row["authority_mutation_seq"] < 1:
        raise KnowledgeRepositoryError("Formal relation authority mutation sequence is invalid")
    if row["external_anchor_required"] != 1 or row["production_eligible"] != 0:
        raise KnowledgeRepositoryError("Formal relation receipt protocol flags are invalid")

    anchor_binding = {
        "anchor_enabled": bool(row["external_anchor_enabled"]),
        "anchor_satisfied": bool(row["external_anchor_satisfied"]),
        "anchor_identity": row["external_anchor_identity"],
        "anchor_assertion_digest": row["external_anchor_assertion_digest"],
        "anchor_reason_digest": row["external_anchor_reason_digest"],
    }
    if anchor_binding["anchor_enabled"]:
        if not anchor_binding["anchor_satisfied"] or not anchor_binding["anchor_identity"] or not anchor_binding["anchor_assertion_digest"]:
            raise KnowledgeRepositoryError("Formal relation anchor receipt is inconsistent")
    elif anchor_binding["anchor_satisfied"] or anchor_binding["anchor_identity"] is not None or anchor_binding["anchor_assertion_digest"] is not None:
        raise KnowledgeRepositoryError("Formal relation disabled anchor receipt is inconsistent")

    persistence_payload = {
        "protocol": PERSISTENCE_PROTOCOL_IDENTITY,
        "candidate_id": row["candidate_id"],
        "candidate_payload_hash": row["candidate_payload_hash"],
        "review_plan_id": row["review_plan_id"],
        "plan_payload_hash": row["plan_payload_hash"],
        "decision_id": row["decision_id"],
        "decision_payload_hash": row["decision_payload_hash"],
        "authority_store_id": row["authority_store_id"],
        "authority_generation": row["authority_generation"],
        "authority_mutation_seq": row["authority_mutation_seq"],
        "authority_mutation_digest": row["authority_mutation_digest"],
        **store_binding,
        **anchor_binding,
    }
    expected_persistence = "persistence_" + canonical_hash(
        "accepted_relation_persistence", persistence_payload,
    )
    if row["persistence_id"] != expected_persistence:
        raise KnowledgeRepositoryError("Formal relation persistence identity mismatch")
    expected_transaction = "transaction_" + canonical_hash(
        "knowledge_persistence_transaction",
        {
            "persistence_id": expected_persistence,
            "backup_identity": row["backup_identity"],
            "authority_mutation_seq": row["authority_mutation_seq"],
            "authority_mutation_digest": row["authority_mutation_digest"],
            **store_binding,
            **anchor_binding,
        },
    )
    if row["transaction_identity"] != expected_transaction:
        raise KnowledgeRepositoryError("Formal relation transaction identity mismatch")
    expected_receipt = "receipt_" + canonical_hash(
        "knowledge_persistence_receipt", {"transaction_identity": expected_transaction},
    )
    if row["receipt_id"] != expected_receipt:
        raise KnowledgeRepositoryError("Formal relation receipt identity mismatch")
    return candidate


def _store_binding(store: dict[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_store_id": store["knowledge_store_id"],
        "knowledge_generation": store["knowledge_generation"],
        "knowledge_initialization_seal": store["initialization_seal"],
        "knowledge_schema_fingerprint": store["schema_fingerprint"],
        "knowledge_metadata_digest": store["metadata_digest"],
    }


def _require_prefixed(value: Any, prefix: str, label: str) -> None:
    if not isinstance(value, str) or not _PREFIXED64.fullmatch(value) or not value.startswith(prefix + "_"):
        raise KnowledgeRepositoryError(f"Formal relation {label} is invalid")


def _require_hex(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise KnowledgeRepositoryError(f"Formal relation {label} is invalid")


def _load_graph_relations(
    conn: sqlite3.Connection, relation_type: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sql = (
        "SELECT edge_id,edge_schema_version,relation_type,source_node_id,target_node_id,direction,"
        "generator_identity,generator_config_hash,evidence_identity,score,threshold,status "
        "FROM graph_edges"
    )
    params: tuple[Any, ...] = ()
    if relation_type is not None:
        sql += " WHERE relation_type=?"
        params = (relation_type,)
    deterministic: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for row in conn.execute(sql + " ORDER BY edge_id", params):
        try:
            verified = make_edge(
                row["relation_type"], row["source_node_id"], row["target_node_id"],
                direction=row["direction"], generator_identity=row["generator_identity"],
                generator_config_hash=row["generator_config_hash"],
                evidence_identity=row["evidence_identity"], status=row["status"],
                score=row["score"], threshold=row["threshold"],
            )
        except ValueError as exc:
            raise KnowledgeRepositoryError("Knowledge graph edge identity is invalid") from exc
        if verified.edge_id != row["edge_id"] or verified.edge_schema_version != row["edge_schema_version"]:
            raise KnowledgeRepositoryError("Knowledge graph edge identity mismatch")
        relation_class = (
            "deterministic" if row["status"] == "deterministic"
            else "unavailable" if row["status"] == "unavailable"
            else "candidate"
        )
        item = {
            "relation_id": row["edge_id"],
            "relation_class": relation_class,
            "edge_schema_version": row["edge_schema_version"],
            "relation_type": row["relation_type"],
            "source_node_id": row["source_node_id"],
            "target_node_id": row["target_node_id"],
            "direction": row["direction"],
            "generator_identity": row["generator_identity"],
            "generator_config_hash": row["generator_config_hash"],
            "evidence_identity": row["evidence_identity"],
            "score": row["score"],
            "threshold": row["threshold"],
            "status": row["status"],
            "explanation": _explanation(relation_class, row["relation_type"]),
        }
        if relation_class == "deterministic":
            deterministic.append(item)
        elif relation_class == "candidate":
            candidates.append(item)
        else:
            unavailable.append(item)
    return deterministic, candidates, unavailable


def _seed_node_ids(nodes: dict[str, dict[str, Any]], query: KnowledgeGraphQuery) -> list[str]:
    result = []
    for node_id, node in nodes.items():
        if query.node_id is not None and node_id != query.node_id:
            continue
        if query.conversation_id is not None and node["conversation_id"] != query.conversation_id:
            continue
        if query.memory_space is not None and node["memory_space"] != query.memory_space:
            continue
        result.append(node_id)
    return sorted(result)[: query.limit]


def _bounded_paths(
    relations: list[dict[str, Any]], seeds: Iterable[str], max_depth: int, limit: int,
) -> list[dict[str, Any]]:
    if max_depth <= 0:
        return []
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for relation in relations:
        source, target = _endpoints(relation)
        adjacency[source].append((target, relation))
        # Query traversal is endpoint-oriented, not a claim that the stored
        # direction is reversible. This lets a conversation or memory-space
        # anchor discover incoming ownership edges while the relation payload
        # continues to report its exact authoritative direction.
        adjacency[target].append((source, relation))
    for values in adjacency.values():
        values.sort(key=lambda pair: (pair[1]["relation_id"], pair[0]))

    paths: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    queue = deque((seed, (seed,), ()) for seed in sorted(set(seeds)))
    while queue and len(paths) < limit:
        current, node_path, relation_path = queue.popleft()
        if len(relation_path) >= max_depth:
            continue
        for next_node, relation in adjacency.get(current, []):
            if next_node in node_path:
                continue
            next_nodes = (*node_path, next_node)
            next_relations = (*relation_path, relation["relation_id"])
            canonical_key = next_relations if next_nodes[0] <= next_nodes[-1] else tuple(reversed(next_relations))
            if canonical_key in seen_paths:
                continue
            seen_paths.add(canonical_key)
            path_id = "path_" + canonical_hash("knowledge_graph_path", {
                "node_ids": list(next_nodes), "relation_ids": list(next_relations),
            })
            paths.append({
                "path_id": path_id,
                "depth": len(next_relations),
                "node_ids": list(next_nodes),
                "relation_ids": list(next_relations),
                "explanation": {
                    "basis_code": "bounded_evidence_path",
                    "summary": "These nodes are connected by the listed versioned relations and evidence identities.",
                    "redacted": True,
                },
            })
            if len(paths) >= limit:
                break
            queue.append((next_node, next_nodes, next_relations))
    return paths


def _aggregate_related_memories(
    nodes: dict[str, dict[str, Any]], paths: list[dict[str, Any]], limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for path in paths:
        for node_id in path["node_ids"]:
            node = nodes.get(node_id)
            if node is None or node["node_type"] != "summary":
                continue
            entry = grouped.setdefault(node_id, {
                "node_id": node_id,
                "node_schema_version": node["node_schema_version"],
                "conversation_id": node["conversation_id"],
                "memory_space": node["memory_space"],
                "summary_version": node["summary_version"],
                "summary_text_hash": node["summary_text_hash"],
                "source_snapshot_hash": node["source_snapshot_hash"],
                "via_path_ids": [],
            })
            entry["via_path_ids"].append(path["path_id"])
    result = []
    for node_id in sorted(grouped):
        item = grouped[node_id]
        item["via_path_ids"] = sorted(set(item["via_path_ids"]))[:limit]
        result.append(item)
    return result[:limit]


def _explanation(relation_class: str, relation_type: str) -> dict[str, Any]:
    if relation_class == "formal":
        summary = "A reviewed semantic candidate was persisted as an active formal relation with bound node versions and evidence."
        basis = "accepted_relation_version"
    elif relation_class == "candidate":
        summary = "Stored evidence produced a review candidate; it is not a formal fact."
        basis = "semantic_candidate"
    elif relation_class == "unavailable":
        summary = "The stored graph marks this relation unavailable; it is not a fact or traversal edge."
        basis = "unavailable_relation"
    else:
        summaries = {
            "belongs_to_conversation": "The summary node is deterministically bound to its conversation identity.",
            "belongs_to_memory_space": "The summary node is deterministically bound to its memory-space identity.",
            "newer_version_of": "Consecutive accepted summary versions create this deterministic version link.",
            "semantic_related": "The stored graph marks this relation deterministic under its generator protocol.",
        }
        summary = summaries[relation_type]
        basis = "deterministic_graph_protocol"
    return {"basis_code": basis, "summary": summary, "redacted": True}


def _unavailable(query: KnowledgeGraphQuery, reason_code: str) -> dict[str, Any]:
    payload = {
        "protocol_version": QUERY_PROTOCOL_VERSION,
        "status": "unavailable",
        "reason_code": reason_code,
        "readonly": True,
        "executed": False,
        "query": query.public_dict(),
        "query_identity": "query_" + canonical_hash("knowledge_graph_query_unavailable", {
            "reason_code": reason_code, "query": query.public_dict(),
        }),
        "store": None,
        "availability": {
            "knowledge_sidecar": "unavailable",
            "formal_relations": "unavailable",
            "formal_relation_reason": reason_code,
        },
        "counts": {
            "matched_nodes": 0, "formal_relations": 0, "deterministic_relations": 0,
            "candidate_relations": 0, "unavailable_relations": 0,
            "paths": 0, "related_memories": 0,
        },
        "nodes": [],
        "relations": {"formal": [], "deterministic": [], "candidates": [], "unavailable": []},
        "paths": [],
        "related_memories": [],
        "truncated": False,
    }
    return payload


def _optional_token(value: Any, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    if len(clean) > max_length or any(ord(char) < 32 for char in clean):
        raise KnowledgeGraphQueryError(f"{label} is invalid")
    return clean


def _endpoints(relation: dict[str, Any]) -> tuple[str, str]:
    return relation["source_node_id"], relation["target_node_id"]


def _relation_sort_key(relation: dict[str, Any]) -> tuple[int, str]:
    rank = {"formal": 0, "deterministic": 1, "candidate": 2}
    return rank[relation["relation_class"]], relation["relation_id"]


def _node_result_binding(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node[key] for key in (
        "node_id", "node_schema_version", "node_type", "source_table", "source_id",
        "conversation_id", "summary_version", "summary_text_hash", "source_snapshot_hash",
        "memory_space",
    )}


def _relation_result_binding(relation: dict[str, Any]) -> dict[str, Any]:
    version = relation.get("relation_version", relation.get("edge_schema_version"))
    return {
        "relation_id": relation["relation_id"],
        "relation_class": relation["relation_class"],
        "relation_version": version,
        "relation_type": relation["relation_type"],
        "source_node_id": relation["source_node_id"],
        "target_node_id": relation["target_node_id"],
        "evidence_identity": relation["evidence_identity"],
        "status": relation["status"],
        "receipt_id": relation.get("receipt_id"),
        "transaction_identity": relation.get("transaction_identity"),
    }
