from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.controlled_knowledge_persistence import (
    AnchorAssessment, ControlledPersistenceError, PersistenceRequest,
    SimulatedInterruption, persist_accepted_relation,
    production_enablement_conditions, restore_test_sidecar,
)
from memory.current_relation_discovery import _TestCurrentDiscoveryFactory
from memory.graph_identity import make_edge, make_evidence_identity, make_generator_config_hash, make_summary_node
from memory.knowledge_sidecar import PRODUCTION_KNOWLEDGE_SIDECAR, TestKnowledgeGraphRepository, initialize_test_knowledge_sidecar
from memory.memory_graph import MemoryGraphBuild
from memory.relation_review import decide_relation_candidate, prepare_review_plan, required_confirmation_fields
from memory.relation_review_state import _TestReviewAuthorityFactory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()


def _build(*, evidence: str = "e1", config: str = "c1", text_suffix: str = "") -> MemoryGraphBuild:
    nodes = []
    for source_id, conversation, text in (("s1", "c1", "alpha"), ("s2", "c2", "beta")):
        text += text_suffix
        nodes.append(make_summary_node({
            "source_table":"summary_versions","source_id":source_id,"conversation_id":conversation,
            "memory_space":"whale","summary_version":1,"summary_text":text,
            "summary_text_hash":_sha(text),"source_snapshot_hash":_sha("snapshot-"+text),
        }))
    edge = make_edge("semantic_related",nodes[0].node_id,nodes[1].node_id,direction="undirected",
        generator_identity="local_embedding:test",generator_config_hash=make_generator_config_hash({"config":config}),
        evidence_identity=make_evidence_identity({"evidence":evidence}),score=.91,threshold=.8,status="candidate")
    return MemoryGraphBuild("v6.0",make_generator_config_hash({"build":config}),tuple(nodes),(edge,),())


class _Anchor:
    def __init__(self, satisfied: bool): self.satisfied=satisfied
    def assess(self, **_: object) -> AnchorAssessment:
        return AnchorAssessment(True,self.satisfied,"anchor:test","assertion_"+"a"*64,"test")


class ControlledKnowledgePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.root=Path(self.tmp.name)
        self.authority_root=self.root/"authority"; self.authority_root.mkdir()
        self.discovery_root=self.root/"discovery"; self.discovery_root.mkdir()
        self.backups=self.root/"backups"; self.backups.mkdir(); self.db=self.root/"knowledge.db"
        self.build=_build(); self.provider=_TestCurrentDiscoveryFactory.initialize(self.discovery_root,self.build)
        self.authority=_TestReviewAuthorityFactory.initialize(self.authority_root)
        discovery=self.provider.rebuild(); self.candidate=discovery.candidates[0]
        self.plan=prepare_review_plan(discovery,self.candidate.candidate_id,authority=self.authority).plan
        result=decide_relation_candidate(discovery,self.plan,decision="accept",reason_code="reviewed",
            reviewer_identity="operator@example.test",confirmation_fields=required_confirmation_fields(self.plan,"accept"),authority=self.authority)
        self.request=PersistenceRequest(self.candidate.candidate_id,self.plan.review_plan_id,result["decision_id"])
        initialize_test_knowledge_sidecar(self.db); TestKnowledgeGraphRepository(self.db).append_build(self.build,generated_at="2026-01-01T00:00:00Z")

    def tearDown(self): self.tmp.cleanup()

    def preview(self, *, db=None, authority=None, request=None, provider=None, anchor=None):
        return persist_accepted_relation(authority or self.authority,request or self.request,
            discovery_provider=provider or self.provider,sidecar_path=db or self.db,backup_dir=self.backups,
            execute=False,test_mode=True,external_anchor=anchor)

    def persist(self, *, confirmation=None, db=None, authority=None, request=None, provider=None, anchor=None, failpoint=None):
        preview=self.preview(db=db,authority=authority,request=request,provider=provider,anchor=anchor)
        return persist_accepted_relation(authority or self.authority,request or self.request,
            discovery_provider=provider or self.provider,sidecar_path=db or self.db,backup_dir=self.backups,
            execute=True,test_mode=True,confirm_persistence_id=confirmation or preview["persistence_id"],
            external_anchor=anchor,_failpoint=failpoint)

    def counts(self, db=None):
        conn=sqlite3.connect(db or self.db)
        try: return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("accepted_relation_versions","persistence_receipts"))
        finally: conn.close()

    def test_preview_confirmation_and_idempotency(self):
        preview=self.preview(); self.assertEqual((0,0),self.counts())
        with self.assertRaises(ControlledPersistenceError): self.persist(confirmation="persistence_"+"0"*64)
        first=self.persist(); retry=self.persist(confirmation=first["persistence_id"])
        self.assertEqual("persisted",first["status"]); self.assertTrue(retry["idempotent"]); self.assertEqual((1,1),self.counts())

    def test_old_discovery_source_invalidates_confirmation(self):
        old=self.preview(); _TestCurrentDiscoveryFactory.replace(self.discovery_root,_build(config="later",text_suffix=" changed"))
        with self.assertRaises((ControlledPersistenceError,ValueError)):
            self.persist(confirmation=old["persistence_id"])
        self.assertEqual((0,0),self.counts())

    def test_confirmation_is_bound_to_unique_knowledge_store(self):
        other=self.root/"other.db"; initialize_test_knowledge_sidecar(other); TestKnowledgeGraphRepository(other).append_build(self.build,generated_at="2026-01-01T00:00:00Z")
        first=self.preview(); second=self.preview(db=other)
        self.assertNotEqual(first["knowledge_store_id"],second["knowledge_store_id"])
        self.assertNotEqual(first["persistence_id"],second["persistence_id"])
        with self.assertRaises(ControlledPersistenceError): self.persist(db=other,confirmation=first["persistence_id"])
        self.assertEqual((0,0),self.counts(other))

    def test_nonaccept_and_superseded_decisions_are_refused(self):
        discovery=self.provider.rebuild()
        for decision_name in ("reject","defer"):
            root=self.root/f"authority-{decision_name}"; root.mkdir(); authority=_TestReviewAuthorityFactory.initialize(root)
            plan=prepare_review_plan(discovery,self.candidate.candidate_id,authority=authority).plan
            decision=decide_relation_candidate(discovery,plan,decision=decision_name,reason_code="reviewed",
                reviewer_identity="operator@example.test",confirmation_fields=required_confirmation_fields(plan,decision_name),authority=authority)
            request=PersistenceRequest(self.candidate.candidate_id,plan.review_plan_id,decision["decision_id"])
            with self.assertRaises((ControlledPersistenceError,ValueError)): self.preview(authority=authority,request=request)

        first,second=_build(evidence="e1"),_build(evidence="e2")
        combined=MemoryGraphBuild(first.schema_version,first.generator_config_hash,first.nodes,(first.edges[0],second.edges[0]),())
        source_root=self.root/"replacement-source"; source_root.mkdir(); provider=_TestCurrentDiscoveryFactory.initialize(source_root,combined)
        authority_root=self.root/"replacement-authority"; authority_root.mkdir(); authority=_TestReviewAuthorityFactory.initialize(authority_root)
        current=provider.rebuild(); original,replacement=current.candidates
        original_plan=prepare_review_plan(current,original.candidate_id,authority=authority).plan
        replacement_plan=prepare_review_plan(current,replacement.candidate_id,authority=authority).plan
        decision=decide_relation_candidate(current,original_plan,decision="replace",reason_code="replacement_preferred",
            reviewer_identity="operator@example.test",confirmation_fields=required_confirmation_fields(original_plan,"replace",replacement_candidate=replacement,replacement_plan=replacement_plan),
            authority=authority,replacement_candidate_id=replacement.candidate_id,replacement_plan=replacement_plan)
        request=PersistenceRequest(original.candidate_id,original_plan.review_plan_id,decision["decision_id"])
        with self.assertRaises((ControlledPersistenceError,ValueError)): self.preview(authority=authority,request=request,provider=provider)

    def test_different_accepts_cannot_duplicate_active_edge(self):
        first,second=_build(evidence="e1"),_build(evidence="e2")
        combined=MemoryGraphBuild(first.schema_version,first.generator_config_hash,first.nodes,(first.edges[0],second.edges[0]),())
        source_root=self.root/"duplicate-source"; source_root.mkdir(); provider=_TestCurrentDiscoveryFactory.initialize(source_root,combined)
        authority_root=self.root/"duplicate-authority"; authority_root.mkdir(); authority=_TestReviewAuthorityFactory.initialize(authority_root)
        db=self.root/"duplicate.db"; initialize_test_knowledge_sidecar(db); TestKnowledgeGraphRepository(db).append_build(combined,generated_at="2026-01-01T00:00:00Z")
        requests=[]; discovery=provider.rebuild()
        for candidate in discovery.candidates:
            plan=prepare_review_plan(discovery,candidate.candidate_id,authority=authority).plan
            decision=decide_relation_candidate(discovery,plan,decision="accept",reason_code="reviewed",reviewer_identity="operator@example.test",
                confirmation_fields=required_confirmation_fields(plan,"accept"),authority=authority)
            requests.append(PersistenceRequest(candidate.candidate_id,plan.review_plan_id,decision["decision_id"]))
        self.persist(db=db,authority=authority,request=requests[0],provider=provider)
        with self.assertRaises(ControlledPersistenceError): self.persist(db=db,authority=authority,request=requests[1],provider=provider)
        self.assertEqual((1,1),self.counts(db))

    def test_authority_mutation_is_excluded_until_commit(self):
        import memory.controlled_knowledge_persistence as gateway
        entered=threading.Event(); mutation_done=threading.Event(); original=gateway._insert_transaction
        newer=_TestCurrentDiscoveryFactory.replace(self.discovery_root,self.build).rebuild()
        def delayed(*args,**kwargs): entered.set(); time.sleep(.3); return original(*args,**kwargs)
        def mutate():
            entered.wait(); alternative=_build(config="authority-later")
            alt_root=self.root/"alt-discovery"; alt_root.mkdir(); alt=_TestCurrentDiscoveryFactory.initialize(alt_root,alternative).rebuild()
            prepare_review_plan(alt,alt.candidates[0].candidate_id,authority=_TestReviewAuthorityFactory.open(self.authority_root)); mutation_done.set()
        worker=threading.Thread(target=mutate); worker.start()
        with patch.object(gateway,"_insert_transaction",side_effect=delayed):
            result=self.persist(); self.assertFalse(mutation_done.is_set())
        worker.join(5); self.assertTrue(mutation_done.is_set()); self.assertTrue(result["persisted"])
        conn=sqlite3.connect(self.db)
        try: receipt_head=conn.execute("SELECT authority_mutation_seq FROM persistence_receipts").fetchone()[0]
        finally: conn.close()
        self.assertEqual(2,receipt_head)

    def test_authority_change_after_preview_invalidates_confirmation(self):
        preview=self.preview()
        alt_root=self.root/"changed-authority-discovery"; alt_root.mkdir()
        alternate=_TestCurrentDiscoveryFactory.initialize(alt_root,_build(config="changed-head")).rebuild()
        prepare_review_plan(alternate,alternate.candidates[0].candidate_id,authority=self.authority)
        with self.assertRaises(ControlledPersistenceError): self.persist(confirmation=preview["persistence_id"])
        self.assertEqual((0,0),self.counts())

    def test_restore_and_persist_share_coordination_lock(self):
        result=self.persist(); restore_preview=restore_test_sidecar(self.db,result["backup_manifest"])
        restore_test_sidecar(self.db,result["backup_manifest"],execute=True,confirm_restore_id=restore_preview["restore_id"])
        import memory.controlled_knowledge_persistence as gateway
        entered=threading.Event(); release=threading.Event(); original=gateway._insert_transaction
        def delayed(*args,**kwargs): entered.set(); release.wait(3); return original(*args,**kwargs)
        errors=[]
        def writer():
            try: self.persist()
            except Exception as exc: errors.append(exc)
        with patch.object(gateway,"_insert_transaction",side_effect=delayed):
            worker=threading.Thread(target=writer); worker.start(); entered.wait(3)
            with self.assertRaises(ControlledPersistenceError): restore_test_sidecar(self.db,result["backup_manifest"])
            release.set(); worker.join(5)
        self.assertFalse(errors)

    def test_exception_interruptions_and_restore(self):
        for failpoint,committed in (("after_backup",False),("before_commit",False),("after_commit",True)):
            with self.subTest(failpoint=failpoint):
                with self.assertRaises(SimulatedInterruption): self.persist(failpoint=failpoint)
                self.assertEqual((1,1) if committed else (0,0),self.counts())
                recovered=self.persist(); self.assertTrue(recovered["persisted"])
                if failpoint!="after_commit":
                    manifest=sorted(self.backups.glob("*.manifest.json"))[0]
                    auth=restore_test_sidecar(self.db,manifest); restore_test_sidecar(self.db,manifest,execute=True,confirm_restore_id=auth["restore_id"])
                    self.assertEqual((0,0),self.counts())
                else: break

    def test_hard_process_interruptions_recover(self):
        for failpoint,exit_code,committed in (("hard_before_commit",91,False),("hard_after_commit",92,True)):
            with self.subTest(failpoint=failpoint):
                preview=self.preview()
                code=(
                    "from memory.relation_review_state import _TestReviewAuthorityFactory;"
                    "from memory.current_relation_discovery import _TestCurrentDiscoveryFactory;"
                    "from memory.controlled_knowledge_persistence import PersistenceRequest,persist_accepted_relation;"
                    f"a=_TestReviewAuthorityFactory.open({str(self.authority_root)!r});"
                    f"p=_TestCurrentDiscoveryFactory.open({str(self.discovery_root)!r});"
                    f"r=PersistenceRequest({self.request.candidate_id!r},{self.request.review_plan_id!r},{self.request.decision_id!r});"
                    f"persist_accepted_relation(a,r,discovery_provider=p,sidecar_path={str(self.db)!r},backup_dir={str(self.backups)!r},execute=True,test_mode=True,confirm_persistence_id={preview['persistence_id']!r},_failpoint={failpoint!r})"
                )
                proc=subprocess.run([sys.executable,"-c",code],cwd=PROJECT_ROOT,check=False)
                self.assertEqual(exit_code,proc.returncode); self.assertEqual((1,1) if committed else (0,0),self.counts())
                retry=self.persist(confirmation=preview["persistence_id"]); self.assertTrue(retry["persisted"])
                if not committed:
                    manifest=sorted(self.backups.glob("*.manifest.json"))[0]; auth=restore_test_sidecar(self.db,manifest)
                    restore_test_sidecar(self.db,manifest,execute=True,confirm_restore_id=auth["restore_id"])
                else: break

    def test_hard_restore_interruption_rolls_back_and_retries(self):
        result=self.persist(); authorization=restore_test_sidecar(self.db,result["backup_manifest"])
        code=(
            "from memory.controlled_knowledge_persistence import restore_test_sidecar;"
            f"restore_test_sidecar({str(self.db)!r},{result['backup_manifest']!r},execute=True,"
            f"confirm_restore_id={authorization['restore_id']!r},_failpoint='hard_during_restore')"
        )
        proc=subprocess.run([sys.executable,"-c",code],cwd=PROJECT_ROOT,check=False)
        self.assertEqual(93,proc.returncode); self.assertEqual((1,1),self.counts())
        conn=sqlite3.connect(self.db)
        try: self.assertEqual("ok",conn.execute("PRAGMA quick_check").fetchone()[0])
        finally: conn.close()
        retry=restore_test_sidecar(self.db,result["backup_manifest"])
        restore_test_sidecar(self.db,result["backup_manifest"],execute=True,confirm_restore_id=retry["restore_id"])
        self.assertEqual((0,0),self.counts())
        self.db.unlink(); self.assertFalse(self.db.exists())

    def test_anchor_receipt_is_complete_and_production_ineligible(self):
        with self.assertRaises(ControlledPersistenceError): self.preview(anchor=_Anchor(False))
        result=self.persist(anchor=_Anchor(True)); self.assertFalse(result["production_eligible"])
        conn=sqlite3.connect(self.db)
        try: row=conn.execute("SELECT external_anchor_enabled,external_anchor_satisfied,external_anchor_identity,external_anchor_assertion_digest,production_eligible FROM persistence_receipts").fetchone()
        finally: conn.close()
        self.assertEqual((1,1,"anchor:test","assertion_"+"a"*64,0),row)

    def test_schema_tampering_and_production_paths_are_refused(self):
        conn=sqlite3.connect(self.db); conn.execute("DROP TRIGGER persistence_receipts_no_delete"); conn.commit(); conn.close()
        with self.assertRaises(ControlledPersistenceError): self.preview()
        self.assertFalse(production_enablement_conditions()["production_enabled"])
        before=PRODUCTION_KNOWLEDGE_SIDECAR.exists()
        with self.assertRaises(ControlledPersistenceError):
            persist_accepted_relation(self.authority,self.request,discovery_provider=self.provider,
                sidecar_path=PRODUCTION_KNOWLEDGE_SIDECAR,backup_dir=self.backups,execute=True,test_mode=False)
        self.assertEqual(before,PRODUCTION_KNOWLEDGE_SIDECAR.exists())


class ControlledPersistenceCliTests(unittest.TestCase):
    def test_cli_is_read_only_and_persist_is_sealed(self):
        command=[sys.executable,str(PROJECT_ROOT/"tools"/"persist_knowledge_relation.py")]
        status=subprocess.run(command+["status"],cwd=PROJECT_ROOT,text=True,capture_output=True,check=False)
        self.assertEqual(0,status.returncode); self.assertTrue(json.loads(status.stdout)["readonly"])
        refused=subprocess.run(command+["persist","--candidate-id","candidate_x","--review-plan-id","plan_x","--decision-id","decision_x"],cwd=PROJECT_ROOT,text=True,capture_output=True,check=False)
        self.assertEqual(2,refused.returncode); self.assertEqual("production_enablement_incomplete",json.loads(refused.stdout)["reason_code"])


if __name__=="__main__": unittest.main()
