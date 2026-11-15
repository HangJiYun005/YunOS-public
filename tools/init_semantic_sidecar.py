from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import SemanticSidecarPathError, validate_semantic_sidecar_write_target
from memory.semantic_memory import REQUIRED_EMBEDDING_COLUMNS


CREATE_MEMORY_EMBEDDINGS_SQL = """
CREATE TABLE memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL, source_id TEXT NOT NULL,
    memory_space TEXT NOT NULL DEFAULT 'general', source_platform TEXT,
    embedding_provider TEXT NOT NULL, embedding_model TEXT NOT NULL,
    model_revision TEXT NOT NULL, model_fingerprint TEXT NOT NULL,
    embedding_protocol TEXT NOT NULL, embedding_normalized INTEGER NOT NULL,
    embedding_dim INTEGER NOT NULL, embedding_json TEXT NOT NULL,
    content_hash TEXT NOT NULL, content_preview TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT,
    UNIQUE(source_table, source_id, embedding_provider, embedding_model, model_revision,
           model_fingerprint, embedding_protocol, embedding_normalized, content_hash)
)
"""


def init_semantic_sidecar(execute: bool = False, allow_production_sidecar: bool = False, confirm_production_sidecar_write: bool = False) -> dict:
    if not execute:
        return {"status": "dry_run", "written": False}
    if not (allow_production_sidecar and confirm_production_sidecar_write):
        return {"status": "refused", "written": False, "reason": "production sidecar write requires all confirmations"}
    target = validate_semantic_sidecar_write_target(PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db")
    conn = sqlite3.connect(target)
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_embeddings'").fetchone()
        if not exists:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
            conn.commit()
            return {"status": "created", "written": True}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")}
        if REQUIRED_EMBEDDING_COLUMNS.issubset(columns):
            return {"status": "already_ready", "written": False}
        return {"status": "refused", "written": False, "reason": "sidecar schema is incompatible"}
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the approved production semantic sidecar.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-production-sidecar", action="store_true")
    parser.add_argument("--confirm-production-sidecar-write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = init_semantic_sidecar(args.execute, args.allow_production_sidecar, args.confirm_production_sidecar_write)
    except SemanticSidecarPathError as exc:
        result = {"status": "refused", "written": False, "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] in {"dry_run", "created", "already_ready"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
