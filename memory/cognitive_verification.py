from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from memory.cognitive_capability import CapabilityProfile, CognitiveCapabilityError
from memory.cognitive_command_policy import (
    CognitiveCommandPolicyError,
    CommandTemplateRegistry,
    parse_git_status_porcelain_v2,
)
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
)
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    ToolExecutionPreview,
    ToolExecutionResult,
    ToolProposal,
)
from memory.cognitive_local_broker import CognitiveLocalBroker, LOCAL_BROKER_VERSION
from memory.cognitive_orchestration import contains_secret_material
from memory.cognitive_path_policy import (
    CognitivePathPolicyError,
    ResolvedProjectPath,
    resolve_project_path,
)
from memory.db_path_policy import is_reparse_point
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


INDEPENDENT_VERIFICATION_VERSION = 1
_VERIFICATION_FACTORY_SEAL = object()
INDEPENDENT_VERIFIER_IDENTITY_DIGEST = canonical_digest(
    {
        "implementation": "memory.cognitive_verification.CognitiveIndependentVerifier",
        "protocol_version": INDEPENDENT_VERIFICATION_VERSION,
        "verifier_kind": "independent_tool",
    }
)
_MAX_BACKUP_RECORD_BYTES = 64 * 1024
_MAX_PROCESS_ARTIFACT_BYTES = 8 * 1024 * 1024

_VERIFICATION_KINDS = {
    "file_diff",
    "test",
    "git",
    "schema",
    "hash",
    "api",
    "browser",
    "static_check",
    "independent_query",
    "human_visual",
    "business_acceptance",
}
_RESULTS = {"passed", "failed", "unverifiable"}
_READ_CAPABILITIES = {
    "project.file.read",
    "project.tree.list",
    "project.text.search",
    "database.inspect.readonly",
}
_WRITE_CAPABILITIES = {"project.file.patch", "project.file.create"}
_ROLLBACK_CAPABILITY = "project.change.rollback"
_DISPOSAL_CAPABILITY = "project.temp.dispose_owned"
_PROCESS_CAPABILITIES = {
    "git.status.read",
    "git.log.read",
    "git.show.read",
    "git.diff.read",
    "test.run.allowlisted",
    "process.template.run",
}
_EXPECTED_TEMPLATES = {
    "git.status.read": "git.status.porcelain-v2.v1",
    "git.log.read": "git.log.bounded.v1",
    "git.show.read": "git.show.bounded.v1",
    "git.diff.read": "git.diff.bounded.v1",
    "test.run.allowlisted": "python.unittest.v1",
}


class CognitiveIndependentVerificationError(OperationalStateError):
    pass


class _ObservationFailure(Exception):
    def __init__(self, result: str, reason_code: str):
        super().__init__(reason_code)
        self.result = result
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CognitiveIndependentVerificationError(reason_code)


def _identity(value: Any, reason_code: str) -> str:
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _sha256(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _normalize_strings(
    values: Sequence[str],
    reason_code: str,
    *,
    maximum_items: int = 64,
    maximum_length: int = 1024,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail(reason_code)
    normalized = tuple(sorted(set(values)))
    if not normalized or len(normalized) > maximum_items:
        _fail(reason_code)
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > maximum_length
        or any(ord(char) < 32 for char in item)
        or contains_secret_material(item)
        for item in normalized
    ):
        _fail(reason_code)
    return normalized


def _freeze_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        normalized = json.loads(canonical_json(value))
    except (OperationalStateError, TypeError, ValueError):
        _fail("invalid_independent_verification_evidence")
    if not isinstance(normalized, dict) or contains_secret_material(normalized):
        _fail("invalid_independent_verification_evidence")
    return MappingProxyType(normalized)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(root)))
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


