from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from memory.cognitive_provider import (
    HEALTH_STATES,
    CognitiveProviderError,
    ProviderProfile,
    ProviderRegistry,
    build_suggested_provider_registry,
)
from memory.cognitive_state_migration import verify_cognitive_state_schema
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    utc_now,
    validate_identity,
)
from memory.operational_state_store import OperationalStateStore


_MAX_PROVIDER_PROFILE_BYTES = 262_144
_REGISTRY_ENTITY_ID = "provider-registry:yunos-v10"
_REGISTRY_EVENT_TYPE = "provider_registry_install"


def _decode_canonical_profile(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, str)
        or len(raw.encode("utf-8")) > _MAX_PROVIDER_PROFILE_BYTES
    ):
        raise OperationalStateError("provider_record_corrupt")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise OperationalStateError("provider_record_corrupt") from None
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise OperationalStateError("provider_record_corrupt")
    return value


class CognitiveProviderStore:
    """Append-only Provider Registry in the existing Operational State DB.

    The store persists code-owned profile descriptors only.  It never resolves
    a ``SecretReference``, selects a provider, invokes an adapter, grants
    authority, or performs fallback.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        trusted_clock: Callable[[], str] | None = None,
        allow_production_write: bool = False,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.operational_store = OperationalStateStore(
            self.path,
            allow_production_write=allow_production_write,
        )
        self._trusted_clock = trusted_clock or utc_now
        verify_cognitive_state_schema(self.path)

    def _trusted_at(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = _decode_canonical_profile(row["canonical_profile"])
        try:
            profile = ProviderProfile.from_record(payload)
        except CognitiveProviderError:
            raise OperationalStateError("provider_record_corrupt") from None

        expected_secret_reference = (
            None
            if profile.secret_reference is None
            else profile.secret_reference.reference_id
        )
        if (
            profile.provider_id != row["provider_id"]
            or profile.profile_version != row["profile_version"]
            or profile.display_name != row["display_name"]
            or profile.endpoint != row["endpoint"]
            or profile.model != row["model"]
            or int(profile.enabled) != row["enabled"]
            or row["manual_selection_only"] != 1
            or expected_secret_reference != row["secret_reference"]
            or profile.health_state != row["health_state"]
            or profile.profile_digest != row["profile_digest"]
            or canonical_json(profile.to_dict()) != row["canonical_profile"]
            or profile.grants_authority is not False
            or profile.executable is not False
        ):
            raise OperationalStateError("provider_record_tampered")

        created_at = normalize_timestamp(row["created_at"])
        return {
            **profile.to_dict(),
            "manual_selection_only": True,
            "hidden_fallback": False,
            "grants_authority": False,
            "execution_allowed": False,
            "created_at": created_at,
        }

    @staticmethod
    def _registry_snapshot(registry: ProviderRegistry) -> str:
        public = registry.to_public_dict()
        if (
            public.get("selection_policy") != "manual_only"
            or public.get("hidden_fallback") is not False
            or public.get("grants_authority") is not False
            or public.get("executable") is not False
            or registry.grants_authority is not False
            or registry.executable is not False
        ):
            raise OperationalStateError("untrusted_provider_registry")
        return canonical_json(public)

    @classmethod
    def _validate_code_owned_registry(
        cls,
        registry: ProviderRegistry,
    ) -> ProviderRegistry:
        if not isinstance(registry, ProviderRegistry):
            raise OperationalStateError("invalid_provider_registry")
        try:
            actual_snapshot = cls._registry_snapshot(registry)
            profiles = registry.list_profiles()
        except CognitiveProviderError:
            raise OperationalStateError(
                "untrusted_provider_registry"
            ) from None

        # The factory only uses opaque reference ids to derive health metadata.
        # Enumerating their tiny descriptor set proves that the complete
        # manifest is one exact code-owned factory output without resolving a
        # credential or depending on hidden ProviderRegistry state.
        reference_ids = tuple(
            sorted(
                {
                    profile.secret_reference.reference_id
                    for profile in profiles
                    if profile.secret_reference is not None
                }
            )
        )
        if len(reference_ids) > 8:
            raise OperationalStateError("untrusted_provider_registry")
        for reference_count in range(len(reference_ids) + 1):
            for references in combinations(reference_ids, reference_count):
                for allow_fake in (False, True):
                    try:
                        expected = build_suggested_provider_registry(
                            references,
                            allow_fake=allow_fake,
                        )
                        expected_snapshot = cls._registry_snapshot(expected)
                    except CognitiveProviderError:
                        continue
                    if actual_snapshot == expected_snapshot:
                        return expected
        raise OperationalStateError("untrusted_provider_registry")

    @classmethod
    def _load_code_owned_registry_on_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> tuple[ProviderRegistry, list[dict[str, Any]]]:
        rows = connection.execute(
            "SELECT * FROM provider_registry "
            "ORDER BY provider_id,profile_version"
        ).fetchall()
        if not rows:
            raise OperationalStateError("provider_registry_uninstalled")
        records = [cls._profile_from_row(row) for row in rows]
        try:
            registry = ProviderRegistry(
                ProviderProfile.from_record(
                    {
                        key: value
                        for key, value in record.items()
                        if key
                        not in {
                            "manual_selection_only",
                            "hidden_fallback",
                            "grants_authority",
                            "execution_allowed",
                            "created_at",
                        }
                    }
                )
                for record in records
            )
        except CognitiveProviderError:
            raise OperationalStateError(
                "untrusted_provider_registry"
            ) from None
        validated = cls._validate_code_owned_registry(registry)
        if cls._registry_snapshot(validated) != cls._registry_snapshot(registry):
            raise OperationalStateError("provider_registry_drift")
        return validated, records

    def install_builtin_providers(
        self,
        *,
        registry: ProviderRegistry,
        authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        validated = self._validate_code_owned_registry(registry)
        trusted_at = self._trusted_at()
        if (
            not isinstance(authority, AuthorityArtifact)
            or not authority.issued_by.startswith("user:")
        ):
            raise OperationalStateError(
                "provider_registry_requires_user_authority"
            )
        authority.authorize_event(
            project_id="project:yunos",
            entity_id=_REGISTRY_ENTITY_ID,
            event_type=_REGISTRY_EVENT_TYPE,
            at=trusted_at,
        )
        with closing(
            self.operational_store._connect(write=True)
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational_store._authorize_auxiliary_write(
                    connection,
                    authority=authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id="project:yunos",
                    entity_id=_REGISTRY_ENTITY_ID,
                    operation=_REGISTRY_EVENT_TYPE,
                    at=trusted_at,
                    enforce_for_test=True,
                )
                existing_rows = connection.execute(
                    "SELECT * FROM provider_registry "
                    "ORDER BY provider_id,profile_version"
                ).fetchall()
                expected_keys = {
                    (profile.provider_id, profile.profile_version)
                    for profile in validated.list_profiles()
                }
                existing_keys = {
                    (row["provider_id"], row["profile_version"])
                    for row in existing_rows
                }
                if existing_keys - expected_keys:
                    raise OperationalStateError("provider_registry_rogue")
                if expected_keys - existing_keys and existing_keys:
                    raise OperationalStateError("provider_registry_partial")

                existing = {
                    (row["provider_id"], row["profile_version"]): row
                    for row in existing_rows
                }
                inserted = 0
                for profile in validated.list_profiles():
                    key = (profile.provider_id, profile.profile_version)
                    row = existing.get(key)
                    if row is not None:
                        try:
                            record = self._profile_from_row(row)
                        except OperationalStateError:
                            raise OperationalStateError(
                                "provider_registry_drift"
                            ) from None
                        if (
                            record["profile_digest"]
                            != profile.profile_digest
                            or canonical_json(profile.to_dict())
                            != row["canonical_profile"]
                        ):
                            raise OperationalStateError(
                                "provider_registry_drift"
                            )
                        continue
                    secret_reference = (
                        None
                        if profile.secret_reference is None
                        else profile.secret_reference.reference_id
                    )
                    connection.execute(
                        "INSERT INTO provider_registry("
                        "provider_id,profile_version,display_name,endpoint,"
                        "model,enabled,manual_selection_only,"
                        "secret_reference,health_state,canonical_profile,"
                        "profile_digest,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            profile.provider_id,
                            profile.profile_version,
                            profile.display_name,
                            profile.endpoint,
                            profile.model,
                            int(profile.enabled),
                            1,
                            secret_reference,
                            profile.health_state,
                            canonical_json(profile.to_dict()),
                            profile.profile_digest,
                            trusted_at,
                        ),
                    )
                    inserted += 1

                try:
                    final_registry, _records = (
                        self._load_code_owned_registry_on_connection(
                            connection
                        )
                    )
                except OperationalStateError:
                    raise OperationalStateError(
                        "provider_registry_drift"
                    ) from None
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise OperationalStateError(
                    "provider_registry_insert_conflict"
                ) from None
            except Exception:
                connection.rollback()
                raise

        public = final_registry.to_public_dict()
        return {
            "registry_digest": canonical_digest(public),
            "profile_count": len(validated.list_profiles()),
            "inserted": inserted,
            "idempotent_replay": inserted == 0,
            "selection_policy": "manual_only",
            "hidden_fallback": False,
            "grants_authority": False,
            "execution_allowed": False,
            "secret_material_read": False,
        }

    def list_providers(
        self,
        *,
        provider_id: str | None = None,
        enabled: bool | None = None,
        health_state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if provider_id is not None:
            validate_identity(provider_id, "invalid_provider_id")
        if enabled is not None and not isinstance(enabled, bool):
            raise OperationalStateError("invalid_provider_enabled_filter")
        if health_state is not None and health_state not in HEALTH_STATES:
            raise OperationalStateError("invalid_provider_health_filter")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise OperationalStateError("invalid_provider_limit")
        with closing(self.operational_store._connect()) as connection:
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM provider_registry"
                ).fetchone()[0]
                == 0
            ):
                return []
            _registry, records = self._load_code_owned_registry_on_connection(
                connection
            )
        return [
            record
            for record in records
            if (
                provider_id is None
                or record["provider_id"] == provider_id
            )
            and (enabled is None or record["enabled"] is enabled)
            and (
                health_state is None
                or record["health_state"] == health_state
            )
        ][:limit]

    def get_provider(
        self,
        provider_id: str,
        profile_version: int | None = None,
    ) -> dict[str, Any] | None:
        validate_identity(provider_id, "invalid_provider_id")
        if profile_version is not None and (
            not isinstance(profile_version, int)
            or isinstance(profile_version, bool)
            or not 1 <= profile_version <= 2_147_483_647
        ):
            raise OperationalStateError(
                "invalid_provider_profile_version"
            )
        with closing(self.operational_store._connect()) as connection:
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM provider_registry"
                ).fetchone()[0]
                == 0
            ):
                return None
            _registry, records = self._load_code_owned_registry_on_connection(
                connection
            )
        matches = [
            record
            for record in records
            if record["provider_id"] == provider_id
            and (
                profile_version is None
                or record["profile_version"] == profile_version
            )
        ]
        if not matches:
            return None
        return max(matches, key=lambda record: record["profile_version"])


__all__ = ["CognitiveProviderStore"]
