"""Unified read-only keyword, semantic, and knowledge-graph retrieval.

The v6.4 boundary is deliberately retrieval-only: production databases are
opened with SQLite ``mode=ro`` and missing sidecars are reported, never
initialized.  Direct keyword/semantic hits are the only graph seeds.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from memory.db_path_policy import (
    DEFAULT_KNOWLEDGE_SIDECAR_PATH,
    DEFAULT_REAL_DB_PATH,
    DEFAULT_SEMANTIC_SIDECAR_PATH,
)
from memory.knowledge_graph_query import (
    KnowledgeGraphQuery,
    KnowledgeGraphQueryService,
)
from memory.knowledge_sidecar import KnowledgeRepositoryError
from memory.semantic_memory import (
    MEMORY_EMBEDDINGS_TABLE,
    REQUIRED_EMBEDDING_COLUMNS,
    SemanticRetrievalError,
    semantic_summary_search_preview,
)


PROTOCOL_VERSION = "v6.4-retrieval-2"
RESULT_MODES = frozenset({"snippets", "conversation_anchor"})
HIT_TYPES = frozenset({"keyword", "semantic", "graph"})
DIRECT_HIT_TYPES = ("keyword", "semantic")
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
DEFAULT_GRAPH_DEPTH = 1
MAX_GRAPH_DEPTH = 2
DEFAULT_CONTEXT_WINDOW = 0
MAX_CONTEXT_WINDOW = 5
DEFAULT_RELATION_BUDGET = 24
MAX_RELATION_BUDGET = 60
DEFAULT_PATH_BUDGET = 12
MAX_PATH_BUDGET = 30
DEFAULT_CONTEXT_BUDGET = 6000
MAX_CONTEXT_BUDGET = 20000
MAX_DIRECT_CANDIDATES = 100
MAX_RESULTS_PER_CONVERSATION = 3
SEMANTIC_MIN_SCORE = 0.35

RETRIEVAL_STATUS_REASON_CODES = {
    "ok": frozenset({"query_complete"}),
    "unavailable": frozenset({"source_database_missing", "source_database_unreadable"}),
}
AVAILABILITY_STATUS_REASON_CODES = {
    "source_database": {
        "available": frozenset({"readonly_source_opened"}),
        "unavailable": frozenset({"source_database_missing", "source_database_unreadable"}),
    },
    "semantic_sidecar": {
        "available": frozenset({"semantic_query_complete"}),
        "unavailable": frozenset({
            "semantic_sidecar_missing", "semantic_sidecar_corrupt", "semantic_sidecar_invalid",
            "semantic_dependency_unavailable", "semantic_model_unavailable",
            "semantic_time_evidence_unprovable",
        }),
        "not_requested": frozenset({"hit_type_disabled"}),
    },
    "knowledge_sidecar": {
        "available": frozenset({"graph_query_complete"}),
        "unavailable": frozenset({
            "knowledge_sidecar_missing", "knowledge_sidecar_invalid",
            "knowledge_sidecar_unavailable", "knowledge_sidecar_not_initialized",
            "direct_channel_unavailable", "no_direct_seed",
        }),
        "not_requested": frozenset({"hit_type_disabled", "graph_depth_zero"}),
    },
}


class RetrievalValidationError(ValueError):
    """The request cannot be represented by the safe v6.4 contract."""


@dataclass(frozen=True)
class DirectSeedState:
    """Shared proof of which requested direct channels can seed graph retrieval."""

    requested_hit_types: tuple[str, ...]
    capable_hit_types: tuple[str, ...]
    proven_hit_types: tuple[str, ...]

    @property
    def has_legal_seed(self) -> bool:
        return bool(self.proven_hit_types)


@dataclass(frozen=True)
class RetrievalEvidenceState:
    """The evidence fields needed by the shared retrieval-envelope state machine."""

    rank: int
    hit_type: str
    conversation_id: str
    anchor_message_id: str
    seed_conversation_id: str = ""
    seed_anchor_message_id: str = ""
    seed_hit_types: tuple[str, ...] = ()
    path_start_conversation_id: str = ""
    path_start_anchor_message_id: str = ""


def validate_retrieval_protocol_status(status: Any, reason_code: Any) -> None:
    """Validate the sealed v6.4 top-level status/reason pair."""
    if not isinstance(status, str) or not isinstance(reason_code, str):
        raise RetrievalValidationError("retrieval status/reason is outside the v6.4 contract")
    reasons = RETRIEVAL_STATUS_REASON_CODES.get(status)
    if reasons is None or reason_code not in reasons:
        raise RetrievalValidationError("retrieval status/reason is outside the v6.4 contract")


def validate_retrieval_availability_state(component: str, state: Any) -> None:
    """Validate one sealed v6.4 availability component without widening it."""
    if not isinstance(state, dict) or set(state) != {"status", "reason_code"}:
        raise RetrievalValidationError("retrieval availability state is malformed")
    if not isinstance(state["status"], str) or not isinstance(state["reason_code"], str):
        raise RetrievalValidationError("retrieval availability status/reason is outside the v6.4 contract")
    statuses = AVAILABILITY_STATUS_REASON_CODES.get(component)
    reasons = statuses.get(state["status"]) if statuses is not None else None
    if reasons is None or state["reason_code"] not in reasons:
        raise RetrievalValidationError("retrieval availability status/reason is outside the v6.4 contract")


@dataclass(frozen=True)
class RetrievalQuery:
    query: str
    start_time: str | None = None
    end_time: str | None = None
    result_mode: str = "snippets"
    context_window: int = DEFAULT_CONTEXT_WINDOW
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    hit_types: tuple[str, ...] = ("keyword", "semantic", "graph")
    graph_depth: int = DEFAULT_GRAPH_DEPTH
    relation_budget: int = DEFAULT_RELATION_BUDGET
    path_budget: int = DEFAULT_PATH_BUDGET
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    memory_space: str | None = None

    def normalized(self) -> "NormalizedRetrievalQuery":
        text = " ".join(str(self.query or "").split())
        if not text or len(text) > 500 or any(ord(char) < 32 for char in text):
            raise RetrievalValidationError("query must contain 1..500 printable characters")
        mode = str(self.result_mode or "").strip()
        if mode not in RESULT_MODES:
            raise RetrievalValidationError("result_mode must be snippets or conversation_anchor")
        requested_types = tuple(dict.fromkeys(str(item).strip().lower() for item in self.hit_types))
        if not requested_types or any(item not in HIT_TYPES for item in requested_types):
            raise RetrievalValidationError("hit_types contains an unsupported value")
        if "graph" in requested_types and not any(
            item in DIRECT_HIT_TYPES for item in requested_types
        ):
            raise RetrievalValidationError("graph_requires_direct_channel")
        values = {
            "context_window": (self.context_window, 0, MAX_CONTEXT_WINDOW),
            "limit": (self.limit, 1, MAX_LIMIT),
            "graph_depth": (self.graph_depth, 0, MAX_GRAPH_DEPTH),
            "relation_budget": (self.relation_budget, 1, MAX_RELATION_BUDGET),
            "path_budget": (self.path_budget, 1, MAX_PATH_BUDGET),
            "context_budget": (self.context_budget, 0, MAX_CONTEXT_BUDGET),
        }
        clean: dict[str, int] = {}
        for name, (value, minimum, maximum) in values.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise RetrievalValidationError(f"{name} must be an integer") from exc
            if not minimum <= parsed <= maximum:
                raise RetrievalValidationError(f"{name} must be between {minimum} and {maximum}")
            clean[name] = parsed
        start = _parse_boundary(self.start_time, end=False)
        end = _parse_boundary(self.end_time, end=True)
        if start is not None and end is not None and start > end:
            raise RetrievalValidationError("start_time must not be after end_time")
        memory_space = _optional_token(self.memory_space, 128)
        return NormalizedRetrievalQuery(
            query=text, start=start, end=end, result_mode=mode,
            context_window=clean["context_window"], limit=clean["limit"],
            cursor=_optional_token(self.cursor, 512), hit_types=requested_types,
            graph_depth=clean["graph_depth"], relation_budget=clean["relation_budget"],
            path_budget=clean["path_budget"], context_budget=clean["context_budget"],
            memory_space=memory_space,
        )


@dataclass(frozen=True)
class NormalizedRetrievalQuery:
    query: str
    start: datetime | None
    end: datetime | None
    result_mode: str
    context_window: int
    limit: int
    cursor: str | None
    hit_types: tuple[str, ...]
    graph_depth: int
    relation_budget: int
    path_budget: int
    context_budget: int
    memory_space: str | None

    @property
    def start_epoch(self) -> float | None:
        return self.start.timestamp() if self.start else None

    @property
    def end_epoch(self) -> float | None:
        return self.end.timestamp() if self.end else None

    def identity_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "start_time": _iso(self.start), "end_time": _iso(self.end),
            "result_mode": self.result_mode, "context_window": self.context_window,
            "hit_types": list(self.hit_types), "graph_depth": self.graph_depth,
            "relation_budget": self.relation_budget, "path_budget": self.path_budget,
            "context_budget": self.context_budget, "memory_space": self.memory_space,
        }


def expected_retrieval_capabilities(
    request: NormalizedRetrievalQuery,
    availability: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Project the only capability states permitted by the v6.4 envelope."""
    return {
        "keyword": (
            dict(availability["source_database"])
            if "keyword" in request.hit_types else
            {"status": "not_requested", "reason_code": "hit_type_disabled"}
        ),
        "semantic": dict(availability["semantic_sidecar"]),
        "graph": dict(availability["knowledge_sidecar"]),
    }


