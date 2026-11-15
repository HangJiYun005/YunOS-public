from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import InitVar, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from memory.cognitive_capability import CapabilityProfile
from memory.cognitive_path_policy import ResolvedProjectPath
from memory.operational_state_models import canonical_digest, canonical_json


COMMAND_POLICY_VERSION = 2

_RESOLVED_COMMAND_FACTORY_SEAL = object()
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SHELL_TOKENS = ("&&", "||", "|", ">", "<", "$(`", "$(", "`", "\r", "\n")
_TEMPLATE_CAPABILITIES = {
    "git.status.porcelain-v2.v1": "git.status.read",
    "git.log.bounded.v1": "git.log.read",
    "git.show.bounded.v1": "git.show.read",
    "git.diff.bounded.v1": "git.diff.read",
    "git.diff.check.v1": "process.template.run",
}
_UNAVAILABLE_PYTHON_TEMPLATES = frozenset(
    {"python.unittest.v1", "python.compileall.v1"}
)


def _git_safe_prefix() -> tuple[str, ...]:
    """Code-owned Git configuration that disables indirect executors."""

    return (
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
    )


class CognitiveCommandPolicyError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CognitiveCommandPolicyError(reason_code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        _fail("tool_command_executable_untrusted")
    return digest.hexdigest()


def _executable_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        resolved = Path(path).resolve(strict=True)
        file_stat = resolved.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError):
        _fail("tool_command_executable_untrusted")
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_dev <= 0 or file_stat.st_ino <= 0:
        _fail("tool_command_executable_untrusted")
    return {
        "absolute_path": str(resolved),
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
        "size": int(file_stat.st_size),
        "sha256": _sha256_file(resolved),
    }


def _safe_argument(value: Any, reason_code: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(reason_code)
    if value.startswith("-") or any(token in value for token in _SHELL_TOKENS):
        _fail(reason_code)
    if "%" in value or "${" in value or any(ord(char) < 32 for char in value):
        _fail(reason_code)
    return value


def _minimal_environment() -> dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    temp_root = os.environ.get("TEMP", str(Path.cwd()))
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "TEMP": temp_root,
        "TMP": temp_root,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "NUL",
    }


def parse_git_status_porcelain_v2(
    stdout: str,
    *,
    maximum_paths: int = 2_000,
) -> dict[str, Any]:
    """Parse only the fixed ``git status --porcelain=v2 --branch -z`` shape."""

    if (
        not isinstance(stdout, str)
        or isinstance(maximum_paths, bool)
        or not isinstance(maximum_paths, int)
        or not 1 <= maximum_paths <= 2_000
    ):
        _fail("tool_git_status_output_invalid")
    records = stdout.split("\0") if "\0" in stdout else stdout.splitlines()
    head: str | None = None
    branch: str | None = None
    changed_paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        path: str | None = None
        if record.startswith("# branch.oid "):
            value = record[len("# branch.oid ") :]
            if head is not None or not value or len(value) > 128:
                _fail("tool_git_status_output_invalid")
            head = value
            continue
        if record.startswith("# branch.head "):
            value = record[len("# branch.head ") :]
            if branch is not None or not value or len(value) > 256:
                _fail("tool_git_status_output_invalid")
            branch = value
            continue
        if record.startswith("# branch."):
            # branch.upstream and branch.ab are informational and not projected.
            continue
        if record.startswith("1 "):
            fields = record.split(" ", 8)
            if len(fields) != 9:
                _fail("tool_git_status_output_invalid")
            path = fields[8]
        elif record.startswith("2 "):
            fields = record.split(" ", 9)
            if len(fields) != 10:
                _fail("tool_git_status_output_invalid")
            path = fields[9]
            if "\0" in stdout:
                if index >= len(records) or not records[index]:
                    _fail("tool_git_status_output_invalid")
                index += 1  # Original path for a rename/copy record.
        elif record.startswith("u "):
            fields = record.split(" ", 10)
            if len(fields) != 11:
                _fail("tool_git_status_output_invalid")
            path = fields[10]
        elif record.startswith("? ") or record.startswith("! "):
            path = record[2:]
        else:
            _fail("tool_git_status_output_invalid")
        if (
            not path
            or len(path) > 1_024
            or "\0" in path
            or any(ord(character) < 32 for character in path)
        ):
            _fail("tool_git_status_output_invalid")
        changed_paths.append(path.replace("\\", "/"))
    if head is None or branch is None:
        _fail("tool_git_status_output_invalid")
    unique_paths = sorted(set(changed_paths))
    return {
        "head": head,
        "branch": branch,
        "changed_paths": unique_paths[:maximum_paths],
        "truncated": len(unique_paths) > maximum_paths,
    }


