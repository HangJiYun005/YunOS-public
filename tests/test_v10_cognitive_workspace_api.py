from __future__ import annotations

from collections.abc import Mapping
import hashlib
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_models import OperationalStateError
from memory.operational_state_store import initialize_operational_database
from tests import test_v10_agent_loop as agent_fixture_module
from tools.local_web import AyunOSHandler, CognitiveObjectiveReadonlyService


ROOT = Path(__file__).resolve().parents[1]
LOCAL_WEB = ROOT / "tools" / "local_web.py"


class _WorkspaceService:
    def list_runs(self, **kwargs):
        return {
            "runs_schema_version": 1,
            "items": [{"run_id": "run:test", "status": "executing"}],
            "count": 1,
            "limit": kwargs["limit"],
        }

    def get_workspace(self, run_id: str, *, surface: str):
        return {
            "workspace_schema_version": 1,
            "surface": surface,
            "run": {"run_id": run_id},
            "objective": {},
            "plan": {},
            "context": {"items": [], "count": 0},
            "authorization": None,
            "provider": {},
            "checkpoint": {},
            "proposals": {"items": [], "count": 0},
            "executions": {"items": [], "count": 0},
            "supervision": {"items": [], "count": 0},
            "verifications": {"items": [], "count": 0},
            "outcome": {"current": None, "items": [], "count": 0},
            "learning": {"items": [], "count": 0},
            "boundaries": {
                "readonly": True,
                "write_api_available": False,
                "mobile_write_allowed": False,
                "tailnet_write_allowed": False,
                "acceptance_write_allowed": False,
                "authority_expansion_allowed": False,
            },
        }


