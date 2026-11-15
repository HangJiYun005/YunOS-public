"""YunOS v6.2 production persistence status and sealed command surface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.controlled_knowledge_persistence import production_enablement_conditions
from memory.knowledge_sidecar import production_knowledge_status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the sealed YunOS v6.2 knowledge persistence gateway.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read production sidecar status without initialization.")
    persist = sub.add_parser("persist", help="Request persistence (sealed until production enablement is approved).")
    persist.add_argument("--candidate-id", required=True)
    persist.add_argument("--review-plan-id", required=True)
    persist.add_argument("--decision-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    enablement = production_enablement_conditions()
    if args.command == "status":
        payload = {"status": "inspected", "readonly": True, "persisted": False, "knowledge_sidecar": production_knowledge_status(), "enablement": enablement}
        code = 0
    else:
        payload = {
            "status": "refused", "reason_code": "production_enablement_incomplete",
            "candidate_id": args.candidate_id, "review_plan_id": args.review_plan_id,
            "decision_id": args.decision_id, "executed": False, "persisted": False,
            "readonly": True, "enablement": enablement,
        }
        code = 2
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
