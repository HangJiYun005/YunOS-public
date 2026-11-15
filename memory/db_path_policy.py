from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_AUTHORITY_BOUNDARY_VERSION = "v8.0-d2.1-fixed-production-authority-boundary-1"
DEFAULT_TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"
DEFAULT_REAL_DB_PATH = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
DEFAULT_SEMANTIC_SIDECAR_PATH = PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db"
DEFAULT_MEMORY_SIDECAR_PATH = PROJECT_ROOT / "data" / "memory" / "ayunos_memory.db"
DEFAULT_KNOWLEDGE_SIDECAR_PATH = PROJECT_ROOT / "data" / "knowledge" / "ayunos_knowledge.db"
DEFAULT_APPROVED_PROFILE_SIDECAR_PATH = PROJECT_ROOT / "data" / "profile" / "approved_profile.db"
DEFAULT_RUNTIME_SIDECAR_PATH = PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
DEFAULT_OPERATIONAL_STATE_PATH = (
    PROJECT_ROOT / "data" / "operational" / "ayunos_operational_state.db"
)
DEFAULT_REVIEW_LEDGER_PATH = PROJECT_ROOT / "data" / "review" / "ayunos_review_authority.db"
DEFAULT_REVIEW_AUTHORITY_REGISTRY_PATH = (
    PROJECT_ROOT / "data" / "review" / "ayunos_review_authority_registry.db"
)

def _fixed_production_database_targets() -> tuple[Path, ...]:
    """Build the nine code-defined production identities without an override seam."""
    root = Path(__file__).resolve().parents[1]
    return (
        root / "data" / "ayunos_chatgpt.db",
        root / "data" / "semantic" / "ayunos_semantic.db",
        root / "data" / "profile" / "approved_profile.db",
        root / "data" / "runtime" / "ayunos_runtime.db",
        root / "data" / "operational" / "ayunos_operational_state.db",
        root / "data" / "memory" / "ayunos_memory.db",
        root / "data" / "knowledge" / "ayunos_knowledge.db",
        root / "data" / "review" / "ayunos_review_authority.db",
        root / "data" / "review" / "ayunos_review_authority_registry.db",
    )


# Publicly inspectable inventory only. Formal validators rebuild the fixed
# identities above and do not read this name or accept a replacement policy.
PROTECTED_PRODUCTION_DATABASE_TARGETS = _fixed_production_database_targets()


class TestDatabasePathError(ValueError):
    """Raised when a semantic-memory write target is outside the controlled test root."""


class SemanticSidecarPathError(ValueError):
    """Raised when a production semantic sidecar target is not the exact approved file."""


class ControlledSummaryPathError(ValueError):
    """Raised when controlled-summary paths cross a production database boundary."""


class KnowledgeSidecarPathError(ValueError):
    """Raised when a knowledge-sidecar path crosses its read/write boundary."""


@dataclass(frozen=True)
class AliasSafeDatabaseTarget:
    """Immutable identity snapshot captured before SQLite opens a test target."""

    target: Path
    lexical_identity: str
    real_identity: str
    parent_real_identity: str
    parent_file_identity: tuple[int, int]
    existed: bool
    file_identity: tuple[int, int] | None


def validate_knowledge_test_db_target(
    db_path: str | Path,
) -> Path:
    """Allow writes only to the fixed policy's alias-safe temporary target."""
    return authorize_alias_safe_test_database_target(db_path).target


def authorize_alias_safe_test_database_target(
    db_path: str | Path,
    *,
    require_exists: bool | None = None,
) -> AliasSafeDatabaseTarget:
    """Capture a pre-open identity against the fixed production target inventory."""
    root = _absolute_path(tempfile.gettempdir())
    target = _absolute_path(db_path)
    if target == root or not _is_within(target, root):
        raise KnowledgeSidecarPathError(
            "Test databases require the system temporary directory"
        )
    if not target.parent.is_dir():
        raise KnowledgeSidecarPathError(
            "Test database parent directory must already exist"
        )
    try:
        _reject_reparse_components(root, target.parent)
        if _strict_path_exists(target):
            _reject_reparse_point(target)
    except (OSError, TestDatabasePathError) as exc:
        raise KnowledgeSidecarPathError(
            "Test databases reject linked or unverifiable paths"
        ) from exc

    root_real = _real_path_key(root)
    target_real = _real_path_key(target)
    parent_real = _real_path_key(target.parent)
    if target_real == root_real or not _key_is_within(target_real, root_real):
        raise KnowledgeSidecarPathError(
            "Test databases require the system temporary directory"
        )

    existed = _strict_path_exists(target)
    if require_exists is True and not existed:
        raise KnowledgeSidecarPathError("Test database must already exist")
    if require_exists is False and existed:
        raise KnowledgeSidecarPathError("Test database must not already exist")

    for protected in _fixed_production_database_targets():
        _reject_protected_database_alias(target, protected)

    return AliasSafeDatabaseTarget(
        target=target,
        lexical_identity=_path_key(target),
        real_identity=target_real,
        parent_real_identity=parent_real,
        parent_file_identity=_strict_file_identity(target.parent, directory=True),
        existed=existed,
        file_identity=(
            _strict_file_identity(target, directory=False) if existed else None
        ),
    )


