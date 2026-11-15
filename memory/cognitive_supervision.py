from __future__ import annotations

import hashlib
import os
import queue
import signal
import stat
import subprocess
import threading
import time
from dataclasses import InitVar, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from memory.cognitive_command_policy import ResolvedCommand
from memory.cognitive_orchestration import contains_secret_material
from memory.cognitive_path_policy import resolve_project_path
from memory.db_path_policy import is_reparse_point
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    make_identity,
    normalize_timestamp,
    utc_now,
    validate_identity,
    validate_sha256,
)


SUPERVISION_PROTOCOL_VERSION = 2
DEFAULT_STALL_CHECK_MS = 20 * 60 * 1000
SUPERVISION_EVENT_TYPES = (
    "heartbeat",
    "checkpoint",
    "suspected_stall",
    "stall_cleared",
    "budget_warning",
    "budget_exhausted",
    "overreach_blocked",
    "authorization_expired",
    "provider_failure",
    "process_terminated",
    "retry_started",
    "recovery_started",
    "recovery_completed",
    "cancellation_requested",
)

_PROCESS_RESULT_FACTORY_SEAL = object()
_SUPERVISION_EVENT_FACTORY_SEAL = object()
_STREAM_QUEUE_MAX_CHUNKS = 64
_STREAM_DRAIN_GRACE_SECONDS = 2.0


class CognitiveSupervisionError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CognitiveSupervisionError(reason_code)


def _process_metrics(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cpu_seconds": None,
        "memory_bytes": None,
        "metrics_available": False,
    }
    if os.name != "nt" or pid <= 0:
        return result
    try:
        import ctypes
        from ctypes import wintypes

        process_query = 0x1000
        process_vm_read = 0x0010
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        handle = kernel32.OpenProcess(process_query | process_vm_read, False, pid)
        if not handle:
            return result
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            cpu_seconds = None
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                kernel_ticks = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
                user_ticks = (user.dwHighDateTime << 32) | user.dwLowDateTime
                cpu_seconds = (kernel_ticks + user_ticks) / 10_000_000
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            memory_bytes = None
            if psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            ):
                memory_bytes = int(counters.WorkingSetSize)
            return {
                "cpu_seconds": cpu_seconds,
                "memory_bytes": memory_bytes,
                "metrics_available": cpu_seconds is not None or memory_bytes is not None,
            }
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return result


