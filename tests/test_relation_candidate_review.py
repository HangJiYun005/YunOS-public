from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memory.graph_identity import canonical_hash, make_edge, make_evidence_identity, make_generator_config_hash, make_summary_node
from memory.memory_graph import MemoryGraphBuild
from memory.relation_review import (
    RelationReviewError,
    decide_relation_candidate,
    discover_relation_candidates,
    prepare_review_plan,
    replacement_preview,
    required_confirmation_fields,
    validate_candidate_identity,
    validate_review_plan_identity,
    verify_review_plan,
)
from memory.relation_review_state import (
    LEDGER_NAME,
    REGISTRY_NAME,
    ReviewAuthorityMismatch,
    ReviewAuthorityNotInitialized,
    ReviewAuthorityStateLost,
    _TestReviewAuthorityFactory,
    _decision_event_identity,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _node(source_id: str, conversation: str, text: str):
    return make_summary_node({
        "source_table": "summary_versions", "source_id": source_id,
        "conversation_id": conversation, "memory_space": "whale",
        "summary_version": 1, "summary_text": text,
        "summary_text_hash": _sha(text), "source_snapshot_hash": _sha("snapshot-" + text),
    })


def _build(*, evidence="e1", score=.91, threshold=.8, config="c1", reverse=False, status="candidate"):
    left, right = _node("s1", "c1", "alpha"), _node("s2", "c2", "beta")
    source, target = (right, left) if reverse else (left, right)
    edge = make_edge(
        "semantic_related", source.node_id, target.node_id, direction="undirected",
        generator_identity="local_embedding:test",
        generator_config_hash=make_generator_config_hash({"config": config}),
        evidence_identity=make_evidence_identity({"evidence": evidence}),
        score=score, threshold=threshold, status=status,
    )
    return MemoryGraphBuild("v6.0", make_generator_config_hash({"build": config}), (left, right), (edge,), ())


def _combined():
    one, two = _build(), _build(evidence="e2")
    return MemoryGraphBuild(one.schema_version, one.generator_config_hash, one.nodes, (one.edges[0], two.edges[0]), ())


class AuthorityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.authority = _TestReviewAuthorityFactory.initialize(self.root)
        self.discovery = discover_relation_candidates(_combined())
        self.first, self.second = self.discovery.candidates

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, candidate=None):
        return prepare_review_plan(self.discovery, (candidate or self.first).candidate_id, authority=self.authority).plan

    def decide(self, plan, decision="accept", *, replacement=None, replacement_plan=None):
        return decide_relation_candidate(
            self.discovery, plan, decision=decision, reason_code="reviewed",
            reviewer_identity="operator@example.test",
            confirmation_fields=required_confirmation_fields(
                plan, decision, replacement_candidate=replacement, replacement_plan=replacement_plan),
            authority=self.authority,
            replacement_candidate_id=replacement.candidate_id if replacement else None,
            replacement_plan=replacement_plan,
        )


class CandidateIdentityTests(unittest.TestCase):
    def test_identity_is_time_independent_and_undirected(self):
        one = discover_relation_candidates(_build(), created_at="2026-01-01T00:00:00Z").candidates[0]
        two = discover_relation_candidates(_build(reverse=True), created_at="2027-01-01T00:00:00Z").candidates[0]
        self.assertEqual(one.candidate_id, two.candidate_id)

    def test_effective_changes_change_identity(self):
        base = discover_relation_candidates(_build()).candidates[0].candidate_id
        for changed in (_build(evidence="e2"), _build(score=.92), _build(threshold=.81), _build(config="c2")):
            self.assertNotEqual(base, discover_relation_candidates(changed).candidates[0].candidate_id)

    def test_strict_candidate_validation_rejects_protocol_inventions(self):
        candidate = discover_relation_candidates(_build()).candidates[0]
        for bad in (
            replace(candidate, candidate_schema_version="v999"),
            replace(candidate, relation_type="invented_fact"),
            replace(candidate, candidate_status="invented"),
            replace(candidate, score=float("nan")),
            replace(candidate, source_node_id=candidate.target_node_id, target_node_id=candidate.source_node_id),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RelationReviewError):
                    validate_candidate_identity(bad)


