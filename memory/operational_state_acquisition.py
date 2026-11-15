from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from memory.operational_state_models import (
    ENTITY_TYPES,
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    StateEvent,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    utc_now,
    validate_identity,
)
from memory.runtime_protocol_contracts import profile_item_identity


ACQUISITION_CONTRACT_VERSION = "yunos-operational-state-acquisition-1"
AUTHORITATIVE_PROPOSAL = "authoritative_event"
CANDIDATE_PROPOSAL = "candidate"
DIAGNOSTIC_PROPOSAL = "diagnostic"
PROPOSAL_KINDS = frozenset(
    {AUTHORITATIVE_PROPOSAL, CANDIDATE_PROPOSAL, DIAGNOSTIC_PROPOSAL}
)
APPROVED_MEMORY_KINDS = frozenset(
    {"fact", "preference", "goal", "decision", "constraint"}
)
AMBIGUOUS_FIELDS = frozenset(
    {
        "project_completion",
        "user_priority",
        "ambiguous_goal",
        "value_conflict",
        "pending_decision",
        "conflicting_user_statement",
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_alias(value: str, reason_code: str = "invalid_source_alias") -> str:
    validate_identity(value, reason_code)
    if "\\" in value or "/" in value:
        raise OperationalStateError(reason_code)
    return value


def _approved_time_to_utc(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise OperationalStateError("approved_memory_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OperationalStateError("approved_memory_time_invalid") from None
    if parsed.tzinfo is None:
        raise OperationalStateError("approved_memory_time_invalid")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


@dataclass(frozen=True)
class AcquisitionProposal:
    proposal_kind: str
    project_id: str
    entity_id: str
    entity_type: str
    event_type: str
    source_kind: str
    source_anchor: SourceAnchor
    authority_kind: str
    actor_kind: str
    actor_id: str
    observed_at: str
    effective_at: str
    expires_at: str | None
    supersedes_event_id: str | None
    caused_by_event_id: str | None
    conflict_group_id: str | None
    payload: dict[str, Any]
    privacy_scope: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.proposal_kind not in PROPOSAL_KINDS:
            raise OperationalStateError("invalid_acquisition_proposal_kind")
        validate_identity(self.project_id, "invalid_project_id")
        validate_identity(self.entity_id, "invalid_entity_id")
        if self.entity_type not in ENTITY_TYPES:
            raise OperationalStateError("invalid_entity_type")
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))
        object.__setattr__(self, "effective_at", normalize_timestamp(self.effective_at))
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", normalize_timestamp(self.expires_at)
            )
        if not isinstance(self.payload, dict):
            raise OperationalStateError("acquisition_payload_must_be_object")
        canonical_json(self.payload)

    @property
    def proposal_digest(self) -> str:
        return canonical_digest(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": ACQUISITION_CONTRACT_VERSION,
            "proposal_kind": self.proposal_kind,
            "project_id": self.project_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "event_type": self.event_type,
            "source_kind": self.source_kind,
            "source_anchor": self.source_anchor.to_dict(),
            "authority_kind": self.authority_kind,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "supersedes_event_id": self.supersedes_event_id,
            "caused_by_event_id": self.caused_by_event_id,
            "conflict_group_id": self.conflict_group_id,
            "payload": self.payload,
            "privacy_scope": self.privacy_scope,
            "reason_code": self.reason_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "proposal_digest": self.proposal_digest,
        }

    def to_event(
        self,
        *,
        authority: AuthorityArtifact,
        recorded_at: str,
        idempotency_key: str | None = None,
    ) -> StateEvent:
        if self.proposal_kind != AUTHORITATIVE_PROPOSAL:
            raise OperationalStateError("proposal_is_not_authoritative")
        return build_state_event(
            project_id=self.project_id,
            entity_id=self.entity_id,
            entity_type=self.entity_type,
            event_type=self.event_type,
            source_kind=self.source_kind,
            source_anchor=self.source_anchor,
            authority_kind=self.authority_kind,
            actor_kind=self.actor_kind,
            actor_id=self.actor_id,
            observed_at=self.observed_at,
            effective_at=self.effective_at,
            recorded_at=recorded_at,
            expires_at=self.expires_at,
            supersedes_event_id=self.supersedes_event_id,
            caused_by_event_id=self.caused_by_event_id,
            idempotency_key=(
                idempotency_key
                or make_identity(
                    "idempotency",
                    {
                        "proposal_digest": self.proposal_digest,
                        "authority_digest": authority.authority_digest,
                    },
                )
            ),
            conflict_group_id=self.conflict_group_id,
            payload=self.payload,
            privacy_scope=self.privacy_scope,
            authority_digest=authority.authority_digest,
        )


class UserDeclarationAdapter:
    _ACTIONS = {
        "declare": "declared",
        "approve": "approved",
        "revoke": "revoked",
        "replace": "superseded",
    }

    @classmethod
    def adapt(
        cls,
        *,
        project_id: str,
        entity_id: str,
        entity_type: str,
        action: str,
        state: str,
        statement: str,
        declared_at: str,
        user_actor_id: str,
        supersedes_event_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        privacy_scope: str = "private_metadata",
    ) -> AcquisitionProposal:
        if action not in cls._ACTIONS:
            raise OperationalStateError("unsupported_user_declaration_action")
        if not isinstance(statement, str) or not statement.strip():
            raise OperationalStateError("user_declaration_statement_required")
        timestamp = normalize_timestamp(declared_at)
        evidence = {
            "action": action,
            "statement": statement.strip(),
            "declared_at": timestamp,
            "user_actor_id": user_actor_id,
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind="user_declaration",
            locator="user-declaration:explicit",
            evidence_digest=canonical_digest(evidence),
            observed_at=timestamp,
            privacy_scope=privacy_scope,
        )
        merged = {
            "state": state,
            "statement": statement.strip(),
            "declaration_action": action,
            **dict(payload or {}),
        }
        return AcquisitionProposal(
            proposal_kind=AUTHORITATIVE_PROPOSAL,
            project_id=project_id,
            entity_id=entity_id,
            entity_type=entity_type,
            event_type=f"{entity_type}_{cls._ACTIONS[action]}",
            source_kind="declared",
            source_anchor=anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id=user_actor_id,
            observed_at=timestamp,
            effective_at=timestamp,
            expires_at=None,
            supersedes_event_id=supersedes_event_id,
            caused_by_event_id=None,
            conflict_group_id=None,
            payload=merged,
            privacy_scope=privacy_scope,
            reason_code="explicit_user_declaration",
        )


class GitObservationAdapter:
    def __init__(
        self,
        repository: str | os.PathLike[str],
        *,
        repository_alias: str,
        timeout_seconds: int = 15,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.repository_alias = _safe_alias(repository_alias)
        self.timeout_seconds = timeout_seconds
        if not self.repository.is_dir() or not (self.repository / ".git").exists():
            raise OperationalStateError("git_repository_unavailable")

    def _git(self, *args: str, check: bool = True) -> str:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.repository}",
                "-C",
                str(self.repository),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            env=environment,
        )
        if check and result.returncode != 0:
            raise OperationalStateError("git_observation_failed")
        return result.stdout.strip()

    def observe(
        self,
        *,
        project_id: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        head = self._git("rev-parse", "HEAD")
        branch = self._git("branch", "--show-current") or None
        tags = sorted(
            line
            for line in self._git("tag", "--points-at", "HEAD").splitlines()
            if line
        )
        porcelain = [
            line
            for line in self._git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ).splitlines()
            if line
        ]
        staged_paths = sorted(
            line
            for line in self._git(
                "diff", "--cached", "--name-only", "--no-ext-diff"
            ).splitlines()
            if line
        )
        worktree_paths = sorted(
            line
            for line in self._git(
                "diff", "--name-only", "--no-ext-diff"
            ).splitlines()
            if line
        )
        untracked_paths = sorted(
            line[3:] for line in porcelain if line.startswith("?? ")
        )
        upstream = self._git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
            check=False,
        )
        ahead = behind = None
        if upstream:
            counts = self._git(
                "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
            ).split()
            if len(counts) == 2:
                ahead, behind = (int(counts[0]), int(counts[1]))
        commit_fields = self._git(
            "show", "-s", "--format=%H%x00%T%x00%P%x00%cI", "HEAD"
        ).split("\x00")
        if len(commit_fields) != 4:
            raise OperationalStateError("git_commit_evidence_invalid")
        evidence = {
            "repository_alias": self.repository_alias,
            "project_id": project_id,
            "branch": branch,
            "head": head,
            "head_tags": tags,
            "worktree": {
                "clean": not porcelain,
                "changed_count": len(porcelain),
                "tracked_change_count": len(worktree_paths),
                "tracked_path_digest": canonical_digest(worktree_paths),
            },
            "staging": {
                "count": len(staged_paths),
                "path_digest": canonical_digest(staged_paths),
            },
            "untracked": {
                "count": len(untracked_paths),
                "path_digest": canonical_digest(untracked_paths),
            },
            "upstream": {
                "name": upstream or None,
                "ahead": ahead,
                "behind": behind,
                "available": bool(upstream),
            },
            "commit_evidence": {
                "commit": commit_fields[0],
                "tree": commit_fields[1],
                "parents": commit_fields[2].split() if commit_fields[2] else [],
                "committed_at": commit_fields[3],
            },
            "observed_at": timestamp,
            "observation_mode": "read_only_git",
        }
        digest = canonical_digest(evidence)
        anchor = SourceAnchor(
            anchor_id=make_identity(
                "anchor",
                {
                    "source": "git",
                    "repository_alias": self.repository_alias,
                    "evidence_digest": digest,
                },
            ),
            source_kind="git",
            locator=f"git-repository:{self.repository_alias}",
            evidence_digest=digest,
            observed_at=timestamp,
            privacy_scope="private_metadata",
        )
        return {
            "contract_version": ACQUISITION_CONTRACT_VERSION,
            "status": "observed",
            "source_kind": "git",
            "evidence": evidence,
            "source_anchor": anchor,
            "evidence_digest": digest,
        }

    def project_proposal(
        self,
        *,
        project_id: str,
        state: str = "active",
        observed_at: str | None = None,
    ) -> AcquisitionProposal:
        observation = self.observe(
            project_id=project_id, observed_at=observed_at
        )
        evidence = observation["evidence"]
        return AcquisitionProposal(
            proposal_kind=AUTHORITATIVE_PROPOSAL,
            project_id=project_id,
            entity_id=project_id,
            entity_type="project",
            event_type="project_observed",
            source_kind="observed",
            source_anchor=observation["source_anchor"],
            authority_kind="deterministic_observation",
            actor_kind="system",
            actor_id="system:git-observation-adapter",
            observed_at=evidence["observed_at"],
            effective_at=evidence["observed_at"],
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            conflict_group_id=None,
            payload={
                "state": state,
                "repository_alias": self.repository_alias,
                "branch": evidence["branch"],
                "head": evidence["head"],
                "head_tags": evidence["head_tags"],
                "worktree": evidence["worktree"],
                "staging": evidence["staging"],
                "untracked": evidence["untracked"],
                "upstream": evidence["upstream"],
                "commit_evidence": evidence["commit_evidence"],
            },
            privacy_scope="private_metadata",
            reason_code="deterministic_git_observation",
        )


