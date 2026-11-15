from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from memory.cognitive_capability import build_suggested_capability_registry
from memory.cognitive_objective import AuthorityRequest, build_objective_contract
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanEdge,
    PlanGraph,
    PlanNode,
    PlanRevision,
    build_plan_graph,
    revise_plan,
)
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.cognitive_state_store import CognitiveStateStore
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    make_identity,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


T0 = "2026-07-31T00:00:00.000000Z"
T1 = "2026-07-31T00:01:00.000000Z"
T2 = "2026-07-31T00:02:00.000000Z"
T3 = "2026-07-31T00:03:00.000000Z"
T4 = "2026-07-31T00:04:00.000000Z"
T6 = "2026-07-31T02:00:00.000000Z"
ROOT = Path(__file__).resolve().parents[1]


class CognitivePlanningStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Path(self.temp.name) / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        apply_cognitive_state_migration(self.database, created_at=T1)
        self.operational = OperationalStateStore(self.database)
        self.owner = "owner:v10-b-test"
        self.token = "v10-b-test-lease"
        self.operational.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T1,
            expires_at=T6,
        )
        self.anchor = SourceAnchor(
            anchor_id="anchor:user:v10-b-store",
            source_kind="user_declaration",
            locator="task:v10-b-store",
            evidence_digest="a" * 64,
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        self.objective = build_objective_contract(
            user_utterance="Create a bounded read-only engineering plan.",
            interpreted_goal="Create and verify a plan without executing it.",
            project_id="project:yunos",
            source_anchors=(self.anchor,),
            success_criteria=("The graph is deterministic and persisted.",),
            agency_level="plan",
            agency_selected_by="user",
            maximum_authorized_agency_level="plan",
            risk_tolerance="medium",
            authority_request=AuthorityRequest(
                requested=True,
                read_roots=("E:\\AyunOS",),
                database_permission="readonly",
            ),
        )
        self.objective_authority = self.authority(
            "authority:v10-b-objective",
            ("decision_approved",),
        )
        self.objective_event = build_state_event(
            project_id="project:yunos",
            entity_id="decision:v10-b-objective",
            entity_type="decision",
            event_type="decision_approved",
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id="user:yunos",
            observed_at=T0,
            effective_at=T1,
            recorded_at=T2,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key="idempotency:v10-b-objective",
            conflict_group_id=None,
            payload={
                "state": "approved",
                "objective_id": self.objective.objective_id,
                "objective_version": self.objective.version,
                "objective_digest": self.objective.canonical_digest,
            },
            privacy_scope="private_metadata",
            authority_digest=self.objective_authority.authority_digest,
        )
        CognitiveStateStore(
            self.database,
            trusted_clock=lambda: T2,
        ).commit_objective(
            contract=self.objective.to_dict(),
            authority=self.objective_authority,
            event=self.objective_event,
            lease_owner_id=self.owner,
            lease_token=self.token,
            created_at=T0,
        )
        self.trusted_at = T3
        self.store = CognitivePlanningStore(
            self.database,
            trusted_clock=lambda: self.trusted_at,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def authority(
        authority_id: str,
        event_types: tuple[str, ...],
        *,
        issued_by: str = "user:yunos",
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at=T6,
            target_project_id="project:yunos",
            allowed_event_types=event_types,
            read_scope=("project:yunos",),
            write_scope=("*",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "start_next_version",
            ),
            commit_permission=True,
        )

    def install_registry(self):
        registry = build_suggested_capability_registry()
        result = self.store.install_builtin_capabilities(
            registry=registry,
            authority=self.authority(
                "authority:v10-b-registry",
                ("capability_registry_install",),
            ),
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        return registry, result

    def graph(self, *, with_capability: bool = False):
        requirements = ()
        if with_capability:
            profile = build_suggested_capability_registry().lookup(
                "project.file.read",
                version=1,
            )
            requirements = (
                CapabilityRequirement(
                    capability_id=profile.capability_id,
                    capability_version=profile.version,
                    capability_digest=profile.capability_digest,
                ),
            )
        first = PlanNode(
            node_id="node:inspect",
            objective_id=self.objective.objective_id,
            title="Inspect bounded evidence",
            purpose="Read only the evidence needed by the objective.",
            capability_requirements=requirements,
            read_scope=("E:\\AyunOS\\docs",),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The bounded evidence is identified.",),
            verification=("Verify the selected source digest.",),
            timeout_ms=5_000,
        )
        verify = PlanNode(
            node_id="node:verify",
            objective_id=self.objective.objective_id,
            title="Verify the plan",
            purpose="Verify the graph independently.",
            dependencies=(first.node_id,),
            node_kind="verification",
            read_scope=("E:\\AyunOS\\tests",),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The graph passes deterministic checks.",),
            verification=("Recompute all graph digests.",),
            timeout_ms=5_000,
        )
        edge = PlanEdge(
            edge_id="edge:inspect-verify",
            from_node_id=first.node_id,
            to_node_id=verify.node_id,
            edge_kind="sequence",
            condition=None,
            priority=0,
        )
        return build_plan_graph(
            objective=self.objective,
            nodes=(first, verify),
            edges=(edge,),
            change_reason="Create the bounded initial plan.",
            created_at=T1,
        )

    def forged_state_graph(
        self,
        graph: PlanGraph,
        *,
        state_by_node: dict[str, str],
        next_version: bool = False,
        created_by: str = "agent",
    ) -> PlanGraph:
        nodes = tuple(
            replace(node, state=state_by_node.get(node.node_id, node.state))
            for node in graph.nodes
        )
        version = graph.version + 1 if next_version else graph.version
        identity = {
            "plan_id": graph.plan_id,
            "objective_id": graph.objective_id,
            "objective_version": graph.objective_version,
            "objective_digest": graph.objective_digest,
            "nodes": [item.to_dict() for item in nodes],
            "edges": [item.to_dict() for item in graph.edges],
            "entry_node_ids": list(graph.entry_node_ids),
            "terminal_node_ids": list(graph.terminal_node_ids),
            "version": version,
        }
        digest = canonical_digest(identity)
        revision_seed = {
            "plan_id": graph.plan_id,
            "from_version": graph.version if next_version else None,
            "to_version": version,
            "change_reason": "Forge a runtime state without runtime evidence.",
            "authority_impact": "none",
            "previous_digest": graph.plan_digest if next_version else None,
            "plan_digest": digest,
            "created_at": T1,
            "created_by": created_by,
        }
        revision = PlanRevision(
            revision_id=make_identity("revision", revision_seed),
            revision_digest="",
            **revision_seed,
        )
        revisions = (
            graph.revisions + (revision,)
            if next_version
            else (revision,)
        )
        return PlanGraph(
            plan_id=graph.plan_id,
            objective_id=graph.objective_id,
            objective_version=graph.objective_version,
            objective_digest=graph.objective_digest,
            nodes=nodes,
            edges=graph.edges,
            revisions=revisions,
            entry_node_ids=graph.entry_node_ids,
            terminal_node_ids=graph.terminal_node_ids,
            plan_digest=digest,
            version=version,
        )

    def plan_event(self, graph, authority, *, cause: str):
        revision = graph.revisions[-1]
        event_type = (
            "plan_created"
            if graph.version == 1
            else "plan_scope_expansion_approved"
            if revision.authority_impact == "requires_new_authority"
            else "plan_revised"
        )
        return build_state_event(
            project_id="project:yunos",
            entity_id=make_identity(
                "workitem",
                {"plan_id": graph.plan_id, "version": graph.version},
            ),
            entity_type="work_item",
            event_type=event_type,
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="agent",
            actor_id="agent:yunos-planner",
            observed_at=T1,
            effective_at=T1,
            recorded_at=T1,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=cause,
            idempotency_key=f"idempotency:{graph.plan_id}:{graph.version}",
            conflict_group_id=None,
            payload={
                "state": "proposed",
                "plan_id": graph.plan_id,
                "plan_version": graph.version,
                "plan_digest": graph.plan_digest,
                "objective_id": graph.objective_id,
                "objective_version": graph.objective_version,
                "objective_digest": graph.objective_digest,
                "authority_impact": revision.authority_impact,
            },
            privacy_scope="private_metadata",
            authority_digest=authority.authority_digest,
        )

    def commit_graph(self, graph, *, cause: str, suffix: str):
        event_type = (
            "plan_created" if graph.version == 1 else "plan_revised"
        )
        if graph.revisions[-1].authority_impact == "requires_new_authority":
            event_type = "plan_scope_expansion_approved"
        authority = self.authority(
            f"authority:v10-b-plan-{suffix}",
            (event_type,),
        )
        event = self.plan_event(graph, authority, cause=cause)
        return self.store.commit_plan(
            graph=graph,
            authority=authority,
            event=event,
            lease_owner_id=self.owner,
            lease_token=self.token,
        ), authority, event

    def test_builtin_registry_is_atomic_immutable_and_idempotent(self) -> None:
        registry, first = self.install_registry()
        _registry, replay = self.install_registry()
        self.assertEqual(4, first["inserted"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(registry.registry_digest, replay["registry_digest"])
        self.assertFalse(first["grants_authority"])
        self.assertFalse(first["execution_allowed"])
        self.assertEqual(4, len(self.store.list_capabilities()))
        with closing(self.operational._connect(write=True)) as connection:
            with self.assertRaisesRegex(
                sqlite3.DatabaseError,
                "capability_registry_append_only",
            ):
                connection.execute(
                    "UPDATE capability_registry SET availability='available' "
                    "WHERE capability_id='project.file.read'"
                )
            with self.assertRaisesRegex(
                sqlite3.DatabaseError,
                "capability_registry_append_only",
            ):
                connection.execute(
                    "DELETE FROM capability_registry "
                    "WHERE capability_id='project.file.read'"
                )

    def test_exact_partial_registry_is_completed_but_agent_install_is_rejected(
        self,
    ) -> None:
        registry = build_suggested_capability_registry()
        profile = registry.profiles[0]
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute(
                "INSERT INTO capability_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    profile.to_canonical_json(),
                    profile.capability_digest,
                    T2,
                ),
            )
            connection.commit()
        result = self.store.install_builtin_capabilities(
            registry=registry,
            authority=self.authority(
                "authority:v10-b-registry-partial",
                ("capability_registry_install",),
            ),
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(len(registry.profiles) - 1, result["inserted"])
        self.assertEqual(len(registry.profiles), result["profile_count"])
        self.assertEqual(
            registry.registry_digest,
            self.store.load_capability_registry().registry_digest,
        )
        agent_authority = self.authority(
            "authority:v10-b-registry-agent",
            ("capability_registry_install",),
            issued_by="agent:yunos",
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "capability_registry_requires_user_authority",
        ):
            self.store.install_builtin_capabilities(
                registry=registry,
                authority=agent_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

    def test_rogue_registry_append_is_rejected_by_every_read_and_plan_commit(
        self,
    ) -> None:
        registry, _result = self.install_registry()
        rogue = replace(
            registry.lookup("project.file.read", version=1),
            capability_id="project.file.audit",
            display_name="Rogue audit capability",
            capability_digest="",
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute(
                "INSERT INTO capability_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rogue.capability_id,
                    rogue.version,
                    rogue.display_name,
                    rogue.risk_class,
                    int(rogue.reversible),
                    rogue.required_authority,
                    rogue.executor_kind,
                    rogue.availability,
                    rogue.timeout_ms,
                    rogue.max_output_bytes,
                    rogue.to_canonical_json(),
                    rogue.capability_digest,
                    T2,
                ),
            )
            connection.commit()
        for reader in (
            self.store.list_capabilities,
            self.store.load_capability_registry,
        ):
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(
                    OperationalStateError,
                    "untrusted_capability_registry",
                ):
                    reader()

        node = PlanNode(
            node_id="node:rogue-capability",
            objective_id=self.objective.objective_id,
            title="Bind a rogue capability",
            purpose="Prove an appended descriptor cannot enter a plan.",
            capability_requirements=(
                CapabilityRequirement(
                    capability_id=rogue.capability_id,
                    capability_version=rogue.version,
                    capability_digest=rogue.capability_digest,
                ),
            ),
            read_scope=("E:\\AyunOS",),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The rogue binding is rejected.",),
            verification=("Check the exact code-owned manifest.",),
        )
        graph = build_plan_graph(
            objective=self.objective,
            nodes=(node,),
            change_reason="Attempt to bind an appended capability.",
            created_at=T1,
        )
        authority = self.authority(
            "authority:v10-b-rogue-plan",
            ("plan_created",),
        )
        event = self.plan_event(
            graph,
            authority,
            cause=self.objective_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "untrusted_capability_registry",
        ):
            self.store.commit_plan(
                graph=graph,
                authority=authority,
                event=event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        with closing(self.operational._connect()) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_graphs"
                ).fetchone()[0],
            )

    def test_capability_semantics_cannot_be_understated_at_commit(self) -> None:
        registry, _result = self.install_registry()
        patch = registry.lookup("project.file.patch", version=1)
        node = PlanNode(
            node_id="node:understated-patch",
            objective_id=self.objective.objective_id,
            title="Understate a controlled write",
            purpose="Probe profile to node monotonic safety checks.",
            capability_requirements=(
                CapabilityRequirement(
                    capability_id=patch.capability_id,
                    capability_version=patch.version,
                    capability_digest=patch.capability_digest,
                ),
            ),
            read_scope=("E:\\AyunOS",),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The understated write is rejected.",),
            verification=("Compare the node to the capability profile.",),
        )
        graph = build_plan_graph(
            objective=self.objective,
            nodes=(node,),
            change_reason="Attempt to understate a write capability.",
            created_at=T1,
        )
        authority = self.authority(
            "authority:v10-b-understated-plan",
            ("plan_created",),
        )
        event = self.plan_event(
            graph,
            authority,
            cause=self.objective_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "plan_node_capability_(authority|risk|write_scope)_",
        ):
            self.store.commit_plan(
                graph=graph,
                authority=authority,
                event=event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        with closing(self.operational._connect()) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
            )

    def test_forged_initial_and_terminal_runtime_states_never_persist(
        self,
    ) -> None:
        initial = self.forged_state_graph(
            self.graph(),
            state_by_node={"node:inspect": "completed"},
        )
        initial_authority = self.authority(
            "authority:v10-b-forged-initial",
            ("plan_created",),
        )
        initial_event = self.plan_event(
            initial,
            initial_authority,
            cause=self.objective_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "initial_plan_node_state_forbidden",
        ):
            self.store.commit_plan(
                graph=initial,
                authority=initial_authority,
                event=initial_event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

        base = self.graph()
        _result, _authority, base_event = self.commit_graph(
            base,
            cause=self.objective_event.event_id,
            suffix="state-base",
        )
        terminal = self.forged_state_graph(
            base,
            state_by_node={"node:inspect": "completed"},
            next_version=True,
        )
        terminal_authority = self.authority(
            "authority:v10-b-forged-terminal",
            ("plan_revised",),
        )
        terminal_event = self.plan_event(
            terminal,
            terminal_authority,
            cause=base_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "plan_runtime_state_transition_denied",
        ):
            self.store.commit_plan(
                graph=terminal,
                authority=terminal_authority,
                event=terminal_event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        with closing(self.operational._connect()) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id IN (?,?)",
                    (initial_event.event_id, terminal_event.event_id),
                ).fetchone()
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_graphs"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_revisions"
                ).fetchone()[0],
            )

    def test_human_gate_completion_binds_user_revision_event_and_authority(
        self,
    ) -> None:
        gate = PlanNode(
            node_id="node:user-gate",
            objective_id=self.objective.objective_id,
            title="Wait for an explicit user decision",
            purpose="Keep the plan blocked until the user decides.",
            node_kind="human_gate",
            risk="medium",
            reversible=True,
            authority_requirement="human_gate",
            success_criteria=("The user supplies an explicit decision.",),
            verification=("Bind the decision to user authority.",),
            state="waiting_authority",
        )
        base = build_plan_graph(
            objective=self.objective,
            nodes=(gate,),
            change_reason="Create the explicit human gate.",
            created_at=T1,
        )
        _result, _authority, base_event = self.commit_graph(
            base,
            cause=self.objective_event.event_id,
            suffix="human-gate-base",
        )

        forged_agent = self.forged_state_graph(
            base,
            state_by_node={"node:user-gate": "completed"},
            next_version=True,
            created_by="agent",
        )
        agent_authority = self.authority(
            "authority:v10-b-human-gate-agent",
            ("plan_revised",),
        )
        agent_event = self.plan_event(
            forged_agent,
            agent_authority,
            cause=base_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "human_gate_resume_requires_user",
        ):
            self.store.commit_plan(
                graph=forged_agent,
                authority=agent_authority,
                event=agent_event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )

        forged_user = self.forged_state_graph(
            base,
            state_by_node={"node:user-gate": "completed"},
            next_version=True,
            created_by="user",
        )
        user_authority = self.authority(
            "authority:v10-b-human-gate-event",
            ("plan_revised",),
        )
        agent_claim_event = self.plan_event(
            forged_user,
            user_authority,
            cause=base_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "human_gate_event_requires_authority_issuer",
        ):
            self.store.commit_plan(
                graph=forged_user,
                authority=user_authority,
                event=agent_claim_event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        with closing(self.operational._connect()) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id IN (?,?)",
                    (agent_event.event_id, agent_claim_event.event_id),
                ).fetchone()
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_graphs"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_revisions"
                ).fetchone()[0],
            )

    def test_plan_commit_read_revision_and_replay_are_atomic(self) -> None:
        self.install_registry()
        graph = self.graph(with_capability=True)
        first, _authority, event = self.commit_graph(
            graph,
            cause=self.objective_event.event_id,
            suffix="one",
        )
        stored = self.store.get_plan(graph.plan_id, 1)
        self.assertEqual(graph.plan_digest, stored["plan_digest"])
        self.assertEqual(T3, stored["created_at"])
        self.assertEqual(T3, stored["revisions"][-1]["created_at"])
        replay, _authority, _event = self.commit_graph(
            graph,
            cause=self.objective_event.event_id,
            suffix="one",
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["receipt_digest"], replay["receipt_digest"])

        self.trusted_at = T4
        stored_graph = PlanGraph.from_dict(
            {
                key: stored[key]
                for key in (
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
            }
        )
        changed = replace(
            stored_graph.nodes[-1],
            title="Verify the revised plan independently",
        )
        revised = revise_plan(
            stored_graph,
            objective=self.objective,
            nodes=(stored_graph.nodes[0], changed),
            change_reason="Clarify the independent verification node.",
            created_at=T1,
        )
        revision_result, _authority, _event = self.commit_graph(
            revised,
            cause=event.event_id,
            suffix="two",
        )
        self.assertEqual(2, revision_result["version"])
        latest = self.store.get_plan(graph.plan_id)
        self.assertEqual(2, latest["version"])
        self.assertEqual(
            [None, graph.plan_digest],
            [item["previous_digest"] for item in latest["revisions"]],
        )

    def test_capability_drift_and_wrong_event_fail_before_state_write(self) -> None:
        self.install_registry()
        graph = self.graph(with_capability=True)
        bad_requirement = replace(
            graph.nodes[0].capability_requirements[0],
            capability_digest="f" * 64,
        )
        bad_node = replace(
            graph.nodes[0],
            capability_requirements=(bad_requirement,),
        )
        bad_graph = build_plan_graph(
            objective=self.objective,
            nodes=(bad_node, graph.nodes[1]),
            edges=graph.edges,
            change_reason="Attempt a drifted capability binding.",
            created_at=T1,
        )
        authority = self.authority(
            "authority:v10-b-plan-bad-capability",
            ("plan_created",),
        )
        event = self.plan_event(
            bad_graph,
            authority,
            cause=self.objective_event.event_id,
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "plan_capability_drift",
        ):
            self.store.commit_plan(
                graph=bad_graph,
                authority=authority,
                event=event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        wrong_event = replace(
            self.plan_event(
                graph,
                authority,
                cause=self.objective_event.event_id,
            ),
            event_type="plan_revised",
        )
        with self.assertRaisesRegex(
            OperationalStateError,
            "plan_event_binding_mismatch",
        ):
            self.store.commit_plan(
                graph=graph,
                authority=authority,
                event=wrong_event,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        with closing(self.operational._connect()) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_graphs"
                ).fetchone()[0],
            )

    def test_mid_transaction_node_failure_rolls_back_event_and_graph(self) -> None:
        graph = self.graph()
        authority = self.authority(
            "authority:v10-b-plan-failure",
            ("plan_created",),
        )
        event = self.plan_event(
            graph,
            authority,
            cause=self.objective_event.event_id,
        )
        def failpoint(name: str) -> None:
            if name == "after_graph":
                raise RuntimeError("injected_plan_graph_failure")

        with self.assertRaisesRegex(RuntimeError, "injected_plan_graph_failure"):
            self.store.commit_plan(
                graph=graph,
                authority=authority,
                event=event,
                lease_owner_id=self.owner,
                lease_token=self.token,
                _failpoint=failpoint,
            )
        with closing(self.operational._connect()) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM plan_graphs"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM state_receipts WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
            )

    def test_plan_read_detects_tampered_node_after_schema_attack(self) -> None:
        graph = self.graph()
        self.commit_graph(
            graph,
            cause=self.objective_event.event_id,
            suffix="tamper",
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("DROP TRIGGER plan_nodes_no_update")
            connection.execute(
                "UPDATE plan_nodes SET node_digest=? WHERE node_id=?",
                ("f" * 64, "node:inspect"),
            )
            connection.commit()
        with self.assertRaisesRegex(
            OperationalStateError,
            "operational_schema_drift",
        ):
            self.store.get_plan(graph.plan_id, 1)

    def test_shared_schema_closes_plan_and_capability_preaudit_blockers(
        self,
    ) -> None:
        schema = json.loads(
            (
                ROOT
                / "docs"
                / "schemas"
                / "yunos_v10_cognitive_agency.schema.json"
            ).read_text(encoding="utf-8")
        )
        plan = schema["$defs"]["plan_graph"]
        revision = schema["$defs"]["plan_revision"]
        proposal = schema["$defs"]["tool_proposal"]
        authorization = schema["$defs"]["authorization_artifact"]
        self.assertIn("objective_version", plan["required"])
        self.assertTrue(
            {
                "from_version",
                "to_version",
                "revision_digest",
            }.issubset(revision["required"])
        )
        self.assertTrue(
            {
                "capability_id",
                "capability_version",
                "capability_digest",
            }.issubset(proposal["required"])
        )
        self.assertTrue(
            {
                "delegation_id",
                "source_authority_id",
                "source_authority_digest",
                "issuer_kind",
                "issuer_id",
                "nonce_digest",
            }.issubset(authorization["required"])
        )
        sql = (
            ROOT / "memory" / "cognitive_schema_10001.sql"
        ).read_text(encoding="utf-8")
        for fragment in (
            "CREATE TRIGGER capability_registry_no_update",
            "CREATE TRIGGER capability_registry_no_delete",
            "UNIQUE (plan_id, to_version)",
            "FOREIGN KEY (plan_id, from_version)",
            "capability_digest TEXT NOT NULL",
            "capability_id, capability_version, capability_digest",
        ):
            self.assertIn(fragment, sql)


if __name__ == "__main__":
    unittest.main()
