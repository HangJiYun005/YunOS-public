from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_objective import AuthorityRequest, build_objective_contract
from memory.cognitive_external_send import create_external_send_consent
from memory.cognitive_orchestration import (
    ContextBudget,
    ModelBudget,
    ModelBudgetState,
    assemble_cognitive_context,
    assert_search_mode_zero_model,
    bind_manual_provider_selection,
    context_source_from_v7_memory_package,
    context_source_from_v9_package,
    prepare_model_request,
    reserve_model_call,
    settle_model_call,
)
from memory.cognitive_provider import (
    ProviderHealthAttestation,
    build_suggested_provider_registry,
)
from memory.cognitive_runtime_bridge import ProductionCognitiveRuntimeBridge
from memory.deepseek_v4_adapter import DeepSeekV4ProviderError
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.memory_package import build_production_memory_package
from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_models import (
    SourceAnchor,
    canonical_digest,
    canonical_json,
)
from memory.production_runtime_execution import (
    ProductionRuntimeExecutionService,
    open_production_runtime_execution,
)
from memory.runtime_enablement import (
    RuntimeEnablementError,
    open_production_deepseek_readiness,
    run_production_deepseek_readiness,
)


ACCEPTANCE_VERSION = "v10-c-real-provider-acceptance-1"
QUESTION = "讨论 YunOS 下一阶段方向。"
USER_CONSENT_UTTERANCE = "授权，继续"
EXTERNAL_SEND_DISCLOSURE = {
    "version": "v10-c-one-shot-external-send-disclosure-1",
    "purpose": "discussion",
    "provider_id": "deepseek",
    "model": "deepseek-v4-flash",
    "maximum_model_calls": 1,
    "automatic_retry_allowed": False,
    "provider_fallback_allowed": False,
    "context_policy": "minimum_required_v7_memory_and_v9_state",
    "project_write_allowed": False,
    "delegation_allowed": False,
    "state_update_allowed": False,
    "result_acceptance_allowed": False,
}
EXTERNAL_SEND_DISCLOSURE_DIGEST = canonical_digest(EXTERNAL_SEND_DISCLOSURE)
SEARCH_LOCAL_ENDPOINT = "http://127.0.0.1:8765"
RUNTIME_REQUEST_ID = "request-v10-c-real-discussion-20260731"
RUNTIME_MUTATION_ID = "mutation-v10-c-real-discussion-20260731"
CANONICAL_OUTPUT_DIR = PROJECT_ROOT / "output" / "v10-c"
RUNTIME_DB = PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
DATABASES = {
    "main": PROJECT_ROOT / "data" / "ayunos_chatgpt.db",
    "profile": PROJECT_ROOT / "data" / "profile" / "approved_profile.db",
    "semantic": PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db",
    "runtime": RUNTIME_DB,
    "operational": (
        PROJECT_ROOT
        / "data"
        / "operational"
        / "ayunos_operational_state.db"
    ),
}
RUNTIME_TABLES = {
    "conversations": "runtime_conversations",
    "request_artifacts": "runtime_request_artifacts",
    "model_runs": "runtime_model_runs",
    "messages": "runtime_messages",
    "events": "runtime_run_events",
    "errors": "runtime_errors",
    "receipts": "runtime_receipts",
}


class AcceptanceError(RuntimeError):
    pass


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _plus_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        (parsed + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size": stat.st_size,
        "sha256": _sha256(path),
        "mtime_ns": stat.st_mtime_ns,
    }


def _database_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for name, path in DATABASES.items():
        snapshot[name] = {
            "database": _file_record(path),
            "wal": _file_record(Path(str(path) + "-wal")),
            "shm_observational": _file_record(Path(str(path) + "-shm")),
        }
    return snapshot


def _runtime_status() -> dict[str, Any]:
    uri = f"file:{RUNTIME_DB.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        counts = {
            name: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for name, table in RUNTIME_TABLES.items()
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    return {
        "counts": counts,
        "integrity_check": integrity,
        "quick_check": quick,
        "foreign_key_violations": foreign_keys,
    }


