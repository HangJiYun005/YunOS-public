from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
from collections import Counter
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.media_index import (
    DEFAULT_ALLOWED_ENTITY_ROOTS,
    EntityState,
    MediaIndexCancelled,
    MediaIndexConflict,
    build_media_index,
    derive_media_id,
    get_media_descriptor,
    inspect_media_entity,
    list_recovery_candidates,
    resolve_media_relationship,
    validate_allowed_entity_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
LOCAL_WEB = PROJECT_ROOT / "tools" / "local_web.py"
OFFLINE_CACHE = PROJECT_ROOT / "web" / "offline-cache.js"
SERVICE_WORKER = PROJECT_ROOT / "web" / "service-worker.js"
APP = PROJECT_ROOT / "web" / "app.js"

SCHEMA = """
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY
);
CREATE TABLE messages (
    message_id TEXT,
    conversation_id TEXT,
    metadata_json TEXT,
    source_platform TEXT
);
CREATE TABLE assets (
    asset_file TEXT PRIMARY KEY,
    original_name TEXT,
    size_bytes INTEGER
);
"""

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-test-payload"


def attachment(
    attachment_id: str,
    *,
    name: str = "sample.png",
    mime_type: str = "image/png",
    size: int | None = len(PNG),
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": attachment_id,
        "name": name,
        "mime_type": mime_type,
        "size": size,
    }
    result.update(extra)
    return result


def claude_attachment(
    *,
    file_name: str = "private note.txt",
    file_type: str = "text/plain",
    file_size: int = 12,
    extracted_content: str = "private extracted content",
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "file_name": file_name,
        "file_type": file_type,
        "file_size": file_size,
        "extracted_content": extracted_content,
    }
    result.update(extra)
    return result


class MediaIndexFixture:
    def __init__(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entity_root = self.root / "entities"
        self.entity_root.mkdir()
        self.db = self.root / "media.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def close(self) -> None:
        self.temp.cleanup()

    def add_conversation(self, conversation_id: str) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "INSERT INTO conversations (conversation_id) VALUES (?)",
                (conversation_id,),
            )
            connection.commit()

    def add_message(
        self,
        message_id: str,
        conversation_id: str,
        attachments: list[object],
        *,
        source_platform: str = "chatgpt",
    ) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, conversation_id, metadata_json, source_platform
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    json.dumps({"attachments": attachments}),
                    source_platform,
                ),
            )
            connection.commit()

    def add_asset(
        self,
        attachment_id: str,
        *,
        size: int | None = len(PNG),
    ) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                """
                INSERT INTO assets (asset_file, original_name, size_bytes)
                VALUES (?, ?, ?)
                """,
                (f"{attachment_id}.dat", "catalog-name", size),
            )
            connection.commit()

    def replace_message_attachments(
        self,
        message_id: str,
        attachments: list[object],
    ) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "UPDATE messages SET metadata_json = ? WHERE message_id = ?",
                (json.dumps({"attachments": attachments}), message_id),
            )
            connection.commit()

    def write_entity(self, attachment_id: str, payload: bytes = PNG) -> Path:
        target = self.entity_root / f"{attachment_id}.dat"
        target.write_bytes(payload)
        return target

    def build(self, **kwargs: object):
        return build_media_index(
            self.db,
            _allowed_roots=(self.entity_root,),
            **kwargs,
        )


class MediaIndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = MediaIndexFixture()
        self.fixture.add_conversation("conversation-a")

    def tearDown(self) -> None:
        self.fixture.close()

    def _single_record(self, **build_kwargs: object):
        index = self.fixture.build(**build_kwargs)
        self.assertEqual(1, len(index.records))
        return index, index.records[0]


