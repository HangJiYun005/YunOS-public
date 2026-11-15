from __future__ import annotations

import ast
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory.operational_state_models import canonical_digest
from tools import v10_f_real_discussion_acceptance as acceptance


def substantive_document() -> str:
    evidence = (
        "YunOS 已经形成长期记忆、可信运行时、当前状态和受限认知代理四层能力，"
        "现阶段最重要的不是扩大功能数量，而是让已有能力在真实日常使用中稳定、"
        "可解释、可恢复，并持续证明搜索、本地数据和执行权限之间的边界。"
    )
    inference = (
        "因此下一阶段应把价值验证放在架构扩张之前，通过少量高频场景观察上下文"
        "是否准确、建议是否真正节省判断成本、失败是否能够被用户理解和恢复。"
    )
    recommendation = (
        "建议建立固定的周度使用回顾，选择讨论、规划和低风险准备三类任务，记录"
        "输入证据、方案质量、用户修正、时间节省和未解决警告，再据此调整工作流"
        "候选；任何新能力都必须先给出权限、回滚和验收证据。"
    )
    return "\n\n".join(
        (
            "# YunOS 下一阶段方向方案",
            "## 当前判断\n[证据] " + evidence * 2,
            "## 候选方向\n[推断] " + inference * 2,
            (
                "## 推荐路径\n[建议] 优先建设真实使用闭环，其次提升上下文质量，"
                "最后才评估新增能力。" + recommendation
            ),
            (
                "## 风险与边界\n继续保持搜索完全本地、AI 手动选择唯一 Provider、"
                "讨论不产生委托、模型不拥有工具、系统验证不等于用户接受。"
                + evidence
            ),
            "## 近期建议\n" + recommendation * 2,
        )
    )


@dataclass(frozen=True)
class FakeTypedAnswerRequest:
    binding: dict

    def audit_binding(self):
        return deepcopy(self.binding)


class FakeRuntimeService:
    def __init__(self, *, text: str | None = None, failure: Exception | None = None):
        self.text = substantive_document() if text is None else text
        self.failure = failure
        self.preview_calls = 0
        self.execute_calls = 0
        self.get_run_calls = 0
        self.last_request = None
        self.binding = {
            "version": "reviewed-answer-test-1",
            "identity": "reb_" + "a" * 64,
        }
        self.receipt = {
            "protocol_version": "runtime-test-1",
            "model_run_id": "run_" + "b" * 64,
            "conversation_id": "conversation:test-discussion",
            "request_id": acceptance.REQUEST_ID,
            "mutation_id": acceptance.MUTATION_ID,
            "status": "succeeded",
            "execution_phase": "succeeded",
            "delivery_identity": "pdv_" + "c" * 64,
            "request_artifact": {},
            "provider": acceptance.PROVIDER_ID,
            "model": acceptance.MODEL_ID,
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 700,
                "total_tokens": 1600,
            },
            "response": {
                "message_id": "message:test-discussion-assistant",
                "content": self.text,
                "provider_response_id": "response:test-discussion",
                "finish_reason": "stop",
            },
            "error": None,
            "execution": {
                "persisted": True,
                "sent_to_model": True,
                "external_model_calls": True,
            },
            "candidate_hook": {"status": "reserved", "invoked": False},
            "receipt_identity": "rcp_" + "d" * 64,
            "api_key": "sk-must-never-enter-salvage",
        }

    def preview_answer(self, request, *, request_id, mutation_id):
        self.preview_calls += 1
        self.last_request = request
        binding = request.audit_binding()
        self.receipt["request_artifact"] = {
            "artifact_identity": "rqa_" + "e" * 64,
            "cognitive_answer_request_binding": binding,
            "answer_context_binding": {
                "delivery_base_model_context_included": False,
                "delivery_base_model_context_model_visible": False,
                "wire_input_estimated_tokens": 1_500,
                "wire_input_utf8_bytes_conservative_token_upper_bound": 9_000,
                "maximum_input_tokens": acceptance.MAXIMUM_INPUT_TOKENS,
                "maximum_output_tokens": acceptance.MAXIMUM_OUTPUT_TOKENS,
                "maximum_total_tokens": acceptance.MAXIMUM_TOTAL_TOKENS,
                "maximum_cost_microunits": (
                    acceptance.MAXIMUM_COST_MICROUNITS
                ),
                "maximum_output_bytes": acceptance.MAXIMUM_OUTPUT_BYTES,
            },
        }
        return {
            "status": "preview",
            "confirmation_required": True,
            "question": acceptance.QUESTION,
            "request_id": request_id,
            "mutation_id": mutation_id,
            "diagnostic_bindings": {
                "model_run_id": self.receipt["model_run_id"],
            },
            "reviewed_execution_binding": deepcopy(self.binding),
            "cognitive_answer_request_binding": binding,
            "request_artifact": deepcopy(self.receipt["request_artifact"]),
            "execution": {
                "persisted": False,
                "credential_read": False,
                "sent_to_model": False,
                "external_model_calls": False,
                "network_request_count": 0,
                "automatic_retry": False,
                "mock_transport": False,
            },
        }

    def execute_answer(
        self,
        request,
        *,
        request_id,
        mutation_id,
        expected_bindings,
        confirmed,
        authority,
    ):
        self.execute_calls += 1
        self.last_request = request
        if self.failure is not None:
            raise self.failure
        if (
            request_id != acceptance.REQUEST_ID
            or mutation_id != acceptance.MUTATION_ID
            or expected_bindings != self.binding
            or confirmed is not True
            or authority != "mock-one-shot-authority"
        ):
            raise RuntimeError("fake_runtime_binding_mismatch")
        return {
            "status": "succeeded",
            "execution_phase": "succeeded",
            "result": deepcopy(self.receipt),
            "transport": {
                "adapter_constructor_count_this_request": 1,
                "adapter_call_count_this_request": 1,
                "model_call_count_for_run": 1,
                "exactly_one_call": True,
                "automatic_retry": False,
                "mock_transport": False,
                "network_request_count": 1,
                "credential_read": True,
            },
        }

    def get_run(self, model_run_id):
        self.get_run_calls += 1
        if model_run_id != self.receipt["model_run_id"]:
            raise RuntimeError("model_run_not_found")
        return deepcopy(self.receipt)


