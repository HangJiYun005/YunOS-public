"""Test-only knowledge graph repository and read-only production status checks."""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.db_path_policy import (
    DEFAULT_KNOWLEDGE_SIDECAR_PATH,
    validate_knowledge_test_db_target,
    validate_production_knowledge_sidecar,
)
from memory.graph_identity import canonical_hash
from memory.memory_graph import MemoryGraphBuild


KNOWLEDGE_SCHEMA_VERSION = 3
PRODUCTION_KNOWLEDGE_SIDECAR = DEFAULT_KNOWLEDGE_SIDECAR_PATH
PRODUCTION_KNOWLEDGE_BACKUPS = Path(__file__).resolve().parents[1] / "backups" / "knowledge"
REQUIRED_TABLES = {
    "knowledge_store_metadata", "graph_nodes", "graph_edges", "graph_metadata",
    "accepted_relation_versions", "persistence_receipts",
}

KNOWLEDGE_GRAPH_SCHEMA = """
PRAGMA user_version = 3;
CREATE TABLE knowledge_store_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    knowledge_store_id TEXT NOT NULL UNIQUE,
    knowledge_schema_version INTEGER NOT NULL CHECK(knowledge_schema_version = 3),
    knowledge_generation TEXT NOT NULL UNIQUE,
    initialization_seal TEXT NOT NULL UNIQUE,
    schema_fingerprint TEXT NOT NULL,
    metadata_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE graph_nodes (
    node_id TEXT PRIMARY KEY,
    node_schema_version INTEGER NOT NULL,
    node_type TEXT NOT NULL CHECK(node_type IN ('conversation', 'summary', 'memory_space')),
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    conversation_id TEXT,
    summary_version INTEGER CHECK(summary_version IS NULL OR summary_version >= 1),
    summary_text_hash TEXT,
    source_snapshot_hash TEXT,
    memory_space TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE graph_edges (
    edge_id TEXT PRIMARY KEY,
    edge_schema_version INTEGER NOT NULL,
    relation_type TEXT NOT NULL CHECK(relation_type IN (
        'belongs_to_conversation', 'belongs_to_memory_space',
        'newer_version_of', 'semantic_related'
    )),
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('directed', 'undirected')),
    generator_identity TEXT NOT NULL,
    generator_config_hash TEXT NOT NULL,
    evidence_identity TEXT NOT NULL,
    score REAL,
    threshold REAL,
    status TEXT NOT NULL CHECK(status IN ('deterministic', 'candidate', 'unavailable', 'manual_review')),
    created_at TEXT NOT NULL,
    CHECK(source_node_id <> target_node_id),
    CHECK(direction != 'undirected' OR source_node_id < target_node_id),
    FOREIGN KEY(source_node_id) REFERENCES graph_nodes(node_id),
    FOREIGN KEY(target_node_id) REFERENCES graph_nodes(node_id)
);

CREATE TABLE graph_metadata (
    metadata_id TEXT PRIMARY KEY,
    knowledge_schema_version INTEGER NOT NULL,
    graph_schema_version TEXT NOT NULL,
    build_id TEXT NOT NULL UNIQUE,
    generator_config_hash TEXT NOT NULL,
    node_count INTEGER NOT NULL CHECK(node_count >= 0),
    edge_count INTEGER NOT NULL CHECK(edge_count >= 0),
    unavailable_count INTEGER NOT NULL CHECK(unavailable_count >= 0),
    generated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE accepted_relation_versions (
    persistence_id TEXT PRIMARY KEY,
    knowledge_schema_version INTEGER NOT NULL CHECK(knowledge_schema_version = 3),
    relation_version INTEGER NOT NULL CHECK(relation_version = 1),
    knowledge_store_id TEXT NOT NULL,
    knowledge_generation TEXT NOT NULL REFERENCES knowledge_store_metadata(knowledge_generation),
    knowledge_metadata_digest TEXT NOT NULL REFERENCES knowledge_store_metadata(metadata_digest),
    candidate_id TEXT NOT NULL UNIQUE,
    candidate_payload_hash TEXT NOT NULL,
    review_plan_id TEXT NOT NULL UNIQUE,
    plan_payload_hash TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    decision_payload_hash TEXT NOT NULL,
    authority_store_id TEXT NOT NULL,
    authority_generation TEXT NOT NULL,
    authority_metadata_digest TEXT NOT NULL,
    review_chain_id TEXT NOT NULL UNIQUE,
    relation_type TEXT NOT NULL CHECK(relation_type = 'semantic_related'),
    source_node_id TEXT NOT NULL,
    source_node_payload_hash TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    target_node_payload_hash TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction = 'undirected'),
    generator_identity TEXT NOT NULL,
    generator_config_hash TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    graph_build_identity TEXT NOT NULL,
    score REAL NOT NULL,
    threshold REAL NOT NULL,
    accepted_at TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status = 'active'),
    CHECK(source_node_id < target_node_id),
    UNIQUE(relation_type, source_node_id, target_node_id, status),
    FOREIGN KEY(knowledge_store_id) REFERENCES knowledge_store_metadata(knowledge_store_id),
    FOREIGN KEY(source_node_id) REFERENCES graph_nodes(node_id),
    FOREIGN KEY(target_node_id) REFERENCES graph_nodes(node_id)
);

CREATE TABLE persistence_receipts (
    receipt_id TEXT PRIMARY KEY,
    persistence_id TEXT NOT NULL UNIQUE REFERENCES accepted_relation_versions(persistence_id),
    knowledge_store_id TEXT NOT NULL REFERENCES knowledge_store_metadata(knowledge_store_id),
    knowledge_generation TEXT NOT NULL REFERENCES knowledge_store_metadata(knowledge_generation),
    knowledge_metadata_digest TEXT NOT NULL REFERENCES knowledge_store_metadata(metadata_digest),
    transaction_identity TEXT NOT NULL UNIQUE,
    backup_identity TEXT NOT NULL,
    authority_mutation_seq INTEGER NOT NULL CHECK(authority_mutation_seq >= 1),
    authority_mutation_digest TEXT NOT NULL,
    external_anchor_required INTEGER NOT NULL CHECK(external_anchor_required IN (0, 1)),
    external_anchor_enabled INTEGER NOT NULL CHECK(external_anchor_enabled IN (0, 1)),
    external_anchor_satisfied INTEGER NOT NULL CHECK(external_anchor_satisfied IN (0, 1)),
    external_anchor_identity TEXT,
    external_anchor_assertion_digest TEXT,
    external_anchor_reason_digest TEXT NOT NULL,
    production_eligible INTEGER NOT NULL CHECK(production_eligible IN (0, 1)),
    committed_at TEXT NOT NULL,
    CHECK(external_anchor_satisfied = 0 OR external_anchor_enabled = 1),
    CHECK(external_anchor_enabled = 1 OR (external_anchor_satisfied = 0 AND external_anchor_identity IS NULL AND external_anchor_assertion_digest IS NULL)),
    CHECK(production_eligible = 0 OR (external_anchor_enabled = 1 AND external_anchor_satisfied = 1 AND external_anchor_identity IS NOT NULL AND external_anchor_assertion_digest IS NOT NULL))
);

CREATE INDEX idx_graph_nodes_conversation ON graph_nodes(conversation_id, node_type);
CREATE INDEX idx_graph_nodes_memory_space ON graph_nodes(memory_space, node_type);
CREATE INDEX idx_graph_edges_relation ON graph_edges(relation_type, status);
CREATE INDEX idx_accepted_relations_endpoints ON accepted_relation_versions(
    relation_type, source_node_id, target_node_id, status
);

CREATE TRIGGER graph_nodes_no_update BEFORE UPDATE ON graph_nodes
BEGIN SELECT RAISE(ABORT, 'graph_nodes is append-only'); END;
CREATE TRIGGER graph_nodes_no_delete BEFORE DELETE ON graph_nodes
BEGIN SELECT RAISE(ABORT, 'graph_nodes is append-only'); END;
CREATE TRIGGER graph_edges_no_update BEFORE UPDATE ON graph_edges
BEGIN SELECT RAISE(ABORT, 'graph_edges is append-only'); END;
CREATE TRIGGER graph_edges_no_delete BEFORE DELETE ON graph_edges
BEGIN SELECT RAISE(ABORT, 'graph_edges is append-only'); END;
CREATE TRIGGER graph_metadata_no_update BEFORE UPDATE ON graph_metadata
BEGIN SELECT RAISE(ABORT, 'graph_metadata is append-only'); END;
CREATE TRIGGER graph_metadata_no_delete BEFORE DELETE ON graph_metadata
BEGIN SELECT RAISE(ABORT, 'graph_metadata is append-only'); END;
CREATE TRIGGER accepted_relation_versions_no_update BEFORE UPDATE ON accepted_relation_versions
BEGIN SELECT RAISE(ABORT, 'accepted_relation_versions is append-only'); END;
CREATE TRIGGER accepted_relation_versions_no_delete BEFORE DELETE ON accepted_relation_versions
BEGIN SELECT RAISE(ABORT, 'accepted_relation_versions is append-only'); END;
CREATE TRIGGER persistence_receipts_no_update BEFORE UPDATE ON persistence_receipts
BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END;
CREATE TRIGGER persistence_receipts_no_delete BEFORE DELETE ON persistence_receipts
BEGIN SELECT RAISE(ABORT, 'persistence_receipts is append-only'); END;
CREATE TRIGGER knowledge_store_metadata_no_update BEFORE UPDATE ON knowledge_store_metadata
BEGIN SELECT RAISE(ABORT, 'knowledge_store_metadata is immutable'); END;
CREATE TRIGGER knowledge_store_metadata_no_delete BEFORE DELETE ON knowledge_store_metadata
BEGIN SELECT RAISE(ABORT, 'knowledge_store_metadata is immutable'); END;
"""