class StableMediaIdTests(MediaIndexTestCase):
    def test_same_record_is_stable_across_builds_and_restarts(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        first, first_record = self._single_record()
        second, second_record = self._single_record()
        self.assertEqual(first_record.media_id, second_record.media_id)
        self.assertEqual(
            first.get_media_descriptor(first_record.media_id).as_public_dict(),
            second.get_media_descriptor(second_record.media_id).as_public_dict(),
        )

    def test_different_attachment_ids_do_not_conflict(self) -> None:
        first = derive_media_id("attachment-a")
        second = derive_media_id("attachment-b")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ym1_"))
        self.assertEqual(56, len(first))

    def test_attachment_order_does_not_change_media_id(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a"), attachment("attachment-b")],
        )
        first = {
            record.source_attachment_id: record.media_id
            for record in self.fixture.build().records
        }
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE messages SET metadata_json = ? WHERE message_id = ?",
                (
                    json.dumps(
                        {
                            "attachments": [
                                attachment("attachment-b"),
                                attachment("attachment-a"),
                            ]
                        }
                    ),
                    "message-a",
                ),
            )
            connection.commit()
        second = {
            record.source_attachment_id: record.media_id
            for record in self.fixture.build().records
        }
        self.assertEqual(first, second)

    def test_file_path_changes_do_not_change_media_id(self) -> None:
        media_id = derive_media_id("attachment-a")
        first = validate_allowed_entity_path(
            "first/attachment-a.dat",
            _allowed_roots=(self.fixture.entity_root,),
        )
        second = validate_allowed_entity_path(
            "second/attachment-a.dat",
            _allowed_roots=(self.fixture.entity_root,),
        )
        self.assertEqual(EntityState.ENTITY_MISSING, first.state)
        self.assertEqual(EntityState.ENTITY_MISSING, second.state)
        self.assertEqual(media_id, derive_media_id("attachment-a"))

    def test_invalid_media_id_fails_closed(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        index = self.fixture.build()
        for invalid in ("", "../media", "attachment-a", "ym1_short"):
            result = get_media_descriptor(index, invalid)
            self.assertFalse(result.ok)
            self.assertEqual("invalid_media_id", result.error_code)
            self.assertNotIn(str(self.fixture.root), str(result.as_public_dict()))

    def test_derived_id_collision_fails_closed(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a"), attachment("attachment-b")],
        )
        collision = "ym1_" + ("a" * 52)
        with patch("memory.media_index.derive_media_id", return_value=collision):
            with self.assertRaisesRegex(MediaIndexConflict, "collision"):
                self.fixture.build()


class SyntheticSourceAttachmentIdentityTests(MediaIndexTestCase):
    def _add_claude_message(
        self,
        attachments: list[object],
        *,
        message_id: str = "claude-message-a",
    ) -> None:
        self.fixture.add_message(
            message_id,
            "conversation-a",
            attachments,
            source_platform="claude",
        )

    def test_claude_idless_attachment_is_stable_across_builds(self) -> None:
        self._add_claude_message([claude_attachment()])
        first, first_record = self._single_record()
        second, second_record = self._single_record()
        self.assertEqual(first_record.source_attachment_id, second_record.source_attachment_id)
        self.assertEqual(first_record.media_id, second_record.media_id)
        self.assertTrue(first_record.source_attachment_id.startswith("ysa1_"))
        self.assertTrue(first_record.media_id.startswith("ym1_"))
        self.assertEqual("synthetic", first_record.source_identity_kind)
        self.assertTrue(first.get_media_descriptor(first_record.media_id).ok)
        self.assertTrue(second.get_media_descriptor(second_record.media_id).ok)

    def test_dictionary_key_order_does_not_change_identity(self) -> None:
        original = claude_attachment()
        reordered = dict(reversed(list(original.items())))
        self._add_claude_message([original])
        first = self.fixture.build().records[0].media_id
        self.fixture.replace_message_attachments("claude-message-a", [reordered])
        second = self.fixture.build().records[0].media_id
        self.assertEqual(first, second)

    def test_attachment_array_order_is_not_part_of_identity(self) -> None:
        first_attachment = claude_attachment(file_name="first.txt")
        second_attachment = claude_attachment(file_name="second.txt")
        self._add_claude_message([first_attachment, second_attachment])
        first = {
            record.canonical_attachment_digest: record.media_id
            for record in self.fixture.build().records
        }
        self.fixture.replace_message_attachments(
            "claude-message-a",
            [second_attachment, first_attachment],
        )
        second = {
            record.canonical_attachment_digest: record.media_id
            for record in self.fixture.build().records
        }
        self.assertEqual(first, second)

    def test_path_metadata_is_not_part_of_identity(self) -> None:
        self._add_claude_message(
            [claude_attachment(relative_path="first/location")]
        )
        first = self.fixture.build().records[0].media_id
        self.fixture.replace_message_attachments(
            "claude-message-a",
            [claude_attachment(relative_path="second/location")],
        )
        second = self.fixture.build().records[0].media_id
        self.assertEqual(first, second)

    def test_unicode_filename_uses_nfc(self) -> None:
        composed = "caf\u00e9.txt"
        decomposed = "cafe\u0301.txt"
        self._add_claude_message([claude_attachment(file_name=composed)])
        first = self.fixture.build().records[0].media_id
        self.fixture.replace_message_attachments(
            "claude-message-a",
            [claude_attachment(file_name=decomposed)],
        )
        second = self.fixture.build().records[0].media_id
        self.assertEqual(first, second)

    def test_missing_and_none_fields_share_explicit_sentinel(self) -> None:
        missing = claude_attachment()
        del missing["file_name"]
        explicit_none = claude_attachment()
        explicit_none["file_name"] = None
        self._add_claude_message([missing])
        first = self.fixture.build().records[0].media_id
        self.fixture.replace_message_attachments(
            "claude-message-a",
            [explicit_none],
        )
        second = self.fixture.build().records[0].media_id
        self.assertEqual(first, second)

    def test_private_values_and_source_ids_do_not_leak_into_identity(self) -> None:
        private_filename = "private-family-document.txt"
        private_content = "private extracted sentence"
        self._add_claude_message(
            [
                claude_attachment(
                    file_name=private_filename,
                    extracted_content=private_content,
                )
            ],
            message_id="private-source-message-id",
        )
        index, record = self._single_record()
        serialized = json.dumps(
            {
                "sourceAttachmentId": record.source_attachment_id,
                "mediaId": record.media_id,
                "report": list_recovery_candidates(index),
            }
        )
        for private_value in (
            private_filename,
            private_content,
            "private-source-message-id",
            "conversation-a",
        ):
            self.assertNotIn(private_value, serialized)

    def test_critical_descriptor_changes_change_identity(self) -> None:
        baseline = claude_attachment()
        self._add_claude_message([baseline])
        baseline_id = self.fixture.build().records[0].media_id
        changes = (
            claude_attachment(file_name="changed.txt"),
            claude_attachment(file_type="application/json"),
            claude_attachment(file_size=99),
            claude_attachment(extracted_content="changed private content"),
        )
        for changed in changes:
            with self.subTest(changed_fields=tuple(changed)):
                self.fixture.replace_message_attachments(
                    "claude-message-a",
                    [changed],
                )
                self.assertNotEqual(
                    baseline_id,
                    self.fixture.build().records[0].media_id,
                )

    def test_indistinguishable_occurrences_share_logical_identity(self) -> None:
        duplicate = claude_attachment()
        self._add_claude_message([duplicate, dict(duplicate)])
        index = self.fixture.build()
        self.assertEqual(2, len(index.records))
        self.assertEqual(1, len({record.media_id for record in index.records}))
        self.assertTrue(
            all(record.logical_occurrence_count == 2 for record in index.records)
        )
        self.assertTrue(
            all(
                record.indistinguishable_duplicate_group
                for record in index.records
            )
        )
        report = list_recovery_candidates(index)
        self.assertEqual(2, report["occurrenceCount"])
        self.assertEqual(1, report["logicalIdentityCount"])
        self.assertEqual(1, report["syntheticIdentityCount"])
        self.assertEqual(2, report["syntheticOccurrenceCount"])
        self.assertEqual({"2": 1}, report["logicalOccurrenceCountDistribution"])
        self.assertEqual(1, report["indistinguishableDuplicateGroups"])

    def test_unapproved_source_without_native_id_fails_closed(self) -> None:
        self.fixture.add_message(
            "deepseek-message-a",
            "conversation-a",
            [claude_attachment()],
            source_platform="deepseek",
        )
        _, record = self._single_record()
        self.assertEqual("", record.media_id)
        self.assertEqual("missing", record.source_identity_kind)
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, record.entity_state)

    def test_invalid_descriptor_type_fails_closed(self) -> None:
        record = claude_attachment()
        record["file_size"] = "12"
        self._add_claude_message([record])
        _, indexed = self._single_record()
        self.assertEqual("", indexed.media_id)
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, indexed.entity_state)

    def test_synthetic_relationship_resolves_to_source_message_and_conversation(
        self,
    ) -> None:
        self._add_claude_message([claude_attachment()])
        index, record = self._single_record()
        result = resolve_media_relationship(
            index,
            record.media_id,
            expected_message_id="claude-message-a",
            expected_conversation_id="conversation-a",
        )
        self.assertTrue(result.ok)

    def test_conflicting_entity_hashes_block_shared_identity(self) -> None:
        first = claude_attachment(sha256="1" * 64)
        second = claude_attachment(sha256="2" * 64)
        self._add_claude_message([first, second])
        index = self.fixture.build()
        self.assertEqual(1, len({record.media_id for record in index.records}))
        self.assertTrue(
            all(
                record.entity_state is EntityState.ENTITY_IDENTITY_CONFLICT
                for record in index.records
            )
        )
        resolution = get_media_descriptor(index, index.records[0].media_id)
        self.assertFalse(resolution.ok)
        self.assertEqual("entity_identity_conflict", resolution.error_code)
        report = list_recovery_candidates(index)
        self.assertEqual(1, report["entityIdentityConflicts"])


