from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_store import initialize_operational_database
from tools.local_web import AyunOSHandler, CognitiveObjectiveReadonlyService


ROOT = Path(__file__).resolve().parents[1]
LOCAL_WEB = ROOT / "tools" / "local_web.py"


class _ReadonlyPlanningService:
    def list_capabilities(
        self,
        *,
        risk_class: str | None = None,
        availability: str | None = None,
        executor_kind: str | None = None,
        limit: int = 100,
    ) -> dict:
        return {
            "items": [
                {
                    "capability_id": "read_file",
                    "capability_version": 1,
                    "risk_class": risk_class or "low",
                    "availability": availability or "available",
                    "executor_kind": executor_kind or "native",
                }
            ],
            "count": 1,
            "limit": limit,
        }

    def list_plans(
        self,
        *,
        objective_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        return {
            "items": [
                {
                    "plan_id": "plan:test",
                    "version": 1,
                    "objective_id": objective_id or "objective:test",
                    "project_id": project_id or "project:test",
                }
            ],
            "count": 1,
            "limit": limit,
        }

    def get_plan(self, plan_id: str, *, version: int | None = None) -> dict:
        return {
            "plan_id": plan_id,
            "version": version or 1,
            "plan_digest": "a" * 64,
        }


class PlanningReadonlyApiTest(unittest.TestCase):
    def _handler(self) -> tuple[AyunOSHandler, list[tuple[dict, HTTPStatus]]]:
        handler = object.__new__(AyunOSHandler)
        handler.app = SimpleNamespace(
            cognitive_state_service=_ReadonlyPlanningService(),
        )
        responses: list[tuple[dict, HTTPStatus]] = []
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

    def test_readonly_planning_routes_are_dispatched(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        for route in (
            "/api/cognitive/capabilities",
            "/api/cognitive/plans",
            "/api/cognitive/plan",
        ):
            self.assertIn(f'path == "{route}"', source)

    def test_planning_get_payloads_are_readonly_and_no_store(self) -> None:
        handler, responses = self._handler()
        cases = (
            (
                "/api/cognitive/capabilities",
                {
                    "risk_class": ["low"],
                    "availability": ["available"],
                    "executor_kind": ["native"],
                    "limit": ["10"],
                },
            ),
            (
                "/api/cognitive/plans",
                {
                    "objective_id": ["objective:test"],
                    "project_id": ["project:test"],
                    "limit": ["10"],
                },
            ),
            (
                "/api/cognitive/plan",
                {"plan_id": ["plan:test"], "version": ["1"]},
            ),
        )
        for route, params in cases:
            handler.api_cognitive_objectives(route, params)
        self.assertEqual(3, len(responses))
        for payload, status in responses:
            self.assertEqual(HTTPStatus.OK, status)
            self.assertTrue(payload["readonly"])
            self.assertEqual("no-store", payload["cache_control"])

    def test_unknown_parameter_and_missing_plan_id_fail_closed(self) -> None:
        handler, responses = self._handler()
        handler.api_cognitive_objectives(
            "/api/cognitive/capabilities",
            {"write": ["1"]},
        )
        handler.api_cognitive_objectives("/api/cognitive/plan", {})
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][1])
        self.assertEqual(
            "unsupported_cognitive_objective_parameter",
            responses[0][0]["reason_code"],
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[1][1])
        self.assertEqual("plan_id_required", responses[1][0]["reason_code"])

    def test_post_dispatch_has_no_planning_write_route(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        post_gate = source.split("    def do_POST", 1)[1].split(
            "    def handle_api_get", 1
        )[0]
        post_dispatch = source.split("    def handle_api_post", 1)[1].split(
            "    def api_runtime_execute", 1
        )[0]
        for value in ("cognitive/plan", "cognitive/capabilit"):
            self.assertNotIn(value, post_gate)
            self.assertNotIn(value, post_dispatch)

    def test_search_route_still_has_no_planning_dependency(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        search_method = source.split("    def api_search", 1)[1].split(
            "\n    def ", 1
        )[0]
        self.assertNotIn("Cognitive", search_method)
        self.assertNotIn("planning", search_method)
        self.assertNotIn("provider", search_method)

    def test_real_readonly_service_uses_the_existing_operational_database(
        self,
    ) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "operational.db"
            initialize_operational_database(
                database,
                created_at="2026-07-31T00:00:00Z",
            )
            apply_cognitive_state_migration(
                database,
                created_at="2026-07-31T00:01:00Z",
            )
            service = CognitiveObjectiveReadonlyService(database)
            self.assertEqual(
                {"items": [], "count": 0, "limit": 100},
                service.list_capabilities(),
            )
            self.assertEqual(
                {"items": [], "count": 0, "limit": 50},
                service.list_plans(),
            )
            self.assertIsNone(service.get_plan("plan:not-found"))


if __name__ == "__main__":
    unittest.main()
