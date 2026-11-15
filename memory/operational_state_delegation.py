from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)
from memory.operational_state_store import OperationalStateStore


DELEGATION_CONTRACT_VERSION = "yunos-delegation-contract-1"
OUTCOME_LOOP_VERSION = "yunos-outcome-loop-1"
RISK_LEVELS = frozenset({"low", "medium", "high"})

_ALWAYS_PROHIBITED_AGENT_ACTIONS = frozenset(
    {
        "declare_project_complete",
        "declare_project_sealed",
        "resolve_conflict",
        "start_next_version",
        "create_long_term_plan",
        "update_user_profile",
    }
)
_SCOPED_ACTIONS = frozenset(
    {
        "read",
        "write",
        "use_tool",
        "create_node",
        "change_version",
        "commit",
        "merge",
        "tag",
        "push",
        *_ALWAYS_PROHIBITED_AGENT_ACTIONS,
    }
)


def _canonical_scope(values: Sequence[str], reason_code: str) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if not normalized or any(
        not isinstance(value, str)
        or not value
        or len(value) > 512
        for value in normalized
    ):
        raise OperationalStateError(reason_code)
    return normalized


def _scope_allows(allowed: Sequence[str], requested: str) -> bool:
    if "*" in allowed:
        return True
    for scope in allowed:
        if requested == scope or requested.startswith(scope.rstrip("/") + "/"):
            return True
    return False