class RelationshipValidationTests(MediaIndexTestCase):
    def test_attachment_message_conversation_relationship_resolves(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        index, record = self._single_record()
        result = resolve_media_relationship(
            index,
            record.media_id,
            expected_message_id="message-a",
            expected_conversation_id="conversation-a",
        )
        self.assertTrue(result.ok)

    def test_attachment_cannot_be_claimed_by_another_message(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        index, record = self._single_record()
        result = resolve_media_relationship(
            index,
            record.media_id,
            expected_message_id="message-forged",
            expected_conversation_id="conversation-a",
        )
        self.assertFalse(result.ok)
        self.assertEqual("media_relationship_mismatch", result.error_code)

    def test_message_cannot_be_claimed_by_another_conversation(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        index, record = self._single_record()
        result = resolve_media_relationship(
            index,
            record.media_id,
            expected_message_id="message-a",
            expected_conversation_id="conversation-forged",
        )
        self.assertFalse(result.ok)
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, result.entity_state)

    def test_duplicate_attachment_id_is_invalid_relationship(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a"), attachment("attachment-a")],
        )
        index = self.fixture.build()
        self.assertEqual(2, len(index.records))
        self.assertTrue(
            all(
                record.entity_state is EntityState.INVALID_RELATIONSHIP
                for record in index.records
            )
        )
        self.assertEqual({}, dict(index._by_media_id))

    def test_empty_attachment_id_is_invalid_relationship(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("")],
        )
        _, record = self._single_record()
        self.assertEqual("", record.media_id)
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, record.entity_state)

    def test_orphan_message_conversation_is_invalid_relationship(self) -> None:
        self.fixture.add_message(
            "message-a",
            "missing-conversation",
            [attachment("attachment-a")],
        )
        _, record = self._single_record()
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, record.entity_state)

    def test_duplicate_message_id_is_invalid_relationship(self) -> None:
        self.fixture.add_conversation("conversation-b")
        self.fixture.add_message(
            "shared-message",
            "conversation-a",
            [attachment("attachment-a")],
        )
        self.fixture.add_message(
            "shared-message",
            "conversation-b",
            [attachment("attachment-b")],
        )
        index = self.fixture.build()
        self.assertTrue(
            all(
                record.entity_state is EntityState.INVALID_RELATIONSHIP
                for record in index.records
            )
        )

    def test_frontend_forged_path_is_ignored(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [
                attachment(
                    "attachment-a",
                    path=r"..\outside\secret.dat",
                    canonical_path=r"C:\private\secret.dat",
                )
            ],
        )
        self.fixture.add_asset("attachment-a")
        entity = self.fixture.write_entity("attachment-a")
        index, record = self._single_record()
        self.assertEqual(EntityState.ENTITY_AVAILABLE, record.entity_state)
        self.assertEqual(entity.resolve(), record.canonical_path)
        public = inspect_media_entity(index, record.media_id).as_public_dict()
        self.assertNotIn("path", {key.lower() for key in public})
        self.assertNotIn(str(self.fixture.root), str(public))

    def test_non_mapping_attachment_is_invalid_relationship(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            ["not-an-attachment-object"],
        )
        _, record = self._single_record()
        self.assertEqual(EntityState.INVALID_RELATIONSHIP, record.entity_state)


