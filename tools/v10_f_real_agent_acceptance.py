from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_agent_loop import (
    AgentLoopLimits,
    BoundedCognitiveAgentLoop,
)
from memory.cognitive_agent_runtime_source import (
    AgentRuntimeModelLimits,
    ProductionAgentRuntimeTurnSource,
    create_mock_agent_runtime_source,
    create_production_agent_runtime_source,
)
from memory.cognitive_authorization import issue_authorization_artifact
from memory.cognitive_command_policy import CommandTemplateRegistry
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_execution_models import create_tool_proposal
from memory.cognitive_external_send import (
    ExternalSendConsent,
    TrustedLocalControllerAuthority,
    TrustedUserEventArtifact,
    V10_F_USER_AUTHORIZATION_TEXT_SHA256,
    V10_F_USER_AUTHORIZATION_UTTERANCE,
    create_trusted_external_send_consent,
    create_trusted_user_event,
    open_v10_f_trusted_local_controller,
)
from memory.cognitive_local_broker import CognitiveLocalBroker
from memory.cognitive_objective import (
    AuthorityRequest,
    ObjectiveSourceAnchor,
    build_objective_contract,
)
from memory.cognitive_orchestration import (
    CognitiveContext,
    ContextBudget,
    ContextSource,
    assemble_cognitive_context,
    context_source_from_v7_memory_package,
    context_source_from_v9_package,
    estimate_tokens,
)
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanEdge,
    PlanNode,
    RetryPolicy,
    build_plan_graph,
    pause_plan,
    resume_plan,
)
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_path_policy import resolve_project_path
from memory.cognitive_provider import (
    ProviderHealthAttestation,
    ProviderRegistry,
    ProviderSelection,
    build_suggested_provider_registry,
)
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_runtime_bridge import (
    MockCognitiveRuntimeBridge,
    PRODUCTION_FIXED_OUTPUT_TOKENS,
)
from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    verify_cognitive_state_schema,
)
from memory.cognitive_state_store import CognitiveStateStore
from memory.cognitive_supervision import (
    ProcessSupervisor,
    create_supervision_event,
)
from memory.cognitive_tool_gateway import CognitiveToolGateway
from memory.cognitive_verification import CognitiveIndependentVerifier
from memory.db_path_policy import (
    DEFAULT_OPERATIONAL_STATE_PATH,
    DEFAULT_RUNTIME_SIDECAR_PATH,
    is_reparse_point,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.memory_package import build_production_memory_package
from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_delegation import (
    DelegationContract,
    DelegationService,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
)
from memory.operational_state_store import (
    PRODUCTION_WRITE_ENV,
    OperationalStateStore,
)
from memory.runtime_enablement import (
    open_production_deepseek_readiness,
    run_production_deepseek_readiness,
)
from memory.production_runtime_execution import (
    MockRuntimeExecutionService,
    ProductionRuntimeExecutionService,
)


ACCEPTANCE_VERSION = "v10-f-real-bounded-agent-acceptance-1"
PROJECT_ID = "project:yunos"
USER_ACTOR_ID = "user:yunos"
AGENT_ID = "agent:yunos-v10-f-native-loop"
RUNTIME_ACTOR_ID = "runtime:yunos-v8"
USER_CONSENT_UTTERANCE = V10_F_USER_AUTHORIZATION_UTTERANCE
USER_CONSENT_TEXT_SHA256 = V10_F_USER_AUTHORIZATION_TEXT_SHA256
USER_APPROVED_COMBINED_BOUNDARY_SHA256 = (
    "aaefa69451dae59d5d13df20f0a6efde03404a3a87652c71dfcacfddae5044ef"
)
PRODUCTION_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "v10-f-production-adoption"
    / "real-agent-acceptance"
).absolute()
PRODUCTION_OPERATIONAL_DB = (
    PROJECT_ROOT / "data" / "operational" / "ayunos_operational_state.db"
).absolute()
PRODUCTION_RUNTIME_DB = (
    PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
).absolute()
SENTRY_NAME = "one-shot-dispatch-sentry.json"
RESULT_NAME = "real-bounded-agent-acceptance.json"
FAILURE_NAME = "real-bounded-agent-failure.json"
SANDBOX_NAME = "disposable-git-sandbox"
AUTHORIZED_RELATIVE_ROOT = "authorized"
CREATED_RELATIVE_PATH = f"{AUTHORIZED_RELATIVE_ROOT}/bounded-agent-result.txt"
OVERREACH_RELATIVE_PATH = "outside-authorized-root.txt"
OVERREACH_BASELINE_CONTENT = "outside authorized write root; read-only probe\n"
OVERREACH_BASELINE_SHA256 = hashlib.sha256(
    OVERREACH_BASELINE_CONTENT.encode("utf-8")
).hexdigest()
OVERREACH_REPLACEMENT_CONTENT = "this write must never happen\n"
OVERREACH_REPLACEMENT_SHA256 = hashlib.sha256(
    OVERREACH_REPLACEMENT_CONTENT.encode("utf-8")
).hexdigest()
BASELINE_CONTENT = (
    "YunOS v10-F bounded native Agent acceptance baseline.\n"
    "This tracked baseline belongs only to the disposable acceptance sandbox.\n"
)
BASELINE_CONTENT_SHA256 = hashlib.sha256(
    BASELINE_CONTENT.encode("utf-8")
).hexdigest()
PATCHED_CONTENT = (
    "YunOS v10-F bounded native Agent acceptance.\n"
    "This file belongs only to the disposable acceptance sandbox.\n"
)
PATCHED_CONTENT_SHA256 = hashlib.sha256(
    PATCHED_CONTENT.encode("utf-8")
).hexdigest()
ROLLBACK_REHEARSAL_CONTENT = (
    "YunOS v10-F rollback rehearsal intermediate content.\n"
)
ROLLBACK_REHEARSAL_CONTENT_SHA256 = hashlib.sha256(
    ROLLBACK_REHEARSAL_CONTENT.encode("utf-8")
).hexdigest()
# Backwards-compatible report constants.  The action is a bound patch, not a
# create; new code must use the PATCHED_* names.
CREATED_CONTENT = PATCHED_CONTENT
CREATED_CONTENT_SHA256 = PATCHED_CONTENT_SHA256
TRUSTED_USER_EVENT_NAME = "trusted-user-event.json"
CONSENT_BINDINGS_NAME = "external-send-consent-bindings.json"
EXTERNAL_SEND_DISCLOSURE = {
    "version": "v10-f-bounded-tool-external-send-1",
    "purpose": "bounded_tool_proposal",
    "provider_id": "deepseek",
    "model": "deepseek-v4-flash",
    "maximum_model_calls": 2,
    "automatic_retry_allowed": False,
    "provider_fallback_allowed": False,
    "context_policy": "minimum_required_v7_memory_and_v9_state",
    "tool_execution_authority_from_model": False,
    "project_write_allowed_by_model": False,
    "result_acceptance_allowed": False,
}
EXTERNAL_SEND_DISCLOSURE_DIGEST = canonical_digest(EXTERNAL_SEND_DISCLOSURE)

_RUNTIME_TABLES = {
    "conversations": "runtime_conversations",
    "request_artifacts": "runtime_request_artifacts",
    "model_runs": "runtime_model_runs",
    "messages": "runtime_messages",
    "events": "runtime_run_events",
    "errors": "runtime_errors",
    "receipts": "runtime_receipts",
}
_EXPECTED_TWO_TURN_RUNTIME_DELTA = {
    "conversations": 2,
    "request_artifacts": 2,
    "model_runs": 2,
    "messages": 4,
    "events": 10,
    "errors": 0,
    "receipts": 2,
}
_EXPECTED_OPERATIONAL_DELTA: dict[str, int] = {
    "authorization_artifacts": 3,
    "capability_registry": 19,
    "cognitive_run_checkpoints": 34,
    "cognitive_runs": 3,
    "delegation_contracts": 3,
    "objective_contracts": 1,
    "outcome_records": 1,
    "plan_edges": 6,
    "plan_graphs": 7,
    "plan_nodes": 13,
    "plan_revisions": 7,
    "provider_registry": 5,
    "state_candidates": 3,
    "state_entities": 24,
    "state_events": 41,
    "state_lease_events": 1,
    "state_projections": 24,
    "state_receipts": 41,
    "state_source_anchors": 21,
    "state_verifications": 4,
    "state_writer_leases": 1,
    "tool_executions": 4,
    "tool_proposals": 5,
}

class V10FRealAgentAcceptanceError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason_code: str) -> None:
    raise V10FRealAgentAcceptanceError(reason_code)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _runtime_bridge_compatible_now() -> str:
    """Match the v8 Runtime bridge's trusted clock precision."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _trusted_event_time_after(disclosure_prepared_at: str) -> str:
    """Use the Runtime bridge's seconds precision without future dating consent."""

    prepared = normalize_timestamp(disclosure_prepared_at)
    prepared_value = datetime.fromisoformat(prepared.replace("Z", "+00:00"))
    deadline = time.monotonic() + 2.0
    while True:
        observed_value = datetime.now(timezone.utc).replace(microsecond=0)
        if observed_value > prepared_value:
            return observed_value.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        if time.monotonic() >= deadline:
            _fail("trusted_user_event_clock_boundary_timeout")
        time.sleep(0.01)


def _stamp(base: datetime, seconds: int) -> str:
    return (
        (base + timedelta(seconds=seconds))
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _write_result_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    """Dedicated finalization seam used by deterministic failure injection."""

    _write_exclusive(path, payload)


def _runtime_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {name: 0 for name in _RUNTIME_TABLES}
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        counts = {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for name, table in _RUNTIME_TABLES.items()
        }
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            _fail("runtime_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("runtime_foreign_key_check_failed")
    return counts


def _operational_snapshot(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        integrity_rows = [
            row[0] for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_key_rows = [
            tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        if integrity_rows != ["ok"]:
            _fail("operational_integrity_check_failed")
        if foreign_key_rows:
            _fail("operational_foreign_key_check_failed")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        semantic: dict[str, Any] = {}
        for table in tables:
            quoted = table.replace('"', '""')
            counts[table] = int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{quoted}"'
                ).fetchone()[0]
            )
            columns = [
                row[1]
                for row in connection.execute(
                    f'PRAGMA table_info("{quoted}")'
                )
            ]
            rows = [
                {column: row[column] for column in columns}
                for row in connection.execute(
                    f'SELECT * FROM "{quoted}" ORDER BY rowid'
                )
            ]
            semantic[table] = rows
    return {
        "counts": counts,
        "semantic_ledger_digest": canonical_digest(semantic),
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def _operational_delta(
    after: Mapping[str, Any], before: Mapping[str, Any]
) -> dict[str, int]:
    after_counts = dict(after["counts"])
    before_counts = dict(before["counts"])
    if set(after_counts) != set(before_counts):
        _fail("operational_table_set_changed")
    return {
        table: int(after_counts[table]) - int(before_counts[table])
        for table in sorted(after_counts)
        if int(after_counts[table]) != int(before_counts[table])
    }


def _count_delta(after: Mapping[str, int], before: Mapping[str, int]) -> dict[str, int]:
    return {name: int(after[name]) - int(before[name]) for name in _RUNTIME_TABLES}


def _runtime_receipt_evidence(
    *,
    target: AcceptanceTarget,
    execution_store: CognitiveExecutionStore,
    run_id: str,
    proposal_ids: Sequence[str],
) -> dict[str, Any]:
    proposals = [execution_store.get_tool_proposal(item) for item in proposal_ids]
    checkpoint = execution_store.latest_checkpoint(run_id)
    bindings = [
        dict(item)
        for item in checkpoint.runtime_recovery_bindings
        if item.get("kind") == "model_call"
    ]
    if len(bindings) != 2 or len(proposals) != 2:
        _fail("runtime_exact_two_receipt_bindings_required")
    service: Any
    if target.production:
        service = ProductionRuntimeExecutionService()
        mock_transport = False
    else:
        service = MockRuntimeExecutionService(target.runtime_db)
        mock_transport = True
    binding_by_run = {
        item["runtime_execution_reference"]: item for item in bindings
    }
    records = []
    for proposal in proposals:
        model_run_id = proposal.runtime_execution_reference
        if not isinstance(model_run_id, str) or model_run_id not in binding_by_run:
            _fail("runtime_proposal_model_run_binding_missing")
        binding = binding_by_run[model_run_id]
        receipt = service.get_run(model_run_id)
        artifact = receipt.get("request_artifact")
        if not isinstance(artifact, Mapping):
            _fail("runtime_request_artifact_missing")
        artifact_identity = artifact.get("artifact_identity")
        receipt_identity = receipt.get("receipt_identity")
        expected_bridge_digest = canonical_digest(
            {
                "runtime_model_run_id": model_run_id,
                "runtime_receipt_identity": receipt_identity,
                "runtime_artifact_identity": artifact_identity,
            }
        )
        if receipt.get("status") != "succeeded":
            _fail("runtime_receipt_status_failed")
        if receipt.get("provider") != "deepseek":
            _fail("runtime_receipt_provider_failed")
        if receipt.get("model") != "deepseek-v4-flash":
            _fail("runtime_receipt_model_failed")
        execution = receipt.get("execution", {})
        if execution.get("persisted") is not True:
            _fail("runtime_receipt_persisted_failed")
        if execution.get("sent_to_model") is not True:
            _fail("runtime_receipt_send_failed")
        if execution.get("external_model_calls") is not True:
            _fail("runtime_receipt_external_call_failed")
        if not isinstance(receipt_identity, str):
            _fail("runtime_receipt_identity_missing")
        if not isinstance(artifact_identity, str):
            _fail("runtime_artifact_identity_missing")
        if binding.get("runtime_receipt_identity") != receipt_identity:
            _fail("runtime_checkpoint_receipt_identity_mismatch")
        if binding.get("runtime_receipt_digest") != expected_bridge_digest:
            _fail("runtime_checkpoint_receipt_digest_mismatch")
        if proposal.runtime_receipt_digest != expected_bridge_digest:
            _fail("runtime_proposal_receipt_digest_mismatch")
        records.append(
            {
                "model_run_id": model_run_id,
                "receipt_identity": receipt_identity,
                "receipt_digest": canonical_digest(receipt),
                "bridge_receipt_digest": expected_bridge_digest,
                "request_artifact_identity": artifact_identity,
                "request_artifact_digest": canonical_digest(dict(artifact)),
                "provider": receipt["provider"],
                "model": receipt["model"],
                "usage": receipt.get("usage"),
                "status": receipt["status"],
            }
        )
    if len({item["model_run_id"] for item in records}) != 2:
        _fail("runtime_model_run_reuse_detected")
    return {
        "mock_transport": mock_transport,
        "model_calls": records,
        "model_run_ids": [item["model_run_id"] for item in records],
        "receipt_identities": [item["receipt_identity"] for item in records],
        "all_deepseek": True,
        "all_receipts_and_artifacts_verified": True,
    }


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _filesystem_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError):
        _fail("acceptance_path_identity_unverifiable")
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
    )


_WINDOWS_DIRECTORY_DESIRED_ACCESS = 0x00000080  # FILE_READ_ATTRIBUTES
_WINDOWS_DIRECTORY_SHARE_MODE = (
    0x00000001 | 0x00000002
)  # FILE_SHARE_READ | FILE_SHARE_WRITE


class _DirectoryHandlePins:
    """Keep trusted directories open without requesting Windows delete access."""

    def __init__(self) -> None:
        self._entries: dict[Path, dict[str, Any]] = {}
        self._retained = False

    @staticmethod
    def _open_windows_directory(path: Path) -> tuple[int, tuple[int, int]]:
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            )

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
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        )
        get_information.restype = wintypes.BOOL
        handle = create_file(
            str(path),
            _WINDOWS_DIRECTORY_DESIRED_ACCESS,
            _WINDOWS_DIRECTORY_SHARE_MODE,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        handle_value = int(handle) if handle is not None else 0
        if handle_value in {0, invalid_handle}:
            error = ctypes.get_last_error()
            raise OSError(error, "acceptance_directory_handle_open_failed", str(path))
        information = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "acceptance_directory_handle_identity_failed", str(path))
        if (
            information.dwFileAttributes & 0x00000010 == 0
            or information.dwFileAttributes & 0x00000400 != 0
        ):
            kernel32.CloseHandle(handle)
            _fail("acceptance_directory_handle_reparse_denied")
        file_index = (
            int(information.nFileIndexHigh) << 32
        ) | int(information.nFileIndexLow)
        return handle_value, (int(information.dwVolumeSerialNumber), file_index)

    def pin(self, path: Path) -> None:
        lexical = _lexical_absolute(path)
        if lexical in self._entries:
            self.assert_path(lexical)
            return
        _assert_plain_directory(
            lexical,
            reason_code="acceptance_directory_handle_reparse_denied",
        )
        path_identity = _filesystem_identity(lexical)
        if os.name == "nt":
            handle, handle_identity = self._open_windows_directory(lexical)
            entry = {
                "kind": "windows",
                "handle": handle,
                "handle_identity": handle_identity,
                "path_identity": path_identity,
            }
        else:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lexical, flags)
            observed = os.fstat(descriptor)
            if not stat.S_ISDIR(observed.st_mode):
                os.close(descriptor)
                _fail("acceptance_directory_handle_not_directory")
            entry = {
                "kind": "posix",
                "handle": descriptor,
                "handle_identity": (
                    int(observed.st_dev),
                    int(observed.st_ino),
                ),
                "path_identity": path_identity,
            }
        self._entries[lexical] = entry
        try:
            self.assert_path(lexical)
        except BaseException:
            self._close_entry(entry)
            self._entries.pop(lexical, None)
            raise

    def assert_path(self, path: Path) -> None:
        lexical = _lexical_absolute(path)
        entry = self._entries.get(lexical)
        if entry is None:
            _fail("acceptance_directory_handle_not_pinned")
        if _filesystem_identity(lexical) != entry["path_identity"]:
            _fail("acceptance_directory_handle_path_changed")
        _assert_plain_directory(
            lexical,
            reason_code="acceptance_directory_handle_reparse_denied",
        )
        if entry["kind"] == "posix":
            observed = os.fstat(entry["handle"])
            current = (int(observed.st_dev), int(observed.st_ino))
            if current != entry["handle_identity"] or not stat.S_ISDIR(
                observed.st_mode
            ):
                _fail("acceptance_directory_handle_identity_changed")

    def assert_all(self) -> None:
        for path in tuple(self._entries):
            self.assert_path(path)

    def retain(self) -> "_DirectoryHandlePins":
        self.assert_all()
        self._retained = True
        return self

    @staticmethod
    def _close_entry(entry: Mapping[str, Any]) -> None:
        if entry["kind"] == "windows":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(entry["handle"]))
        else:
            os.close(entry["handle"])

    def close(self) -> None:
        for entry in reversed(tuple(self._entries.values())):
            try:
                self._close_entry(entry)
            except OSError:
                pass
        self._entries.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


@contextmanager
def _pinned_output_directory_handles(target: "AcceptanceTarget"):
    pins = _DirectoryHandlePins()
    try:
        if target.production:
            cursor = _lexical_absolute(PROJECT_ROOT)
            pins.pin(cursor)
            relative_anchor = target.output_anchor.relative_to(cursor)
            for part in relative_anchor.parts:
                cursor = cursor / part
                pins.pin(cursor)
        else:
            pins.pin(target.output_anchor)
        pins.assert_all()
        yield pins
    finally:
        if not pins._retained:
            pins.close()


def _assert_plain_directory(path: Path, *, reason_code: str) -> None:
    try:
        if (
            not path.is_dir()
            or path.is_symlink()
            or is_reparse_point(path)
            or path.resolve(strict=True) != _lexical_absolute(path)
        ):
            _fail(reason_code)
    except (OSError, RuntimeError, ValueError):
        _fail(reason_code)


def _assert_plain_artifact(path: Path, *, reason_code: str) -> None:
    try:
        if (
            not path.is_file()
            or path.is_symlink()
            or is_reparse_point(path)
            or path.resolve(strict=True) != _lexical_absolute(path)
        ):
            _fail(reason_code)
    except (OSError, RuntimeError, ValueError):
        _fail(reason_code)


