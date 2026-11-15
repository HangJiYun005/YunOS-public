"""Measure YunOS v9 operational-state performance without production writes."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.operational_state_context import (  # noqa: E402
    OperationalStateContextService,
)
from memory.operational_state_models import (  # noqa: E402
    AuthorityArtifact,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    utc_now,
)
from memory.operational_state_store import (  # noqa: E402
    DEFAULT_OPERATIONAL_STATE_PATH,
    OperationalStateStore,
)


CONTRACT_VERSION = "yunos-v9-performance-acceptance-1"
YUNOS_PROJECT = "project:yunos"
THRESHOLDS_MS = {
    "append_max": 500.0,
    "snapshot_max": 1_000.0,
    "backup_restore_max": 2_000.0,
    "projection_rebuild_max": 2_000.0,
    "now_p95": 500.0,
    "resume_p95": 500.0,
    "project_list_p95": 250.0,
    "stale_preview_p95": 250.0,
    "conflict_preview_p95": 250.0,
}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1_000


def _distribution(call: Callable[[], Any], count: int = 12) -> dict[str, Any]:
    samples = []
    digest = None
    for _ in range(count):
        value, elapsed = _timed(call)
        samples.append(elapsed)
        digest = canonical_digest(value)
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "samples": len(samples),
        "min_ms": round(ordered[0], 3),
        "median_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(ordered[index], 3),
        "max_ms": round(ordered[-1], 3),
        "result_digest": digest,
    }


def _latest_snapshot_id(database: Path) -> str:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            "SELECT snapshot_id FROM state_snapshots "
            "ORDER BY through_sequence DESC, created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("performance_snapshot_missing")
    return str(row[0])


def measure(database: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    source_before = {
        "size": database.stat().st_size,
        "sha256": __import__("hashlib").sha256(database.read_bytes()).hexdigest(),
    }
    context = OperationalStateContextService(database)
    reads = {
        "now": _distribution(
            lambda: context.now_package(budget_profile="Medium")
        ),
        "resume": _distribution(
            lambda: context.resume_package(
                YUNOS_PROJECT, budget_profile="Medium"
            )
        ),
        "project_list": _distribution(context.project_list),
        "stale_preview": _distribution(
            lambda: context.filtered_state(
                freshness="stale", project_id=YUNOS_PROJECT
            )
        ),
        "conflict_preview": _distribution(
            lambda: context.conflicts(project_id=YUNOS_PROJECT)
        ),
    }

    with tempfile.TemporaryDirectory(prefix="ayunos-v9-performance-") as name:
        root = Path(name)
        clone = root / "operational-clone.db"
        backup = root / "operational-backup.db"
        restored = root / "operational-restored.db"
        source = OperationalStateStore(database)
        source.backup_to(clone)
        store = OperationalStateStore(clone)
        base = datetime(2030, 1, 1, tzinfo=timezone.utc)
        issued = _timestamp(base)
        expires = _timestamp(base + timedelta(hours=1))
        owner = "owner:v9-performance-probe"
        token = "v9-performance-probe-token"
        authority = AuthorityArtifact(
            authority_id="authority:v9-performance-probe",
            issued_by="actor:local-user",
            issued_at=issued,
            expires_at=expires,
            target_project_id=YUNOS_PROJECT,
            allowed_event_types=(
                "resource_observed",
                "snapshot_create",
                "projection_rebuild",
            ),
            read_scope=("operational_state:metadata",),
            write_scope=("*",),
            prohibited_operations=("push", "production-write"),
            commit_permission=True,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )
        anchor = SourceAnchor(
            anchor_id="anchor:v9-performance-probe",
            source_kind="runtime",
            locator="performance:disposable-clone",
            evidence_digest=canonical_digest(
                {"contract_version": CONTRACT_VERSION}
            ),
            observed_at=issued,
            privacy_scope="private_metadata",
        )
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=issued,
            expires_at=expires,
        )
        event = build_state_event(
            project_id=YUNOS_PROJECT,
            entity_id="resource:v9-performance-probe",
            entity_type="resource",
            event_type="resource_observed",
            source_kind="observed",
            source_anchor=anchor,
            authority_kind="verified_runtime",
            actor_kind="runtime",
            actor_id="runtime:v9-performance-probe",
            observed_at=_timestamp(base + timedelta(seconds=1)),
            effective_at=_timestamp(base + timedelta(seconds=1)),
            recorded_at=_timestamp(base + timedelta(seconds=1)),
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key="idempotency:v9-performance-probe",
            conflict_group_id=None,
            payload={
                "state": "available",
                "title": "Disposable performance probe",
            },
            privacy_scope="private_metadata",
            authority_digest=authority.authority_digest,
        )

        def append_probe() -> Any:
            preview = store.preview_event(authority=authority, event=event)
            return store.commit_preview(
                authority=authority,
                preview=preview,
                lease_owner_id=owner,
                lease_token=token,
            ).to_dict()

        _, append_ms = _timed(append_probe)
        snapshot, snapshot_ms = _timed(
            lambda: store.create_snapshot(
                created_at=_timestamp(base + timedelta(seconds=2)),
                authority=authority,
                lease_owner_id=owner,
                lease_token=token,
            )
        )
        store.release_writer_lease(
            owner_id=owner,
            token=token,
            released_at=_timestamp(base + timedelta(seconds=3)),
        )

        def backup_restore() -> Any:
            store.backup_to(backup)
            restored_store = OperationalStateStore.restore_from_backup(
                backup, restored
            )
            return restored_store.integrity_report().__dict__

        _, restore_ms = _timed(backup_restore)
        restored_store = OperationalStateStore(restored)
        projection_before = restored_store.list_projections()
        _, rebuild_ms = _timed(
            lambda: restored_store.rebuild_projections(
                snapshot_id=snapshot["snapshot_id"],
                recorded_at=_timestamp(base + timedelta(seconds=4)),
            )
        )
        if projection_before != restored_store.list_projections():
            raise RuntimeError("performance_projection_rebuild_mismatch")

    source_after = {
        "size": database.stat().st_size,
        "sha256": __import__("hashlib").sha256(database.read_bytes()).hexdigest(),
    }
    metrics = {
        "append_ms": round(append_ms, 3),
        "snapshot_ms": round(snapshot_ms, 3),
        "backup_restore_ms": round(restore_ms, 3),
        "projection_rebuild_ms": round(rebuild_ms, 3),
        "reads": reads,
    }
    gates = {
        "append": metrics["append_ms"] <= THRESHOLDS_MS["append_max"],
        "snapshot": metrics["snapshot_ms"] <= THRESHOLDS_MS["snapshot_max"],
        "backup_restore": (
            metrics["backup_restore_ms"]
            <= THRESHOLDS_MS["backup_restore_max"]
        ),
        "projection_rebuild": (
            metrics["projection_rebuild_ms"]
            <= THRESHOLDS_MS["projection_rebuild_max"]
        ),
        **{
            name: reads[name]["p95_ms"] <= THRESHOLDS_MS[f"{name}_p95"]
            for name in (
                "now",
                "resume",
                "project_list",
                "stale_preview",
                "conflict_preview",
            )
        },
    }
    report = {
        "contract_version": CONTRACT_VERSION,
        "measured_at": utc_now(),
        "production_source_opened_read_only": True,
        "production_source_unchanged": source_before == source_after,
        "temporary_clone_only_for_writes": True,
        "thresholds_ms": THRESHOLDS_MS,
        "metrics": metrics,
        "gates": gates,
        "passed": source_before == source_after and all(gates.values()),
    }
    return {**report, "report_digest": canonical_digest(report)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_OPERATIONAL_STATE_PATH
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = measure(args.db)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "blocked",
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(canonical_json(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
