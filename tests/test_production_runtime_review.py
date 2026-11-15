from __future__ import annotations

from http import HTTPStatus
import inspect
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from memory.production_delivery import production_profile_binding
from memory.production_runtime_execution import ProductionRuntimeExecutionService
from memory.production_runtime_review import (
    ProductionRuntimeReviewError,
    ProductionRuntimeReviewService,
)
from memory.runtime_protocol_contracts import (
    REVIEWED_ARTIFACT_VERSION,
    REVIEWED_EXECUTION_BINDING_VERSION,
)
from tests import test_production_delivery as production_delivery_tests
from tests.test_production_delivery import QUESTION
from tools.local_web import App, AyunOSHandler, REAL_DB_PATH


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "gate-f-first-deepseek-macbook-request"
MUTATION_ID = "gate-f-first-deepseek-macbook-mutation"


def ready_zero_status() -> dict:
    return {
        "status": "ready",
        "ready": True,
        "readonly": True,
        "runtime_state": "initialized_zero_run",
        "counts": {
            "conversations": 0,
            "request_artifacts": 0,
            "model_runs": 0,
            "messages": 0,
            "events": 0,
            "errors": 0,
            "receipts": 0,
        },
        "execution": {
            "credential_read": False,
            "sent_to_model": False,
            "external_model_calls": False,
        },
    }


class ProductionRuntimeReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = production_delivery_tests.ProductionDeliveryTest(
            "test_delivery_assembly_and_runtime_bindings_cover_exact_contents"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def builder(self, _query, *, budget_profile: str):
        self.assertEqual("Large", budget_profile)
        delivery = self.fixture.build()
        return delivery, production_profile_binding(delivery)

    def test_review_preview_matches_released_execution_preview_byte_for_byte(self):
        status = ready_zero_status()
        with (
            patch("memory.production_runtime_review._production_delivery", self.builder),
            patch("memory.production_runtime_review.production_runtime_status", return_value=status),
            patch("memory.production_runtime_execution._production_delivery", self.builder),
            patch("memory.production_runtime_execution.production_runtime_status", return_value=status),
        ):
            reviewed = ProductionRuntimeReviewService().preview(
                QUESTION,
                budget_profile="Large",
                request_id=REQUEST_ID,
                mutation_id=MUTATION_ID,
            )
            released = ProductionRuntimeExecutionService().preview(
                QUESTION,
                budget_profile="Large",
                request_id=REQUEST_ID,
                mutation_id=MUTATION_ID,
            )
        self.assertEqual(released, reviewed)
        self.assertEqual(REQUEST_ID, reviewed["request_id"])
        self.assertEqual(MUTATION_ID, reviewed["mutation_id"])
        self.assertEqual(
            REVIEWED_EXECUTION_BINDING_VERSION,
            reviewed["reviewed_execution_binding"]["version"],
        )
        self.assertEqual(
            REVIEWED_ARTIFACT_VERSION,
            reviewed["reviewed_execution_binding"]["reviewed_artifact"]["version"],
        )

    def test_review_repeats_exact_ids_without_random_or_timestamp_fallback(self):
        with (
            patch("memory.production_runtime_review._production_delivery", self.builder),
            patch(
                "memory.production_runtime_review.production_runtime_status",
                return_value=ready_zero_status(),
            ),
        ):
            first = ProductionRuntimeReviewService().preview(
                QUESTION,
                budget_profile="Large",
                request_id=REQUEST_ID,
                mutation_id=MUTATION_ID,
            )
            second = ProductionRuntimeReviewService().preview(
                QUESTION,
                budget_profile="Large",
                request_id=REQUEST_ID,
                mutation_id=MUTATION_ID,
            )
        self.assertEqual(first, second)
        self.assertEqual(REQUEST_ID, first["reviewed_execution_binding"]["request_id"])
        self.assertEqual(MUTATION_ID, first["reviewed_execution_binding"]["mutation_id"])
        with self.assertRaises(ProductionRuntimeReviewError):
            ProductionRuntimeReviewService().preview(
                f" {QUESTION}",
                budget_profile="Large",
                request_id=REQUEST_ID,
                mutation_id=MUTATION_ID,
            )

    def test_review_service_has_no_authority_execute_or_transport_surface(self):
        import memory.production_runtime_review as review_module

        service = ProductionRuntimeReviewService()
        source = inspect.getsource(review_module)
        self.assertFalse(hasattr(service, "execute"))
        self.assertFalse(hasattr(service, "get_run"))
        for forbidden in (
            "production_runtime_execution",
            "ProductionRuntimeExecutionSession",
            "DeepSeekV4FlashHTTPAdapter",
            "load_production_deepseek_api_key",
            "config_private",
            "urlopen",
            "Request(",
        ):
            self.assertNotIn(forbidden, source)

    def test_fresh_review_startup_and_build_load_no_execution_or_adapter_module(self):
        code = r'''
import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch
import tools.local_web as web

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
    fixed_source = Path(handle.name).resolve()
web.REAL_DB_PATH = fixed_source
args = argparse.Namespace(
    host="127.0.0.1", port=8765, db=str(web.REAL_DB_PATH), real=True,
    readonly=True, profile_admin=False, runtime_review=True,
    runtime_admin=False, allow_external_model_calls=False,
    confirm_deepseek_send=False, runtime_admin_mock=False,
    runtime_test_sidecar=None,
)
server = Mock()
server.server_address = ("127.0.0.1", 8765)
with patch("tools.local_web.parse_args", return_value=args), patch(
    "tools.local_web.ThreadingHTTPServer", return_value=server,
):
    result = web.main()
app = web.AyunOSHandler.app
startup_execution_loaded = "memory.production_runtime_execution" in sys.modules
startup_adapter_loaded = "memory.deepseek_v4_adapter" in sys.modules

from memory.production_delivery import production_profile_binding
from tests import test_production_delivery as delivery_tests
from tests.test_production_delivery import QUESTION

fixture = delivery_tests.ProductionDeliveryTest(
    "test_delivery_assembly_and_runtime_bindings_cover_exact_contents"
)
fixture.setUp()
# The shared delivery fixture imports older Runtime test helpers. Reset the two
# production transport/execution module markers immediately before the review
# build so this assertion measures only the review path itself.
sys.modules.pop("memory.production_runtime_execution", None)
sys.modules.pop("memory.deepseek_v4_adapter", None)
def builder(_query, *, budget_profile):
    assert budget_profile == "Large"
    delivery = fixture.build()
    return delivery, production_profile_binding(delivery)
with patch("memory.production_runtime_review._production_delivery", builder), patch(
    "memory.production_runtime_review.production_runtime_status",
    return_value={"ready": True, "readonly": True, "counts": {}},
):
    preview = app.runtime_execution_service.preview(
        QUESTION, budget_profile="Large",
        request_id="gate-f-first-deepseek-macbook-request",
        mutation_id="gate-f-first-deepseek-macbook-mutation",
    )
fixture.tearDown()
fixed_source.unlink(missing_ok=True)
print(json.dumps({
    "result": result,
    "runtime_review": app.runtime_review,
    "runtime_admin": app.runtime_admin,
    "readonly": app.readonly,
    "execution_authority_present": app.execution_authority_present,
    "can_execute": app.can_execute,
    "execution_session_present": app.runtime_execution_session is not None,
    "startup_execution_module_loaded": startup_execution_loaded,
    "startup_adapter_module_loaded": startup_adapter_loaded,
    "execution_module_loaded": "memory.production_runtime_execution" in sys.modules,
    "adapter_module_loaded": "memory.deepseek_v4_adapter" in sys.modules,
    "preview_request_id": preview["request_id"],
    "preview_mutation_id": preview["mutation_id"],
    "serve_forever_calls": server.serve_forever.call_count,
}, sort_keys=True))
'''
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual({
            "result": 0,
            "runtime_review": True,
            "runtime_admin": False,
            "readonly": True,
            "execution_authority_present": False,
            "can_execute": False,
            "execution_session_present": False,
            "startup_execution_module_loaded": False,
            "startup_adapter_module_loaded": False,
            "execution_module_loaded": False,
            "adapter_module_loaded": False,
            "preview_request_id": REQUEST_ID,
            "preview_mutation_id": MUTATION_ID,
            "serve_forever_calls": 1,
        }, data)

    def test_review_status_and_every_execute_post_fail_before_service_call(self):
        service = ProductionRuntimeReviewService()
        app = App(
            REAL_DB_PATH,
            real=True,
            readonly=True,
            runtime_execution_service=service,
            runtime_review=True,
        )
        handler = object.__new__(AyunOSHandler)
        handler.app = app
        handler.send_json = Mock()
        handler.send_error_json = Mock()

        with patch("tools.local_web.production_runtime_status", return_value=ready_zero_status()):
            handler.api_status()
        status = handler.send_json.call_args.args[0]
        self.assertEqual({
            "runtime_review": True,
            "runtime_admin": False,
            "readonly": True,
            "execution_authority_present": False,
            "can_execute": False,
        }, {key: status[key] for key in (
            "runtime_review", "runtime_admin", "readonly",
            "execution_authority_present", "can_execute",
        )})

        handler.send_json.reset_mock()
        handler.api_runtime_execute({"crafted": True})
        response, http_status = handler.send_json.call_args.args
        self.assertEqual(HTTPStatus.FORBIDDEN, http_status)
        self.assertEqual("runtime_review_execution_forbidden", response["reason_code"])

        handler.send_json.reset_mock()
        handler.path = "/api/runtime-execute"
        AyunOSHandler.do_POST(handler)
        response, http_status = handler.send_json.call_args.args
        self.assertEqual(HTTPStatus.FORBIDDEN, http_status)
        self.assertEqual("runtime_review_execution_forbidden", response["reason_code"])

    def test_ui_keeps_review_confirmation_unchecked_and_send_disabled(self):
        source = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for marker in (
            'requestId.id = "runtime-execution-request-id"',
            'mutationId.id = "runtime-execution-mutation-id"',
            "const requestId = callerRequestId || runtimeIdentifier(\"request\")",
            "if (state.runtimeReview && (!callerRequestId || !callerMutationId))",
            'checkbox.checked = false',
            'checkbox.disabled = true',
            'const send = button(t("common.send"',
            'send.disabled = true',
            '"runtime.sendUnavailable"',
            '"runtime.completeReviewedObject"',
            '"runtime.uncertaintySection"',
            '"runtime.estimatedInputTokens"',
            "state.runtimeReview\n    || !state.runtimeAdmin",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
