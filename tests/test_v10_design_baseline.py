from __future__ import annotations

from copy import deepcopy
import json
import re
import sqlite3
from pathlib import Path
import unittest

from memory.cognitive_provider import suggested_provider_profiles


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "yunos_v10_cognitive_agency.schema.json"
SQL_PATH = ROOT / "docs" / "sql" / "yunos_v10_cognitive_agency_blueprint.sql"
EXECUTABLE_SQL_PATH = ROOT / "memory" / "cognitive_schema_10001.sql"
BASELINE_PATH = (
    ROOT / "docs" / "design" / "yunos_v10_bounded_cognitive_agency_baseline.md"
)
ADR_PATH = (
    ROOT / "docs" / "adr" / "yunos_v10_adr_001_bounded_cognitive_agency.md"
)
THREAT_PATH = ROOT / "docs" / "security" / "yunos_v10_threat_model.md"
MIGRATION_PLAN_PATH = (
    ROOT
    / "docs"
    / "design"
    / "yunos_v10_production_migration_acceptance_plan.md"
)
CLOSURE_PATH = (
    ROOT
    / "docs"
    / "version_reports"
    / "yunos_v10_0_agency_design_baseline_closure.md"
)
ZERO_GATE_PATH = (
    ROOT
    / "docs"
    / "version_reports"
    / "yunos_v9_0_1_final_full_regression.md"
)

RELEASE_NODES = ("v10.0", "v10-A", "v10-B", "v10-C", "v10-D", "v10-E", "v10-F")
DOCUMENT_DEFS = {
    "objective_contract",
    "plan_graph",
    "provider_profile",
    "capability",
    "context_binding",
    "authorization_artifact",
    "tool_proposal",
    "tool_execution",
    "cognitive_run",
    "cognitive_run_checkpoint",
    "supervision_event",
    "verification_record",
    "learning_candidate",
}
V10_TABLES = {
    "cognitive_schema",
    "objective_contracts",
    "cognitive_runs",
    "cognitive_run_checkpoints",
    "plan_graphs",
    "plan_nodes",
    "plan_edges",
    "plan_revisions",
    "provider_registry",
    "capability_registry",
    "context_bindings",
    "authorization_artifacts",
    "tool_proposals",
    "tool_executions",
}
V10_TRIGGERS = {
    "objective_contracts_no_update",
    "objective_contracts_no_delete",
    "provider_registry_no_update",
    "provider_registry_no_delete",
    "capability_registry_no_update",
    "capability_registry_no_delete",
    "plan_graphs_no_update",
    "plan_graphs_no_delete",
    "plan_nodes_no_update",
    "plan_nodes_no_delete",
    "plan_edges_no_update",
    "plan_edges_no_delete",
    "plan_revisions_no_update",
    "plan_revisions_no_delete",
    "authorization_artifacts_no_update",
    "authorization_artifacts_no_delete",
    "context_bindings_no_update",
    "context_bindings_no_delete",
    "cognitive_run_checkpoints_no_update",
    "cognitive_run_checkpoints_no_delete",
    "tool_proposals_no_update",
    "tool_proposals_no_delete",
    "tool_executions_no_update",
    "tool_executions_no_delete",
}
V10_INDEXES = {
    "objective_contracts_project_order",
    "plan_graphs_objective_order",
    "plan_edges_outgoing",
    "context_bindings_objective_source",
    "cognitive_runs_status_activity",
    "tool_proposals_run_status",
    "tool_executions_run_status",
}
REUSED_V9_TABLES = {
    "delegation_contracts",
    "outcome_records",
    "state_approvals",
    "state_source_anchors",
    "state_conflicts",
    "state_writer_leases",
    "state_events",
    "state_verifications",
    "state_candidates",
}


def load_schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def local_ref_target(schema: dict[str, object], reference: str) -> object:
    if not reference.startswith("#/"):
        raise AssertionError(f"non-local reference: {reference}")
    target: object = schema
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or token not in target:
            raise AssertionError(f"unresolved reference: {reference}")
        target = target[token]
    return target


def walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def _json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _schema_type_matches(value: object, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[expected]


def schema_accepts(
    root: dict[str, object],
    schema: object,
    instance: object,
) -> bool:
    """Evaluate the Draft 2020-12 subset used by the v10 contracts.

    The project intentionally has no runtime JSON-Schema dependency.  This
    focused evaluator makes the provider-profile conditionals executable in
    the standard-library test suite instead of merely asserting their text.
    """

    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise AssertionError("schema node must be an object or boolean")
    reference = schema.get("$ref")
    if reference is not None and not schema_accepts(
        root, local_ref_target(root, reference), instance
    ):
        return False
    expected_type = schema.get("type")
    if expected_type is not None:
        alternatives = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not any(_schema_type_matches(instance, item) for item in alternatives):
            return False
    if "const" in schema and not _json_equal(instance, schema["const"]):
        return False
    if "enum" in schema and not any(
        _json_equal(instance, candidate) for candidate in schema["enum"]
    ):
        return False
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            return False
        maximum = schema.get("maxLength")
        if maximum is not None and len(instance) > maximum:
            return False
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            return False
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            return False
        if "maximum" in schema and instance > schema["maximum"]:
            return False
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            return False
        maximum = schema.get("maxItems")
        if maximum is not None and len(instance) > maximum:
            return False
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in instance}
        ) != len(instance):
            return False
        if "items" in schema and any(
            not schema_accepts(root, schema["items"], item) for item in instance
        ):
            return False
        if "contains" in schema and not any(
            schema_accepts(root, schema["contains"], item) for item in instance
        ):
            return False
    if isinstance(instance, dict):
        if any(key not in instance for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in instance and not schema_accepts(
                root, subschema, instance[key]
            ):
                return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in instance
        ):
            return False
    if "not" in schema and schema_accepts(root, schema["not"], instance):
        return False
    if "allOf" in schema and not all(
        schema_accepts(root, subschema, instance)
        for subschema in schema["allOf"]
    ):
        return False
    if "anyOf" in schema and not any(
        schema_accepts(root, subschema, instance)
        for subschema in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        schema_accepts(root, subschema, instance)
        for subschema in schema["oneOf"]
    ) != 1:
        return False
    condition = schema.get("if")
    if condition is not None:
        matched = schema_accepts(root, condition, instance)
        branch = schema.get("then") if matched else schema.get("else")
        if branch is not None and not schema_accepts(root, branch, instance):
            return False
    return True


