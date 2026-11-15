from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from memory.cognitive_objective import (
    DEFAULT_PROHIBITED_OUTCOMES,
    AcceptancePolicy,
    AmbiguitySignal,
    AuthorityRequest,
    ContextRequirement,
    ObjectiveContract,
    ObjectiveIntentError,
    ObjectiveSourceAnchor,
    build_objective_contract,
    classify_ambiguities,
    default_context_requirements,
    resolve_agency_level,
    resolve_objective_intent,
)
from memory.operational_state_models import OperationalStateError, canonical_digest


T0 = "2026-07-31T00:00:00Z"
T1 = "2026-07-31T00:01:00Z"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class V10ObjectiveIntentTest(unittest.TestCase):
    def make_anchor(
        self,
        *,
        anchor_id: str = "anchor:user:v10-objective",
        locator: str = "task-authority:v10-objective",
        observed_at: str = T0,
        source_kind: str = "user_declaration",
    ) -> ObjectiveSourceAnchor:
        return ObjectiveSourceAnchor(
            anchor_id=anchor_id,
            source_kind=source_kind,
            locator=locator,
            evidence_digest=digest(anchor_id),
            observed_at=observed_at,
            privacy_scope="private_metadata",
        )

    def make_contract(self, **overrides) -> ObjectiveContract:
        values = {
            "user_utterance": "为真实项目生成一个可验证的计划。",
            "interpreted_goal": "生成一个不执行写入的可验证计划。",
            "project_id": "project:ayun-english-core",
            "source_anchors": (self.make_anchor(),),
            "success_criteria": (
                "生成 Objective Contract",
                "生成成功标准并保持零写入",
            ),
            "agency_level": "plan",
            "agency_selected_by": "user",
            "maximum_authorized_agency_level": "plan",
            "cognitive_stance": "continue",
            "prohibited_outcomes": ("不得修改项目",),
            "risk_tolerance": "low",
            "time_constraints": ("本轮完成",),
            "resource_constraints": ("只使用本地证据",),
            "assumptions": ("项目存在可读 Resume Package",),
        }
        values.update(overrides)
        return build_objective_contract(**values)

    def test_consult_contract_matches_strict_schema_shape(self) -> None:
        contract = self.make_contract(
            agency_level="consult",
            maximum_authorized_agency_level="consult",
        )
        envelope = contract.to_envelope()
        self.assertEqual(envelope["schema_generation"], 10001)
        self.assertEqual(envelope["document_type"], "objective_contract")
        expected_fields = {
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
        self.assertEqual(set(envelope["document"]), expected_fields)
        self.assertFalse(contract.grants_execution_authority)
        self.assertFalse(contract.execution_ready)
        self.assertFalse(contract.authority_request.grants_authority)

    def test_deterministic_digest_ignores_set_like_input_order(self) -> None:
        anchor_a = self.make_anchor(
            anchor_id="anchor:user:v10-a",
            locator="authority:a",
        )
        anchor_b = self.make_anchor(
            anchor_id="anchor:user:v10-b",
            locator="authority:b",
        )
        contexts = (
            ContextRequirement(
                source_system="project",
                purpose="Read the minimum project evidence",
                maximum_items=4,
                maximum_bytes=4096,
                required=True,
            ),
            ContextRequirement(
                source_system="v9_now",
                purpose="Bind current state",
                maximum_items=1,
                maximum_bytes=2048,
                required=True,
            ),
        )
        first = self.make_contract(
            source_anchors=(anchor_a, anchor_b),
            agency_selection_source_anchor_id=anchor_a.anchor_id,
            success_criteria=("criterion:b", "criterion:a"),
            prohibited_outcomes=("deny:b", "deny:a"),
            time_constraints=("time:b", "time:a"),
            resource_constraints=("resource:b", "resource:a"),
            assumptions=("assumption:b", "assumption:a"),
            context_requirements=contexts,
        )
        second = self.make_contract(
            source_anchors=(anchor_b, anchor_a),
            agency_selection_source_anchor_id=anchor_a.anchor_id,
            success_criteria=("criterion:a", "criterion:b"),
            prohibited_outcomes=("deny:a", "deny:b"),
            time_constraints=("time:a", "time:b"),
            resource_constraints=("resource:a", "resource:b"),
            assumptions=("assumption:a", "assumption:b"),
            context_requirements=tuple(reversed(contexts)),
        )
        self.assertEqual(first.objective_id, second.objective_id)
        self.assertEqual(first.canonical_digest, second.canonical_digest)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            first.canonical_digest,
            canonical_digest(first.identity_payload()),
        )

    def test_nfc_equivalent_inputs_have_identical_contracts(self) -> None:
        composed = "讨论 café 的下一步"
        decomposed = "讨论 cafe\u0301 的下一步"
        first = self.make_contract(
            user_utterance=composed,
            interpreted_goal=composed,
            success_criteria=("café 方案可验证",),
        )
        second = self.make_contract(
            user_utterance=decomposed,
            interpreted_goal=decomposed,
            success_criteria=("cafe\u0301 方案可验证",),
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.canonical_digest, second.canonical_digest)

    def test_from_dict_round_trip_and_digest_tamper_detection(self) -> None:
        contract = self.make_contract()
        restored = ObjectiveContract.from_dict(
            json.loads(json.dumps(contract.to_dict(), ensure_ascii=False))
        )
        self.assertEqual(restored, contract)
        tampered = contract.to_dict()
        tampered["interpreted_goal"] = "越权执行"
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "objective_digest_mismatch",
        ):
            ObjectiveContract.from_dict(tampered)

    def test_identity_and_utc_timestamp_validation_are_reused(self) -> None:
        with self.assertRaisesRegex(
            OperationalStateError,
            "invalid_anchor_id",
        ):
            self.make_anchor(anchor_id="Invalid ID")
        with self.assertRaisesRegex(
            OperationalStateError,
            "timestamp_must_be_utc",
        ):
            self.make_anchor(observed_at="2026-07-31T08:00:00+08:00")

    def test_agent_cannot_elevate_user_selected_agency(self) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "agent_agency_escalation_denied",
        ):
            resolve_agency_level(
                selected_level="plan",
                selected_by="user",
                maximum_authorized_level="act",
                agent_proposed_level="act",
            )
        self.assertEqual(
            resolve_agency_level(
                selected_level="plan",
                selected_by="user",
                maximum_authorized_level="act",
                agent_proposed_level="consult",
            ),
            "plan",
        )

    def test_untrusted_selector_and_default_escalation_are_denied(self) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "untrusted_agency_selector",
        ):
            resolve_agency_level(
                selected_level="act",
                selected_by="agent",
            )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "default_agency_must_be_consult",
        ):
            resolve_agency_level(
                selected_level="plan",
                selected_by="default",
            )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "agency_level_exceeds_authorized_maximum",
        ):
            resolve_agency_level(
                selected_level="act",
                selected_by="user",
                maximum_authorized_level="prepare",
            )

    def test_cognitive_stance_does_not_expand_permissions(self) -> None:
        contracts = [
            self.make_contract(cognitive_stance=stance)
            for stance in ("continue", "explore", "breakthrough")
        ]
        for contract in contracts:
            self.assertEqual(contract.agency_level, "plan")
            self.assertFalse(contract.authority_request.requests_write)
            self.assertFalse(contract.grants_execution_authority)

    def test_every_high_impact_signal_requires_clarification(self) -> None:
        kinds = (
            "material_goal_change",
            "material_risk_change",
            "irreversible_operation",
            "new_write_scope",
            "account_action",
            "money_action",
            "external_publication",
            "major_value_conflict",
            "evidence_conflict",
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                assessment = classify_ambiguities(
                    (AmbiguitySignal(kind=kind, detail=f"fixture:{kind}"),),
                    success_criteria=("fixture is verified",),
                )
                self.assertTrue(assessment.clarification_required)
                self.assertEqual(len(assessment.hard_reasons), 1)
                self.assertTrue(assessment.readonly_work_may_continue)

    def test_low_impact_checks_do_not_interrupt_the_user(self) -> None:
        signals = tuple(
            AmbiguitySignal(kind=kind, detail=f"fixture:{kind}")
            for kind in (
                "required_project_read",
                "existing_test_run",
                "git_status_check",
                "obvious_local_fix",
                "verification_report",
                "authorized_reversible_step",
            )
        )
        assessment = classify_ambiguities(
            tuple(reversed(signals)),
            success_criteria=("all checks pass",),
        )
        self.assertFalse(assessment.clarification_required)
        self.assertFalse(assessment.hard_reasons)
        self.assertEqual(len(assessment.low_impact_reasons), len(signals))
        self.assertTrue(assessment.readonly_work_may_continue)

    def test_missing_success_standard_is_a_hard_clarification(self) -> None:
        resolution = resolve_objective_intent(
            user_utterance="帮我处理一下。",
            success_criteria=(),
        )
        self.assertFalse(resolution.contract_ready)
        self.assertTrue(
            resolution.ambiguity_assessment.clarification_required
        )
        self.assertTrue(
            any(
                reason.startswith("missing_success_criteria:")
                for reason in resolution.ambiguity_assessment.hard_reasons
            )
        )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "success_criteria_required_before_contract",
        ):
            build_objective_contract(
                user_utterance="帮我处理一下。",
                project_id="project:yunos",
                source_anchors=(self.make_anchor(),),
                success_criteria=(),
            )

    def test_invalid_ambiguity_kind_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "invalid_ambiguity_kind",
        ):
            AmbiguitySignal(
                kind="agent_made_up_reason",
                detail="unknown classification",
            )

    def test_scalar_string_is_not_accepted_as_a_string_list(self) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "invalid_prohibited_outcome",
        ):
            resolve_objective_intent(
                user_utterance="讨论目标",
                success_criteria=("目标可验证",),
                prohibited_outcomes="不得写入",
            )

    def test_consult_plan_strip_but_prepare_preserves_authority_requirements(
        self,
    ) -> None:
        requested_write = AuthorityRequest(
            requested=True,
            read_roots=("E:\\AyunOS",),
            write_roots=("E:\\AyunOS\\docs",),
            capabilities=("capability:write-file",),
            commands=("git commit",),
            risk_ceiling="medium",
            commit_permission=True,
            database_permission="write",
        )
        for agency in ("consult", "plan"):
            with self.subTest(agency=agency):
                contract = self.make_contract(
                    agency_level=agency,
                    maximum_authorized_agency_level=agency,
                    authority_request=requested_write,
                )
                request = contract.authority_request
                self.assertFalse(request.requested)
                self.assertFalse(request.requests_write)
                self.assertFalse(request.write_roots)
                self.assertFalse(request.commands)
                self.assertFalse(request.capabilities)
                self.assertEqual(request.database_permission, "none")
        prepared = self.make_contract(
            agency_level="prepare",
            maximum_authorized_agency_level="prepare",
            authority_request=requested_write,
        )
        self.assertTrue(prepared.authority_request.requested)
        self.assertTrue(prepared.authority_request.requests_write)
        self.assertFalse(prepared.authority_request.grants_authority)

    def test_direct_non_act_contract_rejects_write_authority(self) -> None:
        base = self.make_contract()
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "consult_plan_write_authority_request_denied",
        ):
            replace(
                base,
                authority_request=AuthorityRequest(
                    requested=True,
                    write_roots=("E:\\AyunOS",),
                ),
                canonical_digest="",
            )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "consult_plan_write_authority_request_denied",
        ):
            replace(
                base,
                authority_request=AuthorityRequest(
                    requested=True,
                    capabilities=("capability:write-file",),
                ),
                canonical_digest="",
            )

    def test_act_produces_only_an_authority_request(self) -> None:
        request = AuthorityRequest(
            requested=True,
            read_roots=("E:\\sandbox",),
            write_roots=("E:\\sandbox\\owned",),
            capabilities=("capability:write-file",),
            risk_ceiling="medium",
        )
        contract = self.make_contract(
            agency_level="act",
            maximum_authorized_agency_level="act",
            authority_request=request,
        )
        self.assertTrue(contract.authority_request.requested)
        self.assertTrue(contract.authority_request.requests_write)
        self.assertFalse(contract.authority_request.grants_authority)
        self.assertFalse(contract.grants_execution_authority)
        self.assertFalse(contract.execution_ready)

    def test_act_without_explicit_request_gets_request_marker_not_authority(self) -> None:
        contract = self.make_contract(
            agency_level="act",
            maximum_authorized_agency_level="act",
        )
        self.assertTrue(contract.authority_request.requested)
        self.assertFalse(contract.authority_request.grants_authority)
        self.assertFalse(contract.grants_execution_authority)

    def test_hard_ambiguity_suppresses_act_write_request(self) -> None:
        contract = self.make_contract(
            agency_level="act",
            maximum_authorized_agency_level="act",
            authority_request=AuthorityRequest(
                requested=True,
                write_roots=("E:\\sandbox\\owned",),
                capabilities=("capability:write-file",),
            ),
            ambiguity_signals=(
                AmbiguitySignal(
                    kind="new_write_scope",
                    detail="The requested write root has not been approved",
                ),
            ),
        )
        self.assertTrue(contract.clarification_required)
        self.assertFalse(contract.authority_request.requested)
        self.assertFalse(contract.authority_request.requests_write)
        self.assertFalse(contract.grants_execution_authority)

    def test_context_requirements_are_bounded_and_deterministic(self) -> None:
        defaults = default_context_requirements()
        self.assertLessEqual(sum(item.maximum_items for item in defaults), 64)
        self.assertLessEqual(
            sum(item.maximum_bytes for item in defaults),
            131_072,
        )
        first = self.make_contract(context_requirements=defaults)
        second = self.make_contract(
            context_requirements=tuple(reversed(defaults))
        )
        self.assertEqual(
            first.context_requirements,
            second.context_requirements,
        )
        self.assertEqual(first.canonical_digest, second.canonical_digest)

    def test_context_budget_overflow_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "context_item_budget_exceeded",
        ):
            self.make_contract(
                context_requirements=(
                    ContextRequirement(
                        source_system="project",
                        purpose="fixture:a",
                        maximum_items=40,
                        maximum_bytes=4096,
                        required=True,
                    ),
                    ContextRequirement(
                        source_system="v7_memory",
                        purpose="fixture:b",
                        maximum_items=40,
                        maximum_bytes=4096,
                        required=False,
                    ),
                )
            )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "invalid_context_maximum_bytes",
        ):
            ContextRequirement(
                source_system="project",
                purpose="fixture",
                maximum_items=1,
                maximum_bytes=255,
                required=True,
            )

    def test_acceptance_policy_keeps_verification_and_acceptance_separate(self) -> None:
        policy = AcceptancePolicy()
        self.assertTrue(policy.require_system_verification)
        self.assertTrue(policy.requires_user_event)
        self.assertFalse(policy.automatic_allowed)
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "explicit_acceptance_requires_user_event",
        ):
            AcceptancePolicy(
                mode="explicit_user",
                automatic_allowed=True,
                requires_user_event=False,
                deterministic_criteria=("test passes",),
            )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "unsafe_automatic_acceptance",
        ):
            AcceptancePolicy(
                mode="preauthorized_deterministic",
                automatic_allowed=True,
                requires_user_event=False,
                deterministic_criteria=("test passes",),
                maximum_risk="medium",
            )

    def test_safe_preauthorized_acceptance_is_contract_only(self) -> None:
        policy = AcceptancePolicy(
            mode="preauthorized_deterministic",
            automatic_allowed=True,
            requires_user_event=False,
            deterministic_criteria=("exact hash matches",),
            maximum_risk="low",
            preauthorization_reference="anchor:user:v10-objective",
            preauthorization_digest=digest("anchor:user:v10-objective"),
        )
        contract = self.make_contract(acceptance_policy=policy)
        self.assertTrue(contract.acceptance_policy.automatic_allowed)
        self.assertFalse(contract.grants_execution_authority)
        self.assertTrue(contract.acceptance_policy.require_reversible)
        self.assertTrue(contract.acceptance_policy.require_no_conflicts)
        self.assertTrue(contract.acceptance_policy.require_no_overreach)
        self.assertTrue(
            contract.acceptance_policy.require_no_unresolved_warnings
        )

    def test_automatic_acceptance_requires_independent_low_risk_anchor(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "automatic_acceptance_preauthorization_required",
        ):
            AcceptancePolicy(
                mode="preauthorized_deterministic",
                automatic_allowed=True,
                requires_user_event=False,
                deterministic_criteria=("exact hash matches",),
            )
        policy = AcceptancePolicy(
            mode="preauthorized_deterministic",
            automatic_allowed=True,
            requires_user_event=False,
            deterministic_criteria=("exact hash matches",),
            preauthorization_reference="anchor:user:v10-objective",
            preauthorization_digest=digest("anchor:user:v10-objective"),
        )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "automatic_acceptance_objective_risk_denied",
        ):
            self.make_contract(
                acceptance_policy=policy,
                risk_tolerance="critical",
            )

    def test_agency_selection_provenance_is_digest_bound(self) -> None:
        contract = self.make_contract()
        selection = contract.agency_selection
        self.assertEqual(selection.selected_by, "user")
        self.assertEqual(
            selection.source_anchor_id,
            "anchor:user:v10-objective",
        )
        self.assertEqual(
            selection.source_anchor_digest,
            digest("anchor:user:v10-objective"),
        )
        tampered = contract.to_dict()
        tampered["agency_selection"]["selection_digest"] = "0" * 64
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "agency_selection_digest_mismatch",
        ):
            ObjectiveContract.from_dict(tampered)

    def test_secret_literals_are_rejected_before_contract_output(self) -> None:
        synthetics = (
            "DEEPSEEK_API_KEY=SYNTHETIC_TEST_VALUE_123456",
            "token=1234567890abcdef",
            "secret=1234567890abcdef",
            '{"api_key":"1234567890abcdef"}',
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        )
        for synthetic in synthetics:
            for override in (
                {"user_utterance": synthetic},
                {"interpreted_goal": synthetic},
                {"assumptions": (synthetic,)},
                {"source_anchors": (self.make_anchor(locator=synthetic),)},
            ):
                with self.subTest(
                    synthetic=synthetic.split("=", 1)[0],
                    override=next(iter(override)),
                ):
                    with self.assertRaisesRegex(
                        ObjectiveIntentError,
                        "objective_secret_literal_forbidden",
                    ):
                        self.make_contract(**override)

    def test_from_dict_enforces_total_canonical_byte_budget(self) -> None:
        payload = self.make_contract().to_dict()
        payload["success_criteria"] = [
            f"criterion-{index:03d}-" + ("x" * 3000)
            for index in range(100)
        ]
        payload["canonical_digest"] = canonical_digest(
            {
                key: value
                for key, value in payload.items()
                if key != "canonical_digest"
            }
        )
        with self.assertRaisesRegex(
            ObjectiveIntentError,
            "objective_contract_byte_budget_exceeded",
        ):
            ObjectiveContract.from_dict(payload)

    def test_default_prohibited_outcomes_are_always_present(self) -> None:
        contract = self.make_contract(prohibited_outcomes=())
        self.assertTrue(
            set(DEFAULT_PROHIBITED_OUTCOMES).issubset(
                set(contract.prohibited_outcomes)
            )
        )

    def test_canonical_json_is_stable_and_contains_no_authority_artifact(self) -> None:
        contract = self.make_contract()
        first = contract.to_canonical_json()
        second = contract.to_canonical_json()
        self.assertEqual(first, second)
        envelope = json.loads(first)
        self.assertNotIn("authorization_artifact", envelope["document"])
        self.assertNotIn("tool_execution", envelope["document"])
        self.assertIn(contract.canonical_digest, first)

    def test_resolution_digest_is_order_and_nfc_stable(self) -> None:
        first = resolve_objective_intent(
            user_utterance="café",
            interpreted_goal="讨论 café",
            agency_level="consult",
            success_criteria=("criterion:b", "criterion:a"),
            prohibited_outcomes=("deny:b", "deny:a"),
            ambiguity_signals=(
                AmbiguitySignal(
                    kind="git_status_check",
                    detail="check café status",
                ),
            ),
        )
        second = resolve_objective_intent(
            user_utterance="cafe\u0301",
            interpreted_goal="讨论 cafe\u0301",
            agency_level="consult",
            success_criteria=("criterion:a", "criterion:b"),
            prohibited_outcomes=("deny:a", "deny:b"),
            ambiguity_signals=(
                AmbiguitySignal(
                    kind="git_status_check",
                    detail="check cafe\u0301 status",
                ),
            ),
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.resolution_digest, second.resolution_digest)

    def test_objective_keeps_locator_identity_and_canonicalizes_its_digest(
        self,
    ) -> None:
        legacy = self.make_anchor(locator="  locator:cafe\u0301  ")
        legacy_contract = self.make_contract(source_anchors=(legacy,))
        self.assertEqual(
            "  locator:cafe\u0301  ",
            legacy_contract.source_anchors[0].locator,
        )
        decomposed = self.make_anchor(locator="locator:cafe\u0301")
        composed = self.make_anchor(locator="locator:café")
        first = self.make_contract(source_anchors=(decomposed,))
        second = self.make_contract(source_anchors=(composed,))
        self.assertEqual(first.objective_id, second.objective_id)
        self.assertEqual(first.canonical_digest, second.canonical_digest)
        self.assertEqual(first.to_canonical_json(), second.to_canonical_json())


if __name__ == "__main__":
    unittest.main()
