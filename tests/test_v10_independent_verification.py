from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory.cognitive_command_policy import CommandTemplateRegistry
from memory.cognitive_execution_capabilities import (
    bounded_execution_capability_profiles,
)
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    _create_tool_execution_preview,
    _create_tool_execution_result,
    _tool_proposal_from_verified_record,
)
from memory.cognitive_local_broker import CognitiveLocalBroker
from memory.cognitive_path_policy import resolve_project_path
from memory.cognitive_verification import (
    CognitiveIndependentVerificationError,
    CognitiveIndependentVerifier,
    IndependentToolVerification,
)
from memory.operational_state_models import canonical_digest


T1 = "2026-08-01T00:00:00.000000Z"
T2 = "2026-08-01T00:01:00.000000Z"
T3 = "2026-08-01T00:02:00.000000Z"


def sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class IndependentVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.artifacts = root / "artifacts"
        self.project.mkdir()
        self.artifacts.mkdir()
        self.command_registry = CommandTemplateRegistry()
        self.verifier = CognitiveIndependentVerifier(
            project_root=self.project,
            artifact_root=self.artifacts,
            command_registry=self.command_registry,
        )
        self.broker = CognitiveLocalBroker(
            project_root=self.project,
            artifact_root=self.artifacts,
        )

    def profile(self, capability_id: str):
        return next(
            item
            for item in bounded_execution_capability_profiles(
                {capability_id: "available"}
            )
            if item.capability_id == capability_id
        )

    def proposal(self, capability_id: str, arguments: dict):
        profile = self.profile(capability_id)
        typed_arguments = profile.validate_input(arguments)
        return _tool_proposal_from_verified_record(
            {
                "protocol_version": TOOL_PROTOCOL_VERSION,
                "proposal_id": "toolproposal:test",
                "run_id": "run:test",
                "objective_id": "objective:test",
                "objective_digest": sha("objective"),
                "plan_id": "plan:test",
                "plan_version": 1,
                "plan_digest": sha("plan"),
                "plan_node_id": "node:test",
                "capability_id": profile.capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": typed_arguments,
                "argument_digest": canonical_digest(typed_arguments),
                "idempotency_key": "capability:idempotency",
                "reason": "Verify one bounded operation",
                "expected_observation": "An independently observable result",
                "risk_class": profile.risk_class,
                "authorization_id": "authorization:test",
                "authorization_digest": sha("authorization"),
                "status": "proposed",
                "proposed_at": T1,
                "runtime_execution_reference": None,
                "runtime_receipt_digest": None,
                "proposal_digest": "",
            }
        )

    def native_preview(self, proposal, binding, *, invocation_kind="native_call"):
        return _create_tool_execution_preview(
            preview_id="preview:test",
            proposal_digest=proposal.proposal_digest,
            authorization_digest=proposal.authorization_digest,
            plan_digest=proposal.plan_digest,
            node_digest=sha("node"),
            capability_digest=proposal.capability_digest,
            attempt_number=1,
            invocation_kind=invocation_kind,
            resolved_argv=(),
            working_directory=str(self.project.resolve()),
            resolved_bindings=(binding.to_dict(),),
            template_id=None,
            template_digest=None,
            effective_timeout_ms=10_000,
            effective_output_bytes=100_000,
            prepared_at=T1,
            expires_at=T3,
        )

    def execution(
        self,
        proposal,
        preview,
        output,
        *,
        execution_key="execution:key",
        executor_kind="native",
        exit_code=None,
        stdout_digest=None,
        stderr_digest=None,
        artifact_references=(),
        resolved_argv=None,
    ):
        return _create_tool_execution_result(
            execution_id="execution:test",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            run_id=proposal.run_id,
            idempotency_key=execution_key,
            reservation_event_id="event:reservation",
            reservation_receipt_digest=sha("reservation"),
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            attempt_number=preview.attempt_number,
            invocation_kind=preview.invocation_kind,
            executor_kind=executor_kind,
            resolved_argv=(
                preview.resolved_argv if resolved_argv is None else resolved_argv
            ),
            working_directory=preview.working_directory,
            policy_decision="allow",
            reason_codes=("tool_execution_verified",),
            started_at=T1,
            finished_at=T2,
            status="succeeded",
            exit_code=exit_code,
            output=output,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            artifact_references=artifact_references,
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status="passed",
        )

    def read_fixture(self, *, forged: bool):
        target = self.project / "truth.txt"
        target.write_bytes(b"truth\n")
        proposal = self.proposal(
            "project.file.read",
            {"relative_path": "truth.txt"},
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="truth.txt",
            access_mode="read_file",
        )
        preview = self.native_preview(proposal, binding)
        content = "forged\n" if forged else "truth\n"
        output = {
            "relative_path": "truth.txt",
            "content": content,
            "content_sha256": sha(content),
            "truncated": False,
        }
        return proposal, preview, self.execution(proposal, preview, output)

    def patch_fixture(self):
        target = self.project / "bounded.txt"
        target.write_bytes(b"before\n")
        proposal = self.proposal(
            "project.file.patch",
            {
                "relative_path": "bounded.txt",
                "expected_before_sha256": sha("before\n"),
                "replacement_content": "after\n",
                "expected_after_sha256": sha("after\n"),
            },
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="bounded.txt",
            access_mode="write_file",
        )
        preview = self.native_preview(proposal, binding)
        execution_key = "execution:patch"
        output = self.broker.patch_file(
            proposal=proposal,
            execution_key=execution_key,
            binding=binding,
        )
        result = self.execution(
            proposal,
            preview,
            output,
            execution_key=execution_key,
            executor_kind="local_broker",
        )
        return target, proposal, preview, result

    def process_fixture(self, *, with_artifacts: bool):
        proposal = self.proposal(
            "process.template.run",
            {"template_id": "git.diff.check", "relative_paths": []},
        )
        profile = self.profile("process.template.run")
        command = self.command_registry.resolve(
            template_id="git.diff.check.v1",
            profile=profile,
            typed_arguments=dict(proposal.typed_arguments),
            path_bindings=(),
            project_root=str(self.project),
        )
        preview = _create_tool_execution_preview(
            preview_id="preview:process",
            proposal_digest=proposal.proposal_digest,
            authorization_digest=proposal.authorization_digest,
            plan_digest=proposal.plan_digest,
            node_digest=sha("process-node"),
            capability_digest=proposal.capability_digest,
            attempt_number=1,
            invocation_kind="fixed_process",
            resolved_argv=command.argv,
            working_directory=command.working_directory,
            resolved_bindings=(),
            template_id=command.template_id,
            template_digest=command.template_digest,
            effective_timeout_ms=10_000,
            effective_output_bytes=100_000,
            prepared_at=T1,
            expires_at=T3,
        )
        stdout = b"one test passed\n"
        stderr = b""
        references = []
        if with_artifacts:
            storage_key = sha("process-artifact")
            stdout_path = self.artifacts / f"{storage_key}.stdout.log"
            stderr_path = self.artifacts / f"{storage_key}.stderr.log"
            stdout_path.write_bytes(stdout)
            stderr_path.write_bytes(stderr)
            references = [str(stdout_path.resolve()), str(stderr_path.resolve())]
        output = {
            "exit_code": 0,
            "stdout": stdout.decode("utf-8"),
            "stderr": "",
            "stdout_sha256": sha(stdout),
            "stderr_sha256": sha(stderr),
            "truncated": False,
            "artifact_references": references,
        }
        result = self.execution(
            proposal,
            preview,
            output,
            executor_kind="local_broker",
            exit_code=0,
            stdout_digest=sha(stdout),
            stderr_digest=sha(stderr),
            artifact_references=tuple(references),
        )
        return proposal, preview, result

    def create_fixture(self):
        proposal = self.proposal(
            "project.file.create",
            {
                "relative_path": "created.txt",
                "content": "created by bounded broker\n",
                "expected_after_sha256": sha("created by bounded broker\n"),
            },
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="created.txt",
            access_mode="create_file",
        )
        preview = self.native_preview(proposal, binding)
        execution_key = "execution:create"
        output = self.broker.create_file(
            proposal=proposal,
            execution_key=execution_key,
            binding=binding,
        )
        result = self.execution(
            proposal,
            preview,
            output,
            execution_key=execution_key,
            executor_kind="local_broker",
        )
        return proposal, preview, result

    def test_factory_seal_and_deterministic_record(self) -> None:
        proposal, preview, result = self.read_fixture(forged=False)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        repeated = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("passed", verification.result)
        self.assertEqual(verification.to_dict(), repeated.to_dict())
        self.assertEqual(
            verification.evidence_digest,
            canonical_digest(dict(verification.evidence)),
        )
        self.assertEqual(64, len(verification.verifier_identity_digest))
        self.assertEqual("not_requested", verification.acceptance_state)
        self.assertIsNone(verification.acceptance_event_id)
        schema_record = verification.to_verification_record()
        self.assertNotIn("proposal_digest", schema_record)
        self.assertNotIn("preview_digest", schema_record)
        self.assertNotIn("execution_digest", schema_record)
        self.assertEqual(
            {
                "verification_id",
                "run_id",
                "objective_id",
                "plan_node_id",
                "tool_execution_id",
                "verification_kind",
                "verifier_kind",
                "criteria",
                "result",
                "evidence_references",
                "evidence_digest",
                "verified_at",
                "acceptance_state",
                "acceptance_event_id",
                "verification_digest",
            },
            set(schema_record),
        )
        with self.assertRaisesRegex(
            CognitiveIndependentVerificationError,
            "independent_tool_verification_factory_required",
        ):
            direct = verification.to_dict()
            direct.pop("verification_protocol_version")
            IndependentToolVerification(**direct)
        with self.assertRaisesRegex(
            CognitiveIndependentVerificationError,
            "independent_tool_verification_factory_required",
        ):
            dataclasses.replace(verification)

        verification.evidence["observation"]["method"] = "executor_forged"
        with self.assertRaisesRegex(
            CognitiveIndependentVerificationError,
            "independent_verification_evidence_digest_mismatch",
        ):
            verification.assert_factory_sealed()

    def test_succeeded_executor_self_check_cannot_forge_passed(self) -> None:
        proposal, preview, forged_result = self.read_fixture(forged=True)
        self.assertEqual("passed", forged_result.verification_status)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=forged_result,
            verified_at=T3,
        )
        self.assertEqual("failed", verification.result)
        self.assertNotEqual(
            forged_result.execution_digest,
            verification.verification_digest,
        )

    def test_list_search_and_sqlite_are_independently_reobserved(self) -> None:
        listing = self.project / "listing"
        listing.mkdir()
        (listing / "one.txt").write_bytes(b"needle\n")
        database = self.project / "sample.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO sample DEFAULT VALUES")
        connection.commit()

        cases = []
        list_proposal = self.proposal(
            "project.tree.list",
            {"relative_path": "listing", "maximum_entries": 10},
        )
        list_binding = resolve_project_path(
            project_root=self.project,
            relative_path="listing",
            access_mode="read_directory",
        )
        cases.append(
            (
                list_proposal,
                list_binding,
                self.broker.list_directory(list_binding, maximum_entries=10),
            )
        )

        search_proposal = self.proposal(
            "project.text.search",
            {
                "relative_path": "listing/one.txt",
                "search_text": "needle",
                "maximum_results": 10,
            },
        )
        search_binding = resolve_project_path(
            project_root=self.project,
            relative_path="listing/one.txt",
            access_mode="read_file",
        )
        cases.append(
            (
                search_proposal,
                search_binding,
                self.broker.search_text(
                    search_binding,
                    search_text="needle",
                    maximum_results=10,
                ),
            )
        )

        database_proposal = self.proposal(
            "database.inspect.readonly",
            {"relative_path": "sample.db"},
        )
        database_binding = resolve_project_path(
            project_root=self.project,
            relative_path="sample.db",
            access_mode="read_file",
        )
        cases.append(
            (
                database_proposal,
                database_binding,
                self.broker.inspect_database(database_binding),
            )
        )

        for proposal, binding, output in cases:
            with self.subTest(capability_id=proposal.capability_id):
                preview = self.native_preview(proposal, binding)
                result = self.execution(proposal, preview, output)
                verification = self.verifier.verify(
                    proposal=proposal,
                    preview=preview,
                    result=result,
                    verified_at=T3,
                )
                self.assertEqual("passed", verification.result)

    def test_database_sidecar_change_is_not_independently_accepted(self) -> None:
        database = self.project / "changing.db"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        self.assertEqual("wal", writer.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO sample(value) VALUES('before')")
        writer.commit()
        proposal = self.proposal(
            "database.inspect.readonly",
            {"relative_path": "changing.db"},
        )
        binding = resolve_project_path(
            project_root=self.project,
            relative_path="changing.db",
            access_mode="read_file",
        )
        output = self.broker.inspect_database(binding)
        preview = self.native_preview(proposal, binding)
        result = self.execution(proposal, preview, output)

        writer.execute("INSERT INTO sample(value) VALUES('after')")
        writer.commit()
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("failed", verification.result)
        self.assertEqual(
            "independent_read_observation_mismatch",
            verification.evidence["reason_code"],
        )

    def test_write_hash_mismatch_fails_even_with_valid_self_check(self) -> None:
        target, proposal, preview, result = self.patch_fixture()
        target.write_bytes(b"tampered after execution\n")
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("passed", result.verification_status)
        self.assertEqual("failed", verification.result)

    def test_write_hash_and_backup_record_match_passes(self) -> None:
        _target, proposal, preview, result = self.patch_fixture()
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("passed", verification.result)
        self.assertEqual(proposal.proposal_digest, verification.proposal_digest)
        self.assertEqual(preview.preview_digest, verification.preview_digest)
        self.assertEqual(result.execution_digest, verification.execution_digest)

    def test_create_hash_and_backup_record_match_passes(self) -> None:
        proposal, preview, result = self.create_fixture()
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("passed", verification.result)
        self.assertEqual(
            b"created by bounded broker\n",
            (self.project / "created.txt").read_bytes(),
        )

    def test_process_without_required_artifacts_is_unverifiable(self) -> None:
        proposal, preview, result = self.process_fixture(with_artifacts=False)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("unverifiable", verification.result)

    def test_process_receipt_artifacts_and_template_binding_pass(self) -> None:
        proposal, preview, result = self.process_fixture(with_artifacts=True)
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("passed", verification.result)
        self.assertEqual("static_check", verification.verification_kind)
        self.assertEqual(2, len(verification.evidence_references))

    def test_process_receipt_artifact_tamper_fails(self) -> None:
        proposal, preview, result = self.process_fixture(with_artifacts=True)
        Path(result.artifact_references[0]).write_bytes(b"tampered\n")
        verification = self.verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=T3,
        )
        self.assertEqual("failed", verification.result)


if __name__ == "__main__":
    unittest.main()
