from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from memory.cognitive_capability import (
    CapabilityProfile,
    CapabilityRegistry,
    CognitiveCapabilityError,
    build_suggested_capability_registry,
)
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_planning import (
    PlanEdge,
    PlanGraph,
    PlanNode,
    PlanRevision,
    PlanningError,
    capability_node_constraint_reasons,
    validate_initial_plan_states,
    validate_plan_state_transition,
)
from memory.cognitive_state_migration import verify_cognitive_state_schema
from memory.cognitive_state_store import CognitiveStateStore
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
)
from memory.operational_state_store import OperationalStateStore


_MAX_PLAN_BYTES = 1_048_576
_MAX_CAPABILITY_BYTES = 262_144
_CAPABILITY_RISKS = {"low", "medium", "high", "critical"}
_CAPABILITY_AVAILABILITY = {
    "available",
    "unavailable",
    "disabled",
    "handoff_only",
}
_CAPABILITY_EXECUTORS = {
    "native",
    "runtime",
    "codex",
    "external_agent",
    "local_broker",
}


def _decode_canonical_object(
    raw: str,
    *,
    maximum_bytes: int,
    reason_code: str,
) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum_bytes:
        raise OperationalStateError(reason_code)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise OperationalStateError(reason_code) from None
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise OperationalStateError(reason_code)
    return value


