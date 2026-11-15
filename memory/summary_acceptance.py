"""Controlled, single-candidate acceptance into the versioned memory sidecar."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from memory.change_detector import detect_changes
from memory.db_path_policy import ControlledSummaryPathError, validate_summary_acceptance_paths
from memory.memory_sidecar import PRODUCTION_MEMORY_BACKUPS, PRODUCTION_MEMORY_SIDECAR, sidecar_initialized
from memory.summary_candidate import ALLOWED_REASONS, generate_summary_candidate
from memory.summary_repository import _append_summary_record, current_summary, legacy_summary


ACCEPTANCE_PLAN_SCHEMA_VERSION = 1


def prepare_acceptance(
    source_db: str | Path, sidecar_path: str | Path, conversation_id: str, memory_space: str | None = None,
    *, include_candidate_text: bool = False,
) -> dict[str, Any]:
    """Build an immutable, text-free acceptance plan without opening a writable DB."""
    try:
        source_db, sidecar_path, production = validate_summary_acceptance_paths(
            source_db,
            sidecar_path,
            production_memory_path=PRODUCTION_MEMORY_SIDECAR,
        )
    except ControlledSummaryPathError as exc:
        return {**_result("refused", "invalid_target", executed=False, next_action="manual_review"), "error": str(exc)}
    target_kind = "production_memory_sidecar" if production else "test_memory_sidecar"
    detected = detect_changes(
        source_db,
        memory_space=memory_space,
        conversation_id=conversation_id,
        memory_sidecar_path=sidecar_path,
    )
    item = next((entry for entry in detected["items"] if entry["conversation_id"] == conversation_id and entry["status"] in ALLOWED_REASONS), None)
    if not item:
        return _result("no_eligible_candidate", target_kind, executed=False, next_action="manual_review")
    candidate_payload = generate_summary_candidate(source_db, conversation_id, item["memory_space"], item["status"])
    if candidate_payload.get("status") != "candidate_ready":
        return _result(str(candidate_payload.get("status") or "manual_review"), target_kind, executed=False, next_action="manual_review")
    candidate = candidate_payload["candidate"]
    base = current_summary(sidecar_path, source_db, conversation_id, item["memory_space"])
    plan = _plan(candidate, target_kind, base)
    initialized = sidecar_initialized(sidecar_path)
    return {
        **_result("prepared", target_kind, executed=False, next_action="confirm_acceptance" if initialized else "initialize_memory_sidecar"),
        "executable": initialized,
        "target_status": "ready" if initialized else "sidecar_not_initialized",
        "acceptance_plan": plan,
        "candidate": dict(candidate) if include_candidate_text else _public_candidate(candidate),
    }


def execute_acceptance(
    source_db: str | Path, sidecar_path: str | Path, conversation_id: str, *,
    confirm_candidate_id: str | None, confirm_plan_id: str | None,
    real_readonly: bool = False, allow_production_memory_sidecar: bool = False,
    confirm_production_summary_write: bool = False, backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild and bind all inputs under source/sidecar concurrency guards."""
    run_id = uuid.uuid4().hex
    try:
        source_db, sidecar_path, production = validate_summary_acceptance_paths(
            source_db,
            sidecar_path,
            production_memory_path=PRODUCTION_MEMORY_SIDECAR,
        )
    except ControlledSummaryPathError as exc:
        return {**_result("refused", "invalid_target", run_id=run_id, executed=False, next_action="manual_review"), "error": str(exc)}
    target_kind = "production_memory_sidecar" if production else "test_memory_sidecar"
    base_result = _result("refused", target_kind, run_id=run_id, executed=False, next_action="manual_review")
    if not confirm_candidate_id or not confirm_plan_id:
        return {**base_result, "error": "candidate_id and plan_id confirmations are required"}
    if target_kind == "production_memory_sidecar" and not (real_readonly and allow_production_memory_sidecar and confirm_production_summary_write):
        return {**base_result, "error": "production summary write requires all confirmation flags"}
    if not sidecar_initialized(sidecar_path):
        return {**_result("sidecar_not_initialized", target_kind, run_id=run_id, executed=False, next_action="initialize_memory_sidecar"), "target_status": "sidecar_not_initialized"}

    backup_root = Path(backup_dir).resolve() if backup_dir else (PRODUCTION_MEMORY_BACKUPS if target_kind == "production_memory_sidecar" else Path(sidecar_path).resolve().parent / "backups")
    try:
        with _source_write_guard(source_db), _sidecar_protocol_guard(sidecar_path):
            locked = _locked_revalidation(
                source_db, sidecar_path, conversation_id, target_kind,
                confirm_candidate_id, confirm_plan_id,
            )
            if locked["status"] != "ready":
                return {**base_result, **locked}
            current, base = locked["candidate"], locked["base"]
            backup = _backup_sidecar(sidecar_path, backup_root, confirm_plan_id, run_id)
            committed = _commit_revalidated(
                source_db, sidecar_path, conversation_id, target_kind,
                confirm_candidate_id, confirm_plan_id, current, base, backup,
                run_id,
            )
            if committed["status"] != "accepted":
                if committed["status"] in {"stale_plan", "source_busy", "sidecar_busy"}:
                    backup.unlink(missing_ok=True)
                    committed["backup_created"] = False
                    committed["backup_name"] = None
                    committed["backup_sha256"] = None
                return committed
            return committed
    except _SourceBusy as exc:
        return {**_result("source_busy", target_kind, run_id=run_id, executed=False, acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="prepare_again"), "error": str(exc)}
    except _SidecarBusy as exc:
        return {**_result("sidecar_busy", target_kind, run_id=run_id, executed=False, acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="manual_review"), "error": str(exc)}
    except sqlite3.OperationalError as exc:
        status = "sidecar_busy" if _is_busy(exc) else "failed"
        return {**_result(status, target_kind, run_id=run_id, executed=False, acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="manual_review"), "error": _safe_error(exc)}
    except OSError as exc:
        return {**_result("failed", target_kind, run_id=run_id, executed=False, acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="manual_review"), "error": _safe_error(exc)}


