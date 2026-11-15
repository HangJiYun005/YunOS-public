from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from memory.deepseek_v4_adapter import (
    ARTIFACT_VERSION,
    BALANCE_URL,
    BASE_URL,
    CHAT_URL,
    MODELS_URL,
    MODEL_ID,
    READINESS_PROTOCOL_VERSION,
    RENDERER_VERSION,
    RUNTIME_PROFILE_VERSION,
    DeepSeekV4FlashHTTPAdapter,
    DeepSeekV4ProviderError,
    _run_production_readiness_transport,
    _run_test_readiness_transport,
    prepare_v4_flash_request,
)
from memory.runtime_enablement import (
    FIXED_RUNTIME_PROFILE,
    PROTOCOL_VERSION,
    RuntimeEnablementError,
    _V4FlashRuntimeCore,
    build_runtime_enablement_artifact,
    create_production_v4_flash_runtime,
    deepseek_readiness_authority_state,
    deepseek_readiness_dry_run,
    expire_production_deepseek_readiness,
    initialize_runtime_enablement_sidecar,
    open_production_deepseek_readiness,
    run_production_deepseek_readiness,
    runtime_enablement_sidecar_status,
    runtime_profile_identity,
    validate_runtime_enablement_artifact,
)
from tests import test_context_delivery as v74_tests


class RuntimeEnablementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.delivery_fixture = v74_tests.ContextDeliveryTest(
            "test_four_review_layers_and_trust_boundaries_are_explicit"
        )
        self.delivery_fixture.setUp()
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.sidecar = Path(self.temp.name) / "runtime-d1.db"

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.delivery_fixture.tearDown()

    def test_artifact_uses_new_fixed_profile_renderer_and_exact_wire_bytes(self):
        delivery = self.delivery_fixture.build()
        artifact = build_runtime_enablement_artifact(delivery)
        validate_runtime_enablement_artifact(artifact, trusted_delivery=delivery)
        prepared = prepare_v4_flash_request(artifact)
        self.assertEqual(PROTOCOL_VERSION, artifact["protocol_version"])
        self.assertEqual(ARTIFACT_VERSION, artifact["artifact_version"])
        self.assertEqual(RUNTIME_PROFILE_VERSION, artifact["runtime_profile"]["version"])
        self.assertEqual(RENDERER_VERSION, artifact["renderer"]["version"])
        self.assertEqual({
            "version": RUNTIME_PROFILE_VERSION,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "stream": False,
            "thinking": {"type": "disabled"},
        }, artifact["runtime_profile"])
        self.assertEqual(MODEL_ID, artifact["payload"]["model"])
        self.assertIs(artifact["payload"]["stream"], False)
        self.assertEqual({"type": "disabled"}, artifact["payload"]["thinking"])
        self.assertEqual(prepared.wire_body, base64.b64decode(artifact["wire_body_base64"]))
        self.assertEqual(artifact["wire_body_sha256"], hashlib.sha256(prepared.wire_body).hexdigest())
        self.assertEqual(artifact["payload"], json.loads(prepared.wire_body.decode("utf-8")))

        for field, value in (
            ("model", "deepseek-chat"),
            ("stream", True),
            ("thinking", {"type": "enabled"}),
        ):
            tampered = deepcopy(artifact)
            tampered["payload"][field] = value
            with self.subTest(field=field), self.assertRaises((RuntimeEnablementError, DeepSeekV4ProviderError)):
                validate_runtime_enablement_artifact(tampered, trusted_delivery=delivery)

    def test_adapter_posts_the_validated_wire_without_reserialization(self):
        artifact = build_runtime_enablement_artifact(self.delivery_fixture.build())
        prepared = prepare_v4_flash_request(artifact)
        captured = {}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps({
                    "id": "response-v4-1", "model": MODEL_ID,
                    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                }).encode("utf-8")

        def fake_urlopen(request, *, timeout):
            captured.update(method=request.get_method(), url=request.full_url, body=request.data, timeout=timeout)
            return Response()

        with patch("memory.deepseek_v4_adapter.load_production_deepseek_api_key", return_value="synthetic-test-key"), patch(
            "memory.deepseek_v4_adapter.urlopen", side_effect=fake_urlopen,
        ):
            response = DeepSeekV4FlashHTTPAdapter(timeout_seconds=7).send(prepared)
        self.assertEqual("answer", response.content)
        self.assertEqual(("POST", CHAT_URL), (captured["method"], captured["url"]))
        self.assertEqual(prepared.wire_body, captured["body"])
        self.assertEqual(artifact["wire_body_sha256"], hashlib.sha256(captured["body"]).hexdigest())
        with self.assertRaisesRegex(DeepSeekV4ProviderError, "request_artifact_invalid"):
            DeepSeekV4FlashHTTPAdapter().send({"payload": artifact["payload"]})

    def test_runtime_schema_is_versioned_fixed_append_only_and_strictly_gated(self):
        dry = initialize_runtime_enablement_sidecar(self.sidecar, test_mode=True)
        self.assertEqual("would_initialize", dry["status"])
        self.assertFalse(self.sidecar.exists())
        initialized = initialize_runtime_enablement_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )
        self.assertEqual("initialized", initialized["status"])
        status = runtime_enablement_sidecar_status(self.sidecar)
        self.assertEqual(("ready", runtime_profile_identity()), (
            status["status"], status["runtime_profile_identity"],
        ))
        import sqlite3
        with sqlite3.connect(self.sidecar) as conn:
            profile = json.loads(conn.execute("SELECT runtime_profile_json FROM runtime_schema").fetchone()[0])
            self.assertEqual(FIXED_RUNTIME_PROFILE, profile)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("UPDATE runtime_schema SET runtime_profile_json='{}'")

        target = Path(self.temp.name) / "production-like.db"
        with patch("memory.runtime_enablement.PRODUCTION_RUNTIME_SIDECAR", target):
            refused = initialize_runtime_enablement_sidecar(
                target, execute=True, allow_production_runtime_sidecar=1,
                confirm_production_runtime_sidecar_init=True,
            )
        self.assertEqual("confirmation_type_invalid", refused["reason_code"])
        self.assertFalse(target.exists())

    def test_production_factory_has_no_dependency_or_configuration_injection(self):
        self.assertEqual(0, len(inspect.signature(create_production_v4_flash_runtime).parameters))
        service = create_production_v4_flash_runtime()
        manifest = service.dependency_manifest()
        self.assertEqual("DeepSeekV4FlashHTTPAdapter", manifest["adapter"])
        self.assertEqual(FIXED_RUNTIME_PROFILE, manifest["runtime_profile"])
        self.assertFalse(manifest["client_overrides_allowed"])
        with patch("memory.runtime_enablement.PRODUCTION_RUNTIME_SIDECAR", self.sidecar):
            with self.assertRaisesRegex(RuntimeEnablementError, "test runtime primitive"):
                _V4FlashRuntimeCore(sidecar_path=self.sidecar, delivery_builder=Mock())

    def test_readiness_is_dry_by_default_and_authority_is_one_shot(self):
        dry = deepseek_readiness_dry_run()
        self.assertFalse(dry["executed"])
        self.assertFalse(dry["credential_read"])
        self.assertFalse(dry["chat_completion_sent"])
        with self.assertRaises(RuntimeEnablementError):
            open_production_deepseek_readiness(
                execute=1, allow_official_readiness_gets=True,
                confirm_no_chat_completion=True,
            )
        authority = open_production_deepseek_readiness(
            execute=True, allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        self.assertEqual("issued", deepseek_readiness_authority_state(authority))
        safe = {
            "protocol_version": READINESS_PROTOCOL_VERSION,
            "status": "ready", "checks": {}, "requests": [],
            "chat_completion_sent": False, "credential_persisted": False,
            "response_body_persisted": False,
        }
        with patch("memory.runtime_enablement.load_deepseek_api_key", return_value="synthetic"), patch(
            "memory.runtime_enablement._run_production_readiness_transport", return_value=safe,
        ) as transport:
            self.assertEqual(safe, run_production_deepseek_readiness(authority=authority))
            self.assertEqual("consumed", deepseek_readiness_authority_state(authority))
            with self.assertRaisesRegex(RuntimeEnablementError, "consumed"):
                run_production_deepseek_readiness(authority=authority)
            transport.assert_called_once_with("synthetic")
        with self.assertRaises(RuntimeEnablementError):
            run_production_deepseek_readiness(authority=object())
        expired = open_production_deepseek_readiness(
            execute=True, allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        expire_production_deepseek_readiness(expired)
        self.assertEqual("expired", deepseek_readiness_authority_state(expired))
        with self.assertRaisesRegex(RuntimeEnablementError, "expired"):
            run_production_deepseek_readiness(authority=expired)

    def test_readiness_authority_concurrency_failure_consumption_and_new_authority(self):
        authority = open_production_deepseek_readiness(
            execute=True, allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        entered = threading.Event()
        release = threading.Event()
        requests = []

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, *, timeout):
            requests.append((request.get_method(), request.full_url, request.data, timeout))
            if request.full_url == MODELS_URL:
                entered.set()
                release.wait(2)
                return Response({"data": [{"id": MODEL_ID}]})
            if request.full_url == BALANCE_URL:
                return Response({"is_available": True})
            raise AssertionError("unexpected readiness endpoint")

        def execute():
            try:
                return run_production_deepseek_readiness(authority=authority)
            except RuntimeEnablementError as exc:
                return str(exc)

        with patch("memory.runtime_enablement.load_deepseek_api_key", return_value="synthetic") as key, patch(
            "memory.deepseek_v4_adapter.urlopen", side_effect=fake_urlopen,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                self.assertTrue(entered.wait(1))
                second = pool.submit(execute)
                release.set()
                outcomes = [first.result(), second.result()]
        self.assertEqual([("GET", MODELS_URL), ("GET", BALANCE_URL)], [item[:2] for item in requests])
        self.assertTrue(all(item[2] is None for item in requests))
        key.assert_called_once_with()
        self.assertEqual(1, sum(isinstance(item, dict) for item in outcomes))
        self.assertEqual("consumed", deepseek_readiness_authority_state(authority))

        failed = open_production_deepseek_readiness(
            execute=True, allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        with patch("memory.runtime_enablement.load_deepseek_api_key", return_value="synthetic"), patch(
            "memory.deepseek_v4_adapter.urlopen", side_effect=TimeoutError("offline"),
        ) as failed_get:
            with self.assertRaisesRegex(DeepSeekV4ProviderError, "readiness_network_error"):
                run_production_deepseek_readiness(authority=failed)
        failed_get.assert_called_once()
        self.assertEqual("consumed", deepseek_readiness_authority_state(failed))
        with self.assertRaisesRegex(RuntimeEnablementError, "consumed"):
            run_production_deepseek_readiness(authority=failed)

        fresh = open_production_deepseek_readiness(
            execute=True, allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        fresh_requests = []
        def fresh_urlopen(request, *, timeout):
            fresh_requests.append(request.full_url)
            if request.full_url == MODELS_URL:
                return Response({"data": [{"id": MODEL_ID}]})
            return Response({"is_available": True})
        with patch("memory.runtime_enablement.load_deepseek_api_key", return_value="synthetic"), patch(
            "memory.deepseek_v4_adapter.urlopen", side_effect=fresh_urlopen,
        ):
            self.assertEqual("ready", run_production_deepseek_readiness(authority=fresh)["status"])
        self.assertEqual([MODELS_URL, BALANCE_URL], fresh_requests)

    def test_private_test_transport_calls_only_models_and_balance_and_redacts(self):
        captured = []

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, *, timeout):
            captured.append((request.get_method(), request.full_url, request.data, timeout))
            if request.full_url == MODELS_URL:
                return Response({"data": [{"id": MODEL_ID}, {"id": "other"}]})
            if request.full_url == BALANCE_URL:
                return Response({"is_available": True, "balance_infos": [{"total_balance": "999"}]})
            raise AssertionError("unexpected URL")

        result = _run_test_readiness_transport(
            synthetic_credential="synthetic-test-key", opener=fake_urlopen, timeout_seconds=5,
        )
        self.assertEqual([("GET", MODELS_URL), ("GET", BALANCE_URL)], [item[:2] for item in captured])
        self.assertTrue(all(item[2] is None for item in captured))
        self.assertEqual("ready", result["status"])
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-test-key", serialized)
        self.assertNotIn("999", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertFalse(result["chat_completion_sent"])
        with self.assertRaisesRegex(DeepSeekV4ProviderError, "authority_required"):
            _run_production_readiness_transport("synthetic-test-key")


if __name__ == "__main__":
    unittest.main()
