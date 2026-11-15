"""YunOS v8.0-D1 approved-profile enablement.

This is a new protocol layered beside the sealed v8.0-A control plane.  It
keeps v7.2 candidates source-backed and adds direct user proposals as a
separate, never-auto-approved origin.  Production source reads are fixed and
readonly; all mutable state is confined to the versioned profile sidecar.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any, Callable
import weakref

from memory.approved_profile_control import (
    PRODUCTION_PROFILE_SIDECAR,
    ProfileAdminSession,
    is_trusted_profile_admin_session,
)
from memory.context_assembly import make_approved_profile_item, redact_context_text
from memory.db_path_policy import (
    ControlledSummaryPathError,
    KnowledgeSidecarPathError,
    connect_alias_safe_test_database,
    create_alias_safe_test_database_connection,
    validate_knowledge_test_db_target,
    validate_memory_sidecar_target,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.runtime_protocol_contracts import profile_projection_identity
from memory.temporal_memory_candidates import (
    PROTOCOL_VERSION as TEMPORAL_PROTOCOL_VERSION,
    build_production_temporal_candidates,
    validate_temporal_candidates_standalone,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SOURCE_DB = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
PROTOCOL_VERSION = "v8.0-d1-profile-enablement-control-2"
SIDECAR_SCHEMA_VERSION = "v8.0-d1-approved-profile-sidecar-2"
PROPOSAL_VERSION = "v8.0-d1-direct-user-profile-proposal-2"
ACTIVE_PROJECTION_VERSION = "v8.0-d1-active-approved-profile-1"
GATE_B1_AUTHORIZATION_VERSION = "v8.0-d1-gate-b1-proposal-authorization-1"
GATE_B2_AUTHORIZATION_VERSION = "v8.0-d1-gate-b2-decision-authorization-1"
DECISION_ACTIONS = ("approve", "edit_and_approve", "reject", "defer")
SUBJECT_ORIGINS = ("v7.2_candidate", "direct_user_proposal")
PROPOSAL_TYPES = ("fact", "preference", "goal", "decision", "constraint")
PROPOSED_BY = "local_user"
MAX_STATEMENT_CHARS = 500
MAX_REASON_CHARS = 300
MAX_SOURCE_ANCHORS = 8

_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_PACKAGE_RE = re.compile(r"tmc_[0-9a-f]{64}")
_SELECTION_RE = re.compile(r"msp_[0-9a-f]{64}")
_CANDIDATE_RE = re.compile(r"mc_[0-9a-f]{64}")
_PROPOSAL_RE = re.compile(r"dpp_[0-9a-f]{64}")
_PROMOTED_KIND = {
    "fact": "fact",
    "preference": "preference",
    "goal": "goal",
    "decision": "decision",
    "constraint": "constraint",
    "acceptable_alternative": "preference",
    "open_question": None,
}

_SCHEMA_SQL = """
CREATE TABLE profile_enablement_schema (
    schema_version TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);

CREATE TABLE direct_profile_proposals (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    mutation_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    proposal_version TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin = 'direct_user_proposal'),
    original_text TEXT NOT NULL,
    proposal_type TEXT NOT NULL CHECK(proposal_type IN ('fact','preference','goal','decision','constraint')),
    proposed_by TEXT NOT NULL CHECK(proposed_by = 'local_user'),
    source_anchors_json TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    authorization_version TEXT NOT NULL,
    authorization_identity TEXT NOT NULL UNIQUE,
    authorization_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE direct_profile_proposal_receipts (
    proposal_id TEXT PRIMARY KEY REFERENCES direct_profile_proposals(proposal_id),
    receipt_identity TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
);

CREATE TABLE profile_enablement_decisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    mutation_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    subject_origin TEXT NOT NULL CHECK(subject_origin IN ('v7.2_candidate','direct_user_proposal')),
    subject_id TEXT NOT NULL,
    source_package_identity TEXT,
    source_selection_identity TEXT,
    proposal_identity TEXT REFERENCES direct_profile_proposals(proposal_id),
    decision_source TEXT NOT NULL CHECK(decision_source = 'manual'),
    authorization_version TEXT NOT NULL,
    authorization_identity TEXT NOT NULL UNIQUE,
    authorization_snapshot_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('approve','edit_and_approve','reject','defer')),
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    resulting_revision INTEGER NOT NULL UNIQUE CHECK(resulting_revision > 0),
    subject_snapshot_json TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    profile_item_json TEXT,
    original_text TEXT NOT NULL,
    edited_statement TEXT,
    final_text TEXT,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(subject_origin, subject_id, resulting_revision)
);

CREATE INDEX idx_profile_enablement_decision_subject
ON profile_enablement_decisions(subject_origin, subject_id, sequence);

CREATE TABLE profile_enablement_receipts (
    decision_id TEXT PRIMARY KEY REFERENCES profile_enablement_decisions(decision_id),
    receipt_identity TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
);
"""

_APPEND_ONLY_TABLES = (
    "profile_enablement_schema",
    "direct_profile_proposals",
    "direct_profile_proposal_receipts",
    "profile_enablement_decisions",
    "profile_enablement_receipts",
)

_EXPECTED_COLUMNS = {
    "profile_enablement_schema": {"schema_version", "initialized_at"},
    "direct_profile_proposals": {
        "sequence", "proposal_id", "request_id", "mutation_id", "request_fingerprint",
        "proposal_version", "origin", "original_text", "proposal_type", "proposed_by",
        "source_anchors_json", "expected_revision", "authorization_version",
        "authorization_identity", "authorization_snapshot_json", "created_at",
    },
    "direct_profile_proposal_receipts": {"proposal_id", "receipt_identity", "receipt_json"},
    "profile_enablement_decisions": {
        "sequence", "decision_id", "request_id", "mutation_id", "request_fingerprint",
        "subject_origin", "subject_id", "source_package_identity", "source_selection_identity",
        "proposal_identity", "decision_source", "authorization_version",
        "authorization_identity", "authorization_snapshot_json", "action", "expected_revision",
        "resulting_revision", "subject_snapshot_json", "evidence_snapshot_json",
        "profile_item_json", "original_text", "edited_statement", "final_text", "reason",
        "decided_at",
    },
    "profile_enablement_receipts": {"decision_id", "receipt_identity", "receipt_json"},
}


class ProfileEnablementError(ValueError):
    """The D1 profile proposal or decision is invalid."""


class ProfileEnablementSidecarUnavailable(ProfileEnablementError):
    """The D1 sidecar is missing or invalid."""


class ProfileEnablementRevisionConflict(ProfileEnablementError):
    def __init__(self, current_revision: int):
        super().__init__("expected_revision does not match the current control revision")
        self.current_revision = current_revision


class ProfileEnablementIdempotencyConflict(ProfileEnablementError):
    """A request or mutation identifier was reused with another payload."""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class GateB1ProposalCapability:
    """Unforgeable one-shot capability for exactly one proposal creation."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise ProfileEnablementError("Gate B1 capability is issued only by an authorized opener")

    def __copy__(self):
        raise ProfileEnablementError("Gate B1 capability cannot be copied")

    def __deepcopy__(self, _memo):
        raise ProfileEnablementError("Gate B1 capability cannot be copied")

    def __reduce__(self):
        raise ProfileEnablementError("Gate B1 capability cannot be serialized")


