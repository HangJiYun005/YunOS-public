from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.cognitive_authorization import (
    CognitiveAuthorizationArtifact,
    CognitiveAuthorizationError,
    issue_authorization_artifact,
)
from memory.cognitive_capability import (
    CapabilityProfile,
    CapabilityRegistry,
    suggested_capability_profiles,
)
from memory.cognitive_command_policy import (
    CognitiveCommandPolicyError,
    CommandTemplateRegistry,
    ResolvedCommand,
)
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
)
import memory.cognitive_execution_models as execution_models
from memory.cognitive_execution_models import (
    CognitiveExecutionModelError,
    ToolExecutionPreview,
    ToolExecutionResult,
    ToolProposal,
    create_tool_proposal,
)
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
)
from memory.cognitive_objective import (
    AuthorityRequest,
    ObjectiveSourceAnchor,
    build_objective_contract,
)
from memory.cognitive_path_policy import (
    CognitivePathPolicyError,
    ResolvedProjectPath,
    path_is_within_roots,
    resolve_project_path,
    revalidate_resolved_path,
)
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanNode,
    build_plan_graph,
)
from memory.operational_state_delegation import DelegationContract
from memory.operational_state_models import AuthorityArtifact


T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-01T00:01:00Z"
T2 = "2026-08-01T00:02:00Z"
T3 = "2026-08-01T00:03:00Z"
T4 = "2026-08-01T00:04:00Z"
T5 = "2026-08-01T00:05:00Z"
T6 = "2026-08-01T00:06:00Z"


