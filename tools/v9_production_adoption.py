from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import (  # noqa: E402
    DEFAULT_OPERATIONAL_STATE_PATH,
    DEFAULT_REAL_DB_PATH,
)
from memory.operational_state_acquisition import (  # noqa: E402
    DatabaseObservationAdapter,
    GitObservationAdapter,
)
from memory.operational_state_context import (  # noqa: E402
    OperationalStateContextService,
)
from memory.operational_state_delegation import (  # noqa: E402
    DelegationContract,
    DelegationService,
)
from memory.operational_state_models import (  # noqa: E402
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
)
from memory.operational_state_store import (  # noqa: E402
    OperationalStateStore,
    initialize_operational_database,
    is_production_operational_path,
)


ADOPTION_CONTRACT_VERSION = "yunos-v9-production-adoption-1"
USER_ACTOR = "actor:local-user"
YUNOS_PROJECT = "project:yunos"
ENGLISH_PROJECT = "project:ayun-english-core"
PROHIBITED_FUTURE_NODES = (
    "v9-G",
    "v9.1",
    "v10",
)
EVENT_TYPES = (
    "project_declared",
    "project_observed",
    "goal_declared",
    "work_item_declared",
    "work_item_observed",
    "work_item_completed",
    "decision_declared",
    "blocker_observed",
    "commitment_declared",
    "resource_observed",
    "delegation_proposed",
    "delegation_authorized",
    "delegation_queued",
    "delegation_running",
    "delegation_waiting",
    "delegation_completed",
    "delegation_failed",
    "delegation_cancelled",
    "delegation_accepted",
    "delegation_rejected",
    "delegation_overreach",
    "outcome_reported",
    "outcome_verified",
    "outcome_accepted",
    "outcome_rejected",
    "candidate_decision",
    "candidate_create",
    "delegation_contract_register",
    "outcome_projection_update",
    "freshness_update",
    "snapshot_create",
    "projection_rebuild",
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(
        normalize_timestamp(value).replace("Z", "+00:00")
    )


def _run_git(repository: Path, *args: str) -> str:
    command = [
        "git",
        "-c",
        f"safe.directory={repository.resolve().as_posix()}",
        "-C",
        str(repository),
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        raise OperationalStateError("production_adoption_git_audit_failed") from None
    return result.stdout.strip()


def _git_observation(
    repository: Path,
    *,
    alias: str,
    project_id: str,
    observed_at: str,
) -> dict[str, Any]:
    before_status = _run_git(repository, "status", "--porcelain=v1", "-uall")
    observation = GitObservationAdapter(
        repository,
        repository_alias=alias,
    ).observe(project_id=project_id, observed_at=observed_at)
    after_status = _run_git(repository, "status", "--porcelain=v1", "-uall")
    if before_status != after_status:
        raise OperationalStateError("production_adoption_git_source_changed")
    evidence = observation["evidence"]
    return {
        **evidence,
        "tracked_change_count": evidence["worktree"]["tracked_change_count"],
        "staged_count": evidence["staging"]["count"],
        "untracked_count": evidence["untracked"]["count"],
        "evidence_digest": observation["evidence_digest"],
    }


def collect_real_evidence(
    *,
    yunos_repository: Path,
    english_repository: Path,
    observed_at: str,
) -> dict[str, Any]:
    timestamp = normalize_timestamp(observed_at)
    yunos = _git_observation(
        yunos_repository,
        alias="repo:yunos",
        project_id=YUNOS_PROJECT,
        observed_at=timestamp,
    )
    english = _git_observation(
        english_repository,
        alias="repo:ayun-english-core",
        project_id=ENGLISH_PROJECT,
        observed_at=timestamp,
    )
    database_observation = DatabaseObservationAdapter(
        DEFAULT_REAL_DB_PATH,
        database_alias="database:yunos-main",
    ).observe(
        project_id=YUNOS_PROJECT,
        observed_at=timestamp,
        count_tables=("conversations", "messages", "assets"),
    )
    database = {
        **database_observation["evidence"],
        "evidence_digest": database_observation["evidence_digest"],
    }
    return {
        "contract_version": ADOPTION_CONTRACT_VERSION,
        "observed_at": timestamp,
        "yunos_git": yunos,
        "english_git": english,
        "main_database": database,
        "external_repository_modified": False,
        "full_history_backfill": False,
    }


def _authority(
    project_id: str,
    *,
    issued_at: str,
    expires_at: str,
) -> AuthorityArtifact:
    return AuthorityArtifact(
        authority_id=f"authority:v9-production-adoption:{project_id.split(':')[-1]}",
        issued_by=USER_ACTOR,
        issued_at=issued_at,
        expires_at=expires_at,
        target_project_id=project_id,
        allowed_event_types=EVENT_TYPES,
        read_scope=("*",),
        write_scope=("*",),
        prohibited_operations=(
            "push",
            "create-next-node",
            "start-v9-G",
            "start-v9.1",
            "start-v10",
        ),
        commit_permission=True,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
    )


def _anchor(
    anchor_id: str,
    *,
    source_kind: str,
    locator: str,
    evidence_digest: str,
    observed_at: str,
) -> SourceAnchor:
    return SourceAnchor(
        anchor_id=anchor_id,
        source_kind=source_kind,
        locator=locator,
        evidence_digest=evidence_digest,
        observed_at=observed_at,
        privacy_scope="private_metadata",
    )


class AdoptionWriter:
    def __init__(
        self,
        store: OperationalStateStore,
        *,
        owner_id: str,
        lease_token: str,
        authorities: Mapping[str, AuthorityArtifact],
        start: datetime,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.lease_token = lease_token
        self.authorities = authorities
        self.cursor = start
        self.events: list[dict[str, Any]] = []

    def next_time(self) -> str:
        self.cursor += timedelta(seconds=1)
        return _timestamp(self.cursor)

    def append(
        self,
        *,
        project_id: str,
        entity_id: str,
        entity_type: str,
        event_type: str,
        state: str,
        anchor: SourceAnchor,
        payload: Mapping[str, Any],
        authority_kind: str = "user",
        actor_kind: str = "user",
        actor_id: str = USER_ACTOR,
        expires_at: str | None = None,
        supersedes_event_id: str | None = None,
    ):
        at = self.next_time()
        authority = self.authorities[project_id]
        event = build_state_event(
            project_id=project_id,
            entity_id=entity_id,
            entity_type=entity_type,
            event_type=event_type,
            source_kind=(
                "declared"
                if authority_kind == "user"
                else "observed"
            ),
            source_anchor=anchor,
            authority_kind=authority_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
            observed_at=at,
            effective_at=at,
            recorded_at=at,
            expires_at=expires_at,
            supersedes_event_id=supersedes_event_id,
            caused_by_event_id=None,
            idempotency_key=make_identity(
                "idempotency",
                {
                    "contract": ADOPTION_CONTRACT_VERSION,
                    "project_id": project_id,
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "effective_at": at,
                },
            ),
            conflict_group_id=None,
            payload={"state": state, **dict(payload)},
            privacy_scope="private_metadata",
            authority_digest=authority.authority_digest,
        )
        preview = self.store.preview_event(authority=authority, event=event)
        receipt = self.store.commit_preview(
            authority=authority,
            preview=preview,
            lease_owner_id=self.owner_id,
            lease_token=self.lease_token,
        )
        self.events.append(
            {
                "event_id": receipt.event_id,
                "event_type": event_type,
                "entity_id": entity_id,
                "sequence": receipt.sequence,
            }
        )
        return receipt


def _seed_yunos(
    writer: AdoptionWriter,
    evidence: Mapping[str, Any],
    anchors: Mapping[str, SourceAnchor],
) -> dict[str, Any]:
    git = evidence["yunos_git"]
    database = evidence["main_database"]
    project = writer.append(
        project_id=YUNOS_PROJECT,
        entity_id=YUNOS_PROJECT,
        entity_type="project",
        event_type="project_declared",
        state="active",
        anchor=anchors["user"],
        payload={
            "name": "YunOS",
            "purpose": "Private local memory and personal operational state system",
            "focus": True,
            "priority": 1,
            "authoritative_baseline": "v8.3-unified-ingestion",
            "authoritative_main": "9a1794d8b997e559d33750ee046c6649683aa778",
            "authoritative_tag": "v8.3-unified-ingestion",
            "current_branch": git["branch"],
            "current_commit": git["head"],
            "current_version": "v9",
            "current_phase": "v9-F production adoption",
            "completed_nodes": ["v9.0", "v9-A", "v9-B", "v9-C", "v9-D", "v9-E"],
            "next_approved_step": "Complete v9-F final seal",
            "pending_conflicts": 13,
            "deferred": ["user final visual review may follow automated acceptance"],
            "out_of_scope": [
                "v8302",
                "Historical Reconstruction",
                "Historical Backfill",
                "G0",
            ],
            "prohibited_actions": [
                "push",
                "create-v9-G",
                "create-v9.1",
                "start-v10",
            ],
            "future_nodes_authorized": False,
        },
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="goal:v9-final-seal",
        entity_type="goal",
        event_type="goal_declared",
        state="active",
        anchor=anchors["user"],
        payload={
            "title": "Complete v9 production adoption and final seal",
            "priority": 1,
        },
    )
    for node in ("v9.0", "v9-A", "v9-B", "v9-C", "v9-D"):
        entity_id = f"work-item:{node.lower().replace('.', '-')}"
        declared = writer.append(
            project_id=YUNOS_PROJECT,
            entity_id=entity_id,
            entity_type="work_item",
            event_type="work_item_declared",
            state="in_progress",
            anchor=anchors["user"],
            payload={"title": f"{node} completion awaiting Git observation", "node": node},
        )
        writer.append(
            project_id=YUNOS_PROJECT,
            entity_id=entity_id,
            entity_type="work_item",
            event_type="work_item_completed",
            state="completed",
            anchor=anchors["git_yunos"],
            payload={"title": f"{node} completed", "node": node},
            authority_kind="deterministic_observation",
            actor_kind="system",
            actor_id="system:git-observer",
            supersedes_event_id=declared.event_id,
        )
    old_v9e = writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="work-item:v9-e-control-surface",
        entity_type="work_item",
        event_type="work_item_declared",
        state="in_progress",
        anchor=anchors["user"],
        payload={"title": "v9-E Operational Control Surface incomplete"},
    )
    new_v9e = writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="work-item:v9-e-control-surface",
        entity_type="work_item",
        event_type="work_item_observed",
        state="completed",
        anchor=anchors["git_yunos"],
        payload={
            "title": "v9-E Operational Control Surface completed",
            "evidence_head": git["head"],
            "superseded_state": "in_progress",
        },
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:git-observer",
        supersedes_event_id=old_v9e.event_id,
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="work-item:v9-f-production-adoption",
        entity_type="work_item",
        event_type="work_item_declared",
        state="in_progress",
        anchor=anchors["user"],
        payload={"title": "Complete controlled v9-F production adoption"},
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="work-item:v9-production-readonly-audit",
        entity_type="work_item",
        event_type="work_item_declared",
        state="in_progress",
        anchor=anchors["user"],
        payload={"title": "Run controlled read-only production state audit"},
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="work-item:v9-agent-boundary-check",
        entity_type="work_item",
        event_type="work_item_declared",
        state="in_progress",
        anchor=anchors["user"],
        payload={"title": "Verify Agent cannot create or announce future nodes"},
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="blocker:v8-ingestion-pending-conflicts",
        entity_type="blocker",
        event_type="blocker_observed",
        state="open",
        anchor=anchors["db"],
        payload={
            "title": "13 preserved v8 ingestion conflicts remain pending",
            "count": 13,
            "blocks_v9_seal": False,
            "scope": "v8 historical provenance",
        },
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:database-observer",
    )
    writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="resource:yunos-main-db",
        entity_type="resource",
        event_type="resource_observed",
        state="available",
        anchor=anchors["db"],
        payload={
            "title": "YunOS main database",
            "size": database["size"],
            "sha256": database["sha256"],
            "integrity_check": database["integrity_check"],
            "foreign_key_violations": database["foreign_key_violations"],
        },
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:database-observer",
    )
    expired = writer.append(
        project_id=YUNOS_PROJECT,
        entity_id="goal:v9-short-lived-focus-check",
        entity_type="goal",
        event_type="goal_declared",
        state="active",
        anchor=anchors["user"],
        payload={
            "title": "Short-lived focus freshness acceptance",
            "focus": True,
            "priority": 0,
            "reconfirmation_required": True,
        },
        expires_at=_timestamp(writer.cursor + timedelta(seconds=3)),
    )
    authority = writer.authorities[YUNOS_PROJECT]
    aging = writer.store.set_freshness(
        entity_id="goal:v9-short-lived-focus-check",
        freshness="aging",
        authority=authority,
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
        recorded_at=writer.next_time(),
    )
    stale = writer.store.set_freshness(
        entity_id="goal:v9-short-lived-focus-check",
        freshness="stale",
        authority=authority,
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
        recorded_at=writer.next_time(),
    )
    return {
        "project_event_id": project.event_id,
        "superseded_event_id": old_v9e.event_id,
        "replacement_event_id": new_v9e.event_id,
        "expired_focus_event_id": expired.event_id,
        "freshness_path": [aging["freshness"], stale["freshness"]],
    }


