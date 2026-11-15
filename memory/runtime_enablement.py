"""YunOS v8.0-D1 fixed DeepSeek v4-flash production runtime profile."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any, Callable
import weakref

from memory.db_path_policy import (
    ControlledSummaryPathError,
    KnowledgeSidecarPathError,
    validate_knowledge_test_db_target,
    validate_memory_sidecar_target,
)
from memory.deepseek_v4_adapter import (
    DeepSeekV4FlashHTTPAdapter,
    READINESS_PROTOCOL_VERSION,
    _authorize_production_readiness_runner,
    _run_production_readiness_transport,
    _seal_production_readiness_transport,
)
from memory.deepseek_adapter import load_deepseek_api_key
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.production_delivery import (
    build_production_delivery,
    production_profile_binding,
    validate_trusted_delivery_standalone,
)
from memory.runtime_protocol_contracts import (
    ARTIFACT_VERSION,
    BASE_URL,
    FIXED_GENERATION_CONFIG,
    FIXED_RUNTIME_PROFILE,
    MODEL_ID,
    PROVIDER_ID,
    RENDERER_VERSION,
    RUNTIME_PROFILE_VERSION,
    RUNTIME_PROTOCOL_VERSION,
    RUNTIME_SCHEMA_VERSION,
    SYSTEM_INSTRUCTION,
    SYSTEM_INSTRUCTION_VERSION,
    RuntimeProtocolContractError,
    build_runtime_artifact,
    canonical_json,
    prepare_runtime_request,
    public_runtime_artifact,
    runtime_profile_identity as contract_runtime_profile_identity,
    validate_runtime_artifact_binding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_RUNTIME_SIDECAR = PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
PROTOCOL_VERSION = RUNTIME_PROTOCOL_VERSION
SIDECAR_SCHEMA_VERSION = RUNTIME_SCHEMA_VERSION

_SCHEMA_SQL = """
CREATE TABLE runtime_schema (
    schema_version TEXT PRIMARY KEY,
    runtime_profile_json TEXT NOT NULL,
    runtime_profile_identity TEXT NOT NULL UNIQUE,
    initialized_at TEXT NOT NULL
);

