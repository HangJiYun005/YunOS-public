from __future__ import annotations

import inspect
from pathlib import Path
import unittest

from memory.operational_state_models import canonical_digest
from tools import v10_c_real_acceptance as acceptance


class V10CRealAcceptanceBoundaryTest(unittest.TestCase):
    def test_search_proof_is_fixed_to_exact_loopback_endpoint(self) -> None:
        self.assertEqual(
            acceptance.SEARCH_LOCAL_ENDPOINT,
            "http://127.0.0.1:8765",
        )
        self.assertEqual(
            tuple(inspect.signature(acceptance._search_zero_model_proof).parameters),
            (),
        )
        self.assertNotIn(
            "base_url",
            inspect.signature(acceptance.run).parameters,
        )

    def test_cli_has_no_search_endpoint_override(self) -> None:
        args = acceptance.parse_args([])
        self.assertFalse(hasattr(args, "base_url"))
        with self.assertRaises(SystemExit):
            acceptance.parse_args(
                ["--base-url", "https://remote.example"]
            )

    def test_dry_run_remains_non_creating(self) -> None:
        output = Path("output/v10-c-boundary-test-must-not-exist")
        self.assertFalse(output.exists())
        self.assertEqual(acceptance.main(["--output-dir", str(output)]), 0)
        self.assertFalse(output.exists())

    def test_execution_cannot_escape_the_canonical_one_shot_sentry(self) -> None:
        with self.assertRaisesRegex(
            acceptance.AcceptanceError,
            "acceptance_output_directory_must_be_canonical",
        ):
            acceptance.run(Path("output/v10-c-alternate"))
        self.assertTrue(
            (
                acceptance.CANONICAL_OUTPUT_DIR
                / "real-deepseek-one-shot-sentry.json"
            ).is_file()
        )

    def test_future_runner_binds_typed_consent_and_unknown_cost(self) -> None:
        self.assertEqual("授权，继续", acceptance.USER_CONSENT_UTTERANCE)
        self.assertEqual(
            acceptance.EXTERNAL_SEND_DISCLOSURE_DIGEST,
            canonical_digest(acceptance.EXTERNAL_SEND_DISCLOSURE),
        )
        self.assertEqual(
            {
                "automatic_retry_allowed": False,
                "provider_fallback_allowed": False,
                "project_write_allowed": False,
                "delegation_allowed": False,
                "state_update_allowed": False,
                "result_acceptance_allowed": False,
            },
            {
                key: acceptance.EXTERNAL_SEND_DISCLOSURE[key]
                for key in (
                    "automatic_retry_allowed",
                    "provider_fallback_allowed",
                    "project_write_allowed",
                    "delegation_allowed",
                    "state_update_allowed",
                    "result_acceptance_allowed",
                )
            },
        )
        source = inspect.getsource(acceptance.run)
        self.assertIn("prepare_disclosure", source)
        self.assertIn("create_external_send_consent", source)
        self.assertIn("**disclosure.consent_binding()", source)
        self.assertIn("disclosure.expires_at", source)
        self.assertIn("bridge.preview(\n        disclosure,", source)
        self.assertIn("consent=consent", source)
        self.assertIn("actual_cost_microunits=None", source)
        self.assertNotIn("actual_cost_microunits=0", source)
        self.assertIn('"status": "not_evaluated_in_runner"', source)
        self.assertIn("independent_post_call_value_scan", source)
        self.assertNotIn('"secret_material_persisted": False', source)


if __name__ == "__main__":
    unittest.main()