def _content_identity(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    record = snapshot[name]
    return {
        "database": {
            key: record["database"].get(key)
            for key in ("exists", "size", "sha256")
        },
        "wal": {
            key: record["wal"].get(key)
            for key in ("exists", "size", "sha256")
        },
    }


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _search_zero_model_proof() -> dict[str, Any]:
    before_file = _file_record(RUNTIME_DB)
    before = _runtime_status()
    url = SEARCH_LOCAL_ENDPOINT + "/api/search?" + urlencode({"q": "YunOS"})
    with urlopen(url, timeout=10) as response:
        body = json.loads(response.read().decode("utf-8"))
        status = response.status
    after = _runtime_status()
    after_file = _file_record(RUNTIME_DB)
    deltas = {
        key: after["counts"][key] - before["counts"][key]
        for key in before["counts"]
    }
    proof = assert_search_mode_zero_model(
        runtime_execution_delta=deltas["model_runs"],
        model_call_delta=deltas["events"],
        input_token_delta=0,
        output_token_delta=0,
        cost_microunits_delta=0,
    )
    if (
        status != 200
        or before_file != after_file
        or any(deltas.values())
        or not isinstance(body, dict)
        or not isinstance(body.get("items"), list)
        or not body["items"]
    ):
        raise AcceptanceError("search_zero_model_proof_failed")
    return {
        **proof,
        "http_status": status,
        "local_endpoint": SEARCH_LOCAL_ENDPOINT,
        "query": "YunOS",
        "result_count": len(body["items"]),
        "runtime_file_unchanged": True,
        "runtime_counts_before": before["counts"],
        "runtime_counts_after": after["counts"],
    }


def _backup_runtime(output_dir: Path) -> dict[str, Any]:
    backup_dir = output_dir / "pre-call-runtime-backup"
    backup_dir.mkdir(parents=True, exist_ok=False)
    copied: list[dict[str, Any]] = []
    for source in (
        RUNTIME_DB,
        Path(str(RUNTIME_DB) + "-wal"),
        Path(str(RUNTIME_DB) + "-shm"),
    ):
        if not source.exists():
            continue
        target = backup_dir / source.name
        shutil.copy2(source, target)
        source_record = _file_record(source)
        target_record = _file_record(target)
        if (
            source_record["size"] != target_record["size"]
            or source_record["sha256"] != target_record["sha256"]
        ):
            raise AcceptanceError("runtime_backup_copy_mismatch")
        copied.append(
            {
                "source": source_record,
                "backup": target_record,
            }
        )
    if not copied:
        raise AcceptanceError("runtime_backup_missing")
    manifest = {
        "version": "v10-c-runtime-pre-call-backup-1",
        "created_at": _utc_now(),
        "files": copied,
    }
    manifest["backup_digest"] = canonical_digest(manifest)
    _write_exclusive(backup_dir / "manifest.json", manifest)
    return manifest


def _build_cognitive_request(readiness: dict[str, Any]) -> dict[str, Any]:
    checked_at = _utc_now()
    registry = build_suggested_provider_registry(
        ("secret.deepseek.production",)
    )
    profile = registry.get("deepseek", 1)
    health = ProviderHealthAttestation(
        provider_id=profile.provider_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        health_state="healthy",
        checked_at=checked_at,
        expires_at=_plus_seconds(checked_at, 600),
        evidence_digest=canonical_digest(readiness),
    )
    selection = registry.manual_select(
        provider_id=profile.provider_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        health_attestation=health,
        evaluated_at=checked_at,
    )
    provider_binding = bind_manual_provider_selection(selection)

    anchor = SourceAnchor(
        anchor_id="anchor:user:v10-c-real-discussion",
        source_kind="user_declaration",
        locator="goal:v10-c-real-discussion",
        evidence_digest=canonical_digest({"user_utterance": QUESTION}),
        observed_at=checked_at,
        privacy_scope="private_metadata",
    )
    consent_anchor = SourceAnchor(
        anchor_id="anchor:user:v10-c-real-discussion-consent",
        source_kind="user_declaration",
        locator="goal:v10-c-real-discussion-consent",
        evidence_digest=canonical_digest(
            {
                "user_utterance": USER_CONSENT_UTTERANCE,
                "disclosure_digest": EXTERNAL_SEND_DISCLOSURE_DIGEST,
            }
        ),
        observed_at=checked_at,
        privacy_scope="private_metadata",
    )
    objective = build_objective_contract(
        user_utterance=QUESTION,
        interpreted_goal=(
            "基于有来源、受预算的长期记忆与当前项目状态，讨论 YunOS "
            "下一阶段方向；只形成建议，不执行或改变状态。"
        ),
        project_id="project:yunos",
        source_anchors=(anchor, consent_anchor),
        success_criteria=(
            "输出中文优先的文档式方向建议。",
            "明确区分现状证据、推断与建议。",
            "不创建委托，不调用写工具，不修改项目状态。",
        ),
        prohibited_outcomes=(
            "不得执行工具或项目写入。",
            "不得把模型建议标记为用户接受。",
            "不得创建下一版本或扩大授权。",
        ),
        agency_level="consult",
        agency_selected_by="user",
        maximum_authorized_agency_level="consult",
        agency_selection_source_anchor_id=anchor.anchor_id,
        cognitive_stance="continue",
        risk_tolerance="low",
        resource_constraints=("最多一次真实模型调用。",),
        assumptions=("生产 Operational State 是当前状态证据来源。",),
        authority_request=AuthorityRequest(
            requested=False,
            database_permission="readonly",
        ),
    )

    memory_package = build_production_memory_package(
        RetrievalQuery("YunOS"),
        future_token_budget=500,
    )
    operational = OperationalStateContextService()
    now_package = operational.now_package(budget_profile="Small")
    resume_package = operational.resume_package(
        "project:yunos",
        budget_profile="Small",
    )
    evaluated_at = _utc_now()
    sources = (
        context_source_from_v7_memory_package(
            memory_package,
            purpose="提供与 YunOS 相关的 v7 长期记忆证据。",
            maximum_items=8,
            maximum_bytes=20_000,
            required=True,
            priority=20,
        ),
        context_source_from_v9_package(
            now_package,
            source_system="v9_now",
            purpose="提供 v9 当前目标、阻塞和待决事项。",
            maximum_items=8,
            maximum_bytes=12_000,
            evaluated_at=evaluated_at,
            include_sections=(
                "active_goals",
                "blockers",
                "pending_decisions",
            ),
            required=True,
            priority=10,
        ),
        context_source_from_v9_package(
            resume_package,
            source_system="v9_resume",
            purpose="提供 YunOS 项目的 v9 Resume Package。",
            maximum_items=6,
            maximum_bytes=12_000,
            evaluated_at=evaluated_at,
            include_sections=(
                "current_work",
                "next_approved_steps",
                "blockers",
                "outcomes",
            ),
            required=True,
            priority=11,
        ),
    )
    context = assemble_cognitive_context(
        objective_id=objective.objective_id,
        objective_digest=objective.canonical_digest,
        sources=sources,
        budget=ContextBudget(
            maximum_items=20,
            maximum_bytes=40_000,
            maximum_estimated_tokens=10_000,
        ),
    )
    budget = ModelBudget(
        maximum_model_calls=1,
        maximum_input_tokens=12_000,
        maximum_output_tokens=1_024,
        maximum_total_tokens=13_024,
        maximum_cost_microunits=20_000_000,
    )
    request = prepare_model_request(
        mode="ai",
        objective=objective,
        context=context,
        provider_binding=provider_binding,
        model_budget=budget,
        expected_output_kind="answer",
        maximum_output_bytes=262_144,
    )
    estimated_input = min(
        budget.maximum_input_tokens,
        max(4_000, context.estimated_tokens + 1_000),
    )
    reservation, state = reserve_model_call(
        mode="ai",
        request_digest=request.request_digest,
        provider_binding=provider_binding,
        estimated_input_tokens=estimated_input,
        requested_output_tokens=1_024,
        estimated_cost_microunits=20_000_000,
        provider_context_limit_tokens=profile.context_limit_tokens,
        budget=budget,
        state=ModelBudgetState(),
    )
    return {
        "profile": profile,
        "health": health,
        "selection": selection,
        "objective": objective,
        "context": context,
        "budget": budget,
        "request": request,
        "reservation": reservation,
        "budget_state": state,
        "memory_package": memory_package,
        "now_package": now_package,
        "resume_package": resume_package,
    }


def run(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    if output_dir != CANONICAL_OUTPUT_DIR.resolve():
        raise AcceptanceError("acceptance_output_directory_must_be_canonical")
    sentry_path = output_dir / "real-deepseek-one-shot-sentry.json"
    if sentry_path.exists():
        raise AcceptanceError("one_shot_sentry_exists_do_not_rerun")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise AcceptanceError("acceptance_output_directory_not_empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    database_before = _database_snapshot()
    runtime_before = _runtime_status()
    if (
        runtime_before["integrity_check"] != "ok"
        or runtime_before["quick_check"] != "ok"
        or runtime_before["foreign_key_violations"] != 0
    ):
        raise AcceptanceError("runtime_preflight_failed")
    search_proof = _search_zero_model_proof()
    backup = _backup_runtime(output_dir)

    readiness_authority = open_production_deepseek_readiness(
        execute=True,
        allow_official_readiness_gets=True,
        confirm_no_chat_completion=True,
    )
    readiness = run_production_deepseek_readiness(
        authority=readiness_authority
    )
    if (
        readiness.get("status") != "ready"
        or readiness.get("chat_completion_sent") is not False
    ):
        raise AcceptanceError("deepseek_readiness_failed")

    cognitive = _build_cognitive_request(readiness)
    bridge = ProductionCognitiveRuntimeBridge()
    disclosure = bridge.prepare_disclosure(
        cognitive["selection"],
        cognitive["request"],
        cognitive["reservation"],
        request_id=RUNTIME_REQUEST_ID,
        mutation_id=RUNTIME_MUTATION_ID,
    )
    consent_granted_at = _utc_now()
    consent = create_external_send_consent(
        consent_id="consent:v10-c-real-discussion-20260731",
        user_utterance=USER_CONSENT_UTTERANCE,
        disclosure_digest=EXTERNAL_SEND_DISCLOSURE_DIGEST,
        selection=cognitive["selection"],
        request=cognitive["request"],
        reservation=cognitive["reservation"],
        **disclosure.consent_binding(),
        granted_at=consent_granted_at,
        expires_at=disclosure.expires_at,
    )
    preview = bridge.preview(
        disclosure,
        cognitive["selection"],
        cognitive["request"],
        cognitive["reservation"],
        consent=consent,
    )
    preview_record = preview.to_dict()
    sentry = {
        "version": "v10-c-real-deepseek-one-shot-sentry-1",
        "state": "DISPATCH_INTENT_DO_NOT_RERUN",
        "created_at": _utc_now(),
        "do_not_rerun": True,
        "question": QUESTION,
        "readiness_digest": canonical_digest(readiness),
        "health_attestation_digest": (
            cognitive["health"].attestation_digest
        ),
        "provider_selection_digest": (
            cognitive["selection"].selection_digest
        ),
        "objective_id": cognitive["objective"].objective_id,
        "objective_digest": cognitive["objective"].canonical_digest,
        "context_digest": cognitive["context"].context_digest,
        "context_bindings": [
            binding.to_dict()
            for binding in cognitive["context"].bindings
        ],
        "request_digest": cognitive["request"].request_digest,
        "reservation_id": cognitive["reservation"].reservation_id,
        "bridge_digest": preview_record["bridge_digest"],
        "external_send_consent": consent.to_dict(),
        "runtime_model_run_id": preview_record["runtime_model_run_id"],
        "runtime_counts_before": runtime_before["counts"],
        "database_before": database_before,
    }
    _write_exclusive(sentry_path, sentry)

    authority = open_production_runtime_execution(
        execute=True,
        allow_external_model_call=True,
        confirm_deepseek_send=True,
    )
    execution = bridge.execute(
        preview,
        cognitive["selection"],
        cognitive["request"],
        cognitive["reservation"],
        consent=consent,
        confirmed=True,
        authority=authority,
    )
    execution_record = execution.to_dict()
    runtime_references = execution_record["runtime_references"]
    model_run_id = runtime_references["model_run_id"]
    readback = ProductionRuntimeExecutionService().get_run(model_run_id)

    usage = readback.get("usage") or {}
    settled = settle_model_call(
        reservation=cognitive["reservation"],
        state=cognitive["budget_state"],
        actual_provider=(
            cognitive["request"].provider_binding.response_identity()
        ),
        actual_input_tokens=usage.get("prompt_tokens", -1),
        actual_output_tokens=usage.get("completion_tokens", -1),
        actual_cost_microunits=None,
        budget=cognitive["budget"],
    )
    runtime_after = _runtime_status()
    database_after = _database_snapshot()
    deltas = {
        key: runtime_after["counts"][key] - runtime_before["counts"][key]
        for key in runtime_before["counts"]
    }
    expected_delta = {
        "conversations": 1,
        "request_artifacts": 1,
        "model_runs": 1,
        "messages": 2,
        "events": 5,
        "errors": 0,
        "receipts": 1,
    }
    non_runtime_unchanged = {
        name: (
            _content_identity(database_before, name)
            == _content_identity(database_after, name)
        )
        for name in DATABASES
        if name != "runtime"
    }
    response = readback.get("response") or {}
    response_text = response.get("content") or ""
    transport = execution_record.get("transport", {})
    checks = {
        "runtime_succeeded": readback.get("status") == "succeeded",
        "execution_reference_succeeded": (
            execution_record.get("status") == "succeeded"
        ),
        "exact_runtime_delta": deltas == expected_delta,
        "adapter_call_once": (
            transport.get("adapter_call_count_this_request") == 1
        ),
        "model_call_once": transport.get("model_call_count_for_run") == 1,
        "exactly_one_call": transport.get("exactly_one_call") is True,
        "automatic_retry_false": transport.get("automatic_retry") is False,
        "mock_transport_false": transport.get("mock_transport") is False,
        "network_request_once": (
            transport.get("network_request_count") == 1
        ),
        "credential_read": transport.get("credential_read") is True,
        "receipt_present": bool(readback.get("receipt_identity")),
        "usage_valid": (
            isinstance(usage.get("prompt_tokens"), int)
            and isinstance(usage.get("completion_tokens"), int)
            and usage.get("total_tokens")
            == usage.get("prompt_tokens") + usage.get("completion_tokens")
        ),
        "discussion_present": bool(response_text.strip()),
        "finish_reason_present": bool(response.get("finish_reason")),
        "non_runtime_databases_unchanged": all(
            non_runtime_unchanged.values()
        ),
        "runtime_integrity_ok": (
            runtime_after["integrity_check"] == "ok"
            and runtime_after["quick_check"] == "ok"
            and runtime_after["foreign_key_violations"] == 0
        ),
        "token_usage_settled_once": (
            settled.settled_reservation_ids
            == (cognitive["reservation"].reservation_id,)
        ),
        "unknown_cost_reserved_at_ceiling": (
            settled.unknown_cost_reservation_ids
            == (cognitive["reservation"].reservation_id,)
            and settled.unknown_cost_reserved_ceiling_microunits
            == cognitive["reservation"].cost_microunits_ceiling
            and settled.actual_cost_microunits == 0
        ),
        "external_send_consent_bound": (
            preview_record.get("external_send_consent_digest")
            == consent.consent_digest
        ),
        "search_zero_model": search_proof["local_only"] is True,
    }
    report = {
        "version": ACCEPTANCE_VERSION,
        "status": "GO" if all(checks.values()) else "NO_GO_DO_NOT_RERUN",
        "do_not_rerun": True,
        "completed_at": _utc_now(),
        "question": QUESTION,
        "external_send_consent": consent.to_dict(),
        "external_send_disclosure": EXTERNAL_SEND_DISCLOSURE,
        "checks": checks,
        "search_proof": search_proof,
        "backup": backup,
        "readiness": readiness,
        "provider": {
            "provider_id": cognitive["selection"].provider_id,
            "profile_version": cognitive["selection"].profile_version,
            "profile_digest": cognitive["selection"].profile_digest,
            "model": cognitive["selection"].model,
            "selection_digest": cognitive["selection"].selection_digest,
            "health_attestation_digest": (
                cognitive["health"].attestation_digest
            ),
            "external_send_consent_digest": consent.consent_digest,
            "hidden_fallback": False,
            "secret_persistence": {
                "status": "not_evaluated_in_runner",
                "required_evidence": (
                    "independent_post_call_value_scan"
                ),
            },
        },
        "objective": cognitive["objective"].to_dict(),
        "context": {
            "context_digest": cognitive["context"].context_digest,
            "selected_items": cognitive["context"].selected_items,
            "canonical_bytes": cognitive["context"].canonical_bytes,
            "estimated_tokens": cognitive["context"].estimated_tokens,
            "bindings": [
                binding.to_dict()
                for binding in cognitive["context"].bindings
            ],
            "v7_package_identity": (
                cognitive["memory_package"]["package_identity"]
            ),
            "v9_now_digest": cognitive["now_package"]["package_digest"],
            "v9_resume_digest": (
                cognitive["resume_package"]["package_digest"]
            ),
        },
        "runtime": {
            "model_run_id": model_run_id,
            "receipt_identity": readback.get("receipt_identity"),
            "artifact_identity": (
                readback.get("request_artifact") or {}
            ).get("artifact_identity"),
            "delivery_identity": readback.get("delivery_identity"),
            "usage": usage,
            "cost": {
                "amount": None,
                "reason": "provider_price_metadata_unknown",
                "accounting_status": "unknown_reserved_ceiling",
                "reserved_ceiling_microunits": (
                    settled.unknown_cost_reserved_ceiling_microunits
                ),
            },
            "finish_reason": response.get("finish_reason"),
            "provider_response_id": response.get("provider_response_id"),
            "response_sha256": hashlib.sha256(
                response_text.encode("utf-8")
            ).hexdigest(),
            "response_characters": len(response_text),
            "response_text": response_text,
            "transport": transport,
            "counts_before": runtime_before["counts"],
            "counts_after": runtime_after["counts"],
            "count_delta": deltas,
        },
        "databases": {
            "before": database_before,
            "after": database_after,
            "non_runtime_content_unchanged": non_runtime_unchanged,
        },
    }
    report["report_digest"] = canonical_digest(report)
    _write_exclusive(
        output_dir / "real-deepseek-acceptance.json",
        report,
    )
    if report["status"] != "GO":
        raise AcceptanceError("real_provider_acceptance_no_go_do_not_rerun")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot YunOS v10-C search isolation and real DeepSeek "
            "discussion acceptance. Never rerun after the sentry exists."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Cross the one-shot provider boundary.",
    )
    parser.add_argument(
        "--allow-official-readiness-gets",
        action="store_true",
    )
    parser.add_argument(
        "--confirm-one-deepseek-discussion",
        action="store_true",
    )
    parser.add_argument(
        "--output-dir",
        default=str(CANONICAL_OUTPUT_DIR),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not (
        args.execute
        and args.allow_official_readiness_gets
        and args.confirm_one_deepseek_discussion
    ):
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "external_model_calls": 0,
                    "chat_completion_sent": False,
                    "required_flags": [
                        "--execute",
                        "--allow-official-readiness-gets",
                        "--confirm-one-deepseek-discussion",
                    ],
                    "output_dir": args.output_dir,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    try:
        report = run(Path(args.output_dir))
    except (
        AcceptanceError,
        RuntimeEnablementError,
        DeepSeekV4ProviderError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason_code": str(exc),
                    "do_not_rerun_if_sentry_exists": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_digest": report["report_digest"],
                "model_run_id": report["runtime"]["model_run_id"],
                "receipt_identity": report["runtime"]["receipt_identity"],
                "usage": report["runtime"]["usage"],
                "report_path": str(
                    Path(args.output_dir).resolve()
                    / "real-deepseek-acceptance.json"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