CREATE TABLE runtime_conversations (
    conversation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE runtime_request_artifacts (
    artifact_identity TEXT PRIMARY KEY,
    artifact_version TEXT NOT NULL,
    runtime_profile_version TEXT NOT NULL,
    renderer_version TEXT NOT NULL,
    delivery_identity TEXT NOT NULL,
    delivery_json TEXT NOT NULL,
    approved_profile_projection_identity TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider = 'deepseek'),
    model TEXT NOT NULL CHECK(model = 'deepseek-v4-flash'),
    base_url TEXT NOT NULL CHECK(base_url = 'https://api.deepseek.com'),
    stream INTEGER NOT NULL CHECK(stream = 0),
    thinking_type TEXT NOT NULL CHECK(thinking_type = 'disabled'),
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
    "runtime_schema", "runtime_conversations", "runtime_request_artifacts",
    "runtime_model_runs", "runtime_messages", "runtime_errors",
    "runtime_run_events", "runtime_receipts",
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


class RuntimeEnablementError(ValueError):
    """The D1 runtime profile, artifact, or authority is invalid."""


class RuntimeEnablementSidecarUnavailable(RuntimeEnablementError):
    """The D1 runtime sidecar is missing or invalid."""


class DeepSeekReadinessSession:
    """Unforgeable one-shot authority for the two official Gate-E GET requests."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise RuntimeEnablementError("readiness sessions are issued only by the explicit opener")

    def __copy__(self):
        raise RuntimeEnablementError("readiness authority cannot be copied")

    def __deepcopy__(self, _memo):
        raise RuntimeEnablementError("readiness authority cannot be copied")

    def __reduce__(self):
        raise RuntimeEnablementError("readiness authority cannot be serialized")


def _build_readiness_issuer():
    records: weakref.WeakKeyDictionary[DeepSeekReadinessSession, str] = weakref.WeakKeyDictionary()
    authorized_codes: set[Any] = set()
    lock = threading.Lock()

    def issue() -> DeepSeekReadinessSession:
        if sys._getframe(1).f_code not in authorized_codes:
            raise RuntimeEnablementError("readiness session issuer is not authorized")
        session = object.__new__(DeepSeekReadinessSession)
        with lock:
            records[session] = "issued"
        return session

    def state(value: Any) -> str:
        if not isinstance(value, DeepSeekReadinessSession):
            return "invalid"
        with lock:
            return records.get(value, "invalid")

    def consume(value: Any) -> None:
        if not isinstance(value, DeepSeekReadinessSession):
            raise RuntimeEnablementError("trusted readiness authorization is required")
        with lock:
            current = records.get(value)
            if current != "issued":
                raise RuntimeEnablementError(f"readiness authority is {current or 'invalid'}")
            records[value] = "consumed"

    def expire(value: Any) -> None:
        if not isinstance(value, DeepSeekReadinessSession):
            raise RuntimeEnablementError("trusted readiness authorization is required")
        with lock:
            current = records.get(value)
            if current == "issued":
                records[value] = "expired"
                return
            raise RuntimeEnablementError(f"readiness authority is {current or 'invalid'}")

    def authorize(code: Any) -> None:
        authorized_codes.add(code)

    return issue, state, consume, expire, authorize


(
    _issue_readiness,
    deepseek_readiness_authority_state,
    _consume_readiness_authority,
    expire_production_deepseek_readiness,
    _authorize_readiness_issuer,
) = _build_readiness_issuer()


def open_production_deepseek_readiness(
    *, execute: bool, allow_official_readiness_gets: bool,
    confirm_no_chat_completion: bool,
) -> DeepSeekReadinessSession:
    values = (execute, allow_official_readiness_gets, confirm_no_chat_completion)
    if any(value is not True for value in values):
        raise RuntimeEnablementError("all readiness confirmations must be boolean true")
    return _issue_readiness()


_authorize_readiness_issuer(open_production_deepseek_readiness.__code__)
del _authorize_readiness_issuer


def _dump(value: Any) -> str:
    return canonical_json(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def runtime_profile_identity() -> str:
    return contract_runtime_profile_identity()


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _validated_target(path: str | Path) -> Path:
    candidate = _absolute(path)
    production = _absolute(PRODUCTION_RUNTIME_SIDECAR)
    try:
        if candidate == production:
            target, is_production = validate_memory_sidecar_target(
                candidate, production_memory_path=production,
            )
            if not is_production:
                raise RuntimeEnablementError("production runtime sidecar identity mismatch")
            return target
        return validate_knowledge_test_db_target(candidate)
    except (ControlledSummaryPathError, KnowledgeSidecarPathError) as exc:
        raise RuntimeEnablementError("runtime sidecar path is not allowed") from exc


def is_production_runtime_enablement_sidecar(path: str | Path) -> bool:
    return _absolute(path) == _absolute(PRODUCTION_RUNTIME_SIDECAR)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _trigger_name(table: str, action: str) -> str:
    return f"{table}_no_{action.lower()}"


def _trigger_sql(table: str, action: str) -> str:
    return f"""
CREATE TRIGGER {_trigger_name(table, action)}
BEFORE {action} ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""


def _validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT * FROM runtime_schema WHERE schema_version=?", (SIDECAR_SCHEMA_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeEnablementError("runtime enablement schema version mismatch")
    if (
        row["runtime_profile_json"] != _dump(FIXED_RUNTIME_PROFILE)
        or row["runtime_profile_identity"] != runtime_profile_identity()
    ):
        raise RuntimeEnablementError("runtime profile binding mismatch")
    for table, columns in _EXPECTED_COLUMNS.items():
        actual = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        if actual != columns:
            raise RuntimeEnablementError(f"{table} schema mismatch")
        triggers = {
            item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
            )
        }
        if triggers != {_trigger_name(table, "UPDATE"), _trigger_name(table, "DELETE")}:
            raise RuntimeEnablementError(f"{table} append-only triggers are missing")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeEnablementError("runtime sidecar integrity check failed")


def runtime_enablement_sidecar_status(
    path: str | Path = PRODUCTION_RUNTIME_SIDECAR,
) -> dict[str, Any]:
    try:
        target = _validated_target(path)
    except RuntimeEnablementError:
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_path_invalid"}
    if not target.is_file():
        return {"status": "missing", "initialized": False, "reason_code": "sidecar_not_initialized"}
    try:
        with _connect_readonly(target) as conn:
            _validate_schema(conn)
    except (sqlite3.Error, RuntimeEnablementError):
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_invalid"}
    return {
        "status": "ready", "initialized": True, "reason_code": "ready",
        "runtime_profile_identity": runtime_profile_identity(),
    }


