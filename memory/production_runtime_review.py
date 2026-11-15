"""Pure review-only construction for the fixed D1 production Runtime request.

This module deliberately has no execution authority, execute facade, provider
adapter, credential loader, ledger writer, HTTP request, or network dependency.
It may inspect the initialized production Runtime only through the readonly
status projection.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable

from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.production_delivery import (
    build_production_delivery,
    production_profile_binding,
    validate_trusted_delivery_standalone,
)
from memory.production_runtime_status import production_runtime_status
from memory.runtime_protocol_contracts import (
    FIXED_RUNTIME_PROFILE,
    RUNTIME_PROTOCOL_VERSION,
    RuntimeProtocolContractError,
    build_reviewed_execution_binding,
    build_runtime_artifact,
    canonical_digest,
    conversation_identity,
    model_run_identity,
    prepare_runtime_request,
    public_runtime_artifact,
    request_fingerprint,
    validate_runtime_artifact_binding,
)


PROTOCOL_VERSION = RUNTIME_PROTOCOL_VERSION
DIAGNOSTICS_BINDING_VERSION = "v8.0-g-runtime-preview-diagnostics-1"
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class ProductionRuntimeReviewError(ValueError):
    """Sanitized failure while constructing a fixed production review."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _normalize_question(value: Any) -> str:
    if not isinstance(value, str):
        raise ProductionRuntimeReviewError("question_invalid")
    normalized = " ".join(value.split())
    if (
        not normalized
        or normalized != value
        or len(normalized) > 500
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ProductionRuntimeReviewError("question_invalid")
    return normalized


def _normalize_budget(value: Any) -> str:
    if value not in {"Small", "Medium", "Large"}:
        raise ProductionRuntimeReviewError("budget_profile_invalid")
    return str(value)


def _normalize_id(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProductionRuntimeReviewError(reason_code)
    return value


def _production_delivery(
    query: RetrievalQuery, *, budget_profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery = build_production_delivery(query, budget_profile=budget_profile)
    return delivery, production_profile_binding(delivery)


def _build_reviewed_request(
    question: Any,
    *,
    budget_profile: Any,
    request_id: Any,
    mutation_id: Any,
    delivery_builder: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    normalized = _normalize_question(question)
    budget = _normalize_budget(budget_profile)
    request = _normalize_id(request_id, "request_id_invalid")
    mutation = _normalize_id(mutation_id, "mutation_id_invalid")
    delivery, profile_binding = delivery_builder(
        RetrievalQuery(normalized), budget_profile=budget,
    )
    validate_trusted_delivery_standalone(delivery)
    if delivery.get("controls", {}).get("query") != normalized:
        raise ProductionRuntimeReviewError("delivery_query_binding_mismatch")
    try:
        artifact = build_runtime_artifact(
            delivery, approved_profile_binding=profile_binding,
        )
        validate_runtime_artifact_binding(artifact, delivery)
        prepared = prepare_runtime_request(artifact)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeReviewError(exc.reason_code) from None

    fingerprint = request_fingerprint(
        normalized, budget, artifact["artifact_identity"], artifact["wire_body_sha256"],
    )
    run_id = model_run_identity(request, mutation, fingerprint, artifact["artifact_identity"])
    conversation_id = conversation_identity(run_id)
    diagnostic = {
        "delivery_identity": delivery["delivery_identity"],
        "assembly_identity": delivery["assembly_binding"]["assembly_identity"],
        "artifact_identity": artifact["artifact_identity"],
        "model_context_sha256": artifact["delivery_binding"]["model_context_sha256"],
        "wire_body_sha256": prepared.wire_body_sha256,
        "profile_projection_identity": artifact["approved_profile_binding"][
            "projection_identity"
        ],
        "request_fingerprint": fingerprint,
        "model_run_id": run_id,
        "conversation_id": conversation_id,
    }
    diagnostic["version"] = DIAGNOSTICS_BINDING_VERSION
    diagnostic["identity"] = "rdi_" + canonical_digest({
        key: value for key, value in diagnostic.items() if key != "identity"
    })
    try:
        reviewed_binding = build_reviewed_execution_binding(
            delivery, artifact, request_id=request, mutation_id=mutation,
        )
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeReviewError(exc.reason_code) from None
    return delivery, artifact, {
        "question": normalized,
        "budget_profile": budget,
        "request_id": request,
        "mutation_id": mutation,
        "diagnostic": diagnostic,
        "reviewed_execution_binding": reviewed_binding,
    }


def _preview_result(
    delivery: dict[str, Any],
    artifact: dict[str, Any],
    request: dict[str, Any],
    sidecar: dict[str, Any],
) -> dict[str, Any]:
    profile_items = delivery["layers"]["approved_profile"]["items"]
    evidence_items = delivery["layers"]["historical_evidence"]["items"]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "preview",
        "execution_phase": "not_attempted",
        "confirmation_required": True,
        "question": request["question"],
        "budget_profile": request["budget_profile"],
        "request_id": request["request_id"],
        "mutation_id": request["mutation_id"],
        "diagnostic_bindings": deepcopy(request["diagnostic"]),
        "reviewed_execution_binding": deepcopy(request["reviewed_execution_binding"]),
        "delivery_identity": delivery["delivery_identity"],
        "delivery": deepcopy(delivery),
        "request_artifact": public_runtime_artifact(artifact),
        "runtime_profile": deepcopy(FIXED_RUNTIME_PROFILE),
        "runtime_sidecar": deepcopy(sidecar),
        "review": {
            "approved_profile_included": len(profile_items),
            "approved_profile_available": delivery["counts"]["approved_profile_available"],
            "profile_item_ids": [item["profile_item_id"] for item in profile_items],
            "evidence_reference_ids": [
                item["evidence_reference_id"] for item in evidence_items
            ],
            "estimated_tokens": delivery["budget"]["estimated_tokens_used"],
        },
        "usage": None,
        "response": None,
        "error": None,
        "execution": {
            "persisted": False,
            "credential_read": False,
            "sent_to_model": False,
            "external_model_calls": False,
            "network_request_count": 0,
            "automatic_retry": False,
            "mock_transport": False,
        },
    }


class ProductionRuntimeReviewService:
    """Fixed preview facade with no execute method or execution dependencies."""

    __slots__ = ()

    def preview(
        self, question: Any, *, budget_profile: Any, request_id: Any, mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_reviewed_request(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=_production_delivery,
        )
        return _preview_result(delivery, artifact, request, production_runtime_status())


__all__ = [
    "DIAGNOSTICS_BINDING_VERSION",
    "PROTOCOL_VERSION",
    "ProductionRuntimeReviewError",
    "ProductionRuntimeReviewService",
]
