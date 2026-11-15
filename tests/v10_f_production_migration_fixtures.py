from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from memory.cognitive_production_migration import (
    COGNITIVE_CONTROL_PREPARATION_VERSION,
    COGNITIVE_PRODUCTION_AUTHORITY_ID,
    COGNITIVE_PRODUCTION_AUTHORITY_ISSUER,
    COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256,
    _paths_for_clone_test,
    build_cognitive_quiescence_capture_artifact,
    build_cognitive_migration_backup_verification,
    build_cognitive_migration_plan_for_clone,
    build_cognitive_production_migration_authority,
    canonical_control_document_bytes,
    cognitive_production_execution_id,
)
from memory.operational_state_store import initialize_operational_database
from memory.operational_state_models import canonical_digest
from tools.v10_release_recovery import audit_database


T0 = "2026-08-01T00:00:00.000000Z"
T1 = "2026-08-01T00:01:00.000000Z"
T2 = "2026-08-01T00:02:00.000000Z"
T3 = "2026-08-01T00:03:00.000000Z"
T4 = "2026-08-01T00:20:00.000000Z"
NOW = datetime(2026, 8, 1, 0, 3, tzinfo=timezone.utc)
CODE_COMMIT = "a" * 40


def write_control_document(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(canonical_control_document_bytes(value))


def build_bound_fixture(root: Path) -> dict[str, Any]:
    database = root / "operational.db"
    control = root / "control"
    control.mkdir()
    initialize_operational_database(database, created_at=T0)
    plan = build_cognitive_migration_plan_for_clone(database)
    package = (root / "bound-preproduction-package").absolute()
    packaged_database = package / "databases" / "ayunos_operational_state.db"
    packaged_database.parent.mkdir(parents=True)
    shutil.copy2(database, packaged_database)
    evidence = {
        "version": "yunos-v10-f-service-quiescence-evidence-1",
        "captured_at": T1,
        "local_service_port": 8765,
        "local_service_listener_count": 0,
        "local_service_stopped": True,
        "writer_lease_count": 0,
        "operational_wal_size": 0,
        "production_write_env_enabled": False,
        "tailscale_backend": "Running",
        "tailscale_serve_config_digest": "f" * 64,
        "funnel_enabled": False,
    }
    evidence["evidence_digest"] = canonical_digest(evidence)
    source_identities = {
        "capture_controller": {
            "source_id": "capture_controller",
            "path_scope": "project",
            "path_reference": "tools/v10_f_capture_production_quiescence.py",
            "device": 1,
            "inode": 11,
            "size": 101,
            "mtime_ns": 1001,
            "sha256": "1" * 64,
        },
        "operational_database": {
            "source_id": "operational_database",
            "path_scope": "project",
            "path_reference": "data/operational/ayunos_operational_state.db",
            "device": 1,
            "inode": 12,
            "size": plan["source_database_size"],
            "mtime_ns": 1002,
            "sha256": plan["source_database_sha256"],
        },
        "tailscale_executable": {
            "source_id": "tailscale_executable",
            "path_scope": "external-path-digest",
            "path_reference": "2" * 64,
            "device": 1,
            "inode": 13,
            "size": 102,
            "mtime_ns": 1003,
            "sha256": "3" * 64,
        },
    }
    capture = build_cognitive_quiescence_capture_artifact(
        evidence=evidence,
        source_identities=source_identities,
        nonce="4" * 32,
    )
    manifest = {
        "phase": "preproduction",
        "backup_set_digest": "c" * 64,
        "service_quiescence_digest": evidence["evidence_digest"],
        "database_results": {
            "operational": {
                "backup_relative_path": "databases/ayunos_operational_state.db",
                "backup_audit": audit_database(
                    "operational-fixture", packaged_database
                ),
            }
        },
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    manifest_path = package / "release_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_bytes = manifest_path.read_bytes()
    manifest_gate = {
        "exists": True,
        "size": len(manifest_bytes),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    backup = build_cognitive_migration_backup_verification(
        plan=plan,
        backup_manifest_digest=manifest["manifest_digest"],
        backup_set_digest="c" * 64,
        backup_database_sha256=plan["source_database_sha256"],
        backup_database_size=plan["source_database_size"],
        preproduction_package_absolute_path=str(package),
        operational_backup_relative_path=(
            "databases/ayunos_operational_state.db"
        ),
        release_manifest_file_gate=manifest_gate,
        service_quiescence_digest=evidence["evidence_digest"],
        service_quiescence_captured_at=T1,
        capture_artifact_digest=capture["artifact_digest"],
        capture_record_digest=capture["record_digest"],
        capture_issuer_digest=capture["issuer"]["issuer_digest"],
        capture_nonce=capture["issuer"]["nonce"],
        capture_source_identities_digest=capture["issuer"][
            "source_identities_digest"
        ],
        capture_relative_path=capture["issuer"]["capture_relative_path"],
        verified_at=T1,
        expires_at=T4,
    )
    authority = build_cognitive_production_migration_authority(
        plan=plan,
        backup_record=backup,
        authority_id=COGNITIVE_PRODUCTION_AUTHORITY_ID,
        one_time_execution_id=cognitive_production_execution_id(
            plan_digest=plan["plan_digest"],
            backup_set_digest=backup["backup_set_digest"],
            code_commit=CODE_COMMIT,
        ),
        issued_by=COGNITIVE_PRODUCTION_AUTHORITY_ISSUER,
        code_commit=CODE_COMMIT,
        user_authorization_text_digest=(
            COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
        ),
        authorized_at=T2,
        expires_at=T4,
    )
    control_documents = {
        "cognitive_migration_plan.json": plan,
        "cognitive_migration_backup_verification.json": backup,
        "cognitive_migration_authority.json": authority,
        "service_quiescence_evidence.json": capture,
    }
    control_manifest = {
        "version": COGNITIVE_CONTROL_PREPARATION_VERSION,
        "prepared_at": T2,
        "expires_at": T4,
        "code_commit": CODE_COMMIT,
        "plan_digest": plan["plan_digest"],
        "backup_record_digest": backup["record_digest"],
        "authority_digest": authority["authority_digest"],
        "service_quiescence_digest": evidence["evidence_digest"],
        "service_quiescence_captured_at": T1,
        "capture_artifact_digest": capture["artifact_digest"],
        "capture_record_digest": capture["record_digest"],
        "capture_issuer_digest": capture["issuer"]["issuer_digest"],
        "capture_nonce": capture["issuer"]["nonce"],
        "capture_source_identities_digest": capture["issuer"][
            "source_identities_digest"
        ],
        "capture_relative_path": capture["issuer"]["capture_relative_path"],
        "preproduction_package_absolute_path": str(package),
        "operational_backup_relative_path": (
            "databases/ayunos_operational_state.db"
        ),
        "release_manifest_file_gate": manifest_gate,
        "preproduction_manifest_digest": manifest["manifest_digest"],
        "backup_set_digest": manifest["backup_set_digest"],
        "source_provenance_digest": None,
        "source_provenance_verification_digest": None,
        "document_sha256": {
            name: hashlib.sha256(
                canonical_control_document_bytes(document)
            ).hexdigest()
            for name, document in control_documents.items()
        },
        "files": sorted([*control_documents, "control_manifest.json"]),
        "migration_executed": False,
    }
    control_manifest["manifest_digest"] = canonical_digest(control_manifest)
    paths = _paths_for_clone_test(database, control)
    write_control_document(paths.plan_record, plan)
    write_control_document(paths.authority_record, authority)
    write_control_document(paths.backup_record, backup)
    write_control_document(paths.quiescence_record, capture)
    write_control_document(paths.control_manifest, control_manifest)
    return {
        "database": database,
        "control": control,
        "paths": paths,
        "plan": plan,
        "backup": backup,
        "authority": authority,
        "package": package,
        "packaged_database": packaged_database,
        "evidence": evidence,
        "capture": capture,
        "control_manifest": control_manifest,
    }


def clean_code_state() -> dict[str, Any]:
    return {
        "commit": CODE_COMMIT,
        "tracked_worktree_clean": True,
        "staging_empty": True,
        "untracked_empty": True,
        "worktree_clean": True,
    }


def clone_package_verifier(package: Path) -> dict[str, Any]:
    manifest = json.loads((package / "release_manifest.json").read_text("utf-8"))
    service_quiescence = manifest.get("service_quiescence")
    service_digest = manifest.get("service_quiescence_digest")
    if service_digest is None and isinstance(service_quiescence, dict):
        service_digest = service_quiescence.get("evidence_digest")
    return {
        "status": "verified",
        "phase": "preproduction",
        "manifest_digest": manifest["manifest_digest"],
        "backup_set_digest": manifest["backup_set_digest"],
        "service_quiescence_digest": service_digest,
    }


def clone_live_gate_probe(_database: Path) -> dict[str, Any]:
    return {
        "local_service_port": 8765,
        "local_service_listener_count": 0,
        "writer_lease_count": 0,
        "operational_wal_size": 0,
        "production_write_env_enabled": False,
    }


def clone_tailscale_gate_probe() -> dict[str, Any]:
    return {
        "tailscale_backend": "Running",
        "tailscale_serve_config_digest": "f" * 64,
        "funnel_enabled": False,
    }


def clone_execution_kwargs() -> dict[str, Any]:
    return {
        "_test_package_verifier": clone_package_verifier,
        "_test_live_gate_probe": clone_live_gate_probe,
        "_test_tailscale_gate_probe": clone_tailscale_gate_probe,
    }