class _RetainedArtifactGuard:
    """Retain the written sentry so Windows cannot rename its parent tree."""

    def __init__(self, path: Path) -> None:
        self.path = _lexical_absolute(path)
        _assert_plain_artifact(
            self.path,
            reason_code="acceptance_guard_artifact_reparse_denied",
        )
        self.path_identity = _filesystem_identity(self.path)
        self.kind = "windows" if os.name == "nt" else "posix"
        self.handle = (
            self._open_windows_artifact(self.path)
            if self.kind == "windows"
            else self._open_posix_artifact(self.path)
        )
        try:
            self.assert_current()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _open_windows_artifact(path: Path) -> int:
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
            str(path),
            _WINDOWS_DIRECTORY_DESIRED_ACCESS,
            _WINDOWS_DIRECTORY_SHARE_MODE,
            None,
            3,
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        handle_value = int(handle) if handle is not None else 0
        if handle_value in {0, invalid_handle}:
            error = ctypes.get_last_error()
            raise OSError(error, "acceptance_guard_artifact_open_failed", str(path))
        return handle_value

    @staticmethod
    def _open_posix_artifact(path: Path) -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            os.close(descriptor)
            _fail("acceptance_guard_artifact_not_regular")
        return descriptor

    def assert_current(self) -> None:
        if _filesystem_identity(self.path) != self.path_identity:
            _fail("acceptance_guard_artifact_identity_changed")
        _assert_plain_artifact(
            self.path,
            reason_code="acceptance_guard_artifact_reparse_denied",
        )
        if self.kind == "posix":
            observed = os.fstat(self.handle)
            current = (
                int(observed.st_dev),
                int(observed.st_ino),
                int(observed.st_mode),
                int(observed.st_nlink),
            )
            if current != self.path_identity or not stat.S_ISREG(observed.st_mode):
                _fail("acceptance_guard_artifact_handle_changed")

    def close(self) -> None:
        handle = self.handle
        if handle is None:
            return
        self.handle = None
        if self.kind == "windows":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            os.close(handle)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _pinned_output_anchor(output_root: Path) -> tuple[Path, tuple[int, int, int, int]]:
    project = _lexical_absolute(PROJECT_ROOT)
    output = _lexical_absolute(output_root)
    try:
        output.relative_to(project)
    except ValueError:
        _fail("production_acceptance_output_outside_project")
    current = output.parent
    while not current.exists():
        if current == project or current.parent == current:
            _fail("production_acceptance_output_parent_unavailable")
        current = current.parent
    relative = current.relative_to(project)
    cursor = project
    _assert_plain_directory(cursor, reason_code="production_acceptance_reparse_denied")
    for part in relative.parts:
        cursor = cursor / part
        _assert_plain_directory(cursor, reason_code="production_acceptance_reparse_denied")
    return current, _filesystem_identity(current)


def _pinned_test_output_anchor(output_root: Path) -> tuple[Path, tuple[int, int, int, int]]:
    current = _lexical_absolute(output_root).parent
    while not current.exists():
        if current.parent == current:
            _fail("acceptance_output_parent_unavailable")
        current = current.parent
    _assert_plain_directory(current, reason_code="acceptance_output_reparse_denied")
    return current, _filesystem_identity(current)


def _assert_fixed_production_path(path: Path, expected: Path) -> None:
    lexical = _lexical_absolute(path)
    if lexical != _lexical_absolute(expected) or _resolved(lexical) != lexical:
        _fail("production_acceptance_path_mismatch")
    project = _lexical_absolute(PROJECT_ROOT)
    try:
        relative = lexical.relative_to(project)
    except ValueError:
        _fail("production_acceptance_path_mismatch")
    cursor = project
    _assert_plain_directory(cursor, reason_code="production_acceptance_reparse_denied")
    for part in relative.parts[:-1]:
        cursor = cursor / part
        _assert_plain_directory(cursor, reason_code="production_acceptance_reparse_denied")
    if lexical.exists() and (lexical.is_symlink() or is_reparse_point(lexical)):
        _fail("production_acceptance_reparse_denied")


def _create_pinned_output_root(
    target: "AcceptanceTarget",
    *,
    directory_pins: _DirectoryHandlePins | None = None,
) -> None:
    if directory_pins is not None:
        directory_pins.assert_path(target.output_anchor)
    anchor = target.output_anchor
    if _filesystem_identity(anchor) != target.output_anchor_identity:
        _fail("acceptance_output_parent_identity_changed")
    _assert_plain_directory(anchor, reason_code="acceptance_output_reparse_denied")
    relative = target.output_root.relative_to(anchor)
    parent = anchor
    parent_identity = target.output_anchor_identity
    for index, part in enumerate(relative.parts):
        if directory_pins is not None:
            directory_pins.assert_path(parent)
        if _filesystem_identity(parent) != parent_identity:
            _fail("acceptance_output_parent_identity_changed")
        child = parent / part
        final = index == len(relative.parts) - 1
        try:
            child.mkdir(exist_ok=not final)
        except FileExistsError:
            _fail("real_agent_output_root_already_exists")
        _assert_plain_directory(child, reason_code="acceptance_output_reparse_denied")
        if directory_pins is not None:
            directory_pins.pin(child)
        if _filesystem_identity(parent) != parent_identity:
            _fail("acceptance_output_parent_identity_changed")
        parent = child
        parent_identity = _filesystem_identity(parent)
    object.__setattr__(target, "output_root_identity", parent_identity)
    if directory_pins is not None:
        directory_pins.assert_path(target.output_root)
    _assert_pinned_output_root(target)


def _assert_pinned_output_root(target: "AcceptanceTarget") -> None:
    expected_identity = target.output_root_identity
    if expected_identity is None:
        _fail("acceptance_output_root_not_pinned")
    if _filesystem_identity(target.output_anchor) != target.output_anchor_identity:
        _fail("acceptance_output_parent_identity_changed")
    _assert_plain_directory(
        target.output_anchor,
        reason_code="acceptance_output_reparse_denied",
    )
    if _filesystem_identity(target.output_root) != expected_identity:
        _fail("acceptance_output_root_identity_changed")
    _assert_plain_directory(
        target.output_root,
        reason_code="acceptance_output_reparse_denied",
    )
    relative = target.output_root.relative_to(target.output_anchor)
    cursor = target.output_anchor
    for part in relative.parts:
        cursor = cursor / part
        _assert_plain_directory(
            cursor,
            reason_code="acceptance_output_reparse_denied",
        )
    if _filesystem_identity(target.output_root) != expected_identity:
        _fail("acceptance_output_root_identity_changed")


def _write_pinned_output_exclusive(
    *,
    target: "AcceptanceTarget",
    name: str,
    payload: Mapping[str, Any],
) -> None:
    if not name or Path(name).name != name:
        _fail("acceptance_output_artifact_name_invalid")
    _assert_pinned_output_root(target)
    _write_exclusive(target.output_root / name, payload)
    _assert_pinned_output_root(target)


def _write_pinned_result_exclusive(
    *,
    target: "AcceptanceTarget",
    payload: Mapping[str, Any],
) -> None:
    """Dedicated finalization seam used by deterministic failure injection."""

    _assert_pinned_output_root(target)
    _write_result_exclusive(target.output_root / RESULT_NAME, payload)
    _assert_pinned_output_root(target)


@dataclass(frozen=True)
class AcceptanceTarget:
    operational_db: Path
    runtime_db: Path
    output_root: Path
    production: bool
    output_anchor: Path = field(init=False, repr=False)
    output_anchor_identity: tuple[int, int, int, int] = field(init=False, repr=False)
    output_root_identity: tuple[int, int, int, int] | None = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        requested_operational = _lexical_absolute(self.operational_db)
        requested_runtime = _lexical_absolute(self.runtime_db)
        requested_output = _lexical_absolute(self.output_root)
        operational = _resolved(requested_operational)
        runtime = _resolved(requested_runtime)
        output = _resolved(requested_output)
        object.__setattr__(self, "operational_db", operational)
        object.__setattr__(self, "runtime_db", runtime)
        object.__setattr__(self, "output_root", output)
        if self.production:
            _assert_fixed_production_path(requested_operational, PRODUCTION_OPERATIONAL_DB)
            _assert_fixed_production_path(requested_operational, DEFAULT_OPERATIONAL_STATE_PATH)
            _assert_fixed_production_path(requested_runtime, PRODUCTION_RUNTIME_DB)
            _assert_fixed_production_path(requested_runtime, DEFAULT_RUNTIME_SIDECAR_PATH)
            if requested_output != _lexical_absolute(PRODUCTION_OUTPUT_ROOT):
                _fail("production_acceptance_path_mismatch")
            anchor, identity = _pinned_output_anchor(requested_output)
            object.__setattr__(self, "operational_db", requested_operational)
            object.__setattr__(self, "runtime_db", requested_runtime)
            object.__setattr__(self, "output_root", requested_output)
        else:
            temporary = Path(tempfile.gettempdir()).resolve(strict=True)
            for value in (operational, runtime, output):
                try:
                    value.relative_to(temporary)
                except ValueError:
                    _fail("mock_acceptance_requires_system_temp")
            anchor, identity = _pinned_test_output_anchor(output)
        object.__setattr__(self, "output_anchor", anchor)
        object.__setattr__(self, "output_anchor_identity", identity)


@dataclass(frozen=True)
class ProviderSetup:
    registry: ProviderRegistry
    selection: ProviderSelection
    readiness_digest: str


@dataclass(frozen=True)
class ControllerConfirmation:
    execute: bool
    allow_production_operational_write: bool
    allow_official_readiness_gets: bool
    confirm_one_shot: bool
    confirm_two_deepseek_tool_proposals: bool
    confirm_disposable_sandbox: bool
    user_authorization_sha256: str
    combined_boundary_sha256: str
    production: bool

    def __post_init__(self) -> None:
        values = (
            self.execute,
            self.allow_production_operational_write,
            self.allow_official_readiness_gets,
            self.confirm_one_shot,
            self.confirm_two_deepseek_tool_proposals,
            self.confirm_disposable_sandbox,
        )
        if any(value is not True for value in values):
            _fail("controller_confirmation_incomplete")
        if self.user_authorization_sha256 != USER_CONSENT_TEXT_SHA256:
            _fail("controller_user_authorization_sha256_mismatch")
        if self.combined_boundary_sha256 != USER_APPROVED_COMBINED_BOUNDARY_SHA256:
            _fail("controller_combined_boundary_sha256_mismatch")
        if not isinstance(self.production, bool):
            _fail("controller_confirmation_mode_invalid")

    @property
    def binding_digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "execute": self.execute,
            "allow_production_operational_write": (
                self.allow_production_operational_write
            ),
            "allow_official_readiness_gets": self.allow_official_readiness_gets,
            "confirm_one_shot": self.confirm_one_shot,
            "confirm_two_deepseek_tool_proposals": (
                self.confirm_two_deepseek_tool_proposals
            ),
            "confirm_disposable_sandbox": self.confirm_disposable_sandbox,
            "user_authorization_sha256": self.user_authorization_sha256,
            "combined_boundary_sha256": self.combined_boundary_sha256,
            "production": self.production,
        }


class TrustedConsentLedger:
    def __init__(
        self,
        *,
        confirmation: ControllerConfirmation,
        objective_id: str,
        objective_digest: str,
        controller_binding: Mapping[str, Any],
    ) -> None:
        self.confirmation = confirmation
        self.objective_id = objective_id
        self.objective_digest = objective_digest
        self.controller_binding = dict(controller_binding)
        self.controller_binding_digest = canonical_digest(
            self.controller_binding
        )
        issued_at = _utc_now()
        self.controller = open_v10_f_trusted_local_controller(
            user_authorization_text_sha256=USER_CONSENT_TEXT_SHA256,
            cli_confirmation_digest=self.controller_binding_digest,
            issued_at=issued_at,
            expires_at=_stamp(
                datetime.fromisoformat(issued_at.replace("Z", "+00:00")),
                1_200,
            ),
            confirm_exact_cli=True,
            confirm_two_model_calls=True,
            confirm_do_not_rerun=True,
        )
        if (
            type(self.controller) is not TrustedLocalControllerAuthority
            or self.controller.authority_digest
            != canonical_digest(self.controller.identity_payload())
            or self.controller.cli_confirmation_digest
            != self.controller_binding_digest
            or self.controller.user_authorization_text_sha256
            != USER_CONSENT_TEXT_SHA256
        ):
            _fail("trusted_local_controller_integrity_failed")
        self.records: list[dict[str, Any]] = []
        self.last_error_code: str | None = None

    def __call__(self, disclosure, selection, request, reservation):
        try:
            return self._create(disclosure, selection, request, reservation)
        except BaseException as error:
            self.last_error_code = getattr(
                error, "reason_code", type(error).__name__
            )
            raise

    def _create(self, disclosure, selection, request, reservation):
        sequence = len(self.records) + 1
        observed_at = _trusted_event_time_after(disclosure.prepared_at)
        turn_binding = {
            "controller_confirmation_digest": self.controller_binding_digest,
            "objective_id": self.objective_id,
            "objective_digest": self.objective_digest,
            "turn_sequence": sequence,
            "request_digest": request.request_digest,
            "reservation_id": reservation.reservation_id,
            "runtime_disclosure_digest": disclosure.disclosure_digest,
        }
        consent_id = make_identity(
            "consent",
            {
                "controller_authority_digest": self.controller.authority_digest,
                "reservation_id": reservation.reservation_id,
                "runtime_disclosure_digest": disclosure.disclosure_digest,
                "sequence": sequence,
            },
        )
        event = create_trusted_user_event(
            controller=self.controller,
            event_id=make_identity(
                "trusteduserevent",
                {
                    **turn_binding,
                    "intended_consent_id": consent_id,
                    "observed_at": observed_at,
                },
            ),
            user_utterance=USER_CONSENT_UTTERANCE,
            runtime_disclosure_digest=disclosure.disclosure_digest,
            confirmation_binding_digest=self.controller_binding_digest,
            intended_consent_id=consent_id,
            sequence=sequence,
            observed_at=observed_at,
        )
        if type(event) is not TrustedUserEventArtifact:
            _fail("trusted_user_event_exact_type_failed")
        if event.event_digest != canonical_digest(event.identity_payload()):
            _fail("trusted_user_event_digest_failed")
        if event.user_utterance_sha256 != USER_CONSENT_TEXT_SHA256:
            _fail("trusted_user_event_utterance_digest_failed")
        if event.trusted_user_event_verified is not True:
            _fail("trusted_user_event_verification_failed")
        if datetime.fromisoformat(event.observed_at.replace("Z", "+00:00")) <= (
            datetime.fromisoformat(disclosure.prepared_at.replace("Z", "+00:00"))
        ):
            _fail("trusted_user_event_not_after_disclosure")
        consent = create_trusted_external_send_consent(
            consent_id=consent_id,
            trusted_user_event=event,
            disclosure_digest=EXTERNAL_SEND_DISCLOSURE_DIGEST,
            selection=selection,
            request=request,
            reservation=reservation,
            **disclosure.consent_binding(),
            expires_at=disclosure.expires_at,
        )
        if (
            type(consent) is not ExternalSendConsent
            or consent.consent_digest
            != canonical_digest(consent.identity_payload())
            or consent.trusted_user_event_verified is not True
            or consent.trusted_user_event_digest != event.event_digest
            or consent.evidence_source
            != "trusted_local_operator_cli_confirmation"
        ):
            _fail("trusted_external_send_consent_binding_failed")
        self.records.append(
            {
                "turn_sequence": sequence,
                "turn_binding": turn_binding,
                "confirmation_binding_digest": self.controller_binding_digest,
                "trusted_user_event": event.to_dict(),
                "external_send_consent": consent.to_dict(),
            }
        )
        return consent

    def assert_complete(self, expected: int = 2) -> tuple[Mapping[str, Any], ...]:
        if len(self.records) != expected:
            _fail("trusted_external_send_consent_count_mismatch")
        event_ids = {
            item["trusted_user_event"]["event_id"] for item in self.records
        }
        consent_ids = {
            item["external_send_consent"]["consent_id"]
            for item in self.records
        }
        if len(event_ids) != expected or len(consent_ids) != expected:
            _fail("trusted_external_send_consent_reuse_detected")
        if [item["turn_sequence"] for item in self.records] != [1, 2]:
            _fail("trusted_user_event_sequence_mismatch")
        disclosure_digests: set[str] = set()
        for item in self.records:
            event = dict(item["trusted_user_event"])
            consent = dict(item["external_send_consent"])
            event_digest = event.pop("event_digest", None)
            consent_digest = consent.pop("consent_digest", None)
            if (
                event.get("user_utterance") != USER_CONSENT_UTTERANCE
                or event.get("user_utterance_sha256")
                != USER_CONSENT_TEXT_SHA256
                or event.get("trusted_user_event_verified") is not True
                or event.get("evidence_source")
                != "trusted_local_operator_cli_confirmation"
                or event_digest != canonical_digest(event)
            ):
                _fail("trusted_user_event_record_integrity_mismatch")
            if (
                consent.get("user_utterance") != USER_CONSENT_UTTERANCE
                or consent.get("trusted_user_event_verified") is not True
                or consent.get("trusted_user_event_digest") != event_digest
                or consent.get("evidence_source")
                != "trusted_local_operator_cli_confirmation"
                or consent_digest != canonical_digest(consent)
            ):
                _fail("trusted_external_send_consent_record_integrity_mismatch")
            if datetime.fromisoformat(
                event["observed_at"].replace("Z", "+00:00")
            ) <= datetime.fromisoformat(
                consent["runtime_disclosure_prepared_at"].replace(
                    "Z", "+00:00"
                )
            ):
                _fail("trusted_user_event_not_after_disclosure")
            disclosure_digests.add(consent["runtime_disclosure_digest"])
        if len(disclosure_digests) != expected:
            _fail("trusted_external_send_disclosure_reuse_detected")
        if any(
            item["confirmation_binding_digest"]
            != self.controller_binding_digest
            or item["trusted_user_event"]["controller_authority_digest"]
            != self.controller.authority_digest
            or item["trusted_user_event"]["intended_consent_id"]
            != item["external_send_consent"]["consent_id"]
            for item in self.records
        ):
            _fail("trusted_user_event_authority_binding_mismatch")
        return tuple(self.records)


@dataclass(frozen=True)
class AcceptanceContracts:
    anchor: SourceAnchor
    upstream_authority: AuthorityArtifact
    authority: AuthorityArtifact
    objective: Any
    capability_registry: Any
    provider_registry: ProviderRegistry
    provider_selection: ProviderSelection
    patch_requirement: CapabilityRequirement
    check_requirement: CapabilityRequirement
    rollback_requirement: CapabilityRequirement
    plan: Any
    plan_history: tuple[Any, ...]
    rollback_plan: Any
    rollback_plan_history: tuple[Any, ...]
    overreach_plan: Any
    delegation: DelegationContract
    rollback_delegation: DelegationContract
    overreach_delegation: DelegationContract
    authorization: Any
    rollback_authorization: Any
    overreach_authorization: Any
    event_times: Mapping[str, str]


SourceBuilder = Callable[..., ProductionAgentRuntimeTurnSource]


def build_deepseek_provider_setup(
    readiness_evidence: Mapping[str, Any],
    *,
    checked_at: str | None = None,
) -> ProviderSetup:
    registry = build_suggested_provider_registry(("secret.deepseek.production",))
    profile = registry.get("deepseek", 1)
    checked = normalize_timestamp(checked_at or _utc_now())
    expires = _stamp(
        datetime.fromisoformat(checked.replace("Z", "+00:00")),
        600,
    )
    evidence_digest = canonical_digest(dict(readiness_evidence))
    health = ProviderHealthAttestation(
        provider_id=profile.provider_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        health_state="healthy",
        checked_at=checked,
        expires_at=expires,
        evidence_digest=evidence_digest,
    )
    selection = registry.manual_select(
        provider_id=profile.provider_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        health_attestation=health,
        evaluated_at=checked,
    )
    return ProviderSetup(
        registry=registry,
        selection=selection,
        readiness_digest=evidence_digest,
    )


