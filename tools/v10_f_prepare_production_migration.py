"""Prepare the fixed v10-F production migration control records.

This command does not migrate a database.  It verifies one preproduction
release package, binds its Operational backup to the current read-only
production migration plan and clean Git commit, and creates the two canonical
records consumed by :mod:`memory.cognitive_production_migration`.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_production_migration import (  # noqa: E402
    COGNITIVE_CAPTURE_ARTIFACT_VERSION,
    COGNITIVE_CAPTURE_CONTROLLER_DIGEST,
    COGNITIVE_CAPTURE_ISSUER_ID,
    COGNITIVE_CAPTURE_ISSUER_VERSION,
    COGNITIVE_CONTROL_PREPARATION_VERSION,
    COGNITIVE_PRODUCTION_AUTHORITY_ID,
    COGNITIVE_PRODUCTION_AUTHORITY_ISSUER,
    COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256,
    COGNITIVE_PRODUCTION_QUIESCENCE_VERSION,
    PRODUCTION_AUTHORITY_RECORD,
    PRODUCTION_BACKUP_VERIFICATION_RECORD,
    PRODUCTION_MIGRATION_CONTROL_ROOT,
    PRODUCTION_OPERATIONAL_DATABASE,
    _completion_publication_pins,
    build_cognitive_migration_backup_verification,
    build_cognitive_production_migration_authority,
    build_cognitive_quiescence_capture_artifact,
    canonical_control_document_bytes,
    cognitive_production_execution_id,
    inspect_fixed_production_cognitive_migration_plan,
    validate_cognitive_quiescence_capture_artifact,
    validate_cognitive_production_quiescence_evidence,
)
from memory.db_path_policy import is_reparse_point  # noqa: E402
from memory.secure_control_files import (  # noqa: E402
    SecureControlFileError,
    pinned_control_directory,
)
from memory.operational_state_models import (  # noqa: E402
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    validate_sha256,
)
from tools.v10_release_recovery import (  # noqa: E402
    DEFAULT_DATABASES,
    RecoveryError,
    validate_fixed_production_provenance,
    verify_package,
)


CONTROL_PREPARATION_VERSION = COGNITIVE_CONTROL_PREPARATION_VERSION
QUIESCENCE_EVIDENCE_VERSION = COGNITIVE_PRODUCTION_QUIESCENCE_VERSION
USER_AUTHORIZATION_TEXT = "授权，继续"
USER_AUTHORIZATION_TEXT_SHA256 = (
    COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
)
FORMAL_CAPTURE_ROOT = (
    PROJECT_ROOT / "output" / "v10-f-quiescence-evidence"
).absolute()
FORMAL_CAPTURE_CONTROLLER_PATH = (
    PROJECT_ROOT / "tools" / "v10_f_capture_production_quiescence.py"
).absolute()
TRUSTED_TAILSCALE_EXECUTABLE = Path(r"E:\Tailscale\tailscale.exe")
CAPTURE_ARTIFACT_VERSION = COGNITIVE_CAPTURE_ARTIFACT_VERSION
CAPTURE_ISSUER_VERSION = COGNITIVE_CAPTURE_ISSUER_VERSION
CAPTURE_ISSUER_ID = COGNITIVE_CAPTURE_ISSUER_ID
CAPTURE_CONTROLLER_DIGEST = COGNITIVE_CAPTURE_CONTROLLER_DIGEST
_SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve(strict=True)
_CAPTURE_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_FILENAMES = {
    "cognitive_migration_plan.json",
    "cognitive_migration_backup_verification.json",
    "cognitive_migration_authority.json",
    "service_quiescence_evidence.json",
    "control_manifest.json",
}
_CAPTURE_SOURCE_IDS = {
    "capture_controller",
    "operational_database",
    "tailscale_executable",
}


class ControlPreparationError(ValueError):
    """Stable, non-secret preparation failure."""


def _fail(reason_code: str) -> None:
    raise ControlPreparationError(reason_code)


def _reject_reparse_chain(path: Path, reason_code: str) -> None:
    current = path.absolute()
    while True:
        try:
            if (current.exists() or current.is_symlink()) and is_reparse_point(
                current
            ):
                _fail(reason_code)
        except OSError:
            _fail(reason_code)
        if current.parent == current:
            return
        current = current.parent


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _path_is_within(candidate: Path, root: Path) -> bool:
    key = _path_key(candidate)
    boundary = _path_key(root).rstrip("\\/")
    return key == boundary or key.startswith(boundary + os.sep)


def _canonical_path(path: Path, reason_code: str) -> Path:
    selected = Path(path).expanduser().absolute()
    _reject_reparse_chain(selected, reason_code)
    try:
        return selected.resolve(strict=False)
    except OSError:
        _fail(reason_code)


def _formal_control_paths() -> tuple[Path, ...]:
    return tuple(
        PRODUCTION_MIGRATION_CONTROL_ROOT / name
        for name in sorted(_CONTROL_FILENAMES)
    )


def _reject_formal_path_alias(path: Path, reason_code: str) -> None:
    """Reject lexical, resolved, reparse and existing hardlink aliases."""

    selected = _canonical_path(path, reason_code)
    formal_roots = (
        FORMAL_CAPTURE_ROOT,
        PRODUCTION_MIGRATION_CONTROL_ROOT,
    )
    if any(_path_is_within(selected, root) for root in formal_roots):
        _fail(reason_code)
    formal_records = _formal_control_paths()
    if any(_path_key(selected) == _path_key(item) for item in formal_records):
        _fail(reason_code)
    try:
        if selected.exists() and selected.is_file():
            selected_stat = selected.stat()
            for formal in formal_records:
                if formal.exists() and formal.is_file():
                    formal_stat = formal.stat()
                    if (selected_stat.st_dev, selected_stat.st_ino) == (
                        formal_stat.st_dev,
                        formal_stat.st_ino,
                    ):
                        _fail(reason_code)
    except OSError:
        _fail(reason_code)


def _temp_only_path(path: Path, reason_code: str) -> Path:
    selected = _canonical_path(path, reason_code)
    _reject_formal_path_alias(selected, reason_code)
    if not _path_is_within(selected, _SYSTEM_TEMP_ROOT) or _path_key(
        selected
    ) == _path_key(_SYSTEM_TEMP_ROOT):
        _fail(reason_code)
    return selected


def _preflight_shared_control_paths(
    *,
    control_root: Path,
    authority_record_path: Path,
    backup_record_path: Path,
) -> tuple[Path, Path, Path]:
    """Resolve every shared output before package reads or callbacks occur."""

    root = _temp_only_path(
        control_root, "shared_control_formal_path_forbidden"
    )
    authority = _temp_only_path(
        authority_record_path, "shared_control_formal_path_forbidden"
    )
    backup = _temp_only_path(
        backup_record_path, "shared_control_formal_path_forbidden"
    )
    expected_authority = root / "cognitive_migration_authority.json"
    expected_backup = root / "cognitive_migration_backup_verification.json"
    if (
        _path_key(authority) != _path_key(expected_authority)
        or _path_key(backup) != _path_key(expected_backup)
    ):
        _fail("production_control_fixed_path_mismatch")
    if root.exists():
        _fail("production_control_root_exists")
    return root, authority, backup


def _load_document(
    path: Path,
    reason_code: str,
    *,
    canonical_required: bool,
) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail(reason_code)
    if not isinstance(value, dict) or (
        canonical_required and raw.strip() != canonical_json(value)
    ):
        _fail(reason_code)
    return value


def _validate_quiescence(
    value: Mapping[str, Any],
    *,
    now: datetime | None = None,
    authorized_at: datetime | None = None,
) -> dict[str, Any]:
    try:
        return validate_cognitive_production_quiescence_evidence(
            value,
            now=now,
            authorized_at=authorized_at,
        )
    except Exception as exc:
        _fail(str(exc))


def build_service_quiescence_evidence(
    *,
    captured_at: str,
    tailscale_serve_config_digest: str,
    local_service_listener_count: int,
    writer_lease_count: int,
    operational_wal_size: int,
    production_write_env_enabled: bool,
    tailscale_backend: str,
    funnel_enabled: bool,
) -> dict[str, Any]:
    """Build evidence from measurements captured by the release controller."""

    record = {
        "version": QUIESCENCE_EVIDENCE_VERSION,
        "captured_at": normalize_timestamp(captured_at),
        "local_service_port": 8765,
        "local_service_listener_count": local_service_listener_count,
        "local_service_stopped": local_service_listener_count == 0,
        "writer_lease_count": writer_lease_count,
        "operational_wal_size": operational_wal_size,
        "production_write_env_enabled": production_write_env_enabled,
        "tailscale_backend": tailscale_backend,
        "tailscale_serve_config_digest": validate_sha256(
            tailscale_serve_config_digest,
            "service_quiescence_evidence_invalid",
        ),
        "funnel_enabled": funnel_enabled,
    }
    record["evidence_digest"] = canonical_digest(record)
    return _validate_quiescence(record)


def _fixed_file_identity(
    path: Path,
    *,
    source_id: str,
    project_relative: bool,
    project_root: Path,
) -> dict[str, Any]:
    """Hash one fixed source while rejecting path or identity substitution."""

    _reject_reparse_chain(path, "capture_source_identity_forbidden")
    try:
        selected = path.resolve(strict=True)
        before = selected.stat()
        if (
            not selected.is_file()
            or is_reparse_point(selected)
            or int(before.st_nlink) != 1
        ):
            _fail("capture_source_identity_forbidden")
        digest = hashlib.sha256()
        with selected.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        after = selected.stat()
    except OSError:
        _fail("capture_source_identity_unavailable")
    before_gate = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_nlink),
    )
    after_gate = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_nlink),
    )
    if before_gate != after_gate:
        _fail("capture_source_identity_changed")
    if project_relative:
        try:
            reference = selected.relative_to(project_root).as_posix()
        except (OSError, ValueError):
            _fail("capture_source_identity_forbidden")
        path_scope = "project"
    else:
        reference = hashlib.sha256(
            _path_key(selected).encode("utf-8")
        ).hexdigest()
        path_scope = "external-path-digest"
    return {
        "source_id": source_id,
        "path_scope": path_scope,
        "path_reference": reference,
        "device": before_gate[0],
        "inode": before_gate[1],
        "size": before_gate[2],
        "mtime_ns": before_gate[3],
        "sha256": digest.hexdigest(),
    }


def _install_fixed_capture_source_identity_probe():
    fixed_project_root = PROJECT_ROOT.resolve(strict=True)
    fixed_controller = FORMAL_CAPTURE_CONTROLLER_PATH.absolute()
    fixed_operational = (
        PROJECT_ROOT / DEFAULT_DATABASES["operational"]
    ).absolute()
    fixed_tailscale = TRUSTED_TAILSCALE_EXECUTABLE.absolute()
    real_identity = _fixed_file_identity

    def capture() -> dict[str, Any]:
        return {
            "capture_controller": real_identity(
                fixed_controller,
                source_id="capture_controller",
                project_relative=True,
                project_root=fixed_project_root,
            ),
            "operational_database": real_identity(
                fixed_operational,
                source_id="operational_database",
                project_relative=True,
                project_root=fixed_project_root,
            ),
            "tailscale_executable": real_identity(
                fixed_tailscale,
                source_id="tailscale_executable",
                project_relative=False,
                project_root=fixed_project_root,
            ),
        }

    return capture


_FIXED_CAPTURE_SOURCE_IDENTITY_PROBE = (
    _install_fixed_capture_source_identity_probe()
)
del _install_fixed_capture_source_identity_probe


def capture_fixed_source_identities() -> dict[str, Any]:
    """Read identities for the three code-owned capture sources."""

    return _FIXED_CAPTURE_SOURCE_IDENTITY_PROBE()


def _validate_source_identities(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CAPTURE_SOURCE_IDS:
        _fail("capture_artifact_source_identity_invalid")
    normalized: dict[str, Any] = {}
    expected_fields = {
        "source_id",
        "path_scope",
        "path_reference",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "sha256",
    }
    for source_id in sorted(_CAPTURE_SOURCE_IDS):
        record = value.get(source_id)
        if not isinstance(record, Mapping) or set(record) != expected_fields:
            _fail("capture_artifact_source_identity_invalid")
        item = dict(record)
        if (
            item.get("source_id") != source_id
            or item.get("path_scope")
            not in {"project", "external-path-digest"}
            or not isinstance(item.get("path_reference"), str)
            or not item["path_reference"]
            or any(
                type(item.get(field)) is not int or item[field] < 0
                for field in ("device", "inode", "size", "mtime_ns")
            )
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
        ):
            _fail("capture_artifact_source_identity_invalid")
        if source_id == "tailscale_executable":
            if item["path_scope"] != "external-path-digest" or _SHA256.fullmatch(
                item["path_reference"]
            ) is None:
                _fail("capture_artifact_source_identity_invalid")
        elif item["path_scope"] != "project" or Path(
            item["path_reference"]
        ).is_absolute() or ".." in Path(item["path_reference"]).parts:
            _fail("capture_artifact_source_identity_invalid")
        normalized[source_id] = item
    return normalized


def build_sealed_quiescence_capture_artifact(
    *,
    evidence: Mapping[str, Any],
    source_identities: Mapping[str, Any],
    nonce: str,
) -> dict[str, Any]:
    """Pure builder used by the fixed capture executor and temp-only tests."""
    try:
        return build_cognitive_quiescence_capture_artifact(
            evidence=evidence,
            source_identities=source_identities,
            nonce=nonce,
        )
    except Exception as exc:
        _fail(str(exc))


def validate_sealed_quiescence_capture_artifact(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Strictly validate every issuer and digest field without doing I/O."""
    try:
        return validate_cognitive_quiescence_capture_artifact(value)
    except Exception as exc:
        _fail(str(exc))


