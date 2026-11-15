from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from memory.cognitive_authorization import CognitiveAuthorizationArtifact
from memory.cognitive_capability import (
    CapabilityProfile,
    CapabilityRegistry,
    CognitiveCapabilityError,
)
from memory.cognitive_command_policy import (
    CommandTemplateRegistry,
    ResolvedCommand,
    parse_git_status_porcelain_v2,
)
from memory.cognitive_execution_models import (
    GatewayExecutionOutcome,
    ToolExecutionPreview,
    ToolExecutionResult,
    ToolProposal,
    _create_tool_execution_preview,
    _create_tool_execution_result,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_local_broker import (
    CognitiveLocalBroker,
    CognitiveLocalBrokerError,
)
from memory.cognitive_orchestration import contains_secret_material
from memory.cognitive_path_policy import (
    CognitivePathPolicyError,
    ResolvedProjectPath,
    path_is_within_roots,
    resolve_project_path,
    revalidate_resolved_path,
)
from memory.cognitive_planning import PlanGraph, PlanNode
from memory.cognitive_supervision import (
    CognitiveSupervisionError,
    ProcessRunResult,
    ProcessSupervisor,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    utc_now,
)


TOOL_GATEWAY_VERSION = 2
_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_FIXED_TEMPLATES = {
    "git.status.read": "git.status.porcelain-v2.v1",
    "git.log.read": "git.log.bounded.v1",
    "git.show.read": "git.show.bounded.v1",
    "git.diff.read": "git.diff.bounded.v1",
}
_NATIVE_CAPABILITIES = {
    "project.file.read",
    "project.tree.list",
    "project.text.search",
    "database.inspect.readonly",
    "project.file.patch",
    "project.file.create",
    "project.temp.dispose_owned",
    "project.change.rollback",
}
_READ_ONLY_PROCESS_CAPABILITIES = {
    "git.status.read",
    "git.log.read",
    "git.show.read",
    "git.diff.read",
}


class CognitiveToolGatewayError(OperationalStateError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CognitiveToolGatewayError(reason_code)


def _as_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        _fail("tool_gateway_clock_invalid")
    if parsed.tzinfo is None:
        _fail("tool_gateway_clock_invalid")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return normalize_timestamp(
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class _PreparedComponents:
    preview: ToolExecutionPreview
    profile: CapabilityProfile
    node: PlanNode
    path_bindings: tuple[ResolvedProjectPath, ...]
    command: ResolvedCommand | None


class CognitiveToolGateway:
    """Fail-closed gateway for typed native calls and code-owned argv templates."""

    def __init__(
        self,
        *,
        project_root: str,
        registry: CapabilityRegistry,
        execution_store: CognitiveExecutionStore,
        command_registry: CommandTemplateRegistry,
        local_broker: CognitiveLocalBroker,
        process_supervisor: ProcessSupervisor,
        trusted_clock: Callable[[], str] | None = None,
        hard_output_limit_bytes: int = 1_000_000,
    ) -> None:
        try:
            root = Path(project_root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            _fail("tool_project_root_unavailable")
        if not root.is_dir() or root != local_broker.project_root:
            _fail("tool_gateway_project_root_mismatch")
        if not isinstance(registry, CapabilityRegistry):
            _fail("invalid_tool_capability_registry")
        if not isinstance(execution_store, CognitiveExecutionStore):
            _fail("invalid_tool_execution_store")
        if not isinstance(command_registry, CommandTemplateRegistry):
            _fail("invalid_tool_command_registry")
        if not isinstance(process_supervisor, ProcessSupervisor):
            _fail("invalid_tool_process_supervisor")
        if (
            isinstance(hard_output_limit_bytes, bool)
            or not isinstance(hard_output_limit_bytes, int)
            or not 1_024 <= hard_output_limit_bytes <= 104_857_600
        ):
            _fail("invalid_tool_gateway_output_limit")
        self.project_root = root
        self.registry = registry
        self.execution_store = execution_store
        self.command_registry = command_registry
        self.local_broker = local_broker
        self.process_supervisor = process_supervisor
        self._trusted_clock = trusted_clock or utc_now
        self.hard_output_limit_bytes = hard_output_limit_bytes

    def _trusted_at(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _node(plan: PlanGraph, proposal: ToolProposal) -> PlanNode:
        if not isinstance(plan, PlanGraph):
            _fail("invalid_tool_plan")
        node = next(
            (item for item in plan.nodes if item.node_id == proposal.plan_node_id),
            None,
        )
        if node is None or node.node_kind in {"human_gate", "decision"}:
            _fail("tool_plan_node_not_executable")
        return node

    @staticmethod
    def _path_mode(
        capability_id: str,
        *,
        single: bool,
    ) -> str:
        if capability_id == "project.tree.list":
            return "read_directory"
        if capability_id == "project.file.patch":
            return "write_file"
        if capability_id == "project.file.create":
            return "create_file"
        return "read_file" if single else "read_optional"

    def _resolve_one(
        self,
        *,
        relative_path: str,
        access_mode: str,
    ) -> ResolvedProjectPath:
        try:
            binding = resolve_project_path(
                project_root=self.project_root,
                relative_path=relative_path,
                access_mode=access_mode,
            )
        except CognitivePathPolicyError as error:
            if (
                access_mode in {"read_file", "read_optional"}
                and error.reason_code == "tool_path_type_mismatch"
            ):
                binding = resolve_project_path(
                    project_root=self.project_root,
                    relative_path=relative_path,
                    access_mode="read_directory",
                )
            else:
                raise
        if binding.canonical_relative_path != relative_path:
            _fail("tool_path_noncanonical")
        return binding

    def _resolve_paths(
        self,
        profile: CapabilityProfile,
        arguments: Mapping[str, Any],
        *,
        proposal: ToolProposal,
    ) -> tuple[ResolvedProjectPath, ...]:
        capability_id = profile.capability_id
        if capability_id in {
            "project.file.read",
            "project.tree.list",
            "project.text.search",
            "database.inspect.readonly",
            "project.file.patch",
            "project.file.create",
        }:
            relative_path = arguments["relative_path"]
            return (
                self._resolve_one(
                    relative_path=relative_path,
                    access_mode=self._path_mode(capability_id, single=True),
                ),
            )
        if capability_id == "project.change.rollback":
            return (
                self.local_broker.resolve_rollback_binding(
                    proposal=proposal,
                    backup_reference=arguments["backup_reference"],
                    expected_current_sha256=arguments[
                        "expected_current_sha256"
                    ],
                ),
            )
        if capability_id == "project.temp.dispose_owned":
            return (
                self.local_broker.resolve_owned_disposal_binding(
                    proposal=proposal,
                    relative_path=arguments["relative_path"],
                    ownership_reference=arguments["ownership_reference"],
                    expected_before_sha256=arguments["expected_before_sha256"],
                ),
            )
        if capability_id in {
            "git.log.read",
            "git.show.read",
            "git.diff.read",
            "process.template.run",
        }:
            return tuple(
                self._resolve_one(
                    relative_path=value,
                    access_mode="read_optional",
                )
                for value in arguments["relative_paths"]
            )
        return ()

    @staticmethod
    def _template_for(
        profile: CapabilityProfile,
        arguments: Mapping[str, Any],
    ) -> str | None:
        if profile.capability_id == "process.template.run":
            return {
                "python.compileall": "python.compileall.v1",
                "git.diff.check": "git.diff.check.v1",
            }.get(arguments["template_id"])
        return _FIXED_TEMPLATES.get(profile.capability_id)

    @staticmethod
    def _check_scope(
        *,
        binding: ResolvedProjectPath,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
    ) -> None:
        writes = binding.access_mode in {"write_file", "create_file"}
        authorized_roots = (
            authorization.write_roots if writes else authorization.read_roots
        )
        plan_roots = node.write_scope if writes else node.read_scope
        if not path_is_within_roots(binding.absolute_path, authorized_roots):
            _fail(
                "tool_path_outside_authorized_write_root"
                if writes
                else "tool_path_outside_authorized_read_root"
            )
        if not path_is_within_roots(binding.absolute_path, plan_roots):
            _fail(
                "tool_path_outside_plan_write_scope"
                if writes
                else "tool_path_outside_plan_read_scope"
            )

    def _prepare_components(
        self,
        *,
        proposal: ToolProposal,
        plan: PlanGraph,
        authorization: CognitiveAuthorizationArtifact,
        attempt_number: int,
        prepared_at: str,
    ) -> _PreparedComponents:
        if not isinstance(proposal, ToolProposal):
            _fail("invalid_tool_proposal")
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            _fail("invalid_tool_authorization")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number not in {1, 2}
        ):
            _fail("invalid_tool_execution_attempt")
        timestamp = normalize_timestamp(prepared_at)
        authorization.assert_current(timestamp)
        run = self.execution_store.get_run(proposal.run_id)
        if self.execution_store.cancellation_requested(proposal.run_id):
            _fail("tool_cancellation_requested")
        if run.status != "executing":
            _fail("tool_run_not_executing")
        if (
            run.objective_id != proposal.objective_id
            or run.objective_digest != proposal.objective_digest
            or run.plan_id != proposal.plan_id
            or run.plan_version != proposal.plan_version
            or run.plan_digest != proposal.plan_digest
            or run.authorization_id != proposal.authorization_id
            or run.authorization_digest != proposal.authorization_digest
            or authorization.authorization_id != proposal.authorization_id
            or authorization.authority_digest != proposal.authorization_digest
            or authorization.plan_digest != plan.plan_digest
        ):
            _fail("tool_gateway_binding_mismatch")
        persisted_authorization = self.execution_store.get_authorization(
            authorization.authorization_id
        )
        if persisted_authorization.to_dict() != authorization.to_dict():
            _fail("tool_gateway_authorization_mismatch")
        persisted_proposal = self.execution_store.get_tool_proposal(
            proposal.proposal_id
        )
        if (
            persisted_proposal.identity_payload() != proposal.identity_payload()
            or persisted_proposal.proposal_digest != proposal.proposal_digest
        ):
            _fail("tool_gateway_proposal_mismatch")
        node = self._node(plan, proposal)
        profile = self.registry.lookup(
            proposal.capability_id,
            version=proposal.capability_version,
            expected_digest=proposal.capability_digest,
        )
        if profile.availability != "available":
            _fail(f"tool_capability_{profile.availability}")
        if not authorization.allows_capability(
            profile.capability_id,
            profile.version,
            profile.capability_digest,
        ):
            _fail("tool_authorization_capability_denied")
        if (
            profile.capability_id == "database.inspect.readonly"
            and authorization.database_permission != "readonly"
        ):
            _fail("tool_authorization_database_permission_denied")
        if _RISK_RANK[profile.risk_class] > _RISK_RANK[authorization.risk_ceiling]:
            _fail("tool_authorization_risk_ceiling_exceeded")
        if run.steps_used >= authorization.max_steps:
            _fail("tool_authorization_step_budget_exhausted")
        if (
            run.model_calls_used > authorization.max_model_calls
            or run.tokens_used > authorization.max_tokens
            or run.cost_used_microunits > authorization.max_cost_microunits
        ):
            _fail("tool_authorization_model_budget_exhausted")
        if run.started_at is None:
            _fail("tool_run_start_time_missing")
        elapsed_ms = int(
            (_as_datetime(timestamp) - _as_datetime(run.started_at)).total_seconds()
            * 1000
        )
        if elapsed_ms < 0:
            _fail("tool_gateway_clock_rollback_detected")
        remaining_duration = authorization.max_duration_ms - elapsed_ms
        if remaining_duration <= 0:
            _fail("tool_authorization_duration_budget_exhausted")
        arguments = profile.validate_input(dict(proposal.typed_arguments))
        if contains_secret_material(canonical_json(arguments)):
            _fail("tool_input_secret_detected")
        path_bindings = self._resolve_paths(
            profile,
            arguments,
            proposal=proposal,
        )
        for binding in path_bindings:
            self._check_scope(
                binding=binding,
                node=node,
                authorization=authorization,
            )
        template_id = self._template_for(profile, arguments)
        command: ResolvedCommand | None = None
        invocation_kind = "native_call"
        argv: tuple[str, ...] = ()
        template_digest: str | None = None
        if template_id is not None:
            if template_id not in authorization.allowed_commands:
                _fail("tool_command_not_authorized")
            command = self.command_registry.resolve(
                template_id=template_id,
                profile=profile,
                typed_arguments=arguments,
                path_bindings=path_bindings,
                project_root=str(self.project_root),
            )
            invocation_kind = "fixed_process"
            argv = command.argv
            template_digest = command.template_digest
        elif profile.capability_id not in _NATIVE_CAPABILITIES:
            _fail("tool_executor_unavailable")
        effective_timeout = min(
            profile.timeout_ms,
            node.timeout_ms,
            remaining_duration,
        )
        effective_output = min(
            profile.max_output_bytes,
            self.hard_output_limit_bytes,
        )
        expiry = min(
            _as_datetime(authorization.expires_at),
            _as_datetime(timestamp) + timedelta(milliseconds=effective_timeout),
        )
        if expiry <= _as_datetime(timestamp):
            _fail("tool_preview_expired")
        binding_records = tuple(item.to_dict() for item in path_bindings)
        preview_identity = {
            "gateway_version": TOOL_GATEWAY_VERSION,
            "proposal_digest": proposal.proposal_digest,
            "authorization_digest": authorization.authority_digest,
            "plan_digest": plan.plan_digest,
            "node_digest": node.node_digest,
            "capability_digest": profile.capability_digest,
            "attempt_number": attempt_number,
            "invocation_kind": invocation_kind,
            "resolved_argv": list(argv),
            "working_directory": str(self.project_root),
            "resolved_binding_digests": [
                item.binding_digest for item in path_bindings
            ],
            "template_id": template_id,
            "template_digest": template_digest,
            "effective_timeout_ms": effective_timeout,
            "effective_output_bytes": effective_output,
            "prepared_at": timestamp,
            "expires_at": _timestamp(expiry),
        }
        preview = _create_tool_execution_preview(
            preview_id=make_identity("toolpreview", preview_identity),
            proposal_digest=proposal.proposal_digest,
            authorization_digest=authorization.authority_digest,
            plan_digest=plan.plan_digest,
            node_digest=node.node_digest,
            capability_digest=profile.capability_digest,
            attempt_number=attempt_number,
            invocation_kind=invocation_kind,
            resolved_argv=argv,
            working_directory=str(self.project_root),
            resolved_bindings=binding_records,
            template_id=template_id,
            template_digest=template_digest,
            effective_timeout_ms=effective_timeout,
            effective_output_bytes=effective_output,
            prepared_at=timestamp,
            expires_at=_timestamp(expiry),
        )
        return _PreparedComponents(
            preview=preview,
            profile=profile,
            node=node,
            path_bindings=path_bindings,
            command=command,
        )

    def preview(
        self,
        *,
        proposal: ToolProposal,
        plan: PlanGraph,
        authorization: CognitiveAuthorizationArtifact,
        attempt_number: int = 1,
    ) -> ToolExecutionPreview:
        return self._prepare_components(
            proposal=proposal,
            plan=plan,
            authorization=authorization,
            attempt_number=attempt_number,
            prepared_at=self._trusted_at(),
        ).preview

    def _execute_native(
        self,
        *,
        profile: CapabilityProfile,
        proposal: ToolProposal,
        execution_key: str,
        bindings: Sequence[ResolvedProjectPath],
    ) -> Mapping[str, Any]:
        arguments = dict(proposal.typed_arguments)
        if profile.capability_id == "project.file.read":
            return self.local_broker.read_file(bindings[0])
        if profile.capability_id == "project.tree.list":
            return self.local_broker.list_directory(
                bindings[0],
                maximum_entries=arguments["maximum_entries"],
            )
        if profile.capability_id == "project.text.search":
            return self.local_broker.search_text(
                bindings[0],
                search_text=arguments["search_text"],
                maximum_results=arguments["maximum_results"],
            )
        if profile.capability_id == "database.inspect.readonly":
            return self.local_broker.inspect_database(bindings[0])
        if profile.capability_id == "project.file.patch":
            return self.local_broker.patch_file(
                proposal=proposal,
                execution_key=execution_key,
                binding=bindings[0],
            )
        if profile.capability_id == "project.file.create":
            return self.local_broker.create_file(
                proposal=proposal,
                execution_key=execution_key,
                binding=bindings[0],
            )
        if profile.capability_id == "project.temp.dispose_owned":
            return self.local_broker.dispose_owned_file(
                proposal=proposal,
                relative_path=arguments["relative_path"],
                ownership_reference=arguments["ownership_reference"],
                expected_before_sha256=arguments["expected_before_sha256"],
            )
        if profile.capability_id == "project.change.rollback":
            return self.local_broker.rollback_patch(
                proposal=proposal,
                backup_reference=arguments["backup_reference"],
                expected_current_sha256=arguments["expected_current_sha256"],
            )
        _fail("tool_executor_unavailable")

    @staticmethod
    def _process_output(
        result: ProcessRunResult,
        capability_id: str | None = None,
    ) -> dict[str, Any]:
        if capability_id == "git.status.read":
            if result.exit_code != 0 or result.truncated:
                _fail("tool_git_status_process_failed")
            return parse_git_status_porcelain_v2(result.stdout)
        return {
            "artifact_references": list(result.artifact_references),
            "exit_code": result.exit_code if result.exit_code is not None else -1,
            "stderr": result.stderr,
            "stderr_sha256": result.stderr_sha256,
            "stdout": result.stdout,
            "stdout_sha256": result.stdout_sha256,
            "truncated": result.truncated,
        }

    def execute(
        self,
        *,
        preview: ToolExecutionPreview,
        proposal: ToolProposal,
        plan: PlanGraph,
        authorization: CognitiveAuthorizationArtifact,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> GatewayExecutionOutcome:
        if not isinstance(preview, ToolExecutionPreview):
            _fail("invalid_tool_execution_preview")
        execution_key = self.execution_store._execution_key(
            proposal=proposal,
            node_digest=preview.node_digest,
            attempt_number=preview.attempt_number,
        )
        now = self._trusted_at()
        authorization.assert_current(now)
        if not preview.prepared_at <= now < preview.expires_at:
            _fail("tool_execution_preview_expired")
        components = self._prepare_components(
            proposal=proposal,
            plan=plan,
            authorization=authorization,
            attempt_number=preview.attempt_number,
            prepared_at=preview.prepared_at,
        )
        if components.preview.to_dict() != preview.to_dict():
            _fail("tool_execution_preview_drift")
        for binding in components.path_bindings:
            revalidate_resolved_path(binding)
        if components.command is not None:
            self.command_registry.revalidate(components.command)
        prior = self.execution_store.get_execution_by_idempotency(execution_key)
        if prior is not None:
            if (
                prior.proposal_id != proposal.proposal_id
                or prior.proposal_digest != proposal.proposal_digest
                or prior.run_id != proposal.run_id
                or prior.preview_id != preview.preview_id
                or prior.preview_digest != preview.preview_digest
                or prior.attempt_number != preview.attempt_number
                or prior.invocation_kind != preview.invocation_kind
                or tuple(prior.resolved_argv) != tuple(preview.resolved_argv)
                or prior.working_directory != preview.working_directory
                or prior.idempotency_key != execution_key
            ):
                _fail("tool_execution_replay_collision")
            return GatewayExecutionOutcome(prior, idempotent_replay=True)
        reservation = self.execution_store.reserve_tool_execution(
            proposal=proposal,
            preview=preview,
            source_authority=source_authority,
            authorization=authorization,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        if reservation["status"] == "terminal_replay":
            prior = reservation["execution"]
            if (
                prior.proposal_id != proposal.proposal_id
                or prior.proposal_digest != proposal.proposal_digest
                or prior.run_id != proposal.run_id
                or prior.idempotency_key != execution_key
                or prior.preview_id != preview.preview_id
                or prior.preview_digest != preview.preview_digest
                or prior.attempt_number != preview.attempt_number
                or prior.invocation_kind != preview.invocation_kind
                or tuple(prior.resolved_argv) != tuple(preview.resolved_argv)
                or prior.working_directory != preview.working_directory
            ):
                _fail("tool_execution_replay_collision")
            return GatewayExecutionOutcome(
                prior,
                idempotent_replay=True,
            )
        if reservation["status"] == "recovery_required":
            run = self.execution_store.get_run(proposal.run_id)
            if run.status != "recovery_required":
                self.execution_store.transition_run(
                    run_id=run.run_id,
                    expected_run_digest=run.run_digest,
                    next_status="recovery_required",
                    failure_code="tool_execution_outcome_unknown",
                    source_authority=source_authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
            _fail("tool_execution_outcome_unknown")
        started_at = self._trusted_at()
        process_result: ProcessRunResult | None = None
        output: Mapping[str, Any] = {}
        status = "succeeded"
        verification = "passed"
        reason_codes: list[str] = ["tool_execution_verified"]
        effect_may_have_occurred = False
        try:
            if self.execution_store.cancellation_requested(proposal.run_id):
                status = "failed"
                verification = "failed"
                reason_codes = (
                    "tool_execution_cancelled",
                    "tool_execution_no_owned_change",
                )
                output = {"reason_code": "tool_execution_cancelled"}
            elif components.command is None:
                output = self._execute_native(
                    profile=components.profile,
                    proposal=proposal,
                    execution_key=reservation["execution_key"],
                    bindings=components.path_bindings,
                )
            else:
                process_result = self.process_supervisor.run(
                    components.command,
                    timeout_ms=preview.effective_timeout_ms,
                    output_limit_bytes=preview.effective_output_bytes,
                    cancellation_requested=lambda: (
                        self.execution_store.cancellation_requested(
                            proposal.run_id
                        )
                    ),
                )
                output = self._process_output(
                    process_result,
                    components.profile.capability_id,
                )
                reason_codes = list(process_result.reason_codes)
                if (
                    process_result.exit_code != 0
                    or process_result.suspected_stall
                    or any(
                        item
                        in {
                            "tool_output_limit_exceeded",
                            "tool_output_artifact_limit_exceeded",
                            "tool_output_encoding_invalid",
                            "tool_output_secret_detected",
                            "tool_execution_timeout_exceeded",
                            "tool_execution_cancelled",
                            "tool_cancellation_check_failed",
                            "owned_child_process_terminated",
                        }
                        for item in process_result.reason_codes
                    )
                ):
                    status = (
                        "failed"
                        if components.profile.capability_id
                        in _READ_ONLY_PROCESS_CAPABILITIES
                        else "recovery_required"
                    )
                    verification = (
                        "failed" if status == "failed" else "unverifiable"
                    )
                    if status == "failed":
                        reason_codes.append("tool_execution_no_owned_change")
            if status == "succeeded":
                output = components.profile.validate_output(output)
                if contains_secret_material(canonical_json(output)):
                    _fail("tool_output_secret_detected")
        except CognitiveLocalBrokerError as error:
            effect_may_have_occurred = error.effect_may_have_occurred
            status = "recovery_required" if effect_may_have_occurred else "failed"
            verification = "unverifiable" if status == "recovery_required" else "failed"
            reason_codes = [error.reason_code]
            if status == "failed":
                reason_codes.append("tool_execution_no_owned_change")
            output = {"reason_code": error.reason_code}
        except (
            CognitiveCapabilityError,
            CognitiveSupervisionError,
            CognitiveToolGatewayError,
        ) as error:
            status = "recovery_required"
            verification = "unverifiable"
            reason_codes = [getattr(error, "reason_code", str(error))]
            output = {"reason_code": reason_codes[0]}
        finished_at = self._trusted_at()
        result = _create_tool_execution_result(
            execution_id=make_identity(
                "toolexecution",
                {
                    "execution_key": reservation["execution_key"],
                    "reservation_receipt_digest": reservation[
                        "reservation_receipt_digest"
                    ],
                },
            ),
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            run_id=proposal.run_id,
            idempotency_key=reservation["execution_key"],
            reservation_event_id=reservation["reservation_event_id"],
            reservation_receipt_digest=reservation[
                "reservation_receipt_digest"
            ],
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            attempt_number=preview.attempt_number,
            invocation_kind=preview.invocation_kind,
            executor_kind=(
                "native"
                if preview.invocation_kind == "native_call"
                and components.profile.capability_id
                not in {
                    "project.file.patch",
                    "project.file.create",
                    "project.temp.dispose_owned",
                    "project.change.rollback",
                }
                else "local_broker"
            ),
            resolved_argv=preview.resolved_argv,
            working_directory=preview.working_directory,
            policy_decision="allow",
            reason_codes=tuple(reason_codes),
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            exit_code=(None if process_result is None else process_result.exit_code),
            output=output,
            stdout_digest=(
                None if process_result is None else process_result.stdout_sha256
            ),
            stderr_digest=(
                None if process_result is None else process_result.stderr_sha256
            ),
            artifact_references=(
                ()
                if process_result is None
                else process_result.artifact_references
            ),
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status=verification,
        )
        recorded = self.execution_store.record_tool_execution(
            result=result,
            source_authority=source_authority,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        return GatewayExecutionOutcome(
            recorded["execution"],
            idempotent_replay=recorded["idempotent_replay"],
        )


__all__ = [
    "CognitiveToolGateway",
    "CognitiveToolGatewayError",
    "TOOL_GATEWAY_VERSION",
]
