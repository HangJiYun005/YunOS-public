from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
import io
import inspect
import json
from http import HTTPStatus
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from memory.evidence_selection import EvidenceSelectionService
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import MemoryPackageService
from memory.temporal_memory_candidates import (
    REASON_MAX_CHARS,
    SAFE_STATEMENT_PLACEHOLDER,
    STATEMENT_MAX_CHARS,
    TemporalCandidateService,
    TemporalCandidateValidationError,
    _TemporalCandidateCore,
    _candidate_from_event,
    _derive_relations,
    _digest,
    _events_from_selected,
    _identity_payload,
    _interpret_clause,
    _scan_scoped_segments,
    _selected_provider_envelope,
    build_candidates_from_selection,
    canonical_temporal_candidates_json,
    create_timezone_config,
    default_timezone_config,
    render_temporal_candidates,
    validate_temporal_candidates_against_selection,
    validate_temporal_candidates_source_backed,
    validate_temporal_candidates_standalone,
)
from tools.local_web import AyunOSHandler, REAL_DB_PATH
from tools.temporal_memory_candidates_preview import main as preview_main, parse_args


class FakeExternalProvider:
    provider_id = "remote-model"

    def interpret(self, selection, timezone_config):
        raise AssertionError("must never be called")


class TemporalMemoryCandidatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.semantic = self.root / "semantic-missing.db"
        self.knowledge = self.root / "knowledge-missing.db"
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
            rows = [
                "memory Preference: I prefer tea.",
                "memory Preference: Now I prefer coffee.",
                "memory Preference: I also prefer water.",
                "memory Decision: use A.",
                "memory Decision: replace A with only use B.",
                "memory Constraint: must use TLS.",
                "memory Constraint: cannot use TLS.",
                "memory Goal: when traveling, pack light.",
                "memory Goal: when home, keep supplies ready.",
                "memory Fact: version one is deployed.",
                "memory Fact: version two is observed.",
                "memory Acceptable alternative: bus is fine.",
                "memory Open question: which route?",
                "memory Fact: maybe the service is stable.",
                "memory Fact: " + "x" * 180 + ".",
            ]
            conn.executemany(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                [(
                    f"c{index}", f"Memory candidate {index}", 1735689600.0 + index * 3600,
                    f"2025-01-01T{index % 20:02d}:00:00+00:00", 1736900000.0 + index,
                    "2025-01-15T00:00:00+00:00",
                ) for index in range(1, len(rows) + 1)],
            )
            conn.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                [(
                    f"m{index}", f"c{index}", f"Memory candidate {index}", index, "user",
                    1735689600.0 + index * 3600,
                    f"2025-01-{1 + (index // 20):02d}T{index % 20:02d}:00:00+00:00",
                    text, "general",
                ) for index, text in enumerate(rows, start=1)],
            )
            conn.commit()
        retriever = GraphEnhancedRetrievalService(
            self.source, self.semantic, self.knowledge,
            semantic_searcher=lambda *args, **kwargs: {"results": []},
        )
        self.memory_service = MemoryPackageService(self.source, retriever=retriever)
        self.selection_service = EvidenceSelectionService(
            self.source, memory_package_service=self.memory_service,
        )
        self.service = _TemporalCandidateCore(
            self.source, selection_service=self.selection_service,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def query(self) -> RetrievalQuery:
        return RetrievalQuery("memory", hit_types=("keyword",), limit=20)

    def selection(self) -> dict:
        return self.selection_service.build(
            self.query(), budget_profile="Custom", character_budget=100_000,
            estimated_token_budget=25_000, per_conversation_cap=20, max_selected=100,
        )

    def insert_messages(self, rows: list[tuple[str, str]]) -> None:
        with sqlite3.connect(self.source) as conn:
            start = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] + 1
            for offset, (role, text) in enumerate(rows):
                index = start + offset
                observed = 1738368000.0 + index * 3600
                iso = f"2025-02-{1 + offset:02d}T12:00:00+00:00"
                conn.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                    (f"cx{index}", f"Added {index}", observed, iso, observed, iso),
                )
                conn.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"mx{index}", f"cx{index}", f"Added {index}", index, role, observed, iso, text, "general"),
                )
            conn.commit()

    def reseal(self, package: dict) -> None:
        package["package_identity"] = "tmc_" + _digest(_identity_payload(package))

    def test_all_candidate_types_and_review_evidence_are_preserved(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        types = {item["candidate_type"] for item in package["candidates"]}
        self.assertEqual({
            "fact", "preference", "goal", "decision", "constraint",
            "acceptable_alternative", "open_question",
        }, types)
        self.assertTrue(all(item["state"] == "candidate" for item in package["candidates"]))
        self.assertTrue(all(item["evidence"]["message_id"] for item in package["candidates"]))
        speculative = next(item for item in package["candidates"] if "maybe" in item["statement"])
        self.assertEqual(("needs_review", "low", "speculative"), (
            speculative["review_status"], speculative["confidence"], speculative["explicitness"],
        ))

    def test_temporal_rules_distinguish_preference_coexistence_replacement_conflict_and_unknown(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        relations = package["temporal_relations"]
        kinds = {item["relation"] for item in relations}
        self.assertTrue({"evolution", "coexistence", "conditional", "supersedes", "conflict", "unknown"} <= kinds)
        preference_relations = []
        candidates = {item["candidate_id"]: item for item in package["candidates"]}
        for relation in relations:
            if candidates[relation["newer_candidate_id"]]["candidate_type"] == "preference":
                preference_relations.append(relation)
        self.assertNotIn("supersedes", {item["relation"] for item in preference_relations})
        supersedes = next(item for item in relations if item["relation"] == "supersedes")
        self.assertTrue(supersedes["exclusive"])
        self.assertFalse(next(item for item in relations if item["relation"] == "coexistence")["exclusive"])

    def test_old_candidate_is_retained_and_unknown_does_not_expire_it(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        facts = [item for item in package["candidates"] if item["candidate_type"] == "fact"]
        self.assertGreaterEqual(len(facts), 2)
        self.assertTrue(any(item["relation"] == "unknown" for item in package["temporal_relations"]))
        self.assertTrue(any("version one" in item["statement"] for item in facts))

    def test_character_limits_are_hard_and_overflow_needs_review(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        self.assertTrue(all(len(item["statement"]) <= STATEMENT_MAX_CHARS for item in package["candidates"]))
        self.assertTrue(all(len(item["reason"]) <= REASON_MAX_CHARS for item in package["candidates"]))
        clipped = next(item for item in package["candidates"] if item["statement"] == SAFE_STATEMENT_PLACEHOLDER)
        self.assertEqual("needs_review", clipped["review_status"])

    def test_same_input_and_timezone_identity_are_stable(self):
        selection = self.selection()
        first = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        second = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual(first, second)
        self.assertEqual(first["package_identity"], second["package_identity"])
        self.assertEqual("Asia/Shanghai", first["timezone_config"]["timezone"])
        self.assertTrue(all(item["observed_at_utc"].endswith("Z") for item in first["events"]))

    def test_timezone_revision_protocol_binds_previous_identity(self):
        initial = default_timezone_config()
        changed = create_timezone_config(
            "Europe/Paris", country_or_region="FR", revision=2, confirmation_status="confirmed",
            previous_config=initial, change_reason="user_changed_timezone",
        )
        self.assertNotEqual(initial["config_identity"], changed["config_identity"])
        self.assertEqual(initial["config_identity"], changed["predecessor"]["config_identity"])
        self.assertEqual(initial, changed["predecessor"]["config"])
        with self.assertRaises(TemporalCandidateValidationError):
            create_timezone_config(
                "Europe/Paris", country_or_region="FR", revision=2, confirmation_status="confirmed",
                change_reason="missing predecessor",
            )
        with self.assertRaises(TemporalCandidateValidationError):
            create_timezone_config(
                "Europe/Paris", country_or_region="FR", revision=3, confirmation_status="confirmed",
                previous_config=initial, change_reason="revision jump",
            )
        initial_package = build_candidates_from_selection(
            self.selection(), timezone_config=initial,
        )
        from_package = create_timezone_config(
            "America/New_York", country_or_region="US", revision=2,
            confirmation_status="confirmed", previous_package=initial_package,
            change_reason="user confirmed location",
        )
        self.assertEqual(initial_package["package_identity"], from_package["predecessor"]["package_identity"])
        self.assertEqual(initial, from_package["predecessor"]["config"])
        changed_package = build_candidates_from_selection(self.selection(), timezone_config=from_package)
        self.assertNotEqual(initial_package["package_identity"], changed_package["package_identity"])
        self.assertEqual(
            [item["observed_at_utc"] for item in initial_package["events"]],
            [item["observed_at_utc"] for item in changed_package["events"]],
        )
        selection = self.selection()
        source_package = self.memory_service.build(self.query())
        with self.assertRaisesRegex(TemporalCandidateValidationError, "trusted timezone predecessor"):
            validate_temporal_candidates_against_selection(
                changed_package, trusted_selection=selection,
            )
        validate_temporal_candidates_against_selection(
            changed_package, trusted_selection=selection,
            trusted_timezone_predecessor=initial_package,
        )
        with self.assertRaisesRegex(TemporalCandidateValidationError, "trusted timezone predecessor"):
            validate_temporal_candidates_source_backed(
                changed_package, trusted_selection=selection, trusted_source_package=source_package,
                trusted_source_db=self.source,
            )
        validate_temporal_candidates_source_backed(
            changed_package, trusted_selection=selection, trusted_source_package=source_package,
            trusted_source_db=self.source, trusted_timezone_predecessor=initial_package,
        )

        format_only = deepcopy(changed_package)
        format_only["timezone_config"]["predecessor"].pop("config")
        timezone_config = format_only["timezone_config"]
        timezone_config["config_identity"] = "tzc_" + _digest({
            key: value for key, value in timezone_config.items() if key != "config_identity"
        })
        self.reseal(format_only)
        for validator in (
            lambda: validate_temporal_candidates_standalone(format_only),
            lambda: validate_temporal_candidates_against_selection(
                format_only, trusted_selection=selection,
                trusted_timezone_predecessor=initial_package,
            ),
            lambda: validate_temporal_candidates_source_backed(
                format_only, trusted_selection=selection, trusted_source_package=source_package,
                trusted_source_db=self.source, trusted_timezone_predecessor=initial_package,
            ),
        ):
            with self.assertRaises(TemporalCandidateValidationError):
                validator()

        forged_initial = create_timezone_config(
            "Asia/Shanghai", country_or_region="CN", revision=1,
            confirmation_status="confirmed", change_reason="forged predecessor",
        )
        forged_config = create_timezone_config(
            "America/New_York", country_or_region="US", revision=2,
            confirmation_status="confirmed", previous_config=forged_initial,
            change_reason="forged chain",
        )
        forged_package = build_candidates_from_selection(selection, timezone_config=forged_config)
        validate_temporal_candidates_standalone(forged_package)
        with self.assertRaisesRegex(TemporalCandidateValidationError, "binding mismatch"):
            validate_temporal_candidates_against_selection(
                forged_package, trusted_selection=selection,
                trusted_timezone_predecessor=initial,
            )
        with self.assertRaisesRegex(TemporalCandidateValidationError, "binding mismatch"):
            validate_temporal_candidates_source_backed(
                forged_package, trusted_selection=selection, trusted_source_package=source_package,
                trusted_source_db=self.source, trusted_timezone_predecessor=initial,
            )

    def test_timezone_reserved_missing_and_jump_predecessors_fail_closed(self):
        selection = self.selection()
        source_package = self.memory_service.build(self.query())
        initial = default_timezone_config()
        changed = create_timezone_config(
            "Europe/Paris", country_or_region="FR", revision=2,
            confirmation_status="confirmed", previous_config=initial,
            change_reason="reserved identity test",
        )
        package = build_candidates_from_selection(selection, timezone_config=changed)

        def reseal_timezone(candidate):
            config = candidate["timezone_config"]
            config["config_identity"] = "tzc_" + _digest({
                key: value for key, value in config.items() if key != "config_identity"
            })
            self.reseal(candidate)

        for payload in ("0" * 64, "f" * 64):
            with self.subTest(package_identity=payload[0]):
                forged = deepcopy(package)
                forged["timezone_config"]["predecessor"]["package_identity"] = "tmc_" + payload
                reseal_timezone(forged)
                validators = (
                    lambda: validate_temporal_candidates_standalone(forged),
                    lambda: validate_temporal_candidates_against_selection(
                        forged, trusted_selection=selection,
                        trusted_timezone_predecessor=initial,
                    ),
                    lambda: validate_temporal_candidates_source_backed(
                        forged, trusted_selection=selection,
                        trusted_source_package=source_package, trusted_source_db=self.source,
                        trusted_timezone_predecessor=initial,
                    ),
                )
                for validator in validators:
                    with self.assertRaises(TemporalCandidateValidationError):
                        validator()

        for field in ("config_identity", "package_identity"):
            with self.subTest(empty_identity=field):
                forged = deepcopy(package)
                forged["timezone_config"]["predecessor"][field] = ""
                reseal_timezone(forged)
                with self.assertRaises(TemporalCandidateValidationError):
                    validate_temporal_candidates_standalone(forged)

        for payload in ("0" * 64, "f" * 64):
            with self.subTest(config_identity=payload[0]):
                forged = deepcopy(package)
                forged["timezone_config"]["predecessor"]["config_identity"] = "tzc_" + payload
                reseal_timezone(forged)
                with self.assertRaises(TemporalCandidateValidationError):
                    validate_temporal_candidates_standalone(forged)

        missing = deepcopy(package)
        missing["timezone_config"]["predecessor"] = None
        reseal_timezone(missing)
        with self.assertRaises(TemporalCandidateValidationError):
            validate_temporal_candidates_standalone(missing)

        jumped = deepcopy(package)
        jumped["timezone_config"]["revision"] = 99
        reseal_timezone(jumped)
        with self.assertRaisesRegex(TemporalCandidateValidationError, "not contiguous"):
            validate_temporal_candidates_standalone(jumped)

    def test_external_provider_is_rejected_without_call(self):
        service = _TemporalCandidateCore(
            self.source, selection_service=self.selection_service, provider=FakeExternalProvider(),
        )
        with patch.object(self.selection_service, "build", side_effect=AssertionError("selection must not run")):
            with self.assertRaisesRegex(TemporalCandidateValidationError, "external"):
                service.build(self.query(), budget_profile="Large")

    def test_strict_validator_rejects_promotion_and_relation_forgery(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        promoted = deepcopy(package)
        promoted["candidates"][0]["state"] = "approved"
        self.reseal(promoted)
        with self.assertRaises(TemporalCandidateValidationError):
            validate_temporal_candidates_standalone(promoted)
        forged = deepcopy(package)
        forged["temporal_relations"][0]["relation"] = "supersedes"
        self.reseal(forged)
        with self.assertRaises(TemporalCandidateValidationError):
            validate_temporal_candidates_standalone(forged)

    def test_source_backed_and_trusted_selection_reconstruction(self):
        query = self.query()
        selection = self.selection()
        source_package = self.memory_service.build(query)
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        validate_temporal_candidates_against_selection(package, trusted_selection=selection)
        validate_temporal_candidates_source_backed(
            package, trusted_selection=selection, trusted_source_package=source_package,
            trusted_source_db=self.source,
        )
        forged = deepcopy(package)
        forged["events"][0]["statement"] = "Fact: forged"
        forged["events"][0]["event_id"] = "evt_" + _digest({
            k: v for k, v in forged["events"][0].items() if k != "event_id"
        })
        forged["candidates"] = []
        with self.assertRaises(TemporalCandidateValidationError):
            validate_temporal_candidates_against_selection(forged, trusted_selection=selection)

    def test_attribution_gate_rejects_non_user_and_scoped_non_assertions(self):
        blocked = [
            ("user", "scopecheck 他说：我决定买 Pro。"),
            ("user", "scopecheck 模型建议我买 Pro。"),
            ("user", "scopecheck 假如我只买 Pro。"),
            ("user", "scopecheck 难道我要买 Pro 吗？"),
            ("user", "scopecheck 这不是事实：我已经买了 Pro。"),
            ("user", "scopecheck 示例：我决定买 Pro。"),
            ("user", "scopecheck “我决定买 Pro”。"),
            ("user", "scopecheck “我现在更想买 Pro”。"),
            ("user", "scopecheck “Air 便宜很多也可以”。"),
            ("user", "scopecheck “我还没买 Pro”。"),
            ("user", "scopecheck 'I decided to buy Pro'."),
            ("user", "> scopecheck 我决定买 Pro。"),
            ("user", "scopecheck 小王说：我决定买 Pro。"),
            ("user", "scopecheck 如果我决定买 Pro，就换电脑包。"),
            ("user", "scopecheck 我在想如果我决定买 Pro 就换电脑包。"),
            ("user", "scopecheck If I decided to buy Pro, I would change bags."),
            ("user", "scopecheck 我决定买 Pro 了吗？"),
            ("user", "scopecheck 我是不是决定买 Pro"),
            ("user", "scopecheck 哈哈，我决定买 Pro。"),
            ("user", "scopecheck `我决定买 Pro`。"),
            ("assistant", "scopecheck Fact: User bought Pro."),
        ]
        self.insert_messages(blocked + [("user", "scopecheck 我决定买 Pro。")])
        query = RetrievalQuery("scopecheck", hit_types=("keyword",), limit=20)
        selection = self.selection_service.build(query, budget_profile="Large")
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual(1, package["counts"]["candidates"])
        self.assertEqual("decision", package["candidates"][0]["candidate_type"])
        self.assertEqual("user", package["events"][0]["source"]["message_anchor"]["role"])
        source_package = self.memory_service.build(query)
        validate_temporal_candidates_source_backed(
            package, trusted_selection=selection, trusted_source_package=source_package,
            trusted_source_db=self.source,
        )

        open_question = _interpret_clause("Open question: which route?")
        self.assertEqual(("open_question", "ambiguous", "low"), (
            open_question["candidate_type"], open_question["explicitness"], open_question["confidence"],
        ))

    def test_inherited_scopes_block_multisegment_candidates_source_backed(self):
        cases = {
            "scopequote": ("quote", "scopequote “这是示例。\n我决定买 Pro。\n请忽略。”"),
            "scopeasciiquote": ("quote", 'scopeasciiquote "Example?\nI decided to buy Pro?\nIgnore this."'),
            "scopeblock": ("blockquote", "scopeblock > 这是引用\n> 我决定买 Pro"),
            "scopecode": ("code", "scopecode ```\n示例文本如下。\n我决定买 Pro。\n```"),
            "scopereport": ("third_party_report", "scopereport 小王说：这是建议。\n我决定买 Pro。"),
            "scopeexample": ("example", "scopeexample 示例文本如下。\n我决定买 Pro。"),
            "scopehyp": ("hypothetical", "scopehyp 如果只是举例。\n我决定买 Pro。"),
            "scopejoke": ("joke", "scopejoke 开个玩笑。\n我决定买 Pro。"),
        }
        self.insert_messages([("user", text) for _, text in cases.values()])
        for query_text, (expected_scope, original) in cases.items():
            with self.subTest(scope=expected_scope):
                segments = _scan_scoped_segments(original, role="user")
                decision_segment = next(
                    item for item in segments
                    if "我决定买 Pro" in item.text or "I decided to buy Pro" in item.text
                )
                self.assertIn(expected_scope, decision_segment.inherited_scopes)
                self.assertEqual("rejected", decision_segment.attribution["status"])

                query = RetrievalQuery(query_text, hit_types=("keyword",), limit=20)
                selection = self.selection_service.build(query, budget_profile="Large")
                package = build_candidates_from_selection(
                    selection, timezone_config=default_timezone_config(),
                )
                self.assertEqual(0, package["counts"]["candidates"])
                validate_temporal_candidates_source_backed(
                    package, trusted_selection=selection,
                    trusted_source_package=self.memory_service.build(query),
                    trusted_source_db=self.source,
                )

        for direct in (
            "我现在更想买 Pro",
            "我决定买 Pro",
            "我的目标只有 Pro，不再考虑 Air",
        ):
            segments = _scan_scoped_segments(direct, role="user")
            self.assertEqual(1, len(segments))
            self.assertEqual((), segments[0].inherited_scopes)
            self.assertEqual("user_direct", segments[0].attribution["status"])
            self.assertIsNotNone(_interpret_clause(direct, attribution=segments[0].attribution))

    def test_negation_tendency_decision_and_exclusivity_are_conservative(self):
        tendency = _interpret_clause("Pro 好像更适合我")
        self.assertEqual(("preference", "speculative", False), (
            tendency["candidate_type"], tendency["explicitness"], tendency["markers"]["exclusive"],
        ))
        wanting = _interpret_clause("我现在更想买 Pro")
        self.assertEqual("preference", wanting["candidate_type"])
        self.assertFalse(wanting["markers"]["exclusive"])
        alternative = _interpret_clause("Air 便宜很多也可以")
        self.assertEqual("acceptable_alternative", alternative["candidate_type"])
        self.assertEqual("price_lower", alternative["condition"])
        self.assertEqual("decision", _interpret_clause("我决定买 Pro")["candidate_type"])
        exclusive = _interpret_clause("我的目标只有 Pro，不再考虑 Air")
        self.assertTrue(exclusive["markers"]["exclusive_replacement"])
        self.assertEqual("positive", exclusive["polarity"])
        self.assertTrue(exclusive["target_keys"])
        self.assertTrue(exclusive["excluded_target_keys"])
        non_exclusive = _interpret_clause("我的目标不是只考虑 Pro")
        self.assertFalse(non_exclusive["markers"]["exclusive"])
        not_bought = _interpret_clause("我还没买 Pro")
        self.assertEqual(("fact", "negative"), (not_bought["candidate_type"], not_bought["polarity"]))
        self.assertIsNone(_interpret_clause("可能会买 Pro"))
        unknown = _interpret_clause("不知道买没买 Pro")
        self.assertEqual(("open_question", "uncertain"), (unknown["candidate_type"], unknown["polarity"]))

    def test_macbook_evolution_and_conditional_alternative_coexist_without_superseding(self):
        self.insert_messages([
            ("user", "macbookcase 早期考虑 Air。"),
            ("user", "macbookcase 后来更倾向 Pro。"),
            ("user", "macbookcase Air 便宜很多也可以。"),
        ])
        query = RetrievalQuery("macbookcase", hit_types=("keyword",), limit=20)
        selection = self.selection_service.build(query, budget_profile="Large")
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual(
            ["preference", "preference", "acceptable_alternative"],
            [item["candidate_type"] for item in package["candidates"]],
        )
        relations = package["temporal_relations"]
        self.assertEqual(["evolution", "coexistence"], [item["relation"] for item in relations])
        self.assertTrue(package["events"][2]["markers"]["conditional"])
        self.assertEqual("price_lower", package["events"][2]["condition"])
        self.assertNotIn("supersedes", {item["relation"] for item in relations})
        self.assertNotIn("conflict", {item["relation"] for item in relations})

    def test_intent_and_current_state_are_not_a_conflict(self):
        self.insert_messages([
            ("user", "intentcase 我决定买 Pro。"),
            ("user", "intentcase 我还没买 Pro。"),
        ])
        selection = self.selection_service.build(
            RetrievalQuery("intentcase", hit_types=("keyword",), limit=20),
            budget_profile="Large",
        )
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual(["decision", "fact"], [item["candidate_type"] for item in package["candidates"]])
        self.assertEqual("unknown", package["temporal_relations"][0]["relation"])

    def test_excluded_air_does_not_negate_or_conflict_with_positive_pro_goal(self):
        self.insert_messages([
            ("user", "goalpolarity 我的目标只有 Pro，不再考虑 Air。"),
            ("user", "goalpolarity Goal: Pro."),
        ])
        selection = self.selection_service.build(
            RetrievalQuery("goalpolarity", hit_types=("keyword",), limit=20),
            budget_profile="Large",
        )
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual(["positive", "positive"], [item["polarity"] for item in package["events"]])
        self.assertTrue(package["events"][0]["excluded_target_keys"])
        self.assertEqual("unknown", package["temporal_relations"][0]["relation"])
        self.assertNotIn("conflict", {item["relation"] for item in package["temporal_relations"]})

    def test_partial_source_capabilities_remain_partial(self):
        for capability in ("semantic", "graph"):
            with self.subTest(capability=capability):
                retriever = GraphEnhancedRetrievalService(self.source, self.semantic, self.knowledge)
                memory_service = MemoryPackageService(self.source, retriever=retriever)
                selection_service = EvidenceSelectionService(self.source, memory_package_service=memory_service)
                query = RetrievalQuery("memory", hit_types=("keyword", capability), limit=20)
                selection = selection_service.build(query, budget_profile="Large")
                self.assertEqual("partial", selection["status"])
                package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
                self.assertEqual("partial", package["status"])
                self.assertEqual(selection["capabilities"], package["source_projection"]["capabilities"])
                self.assertEqual(selection["missing_capabilities"], package["source_projection"]["missing_capabilities"])
                self.assertIn(capability, package["source_projection"]["missing_capabilities"])
                validate_temporal_candidates_source_backed(
                    package, trusted_selection=selection, trusted_source_package=memory_service.build(query),
                    trusted_source_db=self.source,
                )
                forged = deepcopy(package)
                forged["source_projection"]["capabilities"][capability]["unknown_extension"] = True
                self.reseal(forged)
                with self.assertRaises(TemporalCandidateValidationError):
                    validate_temporal_candidates_standalone(forged)

    def test_unavailable_source_cannot_become_a_normal_empty_package(self):
        missing = self.root / "source-missing.db"
        memory_service = MemoryPackageService(missing)
        selection_service = EvidenceSelectionService(missing, memory_package_service=memory_service)
        query = RetrievalQuery("memory", hit_types=("keyword",), limit=20)
        selection = selection_service.build(query, budget_profile="Large")
        self.assertEqual("unavailable", selection["status"])
        package = build_candidates_from_selection(selection, timezone_config=default_timezone_config())
        self.assertEqual("unavailable", package["status"])
        self.assertEqual(0, package["counts"]["candidates"])
        self.assertNotEqual("no_interpretable_candidate", package["reason_code"])

    def test_safe_length_never_preserves_a_semantic_prefix(self):
        statement, clipped = __import__(
            "memory.temporal_memory_candidates", fromlist=["_bounded"]
        )._bounded("Fact: I bought Pro " + "x" * 200 + " but that is false", STATEMENT_MAX_CHARS)
        self.assertTrue(clipped)
        self.assertEqual(SAFE_STATEMENT_PLACEHOLDER, statement)
        self.assertNotIn("bought Pro", statement)
        self.assertIsNone(_interpret_clause("这不是事实：我已经买了 Pro " + "x" * 200))

    def test_nested_unknown_field_fails_even_after_coordinated_reseal(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        forged = deepcopy(package)
        forged["events"][0]["markers"]["unknown_extension"] = False
        event = forged["events"][0]
        event["event_id"] = "evt_" + _digest({k: v for k, v in event.items() if k != "event_id"})
        forged["candidates"] = [_candidate_from_event(item) for item in forged["events"]]
        forged["temporal_relations"] = _derive_relations(forged["candidates"], forged["events"])
        forged["counts"]["relations"] = len(forged["temporal_relations"])
        forged["counts"]["relation_types"] = {
            kind: sum(item["relation"] == kind for item in forged["temporal_relations"])
            for kind in ("evolution", "coexistence", "conditional", "supersedes", "conflict", "unknown")
        }
        self.reseal(forged)
        with self.assertRaisesRegex(TemporalCandidateValidationError, "markers fields"):
            validate_temporal_candidates_standalone(forged)

    def test_fullwidth_unicode_and_evidence_permutations_are_deterministic(self):
        ascii_rule = _interpret_clause("Preference: I prefer Pro")
        fullwidth_rule = _interpret_clause("ＰＲＥＦＥＲＥＮＣＥ： I prefer Ｐｒｏ")
        self.assertEqual(ascii_rule, fullwidth_rule)
        self.assertEqual(
            _interpret_clause("Preference: I prefer Cafe\u0301"),
            _interpret_clause("Preference: I prefer Café"),
        )
        selection = self.selection()
        first = []
        second = []
        timezone_config = default_timezone_config()
        for item in selection["selected"]:
            first.extend(_events_from_selected(item, timezone_config))
        for item in reversed(selection["selected"]):
            second.extend(_events_from_selected(item, timezone_config))
        key = lambda item: (item["observed_at_utc"], item["event_id"])
        self.assertEqual(sorted(first, key=key), sorted(second, key=key))

    def test_public_core_and_future_provider_envelope_are_closed(self):
        self.assertEqual("()", str(inspect.signature(TemporalCandidateService)))
        with self.assertRaises(TypeError):
            TemporalCandidateService(self.source)
        envelope = _selected_provider_envelope(self.selection())
        self.assertTrue(envelope)
        self.assertEqual({"candidate_id", "source_anchor", "excerpt", "evidence_ids"}, set(envelope[0]))
        self.assertEqual(
            {"conversation_id", "message_id", "role", "time", "reference_uri"},
            set(envelope[0]["source_anchor"]),
        )
        serialized = json.dumps(envelope)
        self.assertNotIn("omitted", serialized)
        self.assertNotIn("model_payload", serialized)

    def test_json_and_text_preview_validate(self):
        package = build_candidates_from_selection(
            self.selection(), timezone_config=default_timezone_config(),
        )
        self.assertEqual(package, json.loads(canonical_temporal_candidates_json(package)))
        text = render_temporal_candidates(package)
        self.assertIn(package["package_identity"], text)
        self.assertIn("candidate", text.casefold())

    def test_cli_has_no_database_or_provider_argument(self):
        args = parse_args(["memory", "--budget", "large", "--json"])
        self.assertFalse(hasattr(args, "db"))
        self.assertFalse(hasattr(args, "provider"))
        expected = {"query", "budget", "start_time", "end_time", "result_mode", "context_window", "limit", "cursor", "hit_types", "graph_depth", "relation_budget", "path_budget", "context_budget", "memory_space", "json"}
        self.assertEqual(expected, set(vars(args)))
        with patch("tools.temporal_memory_candidates_preview.build_production_temporal_candidates", return_value={"x": 1}), patch(
            "tools.temporal_memory_candidates_preview.canonical_temporal_candidates_json", return_value="{}"
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, preview_main(["memory", "--json"]))
            self.assertEqual("{}", output.getvalue().strip())

    def test_get_requires_fixed_production_readonly_and_rejects_db(self):
        handler = object.__new__(AyunOSHandler)
        handler.app = Mock(readonly=True, db_mode="real", db_path=REAL_DB_PATH)
        handler.send_json = Mock()
        handler.send_text = Mock()
        handler.send_error_json = Mock()
        method_globals = AyunOSHandler.api_temporal_memory_candidates_preview.__globals__
        with patch.dict(method_globals, {
            "build_production_temporal_candidates": Mock(return_value={"value": 1}),
        }):
            handler.api_temporal_memory_candidates_preview({"q": ["memory"]})
        handler.send_json.assert_called_once_with({"value": 1})
        handler.send_error_json.reset_mock()
        handler.api_temporal_memory_candidates_preview({"q": ["memory"], "db": ["x.db"]})
        handler.send_error_json.assert_called_once_with(
            "Unsupported temporal candidate parameter", HTTPStatus.BAD_REQUEST,
        )
        handler.send_json.reset_mock()
        handler.app = Mock(readonly=False, db_mode="real", db_path=REAL_DB_PATH)
        handler.api_temporal_memory_candidates_preview({"q": ["memory"]})
        payload, status = handler.send_json.call_args.args
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("production_readonly_required", payload["reason_code"])


if __name__ == "__main__":
    unittest.main()
