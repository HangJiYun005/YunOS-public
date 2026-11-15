from __future__ import annotations

import unittest
from dataclasses import replace

from memory.cognitive_cancellation import (
    CANCELLATION_ARTIFACT_VERSION,
    CANCELLATION_OPERATION,
    CognitiveCancellationError,
    UserCancellationArtifact,
    _cancellation_from_verified_record,
    issue_user_cancellation_artifact,
)
from memory.operational_state_models import AuthorityArtifact


T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-01T00:01:00Z"
T2 = "2026-08-01T00:02:00Z"
T3 = "2026-08-01T00:03:00Z"
T4 = "2026-08-01T00:04:00Z"
PROJECT_ID = "project:yunos"
RUN_ID = "run:v10-d-cancellation"
RUN_DIGEST = "a" * 64
SESSION_DIGEST = "b" * 64
PROOF_DIGEST = "c" * 64


def authority(
    *,
    issued_by: str = "user:local-user",
    project_id: str = PROJECT_ID,
    write_scope: tuple[str, ...] = (RUN_ID,),
    commit_permission: bool = True,
    issued_at: str = T0,
    expires_at: str = T4,
) -> AuthorityArtifact:
    return AuthorityArtifact(
        authority_id="authority:v10-d-cancellation",
        issued_by=issued_by,
        issued_at=issued_at,
        expires_at=expires_at,
        target_project_id=project_id,
        allowed_event_types=("cognitive_run_update",),
        read_scope=("*",),
        write_scope=write_scope,
        prohibited_operations=("merge", "tag", "push"),
        commit_permission=commit_permission,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        next_node_permission=False,
    )


def issue(
    *,
    source_authority: AuthorityArtifact | None = None,
    project_id: str = PROJECT_ID,
    requested_at: str = T1,
    expires_at: str = T3,
    reason: str = "User requested cancellation.",
) -> UserCancellationArtifact:
    return issue_user_cancellation_artifact(
        source_authority=source_authority or authority(),
        project_id=project_id,
        run_id=RUN_ID,
        expected_run_digest=RUN_DIGEST,
        session_digest=SESSION_DIGEST,
        csrf_session_proof_digest=PROOF_DIGEST,
        requested_at=requested_at,
        expires_at=expires_at,
        reason=reason,
    )


