from __future__ import annotations

import base64
import hashlib
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import unicodedata
from copy import deepcopy

from memory.sqlite_snapshot import (
    PRODUCTION_PROFILE_SIDECAR,
    PRODUCTION_PROFILE_SNAPSHOT_TABLES,
    SNAPSHOT_PROTOCOL_VERSION,
    SQLiteSnapshotError,
    canonical_snapshot_bytes,
    canonical_sqlite_value,
    snapshot_sqlite_database,
    verify_preexisting_rows_unchanged,
)


class SQLiteValueEncodingTest(unittest.TestCase):
    def test_blob_all_byte_values_round_trips_exactly(self) -> None:
        raw = bytes(range(256))
        encoded = canonical_sqlite_value(raw)
        self.assertEqual("blob", encoded["sqlite_type"])
        self.assertEqual(raw, base64.b64decode(encoded["base64"], validate=True))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), encoded["sha256"])
        self.assertEqual(256, encoded["length"])
        canonical_snapshot_bytes(encoded).decode("ascii")

    def test_empty_and_ascii_blob_remain_distinct_from_text(self) -> None:
        empty_blob = canonical_sqlite_value(b"")
        empty_text = canonical_sqlite_value("")
        ascii_blob = canonical_sqlite_value(b"abc")
        ascii_text = canonical_sqlite_value("abc")
        self.assertNotEqual(canonical_snapshot_bytes(empty_blob), canonical_snapshot_bytes(empty_text))
        self.assertNotEqual(canonical_snapshot_bytes(ascii_blob), canonical_snapshot_bytes(ascii_text))

    def test_chinese_text_binds_utf8_hash_and_code_points_without_normalization(self) -> None:
        value = "中文，保持原始码点。"
        encoded = canonical_sqlite_value(value)
        raw = value.encode("utf-8")
        self.assertEqual(raw, base64.b64decode(encoded["utf8_base64"], validate=True))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), encoded["utf8_sha256"])
        self.assertEqual([ord(char) for char in value], encoded["code_points"])

        nfc = unicodedata.normalize("NFC", "e\u0301")
        nfd = unicodedata.normalize("NFD", "é")
        self.assertNotEqual(nfc, nfd)
        self.assertNotEqual(
            canonical_snapshot_bytes(canonical_sqlite_value(nfc)),
            canonical_snapshot_bytes(canonical_sqlite_value(nfd)),
        )

    def test_invalid_utf8_blob_and_buffer_types_are_lossless(self) -> None:
        raw = b"\xff\xfe\x80\x00"
        expected = canonical_snapshot_bytes(canonical_sqlite_value(raw))
        self.assertEqual(expected, canonical_snapshot_bytes(canonical_sqlite_value(bytearray(raw))))
        self.assertEqual(expected, canonical_snapshot_bytes(canonical_sqlite_value(memoryview(raw))))

    def test_null_integer_negative_and_real_are_stable(self) -> None:
        values = [None, 0, -9223372036854775808, 9223372036854775807, -1.25, -0.0]
        first = [canonical_snapshot_bytes(canonical_sqlite_value(value)) for value in values]
        second = [canonical_snapshot_bytes(canonical_sqlite_value(value)) for value in values]
        self.assertEqual(first, second)
        self.assertEqual("null", canonical_sqlite_value(None)["sqlite_type"])
        self.assertEqual("-9223372036854775808", canonical_sqlite_value(values[2])["decimal"])
        self.assertNotEqual(
            canonical_sqlite_value(0.0)["ieee754_binary64_hex"],
            canonical_sqlite_value(-0.0)["ieee754_binary64_hex"],
        )

    def test_non_finite_real_and_ambiguous_values_fail_closed(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(SQLiteSnapshotError):
                canonical_sqlite_value(value)
        with self.assertRaises(SQLiteSnapshotError):
            canonical_sqlite_value(True)
        with self.assertRaises(SQLiteSnapshotError):
            canonical_sqlite_value(2**63)
        with self.assertRaises(SQLiteSnapshotError):
            canonical_sqlite_value("abc", sqlite_type="blob")


class SQLiteDatabaseSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _database(self, name: str, rows: list[tuple[int, str, bytes]]) -> Path:
        path = self.root / name
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE items(id INTEGER PRIMARY KEY, text_value TEXT NOT NULL, payload BLOB NOT NULL)"
            )
            conn.executemany("INSERT INTO items VALUES(?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
        return path

    def test_logical_primary_key_order_ignores_insert_order(self) -> None:
        rows = [(1, "一", b"a"), (2, "二", b"b"), (3, "三", b"c")]
        first = snapshot_sqlite_database(self._database("first.db", rows))
        second = snapshot_sqlite_database(self._database("second.db", list(reversed(rows))))
        self.assertEqual(SNAPSHOT_PROTOCOL_VERSION, first["snapshot_protocol_version"])
        self.assertEqual(first["snapshot_identity"], second["snapshot_identity"])
        self.assertEqual(first["tables"][0]["table_identity"], second["tables"][0]["table_identity"])

    def test_nullable_primary_key_component_fails_before_snapshot_identity(self) -> None:
        for name, rows in (
            ("single-null.db", [(None, "a", "one")]),
            ("distinct-null.db", [(None, "a", "one"), (None, "b", "two")]),
        ):
            path = self.root / name
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE items(a TEXT,b TEXT,value TEXT,PRIMARY KEY(a,b))"
                )
                conn.executemany("INSERT INTO items VALUES(?,?,?)", rows)
                conn.commit()
            finally:
                conn.close()
            with self.subTest(name=name), self.assertRaises(SQLiteSnapshotError):
                snapshot_sqlite_database(path)

    def test_repeated_snapshot_is_byte_for_byte_deterministic(self) -> None:
        path = self._database("repeat.db", [(1, "中文", bytes(range(32)))])
        first = snapshot_sqlite_database(path)
        second = snapshot_sqlite_database(path)
        self.assertEqual(canonical_snapshot_bytes(first), canonical_snapshot_bytes(second))

    def test_cell_change_changes_row_table_and_database_identity(self) -> None:
        path = self._database("changed.db", [(1, "before", b"payload")])
        before = snapshot_sqlite_database(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("UPDATE items SET text_value='after' WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        after = snapshot_sqlite_database(path)
        self.assertNotEqual(before["tables"][0]["rows"][0]["row_identity"], after["tables"][0]["rows"][0]["row_identity"])
        self.assertNotEqual(before["tables"][0]["table_identity"], after["tables"][0]["table_identity"])
        self.assertNotEqual(before["snapshot_identity"], after["snapshot_identity"])
        with self.assertRaises(SQLiteSnapshotError):
            verify_preexisting_rows_unchanged(before, after)

    def test_preexisting_row_verification_accepts_only_additive_rows(self) -> None:
        path = self._database("append.db", [(1, "one", b"1")])
        before = snapshot_sqlite_database(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("INSERT INTO items VALUES(2,'two',?)", (b"2",))
            conn.commit()
        finally:
            conn.close()
        after = snapshot_sqlite_database(path)
        result = verify_preexisting_rows_unchanged(before, after)
        self.assertEqual("preexisting_rows_unchanged", result["status"])
        self.assertEqual(1, result["preexisting_rows_verified"])

    def test_preexisting_row_verification_rejects_table_inventory_changes(self) -> None:
        added_path = self._database("add-table.db", [(1, "one", b"1")])
        before_add = snapshot_sqlite_database(added_path)
        conn = sqlite3.connect(added_path)
        try:
            conn.execute("CREATE TABLE extra(id INTEGER PRIMARY KEY,value TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SQLiteSnapshotError):
            verify_preexisting_rows_unchanged(
                before_add, snapshot_sqlite_database(added_path),
            )

        removed_path = self.root / "remove-table.db"
        conn = sqlite3.connect(removed_path)
        try:
            conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,value TEXT)")
            conn.execute("CREATE TABLE extra(id INTEGER PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO items VALUES(1,'one')")
            conn.commit()
        finally:
            conn.close()
        before_remove = snapshot_sqlite_database(removed_path)
        conn = sqlite3.connect(removed_path)
        try:
            conn.execute("DROP TABLE extra")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SQLiteSnapshotError):
            verify_preexisting_rows_unchanged(
                before_remove, snapshot_sqlite_database(removed_path),
            )

    def test_preexisting_row_verification_rejects_schema_and_row_drift(self) -> None:
        schema_path = self._database("schema-change.db", [(1, "one", b"1")])
        before_schema = snapshot_sqlite_database(schema_path)
        conn = sqlite3.connect(schema_path)
        try:
            conn.execute("CREATE INDEX items_text_idx ON items(text_value)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SQLiteSnapshotError):
            verify_preexisting_rows_unchanged(
                before_schema, snapshot_sqlite_database(schema_path),
            )

        for name, statement in (
            ("modified", "UPDATE items SET text_value='changed' WHERE id=1"),
            ("deleted", "DELETE FROM items WHERE id=1"),
            ("storage", "UPDATE items SET text_value=1 WHERE id=1"),
        ):
            path = self._database(f"{name}.db", [(1, "one", b"1")])
            before = snapshot_sqlite_database(path)
            conn = sqlite3.connect(path)
            try:
                conn.execute(statement)
                conn.commit()
            finally:
                conn.close()
            with self.subTest(name=name), self.assertRaises(SQLiteSnapshotError):
                verify_preexisting_rows_unchanged(before, snapshot_sqlite_database(path))

    def test_preexisting_row_verification_rejects_duplicate_post_ordering_key(self) -> None:
        path = self._database("duplicate-order.db", [(1, "one", b"1")])
        before = snapshot_sqlite_database(path)
        after = deepcopy(before)
        after["tables"][0]["rows"].append(deepcopy(after["tables"][0]["rows"][0]))
        with self.assertRaises(SQLiteSnapshotError):
            verify_preexisting_rows_unchanged(before, after)

    def test_identifier_injection_and_unstable_metadata_fail_closed(self) -> None:
        path = self._database("identifier.db", [(1, "safe", b"safe")])
        with self.assertRaises(SQLiteSnapshotError):
            snapshot_sqlite_database(path, table_names=["items;DELETE FROM items"])
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        finally:
            conn.close()

    def test_rowid_fallback_is_explicit_and_shadowed_rowid_fails_closed(self) -> None:
        stable = self.root / "rowid.db"
        conn = sqlite3.connect(stable)
        try:
            conn.execute("CREATE TABLE items(value TEXT)")
            conn.executemany("INSERT INTO items VALUES(?)", [("b",), ("a",)])
            conn.commit()
        finally:
            conn.close()
        snapshot = snapshot_sqlite_database(stable)
        self.assertEqual("rowid", snapshot["tables"][0]["ordering"]["kind"])
        self.assertEqual(2, len(snapshot["tables"][0]["rows"]))

        shadowed = self.root / "shadowed-rowid.db"
        conn = sqlite3.connect(shadowed)
        try:
            conn.execute('CREATE TABLE items(rowid TEXT, _rowid_ TEXT, oid TEXT)')
            conn.execute("INSERT INTO items VALUES('a','b','c')")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SQLiteSnapshotError):
            snapshot_sqlite_database(shadowed)

    def test_r1_production_schema_copy_snapshots_blob_without_typeerror(self) -> None:
        if not PRODUCTION_PROFILE_SIDECAR.is_file():
            self.skipTest("fixed production profile sidecar is absent")
        production_hash_before = hashlib.sha256(PRODUCTION_PROFILE_SIDECAR.read_bytes()).hexdigest()
        copied = self.root / "r1-profile-copy.db"
        shutil.copyfile(PRODUCTION_PROFILE_SIDECAR, copied)
        snapshot = snapshot_sqlite_database(
            copied, table_names=PRODUCTION_PROFILE_SNAPSHOT_TABLES,
        )
        production_hash_after = hashlib.sha256(PRODUCTION_PROFILE_SIDECAR.read_bytes()).hexdigest()
        self.assertEqual(production_hash_before, production_hash_after)
        manifests = next(
            table for table in snapshot["tables"]
            if table["table_name"] == "d2_authorization_manifests"
        )
        copy_uri = f"file:{copied.resolve().as_posix()}?mode=ro&immutable=1"
        source = sqlite3.connect(copy_uri, uri=True)
        try:
            expected_manifest_count = source.execute(
                "SELECT COUNT(*) FROM d2_authorization_manifests"
            ).fetchone()[0]
        finally:
            source.close()
        self.assertGreater(expected_manifest_count, 0)
        self.assertEqual(expected_manifest_count, len(manifests["rows"]))
        for manifest in manifests["rows"]:
            canonical_cell = next(
                cell for cell in manifest["cells"]
                if cell["column"] == "canonical_ascii"
            )
            self.assertEqual("blob", canonical_cell["value"]["sqlite_type"])
            self.assertGreater(canonical_cell["value"]["length"], 0)
        canonical_snapshot_bytes(snapshot)


if __name__ == "__main__":
    unittest.main()
