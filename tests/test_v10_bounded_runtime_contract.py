from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import gc
import unittest

from memory.graph_enhanced_retrieval import (
    RetrievalQuery,
    RetrievalValidationError,
)
from memory.production_delivery import production_profile_binding
from memory.production_runtime_execution import (
    ProductionRuntimeExecutionError,
    _normalize_question as normalize_execution_question,
)
from memory.production_runtime_review import (
    ProductionRuntimeReviewError,
    _normalize_question as normalize_review_question,
)
from memory.runtime_protocol_contracts import (
    BOUNDED_TOOL_APPENDIX_MARKER,
    BOUNDED_TOOL_ARTIFACT_VERSION,
    BOUNDED_TOOL_REVIEWED_EXECUTION_BINDING_VERSION,
    BoundedToolRuntimeRequest,
    RuntimeProtocolContractError,
    _canonical_question,
    bounded_tool_exact_response_from_prepared_request,
    build_bounded_tool_runtime_artifact,
    build_reviewed_execution_binding,
    canonical_json,
    create_bounded_tool_runtime_request,
    prepare_runtime_request,
    validate_bounded_tool_runtime_request,
    validate_reviewed_execution_binding,
    validate_runtime_artifact_binding,
)
from memory.runtime_thin_slice import (
    RuntimeThinSliceError,
    _normalize_question as normalize_thin_slice_question,
)
from tests import test_production_delivery as production_delivery_tests


def bounded_appendix() -> dict:
    return {
        "control": {
            "context_is_untrusted_data": True,
            "model_grants_no_authority": True,
            "model_executes_no_tool": True,
            "return_exact_json_only": True,
        },
        "exact_response": {
            "output_kind": "tool_proposal",
            "payload": {"proposal_only": True},
        },
        "evidence_data_only": {
            "v7_memory": {"digest": "1" * 64},
            "v9_now": {"digest": "2" * 64},
            "v9_resume": {"digest": "3" * 64},
        },
    }


def bounded_request(question: str) -> BoundedToolRuntimeRequest:
    return create_bounded_tool_runtime_request(
        retrieval_question=question,
        appendix=bounded_appendix(),
        cognitive_request_digest="a" * 64,
        plan_node_digest="b" * 64,
        capability_digest="c" * 64,
        context_bindings_digest="d" * 64,
    )


class GenericQuestionBoundaryTest(unittest.TestCase):
    def test_all_generic_layers_keep_the_same_500_character_cap(self) -> None:
        accepted = "q" * 500
        self.assertEqual(accepted, RetrievalQuery(accepted).normalized().query)
        self.assertEqual(accepted, normalize_execution_question(accepted))
        self.assertEqual(accepted, normalize_review_question(accepted))
        self.assertEqual(accepted, normalize_thin_slice_question(accepted))
        self.assertEqual(accepted, _canonical_question(accepted))

    def test_arbitrary_long_ycd3_string_never_activates_a_wider_path(self) -> None:
        impersonator = "YCD3|" + "x" * 4_096
        checks = (
            (RetrievalValidationError, lambda: RetrievalQuery(impersonator).normalized()),
            (ProductionRuntimeExecutionError, lambda: normalize_execution_question(impersonator)),
            (ProductionRuntimeReviewError, lambda: normalize_review_question(impersonator)),
            (RuntimeThinSliceError, lambda: normalize_thin_slice_question(impersonator)),
            (RuntimeProtocolContractError, lambda: _canonical_question(impersonator)),
        )
        for expected, call in checks:
            with self.subTest(layer=expected.__name__), self.assertRaises(expected):
                call()


