from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from memory.cognitive_capability import (
    AVAILABILITY_STATES,
    CAPABILITY_CONTRACT_VERSION,
    EXECUTOR_KINDS,
    IDEMPOTENCY_MODES,
    REQUIRED_AUTHORITIES,
    RISK_CLASSES,
    CapabilityProfile,
    CapabilityProposal,
    CapabilityRegistry,
    CapabilityRequirement,
    CognitiveCapabilityError,
    build_suggested_capability_registry,
    suggested_capability_profiles,
)
from memory.operational_state_models import canonical_digest


ROOT = Path(__file__).resolve().parents[1]


def profile_copy(
    profile: CapabilityProfile,
    **overrides: object,
) -> CapabilityProfile:
    value = profile.to_dict()
    value.update(overrides)
    return CapabilityProfile.from_dict(value)


def available_profile(capability_id: str) -> CapabilityProfile:
    profiles = suggested_capability_profiles({capability_id: "available"})
    return next(
        profile for profile in profiles if profile.capability_id == capability_id
    )


class V10CapabilityProfileTest(unittest.TestCase):
    def test_profile_shape_matches_v10_json_schema_and_sql_enums(self) -> None:
        schema = json.loads(
            (ROOT / "docs/schemas/yunos_v10_cognitive_agency.schema.json").read_text(
                encoding="utf-8"
            )
        )
        capability_schema = schema["$defs"]["capability"]
        profile = available_profile("project.file.read")
        self.assertEqual(set(capability_schema["required"]), set(profile.to_dict()))
        self.assertFalse(capability_schema["additionalProperties"])
        self.assertEqual(
            capability_schema["properties"]["risk_class"]["$ref"],
            "#/$defs/risk_class",
        )
        self.assertEqual(
            set(capability_schema["properties"]["required_authority"]["enum"]),
            set(REQUIRED_AUTHORITIES),
        )
        self.assertEqual(
            set(capability_schema["properties"]["idempotency"]["enum"]),
            set(IDEMPOTENCY_MODES),
        )
        self.assertEqual(
            set(capability_schema["properties"]["availability"]["enum"]),
            set(AVAILABILITY_STATES),
        )
        self.assertEqual(
            set(capability_schema["properties"]["executor_kind"]["enum"]),
            set(EXECUTOR_KINDS),
        )
        sql = (ROOT / "memory/cognitive_schema_10001.sql").read_text(
            encoding="utf-8"
        )
        for value in (*RISK_CLASSES, *REQUIRED_AUTHORITIES, *EXECUTOR_KINDS):
            self.assertIn(f"'{value}'", sql)

    def test_digest_and_canonical_document_are_order_deterministic(self) -> None:
        first = available_profile("project.file.read")
        value = first.to_dict()
        value["read_scopes"] = list(reversed(value["read_scopes"]))
        value["verification_method"] = list(
            reversed(value["verification_method"])
        )
        value["input_schema"] = {
            key: value["input_schema"][key]
            for key in reversed(value["input_schema"])
        }
        second = CapabilityProfile.from_dict(value)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.capability_digest, second.capability_digest)
        self.assertEqual(
            first.capability_digest,
            canonical_digest(first.identity_payload()),
        )

    def test_nfc_equivalent_profile_text_has_same_digest(self) -> None:
        first = available_profile("project.file.read")
        composed = profile_copy(first, display_name="Read café project file")
        decomposed = profile_copy(
            first,
            display_name="Read cafe\u0301 project file",
        )
        self.assertEqual(composed.to_dict(), decomposed.to_dict())
        self.assertEqual(composed.capability_digest, decomposed.capability_digest)

    def test_record_round_trip_and_digest_tamper_fail_closed(self) -> None:
        profile = available_profile("project.file.read")
        restored = CapabilityProfile.from_record(
            json.loads(json.dumps(profile.to_record()))
        )
        self.assertEqual(profile, restored)
        record = profile.to_record()
        record["canonical_capability"]["timeout_ms"] = profile.timeout_ms + 1
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_digest_mismatch",
        ):
            CapabilityProfile.from_record(record)

    def test_json_duplicate_keys_and_unknown_fields_fail_closed(self) -> None:
        profile = available_profile("project.file.read")
        raw = profile.to_canonical_json()
        duplicate = raw[:-1] + ',"capability_id":"project.file.read"}'
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "duplicate_capability_json_key",
        ):
            CapabilityProfile.from_json(duplicate)
        value = profile.to_dict()
        value["extra"] = True
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "invalid_capability_profile",
        ):
            CapabilityProfile.from_dict(value)

    def test_schema_is_closed_bounded_and_supports_no_refs_or_patterns(self) -> None:
        profile = available_profile("project.file.read")
        for bad_schema, reason in (
            (
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": True,
                },
                "capability_object_schema_must_be_closed",
            ),
            (
                {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "minLength": 0}
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "capability_string_schema_must_be_bounded",
            ),
            (
                {
                    "type": "object",
                    "properties": {"value": {"$ref": "#/$defs/string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "unsupported_capability_schema_keyword",
            ),
            (
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "enum": ["safe", 7],
                        }
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "capability_schema_enum_type_mismatch",
            ),
            (
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "boolean",
                            "const": 1,
                        }
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "capability_schema_const_type_mismatch",
            ),
            (
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "maxLength": 10,
                            "pattern": ".*",
                        }
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "unsupported_capability_schema_keyword",
            ),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(CognitiveCapabilityError, reason):
                    profile_copy(profile, input_schema=bad_schema)

    def test_forbidden_control_capabilities_and_fields_are_rejected(self) -> None:
        profile = available_profile("project.file.read")
        for capability_id in (
            "system.shell.run",
            "system.powershell.run",
            "git.reset.run",
            "git.rebase.run",
            "git.force_push",
            "network.download",
            "database.sql",
            "project.cross_root.read",
            "system.arbitrary_shell.run",
        ):
            with self.subTest(capability_id=capability_id):
                with self.assertRaisesRegex(
                    CognitiveCapabilityError,
                    "prohibited_capability",
                ):
                    profile_copy(profile, capability_id=capability_id)
        bad_schema = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "maxLength": 128}
            },
            "required": ["command"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_control_field_forbidden",
        ):
            profile_copy(profile, input_schema=bad_schema)

    def test_secret_fields_and_literal_values_are_rejected(self) -> None:
        profile = available_profile("project.file.read")
        bad_schema = {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "maxLength": 128}
            },
            "required": ["api_key"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_secret_field_forbidden",
        ):
            profile_copy(profile, input_schema=bad_schema)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_secret_literal_forbidden",
        ):
            profile.validate_input(
                {"relative_path": "notes/sk-proj-abcdefghijklmnop.txt"}
            )

    def test_read_and_controlled_write_invariants_are_mandatory(self) -> None:
        read_profile = available_profile("project.file.read")
        for overrides in (
            {"idempotency": "idempotent"},
            {"required_authority": "none"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(CognitiveCapabilityError):
                    profile_copy(read_profile, **overrides)
        write_profile = available_profile("project.file.patch")
        for overrides in (
            {"required_authority": "prepare"},
            {"risk_class": "low"},
            {"reversible": False},
            {"idempotency": "non_idempotent"},
            {"verification_method": []},
            {"executor_kind": "codex"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(
                    CognitiveCapabilityError,
                    "unsafe_controlled_write_capability",
                ):
                    profile_copy(write_profile, **overrides)

    def test_input_schema_rejects_extra_type_and_cross_root_paths(self) -> None:
        profile = available_profile("project.file.read")
        self.assertEqual(
            {"relative_path": "memory/cognitive_capability.py"},
            profile.validate_input(
                {"relative_path": "memory/cognitive_capability.py"}
            ),
        )
        for arguments, reason in (
            (
                {"relative_path": "README.md", "extra": True},
                "capability_schema_additional_property",
            ),
            (
                {"relative_path": 7},
                "capability_schema_type_mismatch",
            ),
            (
                {"relative_path": "../outside.txt"},
                "capability_path_outside_authorized_root",
            ),
            (
                {"relative_path": "C:/outside.txt"},
                "capability_path_outside_authorized_root",
            ),
            (
                {"relative_path": "\\\\server\\share\\file.txt"},
                "capability_path_outside_authorized_root",
            ),
            (
                {"relative_path": "file.txt:stream"},
                "capability_path_outside_authorized_root",
            ),
            (
                {"relative_path": ".git/config"},
                "capability_git_control_path_forbidden",
            ),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(CognitiveCapabilityError, reason):
                    profile.validate_input(arguments)
        patch_profile = available_profile("project.file.patch")
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "invalid_capability_sha256_value",
        ):
            patch_profile.validate_input(
                {
                    "relative_path": "memory/example.py",
                    "expected_before_sha256": "z" * 64,
                    "replacement_content": "safe",
                    "expected_after_sha256": "b" * 64,
                }
            )

    def test_output_schema_and_byte_budget_are_strict(self) -> None:
        profile = available_profile("project.file.patch")
        valid = {
            "relative_path": "memory/example.py",
            "before_sha256": "a" * 64,
            "after_sha256": "b" * 64,
            "backup_reference": "backup:example",
            "verification_passed": True,
        }
        self.assertEqual(valid, profile.validate_output(valid))
        invalid = dict(valid, verification_passed=False)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_schema_const_mismatch",
        ):
            profile.validate_output(invalid)
        tiny = profile_copy(profile, max_output_bytes=10)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_output_budget_exceeded",
        ):
            tiny.validate_output(valid)

    def test_boolean_budget_values_are_not_integers(self) -> None:
        profile = available_profile("project.file.read")
        for field in ("version", "timeout_ms", "max_output_bytes"):
            with self.subTest(field=field):
                with self.assertRaises(CognitiveCapabilityError):
                    profile_copy(profile, **{field: True})


class V10CapabilityRegistryTest(unittest.TestCase):
    def test_suggested_registry_is_manually_disabled_by_default(self) -> None:
        registry = build_suggested_capability_registry()
        self.assertEqual(4, len(registry.profiles))
        self.assertEqual(
            {"disabled"},
            {profile.availability for profile in registry.profiles},
        )
        self.assertFalse(registry.grants_authority)
        self.assertFalse(registry.can_execute)
        for profile in registry.profiles:
            self.assertFalse(profile.grants_authority)
            self.assertFalse(profile.executable)

    def test_profiles_and_registry_are_immutable_snapshots(self) -> None:
        profile = available_profile("project.file.read")
        registry = CapabilityRegistry((profile,))
        with self.assertRaises(TypeError):
            profile.input_schema["additionalProperties"] = True
        with self.assertRaises(TypeError):
            profile.input_schema["properties"]["relative_path"][
                "maxLength"
            ] = 1_000_000
        with self.assertRaises(Exception):
            registry.profiles += (profile,)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_registry_immutable",
        ):
            registry._profiles = ()
        self.assertFalse(hasattr(registry, "register"))

    def test_availability_override_is_manual_exact_and_deterministic(self) -> None:
        first = build_suggested_capability_registry(
            {"project.file.read": "available"}
        )
        second = build_suggested_capability_registry(
            {"project.file.read": "available"}
        )
        self.assertEqual(first.registry_digest, second.registry_digest)
        self.assertEqual(
            "available",
            first.lookup("project.file.read").availability,
        )
        self.assertEqual(
            "disabled",
            first.lookup("project.file.patch").availability,
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "unknown_capability_availability_override",
        ):
            build_suggested_capability_registry({"unknown.cap": "available"})

    def test_lookup_binds_version_and_digest(self) -> None:
        version_one = available_profile("project.file.read")
        version_two = profile_copy(
            version_one,
            version=2,
            display_name="Read one authorized project file v2",
        )
        registry = CapabilityRegistry((version_two, version_one))
        self.assertEqual(2, registry.lookup("project.file.read").version)
        self.assertEqual(
            version_one,
            registry.lookup("project.file.read", version=1),
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_profile_drift",
        ):
            registry.lookup(
                "project.file.read",
                version=1,
                expected_digest=version_two.capability_digest,
            )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "unknown_capability",
        ):
            registry.lookup("project.file.missing")

    def test_duplicate_and_same_version_drift_fail_closed(self) -> None:
        profile = available_profile("project.file.read")
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "duplicate_capability_profile",
        ):
            CapabilityRegistry((profile, profile))
        drifted = profile_copy(profile, display_name="Drifted descriptor")
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_profile_drift",
        ):
            CapabilityRegistry((profile, drifted))
        same_version_availability_change = profile_copy(
            profile,
            availability="disabled",
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_profile_drift",
        ):
            CapabilityRegistry((profile, same_version_availability_change))

    def test_snapshot_round_trip_and_registry_drift_fail_closed(self) -> None:
        registry = build_suggested_capability_registry(
            {"project.file.read": "available"}
        )
        snapshot = json.loads(json.dumps(registry.to_snapshot()))
        restored = CapabilityRegistry.from_snapshot(snapshot)
        self.assertEqual(registry.registry_digest, restored.registry_digest)
        self.assertEqual(CAPABILITY_CONTRACT_VERSION, snapshot["contract_version"])
        snapshot["registry_digest"] = "0" * 64
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_registry_digest_mismatch",
        ):
            CapabilityRegistry.from_snapshot(snapshot)

    def test_requirement_matching_success_is_not_authorization(self) -> None:
        profile = available_profile("project.file.read")
        registry = CapabilityRegistry((profile,))
        requirement = CapabilityRequirement(
            capability_id=profile.capability_id,
            capability_version=profile.version,
            capability_digest=profile.capability_digest,
            maximum_risk="low",
            allowed_read_scopes=("project.files",),
            maximum_authority="read",
            maximum_timeout_ms=profile.timeout_ms,
            maximum_output_bytes=profile.max_output_bytes,
            allowed_idempotency=("read_only",),
            required_verification=("content_sha256",),
            allowed_executor_kinds=("native",),
        )
        result = registry.feasibility(requirement)
        self.assertTrue(result.feasible)
        self.assertEqual((), result.reason_codes)
        self.assertFalse(result.registry_grants_authority)
        self.assertFalse(result.execution_allowed)

    def test_requirement_matching_reports_all_bounded_failures(self) -> None:
        profile = available_profile("project.file.patch")
        registry = CapabilityRegistry((profile,))
        requirement = CapabilityRequirement(
            capability_id=profile.capability_id,
            capability_version=profile.version,
            capability_digest="0" * 64,
            maximum_risk="low",
            require_reversible=True,
            allowed_read_scopes=(),
            allowed_write_scopes=(),
            maximum_authority="read",
            maximum_timeout_ms=1,
            maximum_output_bytes=1,
            allowed_idempotency=("read_only",),
            required_verification=("independent_runtime_receipt",),
            allowed_executor_kinds=("native",),
        )
        result = registry.feasibility(requirement)
        self.assertFalse(result.feasible)
        self.assertEqual(
            {
                "capability_profile_drift",
                "capability_risk_exceeds_requirement",
                "capability_read_scope_not_allowed",
                "capability_write_scope_not_allowed",
                "capability_authority_exceeds_requirement",
                "capability_timeout_exceeds_requirement",
                "capability_output_exceeds_requirement",
                "capability_idempotency_not_allowed",
                "capability_verification_missing",
                "capability_executor_not_allowed",
            },
            set(result.reason_codes),
        )

    def test_unavailable_requirement_and_unknown_capability_fail_closed(self) -> None:
        registry = build_suggested_capability_registry()
        disabled = registry.feasibility(
            CapabilityRequirement(
                capability_id="project.file.read",
                capability_version=1,
                capability_digest=registry.lookup(
                    "project.file.read"
                ).capability_digest,
                allowed_read_scopes=("project.files",),
                maximum_output_bytes=300_000,
            )
        )
        self.assertEqual(("capability_disabled",), disabled.reason_codes)
        unknown = registry.feasibility(
            CapabilityRequirement(
                capability_id="project.unknown.read",
                capability_version=1,
                capability_digest="0" * 64,
            )
        )
        self.assertEqual(("unknown_capability",), unknown.reason_codes)
        self.assertIsNone(unknown.profile)
        unavailable_version = registry.feasibility(
            CapabilityRequirement(
                capability_id="project.file.read",
                capability_version=2,
                capability_digest="0" * 64,
            )
        )
        self.assertEqual(
            ("capability_version_unavailable",),
            unavailable_version.reason_codes,
        )

    def test_model_proposal_validates_types_but_never_executes_or_authorizes(self) -> None:
        profile = available_profile("project.file.read")
        registry = CapabilityRegistry((profile,))
        proposal = CapabilityProposal.create(
            profile=profile,
            typed_arguments={"relative_path": "README.md"},
            proposal_binding="proposal:v10-b:test",
        )
        self.assertFalse(proposal.grants_authority)
        self.assertFalse(proposal.execution_allowed)
        decision = registry.validate_proposal(proposal)
        self.assertEqual("validated_proposal_only", decision["decision"])
        self.assertFalse(decision["registry_grants_authority"])
        self.assertFalse(decision["execution_allowed"])

    def test_proposal_replay_digest_drift_and_manual_availability_fail_closed(
        self,
    ) -> None:
        profile = available_profile("project.file.read")
        registry = CapabilityRegistry((profile,))
        proposal = CapabilityProposal.create(
            profile=profile,
            typed_arguments={"relative_path": "README.md"},
            proposal_binding="proposal:v10-b:replay",
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_proposal_replay",
        ):
            registry.validate_proposal(
                proposal,
                seen_idempotency_keys=(proposal.idempotency_key,),
            )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "duplicate_capability_replay_ledger_entry",
        ):
            registry.validate_proposal(
                proposal,
                seen_idempotency_keys=("idem:one", "idem:one"),
            )
        drifted = replace(proposal, capability_digest="0" * 64)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_profile_drift",
        ):
            registry.validate_proposal(drifted)
        disabled_profile = suggested_capability_profiles()[0]
        disabled_registry = CapabilityRegistry((disabled_profile,))
        disabled_proposal = CapabilityProposal.create(
            profile=disabled_profile,
            typed_arguments=(
                {}
                if disabled_profile.capability_id == "git.status.read"
                else {"relative_path": "README.md"}
            ),
            proposal_binding="proposal:v10-b:disabled",
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_disabled",
        ):
            disabled_registry.validate_proposal(disabled_proposal)

    def test_argument_digest_and_secret_budget_are_fail_closed(self) -> None:
        profile = available_profile("project.file.read")
        proposal = CapabilityProposal.create(
            profile=profile,
            typed_arguments={"relative_path": "README.md"},
            proposal_binding="proposal:v10-b:digest",
        )
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_argument_digest_mismatch",
        ):
            replace(proposal, argument_digest="0" * 64)
        with self.assertRaisesRegex(
            CognitiveCapabilityError,
            "capability_secret_literal_forbidden",
        ):
            CapabilityProposal.create(
                profile=profile,
                typed_arguments={
                    "relative_path": "notes/Bearer abcdefghijklmnop.txt"
                },
                proposal_binding="proposal:v10-b:secret",
            )


if __name__ == "__main__":
    unittest.main()
