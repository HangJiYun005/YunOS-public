"""Run the v10-F restored tree as an isolated, readonly web acceptance.

The production CLI has exactly two caller-controlled values: an already
materialized recovery root below ``E:\\AyunOS\\output`` and an isolated port.
Code is copied only from the clean current HEAD of the fixed source tree.  The
child interpreter imports that copied code, so every code-defined production
database identity resolves below the restored tree rather than the live source
tree.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib import error, parse, request
import uuid


SOURCE_ROOT = Path(r"E:\AyunOS")
OUTPUT_ROOT = SOURCE_ROOT / "output"
PYTHON_EXE = SOURCE_ROOT / ".venv" / "Scripts" / "python.exe"
PRODUCTION_WEB_PORTS = frozenset({8765, 8766})
CONTRACT_VERSION = "yunos-v10-f-isolated-restore-web-acceptance-1"
RECOVERY_CONTRACT_VERSION = "yunos-v10-release-recovery-1"
EVIDENCE_NAME = "v10_f_isolated_restore_web_acceptance.json"
_OWNER_MARKER = ".yunos-v10-recovery-owned.json"
_OWNERSHIP_MARKER_VERSION = "yunos-v10-recovery-ownership-marker-1"
_REPARSE_ATTRIBUTE = 0x400
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_HTTP_BYTES = 4 * 1024 * 1024
_MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
_POSIX_SERVER_BROKER = r"""
import signal
import subprocess
import sys
import time

def hold_process_group_identity(_signum, _frame):
    return None

signal.signal(signal.SIGTERM, hold_process_group_identity)
child = None
try:
    child = subprocess.Popen(
        sys.argv[1:],
        stdin=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
    )
except BaseException:
    sys.stderr.write("isolated server broker child start failed\n")
    sys.stderr.flush()
while True:
    if child is not None and child.poll() is not None:
        sys.stderr.write("isolated server broker child exited\n")
        sys.stderr.flush()
        child = None
    time.sleep(0.2)
"""
_FIXED_DATABASES = {
    "main": Path("data/ayunos_chatgpt.db"),
    "profile": Path("data/profile/approved_profile.db"),
    "semantic": Path("data/semantic/ayunos_semantic.db"),
    "runtime": Path("data/runtime/ayunos_runtime.db"),
    "operational": Path("data/operational/ayunos_operational_state.db"),
}
_REQUIRED_TRACKED = frozenset(
    {
        "memory/__init__.py",
        "tools/local_web.py",
        "tools/optimize_whale_context.py",
        "whale_chat.py",
        "web/app.js",
        "web/index.html",
        "web/manifest.webmanifest",
        "web/service-worker.js",
        "web/style.css",
        "config/persona_whale.md",
        "config/projects_public.json",
    }
)
_ALLOWED_WEB_SUFFIXES = frozenset(
    {".css", ".html", ".js", ".json", ".md", ".png", ".svg", ".webmanifest"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_RECOVERY_MANIFEST_FIELDS = frozenset(
    {
        "contract_version",
        "release_name",
        "phase",
        "created_at",
        "method",
        "database_count",
        "production_databases_opened_read_only",
        "production_write_performed",
        "database_results",
        "assets",
        "release_reports",
        "source_provenance",
        "service_quiescence",
        "schema_manifest_relative_path",
        "logical_summaries_relative_path",
        "provider_template_relative_path",
        "recovery_guide_relative_path",
        "artifact_inventory",
        "restore_rehearsal",
        "ownership_marker_identity_digest",
        "backup_set_digest",
        "elapsed_seconds",
        "manifest_digest",
    }
)
_RESTORE_RUNTIME_FIELDS = frozenset(
    {
        "contract_version",
        "phase",
        "mode",
        "requested_loopback_origin",
        "production_database_connected",
        "write_routes_enabled",
        "restored_database_paths",
        "asset_path",
        "provider_template_path",
        "release_reports_path",
        "ready_for_isolated_readonly_start",
        "ownership_marker_identity_digest",
        "source_service_quiescence_digest",
        "runtime_digest",
    }
)
_SEALED_MARKER_FIELDS = frozenset(
    {
        "marker_schema_version",
        "contract_version",
        "purpose",
        "state",
        "nonce",
        "phase",
        "manifest_digest",
        "backup_set_digest",
        "ownership_identity_digest",
        "runtime_digest",
        "marker_digest",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|authorization|credential|"
    r"password|private[_-]?key|secret|token[_-]?value)(?:$|[_-])"
)
_SECRET_LITERAL = (
    re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/=-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)
_SAFE_SECRET_METADATA_KEYS = frozenset(
    {
        "secret_material_included",
        "secret_references_included",
        "requires_local_secret_configuration",
    }
)


class IsolatedRestoreAcceptanceError(RuntimeError):
    """A restore, source, process, or readonly boundary was not proven."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        raise IsolatedRestoreAcceptanceError("path_identity_unverifiable") from exc
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _reject_reparse(path: Path) -> None:
    if _is_reparse(path):
        raise IsolatedRestoreAcceptanceError("reparse_path_forbidden")


def _reject_hardlink(path: Path) -> None:
    try:
        if path.is_file() and int(path.stat().st_nlink) != 1:
            raise IsolatedRestoreAcceptanceError("hardlink_path_forbidden")
    except OSError as exc:
        raise IsolatedRestoreAcceptanceError("path_identity_unverifiable") from exc


def _validate_absolute_shape(path: Path) -> None:
    raw = os.fspath(path)
    windows = raw.replace("/", "\\")
    if (
        not path.is_absolute()
        or not raw
        or "\x00" in raw
        or windows.startswith("\\\\")
        or windows.startswith("\\?\\")
        or windows.startswith("\\.\\")
        or ".." in path.parts
    ):
        raise IsolatedRestoreAcceptanceError("unsafe_path_shape")
    for part in path.parts[1:]:
        if ":" in part:
            raise IsolatedRestoreAcceptanceError("alternate_data_stream_forbidden")


