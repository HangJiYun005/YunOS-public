from __future__ import annotations

import hashlib
from http import HTTPStatus
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_models import (
    AuthorityArtifact,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    make_identity,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools.local_web import AyunOSHandler


T0 = "2026-07-31T00:00:00Z"
T1 = "2026-07-31T00:01:00Z"
T2 = "2026-07-31T00:02:00Z"
T3 = "2026-07-31T00:03:00Z"
T4 = "2026-07-31T00:04:00Z"
T9 = "2026-07-31T00:09:00Z"


class OperationalStateContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.database = self.root / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        self.store = OperationalStateStore(self.database)
        self.service = OperationalStateContextService(self.database)
        self.project_id = "project:yunos"
        self.authority = AuthorityArtifact(
            authority_id="authority:v9-context-test",
            issued_by="actor:local-user",
            issued_at=T0,
            expires_at="2026-08-01T00:00:00Z",
            target_project_id=self.project_id,
            allowed_event_types=(
                "project_declared",
                "goal_declared",
                "work_item_declared",
                "decision_declared",
                "blocker_declared",
                "blocker_observed",
                "delegation_declared",
                "resource_declared",
                "commitment_declared",
            ),
            read_scope=("operational-state",),
            write_scope=("*",),
            prohibited_operations=("push", "create-next-node"),
            commit_permission=True,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )
        self.anchor = SourceAnchor(
            anchor_id="anchor:v9-context-test",
            source_kind="user_declaration",
            locator="user-declaration:v9-context-test",
            evidence_digest=hashlib.sha256(b"context-test").hexdigest(),
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        self.owner = "owner:v9-context-test"
        self.token = "v9-context-writer-token"
        self.store.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T0,
            expires_at=T9,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append(
        self,
        *,
        entity_id: str,
        entity_type: str,
        event_type: str,
        state: str,
        at: str,
        authority_kind: str = "user",
        source_kind: str = "declared",
        actor_kind: str = "user",
        actor_id: str = "actor:local-user",
        payload: dict | None = None,
    ):
        event = build_state_event(
            project_id=self.project_id,
            entity_id=entity_id,
            entity_type=entity_type,
            event_type=event_type,
            source_kind=source_kind,
            source_anchor=self.anchor,
            authority_kind=authority_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
            observed_at=at,
            effective_at=at,
            recorded_at=at,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key=make_identity(
                "idempotency",
                {
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "at": at,
                },
            ),
            conflict_group_id=None,
            payload={"state": state, **(payload or {})},
            privacy_scope="private_metadata",
            authority_digest=self.authority.authority_digest,
        )
        preview = self.store.preview_event(
            authority=self.authority, event=event
        )
        return self.store.commit_preview(
            authority=self.authority,
            preview=preview,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def seed(self) -> None:
        self.append(
            entity_id=self.project_id,
            entity_type="project",
            event_type="project_declared",
            state="active",
            at=T1,
            payload={
                "name": "YunOS",
                "focus": True,
                "priority": 1,
                "purpose": "Private local memory operating system",
                "authoritative_baseline": "v8.3-unified-ingestion",
                "current_version": "v9",
                "current_phase": "v9-C",
                "deferred": ["historical reconstruction"],
                "out_of_scope": ["v8302", "G0"],
                "prohibited_actions": ["push", "create-v9-G"],
            },
        )
        self.append(
            entity_id="goal:v9-final-seal",
            entity_type="goal",
            event_type="goal_declared",
            state="active",
            at=T2,
            payload={"title": "Complete v9 final seal", "priority": 1},
        )
        self.append(
            entity_id="work-item:waiting-recovery",
            entity_type="work_item",
            event_type="work_item_declared",
            state="waiting",
            at=T2,
            payload={"title": "Run five-database recovery"},
        )
        self.append(
            entity_id="decision:pending-visual-review",
            entity_type="decision",
            event_type="decision_declared",
            state="candidate",
            at=T2,
            payload={"title": "Final user visual review"},
        )
        first_blocker = self.append(
            entity_id="blocker:test",
            entity_type="blocker",
            event_type="blocker_declared",
            state="open",
            at=T2,
            payload={"title": "Synthetic test blocker"},
        )
        second_blocker = self.append(
            entity_id="blocker:test",
            entity_type="blocker",
            event_type="blocker_observed",
            state="open",
            at=T3,
            authority_kind="deterministic_observation",
            source_kind="observed",
            actor_kind="system",
            actor_id="system:test-observer",
            payload={"title": "Synthetic observed blocker"},
        )
        self.store.create_conflict(
            conflict_group_id="conflict:test-blocker",
            project_id=self.project_id,
            entity_id="blocker:test",
            field_name="title",
            member_event_ids=(
                first_blocker.event_id,
                second_blocker.event_id,
            ),
            created_at=T4,
        )
        self.append(
            entity_id="delegation:test",
            entity_type="delegation",
            event_type="delegation_declared",
            state="authorized",
            at=T2,
            payload={"title": "Synthetic safe delegation"},
        )
        self.append(
            entity_id="resource:main-database",
            entity_type="resource",
            event_type="resource_declared",
            state="available",
            at=T2,
            payload={"title": "YunOS main database"},
        )
        self.store.set_freshness(
            entity_id="resource:main-database",
            freshness="stale",
        )
        self.append(
            entity_id="commitment:readonly",
            entity_type="commitment",
            event_type="commitment_declared",
            state="active",
            at=T2,
            payload={
                "title": "Normal web remains read-only",
                "constraint": True,
            },
        )
        self.store.create_candidate(
            candidate_id="candidate:next-step",
            project_id=self.project_id,
            entity_id="work-item:next-step",
            entity_type="work_item",
            candidate_type="next_step",
            candidate={"title": "Candidate-only future step"},
            confidence_ppm=700000,
            source_anchor=self.anchor,
            created_at=T3,
            expires_at=None,
        )

    def test_missing_production_database_status_is_readonly_unavailable(self) -> None:
        missing = OperationalStateContextService(self.root / "missing.db")
        status = missing.status_summary()
        self.assertEqual(status["status"], "unavailable")
        self.assertTrue(status["readonly"])
        self.assertFalse(status["writer_available"])
        self.assertFalse(status["path_disclosed"])

    def test_status_project_list_and_detail_are_digest_bound(self) -> None:
        self.seed()
        status = self.service.status_summary()
        projects = self.service.project_list()
        detail = self.service.project_detail(self.project_id)
        self.assertEqual(status["status"], "ready")
        self.assertFalse(status["writer_available"])
        self.assertEqual(projects["count"], 1)
        self.assertEqual(detail["project"]["fields"]["name"], "YunOS")
        self.assertEqual(
            projects["list_digest"],
            canonical_digest(
                {
                    key: value
                    for key, value in projects.items()
                    if key != "list_digest"
                }
            ),
        )

    def test_now_package_is_deterministic_bounded_and_candidate_safe(self) -> None:
        self.seed()
        first = self.service.now_package(
            budget_profile="Large", as_of=T9
        )
        second = self.service.now_package(
            budget_profile="Large", as_of=T9
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first["package_digest"],
            canonical_digest(
                {
                    key: value
                    for key, value in first.items()
                    if key != "package_digest"
                }
            ),
        )

        self.assertEqual(first["cache_control"], "no-store")
        self.assertEqual(first["cache_classification"], "private_operational_state_live")
        self.assertIsNone(first["cached_at"])
        self.assertFalse(first["cache_stale"])
        self.assertTrue(first["selection_policy"]["source_backed"])
        self.assertTrue(first["current_focus"])
        self.assertTrue(first["active_goals"])
        self.assertTrue(first["waiting"])
        self.assertTrue(first["delegations"])
        self.assertTrue(first["pending_decisions"])
        self.assertTrue(first["conflicts"])
        self.assertTrue(first["stale"])
        self.assertTrue(first["resources"])
        self.assertTrue(first["constraints"])
        candidates = [
            item
            for item in first["candidates"]
            if item["candidate_id"] == "candidate:next-step"
        ]
        self.assertEqual(candidates[0]["trust"], "candidate_not_fact")
        self.assertEqual(first["visibility_summary"]["conflicts"]["total"], 1)
        self.assertEqual(first["visibility_summary"]["stale"]["total"], 1)
        self.assertLessEqual(
            first["budget"]["characters_used"],
            first["budget"]["character_budget"],
        )
        self.assertLessEqual(
            first["budget"]["estimated_tokens_used"],
            first["budget"]["estimated_token_budget"],
        )

    def test_resume_timeline_and_delegation_overview_are_readonly(self) -> None:
        self.seed()
        resume = self.service.resume_package(self.project_id, as_of=T9)
        self.assertGreater(len(resume["state_timeline"]), 0)
        self.assertTrue(
            all(item["source_anchor_id"] for item in resume["state_timeline"])
        )

        delegations = self.service.delegation_overview(
            project_id=self.project_id
        )
        self.assertTrue(delegations["readonly"])
        self.assertEqual(delegations["count"], 1)
        self.assertEqual(
            delegations["items"][0]["delegation"]["entity_id"],
            "delegation:test",
        )
        self.assertIsNone(delegations["items"][0]["authority_boundary"])
        self.assertIsNone(delegations["items"][0]["latest_outcome"])

    def test_stale_explicit_focus_is_not_presented_as_current(self) -> None:
        self.seed()
        self.append(
            entity_id="goal:expired-focus",
            entity_type="goal",
            event_type="goal_declared",
            state="active",
            at=T3,
            payload={
                "title": "Expired short-lived focus",
                "focus": True,
                "priority": 0,
            },
        )
        self.store.set_freshness(
            entity_id="goal:expired-focus",
            freshness="stale",
        )
        package = self.service.now_package(
            budget_profile="Large", as_of=T9
        )
        self.assertEqual(
            package["current_focus"][0]["entity_id"],
            self.project_id,
        )
        stale_ids = {item["entity_id"] for item in package["stale"]}
        self.assertIn("goal:expired-focus", stale_ids)

    def test_small_now_package_explains_every_omission(self) -> None:
        self.seed()
        package = self.service.now_package(
            budget_profile="Small", as_of=T9
        )
        self.assertTrue(package["omitted"])
        self.assertTrue(
            all(item["reason_code"] for item in package["omitted"])
        )
        self.assertEqual(
            package["budget"]["omitted_count"], len(package["omitted"])
        )
        self.assertEqual(
            package["visibility_summary"]["conflicts"]["total"],
            package["visibility_summary"]["conflicts"]["selected"]
            + package["visibility_summary"]["conflicts"]["omitted"],
        )

    def test_resume_package_has_required_project_resume_boundaries(self) -> None:
        self.seed()
        package = self.service.resume_package(
            self.project_id,
            budget_profile="Large",
            as_of=T9,
        )
        self.assertEqual(
            package["project_purpose"],
            "Private local memory operating system",
        )
        self.assertEqual(
            package["authoritative_baseline"], "v8.3-unified-ingestion"
        )
        self.assertEqual(package["current_version"], "v9")
        self.assertEqual(package["current_phase"], "v9-C")
        self.assertIn("v8302", package["out_of_scope"])
        self.assertIn("push", package["prohibited_actions"])
        self.assertTrue(package["current_work"])
        self.assertTrue(package["next_candidate_steps"])
        self.assertTrue(package["pending_decisions"])
        self.assertTrue(package["blockers"])
        self.assertTrue(package["delegations"])
        self.assertTrue(package["source_anchors"])
        self.assertEqual(package["cache_control"], "no-store")

    def test_read_queries_cover_waiting_decisions_delegations_conflicts_freshness_evidence(
        self,
    ) -> None:
        self.seed()
        self.assertEqual(
            self.service.filtered_state(
                state_name="waiting", project_id=self.project_id
            )["count"],
            1,
        )
        self.assertEqual(
            self.service.filtered_state(
                entity_type="decision", project_id=self.project_id
            )["count"],
            1,
        )
        self.assertEqual(
            self.service.filtered_state(
                entity_type="delegation", project_id=self.project_id
            )["count"],
            1,
        )
        self.assertEqual(
            self.service.conflicts(project_id=self.project_id)["count"], 1
        )
        self.assertEqual(
            self.service.filtered_state(
                freshness="stale", project_id=self.project_id
            )["count"],
            1,
        )
        self.assertGreater(
            self.service.source_evidence(project_id=self.project_id)["count"],
            0,
        )

    def _handler(self):
        handler = object.__new__(AyunOSHandler)
        handler.app = SimpleNamespace(operational_state_service=self.service)
        responses = []
        handler.send_json = (
            lambda payload, status=HTTPStatus.OK: responses.append(
                (payload, status)
            )
        )
        handler.send_error_json = (
            lambda message, status=HTTPStatus.BAD_REQUEST: responses.append(
                ({"error": message}, status)
            )
        )
        return handler, responses

    def test_operational_state_api_routes_are_get_readonly_and_no_store(self) -> None:
        self.seed()
        handler, responses = self._handler()
        cases = (
            ("/api/operational-state/status", {}),
            ("/api/operational-state/now", {"as_of": [T9]}),
            ("/api/operational-state/projects", {}),
            (
                "/api/operational-state/project",
                {"project_id": [self.project_id]},
            ),
            (
                "/api/operational-state/resume",
                {"project_id": [self.project_id], "as_of": [T9]},
            ),
            (
                "/api/operational-state/waiting",
                {"project_id": [self.project_id]},
            ),
            (
                "/api/operational-state/decisions",
                {"project_id": [self.project_id]},
            ),
            (
                "/api/operational-state/delegations",
                {"project_id": [self.project_id]},
            ),
            (
                "/api/operational-state/conflicts",
                {"project_id": [self.project_id]},
            ),
            (
                "/api/operational-state/freshness",
                {
                    "project_id": [self.project_id],
                    "freshness": ["stale"],
                },
            ),
            (
                "/api/operational-state/evidence",
                {"project_id": [self.project_id]},
            ),
        )
        for path, params in cases:
            handler.api_operational_state(path, params)
        self.assertEqual(len(responses), len(cases))
        self.assertTrue(all(status == HTTPStatus.OK for _, status in responses))
        self.assertEqual(responses[1][0]["cache_control"], "no-store")
        source = Path("tools/local_web.py").read_text(encoding="utf-8")
        self.assertIn(
            'elif path.startswith("/api/operational-state/")', source
        )
        self.assertNotIn(
            'parsed.path == "/api/operational-state/', source.split(
                "    def do_POST", 1
            )[1].split("    def handle_api_get", 1)[0]
        )
        self.assertIn('self.send_header("Cache-Control", "no-store")', source)

    def test_operational_state_api_rejects_unknown_params_and_missing_project(
        self,
    ) -> None:
        handler, responses = self._handler()
        handler.api_operational_state(
            "/api/operational-state/now", {"write": ["1"]}
        )
        handler.api_operational_state(
            "/api/operational-state/resume", {}
        )
        self.assertEqual(responses[0][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            responses[0][0]["reason_code"],
            "unsupported_operational_state_parameter",
        )
        self.assertEqual(responses[1][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            responses[1][0]["reason_code"], "project_id_required"
        )

    def test_tampered_candidate_and_conflict_records_fail_closed(self) -> None:
        self.seed()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE state_candidates SET canonical_candidate='{}'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            Exception, "candidate_record_tampered"
        ):
            self.service.now_package(as_of=T9)


if __name__ == "__main__":
    unittest.main()