class V10JsonContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema()
        cls.defs = cls.schema["$defs"]

    def test_schema_is_2020_12_strict_envelope(self) -> None:
        self.assertEqual(
            self.schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(self.schema["properties"]["schema_generation"]["const"], 10001)
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(
            set(self.schema["properties"]["document_type"]["enum"]),
            DOCUMENT_DEFS,
        )
        refs = {
            entry["$ref"].rsplit("/", 1)[-1]
            for entry in self.schema["properties"]["document"]["oneOf"]
        }
        self.assertEqual(refs, DOCUMENT_DEFS)

    def test_all_local_references_resolve(self) -> None:
        references = [
            node["$ref"]
            for node in walk(self.schema)
            if isinstance(node, dict) and "$ref" in node
        ]
        self.assertGreater(len(references), 50)
        for reference in references:
            local_ref_target(self.schema, reference)

    def test_declared_contract_objects_are_closed_and_complete(self) -> None:
        for name in DOCUMENT_DEFS | {"plan_node", "plan_edge", "plan_revision"}:
            definition = self.defs[name]
            self.assertEqual(definition["type"], "object", name)
            self.assertFalse(definition["additionalProperties"], name)
            self.assertEqual(
                set(definition["required"]),
                set(definition["properties"]),
                f"{name} must require every declared field",
            )

    def test_objective_plan_and_control_enums_match_the_design(self) -> None:
        self.assertEqual(
            self.defs["agency_level"]["enum"],
            ["consult", "plan", "prepare", "act"],
        )
        self.assertEqual(
            self.defs["cognitive_stance"]["enum"],
            ["continue", "explore", "breakthrough"],
        )
        expected_states = {
            "queued",
            "reasoning",
            "planning",
            "waiting_authority",
            "executing",
            "waiting_external",
            "verifying",
            "waiting_acceptance",
            "completed",
            "failed",
            "cancelled",
            "recovery_required",
        }
        self.assertEqual(
            set(self.defs["cognitive_run"]["properties"]["status"]["enum"]),
            expected_states,
        )
        objective_fields = {
            "objective_id",
            "user_utterance",
            "interpreted_goal",
            "project_id",
            "agency_level",
            "agency_selection",
            "cognitive_stance",
            "success_criteria",
            "prohibited_outcomes",
            "risk_tolerance",
            "time_constraints",
            "resource_constraints",
            "assumptions",
            "ambiguities",
            "clarification_required",
            "context_requirements",
            "acceptance_policy",
            "authority_request",
            "source_anchors",
            "canonical_digest",
            "version",
        }
        self.assertEqual(
            set(self.defs["objective_contract"]["required"]),
            objective_fields,
        )

    def test_plan_schema_matches_runtime_cross_field_constraints(self) -> None:
        node_schema = self.defs["plan_node"]
        node = {
            "node_id": "node:one",
            "objective_id": "objective:one",
            "title": "Inspect the bounded target",
            "purpose": "Produce independently verifiable evidence",
            "dependencies": [],
            "node_kind": "action",
            "capability_requirements": [],
            "read_scope": [],
            "write_scope": [],
            "risk": "low",
            "reversible": True,
            "authority_requirement": "none",
            "success_criteria": ["The bounded observation is recorded"],
            "verification": ["Re-read the bounded target"],
            "rollback": [],
            "timeout_ms": 1000,
            "retry_policy": {
                "maximum_attempts": 1,
                "equivalent_retry_limit": 0,
                "backoff_ms": 0,
                "retryable_failures": [],
            },
            "state": "proposed",
            "evidence": [],
            "omitted_reason": None,
        }
        self.assertTrue(schema_accepts(self.schema, node_schema, node))

        omitted = deepcopy(node)
        omitted.update(state="omitted", omitted_reason="Not selected")
        self.assertTrue(schema_accepts(self.schema, node_schema, omitted))
        omitted["omitted_reason"] = None
        self.assertFalse(schema_accepts(self.schema, node_schema, omitted))
        unexpected_reason = deepcopy(node)
        unexpected_reason["omitted_reason"] = "Not permitted here"
        self.assertFalse(
            schema_accepts(self.schema, node_schema, unexpected_reason)
        )

        human_gate = deepcopy(node)
        human_gate["node_kind"] = "human_gate"
        self.assertFalse(schema_accepts(self.schema, node_schema, human_gate))
        human_gate["authority_requirement"] = "human_gate"
        self.assertTrue(schema_accepts(self.schema, node_schema, human_gate))
        human_gate["state"] = "running"
        self.assertFalse(schema_accepts(self.schema, node_schema, human_gate))

        rollback = deepcopy(node)
        rollback["node_kind"] = "rollback"
        self.assertFalse(schema_accepts(self.schema, node_schema, rollback))
        rollback["rollback"] = ["Restore the owned backup"]
        self.assertTrue(schema_accepts(self.schema, node_schema, rollback))

        irreversible = deepcopy(node)
        irreversible["reversible"] = False
        self.assertFalse(schema_accepts(self.schema, node_schema, irreversible))
        irreversible["authority_requirement"] = "explicit_act"
        self.assertTrue(schema_accepts(self.schema, node_schema, irreversible))

        reversible_write = deepcopy(node)
        reversible_write["write_scope"] = ["project:bounded"]
        self.assertFalse(
            schema_accepts(self.schema, node_schema, reversible_write)
        )
        reversible_write["rollback"] = ["Restore the owned backup"]
        self.assertTrue(schema_accepts(self.schema, node_schema, reversible_write))
        reversible_write["verification"] = []
        self.assertFalse(schema_accepts(self.schema, node_schema, reversible_write))

        edge_schema = self.defs["plan_edge"]
        edge = {
            "edge_id": "edge:one",
            "from_node_id": "node:one",
            "to_node_id": "node:two",
            "edge_kind": "sequence",
            "condition": None,
            "priority": 0,
        }
        self.assertTrue(schema_accepts(self.schema, edge_schema, edge))
        edge["condition"] = "Unexpected condition"
        self.assertFalse(schema_accepts(self.schema, edge_schema, edge))
        edge.update(edge_kind="decision", condition="User selected safe path")
        self.assertTrue(schema_accepts(self.schema, edge_schema, edge))
        edge["condition"] = None
        self.assertFalse(schema_accepts(self.schema, edge_schema, edge))
        edge["condition"] = ""
        self.assertFalse(schema_accepts(self.schema, edge_schema, edge))
        edge.update(condition="User selected safe path", priority=1_000_001)
        self.assertFalse(schema_accepts(self.schema, edge_schema, edge))

    def test_runtime_nullable_fields_match_runtime_validation(self) -> None:
        run = self.defs["cognitive_run"]["properties"]
        checkpoint = self.defs["cognitive_run_checkpoint"]["properties"]
        proposal = self.defs["tool_proposal"]["properties"]
        execution = self.defs["tool_execution"]["properties"]

        for schema in (
            run["failure_code"],
            checkpoint["failure_code"],
        ):
            self.assertTrue(schema_accepts(self.schema, schema, None))
            self.assertTrue(
                schema_accepts(self.schema, schema, "bounded_failure")
            )
            self.assertFalse(schema_accepts(self.schema, schema, ""))

        safe_checkpoint = run["last_safe_checkpoint"]
        self.assertTrue(schema_accepts(self.schema, safe_checkpoint, None))
        self.assertTrue(
            schema_accepts(self.schema, safe_checkpoint, "a" * 64)
        )
        for invalid in ("", "not-a-digest", "A" * 64):
            self.assertFalse(
                schema_accepts(self.schema, safe_checkpoint, invalid)
            )

        for schema in (
            proposal["runtime_execution_reference"],
            execution["runtime_execution_reference"],
        ):
            self.assertTrue(schema_accepts(self.schema, schema, None))
            self.assertTrue(
                schema_accepts(self.schema, schema, "runtime:model:one")
            )
            self.assertFalse(schema_accepts(self.schema, schema, ""))

        for schema in (
            execution["reservation_event_id"],
            execution["preview_id"],
        ):
            self.assertTrue(schema_accepts(self.schema, schema, None))
            self.assertTrue(
                schema_accepts(self.schema, schema, "stateevent:one")
            )
            self.assertFalse(schema_accepts(self.schema, schema, ""))

        for schema in (
            proposal["runtime_receipt_digest"],
            execution["reservation_receipt_digest"],
            execution["preview_digest"],
            execution["runtime_receipt_digest"],
        ):
            self.assertTrue(schema_accepts(self.schema, schema, None))
            self.assertTrue(schema_accepts(self.schema, schema, "b" * 64))
            self.assertFalse(schema_accepts(self.schema, schema, ""))

    def test_authorization_is_default_deny_and_never_self_expands(self) -> None:
        properties = self.defs["authorization_artifact"]["properties"]
        for field in (
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
        ):
            self.assertIs(properties[field]["default"], False, field)
        self.assertEqual(properties["database_permission"]["default"], "none")
        self.assertIs(properties["next_version_creation_permission"]["const"], False)
        self.assertIs(
            properties["persistent_background_agent_permission"]["const"],
            False,
        )
        acceptance = self.defs["acceptance_policy"]["properties"]
        self.assertIs(acceptance["automatic_allowed"]["default"], False)
        self.assertIs(acceptance["requires_user_event"]["default"], True)
        for field in (
            "require_system_verification",
            "require_reversible",
            "require_no_conflicts",
            "require_no_overreach",
            "require_no_unresolved_warnings",
        ):
            self.assertIs(acceptance[field]["const"], True)

    def test_verification_acceptance_and_learning_are_separate(self) -> None:
        verification = self.defs["verification_record"]["properties"]
        self.assertIn("acceptance_state", verification)
        self.assertIn("acceptance_event_id", verification)
        learning = self.defs["learning_candidate"]["properties"]
        self.assertEqual(
            set(learning["candidate_kind"]["enum"]),
            {
                "fact_record",
                "observation",
                "pattern_candidate",
                "workflow_candidate",
            },
        )
        self.assertIs(learning["requires_user_approval"]["const"], True)
        self.assertIs(learning["long_term_memory_write_allowed"]["const"], False)

    def test_search_contract_is_local_and_not_a_cognitive_document(self) -> None:
        description = self.schema["description"].lower()
        self.assertIn("search mode remains local", description)
        self.assertIn("never invokes a provider", description)
        self.assertNotIn("search", self.schema["properties"]["document_type"]["enum"])

    def test_provider_profile_schema_enforces_cross_field_security(self) -> None:
        provider_schema = self.defs["provider_profile"]
        profiles = {
            profile.provider_id: profile.to_dict()
            for profile in suggested_provider_profiles(
                (
                    "secret.deepseek.production",
                    "secret.kimi.compatible",
                ),
                allow_fake=True,
            )
        }
        for provider_id, profile in profiles.items():
            self.assertTrue(
                schema_accepts(self.schema, provider_schema, profile),
                provider_id,
            )

        deepseek = profiles["deepseek"]
        local = profiles["local.openai_compatible"]
        fake = profiles["fake"]
        invalid: dict[str, dict[str, object]] = {}

        def changed(
            name: str,
            source: dict[str, object],
            **fields: object,
        ) -> dict[str, object]:
            value = deepcopy(source)
            value.update(fields)
            invalid[name] = value
            return value

        changed("remote_http", deepseek, endpoint="http://api.deepseek.com")
        changed(
            "remote_userinfo",
            deepseek,
            endpoint="https://user@api.deepseek.com",
        )
        changed(
            "remote_query",
            deepseek,
            endpoint="https://api.deepseek.com?token=value",
        )
        changed("local_remote_host", local, endpoint="http://example.com/v1")
        changed("local_https", local, endpoint="https://127.0.0.1:11434/v1")
        changed("fake_https", fake, endpoint="https://contract.invalid/v1")
        changed("profile_version_overflow", deepseek, profile_version=2_147_483_648)
        changed("invalid_model", deepseek, model="model with spaces")

        known_cost = deepcopy(deepseek["cost_metadata"])
        known_cost["pricing_state"] = "known"
        changed("known_cost_missing_prices", deepseek, cost_metadata=known_cost)
        unknown_cost = deepcopy(deepseek["cost_metadata"])
        unknown_cost["input_microunits_per_million_tokens"] = 1
        changed("unknown_cost_with_price", deepseek, cost_metadata=unknown_cost)
        not_applicable_cost = deepcopy(deepseek["cost_metadata"])
        not_applicable_cost["pricing_state"] = "not_applicable"
        changed(
            "not_applicable_with_currency",
            deepseek,
            cost_metadata=not_applicable_cost,
        )

        changed(
            "enabled_but_disabled",
            deepseek,
            health_state="disabled",
            health_reason="profile_disabled",
        )
        changed("disabled_health_mismatch", deepseek, enabled=False)
        changed(
            "healthy_reason_mismatch",
            deepseek,
            health_state="healthy",
            health_reason="health_not_checked",
        )
        changed(
            "credential_reason_without_reference",
            deepseek,
            secret_reference=None,
            health_state="unavailable",
            health_reason="credential_reference_unavailable",
        )
        optional_reference = deepcopy(deepseek["secret_reference"])
        optional_reference["required"] = False
        changed(
            "credential_reason_with_optional_reference",
            deepseek,
            secret_reference=optional_reference,
            health_state="unavailable",
            health_reason="credential_reference_unavailable",
        )
        changed(
            "credential_reason_wrong_state",
            deepseek,
            health_reason="credential_reference_unavailable",
        )

        changed(
            "tool_none_with_capability",
            deepseek,
            capability_flags=[*deepseek["capability_flags"], "tool_proposals"],
        )
        changed(
            "tool_support_without_capability",
            deepseek,
            tool_calling_support="compatible",
        )
        changed(
            "structured_none_with_capability",
            deepseek,
            capability_flags=[*deepseek["capability_flags"], "structured_output"],
        )
        changed(
            "structured_support_without_capability",
            deepseek,
            structured_output_support="json_schema",
        )
        changed(
            "reasoning_with_disabled_only",
            deepseek,
            capability_flags=[*deepseek["capability_flags"], "reasoning"],
        )
        changed(
            "local_only_on_remote_adapter",
            deepseek,
            capability_flags=[*deepseek["capability_flags"], "local_only"],
        )
        changed(
            "fake_capability_on_remote_adapter",
            deepseek,
            capability_flags=[
                *deepseek["capability_flags"],
                "deterministic_fake",
            ],
        )

        for name, profile in invalid.items():
            self.assertFalse(
                schema_accepts(self.schema, provider_schema, profile),
                name,
            )


class V10SqlBlueprintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = SQL_PATH.read_text(encoding="utf-8")
        cls.sql_lower = cls.sql.lower()
        cls.created_tables = set(
            re.findall(
                r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?([a-z0-9_]+)",
                cls.sql_lower,
            )
        )

    def test_design_only_marker_and_generation_are_unambiguous(self) -> None:
        first_lines = "\n".join(self.sql.splitlines()[:8])
        self.assertIn("DESIGN ONLY", first_lines)
        self.assertIn("DO NOT APPLY", first_lines)
        self.assertRegex(
            self.sql,
            r"schema_generation\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*"
            r"\(\s*schema_generation\s*=\s*10001\s*\)",
        )

    def test_additive_operational_tables_exist_without_a_sixth_database(self) -> None:
        self.assertTrue(V10_TABLES.issubset(self.created_tables))
        self.assertTrue(REUSED_V9_TABLES.isdisjoint(self.created_tables))
        self.assertNotRegex(self.sql_lower, r"\battach\b\s+(?:database\s+)?['\"]")
        self.assertNotRegex(self.sql_lower, r"\bcreate\s+database\b")
        self.assertNotIn("runtime_receipts", self.created_tables)
        self.assertNotIn("runtime_ledger", self.created_tables)

    def test_sql_permissions_are_fail_closed(self) -> None:
        for field in (
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
        ):
            pattern = rf"{field}\s+integer\s+not\s+null\s+default\s+0"
            self.assertRegex(self.sql_lower, pattern)

    def test_immutable_audit_records_have_update_and_delete_guards(self) -> None:
        for table in (
            "objective_contracts",
            "plan_graphs",
            "plan_nodes",
            "plan_edges",
            "plan_revisions",
            "authorization_artifacts",
            "tool_executions",
        ):
            self.assertRegex(
                self.sql_lower,
                rf"before\s+update\s+on\s+{table}\b",
                table,
            )
            self.assertRegex(
                self.sql_lower,
                rf"before\s+delete\s+on\s+{table}\b",
                table,
            )

    def test_tool_execution_is_an_immutable_terminal_receipt(self) -> None:
        table = re.search(
            r"create\s+table\s+tool_executions\s*\((.*?)\n\);",
            self.sql_lower,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(table)
        definition = table.group(1)
        for transient_status in ("'queued'", "'running'", "'suspected_stall'"):
            self.assertNotIn(transient_status, definition)
        for terminal_status in (
            "'denied'",
            "'succeeded'",
            "'failed'",
            "'cancelled'",
            "'recovery_required'",
        ):
            self.assertIn(terminal_status, definition)
        self.assertIn("cognitive_run_checkpoints", self.sql_lower)
        self.assertIn("state_events supervision stream", self.sql_lower)

    def test_denied_tool_receipt_requires_no_fake_path_or_argv(self) -> None:
        tool_execution = load_schema()["$defs"]["tool_execution"]
        self.assertEqual(
            tool_execution["properties"]["resolved_argv"]["minItems"],
            0,
        )
        self.assertIn(
            "null",
            tool_execution["properties"]["working_directory"]["type"],
        )
        conditional = tool_execution["allOf"][0]
        denied = conditional["else"]["properties"]
        self.assertEqual(denied["resolved_argv"]["maxItems"], 0)
        self.assertEqual(denied["working_directory"]["type"], "null")
        self.assertEqual(denied["status"]["const"], "denied")
        self.assertEqual(denied["started_at"]["type"], "null")
        self.assertEqual(denied["exit_code"]["type"], "null")
        self.assertIsNotNone(
            re.search(
                r"policy_decision\s*<>\s*'allow'.*?"
                r"json_array_length\(resolved_argv_json\)\s*=\s*0.*?"
                r"working_directory\s+is\s+null.*?"
                r"status\s*=\s*'denied'",
                self.sql_lower,
                flags=re.DOTALL,
            )
        )

    def test_supervision_verification_and_learning_reuse_v9_storage(self) -> None:
        self.assertTrue(REUSED_V9_TABLES.isdisjoint(self.created_tables))
        mappings = {
            "supervision_event  -> state_events",
            "verification_record -> state_verifications",
            "learning_candidate -> state_candidates",
        }
        for mapping in mappings:
            self.assertIn(mapping, self.sql_lower)

    def test_blueprint_is_valid_sql_against_v9_contract_stubs(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE state_source_anchors(anchor_id TEXT PRIMARY KEY);
                CREATE TABLE state_events(event_id TEXT PRIMARY KEY);
                CREATE TABLE state_approvals(approval_id TEXT PRIMARY KEY);
                CREATE TABLE outcome_records(outcome_id TEXT PRIMARY KEY);
                """
            )
            connection.executescript(self.sql)
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(V10_TABLES.issubset(names))
        finally:
            connection.close()

    def test_executable_resource_is_the_exact_complete_10001_schema(self) -> None:
        resource = EXECUTABLE_SQL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("DESIGN ONLY", resource)
        self.assertNotIn("DO NOT APPLY", resource)
        resource_objects = {
            (match.group(1).lower(), match.group(2).lower())
            for match in re.finditer(
                r"^CREATE\s+(TABLE|TRIGGER|INDEX)\s+([a-z0-9_]+)",
                resource,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        }
        expected_objects = (
            {("table", name) for name in V10_TABLES}
            | {("trigger", name) for name in V10_TRIGGERS}
            | {("index", name) for name in V10_INDEXES}
        )
        self.assertEqual(14, len(V10_TABLES))
        self.assertEqual(24, len(V10_TRIGGERS))
        self.assertEqual(7, len(V10_INDEXES))
        self.assertEqual(expected_objects, resource_objects)
        self.assertEqual(45, len(resource_objects))

        blueprint_objects = {
            (match.group(1).lower(), match.group(2).lower())
            for match in re.finditer(
                r"^CREATE\s+(TABLE|TRIGGER|INDEX)\s+([a-z0-9_]+)",
                self.sql,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        }
        self.assertEqual(blueprint_objects, resource_objects)
        blueprint_ddl = self.sql[self.sql.index("CREATE TABLE cognitive_schema") :]
        resource_ddl = resource[resource.index("CREATE TABLE cognitive_schema") :]

        def without_line_comments(value: str) -> str:
            return "\n".join(
                line.rstrip()
                for line in value.splitlines()
                if line.strip() and not line.lstrip().startswith("--")
            ).strip()

        self.assertEqual(
            without_line_comments(blueprint_ddl),
            without_line_comments(resource_ddl),
        )

    def test_executable_resource_runs_independently_against_v9_stubs(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE state_source_anchors(anchor_id TEXT PRIMARY KEY);
                CREATE TABLE state_events(event_id TEXT PRIMARY KEY);
                CREATE TABLE state_approvals(approval_id TEXT PRIMARY KEY);
                CREATE TABLE outcome_records(outcome_id TEXT PRIMARY KEY);
                """
            )
            connection.executescript(
                EXECUTABLE_SQL_PATH.read_text(encoding="utf-8")
            )
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                )
                if not row[1].startswith("state_")
                and row[1] != "outcome_records"
            }
            expected_objects = (
                {("table", name) for name in V10_TABLES}
                | {("trigger", name) for name in V10_TRIGGERS}
                | {("index", name) for name in V10_INDEXES}
            )
            self.assertEqual(expected_objects, objects)
        finally:
            connection.close()


