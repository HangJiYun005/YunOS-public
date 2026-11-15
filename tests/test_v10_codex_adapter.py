from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import unittest

from memory.cognitive_codex_adapter import (
    CODEX_HANDOFF_MANDATORY_PROHIBITIONS,
    CodexAdapterAuditSnapshot,
    CodexAdapterError,
    CodexHandoffArtifact,
    CodexHandoffContextReference,
    CodexHandoffOnlyAdapter,
    build_code_owned_codex_audit_snapshot,
    create_codex_handoff_artifact,
    create_codex_handoff_context_reference,
    create_codex_handoff_only_adapter,
    load_codex_handoff_artifact,
    validate_codex_audit_snapshot,
)
from memory.operational_state_models import canonical_digest


OBJECTIVE_DIGEST = "1" * 64
PLAN_DIGEST = "2" * 64
DELEGATION_DIGEST = "3" * 64


def context_references() -> tuple[CodexHandoffContextReference, ...]:
    return (
        create_codex_handoff_context_reference(
            context_kind="v7_memory_package",
            context_digest="4" * 64,
        ),
        create_codex_handoff_context_reference(
            context_kind="v9_now_package",
            context_digest="5" * 64,
        ),
        create_codex_handoff_context_reference(
            context_kind="v9_resume_package",
            context_digest="6" * 64,
        ),
    )


def handoff(**overrides: object) -> CodexHandoffArtifact:
    values: dict[str, object] = {
        "objective_digest": OBJECTIVE_DIGEST,
        "plan_digest": PLAN_DIGEST,
        "delegation_digest": DELEGATION_DIGEST,
        "context_references": context_references(),
        "authorized_read_roots": (
            r"E:\AyunOS",
            r"E:\AyunOS-v10-sandbox",
        ),
        "authorized_write_roots": (r"E:\AyunOS-v10-sandbox\work",),
        "additional_prohibited_operations": ("provider_call",),
    }
    values.update(overrides)
    return create_codex_handoff_artifact(**values)


class V10CodexAuditSnapshotTest(unittest.TestCase):
    def test_snapshot_is_fixed_code_owned_and_canonical(self) -> None:
        snapshot = build_code_owned_codex_audit_snapshot()
        self.assertEqual(snapshot.adapter_mode, "handoff_only")
        self.assertFalse(snapshot.executor_available)
        self.assertEqual(snapshot.reason_code, "codex_cli_access_denied")
        self.assertEqual(snapshot.desktop_package_version, "26.727.6591.0")
        self.assertIsNone(snapshot.cli_version)
        self.assertIsNone(snapshot.supported_executable_path)
        self.assertIsNone(snapshot.supported_noninteractive_entrypoint)
        for field in (
            "cwd_control",
            "sandbox_control",
            "approval_control",
            "command_allowlist_control",
            "structured_output_control",
            "log_control",
            "token_budget_control",
            "timeout_control",
            "process_protocol",
        ):
            self.assertEqual(getattr(snapshot, field), "unverified")
        self.assertFalse(snapshot.real_delegation_performed)
        self.assertFalse(snapshot.real_acceptance_performed)
        self.assertEqual(
            snapshot.audit_digest,
            canonical_digest(snapshot.identity_payload()),
        )
        self.assertEqual(
            validate_codex_audit_snapshot(snapshot.to_dict()),
            snapshot,
        )

    def test_snapshot_direct_construction_replace_and_tamper_fail(self) -> None:
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_audit_snapshot_factory_required",
        ):
            CodexAdapterAuditSnapshot()
        snapshot = build_code_owned_codex_audit_snapshot()
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_audit_snapshot_factory_required",
        ):
            replace(snapshot, executor_available=True)
        tampered = snapshot.to_dict()
        tampered["adapter_mode"] = "executable"
        tampered["audit_digest"] = canonical_digest(
            {
                key: value
                for key, value in tampered.items()
                if key != "audit_digest"
            }
        )
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_audit_snapshot_mismatch",
        ):
            validate_codex_audit_snapshot(tampered)