class PathGateTests(MediaIndexTestCase):
    def test_normal_relative_locator_inside_fixed_root_is_allowed(self) -> None:
        target = self.fixture.entity_root / "safe.dat"
        target.write_bytes(PNG)
        result = validate_allowed_entity_path(
            "safe.dat",
            _allowed_roots=(self.fixture.entity_root,),
        )
        self.assertEqual(EntityState.ASSET_RECORD_PRESENT, result.state)
        self.assertEqual(target.resolve(), result.canonical_path)

    def test_dot_dot_forward_and_backslash_are_rejected(self) -> None:
        for locator in ("../secret.dat", r"..\secret.dat", "safe/../secret.dat"):
            result = validate_allowed_entity_path(
                locator,
                _allowed_roots=(self.fixture.entity_root,),
            )
            self.assertEqual(EntityState.ENTITY_OUTSIDE_ALLOWED_ROOT, result.state)

    def test_encoded_and_double_encoded_traversal_are_rejected(self) -> None:
        for locator in (
            "%2e%2e/secret.dat",
            "%252e%252e%255csecret.dat",
        ):
            result = validate_allowed_entity_path(
                locator,
                _allowed_roots=(self.fixture.entity_root,),
            )
            self.assertEqual(EntityState.ENTITY_OUTSIDE_ALLOWED_ROOT, result.state)

    def test_windows_drive_unc_and_device_paths_are_rejected(self) -> None:
        for locator in (
            r"C:\secret.dat",
            r"\\server\share\secret.dat",
            r"\\?\C:\secret.dat",
            r"\\.\PhysicalDrive0",
        ):
            result = validate_allowed_entity_path(
                locator,
                _allowed_roots=(self.fixture.entity_root,),
            )
            self.assertEqual(EntityState.ENTITY_OUTSIDE_ALLOWED_ROOT, result.state)

    def test_trailing_dot_space_and_alternate_data_stream_are_rejected(self) -> None:
        for locator in ("secret.dat.", "secret.dat ", "secret.dat:stream"):
            result = validate_allowed_entity_path(
                locator,
                _allowed_roots=(self.fixture.entity_root,),
            )
            self.assertEqual(EntityState.ENTITY_OUTSIDE_ALLOWED_ROOT, result.state)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.fixture.root / "outside.dat"
        outside.write_bytes(PNG)
        linked = self.fixture.entity_root / "linked.dat"
        try:
            os.symlink(outside, linked)
        except OSError:
            linked.write_bytes(PNG)
            with patch(
                "memory.media_index._is_reparse_point",
                side_effect=lambda path: Path(path).name == "linked.dat",
            ):
                result = validate_allowed_entity_path(
                    "linked.dat",
                    _allowed_roots=(self.fixture.entity_root,),
                )
        else:
            result = validate_allowed_entity_path(
                "linked.dat",
                _allowed_roots=(self.fixture.entity_root,),
            )
        self.assertEqual(EntityState.ENTITY_SYMLINK_ESCAPE, result.state)
        self.assertIsNone(result.canonical_path)

    def test_junction_or_reparse_component_is_rejected(self) -> None:
        linked_parent = self.fixture.entity_root / "junction"
        linked_parent.mkdir()
        with patch(
            "memory.media_index._is_reparse_point",
            side_effect=lambda path: Path(path).name == "junction",
        ):
            result = validate_allowed_entity_path(
                "junction/entity.dat",
                _allowed_roots=(self.fixture.entity_root,),
            )
        self.assertEqual(EntityState.ENTITY_SYMLINK_ESCAPE, result.state)

    def test_linked_allowed_root_is_rejected(self) -> None:
        with patch(
            "memory.media_index._is_reparse_point",
            side_effect=lambda path: Path(path) == self.fixture.entity_root,
        ):
            result = validate_allowed_entity_path(
                "entity.dat",
                _allowed_roots=(self.fixture.entity_root,),
            )
        self.assertEqual(EntityState.ENTITY_SYMLINK_ESCAPE, result.state)

    def test_case_difference_remains_inside_root(self) -> None:
        target = self.fixture.entity_root / "CaseSensitiveName.dat"
        target.write_bytes(PNG)
        locator = (
            "casesensitivename.dat"
            if os.path.normcase("A") == os.path.normcase("a")
            else target.name
        )
        result = validate_allowed_entity_path(
            locator,
            _allowed_roots=(self.fixture.entity_root,),
        )
        self.assertEqual(EntityState.ASSET_RECORD_PRESENT, result.state)
        self.assertTrue(result.canonical_path.is_relative_to(self.fixture.entity_root))

    def test_safe_nested_relative_path_is_normalized_inside_root(self) -> None:
        nested = self.fixture.entity_root / "nested"
        nested.mkdir()
        target = nested / "entity.dat"
        target.write_bytes(PNG)
        result = validate_allowed_entity_path(
            "nested/entity.dat",
            _allowed_roots=(self.fixture.entity_root,),
        )
        self.assertEqual(target.resolve(), result.canonical_path)

    def test_arbitrary_relative_root_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "absolute"):
            validate_allowed_entity_path(
                "safe.dat",
                _allowed_roots=("relative/user/root",),
            )


