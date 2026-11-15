"""Deterministic cross-process restart recovery acceptance for YunOS v10-F.

The acceptance is intentionally disposable and offline.  A first Python
process creates a complete schema-10001 test chain, executes one bounded
read-only tool, and terminates with ``os._exit`` immediately before independent
verification.  A second Python process constructs a new Store and a new native
bounded Agent Loop from the persisted database and must finish verification
without another model send or tool execution.  The same second process also
proves that a separate interrupted run whose authorization has expired cannot
advance.

This tool never opens a production database for write and never calls a real
Provider.  The parent records read-only hashes for all five production DB files
and sidecars plus Runtime row counts before and after the disposable exercise.
No shell, network, Git, or database-write capability is exposed to the Agent.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_agent_loop import (  # noqa: E402
    AgentLoopLimits,
    BoundedCognitiveAgentLoop,
)
from memory.cognitive_command_policy import CommandTemplateRegistry  # noqa: E402
from memory.cognitive_execution_capabilities import (  # noqa: E402
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_store import CognitiveExecutionStore  # noqa: E402
from memory.cognitive_local_broker import CognitiveLocalBroker  # noqa: E402
from memory.cognitive_planning_store import CognitivePlanningStore  # noqa: E402
from memory.cognitive_supervision import ProcessSupervisor  # noqa: E402
from memory.cognitive_tool_gateway import CognitiveToolGateway  # noqa: E402
from memory.cognitive_verification import CognitiveIndependentVerifier  # noqa: E402
from memory.operational_state_models import (  # noqa: E402
    AuthorityArtifact,
    canonical_digest,
)


ACCEPTANCE_VERSION = "v10-f-cross-process-restart-recovery-2"
INTERRUPT_EXIT_CODE = 73
ACTIVE_TIME = "2026-08-01T00:08:30Z"
EXPIRED_TIME = "2026-08-01T01:00:00Z"
DATA_ROOT = (PROJECT_ROOT / "data").resolve()
PRODUCTION_DATABASES = {
    "main": (DATA_ROOT / "ayunos_chatgpt.db").resolve(),
    "profile": (DATA_ROOT / "profile" / "approved_profile.db").resolve(),
    "semantic": (DATA_ROOT / "semantic" / "ayunos_semantic.db").resolve(),
    "runtime": (DATA_ROOT / "runtime" / "ayunos_runtime.db").resolve(),
    "operational": (
        DATA_ROOT / "operational" / "ayunos_operational_state.db"
    ).resolve(),
}
PRODUCTION_DATABASE = PRODUCTION_DATABASES["operational"]
OWNER_MARKER = ".yunos-v10f-restart-recovery-owner.json"
OWNER_GUARD = ".yunos-v10f-restart-recovery-owner.guard"
TEST_PATH = PROJECT_ROOT / "tests" / "test_v10_f_restart_recovery_acceptance.py"


class _OwnedDirectory:
    """TemporaryDirectory-compatible holder whose root belongs to the parent."""

    def __init__(self, root: Path) -> None:
        self.name = str(root)

    def cleanup(self) -> None:
        return None


class _NoSendTurnSource:
    """Fail-closed source proving recovery does not request another model turn."""

    def __init__(self) -> None:
        self.prepare_count = 0

    def prepare_turn(self, **_kwargs):
        self.prepare_count += 1
        raise AssertionError("restart_recovery_model_send_forbidden")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("restart_recovery_json_object_required")
    return value


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256": None}
    return {
        "exists": True,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _production_file_snapshot() -> dict[str, Any]:
    return {
        label: {
            "database": _file_snapshot(path),
            "wal": _file_snapshot(Path(str(path) + "-wal")),
            "shm": _file_snapshot(Path(str(path) + "-shm")),
        }
        for label, path in sorted(PRODUCTION_DATABASES.items())
    }


def _runtime_counts() -> dict[str, int]:
    database = PRODUCTION_DATABASES["runtime"]
    uri = database.as_uri() + "?mode=ro"
    tables = (
        "runtime_conversations",
        "runtime_request_artifacts",
        "runtime_model_runs",
        "runtime_messages",
        "runtime_run_events",
        "runtime_errors",
        "runtime_receipts",
    )
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("restart_recovery_git_head_unavailable")
    return completed.stdout.strip()


def _copy_scenario_evidence(scenario_root: Path, evidence_root: Path) -> None:
    for source in sorted(scenario_root.iterdir(), key=lambda item: item.name):
        if not source.is_file() or source.name in {OWNER_MARKER, OWNER_GUARD}:
            continue
        shutil.copy2(source, evidence_root / source.name)


def _supporting_evidence_manifest(evidence_root: Path) -> dict[str, Any]:
    excluded = {"acceptance-report.json", "supporting-evidence-manifest.json"}
    files = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(evidence_root.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.name not in excluded
    ]
    manifest = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "files": files,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def _table_count(database: Path, table: str, run_id: str) -> int:
    allowed = {
        "tool_proposals": "run_id",
        "tool_executions": "run_id",
    }
    column = allowed.get(table)
    if column is None:
        raise RuntimeError("restart_recovery_table_not_allowlisted")
    with closing(sqlite3.connect(database)) as connection:
        return int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                (run_id,),
            ).fetchone()[0]
        )


def _verification_count(database: Path, run_id: str) -> int:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT canonical_verification FROM state_verifications"
        ).fetchall()
    count = 0
    for (canonical,) in rows:
        try:
            payload = json.loads(canonical)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError("restart_recovery_verification_corrupt") from error
        if payload.get("details", {}).get("run_id") == run_id:
            count += 1
    return count


def _assert_disposable_root(scenario_root: Path) -> Path:
    resolved = scenario_root.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        resolved == PROJECT_ROOT
        or resolved == DATA_ROOT
        or DATA_ROOT in resolved.parents
    ):
        raise RuntimeError("production_data_root_forbidden")
    if temporary_root not in resolved.parents:
        raise RuntimeError("restart_recovery_temp_root_required")
    if not resolved.name.startswith("yunos-v10f-restart-recovery-"):
        raise RuntimeError("restart_recovery_owned_prefix_required")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _owner_marker_basis(
    *, scenario_root: Path, parent_pid: int, owner_token: str
) -> dict[str, Any]:
    return {
        "acceptance_version": ACCEPTANCE_VERSION,
        "scenario_root": str(scenario_root.resolve()),
        "parent_pid": parent_pid,
        "owner_token_sha256": hashlib.sha256(owner_token.encode("utf-8")).hexdigest(),
    }


def _open_owner_guard(scenario_root: Path) -> int:
    if os.name != "nt":
        raise RuntimeError("restart_recovery_windows_guard_required")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(scenario_root / OWNER_GUARD),
        0x80000000,  # GENERIC_READ; establishes Windows share compatibility.
        0x00000001 | 0x00000002,  # SHARE_READ | SHARE_WRITE; no DELETE share
        None,
        1,  # CREATE_NEW
        0x00000100 | 0x00000002,  # TEMPORARY | HIDDEN
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    value = int(handle) if handle is not None else 0
    if value in {0, invalid}:
        raise OSError(
            ctypes.get_last_error(),
            "restart_recovery_owner_guard_open_failed",
            str(scenario_root / OWNER_GUARD),
        )
    return value


def _close_owner_guard(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise OSError(ctypes.get_last_error(), "restart_recovery_owner_guard_close_failed")


def _assert_owner_guard_held(scenario_root: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("restart_recovery_windows_guard_required")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(scenario_root / OWNER_GUARD),
        0x00010000,  # DELETE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    value = int(handle) if handle is not None else 0
    if value in {0, invalid}:
        error = ctypes.get_last_error()
        if error == 32:  # ERROR_SHARING_VIOLATION: parent still owns the guard.
            return
        raise OSError(
            error,
            "restart_recovery_owner_guard_probe_failed",
            str(scenario_root / OWNER_GUARD),
        )
    try:
        raise RuntimeError("restart_recovery_owner_guard_not_held")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(value))


def _initialize_owned_scenario_root(scenario_root: Path) -> tuple[str, int, int]:
    scenario_root = _assert_disposable_root(scenario_root)
    if any(scenario_root.iterdir()):
        raise RuntimeError("restart_recovery_owned_root_must_be_empty")
    owner_token = secrets.token_hex(32)
    parent_pid = os.getpid()
    marker = _owner_marker_basis(
        scenario_root=scenario_root,
        parent_pid=parent_pid,
        owner_token=owner_token,
    )
    marker["marker_digest"] = canonical_digest(marker)
    _write_json(scenario_root / OWNER_MARKER, marker)
    guard_handle = _open_owner_guard(scenario_root)
    return owner_token, parent_pid, guard_handle


def _validate_owned_scenario_root(
    scenario_root: Path,
    *,
    owner_token: str,
    owner_parent_pid: int,
) -> Path:
    scenario_root = _assert_disposable_root(scenario_root)
    marker = _read_json(scenario_root / OWNER_MARKER)
    expected = _owner_marker_basis(
        scenario_root=scenario_root,
        parent_pid=owner_parent_pid,
        owner_token=owner_token,
    )
    expected_digest = canonical_digest(expected)
    if marker != {**expected, "marker_digest": expected_digest}:
        raise RuntimeError("restart_recovery_owner_marker_mismatch")
    _assert_owner_guard_held(scenario_root)
    return scenario_root


def _build_new_loop(
    *,
    scenario_root: Path,
    run_id: str,
    source_authority: AuthorityArtifact,
    now: str,
) -> tuple[BoundedCognitiveAgentLoop, _NoSendTurnSource]:
    database = scenario_root / "operational.db"
    execution_store = CognitiveExecutionStore(
        database,
        trusted_clock=lambda: now,
    )
    run = execution_store.get_run(run_id)
    if run.authorization_id is None:
        raise RuntimeError("restart_recovery_authorization_missing")
    authorization = execution_store.get_authorization(run.authorization_id)
    registry = build_bounded_execution_capability_registry(
        {"project.file.read": "available"}
    )
    artifact_root = scenario_root / "agent-artifacts"
    artifact_root.mkdir(exist_ok=True)
    commands = CommandTemplateRegistry()
    gateway = CognitiveToolGateway(
        project_root=str(scenario_root),
        registry=registry,
        execution_store=execution_store,
        command_registry=commands,
        local_broker=CognitiveLocalBroker(
            project_root=scenario_root,
            artifact_root=artifact_root,
        ),
        process_supervisor=ProcessSupervisor(
            artifact_root=artifact_root,
            trusted_clock=lambda: now,
        ),
        trusted_clock=lambda: now,
    )
    verifier = CognitiveIndependentVerifier(
        project_root=scenario_root,
        artifact_root=artifact_root,
        command_registry=commands,
    )
    source = _NoSendTurnSource()
    return (
        BoundedCognitiveAgentLoop(
            execution_store=execution_store,
            planning_store=CognitivePlanningStore(
                database,
                trusted_clock=lambda: now,
            ),
            tool_gateway=gateway,
            independent_verifier=verifier,
            model_turn_source=source,
            source_authority=source_authority,
            lease_owner_id="owner:v10-execution-store",
            lease_token="v10-execution-store-lease",
            limits=AgentLoopLimits(
                maximum_steps=authorization.max_steps,
                maximum_duration_ms=authorization.max_duration_ms,
                maximum_model_calls=authorization.max_model_calls,
            ),
            trusted_clock=lambda: now,
        ),
        source,
    )


def _seed_and_interrupt(
    scenario_root: Path,
    *,
    owner_token: str,
    owner_parent_pid: int,
) -> None:
    """Run only in the first child; successful completion is forbidden."""

    from unittest.mock import patch

    import tests.test_v10_execution_store as store_fixture_module
    from tests.test_v10_agent_loop import V10BoundedAgentLoopTest

    scenario_root = _validate_owned_scenario_root(
        scenario_root,
        owner_token=owner_token,
        owner_parent_pid=owner_parent_pid,
    )
    harness = V10BoundedAgentLoopTest(methodName="runTest")
    harness.setUp()
    owned = _OwnedDirectory(scenario_root)
    with patch.object(
        store_fixture_module.tempfile,
        "TemporaryDirectory",
        return_value=owned,
    ):
        fixture = harness._fixture()

    expired_authorization, expired_run = fixture.second_run()
    expired_run = fixture.transition(expired_run, "executing")
    proposal = harness._proposal(fixture, relative_path="README.txt")
    loop, source, _artifacts = harness._loop(
        fixture,
        {fixture.action.node_id: proposal},
    )
    seed_manifest = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "old_process_pid": os.getpid(),
        "database": str(fixture.database.resolve()),
        "production_database_used": False,
        "provider": "fake",
        "real_provider_calls": 0,
        "source_authority": fixture.source_authority.to_dict(),
        "interrupted_run": {
            "run_id": fixture.run.run_id,
            "objective_digest": fixture.run.objective_digest,
            "plan_digest": fixture.run.plan_digest,
            "authorization_id": fixture.authorization.authorization_id,
            "authorization_digest": fixture.authorization.authority_digest,
            "expected_action_node_id": fixture.action.node_id,
        },
        "expired_run": {
            "run_id": expired_run.run_id,
            "run_digest": expired_run.run_digest,
            "authorization_id": expired_authorization.authorization_id,
            "authorization_digest": expired_authorization.authority_digest,
            "expires_at": expired_authorization.expires_at,
        },
    }
    _write_json(scenario_root / "seed-manifest.json", seed_manifest)

    def interrupt_before_verification(**_kwargs):
        persisted = fixture.execution_store.get_run(fixture.run.run_id)
        checkpoint = fixture.execution_store.latest_checkpoint(fixture.run.run_id)
        marker = {
            "acceptance_version": ACCEPTANCE_VERSION,
            "old_process_pid": os.getpid(),
            "exit_code": INTERRUPT_EXIT_CODE,
            "interruption_point": "after_tool_result_before_independent_verification",
            "run_id": persisted.run_id,
            "run_status": persisted.status,
            "run_digest": persisted.run_digest,
            "checkpoint_digest": checkpoint.checkpoint_digest,
            "checkpoint_sequence": checkpoint.checkpoint_sequence,
            "checkpoint_safe_to_resume": checkpoint.safe_to_resume,
            "unsafe_reason_codes": list(checkpoint.unsafe_reason_codes),
            "tool_proposal_count": _table_count(
                fixture.database, "tool_proposals", persisted.run_id
            ),
            "tool_execution_count": _table_count(
                fixture.database, "tool_executions", persisted.run_id
            ),
            "verification_count": _verification_count(
                fixture.database, persisted.run_id
            ),
            "fake_model_sends": source.total_sends,
            "real_provider_calls": 0,
        }
        _write_json(scenario_root / "interrupt-marker.json", marker)
        os._exit(INTERRUPT_EXIT_CODE)

    loop.independent_verifier.verify = interrupt_before_verification
    unexpected = loop.run(fixture.run.run_id)
    _write_json(
        scenario_root / "unexpected-old-process-outcome.json",
        {
            "status": unexpected.run.status,
            "reason_code": unexpected.reason_code,
            "model_sends": unexpected.model_sends,
            "tool_execution_count": len(
                fixture.execution_store.list_tool_executions(fixture.run.run_id)
            ),
            "verification_ids": list(unexpected.verification_ids),
        },
    )
    raise RuntimeError("old_process_failed_to_interrupt")


def _recover_in_new_process(
    scenario_root: Path,
    *,
    owner_token: str,
    owner_parent_pid: int,
) -> dict[str, Any]:
    scenario_root = _validate_owned_scenario_root(
        scenario_root,
        owner_token=owner_token,
        owner_parent_pid=owner_parent_pid,
    )
    database = scenario_root / "operational.db"
    manifest = _read_json(scenario_root / "seed-manifest.json")
    marker = _read_json(scenario_root / "interrupt-marker.json")
    authority = AuthorityArtifact.from_dict(manifest["source_authority"])
    interrupted_id = manifest["interrupted_run"]["run_id"]
    expired_id = manifest["expired_run"]["run_id"]

    loop, source = _build_new_loop(
        scenario_root=scenario_root,
        run_id=interrupted_id,
        source_authority=authority,
        now=ACTIVE_TIME,
    )
    store = loop.execution_store
    before = store.get_run(interrupted_id)
    before_checkpoint = store.latest_checkpoint(interrupted_id)
    execution_count_before = len(store.list_tool_executions(interrupted_id))
    verification_count_before = _verification_count(database, interrupted_id)
    execute_calls = 0

    def deny_tool_reexecution(**_kwargs):
        nonlocal execute_calls
        execute_calls += 1
        raise AssertionError("restart_recovery_tool_reexecution_forbidden")

    loop.tool_gateway.execute = deny_tool_reexecution
    recovered = loop.run(interrupted_id)
    after = store.get_run(interrupted_id)
    after_checkpoint = store.latest_checkpoint(interrupted_id)
    execution_count_after = len(store.list_tool_executions(interrupted_id))
    verification_count_after = _verification_count(database, interrupted_id)

    replay_loop, replay_source = _build_new_loop(
        scenario_root=scenario_root,
        run_id=interrupted_id,
        source_authority=authority,
        now=ACTIVE_TIME,
    )
    replay_execute_calls = 0

    def deny_replay_tool(**_kwargs):
        nonlocal replay_execute_calls
        replay_execute_calls += 1
        raise AssertionError("restart_replay_tool_execution_forbidden")

    replay_loop.tool_gateway.execute = deny_replay_tool
    stable_digest = replay_loop.execution_store.get_run(interrupted_id).run_digest
    replay = replay_loop.run(interrupted_id)
    stable_after = replay_loop.execution_store.get_run(interrupted_id)

    expired_loop, expired_source = _build_new_loop(
        scenario_root=scenario_root,
        run_id=expired_id,
        source_authority=authority,
        now=EXPIRED_TIME,
    )
    expired_before = expired_loop.execution_store.get_run(expired_id)
    expired_checkpoint_before = expired_loop.execution_store.latest_checkpoint(
        expired_id
    )
    expired_execution_before = len(
        expired_loop.execution_store.list_tool_executions(expired_id)
    )
    expired_outcome = expired_loop.recover(expired_id)
    expired_after = expired_loop.execution_store.get_run(expired_id)
    expired_checkpoint_after = expired_loop.execution_store.latest_checkpoint(
        expired_id
    )
    expired_execution_after = len(
        expired_loop.execution_store.list_tool_executions(expired_id)
    )

    checks = {
        "different_python_process": os.getpid() != manifest["old_process_pid"],
        "interrupted_after_successful_tool": (
            marker["run_status"] == "verifying"
            and marker["tool_execution_count"] == 1
            and marker["verification_count"] == 0
            and "tool_execution_pending_independent_verification"
            in marker["unsafe_reason_codes"]
        ),
        "new_store_constructed": type(store) is CognitiveExecutionStore,
        "new_native_agent_loop_constructed": (
            type(loop) is BoundedCognitiveAgentLoop
        ),
        "persisted_checkpoint_loaded": (
            before.status == "verifying"
            and before.run_digest == marker["run_digest"]
            and before_checkpoint.checkpoint_digest == marker["checkpoint_digest"]
        ),
        "tool_execution_not_repeated": (
            execution_count_before == 1
            and execution_count_after == 1
            and execute_calls == 0
        ),
        "independent_verification_recovered_once": (
            verification_count_before == 0
            and verification_count_after == 1
            and len(recovered.verification_ids) == 1
        ),
        "recovered_state_consistent": (
            recovered.run.status == "waiting_acceptance"
            and after.status == "waiting_acceptance"
            and after.objective_digest
            == manifest["interrupted_run"]["objective_digest"]
            and after.plan_digest == manifest["interrupted_run"]["plan_digest"]
            and after.authorization_digest
            == manifest["interrupted_run"]["authorization_digest"]
            and manifest["interrupted_run"]["expected_action_node_id"]
            in after_checkpoint.completed_node_ids
        ),
        "recovery_made_no_model_send": (
            recovered.model_sends == 0 and source.prepare_count == 0
        ),
        "terminal_replay_is_noop": (
            replay.run.status == "waiting_acceptance"
            and replay.model_sends == 0
            and replay_source.prepare_count == 0
            and replay_execute_calls == 0
            and stable_after.run_digest == stable_digest
            and len(replay_loop.execution_store.list_tool_executions(interrupted_id))
            == 1
        ),
        "expired_authorization_does_not_resume": (
            expired_outcome.reason_code
            == "authorization_expired_or_not_yet_valid"
            and expired_outcome.model_sends == 0
            and expired_source.prepare_count == 0
            and expired_after.run_digest == expired_before.run_digest
            and expired_after.status == expired_before.status
            and expired_checkpoint_after.checkpoint_digest
            == expired_checkpoint_before.checkpoint_digest
            and expired_execution_before == 0
            and expired_execution_after == 0
        ),
        "real_provider_calls_zero": (
            manifest["real_provider_calls"] == 0
            and marker["real_provider_calls"] == 0
        ),
        "production_database_not_used": database.resolve() != PRODUCTION_DATABASE,
    }
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise RuntimeError("restart_recovery_checks_failed:" + ",".join(failed))

    result = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "conclusion": "YUNOS_V10_F_RESTART_RECOVERY_GO",
        "old_process": {
            "pid": manifest["old_process_pid"],
            "exit_code": marker["exit_code"],
            "interruption_point": marker["interruption_point"],
        },
        "new_process": {
            "pid": os.getpid(),
            "store_class": type(store).__name__,
            "agent_loop_class": type(loop).__name__,
        },
        "interrupted_run": {
            "run_id": interrupted_id,
            "status_before": before.status,
            "status_after": after.status,
            "checkpoint_before": before_checkpoint.checkpoint_digest,
            "checkpoint_after": after_checkpoint.checkpoint_digest,
            "tool_executions_before": execution_count_before,
            "tool_executions_after": execution_count_after,
            "verifications_before": verification_count_before,
            "verifications_after": verification_count_after,
            "recovery_model_sends": recovered.model_sends,
            "recovery_source_prepare_calls": source.prepare_count,
            "recovery_gateway_execute_calls": execute_calls,
            "replay_gateway_execute_calls": replay_execute_calls,
        },
        "expired_authorization_run": {
            "run_id": expired_id,
            "status_before": expired_before.status,
            "status_after": expired_after.status,
            "run_digest_unchanged": (
                expired_before.run_digest == expired_after.run_digest
            ),
            "checkpoint_digest_unchanged": (
                expired_checkpoint_before.checkpoint_digest
                == expired_checkpoint_after.checkpoint_digest
            ),
            "reason_code": expired_outcome.reason_code,
            "model_sends": expired_outcome.model_sends,
            "tool_executions_before": expired_execution_before,
            "tool_executions_after": expired_execution_after,
        },
        "provider_boundary": {
            "seed_provider": "fake",
            "real_provider_calls": 0,
            "recovery_model_sends": 0,
            "network_used": False,
        },
        "database_boundary": {
            "database": str(database.resolve()),
            "production_database": str(PRODUCTION_DATABASE),
            "production_database_used": False,
        },
        "checks": checks,
    }
    result["result_digest"] = canonical_digest(result)
    _write_json(scenario_root / "new-process-recovery.json", result)
    return result


def _run_child(
    mode: str,
    scenario_root: Path,
    *,
    owner_token: str,
    owner_parent_pid: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            mode,
            "--scenario-root",
            str(scenario_root),
            "--owner-token",
            owner_token,
            "--owner-parent-pid",
            str(owner_parent_pid),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=120,
    )


def _execute(output_root: Path) -> dict[str, Any]:
    tool_path = Path(__file__).resolve()
    source_identity_before = {
        "git_head": _git_head(),
        "tool_sha256": _sha256_file(tool_path),
        "test_sha256": _sha256_file(TEST_PATH),
    }
    production_files_before = _production_file_snapshot()
    runtime_counts_before = _runtime_counts()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    evidence_root = output_root / f"run-{stamp}-{os.getpid()}"
    evidence_root.mkdir()
    with tempfile.TemporaryDirectory(
        prefix="yunos-v10f-restart-recovery-",
        ignore_cleanup_errors=True,
    ) as temporary:
        scenario_root = _assert_disposable_root(Path(temporary))
        owner_token, owner_parent_pid, owner_guard = (
            _initialize_owned_scenario_root(scenario_root)
        )
        try:
            seed = _run_child(
                "_seed",
                scenario_root,
                owner_token=owner_token,
                owner_parent_pid=owner_parent_pid,
            )
            (scenario_root / "old-process.stdout.log").write_text(
                seed.stdout, encoding="utf-8"
            )
            (scenario_root / "old-process.stderr.log").write_text(
                seed.stderr, encoding="utf-8"
            )
            if seed.returncode != INTERRUPT_EXIT_CODE:
                _copy_scenario_evidence(scenario_root, evidence_root)
                raise RuntimeError(
                    "old_process_interruption_exit_mismatch:"
                    f"expected={INTERRUPT_EXIT_CODE},actual={seed.returncode},"
                    f"stderr={seed.stderr[-1000:]}"
                )

            recovered = _run_child(
                "_recover",
                scenario_root,
                owner_token=owner_token,
                owner_parent_pid=owner_parent_pid,
            )
            (scenario_root / "new-process.stdout.log").write_text(
                recovered.stdout, encoding="utf-8"
            )
            (scenario_root / "new-process.stderr.log").write_text(
                recovered.stderr, encoding="utf-8"
            )
            if recovered.returncode != 0:
                _copy_scenario_evidence(scenario_root, evidence_root)
                raise RuntimeError(
                    "new_process_recovery_failed:"
                    f"exit={recovered.returncode},stderr={recovered.stderr[-2000:]}"
                )

            detail_path = scenario_root / "new-process-recovery.json"
            detail = _read_json(detail_path)
            database = scenario_root / "operational.db"
            _copy_scenario_evidence(scenario_root, evidence_root)
            detail_evidence_path = evidence_root / detail_path.name
            database_evidence_path = evidence_root / database.name
            detail_file_sha256 = _sha256_file(detail_evidence_path)
            database_sha256 = _sha256_file(database_evidence_path)
            database_size = database_evidence_path.stat().st_size
        finally:
            _close_owner_guard(owner_guard)

    disposable_scenario_cleaned = not scenario_root.exists()
    if not disposable_scenario_cleaned:
        raise RuntimeError("disposable_restart_recovery_root_not_cleaned")
    production_files_after = _production_file_snapshot()
    runtime_counts_after = _runtime_counts()
    runtime_count_delta = {
        name: runtime_counts_after[name] - runtime_counts_before[name]
        for name in sorted(runtime_counts_before)
    }
    production_write_detected = production_files_before != production_files_after
    source_identity_after = {
        "git_head": _git_head(),
        "tool_sha256": _sha256_file(tool_path),
        "test_sha256": _sha256_file(TEST_PATH),
    }
    parent_checks = {
        "authoritative_production_path_bound": (
            detail["database_boundary"]["production_database"]
            == str(PRODUCTION_DATABASE)
        ),
        "production_files_unchanged": not production_write_detected,
        "production_runtime_counts_unchanged": all(
            delta == 0 for delta in runtime_count_delta.values()
        ),
        "source_identity_unchanged": (
            source_identity_before == source_identity_after
        ),
        "disposable_database_preserved_for_audit": (
            database_evidence_path.is_file()
            and database_evidence_path.stat().st_size == database_size
            and _sha256_file(database_evidence_path) == database_sha256
        ),
    }
    checks = {**detail["checks"], **parent_checks}
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise RuntimeError("restart_recovery_parent_checks_failed:" + ",".join(failed))
    supporting_manifest = _supporting_evidence_manifest(evidence_root)
    supporting_manifest_path = evidence_root / "supporting-evidence-manifest.json"
    _write_json(supporting_manifest_path, supporting_manifest)
    report = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "conclusion": detail["conclusion"],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evidence_root": str(evidence_root),
        "disposable_scenario_root": str(scenario_root),
        "disposable_scenario_cleaned": disposable_scenario_cleaned,
        "old_process_exit_code": seed.returncode,
        "old_process_pid": detail["old_process"]["pid"],
        "new_process_pid": detail["new_process"]["pid"],
        "fresh_python_process": (
            detail["old_process"]["pid"] != detail["new_process"]["pid"]
        ),
        "detail_digest": detail["result_digest"],
        "detail_file_sha256": detail_file_sha256,
        "disposable_database": {
            "relative_path": database_evidence_path.name,
            "sha256": database_sha256,
            "size": database_size,
        },
        "source_identity": source_identity_after,
        "production_boundary": {
            "database_paths": {
                name: str(path) for name, path in sorted(PRODUCTION_DATABASES.items())
            },
            "files_before": production_files_before,
            "files_after": production_files_after,
            "runtime_counts_before": runtime_counts_before,
            "runtime_counts_after": runtime_counts_after,
            "runtime_count_delta": runtime_count_delta,
        },
        "supporting_evidence": {
            "manifest_digest": supporting_manifest["manifest_digest"],
            "manifest_sha256": _sha256_file(supporting_manifest_path),
            "manifest_relative_path": supporting_manifest_path.name,
            "file_count": len(supporting_manifest["files"]),
        },
        "checks": checks,
        "real_provider_calls": runtime_count_delta["runtime_model_runs"],
        "production_database_writes": int(production_write_detected),
    }
    report["report_digest"] = canonical_digest(report)
    report_path = evidence_root / "acceptance-report.json"
    _write_json(report_path, report)
    return {**report, "report_path": str(report_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "output" / "v10-f-restart-recovery-acceptance",
    )
    for command in ("_seed", "_recover"):
        child = subparsers.add_parser(command)
        child.add_argument("--scenario-root", type=Path, required=True)
        child.add_argument("--owner-token", required=True)
        child.add_argument("--owner-parent-pid", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "execute":
        result = _execute(args.output_root)
        print(
            json.dumps(
                {
                    "conclusion": result["conclusion"],
                    "report_path": result["report_path"],
                    "report_digest": result["report_digest"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "_seed":
        _seed_and_interrupt(
            args.scenario_root,
            owner_token=args.owner_token,
            owner_parent_pid=args.owner_parent_pid,
        )
        return 1
    if args.command == "_recover":
        result = _recover_in_new_process(
            args.scenario_root,
            owner_token=args.owner_token,
            owner_parent_pid=args.owner_parent_pid,
        )
        print(
            json.dumps(
                {
                    "conclusion": result["conclusion"],
                    "result_digest": result["result_digest"],
                },
                sort_keys=True,
            )
        )
        return 0
    raise RuntimeError("restart_recovery_command_unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
