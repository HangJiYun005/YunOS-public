from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.semantic_memory import (
    build_embedding_record,
    rank_by_semantic_similarity,
    save_embedding_record,
    semantic_search_records,
)
from memory.embedding_provider import get_embedding_provider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Semantic memory dry-run helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    embed = subparsers.add_parser("embed", help="Build one embedding record without writing by default.")
    embed.add_argument("--text", required=True)
    embed.add_argument("--source-table", default="manual")
    embed.add_argument("--source-id", default="dry-run")
    embed.add_argument("--memory-space", default="general")
    embed.add_argument("--source-platform")
    embed.add_argument("--provider", default="deterministic_test")
    embed.add_argument("--dry-run", action="store_true", default=True)
    embed.add_argument("--db")
    embed.add_argument("--write", action="store_true")
    embed.add_argument("--json", action="store_true", help="Emit JSON output (default output format).")

    rank = subparsers.add_parser("rank", help="Rank sample texts without database access.")
    rank.add_argument("--query", required=True)
    rank.add_argument("--sample-texts", required=True, help="Pipe-separated sample texts.")
    rank.add_argument("--provider", default="deterministic_test")
    rank.add_argument("--top-k", type=int, default=5)

    search = subparsers.add_parser("search", help="Search stored test embeddings without reading source text.")
    search.add_argument("--db", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--memory-space")
    search.add_argument("--provider", default="deterministic_test")
    search.add_argument("--top-k", type=int, default=5)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "embed":
            provider = get_embedding_provider(args.provider)
            record = build_embedding_record(
                source_table=args.source_table,
                source_id=args.source_id,
                text=args.text,
                memory_space=args.memory_space,
                source_platform=args.source_platform,
                provider_name=args.provider,
            )
            save = None
            if args.db:
                save = save_embedding_record(record, args.db, dry_run=not args.write)
            print(
                json.dumps(
                    {
                        "dry_run": not args.write,
                        "provider": args.provider,
                        "model_name": record["embedding_model"],
                        "embedding_dim": record["embedding_dim"],
                        "cache_root": getattr(provider, "cache_root", None),
                        "embedding_preview": json.loads(record["embedding_json"])[:5],
                        "record": {key: value for key, value in record.items() if key != "embedding_json"},
                        "save": save,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "rank":
            records = [
                build_embedding_record("sample_texts", str(index), text, provider_name=args.provider)
                for index, text in enumerate(args.sample_texts.split("|"), start=1)
            ]
            ranked = rank_by_semantic_similarity(
                args.query,
                records,
                provider_name=args.provider,
                top_k=args.top_k,
            )
            print(
                json.dumps(
                    {
                        "provider": args.provider,
                        "embedding_dim": records[0]["embedding_dim"] if records else 0,
                        "results": [
                            {
                                "source_id": item["source_id"],
                                "score": item["score"],
                                "content_preview": item["content_preview"],
                            }
                            for item in ranked
                        ],
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0

        if args.command == "search":
            results = semantic_search_records(
                args.query,
                args.db,
                memory_space=args.memory_space,
                top_k=args.top_k,
                provider_name=args.provider,
            )
            print(
                json.dumps(
                    {
                        "provider": args.provider,
                        "top_k": args.top_k,
                        "results": results,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return 0
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
