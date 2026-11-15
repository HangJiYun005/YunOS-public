from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.cognitive_objective import build_objective_contract
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.cognitive_state_store import CognitiveStateStore
from memory.operational_state_models import (
    AuthorityArtifact,
    SourceAnchor,
    build_state_event,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools.objective_admin import (
    MAX_INPUT_BYTES,
    MAX_TOKEN_FILE_BYTES,
    ObjectiveAdminError,
    _read_json,
    _read_token,
    _run_write_command,
)


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "objective_admin.py"


class ObjectiveAdminCliTest(unittest.TestCase):
    def run_cli(
        self,
        *arguments: str,
        stdin_payload: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(CLI), *arguments],
            cwd=ROOT,
            input=(
                json.dumps(stdin_payload, ensure_ascii=False)
                if stdin_payload is not None
                else None
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_cli_entrypoint_exists(self) -> None:
        self.assertTrue(CLI.is_file())
        runner = (ROOT / "run.ps1").read_text(encoding="utf-8")
        self.assertIn('"objective-admin"', runner)
        self.assertIn('"tools\\objective_admin.py"', runner)

    def test_help_exposes_only_the_v10_a_commands(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            "resolve",
            "preview",
            "status",
            "list",
            "show",
            "candidate-create",
            "candidate-decide",
            "apply",
        ):
            self.assertIn(command, result.stdout)
        self.assertNotIn("provider", result.stdout.lower())
        self.assertNotIn("plan-graph", result.stdout.lower())

    def test_resolve_accepts_stdin_json_without_database(self) -> None:
        result = self.run_cli(
            "resolve",
            stdin_payload={
                "user_utterance": "只讨论下一步，不执行任何写入。",
                "project_id": "project:yunos",
                "agency_level": "consult",
                "cognitive_stance": "continue",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["persisted"])
        self.assertEqual(payload["objective_contract"]["agency_level"], "consult")

    def test_preview_accepts_json_file_and_does_not_require_authority(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            request_path = Path(temporary) / "objective.json"
            request_path.write_text(
                json.dumps(
                    {
                        "user_utterance": "生成一个可验证的只读检查目标。",
                        "project_id": "project:yunos",
                        "agency_level": "prepare",
                        "agency_selected_by": "user",
                        "cognitive_stance": "explore",
                        "success_criteria": ["只读检查生成可验证结果。"],
                        "source_anchors": [
                            {
                                "anchor_id": "anchor:objective-cli-test",
                                "source_kind": "user_declaration",
                                "locator": "test:objective-cli-input",
                                "evidence_digest": "a" * 64,
                                "observed_at": "2026-07-31T00:00:00Z",
                                "privacy_scope": "private_content",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = self.run_cli("preview", "--input", str(request_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["persisted"])
        self.assertRegex(payload["preview_digest"], r"^[0-9a-f]{64}$")

    def test_preview_rejects_secret_literal_without_echoing_it(self) -> None:
        synthetic = "DEEPSEEK_API_KEY=SYNTHETIC_TEST_VALUE_123456"
        result = self.run_cli(
            "preview",
            stdin_payload={
                "user_utterance": synthetic,
                "project_id": "project:yunos",
                "success_criteria": ["No secret is persisted."],
                "source_anchors": [
                    {
                        "anchor_id": "anchor:objective-cli-secret",
                        "source_kind": "user_declaration",
                        "locator": "test:objective-secret-filter",
                        "evidence_digest": "a" * 64,
                        "observed_at": "2026-07-31T00:00:00Z",
                        "privacy_scope": "private_content",
                    }
                ],
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(synthetic, result.stdout)
        self.assertNotIn(synthetic, result.stderr)
        self.assertIn("objective_secret_literal_forbidden", result.stderr)

    def test_generic_preview_cannot_self_declare_automatic_acceptance(
        self,
    ) -> None:
        result = self.run_cli(
            "preview",
            stdin_payload={
                "user_utterance": "Prepare a bounded preview.",
                "project_id": "project:yunos",
                "success_criteria": ["Preview is deterministic."],
                "source_anchors": [
                    {
                        "anchor_id": "anchor:objective-cli-auto",
                        "source_kind": "user_declaration",
                        "locator": "test:objective-auto-filter",
                        "evidence_digest": "a" * 64,
                        "observed_at": "2026-07-31T00:00:00Z",
                        "privacy_scope": "private_content",
                    }
                ],
                "acceptance_policy": {
                    "mode": "preauthorized_deterministic",
                    "automatic_allowed": True,
                    "requires_user_event": False,
                    "deterministic_criteria": ["Exact digest matches."],
                    "maximum_risk": "low",
                    "require_system_verification": True,
                    "require_reversible": True,
                    "require_no_conflicts": True,
                    "require_no_overreach": True,
                    "require_no_unresolved_warnings": True,
                    "preauthorization_reference": "anchor:objective-cli-auto",
                    "preauthorization_digest": "a" * 64,
                },
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "trusted_acceptance_preauthorization_required",
            result.stderr,
        )

    def test_write_commands_require_all_four_write_gates(self) -> None:
        for command in ("candidate-create", "candidate-decide", "apply"):
            result = self.run_cli(command)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--preview-digest", result.stderr)
            self.assertIn("--authority", result.stderr)
            self.assertIn("--lease-owner", result.stderr)
            self.assertIn("--lease-token", result.stderr)

    def test_write_commands_reject_stdin_to_avoid_powershell_encoding_loss(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ObjectiveAdminError,
            "objective_write_requires_utf8_input_file",
        ):
            _run_write_command(
                type(
                    "Args",
                    (),
                    {"input": "-", "command": "apply"},
                )()
            )

    def test_cli_readers_bound_files_and_stdin_before_full_allocation(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            oversized_json = root / "oversized.json"
            oversized_json.write_text(
                "{" + (" " * MAX_INPUT_BYTES) + "}",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ObjectiveAdminError,
                "objective_input_too_large",
            ):
                _read_json(str(oversized_json))
            oversized_token = root / "oversized-token.txt"
            oversized_token.write_text(
                "x" * (MAX_TOKEN_FILE_BYTES + 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ObjectiveAdminError,
                "lease_token_file_too_large",
            ):
                _read_token(str(oversized_token))
        with patch(
            "tools.objective_admin.sys.stdin",
            io.StringIO("{" + (" " * MAX_INPUT_BYTES) + "}"),
        ):
            with self.assertRaisesRegex(
                ObjectiveAdminError,
                "objective_input_too_large",
            ):
                _read_json("-")

    def test_private_utterance_is_not_accepted_as_a_command_argument(self) -> None:
        result = self.run_cli(
            "resolve",
            "--user-utterance",
            "private text",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_source_has_no_provider_or_production_database_dependency(self) -> None:
        source = CLI.read_text(encoding="utf-8")
        for forbidden in (
            "deepseek_adapter",
            "production_runtime",
            "DEFAULT_OPERATIONAL_STATE_PATH",
            "allow_production_write=True",
            "YUNOS_OPERATIONAL_STATE_PRODUCTION_WRITE",
        ):
            self.assertNotIn(forbidden, source)

    def test_candidate_approval_and_apply_workflow_is_real_and_bounded(
        self,
    ) -> None:
        t0 = "2020-01-01T00:00:00Z"
        t1 = "2020-01-02T00:00:00Z"
        t2 = "2026-01-01T00:00:00Z"
        t3 = "2026-07-31T00:03:00Z"
        t5 = "2099-01-01T00:00:00Z"
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            database = root / "operational.db"
            initialize_operational_database(database, created_at=t0)
            apply_cognitive_state_migration(database, created_at=t1)
            operational = OperationalStateStore(database)
            token = "objective-cli-e2e-lease-token"
            owner = "owner:objective-cli-e2e"
            operational.acquire_writer_lease(
                owner_id=owner,
                token=token,
                acquired_at=t1,
                expires_at=t5,
            )
            token_file = root / "lease-token.txt"
            token_file.write_text(token, encoding="utf-8")
            anchor_payload = {
                "anchor_id": "anchor:objective-cli-e2e",
                "source_kind": "user_declaration",
                "locator": "test:objective-cli-e2e",
                "evidence_digest": "a" * 64,
                "observed_at": t1,
                "privacy_scope": "private_content",
            }
            anchor = SourceAnchor.from_dict(anchor_payload)
            authority = AuthorityArtifact(
                authority_id="authority:objective-cli-e2e",
                issued_by="user:objective-cli-e2e",
                issued_at=t0,
                expires_at=t5,
                target_project_id="project:yunos",
                allowed_event_types=(
                    "candidate_create",
                    "candidate_decision",
                    "decision_approved",
                ),
                read_scope=("project:yunos",),
                write_scope=("*",),
                prohibited_operations=("merge", "tag", "push"),
                commit_permission=True,
            )
            authority_file = root / "authority.json"
            authority_file.write_text(
                json.dumps(authority.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            candidate_contract = build_objective_contract(
                user_utterance="先澄清目标，再写入受控 Objective。",
                project_id="project:yunos",
                source_anchors=[anchor_payload],
                success_criteria=["Objective 可被确定性查询。"],
                agency_level="plan",
                agency_selected_by="user",
                ambiguity_signals=[
                    {
                        "kind": "material_goal_change",
                        "detail": "目标范围尚未确认",
                    }
                ],
            )
            candidate_request = {
                "operation": "candidate-create",
                "contract": candidate_contract.to_dict(),
                "source_anchor_id": anchor.anchor_id,
                "confidence_ppm": 700_000,
                "created_at": t2,
                "expires_at": t5,
            }

            def preview_and_write(
                name: str,
                request: dict,
                command: str,
            ) -> dict:
                request_file = root / f"{name}.json"
                request_file.write_text(
                    json.dumps(request, ensure_ascii=False),
                    encoding="utf-8",
                )
                preview = self.run_cli(
                    "preview",
                    "--input",
                    str(request_file),
                )
                self.assertEqual(preview.returncode, 0, preview.stderr)
                preview_payload = json.loads(preview.stdout)
                result = self.run_cli(
                    command,
                    "--db",
                    str(database),
                    "--input",
                    str(request_file),
                    "--preview-digest",
                    preview_payload["preview_digest"],
                    "--authority",
                    str(authority_file),
                    "--lease-owner",
                    owner,
                    "--lease-token-file",
                    str(token_file),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            candidate = preview_and_write(
                "candidate-create",
                candidate_request,
                "candidate-create",
            )
            decision_request = {
                "operation": "candidate-decide",
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": candidate["candidate_digest"],
                "decision": "approve",
                "recorded_at": t3,
            }
            approved = preview_and_write(
                "candidate-decide",
                decision_request,
                "candidate-decide",
            )

            resolved = build_objective_contract(
                user_utterance="在已确认范围内写入受控 Objective。",
                project_id="project:yunos",
                source_anchors=[anchor_payload],
                success_criteria=["Objective 可被确定性查询。"],
                objective_id=candidate_contract.objective_id,
                agency_level="plan",
                agency_selected_by="user",
            )
            resolved_candidate = preview_and_write(
                "resolved-candidate-create",
                {
                    "operation": "candidate-create",
                    "contract": resolved.to_dict(),
                    "source_anchor_id": anchor.anchor_id,
                    "confidence_ppm": 1_000_000,
                    "created_at": t2,
                    "expires_at": t5,
                },
                "candidate-create",
            )
            resolved_approval = preview_and_write(
                "resolved-candidate-decide",
                {
                    "operation": "candidate-decide",
                    "candidate_id": resolved_candidate["candidate_id"],
                    "candidate_digest": resolved_candidate["candidate_digest"],
                    "decision": "approve",
                    "recorded_at": t3,
                },
                "candidate-decide",
            )
            event = build_state_event(
                project_id="project:yunos",
                entity_id="decision:objective-cli-e2e",
                entity_type="decision",
                event_type="decision_approved",
                source_kind="declared",
                source_anchor=anchor,
                authority_kind="user",
                actor_kind="user",
                actor_id="user:objective-cli-e2e",
                observed_at=t1,
                effective_at=t2,
                recorded_at=t3,
                expires_at=None,
                supersedes_event_id=None,
                caused_by_event_id=None,
                idempotency_key="idempotency:objective-cli-e2e",
                conflict_group_id=None,
                payload={
                    "state": "approved",
                    "objective_id": resolved.objective_id,
                    "objective_version": resolved.version,
                    "objective_digest": resolved.canonical_digest,
                },
                privacy_scope="private_content",
                authority_digest=authority.authority_digest,
            )
            applied = preview_and_write(
                "apply",
                {
                    "operation": "apply",
                    "contract": resolved.to_dict(),
                    "event": event.to_dict(),
                    "approval_id": resolved_approval["approval_id"],
                    "created_at": t3,
                },
                "apply",
            )
            self.assertFalse(applied["idempotent_replay"])
            record = CognitiveStateStore(database).get_objective(
                resolved.objective_id,
            )
            self.assertEqual(resolved.canonical_digest, record["canonical_digest"])
            self.assertEqual(
                resolved_approval["approval_id"],
                record["approval_id"],
            )


if __name__ == "__main__":
    unittest.main()