class AuthorityBoundaryTests(AuthorityCase):
    def test_public_api_requires_sealed_handle(self):
        with self.assertRaises(RelationReviewError):
            prepare_review_plan(self.discovery, self.first.candidate_id, authority=object())

    def test_production_module_exposes_no_test_factory_or_sentinel(self):
        import memory.relation_review as review_module
        import memory.relation_review_state as state_module
        self.assertFalse(hasattr(state_module, "TestReviewAuthorityFactory"))
        self.assertFalse(hasattr(state_module, "_HANDLE_SENTINEL"))
        self.assertFalse(hasattr(review_module, "_AUTHORITY_HANDLE_SEAL"))
        with self.assertRaises(RelationReviewError):
            state_module.ReviewAuthorityHandle()
        with self.assertRaises(RelationReviewError):
            state_module._issue_authority_handle(
                self.root / REGISTRY_NAME, self.root / LEDGER_NAME,
                self.authority.identity,
            )

    def test_repeated_prepare_returns_same_chain_and_plan(self):
        first = prepare_review_plan(self.discovery, self.first.candidate_id, authority=self.authority)
        second = prepare_review_plan(self.discovery, self.first.candidate_id, authority=_TestReviewAuthorityFactory.open(self.root))
        self.assertEqual((first.plan.review_chain_id, first.plan.review_plan_id), (second.plan.review_chain_id, second.plan.review_plan_id))

    def test_decision_is_unique_and_idempotent(self):
        plan = self.plan()
        accepted = self.decide(plan)
        retry = self.decide(plan)
        conflict = self.decide(plan, "reject")
        self.assertEqual(accepted["decision_id"], retry["decision_id"])
        self.assertTrue(retry["idempotent"])
        self.assertEqual("decision_conflict", conflict["status"])
        prepared = prepare_review_plan(self.discovery, self.first.candidate_id, authority=self.authority)
        self.assertEqual("decided", prepared.status)

    def test_old_plan_is_rejected_by_new_authority(self):
        old = self.plan()
        with tempfile.TemporaryDirectory() as other_root:
            new = _TestReviewAuthorityFactory.initialize(other_root)
            self.assertEqual("unavailable", verify_review_plan(self.discovery, old, authority=new)["status"])

    def test_concurrent_prepare_has_one_chain(self):
        barrier = threading.Barrier(4); results=[]; errors=[]
        def worker():
            try:
                handle = _TestReviewAuthorityFactory.open(self.root); barrier.wait()
                results.append(prepare_review_plan(self.discovery, self.first.candidate_id, authority=handle).plan.review_chain_id)
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertFalse(errors); self.assertEqual(1, len(set(results)))

    def test_later_graph_build_creates_explicit_successor(self):
        old_plans = [self.plan(candidate) for candidate in self.discovery.candidates]
        changed = discover_relation_candidates(_build(config="later"))
        successor = prepare_review_plan(changed, changed.candidates[0].candidate_id, authority=self.authority).plan
        self.assertIn(successor.predecessor_review_chain_id, {plan.review_chain_id for plan in old_plans})
        self.assertEqual(["stale", "stale"], [verify_review_plan(self.discovery, plan, authority=self.authority)["status"] for plan in old_plans])

    def test_injected_illegal_decision_is_rejected_on_reopen(self):
        plan = self.plan(); captured={}
        def capture(candidate, bound_plan, envelope, **kwargs):
            captured["envelope"]=envelope
            return {"status":"recorded", "envelope":envelope}
        with patch.object(self.authority, "_commit_decision", side_effect=capture):
            self.decide(plan)
        bad=replace(captured["envelope"], decision_schema_version="v999", decision="invented_fact")
        bad=replace(bad, decision_id="decision_"+canonical_hash("relation_review_decision", bad.immutable_payload()))
        ledger=self.root/LEDGER_NAME; registry=self.root/REGISTRY_NAME
        reg=sqlite3.connect(registry); previous_seq,previous=reg.execute("SELECT mutation_seq,mutation_digest FROM authority_registry_state").fetchone()
        seq=previous_seq+1; event_identity=_decision_event_identity(bad.decision_id,None)
        digest=canonical_hash("review_authority_mutation",{"authority_store_id":plan.authority_store_id,"mutation_seq":seq,"previous_digest":previous,"event_kind":"decision_recorded","event_identity":event_identity})
        conn=sqlite3.connect(ledger); conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?)",(bad.decision_id,bad.decision_schema_version,bad.review_plan_id,bad.review_chain_id,bad.payload_hash,json.dumps(bad.public_dict(),ensure_ascii=True,sort_keys=True,separators=(",",":"))))
        conn.execute("INSERT INTO authority_events VALUES(?,?,?,?,?,?)",(seq,previous,digest,"decision_recorded",event_identity,"2026-01-01T00:00:00Z"))
        conn.execute("UPDATE authority_ledger_metadata SET mutation_seq=?,mutation_digest=? WHERE singleton=1",(seq,digest)); conn.commit(); conn.close()
        reg.execute("UPDATE authority_registry_state SET mutation_seq=?,mutation_digest=? WHERE singleton=1",(seq,digest)); reg.commit(); reg.close()
        with self.assertRaises(ReviewAuthorityMismatch):
            _TestReviewAuthorityFactory.open(self.root)

    def test_valid_decision_injected_without_mutation_head_is_rejected(self):
        plan=self.plan(); captured={}
        def capture(candidate, bound_plan, envelope, **kwargs):
            captured["envelope"]=envelope
            return {"status":"recorded", "envelope":envelope}
        with patch.object(self.authority,"_commit_decision",side_effect=capture): self.decide(plan)
        envelope=captured["envelope"]; ledger=self.root/LEDGER_NAME
        conn=sqlite3.connect(ledger); conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?)",(envelope.decision_id,envelope.decision_schema_version,envelope.review_plan_id,envelope.review_chain_id,envelope.payload_hash,json.dumps(envelope.public_dict(),ensure_ascii=True,sort_keys=True,separators=(",",":"))))
        conn.commit(); conn.close()
        with self.assertRaises(ReviewAuthorityStateLost): _TestReviewAuthorityFactory.open(self.root)

    def test_injected_candidate_without_mutation_event_is_rejected(self):
        candidate = self.first; ledger = self.root / LEDGER_NAME
        conn=sqlite3.connect(ledger)
        conn.execute("INSERT INTO review_candidates VALUES(?,?,?,?)", (
            candidate.candidate_id, "v999", candidate.payload_hash, json.dumps(candidate.public_dict())))
        conn.commit(); conn.close()
        with self.assertRaises((ReviewAuthorityMismatch, ReviewAuthorityStateLost)):
            _TestReviewAuthorityFactory.open(self.root)

    def test_foreign_key_bypass_and_unsealed_event_are_rejected(self):
        ledger = self.root / LEDGER_NAME
        conn=sqlite3.connect(ledger)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?)", (
            "decision_" + "d"*64, "v6.1", "plan_" + "a"*64,
            "chain_" + "b"*64, "c"*64, json.dumps({"decision": "accept"})))
        conn.commit(); conn.close()
        with self.assertRaises(ReviewAuthorityMismatch):
            _TestReviewAuthorityFactory.open(self.root)

    def test_ledger_deletion_and_reinitialization_fail_closed(self):
        plan = self.plan(); (self.root / LEDGER_NAME).unlink()
        with self.assertRaises(ReviewAuthorityStateLost): _TestReviewAuthorityFactory.open(self.root)
        with self.assertRaises(RelationReviewError): _TestReviewAuthorityFactory.initialize(self.root)
        (self.root / REGISTRY_NAME).unlink()
        new = _TestReviewAuthorityFactory.initialize(self.root)
        self.assertEqual("unavailable", verify_review_plan(self.discovery, plan, authority=new)["status"])

    def test_empty_substitution_and_rollback_are_rejected(self):
        plan = self.plan(); ledger = self.root / LEDGER_NAME; backup = self.root / "old.db"
        shutil.copy2(ledger, backup)
        self.decide(plan)
        shutil.copy2(backup, ledger)
        with self.assertRaises(ReviewAuthorityStateLost): _TestReviewAuthorityFactory.open(self.root)
        ledger.unlink(); sqlite3.connect(ledger).close()
        with self.assertRaises(ReviewAuthorityMismatch): _TestReviewAuthorityFactory.open(self.root)

    def test_schema_and_metadata_corruption_rejected(self):
        registry = self.root / REGISTRY_NAME
        conn=sqlite3.connect(registry); conn.execute("DROP TRIGGER authority_registry_metadata_no_update"); conn.commit(); conn.close()
        with self.assertRaises(ReviewAuthorityMismatch): _TestReviewAuthorityFactory.open(self.root)

    def test_missing_authority_is_not_initialized(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ReviewAuthorityNotInitialized): _TestReviewAuthorityFactory.open(root)