def _upstream_authority(base: datetime, sandbox_root: Path) -> AuthorityArtifact:
    """User project scope; deliberately grants no ledger or Git commit power."""

    sandbox = str(sandbox_root.resolve(strict=False))
    authorized_write = str(
        (sandbox_root / AUTHORIZED_RELATIVE_ROOT).resolve(strict=False)
    )
    return AuthorityArtifact(
        authority_id="authority:yunos-v10-f-user-project-scope",
        issued_by=USER_ACTOR_ID,
        issued_at=_stamp(base, -90),
        expires_at=_stamp(base, 3600),
        target_project_id=PROJECT_ID,
        allowed_event_types=("decision_approved",),
        read_scope=(sandbox,),
        write_scope=(authorized_write,),
        prohibited_operations=(
            "commit",
            "merge",
            "tag",
            "push",
            "start_next_version",
            "create-next-node",
        ),
        commit_permission=False,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
    )


def _persistence_authority(base: datetime) -> AuthorityArtifact:
    return AuthorityArtifact(
        authority_id="authority:yunos-v10-f-real-agent",
        issued_by=USER_ACTOR_ID,
        issued_at=_stamp(base, -90),
        expires_at=_stamp(base, 3600),
        target_project_id=PROJECT_ID,
        allowed_event_types=(
            "decision_approved",
            "capability_registry_install",
            "provider_registry_install",
            "plan_created",
            "plan_revised",
            "delegation_proposed",
            "delegation_authorized",
            "delegation_queued",
            "delegation_running",
            "delegation_completed",
            "delegation_failed",
            "delegation_cancelled",
            "delegation_overreach",
            "delegation_contract_register",
            "outcome_reported",
            "outcome_projection_update",
            "outcome_verified",
            "candidate_create",
            "authorization_artifact_issued",
            "cognitive_run_created",
            "cognitive_run_update",
            "model_call_reserved",
            "model_call_settled",
            "model_call_recovery_required",
            "tool_proposal_record",
            "tool_execution_reserved",
            "tool_execution_record",
            "verification_record",
        ),
        read_scope=("*",),
        write_scope=("*",),
        prohibited_operations=(
            "merge",
            "tag",
            "push",
            "start_next_version",
            "create-next-node",
        ),
        commit_permission=True,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
    )


def _build_contracts(
    *,
    sandbox_root: Path,
    provider_setup: ProviderSetup,
    base: datetime,
) -> AcceptanceContracts:
    sandbox_text = str(sandbox_root.absolute())
    authorized_write_text = str(
        (sandbox_root / AUTHORIZED_RELATIVE_ROOT).absolute()
    )
    upstream_authority = _upstream_authority(base, sandbox_root)
    times = {
        "anchor": _stamp(base, -80),
        "objective": _stamp(base, -70),
        "plan": _stamp(base, -60),
        "plan_pause": _stamp(base, -58),
        "plan_resume": _stamp(base, -56),
        "rollback_plan": _stamp(base, -55),
        "rollback_plan_pause": _stamp(base, -54),
        "rollback_plan_resume": _stamp(base, -53),
        "overreach_plan": _stamp(base, -52),
        "delegation": _stamp(base, -50),
        "delegation_authorized": _stamp(base, -48),
        "delegation_queued": _stamp(base, -46),
        "delegation_running": _stamp(base, -44),
        "overreach": _stamp(base, -42),
        "overreach_authorized": _stamp(base, -40),
        "overreach_queued": _stamp(base, -38),
        "overreach_running": _stamp(base, -36),
        "rollback_delegation": _stamp(base, -35),
        "rollback_delegation_authorized": _stamp(base, -34),
        "rollback_delegation_queued": _stamp(base, -33),
        "rollback_delegation_running": _stamp(base, -32),
        "authorization": _stamp(base, -20),
        "expires": _stamp(base, 3500),
    }
    anchor_evidence = {
        "user_utterance": USER_CONSENT_UTTERANCE,
        "user_utterance_sha256": USER_CONSENT_TEXT_SHA256,
        "upstream_authority_digest": upstream_authority.authority_digest,
        "purpose": "v10-F real bounded native Agent acceptance",
        "sandbox_root": sandbox_text,
        "maximum_model_calls": 2,
    }
    anchor = ObjectiveSourceAnchor(
        anchor_id="anchor:user:v10-f-real-agent",
        source_kind="user_declaration",
        locator="acceptance:v10-f-real-bounded-agent",
        evidence_digest=canonical_digest(anchor_evidence),
        observed_at=times["anchor"],
        privacy_scope="private_metadata",
    )
    # Operational stores currently require ledger commit authority.  This is a
    # separate compatibility authority and must never be presented as the
    # user's upstream project authorization.
    authority = _persistence_authority(base)
    objective = build_objective_contract(
        user_utterance=(
            "在独立沙箱中运行一次真实 Provider 驱动的受控 Agent 小任务，并等待接受。"
        ),
        interpreted_goal=(
            "使用两个受限 Plan action 创建一份沙箱文件并运行固定 git diff check；"
            "所有工具均由 YunOS Tool Gateway 执行和独立验证。"
        ),
        project_id=PROJECT_ID,
        source_anchors=(anchor,),
        success_criteria=(
            "模型仅提出两个与 Plan node 精确绑定的 typed tool proposal。",
            "Tool Gateway 在授权沙箱中创建文件并执行固定 git diff check。",
            "系统独立验证两个工具结果，最终状态为 waiting_acceptance。",
            "重放不发送模型请求且不重复工具执行。",
        ),
        prohibited_outcomes=(
            "不得写入沙箱以外的路径。",
            "不得运行任意 shell、访问网络、写数据库或读取秘密。",
            "不得 commit、merge、tag、push 或创建下一版本。",
            "不得把 Agent 报告或系统验证当作用户接受。",
        ),
        agency_level="act",
        agency_selected_by="user",
        maximum_authorized_agency_level="act",
        agency_selection_source_anchor_id=anchor.anchor_id,
        cognitive_stance="continue",
        risk_tolerance="medium",
        resource_constraints=(
            "最多两个模型调用且不自动重试。",
            "只允许一个独立沙箱根目录。",
        ),
        authority_request=AuthorityRequest(
            requested=True,
            read_roots=(sandbox_text,),
            write_roots=(authorized_write_text,),
            capabilities=(
                "project.file.patch",
                "process.template.run",
                "project.change.rollback",
            ),
            commands=("git.diff.check.v1",),
            risk_ceiling="medium",
            database_permission="none",
        ),
    )
    capability_registry = build_bounded_execution_capability_registry(
        {
            "project.file.patch": "available",
            "process.template.run": "available",
            "project.change.rollback": "available",
        }
    )
    patch_profile = capability_registry.lookup("project.file.patch", version=1)
    check_profile = capability_registry.lookup("process.template.run", version=1)
    rollback_profile = capability_registry.lookup(
        "project.change.rollback", version=1
    )
    patch_requirement = CapabilityRequirement(
        capability_id=patch_profile.capability_id,
        capability_version=patch_profile.version,
        capability_digest=patch_profile.capability_digest,
    )
    check_requirement = CapabilityRequirement(
        capability_id=check_profile.capability_id,
        capability_version=check_profile.version,
        capability_digest=check_profile.capability_digest,
    )
    rollback_requirement = CapabilityRequirement(
        capability_id=rollback_profile.capability_id,
        capability_version=rollback_profile.version,
        capability_digest=rollback_profile.capability_digest,
    )
    patch_node = PlanNode(
        node_id="node:v10-f-real-agent-patch",
        objective_id=objective.objective_id,
        title="Patch one tracked sandbox file",
        purpose="Patch only the exact tracked acceptance artifact inside the disposable root.",
        capability_requirements=(patch_requirement,),
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        risk=patch_profile.risk_class,
        reversible=True,
        authority_requirement="explicit_act",
        success_criteria=(
            "The tracked baseline is replaced only when both before and after SHA-256 digests match.",
        ),
        verification=(
            "Independently compare both file digests and retain the owned backup receipt.",
        ),
        rollback=("Use only project.change.rollback with the owned backup receipt.",),
        timeout_ms=10_000,
        retry_policy=RetryPolicy(
            maximum_attempts=1,
            equivalent_retry_limit=0,
            backoff_ms=0,
            retryable_failures=(),
        ),
    )
    check_node = PlanNode(
        node_id="node:v10-f-real-agent-diff-check",
        objective_id=objective.objective_id,
        title="Run the fixed git diff check",
        purpose="Invoke only the code-owned git.diff.check command template.",
        dependencies=(patch_node.node_id,),
        capability_requirements=(check_requirement,),
        read_scope=(sandbox_text,),
        risk=check_profile.risk_class,
        reversible=True,
        authority_requirement="read",
        success_criteria=("The fixed git diff check exits with code zero.",),
        verification=("Verify resolved argv, exit code, and bounded output digests.",),
        timeout_ms=20_000,
        retry_policy=RetryPolicy(
            maximum_attempts=1,
            equivalent_retry_limit=0,
            backoff_ms=0,
            retryable_failures=(),
        ),
    )
    edge = PlanEdge(
        edge_id="edge:v10-f-create-before-diff-check",
        from_node_id=patch_node.node_id,
        to_node_id=check_node.node_id,
        edge_kind="sequence",
        condition=None,
        priority=0,
    )
    initial_plan = build_plan_graph(
        objective=objective,
        nodes=(patch_node, check_node),
        edges=(edge,),
        change_reason="Create the exact two-node v10-F bounded Agent acceptance plan.",
        created_at=times["plan"],
        plan_id="plan:v10-f-real-agent",
    )
    paused_plan = pause_plan(
        initial_plan,
        objective=objective,
        node_ids=(patch_node.node_id, check_node.node_id),
        reason="Persist the user-authorized scheduling boundary before execution.",
        created_at=times["plan_pause"],
        created_by="agent",
    )
    plan = resume_plan(
        paused_plan,
        objective=objective,
        node_ids=(patch_node.node_id, check_node.node_id),
        reason="The explicit v10-F act authorization makes both nodes ready.",
        created_at=times["plan_resume"],
        created_by="user",
    )
    rollback_seed_node = PlanNode(
        node_id="node:v10-f-owned-rollback-seed",
        objective_id=objective.objective_id,
        title="Create one run-owned rollback seed",
        purpose=(
            "Patch the already-patched sandbox artifact through Tool Gateway "
            "so its backup receipt is owned by this drill run."
        ),
        capability_requirements=(patch_requirement,),
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        risk=patch_profile.risk_class,
        reversible=True,
        authority_requirement="explicit_act",
        success_criteria=(
            "The intermediate digest and run-owned backup receipt are persisted.",
        ),
        verification=(
            "Independently verify the intermediate bytes and backup reference.",
        ),
        rollback=("Use the dependent project.change.rollback node only.",),
        timeout_ms=10_000,
        retry_policy=RetryPolicy(
            maximum_attempts=1,
            equivalent_retry_limit=0,
            backoff_ms=0,
            retryable_failures=(),
        ),
    )
    rollback_node = PlanNode(
        node_id="node:v10-f-owned-rollback-rehearsal",
        objective_id=objective.objective_id,
        title="Restore one run-owned rollback backup",
        purpose="Verify project.change.rollback against a backup owned by the drill run.",
        dependencies=(rollback_seed_node.node_id,),
        capability_requirements=(rollback_requirement,),
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        risk=rollback_profile.risk_class,
        reversible=True,
        authority_requirement="explicit_act",
        success_criteria=("The exact pre-drill patched SHA-256 digest is restored.",),
        verification=("Independently verify backup ownership and restored bytes.",),
        rollback=("Stop if the run-owned backup cannot be verified.",),
        timeout_ms=10_000,
        retry_policy=RetryPolicy(
            maximum_attempts=1,
            equivalent_retry_limit=0,
            backoff_ms=0,
            retryable_failures=(),
        ),
    )
    initial_rollback_plan = build_plan_graph(
        objective=objective,
        nodes=(rollback_seed_node, rollback_node),
        edges=(
            PlanEdge(
                edge_id="edge:v10-f-rollback-seed-before-restore",
                from_node_id=rollback_seed_node.node_id,
                to_node_id=rollback_node.node_id,
                edge_kind="sequence",
                condition=None,
                priority=0,
            ),
        ),
        change_reason="Create the two-node Tool Gateway rollback rehearsal plan.",
        created_at=times["rollback_plan"],
        plan_id="plan:v10-f-owned-rollback-rehearsal",
    )
    paused_rollback_plan = pause_plan(
        initial_rollback_plan,
        objective=objective,
        node_ids=(rollback_seed_node.node_id, rollback_node.node_id),
        reason="Persist the rollback scheduling boundary before the drill.",
        created_at=times["rollback_plan_pause"],
        created_by="agent",
    )
    rollback_plan = resume_plan(
        paused_rollback_plan,
        objective=objective,
        node_ids=(rollback_seed_node.node_id, rollback_node.node_id),
        reason="The explicit acceptance authority permits this owned rollback drill.",
        created_at=times["rollback_plan_resume"],
        created_by="user",
    )
    overreach_node = PlanNode(
        node_id="node:v10-f-overreach-path-probe",
        objective_id=objective.objective_id,
        title="Probe one out-of-scope write",
        purpose=(
            "Submit one typed patch proposal whose path is outside the exact "
            "authorized write root and require fail-closed rejection."
        ),
        capability_requirements=(patch_requirement,),
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        risk=patch_profile.risk_class,
        reversible=True,
        authority_requirement="explicit_act",
        success_criteria=(
            "Tool Gateway rejects before reservation or execution and persists overreach evidence.",
        ),
        verification=(
            "Verify the outside-scope file digest and the absence of a tool execution.",
        ),
        rollback=("No rollback is needed because execution must not start.",),
        timeout_ms=10_000,
        retry_policy=RetryPolicy(
            maximum_attempts=1,
            equivalent_retry_limit=0,
            backoff_ms=0,
            retryable_failures=(),
        ),
    )
    overreach_plan = build_plan_graph(
        objective=objective,
        nodes=(overreach_node,),
        edges=(),
        change_reason="Create the isolated Tool Gateway overreach probe plan.",
        created_at=times["overreach_plan"],
        plan_id="plan:v10-f-overreach-path-probe",
    )
    prohibited = (
        "merge",
        "tag",
        "push",
        "declare_project_complete",
        "declare_project_sealed",
        "resolve_conflict",
        "start_next_version",
        "create_long_term_plan",
        "update_user_profile",
    )
    delegation = DelegationContract(
        delegation_id="delegation:v10-f-real-agent",
        project_id=PROJECT_ID,
        work_item_id="work-item:v10-f-real-agent",
        intent="Execute only the persisted two-node disposable sandbox plan.",
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        prohibited_actions=prohibited,
        tool_scope=(patch_profile.capability_id, check_profile.capability_id),
        risk="medium",
        valid_from=times["delegation"],
        expires_at=times["expires"],
        acceptance_criteria=(
            "Both tool calls are independently verified and remain unaccepted by the Agent.",
        ),
        rollback="Discard only the owned disposable sandbox or use its owned backup receipt.",
        version_scope=("v10-F",),
        commit_permission=False,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
        authority_digest=authority.authority_digest,
    )
    rollback_delegation = DelegationContract(
        delegation_id="delegation:v10-f-owned-rollback-rehearsal",
        project_id=PROJECT_ID,
        work_item_id="work-item:v10-f-owned-rollback-rehearsal",
        intent=(
            "Execute only one zero-model Tool Gateway patch seed followed by "
            "project.change.rollback."
        ),
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        prohibited_actions=prohibited,
        tool_scope=(patch_profile.capability_id, rollback_profile.capability_id),
        risk="medium",
        valid_from=times["rollback_delegation"],
        expires_at=times["expires"],
        acceptance_criteria=(
            "The run-owned backup is restored and independently verified.",
        ),
        rollback="Stop without widening the run-owned backup boundary.",
        version_scope=("v10-F",),
        commit_permission=False,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
        authority_digest=authority.authority_digest,
    )
    overreach = DelegationContract(
        delegation_id="delegation:v10-f-overreach-probe",
        project_id=PROJECT_ID,
        work_item_id="work-item:v10-f-overreach-probe",
        intent="Record one representative fail-closed path overreach decision.",
        read_scope=(sandbox_text,),
        write_scope=(authorized_write_text,),
        prohibited_actions=prohibited,
        tool_scope=(patch_profile.capability_id,),
        risk="medium",
        valid_from=times["overreach"],
        expires_at=times["expires"],
        acceptance_criteria=(
            "The out-of-scope patch is blocked and no operation executes.",
        ),
        rollback="No rollback is needed because the operation must not execute.",
        version_scope=("v10-F",),
        commit_permission=False,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
        authority_digest=authority.authority_digest,
    )
    authorization = issue_authorization_artifact(
        objective=objective,
        plan=plan,
        delegation=delegation,
        source_authority=authority,
        read_roots=(sandbox_text,),
        write_roots=(authorized_write_text,),
        allowed_capabilities=(patch_requirement, check_requirement),
        denied_capabilities=(
            "database.write",
            "git.commit.create",
            "network.request",
            "shell.execute",
        ),
        allowed_commands=("git.diff.check.v1",),
        risk_ceiling="medium",
        max_steps=2,
        max_duration_ms=1_200_000,
        max_model_calls=2,
        max_tokens=32_768,
        max_cost_microunits=2_000_000,
        commit_permission=False,
        database_permission="none",
        valid_from=times["authorization"],
        expires_at=times["expires"],
        rollback_policy=("Operate only on artifacts owned by this run.",),
        nonce_material="v10-f-real-agent-authorization-one-shot-nonce",
    )
    rollback_authorization = issue_authorization_artifact(
        objective=objective,
        plan=rollback_plan,
        delegation=rollback_delegation,
        source_authority=authority,
        read_roots=(sandbox_text,),
        write_roots=(authorized_write_text,),
        allowed_capabilities=(patch_requirement, rollback_requirement),
        denied_capabilities=(
            "database.write",
            "git.commit.create",
            "network.request",
            "shell.execute",
        ),
        allowed_commands=(),
        risk_ceiling="medium",
        max_steps=2,
        max_duration_ms=1_200_000,
        max_model_calls=0,
        max_tokens=0,
        max_cost_microunits=0,
        commit_permission=False,
        database_permission="none",
        valid_from=times["authorization"],
        expires_at=times["expires"],
        rollback_policy=("Restore only the run-owned backup receipt.",),
        nonce_material="v10-f-real-agent-rollback-rehearsal-nonce",
    )
    overreach_authorization = issue_authorization_artifact(
        objective=objective,
        plan=overreach_plan,
        delegation=overreach,
        source_authority=authority,
        read_roots=(sandbox_text,),
        write_roots=(authorized_write_text,),
        allowed_capabilities=(patch_requirement,),
        denied_capabilities=(
            "database.write",
            "git.commit.create",
            "network.request",
            "shell.execute",
        ),
        allowed_commands=(),
        risk_ceiling="medium",
        max_steps=1,
        max_duration_ms=60_000,
        max_model_calls=0,
        max_tokens=0,
        max_cost_microunits=0,
        commit_permission=False,
        database_permission="none",
        valid_from=times["authorization"],
        expires_at=times["expires"],
        rollback_policy=("No operation may start after a denied preview.",),
        nonce_material="v10-f-real-agent-overreach-probe-nonce",
    )
    return AcceptanceContracts(
        anchor=anchor,
        upstream_authority=upstream_authority,
        authority=authority,
        objective=objective,
        capability_registry=capability_registry,
        provider_registry=provider_setup.registry,
        provider_selection=provider_setup.selection,
        patch_requirement=patch_requirement,
        check_requirement=check_requirement,
        rollback_requirement=rollback_requirement,
        plan=plan,
        plan_history=(initial_plan, paused_plan, plan),
        rollback_plan=rollback_plan,
        rollback_plan_history=(
            initial_rollback_plan,
            paused_rollback_plan,
            rollback_plan,
        ),
        overreach_plan=overreach_plan,
        delegation=delegation,
        rollback_delegation=rollback_delegation,
        overreach_delegation=overreach,
        authorization=authorization,
        rollback_authorization=rollback_authorization,
        overreach_authorization=overreach_authorization,
        event_times=times,
    )


