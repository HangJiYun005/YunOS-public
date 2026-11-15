"""Fixed D1 production Runtime preview, confirmation, and single-call execution.

Production entry points accept no sidecar, provider, model, renderer, schema,
credential, transport, delivery-builder, or validator override.  Tests and the
visible mock acceptance surface use an explicitly named temporary-sidecar
factory whose adapter never performs credential or network I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable
import weakref

from memory.deepseek_v4_adapter import (
    DeepSeekV4FlashHTTPAdapter,
    DeepSeekV4ProviderError,
    DeepSeekV4Response,
    prepare_v4_flash_request,
)
from memory.context_assembly import estimate_tokens
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.production_delivery import (
    build_production_delivery,
    production_profile_binding,
    validate_trusted_delivery_standalone,
)
from memory.production_runtime_status import (
    PRODUCTION_RUNTIME_SIDECAR,
    production_runtime_status,
)
from memory.runtime_protocol_contracts import (
    BASE_URL,
    BoundedToolRuntimeRequest,
    CognitiveAnswerRuntimeRequest,
    FIXED_RUNTIME_PROFILE,
    MODEL_ID,
    PROVIDER_ID,
    RENDERER_VERSION,
    RUNTIME_PROFILE_VERSION,
    RUNTIME_PROTOCOL_VERSION,
    RUNTIME_SCHEMA_VERSION,
    RuntimeProtocolContractError,
    build_reviewed_execution_binding,
    build_bounded_tool_runtime_artifact,
    build_cognitive_answer_runtime_artifact,
    build_runtime_artifact,
    build_runtime_receipt,
    canonical_digest,
    canonical_json,
    conversation_identity,
    error_identity,
    event_identity,
    message_identity,
    model_run_identity,
    prepare_runtime_request,
    profile_projection_identity,
    project_runtime_events,
    public_runtime_artifact,
    request_fingerprint,
    runtime_profile_identity,
    text_sha256,
    validate_reviewed_execution_binding,
    validate_bounded_tool_runtime_request,
    validate_cognitive_answer_runtime_request,
    validate_runtime_artifact_binding,
    validate_runtime_ledger,
)


PROTOCOL_VERSION = RUNTIME_PROTOCOL_VERSION
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_RUN_RE = re.compile(r"run_[0-9a-f]{64}")

_LEDGER_TABLES = (
    "runtime_conversations",
    "runtime_request_artifacts",
    "runtime_model_runs",
    "runtime_messages",
    "runtime_run_events",
    "runtime_errors",
    "runtime_receipts",
)
_EXPECTED_COLUMNS = {
    "runtime_schema": {
        "schema_version", "runtime_profile_json", "runtime_profile_identity", "initialized_at",
    },
    "runtime_conversations": {"conversation_id", "created_at"},
    "runtime_request_artifacts": {
        "artifact_identity", "artifact_version", "runtime_profile_version", "renderer_version",
        "delivery_identity", "delivery_json", "approved_profile_projection_identity",
        "provider", "model", "base_url", "stream", "thinking_type", "wire_body_sha256",
        "artifact_json", "created_at",
    },
    "runtime_model_runs": {
        "model_run_id", "request_id", "mutation_id", "request_fingerprint",
        "conversation_id", "artifact_identity", "created_at",
    },
    "runtime_messages": {
        "message_id", "conversation_id", "model_run_id", "role", "content", "created_at",
    },
    "runtime_errors": {
        "error_id", "model_run_id", "category", "code", "retryable", "http_status",
        "sent_to_model", "external_model_calls", "created_at",
    },
    "runtime_run_events": {
        "sequence", "event_id", "model_run_id", "event_type", "from_status", "to_status",
        "assistant_message_id", "error_id", "usage_json", "provider_response_id",
        "finish_reason", "response_content_sha256", "occurred_at",
    },
    "runtime_receipts": {"model_run_id", "receipt_identity", "receipt_json"},
}
_DIAGNOSTICS_BINDING_VERSION = "v8.0-g-runtime-preview-diagnostics-1"


class ProductionRuntimeExecutionError(ValueError):
    """Sanitized fixed-production execution or confirmation failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class RuntimeSidecarUnavailable(ProductionRuntimeExecutionError):
    pass


class RuntimeIdempotencyConflict(ProductionRuntimeExecutionError):
    pass


class RuntimePreviewMismatch(ProductionRuntimeExecutionError):
    pass


class ProductionRuntimeExecutionSession:
    """Unforgeable process-local authority for the fixed production adapter."""

    def __new__(cls):
        raise ProductionRuntimeExecutionError("runtime_execution_session_not_issued")


def _build_production_session_issuer():
    issued: weakref.WeakSet[ProductionRuntimeExecutionSession] = weakref.WeakSet()
    authorized_codes: set[Any] = set()

    def issue() -> ProductionRuntimeExecutionSession:
        if sys._getframe(1).f_code not in authorized_codes:
            raise ProductionRuntimeExecutionError("runtime_execution_session_issuer_not_authorized")
        session = object.__new__(ProductionRuntimeExecutionSession)
        issued.add(session)
        return session

    def trusted(value: Any) -> bool:
        return isinstance(value, ProductionRuntimeExecutionSession) and value in issued

    def authorize(code: Any) -> None:
        authorized_codes.add(code)

    return issue, trusted, authorize


_issue_production_session, is_trusted_production_session, _authorize_session_issuer = (
    _build_production_session_issuer()
)


