from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from memory.cognitive_execution_models import ToolProposal
from memory.cognitive_orchestration import contains_secret_material
from memory.cognitive_path_policy import (
    ResolvedProjectPath,
    resolve_project_path,
    revalidate_resolved_path,
)
from memory.db_path_policy import is_reparse_point
from memory.operational_state_models import (
    canonical_digest,
    canonical_json,
    make_identity,
)


LOCAL_BROKER_VERSION = 2
_MAX_TEXT_BYTES = 262_144
_MAX_BACKUP_RECORD_BYTES = 65_536
_MAX_SQLITE_CAPTURE_BYTES = 100 * 1024 * 1024


class CognitiveLocalBrokerError(ValueError):
    def __init__(self, reason_code: str, *, effect_may_have_occurred: bool = False):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.effect_may_have_occurred = effect_may_have_occurred


def _fail(reason_code: str, *, effect_may_have_occurred: bool = False) -> None:
    raise CognitiveLocalBrokerError(
        reason_code,
        effect_may_have_occurred=effect_may_have_occurred,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bounded(path: Path, maximum_bytes: int = _MAX_TEXT_BYTES) -> bytes:
    try:
        size = path.stat(follow_symlinks=False).st_size
        if size < 0 or size > maximum_bytes:
            _fail("tool_file_size_limit_exceeded")
        with path.open("rb") as handle:
            value = handle.read(maximum_bytes + 1)
    except CognitiveLocalBrokerError:
        raise
    except OSError:
        _fail("tool_native_read_failed")
    if len(value) > maximum_bytes or len(value) != size:
        _fail("tool_file_size_limit_exceeded")
    return value


def _sqlite_file_snapshot(path: Path, *, kind: str) -> dict[str, Any]:
    """Hash one SQLite file without trusting a path-only observation."""

    try:
        if path.is_symlink() or is_reparse_point(path):
            _fail("tool_database_sidecar_unsafe")
        before = path.stat(follow_symlinks=False)
    except CognitiveLocalBrokerError:
        raise
    except OSError:
        _fail("tool_database_snapshot_failed")
    if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
        _fail("tool_database_sidecar_unsafe")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError:
        _fail("tool_database_snapshot_failed")
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_mode),
        int(before.st_nlink),
        int(before.st_size),
        int(before.st_mtime_ns),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_mode),
        int(after.st_nlink),
        int(after.st_size),
        int(after.st_mtime_ns),
    )
    if before_identity != after_identity or total != before.st_size:
        _fail("tool_database_snapshot_changed")
    identity = {
        "kind": kind,
        "device": before_identity[0],
        "inode": before_identity[1],
        "mode": before_identity[2],
        "link_count": before_identity[3],
        "size": before_identity[4],
        "mtime_ns": before_identity[5],
        "sha256": digest.hexdigest(),
    }
    return {**identity, "identity_digest": canonical_digest(identity)}


def _sqlite_snapshot(path: Path) -> dict[str, Any]:
    database = _sqlite_file_snapshot(path, kind="database")
    sidecars: list[dict[str, Any]] = []
    absent_sidecars: list[str] = []
    for suffix in ("-wal", "-shm"):
        sidecar = path.parent / f"{path.name}{suffix}"
        try:
            exists = sidecar.exists()
            unsafe_absent = sidecar.is_symlink() or is_reparse_point(sidecar)
        except OSError:
            _fail("tool_database_snapshot_failed")
        if unsafe_absent:
            _fail("tool_database_sidecar_unsafe")
        if not exists:
            absent_sidecars.append(suffix)
            continue
        sidecars.append(_sqlite_file_snapshot(sidecar, kind=suffix[1:]))
    identity = {
        "database": database,
        "sidecars": sidecars,
        "absent_sidecars": absent_sidecars,
    }
    return {**identity, "snapshot_digest": canonical_digest(identity)}


def _public_sqlite_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    database = snapshot["database"]
    sidecars = snapshot["sidecars"]
    return {
        "database_identity_digest": database["identity_digest"],
        "sidecars": [
            {
                "kind": item["kind"],
                "size": item["size"],
                "sha256": item["sha256"],
                "identity_digest": item["identity_digest"],
            }
            for item in sidecars
        ],
        "absent_sidecars": list(snapshot["absent_sidecars"]),
        "snapshot_digest": snapshot["snapshot_digest"],
        "freshness": "stable_readonly_snapshot",
    }


def _sqlite_capture_identity(
    binding: ResolvedProjectPath,
    value: bytes,
    file_stat: os.stat_result,
    *,
    kind: str,
) -> dict[str, Any]:
    identity = {
        "kind": kind,
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
        "mode": int(file_stat.st_mode),
        "link_count": int(file_stat.st_nlink),
        "size": len(value),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "sha256": _sha256_bytes(value),
    }
    if (
        binding.target_size != len(value)
        or binding.target_sha256 != identity["sha256"]
    ):
        _fail("tool_database_snapshot_changed")
    return {**identity, "identity_digest": canonical_digest(identity)}


def _sqlite_snapshot_matches_operation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Permit only SQLite's own SHM reader-mark bytes to settle on first read."""

    if (
        before["database"] != after["database"]
        or before["absent_sidecars"] != after["absent_sidecars"]
    ):
        return False
    before_sidecars = {item["kind"]: item for item in before["sidecars"]}
    after_sidecars = {item["kind"]: item for item in after["sidecars"]}
    if set(before_sidecars) != set(after_sidecars):
        return False
    if before_sidecars.get("wal") != after_sidecars.get("wal"):
        return False
    before_shm = before_sidecars.get("shm")
    after_shm = after_sidecars.get("shm")
    if before_shm is None:
        return after_shm is None
    stable_identity_fields = {
        "kind",
        "device",
        "inode",
        "mode",
        "link_count",
        "size",
        "mtime_ns",
    }
    return all(
        before_shm[field] == after_shm[field]
        for field in stable_identity_fields
    )


def _decode_text(value: bytes) -> str:
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("tool_output_encoding_invalid")
    if contains_secret_material(text):
        _fail("tool_output_secret_detected")
    return text


def _write_exclusive(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _fail("tool_owned_artifact_collision")
    except OSError:
        _fail("tool_owned_artifact_write_failed")


def _write_race_observation_point(_binding: ResolvedProjectPath) -> None:
    """Deterministic no-op hook used only to inject path races in tests."""


def _artifact_write_race_observation_point(_binding: ResolvedProjectPath) -> None:
    """Separate deterministic hook for owned-artifact path race tests."""


def _read_race_observation_point(_binding: ResolvedProjectPath) -> None:
    """Deterministic no-op hook used only to inject read path races in tests."""


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
    )


