#!/usr/bin/env python3
"""Generate redacted, test-only v8.3-E acceptance evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_execution import (
    execute_test_bundle,
    preflight_execution,
    read_execution_journal,
)
from memory.ingestion_verification import (
    IngestionVerificationError,
    capture_test_target_snapshot,
)
from tests.v83_ingestion_execution_fixtures import (
    PREFLIGHT_AT,
    build_execution_fixture,
)


OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "v8.3-e-approval-atomic-commit"
)
PRODUCTION_DB = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
IMPORT_ROOT = PROJECT_ROOT / "imports" / "chatgpt"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def production_gate() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for path in (
        PRODUCTION_DB,
        Path(str(PRODUCTION_DB) + "-wal"),
        Path(str(PRODUCTION_DB) + "-shm"),
    ):
        if not path.exists():
            rows[path.name] = {"exists": False}
            continue
        stat = path.stat()
        rows[path.name] = {
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _sha256_file(path),
        }
    tree_rows: list[dict[str, Any]] = []
    tree_digest_lines: list[str] = []
    if IMPORT_ROOT.exists():
        for path in sorted(
            (item for item in IMPORT_ROOT.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        ):
            tree_rows.append({
                "relative_path_digest": hashlib.sha256(
                    path.relative_to(IMPORT_ROOT).as_posix().encode("utf-8")
                ).hexdigest(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
            tree_digest_lines.append(
                path.relative_to(IMPORT_ROOT).as_posix()
                + "|"
                + str(path.stat().st_size)
                + "|"
                + tree_rows[-1]["sha256"]
            )
    rows["imports_chatgpt"] = {
        "file_count": len(tree_rows),
        "total_bytes": sum(item["size"] for item in tree_rows),
        "tree_digest": hashlib.sha256(
            "\n".join(tree_digest_lines).encode("utf-8")
        ).hexdigest(),
    }
    stash = subprocess.run(
        ["git", "rev-parse", "stash@{0}"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rows["stash_0_oid"] = stash
    return rows


def production_schema_audit(asset_root: Path) -> dict[str, Any]:
    try:
        capture_test_target_snapshot(PRODUCTION_DB, asset_root)
    except IngestionVerificationError as error:
        return {
            "represented_readonly": True,
            "execution_allowed": False,
            "reason_code": error.reason_code,
            "production_write_executed": False,
            "production_schema_migrated": False,
        }
    return {
        "represented_readonly": True,
        "execution_allowed": False,
        "reason_code": "unexpected_test_schema_marker",
        "production_write_executed": False,
        "production_schema_migrated": False,
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=OUTPUT_ROOT))
    before_gate = production_gate()
    fixture = build_execution_fixture(
        run_root / "synthetic-fixture",
        with_asset=True,
    )
    preflight = preflight_execution(
        batch=fixture.batch,
        manifest=fixture.manifest,
        import_plan=fixture.plan,
        stage_plan=fixture.stage_plan,
        stage_result=fixture.stage_result,
        approval=fixture.approval,
        operations=fixture.operations,
        target=fixture.target,
        before_snapshot=fixture.before,
        staged_entities=fixture.staged_entities,
        now=PREFLIGHT_AT,
    )
    if not preflight.passed or preflight.bundle is None:
        raise RuntimeError("synthetic_preflight_failed")
    result = execute_test_bundle(
        bundle=preflight.bundle,
        target=fixture.target,
        before_snapshot=fixture.before,
        staged_entities=fixture.staged_entities,
    )
    execution_root = (
        fixture.target.root
        / "executions"
        / preflight.bundle.execution_id
    )
    events = read_execution_journal(
        execution_root / "journal.jsonl",
        execution_id=preflight.bundle.execution_id,
    )
    after_gate = production_gate()
    report = {
        "node": "YunOS v8.3-E",
        "scenario": "synthetic_test_target_only",
        "preflight_passed": preflight.passed,
        "preflight_checks": dict(preflight.check_results),
        "execution": result.to_dict(),
        "journal": {
            "event_count": len(events),
            "event_types": [item.event_type for item in events],
            "chain_digest": events[-1].event_digest,
        },
        "production_schema_audit": production_schema_audit(
            fixture.target.asset_root
        ),
        "production_gate_before": before_gate,
        "production_gate_after": after_gate,
        "production_gate_unchanged": before_gate == after_gate,
        "private_content_included": False,
        "absolute_paths_included": False,
        "network_used": False,
    }
    evidence_path = run_root / "acceptance-evidence.json"
    evidence_path.write_text(
        json.dumps(
            report,
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": result.status,
        "verification_passed": bool(
            result.verification and result.verification.passed
        ),
        "production_gate_unchanged": before_gate == after_gate,
        "evidence_file": evidence_path.name,
    }, sort_keys=True))
    return 0 if (
        result.status == "completed"
        and result.verification is not None
        and result.verification.passed
        and before_gate == after_gate
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
