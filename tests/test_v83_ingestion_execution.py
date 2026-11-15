from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_execution import (
    IngestionExecutionError,
    execute_test_bundle,
    make_execution_operation,
    preflight_execution,
)
from memory.ingestion_verification import capture_test_target_snapshot
from tests.v83_ingestion_execution_fixtures import (
    PREFLIGHT_AT,
    approve_fixture,
    build_execution_fixture,
)


class IngestionExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = build_execution_fixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def preflight(self, operations=None, approval=None, batch=None):
        return preflight_execution(
            batch=batch or self.fixture.batch,
            manifest=self.fixture.manifest,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            stage_result=self.fixture.stage_result,
            approval=approval or self.fixture.approval,
            operations=operations or self.fixture.operations,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
            now=PREFLIGHT_AT,
        )

    def test_create_conversation_commits_and_verifies(self) -> None:
        preflight = self.preflight()
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertEqual("completed", result.status)
        self.assertTrue(result.verification.passed)
        with closing(sqlite3.connect(
            self.fixture.target.database_path
        )) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0],
            )

    def test_message_attachment_binding_and_metadata_handlers(self) -> None:
        self.fixture = build_execution_fixture(
            Path(self.temp.name) / "rich",
            with_asset=True,
        )
        conversation = self.fixture.operations[0]
        register_asset = self.fixture.operations[1]
        message_action = next(
            item
            for item in self.fixture.plan.actions
            if item.record_kind == "message"
        )
        message = make_execution_operation(
            plan_action_id=message_action.action_id,
            operation_type="create_message",
            payload={
                "message_id": "message_" + "2" * 32,
                "conversation_id": "conversation_" + "1" * 32,
                "source_identity": message_action.source_identity,
                "content_hash": "2" * 64,
                "safe_metadata": {"role": "fixture"},
            },
            dependency_operation_ids=(conversation.operation_id,),
        )
        attachment_action = next(
            item
            for item in self.fixture.plan.actions
            if item.record_kind == "attachment"
        )
        attachment = make_execution_operation(
            plan_action_id=attachment_action.action_id,
            operation_type="create_attachment_metadata",
            payload={
                "attachment_id": "attachment_" + "3" * 32,
                "message_id": "message_" + "2" * 32,
                "source_identity": attachment_action.source_identity,
                "state": "metadata_only",
                "safe_metadata": {"kind": "fixture"},
            },
            dependency_operation_ids=(message.operation_id,),
        )
        conversation_action = next(
            item
            for item in self.fixture.plan.actions
            if item.record_kind == "conversation"
        )
        binding = make_execution_operation(
            plan_action_id=conversation_action.action_id,
            operation_type="bind_source_record",
            payload={
                "source_type": "chatgpt",
                "source_identity": conversation_action.source_identity,
                "record_kind": "conversation",
                "target_id": "conversation_" + "1" * 32,
            },
            dependency_operation_ids=(conversation.operation_id,),
        )
        metadata = make_execution_operation(
            plan_action_id=conversation_action.action_id,
            operation_type="update_safe_metadata",
            payload={
                "record_kind": "conversation",
                "target_id": "conversation_" + "1" * 32,
                "safe_metadata": {"updated": True},
            },
            dependency_operation_ids=(conversation.operation_id,),
        )
        operations = (
            conversation,
            register_asset,
            message,
            attachment,
            binding,
            metadata,
        )
        approval, batch = approve_fixture(
            self.fixture,
            operations=operations,
        )
        preflight = self.preflight(operations, approval, batch)
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(
            {
                "conversations": 1,
                "messages": 1,
                "attachments": 1,
                "entities": 1,
                "source_bindings": 1,
            },
            dict(result.verification.actual_deltas),
        )

    def test_unknown_operation_type_is_fail_closed(self) -> None:
        action = self.fixture.plan.actions[0]
        with self.assertRaisesRegex(
            IngestionExecutionError,
            "unknown_operation_type",
        ):
            make_execution_operation(
                plan_action_id=action.action_id,
                operation_type="execute_arbitrary_sql",
                payload={},
            )

    def test_rejected_operation_is_not_applied(self) -> None:
        first = self.fixture.operations[0]
        second = make_execution_operation(
            plan_action_id=first.plan_action_id,
            operation_type="create_conversation",
            payload={
                "conversation_id": "conversation_" + "9" * 32,
                "source_identity": "source_conversation_" + "9" * 64,
            },
        )
        operations = (first, second)
        approval, batch = approve_fixture(
            self.fixture,
            operations=operations,
            approved_ids=(first.operation_id,),
            rejected_ids=(second.operation_id,),
        )
        preflight = self.preflight(operations, approval, batch)
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertNotIn(
            second.operation_id,
            result.applied_operation_ids,
        )

    def test_unapproved_dependency_blocks_preflight(self) -> None:
        first = self.fixture.operations[0]
        second = make_execution_operation(
            plan_action_id=first.plan_action_id,
            operation_type="create_conversation",
            payload={
                "conversation_id": "conversation_" + "8" * 32,
                "source_identity": "source_conversation_" + "8" * 64,
            },
            dependency_operation_ids=(first.operation_id,),
        )
        operations = (first, second)
        approval, batch = approve_fixture(
            self.fixture,
            operations=operations,
            approved_ids=(second.operation_id,),
            rejected_ids=(first.operation_id,),
        )
        result = self.preflight(operations, approval, batch)
        self.assertFalse(result.passed)
        self.assertIn(
            "operation_dependency_invalid",
            result.reason_codes,
        )

    def test_foreign_key_failure_rolls_back_transaction(self) -> None:
        message_action = next(
            item
            for item in self.fixture.plan.actions
            if item.record_kind == "message"
        )
        message = make_execution_operation(
            plan_action_id=message_action.action_id,
            operation_type="create_message",
            payload={
                "message_id": "message_" + "5" * 32,
                "conversation_id": "conversation_" + "5" * 32,
                "source_identity": message_action.source_identity,
                "content_hash": "5" * 64,
            },
        )
        approval, batch = approve_fixture(
            self.fixture,
            operations=(message,),
        )
        preflight = self.preflight((message,), approval, batch)
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(
            self.fixture.before.target_fingerprint,
            capture_test_target_snapshot(
                self.fixture.target.database_path,
                self.fixture.target.asset_root,
            ).target_fingerprint,
        )

    def test_duplicate_identity_failure_rolls_back_every_operation(self) -> None:
        first = self.fixture.operations[0]
        duplicate = make_execution_operation(
            plan_action_id=first.plan_action_id,
            operation_type="create_conversation",
            payload={
                "conversation_id": "conversation_" + "1" * 32,
                "source_identity": "source_conversation_" + "7" * 64,
            },
        )
        operations = (first, duplicate)
        approval, batch = approve_fixture(
            self.fixture,
            operations=operations,
        )
        preflight = self.preflight(operations, approval, batch)
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertEqual("failed", result.status)
        with closing(sqlite3.connect(
            self.fixture.target.database_path
        )) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0],
            )

    def test_replay_of_completed_bundle_is_rejected(self) -> None:
        preflight = self.preflight()
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertEqual("completed", result.status)
        with self.assertRaisesRegex(
            IngestionExecutionError,
            "target_snapshot_changed|execution_already_exists",
        ):
            execute_test_bundle(
                bundle=preflight.bundle,
                target=self.fixture.target,
                before_snapshot=self.fixture.before,
                staged_entities=(),
            )


if __name__ == "__main__":
    unittest.main()