def _install_fixed_capture_artifact_validator():
    fixed_project_root = PROJECT_ROOT.resolve(strict=True)
    fixed_capture_root = FORMAL_CAPTURE_ROOT.absolute()
    fixed_artifact_version = CAPTURE_ARTIFACT_VERSION
    fixed_issuer_version = CAPTURE_ISSUER_VERSION
    fixed_issuer_id = CAPTURE_ISSUER_ID
    fixed_controller_digest = CAPTURE_CONTROLLER_DIGEST
    fixed_user_authorization_digest = USER_AUTHORIZATION_TEXT_SHA256
    real_source_identities = _FIXED_CAPTURE_SOURCE_IDENTITY_PROBE
    real_validate_document = validate_sealed_quiescence_capture_artifact
    real_validate_quiescence = _validate_quiescence
    real_clock = datetime.now
    utc = timezone.utc

    def validate(path: Path) -> dict[str, Any]:
        selected = Path(path).expanduser().absolute()
        if _path_key(selected.parent) != _path_key(fixed_capture_root):
            _fail("service_quiescence_evidence_path_forbidden")
        try:
            with pinned_control_directory(
                anchor=fixed_project_root,
                path=fixed_capture_root,
                create_if_missing=False,
            ) as directory:
                pinned = directory.open_file(selected.name)
                raw_bytes = pinned.read()
                file_stat = os.fstat(pinned.handle.fileno())
                resolved = pinned.path.resolve(strict=True)
                if _path_key(resolved) != _path_key(selected):
                    _fail("service_quiescence_evidence_path_forbidden")
                pinned.verify()
            raw_text = raw_bytes.decode("utf-8", errors="strict")
            raw = json.loads(raw_text)
        except SecureControlFileError:
            _fail("capture_artifact_identity_changed")
        except (OSError, UnicodeError, json.JSONDecodeError):
            _fail("capture_artifact_invalid")
        if (
            not isinstance(raw, dict)
            or raw_text.strip() != canonical_json(raw)
        ):
            _fail("capture_artifact_identity_changed")
        artifact = real_validate_document(raw)
        issuer = artifact["issuer"]
        if (
            artifact["version"] != fixed_artifact_version
            or issuer["version"] != fixed_issuer_version
            or issuer["issuer_id"] != fixed_issuer_id
            or issuer["controller_digest"] != fixed_controller_digest
            or issuer["user_authorization_text_digest"]
            != fixed_user_authorization_digest
        ):
            _fail("capture_artifact_issuer_invalid")
        stamp = artifact["evidence"]["captured_at"].replace(":", "").replace(
            ".", ""
        ).replace("-", "")
        if resolved.name != f"service_quiescence_evidence_{stamp}.json":
            _fail("service_quiescence_evidence_path_forbidden")
        if issuer["source_identities"] != real_source_identities():
            _fail("capture_artifact_source_identity_changed")
        evidence = real_validate_quiescence(
            artifact["evidence"], now=real_clock(utc)
        )
        file_gate = {
            "device": int(file_stat.st_dev),
            "inode": int(file_stat.st_ino),
            "size": int(file_stat.st_size),
            "mtime_ns": int(file_stat.st_mtime_ns),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        }
        return {
            "artifact": artifact,
            "evidence": evidence,
            "path": str(resolved),
            "path_gate": file_gate,
            "record_digest": artifact["record_digest"],
        }

    return validate


