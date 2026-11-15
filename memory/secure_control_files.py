"""Pinned filesystem primitives for v10-F release control artifacts.

The release controllers need a smaller boundary than the general Tool Gateway:
one already trusted anchor, one directory below it, and a closed set of regular
files.  This module keeps every ancestry component open while it creates or
reads those files.  Windows handles deliberately omit ``FILE_SHARE_DELETE``;
POSIX uses directory descriptors plus ``openat``/``O_NOFOLLOW`` and verifies
the pathname bindings again before releasing the descriptors.

The primitives do not authenticate document contents.  Callers must still
validate their closed schemas and canonical digests while the returned file
handles remain pinned.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO, Iterator

from memory.db_path_policy import is_reparse_point


class SecureControlFileError(ValueError):
    """Stable release-control filesystem failure."""


def _fail(reason_code: str) -> None:
    raise SecureControlFileError(reason_code)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _reject_device_or_alias_syntax(path: Path) -> None:
    text = str(path)
    normalized = text.replace("/", "\\")
    if (
        "\x00" in text
        or normalized.startswith(("\\\\?\\", "\\\\.\\", "\\\\"))
        or (os.name == "nt" and ":" in normalized[2:])
    ):
        _fail("secure_control_path_forbidden")


def _reject_reparse_chain(path: Path) -> None:
    _reject_device_or_alias_syntax(path)
    current = path.absolute()
    while True:
        try:
            if (current.exists() or current.is_symlink()) and is_reparse_point(
                current
            ):
                _fail("secure_control_path_reparse_forbidden")
        except OSError:
            _fail("secure_control_path_identity_unavailable")
        if current.parent == current:
            return
        current = current.parent


def _safe_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).name != name
        or any(character in name for character in ("/", "\\", ":", "\x00"))
    ):
        _fail("secure_control_filename_forbidden")
    return name


def _identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _hash_handle(handle: BinaryIO, maximum_bytes: int) -> tuple[int, str]:
    try:
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = handle.read(min(1024 * 1024, maximum_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                _fail("secure_control_file_too_large")
            digest.update(chunk)
        handle.seek(0)
        return size, digest.hexdigest()
    except SecureControlFileError:
        raise
    except (OSError, ValueError):
        _fail("secure_control_file_read_failed")


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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    value = ByHandleFileInformation()
    if not get_information(wintypes.HANDLE(handle), ctypes.byref(value)):
        _fail("secure_control_handle_identity_unavailable")
    return {
        "attributes": int(value.file_attributes),
        "device": int(value.volume_serial_number),
        "inode": (int(value.file_index_high) << 32) | int(value.file_index_low),
        "links": int(value.number_of_links),
        "size": (int(value.file_size_high) << 32) | int(value.file_size_low),
    }


def _windows_open_handle(
    path: Path,
    *,
    directory: bool,
    create_file: bool = False,
    open_or_create: bool = False,
    read_only: bool = False,
) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    desired_access = 0x80000000 if directory or read_only else 0xC0000000
    # Directory handles allow ordinary child reads/writes but never delete
    # sharing, so a pinned component cannot be renamed or replaced.  File
    # handles permit only additional readers; writes, hard-link mutation and
    # rename/delete remain denied for the complete validation window.
    share_mode = 0x00000001 | 0x00000002 if directory else 0x00000001
    if create_file and open_or_create:
        _fail("secure_control_open_mode_invalid")
    disposition = (
        1 if create_file else 4 if open_or_create else 3
    )  # CREATE_NEW / OPEN_ALWAYS / OPEN_EXISTING
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = create(
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
        if create_file and error in {80, 183}:
            _fail("secure_control_file_exists")
        if not create_file and not open_or_create and error in {2, 3}:
            _fail("secure_control_file_missing")
        _fail("secure_control_handle_open_failed")
    info = _windows_handle_info(int(handle))
    if info["attributes"] & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        _fail("secure_control_path_reparse_forbidden")
    return int(handle)


def _windows_final_handle_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel32.GetFinalPathNameByHandleW
    get_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_path.restype = wintypes.DWORD
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    written = get_path(wintypes.HANDLE(handle), buffer, size, 0)
    if written == 0 or written >= size:
        _fail("secure_control_handle_path_unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        _fail("secure_control_handle_close_failed")


@dataclass
class PinnedControlFile:
    path: Path
    handle: BinaryIO
    identity: tuple[int, int]
    expected_size: int
    expected_sha256: str

    def read(self, maximum_bytes: int = 1024 * 1024) -> bytes:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            _fail("secure_control_file_limit_invalid")
        try:
            self.handle.seek(0)
            value = self.handle.read(maximum_bytes + 1)
            self.handle.seek(0)
        except OSError:
            _fail("secure_control_file_read_failed")
        if len(value) > maximum_bytes:
            _fail("secure_control_file_too_large")
        return value

    def verify(self) -> None:
        try:
            descriptor_stat = os.fstat(self.handle.fileno())
            path_stat = self.path.stat()
            size, digest = _hash_handle(
                self.handle, max(self.expected_size, 1) + 1
            )
        except (OSError, ValueError):
            _fail("secure_control_file_identity_changed")
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or int(descriptor_stat.st_nlink) != 1
            or _identity(descriptor_stat) != self.identity
            or _identity(path_stat) != self.identity
            or size != self.expected_size
            or digest != self.expected_sha256
            or is_reparse_point(self.path)
        ):
            _fail("secure_control_file_identity_changed")
        if os.name == "nt":
            import msvcrt

            raw = msvcrt.get_osfhandle(self.handle.fileno())
            if _path_key(_windows_final_handle_path(raw)) != _path_key(
                self.path.resolve(strict=True)
            ):
                _fail("secure_control_file_identity_changed")


@dataclass
class PinnedMutableControlFile:
    """A regular file whose pathname identity is pinned but bytes may change.

    The owning directory holds this handle for the complete release-control
    operation.  On Windows the handle denies write/delete sharing; on POSIX the
    descriptor is opened relative to the pinned directory and carries an
    exclusive advisory lock.  Callers still validate the application-level
    append/rewrite protocol after every mutation.
    """

    path: Path
    handle: BinaryIO
    identity: tuple[int, int]
    maximum_bytes: int

    def verify_binding(self) -> None:
        try:
            descriptor_stat = os.fstat(self.handle.fileno())
            path_stat = self.path.stat()
        except (OSError, ValueError):
            _fail("secure_control_mutable_identity_changed")
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or int(descriptor_stat.st_nlink) != 1
            or _identity(descriptor_stat) != self.identity
            or _identity(path_stat) != self.identity
            or int(descriptor_stat.st_size) > self.maximum_bytes
            or is_reparse_point(self.path)
        ):
            _fail("secure_control_mutable_identity_changed")
        if os.name == "nt":
            import msvcrt

            raw = msvcrt.get_osfhandle(self.handle.fileno())
            if _path_key(_windows_final_handle_path(raw)) != _path_key(
                self.path.resolve(strict=True)
            ):
                _fail("secure_control_mutable_identity_changed")

    def read(self) -> bytes:
        self.verify_binding()
        try:
            self.handle.seek(0)
            value = self.handle.read(self.maximum_bytes + 1)
            self.handle.seek(0)
        except (OSError, ValueError):
            _fail("secure_control_mutable_read_failed")
        if len(value) > self.maximum_bytes:
            _fail("secure_control_mutable_too_large")
        self.verify_binding()
        return value

    def rewrite(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or len(payload) > self.maximum_bytes:
            _fail("secure_control_mutable_too_large")
        self.verify_binding()
        try:
            self.handle.seek(0)
            self.handle.truncate(0)
            self.handle.write(payload)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.seek(0)
        except (OSError, ValueError):
            _fail("secure_control_mutable_write_failed")
        self.verify_binding()

    def append(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            _fail("secure_control_mutable_write_failed")
        self.verify_binding()
        try:
            size = int(os.fstat(self.handle.fileno()).st_size)
            if size + len(payload) > self.maximum_bytes:
                _fail("secure_control_mutable_too_large")
            self.handle.seek(0, os.SEEK_END)
            self.handle.write(payload)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.seek(0)
        except SecureControlFileError:
            raise
        except (OSError, ValueError):
            _fail("secure_control_mutable_write_failed")
        self.verify_binding()


@dataclass
class PinnedControlDirectory:
    anchor: Path
    path: Path
    directory_tokens: list[tuple[Path, tuple[int, int], int]]
    files: list[PinnedControlFile] = field(default_factory=list)
    mutable_files: list[PinnedMutableControlFile] = field(default_factory=list)
    owned_created_files: list[tuple[str, tuple[int, int]]] = field(
        default_factory=list
    )
    created_leaf: bool = False

    @property
    def leaf_token(self) -> int:
        return self.directory_tokens[-1][2]

    def _path_for_name(self, name: str) -> Path:
        return self.path / _safe_name(name)

    def _open_descriptor(
        self,
        name: str,
        *,
        create: bool,
        open_or_create: bool = False,
        read_only: bool,
        acquire_lock: bool = True,
    ) -> BinaryIO:
        target = self._path_for_name(name)
        if os.name == "nt":
            import msvcrt

            raw = _windows_open_handle(
                target,
                directory=False,
                create_file=create,
                open_or_create=open_or_create,
                read_only=read_only,
            )
            try:
                descriptor = msvcrt.open_osfhandle(
                    raw,
                    (os.O_RDONLY if read_only else os.O_RDWR)
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOINHERIT", 0),
                )
            except OSError:
                _close_windows_handle(raw)
                _fail("secure_control_handle_open_failed")
            try:
                return os.fdopen(
                    descriptor,
                    "rb" if read_only else "r+b",
                    buffering=0,
                    closefd=True,
                )
            except (OSError, ValueError):
                os.close(descriptor)
                _fail("secure_control_handle_open_failed")

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            _fail("secure_control_nofollow_unavailable")
        flags = (
            (os.O_RDONLY if read_only else os.O_RDWR)
            | nofollow
            | getattr(os, "O_NONBLOCK", 0)
        )
        if create or open_or_create:
            flags |= os.O_CREAT
        if create:
            flags |= os.O_EXCL
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=self.leaf_token,
            )
            try:
                import fcntl

                if acquire_lock:
                    fcntl.flock(
                        descriptor,
                        (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX)
                        | fcntl.LOCK_NB,
                    )
            except (ImportError, OSError):
                os.close(descriptor)
                _fail("secure_control_file_lock_unavailable")
        except FileExistsError:
            _fail("secure_control_file_exists")
        except FileNotFoundError:
            _fail("secure_control_file_missing")
        except OSError:
            _fail("secure_control_handle_open_failed")
        try:
            return os.fdopen(
                descriptor,
                "rb" if read_only else "r+b",
                buffering=0,
                closefd=True,
            )
        except (OSError, ValueError):
            os.close(descriptor)
            _fail("secure_control_handle_open_failed")

    def _unlink_owned_file(
        self, name: str, expected_identity: tuple[int, int]
    ) -> None:
        target = self._path_for_name(name)
        try:
            if os.name == "nt":
                details = target.stat()
                if is_reparse_point(target):
                    _fail("secure_control_partial_cleanup_unsafe")
                if (
                    not stat.S_ISREG(details.st_mode)
                    or int(details.st_nlink) != 1
                    or _identity(details) != expected_identity
                ):
                    _fail("secure_control_partial_cleanup_unsafe")
                target.unlink()
            else:
                details = os.stat(
                    name,
                    dir_fd=self.leaf_token,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(details.st_mode)
                    or int(details.st_nlink) != 1
                    or _identity(details) != expected_identity
                ):
                    _fail("secure_control_partial_cleanup_unsafe")
                os.unlink(name, dir_fd=self.leaf_token)
                os.fsync(self.leaf_token)
        except FileNotFoundError:
            return
        except SecureControlFileError:
            raise
        except OSError:
            _fail("secure_control_partial_cleanup_failed")

    def create_file(
        self,
        name: str,
        payload: bytes,
        *,
        maximum_bytes: int = 1024 * 1024,
    ) -> PinnedControlFile:
        if (
            type(payload) is not bytes
            or type(maximum_bytes) is not int
            or maximum_bytes < 1
            or len(payload) > maximum_bytes
        ):
            _fail("secure_control_file_payload_invalid")
        target = self._path_for_name(name)
        handle = self._open_descriptor(name, create=True, read_only=False)
        try:
            created_identity = _identity(os.fstat(handle.fileno()))
        except OSError:
            handle.close()
            _fail("secure_control_file_identity_changed")
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            details = os.fstat(handle.fileno())
            handle.seek(0)
            actual = handle.read(len(payload) + 1)
            handle.seek(0)
            if os.name != "nt":
                os.fsync(self.leaf_token)
        except BaseException:
            try:
                handle.close()
            except BaseException:
                pass
            self._unlink_owned_file(name, created_identity)
            _fail("secure_control_file_write_failed")
        if (
            not stat.S_ISREG(details.st_mode)
            or int(details.st_nlink) != 1
            or actual != payload
        ):
            try:
                handle.close()
            except BaseException:
                pass
            self._unlink_owned_file(name, created_identity)
            _fail("secure_control_file_identity_changed")
        pinned = PinnedControlFile(
            path=target,
            handle=handle,
            identity=_identity(details),
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        pinned.verify()
        self.files.append(pinned)
        self.owned_created_files.append((name, created_identity))
        return pinned

    def open_mutable_file(
        self,
        name: str,
        *,
        create_if_missing: bool,
        maximum_bytes: int,
        acquire_lock: bool = True,
        missing_ok: bool = False,
    ) -> PinnedMutableControlFile | None:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            _fail("secure_control_mutable_limit_invalid")
        target = self._path_for_name(name)
        for existing in self.mutable_files:
            if existing.path == target:
                if existing.maximum_bytes != maximum_bytes:
                    _fail("secure_control_mutable_limit_mismatch")
                existing.verify_binding()
                return existing
        try:
            handle = self._open_descriptor(
                name,
                create=False,
                open_or_create=create_if_missing,
                read_only=False,
                acquire_lock=acquire_lock,
            )
        except SecureControlFileError as exc:
            if missing_ok and str(exc) == "secure_control_file_missing":
                return None
            raise
        try:
            details = os.fstat(handle.fileno())
        except OSError:
            handle.close()
            _fail("secure_control_mutable_identity_unavailable")
        if (
            not stat.S_ISREG(details.st_mode)
            or int(details.st_nlink) != 1
            or int(details.st_size) > maximum_bytes
        ):
            handle.close()
            _fail("secure_control_mutable_identity_changed")
        pinned = PinnedMutableControlFile(
            path=target,
            handle=handle,
            identity=_identity(details),
            maximum_bytes=maximum_bytes,
        )
        pinned.verify_binding()
        self.mutable_files.append(pinned)
        return pinned

    def open_file(
        self, name: str, *, maximum_bytes: int = 1024 * 1024
    ) -> PinnedControlFile:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            _fail("secure_control_file_limit_invalid")
        target = self._path_for_name(name)
        handle = self._open_descriptor(name, create=False, read_only=True)
        try:
            details = os.fstat(handle.fileno())
            size, digest = _hash_handle(handle, maximum_bytes)
        except (OSError, SecureControlFileError):
            handle.close()
            _fail("secure_control_file_read_failed")
        if (
            not stat.S_ISREG(details.st_mode)
            or int(details.st_nlink) != 1
        ):
            handle.close()
            _fail("secure_control_file_identity_changed")
        pinned = PinnedControlFile(
            path=target,
            handle=handle,
            identity=_identity(details),
            expected_size=size,
            expected_sha256=digest,
        )
        pinned.verify()
        self.files.append(pinned)
        return pinned

    def verify(self) -> None:
        for component, expected, token in self.directory_tokens:
            try:
                path_stat = component.stat()
            except OSError:
                _fail("secure_control_directory_identity_changed")
            if is_reparse_point(component) or _identity(path_stat) != expected:
                _fail("secure_control_directory_identity_changed")
            if os.name == "nt":
                info = _windows_handle_info(token)
                if info["attributes"] & 0x00000400:
                    _fail("secure_control_directory_identity_changed")
                if _path_key(_windows_final_handle_path(token)) != _path_key(
                    component.resolve(strict=True)
                ):
                    _fail("secure_control_directory_identity_changed")
            elif _identity(os.fstat(token)) != expected:
                _fail("secure_control_directory_identity_changed")
        for pinned in self.files:
            pinned.verify()
        for pinned in self.mutable_files:
            pinned.verify_binding()

    def close(self, *, cleanup_owned: bool = False) -> None:
        failure: BaseException | None = None
        try:
            self.verify()
        except BaseException as exc:  # preserve verification failure after cleanup
            failure = exc
        for pinned in reversed(self.files):
            try:
                pinned.handle.close()
            except OSError as exc:
                failure = failure or exc
        for pinned in reversed(self.mutable_files):
            try:
                pinned.handle.close()
            except OSError as exc:
                failure = failure or exc
        if cleanup_owned:
            for name, identity in reversed(self.owned_created_files):
                try:
                    self._unlink_owned_file(name, identity)
                except BaseException as exc:
                    failure = failure or exc
        if cleanup_owned and self.created_leaf and len(self.directory_tokens) > 1:
            _leaf_component, _leaf_identity, leaf_token = self.directory_tokens.pop()
            try:
                if os.name == "nt":
                    _close_windows_handle(leaf_token)
                    self.path.rmdir()
                else:
                    os.close(leaf_token)
                    os.rmdir(
                        self.path.name,
                        dir_fd=self.directory_tokens[-1][2],
                    )
                    os.fsync(self.directory_tokens[-1][2])
            except BaseException as exc:
                failure = failure or exc
        for _component, _expected, token in reversed(self.directory_tokens):
            try:
                if os.name == "nt":
                    _close_windows_handle(token)
                else:
                    os.close(token)
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            if isinstance(failure, SecureControlFileError):
                raise failure
            _fail("secure_control_handle_close_failed")


def _relative_parts(anchor: Path, target: Path) -> tuple[str, ...]:
    try:
        relative = target.relative_to(anchor)
    except ValueError:
        _fail("secure_control_path_outside_anchor")
    if any(part in {"", ".", ".."} for part in relative.parts):
        _fail("secure_control_path_forbidden")
    return relative.parts


def _open_directory_token(path: Path) -> tuple[tuple[int, int], int]:
    if os.name == "nt":
        token = _windows_open_handle(path, directory=True)
        details = path.stat()
        info = _windows_handle_info(token)
        if info["attributes"] & 0x00000400:
            _close_windows_handle(token)
            _fail("secure_control_path_reparse_forbidden")
        return _identity(details), token
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        _fail("secure_control_nofollow_unavailable")
    try:
        token = os.open(path, os.O_RDONLY | directory_flag | nofollow)
        details = os.fstat(token)
    except OSError:
        _fail("secure_control_handle_open_failed")
    if not stat.S_ISDIR(details.st_mode):
        os.close(token)
        _fail("secure_control_directory_required")
    return _identity(details), token


@contextmanager
def pinned_control_directory(
    *,
    anchor: Path,
    path: Path,
    create_if_missing: bool,
) -> Iterator[PinnedControlDirectory]:
    """Pin ``anchor`` and every component through ``path``.

    A missing final component may be created exactly once.  All ancestors must
    already exist and be ordinary directories.
    """

    selected_anchor = Path(anchor).expanduser().absolute()
    selected_path = Path(path).expanduser().absolute()
    _reject_reparse_chain(selected_anchor)
    _reject_device_or_alias_syntax(selected_path)
    parts = _relative_parts(selected_anchor, selected_path)
    if not selected_anchor.is_dir() or is_reparse_point(selected_anchor):
        _fail("secure_control_anchor_unavailable")
    tokens: list[tuple[Path, tuple[int, int], int]] = []
    pin: PinnedControlDirectory | None = None
    created_leaf = False
    created_leaf_identity: tuple[int, int] | None = None
    body_succeeded = False
    try:
        identity, token = _open_directory_token(selected_anchor)
        tokens.append((selected_anchor, identity, token))
        current = selected_anchor
        for index, part in enumerate(parts):
            current = current / part
            is_leaf = index == len(parts) - 1
            if not current.exists():
                if not (create_if_missing and is_leaf):
                    _fail("secure_control_directory_unavailable")
                try:
                    if os.name == "nt":
                        current.mkdir(parents=False, exist_ok=False)
                    else:
                        os.mkdir(part, 0o700, dir_fd=tokens[-1][2])
                        os.fsync(tokens[-1][2])
                    created_leaf = True
                    created_leaf_identity = _identity(current.stat())
                except FileExistsError:
                    _fail("secure_control_directory_exists")
                except OSError:
                    _fail("secure_control_directory_create_failed")
            if not current.is_dir() or current.is_symlink() or is_reparse_point(current):
                _fail("secure_control_path_reparse_forbidden")
            if os.name == "nt":
                identity, token = _open_directory_token(current)
            else:
                nofollow = getattr(os, "O_NOFOLLOW", 0)
                directory_flag = getattr(os, "O_DIRECTORY", 0)
                try:
                    token = os.open(
                        part,
                        os.O_RDONLY | directory_flag | nofollow,
                        dir_fd=tokens[-1][2],
                    )
                    details = os.fstat(token)
                except OSError:
                    _fail("secure_control_handle_open_failed")
                if not stat.S_ISDIR(details.st_mode):
                    os.close(token)
                    _fail("secure_control_directory_required")
                identity = _identity(details)
            tokens.append((current, identity, token))
        if (
            created_leaf
            and created_leaf_identity is not None
            and tokens[-1][1] != created_leaf_identity
        ):
            _fail("secure_control_directory_identity_changed")
        pin = PinnedControlDirectory(
            anchor=selected_anchor,
            path=selected_path,
            directory_tokens=tokens,
            created_leaf=created_leaf,
        )
        pin.verify()
        yield pin
        body_succeeded = True
    finally:
        if pin is not None:
            pin.close(cleanup_owned=not body_succeeded)
        else:
            if created_leaf and created_leaf_identity is not None:
                try:
                    if (
                        len(tokens) > 1
                        and tokens[-1][0] == selected_path
                    ):
                        _component, _identity_value, leaf_token = tokens.pop()
                        if os.name == "nt":
                            _close_windows_handle(leaf_token)
                        else:
                            os.close(leaf_token)
                    details = selected_path.stat()
                    if (
                        is_reparse_point(selected_path)
                        or _identity(details) != created_leaf_identity
                        or any(selected_path.iterdir())
                    ):
                        _fail("secure_control_partial_cleanup_unsafe")
                    if os.name == "nt":
                        selected_path.rmdir()
                    elif tokens:
                        os.rmdir(
                            selected_path.name,
                            dir_fd=tokens[-1][2],
                        )
                        os.fsync(tokens[-1][2])
                except BaseException:
                    pass
            for _component, _expected, token in reversed(tokens):
                try:
                    if os.name == "nt":
                        _close_windows_handle(token)
                    else:
                        os.close(token)
                except BaseException:
                    pass


__all__ = [
    "PinnedControlDirectory",
    "PinnedControlFile",
    "PinnedMutableControlFile",
    "SecureControlFileError",
    "pinned_control_directory",
]