class EntityClassificationTests(MediaIndexTestCase):
    def _prepare(
        self,
        record: dict[str, object],
        *,
        asset: bool = True,
        payload: bytes | None = None,
        entity_directory: bool = False,
        compute_hash: bool = False,
    ):
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [record],
        )
        attachment_id = str(record["id"])
        if asset:
            self.fixture.add_asset(attachment_id)
        if entity_directory:
            (self.fixture.entity_root / f"{attachment_id}.dat").mkdir()
        elif payload is not None:
            self.fixture.write_entity(attachment_id, payload)
        return self._single_record(compute_hash=compute_hash)

    def test_asset_record_missing(self) -> None:
        _, record = self._prepare(attachment("attachment-a"), asset=False)
        self.assertEqual(EntityState.ASSET_RECORD_MISSING, record.entity_state)

    def test_entity_file_missing(self) -> None:
        index, record = self._prepare(attachment("attachment-a"))
        self.assertEqual(EntityState.ENTITY_MISSING, record.entity_state)
        resolution = inspect_media_entity(index, record.media_id)
        self.assertFalse(resolution.ok)
        self.assertEqual("media_entity_unavailable", resolution.error_code)
        self.assertEqual(EntityState.ENTITY_MISSING, resolution.entity_state)

    def test_asset_catalog_size_conflict_is_reported_without_changing_missing_state(
        self,
    ) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a", size=len(PNG))],
        )
        self.fixture.add_asset("attachment-a", size=len(PNG) + 9)
        index, record = self._single_record()
        self.assertEqual(EntityState.ENTITY_MISSING, record.entity_state)
        report = list_recovery_candidates(index)
        self.assertEqual(1, report["assetCatalogDeclaredSizeMismatches"])

    def test_directory_instead_of_file(self) -> None:
        _, record = self._prepare(
            attachment("attachment-a"),
            entity_directory=True,
        )
        self.assertEqual(EntityState.ENTITY_NOT_REGULAR_FILE, record.entity_state)

    def test_empty_file(self) -> None:
        _, record = self._prepare(
            attachment("attachment-a", size=0),
            payload=b"",
        )
        self.assertEqual(EntityState.ENTITY_EMPTY, record.entity_state)

    def test_declared_size_mismatch(self) -> None:
        _, record = self._prepare(
            attachment("attachment-a", size=len(PNG) + 1),
            payload=PNG,
        )
        self.assertEqual(EntityState.ENTITY_SIZE_MISMATCH, record.entity_state)

    def test_mime_mismatch(self) -> None:
        _, record = self._prepare(
            attachment("attachment-a", mime_type="image/jpeg"),
            payload=PNG,
        )
        self.assertEqual(EntityState.ENTITY_MIME_MISMATCH, record.entity_state)

    def test_hash_mismatch(self) -> None:
        _, record = self._prepare(
            attachment("attachment-a", sha256="0" * 64),
            payload=PNG,
        )
        self.assertEqual(EntityState.ENTITY_HASH_MISMATCH, record.entity_state)

    def test_change_during_inspection_is_detected(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        self.fixture.add_asset("attachment-a")
        self.fixture.write_entity("attachment-a")
        with patch(
            "memory.media_index._stat_identity",
            side_effect=[
                (len(PNG), 1, 1, 1),
                (len(PNG), 2, 1, 1),
            ],
        ):
            _, record = self._single_record()
        self.assertEqual(
            EntityState.ENTITY_CHANGED_DURING_CHECK,
            record.entity_state,
        )

    def test_entity_available_and_hash_is_streamed(self) -> None:
        index, record = self._prepare(
            attachment("attachment-a"),
            payload=PNG,
            compute_hash=True,
        )
        self.assertEqual(EntityState.ENTITY_AVAILABLE, record.entity_state)
        self.assertEqual(hashlib.sha256(PNG).hexdigest(), record.actual_hash)
        descriptor = inspect_media_entity(index, record.media_id).as_public_dict()
        self.assertEqual("image/png", descriptor["detectedMimeType"])
        self.assertTrue(descriptor["canOpen"])
        self.assertFalse(descriptor["canStream"])
        self.assertTrue(descriptor["etagCandidate"].startswith('"sha256-'))

    def test_large_file_hash_can_be_cancelled(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [
                attachment(
                    "attachment-a",
                    mime_type="application/octet-stream",
                    size=2 * 1024 * 1024,
                )
            ],
        )
        self.fixture.add_asset("attachment-a", size=2 * 1024 * 1024)
        self.fixture.write_entity("attachment-a", b"x" * (2 * 1024 * 1024))
        with self.assertRaises(MediaIndexCancelled):
            self.fixture.build(
                compute_hash=True,
                cancel_check=lambda: True,
            )

    def test_external_url_is_blocked_without_request(self) -> None:
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network access attempted"),
        ):
            _, record = self._prepare(
                attachment(
                    "attachment-a",
                    source_url="https://example.invalid/private",
                ),
                asset=False,
            )
        self.assertEqual(EntityState.EXTERNAL_URL_BLOCKED, record.entity_state)
        self.assertNotIn("example.invalid", record.unavailable_reason)


