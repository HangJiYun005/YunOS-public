from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_migrations import (
    MIGRATION_DIGEST,
    build_migration_plan,
    inspect_schema,
)
from memory.production_migration_authorization import (
    AUTHORIZATION_SCOPE,
    ProductionMigrationAuthorization,
    ProductionMigrationAuthorizationError,
    canonical_document_digest,
    migration_plan_digest,
    validate_authorization_binding,
    validate_approval_package,
)
from tests.v83_migration_fixtures import create_legacy_database
from tests.v83_production_migration_fixtures import (
    build_test_approval_package,
    build_test_authorization_document,
    hash_file,
    utc_text,
)


class ProductionMigrationAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v8301-auth-")
        self.root = Path(self.temp.name)
        self.database = create_legacy_database(self.root / "source.db")
        self.plan = build_migration_plan(inspect_schema(self.database))
        self.database_sha = hash_file(self.database)
        self.now = datetime.now(timezone.utc)
        self.package = build_test_approval_package(
            plan=self.plan,
            source_database_sha256=self.database_sha,
            source_database_size=self.database.stat().st_size,
            now=self.now,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def authorization(self, **overrides):
        document = build_test_authorization_document(
            package=self.package,
            plan=self.plan,
            source_database_sha256=self.database_sha,
            now=self.now,
            overrides=overrides,
        )
        return ProductionMigrationAuthorization.from_mapping(document)

    def assert_binding_refused(self, field: str, value, reason: str) -> None:
        authorization = self.authorization(**{field: value})
        with self.assertRaisesRegex(ProductionMigrationAuthorizationError, reason):
            validate_authorization_binding(
                authorization,
                self.package,
                expected_database_sha256=self.database_sha,
                expected_current_schema=self.plan.source_schema_fingerprint,
                expected_target_schema=self.plan.expected_target_schema_fingerprint,
                expected_migration_digest=MIGRATION_DIGEST,
                expected_plan_digest=migration_plan_digest(self.plan),
                current_code_commit=self.package["code_commit"],
                now=self.now,
            )

    def test_exact_authorization_and_package_bind_all_fingerprints(self) -> None:
        authorization = self.authorization()
        validate_authorization_binding(
            authorization,
            self.package,
            expected_database_sha256=self.database_sha,
            expected_current_schema=self.plan.source_schema_fingerprint,
            expected_target_schema=self.plan.expected_target_schema_fingerprint,
            expected_migration_digest=MIGRATION_DIGEST,
            expected_plan_digest=migration_plan_digest(self.plan),
            current_code_commit=self.package["code_commit"],
            now=self.now,
        )
        self.assertEqual(AUTHORIZATION_SCOPE, authorization.authorization_scope)
        self.assertFalse(self.package["production_execution_authorized"])

    def test_binding_drift_is_rejected(self) -> None:
        cases = (
            ("approval_package_digest", "0" * 64, "package_digest_mismatch"),
            ("migration_plan_digest", "1" * 64, "plan_digest_mismatch"),
            ("source_database_sha256", "2" * 64, "database_digest_mismatch"),
            ("source_schema_fingerprint", "3" * 64, "source_schema_mismatch"),
            ("target_schema_fingerprint", "4" * 64, "target_schema_mismatch"),
            ("code_commit", "5" * 40, "code_commit_mismatch"),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                self.assert_binding_refused(field, value, reason)

    def test_scope_target_and_migration_digest_are_fixed(self) -> None:
        cases = (
            (
                {"authorization_scope": "v8302_production_migration"},
                "authorization_scope_mismatch",
            ),
            ({"migration_version": 8302}, "authorization_target_version_mismatch"),
            ({"migration_digest": "f" * 64}, "authorization_migration_digest_mismatch"),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                document = build_test_authorization_document(
                    package=self.package,
                    plan=self.plan,
                    source_database_sha256=self.database_sha,
                    now=self.now,
                    overrides=overrides,
                )
                with self.assertRaisesRegex(
                    ProductionMigrationAuthorizationError,
                    reason,
                ):
                    ProductionMigrationAuthorization.from_mapping(document)

    def test_expired_authorization_is_rejected(self) -> None:
        authorization = self.authorization(
            authorized_at=utc_text(self.now - timedelta(hours=2)),
            expires_at=utc_text(self.now - timedelta(hours=1)),
        )
        with self.assertRaisesRegex(
            ProductionMigrationAuthorizationError,
            "authorization_expired",
        ):
            authorization.validate_time(now=self.now)

    def test_authorization_and_user_text_digest_tampering_is_rejected(self) -> None:
        document = build_test_authorization_document(
            package=self.package,
            plan=self.plan,
            source_database_sha256=self.database_sha,
            now=self.now,
        )
        document["authorization_digest"] = "f" * 64
        with self.assertRaisesRegex(
            ProductionMigrationAuthorizationError,
            "authorization_digest_mismatch",
        ):
            ProductionMigrationAuthorization.from_mapping(document)

        document = build_test_authorization_document(
            package=self.package,
            plan=self.plan,
            source_database_sha256=self.database_sha,
            now=self.now,
        )
        document["user_authorization_text_digest"] = "e" * 64
        with self.assertRaisesRegex(
            ProductionMigrationAuthorizationError,
            "authorization_digest_mismatch",
        ):
            ProductionMigrationAuthorization.from_mapping(document)

    def test_package_tampering_and_old_schema_are_rejected(self) -> None:
        tampered = dict(self.package)
        tampered["code_commit"] = "e" * 40
        with self.assertRaisesRegex(
            ProductionMigrationAuthorizationError,
            "approval_package_digest_mismatch",
        ):
            validate_approval_package(tampered, now=self.now)

        old = dict(self.package)
        old["package_schema_version"] = "v8.3-production-v8301-approval-package-1"
        old["package_digest"] = canonical_document_digest(
            old,
            digest_field="package_digest",
        )
        with self.assertRaisesRegex(
            ProductionMigrationAuthorizationError,
            "approval_package_schema_mismatch",
        ):
            validate_approval_package(old, now=self.now)


if __name__ == "__main__":
    unittest.main()