def _seed_english(
    writer: AdoptionWriter,
    evidence: Mapping[str, Any],
    anchors: Mapping[str, SourceAnchor],
) -> dict[str, Any]:
    git = evidence["english_git"]
    declared = writer.append(
        project_id=ENGLISH_PROJECT,
        entity_id=ENGLISH_PROJECT,
        entity_type="project",
        event_type="project_declared",
        state="active",
        anchor=anchors["user"],
        payload={
            "name": "Ayun English Core",
            "purpose": "Core vocabulary review system for Shaanxi vocational-to-undergraduate English exams",
            "focus": False,
            "priority": 2,
            "authoritative_baseline": git["head"],
            "current_version": "v0.1.1",
            "current_phase": "local migration work observed, not adjudicated",
            "external_repository_policy": "read_only_observation_only",
            "source_repository_modified_by_yunos": False,
        },
    )
    declared = writer.append(
        project_id=ENGLISH_PROJECT,
        entity_id="work-item:english-v011-baseline",
        entity_type="work_item",
        event_type="work_item_declared",
        state="in_progress",
        anchor=anchors["user"],
        payload={"title": "v0.1.1 migration baseline awaiting Git observation"},
    )
    writer.append(
        project_id=ENGLISH_PROJECT,
        entity_id="work-item:english-v011-baseline",
        entity_type="work_item",
        event_type="work_item_observed",
        state="completed",
        anchor=anchors["git_english"],
        payload={
            "title": "v0.1.1 migration baseline established",
            "commit": git["head"],
        },
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:git-observer",
        supersedes_event_id=declared.event_id,
    )
    writer.append(
        project_id=ENGLISH_PROJECT,
        entity_id="goal:english-preserve-baseline",
        entity_type="goal",
        event_type="goal_declared",
        state="active",
        anchor=anchors["user"],
        payload={
            "title": "Preserve v0.1.1 while validating current local migration work",
            "priority": 2,
        },
    )
    writer.append(
        project_id=ENGLISH_PROJECT,
        entity_id="blocker:english-dirty-worktree",
        entity_type="blocker",
        event_type="blocker_observed",
        state="open",
        anchor=anchors["git_english"],
        payload={
            "title": "Local worktree contains uncommitted migration work",
            "tracked_change_count": git["tracked_change_count"],
            "staged_count": git["staged_count"],
            "untracked_count": git["untracked_count"],
            "requires_owner_adjudication": True,
        },
        authority_kind="deterministic_observation",
        actor_kind="system",
        actor_id="system:git-observer",
    )
    authority = writer.authorities[ENGLISH_PROJECT]
    candidate = writer.store.create_candidate(
        candidate_id="candidate:english-next-validate-migration",
        project_id=ENGLISH_PROJECT,
        entity_id="work-item:english-next-validate-migration",
        entity_type="work_item",
        candidate_type="next_step",
        candidate={
            "title": "Review and validate the current local migration diff before deciding its release path",
            "trust": "candidate_not_fact",
        },
        confidence_ppm=900_000,
        source_anchor=anchors["git_english"],
        created_at=writer.next_time(),
        expires_at=None,
        authority=authority,
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    return {
        "head": git["head"],
        "version": "v0.1.1",
        "candidate_id": candidate["candidate_id"],
        "source_repository_modified": False,
    }


def _contract(
    *,
    authority: AuthorityArtifact,
    delegation_id: str,
    work_item_id: str,
    intent: str,
    valid_from: str,
    expires_at: str,
) -> DelegationContract:
    return DelegationContract(
        delegation_id=delegation_id,
        project_id=YUNOS_PROJECT,
        work_item_id=work_item_id,
        intent=intent,
        read_scope=("repo:yunos", "database:operational-state"),
        write_scope=(work_item_id,),
        prohibited_actions=(
            "merge",
            "tag",
            "push",
            "declare_project_complete",
            "declare_project_sealed",
            "resolve_conflict",
            "start_next_version",
            "create_long_term_plan",
            "update_user_profile",
        ),
        tool_scope=("tool:readonly-audit",),
        risk="low",
        valid_from=valid_from,
        expires_at=expires_at,
        acceptance_criteria=(
            "Operational State integrity and foreign-key checks pass.",
            "No production source outside the Operational State database is modified.",
        ),
        rollback="Discard only the generated audit artifact and preserve append-only audit events.",
        version_scope=("v9-F",),
        commit_permission=False,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
        authority_digest=authority.authority_digest,
    )


def _run_delegations(
    writer: AdoptionWriter,
    *,
    anchor: SourceAnchor,
) -> dict[str, Any]:
    authority = writer.authorities[YUNOS_PROJECT]
    service = DelegationService(writer.store, source_anchor=anchor)
    valid_from = writer.next_time()
    expires_at = _timestamp(_parse_time(valid_from) + timedelta(minutes=30))
    safe_contract = _contract(
        authority=authority,
        delegation_id="delegation:v9-production-readonly-audit",
        work_item_id="work-item:v9-production-readonly-audit",
        intent="Run a bounded read-only integrity audit of the production Operational State database.",
        valid_from=valid_from,
        expires_at=expires_at,
    )
    service.create(
        contract=safe_contract,
        authority=authority,
        user_actor_id=USER_ACTOR,
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    for state in ("authorized", "queued", "running"):
        service.transition(
            delegation_id=safe_contract.delegation_id,
            next_state=state,
            authority=authority,
            actor_kind=("user" if state == "authorized" else "agent"),
            actor_id=(USER_ACTOR if state == "authorized" else "agent:v9-release"),
            at=writer.next_time(),
            lease_owner_id=writer.owner_id,
            lease_token=writer.lease_token,
            reason_code=f"production_acceptance_{state}",
        )
    integrity = writer.store.integrity_report()
    audit_artifact = {
        "contract_version": ADOPTION_CONTRACT_VERSION,
        "audit_kind": "controlled_readonly_operational_state_audit",
        "integrity": asdict(integrity),
        "status": writer.store.read_status(),
        "source_databases_modified": False,
    }
    receipt_digest = canonical_digest(audit_artifact)
    service.record_execution_result(
        delegation_id=safe_contract.delegation_id,
        outcome_id="outcome:v9-production-readonly-audit",
        runtime_run_id="runtime-run:v9-production-readonly-audit",
        result="completed",
        receipt_digest=receipt_digest,
        artifact_references=("artifact:v9-production-readonly-audit",),
        authority=authority,
        runtime_actor_id="runtime:v9-controlled-local",
        at=writer.next_time(),
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    verification_digest = canonical_digest(
        {
            "receipt_digest": receipt_digest,
            "integrity_check": integrity.integrity_check,
            "foreign_key_violations": integrity.foreign_key_violations,
            "schema_drift": integrity.schema_drift,
        }
    )
    service.verify_outcome(
        outcome_id="outcome:v9-production-readonly-audit",
        verification_digest=verification_digest,
        passed=(
            integrity.integrity_check == "ok"
            and integrity.foreign_key_violations == 0
            and not integrity.schema_drift
        ),
        authority=authority,
        verifier_id="system:v9-release-verifier",
        at=writer.next_time(),
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    accepted = service.accept_outcome(
        outcome_id="outcome:v9-production-readonly-audit",
        authority=authority,
        user_actor_id=USER_ACTOR,
        at=writer.next_time(),
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    applied = service.approve_and_apply_completion_candidate(
        candidate_id=accepted["state_candidate"]["candidate_id"],
        authority=authority,
        user_actor_id=USER_ACTOR,
        at=writer.next_time(),
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )

    overreach_contract = _contract(
        authority=authority,
        delegation_id="delegation:v9-agent-overreach",
        work_item_id="work-item:v9-agent-boundary-check",
        intent="Verify that an Agent cannot create future versions or expand release authority.",
        valid_from=writer.next_time(),
        expires_at=_timestamp(writer.cursor + timedelta(minutes=30)),
    )
    service.create(
        contract=overreach_contract,
        authority=authority,
        user_actor_id=USER_ACTOR,
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    for state in ("authorized", "queued", "running"):
        service.transition(
            delegation_id=overreach_contract.delegation_id,
            next_state=state,
            authority=authority,
            actor_kind=("user" if state == "authorized" else "agent"),
            actor_id=(USER_ACTOR if state == "authorized" else "agent:v9-boundary"),
            at=writer.next_time(),
            lease_owner_id=writer.owner_id,
            lease_token=writer.lease_token,
            reason_code=f"overreach_acceptance_{state}",
        )
    project_before = writer.store.get_projection(YUNOS_PROJECT)[
        "projection_digest"
    ]
    blocked = service.enforce_action(
        delegation_id=overreach_contract.delegation_id,
        proposed_action={"action": "create_node", "version": "v9-G"},
        authority=authority,
        agent_id="agent:v9-boundary",
        at=writer.next_time(),
        lease_owner_id=writer.owner_id,
        lease_token=writer.lease_token,
    )
    simulated = {
        "create_v9_g": overreach_contract.validate_action(
            {"action": "create_node", "version": "v9-G"},
            at=writer.next_time(),
        ),
        "expand_scope": overreach_contract.validate_action(
            {"action": "write", "target": "project:other"},
            at=writer.next_time(),
        ),
        "auto_merge": overreach_contract.validate_action(
            {"action": "merge"}, at=writer.next_time()
        ),
        "auto_tag": overreach_contract.validate_action(
            {"action": "tag"}, at=writer.next_time()
        ),
        "announce_v10": overreach_contract.validate_action(
            {"action": "start_next_version", "version": "v10"},
            at=writer.next_time(),
        ),
    }
    project_after = writer.store.get_projection(YUNOS_PROJECT)[
        "projection_digest"
    ]
    return {
        "safe_delegation": {
            "contract_digest": safe_contract.contract_digest,
            "receipt_digest": receipt_digest,
            "verification_digest": verification_digest,
            "outcome_status": "accepted",
            "work_item_status": applied["status"],
            "audit_artifact": audit_artifact,
        },
        "overreach": {
            "status": blocked["status"],
            "violations": blocked["violations"],
            "simulated_attempts": {
                key: list(value) for key, value in simulated.items()
            },
            "project_projection_unchanged": project_before == project_after,
            "delegation_state": writer.store.get_projection(
                overreach_contract.delegation_id
            )["state"],
        },
    }


def apply_adoption(
    database: Path,
    *,
    evidence: Mapping[str, Any],
    authority_evidence_digest: str,
    created_at: str,
    production: bool,
) -> dict[str, Any]:
    start = _parse_time(created_at)
    expires = _timestamp(start + timedelta(hours=2))
    if database.exists():
        raise OperationalStateError("operational_database_already_exists")
    initialize_operational_database(
        database,
        created_at=created_at,
        allow_production=production,
    )
    store = OperationalStateStore(
        database, allow_production_write=production
    )
    if any(store.read_status()["counts"].values()):
        raise OperationalStateError("production_adoption_nonempty_database")
    owner = "owner:v9-production-adoption"
    token = secrets.token_urlsafe(32)
    authorities = {
        project_id: _authority(
            project_id,
            issued_at=created_at,
            expires_at=expires,
        )
        for project_id in (YUNOS_PROJECT, ENGLISH_PROJECT)
    }
    anchors = {
        "user": _anchor(
            "anchor:v9-authorized-user-task",
            source_kind="user_declaration",
            locator="user-authority:yunos-v9-personal-operational-state",
            evidence_digest=authority_evidence_digest,
            observed_at=created_at,
        ),
        "git_yunos": _anchor(
            "anchor:v9-yunos-git-observation",
            source_kind="git",
            locator="repo:yunos",
            evidence_digest=evidence["yunos_git"]["evidence_digest"],
            observed_at=evidence["observed_at"],
        ),
        "git_english": _anchor(
            "anchor:v9-english-git-observation",
            source_kind="git",
            locator="repo:ayun-english-core",
            evidence_digest=evidence["english_git"]["evidence_digest"],
            observed_at=evidence["observed_at"],
        ),
        "db": _anchor(
            "anchor:v9-main-database-observation",
            source_kind="database",
            locator="database:yunos-main",
            evidence_digest=evidence["main_database"]["evidence_digest"],
            observed_at=evidence["observed_at"],
        ),
    }
    lease = store.acquire_writer_lease(
        owner_id=owner,
        token=token,
        acquired_at=created_at,
        expires_at=expires,
    )
    writer = AdoptionWriter(
        store,
        owner_id=owner,
        lease_token=token,
        authorities=authorities,
        start=start,
    )
    try:
        yunos = _seed_yunos(writer, evidence, anchors)
        english = _seed_english(writer, evidence, anchors)
        delegation = _run_delegations(writer, anchor=anchors["user"])
        snapshot = store.create_snapshot(
            created_at=writer.next_time(),
            authority=authorities[YUNOS_PROJECT],
            lease_owner_id=owner,
            lease_token=token,
        )
    finally:
        store.release_writer_lease(
            owner_id=owner,
            token=token,
            released_at=writer.next_time(),
        )
    context = OperationalStateContextService(database)
    now = context.now_package(budget_profile="Large")
    yunos_resume = context.resume_package(
        YUNOS_PROJECT, budget_profile="Large"
    )
    english_resume = context.resume_package(
        ENGLISH_PROJECT, budget_profile="Large"
    )
    status = store.read_status()
    report = {
        "contract_version": ADOPTION_CONTRACT_VERSION,
        "database_kind": status["database_kind"],
        "path_kind": status["path_kind"],
        "writer_default_enabled": status["writer_default_enabled"],
        "active_writer_lease": status["lease"] is not None,
        "schema_version": status["schema_version"],
        "schema_digest": status["schema_digest"],
        "counts": status["counts"],
        "integrity": asdict(store.integrity_report()),
        "lease_token_digest": lease.token_digest,
        "evidence_digest": canonical_digest(evidence),
        "projects": {"yunos": yunos, "ayun_english_core": english},
        "delegation": delegation,
        "snapshot": snapshot,
        "packages": {
            "now_digest": now["package_digest"],
            "yunos_resume_digest": yunos_resume["package_digest"],
            "english_resume_digest": english_resume["package_digest"],
            "current_focus": now["current_focus"],
        },
        "source_repository_modified": False,
        "full_history_backfill": False,
        "future_nodes_created": False,
    }
    return {**report, "report_digest": canonical_digest(report)}


def reconcile_adoption(
    database: Path,
    *,
    evidence: Mapping[str, Any],
    authority_evidence_digest: str,
    recorded_at: str,
    production: bool,
) -> dict[str, Any]:
    """Supersede declaration-only completion projections after real evidence.

    Early v9-F rehearsals retained the lower-ranked deterministic completion
    events but did not explicitly supersede their user declaration events.
    This idempotent reconciliation keeps both events and appends only the
    missing authoritative supersession.
    """
    if not database.is_file():
        raise OperationalStateError("operational_database_missing")
    if canonical_digest(evidence) == "":
        raise OperationalStateError("production_adoption_evidence_invalid")
    start = _parse_time(recorded_at)
    expires = _timestamp(start + timedelta(minutes=20))
    store = OperationalStateStore(
        database, allow_production_write=production
    )
    owner = "owner:v9-production-adoption-reconciliation"
    token = secrets.token_urlsafe(32)
    authorities = {
        YUNOS_PROJECT: _authority(
            YUNOS_PROJECT,
            issued_at=recorded_at,
            expires_at=expires,
        ),
        ENGLISH_PROJECT: _authority(
            ENGLISH_PROJECT,
            issued_at=recorded_at,
            expires_at=expires,
        ),
    }
    anchors = {
        YUNOS_PROJECT: _anchor(
            "anchor:v9-yunos-completion-reconciliation",
            source_kind="git",
            locator="repo:yunos",
            evidence_digest=evidence["yunos_git"]["evidence_digest"],
            observed_at=evidence["observed_at"],
        ),
        ENGLISH_PROJECT: _anchor(
            "anchor:v9-english-completion-reconciliation",
            source_kind="git",
            locator="repo:ayun-english-core",
            evidence_digest=evidence["english_git"]["evidence_digest"],
            observed_at=evidence["observed_at"],
        ),
    }
    store.acquire_writer_lease(
        owner_id=owner,
        token=token,
        acquired_at=recorded_at,
        expires_at=expires,
    )
    writer = AdoptionWriter(
        store,
        owner_id=owner,
        lease_token=token,
        authorities=authorities,
        start=start,
    )
    targets = [
        (
            YUNOS_PROJECT,
            f"work-item:{node.lower().replace('.', '-')}",
            f"{node} completed",
            {"node": node},
        )
        for node in ("v9.0", "v9-A", "v9-B", "v9-C", "v9-D")
    ]
    targets.append(
        (
            ENGLISH_PROJECT,
            "work-item:english-v011-baseline",
            "v0.1.1 migration baseline established",
            {"commit": evidence["english_git"]["head"]},
        )
    )
    reconciled: list[dict[str, Any]] = []
    try:
        for project_id, entity_id, title, extra in targets:
            projection = store.get_projection(entity_id)
            if projection["state"] == "completed":
                reconciled.append(
                    {
                        "entity_id": entity_id,
                        "status": "already_completed",
                        "projection_digest": projection["projection_digest"],
                    }
                )
                continue
            receipt = writer.append(
                project_id=project_id,
                entity_id=entity_id,
                entity_type="work_item",
                event_type="work_item_completed",
                state="completed",
                anchor=anchors[project_id],
                payload={"title": title, **extra},
                authority_kind="deterministic_observation",
                actor_kind="system",
                actor_id="system:git-observer",
                supersedes_event_id=projection["winner_events"]["state"],
            )
            reconciled.append(
                {
                    "entity_id": entity_id,
                    "status": "reconciled",
                    "event_id": receipt.event_id,
                }
            )
        snapshot = store.create_snapshot(
            created_at=writer.next_time(),
            authority=authorities[YUNOS_PROJECT],
            lease_owner_id=owner,
            lease_token=token,
        )
    finally:
        store.release_writer_lease(
            owner_id=owner,
            token=token,
            released_at=writer.next_time(),
        )
    status = store.read_status()
    report = {
        "contract_version": ADOPTION_CONTRACT_VERSION,
        "status": "adoption_reconciled",
        "reconciled": reconciled,
        "snapshot": snapshot,
        "writer_default_enabled": status["writer_default_enabled"],
        "active_writer_lease": status["lease"] is not None,
        "integrity": asdict(store.integrity_report()),
        "authority_evidence_digest": authority_evidence_digest,
        "evidence_digest": canonical_digest(evidence),
        "source_repository_modified": False,
    }
    return {**report, "report_digest": canonical_digest(report)}


def finalize_release(
    database: Path,
    *,
    yunos_repository: Path,
    tag_name: str,
    authority_evidence_digest: str,
    recorded_at: str,
    production: bool,
) -> dict[str, Any]:
    if not database.is_file():
        raise OperationalStateError("operational_database_missing")
    tag_type = _run_git(
        yunos_repository, "cat-file", "-t", f"refs/tags/{tag_name}"
    )
    if tag_type != "tag":
        raise OperationalStateError("final_release_tag_not_annotated")
    tag_object = _run_git(
        yunos_repository, "rev-parse", f"refs/tags/{tag_name}"
    )
    peeled = _run_git(
        yunos_repository, "rev-parse", f"refs/tags/{tag_name}^{{}}"
    )
    head = _run_git(yunos_repository, "rev-parse", "HEAD")
    branch = _run_git(yunos_repository, "branch", "--show-current")
    if head != peeled or branch != "main":
        raise OperationalStateError("final_release_git_target_mismatch")
    message = _run_git(
        yunos_repository,
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag_name}",
    )
    tag_message_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    source_digest = canonical_digest(
        {
            "head": head,
            "branch": branch,
            "tag": tag_name,
            "tag_object": tag_object,
            "peeled": peeled,
            "tag_message_digest": tag_message_digest,
        }
    )
    store = OperationalStateStore(
        database, allow_production_write=production
    )
    owner = "owner:v9-final-release-observation"
    token = secrets.token_urlsafe(32)
    start = _parse_time(recorded_at)
    expires = _timestamp(start + timedelta(minutes=20))
    authority = _authority(
        YUNOS_PROJECT,
        issued_at=recorded_at,
        expires_at=expires,
    )
    lease = store.acquire_writer_lease(
        owner_id=owner,
        token=token,
        acquired_at=recorded_at,
        expires_at=expires,
    )
    anchor = _anchor(
        "anchor:v9-final-release-git-tag",
        source_kind="git",
        locator=f"repo:yunos:tag:{tag_name}",
        evidence_digest=source_digest,
        observed_at=recorded_at,
    )
    writer = AdoptionWriter(
        store,
        owner_id=owner,
        lease_token=token,
        authorities={YUNOS_PROJECT: authority},
        start=start,
    )
    previous = store.get_projection(YUNOS_PROJECT)
    payload = dict(previous["fields"])
    payload.update(
        {
            "authoritative_main": head,
            "authoritative_tag": tag_name,
            "authoritative_tag_object": tag_object,
            "tag_message_digest": tag_message_digest,
            "current_branch": "main",
            "current_commit": head,
            "current_version": "v9",
            "current_phase": "v9-F sealed",
            "completed_nodes": [
                "v9.0",
                "v9-A",
                "v9-B",
                "v9-C",
                "v9-D",
                "v9-E",
                "v9-F",
            ],
            "next_approved_step": None,
            "future_nodes_authorized": False,
        }
    )
    try:
        receipt = writer.append(
            project_id=YUNOS_PROJECT,
            entity_id=YUNOS_PROJECT,
            entity_type="project",
            event_type="project_observed",
            state="active",
            anchor=anchor,
            payload=payload,
            authority_kind="deterministic_observation",
            actor_kind="system",
            actor_id="system:git-tag-observer",
            supersedes_event_id=previous["winner_events"]["state"],
        )
        snapshot = store.create_snapshot(
            created_at=writer.next_time(),
            authority=authority,
            lease_owner_id=owner,
            lease_token=token,
        )
    finally:
        store.release_writer_lease(
            owner_id=owner,
            token=token,
            released_at=writer.next_time(),
        )
    resume = OperationalStateContextService(database).resume_package(
        YUNOS_PROJECT, budget_profile="Large"
    )
    report = {
        "contract_version": ADOPTION_CONTRACT_VERSION,
        "status": "final_release_observed",
        "head": head,
        "branch": branch,
        "tag": tag_name,
        "tag_object": tag_object,
        "peeled_target": peeled,
        "tag_message_digest": tag_message_digest,
        "event_id": receipt.event_id,
        "snapshot": snapshot,
        "resume_digest": resume["package_digest"],
        "active_writer_lease": store.read_status()["lease"] is not None,
        "lease_token_digest": lease.token_digest,
        "authority_evidence_digest": authority_evidence_digest,
    }
    return {**report, "report_digest": canonical_digest(report)}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Controlled YunOS v9 production adoption and finalization."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    adopt = sub.add_parser("adopt")
    adopt.add_argument("--db", required=True, type=Path)
    adopt.add_argument("--yunos-repository", required=True, type=Path)
    adopt.add_argument("--english-repository", required=True, type=Path)
    adopt.add_argument("--authority-evidence-digest", required=True)
    adopt.add_argument("--created-at", required=True)
    adopt.add_argument("--output", required=True, type=Path)
    adopt.add_argument("--production", action="store_true")
    adopt.add_argument("--execute", action="store_true")

    reconcile = sub.add_parser("reconcile-adoption")
    reconcile.add_argument("--db", required=True, type=Path)
    reconcile.add_argument("--yunos-repository", required=True, type=Path)
    reconcile.add_argument("--english-repository", required=True, type=Path)
    reconcile.add_argument("--authority-evidence-digest", required=True)
    reconcile.add_argument("--recorded-at", required=True)
    reconcile.add_argument("--output", required=True, type=Path)
    reconcile.add_argument("--production", action="store_true")
    reconcile.add_argument("--execute", action="store_true")

    final = sub.add_parser("finalize-release")
    final.add_argument("--db", required=True, type=Path)
    final.add_argument("--yunos-repository", required=True, type=Path)
    final.add_argument("--tag", required=True)
    final.add_argument("--authority-evidence-digest", required=True)
    final.add_argument("--recorded-at", required=True)
    final.add_argument("--output", required=True, type=Path)
    final.add_argument("--production", action="store_true")
    final.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = args.db.expanduser().resolve()
        if not args.execute:
            raise OperationalStateError("production_adoption_execute_required")
        if args.production != is_production_operational_path(database):
            raise OperationalStateError("production_adoption_path_mode_mismatch")
        if args.production and os.environ.get(
            "YUNOS_OPERATIONAL_STATE_PRODUCTION_WRITE", ""
        ) != "1":
            raise OperationalStateError("production_writer_disabled")
        if args.command in {"adopt", "reconcile-adoption"}:
            observed_at = (
                args.created_at
                if args.command == "adopt"
                else args.recorded_at
            )
            evidence = collect_real_evidence(
                yunos_repository=args.yunos_repository.resolve(),
                english_repository=args.english_repository.resolve(),
                observed_at=observed_at,
            )
            if args.command == "adopt":
                report = apply_adoption(
                    database,
                    evidence=evidence,
                    authority_evidence_digest=args.authority_evidence_digest,
                    created_at=args.created_at,
                    production=args.production,
                )
            else:
                report = reconcile_adoption(
                    database,
                    evidence=evidence,
                    authority_evidence_digest=args.authority_evidence_digest,
                    recorded_at=args.recorded_at,
                    production=args.production,
                )
            result = {**report, "evidence": evidence}
        else:
            result = finalize_release(
                database,
                yunos_repository=args.yunos_repository.resolve(),
                tag_name=args.tag,
                authority_evidence_digest=args.authority_evidence_digest,
                recorded_at=args.recorded_at,
                production=args.production,
            )
        _write_json(args.output.resolve(), result)
        print(canonical_json(result))
        return 0
    except OperationalStateError as exc:
        print(
            canonical_json(
                {
                    "status": "error",
                    "reason_code": exc.reason_code,
                    "production_writer_default_enabled": False,
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
