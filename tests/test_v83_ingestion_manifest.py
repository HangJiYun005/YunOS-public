from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory.ingestion_core import (
    ArchiveEntryMetadata,
    ArchiveLimits,
    IngestionCoreError,
    build_source_manifest,
    canonical_json_bytes,
    load_json_strict,
    normalized_json_digest,
    sanitize_reason_code,
    scan_source_archive,
    stream_file_sha256,
    validate_archive_metadata,
)


TS1 = "2026-07-26T00:00:00.000000Z"
TS2 = "2026-07-26T00:01:00.000000Z"


class SourceManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _scan(self, path: Path, **overrides):
        values = {
            "source_type": "chatgpt",
            "source_version": "export_v1",
            "adapter_name": "fixture_adapter",
            "adapter_version": "adapter_v1",
            "archive_kind": "zip",
            "discovered_at": TS1,
            "scanned_at": TS2,
            "record_counts": {"conversations": 2, "messages": 4},
            "asset_counts": {"attachments": 1},
        }
        values.update(overrides)
        return scan_source_archive(path, **values)

    def test_same_content_at_different_paths_has_same_manifest_identity(self) -> None:
        first = self.root / "private-name-one.zip"
        second = self.root / "unrelated-name-two.zip"
        first.write_bytes(b"same source bytes")
        second.write_bytes(first.read_bytes())
        one = self._scan(first)
        two = self._scan(second)
        self.assertEqual(one.manifest_id, two.manifest_id)
        self.assertEqual(one.source_locator_fingerprint, two.source_locator_fingerprint)

    def test_content_change_changes_manifest_identity(self) -> None:
        source = self.root / "source.zip"
        source.write_bytes(b"before")
        before = self._scan(source)
        source.write_bytes(b"after")
        after = self._scan(source)
        self.assertNotEqual(before.manifest_id, after.manifest_id)
        self.assertNotEqual(before.archive_sha256, after.archive_sha256)

    def test_same_filename_with_different_content_has_different_identity(self) -> None:
        first = self.root / "one" / "export.zip"
        second = self.root / "two" / "export.zip"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self.assertNotEqual(self._scan(first).manifest_id, self._scan(second).manifest_id)

    def test_repeated_scan_time_does_not_change_identity(self) -> None:
        source = self.root / "source.zip"
        source.write_bytes(b"stable")
        first = self._scan(source, discovered_at=TS1, scanned_at=TS1)
        second = self._scan(source, discovered_at=TS2, scanned_at=TS2)
        self.assertEqual(first.manifest_id, second.manifest_id)

    def test_adapter_and_manifest_schema_versions_change_identity(self) -> None:
        digest = hashlib.sha256(b"source").hexdigest()
        base = dict(
            source_type="chatgpt",
            source_version="export_v1",
            adapter_name="fixture_adapter",
            adapter_version="adapter_v1",
            archive_sha256=digest,
            archive_size=6,
            archive_kind="zip",
            discovered_at=TS1,
            scanned_at=TS2,
        )
        first = build_source_manifest(**base)
        adapter = build_source_manifest(**{**base, "adapter_version": "adapter_v2"})
        schema = build_source_manifest(
            **base, manifest_schema_version="v8.3-a-source-manifest-2"
        )
        self.assertNotEqual(first.manifest_id, adapter.manifest_id)
        self.assertNotEqual(first.manifest_id, schema.manifest_id)

    def test_streaming_hash_matches_hashlib_and_uses_multiple_chunks(self) -> None:
        source = self.root / "large.bin"
        payload = (b"0123456789abcdef" * 300_000)
        source.write_bytes(payload)
        progress: list[int] = []
        digest, size = stream_file_sha256(
            source, chunk_size=64 * 1024, progress_callback=lambda done, _total: progress.append(done)
        )
        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        self.assertEqual(len(payload), size)
        self.assertGreater(len(progress), 2)

    def test_cancellation_before_and_during_scan_fails_closed(self) -> None:
        source = self.root / "source.bin"
        source.write_bytes(b"x" * 100_000)
        with self.assertRaisesRegex(IngestionCoreError, "scan_cancelled"):
            stream_file_sha256(source, cancel_check=lambda: True)

        calls = 0

        def cancel() -> bool:
            nonlocal calls
            calls += 1
            return calls > 2

        with self.assertRaisesRegex(IngestionCoreError, "scan_cancelled"):
            stream_file_sha256(source, chunk_size=4096, cancel_check=cancel)

    def test_file_change_during_scan_fails_closed(self) -> None:
        source = self.root / "changing.bin"
        source.write_bytes(b"a" * 100_000)
        changed = False

        def mutate(done: int, _total: int) -> None:
            nonlocal changed
            if not changed and done:
                changed = True
                with source.open("r+b") as handle:
                    handle.seek(70_000)
                    handle.write(b"b" * 100)

        with self.assertRaisesRegex(IngestionCoreError, "source_changed_during_scan"):
            stream_file_sha256(source, chunk_size=4096, progress_callback=mutate)

    def test_invalid_source_type_and_counter_limits_are_rejected(self) -> None:
        source = self.root / "source.zip"
        source.write_bytes(b"x")
        with self.assertRaisesRegex(IngestionCoreError, "invalid_source_type"):
            self._scan(source, source_type="Chat GPT/private")
        with self.assertRaisesRegex(IngestionCoreError, "invalid_record_counts"):
            self._scan(source, record_counts={"messages": 1_000_000_001})

    def test_manifest_output_contains_no_absolute_path_or_private_filename(self) -> None:
        source = self.root / "very-private-conversation-title.zip"
        source.write_bytes(b"secret body that must not persist")
        manifest = self._scan(source)
        encoded = json.dumps(manifest.to_dict(), ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(source.name, encoded)
        self.assertNotIn("secret body", encoded)
        self.assertNotIn("path", encoded.lower())

    def test_warnings_are_reason_codes_not_raw_errors(self) -> None:
        source = self.root / "source.zip"
        source.write_bytes(b"x")
        raw = f"failed at {self.root}\\private.zip with bearer token"
        manifest = self._scan(source, warnings=[raw, "entry_count_warning"])
        self.assertEqual(("redacted_error_detail", "entry_count_warning"), manifest.warnings)
        self.assertEqual("redacted_error_detail", sanitize_reason_code(raw))

    def test_symlink_or_reparse_point_is_rejected(self) -> None:
        source = self.root / "source.zip"
        source.write_bytes(b"x")
        before = source.lstat()

        class ReparseStat:
            st_mode = before.st_mode
            st_size = before.st_size
            st_mtime_ns = before.st_mtime_ns
            st_ctime_ns = before.st_ctime_ns
            st_dev = before.st_dev
            st_ino = before.st_ino
            st_file_attributes = 0x400

        with patch.object(Path, "lstat", return_value=ReparseStat()):
            with self.assertRaisesRegex(IngestionCoreError, "source_link_not_allowed"):
                stream_file_sha256(source)

    def test_unc_device_and_traversal_paths_are_rejected(self) -> None:
        for path in (
            r"\\server\share\private.zip",
            r"\\?\C:\private.zip",
            r"\\.\PhysicalDrive0",
            self.root / ".." / "private.zip",
        ):
            with self.subTest(path=str(path)), self.assertRaises(IngestionCoreError):
                stream_file_sha256(path)


class CanonicalJsonTest(unittest.TestCase):
    def test_field_order_does_not_change_digest(self) -> None:
        self.assertEqual(
            normalized_json_digest({"a": 1, "b": 2}),
            normalized_json_digest({"b": 2, "a": 1}),
        )

    def test_unicode_is_nfc_normalized(self) -> None:
        self.assertEqual(
            normalized_json_digest({"text": "e\u0301"}),
            normalized_json_digest({"text": "\u00e9"}),
        )

    def test_finite_float_and_negative_zero_are_stable_and_explicit(self) -> None:
        value = {"number": 0.1, "negative_zero": -0.0}
        self.assertEqual(canonical_json_bytes(value), canonical_json_bytes(value))
        self.assertIn(b"-0.0", canonical_json_bytes(value))
        self.assertNotEqual(
            normalized_json_digest({"number": 0.0}),
            normalized_json_digest({"number": -0.0}),
        )

    def test_non_finite_numbers_are_rejected(self) -> None:
        for number in (math.nan, math.inf, -math.inf):
            with self.subTest(number=number), self.assertRaisesRegex(
                IngestionCoreError, "non_finite_number"
            ):
                normalized_json_digest({"value": number})

    def test_duplicate_and_normalization_colliding_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "duplicate_json_field"):
            load_json_strict('{"a":1,"a":2}')
        with self.assertRaisesRegex(IngestionCoreError, "duplicate_json_field"):
            load_json_strict('{"e\\u0301":1,"\\u00e9":2}')

    def test_invalid_utf8_and_unpaired_surrogate_are_rejected(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "invalid_unicode"):
            load_json_strict(b'{"x":"\xff"}')
        with self.assertRaisesRegex(IngestionCoreError, "invalid_unicode"):
            load_json_strict('{"x":"\\ud800"}')

    def test_json_depth_and_record_limits_are_enforced(self) -> None:
        nested = "[" * 12 + "0" + "]" * 12
        with self.assertRaisesRegex(IngestionCoreError, "json_depth_limit_exceeded"):
            load_json_strict(nested, max_depth=5)
        with self.assertRaisesRegex(IngestionCoreError, "json_record_limit_exceeded"):
            load_json_strict("[1,2,3,4]", max_nodes=3)

    def test_json_byte_limit_is_enforced_before_parse(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "json_size_limit_exceeded"):
            load_json_strict('{"payload":"123456789"}', max_bytes=10)