def open_production_runtime_execution(
    *, execute: bool, allow_external_model_call: bool, confirm_deepseek_send: bool,
) -> ProductionRuntimeExecutionSession:
    if (
        execute is not True
        or allow_external_model_call is not True
        or confirm_deepseek_send is not True
    ):
        raise ProductionRuntimeExecutionError("production_runtime_confirmation_required")
    return _issue_production_session()


_authorize_session_issuer(open_production_runtime_execution.__code__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_question(value: Any) -> str:
    if not isinstance(value, str):
        raise ProductionRuntimeExecutionError("question_invalid")
    normalized = " ".join(value.split())
    if (
        not normalized
        or normalized != value
        or len(normalized) > 500
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ProductionRuntimeExecutionError("question_invalid")
    return normalized


def _normalize_budget(value: Any) -> str:
    if value not in {"Small", "Medium", "Large"}:
        raise ProductionRuntimeExecutionError("budget_profile_invalid")
    return str(value)


def _normalize_id(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProductionRuntimeExecutionError(reason_code)
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
        raise ProductionRuntimeExecutionError("delivery_query_binding_mismatch")
    try:
        artifact = build_runtime_artifact(
            delivery, approved_profile_binding=profile_binding,
        )
        validate_runtime_artifact_binding(artifact, delivery)
        prepared = prepare_runtime_request(artifact)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
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
        "profile_projection_identity": artifact["approved_profile_binding"]["projection_identity"],
        "request_fingerprint": fingerprint,
        "model_run_id": run_id,
        "conversation_id": conversation_id,
    }
    diagnostic["version"] = _DIAGNOSTICS_BINDING_VERSION
    diagnostic["identity"] = "rdi_" + canonical_digest({
        key: value for key, value in diagnostic.items() if key != "identity"
    })
    try:
        reviewed_binding = build_reviewed_execution_binding(
            delivery, artifact, request_id=request, mutation_id=mutation,
        )
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    return delivery, artifact, {
        "question": normalized,
        "budget_profile": budget,
        "request_id": request,
        "mutation_id": mutation,
        "diagnostic": diagnostic,
        "reviewed_execution_binding": reviewed_binding,
    }


def _build_bounded_reviewed_request(
    bounded_request: Any,
    *,
    request_id: Any,
    mutation_id: Any,
    delivery_builder: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        typed = validate_bounded_tool_runtime_request(bounded_request)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    normalized = _normalize_question(typed.retrieval_question)
    request = _normalize_id(request_id, "request_id_invalid")
    mutation = _normalize_id(mutation_id, "mutation_id_invalid")
    budget = "Medium"
    delivery, profile_binding = delivery_builder(
        RetrievalQuery(normalized), budget_profile=budget,
    )
    validate_trusted_delivery_standalone(delivery)
    if delivery.get("controls", {}).get("query") != normalized:
        raise ProductionRuntimeExecutionError("delivery_query_binding_mismatch")
    try:
        artifact = build_bounded_tool_runtime_artifact(
            delivery,
            typed,
            approved_profile_binding=profile_binding,
        )
        validate_runtime_artifact_binding(artifact, delivery)
        prepared = prepare_runtime_request(artifact)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    fingerprint = request_fingerprint(
        normalized,
        budget,
        artifact["artifact_identity"],
        artifact["wire_body_sha256"],
    )
    run_id = model_run_identity(request, mutation, fingerprint, artifact["artifact_identity"])
    conversation_id = conversation_identity(run_id)
    diagnostic = {
        "delivery_identity": delivery["delivery_identity"],
        "assembly_identity": delivery["assembly_binding"]["assembly_identity"],
        "artifact_identity": artifact["artifact_identity"],
        "model_context_sha256": artifact["bounded_tool_binding"][
            "wire_model_context_sha256"
        ],
        "wire_body_sha256": prepared.wire_body_sha256,
        "profile_projection_identity": artifact["approved_profile_binding"][
            "projection_identity"
        ],
        "request_fingerprint": fingerprint,
        "model_run_id": run_id,
        "conversation_id": conversation_id,
        "bounded_tool_request_identity": typed.request_identity,
        "bounded_tool_appendix_sha256": typed.appendix_sha256,
    }
    diagnostic["version"] = _DIAGNOSTICS_BINDING_VERSION
    diagnostic["identity"] = "rdi_" + canonical_digest(
        {key: value for key, value in diagnostic.items() if key != "identity"}
    )
    try:
        reviewed_binding = build_reviewed_execution_binding(
            delivery,
            artifact,
            request_id=request,
            mutation_id=mutation,
        )
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    model_context = artifact["payload"]["messages"][1]["content"]
    return delivery, artifact, {
        "question": normalized,
        "budget_profile": budget,
        "request_id": request,
        "mutation_id": mutation,
        "diagnostic": diagnostic,
        "reviewed_execution_binding": reviewed_binding,
        "bounded_tool_request_binding": typed.audit_binding(),
        "estimated_tokens": estimate_tokens(model_context),
    }


def _build_cognitive_answer_reviewed_request(
    answer_request: Any,
    *,
    request_id: Any,
    mutation_id: Any,
    delivery_builder: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        typed = validate_cognitive_answer_runtime_request(answer_request)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    normalized = _normalize_question(typed.question)
    request = _normalize_id(request_id, "request_id_invalid")
    mutation = _normalize_id(mutation_id, "mutation_id_invalid")
    # The typed appendix independently binds the full v7/v9 cognitive context.
    # Keep the legacy delivery contribution minimal and deterministic.
    requested_budget = "Small"
    delivery, profile_binding = delivery_builder(
        RetrievalQuery(normalized), budget_profile=requested_budget,
    )
    validate_trusted_delivery_standalone(delivery)
    if delivery.get("controls", {}).get("query") != normalized:
        raise ProductionRuntimeExecutionError("delivery_query_binding_mismatch")
    try:
        budget = _normalize_budget(delivery["controls"]["budget_profile"])
    except (KeyError, TypeError):
        raise ProductionRuntimeExecutionError("delivery_budget_binding_mismatch") from None
    try:
        artifact = build_cognitive_answer_runtime_artifact(
            delivery,
            typed,
            approved_profile_binding=profile_binding,
        )
        validate_runtime_artifact_binding(artifact, delivery)
        prepared = prepare_runtime_request(artifact)
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    fingerprint = request_fingerprint(
        normalized,
        budget,
        artifact["artifact_identity"],
        artifact["wire_body_sha256"],
    )
    run_id = model_run_identity(
        request, mutation, fingerprint, artifact["artifact_identity"]
    )
    conversation_id = conversation_identity(run_id)
    answer_context = artifact["answer_context_binding"]
    diagnostic = {
        "delivery_identity": delivery["delivery_identity"],
        "assembly_identity": delivery["assembly_binding"]["assembly_identity"],
        "artifact_identity": artifact["artifact_identity"],
        "model_context_sha256": answer_context["wire_model_context_sha256"],
        "wire_body_sha256": prepared.wire_body_sha256,
        "profile_projection_identity": artifact["approved_profile_binding"][
            "projection_identity"
        ],
        "request_fingerprint": fingerprint,
        "model_run_id": run_id,
        "conversation_id": conversation_id,
        "cognitive_answer_request_identity": typed.request_identity,
        "objective_digest": typed.objective_digest,
        "context_digest": typed.context_digest,
        "source_anchors_digest": typed.source_anchors_digest,
    }
    diagnostic["version"] = _DIAGNOSTICS_BINDING_VERSION
    diagnostic["identity"] = "rdi_" + canonical_digest(
        {key: value for key, value in diagnostic.items() if key != "identity"}
    )
    try:
        reviewed_binding = build_reviewed_execution_binding(
            delivery,
            artifact,
            request_id=request,
            mutation_id=mutation,
        )
    except RuntimeProtocolContractError as exc:
        raise ProductionRuntimeExecutionError(exc.reason_code) from None
    return delivery, artifact, {
        "question": normalized,
        "budget_profile": budget,
        "request_id": request,
        "mutation_id": mutation,
        "diagnostic": diagnostic,
        "reviewed_execution_binding": reviewed_binding,
        "cognitive_answer_request_binding": typed.audit_binding(),
        "estimated_tokens": answer_context["wire_input_estimated_tokens"],
    }


def _preview_result(
    delivery: dict[str, Any],
    artifact: dict[str, Any],
    request: dict[str, Any],
    sidecar: dict[str, Any],
    *,
    mock_transport: bool,
) -> dict[str, Any]:
    profile_items = delivery["layers"]["approved_profile"]["items"]
    evidence_items = delivery["layers"]["historical_evidence"]["items"]
    result = {
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
            "evidence_reference_ids": [item["evidence_reference_id"] for item in evidence_items],
            "estimated_tokens": request.get(
                "estimated_tokens",
                delivery["budget"]["estimated_tokens_used"],
            ),
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
            "mock_transport": mock_transport,
        },
    }
    bounded_binding = request.get("bounded_tool_request_binding")
    if bounded_binding is not None:
        result["bounded_tool_request_binding"] = deepcopy(bounded_binding)
    answer_binding = request.get("cognitive_answer_request_binding")
    if answer_binding is not None:
        result["cognitive_answer_request_binding"] = deepcopy(answer_binding)
        result["model_run_id"] = request["diagnostic"]["model_run_id"]
    return result


def _validate_cognitive_answer_provider_response(
    artifact: dict[str, Any], response: DeepSeekV4Response,
) -> None:
    binding = artifact.get("cognitive_answer_request_binding")
    if binding is None:
        return
    if not isinstance(binding, dict):
        raise DeepSeekV4ProviderError(
            "provider_response_budget_binding_invalid",
            category="budget",
            retryable=False,
        )
    usage = response.usage
    if (
        not isinstance(response.content, str)
        or not response.content.strip()
        or not isinstance(usage, dict)
        or set(usage)
        != {"prompt_tokens", "completion_tokens", "total_tokens"}
        or any(
            type(value) is not int or value < 0 for value in usage.values()
        )
        or usage["total_tokens"]
        != usage["prompt_tokens"] + usage["completion_tokens"]
    ):
        raise DeepSeekV4ProviderError(
            "provider_response_invalid",
            category="provider",
            retryable=False,
        )
    if len(response.content.encode("utf-8")) > binding["maximum_output_bytes"]:
        raise DeepSeekV4ProviderError(
            "provider_response_output_budget_exceeded",
            category="budget",
            retryable=False,
        )
    if (
        usage["prompt_tokens"] > binding["maximum_input_tokens"]
        or not 1
        <= usage["completion_tokens"]
        <= binding["maximum_output_tokens"]
        or usage["total_tokens"] > binding["maximum_total_tokens"]
    ):
        raise DeepSeekV4ProviderError(
            "provider_response_usage_budget_exceeded",
            category="budget",
            retryable=False,
        )


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
        for table in _LEDGER_TABLES
    }


def _validate_schema_and_ledger(connection: sqlite3.Connection) -> dict[str, Any]:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeSidecarUnavailable("runtime_integrity_failure")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RuntimeSidecarUnavailable("runtime_quick_check_failure")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeSidecarUnavailable("runtime_foreign_key_failure")
    for table, expected in _EXPECTED_COLUMNS.items():
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns != expected:
            raise RuntimeSidecarUnavailable("runtime_schema_incompatible")
        triggers = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,),
            )
        }
        if triggers != {f"{table}_no_update", f"{table}_no_delete"}:
            raise RuntimeSidecarUnavailable("append_only_protection_missing")
    rows = connection.execute("SELECT * FROM runtime_schema").fetchall()
    if len(rows) != 1:
        raise RuntimeSidecarUnavailable("runtime_schema_incompatible")
    schema = rows[0]
    if (
        schema["schema_version"] != RUNTIME_SCHEMA_VERSION
        or schema["runtime_profile_json"] != canonical_json(FIXED_RUNTIME_PROFILE)
        or schema["runtime_profile_identity"] != runtime_profile_identity()
    ):
        raise RuntimeSidecarUnavailable("runtime_profile_binding_mismatch")
    try:
        validated = validate_runtime_ledger(_snapshot(connection))
    except RuntimeProtocolContractError as exc:
        raise RuntimeSidecarUnavailable(exc.reason_code) from None
    return validated