class V10CancellationArtifactTest(unittest.TestCase):
    def test_factory_binds_exact_user_run_and_proof_deterministically(self) -> None:
        first = issue(reason="  User   requested cancellation. ")
        replay = issue()

        self.assertEqual(first, replay)
        self.assertEqual(first.cancellation_id, replay.cancellation_id)
        self.assertEqual(first.canonical_digest, replay.canonical_digest)
        self.assertEqual(first.project_id, PROJECT_ID)
        self.assertEqual(first.run_id, RUN_ID)
        self.assertEqual(first.expected_run_digest, RUN_DIGEST)
        self.assertEqual(first.user_authority_id, authority().authority_id)
        self.assertEqual(
            first.user_authority_digest,
            authority().authority_digest,
        )
        self.assertEqual(first.user_actor, "user:local-user")
        self.assertEqual(first.session_digest, SESSION_DIGEST)
        self.assertEqual(first.csrf_session_proof_digest, PROOF_DIGEST)
        self.assertEqual(first.requested_at, "2026-08-01T00:01:00.000000Z")
        self.assertEqual(first.expires_at, "2026-08-01T00:03:00.000000Z")
        self.assertEqual(first.reason, "User requested cancellation.")
        self.assertEqual(first.allowed_operation, CANCELLATION_OPERATION)
        self.assertEqual(first.version, CANCELLATION_ARTIFACT_VERSION)

    def test_artifact_grants_no_other_permissions(self) -> None:
        artifact = issue()
        for name in (
            "project_mutation_permission",
            "git_commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "database_write_permission",
            "acceptance_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
        ):
            self.assertIs(getattr(artifact, name), False)

    def test_direct_construction_and_dataclass_replace_are_denied(self) -> None:
        artifact = issue()
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_artifact_factory_required",
        ):
            UserCancellationArtifact(
                cancellation_id=artifact.cancellation_id,
                project_id=artifact.project_id,
                run_id=artifact.run_id,
                expected_run_digest=artifact.expected_run_digest,
                user_authority_id=artifact.user_authority_id,
                user_authority_digest=artifact.user_authority_digest,
                user_actor=artifact.user_actor,
                session_digest=artifact.session_digest,
                csrf_session_proof_digest=artifact.csrf_session_proof_digest,
                requested_at=artifact.requested_at,
                expires_at=artifact.expires_at,
                reason=artifact.reason,
            )
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_artifact_factory_required",
        ):
            replace(artifact, run_id="run:other")

    def test_verified_record_replay_preserves_identity_and_tampering_fails(self) -> None:
        artifact = issue()
        replay = _cancellation_from_verified_record(artifact.to_dict())
        self.assertEqual(replay, artifact)

        tampered = artifact.to_dict()
        tampered["run_id"] = "run:other"
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_digest_mismatch",
        ):
            _cancellation_from_verified_record(tampered)

        expanded = artifact.to_dict()
        expanded["push_permission"] = True
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_permission_expansion_denied",
        ):
            _cancellation_from_verified_record(expanded)

    def test_factory_requires_current_user_authority(self) -> None:
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "trusted_user_issuer",
        ):
            issue(source_authority=authority(issued_by="agent:v10"))
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "expired_or_not_yet_valid",
        ):
            issue(requested_at=T0, source_authority=authority(issued_at=T1))
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "expired_or_not_yet_valid",
        ):
            issue(requested_at=T4)

    def test_factory_requires_matching_project_commit_and_run_scope(self) -> None:
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_project_mismatch",
        ):
            issue(project_id="project:other")
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_event_commit_not_authorized",
        ):
            issue(source_authority=authority(commit_permission=False))
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_run_write_not_authorized",
        ):
            issue(source_authority=authority(write_scope=("run:other",)))

        wildcard = issue(source_authority=authority(write_scope=("*",)))
        self.assertEqual(wildcard.run_id, RUN_ID)

    def test_factory_refuses_lifetime_beyond_user_authority(self) -> None:
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_exceeds_user_authority_interval",
        ):
            issue(expires_at="2026-08-01T00:05:00Z")
        with self.assertRaisesRegex(
            CognitiveCancellationError,
            "cancellation_exceeds_user_authority_interval",
        ):
            issue(expires_at=T1)

    def test_binding_validation_is_exact_and_expiring(self) -> None:
        artifact = issue()
        artifact.assert_valid_for(
            project_id=PROJECT_ID,
            run_id=RUN_ID,
            expected_run_digest=RUN_DIGEST,
            at=T2,
        )
        cases = (
            ({"project_id": "project:other"}, "project_mismatch"),
            ({"run_id": "run:other"}, "run_mismatch"),
            ({"expected_run_digest": "d" * 64}, "run_digest_mismatch"),
            ({"at": T4}, "expired_or_not_yet_valid"),
        )
        base = {
            "project_id": PROJECT_ID,
            "run_id": RUN_ID,
            "expected_run_digest": RUN_DIGEST,
            "at": T2,
        }
        for changes, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                CognitiveCancellationError,
                reason,
            ):
                artifact.assert_valid_for(**{**base, **changes})

    def test_invalid_session_or_csrf_proof_digest_fails_closed(self) -> None:
        base = {
            "source_authority": authority(),
            "project_id": PROJECT_ID,
            "run_id": RUN_ID,
            "expected_run_digest": RUN_DIGEST,
            "session_digest": SESSION_DIGEST,
            "csrf_session_proof_digest": PROOF_DIGEST,
            "requested_at": T1,
            "expires_at": T3,
            "reason": "Stop this run.",
        }
        for field in ("session_digest", "csrf_session_proof_digest"):
            with self.subTest(field=field), self.assertRaisesRegex(
                CognitiveCancellationError,
                field,
            ):
                issue_user_cancellation_artifact(
                    **{**base, field: "not-a-digest"}
                )


if __name__ == "__main__":
    unittest.main()
