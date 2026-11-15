from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.cognitive_orchestration import (
    CognitiveContext,
    CognitiveModelRequest,
    ContextBinding,
    ContextBudget,
    ModelBudget,
    ModelCallReservation,
    ProviderBinding,
    canonical_json,
    estimate_tokens,
)
from memory.cognitive_external_send import (
    ExternalSendConsentError,
    V10_F_USER_AUTHORIZATION_TEXT_SHA256,
    V10_F_USER_AUTHORIZATION_UTTERANCE,
    create_external_send_consent,
    create_trusted_external_send_consent,
    create_trusted_user_event,
    open_v10_f_trusted_local_controller,
)
from memory.cognitive_provider import (
    CognitiveProviderError,
    ProviderHealthAttestation,
    ProviderSelection,
    build_suggested_provider_registry,
)
from memory.cognitive_runtime_bridge import (
    CognitiveRuntimeBridgeError,
    CognitiveRuntimePreview,
    ExternalSendDisclosure,
    PRODUCTION_BUDGET_PROFILE,
    PRODUCTION_FIXED_OUTPUT_TOKENS,
    PRODUCTION_MODEL_ID,
    PRODUCTION_PROVIDER_ID,
    PRODUCTION_PROVIDER_PROFILE_DIGEST,
    PRODUCTION_PROVIDER_PROFILE_VERSION,
    ProductionCognitiveRuntimeBridge,
    create_mock_cognitive_runtime_bridge,
)
from memory.runtime_enablement import initialize_runtime_enablement_sidecar


LEDGER_TABLES = (
    "runtime_conversations",
    "runtime_request_artifacts",
    "runtime_model_runs",
    "runtime_messages",
    "runtime_run_events",
    "runtime_errors",
    "runtime_receipts",
)
RUNTIME_REQUEST_ID = "request:v10-c-runtime-0001"
RUNTIME_MUTATION_ID = "mutation:v10-c-runtime-0001"


class CognitiveRuntimeBridgeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.sidecar = Path(self.temp.name) / "runtime-v10-c.db"
        initialized = initialize_runtime_enablement_sidecar(
            self.sidecar,
            execute=True,
            test_mode=True,
        )
        self.assertEqual("initialized", initialized["status"])
        self.selection = self.make_production_selection()
        self.provider_binding = ProviderBinding(selection=self.selection)
        self.objective_id = "objective:v10-c-runtime-bridge"
        self.objective_digest = "a" * 64
        self.context = self.make_context()
        self.budget = ModelBudget(
            maximum_model_calls=1,
            maximum_input_tokens=64_512,
            maximum_output_tokens=PRODUCTION_FIXED_OUTPUT_TOKENS,
            maximum_total_tokens=65_536,
            maximum_cost_microunits=1_000_000,
        )
        self.request = self.make_request()
        self.reservation = self.make_reservation()
        baseline_bridge = self.bridge()
        self.disclosure = baseline_bridge.prepare_disclosure(
            self.selection,
            self.request,
            self.reservation,
            request_id=RUNTIME_REQUEST_ID,
            mutation_id=RUNTIME_MUTATION_ID,
        )
        self.consent = self.make_consent(
            self.selection,
            self.request,
            self.reservation,
            self.disclosure,
        )
        self._preview_consents = {}

    def make_production_selection(
        self,
        *,
        evidence_digest: str = "c" * 64,
        checked_at: str | None = None,
        expires_at: str | None = None,
        evaluated_at: str | None = None,
    ) -> ProviderSelection:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        checked_at = checked_at or (
            (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        )
        expires_at = expires_at or (
            (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        )
        evaluated_at = evaluated_at or now.isoformat().replace("+00:00", "Z")
        registry = build_suggested_provider_registry(
            ("secret.deepseek.production",)
        )
        profile = registry.get(
            PRODUCTION_PROVIDER_ID,
            PRODUCTION_PROVIDER_PROFILE_VERSION,
        )
        self.assertEqual(PRODUCTION_PROVIDER_PROFILE_DIGEST, profile.profile_digest)
        attestation = ProviderHealthAttestation(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_state="healthy",
            checked_at=checked_at,
            expires_at=expires_at,
            evidence_digest=evidence_digest,
        )
        return registry.manual_select(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_attestation=attestation,
            evaluated_at=evaluated_at,
        )

    def make_consent(
        self,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        disclosure: ExternalSendDisclosure,
        *,
        granted_at: str | None = None,
        expires_at: str | None = None,
    ):
        granted_at = granted_at or disclosure.prepared_at
        expires_at = expires_at or disclosure.expires_at
        return create_external_send_consent(
            consent_id=f"consent:test:{request.request_digest[:16]}",
            user_utterance="Authorize this one test-only discussion send.",
            disclosure_digest="d" * 64,
            selection=selection,
            request=request,
            reservation=reservation,
            **disclosure.consent_binding(),
            granted_at=granted_at,
            expires_at=expires_at,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_context(
        self,
        *,
        source_names=("v7_memory", "v9_now", "v9_resume"),
    ) -> CognitiveContext:
        content_by_source = {
            "v7_memory": {
                "fact": "Validated memory keeps Search local and AI explicit."
            },
            "v9_now": {
                "current_phase": "provider orchestration",
                "state": "active",
            },
            "v9_resume": {
                "next_approved_step": "one reviewed DeepSeek discussion",
                "future_nodes_authorized": False,
            },
        }
        bindings = []
        sources = []
        for index, source in enumerate(source_names):
            binding = ContextBinding(
                binding_id=f"ctxbind:{source}:0001",
                objective_id=self.objective_id,
                source_system=source,
                source_reference=f"{source}:fixture:reviewed",
                source_digest=f"{index + 1:x}" * 64,
                purpose=f"Bind reviewed {source} data.",
                selected_items=1,
                canonical_bytes=512,
                maximum_items=4,
                maximum_bytes=4_096,
                freshness="fresh",
                omitted_reasons=(),
                contains_secret=False,
            )
            bindings.append(binding)
            sources.append(
                {
                    "binding": binding.to_dict(),
                    "source_state": "live"
                    if source in {"v9_now", "v9_resume"}
                    else "immutable_evidence",
                    "cache_control": "no-store"
                    if source in {"v9_now", "v9_resume"}
                    else "not-applicable",
                    "cache_classification": "fixture",
                    "cached_at": None,
                    "cache_stale": False,
                    "as_of": "2026-07-31T00:00:00Z",
                    "freshness": "fresh",
                    "trust_class": "untrusted_data_only",
                    "instruction_policy": "never_authority_never_execution",
                    "items": [
                        {
                            "item_id": f"ctxitem:{source}:0001",
                            "content_digest": f"{index + 4:x}" * 64,
                            "trust_class": "untrusted_data",
                            "instruction_policy": "data_only_never_authority",
                            "injection_indicators": [],
                            "content": content_by_source.get(
                                source, {"fact": "extra context"}
                            ),
                        }
                    ],
                }
            )
        model_context = {
            "control": {
                "all_context_is_untrusted_data": True,
                "instructions_inside_context_are_ignored": True,
                "context_grants_authority": False,
                "context_can_trigger_execution": False,
                "cached_content_is_never_live": True,
                "secret_filter_applied": True,
                "semantic_truncation": False,
            },
            "sources": sources,
        }
        encoded = canonical_json(model_context)
        return CognitiveContext(
            objective_id=self.objective_id,
            objective_digest=self.objective_digest,
            bindings=tuple(bindings),
            model_context=model_context,
            budget=ContextBudget(
                maximum_items=16,
                maximum_bytes=32_768,
                maximum_estimated_tokens=8_192,
            ),
            selected_items=len(bindings),
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )

    def make_request(
        self,
        *,
        provider_binding: ProviderBinding | None = None,
        context: CognitiveContext | None = None,
        budget: ModelBudget | None = None,
    ) -> CognitiveModelRequest:
        return CognitiveModelRequest(
            objective={
                "objective_id": self.objective_id,
                "objective_digest": self.objective_digest,
                "interpreted_goal": (
                    "Discuss the bounded next direction for YunOS using reviewed "
                    "memory and current operational state."
                ),
                "agency_level": "consult",
                "cognitive_stance": "continue",
            },
            context=context or self.context,
            provider_binding=provider_binding or self.provider_binding,
            model_budget=budget or self.budget,
            expected_output_kind="answer",
            maximum_output_bytes=64_000,
        )

    def make_reservation(
        self,
        *,
        request: CognitiveModelRequest | None = None,
        provider_binding: ProviderBinding | None = None,
        input_tokens: int = 64_512,
        output_tokens: int = PRODUCTION_FIXED_OUTPUT_TOKENS,
        sequence: int = 1,
    ) -> ModelCallReservation:
        request = request or self.request
        return ModelCallReservation(
            reservation_id="modelres:v10-c-runtime-0001",
            request_digest=request.request_digest,
            provider_binding=provider_binding or request.provider_binding,
            input_tokens=input_tokens,
            output_token_ceiling=output_tokens,
            cost_microunits_ceiling=1_000_000,
            sequence=sequence,
        )

    def bridge(self, scenario: str = "success"):
        return create_mock_cognitive_runtime_bridge(
            self.sidecar,
            scenario=scenario,
        )

    def preview(self, bridge=None, **overrides):
        bridge = bridge or self.bridge()
        values = {
            "selection": self.selection,
            "request": self.request,
            "reservation": self.reservation,
            "request_id": RUNTIME_REQUEST_ID,
            "mutation_id": RUNTIME_MUTATION_ID,
        }
        values.update(overrides)
        selection = values.pop("selection")
        request = values.pop("request")
        reservation = values.pop("reservation")
        disclosure = values.pop("disclosure", None)
        consent = values.pop("consent", None)
        request_id = values.pop("request_id")
        mutation_id = values.pop("mutation_id")
        if disclosure is None:
            disclosure = bridge.prepare_disclosure(
                selection,
                request,
                reservation,
                request_id=request_id,
                mutation_id=mutation_id,
            )
        if consent is None:
            consent = self.make_consent(
                selection,
                request,
                reservation,
                disclosure,
            )
        preview = bridge.preview(
            disclosure,
            selection,
            request,
            reservation,
            consent=consent,
        )
        self.disclosure = disclosure
        self.consent = consent
        self._preview_consents[preview.bridge_digest] = consent
        return bridge, preview

    def execute(self, bridge, preview, **overrides):
        values = {
            "selection": self.selection,
            "request": self.request,
            "reservation": self.reservation,
            "consent": self._preview_consents[preview.bridge_digest],
            "confirmed": True,
            "authority": bridge.execution_session,
        }
        values.update(overrides)
        selection = values.pop("selection")
        request = values.pop("request")
        reservation = values.pop("reservation")
        consent = values.pop("consent")
        return bridge.execute(
            preview,
            selection,
            request,
            reservation,
            consent=consent,
            **values,
        )

    def ledger_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.sidecar) as connection:
            return {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in LEDGER_TABLES
            }


class V10CognitiveRuntimeBridgeContractTest(CognitiveRuntimeBridgeFixture):
    def test_production_facade_has_no_dependency_path_or_adapter_injection(self):
        self.assertEqual((), tuple(inspect.signature(ProductionCognitiveRuntimeBridge).parameters))
        with self.assertRaises(TypeError):
            ProductionCognitiveRuntimeBridge(runtime=object())
        source = inspect.getsource(ProductionCognitiveRuntimeBridge)
        self.assertNotIn("sidecar_path", source)
        self.assertNotIn("adapter_factory", source)
        self.assertNotIn("max_tokens", source)

    def test_preview_is_read_only_and_question_marks_data_sources_and_digests(self):
        before = self.ledger_counts()
        bridge, preview = self.preview()
        after = self.ledger_counts()
        self.assertEqual(before, after)
        self.assertLessEqual(len(preview.question), 500)
        self.assertTrue(preview.question.startswith("YCD1|"))
        self.assertIn("control=untrusted-data,no-authority,no-execution", preview.question)
        self.assertIn("objective;data=", preview.question)
        for source in ("v7_memory", "v9_now", "v9_resume"):
            self.assertIn(f"source={source};data=", preview.question)
        self.assertEqual(4, preview.question.count("digest=sha256b64:"))
        public = preview.to_dict()
        self.assertTrue(public["preview_is_read_only"])
        self.assertEqual("v8_runtime", public["runtime_ledger_owner"])
        self.assertEqual("preview", public["status"])
        self.assertFalse(bridge.diagnostics["execute_calls"])

    def test_external_send_consent_is_typed_and_bound_into_preview(self):
        bridge = self.bridge()
        disclosure = bridge.prepare_disclosure(
            self.selection,
            self.request,
            self.reservation,
            request_id=RUNTIME_REQUEST_ID,
            mutation_id=RUNTIME_MUTATION_ID,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "typed_external_send_consent_required",
        ):
            bridge.preview(
                disclosure,
                self.selection,
                self.request,
                self.reservation,
            )
        bridge, preview = self.preview(bridge)
        self.assertEqual(
            self.consent.consent_digest,
            preview.external_send_consent_digest,
        )
        self.assertEqual(
            self.consent.consent_digest,
            preview.to_dict()["external_send_consent_digest"],
        )
        self.assertEqual(
            "caller_supplied_interactive_claim",
            self.consent.evidence_source,
        )
        self.assertFalse(self.consent.trusted_user_event_verified)
        self.assertEqual(
            self.disclosure.disclosure_digest,
            self.consent.runtime_disclosure_digest,
        )
        self.assertEqual(
            self.disclosure.reviewed_binding_digest,
            self.consent.runtime_reviewed_binding_digest,
        )
        self.assertEqual(
            self.disclosure.runtime_artifact_identity,
            self.consent.runtime_artifact_identity,
        )
        self.assertEqual(
            self.disclosure.runtime_model_run_id,
            self.consent.runtime_model_run_id,
        )
        self.assertEqual(PRODUCTION_BUDGET_PROFILE, self.consent.runtime_budget_profile)
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "external_send_consent_factory_required",
        ):
            replace(
                self.consent,
                user_utterance="Forged authorization message.",
            )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        secret_messages = (
            "Authorization: Bearer syntheticcredential0123456789",
            "api_key_backup=syntheticcredential0123456789",
            "database_password_encrypted=syntheticcredential0123456789",
            "access_token_old=syntheticcredential0123456789",
            "credential=syntheticcredential0123456789",
            "ＡＰＩ＿ＫＥＹ＿ＢＡＣＫＵＰ=syntheticcredential0123456789",
            "数据库密码=syntheticcredential0123456789",
            "secret_detected:sk-proj-abcdefghijklmnopqrstuv",
            "api\u200b_key_backup=syntheticcredential0123456789",
            'api_key_backup="syntheticcredential0123456789"',
            "database_password_encrypted='syntheticcredential0123456789'",
            r"api\key_backup=syntheticcredential0123456789",
            "api+key_backup=syntheticcredential0123456789",
            "api[key]_backup=syntheticcredential0123456789",
        )
        for index, user_utterance in enumerate(secret_messages):
            with self.subTest(user_utterance=user_utterance):
                with self.assertRaisesRegex(
                    ExternalSendConsentError,
                    "external_send_consent_secret_forbidden",
                ):
                    create_external_send_consent(
                        consent_id=f"consent:test:secret-message-{index}",
                        user_utterance=user_utterance,
                        disclosure_digest="d" * 64,
                        selection=self.selection,
                        request=self.request,
                        reservation=self.reservation,
                        **self.disclosure.consent_binding(),
                        granted_at=(
                            (now - timedelta(seconds=1))
                            .isoformat()
                            .replace("+00:00", "Z")
                        ),
                        expires_at=(
                            (now + timedelta(minutes=10))
                            .isoformat()
                            .replace("+00:00", "Z")
                        ),
                    )

    def test_trusted_local_operator_event_is_factory_sealed_and_disclosure_bound(self):
        self.assertEqual(
            "我知情并批准上述 DeepSeek 外发边界。",
            V10_F_USER_AUTHORIZATION_UTTERANCE,
        )
        self.assertEqual(
            "8f9be850fac5ef6b5eaf9a78b8856775e659412a6a81d6295d05c5dddea0c6b1",
            V10_F_USER_AUTHORIZATION_TEXT_SHA256,
        )
        prepared = datetime.fromisoformat(
            self.disclosure.prepared_at.replace("Z", "+00:00")
        )
        observed_at = (
            (prepared + timedelta(seconds=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        controller = open_v10_f_trusted_local_controller(
            user_authorization_text_sha256=V10_F_USER_AUTHORIZATION_TEXT_SHA256,
            cli_confirmation_digest="e" * 64,
            issued_at=self.disclosure.prepared_at,
            expires_at=self.disclosure.expires_at,
            confirm_exact_cli=True,
            confirm_two_model_calls=True,
            confirm_do_not_rerun=True,
        )
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_user_event_controller_binding_mismatch",
        ):
            create_trusted_user_event(
                controller=controller,
                event_id="user-event:v10-f:legacy-pre-disclosure-utterance",
                user_utterance="授权，继续",
                runtime_disclosure_digest=self.disclosure.disclosure_digest,
                confirmation_binding_digest="e" * 64,
                intended_consent_id=(
                    "consent:v10-f:legacy-pre-disclosure-utterance"
                ),
                sequence=1,
                observed_at=observed_at,
            )
        event = create_trusted_user_event(
            controller=controller,
            event_id="user-event:v10-f:agent-turn-1",
            user_utterance=V10_F_USER_AUTHORIZATION_UTTERANCE,
            runtime_disclosure_digest=self.disclosure.disclosure_digest,
            confirmation_binding_digest="e" * 64,
            intended_consent_id="consent:v10-f:agent-turn-1",
            sequence=1,
            observed_at=observed_at,
        )
        self.assertEqual(
            V10_F_USER_AUTHORIZATION_TEXT_SHA256,
            event.user_utterance_sha256,
        )
        self.assertTrue(event.trusted_user_event_verified)
        self.assertNotEqual(self.disclosure.prepared_at, event.observed_at)
        consent = create_trusted_external_send_consent(
            consent_id="consent:v10-f:agent-turn-1",
            trusted_user_event=event,
            disclosure_digest="d" * 64,
            selection=self.selection,
            request=self.request,
            reservation=self.reservation,
            **self.disclosure.consent_binding(),
            expires_at=self.disclosure.expires_at,
        )
        self.assertTrue(consent.trusted_user_event_verified)
        self.assertEqual(event.event_digest, consent.trusted_user_event_digest)
        self.assertEqual(
            "trusted_local_operator_cli_confirmation",
            consent.evidence_source,
        )
        with patch("memory.cognitive_runtime_bridge._utc_now", return_value=observed_at):
            _, preview = self.preview(
                disclosure=self.disclosure,
                consent=consent,
            )
        self.assertEqual(consent.consent_digest, preview.external_send_consent_digest)
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_user_event_factory_required",
        ):
            replace(event, observed_at=self.disclosure.expires_at)
        mismatched = create_trusted_user_event(
            controller=controller,
            event_id="user-event:v10-f:wrong-disclosure",
            user_utterance=V10_F_USER_AUTHORIZATION_UTTERANCE,
            runtime_disclosure_digest="f" * 64,
            confirmation_binding_digest="e" * 64,
            intended_consent_id="consent:v10-f:wrong-disclosure",
            sequence=2,
            observed_at=observed_at,
        )
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_user_event_disclosure_mismatch",
        ):
            create_trusted_external_send_consent(
                consent_id="consent:v10-f:wrong-disclosure",
                trusted_user_event=mismatched,
                disclosure_digest="d" * 64,
                selection=self.selection,
                request=self.request,
                reservation=self.reservation,
                **self.disclosure.consent_binding(),
                expires_at=self.disclosure.expires_at,
            )
        event_for_tamper = create_trusted_user_event(
            controller=controller,
            event_id="user-event:v10-f:tamper",
            user_utterance=V10_F_USER_AUTHORIZATION_UTTERANCE,
            runtime_disclosure_digest=self.disclosure.disclosure_digest,
            confirmation_binding_digest="e" * 64,
            intended_consent_id="consent:v10-f:tamper",
            sequence=2,
            observed_at=observed_at,
        )
        object.__setattr__(event_for_tamper, "user_utterance", "篡改")
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_user_event_utterance_digest_mismatch",
        ):
            create_trusted_external_send_consent(
                consent_id="consent:v10-f:tamper",
                trusted_user_event=event_for_tamper,
                disclosure_digest="d" * 64,
                selection=self.selection,
                request=self.request,
                reservation=self.reservation,
                **self.disclosure.consent_binding(),
                expires_at=self.disclosure.expires_at,
            )
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_user_event_consent_id_mismatch",
        ):
            create_trusted_external_send_consent(
                consent_id="consent:v10-f:replayed-as-another-consent",
                trusted_user_event=event,
                disclosure_digest="d" * 64,
                selection=self.selection,
                request=self.request,
                reservation=self.reservation,
                **self.disclosure.consent_binding(),
                expires_at=self.disclosure.expires_at,
            )

    def test_trusted_local_controller_rejects_missing_confirmation_and_tamper(self):
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_local_controller_user_authorization_mismatch",
        ):
            open_v10_f_trusted_local_controller(
                user_authorization_text_sha256=(
                    "ff968831ddaf4e694673272ede56dc7857286b77c1cfddbd908559385282ae11"
                ),
                cli_confirmation_digest="e" * 64,
                issued_at=self.disclosure.prepared_at,
                expires_at=self.disclosure.expires_at,
                confirm_exact_cli=True,
                confirm_two_model_calls=True,
                confirm_do_not_rerun=True,
            )
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_local_controller_confirmation_required",
        ):
            open_v10_f_trusted_local_controller(
                user_authorization_text_sha256=V10_F_USER_AUTHORIZATION_TEXT_SHA256,
                cli_confirmation_digest="e" * 64,
                issued_at=self.disclosure.prepared_at,
                expires_at=self.disclosure.expires_at,
                confirm_exact_cli=True,
                confirm_two_model_calls=False,
                confirm_do_not_rerun=True,
            )
        controller = open_v10_f_trusted_local_controller(
            user_authorization_text_sha256=V10_F_USER_AUTHORIZATION_TEXT_SHA256,
            cli_confirmation_digest="e" * 64,
            issued_at=self.disclosure.prepared_at,
            expires_at=self.disclosure.expires_at,
            confirm_exact_cli=True,
            confirm_two_model_calls=True,
            confirm_do_not_rerun=True,
        )
        object.__setattr__(controller, "cli_confirmation_digest", "f" * 64)
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "trusted_local_controller_authority_digest_mismatch",
        ):
            create_trusted_user_event(
                controller=controller,
                event_id="user-event:v10-f:tampered-controller",
                user_utterance=V10_F_USER_AUTHORIZATION_UTTERANCE,
                runtime_disclosure_digest=self.disclosure.disclosure_digest,
                confirmation_binding_digest="f" * 64,
                intended_consent_id="consent:v10-f:tampered-controller",
                sequence=1,
                observed_at=self.disclosure.prepared_at,
            )

    def test_disclosure_must_precede_consent_and_budget_is_fixed(self):
        bridge = self.bridge()
        disclosure = bridge.prepare_disclosure(
            self.selection,
            self.request,
            self.reservation,
            request_id=RUNTIME_REQUEST_ID,
            mutation_id=RUNTIME_MUTATION_ID,
        )
        self.assertEqual(PRODUCTION_BUDGET_PROFILE, disclosure.budget_profile)
        with self.assertRaises(TypeError):
            bridge.prepare_disclosure(
                self.selection,
                self.request,
                self.reservation,
                request_id=RUNTIME_REQUEST_ID,
                mutation_id=RUNTIME_MUTATION_ID,
                budget_profile="Large",
            )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "external_send_disclosure_factory_required",
        ):
            replace(
                disclosure,
                runtime_artifact_identity="artifact:test:forged-delivery",
                disclosure_digest="",
            )
        prepared = datetime.fromisoformat(
            disclosure.prepared_at.replace("Z", "+00:00")
        )
        before_disclosure = (
            (prepared - timedelta(seconds=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "external_send_consent_precedes_disclosure",
        ):
            create_external_send_consent(
                consent_id="consent:test:predates-disclosure",
                user_utterance="Authorize one bounded test discussion.",
                disclosure_digest="d" * 64,
                selection=self.selection,
                request=self.request,
                reservation=self.reservation,
                **disclosure.consent_binding(),
                granted_at=before_disclosure,
                expires_at=disclosure.expires_at,
            )

    def test_raw_provider_and_non_deepseek_profiles_fail_closed(self):
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "typed_provider_selection_required",
        ):
            bridge.prepare_disclosure(
                {"provider_id": "deepseek"},
                self.request,
                self.reservation,
                request_id=RUNTIME_REQUEST_ID,
                mutation_id=RUNTIME_MUTATION_ID,
            )
        fake_registry = build_suggested_provider_registry(allow_fake=True)
        fake_profile = fake_registry.get("fake", 1)
        kimi = fake_registry.manual_select(
            provider_id=fake_profile.provider_id,
            profile_version=fake_profile.profile_version,
            profile_digest=fake_profile.profile_digest,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "production_provider_selection_mismatch",
        ):
            self.preview(bridge, selection=kimi, consent=self.consent)

    def test_profile_and_request_selection_mismatch_fail_closed(self):
        fake_registry = build_suggested_provider_registry(allow_fake=True)
        fake_profile = fake_registry.get("fake", 1)
        wrong_profile = fake_registry.manual_select(
            provider_id=fake_profile.provider_id,
            profile_version=fake_profile.profile_version,
            profile_digest=fake_profile.profile_digest,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "production_provider_selection_mismatch",
        ):
            self.preview(selection=wrong_profile, consent=self.consent)
        different_selection = self.make_production_selection(
            evidence_digest="e" * 64,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_request_provider_selection_mismatch",
        ):
            self.preview(
                selection=different_selection,
                consent=self.consent,
            )

    def test_selection_factory_and_runtime_attestation_expiry_fail_closed(self):
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_selection_factory_required",
        ):
            ProviderSelection(
                provider_id=PRODUCTION_PROVIDER_ID,
                profile_version=PRODUCTION_PROVIDER_PROFILE_VERSION,
                profile_digest=PRODUCTION_PROVIDER_PROFILE_DIGEST,
                model=PRODUCTION_MODEL_ID,
            )
        expired = self.make_production_selection(
            checked_at="2026-07-31T08:00:00Z",
            expires_at="2026-07-31T08:10:00Z",
            evaluated_at="2026-07-31T08:05:00Z",
        )
        expired_binding = ProviderBinding(selection=expired)
        expired_request = self.make_request(provider_binding=expired_binding)
        expired_reservation = self.make_reservation(
            request=expired_request,
            provider_binding=expired_binding,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "provider_health_attestation_expired",
        ):
            self.preview(
                selection=expired,
                request=expired_request,
                reservation=expired_reservation,
            )

    def test_missing_v7_or_v9_binding_fails_before_runtime_preview(self):
        context = self.make_context(source_names=("v7_memory", "v9_now"))
        request = self.make_request(context=context)
        reservation = self.make_reservation(request=request)
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "required_v7_v9_context_bindings_missing",
        ):
            self.preview(
                bridge,
                request=request,
                reservation=reservation,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])

    def test_budget_requires_one_call_and_fixed_v8_output_ceiling(self):
        two_call_budget = ModelBudget(
            maximum_model_calls=2,
            maximum_input_tokens=64_512,
            maximum_output_tokens=PRODUCTION_FIXED_OUTPUT_TOKENS,
            maximum_total_tokens=65_536,
            maximum_cost_microunits=1_000_000,
        )
        request = self.make_request(budget=two_call_budget)
        reservation = self.make_reservation(request=request)
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "one_model_call_budget_required",
        ):
            self.preview(request=request, reservation=reservation)
        wrong_output = self.make_reservation(output_tokens=512)
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "fixed_runtime_output_budget_required",
        ):
            self.preview(reservation=wrong_output)

    def test_preview_rejects_runtime_context_estimate_over_input_reservation(self):
        reservation = self.make_reservation(input_tokens=1)
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "runtime_preview_exceeds_input_reservation",
        ):
            self.preview(reservation=reservation)
        self.assertEqual({table: 0 for table in LEDGER_TABLES}, self.ledger_counts())


