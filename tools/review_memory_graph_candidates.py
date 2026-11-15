"""Read-only YunOS v6.1 relation-candidate review CLI."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.memory_graph import DEFAULT_MIN_SCORE, build_memory_graph
from memory.relation_review import (
    ALLOWED_DECISIONS,
    CANDIDATE_STATUSES,
    RelationReviewError,
    ReviewPlan,
    decide_relation_candidate,
    discover_relation_candidates,
    prepare_review_plan,
    replacement_preview,
    verify_review_plan,
)
from memory.relation_review_state import (
    ProductionReviewAuthorityFactory,
    ReviewAuthorityNotInitialized,
    ReviewAuthorityStateLost,
)
from memory.semantic_memory import load_embedding_records
from memory.summary_repository import graph_summary_sources


REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
MEMORY_SIDECAR_PATH = PROJECT_ROOT / "data" / "memory" / "ayunos_memory.db"
SEMANTIC_SIDECAR_PATH = PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db"
RELATION_TYPES = ("belongs_to_conversation", "belongs_to_memory_space", "newer_version_of", "semantic_related")
COMMANDS = {"list", "prepare", "decide", "verify", "replacement-preview"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] not in COMMANDS:
        values.insert(0, "list")
    parser = argparse.ArgumentParser(description="Discover and review evidence-bound relation candidates without persistence.")
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="List candidates read-only (default).")
    _common_filters(list_parser)
    list_parser.add_argument("--status", choices=sorted(CANDIDATE_STATUSES))
    list_parser.add_argument("--limit", type=int, default=20)

    prepare = sub.add_parser("prepare", help="Prepare a deterministic authority-bound review plan.")
    _common_filters(prepare)
    prepare.add_argument("--candidate-id", required=True)

    verify = sub.add_parser("verify", help="Rebuild current state and verify a review plan.")
    _common_filters(verify)
    verify.add_argument("--plan-file", type=Path, required=True)

    decide = sub.add_parser("decide", help="Validate one human decision and emit a non-persisted envelope.")
    _common_filters(decide)
    decide.add_argument("--plan-file", type=Path, required=True)
    decide.add_argument("--confirmation-file", type=Path, required=True)
    decide.add_argument("--decision", choices=ALLOWED_DECISIONS, required=True)
    decide.add_argument("--reason-code", required=True)
    decide.add_argument("--reviewer-identity", required=True)
    decide.add_argument("--replacement-candidate-id")
    decide.add_argument("--replacement-plan-file", type=Path)

    replacement = sub.add_parser("replacement-preview", help="Preview a bound replacement without recording supersession.")
    _common_filters(replacement)
    replacement.add_argument("--candidate-id", required=True)
    replacement.add_argument("--replacement-candidate-id", required=True)
    replacement.add_argument("--plan-file", type=Path, required=True)
    replacement.add_argument("--replacement-plan-file", type=Path, required=True)
    return parser.parse_args(values)


def _common_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--conversation")
    parser.add_argument("--memory-space")
    parser.add_argument("--relation-type", choices=RELATION_TYPES)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--json", action="store_true")


def main(argv: list[str] | None = None) -> int:
    started = _utc_now()
    try:
        args = parse_args(argv)
        discovery = _discover(args)
        if args.command == "list":
            payload = _list_payload(discovery, args)
        elif args.command == "prepare":
            authority = _open_authority()
            prepared = prepare_review_plan(discovery, args.candidate_id, authority=authority)
            payload = {**prepared.public_dict(), **_plan_audit_fields(prepared.plan)}
        elif args.command == "verify":
            authority = _open_authority()
            plan = _load_plan(args.plan_file)
            payload = {**verify_review_plan(discovery, plan, authority=authority), **_plan_audit_fields(plan)}
        elif args.command == "replacement-preview":
            authority = _open_authority()
            original_plan = _load_plan(args.plan_file)
            replacement_plan = _load_plan(args.replacement_plan_file)
            payload = replacement_preview(
                discovery,
                args.candidate_id,
                args.replacement_candidate_id,
                original_plan=original_plan,
                replacement_plan=replacement_plan,
                authority=authority,
            )
        else:
            authority = _open_authority()
            plan = _load_plan(args.plan_file)
            replacement_plan = _load_plan(args.replacement_plan_file) if args.replacement_plan_file else None
            confirmation = _read_json_object(args.confirmation_file)
            payload = decide_relation_candidate(
                discovery,
                plan,
                decision=args.decision,
                reason_code=args.reason_code,
                reviewer_identity=args.reviewer_identity,
                confirmation_fields=confirmation,
                authority=authority,
                replacement_candidate_id=args.replacement_candidate_id,
                replacement_plan=replacement_plan,
            )
        payload = _audit(payload, started)
        _emit(payload, args.json)
        return 0
    except ReviewAuthorityNotInitialized:
        payload = _audit({"status": "review_authority_not_initialized", "reason_code": "review_authority_not_initialized", "next_action": "manual_initialization_required", "executable": False}, started)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 2
    except ReviewAuthorityStateLost:
        payload = _audit({"status": "authority_state_lost", "reason_code": "authority_state_lost", "next_action": "manual_recovery_required", "executable": False}, started)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 2
    except (RelationReviewError, ValueError, TypeError, OSError, json.JSONDecodeError):
        payload = _audit({"status": "refused", "reason_code": "invalid_or_stale_review_input", "next_action": "prepare_again"}, started)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 2
    except Exception:
        payload = _audit({"status": "unavailable", "reason_code": "candidate_discovery_unavailable", "next_action": "manual_review"}, started)
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 2


def _discover(args: argparse.Namespace):
    summaries = graph_summary_sources(
        MEMORY_SIDECAR_PATH,
        REAL_DB_PATH,
        memory_space=args.memory_space,
        conversation_id=args.conversation,
        include_legacy=args.include_legacy,
    )
    embeddings: list[dict[str, Any]] = []
    for source_table in ("memory_summaries", "summary_versions"):
        embeddings.extend(load_embedding_records(
            SEMANTIC_SIDECAR_PATH,
            memory_space=args.memory_space,
            source_table=source_table,
            limit=1000,
        ))
    build = build_memory_graph(summaries, embeddings, min_score=args.min_score)
    return discover_relation_candidates(build)


def _list_payload(discovery, args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1 or args.limit > 20:
        raise RelationReviewError("limit must be between 1 and 20")
    candidates = [item for item in discovery.candidates if args.relation_type is None or item.relation_type == args.relation_type]
    unavailable = [item for item in discovery.unavailable if args.relation_type is None or item.relation_type == args.relation_type]
    if args.status:
        candidates = [item for item in candidates if item.candidate_status == args.status]
        unavailable = [item for item in unavailable if item.candidate_status == args.status]
    if args.min_score is not None:
        candidates = [item for item in candidates if item.score is None or item.score >= args.min_score]
        unavailable = [item for item in unavailable if item.score is None or item.score >= args.min_score]
    return {
        "status": "listed",
        "graph_build_identity": discovery.graph_build_identity,
        "candidate_count": len(candidates),
        "unavailable_count": len(unavailable),
        "candidates": [item.public_dict() for item in candidates[:args.limit]],
        "unavailable": [item.public_dict() for item in unavailable[:args.limit]],
        "next_action": "prepare",
    }


def _load_plan(path: Path) -> ReviewPlan:
    value = _read_json_object(path)
    value["allowed_decisions"] = tuple(value.get("allowed_decisions") or ())
    return ReviewPlan(**value)


def _plan_audit_fields(plan: ReviewPlan) -> dict[str, Any]:
    return {
        "candidate_id": plan.candidate_id,
        "review_plan_id": plan.review_plan_id,
        "relation_type": plan.relation_type,
        "source_node_id": plan.source_node_id,
        "target_node_id": plan.target_node_id,
        "generator_identity": plan.generator_identity,
        "evidence_digest": plan.evidence_digest,
        "authority_id_digest": __import__("hashlib").sha256(plan.authority_store_id.encode("utf-8")).hexdigest(),
        "review_session_id": plan.review_session_id,
        "review_chain_id": plan.review_chain_id,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RelationReviewError("JSON input must be an object")
    return value


def _open_authority():
    return ProductionReviewAuthorityFactory.open()


def _audit(payload: dict[str, Any], started_at: str) -> dict[str, Any]:
    identity = {
        "candidate_id": payload.get("candidate_id") or payload.get("original_candidate_id"),
        "review_plan_id": payload.get("review_plan_id"),
        "decision_id": payload.get("decision_id"),
        "started_at": started_at,
    }
    workflow_run_id = "workflow_" + __import__("hashlib").sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "workflow_run_id": workflow_run_id,
        **payload,
        "persisted": False,
        "readonly": True,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key in ("status", "candidate_count", "unavailable_count", "candidate_id", "review_plan_id", "decision_id", "next_action", "persisted", "readonly"):
        if key in payload:
            print(f"{key}={json.dumps(payload[key], ensure_ascii=True, sort_keys=True)}")
    for candidate in payload.get("candidates", []):
        print("candidate=" + json.dumps(candidate, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    for item in payload.get("unavailable", []):
        print("unavailable=" + json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
