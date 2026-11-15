"""Read-only identity and relationship preview for the YunOS v6.0 memory graph."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.knowledge_sidecar import production_knowledge_status
from memory.memory_graph import DEFAULT_MIN_SCORE, build_memory_graph
from memory.semantic_memory import load_embedding_records
from memory.summary_repository import graph_summary_sources


REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
MEMORY_SIDECAR_PATH = PROJECT_ROOT / "data" / "memory" / "ayunos_memory.db"
SEMANTIC_SIDECAR_PATH = PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db"
RELATION_TYPES = (
    "belongs_to_conversation",
    "belongs_to_memory_space",
    "newer_version_of",
    "semantic_related",
)
MAX_LIMIT = 20


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview evidence-backed memory graph identities without writing any database or file."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--conversation-id")
    parser.add_argument("--memory-space")
    parser.add_argument("--relation-type", choices=RELATION_TYPES)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--include-legacy", action="store_true")
    return parser.parse_args(argv)


def build_preview(
    *,
    source_db: str | Path = REAL_DB_PATH,
    memory_sidecar: str | Path = MEMORY_SIDECAR_PATH,
    semantic_sidecar: str | Path = SEMANTIC_SIDECAR_PATH,
    conversation_id: str | None = None,
    memory_space: str | None = None,
    relation_type: str | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    include_legacy: bool = False,
    limit: int = MAX_LIMIT,
) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit)
    if relation_type is not None and relation_type not in RELATION_TYPES:
        raise ValueError("relation_type is unsupported")
    summaries = graph_summary_sources(
        memory_sidecar,
        source_db,
        memory_space=memory_space,
        conversation_id=conversation_id,
        include_legacy=include_legacy,
    )
    embeddings: list[dict[str, Any]] = []
    for source_table in ("memory_summaries", "summary_versions"):
        embeddings.extend(load_embedding_records(
            semantic_sidecar,
            memory_space=memory_space,
            source_table=source_table,
            limit=1000,
        ))
    build = build_memory_graph(summaries, embeddings, min_score=min_score)
    edges = [
        edge for edge in build.edges
        if relation_type is None or edge.relation_type == relation_type
    ]
    unavailable = [
        item for item in build.unavailable
        if relation_type is None or item.relation_type == relation_type
    ]
    return {
        "schema_version": build.schema_version,
        "generated_at": _utc_now(),
        "readonly": True,
        "executed": False,
        "generator_config_hash": build.generator_config_hash,
        "filters": {
            "conversation_id": conversation_id,
            "memory_space": memory_space,
            "relation_type": relation_type,
            "min_score": float(min_score),
            "include_legacy": bool(include_legacy),
            "limit": bounded_limit,
        },
        "node_counts": build.node_counts(),
        "edge_counts": dict(sorted(Counter(edge.relation_type for edge in edges).items())),
        "edge_status_counts": dict(sorted(Counter(edge.status for edge in edges).items())),
        "unavailable_counts": dict(sorted(Counter(item.reason for item in unavailable).items())),
        "knowledge_sidecar_status": production_knowledge_status(),
        "nodes": [node.public_dict() for node in build.nodes[:bounded_limit]],
        "edges": [edge.public_dict() for edge in edges[:bounded_limit]],
        "unavailable": [item.public_dict() for item in unavailable[:bounded_limit]],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = build_preview(
            conversation_id=args.conversation_id,
            memory_space=args.memory_space,
            relation_type=args.relation_type,
            min_score=args.min_score,
            include_legacy=args.include_legacy,
            limit=args.limit,
        )
    except Exception:
        payload = {
            "schema_version": "v6.0",
            "readonly": True,
            "executed": False,
            "status": "blocked",
            "reason": "memory graph preview is unavailable",
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text(payload)
    return 0


def _print_text(payload: dict[str, Any]) -> None:
    print("readonly=true")
    print("executed=false")
    print(f"schema_version={payload['schema_version']}")
    print(f"node_counts={json.dumps(payload['node_counts'], ensure_ascii=True, sort_keys=True)}")
    print(f"edge_counts={json.dumps(payload['edge_counts'], ensure_ascii=True, sort_keys=True)}")
    print(f"unavailable_counts={json.dumps(payload['unavailable_counts'], ensure_ascii=True, sort_keys=True)}")
    for node in payload["nodes"]:
        print("node=" + json.dumps(node, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    for edge in payload["edges"]:
        print("edge=" + json.dumps(edge, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    for item in payload["unavailable"]:
        print("unavailable=" + json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _bounded_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_LIMIT
    return max(1, min(parsed, MAX_LIMIT))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