def initialize_runtime_enablement_sidecar(
    path: str | Path = PRODUCTION_RUNTIME_SIDECAR,
    *,
    execute: bool = False,
    test_mode: bool = False,
    allow_production_runtime_sidecar: bool = False,
    confirm_production_runtime_sidecar_init: bool = False,
) -> dict[str, Any]:
    confirmations = (
        execute, test_mode, allow_production_runtime_sidecar,
        confirm_production_runtime_sidecar_init,
    )
    if any(value is not True and value is not False for value in confirmations):
        return {"status": "refused", "executed": False, "reason_code": "confirmation_type_invalid"}
    try:
        target = _validated_target(path)
    except RuntimeEnablementError:
        return {"status": "refused", "executed": False, "reason_code": "sidecar_path_invalid"}
    production = is_production_runtime_enablement_sidecar(target)
    target_kind = "production_runtime_sidecar" if production else "test_runtime_sidecar"
    status = runtime_enablement_sidecar_status(target)
    if status["status"] == "ready":
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
        with sqlite3.connect(target) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA_SQL)
            for table in _APPEND_ONLY_TABLES:
                conn.executescript(_trigger_sql(table, "UPDATE"))
                conn.executescript(_trigger_sql(table, "DELETE"))
            conn.execute(
                "INSERT INTO runtime_schema VALUES(?,?,?,?)",
                (
                    SIDECAR_SCHEMA_VERSION, _dump(FIXED_RUNTIME_PROFILE),
                    runtime_profile_identity(), _utc_now(),
                ),
            )
            conn.commit()
        if runtime_enablement_sidecar_status(target)["status"] != "ready":
            raise RuntimeEnablementError("initialized runtime enablement sidecar failed validation")
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