def _assert_stat_identity(
    value: os.stat_result,
    expected: tuple[int, int, int, int],
    reason_code: str,
) -> None:
    if _stat_identity(value) != tuple(expected):
        _fail(reason_code)


def _hash_open_file(handle: Any) -> tuple[str, int]:
    try:
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        return digest.hexdigest(), total
    except OSError:
        _fail("tool_secured_write_verification_failed", effect_may_have_occurred=True)


def _read_open_bounded(
    handle: Any,
    maximum_bytes: int = _MAX_TEXT_BYTES,
) -> bytes:
    try:
        handle.seek(0)
        value = handle.read(maximum_bytes + 1)
    except OSError:
        _fail("tool_secured_handle_read_failed")
    if len(value) > maximum_bytes:
        _fail("tool_output_limit_exceeded")
    return value


def _windows_handle_info(handle: int) -> dict[str, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    value = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle), ctypes.byref(value)
    ):
        _fail("tool_secured_handle_identity_unavailable")
    return {
        "attributes": int(value.file_attributes),
        "inode": (int(value.file_index_high) << 32) | int(value.file_index_low),
        "link_count": int(value.number_of_links),
    }


def _windows_open_handle(
    path: Path,
    *,
    directory: bool,
    create: bool = False,
    delete_access: bool = False,
    read_only: bool = False,
    share_mode: int = 0,
) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    # GENERIC_READ makes the zero-share directory handle participate in share
    # checks; FILE_READ_ATTRIBUTES alone does not reliably block MoveFileEx.
    desired_access = 0x80000000 if directory or read_only else 0xC0000000
    if delete_access:
        desired_access |= 0x00010000  # DELETE
    # Directory ancestry is held without any share rights.  This deliberately
    # prevents rename/delete/reparse replacement for the entire write window.
    disposition = 1 if create else 3  # CREATE_NEW / OPEN_EXISTING
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel32.CreateFileW(
        str(path),
        desired_access,
        share_mode,
        None,
        disposition,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if create and error in {80, 183}:
            _fail("tool_create_target_exists")
        _fail("tool_secured_handle_open_failed")
    info = _windows_handle_info(int(handle))
    if info["attributes"] & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        _fail("tool_path_reparse_denied")
    return int(handle)


def _windows_final_handle_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if required <= 0 or required > 32_768:
        _fail("tool_secured_handle_identity_unavailable")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = kernel32.GetFinalPathNameByHandleW(
        handle,
        buffer,
        len(buffer),
        0,
    )
    if written <= 0 or written >= len(buffer):
        _fail("tool_secured_handle_identity_unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
        wintypes.HANDLE(handle)
    )


def _windows_mark_delete(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    disposition = FileDispositionInfo(True)
    if not kernel32.SetFileInformationByHandle(
        wintypes.HANDLE(handle),
        4,  # FileDispositionInfo
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        _fail("tool_secured_delete_failed")


def _assert_locked_binding(binding: ResolvedProjectPath) -> None:
    try:
        revalidate_resolved_path(binding)
    except Exception:
        _fail("tool_secured_path_identity_changed")


@contextmanager
def _secured_write_file(
    binding: ResolvedProjectPath,
    *,
    create: bool,
    delete: bool = False,
    artifact: bool = False,
):
    """Open a write target through locked ancestry and a no-follow handle."""

    if create and delete:
        _fail("tool_secured_write_mode_invalid")
    relative_parts = tuple(binding.canonical_relative_path.split("/"))
    if not relative_parts:
        _fail("tool_secured_path_identity_changed")
    project_root = Path(binding.project_root)
    target_path = Path(binding.absolute_path)
    if os.name == "nt":
        import msvcrt

        directory_handles: list[int] = []
        target_handle: int | None = None
        file_object = None
        target_created = False
        target_deleted = False
        try:
            current = project_root
            root_handle = _windows_open_handle(current, directory=True)
            directory_handles.append(root_handle)
            root_info = _windows_handle_info(root_handle)
            if (
                root_info["inode"] != binding.root_identity[1]
                or root_info["link_count"] != binding.root_identity[3]
            ):
                _fail("tool_secured_path_identity_changed")
            for part in relative_parts[:-1]:
                current = current / part
                handle = _windows_open_handle(current, directory=True)
                directory_handles.append(handle)
            parent_info = _windows_handle_info(directory_handles[-1])
            if (
                parent_info["inode"] != binding.parent_identity[1]
                or parent_info["link_count"] != binding.parent_identity[3]
            ):
                _fail("tool_secured_path_identity_changed")
            if artifact:
                _artifact_write_race_observation_point(binding)
            else:
                _write_race_observation_point(binding)
            _assert_locked_binding(binding)
            target_handle = _windows_open_handle(
                target_path,
                directory=False,
                create=create,
                delete_access=delete,
            )
            target_created = create
            info = _windows_handle_info(target_handle)
            if (
                (not create and info["inode"] != binding.target_identity[1])
                or info["link_count"] != 1
            ):
                _fail("tool_secured_target_identity_changed")
            descriptor = msvcrt.open_osfhandle(
                target_handle,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            target_handle = None  # descriptor now owns the HANDLE
            file_object = os.fdopen(descriptor, "r+b", closefd=True)
            opened_handle = msvcrt.get_osfhandle(file_object.fileno())
            if _windows_final_handle_path(opened_handle).resolve(strict=False) != target_path.resolve(strict=False):
                _fail("tool_secured_target_identity_changed")
            yield file_object
            if delete:
                info = _windows_handle_info(opened_handle)
                if (
                    info["inode"] != binding.target_identity[1]
                    or info["link_count"] != 1
                ):
                    _fail("tool_secured_target_identity_changed")
                _windows_mark_delete(opened_handle)
                target_deleted = True
        except CognitiveLocalBrokerError as error:
            if (target_created or target_deleted) and not error.effect_may_have_occurred:
                _fail(error.reason_code, effect_may_have_occurred=True)
            raise
        except OSError:
            _fail(
                "tool_secured_handle_open_failed",
                effect_may_have_occurred=(target_created or target_deleted),
            )
        finally:
            if file_object is not None:
                file_object.close()
            if target_handle is not None:
                _close_windows_handle(target_handle)
            for handle in reversed(directory_handles):
                _close_windows_handle(handle)
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        _fail("tool_secured_dirfd_unavailable")
    directory_fds: list[int] = []
    target_fd: int | None = None
    file_object = None
    target_created = False
    target_deleted = False
    try:
        current_fd = os.open(
            project_root,
            os.O_RDONLY | directory_flag | nofollow,
        )
        directory_fds.append(current_fd)
        _assert_stat_identity(
            os.fstat(current_fd),
            binding.root_identity,
            "tool_secured_path_identity_changed",
        )
        for part in relative_parts[:-1]:
            current_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow,
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        _assert_stat_identity(
            os.fstat(directory_fds[-1]),
            binding.parent_identity,
            "tool_secured_path_identity_changed",
        )
        if artifact:
            _artifact_write_race_observation_point(binding)
        else:
            _write_race_observation_point(binding)
        _assert_locked_binding(binding)
        flags = os.O_RDWR | nofollow
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            target_fd = os.open(
                relative_parts[-1],
                flags,
                0o600,
                dir_fd=directory_fds[-1],
            )
            target_created = create
        except FileExistsError:
            _fail("tool_create_target_exists")
        target_stat = os.fstat(target_fd)
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_nlink != 1
            or (
                not create
                and _stat_identity(target_stat) != tuple(binding.target_identity)
            )
        ):
            _fail("tool_secured_target_identity_changed")
        file_object = os.fdopen(target_fd, "r+b", closefd=True)
        target_fd = None
        yield file_object
        if delete:
            target_stat = os.fstat(file_object.fileno())
            try:
                path_stat = os.stat(
                    relative_parts[-1],
                    dir_fd=directory_fds[-1],
                    follow_symlinks=False,
                )
            except OSError:
                _fail("tool_secured_target_identity_changed")
            if (
                _stat_identity(path_stat) != _stat_identity(target_stat)
                or _stat_identity(target_stat) != tuple(binding.target_identity)
            ):
                _fail("tool_secured_target_identity_changed")
            try:
                os.unlink(relative_parts[-1], dir_fd=directory_fds[-1])
            except OSError:
                _fail("tool_secured_delete_failed")
            target_deleted = True
    except CognitiveLocalBrokerError as error:
        if (target_created or target_deleted) and not error.effect_may_have_occurred:
            _fail(error.reason_code, effect_may_have_occurred=True)
        raise
    except OSError:
        _fail(
            "tool_secured_handle_open_failed",
            effect_may_have_occurred=(target_created or target_deleted),
        )
    finally:
        if file_object is not None:
            file_object.close()
        if target_fd is not None:
            os.close(target_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


@contextmanager
def _secured_read_file(binding: ResolvedProjectPath):
    """Open the exact bound file while every ancestry component is held."""

    if binding.target_identity is None or binding.access_mode not in {
        "read_file",
        "read_optional",
        "write_file",
    }:
        _fail("tool_secured_read_mode_invalid")
    relative_parts = tuple(binding.canonical_relative_path.split("/"))
    if not relative_parts:
        _fail("tool_secured_path_identity_changed")
    project_root = Path(binding.project_root)
    target_path = Path(binding.absolute_path)
    if os.name == "nt":
        import msvcrt

        directory_handles: list[int] = []
        target_handle: int | None = None
        file_object = None
        try:
            current = project_root
            root_handle = _windows_open_handle(current, directory=True)
            directory_handles.append(root_handle)
            root_info = _windows_handle_info(root_handle)
            if (
                root_info["inode"] != binding.root_identity[1]
                or root_info["link_count"] != binding.root_identity[3]
            ):
                _fail("tool_secured_path_identity_changed")
            for part in relative_parts[:-1]:
                current = current / part
                directory_handles.append(
                    _windows_open_handle(current, directory=True)
                )
            parent_info = _windows_handle_info(directory_handles[-1])
            if (
                parent_info["inode"] != binding.parent_identity[1]
                or parent_info["link_count"] != binding.parent_identity[3]
            ):
                _fail("tool_secured_path_identity_changed")
            _read_race_observation_point(binding)
            _assert_locked_binding(binding)
            target_handle = _windows_open_handle(
                target_path,
                directory=False,
                read_only=True,
                # Excluding FILE_SHARE_DELETE still pins the file identity,
                # while READ/WRITE sharing lets us inspect a live SQLite
                # main/WAL/SHM set and detect concurrent mutation by digest.
                share_mode=0x00000001 | 0x00000002,
            )
            info = _windows_handle_info(target_handle)
            if (
                info["inode"] != binding.target_identity[1]
                or info["link_count"] != binding.target_identity[3]
            ):
                _fail("tool_secured_target_identity_changed")
            descriptor = msvcrt.open_osfhandle(
                target_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            target_handle = None
            file_object = os.fdopen(descriptor, "rb", closefd=True)
            opened_handle = msvcrt.get_osfhandle(file_object.fileno())
            if (
                _windows_final_handle_path(opened_handle).resolve(strict=False)
                != target_path.resolve(strict=False)
            ):
                _fail("tool_secured_target_identity_changed")
            yield file_object
        except CognitiveLocalBrokerError:
            raise
        except OSError:
            _fail("tool_secured_handle_open_failed")
        finally:
            if file_object is not None:
                file_object.close()
            if target_handle is not None:
                _close_windows_handle(target_handle)
            for handle in reversed(directory_handles):
                _close_windows_handle(handle)
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        _fail("tool_secured_dirfd_unavailable")
    directory_fds: list[int] = []
    target_fd: int | None = None
    file_object = None
    try:
        current_fd = os.open(
            project_root,
            os.O_RDONLY | directory_flag | nofollow,
        )
        directory_fds.append(current_fd)
        _assert_stat_identity(
            os.fstat(current_fd),
            binding.root_identity,
            "tool_secured_path_identity_changed",
        )
        for part in relative_parts[:-1]:
            current_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow,
                dir_fd=current_fd,
            )
            directory_fds.append(current_fd)
        _assert_stat_identity(
            os.fstat(directory_fds[-1]),
            binding.parent_identity,
            "tool_secured_path_identity_changed",
        )
        _read_race_observation_point(binding)
        _assert_locked_binding(binding)
        target_fd = os.open(
            relative_parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=directory_fds[-1],
        )
        target_stat = os.fstat(target_fd)
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or _stat_identity(target_stat) != tuple(binding.target_identity)
        ):
            _fail("tool_secured_target_identity_changed")
        file_object = os.fdopen(target_fd, "rb", closefd=True)
        target_fd = None
        yield file_object
    except CognitiveLocalBrokerError:
        raise
    except OSError:
        _fail("tool_secured_handle_open_failed")
    finally:
        if file_object is not None:
            file_object.close()
        if target_fd is not None:
            os.close(target_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _secured_file_capture(
    binding: ResolvedProjectPath,
    *,
    maximum_bytes: int,
    failure_code: str = "tool_path_identity_changed",
) -> tuple[bytes, os.stat_result]:
    with _secured_read_file(binding) as handle:
        try:
            before = os.fstat(handle.fileno())
        except OSError:
            _fail(failure_code)
        value = _read_open_bounded(handle, maximum_bytes)
        try:
            after = os.fstat(handle.fileno())
        except OSError:
            _fail(failure_code)
    if (
        _stat_identity(before) != tuple(binding.target_identity)
        or _stat_identity(after) != tuple(binding.target_identity)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(value) != before.st_size
        or _sha256_bytes(value) != binding.target_sha256
    ):
        _fail(failure_code)
    return value, after


def _secured_directory_entries(
    binding: ResolvedProjectPath,
) -> tuple[tuple[str, str, bool], ...]:
    """List an exact directory handle without following child links."""

    if binding.access_mode != "read_directory" or binding.target_identity is None:
        _fail("tool_secured_read_mode_invalid")
    parts = tuple(binding.canonical_relative_path.split("/"))
    project_root = Path(binding.project_root)
    target_path = Path(binding.absolute_path)
    entries: list[tuple[str, str, bool]] = []
    if os.name == "nt":
        handles: list[int] = []
        try:
            current = project_root
            root_handle = _windows_open_handle(current, directory=True)
            handles.append(root_handle)
            root_info = _windows_handle_info(root_handle)
            if (
                root_info["inode"] != binding.root_identity[1]
                or root_info["link_count"] != binding.root_identity[3]
            ):
                _fail("tool_secured_path_identity_changed")
            for part in parts:
                current = current / part
                handles.append(
                    _windows_open_handle(
                        current,
                        directory=True,
                        # pathlib/os.listdir must open the already pinned
                        # target directory.  Denying DELETE continues to
                        # block rename/reparse replacement during enumeration.
                        share_mode=0x00000001 | 0x00000002,
                    )
                )
            target_info = _windows_handle_info(handles[-1])
            if (
                target_info["inode"] != binding.target_identity[1]
                or target_info["link_count"] != binding.target_identity[3]
            ):
                _fail("tool_secured_target_identity_changed")
            _read_race_observation_point(binding)
            _assert_locked_binding(binding)
            for child in target_path.iterdir():
                linked = child.is_symlink() or is_reparse_point(child)
                try:
                    value = child.stat(follow_symlinks=False)
                except OSError:
                    _fail("tool_native_list_failed")
                kind = (
                    "directory"
                    if stat.S_ISDIR(value.st_mode)
                    else "file"
                    if stat.S_ISREG(value.st_mode)
                    else "other"
                )
                entries.append((child.name, kind, linked))
        except CognitiveLocalBrokerError:
            raise
        except OSError:
            _fail("tool_native_list_failed")
        finally:
            for handle in reversed(handles):
                _close_windows_handle(handle)
    else:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory_flag:
            _fail("tool_secured_dirfd_unavailable")
        handles: list[int] = []
        try:
            current_fd = os.open(
                project_root,
                os.O_RDONLY | directory_flag | nofollow,
            )
            handles.append(current_fd)
            _assert_stat_identity(
                os.fstat(current_fd),
                binding.root_identity,
                "tool_secured_path_identity_changed",
            )
            for part in parts:
                current_fd = os.open(
                    part,
                    os.O_RDONLY | directory_flag | nofollow,
                    dir_fd=current_fd,
                )
                handles.append(current_fd)
            _assert_stat_identity(
                os.fstat(handles[-1]),
                binding.target_identity,
                "tool_secured_target_identity_changed",
            )
            _read_race_observation_point(binding)
            _assert_locked_binding(binding)
            for name in os.listdir(handles[-1]):
                try:
                    value = os.stat(
                        name,
                        dir_fd=handles[-1],
                        follow_symlinks=False,
                    )
                except OSError:
                    _fail("tool_native_list_failed")
                linked = stat.S_ISLNK(value.st_mode)
                kind = (
                    "directory"
                    if stat.S_ISDIR(value.st_mode)
                    else "file"
                    if stat.S_ISREG(value.st_mode)
                    else "other"
                )
                entries.append((name, kind, linked))
        except CognitiveLocalBrokerError:
            raise
        except OSError:
            _fail("tool_native_list_failed")
        finally:
            for handle in reversed(handles):
                os.close(handle)
    return tuple(sorted(entries, key=lambda item: item[0].casefold()))


def _write_secured_artifact(
    root: Path,
    name: str,
    value: bytes,
    *,
    expected_root_identity: tuple[int, int, int, int],
) -> None:
    try:
        binding = resolve_project_path(
            project_root=root,
            relative_path=name,
            access_mode="create_file",
        )
        if tuple(binding.root_identity) != tuple(expected_root_identity):
            _fail("tool_artifact_root_changed")
        with _secured_write_file(binding, create=True, artifact=True) as handle:
            handle.write(value)
            _verify_secured_artifact_file(
                handle,
                binding=binding,
                expected_sha256=_sha256_bytes(value),
                expected_size=len(value),
                create=True,
            )
    except CognitiveLocalBrokerError:
        raise
    except Exception:
        _fail("tool_owned_artifact_write_failed")


def _verify_secured_open_file(
    handle: Any,
    *,
    binding: ResolvedProjectPath,
    expected_sha256: str,
    expected_size: int,
    create: bool,
) -> None:
    observed_sha256, observed_size = _hash_open_file(handle)
    target_stat = os.fstat(handle.fileno())
    if (
        observed_sha256 != expected_sha256
        or observed_size != expected_size
        or not stat.S_ISREG(target_stat.st_mode)
        or target_stat.st_nlink != 1
        or (
            not create
            and target_stat.st_ino != binding.target_identity[1]
        )
    ):
        _fail("tool_secured_write_verification_failed", effect_may_have_occurred=True)
    if os.name != "nt":
        try:
            current = os.stat(binding.absolute_path, follow_symlinks=False)
        except OSError:
            _fail("tool_secured_path_identity_changed", effect_may_have_occurred=True)
        if _stat_identity(current) != _stat_identity(target_stat):
            _fail("tool_secured_path_identity_changed", effect_may_have_occurred=True)


def _verify_secured_file(
    handle: Any,
    *,
    binding: ResolvedProjectPath,
    expected_sha256: str,
    expected_size: int,
    create: bool,
) -> None:
    """Project-write verifier with its own deterministic fault-injection seam."""

    _verify_secured_open_file(
        handle,
        binding=binding,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        create=create,
    )


def _verify_secured_artifact_file(
    handle: Any,
    *,
    binding: ResolvedProjectPath,
    expected_sha256: str,
    expected_size: int,
    create: bool,
) -> None:
    """Owned-artifact verifier isolated from project-write fault injection."""

    _verify_secured_open_file(
        handle,
        binding=binding,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        create=create,
    )


class CognitiveLocalBroker:
    """Code-owned native executor; it never accepts an argv or shell string."""

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str],
        artifact_root: str | os.PathLike[str],
    ) -> None:
        requested_project = Path(os.path.abspath(str(project_root)))
        requested_artifacts = Path(os.path.abspath(str(artifact_root)))
        try:
            linked_project = requested_project.is_symlink() or is_reparse_point(
                requested_project
            )
            linked_artifacts = (
                requested_artifacts.is_symlink()
                or is_reparse_point(requested_artifacts)
            )
            project = requested_project.resolve(strict=True)
            artifacts = requested_artifacts.resolve(strict=True)
            artifact_stat = requested_artifacts.stat(follow_symlinks=False)
        except (OSError, RuntimeError, ValueError):
            _fail("tool_broker_root_unavailable")
        if (
            linked_project
            or linked_artifacts
            or not project.is_dir()
            or not artifacts.is_dir()
            or project.is_symlink()
            or artifacts.is_symlink()
            or is_reparse_point(project)
            or is_reparse_point(artifacts)
        ):
            _fail("tool_broker_root_unavailable")
        self.project_root = project
        self.artifact_root = artifacts
        self._artifact_root_requested = requested_artifacts
        self._artifact_root_identity = _stat_identity(artifact_stat)

    def _assert_binding(self, binding: ResolvedProjectPath) -> Path:
        if (
            not isinstance(binding, ResolvedProjectPath)
            or Path(binding.project_root) != self.project_root
        ):
            _fail("tool_broker_path_binding_mismatch")
        revalidate_resolved_path(binding)
        return Path(binding.absolute_path)

    def _assert_artifact_root(self) -> None:
        try:
            requested = self._artifact_root_requested
            if requested.is_symlink() or is_reparse_point(requested):
                _fail("tool_artifact_root_changed")
            resolved = requested.resolve(strict=True)
            value = requested.stat(follow_symlinks=False)
        except CognitiveLocalBrokerError:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("tool_artifact_root_unavailable")
        if (
            resolved != self.artifact_root
            or not stat.S_ISDIR(value.st_mode)
            or _stat_identity(value) != self._artifact_root_identity
        ):
            _fail("tool_artifact_root_changed")

    def read_file(self, binding: ResolvedProjectPath) -> dict[str, Any]:
        if binding.access_mode != "read_file":
            _fail("tool_broker_path_mode_mismatch")
        self._assert_binding(binding)
        value, _file_stat = _secured_file_capture(
            binding,
            maximum_bytes=_MAX_TEXT_BYTES,
        )
        return {
            "relative_path": binding.canonical_relative_path,
            "content": _decode_text(value),
            "content_sha256": _sha256_bytes(value),
            "truncated": False,
        }

    def list_directory(
        self,
        binding: ResolvedProjectPath,
        *,
        maximum_entries: int,
    ) -> dict[str, Any]:
        if binding.access_mode != "read_directory":
            _fail("tool_broker_path_mode_mismatch")
        self._assert_binding(binding)
        children = _secured_directory_entries(binding)
        entries: list[str] = []
        truncated = len(children) > maximum_entries
        for name, _kind, linked in children[:maximum_entries]:
            if linked:
                _fail("tool_path_reparse_denied")
            relative = f"{binding.canonical_relative_path}/{name}"
            entries.append(relative)
        return {"entries": entries, "truncated": truncated}

    def search_text(
        self,
        binding: ResolvedProjectPath,
        *,
        search_text: str,
        maximum_results: int,
    ) -> dict[str, Any]:
        self._assert_binding(binding)
        if not isinstance(search_text, str) or not search_text:
            _fail("tool_search_text_invalid")
        candidates: list[ResolvedProjectPath] = []
        if binding.access_mode == "read_file":
            candidates.append(binding)
        elif binding.access_mode == "read_directory":
            pending = [binding]
            while pending:
                current = pending.pop()
                for name, kind, linked in reversed(
                    _secured_directory_entries(current)
                ):
                    if linked or name.casefold() == ".git":
                        continue
                    relative = f"{current.canonical_relative_path}/{name}"
                    if kind == "directory":
                        pending.append(
                            resolve_project_path(
                                project_root=self.project_root,
                                relative_path=relative,
                                access_mode="read_directory",
                            )
                        )
                    elif kind == "file":
                        candidates.append(
                            resolve_project_path(
                                project_root=self.project_root,
                                relative_path=relative,
                                access_mode="read_file",
                            )
                        )
                        if len(candidates) > 10_000:
                            _fail("tool_search_scope_too_large")
        else:
            _fail("tool_path_type_mismatch")
        matches: list[dict[str, Any]] = []
        truncated = False
        for candidate in candidates:
            try:
                raw, _file_stat = _secured_file_capture(
                    candidate,
                    maximum_bytes=_MAX_TEXT_BYTES,
                )
                text = _decode_text(raw)
            except CognitiveLocalBrokerError as error:
                if error.reason_code in {
                    "tool_file_size_limit_exceeded",
                    "tool_output_encoding_invalid",
                }:
                    continue
                raise
            relative = candidate.canonical_relative_path
            for line_number, line in enumerate(text.splitlines(), start=1):
                if search_text in line:
                    if len(matches) >= maximum_results:
                        truncated = True
                        break
                    matches.append(
                        {
                            "relative_path": relative,
                            "line": line_number,
                            "text": line[:16_384],
                        }
                    )
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    def _capture_sqlite_set(
        self,
        binding: ResolvedProjectPath,
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        bindings: list[tuple[str, str, ResolvedProjectPath]] = [
            ("database", "", binding)
        ]
        absent: list[str] = []
        for suffix in ("-wal", "-shm"):
            sidecar = resolve_project_path(
                project_root=self.project_root,
                relative_path=f"{binding.canonical_relative_path}{suffix}",
                access_mode="read_optional",
                maximum_file_bytes=_MAX_SQLITE_CAPTURE_BYTES,
            )
            if sidecar.target_exists:
                bindings.append((suffix[1:], suffix, sidecar))
            else:
                absent.append(suffix)
        snapshots: list[dict[str, Any]] = []
        files: dict[str, bytes] = {}
        for kind, suffix, file_binding in bindings:
            value, file_stat = _secured_file_capture(
                file_binding,
                maximum_bytes=_MAX_SQLITE_CAPTURE_BYTES,
                failure_code="tool_database_snapshot_changed",
            )
            snapshot = _sqlite_capture_identity(
                file_binding,
                value,
                file_stat,
                kind=kind,
            )
            files[suffix] = value
            snapshots.append(snapshot)
        database = snapshots[0]
        sidecars = snapshots[1:]
        identity = {
            "database": database,
            "sidecars": sidecars,
            "absent_sidecars": absent,
        }
        return (
            {**identity, "snapshot_digest": canonical_digest(identity)},
            files,
        )

    def inspect_database(self, binding: ResolvedProjectPath) -> dict[str, Any]:
        if binding.access_mode != "read_file":
            _fail("tool_broker_path_mode_mismatch")
        self._assert_binding(binding)
        initial, captured = self._capture_sqlite_set(binding)
        if initial["database"]["sha256"] != binding.target_sha256:
            _fail("tool_path_identity_changed")
        try:
            with TemporaryDirectory(prefix="yunos-v10-db-inspect-") as temp:
                clone = Path(temp) / "snapshot.db"
                _write_exclusive(clone, captured[""])
                for suffix in ("-wal", "-shm"):
                    if suffix in captured:
                        _write_exclusive(
                            Path(temp) / f"snapshot.db{suffix}",
                            captured[suffix],
                        )
                uri = f"{clone.as_uri()}?mode=ro"
                with closing(
                    sqlite3.connect(uri, uri=True, timeout=1.0)
                ) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN")
                    connection.execute(
                        "SELECT 1 FROM sqlite_master LIMIT 1"
                    ).fetchone()
                    integrity_rows = connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchall()
                    foreign_keys = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    user_version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    tables = sorted(
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                        ).fetchall()
                    )
                    connection.rollback()
        except CognitiveLocalBrokerError:
            raise
        except (OSError, sqlite3.Error):
            _fail("tool_database_readonly_check_failed")
        after, _after_files = self._capture_sqlite_set(binding)
        if after != initial:
            _fail("tool_database_snapshot_changed")
        if integrity_rows != [("ok",)]:
            _fail("tool_database_integrity_failed")
        return {
            "relative_path": binding.canonical_relative_path,
            "integrity": "ok",
            "foreign_key_violations": len(foreign_keys),
            "user_version": user_version,
            "tables": tables,
            "database_sha256": initial["database"]["sha256"],
            **_public_sqlite_snapshot(initial),
        }

    def _backup_record(
        self,
        *,
        proposal: ToolProposal,
        execution_key: str,
        binding: ResolvedProjectPath,
        before_bytes: bytes | None,
        after_sha256: str,
    ) -> tuple[str, Path]:
        self._assert_artifact_root()
        identity = {
            "broker_version": LOCAL_BROKER_VERSION,
            "run_id": proposal.run_id,
            "objective_id": proposal.objective_id,
            "objective_digest": proposal.objective_digest,
            "plan_id": proposal.plan_id,
            "plan_digest": proposal.plan_digest,
            "authority_artifact_digest": proposal.authorization_digest,
            "execution_key": execution_key,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "relative_path": binding.canonical_relative_path,
            "before_sha256": None if before_bytes is None else _sha256_bytes(before_bytes),
            "after_sha256": after_sha256,
        }
        reference = make_identity("toolbackup", identity)
        storage_key = canonical_digest({"backup_reference": reference})
        before_name = None
        if before_bytes is not None:
            before_name = f"{storage_key}.before.bin"
            _write_secured_artifact(
                self.artifact_root,
                before_name,
                before_bytes,
                expected_root_identity=self._artifact_root_identity,
            )
        record = {
            **identity,
            "backup_reference": reference,
            "artifact_storage_key": storage_key,
            "before_artifact": before_name,
            "record_digest": canonical_digest(identity),
        }
        record_path = self.artifact_root / f"{storage_key}.json"
        _write_secured_artifact(
            self.artifact_root,
            record_path.name,
            canonical_json(record).encode("utf-8"),
            expected_root_identity=self._artifact_root_identity,
        )
        return reference, record_path

    def _read_owned_artifact(self, path: Path, *, maximum_bytes: int) -> bytes:
        self._assert_artifact_root()
        if path.parent != self.artifact_root:
            _fail("tool_rollback_artifact_boundary_mismatch")
        try:
            binding = resolve_project_path(
                project_root=self.artifact_root,
                relative_path=path.name,
                access_mode="read_file",
                maximum_file_bytes=maximum_bytes,
            )
            if tuple(binding.root_identity) != self._artifact_root_identity:
                _fail("tool_artifact_root_changed")
            value, file_stat = _secured_file_capture(
                binding,
                maximum_bytes=maximum_bytes,
                failure_code="tool_rollback_artifact_boundary_mismatch",
            )
        except CognitiveLocalBrokerError:
            raise
        except Exception:
            _fail("tool_rollback_record_unavailable")
        if file_stat.st_nlink != 1:
            _fail("tool_rollback_artifact_boundary_mismatch")
        return value

    @staticmethod
    def _backup_storage_key(backup_reference: str) -> str:
        reference_prefix = "toolbackup:sha256:"
        reference_digest = (
            backup_reference[len(reference_prefix) :]
            if isinstance(backup_reference, str)
            and backup_reference.startswith(reference_prefix)
            else ""
        )
        if (
            len(reference_digest) != 64
            or any(character not in "0123456789abcdef" for character in reference_digest)
        ):
            _fail("tool_rollback_reference_invalid")
        return canonical_digest({"backup_reference": backup_reference})

    def _load_backup_record(self, backup_reference: str) -> dict[str, Any]:
        storage_key = self._backup_storage_key(backup_reference)
        record_path = self.artifact_root / f"{storage_key}.json"
        raw_bytes = self._read_owned_artifact(
            record_path,
            maximum_bytes=_MAX_BACKUP_RECORD_BYTES,
        )
        try:
            raw = raw_bytes.decode("utf-8", errors="strict")
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _fail("tool_rollback_record_unavailable")
        expected_keys = {
            "broker_version",
            "run_id",
            "objective_id",
            "objective_digest",
            "plan_id",
            "plan_digest",
            "authority_artifact_digest",
            "execution_key",
            "proposal_id",
            "proposal_digest",
            "relative_path",
            "before_sha256",
            "after_sha256",
            "backup_reference",
            "artifact_storage_key",
            "before_artifact",
            "record_digest",
        }
        if (
            not isinstance(record, dict)
            or set(record) != expected_keys
            or canonical_json(record) != raw
            or contains_secret_material(record)
            or record.get("backup_reference") != backup_reference
            or record.get("artifact_storage_key") != storage_key
        ):
            _fail("tool_rollback_record_tampered")
        identity = {
            key: record[key]
            for key in (
                "broker_version",
                "run_id",
                "objective_id",
                "objective_digest",
                "plan_id",
                "plan_digest",
                "authority_artifact_digest",
                "execution_key",
                "proposal_id",
                "proposal_digest",
                "relative_path",
                "before_sha256",
                "after_sha256",
            )
        }
        before_sha256 = identity["before_sha256"]
        after_sha256 = identity["after_sha256"]
        if (
            identity["broker_version"] != LOCAL_BROKER_VERSION
            or not isinstance(identity["run_id"], str)
            or not identity["run_id"]
            or not isinstance(identity["objective_id"], str)
            or not identity["objective_id"]
            or not isinstance(identity["objective_digest"], str)
            or len(identity["objective_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["objective_digest"]
            )
            or not isinstance(identity["plan_id"], str)
            or not identity["plan_id"]
            or not isinstance(identity["plan_digest"], str)
            or len(identity["plan_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["plan_digest"]
            )
            or not isinstance(identity["authority_artifact_digest"], str)
            or len(identity["authority_artifact_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["authority_artifact_digest"]
            )
            or not isinstance(identity["execution_key"], str)
            or not identity["execution_key"]
            or not isinstance(identity["proposal_id"], str)
            or not identity["proposal_id"]
            or not isinstance(identity["proposal_digest"], str)
            or len(identity["proposal_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity["proposal_digest"]
            )
            or not isinstance(identity["relative_path"], str)
            or not identity["relative_path"]
            or not isinstance(after_sha256, str)
            or len(after_sha256) != 64
            or any(character not in "0123456789abcdef" for character in after_sha256)
            or (
                before_sha256 is not None
                and (
                    not isinstance(before_sha256, str)
                    or len(before_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in before_sha256
                    )
                )
            )
            or record["record_digest"] != canonical_digest(identity)
            or make_identity("toolbackup", identity) != backup_reference
        ):
            _fail("tool_rollback_record_tampered")
        expected_before_artifact = (
            None if before_sha256 is None else f"{storage_key}.before.bin"
        )
        if record["before_artifact"] != expected_before_artifact:
            _fail("tool_rollback_record_tampered")
        return record

    @staticmethod
    def _assert_rollback_owner(
        record: Mapping[str, Any],
        proposal: ToolProposal,
    ) -> None:
        if not isinstance(proposal, ToolProposal):
            _fail("invalid_tool_rollback_proposal")
        expected_owner = {
            "run_id": proposal.run_id,
            "objective_id": proposal.objective_id,
            "objective_digest": proposal.objective_digest,
            "plan_id": proposal.plan_id,
            "plan_digest": proposal.plan_digest,
            "authority_artifact_digest": proposal.authorization_digest,
        }
        if any(record.get(key) != value for key, value in expected_owner.items()):
            _fail("tool_rollback_owner_mismatch")

    def _prepare_rollback(
        self,
        *,
        proposal: ToolProposal,
        backup_reference: str,
        expected_current_sha256: str,
    ) -> tuple[dict[str, Any], ResolvedProjectPath]:
        if (
            not isinstance(expected_current_sha256, str)
            or len(expected_current_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_current_sha256
            )
        ):
            _fail("tool_rollback_scope_mismatch")
        record = self._load_backup_record(backup_reference)
        self._assert_rollback_owner(record, proposal)
        if (
            record["after_sha256"] != expected_current_sha256
            or record["before_sha256"] is None
        ):
            _fail("tool_rollback_scope_mismatch")
        binding = resolve_project_path(
            project_root=self.project_root,
            relative_path=record["relative_path"],
            access_mode="write_file",
        )
        self._assert_binding(binding)
        if binding.target_sha256 != expected_current_sha256:
            _fail("tool_rollback_current_digest_mismatch")
        return record, binding

    def resolve_rollback_binding(
        self,
        *,
        proposal: ToolProposal,
        backup_reference: str,
        expected_current_sha256: str,
    ) -> ResolvedProjectPath:
        _record, binding = self._prepare_rollback(
            proposal=proposal,
            backup_reference=backup_reference,
            expected_current_sha256=expected_current_sha256,
        )
        return binding

    def _prepare_owned_disposal(
        self,
        *,
        proposal: ToolProposal,
        relative_path: str,
        ownership_reference: str,
        expected_before_sha256: str,
    ) -> tuple[dict[str, Any], ResolvedProjectPath]:
        if (
            not isinstance(proposal, ToolProposal)
            or proposal.capability_id != "project.temp.dispose_owned"
        ):
            _fail("invalid_tool_owned_disposal_proposal")
        record = self._load_backup_record(ownership_reference)
        self._assert_rollback_owner(record, proposal)
        if (
            record["before_sha256"] is not None
            or record["relative_path"] != relative_path
            or record["after_sha256"] != expected_before_sha256
        ):
            _fail("tool_owned_disposal_scope_mismatch")
        binding = resolve_project_path(
            project_root=self.project_root,
            relative_path=record["relative_path"],
            access_mode="write_file",
        )
        self._assert_binding(binding)
        if binding.target_sha256 != expected_before_sha256:
            _fail("tool_owned_disposal_current_digest_mismatch")
        return record, binding

    def resolve_owned_disposal_binding(
        self,
        *,
        proposal: ToolProposal,
        relative_path: str,
        ownership_reference: str,
        expected_before_sha256: str,
    ) -> ResolvedProjectPath:
        _record, binding = self._prepare_owned_disposal(
            proposal=proposal,
            relative_path=relative_path,
            ownership_reference=ownership_reference,
            expected_before_sha256=expected_before_sha256,
        )
        return binding

    def patch_file(
        self,
        *,
        proposal: ToolProposal,
        execution_key: str,
        binding: ResolvedProjectPath,
    ) -> dict[str, Any]:
        if binding.access_mode != "write_file":
            _fail("tool_broker_path_mode_mismatch")
        path = self._assert_binding(binding)
        arguments = dict(proposal.typed_arguments)
        before_bytes = _read_bounded(path)
        before_sha = _sha256_bytes(before_bytes)
        if before_sha != arguments["expected_before_sha256"]:
            _fail("tool_patch_before_digest_mismatch")
        try:
            replacement = arguments["replacement_content"].encode("utf-8", errors="strict")
        except (AttributeError, UnicodeEncodeError):
            _fail("tool_patch_content_invalid")
        after_sha = _sha256_bytes(replacement)
        if after_sha != arguments["expected_after_sha256"]:
            _fail("tool_patch_after_digest_mismatch")
        reference, _record_path = self._backup_record(
            proposal=proposal,
            execution_key=execution_key,
            binding=binding,
            before_bytes=before_bytes,
            after_sha256=after_sha,
        )
        changed = False
        try:
            with _secured_write_file(binding, create=False) as handle:
                current = _read_open_bounded(handle)
                if current != before_bytes or _sha256_bytes(current) != before_sha:
                    _fail("tool_secured_target_identity_changed")
                handle.seek(0)
                handle.truncate(0)
                changed = True
                handle.write(replacement)
                _verify_secured_file(
                    handle,
                    binding=binding,
                    expected_sha256=after_sha,
                    expected_size=len(replacement),
                    create=False,
                )
        except CognitiveLocalBrokerError as error:
            if changed and not error.effect_may_have_occurred:
                _fail(error.reason_code, effect_may_have_occurred=True)
            raise
        except OSError:
            _fail("tool_patch_write_failed", effect_may_have_occurred=changed)
        return {
            "relative_path": binding.canonical_relative_path,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "backup_reference": reference,
            "verification_passed": True,
        }

    def create_file(
        self,
        *,
        proposal: ToolProposal,
        execution_key: str,
        binding: ResolvedProjectPath,
    ) -> dict[str, Any]:
        if binding.access_mode != "create_file":
            _fail("tool_broker_path_mode_mismatch")
        self._assert_binding(binding)
        arguments = dict(proposal.typed_arguments)
        try:
            content = arguments["content"].encode("utf-8", errors="strict")
        except (AttributeError, UnicodeEncodeError):
            _fail("tool_create_content_invalid")
        after_sha = _sha256_bytes(content)
        if after_sha != arguments["expected_after_sha256"]:
            _fail("tool_create_after_digest_mismatch")
        reference, _record_path = self._backup_record(
            proposal=proposal,
            execution_key=execution_key,
            binding=binding,
            before_bytes=None,
            after_sha256=after_sha,
        )
        changed = False
        try:
            with _secured_write_file(binding, create=True) as handle:
                changed = True
                handle.write(content)
                _verify_secured_file(
                    handle,
                    binding=binding,
                    expected_sha256=after_sha,
                    expected_size=len(content),
                    create=True,
                )
        except CognitiveLocalBrokerError as error:
            if changed and not error.effect_may_have_occurred:
                _fail(error.reason_code, effect_may_have_occurred=True)
            raise
        except OSError:
            _fail("tool_create_write_failed", effect_may_have_occurred=changed)
        return {
            "relative_path": binding.canonical_relative_path,
            "after_sha256": after_sha,
            "backup_reference": reference,
            "created": True,
            "verification_passed": True,
        }

    def rollback_patch(
        self,
        *,
        proposal: ToolProposal,
        backup_reference: str,
        expected_current_sha256: str,
    ) -> dict[str, Any]:
        record, binding = self._prepare_rollback(
            proposal=proposal,
            backup_reference=backup_reference,
            expected_current_sha256=expected_current_sha256,
        )
        before_path = self.artifact_root / record["before_artifact"]
        before_bytes = self._read_owned_artifact(
            before_path,
            maximum_bytes=_MAX_TEXT_BYTES,
        )
        if _sha256_bytes(before_bytes) != record["before_sha256"]:
            _fail("tool_rollback_backup_tampered")
        changed = False
        try:
            with _secured_write_file(binding, create=False) as handle:
                current = _read_open_bounded(handle)
                if _sha256_bytes(current) != expected_current_sha256:
                    _fail("tool_rollback_current_digest_mismatch")
                handle.seek(0)
                handle.truncate(0)
                changed = True
                handle.write(before_bytes)
                _verify_secured_file(
                    handle,
                    binding=binding,
                    expected_sha256=record["before_sha256"],
                    expected_size=len(before_bytes),
                    create=False,
                )
        except CognitiveLocalBrokerError as error:
            if changed and not error.effect_may_have_occurred:
                _fail(error.reason_code, effect_may_have_occurred=True)
            raise
        except OSError:
            _fail("tool_rollback_write_failed", effect_may_have_occurred=changed)
        return {
            "backup_reference": backup_reference,
            "relative_path": record["relative_path"],
            "restored_sha256": record["before_sha256"],
            "verification_passed": True,
        }

    def dispose_owned_file(
        self,
        *,
        proposal: ToolProposal,
        relative_path: str,
        ownership_reference: str,
        expected_before_sha256: str,
    ) -> dict[str, Any]:
        _record, binding = self._prepare_owned_disposal(
            proposal=proposal,
            relative_path=relative_path,
            ownership_reference=ownership_reference,
            expected_before_sha256=expected_before_sha256,
        )
        changed = False
        try:
            with _secured_write_file(binding, create=False, delete=True) as handle:
                current = _read_open_bounded(handle)
                if _sha256_bytes(current) != expected_before_sha256:
                    _fail("tool_owned_disposal_current_digest_mismatch")
            changed = True
            try:
                missing = resolve_project_path(
                    project_root=self.project_root,
                    relative_path=relative_path,
                    access_mode="create_file",
                )
            except Exception:
                _fail(
                    "tool_owned_disposal_verification_failed",
                    effect_may_have_occurred=True,
                )
            if missing.target_exists:
                _fail(
                    "tool_owned_disposal_verification_failed",
                    effect_may_have_occurred=True,
                )
        except CognitiveLocalBrokerError as error:
            if changed and not error.effect_may_have_occurred:
                _fail(error.reason_code, effect_may_have_occurred=True)
            raise
        except OSError:
            _fail(
                "tool_owned_disposal_failed",
                effect_may_have_occurred=changed,
            )
        return {
            "relative_path": relative_path,
            "before_sha256": expected_before_sha256,
            "ownership_reference": ownership_reference,
            "backup_reference": ownership_reference,
            "disposed": True,
        }


__all__ = [
    "CognitiveLocalBroker",
    "CognitiveLocalBrokerError",
    "LOCAL_BROKER_VERSION",
]
