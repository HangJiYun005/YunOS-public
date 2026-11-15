from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http import HTTPStatus
import io
import inspect
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.context_assembly import (
    ASSEMBLY_IDENTITY_VERSION,
    PROTOCOL_VERSION,
    SELECTION_POLICY_VERSION,
    ContextAssemblyService,
    ContextAssemblyValidationError,
    _ContextAssemblyCore,
    _matched_topics,
    _project_warnings,
    _topic_terms,
    canonical_context_assembly_json,
    make_approved_profile_item,
    normalize_assembly_policy,
    redact_context_text,
    render_context_assembly,
    validate_context_assembly_source_backed,
    validate_context_assembly_standalone,
)
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import (
    ABSOLUTE_PATH_REPLACEMENT,
    MemoryPackageService,
    _reject_absolute_paths,
)
from tools.context_assembly_preview import main as preview_main, parse_args
from tools.local_web import AyunOSHandler, REAL_DB_PATH
from tools.migrate_semantic_memory import CREATE_MEMORY_EMBEDDINGS_SQL


class ContextAssemblyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.semantic = self.root / "semantic.db"
        self.knowledge = self.root / "knowledge.db"
        with sqlite3.connect(self.source) as conn:
            conn.executescript("""
                CREATE TABLE conversations(
                    conversation_id TEXT PRIMARY KEY, title TEXT, create_time REAL,
                    create_time_iso TEXT, update_time REAL, update_time_iso TEXT
                );
                CREATE TABLE messages(
                    message_id TEXT, conversation_id TEXT, conversation_title TEXT,
                    ordinal INTEGER, role TEXT, create_time REAL, create_time_iso TEXT,
                    text TEXT, memory_space TEXT
                );
            """)
            conversations = [
                ("c1", "MacBook purchase", 1.0, "2025-01-01T00:00:00+00:00", 1.0, "2025-01-01T00:00:00+00:00"),
                ("c2", "MacBook purchase history", 2.0, "2025-02-01T00:00:00+00:00", 2.0, "2025-02-01T00:00:00+00:00"),
                ("c3", "Game preferences", 3.0, "2025-03-01T00:00:00+00:00", 3.0, "2025-03-01T00:00:00+00:00"),
                ("c4", "Novel project", 4.0, "2025-04-01T00:00:00+00:00", 4.0, "2025-04-01T00:00:00+00:00"),
                ("c5", "Family notes", 5.0, "2025-05-01T00:00:00+00:00", 5.0, "2025-05-01T00:00:00+00:00"),
            ]
            messages = [
                ("m1", "c1", "MacBook purchase", 1, "user", 1735689600.0, "2025-01-01T00:00:00+00:00", "Preference: For MacBook I prefer Pro.", "general"),
                ("m2", "c2", "MacBook purchase history", 1, "user", 1738368000.0, "2025-02-01T00:00:00+00:00", "Preference: Earlier for MacBook I considered Air.", "general"),
                ("m3", "c3", "Game preferences", 1, "user", 1740787200.0, "2025-03-01T00:00:00+00:00", "Preference: I like strategy games.", "general"),
                ("m4", "c4", "Novel project", 1, "user", 1743465600.0, "2025-04-01T00:00:00+00:00", "Goal: finish the historical novel.", "general"),
                ("m5", "c5", "Family notes", 1, "user", 1746057600.0, "2025-05-01T00:00:00+00:00", "Fact: a family event is scheduled.", "general"),
            ]
            conn.executemany("INSERT INTO conversations VALUES(?,?,?,?,?,?)", conversations)
            conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", messages)
            conn.commit()
        with sqlite3.connect(self.semantic) as conn:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
            conn.commit()

        def semantic_search(*args, **kwargs):
            return {"results": [
                {
                    "rank": index,
                    "score": 0.95 - index / 100,
                    "source_id": f"s{index}",
                    "source_table": "memory_summaries",
                    "memory_space": "general",
                    "content_preview": preview,
                    "summary_type": "conversation_summary",
                    "conversation_id": conversation_id,
                }
                for index, (conversation_id, preview) in enumerate((
                    ("c3", "Preference: I like strategy games."),
                    ("c4", "Goal: finish the historical novel."),
                    ("c5", "Fact: a family event is scheduled."),
                ), start=1)
            ]}

        retriever = GraphEnhancedRetrievalService(
            self.source,
            self.semantic,
            self.knowledge,
            semantic_provider="deterministic_test",
            semantic_searcher=semantic_search,
        )
        self.memory_service = MemoryPackageService(self.source, retriever=retriever)
        self.core = _ContextAssemblyCore(
            self.source, memory_package_service=self.memory_service,
        )
        self.query = RetrievalQuery("MacBook", hit_types=("keyword", "semantic"), limit=20)
        self.approved = make_approved_profile_item(
            "Keep the MacBook budget below 2000.",
            kind="constraint",
            topics=["MacBook"],
            approval_id="approval-macbook-budget",
            approved_at="2025-01-01T00:00:00Z",
        )
        self.unrelated_approved = make_approved_profile_item(
            "A family event is important.",
            kind="fact",
            topics=["family"],
            approval_id="approval-family",
            approved_at="2025-01-01T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, **kwargs):
        return self.core.build(
            self.query,
            budget_profile=kwargs.pop("budget_profile", "Large"),
            approved_profile=kwargs.pop("approved_profile", [self.approved, self.unrelated_approved]),
            **kwargs,
        )

    def test_macbook_task_assembly_and_unrelated_memories_are_excluded(self):
        package = self.build()
        context = package["model_context"]
        self.assertIn("MacBook budget below 2000", context)
        self.assertIn("MacBook I prefer Pro", context)
        self.assertIn("MacBook I considered Air", context)
        self.assertIn("candidate_relation/evolution", context)
        self.assertNotIn("strategy games", context)
        self.assertNotIn("historical novel", context)
        self.assertNotIn("family event", context)
        self.assertGreaterEqual(package["counts"]["omission_reasons"]["irrelevant_to_task"], 4)
        self.assertEqual(
            "citation_and_anchor_only",
            package["historical_evidence"]["default_representation"],
        )

    def test_empty_degraded_source_state_is_propagated_without_promotion(self):
        missing_semantic = self.root / "missing-semantic.db"
        missing_knowledge = self.root / "missing-knowledge.db"
        retriever = GraphEnhancedRetrievalService(
            self.source,
            missing_semantic,
            missing_knowledge,
        )
        core = _ContextAssemblyCore(
            self.source,
            memory_package_service=MemoryPackageService(self.source, retriever=retriever),
        )
        package = core.build(
            RetrievalQuery(
                "No matching evidence",
                hit_types=("keyword", "semantic", "graph"),
                limit=20,
            ),
            budget_profile="Small",
            approved_profile=(),
        )
        current = package["task_context"]["current_retrieval_results"]
        source = package["source_chain"]["evidence_selection"]
        self.assertEqual(("partial", "degraded_source_context"), (
            package["status"], package["reason_code"],
        ))
        self.assertEqual("degraded", current["degradation"]["status"])
        self.assertTrue(current["missing_capabilities"])
        self.assertTrue(current["warnings"])
        for field in ("capabilities", "degradation", "missing_capabilities", "warnings"):
            self.assertEqual(source[field], current[field])
        self.assertIn("source_warnings:", package["model_context"])
        self.assertFalse(missing_semantic.exists())
        self.assertFalse(missing_knowledge.exists())

    def test_relevance_uses_exact_latin_tokens_and_conservative_han_rules(self):
        package = self.core.build(
            RetrievalQuery("Pro", hit_types=("keyword", "semantic"), limit=20),
            budget_profile="Large",
            approved_profile=(),
        )
        self.assertIn("MacBook I prefer Pro", package["model_context"])
        self.assertNotIn("Novel project", canonical_context_assembly_json(package))
        self.assertNotIn("historical novel", canonical_context_assembly_json(package))
        self.assertEqual(["pro"], _matched_topics(_topic_terms("Pro", allow_fallback=True), "MacBook Pro"))
        self.assertEqual([], _matched_topics(_topic_terms("Pro", allow_fallback=True), "Novel project"))
        self.assertEqual([], _matched_topics(_topic_terms("苹果", allow_fallback=True), "苹果电脑"))
        self.assertEqual(["苹果"], _matched_topics(_topic_terms("苹果", allow_fallback=True), "苹果"))
        self.assertEqual(["苹果电脑"], _matched_topics(_topic_terms("苹果电脑", allow_fallback=True), "我偏好苹果电脑型号"))

    def test_v70_path_boundary_covers_all_context_surfaces(self):
        paths = (
            "/opt/private/macbook.txt",
            "/mnt/private/macbook.txt",
            r"C:\Users\ExampleUser\private\macbook.txt",
            r"\\server\share\private\macbook.txt",
            "file:///home/yun/private/macbook.txt",
        )
        approved = [
            make_approved_profile_item(
                f"MacBook source {path}.",
                kind="preference",
                topics=["MacBook"],
                approval_id=f"path-{index}",
                approved_at="2025-01-01T00:00:00Z",
            )
            for index, path in enumerate(paths)
        ]
        for item in approved:
            self.assertIn(ABSOLUTE_PATH_REPLACEMENT, item["statement"])
            _reject_absolute_paths(item)
        projected_warnings = _project_warnings([
            {"code": f"path_{index}", "message": f"unavailable at {path}."}
            for index, path in enumerate(paths)
        ])
        for warning in projected_warnings:
            self.assertIn(ABSOLUTE_PATH_REPLACEMENT, warning["message"])
            _reject_absolute_paths(warning)

        with sqlite3.connect(self.source) as conn:
            for index, path in enumerate(paths, start=6):
                conversation_id = f"c{index}"
                message_id = f"m{index}"
                conn.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                    (conversation_id, "MacBook Pro paths", float(index), "2025-06-01T00:00:00+00:00", float(index), "2025-06-01T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (message_id, conversation_id, "MacBook Pro paths", 1, "user", 1748736000.0 + index, "2025-06-01T00:00:00+00:00", f"Preference: For MacBook Pro I prefer option {index} at {path}.", "general"),
                )
            conn.commit()
        package = self.core.build(
            RetrievalQuery("MacBook Pro", hit_types=("keyword", "semantic"), limit=20),
            budget_profile="Custom",
            total_characters=50_000,
            estimated_tokens=12_500,
            max_evidence=100,
            max_candidates=100,
            max_history_expansions=20,
            approved_profile=approved,
        )
        json_preview = canonical_context_assembly_json(package)
        text_preview = render_context_assembly(package)
        self.assertIn(ABSOLUTE_PATH_REPLACEMENT, json_preview)
        self.assertGreaterEqual(sum(
            ABSOLUTE_PATH_REPLACEMENT in item["statement"]
            for item in package["candidate_memory"]["items"]
        ), len(paths))
        self.assertGreaterEqual(sum(
            ABSOLUTE_PATH_REPLACEMENT in (item["expanded_excerpt"] or "")
            for item in package["historical_evidence"]["items"]
        ), len(paths))
        for path in paths:
            self.assertNotIn(path, json_preview)
            self.assertNotIn(path, package["model_context"])
            self.assertNotIn(path, text_preview)
            self.assertIn(ABSOLUTE_PATH_REPLACEMENT, redact_context_text(path))
        _reject_absolute_paths(package)

    def test_candidate_never_masquerades_as_fact_or_approved_profile(self):
        package = self.build()
        approved_ids = set(package["transparency"]["model_saw"]["approved_profile_ids"])
        for candidate in package["candidate_memory"]["items"]:
            self.assertEqual("candidate", candidate["state"])
            self.assertEqual("not_approved", candidate["approval_status"])
            self.assertNotIn(candidate["candidate_id"], approved_ids)
        self.assertTrue(all(
            item["approval"]["status"] == "approved"
            and item["profile_item_id"].startswith("upi_")
            for item in package["confirmed_user_profile"]["items"]
        ))
        forged = deepcopy(self.approved)
        forged["approval"]["status"] = "candidate"
        with self.assertRaisesRegex(ContextAssemblyValidationError, "only user-approved"):
            self.build(approved_profile=[forged])

    def test_budget_boundaries_are_hard_and_omissions_are_semantic_whole_items(self):
        package = self.build(
            budget_profile="Custom",
            total_characters=900,
            estimated_tokens=225,
            max_evidence=10,
            max_candidates=10,
            max_history_expansions=5,
        )
        self.assertLessEqual(package["budget"]["used_characters"], 900)
        self.assertLessEqual(package["budget"]["estimated_tokens_used"], 225)
        self.assertTrue(package["budget"]["character_hard_limit_satisfied"])
        reasons = {item["reason"] for item in package["omitted"]}
        self.assertTrue({"character_budget", "estimated_token_budget"} & reasons)
        for item in package["candidate_memory"]["items"]:
            source_candidate = next(
                candidate for candidate in self.build()["candidate_memory"]["items"]
                if candidate["candidate_id"] == item["candidate_id"]
            )
            self.assertEqual(source_candidate["statement"], item["statement"])
        limited = self.build(
            budget_profile="Custom",
            total_characters=5_000,
            estimated_tokens=1_250,
            max_evidence=1,
            max_candidates=1,
            max_history_expansions=0,
        )
        self.assertEqual(1, limited["counts"]["candidates_used"])
        self.assertEqual(1, limited["counts"]["evidence_used"])
        self.assertGreater(limited["counts"]["omission_reasons"]["candidate_count_limit"], 0)
        self.assertGreater(limited["counts"]["omission_reasons"]["evidence_count_limit"], 0)

    def test_json_text_consistency_and_stable_identities(self):
        first = self.build()
        second = self.build()
        self.assertEqual(PROTOCOL_VERSION, first["protocol_version"])
        self.assertEqual(ASSEMBLY_IDENTITY_VERSION, first["assembly_identity_version"])
        self.assertEqual(SELECTION_POLICY_VERSION, first["selection_policy"]["policy_version"])
        self.assertEqual(first["assembly_identity"], second["assembly_identity"])
        self.assertEqual(first["selection_policy_identity"], second["selection_policy_identity"])
        self.assertEqual(first, json.loads(canonical_context_assembly_json(first)))
        text = render_context_assembly(first)
        self.assertIn(first["assembly_identity"], text)
        self.assertIn(first["model_context"], text)
        self.assertIn("本次回答模型看到了什么", first["transparency"]["description"])
        tampered = deepcopy(first)
        tampered["candidate_memory"]["items"][0]["approval_status"] = "approved"
        with self.assertRaises(ContextAssemblyValidationError):
            validate_context_assembly_standalone(tampered)

    def test_source_backed_validation_and_build_are_zero_write(self):
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.source, self.semantic)
        }
        with patch(
            "memory.context_assembly.validate_context_assembly_source_backed",
            wraps=validate_context_assembly_source_backed,
        ) as source_backed:
            package = self.build()
        source_backed.assert_called_once()
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.source, self.semantic)
        }
        self.assertEqual(before, after)
        self.assertFalse(self.knowledge.exists())
        self.assertFalse((self.root / "memory.db").exists())
        self.assertFalse((self.root / "review.db").exists())
        self.assertTrue(package["readonly"])
        self.assertFalse(package["persisted"])
        self.assertFalse(package["external_model_calls"])

    def test_policy_profiles_and_custom_validation(self):
        for name in ("small", "medium", "large"):
            policy = normalize_assembly_policy(name)
            self.assertRegex(policy["policy_identity"], r"^csp_[0-9a-f]{64}$")
        custom = normalize_assembly_policy(
            "custom",
            total_characters=1_000,
            estimated_tokens=250,
            max_evidence=2,
            max_candidates=2,
            max_history_expansions=0,
        )
        self.assertEqual("Custom", custom["budget_profile"])
        with self.assertRaises(ContextAssemblyValidationError):
            normalize_assembly_policy(
                "custom",
                total_characters=511,
                estimated_tokens=250,
                max_evidence=2,
                max_candidates=2,
                max_history_expansions=0,
            )

    def test_cli_and_get_are_fixed_path_readonly_surfaces(self):
        args = parse_args(["MacBook", "--budget", "large", "--json"])
        self.assertFalse(hasattr(args, "db"))
        self.assertFalse(hasattr(args, "profile"))
        self.assertNotIn("source_db", inspect.signature(ContextAssemblyService).parameters)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["MacBook", "--db", "other.db"])
        with patch("tools.context_assembly_preview.build_production_context_assembly", return_value={"x": 1}), patch(
            "tools.context_assembly_preview.canonical_context_assembly_json", return_value="{}"
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, preview_main(["MacBook", "--json"]))
            self.assertEqual("{}", output.getvalue().strip())
        with patch(
            "tools.context_assembly_preview.build_production_context_assembly",
            side_effect=ContextAssemblyValidationError("invalid source /opt/private/context.db."),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(2, preview_main(["MacBook", "--json"]))
            error_payload = json.loads(output.getvalue())
            self.assertNotIn("/opt/private/context.db", error_payload["error"])
            self.assertIn(ABSOLUTE_PATH_REPLACEMENT, error_payload["error"])

        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH)
        handler.send_json = Mock()
        handler.send_text = Mock()
        handler.send_error_json = Mock()
        method_globals = AyunOSHandler.api_context_assembly_preview.__globals__
        with patch.dict(method_globals, {
            "build_production_context_assembly": Mock(return_value={"value": 1}),
        }):
            handler.api_context_assembly_preview({"q": ["MacBook"]})
        handler.send_json.assert_called_once_with({"value": 1})
        handler.send_error_json.reset_mock()
        handler.api_context_assembly_preview({"q": ["MacBook"], "db": ["x.db"]})
        handler.send_error_json.assert_called_once_with(
            "Unsupported context assembly parameter", HTTPStatus.BAD_REQUEST,
        )
        handler.send_json.reset_mock()
        handler.app = Mock(readonly=False, db_mode="real", db_path=REAL_DB_PATH)
        handler.api_context_assembly_preview({"q": ["MacBook"]})
        payload, status = handler.send_json.call_args.args
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("production_readonly_required", payload["reason_code"])


if __name__ == "__main__":
    unittest.main()