@contextmanager
def _open_validated_sidecar(path: Path, *, write: bool):
    if not path.is_file():
        raise RuntimeSidecarUnavailable("runtime_sidecar_missing")
    if write:
        connection = sqlite3.connect(path)
    else:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if not write:
        connection.execute("PRAGMA query_only=ON")
    try:
        _validate_schema_and_ledger(connection)
        yield connection
    finally:
        connection.close()


def _temporary_sidecar_status(path: Path) -> dict[str, Any]:
    try:
        with _open_validated_sidecar(path, write=False):
            pass
    except ProductionRuntimeExecutionError as exc:
        return {"status": "invalid", "ready": False, "reason_code": exc.reason_code}
    return {
        "status": "ready",
        "ready": True,
        "reason_code": "ready",
        "path": str(path.resolve()),
        "schema_version": RUNTIME_SCHEMA_VERSION,
    }


def _expected_bindings(value: Any) -> dict[str, Any]:
    try:
        return validate_reviewed_execution_binding(value)
    except RuntimeProtocolContractError:
        raise RuntimePreviewMismatch("runtime_preview_binding_invalid") from None


def _load_receipt(connection: sqlite3.Connection, model_run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT receipt_json FROM runtime_receipts WHERE model_run_id=?", (model_run_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(row["receipt_json"])
    except (TypeError, json.JSONDecodeError):
        raise RuntimeSidecarUnavailable("malformed_receipt_identity") from None
    return receipt


def _run_projection(connection: sqlite3.Connection, model_run_id: str) -> dict[str, Any]:
    rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM runtime_run_events WHERE model_run_id=? ORDER BY sequence",
            (model_run_id,),
        ).fetchall()
    ]
    try:
        return project_runtime_events(rows)
    except RuntimeProtocolContractError as exc:
        raise RuntimeSidecarUnavailable(exc.reason_code) from None