class CognitivePlanningStore:
    """Atomic Plan/Capability persistence in the existing Operational DB."""

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
        self.objective_store = CognitiveStateStore(
            self.path,
            trusted_clock=trusted_clock,
            allow_production_write=allow_production_write,
        )
        self._trusted_clock = trusted_clock or utc_now
        verify_cognitive_state_schema(self.path)

    def _trusted_at(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _validate_graph(value: PlanGraph | Mapping[str, Any]) -> PlanGraph:
        if isinstance(value, PlanGraph):
            return PlanGraph.from_dict(value.to_dict())
        if not isinstance(value, Mapping):
            raise OperationalStateError("plan_graph_must_be_object")
        try:
            return PlanGraph.from_dict(dict(value))
        except PlanningError as exc:
            raise OperationalStateError(exc.reason_code) from None

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = _decode_canonical_object(
            row["canonical_capability"],
            maximum_bytes=_MAX_CAPABILITY_BYTES,
            reason_code="capability_record_corrupt",
        )
        try:
            profile = CapabilityProfile.from_record(
                {
                    "canonical_capability": payload,
                    "capability_digest": row["capability_digest"],
                }
            )
        except CognitiveCapabilityError as exc:
            raise OperationalStateError(exc.reason_code) from None
        if (
            profile.capability_id != row["capability_id"]
            or profile.version != row["capability_version"]
            or profile.display_name != row["display_name"]
            or profile.risk_class != row["risk_class"]
            or int(profile.reversible) != row["reversible"]
            or profile.required_authority != row["required_authority"]
            or profile.executor_kind != row["executor_kind"]
            or profile.availability != row["availability"]
            or profile.timeout_ms != row["timeout_ms"]
            or profile.max_output_bytes != row["max_output_bytes"]
        ):
            raise OperationalStateError("capability_record_tampered")
        return {
            **profile.to_dict(),
            "capability_digest": profile.capability_digest,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _validate_code_owned_registry(
        registry: CapabilityRegistry,
    ) -> CapabilityRegistry:
        if not isinstance(registry, CapabilityRegistry):
            raise OperationalStateError("invalid_capability_registry")
        availability = {
            profile.capability_id: profile.availability
            for profile in registry.profiles
        }
        candidates: list[CapabilityRegistry] = []
        for builder in (
            build_suggested_capability_registry,
            build_bounded_execution_capability_registry,
        ):
            try:
                candidates.append(builder(availability))
            except CognitiveCapabilityError:
                continue
        for expected in candidates:
            if (
                registry.registry_digest == expected.registry_digest
                and registry.to_snapshot() == expected.to_snapshot()
            ):
                return expected
        raise OperationalStateError("untrusted_capability_registry")

    @classmethod
    def _load_code_owned_registry_on_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> tuple[CapabilityRegistry, list[dict[str, Any]]]:
        rows = connection.execute(
            "SELECT * FROM capability_registry "
            "ORDER BY capability_id,capability_version"
        ).fetchall()
        if not rows:
            raise OperationalStateError("capability_registry_uninstalled")
        records = [cls._profile_from_row(row) for row in rows]
        try:
            registry = CapabilityRegistry(
                CapabilityProfile.from_dict(
                    {
                        key: value
                        for key, value in record.items()
                        if key not in {"capability_digest", "created_at"}
                    }
                )
                for record in records
            )
        except CognitiveCapabilityError:
            raise OperationalStateError(
                "untrusted_capability_registry"
            ) from None
        validated = cls._validate_code_owned_registry(registry)
        if validated.to_snapshot() != registry.to_snapshot():
            raise OperationalStateError("untrusted_capability_registry")
        return validated, records

    def install_builtin_capabilities(
        self,
        *,
        registry: CapabilityRegistry,
        authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        validated = self._validate_code_owned_registry(registry)
        trusted_at = self._trusted_at()
        if not authority.issued_by.startswith("user:"):
            raise OperationalStateError(
                "capability_registry_requires_user_authority"
            )
        entity_id = "capability-registry:yunos-v10"
        authority.authorize_event(
            project_id="project:yunos",
            entity_id=entity_id,
            event_type="capability_registry_install",
            at=trusted_at,
        )
        with closing(self.operational_store._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational_store._authorize_auxiliary_write(
                    connection,
                    authority=authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id="project:yunos",
                    entity_id=entity_id,
                    operation="capability_registry_install",
                    at=trusted_at,
                    enforce_for_test=True,
                )
                existing_rows = connection.execute(
                    "SELECT * FROM capability_registry "
                    "ORDER BY capability_id,capability_version"
                ).fetchall()
                expected_keys = {
                    (item.capability_id, item.version)
                    for item in validated.profiles
                }
                existing_keys = {
                    (row["capability_id"], row["capability_version"])
                    for row in existing_rows
                }
                if existing_keys - expected_keys:
                    raise OperationalStateError("capability_registry_drift")
                existing = {
                    (row["capability_id"], row["capability_version"]): row
                    for row in existing_rows
                }
                inserted = 0
                for profile in validated.profiles:
                    key = (profile.capability_id, profile.version)
                    row = existing.get(key)
                    if row is not None:
                        record = self._profile_from_row(row)
                        if (
                            record["capability_digest"]
                            != profile.capability_digest
                            or canonical_json(profile.to_dict())
                            != row["canonical_capability"]
                        ):
                            raise OperationalStateError(
                                "capability_registry_drift"
                            )
                        continue
                    connection.execute(
                        "INSERT INTO capability_registry("
                        "capability_id,capability_version,display_name,"
                        "risk_class,reversible,required_authority,executor_kind,"
                        "availability,timeout_ms,max_output_bytes,"
                        "canonical_capability,capability_digest,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            profile.capability_id,
                            profile.version,
                            profile.display_name,
                            profile.risk_class,
                            int(profile.reversible),
                            profile.required_authority,
                            profile.executor_kind,
                            profile.availability,
                            profile.timeout_ms,
                            profile.max_output_bytes,
                            canonical_json(profile.to_dict()),
                            profile.capability_digest,
                            trusted_at,
                        ),
                    )
                    inserted += 1
                final_count = connection.execute(
                    "SELECT COUNT(*) FROM capability_registry"
                ).fetchone()[0]
                if final_count != len(validated.profiles):
                    raise OperationalStateError(
                        "capability_registry_partial"
                    )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise OperationalStateError(
                    "capability_registry_insert_conflict"
                ) from None
            except Exception:
                connection.rollback()
                raise
        return {
            "registry_digest": validated.registry_digest,
            "profile_count": len(validated.profiles),
            "inserted": inserted,
            "idempotent_replay": inserted == 0,
            "grants_authority": False,
            "execution_allowed": False,
        }

    def list_capabilities(
        self,
        *,
        risk_class: str | None = None,
        availability: str | None = None,
        executor_kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if risk_class is not None and risk_class not in _CAPABILITY_RISKS:
            raise OperationalStateError("invalid_capability_risk_filter")
        if (
            availability is not None
            and availability not in _CAPABILITY_AVAILABILITY
        ):
            raise OperationalStateError(
                "invalid_capability_availability_filter"
            )
        if (
            executor_kind is not None
            and executor_kind not in _CAPABILITY_EXECUTORS
        ):
            raise OperationalStateError(
                "invalid_capability_executor_filter"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise OperationalStateError("invalid_capability_limit")
        with closing(self.operational_store._connect()) as connection:
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM capability_registry"
                ).fetchone()[0]
                == 0
            ):
                return []
            _registry, records = self._load_code_owned_registry_on_connection(
                connection
            )
        return [
            record
            for record in records
            if (risk_class is None or record["risk_class"] == risk_class)
            and (
                availability is None
                or record["availability"] == availability
            )
            and (
                executor_kind is None
                or record["executor_kind"] == executor_kind
            )
        ][:limit]

    def load_capability_registry(self) -> CapabilityRegistry:
        with closing(self.operational_store._connect()) as connection:
            registry, _records = self._load_code_owned_registry_on_connection(
                connection
            )
        return registry

    @staticmethod
    def _validate_plan_event(
        graph: PlanGraph,
        objective: Mapping[str, Any],
        event: StateEvent,
    ) -> None:
        current_revision = graph.revisions[-1]
        expected_entity = make_identity(
            "workitem",
            {"plan_id": graph.plan_id, "version": graph.version},
        )
        expected_event_type = (
            "plan_created"
            if graph.version == 1
            else "plan_scope_expansion_approved"
            if current_revision.authority_impact == "requires_new_authority"
            else "plan_revised"
        )
        expected_payload = {
            "state": "proposed",
            "plan_id": graph.plan_id,
            "plan_version": graph.version,
            "plan_digest": graph.plan_digest,
            "objective_id": graph.objective_id,
            "objective_version": graph.objective_version,
            "objective_digest": graph.objective_digest,
            "authority_impact": current_revision.authority_impact,
        }
        anchors = [
            item
            for item in objective["contract"]["source_anchors"]
            if item["anchor_id"] == event.source_anchor.anchor_id
        ]
        if (
            event.project_id != objective["project_id"]
            or event.entity_id != expected_entity
            or event.entity_type != "work_item"
            or event.event_type != expected_event_type
            or event.source_kind != "declared"
            or event.authority_kind != "user"
            or event.actor_kind not in {"user", "agent", "runtime"}
            or event.canonical_payload != expected_payload
            or len(anchors) != 1
            or canonical_json(anchors[0])
            != canonical_json(event.source_anchor.to_dict())
        ):
            raise OperationalStateError("plan_event_binding_mismatch")

    @staticmethod
    def _validate_plan_capabilities(
        connection: sqlite3.Connection,
        graph: PlanGraph,
    ) -> None:
        requirements = {
            (
                item.capability_id,
                item.capability_version,
                item.capability_digest,
            )
            for node in graph.nodes
            for item in node.capability_requirements
        }
        registry_rows = connection.execute(
            "SELECT COUNT(*) FROM capability_registry"
        ).fetchone()[0]
        if not registry_rows:
            if requirements:
                raise OperationalStateError(
                    "capability_registry_uninstalled"
                )
            return
        registry, _records = (
            CognitivePlanningStore._load_code_owned_registry_on_connection(
                connection
            )
        )
        for node in graph.nodes:
            for requirement in node.capability_requirements:
                try:
                    profile = registry.lookup(
                        requirement.capability_id,
                        version=requirement.capability_version,
                        expected_digest=requirement.capability_digest,
                    )
                except CognitiveCapabilityError as exc:
                    reason = (
                        "plan_capability_drift"
                        if exc.reason_code
                        in {
                            "capability_profile_drift",
                            "invalid_capability_lookup_digest",
                        }
                        else "plan_capability_version_unavailable"
                    )
                    raise OperationalStateError(reason) from None
                violations = capability_node_constraint_reasons(
                    node,
                    profile,
                    include_availability=False,
                )
                if violations:
                    raise OperationalStateError(violations[0])

    @staticmethod
    def _graph_from_record(record: Mapping[str, Any]) -> PlanGraph:
        fields = (
            "plan_id",
            "objective_id",
            "objective_version",
            "objective_digest",
            "nodes",
            "edges",
            "revisions",
            "entry_node_ids",
            "terminal_node_ids",
            "plan_digest",
            "version",
        )
        try:
            return PlanGraph.from_dict(
                {field: record[field] for field in fields}
            )
        except (KeyError, PlanningError) as exc:
            reason = getattr(exc, "reason_code", "plan_graph_record_corrupt")
            raise OperationalStateError(reason) from None

    @staticmethod
    def _completes_human_gate(
        previous: PlanGraph,
        candidate: PlanGraph,
    ) -> bool:
        old = {item.node_id: item for item in previous.nodes}
        return any(
            node_id in old
            and old[node_id].node_kind == "human_gate"
            and old[node_id].state == "waiting_authority"
            and node.state == "completed"
            for node_id, node in (
                (item.node_id, item) for item in candidate.nodes
            )
        )

    def _plan_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        raw_graph = _decode_canonical_object(
            row["canonical_graph"],
            maximum_bytes=_MAX_PLAN_BYTES,
            reason_code="plan_graph_record_corrupt",
        )
        node_rows = connection.execute(
            "SELECT * FROM plan_nodes WHERE plan_id=? AND plan_version=? "
            "ORDER BY ordinal",
            (row["plan_id"], row["version"]),
        ).fetchall()
        if [item["ordinal"] for item in node_rows] != list(
            range(len(node_rows))
        ):
            raise OperationalStateError("plan_node_ordinal_drift")
        nodes: list[PlanNode] = []
        for node_row in node_rows:
            payload = _decode_canonical_object(
                node_row["canonical_node"],
                maximum_bytes=_MAX_PLAN_BYTES,
                reason_code="plan_node_record_corrupt",
            )
            try:
                node = PlanNode.from_dict(payload)
            except PlanningError as exc:
                raise OperationalStateError(exc.reason_code) from None
            if (
                canonical_digest(payload) != node_row["node_digest"]
                or node.node_id != node_row["node_id"]
                or node.node_kind != node_row["node_kind"]
                or node.risk != node_row["risk_class"]
                or int(node.reversible) != node_row["reversible"]
                or node.authority_requirement
                != node_row["authority_requirement"]
                or node.timeout_ms != node_row["timeout_ms"]
                or node.state != node_row["state"]
            ):
                raise OperationalStateError("plan_node_record_tampered")
            nodes.append(node)
        edge_rows = connection.execute(
            "SELECT * FROM plan_edges WHERE plan_id=? AND plan_version=? "
            "ORDER BY from_node_id,priority,edge_id,to_node_id",
            (row["plan_id"], row["version"]),
        ).fetchall()
        edges: list[PlanEdge] = []
        for edge_row in edge_rows:
            edge_payload = {
                "edge_id": edge_row["edge_id"],
                "from_node_id": edge_row["from_node_id"],
                "to_node_id": edge_row["to_node_id"],
                "edge_kind": edge_row["edge_kind"],
                "condition": (
                    json.loads(edge_row["condition_json"])
                    if edge_row["condition_json"] is not None
                    else None
                ),
                "priority": edge_row["priority"],
            }
            try:
                edge = PlanEdge.from_dict(edge_payload)
            except (PlanningError, json.JSONDecodeError) as exc:
                reason = getattr(exc, "reason_code", "plan_edge_record_corrupt")
                raise OperationalStateError(reason) from None
            if canonical_digest(edge.to_dict()) != edge_row["edge_digest"]:
                raise OperationalStateError("plan_edge_record_tampered")
            edges.append(edge)
        revision_rows = connection.execute(
            "SELECT * FROM plan_revisions WHERE plan_id=? AND to_version<=? "
            "ORDER BY to_version",
            (row["plan_id"], row["version"]),
        ).fetchall()
        revisions: list[PlanRevision] = []
        for revision_row in revision_rows:
            payload = _decode_canonical_object(
                revision_row["canonical_revision"],
                maximum_bytes=_MAX_PLAN_BYTES,
                reason_code="plan_revision_record_corrupt",
            )
            try:
                revision = PlanRevision.from_dict(payload)
            except PlanningError as exc:
                raise OperationalStateError(exc.reason_code) from None
            if (
                revision.revision_id != revision_row["revision_id"]
                or revision.plan_id != revision_row["plan_id"]
                or revision.from_version != revision_row["from_version"]
                or revision.to_version != revision_row["to_version"]
                or revision.change_reason != revision_row["change_reason"]
                or revision.authority_impact
                != revision_row["authority_impact"]
                or revision.previous_digest
                != revision_row["previous_digest"]
                or revision.plan_digest != revision_row["plan_digest"]
                or revision.revision_digest
                != revision_row["revision_digest"]
                or revision.created_at != revision_row["created_at"]
            ):
                raise OperationalStateError("plan_revision_record_tampered")
            revisions.append(revision)
        payload = dict(raw_graph)
        payload["nodes"] = [item.to_dict() for item in nodes]
        payload["edges"] = [item.to_dict() for item in edges]
        payload["revisions"] = [item.to_dict() for item in revisions]
        try:
            graph = PlanGraph.from_dict(payload)
        except PlanningError as exc:
            raise OperationalStateError(exc.reason_code) from None
        if (
            graph.plan_id != row["plan_id"]
            or graph.version != row["version"]
            or graph.objective_id != row["objective_id"]
            or graph.objective_version != row["objective_version"]
            or graph.objective_digest != row["objective_digest"]
            or graph.plan_digest != row["plan_digest"]
            or canonical_json(graph.to_dict()) != row["canonical_graph"]
        ):
            raise OperationalStateError("plan_graph_record_tampered")
        objective = self.objective_store.get_objective(
            graph.objective_id,
            graph.objective_version,
        )
        if (
            objective is None
            or objective["canonical_digest"] != graph.objective_digest
        ):
            raise OperationalStateError("plan_objective_binding_missing")
        self._validate_plan_capabilities(connection, graph)
        return {
            **graph.to_dict(),
            "project_id": objective["project_id"],
            "created_event_id": row["created_event_id"],
            "created_at": row["created_at"],
        }

    def get_plan(
        self,
        plan_id: str,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        validate_identity(plan_id, "invalid_plan_id")
        if version is not None and (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise OperationalStateError("invalid_plan_version")
        with closing(self.operational_store._connect()) as connection:
            if version is None:
                row = connection.execute(
                    "SELECT * FROM plan_graphs WHERE plan_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (plan_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM plan_graphs WHERE plan_id=? AND version=?",
                    (plan_id, version),
                ).fetchone()
            return self._plan_from_row(connection, row) if row else None

    def list_plans(
        self,
        *,
        objective_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if objective_id is not None:
            validate_identity(objective_id, "invalid_objective_id")
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise OperationalStateError("invalid_plan_limit")
        clauses: list[str] = []
        args: list[Any] = []
        if objective_id is not None:
            clauses.append("g.objective_id=?")
            args.append(objective_id)
        if project_id is not None:
            clauses.append("o.project_id=?")
            args.append(project_id)
        query = (
            "SELECT g.* FROM plan_graphs g "
            "JOIN objective_contracts o "
            "ON o.objective_id=g.objective_id "
            "AND o.version=g.objective_version"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY g.plan_id,g.version LIMIT ?"
        args.append(limit)
        with closing(self.operational_store._connect()) as connection:
            return [
                self._plan_from_row(connection, row)
                for row in connection.execute(query, tuple(args)).fetchall()
            ]

    def commit_plan(
        self,
        *,
        graph: PlanGraph | Mapping[str, Any],
        authority: AuthorityArtifact,
        event: StateEvent,
        lease_owner_id: str,
        lease_token: str,
        _failpoint: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if _failpoint is not None and not callable(_failpoint):
            raise OperationalStateError("invalid_plan_failpoint")
        validated = self._validate_graph(graph)
        trusted_at = self._trusted_at()
        current_revision = validated.revisions[-1]
        if current_revision.created_at != trusted_at:
            revision_seed = {
                "plan_id": current_revision.plan_id,
                "from_version": current_revision.from_version,
                "to_version": current_revision.to_version,
                "change_reason": current_revision.change_reason,
                "authority_impact": current_revision.authority_impact,
                "previous_digest": current_revision.previous_digest,
                "plan_digest": current_revision.plan_digest,
                "created_at": trusted_at,
                "created_by": current_revision.created_by,
            }
            trusted_revision = PlanRevision(
                revision_id=make_identity("revision", revision_seed),
                revision_digest="",
                **revision_seed,
            )
            validated = replace(
                validated,
                revisions=validated.revisions[:-1] + (trusted_revision,),
            )
        previous: dict[str, Any] | None = None
        previous_graph: PlanGraph | None = None
        try:
            if validated.version == 1:
                validate_initial_plan_states(validated.nodes)
            else:
                previous = self.get_plan(
                    validated.plan_id,
                    validated.version - 1,
                )
                if previous is None:
                    raise OperationalStateError("plan_predecessor_missing")
                previous_graph = self._graph_from_record(previous)
                validate_plan_state_transition(
                    previous_graph,
                    validated,
                    mode="persistence",
                )
        except PlanningError as exc:
            raise OperationalStateError(exc.reason_code) from None
        if not authority.issued_by.startswith("user:"):
            raise OperationalStateError("plan_commit_requires_user_authority")
        objective = self.objective_store.get_objective(
            validated.objective_id,
            validated.objective_version,
        )
        if (
            objective is None
            or objective["canonical_digest"] != validated.objective_digest
        ):
            raise OperationalStateError("plan_objective_binding_missing")
        if (
            previous_graph is not None
            and self._completes_human_gate(previous_graph, validated)
            and (
                event.actor_kind != "user"
                or event.actor_id != authority.issued_by
            )
        ):
            raise OperationalStateError(
                "human_gate_event_requires_authority_issuer"
            )
        self._validate_plan_event(validated, objective, event)
        event.validate_event_id(authority.authority_digest)
        authority.authorize_event(
            project_id=event.project_id,
            entity_id=event.entity_id,
            event_type=event.event_type,
            at=trusted_at,
        )
        event = replace(event, recorded_at=trusted_at)
        preview = self.operational_store.preview_event(
            authority=authority,
            event=event,
        )
        current_revision = validated.revisions[-1]
        expected_cause = (
            objective["created_event_id"]
            if validated.version == 1
            else None
        )
        if validated.version > 1:
            if previous is None:
                raise OperationalStateError("plan_predecessor_missing")
            expected_cause = previous["created_event_id"]
            if (
                current_revision.from_version != previous["version"]
                or current_revision.previous_digest
                != previous["plan_digest"]
                or canonical_json(
                    [item.to_dict() for item in validated.revisions[:-1]]
                )
                != canonical_json(previous["revisions"])
            ):
                raise OperationalStateError(
                    "plan_revision_predecessor_mismatch"
                )
        if event.caused_by_event_id != expected_cause:
            raise OperationalStateError("plan_event_cause_mismatch")
        with closing(self.operational_store._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational_store._assert_lease(
                    connection,
                    owner_id=lease_owner_id,
                    token=lease_token,
                    at=trusted_at,
                )
                objective_row = connection.execute(
                    "SELECT canonical_digest,created_event_id "
                    "FROM objective_contracts "
                    "WHERE objective_id=? AND version=?",
                    (
                        validated.objective_id,
                        validated.objective_version,
                    ),
                ).fetchone()
                if (
                    objective_row is None
                    or objective_row["canonical_digest"]
                    != validated.objective_digest
                ):
                    raise OperationalStateError(
                        "plan_objective_binding_missing"
                    )
                if validated.version == 1:
                    try:
                        validate_initial_plan_states(validated.nodes)
                    except PlanningError as exc:
                        raise OperationalStateError(
                            exc.reason_code
                        ) from None
                    transaction_expected_cause = objective_row[
                        "created_event_id"
                    ]
                else:
                    predecessor_row = connection.execute(
                        "SELECT * FROM plan_graphs "
                        "WHERE plan_id=? AND version=?",
                        (validated.plan_id, validated.version - 1),
                    ).fetchone()
                    if predecessor_row is None:
                        raise OperationalStateError(
                            "plan_predecessor_missing"
                        )
                    predecessor_record = self._plan_from_row(
                        connection,
                        predecessor_row,
                    )
                    predecessor_graph = self._graph_from_record(
                        predecessor_record
                    )
                    try:
                        validate_plan_state_transition(
                            predecessor_graph,
                            validated,
                            mode="persistence",
                        )
                    except PlanningError as exc:
                        raise OperationalStateError(
                            exc.reason_code
                        ) from None
                    if (
                        self._completes_human_gate(
                            predecessor_graph,
                            validated,
                        )
                        and (
                            event.actor_kind != "user"
                            or event.actor_id != authority.issued_by
                        )
                    ):
                        raise OperationalStateError(
                            "human_gate_event_requires_authority_issuer"
                        )
                    if (
                        current_revision.from_version
                        != predecessor_record["version"]
                        or current_revision.previous_digest
                        != predecessor_record["plan_digest"]
                        or canonical_json(
                            [
                                item.to_dict()
                                for item in validated.revisions[:-1]
                            ]
                        )
                        != canonical_json(
                            predecessor_record["revisions"]
                        )
                    ):
                        raise OperationalStateError(
                            "plan_revision_predecessor_mismatch"
                        )
                    transaction_expected_cause = predecessor_record[
                        "created_event_id"
                    ]
                if event.caused_by_event_id != transaction_expected_cause:
                    raise OperationalStateError(
                        "plan_event_cause_mismatch"
                    )
                self._validate_plan_capabilities(connection, validated)
                existing = connection.execute(
                    "SELECT * FROM plan_graphs "
                    "WHERE plan_id=? AND version=?",
                    (validated.plan_id, validated.version),
                ).fetchone()
                if existing is not None:
                    record = self._plan_from_row(connection, existing)
                    receipt_row = connection.execute(
                        "SELECT * FROM state_receipts "
                        "WHERE idempotency_key=?",
                        (event.idempotency_key,),
                    ).fetchone()
                    if (
                        record["plan_digest"] != validated.plan_digest
                        or existing["created_event_id"] != event.event_id
                        or receipt_row is None
                        or receipt_row["event_id"] != event.event_id
                        or receipt_row["authority_digest"]
                        != authority.authority_digest
                    ):
                        raise OperationalStateError(
                            "plan_version_collision"
                        )
                    connection.rollback()
                    receipt = self.operational_store._receipt_from_row(
                        receipt_row,
                        idempotent_replay=True,
                    )
                    return {
                        "plan_id": validated.plan_id,
                        "version": validated.version,
                        "plan_digest": validated.plan_digest,
                        "event_id": event.event_id,
                        "receipt_digest": receipt.receipt_digest,
                        "idempotent_replay": True,
                    }
                receipt = self.operational_store._commit_preview_on_connection(
                    connection,
                    authority=authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                if _failpoint is not None:
                    _failpoint("after_event")
                connection.execute(
                    "INSERT INTO plan_graphs("
                    "plan_id,version,objective_id,objective_version,"
                    "objective_digest,canonical_graph,plan_digest,"
                    "created_event_id,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        validated.plan_id,
                        validated.version,
                        validated.objective_id,
                        validated.objective_version,
                        validated.objective_digest,
                        canonical_json(validated.to_dict()),
                        validated.plan_digest,
                        event.event_id,
                        trusted_at,
                    ),
                )
                if _failpoint is not None:
                    _failpoint("after_graph")
                for ordinal, node in enumerate(validated.nodes):
                    connection.execute(
                        "INSERT INTO plan_nodes("
                        "plan_id,plan_version,node_id,node_kind,risk_class,"
                        "reversible,authority_requirement,timeout_ms,state,"
                        "canonical_node,node_digest,ordinal"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            validated.plan_id,
                            validated.version,
                            node.node_id,
                            node.node_kind,
                            node.risk,
                            int(node.reversible),
                            node.authority_requirement,
                            node.timeout_ms,
                            node.state,
                            canonical_json(node.to_dict()),
                            canonical_digest(node.to_dict()),
                            ordinal,
                        ),
                    )
                if _failpoint is not None:
                    _failpoint("after_nodes")
                for edge in validated.edges:
                    connection.execute(
                        "INSERT INTO plan_edges("
                        "plan_id,plan_version,edge_id,from_node_id,"
                        "to_node_id,edge_kind,condition_json,priority,"
                        "edge_digest"
                        ") VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            validated.plan_id,
                            validated.version,
                            edge.edge_id,
                            edge.from_node_id,
                            edge.to_node_id,
                            edge.edge_kind,
                            (
                                canonical_json(edge.condition)
                                if edge.condition is not None
                                else None
                            ),
                            edge.priority,
                            canonical_digest(edge.to_dict()),
                        ),
                    )
                connection.execute(
                    "INSERT INTO plan_revisions("
                    "revision_id,plan_id,from_version,to_version,"
                    "change_reason,authority_impact,previous_digest,"
                    "plan_digest,canonical_revision,revision_digest,"
                    "created_event_id,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        current_revision.revision_id,
                        current_revision.plan_id,
                        current_revision.from_version,
                        current_revision.to_version,
                        current_revision.change_reason,
                        current_revision.authority_impact,
                        current_revision.previous_digest,
                        current_revision.plan_digest,
                        canonical_json(current_revision.to_dict()),
                        current_revision.revision_digest,
                        event.event_id,
                        trusted_at,
                    ),
                )
                if _failpoint is not None:
                    _failpoint("before_commit")
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise OperationalStateError("plan_insert_conflict") from None
            except Exception:
                connection.rollback()
                raise
        return {
            "plan_id": validated.plan_id,
            "version": validated.version,
            "plan_digest": validated.plan_digest,
            "event_id": event.event_id,
            "receipt_digest": receipt.receipt_digest,
            "idempotent_replay": False,
        }