@dataclass(frozen=True)
class DelegationContract:
    delegation_id: str
    project_id: str
    work_item_id: str
    intent: str
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    tool_scope: tuple[str, ...]
    risk: str
    valid_from: str
    expires_at: str
    acceptance_criteria: tuple[str, ...]
    rollback: str
    version_scope: tuple[str, ...]
    commit_permission: bool
    merge_permission: bool
    tag_permission: bool
    push_permission: bool
    next_node_permission: bool
    authority_digest: str

    def __post_init__(self) -> None:
        validate_identity(self.delegation_id, "invalid_delegation_id")
        validate_identity(self.project_id, "invalid_project_id")
        validate_identity(self.work_item_id, "invalid_work_item_id")
        if not isinstance(self.intent, str) or not 1 <= len(self.intent) <= 1000:
            raise OperationalStateError("invalid_delegation_intent")
        object.__setattr__(
            self,
            "read_scope",
            _canonical_scope(self.read_scope, "invalid_delegation_read_scope"),
        )
        object.__setattr__(
            self,
            "write_scope",
            _canonical_scope(self.write_scope, "invalid_delegation_write_scope"),
        )
        object.__setattr__(
            self,
            "prohibited_actions",
            _canonical_scope(
                self.prohibited_actions,
                "invalid_delegation_prohibited_actions",
            ),
        )
        object.__setattr__(
            self,
            "tool_scope",
            _canonical_scope(self.tool_scope, "invalid_delegation_tool_scope"),
        )
        if self.risk not in RISK_LEVELS:
            raise OperationalStateError("invalid_delegation_risk")
        object.__setattr__(
            self, "valid_from", normalize_timestamp(self.valid_from)
        )
        object.__setattr__(
            self, "expires_at", normalize_timestamp(self.expires_at)
        )
        if self.expires_at <= self.valid_from:
            raise OperationalStateError("invalid_delegation_interval")
        criteria = tuple(
            sorted(
                set(
                    criterion.strip()
                    for criterion in self.acceptance_criteria
                    if isinstance(criterion, str) and criterion.strip()
                )
            )
        )
        if not criteria or len(criteria) != len(set(self.acceptance_criteria)):
            raise OperationalStateError("invalid_acceptance_criteria")
        object.__setattr__(self, "acceptance_criteria", criteria)
        if not isinstance(self.rollback, str) or not self.rollback.strip():
            raise OperationalStateError("delegation_rollback_required")
        versions = _canonical_scope(
            self.version_scope, "invalid_delegation_version_scope"
        )
        object.__setattr__(self, "version_scope", versions)
        validate_sha256(self.authority_digest, "invalid_authority_digest")

    @property
    def contract_digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": DELEGATION_CONTRACT_VERSION,
            "delegation_id": self.delegation_id,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "intent": self.intent,
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "prohibited_actions": list(self.prohibited_actions),
            "tool_scope": list(self.tool_scope),
            "risk": self.risk,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "acceptance_criteria": list(self.acceptance_criteria),
            "rollback": self.rollback,
            "version_scope": list(self.version_scope),
            "commit_permission": self.commit_permission,
            "merge_permission": self.merge_permission,
            "tag_permission": self.tag_permission,
            "push_permission": self.push_permission,
            "next_node_permission": self.next_node_permission,
            "authority_digest": self.authority_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DelegationContract":
        value = dict(payload)
        if value.pop("contract_version", None) != DELEGATION_CONTRACT_VERSION:
            raise OperationalStateError("invalid_delegation_contract_version")
        for field in (
            "read_scope",
            "write_scope",
            "prohibited_actions",
            "tool_scope",
            "acceptance_criteria",
            "version_scope",
        ):
            if not isinstance(value.get(field), list):
                raise OperationalStateError("invalid_delegation_contract")
            value[field] = tuple(value[field])
        try:
            return cls(**value)
        except TypeError:
            raise OperationalStateError("invalid_delegation_contract") from None

    def validate_against_authority(
        self, authority: AuthorityArtifact
    ) -> None:
        if authority.authority_digest != self.authority_digest:
            raise OperationalStateError("delegation_authority_digest_mismatch")
        if authority.issued_by.startswith("agent:"):
            raise OperationalStateError("agent_cannot_authorize_delegation")
        if authority.target_project_id != self.project_id:
            raise OperationalStateError("authority_project_mismatch")
        if (
            self.valid_from < authority.issued_at
            or self.expires_at > authority.expires_at
        ):
            raise OperationalStateError(
                "delegation_exceeds_authority_interval"
            )
        for requested in self.read_scope:
            if not _scope_allows(authority.read_scope, requested):
                raise OperationalStateError(
                    "delegation_exceeds_authority_read_scope"
                )
        for requested in self.write_scope:
            if not _scope_allows(authority.write_scope, requested):
                raise OperationalStateError(
                    "delegation_exceeds_authority_write_scope"
                )
        permission_pairs = (
            ("commit", self.commit_permission, authority.commit_permission),
            ("merge", self.merge_permission, authority.merge_permission),
            ("tag", self.tag_permission, authority.tag_permission),
            ("push", self.push_permission, authority.push_permission),
            (
                "next_node",
                self.next_node_permission,
                authority.next_node_permission,
            ),
        )
        for name, requested, permitted in permission_pairs:
            if requested and name in authority.prohibited_operations:
                raise OperationalStateError(
                    f"delegation_conflicts_with_{name}_prohibition"
                )
            if requested and not permitted:
                raise OperationalStateError(
                    f"delegation_exceeds_{name}_authority"
                )
        authority.authorize_event(
            project_id=self.project_id,
            entity_id=self.delegation_id,
            event_type="delegation_proposed",
            at=self.valid_from,
        )

    def validate_action(
        self,
        action: Mapping[str, Any],
        *,
        at: str,
    ) -> tuple[str, ...]:
        timestamp = normalize_timestamp(at)
        action_type = action.get("action")
        if action_type not in _SCOPED_ACTIONS:
            return ("unknown_action",)
        violations: list[str] = []
        if not (self.valid_from <= timestamp < self.expires_at):
            violations.append("authority_expired_or_not_yet_valid")
        if action_type in self.prohibited_actions:
            violations.append("contract_prohibited_action")
        if action_type in _ALWAYS_PROHIBITED_AGENT_ACTIONS:
            violations.append(f"agent_{action_type}_forbidden")
        if action_type == "read":
            target = action.get("target")
            if not isinstance(target, str) or not _scope_allows(
                self.read_scope, target
            ):
                violations.append("read_scope_expansion")
        elif action_type == "write":
            target = action.get("target")
            if not isinstance(target, str) or not _scope_allows(
                self.write_scope, target
            ):
                violations.append("write_scope_expansion")
        elif action_type == "use_tool":
            tool = action.get("tool")
            if not isinstance(tool, str) or not _scope_allows(
                self.tool_scope, tool
            ):
                violations.append("tool_scope_expansion")
        elif action_type == "create_node" and not self.next_node_permission:
            violations.append("next_node_not_authorized")
        elif action_type == "change_version":
            version = action.get("version")
            if not isinstance(version, str) or version not in self.version_scope:
                violations.append("version_scope_expansion")
        elif action_type == "commit" and not self.commit_permission:
            violations.append("commit_not_authorized")
        elif action_type == "merge" and not self.merge_permission:
            violations.append("merge_not_authorized")
        elif action_type == "tag" and not self.tag_permission:
            violations.append("tag_not_authorized")
        elif action_type == "push" and not self.push_permission:
            violations.append("push_not_authorized")
        return tuple(sorted(set(violations)))