def production_context_sources(*, evaluated_at: str) -> tuple[ContextSource, ...]:
    memory_package = build_production_memory_package(
        RetrievalQuery("YunOS 受限认知代理"),
        future_token_budget=240,
    )
    operational = OperationalStateContextService()
    now_package = operational.now_package(
        budget_profile="Small",
        as_of=evaluated_at,
    )
    resume_package = operational.resume_package(
        PROJECT_ID,
        budget_profile="Small",
        as_of=evaluated_at,
    )
    return (
        context_source_from_v7_memory_package(
            memory_package,
            purpose="Provide only source-backed v7 evidence relevant to this acceptance.",
            maximum_items=2,
            maximum_bytes=6_000,
            required=True,
            priority=20,
        ),
        context_source_from_v9_package(
            now_package,
            source_system="v9_now",
            purpose="Bind the live v9 current state as untrusted data only.",
            maximum_items=2,
            maximum_bytes=6_000,
            evaluated_at=evaluated_at,
            include_sections=("active_goals", "blockers", "pending_decisions"),
            required=True,
            priority=10,
        ),
        context_source_from_v9_package(
            resume_package,
            source_system="v9_resume",
            purpose="Bind the live YunOS v9 resume package as untrusted data only.",
            maximum_items=2,
            maximum_bytes=6_000,
            evaluated_at=evaluated_at,
            include_sections=("current_work", "next_approved_steps", "blockers", "outcomes"),
            required=True,
            priority=11,
        ),
    )


def build_bounded_context_factory(
    *,
    objective: Any,
    sources: Sequence[ContextSource],
    capability_registry: Any,
) -> Callable[..., CognitiveContext]:
    base = assemble_cognitive_context(
        objective_id=objective.objective_id,
        objective_digest=objective.canonical_digest,
        sources=tuple(sources),
        budget=ContextBudget(
            maximum_items=8,
            maximum_bytes=32_000,
            maximum_estimated_tokens=7_000,
        ),
        allow_nonfresh_required=False,
    )

    def context_factory(run, plan, node, authorization) -> CognitiveContext:
        if (
            run.objective_id != objective.objective_id
            or plan.plan_id != run.plan_id
            or authorization.authorization_id != run.authorization_id
        ):
            _fail("context_factory_binding_mismatch")
        if node.node_id == "node:v10-f-real-agent-patch":
            profile = capability_registry.lookup("project.file.patch", version=1)
            typed_arguments = {
                "relative_path": CREATED_RELATIVE_PATH,
                "expected_before_sha256": BASELINE_CONTENT_SHA256,
                "replacement_content": PATCHED_CONTENT,
                "expected_after_sha256": PATCHED_CONTENT_SHA256,
            }
            reason = "Patch only the exact Plan-bound tracked acceptance file."
            observation = "Return before/after digests and the owned backup receipt."
        elif node.node_id == "node:v10-f-real-agent-diff-check":
            profile = capability_registry.lookup("process.template.run", version=1)
            typed_arguments = {
                "template_id": "git.diff.check",
                "relative_paths": [CREATED_RELATIVE_PATH],
            }
            reason = "Run only the authorized code-owned git diff check template."
            observation = "Return resolved argv, exit code, and bounded output digests."
        else:
            _fail("context_factory_unknown_plan_node")
        payload = dict(base.to_dict()["model_context"])
        payload["bounded_tool_proposal_blueprint"] = {
            "capability_id": profile.capability_id,
            "capability_version": profile.version,
            "capability_digest": profile.capability_digest,
            "typed_arguments": typed_arguments,
            "reason": reason,
            "expected_observation": observation,
            "risk_class": profile.risk_class,
        }
        encoded = canonical_json(payload)
        return CognitiveContext(
            objective_id=base.objective_id,
            objective_digest=base.objective_digest,
            bindings=base.bindings,
            model_context=payload,
            budget=base.budget,
            selected_items=base.selected_items,
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )

    return context_factory


def build_external_consent_factory(
    *,
    confirmation: ControllerConfirmation,
    objective: Any,
    controller_binding: Mapping[str, Any],
) -> TrustedConsentLedger:
    return TrustedConsentLedger(
        confirmation=confirmation,
        objective_id=objective.objective_id,
        objective_digest=objective.canonical_digest,
        controller_binding=controller_binding,
    )


def build_controller_confirmation_binding(
    *,
    confirmation: ControllerConfirmation,
    contracts: AcceptanceContracts,
    run_id: str,
) -> dict[str, Any]:
    return {
        "version": "v10-f-local-controller-binding-1",
        "cli_confirmation": confirmation.to_dict(),
        "cli_confirmation_digest": confirmation.binding_digest,
        "user_authorization_text_sha256": USER_CONSENT_TEXT_SHA256,
        "user_approved_combined_boundary_sha256": (
            confirmation.combined_boundary_sha256
        ),
        "agent_external_send_disclosure_digest": (
            EXTERNAL_SEND_DISCLOSURE_DIGEST
        ),
        "objective_id": contracts.objective.objective_id,
        "objective_digest": contracts.objective.canonical_digest,
        "plan_id": contracts.plan.plan_id,
        "plan_digest": contracts.plan.plan_digest,
        "authorization_id": contracts.authorization.authorization_id,
        "authorization_digest": contracts.authorization.authority_digest,
        "cognitive_run_id": run_id,
        "provider_selection_digest": (
            contracts.provider_selection.selection_digest
        ),
        "maximum_model_calls": 2,
        "automatic_retry_allowed": False,
        "do_not_rerun": True,
    }


def _plan_event(
    contracts: AcceptanceContracts,
    *,
    graph: Any,
    caused_by_event_id: str,
) -> Any:
    created_at = graph.revisions[-1].created_at
    actor_kind = graph.revisions[-1].created_by
    return build_state_event(
        project_id=PROJECT_ID,
        entity_id=make_identity(
            "workitem", {"plan_id": graph.plan_id, "version": graph.version}
        ),
        entity_type="work_item",
        event_type="plan_created" if graph.version == 1 else "plan_revised",
        source_kind="declared",
        source_anchor=contracts.anchor,
        authority_kind="user",
        actor_kind=actor_kind,
        actor_id=(
            USER_ACTOR_ID
            if actor_kind == "user"
            else "agent:yunos-v10-f-planner"
        ),
        observed_at=created_at,
        effective_at=created_at,
        recorded_at=created_at,
        expires_at=None,
        supersedes_event_id=None,
        caused_by_event_id=caused_by_event_id,
        idempotency_key=make_identity(
            "idempotency",
            {"plan_digest": graph.plan_digest, "plan_version": graph.version},
        ),
        conflict_group_id=None,
        payload={
            "state": "proposed",
            "plan_id": graph.plan_id,
            "plan_version": graph.version,
            "plan_digest": graph.plan_digest,
            "objective_id": graph.objective_id,
            "objective_version": graph.objective_version,
            "objective_digest": graph.objective_digest,
            "authority_impact": graph.revisions[-1].authority_impact,
        },
        privacy_scope="private_metadata",
        authority_digest=contracts.authority.authority_digest,
    )