def _result_for_run(connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
    _validate_schema_and_ledger(connection)
    receipt = _load_receipt(connection, run["model_run_id"])
    if receipt is not None:
        return receipt
    artifact_row = connection.execute(
        "SELECT artifact_json FROM runtime_request_artifacts WHERE artifact_identity=?",
        (run["artifact_identity"],),
    ).fetchone()
    if artifact_row is None:
        raise RuntimeSidecarUnavailable("request_artifact_binding_mismatch")
    artifact = json.loads(artifact_row["artifact_json"])
    projection = _run_projection(connection, run["model_run_id"])
    if projection["provider_response_received"]:
        phase = "provider_response_received_local_commit_pending"
    elif projection["send_attempted"]:
        phase = "send_attempted_result_unknown"
    else:
        phase = "not_attempted"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "model_run_id": run["model_run_id"],
        "conversation_id": run["conversation_id"],
        "request_id": run["request_id"],
        "mutation_id": run["mutation_id"],
        "status": projection["status"],
        "execution_phase": phase,
        "delivery_identity": artifact["delivery_binding"]["delivery_identity"],
        "request_artifact": public_runtime_artifact(artifact),
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "usage": None,
        "response": None,
        "error": None,
        "execution": {
            "persisted": True,
            "sent_to_model": bool(projection["send_attempted"]),
            "external_model_calls": bool(projection["send_attempted"]),
        },
        "candidate_hook": {"status": "reserved", "invoked": False},
    }


def _execution_envelope(
    result: dict[str, Any], *, adapter_constructor_count: int,
    adapter_call_count: int, mock_transport: bool,
) -> dict[str, Any]:
    simulated_call = bool(result.get("execution", {}).get("external_model_calls"))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": result.get("status"),
        "execution_phase": result.get("execution_phase"),
        "result": deepcopy(result),
        "transport": {
            "adapter_constructor_count_this_request": adapter_constructor_count,
            "adapter_call_count_this_request": adapter_call_count,
            "model_call_count_for_run": 1 if simulated_call else 0,
            "exactly_one_call": simulated_call,
            "automatic_retry": False,
            "mock_transport": mock_transport,
            "network_request_count": 0 if mock_transport else adapter_call_count,
            "credential_read": bool(adapter_call_count and not mock_transport),
        },
    }