class V10CodexHandoffArtifactTest(unittest.TestCase):
    def test_artifact_is_minimal_deterministic_and_round_trips(self) -> None:
        first = handoff()
        second = handoff(
            context_references=tuple(reversed(context_references())),
            authorized_read_roots=(
                r"E:\AyunOS-v10-sandbox",
                r"E:\AyunOS",
            ),
            additional_prohibited_operations=("provider_call",),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.artifact_digest, second.artifact_digest)
        self.assertEqual(
            first.artifact_digest,
            canonical_digest(first.identity_payload()),
        )
        self.assertEqual(first.verify_canonical_digest(), first.artifact_digest)
        record = json.loads(json.dumps(first.to_dict()))
        self.assertEqual(load_codex_handoff_artifact(record), first)
        self.assertEqual(
            set(first.to_dict()),
            {
                "contract_version",
                "adapter_mode",
                "objective_digest",
                "plan_digest",
                "delegation_digest",
                "context_references",
                "authorized_read_roots",
                "authorized_write_roots",
                "prohibited_operations",
                "execution_allowed",
                "grants_authority",
                "grants_acceptance",
                "artifact_digest",
            },
        )
        serialized = json.dumps(first.to_dict(), ensure_ascii=False)
        for forbidden_field in (
            '"prompt"',
            '"command"',
            '"model_output"',
            '"execution_result"',
            '"secret_reference"',
        ):
            self.assertNotIn(forbidden_field, serialized)

    def test_factory_adds_non_removable_prohibitions_and_grants_nothing(self) -> None:
        artifact = handoff()
        self.assertTrue(
            set(CODEX_HANDOFF_MANDATORY_PROHIBITIONS).issubset(
                artifact.prohibited_operations
            )
        )
        self.assertFalse(artifact.execution_allowed)
        self.assertFalse(artifact.grants_authority)
        self.assertFalse(artifact.grants_acceptance)
        record = artifact.to_dict()
        record["prohibited_operations"].remove("git_push")
        record["artifact_digest"] = canonical_digest(
            {
                key: value
                for key, value in record.items()
                if key != "artifact_digest"
            }
        )
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_mandatory_prohibition_missing",
        ):
            load_codex_handoff_artifact(record)

    def test_direct_construction_and_replace_cannot_forge_state(self) -> None:
        artifact = handoff()
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_context_reference_factory_required",
        ):
            CodexHandoffContextReference(
                context_kind="v7_memory_package",
                context_digest="4" * 64,
            )
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_handoff_artifact_factory_required",
        ):
            CodexHandoffArtifact(
                objective_digest=artifact.objective_digest,
                plan_digest=artifact.plan_digest,
                delegation_digest=artifact.delegation_digest,
                context_references=artifact.context_references,
                authorized_read_roots=artifact.authorized_read_roots,
                authorized_write_roots=artifact.authorized_write_roots,
                prohibited_operations=artifact.prohibited_operations,
            )
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_handoff_artifact_factory_required",
        ):
            replace(
                artifact,
                execution_allowed=True,
                grants_authority=True,
                grants_acceptance=True,
            )

    def test_serialized_tamper_and_success_claim_fail_closed(self) -> None:
        artifact = handoff()
        tampered = artifact.to_dict()
        tampered["plan_digest"] = "9" * 64
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_handoff_artifact_digest_mismatch",
        ):
            load_codex_handoff_artifact(tampered)

        forged = artifact.to_dict()
        forged["execution_allowed"] = True
        forged["grants_authority"] = True
        forged["grants_acceptance"] = True
        forged["artifact_digest"] = canonical_digest(
            {
                key: value
                for key, value in forged.items()
                if key != "artifact_digest"
            }
        )
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_handoff_scope_violation",
        ):
            load_codex_handoff_artifact(forged)

        unknown_success = artifact.to_dict()
        unknown_success["execution_succeeded"] = True
        with self.assertRaisesRegex(
            CodexAdapterError,
            "invalid_codex_handoff_record",
        ):
            load_codex_handoff_artifact(unknown_success)

    def test_secret_material_is_rejected_without_echo(self) -> None:
        secret = "sk-proj-abcdefghijklmnop"
        cases = (
            {
                "authorized_read_roots": (
                    rf"E:\sandbox\api_key={secret}",
                )
            },
            {
                "context_references": (
                    {
                        "context_kind": "project_artifact",
                        "context_digest": "7" * 64,
                        "api_key": secret,
                    },
                )
            },
        )
        for overrides in cases:
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(CodexAdapterError) as captured:
                    handoff(**overrides)
                self.assertEqual(
                    captured.exception.reason_code,
                    "codex_handoff_secret_forbidden",
                )
                self.assertNotIn(secret, str(captured.exception))

    def test_roots_are_absolute_bounded_and_write_is_under_read(self) -> None:
        for root in (
            r"..\escape",
            r"\\server\share\work",
            r"E:\sandbox\..\escape",
            r"E:\sandbox:file",
            "E:\\",
        ):
            with self.subTest(root=root):
                with self.assertRaises(CodexAdapterError):
                    handoff(authorized_read_roots=(root,))
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_write_root_outside_read_roots",
        ):
            handoff(
                authorized_read_roots=(r"E:\sandbox",),
                authorized_write_roots=(r"E:\other\work",),
            )


class V10CodexHandoffOnlyAdapterTest(unittest.TestCase):
    def test_adapter_is_factory_only_and_execute_always_fails_closed(self) -> None:
        snapshot = build_code_owned_codex_audit_snapshot()
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_adapter_factory_required",
        ):
            CodexHandoffOnlyAdapter(snapshot)
        adapter = create_codex_handoff_only_adapter()
        self.assertEqual(adapter.adapter_mode, "handoff_only")
        self.assertFalse(adapter.executor_available)
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_execute_unavailable_handoff_only",
        ):
            adapter.execute(handoff())
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_execute_unavailable_handoff_only",
        ):
            adapter.execute({"execution_succeeded": True})
        with self.assertRaisesRegex(
            CodexAdapterError,
            "codex_adapter_factory_required",
        ):
            replace(adapter)

    def test_module_has_no_process_network_environment_or_file_io(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "memory"
            / "cognitive_codex_adapter.py"
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
            "subprocess",
            "urllib.request",
            "requests",
            "httpx",
        ):
            self.assertNotIn(forbidden, imported)


if __name__ == "__main__":
    unittest.main()
