"""Sealed providers that rebuild relation discovery from fixed read-only sources."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import weakref
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memory.db_path_policy import (
    DEFAULT_MEMORY_SIDECAR_PATH,
    DEFAULT_REAL_DB_PATH,
    DEFAULT_SEMANTIC_SIDECAR_PATH,
    validate_knowledge_test_db_target,
)
from memory.graph_identity import GraphEdge, GraphNode, canonical_hash
from memory.memory_graph import DEFAULT_MIN_SCORE, GraphUnavailable, MemoryGraphBuild, build_memory_graph
from memory.relation_review import CandidateDiscovery, RelationReviewError, discover_relation_candidates
from memory.semantic_memory import load_embedding_records
from memory.summary_repository import graph_summary_sources


TEST_SOURCE_NAME = "current-relation-graph.json"


def _capability_runtime():
    issued = weakref.WeakSet(); authorized: set[Any] = set()
    def issue(kind: str, source_path: Path | None):
        if sys._getframe(1).f_code not in authorized:
            raise RelationReviewError("current discovery providers are factory issued")
        value = object.__new__(CurrentRelationDiscoveryProvider)
        value._CurrentRelationDiscoveryProvider__kind = kind
        value._CurrentRelationDiscoveryProvider__source_path = source_path
        issued.add(value); return value
    def trust(value: Any) -> bool:
        return isinstance(value, CurrentRelationDiscoveryProvider) and value in issued
    def authorize(code: Any) -> None: authorized.add(code)
    return issue, trust, authorize


_issue_provider, is_trusted_current_discovery_provider, _authorize_provider = _capability_runtime()


class CurrentRelationDiscoveryProvider:
    def __init__(self, *_: Any, **__: Any) -> None:
        raise RelationReviewError("current discovery providers are factory issued")

    def rebuild(self) -> CandidateDiscovery:
        if not is_trusted_current_discovery_provider(self):
            raise RelationReviewError("trusted current discovery provider is required")
        if self.__kind == "production":
            summaries = graph_summary_sources(
                DEFAULT_MEMORY_SIDECAR_PATH, DEFAULT_REAL_DB_PATH,
                include_legacy=False,
            )
            embeddings: list[dict[str, Any]] = []
            for source_table in ("memory_summaries", "summary_versions"):
                embeddings.extend(load_embedding_records(
                    DEFAULT_SEMANTIC_SIDECAR_PATH,
                    source_table=source_table, limit=1000,
                ))
            return discover_relation_candidates(
                build_memory_graph(summaries, embeddings, min_score=DEFAULT_MIN_SCORE)
            )
        return discover_relation_candidates(_read_test_build(self.__source_path))


class ProductionCurrentDiscoveryFactory:
    @staticmethod
    def open() -> CurrentRelationDiscoveryProvider:
        return _issue_provider("production", None)


class _TestCurrentDiscoveryFactory:
    @staticmethod
    def initialize(root: str | Path, build: MemoryGraphBuild) -> CurrentRelationDiscoveryProvider:
        path = _validated_test_source(root)
        if path.exists():
            raise RelationReviewError("test current discovery source already exists")
        _write_test_build(path, build, create=True)
        return _TestCurrentDiscoveryFactory.open(root)

    @staticmethod
    def replace(root: str | Path, build: MemoryGraphBuild) -> CurrentRelationDiscoveryProvider:
        path = _validated_test_source(root)
        if not path.is_file():
            raise RelationReviewError("test current discovery source is not initialized")
        _write_test_build(path, build, create=False)
        return _TestCurrentDiscoveryFactory.open(root)

    @staticmethod
    def open(root: str | Path) -> CurrentRelationDiscoveryProvider:
        path = _validated_test_source(root)
        _read_test_build(path)
        return _issue_provider("test", path)


_authorize_provider(ProductionCurrentDiscoveryFactory.open.__code__)
_authorize_provider(_TestCurrentDiscoveryFactory.open.__code__)
del _authorize_provider


def _validated_test_source(root: str | Path) -> Path:
    directory = Path(root).expanduser().absolute()
    temp_root = Path(tempfile.gettempdir()).absolute()
    try: directory.relative_to(temp_root)
    except ValueError as exc: raise RelationReviewError("test discovery source requires the system temporary directory") from exc
    if directory == temp_root or not directory.is_dir():
        raise RelationReviewError("test discovery source root is invalid")
    # Reuse the alias/reparse-safe policy with a synthetic database-shaped path.
    validate_knowledge_test_db_target(directory / "discovery-policy.db")
    return directory / TEST_SOURCE_NAME


def _build_payload(build: MemoryGraphBuild) -> dict[str, Any]:
    value = {
        "schema_version": build.schema_version,
        "generator_config_hash": build.generator_config_hash,
        "nodes": [node.public_dict() for node in build.nodes],
        "edges": [edge.public_dict() for edge in build.edges],
        "unavailable": [item.public_dict() for item in build.unavailable],
    }
    return {"build": value, "content_digest": canonical_hash("test_current_relation_graph", value)}


def _write_test_build(path: Path, build: MemoryGraphBuild, *, create: bool) -> None:
    payload = _build_payload(build)
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if create:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        return
    replacement = path.with_suffix(".replacement")
    descriptor = os.open(replacement, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.replace(replacement, path)
    finally:
        replacement.unlink(missing_ok=True)


def _read_test_build(path: Path | None) -> MemoryGraphBuild:
    if path is None or not path.is_file():
        raise RelationReviewError("test current discovery source is unavailable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = payload.get("build")
    if not isinstance(value, dict) or payload.get("content_digest") != canonical_hash("test_current_relation_graph", value):
        raise RelationReviewError("test current discovery source digest mismatch")
    build = MemoryGraphBuild(
        schema_version=value["schema_version"],
        generator_config_hash=value["generator_config_hash"],
        nodes=tuple(GraphNode(**item) for item in value["nodes"]),
        edges=tuple(GraphEdge(**item) for item in value["edges"]),
        unavailable=tuple(GraphUnavailable(**item) for item in value["unavailable"]),
    )
    discovery = discover_relation_candidates(build)
    for candidate in discovery.candidates:
        from memory.relation_review import validate_candidate_identity
        validate_candidate_identity(candidate)
    return build
