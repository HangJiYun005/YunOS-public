from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.context_assembly import ContextAssemblyValidationError, redact_context_text
from memory.production_delivery import (
    ProductionDeliveryValidationError as ContextDeliveryValidationError,
    build_production_delivery as build_production_context_delivery,
    canonical_production_delivery_json as canonical_context_delivery_json,
    render_production_delivery as render_context_delivery,
)
from memory.evidence_selection import EvidenceSelectionValidationError
from memory.graph_enhanced_retrieval import RetrievalQuery, RetrievalValidationError
from memory.memory_package import MemoryPackageValidationError
from memory.temporal_memory_candidates import TemporalCandidateValidationError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview one deterministic, readonly YunOS v8.0-E production Delivery.",
    )
    parser.add_argument("query")
    parser.add_argument("--budget", default="Medium", choices=("Small", "Medium", "Large", "Custom", "small", "medium", "large", "custom"))
    parser.add_argument("--total-characters", type=int)
    parser.add_argument("--estimated-tokens", type=int)
    parser.add_argument("--max-evidence", type=int)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--max-history-expansions", type=int)
    parser.add_argument("--exclude", action="append", default=[], metavar="ITEM_ID")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--result-mode", default="snippets", choices=("citations", "snippets", "summary"))
    parser.add_argument("--context-window", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--hit-types", default="keyword,semantic,graph")
    parser.add_argument("--graph-depth", type=int, default=1)
    parser.add_argument("--relation-budget", type=int, default=24)
    parser.add_argument("--path-budget", type=int, default=12)
    parser.add_argument("--context-budget", type=int, default=6000)
    parser.add_argument("--memory-space")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        query = RetrievalQuery(
            query=args.query,
            start_time=args.start_time,
            end_time=args.end_time,
            result_mode=args.result_mode,
            context_window=args.context_window,
            limit=args.limit,
            hit_types=tuple(item.strip() for item in args.hit_types.split(",") if item.strip()),
            graph_depth=args.graph_depth,
            relation_budget=args.relation_budget,
            path_budget=args.path_budget,
            context_budget=args.context_budget,
            memory_space=args.memory_space,
        )
        delivery = build_production_context_delivery(
            query,
            budget_profile=args.budget,
            total_characters=args.total_characters,
            estimated_tokens=args.estimated_tokens,
            max_evidence=args.max_evidence,
            max_candidates=args.max_candidates,
            max_history_expansions=args.max_history_expansions,
            excluded_item_ids=args.exclude,
        )
    except (
        RetrievalValidationError,
        MemoryPackageValidationError,
        EvidenceSelectionValidationError,
        TemporalCandidateValidationError,
        ContextAssemblyValidationError,
        ContextDeliveryValidationError,
        ValueError,
    ) as exc:
        message = redact_context_text(str(exc))
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2
    print(
        canonical_context_delivery_json(delivery)
        if args.json else render_context_delivery(delivery),
        end="" if args.json else "",
    )
    if args.json:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