def _create_windows_job(process: subprocess.Popen[bytes]) -> int | None:
    """Bind the process to a kill-on-close Job Object before it may fan out."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x00002000

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
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
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
            _fail("owned_process_job_unavailable")
        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            job_object_limit_kill_on_job_close
        )
        if not kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            kernel32.CloseHandle(job)
            _fail("owned_process_job_unavailable")
        process_handle = wintypes.HANDLE(int(getattr(process, "_handle", 0)))
        if not process_handle or not kernel32.AssignProcessToJobObject(job, process_handle):
            kernel32.CloseHandle(job)
            _fail("owned_process_job_assignment_failed")
        return int(job)
    except CognitiveSupervisionError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _fail("owned_process_job_unavailable")


def _windows_job_metrics(job_handle: int | None) -> dict[str, Any]:
    unavailable = {"active_processes": None, "cpu_seconds": None}
    if os.name != "nt" or job_handle is None:
        return unavailable
    try:
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
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        information = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            return unavailable
        return {
            "active_processes": int(information.ActiveProcesses),
            "cpu_seconds": (
                int(information.TotalUserTime) + int(information.TotalKernelTime)
            )
            / 10_000_000,
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return unavailable


def _windows_job_active_processes(job_handle: int | None) -> int | None:
    value = _windows_job_metrics(job_handle).get("active_processes")
    return value if isinstance(value, int) else None


def _close_windows_job(job_handle: int | None, *, terminate: bool) -> bool:
    if os.name != "nt" or job_handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        forced = False
        if terminate:
            forced = bool(
                kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)
            )
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
        return forced
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _posix_group_alive(process_group_id: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_owned_process(
    process: subprocess.Popen[bytes],
    grace_ms: int,
    *,
    windows_job_handle: int | None,
) -> dict[str, Any]:
    graceful = False
    forced = False
    if os.name == "nt":
        try:
            process.terminate()
            process.wait(timeout=grace_ms / 1000)
            graceful = True
        except (OSError, subprocess.TimeoutExpired):
            pass
        # Always terminate the Job Object, even when the parent already exited:
        # descendants remain members of the job and cannot escape this cleanup.
        forced = _close_windows_job(windows_job_handle, terminate=True) or forced
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=grace_ms / 1000)
            graceful = not _posix_group_alive(process.pid)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _posix_group_alive(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                forced = True
            except OSError:
                pass
    try:
        process.wait(timeout=max(2.0, grace_ms / 1000))
    except subprocess.TimeoutExpired:
        _fail("owned_process_cleanup_failed")
    residual = (
        False
        if os.name == "nt"
        else _posix_group_alive(process.pid)
    )
    if residual:
        _fail("owned_process_cleanup_failed")
    return {
        "graceful_termination": graceful,
        "forced_termination": forced,
        "residual_process": residual or process.poll() is None,
    }


def _stream_reader(
    stream: Any,
    channel: str,
    events: queue.Queue[tuple[str, bytes]],
    stop_requested: threading.Event,
) -> None:
    def deliver(item: tuple[str, bytes]) -> bool:
        while not stop_requested.is_set():
            try:
                events.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    try:
        while True:
            chunk = stream.read(16 * 1024)
            if not chunk:
                break
            if not deliver((channel, bytes(chunk))):
                break
    finally:
        try:
            stream.close()
        except OSError:
            pass
        deliver((channel, b""))


@dataclass(frozen=True)
class ProcessRunResult:
    pid: int
    command_digest: str
    started_at: str
    finished_at: str
    duration_ms: int
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    artifact_references: tuple[str, ...]
    reason_codes: tuple[str, ...]
    suspected_stall: bool
    termination: Mapping[str, Any]
    observations: tuple[Mapping[str, Any], ...]
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _PROCESS_RESULT_FACTORY_SEAL:
            _fail("process_run_result_factory_required")
        if self.pid <= 0 or self.duration_ms < 0:
            _fail("invalid_process_run_result")
        if len(self.stdout_sha256) != 64 or len(self.stderr_sha256) != 64:
            _fail("invalid_process_run_digest")
        object.__setattr__(
            self,
            "artifact_references",
            tuple(sorted(set(self.artifact_references))),
        )
        reasons = tuple(sorted(set(self.reason_codes)))
        if not reasons:
            _fail("process_run_reason_required")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "termination", MappingProxyType(dict(self.termination)))
        object.__setattr__(
            self,
            "observations",
            tuple(MappingProxyType(dict(item)) for item in self.observations),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "command_digest": self.command_digest,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
            "artifact_references": list(self.artifact_references),
            "reason_codes": list(self.reason_codes),
            "suspected_stall": self.suspected_stall,
            "termination": dict(self.termination),
            "observations": [dict(item) for item in self.observations],
        }


class ProcessSupervisor:
    def __init__(
        self,
        *,
        artifact_root: str | os.PathLike[str],
        stall_check_ms: int = DEFAULT_STALL_CHECK_MS,
        poll_ms: int = 50,
        termination_grace_ms: int = 2_000,
        artifact_hard_limit_bytes: int = 8 * 1024 * 1024,
        trusted_clock: Callable[[], str] | None = None,
    ) -> None:
        for value, minimum, maximum, code in (
            (stall_check_ms, 10, 86_400_000, "invalid_stall_check_ms"),
            (poll_ms, 5, 1_000, "invalid_process_poll_ms"),
            (termination_grace_ms, 10, 30_000, "invalid_termination_grace_ms"),
            (
                artifact_hard_limit_bytes,
                1024,
                104_857_600,
                "invalid_process_artifact_limit",
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                _fail(code)
        requested_root = Path(os.path.abspath(str(artifact_root)))
        requested_root.mkdir(parents=True, exist_ok=True)
        try:
            if requested_root.is_symlink() or is_reparse_point(requested_root):
                _fail("process_artifact_root_unavailable")
            self.artifact_root = requested_root.resolve(strict=True)
            root_stat = requested_root.stat(follow_symlinks=False)
        except (OSError, RuntimeError, ValueError):
            _fail("process_artifact_root_unavailable")
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or self.artifact_root.is_symlink()
            or is_reparse_point(self.artifact_root)
            or root_stat.st_dev <= 0
            or root_stat.st_ino <= 0
        ):
            _fail("process_artifact_root_unavailable")
        self._artifact_root_requested = requested_root
        self._artifact_root_identity = (
            int(root_stat.st_dev),
            int(root_stat.st_ino),
            int(root_stat.st_mode),
            int(root_stat.st_nlink),
        )
        self.stall_check_ms = stall_check_ms
        self.poll_ms = poll_ms
        self.termination_grace_ms = termination_grace_ms
        self.artifact_hard_limit_bytes = artifact_hard_limit_bytes
        self._trusted_clock = trusted_clock or utc_now

    def _assert_artifact_root(self) -> None:
        try:
            if (
                self._artifact_root_requested.is_symlink()
                or is_reparse_point(self._artifact_root_requested)
            ):
                _fail("process_artifact_root_changed")
            resolved = self._artifact_root_requested.resolve(strict=True)
            value = self._artifact_root_requested.stat(follow_symlinks=False)
        except (OSError, RuntimeError, ValueError):
            _fail("process_artifact_root_unavailable")
        identity = (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_mode),
            int(value.st_nlink),
        )
        if (
            resolved != self.artifact_root
            or not stat.S_ISDIR(value.st_mode)
            or resolved.is_symlink()
            or is_reparse_point(resolved)
            or identity != self._artifact_root_identity
        ):
            _fail("process_artifact_root_changed")

    def _write_artifact(self, name: str, value: bytes) -> Path:
        self._assert_artifact_root()
        try:
            from memory.cognitive_local_broker import (
                _secured_write_file,
                _verify_secured_artifact_file,
            )

            binding = resolve_project_path(
                project_root=self.artifact_root,
                relative_path=name,
                access_mode="create_file",
            )
            if tuple(binding.root_identity) != self._artifact_root_identity:
                _fail("process_artifact_root_changed")
            with _secured_write_file(binding, create=True, artifact=True) as handle:
                handle.write(value)
                _verify_secured_artifact_file(
                    handle,
                    binding=binding,
                    expected_sha256=hashlib.sha256(value).hexdigest(),
                    expected_size=len(value),
                    create=True,
                )
        except CognitiveSupervisionError:
            raise
        except Exception:
            _fail("process_artifact_write_failed")
        return self.artifact_root / name

    def run(
        self,
        command: ResolvedCommand,
        *,
        timeout_ms: int,
        output_limit_bytes: int,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> ProcessRunResult:
        if not isinstance(command, ResolvedCommand):
            _fail("invalid_supervised_command")
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 86_400_000
            or isinstance(output_limit_bytes, bool)
            or not isinstance(output_limit_bytes, int)
            or not 1 <= output_limit_bytes <= 104_857_600
        ):
            _fail("invalid_supervision_budget")
        if cancellation_requested is not None and not callable(cancellation_requested):
            _fail("invalid_process_cancellation_check")
        self._assert_artifact_root()
        effective_artifact_limit = min(
            self.artifact_hard_limit_bytes,
            max(output_limit_bytes, 1024),
        )
        started_at = normalize_timestamp(self._trusted_clock())
        started_monotonic = time.monotonic()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        windows_job_handle: int | None = None
        try:
            process = subprocess.Popen(
                list(command.argv),
                cwd=command.working_directory,
                env=dict(command.environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=(os.name != "nt"),
            )
        except OSError:
            _fail("tool_process_start_failed")
        try:
            windows_job_handle = _create_windows_job(process)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
        if process.stdout is None or process.stderr is None:
            _terminate_owned_process(
                process,
                self.termination_grace_ms,
                windows_job_handle=windows_job_handle,
            )
            _fail("tool_process_pipe_unavailable")

        event_queue: queue.Queue[tuple[str, bytes]] = queue.Queue(
            maxsize=_STREAM_QUEUE_MAX_CHUNKS
        )
        reader_stop = threading.Event()
        readers = (
            threading.Thread(
                target=_stream_reader,
                args=(process.stdout, "stdout", event_queue, reader_stop),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_reader,
                args=(process.stderr, "stderr", event_queue, reader_stop),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        digests = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        byte_counts = {"stdout": 0, "stderr": 0}
        closed: set[str] = set()
        observations: list[dict[str, Any]] = []
        last_progress_total = 0
        last_cpu_seconds = 0.0
        next_stall_check = started_monotonic + self.stall_check_ms / 1000
        truncated = False
        suspected_stall = False
        timeout = False
        cancelled = False
        cancellation_check_failed = False
        artifact_overflow = False
        termination_requested = False
        stream_drain_deadline: float | None = None
        termination = {
            "graceful_termination": False,
            "forced_termination": False,
            "residual_process": False,
        }
        while process.poll() is None or len(closed) < 2:
            try:
                channel, chunk = event_queue.get(timeout=self.poll_ms / 1000)
                if not chunk:
                    closed.add(channel)
                else:
                    byte_counts[channel] += len(chunk)
                    digests[channel].update(chunk)
                    buffered_total = len(buffers["stdout"]) + len(buffers["stderr"])
                    if buffered_total < effective_artifact_limit:
                        remaining = effective_artifact_limit - buffered_total
                        buffers[channel].extend(chunk[:remaining])
                    if byte_counts["stdout"] + byte_counts["stderr"] > output_limit_bytes:
                        truncated = True
                    if byte_counts["stdout"] + byte_counts["stderr"] > effective_artifact_limit:
                        artifact_overflow = True
            except queue.Empty:
                pass
            now = time.monotonic()
            total = byte_counts["stdout"] + byte_counts["stderr"]
            if cancellation_requested is not None and not cancelled:
                try:
                    cancelled = bool(cancellation_requested())
                except Exception:
                    cancellation_check_failed = True
                    cancelled = True
            if now >= next_stall_check and process.poll() is None:
                metrics = _process_metrics(process.pid)
                job_metrics = _windows_job_metrics(windows_job_handle)
                tree_cpu_seconds = job_metrics.get("cpu_seconds")
                cpu_seconds = (
                    tree_cpu_seconds
                    if isinstance(tree_cpu_seconds, (int, float))
                    else metrics.get("cpu_seconds")
                )
                cpu_progress = (
                    isinstance(cpu_seconds, (int, float))
                    and float(cpu_seconds) > last_cpu_seconds + 0.01
                )
                progress = total > last_progress_total or cpu_progress
                observations.append(
                    {
                        "pid": process.pid,
                        "elapsed_ms": int((now - started_monotonic) * 1000),
                        "stdout_bytes": byte_counts["stdout"],
                        "stderr_bytes": byte_counts["stderr"],
                        "output_growth": progress,
                        "input_wait": "stdin_disabled",
                        # Every executable command admitted by the v10-D
                        # command registry is a code-owned local Git template.
                        # Those templates carry neither network nor SQLite
                        # capability, so these checks are structurally not
                        # applicable instead of silently unobserved.
                        "network_wait": "not_applicable_fixed_local_template",
                        "sqlite_lock": "not_applicable_non_database_template",
                        "child_processes": (
                            job_metrics.get("active_processes")
                            if os.name == "nt"
                            else "owned_process_group"
                        ),
                        "process_tree_cpu_seconds": tree_cpu_seconds,
                        **metrics,
                    }
                )
                if not progress:
                    suspected_stall = True
                else:
                    last_progress_total = total
                    if isinstance(cpu_seconds, (int, float)):
                        last_cpu_seconds = float(cpu_seconds)
                    next_stall_check = now + self.stall_check_ms / 1000
            if now - started_monotonic >= timeout_ms / 1000:
                timeout = True
            if (
                (suspected_stall or timeout or artifact_overflow or cancelled)
                and not termination_requested
            ):
                termination = _terminate_owned_process(
                    process,
                    self.termination_grace_ms,
                    windows_job_handle=windows_job_handle,
                )
                windows_job_handle = None
                termination_requested = True
                stream_drain_deadline = (
                    time.monotonic() + _STREAM_DRAIN_GRACE_SECONDS
                )
            if (
                termination_requested
                and len(closed) < 2
                and stream_drain_deadline is not None
                and time.monotonic() >= stream_drain_deadline
            ):
                reader_stop.set()
                break
        reader_stop.set()
        for reader in readers:
            reader.join(timeout=2.0)
        if any(reader.is_alive() for reader in readers):
            if windows_job_handle is not None:
                _close_windows_job(windows_job_handle, terminate=True)
            _fail("owned_process_stream_cleanup_failed")
        while True:
            try:
                channel, chunk = event_queue.get_nowait()
            except queue.Empty:
                break
            if chunk:
                byte_counts[channel] += len(chunk)
                digests[channel].update(chunk)
                buffered_total = len(buffers["stdout"]) + len(buffers["stderr"])
                if buffered_total < effective_artifact_limit:
                    remaining = effective_artifact_limit - buffered_total
                    buffers[channel].extend(chunk[:remaining])
        if byte_counts["stdout"] + byte_counts["stderr"] > output_limit_bytes:
            truncated = True
        if (
            byte_counts["stdout"] + byte_counts["stderr"]
            > effective_artifact_limit
        ):
            artifact_overflow = True

        # A command is not allowed to leave a detached descendant behind even
        # when its direct parent exits successfully. Closing the Job Object (or
        # killing the POSIX process group) makes that guarantee explicit.
        lingering_children = False
        if windows_job_handle is not None:
            active = _windows_job_active_processes(windows_job_handle)
            if active is None:
                _close_windows_job(windows_job_handle, terminate=True)
                _fail("owned_process_job_observation_failed")
            lingering_children = active > 0
            _close_windows_job(windows_job_handle, terminate=lingering_children)
            windows_job_handle = None
        elif os.name != "nt" and _posix_group_alive(process.pid):
            lingering_children = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            if _posix_group_alive(process.pid):
                _fail("owned_process_cleanup_failed")

        finished_at = normalize_timestamp(self._trusted_clock())
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        reason_codes: list[str] = []
        if suspected_stall:
            reason_codes.append("suspected_stall")
        if timeout:
            reason_codes.append("tool_execution_timeout_exceeded")
        if cancelled:
            reason_codes.append("tool_execution_cancelled")
        if cancellation_check_failed:
            reason_codes.append("tool_cancellation_check_failed")
        if lingering_children:
            reason_codes.append("owned_child_process_terminated")
        if artifact_overflow:
            reason_codes.append("tool_output_artifact_limit_exceeded")
        if truncated:
            reason_codes.append("tool_output_limit_exceeded")
        try:
            stdout_text = bytes(buffers["stdout"]).decode("utf-8", errors="strict")
            stderr_text = bytes(buffers["stderr"]).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            stdout_text = ""
            stderr_text = ""
            reason_codes.append("tool_output_encoding_invalid")
        if stdout_text and contains_secret_material(stdout_text):
            stdout_text = ""
            reason_codes.append("tool_output_secret_detected")
        if stderr_text and contains_secret_material(stderr_text):
            stderr_text = ""
            reason_codes.append("tool_output_secret_detected")
        if "tool_output_secret_detected" in reason_codes:
            buffers = {
                "stdout": bytearray(b"[REDACTED: secret material detected]\n"),
                "stderr": bytearray(b"[REDACTED: secret material detected]\n"),
            }
        if process.returncode == 0 and not reason_codes:
            reason_codes.append("process_succeeded")
        elif process.returncode not in {0, None}:
            reason_codes.append("process_exit_nonzero")
        if not reason_codes:
            reason_codes.append("process_failed")

        artifact_references: list[str] = []
        if byte_counts["stdout"] or byte_counts["stderr"]:
            artifact_id = make_identity(
                "processartifact",
                {
                    "command_digest": command.command_digest,
                    "pid": process.pid,
                    "started_at": started_at,
                },
            )
            artifact_storage_key = canonical_digest(
                {"process_artifact_id": artifact_id}
            )
            for channel in ("stdout", "stderr"):
                name = f"{artifact_storage_key}.{channel}.log"
                target = self._write_artifact(
                    name,
                    bytes(buffers[channel]),
                )
                artifact_references.append(str(target))
        return ProcessRunResult(
            pid=process.pid,
            command_digest=command.command_digest,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=process.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            stdout_sha256=digests["stdout"].hexdigest(),
            stderr_sha256=digests["stderr"].hexdigest(),
            stdout_bytes=byte_counts["stdout"],
            stderr_bytes=byte_counts["stderr"],
            truncated=truncated,
            artifact_references=tuple(artifact_references),
            reason_codes=tuple(reason_codes),
            suspected_stall=suspected_stall,
            termination=termination,
            observations=tuple(observations),
            _factory_seal=_PROCESS_RESULT_FACTORY_SEAL,
        )


@dataclass(frozen=True)
class SupervisionEvent:
    supervision_event_id: str
    run_id: str
    sequence: int
    event_type: str
    severity: str
    plan_node_id: str | None
    tool_execution_id: str | None
    observed_at: str
    detail: str
    evidence_digest: str
    action_taken: tuple[str, ...]
    event_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _SUPERVISION_EVENT_FACTORY_SEAL:
            _fail("supervision_event_factory_required")
        if self.sequence < 1 or self.event_type not in SUPERVISION_EVENT_TYPES:
            _fail("invalid_supervision_event")
        if self.severity not in {"info", "warning", "error", "critical"}:
            _fail("invalid_supervision_event_severity")
        try:
            validate_identity(
                self.supervision_event_id,
                "invalid_supervision_event_id",
            )
            validate_identity(self.run_id, "invalid_supervision_run_id")
            if self.plan_node_id is not None:
                validate_identity(
                    self.plan_node_id,
                    "invalid_supervision_plan_node_id",
                )
            if self.tool_execution_id is not None:
                validate_identity(
                    self.tool_execution_id,
                    "invalid_supervision_tool_execution_id",
                )
            validate_sha256(
                self.evidence_digest,
                "invalid_supervision_evidence_digest",
            )
        except OperationalStateError as error:
            _fail(str(error))
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 2048:
            _fail("invalid_supervision_event_detail")
        if (
            isinstance(self.action_taken, (str, bytes))
            or not isinstance(self.action_taken, tuple)
            or len(self.action_taken) > 32
            or len(self.action_taken) != len(set(self.action_taken))
            or any(
                not isinstance(item, str) or not item or len(item) > 191
                for item in self.action_taken
            )
        ):
            _fail("invalid_supervision_event_action")
        expected_id = make_identity(
            "supervision",
            {
                "run_id": self.run_id,
                "sequence": self.sequence,
                "event_type": self.event_type,
                "evidence_digest": self.evidence_digest,
            },
        )
        if self.supervision_event_id != expected_id:
            _fail("supervision_event_identity_mismatch")
        expected = canonical_digest(self.identity_payload())
        if self.event_digest and self.event_digest != expected:
            _fail("supervision_event_digest_mismatch")
        object.__setattr__(self, "event_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "supervision_protocol_version": SUPERVISION_PROTOCOL_VERSION,
            "supervision_event_id": self.supervision_event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "severity": self.severity,
            "plan_node_id": self.plan_node_id,
            "tool_execution_id": self.tool_execution_id,
            "observed_at": self.observed_at,
            "detail": self.detail,
            "evidence_digest": self.evidence_digest,
            "action_taken": list(self.action_taken),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_digest": self.event_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SupervisionEvent":
        if not isinstance(value, Mapping):
            _fail("invalid_supervision_event_record")
        payload = dict(value)
        expected = {
            "supervision_protocol_version",
            "supervision_event_id",
            "run_id",
            "sequence",
            "event_type",
            "severity",
            "plan_node_id",
            "tool_execution_id",
            "observed_at",
            "detail",
            "evidence_digest",
            "action_taken",
            "event_digest",
        }
        if set(payload) != expected or payload.pop(
            "supervision_protocol_version"
        ) != SUPERVISION_PROTOCOL_VERSION:
            _fail("invalid_supervision_event_record")
        actions = payload.get("action_taken")
        if isinstance(actions, (str, bytes)) or not isinstance(actions, list):
            _fail("invalid_supervision_event_record")
        payload["action_taken"] = tuple(actions)
        return cls(**payload, _factory_seal=_SUPERVISION_EVENT_FACTORY_SEAL)


def create_supervision_event(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    severity: str,
    observed_at: str,
    detail: str,
    evidence: Mapping[str, Any],
    action_taken: tuple[str, ...] = (),
    plan_node_id: str | None = None,
    tool_execution_id: str | None = None,
) -> SupervisionEvent:
    evidence_digest = canonical_digest(evidence)
    identity = {
        "run_id": run_id,
        "sequence": sequence,
        "event_type": event_type,
        "evidence_digest": evidence_digest,
    }
    return SupervisionEvent(
        supervision_event_id=make_identity("supervision", identity),
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        severity=severity,
        plan_node_id=plan_node_id,
        tool_execution_id=tool_execution_id,
        observed_at=observed_at,
        detail=detail,
        evidence_digest=evidence_digest,
        action_taken=tuple(action_taken),
        _factory_seal=_SUPERVISION_EVENT_FACTORY_SEAL,
    )


__all__ = [
    "CognitiveSupervisionError",
    "DEFAULT_STALL_CHECK_MS",
    "ProcessRunResult",
    "ProcessSupervisor",
    "SUPERVISION_EVENT_TYPES",
    "SupervisionEvent",
    "create_supervision_event",
]