class GateB2DecisionCapability:
    """Unforgeable one-shot capability for exactly one bound manual decision."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise ProfileEnablementError("Gate B2 capability is issued only by an authorized opener")

    def __copy__(self):
        raise ProfileEnablementError("Gate B2 capability cannot be copied")

    def __deepcopy__(self, _memo):
        raise ProfileEnablementError("Gate B2 capability cannot be copied")

    def __reduce__(self):
        raise ProfileEnablementError("Gate B2 capability cannot be serialized")


def _build_profile_gate_authority_registry():
    records: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
    authorized_codes: set[Any] = set()
    lock = threading.Lock()

    def issue(kind: str, binding: dict[str, Any]) -> Any:
        if sys._getframe(1).f_code not in authorized_codes:
            raise ProfileEnablementError("profile Gate authority issuer is not authorized")
        capability_type = GateB1ProposalCapability if kind == "B1" else GateB2DecisionCapability
        capability = object.__new__(capability_type)
        record = deepcopy(binding)
        record["gate"] = kind
        record["state"] = "issued"
        record["issued_at"] = _utc_now()
        version = (
            GATE_B1_AUTHORIZATION_VERSION if kind == "B1" else GATE_B2_AUTHORIZATION_VERSION
        )
        record["authorization_version"] = version
        authorization_snapshot = {
            "authorization_version": version,
            "gate": kind,
            "binding": {
                key: deepcopy(value) for key, value in binding.items() if key != "target_path"
            },
        }
        record["authorization_snapshot"] = authorization_snapshot
        record["authorization_identity"] = (
            ("gb1_" if kind == "B1" else "gb2_")
            + _digest(authorization_snapshot)
        )
        with lock:
            records[capability] = record
        return capability

    def state(capability: Any) -> str:
        with lock:
            record = records.get(capability) if isinstance(
                capability, (GateB1ProposalCapability, GateB2DecisionCapability)
            ) else None
            return record["state"] if record is not None else "invalid"

    def consume(
        capability: Any,
        *,
        gate: str,
        target: Path,
        asserted_binding: dict[str, Any],
    ) -> dict[str, Any]:
        expected_type = GateB1ProposalCapability if gate == "B1" else GateB2DecisionCapability
        if not isinstance(capability, expected_type):
            raise ProfileEnablementError(f"Gate {gate} capability is required")
        with lock:
            record = records.get(capability)
            if record is None:
                raise ProfileEnablementError(f"Gate {gate} capability is invalid")
            if record["state"] != "issued":
                raise ProfileEnablementError(
                    f"Gate {gate} capability is {record['state']}"
                )
            binding_mismatch = any(
                record.get(key) != value for key, value in asserted_binding.items()
            )
            if (
                record["gate"] != gate
                or record["target_path"] != str(_absolute(target))
                or binding_mismatch
            ):
                record["state"] = "expired"
                raise ProfileEnablementError(f"Gate {gate} capability binding mismatch")
            record["state"] = "consumed"
            return deepcopy({key: value for key, value in record.items() if key != "state"})

    def expire(capability: Any) -> None:
        with lock:
            record = records.get(capability) if isinstance(
                capability, (GateB1ProposalCapability, GateB2DecisionCapability)
            ) else None
            if record is None:
                raise ProfileEnablementError("profile Gate capability is invalid")
            if record["state"] != "issued":
                raise ProfileEnablementError(f"profile Gate capability is {record['state']}")
            record["state"] = "expired"

    def authorize(code: Any) -> None:
        authorized_codes.add(code)

    return issue, state, consume, expire, authorize


(
    _issue_profile_gate_authority,
    profile_gate_authority_state,
    _consume_profile_gate_authority,
    expire_profile_gate_authority,
    _authorize_profile_gate_issuer,
) = _build_profile_gate_authority_registry()
del _build_profile_gate_authority_registry


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _validated_target(path: str | Path) -> Path:
    candidate = _absolute(path)
    production = _absolute(PRODUCTION_PROFILE_SIDECAR)
    try:
        if candidate == production:
            target, is_production = validate_memory_sidecar_target(
                candidate, production_memory_path=production,
            )
            if not is_production:
                raise ProfileEnablementError("production profile sidecar identity mismatch")
            return target
        return validate_knowledge_test_db_target(candidate)
    except (ControlledSummaryPathError, KnowledgeSidecarPathError) as exc:
        raise ProfileEnablementError("approved profile sidecar path is not allowed") from exc


def is_production_profile_enablement_sidecar(path: str | Path) -> bool:
    return _absolute(path) == _absolute(PRODUCTION_PROFILE_SIDECAR)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _connect_writable(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    if is_production_profile_enablement_sidecar(path):
        return sqlite3.connect(path, timeout=timeout)
    return connect_alias_safe_test_database(path, timeout=timeout)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    for table in _APPEND_ONLY_TABLES:
        conn.executescript(_trigger_sql(table, "UPDATE"))
        conn.executescript(_trigger_sql(table, "DELETE"))
    conn.execute(
        "INSERT INTO profile_enablement_schema(schema_version, initialized_at) VALUES(?,?)",
        (SIDECAR_SCHEMA_VERSION, _utc_now()),
    )
    conn.commit()


def _trigger_name(table: str, action: str) -> str:
    return f"{table}_no_{action.lower()}"


def _trigger_sql(table: str, action: str) -> str:
    return f"""
CREATE TRIGGER {_trigger_name(table, action)}
BEFORE {action} ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""


def _validate_schema(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM profile_enablement_schema WHERE schema_version=?",
        (SIDECAR_SCHEMA_VERSION,),
    ).fetchone() is None:
        raise ProfileEnablementError("profile enablement schema version mismatch")
    for table, columns in _EXPECTED_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if actual != columns:
            raise ProfileEnablementError(f"{table} schema mismatch")
        triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
            )
        }
        expected = {_trigger_name(table, "UPDATE"), _trigger_name(table, "DELETE")}
        if triggers != expected:
            raise ProfileEnablementError(f"{table} append-only triggers are missing")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ProfileEnablementError("profile enablement sidecar integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ProfileEnablementError("profile enablement sidecar foreign key check failed")


def profile_enablement_sidecar_status(
    path: str | Path = PRODUCTION_PROFILE_SIDECAR,
) -> dict[str, Any]:
    try:
        target = _validated_target(path)
    except ProfileEnablementError:
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_path_invalid"}
    if not target.is_file():
        return {"status": "missing", "initialized": False, "reason_code": "sidecar_not_initialized"}
    try:
        with _connect_readonly(target) as conn:
            _validate_schema(conn)
    except (sqlite3.Error, ProfileEnablementError):
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_invalid"}
    return {"status": "ready", "initialized": True, "reason_code": "ready"}


def initialize_profile_enablement_sidecar(
    path: str | Path = PRODUCTION_PROFILE_SIDECAR,
    *,
    execute: bool = False,
    test_mode: bool = False,
    allow_production_profile_sidecar: bool = False,
    confirm_production_profile_sidecar_init: bool = False,
) -> dict[str, Any]:
    confirmations = (
        execute, test_mode, allow_production_profile_sidecar,
        confirm_production_profile_sidecar_init,
    )
    if any(value is not True and value is not False for value in confirmations):
        return {"status": "refused", "executed": False, "reason_code": "confirmation_type_invalid"}
    try:
        target = _validated_target(path)
    except ProfileEnablementError:
        return {"status": "refused", "executed": False, "reason_code": "sidecar_path_invalid"}
    production = is_production_profile_enablement_sidecar(target)
    target_kind = "production_profile_sidecar" if production else "test_profile_sidecar"
    existing = profile_enablement_sidecar_status(target)
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
        allow_production_profile_sidecar is True
        and confirm_production_profile_sidecar_init is True
    ):
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "production_confirmation_required",
        }
    if production:
        target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        if production:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created = True
            with sqlite3.connect(target) as conn:
                _initialize_schema(conn)
        else:
            with create_alias_safe_test_database_connection(target) as conn:
                _initialize_schema(conn)
        if profile_enablement_sidecar_status(target)["status"] != "ready":
            raise ProfileEnablementError("initialized D1 profile sidecar failed validation")
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


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProfileEnablementError(f"invalid {label}")
    return value


