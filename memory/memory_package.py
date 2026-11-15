"""YunOS v7.0 deterministic, model-independent memory package contract.

The package layer is read-only.  It consumes the sealed v6.4 retrieval result,
preserves every evidence source binding, and performs no classification or
persistence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from memory.db_path_policy import DEFAULT_REAL_DB_PATH
from memory.graph_enhanced_retrieval import (
    GraphEnhancedRetrievalService,
    DIRECT_HIT_TYPES,
    NormalizedRetrievalQuery,
    RetrievalValidationError,
    PROTOCOL_VERSION as RETRIEVAL_PROTOCOL_VERSION,
    RetrievalQuery,
    expected_retrieval_capabilities,
    project_retrieval_evidence_state,
    project_retrieval_excerpt,
    project_retrieval_explanation,
    validate_retrieval_availability_state,
    validate_retrieval_envelope_state,
    validate_retrieval_protocol_status,
)


PROTOCOL_VERSION = "v7-memory-package-1"
CLASSIFICATION_KEYS = (
    "facts", "preferences", "decisions", "constraints", "open_questions",
    "historical_states", "conflicts", "unclassified_evidence",
)
HIT_TYPES = ("keyword", "semantic", "graph")
PACKAGE_STATUSES = {"ok", "partial", "empty", "unavailable"}
ABSOLUTE_PATH_REPLACEMENT = "[absolute-path]"
LITERAL_REDACTION_MARKER = "[literal-absolute-path-marker]"
_PATH_OPENERS = {"(": ")", "[": "]", "{": "}"}
_PATH_CLOSERS = frozenset(_PATH_OPENERS.values())
_PATH_SENTENCE_TERMINATORS = frozenset(".,;:!?，。；：！？…")
_PATH_HARD_STOPS = frozenset("\"'`|<>")


class MemoryPackageValidationError(ValueError):
    """The package is malformed, inconsistent, or has an invalid source binding."""


class _PrivacyRedactor:
    def __init__(self) -> None:
        self.count = 0

    def text(self, value: str) -> str:
        clean = value.replace(ABSOLUTE_PATH_REPLACEMENT, LITERAL_REDACTION_MARKER)
        spans = _absolute_path_spans(clean)
        if not spans:
            return clean
        pieces = []
        cursor = 0
        for start, end in spans:
            pieces.extend((clean[cursor:start], ABSOLUTE_PATH_REPLACEMENT))
            self.count += 1
            cursor = end
        pieces.append(clean[cursor:])
        return "".join(pieces)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        return deepcopy(value)

class MemoryPackageService:
    """Build packages from v6.4 without accepting persistence destinations."""

    def __init__(
        self,
        source_db: str | Path = DEFAULT_REAL_DB_PATH,
        *,
        retriever: Any | None = None,
    ) -> None:
        self.source_db = Path(source_db).expanduser().resolve()
        self.retriever = retriever or GraphEnhancedRetrievalService(source_db=self.source_db)

    def build(
        self,
        query: RetrievalQuery,
        *,
        future_token_budget: int | None = None,
    ) -> dict[str, Any]:
        if future_token_budget is not None:
            if isinstance(future_token_budget, bool) or not isinstance(future_token_budget, int):
                raise MemoryPackageValidationError("future_token_budget must be an integer or null")
            if not 1 <= future_token_budget <= 1_000_000:
                raise MemoryPackageValidationError("future_token_budget must be between 1 and 1000000")
        normalized = query.normalized()
        if _contains_absolute_path(normalized.query):
            raise MemoryPackageValidationError("query violates the memory package privacy contract")
        retrieval = self.retriever.search(query)
        package = _build_from_retrieval(
            retrieval, normalized_request=normalized,
            future_token_budget=future_token_budget,
        )
        validate_memory_package_source_backed(package, trusted_source_db=self.source_db)
        return package


def build_production_memory_package(
    query: RetrievalQuery,
    *,
    future_token_budget: int | None = None,
) -> dict[str, Any]:
    return MemoryPackageService().build(query, future_token_budget=future_token_budget)


def canonical_json(package: dict[str, Any]) -> str:
    """Return the one canonical UTF-8 JSON representation after strict validation."""
    validate_memory_package_standalone(package)
    return _canonical_dump(package)


def render_memory_package(package: dict[str, Any]) -> str:
    """Render a deterministic, inference-free human preview of the JSON package."""
    validate_memory_package_standalone(package)
    request = package["request"]
    scope = request["scope"]
    time_range = request["time"]
    lines = [
        f"YunOS Memory Package {package['protocol_version']}",
        f"package_identity: {package['package_identity']}",
        f"status: {package['status']} ({package['reason_code']})",
        f"degradation: {package['degradation']['status']}",
        f"readonly: true; persisted: false",
        "privacy: "
        f"absolute_path_redaction_applied={_text_value(package['privacy']['absolute_path_redaction_applied'])}; "
        f"redaction_count={package['privacy']['redaction_count']}; "
        f"replacement={package['privacy']['replacement']}",
        f"query: {request['task_or_query']}",
        "scope: "
        f"mode={scope['result_mode']}; memory_space={_text_value(scope['memory_space'])}; "
        f"hit_types={','.join(scope['hit_types'])}",
        "time: "
        f"start={_text_value(time_range['start_time'])}; end={_text_value(time_range['end_time'])}",
        "capabilities:",
    ]
    for name in HIT_TYPES:
        state = package["capabilities"][name]
        lines.append(f"- {name}: {state['status']} ({state['reason_code']})")
    lines.append("missing_capabilities: " + (
        ",".join(package["missing_capabilities"]) or "none"
    ))
    lines.append("warnings:")
    if package["warnings"]:
        for warning in package["warnings"]:
            lines.append(f"- {warning['code']}: {warning['message']}")
    else:
        lines.append("- none")
    lines.append(f"evidence ({len(package['evidence'])}):")
    if not package["evidence"]:
        lines.append("- none")
    for item in package["evidence"]:
        conversation = item["source"]["conversation"]
        anchor = item["source"]["message_anchor"]
        lines.extend([
            f"- {item['evidence_id']} [{item['hit']['type']}] rank={item['result_rank']}",
            f"  conversation: {conversation['conversation_id']} | {conversation['title']}",
            "  message_anchor: "
            f"{anchor['message_id']} | {anchor['role']} | {anchor['time']}",
            f"  score: {_text_value(item['hit']['score'])}",
            f"  explanation: {item['explanation']['text']}",
            f"  excerpt: {item['excerpt']}",
            "  classification: unclassified_evidence",
        ])
    lines.append("classifications:")
    for key in CLASSIFICATION_KEYS:
        values = package["classifications"][key]
        lines.append(f"- {key}: {','.join(values) if values else 'none'}")
    size = package["size"]
    lines.append(
        "size: "
        f"canonical_json_chars={size['canonical_json_chars']}; "
        f"evidence_excerpt_chars={size['evidence_excerpt_chars']}; "
        f"future_token_budget={_text_value(size['future_token_budget'])}; "
        "token_selection_applied=false"
    )
    return "\n".join(lines) + "\n"


def validate_memory_package_standalone(package: dict[str, Any]) -> None:
    """Validate schema, derivations, and internal consistency without authenticity claims."""
    if not isinstance(package, dict):
        raise MemoryPackageValidationError("package must be an object")
    _exact_keys(package, {
        "protocol_version", "package_identity", "status", "reason_code", "readonly",
        "persisted", "request", "retrieval", "capabilities", "degradation",
        "warnings", "missing_capabilities", "evidence", "classifications", "privacy", "size",
    }, "package")
    if package["protocol_version"] != PROTOCOL_VERSION:
        raise MemoryPackageValidationError("memory package protocol version mismatch")
    if package["status"] not in PACKAGE_STATUSES or not _token(package["reason_code"]):
        raise MemoryPackageValidationError("invalid package status")
    if package["readonly"] is not True or package["persisted"] is not False:
        raise MemoryPackageValidationError("memory package must be readonly and non-persisted")
    normalized_request = _validate_request(package["request"])
    _validate_retrieval(package["retrieval"])
    _validate_capabilities(package["capabilities"], package["request"])
    _validate_degradation(package["degradation"])
    _validate_warnings(package["warnings"])
    if not isinstance(package["missing_capabilities"], list) or any(
        item not in HIT_TYPES for item in package["missing_capabilities"]
    ) or package["missing_capabilities"] != sorted(set(package["missing_capabilities"])):
        raise MemoryPackageValidationError("invalid missing_capabilities")
    _validate_evidence(package["evidence"])
    _validate_classifications(package["classifications"], package["evidence"])
    _validate_privacy(package["privacy"], package)
    _validate_cross_fields(package, normalized_request)
    expected_identity = "mp_" + _digest(_identity_payload(package))
    if package["package_identity"] != expected_identity:
        raise MemoryPackageValidationError("package identity mismatch")
    _validate_size(package)
    _reject_absolute_paths(package)


def validate_memory_package_source_backed(
    package: dict[str, Any], *, trusted_source_db: str | Path,
) -> None:
    """Validate internal consistency and rebuild DB-backed source projections."""
    validate_memory_package_standalone(package)
    if package["evidence"]:
        _verify_source_backed_package(
            package, Path(trusted_source_db).expanduser().resolve(),
        )


def _build_from_retrieval(
    retrieval: dict[str, Any],
    *,
    normalized_request: NormalizedRetrievalQuery,
    future_token_budget: int | None,
) -> dict[str, Any]:
    normalized_query = {
        **normalized_request.identity_payload(),
        "limit": normalized_request.limit,
        "cursor": normalized_request.cursor,
    }
    for key, value in retrieval["query"].items():
        if key in normalized_query and normalized_query[key] != value:
            raise MemoryPackageValidationError("v6.4 query binding mismatch")
    _validate_v64_retrieval(retrieval, normalized_request)
    query = normalized_query
    if _contains_absolute_path(query["query"]):
        raise MemoryPackageValidationError("query violates the memory package privacy contract")
    redactor = _PrivacyRedactor()
    availability = deepcopy(retrieval["availability"])
    source_state = availability["source_database"]
    capabilities = expected_retrieval_capabilities(normalized_request, availability)
    missing = sorted(name for name, state in capabilities.items() if state["status"] == "unavailable")
    evidence = []
    for result in retrieval["results"]:
        for raw in result["evidence"]:
            source = raw["source"]
            if any(_contains_absolute_path(str(source[key])) for key in (
                "conversation_id", "anchor_message_id", "role", "time",
            )):
                raise MemoryPackageValidationError("source identity violates the memory package privacy contract")
            item = {
                "evidence_id": "",
                "classification": "unclassified_evidence",
                "result_rank": result["rank"],
                "source": {
                    "conversation": {
                        "conversation_id": source["conversation_id"],
                        "title": redactor.text(source["title"]),
                    },
                    "message_anchor": {
                        "message_id": source["anchor_message_id"],
                        "role": source["role"],
                        "time": source["time"],
                        "timestamp": source["timestamp"],
                    },
                    "reference_uri": (
                        f"ayunos://conversation/{source['conversation_id']}"
                        f"?message={source['anchor_message_id']}"
                    ),
                },
                "hit": {"type": raw["hit_type"], "score": raw["score"]},
                "explanation": {
                    "basis_code": raw["hit_type"] + "_retrieval_evidence",
                    "text": redactor.text(raw["reason"]),
                },
                "excerpt": redactor.text(source["snippet"]),
                "details": redactor.value(_safe_evidence_details(raw)),
            }
            item["evidence_id"] = "ev_" + _digest({key: value for key, value in item.items() if key != "evidence_id"})
            evidence.append(item)
    evidence.sort(key=lambda item: (item["result_rank"], _hit_rank(item["hit"]["type"]), item["evidence_id"]))
    unclassified = [item["evidence_id"] for item in evidence]
    classifications = {key: [] for key in CLASSIFICATION_KEYS}
    classifications["unclassified_evidence"] = unclassified
    if source_state["status"] == "unavailable":
        status, reason = "unavailable", retrieval["reason_code"]
    elif not evidence:
        status, reason = "empty", "no_evidence"
    elif missing:
        status, reason = "partial", "partial_capability_result"
    else:
        status, reason = "ok", "package_complete"
    degradation_status = (
        "unavailable" if status == "unavailable" else "degraded" if missing else "complete"
    )
    warnings = [
        {
            "code": f"{name}_unavailable",
            "message": f"{name} capability unavailable: {capabilities[name]['reason_code']}",
        }
        for name in missing
    ]
    package = {
        "protocol_version": PROTOCOL_VERSION,
        "package_identity": "",
        "status": status,
        "reason_code": reason,
        "readonly": True,
        "persisted": False,
        "request": {
            "task_or_query": redactor.text(query["query"]),
            "scope": {
                "memory_space": query["memory_space"],
                "result_mode": query["result_mode"],
                "hit_types": list(query["hit_types"]),
            },
            "time": {"start_time": query["start_time"], "end_time": query["end_time"]},
            "retrieval_parameters": {
                "context_window": query["context_window"],
                "limit": query["limit"],
                "cursor": query["cursor"],
                "graph_depth": query["graph_depth"],
                "relation_budget": query["relation_budget"],
                "path_budget": query["path_budget"],
                "context_budget": query["context_budget"],
            },
        },
        "retrieval": {
            "protocol_version": retrieval["protocol_version"],
            "status": retrieval["status"],
            "reason_code": retrieval["reason_code"],
            "result_count": retrieval["result_count"],
            "total_ranked": retrieval["total_ranked"],
            "next_cursor": retrieval["next_cursor"],
            "availability": availability,
        },
        "capabilities": capabilities,
        "degradation": {"status": degradation_status, "reason_codes": sorted(
            capabilities[name]["reason_code"] for name in missing
        )},
        "warnings": warnings,
        "missing_capabilities": missing,
        "evidence": evidence,
        "classifications": classifications,
        "privacy": {
            "absolute_path_redaction_applied": redactor.count > 0,
            "redaction_count": redactor.count,
            "replacement": ABSOLUTE_PATH_REPLACEMENT,
        },
        "size": {
            "canonical_json_chars": 0,
            "evidence_excerpt_chars": sum(len(item["excerpt"]) for item in evidence),
            "future_token_budget": future_token_budget,
            "token_selection_applied": False,
        },
    }
    package["package_identity"] = "mp_" + _digest(_identity_payload(package))
    package["size"]["canonical_json_chars"] = _fixed_canonical_size(package)
    return package


def _validate_v64_retrieval(
    payload: Any, normalized_request: NormalizedRetrievalQuery,
) -> None:
    if not isinstance(payload, dict) or payload.get("protocol_version") != RETRIEVAL_PROTOCOL_VERSION:
        raise MemoryPackageValidationError("v6.4 retrieval protocol mismatch")
    required = {"status", "reason_code", "readonly", "executed", "query", "availability",
                "result_count", "total_ranked", "results", "next_cursor"}
    if not required.issubset(payload) or payload["readonly"] is not True or payload["executed"] is not False:
        raise MemoryPackageValidationError("invalid v6.4 retrieval envelope")
    try:
        validate_retrieval_protocol_status(payload["status"], payload["reason_code"])
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError("invalid v6.4 retrieval status") from exc
    if not isinstance(payload["query"], dict) or not isinstance(payload["results"], list):
        raise MemoryPackageValidationError("invalid v6.4 retrieval content")
    query_required = {"query", "start_time", "end_time", "result_mode", "context_window",
                      "hit_types", "graph_depth", "relation_budget", "path_budget",
                      "context_budget", "memory_space"}
    if not query_required.issubset(payload["query"]):
        raise MemoryPackageValidationError("incomplete v6.4 query")
    requested_hit_types = payload["query"]["hit_types"]
    if (
        not isinstance(requested_hit_types, list)
        or not requested_hit_types
        or any(item not in HIT_TYPES for item in requested_hit_types)
    ):
        raise MemoryPackageValidationError("invalid v6.4 query hit types")
    availability = payload["availability"]
    if not isinstance(availability, dict) or set(availability) != {
        "source_database", "semantic_sidecar", "knowledge_sidecar",
    }:
        raise MemoryPackageValidationError("invalid v6.4 availability")
    for component, state in availability.items():
        _validate_shared_availability(component, state)
    if payload["result_count"] != len(payload["results"]):
        raise MemoryPackageValidationError("v6.4 result count mismatch")
    result_ranks = []
    evidence_states = []
    for result in payload["results"]:
        if not isinstance(result, dict) or not isinstance(result.get("evidence"), list):
            raise MemoryPackageValidationError("invalid v6.4 result")
        if not isinstance(result.get("hit_types"), list):
            raise MemoryPackageValidationError("invalid v6.4 result hit types")
        if not isinstance(result.get("rank"), int) or result["rank"] < 1:
            raise MemoryPackageValidationError("invalid v6.4 result rank")
        result_ranks.append(result["rank"])
        for evidence in result["evidence"]:
            if not isinstance(evidence, dict) or evidence.get("hit_type") not in HIT_TYPES:
                raise MemoryPackageValidationError("invalid v6.4 evidence")
            if (
                evidence["hit_type"] not in requested_hit_types
                or evidence["hit_type"] not in result["hit_types"]
            ):
                raise MemoryPackageValidationError("v6.4 evidence hit type is not request-bound")
            if not isinstance(evidence.get("reason"), str) or not evidence["reason"]:
                raise MemoryPackageValidationError("invalid v6.4 evidence explanation")
            source = evidence.get("source")
            if not isinstance(source, dict) or any(key not in source for key in (
                "conversation_id", "anchor_message_id", "time", "timestamp", "title", "role", "snippet",
            )):
                raise MemoryPackageValidationError("incomplete v6.4 evidence source")
            if not all(isinstance(source[key], str) for key in (
                "conversation_id", "anchor_message_id", "time", "title", "role", "snippet",
            )) or not source["conversation_id"] or not source["anchor_message_id"]:
                raise MemoryPackageValidationError("invalid v6.4 evidence source")
            try:
                evidence_states.append(project_retrieval_evidence_state(
                    result["rank"], evidence["hit_type"], evidence,
                    conversation_id=source["conversation_id"],
                    anchor_message_id=source["anchor_message_id"],
                ))
            except RetrievalValidationError as exc:
                raise MemoryPackageValidationError("invalid v6.4 graph seed proof") from exc
    try:
        validate_retrieval_envelope_state(
            normalized_request,
            status=payload["status"], reason_code=payload["reason_code"],
            availability=payload["availability"], result_count=payload["result_count"],
            total_ranked=payload["total_ranked"], next_cursor=payload["next_cursor"],
            result_ranks=result_ranks, evidence=evidence_states,
        )
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError("invalid v6.4 retrieval envelope state") from exc


def _validate_request(request: Any) -> NormalizedRetrievalQuery:
    if not isinstance(request, dict):
        raise MemoryPackageValidationError("request must be an object")
    _exact_keys(request, {"task_or_query", "scope", "time", "retrieval_parameters"}, "request")
    if not _token(request["task_or_query"]):
        raise MemoryPackageValidationError("invalid task_or_query")
    _exact_keys(request["scope"], {"memory_space", "result_mode", "hit_types"}, "scope")
    if request["scope"]["memory_space"] is not None and not _token(request["scope"]["memory_space"]):
        raise MemoryPackageValidationError("invalid memory_space")
    if request["scope"]["result_mode"] not in {"snippets", "conversation_anchor"}:
        raise MemoryPackageValidationError("invalid result_mode")
    hit_types = request["scope"]["hit_types"]
    if not isinstance(hit_types, list) or not hit_types or any(item not in HIT_TYPES for item in hit_types):
        raise MemoryPackageValidationError("invalid hit_types")
    _exact_keys(request["time"], {"start_time", "end_time"}, "time")
    if any(value is not None and not isinstance(value, str) for value in request["time"].values()):
        raise MemoryPackageValidationError("invalid time range")
    parameters = request["retrieval_parameters"]
    _exact_keys(parameters, {"context_window", "limit", "cursor", "graph_depth", "relation_budget",
                             "path_budget", "context_budget"}, "retrieval_parameters")
    for key in ("context_window", "limit", "graph_depth", "relation_budget", "path_budget", "context_budget"):
        if isinstance(parameters[key], bool) or not isinstance(parameters[key], int) or parameters[key] < 0:
            raise MemoryPackageValidationError(f"invalid {key}")
    if parameters["cursor"] is not None and not isinstance(parameters["cursor"], str):
        raise MemoryPackageValidationError("invalid cursor")
    try:
        normalized = RetrievalQuery(
            query=request["task_or_query"],
            start_time=request["time"]["start_time"],
            end_time=request["time"]["end_time"],
            result_mode=request["scope"]["result_mode"],
            context_window=parameters["context_window"],
            limit=parameters["limit"],
            cursor=parameters["cursor"],
            hit_types=tuple(hit_types),
            graph_depth=parameters["graph_depth"],
            relation_budget=parameters["relation_budget"],
            path_budget=parameters["path_budget"],
            context_budget=parameters["context_budget"],
            memory_space=request["scope"]["memory_space"],
        ).normalized()
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError("request is outside the v6.4 contract") from exc
    expected = {
        "task_or_query": normalized.query,
        "scope": {
            "memory_space": normalized.memory_space,
            "result_mode": normalized.result_mode,
            "hit_types": list(normalized.hit_types),
        },
        "time": {"start_time": _normalized_iso(normalized.start), "end_time": _normalized_iso(normalized.end)},
        "retrieval_parameters": {
            "context_window": normalized.context_window,
            "limit": normalized.limit,
            "cursor": normalized.cursor,
            "graph_depth": normalized.graph_depth,
            "relation_budget": normalized.relation_budget,
            "path_budget": normalized.path_budget,
            "context_budget": normalized.context_budget,
        },
    }
    if request != expected:
        raise MemoryPackageValidationError("request is not in canonical v6.4 normalized form")
    return normalized


def _validate_retrieval(retrieval: Any) -> None:
    if not isinstance(retrieval, dict):
        raise MemoryPackageValidationError("retrieval must be an object")
    _exact_keys(retrieval, {"protocol_version", "status", "reason_code", "result_count",
                            "total_ranked", "next_cursor", "availability"}, "retrieval")
    if retrieval["protocol_version"] != RETRIEVAL_PROTOCOL_VERSION:
        raise MemoryPackageValidationError("retrieval protocol version mismatch")
    try:
        validate_retrieval_protocol_status(retrieval["status"], retrieval["reason_code"])
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError("invalid retrieval status") from exc
    for key in ("result_count", "total_ranked"):
        if isinstance(retrieval[key], bool) or not isinstance(retrieval[key], int) or retrieval[key] < 0:
            raise MemoryPackageValidationError("invalid retrieval counts")
    if retrieval["next_cursor"] is not None and not isinstance(retrieval["next_cursor"], str):
        raise MemoryPackageValidationError("invalid next_cursor")
    if not isinstance(retrieval["availability"], dict) or set(retrieval["availability"]) != {
        "source_database", "semantic_sidecar", "knowledge_sidecar",
    }:
        raise MemoryPackageValidationError("invalid retrieval availability")
    for component, state in retrieval["availability"].items():
        _validate_shared_availability(component, state)


def _validate_capabilities(capabilities: Any, request: dict[str, Any]) -> None:
    if not isinstance(capabilities, dict) or set(capabilities) != set(HIT_TYPES):
        raise MemoryPackageValidationError("invalid capabilities")
    for name, state in capabilities.items():
        if not isinstance(state, dict) or set(state) != {"status", "reason_code"}:
            raise MemoryPackageValidationError(f"invalid {name} capability")
        if not _token(state["status"]) or not _token(state["reason_code"]):
            raise MemoryPackageValidationError(f"invalid {name} capability")


def _validate_shared_availability(component: str, state: Any) -> None:
    try:
        validate_retrieval_availability_state(component, state)
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError(f"invalid {component} availability") from exc


def _validate_degradation(value: Any) -> None:
    if not isinstance(value, dict):
        raise MemoryPackageValidationError("degradation must be an object")
    _exact_keys(value, {"status", "reason_codes"}, "degradation")
    if value["status"] not in {"complete", "degraded", "unavailable"}:
        raise MemoryPackageValidationError("invalid degradation status")
    if not isinstance(value["reason_codes"], list) or any(not _token(item) for item in value["reason_codes"]):
        raise MemoryPackageValidationError("invalid degradation reasons")
    if value["reason_codes"] != sorted(value["reason_codes"]):
        raise MemoryPackageValidationError("degradation reasons are not canonical")


def _validate_warnings(warnings: Any) -> None:
    if not isinstance(warnings, list):
        raise MemoryPackageValidationError("warnings must be a list")
    for warning in warnings:
        if not isinstance(warning, dict):
            raise MemoryPackageValidationError("warning must be an object")
        _exact_keys(warning, {"code", "message"}, "warning")
        if not _token(warning["code"]) or not _token(warning["message"]):
            raise MemoryPackageValidationError("invalid warning")


def _validate_evidence(evidence: Any) -> None:
    if not isinstance(evidence, list):
        raise MemoryPackageValidationError("evidence must be a list")
    ids = []
    prior_key = None
    for item in evidence:
        if not isinstance(item, dict):
            raise MemoryPackageValidationError("evidence item must be an object")
        _exact_keys(item, {"evidence_id", "classification", "result_rank", "source", "hit",
                           "explanation", "excerpt", "details"}, "evidence")
        if item["classification"] != "unclassified_evidence":
            raise MemoryPackageValidationError("retrieval evidence cannot be promoted to a classification")
        if not isinstance(item["result_rank"], int) or isinstance(item["result_rank"], bool) or item["result_rank"] < 1:
            raise MemoryPackageValidationError("invalid evidence result rank")
        source = item["source"]
        _exact_keys(source, {"conversation", "message_anchor", "reference_uri"}, "evidence source")
        conversation = source["conversation"]
        anchor = source["message_anchor"]
        _exact_keys(conversation, {"conversation_id", "title"}, "conversation source")
        _exact_keys(anchor, {"message_id", "role", "time", "timestamp"}, "message anchor")
        if any(not isinstance(conversation[key], str) for key in conversation):
            raise MemoryPackageValidationError("invalid conversation source")
        if not conversation["conversation_id"] or not isinstance(anchor["message_id"], str) or not anchor["message_id"]:
            raise MemoryPackageValidationError("invalid source identity")
        if not all(isinstance(anchor[key], str) for key in ("role", "time")):
            raise MemoryPackageValidationError("invalid message anchor")
        if isinstance(anchor["timestamp"], bool) or not isinstance(anchor["timestamp"], (int, float)):
            raise MemoryPackageValidationError("invalid anchor timestamp")
        expected_uri = f"ayunos://conversation/{conversation['conversation_id']}?message={anchor['message_id']}"
        if source["reference_uri"] != expected_uri:
            raise MemoryPackageValidationError("source reference URI mismatch")
        _exact_keys(item["hit"], {"type", "score"}, "hit")
        if item["hit"]["type"] not in HIT_TYPES or isinstance(item["hit"]["score"], bool) or not isinstance(item["hit"]["score"], (int, float)):
            raise MemoryPackageValidationError("invalid evidence hit")
        _exact_keys(item["explanation"], {"basis_code", "text"}, "explanation")
        if item["explanation"]["basis_code"] != item["hit"]["type"] + "_retrieval_evidence" or not _token(item["explanation"]["text"]):
            raise MemoryPackageValidationError("invalid evidence explanation")
        if not isinstance(item["excerpt"], str) or not isinstance(item["details"], dict):
            raise MemoryPackageValidationError("invalid evidence content")
        _validate_evidence_details(item["hit"]["type"], item["details"])
        expected_id = "ev_" + _digest({key: value for key, value in item.items() if key != "evidence_id"})
        if item["evidence_id"] != expected_id:
            raise MemoryPackageValidationError("evidence identity mismatch")
        key = (item["result_rank"], _hit_rank(item["hit"]["type"]), item["evidence_id"])
        if prior_key is not None and key < prior_key:
            raise MemoryPackageValidationError("evidence order is not canonical")
        prior_key = key
        ids.append(item["evidence_id"])
    if len(ids) != len(set(ids)):
        raise MemoryPackageValidationError("duplicate evidence identity")


def _validate_classifications(classifications: Any, evidence: list[dict[str, Any]]) -> None:
    if not isinstance(classifications, dict) or set(classifications) != set(CLASSIFICATION_KEYS):
        raise MemoryPackageValidationError("classification schema mismatch")
    for key, values in classifications.items():
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise MemoryPackageValidationError(f"invalid classification list: {key}")
    if any(classifications[key] for key in CLASSIFICATION_KEYS if key != "unclassified_evidence"):
        raise MemoryPackageValidationError("v7.0 cannot assert classified memory")
    expected = [item["evidence_id"] for item in evidence]
    if classifications["unclassified_evidence"] != expected:
        raise MemoryPackageValidationError("unclassified evidence references mismatch")


def _validate_privacy(privacy: Any, package: dict[str, Any]) -> None:
    if not isinstance(privacy, dict):
        raise MemoryPackageValidationError("privacy must be an object")
    _exact_keys(privacy, {
        "absolute_path_redaction_applied", "redaction_count", "replacement",
    }, "privacy")
    if privacy["replacement"] != ABSOLUTE_PATH_REPLACEMENT:
        raise MemoryPackageValidationError("privacy replacement mismatch")
    if not isinstance(privacy["absolute_path_redaction_applied"], bool):
        raise MemoryPackageValidationError("invalid privacy redaction flag")
    if (
        isinstance(privacy["redaction_count"], bool)
        or not isinstance(privacy["redaction_count"], int)
        or privacy["redaction_count"] < 0
    ):
        raise MemoryPackageValidationError("invalid privacy redaction count")
    public_core = {key: value for key, value in package.items() if key != "privacy"}
    marker_count = _marker_count(public_core)
    if privacy["redaction_count"] != marker_count:
        raise MemoryPackageValidationError("privacy redaction count mismatch")
    if privacy["absolute_path_redaction_applied"] != (marker_count > 0):
        raise MemoryPackageValidationError("privacy redaction flag mismatch")


def _validate_cross_fields(
    package: dict[str, Any], normalized_request: NormalizedRetrievalQuery,
) -> None:
    retrieval = package["retrieval"]
    try:
        evidence_states = [
            project_retrieval_evidence_state(
                item["result_rank"], item["hit"]["type"], item["details"],
                conversation_id=item["source"]["conversation"]["conversation_id"],
                anchor_message_id=item["source"]["message_anchor"]["message_id"],
            )
            for item in package["evidence"]
        ]
        validate_retrieval_envelope_state(
            normalized_request,
            status=retrieval["status"], reason_code=retrieval["reason_code"],
            availability=retrieval["availability"], result_count=retrieval["result_count"],
            total_ranked=retrieval["total_ranked"], next_cursor=retrieval["next_cursor"],
            result_ranks=[item.rank for item in evidence_states],
            evidence=evidence_states,
        )
    except RetrievalValidationError as exc:
        raise MemoryPackageValidationError("retrieval envelope state mismatch") from exc
    expected_capabilities = expected_retrieval_capabilities(
        normalized_request, retrieval["availability"],
    )
    if package["capabilities"] != expected_capabilities:
        raise MemoryPackageValidationError("capability projection mismatch")
    missing = sorted(name for name, state in package["capabilities"].items() if state["status"] == "unavailable")
    if package["missing_capabilities"] != missing:
        raise MemoryPackageValidationError("missing capability state mismatch")
    reasons = sorted(package["capabilities"][name]["reason_code"] for name in missing)
    if package["degradation"]["reason_codes"] != reasons:
        raise MemoryPackageValidationError("degradation reason mismatch")
    expected_degradation = (
        "unavailable" if package["status"] == "unavailable" else "degraded" if missing else "complete"
    )
    if package["degradation"]["status"] != expected_degradation:
        raise MemoryPackageValidationError("degradation status mismatch")
    expected_warnings = [
        {
            "code": f"{name}_unavailable",
            "message": f"{name} capability unavailable: {package['capabilities'][name]['reason_code']}",
        }
        for name in missing
    ]
    if package["warnings"] != expected_warnings:
        raise MemoryPackageValidationError("warning propagation mismatch")
    availability = retrieval["availability"]
    if package["capabilities"]["semantic"] != availability["semantic_sidecar"]:
        raise MemoryPackageValidationError("semantic availability propagation mismatch")
    if package["capabilities"]["graph"] != availability["knowledge_sidecar"]:
        raise MemoryPackageValidationError("knowledge availability propagation mismatch")
    expected_keyword = availability["source_database"] if "keyword" in package["request"]["scope"]["hit_types"] else {
        "status": "not_requested", "reason_code": "hit_type_disabled",
    }
    if package["capabilities"]["keyword"] != expected_keyword:
        raise MemoryPackageValidationError("keyword availability propagation mismatch")
    evidence_count = len(package["evidence"])
    for item in package["evidence"]:
        hit_type = item["hit"]["type"]
        if hit_type not in package["request"]["scope"]["hit_types"]:
            raise MemoryPackageValidationError("evidence hit type was not requested")
        if package["capabilities"][hit_type]["status"] != "available":
            raise MemoryPackageValidationError("evidence requires an available capability")
    if package["status"] == "unavailable" and evidence_count:
        raise MemoryPackageValidationError("unavailable package cannot contain evidence")
    if package["status"] == "empty" and evidence_count:
        raise MemoryPackageValidationError("empty package cannot contain evidence")
    if package["status"] in {"ok", "partial"} and not evidence_count:
        raise MemoryPackageValidationError("non-empty package requires evidence")
    source_unavailable = availability["source_database"]["status"] == "unavailable"
    expected_status = (
        "unavailable" if source_unavailable else "empty" if not evidence_count
        else "partial" if missing else "ok"
    )
    expected_reason = (
        retrieval["reason_code"] if expected_status == "unavailable" else
        "no_evidence" if expected_status == "empty" else
        "partial_capability_result" if expected_status == "partial" else "package_complete"
    )
    if package["status"] != expected_status or package["reason_code"] != expected_reason:
        raise MemoryPackageValidationError("package status derivation mismatch")


def _validate_size(package: dict[str, Any]) -> None:
    size = package["size"]
    if not isinstance(size, dict):
        raise MemoryPackageValidationError("size must be an object")
    _exact_keys(size, {"canonical_json_chars", "evidence_excerpt_chars", "future_token_budget",
                       "token_selection_applied"}, "size")
    if size["token_selection_applied"] is not False:
        raise MemoryPackageValidationError("token selection is not implemented")
    if size["future_token_budget"] is not None and (
        isinstance(size["future_token_budget"], bool) or not isinstance(size["future_token_budget"], int)
        or not 1 <= size["future_token_budget"] <= 1_000_000
    ):
        raise MemoryPackageValidationError("invalid future token budget")
    if size["evidence_excerpt_chars"] != sum(len(item["excerpt"]) for item in package["evidence"]):
        raise MemoryPackageValidationError("evidence character count mismatch")
    if size["canonical_json_chars"] != _fixed_canonical_size(package):
        raise MemoryPackageValidationError("canonical JSON character count mismatch")


def _verify_source_backed_package(package: dict[str, Any], source_db: Path) -> None:
    if not source_db.is_file():
        raise MemoryPackageValidationError("trusted source database unavailable for verification")
    try:
        conn = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        expected_package = deepcopy(package)
        query = package["request"]["task_or_query"]
        verified_direct_anchors: set[tuple[str, str]] = set()
        for index, item in enumerate(package["evidence"]):
            conversation = item["source"]["conversation"]
            anchor = item["source"]["message_anchor"]
            row = conn.execute(
                "SELECT conversation_id,conversation_title,role,create_time,create_time_iso,text "
                "FROM messages WHERE message_id=? AND conversation_id=?",
                (anchor["message_id"], conversation["conversation_id"]),
            ).fetchone()
            if row is None:
                raise MemoryPackageValidationError("message anchor does not belong to source conversation")
            if str(row["role"] or "") != anchor["role"] or (
                str(row["create_time_iso"] or "") != anchor["time"]
            ) or float(row["create_time"] or 0) != float(anchor["timestamp"]):
                raise MemoryPackageValidationError("message anchor source fields mismatch")
            expected_title = _redacted_text(str(row["conversation_title"] or ""))
            expected_excerpt = _redacted_text(project_retrieval_excerpt(
                item["hit"]["type"], row["text"], query,
            ))
            expected_explanation = _redacted_text(project_retrieval_explanation(
                item["hit"]["type"], item["hit"]["score"], item["details"],
            ))
            if conversation["title"] != expected_title:
                raise MemoryPackageValidationError("source-backed conversation title projection mismatch")
            if item["excerpt"] != expected_excerpt:
                raise MemoryPackageValidationError("source-backed evidence excerpt projection mismatch")
            if item["explanation"]["text"] != expected_explanation:
                raise MemoryPackageValidationError("source-backed evidence explanation projection mismatch")
            if item["hit"]["type"] in DIRECT_HIT_TYPES:
                verified_direct_anchors.add((
                    conversation["conversation_id"], anchor["message_id"],
                ))
            expected_item = expected_package["evidence"][index]
            expected_item["source"]["conversation"]["title"] = expected_title
            expected_item["excerpt"] = expected_excerpt
            expected_item["explanation"]["text"] = expected_explanation
        for item in package["evidence"]:
            if item["hit"]["type"] != "graph":
                continue
            seed_anchor = (
                item["details"]["seed_conversation_id"],
                item["details"]["seed_anchor_message_id"],
            )
            if seed_anchor not in verified_direct_anchors:
                raise MemoryPackageValidationError("source-backed graph seed anchor mismatch")
        expected_count = _marker_count({
            key: value for key, value in expected_package.items() if key != "privacy"
        })
        expected_privacy = {
            "absolute_path_redaction_applied": expected_count > 0,
            "redaction_count": expected_count,
            "replacement": ABSOLUTE_PATH_REPLACEMENT,
        }
        if package["privacy"] != expected_privacy:
            raise MemoryPackageValidationError("source-backed privacy manifest mismatch")
        conn.rollback()
    except sqlite3.Error as exc:
        raise MemoryPackageValidationError("trusted source database verification failed") from exc
    finally:
        if "conn" in locals():
            conn.close()


def _safe_evidence_details(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "terms", "anchor_basis", "semantic_source_table", "semantic_source_id",
        "semantic_score", "summary_type", "seed_conversation_id",
        "seed_anchor_message_id", "seed_hit_types", "path_start_conversation_id",
        "path_start_anchor_message_id", "path_id", "depth", "relations",
    }
    return {key: deepcopy(raw[key]) for key in sorted(allowed.intersection(raw))}


def _validate_evidence_details(hit_type: str, details: dict[str, Any]) -> None:
    allowed = {
        "keyword": {"terms"},
        "semantic": {"anchor_basis", "semantic_source_table", "semantic_source_id",
                     "semantic_score", "summary_type"},
        "graph": {"anchor_basis", "seed_conversation_id", "seed_anchor_message_id",
                  "seed_hit_types", "path_start_conversation_id",
                  "path_start_anchor_message_id", "path_id", "depth", "relations"},
    }[hit_type]
    if not set(details).issubset(allowed):
        raise MemoryPackageValidationError("evidence details schema mismatch")
    if "terms" in details and (
        not isinstance(details["terms"], list) or any(not isinstance(item, str) for item in details["terms"])
    ):
        raise MemoryPackageValidationError("invalid keyword evidence details")
    for key in ("anchor_basis", "semantic_source_table", "semantic_source_id", "summary_type",
                "seed_conversation_id", "seed_anchor_message_id",
                "path_start_conversation_id", "path_start_anchor_message_id", "path_id"):
        if key in details and details[key] is not None and not isinstance(details[key], str):
            raise MemoryPackageValidationError("invalid evidence detail value")
    if "semantic_score" in details and (
        isinstance(details["semantic_score"], bool) or not isinstance(details["semantic_score"], (int, float))
    ):
        raise MemoryPackageValidationError("invalid semantic score detail")
    if "seed_hit_types" in details and (
        not isinstance(details["seed_hit_types"], list)
        or any(not isinstance(item, str) for item in details["seed_hit_types"])
    ):
        raise MemoryPackageValidationError("invalid graph seed hit types")
    if "depth" in details and (
        isinstance(details["depth"], bool) or not isinstance(details["depth"], int) or details["depth"] < 1
    ):
        raise MemoryPackageValidationError("invalid graph depth detail")
    if "relations" in details:
        if not isinstance(details["relations"], list):
            raise MemoryPackageValidationError("invalid graph relations detail")
        for relation in details["relations"]:
            _exact_keys(relation, {"relation_id", "relation_class", "relation_type", "evidence_identity"},
                        "graph relation detail")
            if any(not _token(value) for value in relation.values()):
                raise MemoryPackageValidationError("invalid graph relation detail")


def _identity_payload(package: dict[str, Any]) -> dict[str, Any]:
    payload = {key: deepcopy(value) for key, value in package.items() if key not in {"package_identity", "size"}}
    payload["budget_policy"] = {
        "future_token_budget": package["size"]["future_token_budget"],
        "token_selection_applied": package["size"]["token_selection_applied"],
    }
    return payload


def _fixed_canonical_size(package: dict[str, Any]) -> int:
    candidate = deepcopy(package)
    candidate["size"]["canonical_json_chars"] = 0
    value = 0
    for _ in range(12):
        candidate["size"]["canonical_json_chars"] = value
        updated = len(_canonical_dump(candidate))
        if updated == value:
            return value
        value = updated
    raise MemoryPackageValidationError("canonical size did not converge")


def _canonical_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _absolute_path_spans(value: str) -> list[tuple[int, int]]:
    """Return deterministic path spans, preferring over-redaction to suffix leaks."""
    text = str(value or "")
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if not _absolute_path_starts_at(text, index):
            index += 1
            continue
        end = _scan_absolute_path_end(text, index)
        while end > index and text[end - 1].isspace():
            end -= 1
        if end <= index:
            index += 1
            continue
        spans.append((index, end))
        index = end
    return spans


def _absolute_path_starts_at(text: str, index: int) -> bool:
    previous = text[index - 1] if index else ""
    previous_is_word = bool(previous) and (previous.isalnum() or previous == "_")
    suffix = text[index:]
    if suffix[:5].lower() == "file:" and not previous_is_word:
        return len(suffix) > 5 and suffix[5] in "/\\"
    if (
        len(suffix) >= 3 and suffix[0].isascii() and suffix[0].isalpha()
        and suffix[1] == ":" and suffix[2] in "/\\"
        and not previous_is_word and previous != "\\"
    ):
        return True
    if suffix.startswith("\\\\") and not previous_is_word and previous != "\\":
        return len(suffix) > 2 and not suffix[2].isspace()
    if suffix.startswith("/") and len(suffix) > 1 and not suffix[1].isspace():
        return not previous_is_word and previous not in ":/\\"
    return False


def _scan_absolute_path_end(text: str, start: int) -> int:
    depths = {opener: 0 for opener in _PATH_OPENERS}
    external_closer = _PATH_OPENERS.get(text[start - 1]) if start else None
    index = start
    while index < len(text):
        char = text[index]
        if ord(char) < 32 or char in _PATH_HARD_STOPS:
            break
        if char in _PATH_OPENERS:
            depths[char] += 1
            index += 1
            continue
        if char in _PATH_CLOSERS:
            opener = next(key for key, closer in _PATH_OPENERS.items() if closer == char)
            if depths[opener] == 0:
                if char == external_closer:
                    break
                index += 1
                continue
            depths[opener] -= 1
            index += 1
            continue
        if (
            char in _PATH_SENTENCE_TERMINATORS
            and not any(depths.values())
            and (index + 1 == len(text) or text[index + 1].isspace())
        ):
            break
        index += 1
    return index


def _contains_absolute_path(value: str) -> bool:
    return bool(_absolute_path_spans(str(value or "")))


def _redacted_text(value: str) -> str:
    return _PrivacyRedactor().text(value)


def _marker_count(value: Any) -> int:
    if isinstance(value, str):
        return value.count(ABSOLUTE_PATH_REPLACEMENT)
    if isinstance(value, list):
        return sum(_marker_count(item) for item in value)
    if isinstance(value, dict):
        return sum(_marker_count(item) for item in value.values())
    return 0


def _reject_absolute_paths(value: Any) -> None:
    if isinstance(value, str):
        if _contains_absolute_path(value):
            raise MemoryPackageValidationError("memory package contains an absolute path")
        return
    if isinstance(value, list):
        for item in value:
            _reject_absolute_paths(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_absolute_paths(item)


def _normalized_iso(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_dump(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise MemoryPackageValidationError(f"{label} schema mismatch")


def _token(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(ord(char) < 32 for char in value)


def _hit_rank(value: str) -> int:
    return HIT_TYPES.index(value)


def _text_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)