class _RuntimeCore:
    def __init__(
        self,
        *,
        sidecar_path: Path,
        adapter_factory: Callable[[], Any],
        delivery_builder: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
        production: bool,
        mock_transport: bool,
    ) -> None:
        path = sidecar_path.expanduser().resolve()
        fixed = PRODUCTION_RUNTIME_SIDECAR.resolve()
        if production is not (path == fixed):
            raise ProductionRuntimeExecutionError("runtime_sidecar_scope_invalid")
        if not callable(delivery_builder) or not callable(adapter_factory):
            raise ProductionRuntimeExecutionError("runtime_dependency_invalid")
        if production and mock_transport:
            raise ProductionRuntimeExecutionError("production_adapter_invalid")
        if not production and not mock_transport:
            raise ProductionRuntimeExecutionError("test_runtime_requires_mock_transport")
        self.sidecar_path = path
        self.adapter_factory = adapter_factory
        self.delivery_builder = delivery_builder
        self.production = production
        self.mock_transport = mock_transport

    def _construct_adapter(self) -> Any:
        adapter = self.adapter_factory()
        if not callable(getattr(adapter, "send", None)):
            raise ProductionRuntimeExecutionError("runtime_dependency_invalid")
        if (
            getattr(adapter, "provider_id", None) != PROVIDER_ID
            or getattr(adapter, "model_id", None) != MODEL_ID
            or getattr(adapter, "base_url", None) != BASE_URL
        ):
            raise ProductionRuntimeExecutionError("runtime_adapter_binding_mismatch")
        if self.production and not isinstance(adapter, DeepSeekV4FlashHTTPAdapter):
            raise ProductionRuntimeExecutionError("production_adapter_invalid")
        if not self.production and isinstance(adapter, DeepSeekV4FlashHTTPAdapter):
            raise ProductionRuntimeExecutionError("test_runtime_requires_mock_transport")
        return adapter

    def _existing_run(
        self,
        connection: sqlite3.Connection,
        request: dict[str, Any],
        expected: dict[str, Any],
    ) -> sqlite3.Row | None:
        existing = list(connection.execute(
            "SELECT * FROM runtime_model_runs WHERE request_id=? OR mutation_id=?",
            (request["request_id"], request["mutation_id"]),
        ))
        if not existing:
            return None
        if (
            len(existing) != 1
            or existing[0]["request_id"] != request["request_id"]
            or existing[0]["mutation_id"] != request["mutation_id"]
        ):
            raise RuntimeIdempotencyConflict("runtime_idempotency_conflict")
        artifact_row = connection.execute(
            "SELECT artifact_json,delivery_json FROM runtime_request_artifacts "
            "WHERE artifact_identity=?",
            (existing[0]["artifact_identity"],),
        ).fetchone()
        if artifact_row is None:
            raise RuntimeSidecarUnavailable("request_artifact_binding_mismatch")
        try:
            stored_artifact = json.loads(artifact_row["artifact_json"])
            stored_delivery = json.loads(artifact_row["delivery_json"])
            stored_binding = build_reviewed_execution_binding(
                stored_delivery,
                stored_artifact,
                request_id=existing[0]["request_id"],
                mutation_id=existing[0]["mutation_id"],
            )
        except (TypeError, json.JSONDecodeError, RuntimeProtocolContractError) as exc:
            reason = getattr(exc, "reason_code", "request_artifact_binding_mismatch")
            raise RuntimeSidecarUnavailable(reason) from None
        if stored_binding != expected:
            raise RuntimeIdempotencyConflict("runtime_idempotency_conflict")
        return existing[0]

    def preview(
        self, question: Any, *, budget_profile: Any, request_id: Any, mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_reviewed_request(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        sidecar = production_runtime_status() if self.production else _temporary_sidecar_status(self.sidecar_path)
        return _preview_result(
            delivery, artifact, request, sidecar, mock_transport=self.mock_transport,
        )

    def preview_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_bounded_reviewed_request(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        sidecar = (
            production_runtime_status()
            if self.production
            else _temporary_sidecar_status(self.sidecar_path)
        )
        return _preview_result(
            delivery,
            artifact,
            request,
            sidecar,
            mock_transport=self.mock_transport,
        )

    def preview_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_cognitive_answer_reviewed_request(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        sidecar = (
            production_runtime_status()
            if self.production
            else _temporary_sidecar_status(self.sidecar_path)
        )
        return _preview_result(
            delivery,
            artifact,
            request,
            sidecar,
            mock_transport=self.mock_transport,
        )

    def execute(
        self,
        question: Any,
        *,
        budget_profile: Any,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise RuntimePreviewMismatch("explicit_runtime_confirmation_required")
        expected = _expected_bindings(expected_bindings)
        delivery, artifact, request = _build_reviewed_request(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        return self._execute_built(delivery, artifact, request, expected)

    def execute_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise RuntimePreviewMismatch("explicit_runtime_confirmation_required")
        expected = _expected_bindings(expected_bindings)
        delivery, artifact, request = _build_bounded_reviewed_request(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        return self._execute_built(delivery, artifact, request, expected)

    def execute_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise RuntimePreviewMismatch("explicit_runtime_confirmation_required")
        expected = _expected_bindings(expected_bindings)
        delivery, artifact, request = _build_cognitive_answer_reviewed_request(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=self.delivery_builder,
        )
        return self._execute_built(delivery, artifact, request, expected)

    def _execute_built(
        self,
        delivery: dict[str, Any],
        artifact: dict[str, Any],
        request: dict[str, Any],
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        if expected != request["reviewed_execution_binding"]:
            raise RuntimePreviewMismatch("runtime_preview_binding_mismatch")
        prepared = prepare_v4_flash_request(artifact)

        with _open_validated_sidecar(self.sidecar_path, write=False) as connection:
            existing = self._existing_run(connection, request, expected)
            if existing is not None:
                result = _result_for_run(connection, existing)
                return _execution_envelope(
                    result,
                    adapter_constructor_count=0,
                    adapter_call_count=0,
                    mock_transport=self.mock_transport,
                )

        adapter = self._construct_adapter()
        adapter_constructor_count = 1

        with _open_validated_sidecar(self.sidecar_path, write=True) as connection:
            existing = self._existing_run(connection, request, expected)
            if existing is not None:
                result = _result_for_run(connection, existing)
                return _execution_envelope(
                    result,
                    adapter_constructor_count=adapter_constructor_count,
                    adapter_call_count=0,
                    mock_transport=self.mock_transport,
                )
            run = self._prepare(connection, delivery, artifact, request)
            self._append_state_running(connection, run["model_run_id"])
            connection.commit()
            self._append_send_attempt(connection, run["model_run_id"])
            connection.commit()

        adapter_call_count = 1
        try:
            response = adapter.send(prepared)
            if not isinstance(response, DeepSeekV4Response):
                raise DeepSeekV4ProviderError(
                    "provider_response_invalid", category="provider", retryable=False,
                )
            _validate_cognitive_answer_provider_response(artifact, response)
        except DeepSeekV4ProviderError as exc:
            try:
                result = self._persist_failure(
                    run,
                    artifact,
                    category=exc.category,
                    code=exc.code,
                    retryable=exc.retryable,
                    http_status=exc.http_status,
                )
            except Exception:
                result = self._read_in_doubt(run["model_run_id"])
            return _execution_envelope(
                result,
                adapter_constructor_count=adapter_constructor_count,
                adapter_call_count=adapter_call_count,
                mock_transport=self.mock_transport,
            )
        except Exception:
            try:
                result = self._persist_failure(
                    run,
                    artifact,
                    category="adapter",
                    code="adapter_unexpected_error",
                    retryable=False,
                    http_status=None,
                )
            except Exception:
                result = self._read_in_doubt(run["model_run_id"])
            return _execution_envelope(
                result,
                adapter_constructor_count=adapter_constructor_count,
                adapter_call_count=adapter_call_count,
                mock_transport=self.mock_transport,
            )

        try:
            self._append_provider_response(run["model_run_id"], response)
            result = self._persist_success(run, artifact, response)
        except Exception:
            result = self._read_in_doubt(run["model_run_id"])
        return _execution_envelope(
            result,
            adapter_constructor_count=adapter_constructor_count,
            adapter_call_count=adapter_call_count,
            mock_transport=self.mock_transport,
        )

    def get_run(self, model_run_id: Any) -> dict[str, Any]:
        if not isinstance(model_run_id, str) or not _RUN_RE.fullmatch(model_run_id):
            raise ProductionRuntimeExecutionError("model_run_id_invalid")
        with _open_validated_sidecar(self.sidecar_path, write=False) as connection:
            run = connection.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run_id,),
            ).fetchone()
            if run is None:
                raise ProductionRuntimeExecutionError("model_run_not_found")
            return _result_for_run(connection, run)

    def _prepare(
        self,
        connection: sqlite3.Connection,
        delivery: dict[str, Any],
        artifact: dict[str, Any],
        request: dict[str, Any],
    ) -> sqlite3.Row:
        expected = request["diagnostic"]
        now = _utc_now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO runtime_conversations(conversation_id,created_at) VALUES(?,?)",
                (expected["conversation_id"], now),
            )
            artifact_json = canonical_json(artifact)
            delivery_json = canonical_json(delivery)
            stored = connection.execute(
                "SELECT artifact_json,delivery_json FROM runtime_request_artifacts WHERE artifact_identity=?",
                (expected["artifact_identity"],),
            ).fetchone()
            if stored is None:
                connection.execute(
                    """INSERT INTO runtime_request_artifacts(
                        artifact_identity,artifact_version,runtime_profile_version,renderer_version,
                        delivery_identity,delivery_json,approved_profile_projection_identity,
                        provider,model,base_url,stream,thinking_type,wire_body_sha256,
                        artifact_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        expected["artifact_identity"], artifact["artifact_version"], RUNTIME_PROFILE_VERSION,
                        RENDERER_VERSION, expected["delivery_identity"], delivery_json,
                        expected["profile_projection_identity"], PROVIDER_ID, MODEL_ID, BASE_URL,
                        0, "disabled", expected["wire_body_sha256"], artifact_json, now,
                    ),
                )
            elif stored["artifact_json"] != artifact_json or stored["delivery_json"] != delivery_json:
                raise RuntimeSidecarUnavailable("runtime_artifact_identity_collision")
            connection.execute(
                """INSERT INTO runtime_model_runs(
                    model_run_id,request_id,mutation_id,request_fingerprint,
                    conversation_id,artifact_identity,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    expected["model_run_id"], request["request_id"], request["mutation_id"],
                    expected["request_fingerprint"], expected["conversation_id"],
                    expected["artifact_identity"], now,
                ),
            )
            connection.execute(
                """INSERT INTO runtime_messages(
                    message_id,conversation_id,model_run_id,role,content,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    message_identity(expected["model_run_id"], "user"),
                    expected["conversation_id"], expected["model_run_id"], "user",
                    request["question"], now,
                ),
            )
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    event_identity(expected["model_run_id"], "state_prepared"),
                    expected["model_run_id"], "state_prepared", None, "prepared", now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        run = connection.execute(
            "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (expected["model_run_id"],),
        ).fetchone()
        if run is None:
            raise RuntimeSidecarUnavailable("runtime_prepare_failed")
        _validate_schema_and_ledger(connection)
        return run

    def _append_state_running(self, connection: sqlite3.Connection, model_run_id: str) -> None:
        projection = _run_projection(connection, model_run_id)
        if projection["status"] != "prepared" or len(projection["rows"]) != 1:
            raise RuntimeIdempotencyConflict("runtime_run_not_prepared")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO runtime_run_events(
                event_id,model_run_id,event_type,from_status,to_status,occurred_at
            ) VALUES(?,?,?,?,?,?)""",
            (
                event_identity(model_run_id, "state_running"), model_run_id,
                "state_running", "prepared", "running", _utc_now(),
            ),
        )

    def _append_send_attempt(self, connection: sqlite3.Connection, model_run_id: str) -> None:
        projection = _run_projection(connection, model_run_id)
        if (
            projection["status"] != "running"
            or projection["send_attempted"]
            or projection["provider_response_received"]
        ):
            raise RuntimeIdempotencyConflict("runtime_send_boundary_invalid")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO runtime_run_events(
                event_id,model_run_id,event_type,occurred_at
            ) VALUES(?,?,?,?)""",
            (
                event_identity(model_run_id, "send_attempt_started"), model_run_id,
                "send_attempt_started", _utc_now(),
            ),
        )

    def _append_provider_response(
        self, model_run_id: str, response: DeepSeekV4Response,
    ) -> None:
        with _open_validated_sidecar(self.sidecar_path, write=True) as connection:
            projection = _run_projection(connection, model_run_id)
            if (
                projection["status"] != "running"
                or not projection["send_attempted"]
                or projection["provider_response_received"]
            ):
                raise RuntimeSidecarUnavailable("provider_response_boundary_invalid")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,usage_json,provider_response_id,
                        finish_reason,response_content_sha256,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        event_identity(model_run_id, "provider_response_received"), model_run_id,
                        "provider_response_received", canonical_json(response.usage),
                        response.provider_response_id, response.finish_reason,
                        text_sha256(response.content), _utc_now(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _persist_success(
        self, run: sqlite3.Row, artifact: dict[str, Any], response: DeepSeekV4Response,
    ) -> dict[str, Any]:
        model_run_id = run["model_run_id"]
        assistant_id = message_identity(model_run_id, "assistant")
        now = _utc_now()
        with _open_validated_sidecar(self.sidecar_path, write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO runtime_messages(
                        message_id,conversation_id,model_run_id,role,content,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        assistant_id, run["conversation_id"], model_run_id,
                        "assistant", response.content, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,from_status,to_status,assistant_message_id,
                        usage_json,provider_response_id,finish_reason,response_content_sha256,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_identity(model_run_id, "state_succeeded"), model_run_id,
                        "state_succeeded", "running", "succeeded", assistant_id,
                        canonical_json(response.usage), response.provider_response_id,
                        response.finish_reason, text_sha256(response.content), now,
                    ),
                )
                receipt = self._build_receipt(connection, run, artifact)
                connection.execute(
                    "INSERT INTO runtime_receipts(model_run_id,receipt_identity,receipt_json) VALUES(?,?,?)",
                    (model_run_id, receipt["receipt_identity"], canonical_json(receipt)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return receipt

    def _persist_failure(
        self,
        run: sqlite3.Row,
        artifact: dict[str, Any],
        *,
        category: str,
        code: str,
        retryable: bool,
        http_status: int | None,
    ) -> dict[str, Any]:
        model_run_id = run["model_run_id"]
        error_id = error_identity(model_run_id, code)
        now = _utc_now()
        with _open_validated_sidecar(self.sidecar_path, write=True) as connection:
            projection = _run_projection(connection, model_run_id)
            if (
                projection["status"] != "running"
                or not projection["send_attempted"]
                or projection["provider_response_received"]
            ):
                raise RuntimeSidecarUnavailable("runtime_failure_boundary_invalid")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO runtime_errors(
                        error_id,model_run_id,category,code,retryable,http_status,
                        sent_to_model,external_model_calls,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        error_id, model_run_id, str(category), str(code), int(bool(retryable)),
                        http_status, 1, 1, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,from_status,to_status,error_id,occurred_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        event_identity(model_run_id, "state_failed"), model_run_id,
                        "state_failed", "running", "failed", error_id, now,
                    ),
                )
                receipt = self._build_receipt(connection, run, artifact)
                connection.execute(
                    "INSERT INTO runtime_receipts(model_run_id,receipt_identity,receipt_json) VALUES(?,?,?)",
                    (model_run_id, receipt["receipt_identity"], canonical_json(receipt)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return receipt

    def _build_receipt(
        self, connection: sqlite3.Connection, run: sqlite3.Row, artifact: dict[str, Any],
    ) -> dict[str, Any]:
        projection = _run_projection(connection, run["model_run_id"])
        messages = {
            row["message_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM runtime_messages WHERE model_run_id=?", (run["model_run_id"],),
            )
        }
        errors = {
            row["error_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM runtime_errors WHERE model_run_id=?", (run["model_run_id"],),
            )
        }
        try:
            return build_runtime_receipt(dict(run), artifact, projection, messages, errors)
        except RuntimeProtocolContractError as exc:
            raise RuntimeSidecarUnavailable(exc.reason_code) from None

    def _read_in_doubt(self, model_run_id: str) -> dict[str, Any]:
        with _open_validated_sidecar(self.sidecar_path, write=False) as connection:
            run = connection.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeSidecarUnavailable("model_run_not_found")
            return _result_for_run(connection, run)


def _build_production_core_factory():
    token = object()

    def create(authority: ProductionRuntimeExecutionSession) -> _RuntimeCore:
        if token is None or not is_trusted_production_session(authority):
            raise ProductionRuntimeExecutionError("production_runtime_authority_required")
        return _RuntimeCore(
            sidecar_path=PRODUCTION_RUNTIME_SIDECAR,
            adapter_factory=DeepSeekV4FlashHTTPAdapter,
            delivery_builder=_production_delivery,
            production=True,
            mock_transport=False,
        )

    return create


_create_production_core = _build_production_core_factory()
del _build_production_core_factory


class ProductionRuntimeExecutionService:
    """Fixed facade: no caller-supplied path, dependency, or transport override."""

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
        return _preview_result(
            delivery,
            artifact,
            request,
            production_runtime_status(),
            mock_transport=False,
        )

    def preview_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_bounded_reviewed_request(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=_production_delivery,
        )
        return _preview_result(
            delivery,
            artifact,
            request,
            production_runtime_status(),
            mock_transport=False,
        )

    def preview_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        delivery, artifact, request = _build_cognitive_answer_reviewed_request(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
            delivery_builder=_production_delivery,
        )
        return _preview_result(
            delivery,
            artifact,
            request,
            production_runtime_status(),
            mock_transport=False,
        )

    def execute(
        self,
        question: Any,
        *,
        budget_profile: Any,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: ProductionRuntimeExecutionSession,
    ) -> dict[str, Any]:
        core = _create_production_core(authority)
        return core.execute(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def execute_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: ProductionRuntimeExecutionSession,
    ) -> dict[str, Any]:
        core = _create_production_core(authority)
        return core.execute_bounded_tool(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def execute_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: ProductionRuntimeExecutionSession,
    ) -> dict[str, Any]:
        core = _create_production_core(authority)
        return core.execute_answer(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def get_run(self, model_run_id: Any) -> dict[str, Any]:
        core = _RuntimeCore(
            sidecar_path=PRODUCTION_RUNTIME_SIDECAR,
            adapter_factory=DeepSeekV4FlashHTTPAdapter,
            delivery_builder=_production_delivery,
            production=True,
            mock_transport=False,
        )
        return core.get_run(model_run_id)


class _VisibleMockAdapter:
    provider_id = PROVIDER_ID
    model_id = MODEL_ID
    base_url = BASE_URL

    def __init__(self) -> None:
        self.calls = 0

    def send(self, prepared_request: Any) -> DeepSeekV4Response:
        prepare_v4_flash_request(json.loads(prepared_request.artifact_canonical_bytes.decode("utf-8")))
        self.calls += 1
        return DeepSeekV4Response(
            provider_response_id="mock-response-v8-0-g",
            model=MODEL_ID,
            content=(
                "Mock success: the reviewed D1 artifact reached the fixed execution boundary. "
                "No credential was read and no provider network request was made."
            ),
            usage={"prompt_tokens": 96, "completion_tokens": 24, "total_tokens": 120},
            finish_reason="stop",
        )


class _MockExecutionSession:
    pass


class MockRuntimeExecutionService:
    """Explicit temporary-sidecar/mock-transport service for tests and UI acceptance."""

    def __init__(self, sidecar_path: str | Path) -> None:
        path = Path(sidecar_path).expanduser().resolve()
        if path == PRODUCTION_RUNTIME_SIDECAR.resolve():
            raise ProductionRuntimeExecutionError("mock_service_cannot_target_production")
        self.execution_session = _MockExecutionSession()
        self._core = _RuntimeCore(
            sidecar_path=path,
            adapter_factory=_VisibleMockAdapter,
            delivery_builder=_production_delivery,
            production=False,
            mock_transport=True,
        )

    def preview(
        self, question: Any, *, budget_profile: Any, request_id: Any, mutation_id: Any,
    ) -> dict[str, Any]:
        return self._core.preview(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
        )

    def preview_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        return self._core.preview_bounded_tool(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
        )

    def preview_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
    ) -> dict[str, Any]:
        return self._core.preview_answer(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
        )

    def execute(
        self,
        question: Any,
        *,
        budget_profile: Any,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: Any,
    ) -> dict[str, Any]:
        if authority is not self.execution_session:
            raise ProductionRuntimeExecutionError("mock_runtime_authority_required")
        return self._core.execute(
            question,
            budget_profile=budget_profile,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def execute_bounded_tool(
        self,
        bounded_request: BoundedToolRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: Any,
    ) -> dict[str, Any]:
        if authority is not self.execution_session:
            raise ProductionRuntimeExecutionError("mock_runtime_authority_required")
        return self._core.execute_bounded_tool(
            bounded_request,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def execute_answer(
        self,
        answer_request: CognitiveAnswerRuntimeRequest,
        *,
        request_id: Any,
        mutation_id: Any,
        expected_bindings: Any,
        confirmed: Any,
        authority: Any,
    ) -> dict[str, Any]:
        if authority is not self.execution_session:
            raise ProductionRuntimeExecutionError("mock_runtime_authority_required")
        return self._core.execute_answer(
            answer_request,
            request_id=request_id,
            mutation_id=mutation_id,
            expected_bindings=expected_bindings,
            confirmed=confirmed,
        )

    def get_run(self, model_run_id: Any) -> dict[str, Any]:
        return self._core.get_run(model_run_id)


def create_mock_runtime_execution_service(sidecar_path: str | Path) -> MockRuntimeExecutionService:
    return MockRuntimeExecutionService(sidecar_path)


__all__ = [
    "MockRuntimeExecutionService",
    "PROTOCOL_VERSION",
    "ProductionRuntimeExecutionError",
    "ProductionRuntimeExecutionService",
    "ProductionRuntimeExecutionSession",
    "RuntimeIdempotencyConflict",
    "RuntimePreviewMismatch",
    "RuntimeSidecarUnavailable",
    "create_mock_runtime_execution_service",
    "is_trusted_production_session",
    "open_production_runtime_execution",
]
