from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.semantic_memory import semantic_summary_search_preview


TEST_PROVIDER_WARNING = (
    "deterministic_test provider only validates the pipeline and does not represent "
    "production semantic quality."
)
LOCAL_PROVIDER_WARNING = "local embedding evaluation does not enable runtime semantic retrieval."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only semantic summary search preview.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--memory-space")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=float)
    parser.add_argument("--provider", default="deterministic_test")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = semantic_summary_search_preview(
            args.query,
            args.db,
            memory_space=args.memory_space,
            top_k=args.top_k,
            min_score=args.min_score,
            provider_name=args.provider,
        )
    except ValueError as exc:
        print(json.dumps({"readonly": True, "status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2

    provider_is_test_only = args.provider == "deterministic_test"
    warning = TEST_PROVIDER_WARNING if provider_is_test_only else LOCAL_PROVIDER_WARNING
    payload = {
        "readonly": True,
        "provider_is_test_only": provider_is_test_only,
        "warning": warning,
        **result,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"readonly=true")
        print(f"provider={payload['provider']}")
        print(f"provider_is_test_only=true")
        print(f"warning={warning}")
        print(f"candidate_count={payload['candidate_count']}")
        print(f"result_count={payload['result_count']}")
        for item in payload["results"]:
            print(
                "rank={rank} score={score:.6f} source_id={source_id} memory_space={memory_space} "
                "content_preview={content_preview}".format(**item)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