def _safe_existing_directory(path: Path) -> Path:
    _validate_absolute_shape(path)
    lexical = Path(os.path.abspath(os.fspath(path)))
    if not lexical.is_dir():
        raise IsolatedRestoreAcceptanceError("required_directory_missing")
    current = lexical
    while True:
        _reject_reparse(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    resolved = lexical.resolve(strict=True)
    if not resolved.is_dir():
        raise IsolatedRestoreAcceptanceError("required_directory_missing")
    return resolved


def _safe_descendant(root: Path, relative: Path, *, directory: bool = False) -> Path:
    if (
        relative.is_absolute()
        or relative.drive
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise IsolatedRestoreAcceptanceError("unsafe_relative_path")
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            raise IsolatedRestoreAcceptanceError("required_path_missing")
        _reject_reparse(current)
    resolved = current.resolve(strict=True)
    if not _is_within(resolved, root):
        raise IsolatedRestoreAcceptanceError("path_escape_detected")
    if directory:
        if not resolved.is_dir():
            raise IsolatedRestoreAcceptanceError("required_directory_missing")
    else:
        if not resolved.is_file():
            raise IsolatedRestoreAcceptanceError("required_file_missing")
        _reject_hardlink(resolved)
    return resolved


def _safe_destination_parent(root: Path, relative: Path) -> tuple[Path, tuple[int, int]]:
    current = root
    for part in relative.parent.parts:
        if part in {"", ".", ".."} or ":" in part:
            raise IsolatedRestoreAcceptanceError("unsafe_relative_path")
        current = current / part
        if current.exists() or current.is_symlink():
            _reject_reparse(current)
            if not current.is_dir():
                raise IsolatedRestoreAcceptanceError("runtime_source_parent_invalid")
        else:
            current.mkdir()
            _reject_reparse(current)
        resolved = current.resolve(strict=True)
        if not _is_within(resolved, root):
            raise IsolatedRestoreAcceptanceError("path_escape_detected")
        current = resolved
    details = current.stat()
    return current, (int(details.st_dev), int(details.st_ino))


def _audit_alias_free_tree(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    directory_count = 0
    for current_name, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_name)
        _reject_reparse(current)
        directory_count += 1
        for name in tuple(directory_names):
            child = current / name
            _reject_reparse(child)
            if not child.is_dir():
                raise IsolatedRestoreAcceptanceError("tree_directory_invalid")
        for name in file_names:
            child = current / name
            _reject_reparse(child)
            if not child.is_file():
                raise IsolatedRestoreAcceptanceError("tree_file_invalid")
            _reject_hardlink(child)
            files.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "size": int(child.stat().st_size),
                }
            )
    files.sort(key=lambda item: item["path"])
    return {
        "directory_count": directory_count,
        "file_count": len(files),
        "shape_digest": _canonical_digest(files),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "size": 0, "sha256": None}
    _reject_reparse(path)
    if not path.is_file():
        raise IsolatedRestoreAcceptanceError("file_gate_target_invalid")
    _reject_hardlink(path)
    before = path.stat()
    result = {
        "exists": True,
        "size": int(before.st_size),
        "sha256": _hash_file(path),
    }
    after = path.stat()
    if (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_nlink),
        int(before.st_size),
        int(before.st_mtime_ns),
    ) != (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_nlink),
        int(after.st_size),
        int(after.st_mtime_ns),
    ):
        raise IsolatedRestoreAcceptanceError("file_changed_during_hash")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    gate = _file_gate(path)
    if not gate["exists"] or gate["size"] > _MAX_JSON_BYTES:
        raise IsolatedRestoreAcceptanceError("json_file_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedRestoreAcceptanceError("json_file_invalid") from exc
    if not isinstance(value, dict):
        raise IsolatedRestoreAcceptanceError("json_object_required")
    return value


def _without(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {item_key: item for item_key, item in value.items() if item_key != key}


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (
                _SENSITIVE_KEY.search(str(key)) is not None
                and str(key) not in _SAFE_SECRET_METADATA_KEYS
            )
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_LITERAL)
    return False


def _validate_port(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1024 <= value <= 65535
        or value in PRODUCTION_WEB_PORTS
    ):
        raise IsolatedRestoreAcceptanceError("independent_restore_port_required")
    return value


@dataclass(frozen=True)
class RestoreContract:
    root: Path
    phase: str
    runtime: dict[str, Any]
    ownership_marker: dict[str, Any]
    manifest: dict[str, Any]
    database_paths: dict[str, Path]
    manifest_digest: str
    backup_set_digest: str


def _validate_restore_contract(
    restore_root: Path,
    port: int,
    *,
    output_root: Path,
) -> RestoreContract:
    selected_port = _validate_port(port)
    safe_output = _safe_existing_directory(output_root)
    root = _safe_existing_directory(restore_root)
    if root == safe_output or not _is_within(root, safe_output):
        raise IsolatedRestoreAcceptanceError("restore_root_must_be_below_output")
    _audit_alias_free_tree(root)
    runtime_path = _safe_descendant(root, Path("restore_runtime.json"))
    marker_path = _safe_descendant(root, Path(_OWNER_MARKER))
    manifest_path = _safe_descendant(
        root, Path("recovery_metadata/release_manifest.json")
    )
    runtime = _read_json(runtime_path)
    marker = _read_json(marker_path)
    manifest = _read_json(manifest_path)
    if set(marker) != _SEALED_MARKER_FIELDS:
        raise IsolatedRestoreAcceptanceError("ownership_marker_shape_invalid")
    if _contains_secret(marker):
        raise IsolatedRestoreAcceptanceError("ownership_marker_secret_detected")
    nonce = marker.get("nonce")
    identity_basis = {
        "marker_schema_version": _OWNERSHIP_MARKER_VERSION,
        "contract_version": RECOVERY_CONTRACT_VERSION,
        "purpose": "restore",
        "nonce": nonce,
    }
    expected_identity = _canonical_digest(identity_basis)
    if (
        marker.get("marker_schema_version") != _OWNERSHIP_MARKER_VERSION
        or marker.get("contract_version") != RECOVERY_CONTRACT_VERSION
        or marker.get("purpose") != "restore"
        or marker.get("state") != "sealed"
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or marker.get("phase") != "final"
        or marker.get("ownership_identity_digest") != expected_identity
        or marker.get("marker_digest")
        != _canonical_digest(_without(marker, "marker_digest"))
    ):
        raise IsolatedRestoreAcceptanceError("ownership_marker_invalid")
    if (
        set(runtime) != _RESTORE_RUNTIME_FIELDS
        or runtime.get("contract_version") != RECOVERY_CONTRACT_VERSION
    ):
        raise IsolatedRestoreAcceptanceError("restore_runtime_contract_mismatch")
    if runtime.get("runtime_digest") != _canonical_digest(
        _without(runtime, "runtime_digest")
    ):
        raise IsolatedRestoreAcceptanceError("restore_runtime_digest_mismatch")
    expected_origin = f"http://127.0.0.1:{selected_port}"
    if (
        runtime.get("phase") != "final"
        or runtime.get("mode") != "real-readonly"
        or runtime.get("production_database_connected") is not False
        or runtime.get("write_routes_enabled") is not False
        or runtime.get("ready_for_isolated_readonly_start") is not True
        or runtime.get("requested_loopback_origin") != expected_origin
        or runtime.get("restored_database_paths")
        != {key: value.as_posix() for key, value in _FIXED_DATABASES.items()}
        or runtime.get("asset_path") != "data/ingestion_assets"
        or runtime.get("provider_template_path")
        != "recovery_metadata/provider_configuration_template.json"
        or runtime.get("release_reports_path") != "recovery_metadata/release_reports"
        or runtime.get("ownership_marker_identity_digest") != expected_identity
        or not _SHA256.fullmatch(
            str(runtime.get("source_service_quiescence_digest") or "")
        )
    ):
        raise IsolatedRestoreAcceptanceError("restore_runtime_boundary_invalid")
    if (
        set(manifest) != _RECOVERY_MANIFEST_FIELDS
        or manifest.get("contract_version") != RECOVERY_CONTRACT_VERSION
        or manifest.get("phase") != runtime.get("phase")
        or manifest.get("release_name")
        != "YunOS v10.0 — Bounded Cognitive Agency"
        or manifest.get("method")
        != "sqlite_online_backup_plus_verified_asset_copy"
        or manifest.get("database_count") != 5
        or manifest.get("production_databases_opened_read_only") is not True
        or manifest.get("production_write_performed") is not False
        or manifest.get("schema_manifest_relative_path") != "schema_manifest.json"
        or manifest.get("logical_summaries_relative_path") != "logical_summaries.json"
        or manifest.get("provider_template_relative_path")
        != "provider_configuration_template.json"
        or manifest.get("recovery_guide_relative_path") != "RECOVERY.md"
        # The immutable package manifest retains the package marker identity.
        # A restored tree has a new, independently owned restore marker whose
        # identity is carried by restore_runtime.json.  The restore marker binds
        # both identities safely through the verified manifest digest and
        # backup-set digest; requiring the package and restore identities to be
        # equal makes every real restore impossible while fixture-only tests pass.
        or not _SHA256.fullmatch(
            str(manifest.get("ownership_marker_identity_digest") or "")
        )
        or manifest.get("ownership_marker_identity_digest") == expected_identity
        or not _SHA256.fullmatch(str(manifest.get("backup_set_digest") or ""))
        or manifest.get("manifest_digest")
        != _canonical_digest(_without(manifest, "manifest_digest"))
    ):
        raise IsolatedRestoreAcceptanceError("recovery_manifest_invalid")
    if (
        marker.get("manifest_digest") != manifest.get("manifest_digest")
        or marker.get("backup_set_digest") != manifest.get("backup_set_digest")
        or marker.get("runtime_digest") != runtime.get("runtime_digest")
    ):
        raise IsolatedRestoreAcceptanceError("ownership_marker_binding_mismatch")
    provider_template = _read_json(
        _safe_descendant(
            root,
            Path("recovery_metadata/provider_configuration_template.json"),
        )
    )
    if (
        provider_template.get("template_digest")
        != _canonical_digest(_without(provider_template, "template_digest"))
        or provider_template.get("secret_material_included") is not False
        or provider_template.get("secret_references_included") is not False
        or _contains_secret(provider_template)
    ):
        raise IsolatedRestoreAcceptanceError("provider_template_secret_boundary_invalid")
    database_results = manifest.get("database_results")
    if not isinstance(database_results, dict) or set(database_results) != set(
        _FIXED_DATABASES
    ):
        raise IsolatedRestoreAcceptanceError("recovery_database_manifest_invalid")
    database_paths: dict[str, Path] = {}
    for label, relative in _FIXED_DATABASES.items():
        path = _safe_descendant(root, relative)
        result = database_results.get(label)
        expected_gate = (
            result.get("backup_audit", {}).get("file_gate")
            if isinstance(result, dict)
            else None
        )
        if expected_gate != _file_gate(path):
            raise IsolatedRestoreAcceptanceError("restored_database_manifest_mismatch")
        database_paths[label] = path
    evidence_path = root / EVIDENCE_NAME
    if evidence_path.exists() or evidence_path.is_symlink():
        raise IsolatedRestoreAcceptanceError("isolated_acceptance_already_recorded")
    return RestoreContract(
        root=root,
        phase=str(runtime["phase"]),
        runtime=runtime,
        ownership_marker=marker,
        manifest=manifest,
        database_paths=database_paths,
        manifest_digest=str(manifest["manifest_digest"]),
        backup_set_digest=str(manifest["backup_set_digest"]),
    )


@dataclass(frozen=True)
class GitState:
    head: str
    status: bytes
    tracked_paths: tuple[str, ...]


def _run_git(source_root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolatedRestoreAcceptanceError("git_inspection_failed") from exc


def _capture_git_state(source_root: Path) -> GitState:
    top = _run_git(source_root, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        raise IsolatedRestoreAcceptanceError("git_source_root_unavailable")
    try:
        reported_root = Path(top.stdout.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise IsolatedRestoreAcceptanceError("git_source_root_unavailable") from exc
    if _path_key(reported_root) != _path_key(source_root):
        raise IsolatedRestoreAcceptanceError("git_source_root_mismatch")
    head_result = _run_git(source_root, ["rev-parse", "--verify", "HEAD"])
    status_result = _run_git(
        source_root,
        ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
    )
    paths_result = _run_git(
        source_root,
        [
            "ls-files",
            "-z",
            "--",
            "memory/**",
            "tools/local_web.py",
            "tools/optimize_whale_context.py",
            "whale_chat.py",
            "web/**",
            "config/persona_whale.md",
            "config/projects_public.json",
        ],
    )
    if any(result.returncode != 0 for result in (head_result, status_result, paths_result)):
        raise IsolatedRestoreAcceptanceError("git_inspection_failed")
    try:
        head = head_result.stdout.decode("ascii").strip().lower()
        paths = tuple(
            item.decode("utf-8")
            for item in paths_result.stdout.split(b"\0")
            if item
        )
    except UnicodeError as exc:
        raise IsolatedRestoreAcceptanceError("git_inspection_encoding_invalid") from exc
    if not _GIT_OBJECT.fullmatch(head):
        raise IsolatedRestoreAcceptanceError("git_head_invalid")
    return GitState(head=head, status=status_result.stdout, tracked_paths=paths)


def _allowed_tracked_path(raw: str) -> bool:
    posix = PurePosixPath(raw)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or not posix.parts
        or any(part in {"", ".", ".."} or ":" in part for part in posix.parts)
    ):
        return False
    normalized = posix.as_posix()
    if normalized.startswith("memory/"):
        return posix.suffix.lower() in {".py", ".sql"}
    if normalized.startswith("web/"):
        return posix.suffix.lower() in _ALLOWED_WEB_SUFFIXES
    return normalized in {
        "tools/local_web.py",
        "tools/optimize_whale_context.py",
        "whale_chat.py",
        "config/persona_whale.md",
        "config/projects_public.json",
    }


def _copy_clean_head_snapshot(
    contract: RestoreContract,
    *,
    source_root: Path,
    state_provider: Callable[[Path], GitState] = _capture_git_state,
) -> dict[str, Any]:
    source = _safe_existing_directory(source_root)
    initial = state_provider(source)
    if initial.status:
        raise IsolatedRestoreAcceptanceError("source_worktree_not_clean")
    if len(initial.tracked_paths) != len(set(path.casefold() for path in initial.tracked_paths)):
        raise IsolatedRestoreAcceptanceError("tracked_path_case_collision")
    if not _REQUIRED_TRACKED.issubset(initial.tracked_paths):
        raise IsolatedRestoreAcceptanceError("required_runtime_source_not_tracked")
    if not initial.tracked_paths or any(
        not _allowed_tracked_path(path) for path in initial.tracked_paths
    ):
        raise IsolatedRestoreAcceptanceError("runtime_source_allowlist_violation")
    top_level_targets = {
        PurePosixPath(path).parts[0] for path in initial.tracked_paths
    }
    for name in top_level_targets:
        target = contract.root / name
        if target.exists() or target.is_symlink():
            raise IsolatedRestoreAcceptanceError("runtime_source_target_exists")
    copied: list[dict[str, Any]] = []
    for raw in sorted(initial.tracked_paths):
        relative = Path(*PurePosixPath(raw).parts)
        source_path = _safe_descendant(source, relative)
        before = _file_gate(source_path)
        destination_parent, parent_identity = _safe_destination_parent(
            contract.root, relative
        )
        destination = destination_parent / relative.name
        if destination.exists() or destination.is_symlink():
            raise IsolatedRestoreAcceptanceError("runtime_source_target_exists")
        try:
            with source_path.open("rb") as source_handle, destination.open(
                "xb"
            ) as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        except FileExistsError as exc:
            raise IsolatedRestoreAcceptanceError("runtime_source_target_exists") from exc
        parent_after = destination.parent.resolve(strict=True).stat()
        if (int(parent_after.st_dev), int(parent_after.st_ino)) != parent_identity:
            raise IsolatedRestoreAcceptanceError("runtime_source_parent_changed")
        if not _is_within(destination.resolve(strict=True), contract.root):
            raise IsolatedRestoreAcceptanceError("path_escape_detected")
        copied_gate = _file_gate(destination)
        if copied_gate != before or _file_gate(source_path) != before:
            raise IsolatedRestoreAcceptanceError("runtime_source_changed_during_copy")
        copied.append({"path": raw, **copied_gate})
    final = state_provider(source)
    if (
        final.status
        or final.head != initial.head
        or final.tracked_paths != initial.tracked_paths
    ):
        raise IsolatedRestoreAcceptanceError("runtime_source_drift_detected")
    for item in copied:
        source_path = source / Path(*PurePosixPath(item["path"]).parts)
        destination = contract.root / Path(*PurePosixPath(item["path"]).parts)
        expected = {
            "exists": item["exists"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        if _file_gate(source_path) != expected or _file_gate(destination) != expected:
            raise IsolatedRestoreAcceptanceError("runtime_source_drift_detected")
    _audit_alias_free_tree(contract.root)
    inventory_basis = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in copied
    ]
    return {
        "source_binding": "fixed_clean_head",
        "source_root": str(source),
        "head": initial.head,
        "file_count": len(copied),
        "inventory_digest": _canonical_digest(inventory_basis),
        "files": inventory_basis,
    }


def _verify_copied_snapshot(
    contract: RestoreContract, snapshot: Mapping[str, Any]
) -> None:
    files = snapshot.get("files")
    if not isinstance(files, list) or len(files) != snapshot.get("file_count"):
        raise IsolatedRestoreAcceptanceError("runtime_snapshot_inventory_invalid")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or not _allowed_tracked_path(item["path"])
        ):
            raise IsolatedRestoreAcceptanceError("runtime_snapshot_inventory_invalid")
        path = _safe_descendant(
            contract.root, Path(*PurePosixPath(item["path"]).parts)
        )
        if _file_gate(path) != {
            "exists": True,
            "size": item["size"],
            "sha256": item["sha256"],
        }:
            raise IsolatedRestoreAcceptanceError("runtime_snapshot_changed")
    if snapshot.get("inventory_digest") != _canonical_digest(files):
        raise IsolatedRestoreAcceptanceError("runtime_snapshot_inventory_invalid")


_PROBE_CODE = r'''
import hashlib, json, re, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve(strict=True)
main = Path(sys.argv[2]).resolve(strict=True)
operational = Path(sys.argv[3]).resolve(strict=True)
sys.path.insert(0, str(root))
import tools.local_web as local_web
from memory import approved_profile_control, db_path_policy, production_runtime_status, read_only_retriever
from memory.operational_state_models import canonical_digest

bindings = {
    "project_root": local_web.PROJECT_ROOT,
    "web_root": local_web.WEB_ROOT,
    "main": local_web.REAL_DB_PATH,
    "default_main": read_only_retriever.DEFAULT_DB_PATH,
    "profile": approved_profile_control.PRODUCTION_PROFILE_SIDECAR,
    "semantic": db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH,
    "runtime": production_runtime_status.PRODUCTION_RUNTIME_SIDECAR,
    "operational": db_path_policy.DEFAULT_OPERATIONAL_STATE_PATH,
    "local_web_module": Path(local_web.__file__),
    "runtime_status_module": Path(production_runtime_status.__file__),
}
bindings = {key: str(value.resolve(strict=False)) for key, value in bindings.items()}
local_modules = []
for name, module in sorted(sys.modules.items()):
    if not (
        name == "memory"
        or name.startswith("memory.")
        or name == "tools"
        or name.startswith("tools.")
        or name == "whale_chat"
    ):
        continue
    module_file = getattr(module, "__file__", None)
    if not module_file:
        continue
    module_path = Path(module_file).resolve(strict=True)
    try:
        relative = module_path.relative_to(root).as_posix()
    except ValueError:
        raise RuntimeError("module_loaded_outside_restore_root")
    local_modules.append({"name": name, "path": relative})
uri = main.as_uri() + "?mode=ro&immutable=1"
with sqlite3.connect(uri, uri=True) as connection:
    connection.execute("PRAGMA query_only=ON")
    rows = connection.execute(
        "SELECT text FROM messages WHERE length(trim(text)) >= 4 "
        "ORDER BY rowid LIMIT 200"
    ).fetchall()
candidates = []
for row in rows:
    text = str(row[0] or "")
    candidates.extend(re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,31}", text))
    candidates.extend(re.findall(r"[\u3400-\u9fff]{2,8}", text))
seen = set()
selected_query = None
expected = None
for candidate in candidates:
    if len(seen) >= 64:
        break
    query = candidate[:12]
    folded = query.casefold()
    if folded in seen:
        continue
    seen.add(folded)
    result = read_only_retriever.search_all_memory_conversations(
        query, sort="relevance", offset=0, limit=5,
        source_platform=None, db_path=main,
    )
    if result.get("items"):
        selected_query = query
        expected = result
        break
if selected_query is None or expected is None:
    raise RuntimeError("isolated_search_probe_no_match")
def identity(value):
    return {
        "total_conversations": value.get("total_conversations"),
        "has_more": value.get("has_more"),
        "next_offset": value.get("next_offset"),
        "sort": value.get("sort"),
        "limit": value.get("limit"),
        "offset": value.get("offset"),
        "items": [
            {
                "conversation_id": item.get("conversation_id"),
                "match_count": item.get("match_count"),
                "other_match_count": item.get("other_match_count"),
                "best_hit": {
                    "conversation_id": item.get("best_hit", {}).get("conversation_id"),
                    "message_id": item.get("best_hit", {}).get("message_id"),
                },
                "other_hits": [
                    {
                        "conversation_id": hit.get("conversation_id"),
                        "message_id": hit.get("message_id"),
                    }
                    for hit in item.get("other_hits", [])
                ],
            }
            for item in value.get("items", [])
        ],
    }
payload = {
    "bindings": bindings,
    "query": selected_query,
    "query_sha256": hashlib.sha256(selected_query.encode("utf-8")).hexdigest(),
    "expected_search_digest": canonical_digest(identity(expected)),
    "expected_search_count": len(expected.get("items", [])),
    "operational_argument": str(operational),
    "local_module_count": len(local_modules),
    "local_module_digest": canonical_digest(local_modules),
}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
'''


def _sanitized_environment() -> dict[str, str]:
    keep = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PATHEXT",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in keep
    }
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "YUNOS_OPERATIONAL_WRITER_ENABLED": "0",
            "YUNOS_COGNITIVE_WRITER_ENABLED": "0",
        }
    )
    return environment


