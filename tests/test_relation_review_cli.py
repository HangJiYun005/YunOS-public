from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memory.relation_review import discover_relation_candidates, required_confirmation_fields
from memory.relation_review_state import LEDGER_NAME, _TestReviewAuthorityFactory
from tests.test_relation_candidate_review import _combined


class CliAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.authority = _TestReviewAuthorityFactory.initialize(self.root)
        self.discovery = discover_relation_candidates(_combined())

    def tearDown(self): self.tmp.cleanup()

    def run_cli(self, argv):
        bootstrap = (
            "import sys; from tools import review_memory_graph_candidates as c; "
            "from memory.relation_review import discover_relation_candidates; "
            "from tests.test_relation_candidate_review import _combined; "
            "from memory.relation_review_state import _TestReviewAuthorityFactory; "
            f"c.ProductionReviewAuthorityFactory.open=lambda: _TestReviewAuthorityFactory.open({str(self.root)!r}); "
            "c._discover=lambda _args: discover_relation_candidates(_combined()); "
            "raise SystemExit(c.main(sys.argv[1:]))"
        )
        result=subprocess.run([sys.executable, "-c", bootstrap, *argv], cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), capture_output=True, text=True, check=False)
        return result.returncode, json.loads(result.stdout)

    def test_prepare_decide_conflict_across_independent_opens(self):
        candidate=self.discovery.candidates[0]
        code, prepared=self.run_cli(["prepare", "--candidate-id", candidate.candidate_id, "--json"])
        self.assertEqual(0, code)
        plan_path=self.root / "plan.json"; plan_path.write_text(json.dumps(prepared["review_plan"]), encoding="utf-8")
        from memory.relation_review import ReviewPlan
        value=prepared["review_plan"]; value["allowed_decisions"]=tuple(value["allowed_decisions"]); plan=ReviewPlan(**value)
        confirmation=self.root / "confirmation.json"; confirmation.write_text(json.dumps(required_confirmation_fields(plan, "accept")), encoding="utf-8")
        code, accepted=self.run_cli(["decide", "--plan-file", str(plan_path), "--confirmation-file", str(confirmation), "--decision", "accept", "--reason-code", "reviewed", "--reviewer-identity", "reviewer", "--json"])
        self.assertEqual((0, "accepted_pending_persistence"), (code, accepted["status"]))
        confirmation.write_text(json.dumps(required_confirmation_fields(plan, "reject")), encoding="utf-8")
        code, conflict=self.run_cli(["decide", "--plan-file", str(plan_path), "--confirmation-file", str(confirmation), "--decision", "reject", "--reason-code", "reviewed", "--reviewer-identity", "reviewer", "--json"])
        self.assertEqual((0, "decision_conflict"), (code, conflict["status"]))
        self.assertNotIn(str(self.root).lower(), json.dumps(conflict).lower())

    def test_deleted_ledger_fails_closed_without_path_leak(self):
        (self.root / LEDGER_NAME).unlink()
        code, result=self.run_cli(["prepare", "--candidate-id", self.discovery.candidates[0].candidate_id, "--json"])
        self.assertEqual((2, "authority_state_lost"), (code, result["status"]))
        self.assertNotIn(str(self.root).lower(), json.dumps(result).lower())

    def test_list_does_not_open_authority_and_limit_is_bounded(self):
        code, result=self.run_cli(["list", "--limit", "20", "--json"])
        self.assertEqual((0, "listed"), (code, result["status"]))
        code, result=self.run_cli(["list", "--limit", "21", "--json"])
        self.assertEqual((2, "refused"), (code, result["status"]))

    def test_production_cli_ignores_legacy_test_authority_environment(self):
        bootstrap = (
            "import sys; from tools import review_memory_graph_candidates as c; "
            "from memory.relation_review import discover_relation_candidates; "
            "from tests.test_relation_candidate_review import _combined; "
            "c._discover=lambda _args: discover_relation_candidates(_combined()); "
            "raise SystemExit(c.main(sys.argv[1:]))"
        )
        env=os.environ.copy(); env.update({"YUNOS_RELATION_REVIEW_TEST_MODE":"1", "YUNOS_RELATION_REVIEW_TEST_ROOT":str(self.root)})
        candidate=self.discovery.candidates[0]
        result=subprocess.run([sys.executable,"-c",bootstrap,"prepare","--candidate-id",candidate.candidate_id,"--json"],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,check=False)
        payload=json.loads(result.stdout)
        self.assertEqual((2,"review_authority_not_initialized"),(result.returncode,payload["status"]))


if __name__ == "__main__": unittest.main()
