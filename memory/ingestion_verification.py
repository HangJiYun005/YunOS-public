"""Post-commit verification models for YunOS v8.3-E test execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from memory.ingestion_core import (
    AssetFingerprintItem,
    AssetStageFingerprint,
    build_asset_stage_fingerprint,
    normalized_json_digest,
)


TEST_EXECUTION_SCHEMA_VERSION = "v8.3-e-test-target-1"
VERIFICATION_EXPECTATION_VERSION = "v8.3-e-verification-expectation-1"
EXECUTION_VERIFICATION_VERSION = "v8.3-e-execution-verification-1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")

COUNT_KEYS = (
    "conversations",
    "messages",
    "attachments",
    "entities",
    "source_bindings",
)


class IngestionVerificationError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IngestionVerificationError(reason)
    return value


def _identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise IngestionVerificationError(reason)
    return value


def _token(value: str, reason: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise IngestionVerificationError(reason)
    return value


def _counts(value: Mapping[str, int], reason: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise IngestionVerificationError(reason)
    result: dict[str, int] = {}
    for key, count in value.items():
        _token(key, reason)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise IngestionVerificationError(reason)
        result[key] = count
    return dict(sorted(result.items()))


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class TestTargetSnapshot:
    target_fingerprint: str
    target_identity_digest: str
    database_content_sha256: str
    database_size: int
    schema_digest: str
    logical_digest: str
    counts: Mapping[str, int]
    identity_sets: Mapping[str, frozenset[str]] = field(repr=False)
    logical_rows: Mapping[str, tuple[tuple[Any, ...], ...]] = field(repr=False)
    asset_stage_fingerprint: AssetStageFingerprint
    asset_digests: tuple[str, ...]
    integrity_check: str
    foreign_key_violations: int
    orphan_messages: int
    orphan_attachments: int
    duplicate_source_bindings: int
    schema_version: str = TEST_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, reason in (
            (self.target_fingerprint, "invalid_target_fingerprint"),
            (self.target_identity_digest, "invalid_target_identity_digest"),
            (self.database_content_sha256, "invalid_database_digest"),
            (self.schema_digest, "invalid_schema_digest"),
            (self.logical_digest, "invalid_logical_digest"),
        ):
            _sha(value, reason)
        if (
            isinstance(self.database_size, bool)
            or not isinstance(self.database_size, int)
            or self.database_size < 0
        ):
            raise IngestionVerificationError("invalid_database_size")
        normalized_counts = _counts(self.counts, "invalid_target_counts")
        if set(normalized_counts) != set(COUNT_KEYS):
            raise IngestionVerificationError("invalid_target_counts")
        object.__setattr__(
            self, "counts", MappingProxyType(normalized_counts)
        )
        identities: dict[str, frozenset[str]] = {}
        for table, values in self.identity_sets.items():
            _token(table, "invalid_identity_table")
            identities[table] = frozenset(str(value) for value in values)
        object.__setattr__(
            self, "identity_sets", MappingProxyType(dict(sorted(identities.items())))
        )
        rows: dict[str, tuple[tuple[Any, ...], ...]] = {}
        for table, table_rows in self.logical_rows.items():
            _token(table, "invalid_logical_table")
            rows[table] = tuple(tuple(row) for row in table_rows)
        object.__setattr__(
            self,
            "logical_rows",
            MappingProxyType(dict(sorted(rows.items()))),
        )
        if not isinstance(self.asset_stage_fingerprint, AssetStageFingerprint):
            raise IngestionVerificationError("invalid_asset_fingerprint")
        digests = tuple(sorted(set(self.asset_digests)))
        for digest in digests:
            _sha(digest, "invalid_asset_digest")
        object.__setattr__(self, "asset_digests", digests)
        if self.integrity_check != "ok":
            raise IngestionVerificationError("database_integrity_failed")
        for value in (
            self.foreign_key_violations,
            self.orphan_messages,
            self.orphan_attachments,
            self.duplicate_source_bindings,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IngestionVerificationError("invalid_invariant_count")
        if self.schema_version != TEST_EXECUTION_SCHEMA_VERSION:
            raise IngestionVerificationError("test_target_schema_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_fingerprint": self.target_fingerprint,
            "target_identity_digest": self.target_identity_digest,
            "database_content_sha256": self.database_content_sha256,
            "database_size": self.database_size,
            "schema_digest": self.schema_digest,
            "logical_digest": self.logical_digest,
            "counts": dict(self.counts),
            "identity_digests": {
                table: normalized_json_digest(sorted(values))
                for table, values in self.identity_sets.items()
            },
            "asset_stage_fingerprint": self.asset_stage_fingerprint.to_dict(),
            "asset_digests": list(self.asset_digests),
            "integrity_check": self.integrity_check,
            "foreign_key_violations": self.foreign_key_violations,
            "orphan_messages": self.orphan_messages,
            "orphan_attachments": self.orphan_attachments,
            "duplicate_source_bindings": self.duplicate_source_bindings,
            "schema_version": self.schema_version,
        }


def _asset_snapshot(asset_root: Path) -> tuple[AssetStageFingerprint, tuple[str, ...]]:
    final_root = asset_root / "final"
    if not final_root.exists():
        return build_asset_stage_fingerprint(()), ()
    if not final_root.is_dir() or final_root.is_symlink():
        raise IngestionVerificationError("asset_target_unsafe")
    items: list[AssetFingerprintItem] = []
    digests: list[str] = []
    for path in sorted(final_root.iterdir(), key=lambda item: item.name):
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.name.startswith("sha256_")
        ):
            raise IngestionVerificationError("unexpected_asset_target_entry")
        name_digest = path.name[7:]
        _sha(name_digest, "invalid_asset_filename")
        actual_digest, size = _hash_file(path)
        if actual_digest != name_digest:
            raise IngestionVerificationError("asset_target_digest_mismatch")
        digests.append(actual_digest)
        items.append(AssetFingerprintItem(
            logical_key_digest=normalized_json_digest(
                {"test_asset_entity": actual_digest}
            ),
            content_sha256=actual_digest,
            size_bytes=size,
        ))
    return build_asset_stage_fingerprint(items), tuple(digests)


def capture_test_target_snapshot(
    database_path: str | Path,
    asset_root: str | Path,
) -> TestTargetSnapshot:
    database = Path(database_path)
    assets = Path(asset_root)
    if not database.is_file() or database.is_symlink():
        raise IngestionVerificationError("test_database_missing")
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        try:
            schema_row = connection.execute(
                "SELECT schema_version,target_id FROM execution_schema"
            ).fetchone()
        except sqlite3.DatabaseError:
            raise IngestionVerificationError(
                "production_execution_schema_missing"
            ) from None
        if schema_row is None or schema_row["schema_version"] != TEST_EXECUTION_SCHEMA_VERSION:
            raise IngestionVerificationError("test_target_schema_mismatch")
        target_id = str(schema_row["target_id"])
        if _IDENTITY_RE.fullmatch(target_id) is None:
            raise IngestionVerificationError("invalid_test_target_identity")
        target_identity_digest = normalized_json_digest(
            {"test_target_id": target_id}
        )
        tables = [
            row["name"]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        schema_rows = {
            table: [
                tuple(row)
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for table in tables
        }
        table_map = {
            "conversations": ("conversations", "conversation_id"),
            "messages": ("messages", "message_id"),
            "attachments": ("attachments", "attachment_id"),
            "entities": ("asset_entities", "entity_digest"),
            "source_bindings": ("source_bindings", "source_identity"),
        }
        counts = {
            key: connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for key, (table, _) in table_map.items()
        }
        identity_sets = {
            table: frozenset(
                str(row[0])
                for row in connection.execute(
                    f'SELECT "{column}" FROM "{table}" ORDER BY "{column}"'
                )
            )
            for table, column in (
                ("conversations", "conversation_id"),
                ("messages", "message_id"),
                ("attachments", "attachment_id"),
                ("asset_entities", "entity_digest"),
                ("source_bindings", "source_identity"),
            )
        }
        logical_rows = {
            table: sorted(
                [
                    tuple(row)
                    for row in connection.execute(f'SELECT * FROM "{table}"')
                ],
                key=lambda row: tuple(
                    "" if value is None else str(value) for value in row
                ),
            )
            for table in (
                "conversations",
                "messages",
                "attachments",
                "asset_entities",
                "source_bindings",
                "execution_applied_operations",
                "execution_conflict_resolutions",
            )
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        orphan_messages = connection.execute(
            """SELECT COUNT(*) FROM messages m
               LEFT JOIN conversations c
                 ON c.conversation_id=m.conversation_id
               WHERE c.conversation_id IS NULL"""
        ).fetchone()[0]
        orphan_attachments = connection.execute(
            """SELECT COUNT(*) FROM attachments a
               LEFT JOIN messages m ON m.message_id=a.message_id
               WHERE m.message_id IS NULL"""
        ).fetchone()[0]
        duplicate_bindings = connection.execute(
            """SELECT COUNT(*) FROM (
                 SELECT source_type,source_identity,COUNT(*) AS n
                 FROM source_bindings
                 GROUP BY source_type,source_identity
                 HAVING n > 1
               )"""
        ).fetchone()[0]
    finally:
        connection.close()
    content_digest, size = _hash_file(database)
    asset_fingerprint, asset_digests = _asset_snapshot(assets)
    schema_digest = normalized_json_digest({
        "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
        "tables": schema_rows,
    })
    logical_digest = normalized_json_digest({
        "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
        "rows": logical_rows,
    })
    payload = {
        "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
        "target_identity_digest": target_identity_digest,
        "database_content_sha256": content_digest,
        "database_size": size,
        "schema_digest": schema_digest,
        "logical_digest": logical_digest,
        "counts": counts,
        "asset_stage_fingerprint": asset_fingerprint.to_dict(),
    }
    return TestTargetSnapshot(
        target_fingerprint=normalized_json_digest(payload),
        target_identity_digest=target_identity_digest,
        database_content_sha256=content_digest,
        database_size=size,
        schema_digest=schema_digest,
        logical_digest=logical_digest,
        counts=counts,
        identity_sets=identity_sets,
        logical_rows={
            table: tuple(rows) for table, rows in logical_rows.items()
        },
        asset_stage_fingerprint=asset_fingerprint,
        asset_digests=asset_digests,
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        orphan_messages=orphan_messages,
        orphan_attachments=orphan_attachments,
        duplicate_source_bindings=duplicate_bindings,
    )


@dataclass(frozen=True)
class VerificationExpectation:
    expected_deltas: Mapping[str, int]
    approved_operation_ids: tuple[str, ...]
    rejected_operation_ids: tuple[str, ...]
    expected_conflict_resolutions: int
    database_before_fingerprint: str
    asset_before_fingerprint: str
    expected_after_logical_digest: str
    expected_asset_digests: tuple[str, ...]
    expected_identity_additions: Mapping[str, frozenset[str]]
    expectation_digest: str
    expectation_schema_version: str = VERIFICATION_EXPECTATION_VERSION

    def __post_init__(self) -> None:
        deltas = _counts(self.expected_deltas, "invalid_expected_deltas")
        if set(deltas) != set(COUNT_KEYS):
            raise IngestionVerificationError("invalid_expected_deltas")
        object.__setattr__(self, "expected_deltas", MappingProxyType(deltas))
        approved = tuple(sorted(set(self.approved_operation_ids)))
        rejected = tuple(sorted(set(self.rejected_operation_ids)))
        for operation_id in (*approved, *rejected):
            _identity(operation_id, "invalid_operation_id")
        if set(approved).intersection(rejected):
            raise IngestionVerificationError("verification_operation_overlap")
        object.__setattr__(self, "approved_operation_ids", approved)
        object.__setattr__(self, "rejected_operation_ids", rejected)
        if (
            isinstance(self.expected_conflict_resolutions, bool)
            or not isinstance(self.expected_conflict_resolutions, int)
            or self.expected_conflict_resolutions < 0
        ):
            raise IngestionVerificationError("invalid_conflict_resolution_count")
        _sha(
            self.database_before_fingerprint,
            "invalid_database_before_fingerprint",
        )
        _sha(self.asset_before_fingerprint, "invalid_asset_before_fingerprint")
        _sha(
            self.expected_after_logical_digest,
            "invalid_expected_after_logical_digest",
        )
        asset_digests = tuple(sorted(set(self.expected_asset_digests)))
        for digest in asset_digests:
            _sha(digest, "invalid_expected_asset_digest")
        object.__setattr__(self, "expected_asset_digests", asset_digests)
        additions: dict[str, frozenset[str]] = {}
        for table, identities in self.expected_identity_additions.items():
            _token(table, "invalid_expected_identity_table")
            additions[table] = frozenset(str(item) for item in identities)
        object.__setattr__(
            self,
            "expected_identity_additions",
            MappingProxyType(dict(sorted(additions.items()))),
        )
        _sha(self.expectation_digest, "invalid_expectation_digest")
        if self.expectation_schema_version != VERIFICATION_EXPECTATION_VERSION:
            raise IngestionVerificationError("expectation_schema_version_mismatch")
        if self.expectation_digest != normalized_json_digest(
            self.identity_payload()
        ):
            raise IngestionVerificationError("expectation_digest_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "expected_deltas": dict(self.expected_deltas),
            "approved_operation_ids": list(self.approved_operation_ids),
            "rejected_operation_ids": list(self.rejected_operation_ids),
            "expected_conflict_resolutions": self.expected_conflict_resolutions,
            "database_before_fingerprint": self.database_before_fingerprint,
            "asset_before_fingerprint": self.asset_before_fingerprint,
            "expected_after_logical_digest": self.expected_after_logical_digest,
            "expected_asset_digests": list(self.expected_asset_digests),
            "expected_identity_additions": {
                table: sorted(values)
                for table, values in self.expected_identity_additions.items()
            },
            "expectation_schema_version": self.expectation_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "expectation_digest": self.expectation_digest,
        }


def make_verification_expectation(
    *,
    expected_deltas: Mapping[str, int],
    approved_operation_ids: Sequence[str],
    rejected_operation_ids: Sequence[str],
    expected_conflict_resolutions: int,
    before_snapshot: TestTargetSnapshot,
    expected_after_logical_digest: str | None = None,
    approved_asset_digests: Sequence[str] = (),
    expected_identity_additions: Mapping[str, Iterable[str]] | None = None,
) -> VerificationExpectation:
    payload = {
        "expected_deltas": dict(expected_deltas),
        "approved_operation_ids": sorted(set(approved_operation_ids)),
        "rejected_operation_ids": sorted(set(rejected_operation_ids)),
        "expected_conflict_resolutions": expected_conflict_resolutions,
        "database_before_fingerprint": before_snapshot.target_fingerprint,
        "asset_before_fingerprint": normalized_json_digest(
            before_snapshot.asset_stage_fingerprint.to_dict()
        ),
        "expected_after_logical_digest": (
            expected_after_logical_digest or before_snapshot.logical_digest
        ),
        "expected_asset_digests": sorted(
            set(before_snapshot.asset_digests).union(approved_asset_digests)
        ),
        "expected_identity_additions": {
            table: sorted(set(values))
            for table, values in sorted(
                (expected_identity_additions or {}).items()
            )
        },
        "expectation_schema_version": VERIFICATION_EXPECTATION_VERSION,
    }
    return VerificationExpectation(
        expectation_digest=normalized_json_digest(payload),
        **payload,
    )


@dataclass(frozen=True)
class ExecutionVerificationResult:
    verification_id: str
    execution_id: str
    passed: bool
    invariant_results: Mapping[str, bool]
    actual_deltas: Mapping[str, int]
    expected_deltas: Mapping[str, int]
    database_after_fingerprint: str
    asset_after_fingerprint: str
    verification_digest: str
    verification_schema_version: str = EXECUTION_VERIFICATION_VERSION

    def __post_init__(self) -> None:
        _identity(self.verification_id, "invalid_verification_id")
        _identity(self.execution_id, "invalid_execution_id")
        if not isinstance(self.passed, bool):
            raise IngestionVerificationError("invalid_verification_result")
        invariants: dict[str, bool] = {}
        for key, value in self.invariant_results.items():
            _token(key, "invalid_invariant_name")
            if not isinstance(value, bool):
                raise IngestionVerificationError("invalid_invariant_value")
            invariants[key] = value
        object.__setattr__(
            self, "invariant_results", MappingProxyType(dict(sorted(invariants.items())))
        )
        object.__setattr__(
            self,
            "actual_deltas",
            MappingProxyType(_counts(self.actual_deltas, "invalid_actual_deltas")),
        )
        object.__setattr__(
            self,
            "expected_deltas",
            MappingProxyType(_counts(self.expected_deltas, "invalid_expected_deltas")),
        )
        _sha(
            self.database_after_fingerprint,
            "invalid_database_after_fingerprint",
        )
        _sha(self.asset_after_fingerprint, "invalid_asset_after_fingerprint")
        _sha(self.verification_digest, "invalid_verification_digest")
        if self.verification_schema_version != EXECUTION_VERIFICATION_VERSION:
            raise IngestionVerificationError("verification_schema_version_mismatch")
        expected = normalized_json_digest(self.identity_payload())
        if self.verification_digest != expected:
            raise IngestionVerificationError("verification_digest_mismatch")
        if self.verification_id != "execverify_" + expected:
            raise IngestionVerificationError("verification_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "passed": self.passed,
            "invariant_results": dict(self.invariant_results),
            "actual_deltas": dict(self.actual_deltas),
            "expected_deltas": dict(self.expected_deltas),
            "database_after_fingerprint": self.database_after_fingerprint,
            "asset_after_fingerprint": self.asset_after_fingerprint,
            "verification_schema_version": self.verification_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            **self.identity_payload(),
            "verification_digest": self.verification_digest,
        }


def verify_test_execution(
    *,
    execution_id: str,
    database_path: str | Path,
    asset_root: str | Path,
    before_snapshot: TestTargetSnapshot,
    expectation: VerificationExpectation,
    executed_operation_ids: Iterable[str],
    resolved_conflict_count: int,
    verification_delegate: Any | None = None,
) -> tuple[TestTargetSnapshot, ExecutionVerificationResult]:
    if verification_delegate is not None:
        return verification_delegate(
            execution_id=execution_id,
            database_path=database_path,
            asset_root=asset_root,
            before_snapshot=before_snapshot,
            expectation=expectation,
            executed_operation_ids=executed_operation_ids,
            resolved_conflict_count=resolved_conflict_count,
        )
    after = capture_test_target_snapshot(database_path, asset_root)
    actual_deltas = {
        key: after.counts[key] - before_snapshot.counts[key]
        for key in COUNT_KEYS
    }
    executed = set(executed_operation_ids)
    approved = set(expectation.approved_operation_ids)
    rejected = set(expectation.rejected_operation_ids)
    identities_preserved = all(
        before_snapshot.identity_sets.get(table, frozenset()).issubset(values)
        for table, values in after.identity_sets.items()
    )
    invariants = {
        "expected_deltas_match": actual_deltas == dict(expectation.expected_deltas),
        "database_before_fingerprint_bound": (
            expectation.database_before_fingerprint
            == before_snapshot.target_fingerprint
        ),
        "asset_before_fingerprint_bound": (
            expectation.asset_before_fingerprint
            == normalized_json_digest(
                before_snapshot.asset_stage_fingerprint.to_dict()
            )
        ),
        "existing_ids_not_rewritten": identities_preserved,
        "logical_rows_exact": (
            after.logical_digest == expectation.expected_after_logical_digest
        ),
        "identity_sets_exact": all(
            after.identity_sets.get(table, frozenset())
            == before_snapshot.identity_sets.get(table, frozenset()).union(
                expectation.expected_identity_additions.get(
                    table, frozenset()
                )
            )
            for table in after.identity_sets
        ),
        "asset_set_exact": (
            set(after.asset_digests)
            == set(expectation.expected_asset_digests)
        ),
        "no_orphan_messages": after.orphan_messages == 0,
        "no_orphan_attachments": after.orphan_attachments == 0,
        "foreign_key_check_ok": after.foreign_key_violations == 0,
        "source_bindings_unique": after.duplicate_source_bindings == 0,
        "entity_digests_unique": (
            after.counts["entities"] == len(after.identity_sets["asset_entities"])
        ),
        "approved_operations_complete": approved.issubset(executed),
        "rejected_operations_absent": not rejected.intersection(executed),
        "no_unapproved_operations": executed.issubset(approved),
        "conflict_resolution_count_matches": (
            resolved_conflict_count
            == expectation.expected_conflict_resolutions
        ),
    }
    passed = all(invariants.values())
    payload = {
        "execution_id": execution_id,
        "passed": passed,
        "invariant_results": dict(sorted(invariants.items())),
        "actual_deltas": actual_deltas,
        "expected_deltas": dict(expectation.expected_deltas),
        "database_after_fingerprint": after.target_fingerprint,
        "asset_after_fingerprint": normalized_json_digest(
            after.asset_stage_fingerprint.to_dict()
        ),
        "verification_schema_version": EXECUTION_VERIFICATION_VERSION,
    }
    digest = normalized_json_digest(payload)
    return after, ExecutionVerificationResult(
        verification_id="execverify_" + digest,
        verification_digest=digest,
        **payload,
    )
