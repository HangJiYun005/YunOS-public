from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import InitVar, dataclass
from pathlib import Path
from typing import Any, Sequence

from memory.db_path_policy import is_reparse_point
from memory.operational_state_models import canonical_digest


PATH_POLICY_VERSION = 1
ACCESS_MODES = (
    "read_file",
    "read_directory",
    "read_optional",
    "write_file",
    "create_file",
)

_RESOLVED_PATH_FACTORY_SEAL = object()
_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:")
_RESERVED_BASE_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])$",
    flags=re.IGNORECASE,
)
_INVALID_COMPONENT_CHARS = frozenset('<>"|?*')
_MAX_PATH_CHARS = 1024
_MAX_COMPONENT_CHARS = 240
_DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024


class CognitivePathPolicyError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CognitivePathPolicyError(reason_code)


def _identity(path: Path, *, directory: bool | None = None) -> tuple[int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        _fail("tool_path_resolution_unverifiable")
    if value.st_dev <= 0 or value.st_ino <= 0:
        _fail("tool_path_resolution_unverifiable")
    if directory is True and not stat.S_ISDIR(value.st_mode):
        _fail("tool_path_type_mismatch")
    if directory is False and not stat.S_ISREG(value.st_mode):
        _fail("tool_path_type_mismatch")
    return (int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_nlink))


def _hash_file(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    try:
        size = path.stat(follow_symlinks=False).st_size
    except OSError:
        _fail("tool_path_resolution_unverifiable")
    if size < 0 or size > maximum_bytes:
        _fail("tool_file_size_limit_exceeded")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    _fail("tool_file_size_limit_exceeded")
                digest.update(chunk)
    except CognitivePathPolicyError:
        raise
    except OSError:
        _fail("tool_path_resolution_unverifiable")
    if total != size:
        _fail("tool_path_identity_changed")
    return digest.hexdigest(), total


def _long_path_name(path: Path) -> Path:
    if os.name != "nt" or not path.exists():
        return path
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetLongPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_uint
        raw = str(path)
        needed = function(raw, None, 0)
        if needed == 0:
            return path
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = function(raw, buffer, len(buffer))
        if written == 0 or written >= len(buffer):
            return path
        return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        return path


def _canonical_components(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        _fail("tool_path_syntax_denied")
    if not relative_path or len(relative_path) > _MAX_PATH_CHARS:
        _fail("tool_path_syntax_denied")
    if unicodedata.normalize("NFC", relative_path) != relative_path:
        _fail("tool_path_unicode_normalization_denied")
    if any(ord(char) < 32 or ord(char) == 127 for char in relative_path):
        _fail("tool_path_syntax_denied")
    lowered = relative_path.casefold()
    if relative_path.startswith(("\\", "/")) or lowered.startswith(
        ("\\\\?\\", "\\\\.\\", "\\??\\", "//?/", "//./")
    ):
        _fail("tool_path_namespace_denied")
    if _DRIVE_RELATIVE_RE.match(relative_path) or ":" in relative_path:
        _fail("tool_path_namespace_denied")
    normalized = relative_path.replace("\\", "/")
    raw_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        _fail("tool_path_syntax_denied")
    result: list[str] = []
    for part in raw_parts:
        if not part or len(part) > _MAX_COMPONENT_CHARS:
            _fail("tool_path_syntax_denied")
        if part.rstrip(" .") != part:
            _fail("tool_path_windows_alias_denied")
        if any(char in _INVALID_COMPONENT_CHARS for char in part):
            _fail("tool_path_syntax_denied")
        if part.casefold() == ".git":
            _fail("tool_git_control_path_denied")
        base = part.split(".", 1)[0].rstrip(" .")
        if _RESERVED_BASE_RE.fullmatch(base):
            _fail("tool_path_reserved_name_denied")
        result.append(part)
    return tuple(result)


def _normalize_project_root(project_root: str | os.PathLike[str]) -> Path:
    raw = str(project_root)
    if (
        not _DRIVE_ABSOLUTE_RE.match(raw)
        or raw.startswith(("\\\\", "//", "\\?\\", "\\.\\"))
        or ":" in raw[2:]
    ):
        _fail("tool_project_root_invalid")
    requested = Path(os.path.abspath(raw))
    try:
        linked_root = requested.is_symlink() or is_reparse_point(requested)
    except (OSError, RuntimeError, ValueError):
        _fail("tool_project_root_unavailable")
    if linked_root:
        _fail("tool_path_reparse_denied")
    try:
        root = requested.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        _fail("tool_project_root_unavailable")
    if not root.is_dir():
        _fail("tool_project_root_invalid")
    if is_reparse_point(root) or root.is_symlink():
        _fail("tool_path_reparse_denied")
    _identity(root, directory=True)
    return root


def _within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(candidate)), os.path.normcase(str(root)))
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