@dataclass(frozen=True)
class IndependentToolVerification:
    """A sealed observation. It grants no authority and performs no state write."""

    verification_id: str
    run_id: str
    objective_id: str
    plan_node_id: str | None
    tool_execution_id: str | None
    verification_kind: str
    verifier_kind: str
    criteria: tuple[str, ...]
    result: str
    evidence_references: tuple[str, ...]
    evidence: Mapping[str, Any]
    evidence_digest: str
    verified_at: str
    acceptance_state: str
    acceptance_event_id: str | None
    proposal_digest: str
    execution_digest: str
    preview_digest: str
    verifier_identity_digest: str = INDEPENDENT_VERIFIER_IDENTITY_DIGEST
    verification_digest: str = ""
    _factory_seal: InitVar[object | None] = None
    _factory_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _VERIFICATION_FACTORY_SEAL:
            _fail("independent_tool_verification_factory_required")
        object.__setattr__(self, "_factory_marker", _VERIFICATION_FACTORY_SEAL)
        for value, code in (
            (self.verification_id, "invalid_independent_verification_id"),
            (self.run_id, "invalid_independent_verification_run_id"),
            (self.objective_id, "invalid_independent_verification_objective_id"),
        ):
            _identity(value, code)
        if self.plan_node_id is not None:
            _identity(
                self.plan_node_id,
                "invalid_independent_verification_plan_node_id",
            )
        if self.tool_execution_id is not None:
            _identity(
                self.tool_execution_id,
                "invalid_independent_verification_execution_id",
            )
        if self.verification_kind not in _VERIFICATION_KINDS:
            _fail("invalid_independent_verification_kind")
        if self.verifier_kind != "independent_tool":
            _fail("independent_verifier_kind_required")
        if self.verifier_identity_digest != INDEPENDENT_VERIFIER_IDENTITY_DIGEST:
            _fail("independent_verifier_identity_mismatch")
        object.__setattr__(
            self,
            "criteria",
            _normalize_strings(
                self.criteria,
                "invalid_independent_verification_criteria",
                maximum_length=191,
            ),
        )
        if self.result not in _RESULTS:
            _fail("invalid_independent_verification_result")
        object.__setattr__(
            self,
            "evidence_references",
            _normalize_strings(
                self.evidence_references,
                "invalid_independent_verification_evidence_references",
            ),
        )
        object.__setattr__(self, "evidence", _freeze_json(self.evidence))
        if canonical_digest(dict(self.evidence)) != self.evidence_digest:
            _fail("independent_verification_evidence_digest_mismatch")
        for value, code in (
            (self.evidence_digest, "invalid_independent_verification_evidence_digest"),
            (self.proposal_digest, "invalid_independent_verification_proposal_digest"),
            (self.execution_digest, "invalid_independent_verification_execution_digest"),
            (self.preview_digest, "invalid_independent_verification_preview_digest"),
        ):
            _sha256(value, code)
        object.__setattr__(self, "verified_at", normalize_timestamp(self.verified_at))
        if self.acceptance_state != "not_requested" or self.acceptance_event_id is not None:
            _fail("independent_verification_cannot_accept_result")
        expected = canonical_digest(self.identity_payload())
        if self.verification_digest and self.verification_digest != expected:
            _fail("independent_verification_digest_mismatch")
        object.__setattr__(self, "verification_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "verification_protocol_version": INDEPENDENT_VERIFICATION_VERSION,
            "verification_id": self.verification_id,
            "run_id": self.run_id,
            "objective_id": self.objective_id,
            "plan_node_id": self.plan_node_id,
            "tool_execution_id": self.tool_execution_id,
            "verification_kind": self.verification_kind,
            "verifier_kind": self.verifier_kind,
            "verifier_identity_digest": self.verifier_identity_digest,
            "criteria": list(self.criteria),
            "result": self.result,
            "evidence_references": list(self.evidence_references),
            "evidence": dict(self.evidence),
            "evidence_digest": self.evidence_digest,
            "verified_at": self.verified_at,
            "acceptance_state": self.acceptance_state,
            "acceptance_event_id": self.acceptance_event_id,
            "proposal_digest": self.proposal_digest,
            "execution_digest": self.execution_digest,
            "preview_digest": self.preview_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "verification_digest": self.verification_digest}

    def to_verification_record(self) -> dict[str, Any]:
        """Project the observation onto the v10 verification_record JSON contract."""

        return {
            "verification_id": self.verification_id,
            "run_id": self.run_id,
            "objective_id": self.objective_id,
            "plan_node_id": self.plan_node_id,
            "tool_execution_id": self.tool_execution_id,
            "verification_kind": self.verification_kind,
            "verifier_kind": self.verifier_kind,
            "criteria": list(self.criteria),
            "result": self.result,
            "evidence_references": list(self.evidence_references),
            "evidence_digest": self.evidence_digest,
            "verified_at": self.verified_at,
            "acceptance_state": self.acceptance_state,
            "acceptance_event_id": self.acceptance_event_id,
            "verification_digest": self.verification_digest,
        }

    def to_operational_details(self) -> dict[str, Any]:
        """Return JSON-safe details for v9 state_verifications without writing them."""

        return {
            "verification_record": self.to_verification_record(),
            "execution_bindings": {
                "proposal_digest": self.proposal_digest,
                "execution_digest": self.execution_digest,
                "preview_digest": self.preview_digest,
            },
            "sealed_evidence": dict(self.evidence),
            "verifier_identity_digest": self.verifier_identity_digest,
            "binding_digest": canonical_digest(
                {
                    "proposal_digest": self.proposal_digest,
                    "execution_digest": self.execution_digest,
                    "preview_digest": self.preview_digest,
                }
            ),
        }

    def assert_factory_sealed(self) -> None:
        """Revalidate the immutable factory artifact at a persistence boundary."""

        if (
            type(self) is not IndependentToolVerification
            or getattr(self, "_factory_marker", None) is not _VERIFICATION_FACTORY_SEAL
        ):
            _fail("independent_tool_verification_factory_required")
        if (
            self.verifier_kind != "independent_tool"
            or self.verifier_identity_digest != INDEPENDENT_VERIFIER_IDENTITY_DIGEST
        ):
            _fail("independent_verifier_identity_mismatch")
        if not isinstance(self.evidence, Mapping):
            _fail("invalid_independent_verification_evidence")
        try:
            normalized_evidence = json.loads(canonical_json(dict(self.evidence)))
        except (OperationalStateError, TypeError, ValueError):
            _fail("invalid_independent_verification_evidence")
        if (
            not isinstance(normalized_evidence, dict)
            or canonical_digest(normalized_evidence) != self.evidence_digest
        ):
            _fail("independent_verification_evidence_digest_mismatch")
        normalized_criteria = _normalize_strings(
            self.criteria,
            "invalid_independent_verification_criteria",
            maximum_length=191,
        )
        normalized_references = _normalize_strings(
            self.evidence_references,
            "invalid_independent_verification_evidence_references",
        )
        if normalized_criteria != self.criteria or normalized_references != self.evidence_references:
            _fail("independent_tool_verification_mutated")
        if canonical_digest(self.identity_payload()) != self.verification_digest:
            _fail("independent_verification_digest_mismatch")


def _create_independent_verification(**values: Any) -> IndependentToolVerification:
    return IndependentToolVerification(
        **values,
        _factory_seal=_VERIFICATION_FACTORY_SEAL,
    )


