from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import unicodedata

from memory.asset_baseline import (
    ASSET_BASELINE_SCHEMA_VERSION,
    AssetBaselineError,
    build_asset_baseline,
    capture_asset_baseline,
    require_asset_baseline_match,
    validate_asset_baseline,
)
from memory.ingestion_core import canonical_json_bytes
from memory.production_real_import_entry import _tree_gate
from tests.v83_production_real_import_fixtures import build_test_package
from tools.v83_real_import_readiness import _production_asset_gate


class AssetBaselineContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ayunos-asset-baseline-"
        )
        self.project = Path(self.temporary.name)
        self.root = self.project / "data" / "ingestion_assets"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self) -> dict[str, object]:
        return capture_asset_baseline(
            self.root,
            project_root=self.project,
        )

    def test_absent_root_has_explicit_state_and_canonical_empty_inventory(self) -> None:
        baseline = self.capture()
        self.assertEqual(ASSET_BASELINE_SCHEMA_VERSION, baseline["schema_version"])
        self.assertEqual("absent", baseline["root_state"])
        self.assertEqual(0, baseline["inventory_count"])
        self.assertEqual(0, baseline["physical_bytes"])
        self.assertEqual(
            hashlib.sha256(canonical_json_bytes([])).hexdigest(),
            baseline["inventory_digest"],
        )

    def test_empty_existing_root_is_not_absent(self) -> None:
        absent = self.capture()
        self.root.mkdir(parents=True)
        empty = self.capture()
        self.assertEqual("empty", empty["root_state"])
        self.assertEqual(absent["inventory_digest"], empty["inventory_digest"])
        self.assertNotEqual(absent["baseline_digest"], empty["baseline_digest"])

    def test_populated_root_binds_file_content_size_and_type(self) -> None:
        target = self.root / "sha256" / "ab" / "asset.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"asset")
        baseline = self.capture()
        self.assertEqual("populated", baseline["root_state"])
        self.assertEqual(1, baseline["inventory_count"])
        self.assertEqual(5, baseline["physical_bytes"])
        self.assertEqual("file", baseline["inventory"][0]["entry_type"])
        self.assertEqual(
            hashlib.sha256(b"asset").hexdigest(),
            baseline["inventory"][0]["sha256"],
        )

    def test_generator_and_runtime_are_identical_for_absent_root(self) -> None:
        with patch(
            "tools.v83_real_import_readiness.PROJECT_ROOT",
            self.project,
        ):
            generated = _production_asset_gate(self.root)
        runtime = _tree_gate(self.root, project_root=self.project)
        self.assertEqual(generated, runtime)

    def test_generator_and_runtime_are_identical_for_empty_root(self) -> None:
        self.root.mkdir(parents=True)
        with patch(
            "tools.v83_real_import_readiness.PROJECT_ROOT",
            self.project,
        ):
            generated = _production_asset_gate(self.root)
        runtime = _tree_gate(self.root, project_root=self.project)
        self.assertEqual(generated, runtime)

    def test_generator_and_runtime_are_identical_for_populated_root(self) -> None:
        target = self.root / "nested" / "asset.dat"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"bytes")
        with patch(
            "tools.v83_real_import_readiness.PROJECT_ROOT",
            self.project,
        ):
            generated = _production_asset_gate(self.root)
        runtime = _tree_gate(self.root, project_root=self.project)
        self.assertEqual(generated, runtime)

    def test_canonical_output_is_stable(self) -> None:
        for name, value in (("b.bin", b"b"), ("a.bin", b"a")):
            target = self.root / "nested" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        first = self.capture()
        second = self.capture()
        self.assertEqual(first, second)
        self.assertEqual(
            ["nested/a.bin", "nested/b.bin"],
            [item["relative_path"] for item in first["inventory"]],
        )

    def test_file_creation_after_preflight_is_mismatch(self) -> None:
        approved = self.capture()
        self.root.mkdir(parents=True)
        (self.root / "new.bin").write_bytes(b"new")
        with self.assertRaisesRegex(AssetBaselineError, "asset_baseline_mismatch"):
            require_asset_baseline_match(approved, self.capture())

    def test_file_deletion_after_preflight_is_mismatch(self) -> None:
        self.root.mkdir(parents=True)
        target = self.root / "asset.bin"
        target.write_bytes(b"asset")
        approved = self.capture()
        target.unlink()
        with self.assertRaisesRegex(AssetBaselineError, "asset_baseline_mismatch"):
            require_asset_baseline_match(approved, self.capture())

    def test_file_content_modification_is_mismatch(self) -> None:
        self.root.mkdir(parents=True)
        target = self.root / "asset.bin"
        target.write_bytes(b"one")
        approved = self.capture()
        target.write_bytes(b"two")
        with self.assertRaisesRegex(AssetBaselineError, "asset_baseline_mismatch"):
            require_asset_baseline_match(approved, self.capture())

    def test_file_size_modification_is_mismatch(self) -> None:
        self.root.mkdir(parents=True)
        target = self.root / "asset.bin"
        target.write_bytes(b"one")
        approved = self.capture()
        target.write_bytes(b"longer")
        with self.assertRaisesRegex(AssetBaselineError, "asset_baseline_mismatch"):
            require_asset_baseline_match(approved, self.capture())

    def test_nested_paths_use_posix_separators(self) -> None:
        target = self.root / "one" / "two" / "asset.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"asset")
        relative = self.capture()["inventory"][0]["relative_path"]
        self.assertEqual("one/two/asset.bin", relative)
        self.assertNotIn("\\", relative)

    def test_noncanonical_windows_separator_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            AssetBaselineError,
            "asset_relative_path_not_canonical",
        ):
            build_asset_baseline(
                root_state="populated",
                inventory=[{
                    "relative_path": "one\\asset.bin",
                    "size": 1,
                    "sha256": "a" * 64,
                    "entry_type": "file",
                }],
            )

    def test_unicode_paths_are_normalized_to_nfc(self) -> None:
        decomposed = unicodedata.normalize("NFD", "café.bin")
        target = self.root / decomposed
        target.parent.mkdir(parents=True)
        target.write_bytes(b"asset")
        relative = self.capture()["inventory"][0]["relative_path"]
        self.assertEqual(unicodedata.normalize("NFC", decomposed), relative)

    def test_case_is_preserved_and_casefold_collisions_are_rejected(self) -> None:
        target = self.root / "MixedCase.BIN"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"asset")
        self.assertEqual(
            "MixedCase.BIN",
            self.capture()["inventory"][0]["relative_path"],
        )
        with self.assertRaisesRegex(
            AssetBaselineError,
            "asset_relative_path_collision",
        ):
            build_asset_baseline(
                root_state="populated",
                inventory=[
                    {
                        "relative_path": "A.bin",
                        "size": 1,
                        "sha256": "a" * 64,
                        "entry_type": "file",
                    },
                    {
                        "relative_path": "a.bin",
                        "size": 1,
                        "sha256": "b" * 64,
                        "entry_type": "file",
                    },
                ],
            )

    def test_inventory_order_tamper_is_rejected(self) -> None:
        baseline = build_asset_baseline(
            root_state="populated",
            inventory=[
                {
                    "relative_path": "a.bin",
                    "size": 1,
                    "sha256": "a" * 64,
                    "entry_type": "file",
                },
                {
                    "relative_path": "b.bin",
                    "size": 1,
                    "sha256": "b" * 64,
                    "entry_type": "file",
                },
            ],
        )
        tampered = dict(baseline)
        tampered["inventory"] = list(reversed(tampered["inventory"]))
        payload = dict(tampered)
        payload.pop("baseline_digest")
        tampered["baseline_digest"] = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
        with self.assertRaisesRegex(
            AssetBaselineError,
            "asset_inventory_order_invalid",
        ):
            validate_asset_baseline(tampered)

    def test_symlink_root_is_rejected(self) -> None:
        self.root.mkdir(parents=True)
        original = Path.is_symlink
        with patch.object(
            Path,
            "is_symlink",
            autospec=True,
            side_effect=lambda candidate: (
                candidate == self.root or original(candidate)
            ),
        ):
            with self.assertRaisesRegex(
                AssetBaselineError,
                "asset_root_reparse_forbidden",
            ):
                self.capture()

    def test_junction_or_reparse_root_is_rejected(self) -> None:
        self.root.mkdir(parents=True)
        with patch(
            "memory.asset_baseline.is_reparse_point",
            side_effect=lambda candidate: candidate == self.root,
        ):
            with self.assertRaisesRegex(
                AssetBaselineError,
                "asset_root_reparse_forbidden",
            ):
                self.capture()

    def test_unc_and_device_roots_are_rejected(self) -> None:
        for value in (
            r"\\server\share\assets",
            r"\\?\E:\AyunOS\data\ingestion_assets",
            r"\\.\E:\AyunOS\data\ingestion_assets",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                AssetBaselineError,
                "asset_root_device_or_unc_forbidden",
            ):
                capture_asset_baseline(value, project_root=self.project)

    def test_old_package_schema_is_rejected(self) -> None:
        package = build_test_package()
        package["package_schema_version"] = (
            "v8.3-real-import-approval-package-2"
        )
        from memory.production_real_import_authorization import (
            ProductionRealImportAuthorizationError,
            canonical_document_digest,
            validate_approval_package,
        )

        package["package_digest"] = canonical_document_digest(
            package,
            digest_field="package_digest",
        )
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "approval_package_schema_mismatch",
        ):
            validate_approval_package(package)


if __name__ == "__main__":
    unittest.main()
