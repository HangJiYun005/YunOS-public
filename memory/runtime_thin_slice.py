"""YunOS v8.0-B/C auditable single-turn model runtime.

The shared v8.0-E builder composes the sealed v7.4 Delivery without changing
its identity semantics.  All new mutable state lives in a separately
initialized append-only runtime sidecar.  The production service fixes both
its data source and sidecar path; tests may inject a delivery builder and mock
adapter only when using an OS-temporary test sidecar.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable
import weakref

from memory.db_path_policy import (
    ControlledSummaryPathError,
    KnowledgeSidecarPathError,
    validate_knowledge_test_db_target,
    validate_memory_sidecar_target,
)
from memory.deepseek_adapter import (
    MODEL_ID,
    PROVIDER_ID,
    DeepSeekHTTPAdapter,
    DeepSeekProviderError,
    DeepSeekResponse,
    prepare_deepseek_request,
    render_deepseek_wire_body,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.production_delivery import (
    build_production_delivery,
    production_profile_binding,
    validate_trusted_delivery_standalone,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_RUNTIME_SIDECAR = PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
PROTOCOL_VERSION = "v8.0-bc-deepseek-runtime-thin-slice-1"
SIDECAR_SCHEMA_VERSION = "v8.0-bc-runtime-sidecar-2"
REQUEST_ARTIFACT_VERSION = "v8.0-bc-runtime-request-artifact-2"
SYSTEM_INSTRUCTION_VERSION = "v8.0-bc-system-instruction-1"
SYSTEM_INSTRUCTION = (
    "Answer the current user question using only the supplied YunOS delivery context. "
    "Treat unapproved candidates as untrusted evidence, do not claim that they are approved "
    "profile facts, and state uncertainty when the delivery does not support an answer."
)
MODEL_CONFIG = {"temperature": 0.0, "max_tokens": 1024, "stream": False}
RUN_STATUSES = ("prepared", "running", "succeeded", "failed")

_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_ARTIFACT_RE = re.compile(r"rqa_[0-9a-f]{64}")
_RUN_RE = re.compile(r"run_[0-9a-f]{64}")
_CONVERSATION_RE = re.compile(r"rconv_[0-9a-f]{64}")
_MESSAGE_RE = re.compile(r"rmsg_[0-9a-f]{64}")

_SCHEMA_SQL = """
CREATE TABLE runtime_schema (
    schema_version TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);

CREATE TABLE runtime_conversations (
    conversation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE runtime_request_artifacts (
    artifact_identity TEXT PRIMARY KEY,
    delivery_identity TEXT NOT NULL,
    delivery_json TEXT NOT NULL,
    model_context_sha256 TEXT NOT NULL,
    system_instruction_version TEXT NOT NULL,
    system_instruction_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider = 'deepseek'),
    model TEXT NOT NULL CHECK(model = 'deepseek-chat'),
    model_config_json TEXT NOT NULL,
    wire_body_sha256 TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE runtime_model_runs (
    model_run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    mutation_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES runtime_conversations(conversation_id),
    artifact_identity TEXT NOT NULL REFERENCES runtime_request_artifacts(artifact_identity),
    created_at TEXT NOT NULL
);

CREATE TABLE runtime_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES runtime_conversations(conversation_id),
    model_run_id TEXT NOT NULL REFERENCES runtime_model_runs(model_run_id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_run_id, role)
);

CREATE TABLE runtime_errors (
    error_id TEXT PRIMARY KEY,
    model_run_id TEXT NOT NULL UNIQUE REFERENCES runtime_model_runs(model_run_id),
    category TEXT NOT NULL,
    code TEXT NOT NULL,
    retryable INTEGER NOT NULL CHECK(retryable IN (0,1)),
    http_status INTEGER,
    sent_to_model INTEGER NOT NULL CHECK(sent_to_model IN (0,1)),
    external_model_calls INTEGER NOT NULL CHECK(external_model_calls IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE runtime_run_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    model_run_id TEXT NOT NULL REFERENCES runtime_model_runs(model_run_id),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'state_prepared','state_running','send_attempt_started',
        'provider_response_received','state_succeeded','state_failed'
    )),
    from_status TEXT CHECK(from_status IS NULL OR from_status IN ('prepared','running','succeeded','failed')),
    to_status TEXT CHECK(to_status IS NULL OR to_status IN ('prepared','running','succeeded','failed')),
    assistant_message_id TEXT REFERENCES runtime_messages(message_id),
    error_id TEXT REFERENCES runtime_errors(error_id),
    usage_json TEXT,
    provider_response_id TEXT,
    finish_reason TEXT,
    response_content_sha256 TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX idx_runtime_run_events_run ON runtime_run_events(model_run_id, sequence);

