from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
import ast
import base64
import hashlib
import inspect
import io
import json
from http import HTTPStatus
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from memory.deepseek_adapter import (
    DeepSeekHTTPAdapter,
    MODEL_ID,
    PROVIDER_ID,
    DeepSeekProviderError,
    DeepSeekResponse,
    PreparedDeepSeekRequest,
    parse_deepseek_response,
    prepare_deepseek_request,
)
from memory.runtime_thin_slice import (
    PRODUCTION_RUNTIME_SIDECAR,
    RuntimeIdempotencyConflict,
    RuntimeRunInDoubt,
    RuntimeSidecarUnavailable,
    RuntimeThinSliceError,
    RuntimeThinSliceService,
    _RuntimeCore,
    _append_only_trigger_sql,
    _production_runtime_delivery,
    build_runtime_request_artifact,
    initialize_runtime_sidecar,
    open_production_runtime_execution,
    runtime_privacy_manifest,
    runtime_sidecar_status,
    validate_runtime_request_artifact,
)
from tests import test_context_delivery as v74_tests
from tools.init_runtime_sidecar import parse_args as parse_init_args
from tools.local_web import AyunOSHandler, REAL_DB_PATH
from tools.runtime_thin_slice import main as runtime_cli_main, parse_args as parse_runtime_args


class MockDeepSeekAdapter:
    provider_id = PROVIDER_ID
    model_id = MODEL_ID

    def __init__(self, *, content: str = "Mock answer.", failure: Exception | None = None) -> None:
        self.content = content
        self.failure = failure
        self.calls = 0
        self.requests = []
        self._lock = threading.Lock()

    def send(self, prepared_request):
        if not isinstance(prepared_request, PreparedDeepSeekRequest):
            raise DeepSeekProviderError(
                "request_artifact_invalid", category="request", retryable=False,
            )
        with self._lock:
            self.calls += 1
            call = self.calls
            self.requests.append(prepared_request)
        if self.failure is not None:
            raise self.failure
        return DeepSeekResponse(
            provider_response_id=f"mock-response-{call}",
            model=MODEL_ID,
            content=self.content,
            usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            finish_reason="stop",
        )


class RuntimeThinSliceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.delivery_fixture = v74_tests.ContextDeliveryTest(
            "test_four_review_layers_and_trust_boundaries_are_explicit"
        )
        self.delivery_fixture.setUp()
        self.temp = TemporaryDirectory()
        self.sidecar = Path(self.temp.name) / "runtime.db"
        result = initialize_runtime_sidecar(self.sidecar, execute=True, test_mode=True)
        self.assertEqual("initialized", result["status"])

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.delivery_fixture.tearDown()

    def builder(self, query, *, budget_profile="Large"):
        return self.delivery_fixture.build(query=query, budget_profile=budget_profile)

    def core(self, adapter=None):
        return _RuntimeCore(
            sidecar_path=self.sidecar,
            adapter=adapter or MockDeepSeekAdapter(),
            delivery_builder=self.builder,
        )

    def test_artifact_binds_exact_v74_delivery_context_system_model_and_payload(self):
        delivery = self.delivery_fixture.build()
        artifact = build_runtime_request_artifact(delivery)
        validate_runtime_request_artifact(artifact, trusted_delivery=delivery)
        self.assertEqual(delivery["delivery_identity"], artifact["delivery_binding"]["delivery_identity"])
        self.assertEqual(delivery["model_context"], artifact["payload"]["messages"][1]["content"])
        prepared = prepare_deepseek_request(artifact)
        self.assertEqual(artifact["wire_body_sha256"], hashlib.sha256(prepared.wire_body).hexdigest())
        self.assertEqual(prepared.wire_body, base64.b64decode(artifact["wire_body_base64"]))
        self.assertEqual({"id": "deepseek", "model": "deepseek-chat"}, artifact["provider"])
        self.assertFalse(artifact["model_config"]["stream"])

        for mutation in ("context", "identity", "payload"):
            tampered = deepcopy(artifact)
            if mutation == "context":
                tampered["payload"]["messages"][1]["content"] += " forged"
            elif mutation == "identity":
                tampered["delivery_binding"]["delivery_identity"] = "dvy_" + "0" * 64
            else:
                tampered["payload"]["model"] = "other"
            with self.assertRaises(RuntimeThinSliceError):
                validate_runtime_request_artifact(tampered, trusted_delivery=delivery)

    def test_adapter_sends_the_exact_artifact_wire_bytes_and_rejects_payload_only(self):
        delivery = self.delivery_fixture.build()
        artifact = build_runtime_request_artifact(delivery)
        prepared = prepare_deepseek_request(artifact)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "id": "wire-response-1", "model": MODEL_ID,
                    "choices": [{
                        "message": {"content": "Wire answer"}, "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3,
                    },
                }).encode("utf-8")

        def fake_urlopen(request, *, timeout):
            captured["body"] = request.data
            captured["timeout"] = timeout
            return Response()

        adapter = DeepSeekHTTPAdapter(timeout_seconds=7)
        with patch("memory.deepseek_adapter.load_deepseek_api_key", return_value="test-key"), patch(
            "memory.deepseek_adapter.urlopen", side_effect=fake_urlopen,
        ):
            response = adapter.send(prepared)
        self.assertEqual("Wire answer", response.content)
        self.assertEqual(prepared.wire_body, captured["body"])
        self.assertEqual(
            artifact["wire_body_sha256"], hashlib.sha256(captured["body"]).hexdigest(),
        )
        with self.assertRaisesRegex(DeepSeekProviderError, "request_artifact_invalid"):
            adapter.send({"payload": artifact["payload"]})

    def test_production_builder_delegates_to_shared_delivery_and_binds_actual_items(self):
        delivery = self.delivery_fixture.build()
        profile_ids = [
            item["profile_item_id"]
            for item in delivery["layers"]["approved_profile"]["items"]
        ]
        shared_binding = {
            "authority": "v8.0-d1-approved-profile-projection",
            "projection_protocol_version": "v8.0-d1-active-approved-profile-1",
            "projection_identity": "app_" + "a" * 64,
            "revision": 3,
            "decision_count": 3,
            "model_visible": bool(profile_ids),
            "profile_item_ids": profile_ids,
        }
        with patch("memory.runtime_thin_slice.build_production_delivery", return_value=delivery) as builder, patch(
            "memory.runtime_thin_slice.production_profile_binding", return_value=shared_binding,
        ):
            rebuilt, binding = _production_runtime_delivery(
                self.delivery_fixture.fixture.query, budget_profile="Large",
            )
        self.assertIs(delivery, rebuilt)
        builder.assert_called_once_with(
            self.delivery_fixture.fixture.query, budget_profile="Large",
        )
        self.assertEqual(profile_ids, binding["profile_item_ids"])
        self.assertEqual("v8.0-d1-approved-profile-projection", binding["authority"])
        artifact = build_runtime_request_artifact(
            rebuilt, approved_profile_binding=binding,
        )
        validate_runtime_request_artifact(artifact, trusted_delivery=rebuilt)

    def test_success_persists_one_append_only_run_and_exact_receipt_replay(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        first = core.execute(
            "What do I prefer?", request_id="request-0001", mutation_id="mutation-0001",
            budget_profile="Large",
        )
        self.assertEqual("succeeded", first["status"])
        self.assertEqual({"persisted": True, "sent_to_model": True, "external_model_calls": True}, first["execution"])
        self.assertEqual(14, first["usage"]["total_tokens"])
        self.assertEqual({"status": "reserved", "invoked": False}, first["candidate_hook"])

        second = core.execute(
            "What do I prefer?", request_id="request-0001", mutation_id="mutation-0001",
            budget_profile="Large",
        )
        self.assertEqual(first, second)
        self.assertEqual(1, adapter.calls)
        with closing(sqlite3.connect(self.sidecar)) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runtime_model_runs").fetchone()[0])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM runtime_messages").fetchone()[0])
            self.assertEqual(5, conn.execute("SELECT COUNT(*) FROM runtime_run_events").fetchone()[0])
            self.assertEqual(
                [
                    "state_prepared", "state_running", "send_attempt_started",
                    "provider_response_received", "state_succeeded",
                ],
                [row[0] for row in conn.execute(
                    "SELECT event_type FROM runtime_run_events ORDER BY sequence"
                )],
            )
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runtime_receipts").fetchone()[0])

    def test_idempotency_ids_reject_different_payload_or_partial_key_collision(self):
        core = self.core()
        core.execute("Question one", request_id="request-0002", mutation_id="mutation-0002")
        with self.assertRaises(RuntimeIdempotencyConflict):
            core.execute("Question two", request_id="request-0002", mutation_id="mutation-0002")
        with self.assertRaises(RuntimeIdempotencyConflict):
            core.execute("Question one", request_id="request-0002", mutation_id="mutation-OTHER")
        with self.assertRaises(RuntimeThinSliceError):
            core.execute("Question one", request_id=True, mutation_id="mutation-0003")
        with self.assertRaises(RuntimeThinSliceError):
            core.execute("Question one", request_id="request-0003", mutation_id=None)

    def test_same_ids_reject_changed_artifact_and_wire_body(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        first = core.execute(
            "Stable question", request_id="request-wire-1", mutation_id="mutation-wire-1",
        )
        with patch(
            "memory.runtime_thin_slice.SYSTEM_INSTRUCTION",
            "Changed instruction must produce different wire bytes.",
        ):
            with self.assertRaises(RuntimeIdempotencyConflict):
                core.execute(
                    "Stable question", request_id="request-wire-1",
                    mutation_id="mutation-wire-1",
                )
        replay = core.execute(
            "Stable question", request_id="request-wire-1", mutation_id="mutation-wire-1",
        )
        self.assertEqual(first, replay)
        self.assertEqual(1, adapter.calls)

    def test_provider_failure_is_sanitized_terminal_and_replayed_without_call(self):
        adapter = MockDeepSeekAdapter(failure=DeepSeekProviderError(
            "provider_http_error", category="provider", retryable=True, http_status=503,
        ))
        core = self.core(adapter)
        first = core.execute(
            "Will this fail?", request_id="request-0004", mutation_id="mutation-0004",
        )
        second = core.execute(
            "Will this fail?", request_id="request-0004", mutation_id="mutation-0004",
        )
        self.assertEqual(first, second)
        self.assertEqual("failed", first["status"])
        self.assertEqual("provider_http_error", first["error"]["code"])
        self.assertNotIn(str(self.sidecar), json.dumps(first))
        self.assertEqual(1, adapter.calls)
        with closing(sqlite3.connect(self.sidecar)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM runtime_messages WHERE role='assistant'").fetchone()[0])

    def test_concurrent_same_ids_create_only_one_run_and_one_reply(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def invoke():
            barrier.wait()
            try:
                results.append(core.execute(
                    "Concurrent request",
                    request_id="request-0007",
                    mutation_id="mutation-0007",
                ))
            except RuntimeRunInDoubt as exc:
                errors.append(exc)

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        final = core.execute(
            "Concurrent request", request_id="request-0007", mutation_id="mutation-0007",
        )
        self.assertEqual("succeeded", final["status"])
        self.assertTrue(results or errors)
        self.assertEqual(1, adapter.calls)
        with closing(sqlite3.connect(self.sidecar)) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runtime_model_runs").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runtime_messages WHERE role='assistant'").fetchone()[0])

    def test_interrupted_running_run_is_fail_closed_and_explicitly_recoverable(self):
        core = self.core(MockDeepSeekAdapter(failure=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            core.execute("Crash point", request_id="request-0005", mutation_id="mutation-0005")
        with closing(sqlite3.connect(self.sidecar)) as conn:
            model_run_id = conn.execute("SELECT model_run_id FROM runtime_model_runs").fetchone()[0]
        self.assertEqual("running", core.get_run(model_run_id)["status"])
        with self.assertRaises(RuntimeRunInDoubt):
            core.execute("Crash point", request_id="request-0005", mutation_id="mutation-0005")
        recovered = core.recover_running(model_run_id)
        self.assertEqual("failed", recovered["status"])
        self.assertEqual("interrupted_run_in_doubt", recovered["error"]["code"])
        self.assertEqual(recovered, core.recover_running(model_run_id))

    def test_recovery_distinguishes_interruption_before_send_attempt(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        with patch.object(
            core, "_append_send_attempt_started",
            side_effect=RuntimeThinSliceError("forced pre-send interruption"),
        ):
            with self.assertRaises(RuntimeThinSliceError):
                core.execute(
                    "Before send", request_id="request-before-send",
                    mutation_id="mutation-before-send",
                )
        with closing(sqlite3.connect(self.sidecar)) as conn:
            model_run_id = conn.execute(
                "SELECT model_run_id FROM runtime_model_runs WHERE request_id='request-before-send'"
            ).fetchone()[0]
        pending = core.get_run(model_run_id)
        self.assertEqual("not_attempted", pending["execution_phase"])
        self.assertEqual(
            {"persisted": True, "sent_to_model": False, "external_model_calls": False},
            pending["execution"],
        )
        recovered = core.recover_running(model_run_id)
        self.assertEqual("interrupted_before_send", recovered["error"]["code"])
        self.assertEqual("not_attempted_recovered", recovered["execution_phase"])
        self.assertEqual(0, adapter.calls)

    def test_provider_response_then_success_commit_failure_is_immediately_auditable(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        with patch.object(
            core, "_persist_success", side_effect=sqlite3.OperationalError("forced commit failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                core.execute(
                    "Commit failure", request_id="request-commit-1",
                    mutation_id="mutation-commit-1",
                )
        with closing(sqlite3.connect(self.sidecar)) as conn:
            model_run_id = conn.execute(
                "SELECT model_run_id FROM runtime_model_runs WHERE request_id='request-commit-1'"
            ).fetchone()[0]
            self.assertEqual(
                [
                    "state_prepared", "state_running", "send_attempt_started",
                    "provider_response_received",
                ],
                [row[0] for row in conn.execute(
                    "SELECT event_type FROM runtime_run_events ORDER BY sequence"
                )],
            )
        pending = core.get_run(model_run_id)
        self.assertEqual("running", pending["status"])
        self.assertEqual(
            "provider_response_received_local_commit_pending", pending["execution_phase"],
        )
        self.assertEqual(
            {"persisted": True, "sent_to_model": True, "external_model_calls": True},
            pending["execution"],
        )
        with self.assertRaises(RuntimeRunInDoubt):
            core.execute(
                "Commit failure", request_id="request-commit-1",
                mutation_id="mutation-commit-1",
            )
        self.assertEqual(1, adapter.calls)
        recovered = core.recover_running(model_run_id)
        self.assertEqual("failed", recovered["status"])
        self.assertEqual("provider_response_persist_in_doubt", recovered["execution_phase"])
        self.assertEqual("provider_response_persist_in_doubt", recovered["error"]["code"])
        self.assertEqual(1, adapter.calls)

    def test_all_ledger_tables_reject_update_and_delete(self):
        core = self.core()
        result = core.execute(
            "Append only", request_id="request-0006", mutation_id="mutation-0006",
        )
        tables = (
            "runtime_conversations", "runtime_request_artifacts", "runtime_model_runs",
            "runtime_messages", "runtime_run_events", "runtime_receipts",
        )
        with closing(sqlite3.connect(self.sidecar)) as conn:
            for table in tables:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(f"UPDATE {table} SET rowid=rowid")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(f"DELETE FROM {table}")
        self.assertEqual(result, core.get_run(result["model_run_id"]))

    def test_coordinated_offline_run_tamper_fails_identity_validation(self):
        core = self.core()
        result = core.execute(
            "Tamper proof", request_id="request-0008", mutation_id="mutation-0008",
        )
        with closing(sqlite3.connect(self.sidecar)) as conn:
            conn.execute("DROP TRIGGER runtime_model_runs_no_update")
            conn.execute(
                "UPDATE runtime_model_runs SET request_fingerprint=? WHERE model_run_id=?",
                ("rrf_" + "0" * 64, result["model_run_id"]),
            )
            conn.execute(_append_only_trigger_sql("runtime_model_runs", "UPDATE"))
            conn.commit()
        with self.assertRaises(RuntimeSidecarUnavailable):
            core.get_run(result["model_run_id"])

    def test_strict_initialization_and_production_core_gate(self):
        candidate = Path(self.temp.name) / "strict.db"
        for invalid in (1, "true", None):
            result = initialize_runtime_sidecar(
                candidate,
                execute=invalid,
                test_mode=True,
                allow_production_runtime_sidecar=False,
                confirm_production_runtime_sidecar_init=False,
            )
            self.assertEqual("confirmation_type_invalid", result["reason_code"])
            self.assertFalse(candidate.exists())
        with self.assertRaises(RuntimeThinSliceError):
            _RuntimeCore(
                sidecar_path=PRODUCTION_RUNTIME_SIDECAR,
                adapter=MockDeepSeekAdapter(),
                delivery_builder=self.builder,
            )
        authority = open_production_runtime_execution(
            execute=True, allow_external_model_call=True, confirm_deepseek_send=True,
        )
        with self.assertRaisesRegex(RuntimeThinSliceError, "test runtime primitive"):
            _RuntimeCore(
                sidecar_path=PRODUCTION_RUNTIME_SIDECAR,
                adapter=MockDeepSeekAdapter(),
                delivery_builder=self.builder,
                authority=authority,
            )
        for values in (
            {"execute": 1, "allow_external_model_call": True, "confirm_deepseek_send": True},
            {"execute": True, "allow_external_model_call": "true", "confirm_deepseek_send": True},
            {"execute": True, "allow_external_model_call": True, "confirm_deepseek_send": False},
        ):
            with self.assertRaises(RuntimeThinSliceError):
                open_production_runtime_execution(**values)

        production_candidate = Path(self.temp.name) / "patched-production.db"
        import memory.runtime_thin_slice as runtime_module

        with patch("memory.runtime_thin_slice.PRODUCTION_RUNTIME_SIDECAR", production_candidate):
            production_core = runtime_module._create_production_runtime_core(authority)
            self.assertEqual(production_candidate, production_core.sidecar_path)
            self.assertIsInstance(production_core.adapter, DeepSeekHTTPAdapter)
            self.assertIs(production_core.delivery_builder, runtime_module._production_runtime_delivery)
            self.assertFalse(production_candidate.exists())
            with self.assertRaisesRegex(RuntimeThinSliceError, "test runtime primitive"):
                _RuntimeCore(
                    sidecar_path=production_candidate,
                    adapter=MockDeepSeekAdapter(),
                    delivery_builder=self.builder,
                    authority=authority,
                )
            partial = initialize_runtime_sidecar(
                production_candidate,
                execute=True,
                allow_production_runtime_sidecar=True,
                confirm_production_runtime_sidecar_init=False,
            )
            self.assertEqual("production_confirmation_required", partial["reason_code"])
            self.assertFalse(production_candidate.exists())
            invalid = initialize_runtime_sidecar(
                production_candidate,
                execute=True,
                allow_production_runtime_sidecar=True,
                confirm_production_runtime_sidecar_init=1,
            )
            self.assertEqual("confirmation_type_invalid", invalid["reason_code"])
            self.assertFalse(production_candidate.exists())
            initialized = initialize_runtime_sidecar(
                production_candidate,
                execute=True,
                allow_production_runtime_sidecar=True,
                confirm_production_runtime_sidecar_init=True,
            )
            self.assertEqual("initialized", initialized["status"])

    def test_preview_does_not_initialize_or_send(self):
        adapter = MockDeepSeekAdapter()
        core = self.core(adapter)
        preview = core.preview("Preview only", budget_profile="Large")
        self.assertEqual("preview", preview["status"])
        self.assertEqual({
            "persisted": False, "sent_to_model": False,
            "external_model_calls": False, "credential_read": False,
        }, preview["execution"])
        self.assertEqual(0, adapter.calls)

        existed = PRODUCTION_RUNTIME_SIDECAR.exists()
        before = PRODUCTION_RUNTIME_SIDECAR.read_bytes() if existed else None
        delivery = self.delivery_fixture.build()
        import memory.runtime_thin_slice as runtime_module

        with patch(
            "memory.runtime_thin_slice._production_runtime_delivery",
            return_value=(delivery, runtime_module._default_profile_binding(delivery)),
        ):
            production_preview = RuntimeThinSliceService().preview("Preview production")
        self.assertFalse(production_preview["execution"]["persisted"])
        self.assertEqual(existed, PRODUCTION_RUNTIME_SIDECAR.exists())
        if existed:
            self.assertEqual(before, PRODUCTION_RUNTIME_SIDECAR.read_bytes())

    def test_adapter_module_has_no_database_or_memory_control_dependency(self):
        import memory.deepseek_adapter as adapter_module

        source = inspect.getsource(adapter_module)
        imported_roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertTrue({
            "sqlite3", "memory", "retrieval", "profile", "candidate", "approval",
        }.isdisjoint(imported_roots))

    def test_deepseek_response_parser_enforces_usage_and_model(self):
        payload = {
            "id": "response-1", "model": MODEL_ID,
            "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        parsed = parse_deepseek_response(payload)
        self.assertEqual(5, parsed.usage["total_tokens"])
        bad = deepcopy(payload)
        bad["usage"]["total_tokens"] = 99
        with self.assertRaises(DeepSeekProviderError):
            parse_deepseek_response(bad)

    def test_privacy_manifest_keeps_credentials_and_other_databases_out(self):
        manifest = runtime_privacy_manifest()
        self.assertFalse(manifest["api_key_persisted"])
        self.assertFalse(manifest["api_key_logged"])
        self.assertFalse(manifest["main_chat_database_writes"])
        self.assertFalse(manifest["profile_sidecar_writes"])
        self.assertFalse(manifest["response_memory_promotion"])
        self.assertEqual("ready", runtime_sidecar_status(self.sidecar)["status"])

    def test_cli_and_api_are_fixed_source_and_runtime_admin_gated(self):
        preview_args = parse_runtime_args(["preview", "--question", "hello"])
        self.assertFalse(hasattr(preview_args, "db"))
        self.assertFalse(hasattr(preview_args, "provider"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_runtime_args(["preview", "--question", "hello", "--db", "other.db"])
        init_args = parse_init_args([])
        self.assertFalse(init_args.execute)
        self.assertIsNone(init_args.test_sidecar)

        service = Mock()
        service.preview.return_value = {"status": "preview"}
        with patch("tools.runtime_thin_slice.RuntimeThinSliceService", return_value=service):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, runtime_cli_main(["preview", "--question", "hello"]))
        self.assertEqual("preview", json.loads(output.getvalue())["status"])

        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(
            readonly=True, runtime_admin=False, runtime_execution_session=None,
            runtime_execution_service=Mock(), db_mode="real", db_path=REAL_DB_PATH,
        )
        handler.send_json = Mock()
        handler.send_error_json = Mock()
        payload = {
            "q": "hello", "budget": "Medium",
            "request_id": "request-api-1", "mutation_id": "mutation-api-1",
            "expected_bindings": {"binding": "reviewed"}, "confirmed": True,
        }
        handler.api_runtime_execute(payload)
        denied, status = handler.send_json.call_args.args
        self.assertEqual(("runtime_admin_required", HTTPStatus.FORBIDDEN), (
            denied["reason_code"], status,
        ))

        handler.send_json.reset_mock()
        handler.api_runtime_execute({**payload, "provider": "other"})
        handler.send_error_json.assert_called_with(
            "Runtime execution fields mismatch", HTTPStatus.BAD_REQUEST,
        )

        authority = object()
        production_service = Mock()
        production_service.execute.return_value = {"status": "succeeded"}
        handler.app = Mock(
            readonly=True, runtime_admin=True, runtime_execution_session=authority,
            runtime_execution_service=production_service,
            db_mode="real", db_path=REAL_DB_PATH,
        )
        handler.api_runtime_execute(payload)
        handler.send_json.assert_called_with({"status": "succeeded"})
        production_service.execute.assert_called_once()

        do_post_source = inspect.getsource(AyunOSHandler.do_POST)
        self.assertIn("runtime_admin_mutation", do_post_source)
        self.assertIn("self.app.runtime_admin", do_post_source)


if __name__ == "__main__":
    unittest.main()