def _persist_contracts(
    *,
    target: AcceptanceTarget,
    contracts: AcceptanceContracts,
    owner: str,
    token: str,
) -> tuple[CognitiveExecutionStore, CognitivePlanningStore, Any]:
    allow = target.production
    now = _utc_now
    state_store = CognitiveStateStore(
        target.operational_db,
        trusted_clock=now,
        allow_production_write=allow,
    )
    objective_event = build_state_event(
        project_id=PROJECT_ID,
        entity_id="decision:v10-f-real-agent-objective",
        entity_type="decision",
        event_type="decision_approved",
        source_kind="declared",
        source_anchor=contracts.anchor,
        authority_kind="user",
        actor_kind="user",
        actor_id=USER_ACTOR_ID,
        observed_at=contracts.event_times["objective"],
        effective_at=contracts.event_times["objective"],
        recorded_at=contracts.event_times["objective"],
        expires_at=None,
        supersedes_event_id=None,
        caused_by_event_id=None,
        idempotency_key="idempotency:v10-f-real-agent-objective",
        conflict_group_id=None,
        payload={
            "state": "approved",
            "objective_id": contracts.objective.objective_id,
            "objective_version": contracts.objective.version,
            "objective_digest": contracts.objective.canonical_digest,
        },
        privacy_scope="private_metadata",
        authority_digest=contracts.authority.authority_digest,
    )
    objective_result = state_store.commit_objective(
        contract=contracts.objective.to_dict(),
        authority=contracts.authority,
        event=objective_event,
        lease_owner_id=owner,
        lease_token=token,
        created_at=contracts.event_times["objective"],
    )
    planning_store = CognitivePlanningStore(
        target.operational_db,
        trusted_clock=now,
        allow_production_write=allow,
    )
    planning_store.install_builtin_capabilities(
        registry=contracts.capability_registry,
        authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    CognitiveProviderStore(
        target.operational_db,
        trusted_clock=now,
        allow_production_write=allow,
    ).install_builtin_providers(
        registry=contracts.provider_registry,
        authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    for history in (
        contracts.plan_history,
        contracts.rollback_plan_history,
        (contracts.overreach_plan,),
    ):
        caused_by_event_id = objective_result["event_id"]
        for graph in history:
            graph_created_at = graph.revisions[-1].created_at
            commit_store = CognitivePlanningStore(
                target.operational_db,
                trusted_clock=lambda value=graph_created_at: value,
                allow_production_write=allow,
            )
            committed_plan = commit_store.commit_plan(
                graph=graph,
                authority=contracts.authority,
                event=_plan_event(
                    contracts,
                    graph=graph,
                    caused_by_event_id=caused_by_event_id,
                ),
                lease_owner_id=owner,
                lease_token=token,
            )
            caused_by_event_id = committed_plan["event_id"]
    operational = OperationalStateStore(
        target.operational_db,
        allow_production_write=allow,
    )
    delegation_service = DelegationService(
        operational,
        source_anchor=contracts.anchor,
    )
    for contract, times in (
        (
            contracts.delegation,
            (
                contracts.event_times["delegation_authorized"],
                contracts.event_times["delegation_queued"],
                contracts.event_times["delegation_running"],
            ),
        ),
        (
            contracts.overreach_delegation,
            (
                contracts.event_times["overreach_authorized"],
                contracts.event_times["overreach_queued"],
                contracts.event_times["overreach_running"],
            ),
        ),
        (
            contracts.rollback_delegation,
            (
                contracts.event_times["rollback_delegation_authorized"],
                contracts.event_times["rollback_delegation_queued"],
                contracts.event_times["rollback_delegation_running"],
            ),
        ),
    ):
        delegation_service.create(
            contract=contract,
            authority=contracts.authority,
            user_actor_id=USER_ACTOR_ID,
            lease_owner_id=owner,
            lease_token=token,
        )
        for state, at, actor_kind, actor_id in (
            ("authorized", times[0], "user", USER_ACTOR_ID),
            ("queued", times[1], "agent", AGENT_ID),
            ("running", times[2], "agent", AGENT_ID),
        ):
            delegation_service.transition(
                delegation_id=contract.delegation_id,
                next_state=state,
                authority=contracts.authority,
                actor_kind=actor_kind,
                actor_id=actor_id,
                at=at,
                lease_owner_id=owner,
                lease_token=token,
                reason_code=f"v10_f_real_agent_{state}",
            )
    execution_store = CognitiveExecutionStore(
        target.operational_db,
        trusted_clock=now,
        allow_production_write=allow,
    )
    execution_store.commit_authorization(
        authorization=contracts.authorization,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    execution_store.commit_authorization(
        authorization=contracts.rollback_authorization,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    execution_store.commit_authorization(
        authorization=contracts.overreach_authorization,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    run = execution_store.create_run(
        authorization=contracts.authorization,
        source_authority=contracts.authority,
        provider_id=contracts.provider_selection.provider_id,
        provider_profile_version=contracts.provider_selection.profile_version,
        run_nonce="v10-f-real-agent-one-shot-run-nonce",
        lease_owner_id=owner,
        lease_token=token,
    )
    if objective_result["canonical_digest"] != run.objective_digest:
        _fail("persisted_objective_run_binding_mismatch")
    return execution_store, planning_store, run


def _git_environment(sandbox_root: Path) -> dict[str, str]:
    return {
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
        "PATH": os.environ.get("PATH", ""),
        "TEMP": os.environ.get("TEMP", str(sandbox_root)),
        "TMP": os.environ.get("TMP", str(sandbox_root)),
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "NUL",
    }


def _run_fixed_git(
    sandbox_root: Path,
    argv: Sequence[str],
    *,
    maximum_output_bytes: int = 256 * 1024,
) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if git is None:
        _fail("git_executable_unavailable")
    completed = subprocess.run(
        [git, *tuple(argv)],
        cwd=sandbox_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=30,
        env=_git_environment(sandbox_root),
    )
    if (
        len(completed.stdout) > maximum_output_bytes
        or len(completed.stderr) > maximum_output_bytes
    ):
        _fail("fixed_git_output_limit_exceeded")
    return completed


def _initialize_disposable_git_sandbox(
    sandbox_root: Path,
    *,
    output_target: AcceptanceTarget | None = None,
    directory_pins: _DirectoryHandlePins | None = None,
) -> None:
    if output_target is not None:
        _assert_pinned_output_root(output_target)
    if directory_pins is not None:
        directory_pins.assert_path(sandbox_root.parent)
    if sandbox_root.exists():
        _fail("disposable_sandbox_already_exists")
    sandbox_root.mkdir(parents=True, exist_ok=False)
    sandbox_identity = _filesystem_identity(sandbox_root)

    def assert_sandbox_identity() -> None:
        if output_target is not None:
            _assert_pinned_output_root(output_target)
        if directory_pins is not None:
            directory_pins.assert_path(sandbox_root.parent)
        if _filesystem_identity(sandbox_root) != sandbox_identity:
            _fail("disposable_sandbox_identity_changed")
        _assert_plain_directory(
            sandbox_root,
            reason_code="disposable_sandbox_reparse_denied",
        )

    assert_sandbox_identity()
    baseline = sandbox_root / CREATED_RELATIVE_PATH
    baseline.parent.mkdir(parents=True, exist_ok=False)
    assert_sandbox_identity()
    baseline.write_text(BASELINE_CONTENT, encoding="utf-8", newline="")
    assert_sandbox_identity()
    (sandbox_root / OVERREACH_RELATIVE_PATH).write_text(
        OVERREACH_BASELINE_CONTENT,
        encoding="utf-8",
        newline="",
    )
    assert_sandbox_identity()
    if _sha256_file(baseline) != BASELINE_CONTENT_SHA256:
        _fail("disposable_git_baseline_digest_mismatch")
    completed = _run_fixed_git(sandbox_root, ("init", "--quiet"))
    assert_sandbox_identity()
    if completed.returncode != 0 or not (sandbox_root / ".git").is_dir():
        _fail("disposable_git_init_failed")
    staged = _run_fixed_git(
        sandbox_root,
        ("add", "--", CREATED_RELATIVE_PATH),
    )
    assert_sandbox_identity()
    if staged.returncode != 0:
        _fail("disposable_git_exact_stage_failed")
    index = _run_fixed_git(
        sandbox_root,
        ("ls-files", "--stage", "--", CREATED_RELATIVE_PATH),
    )
    assert_sandbox_identity()
    lines = index.stdout.decode("utf-8", errors="strict").splitlines()
    if (
        index.returncode != 0
        or len(lines) != 1
        or not lines[0].endswith(f"\t{CREATED_RELATIVE_PATH}")
    ):
        _fail("disposable_git_baseline_not_exactly_tracked")
    commits = _run_fixed_git(sandbox_root, ("rev-list", "--all", "--count"))
    assert_sandbox_identity()
    if commits.returncode != 0 or commits.stdout.strip() != b"0":
        _fail("disposable_git_baseline_commit_forbidden")


def _write_bytes_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _verify_tracked_git_diff(
    *,
    sandbox_root: Path,
    artifact_root: Path,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
    run_id: str,
    proposal_ids: Sequence[str],
) -> dict[str, Any]:
    proposals = [execution_store.get_tool_proposal(item) for item in proposal_ids]
    if [item.capability_id for item in proposals] != [
        "project.file.patch",
        "process.template.run",
    ]:
        _fail("real_agent_strict_tool_proposal_order_failed")
    results = {
        item.proposal_id: item
        for item in execution_store.list_tool_executions(run_id)
    }
    if set(results) != set(proposal_ids):
        _fail("real_agent_tool_execution_set_mismatch")
    patch_result = results[proposals[0].proposal_id]
    check_result = results[proposals[1].proposal_id]
    patch_output = dict(patch_result.output)
    backup_reference = patch_output.get("backup_reference")
    if (
        patch_result.status != "succeeded"
        or patch_result.verification_status != "passed"
        or patch_output.get("before_sha256") != BASELINE_CONTENT_SHA256
        or patch_output.get("after_sha256") != PATCHED_CONTENT_SHA256
        or not isinstance(backup_reference, str)
        or not backup_reference.startswith("toolbackup:sha256:")
    ):
        _fail("real_agent_patch_backup_receipt_failed")
    registry = CommandTemplateRegistry()
    profile = contracts.capability_registry.lookup(
        "process.template.run", version=1
    )
    binding = resolve_project_path(
        project_root=sandbox_root,
        relative_path=CREATED_RELATIVE_PATH,
        access_mode="read_file",
    )
    expected_command = registry.resolve(
        template_id="git.diff.check.v1",
        profile=profile,
        typed_arguments={
            "template_id": "git.diff.check",
            "relative_paths": [CREATED_RELATIVE_PATH],
        },
        path_bindings=(binding,),
        project_root=str(sandbox_root),
    )
    registry.revalidate(expected_command)
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    if (
        check_result.status != "succeeded"
        or check_result.verification_status != "passed"
        or check_result.exit_code != 0
        or tuple(check_result.resolved_argv) != tuple(expected_command.argv)
        or check_result.stdout_digest != empty_sha256
        or check_result.stderr_digest != empty_sha256
        or dict(check_result.output).get("stdout_sha256") != empty_sha256
        or dict(check_result.output).get("stderr_sha256") != empty_sha256
    ):
        _fail("real_agent_fixed_diff_check_binding_failed")

    tracked = _run_fixed_git(
        sandbox_root,
        ("ls-files", "--stage", "--", CREATED_RELATIVE_PATH),
    )
    names = _run_fixed_git(
        sandbox_root,
        ("diff", "--name-only", "--", CREATED_RELATIVE_PATH),
    )
    diff = _run_fixed_git(
        sandbox_root,
        (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--",
            CREATED_RELATIVE_PATH,
        ),
    )
    status = _run_fixed_git(
        sandbox_root,
        ("status", "--porcelain=v2", "--", CREATED_RELATIVE_PATH),
    )
    commits = _run_fixed_git(sandbox_root, ("rev-list", "--all", "--count"))
    tracked_lines = tracked.stdout.decode("utf-8", errors="strict").splitlines()
    name_lines = names.stdout.decode("utf-8", errors="strict").splitlines()
    if (
        tracked.returncode != 0
        or len(tracked_lines) != 1
        or not tracked_lines[0].endswith(f"\t{CREATED_RELATIVE_PATH}")
        or names.returncode != 0
        or name_lines != [CREATED_RELATIVE_PATH]
        or diff.returncode != 0
        or not diff.stdout
        or status.returncode != 0
        or CREATED_RELATIVE_PATH.encode("utf-8") not in status.stdout
        or commits.returncode != 0
        or commits.stdout.strip() != b"0"
    ):
        _fail("real_agent_tracked_diff_verification_failed")
    diff_artifact = artifact_root / "tracked-working-tree.diff"
    _write_bytes_exclusive(diff_artifact, diff.stdout)
    diff_sha256 = _sha256_file(diff_artifact)
    evidence = {
        "relative_path": CREATED_RELATIVE_PATH,
        "baseline_sha256": BASELINE_CONTENT_SHA256,
        "patched_sha256": PATCHED_CONTENT_SHA256,
        "backup_reference": backup_reference,
        "patch_execution_id": patch_result.execution_id,
        "diff_check_execution_id": check_result.execution_id,
        "diff_check_resolved_argv": list(check_result.resolved_argv),
        "tracked_index_record": tracked_lines[0],
        "changed_paths": name_lines,
        "diff_artifact": str(diff_artifact),
        "diff_artifact_sha256": diff_sha256,
        "diff_bytes": len(diff.stdout),
        "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
        "commit_count": 0,
    }
    return {**evidence, "evidence_digest": canonical_digest(evidence)}


def _make_loop(
    *,
    sandbox_root: Path,
    artifact_root: Path,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
    planning_store: CognitivePlanningStore,
    source: ProductionAgentRuntimeTurnSource,
    owner: str,
    token: str,
) -> BoundedCognitiveAgentLoop:
    gateway, verifier, _broker = _gateway_components(
        sandbox_root=sandbox_root,
        artifact_root=artifact_root,
        contracts=contracts,
        execution_store=execution_store,
    )
    return BoundedCognitiveAgentLoop(
        execution_store=execution_store,
        planning_store=planning_store,
        tool_gateway=gateway,
        independent_verifier=verifier,
        model_turn_source=source,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
        limits=AgentLoopLimits(
            maximum_steps=contracts.authorization.max_steps,
            maximum_duration_ms=contracts.authorization.max_duration_ms,
            maximum_model_calls=contracts.authorization.max_model_calls,
        ),
        trusted_clock=_utc_now,
    )


class _ObservedCognitiveLocalBroker(CognitiveLocalBroker):
    """Records every successful mutating Broker call for Gateway attribution."""

    def __init__(self, *, project_root: Path, artifact_root: Path) -> None:
        super().__init__(project_root=project_root, artifact_root=artifact_root)
        self._successful_mutation_calls: list[dict[str, str]] = []

    @property
    def successful_mutation_calls(self) -> tuple[Mapping[str, str], ...]:
        return tuple(dict(item) for item in self._successful_mutation_calls)

    def patch_file(
        self,
        *,
        proposal: Any,
        execution_key: str,
        binding: Any,
    ) -> dict[str, Any]:
        result = super().patch_file(
            proposal=proposal,
            execution_key=execution_key,
            binding=binding,
        )
        self._successful_mutation_calls.append(
            {
                "method": "patch_file",
                "proposal_id": proposal.proposal_id,
                "execution_key": execution_key,
            }
        )
        return result

    def rollback_patch(
        self,
        *,
        proposal: Any,
        backup_reference: str,
        expected_current_sha256: str,
    ) -> dict[str, Any]:
        result = super().rollback_patch(
            proposal=proposal,
            backup_reference=backup_reference,
            expected_current_sha256=expected_current_sha256,
        )
        self._successful_mutation_calls.append(
            {
                "method": "rollback_patch",
                "proposal_id": proposal.proposal_id,
            }
        )
        return result


def _gateway_components(
    *,
    sandbox_root: Path,
    artifact_root: Path,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
) -> tuple[
    CognitiveToolGateway,
    CognitiveIndependentVerifier,
    _ObservedCognitiveLocalBroker,
]:
    command_registry = CommandTemplateRegistry()
    broker = _ObservedCognitiveLocalBroker(
        project_root=sandbox_root,
        artifact_root=artifact_root,
    )
    gateway = CognitiveToolGateway(
        project_root=str(sandbox_root),
        registry=contracts.capability_registry,
        execution_store=execution_store,
        command_registry=command_registry,
        local_broker=broker,
        process_supervisor=ProcessSupervisor(
            artifact_root=artifact_root,
            trusted_clock=_utc_now,
        ),
        trusted_clock=_utc_now,
    )
    verifier = CognitiveIndependentVerifier(
        project_root=sandbox_root,
        artifact_root=artifact_root,
        command_registry=command_registry,
    )
    return gateway, verifier, broker


def _rehearse_owned_rollback(
    *,
    sandbox_root: Path,
    artifact_root: Path,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
    owner: str,
    token: str,
) -> dict[str, Any]:
    """Restore a run-owned backup through ``project.change.rollback``.

    Both the intermediate patch seed and restoration are policy checked,
    reserved, executed, independently verified, and persisted by Tool Gateway.
    """

    run = execution_store.create_run(
        authorization=contracts.rollback_authorization,
        source_authority=contracts.authority,
        provider_id=contracts.provider_selection.provider_id,
        provider_profile_version=contracts.provider_selection.profile_version,
        run_nonce="v10-f-owned-rollback-rehearsal-run-nonce",
        lease_owner_id=owner,
        lease_token=token,
    )
    run = execution_store.transition_run(
        run_id=run.run_id,
        expected_run_digest=run.run_digest,
        next_status="executing",
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    gateway, verifier, broker = _gateway_components(
        sandbox_root=sandbox_root,
        artifact_root=artifact_root,
        contracts=contracts,
        execution_store=execution_store,
    )
    patch_profile = contracts.capability_registry.lookup(
        "project.file.patch", version=1
    )
    rollback_profile = contracts.capability_registry.lookup(
        "project.change.rollback", version=1
    )
    seed_node = next(
        node
        for node in contracts.rollback_plan.nodes
        if node.capability_requirements[0].capability_id
        == patch_profile.capability_id
    )
    rollback_node = next(
        node
        for node in contracts.rollback_plan.nodes
        if node.capability_requirements[0].capability_id
        == rollback_profile.capability_id
    )
    seed = create_tool_proposal(
        run_id=run.run_id,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
        registry=contracts.capability_registry,
        plan_node_id=seed_node.node_id,
        model_proposal={
            "capability_id": patch_profile.capability_id,
            "capability_version": patch_profile.version,
            "capability_digest": patch_profile.capability_digest,
            "typed_arguments": {
            "relative_path": CREATED_RELATIVE_PATH,
            "expected_before_sha256": PATCHED_CONTENT_SHA256,
            "replacement_content": ROLLBACK_REHEARSAL_CONTENT,
            "expected_after_sha256": ROLLBACK_REHEARSAL_CONTENT_SHA256,
            },
            "reason": "Create one run-owned intermediate backup for the rollback drill.",
            "expected_observation": "A backup reference owned by this drill run.",
        },
        proposed_at=_utc_now(),
    )
    execution_store.record_tool_proposal(
        proposal=seed,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    seed_preview = gateway.preview(
        proposal=seed,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
    )
    seed_outcome = gateway.execute(
        preview=seed_preview,
        proposal=seed,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    if seed_outcome.result.status != "succeeded":
        _fail("rollback_rehearsal_gateway_seed_failed")
    seed_verification = verifier.verify(
        proposal=seed,
        preview=seed_preview,
        result=seed_outcome.result,
        verified_at=_utc_now(),
    )
    seed_run = execution_store.get_run(run.run_id)
    execution_store.record_independent_tool_verification(
        verification=seed_verification,
        expected_run_digest=seed_run.run_digest,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    seed_output = dict(seed_outcome.result.output)
    if (
        _sha256_file(sandbox_root / CREATED_RELATIVE_PATH)
        != ROLLBACK_REHEARSAL_CONTENT_SHA256
        or seed_verification.result != "passed"
        or seed_output.get("before_sha256") != PATCHED_CONTENT_SHA256
        or seed_output.get("after_sha256")
        != ROLLBACK_REHEARSAL_CONTENT_SHA256
        or not isinstance(seed_output.get("backup_reference"), str)
    ):
        _fail("rollback_rehearsal_seed_failed")
    proposal = create_tool_proposal(
        run_id=run.run_id,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
        registry=contracts.capability_registry,
        plan_node_id=rollback_node.node_id,
        model_proposal={
            "capability_id": rollback_profile.capability_id,
            "capability_version": rollback_profile.version,
            "capability_digest": rollback_profile.capability_digest,
            "typed_arguments": {
                "backup_reference": seed_output["backup_reference"],
                "expected_current_sha256": ROLLBACK_REHEARSAL_CONTENT_SHA256,
            },
            "reason": "Restore only the backup owned by this zero-model-call drill.",
            "expected_observation": "The exact patched acceptance digest is restored.",
        },
        proposed_at=_utc_now(),
    )
    execution_store.record_tool_proposal(
        proposal=proposal,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    preview = gateway.preview(
        proposal=proposal,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
    )
    outcome = gateway.execute(
        preview=preview,
        proposal=proposal,
        plan=contracts.rollback_plan,
        authorization=contracts.rollback_authorization,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    if outcome.result.status != "succeeded":
        _fail("rollback_rehearsal_gateway_execution_failed")
    verification = verifier.verify(
        proposal=proposal,
        preview=preview,
        result=outcome.result,
        verified_at=_utc_now(),
    )
    verifying = execution_store.get_run(run.run_id)
    recorded = execution_store.record_independent_tool_verification(
        verification=verification,
        expected_run_digest=verifying.run_digest,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    restored = sandbox_root / CREATED_RELATIVE_PATH
    if (
        verification.result != "passed"
        or dict(outcome.result.output).get("restored_sha256")
        != PATCHED_CONTENT_SHA256
        or dict(outcome.result.output).get("backup_reference")
        != seed_output["backup_reference"]
        or _sha256_file(restored) != PATCHED_CONTENT_SHA256
    ):
        _fail("rollback_rehearsal_restore_verification_failed")
    persisted_executions = tuple(execution_store.list_tool_executions(run.run_id))
    expected_execution_ids = {
        seed_outcome.result.execution_id,
        outcome.result.execution_id,
    }
    persisted_gateway_execution_ids = {
        item.execution_id
        for item in persisted_executions
        if item.executor_kind == "local_broker"
        and item.status == "succeeded"
        and item.verification_status == "passed"
        and isinstance(item.reservation_event_id, str)
        and isinstance(item.reservation_receipt_digest, str)
    }
    persisted_gateway_proposal_ids = {
        item.proposal_id
        for item in persisted_executions
        if item.execution_id in persisted_gateway_execution_ids
    }
    broker_mutation_calls = broker.successful_mutation_calls
    broker_mutation_proposal_ids = {
        item["proposal_id"] for item in broker_mutation_calls
    }
    expected_proposal_ids = {seed.proposal_id, proposal.proposal_id}
    gateway_execution_count = len(persisted_gateway_execution_ids)
    unattributed_expected_execution_count = len(
        expected_execution_ids - persisted_gateway_execution_ids
    )
    unattributed_broker_side_effect_count = max(
        len(
            [
                item
                for item in broker_mutation_calls
                if item["proposal_id"] not in persisted_gateway_proposal_ids
            ]
        ),
        len(broker_mutation_calls) - len(persisted_executions),
    )
    if (
        gateway_execution_count != 2
        or unattributed_expected_execution_count != 0
        or unattributed_broker_side_effect_count != 0
        or {item.execution_id for item in persisted_executions}
        != expected_execution_ids
        or persisted_gateway_proposal_ids != expected_proposal_ids
        or broker_mutation_proposal_ids != expected_proposal_ids
        or [item["method"] for item in broker_mutation_calls]
        != ["patch_file", "rollback_patch"]
    ):
        _fail("rollback_rehearsal_persisted_gateway_evidence_failed")
    current = recorded["run"]
    cancelled = execution_store.transition_run(
        run_id=current.run_id,
        expected_run_digest=current.run_digest,
        next_status="cancelled",
        failure_code="rollback_rehearsal_complete_no_acceptance_requested",
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    return {
        "run_id": cancelled.run_id,
        "run_status": cancelled.status,
        "model_calls_used": cancelled.model_calls_used,
        "backup_reference": seed_output["backup_reference"],
        "seed_proposal_id": seed.proposal_id,
        "seed_execution_id": seed_outcome.result.execution_id,
        "seed_execution_digest": seed_outcome.result.execution_digest,
        "seed_verification_id": seed_verification.verification_id,
        "rollback_proposal_id": proposal.proposal_id,
        "rollback_execution_id": outcome.result.execution_id,
        "rollback_execution_digest": outcome.result.execution_digest,
        "verification_id": verification.verification_id,
        "restored_sha256": PATCHED_CONTENT_SHA256,
        "backup_reference_verified": True,
        "gateway_execution_count": gateway_execution_count,
        "gateway_execution_ids": sorted(persisted_gateway_execution_ids),
        "broker_mutation_call_count": len(broker_mutation_calls),
        "broker_mutation_call_digests": [
            canonical_digest(dict(item)) for item in broker_mutation_calls
        ],
        "direct_broker_side_effects": unattributed_broker_side_effect_count,
        "side_effect_attribution": (
            "instrumented_broker_calls_bound_to_persisted_tool_execution_records"
        ),
    }


def _record_gateway_overreach(
    *,
    target: AcceptanceTarget,
    sandbox_root: Path,
    artifact_root: Path,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
    owner: str,
    token: str,
) -> dict[str, Any]:
    run = execution_store.create_run(
        authorization=contracts.overreach_authorization,
        source_authority=contracts.authority,
        provider_id=contracts.provider_selection.provider_id,
        provider_profile_version=contracts.provider_selection.profile_version,
        run_nonce="v10-f-overreach-path-probe-run-nonce",
        lease_owner_id=owner,
        lease_token=token,
    )
    run = execution_store.transition_run(
        run_id=run.run_id,
        expected_run_digest=run.run_digest,
        next_status="executing",
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    run_id = run.run_id
    gateway, _verifier, _broker = _gateway_components(
        sandbox_root=sandbox_root,
        artifact_root=artifact_root,
        contracts=contracts,
        execution_store=execution_store,
    )
    profile = contracts.capability_registry.lookup(
        "project.file.patch", version=1
    )
    node = next(
        node
        for node in contracts.overreach_plan.nodes
        if node.capability_requirements[0].capability_id == profile.capability_id
    )
    proposal = create_tool_proposal(
        run_id=run_id,
        plan=contracts.overreach_plan,
        authorization=contracts.overreach_authorization,
        registry=contracts.capability_registry,
        plan_node_id=node.node_id,
        model_proposal={
            "capability_id": profile.capability_id,
            "capability_version": profile.version,
            "capability_digest": profile.capability_digest,
            "typed_arguments": {
                "relative_path": OVERREACH_RELATIVE_PATH,
                "expected_before_sha256": OVERREACH_BASELINE_SHA256,
                "replacement_content": OVERREACH_REPLACEMENT_CONTENT,
                "expected_after_sha256": OVERREACH_REPLACEMENT_SHA256,
            },
            "reason": "Attempt a write inside the project but outside the exact authorized root.",
            "expected_observation": "Tool Gateway must reject before reservation or execution.",
        },
        proposed_at=_utc_now(),
    )
    execution_store.record_tool_proposal(
        proposal=proposal,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    execution_ids_before = tuple(
        item.execution_id for item in execution_store.list_tool_executions(run_id)
    )
    reason_code = None
    try:
        gateway.preview(
            proposal=proposal,
            plan=contracts.overreach_plan,
            authorization=contracts.overreach_authorization,
        )
    except BaseException as error:
        reason_code = getattr(error, "reason_code", str(error))
    if reason_code != "tool_path_outside_authorized_write_root":
        _fail("tool_gateway_overreach_not_exactly_rejected")
    if _sha256_file(sandbox_root / OVERREACH_RELATIVE_PATH) != (
        OVERREACH_BASELINE_SHA256
    ):
        _fail("tool_gateway_overreach_changed_file")
    run = execution_store.get_run(run_id)
    sequence = len(execution_store.list_supervision_events(run_id)) + 1
    event = create_supervision_event(
        run_id=run_id,
        sequence=sequence,
        event_type="overreach_blocked",
        severity="error",
        observed_at=_utc_now(),
        detail="Tool Gateway rejected a write outside the exact authorized root.",
        evidence={
            "proposal_digest": proposal.proposal_digest,
            "reason_code": reason_code,
            "relative_path": OVERREACH_RELATIVE_PATH,
            "authorized_write_roots": list(
                contracts.overreach_authorization.write_roots
            ),
            "operation_executed": False,
        },
        action_taken=("rejected_before_reservation", "delegation_failed"),
        plan_node_id=node.node_id,
    )
    recorded = execution_store.record_supervision_event(
        event=event,
        expected_run_digest=run.run_digest,
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    persisted = execution_store.list_supervision_events(run_id)
    execution_ids_after = tuple(
        item.execution_id for item in execution_store.list_tool_executions(run_id)
    )
    service = DelegationService(
        OperationalStateStore(
            target.operational_db,
            allow_production_write=target.production,
        ),
        source_anchor=contracts.anchor,
    )
    failed_delegation = service.transition(
        delegation_id=contracts.overreach_delegation.delegation_id,
        next_state="failed",
        authority=contracts.authority,
        actor_kind="runtime",
        actor_id="runtime:yunos-v10-f-tool-gateway",
        at=_utc_now(),
        lease_owner_id=owner,
        lease_token=token,
        reason_code=reason_code,
    )
    current = execution_store.get_run(run_id)
    failed_run = execution_store.transition_run(
        run_id=run_id,
        expected_run_digest=current.run_digest,
        next_status="failed",
        failure_code="agent_overreach_blocked",
        source_authority=contracts.authority,
        lease_owner_id=owner,
        lease_token=token,
    )
    if (
        not persisted
        or persisted[-1].event_digest != event.event_digest
        or recorded["event_digest"] != event.event_digest
        or execution_ids_after != execution_ids_before
        or failed_delegation["state"] != "failed"
        or failed_run.status != "failed"
    ):
        _fail("tool_gateway_overreach_persistence_failed")
    return {
        "delegation_id": contracts.overreach_delegation.delegation_id,
        "run_id": failed_run.run_id,
        "run_status": failed_run.status,
        "proposal_id": proposal.proposal_id,
        "reason_code": reason_code,
        "supervision_event_id": event.supervision_event_id,
        "supervision_event_digest": event.event_digest,
        "operation_executed": False,
        "tool_execution_delta": 0,
        "delegation_state": failed_delegation["state"],
    }


def _recover_acceptance_failure(
    *,
    target: AcceptanceTarget,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore | None,
    consent_ledger: TrustedConsentLedger | None,
    reason_code: str,
    owner: str,
    token: str,
) -> dict[str, Any]:
    recovery = {
        "reason_code": reason_code,
        "run_states": {},
        "delegation_states": {},
        "audit_event_ids": [],
        "runtime_bindings": [],
    }
    checkpoint_runtime_bindings: dict[str, dict[str, Any]] = {}
    if execution_store is not None:
        with closing(sqlite3.connect(target.operational_db)) as connection:
            run_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM cognitive_runs WHERE authorization_id IN (?,?,?) "
                    "ORDER BY run_id",
                    (
                        contracts.authorization.authorization_id,
                        contracts.rollback_authorization.authorization_id,
                        contracts.overreach_authorization.authorization_id,
                    ),
                )
            ]
        for run_id in run_ids:
            current = execution_store.get_run(run_id)
            try:
                checkpoint = execution_store.latest_checkpoint(run_id)
                for item in checkpoint.runtime_recovery_bindings:
                    reference = item.get("runtime_execution_reference")
                    if item.get("kind") == "model_call" and isinstance(
                        reference, str
                    ):
                        checkpoint_runtime_bindings[reference] = dict(item)
            except BaseException:
                checkpoint = None
            if current.status in {"completed", "failed", "cancelled"}:
                recovery["run_states"][run_id] = current.status
                continue
            try:
                sequence = len(
                    execution_store.list_supervision_events(run_id)
                ) + 1
                event_type = (
                    "provider_failure"
                    if "model" in reason_code
                    or "provider" in reason_code
                    or "transport" in reason_code
                    else "recovery_started"
                )
                event = create_supervision_event(
                    run_id=run_id,
                    sequence=sequence,
                    event_type=event_type,
                    severity="error",
                    observed_at=_utc_now(),
                    detail="Acceptance failed closed; recovery state was persisted.",
                    evidence={
                        "reason_code": reason_code,
                        "prior_run_status": current.status,
                        "has_safe_checkpoint": bool(
                            checkpoint is not None
                            and checkpoint.safe_to_resume
                            and not checkpoint.unsafe_reason_codes
                        ),
                    },
                    action_taken=(
                        "stop_new_tool_calls",
                        "persist_failure_state",
                    ),
                )
                recorded = execution_store.record_supervision_event(
                    event=event,
                    expected_run_digest=current.run_digest,
                    source_authority=contracts.authority,
                    lease_owner_id=owner,
                    lease_token=token,
                )
                recovery["audit_event_ids"].append(recorded["event_id"])
                current = recorded["run"]
            except BaseException:
                current = execution_store.get_run(run_id)
            if current.status not in {
                "completed",
                "failed",
                "cancelled",
                "recovery_required",
            }:
                try:
                    current = execution_store.transition_run(
                        run_id=run_id,
                        expected_run_digest=current.run_digest,
                        next_status="failed",
                        failure_code=reason_code,
                        source_authority=contracts.authority,
                        lease_owner_id=owner,
                        lease_token=token,
                    )
                except BaseException:
                    current = execution_store.get_run(run_id)
                    if current.status not in {
                        "completed",
                        "failed",
                        "cancelled",
                        "recovery_required",
                    }:
                        current = execution_store.transition_run(
                            run_id=run_id,
                            expected_run_digest=current.run_digest,
                            next_status="recovery_required",
                            failure_code=reason_code,
                            source_authority=contracts.authority,
                            lease_owner_id=owner,
                            lease_token=token,
                        )
            recovery["run_states"][run_id] = current.status

    ordered_runtime_references: list[str] = []
    if consent_ledger is not None:
        runtime_service: Any = (
            ProductionRuntimeExecutionService()
            if target.production
            else MockRuntimeExecutionService(target.runtime_db)
        )
        for record in consent_ledger.records:
            consent = record.get("external_send_consent", {})
            model_run_id = consent.get("runtime_model_run_id")
            if not isinstance(model_run_id, str):
                continue
            ordered_runtime_references.append(model_run_id)
            try:
                receipt = runtime_service.get_run(model_run_id)
            except BaseException:
                checkpoint_runtime_bindings.setdefault(
                    model_run_id,
                    {
                        "kind": "model_call",
                        "runtime_execution_reference": model_run_id,
                        "runtime_status": "not_persisted_or_unreadable",
                        "runtime_receipt_identity": None,
                        "runtime_receipt_digest": None,
                        "runtime_artifact_identity": consent.get(
                            "runtime_artifact_identity"
                        ),
                        "binding_source": "trusted_external_send_consent",
                    },
                )
                continue
            if (
                receipt.get("model_run_id") != model_run_id
                or receipt.get("provider") != "deepseek"
                or receipt.get("model") != "deepseek-v4-flash"
                or receipt.get("status") not in {
                    "prepared",
                    "running",
                    "succeeded",
                    "failed",
                }
                or receipt.get("execution", {}).get("persisted") is not True
            ):
                _fail("recovery_runtime_receipt_identity_mismatch")
            artifact = receipt.get("request_artifact")
            artifact_identity = (
                artifact.get("artifact_identity")
                if isinstance(artifact, Mapping)
                else None
            )
            receipt_identity = receipt.get("receipt_identity")
            if artifact_identity != consent.get("runtime_artifact_identity"):
                _fail("recovery_runtime_artifact_binding_mismatch")
            if receipt.get("status") in {"succeeded", "failed"} and not isinstance(
                receipt_identity, str
            ):
                _fail("recovery_runtime_terminal_receipt_missing")
            bridge_digest = None
            if isinstance(receipt_identity, str) and isinstance(
                artifact_identity, str
            ):
                bridge_digest = canonical_digest(
                    {
                        "runtime_model_run_id": model_run_id,
                        "runtime_receipt_identity": receipt_identity,
                        "runtime_artifact_identity": artifact_identity,
                    }
                )
            existing = checkpoint_runtime_bindings.get(model_run_id, {})
            if (
                existing.get("runtime_receipt_identity") not in {
                    None,
                    receipt_identity,
                }
                or existing.get("runtime_receipt_digest")
                not in {None, bridge_digest}
            ):
                _fail("recovery_runtime_receipt_binding_mismatch")
            checkpoint_runtime_bindings[model_run_id] = {
                **existing,
                "kind": "model_call",
                "runtime_execution_reference": model_run_id,
                "runtime_status": receipt.get("status"),
                "runtime_receipt_identity": receipt_identity,
                "runtime_receipt_digest": bridge_digest,
                "runtime_artifact_identity": artifact_identity,
                "binding_source": (
                    "checkpoint_and_trusted_external_send_consent"
                    if existing
                    else "trusted_external_send_consent"
                ),
            }
    ordered_runtime_references.extend(
        sorted(
            set(checkpoint_runtime_bindings) - set(ordered_runtime_references)
        )
    )
    recovery["runtime_bindings"] = [
        checkpoint_runtime_bindings[item]
        for item in ordered_runtime_references
        if item in checkpoint_runtime_bindings
    ]

    operational = OperationalStateStore(
        target.operational_db,
        allow_production_write=target.production,
    )
    service = DelegationService(operational, source_anchor=contracts.anchor)
    for delegation_id in (
        contracts.delegation.delegation_id,
        contracts.rollback_delegation.delegation_id,
        contracts.overreach_delegation.delegation_id,
    ):
        projection = operational.get_projection(delegation_id)
        if projection is None:
            continue
        state = projection["state"]
        if state in {"proposed", "authorized", "queued"}:
            next_state = "cancelled"
        elif state in {"running", "waiting"}:
            next_state = "failed"
        else:
            recovery["delegation_states"][delegation_id] = state
            continue
        transitioned = service.transition(
            delegation_id=delegation_id,
            next_state=next_state,
            authority=contracts.authority,
            actor_kind="agent",
            actor_id=AGENT_ID,
            at=_utc_now(),
            lease_owner_id=owner,
            lease_token=token,
            reason_code=reason_code,
        )
        recovery["delegation_states"][delegation_id] = transitioned["state"]
    return recovery


def _record_outcome_and_learning(
    *,
    target: AcceptanceTarget,
    contracts: AcceptanceContracts,
    execution_store: CognitiveExecutionStore,
    outcome: Any,
    owner: str,
    token: str,
) -> dict[str, Any]:
    operational = OperationalStateStore(
        target.operational_db,
        allow_production_write=target.production,
    )
    service = DelegationService(operational, source_anchor=contracts.anchor)
    receipt_evidence = {
        "run_id": outcome.run.run_id,
        "run_digest": outcome.run.run_digest,
        "proposal_ids": list(outcome.proposal_ids),
        "execution_ids": list(outcome.execution_ids),
        "verification_ids": list(outcome.verification_ids),
        "status": outcome.run.status,
        "accepted": False,
    }
    receipt_digest = canonical_digest(receipt_evidence)
    reported = service.record_execution_result(
        delegation_id=contracts.delegation.delegation_id,
        outcome_id="outcome:v10-f-real-agent",
        runtime_run_id=outcome.run.run_id,
        result="completed",
        receipt_digest=receipt_digest,
        artifact_references=tuple(
            [*outcome.execution_ids, *outcome.verification_ids]
        ),
        authority=contracts.authority,
        runtime_actor_id=RUNTIME_ACTOR_ID,
        at=_utc_now(),
        lease_owner_id=owner,
        lease_token=token,
    )
    verification_digest = canonical_digest(
        {
            "run_digest": outcome.run.run_digest,
            "verification_ids": list(outcome.verification_ids),
            "system_verified": True,
            "user_accepted": False,
        }
    )
    verified = service.verify_outcome(
        outcome_id="outcome:v10-f-real-agent",
        verification_digest=verification_digest,
        passed=True,
        authority=contracts.authority,
        verifier_id="runtime:yunos-v10-f-independent-verifier",
        at=_utc_now(),
        lease_owner_id=owner,
        lease_token=token,
    )
    learning_evidence = {
        "outcome_id": "outcome:v10-f-real-agent",
        "verification_digest": verification_digest,
        "run_id": outcome.run.run_id,
        "acceptance_state": "waiting_acceptance",
    }
    learning_anchor = SourceAnchor(
        anchor_id=make_identity("anchor", learning_evidence),
        source_kind="runtime",
        locator="cognitive-run:v10-f-real-agent-learning-boundary",
        evidence_digest=canonical_digest(learning_evidence),
        observed_at=_utc_now(),
        privacy_scope="private_metadata",
    )
    candidate_payloads = (
        (
            "fact_record",
            1_000_000,
            {
                "layer": "fact",
                "fact": "Two authorized sandbox tools executed and were independently verified.",
                "promotion_state": "pending",
                "long_term_profile_updated": False,
            },
        ),
        (
            "observation",
            800_000,
            {
                "layer": "observation",
                "observation": "The exact two-node bounded workflow succeeded once.",
                "promotion_state": "pending_repeated_evidence",
                "long_term_profile_updated": False,
            },
        ),
        (
            "workflow_candidate",
            600_000,
            {
                "layer": "workflow_candidate",
                "workflow": "Create one scoped file, then run the fixed git diff check.",
                "promotion_state": "pending_user_approval_or_repeated_evidence",
                "long_term_profile_updated": False,
            },
        ),
    )
    candidates = []
    for candidate_type, confidence, payload in candidate_payloads:
        candidate = {
            **payload,
            "outcome_id": "outcome:v10-f-real-agent",
            "verification_digest": verification_digest,
            "accepted": False,
        }
        candidates.append(
            operational.create_candidate(
                candidate_id=make_identity(
                    "candidate",
                    {"candidate_type": candidate_type, "candidate": candidate},
                ),
                project_id=PROJECT_ID,
                entity_id="outcome:v10-f-real-agent",
                entity_type="outcome",
                candidate_type=candidate_type,
                candidate=candidate,
                confidence_ppm=confidence,
                source_anchor=learning_anchor,
                created_at=_utc_now(),
                expires_at=contracts.event_times["expires"],
                authority=contracts.authority,
                lease_owner_id=owner,
                lease_token=token,
            )
        )
    persisted_outcome = next(
        (
            item
            for item in operational.list_outcomes()
            if item["outcome_id"] == "outcome:v10-f-real-agent"
        ),
        None,
    )
    if (
        verified["status"] != "verified"
        or persisted_outcome is None
        or persisted_outcome["status"] != "verified"
        or persisted_outcome["outcome"]["accepted"] is not False
        or any(item["status"] != "pending" for item in candidates)
    ):
        _fail("outcome_learning_boundary_failed")
    persisted = execution_store.get_run(outcome.run.run_id)
    if persisted.status != "waiting_acceptance":
        _fail("outcome_recording_changed_acceptance_state")
    return {
        "reported": reported,
        "verified": verified,
        "learning_candidates": candidates,
        "receipt_digest": receipt_digest,
        "verification_digest": verification_digest,
    }


@dataclass(frozen=True, slots=True, init=False)
class _AcceptanceExecutionContext:
    target: AcceptanceTarget
    provider_setup: ProviderSetup
    sources: tuple[ContextSource, ...]
    source_builder: SourceBuilder
    confirmation: ControllerConfirmation


def _execute_acceptance_context(
    context: _AcceptanceExecutionContext,
) -> dict[str, Any]:
    if type(context) is not _AcceptanceExecutionContext:
        _fail("exact_acceptance_execution_context_required")
    target = context.target
    if target.production is not False:
        _fail("production_acceptance_dependency_injection_forbidden")
    provider_setup = context.provider_setup
    sources = context.sources
    source_builder = context.source_builder
    confirmation = context.confirmation
    if confirmation.production is not False:
        _fail("mock_acceptance_production_seal_forbidden")
    schema = verify_cognitive_state_schema(target.operational_db)
    if schema["schema_generation"] != COGNITIVE_SCHEMA_GENERATION:
        _fail("production_cognitive_schema_10001_required")
    operational = OperationalStateStore(
        target.operational_db,
        allow_production_write=target.production,
    )
    initial_status = operational.read_status()
    if initial_status["lease"] is not None:
        _fail("writer_lease_must_be_zero_before_acceptance")
    with _pinned_output_directory_handles(target) as directory_pins:
        if target.output_root.exists():
            _fail("real_agent_output_root_already_exists")
        _create_pinned_output_root(
            target,
            directory_pins=directory_pins,
        )
        sandbox_root = target.output_root / SANDBOX_NAME
        _assert_pinned_output_root(target)
        _initialize_disposable_git_sandbox(
            sandbox_root,
            output_target=target,
            directory_pins=directory_pins,
        )
        _assert_pinned_output_root(target)
        artifact_root = sandbox_root / ".yunos-agent-artifacts"
        artifact_root.mkdir(exist_ok=False)
        directory_pins.assert_all()
        _assert_pinned_output_root(target)
        base = datetime.now(timezone.utc)
        contracts = _build_contracts(
            sandbox_root=sandbox_root,
            provider_setup=provider_setup,
            base=base,
        )
        context_factory = build_bounded_context_factory(
            objective=contracts.objective,
            sources=sources,
            capability_registry=contracts.capability_registry,
        )
        runtime_before = _runtime_counts(target.runtime_db)
        operational_before = _operational_snapshot(target.operational_db)
        sentry = {
            "version": "v10-f-real-agent-one-shot-sentry-1",
            "state": "DISPATCH_INTENT_DO_NOT_RERUN",
            "created_at": _utc_now(),
            "do_not_rerun": True,
            "production": target.production,
            "operational_database": str(target.operational_db),
            "runtime_database": str(target.runtime_db),
            "sandbox_root": str(sandbox_root),
            "objective_id": contracts.objective.objective_id,
            "objective_digest": contracts.objective.canonical_digest,
            "plan_id": contracts.plan.plan_id,
            "plan_digest": contracts.plan.plan_digest,
            "authorization_id": contracts.authorization.authorization_id,
            "authorization_digest": contracts.authorization.authority_digest,
            "provider_selection_digest": provider_setup.selection.selection_digest,
            "provider_health_attestation_digest": (
                provider_setup.selection.health_attestation_digest
            ),
            "readiness_digest": provider_setup.readiness_digest,
            "controller_confirmation_digest": confirmation.binding_digest,
            "user_authorization_sha256": confirmation.user_authorization_sha256,
            "user_approved_combined_boundary_sha256": (
                confirmation.combined_boundary_sha256
            ),
            "agent_external_send_disclosure_digest": (
                EXTERNAL_SEND_DISCLOSURE_DIGEST
            ),
            "maximum_model_calls": 2,
            "automatic_retry": False,
            "retained_guard_artifact": SENTRY_NAME,
            "retained_guard_required_before_dispatch": True,
            "runtime_counts_before": runtime_before,
        }
        directory_pins.assert_all()
        _write_pinned_output_exclusive(
            target=target,
            name=SENTRY_NAME,
            payload=sentry,
        )
        directory_pins.assert_all()
        sentry_guard = _RetainedArtifactGuard(
            target.output_root / SENTRY_NAME
        )
        sentry_guard.assert_current()
        publication_pins = directory_pins.retain()
        sentry_guard.assert_current()
    owner = "owner:yunos-v10-f-real-agent"
    token = secrets.token_hex(32)
    lease_acquired = False
    failure: BaseException | None = None
    recovery_summary: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    consent_ledger: TrustedConsentLedger | None = None
    execution_store: CognitiveExecutionStore | None = None
    run = None
    try:
        operational.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=_utc_now(),
            expires_at=_stamp(datetime.now(timezone.utc), 1800),
        )
        lease_acquired = True
        execution_store, planning_store, run = _persist_contracts(
            target=target,
            contracts=contracts,
            owner=owner,
            token=token,
        )
        consent_ledger = build_external_consent_factory(
            confirmation=confirmation,
            objective=contracts.objective,
            controller_binding=build_controller_confirmation_binding(
                confirmation=confirmation,
                contracts=contracts,
                run_id=run.run_id,
            ),
        )
        source = source_builder(
            objective=contracts.objective,
            provider_registry=contracts.provider_registry,
            provider_selection=contracts.provider_selection,
            context_factory=context_factory,
            consent_factory=consent_ledger,
        )
        if not isinstance(source, ProductionAgentRuntimeTurnSource):
            _fail("production_agent_runtime_turn_source_required")
        if target.production and source.source_kind != "production":
            _fail("production_agent_runtime_turn_source_required")
        if not target.production and source.source_kind != "mock":
            _fail("mock_agent_runtime_turn_source_required")
        overreach_evidence = _record_gateway_overreach(
            target=target,
            sandbox_root=sandbox_root,
            artifact_root=artifact_root,
            contracts=contracts,
            execution_store=execution_store,
            owner=owner,
            token=token,
        )
        loop = _make_loop(
            sandbox_root=sandbox_root,
            artifact_root=artifact_root,
            contracts=contracts,
            execution_store=execution_store,
            planning_store=planning_store,
            source=source,
            owner=owner,
            token=token,
        )
        sentry_guard.assert_current()
        outcome = loop.run(run.run_id)
        sentry_guard.assert_current()
        runtime_after_execution = _runtime_counts(target.runtime_db)
        first_delta = _count_delta(runtime_after_execution, runtime_before)
        if outcome.run.status != "waiting_acceptance":
            _fail("real_agent_two_turn_run_status_failed")
        if outcome.user_accepted is not False:
            _fail("real_agent_self_acceptance_boundary_failed")
        if outcome.model_sends != 2:
            _fail("real_agent_exact_model_send_count_failed")
        if len(outcome.proposal_ids) != 2:
            _fail("real_agent_exact_proposal_count_failed")
        if len(outcome.execution_ids) != 2:
            _fail("real_agent_exact_execution_count_failed")
        if len(outcome.verification_ids) != 2:
            _fail("real_agent_exact_verification_count_failed")
        if first_delta != _EXPECTED_TWO_TURN_RUNTIME_DELTA:
            _fail("real_agent_exact_runtime_delta_failed")
        checkpoint_before_replay = execution_store.latest_checkpoint(
            outcome.run.run_id
        )
        replay_loop = _make_loop(
            sandbox_root=sandbox_root,
            artifact_root=artifact_root,
            contracts=contracts,
            execution_store=execution_store,
            planning_store=planning_store,
            source=source,
            owner=owner,
            token=token,
        )
        replay = replay_loop.run(outcome.run.run_id)
        runtime_after_replay = _runtime_counts(target.runtime_db)
        checkpoint_after_replay = execution_store.latest_checkpoint(
            outcome.run.run_id
        )
        if (
            replay.run.status != "waiting_acceptance"
            or replay.model_sends != 0
            or runtime_after_replay != runtime_after_execution
            or checkpoint_after_replay.checkpoint_digest
            != checkpoint_before_replay.checkpoint_digest
        ):
            _fail("real_agent_replay_not_exactly_once")
        created_file = sandbox_root / CREATED_RELATIVE_PATH
        if (
            not created_file.is_file()
            or _sha256_file(created_file) != PATCHED_CONTENT_SHA256
        ):
            _fail("real_agent_patched_file_verification_failed")
        git_evidence = _verify_tracked_git_diff(
            sandbox_root=sandbox_root,
            artifact_root=artifact_root,
            contracts=contracts,
            execution_store=execution_store,
            run_id=outcome.run.run_id,
            proposal_ids=outcome.proposal_ids,
        )
        runtime_receipts = _runtime_receipt_evidence(
            target=target,
            execution_store=execution_store,
            run_id=outcome.run.run_id,
            proposal_ids=outcome.proposal_ids,
        )
        if target.production and runtime_receipts["mock_transport"] is not False:
            _fail("production_runtime_mock_transport_forbidden")
        if consent_ledger is None:
            _fail("trusted_consent_ledger_missing")
        consent_records = consent_ledger.assert_complete(2)
        consent_artifact = {
            "version": "v10-f-trusted-external-send-consents-1",
            "controller_confirmation": confirmation.to_dict(),
            "cli_confirmation_digest": confirmation.binding_digest,
            "controller_binding": consent_ledger.controller_binding,
            "controller_binding_digest": (
                consent_ledger.controller_binding_digest
            ),
            "trusted_local_controller": consent_ledger.controller.to_dict(),
            "records": list(consent_records),
        }
        consent_artifact["artifact_digest"] = canonical_digest(consent_artifact)
        _write_pinned_output_exclusive(
            target=target,
            name=CONSENT_BINDINGS_NAME,
            payload=consent_artifact,
        )
        rollback_evidence = _rehearse_owned_rollback(
            sandbox_root=sandbox_root,
            artifact_root=artifact_root,
            contracts=contracts,
            execution_store=execution_store,
            owner=owner,
            token=token,
        )
        operational_evidence = _record_outcome_and_learning(
            target=target,
            contracts=contracts,
            execution_store=execution_store,
            outcome=outcome,
            owner=owner,
            token=token,
        )
        final_run = execution_store.get_run(outcome.run.run_id)
        final_checkpoint = execution_store.latest_checkpoint(outcome.run.run_id)
        budget_state = dict(final_checkpoint.model_budget_state)
        if (
            final_run.status != "waiting_acceptance"
            or final_run.model_calls_used != 2
            or int(budget_state["calls_reserved"]) != 2
            or len(tuple(budget_state["settled_reservation_ids"])) != 2
            or int(budget_state["unknown_cost_reserved_ceiling_microunits"])
            > contracts.authorization.max_cost_microunits
        ):
            _fail("real_agent_model_budget_settlement_failed")
        operational_after = _operational_snapshot(target.operational_db)
        operational_delta = _operational_delta(
            operational_after,
            operational_before,
        )
        if operational_delta != _EXPECTED_OPERATIONAL_DELTA:
            _fail("operational_exact_delta_mismatch")
        result = {
            "version": ACCEPTANCE_VERSION,
            "status": "GO",
            "completed_at": _utc_now(),
            "production": target.production,
            "do_not_rerun": True,
            "schema": schema,
            "provider": {
                "provider_id": contracts.provider_selection.provider_id,
                "profile_version": contracts.provider_selection.profile_version,
                "profile_digest": contracts.provider_selection.profile_digest,
                "model": contracts.provider_selection.model,
                "selection_mode": contracts.provider_selection.selection_mode,
                "fallback_used": contracts.provider_selection.fallback_used,
                "selection_digest": contracts.provider_selection.selection_digest,
                "readiness_digest": provider_setup.readiness_digest,
                "external_send_disclosure": EXTERNAL_SEND_DISCLOSURE,
                "secret_reference_only": True,
            },
            "objective": {
                "objective_id": contracts.objective.objective_id,
                "objective_digest": contracts.objective.canonical_digest,
                "agency_level": contracts.objective.agency_level,
                "cognitive_stance": contracts.objective.cognitive_stance,
            },
            "plan": {
                "plan_id": contracts.plan.plan_id,
                "plan_digest": contracts.plan.plan_digest,
                "action_nodes": [
                    {
                        "node_id": item.node_id,
                        "node_digest": item.node_digest,
                        "capability_ids": [
                            requirement.capability_id
                            for requirement in item.capability_requirements
                        ],
                    }
                    for item in contracts.plan.nodes
                ],
            },
            "authorization": {
                "authorization_id": contracts.authorization.authorization_id,
                "authority_digest": contracts.authorization.authority_digest,
                "max_steps": contracts.authorization.max_steps,
                "max_model_calls": contracts.authorization.max_model_calls,
                "max_cost_microunits": contracts.authorization.max_cost_microunits,
                "commit": contracts.authorization.commit_permission,
                "merge": contracts.authorization.merge_permission,
                "tag": contracts.authorization.tag_permission,
                "push": contracts.authorization.push_permission,
                "network": contracts.authorization.network_permission,
                "database": contracts.authorization.database_permission,
                "next_version": contracts.authorization.next_version_creation_permission,
                "upstream_authority_id": contracts.upstream_authority.authority_id,
                "upstream_authority_digest": (
                    contracts.upstream_authority.authority_digest
                ),
                "upstream_read_scope": list(contracts.upstream_authority.read_scope),
                "upstream_write_scope": list(contracts.upstream_authority.write_scope),
                "upstream_commit": contracts.upstream_authority.commit_permission,
                "upstream_has_wildcard": (
                    "*" in contracts.upstream_authority.read_scope
                    or "*" in contracts.upstream_authority.write_scope
                ),
            },
            "agent": {
                "run_id": final_run.run_id,
                "run_digest": final_run.run_digest,
                "status": final_run.status,
                "model_sends": outcome.model_sends,
                "replay_model_sends": replay.model_sends,
                "model_calls_used": final_run.model_calls_used,
                "tokens_used": final_run.tokens_used,
                "cost_used_microunits": final_run.cost_used_microunits,
                "steps_used": final_run.steps_used,
                "proposal_ids": list(outcome.proposal_ids),
                "execution_ids": list(outcome.execution_ids),
                "verification_ids": list(outcome.verification_ids),
                "user_accepted": False,
                "budget_state": budget_state,
            },
            "runtime": {
                "counts_before": runtime_before,
                "counts_after_execution": runtime_after_execution,
                "counts_after_replay": runtime_after_replay,
                "delta": first_delta,
                "expected_delta": _EXPECTED_TWO_TURN_RUNTIME_DELTA,
                "automatic_retry": False,
                **runtime_receipts,
            },
            "sandbox": {
                "root": str(sandbox_root),
                "git_repository": (sandbox_root / ".git").is_dir(),
                "publication_guard": {
                    "artifact": SENTRY_NAME,
                    "held_before_dispatch": True,
                    "delete_sharing": False,
                },
                "tracked_relative_path": CREATED_RELATIVE_PATH,
                "baseline_sha256": BASELINE_CONTENT_SHA256,
                "patched_sha256": PATCHED_CONTENT_SHA256,
                "artifact_root": str(artifact_root),
                "git_diff": git_evidence,
                "rollback_rehearsal": rollback_evidence,
            },
            "outcome_boundary": {
                "outcome_id": "outcome:v10-f-real-agent",
                "reported": True,
                "system_verified": True,
                "user_accepted": False,
                "cognitive_run_status": final_run.status,
                "verification_digest": operational_evidence["verification_digest"],
            },
            "learning": {
                "candidate_types": ["fact_record", "observation", "workflow_candidate"],
                "all_pending": True,
                "long_term_profile_updated": False,
            },
            "overreach": {
                **overreach_evidence,
                "project_completion_updated": False,
            },
            "trusted_external_send": {
                "user_utterance_sha256": USER_CONSENT_TEXT_SHA256,
                "user_approved_combined_boundary_sha256": (
                    confirmation.combined_boundary_sha256
                ),
                "agent_external_send_disclosure_digest": (
                    EXTERNAL_SEND_DISCLOSURE_DIGEST
                ),
                "trusted_user_event_verified": True,
                "cli_confirmation_digest": confirmation.binding_digest,
                "controller_binding_digest": (
                    consent_ledger.controller_binding_digest
                ),
                "controller_authority_digest": (
                    consent_ledger.controller.authority_digest
                ),
                "consent_artifact": str(
                    target.output_root / CONSENT_BINDINGS_NAME
                ),
                "consent_artifact_digest": consent_artifact["artifact_digest"],
                "trusted_user_event_ids": [
                    item["trusted_user_event"]["event_id"]
                    for item in consent_records
                ],
                "consent_ids": [
                    item["external_send_consent"]["consent_id"]
                    for item in consent_records
                ],
            },
            "operational": {
                "counts_before": operational_before["counts"],
                "counts_after": operational_after["counts"],
                "delta": operational_delta,
                "expected_delta": _EXPECTED_OPERATIONAL_DELTA,
                "semantic_ledger_digest_before": (
                    operational_before["semantic_ledger_digest"]
                ),
                "semantic_ledger_digest_after": (
                    operational_after["semantic_ledger_digest"]
                ),
                "integrity_check": operational_after["integrity_check"],
                "foreign_key_violations": operational_after[
                    "foreign_key_violations"
                ],
            },
        }
    except BaseException as exc:
        failure = exc
        if lease_acquired:
            try:
                recovery_summary = _recover_acceptance_failure(
                    target=target,
                    contracts=contracts,
                    execution_store=execution_store,
                    consent_ledger=consent_ledger,
                    reason_code=getattr(exc, "reason_code", type(exc).__name__),
                    owner=owner,
                    token=token,
                )
            except BaseException as recovery_error:
                recovery_summary = {
                    "reason_code": getattr(
                        exc, "reason_code", type(exc).__name__
                    ),
                    "recovery_error_code": getattr(
                        recovery_error,
                        "reason_code",
                        type(recovery_error).__name__,
                    ),
                }
    finally:
        if lease_acquired:
            try:
                operational.release_writer_lease(
                    owner_id=owner,
                    token=token,
                    released_at=_utc_now(),
                )
            except BaseException as release_error:
                if failure is None:
                    failure = release_error
    final_status = operational.read_status()
    if final_status["lease"] is not None:
        failure = V10FRealAgentAcceptanceError("writer_lease_release_not_verified")
    if failure is None:
        if result is None:
            failure = V10FRealAgentAcceptanceError("real_agent_result_missing")
        else:
            result["writer"] = {
                "lease_before": initial_status["lease"],
                "lease_after": final_status["lease"],
                "writer_default_enabled": final_status["writer_default_enabled"],
                "writer_enabled_for_process_during_explicit_run": target.production,
                "released_in_finally": True,
            }
            result["report_digest"] = canonical_digest(result)
            try:
                sentry_guard.assert_current()
                publication_pins.assert_all()
                _write_pinned_result_exclusive(
                    target=target,
                    payload=result,
                )
                publication_pins.assert_all()
                sentry_guard.assert_current()
            except BaseException as result_error:
                failure = result_error
                recovery_owner = "owner:yunos-v10-f-result-write-recovery"
                recovery_token = secrets.token_hex(32)
                try:
                    operational.acquire_writer_lease(
                        owner_id=recovery_owner,
                        token=recovery_token,
                        acquired_at=_utc_now(),
                        expires_at=_stamp(datetime.now(timezone.utc), 300),
                    )
                    recovery_summary = _recover_acceptance_failure(
                        target=target,
                        contracts=contracts,
                        execution_store=execution_store,
                        consent_ledger=consent_ledger,
                        reason_code=getattr(
                            result_error,
                            "reason_code",
                            type(result_error).__name__,
                        ),
                        owner=recovery_owner,
                        token=recovery_token,
                    )
                except BaseException as recovery_error:
                    recovery_summary = {
                        "reason_code": getattr(
                            result_error,
                            "reason_code",
                            type(result_error).__name__,
                        ),
                        "recovery_error_code": getattr(
                            recovery_error,
                            "reason_code",
                            type(recovery_error).__name__,
                        ),
                    }
                finally:
                    try:
                        operational.release_writer_lease(
                            owner_id=recovery_owner,
                            token=recovery_token,
                            released_at=_utc_now(),
                        )
                    except BaseException:
                        pass
                final_status = operational.read_status()
    if failure is not None:
        reason_code = getattr(failure, "reason_code", type(failure).__name__)
        failure_record = {
            "version": ACCEPTANCE_VERSION,
            "status": "NO_GO_DO_NOT_RERUN",
            "failed_at": _utc_now(),
            "reason_code": reason_code,
            "do_not_rerun": True,
            "writer_lease_released": final_status["lease"] is None,
            "recovery": recovery_summary,
            "sanitized": True,
        }
        try:
            sentry_guard.assert_current()
            publication_pins.assert_all()
            _write_pinned_output_exclusive(
                target=target,
                name=FAILURE_NAME,
                payload=failure_record,
            )
            publication_pins.assert_all()
            sentry_guard.assert_current()
        except (OSError, V10FRealAgentAcceptanceError):
            pass
        finally:
            publication_pins.close()
            sentry_guard.close()
        raise failure
    if result is None:
        publication_pins.close()
        sentry_guard.close()
        _fail("real_agent_result_missing")
    try:
        sentry_guard.assert_current()
        publication_pins.assert_all()
    finally:
        publication_pins.close()
        sentry_guard.close()
    return result


def _new_test_execution_context(
    *,
    target: AcceptanceTarget,
    provider_setup: ProviderSetup,
    sources: Sequence[ContextSource],
    source_builder: SourceBuilder,
    confirmation: ControllerConfirmation,
) -> _AcceptanceExecutionContext:
    if target.production:
        _fail("production_acceptance_dependency_injection_forbidden")
    context = object.__new__(_AcceptanceExecutionContext)
    object.__setattr__(context, "target", target)
    object.__setattr__(context, "provider_setup", provider_setup)
    object.__setattr__(context, "sources", tuple(sources))
    object.__setattr__(context, "source_builder", source_builder)
    object.__setattr__(context, "confirmation", confirmation)
    return context


def execute_acceptance(
    *,
    target: AcceptanceTarget,
    provider_setup: ProviderSetup,
    sources: Sequence[ContextSource],
    source_builder: SourceBuilder,
    confirmation: ControllerConfirmation,
) -> dict[str, Any]:
    if target.production:
        _fail("production_acceptance_dependency_injection_forbidden")
    return _execute_acceptance_context(
        _new_test_execution_context(
            target=target,
            provider_setup=provider_setup,
            sources=sources,
            source_builder=source_builder,
            confirmation=confirmation,
        )
    )


def mock_source_builder(
    bridge: MockCognitiveRuntimeBridge,
) -> SourceBuilder:
    def build(**kwargs: Any) -> ProductionAgentRuntimeTurnSource:
        return create_mock_agent_runtime_source(
            **kwargs,
            bridge=bridge,
            limits=AgentRuntimeModelLimits(
                maximum_context_items=8,
                maximum_context_bytes=65_536,
                maximum_input_tokens=8_192,
                maximum_total_tokens=8_192 + PRODUCTION_FIXED_OUTPUT_TOKENS,
                maximum_cost_microunits=2_000_000,
                maximum_output_bytes=65_536,
            ),
            trusted_clock=_utc_now,
        )

    return build


class _FixedProductionAcceptanceExecutor:
    __slots__ = ()

    def run(
        self,
        *,
        confirmation: ControllerConfirmation,
    ) -> dict[str, Any]:
        if type(self) is not _FixedProductionAcceptanceExecutor:
            _fail("exact_production_acceptance_executor_required")
        if type(confirmation) is not ControllerConfirmation:
            _fail("exact_controller_confirmation_required")
        try:
            confirmation_complete = (
                confirmation.execute is True
                and confirmation.allow_production_operational_write is True
                and confirmation.allow_official_readiness_gets is True
                and confirmation.confirm_one_shot is True
                and confirmation.confirm_two_deepseek_tool_proposals is True
                and confirmation.confirm_disposable_sandbox is True
                and confirmation.user_authorization_sha256
                == USER_CONSENT_TEXT_SHA256
                and confirmation.combined_boundary_sha256
                == USER_APPROVED_COMBINED_BOUNDARY_SHA256
                and confirmation.production is True
            )
        except AttributeError:
            confirmation_complete = False
        if not confirmation_complete:
            _fail("production_controller_confirmation_required")
        if os.environ.get(PRODUCTION_WRITE_ENV, "").strip() != "1":
            _fail("production_operational_write_environment_gate_required")
        target = AcceptanceTarget(
            operational_db=PRODUCTION_OPERATIONAL_DB,
            runtime_db=PRODUCTION_RUNTIME_DB,
            output_root=PRODUCTION_OUTPUT_ROOT,
            production=True,
        )
        readiness_authority = open_production_deepseek_readiness(
            execute=True,
            allow_official_readiness_gets=True,
            confirm_no_chat_completion=True,
        )
        readiness = run_production_deepseek_readiness(
            authority=readiness_authority
        )
        if (
            readiness.get("status") != "ready"
            or readiness.get("chat_completion_sent") is not False
        ):
            _fail("deepseek_readiness_failed_without_chat_completion")
        checked_at = _runtime_bridge_compatible_now()
        provider_setup = build_deepseek_provider_setup(
            readiness,
            checked_at=checked_at,
        )
        sources = production_context_sources(evaluated_at=checked_at)

        def source_builder(**kwargs: Any) -> ProductionAgentRuntimeTurnSource:
            return create_production_agent_runtime_source(
                **kwargs,
                limits=AgentRuntimeModelLimits(
                    maximum_context_items=8,
                    maximum_context_bytes=65_536,
                    maximum_input_tokens=8_192,
                    maximum_total_tokens=(
                        8_192 + PRODUCTION_FIXED_OUTPUT_TOKENS
                    ),
                    maximum_cost_microunits=2_000_000,
                    maximum_output_bytes=65_536,
                ),
                execute=True,
                allow_external_model_call=True,
                confirm_deepseek_send=True,
                trusted_clock=_utc_now,
            )

        schema = verify_cognitive_state_schema(target.operational_db)
        if schema["schema_generation"] != COGNITIVE_SCHEMA_GENERATION:
            _fail("production_cognitive_schema_10001_required")
        operational = OperationalStateStore(
            target.operational_db,
            allow_production_write=target.production,
        )
        initial_status = operational.read_status()
        if initial_status["lease"] is not None:
            _fail("writer_lease_must_be_zero_before_acceptance")
        with _pinned_output_directory_handles(target) as directory_pins:
            if target.output_root.exists():
                _fail("real_agent_output_root_already_exists")
            _create_pinned_output_root(
                target,
                directory_pins=directory_pins,
            )
            sandbox_root = target.output_root / SANDBOX_NAME
            _assert_pinned_output_root(target)
            _initialize_disposable_git_sandbox(
                sandbox_root,
                output_target=target,
                directory_pins=directory_pins,
            )
            _assert_pinned_output_root(target)
            artifact_root = sandbox_root / ".yunos-agent-artifacts"
            artifact_root.mkdir(exist_ok=False)
            directory_pins.assert_all()
            _assert_pinned_output_root(target)
            base = datetime.now(timezone.utc)
            contracts = _build_contracts(
                sandbox_root=sandbox_root,
                provider_setup=provider_setup,
                base=base,
            )
            context_factory = build_bounded_context_factory(
                objective=contracts.objective,
                sources=sources,
                capability_registry=contracts.capability_registry,
            )
            runtime_before = _runtime_counts(target.runtime_db)
            operational_before = _operational_snapshot(target.operational_db)
            sentry = {
                "version": "v10-f-real-agent-one-shot-sentry-1",
                "state": "DISPATCH_INTENT_DO_NOT_RERUN",
                "created_at": _utc_now(),
                "do_not_rerun": True,
                "production": target.production,
                "operational_database": str(target.operational_db),
                "runtime_database": str(target.runtime_db),
                "sandbox_root": str(sandbox_root),
                "objective_id": contracts.objective.objective_id,
                "objective_digest": contracts.objective.canonical_digest,
                "plan_id": contracts.plan.plan_id,
                "plan_digest": contracts.plan.plan_digest,
                "authorization_id": contracts.authorization.authorization_id,
                "authorization_digest": contracts.authorization.authority_digest,
                "provider_selection_digest": provider_setup.selection.selection_digest,
                "provider_health_attestation_digest": (
                    provider_setup.selection.health_attestation_digest
                ),
                "readiness_digest": provider_setup.readiness_digest,
                "controller_confirmation_digest": confirmation.binding_digest,
                "user_authorization_sha256": confirmation.user_authorization_sha256,
                "user_approved_combined_boundary_sha256": (
                    confirmation.combined_boundary_sha256
                ),
                "agent_external_send_disclosure_digest": (
                    EXTERNAL_SEND_DISCLOSURE_DIGEST
                ),
                "maximum_model_calls": 2,
                "automatic_retry": False,
                "retained_guard_artifact": SENTRY_NAME,
                "retained_guard_required_before_dispatch": True,
                "runtime_counts_before": runtime_before,
            }
            directory_pins.assert_all()
            _write_pinned_output_exclusive(
                target=target,
                name=SENTRY_NAME,
                payload=sentry,
            )
            directory_pins.assert_all()
            sentry_guard = _RetainedArtifactGuard(
                target.output_root / SENTRY_NAME
            )
            sentry_guard.assert_current()
            publication_pins = directory_pins.retain()
            sentry_guard.assert_current()
        owner = "owner:yunos-v10-f-real-agent"
        token = secrets.token_hex(32)
        lease_acquired = False
        failure: BaseException | None = None
        recovery_summary: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        consent_ledger: TrustedConsentLedger | None = None
        execution_store: CognitiveExecutionStore | None = None
        run = None
        try:
            operational.acquire_writer_lease(
                owner_id=owner,
                token=token,
                acquired_at=_utc_now(),
                expires_at=_stamp(datetime.now(timezone.utc), 1800),
            )
            lease_acquired = True
            execution_store, planning_store, run = _persist_contracts(
                target=target,
                contracts=contracts,
                owner=owner,
                token=token,
            )
            consent_ledger = build_external_consent_factory(
                confirmation=confirmation,
                objective=contracts.objective,
                controller_binding=build_controller_confirmation_binding(
                    confirmation=confirmation,
                    contracts=contracts,
                    run_id=run.run_id,
                ),
            )
            source = source_builder(
                objective=contracts.objective,
                provider_registry=contracts.provider_registry,
                provider_selection=contracts.provider_selection,
                context_factory=context_factory,
                consent_factory=consent_ledger,
            )
            if not isinstance(source, ProductionAgentRuntimeTurnSource):
                _fail("production_agent_runtime_turn_source_required")
            if target.production and source.source_kind != "production":
                _fail("production_agent_runtime_turn_source_required")
            if not target.production and source.source_kind != "mock":
                _fail("mock_agent_runtime_turn_source_required")
            overreach_evidence = _record_gateway_overreach(
                target=target,
                sandbox_root=sandbox_root,
                artifact_root=artifact_root,
                contracts=contracts,
                execution_store=execution_store,
                owner=owner,
                token=token,
            )
            loop = _make_loop(
                sandbox_root=sandbox_root,
                artifact_root=artifact_root,
                contracts=contracts,
                execution_store=execution_store,
                planning_store=planning_store,
                source=source,
                owner=owner,
                token=token,
            )
            sentry_guard.assert_current()
            outcome = loop.run(run.run_id)
            sentry_guard.assert_current()
            runtime_after_execution = _runtime_counts(target.runtime_db)
            first_delta = _count_delta(runtime_after_execution, runtime_before)
            if outcome.run.status != "waiting_acceptance":
                _fail("real_agent_two_turn_run_status_failed")
            if outcome.user_accepted is not False:
                _fail("real_agent_self_acceptance_boundary_failed")
            if outcome.model_sends != 2:
                _fail("real_agent_exact_model_send_count_failed")
            if len(outcome.proposal_ids) != 2:
                _fail("real_agent_exact_proposal_count_failed")
            if len(outcome.execution_ids) != 2:
                _fail("real_agent_exact_execution_count_failed")
            if len(outcome.verification_ids) != 2:
                _fail("real_agent_exact_verification_count_failed")
            if first_delta != _EXPECTED_TWO_TURN_RUNTIME_DELTA:
                _fail("real_agent_exact_runtime_delta_failed")
            checkpoint_before_replay = execution_store.latest_checkpoint(
                outcome.run.run_id
            )
            replay_loop = _make_loop(
                sandbox_root=sandbox_root,
                artifact_root=artifact_root,
                contracts=contracts,
                execution_store=execution_store,
                planning_store=planning_store,
                source=source,
                owner=owner,
                token=token,
            )
            replay = replay_loop.run(outcome.run.run_id)
            runtime_after_replay = _runtime_counts(target.runtime_db)
            checkpoint_after_replay = execution_store.latest_checkpoint(
                outcome.run.run_id
            )
            if (
                replay.run.status != "waiting_acceptance"
                or replay.model_sends != 0
                or runtime_after_replay != runtime_after_execution
                or checkpoint_after_replay.checkpoint_digest
                != checkpoint_before_replay.checkpoint_digest
            ):
                _fail("real_agent_replay_not_exactly_once")
            created_file = sandbox_root / CREATED_RELATIVE_PATH
            if (
                not created_file.is_file()
                or _sha256_file(created_file) != PATCHED_CONTENT_SHA256
            ):
                _fail("real_agent_patched_file_verification_failed")
            git_evidence = _verify_tracked_git_diff(
                sandbox_root=sandbox_root,
                artifact_root=artifact_root,
                contracts=contracts,
                execution_store=execution_store,
                run_id=outcome.run.run_id,
                proposal_ids=outcome.proposal_ids,
            )
            runtime_receipts = _runtime_receipt_evidence(
                target=target,
                execution_store=execution_store,
                run_id=outcome.run.run_id,
                proposal_ids=outcome.proposal_ids,
            )
            if target.production and runtime_receipts["mock_transport"] is not False:
                _fail("production_runtime_mock_transport_forbidden")
            if consent_ledger is None:
                _fail("trusted_consent_ledger_missing")
            consent_records = consent_ledger.assert_complete(2)
            consent_artifact = {
                "version": "v10-f-trusted-external-send-consents-1",
                "controller_confirmation": confirmation.to_dict(),
                "cli_confirmation_digest": confirmation.binding_digest,
                "controller_binding": consent_ledger.controller_binding,
                "controller_binding_digest": (
                    consent_ledger.controller_binding_digest
                ),
                "trusted_local_controller": consent_ledger.controller.to_dict(),
                "records": list(consent_records),
            }
            consent_artifact["artifact_digest"] = canonical_digest(consent_artifact)
            _write_pinned_output_exclusive(
                target=target,
                name=CONSENT_BINDINGS_NAME,
                payload=consent_artifact,
            )
            rollback_evidence = _rehearse_owned_rollback(
                sandbox_root=sandbox_root,
                artifact_root=artifact_root,
                contracts=contracts,
                execution_store=execution_store,
                owner=owner,
                token=token,
            )
            operational_evidence = _record_outcome_and_learning(
                target=target,
                contracts=contracts,
                execution_store=execution_store,
                outcome=outcome,
                owner=owner,
                token=token,
            )
            final_run = execution_store.get_run(outcome.run.run_id)
            final_checkpoint = execution_store.latest_checkpoint(outcome.run.run_id)
            budget_state = dict(final_checkpoint.model_budget_state)
            if (
                final_run.status != "waiting_acceptance"
                or final_run.model_calls_used != 2
                or int(budget_state["calls_reserved"]) != 2
                or len(tuple(budget_state["settled_reservation_ids"])) != 2
                or int(budget_state["unknown_cost_reserved_ceiling_microunits"])
                > contracts.authorization.max_cost_microunits
            ):
                _fail("real_agent_model_budget_settlement_failed")
            operational_after = _operational_snapshot(target.operational_db)
            operational_delta = _operational_delta(
                operational_after,
                operational_before,
            )
            if operational_delta != _EXPECTED_OPERATIONAL_DELTA:
                _fail("operational_exact_delta_mismatch")
            result = {
                "version": ACCEPTANCE_VERSION,
                "status": "GO",
                "completed_at": _utc_now(),
                "production": target.production,
                "do_not_rerun": True,
                "schema": schema,
                "provider": {
                    "provider_id": contracts.provider_selection.provider_id,
                    "profile_version": contracts.provider_selection.profile_version,
                    "profile_digest": contracts.provider_selection.profile_digest,
                    "model": contracts.provider_selection.model,
                    "selection_mode": contracts.provider_selection.selection_mode,
                    "fallback_used": contracts.provider_selection.fallback_used,
                    "selection_digest": contracts.provider_selection.selection_digest,
                    "readiness_digest": provider_setup.readiness_digest,
                    "external_send_disclosure": EXTERNAL_SEND_DISCLOSURE,
                    "secret_reference_only": True,
                },
                "objective": {
                    "objective_id": contracts.objective.objective_id,
                    "objective_digest": contracts.objective.canonical_digest,
                    "agency_level": contracts.objective.agency_level,
                    "cognitive_stance": contracts.objective.cognitive_stance,
                },
                "plan": {
                    "plan_id": contracts.plan.plan_id,
                    "plan_digest": contracts.plan.plan_digest,
                    "action_nodes": [
                        {
                            "node_id": item.node_id,
                            "node_digest": item.node_digest,
                            "capability_ids": [
                                requirement.capability_id
                                for requirement in item.capability_requirements
                            ],
                        }
                        for item in contracts.plan.nodes
                    ],
                },
                "authorization": {
                    "authorization_id": contracts.authorization.authorization_id,
                    "authority_digest": contracts.authorization.authority_digest,
                    "max_steps": contracts.authorization.max_steps,
                    "max_model_calls": contracts.authorization.max_model_calls,
                    "max_cost_microunits": contracts.authorization.max_cost_microunits,
                    "commit": contracts.authorization.commit_permission,
                    "merge": contracts.authorization.merge_permission,
                    "tag": contracts.authorization.tag_permission,
                    "push": contracts.authorization.push_permission,
                    "network": contracts.authorization.network_permission,
                    "database": contracts.authorization.database_permission,
                    "next_version": contracts.authorization.next_version_creation_permission,
                    "upstream_authority_id": contracts.upstream_authority.authority_id,
                    "upstream_authority_digest": (
                        contracts.upstream_authority.authority_digest
                    ),
                    "upstream_read_scope": list(contracts.upstream_authority.read_scope),
                    "upstream_write_scope": list(contracts.upstream_authority.write_scope),
                    "upstream_commit": contracts.upstream_authority.commit_permission,
                    "upstream_has_wildcard": (
                        "*" in contracts.upstream_authority.read_scope
                        or "*" in contracts.upstream_authority.write_scope
                    ),
                },
                "agent": {
                    "run_id": final_run.run_id,
                    "run_digest": final_run.run_digest,
                    "status": final_run.status,
                    "model_sends": outcome.model_sends,
                    "replay_model_sends": replay.model_sends,
                    "model_calls_used": final_run.model_calls_used,
                    "tokens_used": final_run.tokens_used,
                    "cost_used_microunits": final_run.cost_used_microunits,
                    "steps_used": final_run.steps_used,
                    "proposal_ids": list(outcome.proposal_ids),
                    "execution_ids": list(outcome.execution_ids),
                    "verification_ids": list(outcome.verification_ids),
                    "user_accepted": False,
                    "budget_state": budget_state,
                },
                "runtime": {
                    "counts_before": runtime_before,
                    "counts_after_execution": runtime_after_execution,
                    "counts_after_replay": runtime_after_replay,
                    "delta": first_delta,
                    "expected_delta": _EXPECTED_TWO_TURN_RUNTIME_DELTA,
                    "automatic_retry": False,
                    **runtime_receipts,
                },
                "sandbox": {
                    "root": str(sandbox_root),
                    "git_repository": (sandbox_root / ".git").is_dir(),
                    "publication_guard": {
                        "artifact": SENTRY_NAME,
                        "held_before_dispatch": True,
                        "delete_sharing": False,
                    },
                    "tracked_relative_path": CREATED_RELATIVE_PATH,
                    "baseline_sha256": BASELINE_CONTENT_SHA256,
                    "patched_sha256": PATCHED_CONTENT_SHA256,
                    "artifact_root": str(artifact_root),
                    "git_diff": git_evidence,
                    "rollback_rehearsal": rollback_evidence,
                },
                "outcome_boundary": {
                    "outcome_id": "outcome:v10-f-real-agent",
                    "reported": True,
                    "system_verified": True,
                    "user_accepted": False,
                    "cognitive_run_status": final_run.status,
                    "verification_digest": operational_evidence["verification_digest"],
                },
                "learning": {
                    "candidate_types": ["fact_record", "observation", "workflow_candidate"],
                    "all_pending": True,
                    "long_term_profile_updated": False,
                },
                "overreach": {
                    **overreach_evidence,
                    "project_completion_updated": False,
                },
                "trusted_external_send": {
                    "user_utterance_sha256": USER_CONSENT_TEXT_SHA256,
                    "user_approved_combined_boundary_sha256": (
                        confirmation.combined_boundary_sha256
                    ),
                    "agent_external_send_disclosure_digest": (
                        EXTERNAL_SEND_DISCLOSURE_DIGEST
                    ),
                    "trusted_user_event_verified": True,
                    "cli_confirmation_digest": confirmation.binding_digest,
                    "controller_binding_digest": (
                        consent_ledger.controller_binding_digest
                    ),
                    "controller_authority_digest": (
                        consent_ledger.controller.authority_digest
                    ),
                    "consent_artifact": str(
                        target.output_root / CONSENT_BINDINGS_NAME
                    ),
                    "consent_artifact_digest": consent_artifact["artifact_digest"],
                    "trusted_user_event_ids": [
                        item["trusted_user_event"]["event_id"]
                        for item in consent_records
                    ],
                    "consent_ids": [
                        item["external_send_consent"]["consent_id"]
                        for item in consent_records
                    ],
                },
                "operational": {
                    "counts_before": operational_before["counts"],
                    "counts_after": operational_after["counts"],
                    "delta": operational_delta,
                    "expected_delta": _EXPECTED_OPERATIONAL_DELTA,
                    "semantic_ledger_digest_before": (
                        operational_before["semantic_ledger_digest"]
                    ),
                    "semantic_ledger_digest_after": (
                        operational_after["semantic_ledger_digest"]
                    ),
                    "integrity_check": operational_after["integrity_check"],
                    "foreign_key_violations": operational_after[
                        "foreign_key_violations"
                    ],
                },
            }
        except BaseException as exc:
            failure = exc
            if lease_acquired:
                try:
                    recovery_summary = _recover_acceptance_failure(
                        target=target,
                        contracts=contracts,
                        execution_store=execution_store,
                        consent_ledger=consent_ledger,
                        reason_code=getattr(exc, "reason_code", type(exc).__name__),
                        owner=owner,
                        token=token,
                    )
                except BaseException as recovery_error:
                    recovery_summary = {
                        "reason_code": getattr(
                            exc, "reason_code", type(exc).__name__
                        ),
                        "recovery_error_code": getattr(
                            recovery_error,
                            "reason_code",
                            type(recovery_error).__name__,
                        ),
                    }
        finally:
            if lease_acquired:
                try:
                    operational.release_writer_lease(
                        owner_id=owner,
                        token=token,
                        released_at=_utc_now(),
                    )
                except BaseException as release_error:
                    if failure is None:
                        failure = release_error
        final_status = operational.read_status()
        if final_status["lease"] is not None:
            failure = V10FRealAgentAcceptanceError("writer_lease_release_not_verified")
        if failure is None:
            if result is None:
                failure = V10FRealAgentAcceptanceError("real_agent_result_missing")
            else:
                result["writer"] = {
                    "lease_before": initial_status["lease"],
                    "lease_after": final_status["lease"],
                    "writer_default_enabled": final_status["writer_default_enabled"],
                    "writer_enabled_for_process_during_explicit_run": target.production,
                    "released_in_finally": True,
                }
                result["report_digest"] = canonical_digest(result)
                try:
                    sentry_guard.assert_current()
                    publication_pins.assert_all()
                    _write_pinned_result_exclusive(
                        target=target,
                        payload=result,
                    )
                    publication_pins.assert_all()
                    sentry_guard.assert_current()
                except BaseException as result_error:
                    failure = result_error
                    recovery_owner = "owner:yunos-v10-f-result-write-recovery"
                    recovery_token = secrets.token_hex(32)
                    try:
                        operational.acquire_writer_lease(
                            owner_id=recovery_owner,
                            token=recovery_token,
                            acquired_at=_utc_now(),
                            expires_at=_stamp(datetime.now(timezone.utc), 300),
                        )
                        recovery_summary = _recover_acceptance_failure(
                            target=target,
                            contracts=contracts,
                            execution_store=execution_store,
                            consent_ledger=consent_ledger,
                            reason_code=getattr(
                                result_error,
                                "reason_code",
                                type(result_error).__name__,
                            ),
                            owner=recovery_owner,
                            token=recovery_token,
                        )
                    except BaseException as recovery_error:
                        recovery_summary = {
                            "reason_code": getattr(
                                result_error,
                                "reason_code",
                                type(result_error).__name__,
                            ),
                            "recovery_error_code": getattr(
                                recovery_error,
                                "reason_code",
                                type(recovery_error).__name__,
                            ),
                        }
                    finally:
                        try:
                            operational.release_writer_lease(
                                owner_id=recovery_owner,
                                token=recovery_token,
                                released_at=_utc_now(),
                            )
                        except BaseException:
                            pass
                    final_status = operational.read_status()
        if failure is not None:
            reason_code = getattr(failure, "reason_code", type(failure).__name__)
            failure_record = {
                "version": ACCEPTANCE_VERSION,
                "status": "NO_GO_DO_NOT_RERUN",
                "failed_at": _utc_now(),
                "reason_code": reason_code,
                "do_not_rerun": True,
                "writer_lease_released": final_status["lease"] is None,
                "recovery": recovery_summary,
                "sanitized": True,
            }
            try:
                sentry_guard.assert_current()
                publication_pins.assert_all()
                _write_pinned_output_exclusive(
                    target=target,
                    name=FAILURE_NAME,
                    payload=failure_record,
                )
                publication_pins.assert_all()
                sentry_guard.assert_current()
            except (OSError, V10FRealAgentAcceptanceError):
                pass
            finally:
                publication_pins.close()
                sentry_guard.close()
            raise failure
        if result is None:
            publication_pins.close()
            sentry_guard.close()
            _fail("real_agent_result_missing")
        try:
            sentry_guard.assert_current()
            publication_pins.assert_all()
        finally:
            publication_pins.close()
            sentry_guard.close()
        return result



_FIXED_PRODUCTION_ACCEPTANCE_EXECUTOR = _FixedProductionAcceptanceExecutor()


def run_production_acceptance(
    *,
    confirmation: ControllerConfirmation,
) -> dict[str, Any]:
    return _FIXED_PRODUCTION_ACCEPTANCE_EXECUTOR.run(
        confirmation=confirmation
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot YunOS v10-F real DeepSeek plus bounded native Agent "
            "acceptance. Paths and Provider are code-owned and cannot be overridden."
        )
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-production-operational-write", action="store_true")
    parser.add_argument("--allow-official-readiness-gets", action="store_true")
    parser.add_argument("--confirm-one-shot", action="store_true")
    parser.add_argument("--confirm-two-deepseek-tool-proposals", action="store_true")
    parser.add_argument("--confirm-disposable-sandbox", action="store_true")
    parser.add_argument("--confirm-user-authorization-sha256")
    parser.add_argument("--confirm-combined-boundary-sha256")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    required = {
        "--execute": args.execute,
        "--allow-production-operational-write": args.allow_production_operational_write,
        "--allow-official-readiness-gets": args.allow_official_readiness_gets,
        "--confirm-one-shot": args.confirm_one_shot,
        "--confirm-two-deepseek-tool-proposals": (
            args.confirm_two_deepseek_tool_proposals
        ),
        "--confirm-disposable-sandbox": args.confirm_disposable_sandbox,
        "--confirm-user-authorization-sha256": (
            args.confirm_user_authorization_sha256
            == USER_CONSENT_TEXT_SHA256
        ),
        "--confirm-combined-boundary-sha256": (
            args.confirm_combined_boundary_sha256
            == USER_APPROVED_COMBINED_BOUNDARY_SHA256
        ),
    }
    if not all(required.values()):
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "external_model_calls": 0,
                    "production_database_writes": 0,
                    "required_flags": [name for name, present in required.items() if not present],
                    "required_environment": f"{PRODUCTION_WRITE_ENV}=1",
                    "operational_database": str(PRODUCTION_OPERATIONAL_DB),
                    "runtime_database": str(PRODUCTION_RUNTIME_DB),
                    "output_root": str(PRODUCTION_OUTPUT_ROOT),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    try:
        confirmation = ControllerConfirmation(
            execute=args.execute,
            allow_production_operational_write=(
                args.allow_production_operational_write
            ),
            allow_official_readiness_gets=args.allow_official_readiness_gets,
            confirm_one_shot=args.confirm_one_shot,
            confirm_two_deepseek_tool_proposals=(
                args.confirm_two_deepseek_tool_proposals
            ),
            confirm_disposable_sandbox=args.confirm_disposable_sandbox,
            user_authorization_sha256=(
                args.confirm_user_authorization_sha256 or ""
            ),
            combined_boundary_sha256=(
                args.confirm_combined_boundary_sha256 or ""
            ),
            production=True,
        )
        report = run_production_acceptance(confirmation=confirmation)
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason_code": getattr(exc, "reason_code", type(exc).__name__),
                    "do_not_rerun_if_sentry_exists": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_digest": report["report_digest"],
                "run_id": report["agent"]["run_id"],
                "run_status": report["agent"]["status"],
                "model_sends": report["agent"]["model_sends"],
                "replay_model_sends": report["agent"]["replay_model_sends"],
                "report_path": str(PRODUCTION_OUTPUT_ROOT / RESULT_NAME),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
