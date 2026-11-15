from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path

from memory.cognitive_provider import (
    ADAPTER_KINDS,
    CAPABILITY_FLAGS,
    HEALTH_STATES,
    PROVIDER_CONTRACT_VERSION,
    REASONING_OPTIONS,
    STRUCTURED_OUTPUT_SUPPORT,
    TOOL_CALLING_SUPPORT,
    CognitiveProviderError,
    CostMetadata,
    FakeProviderAdapter,
    ProviderAdapter,
    ProviderAdapterRequest,
    ProviderAdapterResponse,
    ProviderHealthAttestation,
    ProviderProfile,
    ProviderRegistry,
    ProviderUsage,
    SecretReference,
    build_suggested_provider_registry,
    suggested_provider_profiles,
)
from memory.operational_state_models import canonical_digest
from memory.runtime_protocol_contracts import (
    BASE_URL as V8_DEEPSEEK_BASE_URL,
    MODEL_ID as V8_DEEPSEEK_MODEL_ID,
)


def profile_named(
    provider_id: str,
    *,
    configured: tuple[str, ...] = (),
    allow_fake: bool = False,
) -> ProviderProfile:
    return next(
        profile
        for profile in suggested_provider_profiles(
            configured,
            allow_fake=allow_fake,
        )
        if profile.provider_id == provider_id
    )


def profile_copy(
    profile: ProviderProfile,
    **overrides: object,
) -> ProviderProfile:
    payload = profile.to_dict()
    payload.pop("profile_digest")
    payload.update(overrides)
    payload["profile_digest"] = canonical_digest(
        {
            key: value
            for key, value in payload.items()
            if key != "profile_digest"
        }
    )
    return ProviderProfile.from_dict(payload)


def fake_request(profile: ProviderProfile, **overrides: object) -> ProviderAdapterRequest:
    payload: dict[str, object] = {
        "request_id": "request.fake.001",
        "provider_id": profile.provider_id,
        "profile_version": profile.profile_version,
        "profile_digest": profile.profile_digest,
        "model": profile.model,
        "input_text": "Return a deterministic contract response.",
        "response_mode": "json_schema",
        "reasoning_option": "disabled",
        "max_output_tokens": 128,
        "allow_tool_proposals": False,
    }
    payload.update(overrides)
    return ProviderAdapterRequest(**payload)


def health_attestation(
    profile: ProviderProfile,
    *,
    health_state: str = "healthy",
    checked_at: str = "2026-07-31T08:00:00Z",
    expires_at: str = "2026-07-31T08:10:00Z",
    evidence_digest: str = "1" * 64,
) -> ProviderHealthAttestation:
    return ProviderHealthAttestation(
        provider_id=profile.provider_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        health_state=health_state,
        checked_at=checked_at,
        expires_at=expires_at,
        evidence_digest=evidence_digest,
    )


