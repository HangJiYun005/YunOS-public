from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory import summarizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build v3-B memory summaries with controlled test writes.")
    parser.add_argument("--db", default=None, help="SQLite database path.")
    parser.add_argument("--memory-space", default=summarizer.DEFAULT_MEMORY_SPACE, help="Explicit memory space.")
    parser.add_argument("--conversation-id", default="", help="Conversation id for a conversation summary.")
    parser.add_argument("--overview", action="store_true", help="Build a memory_space overview.")
    parser.add_argument("--max-messages", type=int, default=200, help="Maximum messages for a conversation summary.")
    parser.add_argument("--max-conversations", type=int, default=50, help="Maximum conversations for an overview.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write summaries. This is the default.")
    parser.add_argument("--write", action="store_true", help="Write to a migrated test database.")
    parser.add_argument("--allow-real-write", action="store_true", help="Enable real database writes.")
    parser.add_argument("--confirm-real-write", action="store_true", help="Confirm real database writes.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    if args.write and not args.db:
        print("--write requires explicit --db", file=sys.stderr)
        return 2

    if args.write:
        write_path = Path(args.db).expanduser().resolve()
        if summarizer.is_real_db_path(write_path):
            gate_error = summarizer.real_write_gate_error(args.allow_real_write, args.confirm_real_write)
            if gate_error:
                print(gate_error)
                return 2
            if args.memory_space != summarizer.DEFAULT_MEMORY_SPACE:
                print(summarizer.REAL_WRITE_SPACE_ERROR)
                return 2
        elif not summarizer.is_allowed_summary_write_path(write_path):
            print("v3-B only writes memory summaries to test databases")
            return 2

    db_path = Path(args.db).expanduser().resolve() if args.db else summarizer.DEFAULT_DB_PATH
    summarizer.DEFAULT_DB_PATH = db_path

    if args.overview:
        summary = summarizer.build_memory_space_overview(
            memory_space=args.memory_space,
            max_conversations=args.max_conversations,
        )
    else:
        if not args.conversation_id:
            print("--conversation-id is required unless --overview is used.", file=sys.stderr)
            return 2
        summary = summarizer.build_conversation_summary(
            args.conversation_id,
            memory_space=args.memory_space,
            max_messages=args.max_messages,
        )

    save_result = summarizer.save_summary(
        summary,
        db_path,
        dry_run=not args.write,
        allow_real_write=args.allow_real_write,
        confirm_real_write=args.confirm_real_write,
    )
    print(json.dumps({"summary": summary, "save_result": save_result}, ensure_ascii=False, indent=2))
    return 2 if save_result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
