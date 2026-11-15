from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from memory.ingestion_asset_staging import (
    AssetStagingError,
    PRODUCTION_EXECUTION_STAGING_ROOT,
    ZipAssetSource,
    execute_asset_stage_plan,
    prepare_batch_paths,
    read_stage_ledger_events,
    validate_stage_root,
)
from tests.test_v83_asset_stage_plan import build_fixture


class AssetStagingSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_root = self.root / "stage"
        self.fixture_index = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runtime_fixture(self, *, batch_mode: str = "full"):
        self.fixture_index += 1
        source, _, plan = build_fixture(
            self.root / f"fixture-{self.fixture_index}",
            batch_mode=batch_mode,
        )
        runtime = ZipAssetSource(
            plan.operations[0].operation_id,
            source,
            "assets/image.png",
        )
        return source, plan, runtime

    def test_stage_root_must_be_temp_or_node_output(self) -> None:
        outside = Path(__file__).resolve().parents[1] / "private-asset-stage"
        with self.assertRaisesRegex(
            AssetStagingError, "stage_root_outside_allowed_roots"
        ):
            validate_stage_root(outside)
        self.assertFalse(outside.exists())

    def test_production_execution_stage_root_is_v3_only(self) -> None:
        current = (
            PRODUCTION_EXECUTION_STAGING_ROOT
            / "execution_real_import_test_001"
            / "stage"
        )
        self.assertEqual(current.absolute(), validate_stage_root(current))
        old = (
            Path(__file__).resolve().parents[1]
            / "output"
            / "v8.3-real-import-production-entry"
            / "executions"
            / "execution_real_import_test_001"
            / "stage"
        )
        with self.assertRaisesRegex(
            AssetStagingError,
            "stage_root_outside_allowed_roots",
        ):
            validate_stage_root(old)

    def test_traversal_and_encoded_traversal_roots_are_rejected(self) -> None:
        values = (
            os.fspath(self.root / ".." / "escape"),
            os.fspath(self.root / "%2e%2e" / "escape"),
            os.fspath(self.root / "%252e%252e" / "escape"),
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(AssetStagingError, "unsafe_stage_root"):
                    validate_stage_root(value)

    def test_windows_device_unc_and_ads_roots_are_rejected(self) -> None:
        values = (
            r"\\server\share\stage",
            r"\\?\C:\Temp\stage",
            r"\\.\C:\Temp\stage",
            r"C:\Temp\stage:stream",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(AssetStagingError, "unsafe_stage_root"):
                    validate_stage_root(value)

    def test_trailing_dot_and_space_roots_are_rejected(self) -> None:
        for suffix in ("unsafe.", "unsafe "):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(AssetStagingError, "unsafe_stage_root"):
                    validate_stage_root(self.root / suffix)

    def test_invalid_batch_id_cannot_create_batch_directory(self) -> None:
        with self.assertRaisesRegex(AssetStagingError, "invalid_batch_id"):
            prepare_batch_paths(self.stage_root, "../other-batch")

    def test_root_itself_as_reparse_point_is_rejected_without_skip(self) -> None:
        self.stage_root.mkdir()
        with patch(
            "memory.ingestion_asset_staging.is_reparse_point",
            side_effect=lambda value: Path(value) == self.stage_root,
        ):
            with self.assertRaisesRegex(
                AssetStagingError, "stage_root_reparse_forbidden"
            ):
                validate_stage_root(self.stage_root)

    def test_child_junction_or_reparse_component_is_rejected(self) -> None:
        parent = self.root / "parent"
        parent.mkdir()
        child = parent / "junction"
        child.mkdir()
        with patch(
            "memory.ingestion_asset_staging.is_reparse_point",
            side_effect=lambda value: Path(value) == child,
        ):
            with self.assertRaisesRegex(
                AssetStagingError, "stage_root_reparse_forbidden"
            ):
                validate_stage_root(child / "stage")

    def test_zip_member_traversal_variants_are_rejected(self) -> None:
        source, plan, _ = self.runtime_fixture()
        values = (
            "../secret",
            r"..\secret",
            "%2e%2e/secret",
            "%252e%252e/secret",
            "C:/secret",
            "//server/share/file",
            "//?/C:/secret",
            "//./C:/secret",
            "safe/file:stream",
            "safe/trailing.",
            "safe/trailing ",
        )
        for member_name in values:
            with self.subTest(member_name=member_name):
                with self.assertRaisesRegex(AssetStagingError, "unsafe_zip_member"):
                    ZipAssetSource(
                        plan.operations[0].operation_id,
                        source,
                        member_name,
                    )

    def test_external_url_member_is_rejected(self) -> None:
        source, plan, _ = self.runtime_fixture()
        with self.assertRaisesRegex(AssetStagingError, "unsafe_zip_member"):
            ZipAssetSource(
                plan.operations[0].operation_id,
                source,
                "https://example.invalid/private",
            )

    def test_zip_symlink_entry_is_rejected(self) -> None:
        archive, plan, runtime = self.runtime_fixture()
        link = zipfile.ZipInfo(runtime.member_name)
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(link, b"outside-target")
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        self.assertEqual("failed", result.status)
        self.assertIn("unsafe_zip_member", result.reason_codes)

    def test_source_archive_reparse_point_is_rejected(self) -> None:
        archive, plan, runtime = self.runtime_fixture()
        with patch(
            "memory.ingestion_asset_staging.is_reparse_point",
            side_effect=lambda value: Path(value) == archive.absolute(),
        ):
            result = execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
            )
        self.assertEqual("failed", result.status)
        self.assertIn("unsafe_source_archive", result.reason_codes)

    def test_verified_target_reparse_point_fails_closed(self) -> None:
        _, plan, runtime = self.runtime_fixture()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        target = paths.verified / ("sha256_" + plan.operations[0].entity_digest)
        target.write_bytes(b"placeholder")
        with patch(
            "memory.ingestion_asset_staging.is_reparse_point",
            side_effect=lambda value: Path(value) == target,
        ):
            result = execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
            )
        self.assertEqual("failed", result.status)
        self.assertIn("verified_entity_path_unsafe", result.reason_codes)

    def test_no_network_socket_is_opened(self) -> None:
        _, plan, runtime = self.runtime_fixture()
        with patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("network forbidden"),
        ) as connect:
            result = execute_asset_stage_plan(
                plan=plan,
                sources=(runtime,),
                stage_root=self.stage_root,
            )
        self.assertEqual("staged", result.status)
        connect.assert_not_called()

    def test_source_filename_token_cookie_and_tailnet_text_do_not_reach_ledger(self) -> None:
        private_member = "token_cookie_tailnet_private.png"
        source, _, plan = build_fixture(
            self.root / "privacy-fixture",
            {private_member: b"\x89PNG\r\n\x1a\nprivate"},
            parts=[{
                "type": "image_asset_pointer",
                "id": "private-attachment",
                "asset_pointer": private_member,
                "name": private_member,
                "mime_type": "image/png",
                "size_bytes": len(b"\x89PNG\r\n\x1a\nprivate"),
            }],
        )
        runtime = ZipAssetSource(
            plan.operations[0].operation_id,
            source,
            private_member,
        )
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        events = read_stage_ledger_events(self.stage_root, plan.batch_id)
        encoded = str(events).lower()
        self.assertEqual("staged", result.status)
        for private_text in ("token", "cookie", "tailnet", private_member):
            self.assertNotIn(private_text, encoded)

    def test_stage_plan_and_result_contain_no_absolute_path(self) -> None:
        _, plan, runtime = self.runtime_fixture()
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        self.assertNotIn(str(self.root), str(plan.to_dict()))
        self.assertNotIn(str(self.root), str(result.to_dict()))

    def test_batch_directories_are_isolated(self) -> None:
        _, first_plan, first_runtime = self.runtime_fixture()
        _, second_plan, second_runtime = self.runtime_fixture(
            batch_mode="incremental"
        )
        execute_asset_stage_plan(
            plan=first_plan,
            sources=(first_runtime,),
            stage_root=self.stage_root,
        )
        execute_asset_stage_plan(
            plan=second_plan,
            sources=(second_runtime,),
            stage_root=self.stage_root,
        )
        first = prepare_batch_paths(self.stage_root, first_plan.batch_id)
        second = prepare_batch_paths(self.stage_root, second_plan.batch_id)
        self.assertNotEqual(first.batch, second.batch)
        self.assertTrue(first.verified.is_dir())
        self.assertTrue(second.verified.is_dir())

    def test_unknown_extra_file_is_not_touched_by_staging(self) -> None:
        _, plan, runtime = self.runtime_fixture()
        paths = prepare_batch_paths(self.stage_root, plan.batch_id)
        unknown = paths.quarantine / "user-owned-note"
        unknown.write_text("keep", encoding="utf-8")
        result = execute_asset_stage_plan(
            plan=plan,
            sources=(runtime,),
            stage_root=self.stage_root,
        )
        self.assertEqual("staged", result.status)
        self.assertEqual("keep", unknown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