def _revision(value: Any, label: str = "expected_revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileEnablementError(f"{label} must be a non-negative integer")
    return value


def _safe_text(value: Any, *, label: str, maximum: int, required: bool) -> str:
    if not isinstance(value, str):
        raise ProfileEnablementError(f"{label} must be text")
    text = " ".join(value.split())
    if required and not text:
        raise ProfileEnablementError(f"{label} is required")
    if len(text) > maximum:
        raise ProfileEnablementError(f"{label} exceeds the character limit")
    if redact_context_text(text) != text:
        raise ProfileEnablementError(f"{label} violates the v7.0 privacy boundary")
    return text


def _anchor_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise ProfileEnablementError(f"invalid {label}")
    return value


def _canonical_anchors(
    value: Any,
    *,
    validator: Callable[[str, str], bool] | None,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SOURCE_ANCHORS:
        raise ProfileEnablementError("source_anchors must be a bounded list")
    anchors: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"conversation_id", "message_id"}:
            raise ProfileEnablementError("source anchor fields mismatch")
        conversation_id = _anchor_text(item["conversation_id"], "conversation_id")
        message_id = _anchor_text(item["message_id"], "message_id")
        key = (conversation_id, message_id)
        if key in seen:
            raise ProfileEnablementError("duplicate source anchor")
        if validator is not None and validator(conversation_id, message_id) is not True:
            raise ProfileEnablementError("source anchor is not a controlled user message")
        seen.add(key)
        anchors.append({"conversation_id": conversation_id, "message_id": message_id})
    return sorted(anchors, key=lambda item: (item["conversation_id"], item["message_id"]))


def _production_anchor_validator(conversation_id: str, message_id: str) -> bool:
    if not PRODUCTION_SOURCE_DB.is_file():
        return False
    conn = sqlite3.connect(
        f"file:{PRODUCTION_SOURCE_DB.as_posix()}?mode=ro&immutable=1", uri=True,
    )
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT role FROM messages WHERE conversation_id=? AND message_id=?",
            (conversation_id, message_id),
        ).fetchall()
        return len(rows) == 1 and rows[0][0] == "user"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _proposal_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    anchors = json.loads(row["source_anchors_json"])
    return {
        "proposal_version": row["proposal_version"],
        "proposal_id": row["proposal_id"],
        "origin": row["origin"],
        "original_text": row["original_text"],
        "proposal_type": row["proposal_type"],
        "proposed_by": row["proposed_by"],
        "source_anchors": anchors,
        "expected_revision": row["expected_revision"],
        "authorization_version": row["authorization_version"],
        "authorization_identity": row["authorization_identity"],
        "authorization": json.loads(row["authorization_snapshot_json"]),
        "created_at": row["created_at"],
    }


def _proposal_receipt(row: sqlite3.Row) -> dict[str, Any]:
    receipt = {
        "protocol_version": PROTOCOL_VERSION,
        "receipt_identity": "",
        "status": "proposal_recorded",
        "proposal": _proposal_snapshot(row),
        "active_profile_changed": False,
        "decision_required": True,
        "gate": "B1",
        "control_revision": row["expected_revision"],
    }
    receipt["receipt_identity"] = "ppr_" + _digest({
        key: value for key, value in receipt.items() if key != "receipt_identity"
    })
    return receipt


