from __future__ import annotations

from typing import Any, Mapping, Sequence

from memory.cognitive_capability import (
    AVAILABILITY_STATES,
    CapabilityProfile,
    CapabilityRegistry,
    CognitiveCapabilityError,
    suggested_capability_profiles,
)


EXECUTION_CAPABILITY_CONTRACT_VERSION = 1

_BASELINE_IDS = {
    "project.file.read",
    "project.tree.list",
    "git.status.read",
    "project.file.patch",
}
_EXECUTION_IDS = {
    "project.text.search",
    "git.log.read",
    "git.show.read",
    "git.diff.read",
    "database.inspect.readonly",
    "test.run.allowlisted",
    "browser.check.readonly",
    "memory.retrieve",
    "operational_state.retrieve",
    "project.file.create",
    "project.temp.dispose_owned",
    "process.template.run",
    "git.commit.create",
    "project.change.rollback",
    "external.codex.handoff",
}
EXECUTION_CAPABILITY_IDS = tuple(sorted(_BASELINE_IDS | _EXECUTION_IDS))
_IMPLEMENTED_EXECUTOR_IDS = {
    "project.file.read",
    "project.tree.list",
    "project.text.search",
    "git.status.read",
    "git.log.read",
    "git.show.read",
    "git.diff.read",
    "database.inspect.readonly",
    "project.file.patch",
    "project.file.create",
    "project.temp.dispose_owned",
    "process.template.run",
    "project.change.rollback",
}


def _fail(reason_code: str) -> None:
    raise CognitiveCapabilityError(reason_code)