def _run_probe(contract: RestoreContract, python_exe: Path) -> dict[str, Any]:
    command = [
        str(python_exe),
        "-I",
        "-c",
        _PROBE_CODE,
        str(contract.root),
        str(contract.database_paths["main"]),
        str(contract.database_paths["operational"]),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=contract.root,
            env=_sanitized_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolatedRestoreAcceptanceError("isolated_path_probe_failed") from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > _MAX_HTTP_BYTES
        or len(completed.stderr) > _MAX_PROCESS_OUTPUT_BYTES
    ):
        raise IsolatedRestoreAcceptanceError("isolated_path_probe_failed")
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedRestoreAcceptanceError("isolated_path_probe_invalid") from exc
    if not isinstance(value, dict):
        raise IsolatedRestoreAcceptanceError("isolated_path_probe_invalid")
    return value


def _validate_probe(contract: RestoreContract, probe: Mapping[str, Any]) -> None:
    expected = {
        "project_root": contract.root,
        "web_root": contract.root / "web",
        "main": contract.database_paths["main"],
        "default_main": contract.database_paths["main"],
        "profile": contract.database_paths["profile"],
        "semantic": contract.database_paths["semantic"],
        "runtime": contract.database_paths["runtime"],
        "operational": contract.database_paths["operational"],
        "local_web_module": contract.root / "tools" / "local_web.py",
        "runtime_status_module": contract.root / "memory" / "production_runtime_status.py",
    }
    bindings = probe.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != set(expected):
        raise IsolatedRestoreAcceptanceError("isolated_path_binding_invalid")
    for label, path in expected.items():
        try:
            actual = Path(str(bindings[label])).resolve(strict=True)
        except OSError as exc:
            raise IsolatedRestoreAcceptanceError("isolated_path_binding_invalid") from exc
        if _path_key(actual) != _path_key(path.resolve(strict=True)) or not _is_within(
            actual, contract.root
        ):
            raise IsolatedRestoreAcceptanceError("production_path_binding_detected")
    if _path_key(Path(str(probe.get("operational_argument")))) != _path_key(
        contract.database_paths["operational"]
    ):
        raise IsolatedRestoreAcceptanceError("isolated_operational_argument_mismatch")
    query = probe.get("query")
    if (
        not isinstance(query, str)
        or not query
        or len(query) > 12
        or hashlib.sha256(query.encode("utf-8")).hexdigest()
        != probe.get("query_sha256")
        or not _SHA256.fullmatch(str(probe.get("expected_search_digest") or ""))
        or not isinstance(probe.get("expected_search_count"), int)
        or probe.get("expected_search_count", 0) < 1
        or not isinstance(probe.get("local_module_count"), int)
        or probe.get("local_module_count", 0) < 10
        or not _SHA256.fullmatch(str(probe.get("local_module_digest") or ""))
    ):
        raise IsolatedRestoreAcceptanceError("isolated_search_probe_invalid")


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