class CognitiveWorkspaceReadonlyApiTest(unittest.TestCase):
    def _handler(self, service=None):
        handler = object.__new__(AyunOSHandler)
        handler.app = SimpleNamespace(
            cognitive_state_service=service or _WorkspaceService(),
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

    def _agent_fixture(self):
        case = agent_fixture_module.V10BoundedAgentLoopTest(methodName="runTest")
        case.setUp()
        self.addCleanup(case.tearDown)
        fixture = case._fixture()
        proposals = {
            fixture.action.node_id: case._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, _source, _artifacts = case._loop(fixture, proposals)
        outcome = loop.run(fixture.run.run_id)
        self.assertEqual("waiting_acceptance", outcome.run.status)
        return fixture

    def test_routes_are_strict_get_only_and_dispatched(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        dispatch = source.split("    def handle_api_get", 1)[1].split(
            "    def api_cognitive_objectives", 1
        )[0]
        self.assertIn('path == "/api/cognitive/runs"', dispatch)
        self.assertIn('path == "/api/cognitive/workspace"', dispatch)
        post_gate = source.split("    def do_POST", 1)[1].split(
            "    def handle_api_get", 1
        )[0]
        post_dispatch = source.split("    def handle_api_post", 1)[1].split(
            "    def api_runtime_execute", 1
        )[0]
        for route in ("cognitive/runs", "cognitive/workspace"):
            self.assertNotIn(route, post_gate)
            self.assertNotIn(route, post_dispatch)

    def test_runs_and_workspace_contracts_are_readonly_no_store(self) -> None:
        handler, responses = self._handler()
        handler.api_cognitive_workspace(
            "/api/cognitive/runs",
            {
                "project_id": ["project:test"],
                "status": ["executing"],
                "limit": ["20"],
            },
        )
        handler.api_cognitive_workspace(
            "/api/cognitive/workspace",
            {"run_id": ["run:test"], "surface": ["mobile"]},
        )
        self.assertEqual(2, len(responses))
        for payload, status in responses:
            self.assertEqual(HTTPStatus.OK, status)
            self.assertTrue(payload["readonly"])
            self.assertEqual("no-store", payload["cache_control"])
        self.assertEqual(20, responses[0][0]["limit"])
        self.assertEqual("mobile", responses[1][0]["surface"])

    def test_workspace_bundle_has_stable_sections_and_real_verified_records(self) -> None:
        fixture = self._agent_fixture()
        service = CognitiveObjectiveReadonlyService(fixture.database)
        bundle = service.get_workspace(fixture.run.run_id)
        self.assertEqual(
            {
                "workspace_schema_version",
                "surface",
                "run",
                "objective",
                "plan",
                "context",
                "authorization",
                "provider",
                "checkpoint",
                "proposals",
                "executions",
                "supervision",
                "verifications",
                "outcome",
                "learning",
                "boundaries",
            },
            set(bundle),
        )
        self.assertEqual(1, bundle["workspace_schema_version"])
        self.assertEqual("waiting_acceptance", bundle["run"]["status"])
        self.assertEqual(1, bundle["proposals"]["count"])
        self.assertEqual(1, bundle["executions"]["count"])
        self.assertEqual(1, bundle["supervision"]["count"])
        self.assertEqual(1, bundle["verifications"]["count"])
        self.assertIsNone(bundle["outcome"]["current"])
        self.assertFalse(bundle["boundaries"]["write_api_available"])
        self.assertFalse(bundle["boundaries"]["mobile_write_allowed"])
        self.assertFalse(bundle["boundaries"]["tailnet_write_allowed"])

    def test_run_list_is_the_trusted_workspace_identity_source(self) -> None:
        fixture = self._agent_fixture()
        service = CognitiveObjectiveReadonlyService(fixture.database)
        listing = service.list_runs(
            project_id=fixture.objective.project_id,
            status="waiting_acceptance",
            limit=10,
        )
        self.assertEqual(1, listing["runs_schema_version"])
        self.assertEqual(1, listing["count"])
        item = listing["items"][0]
        self.assertEqual(fixture.run.run_id, item["run_id"])
        self.assertEqual(fixture.objective.interpreted_goal, item["title"])
        self.assertEqual("waiting_acceptance", item["status"])
        self.assertTrue(item["waiting_acceptance"])
        self.assertFalse(item["overreach_detected"])

    def test_mobile_and_desktop_read_the_same_authoritative_bundle(self) -> None:
        fixture = self._agent_fixture()
        service = CognitiveObjectiveReadonlyService(fixture.database)
        desktop = service.get_workspace(fixture.run.run_id, surface="desktop")
        mobile = service.get_workspace(fixture.run.run_id, surface="mobile")
        self.assertEqual("desktop", desktop.pop("surface"))
        self.assertEqual("mobile", mobile.pop("surface"))
        self.assertEqual(desktop, mobile)
        self.assertFalse(mobile["boundaries"]["mobile_write_allowed"])

    def test_query_whitelists_and_identifiers_fail_closed(self) -> None:
        handler, responses = self._handler()
        cases = (
            (
                "/api/cognitive/runs",
                {"write": ["1"]},
                "unsupported_cognitive_workspace_parameter",
            ),
            (
                "/api/cognitive/runs",
                {"limit": ["101"]},
                "invalid_cognitive_workspace_limit",
            ),
            (
                "/api/cognitive/workspace",
                {},
                "cognitive_workspace_run_id_required",
            ),
            (
                "/api/cognitive/workspace",
                {"run_id": ["run:test"], "surface": ["act"]},
                "invalid_cognitive_workspace_surface",
            ),
        )
        for path, params, reason in cases:
            handler.api_cognitive_workspace(path, params)
            payload, status = responses[-1]
            self.assertEqual(HTTPStatus.BAD_REQUEST, status)
            self.assertEqual(reason, payload["reason_code"])
            self.assertTrue(payload["readonly"])

    def test_unmigrated_is_503_and_empty_migrated_store_is_safe(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "operational.db"
            initialize_operational_database(
                database,
                created_at="2026-08-01T00:00:00Z",
            )
            unmigrated = CognitiveObjectiveReadonlyService(database)
            handler, responses = self._handler(unmigrated)
            handler.api_cognitive_workspace(
                "/api/cognitive/runs",
                {},
            )
            self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, responses[-1][1])
            self.assertTrue(responses[-1][0]["readonly"])

            apply_cognitive_state_migration(
                database,
                created_at="2026-08-01T00:01:00Z",
            )
            migrated = CognitiveObjectiveReadonlyService(database)
            handler, responses = self._handler(migrated)
            handler.api_cognitive_workspace("/api/cognitive/runs", {})
            self.assertEqual(HTTPStatus.OK, responses[-1][1])
            self.assertEqual([], responses[-1][0]["items"])
            handler.api_cognitive_workspace(
                "/api/cognitive/workspace",
                {"run_id": ["run:not-found"]},
            )
            self.assertEqual(HTTPStatus.NOT_FOUND, responses[-1][1])
            self.assertEqual(
                "cognitive_run_not_found",
                responses[-1][0]["reason_code"],
            )

    def test_workspace_omits_secret_reference_and_authority_seal_material(self) -> None:
        fixture = self._agent_fixture()
        service = CognitiveObjectiveReadonlyService(fixture.database)
        bundle = service.get_workspace(fixture.run.run_id)
        serialized = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("secret_reference", serialized)
        self.assertNotIn("nonce_digest", serialized)
        self.assertNotIn("source_authority_id", serialized)
        self.assertNotIn("source_authority_digest", serialized)

    def test_run_list_fails_closed_when_authorization_binding_is_missing(self) -> None:
        fixture = self._agent_fixture()
        with sqlite3.connect(fixture.database) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER authorization_artifacts_no_delete")
            connection.execute(
                "DELETE FROM authorization_artifacts WHERE authorization_id=?",
                (fixture.authorization.authorization_id,),
            )
            connection.execute(
                "CREATE TRIGGER authorization_artifacts_no_delete "
                "BEFORE DELETE ON authorization_artifacts BEGIN "
                "SELECT RAISE(ABORT, 'authorization_artifacts_append_only'); END"
            )
            connection.commit()

        service = CognitiveObjectiveReadonlyService(fixture.database)
        with self.assertRaises(OperationalStateError) as missing:
            service.list_runs(limit=10)
        self.assertEqual(
            "cognitive_run_authorization_missing",
            missing.exception.reason_code,
        )

    def test_secret_filtered_tool_output_is_rechecked_before_publication(self) -> None:
        fixture = self._agent_fixture()
        service = CognitiveObjectiveReadonlyService(fixture.database)
        with patch(
            "memory.cognitive_orchestration.contains_secret_material",
            side_effect=lambda value: (
                isinstance(value, Mapping) and "content" in value
            ),
        ), self.assertRaises(OperationalStateError) as blocked:
            service.get_workspace(fixture.run.run_id)
        self.assertEqual(
            "cognitive_workspace_secret_material_forbidden",
            blocked.exception.reason_code,
        )

    def test_direct_sql_verification_is_digest_checked_before_publication(self) -> None:
        fixture = self._agent_fixture()
        with sqlite3.connect(fixture.database) as connection:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='state_verifications_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER state_verifications_no_update")
            connection.execute(
                "UPDATE state_verifications SET canonical_verification=?",
                ('{"details":{"run_id":"tampered"}}',),
            )
            connection.execute(trigger_sql)
            connection.commit()

        service = CognitiveObjectiveReadonlyService(fixture.database)
        with self.assertRaises(OperationalStateError) as tampered:
            service.get_workspace(fixture.run.run_id)
        self.assertIn(
            tampered.exception.reason_code,
            {
                "cognitive_workspace_verification_tampered",
                "cognitive_workspace_verification_corrupt",
            },
        )

    def test_http_transport_is_no_store(self) -> None:
        handler = object.__new__(AyunOSHandler)
        handler.wfile = BytesIO()
        statuses: list[HTTPStatus] = []
        headers: list[tuple[str, str]] = []
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.append((name, value))
        handler.end_headers = lambda: None
        handler.send_json({"readonly": True})
        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertIn(("Cache-Control", "no-store"), headers)

    def test_local_and_tailnet_posts_remain_forbidden_without_state_change(self) -> None:
        fixture = self._agent_fixture()
        before = hashlib.sha256(fixture.database.read_bytes()).hexdigest()
        for name, headers in (
            (
                "local",
                {"Content-Length": "0", "Origin": "http://127.0.0.1:8765"},
            ),
            (
                "tailnet",
                {
                    "Content-Length": "0",
                    "Origin": "https://example.invalid",
                    "X-Forwarded-For": "100.64.0.10",
                },
            ),
        ):
            for route in (
                "/api/cognitive/runs",
                "/api/cognitive/workspace",
            ):
                with self.subTest(name=name, route=route):
                    handler = object.__new__(AyunOSHandler)
                    handler.path = route
                    handler.client_address = ("127.0.0.1", 54321)
                    handler.headers = headers
                    handler.app = SimpleNamespace(
                        readonly=True,
                        runtime_review=False,
                        profile_admin=False,
                        runtime_admin=False,
                        service_mode="real-readonly",
                    )
                    responses = []
                    handler.send_error_json = (
                        lambda message,
                        status=HTTPStatus.BAD_REQUEST: responses.append(
                            ({"error": message}, status)
                        )
                    )
                    handler.do_POST()
                    self.assertEqual(HTTPStatus.FORBIDDEN, responses[-1][1])
        after = hashlib.sha256(fixture.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_database_and_record_failures_do_not_leak_exception_text(self) -> None:
        class Broken:
            def get_workspace(self, *_args, **_kwargs):
                raise OperationalStateError(
                    "cognitive_workspace_verification_tampered"
                )

        handler, responses = self._handler(Broken())
        handler.api_cognitive_workspace(
            "/api/cognitive/workspace",
            {"run_id": ["run:test"]},
        )
        payload, status = responses[-1]
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual(
            "cognitive_workspace_verification_tampered",
            payload["reason_code"],
        )
        self.assertNotIn("trace", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
