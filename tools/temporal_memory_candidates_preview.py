from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.evidence_selection import EvidenceSelectionValidationError
from memory.graph_enhanced_retrieval import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_GRAPH_DEPTH,
    DEFAULT_LIMIT,
    DEFAULT_PATH_BUDGET,
    DEFAULT_RELATION_BUDGET,
    RetrievalQuery,
    RetrievalValidationError,
)
from memory.memory_package import MemoryPackageValidationError
from memory.temporal_memory_candidates import (
    PROTOCOL_VERSION,
    TemporalCandidateValidationError,
    build_production_temporal_candidates,
    canonical_temporal_candidates_json,
    render_temporal_candidates,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YunOS v7.2 candidate-only temporal preview.")
    parser.add_argument("query")
    parser.add_argument("--budget", choices=("small", "medium", "large"), default="medium")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--result-mode", choices=("snippets", "conversation_anchor"), default="snippets")
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--cursor")
    parser.add_argument("--hit-types", default="keyword,semantic,graph")
    parser.add_argument("--graph-depth", type=int, default=DEFAULT_GRAPH_DEPTH)
    parser.add_argument("--relation-budget", type=int, default=DEFAULT_RELATION_BUDGET)
    parser.add_argument("--path-budget", type=int, default=DEFAULT_PATH_BUDGET)
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET)
    parser.add_argument("--memory-space")
    parser.add_argument("--json", action="store_true", help="emit canonical JSON instead of text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        package = build_production_temporal_candidates(
            RetrievalQuery(
                query=args.query, start_time=args.start_time, end_time=args.end_time,
                result_mode=args.result_mode, context_window=args.context_window,
                limit=args.limit, cursor=args.cursor,
                hit_types=tuple(item.strip() for item in args.hit_types.split(",") if item.strip()),
                graph_depth=args.graph_depth, relation_budget=args.relation_budget,
                path_budget=args.path_budget, context_budget=args.context_budget,
                memory_space=args.memory_space,
            ),
            budget_profile=args.budget,
        )
    except (
        RetrievalValidationError, MemoryPackageValidationError,
        EvidenceSelectionValidationError, TemporalCandidateValidationError,
    ) as exc:
        print(json.dumps({
            "protocol_version": PROTOCOL_VERSION, "status": "unavailable",
            "reason_code": "request_or_interpretation_validation_failed",
            "readonly": True, "persisted": False, "candidate_only": True,
            "error": str(exc),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    sys.stdout.write(
        canonical_temporal_candidates_json(package) + "\n"
        if args.json else render_temporal_candidates(package)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
