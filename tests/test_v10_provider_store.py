from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memory.cognitive_provider import (
    ProviderRegistry,
    build_suggested_provider_registry,
)
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    canonical_json,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


T0 = "2026-07-31T00:00:00.000000Z"
T1 = "2026-07-31T00:01:00.000000Z"
T2 = "2026-07-31T00:02:00.000000Z"
T3 = "2026-07-31T00:03:00.000000Z"
T6 = "2026-07-31T02:00:00.000000Z"


class CognitiveProviderStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Path(self.temp.name) / "operational.db"
        initialize_operational_database(self.database, created_at=T0)
        apply_cognitive_state_migration(self.database, created_at=T1)
        self.operational = OperationalStateStore(self.database)
        self.owner = "owner:v10-c-provider-test"
        self.token = "v10-c-provider-test-lease"
        self.operational.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T1,
            expires_at=T6,
        )
        self.trusted_at = T2
        self.store = CognitiveProviderStore(
            self.database,
            trusted_clock=lambda: self.trusted_at,
        )
        self.registry = build_suggested_provider_registry()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def authority(
        *,
        authority_id: str = "authority:v10-c-provider-install",
        issued_by: str = "user:yunos",
        expires_at: str = T6,
        commit_permission: bool = True,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at=expires_at,
            target_project_id="project:yunos",
            allowed_event_types=("provider_registry_install",),
            read_scope=("project:yunos",),
            write_scope=("provider-registry:yunos-v10",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "start_next_version",
            ),
            commit_permission=commit_permission,
        )

    def install(self, **overrides):
        arguments = {
            "registry": self.registry,
            "authority": self.authority(),
            "lease_owner_id": self.owner,
            "lease_token": self.token,
        }
        arguments.update(overrides)
        return self.store.install_builtin_providers(**arguments)

    def insert_profile(self, profile, **overrides) -> None:
        values = {
            "provider_id": profile.provider_id,
            "profile_version": profile.profile_version,
            "display_name": profile.display_name,
            "endpoint": profile.endpoint,
            "model": profile.model,
            "enabled": int(profile.enabled),
            "manual_selection_only": 1,
            "secret_reference": (
                None
                if profile.secret_reference is None
                else profile.secret_reference.reference_id
            ),
            "health_state": profile.health_state,
            "canonical_profile": canonical_json(profile.to_dict()),
            "profile_digest": profile.profile_digest,
            "created_at": T2,
        }
        values.update(overrides)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO provider_registry("
                "provider_id,profile_version,display_name,endpoint,model,"
                "enabled,manual_selection_only,secret_reference,"
                "health_state,canonical_profile,profile_digest,created_at"
                ") VALUES(:provider_id,:profile_version,:display_name,"
                ":endpoint,:model,:enabled,:manual_selection_only,"
                ":secret_reference,:health_state,:canonical_profile,"
                ":profile_digest,:created_at)",
                values,
            )
            connection.commit()

    def insert_manifest(self, *, first_overrides=None) -> None:
        for index, profile in enumerate(self.registry.list_profiles()):
            self.insert_profile(
                profile,
                **(
                    first_overrides
                    if index == 0 and first_overrides is not None
                    else {}
                ),
            )

    def test_uninstalled_registry_is_readonly_empty(self) -> None:
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(self.store.list_providers(), [])
        self.assertIsNone(self.store.get_provider("deepseek"))
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_install_persists_exact_manual_only_manifest(self) -> None:
        result = self.install()
        profiles = self.store.list_providers()
        self.assertEqual(result["inserted"], len(self.registry.list_profiles()))
        self.assertEqual(result["profile_count"], len(profiles))
        self.assertEqual(
            result["registry_digest"],
            canonical_digest(self.registry.to_public_dict()),
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(result["selection_policy"], "manual_only")
        self.assertFalse(result["hidden_fallback"])
        self.assertFalse(result["grants_authority"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["secret_material_read"])
        self.assertTrue(
            all(profile["manual_selection_only"] for profile in profiles)
        )
        self.assertTrue(
            all(not profile["grants_authority"] for profile in profiles)
        )
        self.assertTrue(
            all(not profile["execution_allowed"] for profile in profiles)
        )
        with closing(sqlite3.connect(self.database)) as connection:
            references = [
                row[0]
                for row in connection.execute(
                    "SELECT secret_reference FROM provider_registry "
                    "WHERE secret_reference IS NOT NULL"
                ).fetchall()
            ]
        self.assertTrue(
            all(reference.startswith("secret.") for reference in references)
        )

    def test_install_is_idempotent_and_preserves_original_timestamp(self) -> None:
        first = self.install()
        self.trusted_at = T3
        replay = self.install()
        self.assertGreater(first["inserted"], 0)
        self.assertEqual(replay["inserted"], 0)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            {profile["created_at"] for profile in self.store.list_providers()},
            {T2},
        )

    def test_get_and_filters_preserve_version_digest_and_policy(self) -> None:
        self.install()
        deepseek = self.registry.get("deepseek", 1)
        stored = self.store.get_provider("deepseek")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["profile_version"], 1)
        self.assertEqual(stored["profile_digest"], deepseek.profile_digest)
        self.assertTrue(stored["manual_selection_only"])
        self.assertFalse(stored["hidden_fallback"])
        self.assertFalse(stored["grants_authority"])
        self.assertIsNone(self.store.get_provider("deepseek", 2))
        self.assertEqual(
            {
                profile["provider_id"]
                for profile in self.store.list_providers(enabled=False)
            },
            {"fake", "local.openai_compatible", "openai.compatible"},
        )
        self.assertEqual(
            [
                profile["provider_id"]
                for profile in self.store.list_providers(
                    provider_id="kimi",
                    health_state="unavailable",
                )
            ],
            ["kimi"],
        )

    def test_only_complete_code_owned_factory_manifest_is_accepted(self) -> None:
        profiles = self.registry.list_profiles()
        subset = ProviderRegistry((profiles[0],))
        with self.assertRaisesRegex(
            OperationalStateError,
            "untrusted_provider_registry",
        ):
            self.install(registry=subset)
        drifted = replace(
            profiles[0],
            display_name="Untrusted provider",
            profile_digest="",
        )
        modified = ProviderRegistry((drifted, *profiles[1:]))
        with self.assertRaisesRegex(
            OperationalStateError,
            "untrusted_provider_registry",
        ):
            self.install(registry=modified)
        self.assertEqual(self.store.list_providers(), [])

    def test_partial_rogue_and_drifted_tables_fail_closed(self) -> None:
        first = self.registry.list_profiles()[0]
        self.insert_profile(first)
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_registry_partial",
        ):
            self.install()
        self.insert_profile(first, provider_id="rogue.provider")
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_registry_rogue",
        ):
            self.install()

    def test_same_keys_with_projection_drift_fail_closed(self) -> None:
        self.insert_manifest(first_overrides={"display_name": "Tampered"})
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_registry_drift",
        ):
            self.install()
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_record_tampered",
        ):
            self.store.list_providers()

    def test_canonical_and_digest_tamper_are_rejected_on_read(self) -> None:
        first = self.registry.list_profiles()[0]
        noncanonical = json.dumps(
            first.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        self.insert_profile(first, canonical_profile=noncanonical)
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_record_corrupt",
        ):
            self.store.list_providers()

    def test_independent_digest_projection_tamper_is_rejected(self) -> None:
        self.insert_manifest(first_overrides={"profile_digest": "0" * 64})
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_record_tampered",
        ):
            self.store.list_providers()

    def test_secret_literal_in_projection_never_reaches_reader(self) -> None:
        secret_literal = "DEEPSEEK_API_KEY=never-return-this-value"
        deepseek_index = next(
            index
            for index, profile in enumerate(self.registry.list_profiles())
            if profile.provider_id == "deepseek"
        )
        for index, profile in enumerate(self.registry.list_profiles()):
            self.insert_profile(
                profile,
                **(
                    {"secret_reference": secret_literal}
                    if index == deepseek_index
                    else {}
                ),
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_record_tampered",
        ):
            self.store.list_providers()
        self.assertNotIn(
            secret_literal,
            canonical_json(self.registry.to_public_dict()),
        )

    def test_environment_secret_is_never_read_or_persisted(self) -> None:
        sentinel = "sk-this-is-a-test-sentinel-never-persist"
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": sentinel},
            clear=False,
        ):
            self.install()
            output = canonical_json(self.store.list_providers())
        self.assertNotIn(sentinel, output)
        for candidate in self.database.parent.glob(
            f"{self.database.name}*"
        ):
            self.assertNotIn(
                sentinel,
                candidate.read_bytes().decode("utf-8", errors="ignore"),
            )

    def test_sql_append_only_triggers_reject_update_and_delete(self) -> None:
        self.install()
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "provider_registry_append_only",
            ):
                connection.execute(
                    "UPDATE provider_registry SET display_name='changed'"
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "provider_registry_append_only",
            ):
                connection.execute("DELETE FROM provider_registry")

    def test_user_authority_and_writer_lease_are_both_required(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError,
            "provider_registry_requires_user_authority",
        ):
            self.install(
                authority=self.authority(
                    authority_id="authority:v10-c-provider-agent",
                    issued_by="agent:yunos",
                )
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "event_commit_not_authorized",
        ):
            self.install(
                authority=self.authority(
                    authority_id="authority:v10-c-provider-no-commit",
                    commit_permission=False,
                )
            )
        with self.assertRaisesRegex(
            OperationalStateError,
            "writer_lease_required",
        ):
            self.install(lease_token="wrong-token")
        self.assertEqual(self.store.list_providers(), [])

    def test_expired_authority_uses_trusted_clock(self) -> None:
        self.trusted_at = T3
        with self.assertRaisesRegex(
            OperationalStateError,
            "authority_expired_or_not_yet_valid",
        ):
            self.install(
                authority=self.authority(
                    authority_id="authority:v10-c-provider-expired",
                    expires_at=T3,
                )
            )
        self.assertEqual(self.store.list_providers(), [])

    def test_filter_validation_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError,
            "invalid_provider_enabled_filter",
        ):
            self.store.list_providers(enabled=1)
        with self.assertRaisesRegex(
            OperationalStateError,
            "invalid_provider_health_filter",
        ):
            self.store.list_providers(health_state="ready")
        with self.assertRaisesRegex(
            OperationalStateError,
            "invalid_provider_profile_version",
        ):
            self.store.get_provider("deepseek", True)


if __name__ == "__main__":
    unittest.main()