def _schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name"
    ).fetchall()
    return canonical_hash("knowledge_sidecar_schema", {
        "objects": [[kind, name, " ".join(sql.split())] for kind, name, sql in rows]
    })


def _expected_schema_fingerprint() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(KNOWLEDGE_GRAPH_SCHEMA)
        return _schema_fingerprint(conn)
    finally:
        conn.close()


KNOWLEDGE_SCHEMA_FINGERPRINT = _expected_schema_fingerprint()


class KnowledgeRepositoryError(RuntimeError):
    """Raised when a graph repository is unavailable or violates its contract."""


def initialize_test_knowledge_sidecar(path: str | Path) -> dict[str, Any]:
    """Create the knowledge schema only for a new system-temporary test file."""
    target = validate_knowledge_test_db_target(path)
    if target.exists():
        return {"status": "refused", "executed": False, "reason": "test knowledge database already exists"}

    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    conn: sqlite3.Connection | None = None
    try:
        validate_knowledge_test_db_target(target)
        conn = sqlite3.connect(target)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(KNOWLEDGE_GRAPH_SCHEMA)
        _insert_store_identity(conn)
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
            conn.close()
            conn = None
        try:
            target.unlink()
        except OSError:
            pass
        raise
    finally:
        if conn is not None:
            conn.close()
    return {"status": "initialized", "executed": True, "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION}


def load_knowledge_store_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT knowledge_store_id,knowledge_schema_version,knowledge_generation,"
        "initialization_seal,schema_fingerprint,metadata_digest,created_at "
        "FROM knowledge_store_metadata WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise KnowledgeRepositoryError("Knowledge store identity is missing")
    keys = ("knowledge_store_id", "knowledge_schema_version", "knowledge_generation",
            "initialization_seal", "schema_fingerprint", "metadata_digest", "created_at")
    identity = dict(zip(keys, row))
    base = {key: identity[key] for key in keys if key not in {"metadata_digest", "created_at"}}
    if identity["knowledge_schema_version"] != KNOWLEDGE_SCHEMA_VERSION:
        raise KnowledgeRepositoryError("Knowledge store schema version mismatch")
    if identity["schema_fingerprint"] != KNOWLEDGE_SCHEMA_FINGERPRINT:
        raise KnowledgeRepositoryError("Knowledge store schema seal mismatch")
    if identity["metadata_digest"] != canonical_hash("knowledge_store_metadata", base):
        raise KnowledgeRepositoryError("Knowledge store metadata digest mismatch")
    return identity


def _insert_store_identity(conn: sqlite3.Connection) -> None:
    base = {
        "knowledge_store_id": "knowledge_" + secrets.token_hex(32),
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "knowledge_generation": "generation_" + secrets.token_hex(32),
        "initialization_seal": "seal_" + secrets.token_hex(32),
        "schema_fingerprint": KNOWLEDGE_SCHEMA_FINGERPRINT,
    }
    digest = canonical_hash("knowledge_store_metadata", base)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO knowledge_store_metadata VALUES(1,?,?,?,?,?,?,?)",
        (*base.values(), digest, created_at),
    )