class ReplacementTests(AuthorityCase):
    def test_preview_does_not_claim_supersession_or_mutate(self):
        original, replacement = self.plan(), self.plan(self.second)
        preview = replacement_preview(self.discovery, self.first.candidate_id, self.second.candidate_id,
                                      original_plan=original, replacement_plan=replacement, authority=self.authority)
        self.assertEqual(("candidate", "would_supersede", False),
                         (preview["current_status"], preview["proposed_effect"], preview["coordination_recorded"]))
        self.assertEqual("valid", verify_review_plan(self.discovery, original, authority=self.authority)["status"])

    def test_replace_is_bound_and_irreversible_in_authority(self):
        original, replacement = self.plan(), self.plan(self.second)
        result = self.decide(original, "replace", replacement=self.second, replacement_plan=replacement)
        self.assertEqual("superseded", result["status"])
        self.assertEqual("superseded", verify_review_plan(self.discovery, original, authority=self.authority)["status"])
        self.assertEqual("superseded", self.decide(original)["status"])
        self.assertEqual("superseded", prepare_review_plan(self.discovery, self.first.candidate_id, authority=self.authority).status)

    def test_plan_protocol_validation_is_strict(self):
        plan = self.plan()
        for bad in (replace(plan, review_schema_version="v999"), replace(plan, allowed_decisions=plan.allowed_decisions + ("execute",)), replace(plan, plan_status="invented")):
            with self.assertRaises(RelationReviewError): validate_review_plan_identity(bad)


if __name__ == "__main__":
    unittest.main()