CREATE TABLE runtime_receipts (
    model_run_id TEXT PRIMARY KEY REFERENCES runtime_model_runs(model_run_id),
    receipt_identity TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
);
"""

_APPEND_ONLY_TABLES = (
    "runtime_schema",
    "runtime_conversations",
    "runtime_request_artifacts",
    "runtime_model_runs",
    "runtime_messages",
    "runtime_errors",
    "runtime_run_events",
    "runtime_receipts",
)

_EXPECTED_COLUMNS = {
    "runtime_schema": {"schema_version", "initialized_at"},
    "runtime_conversations": {"conversation_id", "created_at"},
    "runtime_request_artifacts": {
        "artifact_identity", "delivery_identity", "delivery_json", "model_context_sha256",
        "system_instruction_version", "system_instruction_sha256", "provider", "model",
        "model_config_json", "wire_body_sha256", "artifact_json", "created_at",
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


class RuntimeThinSliceError(ValueError):
    """The runtime request, authority, or sidecar is invalid."""


class RuntimeSidecarUnavailable(RuntimeThinSliceError):
    """The independent runtime sidecar is unavailable or invalid."""


class RuntimeIdempotencyConflict(RuntimeThinSliceError):
    """A request or mutation identifier was reused with different input."""


class RuntimeRunInDoubt(RuntimeThinSliceError):
    """A prior attempt reached running and must be explicitly recovered."""


class RuntimeExecutionSession:
    """Unforgeable process-local authority for production external execution."""

    def __new__(cls):
        raise RuntimeThinSliceError("runtime execution sessions are issued only by the explicit opener")


def _build_runtime_session_issuer():
    issued: weakref.WeakSet[RuntimeExecutionSession] = weakref.WeakSet()
    authorized_codes: set[Any] = set()

    def issue() -> RuntimeExecutionSession:
        if sys._getframe(1).f_code not in authorized_codes:
            raise RuntimeThinSliceError("runtime execution session issuer is not authorized")
        session = object.__new__(RuntimeExecutionSession)
        issued.add(session)
        return session

    def is_trusted(value: Any) -> bool:
        return isinstance(value, RuntimeExecutionSession) and value in issued

    def authorize(code: Any) -> None:
        authorized_codes.add(code)

    return issue, is_trusted, authorize


_issue_runtime_session, is_trusted_runtime_session, _authorize_runtime_issuer = (
    _build_runtime_session_issuer()
)


def open_production_runtime_execution(
    *, execute: bool, allow_external_model_call: bool, confirm_deepseek_send: bool,
) -> RuntimeExecutionSession:
    """Enter explicit production execution mode; this initializes no sidecar."""
    values = (execute, allow_external_model_call, confirm_deepseek_send)
    if any(value is not True for value in values):
        raise RuntimeThinSliceError("all runtime execution confirmations must be boolean true")
    return _issue_runtime_session()


_authorize_runtime_issuer(open_production_runtime_execution.__code__)
del _authorize_runtime_issuer


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _validated_target(path: str | Path) -> Path:
    candidate = _absolute(path)
    production = _absolute(PRODUCTION_RUNTIME_SIDECAR)
    try:
        if candidate == production:
            _, existing_memory_target = validate_memory_sidecar_target(candidate)
            if existing_memory_target:
                raise RuntimeThinSliceError("runtime sidecar must not identify the memory sidecar")
            target, is_runtime = validate_memory_sidecar_target(
                candidate, production_memory_path=production,
            )
            if not is_runtime:
                raise RuntimeThinSliceError("production runtime sidecar identity mismatch")
            return target
        return validate_knowledge_test_db_target(candidate)
    except (ControlledSummaryPathError, KnowledgeSidecarPathError) as exc:
        raise RuntimeThinSliceError("runtime sidecar path is not allowed") from exc


def is_production_runtime_sidecar(path: str | Path) -> bool:
    return _absolute(path) == _absolute(PRODUCTION_RUNTIME_SIDECAR)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _append_only_trigger_name(table: str, action: str) -> str:
    return f"{table}_no_{action.lower()}"


def _append_only_trigger_sql(table: str, action: str) -> str:
    trigger = _append_only_trigger_name(table, action)
    return (
        f"CREATE TRIGGER {trigger} BEFORE {action} ON {table} BEGIN "
        f"SELECT RAISE(ABORT, 'runtime ledger is append-only'); END;"
    )


def _validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT initialized_at FROM runtime_schema WHERE schema_version = ?",
        (SIDECAR_SCHEMA_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeSidecarUnavailable("runtime sidecar schema version mismatch")
    for table, expected in _EXPECTED_COLUMNS.items():
        actual = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        if actual != expected:
            raise RuntimeSidecarUnavailable("runtime sidecar schema mismatch")
        triggers = {
            item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,),
            )
        }
        expected_triggers = {
            _append_only_trigger_name(table, "UPDATE"),
            _append_only_trigger_name(table, "DELETE"),
        }
        if triggers != expected_triggers:
            raise RuntimeSidecarUnavailable("runtime append-only triggers are missing")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeSidecarUnavailable("runtime sidecar integrity check failed")


def runtime_sidecar_status(path: str | Path = PRODUCTION_RUNTIME_SIDECAR) -> dict[str, Any]:
    try:
        target = _validated_target(path)
    except RuntimeThinSliceError:
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_path_invalid"}
    if not target.is_file():
        return {"status": "missing", "initialized": False, "reason_code": "sidecar_not_initialized"}
    try:
        with closing(_connect_readonly(target)) as conn:
            _validate_schema(conn)
    except (sqlite3.Error, RuntimeThinSliceError):
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_invalid"}
    return {"status": "ready", "initialized": True, "reason_code": "ready"}


def initialize_runtime_sidecar(
    path: str | Path = PRODUCTION_RUNTIME_SIDECAR,
    *,
    execute: bool = False,
    test_mode: bool = False,
    allow_production_runtime_sidecar: bool = False,
    confirm_production_runtime_sidecar_init: bool = False,
) -> dict[str, Any]:
    """Create the independent sidecar only behind explicit strict confirmations."""
    confirmations = {
        "execute": execute,
        "allow_production_runtime_sidecar": allow_production_runtime_sidecar,
        "confirm_production_runtime_sidecar_init": confirm_production_runtime_sidecar_init,
    }
    if any(value is not True and value is not False for value in confirmations.values()):
        return {
            "status": "refused", "executed": False, "target_kind": "invalid_confirmation",
            "reason_code": "confirmation_type_invalid",
        }
    if test_mode is not True and test_mode is not False:
        return {
            "status": "refused", "executed": False, "target_kind": "invalid_confirmation",
            "reason_code": "confirmation_type_invalid",
        }
    try:
        target = _validated_target(path)
    except RuntimeThinSliceError:
        return {
            "status": "refused", "executed": False, "target_kind": "invalid_runtime_sidecar",
            "reason_code": "sidecar_path_invalid",
        }
    production = is_production_runtime_sidecar(target)
    target_kind = "production_runtime_sidecar" if production else "test_runtime_sidecar"
    existing = runtime_sidecar_status(target)
    if existing["status"] == "ready":
        return {"status": "already_initialized", "executed": False, "target_kind": target_kind}
    if target.exists():
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "existing_target_invalid",
        }
    if not production and test_mode is not True:
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "test_mode_required",
        }
    if execute is not True:
        return {"status": "would_initialize", "executed": False, "target_kind": target_kind}
    if production and not (
        allow_production_runtime_sidecar is True
        and confirm_production_runtime_sidecar_init is True
    ):
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "production_confirmation_required",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        created = True
        with closing(sqlite3.connect(target)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA_SQL)
            for table in _APPEND_ONLY_TABLES:
                conn.execute(_append_only_trigger_sql(table, "UPDATE"))
                conn.execute(_append_only_trigger_sql(table, "DELETE"))
            conn.execute(
                "INSERT INTO runtime_schema(schema_version, initialized_at) VALUES(?, ?)",
                (SIDECAR_SCHEMA_VERSION, _utc_now()),
            )
            conn.commit()
        if runtime_sidecar_status(target)["status"] != "ready":
            raise RuntimeSidecarUnavailable("initialized runtime sidecar failed validation")
    except FileExistsError:
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "target_appeared_during_initialization",
        }
    except Exception:
        if created and target.exists():
            target.unlink()
        raise
    return {"status": "initialized", "executed": True, "target_kind": target_kind}


def _default_profile_binding(delivery: dict[str, Any]) -> dict[str, Any]:
    items = delivery["layers"]["approved_profile"]["items"]
    return {
        "authority": "delivery_embedded_approved_profile",
        "projection_identity": None,
        "revision": None,
        "decision_count": None,
        "model_visible": bool(items),
        "profile_item_ids": [item["profile_item_id"] for item in items],
    }


def _production_runtime_delivery(
    query: RetrievalQuery, *, budget_profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the same v8.0-E production Delivery used by Gate C UI."""
    delivery = build_production_delivery(query, budget_profile=budget_profile)
    source_binding = production_profile_binding(delivery)
    binding = {
        "authority": source_binding["authority"],
        "projection_identity": source_binding["projection_identity"],
        "revision": source_binding["revision"],
        "decision_count": source_binding["decision_count"],
        "model_visible": source_binding["model_visible"],
        "profile_item_ids": source_binding["profile_item_ids"],
    }
    return delivery, binding


def _delivery_evidence_ids(delivery: dict[str, Any]) -> list[str]:
    return [
        item["evidence_reference_id"]
        for item in delivery["layers"]["historical_evidence"]["items"]
    ]