class CognitiveIndependentVerifier:
    """Independently observes bounded tool effects; never trusts self-check status."""

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str],
        artifact_root: str | os.PathLike[str],
        command_registry: CommandTemplateRegistry | None = None,
    ) -> None:
        try:
            project = Path(project_root).resolve(strict=True)
            artifacts = Path(artifact_root).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            _fail("independent_verifier_root_unavailable")
        if (
            not project.is_dir()
            or not artifacts.is_dir()
            or project.is_symlink()
            or artifacts.is_symlink()
            or is_reparse_point(project)
            or is_reparse_point(artifacts)
        ):
            _fail("independent_verifier_root_unavailable")
        self.project_root = project
        self.artifact_root = artifacts
        self.local_broker = CognitiveLocalBroker(
            project_root=project,
            artifact_root=artifacts,
        )
        if command_registry is not None and not isinstance(
            command_registry,
            CommandTemplateRegistry,
        ):
            _fail("invalid_independent_command_registry")
        self.command_registry = command_registry or CommandTemplateRegistry()

    @staticmethod
    def _verification_kind(capability_id: str) -> str:
        if capability_id in _WRITE_CAPABILITIES | {
            _ROLLBACK_CAPABILITY,
            _DISPOSAL_CAPABILITY,
        } or capability_id == "project.file.read":
            return "hash"
        if capability_id == "database.inspect.readonly":
            return "independent_query"
        if capability_id == "test.run.allowlisted":
            return "test"
        if capability_id.startswith("git."):
            return "git"
        if capability_id == "process.template.run":
            return "static_check"
        return "independent_query"

    @staticmethod
    def _criteria(capability_id: str) -> tuple[str, ...]:
        base = [
            "executor_self_check_not_trusted",
            "proposal_preview_execution_binding",
            "secret_filter",
        ]
        if capability_id in _WRITE_CAPABILITIES | {
            _ROLLBACK_CAPABILITY,
            _DISPOSAL_CAPABILITY,
        }:
            base.extend(("current_content_sha256", "rollback_backup_record"))
        elif capability_id in _READ_CAPABILITIES:
            base.extend(("fresh_readonly_observation", "canonical_output_match"))
        elif capability_id in _PROCESS_CAPABILITIES:
            base.extend(("fixed_template_binding", "process_receipt_artifact_digest"))
        else:
            base.append("supported_independent_method")
        return tuple(base)

    def _current_profile(self, proposal: ToolProposal) -> CapabilityProfile:
        try:
            profiles = bounded_execution_capability_profiles(
                {proposal.capability_id: "available"}
            )
        except Exception:
            raise _ObservationFailure(
                "unverifiable",
                "independent_capability_contract_unavailable",
            ) from None
        profile = next(
            (
                item
                for item in profiles
                if item.capability_id == proposal.capability_id
                and item.version == proposal.capability_version
            ),
            None,
        )
        if profile is None or profile.capability_digest != proposal.capability_digest:
            raise _ObservationFailure(
                "unverifiable",
                "independent_capability_contract_drift",
            )
        try:
            profile.validate_input(dict(proposal.typed_arguments))
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_typed_arguments_invalid",
            ) from None
        return profile

    def _validate_common_binding(
        self,
        *,
        proposal: ToolProposal,
        preview: ToolExecutionPreview,
        result: ToolExecutionResult,
    ) -> None:
        if (
            preview.proposal_digest != proposal.proposal_digest
            or result.proposal_digest != proposal.proposal_digest
            or result.proposal_id != proposal.proposal_id
            or result.run_id != proposal.run_id
            or result.preview_id != preview.preview_id
            or result.preview_digest != preview.preview_digest
            or preview.authorization_digest != proposal.authorization_digest
            or preview.plan_digest != proposal.plan_digest
            or preview.capability_digest != proposal.capability_digest
            or result.attempt_number != preview.attempt_number
            or result.invocation_kind != preview.invocation_kind
            or tuple(result.resolved_argv) != tuple(preview.resolved_argv)
            or result.working_directory != preview.working_directory
        ):
            raise _ObservationFailure(
                "failed",
                "independent_execution_binding_mismatch",
            )
        if result.policy_decision != "allow":
            raise _ObservationFailure(
                "failed",
                "independent_execution_not_authorized",
            )
        if result.status != "succeeded":
            outcome = (
                "unverifiable"
                if result.status in {"cancelled", "recovery_required"}
                else "failed"
            )
            raise _ObservationFailure(
                outcome,
                "independent_execution_not_succeeded",
            )
        if contains_secret_material(dict(result.output)):
            raise _ObservationFailure(
                "unverifiable",
                "independent_output_secret_detected",
            )
        self._validate_preview_path_bindings(proposal=proposal, preview=preview)

    def _validate_preview_path_bindings(
        self,
        *,
        proposal: ToolProposal,
        preview: ToolExecutionPreview,
    ) -> None:
        arguments = dict(proposal.typed_arguments)
        if proposal.capability_id in _READ_CAPABILITIES | _WRITE_CAPABILITIES:
            expected_paths = (arguments["relative_path"],)
        elif proposal.capability_id == _ROLLBACK_CAPABILITY:
            record, _raw, _record_path, _storage_key = self._read_backup_record(
                arguments["backup_reference"]
            )
            self._assert_backup_owner(record, proposal)
            if record["after_sha256"] != arguments["expected_current_sha256"]:
                raise _ObservationFailure(
                    "failed",
                    "independent_rollback_backup_binding_mismatch",
                )
            expected_paths = (record["relative_path"],)
        elif proposal.capability_id == _DISPOSAL_CAPABILITY:
            record, _raw, _record_path, _storage_key = self._read_backup_record(
                arguments["ownership_reference"]
            )
            self._assert_backup_owner(record, proposal)
            if (
                record["before_sha256"] is not None
                or record["relative_path"] != arguments["relative_path"]
                or record["after_sha256"] != arguments["expected_before_sha256"]
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_owned_disposal_binding_mismatch",
                )
            expected_paths = (record["relative_path"],)
        elif proposal.capability_id in {
            "git.log.read",
            "git.show.read",
            "git.diff.read",
            "process.template.run",
        }:
            expected_paths = tuple(arguments["relative_paths"])
        else:
            expected_paths = ()
        records = tuple(dict(item) for item in preview.resolved_bindings)
        if len(records) != len(expected_paths):
            raise _ObservationFailure(
                "failed",
                "independent_preview_path_binding_mismatch",
            )
        required = {
            "path_policy_version",
            "project_root",
            "canonical_relative_path",
            "absolute_path",
            "access_mode",
            "target_exists",
            "root_identity",
            "parent_identity",
            "target_identity",
            "target_sha256",
            "target_size",
            "binding_digest",
        }
        for expected_path, record in zip(expected_paths, records, strict=True):
            if set(record) != required:
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_path_binding_mismatch",
                )
            payload = {key: value for key, value in record.items() if key != "binding_digest"}
            expected_absolute = self.project_root.joinpath(
                *expected_path.split("/")
            ).resolve(strict=False)
            if (
                record["path_policy_version"] != 1
                or record["binding_digest"] != canonical_digest(payload)
                or record["project_root"] != str(self.project_root)
                or record["canonical_relative_path"] != expected_path
                or Path(record["absolute_path"]).resolve(strict=False)
                != expected_absolute
                or not _path_within(expected_absolute, self.project_root)
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_path_binding_mismatch",
                )
            access_mode = record["access_mode"]
            allowed_modes = {
                "project.file.read": {"read_file"},
                "project.tree.list": {"read_directory"},
                "project.text.search": {"read_file", "read_directory"},
                "database.inspect.readonly": {"read_file"},
                "project.file.patch": {"write_file"},
                "project.file.create": {"create_file"},
                _ROLLBACK_CAPABILITY: {"write_file"},
                _DISPOSAL_CAPABILITY: {"write_file"},
            }.get(proposal.capability_id, {"read_optional", "read_directory"})
            if access_mode not in allowed_modes:
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_path_mode_mismatch",
                )
            if proposal.capability_id == "project.file.patch" and (
                record["target_sha256"] != arguments["expected_before_sha256"]
                or record["target_exists"] is not True
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_before_hash_mismatch",
                )
            if proposal.capability_id == "project.file.create" and (
                record["target_exists"] is not False
                or record["target_sha256"] is not None
                or record["target_identity"] is not None
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_create_binding_mismatch",
                )
            if proposal.capability_id == _ROLLBACK_CAPABILITY and (
                record["target_sha256"] != arguments["expected_current_sha256"]
                or record["target_exists"] is not True
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_rollback_binding_mismatch",
                )
            if proposal.capability_id == _DISPOSAL_CAPABILITY and (
                record["target_sha256"] != arguments["expected_before_sha256"]
                or record["target_exists"] is not True
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_preview_owned_disposal_binding_mismatch",
                )

    def _resolve(
        self,
        relative_path: str,
        access_mode: str,
        *,
        allow_directory_fallback: bool = False,
    ) -> ResolvedProjectPath:
        try:
            binding = resolve_project_path(
                project_root=self.project_root,
                relative_path=relative_path,
                access_mode=access_mode,
            )
        except CognitivePathPolicyError as error:
            if allow_directory_fallback and error.reason_code == "tool_path_type_mismatch":
                binding = resolve_project_path(
                    project_root=self.project_root,
                    relative_path=relative_path,
                    access_mode="read_directory",
                )
            else:
                raise _ObservationFailure(
                    "unverifiable",
                    "independent_path_resolution_failed",
                ) from None
        if binding.canonical_relative_path != relative_path:
            raise _ObservationFailure(
                "failed",
                "independent_path_not_canonical",
            )
        return binding

    def _observe_read(
        self,
        *,
        proposal: ToolProposal,
        profile: CapabilityProfile,
        result: ToolExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        arguments = dict(proposal.typed_arguments)
        relative_path = arguments["relative_path"]
        if proposal.capability_id == "project.tree.list":
            binding = self._resolve(relative_path, "read_directory")
            observed = self.local_broker.list_directory(
                binding,
                maximum_entries=arguments["maximum_entries"],
            )
        elif proposal.capability_id == "project.text.search":
            binding = self._resolve(
                relative_path,
                "read_file",
                allow_directory_fallback=True,
            )
            observed = self.local_broker.search_text(
                binding,
                search_text=arguments["search_text"],
                maximum_results=arguments["maximum_results"],
            )
        else:
            binding = self._resolve(relative_path, "read_file")
            if proposal.capability_id == "project.file.read":
                observed = self.local_broker.read_file(binding)
            else:
                observed = self.local_broker.inspect_database(binding)
        try:
            reported = profile.validate_output(dict(result.output))
            current = profile.validate_output(observed)
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_reported_output_invalid",
            ) from None
        if proposal.capability_id == "database.inspect.readonly":
            for snapshot in (reported, current):
                sidecars = snapshot.get("sidecars")
                absent = snapshot.get("absent_sidecars")
                if (
                    snapshot.get("freshness") != "stable_readonly_snapshot"
                    or not isinstance(sidecars, list)
                    or not isinstance(absent, list)
                    or any(
                        not isinstance(item, dict)
                        or item.get("kind") not in {"wal", "shm"}
                        or not isinstance(item.get("size"), int)
                        or item["size"] < 0
                        or not isinstance(item.get("sha256"), str)
                        or len(item["sha256"]) != 64
                        or not isinstance(item.get("identity_digest"), str)
                        or len(item["identity_digest"]) != 64
                        for item in sidecars
                    )
                    or {f"-{item['kind']}" for item in sidecars}
                    | set(absent)
                    != {"-wal", "-shm"}
                    or {f"-{item['kind']}" for item in sidecars}
                    & set(absent)
                ):
                    raise _ObservationFailure(
                        "failed",
                        "independent_database_snapshot_invalid",
                    )
        if canonical_json(reported) != canonical_json(current):
            raise _ObservationFailure(
                "failed",
                "independent_read_observation_mismatch",
            )
        evidence = {
            "method": "fresh_readonly_observation",
            "relative_path": binding.canonical_relative_path,
            "binding_digest": binding.binding_digest,
            "reported_output_digest": canonical_digest(reported),
            "observed_output_digest": canonical_digest(current),
        }
        if proposal.capability_id == "database.inspect.readonly":
            evidence.update(
                {
                    "database_snapshot_digest": current["snapshot_digest"],
                    "database_freshness": current["freshness"],
                    "database_sidecar_identity_digests": [
                        item["identity_digest"] for item in current["sidecars"]
                    ],
                }
            )
        return evidence, (f"project:{binding.canonical_relative_path}",)

    def _read_artifact_file(self, path: Path, *, maximum_bytes: int) -> bytes:
        try:
            resolved = path.resolve(strict=True)
            value = resolved.stat(follow_symlinks=False)
        except (OSError, RuntimeError, ValueError):
            raise _ObservationFailure(
                "unverifiable",
                "independent_artifact_unavailable",
            ) from None
        if (
            not _path_within(resolved, self.artifact_root)
            or resolved.is_symlink()
            or is_reparse_point(resolved)
            or not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_size < 0
            or value.st_size > maximum_bytes
        ):
            raise _ObservationFailure(
                "failed",
                "independent_artifact_boundary_mismatch",
            )
        current = self.artifact_root
        try:
            relative = resolved.relative_to(self.artifact_root)
        except ValueError:
            raise _ObservationFailure(
                "failed",
                "independent_artifact_boundary_mismatch",
            ) from None
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink() or is_reparse_point(current):
                raise _ObservationFailure(
                    "failed",
                    "independent_artifact_boundary_mismatch",
                )
        try:
            with resolved.open("rb") as handle:
                raw = handle.read(maximum_bytes + 1)
        except OSError:
            raise _ObservationFailure(
                "unverifiable",
                "independent_artifact_unavailable",
            ) from None
        if len(raw) != value.st_size or len(raw) > maximum_bytes:
            raise _ObservationFailure(
                "unverifiable",
                "independent_artifact_changed_during_read",
            )
        return raw

    def _read_backup_record(
        self,
        reference: str,
    ) -> tuple[dict[str, Any], bytes, Path, str]:
        if (
            not isinstance(reference, str)
            or not reference.startswith("toolbackup:sha256:")
            or len(reference) != len("toolbackup:sha256:") + 64
            or any(
                character not in "0123456789abcdef"
                for character in reference[len("toolbackup:sha256:") :]
            )
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_reference_invalid",
            )
        storage_key = canonical_digest({"backup_reference": reference})
        record_path = self.artifact_root / f"{storage_key}.json"
        raw = self._read_artifact_file(
            record_path,
            maximum_bytes=_MAX_BACKUP_RECORD_BYTES,
        )
        try:
            text = raw.decode("utf-8", errors="strict")
            record = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_invalid",
            ) from None
        expected_keys = {
            "broker_version",
            "run_id",
            "objective_id",
            "objective_digest",
            "plan_id",
            "plan_digest",
            "authority_artifact_digest",
            "execution_key",
            "proposal_id",
            "proposal_digest",
            "relative_path",
            "before_sha256",
            "after_sha256",
            "backup_reference",
            "artifact_storage_key",
            "before_artifact",
            "record_digest",
        }
        if (
            not isinstance(record, dict)
            or set(record) != expected_keys
            or contains_secret_material(record)
            or canonical_json(record) != text
            or record["backup_reference"] != reference
            or record["artifact_storage_key"] != storage_key
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_invalid",
            )
        identity = {
            key: record[key]
            for key in (
                "broker_version",
                "run_id",
                "objective_id",
                "objective_digest",
                "plan_id",
                "plan_digest",
                "authority_artifact_digest",
                "execution_key",
                "proposal_id",
                "proposal_digest",
                "relative_path",
                "before_sha256",
                "after_sha256",
            )
        }
        before_sha256 = record["before_sha256"]
        after_sha256 = record["after_sha256"]
        expected_before_artifact = (
            None if before_sha256 is None else f"{storage_key}.before.bin"
        )
        if (
            record["broker_version"] != LOCAL_BROKER_VERSION
            or not isinstance(record["run_id"], str)
            or not record["run_id"]
            or not isinstance(record["objective_id"], str)
            or not record["objective_id"]
            or not isinstance(record["objective_digest"], str)
            or len(record["objective_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in record["objective_digest"]
            )
            or not isinstance(record["plan_id"], str)
            or not record["plan_id"]
            or not isinstance(record["plan_digest"], str)
            or len(record["plan_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in record["plan_digest"]
            )
            or not isinstance(record["authority_artifact_digest"], str)
            or len(record["authority_artifact_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in record["authority_artifact_digest"]
            )
            or not isinstance(record["execution_key"], str)
            or not record["execution_key"]
            or not isinstance(record["proposal_id"], str)
            or not record["proposal_id"]
            or not isinstance(record["proposal_digest"], str)
            or len(record["proposal_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in record["proposal_digest"]
            )
            or not isinstance(record["relative_path"], str)
            or not record["relative_path"]
            or not isinstance(after_sha256, str)
            or len(after_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in after_sha256
            )
            or (
                before_sha256 is not None
                and (
                    not isinstance(before_sha256, str)
                    or len(before_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in before_sha256
                    )
                )
            )
            or record["record_digest"] != canonical_digest(identity)
            or make_identity("toolbackup", identity) != reference
            or record["before_artifact"] != expected_before_artifact
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_binding_mismatch",
            )
        return record, raw, record_path, storage_key

    @staticmethod
    def _assert_backup_owner(
        record: Mapping[str, Any],
        proposal: ToolProposal,
    ) -> None:
        expected_owner = {
            "run_id": proposal.run_id,
            "objective_id": proposal.objective_id,
            "objective_digest": proposal.objective_digest,
            "plan_id": proposal.plan_id,
            "plan_digest": proposal.plan_digest,
            "authority_artifact_digest": proposal.authorization_digest,
        }
        if any(record.get(key) != value for key, value in expected_owner.items()):
            raise _ObservationFailure(
                "failed",
                "independent_rollback_owner_mismatch",
            )

    def _observe_rollback(
        self,
        *,
        proposal: ToolProposal,
        profile: CapabilityProfile,
        result: ToolExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        arguments = dict(proposal.typed_arguments)
        reference = arguments["backup_reference"]
        record, raw, record_path, storage_key = self._read_backup_record(reference)
        self._assert_backup_owner(record, proposal)
        before_sha256 = record["before_sha256"]
        if (
            before_sha256 is None
            or record["after_sha256"] != arguments["expected_current_sha256"]
        ):
            raise _ObservationFailure(
                "failed",
                "independent_rollback_backup_binding_mismatch",
            )
        binding = self._resolve(record["relative_path"], "write_file")
        if binding.target_sha256 != before_sha256:
            raise _ObservationFailure(
                "failed",
                "independent_rollback_hash_mismatch",
            )
        try:
            output = profile.validate_output(dict(result.output))
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_reported_output_invalid",
            ) from None
        if (
            output["relative_path"] != record["relative_path"]
            or output["restored_sha256"] != before_sha256
            or output["backup_reference"] != reference
            or output["verification_passed"] is not True
        ):
            raise _ObservationFailure(
                "failed",
                "independent_rollback_receipt_mismatch",
            )
        before_path = self.artifact_root / f"{storage_key}.before.bin"
        before_bytes = self._read_artifact_file(
            before_path,
            maximum_bytes=_MAX_PROCESS_ARTIFACT_BYTES,
        )
        if _sha256_bytes(before_bytes) != before_sha256:
            raise _ObservationFailure(
                "failed",
                "independent_backup_content_mismatch",
            )
        evidence = {
            "method": "restored_hash_and_backup_receipt",
            "relative_path": record["relative_path"],
            "current_sha256": binding.target_sha256,
            "target_binding_digest": binding.binding_digest,
            "backup_reference": reference,
            "backup_record_sha256": _sha256_bytes(raw),
            "backup_record_digest": record["record_digest"],
        }
        return evidence, (
            f"project:{record['relative_path']}",
            f"artifact:{record_path.name}",
            f"artifact:{before_path.name}",
        )

    def _observe_owned_disposal(
        self,
        *,
        proposal: ToolProposal,
        profile: CapabilityProfile,
        result: ToolExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        arguments = dict(proposal.typed_arguments)
        reference = arguments["ownership_reference"]
        record, raw, record_path, _storage_key = self._read_backup_record(reference)
        self._assert_backup_owner(record, proposal)
        if (
            record["before_sha256"] is not None
            or record["relative_path"] != arguments["relative_path"]
            or record["after_sha256"] != arguments["expected_before_sha256"]
        ):
            raise _ObservationFailure(
                "failed",
                "independent_owned_disposal_binding_mismatch",
            )
        try:
            missing = self._resolve(record["relative_path"], "create_file")
        except CognitivePathPolicyError:
            raise _ObservationFailure(
                "failed",
                "independent_owned_disposal_target_present",
            ) from None
        if missing.target_exists:
            raise _ObservationFailure(
                "failed",
                "independent_owned_disposal_target_present",
            )
        try:
            output = profile.validate_output(dict(result.output))
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_reported_output_invalid",
            ) from None
        if (
            output["relative_path"] != record["relative_path"]
            or output["before_sha256"] != record["after_sha256"]
            or output["ownership_reference"] != reference
            or output["backup_reference"] != reference
            or output["disposed"] is not True
        ):
            raise _ObservationFailure(
                "failed",
                "independent_owned_disposal_receipt_mismatch",
            )
        evidence = {
            "method": "owned_create_missing_and_receipt",
            "relative_path": record["relative_path"],
            "previous_sha256": record["after_sha256"],
            "missing_binding_digest": missing.binding_digest,
            "ownership_reference": reference,
            "ownership_record_sha256": _sha256_bytes(raw),
            "ownership_record_digest": record["record_digest"],
            "original_execution_key": record["execution_key"],
            "original_proposal_digest": record["proposal_digest"],
        }
        return evidence, (f"artifact:{record_path.name}",)

    def _observe_write(
        self,
        *,
        proposal: ToolProposal,
        profile: CapabilityProfile,
        result: ToolExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        arguments = dict(proposal.typed_arguments)
        relative_path = arguments["relative_path"]
        binding = self._resolve(relative_path, "write_file")
        expected_after = arguments["expected_after_sha256"]
        if binding.target_sha256 != expected_after:
            raise _ObservationFailure(
                "failed",
                "independent_write_hash_mismatch",
            )
        try:
            output = profile.validate_output(dict(result.output))
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_reported_output_invalid",
            ) from None
        if (
            output.get("relative_path") != relative_path
            or output.get("after_sha256") != expected_after
        ):
            raise _ObservationFailure(
                "failed",
                "independent_write_receipt_mismatch",
            )
        reference = output.get("backup_reference")
        if (
            not isinstance(reference, str)
            or not reference.startswith("toolbackup:sha256:")
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_reference_invalid",
            )
        storage_key = canonical_digest({"backup_reference": reference})
        record_path = self.artifact_root / f"{storage_key}.json"
        raw = self._read_artifact_file(
            record_path,
            maximum_bytes=_MAX_BACKUP_RECORD_BYTES,
        )
        try:
            text = raw.decode("utf-8", errors="strict")
            record = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_invalid",
            ) from None
        if (
            not isinstance(record, dict)
            or contains_secret_material(record)
            or canonical_json(record) != text
            or set(record)
            != {
                "broker_version",
                "run_id",
                "objective_id",
                "objective_digest",
                "plan_id",
                "plan_digest",
                "authority_artifact_digest",
                "execution_key",
                "proposal_id",
                "proposal_digest",
                "relative_path",
                "before_sha256",
                "after_sha256",
                "backup_reference",
                "artifact_storage_key",
                "before_artifact",
                "record_digest",
            }
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_invalid",
            )
        expected_before = (
            arguments["expected_before_sha256"]
            if proposal.capability_id == "project.file.patch"
            else None
        )
        identity = {
            "broker_version": LOCAL_BROKER_VERSION,
            "run_id": proposal.run_id,
            "objective_id": proposal.objective_id,
            "objective_digest": proposal.objective_digest,
            "plan_id": proposal.plan_id,
            "plan_digest": proposal.plan_digest,
            "authority_artifact_digest": proposal.authorization_digest,
            "execution_key": result.idempotency_key,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "relative_path": relative_path,
            "before_sha256": expected_before,
            "after_sha256": expected_after,
        }
        expected_reference = make_identity("toolbackup", identity)
        if (
            record["backup_reference"] != reference
            or reference != expected_reference
            or record["artifact_storage_key"] != storage_key
            or record["record_digest"] != canonical_digest(identity)
            or any(record[key] != value for key, value in identity.items())
        ):
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_binding_mismatch",
            )
        evidence_references = [
            f"project:{relative_path}",
            f"artifact:{record_path.name}",
        ]
        if proposal.capability_id == "project.file.patch":
            expected_name = f"{storage_key}.before.bin"
            if record["before_artifact"] != expected_name:
                raise _ObservationFailure(
                    "failed",
                    "independent_backup_record_binding_mismatch",
                )
            before_path = self.artifact_root / expected_name
            before_bytes = self._read_artifact_file(
                before_path,
                maximum_bytes=_MAX_PROCESS_ARTIFACT_BYTES,
            )
            if _sha256_bytes(before_bytes) != expected_before:
                raise _ObservationFailure(
                    "failed",
                    "independent_backup_content_mismatch",
                )
            evidence_references.append(f"artifact:{before_path.name}")
        elif record["before_artifact"] is not None:
            raise _ObservationFailure(
                "failed",
                "independent_backup_record_binding_mismatch",
            )
        evidence = {
            "method": "current_hash_and_backup_receipt",
            "relative_path": relative_path,
            "current_sha256": binding.target_sha256,
            "target_binding_digest": binding.binding_digest,
            "backup_reference": reference,
            "backup_record_sha256": _sha256_bytes(raw),
            "backup_record_digest": record["record_digest"],
        }
        return evidence, tuple(evidence_references)

    def _process_paths(self, proposal: ToolProposal) -> tuple[ResolvedProjectPath, ...]:
        arguments = dict(proposal.typed_arguments)
        values = arguments.get("relative_paths", ())
        bindings: list[ResolvedProjectPath] = []
        for value in values:
            bindings.append(
                self._resolve(
                    value,
                    "read_optional",
                    allow_directory_fallback=True,
                )
            )
        return tuple(bindings)

    @staticmethod
    def _expected_template(proposal: ToolProposal) -> str | None:
        if proposal.capability_id == "process.template.run":
            return {
                "python.compileall": "python.compileall.v1",
                "git.diff.check": "git.diff.check.v1",
            }.get(dict(proposal.typed_arguments).get("template_id"))
        return _EXPECTED_TEMPLATES.get(proposal.capability_id)

    def _observe_process(
        self,
        *,
        proposal: ToolProposal,
        profile: CapabilityProfile,
        preview: ToolExecutionPreview,
        result: ToolExecutionResult,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        expected_template = self._expected_template(proposal)
        if expected_template is None:
            raise _ObservationFailure(
                "unverifiable",
                "independent_process_template_unavailable",
            )
        if (
            preview.invocation_kind != "fixed_process"
            or preview.template_id != expected_template
            or preview.template_digest is None
        ):
            raise _ObservationFailure(
                "failed",
                "independent_process_template_binding_mismatch",
            )
        try:
            if self.command_registry.template_digest(expected_template) != preview.template_digest:
                raise _ObservationFailure(
                    "failed",
                    "independent_process_template_digest_mismatch",
                )
            command = self.command_registry.resolve(
                template_id=expected_template,
                profile=profile,
                typed_arguments=dict(proposal.typed_arguments),
                path_bindings=self._process_paths(proposal),
                project_root=str(self.project_root),
            )
            self.command_registry.revalidate(command)
        except _ObservationFailure:
            raise
        except Exception:
            raise _ObservationFailure(
                "unverifiable",
                "independent_process_template_unverifiable",
            ) from None
        if (
            tuple(preview.resolved_argv) != command.argv
            or Path(preview.working_directory).resolve(strict=False)
            != self.project_root
            or tuple(result.resolved_argv) != command.argv
        ):
            raise _ObservationFailure(
                "failed",
                "independent_process_command_binding_mismatch",
            )
        try:
            output = profile.validate_output(dict(result.output))
        except Exception:
            raise _ObservationFailure(
                "failed",
                "independent_process_receipt_invalid",
            ) from None
        git_status = proposal.capability_id == "git.status.read"
        stdout: bytes | None = None
        stderr: bytes | None = None
        if git_status:
            if result.exit_code != 0:
                raise _ObservationFailure(
                    "failed",
                    "independent_process_receipt_binding_mismatch",
                )
        else:
            if (
                result.exit_code != 0
                or output["exit_code"] != 0
                or output["truncated"] is not False
                or result.stdout_digest != output["stdout_sha256"]
                or result.stderr_digest != output["stderr_sha256"]
                or tuple(sorted(output["artifact_references"]))
                != tuple(sorted(result.artifact_references))
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_process_receipt_binding_mismatch",
                )
            stdout = output["stdout"].encode("utf-8", errors="strict")
            stderr = output["stderr"].encode("utf-8", errors="strict")
            if (
                _sha256_bytes(stdout) != result.stdout_digest
                or _sha256_bytes(stderr) != result.stderr_digest
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_process_output_digest_mismatch",
                )
        artifacts = tuple(result.artifact_references)
        if (git_status or stdout or stderr) and not artifacts:
            raise _ObservationFailure(
                "unverifiable",
                "independent_process_artifacts_missing",
            )
        if artifacts and len(artifacts) != 2:
            raise _ObservationFailure(
                "unverifiable",
                "independent_process_artifacts_incomplete",
            )
        artifact_evidence: dict[str, str] = {}
        artifact_bytes: dict[str, bytes] = {}
        artifact_storage_keys: set[str] = set()
        evidence_references: list[str] = []
        expected_channels = {"stdout": result.stdout_digest, "stderr": result.stderr_digest}
        for reference in artifacts:
            path = Path(reference)
            channel = (
                "stdout"
                if path.name.endswith(".stdout.log")
                else "stderr"
                if path.name.endswith(".stderr.log")
                else None
            )
            if channel is None or channel in artifact_evidence:
                raise _ObservationFailure(
                    "failed",
                    "independent_process_artifact_name_invalid",
                )
            channel_suffix = f".{channel}.log"
            storage_key = path.name[: -len(channel_suffix)]
            if (
                len(storage_key) != 64
                or any(char not in "0123456789abcdef" for char in storage_key)
            ):
                raise _ObservationFailure(
                    "failed",
                    "independent_process_artifact_name_invalid",
                )
            raw = self._read_artifact_file(
                path,
                maximum_bytes=_MAX_PROCESS_ARTIFACT_BYTES,
            )
            digest = _sha256_bytes(raw)
            if digest != expected_channels[channel]:
                raise _ObservationFailure(
                    "failed",
                    "independent_process_artifact_digest_mismatch",
                )
            artifact_evidence[channel] = digest
            artifact_bytes[channel] = raw
            artifact_storage_keys.add(storage_key)
            evidence_references.append(f"artifact:{path.name}")
        if artifacts and set(artifact_evidence) != {"stdout", "stderr"}:
            raise _ObservationFailure(
                "unverifiable",
                "independent_process_artifacts_incomplete",
            )
        if len(artifact_storage_keys) > 1:
            raise _ObservationFailure(
                "failed",
                "independent_process_artifact_binding_mismatch",
            )
        if git_status:
            try:
                observed_stdout = artifact_bytes["stdout"].decode(
                    "utf-8", errors="strict"
                )
                artifact_bytes["stderr"].decode("utf-8", errors="strict")
                observed_output = profile.validate_output(
                    parse_git_status_porcelain_v2(observed_stdout)
                )
            except (
                CognitiveCapabilityError,
                CognitiveCommandPolicyError,
                KeyError,
                UnicodeDecodeError,
            ):
                raise _ObservationFailure(
                    "unverifiable",
                    "independent_git_status_artifact_unverifiable",
                ) from None
            if observed_output != output:
                raise _ObservationFailure(
                    "failed",
                    "independent_git_status_observation_mismatch",
                )
        evidence = {
            "method": (
                "fixed_process_artifact_reobservation"
                if git_status
                else "fixed_process_receipt"
            ),
            "template_id": expected_template,
            "template_digest": preview.template_digest,
            "command_digest": command.command_digest,
            "exit_code": result.exit_code,
            "stdout_sha256": result.stdout_digest,
            "stderr_sha256": result.stderr_digest,
            "artifact_digests": artifact_evidence,
            "artifact_storage_key": (
                next(iter(artifact_storage_keys)) if artifact_storage_keys else None
            ),
        }
        if not evidence_references:
            evidence_references.append(
                f"process-receipt:{result.execution_id}:{result.execution_digest}"
            )
        return evidence, tuple(evidence_references)

    def verify(
        self,
        *,
        proposal: ToolProposal,
        preview: ToolExecutionPreview,
        result: ToolExecutionResult,
        verified_at: str,
    ) -> IndependentToolVerification:
        if not isinstance(proposal, ToolProposal):
            _fail("invalid_independent_verification_proposal")
        if not isinstance(preview, ToolExecutionPreview):
            _fail("invalid_independent_verification_preview")
        if not isinstance(result, ToolExecutionResult):
            _fail("invalid_independent_verification_execution")
        timestamp = normalize_timestamp(verified_at)
        outcome = "passed"
        reason_code = "independent_verification_passed"
        evidence: dict[str, Any]
        references: tuple[str, ...]
        try:
            self._validate_common_binding(
                proposal=proposal,
                preview=preview,
                result=result,
            )
            profile = self._current_profile(proposal)
            if proposal.capability_id in _READ_CAPABILITIES:
                evidence, references = self._observe_read(
                    proposal=proposal,
                    profile=profile,
                    result=result,
                )
            elif proposal.capability_id in _WRITE_CAPABILITIES:
                evidence, references = self._observe_write(
                    proposal=proposal,
                    profile=profile,
                    result=result,
                )
            elif proposal.capability_id == _ROLLBACK_CAPABILITY:
                evidence, references = self._observe_rollback(
                    proposal=proposal,
                    profile=profile,
                    result=result,
                )
            elif proposal.capability_id == _DISPOSAL_CAPABILITY:
                evidence, references = self._observe_owned_disposal(
                    proposal=proposal,
                    profile=profile,
                    result=result,
                )
            elif proposal.capability_id in _PROCESS_CAPABILITIES:
                evidence, references = self._observe_process(
                    proposal=proposal,
                    profile=profile,
                    preview=preview,
                    result=result,
                )
            else:
                raise _ObservationFailure(
                    "unverifiable",
                    "independent_verification_method_unavailable",
                )
        except _ObservationFailure as failure:
            outcome = failure.result
            reason_code = failure.reason_code
            evidence = {
                "method": "fail_closed",
                "reason_code": reason_code,
                "reported_output_digest": canonical_digest(dict(result.output)),
            }
            references = (
                f"execution-receipt:{result.execution_id}:{result.execution_digest}",
            )
        except Exception:
            outcome = "unverifiable"
            reason_code = "independent_verification_internal_failure"
            evidence = {
                "method": "fail_closed",
                "reason_code": reason_code,
                "reported_output_digest": canonical_digest(dict(result.output)),
            }
            references = (
                f"execution-receipt:{result.execution_id}:{result.execution_digest}",
            )
        full_evidence = _freeze_json(
            {
                "verification_protocol_version": INDEPENDENT_VERIFICATION_VERSION,
                "tool_protocol_version": TOOL_PROTOCOL_VERSION,
                "result": outcome,
                "reason_code": reason_code,
                "proposal_digest": proposal.proposal_digest,
                "preview_digest": preview.preview_digest,
                "execution_digest": result.execution_digest,
                "observation": evidence,
            }
        )
        evidence_digest = canonical_digest(dict(full_evidence))
        identity = {
            "run_id": proposal.run_id,
            "proposal_digest": proposal.proposal_digest,
            "preview_digest": preview.preview_digest,
            "execution_digest": result.execution_digest,
            "evidence_digest": evidence_digest,
            "verified_at": timestamp,
        }
        return _create_independent_verification(
            verification_id=make_identity("verification", identity),
            run_id=proposal.run_id,
            objective_id=proposal.objective_id,
            plan_node_id=proposal.plan_node_id,
            tool_execution_id=result.execution_id,
            verification_kind=self._verification_kind(proposal.capability_id),
            verifier_kind="independent_tool",
            criteria=(
                *self._criteria(proposal.capability_id),
                f"observation:{reason_code}",
            ),
            result=outcome,
            evidence_references=references,
            evidence=full_evidence,
            evidence_digest=evidence_digest,
            verified_at=timestamp,
            acceptance_state="not_requested",
            acceptance_event_id=None,
            proposal_digest=proposal.proposal_digest,
            execution_digest=result.execution_digest,
            preview_digest=preview.preview_digest,
        )


def assert_independent_verification_store_binding(
    *,
    verification: IndependentToolVerification,
    proposal: ToolProposal,
    result: ToolExecutionResult,
) -> None:
    """Fail closed unless a sealed observation exactly binds persisted tool facts."""

    if type(verification) is not IndependentToolVerification:
        _fail("sealed_independent_tool_verification_required")
    verification.assert_factory_sealed()
    if not isinstance(proposal, ToolProposal) or not isinstance(result, ToolExecutionResult):
        _fail("invalid_independent_verification_store_binding")
    if (
        verification.run_id != proposal.run_id
        or verification.run_id != result.run_id
        or verification.objective_id != proposal.objective_id
        or verification.plan_node_id != proposal.plan_node_id
        or verification.tool_execution_id != result.execution_id
        or verification.proposal_digest != proposal.proposal_digest
        or verification.proposal_digest != result.proposal_digest
        or verification.execution_digest != result.execution_digest
        or verification.preview_digest != result.preview_digest
    ):
        _fail("independent_verification_store_binding_mismatch")

    evidence = dict(verification.evidence)
    if set(evidence) != {
        "verification_protocol_version",
        "tool_protocol_version",
        "result",
        "reason_code",
        "proposal_digest",
        "preview_digest",
        "execution_digest",
        "observation",
    }:
        _fail("independent_verification_evidence_contract_mismatch")
    reason_code = evidence.get("reason_code")
    if (
        evidence.get("verification_protocol_version")
        != INDEPENDENT_VERIFICATION_VERSION
        or evidence.get("tool_protocol_version") != TOOL_PROTOCOL_VERSION
        or evidence.get("result") != verification.result
        or evidence.get("proposal_digest") != proposal.proposal_digest
        or evidence.get("preview_digest") != result.preview_digest
        or evidence.get("execution_digest") != result.execution_digest
        or not isinstance(evidence.get("observation"), dict)
        or not isinstance(reason_code, str)
        or not reason_code
        or len(reason_code) > 191
    ):
        _fail("independent_verification_evidence_contract_mismatch")

    expected_criteria = _normalize_strings(
        (
            *CognitiveIndependentVerifier._criteria(proposal.capability_id),
            f"observation:{reason_code}",
        ),
        "invalid_independent_verification_criteria",
        maximum_length=191,
    )
    if (
        verification.criteria != expected_criteria
        or verification.verification_kind
        != CognitiveIndependentVerifier._verification_kind(proposal.capability_id)
    ):
        _fail("independent_verification_criteria_mismatch")
    if result.status != "succeeded":
        expected_result = (
            "unverifiable"
            if result.status in {"cancelled", "recovery_required"}
            else "failed"
        )
        if verification.result != expected_result:
            _fail("independent_verification_execution_outcome_mismatch")

    expected_verification_id = make_identity(
        "verification",
        {
            "run_id": proposal.run_id,
            "proposal_digest": proposal.proposal_digest,
            "preview_digest": result.preview_digest,
            "execution_digest": result.execution_digest,
            "evidence_digest": verification.evidence_digest,
            "verified_at": verification.verified_at,
        },
    )
    if verification.verification_id != expected_verification_id:
        _fail("independent_verification_identity_mismatch")


__all__ = [
    "assert_independent_verification_store_binding",
    "CognitiveIndependentVerificationError",
    "CognitiveIndependentVerifier",
    "INDEPENDENT_VERIFIER_IDENTITY_DIGEST",
    "INDEPENDENT_VERIFICATION_VERSION",
    "IndependentToolVerification",
]
