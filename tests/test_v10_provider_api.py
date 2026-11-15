from __future__ import annotations

from io import BytesIO
import hashlib
from http import HTTPStatus
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory.cognitive_provider import build_suggested_provider_registry
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_models import AuthorityArtifact
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools.local_web import AyunOSHandler, CognitiveObjectiveReadonlyService


T0 = "2026-07-31T00:00:00.000000Z"
T1 = "2026-07-31T00:01:00.000000Z"
T2 = "2026-07-31T00:02:00.000000Z"
T6 = "2026-07-31T02:00:00.000000Z"
SECRET_SENTINEL = "sk-provider-api-test-sentinel-never-return"


class ProviderReadonlyApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Path(self.temporary.name) / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        apply_cognitive_state_migration(self.database, created_at=T1)

        self.owner = "owner:v10-c-provider-api-test"
        self.lease_token = "v10-c-provider-api-test-lease"
        operational = OperationalStateStore(self.database)
        operational.acquire_writer_lease(
            owner_id=self.owner,
            token=self.lease_token,
            acquired_at=T1,
            expires_at=T6,
        )
        self.registry = build_suggested_provider_registry()
        self.store = CognitiveProviderStore(
            self.database,
            trusted_clock=lambda: T2,
        )
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": SECRET_SENTINEL,
                "KIMI_API_KEY": SECRET_SENTINEL,
            },
            clear=False,
        ):
            self.store.install_builtin_providers(
                registry=self.registry,
                authority=self._authority(),
                lease_owner_id=self.owner,
                lease_token=self.lease_token,
            )
        self.service = CognitiveObjectiveReadonlyService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _authority() -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id="authority:v10-c-provider-api-install",
            issued_by="user:yunos",
            issued_at=T0,
            expires_at=T6,
            target_project_id="project:yunos",
            allowed_event_types=("provider_registry_install",),
            read_scope=("project:yunos",),
            write_scope=("provider-registry:yunos-v10",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "start_next_version",
            ),
            commit_permission=True,
        )

    @staticmethod
    def _handler(
        service: CognitiveObjectiveReadonlyService,
    ) -> tuple[AyunOSHandler, list[tuple[dict, HTTPStatus]]]:
        handler = object.__new__(AyunOSHandler)
        handler.app = SimpleNamespace(cognitive_state_service=service)
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

    def _request(
        self,
        path: str,
        params: dict[str, list[str]] | None = None,
        *,
        service: CognitiveObjectiveReadonlyService | None = None,
    ) -> tuple[dict, HTTPStatus]:
        handler, responses = self._handler(service or self.service)
        handler.handle_api_get(path, params or {})
        self.assertEqual(1, len(responses))
        return responses[0]

    def test_list_and_detail_use_the_real_migrated_provider_store(self) -> None:
        listing, list_status = self._request("/api/cognitive/providers")
        self.assertEqual(HTTPStatus.OK, list_status)
        self.assertEqual(5, listing["count"])
        self.assertEqual(100, listing["limit"])
        self.assertEqual(
            [
                "deepseek",
                "fake",
                "kimi",
                "local.openai_compatible",
                "openai.compatible",
            ],
            [item["provider_id"] for item in listing["items"]],
        )
        self.assertTrue(listing["readonly"])
        self.assertEqual("no-store", listing["cache_control"])

        detail, detail_status = self._request(
            "/api/cognitive/provider",
            {"provider_id": ["deepseek"], "profile_version": ["1"]},
        )
        self.assertEqual(HTTPStatus.OK, detail_status)
        self.assertEqual("deepseek", detail["provider_id"])
        self.assertEqual(1, detail["profile_version"])
        self.assertEqual(
            self.registry.get("deepseek", 1).profile_digest,
            detail["profile_digest"],
        )
        self.assertTrue(detail["manual_selection_only"])
        self.assertFalse(detail["hidden_fallback"])
        self.assertFalse(detail["grants_authority"])
        self.assertFalse(detail["execution_allowed"])
        self.assertTrue(detail["readonly"])
        self.assertEqual("no-store", detail["cache_control"])

    def test_list_filters_provider_enabled_health_and_limit(self) -> None:
        cases = (
            (
                {"provider_id": ["kimi"]},
                ["kimi"],
            ),
            (
                {"enabled": ["false"]},
                ["fake", "local.openai_compatible", "openai.compatible"],
            ),
            (
                {"health_state": ["unavailable"]},
                ["deepseek", "kimi"],
            ),
            (
                {
                    "provider_id": ["kimi"],
                    "enabled": ["true"],
                    "health_state": ["unavailable"],
                    "limit": ["1"],
                },
                ["kimi"],
            ),
            (
                {"enabled": ["false"], "limit": ["1"]},
                ["fake"],
            ),
        )
        for params, expected_ids in cases:
            with self.subTest(params=params):
                payload, status = self._request(
                    "/api/cognitive/providers",
                    params,
                )
                self.assertEqual(HTTPStatus.OK, status)
                self.assertEqual(
                    expected_ids,
                    [item["provider_id"] for item in payload["items"]],
                )
                self.assertEqual(len(expected_ids), payload["count"])

    def test_uninstalled_registry_is_empty_and_detail_is_not_found(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "uninstalled-operational.db"
            initialize_operational_database(database, created_at=T0)
            apply_cognitive_state_migration(database, created_at=T1)
            service = CognitiveObjectiveReadonlyService(database)

            listing, list_status = self._request(
                "/api/cognitive/providers",
                service=service,
            )
            self.assertEqual(HTTPStatus.OK, list_status)
            self.assertEqual([], listing["items"])
            self.assertEqual(0, listing["count"])
            self.assertTrue(listing["readonly"])

            detail, detail_status = self._request(
                "/api/cognitive/provider",
                {"provider_id": ["deepseek"]},
                service=service,
            )
            self.assertEqual(HTTPStatus.NOT_FOUND, detail_status)
            self.assertEqual(
                "provider_profile_not_found",
                detail["reason_code"],
            )
            self.assertTrue(detail["readonly"])

    def test_missing_or_unknown_provider_and_version_are_not_found(self) -> None:
        missing_id, missing_id_status = self._request(
            "/api/cognitive/provider",
            {"provider_id": ["provider.not-installed"]},
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, missing_id_status)
        self.assertEqual(
            "provider_profile_not_found",
            missing_id["reason_code"],
        )

        missing_version, missing_version_status = self._request(
            "/api/cognitive/provider",
            {"provider_id": ["deepseek"], "profile_version": ["2"]},
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, missing_version_status)
        self.assertEqual(
            "provider_profile_not_found",
            missing_version["reason_code"],
        )

    def test_query_whitelist_repeated_empty_boolean_and_limits_fail_closed(
        self,
    ) -> None:
        cases = (
            (
                "/api/cognitive/providers",
                {"write": ["1"]},
                "unsupported_cognitive_objective_parameter",
            ),
            (
                "/api/cognitive/providers",
                {"provider_id": ["deepseek", "kimi"]},
                "invalid_cognitive_objective_provider_id",
            ),
            (
                "/api/cognitive/providers",
                {"provider_id": [""]},
                "invalid_cognitive_objective_provider_id",
            ),
            (
                "/api/cognitive/providers",
                {"enabled": ["true", "false"]},
                "invalid_cognitive_objective_enabled",
            ),
            (
                "/api/cognitive/providers",
                {"enabled": ["1"]},
                "invalid_cognitive_objective_enabled",
            ),
            (
                "/api/cognitive/providers",
                {"enabled": [""]},
                "invalid_cognitive_objective_enabled",
            ),
            (
                "/api/cognitive/providers",
                {"limit": ["0"]},
                "invalid_cognitive_objective_limit",
            ),
            (
                "/api/cognitive/providers",
                {"limit": ["101"]},
                "invalid_cognitive_objective_limit",
            ),
            (
                "/api/cognitive/providers",
                {"limit": ["not-an-integer"]},
                "invalid_cognitive_objective_limit",
            ),
            (
                "/api/cognitive/providers",
                {"limit": ["1", "2"]},
                "invalid_cognitive_objective_limit",
            ),
            (
                "/api/cognitive/provider",
                {},
                "provider_id_required",
            ),
            (
                "/api/cognitive/provider",
                {"provider_id": ["deepseek"], "profile_version": ["0"]},
                "invalid_cognitive_objective_profile_version",
            ),
        )
        for path, params, reason_code in cases:
            with self.subTest(path=path, params=params):
                payload, status = self._request(path, params)
                self.assertEqual(HTTPStatus.BAD_REQUEST, status)
                self.assertEqual(reason_code, payload["reason_code"])
                self.assertTrue(payload["readonly"])

    def test_cognitive_query_parser_preserves_blank_and_rejects_bare_terms(
        self,
    ) -> None:
        handler, responses = self._handler(self.service)
        handler.path = "/api/cognitive/providers?enabled="
        handler.do_GET()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[-1][1])
        self.assertEqual(
            "invalid_cognitive_objective_enabled",
            responses[-1][0]["reason_code"],
        )

        handler.path = "/api/cognitive/providers?write"
        handler.do_GET()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[-1][1])
        self.assertEqual("invalid_query_string", responses[-1][0]["error"])

    def test_responses_and_http_transport_are_readonly_no_store(self) -> None:
        listing, status = self._request("/api/cognitive/providers")
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(listing["readonly"])
        self.assertEqual("no-store", listing["cache_control"])

        transport = object.__new__(AyunOSHandler)
        transport.wfile = BytesIO()
        sent_statuses: list[HTTPStatus] = []
        sent_headers: list[tuple[str, str]] = []
        transport.send_response = sent_statuses.append
        transport.send_header = (
            lambda name, value: sent_headers.append((name, value))
        )
        transport.end_headers = lambda: None
        AyunOSHandler.send_json(transport, listing)
        self.assertEqual([HTTPStatus.OK], sent_statuses)
        self.assertIn(("Cache-Control", "no-store"), sent_headers)

    def test_provider_api_never_returns_or_persists_secret_material(self) -> None:
        listing, list_status = self._request("/api/cognitive/providers")
        detail, detail_status = self._request(
            "/api/cognitive/provider",
            {"provider_id": ["deepseek"]},
        )
        self.assertEqual(HTTPStatus.OK, list_status)
        self.assertEqual(HTTPStatus.OK, detail_status)
        serialized = json.dumps(
            {"listing": listing, "detail": detail},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(SECRET_SENTINEL, serialized)
        self.assertNotIn("DEEPSEEK_API_KEY", serialized)
        self.assertNotIn("KIMI_API_KEY", serialized)
        self.assertNotIn(
            SECRET_SENTINEL,
            self.database.read_bytes().decode("utf-8", errors="ignore"),
        )
        self.assertEqual(
            {
                "reference_id",
                "resolver_kind",
                "required",
            },
            set(detail["secret_reference"]),
        )
        self.assertTrue(
            detail["secret_reference"]["reference_id"].startswith("secret.")
        )

    def test_provider_post_is_forbidden_for_local_and_tailnet_readonly(
        self,
    ) -> None:
        before_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        before = self.store.list_providers()
        sources = (
            (
                "local",
                ("127.0.0.1", 54321),
                {
                    "Content-Length": "0",
                    "Origin": "http://127.0.0.1:8765",
                },
            ),
            (
                "tailnet-proxy",
                ("127.0.0.1", 54322),
                {
                    "Content-Length": "0",
                    "Origin": "https://example.invalid",
                    "X-Forwarded-For": "100.64.0.10",
                },
            ),
        )
        for source, client_address, headers in sources:
            for route in (
                "/api/cognitive/providers",
                "/api/cognitive/provider",
            ):
                with self.subTest(source=source, route=route):
                    handler = object.__new__(AyunOSHandler)
                    handler.path = route
                    handler.client_address = client_address
                    handler.headers = headers
                    handler.app = SimpleNamespace(
                        readonly=True,
                        runtime_review=False,
                        profile_admin=False,
                        runtime_admin=False,
                        service_mode="real-readonly",
                    )
                    responses: list[tuple[dict, HTTPStatus]] = []
                    handler.send_error_json = (
                        lambda message,
                        status=HTTPStatus.BAD_REQUEST: responses.append(
                            ({"error": message}, status)
                        )
                    )
                    handler.do_POST()
                    self.assertEqual(
                        [
                            (
                                {"error": "Readonly mode: writes are disabled"},
                                HTTPStatus.FORBIDDEN,
                            )
                        ],
                        responses,
                    )

        after = self.store.list_providers()
        after_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(before_hash, after_hash)


if __name__ == "__main__":
    unittest.main()
