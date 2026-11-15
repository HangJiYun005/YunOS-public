from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.graph_identity import RELATION_TYPES
from memory.knowledge_graph_query import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_DEPTH,
    MAX_LIMIT,
    MAX_PATH_DEPTH,
    KnowledgeGraphQuery,
    KnowledgeGraphQueryError,
    query_production_knowledge_graph,
)
from memory.knowledge_sidecar import KnowledgeRepositoryError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only YunOS knowledge graph query.")
    parser.add_argument("--conversation-id")
    parser.add_argument("--memory-space")
    parser.add_argument("--relation-type", choices=sorted(RELATION_TYPES))
    parser.add_argument("--node-id")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, choices=range(MAX_PATH_DEPTH + 1))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, choices=range(1, MAX_LIMIT + 1))
    parser.add_argument("--include-candidates", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = query_production_knowledge_graph(KnowledgeGraphQuery(
            conversation_id=args.conversation_id,
            memory_space=args.memory_space,
            relation_type=args.relation_type,
            node_id=args.node_id,
            max_depth=args.max_depth,
            limit=args.limit,
            include_candidates=args.include_candidates,
        ))
    except (KnowledgeGraphQueryError, KnowledgeRepositoryError):
        payload = {
            "protocol_version": "v6.3-query-1", "status": "unavailable",
            "reason_code": "knowledge_query_validation_failed", "readonly": True,
            "executed": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        counts = payload["counts"]
        print(f"status={payload['status']} reason={payload['reason_code']} readonly=true")
        print(
            "formal={formal_relations} deterministic={deterministic_relations} "
            "candidates={candidate_relations} unavailable={unavailable_relations} "
            "paths={paths} memories={related_memories}".format(**counts)
        )
        for relation_class in ("formal", "deterministic", "candidates", "unavailable"):
            for relation in payload["relations"][relation_class]:
                print(
                    f"{relation_class}: {relation['relation_type']} "
                    f"{relation['source_node_id']} -> {relation['target_node_id']} "
                    f"evidence={relation['evidence_identity']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