def _freeze_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(sorted(value.items())))


@dataclass(frozen=True)
class ResolvedCommand:
    template_id: str
    capability_id: str
    capability_version: int
    capability_digest: str
    executable_identity: Mapping[str, Any]
    argv: tuple[str, ...]
    working_directory: str
    environment: Mapping[str, str]
    stdin_mode: str
    shell: bool
    template_digest: str
    command_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RESOLVED_COMMAND_FACTORY_SEAL:
            _fail("resolved_command_factory_required")
        if self.template_id not in _TEMPLATE_CAPABILITIES:
            _fail("tool_command_template_unknown")
        if _TEMPLATE_CAPABILITIES[self.template_id] != self.capability_id:
            _fail("tool_command_capability_mismatch")
        if (
            isinstance(self.capability_version, bool)
            or not isinstance(self.capability_version, int)
            or self.capability_version < 1
        ):
            _fail("tool_command_capability_mismatch")
        identity = json.loads(canonical_json(self.executable_identity))
        if not isinstance(identity, dict) or set(identity) != {
            "absolute_path",
            "device",
            "inode",
            "size",
            "sha256",
        }:
            _fail("tool_command_executable_untrusted")
        object.__setattr__(self, "executable_identity", MappingProxyType(identity))
        argv = tuple(self.argv)
        if not argv or argv[0] != identity["absolute_path"]:
            _fail("tool_command_executable_untrusted")
        if any(not isinstance(item, str) or not item or len(item) > 4096 for item in argv):
            _fail("tool_command_argument_denied")
        object.__setattr__(self, "argv", argv)
        if not isinstance(self.working_directory, str) or not Path(
            self.working_directory
        ).is_absolute():
            _fail("tool_command_working_directory_denied")
        environment = dict(self.environment)
        if set(environment) != set(_minimal_environment()):
            _fail("tool_command_environment_denied")
        object.__setattr__(self, "environment", _freeze_mapping(environment))
        if self.stdin_mode != "disabled" or self.shell is not False:
            _fail("tool_command_shell_denied")
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.template_digest) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.capability_digest) is None
        ):
            _fail("tool_command_template_drift")
        expected = canonical_digest(self.identity_payload())
        if self.command_digest and self.command_digest != expected:
            _fail("resolved_command_digest_mismatch")
        object.__setattr__(self, "command_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "command_policy_version": COMMAND_POLICY_VERSION,
            "template_id": self.template_id,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "capability_digest": self.capability_digest,
            "executable_identity": dict(self.executable_identity),
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "environment": dict(self.environment),
            "stdin_mode": self.stdin_mode,
            "shell": self.shell,
            "template_digest": self.template_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "command_digest": self.command_digest}


