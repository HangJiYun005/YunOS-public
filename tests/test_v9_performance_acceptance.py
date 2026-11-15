from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.test_v9_production_adoption import AUTHORITY_DIGEST, T0, evidence
from tools.v9_performance_acceptance import measure
from tools.v9_production_adoption import apply_adoption


class V9PerformanceAcceptanceTest(unittest.TestCase):
    def test_measurement_uses_clone_and_passes_realistic_gates(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v9-performance-acceptance-test-"
        ) as name:
            database = Path(name) / "operational.db"
            apply_adoption(
                database,
                evidence=evidence(),
                authority_evidence_digest=AUTHORITY_DIGEST,
                created_at=T0,
                production=False,
            )
            report = measure(database)
            self.assertTrue(report["passed"])
            self.assertTrue(report["production_source_unchanged"])
            self.assertTrue(report["temporary_clone_only_for_writes"])
            self.assertTrue(all(report["gates"].values()))


if __name__ == "__main__":
    unittest.main()
