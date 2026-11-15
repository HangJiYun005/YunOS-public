from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.summary_reader import get_summary_count, list_summaries
from memory.summarizer import is_real_db_path


DEFAULT_TEST_DB = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="View memory_summaries in read-only mode.")
    parser.add_argument("--db", default=str(DEFAULT_TEST_DB), help="SQLite database path.")
    parser.add_argument("--memory-space", default="", help="Optional memory_space filter.")
    parser.add_argument("--summary-type", default="", help="Optional summary_type filter.")
    parser.add_argument("--conversation-id", default="", help="Optional conversation_id filter.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows, clamped to 100.")
    parser.add_argument("--real-readonly", action="store_true", help="Required when reading the real database.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of compact text.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if is_real_db_path(db_path) and not args.real_readonly:
        print("Reading the real database requires --real-readonly.", file=sys.stderr)
        return 2
    rows = list_summaries(
        db_path,
        memory_space=args.memory_space or None,
        summary_type=args.summary_type or None,
        conversation_id=args.conversation_id or None,
        limit=args.limit,
    )
    payload = {
        "items": rows,
        "count": len(rows),
        "total_count": get_summary_count(db_path, memory_space=args.memory_space or None),
        "db_mode": "real" if is_real_db_path(db_path) else "test",
        "readonly": True,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No memory summaries found. The memory_summaries table may be missing or empty.")
        return 0
    print(f"Memory summaries: {len(rows)}")
    for row in rows:
        conversation_id = row.get("conversation_id") or ""
        short_conv = conversation_id[:12] + "..." if len(conversation_id) > 15 else conversation_id
        print(f"- #{row['id']} {row['memory_space']} {row['summary_type']} {short_conv}")
        print(f"  messages={row.get('source_message_count') or 0} method={row.get('method') or ''}")
        print(f"  {row.get('summary_text') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