class CommandTemplateRegistry:
    def __init__(
        self,
        *,
        python_executable: str | os.PathLike[str] | None = None,
        git_executable: str | os.PathLike[str] | None = None,
    ) -> None:
        # Kept only as a source-compatible argument.  v10-D has no process
        # sandbox capable of proving arbitrary Python imports read-only, so no
        # Python template is registered or executable.
        _ = python_executable
        git_value = git_executable or shutil.which("git")
        if git_value is None:
            _fail("tool_command_executable_untrusted")
        self._git = _executable_identity(git_value)
        self._template_digests = {
            template_id: canonical_digest(
                {
                    "command_policy_version": COMMAND_POLICY_VERSION,
                    "template_id": template_id,
                    "capability_id": capability_id,
                    "executable_identity": self._git,
                    "git_safe_prefix": list(_git_safe_prefix()),
                }
            )
            for template_id, capability_id in _TEMPLATE_CAPABILITIES.items()
        }
        self.registry_digest = canonical_digest(
            {
                "command_policy_version": COMMAND_POLICY_VERSION,
                "templates": self._template_digests,
            }
        )

    @property
    def template_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._template_digests))

    def template_digest(self, template_id: str) -> str:
        try:
            return self._template_digests[template_id]
        except KeyError:
            _fail("tool_command_template_unknown")

    @staticmethod
    def _paths(bindings: Sequence[ResolvedProjectPath]) -> list[str]:
        if isinstance(bindings, (str, bytes)):
            _fail("tool_command_argument_denied")
        paths = [item.canonical_relative_path for item in bindings]
        if len(paths) != len(set(path.casefold() for path in paths)):
            _fail("tool_command_argument_denied")
        return paths

    def resolve(
        self,
        *,
        template_id: str,
        profile: CapabilityProfile,
        typed_arguments: Mapping[str, Any],
        path_bindings: Sequence[ResolvedProjectPath],
        project_root: str,
    ) -> ResolvedCommand:
        if not isinstance(profile, CapabilityProfile):
            _fail("tool_command_capability_mismatch")
        if template_id in _UNAVAILABLE_PYTHON_TEMPLATES:
            _fail("tool_command_sandbox_unavailable")
        expected_capability = _TEMPLATE_CAPABILITIES.get(template_id)
        if expected_capability is None:
            _fail("tool_command_template_unknown")
        if expected_capability != profile.capability_id:
            _fail("tool_command_capability_mismatch")
        arguments = profile.validate_input(typed_arguments)
        paths = self._paths(path_bindings)
        executable = self._git
        argv: list[str] = [executable["absolute_path"], *_git_safe_prefix()]
        if template_id == "git.status.porcelain-v2.v1":
            if arguments:
                _fail("tool_command_argument_denied")
            argv.extend(("status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"))
        elif template_id == "git.log.bounded.v1":
            if paths != list(arguments["relative_paths"]):
                _fail("tool_command_argument_denied")
            maximum = arguments["maximum_commits"]
            argv.extend(("log", "--no-decorate", "--format=%H%x09%aI%x09%s", "-n", str(maximum), "--"))
            argv.extend(paths)
        elif template_id == "git.show.bounded.v1":
            if paths != list(arguments["relative_paths"]):
                _fail("tool_command_argument_denied")
            revision = _safe_argument(arguments["revision"], "tool_command_argument_denied")
            if _REVISION_RE.fullmatch(revision) is None:
                _fail("tool_command_argument_denied")
            argv.extend(("show", "--no-ext-diff", "--no-textconv", revision, "--"))
            argv.extend(paths)
        elif template_id == "git.diff.bounded.v1":
            if paths != list(arguments["relative_paths"]):
                _fail("tool_command_argument_denied")
            argv.extend(("diff", "--no-ext-diff", "--no-textconv", "--"))
            argv.extend(paths)
        elif template_id == "git.diff.check.v1":
            if arguments["template_id"] != "git.diff.check" or paths != list(
                arguments["relative_paths"]
            ):
                _fail("tool_command_argument_denied")
            argv.extend(("diff", "--check", "--", *paths))
        else:
            _fail("tool_command_template_unknown")
        template_digest = self.template_digest(template_id)
        return ResolvedCommand(
            template_id=template_id,
            capability_id=profile.capability_id,
            capability_version=profile.version,
            capability_digest=profile.capability_digest,
            executable_identity=executable,
            argv=tuple(argv),
            working_directory=str(Path(project_root).resolve(strict=True)),
            environment=_minimal_environment(),
            stdin_mode="disabled",
            shell=False,
            template_digest=template_digest,
            _factory_seal=_RESOLVED_COMMAND_FACTORY_SEAL,
        )

    def revalidate(self, command: ResolvedCommand) -> None:
        if not isinstance(command, ResolvedCommand):
            _fail("invalid_resolved_command")
        if (
            command.template_id not in self._template_digests
            or command.template_digest
            != self._template_digests[command.template_id]
        ):
            _fail("tool_command_template_drift")
        current = _executable_identity(command.executable_identity["absolute_path"])
        if current != dict(command.executable_identity):
            _fail("tool_command_executable_untrusted")
        if canonical_digest(command.identity_payload()) != command.command_digest:
            _fail("resolved_command_digest_mismatch")


__all__ = [
    "COMMAND_POLICY_VERSION",
    "CognitiveCommandPolicyError",
    "CommandTemplateRegistry",
    "ResolvedCommand",
    "parse_git_status_porcelain_v2",
]
