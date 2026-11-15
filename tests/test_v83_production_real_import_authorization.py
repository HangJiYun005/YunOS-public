from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from memory.production_real_import_authorization import (
    ProductionRealImportAuthorization,
    ProductionRealImportAuthorizationError,
    canonical_document_digest,
    validate_approval_package,
    validate_authorization_binding,
)
from tests.v83_production_real_import_fixtures import (
    build_test_authorization,
    build_test_package,
    utc_text,
)


class ProductionRealImportAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.package = build_test_package(now=self.now)

    def test_valid_package_remains_non_executing_and_requires_human(self) -> None:
        validated = validate_approval_package(self.package, now=self.now)
        self.assertFalse(validated["production_execution_authorized"])
        self.assertFalse(validated["human_authorization_record_generated"])
        self.assertTrue(validated["human_approval_required"])
        self.assertFalse(validated["execution_contract"]["automatic_replay"])

    def test_package_tamper_expiry_and_scope_widening_are_rejected(self) -> None:
        cases = []
        tampered = dict(self.package)
        tampered["new_message_count"] += 1
        cases.append((tampered, "approval_package_digest_mismatch"))

        expired = build_test_package(now=self.now)
        expired["approval_expires_at"] = utc_text(
            self.now - timedelta(seconds=1)
        )
        expired["package_digest"] = canonical_document_digest(
            expired,
            digest_field="package_digest",
        )
        cases.append((expired, "approval_package_expired"))

        widened = dict(self.package)
        widened["production_execution_authorized"] = True
        widened["package_digest"] = canonical_document_digest(
            widened,
            digest_field="package_digest",
        )
        cases.append((widened, "approval_scope_boundary_invalid"))

        changed_contract = dict(self.package)
        changed_contract["execution_contract"] = dict(
            changed_contract["execution_contract"]
        )
        changed_contract["execution_contract"]["automatic_replay"] = True
        changed_contract["package_digest"] = canonical_document_digest(
            changed_contract,
            digest_field="package_digest",
        )
        cases.append((
            changed_contract,
            "approval_execution_contract_mismatch",
        ))
        for package, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(
                    ProductionRealImportAuthorizationError,
                    reason,
                ):
                    validate_approval_package(package, now=self.now)

    def test_human_authorization_binds_every_production_identity(self) -> None:
        authorization = ProductionRealImportAuthorization.from_mapping(
            build_test_authorization(self.package, now=self.now)
        )
        validate_authorization_binding(
            authorization,
            self.package,
            current_code_commit=self.package["code_commit"],
            now=self.now,
        )
        cases = {
            "exact_import_plan_digest": "authorization_plan_digest_mismatch",
            "bound_import_plan_digest": (
                "authorization_bound_plan_digest_mismatch"
            ),
            "source_archive_sha256": "authorization_archive_digest_mismatch",
            "source_database_sha256": "authorization_database_digest_mismatch",
            "source_manifest_digest": "authorization_manifest_digest_mismatch",
            "bound_source_manifest_digest": (
                "authorization_bound_manifest_digest_mismatch"
            ),
            "execution_contract_digest": (
                "authorization_execution_contract_mismatch"
            ),
            "imports_tree_sha256": "authorization_imports_digest_mismatch",
            "asset_baseline_digest": (
                "authorization_asset_baseline_digest_mismatch"
            ),
            "backup_manifest_digest": (
                "authorization_backup_manifest_digest_mismatch"
            ),
            "standing_authorization_text_digest": (
                "authorization_standing_text_digest_mismatch"
            ),
            "code_commit": "authorization_code_commit_mismatch",
            "bound_code_commit": (
                "authorization_bound_code_commit_mismatch"
            ),
        }
        for field, reason in cases.items():
            with self.subTest(field=field):
                document = build_test_authorization(
                    self.package,
                    now=self.now,
                    overrides={
                        field: (
                            "9" * 40
                            if field in {"code_commit", "bound_code_commit"}
                            else "9" * 64
                        )
                    },
                )
                candidate = ProductionRealImportAuthorization.from_mapping(
                    document
                )
                with self.assertRaisesRegex(
                    ProductionRealImportAuthorizationError,
                    reason,
                ):
                    validate_authorization_binding(
                        candidate,
                        self.package,
                        current_code_commit=self.package["code_commit"],
                        now=self.now,
                    )

    def test_standing_authorization_digest_is_exact_not_self_asserted(self) -> None:
        arbitrary = "8" * 64
        authorization = ProductionRealImportAuthorization.from_mapping(
            build_test_authorization(
                self.package,
                now=self.now,
                overrides={
                    "standing_authorization_text_digest": arbitrary,
                    "user_authorization_text_digest": arbitrary,
                },
            )
        )
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "authorization_standing_text_digest_invalid",
        ):
            validate_authorization_binding(
                authorization,
                self.package,
                current_code_commit=self.package["code_commit"],
                now=self.now,
            )

    def test_authorization_tamper_and_old_schema_are_rejected(self) -> None:
        tampered = build_test_authorization(self.package, now=self.now)
        tampered["one_time_execution_id"] = "execution_real_import_tampered_001"
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "authorization_digest_mismatch",
        ):
            ProductionRealImportAuthorization.from_mapping(tampered)

        old = build_test_authorization(self.package, now=self.now)
        old["authorization_schema_version"] = (
            "v8.3-real-import-human-authorization-1"
        )
        old["authorization_digest"] = canonical_document_digest(
            old,
            digest_field="authorization_digest",
        )
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "authorization_schema_mismatch",
        ):
            ProductionRealImportAuthorization.from_mapping(old)

    def test_blanket_or_future_authorization_cannot_bind_unknown_package(self) -> None:
        future = build_test_authorization(
            self.package,
            now=self.now,
            overrides={
                "authorized_at": utc_text(self.now + timedelta(minutes=5)),
                "expires_at": utc_text(self.now + timedelta(minutes=35)),
            },
        )
        authorization = ProductionRealImportAuthorization.from_mapping(future)
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "authorization_not_yet_valid",
        ):
            validate_authorization_binding(
                authorization,
                self.package,
                current_code_commit=self.package["code_commit"],
                now=self.now,
            )

        unknown = build_test_package(
            now=self.now,
            exact_plan_digest="8" * 64,
        )
        with self.assertRaisesRegex(
            ProductionRealImportAuthorizationError,
            "authorization_package_digest_mismatch",
        ):
            validate_authorization_binding(
                ProductionRealImportAuthorization.from_mapping(
                    build_test_authorization(self.package, now=self.now)
                ),
                unknown,
                current_code_commit=unknown["code_commit"],
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