class V10ExecutionSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name).resolve(strict=True)
        self.root_text = str(self.root)
        (self.root / "nested").mkdir()
        (self.root / "linked").mkdir()
        (self.root / "README.txt").write_text("bounded\n", encoding="utf-8")
        (self.root / "nested" / "file.txt").write_text(
            "safe content\n", encoding="utf-8"
        )
        (self.root / "linked" / "file.txt").write_text(
            "linked content\n", encoding="utf-8"
        )

        self.profile = next(
            item
            for item in suggested_capability_profiles(
                {"project.file.read": "available"}
            )
            if item.capability_id == "project.file.read"
        )
        self.registry = CapabilityRegistry((self.profile,))
        self.requirement = CapabilityRequirement(
            capability_id=self.profile.capability_id,
            capability_version=self.profile.version,
            capability_digest=self.profile.capability_digest,
        )
        self.objective = build_objective_contract(
            user_utterance="Read one file within a bounded disposable project.",
            interpreted_goal="Verify the v10-D controlled execution boundary.",
            project_id="project:v10-d-security-test",
            source_anchors=(
                ObjectiveSourceAnchor(
                    anchor_id="anchor:v10-d-security-test",
                    source_kind="user_declaration",
                    locator="test:v10-d-execution-security",
                    evidence_digest="a" * 64,
                    observed_at=T0,
                    privacy_scope="private_metadata",
                ),
            ),
            success_criteria=("Every unsafe input fails closed.",),
            agency_level="act",
            agency_selected_by="user",
            maximum_authorized_agency_level="act",
            risk_tolerance="low",
            authority_request=AuthorityRequest(
                requested=True,
                read_roots=(self.root_text,),
                write_roots=(),
                capabilities=(self.profile.capability_id,),
                commands=("git.status.porcelain-v2.v1",),
                risk_ceiling="low",
            ),
        )
        self.node = PlanNode(
            node_id="node:v10-d-read",
            objective_id=self.objective.objective_id,
            title="Read one bounded test file",
            purpose="Read only the file explicitly named by the typed proposal.",
            capability_requirements=(self.requirement,),
            read_scope=(self.root_text,),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The bounded content is returned.",),
            verification=("Verify the file SHA-256.",),
            timeout_ms=1_000,
        )
        self.plan = build_plan_graph(
            objective=self.objective,
            nodes=(self.node,),
            change_reason="Create the isolated execution-security plan.",
            created_at=T0,
        )
        self.source_authority = AuthorityArtifact(
            authority_id="authority:v10-d-security-test",
            issued_by="user:local-user",
            issued_at=T0,
            expires_at=T5,
            target_project_id=self.objective.project_id,
            allowed_event_types=("delegation_authorized",),
            read_scope=(self.root_text,),
            write_scope=(self.root_text,),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "create-next-node",
            ),
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )
        self.delegation = DelegationContract(
            delegation_id="delegation:v10-d-security-test",
            project_id=self.objective.project_id,
            work_item_id="work-item:v10-d-security-test",
            intent="Exercise only the disposable v10-D security fixture.",
            read_scope=(self.root_text,),
            write_scope=(self.root_text,),
            prohibited_actions=(
                "merge",
                "tag",
                "push",
                "declare_project_complete",
                "declare_project_sealed",
                "resolve_conflict",
                "start_next_version",
                "create_long_term_plan",
                "update_user_profile",
            ),
            tool_scope=("tool:bounded-native-read",),
            risk="low",
            valid_from=T0,
            expires_at=T5,
            acceptance_criteria=(
                "Unsafe inputs are denied.",
                "No production data is accessed.",
            ),
            rollback="Discard the temporary directory.",
            version_scope=("v10-D",),
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
            authority_digest=self.source_authority.authority_digest,
        )
        self.authorization = self.issue_authorization()
        self.proposal = self.create_valid_proposal()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue_authorization(self, **overrides) -> CognitiveAuthorizationArtifact:
        values = {
            "objective": self.objective,
            "plan": self.plan,
            "delegation": self.delegation,
            "source_authority": self.source_authority,
            "read_roots": (self.root_text,),
            "write_roots": (),
            "allowed_capabilities": (self.requirement,),
            "denied_capabilities": (),
            "allowed_commands": ("git.status.porcelain-v2.v1",),
            "risk_ceiling": "low",
            "max_steps": 8,
            "max_duration_ms": 60_000,
            "max_model_calls": 0,
            "max_tokens": 0,
            "max_cost_microunits": 0,
            "commit_permission": False,
            "database_permission": "none",
            "valid_from": T1,
            "expires_at": T4,
            "rollback_policy": (),
            "nonce_material": "v10-d-security-test-nonce",
        }
        values.update(overrides)
        return issue_authorization_artifact(**values)

    def test_database_capability_requires_explicit_readonly_permission(self) -> None:
        database_profile = next(
            item
            for item in bounded_execution_capability_profiles(
                {"database.inspect.readonly": "available"}
            )
            if item.capability_id == "database.inspect.readonly"
        )
        database_requirement = CapabilityRequirement(
            capability_id=database_profile.capability_id,
            capability_version=database_profile.version,
            capability_digest=database_profile.capability_digest,
        )
        capabilities = (self.requirement, database_requirement)

        with self.assertRaises(CognitiveAuthorizationError) as absent:
            self.issue_authorization(
                allowed_capabilities=capabilities,
                database_permission="none",
            )
        self.assert_reason(
            absent,
            "authorization_database_read_permission_required",
        )

        allowed = self.issue_authorization(
            allowed_capabilities=capabilities,
            database_permission="readonly",
            nonce_material="v10-d-database-readonly-permission",
        )
        self.assertEqual("readonly", allowed.database_permission)
        self.assertTrue(
            allowed.allows_capability(
                database_profile.capability_id,
                database_profile.version,
                database_profile.capability_digest,
            )
        )

        with self.assertRaises(CognitiveAuthorizationError) as write:
            self.issue_authorization(
                allowed_capabilities=capabilities,
                database_permission="write",
                nonce_material="v10-d-database-write-denied",
            )
        self.assert_reason(write, "authorization_database_write_denied")

    def model_proposal(self, **overrides) -> dict:
        value = {
            "capability_id": self.profile.capability_id,
            "capability_version": self.profile.version,
            "capability_digest": self.profile.capability_digest,
            "typed_arguments": {"relative_path": "README.txt"},
            "reason": "Read the explicitly authorized test file.",
            "expected_observation": "Return bounded content and its SHA-256.",
        }
        value.update(overrides)
        return value

    def create_valid_proposal(self) -> ToolProposal:
        return create_tool_proposal(
            run_id="run:v10-d-security-test",
            plan=self.plan,
            authorization=self.authorization,
            registry=self.registry,
            plan_node_id=self.node.node_id,
            model_proposal=self.model_proposal(),
            proposed_at=T2,
        )

    @staticmethod
    def assert_reason(context, expected: str) -> None:
        if getattr(context.exception, "reason_code", str(context.exception)) != expected:
            raise AssertionError(
                f"expected reason {expected!r}, got {context.exception!r}"
            )

    def test_authorization_is_factory_only_and_replace_fails_closed(self) -> None:
        payload = self.authorization.to_dict()
        payload.pop("contract_version")
        with self.assertRaises(CognitiveAuthorizationError) as direct:
            CognitiveAuthorizationArtifact(**payload)
        self.assert_reason(direct, "authorization_artifact_factory_required")
        with self.assertRaises(CognitiveAuthorizationError) as copied:
            replace(self.authorization)
        self.assert_reason(copied, "authorization_artifact_factory_required")

    def test_authorization_time_boundaries_and_source_bindings(self) -> None:
        self.authorization.assert_current(T1)
        self.authorization.assert_current(T3)
        for timestamp in (T0, T4, T5):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(CognitiveAuthorizationError) as denied:
                    self.authorization.assert_current(timestamp)
                self.assert_reason(denied, "authorization_expired_or_not_yet_valid")

        wrong_delegation = replace(
            self.delegation,
            authority_digest="0" * 64,
        )
        with self.assertRaises(CognitiveAuthorizationError) as binding:
            self.issue_authorization(delegation=wrong_delegation)
        self.assert_reason(binding, "authorization_source_authority_mismatch")

        with self.assertRaises(CognitiveAuthorizationError) as interval:
            self.issue_authorization(expires_at=T6)
        self.assert_reason(interval, "authorization_exceeds_source_interval")

    def test_proposal_rechecks_expiry_and_exact_plan_binding(self) -> None:
        with self.assertRaises(CognitiveAuthorizationError) as expired:
            create_tool_proposal(
                run_id="run:v10-d-expired",
                plan=self.plan,
                authorization=self.authorization,
                registry=self.registry,
                plan_node_id=self.node.node_id,
                model_proposal=self.model_proposal(),
                proposed_at=T4,
            )
        self.assert_reason(expired, "authorization_expired_or_not_yet_valid")

        changed_node = replace(
            self.node,
            title="Read one differently bound test file",
        )
        changed_plan = build_plan_graph(
            objective=self.objective,
            nodes=(changed_node,),
            change_reason="Create an independently digested candidate plan.",
            created_at=T1,
        )
        with self.assertRaises(CognitiveExecutionModelError) as mismatch:
            create_tool_proposal(
                run_id="run:v10-d-plan-mismatch",
                plan=changed_plan,
                authorization=self.authorization,
                registry=self.registry,
                plan_node_id=changed_node.node_id,
                model_proposal=self.model_proposal(),
                proposed_at=T2,
            )
        self.assert_reason(mismatch, "tool_authorization_plan_binding_mismatch")

    def test_windows_path_malicious_matrix_fails_closed(self) -> None:
        cases = {
            "": "tool_path_syntax_denied",
            ".": "tool_path_syntax_denied",
            "..": "tool_path_syntax_denied",
            "a//b": "tool_path_syntax_denied",
            "a/../b": "tool_path_syntax_denied",
            "../escape.txt": "tool_path_syntax_denied",
            "/absolute.txt": "tool_path_namespace_denied",
            "\\rooted.txt": "tool_path_namespace_denied",
            r"C:\outside.txt": "tool_path_namespace_denied",
            "C:outside.txt": "tool_path_namespace_denied",
            r"\\server\share\outside.txt": "tool_path_namespace_denied",
            r"\\?\C:\outside.txt": "tool_path_namespace_denied",
            r"\\.\NUL": "tool_path_namespace_denied",
            r"\??\C:\outside.txt": "tool_path_namespace_denied",
            "file.txt:stream": "tool_path_namespace_denied",
            "name.": "tool_path_windows_alias_denied",
            "name ": "tool_path_windows_alias_denied",
            "dir./file.txt": "tool_path_windows_alias_denied",
            "dir /file.txt": "tool_path_windows_alias_denied",
            ".. /escape.txt": "tool_path_windows_alias_denied",
            ".git/config": "tool_git_control_path_denied",
            "sub/.GIT/config": "tool_git_control_path_denied",
            "bad|name.txt": "tool_path_syntax_denied",
            "e\u0301.txt": "tool_path_unicode_normalization_denied",
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(CognitivePathPolicyError) as denied:
                    resolve_project_path(
                        project_root=self.root,
                        relative_path=value,
                        access_mode="read_optional",
                    )
                self.assert_reason(denied, reason)

    def test_windows_reserved_device_names_are_denied(self) -> None:
        for value in (
            "CON",
            "con.txt",
            "PRN.log",
            "AUX",
            "NUL.txt",
            "CLOCK$",
            "COM1",
            "COM9.txt",
            "LPT1",
            "LPT9.log",
            "COM\u00b9.txt",
            "COM\u00b2",
            "LPT\u00b3.txt",
        ):
            with self.subTest(value=value):
                with self.assertRaises(CognitivePathPolicyError) as denied:
                    resolve_project_path(
                        project_root=self.root,
                        relative_path=value,
                        access_mode="read_optional",
                    )
                self.assert_reason(denied, "tool_path_reserved_name_denied")

    def test_path_binding_canonicalization_factory_and_identity_recheck(self) -> None:
        binding = resolve_project_path(
            project_root=self.root,
            relative_path=r"nested\file.txt",
            access_mode="read_file",
        )
        self.assertEqual("nested/file.txt", binding.canonical_relative_path)
        revalidate_resolved_path(binding)

        payload = binding.to_dict()
        payload.pop("path_policy_version")
        with self.assertRaises(CognitivePathPolicyError) as direct:
            ResolvedProjectPath(**payload)
        self.assert_reason(direct, "resolved_project_path_factory_required")
        with self.assertRaises(CognitivePathPolicyError) as copied:
            replace(binding)
        self.assert_reason(copied, "resolved_project_path_factory_required")

        Path(binding.absolute_path).write_text("changed\n", encoding="utf-8")
        with self.assertRaises(CognitivePathPolicyError) as changed:
            revalidate_resolved_path(binding)
        self.assert_reason(changed, "tool_path_identity_changed")

    def test_root_prefix_and_mocked_reparse_components_are_denied(self) -> None:
        sibling = self.root.parent / f"{self.root.name}-escape" / "file.txt"
        self.assertFalse(path_is_within_roots(str(sibling), (self.root_text,)))
        self.assertTrue(
            path_is_within_roots(
                str(self.root / "nested" / "file.txt"),
                (self.root_text,),
            )
        )

        def linked_component(value) -> bool:
            return Path(value).name.casefold() == "linked"

        with patch(
            "memory.cognitive_path_policy.is_reparse_point",
            side_effect=linked_component,
        ):
            with self.assertRaises(CognitivePathPolicyError) as linked:
                resolve_project_path(
                    project_root=self.root,
                    relative_path="linked/file.txt",
                    access_mode="read_file",
                )
        self.assert_reason(linked, "tool_path_reparse_denied")

        with patch(
            "memory.cognitive_path_policy.is_reparse_point",
            side_effect=lambda value: Path(value) == self.root,
        ):
            with self.assertRaises(CognitivePathPolicyError) as root_link:
                resolve_project_path(
                    project_root=self.root,
                    relative_path="README.txt",
                    access_mode="read_file",
                )
        self.assert_reason(root_link, "tool_path_reparse_denied")

    def command_registry(self) -> CommandTemplateRegistry:
        return CommandTemplateRegistry(
            python_executable=sys.executable,
            git_executable=sys.executable,
        )

    @staticmethod
    def unittest_profile() -> CapabilityProfile:
        return CapabilityProfile(
            capability_id="test.run.allowlisted",
            display_name="Run one allowlisted unit test",
            version=1,
            input_schema={
                "type": "object",
                "properties": {
                    "test_ids": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 191,
                        },
                        "minItems": 1,
                        "maxItems": 16,
                        "uniqueItems": True,
                    }
                },
                "required": ["test_ids"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"passed": {"type": "boolean"}},
                "required": ["passed"],
                "additionalProperties": False,
            },
            risk_class="low",
            reversible=True,
            read_scopes=("project.tests",),
            write_scopes=(),
            required_authority="read",
            timeout_ms=30_000,
            max_output_bytes=64_000,
            idempotency="read_only",
            verification_method=("unittest_result",),
            availability="available",
            executor_kind="native",
        )

    def status_profile(self) -> CapabilityProfile:
        return next(
            item
            for item in suggested_capability_profiles(
                {"git.status.read": "available"}
            )
            if item.capability_id == "git.status.read"
        )

    def test_fixed_command_is_code_owned_factory_only_and_shell_free(self) -> None:
        registry = self.command_registry()
        profile = self.status_profile()
        command = registry.resolve(
            template_id="git.status.porcelain-v2.v1",
            profile=profile,
            typed_arguments={},
            path_bindings=(),
            project_root=self.root_text,
        )
        self.assertEqual(str(Path(sys.executable).resolve()), command.argv[0])
        self.assertEqual(
            (
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.pager=cat",
                "-c",
                "pager.status=false",
                "-c",
                "pager.log=false",
                "-c",
                "pager.show=false",
                "-c",
                "pager.diff=false",
                "-c",
                "credential.helper=",
                "-c",
                f"core.hooksPath={os.devnull}",
                "status",
                "--porcelain=v2",
                "--branch",
                "-z",
                "--untracked-files=all",
            ),
            command.argv[1:],
        )
        self.assertFalse(command.shell)
        self.assertEqual("disabled", command.stdin_mode)
        self.assertNotIn("PATH", command.environment)
        self.assertNotIn("HOME", command.environment)
        registry.revalidate(command)

        payload = command.to_dict()
        payload.pop("command_policy_version")
        with self.assertRaises(CognitiveCommandPolicyError) as direct:
            ResolvedCommand(**payload)
        self.assert_reason(direct, "resolved_command_factory_required")
        with self.assertRaises(CognitiveCommandPolicyError) as copied:
            replace(command)
        self.assert_reason(copied, "resolved_command_factory_required")

    def test_command_template_unknown_capability_mismatch_and_injection(self) -> None:
        registry = self.command_registry()
        status_profile = self.status_profile()
        with self.assertRaises(CognitiveCommandPolicyError) as unknown:
            registry.resolve(
                template_id="shell.arbitrary.v1",
                profile=status_profile,
                typed_arguments={},
                path_bindings=(),
                project_root=self.root_text,
            )
        self.assert_reason(unknown, "tool_command_template_unknown")

        with self.assertRaises(CognitiveCommandPolicyError) as mismatch:
            registry.resolve(
                template_id="git.status.porcelain-v2.v1",
                profile=self.profile,
                typed_arguments={"relative_path": "README.txt"},
                path_bindings=(),
                project_root=self.root_text,
            )
        self.assert_reason(mismatch, "tool_command_capability_mismatch")

        profile = self.unittest_profile()
        for value in (
            "tests.safe && whoami",
            "tests.safe || whoami",
            "tests.safe|whoami",
            "tests.safe>output",
            "tests.safe<input",
            "tests.safe$(whoami)",
            "tests.safe`whoami`",
            "%COMSPEC%",
            "tests.safe\nwhoami",
            "-q",
        ):
            with self.subTest(value=value):
                with self.assertRaises(CognitiveCommandPolicyError) as injected:
                    registry.resolve(
                        template_id="python.unittest.v1",
                        profile=profile,
                        typed_arguments={"test_ids": [value]},
                        path_bindings=(),
                        project_root=self.root_text,
                    )
                self.assert_reason(injected, "tool_command_sandbox_unavailable")

    def test_python_test_and_compile_templates_are_unconditionally_unavailable(self) -> None:
        profiles = {
            item.capability_id: item
            for item in bounded_execution_capability_profiles(
                {
                    "test.run.allowlisted": "available",
                    "process.template.run": "available",
                }
            )
        }
        self.assertEqual("disabled", profiles["test.run.allowlisted"].availability)
        self.assertEqual(
            ("git.diff.check",),
            profiles["process.template.run"].input_schema["properties"]
            ["template_id"]["enum"],
        )
        self.assertEqual((), profiles["process.template.run"].write_scopes)
        self.assertEqual("read_only", profiles["process.template.run"].idempotency)

        registry = self.command_registry()
        for template_id, profile, arguments in (
            (
                "python.unittest.v1",
                self.unittest_profile(),
                {"test_ids": ["tests.safe"]},
            ),
            (
                "python.compileall.v1",
                profiles["process.template.run"],
                {"template_id": "git.diff.check", "relative_paths": []},
            ),
        ):
            with self.subTest(template_id=template_id):
                self.assertNotIn(template_id, registry.template_ids)
                with self.assertRaises(CognitiveCommandPolicyError) as denied:
                    registry.resolve(
                        template_id=template_id,
                        profile=profile,
                        typed_arguments=arguments,
                        path_bindings=(),
                        project_root=self.root_text,
                    )
                self.assert_reason(denied, "tool_command_sandbox_unavailable")

    def test_local_git_fsmonitor_sentinel_is_never_executed(self) -> None:
        git_executable = shutil.which("git")
        self.assertIsNotNone(git_executable)
        subprocess.run(
            [git_executable, "init", "-q"],
            cwd=self.root_text,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        marker = self.root / "fsmonitor-executed.txt"
        sentinel = self.root / "fsmonitor-sentinel.py"
        sentinel.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
            "print('builtin:fake')\n",
            encoding="utf-8",
        )
        hook_command = (
            f'"{Path(sys.executable).resolve().as_posix()}" '
            f'"{sentinel.resolve().as_posix()}"'
        )
        subprocess.run(
            [git_executable, "config", "--local", "core.fsmonitor", hook_command],
            cwd=self.root_text,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        registry = CommandTemplateRegistry(git_executable=git_executable)
        command = registry.resolve(
            template_id="git.status.porcelain-v2.v1",
            profile=self.status_profile(),
            typed_arguments={},
            path_bindings=(),
            project_root=self.root_text,
        )
        completed = subprocess.run(
            list(command.argv),
            cwd=command.working_directory,
            env=dict(command.environment),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        self.assertFalse(marker.exists())

    def test_command_digest_and_executable_drift_fail_closed(self) -> None:
        registry = self.command_registry()
        command = registry.resolve(
            template_id="git.status.porcelain-v2.v1",
            profile=self.status_profile(),
            typed_arguments={},
            path_bindings=(),
            project_root=self.root_text,
        )
        object.__setattr__(command, "template_digest", "0" * 64)
        with self.assertRaises(CognitiveCommandPolicyError) as digest:
            registry.revalidate(command)
        self.assert_reason(digest, "tool_command_template_drift")

        command = registry.resolve(
            template_id="git.status.porcelain-v2.v1",
            profile=self.status_profile(),
            typed_arguments={},
            path_bindings=(),
            project_root=self.root_text,
        )
        changed_identity = dict(command.executable_identity)
        changed_identity["sha256"] = "0" * 64
        with patch(
            "memory.cognitive_command_policy._executable_identity",
            return_value=changed_identity,
        ):
            with self.assertRaises(CognitiveCommandPolicyError) as executable:
                registry.revalidate(command)
        self.assert_reason(executable, "tool_command_executable_untrusted")

    def test_model_proposal_is_closed_and_factory_only(self) -> None:
        for field, value in (
            ("argv", ["cmd.exe"]),
            ("command", "whoami"),
            ("cwd", self.root_text),
            ("env", {"TOKEN": "value"}),
            ("stdin", "interactive"),
        ):
            with self.subTest(field=field):
                model = self.model_proposal()
                model[field] = value
                with self.assertRaises(CognitiveExecutionModelError) as denied:
                    create_tool_proposal(
                        run_id=f"run:v10-d-extra-{field}",
                        plan=self.plan,
                        authorization=self.authorization,
                        registry=self.registry,
                        plan_node_id=self.node.node_id,
                        model_proposal=model,
                        proposed_at=T2,
                    )
                self.assert_reason(denied, "tool_proposal_shape_invalid")

        payload = self.proposal.to_dict()
        payload.pop("protocol_version")
        with self.assertRaises(CognitiveExecutionModelError) as direct:
            ToolProposal(**payload)
        self.assert_reason(direct, "tool_proposal_factory_required")
        with self.assertRaises(CognitiveExecutionModelError) as copied:
            replace(self.proposal)
        self.assert_reason(copied, "tool_proposal_factory_required")

    def preview_values(self, **overrides) -> dict:
        binding = resolve_project_path(
            project_root=self.root,
            relative_path="README.txt",
            access_mode="read_file",
        )
        values = {
            "preview_id": "preview:v10-d-security-test",
            "proposal_digest": self.proposal.proposal_digest,
            "authorization_digest": self.authorization.authority_digest,
            "plan_digest": self.plan.plan_digest,
            "node_digest": self.node.node_digest,
            "capability_digest": self.profile.capability_digest,
            "attempt_number": 1,
            "invocation_kind": "native_call",
            "resolved_argv": (),
            "working_directory": self.root_text,
            "resolved_bindings": (binding.to_dict(),),
            "template_id": None,
            "template_digest": None,
            "effective_timeout_ms": 1_000,
            "effective_output_bytes": 8_192,
            "prepared_at": T2,
            "expires_at": T4,
        }
        values.update(overrides)
        return values

    def test_preview_native_fixed_process_union_and_factory_seal(self) -> None:
        native = execution_models._create_tool_execution_preview(
            **self.preview_values()
        )
        fixed = execution_models._create_tool_execution_preview(
            **self.preview_values(
                preview_id="preview:v10-d-fixed-test",
                invocation_kind="fixed_process",
                resolved_argv=(str(Path(sys.executable).resolve()), "-V"),
                template_id="python.unittest.v1",
                template_digest="b" * 64,
            )
        )
        self.assertEqual((), native.resolved_argv)
        self.assertTrue(fixed.resolved_argv)

        with self.assertRaises(CognitiveExecutionModelError) as native_argv:
            execution_models._create_tool_execution_preview(
                **self.preview_values(resolved_argv=("forbidden",))
            )
        self.assert_reason(native_argv, "native_tool_preview_argv_must_be_empty")
        with self.assertRaises(CognitiveExecutionModelError) as process_argv:
            execution_models._create_tool_execution_preview(
                **self.preview_values(
                    preview_id="preview:v10-d-empty-process",
                    invocation_kind="fixed_process",
                    resolved_argv=(),
                    template_id="python.unittest.v1",
                    template_digest="c" * 64,
                )
            )
        self.assert_reason(process_argv, "process_tool_preview_argv_required")

        payload = native.to_dict()
        payload.pop("protocol_version")
        with self.assertRaises(CognitiveExecutionModelError) as direct:
            ToolExecutionPreview(**payload)
        self.assert_reason(direct, "tool_execution_preview_factory_required")
        with self.assertRaises(CognitiveExecutionModelError) as copied:
            replace(native)
        self.assert_reason(copied, "tool_execution_preview_factory_required")

    def execution_values(self, **overrides) -> dict:
        values = {
            "execution_id": "execution:v10-d-security-test",
            "proposal_id": self.proposal.proposal_id,
            "proposal_digest": self.proposal.proposal_digest,
            "run_id": self.proposal.run_id,
            "idempotency_key": self.proposal.idempotency_key,
            "reservation_event_id": "event:v10-d-execution-reservation",
            "reservation_receipt_digest": "d" * 64,
            "preview_id": "preview:v10-d-security-test",
            "preview_digest": "e" * 64,
            "attempt_number": 1,
            "invocation_kind": "native_call",
            "executor_kind": "native",
            "resolved_argv": (),
            "working_directory": self.root_text,
            "policy_decision": "allow",
            "reason_codes": ("policy_allow",),
            "started_at": T2,
            "finished_at": T3,
            "status": "succeeded",
            "exit_code": None,
            "output": {"verified": True},
            "stdout_digest": None,
            "stderr_digest": None,
            "artifact_references": (),
            "runtime_execution_reference": None,
            "runtime_receipt_digest": None,
            "verification_status": "passed",
        }
        values.update(overrides)
        return values

    def test_execution_result_native_fixed_process_union_and_factory_seal(self) -> None:
        native = execution_models._create_tool_execution_result(
            **self.execution_values()
        )
        fixed = execution_models._create_tool_execution_result(
            **self.execution_values(
                execution_id="execution:v10-d-fixed-test",
                invocation_kind="fixed_process",
                resolved_argv=(str(Path(sys.executable).resolve()), "-V"),
                exit_code=0,
            )
        )
        self.assertEqual((), native.resolved_argv)
        self.assertTrue(fixed.resolved_argv)

        with self.assertRaises(CognitiveExecutionModelError) as native_argv:
            execution_models._create_tool_execution_result(
                **self.execution_values(resolved_argv=("forbidden",))
            )
        self.assert_reason(native_argv, "native_tool_execution_argv_must_be_empty")
        with self.assertRaises(CognitiveExecutionModelError) as process_argv:
            execution_models._create_tool_execution_result(
                **self.execution_values(
                    execution_id="execution:v10-d-empty-process",
                    invocation_kind="fixed_process",
                    resolved_argv=(),
                )
            )
        self.assert_reason(process_argv, "process_tool_execution_argv_required")

        payload = native.to_dict()
        payload.pop("protocol_version")
        with self.assertRaises(CognitiveExecutionModelError) as direct:
            ToolExecutionResult(**payload)
        self.assert_reason(direct, "tool_execution_result_factory_required")
        with self.assertRaises(CognitiveExecutionModelError) as copied:
            replace(native)
        self.assert_reason(copied, "tool_execution_result_factory_required")


if __name__ == "__main__":
    unittest.main()
