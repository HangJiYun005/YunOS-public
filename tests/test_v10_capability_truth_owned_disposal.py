from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory.cognitive_local_broker as broker_module
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    _create_tool_execution_preview,
    _create_tool_execution_result,
    _tool_proposal_from_verified_record,
)
from memory.cognitive_local_broker import (
    CognitiveLocalBroker,
    CognitiveLocalBrokerError,
)
from memory.cognitive_path_policy import resolve_project_path
from memory.cognitive_verification import CognitiveIndependentVerifier
from memory.operational_state_models import canonical_digest


T1 = "2026-08-01T00:00:00.000000Z"
T2 = "2026-08-01T00:01:00.000000Z"
T3 = "2026-08-01T00:02:00.000000Z"


def sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class CapabilityTruthAndOwnedDisposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.artifacts = root / "artifacts"
        self.outside = root / "outside"
        self.project.mkdir()
        self.artifacts.mkdir()
        self.outside.mkdir()
        self.broker = CognitiveLocalBroker(
            project_root=self.project,
            artifact_root=self.artifacts,
        )
        self.verifier = CognitiveIndependentVerifier(
            project_root=self.project,
            artifact_root=self.artifacts,
        )

    @staticmethod
    def profile(capability_id: str):
        return next(
            profile
            for profile in bounded_execution_capability_profiles(
                {capability_id: "available"}
            )
            if profile.capability_id == capability_id
        )

    def proposal(
        self,
        capability_id: str,
        arguments: dict,
        *,
        suffix: str,
        run_id: str = "run:owned-disposal",
    ):
        profile = self.profile(capability_id)
        typed_arguments = profile.validate_input(arguments)
        return _tool_proposal_from_verified_record(
            {
                "protocol_version": TOOL_PROTOCOL_VERSION,
                "proposal_id": f"toolproposal:{suffix}",
                "run_id": run_id,
                "objective_id": "objective:owned-disposal",
                "objective_digest": sha("objective:owned-disposal"),
                "plan_id": "plan:owned-disposal",
                "plan_version": 1,
                "plan_digest": sha("plan:owned-disposal"),
                "plan_node_id": f"node:{suffix}",
                "capability_id": capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": typed_arguments,
                "argument_digest": canonical_digest(typed_arguments),
                "idempotency_key": f"idempotency:{suffix}",
                "reason": "Dispose only the file created by this bounded run",
                "expected_observation": "The owned path is independently missing",
                "risk_class": profile.risk_class,
                "authorization_id": "authorization:owned-disposal",
                "authorization_digest": sha("authorization:owned-disposal"),
                "status": "proposed",
                "proposed_at": T1,
                "runtime_execution_reference": None,
                "runtime_receipt_digest": None,
                "proposal_digest": "",
            }
        )

    def create_owned(self, relative_path: str = "temp/owned.txt"):
        parent = self.project / Path(relative_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        create = self.proposal(
            "project.file.create",
            {
                "relative_path": relative_path,
                "content": "owned\n",
                "expected_after_sha256": sha("owned\n"),
            },
            suffix=f"create-{relative_path.replace('/', '-')}",
        )
        output = self.broker.create_file(
            proposal=create,
            execution_key=f"execution:create:{relative_path}",
            binding=resolve_project_path(
                project_root=self.project,
                relative_path=relative_path,
                access_mode="create_file",
            ),
        )
        dispose = self.proposal(
            "project.temp.dispose_owned",
            {
                "relative_path": relative_path,
                "ownership_reference": output["backup_reference"],
                "expected_before_sha256": sha("owned\n"),
            },
            suffix=f"dispose-{relative_path.replace('/', '-')}",
        )
        return create, output, dispose

    def test_non_executors_cannot_be_overridden_to_available(self) -> None:
        unsupported = {
            "git.commit.create",
            "browser.check.readonly",
            "memory.retrieve",
            "operational_state.retrieve",
            "external.codex.handoff",
            "test.run.allowlisted",
        }
        registry = build_bounded_execution_capability_registry(
            {capability_id: "available" for capability_id in unsupported}
        )
        observed = {
            capability_id: registry.lookup(capability_id).availability
            for capability_id in unsupported
        }
        self.assertEqual("handoff_only", observed["browser.check.readonly"])
        self.assertEqual("handoff_only", observed["external.codex.handoff"])
        for capability_id in unsupported - {
            "browser.check.readonly",
            "external.codex.handoff",
        }:
            self.assertEqual("disabled", observed[capability_id])

    def test_owned_create_disposal_and_independent_missing_verification(self) -> None:
        _create, created, dispose = self.create_owned()
        binding = self.broker.resolve_owned_disposal_binding(
            proposal=dispose,
            relative_path="temp/owned.txt",
            ownership_reference=created["backup_reference"],
            expected_before_sha256=sha("owned\n"),
        )
        preview = _create_tool_execution_preview(
            preview_id="toolpreview:owned-disposal",
            proposal_digest=dispose.proposal_digest,
            authorization_digest=dispose.authorization_digest,
            plan_digest=dispose.plan_digest,
            node_digest=sha("node:owned-disposal"),
            capability_digest=dispose.capability_digest,
            attempt_number=1,
            invocation_kind="native_call",
            resolved_argv=(),
            working_directory=str(self.project.resolve()),
            resolved_bindings=(binding.to_dict(),),
            template_id=None,
            template_digest=None,
            effective_timeout_ms=5_000,
            effective_output_bytes=16_384,
            prepared_at=T1,
            expires_at=T3,
        )
        output = self.broker.dispose_owned_file(
            proposal=dispose,
            relative_path="temp/owned.txt",
            ownership_reference=created["backup_reference"],
            expected_before_sha256=sha("owned\n"),
        )
        self.assertFalse((self.project / "temp" / "owned.txt").exists())
        result = _create_tool_execution_result(
            execution_id="toolexecution:owned-disposal",
            proposal_id=dispose.proposal_id,
            proposal_digest=dispose.proposal_digest,
            run_id=dispose.run_id,
            idempotency_key="execution:dispose:owned",
            reservation_event_id="stateevent:owned-disposal",
            reservation_receipt_digest=sha("reservation:owned-disposal"),
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            attempt_number=1,
            invocation_kind="native_call",
            executor_kind="local_broker",
            resolved_argv=(),
            working_directory=str(self.project.resolve()),
            policy_decision="allow",
            reason_codes=("tool_execution_verified",),
            started_at=T1,
            finished_at=T2,
            status="succeeded",
            exit_code=None,
            output=output,
            stdout_digest=None,
            stderr_digest=None,
            artifact_references=(),
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status="passed",
        )
        verification = self.verifier.verify(
            proposal=dispose,
            preview=preview,
            result=result,
            verified_at=T2,
        )
        self.assertEqual("passed", verification.result)
        self.assertEqual(
            created["backup_reference"],
            verification.evidence["observation"]["ownership_reference"],
        )

    def test_disposal_rejects_foreign_owner_and_content_drift(self) -> None:
        _create, created, dispose = self.create_owned("foreign/owned.txt")
        foreign = self.proposal(
            "project.temp.dispose_owned",
            dict(dispose.typed_arguments),
            suffix="foreign-run",
            run_id="run:foreign",
        )
        with self.assertRaises(CognitiveLocalBrokerError) as owner_denied:
            self.broker.resolve_owned_disposal_binding(
                proposal=foreign,
                relative_path="foreign/owned.txt",
                ownership_reference=created["backup_reference"],
                expected_before_sha256=sha("owned\n"),
            )
        self.assertEqual("tool_rollback_owner_mismatch", owner_denied.exception.reason_code)

        (self.project / "foreign" / "owned.txt").write_bytes(b"drifted\n")
        with self.assertRaises(CognitiveLocalBrokerError) as drift_denied:
            self.broker.resolve_owned_disposal_binding(
                proposal=dispose,
                relative_path="foreign/owned.txt",
                ownership_reference=created["backup_reference"],
                expected_before_sha256=sha("owned\n"),
            )
        self.assertEqual(
            "tool_owned_disposal_current_digest_mismatch",
            drift_denied.exception.reason_code,
        )
        self.assertEqual(b"drifted\n", (self.project / "foreign" / "owned.txt").read_bytes())

    def test_disposal_parent_swap_cannot_touch_outside_sentinel(self) -> None:
        _create, created, dispose = self.create_owned("race/owned.txt")
        parent = self.project / "race"
        moved = self.project / "race-moved"
        outside = self.outside / "owned.txt"
        outside.write_bytes(b"outside\n")
        observation = {"blocked": False, "swapped": False}

        def race(_binding) -> None:
            try:
                parent.rename(moved)
            except OSError:
                observation["blocked"] = True
                raise CognitiveLocalBrokerError("tool_secured_path_identity_changed")
            parent.symlink_to(self.outside, target_is_directory=True)
            observation["swapped"] = True

        try:
            with patch(
                "memory.cognitive_local_broker._write_race_observation_point",
                side_effect=race,
            ):
                with self.assertRaises(CognitiveLocalBrokerError) as rejected:
                    self.broker.dispose_owned_file(
                        proposal=dispose,
                        relative_path="race/owned.txt",
                        ownership_reference=created["backup_reference"],
                        expected_before_sha256=sha("owned\n"),
                    )
            self.assertFalse(rejected.exception.effect_may_have_occurred)
            self.assertTrue(observation["blocked"] or observation["swapped"])
            self.assertEqual(b"outside\n", outside.read_bytes())
        finally:
            if parent.is_symlink():
                parent.unlink()
            if moved.exists():
                moved.rename(parent)
        self.assertEqual(b"owned\n", (parent / "owned.txt").read_bytes())

    def test_all_post_effect_failures_are_marked_uncertain(self) -> None:
        # create: target HANDLE already exists before verification fails.
        create = self.proposal(
            "project.file.create",
            {
                "relative_path": "partial-create.txt",
                "content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="partial-create",
        )
        with patch(
            "memory.cognitive_local_broker._verify_secured_file",
            side_effect=OSError("injected post-write failure"),
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as create_error:
                self.broker.create_file(
                    proposal=create,
                    execution_key="execution:partial-create",
                    binding=resolve_project_path(
                        project_root=self.project,
                        relative_path="partial-create.txt",
                        access_mode="create_file",
                    ),
                )
        self.assertTrue(create_error.exception.effect_may_have_occurred)

        target = self.project / "partial-patch.txt"
        target.write_bytes(b"before\n")
        patch_proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "partial-patch.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="partial-patch",
        )
        with patch(
            "memory.cognitive_local_broker._verify_secured_file",
            side_effect=OSError("injected post-write failure"),
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as patch_error:
                self.broker.patch_file(
                    proposal=patch_proposal,
                    execution_key="execution:partial-patch",
                    binding=resolve_project_path(
                        project_root=self.project,
                        relative_path="partial-patch.txt",
                        access_mode="write_file",
                    ),
                )
        self.assertTrue(patch_error.exception.effect_may_have_occurred)

        rollback_target = self.project / "partial-rollback.txt"
        rollback_target.write_bytes(b"before\n")
        rollback_proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "partial-rollback.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
            suffix="partial-rollback",
        )
        patched = self.broker.patch_file(
            proposal=rollback_proposal,
            execution_key="execution:partial-rollback",
            binding=resolve_project_path(
                project_root=self.project,
                relative_path="partial-rollback.txt",
                access_mode="write_file",
            ),
        )
        with patch(
            "memory.cognitive_local_broker._verify_secured_file",
            side_effect=OSError("injected post-write failure"),
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as rollback_error:
                self.broker.rollback_patch(
                    proposal=rollback_proposal,
                    backup_reference=patched["backup_reference"],
                    expected_current_sha256=sha("after\n"),
                )
        self.assertTrue(rollback_error.exception.effect_may_have_occurred)

        _create, created, dispose = self.create_owned("partial-dispose/owned.txt")
        original_resolve = broker_module.resolve_project_path
        calls = 0

        def fail_missing_verification(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2 and kwargs.get("access_mode") == "create_file":
                raise OSError("injected post-delete failure")
            return original_resolve(*args, **kwargs)

        with patch(
            "memory.cognitive_local_broker.resolve_project_path",
            side_effect=fail_missing_verification,
        ):
            with self.assertRaises(CognitiveLocalBrokerError) as dispose_error:
                self.broker.dispose_owned_file(
                    proposal=dispose,
                    relative_path="partial-dispose/owned.txt",
                    ownership_reference=created["backup_reference"],
                    expected_before_sha256=sha("owned\n"),
                )
        self.assertTrue(dispose_error.exception.effect_may_have_occurred)
        self.assertFalse((self.project / "partial-dispose" / "owned.txt").exists())


if __name__ == "__main__":
    unittest.main()
