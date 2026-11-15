from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from memory.db_path_policy import DEFAULT_OPERATIONAL_STATE_PATH
from memory.evidence_selection import (
    ESTIMATOR_PROFILE,
    EvidenceSelectionValidationError,
    estimate_tokens,
    normalize_policy,
)
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    utc_now,
    validate_identity,
)
from memory.operational_state_store import OperationalStateStore


CONTEXT_CONTRACT_VERSION = "yunos-operational-state-context-1"
NOW_PACKAGE_VERSION = "yunos-now-package-1"
RESUME_PACKAGE_VERSION = "yunos-project-resume-package-1"
LIVE_CACHE_CLASSIFICATION = "private_operational_state_live"

_AUTHORITY_PRIORITY = {
    "user": 0,
    "accepted_outcome": 1,
    "verified_runtime": 2,
    "deterministic_observation": 3,
    "system_reconciliation": 4,
    "candidate_only": 5,
}


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[dict[str, Any], ...]
    omitted: tuple[dict[str, Any], ...]
    budget: dict[str, Any]


def _private_live_envelope(as_of: str) -> dict[str, Any]:
    return {
        "private_state": True,
        "cache_control": "no-store",
        "cache_classification": LIVE_CACHE_CLASSIFICATION,
        "cached_at": None,
        "cache_stale": False,
        "source_state": "live",
        "as_of": normalize_timestamp(as_of),
    }


def _package_digest(payload: Mapping[str, Any]) -> str:
    return canonical_digest(payload)


def _projection_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = item.get("fields", {})
    priority = fields.get("priority", 100)
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 100
    return (
        normalized_priority,
        str(fields.get("name") or fields.get("title") or ""),
        item["entity_type"],
        item["entity_id"],
    )


def _candidate_public(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "project_id": candidate["project_id"],
        "entity_id": candidate["entity_id"],
        "entity_type": candidate["entity_type"],
        "candidate_type": candidate["candidate_type"],
        "candidate": candidate["candidate"],
        "candidate_digest": candidate["candidate_digest"],
        "confidence_ppm": candidate["confidence_ppm"],
        "source_anchor_id": candidate["source_anchor_id"],
        "status": candidate["status"],
        "expires_at": candidate["expires_at"],
        "created_at": candidate["created_at"],
        "trust": "candidate_not_fact",
    }


def _selection_entry(
    *,
    section: str,
    identity: str,
    value: Mapping[str, Any],
    priority: tuple[Any, ...],
) -> dict[str, Any]:
    materialized = dict(value)
    digest = canonical_digest(materialized)
    return {
        "section": section,
        "identity": identity,
        "value": materialized,
        "priority": list(priority),
        "content_digest": digest,
    }