class SecurityBoundaryTests(MediaIndexTestCase):
    def test_public_descriptor_and_error_exclude_private_internals(self) -> None:
        private_filename = r"C:\Users\ExampleUser\secret photo.png"
        self.fixture.add_message(
            "private-message-id",
            "conversation-a",
            [
                attachment(
                    "private-attachment-id",
                    name=private_filename,
                )
            ],
        )
        index, record = self._single_record()
        descriptor = get_media_descriptor(index, record.media_id).as_public_dict()
        serialized = json.dumps(descriptor)
        self.assertNotIn("private-attachment-id", serialized)
        self.assertNotIn("private-message-id", serialized)
        self.assertNotIn(str(self.fixture.root), serialized)
        self.assertNotIn(r"C:\\Users", serialized)
        self.assertEqual("secret photo.png", descriptor["safeFilename"])

    def test_build_does_not_modify_source_database(self) -> None:
        self.fixture.add_message(
            "message-a",
            "conversation-a",
            [attachment("attachment-a")],
        )
        before = hashlib.sha256(self.fixture.db.read_bytes()).hexdigest()
        before_size = self.fixture.db.stat().st_size
        self.fixture.build()
        after = hashlib.sha256(self.fixture.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(before_size, self.fixture.db.stat().st_size)

    def test_module_has_no_http_or_download_implementation(self) -> None:
        source = (
            PROJECT_ROOT / "memory" / "media_index.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "urllib.request",
            "requests.",
            "http.client",
            "urlopen(",
            "do_GET",
            "do_HEAD",
            "send_header(",
            "socket.",
        ):
            self.assertNotIn(forbidden, source)

    def test_synthetic_identity_has_no_occurrence_or_runtime_inputs(self) -> None:
        source = (
            PROJECT_ROOT / "memory" / "media_index.py"
        ).read_text(encoding="utf-8")
        identity_source = source.split(
            "def _source_attachment_identity(",
            1,
        )[1].split("def _clean_text(", 1)[0]
        for forbidden in (
            "attachment_index",
            "rowid",
            "uuid",
            "random",
            "time.time",
            "Path(",
        ):
            self.assertNotIn(forbidden, identity_source)

    def test_local_web_has_no_media_route_or_media_bytes(self) -> None:
        source = LOCAL_WEB.read_text(encoding="utf-8")
        self.assertNotIn("/api/media", source)
        self.assertNotIn("media_index", source)
        for method in ("do_PUT", "do_PATCH", "do_DELETE"):
            self.assertNotIn(f"def {method}", source)

    def test_service_worker_remains_api_network_only(self) -> None:
        source = SERVICE_WORKER.read_text(encoding="utf-8")
        api_branch = source.split(
            'if (url.pathname.startsWith("/api/"))',
            1,
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        self.assertNotIn("/api/media", source)

    def test_no_media_store_writer_is_added(self) -> None:
        offline_cache = OFFLINE_CACHE.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn('putRecord("thumbnails"', app)
        self.assertNotIn('putRecord("mediaObjects"', app)
        self.assertIn('"thumbnails"', offline_cache)
        self.assertIn('"mediaObjects"', offline_cache)
        self.assertNotIn("/api/media", offline_cache)

    def test_default_root_is_fixed_to_chatgpt_import_staging(self) -> None:
        self.assertEqual(
            (PROJECT_ROOT / "imports" / "chatgpt").resolve(),
            DEFAULT_ALLOWED_ENTITY_ROOTS[0].resolve(),
        )


class ProductionRecoveryInventoryTests(unittest.TestCase):
    def test_real_database_recovery_inventory_is_aggregate_only(self) -> None:
        if not REAL_DB.exists():
            self.skipTest("real read-only database is unavailable")
        before_hash = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
        before_size = REAL_DB.stat().st_size
        with closing(
            sqlite3.connect(
                f"file:{REAL_DB.resolve().as_posix()}?mode=ro&immutable=1",
                uri=True,
            )
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            self.assertEqual(1, connection.execute("PRAGMA query_only").fetchone()[0])
            attachment_metadata_count = 0
            for (raw_metadata,) in connection.execute(
                "SELECT metadata_json FROM messages"
            ):
                try:
                    metadata = json.loads(raw_metadata) if raw_metadata else {}
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                attachments = (
                    metadata.get("attachments")
                    if isinstance(metadata, dict)
                    else None
                )
                if isinstance(attachments, list):
                    attachment_metadata_count += len(attachments)
            asset_count = connection.execute(
                "SELECT COUNT(*) FROM assets"
            ).fetchone()[0]

        index = build_media_index()
        report = list_recovery_candidates(index)
        occurrence_count = len(index.records)
        logical_occurrences = Counter(
            record.media_id for record in index.records if record.media_id
        )
        self.assertEqual(
            attachment_metadata_count,
            occurrence_count,
        )
        self.assertEqual(occurrence_count, report["records"])
        self.assertEqual(occurrence_count, report["occurrenceCount"])
        self.assertEqual(occurrence_count, report["stableSourceIds"])
        self.assertEqual(occurrence_count, report["stableMediaIds"])
        self.assertEqual(len(logical_occurrences), report["logicalIdentityCount"])
        self.assertEqual(len(logical_occurrences), report["uniqueMediaIds"])
        self.assertEqual(
            report["logicalIdentityCount"],
            report["nativeIdentityCount"] + report["syntheticIdentityCount"],
        )
        self.assertEqual(
            occurrence_count,
            report["nativeOccurrenceCount"] + report["syntheticOccurrenceCount"],
        )
        self.assertEqual(
            occurrence_count,
            sum(report["entityStates"].values()),
        )
        self.assertEqual(0, report["invalidRelationships"])
        self.assertEqual(0, report["unboundOccurrenceCount"])
        self.assertEqual(0, report["collisions"])
        self.assertEqual(0, report["entityIdentityConflicts"])
        self.assertEqual(
            report["logicalIdentityCount"],
            sum(report["logicalOccurrenceCountDistribution"].values()),
        )
        self.assertEqual(
            occurrence_count,
            sum(
                int(count) * groups
                for count, groups
                in report["logicalOccurrenceCountDistribution"].items()
            ),
        )
        for record in index.records:
            self.assertEqual(
                logical_occurrences[record.media_id],
                record.logical_occurrence_count,
            )
            self.assertIn(record.source_identity_kind, {"native", "synthetic"})
        self.assertLessEqual(report["assetCatalogMappings"], asset_count)
        self.assertEqual(0, report["entityAvailable"])
        self.assertEqual(before_hash, hashlib.sha256(REAL_DB.read_bytes()).hexdigest())
        self.assertEqual(before_size, REAL_DB.stat().st_size)
        serialized = json.dumps(report)
        self.assertNotIn(str(PROJECT_ROOT), serialized)
        self.assertNotIn(".dat", serialized.lower())
        self.assertNotIn("attachment-a", serialized)
        self.assertNotIn("https://", serialized)


if __name__ == "__main__":
    unittest.main()
