from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from memory.ingestion_adapters.chatgpt import (
    AdapterLimits,
    ChatGPTAdapter,
    ChatGPTAdapterError,
    DETERMINISTIC_TIMESTAMP,
)
from tests.v83_chatgpt_adapter_fixtures import (
    simple_conversation,
    write_json_source,
    write_zip_source,
)


class ChatGPTAdapterSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_reason(
        self,
        source: Path,
        reason: str,
        *,
        limits: AdapterLimits | None = None,
    ) -> None:
        with self.assertRaises(ChatGPTAdapterError) as caught:
            ChatGPTAdapter(
                source,
                limits=limits,
                timestamp=DETERMINISTIC_TIMESTAMP,
            ).scan_manifest()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertEqual(reason, str(caught.exception))

    def raw_zip(self, rows: list[tuple[zipfile.ZipInfo | str, bytes]]) -> Path:
        path = self.root / "unsafe.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in rows:
                archive.writestr(name, payload)
        return path

    def conversations_payload(self) -> bytes:
        return json.dumps([simple_conversation()]).encode("utf-8")

    def test_missing_source_and_missing_conversations_fail_closed(self) -> None:
        self.assert_reason(self.root / "missing.json", "source_unavailable")
        archive = self.raw_zip([("other.json", b"[]")])
        self.assert_reason(archive, "conversations_json_missing")

    def test_zip_slip_parent_drive_unc_and_extended_paths_are_rejected(self) -> None:
        for index, unsafe in enumerate(
            ("../evil.txt", r"C:\evil.txt", "//server/share.txt", r"\\?\C:\evil.txt")
        ):
            with self.subTest(unsafe=unsafe):
                path = self.root / f"unsafe-{index}.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("conversations.json", self.conversations_payload())
                    archive.writestr(unsafe, b"x")
                self.assert_reason(path, "unsafe_archive_member")

    def test_zip_symlink_entry_is_rejected(self) -> None:
        link = zipfile.ZipInfo("linked-asset")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.raw_zip(
            [
                ("conversations.json", self.conversations_payload()),
                (link, b"target"),
            ]
        )
        self.assert_reason(archive, "unsafe_archive_member")

    def test_directory_symlink_is_rejected_when_platform_allows_creation(self) -> None:
        source = self.root / "source"
        source.mkdir()
        write_json_source(source, [simple_conversation()])
        target = self.root / "target.bin"
        target.write_bytes(b"x")
        link = source / "link.bin"
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest("symlink creation is not available")
        self.assert_reason(source, "source_link_not_allowed")

    def test_duplicate_zip_entry_and_case_collision_are_rejected(self) -> None:
        duplicate = self.root / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("conversations.json", self.conversations_payload())
            with self.assertWarns(UserWarning):
                archive.writestr("conversations.json", self.conversations_payload())
        self.assert_reason(duplicate, "duplicate_archive_entry")
        collision = self.raw_zip(
            [
                ("conversations.json", self.conversations_payload()),
                ("Asset.bin", b"a"),
                ("asset.bin", b"b"),
            ]
        )
        self.assert_reason(collision, "archive_case_collision")

    def test_duplicate_conversations_basename_is_rejected(self) -> None:
        source = self.root / "directory"
        (source / "nested").mkdir(parents=True)
        write_json_source(source, [simple_conversation()])
        write_json_source(source / "nested", [simple_conversation()])
        self.assert_reason(source, "duplicate_conversations_source")

    def test_entry_count_and_compression_ratio_limits_are_enforced(self) -> None:
        archive = self.raw_zip(
            [
                ("conversations.json", self.conversations_payload()),
                ("asset.bin", b"x"),
            ]
        )
        self.assert_reason(
            archive,
            "archive_entry_limit_exceeded",
            limits=AdapterLimits(max_entries=1),
        )
        bomb = self.root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("conversations.json", self.conversations_payload())
            handle.writestr("zeros.bin", b"\0" * 100_000)
        self.assert_reason(
            bomb,
            "archive_compression_ratio_exceeded",
            limits=AdapterLimits(max_compression_ratio=2),
        )

    def test_json_size_depth_duplicate_key_and_unicode_limits_are_enforced(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        self.assert_reason(
            source,
            "json_size_limit_exceeded",
            limits=AdapterLimits(max_json_bytes=32),
        )
        duplicate = self.root / "duplicate-key.json"
        duplicate.write_text(
            '[{"id":"c","mapping":{},"mapping":{}}]',
            encoding="utf-8",
        )
        self.assert_reason(duplicate, "duplicate_json_key")
        deep = self.root / "deep.json"
        deep.write_text(
            '[{"id":"c","mapping":{},"extra":[[[[[[1]]]]]]}]',
            encoding="utf-8",
        )
        self.assert_reason(
            deep,
            "json_depth_limit_exceeded",
            limits=AdapterLimits(max_json_depth=4),
        )
        invalid = self.root / "invalid-unicode.json"
        invalid.write_bytes(b"\xff\xfe")
        self.assert_reason(invalid, "invalid_unicode")

    def test_cancellation_is_checked_before_parsing(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        with self.assertRaises(ChatGPTAdapterError) as caught:
            ChatGPTAdapter(
                source,
                cancel_check=lambda: True,
                timestamp=DETERMINISTIC_TIMESTAMP,
            ).scan_manifest()
        self.assertEqual("scan_cancelled", caught.exception.reason_code)

    def test_source_change_during_scan_is_rejected(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        from memory.ingestion_adapters import chatgpt as module

        real_stream = module.stream_file_sha256

        def mutate_after_hash(path: Path, **kwargs):
            result = real_stream(path, **kwargs)
            Path(path).write_text("[]", encoding="utf-8")
            return result

        with patch.object(module, "stream_file_sha256", side_effect=mutate_after_hash):
            self.assert_reason(source, "source_changed_during_scan")

    def test_external_url_is_blocked_without_network_access(self) -> None:
        part = {
            "type": "file",
            "id": "external",
            "url": "https://example.invalid/asset?signature=secret",
            "name": "remote.bin",
        }
        source = write_json_source(
            self.root,
            [simple_conversation(assistant_parts=[part])],
        )
        with patch(
            "socket.socket.connect",
            side_effect=AssertionError("network access attempted"),
        ):
            occurrence = ChatGPTAdapter(
                source,
                timestamp=DETERMINISTIC_TIMESTAMP,
            ).enumerate_assets()[0]
        self.assertEqual("external_url_blocked", occurrence.entity_state)

    def test_scan_does_not_modify_source_or_create_formal_media(self) -> None:
        source = write_zip_source(self.root, [simple_conversation()])
        before = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "files": sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")),
        }
        ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        ).produce_import_plan(
            target_snapshot=__import__(
                "memory.ingestion_adapters.chatgpt",
                fromlist=["empty_target_snapshot"],
            ).empty_target_snapshot(),
            batch_mode="full",
        )
        after = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "files": sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*")),
        }
        self.assertEqual(before, after)

    def test_conflicting_native_attachment_identity_becomes_explicit_plan_conflict(
        self,
    ) -> None:
        first = {
            "type": "file",
            "id": "same-id",
            "name": "a.txt",
            "mime_type": "text/plain",
        }
        second = {
            "type": "file",
            "id": "same-id",
            "name": "b.txt",
            "mime_type": "text/plain",
        }
        conversation = simple_conversation(assistant_parts=[first, second])
        source = write_json_source(self.root, [conversation])
        adapter = ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )
        result = adapter.produce_import_plan(
            target_snapshot=__import__(
                "memory.ingestion_adapters.chatgpt",
                fromlist=["empty_target_snapshot"],
            ).empty_target_snapshot(),
            batch_mode="full",
        )
        self.assertIn(
            "source_attachment_identity_conflict",
            result.source_audit_summary.warning_codes,
        )
        self.assertIn(
            "asset_locator_ambiguous",
            {item.conflict_type for item in result.import_plan.conflicts},
        )

    def test_error_messages_do_not_echo_private_text_or_paths(self) -> None:
        private_text = "PRIVATE-BODY-SHOULD-NOT-APPEAR"
        source = self.root / "private-source-name.json"
        source.write_text(
            '[{"mapping": {"n": {"message": {"content": {"parts": ["'
            + private_text
            + '"]}}}}, "mapping": {}}]',
            encoding="utf-8",
        )
        with self.assertRaises(ChatGPTAdapterError) as caught:
            ChatGPTAdapter(source).scan_manifest()
        rendered = str(caught.exception)
        self.assertNotIn(private_text, rendered)
        self.assertNotIn(str(source), rendered)
        self.assertEqual("duplicate_json_key", rendered)


if __name__ == "__main__":
    unittest.main()