def production_knowledge_status(
    path: str | Path = PRODUCTION_KNOWLEDGE_SIDECAR,
) -> dict[str, Any]:
    """Inspect the fixed production knowledge sidecar without creating it."""
    target = validate_production_knowledge_sidecar(path)
    base = {"readonly": True, "exists": target.is_file(), "initialized": False}
    if not target.is_file():
        return {**base, "status": "absent", "table_counts": {}}
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        initialized = REQUIRED_TABLES.issubset(tables) and _schema_fingerprint(conn) == KNOWLEDGE_SCHEMA_FINGERPRINT
        if initialized:
            load_knowledge_store_identity(conn)
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in sorted(REQUIRED_TABLES)
        } if initialized else {}
        return {
            **base,
            "initialized": initialized,
            "status": "initialized" if initialized else "schema_unavailable",
            "table_counts": counts,
        }
    except sqlite3.Error as exc:
        raise KnowledgeRepositoryError("Production knowledge sidecar cannot be inspected") from exc
    finally:
        conn.close()


class TestKnowledgeGraphRepository:
    """Append complete graph builds to a validated system-temporary test database."""

    def __init__(self, path: str | Path) -> None:
        self.path = validate_knowledge_test_db_target(path)
        if not self.path.is_file() or not _initialized(self.path):
            raise KnowledgeRepositoryError("Test knowledge database is not initialized")

    def append_build(self, build: MemoryGraphBuild, *, generated_at: str) -> dict[str, Any]:
        build_payload = {
            "graph_schema_version": build.schema_version,
            "generator_config_hash": build.generator_config_hash,
            "node_ids": sorted(node.node_id for node in build.nodes),
            "edge_ids": sorted(edge.edge_id for edge in build.edges),
            "unavailable_evidence": sorted(item.evidence_identity for item in build.unavailable),
        }
        build_id = f"build_{canonical_hash('knowledge_graph_build', build_payload)}"
        metadata_id = f"metadata_{canonical_hash('knowledge_graph_metadata', {'build_id': build_id})}"
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            _require_schema(conn)
            conn.executemany(
                """
                INSERT INTO graph_nodes (
                    node_id, node_schema_version, node_type, source_table, source_id,
                    conversation_id, summary_version, summary_text_hash,
                    source_snapshot_hash, memory_space, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO NOTHING
                """,
                [
                    (
                        node.node_id, node.node_schema_version, node.node_type,
                        node.source_table, node.source_id, node.conversation_id,
                        node.summary_version, node.summary_text_hash,
                        node.source_snapshot_hash, node.memory_space, generated_at,
                    )
                    for node in build.nodes
                ],
            )
            conn.executemany(
                """
                INSERT INTO graph_edges (
                    edge_id, edge_schema_version, relation_type, source_node_id,
                    target_node_id, direction, generator_identity,
                    generator_config_hash, evidence_identity, score, threshold,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO NOTHING
                """,
                [
                    (
                        edge.edge_id, edge.edge_schema_version, edge.relation_type,
                        edge.source_node_id, edge.target_node_id, edge.direction,
                        edge.generator_identity, edge.generator_config_hash,
                        edge.evidence_identity, edge.score, edge.threshold,
                        edge.status, generated_at,
                    )
                    for edge in build.edges
                ],
            )
            _verify_build_identities(conn, build)
            conn.execute(
                """
                INSERT INTO graph_metadata (
                    metadata_id, knowledge_schema_version, graph_schema_version,
                    build_id, generator_config_hash, node_count, edge_count,
                    unavailable_count, generated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata_id, KNOWLEDGE_SCHEMA_VERSION, build.schema_version,
                    build_id, build.generator_config_hash, len(build.nodes),
                    len(build.edges), len(build.unavailable), generated_at, generated_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "status": "appended",
            "build_id": build_id,
            "node_count": len(build.nodes),
            "edge_count": len(build.edges),
            "unavailable_count": len(build.unavailable),
        }


def _initialized(path: Path) -> bool:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        initialized = REQUIRED_TABLES.issubset(tables) and _schema_fingerprint(conn) == KNOWLEDGE_SCHEMA_FINGERPRINT
        if initialized:
            load_knowledge_store_identity(conn)
        return initialized
    finally:
        conn.close()


def _require_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not REQUIRED_TABLES.issubset(tables):
        raise KnowledgeRepositoryError("Test knowledge schema is incomplete")
    if _schema_fingerprint(conn) != KNOWLEDGE_SCHEMA_FINGERPRINT:
        raise KnowledgeRepositoryError("Test knowledge schema fingerprint mismatch")
    if conn.execute("PRAGMA user_version").fetchone()[0] != KNOWLEDGE_SCHEMA_VERSION:
        raise KnowledgeRepositoryError("Test knowledge schema version mismatch")
    load_knowledge_store_identity(conn)


def _verify_build_identities(conn: sqlite3.Connection, build: MemoryGraphBuild) -> None:
    for node in build.nodes:
        stored = conn.execute(
            """
            SELECT node_schema_version, node_type, source_table, source_id,
                   conversation_id, summary_version, summary_text_hash,
                   source_snapshot_hash, memory_space
            FROM graph_nodes WHERE node_id = ?
            """,
            (node.node_id,),
        ).fetchone()
        expected = (
            node.node_schema_version, node.node_type, node.source_table,
            node.source_id, node.conversation_id, node.summary_version,
            node.summary_text_hash, node.source_snapshot_hash, node.memory_space,
        )
        if stored != expected:
            raise KnowledgeRepositoryError("Graph node identity collision")
    for edge in build.edges:
        stored = conn.execute(
            """
            SELECT edge_schema_version, relation_type, source_node_id,
                   target_node_id, direction, generator_identity,
                   generator_config_hash, evidence_identity, score, threshold,
                   status
            FROM graph_edges WHERE edge_id = ?
            """,
            (edge.edge_id,),
        ).fetchone()
        expected = (
            edge.edge_schema_version, edge.relation_type, edge.source_node_id,
            edge.target_node_id, edge.direction, edge.generator_identity,
            edge.generator_config_hash, edge.evidence_identity, edge.score,
            edge.threshold, edge.status,
        )
        if stored != expected:
            raise KnowledgeRepositoryError("Graph edge identity collision")