class V10RouteBoundaryTest(unittest.TestCase):
    def test_release_node_sequence_is_unique_and_terminal_at_v10_f(self) -> None:
        self.assertEqual(
            RELEASE_NODES,
            ("v10.0", "v10-A", "v10-B", "v10-C", "v10-D", "v10-E", "v10-F"),
        )
        self.assertEqual(len(RELEASE_NODES), len(set(RELEASE_NODES)))
        self.assertEqual(RELEASE_NODES[-1], "v10-F")
        artifacts = (
            SCHEMA_PATH.read_text(encoding="utf-8")
            + "\n"
            + SQL_PATH.read_text(encoding="utf-8")
        )
        self.assertNotRegex(artifacts, r"\bv10-(?:G|G0)\b|\bv10\.1\b|\bv11\b")


class V10DesignDocumentTest(unittest.TestCase):
    def test_complete_design_artifact_set_exists(self) -> None:
        for path in (
            BASELINE_PATH,
            ADR_PATH,
            THREAT_PATH,
            MIGRATION_PLAN_PATH,
            CLOSURE_PATH,
            ZERO_GATE_PATH,
            SCHEMA_PATH,
            SQL_PATH,
        ):
            self.assertTrue(path.is_file(), path)
            self.assertGreater(path.stat().st_size, 100, path)

    def test_threat_model_has_all_required_control_fields(self) -> None:
        text = THREAT_PATH.read_text(encoding="utf-8")
        headings = list(
            re.finditer(r"^### T(\d{2}) — .+$", text, flags=re.MULTILINE)
        )
        self.assertEqual(
            [match.group(1) for match in headings],
            [f"{index:02d}" for index in range(1, 36)],
        )
        required = (
            "- **风险：**",
            "- **触发：**",
            "- **预防：**",
            "- **检测：**",
            "- **恢复：**",
            "- **fail-closed：**",
            "- **测试：**",
        )
        for index, heading in enumerate(headings):
            end = (
                headings[index + 1].start()
                if index + 1 < len(headings)
                else len(text)
            )
            block = text[heading.start():end]
            for marker in required:
                self.assertIn(marker, block, f"T{heading.group(1)} missing {marker}")

    def test_migration_plan_covers_all_production_gates(self) -> None:
        text = MIGRATION_PLAN_PATH.read_text(encoding="utf-8").lower()
        for marker in (
            "design only",
            "main db",
            "profile db",
            "semantic db",
            "runtime db",
            "operational state db",
            "sidecar",
            "assets",
            "clone",
            "failure injection",
            "rollback",
            "restore",
            "v9 regression",
            "writer lease",
            "real-readonly",
            "tailnet",
            "funnel",
            "get-only",
            "no-store",
        ):
            self.assertIn(marker, text, marker)

    def test_design_decisions_are_consistent_across_artifacts(self) -> None:
        baseline = BASELINE_PATH.read_text(encoding="utf-8")
        adr = ADR_PATH.read_text(encoding="utf-8")
        sql = SQL_PATH.read_text(encoding="utf-8")
        combined = "\n".join((baseline, adr, sql))
        self.assertIn("9001", combined)
        self.assertIn("10001", combined)
        self.assertIn("cognitive_schema", combined)
        self.assertIn("不创建第六个数据库", baseline)
        self.assertIn("local broker", adr)
        self.assertIn("v8 Runtime", adr)
        self.assertIn("supervision_event  -> state_events", sql)
        self.assertIn("verification_record -> state_verifications", sql)
        self.assertIn("learning_candidate -> state_candidates", sql)


if __name__ == "__main__":
    unittest.main()