def _object_schema(
    properties: Mapping[str, Mapping[str, Any]],
    required: Sequence[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _array_schema(
    items: Mapping[str, Any],
    *,
    minimum: int = 0,
    maximum: int = 256,
    unique: bool = False,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": dict(items),
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": unique,
    }


def bounded_execution_capability_profiles(
    availability_by_id: Mapping[str, str] | None = None,
) -> tuple[CapabilityProfile, ...]:
    """Return code-owned v10-D descriptors; descriptors never grant authority."""

    availability = dict(availability_by_id or {})
    if set(availability) - set(EXECUTION_CAPABILITY_IDS):
        _fail("unknown_execution_capability_availability_override")
    if any(value not in AVAILABILITY_STATES for value in availability.values()):
        _fail("invalid_execution_capability_availability")

    baseline_overrides = {
        key: value for key, value in availability.items() if key in _BASELINE_IDS
    }
    profiles = list(suggested_capability_profiles(baseline_overrides))

    def state(capability_id: str, default: str = "disabled") -> str:
        requested = availability.get(capability_id, default)
        if requested == "available" and capability_id not in _IMPLEMENTED_EXECUTOR_IDS:
            # A profile cannot advertise execution that the Gateway cannot
            # dispatch.  Handoff-only profiles remain honest handoffs; all
            # other non-executors stay disabled even under a bad override.
            return default if default != "available" else "disabled"
        return requested

    relative_path = {"type": "string", "minLength": 1, "maxLength": 1024}
    text = {"type": "string", "minLength": 0, "maxLength": 262_144}
    short_text = {"type": "string", "minLength": 0, "maxLength": 16_384}
    identity = {"type": "string", "minLength": 3, "maxLength": 191}
    sha256 = {"type": "string", "minLength": 64, "maxLength": 64}
    artifact_references = _array_schema(
        {"type": "string", "minLength": 1, "maxLength": 1024},
        maximum=64,
        unique=True,
    )
    relative_paths = _array_schema(
        relative_path,
        minimum=0,
        maximum=256,
        unique=True,
    )
    command_output = _object_schema(
        {
            "exit_code": {"type": "integer"},
            "stdout": text,
            "stderr": text,
            "stdout_sha256": sha256,
            "stderr_sha256": sha256,
            "truncated": {"type": "boolean"},
            "artifact_references": artifact_references,
        },
        (
            "exit_code",
            "stdout",
            "stderr",
            "stdout_sha256",
            "stderr_sha256",
            "truncated",
            "artifact_references",
        ),
    )

    profiles.extend(
        (
            CapabilityProfile(
                capability_id="project.text.search",
                display_name="Search text inside authorized project files",
                version=1,
                input_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "search_text": {"type": "string", "minLength": 1, "maxLength": 512},
                        "maximum_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    ("relative_path", "search_text", "maximum_results"),
                ),
                output_schema=_object_schema(
                    {
                        "matches": _array_schema(
                            _object_schema(
                                {
                                    "relative_path": relative_path,
                                    "line": {"type": "integer", "minimum": 1},
                                    "text": short_text,
                                },
                                ("relative_path", "line", "text"),
                            ),
                            maximum=500,
                        ),
                        "truncated": {"type": "boolean"},
                    },
                    ("matches", "truncated"),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("project.files",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=10_000,
                max_output_bytes=300_000,
                idempotency="read_only",
                verification_method=("resolved_path_within_authorized_root",),
                availability=state("project.text.search"),
                executor_kind="native",
            ),
            CapabilityProfile(
                capability_id="git.log.read",
                display_name="Read bounded Git history",
                version=1,
                input_schema=_object_schema(
                    {
                        "maximum_commits": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                        "relative_paths": relative_paths,
                    },
                    ("maximum_commits", "relative_paths"),
                ),
                output_schema=command_output,
                risk_class="low",
                reversible=True,
                read_scopes=("project.git_history",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=20_000,
                max_output_bytes=300_000,
                idempotency="read_only",
                verification_method=("git_object_identity",),
                availability=state("git.log.read"),
                executor_kind="native",
            ),
            CapabilityProfile(
                capability_id="git.show.read",
                display_name="Read one bounded Git object",
                version=1,
                input_schema=_object_schema(
                    {
                        "revision": {"type": "string", "minLength": 7, "maxLength": 40},
                        "relative_paths": relative_paths,
                    },
                    ("revision", "relative_paths"),
                ),
                output_schema=command_output,
                risk_class="low",
                reversible=True,
                read_scopes=("project.git_history",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=20_000,
                max_output_bytes=500_000,
                idempotency="read_only",
                verification_method=("git_object_identity",),
                availability=state("git.show.read"),
                executor_kind="native",
            ),
            CapabilityProfile(
                capability_id="git.diff.read",
                display_name="Read bounded Git diff",
                version=1,
                input_schema=_object_schema(
                    {"relative_paths": relative_paths},
                    ("relative_paths",),
                ),
                output_schema=command_output,
                risk_class="low",
                reversible=True,
                read_scopes=("project.git_status", "project.files"),
                write_scopes=(),
                required_authority="read",
                timeout_ms=20_000,
                max_output_bytes=500_000,
                idempotency="read_only",
                verification_method=("git_diff_check",),
                availability=state("git.diff.read"),
                executor_kind="native",
            ),
            CapabilityProfile(
                capability_id="database.inspect.readonly",
                display_name="Inspect one authorized SQLite database read only",
                version=1,
                input_schema=_object_schema(
                    {"relative_path": relative_path},
                    ("relative_path",),
                ),
                output_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "integrity": {"type": "string", "enum": ["ok"]},
                        "foreign_key_violations": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "user_version": {"type": "integer", "minimum": 0},
                        "tables": _array_schema(
                            {"type": "string", "minLength": 1, "maxLength": 128},
                            maximum=512,
                            unique=True,
                        ),
                        "database_sha256": sha256,
                        "database_identity_digest": sha256,
                        "sidecars": _array_schema(
                            _object_schema(
                                {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["wal", "shm"],
                                    },
                                    "size": {
                                        "type": "integer",
                                        "minimum": 0,
                                    },
                                    "sha256": sha256,
                                    "identity_digest": sha256,
                                },
                                ("kind", "size", "sha256", "identity_digest"),
                            ),
                            maximum=2,
                            unique=True,
                        ),
                        "absent_sidecars": _array_schema(
                            {
                                "type": "string",
                                "enum": ["-wal", "-shm"],
                            },
                            maximum=2,
                            unique=True,
                        ),
                        "snapshot_digest": sha256,
                        "freshness": {
                            "type": "string",
                            "enum": ["stable_readonly_snapshot"],
                        },
                    },
                    (
                        "relative_path",
                        "integrity",
                        "foreign_key_violations",
                        "user_version",
                        "tables",
                        "database_sha256",
                        "database_identity_digest",
                        "sidecars",
                        "absent_sidecars",
                        "snapshot_digest",
                        "freshness",
                    ),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("project.databases",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=20_000,
                max_output_bytes=100_000,
                idempotency="read_only",
                verification_method=(
                    "sqlite_readonly",
                    "sqlite_sidecar_snapshot",
                    "sqlite_freshness_stable",
                    "integrity_check",
                    "foreign_key_check",
                ),
                availability=state("database.inspect.readonly"),
                executor_kind="native",
            ),
            CapabilityProfile(
                capability_id="test.run.allowlisted",
                display_name="Read-only test runner unavailable without sandbox",
                version=1,
                input_schema=_object_schema(
                    {
                        "test_ids": _array_schema(
                            {"type": "string", "minLength": 1, "maxLength": 191},
                            minimum=1,
                            maximum=64,
                            unique=True,
                        )
                    },
                    ("test_ids",),
                ),
                output_schema=command_output,
                risk_class="low",
                reversible=True,
                read_scopes=("project.files",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=1_200_000,
                max_output_bytes=1_000_000,
                idempotency="read_only",
                verification_method=("sandbox_unavailable",),
                # v10-D deliberately has no arbitrary Python import surface.
                # Ignore every availability override until an OS-enforced
                # read-only, no-network test sandbox exists.
                availability="disabled",
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="browser.check.readonly",
                display_name="Run one bounded read-only browser check",
                version=1,
                input_schema=_object_schema(
                    {
                        "loopback_target": {"type": "string", "minLength": 1, "maxLength": 2048},
                        "check_id": identity,
                    },
                    ("loopback_target", "check_id"),
                ),
                output_schema=_object_schema(
                    {
                        "loopback_target": {"type": "string", "minLength": 1, "maxLength": 2048},
                        "status_code": {"type": "integer", "minimum": 100, "maximum": 599},
                        "title": {"type": "string", "minLength": 0, "maxLength": 512},
                        "artifact_references": artifact_references,
                    },
                    ("loopback_target", "status_code", "title", "artifact_references"),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("browser.loopback",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=60_000,
                max_output_bytes=100_000,
                idempotency="read_only",
                verification_method=("browser_artifact_digest",),
                availability=state("browser.check.readonly", "handoff_only"),
                executor_kind="external_agent",
            ),
            CapabilityProfile(
                capability_id="memory.retrieve",
                display_name="Retrieve bounded v7 memory package references",
                version=1,
                input_schema=_object_schema(
                    {"search_text": {"type": "string", "minLength": 1, "maxLength": 512}},
                    ("search_text",),
                ),
                output_schema=_object_schema(
                    {
                        "references": artifact_references,
                        "summary": short_text,
                    },
                    ("references", "summary"),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("v7.memory",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=10_000,
                max_output_bytes=100_000,
                idempotency="read_only",
                verification_method=("source_anchor_digest",),
                availability=state("memory.retrieve"),
                executor_kind="runtime",
            ),
            CapabilityProfile(
                capability_id="operational_state.retrieve",
                display_name="Retrieve bounded v9 operational state",
                version=1,
                input_schema=_object_schema(
                    {"project_id": identity},
                    ("project_id",),
                ),
                output_schema=_object_schema(
                    {
                        "references": artifact_references,
                        "summary": short_text,
                    },
                    ("references", "summary"),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("v9.operational_state",),
                write_scopes=(),
                required_authority="read",
                timeout_ms=10_000,
                max_output_bytes=100_000,
                idempotency="read_only",
                verification_method=("state_event_digest",),
                availability=state("operational_state.retrieve"),
                executor_kind="runtime",
            ),
            CapabilityProfile(
                capability_id="project.file.create",
                display_name="Create one authorized project file",
                version=1,
                input_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "content": text,
                        "expected_after_sha256": sha256,
                    },
                    ("relative_path", "content", "expected_after_sha256"),
                ),
                output_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "after_sha256": sha256,
                        "backup_reference": identity,
                        "created": {"type": "boolean", "const": True},
                        "verification_passed": {"type": "boolean", "const": True},
                    },
                    (
                        "relative_path",
                        "after_sha256",
                        "backup_reference",
                        "created",
                        "verification_passed",
                    ),
                ),
                risk_class="medium",
                reversible=True,
                read_scopes=("project.files",),
                write_scopes=("project.files",),
                required_authority="explicit_act",
                timeout_ms=10_000,
                max_output_bytes=16_384,
                idempotency="exactly_once",
                verification_method=("backup_receipt", "content_sha256", "resolved_path_within_authorized_root"),
                availability=state("project.file.create"),
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="project.temp.dispose_owned",
                display_name="Dispose one temporary file owned by the current run",
                version=1,
                input_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "ownership_reference": identity,
                        "expected_before_sha256": sha256,
                    },
                    ("relative_path", "ownership_reference", "expected_before_sha256"),
                ),
                output_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "before_sha256": sha256,
                        "ownership_reference": identity,
                        "backup_reference": identity,
                        "disposed": {"type": "boolean", "const": True},
                    },
                    (
                        "relative_path",
                        "before_sha256",
                        "ownership_reference",
                        "backup_reference",
                        "disposed",
                    ),
                ),
                risk_class="medium",
                reversible=True,
                read_scopes=("project.owned_temp",),
                write_scopes=("project.owned_temp",),
                required_authority="explicit_act",
                timeout_ms=5_000,
                max_output_bytes=16_384,
                idempotency="idempotent",
                verification_method=("owned_temp_receipt", "path_absent", "backup_receipt"),
                availability=state("project.temp.dispose_owned"),
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="process.template.run",
                display_name="Invoke one fixed code-owned process template",
                version=1,
                input_schema=_object_schema(
                    {
                        "template_id": {
                            "type": "string",
                            "enum": ["git.diff.check"],
                        },
                        "relative_paths": relative_paths,
                    },
                    ("template_id", "relative_paths"),
                ),
                output_schema=command_output,
                risk_class="low",
                reversible=True,
                read_scopes=("project.files", "project.git_status"),
                write_scopes=(),
                required_authority="read",
                timeout_ms=300_000,
                max_output_bytes=1_000_000,
                idempotency="read_only",
                verification_method=("resolved_argv", "exit_code", "output_digest"),
                availability=state("process.template.run"),
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="git.commit.create",
                display_name="Create one authorized Git commit from exact paths",
                version=1,
                input_schema=_object_schema(
                    {
                        "relative_paths": _array_schema(
                            relative_path,
                            minimum=1,
                            maximum=128,
                            unique=True,
                        ),
                        "message": {"type": "string", "minLength": 1, "maxLength": 256},
                        "expected_head": sha256,
                    },
                    ("relative_paths", "message", "expected_head"),
                ),
                output_schema=_object_schema(
                    {
                        "head_before": sha256,
                        "head_after": sha256,
                        "committed_paths": relative_paths,
                        "verification_passed": {"type": "boolean", "const": True},
                    },
                    ("head_before", "head_after", "committed_paths", "verification_passed"),
                ),
                risk_class="high",
                reversible=True,
                read_scopes=("project.git_status", "project.files"),
                write_scopes=("project.git_history",),
                required_authority="explicit_act",
                timeout_ms=60_000,
                max_output_bytes=100_000,
                idempotency="exactly_once",
                verification_method=("git_head_identity", "git_commit_paths", "inverse_commit_available"),
                availability=state("git.commit.create"),
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="project.change.rollback",
                display_name="Rollback one change owned by the current run",
                version=1,
                input_schema=_object_schema(
                    {
                        "backup_reference": identity,
                        "expected_current_sha256": sha256,
                    },
                    ("backup_reference", "expected_current_sha256"),
                ),
                output_schema=_object_schema(
                    {
                        "relative_path": relative_path,
                        "restored_sha256": sha256,
                        "backup_reference": identity,
                        "verification_passed": {"type": "boolean", "const": True},
                    },
                    ("relative_path", "restored_sha256", "backup_reference", "verification_passed"),
                ),
                risk_class="medium",
                reversible=True,
                read_scopes=("project.files",),
                write_scopes=("project.files",),
                required_authority="explicit_act",
                timeout_ms=10_000,
                max_output_bytes=16_384,
                idempotency="idempotent",
                verification_method=("backup_receipt", "content_sha256"),
                availability=state("project.change.rollback"),
                executor_kind="local_broker",
            ),
            CapabilityProfile(
                capability_id="external.codex.handoff",
                display_name="Create an untrusted Codex handoff artifact",
                version=1,
                input_schema=_object_schema(
                    {"handoff_reference": identity},
                    ("handoff_reference",),
                ),
                output_schema=_object_schema(
                    {
                        "handoff_reference": identity,
                        "executor_available": {"type": "boolean", "const": False},
                    },
                    ("handoff_reference", "executor_available"),
                ),
                risk_class="low",
                reversible=True,
                read_scopes=("project.files",),
                write_scopes=(),
                required_authority="prepare",
                timeout_ms=5_000,
                max_output_bytes=100_000,
                idempotency="read_only",
                verification_method=("handoff_digest", "independent_result_verification"),
                availability=state("external.codex.handoff", "handoff_only"),
                executor_kind="external_agent",
            ),
        )
    )
    return tuple(sorted(profiles, key=lambda item: item.capability_id))


def build_bounded_execution_capability_registry(
    availability_by_id: Mapping[str, str] | None = None,
) -> CapabilityRegistry:
    profiles = bounded_execution_capability_profiles(availability_by_id)
    if any(
        profile.availability == "available"
        and profile.capability_id not in _IMPLEMENTED_EXECUTOR_IDS
        for profile in profiles
    ):
        _fail("execution_capability_executor_unavailable")
    return CapabilityRegistry(profiles)


__all__ = [
    "EXECUTION_CAPABILITY_CONTRACT_VERSION",
    "EXECUTION_CAPABILITY_IDS",
    "bounded_execution_capability_profiles",
    "build_bounded_execution_capability_registry",
]