class DatabaseObservationAdapter:
    def __init__(
        self,
        database: str | os.PathLike[str],
        *,
        database_alias: str,
    ) -> None:
        self.database = Path(database).resolve()
        self.database_alias = _safe_alias(database_alias)
        if not self.database.is_file():
            raise OperationalStateError("database_source_unavailable")

    def observe(
        self,
        *,
        project_id: str,
        observed_at: str | None = None,
        count_tables: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        try:
            with closing(
                sqlite3.connect(
                    f"file:{self.database.as_posix()}?mode=ro&immutable=1",
                    uri=True,
                    timeout=5.0,
                )
            ) as connection:
                connection.execute("PRAGMA query_only=ON")
                schema_rows = [
                    {
                        "type": row[0],
                        "name": row[1],
                        "table": row[2],
                        "sql": " ".join((row[3] or "").split()),
                    }
                    for row in connection.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' "
                        "ORDER BY type,name,tbl_name"
                    ).fetchall()
                ]
                tables = sorted(
                    row["name"]
                    for row in schema_rows
                    if row["type"] == "table"
                )
                selected = sorted(set(count_tables or tables))
                if any(table not in tables for table in selected):
                    raise OperationalStateError("database_count_table_missing")
                counts = {
                    table: connection.execute(
                        'SELECT COUNT(*) FROM "'
                        + table.replace('"', '""')
                        + '"'
                    ).fetchone()[0]
                    for table in selected
                }
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                foreign_key_violations = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                query_only = bool(
                    connection.execute("PRAGMA query_only").fetchone()[0]
                )
        except sqlite3.DatabaseError:
            raise OperationalStateError("database_observation_failed") from None
        evidence = {
            "database_alias": self.database_alias,
            "project_id": project_id,
            "path_identity": {
                "alias": self.database_alias,
                "absolute_path_persisted": False,
            },
            "size": self.database.stat().st_size,
            "sha256": _sha256_file(self.database),
            "schema_digest": canonical_digest(schema_rows),
            "schema_object_count": len(schema_rows),
            "table_counts": counts,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "readonly_state": {
                "sqlite_mode": "ro",
                "immutable": True,
                "query_only": query_only,
                "write_attempted": False,
            },
            "observed_at": timestamp,
        }
        digest = canonical_digest(evidence)
        anchor = SourceAnchor(
            anchor_id=make_identity(
                "anchor",
                {
                    "source": "database",
                    "database_alias": self.database_alias,
                    "evidence_digest": digest,
                },
            ),
            source_kind="database",
            locator=f"database:{self.database_alias}",
            evidence_digest=digest,
            observed_at=timestamp,
            privacy_scope="private_metadata",
        )
        return {
            "contract_version": ACQUISITION_CONTRACT_VERSION,
            "status": (
                "observed"
                if integrity == "ok" and foreign_key_violations == 0
                else "unverifiable"
            ),
            "source_kind": "database",
            "evidence": evidence,
            "source_anchor": anchor,
            "evidence_digest": digest,
        }


