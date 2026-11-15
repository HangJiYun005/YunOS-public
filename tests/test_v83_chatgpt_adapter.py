from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_adapters.chatgpt import (
    ACTIVE_PATH_COMPAT,
    ALL_REACHABLE_AUDIT,
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    empty_target_snapshot,
)
from tests.v83_chatgpt_adapter_fixtures import (
    PNG,
    branched_conversation,
    message,
    node,
    simple_conversation,
    write_directory_source,
    write_json_source,
    write_zip_source,
)


class ChatGPTAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, source: Path) -> ChatGPTAdapter:
        return ChatGPTAdapter(
            source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )

    def test_discovers_zip_directory_and_single_json(self) -> None:
        conversation = simple_conversation()
        single = write_json_source(self.root, [conversation], name="source.data")
        directory = write_directory_source(self.root, [conversation])
        archive = write_zip_source(self.root, [conversation])
        self.assertEqual("single_json", self.adapter(single).probe_source().archive_kind)
        self.assertEqual("directory", self.adapter(directory).probe_source().archive_kind)
        self.assertEqual("zip", self.adapter(archive).probe_source().archive_kind)

    def test_equivalent_zip_and_directory_have_same_normalized_semantics(self) -> None:
        part = {
            "type": "image_asset_pointer",
            "id": "file-native-1",
            "asset_pointer": "assets/picture.png",
            "name": "picture.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG),
        }
        conversation = simple_conversation(assistant_parts=[part])
        assets = {"assets/picture.png": PNG}
        directory = write_directory_source(self.root, [conversation], assets=assets)
        archive = write_zip_source(self.root, [conversation], assets=assets)
        target = empty_target_snapshot()
        directory_result = self.adapter(directory).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        archive_result = self.adapter(archive).produce_import_plan(
            target_snapshot=target,
            batch_mode="full",
        )
        self.assertEqual(
            directory_result.bundle.normalized_digest,
            archive_result.bundle.normalized_digest,
        )
        self.assertEqual(
            directory_result.semantic_plan_digest,
            archive_result.semantic_plan_digest,
        )
        self.assertNotEqual(
            directory_result.manifest.archive_kind,
            archive_result.manifest.archive_kind,
        )

    def test_manifest_contains_required_graph_and_asset_counts(self) -> None:
        conversation = branched_conversation()
        source = write_json_source(self.root, [conversation])
        manifest = self.adapter(source).scan_manifest()
        expected = {
            "conversations",
            "mapping_nodes",
            "active_messages",
            "branch_messages",
            "empty_nodes",
            "orphan_nodes",
            "attachments",
            "asset_entities",
            "metadata_only_assets",
            "unsupported_parts",
            "warnings",
            "errors",
        }
        self.assertTrue(expected.issubset(manifest.record_counts))
        self.assertEqual(1, manifest.record_counts["branch_messages"])
        self.assertIn("branch_messages_excluded", manifest.warnings)

    def test_active_path_compat_excludes_branch_but_audit_enumerates_it(self) -> None:
        source = write_json_source(self.root, [branched_conversation()])
        adapter = self.adapter(source)
        active = adapter.enumerate_messages(branch_policy=ACTIVE_PATH_COMPAT)
        audit = adapter.enumerate_messages(branch_policy=ALL_REACHABLE_AUDIT)
        self.assertEqual(["message-1", "message-2"], [item.source_message_id for item in active])
        self.assertEqual(3, len(audit))
        self.assertEqual(2, len(adapter.build_normalized_bundle(batch_mode="full").messages))

    def test_message_id_precedes_node_id_and_both_evidence_are_preserved(self) -> None:
        conversation = simple_conversation()
        conversation["mapping"]["message-2"]["message"]["id"] = "official-message-id"
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        incoming = next(
            item
            for item in bundle.messages
            if item.provenance.source_record_id == "official-message-id"
        )
        self.assertEqual("message-2", incoming.source_node_id)
        self.assertIn("message_node_id_mismatch", incoming.warning_codes)

    def test_mapping_node_fallback_is_stable(self) -> None:
        conversation = simple_conversation()
        conversation["mapping"]["message-2"]["message"]["id"] = None
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        incoming = next(
            item
            for item in bundle.messages
            if item.provenance.source_record_id == "message-2"
        )
        self.assertEqual("mapping_fallback", incoming.normalized_metadata["identity_kind"])

    def test_synthetic_message_identity_is_path_independent(self) -> None:
        raw_message = message(None, "user", ["synthetic"], created=1, updated=1)
        conversation = {
            "id": "conversation-synthetic",
            "title": "Synthetic",
            "create_time": 1,
            "update_time": 1,
            "current_node": "",
            "mapping": dict([node("", raw_message, parent=None, children=[])]),
        }
        first_root = self.root / "first"
        second_root = self.root / "second"
        first_root.mkdir()
        second_root.mkdir()
        first = write_json_source(first_root, [conversation])
        second = write_json_source(second_root, [conversation], name="renamed.json")
        first_id = self.adapter(first).build_normalized_bundle(
            batch_mode="full"
        ).messages[0].provenance.source_record_id
        second_id = self.adapter(second).build_normalized_bundle(
            batch_mode="full"
        ).messages[0].provenance.source_record_id
        self.assertEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("chatgpt-synthetic-message-v1:"))

    def test_roles_text_code_unicode_and_newlines_are_normalized(self) -> None:
        conversation = simple_conversation(
            user_text="Cafe\u0301\r\nline",
            assistant_parts=[
                {
                    "type": "code",
                    "text": "```py\r\nprint('x')\r\n```",
                    "language": "python",
                }
            ],
        )
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        self.assertEqual("Café\nline", bundle.messages[0].text)
        self.assertIn("```py\nprint('x')\n```", bundle.messages[1].text)
        self.assertEqual(
            "code",
            bundle.messages[1].normalized_metadata["structured_parts"][0]["part_type"],
        )

    def test_unknown_part_is_typed_and_warned_not_dropped(self) -> None:
        conversation = simple_conversation(
            assistant_parts=[{"type": "future_widget", "payload": {"x": 1}}]
        )
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        incoming = bundle.messages[1]
        self.assertEqual(
            "unsupported",
            incoming.normalized_metadata["structured_parts"][0]["part_type"],
        )
        self.assertIn("unsupported_content_part", incoming.warning_codes)
        self.assertEqual(1, bundle.audit_summary.record_counts["unsupported_parts"])

    def test_empty_body_and_attachment_only_message_are_valid(self) -> None:
        part = {
            "type": "file",
            "id": "file-only",
            "name": "note.txt",
            "mime_type": "text/plain",
        }
        conversation = simple_conversation(assistant_parts=[part])
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        self.assertEqual("", bundle.messages[1].text)
        self.assertEqual(("file-only",), bundle.messages[1].normalized_metadata["attachment_refs"])
        self.assertEqual("metadata_only", bundle.attachments[0].entity_state)

    def test_native_attachment_identity_reuses_public_media_protocol(self) -> None:
        part = {
            "type": "file",
            "id": "native-attachment",
            "name": "note.txt",
            "mime_type": "text/plain",
        }
        source = write_json_source(
            self.root,
            [simple_conversation(assistant_parts=[part])],
        )
        occurrence = self.adapter(source).enumerate_assets()[0]
        self.assertEqual("native", occurrence.identity_kind)
        self.assertEqual("native-attachment", occurrence.source_attachment_id)
        self.assertTrue(occurrence.public_media_id.startswith("ym1_"))

    def test_repeated_descriptor_keeps_two_occurrences_one_logical_identity(self) -> None:
        descriptor = {
            "type": "file",
            "name": "same.txt",
            "mime_type": "text/plain",
            "size_bytes": 3,
        }
        source = write_json_source(
            self.root,
            [simple_conversation(assistant_parts=[descriptor, descriptor])],
        )
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        self.assertEqual(2, len(bundle.attachment_occurrences))
        self.assertEqual(1, len(bundle.attachments))
        self.assertEqual(
            bundle.attachment_occurrences[0].source_attachment_id,
            bundle.attachment_occurrences[1].source_attachment_id,
        )
        self.assertEqual("synthetic", bundle.attachment_occurrences[0].identity_kind)

    def test_zip_asset_is_stream_verified_without_persistent_copy(self) -> None:
        part = {
            "type": "image_asset_pointer",
            "id": "image-1",
            "asset_pointer": "assets/image.png",
            "name": "image.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG),
        }
        source = write_zip_source(
            self.root,
            [simple_conversation(assistant_parts=[part])],
            assets={"assets/image.png": PNG},
        )
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        occurrence = bundle.attachment_occurrences[0]
        self.assertEqual("verified", occurrence.entity_state)
        self.assertEqual(len(PNG), occurrence.actual_size)
        self.assertIsNotNone(occurrence.content_sha256)
        self.assertEqual(1, len(bundle.asset_entities))
        self.assertFalse(any(self.root.rglob("*.blob")))

    def test_metadata_external_missing_and_mismatch_states_are_explicit(self) -> None:
        parts = [
            {"type": "file", "id": "meta", "name": "meta.txt"},
            {
                "type": "file",
                "id": "external",
                "url": "https://example.invalid/private?token=secret",
                "name": "remote.bin",
            },
            {
                "type": "file",
                "id": "missing",
                "asset_pointer": "missing.bin",
                "name": "missing.bin",
            },
            {
                "type": "image_asset_pointer",
                "id": "bad-size",
                "asset_pointer": "image.png",
                "name": "image.png",
                "mime_type": "image/jpeg",
                "size_bytes": 1,
            },
        ]
        source = write_zip_source(
            self.root,
            [simple_conversation(assistant_parts=parts)],
            assets={"image.png": PNG},
        )
        states = {
            item.source_attachment_id: item.entity_state
            for item in self.adapter(source).enumerate_assets()
        }
        self.assertEqual("metadata_only", states["meta"])
        self.assertEqual("external_url_blocked", states["external"])
        self.assertEqual("missing", states["missing"])
        self.assertEqual("size_mismatch", states["bad-size"])

    def test_graph_anomalies_are_visible_in_counts_and_warnings(self) -> None:
        conversation = simple_conversation()
        conversation["mapping"]["empty"] = {
            "id": "empty",
            "parent": "missing-parent",
            "children": [],
            "message": None,
        }
        conversation["mapping"]["message-1"]["parent"] = "message-2"
        conversation["mapping"]["message-2"]["children"] = ["message-1"]
        source = write_json_source(self.root, [conversation])
        summary = self.adapter(source).validate_source()
        self.assertEqual(1, summary.record_counts["empty_nodes"])
        self.assertEqual(1, summary.record_counts["orphan_nodes"])
        self.assertGreater(summary.record_counts["warnings"], 0)
        self.assertIn("conversation_cycle", summary.warning_codes)

    def test_invalid_and_missing_times_become_warnings_not_runtime_ids(self) -> None:
        conversation = simple_conversation()
        conversation["mapping"]["message-1"]["message"]["create_time"] = "not-time"
        conversation["mapping"]["message-2"]["message"]["update_time"] = None
        source = write_json_source(self.root, [conversation])
        bundle = self.adapter(source).build_normalized_bundle(batch_mode="full")
        self.assertIsNone(bundle.messages[0].created_at)
        self.assertIsNone(bundle.messages[1].updated_at)
        self.assertIn("message_created_at_invalid", bundle.messages[0].warning_codes)
        self.assertIn("message_updated_at_missing", bundle.messages[1].warning_codes)

    def test_conversation_input_order_does_not_change_normalized_digest(self) -> None:
        first = simple_conversation(conversation_id="conversation-a")
        second = simple_conversation(conversation_id="conversation-b")
        source_a = write_json_source(self.root, [first, second], name="a.json")
        source_b = write_json_source(self.root, [second, first], name="b.json")
        digest_a = self.adapter(source_a).build_normalized_bundle(
            batch_mode="full"
        ).normalized_digest
        digest_b = self.adapter(source_b).build_normalized_bundle(
            batch_mode="full"
        ).normalized_digest
        self.assertEqual(digest_a, digest_b)

    def test_scan_determinism_is_verified(self) -> None:
        source = write_zip_source(self.root, [simple_conversation()])
        self.assertTrue(self.adapter(source).verify_scan_determinism())

    def test_plan_summary_is_audit_only_and_does_not_execute_actions(self) -> None:
        source = write_json_source(self.root, [simple_conversation()])
        result = self.adapter(source).produce_import_plan(
            target_snapshot=empty_target_snapshot(),
            batch_mode="full",
        )
        self.assertFalse(result.summary()["operation_executed"])
        self.assertEqual("planned", result.bundle.batch.current_state)
        self.assertEqual(
            result.bundle.batch.plan_digest,
            result.bundle.batch.plan_digest.lower(),
        )

    def test_persistent_descriptors_do_not_contain_source_absolute_path(self) -> None:
        locator = r"C:\private\secret.png"
        part = {
            "type": "image_asset_pointer",
            "id": "private-path",
            "asset_pointer": locator,
            "name": r"C:\private\secret.png",
        }
        source = write_json_source(
            self.root,
            [simple_conversation(assistant_parts=[part])],
        )
        adapter = self.adapter(source)
        manifest_json = json.dumps(adapter.scan_manifest().to_dict())
        occurrence_json = json.dumps(
            adapter.enumerate_assets()[0].semantic_descriptor()
        )
        bundle = adapter.build_normalized_bundle(batch_mode="full")
        self.assertNotIn(str(source.resolve()), manifest_json)
        self.assertNotIn(locator, occurrence_json)
        self.assertNotIn(locator, bundle.messages[1].text)


if __name__ == "__main__":
    unittest.main()