class V10ProviderProfileTest(unittest.TestCase):
    def test_contract_module_has_no_io_network_or_credential_dependencies(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "memory"
            / "cognitive_provider.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in (
            "os",
            "pathlib",
            "sqlite3",
            "urllib.request",
            "requests",
            "httpx",
        ):
            self.assertNotIn(forbidden, imported)

    def test_suggested_registry_contains_all_required_provider_families(self) -> None:
        profiles = suggested_provider_profiles()
        self.assertEqual(
            {profile.provider_id for profile in profiles},
            {
                "deepseek",
                "kimi",
                "openai.compatible",
                "local.openai_compatible",
                "fake",
            },
        )
        self.assertEqual(
            {profile.adapter_kind for profile in profiles},
            {
                "deepseek_v8_runtime",
                "openai_compatible",
                "local_openai_compatible",
                "fake",
            },
        )
        self.assertEqual(
            set(ADAPTER_KINDS),
            {
                "deepseek_v8_runtime",
                "openai_compatible",
                "local_openai_compatible",
                "fake",
            },
        )

    def test_deepseek_profile_reuses_v8_runtime_identity(self) -> None:
        profile = profile_named("deepseek")
        self.assertEqual(profile.endpoint, V8_DEEPSEEK_BASE_URL)
        self.assertEqual(profile.model, V8_DEEPSEEK_MODEL_ID)
        self.assertEqual(profile.adapter_kind, "deepseek_v8_runtime")
        self.assertFalse(profile.grants_authority)
        self.assertFalse(profile.executable)

    def test_profile_contract_is_closed_and_digest_is_deterministic(self) -> None:
        profile = profile_named("kimi")
        self.assertEqual(
            profile.profile_digest,
            canonical_digest(profile.identity_payload()),
        )
        reordered = profile.to_dict()
        reordered["capability_flags"] = list(
            reversed(reordered["capability_flags"])
        )
        reordered["reasoning_options"] = list(
            reversed(reordered["reasoning_options"])
        )
        reordered["profile_digest"] = profile.profile_digest
        copy = ProviderProfile.from_dict(
            {key: reordered[key] for key in reversed(reordered)}
        )
        self.assertEqual(profile, copy)
        self.assertEqual(profile.profile_digest, copy.profile_digest)
        self.assertEqual(
            profile.to_dict()["contract_version"],
            PROVIDER_CONTRACT_VERSION,
        )

    def test_profile_shape_matches_json_schema_and_sql_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (
                root
                / "docs"
                / "schemas"
                / "yunos_v10_cognitive_agency.schema.json"
            ).read_text(encoding="utf-8")
        )
        provider_schema = schema["$defs"]["provider_profile"]
        profile = profile_named("deepseek")
        self.assertFalse(provider_schema["additionalProperties"])
        self.assertEqual(set(provider_schema["required"]), set(profile.to_dict()))
        self.assertEqual(
            set(provider_schema["properties"]["adapter_kind"]["enum"]),
            set(ADAPTER_KINDS),
        )
        self.assertEqual(
            set(
                provider_schema["properties"]["tool_calling_support"]["enum"]
            ),
            set(TOOL_CALLING_SUPPORT),
        )
        self.assertEqual(
            set(
                provider_schema["properties"][
                    "structured_output_support"
                ]["enum"]
            ),
            set(STRUCTURED_OUTPUT_SUPPORT),
        )
        sql = (root / "memory" / "cognitive_schema_10001.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("PRIMARY KEY (provider_id, profile_version)", sql)
        self.assertIn("UNIQUE (provider_id, profile_digest)", sql)
        self.assertIn("canonical_profile TEXT NOT NULL", sql)

    def test_record_round_trip_and_digest_tamper_fail_closed(self) -> None:
        profile = profile_named("deepseek")
        record = json.loads(json.dumps(profile.to_record()))
        self.assertEqual(profile, ProviderProfile.from_record(record))
        record["timeout_ms"] += 1
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_profile_digest_mismatch",
        ):
            ProviderProfile.from_record(record)

    def test_unknown_fields_and_secret_value_fields_are_rejected(self) -> None:
        profile = profile_named("deepseek")
        payload = profile.to_dict()
        payload["api_key"] = "not-a-secret"
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "invalid_provider_profile",
        ):
            ProviderProfile.from_dict(payload)
        reference = profile.secret_reference.to_dict()
        reference["value"] = "not-a-secret"
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "invalid_provider_secret_reference",
        ):
            SecretReference.from_dict(reference)

    def test_secret_literal_cannot_be_serialized_in_profile_or_request(self) -> None:
        profile = profile_named("deepseek")
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_secret_material_forbidden",
        ):
            profile_copy(
                profile,
                display_name="api_key=super-secret-value-123456",
            )
        fake = profile_named("fake", allow_fake=True)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_secret_material_forbidden",
        ):
            fake_request(
                fake,
                input_text="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            )

    def test_remote_endpoint_rejects_credentials_query_and_http(self) -> None:
        profile = profile_named("kimi")
        for endpoint in (
            "https://user:pass@example.com/v1",
            "https://example.com/v1?token=x",
            "http://example.com/v1",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(
                    CognitiveProviderError,
                    "unsafe_provider_endpoint",
                ):
                    profile_copy(profile, endpoint=endpoint)

    def test_local_endpoint_is_loopback_only(self) -> None:
        profile = profile_named("local.openai_compatible")
        self.assertEqual(profile.endpoint, "http://127.0.0.1:11434/v1")
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "unsafe_local_provider_endpoint",
        ):
            profile_copy(profile, endpoint="http://192.168.1.8:11434/v1")

    def test_capability_support_flags_are_consistent(self) -> None:
        profile = profile_named("kimi")
        self.assertEqual(profile.endpoint, "https://api.moonshot.cn/v1")
        self.assertEqual(profile.model, "kimi-k3")
        self.assertIn(
            profile.tool_calling_support,
            TOOL_CALLING_SUPPORT,
        )
        self.assertIn(
            profile.structured_output_support,
            STRUCTURED_OUTPUT_SUPPORT,
        )
        self.assertTrue(set(profile.capability_flags) <= set(CAPABILITY_FLAGS))
        self.assertTrue(set(profile.reasoning_options) <= set(REASONING_OPTIONS))
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_tool_capability_mismatch",
        ):
            profile_copy(
                profile,
                capability_flags=[
                    flag
                    for flag in profile.capability_flags
                    if flag != "tool_proposals"
                ],
            )

    def test_enabled_and_health_state_are_bound(self) -> None:
        profile = profile_named("fake")
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.health_state, "disabled")
        self.assertIn(profile.health_state, HEALTH_STATES)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "enabled_provider_cannot_be_disabled",
        ):
            profile_copy(profile, enabled=True)

    def test_cost_metadata_is_integer_or_explicitly_unknown(self) -> None:
        deepseek = profile_named("deepseek")
        self.assertEqual(deepseek.cost_metadata.pricing_state, "unknown")
        self.assertIsNone(
            deepseek.cost_metadata.input_microunits_per_million_tokens
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "invalid_provider_pricing_value",
        ):
            CostMetadata("known", "USD", 1.5, 2)