class RuntimeOutcomeAdapter:
    @staticmethod
    def adapt(
        *,
        project_id: str,
        entity_id: str,
        receipt: Mapping[str, Any],
        verification: Mapping[str, Any],
        observed_at: str,
        user_acceptance: AuthorityArtifact | None = None,
        previous_state: str | None = None,
        supersedes_event_id: str | None = None,
    ) -> AcquisitionProposal:
        receipt_payload = dict(receipt)
        verification_payload = dict(verification)
        run_id = receipt_payload.get("run_id") or receipt_payload.get(
            "model_run_id"
        )
        if not isinstance(run_id, str) or not run_id:
            raise OperationalStateError("runtime_run_id_required")
        result = receipt_payload.get("result")
        if result not in {"completed", "failed"}:
            raise OperationalStateError("runtime_result_invalid")
        verification_result = verification_payload.get("result")
        if verification_result not in {"passed", "failed", "unverifiable"}:
            raise OperationalStateError("runtime_verification_invalid")
        timestamp = normalize_timestamp(observed_at)
        accepted = user_acceptance is not None
        if accepted:
            if previous_state != "verified" or supersedes_event_id is None:
                raise OperationalStateError(
                    "runtime_acceptance_requires_verified_predecessor"
                )
            if user_acceptance.issued_by.startswith("agent:"):
                raise OperationalStateError("agent_cannot_accept_runtime_outcome")
            user_acceptance.authorize_event(
                project_id=project_id,
                entity_id=entity_id,
                event_type="outcome_accepted",
                at=timestamp,
            )
        if accepted:
            state = "accepted"
        elif (
            previous_state == "reported"
            and supersedes_event_id is not None
            and result == "completed"
            and verification_result == "passed"
        ):
            state = "verified"
        else:
            state = "reported"
        evidence = {
            "run_id": run_id,
            "receipt_digest": canonical_digest(receipt_payload),
            "verification_digest": canonical_digest(verification_payload),
            "result": result,
            "verification_result": verification_result,
            "accepted": accepted,
            "acceptance_authority_digest": (
                user_acceptance.authority_digest if user_acceptance else None
            ),
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind="runtime",
            locator=f"runtime-run:{run_id}",
            evidence_digest=canonical_digest(evidence),
            observed_at=timestamp,
            privacy_scope="private_metadata",
        )
        return AcquisitionProposal(
            proposal_kind=AUTHORITATIVE_PROPOSAL,
            project_id=project_id,
            entity_id=entity_id,
            entity_type="outcome",
            event_type=f"outcome_{state}",
            source_kind="accepted_outcome" if accepted else "observed",
            source_anchor=anchor,
            authority_kind="accepted_outcome" if accepted else "verified_runtime",
            actor_kind="user" if accepted else "runtime",
            actor_id=(
                user_acceptance.issued_by
                if user_acceptance
                else "runtime:v8-verified-outcome"
            ),
            observed_at=timestamp,
            effective_at=timestamp,
            expires_at=None,
            supersedes_event_id=supersedes_event_id,
            caused_by_event_id=None,
            conflict_group_id=None,
            payload={"state": state, **evidence},
            privacy_scope="private_metadata",
            reason_code=(
                "explicit_user_accepted_runtime_outcome"
                if accepted
                else "verified_runtime_outcome_not_user_accepted"
            ),
        )