class ArchiveMetadataTest(unittest.TestCase):
    def test_safe_metadata_is_summarized_without_extraction(self) -> None:
        result = validate_archive_metadata([
            ArchiveEntryMetadata("folder/a.json", 100, 200),
            ArchiveEntryMetadata("assets/b.bin", 50, 50),
        ])
        self.assertEqual(2, result.entry_count)
        self.assertEqual(150, result.total_compressed_size)
        self.assertEqual(250, result.total_uncompressed_size)

    def test_zip_bomb_ratio_member_and_total_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "archive_compression_ratio_exceeded"):
            validate_archive_metadata(
                [ArchiveEntryMetadata("a.bin", 1, 2000)],
                ArchiveLimits(max_compression_ratio=100),
            )
        with self.assertRaisesRegex(IngestionCoreError, "archive_member_size_limit_exceeded"):
            validate_archive_metadata(
                [ArchiveEntryMetadata("a.bin", 100, 101)],
                ArchiveLimits(max_member_size=100),
            )
        with self.assertRaisesRegex(IngestionCoreError, "archive_total_size_limit_exceeded"):
            validate_archive_metadata(
                [
                    ArchiveEntryMetadata("a.bin", 100, 100),
                    ArchiveEntryMetadata("b.bin", 100, 100),
                ],
                ArchiveLimits(max_total_uncompressed_size=150),
            )

    def test_archive_entry_count_limit_is_enforced_streamingly(self) -> None:
        entries = (ArchiveEntryMetadata(f"{index}.json", 1, 1) for index in range(4))
        with self.assertRaisesRegex(IngestionCoreError, "archive_entry_limit_exceeded"):
            validate_archive_metadata(entries, ArchiveLimits(max_entries=3))

    def test_archive_traversal_drive_absolute_link_and_depth_are_rejected(self) -> None:
        bad = (
            ArchiveEntryMetadata("../private", 1, 1),
            ArchiveEntryMetadata("/absolute", 1, 1),
            ArchiveEntryMetadata("C:/device", 1, 1),
            ArchiveEntryMetadata("safe/link", 1, 1, is_link=True),
            ArchiveEntryMetadata("a/b/c/d", 1, 1),
        )
        for index, entry in enumerate(bad):
            limits = ArchiveLimits(max_path_depth=2) if index == 4 else ArchiveLimits()
            with self.subTest(entry=entry.member_name), self.assertRaisesRegex(
                IngestionCoreError, "unsafe_archive_member"
            ):
                validate_archive_metadata([entry], limits)
