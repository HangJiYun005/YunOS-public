from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.change_detector import detect_changes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only memory source change detection.")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")), help="SQLite database path.")
    parser.add_argument("--memory-space", default="", help="Optional memory space filter.")
    parser.add_argument("--conversation-id", default="", help="Optional conversation ID filter.")
    parser.add_argument("--memory-sidecar", type=Path, help="Optional initialized sidecar to read; never initialized or written here.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = detect_changes(args.db, args.memory_space or None, args.conversation_id or None, args.memory_sidecar)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"Memory change items: {payload['totals']['items']}")
        print(json.dumps({"status_counts": payload["status_counts"], "action_counts": payload["action_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