class ApprovedMemoryAdapter:
    @staticmethod
    def adapt(
        *,
        project_id: str,
        approved_items: Iterable[Mapping[str, Any]],
        project_topics: Sequence[str],
        observed_at: str,
    ) -> list[AcquisitionProposal]:
        timestamp = normalize_timestamp(observed_at)
        topics = {topic.casefold() for topic in project_topics if topic}
        proposals: list[AcquisitionProposal] = []
        for raw_item in approved_items:
            item = dict(raw_item)
            if set(item) != {
                "schema_version",
                "profile_item_id",
                "kind",
                "statement",
                "topics",
                "approval",
            }:
                raise OperationalStateError("approved_memory_item_invalid")
            if item.get("schema_version") != "v7.3-approved-profile-input-1":
                raise OperationalStateError("approved_memory_schema_invalid")
            if item.get("kind") not in APPROVED_MEMORY_KINDS:
                raise OperationalStateError("approved_memory_kind_invalid")
            approval = item.get("approval")
            if (
                not isinstance(approval, dict)
                or approval.get("status") != "approved"
                or approval.get("authority") != "user"
            ):
                raise OperationalStateError("approved_memory_not_user_approved")
            if profile_item_identity(item) != item.get("profile_item_id"):
                raise OperationalStateError("approved_memory_identity_mismatch")
            item_topics = {
                str(topic).casefold() for topic in item.get("topics", [])
            }
            if not topics.intersection(item_topics):
                continue
            profile_item_id = item.get("profile_item_id")
            statement = item.get("statement")
            if not isinstance(profile_item_id, str) or not isinstance(statement, str):
                raise OperationalStateError("approved_memory_item_invalid")
            evidence_digest = canonical_digest(item)
            anchor = SourceAnchor(
                anchor_id=make_identity(
                    "anchor",
                    {
                        "source": "approved_memory",
                        "profile_item_id": profile_item_id,
                        "evidence_digest": evidence_digest,
                    },
                ),
                source_kind="approved_memory",
                locator=f"approved-memory:{profile_item_id}",
                evidence_digest=evidence_digest,
                observed_at=timestamp,
                privacy_scope="private_content",
            )
            entity_type = (
                item["kind"]
                if item["kind"] in {"goal", "decision"}
                else "commitment"
            )
            state = (
                "approved"
                if entity_type in {"goal", "decision"}
                else "active"
            )
            entity_id = make_identity(
                entity_type,
                {
                    "project_id": project_id,
                    "profile_item_id": profile_item_id,
                },
            )
            proposals.append(
                AcquisitionProposal(
                    proposal_kind=AUTHORITATIVE_PROPOSAL,
                    project_id=project_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    event_type=f"{entity_type}_declared",
                    source_kind="declared",
                    source_anchor=anchor,
                    authority_kind="user",
                    actor_kind="system",
                    actor_id="system:approved-memory-adapter",
                    observed_at=timestamp,
                    effective_at=_approved_time_to_utc(
                        approval["approved_at"]
                    ),
                    expires_at=None,
                    supersedes_event_id=None,
                    caused_by_event_id=None,
                    conflict_group_id=None,
                    payload={
                        "state": state,
                        "statement": statement,
                        "approved_profile_item_id": profile_item_id,
                        "memory_kind": item["kind"],
                    },
                    privacy_scope="private_content",
                    reason_code="traceable_project_relevant_approved_memory",
                )
            )
        return sorted(proposals, key=lambda proposal: proposal.entity_id)


