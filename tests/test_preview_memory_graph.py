from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.knowledge_sidecar import PRODUCTION_KNOWLEDGE_SIDECAR
from memory.semantic_memory import build_embedding_record, insert_embedding_record
from memory.summary_repository import graph_summary_sources
from tools.preview_memory_graph import _print_text, build_preview, parse_args


SEMANTIC_SCHEMA = """
CREATE TABLE memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    memory_space TEXT NOT NULL,
    source_platform TEXT,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    embedding_protocol TEXT NOT NULL,
    embedding_normalized INTEGER NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_preview TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT
);
"""


class PreviewMemoryGraphTest(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        source = root / "source.db"
        sidecar = root / "absent-memory.db"
        semantic = root / "semantic.db"
        conn = sqlite3.connect(source)
        try:
            conn.executescript(
                """
                CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT);
                CREATE TABLE messages (
                    message_id TEXT, conversation_id TEXT, role TEXT,
                    text TEXT, created_at TEXT, memory_space TEXT
                );
                CREATE TABLE memory_summaries (
                    id INTEGER PRIMARY KEY, conversation_id TEXT, memory_space TEXT,
                    summary_type TEXT, summary_text TEXT, created_at TEXT
                );
                """
            )
            conn.executemany("INSERT INTO conversations VALUES (?, ?)", [("c1", "whale"), ("c2", "general")])
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("m1", "c1", "user", "private source one", "2026-01-01", "whale"),
                    ("m2", "c2", "user", "private source two", "2026-01-01", "general"),
                ],
            )
            conn.executemany(
                "INSERT INTO memory_summaries VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "c1", "whale", "conversation_summary", "TOP_SECRET_SUMMARY", "2026-01-02"),
                    (2, "c2", "general", "conversation_summary", "TOP_SECRET_SUMMARY", "2026-01-02"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        conn = sqlite3.connect(semantic)
        try:
            conn.executescript(SEMANTIC_SCHEMA)
            for summary in graph_summary_sources(sidecar, source):
                metadata = {
                    "summary_source": summary["summary_source"],
                    "summary_id": summary["source_id"],
                    "conversation_id": summary["conversation_id"],
                    "summary_version": summary["summary_version"],
                    "summary_text_hash": summary["summary_text_hash"],
                    "source_snapshot_hash": summary["source_snapshot_hash"],
                    "memory_space": summary["memory_space"],
                    "semantic_target_kind": "summary_embedding",
                    "summary_type": "conversation_summary",
                }
                insert_embedding_record(conn, build_embedding_record(
                    summary["source_table"], summary["source_id"], summary["summary_text"],
                    memory_space=summary["memory_space"], provider_name="deterministic_test",
                    metadata=metadata,
                ))
            conn.commit()
        finally:
            conn.close()
        return source, sidecar, semantic

    def test_preview_is_path_body_embedding_and_write_free(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, sidecar, semantic = self._fixture(root)
            knowledge_before = PRODUCTION_KNOWLEDGE_SIDECAR.exists()
            payload = build_preview(
                source_db=source,
                memory_sidecar=sidecar,
                semantic_sidecar=semantic,
                limit=1,
            )
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertTrue(payload["readonly"])
            self.assertFalse(payload["executed"])
            self.assertNotIn("TOP_SECRET_SUMMARY", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("embedding_json", serialized)
            self.assertNotIn("content_preview", serialized)
            self.assertLessEqual(len(payload["nodes"]), 1)
            self.assertLessEqual(len(payload["edges"]), 1)
            self.assertLessEqual(len(payload["unavailable"]), 1)
            self.assertFalse(sidecar.exists())
            self.assertEqual(knowledge_before, PRODUCTION_KNOWLEDGE_SIDECAR.exists())

    def test_conversation_memory_space_relation_and_limit_filters_apply(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source, sidecar, semantic = self._fixture(Path(tmp))
            by_conversation = build_preview(
                source_db=source, memory_sidecar=sidecar, semantic_sidecar=semantic,
                conversation_id="c1", limit=50,
            )
            self.assertEqual(1, by_conversation["node_counts"]["summary"])
            self.assertEqual(20, by_conversation["filters"]["limit"])
            self.assertTrue(all(
                node["conversation_id"] in {"c1", None}
                for node in by_conversation["nodes"]
            ))

            by_space = build_preview(
                source_db=source, memory_sidecar=sidecar, semantic_sidecar=semantic,
                memory_space="general",
            )
            self.assertEqual(1, by_space["node_counts"]["summary"])
            self.assertTrue(all(
                node["memory_space"] in {"general", None}
                for node in by_space["nodes"]
            ))

            semantic_only = build_preview(
                source_db=source, memory_sidecar=sidecar, semantic_sidecar=semantic,
                relation_type="semantic_related", min_score=0.5,
            )
            self.assertEqual({"semantic_related"}, set(semantic_only["edge_counts"]))
            self.assertTrue(all(edge["relation_type"] == "semantic_related" for edge in semantic_only["edges"]))

    def test_text_formatter_uses_only_safe_payload(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, sidecar, semantic = self._fixture(root)
            payload = build_preview(source_db=source, memory_sidecar=sidecar, semantic_sidecar=semantic)
            output = io.StringIO()
            with redirect_stdout(output):
                _print_text(payload)
            rendered = output.getvalue()
            self.assertNotIn("TOP_SECRET_SUMMARY", rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("embedding_json", rendered)

    def test_write_and_accept_flags_do_not_exist(self):
        for forbidden in ("--execute", "--write", "--accept"):
            with self.subTest(flag=forbidden):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_args([forbidden])


if __name__ == "__main__":
    unittest.main()