def validate_opened_test_database_connection(
    conn: sqlite3.Connection,
    authorization: AliasSafeDatabaseTarget,
) -> Path:
    """Verify SQLite opened the exact file authorized before connection creation."""
    current = authorize_alias_safe_test_database_target(
        authorization.target,
        require_exists=True,
    )
    if current.parent_file_identity != authorization.parent_file_identity:
        raise KnowledgeSidecarPathError(
            "Test database parent identity changed before SQLite opened it"
        )
    if authorization.file_identity is None:
        raise KnowledgeSidecarPathError("Test database identity was not captured before open")
    if current.file_identity != authorization.file_identity:
        raise KnowledgeSidecarPathError(
            "Test database identity changed before SQLite opened it"
        )

    try:
        database_rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as exc:
        raise KnowledgeSidecarPathError(
            "Unable to inspect the SQLite database identity"
        ) from exc
    main_rows = [row for row in database_rows if len(row) >= 3 and row[1] == "main"]
    if len(main_rows) != 1 or not main_rows[0][2]:
        raise KnowledgeSidecarPathError(
            "SQLite did not report one persistent main database"
        )
    actual = _absolute_path(main_rows[0][2])
    actual_authorization = authorize_alias_safe_test_database_target(
        actual,
        require_exists=True,
    )
    try:
        same_file = os.path.samefile(actual, authorization.target)
    except OSError as exc:
        raise KnowledgeSidecarPathError(
            "Unable to compare the opened SQLite database identity"
        ) from exc
    if not same_file or actual_authorization.file_identity != authorization.file_identity:
        raise KnowledgeSidecarPathError(
            "SQLite opened a different database than the authorized target"
        )
    return actual


