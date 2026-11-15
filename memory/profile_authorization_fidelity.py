from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any, Callable, Iterable
import weakref

from memory.approved_profile_control import (
    PRODUCTION_PROFILE_SIDECAR,
    ProfileAdminSession,
    is_trusted_profile_admin_session,
)
from memory.context_assembly import make_approved_profile_item
from memory.db_path_policy import (
    KnowledgeSidecarPathError,
    connect_alias_safe_test_database,
    validate_knowledge_test_db_target,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.profile_enablement import (
    GATE_B1_AUTHORIZATION_VERSION as D1_GATE_B1_AUTHORIZATION_VERSION,
    GATE_B2_AUTHORIZATION_VERSION as D1_GATE_B2_AUTHORIZATION_VERSION,
    MAX_REASON_CHARS,
    MAX_STATEMENT_CHARS,
    PROPOSAL_TYPES,
    PROPOSAL_VERSION as D1_PROPOSAL_VERSION,
    PROTOCOL_VERSION as D1_CONTROL_VERSION,
    ProfileEnablementError,
    ProfileEnablementIdempotencyConflict,
    ProfileEnablementRevisionConflict,
    _canonical_anchors,
    _candidate_evidence,
    _CANDIDATE_RE,
    _decision_receipt,
    _digest as _d1_digest,
    _dump as _d1_dump,
    _production_anchor_validator,
    _PACKAGE_RE,
    _PROMOTED_KIND,
    _projection_from_rows,
    _proposal_receipt,
    _proposal_snapshot,
    _safe_text,
    _trigger_sql,
    _utc_now,
    _validate_decision_receipts,
    _validate_proposals,
    _validate_schema as _validate_d1_schema,
    is_production_profile_enablement_sidecar,
    profile_enablement_sidecar_status,
)
from memory.temporal_memory_candidates import (
    build_production_temporal_candidates,
    validate_temporal_candidates_standalone,
)


CONTROL_VERSION = "v8.0-d2-authorization-fidelity-control-1"
MANIFEST_VERSION = "v8.0-d2-profile-authorization-manifest-1"
OVERLAY_SCHEMA_VERSION = "v8.0-d2-authorization-fidelity-overlay-1"
BATCH_RECEIPT_VERSION = "v8.0-d2-authorization-fidelity-batch-receipt-1"
GATE_B1_CAPABILITY_VERSION = "v8.0-d2-gate-b1-manifest-capability-1"
GATE_B2_CAPABILITY_VERSION = "v8.0-d2-gate-b2-manifest-capability-1"

_MANIFEST_RE = re.compile(r"paf_[0-9a-f]{64}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_PROPOSAL_RE = re.compile(r"dpp_[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_DECISION_ACTIONS = {"approve", "edit_and_approve", "reject", "defer"}

_OVERLAY_STATEMENTS = (
f"""CREATE TABLE IF NOT EXISTS d2_authorization_schema (
    schema_version TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
)""",
f"""CREATE TABLE IF NOT EXISTS d2_authorization_manifests (
    manifest_identity TEXT PRIMARY KEY,
    manifest_version TEXT NOT NULL CHECK(manifest_version = '{MANIFEST_VERSION}'),
    gate TEXT NOT NULL CHECK(gate IN ('B1','B2')),
    canonical_ascii BLOB NOT NULL,
    canonical_sha256 TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK(item_count > 0),
    recorded_at TEXT NOT NULL
)""",
f"""CREATE TABLE IF NOT EXISTS d2_authorization_results (
    manifest_identity TEXT NOT NULL REFERENCES d2_authorization_manifests(manifest_identity),
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    result_kind TEXT NOT NULL CHECK(result_kind IN ('proposal','decision')),
    subject_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_receipt_identity TEXT NOT NULL,
    text_utf8_sha256 TEXT,
    result_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(manifest_identity, ordinal),
    UNIQUE(record_id)
)""",
f"""CREATE TABLE IF NOT EXISTS d2_authorization_batch_receipts (
    manifest_identity TEXT PRIMARY KEY REFERENCES d2_authorization_manifests(manifest_identity),
    receipt_identity TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL
)""",
)

_OVERLAY_TABLES = (
    "d2_authorization_schema",
    "d2_authorization_manifests",
    "d2_authorization_results",
    "d2_authorization_batch_receipts",
)

_OVERLAY_COLUMNS = {
    "d2_authorization_schema": {"schema_version", "initialized_at"},
    "d2_authorization_manifests": {
        "manifest_identity", "manifest_version", "gate", "canonical_ascii",
        "canonical_sha256", "item_count", "recorded_at",
    },
    "d2_authorization_results": {
        "manifest_identity", "ordinal", "result_kind", "subject_id", "record_id",
        "record_receipt_identity", "text_utf8_sha256", "result_json", "recorded_at",
    },
    "d2_authorization_batch_receipts": {
        "manifest_identity", "receipt_identity", "receipt_json",
    },
}


class AuthorizationFidelityError(ProfileEnablementError):
    """The ASCII manifest or its exact UTF-8 authorization binding is invalid."""


class AuthorizationFidelitySidecarUnavailable(AuthorizationFidelityError):
    """The target is not a valid D1 profile sidecar."""


def _canonical_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")
    if any(byte > 0x7F for byte in rendered):
        raise AuthorizationFidelityError("manifest canonical bytes must be ASCII")
    return rendered


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise AuthorizationFidelityError("manifest is not ASCII-safe") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AuthorizationFidelityError("manifest contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise AuthorizationFidelityError("manifest JSON is invalid") from exc
    if not isinstance(value, dict):
        raise AuthorizationFidelityError("manifest must be a JSON object")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise AuthorizationFidelityError(f"invalid {label}")
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthorizationFidelityError("expected_revision must be a non-negative integer")
    return value


def _exact_text_record(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise AuthorizationFidelityError(f"{label} must be non-empty text")
    if "\ufffd" in value:
        raise AuthorizationFidelityError(f"{label} contains a Unicode replacement character")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorizationFidelityError(f"{label} is not valid Unicode") from exc
    if len(value) > MAX_STATEMENT_CHARS:
        raise AuthorizationFidelityError(f"{label} exceeds the character limit")
    if _safe_text(value, label=label, maximum=MAX_STATEMENT_CHARS, required=True) != value:
        raise AuthorizationFidelityError(f"{label} would require forbidden normalization")
    return {
        "encoding": "utf-8",
        "base64": base64.b64encode(raw).decode("ascii"),
        "utf8_sha256": _sha256_bytes(raw),
        "codepoints": [ord(char) for char in value],
    }


def _decode_text_record(value: Any, *, label: str) -> tuple[str, bytes, str]:
    if not isinstance(value, dict) or set(value) != {
        "encoding", "base64", "utf8_sha256", "codepoints",
    }:
        raise AuthorizationFidelityError(f"{label} record fields mismatch")
    if value["encoding"] != "utf-8" or not isinstance(value["base64"], str):
        raise AuthorizationFidelityError(f"{label} encoding is invalid")
    if not isinstance(value["utf8_sha256"], str) or not _SHA_RE.fullmatch(value["utf8_sha256"]):
        raise AuthorizationFidelityError(f"{label} UTF-8 hash is invalid")
    try:
        raw = base64.b64decode(value["base64"].encode("ascii"), validate=True)
        text = raw.decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
        raise AuthorizationFidelityError(f"{label} bytes are invalid") from exc
    if "\ufffd" in text or _sha256_bytes(raw) != value["utf8_sha256"]:
        raise AuthorizationFidelityError(f"{label} UTF-8 fidelity check failed")
    codepoints = value["codepoints"]
    if (
        not isinstance(codepoints, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in codepoints)
        or codepoints != [ord(char) for char in text]
    ):
        raise AuthorizationFidelityError(f"{label} code point binding mismatch")
    expected = _exact_text_record(text, label=label)
    if expected != value:
        raise AuthorizationFidelityError(f"{label} canonical record mismatch")
    return text, raw, value["utf8_sha256"]


def _canonical_manifest_payload(gate: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "gate": gate,
        "batch_policy": "all_or_nothing",
        "items": items,
    }


def _canonical_b1_item(item: Any, ordinal: int, *, anchor_validator: Callable[[str, str], bool] | None) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {
        "text", "proposal_type", "source_anchors", "user_identity",
        "expected_revision", "request_id", "mutation_id",
    }:
        raise AuthorizationFidelityError("Gate B1 manifest item fields mismatch")
    if item["proposal_type"] not in PROPOSAL_TYPES or item["user_identity"] != "local_user":
        raise AuthorizationFidelityError("Gate B1 proposal type or user identity is invalid")
    return {
        "ordinal": ordinal,
        "proposal_type": item["proposal_type"],
        "source_anchors": _canonical_anchors(item["source_anchors"], validator=anchor_validator),
        "user_identity": "local_user",
        "expected_revision": _revision(item["expected_revision"]),
        "request_id": _identifier(item["request_id"], "request_id"),
        "mutation_id": _identifier(item["mutation_id"], "mutation_id"),
        "text": _exact_text_record(item["text"], label="proposal text"),
    }


def _canonical_b2_item(item: Any, ordinal: int) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {
        "subject_origin", "subject_id", "action", "expected_revision", "request_id",
        "mutation_id", "reason", "edited_text", "query_text", "budget_profile",
        "package_identity",
    }:
        raise AuthorizationFidelityError("Gate B2 manifest item fields mismatch")
    origin = item["subject_origin"]
    if origin == "direct_user_proposal":
        if not isinstance(item["subject_id"], str) or not _PROPOSAL_RE.fullmatch(item["subject_id"]):
            raise AuthorizationFidelityError("invalid Gate B2 proposal identity")
        if item["query_text"] not in (None, "") or item["budget_profile"] not in (None, "") or item["package_identity"] is not None:
            raise AuthorizationFidelityError("proposal decision cannot bind candidate source fields")
        query_text, budget_profile, package_identity = "", "", None
    elif origin == "v7.2_candidate":
        if not isinstance(item["subject_id"], str) or not _CANDIDATE_RE.fullmatch(item["subject_id"]):
            raise AuthorizationFidelityError("invalid Gate B2 candidate identity")
        if not isinstance(item["package_identity"], str) or not _PACKAGE_RE.fullmatch(item["package_identity"]):
            raise AuthorizationFidelityError("invalid Gate B2 source package identity")
        query_text = _safe_text(
            item["query_text"], label="candidate query", maximum=500, required=True,
        )
        if query_text != item["query_text"] or item["budget_profile"] not in {"Small", "Medium", "Large"}:
            raise AuthorizationFidelityError("candidate query or budget requires forbidden normalization")
        budget_profile, package_identity = item["budget_profile"], item["package_identity"]
    else:
        raise AuthorizationFidelityError("invalid Gate B2 subject origin")
    if item["action"] not in _DECISION_ACTIONS:
        raise AuthorizationFidelityError("invalid Gate B2 action")
    reason = _safe_text(item["reason"], label="reason", maximum=MAX_REASON_CHARS, required=False)
    if reason != item["reason"]:
        raise AuthorizationFidelityError("reason would require forbidden normalization")
    edited = item["edited_text"]
    if item["action"] == "edit_and_approve":
        text_record: dict[str, Any] | None = _exact_text_record(edited, label="final edited text")
    else:
        if edited not in (None, ""):
            raise AuthorizationFidelityError("non-edit action cannot contain edited text")
        text_record = None
    return {
        "ordinal": ordinal,
        "subject_origin": origin,
        "subject_id": item["subject_id"],
        "action": item["action"],
        "expected_revision": _revision(item["expected_revision"]),
        "request_id": _identifier(item["request_id"], "request_id"),
        "mutation_id": _identifier(item["mutation_id"], "mutation_id"),
        "reason": reason,
        "edited_text": text_record,
        "query_text": query_text,
        "budget_profile": budget_profile,
        "package_identity": package_identity,
    }


def build_ascii_authorization_manifest(
    gate: str,
    items: Iterable[dict[str, Any]],
    *,
    anchor_validator: Callable[[str, str], bool] | None = None,
) -> str:
    if gate not in {"B1", "B2"}:
        raise AuthorizationFidelityError("manifest gate must be B1 or B2")
    source = list(items)
    if not source:
        raise AuthorizationFidelityError("manifest must contain at least one item")
    canonical_items = [
        _canonical_b1_item(item, index, anchor_validator=anchor_validator)
        if gate == "B1" else _canonical_b2_item(item, index)
        for index, item in enumerate(source, start=1)
    ]
    canonical = _canonical_json_bytes(_canonical_manifest_payload(gate, canonical_items))
    identity = "paf_" + _sha256_bytes(canonical)
    text_hashes = [
        item["text"]["utf8_sha256"] if gate == "B1"
        else item["edited_text"]["utf8_sha256"] if item["edited_text"] is not None else None
        for item in canonical_items
    ]
    envelope = {
        "envelope_version": CONTROL_VERSION,
        "manifest_identity": identity,
        "canonical_manifest_base64": base64.b64encode(canonical).decode("ascii"),
        "item_text_utf8_sha256": text_hashes,
    }
    return _canonical_json_bytes(envelope).decode("ascii")


@dataclass(frozen=True)
class ParsedAuthorizationManifest:
    identity: str
    gate: str
    canonical_ascii: bytes
    payload: dict[str, Any]
    item_text_hashes: tuple[str | None, ...]


def parse_ascii_authorization_manifest(
    envelope_ascii: str | bytes,
    *,
    asserted_identity: str,
    asserted_item_text_hashes: Iterable[str | None],
    anchor_validator: Callable[[str, str], bool] | None = None,
) -> ParsedAuthorizationManifest:
    if isinstance(envelope_ascii, str):
        try:
            envelope_raw = envelope_ascii.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise AuthorizationFidelityError("manifest envelope is not ASCII") from exc
    elif isinstance(envelope_ascii, bytes):
        envelope_raw = bytes(envelope_ascii)
    else:
        raise AuthorizationFidelityError("manifest envelope must be ASCII text or bytes")
    envelope = _strict_json_object(envelope_raw)
    if set(envelope) != {
        "envelope_version", "manifest_identity", "canonical_manifest_base64",
        "item_text_utf8_sha256",
    } or envelope["envelope_version"] != CONTROL_VERSION:
        raise AuthorizationFidelityError("manifest envelope fields mismatch")
    if _canonical_json_bytes(envelope) != envelope_raw:
        raise AuthorizationFidelityError("manifest envelope is not canonical")
    identity = envelope["manifest_identity"]
    if not isinstance(identity, str) or not _MANIFEST_RE.fullmatch(identity):
        raise AuthorizationFidelityError("manifest identity is invalid")
    try:
        canonical = base64.b64decode(envelope["canonical_manifest_base64"].encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, ValueError) as exc:
        raise AuthorizationFidelityError("manifest canonical bytes encoding is invalid") from exc
    payload = _strict_json_object(canonical)
    if set(payload) != {"manifest_version", "gate", "batch_policy", "items"}:
        raise AuthorizationFidelityError("manifest payload fields mismatch")
    if payload["manifest_version"] != MANIFEST_VERSION or payload["batch_policy"] != "all_or_nothing":
        raise AuthorizationFidelityError("manifest version or batch policy mismatch")
    gate = payload["gate"]
    if gate not in {"B1", "B2"} or not isinstance(payload["items"], list) or not payload["items"]:
        raise AuthorizationFidelityError("manifest gate or items are invalid")
    rebuilt_items: list[dict[str, Any]] = []
    decoded_hashes: list[str | None] = []
    for index, raw_item in enumerate(payload["items"], start=1):
        if not isinstance(raw_item, dict) or raw_item.get("ordinal") != index:
            raise AuthorizationFidelityError("manifest item order is invalid")
        if gate == "B1":
            if set(raw_item) != {
                "ordinal", "proposal_type", "source_anchors", "user_identity",
                "expected_revision", "request_id", "mutation_id", "text",
            }:
                raise AuthorizationFidelityError("stored Gate B1 item fields mismatch")
            text, _raw, digest = _decode_text_record(raw_item["text"], label="proposal text")
            rebuilt = _canonical_b1_item({
                "text": text,
                "proposal_type": raw_item["proposal_type"],
                "source_anchors": raw_item["source_anchors"],
                "user_identity": raw_item["user_identity"],
                "expected_revision": raw_item["expected_revision"],
                "request_id": raw_item["request_id"],
                "mutation_id": raw_item["mutation_id"],
            }, index, anchor_validator=anchor_validator)
        else:
            if set(raw_item) != {
                "ordinal", "subject_origin", "subject_id", "action", "expected_revision",
                "request_id", "mutation_id", "reason", "edited_text", "query_text",
                "budget_profile", "package_identity",
            }:
                raise AuthorizationFidelityError("stored Gate B2 item fields mismatch")
            if raw_item["edited_text"] is None:
                text, digest = "", None
            else:
                text, _raw, digest = _decode_text_record(raw_item["edited_text"], label="final edited text")
            rebuilt = _canonical_b2_item({
                "subject_origin": raw_item["subject_origin"],
                "subject_id": raw_item["subject_id"],
                "action": raw_item["action"],
                "expected_revision": raw_item["expected_revision"],
                "request_id": raw_item["request_id"],
                "mutation_id": raw_item["mutation_id"],
                "reason": raw_item["reason"],
                "edited_text": text,
                "query_text": raw_item["query_text"],
                "budget_profile": raw_item["budget_profile"],
                "package_identity": raw_item["package_identity"],
            }, index)
        rebuilt_items.append(rebuilt)
        decoded_hashes.append(digest)
    rebuilt_payload = _canonical_manifest_payload(gate, rebuilt_items)
    if _canonical_json_bytes(rebuilt_payload) != canonical:
        raise AuthorizationFidelityError("manifest canonical content mismatch")
    expected_identity = "paf_" + _sha256_bytes(canonical)
    asserted_hashes = tuple(asserted_item_text_hashes)
    envelope_hashes = tuple(envelope["item_text_utf8_sha256"])
    if (
        identity != expected_identity or asserted_identity != identity
        or tuple(decoded_hashes) != envelope_hashes or asserted_hashes != envelope_hashes
    ):
        raise AuthorizationFidelityError("manifest identity or item hash assertion mismatch")
    return ParsedAuthorizationManifest(
        identity=identity,
        gate=gate,
        canonical_ascii=canonical,
        payload=deepcopy(rebuilt_payload),
        item_text_hashes=tuple(decoded_hashes),
    )


def _revalidate_parsed_manifest(
    manifest: ParsedAuthorizationManifest,
    *,
    anchor_validator: Callable[[str, str], bool] | None,
) -> ParsedAuthorizationManifest:
    envelope = _canonical_json_bytes({
        "envelope_version": CONTROL_VERSION,
        "manifest_identity": manifest.identity,
        "canonical_manifest_base64": base64.b64encode(manifest.canonical_ascii).decode("ascii"),
        "item_text_utf8_sha256": list(manifest.item_text_hashes),
    })
    return parse_ascii_authorization_manifest(
        envelope,
        asserted_identity=manifest.identity,
        asserted_item_text_hashes=manifest.item_text_hashes,
        anchor_validator=anchor_validator,
    )


class GateB1ManifestCapability:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise AuthorizationFidelityError("Gate B1 manifest capability requires an authorized issuer")

    def __copy__(self):
        raise AuthorizationFidelityError("Gate B1 manifest capability cannot be copied")

    def __deepcopy__(self, _memo):
        raise AuthorizationFidelityError("Gate B1 manifest capability cannot be copied")

    def __reduce__(self):
        raise AuthorizationFidelityError("Gate B1 manifest capability cannot be serialized")


class GateB2ManifestCapability:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise AuthorizationFidelityError("Gate B2 manifest capability requires an authorized issuer")

    def __copy__(self):
        raise AuthorizationFidelityError("Gate B2 manifest capability cannot be copied")

    def __deepcopy__(self, _memo):
        raise AuthorizationFidelityError("Gate B2 manifest capability cannot be copied")

    def __reduce__(self):
        raise AuthorizationFidelityError("Gate B2 manifest capability cannot be serialized")


def _build_capability_registry():
    records: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
    authorized_issuers: set[Any] = set()
    lock = threading.Lock()

    def issue(manifest: ParsedAuthorizationManifest, target: Path) -> Any:
        if sys._getframe(1).f_code not in authorized_issuers:
            raise AuthorizationFidelityError("manifest capability issuer is not authorized")
        cls = GateB1ManifestCapability if manifest.gate == "B1" else GateB2ManifestCapability
        capability = object.__new__(cls)
        with lock:
            records[capability] = {
                "state": "issued",
                "manifest": manifest,
                "target": Path(os.path.abspath(target)),
            }
        return capability

    def consume(capability: Any, *, gate: str, target: Path) -> ParsedAuthorizationManifest:
        expected = GateB1ManifestCapability if gate == "B1" else GateB2ManifestCapability
        if not isinstance(capability, expected):
            raise AuthorizationFidelityError(f"Gate {gate} manifest capability is required")
        with lock:
            record = records.get(capability)
            if record is None:
                raise AuthorizationFidelityError(f"Gate {gate} manifest capability is invalid")
            if record["state"] != "issued":
                raise AuthorizationFidelityError(
                    f"Gate {gate} manifest capability is {record['state']}"
                )
            if record["target"] != Path(os.path.abspath(target)):
                record["state"] = "expired"
                raise AuthorizationFidelityError(f"Gate {gate} target binding mismatch")
            manifest = record["manifest"]
            if manifest.gate != gate:
                record["state"] = "expired"
                raise AuthorizationFidelityError(f"Gate {gate} manifest binding mismatch")
            record["state"] = "consumed"
            return manifest

    def state(capability: Any) -> str:
        with lock:
            record = records.get(capability) if isinstance(
                capability, (GateB1ManifestCapability, GateB2ManifestCapability)
            ) else None
            return record["state"] if record is not None else "invalid"

    def authorize(code: Any) -> None:
        authorized_issuers.add(code)

    return issue, consume, state, authorize


(
    _issue_capability,
    _consume_capability,
    manifest_capability_state,
    _authorize_capability_issuer,
) = _build_capability_registry()


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _ensure_test_target(path: str | Path) -> Path:
    target = _absolute(path)
    if is_production_profile_enablement_sidecar(target):
        raise AuthorizationFidelityError("D2 test core cannot target production")
    try:
        return validate_knowledge_test_db_target(target)
    except KnowledgeSidecarPathError as exc:
        raise AuthorizationFidelityError("D2 test core target is not allowed") from exc


def _connect_writable(target: Path) -> sqlite3.Connection:
    if is_production_profile_enablement_sidecar(target):
        return sqlite3.connect(target, timeout=5.0)
    try:
        return connect_alias_safe_test_database(target, timeout=5.0)
    except KnowledgeSidecarPathError as exc:
        raise AuthorizationFidelityError("D2 test core target is not allowed") from exc


def _ensure_overlay(conn: sqlite3.Connection) -> None:
    for statement in _OVERLAY_STATEMENTS:
        conn.execute(statement)
    row = conn.execute(
        "SELECT schema_version FROM d2_authorization_schema"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO d2_authorization_schema(schema_version,initialized_at) VALUES(?,?)",
            (OVERLAY_SCHEMA_VERSION, _utc_now()),
        )
    elif row[0] != OVERLAY_SCHEMA_VERSION:
        raise AuthorizationFidelityError("D2 overlay schema version mismatch")
    for table in _OVERLAY_TABLES:
        triggers = {
            item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
            )
        }
        expected = {f"{table}_no_update", f"{table}_no_delete"}
        if not triggers:
            conn.execute(_trigger_sql(table, "UPDATE"))
            conn.execute(_trigger_sql(table, "DELETE"))
        elif triggers != expected:
            raise AuthorizationFidelityError(f"{table} append-only triggers are invalid")
    _validate_overlay(conn)


def _validate_overlay(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT schema_version FROM d2_authorization_schema"
    ).fetchone()
    if row is None or row[0] != OVERLAY_SCHEMA_VERSION:
        raise AuthorizationFidelityError("D2 overlay schema is unavailable")
    for table, expected_columns in _OVERLAY_COLUMNS.items():
        columns = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        if columns != expected_columns:
            raise AuthorizationFidelityError(f"{table} schema mismatch")
        triggers = {
            item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
            )
        }
        if triggers != {f"{table}_no_update", f"{table}_no_delete"}:
            raise AuthorizationFidelityError(f"{table} append-only triggers are missing")


def _validate_overlay_records(conn: sqlite3.Connection) -> None:
    """Recompute every D2 identity and cross-check its append-only bindings."""
    _validate_overlay(conn)
    manifests = conn.execute(
        "SELECT * FROM d2_authorization_manifests ORDER BY recorded_at, manifest_identity"
    ).fetchall()
    for row in manifests:
        canonical = bytes(row["canonical_ascii"])
        envelope = _canonical_json_bytes({
            "envelope_version": CONTROL_VERSION,
            "manifest_identity": row["manifest_identity"],
            "canonical_manifest_base64": base64.b64encode(canonical).decode("ascii"),
            "item_text_utf8_sha256": [
                item["text"]["utf8_sha256"] if row["gate"] == "B1"
                else item["edited_text"]["utf8_sha256"]
                if item["edited_text"] is not None else None
                for item in _strict_json_object(canonical)["items"]
            ],
        })
        parsed = parse_ascii_authorization_manifest(
            envelope,
            asserted_identity=row["manifest_identity"],
            asserted_item_text_hashes=_strict_json_object(envelope)[
                "item_text_utf8_sha256"
            ],
            anchor_validator=lambda _conversation_id, _message_id: True,
        )
        if (
            row["manifest_version"] != MANIFEST_VERSION
            or row["gate"] != parsed.gate
            or row["canonical_sha256"] != _sha256_bytes(canonical)
            or row["item_count"] != len(parsed.payload["items"])
        ):
            raise AuthorizationFidelityError("stored manifest metadata mismatch")

        results = conn.execute(
            "SELECT * FROM d2_authorization_results WHERE manifest_identity=? ORDER BY ordinal",
            (parsed.identity,),
        ).fetchall()
        batch = conn.execute(
            "SELECT * FROM d2_authorization_batch_receipts WHERE manifest_identity=?",
            (parsed.identity,),
        ).fetchone()
        if batch is None or len(results) != len(parsed.payload["items"]):
            raise AuthorizationFidelityError("stored manifest result set is incomplete")
        receipt = _load_batch_receipt(batch["receipt_json"])
        if (
            batch["receipt_identity"] != receipt["receipt_identity"]
            or receipt.get("manifest_identity") != parsed.identity
            or receipt.get("gate") != parsed.gate
            or receipt.get("results") != [json.loads(result["result_json"]) for result in results]
        ):
            raise AuthorizationFidelityError("stored batch receipt binding mismatch")
        for ordinal, (item, result) in enumerate(
            zip(parsed.payload["items"], results), start=1
        ):
            result_json = json.loads(result["result_json"])
            expected_kind = "proposal" if parsed.gate == "B1" else "decision"
            expected_subject = (
                result_json.get("proposal_id") if parsed.gate == "B1" else item["subject_id"]
            )
            expected_record = (
                result_json.get("proposal_id") if parsed.gate == "B1"
                else result_json.get("decision_id")
            )
            expected_record_receipt = (
                result_json.get("proposal_receipt_identity") if parsed.gate == "B1"
                else result_json.get("decision_receipt_identity")
            )
            expected_text_hash = (
                item["text"]["utf8_sha256"] if parsed.gate == "B1"
                else item["edited_text"]["utf8_sha256"]
                if item["edited_text"] is not None else None
            )
            if (
                result["ordinal"] != ordinal
                or result["result_kind"] != expected_kind
                or result["subject_id"] != expected_subject
                or result["record_id"] != expected_record
                or result["record_receipt_identity"] != expected_record_receipt
                or result["text_utf8_sha256"] != expected_text_hash
                or result_json.get("ordinal") != ordinal
            ):
                raise AuthorizationFidelityError("stored manifest result binding mismatch")


def _store_manifest(conn: sqlite3.Connection, manifest: ParsedAuthorizationManifest) -> None:
    conn.execute(
        """INSERT INTO d2_authorization_manifests(
            manifest_identity,manifest_version,gate,canonical_ascii,canonical_sha256,item_count,recorded_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (
            manifest.identity, MANIFEST_VERSION, manifest.gate, manifest.canonical_ascii,
            _sha256_bytes(manifest.canonical_ascii), len(manifest.payload["items"]), _utc_now(),
        ),
    )


def _existing_batch_receipt(
    conn: sqlite3.Connection, manifest: ParsedAuthorizationManifest,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT m.canonical_ascii,b.receipt_json
           FROM d2_authorization_manifests m
           LEFT JOIN d2_authorization_batch_receipts b USING(manifest_identity)
           WHERE m.manifest_identity=?""",
        (manifest.identity,),
    ).fetchone()
    if row is None:
        return None
    if bytes(row[0]) != manifest.canonical_ascii or row[1] is None:
        raise AuthorizationFidelityError("stored manifest or batch receipt is incomplete")
    return _load_batch_receipt(row[1])


def _assert_ids_unused(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    for item in items:
        request_id, mutation_id = item["request_id"], item["mutation_id"]
        for table in ("direct_profile_proposals", "profile_enablement_decisions"):
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE request_id=? OR mutation_id=?",
                (request_id, mutation_id),
            ).fetchone()
            if row is not None:
                raise ProfileEnablementIdempotencyConflict(
                    "request_id or mutation_id belongs to a different manifest"
                )


def _batch_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "receipt_version": BATCH_RECEIPT_VERSION,
        "receipt_identity": "",
        **payload,
    }
    receipt["receipt_identity"] = "pabr_" + _sha256_bytes(_canonical_json_bytes({
        key: value for key, value in receipt.items() if key != "receipt_identity"
    }))
    return receipt


def _load_batch_receipt(receipt_json: str) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthorizationFidelityError("stored batch receipt JSON is invalid") from exc
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != BATCH_RECEIPT_VERSION:
        raise AuthorizationFidelityError("stored batch receipt version mismatch")
    expected = "pabr_" + _sha256_bytes(_canonical_json_bytes({
        key: value for key, value in receipt.items() if key != "receipt_identity"
    }))
    if receipt.get("receipt_identity") != expected:
        raise AuthorizationFidelityError("stored batch receipt identity mismatch")
    return receipt


def _execute_b1_manifest(
    manifest: ParsedAuthorizationManifest,
    *,
    target: Path,
    anchor_validator: Callable[[str, str], bool],
    runtime_text_transform: Callable[[str, int], str] | None = None,
    readback_transform: Callable[[str, int], str] | None = None,
) -> dict[str, Any]:
    if manifest.gate != "B1" or profile_enablement_sidecar_status(target)["status"] != "ready":
        raise AuthorizationFidelitySidecarUnavailable("D1 profile sidecar is unavailable")
    reparsed = parse_ascii_authorization_manifest(
        build_ascii_authorization_manifest("B1", [
            {
                "text": _decode_text_record(item["text"], label="proposal text")[0],
                "proposal_type": item["proposal_type"],
                "source_anchors": item["source_anchors"],
                "user_identity": item["user_identity"],
                "expected_revision": item["expected_revision"],
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
            } for item in manifest.payload["items"]
        ], anchor_validator=anchor_validator),
        asserted_identity=manifest.identity,
        asserted_item_text_hashes=manifest.item_text_hashes,
        anchor_validator=anchor_validator,
    )
    if reparsed.canonical_ascii != manifest.canonical_ascii:
        raise AuthorizationFidelityError("capability manifest changed before execution")
    conn = _connect_writable(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_d1_schema(conn)
        transactional_manifest = _revalidate_parsed_manifest(
            manifest, anchor_validator=anchor_validator,
        )
        if transactional_manifest.canonical_ascii != manifest.canonical_ascii:
            raise AuthorizationFidelityError("transaction manifest identity mismatch")
        _ensure_overlay(conn)
        _validate_overlay_records(conn)
        existing = _existing_batch_receipt(conn, manifest)
        if existing is not None:
            conn.rollback()
            return existing
        _assert_ids_unused(conn, manifest.payload["items"])
        decisions = conn.execute(
            "SELECT * FROM profile_enablement_decisions ORDER BY sequence"
        ).fetchall()
        current_revision = len(decisions)
        if any(item["expected_revision"] != current_revision for item in manifest.payload["items"]):
            raise ProfileEnablementRevisionConflict(current_revision)
        _store_manifest(conn, manifest)
        results: list[dict[str, Any]] = []
        for item in manifest.payload["items"]:
            ordinal = item["ordinal"]
            text, raw, text_hash = _decode_text_record(item["text"], label="proposal text")
            runtime_text = runtime_text_transform(text, ordinal) if runtime_text_transform else text
            if (
                runtime_text != text or runtime_text.encode("utf-8", errors="strict") != raw
                or _sha256_bytes(runtime_text.encode("utf-8")) != text_hash
                or [ord(char) for char in runtime_text] != item["text"]["codepoints"]
            ):
                raise AuthorizationFidelityError("runtime proposal text differs from authorized manifest")
            anchors = _canonical_anchors(item["source_anchors"], validator=anchor_validator)
            d1_binding = {
                "original_text": text,
                "proposal_type": item["proposal_type"],
                "source_anchors": anchors,
                "proposed_by": "local_user",
                "expected_revision": current_revision,
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
            }
            d1_authorization = {
                "authorization_version": D1_GATE_B1_AUTHORIZATION_VERSION,
                "gate": "B1",
                "binding": d1_binding,
            }
            d1_authorization_identity = "gb1_" + _d1_digest(d1_authorization)
            fingerprint_payload = {
                "protocol_version": D1_CONTROL_VERSION,
                "proposal_version": D1_PROPOSAL_VERSION,
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
                "origin": "direct_user_proposal",
                "original_text": text,
                "proposal_type": item["proposal_type"],
                "proposed_by": "local_user",
                "source_anchors": anchors,
                "expected_revision": current_revision,
                "authorization_version": D1_GATE_B1_AUTHORIZATION_VERSION,
            }
            fingerprint = _d1_digest(fingerprint_payload)
            created_at = _utc_now()
            proposal_id = "dpp_" + _d1_digest({
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
                    proposal_id, item["request_id"], item["mutation_id"], fingerprint,
                    D1_PROPOSAL_VERSION, "direct_user_proposal", text, item["proposal_type"],
                    "local_user", _d1_dump(anchors), current_revision,
                    D1_GATE_B1_AUTHORIZATION_VERSION, d1_authorization_identity,
                    _d1_dump(d1_authorization), created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM direct_profile_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            observed = readback_transform(row["original_text"], ordinal) if readback_transform else row["original_text"]
            if (
                observed != text or observed.encode("utf-8", errors="strict") != raw
                or _sha256_bytes(observed.encode("utf-8")) != text_hash
                or [ord(char) for char in observed] != item["text"]["codepoints"]
            ):
                raise AuthorizationFidelityError("proposal read-back fidelity check failed")
            proposal_receipt = _proposal_receipt(row)
            conn.execute(
                "INSERT INTO direct_profile_proposal_receipts VALUES(?,?,?)",
                (proposal_id, proposal_receipt["receipt_identity"], _d1_dump(proposal_receipt)),
            )
            result = {
                "ordinal": ordinal,
                "proposal_id": proposal_id,
                "proposal_receipt_identity": proposal_receipt["receipt_identity"],
                "proposal_type": item["proposal_type"],
                "text_utf8_sha256": text_hash,
                "source_anchors": anchors,
                "user_identity": "local_user",
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
                "expected_revision": current_revision,
            }
            results.append(result)
            conn.execute(
                """INSERT INTO d2_authorization_results(
                    manifest_identity,ordinal,result_kind,subject_id,record_id,
                    record_receipt_identity,text_utf8_sha256,result_json,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    manifest.identity, ordinal, "proposal", proposal_id, proposal_id,
                    proposal_receipt["receipt_identity"], text_hash, _d1_dump(result), _utc_now(),
                ),
            )
        proposals = conn.execute(
            "SELECT * FROM direct_profile_proposals ORDER BY sequence"
        ).fetchall()
        proposal_receipts = conn.execute(
            "SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid"
        ).fetchall()
        _validate_proposals(proposals, proposal_receipts)
        receipt = _batch_receipt({
            "status": "proposal_batch_recorded",
            "gate": "B1",
            "manifest_identity": manifest.identity,
            "manifest_version": MANIFEST_VERSION,
            "capability_version": GATE_B1_CAPABILITY_VERSION,
            "control_revision": current_revision,
            "active_profile_changed": False,
            "decision_required": True,
            "results": results,
        })
        conn.execute(
            "INSERT INTO d2_authorization_batch_receipts VALUES(?,?,?)",
            (manifest.identity, receipt["receipt_identity"], _d1_dump(receipt)),
        )
        _validate_overlay_records(conn)
        conn.commit()
        return receipt
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _execute_b2_manifest(
    manifest: ParsedAuthorizationManifest,
    *,
    target: Path,
    temporal_builder: Callable[..., dict[str, Any]],
    runtime_text_transform: Callable[[str, int], str] | None = None,
    readback_transform: Callable[[str, int], str] | None = None,
) -> dict[str, Any]:
    if manifest.gate != "B2" or profile_enablement_sidecar_status(target)["status"] != "ready":
        raise AuthorizationFidelitySidecarUnavailable("D1 profile sidecar is unavailable")
    reparsed = _revalidate_parsed_manifest(manifest, anchor_validator=None)
    if reparsed.canonical_ascii != manifest.canonical_ascii:
        raise AuthorizationFidelityError("capability manifest changed before execution")
    conn = _connect_writable(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_d1_schema(conn)
        transactional_manifest = _revalidate_parsed_manifest(manifest, anchor_validator=None)
        if transactional_manifest.canonical_ascii != manifest.canonical_ascii:
            raise AuthorizationFidelityError("transaction manifest identity mismatch")
        _ensure_overlay(conn)
        _validate_overlay_records(conn)
        existing = _existing_batch_receipt(conn, manifest)
        if existing is not None:
            conn.rollback()
            return existing
        _assert_ids_unused(conn, manifest.payload["items"])
        proposals = conn.execute(
            "SELECT * FROM direct_profile_proposals ORDER BY sequence"
        ).fetchall()
        proposal_receipts = conn.execute(
            "SELECT * FROM direct_profile_proposal_receipts ORDER BY rowid"
        ).fetchall()
        decisions = conn.execute(
            "SELECT * FROM profile_enablement_decisions ORDER BY sequence"
        ).fetchall()
        decision_receipts = conn.execute(
            "SELECT * FROM profile_enablement_receipts ORDER BY rowid"
        ).fetchall()
        _validate_proposals(proposals, proposal_receipts)
        _validate_decision_receipts(decisions, decision_receipts)
        current_revision = len(decisions)
        for offset, item in enumerate(manifest.payload["items"]):
            if item["expected_revision"] != current_revision + offset:
                raise ProfileEnablementRevisionConflict(current_revision + offset)
        resolved_candidates: dict[int, dict[str, Any]] = {}
        for item in manifest.payload["items"]:
            if item["subject_origin"] != "v7.2_candidate":
                continue
            package = temporal_builder(
                RetrievalQuery(item["query_text"]), budget_profile=item["budget_profile"],
            )
            validate_temporal_candidates_standalone(package)
            if package["package_identity"] != item["package_identity"]:
                raise AuthorizationFidelityError("source-backed candidate package is stale")
            candidate = next(
                (
                    candidate for candidate in package["candidates"]
                    if candidate["candidate_id"] == item["subject_id"]
                ),
                None,
            )
            if candidate is None:
                raise AuthorizationFidelityError("candidate is absent from the live source-backed package")
            resolved_candidates[item["ordinal"]] = {
                "subject": deepcopy(candidate),
                "evidence": _candidate_evidence(package, candidate),
                "source_package_identity": package["package_identity"],
                "source_selection_identity": package["source_projection"]["package_identity"],
                "proposal_identity": None,
                "promoted_kind": _PROMOTED_KIND[candidate["candidate_type"]],
            }
        _store_manifest(conn, manifest)
        results: list[dict[str, Any]] = []
        for item in manifest.payload["items"]:
            ordinal = item["ordinal"]
            if item["subject_origin"] == "direct_user_proposal":
                proposal_row = conn.execute(
                    "SELECT * FROM direct_profile_proposals WHERE proposal_id=?",
                    (item["subject_id"],),
                ).fetchone()
                if proposal_row is None:
                    raise AuthorizationFidelityError("authorized proposal is not present")
                subject = _proposal_snapshot(proposal_row)
                resolved = {
                    "subject": subject,
                    "evidence": {"source_anchors": subject["source_anchors"]},
                    "source_package_identity": None,
                    "source_selection_identity": None,
                    "proposal_identity": subject["proposal_id"],
                    "promoted_kind": subject["proposal_type"],
                }
                original = subject["original_text"]
            else:
                resolved = resolved_candidates[ordinal]
                subject = resolved["subject"]
                original = subject["statement"]
            action = item["action"]
            if item["edited_text"] is None:
                edited, edited_raw, text_hash = "", b"", None
            else:
                edited, edited_raw, text_hash = _decode_text_record(
                    item["edited_text"], label="final edited text"
                )
                runtime_text = runtime_text_transform(edited, ordinal) if runtime_text_transform else edited
                if runtime_text != edited or runtime_text.encode("utf-8") != edited_raw:
                    raise AuthorizationFidelityError("runtime edited text differs from authorized manifest")
            expected_revision = item["expected_revision"]
            final_text = edited if action == "edit_and_approve" else original if action == "approve" else None
            profile_item = None
            resulting_revision = expected_revision + 1
            decided_at = _utc_now()
            reason = item["reason"]
            d1_binding = {
                "subject_origin": item["subject_origin"],
                "subject_id": item["subject_id"],
                "action": action,
                "expected_revision": expected_revision,
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
                "edited_statement": edited,
                "final_text_sha256": _sha256_bytes(edited.encode("utf-8"))
                if action == "edit_and_approve" else None,
                "reason": reason,
                "query_text": item["query_text"],
                "budget_profile": item["budget_profile"],
                "package_identity": item["package_identity"],
            }
            d1_authorization = {
                "authorization_version": D1_GATE_B2_AUTHORIZATION_VERSION,
                "gate": "B2",
                "binding": d1_binding,
            }
            d1_authorization_identity = "gb2_" + _d1_digest(d1_authorization)
            evidence = resolved["evidence"]
            fingerprint_payload = {
                "protocol_version": D1_CONTROL_VERSION,
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
                "subject_origin": item["subject_origin"],
                "subject": subject,
                "evidence": evidence,
                "source_package_identity": resolved["source_package_identity"],
                "source_selection_identity": resolved["source_selection_identity"],
                "proposal_identity": resolved["proposal_identity"],
                "decision_source": "manual",
                "authorization_version": D1_GATE_B2_AUTHORIZATION_VERSION,
                "action": action,
                "expected_revision": expected_revision,
                "edited_statement": edited,
                "reason": reason,
            }
            fingerprint = _d1_digest(fingerprint_payload)
            decision_id = "apd_" + _d1_digest({
                "request_fingerprint": fingerprint,
                "resulting_revision": resulting_revision,
                "decided_at": decided_at,
            })
            if final_text is not None:
                if resolved["promoted_kind"] is None:
                    raise AuthorizationFidelityError("authorized subject cannot become an approved item")
                profile_item = make_approved_profile_item(
                    final_text, kind=resolved["promoted_kind"],
                    topics=[
                        item["subject_origin"],
                        subject.get("proposal_type") or subject.get("candidate_type"),
                    ],
                    approval_id=decision_id, approved_at=decided_at,
                )
                if profile_item["statement"] != final_text:
                    raise AuthorizationFidelityError("approved text violates the privacy boundary")
            conn.execute(
                """INSERT INTO profile_enablement_decisions(
                    decision_id,request_id,mutation_id,request_fingerprint,subject_origin,subject_id,
                    source_package_identity,source_selection_identity,proposal_identity,decision_source,
                    authorization_version,authorization_identity,authorization_snapshot_json,
                    action,expected_revision,resulting_revision,subject_snapshot_json,evidence_snapshot_json,
                    profile_item_json,original_text,edited_statement,final_text,reason,decided_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, item["request_id"], item["mutation_id"], fingerprint,
                    item["subject_origin"], item["subject_id"],
                    resolved["source_package_identity"], resolved["source_selection_identity"],
                    resolved["proposal_identity"],
                    "manual", D1_GATE_B2_AUTHORIZATION_VERSION, d1_authorization_identity,
                    _d1_dump(d1_authorization), action, expected_revision, resulting_revision,
                    _d1_dump(subject), _d1_dump(evidence),
                    _d1_dump(profile_item) if profile_item is not None else None,
                    original, edited or None, final_text, reason, decided_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM profile_enablement_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if action == "edit_and_approve":
                observed = readback_transform(row["final_text"], ordinal) if readback_transform else row["final_text"]
                if (
                    observed != edited or observed.encode("utf-8") != edited_raw
                    or _sha256_bytes(observed.encode("utf-8")) != text_hash
                    or [ord(char) for char in observed] != item["edited_text"]["codepoints"]
                ):
                    raise AuthorizationFidelityError("decision read-back fidelity check failed")
            decisions.append(row)
            d1_receipt = _decision_receipt(row, _projection_from_rows(decisions))
            conn.execute(
                "INSERT INTO profile_enablement_receipts VALUES(?,?,?)",
                (decision_id, d1_receipt["receipt_identity"], _d1_dump(d1_receipt)),
            )
            result = {
                "ordinal": ordinal,
                "subject_id": item["subject_id"],
                "decision_id": decision_id,
                "decision_receipt_identity": d1_receipt["receipt_identity"],
                "action": action,
                "expected_revision": expected_revision,
                "resulting_revision": resulting_revision,
                "decision_source": "manual",
                "final_text_utf8_sha256": text_hash,
                "request_id": item["request_id"],
                "mutation_id": item["mutation_id"],
            }
            results.append(result)
            conn.execute(
                """INSERT INTO d2_authorization_results(
                    manifest_identity,ordinal,result_kind,subject_id,record_id,
                    record_receipt_identity,text_utf8_sha256,result_json,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    manifest.identity, ordinal, "decision", item["subject_id"], decision_id,
                    d1_receipt["receipt_identity"], text_hash, _d1_dump(result), _utc_now(),
                ),
            )
        receipts = conn.execute(
            "SELECT * FROM profile_enablement_receipts ORDER BY rowid"
        ).fetchall()
        _validate_decision_receipts(decisions, receipts)
        receipt = _batch_receipt({
            "status": "decision_batch_recorded",
            "gate": "B2",
            "manifest_identity": manifest.identity,
            "manifest_version": MANIFEST_VERSION,
            "capability_version": GATE_B2_CAPABILITY_VERSION,
            "starting_revision": current_revision,
            "resulting_revision": current_revision + len(results),
            "decision_source": "manual",
            "results": results,
        })
        conn.execute(
            "INSERT INTO d2_authorization_batch_receipts VALUES(?,?,?)",
            (manifest.identity, receipt["receipt_identity"], _d1_dump(receipt)),
        )
        _validate_overlay_records(conn)
        conn.commit()
        return receipt
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


class _AuthorizationFidelityTestCore:
    """Test-only D2 core; production paths are rejected at construction."""

    def __init__(
        self,
        sidecar_path: str | Path,
        *,
        anchor_validator: Callable[[str, str], bool] = lambda _c, _m: True,
        temporal_builder: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.sidecar_path = _ensure_test_target(sidecar_path)
        self.anchor_validator = anchor_validator
        self.temporal_builder = temporal_builder or self._candidate_builder_unavailable

    @staticmethod
    def _candidate_builder_unavailable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AuthorizationFidelityError("test candidate builder is unavailable")

    def authorize(
        self, envelope_ascii: str | bytes, *, manifest_identity: str,
        item_text_utf8_sha256: Iterable[str | None],
    ) -> Any:
        manifest = parse_ascii_authorization_manifest(
            envelope_ascii,
            asserted_identity=manifest_identity,
            asserted_item_text_hashes=item_text_utf8_sha256,
            anchor_validator=self.anchor_validator,
        )
        return _issue_capability(manifest, self.sidecar_path)

    def execute_b1(
        self, authority: GateB1ManifestCapability, *,
        runtime_text_transform: Callable[[str, int], str] | None = None,
        readback_transform: Callable[[str, int], str] | None = None,
    ) -> dict[str, Any]:
        manifest = _consume_capability(authority, gate="B1", target=self.sidecar_path)
        return _execute_b1_manifest(
            manifest, target=self.sidecar_path, anchor_validator=self.anchor_validator,
            runtime_text_transform=runtime_text_transform, readback_transform=readback_transform,
        )

    def execute_b2(
        self, authority: GateB2ManifestCapability, *,
        runtime_text_transform: Callable[[str, int], str] | None = None,
        readback_transform: Callable[[str, int], str] | None = None,
    ) -> dict[str, Any]:
        manifest = _consume_capability(authority, gate="B2", target=self.sidecar_path)
        return _execute_b2_manifest(
            manifest, target=self.sidecar_path, temporal_builder=self.temporal_builder,
            runtime_text_transform=runtime_text_transform, readback_transform=readback_transform,
        )


class ProfileAuthorizationFidelityService:
    """Fixed production D2 executor; accepts capabilities only, never raw text or paths."""

    __slots__ = ()

    def execute_b1(self, *, authority: GateB1ManifestCapability) -> dict[str, Any]:
        target = _absolute(PRODUCTION_PROFILE_SIDECAR)
        manifest = _consume_capability(authority, gate="B1", target=target)
        return _execute_b1_manifest(
            manifest, target=target, anchor_validator=_production_anchor_validator,
        )

    def execute_b2(self, *, authority: GateB2ManifestCapability) -> dict[str, Any]:
        target = _absolute(PRODUCTION_PROFILE_SIDECAR)
        manifest = _consume_capability(authority, gate="B2", target=target)
        return _execute_b2_manifest(
            manifest, target=target, temporal_builder=build_production_temporal_candidates,
        )


def open_production_manifest_capability(
    *,
    profile_admin: ProfileAdminSession,
    envelope_ascii: str | bytes,
    manifest_identity: str,
    item_text_utf8_sha256: Iterable[str | None],
) -> GateB1ManifestCapability | GateB2ManifestCapability:
    if not is_trusted_profile_admin_session(profile_admin):
        raise AuthorizationFidelityError("trusted profile-admin session is required")
    manifest = parse_ascii_authorization_manifest(
        envelope_ascii,
        asserted_identity=manifest_identity,
        asserted_item_text_hashes=item_text_utf8_sha256,
        anchor_validator=_production_anchor_validator,
    )
    return _issue_capability(manifest, _absolute(PRODUCTION_PROFILE_SIDECAR))


for _issuer in (
    _AuthorizationFidelityTestCore.authorize,
    open_production_manifest_capability,
):
    _authorize_capability_issuer(_issuer.__code__)
del _issuer
del _authorize_capability_issuer


def authorization_fidelity_privacy_manifest() -> dict[str, Any]:
    return {
        "control_version": CONTROL_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "overlay_schema_version": OVERLAY_SCHEMA_VERSION,
        "canonical_encoding": "ASCII JSON envelope containing base64 canonical ASCII JSON",
        "authorized_text_encoding": "strict UTF-8 bytes with SHA-256 and exact code points",
        "production_target": "fixed_profile_sidecar",
        "runtime_sidecar_writes": False,
        "memory_sidecar_writes": False,
        "knowledge_sidecar_writes": False,
        "review_sidecar_writes": False,
        "credential_read": False,
        "external_model_calls": False,
    }