def derive_direct_seed_state(
    request: NormalizedRetrievalQuery,
    availability: dict[str, dict[str, str]],
    proven_hit_types: list[str] | tuple[str, ...] | set[str],
) -> DirectSeedState:
    """Derive the only direct channels that can legally reach the graph branch."""
    requested = tuple(kind for kind in DIRECT_HIT_TYPES if kind in request.hit_types)
    capable = tuple(
        kind for kind in requested
        if (
            kind == "keyword"
            and availability["source_database"]["status"] == "available"
        ) or (
            kind == "semantic"
            and availability["semantic_sidecar"]["status"] == "available"
        )
    )
    raw_proven = list(proven_hit_types)
    if any(not isinstance(kind, str) or kind not in DIRECT_HIT_TYPES for kind in raw_proven):
        raise RetrievalValidationError("graph direct-seed proof has an invalid hit type")
    proven = tuple(kind for kind in DIRECT_HIT_TYPES if kind in set(raw_proven))
    if not set(proven).issubset(requested):
        raise RetrievalValidationError("graph direct-seed proof was not requested")
    if not set(proven).issubset(capable):
        raise RetrievalValidationError("graph direct-seed proof requires an available capability")
    return DirectSeedState(requested, capable, proven)


def derive_graph_precondition_state(
    request: NormalizedRetrievalQuery,
    direct_seed_state: DirectSeedState,
    *,
    has_direct_seed: bool,
) -> dict[str, str] | None:
    """Derive graph gating before any knowledge-sidecar state is considered."""
    if "graph" not in request.hit_types or request.graph_depth == 0:
        return None
    if not direct_seed_state.capable_hit_types:
        return {"status": "unavailable", "reason_code": "direct_channel_unavailable"}
    if not has_direct_seed:
        return {"status": "unavailable", "reason_code": "no_direct_seed"}
    return None


def project_retrieval_evidence_state(
    rank: Any, hit_type: Any, details: Any, *,
    conversation_id: Any, anchor_message_id: Any,
) -> RetrievalEvidenceState:
    """Project producer or package evidence into the shared envelope state."""
    if (
        not isinstance(conversation_id, str) or not conversation_id
        or not isinstance(anchor_message_id, str) or not anchor_message_id
    ):
        raise RetrievalValidationError("retrieval evidence source identity is malformed")
    if hit_type != "graph":
        return RetrievalEvidenceState(
            rank=rank, hit_type=hit_type,
            conversation_id=conversation_id, anchor_message_id=anchor_message_id,
        )
    if not isinstance(details, dict):
        raise RetrievalValidationError("graph evidence details are malformed")
    seed_conversation_id = details.get("seed_conversation_id")
    seed_anchor_message_id = details.get("seed_anchor_message_id")
    seed_hit_types = details.get("seed_hit_types")
    path_start_conversation_id = details.get("path_start_conversation_id")
    path_start_anchor_message_id = details.get("path_start_anchor_message_id")
    if (
        not isinstance(seed_conversation_id, str) or not seed_conversation_id
        or not isinstance(seed_anchor_message_id, str) or not seed_anchor_message_id
        or not isinstance(seed_hit_types, list) or not seed_hit_types
        or any(not isinstance(kind, str) for kind in seed_hit_types)
        or seed_hit_types != [kind for kind in DIRECT_HIT_TYPES if kind in set(seed_hit_types)]
        or path_start_conversation_id != seed_conversation_id
        or path_start_anchor_message_id != seed_anchor_message_id
    ):
        raise RetrievalValidationError("graph evidence lacks a canonical direct-seed proof")
    return RetrievalEvidenceState(
        rank=rank,
        hit_type=hit_type,
        conversation_id=conversation_id,
        anchor_message_id=anchor_message_id,
        seed_conversation_id=seed_conversation_id,
        seed_anchor_message_id=seed_anchor_message_id,
        seed_hit_types=tuple(seed_hit_types),
        path_start_conversation_id=path_start_conversation_id,
        path_start_anchor_message_id=path_start_anchor_message_id,
    )


