"""YunOS v8.0-A Lite approved-profile control plane.

This layer consumes source-backed v7.2 candidates without changing the sealed
v7.0-v7.4 protocols.  The real chat database remains read-only; the only
mutable state is an independently initialized append-only profile sidecar.
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
from typing import Any, Callable
import weakref

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
from memory.temporal_memory_candidates import (
    PROTOCOL_VERSION as TEMPORAL_PROTOCOL_VERSION,
    build_production_temporal_candidates,
    validate_temporal_candidates_standalone,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PROFILE_SIDECAR = PROJECT_ROOT / "data" / "profile" / "approved_profile.db"
PROTOCOL_VERSION = "v8.0-a-lite-approved-profile-control-2"
SIDECAR_SCHEMA_VERSION = "v8.0-a-lite-approved-profile-sidecar-2"
ACTIVE_PROJECTION_VERSION = "v8.0-a-lite-active-approved-profile-2"
DECISION_ACTIONS = ("approve", "edit_and_approve", "reject", "defer")
MAX_EDITED_STATEMENT_CHARS = 500
MAX_REASON_CHARS = 300

_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_PACKAGE_ID_RE = re.compile(r"tmc_[0-9a-f]{64}")
_PROMOTED_KIND = {
    "fact": "fact",
    "preference": "preference",
    "goal": "goal",
    "decision": "decision",
    "constraint": "constraint",
    "acceptable_alternative": "preference",
    "open_question": None,
}

_SCHEMA_SQL = f"""
CREATE TABLE profile_control_schema (
    schema_version TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);

CREATE TABLE approved_profile_decisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    mutation_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    source_package_identity TEXT NOT NULL,
    source_selection_identity TEXT NOT NULL,
    decision_source TEXT NOT NULL CHECK(decision_source = 'manual'),
    action TEXT NOT NULL CHECK(action IN ('approve','edit_and_approve','reject','defer')),
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    resulting_revision INTEGER NOT NULL UNIQUE CHECK(resulting_revision > 0),
    candidate_snapshot_json TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    profile_item_json TEXT,
    edited_statement TEXT,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE INDEX idx_approved_profile_decisions_candidate
ON approved_profile_decisions(candidate_id, sequence);

CREATE TABLE approved_profile_receipts (
    decision_id TEXT PRIMARY KEY REFERENCES approved_profile_decisions(decision_id),
    receipt_identity TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
);

CREATE TRIGGER approved_profile_decisions_no_update
BEFORE UPDATE ON approved_profile_decisions
BEGIN
    SELECT RAISE(ABORT, 'approved profile decisions are append-only');
END;

CREATE TRIGGER approved_profile_decisions_no_delete
BEFORE DELETE ON approved_profile_decisions
BEGIN
    SELECT RAISE(ABORT, 'approved profile decisions are append-only');
END;

CREATE TRIGGER approved_profile_receipts_no_update
BEFORE UPDATE ON approved_profile_receipts
BEGIN
    SELECT RAISE(ABORT, 'approved profile receipts are append-only');
END;

CREATE TRIGGER approved_profile_receipts_no_delete
BEFORE DELETE ON approved_profile_receipts
BEGIN
    SELECT RAISE(ABORT, 'approved profile receipts are append-only');