def _reject_reparse_components(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        _fail("tool_path_outside_project_root")
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if current.is_symlink() or is_reparse_point(current):
            _fail("tool_path_reparse_denied")


@dataclass(frozen=True)
class ResolvedProjectPath:
    project_root: str
    canonical_relative_path: str
    absolute_path: str
    access_mode: str
    target_exists: bool
    root_identity: tuple[int, int, int, int]
    parent_identity: tuple[int, int, int, int]
    target_identity: tuple[int, int, int, int] | None
    target_sha256: str | None
    target_size: int | None
    binding_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RESOLVED_PATH_FACTORY_SEAL:
            _fail("resolved_project_path_factory_required")
        if self.access_mode not in ACCESS_MODES:
            _fail("invalid_resolved_path_access_mode")
        if not isinstance(self.target_exists, bool):
            _fail("invalid_resolved_path_existence")
        if self.target_exists != (self.target_identity is not None):
            _fail("invalid_resolved_path_identity")
        if (self.target_sha256 is None) != (self.target_size is None):
            _fail("invalid_resolved_path_hash")
        if self.target_sha256 is not None and (
            len(self.target_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.target_sha256)
            or self.target_size is None
            or self.target_size < 0
        ):
            _fail("invalid_resolved_path_hash")
        expected = canonical_digest(self.identity_payload())
        if self.binding_digest and self.binding_digest != expected:
            _fail("resolved_project_path_digest_mismatch")
        object.__setattr__(self, "binding_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "path_policy_version": PATH_POLICY_VERSION,
            "project_root": self.project_root,
            "canonical_relative_path": self.canonical_relative_path,
            "absolute_path": self.absolute_path,
            "access_mode": self.access_mode,
            "target_exists": self.target_exists,
            "root_identity": list(self.root_identity),
            "parent_identity": list(self.parent_identity),
            "target_identity": (
                None if self.target_identity is None else list(self.target_identity)
            ),
            "target_sha256": self.target_sha256,
            "target_size": self.target_size,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "binding_digest": self.binding_digest}


def resolve_project_path(
    *,
    project_root: str | os.PathLike[str],
    relative_path: str,
    access_mode: str,
    maximum_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> ResolvedProjectPath:
    if access_mode not in ACCESS_MODES:
        _fail("invalid_resolved_path_access_mode")
    if (
        isinstance(maximum_file_bytes, bool)
        or not isinstance(maximum_file_bytes, int)
        or not 1 <= maximum_file_bytes <= 104_857_600
    ):
        _fail("invalid_tool_file_size_limit")
    root = _normalize_project_root(project_root)
    parts = _canonical_components(relative_path)
    candidate = Path(os.path.abspath(str(root.joinpath(*parts))))
    if not _within(candidate, root):
        _fail("tool_path_outside_project_root")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        _fail("tool_path_parent_unavailable")
    _reject_reparse_components(root, parent)
    if parent.is_symlink() or is_reparse_point(parent):
        _fail("tool_path_reparse_denied")
    long_parent = _long_path_name(parent)
    try:
        long_relative_parent = long_parent.relative_to(_long_path_name(root))
    except ValueError:
        _fail("tool_path_outside_project_root")
    expected_parent = Path(*parts[:-1]) if parts[:-1] else Path()
    if str(long_relative_parent).casefold() != str(expected_parent).casefold():
        _fail("tool_path_windows_alias_denied")

    exists = candidate.exists()
    if access_mode in {"read_file", "write_file", "read_directory"} and not exists:
        _fail("tool_path_target_missing")
    if access_mode == "create_file" and exists:
        _fail("tool_create_target_exists")
    if exists:
        if candidate.is_symlink() or is_reparse_point(candidate):
            _fail("tool_path_reparse_denied")
        long_candidate = _long_path_name(candidate)
        try:
            long_relative = long_candidate.relative_to(_long_path_name(root))
        except ValueError:
            _fail("tool_path_outside_project_root")
        if str(long_relative).replace("\\", "/").casefold() != "/".join(
            parts
        ).casefold():
            _fail("tool_path_windows_alias_denied")

    target_identity: tuple[int, int, int, int] | None = None
    target_sha256: str | None = None
    target_size: int | None = None
    if exists:
        directory = access_mode == "read_directory"
        target_identity = _identity(candidate, directory=directory)
        if access_mode != "read_directory":
            if access_mode == "write_file" and target_identity[3] != 1:
                _fail("tool_path_hardlink_write_denied")
            target_sha256, target_size = _hash_file(
                candidate,
                maximum_bytes=maximum_file_bytes,
            )
    elif access_mode != "read_optional" and access_mode != "create_file":
        _fail("tool_path_target_missing")

    canonical_relative = "/".join(parts)
    return ResolvedProjectPath(
        project_root=str(root),
        canonical_relative_path=canonical_relative,
        absolute_path=str(candidate),
        access_mode=access_mode,
        target_exists=exists,
        root_identity=_identity(root, directory=True),
        parent_identity=_identity(parent, directory=True),
        target_identity=target_identity,
        target_sha256=target_sha256,
        target_size=target_size,
        _factory_seal=_RESOLVED_PATH_FACTORY_SEAL,
    )


def revalidate_resolved_path(
    binding: ResolvedProjectPath,
    *,
    maximum_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> ResolvedProjectPath:
    if not isinstance(binding, ResolvedProjectPath):
        _fail("invalid_resolved_project_path")
    refreshed = resolve_project_path(
        project_root=binding.project_root,
        relative_path=binding.canonical_relative_path,
        access_mode=binding.access_mode,
        maximum_file_bytes=maximum_file_bytes,
    )
    if refreshed.binding_digest != binding.binding_digest:
        _fail("tool_path_identity_changed")
    return refreshed


def path_is_within_roots(path: str, roots: Sequence[str]) -> bool:
    if not isinstance(path, str) or isinstance(roots, (str, bytes)):
        return False
    candidate = Path(path)
    for root_value in roots:
        try:
            root = Path(root_value).resolve(strict=False)
            if _within(candidate, root):
                return True
        except (OSError, RuntimeError, ValueError, TypeError):
            continue
    return False


__all__ = [
    "ACCESS_MODES",
    "CognitivePathPolicyError",
    "PATH_POLICY_VERSION",
    "ResolvedProjectPath",
    "path_is_within_roots",
    "resolve_project_path",
    "revalidate_resolved_path",
]