def _production_delivery(
    query: RetrievalQuery, *, budget_profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery = build_production_delivery(query, budget_profile=budget_profile)
    return delivery, production_profile_binding(delivery)


def build_runtime_enablement_artifact(
    delivery: dict[str, Any], *, approved_profile_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_trusted_delivery_standalone(delivery)
    try:
        return build_runtime_artifact(
            delivery, approved_profile_binding=approved_profile_binding,
        )
    except RuntimeProtocolContractError as exc:
        raise RuntimeEnablementError(exc.reason_code) from None


def validate_runtime_enablement_artifact(
    artifact: dict[str, Any], *, trusted_delivery: dict[str, Any] | None = None,
) -> None:
    if trusted_delivery is not None:
        validate_trusted_delivery_standalone(trusted_delivery)
    try:
        prepare_runtime_request(artifact)
        if trusted_delivery is not None:
            validate_runtime_artifact_binding(artifact, trusted_delivery)
    except RuntimeProtocolContractError as exc:
        raise RuntimeEnablementError(exc.reason_code) from None


def public_runtime_enablement_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    validate_runtime_enablement_artifact(artifact)
    return public_runtime_artifact(artifact)


def _normalize_question(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeEnablementError("question must be text")
    text = " ".join(value.split())
    if not text or len(text) > 500 or any(ord(char) < 32 for char in text):
        raise RuntimeEnablementError("question must contain 1..500 printable characters")
    return text


def _normalize_budget(value: Any) -> str:
    if value not in {"Small", "Medium", "Large"}:
        raise RuntimeEnablementError("invalid budget profile")
    return value


class _V4FlashRuntimeCore:
    """Injectable preview-only test primitive; never production-targetable."""

    def __init__(
        self, *, sidecar_path: str | Path,
        delivery_builder: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        self.sidecar_path = _validated_target(sidecar_path)
        if is_production_runtime_enablement_sidecar(self.sidecar_path):
            raise RuntimeEnablementError("test runtime primitive cannot target production")
        if not callable(delivery_builder):
            raise RuntimeEnablementError("delivery builder is invalid")
        self.delivery_builder = delivery_builder

    def preview(self, question: Any, *, budget_profile: str = "Medium") -> dict[str, Any]:
        normalized = _normalize_question(question)
        budget = _normalize_budget(budget_profile)
        delivery, binding = self.delivery_builder(
            RetrievalQuery(normalized), budget_profile=budget,
        )
        artifact = build_runtime_enablement_artifact(
            delivery, approved_profile_binding=binding,
        )
        return _preview_result(delivery, artifact, runtime_enablement_sidecar_status(self.sidecar_path))


def _preview_result(
    delivery: dict[str, Any], artifact: dict[str, Any], sidecar: dict[str, Any],
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "preview",
        "execution_phase": "not_attempted",
        "delivery_identity": delivery["delivery_identity"],
        "request_artifact": public_runtime_enablement_artifact(artifact),
        "runtime_profile": deepcopy(FIXED_RUNTIME_PROFILE),
        "runtime_sidecar": sidecar,
        "usage": None,
        "response": None,
        "error": None,
        "execution": {
            "persisted": False, "sent_to_model": False, "external_model_calls": False,
            "credential_read": False,
        },
        "candidate_hook": {"status": "reserved", "invoked": False},
    }


def _build_production_factory():
    token = object()

    class _ProductionV4FlashRuntime:
        __slots__ = ("_token", "_adapter", "_delivery_builder", "_sidecar_path")

        def __init__(self, supplied: object) -> None:
            if supplied is not token:
                raise RuntimeEnablementError("production runtime is issued only by the fixed factory")
            self._token = supplied
            self._adapter = DeepSeekV4FlashHTTPAdapter()
            self._delivery_builder = _production_delivery
            self._sidecar_path = _validated_target(PRODUCTION_RUNTIME_SIDECAR)

        def preview(self, question: Any, *, budget_profile: str = "Medium") -> dict[str, Any]:
            normalized = _normalize_question(question)
            budget = _normalize_budget(budget_profile)
            delivery, binding = self._delivery_builder(
                RetrievalQuery(normalized), budget_profile=budget,
            )
            artifact = build_runtime_enablement_artifact(
                delivery, approved_profile_binding=binding,
            )
            return _preview_result(
                delivery, artifact, runtime_enablement_sidecar_status(self._sidecar_path),
            )

        def dependency_manifest(self) -> dict[str, Any]:
            return {
                "runtime_sidecar": "fixed_production_runtime_sidecar",
                "delivery_builder": "shared_source_backed_v8.0-e_production_delivery",
                "adapter": type(self._adapter).__name__,
                "runtime_profile": deepcopy(FIXED_RUNTIME_PROFILE),
                "client_overrides_allowed": False,
            }

    def create() -> _ProductionV4FlashRuntime:
        return _ProductionV4FlashRuntime(token)

    return create


create_production_v4_flash_runtime = _build_production_factory()
del _build_production_factory


def deepseek_readiness_dry_run() -> dict[str, Any]:
    return {
        "protocol_version": READINESS_PROTOCOL_VERSION,
        "status": "would_check",
        "executed": False,
        "requests": [
            {"method": "GET", "endpoint": "/models"},
            {"method": "GET", "endpoint": "/user/balance"},
        ],
        "chat_completion_sent": False,
        "credential_read": False,
        "external_model_calls": False,
    }


def run_production_deepseek_readiness(
    *, authority: DeepSeekReadinessSession,
) -> dict[str, Any]:
    # Consumption is atomic and deliberately precedes both credential access
    # and construction of the sealed HTTP transport.
    _consume_readiness_authority(authority)
    credential = load_deepseek_api_key()
    return _run_production_readiness_transport(credential)


_authorize_production_readiness_runner(run_production_deepseek_readiness.__code__)
_seal_production_readiness_transport()
del _authorize_production_readiness_runner
del _seal_production_readiness_transport


def runtime_enablement_privacy_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "runtime_profile": deepcopy(FIXED_RUNTIME_PROFILE),
        "credential_sources": ["environment", "ignored_private_config"],
        "credential_persisted": False,
        "credential_logged": False,
        "credential_frontend_visible": False,
        "readiness_allowed_requests": ["GET /models", "GET /user/balance"],
        "readiness_authority": "one_shot_atomic_consumption",
        "readiness_chat_completion_allowed": False,
        "main_chat_database_writes": False,
        "semantic_sidecar_writes": False,
        "candidate_hook": {"status": "reserved", "invoked": False},
    }