_NO_PROXY_OPENER = request.build_opener(request.ProxyHandler({}))


def _http_request(
    origin: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> HttpResult:
    target = origin + path
    parsed = parse.urlsplit(target)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise IsolatedRestoreAcceptanceError("non_loopback_http_target_refused")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    outbound = request.Request(target, data=body, headers=headers, method=method)
    try:
        response = _NO_PROXY_OPENER.open(outbound, timeout=3)
    except error.HTTPError as exc:
        response = exc
    except (OSError, error.URLError) as exc:
        raise IsolatedRestoreAcceptanceError("isolated_http_request_failed") from exc
    with response:
        content = response.read(_MAX_HTTP_BYTES + 1)
        if len(content) > _MAX_HTTP_BYTES:
            raise IsolatedRestoreAcceptanceError("isolated_http_output_limit_exceeded")
        return HttpResult(
            status=int(response.status),
            headers={key.lower(): value for key, value in response.headers.items()},
            body=content,
        )


def _json_response(result: HttpResult, *, status: int = 200) -> dict[str, Any]:
    if result.status != status:
        raise IsolatedRestoreAcceptanceError("isolated_http_status_mismatch")
    if result.headers.get("cache-control", "").lower() != "no-store":
        raise IsolatedRestoreAcceptanceError("isolated_api_no_store_missing")
    try:
        value = json.loads(result.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedRestoreAcceptanceError("isolated_api_json_invalid") from exc
    if not isinstance(value, dict):
        raise IsolatedRestoreAcceptanceError("isolated_api_json_invalid")
    return value


def _database_gates(contract: RestoreContract) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, path in contract.database_paths.items():
        result[label] = {
            "db": _file_gate(path),
            "wal": _file_gate(Path(str(path) + "-wal")),
            "shm": _file_gate(Path(str(path) + "-shm")),
        }
    return result


def _windows_job_active_processes(job_handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    information = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    returned = wintypes.DWORD(0)
    if not kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job_handle),
        1,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ctypes.byref(returned),
    ):
        raise IsolatedRestoreAcceptanceError("owned_job_query_failed")
    return int(information.ActiveProcesses)


def _windows_resume_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the one main thread after the suspended process joins its Job."""

    import ctypes
    from ctypes import wintypes

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(THREADENTRY32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(THREADENTRY32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise IsolatedRestoreAcceptanceError("owned_thread_snapshot_failed")
    resumed = 0
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        if not has_entry:
            raise IsolatedRestoreAcceptanceError("owned_thread_enumeration_failed")
        while has_entry:
            if int(entry.th32OwnerProcessID) == process.pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise IsolatedRestoreAcceptanceError("owned_thread_open_failed")
                try:
                    previous_count = int(kernel32.ResumeThread(thread))
                finally:
                    if not kernel32.CloseHandle(thread):
                        raise IsolatedRestoreAcceptanceError(
                            "owned_thread_handle_close_failed"
                        )
                if previous_count != 1:
                    raise IsolatedRestoreAcceptanceError("owned_thread_resume_failed")
                resumed += 1
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        if ctypes.get_last_error() != 18:
            raise IsolatedRestoreAcceptanceError("owned_thread_enumeration_failed")
    finally:
        if not kernel32.CloseHandle(snapshot):
            raise IsolatedRestoreAcceptanceError(
                "owned_thread_snapshot_close_failed"
            )
    if resumed != 1:
        raise IsolatedRestoreAcceptanceError("owned_thread_resume_failed")


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class OwnedServerProcess:
    """One process group/job whose complete tree is terminated in ``close``."""

    def __init__(self, process: subprocess.Popen[bytes], job_handle: int | None) -> None:
        self.process = process
        self.job_handle = job_handle

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def poll(self) -> int | None:
        return self.process.poll()

    def owns_pid(self, pid: object) -> bool:
        """Return whether ``pid`` is inside this acceptance-owned process tree."""

        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if self.poll() is not None:
            return False
        if os.name == "nt":
            if self.job_handle is None:
                return False
            import ctypes
            from ctypes import wintypes

            if pid > 0xFFFFFFFF or wintypes.DWORD(pid).value != pid:
                return False

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.IsProcessInJob.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            ]
            kernel32.IsProcessInJob.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process_handle = kernel32.OpenProcess(0x1000, False, pid)
            if not process_handle:
                return False
            in_owned_job = False
            query_succeeded = False
            try:
                in_job = wintypes.BOOL(False)
                query_succeeded = bool(kernel32.IsProcessInJob(
                    process_handle,
                    wintypes.HANDLE(self.job_handle),
                    ctypes.byref(in_job),
                ))
                in_owned_job = bool(in_job.value)
            finally:
                handle_closed = bool(kernel32.CloseHandle(process_handle))
            return query_succeeded and in_owned_job and handle_closed
        try:
            launcher_group = os.getpgid(self.pid)
            return launcher_group == self.pid and os.getpgid(pid) == launcher_group
        except (OSError, ProcessLookupError):
            return False

    def close(self) -> dict[str, Any]:
        job_terminated = False
        job_closed = False
        job_close_error = 0
        job_query_verified = False
        job_active_processes_after_termination: int | None = None
        process_group_termination_requested = False
        process_group_residual = False
        process_group_identity_held = False
        process_group_identity_lost = False
        process_group_sigkill_sent = False
        if os.name == "nt" and self.job_handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = wintypes.HANDLE(self.job_handle)
            job_terminated = bool(kernel32.TerminateJobObject(handle, 1))
            if job_terminated:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        active_processes = _windows_job_active_processes(
                            self.job_handle
                        )
                    except IsolatedRestoreAcceptanceError:
                        break
                    job_active_processes_after_termination = active_processes
                    if active_processes == 0:
                        job_query_verified = True
                        break
                    time.sleep(0.05)
            job_closed = bool(kernel32.CloseHandle(handle))
            if not job_closed:
                job_close_error = int(ctypes.get_last_error())
            if job_closed:
                self.job_handle = None
            if not job_terminated and not job_closed and self.process.poll() is None:
                self.process.terminate()
        elif os.name != "nt":
            process_group_identity_held = self.process.poll() is None
            process_group_identity_lost = not process_group_identity_held
            if process_group_identity_held:
                process_group_termination_requested = True
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    process_group_identity_lost = True
                except OSError:
                    process_group_identity_lost = True
                    if self.process.poll() is None:
                        self.process.terminate()
        try:
            stdout, stderr = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                self.process.kill()
            else:
                if self.process.poll() is None and not process_group_identity_lost:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        process_group_sigkill_sent = True
                    except (OSError, ProcessLookupError):
                        process_group_identity_lost = True
                        self.process.kill()
                else:
                    process_group_identity_lost = True
            try:
                stdout, stderr = self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise IsolatedRestoreAcceptanceError(
                    "owned_process_output_pipe_residual"
                ) from exc
        if os.name != "nt" and process_group_identity_held:
            deadline = time.monotonic() + 5
            while (
                _posix_process_group_exists(self.process.pid)
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            process_group_residual = _posix_process_group_exists(self.process.pid)
            if process_group_residual and self.process.poll() is not None:
                process_group_identity_lost = True
        if os.name != "nt" and process_group_identity_lost:
            process_group_residual = True
        stdout = stdout or b""
        stderr = stderr or b""
        if self.process.poll() is None:
            raise IsolatedRestoreAcceptanceError("owned_process_residual_detected")
        return {
            "pid": self.pid,
            "tree_termination_requested": True,
            "job_terminated": job_terminated,
            "job_closed": job_closed,
            "job_close_error": job_close_error,
            "job_query_verified": job_query_verified,
            "job_active_processes_after_termination": (
                job_active_processes_after_termination
            ),
            "process_group_termination_requested": (
                process_group_termination_requested
            ),
            "process_group_identity_held": process_group_identity_held,
            "process_group_identity_lost": process_group_identity_lost,
            "process_group_sigkill_sent": process_group_sigkill_sent,
            "process_group_residual": process_group_residual,
            "exited": True,
            "returncode": int(self.process.returncode),
            "stdout_bytes": len(stdout),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_truncated": len(stdout) > _MAX_PROCESS_OUTPUT_BYTES,
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stderr_truncated": len(stderr) > _MAX_PROCESS_OUTPUT_BYTES,
        }


def _validate_process_cleanup(cleanup: Mapping[str, Any]) -> None:
    if cleanup.get("exited") is not True:
        raise IsolatedRestoreAcceptanceError("isolated_process_cleanup_unverified")
    if os.name == "nt" and (
        cleanup.get("job_terminated") is not True
        or cleanup.get("job_query_verified") is not True
        or cleanup.get("job_active_processes_after_termination") != 0
        or cleanup.get("job_closed") is not True
    ):
        raise IsolatedRestoreAcceptanceError("owned_job_cleanup_unverified")
    if os.name != "nt" and (
        cleanup.get("process_group_identity_held") is not True
        or cleanup.get("process_group_identity_lost") is not False
        or cleanup.get("process_group_termination_requested") is not True
        or cleanup.get("process_group_residual") is not False
    ):
        raise IsolatedRestoreAcceptanceError("owned_process_group_cleanup_unverified")


def _close_and_validate_owned_process(
    process: OwnedServerProcess,
) -> dict[str, Any]:
    cleanup = process.close()
    _validate_process_cleanup(cleanup)
    return cleanup


def _windows_job_for(process: subprocess.Popen[bytes]) -> int:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise IsolatedRestoreAcceptanceError("owned_job_creation_failed")
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        if not kernel32.CloseHandle(job):
            raise IsolatedRestoreAcceptanceError(
                "owned_job_configuration_handle_cleanup_failed"
            )
        raise IsolatedRestoreAcceptanceError("owned_job_configuration_failed")
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        if not kernel32.CloseHandle(job):
            raise IsolatedRestoreAcceptanceError(
                "owned_job_assignment_handle_cleanup_failed"
            )
        raise IsolatedRestoreAcceptanceError("owned_job_assignment_failed")
    return int(job)


def _launch_server(
    contract: RestoreContract, port: int, python_exe: Path
) -> OwnedServerProcess:
    server_command = [
        str(python_exe),
        "-I",
        str(contract.root / "tools" / "local_web.py"),
        "--db",
        str(contract.database_paths["main"]),
        "--operational-state-db",
        str(contract.database_paths["operational"]),
        "--real",
        "--readonly",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    command = server_command
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004
    else:
        command = [
            str(python_exe),
            "-I",
            "-c",
            _POSIX_SERVER_BROKER,
            *server_command,
        ]
    try:
        process = subprocess.Popen(
            command,
            cwd=contract.root,
            env=_sanitized_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        raise IsolatedRestoreAcceptanceError("isolated_web_start_failed") from exc
    job_handle: int | None = None
    owned: OwnedServerProcess | None = None
    try:
        if os.name == "nt":
            job_handle = _windows_job_for(process)
        owned = OwnedServerProcess(process, job_handle)
        if os.name == "nt":
            _windows_resume_suspended_process(process)
        return owned
    except BaseException as start_error:
        if owned is not None:
            try:
                _close_and_validate_owned_process(owned)
            except BaseException as cleanup_error:
                combined = IsolatedRestoreAcceptanceError(
                    "isolated_launch_cleanup_unverified"
                )
                if hasattr(combined, "add_note"):
                    combined.add_note(
                        "start_error="
                        f"{type(start_error).__name__}:{start_error}"
                    )
                raise combined from cleanup_error
        else:
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
                if process.poll() is None:
                    raise IsolatedRestoreAcceptanceError(
                        "isolated_suspended_launcher_still_running"
                    )
            except BaseException as cleanup_error:
                combined = IsolatedRestoreAcceptanceError(
                    "isolated_suspended_launcher_cleanup_unverified"
                )
                if hasattr(combined, "add_note"):
                    combined.add_note(
                        "start_error="
                        f"{type(start_error).__name__}:{start_error}"
                    )
                raise combined from cleanup_error
        raise


def _response_evidence(result: HttpResult, payload: Any = None) -> dict[str, Any]:
    value = {
        "status": result.status,
        "cache_control": result.headers.get("cache-control"),
        "content_type": result.headers.get("content-type"),
        "body_bytes": len(result.body),
        "body_sha256": hashlib.sha256(result.body).hexdigest(),
    }
    if payload is not None:
        value["canonical_payload_digest"] = _canonical_digest(payload)
    return value


def _search_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    items = value.get("items")
    if not isinstance(items, list):
        raise IsolatedRestoreAcceptanceError("isolated_search_payload_invalid")
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("best_hit"), dict):
            raise IsolatedRestoreAcceptanceError("isolated_search_payload_invalid")
        other_hits = item.get("other_hits")
        if not isinstance(other_hits, list) or any(
            not isinstance(hit, dict) for hit in other_hits
        ):
            raise IsolatedRestoreAcceptanceError("isolated_search_payload_invalid")
        normalized.append(
            {
                "conversation_id": item.get("conversation_id"),
                "match_count": item.get("match_count"),
                "other_match_count": item.get("other_match_count"),
                "best_hit": {
                    "conversation_id": item["best_hit"].get("conversation_id"),
                    "message_id": item["best_hit"].get("message_id"),
                },
                "other_hits": [
                    {
                        "conversation_id": hit.get("conversation_id"),
                        "message_id": hit.get("message_id"),
                    }
                    for hit in other_hits
                ],
            }
        )
    return {
        "total_conversations": value.get("total_conversations"),
        "has_more": value.get("has_more"),
        "next_offset": value.get("next_offset"),
        "sort": value.get("sort"),
        "limit": value.get("limit"),
        "offset": value.get("offset"),
        "items": normalized,
    }


def _exercise_server(
    contract: RestoreContract,
    port: int,
    python_exe: Path,
    *,
    probe_runner: Callable[[RestoreContract, Path], dict[str, Any]] = _run_probe,
    launcher: Callable[[RestoreContract, int, Path], OwnedServerProcess] = _launch_server,
    http: Callable[..., HttpResult] = _http_request,
    startup_seconds: float = 30.0,
) -> dict[str, Any]:
    probe = probe_runner(contract, python_exe)
    _validate_probe(contract, probe)
    database_before = _database_gates(contract)
    origin = f"http://127.0.0.1:{port}"
    process = launcher(contract, port, python_exe)
    cleanup: dict[str, Any] | None = None
    responses: dict[str, Any] = {}
    try:
        deadline = time.monotonic() + startup_seconds
        status_result: HttpResult | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise IsolatedRestoreAcceptanceError("isolated_web_exited_before_ready")
            try:
                candidate = http(origin, "/api/status")
                if candidate.status == 200:
                    status_result = candidate
                    break
            except IsolatedRestoreAcceptanceError:
                pass
            time.sleep(0.2)
        if status_result is None:
            raise IsolatedRestoreAcceptanceError("isolated_web_start_timeout")
        status = _json_response(status_result)
        if (
            status.get("db_mode") != "real"
            or status.get("service_mode") != "real-readonly"
            or status.get("readonly") is not True
            or status.get("runtime_admin") is not False
            or status.get("profile_admin") is not False
            or status.get("execution_authority_present") is not False
            or status.get("can_execute") is not False
            or status.get("bound_host") != "127.0.0.1"
            or status.get("bound_port") != port
            or not process.owns_pid(status.get("process_id"))
            or status.get("runtime_sidecar", {}).get("sidecar", {}).get("path")
            != str(contract.database_paths["runtime"])
            or status.get("runtime_sidecar", {}).get("status") != "ready"
            or status.get("runtime_sidecar", {}).get("ready") is not True
            or status.get("runtime_sidecar", {}).get("readonly") is not True
            or status.get("runtime_sidecar", {}).get("execution", {}).get(
                "credential_read"
            )
            is not False
        ):
            raise IsolatedRestoreAcceptanceError("isolated_status_boundary_invalid")
        responses["status"] = _response_evidence(status_result, status)

        query = str(probe["query"])
        search_path = (
            "/api/search?"
            + parse.urlencode(
                {
                    "q": query,
                    "scope": "all",
                    "aggregate": "conversation",
                    "sort": "relevance",
                    "offset": "0",
                    "limit": "5",
                }
            )
        )
        search_result = http(origin, search_path)
        search = _json_response(search_result)
        if (
            _canonical_digest(_search_identity(search))
            != probe["expected_search_digest"]
            or len(search.get("items", [])) != probe["expected_search_count"]
        ):
            raise IsolatedRestoreAcceptanceError("isolated_search_mismatch")
        responses["search"] = {
            **_response_evidence(search_result, search),
            "query_sha256": probe["query_sha256"],
            "result_count": len(search["items"]),
            "matches_isolated_direct_query": True,
        }

        cognitive: dict[str, dict[str, Any]] = {}
        for label, path in (
            ("objectives", "/api/cognitive/objectives?limit=5"),
            ("plans", "/api/cognitive/plans?limit=5"),
            ("runs", "/api/cognitive/runs?limit=5"),
        ):
            result = http(origin, path)
            payload = _json_response(result)
            if (
                payload.get("readonly") is not True
                or payload.get("cache_control") != "no-store"
                or not isinstance(payload.get("items"), list)
                or not isinstance(payload.get("count"), int)
                or payload["count"] < 1
            ):
                raise IsolatedRestoreAcceptanceError("isolated_cognitive_api_invalid")
            cognitive[label] = payload
            responses[f"cognitive_{label}"] = {
                **_response_evidence(result, payload),
                "count": payload["count"],
            }
        run_id = cognitive["runs"]["items"][0].get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise IsolatedRestoreAcceptanceError("isolated_workspace_run_missing")
        for surface in ("desktop", "mobile"):
            result = http(
                origin,
                "/api/cognitive/workspace?"
                + parse.urlencode({"run_id": run_id, "surface": surface}),
            )
            payload = _json_response(result)
            boundaries = payload.get("boundaries", {})
            if (
                payload.get("readonly") is not True
                or payload.get("cache_control") != "no-store"
                or payload.get("surface") != surface
                or boundaries.get("readonly") is not True
                or boundaries.get("write_api_available") is not False
                or boundaries.get("mobile_write_allowed") is not False
                or boundaries.get("tailnet_write_allowed") is not False
                or boundaries.get("acceptance_write_allowed") is not False
                or boundaries.get("authority_expansion_allowed") is not False
            ):
                raise IsolatedRestoreAcceptanceError("isolated_workspace_boundary_invalid")
            responses[f"workspace_{surface}"] = _response_evidence(result, payload)

        for label, path, marker in (
            ("index", "/", "认知任务工作区"),
            ("app", "/app.js", "/api/cognitive/workspace"),
            ("service_worker", "/service-worker.js", "CACHE"),
            ("manifest", "/manifest.webmanifest", "YunOS"),
        ):
            result = http(origin, path)
            try:
                text = result.body.decode("utf-8")
            except UnicodeError as exc:
                raise IsolatedRestoreAcceptanceError("isolated_static_utf8_invalid") from exc
            if result.status != 200 or marker not in text:
                raise IsolatedRestoreAcceptanceError("isolated_workspace_static_invalid")
            responses[f"static_{label}"] = _response_evidence(result)

        for label, path in (
            ("mark", "/api/mark"),
            ("runtime_execute", "/api/runtime-execute"),
            ("profile_decision", "/api/approved-profile-decision"),
            ("cognitive_write", "/api/cognitive/accept"),
        ):
            result = http(origin, path, method="POST", body=b"{}")
            payload = _json_response(result, status=403)
            if payload.get("error") != "Readonly mode: writes are disabled":
                raise IsolatedRestoreAcceptanceError("isolated_write_route_not_refused")
            responses[f"write_refusal_{label}"] = _response_evidence(result, payload)
    except BaseException as primary_error:
        try:
            cleanup = _close_and_validate_owned_process(process)
        except BaseException as cleanup_error:
            combined = IsolatedRestoreAcceptanceError(
                "isolated_cleanup_unverified_after_failure"
            )
            if hasattr(combined, "add_note"):
                combined.add_note(
                    "primary_error="
                    f"{type(primary_error).__name__}:{primary_error}"
                )
            raise combined from cleanup_error
        raise
    else:
        cleanup = _close_and_validate_owned_process(process)
    database_after = _database_gates(contract)
    sidecar_observations: dict[str, Any] = {}
    for label in contract.database_paths:
        if database_after[label]["db"] != database_before[label]["db"]:
            raise IsolatedRestoreAcceptanceError(
                "restored_database_changed_during_acceptance"
            )
        before_wal = database_before[label]["wal"]
        after_wal = database_after[label]["wal"]
        if before_wal["exists"] and after_wal != before_wal:
            raise IsolatedRestoreAcceptanceError(
                "restored_wal_changed_during_acceptance"
            )
        if (
            not before_wal["exists"]
            and after_wal["exists"]
            and after_wal["size"] != 0
        ):
            raise IsolatedRestoreAcceptanceError(
                "restored_wal_content_created_during_acceptance"
            )
        sidecar_observations[label] = {
            "wal_changed": before_wal != after_wal,
            "shm_changed": database_before[label]["shm"]
            != database_after[label]["shm"],
            "classification": "restore_local_sqlite_observation",
        }
    if (
        cleanup.get("stdout_bytes", 0) > _MAX_PROCESS_OUTPUT_BYTES
        or cleanup.get("stderr_bytes", 0) > _MAX_PROCESS_OUTPUT_BYTES
    ):
        raise IsolatedRestoreAcceptanceError("isolated_process_output_limit_exceeded")
    return {
        "path_probe": {
            "all_fixed_roots_below_restore_root": True,
            "bindings": dict(probe["bindings"]),
            "bindings_digest": _canonical_digest(probe["bindings"]),
            "local_module_count": probe["local_module_count"],
            "local_module_digest": probe["local_module_digest"],
            "query_sha256": probe["query_sha256"],
        },
        "launch_boundary": {
            "python_executable": str(python_exe),
            "script": str(contract.root / "tools" / "local_web.py"),
            "host": "127.0.0.1",
            "port": port,
            "real": True,
            "readonly": True,
            "shell": False,
            "provider_flags_present": False,
            "launcher_pid": process.pid,
            "service_pid": status["process_id"],
            "service_pid_owned": True,
            "service_pid_matches_launcher": status["process_id"] == process.pid,
            "ownership_method": (
                "windows_job_object" if os.name == "nt" else "posix_process_group"
            ),
            "posix_identity_broker": os.name != "nt",
        },
        "database_gates_before": database_before,
        "database_gates_after": database_after,
        "database_content_unchanged": True,
        "sidecar_observations": sidecar_observations,
        "responses": responses,
        "process_cleanup": cleanup,
    }


def _write_evidence(root: Path, report: Mapping[str, Any]) -> tuple[Path, str]:
    target = root / EVIDENCE_NAME
    if target.exists() or target.is_symlink():
        raise IsolatedRestoreAcceptanceError("isolated_acceptance_already_recorded")
    parent_identity = (int(root.stat().st_dev), int(root.stat().st_ino))
    temporary = root / f".{EVIDENCE_NAME}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if (int(root.stat().st_dev), int(root.stat().st_ino)) != parent_identity:
            raise IsolatedRestoreAcceptanceError("restore_root_identity_changed")
        _reject_reparse(temporary)
        _reject_hardlink(temporary)
        os.replace(temporary, target)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    _reject_reparse(target)
    _reject_hardlink(target)
    return target, _hash_file(target)


def run_acceptance(restore_root: Path, port: int) -> dict[str, Any]:
    started = time.perf_counter()
    started_at = _utc_now()
    fixed_source = _safe_existing_directory(SOURCE_ROOT)
    if _path_key(fixed_source) != _path_key(SOURCE_ROOT):
        raise IsolatedRestoreAcceptanceError("fixed_source_root_mismatch")
    fixed_output = _safe_existing_directory(OUTPUT_ROOT)
    python_exe = PYTHON_EXE.resolve(strict=True)
    _reject_reparse(python_exe)
    _reject_hardlink(python_exe)
    contract = _validate_restore_contract(
        restore_root, port, output_root=fixed_output
    )
    source_snapshot = _copy_clean_head_snapshot(
        contract, source_root=fixed_source
    )
    live = _exercise_server(contract, port, python_exe)
    _verify_copied_snapshot(contract, source_snapshot)
    final_source = _capture_git_state(fixed_source)
    if (
        final_source.status
        or final_source.head != source_snapshot["head"]
        or tuple(sorted(final_source.tracked_paths))
        != tuple(item["path"] for item in source_snapshot["files"])
    ):
        raise IsolatedRestoreAcceptanceError("source_worktree_changed_during_acceptance")
    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "accepted",
        "phase": contract.phase,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "restore_root": str(contract.root),
        "requested_loopback_origin": f"http://127.0.0.1:{port}",
        "production_database_connected": False,
        "provider_call_performed": False,
        "write_route_enabled": False,
        "manifest_digest": contract.manifest_digest,
        "backup_set_digest": contract.backup_set_digest,
        "restore_runtime_digest": contract.runtime["runtime_digest"],
        "ownership_marker_identity_digest": contract.ownership_marker[
            "ownership_identity_digest"
        ],
        "ownership_marker_digest": contract.ownership_marker["marker_digest"],
        "source_snapshot": source_snapshot,
        **live,
    }
    report["evidence_digest"] = _canonical_digest(report)
    evidence_path, evidence_sha = _write_evidence(contract.root, report)
    _audit_alias_free_tree(contract.root)
    return {
        "status": "accepted",
        "production_database_connected": False,
        "provider_call_performed": False,
        "restore_root": str(contract.root),
        "origin": f"http://127.0.0.1:{port}",
        "process_cleanup": report["process_cleanup"],
        "evidence_path": str(evidence_path),
        "evidence_sha256": evidence_sha,
        "evidence_digest": report["evidence_digest"],
    }


def _port_argument(value: str) -> int:
    try:
        return _validate_port(int(value, 10))
    except (ValueError, IsolatedRestoreAcceptanceError) as exc:
        raise argparse.ArgumentTypeError("an isolated port from 1024 through 65535 is required") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-root", type=Path, required=True)
    parser.add_argument("--port", type=_port_argument, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(args.restore_root, args.port)
    except IsolatedRestoreAcceptanceError as exc:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "reason_code": str(exc),
                    "production_database_connected": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