def _validate_proposals(
    proposal_rows: list[sqlite3.Row], receipt_rows: list[sqlite3.Row],
) -> None:
    if len(proposal_rows) != len(receipt_rows):
        raise ProfileEnablementError("direct proposal receipt chain is incomplete")
    receipts = {row["proposal_id"]: row for row in receipt_rows}
    for index, row in enumerate(proposal_rows, start=1):
        if row["sequence"] != index:
            raise ProfileEnablementError("direct proposal sequence is invalid")
        original = _safe_text(
            row["original_text"], label="stored original_text", maximum=MAX_STATEMENT_CHARS,
            required=True,
        )
        try:
            anchors = _canonical_anchors(json.loads(row["source_anchors_json"]), validator=None)
            authorization = json.loads(row["authorization_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileEnablementError("stored proposal binding is invalid") from exc
        fingerprint_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "proposal_version": PROPOSAL_VERSION,
            "request_id": row["request_id"],
            "mutation_id": row["mutation_id"],
            "origin": "direct_user_proposal",
            "original_text": original,
            "proposal_type": row["proposal_type"],
            "proposed_by": PROPOSED_BY,
            "source_anchors": anchors,
            "expected_revision": _revision(row["expected_revision"], "stored expected_revision"),
            "authorization_version": row["authorization_version"],
        }
        fingerprint = _digest(fingerprint_payload)
        expected_authorization = {
            "authorization_version": GATE_B1_AUTHORIZATION_VERSION,
            "gate": "B1",
            "binding": {
                "original_text": original,
                "proposal_type": row["proposal_type"],
                "source_anchors": anchors,
                "proposed_by": PROPOSED_BY,
                "expected_revision": row["expected_revision"],
                "request_id": row["request_id"],
                "mutation_id": row["mutation_id"],
            },
        }
        expected_id = "dpp_" + _digest({
            "request_fingerprint": fingerprint, "created_at": row["created_at"],
        })
        if (
            row["proposal_version"] != PROPOSAL_VERSION
            or row["origin"] != "direct_user_proposal"
            or row["proposal_type"] not in PROPOSAL_TYPES
            or row["proposed_by"] != PROPOSED_BY
            or row["authorization_version"] != GATE_B1_AUTHORIZATION_VERSION
            or authorization != expected_authorization
            or row["authorization_identity"] != "gb1_" + _digest(expected_authorization)
            or row["request_fingerprint"] != fingerprint
            or row["proposal_id"] != expected_id
            or not _PROPOSAL_RE.fullmatch(row["proposal_id"])
        ):
            raise ProfileEnablementError("stored direct proposal identity mismatch")
        _identifier(row["request_id"], "stored proposal request_id")
        _identifier(row["mutation_id"], "stored proposal mutation_id")
        stored = receipts.get(row["proposal_id"])
        expected_receipt = _proposal_receipt(row)
        try:
            payload = json.loads(stored["receipt_json"]) if stored is not None else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileEnablementError("stored proposal receipt is invalid") from exc
        if (
            stored is None or payload != expected_receipt
            or stored["receipt_identity"] != expected_receipt["receipt_identity"]
        ):
            raise ProfileEnablementError("stored proposal receipt identity mismatch")


def _candidate_evidence(package: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    event = next((item for item in package["events"] if item["event_id"] == candidate["event_id"]), None)
    if event is None:
        raise ProfileEnablementError("candidate event binding is missing")
    source = event["source"]
    anchor = source["message_anchor"]
    return {
        "source_candidate_id": event["source_candidate_id"],
        "event_id": event["event_id"],
        "evidence_ids": deepcopy(event["evidence_ids"]),
        "source_anchor": {
            "conversation_id": source["conversation"]["conversation_id"],
            "conversation_title": source["conversation"]["title"],
            "message_id": anchor["message_id"],
            "role": anchor["role"],
            "time": anchor["time"],
            "reference_uri": source["reference_uri"],
        },
        "source_excerpt_sha256": event["source_excerpt_sha256"],
        "clause_sha256": event["clause_sha256"],
        "clause_unicode_codepoints": event["clause_unicode_codepoints"],
        "clause_ordinal": event["clause_ordinal"],
        "observed_at_utc": event["observed_at_utc"],
        "interpreted_local_date": event["interpreted_local_date"],
        "attribution": deepcopy(event["attribution"]),
    }


def _stored_profile_item(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        item = json.loads(raw)
        rebuilt = make_approved_profile_item(
            item["statement"], kind=item["kind"], topics=item["topics"],
            approval_id=item["approval"]["approval_id"],
            approved_at=item["approval"]["approved_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProfileEnablementError("stored profile item is invalid") from exc
    if item != rebuilt:
        raise ProfileEnablementError("stored profile item identity mismatch")
    return item


def _decision_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "subject_origin": row["subject_origin"],
        "subject_id": row["subject_id"],
        "decision_source": row["decision_source"],
        "authorization_version": row["authorization_version"],
        "authorization_identity": row["authorization_identity"],
        "authorization": json.loads(row["authorization_snapshot_json"]),
        "action": row["action"],
        "resulting_revision": row["resulting_revision"],
        "original_text": row["original_text"],
        "edited_statement": row["edited_statement"] or "",
        "final_text": row["final_text"],
        "reason": row["reason"],
        "decided_at": row["decided_at"],
    }


def _decision_history(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **_decision_summary(row),
        "request_id": row["request_id"],
        "mutation_id": row["mutation_id"],
        "expected_revision": row["expected_revision"],
        "source_package_identity": row["source_package_identity"],
        "source_selection_identity": row["source_selection_identity"],
        "proposal_identity": row["proposal_identity"],
        "subject": json.loads(row["subject_snapshot_json"]),
        "evidence": json.loads(row["evidence_snapshot_json"]),
        "profile_item": _stored_profile_item(row["profile_item_json"]),
    }


def _validate_decisions(rows: list[sqlite3.Row]) -> None:
    for index, row in enumerate(rows, start=1):
        if (
            row["sequence"] != index or row["expected_revision"] != index - 1
            or row["resulting_revision"] != index
        ):
            raise ProfileEnablementError("profile decision revision chain is invalid")
        _identifier(row["request_id"], "stored decision request_id")
        _identifier(row["mutation_id"], "stored decision mutation_id")
        try:
            subject = json.loads(row["subject_snapshot_json"])
            evidence = json.loads(row["evidence_snapshot_json"])
            authorization = json.loads(row["authorization_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileEnablementError("stored decision snapshot is invalid") from exc
        original = _safe_text(
            row["original_text"], label="stored original_text", maximum=MAX_STATEMENT_CHARS,
            required=True,
        )
        edited = _safe_text(
            row["edited_statement"] or "", label="stored edited_statement",
            maximum=MAX_STATEMENT_CHARS, required=row["action"] == "edit_and_approve",
        )
        reason = _safe_text(
            row["reason"], label="stored reason", maximum=MAX_REASON_CHARS, required=False,
        )
        if row["action"] != "edit_and_approve" and edited:
            raise ProfileEnablementError("stored non-edit decision contains edited text")
        approved = row["action"] in {"approve", "edit_and_approve"}
        final_text = edited if row["action"] == "edit_and_approve" else original if approved else None
        if row["final_text"] != final_text:
            raise ProfileEnablementError("stored decision final text is invalid")
        if row["subject_origin"] == "v7.2_candidate":
            if (
                not _CANDIDATE_RE.fullmatch(row["subject_id"])
                or not _PACKAGE_RE.fullmatch(row["source_package_identity"] or "")
                or not _SELECTION_RE.fullmatch(row["source_selection_identity"] or "")
                or row["proposal_identity"] is not None
                or subject.get("candidate_id") != row["subject_id"]
                or original != subject.get("statement")
                or evidence.get("event_id") != subject.get("event_id")
            ):
                raise ProfileEnablementError("stored candidate decision binding is invalid")
        elif row["subject_origin"] == "direct_user_proposal":
            if (
                not _PROPOSAL_RE.fullmatch(row["subject_id"])
                or row["proposal_identity"] != row["subject_id"]
                or row["source_package_identity"] is not None
                or row["source_selection_identity"] is not None
                or subject.get("proposal_id") != row["subject_id"]
                or subject.get("origin") != "direct_user_proposal"
                or subject.get("proposed_by") != PROPOSED_BY
                or original != subject.get("original_text")
                or evidence != {"source_anchors": subject.get("source_anchors", [])}
            ):
                raise ProfileEnablementError("stored direct proposal decision binding is invalid")
        else:
            raise ProfileEnablementError("stored decision origin is invalid")
        fingerprint_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": row["request_id"],
            "mutation_id": row["mutation_id"],
            "subject_origin": row["subject_origin"],
            "subject": subject,
            "evidence": evidence,
            "source_package_identity": row["source_package_identity"],
            "source_selection_identity": row["source_selection_identity"],
            "proposal_identity": row["proposal_identity"],
            "decision_source": "manual",
            "authorization_version": row["authorization_version"],
            "action": row["action"],
            "expected_revision": row["expected_revision"],
            "edited_statement": edited,
            "reason": reason,
        }
        fingerprint = _digest(fingerprint_payload)
        expected_authorization_binding = {
            "subject_origin": row["subject_origin"],
            "subject_id": row["subject_id"],
            "action": row["action"],
            "expected_revision": row["expected_revision"],
            "request_id": row["request_id"],
            "mutation_id": row["mutation_id"],
            "edited_statement": edited,
            "final_text_sha256": hashlib.sha256(edited.encode("utf-8")).hexdigest()
            if row["action"] == "edit_and_approve" else None,
            "reason": reason,
            "query_text": "",
            "budget_profile": "",
            "package_identity": None,
        }
        authorization_binding = authorization.get("binding") if isinstance(authorization, dict) else None
        if not isinstance(authorization_binding, dict):
            raise ProfileEnablementError("stored Gate B2 authorization snapshot is invalid")
        if row["subject_origin"] == "v7.2_candidate":
            if (
                authorization_binding.get("package_identity") != row["source_package_identity"]
                or authorization_binding.get("budget_profile") not in {"Small", "Medium", "Large"}
            ):
                raise ProfileEnablementError("stored candidate authorization source mismatch")
            _safe_text(
                authorization_binding.get("query_text"), label="stored candidate query",
                maximum=500, required=True,
            )
            expected_authorization_binding.update({
                "query_text": authorization_binding["query_text"],
                "budget_profile": authorization_binding["budget_profile"],
                "package_identity": row["source_package_identity"],
            })
        if authorization_binding != expected_authorization_binding:
            raise ProfileEnablementError("stored Gate B2 authorization binding mismatch")
        expected_authorization = {
            "authorization_version": GATE_B2_AUTHORIZATION_VERSION,
            "gate": "B2",
            "binding": expected_authorization_binding,
        }
        expected_id = "apd_" + _digest({
            "request_fingerprint": fingerprint,
            "resulting_revision": row["resulting_revision"],
            "decided_at": row["decided_at"],
        })
        if (
            row["decision_source"] != "manual"
            or row["authorization_version"] != GATE_B2_AUTHORIZATION_VERSION
            or authorization != expected_authorization
            or row["authorization_identity"] != "gb2_" + _digest(expected_authorization)
            or row["action"] not in DECISION_ACTIONS
            or row["request_fingerprint"] != fingerprint or row["decision_id"] != expected_id
        ):
            raise ProfileEnablementError("stored decision identity mismatch")
        item = _stored_profile_item(row["profile_item_json"])
        if approved != (item is not None):
            raise ProfileEnablementError("stored decision projection state mismatch")
        if item is not None and item["statement"] != final_text:
            raise ProfileEnablementError("stored profile item final text mismatch")


def _projection_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    _validate_decisions(rows)
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        latest[(row["subject_origin"], row["subject_id"])] = row
    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for (origin, subject_id), row in latest.items():
        if row["action"] not in {"approve", "edit_and_approve"}:
            continue
        item = deepcopy(_stored_profile_item(row["profile_item_json"]))
        item["model_visible"] = True
        paired.append((item, {
            "profile_item_id": item["profile_item_id"],
            "subject_origin": origin,
            "subject_id": subject_id,
            "decision_id": row["decision_id"],
            "revision": row["resulting_revision"],
            "source_package_identity": row["source_package_identity"],
            "proposal_identity": row["proposal_identity"],
        }))
    paired.sort(key=lambda pair: pair[0]["profile_item_id"])
    items = [pair[0] for pair in paired]
    bindings = [pair[1] for pair in paired]
    projection = {
        "protocol_version": ACTIVE_PROJECTION_VERSION,
        "projection_identity": "",
        "status": "ok" if items else "empty",
        "reason_code": "active_approved_profile_available" if items else "no_active_approved_profile",
        "revision": rows[-1]["resulting_revision"] if rows else 0,
        "decision_count": len(rows),
        "items": items,
        "bindings": bindings,
        "model_visible": bool(items),
        "readonly": True,
    }
    projection["projection_identity"] = profile_projection_identity(projection)
    return projection


def _empty_projection(state: dict[str, Any]) -> dict[str, Any]:
    projection = {
        "protocol_version": ACTIVE_PROJECTION_VERSION,
        "projection_identity": "",
        "status": "unavailable" if not state["initialized"] else "empty",
        "reason_code": state["reason_code"] if not state["initialized"] else "no_active_approved_profile",
        "revision": 0,
        "decision_count": 0,
        "items": [],
        "bindings": [],
        "model_visible": False,
        "readonly": True,
    }
    projection["projection_identity"] = profile_projection_identity(projection)
    return projection


def _decision_receipt(row: sqlite3.Row, projection: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "protocol_version": PROTOCOL_VERSION,
        "receipt_identity": "",
        "status": "persisted",
        "gate": "B2",
        "revision": row["resulting_revision"],
        "decision": _decision_summary(row),
        "active_approved_profile": projection,
    }
    receipt["receipt_identity"] = "apr_" + _digest({
        key: value for key, value in receipt.items() if key != "receipt_identity"
    })
    return receipt


def _validate_decision_receipts(
    rows: list[sqlite3.Row], receipt_rows: list[sqlite3.Row],
) -> None:
    _validate_decisions(rows)
    if len(rows) != len(receipt_rows):
        raise ProfileEnablementError("profile decision receipt chain is incomplete")
    receipts = {row["decision_id"]: row for row in receipt_rows}
    for index, decision in enumerate(rows, start=1):
        stored = receipts.get(decision["decision_id"])
        expected = _decision_receipt(decision, _projection_from_rows(rows[:index]))
        try:
            payload = json.loads(stored["receipt_json"]) if stored is not None else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileEnablementError("stored decision receipt is invalid") from exc
        if (
            stored is None or payload != expected
            or stored["receipt_identity"] != expected["receipt_identity"]
        ):
            raise ProfileEnablementError("stored decision receipt identity mismatch")


def _read_validated_rows(target: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    with _connect_readonly(target) as conn:
        _validate_schema(conn)
        proposals = conn.execute("SELECT * FROM direct_profile_proposals ORDER BY sequence").fetchall()
        proposal_receipts = conn.execute(
            "SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid"
        ).fetchall()
        decisions = conn.execute("SELECT * FROM profile_enablement_decisions ORDER BY sequence").fetchall()
        decision_receipts = conn.execute(
            "SELECT * FROM profile_enablement_receipts ORDER BY rowid"
        ).fetchall()
    _validate_proposals(proposals, proposal_receipts)
    _validate_decision_receipts(decisions, decision_receipts)
    proposal_snapshots = {row["proposal_id"]: _proposal_snapshot(row) for row in proposals}
    for row in decisions:
        if row["subject_origin"] != "direct_user_proposal":
            continue
        try:
            stored_subject = json.loads(row["subject_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileEnablementError("stored proposal decision snapshot is invalid") from exc
        if proposal_snapshots.get(row["proposal_identity"]) != stored_subject:
            raise ProfileEnablementError("direct proposal decision no longer binds its proposal record")
    return proposals, proposal_receipts, decisions, decision_receipts


def active_profile_enablement_projection(
    sidecar_path: str | Path = PRODUCTION_PROFILE_SIDECAR,
) -> dict[str, Any]:
    target = _validated_target(sidecar_path)
    state = profile_enablement_sidecar_status(target)
    if state["status"] == "missing":
        return _empty_projection(state)
    if state["status"] != "ready":
        raise ProfileEnablementSidecarUnavailable("profile enablement sidecar is invalid")
    _, _, decisions, _ = _read_validated_rows(target)
    return _projection_from_rows(decisions)


def _existing_by_ids(
    conn: sqlite3.Connection, table: str, request_id: str, mutation_id: str, fingerprint: str,
) -> sqlite3.Row | None:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE request_id=? OR mutation_id=?",
        (request_id, mutation_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ProfileEnablementIdempotencyConflict("request and mutation IDs refer to different records")
    row = rows[0]
    if (
        row["request_id"] != request_id or row["mutation_id"] != mutation_id
        or row["request_fingerprint"] != fingerprint
    ):
        raise ProfileEnablementIdempotencyConflict("request_id or mutation_id payload conflict")
    return row


def _append_direct_proposal(
    *,
    target: Path,
    original_text: Any,
    proposal_type: Any,
    source_anchors: Any,
    expected_revision: Any,
    request_id: Any,
    mutation_id: Any,
    authorization_version: str,
    authorization_identity: str,
    authorization_snapshot: dict[str, Any],
    anchor_validator: Callable[[str, str], bool],
) -> dict[str, Any]:
    if profile_enablement_sidecar_status(target)["status"] != "ready":
        raise ProfileEnablementSidecarUnavailable("profile enablement sidecar is not initialized")
    original = _safe_text(
        original_text, label="original_text", maximum=MAX_STATEMENT_CHARS, required=True,
    )
    if proposal_type not in PROPOSAL_TYPES:
        raise ProfileEnablementError("invalid proposal_type")
    request = _identifier(request_id, "request_id")
    mutation = _identifier(mutation_id, "mutation_id")
    expected = _revision(expected_revision)
    if authorization_version != GATE_B1_AUTHORIZATION_VERSION or not re.fullmatch(
        r"gb1_[0-9a-f]{64}", authorization_identity or "",
    ):
        raise ProfileEnablementError("Gate B1 authorization binding is invalid")
    if authorization_identity != "gb1_" + _digest(authorization_snapshot):
        raise ProfileEnablementError("Gate B1 authorization identity is invalid")
    anchors = _canonical_anchors(source_anchors, validator=anchor_validator)
    fingerprint_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "proposal_version": PROPOSAL_VERSION,
        "request_id": request,
        "mutation_id": mutation,
        "origin": "direct_user_proposal",
        "original_text": original,
        "proposal_type": proposal_type,
        "proposed_by": PROPOSED_BY,
        "source_anchors": anchors,
        "expected_revision": expected,
        "authorization_version": authorization_version,
    }
    fingerprint = _digest(fingerprint_payload)
    conn = _connect_writable(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_schema(conn)
        proposals = conn.execute("SELECT * FROM direct_profile_proposals ORDER BY sequence").fetchall()
        proposal_receipts = conn.execute(
            "SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid"
        ).fetchall()
        decisions = conn.execute("SELECT * FROM profile_enablement_decisions ORDER BY sequence").fetchall()
        decision_receipts = conn.execute("SELECT * FROM profile_enablement_receipts ORDER BY rowid").fetchall()
        _validate_proposals(proposals, proposal_receipts)
        _validate_decision_receipts(decisions, decision_receipts)
        cross_kind = conn.execute(
            "SELECT 1 FROM profile_enablement_decisions WHERE request_id=? OR mutation_id=?",
            (request, mutation),
        ).fetchone()
        if cross_kind is not None:
            raise ProfileEnablementIdempotencyConflict(
                "request_id or mutation_id already belongs to a profile decision"
            )
        existing = _existing_by_ids(
            conn, "direct_profile_proposals", request, mutation, fingerprint,
        )
        if existing is not None:
            stored = next(row for row in proposal_receipts if row["proposal_id"] == existing["proposal_id"])
            receipt = json.loads(stored["receipt_json"])
            conn.rollback()
            return receipt
        current_revision = len(decisions)
        if expected != current_revision:
            conn.rollback()
            raise ProfileEnablementRevisionConflict(current_revision)
        created_at = _utc_now()
        proposal_id = "dpp_" + _digest({
            "request_fingerprint": fingerprint, "created_at": created_at,
        })
        conn.execute(
            """INSERT INTO direct_profile_proposals(
                proposal_id,request_id,mutation_id,request_fingerprint,proposal_version,
                origin,original_text,proposal_type,proposed_by,source_anchors_json,
                expected_revision,authorization_version,authorization_identity,
                authorization_snapshot_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id, request, mutation, fingerprint, PROPOSAL_VERSION,
                "direct_user_proposal", original, proposal_type, PROPOSED_BY,
                _dump(anchors), expected, authorization_version, authorization_identity,
                _dump(authorization_snapshot), created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM direct_profile_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        receipt = _proposal_receipt(row)
        conn.execute(
            "INSERT INTO direct_profile_proposal_receipts VALUES(?,?,?)",
            (proposal_id, receipt["receipt_identity"], _dump(receipt)),
        )
        proposals.append(row)
        proposal_receipts = conn.execute(
            "SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid"
        ).fetchall()
        _validate_proposals(proposals, proposal_receipts)
        conn.commit()
        return receipt
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _append_decision(
    *,
    target: Path,
    subject_origin: str,
    subject: dict[str, Any],
    evidence: dict[str, Any],
    source_package_identity: str | None,
    source_selection_identity: str | None,
    proposal_identity: str | None,
    promoted_kind: str | None,
    action: Any,
    expected_revision: Any,
    request_id: Any,
    mutation_id: Any,
    authorization_version: str,
    authorization_identity: str,
    authorization_snapshot: dict[str, Any],
    edited_statement: Any = "",
    reason: Any = "",
) -> dict[str, Any]:
    if profile_enablement_sidecar_status(target)["status"] != "ready":
        raise ProfileEnablementSidecarUnavailable("profile enablement sidecar is not initialized")
    if subject_origin not in SUBJECT_ORIGINS or action not in DECISION_ACTIONS:
        raise ProfileEnablementError("invalid profile decision")
    expected_revision = _revision(expected_revision)
    if authorization_version != GATE_B2_AUTHORIZATION_VERSION or not re.fullmatch(
        r"gb2_[0-9a-f]{64}", authorization_identity or "",
    ):
        raise ProfileEnablementError("Gate B2 authorization binding is invalid")
    if authorization_identity != "gb2_" + _digest(authorization_snapshot):
        raise ProfileEnablementError("Gate B2 authorization identity is invalid")
    request = _identifier(request_id, "request_id")
    mutation = _identifier(mutation_id, "mutation_id")
    edited = _safe_text(
        edited_statement, label="edited_statement", maximum=MAX_STATEMENT_CHARS,
        required=action == "edit_and_approve",
    )
    reason_text = _safe_text(reason, label="reason", maximum=MAX_REASON_CHARS, required=False)
    if action != "edit_and_approve" and edited:
        raise ProfileEnablementError("edited_statement is accepted only for edit_and_approve")
    if action in {"approve", "edit_and_approve"} and promoted_kind is None:
        raise ProfileEnablementError("this subject type cannot become an approved profile item")
    subject_id = subject["candidate_id"] if subject_origin == "v7.2_candidate" else subject["proposal_id"]
    original = subject["statement"] if subject_origin == "v7.2_candidate" else subject["original_text"]
    fingerprint_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request,
        "mutation_id": mutation,
        "subject_origin": subject_origin,
        "subject": subject,
        "evidence": evidence,
        "source_package_identity": source_package_identity,
        "source_selection_identity": source_selection_identity,
        "proposal_identity": proposal_identity,
        "decision_source": "manual",
        "authorization_version": authorization_version,
        "action": action,
        "expected_revision": expected_revision,
        "edited_statement": edited,
        "reason": reason_text,
    }
    fingerprint = _digest(fingerprint_payload)
    conn = _connect_writable(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_schema(conn)
        proposals = conn.execute("SELECT * FROM direct_profile_proposals ORDER BY sequence").fetchall()
        proposal_receipts = conn.execute("SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid").fetchall()
        decisions = conn.execute("SELECT * FROM profile_enablement_decisions ORDER BY sequence").fetchall()
        receipts = conn.execute("SELECT * FROM profile_enablement_receipts ORDER BY rowid").fetchall()
        _validate_proposals(proposals, proposal_receipts)
        _validate_decision_receipts(decisions, receipts)
        cross_kind = conn.execute(
            "SELECT 1 FROM direct_profile_proposals WHERE request_id=? OR mutation_id=?",
            (request, mutation),
        ).fetchone()
        if cross_kind is not None:
            raise ProfileEnablementIdempotencyConflict(
                "request_id or mutation_id already belongs to a direct proposal"
            )
        existing = _existing_by_ids(
            conn, "profile_enablement_decisions", request, mutation, fingerprint,
        )
        if existing is not None:
            stored = next(row for row in receipts if row["decision_id"] == existing["decision_id"])
            receipt = json.loads(stored["receipt_json"])
            conn.rollback()
            return receipt
        current_revision = len(decisions)
        if expected_revision != current_revision:
            conn.rollback()
            raise ProfileEnablementRevisionConflict(current_revision)
        resulting_revision = current_revision + 1
        decided_at = _utc_now()
        decision_id = "apd_" + _digest({
            "request_fingerprint": fingerprint,
            "resulting_revision": resulting_revision,
            "decided_at": decided_at,
        })
        approved = action in {"approve", "edit_and_approve"}
        final_text = edited if action == "edit_and_approve" else original if approved else None
        profile_item = None
        if approved:
            kind_topic = subject.get("candidate_type") or subject.get("proposal_type")
            profile_item = make_approved_profile_item(
                final_text, kind=promoted_kind, topics=[subject_origin, kind_topic],
                approval_id=decision_id, approved_at=decided_at,
            )
            if profile_item["statement"] != final_text:
                raise ProfileEnablementError("approved statement violates the privacy boundary")
        conn.execute(
            """INSERT INTO profile_enablement_decisions(
                decision_id,request_id,mutation_id,request_fingerprint,subject_origin,subject_id,
                source_package_identity,source_selection_identity,proposal_identity,decision_source,
                authorization_version,authorization_identity,authorization_snapshot_json,
                action,expected_revision,
                resulting_revision,subject_snapshot_json,evidence_snapshot_json,
                profile_item_json,original_text,edited_statement,final_text,reason,decided_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, request, mutation, fingerprint, subject_origin, subject_id,
                source_package_identity, source_selection_identity, proposal_identity, "manual",
                authorization_version, authorization_identity, _dump(authorization_snapshot),
                action, expected_revision,
                resulting_revision, _dump(subject), _dump(evidence),
                _dump(profile_item) if profile_item is not None else None, original,
                edited or None, final_text, reason_text, decided_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM profile_enablement_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        decisions.append(row)
        receipt = _decision_receipt(row, _projection_from_rows(decisions))
        conn.execute(
            "INSERT INTO profile_enablement_receipts VALUES(?,?,?)",
            (decision_id, receipt["receipt_identity"], _dump(receipt)),
        )
        receipts = conn.execute("SELECT * FROM profile_enablement_receipts ORDER BY rowid").fetchall()
        _validate_decision_receipts(decisions, receipts)
        conn.commit()
        return receipt
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _load_proposal(target: Path, proposal_id: Any) -> dict[str, Any]:
    if not isinstance(proposal_id, str) or not _PROPOSAL_RE.fullmatch(proposal_id):
        raise ProfileEnablementError("invalid proposal identity")
    proposals, _, _, _ = _read_validated_rows(target)
    row = next((item for item in proposals if item["proposal_id"] == proposal_id), None)
    if row is None:
        raise ProfileEnablementError("direct proposal was not found")
    return _proposal_snapshot(row)


def build_profile_enablement_review(
    temporal_package: dict[str, Any], *, sidecar_path: str | Path,
) -> dict[str, Any]:
    validate_temporal_candidates_standalone(temporal_package)
    target = _validated_target(sidecar_path)
    state = profile_enablement_sidecar_status(target)
    active = active_profile_enablement_projection(target)
    proposals: list[sqlite3.Row] = []
    decisions: list[sqlite3.Row] = []
    if state["status"] == "ready":
        proposals, _, decisions, _ = _read_validated_rows(target)
    latest = {(row["subject_origin"], row["subject_id"]): _decision_summary(row) for row in decisions}
    candidates = []
    for candidate in temporal_package["candidates"]:
        origin = "v7.2_candidate"
        current = latest.get((origin, candidate["candidate_id"]))
        kind = _PROMOTED_KIND[candidate["candidate_type"]]
        candidates.append({
            "origin": origin,
            "subject_id": candidate["candidate_id"],
            "candidate": deepcopy(candidate),
            "evidence": _candidate_evidence(temporal_package, candidate),
            "current_decision": current,
            "active": bool(current and current["action"] in {"approve", "edit_and_approve"}),
            "promoted_kind": kind,
            "allowed_actions": list(DECISION_ACTIONS) if kind else ["reject", "defer"],
        })
    proposal_items = []
    for row in proposals:
        proposal = _proposal_snapshot(row)
        current = latest.get(("direct_user_proposal", proposal["proposal_id"]))
        proposal_items.append({
            "origin": "direct_user_proposal",
            "subject_id": proposal["proposal_id"],
            "proposal": proposal,
            "evidence": {"source_anchors": proposal["source_anchors"]},
            "current_decision": current,
            "active": bool(current and current["action"] in {"approve", "edit_and_approve"}),
            "promoted_kind": proposal["proposal_type"],
            "allowed_actions": list(DECISION_ACTIONS),
        })
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok" if candidates or proposal_items else "empty",
        "reason_code": "review_subjects_available" if candidates or proposal_items else "no_review_subjects",
        "source": {
            "protocol_version": TEMPORAL_PROTOCOL_VERSION,
            "package_identity": temporal_package["package_identity"],
            "selection_identity": temporal_package["source_projection"]["package_identity"],
            "readonly": True,
            "source_backed_required_for_candidates": True,
        },
        "control_revision": active["revision"],
        "sidecar": state,
        "mutation_available": state["status"] == "ready",
        "candidates": candidates,
        "direct_proposals": proposal_items,
        "decision_history": [_decision_history(row) for row in decisions],
        "active_approved_profile": active,
        "execution": {
            "source_readonly": True,
            "source_persisted": False,
            "external_model_calls": False,
            "sent_to_model": False,
        },
    }


def _gate_b1_payload(
    *,
    original_text: Any,
    proposal_type: Any,
    source_anchors: Any,
    expected_revision: Any,
    request_id: Any,
    mutation_id: Any,
    anchor_validator: Callable[[str, str], bool] | None,
) -> dict[str, Any]:
    original = _safe_text(
        original_text, label="original_text", maximum=MAX_STATEMENT_CHARS, required=True,
    )
    if proposal_type not in PROPOSAL_TYPES:
        raise ProfileEnablementError("invalid proposal_type")
    return {
        "original_text": original,
        "proposal_type": proposal_type,
        "source_anchors": _canonical_anchors(source_anchors, validator=anchor_validator),
        "proposed_by": PROPOSED_BY,
        "expected_revision": _revision(expected_revision),
        "request_id": _identifier(request_id, "request_id"),
        "mutation_id": _identifier(mutation_id, "mutation_id"),
    }


def _gate_b2_payload(
    *,
    subject_origin: Any,
    subject_id: Any,
    action: Any,
    expected_revision: Any,
    request_id: Any,
    mutation_id: Any,
    edited_statement: Any = "",
    reason: Any = "",
    query_text: Any = "",
    budget_profile: Any = "Medium",
    package_identity: Any = None,
) -> dict[str, Any]:
    if subject_origin not in SUBJECT_ORIGINS or action not in DECISION_ACTIONS:
        raise ProfileEnablementError("invalid Gate B2 decision")
    if subject_origin == "v7.2_candidate":
        if not isinstance(subject_id, str) or not _CANDIDATE_RE.fullmatch(subject_id):
            raise ProfileEnablementError("invalid candidate identity")
        if not isinstance(package_identity, str) or not _PACKAGE_RE.fullmatch(package_identity):
            raise ProfileEnablementError("invalid source package identity")
        query = _safe_text(
            query_text, label="candidate query", maximum=500, required=True,
        )
        if budget_profile not in {"Small", "Medium", "Large"}:
            raise ProfileEnablementError("invalid budget_profile")
        budget = budget_profile
    else:
        if not isinstance(subject_id, str) or not _PROPOSAL_RE.fullmatch(subject_id):
            raise ProfileEnablementError("invalid proposal identity")
        if query_text not in ("", None) or package_identity is not None:
            raise ProfileEnablementError("direct proposal decisions cannot bind candidate source fields")
        query = ""
        budget = ""
    edited = _safe_text(
        edited_statement, label="edited_statement", maximum=MAX_STATEMENT_CHARS,
        required=action == "edit_and_approve",
    )
    if action != "edit_and_approve" and edited:
        raise ProfileEnablementError("edited_statement is accepted only for edit_and_approve")
    reason_text = _safe_text(
        reason, label="reason", maximum=MAX_REASON_CHARS, required=False,
    )
    return {
        "subject_origin": subject_origin,
        "subject_id": subject_id,
        "action": action,
        "expected_revision": _revision(expected_revision),
        "request_id": _identifier(request_id, "request_id"),
        "mutation_id": _identifier(mutation_id, "mutation_id"),
        "edited_statement": edited,
        "final_text_sha256": hashlib.sha256(edited.encode("utf-8")).hexdigest()
        if action == "edit_and_approve" else None,
        "reason": reason_text,
        "query_text": query,
        "budget_profile": budget,
        "package_identity": package_identity,
    }


def _execute_gate_b1(
    *,
    target: Path,
    authority: GateB1ProposalCapability,
    payload: dict[str, Any],
    anchor_validator: Callable[[str, str], bool],
) -> dict[str, Any]:
    binding = _consume_profile_gate_authority(
        authority, gate="B1", target=target, asserted_binding=payload,
    )
    return _append_direct_proposal(
        target=target,
        original_text=payload["original_text"],
        proposal_type=payload["proposal_type"],
        source_anchors=payload["source_anchors"],
        expected_revision=payload["expected_revision"],
        request_id=payload["request_id"],
        mutation_id=payload["mutation_id"],
        authorization_version=binding["authorization_version"],
        authorization_identity=binding["authorization_identity"],
        authorization_snapshot=binding["authorization_snapshot"],
        anchor_validator=anchor_validator,
    )


def _execute_gate_b2(
    *,
    target: Path,
    authority: GateB2DecisionCapability,
    payload: dict[str, Any],
    temporal_builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    binding = _consume_profile_gate_authority(
        authority, gate="B2", target=target, asserted_binding=payload,
    )
    decision = {
        "action": payload["action"],
        "expected_revision": payload["expected_revision"],
        "request_id": payload["request_id"],
        "mutation_id": payload["mutation_id"],
        "authorization_version": binding["authorization_version"],
        "authorization_identity": binding["authorization_identity"],
        "authorization_snapshot": binding["authorization_snapshot"],
        "edited_statement": payload["edited_statement"],
        "reason": payload["reason"],
    }
    if payload["subject_origin"] == "direct_user_proposal":
        return _decide_proposal(
            target=target, proposal_id=payload["subject_id"], **decision,
        )
    package = temporal_builder(
        RetrievalQuery(payload["query_text"]), budget_profile=payload["budget_profile"],
    )
    return _decide_candidate(
        package,
        target=target,
        package_identity=payload["package_identity"],
        candidate_id=payload["subject_id"],
        **decision,
    )


class _ProfileEnablementCore:
    """Test-only B1/B2 core; it can never target the production sidecar."""

    def __init__(
        self,
        temporal_builder: Callable[..., dict[str, Any]],
        sidecar_path: str | Path,
        *,
        anchor_validator: Callable[[str, str], bool] = lambda _c, _m: True,
    ) -> None:
        self.temporal_builder = temporal_builder
        self.sidecar_path = _validated_target(sidecar_path)
        self.anchor_validator = anchor_validator
        if is_production_profile_enablement_sidecar(self.sidecar_path):
            raise ProfileEnablementError("test profile core cannot target production")

    def review(self, query: RetrievalQuery, *, budget_profile: str = "Medium") -> dict[str, Any]:
        return build_profile_enablement_review(
            self.temporal_builder(query, budget_profile=budget_profile),
            sidecar_path=self.sidecar_path,
        )

    def authorize_b1(self, **proposal: Any) -> GateB1ProposalCapability:
        payload = _gate_b1_payload(anchor_validator=self.anchor_validator, **proposal)
        return _issue_profile_gate_authority(
            "B1", {"target_path": str(_absolute(self.sidecar_path)), **payload},
        )

    def propose(
        self, *, authority: GateB1ProposalCapability, **proposal: Any,
    ) -> dict[str, Any]:
        payload = _gate_b1_payload(anchor_validator=None, **proposal)
        return _execute_gate_b1(
            target=self.sidecar_path, authority=authority, payload=payload,
            anchor_validator=self.anchor_validator,
        )

    def authorize_b2(self, **decision: Any) -> GateB2DecisionCapability:
        payload = _gate_b2_payload(**decision)
        return _issue_profile_gate_authority(
            "B2", {"target_path": str(_absolute(self.sidecar_path)), **payload},
        )

    def decide(
        self, *, authority: GateB2DecisionCapability, **decision: Any,
    ) -> dict[str, Any]:
        payload = _gate_b2_payload(**decision)
        return _execute_gate_b2(
            target=self.sidecar_path, authority=authority, payload=payload,
            temporal_builder=self.temporal_builder,
        )


def _decide_candidate(
    temporal_package: dict[str, Any], *, target: Path, package_identity: Any,
    candidate_id: Any, **decision: Any,
) -> dict[str, Any]:
    validate_temporal_candidates_standalone(temporal_package)
    if not isinstance(package_identity, str) or not _PACKAGE_RE.fullmatch(package_identity):
        raise ProfileEnablementError("invalid source package identity")
    if package_identity != temporal_package["package_identity"]:
        raise ProfileEnablementError("source package identity is stale")
    if not isinstance(candidate_id, str) or not _CANDIDATE_RE.fullmatch(candidate_id):
        raise ProfileEnablementError("invalid candidate identity")
    candidate = next(
        (item for item in temporal_package["candidates"] if item["candidate_id"] == candidate_id), None,
    )
    if candidate is None:
        raise ProfileEnablementError("candidate is not present in the source-backed package")
    return _append_decision(
        target=target,
        subject_origin="v7.2_candidate",
        subject=deepcopy(candidate),
        evidence=_candidate_evidence(temporal_package, candidate),
        source_package_identity=package_identity,
        source_selection_identity=temporal_package["source_projection"]["package_identity"],
        proposal_identity=None,
        promoted_kind=_PROMOTED_KIND[candidate["candidate_type"]],
        **decision,
    )


def _decide_proposal(*, target: Path, proposal_id: Any, **decision: Any) -> dict[str, Any]:
    proposal = _load_proposal(target, proposal_id)
    return _append_decision(
        target=target,
        subject_origin="direct_user_proposal",
        subject=proposal,
        evidence={"source_anchors": proposal["source_anchors"]},
        source_package_identity=None,
        source_selection_identity=None,
        proposal_identity=proposal["proposal_id"],
        promoted_kind=proposal["proposal_type"],
        **decision,
    )


class ProfileEnablementService:
    """Fixed production facade with no source or sidecar injection."""

    __slots__ = ()

    def review(self, query: RetrievalQuery, *, budget_profile: str = "Medium") -> dict[str, Any]:
        package = build_production_temporal_candidates(query, budget_profile=budget_profile)
        return build_profile_enablement_review(package, sidecar_path=PRODUCTION_PROFILE_SIDECAR)

    def propose(
        self, *, authority: GateB1ProposalCapability, **proposal: Any,
    ) -> dict[str, Any]:
        target = _validated_target(PRODUCTION_PROFILE_SIDECAR)
        payload = _gate_b1_payload(anchor_validator=None, **proposal)
        return _execute_gate_b1(
            target=target, authority=authority, payload=payload,
            anchor_validator=_production_anchor_validator,
        )

    def decide(
        self, *, authority: GateB2DecisionCapability, **decision: Any,
    ) -> dict[str, Any]:
        target = _validated_target(PRODUCTION_PROFILE_SIDECAR)
        payload = _gate_b2_payload(**decision)
        return _execute_gate_b2(
            target=target, authority=authority, payload=payload,
            temporal_builder=build_production_temporal_candidates,
        )


def open_production_gate_b1(
    *, profile_admin: ProfileAdminSession, **proposal: Any,
) -> GateB1ProposalCapability:
    if not is_trusted_profile_admin_session(profile_admin):
        raise ProfileEnablementError("trusted profile-admin session is required")
    target = _validated_target(PRODUCTION_PROFILE_SIDECAR)
    payload = _gate_b1_payload(anchor_validator=_production_anchor_validator, **proposal)
    return _issue_profile_gate_authority(
        "B1", {"target_path": str(_absolute(target)), **payload},
    )


def open_production_gate_b2(
    *, profile_admin: ProfileAdminSession, **decision: Any,
) -> GateB2DecisionCapability:
    if not is_trusted_profile_admin_session(profile_admin):
        raise ProfileEnablementError("trusted profile-admin session is required")
    target = _validated_target(PRODUCTION_PROFILE_SIDECAR)
    payload = _gate_b2_payload(**decision)
    return _issue_profile_gate_authority(
        "B2", {"target_path": str(_absolute(target)), **payload},
    )


for _issuer in (
    _ProfileEnablementCore.authorize_b1,
    _ProfileEnablementCore.authorize_b2,
    open_production_gate_b1,
    open_production_gate_b2,
):
    _authorize_profile_gate_issuer(_issuer.__code__)
del _issuer
del _authorize_profile_gate_issuer


def build_production_profile_enablement_review(
    query: RetrievalQuery, *, budget_profile: str = "Medium",
) -> dict[str, Any]:
    return ProfileEnablementService().review(query, budget_profile=budget_profile)
