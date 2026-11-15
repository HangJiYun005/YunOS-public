from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import whale_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "whale_legacy_prompt_golden.json"


class WhaleLegacyGoldenTest(unittest.TestCase):
    def test_default_legacy_prompt_matches_fixed_golden_fixture(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with patch.object(whale_chat, "load_persona", return_value=fixture["persona"]), patch.object(
            whale_chat, "load_whale_summary_rows", return_value=fixture["summary_rows"]
        ), patch.object(whale_chat, "retrieve_prompt_memory", return_value=fixture["raw_rows"]):
            report = whale_chat.build_whale_prompt_context(
                fixture["user_input"],
                db_path="fixture-source.db",
                include_general=fixture["include_general"],
            )

        actual = {
            "sections": report["sections"],
            "totals": report["totals"],
            "ordering": report["ordering"],
            "prompt": report["prompt"],
            "retrieval_mode_requested": report["retrieval_mode_requested"],
            "retrieval_mode_effective": report["retrieval_mode_effective"],
            "active_profile": report.get("active_profile"),
            "active_profile_source": report.get("active_profile_source"),
        }
        expected = fixture["expected"]
        self.assertEqual(expected["sections"], actual["sections"])
        self.assertEqual(expected["totals"], actual["totals"])
        self.assertEqual(expected["ordering"], actual["ordering"])
        self.assertEqual(expected["prompt"], actual["prompt"])
        self.assertEqual(expected["retrieval_mode_requested"], actual["retrieval_mode_requested"])
        self.assertEqual(expected["retrieval_mode_effective"], actual["retrieval_mode_effective"])
        self.assertEqual(fixture["active_profile"], actual["active_profile"])
        self.assertEqual(fixture["active_profile_source"], actual["active_profile_source"])
        self.assertEqual(
            ["System", "High-level Memory Summaries - whale", "Relevant Memory - whale", "User Input"],
            [section["name"] for section in actual["sections"] if section["enabled"]],
        )

    def test_fixture_contains_no_runtime_or_model_dependency(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual("legacy", fixture["retrieval_mode"])
        self.assertIsNone(fixture["profile"])


if __name__ == "__main__":
    unittest.main()
