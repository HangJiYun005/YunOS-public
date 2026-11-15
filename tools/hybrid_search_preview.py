from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.hybrid_retriever import hybrid_search_preview
from memory.semantic_memory import REAL_DB_PATH, ensure_test_database_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only hybrid keyword and semantic preview.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--semantic-db", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--memory-space", default="whale")
    parser.add_argument("--keyword-top-k", type=int, default=5)
    parser.add_argument("--semantic-top-k", type=int, default=5)
    parser.add_argument("--combined-top-k", type=int, default=8)
    parser.add_argument("--provider", default="local_sentence_transformer")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = Path(args.source_db).expanduser().resolve()
    try:
        ensure_test_database_path(args.semantic_db)
        if source_path == REAL_DB_PATH.resolve() and not args.real_readonly:
            raise ValueError("Real source database requires --real-readonly")
        result = hybrid_search_preview(
            args.query,
            source_path,
            args.semantic_db,
            memory_space=args.memory_space,
            keyword_top_k=args.keyword_top_k,
            semantic_top_k=args.semantic_top_k,
            combined_top_k=args.combined_top_k,
            provider_name=args.provider,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"readonly": True, "status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2

    payload = {"readonly": True, "sent_to_model": False, **result}
    print(json.dumps(payload, ensure_ascii=False if args.json else True, indent=2))
    return 2 if payload.get("status") == "refused" else 0


if __name__ == "__main__":
    raise SystemExit(main())