_FIXED_CAPTURE_ARTIFACT_VALIDATOR = _install_fixed_capture_artifact_validator()
del _install_fixed_capture_artifact_validator


def validate_fixed_capture_artifact(path: Path) -> dict[str, Any]:
    """Read exactly one code-owned formal capture and verify its live sources."""

    return _FIXED_CAPTURE_ARTIFACT_VALIDATOR(path)


def _git_head_state(root: Path) -> dict[str, Any]:
    def run(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), *arguments),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
            )
        except (OSError, UnicodeError, subprocess.SubprocessError):
            _fail("production_control_git_state_unavailable")
        return completed.stdout.strip()

    tracked_status = run("status", "--porcelain", "--untracked-files=no")
    untracked_status = run("ls-files", "--others", "--exclude-standard")
    return {
        "commit": run("rev-parse", "HEAD"),
        "tracked_worktree_clean": not bool(tracked_status),
        "staging_empty": not bool(run("diff", "--cached", "--name-only")),
        "untracked_empty": not bool(untracked_status),
        "worktree_clean": not bool(tracked_status or untracked_status),
    }


def _write_exclusive(path: Path, document: Mapping[str, Any]) -> None:
    selected = _temp_only_path(
        path, "shared_control_formal_path_forbidden"
    )
    if selected.exists() or selected.is_symlink():
        _fail("production_control_record_exists")
    parent = selected.parent
    if not parent.is_dir() or is_reparse_point(parent):
        _fail("production_control_parent_unsafe")
    try:
        with selected.open("xb") as handle:
            handle.write(canonical_control_document_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _fail("production_control_record_exists")
    except OSError:
        _fail("production_control_record_write_failed")


def _safe_new_control_root(root: Path) -> Path:
    selected = _temp_only_path(
        root, "shared_control_formal_path_forbidden"
    )
    if selected.exists():
        _fail("production_control_root_exists")
    parent = selected.parent
    if not parent.is_dir() or is_reparse_point(parent):
        _fail("production_control_parent_unsafe")
    try:
        selected.mkdir(parents=False, exist_ok=False)
    except OSError:
        _fail("production_control_root_create_failed")
    if is_reparse_point(selected):
        _fail("production_control_root_unsafe")
    return selected


def _build_control_documents(
    *,
    preproduction_package: Path,
    quiescence_capture_artifact: Mapping[str, Any],
    plan: Mapping[str, Any],
    code_commit: str,
    authorized_at: str,
    expires_at: str,
    require_fixed_production_provenance: bool = False,
) -> dict[str, Any]:
    """Pure construction core; it has no output path and performs no writes."""

    try:
        normalized_authorized = normalize_timestamp(authorized_at)
        authorized = datetime.fromisoformat(
            normalized_authorized.replace("Z", "+00:00")
        )
    except Exception:
        _fail("authorization_time_invalid")
    if normalized_authorized != authorized_at:
        _fail("authorization_time_invalid")
    artifact = validate_sealed_quiescence_capture_artifact(
        quiescence_capture_artifact
    )
    evidence = _validate_quiescence(
        artifact["evidence"], authorized_at=authorized
    )
    issuer = artifact["issuer"]
    selected_package = preproduction_package.expanduser().absolute()
    _reject_reparse_chain(
        selected_package, "preproduction_package_path_forbidden"
    )
    try:
        package_root = selected_package.resolve(strict=True)
    except OSError:
        _fail("preproduction_package_unavailable")
    if not package_root.is_dir() or is_reparse_point(package_root):
        _fail("preproduction_package_path_forbidden")
    verified = verify_package(
        package_root,
        expected_phase="preproduction",
    )
    manifest_path = package_root / "release_manifest.json"
    try:
        manifest_stat = manifest_path.stat()
        manifest_bytes = manifest_path.read_bytes()
        final_manifest_stat = manifest_path.stat()
    except OSError:
        _fail("preproduction_release_manifest_invalid")
    if (
        not manifest_path.is_file()
        or is_reparse_point(manifest_path)
        or manifest_stat.st_nlink != 1
        or (
            manifest_stat.st_dev,
            manifest_stat.st_ino,
            manifest_stat.st_size,
            manifest_stat.st_mtime_ns,
            manifest_stat.st_nlink,
        )
        != (
            final_manifest_stat.st_dev,
            final_manifest_stat.st_ino,
            final_manifest_stat.st_size,
            final_manifest_stat.st_mtime_ns,
            final_manifest_stat.st_nlink,
        )
    ):
        _fail("preproduction_release_manifest_path_forbidden")
    manifest_gate = {
        "exists": True,
        "size": manifest_stat.st_size,
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    manifest_identity_gate = {
        "device": int(manifest_stat.st_dev),
        "inode": int(manifest_stat.st_ino),
        "size": int(manifest_stat.st_size),
        "mtime_ns": int(manifest_stat.st_mtime_ns),
        "nlink": int(manifest_stat.st_nlink),
    }
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("preproduction_release_manifest_invalid")
    if not isinstance(manifest, dict):
        _fail("preproduction_release_manifest_invalid")
    source_provenance = manifest.get("source_provenance")
    if require_fixed_production_provenance and not isinstance(
        source_provenance, Mapping
    ):
        _fail("fixed_production_source_provenance_missing")
    if (
        verified.get("status") != "verified"
        or verified.get("phase") != "preproduction"
        or verified.get("manifest_digest") != manifest.get("manifest_digest")
        or verified.get("backup_set_digest") != manifest.get("backup_set_digest")
        or verified.get("service_quiescence_digest")
        != evidence["evidence_digest"]
    ):
        _fail("preproduction_package_quiescence_binding_mismatch")
    operational = manifest.get("database_results", {}).get("operational")
    gate = (
        operational.get("backup_audit", {}).get("file_gate")
        if isinstance(operational, dict)
        else None
    )
    if (
        not isinstance(gate, dict)
        or set(gate) != {"exists", "size", "sha256"}
        or gate.get("exists") is not True
    ):
        _fail("preproduction_operational_backup_missing")
    operational_relative = operational.get("backup_relative_path")
    if not isinstance(operational_relative, str):
        _fail("preproduction_operational_backup_missing")
    backup = build_cognitive_migration_backup_verification(
        plan=plan,
        backup_manifest_digest=verified["manifest_digest"],
        backup_set_digest=verified["backup_set_digest"],
        backup_database_sha256=gate["sha256"],
        backup_database_size=gate["size"],
        preproduction_package_absolute_path=str(package_root.absolute()),
        operational_backup_relative_path=operational_relative,
        release_manifest_file_gate=manifest_gate,
        service_quiescence_digest=evidence["evidence_digest"],
        service_quiescence_captured_at=evidence["captured_at"],
        capture_artifact_digest=artifact["artifact_digest"],
        capture_record_digest=artifact["record_digest"],
        capture_issuer_digest=issuer["issuer_digest"],
        capture_nonce=issuer["nonce"],
        capture_source_identities_digest=issuer[
            "source_identities_digest"
        ],
        capture_relative_path=issuer["capture_relative_path"],
        verified_at=authorized_at,
        expires_at=expires_at,
    )
    authority = build_cognitive_production_migration_authority(
        plan=plan,
        backup_record=backup,
        authority_id=COGNITIVE_PRODUCTION_AUTHORITY_ID,
        one_time_execution_id=cognitive_production_execution_id(
            plan_digest=plan["plan_digest"],
            backup_set_digest=verified["backup_set_digest"],
            code_commit=code_commit,
        ),
        issued_by=COGNITIVE_PRODUCTION_AUTHORITY_ISSUER,
        code_commit=code_commit,
        user_authorization_text_digest=USER_AUTHORIZATION_TEXT_SHA256,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )
    provenance_verification: dict[str, Any] | None = None
    if require_fixed_production_provenance:
        try:
            provenance_verification = validate_fixed_production_provenance(
                source_provenance
            )
        except RecoveryError as exc:
            _fail(str(exc))
    manifest_record = {
        "version": CONTROL_PREPARATION_VERSION,
        "prepared_at": authorized_at,
        "expires_at": expires_at,
        "code_commit": code_commit,
        "plan_digest": plan["plan_digest"],
        "backup_record_digest": backup["record_digest"],
        "authority_digest": authority["authority_digest"],
        "service_quiescence_digest": evidence["evidence_digest"],
        "service_quiescence_captured_at": evidence["captured_at"],
        "capture_artifact_digest": artifact["artifact_digest"],
        "capture_record_digest": artifact["record_digest"],
        "capture_issuer_digest": issuer["issuer_digest"],
        "capture_nonce": issuer["nonce"],
        "capture_source_identities_digest": issuer[
            "source_identities_digest"
        ],
        "capture_relative_path": issuer["capture_relative_path"],
        "preproduction_package_absolute_path": str(package_root.absolute()),
        "operational_backup_relative_path": operational_relative,
        "release_manifest_file_gate": manifest_gate,
        "preproduction_manifest_digest": verified["manifest_digest"],
        "backup_set_digest": verified["backup_set_digest"],
        "source_provenance_digest": (
            None
            if provenance_verification is None
            else provenance_verification["provenance_digest"]
        ),
        "source_provenance_verification_digest": (
            None
            if provenance_verification is None
            else provenance_verification["report_digest"]
        ),
        "document_sha256": {
            name: hashlib.sha256(canonical_control_document_bytes(document)).hexdigest()
            for name, document in {
                "cognitive_migration_plan.json": dict(plan),
                "cognitive_migration_backup_verification.json": backup,
                "cognitive_migration_authority.json": authority,
                "service_quiescence_evidence.json": artifact,
            }.items()
        },
        "files": sorted(_CONTROL_FILENAMES),
        "migration_executed": False,
    }
    manifest_record["manifest_digest"] = canonical_digest(manifest_record)
    return {
        "plan": dict(plan),
        "backup": backup,
        "authority": authority,
        "capture": artifact,
        "evidence": evidence,
        "manifest": manifest_record,
        "package_root": package_root,
        "release_manifest_path": manifest_path,
        "release_manifest_file_gate": manifest_gate,
        "release_manifest_identity_gate": manifest_identity_gate,
    }


def prepare_control_records(
    *,
    preproduction_package: Path,
    quiescence_capture_artifact: Mapping[str, Any],
    plan: Mapping[str, Any],
    control_root: Path,
    authority_record_path: Path,
    backup_record_path: Path,
    code_commit: str,
    authorized_at: str,
    expires_at: str,
    expected_code_state: Mapping[str, Any] | None = None,
    code_state_probe: Callable[[], Mapping[str, Any]] | None = None,
    require_fixed_production_provenance: bool = False,
) -> dict[str, Any]:
    """Prepare controls under system temp; formal publication is impossible."""

    root, expected_authority, expected_backup = _preflight_shared_control_paths(
        control_root=control_root,
        authority_record_path=authority_record_path,
        backup_record_path=backup_record_path,
    )
    documents = _build_control_documents(
        preproduction_package=preproduction_package,
        quiescence_capture_artifact=quiescence_capture_artifact,
        plan=plan,
        code_commit=code_commit,
        authorized_at=authorized_at,
        expires_at=expires_at,
        require_fixed_production_provenance=require_fixed_production_provenance,
    )
    if expected_code_state is not None or code_state_probe is not None:
        if expected_code_state is None or code_state_probe is None:
            _fail("production_control_code_state_probe_invalid")
        final_state = dict(code_state_probe())
        if final_state != dict(expected_code_state):
            _fail("production_control_code_state_changed")
    try:
        with pinned_control_directory(
            anchor=_SYSTEM_TEMP_ROOT,
            path=root,
            create_if_missing=True,
        ) as directory:
            directory.create_file(
                "cognitive_migration_plan.json",
                canonical_control_document_bytes(documents["plan"]),
            )
            directory.create_file(
                expected_backup.name,
                canonical_control_document_bytes(documents["backup"]),
            )
            directory.create_file(
                expected_authority.name,
                canonical_control_document_bytes(documents["authority"]),
            )
            directory.create_file(
                "service_quiescence_evidence.json",
                canonical_control_document_bytes(documents["capture"]),
            )
            directory.create_file(
                "control_manifest.json",
                canonical_control_document_bytes(documents["manifest"]),
            )
    except SecureControlFileError as exc:
        _fail(str(exc))
    return dict(documents["manifest"])


def _install_fixed_production_prepare_executor():
    """Seal real probes and formal paths away from imported monkeypatches."""

    fixed_project_root = PROJECT_ROOT.resolve(strict=True)
    fixed_control_root = PRODUCTION_MIGRATION_CONTROL_ROOT.absolute()
    fixed_authority_record = PRODUCTION_AUTHORITY_RECORD.absolute()
    fixed_backup_record = PRODUCTION_BACKUP_VERIFICATION_RECORD.absolute()
    fixed_database = PRODUCTION_OPERATIONAL_DATABASE.absolute()
    fixed_user_authorization_digest = USER_AUTHORIZATION_TEXT_SHA256
    fixed_capture_controller_digest = CAPTURE_CONTROLLER_DIGEST
    real_subprocess_run = subprocess.run
    real_clock = datetime.now
    utc = timezone.utc
    real_validate_capture = _FIXED_CAPTURE_ARTIFACT_VALIDATOR
    real_validate_quiescence = _validate_quiescence
    real_inspect_plan = inspect_fixed_production_cognitive_migration_plan
    real_build_documents = _build_control_documents

    def fixed_git_state() -> dict[str, Any]:
        def run(arguments: tuple[str, ...]) -> str:
            try:
                completed = real_subprocess_run(
                    ("git", "-C", str(fixed_project_root), *arguments),
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    timeout=30,
                )
            except (OSError, UnicodeError, subprocess.SubprocessError):
                _fail("production_control_git_state_unavailable")
            return completed.stdout.strip()

        tracked = run(("status", "--porcelain", "--untracked-files=no"))
        untracked = run(("ls-files", "--others", "--exclude-standard"))
        staged = run(("diff", "--cached", "--name-only"))
        return {
            "commit": run(("rev-parse", "HEAD")),
            "tracked_worktree_clean": not bool(tracked),
            "staging_empty": not bool(staged),
            "untracked_empty": not bool(untracked),
            "worktree_clean": not bool(tracked or untracked),
        }

    class _FixedProductionPrepareExecutor:
        __slots__ = ()

        def run(
            self,
            *,
            preproduction_package: Path,
            quiescence_evidence_path: Path,
            confirm_head: str,
            confirm_user_authorization_sha256: str,
        ) -> dict[str, Any]:
            if type(self) is not _FixedProductionPrepareExecutor:
                _fail("exact_production_prepare_executor_required")
            if os.name != "nt":
                _fail("production_platform_unsupported")
            if (
                confirm_user_authorization_sha256
                != fixed_user_authorization_digest
            ):
                _fail("user_authorization_confirmation_mismatch")
            if (
                fixed_authority_record
                != fixed_control_root / "cognitive_migration_authority.json"
                or fixed_backup_record
                != fixed_control_root
                / "cognitive_migration_backup_verification.json"
            ):
                _fail("production_control_fixed_path_mismatch")
            _reject_reparse_chain(
                fixed_control_root.parent,
                "production_control_parent_unsafe",
            )
            if fixed_control_root.exists() or fixed_control_root.is_symlink():
                _fail("production_control_root_exists")

            state = fixed_git_state()
            if (
                state["commit"] != confirm_head
                or state["tracked_worktree_clean"] is not True
                or state["staging_empty"] is not True
                or state["untracked_empty"] is not True
                or state["worktree_clean"] is not True
            ):
                _fail("production_control_code_state_mismatch")

            capture = real_validate_capture(quiescence_evidence_path)
            issuer = capture["artifact"]["issuer"]
            if (
                issuer["user_authorization_text_digest"]
                != confirm_user_authorization_sha256
                or issuer["controller_digest"]
                != fixed_capture_controller_digest
            ):
                _fail("capture_artifact_issuer_invalid")
            now = real_clock(utc).astimezone(utc)
            evidence = real_validate_quiescence(
                capture["evidence"],
                now=now,
                authorized_at=now,
            )
            authorized_at = now.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
            expires_at = (now + timedelta(minutes=30)).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")
            plan = real_inspect_plan()
            documents = real_build_documents(
                preproduction_package=preproduction_package,
                quiescence_capture_artifact=capture["artifact"],
                plan=plan,
                code_commit=state["commit"],
                authorized_at=authorized_at,
                expires_at=expires_at,
                require_fixed_production_provenance=True,
            )

            final_state = fixed_git_state()
            final_capture = real_validate_capture(quiescence_evidence_path)
            final_plan = real_inspect_plan()
            manifest_path = documents["release_manifest_path"]
            try:
                manifest_stat = manifest_path.stat()
                manifest_bytes = manifest_path.read_bytes()
                final_manifest_stat = manifest_path.stat()
                final_manifest_gate = {
                    "exists": manifest_path.is_file(),
                    "size": int(manifest_stat.st_size),
                    "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                }
                final_manifest_identity = {
                    "device": int(manifest_stat.st_dev),
                    "inode": int(manifest_stat.st_ino),
                    "size": int(manifest_stat.st_size),
                    "mtime_ns": int(manifest_stat.st_mtime_ns),
                    "nlink": int(manifest_stat.st_nlink),
                }
            except OSError:
                _fail("preproduction_release_manifest_invalid")
            if (
                final_state != state
                or final_capture != capture
                or dict(final_plan) != dict(plan)
                or final_manifest_gate
                != documents["release_manifest_file_gate"]
                or final_manifest_identity
                != documents["release_manifest_identity_gate"]
                or (
                    manifest_stat.st_dev,
                    manifest_stat.st_ino,
                    manifest_stat.st_size,
                    manifest_stat.st_mtime_ns,
                    manifest_stat.st_nlink,
                )
                != (
                    final_manifest_stat.st_dev,
                    final_manifest_stat.st_ino,
                    final_manifest_stat.st_size,
                    final_manifest_stat.st_mtime_ns,
                    final_manifest_stat.st_nlink,
                )
                or is_reparse_point(manifest_path)
                or int(manifest_stat.st_nlink) != 1
            ):
                _fail("production_control_inputs_changed")
            final_now = real_clock(utc).astimezone(utc)
            real_validate_quiescence(
                evidence,
                now=final_now,
                authorized_at=now,
            )
            if final_now >= now + timedelta(minutes=30):
                _fail("production_control_authority_expired")

            if fixed_control_root.exists() or fixed_control_root.is_symlink():
                _fail("production_control_root_exists")
            _reject_reparse_chain(
                fixed_control_root.parent,
                "production_control_parent_unsafe",
            )
            ordered_documents = (
                ("cognitive_migration_plan.json", documents["plan"]),
                (
                    "cognitive_migration_backup_verification.json",
                    documents["backup"],
                ),
                ("cognitive_migration_authority.json", documents["authority"]),
                ("service_quiescence_evidence.json", documents["capture"]),
                ("control_manifest.json", documents["manifest"]),
            )
            try:
                with pinned_control_directory(
                    anchor=fixed_project_root,
                    path=fixed_control_root,
                    create_if_missing=True,
                ) as directory, _completion_publication_pins(
                    documents["backup"], fixed_database
                ) as input_pins:
                    if (
                        input_pins["manifest"].size,
                        input_pins["manifest"].sha256,
                    ) != (
                        documents["backup"]["release_manifest_file_gate"][
                            "size"
                        ],
                        documents["backup"]["release_manifest_file_gate"][
                            "sha256"
                        ],
                    ) or (
                        input_pins["backup_database"].size,
                        input_pins["backup_database"].sha256,
                    ) != (
                        documents["backup"]["backup_database_size"],
                        documents["backup"]["backup_database_sha256"],
                    ) or (
                        input_pins["live_database"].size,
                        input_pins["live_database"].sha256,
                    ) != (
                        documents["plan"]["source_database_size"],
                        documents["plan"]["source_database_sha256"],
                    ):
                        _fail("production_control_inputs_changed")
                    for name, document in ordered_documents:
                        directory.create_file(
                            name, canonical_control_document_bytes(document)
                        )
                    directory.verify()
            except (
                CognitiveProductionMigrationError,
                SecureControlFileError,
            ) as exc:
                _fail(str(exc))
            return dict(documents["manifest"])

    return _FixedProductionPrepareExecutor()


_FIXED_PRODUCTION_PREPARE_EXECUTOR = _install_fixed_production_prepare_executor()
del _install_fixed_production_prepare_executor


def prepare_fixed_production_controls(
    *,
    preproduction_package: Path,
    quiescence_evidence_path: Path,
    confirm_head: str,
    confirm_user_authorization_sha256: str,
) -> dict[str, Any]:
    """Non-injectable formal entry; plan, clock, probes and paths are code-owned."""

    return _FIXED_PRODUCTION_PREPARE_EXECUTOR.run(
        preproduction_package=preproduction_package,
        quiescence_evidence_path=quiescence_evidence_path,
        confirm_head=confirm_head,
        confirm_user_authorization_sha256=confirm_user_authorization_sha256,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare fixed, one-shot YunOS v10-F migration controls"
    )
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.add_argument("--preproduction-package", type=Path, required=True)
    parser.add_argument("--quiescence-evidence", type=Path, required=True)
    parser.add_argument("--confirm-head", required=True)
    parser.add_argument(
        "--confirm-user-authorization-sha256",
        choices=(USER_AUTHORIZATION_TEXT_SHA256,),
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = prepare_fixed_production_controls(
            preproduction_package=arguments.preproduction_package,
            quiescence_evidence_path=arguments.quiescence_evidence,
            confirm_head=arguments.confirm_head,
            confirm_user_authorization_sha256=(
                arguments.confirm_user_authorization_sha256
            ),
        )
    except (ControlPreparationError, OSError, ValueError) as exc:
        print(
            canonical_json(
                {
                    "ok": False,
                    "reason_code": str(exc),
                    "migration_executed": False,
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(canonical_json({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
