from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_v10_agent_loop import V10BoundedAgentLoopTest
from tools.local_web import CognitiveObjectiveReadonlyService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a disposable, verified v10-E Cognitive Run fixture."
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).resolve()
    if output.exists() or not output.parent.is_dir():
        raise RuntimeError("fixture output must be a new file in an existing directory")

    case = V10BoundedAgentLoopTest(methodName="runTest")
    case.setUp()
    try:
        fixture = case._fixture()
        proposals = {
            fixture.action.node_id: case._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, _source, _artifacts = case._loop(fixture, proposals)
        result = loop.run(fixture.run.run_id)
        if result.run.status != "waiting_acceptance":
            raise RuntimeError(
                f"fixture did not reach waiting_acceptance: {result.run.status}"
            )

        with sqlite3.connect(
            f"file:{fixture.database.as_posix()}?mode=ro",
            uri=True,
        ) as source, sqlite3.connect(output) as destination:
            source.backup(destination)

        listing = CognitiveObjectiveReadonlyService(output).list_runs(limit=10)
        selected = next(
            item for item in listing["items"]
            if item["run_id"] == result.run.run_id
        )
        print(json.dumps({
            "database": str(output),
            "run_id": result.run.run_id,
            "title": selected["title"],
            "status": selected["status"],
            "agency_level": "act",
        }, ensure_ascii=False))
        return 0
    finally:
        case.tearDown()


if __name__ == "__main__":
    raise SystemExit(main())