class V10ProviderRegistryTest(unittest.TestCase):
    def test_kimi_without_credential_reference_is_safely_unavailable(self) -> None:
        registry = build_suggested_provider_registry()
        kimi = registry.get("kimi", 1)
        self.assertEqual(kimi.health_state, "unavailable")
        self.assertEqual(
            kimi.health_reason,
            "credential_reference_unavailable",
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_profile_unavailable",
        ):
            registry.manual_select(
                provider_id=kimi.provider_id,
                profile_version=kimi.profile_version,
                profile_digest=kimi.profile_digest,
            )

    def test_manual_selection_requires_exact_id_version_and_digest(self) -> None:
        registry = build_suggested_provider_registry(allow_fake=True)
        fake = registry.get("fake", 1)
        selection = registry.manual_select(
            provider_id=fake.provider_id,
            profile_version=fake.profile_version,
            profile_digest=fake.profile_digest,
        )
        self.assertEqual(selection.provider_id, fake.provider_id)
        self.assertEqual(selection.profile_digest, fake.profile_digest)
        self.assertEqual(selection.model, fake.model)
        self.assertEqual(selection.selection_mode, "manual")
        self.assertFalse(selection.fallback_used)
        self.assertFalse(selection.grants_authority)
        self.assertEqual(registry.profile_for_selection(selection), fake)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_profile_binding_mismatch",
        ):
            registry.manual_select(
                provider_id=fake.provider_id,
                profile_version=fake.profile_version,
                profile_digest="0" * 64,
            )

    def test_unknown_provider_does_not_trigger_hidden_fallback(self) -> None:
        registry = build_suggested_provider_registry(allow_fake=True)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_profile_not_found",
        ):
            registry.manual_select(
                provider_id="missing.provider",
                profile_version=1,
                profile_digest="0" * 64,
            )
        public = registry.to_public_dict()
        self.assertEqual(public["selection_policy"], "manual_only")
        self.assertFalse(public["hidden_fallback"])
        self.assertFalse(public["grants_authority"])
        self.assertFalse(public["executable"])

    def test_configured_reference_is_not_enough_without_health_evidence(self) -> None:
        reference = "secret.deepseek.production"
        registry = build_suggested_provider_registry((reference,))
        profile = registry.get("deepseek", 1)
        self.assertEqual(profile.health_state, "unknown")
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_health_attestation_required",
        ):
            registry.manual_select(
                provider_id=profile.provider_id,
                profile_version=profile.profile_version,
                profile_digest=profile.profile_digest,
            )

    def test_unknown_profile_with_valid_attestation_can_be_selected(self) -> None:
        reference = "secret.deepseek.production"
        profile = profile_named(
            "deepseek",
            configured=(reference,),
        )
        registry = ProviderRegistry(
            (profile,),
            configured_secret_references=(reference,),
        )
        attestation = health_attestation(profile)
        selection = registry.manual_select(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_attestation=attestation,
            evaluated_at="2026-07-31T08:05:00Z",
        )
        self.assertEqual(selection.profile_digest, profile.profile_digest)
        self.assertEqual(
            selection.health_attestation_digest,
            attestation.attestation_digest,
        )
        self.assertEqual(selection.health_attestation, attestation)
        self.assertEqual(
            selection.health_evaluated_at,
            "2026-07-31T08:05:00.000000Z",
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_selection_factory_required",
        ):
            replace(selection, model="forged-model")

    def test_health_attestation_expiry_and_future_checks_fail_closed(self) -> None:
        reference = "secret.deepseek.production"
        registry = build_suggested_provider_registry((reference,))
        profile = registry.get("deepseek", 1)
        attestation = health_attestation(profile)
        for evaluated_at, reason in (
            (
                "2026-07-31T08:10:00Z",
                "provider_health_attestation_expired",
            ),
            (
                "2026-07-31T07:59:59Z",
                "provider_health_attestation_not_yet_valid",
            ),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(CognitiveProviderError, reason):
                    registry.manual_select(
                        provider_id=profile.provider_id,
                        profile_version=profile.profile_version,
                        profile_digest=profile.profile_digest,
                        health_attestation=attestation,
                        evaluated_at=evaluated_at,
                    )

    def test_health_attestation_wrong_binding_and_forgery_fail_closed(self) -> None:
        reference = "secret.deepseek.production"
        registry = build_suggested_provider_registry((reference,))
        profile = registry.get("deepseek", 1)
        wrong = ProviderHealthAttestation(
            provider_id="kimi",
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_state="healthy",
            checked_at="2026-07-31T08:00:00Z",
            expires_at="2026-07-31T08:10:00Z",
            evidence_digest="1" * 64,
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_health_attestation_binding_mismatch",
        ):
            registry.manual_select(
                provider_id=profile.provider_id,
                profile_version=profile.profile_version,
                profile_digest=profile.profile_digest,
                health_attestation=wrong,
                evaluated_at="2026-07-31T08:05:00Z",
            )
        attestation = health_attestation(profile)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_health_attestation_digest_mismatch",
        ):
            replace(attestation, evidence_digest="2" * 64)

    def test_unavailable_or_untyped_health_attestation_cannot_bypass(self) -> None:
        reference = "secret.deepseek.production"
        registry = build_suggested_provider_registry((reference,))
        profile = registry.get("deepseek", 1)
        unavailable = health_attestation(profile, health_state="unavailable")
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_health_attestation_unavailable",
        ):
            registry.manual_select(
                provider_id=profile.provider_id,
                profile_version=profile.profile_version,
                profile_digest=profile.profile_digest,
                health_attestation=unavailable,
                evaluated_at="2026-07-31T08:05:00Z",
            )
        for forged in (True, "healthy", {"health_state": "healthy"}):
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(
                    CognitiveProviderError,
                    "provider_health_attestation_required",
                ):
                    registry.manual_select(
                        provider_id=profile.provider_id,
                        profile_version=profile.profile_version,
                        profile_digest=profile.profile_digest,
                        health_attestation=forged,
                        evaluated_at="2026-07-31T08:05:00Z",
                    )

    def test_health_attestation_lifetime_is_bounded(self) -> None:
        profile = profile_named(
            "deepseek",
            configured=("secret.deepseek.production",),
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_health_attestation_lifetime_invalid",
        ):
            health_attestation(
                profile,
                expires_at="2026-07-31T08:15:01Z",
            )

    def test_registry_rejects_duplicate_profile_versions(self) -> None:
        profile = profile_named("fake", allow_fake=True)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "duplicate_provider_profile",
        ):
            ProviderRegistry((profile, profile))

    def test_public_registry_contains_only_opaque_secret_references(self) -> None:
        serialized = json.dumps(
            build_suggested_provider_registry().to_public_dict(),
            sort_keys=True,
        )
        self.assertIn("secret_reference", serialized)
        self.assertIn("secret.deepseek.production", serialized)
        for forbidden in (
            "DEEPSEEK_API_KEY",
            "api_key",
            "Bearer ",
            "private_key",
            '"value"',
        ):
            self.assertNotIn(forbidden, serialized)


