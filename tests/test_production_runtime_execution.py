from __future__ import annotations

import ast
import base64
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.deepseek_v4_adapter import (
    BASE_URL,
    MODEL_ID,
    PROVIDER_ID,
    DeepSeekV4ProviderError,
    DeepSeekV4Response,
    _parse_production_private_credential,
)
from memory.production_delivery import production_profile_binding
from memory.production_runtime_execution import (
    ProductionRuntimeExecutionError,
    ProductionRuntimeExecutionService,
    RuntimeIdempotencyConflict,
    RuntimePreviewMismatch,
    RuntimeSidecarUnavailable,
    _RuntimeCore,
)
from memory.runtime_enablement import initialize_runtime_enablement_sidecar
from memory.runtime_protocol_contracts import (
    REVIEWED_EXECUTION_BINDING_VERSION,
    build_reviewed_execution_binding,
    build_runtime_artifact,
    canonical_digest,
    production_assembly_identity,
    production_delivery_identity,
    validate_reviewed_execution_binding,
    validate_runtime_ledger,
)
from tests import test_production_delivery as production_delivery_tests
from tests.test_production_delivery import QUESTION
from tools.local_web import AyunOSHandler, REAL_DB_PATH


LEDGER_TABLES = (
    "runtime_conversations", "runtime_request_artifacts", "runtime_model_runs",
    "runtime_messages", "runtime_run_events", "runtime_errors", "runtime_receipts",
)


