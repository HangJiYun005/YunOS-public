from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from memory.cognitive_objective import build_objective_contract
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.cognitive_state_store import CognitiveStateStore
from memory.operational_state_models import (
    AuthorityArtifact,
    SourceAnchor,
    build_state_event,
    canonical_digest,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools.local_web import AyunOSHandler, CognitiveObjectiveReadonlyService


ROOT = Path(__file__).resolve().parents[1]
LOCAL_WEB = ROOT / "tools" / "local_web.py"


class _ReadonlyObjectiveService:
    def list_objectives(
        self,
        *,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        return {
            "items": [
                {
                    "objective_id": "objective:test",
                    "version": 1,
                    "project_id": project_id or "project:test",
                    "canonical_digest": "a" * 64,
                }
            ],
            "count": 1,
            "limit": limit,
        }

    def get_objective(self, objective_id: str, *, version: int | None = None) -> dict:
        return {
            "objective_id": objective_id,
            "version": version or 1,
            "project_id": "project:test",
            "canonical_digest": "a" * 64,
        }

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict:
        return {
            "items": [],
            "count": 0,
            "project_id": project_id,
            "status_filter": status,
            "limit": limit,
        }


class ObjectiveReadonlyApiTest(unittest.TestCase):
    def _handler(self) -> tuple[AyunOSHandler, list[tuple[dict, HTTPStatus]]]:
        handler = object.__new__(AyunOSHandler)
        handler.app = SimpleNamespace(
            cognitive_state_service=_ReadonlyObjectiveService(),
        )
        responses: list[tuple[dict, HTTPStatus]] = []
        handler.send_json = (
            lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        )
        handler.send_error_json = (
            lambda message, status=HTTPStatus.BAD_REQUEST: responses.append(
                ({"error": message}, status)
            )
        )
        return handler, responses

    def test_readonly_objective_routes_are_dispatched(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        for route in (
            "/api/cognitive/objectives",
            "/api/cognitive/objective",
            "/api/cognitive/objective-candidates",
        ):
            self.assertIn(f'path == "{route}"', source)

    def test_normal_post_handler_has_no_objective_exception(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        post_gate = source.split("    def do_POST", 1)[1].split(
            "    def handle_api_get", 1
        )[0]
        post_dispatch = source.split("    def handle_api_post", 1)[1].split(
            "    def api_runtime_execute", 1
        )[0]
        self.assertNotIn("cognitive/objective", post_gate)
        self.assertNotIn("cognitive/objective", post_dispatch)
        self.assertIn(
            "if self.app.readonly and not (profile_admin_mutation or runtime_admin_mutation)",
            post_gate,
        )

    def test_objective_gets_use_strict_parameter_whitelists(self) -> None:
        handler, responses = self._handler()
        handler.api_cognitive_objectives(
            "/api/cognitive/objectives",
            {"write": ["1"]},
        )
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            responses[-1][0]["reason_code"],
            "unsupported_cognitive_objective_parameter",
        )
        self.assertTrue(responses[-1][0]["readonly"])

    def test_real_query_parser_preserves_blank_unknown_parameters(self) -> None:
        handler = object.__new__(AyunOSHandler)
        calls: list[tuple[str, dict]] = []
        errors: list[tuple[str, HTTPStatus]] = []
        handler.handle_api_get = lambda path, query: calls.append((path, query))
        handler.send_error_json = (
            lambda message, status=HTTPStatus.BAD_REQUEST: errors.append(
                (message, status)
            )
        )
        handler.path = "/api/cognitive/objectives?write="
        handler.do_GET()
        self.assertEqual(
            calls,
            [("/api/cognitive/objectives", {"write": [""]})],
        )
        handler.path = "/api/cognitive/objectives?unknown"
        handler.do_GET()
        self.assertEqual(
            errors,
            [("invalid_query_string", HTTPStatus.BAD_REQUEST)],
        )
        handler.path = "/api/search?q=test&sort=&"
        handler.do_GET()
        self.assertEqual(
            calls[-1],
            ("/api/search", {"q": ["test"]}),
        )

    def test_objective_get_payloads_are_explicitly_readonly(self) -> None:
        handler, responses = self._handler()
        cases = (
            (
                "/api/cognitive/objectives",
                {"project_id": ["project:test"], "limit": ["20"]},
            ),
            (
                "/api/cognitive/objective",
                {"objective_id": ["objective:test"], "version": ["1"]},
            ),
            (
                "/api/cognitive/objective-candidates",
                {"project_id": ["project:test"], "status": ["pending"]},
            ),
        )
        for route, params in cases:
            handler.api_cognitive_objectives(route, params)
        self.assertEqual(len(responses), 3)
        for payload, status in responses:
            self.assertEqual(status, HTTPStatus.OK)
            self.assertTrue(payload["readonly"])
            self.assertEqual(payload["cache_control"], "no-store")

    def test_missing_identity_and_invalid_limit_fail_closed(self) -> None:
        handler, responses = self._handler()
        handler.api_cognitive_objectives("/api/cognitive/objective", {})
        handler.api_cognitive_objectives(
            "/api/cognitive/objectives",
            {"limit": ["0"]},
        )
        self.assertEqual(responses[0][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            responses[0][0]["reason_code"],
            "objective_id_required",
        )
        self.assertEqual(responses[1][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            responses[1][0]["reason_code"],
            "invalid_cognitive_objective_limit",
        )

    def test_search_route_remains_unchanged_and_separate(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        dispatch = source.split("    def handle_api_get", 1)[1].split(
            "    def api_operational_state", 1
        )[0]
        self.assertIn('elif path == "/api/search":', dispatch)
        search_method = source.split("    def api_search", 1)[1].split(
            "\n    def ", 1
        )[0]
        self.assertNotIn("cognitive", search_method)
        self.assertNotIn("objective", search_method)

    def test_real_readonly_service_reads_migrated_temporary_store(self) -> None:
        t0 = "2026-07-31T00:00:00Z"
        t1 = "2026-07-31T00:01:00Z"
        t2 = "2026-07-31T00:02:00Z"
        t3 = "2026-07-31T00:03:00Z"
        t5 = "2026-07-31T01:00:00Z"
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "operational.db"
            initialize_operational_database(database, created_at=t0)
            apply_cognitive_state_migration(database, created_at=t1)
            anchor = SourceAnchor(
                anchor_id="anchor:objective-api-e2e",
                source_kind="user_declaration",
                locator="test:objective-api-e2e",
                evidence_digest="a" * 64,
                observed_at=t1,
                privacy_scope="private_content",
            )
            contract = build_objective_contract(
                user_utterance="读取一个真实 Objective。",
                project_id="project:yunos",
                source_anchors=[anchor.to_dict()],
                success_criteria=["GET 返回确定性 Objective。"],
                agency_level="plan",
                agency_selected_by="user",
            )
            authority = AuthorityArtifact(
                authority_id="authority:objective-api-e2e",
                issued_by="user:objective-api-e2e",
                issued_at=t0,
                expires_at=t5,
                target_project_id="project:yunos",
                allowed_event_types=("decision_approved",),
                read_scope=("project:yunos",),
                write_scope=("*",),
                prohibited_operations=("merge", "tag", "push"),
                commit_permission=True,
            )
            event = build_state_event(
                project_id="project:yunos",
                entity_id="decision:objective-api-e2e",
                entity_type="decision",
                event_type="decision_approved",
                source_kind="declared",
                source_anchor=anchor,
                authority_kind="user",
                actor_kind="user",
                actor_id="user:objective-api-e2e",
                observed_at=t1,
                effective_at=t2,
                recorded_at=t3,
                expires_at=None,
                supersedes_event_id=None,
                caused_by_event_id=None,
                idempotency_key="idempotency:objective-api-e2e",
                conflict_group_id=None,
                payload={
                    "state": "approved",
                    "objective_id": contract.objective_id,
                    "objective_version": contract.version,
                    "objective_digest": contract.canonical_digest,
                },
                privacy_scope="private_content",
                authority_digest=authority.authority_digest,
            )
            operational = OperationalStateStore(database)
            owner = "owner:objective-api-e2e"
            token = "objective-api-e2e-lease-token"
            operational.acquire_writer_lease(
                owner_id=owner,
                token=token,
                acquired_at=t1,
                expires_at=t5,
            )
            CognitiveStateStore(
                database,
                trusted_clock=lambda: t3,
            ).commit_objective(
                contract=contract.to_dict(),
                authority=authority,
                event=event,
                lease_owner_id=owner,
                lease_token=token,
                created_at=t3,
            )
            service = CognitiveObjectiveReadonlyService(database)
            listing = service.list_objectives(
                project_id="project:yunos",
                limit=1,
            )
            detail = service.get_objective(contract.objective_id)
            self.assertEqual(1, listing["count"])
            self.assertEqual(contract.canonical_digest, detail["canonical_digest"])
            self.assertEqual([], service.list_candidates()["items"])
            malicious = contract.to_dict()
            malicious["user_utterance"] = "token=1234567890abcdef"
            malicious["canonical_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in malicious.items()
                    if key != "canonical_digest"
                }
            )
            operational.create_candidate(
                candidate_id="candidate:objective-api-malicious",
                project_id="project:yunos",
                entity_id="decision:objective-api-malicious",
                entity_type="decision",
                candidate_type="objective_contract",
                candidate=malicious,
                confidence_ppm=1_000_000,
                source_anchor=anchor,
                created_at=t3,
                expires_at=t5,
            )
            with self.assertRaisesRegex(
                Exception,
                "objective_secret_literal_forbidden",
            ) as captured:
                service.list_candidates()
            self.assertNotIn(
                "1234567890abcdef",
                str(captured.exception),
            )


if __name__ == "__main__":
    unittest.main()