def _resolve_delivery_result(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(value, tuple) and len(value) == 2:
        delivery, binding = value
    else:
        delivery, binding = value, None
    validate_trusted_delivery_standalone(delivery)
    return delivery, deepcopy(binding) if binding is not None else _default_profile_binding(delivery)


def build_runtime_request_artifact(
    delivery: dict[str, Any], *, approved_profile_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the exact trusted Delivery contents to one deterministic payload."""
    validate_trusted_delivery_standalone(delivery)
    model_context = delivery["model_context"]
    if not isinstance(model_context, str) or not model_context:
        raise RuntimeThinSliceError("delivery model context is invalid")
    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": model_context},
        ],
        "temperature": MODEL_CONFIG["temperature"],
        "max_tokens": MODEL_CONFIG["max_tokens"],
        "stream": MODEL_CONFIG["stream"],
    }
    wire_body = render_deepseek_wire_body(payload)
    artifact = {
        "protocol_version": PROTOCOL_VERSION,
        "artifact_version": REQUEST_ARTIFACT_VERSION,
        "artifact_identity": "",
        "delivery_binding": {
            "protocol_version": delivery["protocol_version"],
            "delivery_identity_version": delivery["delivery_identity_version"],
            "delivery_identity": delivery["delivery_identity"],
            "assembly_identity": delivery["assembly_binding"]["assembly_identity"],
            "model_context_sha256": _text_sha256(model_context),
            "model_context_characters": len(model_context),
            "profile_item_ids": [
                item["profile_item_id"]
                for item in delivery["layers"]["approved_profile"]["items"]
            ],
            "evidence_reference_ids": _delivery_evidence_ids(delivery),
        },
        "approved_profile_binding": deepcopy(
            approved_profile_binding
            if approved_profile_binding is not None
            else _default_profile_binding(delivery)
        ),
        "system_instruction": {
            "version": SYSTEM_INSTRUCTION_VERSION,
            "sha256": _text_sha256(SYSTEM_INSTRUCTION),
        },
        "provider": {"id": PROVIDER_ID, "model": MODEL_ID},
        "model_config": deepcopy(MODEL_CONFIG),
        "payload": payload,
        "wire_body_base64": base64.b64encode(wire_body).decode("ascii"),
        "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
    }
    identity_payload = {key: value for key, value in artifact.items() if key != "artifact_identity"}
    artifact["artifact_identity"] = "rqa_" + _digest(identity_payload)
    validate_runtime_request_artifact(artifact, trusted_delivery=delivery)
    return artifact


def validate_runtime_request_artifact(
    artifact: dict[str, Any], *, trusted_delivery: dict[str, Any] | None = None,
) -> None:
    if not isinstance(artifact, dict) or set(artifact) != {
        "protocol_version", "artifact_version", "artifact_identity", "delivery_binding",
        "approved_profile_binding", "system_instruction", "provider", "model_config",
        "payload", "wire_body_base64", "wire_body_sha256",
    }:
        raise RuntimeThinSliceError("runtime request artifact fields mismatch")
    if artifact["protocol_version"] != PROTOCOL_VERSION or artifact["artifact_version"] != REQUEST_ARTIFACT_VERSION:
        raise RuntimeThinSliceError("runtime request artifact version mismatch")
    if not _ARTIFACT_RE.fullmatch(str(artifact["artifact_identity"])):
        raise RuntimeThinSliceError("runtime request artifact identity invalid")
    binding = artifact["delivery_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "protocol_version", "delivery_identity_version", "delivery_identity", "assembly_identity",
        "model_context_sha256", "model_context_characters", "profile_item_ids",
        "evidence_reference_ids",
    }:
        raise RuntimeThinSliceError("runtime delivery binding is invalid")
    if artifact["system_instruction"] != {
        "version": SYSTEM_INSTRUCTION_VERSION,
        "sha256": _text_sha256(SYSTEM_INSTRUCTION),
    }:
        raise RuntimeThinSliceError("runtime system instruction binding mismatch")
    profile_binding = artifact["approved_profile_binding"]
    if not isinstance(profile_binding, dict) or set(profile_binding) != {
        "authority", "projection_identity", "revision", "decision_count", "model_visible",
        "profile_item_ids",
    }:
        raise RuntimeThinSliceError("runtime approved profile binding is invalid")
    if artifact["provider"] != {"id": PROVIDER_ID, "model": MODEL_ID}:
        raise RuntimeThinSliceError("runtime provider binding mismatch")
    if artifact["model_config"] != MODEL_CONFIG:
        raise RuntimeThinSliceError("runtime model configuration mismatch")
    payload = artifact["payload"]
    expected_payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": payload.get("messages", [{}, {}])[1].get("content", "")}
            if isinstance(payload, dict) and isinstance(payload.get("messages"), list) and len(payload["messages"]) == 2
            else {},
        ],
        "temperature": MODEL_CONFIG["temperature"],
        "max_tokens": MODEL_CONFIG["max_tokens"],
        "stream": False,
    }
    if not isinstance(payload, dict) or payload != expected_payload or not expected_payload["messages"][1].get("content"):
        raise RuntimeThinSliceError("runtime request payload mismatch")
    try:
        prepare_deepseek_request(artifact)
    except DeepSeekProviderError as exc:
        raise RuntimeThinSliceError("runtime request wire body binding mismatch") from exc
    identity_payload = {key: value for key, value in artifact.items() if key != "artifact_identity"}
    if artifact["artifact_identity"] != "rqa_" + _digest(identity_payload):
        raise RuntimeThinSliceError("runtime request artifact identity mismatch")
    if trusted_delivery is not None:
        validate_trusted_delivery_standalone(trusted_delivery)
        expected_binding = {
            "protocol_version": trusted_delivery["protocol_version"],
            "delivery_identity_version": trusted_delivery["delivery_identity_version"],
            "delivery_identity": trusted_delivery["delivery_identity"],
            "assembly_identity": trusted_delivery["assembly_binding"]["assembly_identity"],
            "model_context_sha256": _text_sha256(trusted_delivery["model_context"]),
            "model_context_characters": len(trusted_delivery["model_context"]),
            "profile_item_ids": [
                item["profile_item_id"]
                for item in trusted_delivery["layers"]["approved_profile"]["items"]
            ],
            "evidence_reference_ids": _delivery_evidence_ids(trusted_delivery),
        }
        if binding != expected_binding or payload["messages"][1]["content"] != trusted_delivery["model_context"]:
            raise RuntimeThinSliceError("runtime artifact does not match trusted delivery")
        delivery_profile_ids = [
            item["profile_item_id"]
            for item in trusted_delivery["layers"]["approved_profile"]["items"]
        ]
        if profile_binding["profile_item_ids"] != delivery_profile_ids:
            raise RuntimeThinSliceError("runtime approved profile binding does not match delivery")
    if (
        profile_binding["authority"] not in {
            "delivery_embedded_approved_profile", "v8.0-a-approved-profile-projection",
            "v8.0-d1-approved-profile-projection",
        }
        or not isinstance(profile_binding["profile_item_ids"], list)
        or len(set(profile_binding["profile_item_ids"])) != len(profile_binding["profile_item_ids"])
        or any(not re.fullmatch(r"upi_[0-9a-f]{64}", str(value)) for value in profile_binding["profile_item_ids"])
        or profile_binding["model_visible"] is not bool(profile_binding["profile_item_ids"])
    ):
        raise RuntimeThinSliceError("runtime approved profile authority binding is invalid")
    if profile_binding["authority"] in {
        "v8.0-a-approved-profile-projection", "v8.0-d1-approved-profile-projection",
    }:
        if (
            not re.fullmatch(r"app_[0-9a-f]{64}", str(profile_binding["projection_identity"]))
            or isinstance(profile_binding["revision"], bool)
            or not isinstance(profile_binding["revision"], int)
            or profile_binding["revision"] < 0
            or isinstance(profile_binding["decision_count"], bool)
            or not isinstance(profile_binding["decision_count"], int)
            or profile_binding["decision_count"] < 0
        ):
            raise RuntimeThinSliceError("runtime approved profile projection binding is invalid")
    elif profile_binding["projection_identity"] is not None or profile_binding["revision"] is not None or profile_binding["decision_count"] is not None:
        raise RuntimeThinSliceError("runtime embedded profile binding contains projection authority")


def public_request_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    validate_runtime_request_artifact(artifact)
    return {
        key: deepcopy(value)
        for key, value in artifact.items()
        if key not in {"payload", "wire_body_base64"}
    }


def _normalize_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RuntimeThinSliceError(f"{name} is invalid")
    return value


def _normalize_question(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeThinSliceError("question must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 500 or any(ord(char) < 32 for char in normalized):
        raise RuntimeThinSliceError("question must contain 1..500 printable characters")
    return normalized


def _normalize_budget(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeThinSliceError("budget_profile must be Small, Medium, or Large")
    normalized = value.strip().capitalize()
    if normalized not in {"Small", "Medium", "Large"}:
        raise RuntimeThinSliceError("budget_profile must be Small, Medium, or Large")
    return normalized


def _request_fingerprint(
    question: str, budget_profile: str, artifact_identity: str, wire_body_sha256: str,
) -> str:
    return "rrf_" + _digest({
        "question": question,
        "budget_profile": budget_profile,
        "artifact_identity": artifact_identity,
        "wire_body_sha256": wire_body_sha256,
    })


@contextmanager
def _open_validated_sidecar(path: Path, *, write: bool):
    if runtime_sidecar_status(path)["status"] != "ready":
        raise RuntimeSidecarUnavailable("runtime sidecar is not initialized")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not write:
        conn.execute("PRAGMA query_only=ON")
    _validate_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def _load_json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeSidecarUnavailable(f"stored runtime {label} is invalid") from exc


def _event_rows(conn: sqlite3.Connection, model_run_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM runtime_run_events WHERE model_run_id=? ORDER BY sequence", (model_run_id,),
    ))


def _validated_usage(raw: str, label: str) -> dict[str, int]:
    usage = _load_json(raw, label)
    if not isinstance(usage, dict) or set(usage) != {
        "prompt_tokens", "completion_tokens", "total_tokens",
    } or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in usage.values()
    ) or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise RuntimeSidecarUnavailable(f"stored runtime {label} is invalid")
    return usage


def _run_event_projection(rows: list[sqlite3.Row]) -> dict[str, Any]:
    if not rows:
        raise RuntimeSidecarUnavailable("runtime model run has no state event")
    types = [row["event_type"] for row in rows]
    if not types or types[0] != "state_prepared" or types.count("state_prepared") != 1:
        raise RuntimeSidecarUnavailable("runtime state history is invalid")
    if len(types) > 1 and types[1] != "state_running":
        raise RuntimeSidecarUnavailable("runtime state history is invalid")
    if types.count("state_running") > 1 or types.count("send_attempt_started") > 1 \
            or types.count("provider_response_received") > 1:
        raise RuntimeSidecarUnavailable("runtime phase history contains duplicates")
    send_index = types.index("send_attempt_started") if "send_attempt_started" in types else None
    response_index = types.index("provider_response_received") if "provider_response_received" in types else None
    if send_index is not None and send_index != 2:
        raise RuntimeSidecarUnavailable("runtime send boundary is out of order")
    if response_index is not None and (send_index is None or response_index != send_index + 1):
        raise RuntimeSidecarUnavailable("runtime provider response boundary is out of order")
    terminal_types = {"state_succeeded", "state_failed"}
    terminal_indexes = [index for index, value in enumerate(types) if value in terminal_types]
    if len(terminal_indexes) > 1 or (terminal_indexes and terminal_indexes[0] != len(types) - 1):
        raise RuntimeSidecarUnavailable("runtime terminal history is invalid")

    status = "prepared"
    model_run_id = rows[0]["model_run_id"]
    for row in rows:
        if row["model_run_id"] != model_run_id:
            raise RuntimeSidecarUnavailable("runtime event run binding is invalid")
        event_type = row["event_type"]
        current_from = row["from_status"]
        target = row["to_status"]
        expected_event_id = "rev_" + _digest({
            "model_run_id": row["model_run_id"], "event_type": event_type,
        })
        if row["event_id"] != expected_event_id:
            raise RuntimeSidecarUnavailable("runtime state event identity is invalid")
        outcome_fields = (
            "assistant_message_id", "error_id", "usage_json", "provider_response_id",
            "finish_reason", "response_content_sha256",
        )
        if event_type == "state_prepared":
            if current_from is not None or target != "prepared" or any(row[name] is not None for name in outcome_fields):
                raise RuntimeSidecarUnavailable("runtime prepared event is invalid")
            status = "prepared"
        elif event_type == "state_running":
            if current_from != "prepared" or target != "running" or any(row[name] is not None for name in outcome_fields):
                raise RuntimeSidecarUnavailable("runtime running event is invalid")
            status = "running"
        elif event_type == "send_attempt_started":
            if current_from is not None or target is not None or any(row[name] is not None for name in outcome_fields):
                raise RuntimeSidecarUnavailable("runtime send attempt event is invalid")
        elif event_type == "provider_response_received":
            if (
                current_from is not None or target is not None
                or row["assistant_message_id"] is not None or row["error_id"] is not None
                or not row["usage_json"] or not row["provider_response_id"]
                or not row["finish_reason"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(row["response_content_sha256"]))
            ):
                raise RuntimeSidecarUnavailable("runtime provider response event is invalid")
            _validated_usage(row["usage_json"], "provider response usage")
        elif event_type == "state_succeeded":
            if (
                current_from != "running" or target != "succeeded"
                or not row["assistant_message_id"] or row["error_id"] is not None
                or not row["usage_json"] or not row["provider_response_id"]
                or not row["finish_reason"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(row["response_content_sha256"]))
            ):
                raise RuntimeSidecarUnavailable("runtime success event is incomplete")
            _validated_usage(row["usage_json"], "success usage")
            status = "succeeded"
        elif event_type == "state_failed":
            if (
                current_from != "running" or target != "failed" or not row["error_id"]
                or any(row[name] is not None for name in (
                    "assistant_message_id", "usage_json", "provider_response_id",
                    "finish_reason", "response_content_sha256",
                ))
            ):
                raise RuntimeSidecarUnavailable("runtime failure event is invalid")
            status = "failed"
        else:
            raise RuntimeSidecarUnavailable("runtime event type is invalid")
    if "state_succeeded" in types and (send_index is None or response_index is None):
        raise RuntimeSidecarUnavailable("runtime success lacks provider boundaries")
    return {
        "status": status,
        "send_attempted": send_index is not None,
        "provider_response_received": response_index is not None,
    }


def _validate_run_events(rows: list[sqlite3.Row]) -> str:
    return str(_run_event_projection(rows)["status"])


def _load_artifact(conn: sqlite3.Connection, identity: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM runtime_request_artifacts WHERE artifact_identity=?",
        (identity,),
    ).fetchone()
    if row is None:
        raise RuntimeSidecarUnavailable("runtime request artifact is missing")
    artifact = _load_json(row["artifact_json"], "request artifact")
    delivery = _load_json(row["delivery_json"], "delivery")
    validate_runtime_request_artifact(artifact, trusted_delivery=delivery)
    expected_columns = {
        "artifact_identity": artifact["artifact_identity"],
        "delivery_identity": delivery["delivery_identity"],
        "model_context_sha256": artifact["delivery_binding"]["model_context_sha256"],
        "system_instruction_version": SYSTEM_INSTRUCTION_VERSION,
        "system_instruction_sha256": artifact["system_instruction"]["sha256"],
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "model_config_json": _dump(MODEL_CONFIG),
        "wire_body_sha256": artifact["wire_body_sha256"],
        "artifact_json": _dump(artifact),
        "delivery_json": _dump(delivery),
    }
    if any(row[name] != value for name, value in expected_columns.items()):
        raise RuntimeSidecarUnavailable("stored runtime artifact columns do not match artifact")
    return artifact, delivery


def _receipt_for_run(conn: sqlite3.Connection, model_run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT receipt_identity, receipt_json FROM runtime_receipts WHERE model_run_id=?",
        (model_run_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = _load_json(row["receipt_json"], "receipt")
    identity = receipt.get("receipt_identity") if isinstance(receipt, dict) else None
    body = {key: value for key, value in receipt.items() if key != "receipt_identity"} if isinstance(receipt, dict) else {}
    if identity != row["receipt_identity"] or identity != "rrc_" + _digest(body):
        raise RuntimeSidecarUnavailable("stored runtime receipt identity mismatch")
    return receipt


def _run_row_by_ids(
    conn: sqlite3.Connection, request_id: str, mutation_id: str,
) -> sqlite3.Row | None:
    rows = list(conn.execute(
        "SELECT * FROM runtime_model_runs WHERE request_id=? OR mutation_id=?",
        (request_id, mutation_id),
    ))
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["request_id"] != request_id or rows[0]["mutation_id"] != mutation_id:
        raise RuntimeIdempotencyConflict("request_id or mutation_id conflicts with another runtime request")
    return rows[0]


def _execution_flags(*, persisted: bool, sent: bool, external: bool) -> dict[str, bool]:
    return {"persisted": persisted, "sent_to_model": sent, "external_model_calls": external}


def _prepared_snapshot(conn: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
    artifact, delivery = _load_artifact(conn, run["artifact_identity"])
    _validate_stored_run(conn, run, artifact, delivery)
    rows = _event_rows(conn, run["model_run_id"])
    projection = _run_event_projection(rows)
    status = projection["status"]
    receipt = _receipt_for_run(conn, run["model_run_id"])
    if status in {"succeeded", "failed"}:
        if receipt is None:
            raise RuntimeSidecarUnavailable("runtime terminal receipt is missing")
        expected = _rebuild_terminal_receipt(conn, run, artifact, status, rows[-1])
        if receipt != expected:
            raise RuntimeSidecarUnavailable("runtime receipt does not match append-only outcome")
        return receipt
    if receipt is not None:
        raise RuntimeSidecarUnavailable("non-terminal runtime run has a receipt")
    if projection["provider_response_received"]:
        execution_phase = "provider_response_received_local_commit_pending"
    elif projection["send_attempted"]:
        execution_phase = "send_attempted_result_unknown"
    else:
        execution_phase = "not_attempted"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "model_run_id": run["model_run_id"],
        "conversation_id": run["conversation_id"],
        "request_id": run["request_id"],
        "mutation_id": run["mutation_id"],
        "status": status,
        "execution_phase": execution_phase,
        "delivery_identity": artifact["delivery_binding"]["delivery_identity"],
        "request_artifact": public_request_artifact(artifact),
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "usage": None,
        "response": None,
        "error": None,
        "execution": _execution_flags(
            persisted=True,
            sent=bool(projection["send_attempted"]),
            external=bool(projection["send_attempted"]),
        ),
        "candidate_hook": {"status": "reserved", "invoked": False},
    }


def _make_receipt(body: dict[str, Any]) -> dict[str, Any]:
    receipt = deepcopy(body)
    receipt["receipt_identity"] = "rrc_" + _digest(receipt)
    return receipt


def _validate_stored_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    artifact: dict[str, Any],
    delivery: dict[str, Any],
) -> None:
    try:
        request_id = _normalize_id(run["request_id"], "stored request_id")
        mutation_id = _normalize_id(run["mutation_id"], "stored mutation_id")
        question = _normalize_question(delivery["controls"]["query"])
        budget = _normalize_budget(delivery["controls"]["budget_profile"])
    except RuntimeThinSliceError as exc:
        raise RuntimeSidecarUnavailable("stored runtime request fields are invalid") from exc
    fingerprint = _request_fingerprint(
        question, budget, artifact["artifact_identity"], artifact["wire_body_sha256"],
    )
    expected_run_id = "run_" + _digest({
        "request_id": request_id,
        "mutation_id": mutation_id,
        "request_fingerprint": fingerprint,
        "artifact_identity": artifact["artifact_identity"],
    })
    expected_conversation_id = "rconv_" + _digest({"model_run_id": expected_run_id})
    expected_user_message_id = "rmsg_" + _digest({
        "model_run_id": expected_run_id, "role": "user",
    })
    if (
        run["request_fingerprint"] != fingerprint
        or run["model_run_id"] != expected_run_id
        or run["conversation_id"] != expected_conversation_id
    ):
        raise RuntimeSidecarUnavailable("stored runtime model run identity is invalid")
    conversation = conn.execute(
        "SELECT conversation_id FROM runtime_conversations WHERE conversation_id=?",
        (expected_conversation_id,),
    ).fetchone()
    user_message = conn.execute(
        """SELECT message_id,conversation_id,content FROM runtime_messages
           WHERE model_run_id=? AND role='user'""",
        (expected_run_id,),
    ).fetchone()
    if (
        conversation is None
        or user_message is None
        or user_message["message_id"] != expected_user_message_id
        or user_message["conversation_id"] != expected_conversation_id
        or user_message["content"] != question
    ):
        raise RuntimeSidecarUnavailable("stored runtime conversation or user message is invalid")


def _rebuild_terminal_receipt(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    artifact: dict[str, Any],
    status: str,
    terminal: sqlite3.Row,
) -> dict[str, Any]:
    response: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    error: dict[str, Any] | None = None
    if status == "succeeded":
        response_events = [
            row for row in _event_rows(conn, run["model_run_id"])
            if row["event_type"] == "provider_response_received"
        ]
        if len(response_events) != 1:
            raise RuntimeSidecarUnavailable("runtime provider response boundary is missing")
        response_event = response_events[0]
        message = conn.execute(
            "SELECT message_id,content FROM runtime_messages WHERE message_id=? AND role='assistant'",
            (terminal["assistant_message_id"],),
        ).fetchone()
        if message is None:
            raise RuntimeSidecarUnavailable("runtime assistant outcome is missing")
        expected_message_id = "rmsg_" + _digest({
            "model_run_id": run["model_run_id"], "role": "assistant",
        })
        if message["message_id"] != expected_message_id:
            raise RuntimeSidecarUnavailable("runtime assistant message identity is invalid")
        if (
            terminal["usage_json"] != response_event["usage_json"]
            or terminal["provider_response_id"] != response_event["provider_response_id"]
            or terminal["finish_reason"] != response_event["finish_reason"]
            or terminal["response_content_sha256"] != response_event["response_content_sha256"]
            or terminal["response_content_sha256"] != _text_sha256(message["content"])
        ):
            raise RuntimeSidecarUnavailable("runtime success does not match provider response boundary")
        usage = _validated_usage(terminal["usage_json"], "usage")
        response = {
            "message_id": message["message_id"],
            "content": message["content"],
            "provider_response_id": terminal["provider_response_id"],
            "finish_reason": terminal["finish_reason"],
        }
        sent, external = True, True
    else:
        row = conn.execute(
            "SELECT * FROM runtime_errors WHERE error_id=? AND model_run_id=?",
            (terminal["error_id"], run["model_run_id"]),
        ).fetchone()
        if row is None:
            raise RuntimeSidecarUnavailable("runtime error outcome is missing")
        error = {
            "error_id": row["error_id"],
            "category": row["category"],
            "code": row["code"],
            "retryable": bool(row["retryable"]),
            "http_status": row["http_status"],
        }
        if row["error_id"] != "rerr_" + _digest({
            "model_run_id": run["model_run_id"], "code": row["code"],
        }):
            raise RuntimeSidecarUnavailable("runtime error identity is invalid")
        sent, external = bool(row["sent_to_model"]), bool(row["external_model_calls"])
        projection = _run_event_projection(_event_rows(conn, run["model_run_id"]))
        expected_sent = error["code"] != "interrupted_before_send"
        if sent is not expected_sent or external is not expected_sent:
            raise RuntimeSidecarUnavailable("runtime error execution boundary is invalid")
        if bool(projection["send_attempted"]) is not expected_sent:
            raise RuntimeSidecarUnavailable("runtime error does not match send history")
        if error["code"] == "provider_response_persist_in_doubt" \
                and not projection["provider_response_received"]:
            raise RuntimeSidecarUnavailable("runtime response in-doubt error lacks response event")
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "model_run_id": run["model_run_id"],
        "conversation_id": run["conversation_id"],
        "request_id": run["request_id"],
        "mutation_id": run["mutation_id"],
        "status": status,
        "execution_phase": (
            "succeeded" if status == "succeeded"
            else "not_attempted_recovered" if error and error["code"] == "interrupted_before_send"
            else "provider_response_persist_in_doubt" if error and error["code"] == "provider_response_persist_in_doubt"
            else "send_attempt_in_doubt" if error and error["code"] == "interrupted_run_in_doubt"
            else "provider_failed"
        ),
        "delivery_identity": artifact["delivery_binding"]["delivery_identity"],
        "request_artifact": public_request_artifact(artifact),
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "usage": usage,
        "response": response,
        "error": error,
        "execution": _execution_flags(persisted=True, sent=sent, external=external),
        "candidate_hook": {"status": "reserved", "invoked": False},
    }
    return _make_receipt(body)


class _RuntimeCore:
    """Injectable test primitive that can never target the production sidecar."""

    def __init__(
        self,
        *,
        sidecar_path: str | Path,
        adapter: Any,
        delivery_builder: Callable[[RetrievalQuery], dict[str, Any]],
        authority: RuntimeExecutionSession | None = None,
    ) -> None:
        self.sidecar_path = _validated_target(sidecar_path)
        if is_production_runtime_sidecar(self.sidecar_path):
            raise RuntimeThinSliceError("test runtime primitive cannot target the production sidecar")
        if authority is not None:
            raise RuntimeThinSliceError("runtime execution authority is accepted only by the production factory")
        if not callable(delivery_builder) or not callable(getattr(adapter, "send", None)):
            raise RuntimeThinSliceError("runtime dependencies are invalid")
        if getattr(adapter, "provider_id", None) != PROVIDER_ID or getattr(adapter, "model_id", None) != MODEL_ID:
            raise RuntimeThinSliceError("runtime adapter provider or model is invalid")
        self.adapter = adapter
        self.delivery_builder = delivery_builder

    def preview(self, question: str, *, budget_profile: str = "Medium") -> dict[str, Any]:
        normalized = _normalize_question(question)
        budget = _normalize_budget(budget_profile)
        delivery, profile_binding = _resolve_delivery_result(
            self.delivery_builder(RetrievalQuery(normalized), budget_profile=budget)
        )
        artifact = build_runtime_request_artifact(
            delivery, approved_profile_binding=profile_binding,
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": "preview",
            "execution_phase": "not_attempted",
            "delivery_identity": delivery["delivery_identity"],
            "request_artifact": public_request_artifact(artifact),
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "usage": None,
            "response": None,
            "error": None,
            "execution": _execution_flags(
                persisted=False, sent=False, external=False,
            ) | {"credential_read": False},
            "candidate_hook": {"status": "reserved", "invoked": False},
        }

    def execute(
        self,
        question: str,
        *,
        request_id: str,
        mutation_id: str,
        budget_profile: str = "Medium",
    ) -> dict[str, Any]:
        normalized = _normalize_question(question)
        budget = _normalize_budget(budget_profile)
        request = _normalize_id(request_id, "request_id")
        mutation = _normalize_id(mutation_id, "mutation_id")
        delivery, profile_binding = _resolve_delivery_result(
            self.delivery_builder(RetrievalQuery(normalized), budget_profile=budget)
        )
        artifact = build_runtime_request_artifact(
            delivery, approved_profile_binding=profile_binding,
        )
        prepared_request = prepare_deepseek_request(artifact)
        fingerprint = _request_fingerprint(
            normalized, budget, artifact["artifact_identity"], artifact["wire_body_sha256"],
        )

        with _open_validated_sidecar(self.sidecar_path, write=True) as conn:
            existing = _run_row_by_ids(conn, request, mutation)
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise RuntimeIdempotencyConflict("runtime idempotency payload conflict")
                snapshot = _prepared_snapshot(conn, existing)
                if snapshot["status"] in {"succeeded", "failed"}:
                    return snapshot
                if snapshot["status"] == "running":
                    raise RuntimeRunInDoubt("runtime run is already running and requires explicit recovery")
                run = existing
                stored_artifact, _ = _load_artifact(conn, run["artifact_identity"])
                if stored_artifact != artifact:
                    raise RuntimeIdempotencyConflict("runtime idempotency artifact conflict")
            else:
                try:
                    run = self._prepare(conn, request, mutation, fingerprint, delivery, artifact)
                except sqlite3.IntegrityError:
                    # A same-key concurrent caller may have prepared the deterministic run
                    # while this caller was waiting for the write lock.  Re-read it; any
                    # other uniqueness collision remains a hard failure.
                    run = _run_row_by_ids(conn, request, mutation)
                    if run is None or run["request_fingerprint"] != fingerprint:
                        raise
                    snapshot = _prepared_snapshot(conn, run)
                    if snapshot["status"] in {"succeeded", "failed"}:
                        return snapshot
                    if snapshot["status"] == "running":
                        raise RuntimeRunInDoubt(
                            "runtime run is already running and requires explicit recovery"
                        )
                    stored_artifact, _ = _load_artifact(conn, run["artifact_identity"])
                    if stored_artifact != artifact:
                        raise RuntimeIdempotencyConflict("runtime idempotency artifact conflict")
            self._append_running(conn, run)
            conn.commit()
            self._append_send_attempt_started(conn, run)
            conn.commit()

        try:
            response = self.adapter.send(prepared_request)
            if not isinstance(response, DeepSeekResponse):
                raise DeepSeekProviderError(
                    "provider_response_invalid", category="provider", retryable=False,
                )
        except DeepSeekProviderError as exc:
            return self._persist_failure(
                run, artifact, category=exc.category, code=exc.code,
                retryable=exc.retryable, http_status=exc.http_status,
                sent=True, external=True, execution_phase="provider_failed",
            )
        except Exception:
            return self._persist_failure(
                run, artifact, category="adapter", code="adapter_unexpected_error",
                retryable=False, http_status=None, sent=True, external=True,
                execution_phase="provider_failed",
            )
        self._append_provider_response_received(run, response)
        return self._persist_success(run, artifact, response)

    def recover_running(self, model_run_id: str) -> dict[str, Any]:
        model_run = str(model_run_id)
        if not _RUN_RE.fullmatch(model_run):
            raise RuntimeThinSliceError("model_run_id is invalid")
        with _open_validated_sidecar(self.sidecar_path, write=True) as conn:
            run = conn.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run,),
            ).fetchone()
            if run is None:
                raise RuntimeThinSliceError("model run was not found")
            snapshot = _prepared_snapshot(conn, run)
            if snapshot["status"] in {"succeeded", "failed"}:
                return snapshot
            if snapshot["status"] != "running":
                raise RuntimeThinSliceError("only a running model run can be recovered")
            artifact, _ = _load_artifact(conn, run["artifact_identity"])
            projection = _run_event_projection(_event_rows(conn, model_run))
        if projection["provider_response_received"]:
            code = "provider_response_persist_in_doubt"
            sent = external = True
            execution_phase = "provider_response_persist_in_doubt"
        elif projection["send_attempted"]:
            code = "interrupted_run_in_doubt"
            sent = external = True
            execution_phase = "send_attempt_in_doubt"
        else:
            code = "interrupted_before_send"
            sent = external = False
            execution_phase = "not_attempted_recovered"
        return self._persist_failure(
            run, artifact, category="recovery", code=code,
            retryable=True, http_status=None, sent=sent, external=external,
            execution_phase=execution_phase,
        )

    def get_run(self, model_run_id: str) -> dict[str, Any]:
        model_run = str(model_run_id)
        if not _RUN_RE.fullmatch(model_run):
            raise RuntimeThinSliceError("model_run_id is invalid")
        with _open_validated_sidecar(self.sidecar_path, write=False) as conn:
            run = conn.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run,),
            ).fetchone()
            if run is None:
                raise RuntimeThinSliceError("model run was not found")
            return _prepared_snapshot(conn, run)

    def _prepare(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        mutation_id: str,
        fingerprint: str,
        delivery: dict[str, Any],
        artifact: dict[str, Any],
    ) -> sqlite3.Row:
        now = _utc_now()
        artifact_id = artifact["artifact_identity"]
        model_run_id = "run_" + _digest({
            "request_id": request_id, "mutation_id": mutation_id,
            "request_fingerprint": fingerprint, "artifact_identity": artifact_id,
        })
        conversation_id = "rconv_" + _digest({"model_run_id": model_run_id})
        user_message_id = "rmsg_" + _digest({"model_run_id": model_run_id, "role": "user"})
        event_id = "rev_" + _digest({
            "model_run_id": model_run_id, "event_type": "state_prepared",
        })
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO runtime_conversations(conversation_id,created_at) VALUES(?,?)",
                (conversation_id, now),
            )
            stored_artifact = conn.execute(
                "SELECT artifact_json, delivery_json FROM runtime_request_artifacts WHERE artifact_identity=?",
                (artifact_id,),
            ).fetchone()
            artifact_json = _dump(artifact)
            delivery_json = _dump(delivery)
            if stored_artifact is None:
                conn.execute(
                    """INSERT INTO runtime_request_artifacts(
                        artifact_identity,delivery_identity,delivery_json,model_context_sha256,
                        system_instruction_version,system_instruction_sha256,provider,model,
                        model_config_json,wire_body_sha256,artifact_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        artifact_id, delivery["delivery_identity"], delivery_json,
                        artifact["delivery_binding"]["model_context_sha256"],
                        SYSTEM_INSTRUCTION_VERSION, artifact["system_instruction"]["sha256"],
                        PROVIDER_ID, MODEL_ID, _dump(MODEL_CONFIG), artifact["wire_body_sha256"],
                        artifact_json, now,
                    ),
                )
            elif stored_artifact["artifact_json"] != artifact_json or stored_artifact["delivery_json"] != delivery_json:
                raise RuntimeSidecarUnavailable("runtime artifact identity collision")
            conn.execute(
                """INSERT INTO runtime_model_runs(
                    model_run_id,request_id,mutation_id,request_fingerprint,
                    conversation_id,artifact_identity,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (model_run_id, request_id, mutation_id, fingerprint, conversation_id, artifact_id, now),
            )
            conn.execute(
                """INSERT INTO runtime_messages(
                    message_id,conversation_id,model_run_id,role,content,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (user_message_id, conversation_id, model_run_id, "user", delivery["controls"]["query"], now),
            )
            conn.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (event_id, model_run_id, "state_prepared", None, "prepared", now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        run = conn.execute(
            "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeSidecarUnavailable("prepared runtime run was not persisted")
        return run

    def _append_running(self, conn: sqlite3.Connection, run: sqlite3.Row) -> None:
        status = _validate_run_events(_event_rows(conn, run["model_run_id"]))
        if status != "prepared":
            raise RuntimeThinSliceError("runtime run is not prepared")
        now = _utc_now()
        event_id = "rev_" + _digest({
            "model_run_id": run["model_run_id"], "event_type": "state_running",
        })
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _validate_run_events(_event_rows(conn, run["model_run_id"]))
            if current != "prepared":
                raise RuntimeRunInDoubt("runtime run was concurrently executed")
            conn.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (event_id, run["model_run_id"], "state_running", "prepared", "running", now),
            )
        except Exception:
            conn.rollback()
            raise

    def _append_send_attempt_started(
        self, conn: sqlite3.Connection, run: sqlite3.Row,
    ) -> None:
        projection = _run_event_projection(_event_rows(conn, run["model_run_id"]))
        if projection != {
            "status": "running", "send_attempted": False,
            "provider_response_received": False,
        }:
            raise RuntimeRunInDoubt("runtime send boundary cannot be appended")
        now = _utc_now()
        event_id = "rev_" + _digest({
            "model_run_id": run["model_run_id"], "event_type": "send_attempt_started",
        })
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,occurred_at
                ) VALUES(?,?,?,?)""",
                (event_id, run["model_run_id"], "send_attempt_started", now),
            )
        except Exception:
            conn.rollback()
            raise

    def _append_provider_response_received(
        self, run: sqlite3.Row, response: DeepSeekResponse,
    ) -> None:
        now = _utc_now()
        event_id = "rev_" + _digest({
            "model_run_id": run["model_run_id"], "event_type": "provider_response_received",
        })
        with _open_validated_sidecar(self.sidecar_path, write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                projection = _run_event_projection(_event_rows(conn, run["model_run_id"]))
                if projection != {
                    "status": "running", "send_attempted": True,
                    "provider_response_received": False,
                }:
                    raise RuntimeRunInDoubt("runtime provider response boundary cannot be appended")
                conn.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,usage_json,provider_response_id,
                        finish_reason,response_content_sha256,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        event_id, run["model_run_id"], "provider_response_received",
                        _dump(response.usage), response.provider_response_id,
                        response.finish_reason, _text_sha256(response.content), now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _persist_success(
        self, run: sqlite3.Row, artifact: dict[str, Any], response: DeepSeekResponse,
    ) -> dict[str, Any]:
        now = _utc_now()
        model_run_id = run["model_run_id"]
        message_id = "rmsg_" + _digest({"model_run_id": model_run_id, "role": "assistant"})
        event_id = "rev_" + _digest({
            "model_run_id": model_run_id, "event_type": "state_succeeded",
        })
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "model_run_id": model_run_id,
            "conversation_id": run["conversation_id"],
            "request_id": run["request_id"],
            "mutation_id": run["mutation_id"],
            "status": "succeeded",
            "execution_phase": "succeeded",
            "delivery_identity": artifact["delivery_binding"]["delivery_identity"],
            "request_artifact": public_request_artifact(artifact),
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "usage": deepcopy(response.usage),
            "response": {
                "message_id": message_id,
                "content": response.content,
                "provider_response_id": response.provider_response_id,
                "finish_reason": response.finish_reason,
            },
            "error": None,
            "execution": _execution_flags(persisted=True, sent=True, external=True),
            "candidate_hook": {"status": "reserved", "invoked": False},
        }
        receipt = _make_receipt(body)
        with _open_validated_sidecar(self.sidecar_path, write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = _receipt_for_run(conn, model_run_id)
                if existing is not None:
                    return existing
                rows = _event_rows(conn, model_run_id)
                projection = _run_event_projection(rows)
                if projection != {
                    "status": "running", "send_attempted": True,
                    "provider_response_received": True,
                }:
                    raise RuntimeRunInDoubt("runtime result cannot be persisted from the current state")
                response_event = rows[-1]
                if (
                    response_event["usage_json"] != _dump(response.usage)
                    or response_event["provider_response_id"] != response.provider_response_id
                    or response_event["finish_reason"] != response.finish_reason
                    or response_event["response_content_sha256"] != _text_sha256(response.content)
                ):
                    raise RuntimeSidecarUnavailable("provider response event does not match success result")
                conn.execute(
                    """INSERT INTO runtime_messages(
                        message_id,conversation_id,model_run_id,role,content,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (message_id, run["conversation_id"], model_run_id, "assistant", response.content, now),
                )
                conn.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,from_status,to_status,assistant_message_id,
                        usage_json,provider_response_id,finish_reason,response_content_sha256,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id, model_run_id, "state_succeeded", "running", "succeeded", message_id,
                        _dump(response.usage), response.provider_response_id,
                        response.finish_reason, _text_sha256(response.content), now,
                    ),
                )
                conn.execute(
                    "INSERT INTO runtime_receipts(model_run_id,receipt_identity,receipt_json) VALUES(?,?,?)",
                    (model_run_id, receipt["receipt_identity"], _dump(receipt)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
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
        sent: bool,
        external: bool,
        execution_phase: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        model_run_id = run["model_run_id"]
        error_id = "rerr_" + _digest({"model_run_id": model_run_id, "code": code})
        event_id = "rev_" + _digest({
            "model_run_id": model_run_id, "event_type": "state_failed",
        })
        error = {
            "error_id": error_id,
            "category": str(category),
            "code": str(code),
            "retryable": bool(retryable),
            "http_status": http_status,
        }
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "model_run_id": model_run_id,
            "conversation_id": run["conversation_id"],
            "request_id": run["request_id"],
            "mutation_id": run["mutation_id"],
            "status": "failed",
            "execution_phase": execution_phase,
            "delivery_identity": artifact["delivery_binding"]["delivery_identity"],
            "request_artifact": public_request_artifact(artifact),
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "usage": None,
            "response": None,
            "error": error,
            "execution": _execution_flags(persisted=True, sent=sent, external=external),
            "candidate_hook": {"status": "reserved", "invoked": False},
        }
        receipt = _make_receipt(body)
        with _open_validated_sidecar(self.sidecar_path, write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = _receipt_for_run(conn, model_run_id)
                if existing is not None:
                    return existing
                projection = _run_event_projection(_event_rows(conn, model_run_id))
                if projection["status"] != "running":
                    raise RuntimeRunInDoubt("runtime failure cannot be persisted from the current state")
                expected_boundary = {
                    "provider_failed": (True, False, True, True),
                    "not_attempted_recovered": (False, False, False, False),
                    "send_attempt_in_doubt": (True, False, True, True),
                    "provider_response_persist_in_doubt": (True, True, True, True),
                }.get(execution_phase)
                actual_boundary = (
                    bool(projection["send_attempted"]),
                    bool(projection["provider_response_received"]),
                    bool(sent), bool(external),
                )
                if expected_boundary is None or actual_boundary != expected_boundary:
                    raise RuntimeSidecarUnavailable("runtime failure execution phase is inconsistent")
                conn.execute(
                    """INSERT INTO runtime_errors(
                        error_id,model_run_id,category,code,retryable,http_status,
                        sent_to_model,external_model_calls,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        error_id, model_run_id, category, code, int(bool(retryable)), http_status,
                        int(sent), int(external), now,
                    ),
                )
                conn.execute(
                    """INSERT INTO runtime_run_events(
                        event_id,model_run_id,event_type,from_status,to_status,error_id,occurred_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (event_id, model_run_id, "state_failed", "running", "failed", error_id, now),
                )
                conn.execute(
                    "INSERT INTO runtime_receipts(model_run_id,receipt_identity,receipt_json) VALUES(?,?,?)",
                    (model_run_id, receipt["receipt_identity"], _dump(receipt)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return receipt


def _build_production_runtime_core_factory():
    factory_token = object()

    class _ProductionRuntimeCore(_RuntimeCore):
        __slots__ = ("sidecar_path", "adapter", "delivery_builder")

        def __init__(self, *, token: object, authority: RuntimeExecutionSession) -> None:
            if token is not factory_token or not is_trusted_runtime_session(authority):
                raise RuntimeThinSliceError(
                    "production runtime instances are issued only by the controlled factory"
                )
            self.sidecar_path = _validated_target(PRODUCTION_RUNTIME_SIDECAR)
            self.adapter = DeepSeekHTTPAdapter()
            self.delivery_builder = _production_runtime_delivery

    def create(authority: RuntimeExecutionSession) -> _ProductionRuntimeCore:
        if not is_trusted_runtime_session(authority):
            raise RuntimeThinSliceError("production runtime execution requires explicit authority")
        return _ProductionRuntimeCore(token=factory_token, authority=authority)

    return create


_create_production_runtime_core = _build_production_runtime_core_factory()
del _build_production_runtime_core_factory


class RuntimeThinSliceService:
    """Production facade with fixed v8.0-E builder, sidecar, provider, and model."""

    __slots__ = ()

    @staticmethod
    def _builder(
        query: RetrievalQuery, *, budget_profile: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return _production_runtime_delivery(query, budget_profile=budget_profile)

    def preview(self, question: str, *, budget_profile: str = "Medium") -> dict[str, Any]:
        normalized = _normalize_question(question)
        budget = _normalize_budget(budget_profile)
        delivery, profile_binding = self._builder(
            RetrievalQuery(normalized), budget_profile=budget,
        )
        artifact = build_runtime_request_artifact(
            delivery, approved_profile_binding=profile_binding,
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": "preview",
            "execution_phase": "not_attempted",
            "delivery_identity": delivery["delivery_identity"],
            "request_artifact": public_request_artifact(artifact),
            "provider": PROVIDER_ID,
            "model": MODEL_ID,
            "runtime_sidecar": runtime_sidecar_status(PRODUCTION_RUNTIME_SIDECAR),
            "usage": None,
            "response": None,
            "error": None,
            "execution": _execution_flags(
                persisted=False, sent=False, external=False,
            ) | {"credential_read": False},
            "candidate_hook": {"status": "reserved", "invoked": False},
        }

    def execute(
        self,
        question: str,
        *,
        request_id: str,
        mutation_id: str,
        budget_profile: str = "Medium",
        authority: RuntimeExecutionSession,
    ) -> dict[str, Any]:
        if not is_trusted_runtime_session(authority):
            raise RuntimeThinSliceError("production runtime execution requires explicit authority")
        core = _create_production_runtime_core(authority)
        return core.execute(
            question, request_id=request_id, mutation_id=mutation_id,
            budget_profile=budget_profile,
        )

    def recover_running(
        self, model_run_id: str, *, authority: RuntimeExecutionSession,
    ) -> dict[str, Any]:
        if not is_trusted_runtime_session(authority):
            raise RuntimeThinSliceError("production runtime recovery requires explicit authority")
        core = _create_production_runtime_core(authority)
        return core.recover_running(model_run_id)

    def get_run(self, model_run_id: str) -> dict[str, Any]:
        if not isinstance(model_run_id, str) or not _RUN_RE.fullmatch(model_run_id):
            raise RuntimeThinSliceError("model_run_id is invalid")
        if runtime_sidecar_status(PRODUCTION_RUNTIME_SIDECAR)["status"] != "ready":
            raise RuntimeSidecarUnavailable("production runtime sidecar is not initialized")
        with closing(_connect_readonly(_validated_target(PRODUCTION_RUNTIME_SIDECAR))) as conn:
            run = conn.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (model_run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeThinSliceError("model run was not found")
            return _prepared_snapshot(conn, run)


def runtime_privacy_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "api_key_sources": ["DEEPSEEK_API_KEY environment", "ignored config_private/deepseek.env"],
        "api_key_persisted": False,
        "api_key_logged": False,
        "api_key_frontend_visible": False,
        "main_chat_database_writes": False,
        "memory_sidecar_writes": False,
        "knowledge_sidecar_writes": False,
        "profile_sidecar_writes": False,
        "review_sidecar_writes": False,
        "runtime_sidecar_default_initialized": False,
        "response_memory_promotion": False,
        "candidate_hook": {"status": "reserved", "invoked": False},
    }


__all__ = [
    "MODEL_CONFIG", "PRODUCTION_RUNTIME_SIDECAR", "PROTOCOL_VERSION",
    "REQUEST_ARTIFACT_VERSION", "RUN_STATUSES", "SIDECAR_SCHEMA_VERSION",
    "SYSTEM_INSTRUCTION_VERSION", "RuntimeExecutionSession", "RuntimeIdempotencyConflict",
    "RuntimeRunInDoubt", "RuntimeSidecarUnavailable", "RuntimeThinSliceError",
    "RuntimeThinSliceService", "build_runtime_request_artifact", "initialize_runtime_sidecar",
    "is_production_runtime_sidecar", "is_trusted_runtime_session",
    "open_production_runtime_execution", "public_request_artifact",
    "runtime_privacy_manifest", "runtime_sidecar_status", "validate_runtime_request_artifact",
]