END;
"""

_DECISION_COLUMNS = {
    "sequence", "decision_id", "request_id", "mutation_id", "request_fingerprint",
    "candidate_id", "source_package_identity", "source_selection_identity", "decision_source", "action",
    "expected_revision", "resulting_revision", "candidate_snapshot_json",
    "evidence_snapshot_json", "profile_item_json", "edited_statement", "reason", "decided_at",
}

_RECEIPT_COLUMNS = {"decision_id", "receipt_identity", "receipt_json"}


class ApprovedProfileControlError(ValueError):
    """The profile control request or sidecar is invalid."""


class ApprovedProfileSidecarUnavailable(ApprovedProfileControlError):
    """The independently initialized sidecar is unavailable."""


class ApprovedProfileRevisionConflict(ApprovedProfileControlError):
    """A stale page attempted to mutate a newer control-plane revision."""

    def __init__(self, current_revision: int):
        super().__init__("expected_revision does not match the current control revision")
        self.current_revision = current_revision


class ApprovedProfileIdempotencyConflict(ApprovedProfileControlError):
    """A request or mutation key was reused for a different operation."""


class ProfileAdminSession:
    """Unforgeable in-process authority issued only by the explicit admin opener."""

    def __new__(cls):
        raise ApprovedProfileControlError("profile-admin sessions are issued only by the admin opener")


def _build_profile_admin_session_runtime():
    issued: weakref.WeakSet[ProfileAdminSession] = weakref.WeakSet()
    authorized_issuer_codes: set[Any] = set()

    def issue() -> ProfileAdminSession:
        if sys._getframe(1).f_code not in authorized_issuer_codes:
            raise ApprovedProfileControlError("profile-admin session issuer is not authorized")
        session = object.__new__(ProfileAdminSession)
        issued.add(session)
        return session

    def is_trusted(value: Any) -> bool:
        return isinstance(value, ProfileAdminSession) and value in issued

    def authorize(code: Any) -> None:
        authorized_issuer_codes.add(code)

    return issue, is_trusted, authorize


_issue_profile_admin_session, is_trusted_profile_admin_session, _authorize_profile_admin_issuer = (
    _build_profile_admin_session_runtime()
)


def open_production_profile_admin() -> ProfileAdminSession:
    """Enter the explicit in-process authority mode; this initializes no sidecar."""
    return _issue_profile_admin_session()


_authorize_profile_admin_issuer(open_production_profile_admin.__code__)
del _authorize_profile_admin_issuer


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _validated_target(path: str | Path) -> Path:
    candidate = _absolute(path)
    production = _absolute(PRODUCTION_PROFILE_SIDECAR)
    try:
        if candidate == production:
            _, existing_memory_target = validate_memory_sidecar_target(candidate)
            if existing_memory_target:
                raise ApprovedProfileControlError("profile sidecar must not identify the memory sidecar")
            target, is_profile_target = validate_memory_sidecar_target(
                candidate, production_memory_path=production,
            )
            if not is_profile_target:
                raise ApprovedProfileControlError("production profile sidecar identity mismatch")
            return target
        return validate_knowledge_test_db_target(candidate)
    except (ControlledSummaryPathError, KnowledgeSidecarPathError) as exc:
        raise ApprovedProfileControlError("approved profile sidecar path is not allowed") from exc


def is_production_profile_sidecar(path: str | Path) -> bool:
    return _absolute(path) == _absolute(PRODUCTION_PROFILE_SIDECAR)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_writable(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    if is_production_profile_sidecar(path):
        return sqlite3.connect(path, timeout=timeout)
    return connect_alias_safe_test_database(path, timeout=timeout)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO profile_control_schema(schema_version, initialized_at) VALUES(?, ?)",
        (SIDECAR_SCHEMA_VERSION, _utc_now()),
    )
    conn.commit()


def _validate_schema(conn: sqlite3.Connection) -> None:
    schema = conn.execute(
        "SELECT initialized_at FROM profile_control_schema WHERE schema_version = ?",
        (SIDECAR_SCHEMA_VERSION,),
    ).fetchone()
    if schema is None:
        raise ApprovedProfileControlError("approved profile sidecar schema version mismatch")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(approved_profile_decisions)")}
    if columns != _DECISION_COLUMNS:
        raise ApprovedProfileControlError("approved profile decision schema mismatch")
    receipt_columns = {row[1] for row in conn.execute("PRAGMA table_info(approved_profile_receipts)")}
    if receipt_columns != _RECEIPT_COLUMNS:
        raise ApprovedProfileControlError("approved profile receipt schema mismatch")
    triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='approved_profile_decisions'"
        )
    }
    if triggers != {"approved_profile_decisions_no_update", "approved_profile_decisions_no_delete"}:
        raise ApprovedProfileControlError("approved profile append-only triggers are missing")
    receipt_triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='approved_profile_receipts'"
        )
    }
    if receipt_triggers != {"approved_profile_receipts_no_update", "approved_profile_receipts_no_delete"}:
        raise ApprovedProfileControlError("approved profile receipt append-only triggers are missing")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ApprovedProfileControlError("approved profile sidecar integrity check failed")


def profile_sidecar_status(path: str | Path = PRODUCTION_PROFILE_SIDECAR) -> dict[str, Any]:
    try:
        target = _validated_target(path)
    except ApprovedProfileControlError:
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_path_invalid"}
    if not target.is_file():
        return {"status": "missing", "initialized": False, "reason_code": "sidecar_not_initialized"}
    try:
        with _connect_readonly(target) as conn:
            _validate_schema(conn)
    except (sqlite3.Error, ApprovedProfileControlError):
        return {"status": "invalid", "initialized": False, "reason_code": "sidecar_invalid"}
    return {"status": "ready", "initialized": True, "reason_code": "ready"}


def initialize_approved_profile_sidecar(
    path: str | Path = PRODUCTION_PROFILE_SIDECAR,
    *,
    execute: bool = False,
    test_mode: bool = False,
    allow_production_profile_sidecar: bool = False,
    confirm_production_profile_sidecar_init: bool = False,
) -> dict[str, Any]:
    """Initialize only after an explicit execute gate; dry-run creates nothing."""
    confirmations = {
        "execute": execute,
        "allow_production_profile_sidecar": allow_production_profile_sidecar,
        "confirm_production_profile_sidecar_init": confirm_production_profile_sidecar_init,
    }
    if any(value is not True and value is not False for value in confirmations.values()):
        return {
            "status": "refused", "executed": False, "target_kind": "invalid_confirmation",
            "reason_code": "confirmation_type_invalid",
        }
    try:
        target = _validated_target(path)
    except ApprovedProfileControlError:
        return {
            "status": "refused", "executed": False, "target_kind": "invalid_profile_sidecar",
            "reason_code": "sidecar_path_invalid",
        }
    production = is_production_profile_sidecar(target)
    target_kind = "production_profile_sidecar" if production else "test_profile_sidecar"
    existing = profile_sidecar_status(target)
    if existing["status"] == "ready":
        return {"status": "already_initialized", "executed": False, "target_kind": target_kind}
    if target.exists():
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "existing_target_invalid",
        }
    if not production and not test_mode:
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "test_mode_required",
        }
    if execute is not True:
        return {"status": "would_initialize", "executed": False, "target_kind": target_kind}
    if production and not (
        allow_production_profile_sidecar is True and confirm_production_profile_sidecar_init is True
    ):
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "production_confirmation_required",
        }
    if production:
        target.parent.mkdir(parents=True, exist_ok=True)
    created_target = False
    try:
        if production:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created_target = True
            with sqlite3.connect(target) as conn:
                _initialize_schema(conn)
        else:
            with create_alias_safe_test_database_connection(target) as conn:
                _initialize_schema(conn)
        if profile_sidecar_status(target)["status"] != "ready":
            raise ApprovedProfileControlError("initialized approved profile sidecar failed validation")
    except FileExistsError:
        return {
            "status": "refused", "executed": False, "target_kind": target_kind,
            "reason_code": "target_appeared_during_initialization",
        }
    except Exception:
        if created_target and target.exists():
            target.unlink()
        raise
    return {"status": "initialized", "executed": True, "target_kind": target_kind}


def _empty_active_projection(sidecar_state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "protocol_version": ACTIVE_PROJECTION_VERSION,
        "projection_identity": "",
        "status": "unavailable" if not sidecar_state["initialized"] else "empty",
        "reason_code": sidecar_state["reason_code"] if not sidecar_state["initialized"] else "no_active_approved_profile",
        "revision": 0,
        "decision_count": 0,
        "items": [],
        "bindings": [],
        "model_visible": False,
        "readonly": True,
    }
    payload["projection_identity"] = "app_" + _digest({k: v for k, v in payload.items() if k != "projection_identity"})
    return payload


def _validated_stored_profile_item(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        item = json.loads(raw)
        rebuilt = make_approved_profile_item(
            item["statement"], kind=item["kind"], topics=item["topics"],
            approval_id=item["approval"]["approval_id"], approved_at=item["approval"]["approved_at"],
        )
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ApprovedProfileControlError("stored approved profile item is invalid") from exc
    if item != rebuilt:
        raise ApprovedProfileControlError("stored approved profile item identity mismatch")
    return item


def _validate_decision_rows(rows: list[sqlite3.Row]) -> None:
    for index, row in enumerate(rows, start=1):
        try:
            candidate = json.loads(row["candidate_snapshot_json"])
            evidence = json.loads(row["evidence_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ApprovedProfileControlError("stored decision snapshot is invalid") from exc
        if (
            row["resulting_revision"] != index
            or row["expected_revision"] != index - 1
            or row["sequence"] != index
        ):
            raise ApprovedProfileControlError("approved profile revision chain is invalid")
        _validate_identifier(row["request_id"], "stored request_id")
        _validate_identifier(row["mutation_id"], "stored mutation_id")
        if (
            row["action"] not in DECISION_ACTIONS
            or row["decision_source"] != "manual"
            or not _PACKAGE_ID_RE.fullmatch(row["source_package_identity"])
            or not re.fullmatch(r"msp_[0-9a-f]{64}", row["source_selection_identity"])
            or not isinstance(candidate, dict)
            or candidate.get("candidate_id") != row["candidate_id"]
            or not re.fullmatch(r"mc_[0-9a-f]{64}", row["candidate_id"])
            or not isinstance(evidence, dict)
            or evidence.get("event_id") != candidate.get("event_id")
            or evidence.get("evidence_ids") != candidate.get("evidence", {}).get("evidence_ids")
        ):
            raise ApprovedProfileControlError("stored decision source binding is invalid")
        edited = row["edited_statement"] or ""
        if _privacy_safe_text(
            edited, label="stored edited_statement", maximum=MAX_EDITED_STATEMENT_CHARS,
            required=row["action"] == "edit_and_approve",
        ) != edited or _privacy_safe_text(
            row["reason"], label="stored reason", maximum=MAX_REASON_CHARS, required=False,
        ) != row["reason"]:
            raise ApprovedProfileControlError("stored decision text is not canonical")
        fingerprint_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": row["request_id"],
            "mutation_id": row["mutation_id"],
            "package_identity": row["source_package_identity"],
            "candidate": candidate,
            "evidence": evidence,
            "decision_source": row["decision_source"],
            "action": row["action"],
            "expected_revision": row["expected_revision"],
            "edited_statement": edited,
            "reason": row["reason"],
        }
        fingerprint = hashlib.sha256(_dump(fingerprint_payload).encode("utf-8")).hexdigest()
        expected_decision_id = "apd_" + _digest({
            "request_fingerprint": fingerprint,
            "resulting_revision": row["resulting_revision"],
            "decided_at": row["decided_at"],
        })
        if row["request_fingerprint"] != fingerprint or row["decision_id"] != expected_decision_id:
            raise ApprovedProfileControlError("stored decision identity mismatch")
        profile_item = _validated_stored_profile_item(row["profile_item_json"])
        approved = row["action"] in {"approve", "edit_and_approve"}
        if approved != (profile_item is not None):
            raise ApprovedProfileControlError("stored decision projection state mismatch")
        if row["action"] == "edit_and_approve" and not edited:
            raise ApprovedProfileControlError("stored edited approval is missing its statement")
        if row["action"] != "edit_and_approve" and edited:
            raise ApprovedProfileControlError("stored non-edit decision contains edited text")


def _active_projection_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    _validate_decision_rows(rows)
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[row["candidate_id"]] = row
    items: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for candidate_id, row in latest.items():
        if row["action"] not in {"approve", "edit_and_approve"}:
            continue
        item = _validated_stored_profile_item(row["profile_item_json"])
        item = deepcopy(item)
        item["model_visible"] = True
        items.append(item)
        bindings.append({
            "profile_item_id": item["profile_item_id"],
            "candidate_id": candidate_id,
            "decision_id": row["decision_id"],
            "source_package_identity": row["source_package_identity"],
            "revision": row["resulting_revision"],
        })
    paired = sorted(zip(items, bindings), key=lambda pair: pair[0]["profile_item_id"])
    items = [pair[0] for pair in paired]
    bindings = [pair[1] for pair in paired]
    revision = int(rows[-1]["resulting_revision"]) if rows else 0
    projection = {
        "protocol_version": ACTIVE_PROJECTION_VERSION,
        "projection_identity": "",
        "status": "ok" if items else "empty",
        "reason_code": "active_approved_profile_available" if items else "no_active_approved_profile",
        "revision": revision,
        "decision_count": len(rows),
        "items": items,
        "bindings": bindings,
        "model_visible": bool(items),
        "readonly": True,
    }
    projection["projection_identity"] = "app_" + _digest({k: v for k, v in projection.items() if k != "projection_identity"})
    return projection


def active_approved_profile_projection(
    sidecar_path: str | Path = PRODUCTION_PROFILE_SIDECAR,
) -> dict[str, Any]:
    target = _validated_target(sidecar_path)
    state = profile_sidecar_status(target)
    if state["status"] == "missing":
        return _empty_active_projection(state)
    if state["status"] != "ready":
        raise ApprovedProfileSidecarUnavailable("approved profile sidecar is invalid")
    with _connect_readonly(target) as conn:
        rows = conn.execute(
            "SELECT * FROM approved_profile_decisions ORDER BY sequence ASC"
        ).fetchall()
        receipts = conn.execute(
            "SELECT * FROM approved_profile_receipts ORDER BY rowid ASC"
        ).fetchall()
    _validate_receipts(rows, receipts)
    return _active_projection_from_rows(rows)


def _candidate_evidence(package: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    event = next((item for item in package["events"] if item["event_id"] == candidate["event_id"]), None)
    if event is None:
        raise ApprovedProfileControlError("candidate event binding is missing")
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


def _decision_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "candidate_id": row["candidate_id"],
        "decision_source": row["decision_source"],
        "action": row["action"],
        "resulting_revision": row["resulting_revision"],
        "reason": row["reason"],
        "decided_at": row["decided_at"],
    }


def _decision_history_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **_decision_summary(row),
        "request_id": row["request_id"],
        "mutation_id": row["mutation_id"],
        "source_package_identity": row["source_package_identity"],
        "source_selection_identity": row["source_selection_identity"],
        "expected_revision": row["expected_revision"],
        "candidate": json.loads(row["candidate_snapshot_json"]),
        "evidence": json.loads(row["evidence_snapshot_json"]),
        "edited_statement": row["edited_statement"] or "",
        "profile_item": _validated_stored_profile_item(row["profile_item_json"]),
    }


def _make_receipt(row: sqlite3.Row, projection: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "protocol_version": PROTOCOL_VERSION,
        "receipt_identity": "",
        "status": "persisted",
        "revision": row["resulting_revision"],
        "decision": _decision_summary(row),
        "active_approved_profile": projection,
    }
    receipt["receipt_identity"] = "apr_" + _digest({
        key: value for key, value in receipt.items() if key != "receipt_identity"
    })
    return receipt


def _validate_receipts(
    decision_rows: list[sqlite3.Row], receipt_rows: list[sqlite3.Row],
) -> None:
    _validate_decision_rows(decision_rows)
    if len(receipt_rows) != len(decision_rows):
        raise ApprovedProfileControlError("approved profile receipt chain is incomplete")
    receipts_by_decision = {row["decision_id"]: row for row in receipt_rows}
    if len(receipts_by_decision) != len(receipt_rows):
        raise ApprovedProfileControlError("approved profile receipt identity is duplicated")
    for index, decision in enumerate(decision_rows, start=1):
        stored = receipts_by_decision.get(decision["decision_id"])
        if stored is None:
            raise ApprovedProfileControlError("approved profile decision receipt is missing")
        try:
            payload = json.loads(stored["receipt_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ApprovedProfileControlError("stored approved profile receipt is invalid") from exc
        expected = _make_receipt(decision, _active_projection_from_rows(decision_rows[:index]))
        if (
            payload != expected
            or stored["receipt_identity"] != expected["receipt_identity"]
            or stored["decision_id"] != decision["decision_id"]
        ):
            raise ApprovedProfileControlError("stored approved profile receipt identity mismatch")


def build_approved_profile_review(
    temporal_package: dict[str, Any],
    *,
    sidecar_path: str | Path,
) -> dict[str, Any]:
    """Private/test seam over an already trusted package; production rebuilds v7.2 itself."""
    validate_temporal_candidates_standalone(temporal_package)
    target = _validated_target(sidecar_path)
    active = active_approved_profile_projection(target)
    state = profile_sidecar_status(target)
    latest: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    if state["status"] == "ready":
        with _connect_readonly(target) as conn:
            rows = conn.execute("SELECT * FROM approved_profile_decisions ORDER BY sequence").fetchall()
            receipts = conn.execute("SELECT * FROM approved_profile_receipts ORDER BY rowid").fetchall()
            _validate_receipts(rows, receipts)
            for row in rows:
                latest[row["candidate_id"]] = _decision_summary(row)
                history.append(_decision_history_entry(row))
    candidates = []
    for candidate in temporal_package["candidates"]:
        promoted_kind = _PROMOTED_KIND[candidate["candidate_type"]]
        current = latest.get(candidate["candidate_id"])
        candidates.append({
            "candidate": deepcopy(candidate),
            "evidence": _candidate_evidence(temporal_package, candidate),
            "current_decision": current,
            "active": bool(current and current["action"] in {"approve", "edit_and_approve"}),
            "promoted_kind": promoted_kind,
            "allowed_actions": (
                ["approve", "edit_and_approve", "reject", "defer"]
                if promoted_kind else ["reject", "defer"]
            ),
        })
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok" if candidates else "empty",
        "reason_code": "candidates_available" if candidates else "no_candidates",
        "source": {
            "protocol_version": TEMPORAL_PROTOCOL_VERSION,
            "package_identity": temporal_package["package_identity"],
            "selection_identity": temporal_package["source_projection"]["package_identity"],
            "readonly": True,
            "source_backed_required": True,
        },
        "control_revision": active["revision"],
        "sidecar": state,
        "mutation_available": state["status"] == "ready",
        "candidates": candidates,
        "decision_history": history,
        "active_approved_profile": active,
        "execution": {
            "source_readonly": True,
            "source_persisted": False,
            "external_model_calls": False,
            "sent_to_model": False,
        },
    }


def _validate_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ApprovedProfileControlError(f"invalid {label}")
    return value


def _privacy_safe_text(value: Any, *, label: str, maximum: int, required: bool) -> str:
    if not isinstance(value, str):
        raise ApprovedProfileControlError(f"{label} must be text")
    text = " ".join(value.split())
    if required and not text:
        raise ApprovedProfileControlError(f"{label} is required")
    if len(text) > maximum:
        raise ApprovedProfileControlError(f"{label} exceeds the character limit")
    if redact_context_text(text) != text:
        raise ApprovedProfileControlError(f"{label} violates the v7.0 privacy boundary")
    return text


def _existing_idempotent_row(
    conn: sqlite3.Connection, request_id: str, mutation_id: str, fingerprint: str,
) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT * FROM approved_profile_decisions WHERE request_id = ? OR mutation_id = ?",
        (request_id, mutation_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ApprovedProfileIdempotencyConflict("request_id and mutation_id refer to different decisions")
    row = rows[0]
    if (
        row["request_id"] != request_id
        or row["mutation_id"] != mutation_id
        or row["request_fingerprint"] != fingerprint
    ):
        raise ApprovedProfileIdempotencyConflict("request_id or mutation_id was reused with another payload")
    return row


def _append_approved_profile_decision(
    temporal_package: dict[str, Any],
    *,
    target: Path,
    package_identity: str,
    candidate_id: str,
    action: str,
    expected_revision: int,
    request_id: str,
    mutation_id: str,
    edited_statement: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Private INSERT primitive; callers must establish package authority first."""
    validate_temporal_candidates_standalone(temporal_package)
    state = profile_sidecar_status(target)
    if state["status"] != "ready":
        raise ApprovedProfileSidecarUnavailable("approved profile sidecar is not initialized")
    if not isinstance(package_identity, str) or not _PACKAGE_ID_RE.fullmatch(package_identity):
        raise ApprovedProfileControlError("invalid source package identity")
    if package_identity != temporal_package["package_identity"]:
        raise ApprovedProfileControlError("source package identity is stale")
    if not isinstance(candidate_id, str) or not re.fullmatch(r"mc_[0-9a-f]{64}", candidate_id):
        raise ApprovedProfileControlError("invalid candidate identity")
    candidate = next((item for item in temporal_package["candidates"] if item["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ApprovedProfileControlError("candidate is not present in the source-backed package")
    if action not in DECISION_ACTIONS:
        raise ApprovedProfileControlError("invalid profile decision action")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ApprovedProfileControlError("expected_revision must be a non-negative integer")
    request_id = _validate_identifier(request_id, "request_id")
    mutation_id = _validate_identifier(mutation_id, "mutation_id")
    edited = _privacy_safe_text(
        edited_statement, label="edited_statement", maximum=MAX_EDITED_STATEMENT_CHARS,
        required=action == "edit_and_approve",
    )
    reason_text = _privacy_safe_text(reason, label="reason", maximum=MAX_REASON_CHARS, required=False)
    if action != "edit_and_approve" and edited:
        raise ApprovedProfileControlError("edited_statement is accepted only for edit_and_approve")
    promoted_kind = _PROMOTED_KIND[candidate["candidate_type"]]
    if action in {"approve", "edit_and_approve"} and promoted_kind is None:
        raise ApprovedProfileControlError("open_question candidates cannot become approved profile items")
    evidence = _candidate_evidence(temporal_package, candidate)
    fingerprint_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "mutation_id": mutation_id,
        "package_identity": package_identity,
        "candidate": candidate,
        "evidence": evidence,
        "decision_source": "manual",
        "action": action,
        "expected_revision": expected_revision,
        "edited_statement": edited,
        "reason": reason_text,
    }
    fingerprint = hashlib.sha256(_dump(fingerprint_payload).encode("utf-8")).hexdigest()
    conn = _connect_writable(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_schema(conn)
        existing_rows = conn.execute(
            "SELECT * FROM approved_profile_decisions ORDER BY sequence ASC"
        ).fetchall()
        existing_receipts = conn.execute(
            "SELECT * FROM approved_profile_receipts ORDER BY rowid ASC"
        ).fetchall()
        _validate_receipts(existing_rows, existing_receipts)
        existing = _existing_idempotent_row(conn, request_id, mutation_id, fingerprint)
        if existing is not None:
            stored_receipt = next(
                row for row in existing_receipts if row["decision_id"] == existing["decision_id"]
            )
            receipt = json.loads(stored_receipt["receipt_json"])
            conn.rollback()
            return receipt
        current_revision = len(existing_rows)
        if expected_revision != current_revision:
            conn.rollback()
            raise ApprovedProfileRevisionConflict(current_revision)
        resulting_revision = current_revision + 1
        decided_at = _utc_now()
        decision_id = "apd_" + _digest({
            "request_fingerprint": fingerprint,
            "resulting_revision": resulting_revision,
            "decided_at": decided_at,
        })
        profile_item = None
        if action in {"approve", "edit_and_approve"}:
            statement = edited if action == "edit_and_approve" else candidate["statement"]
            profile_item = make_approved_profile_item(
                statement,
                kind=promoted_kind,
                topics=[candidate["candidate_type"], candidate["statement"]],
                approval_id=decision_id,
                approved_at=decided_at,
            )
            if profile_item["statement"] != statement:
                raise ApprovedProfileControlError("approved statement violates the v7.0 privacy boundary")
        conn.execute(
            """
            INSERT INTO approved_profile_decisions(
                decision_id, request_id, mutation_id, request_fingerprint, candidate_id,
                source_package_identity, source_selection_identity, decision_source, action, expected_revision,
                resulting_revision, candidate_snapshot_json, evidence_snapshot_json,
                profile_item_json, edited_statement, reason, decided_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id, request_id, mutation_id, fingerprint, candidate_id,
                package_identity, temporal_package["source_projection"]["package_identity"], "manual", action,
                expected_revision, resulting_revision, _dump(candidate), _dump(evidence),
                _dump(profile_item) if profile_item is not None else None,
                edited or None, reason_text, decided_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM approved_profile_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        decision_rows = [*existing_rows, row]
        receipt = _make_receipt(row, _active_projection_from_rows(decision_rows))
        conn.execute(
            "INSERT INTO approved_profile_receipts(decision_id, receipt_identity, receipt_json) VALUES(?,?,?)",
            (decision_id, receipt["receipt_identity"], _dump(receipt)),
        )
        receipt_rows = conn.execute(
            "SELECT * FROM approved_profile_receipts ORDER BY rowid ASC"
        ).fetchall()
        _validate_receipts(decision_rows, receipt_rows)
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    return receipt


def _record_test_approved_profile_decision(
    temporal_package: dict[str, Any],
    *,
    sidecar_path: str | Path,
    package_identity: str,
    candidate_id: str,
    action: str,
    expected_revision: int,
    request_id: str,
    mutation_id: str,
    edited_statement: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Private synthetic-test seam. It is structurally unable to target production."""
    target = _validated_target(sidecar_path)
    if is_production_profile_sidecar(target):
        raise ApprovedProfileControlError("test decision primitive cannot target production")
    return _append_approved_profile_decision(
        temporal_package,
        target=target,
        package_identity=package_identity,
        candidate_id=candidate_id,
        action=action,
        expected_revision=expected_revision,
        request_id=request_id,
        mutation_id=mutation_id,
        edited_statement=edited_statement,
        reason=reason,
    )


class _ApprovedProfileControlCore:
    """Private source-backed test core with fixed source builder and test sidecar."""

    def __init__(
        self,
        temporal_builder: Callable[..., dict[str, Any]],
        sidecar_path: str | Path,
    ) -> None:
        self.temporal_builder = temporal_builder
        self.sidecar_path = _validated_target(sidecar_path)
        if is_production_profile_sidecar(self.sidecar_path):
            raise ApprovedProfileControlError("test control core cannot target production")

    def review(self, query: RetrievalQuery, *, budget_profile: str = "Medium") -> dict[str, Any]:
        package = self.temporal_builder(query, budget_profile=budget_profile)
        return build_approved_profile_review(package, sidecar_path=self.sidecar_path)

    def decide(self, query: RetrievalQuery, **decision: Any) -> dict[str, Any]:
        budget_profile = decision.pop("budget_profile", "Medium")
        package = self.temporal_builder(query, budget_profile=budget_profile)
        return _append_approved_profile_decision(
            package, target=self.sidecar_path, **decision,
        )


class ApprovedProfileControlService:
    """Fixed production service with no source database or sidecar injection."""

    def review(self, query: RetrievalQuery, *, budget_profile: str = "Medium") -> dict[str, Any]:
        package = build_production_temporal_candidates(query, budget_profile=budget_profile)
        return build_approved_profile_review(package, sidecar_path=PRODUCTION_PROFILE_SIDECAR)

    def decide(
        self,
        query: RetrievalQuery,
        *,
        profile_admin: ProfileAdminSession,
        budget_profile: str,
        package_identity: str,
        candidate_id: str,
        action: str,
        expected_revision: int,
        request_id: str,
        mutation_id: str,
        edited_statement: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        if not is_trusted_profile_admin_session(profile_admin):
            raise ApprovedProfileControlError("trusted profile-admin session is required")
        package = build_production_temporal_candidates(query, budget_profile=budget_profile)
        return _append_approved_profile_decision(
            package,
            target=_validated_target(PRODUCTION_PROFILE_SIDECAR),
            package_identity=package_identity,
            candidate_id=candidate_id,
            action=action,
            expected_revision=expected_revision,
            request_id=request_id,
            mutation_id=mutation_id,
            edited_statement=edited_statement,
            reason=reason,
        )


def build_production_approved_profile_review(
    query: RetrievalQuery, *, budget_profile: str = "Medium",
) -> dict[str, Any]:
    return ApprovedProfileControlService().review(query, budget_profile=budget_profile)


def record_production_approved_profile_decision(
    query: RetrievalQuery,
    *,
    profile_admin: ProfileAdminSession,
    budget_profile: str,
    package_identity: str,
    candidate_id: str,
    action: str,
    expected_revision: int,
    request_id: str,
    mutation_id: str,
    edited_statement: str = "",
    reason: str = "",
) -> dict[str, Any]:
    return ApprovedProfileControlService().decide(
        query,
        profile_admin=profile_admin,
        budget_profile=budget_profile,
        package_identity=package_identity,
        candidate_id=candidate_id,
        action=action,
        expected_revision=expected_revision,
        request_id=request_id,
        mutation_id=mutation_id,
        edited_statement=edited_statement,
        reason=reason,
    )