def _select_with_v7_budget(
    entries: Iterable[dict[str, Any]],
    *,
    budget_profile: str,
) -> SelectionResult:
    try:
        policy = normalize_policy(budget_profile)
    except EvidenceSelectionValidationError:
        raise OperationalStateError("invalid_context_budget") from None
    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    characters = 0
    tokens = 0
    def sortable_priority(entry: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(
            (0, int(value))
            if isinstance(value, int) and not isinstance(value, bool)
            else (1, str(value))
            for value in entry["priority"]
        )

    for entry in sorted(
        entries,
        key=lambda item: (
            sortable_priority(item),
            item["section"],
            item["identity"],
        ),
    ):
        digest = entry["content_digest"]
        if digest in seen:
            omitted.append(
                {
                    "section": entry["section"],
                    "identity": entry["identity"],
                    "reason_code": "duplicate",
                    "duplicate_of": seen[digest],
                }
            )
            continue
        encoded = canonical_json(entry["value"])
        cost_chars = len(encoded)
        cost_tokens = estimate_tokens(encoded)
        if len(selected) >= policy["max_selected"]:
            omitted.append(
                {
                    "section": entry["section"],
                    "identity": entry["identity"],
                    "reason_code": "max_selected",
                }
            )
            continue
        if (
            characters + cost_chars > policy["character_budget"]
            or tokens + cost_tokens > policy["estimated_token_budget"]
        ):
            omitted.append(
                {
                    "section": entry["section"],
                    "identity": entry["identity"],
                    "reason_code": "budget_exceeded",
                }
            )
            continue
        seen[digest] = entry["identity"]
        selected.append(entry)
        characters += cost_chars
        tokens += cost_tokens
    reason_counts = Counter(item["reason_code"] for item in omitted)
    budget = {
        "policy_source": "v7.1-evidence-selection",
        "profile": policy["budget_profile"],
        "character_budget": policy["character_budget"],
        "characters_used": characters,
        "estimated_token_budget": policy["estimated_token_budget"],
        "estimated_tokens_used": tokens,
        "estimator_profile": ESTIMATOR_PROFILE,
        "max_selected": policy["max_selected"],
        "selected_count": len(selected),
        "omitted_count": len(omitted),
        "omission_reasons": dict(sorted(reason_counts.items())),
    }
    return SelectionResult(
        selected=tuple(selected),
        omitted=tuple(
            sorted(
                omitted,
                key=lambda item: (
                    item["section"],
                    item["identity"],
                    item["reason_code"],
                ),
            )
        ),
        budget=budget,
    )


def _event_maps(
    events: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for wrapped in events:
        item = {"sequence": wrapped["sequence"], **wrapped["event"]}
        by_id[item["event_id"]] = item
        ordered.append(item)
    return by_id, sorted(
        ordered, key=lambda item: (-item["sequence"], item["event_id"])
    )


def _projection_public(
    projection: Mapping[str, Any],
    event_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    state_event_id = projection["winner_events"]["state"]
    event = event_by_id.get(state_event_id, {})
    authority_kind = event.get("authority_kind", "system_reconciliation")
    return {
        "project_id": projection["project_id"],
        "entity_id": projection["entity_id"],
        "entity_type": projection["entity_type"],
        "state": projection["state"],
        "freshness": projection["freshness"],
        "fields": projection["fields"],
        "winner_events": projection["winner_events"],
        "projection_digest": projection["projection_digest"],
        "last_sequence": projection["last_sequence"],
        "authority_kind": authority_kind,
        "trust": (
            "approved_state"
            if authority_kind in {"user", "accepted_outcome"}
            else "observed_state"
        ),
    }


def _projection_priority(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _AUTHORITY_PRIORITY.get(item["authority_kind"], 99),
        *_projection_sort_key(item),
    )


class OperationalStateContextService:
    def __init__(
        self,
        database: str | Path = DEFAULT_OPERATIONAL_STATE_PATH,
    ) -> None:
        self.database = Path(database).resolve()

    @property
    def available(self) -> bool:
        return self.database.is_file()

    def _store(self) -> OperationalStateStore:
        if not self.available:
            raise OperationalStateError("operational_state_unavailable")
        return OperationalStateStore(self.database)

    def status_summary(self) -> dict[str, Any]:
        if not self.available:
            payload = {
                "contract_version": CONTEXT_CONTRACT_VERSION,
                "status": "unavailable",
                "reason_code": "operational_state_database_missing",
                "database_kind": "operational_state",
                "readonly": True,
                "writer_available": False,
                "path_disclosed": False,
            }
            return {**payload, "status_digest": _package_digest(payload)}
        status = self._store().read_status()
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "status": "ready",
            "reason_code": "operational_state_ready",
            "database_kind": status["database_kind"],
            "path_kind": status["path_kind"],
            "schema_version": status["schema_version"],
            "schema_digest": status["schema_digest"],
            "projection_version": status["projection_version"],
            "writer_default_enabled": status["writer_default_enabled"],
            "writer_available": False,
            "readonly": True,
            "counts": status["counts"],
            "active_writer_lease": status["lease"] is not None,
            "path_disclosed": False,
        }
        return {**payload, "status_digest": _package_digest(payload)}

    def _state(
        self,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        store = self._store()
        projections = store.list_projections(project_id=project_id)
        events = store.list_events(project_id=project_id)
        event_by_id, recent_events = _event_maps(events)
        public_projections = [
            _projection_public(item, event_by_id) for item in projections
        ]
        return {
            "store": store,
            "projections": sorted(
                public_projections, key=_projection_sort_key
            ),
            "events_by_id": event_by_id,
            "recent_events": recent_events,
            "candidates": store.list_candidates(
                project_id=project_id, status="pending"
            ),
            "conflicts": store.list_conflicts(
                project_id=project_id, status="pending"
            ),
            "evidence": store.list_source_evidence(project_id=project_id),
        }

    def project_list(self) -> dict[str, Any]:
        state = self._state()
        projects = [
            item
            for item in state["projections"]
            if item["entity_type"] == "project"
        ]
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "items": projects,
            "count": len(projects),
            "readonly": True,
        }
        return {**payload, "list_digest": _package_digest(payload)}

    def project_detail(self, project_id: str) -> dict[str, Any]:
        validate_identity(project_id, "invalid_project_id")
        state = self._state(project_id=project_id)
        project = next(
            (
                item
                for item in state["projections"]
                if item["entity_id"] == project_id
                and item["entity_type"] == "project"
            ),
            None,
        )
        if project is None:
            raise OperationalStateError("operational_project_not_found")
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "project": project,
            "entity_count": len(state["projections"]),
            "pending_candidate_count": len(state["candidates"]),
            "pending_conflict_count": len(state["conflicts"]),
            "readonly": True,
        }
        return {**payload, "detail_digest": _package_digest(payload)}

    def now_package(
        self,
        *,
        budget_profile: str = "Medium",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(as_of or utc_now())
        state = self._state()
        projections = state["projections"]
        candidates = state["candidates"]
        conflicts = state["conflicts"]
        projects = [
            item
            for item in projections
            if item["entity_type"] == "project"
            and item["state"] in {"active", "paused"}
        ]
        projects.sort(
            key=lambda item: (
                not bool(item["fields"].get("focus")),
                *_projection_sort_key(item),
            )
        )
        explicit_focus = [
            item
            for item in projections
            if item["fields"].get("focus") is True
            and item["state"]
            in {"active", "in_progress", "running", "paused", "waiting"}
            and item["freshness"] in {"fresh", "aging"}
        ]
        explicit_focus.sort(key=_projection_priority)
        eligible_projects = [
            item
            for item in projects
            if item["freshness"] in {"fresh", "aging"}
        ]
        focus = (
            explicit_focus[0]
            if explicit_focus
            else (eligible_projects[0] if eligible_projects else None)
        )

        entries: list[dict[str, Any]] = []
        pre_omitted: list[dict[str, Any]] = []

        def add_projection(section: str, item: Mapping[str, Any]) -> None:
            entries.append(
                _selection_entry(
                    section=section,
                    identity=item["entity_id"],
                    value=item,
                    priority=_projection_priority(item),
                )
            )

        if focus is not None:
            entries.append(
                _selection_entry(
                    section="current_focus",
                    identity=f"focus:{focus['entity_id']}",
                    value={
                        "project_id": focus["project_id"],
                        "entity_id": focus["entity_id"],
                        "state": focus["state"],
                        "freshness": focus["freshness"],
                        "name": focus["fields"].get("name"),
                        "projection_digest": focus["projection_digest"],
                        "selection_reason": (
                            "explicit_focus"
                            if focus["fields"].get("focus")
                            else "highest_ranked_active_project"
                        ),
                    },
                    priority=(0, "current-focus"),
                )
            )
        for item in projects[:3]:
            add_projection("active_projects", item)
        for item in projects[3:]:
            pre_omitted.append(
                {
                    "section": "active_projects",
                    "identity": item["entity_id"],
                    "reason_code": "section_cap",
                }
            )
        for item in projections:
            entity_type = item["entity_type"]
            entity_state = item["state"]
            if entity_type == "goal" and entity_state in {
                "approved",
                "active",
                "paused",
            }:
                add_projection("active_goals", item)
            if entity_type == "blocker" and entity_state in {
                "open",
                "mitigated",
            }:
                add_projection("blockers", item)
            if (
                entity_type == "work_item"
                and entity_state in {"blocked", "waiting"}
            ) or (
                entity_type == "delegation" and entity_state == "waiting"
            ):
                add_projection("waiting", item)
            if entity_type == "delegation" and entity_state not in {
                "accepted",
                "rejected",
                "cancelled",
            }:
                add_projection("delegations", item)
            if entity_type == "decision" and entity_state == "candidate":
                add_projection("pending_decisions", item)
            if item["freshness"] in {
                "stale",
                "expired",
                "unverifiable",
            }:
                entries.append(
                    _selection_entry(
                        section="stale",
                        identity=f"stale:{item['entity_id']}",
                        value={
                            "entity_id": item["entity_id"],
                            "entity_type": item["entity_type"],
                            "project_id": item["project_id"],
                            "state": item["state"],
                            "freshness": item["freshness"],
                            "projection_digest": item[
                                "projection_digest"
                            ],
                            "visibility": "explicit_non_fresh_state",
                        },
                        priority=(-1, item["entity_id"]),
                    )
                )
            if entity_type == "resource":
                add_projection("resources", item)
            if entity_type == "commitment" and (
                item["fields"].get("memory_kind") == "constraint"
                or item["fields"].get("constraint") is True
            ):
                add_projection("constraints", item)

        for conflict in conflicts:
            entries.append(
                _selection_entry(
                    section="conflicts",
                    identity=conflict["conflict_group_id"],
                    value=conflict,
                    priority=(0, conflict["conflict_group_id"]),
                )
            )
        for candidate in candidates:
            section = (
                "pending_decisions"
                if candidate["candidate_type"] == "decision"
                else "candidates"
            )
            entries.append(
                _selection_entry(
                    section=section,
                    identity=candidate["candidate_id"],
                    value=_candidate_public(candidate),
                    priority=(9, candidate["candidate_id"]),
                )
            )
        for event in state["recent_events"][:12]:
            entries.append(
                _selection_entry(
                    section="recent_changes",
                    identity=event["event_id"],
                    value={
                        "sequence": event["sequence"],
                        "event_id": event["event_id"],
                        "project_id": event["project_id"],
                        "entity_id": event["entity_id"],
                        "entity_type": event["entity_type"],
                        "event_type": event["event_type"],
                        "authority_kind": event["authority_kind"],
                        "effective_at": event["effective_at"],
                        "recorded_at": event["recorded_at"],
                        "source_anchor_id": event["source_anchor"]["anchor_id"],
                        "payload_digest": event["payload_digest"],
                    },
                    priority=(
                        5,
                        -event["sequence"],
                        event["event_id"],
                    ),
                )
            )
        for event in state["recent_events"][12:]:
            pre_omitted.append(
                {
                    "section": "recent_changes",
                    "identity": event["event_id"],
                    "reason_code": "section_cap",
                }
            )
        for evidence in state["evidence"][:12]:
            entries.append(
                _selection_entry(
                    section="source_anchors",
                    identity=evidence["anchor_id"],
                    value=evidence,
                    priority=(
                        6,
                        -evidence["latest_sequence"],
                        evidence["anchor_id"],
                    ),
                )
            )
        for evidence in state["evidence"][12:]:
            pre_omitted.append(
                {
                    "section": "source_anchors",
                    "identity": evidence["anchor_id"],
                    "reason_code": "section_cap",
                }
            )

        selection = _select_with_v7_budget(
            entries, budget_profile=budget_profile
        )
        sections = {
            name: []
            for name in (
                "current_focus",
                "active_goals",
                "active_projects",
                "blockers",
                "waiting",
                "delegations",
                "pending_decisions",
                "recent_changes",
                "stale",
                "conflicts",
                "resources",
                "constraints",
                "candidates",
                "source_anchors",
            )
        }
        for entry in selection.selected:
            sections[entry["section"]].append(entry["value"])
        combined_omitted = sorted(
            [*selection.omitted, *pre_omitted],
            key=lambda item: (
                item["section"],
                item["identity"],
                item["reason_code"],
            ),
        )
        budget = dict(selection.budget)
        omission_counts = Counter(
            item["reason_code"] for item in combined_omitted
        )
        budget["omitted_count"] = len(combined_omitted)
        budget["omission_reasons"] = dict(sorted(omission_counts.items()))
        visibility_summary = {
            "stale": {
                "total": sum(
                    1
                    for entry in entries
                    if entry["section"] == "stale"
                ),
                "selected": len(sections["stale"]),
                "omitted": sum(
                    1
                    for item in selection.omitted
                    if item["section"] == "stale"
                ),
            },
            "conflicts": {
                "total": sum(
                    1
                    for entry in entries
                    if entry["section"] == "conflicts"
                ),
                "selected": len(sections["conflicts"]),
                "omitted": sum(
                    1
                    for item in selection.omitted
                    if item["section"] == "conflicts"
                ),
            },
            "candidates": {
                "total": len(candidates),
                "selected": len(sections["candidates"])
                + sum(
                    1
                    for item in sections["pending_decisions"]
                    if item.get("trust") == "candidate_not_fact"
                ),
                "omitted": sum(
                    1
                    for item in selection.omitted
                    if item["section"] in {
                        "candidates",
                        "pending_decisions",
                    }
                ),
            },
        }
        payload = {
            "package_version": NOW_PACKAGE_VERSION,
            **_private_live_envelope(timestamp),
            "selection_policy": {
                "direct_evidence_first": True,
                "approved_state_before_observed": True,
                "candidate_separate_from_fact": True,
                "deterministic_ordering": True,
                "source_backed": True,
            },
            **sections,
            "visibility_summary": visibility_summary,
            "omitted": combined_omitted,
            "budget": budget,
        }
        return {**payload, "package_digest": _package_digest(payload)}

    def resume_package(
        self,
        project_id: str,
        *,
        budget_profile: str = "Medium",
        as_of: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(project_id, "invalid_project_id")
        timestamp = normalize_timestamp(as_of or utc_now())
        state = self._state(project_id=project_id)
        project = next(
            (
                item
                for item in state["projections"]
                if item["entity_type"] == "project"
                and item["entity_id"] == project_id
            ),
            None,
        )
        if project is None:
            raise OperationalStateError("operational_project_not_found")

        entries: list[dict[str, Any]] = []
        completed_states = {
            "completed",
            "achieved",
            "accepted",
            "fulfilled",
            "resolved",
        }
        current_states = {
            "active",
            "in_progress",
            "blocked",
            "waiting",
            "paused",
            "running",
            "queued",
            "authorized",
        }
        non_current_freshness = {
            "stale",
            "expired",
            "superseded",
            "conflicted",
            "unverifiable",
        }
        for item in state["projections"]:
            if item["entity_id"] == project_id:
                continue
            section = None
            if item["state"] in completed_states:
                section = "completed"
            elif item["entity_type"] == "work_item" and item["state"] == "ready":
                section = (
                    "next_approved_steps"
                    if item["trust"] == "approved_state"
                    else "current_work"
                )
            elif item["entity_type"] == "decision":
                section = (
                    "approved_decisions"
                    if item["state"] == "approved"
                    else "pending_decisions"
                )
            elif item["entity_type"] == "blocker":
                section = "blockers"
            elif item["entity_type"] == "delegation":
                section = "delegations"
            elif item["entity_type"] == "outcome":
                section = "outcomes"
            elif item["state"] == "waiting":
                section = "waiting"
            elif (
                item["state"] in current_states
                and item["freshness"] not in non_current_freshness
            ):
                section = "current_work"
            if section is not None:
                entries.append(
                    _selection_entry(
                        section=section,
                        identity=item["entity_id"],
                        value=item,
                        priority=_projection_priority(item),
                    )
                )
            if item["freshness"] != "fresh":
                entries.append(
                    _selection_entry(
                        section="freshness",
                        identity=f"freshness:{item['entity_id']}",
                        value={
                            "entity_id": item["entity_id"],
                            "freshness": item["freshness"],
                            "projection_digest": item["projection_digest"],
                        },
                        priority=(-1, item["entity_id"]),
                    )
                )
        for candidate in state["candidates"]:
            entries.append(
                _selection_entry(
                    section="next_candidate_steps",
                    identity=candidate["candidate_id"],
                    value=_candidate_public(candidate),
                    priority=(3, candidate["candidate_id"]),
                )
            )
        for conflict in state["conflicts"]:
            entries.append(
                _selection_entry(
                    section="conflicts",
                    identity=conflict["conflict_group_id"],
                    value=conflict,
                    priority=(0, conflict["conflict_group_id"]),
                )
            )
        for index, evidence in enumerate(state["evidence"]):
            entries.append(
                _selection_entry(
                    section="source_anchors",
                    identity=evidence["anchor_id"],
                    value=evidence,
                    priority=(
                        -1 if index < 2 else 6,
                        -evidence["latest_sequence"],
                        evidence["anchor_id"],
                    ),
                )
            )
        for index, event in enumerate(state["recent_events"][:16]):
            entries.append(
                _selection_entry(
                    section="state_timeline",
                    identity=event["event_id"],
                    value={
                        "sequence": event["sequence"],
                        "event_id": event["event_id"],
                        "entity_id": event["entity_id"],
                        "entity_type": event["entity_type"],
                        "event_type": event["event_type"],
                        "effective_at": event["effective_at"],
                        "recorded_at": event["recorded_at"],
                        "authority_kind": event["authority_kind"],
                        "source_anchor_id": event["source_anchor"][
                            "anchor_id"
                        ],
                    },
                    priority=(
                        -2 if index == 0 else 5,
                        -event["sequence"],
                        event["event_id"],
                    ),
                )
            )
        selection = _select_with_v7_budget(
            entries, budget_profile=budget_profile
        )
        sections = {
            name: []
            for name in (
                "completed",
                "current_work",
                "next_approved_steps",
                "next_candidate_steps",
                "approved_decisions",
                "pending_decisions",
                "blockers",
                "waiting",
                "delegations",
                "outcomes",
                "freshness",
                "conflicts",
                "source_anchors",
                "state_timeline",
            )
        }
        for entry in selection.selected:
            sections[entry["section"]].append(entry["value"])
        fields = project["fields"]
        payload = {
            "package_version": RESUME_PACKAGE_VERSION,
            **_private_live_envelope(timestamp),
            "project_id": project_id,
            "project_purpose": fields.get("purpose"),
            "authoritative_baseline": fields.get("authoritative_baseline"),
            "current_version": fields.get("current_version"),
            "current_phase": fields.get("current_phase"),
            "completed_nodes": fields.get("completed_nodes", []),
            "next_approved_step": fields.get("next_approved_step"),
            "pending_conflicts": fields.get("pending_conflicts"),
            "future_nodes_authorized": fields.get(
                "future_nodes_authorized"
            ),
            "authoritative_main": fields.get("authoritative_main"),
            "authoritative_tag": fields.get("authoritative_tag"),
            "project_state": project["state"],
            "project_freshness": project["freshness"],
            **sections,
            "deferred": fields.get("deferred", []),
            "out_of_scope": fields.get("out_of_scope", []),
            "prohibited_actions": fields.get("prohibited_actions", []),
            "omitted": list(selection.omitted),
            "budget": selection.budget,
        }
        return {**payload, "package_digest": _package_digest(payload)}

    def delegation_overview(
        self, *, project_id: str | None = None
    ) -> dict[str, Any]:
        state = self._state(project_id=project_id)
        store = state["store"]
        outcomes_by_delegation: dict[str, list[dict[str, Any]]] = {}
        for outcome in store.list_outcomes():
            outcomes_by_delegation.setdefault(
                outcome["delegation_id"], []
            ).append(outcome)
        items = []
        for projection in state["projections"]:
            if projection["entity_type"] != "delegation":
                continue
            try:
                contract = store.get_delegation_contract(
                    projection["entity_id"]
                )
            except OperationalStateError as exc:
                if exc.reason_code != "delegation_contract_not_found":
                    raise
                contract = None
            contract_payload = contract["contract"] if contract else {}
            outcomes = sorted(
                outcomes_by_delegation.get(projection["entity_id"], []),
                key=lambda item: (
                    state["events_by_id"]
                    .get(item["current_event_id"], {})
                    .get("sequence", -1),
                    item["outcome_id"],
                ),
            )
            latest_outcome = outcomes[-1] if outcomes else None
            items.append(
                {
                    "delegation": projection,
                    "agent": projection["fields"].get("agent_id"),
                    "authority_boundary": (
                        {
                            "read_scope": contract_payload.get(
                                "read_scope", []
                            ),
                            "write_scope": contract_payload.get(
                                "write_scope", []
                            ),
                            "tool_scope": contract_payload.get(
                                "tool_scope", []
                            ),
                            "prohibited_actions": contract_payload.get(
                                "prohibited_actions", []
                            ),
                            "version_scope": contract_payload.get(
                                "version_scope", []
                            ),
                            "valid_from": contract["valid_from"],
                            "expires_at": contract["expires_at"],
                            "risk": contract_payload.get("risk"),
                            "commit_permission": contract_payload.get(
                                "commit_permission", False
                            ),
                            "merge_permission": contract_payload.get(
                                "merge_permission", False
                            ),
                            "tag_permission": contract_payload.get(
                                "tag_permission", False
                            ),
                            "push_permission": contract_payload.get(
                                "push_permission", False
                            ),
                            "next_node_permission": contract_payload.get(
                                "next_node_permission", False
                            ),
                            "authority_digest": contract_payload.get(
                                "authority_digest"
                            ),
                        }
                        if contract
                        else None
                    ),
                    "latest_activity_sequence": projection["last_sequence"],
                    "overreach": (
                        projection["fields"].get("reason_code")
                        == "agent_overreach_blocked"
                    ),
                    "latest_outcome": (
                        {
                            "outcome_id": latest_outcome["outcome_id"],
                            "status": latest_outcome["status"],
                            "runtime_run_id": latest_outcome[
                                "runtime_run_id"
                            ],
                            "verification_digest": latest_outcome[
                                "verification_digest"
                            ],
                            "verified": latest_outcome["status"]
                            in {"verified", "accepted"},
                            "accepted": latest_outcome["status"] == "accepted",
                            "artifact_references": latest_outcome[
                                "artifact_references"
                            ],
                        }
                        if latest_outcome
                        else None
                    ),
                }
            )
        items.sort(key=lambda item: item["delegation"]["entity_id"])
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "items": items,
            "count": len(items),
            "readonly": True,
        }
        return {**payload, "query_digest": _package_digest(payload)}

    def filtered_state(
        self,
        *,
        entity_type: str | None = None,
        state_name: str | None = None,
        freshness: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._state(project_id=project_id)
        items = [
            item
            for item in state["projections"]
            if (entity_type is None or item["entity_type"] == entity_type)
            and (state_name is None or item["state"] == state_name)
            and (freshness is None or item["freshness"] == freshness)
        ]
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "items": items,
            "count": len(items),
            "readonly": True,
        }
        return {**payload, "query_digest": _package_digest(payload)}

    def conflicts(self, project_id: str | None = None) -> dict[str, Any]:
        state = self._state(project_id=project_id)
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "items": state["conflicts"],
            "count": len(state["conflicts"]),
            "readonly": True,
        }
        return {**payload, "query_digest": _package_digest(payload)}

    def source_evidence(
        self, project_id: str | None = None
    ) -> dict[str, Any]:
        state = self._state(project_id=project_id)
        payload = {
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "items": state["evidence"],
            "count": len(state["evidence"]),
            "readonly": True,
        }
        return {**payload, "query_digest": _package_digest(payload)}


__all__ = [
    "CONTEXT_CONTRACT_VERSION",
    "LIVE_CACHE_CLASSIFICATION",
    "NOW_PACKAGE_VERSION",
    "RESUME_PACKAGE_VERSION",
    "OperationalStateContextService",
]