class DelegationService:
    def __init__(
        self,
        store: OperationalStateStore,
        *,
        source_anchor: SourceAnchor,
    ) -> None:
        self.store = store
        self.source_anchor = source_anchor

    def _contract(self, delegation_id: str) -> DelegationContract:
        record = self.store.get_delegation_contract(delegation_id)
        contract = DelegationContract.from_dict(record["contract"])
        if contract.contract_digest != record["contract_digest"]:
            raise OperationalStateError(
                "delegation_contract_record_tampered"
            )
        return contract

    def _append(
        self,
        *,
        authority: AuthorityArtifact,
        contract: DelegationContract,
        entity_id: str,
        entity_type: str,
        event_type: str,
        state: str,
        at: str,
        actor_kind: str,
        actor_id: str,
        authority_kind: str,
        source_kind: str,
        payload: Mapping[str, Any],
        lease_owner_id: str,
        lease_token: str,
        source_anchor: SourceAnchor | None = None,
        supersedes_event_id: str | None = None,
        caused_by_event_id: str | None = None,
    ):
        if authority.authority_digest != contract.authority_digest:
            raise OperationalStateError("delegation_authority_digest_mismatch")
        timestamp = normalize_timestamp(at)
        event = build_state_event(
            project_id=contract.project_id,
            entity_id=entity_id,
            entity_type=entity_type,
            event_type=event_type,
            source_kind=source_kind,
            source_anchor=source_anchor or self.source_anchor,
            authority_kind=authority_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
            observed_at=timestamp,
            effective_at=timestamp,
            recorded_at=timestamp,
            expires_at=contract.expires_at,
            supersedes_event_id=supersedes_event_id,
            caused_by_event_id=caused_by_event_id,
            idempotency_key=make_identity(
                "idempotency",
                {
                    "contract_digest": contract.contract_digest,
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "state": state,
                    "at": timestamp,
                    "payload": payload,
                },
            ),
            conflict_group_id=None,
            payload={
                "state": state,
                "delegation_id": contract.delegation_id,
                "contract_digest": contract.contract_digest,
                **dict(payload),
            },
            privacy_scope="private_metadata",
            authority_digest=authority.authority_digest,
        )
        preview = self.store.preview_event(authority=authority, event=event)
        receipt = self.store.commit_preview(
            authority=authority,
            preview=preview,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        return event, receipt

    def create(
        self,
        *,
        contract: DelegationContract,
        authority: AuthorityArtifact,
        user_actor_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        contract.validate_against_authority(authority)
        if user_actor_id.startswith("agent:"):
            raise OperationalStateError("agent_cannot_propose_delegation")
        event, receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=contract.delegation_id,
            entity_type="delegation",
            event_type="delegation_proposed",
            state="proposed",
            at=contract.valid_from,
            actor_kind="user",
            actor_id=user_actor_id,
            authority_kind="user",
            source_kind="declared",
            payload={
                "intent": contract.intent,
                "work_item_id": contract.work_item_id,
                "risk": contract.risk,
                "acceptance_criteria": list(
                    contract.acceptance_criteria
                ),
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        registered = self.store.register_delegation_contract(
            delegation_id=contract.delegation_id,
            project_id=contract.project_id,
            contract=contract.to_dict(),
            authority_digest=authority.authority_digest,
            valid_from=contract.valid_from,
            expires_at=contract.expires_at,
            created_event_id=event.event_id,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        return {
            "status": "proposed",
            "delegation_id": contract.delegation_id,
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            **registered,
        }

    def transition(
        self,
        *,
        delegation_id: str,
        next_state: str,
        authority: AuthorityArtifact,
        actor_kind: str,
        actor_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
        reason_code: str,
        payload: Mapping[str, Any] | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        contract = self._contract(delegation_id)
        if authority.authority_digest != contract.authority_digest:
            raise OperationalStateError("delegation_authority_digest_mismatch")
        projection = self.store.get_projection(delegation_id)
        if projection is None:
            raise OperationalStateError("delegation_projection_not_found")
        current_event_id = projection["winner_events"]["state"]
        event, receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=delegation_id,
            entity_type="delegation",
            event_type=event_type or f"delegation_{next_state}",
            state=next_state,
            at=at,
            actor_kind=actor_kind,
            actor_id=actor_id,
            authority_kind="user",
            source_kind=(
                "declared" if actor_kind == "user" else "observed"
            ),
            payload={
                "intent": contract.intent,
                "work_item_id": contract.work_item_id,
                "risk": contract.risk,
                "acceptance_criteria": list(
                    contract.acceptance_criteria
                ),
                "reason_code": reason_code,
                "previous_state": projection["state"],
                **dict(payload or {}),
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            supersedes_event_id=current_event_id,
        )
        return {
            "delegation_id": delegation_id,
            "previous_state": projection["state"],
            "state": next_state,
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
        }

    def enforce_action(
        self,
        *,
        delegation_id: str,
        proposed_action: Mapping[str, Any],
        authority: AuthorityArtifact,
        agent_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if not agent_id.startswith("agent:"):
            raise OperationalStateError("invalid_agent_identity")
        contract = self._contract(delegation_id)
        violations = contract.validate_action(proposed_action, at=at)
        if not violations:
            payload = {
                "status": "authorized",
                "delegation_id": delegation_id,
                "proposed_action": dict(proposed_action),
                "contract_digest": contract.contract_digest,
            }
            return {**payload, "decision_digest": canonical_digest(payload)}
        waiting = self.transition(
            delegation_id=delegation_id,
            next_state="waiting",
            authority=authority,
            actor_kind="agent",
            actor_id=agent_id,
            at=at,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            reason_code="agent_overreach_blocked",
            event_type="delegation_overreach",
            payload={
                "proposed_action": dict(proposed_action),
                "violated_authority": list(violations),
                "operation_executed": False,
                "project_completion_updated": False,
            },
        )
        return {
            "status": "blocked_waiting_for_controller",
            "reason_code": "agent_overreach_blocked",
            "violations": list(violations),
            "proposed_action": dict(proposed_action),
            "operation_executed": False,
            **waiting,
        }

    def record_execution_result(
        self,
        *,
        delegation_id: str,
        outcome_id: str,
        runtime_run_id: str,
        result: str,
        receipt_digest: str,
        artifact_references: Sequence[str],
        authority: AuthorityArtifact,
        runtime_actor_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if result not in {"completed", "failed"}:
            raise OperationalStateError("invalid_execution_result")
        validate_sha256(receipt_digest, "invalid_receipt_digest")
        contract = self._contract(delegation_id)
        transition = self.transition(
            delegation_id=delegation_id,
            next_state=result,
            authority=authority,
            actor_kind="runtime",
            actor_id=runtime_actor_id,
            at=at,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            reason_code=f"runtime_reported_{result}",
            payload={
                "runtime_run_id": runtime_run_id,
                "receipt_digest": receipt_digest,
                "work_item_completion_updated": False,
            },
        )
        evidence = {
            "runtime_run_id": runtime_run_id,
            "receipt_digest": receipt_digest,
            "artifact_references": sorted(set(artifact_references)),
            "execution_result": result,
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind="runtime",
            locator=f"runtime-run:{runtime_run_id}",
            evidence_digest=canonical_digest(evidence),
            observed_at=at,
            privacy_scope="private_metadata",
        )
        outcome_event, outcome_receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=outcome_id,
            entity_type="outcome",
            event_type="outcome_reported",
            state="reported",
            at=at,
            actor_kind="runtime",
            actor_id=runtime_actor_id,
            authority_kind="verified_runtime",
            source_kind="observed",
            payload={
                **evidence,
                "delegation_state": result,
                "verified": False,
                "accepted": False,
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            source_anchor=anchor,
            caused_by_event_id=transition["event_id"],
        )
        outcome = {
            "outcome_loop_version": OUTCOME_LOOP_VERSION,
            "outcome_id": outcome_id,
            "delegation_id": delegation_id,
            **evidence,
            "status": "reported",
            "verified": False,
            "accepted": False,
            "work_item_completion_updated": False,
        }
        projection = self.store.record_outcome_projection(
            outcome_id=outcome_id,
            delegation_id=delegation_id,
            runtime_run_id=runtime_run_id,
            receipt_digest=receipt_digest,
            verification_digest=None,
            artifact_references=artifact_references,
            status="reported",
            outcome=outcome,
            current_event_id=outcome_event.event_id,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            recorded_at=at,
        )
        return {
            "delegation_transition": transition,
            "outcome_event_id": outcome_event.event_id,
            "outcome_receipt_digest": outcome_receipt.receipt_digest,
            "outcome": projection,
            "work_item_completion_updated": False,
        }

    def verify_outcome(
        self,
        *,
        outcome_id: str,
        verification_digest: str,
        passed: bool,
        authority: AuthorityArtifact,
        verifier_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        validate_sha256(
            verification_digest, "invalid_verification_digest"
        )
        record = next(
            (
                item
                for item in self.store.list_outcomes()
                if item["outcome_id"] == outcome_id
            ),
            None,
        )
        if record is None:
            raise OperationalStateError("outcome_not_found")
        contract = self._contract(record["delegation_id"])
        if record["status"] != "reported":
            raise OperationalStateError("outcome_not_reported")
        next_state = "verified" if passed else "rejected"
        projection = self.store.get_projection(outcome_id)
        evidence = {
            "outcome_id": outcome_id,
            "verification_digest": verification_digest,
            "passed": passed,
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind="runtime",
            locator=f"outcome-verification:{outcome_id}",
            evidence_digest=canonical_digest(evidence),
            observed_at=at,
            privacy_scope="private_metadata",
        )
        event, receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=outcome_id,
            entity_type="outcome",
            event_type=f"outcome_{next_state}",
            state=next_state,
            at=at,
            actor_kind="runtime",
            actor_id=verifier_id,
            authority_kind="verified_runtime",
            source_kind="observed",
            payload={
                **evidence,
                "accepted": False,
                "work_item_completion_updated": False,
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            source_anchor=anchor,
            supersedes_event_id=projection["winner_events"]["state"],
            caused_by_event_id=record["current_event_id"],
        )
        outcome = {
            **record["outcome"],
            "status": next_state,
            "verified": passed,
            "accepted": False,
            "verification_digest": verification_digest,
            "work_item_completion_updated": False,
        }
        result = self.store.record_outcome_projection(
            outcome_id=outcome_id,
            delegation_id=record["delegation_id"],
            runtime_run_id=record["runtime_run_id"],
            receipt_digest=record["receipt_digest"],
            verification_digest=verification_digest,
            artifact_references=record["artifact_references"],
            status=next_state,
            outcome=outcome,
            current_event_id=event.event_id,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            recorded_at=at,
        )
        delegation_transition = None
        if not passed:
            delegation_transition = self.transition(
                delegation_id=record["delegation_id"],
                next_state="rejected",
                authority=authority,
                actor_kind="runtime",
                actor_id=verifier_id,
                at=at,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                reason_code="outcome_verification_failed",
                payload={
                    "outcome_id": outcome_id,
                    "verification_digest": verification_digest,
                },
            )
        return {
            **result,
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            "delegation_transition": delegation_transition,
            "work_item_completion_updated": False,
        }

    def accept_outcome(
        self,
        *,
        outcome_id: str,
        authority: AuthorityArtifact,
        user_actor_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if authority.issued_by.startswith("agent:") or user_actor_id.startswith(
            "agent:"
        ):
            raise OperationalStateError("agent_cannot_accept_outcome")
        record = next(
            (
                item
                for item in self.store.list_outcomes()
                if item["outcome_id"] == outcome_id
            ),
            None,
        )
        if record is None:
            raise OperationalStateError("outcome_not_found")
        if record["status"] != "verified":
            raise OperationalStateError("outcome_not_verified")
        contract = self._contract(record["delegation_id"])
        projection = self.store.get_projection(outcome_id)
        evidence = {
            "outcome_id": outcome_id,
            "delegation_id": record["delegation_id"],
            "verification_digest": record["verification_digest"],
            "acceptance_authority_digest": authority.authority_digest,
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind="user_declaration",
            locator=f"outcome-acceptance:{outcome_id}",
            evidence_digest=canonical_digest(evidence),
            observed_at=at,
            privacy_scope="private_metadata",
        )
        event, receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=outcome_id,
            entity_type="outcome",
            event_type="outcome_accepted",
            state="accepted",
            at=at,
            actor_kind="user",
            actor_id=user_actor_id,
            authority_kind="user",
            source_kind="accepted_outcome",
            payload={
                **evidence,
                "accepted": True,
                "work_item_completion_updated": False,
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            source_anchor=anchor,
            supersedes_event_id=projection["winner_events"]["state"],
            caused_by_event_id=record["current_event_id"],
        )
        outcome = {
            **record["outcome"],
            "status": "accepted",
            "verified": True,
            "accepted": True,
            "acceptance_authority_digest": authority.authority_digest,
            "work_item_completion_updated": False,
        }
        result = self.store.record_outcome_projection(
            outcome_id=outcome_id,
            delegation_id=record["delegation_id"],
            runtime_run_id=record["runtime_run_id"],
            receipt_digest=record["receipt_digest"],
            verification_digest=record["verification_digest"],
            artifact_references=record["artifact_references"],
            status="accepted",
            outcome=outcome,
            current_event_id=event.event_id,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            recorded_at=at,
        )
        delegation_transition = self.transition(
            delegation_id=record["delegation_id"],
            next_state="accepted",
            authority=authority,
            actor_kind="user",
            actor_id=user_actor_id,
            at=at,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            reason_code="user_accepted_verified_outcome",
            payload={"outcome_id": outcome_id},
        )
        candidate_payload = {
            "outcome_id": outcome_id,
            "delegation_id": contract.delegation_id,
            "work_item_id": contract.work_item_id,
            "proposed_state": "completed",
            "acceptance_criteria": list(contract.acceptance_criteria),
            "outcome_digest": result["outcome_digest"],
        }
        candidate_id = make_identity("candidate", candidate_payload)
        candidate = self.store.create_candidate(
            candidate_id=candidate_id,
            project_id=contract.project_id,
            entity_id=contract.work_item_id,
            entity_type="work_item",
            candidate_type="work_item_completion",
            candidate=candidate_payload,
            confidence_ppm=1_000_000,
            source_anchor=anchor,
            created_at=at,
            expires_at=contract.expires_at,
            authority=authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        return {
            **result,
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            "delegation_transition": delegation_transition,
            "state_candidate": candidate,
            "work_item_completion_updated": False,
        }

    def approve_and_apply_completion_candidate(
        self,
        *,
        candidate_id: str,
        authority: AuthorityArtifact,
        user_actor_id: str,
        at: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if authority.issued_by.startswith("agent:") or user_actor_id.startswith(
            "agent:"
        ):
            raise OperationalStateError(
                "agent_cannot_apply_completion_candidate"
            )
        candidates = {
            item["candidate_id"]: item
            for item in self.store.list_candidates()
        }
        candidate = candidates.get(candidate_id)
        if (
            candidate is None
            or candidate["candidate_type"] != "work_item_completion"
            or candidate["status"] != "pending"
        ):
            raise OperationalStateError(
                "completion_candidate_not_pending"
            )
        decision = self.store.decide_candidate(
            candidate_id=candidate_id,
            decision="approve",
            authority=authority,
            recorded_at=at,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        work_item_id = candidate["candidate"]["work_item_id"]
        projection = self.store.get_projection(work_item_id)
        if projection is None:
            raise OperationalStateError("work_item_projection_not_found")
        evidence = next(
            (
                item
                for item in self.store.list_source_evidence(
                    project_id=candidate["project_id"]
                )
                if item["anchor_id"] == candidate["source_anchor_id"]
            ),
            None,
        )
        if evidence is None:
            raise OperationalStateError("candidate_source_anchor_missing")
        anchor = SourceAnchor.from_dict(
            {
                key: evidence[key]
                for key in (
                    "anchor_id",
                    "source_kind",
                    "locator",
                    "evidence_digest",
                    "observed_at",
                    "privacy_scope",
                )
            }
        )
        contract = self._contract(
            candidate["candidate"]["delegation_id"]
        )
        event, receipt = self._append(
            authority=authority,
            contract=contract,
            entity_id=work_item_id,
            entity_type="work_item",
            event_type="work_item_completed",
            state="completed",
            at=at,
            actor_kind="user",
            actor_id=user_actor_id,
            authority_kind="accepted_outcome",
            source_kind="accepted_outcome",
            payload={
                "completion_candidate_id": candidate_id,
                "outcome_id": candidate["candidate"]["outcome_id"],
                "candidate_approval_id": decision["approval_id"],
            },
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            source_anchor=anchor,
            supersedes_event_id=projection["winner_events"]["state"],
        )
        return {
            "status": "projection_updated",
            "candidate_decision": decision,
            "work_item_id": work_item_id,
            "work_item_state": "completed",
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
        }


__all__ = [
    "DELEGATION_CONTRACT_VERSION",
    "OUTCOME_LOOP_VERSION",
    "DelegationContract",
    "DelegationService",
]
