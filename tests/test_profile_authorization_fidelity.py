from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unicodedata
import unittest

from memory.approved_profile_control import PRODUCTION_PROFILE_SIDECAR
from memory.evidence_selection import EvidenceSelectionService
from memory.graph_enhanced_retrieval import GraphEnhancedRetrievalService, RetrievalQuery
from memory.memory_package import MemoryPackageService
from memory.profile_authorization_fidelity import (
    AuthorizationFidelityError,
    BATCH_RECEIPT_VERSION,
    CONTROL_VERSION,
    MANIFEST_VERSION,
    ProfileAuthorizationFidelityService,
    _AuthorizationFidelityTestCore,
    build_ascii_authorization_manifest,
    manifest_capability_state,
)
from memory.profile_enablement import (
    ProfileEnablementIdempotencyConflict,
    active_profile_enablement_projection,
    initialize_profile_enablement_sidecar,
)
from memory.temporal_memory_candidates import _TemporalCandidateCore


TEXTS = (
    "\u5728\u9879\u76ee\u8ba8\u8bba\u4e2d\uff0c\u5982\u679c\u7f3a\u5931\u4fe1\u606f\u53ef\u80fd\u5f71\u54cd\u65b9\u6848\u3001\u67b6\u6784\u6216\u51b3\u7b56\uff0c\u5e94\u4e3b\u52a8\u6307\u51fa\u5e76\u63d0\u95ee\uff0c\u4e0d\u8981\u9ed8\u8ba4\u5047\u8bbe\u3002",
    "\u56de\u7b54\u9ed8\u8ba4\u5148\u7ed9\u7ed3\u8bba\uff0c\u518d\u6839\u636e\u95ee\u9898\u590d\u6742\u5ea6\u5c55\u5f00\uff1b\u5de5\u7a0b\u6267\u884c\u65f6\u4fdd\u6301\u4e25\u683c\u3001\u7cbe\u786e\uff0c\u907f\u514d\u65e0\u5173\u5bd2\u6684\u3002",
    "YunOS \u8d1f\u8d23\u8bb0\u5fc6\u5de5\u7a0b\u4e0e\u4e0a\u4e0b\u6587\u63a7\u5236\uff0cLLM \u8d1f\u8d23\u7406\u89e3\u548c\u8868\u8fbe\uff1b\u957f\u671f\u753b\u50cf\u5fc5\u987b\u900f\u660e\uff0c\u5e76\u7ecf\u7528\u6237\u6279\u51c6\u540e\u624d\u80fd\u8fdb\u5165\u6a21\u578b\u4e0a\u4e0b\u6587\u3002",
    "\u5f53\u524d\u4e3b\u8981\u76ee\u6807\u662f\u5b8c\u6210 YunOS \u7684\u751f\u4ea7\u542f\u7528\u3001\u9996\u6b21\u53ef\u5ba1\u8ba1 DeepSeek \u8c03\u7528\u53ca\u7ea6 20 \u9898 dogfooding \u95ed\u73af\u3002",
)


def _envelope_values(envelope: str) -> tuple[str, list[str | None]]:
    value = json.loads(envelope)
    return value["manifest_identity"], value["item_text_utf8_sha256"]


class AuthorizationFidelityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.sidecar = Path(self.temp.name) / "profile-d2-test.db"
        initialize_profile_enablement_sidecar(self.sidecar, execute=True, test_mode=True)
        self.core = _AuthorizationFidelityTestCore(self.sidecar)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def b1_items(self) -> list[dict]:
        kinds = ("preference", "preference", "decision", "goal")
        return [
            {
                "text": text,
                "proposal_type": kinds[index - 1],
                "source_anchors": [],
                "user_identity": "local_user",
                "expected_revision": 0,
                "request_id": f"d2-b1-request-{index:02d}",
                "mutation_id": f"d2-b1-mutation-{index:02d}",
            }
            for index, text in enumerate(TEXTS, start=1)
        ]

    def authorize(self, envelope: str):
        identity, hashes = _envelope_values(envelope)
        return self.core.authorize(
            envelope, manifest_identity=identity, item_text_utf8_sha256=hashes,
        )

    def execute_b1(self, items: list[dict] | None = None, **kwargs):
        envelope = build_ascii_authorization_manifest("B1", items or self.b1_items())
        return envelope, self.core.execute_b1(self.authorize(envelope), **kwargs)

    def counts(self) -> tuple[int, int, int, int, int]:
        with sqlite3.connect(self.sidecar) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            overlay = (
                conn.execute("SELECT COUNT(*) FROM d2_authorization_manifests").fetchone()[0]
                if "d2_authorization_manifests" in tables else 0
            )
            return (
                conn.execute("SELECT COUNT(*) FROM direct_profile_proposals").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM direct_profile_proposal_receipts").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM profile_enablement_decisions").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM profile_enablement_receipts").fetchone()[0],
                overlay,
            )

    def test_ascii_manifest_preserves_exact_chinese_codepoints_atomically(self) -> None:
        envelope, receipt = self.execute_b1()
        self.assertTrue(envelope.isascii())
        self.assertEqual(MANIFEST_VERSION, json.loads(base64.b64decode(
            json.loads(envelope)["canonical_manifest_base64"]
        ))["manifest_version"])
        self.assertEqual(BATCH_RECEIPT_VERSION, receipt["receipt_version"])
        self.assertEqual(4, len(receipt["results"]))
        with sqlite3.connect(self.sidecar) as conn:
            stored = [row[0] for row in conn.execute(
                "SELECT original_text FROM direct_profile_proposals ORDER BY sequence"
            )]
        self.assertEqual(list(TEXTS), stored)
        self.assertEqual(
            [[ord(char) for char in text] for text in TEXTS],
            [[ord(char) for char in text] for text in stored],
        )
        projection = active_profile_enablement_projection(self.sidecar)
        self.assertEqual(0, projection["revision"])
        self.assertEqual([], projection["items"])
        self.assertEqual((4, 4, 0, 0, 1), self.counts())

    def test_powershell_question_mark_replacement_is_rejected_before_append(self) -> None:
        envelope = build_ascii_authorization_manifest("B1", self.b1_items())
        capability = self.authorize(envelope)
        with self.assertRaisesRegex(AuthorizationFidelityError, "runtime proposal text"):
            self.core.execute_b1(
                capability,
                runtime_text_transform=lambda text, _ordinal: "".join(
                    "?" if ord(char) > 127 else char for char in text
                ),
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_runtime_text_differs_despite_valid_manifest_is_rejected(self) -> None:
        envelope = build_ascii_authorization_manifest("B1", self.b1_items())
        with self.assertRaises(AuthorizationFidelityError):
            self.core.execute_b1(
                self.authorize(envelope),
                runtime_text_transform=lambda text, ordinal: text + "!" if ordinal == 2 else text,
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_utf8_hash_tamper_is_rejected_before_capability_issue(self) -> None:
        envelope = json.loads(build_ascii_authorization_manifest("B1", self.b1_items()))
        payload = json.loads(base64.b64decode(envelope["canonical_manifest_base64"]))
        payload["items"][2]["text"]["utf8_sha256"] = "0" * 64
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        envelope["canonical_manifest_base64"] = base64.b64encode(canonical).decode("ascii")
        envelope["manifest_identity"] = "paf_" + hashlib.sha256(canonical).hexdigest()
        envelope["item_text_utf8_sha256"][2] = "0" * 64
        tampered = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        with self.assertRaisesRegex(AuthorizationFidelityError, "UTF-8 fidelity"):
            self.authorize(tampered)
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_normalization_and_punctuation_changes_are_rejected(self) -> None:
        items = self.b1_items()
        items[0]["text"] = "Caf\u00e9\uff0c\u4e2d\u6587\u3002"
        envelope = build_ascii_authorization_manifest("B1", items)
        with self.assertRaises(AuthorizationFidelityError):
            self.core.execute_b1(
                self.authorize(envelope),
                runtime_text_transform=lambda text, ordinal: (
                    unicodedata.normalize("NFD", text) if ordinal == 1 else text
                ),
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())
        envelope = build_ascii_authorization_manifest("B1", self.b1_items())
        with self.assertRaises(AuthorizationFidelityError):
            self.core.execute_b1(
                self.authorize(envelope),
                runtime_text_transform=lambda text, ordinal: (
                    text.replace("\uff0c", ",", 1) if ordinal == 1 else text
                ),
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_one_damaged_item_and_readback_mismatch_roll_back_whole_batch(self) -> None:
        envelope = build_ascii_authorization_manifest("B1", self.b1_items())
        with self.assertRaises(AuthorizationFidelityError):
            self.core.execute_b1(
                self.authorize(envelope),
                runtime_text_transform=lambda text, ordinal: "?" if ordinal == 4 else text,
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())
        with self.assertRaisesRegex(AuthorizationFidelityError, "read-back"):
            self.core.execute_b1(
                self.authorize(envelope),
                readback_transform=lambda text, ordinal: text + "?" if ordinal == 4 else text,
            )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_same_manifest_replays_immutable_receipt_and_changed_manifest_conflicts(self) -> None:
        envelope, first = self.execute_b1()
        second = self.core.execute_b1(self.authorize(envelope))
        self.assertEqual(first, second)
        self.assertEqual((4, 4, 0, 0, 1), self.counts())
        changed = self.b1_items()
        changed[0]["text"] += "!"
        changed_envelope = build_ascii_authorization_manifest("B1", changed)
        with self.assertRaises(ProfileEnablementIdempotencyConflict):
            self.core.execute_b1(self.authorize(changed_envelope))
        self.assertEqual((4, 4, 0, 0, 1), self.counts())

    def test_b2_edit_manifest_rejects_encoding_drift_and_preserves_original(self) -> None:
        _envelope, b1_receipt = self.execute_b1()
        proposal_id = b1_receipt["results"][0]["proposal_id"]
        final_text = "\u7cbe\u786e\u4fdd\u7559\u7f16\u8f91\u540e\u7684\u4e2d\u6587\u6807\u70b9\u3002"
        item = {
            "subject_origin": "direct_user_proposal",
            "subject_id": proposal_id,
            "action": "edit_and_approve",
            "expected_revision": 0,
            "request_id": "d2-b2-edit-request-01",
            "mutation_id": "d2-b2-edit-mutation-01",
            "reason": "",
            "edited_text": final_text,
            "query_text": "",
            "budget_profile": "",
            "package_identity": None,
        }
        envelope = build_ascii_authorization_manifest("B2", [item])
        with self.assertRaises(AuthorizationFidelityError):
            self.core.execute_b2(
                self.authorize(envelope),
                runtime_text_transform=lambda _text, _ordinal: "????????",
            )
        self.assertEqual((4, 4, 0, 0, 1), self.counts())
        receipt = self.core.execute_b2(self.authorize(envelope))
        self.assertEqual("manual", receipt["decision_source"])
        with sqlite3.connect(self.sidecar) as conn:
            row = conn.execute(
                "SELECT original_text,edited_statement,final_text FROM profile_enablement_decisions"
            ).fetchone()
        self.assertEqual(TEXTS[0], row[0])
        self.assertEqual(final_text, row[1])
        self.assertEqual(final_text, row[2])

    def test_b2_non_edit_binding_and_manifest_replay(self) -> None:
        _envelope, b1_receipt = self.execute_b1()
        proposal_id = b1_receipt["results"][0]["proposal_id"]
        item = {
            "subject_origin": "direct_user_proposal",
            "subject_id": proposal_id,
            "action": "reject",
            "expected_revision": 0,
            "request_id": "d2-b2-reject-request-01",
            "mutation_id": "d2-b2-reject-mutation-01",
            "reason": "authorization_fidelity_unicode_corruption",
            "edited_text": None,
            "query_text": "",
            "budget_profile": "",
            "package_identity": None,
        }
        envelope = build_ascii_authorization_manifest("B2", [item])
        first = self.core.execute_b2(self.authorize(envelope))
        second = self.core.execute_b2(self.authorize(envelope))
        self.assertEqual(first, second)
        changed = dict(item, action="defer")
        with self.assertRaises(ProfileEnablementIdempotencyConflict):
            self.core.execute_b2(
                self.authorize(build_ascii_authorization_manifest("B2", [changed]))
            )

    def test_b2_candidate_manifest_rebuilds_live_source_and_rejects_drift(self) -> None:
        source = Path(self.temp.name) / "candidate-source.db"
        with sqlite3.connect(source) as conn:
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
            conn.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                ("c1", "Candidate", 1735693200.0, "2025-01-01T01:00:00+00:00", 1735693200.0, "2025-01-01T01:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                ("m1", "c1", "Candidate", 1, "user", 1735693200.0, "2025-01-01T01:00:00+00:00", "memory Preference: I prefer tea.", "general"),
            )
        retrieval = GraphEnhancedRetrievalService(
            source,
            Path(self.temp.name) / "semantic-missing.db",
            Path(self.temp.name) / "knowledge-missing.db",
            semantic_searcher=lambda *_args, **_kwargs: {"results": []},
        )
        memory_service = MemoryPackageService(source, retriever=retrieval)
        selection = EvidenceSelectionService(source, memory_package_service=memory_service)
        temporal = _TemporalCandidateCore(source, selection_service=selection)
        builder = temporal.build
        package = builder(RetrievalQuery("memory"), budget_profile="Large")
        candidate = package["candidates"][0]
        core = _AuthorizationFidelityTestCore(self.sidecar, temporal_builder=builder)
        item = {
            "subject_origin": "v7.2_candidate",
            "subject_id": candidate["candidate_id"],
            "action": "reject",
            "expected_revision": 0,
            "request_id": "d2-candidate-request-01",
            "mutation_id": "d2-candidate-mutation-01",
            "reason": "",
            "edited_text": None,
            "query_text": "memory",
            "budget_profile": "Large",
            "package_identity": package["package_identity"],
        }
        envelope = build_ascii_authorization_manifest("B2", [item])
        identity, hashes = _envelope_values(envelope)
        authority = core.authorize(
            envelope, manifest_identity=identity, item_text_utf8_sha256=hashes,
        )
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE messages SET text=? WHERE message_id='m1'", ("memory Preference: I prefer coffee.",))
        with self.assertRaisesRegex(AuthorizationFidelityError, "stale|absent"):
            core.execute_b2(authority)
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_capability_is_noncopyable_one_shot_and_thread_safe(self) -> None:
        envelope = build_ascii_authorization_manifest("B1", self.b1_items())
        capability = self.authorize(envelope)
        with self.assertRaises(AuthorizationFidelityError):
            copy(capability)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.core.execute_b1, capability) for _ in range(2)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result()["status"])
                except AuthorizationFidelityError:
                    outcomes.append("rejected")
        self.assertCountEqual(["proposal_batch_recorded", "rejected"], outcomes)
        self.assertEqual("consumed", manifest_capability_state(capability))

    def test_test_core_cannot_target_production_and_service_accepts_no_raw_text_or_path(self) -> None:
        with self.assertRaises(AuthorizationFidelityError):
            _AuthorizationFidelityTestCore(PRODUCTION_PROFILE_SIDECAR)
        service = ProfileAuthorizationFidelityService()
        with self.assertRaises(TypeError):
            service.execute_b1(authority=object(), raw_text="forbidden")
        self.assertFalse(hasattr(service, "sidecar_path"))

    def test_manifest_version_and_envelope_are_explicit(self) -> None:
        envelope = json.loads(build_ascii_authorization_manifest("B1", self.b1_items()))
        self.assertEqual(CONTROL_VERSION, envelope["envelope_version"])
        self.assertRegex(envelope["manifest_identity"], r"^paf_[0-9a-f]{64}$")
        self.assertEqual(
            [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in TEXTS],
            envelope["item_text_utf8_sha256"],
        )

    def test_reviewed_r2_source_builds_expected_ascii_identity_offline(self) -> None:
        source = Path(__file__).resolve().parents[1] / "docs" / "manifests" / "v8_0_d2_r2_recovery_source.json"
        output = Path(self.temp.name) / "r2-envelope.json"
        result = subprocess.run(
            [
                sys.executable,
                "tools/build_profile_authorization_manifest.py",
                "--input", str(source),
                "--output", str(output),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(output.read_bytes().isascii())
        value = json.loads(output.read_bytes().decode("ascii"))
        self.assertEqual(
            "paf_b8acf79160eaabcb04902eb1ddbd1e577d5f1a59aa4970df098d0739ce6e77a0",
            value["manifest_identity"],
        )
        self.assertEqual(
            [
                "65640cba538dcc30ec3c4278a2e5cd966f8a800d590fe331305f5d420f0717eb",
                "3fc7e233c89a4ded0fe09840b22f4b6902fb5f4882b1219d3afdb984a6f59018",
                "82283dc63feae29f37e30e2c4ef573233f321eb7b77d755810eecd8c83890a3c",
                "73bcc12e06dc0cfe9da406d78f05f871222ccce9a9ae83b5764926753c57a0d8",
            ],
            value["item_text_utf8_sha256"],
        )
        self.assertEqual((0, 0, 0, 0, 0), self.counts())

    def test_reviewed_r1_source_builds_exact_rejection_manifest_offline(self) -> None:
        source = Path(__file__).resolve().parents[1] / "docs" / "manifests" / "v8_0_d2_r1_recovery_source.json"
        output = Path(self.temp.name) / "r1-envelope.json"
        result = subprocess.run(
            [
                sys.executable,
                "tools/build_profile_authorization_manifest.py",
                "--input", str(source),
                "--output", str(output),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(output.read_bytes().decode("ascii"))
        self.assertEqual(
            "paf_2a3f6608e7ce9f0d1f27707dd73006e8e364aaa54e41a0514889d68cd2963ff2",
            value["manifest_identity"],
        )
        self.assertEqual([None, None, None, None], value["item_text_utf8_sha256"])
        self.assertEqual((0, 0, 0, 0, 0), self.counts())


if __name__ == "__main__":
    unittest.main()