class V10FakeProviderAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_suggested_provider_registry(allow_fake=True)
        self.profile = self.registry.get("fake", 1)
        self.adapter = FakeProviderAdapter(self.profile)

    def test_fake_adapter_satisfies_protocol_and_is_deterministic(self) -> None:
        self.assertIsInstance(self.adapter, ProviderAdapter)
        request = fake_request(self.profile)
        first = self.adapter.send(request)
        second = self.adapter.send(request)
        self.assertEqual(first, second)
        self.assertEqual(first.request_digest, request.request_digest)
        self.assertEqual(first.profile_digest, self.profile.profile_digest)
        self.assertEqual(
            first.usage.total_tokens,
            first.usage.input_tokens + first.usage.output_tokens,
        )
        self.assertEqual(
            first.response_digest,
            canonical_digest(first.identity_payload()),
        )
        self.assertEqual(first.tool_proposals, ())
        self.assertEqual(
            first.structured_output["request_digest"],
            request.request_digest,
        )

    def test_fake_adapter_rejects_profile_binding_drift(self) -> None:
        request = fake_request(
            self.profile,
            profile_digest="0" * 64,
        )
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_adapter_binding_mismatch",
        ):
            self.adapter.send(request)

    def test_request_and_response_digest_tamper_fail_closed(self) -> None:
        request = fake_request(self.profile)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_request_digest_mismatch",
        ):
            replace(request, max_output_tokens=64)
        response = self.adapter.send(request)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_response_digest_mismatch",
        ):
            replace(response, content="tampered")

    def test_response_structured_data_is_immutable(self) -> None:
        response = self.adapter.send(fake_request(self.profile))
        with self.assertRaises(TypeError):
            response.structured_output["kind"] = "tampered"

    def test_usage_total_is_validated(self) -> None:
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "provider_usage_total_mismatch",
        ):
            ProviderUsage(input_tokens=2, output_tokens=3, total_tokens=4)

    def test_response_contract_rejects_unvalidated_usage(self) -> None:
        request = fake_request(self.profile)
        with self.assertRaisesRegex(
            CognitiveProviderError,
            "invalid_provider_usage",
        ):
            ProviderAdapterResponse(
                request_id=request.request_id,
                request_digest=request.request_digest,
                provider_id=request.provider_id,
                profile_version=request.profile_version,
                profile_digest=request.profile_digest,
                model=request.model,
                content="response",
                structured_output=None,
                tool_proposals=(),
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                finish_reason="stop",
            )


if __name__ == "__main__":
    unittest.main()
