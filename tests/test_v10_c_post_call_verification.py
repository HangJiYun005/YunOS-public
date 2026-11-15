from __future__ import annotations

import base64
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from memory.cognitive_external_send import (
    ExternalSendConsent,
    ExternalSendConsentError,
)
from tools import v10_c_post_call_verification as verification


class V10CPostCallVerificationContractTest(unittest.TestCase):
    def test_scanner_detects_raw_url_and_base64_secret_encodings(self) -> None:
        secret = "synthetic-test-credential-0123456789"
        patterns = verification._secret_variants(secret)
        samples = (
            secret.encode("utf-8"),
            base64.b64encode(secret.encode("utf-8")),
            secret.encode("utf-16-le"),
        )
        with TemporaryDirectory() as temporary:
            for index, sample in enumerate(samples):
                path = Path(temporary) / f"sample-{index}.bin"
                path.write_bytes(b"prefix:" + sample + b":suffix")
                with self.subTest(index=index):
                    self.assertTrue(
                        verification._stream_contains(path, patterns)
                    )
            clean = Path(temporary) / "clean.bin"
            clean.write_bytes(b"no credential material here")
            self.assertFalse(verification._stream_contains(clean, patterns))

    def test_verifier_has_no_provider_dispatch_primitive(self) -> None:
        source = inspect.getsource(verification)
        for forbidden in (
            "open_production_runtime_execution",
            "run_production_deepseek_readiness",
            "ProductionCognitiveRuntimeBridge",
            "bridge.execute(",
            "urlopen(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("_search_zero_model_proof", source)
        self.assertIn("external_provider_calls", source)

    def test_supplement_is_exclusive_and_honestly_describes_history(self) -> None:
        source = inspect.getsource(verification.run)
        self.assertIn('SUPPLEMENT_PATH.open("x"', source)
        self.assertIn("Original Runtime request did not bind", source)
        self.assertIn("Original settlement API was called with zero", source)
        self.assertIn("does not rerun or alter the Provider call", source)

    def test_external_send_consent_cannot_be_forged_by_direct_constructor(self) -> None:
        with self.assertRaisesRegex(
            ExternalSendConsentError,
            "external_send_consent_factory_required",
        ):
            ExternalSendConsent(
                consent_id="consent:test:direct-forgery",
                user_utterance="Authorize a send.",
                disclosure_digest="1" * 64,
                objective_id="objective:test:direct-forgery",
                objective_digest="2" * 64,
                context_digest="3" * 64,
                provider_selection_digest="4" * 64,
                health_attestation_digest="5" * 64,
                request_digest="6" * 64,
                reservation_id="reservation:test:direct-forgery",
                reservation_digest="7" * 64,
                runtime_disclosure_digest="8" * 64,
                runtime_disclosure_prepared_at="2026-08-01T00:00:00Z",
                runtime_disclosure_expires_at="2026-08-01T00:10:00Z",
                runtime_request_id="request:test:direct-forgery",
                runtime_mutation_id="mutation:test:direct-forgery",
                runtime_budget_profile="Small",
                runtime_question_sha256="9" * 64,
                runtime_reviewed_binding_digest="a" * 64,
                runtime_artifact_identity="artifact:test:direct-forgery",
                runtime_model_run_id="modelrun:test:direct-forgery",
                provider_id="provider:test",
                model="test-model",
                purpose="discussion",
                granted_at="2026-08-01T00:00:00Z",
                expires_at="2026-08-01T00:10:00Z",
            )


if __name__ == "__main__":
    unittest.main()
