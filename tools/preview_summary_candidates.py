from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.summary_candidate import preview_summary_candidates, public_preview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only deterministic summary candidate preview.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--memory-space", choices=("general", "whale"))
    parser.add_argument("--status", choices=("summary_missing", "coverage_unknown", "new_messages", "source_changed"))
    parser.add_argument("--show-text", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 50:
        raise SystemExit("--limit must be between 1 and 50")
    payload = public_preview(preview_summary_candidates(PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db"), args.limit, args.conversation_id or None, args.memory_space, args.status), args.show_text)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