class V10FRealDiscussionAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v10f-discussion-")
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "acceptance"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def binding() -> dict:
        return {
            "contract_version": "cognitive-answer-runtime-request-test-1",
            "request_identity": "car_" + "1" * 64,
            "question_sha256": "2" * 64,
            "objective_digest": "3" * 64,
            "context_digest": "4" * 64,
            "v7_memory_package_digest": "5" * 64,
            "v9_now_package_digest": "6" * 64,
            "v9_resume_package_digest": "7" * 64,
            "provider_id": acceptance.PROVIDER_ID,
            "model": acceptance.MODEL_ID,
            "agency_level": "consult",
            "tools_enabled": False,
            "maximum_model_calls": 1,
            "maximum_input_tokens": acceptance.MAXIMUM_INPUT_TOKENS,
            "maximum_output_tokens": acceptance.MAXIMUM_OUTPUT_TOKENS,
            "maximum_total_tokens": acceptance.MAXIMUM_TOTAL_TOKENS,
            "maximum_cost_microunits": acceptance.MAXIMUM_COST_MICROUNITS,
            "maximum_output_bytes": acceptance.MAXIMUM_OUTPUT_BYTES,
            "context_payload_utf8_bytes": 6_800,
            "context_budget": {
                "maximum_items": 3,
                "maximum_bytes": acceptance.MAXIMUM_CONTEXT_MODEL_BYTES,
                "maximum_estimated_tokens": (
                    acceptance.MAXIMUM_CONTEXT_ESTIMATED_TOKENS
                ),
                "selected_items": 3,
                "canonical_bytes": 6_000,
                "estimated_tokens": 1_500,
            },
        }

    def bundle(self, **provider_updates) -> acceptance.DiscussionBundle:
        binding = self.binding()
        provider = {
            "provider_id": acceptance.PROVIDER_ID,
            "model": acceptance.MODEL_ID,
            "profile_version": 1,
            "profile_digest": "8" * 64,
            "selection_digest": "9" * 64,
            "selection_mode": "manual",
            "fallback_allowed": False,
            **provider_updates,
        }
        bindings = [
            {
                "source_system": system,
                "source_digest": character * 64,
                "selected_items": 1,
                "maximum_items": 1,
                "maximum_bytes": 1200,
            }
            for system, character in (
                ("v7_memory", "5"),
                ("v9_now", "6"),
                ("v9_resume", "7"),
            )
        ]
        return acceptance.DiscussionBundle(
            typed_request=FakeTypedAnswerRequest(binding),
            request_binding=binding,
            objective={
                "project_id": acceptance.PROJECT_ID,
                "agency_level": "consult",
                "cognitive_stance": "continue",
                "canonical_digest": "3" * 64,
                "authority_request": {"requested": False},
            },
            context={
                "context_digest": "4" * 64,
                "bindings": bindings,
                "selected_items": 3,
                "canonical_bytes": 6000,
                "estimated_tokens": 1500,
                "payload_utf8_bytes": 6800,
            },
            packages={
                "v7_memory": "5" * 64,
                "v9_now": "6" * 64,
                "v9_resume": "7" * 64,
            },
            provider=provider,
            readiness={
                "status": "mock_healthy",
                "chat_completion_sent": False,
                "network_calls": 0,
            },
        )

    @staticmethod
    def search_proof() -> dict:
        return {
            "local_only": True,
            "model_calls": 0,
            "provider_calls": 0,
            "api_cost_microunits": 0,
            "runtime_delta": {name: 0 for name in acceptance.RUNTIME_TABLES},
        }

    def dependencies(
        self,
        runtime: FakeRuntimeService | None = None,
        *,
        bundle_factory=None,
        search_probe=None,
    ) -> acceptance._AcceptanceDependencies:
        service = runtime or FakeRuntimeService()
        before_counts = {
            "conversations": 10,
            "request_artifacts": 10,
            "model_runs": 10,
            "messages": 20,
            "events": 50,
            "errors": 0,
            "receipts": 10,
        }

        def runtime_snapshot():
            counts = deepcopy(before_counts)
            if service.execute_calls:
                for name, delta in acceptance.EXPECTED_RUNTIME_DELTA.items():
                    counts[name] += delta
            return {
                "counts": counts,
                "integrity_check": "ok",
                "quick_check": "ok",
                "foreign_key_violations": 0,
                "ledger_digest": (
                    "d" * 64 if not service.execute_calls else "e" * 64
                ),
            }

        database_state = {
            name: {
                "database": {"exists": True, "size": 100, "sha256": char * 64},
                "wal": {"exists": False, "size": 0, "sha256": None},
            }
            for name, char in (
                ("main", "1"),
                ("profile", "2"),
                ("semantic", "3"),
                ("operational", "4"),
            )
        }
        return acceptance._AcceptanceDependencies(
            bundle_factory=bundle_factory or self.bundle,
            runtime_service=service,
            authority_factory=lambda: "mock-one-shot-authority",
            search_probe=search_probe or self.search_proof,
            runtime_snapshot=runtime_snapshot,
            database_snapshot=lambda: deepcopy(database_state),
            clock=lambda: "2026-08-01T08:00:00.000000Z",
        )

    def assert_claimed_failure(
        self,
        output: Path,
        *,
        sentry_expected: bool,
    ) -> None:
        claim_path = output / acceptance.CLAIM_NAME
        failure_path = output / acceptance.FAILURE_NAME
        self.assertTrue(claim_path.is_file())
        self.assertTrue(failure_path.is_file())
        self.assertEqual(
            sentry_expected,
            (output / acceptance.SENTRY_NAME).is_file(),
        )
        self.assertFalse((output / acceptance.RESULT_NAME).exists())
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        self.assertEqual(
            acceptance.USER_AUTHORIZATION_TEXT,
            claim["user_authorization_text"],
        )
        self.assertEqual(
            acceptance.USER_AUTHORIZATION_SHA256,
            claim["user_authorization_sha256"],
        )
        self.assertEqual(
            acceptance.COMBINED_BOUNDARY_SHA256,
            claim["combined_boundary_sha256"],
        )
        self.assertTrue(claim["do_not_rerun"])
        self.assertEqual(
            canonical_digest(
                {key: value for key, value in claim.items() if key != "claim_digest"}
            ),
            claim["claim_digest"],
        )
        self.assertEqual(claim["claim_digest"], failure["claim_digest"])
        self.assertTrue(failure["do_not_rerun"])

    def test_dry_run_is_non_creating_and_never_calls_run(self) -> None:
        stdout = io.StringIO()
        with patch.object(acceptance, "run") as real_run, patch.object(
            acceptance, "urlopen"
        ) as network, redirect_stdout(stdout):
            self.assertEqual(0, acceptance.main([]))
        real_run.assert_not_called()
        network.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertEqual("dry_run", result["status"])
        self.assertEqual(0, result["external_model_calls"])
        self.assertFalse(result["chat_completion_sent"])

    def test_legacy_three_flags_are_dry_run_until_both_digests_confirmed(
        self,
    ) -> None:
        legacy_flags = [
            "--execute",
            "--confirm-one-real-discussion",
            "--confirm-do-not-rerun",
        ]
        stdout = io.StringIO()
        with patch.object(acceptance, "run") as real_run, patch.object(
            acceptance, "urlopen"
        ) as network, redirect_stdout(stdout):
            self.assertEqual(0, acceptance.main(legacy_flags))
        real_run.assert_not_called()
        network.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertEqual("dry_run", result["status"])
        self.assertEqual(0, result["external_model_calls"])
        self.assertFalse(result["chat_completion_sent"])
        self.assertIn(
            "--confirm-user-authorization-sha256="
            + acceptance.USER_AUTHORIZATION_SHA256,
            result["required_flags"],
        )
        self.assertIn(
            "--confirm-combined-boundary-sha256="
            + acceptance.COMBINED_BOUNDARY_SHA256,
            result["required_flags"],
        )

        confirmed = acceptance.parse_args(
            legacy_flags
            + [
                "--confirm-user-authorization-sha256",
                acceptance.USER_AUTHORIZATION_SHA256,
                "--confirm-combined-boundary-sha256",
                acceptance.COMBINED_BOUNDARY_SHA256,
            ]
        )
        self.assertEqual(
            acceptance.USER_AUTHORIZATION_SHA256,
            confirmed.confirm_user_authorization_sha256,
        )
        self.assertEqual(
            acceptance.COMBINED_BOUNDARY_SHA256,
            confirmed.confirm_combined_boundary_sha256,
        )
        for option in (
            "--confirm-user-authorization-sha256",
            "--confirm-combined-boundary-sha256",
        ):
            with self.subTest(option=option), redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                acceptance.parse_args(legacy_flags + [option, "0" * 64])

    def test_consent_digest_and_windows_directory_open_contract_are_fixed(
        self,
    ) -> None:
        consent = "我知情并批准上述 DeepSeek 外发边界。"
        expected_digest = hashlib.sha256(consent.encode("utf-8")).hexdigest()
        self.assertEqual(
            "8f9be850fac5ef6b5eaf9a78b8856775e659412a6a81d6295d05c5dddea0c6b1",
            expected_digest,
        )
        self.assertEqual(consent, acceptance.USER_AUTHORIZATION_TEXT)
        self.assertEqual(expected_digest, acceptance.USER_AUTHORIZATION_SHA256)
        self.assertEqual(
            0x00000080,
            acceptance.WINDOWS_DIRECTORY_DESIRED_ACCESS,
        )
        self.assertEqual(0x00000003, acceptance.WINDOWS_DIRECTORY_SHARE_MODE)
        self.assertEqual(
            0,
            acceptance.WINDOWS_DIRECTORY_DESIRED_ACCESS & 0x00010000,
        )

        shared_source = inspect.getsource(
            acceptance._DirectoryHandlePins._open_windows_directory
        )
        self.assertIn("WINDOWS_DIRECTORY_DESIRED_ACCESS", shared_source)
        self.assertIn("WINDOWS_DIRECTORY_SHARE_MODE", shared_source)
        production_source = inspect.getsource(
            acceptance._FixedProductionDiscussionExecutor.run
        )
        module_tree = ast.parse(
            Path(acceptance.__file__).read_text(encoding="utf-8")
        )
        executor_node = next(
            node
            for node in module_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "_FixedProductionDiscussionExecutor"
        )
        production_node = next(
            node
            for node in executor_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        production_assignments = {
            node.targets[0].id: node.value
            for node in production_node.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        self.assertEqual(
            consent,
            ast.literal_eval(production_assignments["USER_AUTHORIZATION_TEXT"]),
        )
        self.assertIn(
            "WINDOWS_DIRECTORY_DESIRED_ACCESS = 0x00000080",
            production_source,
        )
        self.assertIn(
            "0x00000001 | 0x00000002",
            production_source,
        )
        self.assertIn(
            "USER_AUTHORIZATION_SHA256 = production_hashlib.sha256",
            production_source,
        )
        self.assertNotIn("0x00010000 | 0x00000080", production_source)
        self.assertNotIn("0x00000004", production_source)
        self.assertEqual(
            "aaefa69451dae59d5d13df20f0a6efde03404a3a87652c71dfcacfddae5044ef",
            acceptance.COMBINED_BOUNDARY_SHA256,
        )

    def test_public_runner_and_cli_expose_no_path_provider_or_transport_override(self) -> None:
        self.assertEqual((), tuple(inspect.signature(acceptance.run).parameters))
        args = acceptance.parse_args([])
        for name in ("output_dir", "provider", "model", "base_url", "transport"):
            self.assertFalse(hasattr(args, name))
        for option in (
            "--output-dir",
            "--provider",
            "--model",
            "--base-url",
            "--transport",
        ):
            with self.subTest(option=option), redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                acceptance.parse_args([option, "override"])

    def test_public_run_uses_a_fresh_exact_executor_not_a_singleton(self) -> None:
        seen: list[object] = []

        def fake_production_run(executor):
            self.assertIs(
                type(executor),
                acceptance._FixedProductionDiscussionExecutor,
            )
            seen.append(executor)
            return {"instance": len(seen)}

        class ForbiddenLegacySingleton:
            def run(self):
                raise AssertionError("legacy singleton was used")

        with patch.object(
            acceptance._FixedProductionDiscussionExecutor,
            "run",
            new=fake_production_run,
        ), patch.object(
            acceptance,
            "_FIXED_PRODUCTION_DISCUSSION_EXECUTOR",
            new=ForbiddenLegacySingleton(),
            create=True,
        ):
            self.assertEqual({"instance": 1}, acceptance.run())
            self.assertEqual({"instance": 2}, acceptance.run())
        self.assertEqual(2, len(seen))
        self.assertIsNot(seen[0], seen[1])
        self.assertFalse(
            hasattr(acceptance, "_FIXED_PRODUCTION_DISCUSSION_EXECUTOR")
        )

    def test_imported_test_core_rejects_formal_root_alias_and_reparse_first(self) -> None:
        dependency_calls: list[str] = []

        def forbidden(name):
            def call():
                dependency_calls.append(name)
                raise AssertionError(name)

            return call

        dependencies = acceptance._AcceptanceDependencies(
            bundle_factory=forbidden("bundle"),
            runtime_service=object(),
            authority_factory=forbidden("authority"),
            search_probe=forbidden("search"),
            runtime_snapshot=forbidden("runtime_snapshot"),
            database_snapshot=forbidden("database_snapshot"),
            clock=forbidden("clock"),
        )
        imported_test_core = acceptance._run_with_dependencies
        formal_existed = acceptance.PRODUCTION_OUTPUT_ROOT.exists()
        with patch.object(acceptance, "_file_gate") as file_gate, patch.object(
            acceptance,
            "_write_exclusive",
        ) as writer, self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "production_discussion_dependency_injection_forbidden",
        ):
            imported_test_core(
                acceptance.PRODUCTION_OUTPUT_ROOT,
                dependencies,
            )
        file_gate.assert_not_called()
        writer.assert_not_called()
        self.assertEqual([], dependency_calls)
        self.assertEqual(
            formal_existed,
            acceptance.PRODUCTION_OUTPUT_ROOT.exists(),
        )

        canonical_alias = self.root / "canonical-production-alias"
        real_resolved = acceptance._resolved

        def alias_resolved(path: Path) -> Path:
            if acceptance._lexical_absolute(Path(path)) == canonical_alias:
                return acceptance._lexical_absolute(
                    acceptance.PRODUCTION_OUTPUT_ROOT
                )
            return real_resolved(Path(path))

        with patch.object(
            acceptance,
            "_resolved",
            side_effect=alias_resolved,
        ), self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "production_discussion_dependency_injection_forbidden",
        ):
            imported_test_core(canonical_alias, dependencies)
        self.assertEqual([], dependency_calls)
        self.assertFalse(canonical_alias.exists())

        reparse_parent = self.root / "simulated-reparse-parent"
        reparse_parent.mkdir()
        original_reparse_probe = acceptance.is_reparse_point

        def simulated_reparse(path: Path) -> bool:
            return (
                acceptance._lexical_absolute(Path(path)) == reparse_parent
                or original_reparse_probe(Path(path))
            )

        with patch.object(
            acceptance,
            "is_reparse_point",
            side_effect=simulated_reparse,
        ), self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "discussion_output_reparse_denied",
        ):
            imported_test_core(reparse_parent / "acceptance", dependencies)
        self.assertEqual([], dependency_calls)

    def test_every_shared_output_seam_rejects_forged_production_targets(self) -> None:
        formal = (
            Path(acceptance.__file__).resolve().parents[1]
            / "output"
            / "v10-f-production-adoption"
            / "real-discussion-acceptance"
        )
        formal_existed = formal.exists()

        def forged_target(*, production: bool):
            target = object.__new__(acceptance._DiscussionOutputTarget)
            object.__setattr__(target, "output_root", formal)
            object.__setattr__(target, "production", production)
            object.__setattr__(target, "output_anchor", formal.parent)
            object.__setattr__(target, "output_anchor_identity", (0, 0, 0, 0))
            object.__setattr__(target, "output_root_identity", None)
            return target

        def seam_calls(target):
            return (
                lambda: acceptance._open_pinned_output_directory_handles(target),
                lambda: acceptance._create_pinned_output_root(target, object()),
                lambda: acceptance._assert_pinned_output_root(target, object()),
                lambda: acceptance._write_pinned_output_exclusive(
                    target=target,
                    pins=object(),
                    name=acceptance.CLAIM_NAME,
                    payload={"forged": True},
                ),
                lambda: acceptance._write_result_exclusive(
                    target=target,
                    pins=object(),
                    payload={"forged": True},
                    runtime_readback={},
                ),
                lambda: acceptance._write_pinned_result_exclusive(
                    target=target,
                    pins=object(),
                    payload={"forged": True},
                    runtime_readback={},
                ),
                lambda: acceptance._write_exclusive_pinned(
                    target=target,
                    pins=object(),
                    name=acceptance.CLAIM_NAME,
                    payload={"forged": True},
                ),
                lambda: acceptance._write_exclusive(
                    target.output_root / acceptance.CLAIM_NAME,
                    {"forged": True},
                ),
            )

        for production in (False, True):
            target = forged_target(production=production)
            for index, call in enumerate(seam_calls(target)):
                with self.subTest(
                    production=production,
                    seam=index,
                ), self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "production.*forbidden|production.*injection",
                ):
                    call()
        self.assertEqual(formal_existed, formal.exists())

        alias = self.root / "formal-canonical-alias"
        alias_target = object.__new__(acceptance._DiscussionOutputTarget)
        object.__setattr__(alias_target, "output_root", alias)
        object.__setattr__(alias_target, "production", False)
        object.__setattr__(alias_target, "output_anchor", self.root)
        object.__setattr__(
            alias_target,
            "output_anchor_identity",
            acceptance._filesystem_identity(self.root),
        )
        object.__setattr__(alias_target, "output_root_identity", None)
        real_resolved = acceptance._resolved

        def alias_resolved(path: Path) -> Path:
            lexical = acceptance._lexical_absolute(Path(path))
            try:
                relative = lexical.relative_to(alias)
            except ValueError:
                pass
            else:
                return formal / relative
            return real_resolved(Path(path))

        with patch.object(
            acceptance,
            "_resolved",
            side_effect=alias_resolved,
        ):
            for index, call in enumerate(seam_calls(alias_target)):
                with self.subTest(alias_seam=index), self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "production.*forbidden|production.*injection",
                ):
                    call()
        self.assertFalse(alias.exists())

        mutated_formal = self.root / "mutated-formal"
        with patch.object(
            acceptance,
            "PRODUCTION_OUTPUT_ROOT",
            new=mutated_formal,
        ), self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "production_discussion_dependency_injection_forbidden",
        ):
            acceptance._DiscussionOutputTarget(
                output_root=mutated_formal,
                production=True,
            )
        self.assertFalse(mutated_formal.exists())

    def test_generic_writers_cannot_publish_an_unbound_result(self) -> None:
        target = acceptance._DiscussionOutputTarget(
            output_root=self.output,
            production=False,
        )
        with acceptance._pinned_output_directory_handles(target) as pins:
            acceptance._create_pinned_output_root(target, pins)
            for name, call in (
                (
                    "generic-pinned",
                    lambda: acceptance._write_pinned_output_exclusive(
                        target=target,
                        pins=pins,
                        name=acceptance.RESULT_NAME,
                        payload={"status": "GO"},
                    ),
                ),
                (
                    "low-level-pinned",
                    lambda: acceptance._write_exclusive_pinned(
                        target=target,
                        pins=pins,
                        name=acceptance.RESULT_NAME,
                        payload={"status": "GO"},
                    ),
                ),
                (
                    "path-writer",
                    lambda: acceptance._write_exclusive(
                        self.output / acceptance.RESULT_NAME,
                        {"status": "GO"},
                    ),
                ),
                (
                    "validated-result-without-bindings",
                    lambda: acceptance._write_pinned_result_exclusive(
                        target=target,
                        pins=pins,
                        payload={"status": "GO"},
                        runtime_readback={},
                    ),
                ),
            ):
                with self.subTest(name=name), self.assertRaises(
                    acceptance.AcceptanceError
                ):
                    call()
        self.assertFalse((self.output / acceptance.RESULT_NAME).exists())

    def test_fixed_production_executor_has_no_injection_or_shared_core_path(self) -> None:
        self.assertIsNone(acceptance.run.__closure__)
        self.assertIsNone(acceptance.run.__defaults__)
        self.assertIsNone(acceptance.run.__kwdefaults__)
        self.assertEqual({}, inspect.getclosurevars(acceptance.run).nonlocals)
        self.assertEqual((), tuple(inspect.signature(acceptance.run).parameters))
        production_method = acceptance._FixedProductionDiscussionExecutor.run
        self.assertEqual(
            ("self",),
            tuple(inspect.signature(production_method).parameters),
        )
        self.assertIsNone(production_method.__closure__)
        self.assertIsNone(production_method.__defaults__)
        self.assertIsNone(production_method.__kwdefaults__)
        self.assertEqual(
            {"run"},
            {
                name
                for name, value in vars(
                    acceptance._FixedProductionDiscussionExecutor
                ).items()
                if inspect.isfunction(value)
            },
        )
        production_source = inspect.getsource(production_method)
        for forbidden in (
            "_run_with_dependencies",
            "_DiscussionOutputTarget",
            "_open_pinned_output_directory_handles",
            "_create_pinned_output_root",
            "_AcceptanceDependencies(",
            "PRODUCTION_OUTPUT_ROOT",
        ):
            self.assertNotIn(forbidden, production_source)
        for required in (
            "class ProductionDirectoryPins",
            "class ProductionDependencies",
            "def production_write_named",
            "def production_validate_result",
            "def _write_pinned_output_exclusive",
            "def _write_pinned_result_exclusive",
            "live_readback = runtime_service.get_run(model_run_id)",
            "live_snapshot = production_runtime_snapshot()",
            "def bundle_factory",
            "def authority_factory",
        ):
            self.assertIn(required, production_source)
        self.assertFalse(hasattr(acceptance, "_build_production_dependencies"))
        self.assertFalse(hasattr(acceptance, "_salvage_failure"))
        self.assertFalse(
            hasattr(acceptance, "_FIXED_PRODUCTION_DISCUSSION_EXECUTOR")
        )

        module_tree = ast.parse(
            Path(acceptance.__file__).read_text(encoding="utf-8")
        )
        executor_node = next(
            node
            for node in module_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "_FixedProductionDiscussionExecutor"
        )
        production_node = next(
            node
            for node in executor_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        forbidden_calls = {
            "_run_with_dependencies",
            "_DiscussionOutputTarget",
            "_open_pinned_output_directory_handles",
            "_create_pinned_output_root",
            "_write_result_exclusive",
            "_write_exclusive",
            "_write_exclusive_pinned",
        }
        self.assertEqual(
            set(),
            {
                node.func.id
                for node in ast.walk(production_node)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_calls
            },
        )
        claim_position = production_source.index(
            "production_write_named(CLAIM_NAME, claim)"
        )
        for sensitive in (
            "from memory.production_runtime_execution import",
            "from memory.runtime_enablement import",
            "runtime_service = ProductionRuntimeExecutionService()",
            "legacy_before = _file_gate(LEGACY_V10_C_SENTRY)",
            "database_before = dict(deps.database_snapshot())",
            "search_proof = dict(deps.search_probe())",
            "bundle = deps.bundle_factory()",
            "preview = deps.runtime_service.preview_answer(",
        ):
            self.assertLess(claim_position, production_source.index(sensitive))

        public_run_nodes = [
            node
            for node in module_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        ]
        self.assertEqual(1, len(public_run_nodes))
        public_run_source = inspect.getsource(acceptance.run)
        self.assertIn(
            "return _FixedProductionDiscussionExecutor().run()",
            public_run_source,
        )
        self.assertNotIn("_FIXED_PRODUCTION", public_run_source)

        raw_callables = []
        captured_raw_callables = []
        for name, value in vars(acceptance).items():
            if (
                not inspect.isfunction(value)
                or value.__module__ != acceptance.__name__
            ):
                continue
            if {"output_root", "deps"} <= set(
                inspect.signature(value).parameters
            ):
                raw_callables.append(name)
            for cell in value.__closure__ or ():
                try:
                    captured = cell.cell_contents
                except ValueError:
                    continue
                if not callable(captured):
                    continue
                try:
                    parameters = set(inspect.signature(captured).parameters)
                except (TypeError, ValueError):
                    continue
                if {"output_root", "deps"} <= parameters:
                    captured_raw_callables.append(name)
        self.assertEqual(["_run_with_dependencies"], raw_callables)
        self.assertEqual([], captured_raw_callables)
        self.assertIsNone(acceptance._run_with_dependencies.__defaults__)
        self.assertIsNone(acceptance._run_with_dependencies.__kwdefaults__)
        self.assertIsNone(acceptance._run_with_dependencies.__closure__)

        with patch.object(
            acceptance,
            "_DiscussionOutputTarget",
        ) as target_factory, patch.object(
            acceptance,
            "_runtime_snapshot",
        ) as runtime_snapshot, self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "exact_production_discussion_executor_required",
        ):
            acceptance._FixedProductionDiscussionExecutor.run(object())
        target_factory.assert_not_called()
        runtime_snapshot.assert_not_called()

    def test_module_uses_only_typed_answer_runtime_not_old_or_tool_paths(self) -> None:
        source = inspect.getsource(acceptance)
        self.assertNotIn("v10_c_real_acceptance", source)
        self.assertNotIn("preview_bounded_tool", source)
        self.assertNotIn("execute_bounded_tool", source)
        self.assertNotIn("runtime_service.preview(", source)
        self.assertNotIn("runtime_service.execute(", source)
        self.assertIn("create_cognitive_answer_runtime_request", source)
        self.assertIn("runtime_service.preview_answer(", source)
        self.assertIn("runtime_service.execute_answer(", source)

    def test_production_typed_answer_api_is_really_importable(self) -> None:
        from memory.production_runtime_execution import (
            ProductionRuntimeExecutionService,
        )
        from memory.runtime_protocol_contracts import (
            create_cognitive_answer_runtime_request,
        )

        self.assertTrue(callable(create_cognitive_answer_runtime_request))
        service = ProductionRuntimeExecutionService()
        self.assertTrue(callable(service.preview_answer))
        self.assertTrue(callable(service.execute_answer))
        self.assertIn(
            "answer_request", inspect.signature(service.preview_answer).parameters
        )
        self.assertIn(
            "answer_request", inspect.signature(service.execute_answer).parameters
        )

    def test_production_context_is_real_v7_now_resume_and_strictly_budgeted(self) -> None:
        source = inspect.getsource(acceptance._build_production_bundle)
        for required in (
            "build_production_memory_package",
            'RetrievalQuery("YunOS")',
            "future_token_budget=500",
            'now_package(budget_profile="Small")',
            "resume_package(",
            "_minimum_context_sources",
            "maximum_bytes=MAXIMUM_CONTEXT_MODEL_BYTES",
            "maximum_estimated_tokens=MAXIMUM_CONTEXT_ESTIMATED_TOKENS",
            "create_cognitive_answer_runtime_request",
            "maximum_input_tokens=MAXIMUM_INPUT_TOKENS",
            "maximum_output_tokens=MAXIMUM_OUTPUT_TOKENS",
            "maximum_total_tokens=MAXIMUM_TOTAL_TOKENS",
            "maximum_cost_microunits=MAXIMUM_COST_MICROUNITS",
        ):
            self.assertIn(required, source)
        self.assertNotIn("prepare_model_request", source)
        self.assertNotIn("ProductionCognitiveRuntimeBridge", source)
        self.assertFalse(acceptance.ANSWER_RESPONSE_CONTRACT["tools_enabled"])
        self.assertFalse(
            acceptance.ANSWER_RESPONSE_CONTRACT["delegation_allowed"]
        )
        self.assertIn("650 至 850", acceptance.ANSWER_INSTRUCTION)

    def test_response_contract_requires_substantive_grounded_chinese_document(self) -> None:
        result = acceptance.validate_discussion_document(substantive_document())
        self.assertTrue(result["valid"])
        for invalid in (
            "太短",
            substantive_document().replace("[证据]", "证据", 1),
            substantive_document() + "\n建议创建 v11。",
            substantive_document() + '\n{"tool_calls": []}',
            substantive_document() + "\n我将调用工具执行写入。",
            substantive_document() + "\n请调用工具执行写入。",
            substantive_document() + "\n系统已创建委托。",
            substantive_document() + "\n立即创建委托。",
            substantive_document() + "\n我已接受本结果。",
            substantive_document() + "\n本回答已验收通过。",
            substantive_document() + "\n本结果已经由系统验证通过。",
            substantive_document() + "\n系统验证：通过。",
            substantive_document() + "\nsk-abcdefghijklmnop",
        ):
            with self.subTest(invalid=invalid[-30:]):
                self.assertFalse(
                    acceptance.validate_discussion_document(invalid)["valid"]
                )
        safe_boundary_advice = (
            substantive_document()
            + "\n[建议] 后续应先验证再决定是否采用自动化工具；不应调用工具或创建委托，"
            "系统验证也不等于用户接受。"
        )
        self.assertTrue(
            acceptance.validate_discussion_document(safe_boundary_advice)[
                "valid"
            ]
        )

    def test_mock_acceptance_is_exactly_once_audited_and_not_accepted(self) -> None:
        runtime = FakeRuntimeService()
        report = acceptance._run_with_dependencies(
            self.output,
            self.dependencies(runtime),
        )
        self.assertEqual("GO", report["status"])
        self.assertEqual(1, runtime.preview_calls)
        self.assertEqual(1, runtime.execute_calls)
        self.assertEqual(1, runtime.get_run_calls)
        self.assertTrue((self.output / acceptance.CLAIM_NAME).is_file())
        self.assertTrue((self.output / acceptance.SENTRY_NAME).is_file())
        self.assertTrue((self.output / acceptance.RESULT_NAME).is_file())
        self.assertFalse((self.output / acceptance.FAILURE_NAME).exists())
        self.assertFalse(
            (
                self.output
                / acceptance.WINDOWS_OWNED_DIRECTORY_GUARD_NAME
            ).exists()
        )
        self.assertEqual(
            "system_verified_not_user_accepted", report["acceptance_state"]
        )
        self.assertIsNone(report["cost"]["actual_cost_microunits"])
        self.assertEqual(
            acceptance.MAXIMUM_COST_MICROUNITS,
            report["cost"]["conservative_ceiling_microunits"],
        )
        self.assertTrue(report["checks"]["exactly_one_model_call"])
        self.assertTrue(report["checks"]["readonly_databases_unchanged"])
        self.assertTrue(report["checks"]["legacy_v10_c_sentry_unchanged"])
        self.assertEqual(
            acceptance.EXPECTED_RUNTIME_DELTA,
            report["runtime"]["count_delta"],
        )
        claim = json.loads(
            (self.output / acceptance.CLAIM_NAME).read_text(encoding="utf-8")
        )
        sentry = json.loads(
            (self.output / acceptance.SENTRY_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            acceptance.USER_AUTHORIZATION_TEXT,
            claim["user_authorization_text"],
        )
        self.assertEqual(
            acceptance.USER_AUTHORIZATION_SHA256,
            claim["user_authorization_sha256"],
        )
        self.assertEqual(
            acceptance.COMBINED_BOUNDARY_SHA256,
            claim["combined_boundary_sha256"],
        )
        self.assertEqual(claim["claim_digest"], sentry["claim_digest"])
        self.assertEqual(claim["claim_digest"], report["claim_digest"])
        self.assertEqual(
            canonical_digest(runtime.receipt),
            report["runtime"]["runtime_readback_digest"],
        )

    def test_claim_exists_before_every_dependency_and_runtime_step(self) -> None:
        runtime = FakeRuntimeService()
        base = self.dependencies(runtime)
        observed: list[str] = []

        def observe(name: str) -> None:
            claim_path = self.output / acceptance.CLAIM_NAME
            self.assertTrue(claim_path.is_file(), name)
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            self.assertEqual(
                acceptance.USER_AUTHORIZATION_SHA256,
                claim["user_authorization_sha256"],
            )
            self.assertEqual(
                acceptance.COMBINED_BOUNDARY_SHA256,
                claim["combined_boundary_sha256"],
            )
            observed.append(name)

        def guarded(name, call):
            def invoke(*args, **kwargs):
                observe(name)
                return call(*args, **kwargs)

            return invoke

        runtime.preview_answer = guarded(
            "preview_answer",
            runtime.preview_answer,
        )
        runtime.execute_answer = guarded(
            "execute_answer",
            runtime.execute_answer,
        )
        runtime.get_run = guarded("get_run", runtime.get_run)
        deps = acceptance._AcceptanceDependencies(
            bundle_factory=guarded("bundle_factory", base.bundle_factory),
            runtime_service=runtime,
            authority_factory=guarded(
                "authority_factory",
                base.authority_factory,
            ),
            search_probe=guarded("search_probe", base.search_probe),
            runtime_snapshot=guarded(
                "runtime_snapshot",
                base.runtime_snapshot,
            ),
            database_snapshot=guarded(
                "database_snapshot",
                base.database_snapshot,
            ),
            clock=guarded("clock", base.clock),
        )
        report = acceptance._run_with_dependencies(self.output, deps)
        self.assertEqual("GO", report["status"])
        self.assertEqual(
            {
                "authority_factory",
                "bundle_factory",
                "clock",
                "database_snapshot",
                "execute_answer",
                "get_run",
                "preview_answer",
                "runtime_snapshot",
                "search_probe",
            },
            set(observed),
        )

    def test_preview_side_effect_then_crash_is_permanently_one_shot(self) -> None:
        runtime = FakeRuntimeService()
        side_effects = 0

        def preview_then_crash(*args, **kwargs):
            nonlocal side_effects
            self.assertTrue((self.output / acceptance.CLAIM_NAME).is_file())
            side_effects += 1
            raise RuntimeError("preview_side_effect_then_crash")

        runtime.preview_answer = preview_then_crash
        deps = self.dependencies(runtime)
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "discussion_dispatch_failed_do_not_rerun",
        ):
            acceptance._run_with_dependencies(self.output, deps)
        self.assertEqual(1, side_effects)
        self.assert_claimed_failure(self.output, sentry_expected=False)

        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "one_shot_discussion_claim_exists_do_not_rerun",
        ):
            acceptance._run_with_dependencies(self.output, deps)
        self.assertEqual(1, side_effects)

    def test_result_publication_rejects_self_signed_binding_tampering(self) -> None:
        original_writer = acceptance._write_result_exclusive

        def resign(report: dict) -> None:
            report.pop("report_digest", None)
            report["report_digest"] = canonical_digest(report)

        for mode in (
            "claim",
            "model_run",
            "delta",
            "runtime_readback",
        ):
            with self.subTest(mode=mode):
                output = self.root / ("tampered-result-" + mode)
                runtime = FakeRuntimeService()

                def tampering_writer(
                    *,
                    target,
                    pins,
                    payload,
                    runtime_readback,
                    mode=mode,
                ):
                    forged_report = deepcopy(dict(payload))
                    forged_readback = deepcopy(dict(runtime_readback))
                    if mode == "claim":
                        forged_report["claim_digest"] = "f" * 64
                    elif mode == "model_run":
                        forged_report["runtime"]["model_run_id"] = (
                            "run_" + "f" * 64
                        )
                    elif mode == "delta":
                        forged_report["runtime"]["count_delta"][
                            "events"
                        ] += 1
                    else:
                        forged_readback["status"] = "failed"
                        forged_readback["execution_phase"] = "failed"
                        forged_report["runtime"][
                            "runtime_readback_digest"
                        ] = canonical_digest(forged_readback)
                    resign(forged_report)
                    original_writer(
                        target=target,
                        pins=pins,
                        payload=forged_report,
                        runtime_readback=forged_readback,
                    )

                with patch.object(
                    acceptance,
                    "_write_result_exclusive",
                    new=tampering_writer,
                ), self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "discussion_result_binding_invalid",
                ):
                    acceptance._run_with_dependencies(
                        output,
                        self.dependencies(runtime),
                    )
                self.assertFalse((output / acceptance.RESULT_NAME).exists())
                self.assert_claimed_failure(output, sentry_expected=True)

    def test_directory_handles_block_or_detect_real_anchor_and_output_swap(self) -> None:
        anchor = self.root / "pinned-anchor"
        anchor.mkdir()
        output = anchor / "acceptance-output"
        external = self.root / "external-swap-target"
        external.mkdir()
        target = acceptance._DiscussionOutputTarget(
            output_root=output,
            production=False,
        )
        with acceptance._pinned_output_directory_handles(target) as pins:
            acceptance._create_pinned_output_root(target, pins)
            pins.assert_path(target.output_root)
            if os.name == "nt":
                with self.assertRaises(OSError) as remove_error:
                    target.output_root.rmdir()
                with self.assertRaises(OSError) as output_error:
                    target.output_root.rename(self.root / "moved-pinned-output")
                with self.assertRaises(OSError) as anchor_error:
                    anchor.rename(self.root / "moved-pinned-anchor")
                replacement = self.root / "replacement-output"
                replacement.mkdir()
                with self.assertRaises(OSError) as replacement_error:
                    replacement.replace(target.output_root)
                self.assertIn(remove_error.exception.winerror, {5, 32, 145})
                for error in (
                    output_error.exception,
                    anchor_error.exception,
                    replacement_error.exception,
                ):
                    self.assertIn(error.winerror, {5, 32})
                self.assertTrue(replacement.is_dir())
                pins.assert_all()
            else:
                moved = self.root / "moved-pinned-output"
                target.output_root.rename(moved)
                target.output_root.symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "discussion_output_root_identity_changed|"
                    "discussion_output_reparse_denied|"
                    "discussion_directory_handle_path_changed",
                ):
                    acceptance._write_pinned_output_exclusive(
                        target=target,
                        pins=pins,
                        name=acceptance.SENTRY_NAME,
                        payload={"must_not_escape": True},
                    )
        self.assertFalse((external / acceptance.SENTRY_NAME).exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle sharing contract")
    def test_project_root_pin_coexists_with_an_existing_process_cwd(self) -> None:
        project_root = Path(acceptance.__file__).resolve().parents[1]
        cwd_holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self.assertIsNone(cwd_holder.poll())
            pins = acceptance._DirectoryHandlePins()
            try:
                pins.pin(project_root)
                pins.assert_path(project_root)
            finally:
                pins.close()
        finally:
            cwd_holder.terminate()
            try:
                cwd_holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cwd_holder.kill()
                cwd_holder.wait(timeout=5)

    def test_handles_remain_pinned_through_runtime_and_result_publication(self) -> None:
        runtime = FakeRuntimeService()
        runtime_swap_blocks: list[int | None] = []
        result_swap_blocks: list[int | None] = []
        original_execute = runtime.execute_answer
        original_result_writer = acceptance._write_result_exclusive
        moved = self.root / "result-publication-moved-output"
        external = self.root / "result-publication-external"
        external.mkdir()

        def execute_with_swap_probe(*args, **kwargs):
            if os.name == "nt":
                try:
                    self.output.rename(self.root / "runtime-moved-output")
                except OSError as error:
                    runtime_swap_blocks.append(error.winerror)
                else:
                    self.fail("output root rename escaped during Runtime")
            return original_execute(*args, **kwargs)

        def result_writer_with_swap_probe(
            *,
            target,
            pins,
            payload,
            runtime_readback,
        ):
            if os.name == "nt":
                try:
                    self.output.rename(moved)
                except OSError as error:
                    result_swap_blocks.append(error.winerror)
                else:
                    self.fail(
                        "output root rename escaped during result publication"
                    )
            else:
                self.output.rename(moved)
                self.output.symlink_to(external, target_is_directory=True)
            original_result_writer(
                target=target,
                pins=pins,
                payload=payload,
                runtime_readback=runtime_readback,
            )

        runtime.execute_answer = execute_with_swap_probe
        with patch.object(
            acceptance,
            "_write_result_exclusive",
            new=result_writer_with_swap_probe,
        ):
            if os.name == "nt":
                report = acceptance._run_with_dependencies(
                    self.output,
                    self.dependencies(runtime),
                )
                self.assertEqual("GO", report["status"])
            else:
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "discussion_output_root_identity_changed|"
                    "discussion_output_reparse_denied|"
                    "discussion_directory_handle_path_changed",
                ):
                    acceptance._run_with_dependencies(
                        self.output,
                        self.dependencies(runtime),
                    )
        if os.name == "nt":
            self.assertEqual(1, len(runtime_swap_blocks))
            self.assertIn(runtime_swap_blocks[0], {5, 32})
            self.assertEqual(1, len(result_swap_blocks))
            self.assertIn(result_swap_blocks[0], {5, 32})
            self.assertTrue((self.output / acceptance.RESULT_NAME).is_file())
        else:
            self.assertFalse((external / acceptance.RESULT_NAME).exists())

    def test_failure_salvage_publication_retains_the_same_handle_set(self) -> None:
        runtime = FakeRuntimeService(failure=RuntimeError("provider_failed"))
        blocked_failure_swap: list[int | None] = []
        original_writer = acceptance._write_exclusive_pinned

        def writer_with_failure_swap_probe(*, target, pins, name, payload):
            if name == acceptance.FAILURE_NAME and os.name == "nt":
                try:
                    self.output.rename(
                        self.root / "failure-salvage-moved-output"
                    )
                except OSError as error:
                    blocked_failure_swap.append(error.winerror)
                else:
                    self.fail("output root rename escaped during failure salvage")
            original_writer(
                target=target,
                pins=pins,
                name=name,
                payload=payload,
            )

        with patch.object(
            acceptance,
            "_write_exclusive_pinned",
            new=writer_with_failure_swap_probe,
        ), self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "discussion_dispatch_failed_do_not_rerun",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(runtime),
            )
        if os.name == "nt":
            self.assertEqual(1, len(blocked_failure_swap))
            self.assertIn(blocked_failure_swap[0], {5, 32})
        self.assertTrue((self.output / acceptance.SENTRY_NAME).is_file())
        self.assertTrue((self.output / acceptance.FAILURE_NAME).is_file())
        self.assertFalse(
            (
                self.output
                / acceptance.WINDOWS_OWNED_DIRECTORY_GUARD_NAME
            ).exists()
        )
        self.assertEqual(1, runtime.execute_calls)

    def test_existing_claim_refuses_replay_before_preview_or_execute(self) -> None:
        runtime = FakeRuntimeService()
        deps = self.dependencies(runtime)
        acceptance._run_with_dependencies(self.output, deps)
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "one_shot_discussion_claim_exists_do_not_rerun",
        ):
            acceptance._run_with_dependencies(self.output, deps)
        self.assertEqual(1, runtime.preview_calls)
        self.assertEqual(1, runtime.execute_calls)

    def test_transport_failure_keeps_sentry_and_writes_salvage_without_retry(self) -> None:
        runtime = FakeRuntimeService(failure=RuntimeError("provider_failed"))
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "discussion_dispatch_failed_do_not_rerun",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(runtime),
            )
        self.assertEqual(1, runtime.preview_calls)
        self.assertEqual(1, runtime.execute_calls)
        self.assertTrue((self.output / acceptance.SENTRY_NAME).is_file())
        salvage = json.loads(
            (self.output / acceptance.FAILURE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual("NO_GO_DO_NOT_RERUN", salvage["status"])
        self.assertTrue(salvage["do_not_rerun"])
        runtime_summary = salvage["runtime_readback"]
        self.assertEqual(runtime.receipt["model_run_id"], salvage["model_run_id"])
        self.assertEqual(
            runtime.receipt["receipt_identity"],
            runtime_summary["receipt_identity"],
        )
        self.assertEqual(
            runtime.receipt["request_artifact"]["artifact_identity"],
            runtime_summary["artifact_identity"],
        )
        self.assertFalse(
            runtime_summary["secret_filter"]["response_content_persisted"]
        )
        salvage_text = json.dumps(salvage, ensure_ascii=False)
        self.assertNotIn("sk-must-never-enter-salvage", salvage_text)
        self.assertNotIn(substantive_document(), salvage_text)
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "one_shot_discussion_claim_exists_do_not_rerun",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(runtime),
            )
        self.assertEqual(1, runtime.execute_calls)

    def test_inadequate_answer_is_no_go_and_never_replayed(self) -> None:
        runtime = FakeRuntimeService(text="简短回答。")
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "real_discussion_no_go_do_not_rerun",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(runtime),
            )
        report = json.loads(
            (self.output / acceptance.RESULT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual("NO_GO_DO_NOT_RERUN", report["status"])
        self.assertFalse(report["checks"]["document_substantive"])
        self.assertEqual(1, runtime.execute_calls)

    def test_potential_secret_in_answer_is_not_copied_to_result(self) -> None:
        marker = "sk-abcdefghijklmnop"
        runtime = FakeRuntimeService(text=substantive_document() + "\n" + marker)
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "real_discussion_no_go_do_not_rerun",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(runtime),
            )
        result_text = (self.output / acceptance.RESULT_NAME).read_text(
            encoding="utf-8"
        )
        self.assertNotIn(marker, result_text)
        report = json.loads(result_text)
        self.assertTrue(report["runtime"]["response_text_redacted"])
        self.assertIsNone(report["runtime"]["response_text"])

    def test_go_requires_stop_and_nonzero_usage_within_fixed_budget(self) -> None:
        cases = (
            (
                "finish_reason",
                lambda runtime: runtime.receipt["response"].__setitem__(
                    "finish_reason", "length"
                ),
                "finish_reason_complete",
            ),
            (
                "zero_completion",
                lambda runtime: runtime.receipt.__setitem__(
                    "usage",
                    {
                        "prompt_tokens": 900,
                        "completion_tokens": 0,
                        "total_tokens": 900,
                    },
                ),
                "usage_within_token_budget",
            ),
            (
                "output_over_budget",
                lambda runtime: runtime.receipt.__setitem__(
                    "usage",
                    {
                        "prompt_tokens": 900,
                        "completion_tokens": (
                            acceptance.MAXIMUM_OUTPUT_TOKENS + 1
                        ),
                        "total_tokens": (
                            901 + acceptance.MAXIMUM_OUTPUT_TOKENS
                        ),
                    },
                ),
                "usage_within_token_budget",
            ),
            (
                "output_bytes_over_budget",
                lambda runtime: runtime.receipt["response"].__setitem__(
                    "content",
                    substantive_document()
                    + ("中" * acceptance.MAXIMUM_OUTPUT_BYTES),
                ),
                "response_within_output_byte_budget",
            ),
        )
        for name, mutate, check in cases:
            with self.subTest(name=name):
                runtime = FakeRuntimeService()
                mutate(runtime)
                output = self.root / ("bad-terminal-" + name)
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "real_discussion_no_go_do_not_rerun",
                ):
                    acceptance._run_with_dependencies(
                        output,
                        self.dependencies(runtime),
                    )
                report = json.loads(
                    (output / acceptance.RESULT_NAME).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertFalse(report["checks"][check])
                if name == "output_bytes_over_budget":
                    self.assertTrue(report["runtime"]["response_text_redacted"])
                    self.assertIsNone(report["runtime"]["response_text"])
                self.assertEqual(1, runtime.execute_calls)

    def test_search_failure_stops_before_context_preview_sentry_and_model(self) -> None:
        runtime = FakeRuntimeService()
        bundle_calls = 0

        def bundle_factory():
            nonlocal bundle_calls
            bundle_calls += 1
            return self.bundle()

        bad_search = self.search_proof()
        bad_search["model_calls"] = 1
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "search_zero_model_proof_failed",
        ):
            acceptance._run_with_dependencies(
                self.output,
                self.dependencies(
                    runtime,
                    bundle_factory=bundle_factory,
                    search_probe=lambda: bad_search,
                ),
            )
        self.assertEqual(0, bundle_calls)
        self.assertEqual(0, runtime.preview_calls)
        self.assertEqual(0, runtime.execute_calls)
        self.assert_claimed_failure(self.output, sentry_expected=False)

    def test_preview_must_bind_ids_and_prove_zero_execution(self) -> None:
        def set_top_level(key, value):
            return lambda preview: preview.__setitem__(key, value)

        def set_execution(key, value):
            return lambda preview: preview["execution"].__setitem__(key, value)

        def set_answer_context(key, value):
            return lambda preview: preview["request_artifact"][
                "answer_context_binding"
            ].__setitem__(key, value)

        def remove_diagnostic(preview):
            preview.pop("diagnostic_bindings")

        def pollute_reviewed_binding(preview):
            preview["reviewed_execution_binding"]["model_run_id"] = (
                "run_" + "f" * 64
            )

        cases = (
            ("request_id", set_top_level("request_id", "request-wrong-id")),
            ("mutation_id", set_top_level("mutation_id", "mutation-wrong-id")),
            ("persisted", set_execution("persisted", True)),
            ("sent", set_execution("sent_to_model", True)),
            ("external", set_execution("external_model_calls", True)),
            ("network", set_execution("network_request_count", 1)),
            ("retry", set_execution("automatic_retry", True)),
            ("mock", set_execution("mock_transport", True)),
            (
                "wire_bytes",
                set_answer_context(
                    "wire_input_utf8_bytes_conservative_token_upper_bound",
                    acceptance.MAXIMUM_INPUT_TOKENS + 1,
                ),
            ),
            (
                "legacy_visible",
                set_answer_context(
                    "delivery_base_model_context_model_visible", True
                ),
            ),
            ("diagnostic", remove_diagnostic),
            ("reviewed_pollution", pollute_reviewed_binding),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                runtime = FakeRuntimeService()
                original = runtime.preview_answer

                def bad_preview(
                    request,
                    *,
                    request_id,
                    mutation_id,
                    original=original,
                    mutate=mutate,
                ):
                    preview = original(
                        request,
                        request_id=request_id,
                        mutation_id=mutation_id,
                    )
                    mutate(preview)
                    return preview

                runtime.preview_answer = bad_preview
                output = self.root / ("bad-preview-" + name)
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "typed_answer_preview_invalid",
                ):
                    acceptance._run_with_dependencies(
                        output,
                        self.dependencies(runtime),
                    )
                self.assertEqual(0, runtime.execute_calls)
                self.assert_claimed_failure(output, sentry_expected=False)

    def test_incomplete_context_or_hidden_provider_fallback_fails_before_sentry(self) -> None:
        runtime = FakeRuntimeService()
        budget_binding = self.binding()
        budget_binding["maximum_cost_microunits"] += 1
        normal = self.bundle()
        budget_bundle = acceptance.DiscussionBundle(
            typed_request=FakeTypedAnswerRequest(budget_binding),
            request_binding=budget_binding,
            objective=normal.objective,
            context=normal.context,
            packages=normal.packages,
            provider=normal.provider,
            readiness=normal.readiness,
        )
        for name, bundle in (
            ("fallback", self.bundle(fallback_allowed=True)),
            ("budget", budget_bundle),
            (
                "missing_resume",
                self.bundle(),
            ),
        ):
            if name == "missing_resume":
                context = deepcopy(dict(bundle.context))
                context["bindings"] = context["bindings"][:-1]
                bundle = acceptance.DiscussionBundle(
                    typed_request=bundle.typed_request,
                    request_binding=bundle.request_binding,
                    objective=bundle.objective,
                    context=context,
                    packages=bundle.packages,
                    provider=bundle.provider,
                    readiness=bundle.readiness,
                )
            with self.subTest(name=name):
                output = self.root / name
                with self.assertRaises(acceptance.AcceptanceError):
                    acceptance._run_with_dependencies(
                        output,
                        self.dependencies(
                            runtime,
                            bundle_factory=lambda value=bundle: value,
                        ),
                    )
                self.assert_claimed_failure(output, sentry_expected=False)
        self.assertEqual(0, runtime.preview_calls)
        self.assertEqual(0, runtime.execute_calls)

    def test_cli_execute_error_is_sanitized_and_does_not_retry(self) -> None:
        stderr = io.StringIO()
        with patch.object(
            acceptance,
            "run",
            side_effect=acceptance.AcceptanceError(
                "one_shot_discussion_claim_exists_do_not_rerun"
            ),
        ) as run, redirect_stderr(stderr):
            status = acceptance.main(
                [
                    "--execute",
                    "--confirm-one-real-discussion",
                    "--confirm-do-not-rerun",
                    "--confirm-user-authorization-sha256",
                    acceptance.USER_AUTHORIZATION_SHA256,
                    "--confirm-combined-boundary-sha256",
                    acceptance.COMBINED_BOUNDARY_SHA256,
                ]
            )
        self.assertEqual(2, status)
        run.assert_called_once_with()
        payload = json.loads(stderr.getvalue())
        self.assertEqual(
            "one_shot_discussion_claim_exists_do_not_rerun",
            payload["reason_code"],
        )
        self.assertTrue(payload["do_not_rerun_if_claim_exists"])


if __name__ == "__main__":
    unittest.main()