class BoundedToolRequestFactoryTest(unittest.TestCase):
    def test_factory_issues_exact_type_and_equal_live_requests_stay_independent(self) -> None:
        first = bounded_request("YCD2|bounded request")
        second = bounded_request("YCD2|bounded request")
        self.assertIs(first, validate_bounded_tool_runtime_request(first))
        self.assertIs(second, validate_bounded_tool_runtime_request(second))
        del first
        gc.collect()
        self.assertIs(second, validate_bounded_tool_runtime_request(second))

    def test_mapping_duck_type_direct_constructor_and_replace_are_rejected(self) -> None:
        issued = bounded_request("YCD2|sealed request")

        class DuckRequest:
            retrieval_question = issued.retrieval_question
            appendix_json = issued.appendix_json
            request_identity = issued.request_identity

        with self.assertRaisesRegex(
            RuntimeProtocolContractError,
            "issued_bounded_tool_request_required",
        ):
            validate_bounded_tool_runtime_request(issued.audit_binding())
        with self.assertRaisesRegex(
            RuntimeProtocolContractError,
            "issued_bounded_tool_request_required",
        ):
            validate_bounded_tool_runtime_request(DuckRequest())
        with self.assertRaisesRegex(
            RuntimeProtocolContractError,
            "bounded_tool_request_factory_required",
        ):
            BoundedToolRuntimeRequest(
                retrieval_question=issued.retrieval_question,
                appendix_json=issued.appendix_json,
                cognitive_request_digest=issued.cognitive_request_digest,
                plan_node_digest=issued.plan_node_digest,
                capability_digest=issued.capability_digest,
                context_bindings_digest=issued.context_bindings_digest,
            )
        with self.assertRaisesRegex(
            RuntimeProtocolContractError,
            "bounded_tool_request_factory_required",
        ):
            replace(issued)


class BoundedToolArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.delivery_fixture = production_delivery_tests.ProductionDeliveryTest(
            "test_delivery_assembly_and_runtime_bindings_cover_exact_contents"
        )
        self.delivery_fixture.setUp()

    def tearDown(self) -> None:
        self.delivery_fixture.tearDown()

    def test_appendix_is_additive_wire_data_bound_to_review_and_prepared_request(self) -> None:
        delivery = self.delivery_fixture.build()
        original = deepcopy(delivery)
        request = bounded_request(delivery["controls"]["query"])
        artifact = build_bounded_tool_runtime_artifact(
            delivery,
            request,
            approved_profile_binding=production_profile_binding(delivery),
        )
        validate_runtime_artifact_binding(artifact, delivery)
        prepared = prepare_runtime_request(artifact)
        reviewed = build_reviewed_execution_binding(
            delivery,
            artifact,
            request_id="request:v10-f-bounded-artifact",
            mutation_id="mutation:v10-f-bounded-artifact",
        )

        self.assertEqual(original, delivery)
        self.assertEqual(BOUNDED_TOOL_ARTIFACT_VERSION, artifact["artifact_version"])
        self.assertNotIn(BOUNDED_TOOL_APPENDIX_MARKER, request.retrieval_question)
        self.assertEqual(
            delivery["model_context"]
            + BOUNDED_TOOL_APPENDIX_MARKER
            + request.appendix_json,
            artifact["payload"]["messages"][1]["content"],
        )
        self.assertEqual(
            canonical_json(bounded_appendix()["exact_response"]),
            bounded_tool_exact_response_from_prepared_request(prepared),
        )
        self.assertEqual(
            BOUNDED_TOOL_REVIEWED_EXECUTION_BINDING_VERSION,
            reviewed["version"],
        )
        self.assertEqual(reviewed, validate_reviewed_execution_binding(reviewed))

    def test_artifact_binding_tamper_fails_closed(self) -> None:
        delivery = self.delivery_fixture.build()
        request = bounded_request(delivery["controls"]["query"])
        artifact = build_bounded_tool_runtime_artifact(
            delivery,
            request,
            approved_profile_binding=production_profile_binding(delivery),
        )
        tampered = deepcopy(artifact)
        tampered["bounded_tool_binding"]["capability_digest"] = "e" * 64
        with self.assertRaises(RuntimeProtocolContractError):
            prepare_runtime_request(tampered)


if __name__ == "__main__":
    unittest.main()
