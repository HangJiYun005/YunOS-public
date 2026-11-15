from __future__ import annotations

import ast
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from http import HTTPStatus
import inspect
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.production_delivery import production_profile_binding
from memory.production_runtime_status import (
    EXPECTED_IDENTITIES,
    EXPECTED_RUNTIME_PROFILE,
    production_runtime_status,
)
from memory.runtime_enablement import (
    build_runtime_enablement_artifact,
    initialize_runtime_enablement_sidecar,
)
from memory.runtime_protocol_contracts import (
    build_runtime_artifact,
    build_runtime_receipt,
    canonical_digest,
    canonical_json,
    conversation_identity,
    error_identity,
    event_identity,
    message_identity,
    model_run_identity,
    production_assembly_identity,
    production_delivery_identity,
    profile_projection_identity,
    project_runtime_events,
    receipt_identity,
    request_fingerprint,
    text_sha256,
)
from memory.runtime_thin_slice import initialize_runtime_sidecar
from tests import test_production_delivery as production_delivery_tests
from tools.local_web import AyunOSHandler, REAL_DB_PATH
from tools.runtime_enablement import main as runtime_cli_main, parse_args as parse_runtime_args


class ProductionRuntimeStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.sidecar = self.root / "runtime-d1.db"
        self.delivery_fixture = production_delivery_tests.ProductionDeliveryTest(
            "test_delivery_assembly_and_runtime_bindings_cover_exact_contents"
        )
        self.delivery_fixture.setUp()

    def tearDown(self) -> None:
        self.delivery_fixture.tearDown()
        self.temp.cleanup()

    def initialize_d1(self) -> None:
        result = initialize_runtime_enablement_sidecar(
            self.sidecar, execute=True, test_mode=True,
        )
        self.assertEqual("initialized", result["status"])

    def status(self) -> dict:
        with patch("memory.production_runtime_status.PRODUCTION_RUNTIME_SIDECAR", self.sidecar):
            return production_runtime_status()

    @staticmethod
    def _restore_update_trigger(connection: sqlite3.Connection, table: str) -> None:
        connection.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )

    def _canonical_artifact(self) -> tuple[dict, dict]:
        delivery = self.delivery_fixture.build()
        binding = production_profile_binding(delivery)
        self.assertEqual(
            profile_projection_identity(self.delivery_fixture.projection),
            binding["projection_identity"],
        )
        artifact = build_runtime_enablement_artifact(
            delivery,
            approved_profile_binding=binding,
        )
        return delivery, artifact

    def _insert_artifact(
        self, connection: sqlite3.Connection, delivery: dict, artifact: dict,
    ) -> None:
        profile = artifact["approved_profile_binding"]
        connection.execute(
            "INSERT INTO runtime_request_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact["artifact_identity"], artifact["artifact_version"],
                artifact["runtime_profile"]["version"], artifact["renderer"]["version"],
                delivery["delivery_identity"], canonical_json(delivery),
                profile["projection_identity"], EXPECTED_RUNTIME_PROFILE["provider"],
                EXPECTED_RUNTIME_PROFILE["model"], EXPECTED_RUNTIME_PROFILE["base_url"],
                int(EXPECTED_RUNTIME_PROFILE["stream"]),
                EXPECTED_RUNTIME_PROFILE["thinking"]["type"],
                artifact["wire_body_sha256"], canonical_json(artifact),
                "2026-07-16T00:00:00Z",
            ),
        )

    def _insert_prepared_ledger(self) -> dict:
        self.initialize_d1()
        delivery, artifact = self._canonical_artifact()
        request_id = "request-fixed-0001"
        mutation_id = "mutation-fixed-0001"
        fingerprint = request_fingerprint(
            delivery["controls"]["query"], delivery["controls"]["budget_profile"],
            artifact["artifact_identity"], artifact["wire_body_sha256"],
        )
        run_id = model_run_identity(
            request_id, mutation_id, fingerprint, artifact["artifact_identity"],
        )
        conversation_id = conversation_identity(run_id)
        user_message_id = message_identity(run_id, "user")
        now = "2026-07-16T00:00:00Z"
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO runtime_conversations VALUES(?,?)", (conversation_id, now),
            )
            self._insert_artifact(connection, delivery, artifact)
            connection.execute(
                "INSERT INTO runtime_model_runs VALUES(?,?,?,?,?,?,?)",
                (
                    run_id, request_id, mutation_id, fingerprint, conversation_id,
                    artifact["artifact_identity"], now,
                ),
            )
            connection.execute(
                "INSERT INTO runtime_messages VALUES(?,?,?,?,?,?)",
                (
                    user_message_id, conversation_id, run_id, "user",
                    delivery["controls"]["query"], now,
                ),
            )
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    event_identity(run_id, "state_prepared"), run_id,
                    "state_prepared", None, "prepared", now,
                ),
            )
            connection.commit()
        return {
            "delivery": delivery, "artifact": artifact, "request_id": request_id,
            "mutation_id": mutation_id, "fingerprint": fingerprint,
            "model_run_id": run_id, "conversation_id": conversation_id,
            "user_message_id": user_message_id, "created_at": now,
        }

    def _insert_success_outcome(self, state: dict) -> dict:
        run_id = state["model_run_id"]
        conversation_id = state["conversation_id"]
        now = state["created_at"]
        assistant_id = message_identity(run_id, "assistant")
        content = "Canonical synthetic response; no provider was contacted."
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        usage_json = canonical_json(usage)
        response_id = "synthetic-response-0001"
        finish_reason = "stop"
        response_hash = text_sha256(content)
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (event_identity(run_id, "state_running"), run_id, "state_running", "prepared", "running", now),
            )
            connection.execute(
                "INSERT INTO runtime_run_events(event_id,model_run_id,event_type,occurred_at) VALUES(?,?,?,?)",
                (event_identity(run_id, "send_attempt_started"), run_id, "send_attempt_started", now),
            )
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,usage_json,provider_response_id,
                    finish_reason,response_content_sha256,occurred_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event_identity(run_id, "provider_response_received"), run_id,
                    "provider_response_received", usage_json, response_id,
                    finish_reason, response_hash, now,
                ),
            )
            connection.execute(
                "INSERT INTO runtime_messages VALUES(?,?,?,?,?,?)",
                (assistant_id, conversation_id, run_id, "assistant", content, now),
            )
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,
                    assistant_message_id,usage_json,provider_response_id,finish_reason,
                    response_content_sha256,occurred_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_identity(run_id, "state_succeeded"), run_id, "state_succeeded",
                    "running", "succeeded", assistant_id, usage_json, response_id,
                    finish_reason, response_hash, now,
                ),
            )
            run = dict(connection.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (run_id,),
            ).fetchone())
            events = [dict(row) for row in connection.execute(
                "SELECT * FROM runtime_run_events WHERE model_run_id=? ORDER BY sequence", (run_id,),
            )]
            messages = {
                row["message_id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM runtime_messages WHERE model_run_id=?", (run_id,),
                )
            }
            receipt = build_runtime_receipt(
                run, state["artifact"], project_runtime_events(events), messages, {},
            )
            connection.execute(
                "INSERT INTO runtime_receipts VALUES(?,?,?)",
                (run_id, receipt["receipt_identity"], canonical_json(receipt)),
            )
            connection.commit()
        return {**state, "assistant_message_id": assistant_id, "receipt": receipt}

    def _insert_failed_outcome(self, state: dict) -> dict:
        run_id = state["model_run_id"]
        now = state["created_at"]
        code = "interrupted_before_send"
        stored_error_id = error_identity(run_id, code)
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,occurred_at
                ) VALUES(?,?,?,?,?,?)""",
                (event_identity(run_id, "state_running"), run_id, "state_running", "prepared", "running", now),
            )
            connection.execute(
                "INSERT INTO runtime_errors VALUES(?,?,?,?,?,?,?,?,?)",
                (stored_error_id, run_id, "recovery", code, 1, None, 0, 0, now),
            )
            connection.execute(
                """INSERT INTO runtime_run_events(
                    event_id,model_run_id,event_type,from_status,to_status,error_id,occurred_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (event_identity(run_id, "state_failed"), run_id, "state_failed", "running", "failed", stored_error_id, now),
            )
            run = dict(connection.execute(
                "SELECT * FROM runtime_model_runs WHERE model_run_id=?", (run_id,),
            ).fetchone())
            events = [dict(row) for row in connection.execute(
                "SELECT * FROM runtime_run_events WHERE model_run_id=? ORDER BY sequence", (run_id,),
            )]
            messages = {
                row["message_id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM runtime_messages WHERE model_run_id=?", (run_id,),
                )
            }
            errors = {
                row["error_id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM runtime_errors WHERE model_run_id=?", (run_id,),
                )
            }
            receipt = build_runtime_receipt(
                run, state["artifact"], project_runtime_events(events), messages, errors,
            )
            connection.execute(
                "INSERT INTO runtime_receipts VALUES(?,?,?)",
                (run_id, receipt["receipt_identity"], canonical_json(receipt)),
            )
            connection.commit()
        return {**state, "error_id": stored_error_id, "receipt": receipt}

    def test_reader_has_no_path_or_validator_injection_and_missing_is_explicit(self):
        self.assertEqual(0, len(inspect.signature(production_runtime_status).parameters))
        result = self.status()
        self.assertEqual(("missing", "sidecar_not_initialized", "not_initialized"), (
            result["status"], result["reason_code"], result["runtime_state"],
        ))
        self.assertFalse(result["initialized"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["readonly"])

    def test_d1_zero_run_status_exposes_exact_contract_counts_and_assurance(self):
        self.initialize_d1()
        result = self.status()
        self.assertEqual(("ready", "ready_zero_run", "initialized_zero_run"), (
            result["status"], result["reason_code"], result["runtime_state"],
        ))
        self.assertTrue(result["initialized"])
        self.assertTrue(result["ready"])
        self.assertEqual(EXPECTED_IDENTITIES, result["identities"])
        self.assertEqual(EXPECTED_RUNTIME_PROFILE, result["runtime_profile"])
        self.assertEqual({
            "conversations": 0,
            "request_artifacts": 0,
            "model_runs": 0,
            "messages": 0,
            "events": 0,
            "errors": 0,
            "receipts": 0,
        }, result["counts"])
        self.assertEqual({
            "integrity_check": "ok",
            "quick_check": "ok",
            "foreign_key_violations": 0,
            "append_only": True,
            "trigger_count": 16,
        }, result["integrity"])
        self.assertEqual({
            "credential_read": False,
            "sent_to_model": False,
            "external_model_calls": False,
        }, result["execution"])

    def test_legacy_bc_sidecar_is_incompatible_with_d1_reader(self):
        result = initialize_runtime_sidecar(self.sidecar, execute=True, test_mode=True)
        self.assertEqual("initialized", result["status"])
        status = self.status()
        self.assertEqual(("incompatible", "runtime_schema_incompatible"), (
            status["status"], status["reason_code"],
        ))
        self.assertFalse(status["ready"])

    def test_wrong_schema_and_tampered_profile_are_rejected(self):
        self.initialize_d1()
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("DROP TRIGGER runtime_schema_no_update")
            connection.execute(
                "UPDATE runtime_schema SET schema_version='v8.0-d1-runtime-sidecar-tampered'"
            )
            connection.commit()
        wrong_schema = self.status()
        self.assertEqual(("incompatible", "runtime_schema_incompatible"), (
            wrong_schema["status"], wrong_schema["reason_code"],
        ))

        self.sidecar = self.root / "runtime-d1-profile.db"
        self.initialize_d1()
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("DROP TRIGGER runtime_schema_no_update")
            tampered = {**EXPECTED_RUNTIME_PROFILE, "model": "deepseek-chat"}
            connection.execute(
                "UPDATE runtime_schema SET runtime_profile_json=?",
                (json.dumps(tampered, sort_keys=True, separators=(",", ":")),),
            )
            connection.commit()
        wrong_profile = self.status()
        self.assertEqual(("incompatible", "runtime_profile_binding_mismatch"), (
            wrong_profile["status"], wrong_profile["reason_code"],
        ))

    def test_status_reader_import_closure_is_pure_and_transport_free(self):
        root = Path(__file__).parents[1]
        pending = ["memory.production_runtime_status"]
        visited: set[str] = set()
        imported: set[str] = set()
        while pending:
            module = pending.pop()
            if module in visited:
                continue
            visited.add(module)
            source = root.joinpath(*module.split(".")).with_suffix(".py")
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = (
                        [f"memory.{alias.name}" for alias in node.names]
                        if node.module == "memory" else [node.module]
                    )
                for name in names:
                    imported.add(name)
                    if name.startswith("memory."):
                        pending.append(name)
        self.assertEqual({
            "memory.production_runtime_status",
            "memory.runtime_protocol_contracts",
        }, visited)
        forbidden = (
            "memory.deepseek_v4_adapter", "memory.runtime_enablement",
            "memory.runtime_thin_slice", "memory.runtime_execution",
            "memory.credential", "urllib", "http.client", "requests",
        )
        self.assertFalse({
            name for name in imported
            if any(name == value or name.startswith(value + ".") for value in forbidden)
        })

        contract_path = root / "memory" / "runtime_protocol_contracts.py"
        contract_tree = ast.parse(
            contract_path.read_text(encoding="utf-8"), filename=str(contract_path),
        )
        contract_imports: set[str] = set()
        forbidden_calls: list[str] = []
        for node in ast.walk(contract_tree):
            if isinstance(node, ast.Import):
                contract_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                contract_imports.add(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"open", "exec", "eval"}:
                    forbidden_calls.append(node.func.id)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "connect", "getenv", "read_text", "read_bytes", "write_text",
                    "write_bytes", "urlopen", "request",
                }:
                    forbidden_calls.append(node.func.attr)
        self.assertLessEqual(contract_imports, {
            "__future__", "base64", "copy", "dataclasses", "hashlib", "json", "re",
            "typing", "unicodedata",
        })
        self.assertEqual([], forbidden_calls)

    def test_tampered_protocol_provider_model_and_thinking_fail_closed(self):
        for name, mutate in (
            ("protocol", lambda artifact: artifact.__setitem__("protocol_version", "tampered-runtime")),
            ("provider", lambda artifact: artifact["runtime_profile"].__setitem__("provider", "other")),
            ("model", lambda artifact: artifact["runtime_profile"].__setitem__("model", "deepseek-chat")),
            ("thinking", lambda artifact: artifact["runtime_profile"].__setitem__("thinking", {"type": "enabled"})),
        ):
            with self.subTest(name=name):
                self.sidecar = self.root / f"runtime-config-{name}.db"
                self.initialize_d1()
                delivery, artifact = self._canonical_artifact()
                mutate(artifact)
                artifact["artifact_identity"] = "rqa_" + canonical_digest({
                    key: value for key, value in artifact.items()
                    if key != "artifact_identity"
                })
                with closing(sqlite3.connect(self.sidecar)) as connection:
                    self._insert_artifact(connection, delivery, artifact)
                    connection.commit()
                result = self.status()
                self.assertEqual(("incompatible", "request_artifact_binding_mismatch"), (
                    result["status"], result["reason_code"],
                ))

    def test_missing_append_only_protection_and_artifact_binding_fail_closed(self):
        self.initialize_d1()
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("DROP TRIGGER runtime_messages_no_delete")
            connection.commit()
        unprotected = self.status()
        self.assertEqual(("incompatible", "append_only_protection_missing"), (
            unprotected["status"], unprotected["reason_code"],
        ))

        self.sidecar = self.root / "runtime-d1-artifact.db"
        self.initialize_d1()
        delivery, artifact = self._canonical_artifact()
        artifact["wire_body_sha256"] = "d" * 64
        artifact["artifact_identity"] = "rqa_" + canonical_digest({
            key: value for key, value in artifact.items() if key != "artifact_identity"
        })
        with closing(sqlite3.connect(self.sidecar)) as connection:
            self._insert_artifact(connection, delivery, artifact)
            connection.commit()
        tampered_artifact = self.status()
        self.assertEqual(("incompatible", "wire_binding_failure"), (
            tampered_artifact["status"], tampered_artifact["reason_code"],
        ))

    def test_nonzero_model_run_is_distinct_without_inventing_a_model_call(self):
        state = self._insert_prepared_ledger()
        result = self.status()
        self.assertEqual(("ready", "ready_with_runs", "initialized_with_runs"), (
            result["status"], result["reason_code"], result["runtime_state"],
        ))
        self.assertEqual(1, result["counts"]["model_runs"])
        self.assertEqual({
            "conversations": 1, "request_artifacts": 1, "model_runs": 1,
            "messages": 1, "events": 1, "errors": 0, "receipts": 0,
        }, result["counts"])
        self.assertFalse(result["execution"]["sent_to_model"])
        self.assertFalse(result["execution"]["external_model_calls"])
        self.assertEqual(
            event_identity(state["model_run_id"], "state_prepared"),
            "rev_" + canonical_digest({
                "model_run_id": state["model_run_id"], "event_type": "state_prepared",
            }),
        )

    def test_conversation_artifact_run_message_event_and_request_identities_fail_closed(self):
        cases = {
            "fingerprint": (
                ("runtime_model_runs",),
                "UPDATE runtime_model_runs SET request_fingerprint=?",
                ("rrf_" + "b" * 64,),
                "malformed_model_run_identity",
            ),
            "mutation": (
                ("runtime_model_runs",),
                "UPDATE runtime_model_runs SET mutation_id=?",
                ("mutation-tampered-0001",),
                "malformed_model_run_identity",
            ),
            "message": (
                ("runtime_messages",),
                "UPDATE runtime_messages SET message_id=? WHERE role='user'",
                ("rmsg_" + "b" * 64,),
                "malformed_message_identity",
            ),
            "event": (
                ("runtime_run_events",),
                "UPDATE runtime_run_events SET event_id=? WHERE event_type='state_prepared'",
                ("rev_" + "b" * 64,),
                "malformed_event_identity",
            ),
        }
        for name, (tables, statement, parameters, reason) in cases.items():
            with self.subTest(name=name):
                self.sidecar = self.root / f"runtime-identity-{name}.db"
                self._insert_prepared_ledger()
                with closing(sqlite3.connect(self.sidecar)) as connection:
                    for table in tables:
                        connection.execute(f"DROP TRIGGER {table}_no_update")
                    connection.execute(statement, parameters)
                    for table in tables:
                        self._restore_update_trigger(connection, table)
                    connection.commit()
                result = self.status()
                self.assertEqual(("incompatible", reason), (
                    result["status"], result["reason_code"],
                ))

        linked_cases = {
            "conversation": (
                ("runtime_conversations", "runtime_model_runs", "runtime_messages"),
                (
                    ("UPDATE runtime_conversations SET conversation_id=?", ("rconv_" + "b" * 64,)),
                    ("UPDATE runtime_model_runs SET conversation_id=?", ("rconv_" + "b" * 64,)),
                    ("UPDATE runtime_messages SET conversation_id=?", ("rconv_" + "b" * 64,)),
                ),
                "malformed_model_run_identity",
            ),
            "artifact": (
                ("runtime_request_artifacts", "runtime_model_runs"),
                (
                    ("UPDATE runtime_request_artifacts SET artifact_identity=?", ("rqa_" + "b" * 64,)),
                    ("UPDATE runtime_model_runs SET artifact_identity=?", ("rqa_" + "b" * 64,)),
                ),
                "request_artifact_binding_mismatch",
            ),
            "run": (
                ("runtime_model_runs", "runtime_messages", "runtime_run_events"),
                (
                    ("UPDATE runtime_model_runs SET model_run_id=?", ("run_" + "b" * 64,)),
                    ("UPDATE runtime_messages SET model_run_id=?", ("run_" + "b" * 64,)),
                    ("UPDATE runtime_run_events SET model_run_id=?", ("run_" + "b" * 64,)),
                ),
                "malformed_model_run_identity",
            ),
        }
        for name, (tables, statements, reason) in linked_cases.items():
            with self.subTest(name=name):
                self.sidecar = self.root / f"runtime-linked-{name}.db"
                self._insert_prepared_ledger()
                with closing(sqlite3.connect(self.sidecar)) as connection:
                    connection.execute("PRAGMA foreign_keys=OFF")
                    for table in tables:
                        connection.execute(f"DROP TRIGGER {table}_no_update")
                    for statement, parameters in statements:
                        connection.execute(statement, parameters)
                    for table in tables:
                        self._restore_update_trigger(connection, table)
                    connection.commit()
                result = self.status()
                self.assertEqual(("incompatible", reason), (
                    result["status"], result["reason_code"],
                ))

    def test_delivery_assembly_profile_evidence_and_projection_bindings_fail_closed(self):
        def replace_identity(delivery: dict, old: str, new: str) -> dict:
            replaced = json.loads(canonical_json(delivery).replace(old, new))
            assembly = replaced["source_assembly"]
            assembly["assembly_identity"] = production_assembly_identity(assembly)
            replaced["assembly_binding"]["assembly_identity"] = assembly["assembly_identity"]
            replaced["delivery_identity"] = production_delivery_identity(replaced)
            return replaced

        for name in ("delivery", "assembly", "profile", "evidence", "projection"):
            with self.subTest(name=name):
                self.sidecar = self.root / f"runtime-binding-{name}.db"
                self.initialize_d1()
                delivery, artifact = self._canonical_artifact()
                if name == "delivery":
                    delivery["delivery_identity"] = "pdv_" + "b" * 64
                elif name == "assembly":
                    delivery["source_assembly"]["assembly_identity"] = "pca_" + "b" * 64
                    delivery["assembly_binding"]["assembly_identity"] = "pca_" + "b" * 64
                    delivery["delivery_identity"] = production_delivery_identity(delivery)
                elif name == "profile":
                    old = delivery["layers"]["approved_profile"]["items"][0]["profile_item_id"]
                    delivery = replace_identity(delivery, old, "upi_" + "b" * 64)
                    artifact = build_runtime_artifact(
                        delivery, approved_profile_binding=delivery["profile_binding"],
                    )
                elif name == "evidence":
                    old = delivery["layers"]["historical_evidence"]["items"][0]["evidence_reference_id"]
                    delivery = replace_identity(delivery, old, "pde_" + "b" * 64)
                    artifact = build_runtime_artifact(
                        delivery, approved_profile_binding=delivery["profile_binding"],
                    )
                else:
                    artifact["approved_profile_binding"]["revision"] += 1
                    artifact["artifact_identity"] = "rqa_" + canonical_digest({
                        key: value for key, value in artifact.items()
                        if key != "artifact_identity"
                    })
                with closing(sqlite3.connect(self.sidecar)) as connection:
                    self._insert_artifact(connection, delivery, artifact)
                    connection.commit()
                result = self.status()
                expected = {
                    "delivery": "malformed_delivery_identity",
                    "assembly": "malformed_assembly_identity",
                    "profile": "malformed_profile_item_identity",
                    "evidence": "malformed_evidence_identity",
                    "projection": "projection_binding_failure",
                }[name]
                self.assertEqual(("incompatible", expected), (
                    result["status"], result["reason_code"],
                ))

    def test_model_context_wire_and_provider_column_bindings_fail_closed(self):
        self.initialize_d1()
        delivery, artifact = self._canonical_artifact()
        delivery["model_context"] += " syntactically plausible tampering"
        delivery["delivery_identity"] = production_delivery_identity(delivery)
        with closing(sqlite3.connect(self.sidecar)) as connection:
            self._insert_artifact(connection, delivery, artifact)
            connection.commit()
        result = self.status()
        self.assertEqual(("incompatible", "delivery_binding_failure"), (
            result["status"], result["reason_code"],
        ))

    def test_error_receipt_and_terminal_outcome_bindings_fail_closed(self):
        self.sidecar = self.root / "runtime-error-identity.db"
        failed = self._insert_failed_outcome(self._insert_prepared_ledger())
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in ("runtime_errors", "runtime_run_events"):
                connection.execute(f"DROP TRIGGER {table}_no_update")
            tampered = "rerr_" + "b" * 64
            connection.execute("UPDATE runtime_errors SET error_id=?", (tampered,))
            connection.execute(
                "UPDATE runtime_run_events SET error_id=? WHERE event_type='state_failed'", (tampered,),
            )
            for table in ("runtime_errors", "runtime_run_events"):
                self._restore_update_trigger(connection, table)
            connection.commit()
        result = self.status()
        self.assertEqual(("incompatible", "malformed_error_identity"), (
            result["status"], result["reason_code"],
        ))
        self.assertTrue(failed["error_id"].startswith("rerr_"))

        self.sidecar = self.root / "runtime-receipt-identity.db"
        succeeded = self._insert_success_outcome(self._insert_prepared_ledger())
        receipt = deepcopy(succeeded["receipt"])
        receipt["response"]["content"] = "tampered but syntactically valid"
        receipt["receipt_identity"] = receipt_identity(receipt)
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("DROP TRIGGER runtime_receipts_no_update")
            connection.execute(
                "UPDATE runtime_receipts SET receipt_identity=?,receipt_json=?",
                (receipt["receipt_identity"], canonical_json(receipt)),
            )
            self._restore_update_trigger(connection, "runtime_receipts")
            connection.commit()
        result = self.status()
        self.assertEqual(("incompatible", "malformed_receipt_identity"), (
            result["status"], result["reason_code"],
        ))

        self.sidecar = self.root / "runtime-outcome-binding.db"
        self._insert_success_outcome(self._insert_prepared_ledger())
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("DROP TRIGGER runtime_run_events_no_update")
            connection.execute(
                "UPDATE runtime_run_events SET finish_reason='length' WHERE event_type='state_succeeded'"
            )
            self._restore_update_trigger(connection, "runtime_run_events")
            connection.commit()
        result = self.status()
        self.assertEqual(("incompatible", "ledger_binding_failure"), (
            result["status"], result["reason_code"],
        ))

    def test_foreign_key_failure_precedes_ready_state(self):
        self._insert_prepared_ledger()
        with closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER runtime_model_runs_no_update")
            connection.execute(
                "UPDATE runtime_model_runs SET conversation_id=?", ("rconv_" + "b" * 64,),
            )
            self._restore_update_trigger(connection, "runtime_model_runs")
            connection.commit()
        result = self.status()
        self.assertEqual(("integrity_failure", "database_integrity_failure"), (
            result["status"], result["reason_code"],
        ))

    def test_malformed_database_and_inspection_failure_are_distinct(self):
        self.sidecar.write_bytes(b"not a sqlite database")
        malformed = self.status()
        self.assertEqual(("integrity_failure", "database_malformed"), (
            malformed["status"], malformed["reason_code"],
        ))

        self.sidecar.unlink()
        self.initialize_d1()
        with patch(
            "memory.production_runtime_status._connect_readonly",
            side_effect=sqlite3.OperationalError("access denied"),
        ):
            failed = self.status()
        self.assertEqual(("inspection_failed", "inspection_failed"), (
            failed["status"], failed["reason_code"],
        ))

    def test_cli_api_and_status_projection_use_the_same_reader(self):
        self.initialize_d1()
        args = parse_runtime_args(["sidecar-status"])
        self.assertFalse(hasattr(args, "path"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_runtime_args(["sidecar-status", "--path", "other.db"])

        output = io.StringIO()
        with patch("memory.production_runtime_status.PRODUCTION_RUNTIME_SIDECAR", self.sidecar):
            expected = production_runtime_status()
            with redirect_stdout(output):
                self.assertEqual(0, runtime_cli_main(["sidecar-status"]))
        self.assertEqual(expected, json.loads(output.getvalue()))

        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH)
        handler.send_json = Mock()
        handler.send_error_json = Mock()
        with patch("memory.production_runtime_status.PRODUCTION_RUNTIME_SIDECAR", self.sidecar):
            handler.api_runtime_sidecar_status({})
        handler.send_json.assert_called_once_with(expected)

        handler.send_json.reset_mock()
        handler.api_runtime_sidecar_status({"path": ["other.db"]})
        handler.send_error_json.assert_called_with(
            "Runtime sidecar status accepts no parameters", HTTPStatus.BAD_REQUEST,
        )

        handler.send_json.reset_mock()
        with patch("memory.production_runtime_status.PRODUCTION_RUNTIME_SIDECAR", self.sidecar):
            handler.api_status()
        self.assertEqual(expected, handler.send_json.call_args.args[0]["runtime_sidecar"])

        handler.send_json.reset_mock()
        handler.app = Mock(readonly=False, db_mode="test", db_path=self.sidecar)
        handler.api_runtime_sidecar_status({})
        unavailable, http_status = handler.send_json.call_args.args
        self.assertEqual((
            "v8.0-f-production-runtime-status-1",
            "production_readonly_required",
            HTTPStatus.SERVICE_UNAVAILABLE,
        ), (
            unavailable["protocol_version"], unavailable["reason_code"], http_status,
        ))

    def test_ui_contract_is_get_only_and_contains_no_credential_surface(self):
        app_source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        html_source = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('view: "runtimeStatus"', app_source)
        self.assertIn('api("/api/runtime-sidecar-status")', app_source)
        for marker in (
            't("runtime.productionStatus"',
            '"DeepSeek"',
            't("runtime.modelRuns"',
            't("runtime.credentialRead"',
            't("runtime.sentToModel"',
            't("runtime.externalModelCalls"',
            't("runtime.appendOnly"',
        ):
            self.assertIn(marker, app_source)
        for forbidden in ("DEEPSEEK_API_KEY", "Authorization", "api_key", "deepseek key.txt"):
            self.assertNotIn(forbidden, app_source)

    def test_serialized_status_contains_no_secret_or_authorization_field(self):
        self.initialize_d1()
        serialized = json.dumps(self.status(), sort_keys=True).lower()
        for forbidden in ("api_key", "authorization", "credential_value", "bearer "):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