class StateReconciler:
    @staticmethod
    def semantic_key(proposal: AcquisitionProposal) -> str:
        return canonical_digest(
            {
                "project_id": proposal.project_id,
                "entity_id": proposal.entity_id,
                "entity_type": proposal.entity_type,
                "event_type": proposal.event_type,
                "payload": proposal.payload,
                "source_anchor_digest": proposal.source_anchor.evidence_digest,
            }
        )

    def plan(
        self,
        proposals: Iterable[AcquisitionProposal],
        *,
        existing_events: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        existing_keys: set[str] = set()
        for entry in existing_events:
            event = entry.get("event", entry)
            existing_keys.add(
                canonical_digest(
                    {
                        "project_id": event["project_id"],
                        "entity_id": event["entity_id"],
                        "entity_type": event["entity_type"],
                        "event_type": event["event_type"],
                        "payload": event["canonical_payload"],
                        "source_anchor_digest": event["source_anchor"][
                            "evidence_digest"
                        ],
                    }
                )
            )
        seen: set[str] = set()
        actions: list[dict[str, Any]] = []
        declarations_by_entity: dict[str, list[AcquisitionProposal]] = {}
        for proposal in sorted(
            proposals, key=lambda item: item.proposal_digest
        ):
            key = self.semantic_key(proposal)
            if key in existing_keys or key in seen:
                actions.append(
                    {
                        "action": "reject_duplicate",
                        "proposal_digest": proposal.proposal_digest,
                        "reason_code": "canonical_duplicate",
                    }
                )
                continue
            seen.add(key)
            if proposal.proposal_kind == DIAGNOSTIC_PROPOSAL:
                actions.append(
                    {
                        "action": "preserve_diagnostic",
                        "proposal_digest": proposal.proposal_digest,
                        "reason_code": proposal.reason_code,
                    }
                )
                continue
            if proposal.proposal_kind == CANDIDATE_PROPOSAL:
                actions.append(
                    {
                        "action": "create_candidate",
                        "proposal_digest": proposal.proposal_digest,
                        "reason_code": "candidate_requires_user_decision",
                    }
                )
                continue
            if (
                proposal.authority_kind == "user"
                and proposal.supersedes_event_id is None
            ):
                declarations_by_entity.setdefault(
                    proposal.entity_id, []
                ).append(proposal)
            actions.append(
                {
                    "action": "append_event",
                    "proposal_digest": proposal.proposal_digest,
                    "reason_code": proposal.reason_code,
                }
            )

        for entity_id, declarations in declarations_by_entity.items():
            states = {item.payload.get("state") for item in declarations}
            values = {
                canonical_digest(item.payload) for item in declarations
            }
            if len(declarations) > 1 and (len(states) > 1 or len(values) > 1):
                conflict_id = make_identity(
                    "conflict",
                    {
                        "entity_id": entity_id,
                        "members": sorted(
                            item.proposal_digest for item in declarations
                        ),
                    },
                )
                actions.append(
                    {
                        "action": "create_conflict_group",
                        "entity_id": entity_id,
                        "conflict_group_id": conflict_id,
                        "member_proposal_digests": sorted(
                            item.proposal_digest for item in declarations
                        ),
                        "reason_code": "conflicting_user_statements_require_manual_resolution",
                    }
                )
        actions.sort(
            key=lambda action: (
                action["action"],
                action.get("proposal_digest", ""),
                action.get("conflict_group_id", ""),
            )
        )
        payload = {
            "contract_version": ACQUISITION_CONTRACT_VERSION,
            "actions": actions,
            "agent_auto_resolution_allowed": False,
            "manual_resolution_fields": sorted(AMBIGUOUS_FIELDS),
        }
        return {**payload, "plan_digest": canonical_digest(payload)}


def freshness_for(
    *,
    observed_at: str,
    as_of: str,
    expires_at: str | None = None,
    source_available: bool = True,
    verifiable: bool = True,
    aging_after_seconds: int = 86_400,
    stale_after_seconds: int = 604_800,
) -> str:
    from datetime import datetime

    observed = datetime.fromisoformat(
        normalize_timestamp(observed_at).replace("Z", "+00:00")
    )
    current = datetime.fromisoformat(
        normalize_timestamp(as_of).replace("Z", "+00:00")
    )
    if current < observed:
        raise OperationalStateError("freshness_clock_anomaly")
    if not source_available:
        return "unverifiable"
    if not verifiable:
        return "unverifiable"
    if expires_at is not None and normalize_timestamp(expires_at) <= normalize_timestamp(
        as_of
    ):
        return "expired"
    age = (current - observed).total_seconds()
    if age >= stale_after_seconds:
        return "stale"
    if age >= aging_after_seconds:
        return "aging"
    return "fresh"


def source_diagnostic(
    *,
    project_id: str,
    entity_id: str,
    entity_type: str,
    source_kind: str,
    source_alias: str,
    observed_at: str,
    unavailable: bool = False,
    unverifiable: bool = False,
) -> AcquisitionProposal:
    if unavailable == unverifiable:
        raise OperationalStateError("source_diagnostic_state_required")
    timestamp = normalize_timestamp(observed_at)
    normalized_alias = _safe_alias(source_alias)
    reason_code = (
        "source_unavailable" if unavailable else "source_unverifiable"
    )
    evidence = {
        "source_kind": source_kind,
        "source_alias": normalized_alias,
        "reason_code": reason_code,
        "observed_at": timestamp,
        "content_read": False,
    }
    anchor_kind = source_kind if source_kind in {
        "git",
        "database",
        "file",
        "service",
        "runtime",
        "approved_memory",
    } else "file"
    anchor = SourceAnchor(
        anchor_id=make_identity("anchor", evidence),
        source_kind=anchor_kind,
        locator=f"{anchor_kind}:{normalized_alias}",
        evidence_digest=canonical_digest(evidence),
        observed_at=timestamp,
        privacy_scope="private_metadata",
    )
    return AcquisitionProposal(
        proposal_kind=DIAGNOSTIC_PROPOSAL,
        project_id=project_id,
        entity_id=entity_id,
        entity_type=entity_type,
        event_type=reason_code,
        source_kind="observed",
        source_anchor=anchor,
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:state-acquisition",
        observed_at=timestamp,
        effective_at=timestamp,
        expires_at=None,
        supersedes_event_id=None,
        caused_by_event_id=None,
        conflict_group_id=None,
        payload={"state": "active", **evidence},
        privacy_scope="private_metadata",
        reason_code=reason_code,
    )


__all__ = [
    "ACQUISITION_CONTRACT_VERSION",
    "AUTHORITATIVE_PROPOSAL",
    "CANDIDATE_PROPOSAL",
    "DIAGNOSTIC_PROPOSAL",
    "AcquisitionProposal",
    "ApprovedMemoryAdapter",
    "DatabaseObservationAdapter",
    "GitObservationAdapter",
    "RuntimeOutcomeAdapter",
    "StateReconciler",
    "UserDeclarationAdapter",
    "freshness_for",
    "source_diagnostic",
]