def derive_graph_seed_bindings(
    evidence: list[RetrievalEvidenceState] | tuple[RetrievalEvidenceState, ...],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Bind each graph evidence to the complete direct evidence at its seed anchor."""
    direct: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in evidence:
        if item.hit_type in DIRECT_HIT_TYPES:
            direct[(item.conversation_id, item.anchor_message_id)].add(item.hit_type)
    canonical = {
        key: tuple(kind for kind in DIRECT_HIT_TYPES if kind in hit_types)
        for key, hit_types in direct.items()
    }
    for item in evidence:
        if item.hit_type != "graph":
            continue
        seed_key = (item.seed_conversation_id, item.seed_anchor_message_id)
        if seed_key not in canonical:
            raise RetrievalValidationError("graph evidence seed does not match direct evidence")
        if item.seed_hit_types != canonical[seed_key]:
            raise RetrievalValidationError("graph evidence seed hit types are incomplete")
        if (
            item.path_start_conversation_id,
            item.path_start_anchor_message_id,
        ) != seed_key:
            raise RetrievalValidationError("graph path start does not match its direct seed")
    return canonical


def validate_retrieval_envelope_state(
    request: NormalizedRetrievalQuery,
    *,
    status: Any,
    reason_code: Any,
    availability: Any,
    result_count: Any,
    total_ranked: Any,
    next_cursor: Any,
    result_ranks: list[Any] | tuple[Any, ...],
    evidence: list[RetrievalEvidenceState] | tuple[RetrievalEvidenceState, ...],
) -> None:
    """Validate the complete state machine shared by v6.4 producers and v7."""
    validate_retrieval_protocol_status(status, reason_code)
    if not isinstance(availability, dict) or set(availability) != {
        "source_database", "semantic_sidecar", "knowledge_sidecar",
    }:
        raise RetrievalValidationError("retrieval availability is malformed")
    for component, state in availability.items():
        validate_retrieval_availability_state(component, state)
    for name, value in (("result_count", result_count), ("total_ranked", total_ranked)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RetrievalValidationError(f"{name} is invalid")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise RetrievalValidationError("next_cursor is invalid")

    ranks = list(result_ranks)
    if any(isinstance(rank, bool) or not isinstance(rank, int) or rank < 1 for rank in ranks):
        raise RetrievalValidationError("retrieval result rank is invalid")
    unique_ranks = sorted(set(ranks))
    evidence_states = list(evidence)
    if any(
        not isinstance(item, RetrievalEvidenceState)
        or isinstance(item.rank, bool) or not isinstance(item.rank, int) or item.rank < 1
        or item.hit_type not in HIT_TYPES
        for item in evidence_states
    ):
        raise RetrievalValidationError("retrieval evidence state is invalid")
    evidence_ranks = {item.rank for item in evidence_states}
    if evidence_ranks != set(unique_ranks):
        raise RetrievalValidationError("every retrieval result must have evidence")
    if result_count != len(unique_ranks):
        raise RetrievalValidationError("retrieval result_count mismatch")
    if result_count > request.limit:
        raise RetrievalValidationError("retrieval result_count exceeds the page limit")

    identity = _digest(request.identity_payload())
    offset = _decode_cursor(request.cursor, identity)
    expected_ranks = list(range(offset + 1, offset + result_count + 1))
    if unique_ranks != expected_ranks:
        raise RetrievalValidationError("retrieval ranks do not match the current page")
    page_end = offset + result_count
    if result_count == 0:
        if total_ranked > offset:
            raise RetrievalValidationError("empty current page contradicts total_ranked")
    elif page_end > total_ranked:
        raise RetrievalValidationError("current page exceeds total_ranked")
    expected_cursor = _encode_cursor(identity, page_end) if page_end < total_ranked else None
    if next_cursor != expected_cursor:
        raise RetrievalValidationError("retrieval next_cursor mismatch")

    source = availability["source_database"]
    semantic = availability["semantic_sidecar"]
    knowledge = availability["knowledge_sidecar"]
    if source["status"] == "unavailable":
        if status != "unavailable" or reason_code != source["reason_code"]:
            raise RetrievalValidationError("unavailable source must control retrieval status")
        disabled = {"status": "not_requested", "reason_code": "hit_type_disabled"}
        if semantic != disabled or knowledge != disabled:
            raise RetrievalValidationError("source-unavailable retrieval cannot execute sidecars")
        if result_count or total_ranked or evidence_states or next_cursor is not None:
            raise RetrievalValidationError("source-unavailable retrieval cannot contain results")
        return

    if status != "ok" or reason_code != "query_complete":
        raise RetrievalValidationError("available source requires a completed retrieval status")
    if "semantic" not in request.hit_types:
        if semantic != {"status": "not_requested", "reason_code": "hit_type_disabled"}:
            raise RetrievalValidationError("disabled semantic retrieval has an invalid state")
    elif semantic["status"] == "not_requested":
        raise RetrievalValidationError("requested semantic retrieval cannot be not_requested")
    if "graph" not in request.hit_types:
        if knowledge != {"status": "not_requested", "reason_code": "hit_type_disabled"}:
            raise RetrievalValidationError("disabled graph retrieval has an invalid state")
    elif request.graph_depth == 0:
        if knowledge != {"status": "not_requested", "reason_code": "graph_depth_zero"}:
            raise RetrievalValidationError("zero-depth graph retrieval has an invalid state")
    elif knowledge["status"] == "not_requested":
        raise RetrievalValidationError("requested graph retrieval cannot be not_requested")

    requested = set(request.hit_types)
    direct_evidence_types = {
        item.hit_type for item in evidence_states if item.hit_type in DIRECT_HIT_TYPES
    }
    graph_evidence = [item for item in evidence_states if item.hit_type == "graph"]
    derive_graph_seed_bindings(evidence_states)
    seed_proof_types = set(direct_evidence_types)
    for item in graph_evidence:
        proof = derive_direct_seed_state(request, availability, item.seed_hit_types)
        if not proof.has_legal_seed:
            raise RetrievalValidationError("graph evidence lacks a legal direct seed")
        seed_proof_types.update(proof.proven_hit_types)
    seed_state = derive_direct_seed_state(request, availability, seed_proof_types)

    graph_precondition = derive_graph_precondition_state(
        request,
        seed_state,
        has_direct_seed=seed_state.has_legal_seed or total_ranked > 0,
    )
    if graph_precondition is not None:
        if knowledge != graph_precondition:
            raise RetrievalValidationError("graph precondition reason precedence mismatch")
    elif (
        "graph" in request.hit_types
        and request.graph_depth > 0
        and knowledge.get("reason_code") in {"direct_channel_unavailable", "no_direct_seed"}
    ):
        raise RetrievalValidationError("graph precondition reason contradicts a direct seed")

    for item in evidence_states:
        hit_type = item.hit_type
        if hit_type not in requested:
            raise RetrievalValidationError("retrieval evidence type was not requested")
        if hit_type == "semantic" and semantic["status"] != "available":
            raise RetrievalValidationError("semantic evidence requires an available semantic capability")
        if hit_type == "graph" and knowledge["status"] != "available":
            raise RetrievalValidationError("graph evidence requires an available graph capability")

    if knowledge == {"status": "unavailable", "reason_code": "direct_channel_unavailable"}:
        if seed_state.capable_hit_types or result_count or total_ranked or evidence_states:
            raise RetrievalValidationError("direct_channel_unavailable contradicts retrieval evidence")
    elif knowledge == {"status": "unavailable", "reason_code": "no_direct_seed"}:
        if not seed_state.requested_hit_types or not seed_state.capable_hit_types:
            raise RetrievalValidationError("no_direct_seed requires an executed direct channel")
        if result_count or total_ranked or evidence_states or next_cursor is not None:
            raise RetrievalValidationError("no_direct_seed cannot contain retrieval evidence")
    elif knowledge["status"] == "available":
        if not seed_state.capable_hit_types:
            raise RetrievalValidationError("graph query has no reachable direct-seed channel")
        if total_ranked == 0:
            raise RetrievalValidationError("completed graph query requires a direct seed")
        if offset == 0 and not direct_evidence_types:
            raise RetrievalValidationError("first graph page must expose its direct seed")


class GraphEnhancedRetrievalService:
    def __init__(
        self,
        source_db: str | Path = DEFAULT_REAL_DB_PATH,
        semantic_db: str | Path = DEFAULT_SEMANTIC_SIDECAR_PATH,
        knowledge_db: str | Path = DEFAULT_KNOWLEDGE_SIDECAR_PATH,
        *,
        semantic_provider: str = "local_sentence_transformer",
        semantic_searcher: Callable[..., dict[str, Any]] = semantic_summary_search_preview,
        graph_service: Any | None = None,
    ) -> None:
        self.source_db = Path(source_db).expanduser().resolve()
        self.semantic_db = Path(semantic_db).expanduser().resolve()
        self.knowledge_db = Path(knowledge_db).expanduser().resolve()
        self.semantic_provider = semantic_provider
        self.semantic_searcher = semantic_searcher
        self.graph_service = graph_service

    def search(self, query: RetrievalQuery) -> dict[str, Any]:
        request = query.normalized()
        identity = _digest(request.identity_payload())
        offset = _decode_cursor(request.cursor, identity)
        availability = {
            "source_database": {"status": "available", "reason_code": "readonly_source_opened"},
            "semantic_sidecar": {"status": "not_requested", "reason_code": "hit_type_disabled"},
            "knowledge_sidecar": {"status": "not_requested", "reason_code": "hit_type_disabled"},
        }
        if not self.source_db.is_file():
            return _source_unavailable(request, availability, "source_database_missing")

        try:
            conn = sqlite3.connect(f"file:{self.source_db.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            _validate_source_schema(conn)
            direct: list[dict[str, Any]] = []
            if "keyword" in request.hit_types:
                direct.extend(self._keyword_hits(conn, request))
            if "semantic" in request.hit_types:
                semantic_hits, semantic_state = self._semantic_hits(conn, request)
                direct.extend(semantic_hits)
                availability["semantic_sidecar"] = semantic_state
            merged_direct = _merge_hits(direct, request.result_mode)
            direct_seed_state = derive_direct_seed_state(
                request,
                availability,
                {
                    evidence["hit_type"]
                    for item in merged_direct
                    for evidence in item["evidence"]
                    if evidence["hit_type"] in DIRECT_HIT_TYPES
                },
            )
            graph_hits: list[dict[str, Any]] = []
            if "graph" in request.hit_types:
                graph_precondition = derive_graph_precondition_state(
                    request,
                    direct_seed_state,
                    has_direct_seed=direct_seed_state.has_legal_seed,
                )
                if request.graph_depth == 0:
                    availability["knowledge_sidecar"] = {
                        "status": "not_requested", "reason_code": "graph_depth_zero",
                    }
                elif graph_precondition is not None:
                    availability["knowledge_sidecar"] = graph_precondition
                elif self.graph_service is None and not self.knowledge_db.is_file():
                    availability["knowledge_sidecar"] = {
                        "status": "unavailable", "reason_code": "knowledge_sidecar_missing",
                    }
                else:
                    graph_hits, graph_state = self._graph_hits(
                        conn, request, merged_direct, availability,
                    )
                    availability["knowledge_sidecar"] = graph_state
            merged = _merge_hits([*merged_direct, *graph_hits], request.result_mode)
            ranked = _rank_and_diversify(merged, request.result_mode)
            page = ranked[offset: offset + request.limit]
            _attach_graph_seed_proofs(page, merged_direct)
            _attach_context(conn, page, request)
            conn.rollback()
        except sqlite3.Error:
            return _source_unavailable(request, availability, "source_database_unreadable")
        finally:
            if "conn" in locals():
                conn.close()

        next_offset = offset + len(page)
        next_cursor = _encode_cursor(identity, next_offset) if next_offset < len(ranked) else None
        for rank, item in enumerate(page, start=offset + 1):
            item["rank"] = rank
            item["reference_uri"] = (
                f"ayunos://conversation/{item['conversation_id']}?message={item['anchor_message_id']}"
            )
        payload = {
            "protocol_version": PROTOCOL_VERSION, "status": "ok", "reason_code": "query_complete",
            "readonly": True, "executed": False,
            "query": {**request.identity_payload(), "limit": request.limit},
            "availability": availability,
            "result_count": len(page), "total_ranked": len(ranked),
            "results": page, "next_cursor": next_cursor,
            "budgets": {
                "max_results": request.limit, "max_graph_depth": request.graph_depth,
                "max_relations": request.relation_budget, "max_paths": request.path_budget,
                "max_context_chars": request.context_budget,
            },
        }
        _validate_produced_retrieval(payload, request)
        return payload

    def _keyword_hits(self, conn: sqlite3.Connection, request: NormalizedRetrievalQuery) -> list[dict[str, Any]]:
        terms = [term for term in request.query.split() if term]
        text_expr = "COALESCE(m.text,'') || ' ' || COALESCE(m.conversation_title,'')"
        clauses = [f"{text_expr} LIKE ?" for _ in terms]
        params: list[Any] = [f"%{term}%" for term in terms]
        time_sql, time_params = _time_clause(request, "m.create_time")
        space_sql = ""
        if request.memory_space:
            space_sql = " AND COALESCE(NULLIF(m.memory_space,''),'general')=?"
            time_params.append(request.memory_space)
        rows = conn.execute(
            "SELECT m.message_id,m.conversation_id,m.conversation_title,m.role,m.create_time,m.create_time_iso,m.text "
            "FROM messages m WHERE " + " AND ".join(clauses) + time_sql + space_sql +
            " ORDER BY m.create_time DESC,m.conversation_id,m.message_id LIMIT ?",
            (*params, *time_params, MAX_DIRECT_CANDIDATES),
        ).fetchall()
        return [
            _base_hit(
                row, "keyword", 1.0,
                project_retrieval_explanation("keyword", 1.0, {"terms": terms}),
                snippet=project_retrieval_excerpt("keyword", row["text"], request.query),
                evidence={"terms": terms},
            )
            for row in rows
        ]

    def _semantic_hits(
        self, conn: sqlite3.Connection, request: NormalizedRetrievalQuery,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if not self.semantic_db.is_file():
            return [], {"status": "unavailable", "reason_code": "semantic_sidecar_missing"}
        if request.start is not None or request.end is not None:
            structural_failure = _semantic_sidecar_structure_failure(self.semantic_db)
            if structural_failure is not None:
                return [], {"status": "unavailable", "reason_code": structural_failure}
            # Current production embeddings represent whole-conversation
            # summaries and contain no message-time coverage binding.  A
            # bounded query therefore cannot prove that the evidence itself is
            # inside the requested window.  Do not call the provider and do not
            # allow the summary to become a graph seed.
            return [], {
                "status": "unavailable",
                "reason_code": "semantic_time_evidence_unprovable",
            }
        try:
            payload = self.semantic_searcher(
                request.query, self.semantic_db, memory_space=request.memory_space,
                top_k=min(MAX_DIRECT_CANDIDATES, 20), provider_name=self.semantic_provider,
                min_score=SEMANTIC_MIN_SCORE,
            )
        except (SemanticRetrievalError, RuntimeError, ValueError, sqlite3.Error, OSError) as exc:
            return [], {"status": "unavailable", "reason_code": _semantic_failure_reason(exc)}
        if payload.get("readonly_error"):
            return [], {
                "status": "unavailable",
                "reason_code": _semantic_readonly_error_reason(str(payload["readonly_error"])),
            }
        hits = []
        for semantic in payload.get("results", []):
            conversation_id = str(semantic.get("conversation_id") or "").strip()
            if not conversation_id:
                continue
            anchor = _anchor_for_conversation(conn, conversation_id, request)
            if anchor is None:
                continue
            score = float(semantic.get("score") or 0.0)
            if score < SEMANTIC_MIN_SCORE:
                continue
            semantic_details = {
                "anchor_basis": "conversation_latest_in_query_scope",
                "semantic_source_table": semantic.get("source_table"),
                "semantic_source_id": semantic.get("source_id"),
                "semantic_score": round(score, 8),
                "summary_type": semantic.get("summary_type"),
            }
            hits.append(_base_hit(
                anchor, "semantic", score,
                project_retrieval_explanation("semantic", score, semantic_details),
                # Summary previews may cover messages outside a requested time
                # interval. Return only the independently time-checked anchor.
                snippet=project_retrieval_excerpt("semantic", anchor["text"], request.query),
                evidence=semantic_details,
            ))
        return hits, {"status": "available", "reason_code": "semantic_query_complete"}

    def _graph_hits(
        self, conn: sqlite3.Connection, request: NormalizedRetrievalQuery,
        seeds: list[dict[str, Any]], availability: dict[str, dict[str, str]],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if self.graph_service is None and not self.knowledge_db.is_file():
            return [], {"status": "unavailable", "reason_code": "knowledge_sidecar_missing"}
        service = self.graph_service
        if service is None:
            try:
                service = KnowledgeGraphQueryService(self.knowledge_db)
            except (ValueError, OSError):
                return [], {"status": "unavailable", "reason_code": "knowledge_sidecar_invalid"}
        hits: list[dict[str, Any]] = []
        relations_used = 0
        paths_used = 0
        seen_seed_conversations: set[str] = set()
        for seed in seeds:
            seed_key = (seed["conversation_id"], seed["anchor_message_id"])
            seed_direct_evidence = [
                evidence
                for evidence in seed["evidence"]
                if evidence["hit_type"] in DIRECT_HIT_TYPES
                and (
                    evidence["source"]["conversation_id"],
                    evidence["source"]["anchor_message_id"],
                ) == seed_key
            ]
            seed_state = derive_direct_seed_state(
                request,
                availability,
                [evidence["hit_type"] for evidence in seed_direct_evidence],
            )
            if not seed_state.has_legal_seed:
                raise RetrievalValidationError("graph expansion received an invalid direct seed")
            seed_conversation = seed["conversation_id"]
            if seed_conversation in seen_seed_conversations:
                continue
            seen_seed_conversations.add(seed_conversation)
            remaining_relations = request.relation_budget - relations_used
            remaining_paths = request.path_budget - paths_used
            if remaining_relations <= 0 or remaining_paths <= 0:
                break
            try:
                payload = service.query(KnowledgeGraphQuery(
                    conversation_id=seed_conversation, max_depth=request.graph_depth,
                    limit=min(remaining_relations, remaining_paths), include_candidates=False,
                ))
            except (KnowledgeRepositoryError, ValueError, sqlite3.Error, OSError):
                return [], {"status": "unavailable", "reason_code": "knowledge_sidecar_invalid"}
            if payload.get("status") != "ok":
                reason = str(payload.get("reason_code") or "knowledge_sidecar_unavailable")
                return [], {"status": "unavailable", "reason_code": reason}
            nodes = {item["node_id"]: item for item in payload.get("nodes", [])}
            relations = {
                item["relation_id"]: item
                for group in ("formal", "deterministic")
                for item in payload.get("relations", {}).get(group, [])
            }
            for path in payload.get("paths", []):
                if paths_used >= request.path_budget:
                    break
                relation_ids = list(path.get("relation_ids") or [])
                if not relation_ids or len(relation_ids) > request.graph_depth:
                    continue
                path_relations = [relations.get(item) for item in relation_ids]
                if any(item is None for item in path_relations):
                    continue
                path_node_ids = list(path.get("node_ids") or [])
                if not path_node_ids or path_node_ids[0] not in nodes:
                    continue
                path_start_conversation = str(
                    nodes[path_node_ids[0]].get("conversation_id") or ""
                )
                if path_start_conversation != seed_conversation:
                    continue
                path_conversations = {
                    str(nodes[node_id].get("conversation_id") or "")
                    for node_id in path_node_ids if node_id in nodes
                }
                if seed_conversation not in path_conversations:
                    continue
                if relations_used + len(path_relations) > request.relation_budget:
                    continue
                # Candidates and unavailable edges never appear in `relations` above.
                target_conversations = sorted(path_conversations - {"", seed_conversation})
                if not target_conversations:
                    continue
                relation_explanations = [
                    {
                        "relation_id": item["relation_id"],
                        "relation_class": item["relation_class"],
                        "relation_type": item["relation_type"],
                        "evidence_identity": item["evidence_identity"],
                    }
                    for item in path_relations
                ]
                for target in target_conversations:
                    anchor = _anchor_for_conversation(conn, target, request)
                    if anchor is None:
                        continue
                    strongest = min(
                        1.0 if item["relation_class"] == "formal" else 0.7
                        for item in path_relations
                    )
                    graph_score = strongest / (1.0 + 0.35 * (len(path_relations) - 1))
                    graph_details = {
                        "anchor_basis": "conversation_latest_in_query_scope",
                        "seed_conversation_id": seed_conversation,
                        "seed_anchor_message_id": seed["anchor_message_id"],
                        "seed_hit_types": list(seed_state.proven_hit_types),
                        "path_start_conversation_id": path_start_conversation,
                        "path_start_anchor_message_id": seed["anchor_message_id"],
                        "path_id": path.get("path_id"), "depth": len(path_relations),
                        "relations": relation_explanations,
                    }
                    hits.append(_base_hit(
                        anchor, "graph", graph_score,
                        project_retrieval_explanation("graph", graph_score, graph_details),
                        snippet=project_retrieval_excerpt("graph", anchor["text"], request.query),
                        evidence=graph_details,
                    ))
                relations_used += len(path_relations)
                paths_used += 1
        return hits, {"status": "available", "reason_code": "graph_query_complete"}


def search_production(query: RetrievalQuery) -> dict[str, Any]:
    return GraphEnhancedRetrievalService().search(query)


def _validate_source_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("source connection is not query-only")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"conversations", "messages"}.issubset(tables):
        raise sqlite3.DatabaseError("source schema is incomplete")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    required = {"message_id", "conversation_id", "conversation_title", "role", "create_time", "create_time_iso", "text"}
    if not required.issubset(columns):
        raise sqlite3.DatabaseError("message schema is incomplete")


def _parse_boundary(value: str | None, *, end: bool) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        if len(raw) == 10:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
            parsed = datetime.combine(day, time.max if end else time.min, tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise RetrievalValidationError("time values must be ISO-8601 dates or timestamps") from exc
    return parsed


def _time_clause(request: NormalizedRetrievalQuery, expression: str) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if request.start_epoch is not None:
        clauses.append(f"{expression}>=?")
        params.append(request.start_epoch)
    if request.end_epoch is not None:
        clauses.append(f"{expression}<=?")
        params.append(request.end_epoch)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _anchor_for_conversation(
    conn: sqlite3.Connection, conversation_id: str, request: NormalizedRetrievalQuery,
) -> sqlite3.Row | None:
    time_sql, params = _time_clause(request, "m.create_time")
    space_sql = ""
    if request.memory_space:
        space_sql = " AND COALESCE(NULLIF(m.memory_space,''),'general')=?"
        params.append(request.memory_space)
    return conn.execute(
        "SELECT m.message_id,m.conversation_id,m.conversation_title,m.role,m.create_time,m.create_time_iso,m.text "
        "FROM messages m WHERE m.conversation_id=?" + time_sql + space_sql +
        " ORDER BY m.create_time DESC,m.message_id LIMIT 1",
        (conversation_id, *params),
    ).fetchone()


def _base_hit(
    row: sqlite3.Row, hit_type: str, score: float, reason: str, *, snippet: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    source = {
        "conversation_id": str(row["conversation_id"] or ""),
        "anchor_message_id": str(row["message_id"] or ""),
        "time": str(row["create_time_iso"] or ""),
        "timestamp": float(row["create_time"] or 0),
        "title": str(row["conversation_title"] or ""),
        "role": str(row["role"] or ""),
        "snippet": snippet,
    }
    return {
        **source,
        "hit_types": [hit_type],
        "evidence": [{
            "hit_type": hit_type,
            "score": round(float(score), 8),
            "reason": reason,
            "source": source,
            **evidence,
        }],
    }


def _merge_hits(hits: list[dict[str, Any]], result_mode: str) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for hit in hits:
        key = ((hit["conversation_id"],) if result_mode == "conversation_anchor" else
               (hit["conversation_id"], hit["anchor_message_id"]))
        current = merged.get(key)
        if current is None and result_mode == "snippets" and hit["hit_types"] != ["keyword"]:
            # Conversation-level semantic/graph evidence supplements the first
            # directly matched fragment instead of creating a duplicate row for
            # another anchor in the same conversation.
            current = next((
                item for existing_key, item in merged.items()
                if existing_key[0] == hit["conversation_id"]
            ), None)
        if current is None:
            merged[key] = {**hit, "hit_types": list(hit["hit_types"]), "evidence": list(hit["evidence"])}
            continue
        current["hit_types"] = sorted(set(current["hit_types"] + hit["hit_types"]), key=_hit_type_rank)
        known = {_digest(item) for item in current["evidence"]}
        current["evidence"].extend(item for item in hit["evidence"] if _digest(item) not in known)
    return list(merged.values())


def _rank_and_diversify(hits: list[dict[str, Any]], result_mode: str) -> list[dict[str, Any]]:
    for hit in hits:
        hit["evidence"].sort(key=_evidence_sort_key)
        hit["hit_types"] = sorted(
            {item["hit_type"] for item in hit["evidence"]}, key=_hit_type_rank,
        )
        primary = hit["evidence"][0]
        primary_source = primary["source"]
        for field in (
            "conversation_id", "anchor_message_id", "time", "timestamp",
            "title", "role", "snippet",
        ):
            hit[field] = primary_source[field]
        direct = any(item in hit["hit_types"] for item in ("keyword", "semantic"))
        scores = {kind: max((float(ev["score"]) for ev in hit["evidence"] if ev["hit_type"] == kind), default=0.0)
                  for kind in HIT_TYPES}
        tier = 0 if "keyword" in hit["hit_types"] else 1 if "semantic" in hit["hit_types"] else 2
        evidence_score = scores["keyword"] + 0.85 * scores["semantic"] + (0.15 if direct else 0.45) * scores["graph"]
        hit["sorting_basis"] = {
            "tier": tier, "direct_before_graph": direct,
            "evidence_score": round(evidence_score, 8),
            "tie_breakers": ["time_desc", "conversation_id", "anchor_message_id"],
        }
        hit["primary_hit_type"] = primary["hit_type"]
        hit["hit_reason"] = "; ".join(ev["reason"] for ev in hit["evidence"])
    hits.sort(key=lambda item: (
        item["sorting_basis"]["tier"], -item["sorting_basis"]["evidence_score"],
        -item["timestamp"], item["conversation_id"], item["anchor_message_id"],
    ))
    if result_mode == "conversation_anchor":
        return hits
    counts: dict[str, int] = defaultdict(int)
    result = []
    for hit in hits:
        if counts[hit["conversation_id"]] >= MAX_RESULTS_PER_CONVERSATION:
            continue
        counts[hit["conversation_id"]] += 1
        result.append(hit)
    return result


def _attach_graph_seed_proofs(
    page: list[dict[str, Any]], direct_results: list[dict[str, Any]],
) -> None:
    """Keep each paged graph result accompanied by its actual direct seed evidence."""
    available: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for result in direct_results:
        for evidence in result["evidence"]:
            if evidence["hit_type"] not in DIRECT_HIT_TYPES:
                continue
            source = evidence["source"]
            key = (source["conversation_id"], source["anchor_message_id"])
            available[key][evidence["hit_type"]] = evidence

    present: dict[tuple[str, str], set[str]] = defaultdict(set)
    for result in page:
        for evidence in result["evidence"]:
            if evidence["hit_type"] in DIRECT_HIT_TYPES:
                source = evidence["source"]
                present[(source["conversation_id"], source["anchor_message_id"])].add(
                    evidence["hit_type"]
                )

    for result in page:
        changed = False
        for graph_evidence in list(result["evidence"]):
            if graph_evidence["hit_type"] != "graph":
                continue
            seed_key = (
                graph_evidence["seed_conversation_id"],
                graph_evidence["seed_anchor_message_id"],
            )
            required = tuple(graph_evidence["seed_hit_types"])
            support = available.get(seed_key, {})
            if tuple(kind for kind in DIRECT_HIT_TYPES if kind in support) != required:
                raise RetrievalValidationError("graph seed proof is missing from direct retrieval")
            for hit_type in required:
                if hit_type in present[seed_key]:
                    continue
                result["evidence"].append(deepcopy(support[hit_type]))
                present[seed_key].add(hit_type)
                changed = True
        if changed:
            result["evidence"].sort(key=_evidence_sort_key)
            result["hit_types"] = sorted(
                {item["hit_type"] for item in result["evidence"]}, key=_hit_type_rank,
            )
            result["hit_reason"] = "; ".join(
                evidence["reason"] for evidence in result["evidence"]
            )


def _attach_context(conn: sqlite3.Connection, page: list[dict[str, Any]], request: NormalizedRetrievalQuery) -> None:
    remaining = request.context_budget
    for item in page:
        item["result_mode"] = request.result_mode
        item["context"] = []
        if request.result_mode != "snippets" or request.context_window == 0 or remaining <= 0:
            continue
        focus = conn.execute("SELECT ordinal FROM messages WHERE message_id=? AND conversation_id=?",
                             (item["anchor_message_id"], item["conversation_id"])).fetchone()
        if focus is None:
            continue
        time_sql, time_params = _time_clause(request, "create_time")
        space_sql = ""
        if request.memory_space:
            space_sql = " AND COALESCE(NULLIF(memory_space,''),'general')=?"
            time_params.append(request.memory_space)
        rows = conn.execute(
            "SELECT message_id,role,create_time_iso,text FROM messages WHERE conversation_id=? "
            "AND ordinal BETWEEN ? AND ?" + time_sql + space_sql + " ORDER BY ordinal,message_id",
            (item["conversation_id"], int(focus[0]) - request.context_window,
             int(focus[0]) + request.context_window, *time_params),
        ).fetchall()
        for row in rows:
            text = _clip(row["text"] or "", min(400, remaining))
            if not text:
                continue
            remaining -= len(text)
            item["context"].append({
                "message_id": row["message_id"], "role": row["role"],
                "time": row["create_time_iso"], "snippet": text,
                "is_anchor": row["message_id"] == item["anchor_message_id"],
            })
            if remaining <= 0:
                break


def _source_unavailable(request: NormalizedRetrievalQuery, availability: dict[str, Any], reason: str) -> dict[str, Any]:
    availability = {
        "source_database": {"status": "unavailable", "reason_code": reason},
        "semantic_sidecar": {"status": "not_requested", "reason_code": "hit_type_disabled"},
        "knowledge_sidecar": {"status": "not_requested", "reason_code": "hit_type_disabled"},
    }
    payload = {
        "protocol_version": PROTOCOL_VERSION, "status": "unavailable", "reason_code": reason,
        "readonly": True, "executed": False, "query": request.identity_payload(),
        "availability": availability, "result_count": 0, "total_ranked": 0,
        "results": [], "next_cursor": None,
    }
    _validate_produced_retrieval(payload, request)
    return payload


def _validate_produced_retrieval(
    payload: dict[str, Any], request: NormalizedRetrievalQuery,
) -> None:
    result_ranks = [item["rank"] for item in payload["results"]]
    evidence = [
        project_retrieval_evidence_state(
            item["rank"], evidence_item["hit_type"], evidence_item,
            conversation_id=evidence_item["source"]["conversation_id"],
            anchor_message_id=evidence_item["source"]["anchor_message_id"],
        )
        for item in payload["results"]
        for evidence_item in item["evidence"]
    ]
    validate_retrieval_envelope_state(
        request,
        status=payload["status"], reason_code=payload["reason_code"],
        availability=payload["availability"], result_count=payload["result_count"],
        total_ranked=payload["total_ranked"], next_cursor=payload["next_cursor"],
        result_ranks=result_ranks, evidence=evidence,
    )


def project_retrieval_excerpt(hit_type: str, message_text: Any, query: str) -> str:
    """Reproduce the v6.4 message-backed excerpt projection."""
    if hit_type == "keyword":
        return _snippet(str(message_text or ""), query)
    if hit_type in {"semantic", "graph"}:
        return _clip(str(message_text or ""))
    raise RetrievalValidationError("unsupported evidence hit type")


def project_retrieval_explanation(
    hit_type: str, score: float, details: dict[str, Any],
) -> str:
    """Reproduce the deterministic v6.4 evidence explanation."""
    if hit_type == "keyword":
        return "Exact query terms occur in this message or its conversation title."
    if hit_type == "semantic":
        return f"A compatible accepted summary is semantically similar (score={float(score):.4f})."
    if hit_type == "graph":
        seed = str(details.get("seed_conversation_id") or "")
        relations = details.get("relations")
        relation_count = len(relations) if isinstance(relations, list) else 0
        return (
            f"Expanded from seed conversation {seed} through "
            f"{relation_count} verified relation(s)."
        )
    raise RetrievalValidationError("unsupported evidence hit type")


def _snippet(text: str | None, query: str) -> str:
    value = " ".join((text or "").split())
    lower, needle = value.lower(), query.lower()
    position = lower.find(needle)
    if position < 0:
        return _clip(value)
    start = max(0, position - 80)
    return _clip(("..." if start else "") + value[start:position + len(query) + 160])


def _clip(value: str, limit: int = 320) -> str:
    limit = max(0, int(limit))
    if limit == 0:
        return ""
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    if limit <= 3:
        return "." * limit
    return compact[: max(0, limit - 3)] + "..."


def _semantic_failure_reason(exc: BaseException) -> str:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(str(item).lower() for item in chain)
    if any(isinstance(item, (ImportError, ModuleNotFoundError)) for item in chain) or (
        "sentence-transformers" in text or "sentence_transformers" in text
    ):
        return "semantic_dependency_unavailable"
    if (
        "model identity is unavailable" in text
        or "unable to load the configured local embedding model" in text
        or "model cache" in text
        or (
            "model" in text
            and any(token in text for token in ("unavailable", "unable", "load", "unexpected"))
        )
    ):
        return "semantic_model_unavailable"
    if any(isinstance(item, sqlite3.DatabaseError) for item in chain):
        return "semantic_sidecar_corrupt"
    return "semantic_sidecar_invalid"


def _semantic_readonly_error_reason(message: str) -> str:
    text = message.lower()
    if "database is missing" in text:
        return "semantic_sidecar_missing"
    if "malformed" in text or "not a database" in text or "corrupt" in text:
        return "semantic_sidecar_corrupt"
    return "semantic_sidecar_invalid"


def _semantic_sidecar_structure_failure(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return "semantic_sidecar_corrupt"
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (MEMORY_EMBEDDINGS_TABLE,),
            ).fetchone()
            if table is None:
                return "semantic_sidecar_invalid"
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({MEMORY_EMBEDDINGS_TABLE})")
            }
            if not REQUIRED_EMBEDDING_COLUMNS.issubset(columns):
                return "semantic_sidecar_invalid"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return "semantic_sidecar_corrupt"
    except OSError:
        return "semantic_sidecar_invalid"
    return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _encode_cursor(identity: str, offset: int) -> str:
    raw = json.dumps({"query": identity, "offset": offset}, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, identity: str) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode())
        offset = int(payload["offset"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RetrievalValidationError("cursor is invalid") from exc
    if payload.get("query") != identity or offset < 0:
        raise RetrievalValidationError("cursor does not belong to this query")
    return offset


def _optional_token(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    if len(clean) > limit or any(ord(char) < 32 for char in clean):
        raise RetrievalValidationError("request token is invalid")
    return clean


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _hit_type_rank(value: str) -> int:
    return {"keyword": 0, "semantic": 1, "graph": 2}[value]


def _evidence_sort_key(evidence: dict[str, Any]) -> tuple[int, float, float, str, str]:
    source = evidence["source"]
    return (
        _hit_type_rank(evidence["hit_type"]),
        -float(evidence["score"]),
        -float(source["timestamp"]),
        str(source["conversation_id"]),
        str(source["anchor_message_id"]),
    )
