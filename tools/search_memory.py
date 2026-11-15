from __future__ import annotations

import argparse
from pathlib import Path
import sys

from memory.read_only_retriever import DEFAULT_MEMORY_SPACE, retrieve


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
MAX_SNIPPET_CHARS = 200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deprecated wrapper around memory.read_only_retriever.")
    parser.add_argument("query", help="Search keyword.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum result rows.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--role", choices=["user", "assistant"], default="", help="Filter message hits by role.")
    parser.add_argument("--source-platform", default="", help="Filter hits by source platform, such as deepseek.")
    parser.add_argument("--memory-space", default=DEFAULT_MEMORY_SPACE, help="Explicit memory space; defaults to whale.")
    return parser.parse_args()


def clip(text: str | None, limit: int = MAX_SNIPPET_CHARS) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def text_snippet(text: str | None, query: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    source = " ".join((text or "").split())
    if not source:
        return ""
    pos = source.lower().find(query.lower())
    if pos < 0:
        return clip(source, limit)
    start = max(0, pos - 60)
    end = min(len(source), pos + len(query) + 120)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(source) else ""
    return clip(prefix + source[start:end] + suffix, limit)


def search(
    db_path: Path,
    query: str,
    limit: int,
    role: str = "",
    source_platform: str = "",
    memory_space: str = DEFAULT_MEMORY_SPACE,
):
    rows = retrieve(query, top_k=limit, memory_space=memory_space, db_path=db_path)
    filtered = []
    for row in rows:
        if role and row.get("role") != role:
            continue
        if source_platform and row.get("source_platform") != source_platform:
            continue
        item = dict(row)
        item["source"] = item.pop("hit_source", "")
        filtered.append(item)
    return filtered[: max(1, limit)]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    print("WARNING: tools/search_memory.py is deprecated; using memory.read_only_retriever.retrieve().")
    rows = search(
        db_path,
        args.query,
        args.limit,
        role=args.role,
        source_platform=args.source_platform,
        memory_space=args.memory_space,
    )
    if not rows:
        print("No results.")
        return 0

    for idx, row in enumerate(rows, start=1):
        snippet = text_snippet(row["snippet"], args.query)
        print(f"[{idx}] source: {row['source']}")
        print(f"title: {row['title']}")
        print(f"update_time_iso: {row['update_time_iso']}")
        print(f"role: {row['role']}")
        print(f"source_platform: {row['source_platform']}")
        print(f"memory_space: {row['memory_space']}")
        print(f"snippet: {snippet}")
        print(f"conversation_id: {row['conversation_id']}")
        print(f"message_id: {row['message_id']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
