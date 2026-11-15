from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memory.ingestion_adapters.chatgpt import (
    ALL_REACHABLE_AUDIT,
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
)
from memory.ingestion_assets import AssetStageLimits, build_asset_stage_plan
from memory.production_real_import_authorization import (
    ProductionRealImportAuthorization,
)
from memory.production_real_import_entry import (
    ProductionRealImportExecutionError,
    ProductionRealImportMaterial,
    _hash_file,
    _insert_database_transaction,
    _legacy_preservation_digest,
    _make_exact_precommit_copy,
    _restore_exact_precommit_copy,
    _tree_gate,
    _verify_and_finalize,
)
from tests.test_v83_asset_stage_plan import asset_part
from tests.v83_chatgpt_adapter_fixtures import (
    PNG,
    simple_conversation,
    write_zip_source,
)
from tests.v83_migration_fixtures import (
    create_legacy_database,
    migrate_database,
)
from tests.v83_production_real_import_fixtures import (
    build_test_authorization,
    build_test_package,
)
from tools.v83_real_import_readiness import (
    _build_target_snapshot,
    _database_audit,
)


class ProductionRealImportRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ayunos-real-import-transaction-"
        )
        self.root = Path(self.temporary.name)
        self.source = write_zip_source(
            self.root,
            [
                simple_conversation(
                    conversation_id="fixture-conversation-1",
                    title="Fixture",
                    assistant_parts=[
                        asset_part(
                            "asset-1",
                            "assets/image.png",
                            PNG,
                            "image/png",
                        )
                    ],
                )
            ],
            assets={"assets/image.png": PNG},
        )
        self.adapter = ChatGPTAdapter(
            self.source,
            timestamp=DETERMINISTIC_TIMESTAMP,
        )
        self.bundle = self.adapter.build_normalized_bundle(
            batch_mode="incremental",
            branch_policy=ALL_REACHABLE_AUDIT,
        )
        self.database = create_legacy_database(
            self.root / "main.db",
            with_rows=False,
        )
        conversation = self.bundle.conversations[0]
        existing = self.bundle.messages[0]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    conversation.provenance.source_record_id,
                    conversation.title,
                    1.0,
                    existing.created_at,
                    2.0,
                    existing.updated_at,
                    "fixture.json",
                    "fixture-model",
                    0,
                    0,
                    1,
                    1,
                    0,
                    len(existing.text),
                    "",
                    0.0,
                    "chatgpt",
                    "{}",
                    "general",
                ),
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    existing.provenance.source_record_id,
                    existing.source_node_id,
                    existing.source_conversation_id,
                    conversation.title,
                    0,
                    existing.parent_source_message_id,
                    "[]",
                    existing.role,
                    existing.author,
                    1.0,
                    existing.created_at,
                    1.0,
                    existing.updated_at,
                    existing.content_type,
                    existing.text,
                    existing.status,
                    existing.weight,
                    "{}",
                    "chatgpt",
                    "general",
                ),
            )
            connection.commit()
        migrate_database(
            self.database,
            rehearsal_id="rehearsal_real_import_transaction_001",
        )
        audit = _database_audit("Main", self.database)
        target, _dedup, new_messages = _build_target_snapshot(
            self.bundle,
            self.database,
            audit,
        )
        result = self.adapter.produce_import_plan(
            target_snapshot=target,
            batch_mode="incremental",
            branch_policy=ALL_REACHABLE_AUDIT,
        )
        self.plan = result.import_plan
        self.assertEqual(1, len(new_messages))
        self.stage_plan = build_asset_stage_plan(
            import_plan=self.plan,
            attachments=self.bundle.attachments,
            asset_entities=self.bundle.asset_entities,
            occurrences=None,
            limits=AssetStageLimits(max_operation_seconds=900.0),
        )
        self.material = ProductionRealImportMaterial(
            source_manifest=result.manifest,
            normalized_bundle=self.bundle,
            import_plan=self.plan,
            stage_plan=self.stage_plan,
            exact_envelope={"created_at": DETERMINISTIC_TIMESTAMP},
            new_message_ids=frozenset(
                item.provenance.source_record_id for item in new_messages
            ),
        )
        self.asset_root = self.root / "data" / "ingestion_assets"
        self.database_before_sha = _hash_file(self.database)
        self.schema = audit["schema_fingerprint"]
        self.package = build_test_package(
            now=datetime.now(timezone.utc),
            database_sha256=self.database_before_sha,
            database_schema=self.schema,
            database_size=self.database.stat().st_size,
            archive_sha256=_hash_file(self.source),
            archive_size=self.source.stat().st_size,
            source_manifest_digest="e" * 64,
            exact_plan_digest="f" * 64,
            before_counts={
                "conversations": 1,
                "messages": 1,
                "assets": 0,
            },
            after_counts={
                "conversations": 1,
                "messages": 2,
                "assets": 0,
                "ingestion_asset_entities": 1,
            },
            asset_count=1,
            asset_bytes=len(PNG),
        )
        self.authorization = ProductionRealImportAuthorization.from_mapping(
            build_test_authorization(self.package)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pending_assets(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for operation in self.stage_plan.operations:
            target = (
                self.asset_root
                / "_pending"
                / self.authorization.one_time_execution_id
                / "sha256"
                / operation.entity_digest[:2]
                / operation.entity_digest
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PNG)
            self.assertEqual(operation.entity_digest, _hash_file(target))
            result[operation.entity_digest] = target
        return result

    def test_exact_transaction_and_verification_complete_on_clone(self) -> None:
        legacy = _legacy_preservation_digest(self.database)
        created = _insert_database_transaction(
            database=self.database,
            asset_root=self.asset_root,
            material=self.material,
            package=self.package,
            authorization=self.authorization,
            pending=self.pending_assets(),
            project_root=self.root,
        )
        self.assertEqual(1, len(created))
        verification = _verify_and_finalize(
            database=self.database,
            asset_root=self.asset_root,
            project_root=self.root,
            material=self.material,
            package=self.package,
            authorization=self.authorization,
            legacy_digest_before=legacy,
        )
        self.assertRegex(verification["verification_digest"], r"^[0-9a-f]{64}$")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                2,
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM messages_fts"
                ).fetchone()[0],
            )
            self.assertEqual(
                ("completed",),
                connection.execute(
                    "SELECT execution_state FROM ingestion_executions"
                ).fetchone(),
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM ingestion_leases"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM ingestion_verifications"
                ).fetchone()[0],
            )
        self.assertEqual(
            1,
            _tree_gate(
                self.asset_root,
                project_root=self.root,
            )["inventory_count"],
        )

    def test_writer_lease_conflict_rolls_back_database_and_pending_assets(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO ingestion_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    "production_main",
                    "real_chatgpt_import",
                    "other_owner",
                    "1" * 64,
                    DETERMINISTIC_TIMESTAMP,
                    DETERMINISTIC_TIMESTAMP,
                    "active",
                    "v8.3-e-writer-lease-1",
                ),
            )
            connection.commit()
        before = _hash_file(self.database)
        pending = self.pending_assets()
        with self.assertRaisesRegex(
            ProductionRealImportExecutionError,
            "writer_lease_conflict",
        ):
            _insert_database_transaction(
                database=self.database,
                asset_root=self.asset_root,
                material=self.material,
                package=self.package,
                authorization=self.authorization,
                pending=pending,
                project_root=self.root,
            )
        self.assertEqual(before, _hash_file(self.database))
        self.assertFalse(any(path.exists() for path in pending.values()))

    def test_postcommit_recovery_restores_exact_database_and_assets(self) -> None:
        recovery_root = self.root / "execution"
        copy = _make_exact_precommit_copy(
            self.database,
            recovery_root,
            self.database_before_sha,
        )
        created = _insert_database_transaction(
            database=self.database,
            asset_root=self.asset_root,
            material=self.material,
            package=self.package,
            authorization=self.authorization,
            pending=self.pending_assets(),
            project_root=self.root,
        )
        self.assertNotEqual(self.database_before_sha, _hash_file(self.database))
        _restore_exact_precommit_copy(
            database=self.database,
            precommit_copy=copy,
            expected_sha256=self.database_before_sha,
            created_assets=created,
            asset_root=self.asset_root,
            root=recovery_root,
            authorization=self.authorization,
            reason_code="injected_postcommit_verification_failure",
            asset_root_existed_before=False,
        )
        self.assertEqual(self.database_before_sha, _hash_file(self.database))
        self.assertFalse(any(path.exists() for path in created))
        self.assertTrue((recovery_root / "recovery_required.json").is_file())
        self.assertEqual(
            "absent",
            _tree_gate(
                self.asset_root,
                project_root=self.root,
            )["root_state"],
        )

    def test_pretransaction_asset_revalidation_rejects_toctou_drift(self) -> None:
        pending = self.pending_assets()
        rogue = self.asset_root / "rogue.bin"
        rogue.write_bytes(b"not-approved")
        with self.assertRaisesRegex(
            ProductionRealImportExecutionError,
            "asset_baseline_mismatch",
        ):
            _insert_database_transaction(
                database=self.database,
                asset_root=self.asset_root,
                material=self.material,
                package=self.package,
                authorization=self.authorization,
                pending=pending,
                project_root=self.root,
            )
        self.assertEqual(self.database_before_sha, _hash_file(self.database))


if __name__ == "__main__":
    unittest.main()