def connect_alias_safe_test_database(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Open an existing test database and reject pre/post-open replacement."""
    authorization = authorize_alias_safe_test_database_target(
        db_path,
        require_exists=True,
    )
    uri = f"file:{authorization.target.as_posix()}?mode=rw"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        validate_opened_test_database_connection(conn, authorization)
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        raise


@contextmanager
def create_alias_safe_test_database_connection(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
) -> Iterator[sqlite3.Connection]:
    """Exclusively create, post-open verify, and transactionally initialize a test DB."""
    before = authorize_alias_safe_test_database_target(
        db_path,
        require_exists=False,
    )
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    conn: sqlite3.Connection | None = None
    failed = True
    try:
        descriptor = os.open(before.target, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        created_identity = _identity_from_stat(os.fstat(descriptor))
        os.close(descriptor)
        descriptor = None
        created = authorize_alias_safe_test_database_target(
            before.target,
            require_exists=True,
        )
        if created.parent_file_identity != before.parent_file_identity:
            raise KnowledgeSidecarPathError(
                "Test database parent identity changed during creation"
            )
        if created.file_identity != created_identity:
            raise KnowledgeSidecarPathError(
                "Test database identity changed during creation"
            )
        uri = f"file:{created.target.as_posix()}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        validate_opened_test_database_connection(conn, created)
        yield conn
        failed = False
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if conn is not None:
            conn.close()
        if created_identity is not None and _target_has_identity(
            before.target,
            created_identity,
        ) and failed:
            before.target.unlink()


def validate_production_knowledge_sidecar(
    db_path: str | Path = DEFAULT_KNOWLEDGE_SIDECAR_PATH,
) -> Path:
    """Validate the fixed production knowledge identity for read-only inspection."""
    target = _absolute_path(db_path)
    expected = _absolute_path(DEFAULT_KNOWLEDGE_SIDECAR_PATH)
    if target != expected:
        raise KnowledgeSidecarPathError("Production knowledge inspection requires the fixed sidecar path")
    try:
        _reject_reparse_chain(target.parent)
        if target.exists() or target.is_symlink():
            _reject_reparse_point(target)
    except TestDatabasePathError as exc:
        raise KnowledgeSidecarPathError("Production knowledge sidecar rejects linked paths") from exc
    other_production = (
        _absolute_path(DEFAULT_REAL_DB_PATH),
        _absolute_path(DEFAULT_SEMANTIC_SIDECAR_PATH),
        _absolute_path(DEFAULT_MEMORY_SIDECAR_PATH),
    )
    if target.exists() and _aliases_any(target, other_production):
        raise KnowledgeSidecarPathError("Production knowledge sidecar must not identify another database")
    return target


def validate_memory_sidecar_target(
    db_path: str | Path,
    *,
    production_memory_path: str | Path | None = None,
    production_real_db_path: str | Path | None = None,
    production_semantic_path: str | Path | None = None,
) -> tuple[Path, bool]:
    """Validate an exact production target or an alias-free non-production target."""
    target = _absolute_path(db_path)
    memory_db = _absolute_path(production_memory_path or DEFAULT_MEMORY_SIDECAR_PATH)
    production_paths = _summary_production_paths(
        memory_db,
        production_real_db_path=production_real_db_path,
        production_semantic_path=production_semantic_path,
    )
    try:
        _reject_reparse_chain(target.parent)
        if target.exists() or target.is_symlink():
            _reject_reparse_point(target)
    except TestDatabasePathError as exc:
        raise ControlledSummaryPathError("Controlled summary paths reject linked locations") from exc

    production = target == memory_db
    if production and _aliases_any(target, production_paths[:2] + production_paths[3:]):
        raise ControlledSummaryPathError("Production memory sidecar must not identify another production database")
    if not production and _aliases_any(target, production_paths):
        raise ControlledSummaryPathError("Test sidecar must not identify a production database")
    return target, production


def validate_summary_acceptance_paths(
    source_db: str | Path,
    sidecar_path: str | Path,
    *,
    production_memory_path: str | Path | None = None,
    production_real_db_path: str | Path | None = None,
    production_semantic_path: str | Path | None = None,
) -> tuple[Path, Path, bool]:
    """Validate source/sidecar identity before any controlled-summary operation."""
    source = _absolute_path(source_db)
    real_db = _absolute_path(production_real_db_path or DEFAULT_REAL_DB_PATH)
    target, production = validate_memory_sidecar_target(
        sidecar_path,
        production_memory_path=production_memory_path,
        production_real_db_path=real_db,
        production_semantic_path=production_semantic_path,
    )
    memory_db = _absolute_path(production_memory_path or DEFAULT_MEMORY_SIDECAR_PATH)
    production_paths = _summary_production_paths(
        memory_db,
        production_real_db_path=real_db,
        production_semantic_path=production_semantic_path,
    )
    try:
        _reject_reparse_chain(source.parent)
        _reject_reparse_point(source)
    except TestDatabasePathError as exc:
        raise ControlledSummaryPathError("Controlled summary paths reject linked locations") from exc
    if not source.is_file():
        raise ControlledSummaryPathError("Summary source database must already exist")
    if production:
        if source != real_db:
            raise ControlledSummaryPathError("Production summary acceptance requires the approved read-only source")
    elif source in production_paths or _aliases_any(source, production_paths):
        raise ControlledSummaryPathError("Test source must not identify a production database")
    if target.exists() and _same_file(source, target):
        raise ControlledSummaryPathError("Summary source and sidecar must be different database files")
    return source, target, production


def validate_semantic_sidecar_write_target(
    db_path: str | Path,
    sidecar_path: str | Path | None = None,
    real_db_path: str | Path | None = None,
) -> Path:
    """Validate only the exact production semantic sidecar without changing test policy."""
    expected = _absolute_path(sidecar_path if sidecar_path is not None else DEFAULT_SEMANTIC_SIDECAR_PATH)
    target = _absolute_path(db_path)
    real_db = _absolute_path(real_db_path if real_db_path is not None else DEFAULT_REAL_DB_PATH)
    if target != expected:
        raise SemanticSidecarPathError("Production semantic writes require the approved sidecar database")
    if not target.parent.is_dir():
        raise SemanticSidecarPathError("Production semantic sidecar directory must already exist")
    try:
        _reject_reparse_point(target.parent.parent)
        _reject_reparse_point(target.parent)
        if target.exists() or target.is_symlink():
            _reject_reparse_point(target)
    except TestDatabasePathError as exc:
        raise SemanticSidecarPathError("Production semantic sidecar paths reject linked locations") from exc
    if target.exists() and _same_file(target, real_db):
        raise SemanticSidecarPathError("Refusing to write semantic embeddings to the real local database")
    if target.resolve(strict=False) != expected.resolve(strict=False):
        raise SemanticSidecarPathError("Production semantic writes require the approved sidecar database")
    return target


def validate_test_db_write_target(
    db_path: str | Path,
    test_root: str | Path | None = None,
    real_db_path: str | Path | None = None,
) -> Path:
    """Return a verified test database target without following path aliases."""
    root = _absolute_path(test_root if test_root is not None else DEFAULT_TEST_DB_ROOT)
    target = _absolute_path(db_path)
    real_db = _absolute_path(real_db_path if real_db_path is not None else DEFAULT_REAL_DB_PATH)

    if target == root or not _is_within(target, root):
        raise TestDatabasePathError("Semantic embedding writes require a controlled test database target")
    if not target.parent.is_dir():
        raise TestDatabasePathError("Semantic embedding write target parent directory must already exist")
    _reject_reparse_components(root, target.parent)
    if target.exists() or target.is_symlink():
        _reject_reparse_point(target)

    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_root or not _is_within(resolved_target, resolved_root):
        raise TestDatabasePathError("Semantic embedding writes require a controlled test database target")

    if target.exists() and _same_file(target, real_db):
        raise TestDatabasePathError("Refusing to write semantic embeddings to the real local database")
    return target


def is_reparse_point(path: str | Path) -> bool:
    """Detect symlinks and Windows reparse points without resolving them."""
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(file_stat, "st_file_attributes", 0) & reparse_flag)


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return Path(os.path.abspath(path))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _reject_protected_database_alias(
    target_value: str | Path,
    protected_value: str | Path,
) -> None:
    """Pure one-target identity check used by the fixed validator and test harness."""
    target = _absolute_path(target_value)
    protected = _absolute_path(protected_value)
    if _path_key(target) == _path_key(protected) or _real_path_key(target) == _real_path_key(
        protected
    ):
        raise KnowledgeSidecarPathError(
            "Test database must not identify a production database"
        )
    if _strict_path_exists(protected.parent):
        try:
            if os.path.samefile(target.parent, protected.parent):
                raise KnowledgeSidecarPathError(
                    "Test database parent must not identify a production directory"
                )
        except KnowledgeSidecarPathError:
            raise
        except OSError as exc:
            raise KnowledgeSidecarPathError(
                "Unable to verify production directory identity"
            ) from exc
    if _strict_path_exists(target) and _strict_path_exists(protected):
        try:
            if os.path.samefile(target, protected):
                raise KnowledgeSidecarPathError(
                    "Test database must not identify a production database"
                )
        except KnowledgeSidecarPathError:
            raise
        except OSError as exc:
            raise KnowledgeSidecarPathError(
                "Unable to verify production database identity"
            ) from exc


def _real_path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _key_is_within(path_key: str, root_key: str) -> bool:
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _identity_from_stat(file_stat: os.stat_result) -> tuple[int, int]:
    identity = (int(file_stat.st_dev), int(file_stat.st_ino))
    if identity[1] == 0:
        raise KnowledgeSidecarPathError("Filesystem did not provide a reliable file identity")
    return identity


def _strict_file_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    try:
        file_stat = os.stat(path, follow_symlinks=True)
    except OSError as exc:
        raise KnowledgeSidecarPathError("Unable to read filesystem identity") from exc
    expected = stat.S_ISDIR(file_stat.st_mode) if directory else stat.S_ISREG(file_stat.st_mode)
    if not expected:
        raise KnowledgeSidecarPathError("Database target has an unexpected file type")
    return _identity_from_stat(file_stat)


def _strict_path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KnowledgeSidecarPathError("Unable to determine filesystem identity") from exc
    return True


def _target_has_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return _strict_file_identity(path, directory=False) == identity
    except KnowledgeSidecarPathError:
        return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_reparse_components(root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise TestDatabasePathError("Semantic embedding writes require a controlled test database target") from exc

    current = root
    _reject_reparse_point(current)
    for part in relative.parts:
        current = current / part
        _reject_reparse_point(current)


def _reject_reparse_point(path: Path) -> None:
    if is_reparse_point(path):
        raise TestDatabasePathError("Semantic embedding writes reject linked test database paths")


def _reject_reparse_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            _reject_reparse_point(current)


def _summary_production_paths(
    memory_db: Path,
    *,
    production_real_db_path: str | Path | None,
    production_semantic_path: str | Path | None,
) -> tuple[Path, ...]:
    return (
        _absolute_path(production_real_db_path or DEFAULT_REAL_DB_PATH),
        _absolute_path(production_semantic_path or DEFAULT_SEMANTIC_SIDECAR_PATH),
        memory_db,
        _absolute_path(DEFAULT_KNOWLEDGE_SIDECAR_PATH),
    )


def _aliases_any(target: Path, protected: tuple[Path, ...]) -> bool:
    for path in protected:
        if target.exists() and _same_file(target, path):
            return True
        if (
            not target.exists()
            and target.name.casefold() == path.name.casefold()
            and target.parent.exists()
            and path.parent.exists()
            and _same_file(target.parent, path.parent)
        ):
            return True
    return False


def _same_file(first: Path, second: Path) -> bool:
    if not second.exists():
        return False
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False