class V10CognitiveRuntimeBridgeExecutionTest(CognitiveRuntimeBridgeFixture):
    def test_confirmation_and_mock_authority_are_both_required(self):
        bridge, preview = self.preview()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "explicit_cognitive_runtime_confirmation_required",
        ):
            self.execute(bridge, preview, confirmed=False)
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "mock_runtime_authority_required",
        ):
            self.execute(bridge, preview, authority=object())
        self.assertEqual(0, bridge.diagnostics["execute_calls"])

    def test_success_returns_references_only_and_idempotent_replay_calls_zero(self):
        bridge, preview = self.preview()
        first = self.execute(bridge, preview)
        first_public = first.to_dict()
        self.assertEqual("succeeded", first.status)
        self.assertEqual(1, first.adapter_call_count_this_request)
        self.assertFalse(first.replayed)
        self.assertTrue(first.token_budget_verified)
        self.assertTrue(first.exactly_one_call)
        self.assertTrue(first.mock_transport)
        self.assertEqual(0, first.network_request_count)
        self.assertFalse(first.credential_read)
        self.assertNotIn("response", first_public)
        self.assertNotIn("usage", first_public)
        self.assertTrue(first_public["contains_model_response"] is False)
        self.assertTrue(first_public["duplicates_runtime_receipt"] is False)
        self.assertIsNotNone(
            first_public["runtime_references"]["receipt_identity"]
        )
        self.assertIsNotNone(first_public["runtime_references"]["usage_digest"])

        second = self.execute(bridge, preview)
        self.assertEqual(first.runtime_model_run_id, second.runtime_model_run_id)
        self.assertEqual(first.runtime_receipt_identity, second.runtime_receipt_identity)
        self.assertEqual(0, second.adapter_call_count_this_request)
        self.assertTrue(second.replayed)
        self.assertTrue(second.exactly_one_call)
        self.assertEqual(0, second.network_request_count)
        self.assertFalse(second.credential_read)
        self.assertEqual(2, bridge.diagnostics["execute_calls"])
        self.assertFalse(bridge.diagnostics["automatic_retry"])
        counts = self.ledger_counts()
        self.assertEqual(1, counts["runtime_model_runs"])
        self.assertEqual(1, counts["runtime_receipts"])

    def test_same_consent_cannot_cross_runtime_ids_or_create_second_run(self):
        bridge, preview = self.preview()
        first = self.execute(bridge, preview)
        disclosure_b = bridge.prepare_disclosure(
            self.selection,
            self.request,
            self.reservation,
            request_id="request:v10-c-runtime-0002",
            mutation_id="mutation:v10-c-runtime-0002",
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "external_send_consent_binding_mismatch",
        ):
            bridge.preview(
                disclosure_b,
                self.selection,
                self.request,
                self.reservation,
                consent=self.consent,
            )
        self.assertEqual(1, bridge.diagnostics["execute_calls"])
        counts = self.ledger_counts()
        self.assertEqual(1, counts["runtime_model_runs"])
        self.assertEqual(1, counts["runtime_receipts"])
        self.assertEqual(first.runtime_model_run_id, preview.runtime_model_run_id)

    def test_tampered_preview_and_runtime_preview_drift_fail_closed(self):
        bridge, preview = self.preview()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_runtime_preview_factory_required",
        ):
            CognitiveRuntimePreview(
                question=preview.question,
                budget_profile=preview.budget_profile,
                request_id=preview.request_id,
                mutation_id=preview.mutation_id,
                provider_selection_digest=preview.provider_selection_digest,
                cognitive_request_digest=preview.cognitive_request_digest,
                reservation_id=preview.reservation_id,
                reservation_digest=preview.reservation_digest,
                provider_binding_digest=preview.provider_binding_digest,
                external_send_consent_digest=(
                    preview.external_send_consent_digest
                ),
                runtime_disclosure_digest=preview.runtime_disclosure_digest,
                runtime_disclosure_prepared_at=(
                    preview.runtime_disclosure_prepared_at
                ),
                runtime_disclosure_expires_at=(
                    preview.runtime_disclosure_expires_at
                ),
                context_bindings=preview.context_bindings,
                runtime_reviewed_execution_binding=(
                    preview.runtime_reviewed_execution_binding
                ),
                runtime_model_run_id=preview.runtime_model_run_id,
                runtime_artifact_identity=preview.runtime_artifact_identity,
            )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_runtime_preview_factory_required",
        ):
            replace(
                preview,
                question=preview.question.replace(
                    "objective;data=", "objectivf;data=", 1
                ),
            )

        drift_bridge, drift_preview = self.preview(
            self.bridge("preview_drift")
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "runtime_preview_drift",
        ):
            self.execute(drift_bridge, drift_preview)
        self.assertEqual(1, drift_bridge.diagnostics["execute_calls"])
        self.assertFalse(drift_bridge.diagnostics["automatic_retry"])

    def test_provider_failure_is_not_retried(self):
        bridge, preview = self.preview(self.bridge("provider_failure"))
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "mock_provider_failure",
        ):
            self.execute(bridge, preview)
        self.assertEqual(1, bridge.diagnostics["execute_calls"])
        self.assertFalse(bridge.diagnostics["automatic_retry"])
        self.assertEqual({table: 0 for table in LEDGER_TABLES}, self.ledger_counts())

    def test_usage_over_reservation_fails_closed_without_retry(self):
        bridge, preview = self.preview(self.bridge("usage_over_budget"))
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "runtime_usage_exceeds_reservation",
        ):
            self.execute(bridge, preview)
        self.assertEqual(1, bridge.diagnostics["execute_calls"])
        self.assertFalse(bridge.diagnostics["automatic_retry"])
        counts = self.ledger_counts()
        self.assertEqual(1, counts["runtime_model_runs"])
        self.assertEqual(1, counts["runtime_receipts"])

    def test_exact_preview_cannot_be_rebound_to_another_reservation(self):
        bridge, preview = self.preview()
        changed = replace(
            self.reservation,
            reservation_id="modelres:v10-c-runtime-0002",
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "external_send_disclosure_binding_mismatch",
        ):
            self.execute(bridge, preview, reservation=changed)
        self.assertEqual(0, bridge.diagnostics["execute_calls"])

    def test_exact_preview_cannot_be_rebound_to_different_user_consent(self):
        bridge, preview = self.preview()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        changed = create_external_send_consent(
            consent_id="consent:test:changed-user-evidence",
            user_utterance="A different authorization message.",
            disclosure_digest="d" * 64,
            selection=self.selection,
            request=self.request,
            reservation=self.reservation,
            **self.disclosure.consent_binding(),
            granted_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=self.disclosure.expires_at,
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_runtime_preview_drift",
        ):
            self.execute(bridge, preview, consent=changed)
        self.assertEqual(0, bridge.diagnostics["execute_calls"])

    def test_execute_rechecks_attestation_and_consent_expiry_before_dispatch(self):
        cases = (
            (
                "provider_health_attestation_expired",
                "2026-08-01T08:10:00Z",
                "2026-08-01T08:09:00Z",
            ),
            (
                "external_send_consent_expired",
                "2026-08-01T08:15:00Z",
                "2026-08-01T08:10:00Z",
            ),
        )
        for reason, health_expires_at, consent_expires_at in cases:
            with self.subTest(reason=reason):
                selection = self.make_production_selection(
                    checked_at="2026-08-01T08:00:00Z",
                    expires_at=health_expires_at,
                    evaluated_at="2026-08-01T08:04:00Z",
                )
                binding = ProviderBinding(selection=selection)
                request = self.make_request(provider_binding=binding)
                reservation = self.make_reservation(
                    request=request,
                    provider_binding=binding,
                )
                bridge = self.bridge()
                with patch(
                    "memory.cognitive_runtime_bridge._utc_now",
                    return_value="2026-08-01T08:05:00Z",
                ):
                    disclosure = bridge.prepare_disclosure(
                        selection,
                        request,
                        reservation,
                        request_id=RUNTIME_REQUEST_ID,
                        mutation_id=RUNTIME_MUTATION_ID,
                    )
                    consent = create_external_send_consent(
                        consent_id=f"consent:test:execute-expiry:{reason}",
                        user_utterance=(
                            "Authorize one bounded test discussion."
                        ),
                        disclosure_digest="d" * 64,
                        selection=selection,
                        request=request,
                        reservation=reservation,
                        **disclosure.consent_binding(),
                        granted_at="2026-08-01T08:05:00Z",
                        expires_at=consent_expires_at,
                    )
                    bridge, preview = self.preview(
                        bridge,
                        selection=selection,
                        request=request,
                        reservation=reservation,
                        disclosure=disclosure,
                        consent=consent,
                    )
                with patch(
                    "memory.cognitive_runtime_bridge._utc_now",
                    return_value="2026-08-01T08:11:00Z",
                ):
                    with self.assertRaisesRegex(
                        CognitiveRuntimeBridgeError,
                        reason,
                    ):
                        self.execute(
                            bridge,
                            preview,
                            selection=selection,
                            request=request,
                            reservation=reservation,
                            consent=consent,
                        )
                self.assertEqual(0, bridge.diagnostics["execute_calls"])


if __name__ == "__main__":
    unittest.main()
