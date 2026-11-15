from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from memory.cognitive_objective import ObjectiveContract, ObjectiveIntentError
from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    COGNITIVE_SCHEMA_MARKER,
    verify_cognitive_state_schema,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    StateEvent,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    utc_now,
    validate_identity,
    validate_sha256,
)
from memory.operational_state_store import OperationalStateStore


def _decode_canonical_object(raw: str, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise OperationalStateError(reason_code) from None
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise OperationalStateError(reason_code)
    return value


_MAX_OBJECTIVE_CONTRACT_BYTES = 262_144
_OBJECTIVE_CANDIDATE_STATUSES = {"pending", "approved", "rejected", "expired"}


class CognitiveStateStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        trusted_clock: Callable[[], str] | None = None,
        allow_production_write: bool = False,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.operational_store = OperationalStateStore(
            self.path,
            allow_production_write=allow_production_write,
        )
        self._trusted_clock = trusted_clock or utc_now
        verify_cognitive_state_schema(self.path)

    def _trusted_at(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(contract, Mapping):
            raise OperationalStateError("objective_contract_must_be_object")
        try:
            return ObjectiveContract.from_dict(dict(contract)).to_dict()
        except ObjectiveIntentError as exc:
            raise OperationalStateError(exc.reason_code) from None

    @staticmethod
    def _validate_event_binding(
        contract: Mapping[str, Any], event: StateEvent
    ) -> None:
        matching_anchors = [
            dict(anchor)
            for anchor in contract["source_anchors"]
            if anchor.get("anchor_id") == event.source_anchor.anchor_id
        ]
        if (
            len(matching_anchors) != 1
            or canonical_json(matching_anchors[0])
            != canonical_json(event.source_anchor.to_dict())
        ):
            raise OperationalStateError("objective_source_anchor_mismatch")
        payload = event.canonical_payload
        if (
            event.project_id != contract["project_id"]
            or event.entity_type != "decision"
            or event.event_type != "decision_approved"
            or event.authority_kind != "user"
            or event.actor_kind != "user"
            or not event.actor_id.startswith("user:")
            or payload.get("state") != "approved"
            or payload.get("objective_id") != contract["objective_id"]
            or payload.get("objective_version") != contract["version"]
            or payload.get("objective_digest") != contract["canonical_digest"]
        ):
            raise OperationalStateError("objective_event_binding_mismatch")

    @staticmethod
    def _validate_approval(
        connection: sqlite3.Connection,
        approval_id: str | None,
        contract: Mapping[str, Any] | None = None,
        *,
        authority: AuthorityArtifact | None = None,
        expected_authority_digest: str | None = None,
        require_current_approval: bool = False,
    ) -> dict[str, Any] | None:
        if approval_id is None:
            return None
        validate_identity(approval_id, "invalid_approval_id")
        row = connection.execute(
            "SELECT a.*,c.status AS candidate_status,"
            "c.project_id AS candidate_project_id,"
            "c.candidate_type,c.canonical_candidate,c.candidate_digest,"
            "c.source_anchor_id AS candidate_source_anchor_id "
            "FROM state_approvals a "
            "LEFT JOIN state_candidates c ON c.candidate_id=a.candidate_id "
            "WHERE a.approval_id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise OperationalStateError("objective_approval_not_found")
        if row["decision"] != "approve" or row["candidate_id"] is None:
            raise OperationalStateError("objective_approval_not_approved")
        if require_current_approval and row["candidate_status"] != "approved":
            raise OperationalStateError("objective_approval_not_current")
        payload = _decode_canonical_object(
            row["canonical_approval"], "objective_approval_record_corrupt"
        )
        if (
            canonical_digest(payload) != row["approval_digest"]
            or set(payload)
            != {
                "candidate_id",
                "candidate_digest",
                "decision",
                "authority_id",
                "authority_digest",
                "recorded_at",
            }
            or payload["candidate_id"] != row["candidate_id"]
            or payload["candidate_digest"] != row["candidate_digest"]
            or payload["decision"] != "approve"
            or payload["authority_id"] != row["authority_id"]
            or payload["authority_digest"] != row["authority_digest"]
            or payload["recorded_at"] != row["recorded_at"]
        ):
            raise OperationalStateError("objective_approval_record_tampered")
        if authority is not None:
            if not authority.issued_by.startswith("user:"):
                raise OperationalStateError(
                    "objective_approval_requires_user_authority"
                )
            if (
                row["authority_id"] != authority.authority_id
                or row["authority_digest"] != authority.authority_digest
            ):
                raise OperationalStateError(
                    "objective_approval_authority_mismatch"
                )
        elif (
            expected_authority_digest is None
            or row["authority_digest"] != expected_authority_digest
        ):
            raise OperationalStateError(
                "objective_approval_authority_mismatch"
            )
        candidate = _decode_canonical_object(
            row["canonical_candidate"],
            "objective_candidate_record_corrupt",
        )
        if (
            row["candidate_type"] != "objective_contract"
            or canonical_digest(candidate) != row["candidate_digest"]
        ):
            raise OperationalStateError("objective_candidate_record_tampered")
        if contract is not None:
            source_anchor_ids = {
                anchor.get("anchor_id")
                for anchor in contract["source_anchors"]
                if isinstance(anchor, Mapping)
            }
            if (
                canonical_json(candidate) != canonical_json(contract)
                or candidate.get("canonical_digest")
                != contract["canonical_digest"]
                or row["candidate_project_id"] != contract["project_id"]
                or row["candidate_source_anchor_id"] not in source_anchor_ids
            ):
                raise OperationalStateError(
                    "objective_approval_contract_mismatch"
                )
        return {
            "approval_id": row["approval_id"],
            "candidate_id": row["candidate_id"],
            "approval_digest": row["approval_digest"],
            "authority_id": row["authority_id"],
            "authority_digest": row["authority_digest"],
            "current_candidate_status": row["candidate_status"],
        }

    def _record_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        contract = _decode_canonical_object(
            row["canonical_contract"], "objective_contract_record_corrupt"
        )
        validated = self._validate_contract(contract)
        if (
            row["objective_id"] != validated["objective_id"]
            or row["version"] != validated["version"]
            or row["project_id"] != validated["project_id"]
            or row["agency_level"] != validated["agency_level"]
            or row["cognitive_stance"] != validated["cognitive_stance"]
            or bool(row["clarification_required"])
            != validated["clarification_required"]
            or row["canonical_digest"] != validated["canonical_digest"]
        ):
            raise OperationalStateError("objective_contract_record_tampered")
        event = self.operational_store._load_event(
            connection, row["created_event_id"]
        )
        if event.source_anchor.anchor_id != row["source_anchor_id"]:
            raise OperationalStateError("objective_source_anchor_record_mismatch")
        self._validate_event_binding(validated, event)
        event_authority = connection.execute(
            "SELECT authority_digest FROM state_events WHERE event_id=?",
            (row["created_event_id"],),
        ).fetchone()
        if event_authority is None:
            raise OperationalStateError("objective_event_authority_missing")
        approval = self._validate_approval(
            connection,
            row["approval_id"],
            validated,
            expected_authority_digest=event_authority["authority_digest"],
        )
        return {
            "objective_id": row["objective_id"],
            "version": row["version"],
            "project_id": row["project_id"],
            "agency_level": row["agency_level"],
            "cognitive_stance": row["cognitive_stance"],
            "clarification_required": bool(row["clarification_required"]),
            "contract": validated,
            "canonical_digest": row["canonical_digest"],
            "source_anchor_id": row["source_anchor_id"],
            "created_event_id": row["created_event_id"],
            "approval_id": approval["approval_id"] if approval else None,
            "approval_current_status": (
                approval["current_candidate_status"] if approval else None
            ),
            "created_at": row["created_at"],
        }

    def read_status(self) -> dict[str, Any]:
        cognitive = verify_cognitive_state_schema(self.path)
        operational = self.operational_store.read_status()
        with closing(self.operational_store._connect()) as connection:
            objective_count = connection.execute(
                "SELECT COUNT(*) FROM objective_contracts"
            ).fetchone()[0]
        return {
            **cognitive,
            "status": "ready",
            "objective_count": objective_count,
            "operational_counts": operational["counts"],
        }

    def get_objective(
        self, objective_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        validate_identity(objective_id, "invalid_objective_id")
        if version is not None and (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise OperationalStateError("invalid_objective_version")
        with closing(self.operational_store._connect()) as connection:
            if version is None:
                row = connection.execute(
                    "SELECT * FROM objective_contracts "
                    "WHERE objective_id=? ORDER BY version DESC LIMIT 1",
                    (objective_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM objective_contracts "
                    "WHERE objective_id=? AND version=?",
                    (objective_id, version),
                ).fetchone()
            return self._record_from_row(connection, row) if row else None

    def list_objectives(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        args: tuple[Any, ...] = ()
        query = "SELECT * FROM objective_contracts"
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
            query += " WHERE project_id=?"
            args = (project_id,)
        query += " ORDER BY objective_id,version"
        if limit is not None:
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 1000
            ):
                raise OperationalStateError("invalid_objective_limit")
            query += " LIMIT ?"
            args = (*args, limit)
        with closing(self.operational_store._connect()) as connection:
            return [
                self._record_from_row(connection, row)
                for row in connection.execute(query, args).fetchall()
            ]

    def _objective_candidate_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        raw = row["canonical_candidate"]
        if (
            not isinstance(raw, str)
            or len(raw.encode("utf-8")) > _MAX_OBJECTIVE_CONTRACT_BYTES
        ):
            raise OperationalStateError(
                "objective_candidate_byte_budget_exceeded"
            )
        candidate = _decode_canonical_object(
            raw,
            "objective_candidate_record_corrupt",
        )
        if canonical_digest(candidate) != row["candidate_digest"]:
            raise OperationalStateError("objective_candidate_record_tampered")
        validated = self._validate_contract(candidate)
        expected_entity_id = make_identity(
            "decision",
            {
                "objective_id": validated["objective_id"],
                "version": validated["version"],
            },
        )
        if (
            row["candidate_type"] != "objective_contract"
            or row["project_id"] != validated["project_id"]
            or row["entity_id"] != expected_entity_id
            or row["entity_type"] != "decision"
            or row["status"] not in _OBJECTIVE_CANDIDATE_STATUSES
            or canonical_json(validated) != raw
        ):
            raise OperationalStateError("objective_candidate_record_invalid")
        anchor_row = connection.execute(
            "SELECT canonical_json,anchor_digest FROM state_source_anchors "
            "WHERE anchor_id=?",
            (row["source_anchor_id"],),
        ).fetchone()
        if anchor_row is None:
            raise OperationalStateError("objective_candidate_anchor_missing")
        anchor_payload = _decode_canonical_object(
            anchor_row["canonical_json"],
            "objective_candidate_anchor_corrupt",
        )
        if canonical_digest(anchor_payload) != anchor_row["anchor_digest"]:
            raise OperationalStateError(
                "objective_candidate_anchor_tampered"
            )
        try:
            source_anchor = SourceAnchor.from_dict(anchor_payload)
        except OperationalStateError:
            raise OperationalStateError(
                "objective_candidate_anchor_invalid"
            ) from None
        matching_anchors = [
            dict(anchor)
            for anchor in validated["source_anchors"]
            if anchor.get("anchor_id") == source_anchor.anchor_id
        ]
        if (
            len(matching_anchors) != 1
            or canonical_json(matching_anchors[0])
            != canonical_json(source_anchor.to_dict())
        ):
            raise OperationalStateError(
                "objective_candidate_anchor_mismatch"
            )
        expected_candidate_id = make_identity(
            "candidate",
            {
                "candidate_type": "objective_contract",
                "objective_id": validated["objective_id"],
                "version": validated["version"],
                "canonical_digest": validated["canonical_digest"],
                "confidence_ppm": row["confidence_ppm"],
                "source_anchor_digest": canonical_digest(
                    source_anchor.to_dict()
                ),
                "expires_at": row["expires_at"],
            },
        )
        if row["candidate_id"] != expected_candidate_id:
            raise OperationalStateError("objective_candidate_record_invalid")
        return {
            "candidate_id": row["candidate_id"],
            "project_id": row["project_id"],
            "entity_id": row["entity_id"],
            "entity_type": row["entity_type"],
            "candidate_type": row["candidate_type"],
            "candidate": validated,
            "candidate_digest": row["candidate_digest"],
            "confidence_ppm": row["confidence_ppm"],
            "source_anchor_id": row["source_anchor_id"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def list_objective_candidates(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["candidate_type=?"]
        args: list[Any] = ["objective_contract"]
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
            clauses.append("project_id=?")
            args.append(project_id)
        if status is not None:
            if status not in _OBJECTIVE_CANDIDATE_STATUSES:
                raise OperationalStateError("invalid_candidate_status")
            clauses.append("status=?")
            args.append(status)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise OperationalStateError("invalid_objective_candidate_limit")
        metadata_query = (
            "SELECT candidate_id,"
            "length(CAST(canonical_candidate AS BLOB)) AS candidate_bytes "
            "FROM state_candidates WHERE "
            + " AND ".join(clauses)
            + " ORDER BY candidate_id LIMIT ?"
        )
        args.append(limit)
        with closing(self.operational_store._connect()) as connection:
            metadata = connection.execute(metadata_query, args).fetchall()
            items: list[dict[str, Any]] = []
            for item in metadata:
                if (
                    item["candidate_bytes"] is None
                    or item["candidate_bytes"] > _MAX_OBJECTIVE_CONTRACT_BYTES
                ):
                    raise OperationalStateError(
                        "objective_candidate_byte_budget_exceeded"
                    )
                row = connection.execute(
                    "SELECT * FROM state_candidates WHERE candidate_id=?",
                    (item["candidate_id"],),
                ).fetchone()
                if row is None:
                    raise OperationalStateError(
                        "objective_candidate_record_missing"
                    )
                items.append(self._objective_candidate_from_row(connection, row))
            return items

    def _validate_objective_candidate_on_connection(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
    ) -> None:
        metadata = connection.execute(
            "SELECT length(CAST(canonical_candidate AS BLOB)) "
            "AS candidate_bytes FROM state_candidates "
            "WHERE candidate_id=? AND candidate_type='objective_contract'",
            (candidate_id,),
        ).fetchone()
        if metadata is None:
            raise OperationalStateError("objective_candidate_not_found")
        if (
            metadata["candidate_bytes"] is None
            or metadata["candidate_bytes"] > _MAX_OBJECTIVE_CONTRACT_BYTES
        ):
            raise OperationalStateError(
                "objective_candidate_byte_budget_exceeded"
            )
        row = connection.execute(
            "SELECT * FROM state_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise OperationalStateError("objective_candidate_record_missing")
        self._objective_candidate_from_row(connection, row)

    def _existing_idempotent_result(
        self,
        contract: Mapping[str, Any],
        event: StateEvent,
        approval_id: str | None,
        *,
        authority_digest: str,
    ) -> dict[str, Any] | None:
        with closing(self.operational_store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM objective_contracts "
                "WHERE objective_id=? AND version=?",
                (contract["objective_id"], contract["version"]),
            ).fetchone()
            if row is None:
                return None
            record = self._record_from_row(connection, row)
            if (
                record["canonical_digest"] != contract["canonical_digest"]
                or record["created_event_id"] != event.event_id
                or record["approval_id"] != approval_id
            ):
                raise OperationalStateError("objective_version_collision")
            receipt_row = connection.execute(
                "SELECT * FROM state_receipts WHERE idempotency_key=?",
                (event.idempotency_key,),
            ).fetchone()
            if (
                receipt_row is None
                or receipt_row["event_id"] != event.event_id
                or receipt_row["authority_digest"] != authority_digest
            ):
                raise OperationalStateError("objective_receipt_missing")
            receipt = self.operational_store._receipt_from_row(
                receipt_row, idempotent_replay=True
            )
        return {
            "objective_id": contract["objective_id"],
            "version": contract["version"],
            "canonical_digest": contract["canonical_digest"],
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            "approval_id": approval_id,
            "idempotent_replay": True,
        }

    def commit_objective(
        self,
        *,
        contract: Mapping[str, Any],
        authority: AuthorityArtifact,
        event: StateEvent,
        lease_owner_id: str,
        lease_token: str,
        created_at: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        validated = self._validate_contract(contract)
        trusted_at = self._trusted_at()
        normalize_timestamp(created_at)
        if validated["agency_level"] == "consult":
            raise OperationalStateError("consult_objective_persistence_denied")
        if validated["clarification_required"]:
            raise OperationalStateError("objective_clarification_required")
        if not authority.issued_by.startswith("user:"):
            raise OperationalStateError(
                "objective_commit_requires_user_authority"
            )
        if event.actor_id != authority.issued_by:
            raise OperationalStateError(
                "objective_event_actor_authority_mismatch"
            )
        self._validate_event_binding(validated, event)
        event.validate_event_id(authority.authority_digest)
        authority.authorize_event(
            project_id=event.project_id,
            entity_id=event.entity_id,
            event_type=event.event_type,
            at=trusted_at,
        )
        existing = self._existing_idempotent_result(
            validated,
            event,
            approval_id,
            authority_digest=authority.authority_digest,
        )
        if existing is not None:
            return existing
        event = replace(event, recorded_at=trusted_at)
        preview = self.operational_store.preview_event(
            authority=authority, event=event
        )
        with closing(self.operational_store._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational_store._assert_lease(
                    connection,
                    owner_id=lease_owner_id,
                    token=lease_token,
                    at=trusted_at,
                )
                receipt = self.operational_store._commit_preview_on_connection(
                    connection,
                    authority=authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                approval = self._validate_approval(
                    connection,
                    approval_id,
                    validated,
                    authority=authority,
                    require_current_approval=True,
                )
                row = connection.execute(
                    "SELECT * FROM objective_contracts "
                    "WHERE objective_id=? AND version=?",
                    (validated["objective_id"], validated["version"]),
                ).fetchone()
                if row is not None:
                    record = self._record_from_row(connection, row)
                    if (
                        record["canonical_digest"]
                        != validated["canonical_digest"]
                        or record["created_event_id"] != event.event_id
                        or record["approval_id"] != approval_id
                        or not receipt.idempotent_replay
                    ):
                        raise OperationalStateError(
                            "objective_version_collision"
                        )
                    connection.rollback()
                    return {
                        "objective_id": validated["objective_id"],
                        "version": validated["version"],
                        "canonical_digest": validated["canonical_digest"],
                        "event_id": event.event_id,
                        "receipt_digest": receipt.receipt_digest,
                        "approval_id": approval_id,
                        "idempotent_replay": True,
                    }
                if validated["version"] > 1:
                    predecessor = connection.execute(
                        "SELECT 1 FROM objective_contracts "
                        "WHERE objective_id=? AND version=?",
                        (
                            validated["objective_id"],
                            validated["version"] - 1,
                        ),
                    ).fetchone()
                    if predecessor is None:
                        raise OperationalStateError(
                            "objective_version_predecessor_missing"
                        )
                connection.execute(
                    "INSERT INTO objective_contracts("
                    "objective_id,version,project_id,agency_level,"
                    "cognitive_stance,clarification_required,"
                    "canonical_contract,canonical_digest,source_anchor_id,"
                    "created_event_id,approval_id,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        validated["objective_id"],
                        validated["version"],
                        validated["project_id"],
                        validated["agency_level"],
                        validated["cognitive_stance"],
                        int(validated["clarification_required"]),
                        canonical_json(validated),
                        validated["canonical_digest"],
                        event.source_anchor.anchor_id,
                        event.event_id,
                        approval["approval_id"] if approval else None,
                        trusted_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise OperationalStateError(
                    "objective_contract_insert_conflict"
                ) from None
            except Exception:
                connection.rollback()
                raise
        return {
            "objective_id": validated["objective_id"],
            "version": validated["version"],
            "canonical_digest": validated["canonical_digest"],
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            "approval_id": approval_id,
            "idempotent_replay": False,
        }

    def create_objective_candidate(
        self,
        *,
        contract: Mapping[str, Any],
        source_anchor: SourceAnchor,
        confidence_ppm: int,
        created_at: str,
        expires_at: str | None,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        validated = self._validate_contract(contract)
        normalize_timestamp(created_at)
        trusted_at = self._trusted_at()
        expiry = normalize_timestamp(expires_at) if expires_at else None
        if validated["agency_level"] == "consult":
            raise OperationalStateError(
                "consult_objective_candidate_persistence_denied"
            )
        if expiry is not None and expiry <= trusted_at:
            raise OperationalStateError("candidate_expired")
        if authority is None:
            raise OperationalStateError(
                "objective_candidate_authority_required"
            )
        if not authority.issued_by.startswith("user:"):
            raise OperationalStateError(
                "objective_candidate_create_requires_user_authority"
            )
        if lease_owner_id is None or lease_token is None:
            raise OperationalStateError(
                "objective_candidate_writer_lease_required"
            )
        anchors = [
            dict(anchor)
            for anchor in validated["source_anchors"]
            if anchor.get("anchor_id") == source_anchor.anchor_id
        ]
        if (
            len(anchors) != 1
            or canonical_json(anchors[0])
            != canonical_json(source_anchor.to_dict())
        ):
            raise OperationalStateError("objective_source_anchor_mismatch")
        if (
            not isinstance(confidence_ppm, int)
            or isinstance(confidence_ppm, bool)
            or not 0 <= confidence_ppm <= 1_000_000
        ):
            raise OperationalStateError("invalid_candidate_confidence")
        candidate_id = make_identity(
            "candidate",
            {
                "candidate_type": "objective_contract",
                "objective_id": validated["objective_id"],
                "version": validated["version"],
                "canonical_digest": validated["canonical_digest"],
                "confidence_ppm": confidence_ppm,
                "source_anchor_digest": canonical_digest(
                    source_anchor.to_dict()
                ),
                "expires_at": expiry,
            },
        )
        return self.operational_store.create_candidate(
            candidate_id=candidate_id,
            project_id=validated["project_id"],
            entity_id=make_identity(
                "decision",
                {
                    "objective_id": validated["objective_id"],
                    "version": validated["version"],
                },
            ),
            entity_type="decision",
            candidate_type="objective_contract",
            candidate=validated,
            confidence_ppm=confidence_ppm,
            source_anchor=source_anchor,
            created_at=trusted_at,
            expires_at=expiry,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            enforce_authority=True,
            enforce_not_expired_replay=True,
            strict_replay_binding=True,
        )

    def decide_objective_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        authority: AuthorityArtifact,
        recorded_at: str,
        expected_candidate_digest: str | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(candidate_id, "invalid_candidate_id")
        normalize_timestamp(recorded_at)
        trusted_at = self._trusted_at()
        if authority.issued_by.startswith("agent:"):
            raise OperationalStateError("agent_cannot_approve_candidate")
        if not authority.issued_by.startswith("user:"):
            raise OperationalStateError(
                "objective_candidate_decision_requires_user"
            )
        if expected_candidate_digest is None:
            raise OperationalStateError(
                "objective_candidate_digest_required"
            )
        validate_sha256(
            expected_candidate_digest,
            "invalid_objective_candidate_digest",
        )
        try:
            return self.operational_store.decide_candidate(
                candidate_id=candidate_id,
                decision=decision,
                authority=authority,
                recorded_at=trusted_at,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                enforce_authority=True,
                enforce_not_expired=True,
                expected_candidate_digest=expected_candidate_digest,
                record_validator=self._validate_objective_candidate_on_connection,
            )
        except OperationalStateError as exc:
            if exc.reason_code == "candidate_digest_mismatch":
                raise OperationalStateError(
                    "objective_candidate_digest_mismatch"
                ) from None
            raise


__all__ = [
    "COGNITIVE_SCHEMA_GENERATION",
    "COGNITIVE_SCHEMA_MARKER",
    "CognitiveStateStore",
]
