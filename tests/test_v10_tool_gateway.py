from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.cognitive_command_policy import CommandTemplateRegistry
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_models import (
    _create_tool_execution_preview,
    _tool_proposal_from_verified_record,
    create_tool_proposal,
)
from memory.cognitive_capability import CognitiveCapabilityError
from memory.cognitive_local_broker import CognitiveLocalBroker
from memory.cognitive_supervision import ProcessSupervisor
from memory.cognitive_tool_gateway import CognitiveToolGateway
from memory.cognitive_verification import CognitiveIndependentVerifier
from memory.operational_state_models import canonical_digest, make_identity


_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "_v10_gateway_execution_store_fixture",
    Path(__file__).with_name("test_v10_execution_store.py"),
)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("v10_gateway_fixture_unavailable")
_STORE_FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _STORE_FIXTURE
_FIXTURE_SPEC.loader.exec_module(_STORE_FIXTURE)


class V10ToolGatewayTest(unittest.TestCase):
    """Gateway integration checks against a disposable Operational DB only."""

    def setUp(self) -> None:
        fixture_type = _STORE_FIXTURE.CognitiveExecutionStoreTest
        self.fixture = fixture_type(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

        self.artifact_root = self.fixture.root / "tool-artifacts"
        self.artifact_root.mkdir()
        self.command_registry = CommandTemplateRegistry()
        self.broker = CognitiveLocalBroker(
            project_root=self.fixture.root,
            artifact_root=self.artifact_root,
        )
        self.supervisor = ProcessSupervisor(
            artifact_root=self.artifact_root,
            trusted_clock=lambda: self.fixture.now,
        )
        self.gateway = CognitiveToolGateway(
            project_root=str(self.fixture.root),
            registry=self.fixture.registry,
            execution_store=self.fixture.execution_store,
            command_registry=self.command_registry,
            local_broker=self.broker,
            process_supervisor=self.supervisor,
            trusted_clock=lambda: self.fixture.now,
        )
        self.verifier = CognitiveIndependentVerifier(
            project_root=self.fixture.root,
            artifact_root=self.artifact_root,
            command_registry=self.command_registry,
        )

    @staticmethod
    def assert_reason(context, expected: str) -> None:
        actual = getattr(context.exception, "reason_code", str(context.exception))
        if actual != expected:
            raise AssertionError(f"expected reason {expected!r}, got {actual!r}")

    def enter_execution(self) -> None:
        self.fixture.run = self.fixture.transition(self.fixture.run, "executing")

    def proposal(self, relative_path: str = "README.txt"):
        return create_tool_proposal(
            run_id=self.fixture.run.run_id,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
            registry=self.fixture.registry,
            plan_node_id=self.fixture.action.node_id,
            model_proposal={
                "capability_id": self.fixture.profile.capability_id,
                "capability_version": self.fixture.profile.version,
                "capability_digest": self.fixture.profile.capability_digest,
                "typed_arguments": {"relative_path": relative_path},
                "reason": "Read only the explicitly bounded disposable fixture.",
                "expected_observation": "Return fresh content and its digest.",
            },
            proposed_at=self.fixture.now,
        )

    def execute(self, preview, proposal):
        return self.gateway.execute(
            preview=preview,
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

    @staticmethod
    def operational_details(verification, proposal, preview, result):
        return {
            **verification.to_operational_details(),
            "run_id": proposal.run_id,
            "tool_execution_id": result.execution_id,
            "execution_digest": result.execution_digest,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "preview_id": preview.preview_id,
            "preview_digest": preview.preview_digest,
        }

    def test_preview_execute_independent_verify_and_replay_close_loop(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        preview = self.gateway.preview(
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
        )

        outcome = self.execute(preview, proposal)
        self.assertFalse(outcome.idempotent_replay)
        self.assertEqual("succeeded", outcome.result.status)
        self.assertEqual(preview.preview_id, outcome.result.preview_id)
        self.assertEqual(preview.preview_digest, outcome.result.preview_digest)
        verifying = self.fixture.execution_store.get_run(proposal.run_id)
        self.assertEqual("verifying", verifying.status)

        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=outcome.result,
            verified_at=self.fixture.now,
        )
        self.assertEqual("passed", verification.result)
        recorded = self.fixture.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=verifying.run_digest,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        self.assertEqual("executing", recorded["run"].status)
        self.assertIn(
            self.fixture.action.node_id,
            self.fixture.execution_store.latest_checkpoint(proposal.run_id).completed_node_ids,
        )

        replay = self.execute(preview, proposal)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(outcome.result.execution_id, replay.result.execution_id)
        self.assertEqual(
            1,
            len(self.fixture.execution_store.list_tool_executions(proposal.run_id)),
        )

    def test_terminal_replay_rechecks_current_authorization_expiry(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        preview = self.gateway.preview(
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
        )
        outcome = self.execute(preview, proposal)
        verifying = self.fixture.execution_store.get_run(proposal.run_id)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=outcome.result,
            verified_at=self.fixture.now,
        )
        self.fixture.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=verifying.run_digest,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

        self.fixture.now = _STORE_FIXTURE.T9
        with self.assertRaises(Exception) as expired:
            self.execute(preview, proposal)
        self.assert_reason(expired, "authorization_expired_or_not_yet_valid")

    def test_terminal_replay_rechecks_current_path_binding(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        preview = self.gateway.preview(
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
        )
        outcome = self.execute(preview, proposal)
        verifying = self.fixture.execution_store.get_run(proposal.run_id)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=outcome.result,
            verified_at=self.fixture.now,
        )
        self.fixture.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=verifying.run_digest,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

        (self.fixture.root / "README.txt").write_bytes(b"binding drift\n")
        with self.assertRaises(Exception) as drifted:
            self.execute(preview, proposal)
        self.assertIn(
            getattr(drifted.exception, "reason_code", str(drifted.exception)),
            {"tool_execution_preview_drift", "tool_path_identity_changed"},
        )

    def test_path_overreach_is_denied_before_reservation(self) -> None:
        self.enter_execution()
        valid = self.proposal()
        payload = valid.to_dict()
        payload["typed_arguments"] = {"relative_path": "../outside.txt"}
        payload["argument_digest"] = canonical_digest(payload["typed_arguments"])
        payload["idempotency_key"] = make_identity(
            "capability",
            {
                "proposal": valid.proposal_id,
                "malicious_path": "../outside.txt",
            },
        )
        payload["proposal_digest"] = ""
        proposal = _tool_proposal_from_verified_record(payload)
        self.fixture.persist_proposal(proposal)
        with self.assertRaises(CognitiveCapabilityError) as denied:
            self.gateway.preview(
                proposal=proposal,
                plan=self.fixture.plan,
                authorization=self.fixture.authorization,
            )
        self.assert_reason(denied, "capability_path_outside_authorized_root")
        self.assertEqual(
            (),
            self.fixture.execution_store.list_tool_executions(proposal.run_id),
        )

    def test_clock_rollback_is_denied_before_reservation(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        self.fixture.now = _STORE_FIXTURE.T8_20

        with self.assertRaises(Exception) as denied:
            self.gateway.preview(
                proposal=proposal,
                plan=self.fixture.plan,
                authorization=self.fixture.authorization,
            )
        self.assert_reason(denied, "tool_gateway_clock_rollback_detected")
        self.assertEqual(
            (),
            self.fixture.execution_store.list_tool_executions(proposal.run_id),
        )
        checkpoint = self.fixture.execution_store.latest_checkpoint(proposal.run_id)
        self.assertTrue(checkpoint.safe_to_resume)

    def test_invalid_output_after_reservation_fails_closed_to_recovery(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        preview = self.gateway.preview(
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
        )

        with patch.object(self.broker, "read_file", return_value={"forged": True}):
            outcome = self.execute(preview, proposal)

        self.assertEqual("recovery_required", outcome.result.status)
        self.assertEqual("unverifiable", outcome.result.verification_status)
        self.assertIn(
            "capability_schema_additional_property",
            outcome.result.reason_codes,
        )
        run = self.fixture.execution_store.get_run(proposal.run_id)
        checkpoint = self.fixture.execution_store.latest_checkpoint(proposal.run_id)
        self.assertEqual("recovery_required", run.status)
        self.assertFalse(checkpoint.safe_to_resume)
        self.assertEqual(
            ("tool_execution_not_safe_to_resume",),
            checkpoint.unsafe_reason_codes,
        )

    def test_preview_drift_is_denied_and_valid_preview_remains_executable(self) -> None:
        self.enter_execution()
        proposal = self.proposal()
        self.fixture.persist_proposal(proposal)
        preview = self.gateway.preview(
            proposal=proposal,
            plan=self.fixture.plan,
            authorization=self.fixture.authorization,
        )
        drifted = _create_tool_execution_preview(
            preview_id=preview.preview_id,
            proposal_digest=preview.proposal_digest,
            authorization_digest=preview.authorization_digest,
            plan_digest=preview.plan_digest,
            node_digest=preview.node_digest,
            capability_digest=preview.capability_digest,
            attempt_number=preview.attempt_number,
            invocation_kind=preview.invocation_kind,
            resolved_argv=preview.resolved_argv,
            working_directory=preview.working_directory,
            resolved_bindings=tuple(dict(item) for item in preview.resolved_bindings),
            template_id=preview.template_id,
            template_digest=preview.template_digest,
            effective_timeout_ms=preview.effective_timeout_ms,
            effective_output_bytes=preview.effective_output_bytes - 1,
            prepared_at=preview.prepared_at,
            expires_at=preview.expires_at,
        )
        with self.assertRaisesRegex(Exception, "tool_execution_preview_drift"):
            self.execute(drifted, proposal)
        self.assertEqual(
            (),
            self.fixture.execution_store.list_tool_executions(proposal.run_id),
        )

        outcome = self.execute(preview, proposal)
        self.assertEqual("succeeded", outcome.result.status)
        self.assertFalse(outcome.idempotent_replay)

    def test_git_status_gateway_output_matches_capability_contract(self) -> None:
        registry = build_bounded_execution_capability_registry(
            {"git.status.read": "available"}
        )
        profile = registry.lookup("git.status.read", version=1)
        generic_process_result = types.SimpleNamespace(
            artifact_references=(),
            exit_code=0,
            stderr="",
            stderr_sha256="0" * 64,
            stdout=(
                "# branch.oid 0123456789abcdef0123456789abcdef01234567\0"
                "# branch.head main\0"
                "1 .M N... 100644 100644 100644 "
                "0123456789abcdef0123456789abcdef01234567 "
                "89abcdef0123456789abcdef0123456789abcdef README.md\0"
            ),
            stdout_sha256="1" * 64,
            truncated=False,
        )

        output = self.gateway._process_output(
            generic_process_result,
            capability_id="git.status.read",
        )
        validated = profile.validate_output(output)
        self.assertEqual(
            "0123456789abcdef0123456789abcdef01234567",
            validated["head"],
        )
        self.assertEqual("main", validated["branch"])
        self.assertEqual(["README.md"], validated["changed_paths"])
        self.assertFalse(validated["truncated"])


if __name__ == "__main__":
    unittest.main()