def _locked_revalidation(
    source_db: Path,
    sidecar_path: Path,
    conversation_id: str,
    target_kind: str,
    confirm_candidate_id: str,
    confirm_plan_id: str,
) -> dict[str, Any]:
    conn = sqlite3.connect(sidecar_path, timeout=0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _SidecarBusy("Memory sidecar is busy") from exc
            raise
        accepted = conn.execute(
            "SELECT acceptance_plan_id FROM summary_versions WHERE candidate_id = ?",
            (confirm_candidate_id,),
        ).fetchone()
        if accepted:
            if str(accepted[0]) != confirm_plan_id:
                return {"status": "stale_plan", "next_action": "prepare_again"}
            return {
                "status": "already_accepted", "executed": False,
                "acceptance_plan_id": confirm_plan_id, "candidate_id": confirm_candidate_id,
                "next_action": "none",
            }
        candidate = _candidate_for_execute(source_db, conversation_id, sidecar_path)
        if candidate is None or candidate["candidate_id"] != confirm_candidate_id:
            return {"status": "stale_plan", "next_action": "prepare_again"}
        base = _current_base_in_transaction(conn, source_db, conversation_id, candidate["memory_space"])
        plan = _plan(candidate, target_kind, base)
        if plan["acceptance_plan_id"] != confirm_plan_id:
            return {"status": "stale_plan", "next_action": "prepare_again"}
        verified = _candidate_for_execute(source_db, conversation_id, sidecar_path)
        if verified is None or _plan(verified, target_kind, base)["acceptance_plan_id"] != confirm_plan_id:
            return {"status": "stale_plan", "next_action": "prepare_again"}
        return {"status": "ready", "candidate": verified, "base": base}
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _commit_revalidated(
    source_db: Path,
    sidecar_path: Path,
    conversation_id: str,
    target_kind: str,
    confirm_candidate_id: str,
    confirm_plan_id: str,
    prior_candidate: dict[str, Any],
    prior_base: dict[str, Any] | None,
    backup: Path,
    run_id: str,
) -> dict[str, Any]:
    conn = sqlite3.connect(sidecar_path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM summary_versions WHERE candidate_id = ?", (confirm_candidate_id,)).fetchone():
            conn.rollback()
            return {**_result("stale_plan", target_kind, run_id=run_id, executed=False, backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="prepare_again")}
        candidate = _candidate_for_execute(source_db, conversation_id, sidecar_path)
        if candidate is None:
            conn.rollback()
            return {**_result("stale_plan", target_kind, run_id=run_id, executed=False, backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="prepare_again")}
        base = _current_base_in_transaction(conn, source_db, conversation_id, candidate["memory_space"])
        if (
            _plan(candidate, target_kind, base)["acceptance_plan_id"] != confirm_plan_id
            or candidate != prior_candidate
            or _base_identity(base) != _base_identity(prior_base)
        ):
            conn.rollback()
            return {**_result("stale_plan", target_kind, run_id=run_id, executed=False, backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="prepare_again")}
        now = _utc()
        record = {
            "summary_id": uuid.uuid4().hex, "conversation_id": candidate["conversation_id"], "memory_space": candidate["memory_space"],
            "version": _next_version_in_transaction(conn, candidate["conversation_id"]), "candidate_id": candidate["candidate_id"],
            "candidate_schema_version": candidate["candidate_schema_version"], "source_snapshot_hash": candidate["source_snapshot_hash"],
            "source_message_count": candidate["source_message_count"], "source_last_message_at": candidate["source_last_message_at"],
            "source_latest_message_id": candidate["source_latest_message_id"], "generator_identity": candidate["generator_identity"],
            "generator_config_hash": candidate["generator_config_hash"], "summary_text": candidate["candidate_text"],
            "summary_text_hash": candidate["candidate_text_hash"], "reason": candidate["reason"],
            "base_summary_id": base.get("summary_id") if base else None,
            "base_summary_text_hash": _base_identity(base)[1],
            "acceptance_plan_id": confirm_plan_id, "acceptance_run_id": run_id, "accepted_at": now, "created_at": now,
        }
        _append_summary_record(conn, record)
        conn.commit()
        return _result("accepted", target_kind, run_id=run_id, executed=True, transaction_committed=True, backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=candidate["candidate_id"], summary_id=record["summary_id"], summary_version=record["version"], source_snapshot_hash=candidate["source_snapshot_hash"], candidate_text_hash=candidate["candidate_text_hash"], next_action="prepare_embedding_lifecycle")
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        status = "sidecar_busy" if _is_busy(exc) else "rolled_back"
        return {**_result(status, target_kind, run_id=run_id, executed=False, transaction_rolled_back=not _is_busy(exc), backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="manual_review"), "error": _safe_error(exc)}
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        return {**_result("rolled_back", target_kind, run_id=run_id, executed=False, transaction_rolled_back=True, backup_created=True, backup_name=backup.name, backup_sha256=_file_hash(backup), acceptance_plan_id=confirm_plan_id, candidate_id=confirm_candidate_id, next_action="manual_review"), "error": _safe_error(exc)}
    finally:
        conn.close()


def _current_base_in_transaction(
    conn: sqlite3.Connection,
    source_db: str | Path,
    conversation_id: str,
    memory_space: str,
) -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT * FROM summary_versions WHERE conversation_id = ? AND memory_space = ? ORDER BY version DESC LIMIT 1",
        (conversation_id, memory_space),
    )
    row = cursor.fetchone()
    if row:
        accepted = dict(zip([item[0] for item in cursor.description], row))
        return {"summary_source": "memory_sidecar", "summary_version": accepted["version"], **accepted}
    legacy = legacy_summary(source_db, conversation_id, memory_space)
    return {"summary_source": "legacy", "summary_version": None, **legacy} if legacy else None


def _next_version_in_transaction(conn: sqlite3.Connection, conversation_id: str) -> int:
    row = conn.execute(
        "SELECT version FROM summary_versions WHERE conversation_id = ? ORDER BY version DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return int(row[0]) + 1 if row else 1


def _base_identity(base: dict[str, Any] | None) -> tuple[Any, Any]:
    if not base:
        return None, None
    base_hash = base.get("summary_text_hash") if base.get("summary_source") == "memory_sidecar" else _hash(str(base.get("summary_text") or ""))
    return base.get("summary_id"), base_hash


def _candidate_for_execute(
    source_db: str | Path,
    conversation_id: str,
    sidecar_path: str | Path,
) -> dict[str, Any] | None:
    detected = detect_changes(source_db, conversation_id=conversation_id, memory_sidecar_path=sidecar_path)
    item = next((entry for entry in detected["items"] if entry["conversation_id"] == conversation_id and entry["status"] in ALLOWED_REASONS), None)
    if not item:
        return None
    payload = generate_summary_candidate(source_db, conversation_id, item["memory_space"], item["status"])
    return payload.get("candidate") if payload.get("status") == "candidate_ready" else None


class _SourceBusy(RuntimeError):
    pass


class _SidecarBusy(RuntimeError):
    pass


@contextmanager
def _source_write_guard(source_db: Path) -> Iterator[None]:
    """Hold the source read-only and deny concurrent writable opens on Windows."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateFileW(str(source_db), 0x80000000, 0x00000001, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise _SourceBusy("Summary source is busy")
        try:
            yield
        finally:
            kernel32.CloseHandle(handle)
        return

    import fcntl

    stream = source_db.open("rb")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _SourceBusy("Summary source is busy") from exc
        yield
    finally:
        stream.close()


@contextmanager
def _sidecar_protocol_guard(sidecar_path: Path) -> Iterator[None]:
    """Serialize every supported acceptance writer without leaving repository locks."""
    lock_identity = hashlib.sha256(str(sidecar_path).casefold().encode("utf-8")).hexdigest()
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, f"Local\\YunOSSummaryAcceptance-{lock_identity}")
        if not handle:
            raise OSError("Unable to create the summary acceptance mutex")
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result not in (0, 0x80):
            kernel32.CloseHandle(handle)
            raise _SidecarBusy("Memory sidecar is busy")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    lock_path = Path(tempfile.gettempdir()) / f"yunos-summary-{lock_identity}.lock"
    stream = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _SidecarBusy("Memory sidecar is busy") from exc
        yield
    finally:
        stream.close()


def _plan(candidate: dict[str, Any], target_kind: str, base: dict[str, Any] | None) -> dict[str, Any]:
    plan = {
        "acceptance_plan_schema_version": ACCEPTANCE_PLAN_SCHEMA_VERSION, "target_kind": target_kind,
        "candidate_id": candidate["candidate_id"], "candidate_schema_version": candidate["candidate_schema_version"],
        "conversation_id": candidate["conversation_id"], "memory_space": candidate["memory_space"], "reason": candidate["reason"],
        "source_snapshot_hash": candidate["source_snapshot_hash"], "source_message_count": candidate["source_message_count"],
        "source_last_message_at": candidate["source_last_message_at"], "source_latest_message_id": candidate["source_latest_message_id"],
        "generator_identity": candidate["generator_identity"], "generator_config_hash": candidate["generator_config_hash"],
        "candidate_text_hash": candidate["candidate_text_hash"], "base_summary_id": base.get("summary_id") if base else None,
        "base_summary_text_hash": base.get("summary_text_hash") if base and base.get("summary_source") == "memory_sidecar" else (_hash(str(base.get("summary_text") or "")) if base else None),
    }
    plan["acceptance_plan_id"] = _hash(json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return plan


def _backup_sidecar(source: str | Path, directory: Path, plan_id: str, run_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"ayunos_memory_before_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{plan_id[:2]}_{run_id}.db"
    target = directory / name
    read_conn = None
    write_conn = None
    try:
        read_conn = sqlite3.connect(Path(source).resolve(), timeout=0)
        write_conn = sqlite3.connect(target)
        read_conn.backup(write_conn)
    except Exception:
        if write_conn is not None:
            write_conn.close()
        if read_conn is not None:
            read_conn.close()
        target.unlink(missing_ok=True)
        raise
    write_conn.close()
    read_conn.close()
    return target


def _result(status: str, target_kind: str, **values: Any) -> dict[str, Any]:
    return {"run_id": values.pop("run_id", None), "acceptance_plan_id": values.pop("acceptance_plan_id", None), "candidate_id": values.pop("candidate_id", None), "conversation_id": values.pop("conversation_id", None), "target_kind": target_kind, "target_scope": target_kind, "status": status, "executed": values.pop("executed", False), "transaction_committed": values.pop("transaction_committed", False), "transaction_rolled_back": values.pop("transaction_rolled_back", False), "backup_created": values.pop("backup_created", False), "backup_name": values.pop("backup_name", None), "backup_sha256": values.pop("backup_sha256", None), "summary_id": values.pop("summary_id", None), "summary_version": values.pop("summary_version", None), "source_snapshot_hash": values.pop("source_snapshot_hash", None), "candidate_text_hash": values.pop("candidate_text_hash", None), "readonly_source": True, "started_at": None, "completed_at": _utc(), **values}


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if key != "candidate_text"}


def _hash(value: str) -> str: return hashlib.sha256(value.encode("utf-8")).hexdigest()
def _file_hash(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()
def _utc() -> str: return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
def _is_busy(exc: Exception) -> bool: return "busy" in str(exc).lower() or "locked" in str(exc).lower()
def _safe_error(exc: Exception) -> str: return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