class MockV4Adapter:
    provider_id = PROVIDER_ID
    model_id = MODEL_ID
    base_url = BASE_URL

    def __init__(self, *, failure: DeepSeekV4ProviderError | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def send(self, _prepared) -> DeepSeekV4Response:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return DeepSeekV4Response(
            provider_response_id="mock-response-1",
            model=MODEL_ID,
            content="A source-backed mock answer.",
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            finish_reason="stop",
        )


class CountingAdapterFactory:
    def __init__(self, *, failure: DeepSeekV4ProviderError | None = None) -> None:
        self.constructor_calls = 0
        self.failure = failure
        self.adapter: MockV4Adapter | None = None

    def __call__(self) -> MockV4Adapter:
        self.constructor_calls += 1
        self.adapter = MockV4Adapter(failure=self.failure)
        return self.adapter


class ProductionRuntimeExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.sidecar = Path(self.temp.name) / "runtime-d1.db"
        initialized = initialize_runtime_enablement_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )
        self.assertEqual("initialized", initialized["status"])
        self.delivery_fixture = production_delivery_tests.ProductionDeliveryTest(
            "test_delivery_assembly_and_runtime_bindings_cover_exact_contents"
        )
        self.delivery_fixture.setUp()

    def tearDown(self) -> None:
        self.delivery_fixture.tearDown()
        self.temp.cleanup()

    def builder(self, _query, *, budget_profile: str):
        self.assertEqual("Large", budget_profile)
        delivery = self.delivery_fixture.build()
        return delivery, production_profile_binding(delivery)

    def core(self, factory: CountingAdapterFactory) -> _RuntimeCore:
        core = _RuntimeCore(
            sidecar_path=self.sidecar,
            adapter_factory=factory,
            delivery_builder=self.builder,
            production=False,
            mock_transport=True,
        )
        core.test_factory = factory
        return core

    def preview(self, core: _RuntimeCore) -> dict:
        return core.preview(
            QUESTION,
            budget_profile="Large",
            request_id="request-runtime-g-0001",
            mutation_id="mutation-runtime-g-0001",
        )

    def execute(self, core: _RuntimeCore, preview: dict, **overrides):
        values = {
            "question": QUESTION,
            "budget_profile": "Large",
            "request_id": preview["request_id"],
            "mutation_id": preview["mutation_id"],
            "expected_bindings": preview["reviewed_execution_binding"],
            "confirmed": True,
        }
        values.update(overrides)
        question = values.pop("question")
        return core.execute(question, **values)

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(self.sidecar) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in LEDGER_TABLES
            }

    @staticmethod
    def reseal_reviewed_binding(binding: dict) -> dict:
        value = deepcopy(binding)
        reviewed = value["reviewed_artifact"]
        reviewed["identity"] = "rva_" + canonical_digest({
            key: item for key, item in reviewed.items() if key != "identity"
        })
        value["idempotency_fingerprint"] = "rif_" + canonical_digest({
            "question_utf8_sha256": reviewed["question"]["utf8_sha256"],
            "budget": reviewed["budget"],
            "reviewed_artifact_identity": reviewed["identity"],
            "wire": reviewed["wire"],
        })
        value["identity"] = "reb_" + canonical_digest({
            key: item for key, item in value.items() if key != "identity"
        })
        return validate_reviewed_execution_binding(value)

    @staticmethod
    def diagnostic_variant(delivery: dict) -> dict:
        changed = deepcopy(delivery)
        changed["capability_state"]["capabilities"]["semantic"]["reason_code"] = (
            "semantic_dependency_unavailable"
        )
        changed["source_assembly"]["lexical_fallback"]["raw_match_count"] += 1
        changed["source_assembly"]["assembly_identity"] = production_assembly_identity(
            changed["source_assembly"]
        )
        changed["assembly_binding"]["assembly_identity"] = changed["source_assembly"][
            "assembly_identity"
        ]
        changed["delivery_identity"] = production_delivery_identity(changed)
        return changed

    def test_preview_exposes_complete_fixed_d1_review_without_side_effects(self):
        factory = CountingAdapterFactory()
        preview = self.preview(self.core(factory))
        self.assertEqual("preview", preview["status"])
        self.assertEqual(QUESTION, preview["question"])
        self.assertEqual("deepseek-v4-flash", preview["runtime_profile"]["model"])
        self.assertFalse(preview["runtime_profile"]["stream"])
        self.assertEqual({"type": "disabled"}, preview["runtime_profile"]["thinking"])
        self.assertEqual(4, preview["review"]["approved_profile_included"])
        self.assertTrue(preview["delivery"]["model_context"])
        self.assertEqual(
            preview["diagnostic_bindings"]["artifact_identity"],
            preview["request_artifact"]["artifact_identity"],
        )
        self.assertEqual(
            REVIEWED_EXECUTION_BINDING_VERSION,
            preview["reviewed_execution_binding"]["version"],
        )
        self.assertNotEqual(
            preview["diagnostic_bindings"]["artifact_identity"],
            preview["reviewed_execution_binding"]["reviewed_artifact"]["identity"],
        )
        self.assertNotIn("payload", preview["request_artifact"])
        self.assertNotIn("wire_body_base64", preview["request_artifact"])
        self.assertEqual(
            "rqa_8e36c059c28c2e8182a3bec433faa345fd3a41ff97e269c62e2fc79ee6b62842",
            preview["request_artifact"]["artifact_identity"],
        )
        self.assertEqual(
            "reb_89262db1ca9382b0ae17f75a1b2f4ba94adb55e070b604c285877df19e5a9d12",
            preview["reviewed_execution_binding"]["identity"],
        )
        self.assertEqual(0, factory.constructor_calls)
        self.assertTrue(all(value == 0 for value in self.counts().values()))

    def test_confirmed_success_appends_one_valid_d1_ledger_and_never_retries(self):
        factory = CountingAdapterFactory()
        core = self.core(factory)
        preview = self.preview(core)
        envelope = self.execute(core, preview)
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(18, envelope["result"]["usage"]["total_tokens"])
        self.assertEqual(
            "rrc_0ccc98450e27f1f3dd3a0b58a005054fd77a59f6db8147162b7db6c80c568e0c",
            envelope["result"]["receipt_identity"],
        )
        self.assertEqual(1, factory.constructor_calls)
        self.assertEqual(1, factory.adapter.calls)
        self.assertEqual({
            "adapter_constructor_count_this_request": 1,
            "adapter_call_count_this_request": 1,
            "model_call_count_for_run": 1,
            "exactly_one_call": True,
            "automatic_retry": False,
            "mock_transport": True,
            "network_request_count": 0,
            "credential_read": False,
        }, envelope["transport"])
        self.assertEqual({
            "runtime_conversations": 1,
            "runtime_request_artifacts": 1,
            "runtime_model_runs": 1,
            "runtime_messages": 2,
            "runtime_run_events": 5,
            "runtime_errors": 0,
            "runtime_receipts": 1,
        }, self.counts())
        with sqlite3.connect(self.sidecar) as connection:
            connection.row_factory = sqlite3.Row
            snapshot = {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in LEDGER_TABLES
            }
            validated = validate_runtime_ledger(snapshot)
            event_types = [
                row[0] for row in connection.execute(
                    "SELECT event_type FROM runtime_run_events ORDER BY sequence"
                )
            ]
        self.assertTrue(validated["sent_to_model"])
        self.assertEqual([
            "state_prepared", "state_running", "send_attempt_started",
            "provider_response_received", "state_succeeded",
        ], event_types)

        repeated = self.execute(core, preview)
        self.assertEqual("succeeded", repeated["status"])
        self.assertEqual(0, repeated["transport"]["adapter_constructor_count_this_request"])
        self.assertEqual(0, repeated["transport"]["adapter_call_count_this_request"])
        self.assertEqual(1, factory.constructor_calls)
        self.assertEqual(1, factory.adapter.calls)

    def test_every_preview_binding_and_confirmation_fail_closed_before_write_or_call(self):
        factory = CountingAdapterFactory()
        core = self.core(factory)
        preview = self.preview(core)
        with self.assertRaises(RuntimePreviewMismatch):
            self.execute(core, preview, confirmed=False)
        for field in ("version", "identity", "idempotency_fingerprint"):
            tampered = deepcopy(preview["reviewed_execution_binding"])
            tampered[field] = tampered[field] + "x"
            with self.subTest(field=field), self.assertRaises(RuntimePreviewMismatch):
                self.execute(core, preview, expected_bindings=tampered)
        missing = deepcopy(preview["reviewed_execution_binding"])
        missing.pop("identity")
        with self.assertRaises(RuntimePreviewMismatch):
            self.execute(core, preview, expected_bindings=missing)
        self.assertEqual(0, factory.constructor_calls)
        self.assertTrue(all(value == 0 for value in self.counts().values()))

    def test_reviewed_binding_is_stable_across_diagnostic_capability_and_assembly_drift(self):
        original = self.delivery_fixture.build()
        changed = self.diagnostic_variant(original)

        original_artifact = build_runtime_artifact(
            original, approved_profile_binding=production_profile_binding(original),
        )
        changed_artifact = build_runtime_artifact(
            changed, approved_profile_binding=production_profile_binding(changed),
        )
        original_binding = build_reviewed_execution_binding(
            original,
            original_artifact,
            request_id="request-runtime-g-stable-1",
            mutation_id="mutation-runtime-g-stable-1",
        )
        changed_binding = build_reviewed_execution_binding(
            changed,
            changed_artifact,
            request_id="request-runtime-g-stable-1",
            mutation_id="mutation-runtime-g-stable-1",
        )
        self.assertNotEqual(original["delivery_identity"], changed["delivery_identity"])
        self.assertNotEqual(
            original["source_assembly"]["assembly_identity"],
            changed["source_assembly"]["assembly_identity"],
        )
        self.assertNotEqual(
            original_artifact["artifact_identity"], changed_artifact["artifact_identity"],
        )
        self.assertEqual(original_binding, changed_binding)

    def test_execute_accepts_diagnostic_rebuild_drift_when_reviewed_send_semantics_match(self):
        original = self.delivery_fixture.build()
        changed = self.diagnostic_variant(original)
        deliveries = [original, changed]

        def drifting_builder(_query, *, budget_profile: str):
            self.assertEqual("Large", budget_profile)
            delivery = deliveries.pop(0)
            return delivery, production_profile_binding(delivery)

        factory = CountingAdapterFactory()
        core = _RuntimeCore(
            sidecar_path=self.sidecar,
            adapter_factory=factory,
            delivery_builder=drifting_builder,
            production=False,
            mock_transport=True,
        )
        preview = self.preview(core)
        envelope = self.execute(core, preview)
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(1, factory.constructor_calls)
        self.assertEqual(1, factory.adapter.calls)
        self.assertFalse(deliveries)

    def test_every_authorized_content_binding_change_is_rebuilt_and_rejected_before_constructor(self):
        factory = CountingAdapterFactory()
        core = self.core(factory)
        preview = self.preview(core)
        baseline = preview["reviewed_execution_binding"]
        actual_context = preview["delivery"]["model_context"]
        actual_artifact = build_runtime_artifact(
            preview["delivery"],
            approved_profile_binding=production_profile_binding(preview["delivery"]),
        )
        changed_wire = bytearray(base64.b64decode(actual_artifact["wire_body_base64"]))
        changed_wire[-1] ^= 1

        def change_question(value):
            text = "A different reviewed question"
            encoded = text.encode("utf-8")
            value["reviewed_artifact"]["question"] = {
                "text": text,
                "utf8_sha256": hashlib.sha256(encoded).hexdigest(),
                "utf8_bytes": len(encoded),
            }

        mutations = {
            "question_utf8": change_question,
            "budget_and_version": lambda value: value["reviewed_artifact"]["budget"].update(
                {"profile": "Medium"}
            ),
            "profile_projection": lambda value: value["reviewed_artifact"][
                "approved_profile"
            ].update({"projection_identity": "app_" + "0" * 64}),
            "profile_item_id": lambda value: value["reviewed_artifact"][
                "approved_profile"
            ]["profile_item_ids"].__setitem__(0, "upi_" + "0" * 64),
            "profile_item_order": lambda value: value["reviewed_artifact"][
                "approved_profile"
            ].update({
                "profile_item_ids": list(reversed(
                    value["reviewed_artifact"]["approved_profile"]["profile_item_ids"]
                ))
            }),
            "evidence_reference_id": lambda value: value["reviewed_artifact"][
                "evidence"
            ][0].update({"evidence_reference_id": "pde_" + "0" * 64}),
            "evidence_order": lambda value: value["reviewed_artifact"].update({
                "evidence": list(reversed(value["reviewed_artifact"]["evidence"]))
            }),
            "source_anchor": lambda value: value["reviewed_artifact"]["evidence"][0][
                "source_anchor"
            ].update({"conversation_title": "changed-title"}),
            "model_context": lambda value: value["reviewed_artifact"]["model_context"].update(
                {
                    "sha256": hashlib.sha256(
                        (actual_context[:-1] + ("X" if actual_context[-1] != "X" else "Y")).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "utf8_bytes": len(
                        (actual_context[:-1] + ("X" if actual_context[-1] != "X" else "Y")).encode(
                            "utf-8"
                        )
                    ),
                }
            ),
            "wire": lambda value: value["reviewed_artifact"]["wire"].update(
                {"sha256": hashlib.sha256(changed_wire).hexdigest(), "bytes": len(changed_wire)}
            ),
            "request_id": lambda value: value.update({"request_id": "request-runtime-g-other"}),
            "mutation_id": lambda value: value.update({"mutation_id": "mutation-runtime-g-other"}),
        }
        for name, mutate in mutations.items():
            tampered = deepcopy(baseline)
            mutate(tampered)
            tampered = self.reseal_reviewed_binding(tampered)
            self.assertNotEqual(baseline["identity"], tampered["identity"])
            with self.subTest(name=name), self.assertRaises(RuntimePreviewMismatch):
                self.execute(core, preview, expected_bindings=tampered)

        for name, path, value in (
            ("provider", ("runtime_profile", "provider"), "other"),
            ("renderer", ("renderer", "version"), "other"),
            ("artifact_protocol", None, "other"),
        ):
            tampered = deepcopy(baseline)
            reviewed = tampered["reviewed_artifact"]
            if path is None:
                reviewed["artifact_protocol"] = value
            else:
                reviewed[path[0]][path[1]] = value
            with self.subTest(name=name), self.assertRaises(RuntimePreviewMismatch):
                self.execute(core, preview, expected_bindings=tampered)
        self.assertEqual(0, factory.constructor_calls)
        self.assertTrue(all(value == 0 for value in self.counts().values()))

    def test_idempotency_conflict_and_readonly_rejection_construct_nothing(self):
        first_factory = CountingAdapterFactory()
        first_core = self.core(first_factory)
        first_preview = self.preview(first_core)
        self.execute(first_core, first_preview)
        self.assertEqual(1, first_factory.constructor_calls)

        conflict_factory = CountingAdapterFactory()
        conflict_core = self.core(conflict_factory)
        conflict_preview = conflict_core.preview(
            QUESTION,
            budget_profile="Large",
            request_id=first_preview["request_id"],
            mutation_id="mutation-runtime-g-conflict",
        )
        with self.assertRaises(RuntimeIdempotencyConflict):
            self.execute(conflict_core, conflict_preview)
        self.assertEqual(0, conflict_factory.constructor_calls)

        readonly_factory = CountingAdapterFactory()
        readonly_core = self.core(readonly_factory)
        readonly_preview = readonly_core.preview(
            QUESTION,
            budget_profile="Large",
            request_id="request-runtime-g-missing",
            mutation_id="mutation-runtime-g-missing",
        )
        self.sidecar.unlink()
        with self.assertRaises(RuntimeSidecarUnavailable):
            self.execute(readonly_core, readonly_preview)
        self.assertEqual(0, readonly_factory.constructor_calls)

    def test_rejected_preflight_never_reaches_credential_http_or_network_surfaces(self):
        with (
            patch("memory.deepseek_v4_adapter.load_production_deepseek_api_key") as credential,
            patch("memory.deepseek_v4_adapter.Request") as http_request,
            patch("memory.deepseek_v4_adapter.urlopen") as network,
        ):
            rejected_factory = CountingAdapterFactory()
            rejected_core = self.core(rejected_factory)
            rejected_preview = self.preview(rejected_core)
            with self.assertRaises(RuntimePreviewMismatch):
                self.execute(rejected_core, rejected_preview, confirmed=False)
            tampered = deepcopy(rejected_preview["reviewed_execution_binding"])
            tampered["identity"] = "reb_" + "0" * 64
            with self.assertRaises(RuntimePreviewMismatch):
                self.execute(rejected_core, rejected_preview, expected_bindings=tampered)
            self.assertEqual(0, rejected_factory.constructor_calls)

            legal_factory = CountingAdapterFactory()
            legal_core = self.core(legal_factory)
            self.execute(legal_core, self.preview(legal_core))
            conflict_factory = CountingAdapterFactory()
            conflict_core = self.core(conflict_factory)
            conflict_preview = conflict_core.preview(
                QUESTION,
                budget_profile="Large",
                request_id="request-runtime-g-0001",
                mutation_id="mutation-runtime-g-conflict-2",
            )
            with self.assertRaises(RuntimeIdempotencyConflict):
                self.execute(conflict_core, conflict_preview)
            self.assertEqual(0, conflict_factory.constructor_calls)

            missing_factory = CountingAdapterFactory()
            missing_core = self.core(missing_factory)
            missing_preview = missing_core.preview(
                QUESTION,
                budget_profile="Large",
                request_id="request-runtime-g-missing-2",
                mutation_id="mutation-runtime-g-missing-2",
            )
            self.sidecar.unlink()
            with self.assertRaises(RuntimeSidecarUnavailable):
                self.execute(missing_core, missing_preview)
            self.assertEqual(0, missing_factory.constructor_calls)

            credential.assert_not_called()
            http_request.assert_not_called()
            network.assert_not_called()

    def test_provider_failure_is_terminal_redacted_and_single_attempt(self):
        factory = CountingAdapterFactory(failure=DeepSeekV4ProviderError(
            "provider_http_error", category="provider", retryable=True, http_status=503,
        ))
        core = self.core(factory)
        envelope = self.execute(core, self.preview(core))
        self.assertEqual("failed", envelope["status"])
        self.assertEqual("provider_failed", envelope["execution_phase"])
        self.assertEqual("provider_http_error", envelope["result"]["error"]["code"])
        self.assertEqual(1, factory.constructor_calls)
        self.assertEqual(1, factory.adapter.calls)
        self.assertFalse(envelope["transport"]["automatic_retry"])
        self.assertEqual(1, self.counts()["runtime_errors"])
        self.assertEqual(1, self.counts()["runtime_receipts"])

    def test_fixed_private_loader_is_exact_and_d1_execution_has_no_bc_dependency(self):
        self.assertEqual("secret", _parse_production_private_credential("DEEPSEEK_API_KEY=secret\n"))
        for raw in (
            "DEEPSEEK_API_KEY=", "OTHER=value", "# comment\nDEEPSEEK_API_KEY=value",
            "DEEPSEEK_API_KEY=a\nDEEPSEEK_API_KEY=b", " DEEPSEEK_API_KEY=value",
        ):
            with self.subTest(raw=raw), self.assertRaises(DeepSeekV4ProviderError):
                _parse_production_private_credential(raw)

        import memory.deepseek_v4_adapter as adapter_module
        import memory.production_runtime_execution as execution_module
        import tools.local_web as web_module

        combined = "\n".join((
            inspect.getsource(execution_module),
            inspect.getsource(web_module),
        ))
        self.assertNotIn("runtime_thin_slice", combined)
        self.assertNotIn("RuntimeThinSliceService", combined)
        self.assertNotIn("deepseek-chat", combined)
        adapter_source = inspect.getsource(adapter_module)
        self.assertNotIn("memory.deepseek_adapter", adapter_source)
        self.assertNotIn("os.environ", adapter_source)
        signature = inspect.signature(ProductionRuntimeExecutionService.execute)
        self.assertNotIn("sidecar_path", signature.parameters)
        self.assertNotIn("adapter", signature.parameters)
        self.assertNotIn("adapter_factory", signature.parameters)
        self.assertNotIn("provider", signature.parameters)
        service_source = inspect.getsource(ProductionRuntimeExecutionService)
        self.assertNotIn("DeepSeekV4FlashHTTPAdapter()", service_source)
        execute_source = inspect.getsource(_RuntimeCore.execute)
        self.assertIn("return self._execute_built", execute_source)
        execute_source = inspect.getsource(_RuntimeCore._execute_built)
        self.assertLess(
            execute_source.index("write=False"),
            execute_source.index("adapter = self._construct_adapter()"),
        )
        self.assertLess(
            execute_source.index("adapter = self._construct_adapter()"),
            execute_source.index("write=True"),
        )
        self.assertLess(execute_source.index("write=True"), execute_source.index("adapter.send"))

    def test_web_api_requires_admin_and_exact_confirmation_envelope(self):
        handler = object.__new__(AyunOSHandler)
        service = Mock()
        handler.send_json = Mock()
        handler.send_error_json = Mock()
        handler.app = Mock(
            readonly=True,
            runtime_admin=False,
            runtime_execution_session=None,
            runtime_execution_service=service,
            db_mode="real",
            db_path=REAL_DB_PATH,
        )
        preview = self.preview(self.core(CountingAdapterFactory()))
        payload = {
            "q": QUESTION,
            "budget": "Large",
            "request_id": preview["request_id"],
            "mutation_id": preview["mutation_id"],
            "expected_bindings": preview["reviewed_execution_binding"],
            "confirmed": True,
        }
        handler.api_runtime_execute(payload)
        self.assertEqual("runtime_admin_required", handler.send_json.call_args.args[0]["reason_code"])
        service.execute.assert_not_called()

        authority = object()
        handler.send_json.reset_mock()
        handler.app.runtime_admin = True
        handler.app.runtime_execution_session = authority
        service.execute.return_value = {"status": "succeeded"}
        handler.api_runtime_execute(payload)
        service.execute.assert_called_once_with(
            QUESTION,
            request_id=preview["request_id"],
            mutation_id=preview["mutation_id"],
            budget_profile="Large",
            expected_bindings=preview["reviewed_execution_binding"],
            confirmed=True,
            authority=authority,
        )
        handler.send_json.assert_called_with({"status": "succeeded"})

        handler.send_error_json.reset_mock()
        handler.api_runtime_execute({**payload, "provider": "injected"})
        handler.send_error_json.assert_called_with(
            "Runtime execution fields mismatch", unittest.mock.ANY,
        )

    def test_ui_has_preview_in_readonly_and_confirmation_gates_send(self):
        app_source = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('view: "runtimeExecution"', app_source)
        for marker in (
            't("runtime.completeModelContext"',
            't("runtime.approvedProfileItems"',
            't("runtime.historicalEvidenceAnchors"',
            't("runtime.explicitConfirmation"',
            "expected_bindings",
            't("runtime.executingOnce"',
            't("runtime.executeMockConfirmed"',
            '"runtime.readonlyPreviewComplete"',
            't("runtime.reviewedExecutionBinding"',
            't("runtime.diagnosticIdentities"',
            "saved.preview.reviewed_execution_binding",
            'checkboxInput("runtime-execution-confirm", false)',
            "send.disabled = true",
            "send.disabled = !sidecarReady || !checkbox.checked",
        ):
            self.assertIn(marker, app_source)
        tree = ast.parse(inspect.getsource(ProductionRuntimeExecutionService))
        names = {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
        self.assertTrue({"question", "request_id", "mutation_id", "expected_bindings"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
