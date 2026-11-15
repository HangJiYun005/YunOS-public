from __future__ import annotations

import argparse
from contextlib import closing
import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import uuid


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Local listener with exclusive ownership, including on Windows."""

    allow_reuse_address = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.read_only_retriever import retrieve, search_all_memory_conversations
from memory.conversation_reader import (
    ConversationReaderBadRequest,
    ConversationReaderConflict,
    ConversationReaderNotFound,
    read_conversation_messages,
)
from memory.graph_enhanced_retrieval import (
    GraphEnhancedRetrievalService,
    RetrievalQuery,
    RetrievalValidationError,
)
from memory.knowledge_graph_query import KnowledgeGraphQuery, query_production_knowledge_graph
from memory.memory_package import (
    PROTOCOL_VERSION as MEMORY_PACKAGE_PROTOCOL_VERSION,
    MemoryPackageValidationError,
    build_production_memory_package,
    render_memory_package,
)
from memory.evidence_selection import (
    PROTOCOL_VERSION as EVIDENCE_SELECTION_PROTOCOL_VERSION,
    EvidenceSelectionValidationError,
    build_production_evidence_selection,
    render_evidence_selection,
)
from memory.temporal_memory_candidates import (
    PROTOCOL_VERSION as TEMPORAL_CANDIDATE_PROTOCOL_VERSION,
    TemporalCandidateValidationError,
    build_production_temporal_candidates,
    render_temporal_candidates,
)
from memory.context_assembly import (
    PROTOCOL_VERSION as CONTEXT_ASSEMBLY_PROTOCOL_VERSION,
    ContextAssemblyValidationError,
    build_production_context_assembly,
    render_context_assembly,
)
from memory.production_delivery import (
    PROTOCOL_VERSION as CONTEXT_DELIVERY_PROTOCOL_VERSION,
    ProductionDeliveryValidationError as ContextDeliveryValidationError,
    build_production_delivery as build_production_context_delivery,
    render_production_delivery as render_context_delivery,
)
from memory.approved_profile_control import (
    PROTOCOL_VERSION as APPROVED_PROFILE_CONTROL_PROTOCOL_VERSION,
    ApprovedProfileControlError,
    ApprovedProfileIdempotencyConflict,
    ApprovedProfileRevisionConflict,
    ApprovedProfileSidecarUnavailable,
    active_approved_profile_projection,
    build_production_approved_profile_review,
    open_production_profile_admin,
    record_production_approved_profile_decision,
)
from memory.profile_enablement import (
    PROTOCOL_VERSION as PROFILE_ENABLEMENT_PROTOCOL_VERSION,
    ProfileEnablementError,
    ProfileEnablementIdempotencyConflict,
    ProfileEnablementRevisionConflict,
    ProfileEnablementService,
    ProfileEnablementSidecarUnavailable,
    active_profile_enablement_projection,
    build_production_profile_enablement_review,
    open_production_gate_b1,
    open_production_gate_b2,
)
from memory.production_runtime_review import (
    PROTOCOL_VERSION as RUNTIME_PROTOCOL_VERSION,
    ProductionRuntimeReviewError,
    ProductionRuntimeReviewService,
)
from memory.production_runtime_status import (
    STATUS_PROTOCOL_VERSION as PRODUCTION_RUNTIME_STATUS_PROTOCOL_VERSION,
    production_runtime_status,
)
from memory.operational_state_context import OperationalStateContextService
from memory.db_path_policy import DEFAULT_OPERATIONAL_STATE_PATH
from memory.operational_state_models import OperationalStateError
from memory.semantic_memory import semantic_summary_search_preview
from memory.summary_health import diagnose_summaries, diagnose_summary
from memory.summary_reader import list_summaries
from tools.optimize_whale_context import optimize_context
from whale_chat import build_whale_prompt_context


WEB_ROOT = PROJECT_ROOT / "web"
PUBLIC_PROJECTS_PATH = PROJECT_ROOT / "config" / "projects_public.json"
PRIVATE_PROJECTS_PATH = PROJECT_ROOT / "config_private" / "projects_private.json"
TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
SEMANTIC_SUMMARY_DB_PATH = PROJECT_ROOT / "data" / "test" / "semantic_from_summaries.db"
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
DEFAULT_DB_PATH = TEST_DB_PATH
ALLOWED_STATUSES = {"keep", "trash", "project", "study", "writing", "life", "tech", "review"}
SOURCE_PLATFORM_LABELS = {
    "chatgpt": "ChatGPT",
    "deepseek": "DeepSeek",
    "claude": "Claude",
    "gemini": "Gemini",
    "manual": "手动记录",
    "markdown": "Markdown",
    "txt": "TXT",
    "unknown": "未知来源",
}
SOURCE_PLATFORMS = tuple(SOURCE_PLATFORM_LABELS)
MEMORY_SPACE_LABELS = {
    "whale": "Whale",
    "general": "General",
}
ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "system": "系统",
    "tool": "工具",
}


MARKS_SQL = """
CREATE TABLE IF NOT EXISTS conversation_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_marks (
    conversation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_marks (
    message_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_tags_conversation_id
    ON conversation_tags(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag
    ON conversation_tags(tag);
CREATE INDEX IF NOT EXISTS idx_conversation_marks_status
    ON conversation_marks(status);
CREATE INDEX IF NOT EXISTS idx_message_marks_status
    ON message_marks(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact(text: str | None, limit: int = 240) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def period_from_iso(value: str | None, group_by: str) -> str:
    text_value = (value or "").strip()
    if group_by == "day":
        return text_value[:10] if len(text_value) >= 10 else "unknown"
    return text_value[:7] if len(text_value) >= 7 else "unknown"


def normalize_text_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    seen = set()
    for item in value:
        text_value = str(item).strip()
        if text_value and text_value not in seen:
            seen.add(text_value)
            items.append(text_value)
    return items


def normalize_priority(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100


def normalize_project(raw, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"Project entry {index} must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"Project entry {index} is missing name")
    keywords = normalize_text_list(raw.get("keywords"))
    aliases = normalize_text_list(raw.get("aliases"))
    match_terms = normalize_text_list([name, *keywords, *aliases])
    if not match_terms:
        raise ValueError(f"Project entry {index} has no match terms")
    return {
        "name": name,
        "description": str(raw.get("description") or "").strip(),
        "status": str(raw.get("status") or "active").strip() or "active",
        "priority": normalize_priority(raw.get("priority", 100)),
        "keywords": keywords,
        "aliases": aliases,
        "match_terms": match_terms,
    }


def role_label(role: str | None) -> str:
    value = (role or "").strip()
    return ROLE_LABELS.get(value, value or "未知")


def source_platform_label(value: str | None) -> str:
    platform = (value or "unknown").strip() or "unknown"
    return SOURCE_PLATFORM_LABELS.get(platform, SOURCE_PLATFORM_LABELS["unknown"])


def reference_uri(conversation_id: str | None, message_id: str | None = "") -> str:
    conv = (conversation_id or "").strip()
    msg = (message_id or "").strip()
    if not conv:
        return ""
    if msg:
        return f"ayunos://conversation/{conv}?message={msg}"
    return f"ayunos://conversation/{conv}"


def _local_port(value: str) -> int:
    try:
        port = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 through 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 through 65535")
    return port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AyunOS local-only web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. 0.0.0.0 is refused.")
    parser.add_argument("--port", type=_local_port, default=None, help="Port to bind (1-65535).")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite database path.")
    parser.add_argument(
        "--operational-state-db",
        default=None,
        help=(
            "Optional existing operational-state database. Non-production "
            "paths remain subject to the alias-safe temporary database policy."
        ),
    )
    parser.add_argument("--real", action="store_true", help="Label this run as the real local memory database.")
    parser.add_argument("--readonly", action="store_true", help="Open the database in read-only preview mode.")
    parser.add_argument(
        "--profile-admin", action="store_true",
        help="Enable local approved-profile mutations while keeping the real source database read-only.",
    )
    parser.add_argument(
        "--runtime-review", action="store_true",
        help="Enable the explicit fixed-ID Runtime review surface with no execution authority.",
    )
    parser.add_argument(
        "--runtime-admin", action="store_true",
        help="Enable the fixed DeepSeek runtime execution endpoint; source database remains read-only.",
    )
    parser.add_argument("--allow-external-model-calls", action="store_true")
    parser.add_argument("--confirm-deepseek-send", action="store_true")
    parser.add_argument(
        "--runtime-admin-mock", action="store_true",
        help="Enable the real Runtime-admin UI with a mock transport and temporary D1 sidecar.",
    )
    parser.add_argument(
        "--runtime-test-sidecar",
        help="Existing initialized temporary D1 sidecar used only with --runtime-admin-mock.",
    )
    raw_argv = sys.argv[1:]
    port_arguments = sum(
        argument == "--port" or argument.startswith("--port=")
        for argument in raw_argv
    )
    if port_arguments > 1:
        parser.error("--port may be specified only once")
    args = parser.parse_args(raw_argv)
    if args.port is None:
        if args.runtime_admin:
            parser.error("--runtime-admin requires an explicit --port")
        args.port = 8765
    return args


class CognitiveObjectiveReadonlyService:
    """Lazy, query-only adapter over the v10 Operational extension."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def _store(self):
        from memory.cognitive_state_store import CognitiveStateStore

        return CognitiveStateStore(self.path)

    def _planning_store(self):
        from memory.cognitive_planning_store import CognitivePlanningStore

        return CognitivePlanningStore(self.path)

    def _provider_store(self):
        from memory.cognitive_provider_store import CognitiveProviderStore

        return CognitiveProviderStore(self.path)

    def _execution_store(self):
        from memory.cognitive_execution_store import CognitiveExecutionStore

        return CognitiveExecutionStore(self.path)

    def list_objectives(
        self,
        *,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        items = self._store().list_objectives(
            project_id=project_id,
            limit=limit,
        )
        return {"items": items, "count": len(items), "limit": limit}

    def get_objective(
        self,
        objective_id: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        return self._store().get_objective(objective_id, version)

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        items = self._store().list_objective_candidates(
            project_id=project_id,
            status=status,
            limit=limit,
        )
        return {"items": items, "count": len(items), "limit": limit}

    def list_capabilities(
        self,
        *,
        risk_class: str | None = None,
        availability: str | None = None,
        executor_kind: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = self._planning_store().list_capabilities(
            risk_class=risk_class,
            availability=availability,
            executor_kind=executor_kind,
            limit=limit,
        )
        return {"items": items, "count": len(items), "limit": limit}

    def list_plans(
        self,
        *,
        objective_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        items = self._planning_store().list_plans(
            objective_id=objective_id,
            project_id=project_id,
            limit=limit,
        )
        return {"items": items, "count": len(items), "limit": limit}

    def get_plan(
        self,
        plan_id: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        return self._planning_store().get_plan(plan_id, version)

    def list_providers(
        self,
        *,
        provider_id: str | None = None,
        enabled: bool | None = None,
        health_state: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = self._provider_store().list_providers(
            provider_id=provider_id,
            enabled=enabled,
            health_state=health_state,
            limit=limit,
        )
        return {"items": items, "count": len(items), "limit": limit}

    def get_provider(
        self,
        provider_id: str,
        *,
        profile_version: int | None = None,
    ) -> dict[str, Any] | None:
        return self._provider_store().get_provider(
            provider_id,
            profile_version,
        )

    @staticmethod
    def _public_provider(record: dict[str, Any]) -> dict[str, Any]:
        # Secret references are intentionally absent from the workspace bundle.
        return {
            key: value
            for key, value in record.items()
            if key != "secret_reference"
        }

    @staticmethod
    def _public_authorization(authorization: Any) -> dict[str, Any]:
        record = authorization.to_dict()
        visible = (
            "contract_version",
            "authorization_id",
            "objective_id",
            "objective_version",
            "objective_digest",
            "plan_id",
            "plan_version",
            "plan_digest",
            "project_id",
            "delegation_id",
            "delegation_digest",
            "read_roots",
            "write_roots",
            "allowed_capabilities",
            "denied_capabilities",
            "allowed_commands",
            "risk_ceiling",
            "max_steps",
            "max_duration_ms",
            "max_model_calls",
            "max_tokens",
            "max_cost_microunits",
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "database_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
            "valid_from",
            "expires_at",
            "rollback_policy",
            "acceptance_policy",
            "authority_digest",
        )
        return {key: record[key] for key in visible}

    @staticmethod
    def _collection(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": items, "count": len(items)}

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from memory.cognitive_execution_models import RUN_STATUSES
        from memory.operational_state_models import validate_identity

        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
        if status is not None and status not in RUN_STATUSES:
            raise OperationalStateError("invalid_cognitive_run_status")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise OperationalStateError("invalid_cognitive_workspace_limit")
        execution_store = self._execution_store()
        clauses: list[str] = []
        arguments: list[Any] = []
        if project_id is not None:
            clauses.append("o.project_id=?")
            arguments.append(project_id)
        if status is not None:
            clauses.append("r.status=?")
            arguments.append(status)
        query = (
            "SELECT r.run_id FROM cognitive_runs r "
            "JOIN objective_contracts o ON o.objective_id=r.objective_id "
            "AND o.version=r.objective_version"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY COALESCE(r.last_activity_at,r.started_at,'') DESC,"
            "r.run_id LIMIT ?"
        )
        arguments.append(limit)
        with closing(execution_store.operational._connect()) as connection:
            run_ids = tuple(
                row["run_id"]
                for row in connection.execute(query, arguments).fetchall()
            )
        objective_store = self._store()
        provider_store = self._provider_store()
        items: list[dict[str, Any]] = []
        for run_id in run_ids:
            run = execution_store.get_run(run_id)
            objective = objective_store.get_objective(
                run.objective_id,
                run.objective_version,
            )
            provider = provider_store.get_provider(
                run.provider_id,
                run.provider_profile_version,
            )
            if (
                objective is None
                or objective["canonical_digest"] != run.objective_digest
                or provider is None
                or provider["profile_digest"] != run.provider_profile_digest
            ):
                raise OperationalStateError(
                    "cognitive_workspace_run_binding_mismatch"
                )
            authorization = (
                None
                if run.authorization_id is None
                else execution_store.get_authorization(run.authorization_id)
            )
            if authorization is not None and (
                authorization.authority_digest != run.authorization_digest
                or authorization.objective_digest != run.objective_digest
                or authorization.plan_digest != run.plan_digest
                or authorization.project_id != objective["project_id"]
            ):
                raise OperationalStateError(
                    "cognitive_workspace_authorization_mismatch"
                )
            contract = objective["contract"]
            items.append(
                {
                    "run_id": run.run_id,
                    "run_digest": run.run_digest,
                    "objective_id": run.objective_id,
                    "objective_version": run.objective_version,
                    "title": contract["interpreted_goal"],
                    "project_id": objective["project_id"],
                    "agency_level": objective["agency_level"],
                    "cognitive_stance": objective["cognitive_stance"],
                    "status": run.status,
                    "provider_id": run.provider_id,
                    "provider_profile_version": run.provider_profile_version,
                    "provider_display_name": provider["display_name"],
                    "risk_ceiling": (
                        None
                        if authorization is None
                        else authorization.risk_ceiling
                    ),
                    "started_at": run.started_at,
                    "last_activity_at": run.last_activity_at,
                    "ended_at": run.ended_at,
                    "failure_code": run.failure_code,
                    "waiting_acceptance": run.status == "waiting_acceptance",
                    "overreach_detected": any(
                        event.event_type == "overreach_blocked"
                        for event in execution_store.list_supervision_events(
                            run.run_id
                        )
                    ),
                }
            )
        return {
            "runs_schema_version": 1,
            "items": items,
            "count": len(items),
            "limit": limit,
        }

    @staticmethod
    def _context_bindings(execution_store: Any, run: Any) -> list[dict[str, Any]]:
        from memory.cognitive_orchestration import ContextBinding
        from memory.operational_state_models import canonical_json

        with closing(execution_store.operational._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM context_bindings WHERE objective_id=? "
                "AND objective_version=? ORDER BY source_system,binding_id",
                (run.objective_id, run.objective_version),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["canonical_binding"])
                binding = ContextBinding(**payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise OperationalStateError(
                    "cognitive_context_binding_record_corrupt"
                ) from None
            if (
                canonical_json(payload) != row["canonical_binding"]
                or binding.binding_id != row["binding_id"]
                or binding.objective_id != row["objective_id"]
                or binding.source_system != row["source_system"]
                or binding.source_reference != row["source_reference"]
                or binding.source_digest != row["source_digest"]
                or binding.maximum_items != row["maximum_items"]
                or binding.maximum_bytes != row["maximum_bytes"]
                or binding.selected_items != row["selected_items"]
                or binding.canonical_bytes != row["canonical_bytes"]
                or binding.freshness != row["freshness"]
                or int(binding.contains_secret) != row["contains_secret"]
                or binding.binding_digest != row["binding_digest"]
            ):
                raise OperationalStateError(
                    "cognitive_context_binding_record_tampered"
                )
            items.append(
                {
                    **binding.to_dict(),
                    "objective_version": row["objective_version"],
                    "source_anchor_id": row["source_anchor_id"],
                    "created_at": row["created_at"],
                }
            )
        return items

    @staticmethod
    def _verifications(
        execution_store: Any,
        run_id: str,
        executions: list[Any],
    ) -> list[dict[str, Any]]:
        from memory.operational_state_models import canonical_digest, canonical_json

        reservation_event_ids = tuple(
            execution.reservation_event_id
            for execution in executions
            if execution.reservation_event_id is not None
        )
        if not reservation_event_ids:
            return []
        placeholders = ",".join("?" for _ in reservation_event_ids)
        with closing(execution_store.operational._connect()) as connection:
            entity_ids = tuple(
                row["entity_id"]
                for row in connection.execute(
                    "SELECT entity_id FROM state_events WHERE event_id IN ("
                    f"{placeholders}) ORDER BY entity_id",
                    reservation_event_ids,
                ).fetchall()
            )
            if len(entity_ids) != len(reservation_event_ids):
                raise OperationalStateError(
                    "cognitive_workspace_verification_binding_missing"
                )
            entity_placeholders = ",".join("?" for _ in entity_ids)
            rows = connection.execute(
                "SELECT * FROM state_verifications WHERE entity_id IN ("
                f"{entity_placeholders}) "
                "ORDER BY recorded_at,verification_id",
                entity_ids,
            ).fetchall()
        execution_by_id = {
            execution.execution_id: execution for execution in executions
        }
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["canonical_verification"])
            except (TypeError, json.JSONDecodeError):
                raise OperationalStateError(
                    "cognitive_workspace_verification_corrupt"
                ) from None
            details = payload.get("details") if isinstance(payload, dict) else None
            execution = (
                execution_by_id.get(details.get("tool_execution_id"))
                if isinstance(details, dict)
                else None
            )
            if (
                not isinstance(payload, dict)
                or canonical_json(payload) != row["canonical_verification"]
                or canonical_digest(payload) != row["verification_digest"]
                or payload.get("verification_id") != row["verification_id"]
                or payload.get("entity_id") != row["entity_id"]
                or payload.get("event_id") != row["event_id"]
                or payload.get("verifier_kind") != row["verifier_kind"]
                or payload.get("result") != row["result"]
                or payload.get("evidence_digest") != row["evidence_digest"]
                or payload.get("recorded_at") != row["recorded_at"]
                or not isinstance(details, dict)
                or details.get("run_id") != run_id
                or execution is None
                or details.get("execution_digest")
                != execution.execution_digest
            ):
                raise OperationalStateError(
                    "cognitive_workspace_verification_tampered"
                )
            items.append(payload)
        return items

    @staticmethod
    def _learning_candidates(
        execution_store: Any,
        run: Any,
        project_id: str,
        outcome_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        from memory.cognitive_orchestration import contains_secret_material
        from memory.operational_state_models import canonical_digest, canonical_json

        kinds = (
            "learning_candidate",
            "fact_record",
            "observation",
            "pattern_candidate",
            "workflow_candidate",
        )
        kind_placeholders = ",".join("?" for _ in kinds)
        entity_ids = tuple(dict.fromkeys((run.run_id, run.objective_id, *outcome_ids)))
        entity_placeholders = ",".join("?" for _ in entity_ids)
        query = (
            "SELECT * FROM state_candidates WHERE project_id=? "
            f"AND candidate_type IN ({kind_placeholders}) "
            f"AND entity_id IN ({entity_placeholders}) "
            "ORDER BY created_at,candidate_id LIMIT 501"
        )
        arguments = (
            project_id,
            *kinds,
            *entity_ids,
        )
        with closing(execution_store.operational._connect()) as connection:
            rows = connection.execute(query, arguments).fetchall()
        if len(rows) > 500:
            raise OperationalStateError(
                "cognitive_workspace_learning_limit_exceeded"
            )
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                candidate = json.loads(row["canonical_candidate"])
            except (TypeError, json.JSONDecodeError):
                raise OperationalStateError(
                    "cognitive_workspace_learning_record_corrupt"
                ) from None
            if (
                not isinstance(candidate, dict)
                or canonical_json(candidate) != row["canonical_candidate"]
                or canonical_digest(candidate) != row["candidate_digest"]
            ):
                raise OperationalStateError(
                    "cognitive_workspace_learning_record_tampered"
                )
            if contains_secret_material(candidate):
                raise OperationalStateError(
                    "cognitive_workspace_secret_material_forbidden"
                )
            document = (
                candidate.get("document")
                if candidate.get("document_type") == "learning_candidate"
                else candidate
            )
            if isinstance(document, dict) and (
                (
                    document.get("run_id") is not None
                    and document.get("run_id") != run.run_id
                )
                or (
                    document.get("objective_id") is not None
                    and document.get("objective_id") != run.objective_id
                )
            ):
                raise OperationalStateError(
                    "cognitive_workspace_learning_binding_mismatch"
                )
            items.append(
                {
                    "candidate_id": row["candidate_id"],
                    "candidate_type": row["candidate_type"],
                    "candidate": candidate,
                    "candidate_digest": row["candidate_digest"],
                    "confidence_ppm": row["confidence_ppm"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "requires_user_approval": True,
                    "long_term_memory_write_allowed": False,
                }
            )
        return items

    def get_workspace(
        self,
        run_id: str,
        *,
        surface: str = "desktop",
    ) -> dict[str, Any]:
        from memory.cognitive_orchestration import contains_secret_material

        if surface not in {"desktop", "mobile"}:
            raise OperationalStateError("invalid_cognitive_workspace_surface")
        execution_store = self._execution_store()
        run = execution_store.get_run(run_id)
        objective = self._store().get_objective(
            run.objective_id,
            run.objective_version,
        )
        plan = self._planning_store().get_plan(run.plan_id, run.plan_version)
        provider = self._provider_store().get_provider(
            run.provider_id,
            run.provider_profile_version,
        )
        if (
            objective is None
            or objective["canonical_digest"] != run.objective_digest
            or plan is None
            or plan["plan_digest"] != run.plan_digest
            or provider is None
            or provider["profile_digest"] != run.provider_profile_digest
        ):
            raise OperationalStateError("cognitive_workspace_binding_mismatch")
        authorization = (
            None
            if run.authorization_id is None
            else execution_store.get_authorization(run.authorization_id)
        )
        if authorization is not None and (
            authorization.authority_digest != run.authorization_digest
            or authorization.objective_digest != run.objective_digest
            or authorization.plan_digest != run.plan_digest
            or authorization.project_id != objective["project_id"]
        ):
            raise OperationalStateError(
                "cognitive_workspace_authorization_mismatch"
            )
        checkpoint = execution_store.latest_checkpoint(run.run_id)
        proposal_records = [
            execution_store.get_tool_proposal(proposal_id)
            for proposal_id in checkpoint.proposal_ids
        ]
        if any(
            contains_secret_material(proposal.typed_arguments)
            for proposal in proposal_records
        ):
            raise OperationalStateError(
                "cognitive_workspace_secret_material_forbidden"
            )
        proposals = [proposal.to_dict() for proposal in proposal_records]
        execution_records = list(
            execution_store.list_tool_executions(run.run_id)
        )
        if any(
            contains_secret_material(execution.output)
            for execution in execution_records
        ):
            raise OperationalStateError(
                "cognitive_workspace_secret_material_forbidden"
            )
        executions = [execution.to_dict() for execution in execution_records]
        supervision = [
            event.to_dict()
            for event in execution_store.list_supervision_events(run.run_id)
        ]
        context = self._context_bindings(execution_store, run)
        verifications = self._verifications(
            execution_store,
            run.run_id,
            execution_records,
        )
        outcomes = []
        if authorization is not None:
            outcomes = [
                item
                for item in execution_store.operational.list_outcomes(
                    delegation_id=authorization.delegation_id
                )
                if item["runtime_run_id"] == run.run_id
            ]
        if any(
            contains_secret_material(item["outcome"])
            for item in outcomes
        ):
            raise OperationalStateError(
                "cognitive_workspace_secret_material_forbidden"
            )
        learning = self._learning_candidates(
            execution_store,
            run,
            objective["project_id"],
            tuple(item["outcome_id"] for item in outcomes),
        )
        current_outcome = outcomes[-1] if outcomes else None
        bundle = {
            "workspace_schema_version": 1,
            "surface": surface,
            "run": run.to_dict(),
            "objective": objective,
            "plan": plan,
            "context": self._collection(context),
            "authorization": (
                None
                if authorization is None
                else self._public_authorization(authorization)
            ),
            "provider": self._public_provider(provider),
            "checkpoint": checkpoint.to_dict(),
            "proposals": self._collection(proposals),
            "executions": self._collection(executions),
            "supervision": self._collection(supervision),
            "verifications": self._collection(verifications),
            "outcome": {
                "current": current_outcome,
                "items": outcomes,
                "count": len(outcomes),
            },
            "learning": self._collection(learning),
            "boundaries": {
                "readonly": True,
                "write_api_available": False,
                "mobile_write_allowed": False,
                "tailnet_write_allowed": False,
                "acceptance_write_allowed": False,
                "authority_expansion_allowed": False,
            },
        }
        return bundle


class App:
    def __init__(
        self, db_path: Path, real: bool = False, readonly: bool = False,
        profile_admin_session: Any = None, runtime_execution_session: Any = None,
        runtime_execution_service: Any = None, runtime_mock: bool = False,
        runtime_review: bool = False, service_mode: str = "web",
        bound_host: str = "", bound_port: int = 0, server_instance_id: str = "",
        operational_state_path: Path | None = None,
    ):
        self.db_path = db_path
        self.readonly = readonly
        self.profile_admin_session = profile_admin_session
        self.profile_admin = profile_admin_session is not None
        self.runtime_execution_session = runtime_execution_session
        self.runtime_execution_service = runtime_execution_service
        self.runtime_admin = (
            runtime_execution_session is not None and runtime_execution_service is not None
        )
        self.runtime_mock = bool(runtime_mock and self.runtime_admin)
        self.runtime_review = bool(
            runtime_review and runtime_execution_service is not None and not self.runtime_admin
        )
        self.execution_authority_present = runtime_execution_session is not None
        self.can_execute = bool(self.runtime_admin and self.execution_authority_present)
        self.service_mode = str(service_mode)
        self.service_readonly = bool(readonly and not self.runtime_admin)
        self.bound_host = str(bound_host)
        self.bound_port = int(bound_port)
        self.process_id = os.getpid()
        self.server_instance_id = str(server_instance_id)
        endpoint_host = f"[{self.bound_host}]" if ":" in self.bound_host else self.bound_host
        self.bound_endpoint = (
            f"http://{endpoint_host}:{self.bound_port}"
            if self.bound_host and self.bound_port
            else ""
        )
        self.db_mode = "real" if real or db_path == REAL_DB_PATH.resolve() else "test"
        self.db_path_label = "real database" if self.db_mode == "real" else "test database"
        self.projects_path = PRIVATE_PROJECTS_PATH if self.db_mode == "real" else PUBLIC_PROJECTS_PATH
        self.operational_state_service = OperationalStateContextService(
            operational_state_path or DEFAULT_OPERATIONAL_STATE_PATH
        )
        self.cognitive_state_service = CognitiveObjectiveReadonlyService(
            operational_state_path or DEFAULT_OPERATIONAL_STATE_PATH
        )

    def connect(self) -> sqlite3.Connection:
        if self.readonly:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_marks_schema(self) -> None:
        if self.readonly:
            return
        with self.connect() as conn:
            conn.executescript(MARKS_SQL)
            conn.commit()

    def load_projects(self) -> list[dict]:
        if not self.projects_path.is_file():
            raise FileNotFoundError(f"Project config not found: {self.projects_path.name}")
        try:
            raw = json.loads(self.projects_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Project config is invalid JSON: {exc.msg}") from exc
        if not isinstance(raw, list):
            raise ValueError("Project config must be a JSON array")
        projects = [normalize_project(item, index + 1) for index, item in enumerate(raw)]
        return sorted(projects, key=lambda project: (project["priority"], project["name"]))


def rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


OPTIMIZER_CANDIDATE_FIELDS = (
    "summary_limit",
    "raw_memory_top_k",
    "include_general",
    "total_chars",
    "estimated_total_tokens",
    "summary_count",
    "raw_memory_count",
    "general_context_count",
    "summary_before_raw_memory",
    "enabled_sections",
    "score",
)


def context_optimizer_payload(result: dict, limit: int = 5, max_tokens: int | None = None) -> dict:
    candidates = [
        context_optimizer_candidate(candidate, max_tokens=max_tokens)
        for candidate in result.get("candidates", [])
    ]
    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("score", 0)),
            int(item.get("estimated_total_tokens", 0)),
            -int(item.get("summary_count", 0)),
            -int(item.get("raw_memory_count", 0)),
            bool(item.get("include_general", False)),
            int(item.get("summary_limit", 0)),
            int(item.get("raw_memory_top_k", 0)),
        ),
    )
    top_configs = ranked[:limit]
    recommended = top_configs[0] if top_configs else {}
    recommended_config = {
        "summary_limit": recommended.get("summary_limit"),
        "raw_memory_top_k": recommended.get("raw_memory_top_k"),
        "include_general": recommended.get("include_general", False),
    }
    return {
        "candidate_count": len(candidates),
        "candidates": candidates,
        "top_configs": top_configs,
        "recommended_config": recommended_config,
        "reason": context_optimizer_reason(recommended, max_tokens=max_tokens) if recommended else "",
        "max_tokens": max_tokens,
    }


def context_optimizer_candidate(candidate: dict, max_tokens: int | None = None) -> dict:
    item = {field: candidate.get(field) for field in OPTIMIZER_CANDIDATE_FIELDS}
    if max_tokens is not None:
        tokens = int(item.get("estimated_total_tokens") or 0)
        item["max_tokens"] = max_tokens
        item["over_max_tokens"] = tokens > max_tokens
        item["score"] = context_optimizer_score(item, max_tokens=max_tokens)
    return item


def context_optimizer_score(candidate: dict, max_tokens: int) -> float:
    score = 1000.0
    score -= int(candidate.get("estimated_total_tokens") or 0) * 0.1
    if int(candidate.get("summary_count") or 0) > 0:
        score += 120.0
    if int(candidate.get("raw_memory_count") or 0) > 0:
        score += 100.0
    if int(candidate.get("general_context_count") or 0) == 0:
        score += 30.0
    if candidate.get("summary_before_raw_memory"):
        score += 60.0
    if int(candidate.get("estimated_total_tokens") or 0) > max_tokens:
        score -= 250.0
    return round(score, 2)


def context_optimizer_reason(candidate: dict, max_tokens: int | None = None) -> str:
    parts = [
        f"score={candidate.get('score')}",
        f"estimated_total_tokens={candidate.get('estimated_total_tokens')}",
        f"summary_count={candidate.get('summary_count')}",
        f"raw_memory_count={candidate.get('raw_memory_count')}",
        f"general_context_count={candidate.get('general_context_count')}",
        f"summary_before_raw_memory={str(bool(candidate.get('summary_before_raw_memory'))).lower()}",
    ]
    if max_tokens is not None:
        parts.append(f"max_tokens={max_tokens}")
    return "Recommended because it balances token cost with available whale summaries/raw memory: " + ", ".join(parts) + "."


class AyunOSHandler(SimpleHTTPRequestHandler):
    app: App
    MAX_REQUEST_BODY_BYTES = 1024 * 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args) -> None:
        print(f"[local-web] {self.address_string()} - {format % args}")

    def log_request(self, code="-", size="-") -> None:
        # Search terms and other private API parameters must not enter logs.
        path = urlparse(self.path).path
        self.log_message('"%s %s" %s %s', self.command, path, code, size)

    def send_json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": message}, status)

    def send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if parsed.path.startswith("/api/cognitive/"):
                try:
                    query = parse_qs(
                        parsed.query,
                        keep_blank_values=True,
                        strict_parsing=True,
                    )
                except ValueError:
                    self.send_error_json(
                        "invalid_query_string",
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
            else:
                query = parse_qs(parsed.query)
            self.handle_api_get(parsed.path, query)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        # Rejected or malformed bodies must never become a subsequent request.
        self.close_connection = True
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        if self.app.runtime_review is True and parsed.path == "/api/runtime-execute":
            self.send_json({
                "error": "Review-only mode: execution authority is absent. Sending is unavailable.",
                "reason_code": "runtime_review_execution_forbidden",
                "runtime_review": True,
                "execution_authority_present": False,
                "can_execute": False,
            }, HTTPStatus.FORBIDDEN)
            return
        profile_admin_mutation = bool(
            self.app.profile_admin and parsed.path in {
                "/api/approved-profile-decision",
                "/api/profile-enablement-proposal",
                "/api/profile-enablement-decision",
            }
        )
        runtime_admin_mutation = bool(
            self.app.runtime_admin
            and parsed.path == "/api/runtime-execute"
        )
        if self.app.readonly and not (profile_admin_mutation or runtime_admin_mutation):
            self.send_error_json("Readonly mode: writes are disabled", HTTPStatus.FORBIDDEN)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self.send_error_json("Transfer-Encoding is not supported")
            return
        lengths = (
            self.headers.get_all("Content-Length", [])
            if hasattr(self.headers, "get_all")
            else [self.headers.get("Content-Length", "0")]
        )
        value = lengths[0] if lengths else "0"
        if len(lengths) > 1 or not value.isascii() or not value.isdecimal():
            self.send_error_json("Invalid Content-Length")
            return
        try:
            length = int(value)
        except ValueError:
            self.send_error_json("Invalid Content-Length")
            return
        if length > self.MAX_REQUEST_BODY_BYTES:
            self.send_error_json("Request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw = self.rfile.read(length) if length else b"{}"
        if length and len(raw) != length:
            self.send_error_json("Incomplete request body")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_json("Invalid JSON")
            return
        self.handle_api_post(parsed.path, payload)

    def handle_api_get(self, path: str, params: dict[str, list[str]]) -> None:
        try:
            if path == "/api/recent":
                self.api_recent(params)
            elif path == "/api/status":
                self.api_status()
            elif path == "/api/health":
                self.api_health()
            elif path == "/api/search":
                self.api_search(params)
            elif path == "/api/message-context":
                self.api_message_context(params)
            elif path == "/api/conversation":
                self.api_conversation(params)
            elif path == "/api/conversation-messages":
                self.api_conversation_messages(params)
            elif path == "/api/projects":
                self.api_projects(params)
            elif path == "/api/project":
                self.api_project(params)
            elif path == "/api/timeline":
                self.api_timeline(params)
            elif path == "/api/memory-summaries":
                self.api_memory_summaries(params)
            elif path == "/api/memory-summary-health":
                self.api_memory_summary_health(params)
            elif path == "/api/whale-prompt-preview":
                self.api_whale_prompt_preview(params)
            elif path == "/api/whale-context-optimizer":
                self.api_whale_context_optimizer(params)
            elif path == "/api/semantic-summary-search-preview":
                self.api_semantic_summary_search_preview(params)
            elif path == "/api/knowledge-graph-query":
                self.api_knowledge_graph_query(params)
            elif path == "/api/graph-enhanced-retrieval":
                self.api_graph_enhanced_retrieval(params)
            elif path == "/api/memory-package-preview":
                self.api_memory_package_preview(params)
            elif path == "/api/evidence-selection-preview":
                self.api_evidence_selection_preview(params)
            elif path == "/api/temporal-memory-candidates-preview":
                self.api_temporal_memory_candidates_preview(params)
            elif path == "/api/context-assembly-preview":
                self.api_context_assembly_preview(params)
            elif path == "/api/context-delivery-preview":
                self.api_context_delivery_preview(params)
            elif path == "/api/approved-profile-control":
                self.api_approved_profile_control(params)
            elif path == "/api/approved-profile-active":
                self.api_approved_profile_active(params)
            elif path == "/api/profile-enablement-control":
                self.api_profile_enablement_control(params)
            elif path == "/api/profile-enablement-active":
                self.api_profile_enablement_active(params)
            elif path == "/api/runtime-preview":
                self.api_runtime_preview(params)
            elif path == "/api/runtime-run":
                self.api_runtime_run(params)
            elif path == "/api/runtime-sidecar-status":
                self.api_runtime_sidecar_status(params)
            elif path == "/api/cognitive/objectives":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/objective":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/objective-candidates":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/capabilities":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/plans":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/plan":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/providers":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/provider":
                self.api_cognitive_objectives(path, params)
            elif path == "/api/cognitive/runs":
                self.api_cognitive_workspace(path, params)
            elif path == "/api/cognitive/workspace":
                self.api_cognitive_workspace(path, params)
            elif path == "/api/marks":
                self.api_marks()
            elif path == "/api/tags":
                self.api_tags()
            elif path == "/api/tag":
                self.api_tag(params)
            elif path.startswith("/api/operational-state/"):
                self.api_operational_state(path, params)
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
        except (FileNotFoundError, ValueError) as exc:
            self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        except sqlite3.Error as exc:
            print(f"[local-web] database error: {exc}")
            self.send_error_json("Database connection failed", HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            print(f"[local-web] unexpected API error: {exc.__class__.__name__}")
            self.send_error_json("Unexpected server error", HTTPStatus.INTERNAL_SERVER_ERROR)

    def api_cognitive_objectives(
        self,
        path: str,
        params: dict[str, list[str]],
    ) -> None:
        allowed_by_path = {
            "/api/cognitive/objectives": {"project_id", "limit"},
            "/api/cognitive/objective": {"objective_id", "version"},
            "/api/cognitive/objective-candidates": {
                "project_id",
                "status",
                "limit",
            },
            "/api/cognitive/capabilities": {
                "risk_class",
                "availability",
                "executor_kind",
                "limit",
            },
            "/api/cognitive/plans": {
                "objective_id",
                "project_id",
                "limit",
            },
            "/api/cognitive/plan": {"plan_id", "version"},
            "/api/cognitive/providers": {
                "provider_id",
                "enabled",
                "health_state",
                "limit",
            },
            "/api/cognitive/provider": {
                "provider_id",
                "profile_version",
            },
        }
        allowed = allowed_by_path.get(path)
        if allowed is None:
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self.send_json(
                {
                    "error": "Unsupported cognitive objective parameter",
                    "reason_code": "unsupported_cognitive_objective_parameter",
                    "parameters": unknown,
                    "readonly": True,
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        def optional(name: str) -> str | None:
            values = params.get(name)
            if values is None:
                return None
            if len(values) != 1 or not values[0]:
                raise ValueError(f"invalid_cognitive_objective_{name}")
            return values[0]

        def positive_integer(name: str, *, default: int | None = None) -> int | None:
            raw = optional(name)
            if raw is None:
                return default
            try:
                value = int(raw, 10)
            except ValueError:
                raise ValueError(f"invalid_cognitive_objective_{name}") from None
            maximum = 100 if name == "limit" else None
            if value < 1 or (maximum is not None and value > maximum):
                raise ValueError(f"invalid_cognitive_objective_{name}")
            return value

        def optional_boolean(name: str) -> bool | None:
            raw = optional(name)
            if raw is None:
                return None
            if raw == "true":
                return True
            if raw == "false":
                return False
            raise ValueError(f"invalid_cognitive_objective_{name}")

        try:
            service = self.app.cognitive_state_service
            if path == "/api/cognitive/objectives":
                payload = service.list_objectives(
                    project_id=optional("project_id"),
                    limit=positive_integer("limit", default=50),
                )
            elif path == "/api/cognitive/objective":
                objective_id = optional("objective_id")
                if objective_id is None:
                    raise ValueError("objective_id_required")
                payload = service.get_objective(
                    objective_id,
                    version=positive_integer("version"),
                )
            elif path == "/api/cognitive/objective-candidates":
                payload = service.list_candidates(
                    project_id=optional("project_id"),
                    status=optional("status"),
                    limit=positive_integer("limit", default=50),
                )
            elif path == "/api/cognitive/capabilities":
                payload = service.list_capabilities(
                    risk_class=optional("risk_class"),
                    availability=optional("availability"),
                    executor_kind=optional("executor_kind"),
                    limit=positive_integer("limit", default=100),
                )
            elif path == "/api/cognitive/plans":
                payload = service.list_plans(
                    objective_id=optional("objective_id"),
                    project_id=optional("project_id"),
                    limit=positive_integer("limit", default=50),
                )
            elif path == "/api/cognitive/plan":
                plan_id = optional("plan_id")
                if plan_id is None:
                    raise ValueError("plan_id_required")
                payload = service.get_plan(
                    plan_id,
                    version=positive_integer("version"),
                )
            elif path == "/api/cognitive/providers":
                payload = service.list_providers(
                    provider_id=optional("provider_id"),
                    enabled=optional_boolean("enabled"),
                    health_state=optional("health_state"),
                    limit=positive_integer("limit", default=100),
                )
            else:
                provider_id = optional("provider_id")
                if provider_id is None:
                    raise ValueError("provider_id_required")
                payload = service.get_provider(
                    provider_id,
                    profile_version=positive_integer("profile_version"),
                )
            if payload is None:
                self.send_json(
                    {
                        "error": "Cognitive state record was not found",
                        "reason_code": (
                            "plan_not_found"
                            if path == "/api/cognitive/plan"
                            else "provider_profile_not_found"
                            if path == "/api/cognitive/provider"
                            else "objective_not_found"
                        ),
                        "readonly": True,
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
        except OperationalStateError as exc:
            unavailable = exc.reason_code in {
                "cognitive_schema_missing",
                "cognitive_schema_incomplete",
                "cognitive_schema_marker_mismatch",
                "cognitive_schema_drift",
                "operational_database_missing",
                "operational_schema_missing",
                "operational_schema_drift",
            }
            self.send_json(
                {
                    "error": "Cognitive objective request failed",
                    "reason_code": exc.reason_code,
                    "readonly": True,
                },
                (
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if unavailable
                    else HTTPStatus.BAD_REQUEST
                ),
            )
            return
        except (AttributeError, FileNotFoundError) as exc:
            self.send_json(
                {
                    "error": "Cognitive objective state is unavailable",
                    "reason_code": "cognitive_state_unavailable",
                    "readonly": True,
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except ValueError as exc:
            self.send_json(
                {
                    "error": "Cognitive objective request failed",
                    "reason_code": str(exc),
                    "readonly": True,
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        response = dict(payload) if isinstance(payload, dict) else {"items": payload}
        response["readonly"] = True
        response["cache_control"] = "no-store"
        self.send_json(response)

    def api_cognitive_workspace(
        self,
        path: str,
        params: dict[str, list[str]],
    ) -> None:
        allowed_by_path = {
            "/api/cognitive/runs": {"project_id", "status", "limit"},
            "/api/cognitive/workspace": {"run_id", "surface"},
        }
        allowed = allowed_by_path.get(path)
        if allowed is None:
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self.send_json(
                {
                    "error": "Unsupported cognitive workspace parameter",
                    "reason_code": "unsupported_cognitive_workspace_parameter",
                    "parameters": unknown,
                    "readonly": True,
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        def optional(name: str) -> str | None:
            values = params.get(name)
            if values is None:
                return None
            if len(values) != 1 or not values[0]:
                raise ValueError(f"invalid_cognitive_workspace_{name}")
            return values[0]

        def limit() -> int:
            raw = optional("limit")
            if raw is None:
                return 50
            try:
                value = int(raw, 10)
            except ValueError:
                raise ValueError(
                    "invalid_cognitive_workspace_limit"
                ) from None
            if not 1 <= value <= 100:
                raise ValueError("invalid_cognitive_workspace_limit")
            return value

        try:
            service = self.app.cognitive_state_service
            if path == "/api/cognitive/runs":
                payload = service.list_runs(
                    project_id=optional("project_id"),
                    status=optional("status"),
                    limit=limit(),
                )
            else:
                run_id = optional("run_id")
                if run_id is None:
                    raise ValueError("cognitive_workspace_run_id_required")
                surface = optional("surface") or "desktop"
                if surface not in {"desktop", "mobile"}:
                    raise ValueError("invalid_cognitive_workspace_surface")
                payload = service.get_workspace(
                    run_id,
                    surface=surface,
                )
        except OperationalStateError as exc:
            if exc.reason_code == "cognitive_run_not_found":
                status = HTTPStatus.NOT_FOUND
            elif exc.reason_code in {
                "invalid_project_id",
                "invalid_cognitive_run_status",
                "invalid_cognitive_workspace_limit",
                "invalid_cognitive_workspace_surface",
            }:
                status = HTTPStatus.BAD_REQUEST
            else:
                # Schema absence, projection drift, tampering and secret
                # material are availability failures for this aggregate view.
                status = HTTPStatus.SERVICE_UNAVAILABLE
            self.send_json(
                {
                    "error": "Cognitive workspace request failed",
                    "reason_code": exc.reason_code,
                    "readonly": True,
                },
                status,
            )
            return
        except (AttributeError, FileNotFoundError):
            self.send_json(
                {
                    "error": "Cognitive workspace state is unavailable",
                    "reason_code": "cognitive_workspace_unavailable",
                    "readonly": True,
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except ValueError as exc:
            self.send_json(
                {
                    "error": "Cognitive workspace request failed",
                    "reason_code": str(exc),
                    "readonly": True,
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        response = dict(payload)
        response["readonly"] = True
        response["cache_control"] = "no-store"
        self.send_json(response)

    def api_operational_state(
        self, path: str, params: dict[str, list[str]]
    ) -> None:
        allowed_by_path = {
            "/api/operational-state/status": set(),
            "/api/operational-state/now": {"budget", "as_of"},
            "/api/operational-state/projects": set(),
            "/api/operational-state/project": {"project_id"},
            "/api/operational-state/resume": {
                "project_id",
                "budget",
                "as_of",
            },
            "/api/operational-state/waiting": {"project_id"},
            "/api/operational-state/decisions": {"project_id"},
            "/api/operational-state/delegations": {"project_id"},
            "/api/operational-state/conflicts": {"project_id"},
            "/api/operational-state/freshness": {
                "project_id",
                "freshness",
            },
            "/api/operational-state/evidence": {"project_id"},
        }
        allowed = allowed_by_path.get(path)
        if allowed is None:
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self.send_json(
                {
                    "error": "Unsupported operational-state parameter",
                    "reason_code": "unsupported_operational_state_parameter",
                    "parameters": unknown,
                },
                HTTPStatus.BAD_REQUEST,
            )
            return

        def optional(name: str) -> str | None:
            values = params.get(name)
            if values is None:
                return None
            if len(values) != 1 or not values[0]:
                raise OperationalStateError(
                    f"invalid_operational_state_{name}"
                )
            return values[0]

        try:
            service = self.app.operational_state_service
            project_id = optional("project_id")
            if path == "/api/operational-state/status":
                payload = service.status_summary()
            elif path == "/api/operational-state/now":
                payload = service.now_package(
                    budget_profile=optional("budget") or "Medium",
                    as_of=optional("as_of"),
                )
            elif path == "/api/operational-state/projects":
                payload = service.project_list()
            elif path == "/api/operational-state/project":
                if project_id is None:
                    raise OperationalStateError("project_id_required")
                payload = service.project_detail(project_id)
            elif path == "/api/operational-state/resume":
                if project_id is None:
                    raise OperationalStateError("project_id_required")
                payload = service.resume_package(
                    project_id,
                    budget_profile=optional("budget") or "Medium",
                    as_of=optional("as_of"),
                )
            elif path == "/api/operational-state/waiting":
                payload = service.filtered_state(
                    state_name="waiting", project_id=project_id
                )
            elif path == "/api/operational-state/decisions":
                payload = service.filtered_state(
                    entity_type="decision", project_id=project_id
                )
            elif path == "/api/operational-state/delegations":
                payload = service.delegation_overview(
                    project_id=project_id
                )
            elif path == "/api/operational-state/conflicts":
                payload = service.conflicts(project_id=project_id)
            elif path == "/api/operational-state/freshness":
                payload = service.filtered_state(
                    freshness=optional("freshness"),
                    project_id=project_id,
                )
            else:
                payload = service.source_evidence(project_id=project_id)
        except OperationalStateError as exc:
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if exc.reason_code == "operational_state_unavailable"
                else (
                    HTTPStatus.NOT_FOUND
                    if exc.reason_code == "operational_project_not_found"
                    else HTTPStatus.BAD_REQUEST
                )
            )
            self.send_json(
                {
                    "error": "Operational state request failed",
                    "reason_code": exc.reason_code,
                    "readonly": True,
                },
                status,
            )
            return
        self.send_json(payload)

    def api_status(self) -> None:
        self.send_json(
            {
                "db_mode": self.app.db_mode,
                "service_mode": self.app.service_mode,
                "readonly": self.app.service_readonly,
                "db_path_label": self.app.db_path_label,
                "readonly_source_tables": True,
                "profile_admin": self.app.profile_admin,
                "runtime_review": self.app.runtime_review,
                "runtime_admin": self.app.runtime_admin,
                "runtime_mock": self.app.runtime_mock,
                "execution_authority_present": self.app.execution_authority_present,
                "can_execute": self.app.can_execute,
                "bound_host": self.app.bound_host,
                "bound_port": self.app.bound_port,
                "bound_endpoint": self.app.bound_endpoint,
                "process_id": self.app.process_id,
                "server_instance_id": self.app.server_instance_id,
                "runtime_sidecar": production_runtime_status(),
            }
        )

    def api_knowledge_graph_query(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "conversation_id", "memory_space", "relation_type", "node_id",
            "max_depth", "limit", "include_candidates",
        }
        unknown = sorted(set(params) - allowed)
        if unknown:
            self.send_error_json("Unsupported knowledge graph query parameter", HTTPStatus.BAD_REQUEST)
            return
        include_candidates = (params.get("include_candidates") or ["false"])[0].strip().lower()
        if include_candidates not in {"true", "false", "1", "0"}:
            self.send_error_json("Invalid include_candidates value", HTTPStatus.BAD_REQUEST)
            return
        try:
            query = KnowledgeGraphQuery(
                conversation_id=(params.get("conversation_id") or [None])[0],
                memory_space=(params.get("memory_space") or [None])[0],
                relation_type=(params.get("relation_type") or [None])[0],
                node_id=(params.get("node_id") or [None])[0],
                max_depth=int((params.get("max_depth") or ["1"])[0]),
                limit=int((params.get("limit") or ["25"])[0]),
                include_candidates=include_candidates in {"true", "1"},
            )
            self.send_json(query_production_knowledge_graph(query))
        except ValueError:
            self.send_error_json("Invalid knowledge graph query", HTTPStatus.BAD_REQUEST)

    def api_graph_enhanced_retrieval(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported graph-enhanced retrieval parameter", HTTPStatus.BAD_REQUEST)
            return
        query_text = (params.get("q") or [""])[0]
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )
        try:
            request = RetrievalQuery(
                query=query_text,
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            request.normalized()
            service = GraphEnhancedRetrievalService(source_db=self.app.db_path)
            self.send_json(service.search(request))
        except (RetrievalValidationError, ValueError):
            self.send_error_json("Invalid graph-enhanced retrieval query", HTTPStatus.BAD_REQUEST)

    def api_memory_package_preview(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space", "future_token_budget",
            "format",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported memory package parameter", HTTPStatus.BAD_REQUEST)
            return
        if not (
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        ):
            self.send_json({
                "protocol_version": MEMORY_PACKAGE_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "production_readonly_required",
                "readonly": True,
                "persisted": False,
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        output_format = (params.get("format") or ["json"])[0].strip().lower()
        if output_format not in {"json", "text"}:
            self.send_error_json("Invalid memory package format", HTTPStatus.BAD_REQUEST)
            return
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )
        raw_token_budget = (params.get("future_token_budget") or [""])[0].strip()
        try:
            request = RetrievalQuery(
                query=(params.get("q") or [""])[0],
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            package = build_production_memory_package(
                request,
                future_token_budget=int(raw_token_budget) if raw_token_budget else None,
            )
            if output_format == "text":
                self.send_text(render_memory_package(package))
            else:
                self.send_json(package)
        except (RetrievalValidationError, MemoryPackageValidationError, ValueError):
            self.send_error_json("Invalid memory package query", HTTPStatus.BAD_REQUEST)

    def api_evidence_selection_preview(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space", "budget",
            "character_budget", "estimated_token_budget", "conversation_cap", "max_selected", "format",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported evidence selection parameter", HTTPStatus.BAD_REQUEST)
            return
        if not (
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        ):
            self.send_json({
                "protocol_version": EVIDENCE_SELECTION_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "production_readonly_required",
                "readonly": True,
                "persisted": False,
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        output_format = (params.get("format") or ["json"])[0].strip().lower()
        if output_format not in {"json", "text"}:
            self.send_error_json("Invalid evidence selection format", HTTPStatus.BAD_REQUEST)
            return
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )

        def optional_int(name: str) -> int | None:
            raw = (params.get(name) or [""])[0].strip()
            return int(raw) if raw else None

        try:
            request = RetrievalQuery(
                query=(params.get("q") or [""])[0],
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            package = build_production_evidence_selection(
                request,
                budget_profile=(params.get("budget") or ["medium"])[0],
                character_budget=optional_int("character_budget"),
                estimated_token_budget=optional_int("estimated_token_budget"),
                per_conversation_cap=optional_int("conversation_cap"),
                max_selected=optional_int("max_selected"),
            )
            if output_format == "text":
                self.send_text(render_evidence_selection(package))
            else:
                self.send_json(package)
        except (
            RetrievalValidationError, MemoryPackageValidationError,
            EvidenceSelectionValidationError, ValueError,
        ):
            self.send_error_json("Invalid evidence selection query", HTTPStatus.BAD_REQUEST)

    def api_temporal_memory_candidates_preview(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space", "budget", "format",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported temporal candidate parameter", HTTPStatus.BAD_REQUEST)
            return
        if not (
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        ):
            self.send_json({
                "protocol_version": TEMPORAL_CANDIDATE_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "production_readonly_required",
                "readonly": True,
                "persisted": False,
                "candidate_only": True,
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        output_format = (params.get("format") or ["json"])[0].strip().lower()
        if output_format not in {"json", "text"}:
            self.send_error_json("Invalid temporal candidate format", HTTPStatus.BAD_REQUEST)
            return
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )
        try:
            request = RetrievalQuery(
                query=(params.get("q") or [""])[0],
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            package = build_production_temporal_candidates(
                request, budget_profile=(params.get("budget") or ["medium"])[0],
            )
            if output_format == "text":
                self.send_text(render_temporal_candidates(package))
            else:
                self.send_json(package)
        except (
            RetrievalValidationError, MemoryPackageValidationError,
            EvidenceSelectionValidationError, TemporalCandidateValidationError, ValueError,
        ):
            self.send_error_json("Invalid temporal candidate query", HTTPStatus.BAD_REQUEST)

    def api_context_assembly_preview(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space", "budget",
            "total_characters", "estimated_tokens", "max_evidence", "max_candidates",
            "max_history_expansions", "format",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported context assembly parameter", HTTPStatus.BAD_REQUEST)
            return
        if not (
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        ):
            self.send_json({
                "protocol_version": CONTEXT_ASSEMBLY_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "production_readonly_required",
                "readonly": True,
                "persisted": False,
                "external_model_calls": False,
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        output_format = (params.get("format") or ["json"])[0].strip().lower()
        if output_format not in {"json", "text"}:
            self.send_error_json("Invalid context assembly format", HTTPStatus.BAD_REQUEST)
            return
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )

        def optional_int(name: str) -> int | None:
            raw = (params.get(name) or [""])[0].strip()
            return int(raw) if raw else None

        try:
            request = RetrievalQuery(
                query=(params.get("q") or [""])[0],
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            package = build_production_context_assembly(
                request,
                budget_profile=(params.get("budget") or ["medium"])[0],
                total_characters=optional_int("total_characters"),
                estimated_tokens=optional_int("estimated_tokens"),
                max_evidence=optional_int("max_evidence"),
                max_candidates=optional_int("max_candidates"),
                max_history_expansions=optional_int("max_history_expansions"),
            )
            if output_format == "text":
                self.send_text(render_context_assembly(package))
            else:
                self.send_json(package)
        except (
            RetrievalValidationError,
            MemoryPackageValidationError,
            EvidenceSelectionValidationError,
            TemporalCandidateValidationError,
            ContextAssemblyValidationError,
            ValueError,
        ):
            self.send_error_json("Invalid context assembly query", HTTPStatus.BAD_REQUEST)

    def api_context_delivery_preview(self, params: dict[str, list[str]]) -> None:
        allowed = {
            "q", "start_time", "end_time", "result_mode", "context_window",
            "limit", "cursor", "hit_types", "graph_depth", "relation_budget",
            "path_budget", "context_budget", "memory_space", "budget",
            "total_characters", "estimated_tokens", "max_evidence", "max_candidates",
            "max_history_expansions", "exclude", "format",
        }
        if set(params) - allowed:
            self.send_error_json("Unsupported context delivery parameter", HTTPStatus.BAD_REQUEST)
            return
        if not (
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        ):
            self.send_json({
                "protocol_version": CONTEXT_DELIVERY_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "production_readonly_required",
                "execution": {
                    "readonly": True,
                    "persisted": False,
                    "sent_to_model": False,
                    "external_model_calls": False,
                    "temporary_adjustments_only": True,
                },
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        output_format = (params.get("format") or ["json"])[0].strip().lower()
        if output_format not in {"json", "text"}:
            self.send_error_json("Invalid context delivery format", HTTPStatus.BAD_REQUEST)
            return
        hit_types = tuple(
            item.strip() for item in (params.get("hit_types") or ["keyword,semantic,graph"])[0].split(",")
            if item.strip()
        )

        def optional_int(name: str) -> int | None:
            raw = (params.get(name) or [""])[0].strip()
            return int(raw) if raw else None

        exclusions = sorted({
            item.strip()
            for value in params.get("exclude", [])
            for item in value.split(",")
            if item.strip()
        })
        try:
            request = RetrievalQuery(
                query=(params.get("q") or [""])[0],
                start_time=(params.get("start_time") or [None])[0],
                end_time=(params.get("end_time") or [None])[0],
                result_mode=(params.get("result_mode") or ["snippets"])[0],
                context_window=int((params.get("context_window") or ["0"])[0]),
                limit=int((params.get("limit") or ["20"])[0]),
                cursor=(params.get("cursor") or [None])[0],
                hit_types=hit_types,
                graph_depth=int((params.get("graph_depth") or ["1"])[0]),
                relation_budget=int((params.get("relation_budget") or ["24"])[0]),
                path_budget=int((params.get("path_budget") or ["12"])[0]),
                context_budget=int((params.get("context_budget") or ["6000"])[0]),
                memory_space=(params.get("memory_space") or [None])[0],
            )
            delivery = build_production_context_delivery(
                request,
                budget_profile=(params.get("budget") or ["medium"])[0],
                total_characters=optional_int("total_characters"),
                estimated_tokens=optional_int("estimated_tokens"),
                max_evidence=optional_int("max_evidence"),
                max_candidates=optional_int("max_candidates"),
                max_history_expansions=optional_int("max_history_expansions"),
                excluded_item_ids=exclusions,
            )
            if output_format == "text":
                self.send_text(render_context_delivery(delivery))
            else:
                self.send_json(delivery)
        except (
            RetrievalValidationError,
            MemoryPackageValidationError,
            EvidenceSelectionValidationError,
            TemporalCandidateValidationError,
            ContextAssemblyValidationError,
            ContextDeliveryValidationError,
            ValueError,
        ):
            self.send_error_json("Invalid context delivery query", HTTPStatus.BAD_REQUEST)

    def profile_control_surface_available(self) -> bool:
        return bool(
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
        )

    def profile_mutation_surface_available(self) -> bool:
        return bool(self.profile_control_surface_available() and self.app.profile_admin)

    def runtime_surface_available(self) -> bool:
        return bool(
            self.app.readonly
            and self.app.db_mode == "real"
            and Path(self.app.db_path).resolve() == REAL_DB_PATH.resolve()
            and self.app.runtime_execution_service is not None
        )

    def runtime_mutation_surface_available(self) -> bool:
        return bool(
            self.runtime_surface_available()
            and self.app.runtime_admin is True
            and self.app.runtime_execution_session is not None
            and self.app.runtime_review is not True
        )

    def send_runtime_unavailable(self) -> None:
        self.send_json({
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "status": "unavailable",
            "reason_code": "production_readonly_required",
            "execution": {
                "persisted": False,
                "sent_to_model": False,
                "external_model_calls": False,
            },
        }, HTTPStatus.SERVICE_UNAVAILABLE)

    def api_runtime_preview(self, params: dict[str, list[str]]) -> None:
        if set(params) - {"q", "budget", "request_id", "mutation_id"}:
            self.send_error_json("Unsupported runtime preview parameter", HTTPStatus.BAD_REQUEST)
            return
        if not self.runtime_surface_available():
            self.send_runtime_unavailable()
            return
        question = (params.get("q") or [""])[0]
        if not question.strip():
            self.send_error_json("Missing runtime question", HTTPStatus.BAD_REQUEST)
            return
        request_id = (params.get("request_id") or [""])[0]
        mutation_id = (params.get("mutation_id") or [""])[0]
        try:
            self.send_json(self.app.runtime_execution_service.preview(
                question,
                budget_profile=(params.get("budget") or ["Medium"])[0],
                request_id=request_id,
                mutation_id=mutation_id,
            ))
        except ProductionRuntimeReviewError:
            self.send_error_json("Invalid runtime preview request", HTTPStatus.BAD_REQUEST)

    def api_runtime_run(self, params: dict[str, list[str]]) -> None:
        if set(params) != {"model_run_id"}:
            self.send_error_json("Runtime run query fields mismatch", HTTPStatus.BAD_REQUEST)
            return
        if not self.runtime_mutation_surface_available():
            self.send_runtime_unavailable()
            return
        try:
            self.send_json(self.app.runtime_execution_service.get_run(params["model_run_id"][0]))
        except Exception as exc:
            self.handle_runtime_exception(exc, invalid_message="Invalid runtime run request")

    def api_runtime_sidecar_status(self, params: dict[str, list[str]]) -> None:
        if params:
            self.send_error_json("Runtime sidecar status accepts no parameters", HTTPStatus.BAD_REQUEST)
            return
        if not self.runtime_surface_available():
            self.send_json({
                "protocol_version": PRODUCTION_RUNTIME_STATUS_PROTOCOL_VERSION,
                "status": "unavailable",
                "initialized": False,
                "ready": False,
                "runtime_state": "inspection_failed",
                "reason_code": "production_readonly_required",
                "readonly": True,
                "execution": {
                    "credential_read": False,
                    "sent_to_model": False,
                    "external_model_calls": False,
                },
            }, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json(production_runtime_status())

    def send_profile_control_unavailable(self) -> None:
        self.send_json({
            "protocol_version": APPROVED_PROFILE_CONTROL_PROTOCOL_VERSION,
            "status": "unavailable",
            "reason_code": "production_readonly_required",
            "execution": {
                "source_readonly": True,
                "source_persisted": False,
                "external_model_calls": False,
                "sent_to_model": False,
            },
        }, HTTPStatus.SERVICE_UNAVAILABLE)

    def api_approved_profile_control(self, params: dict[str, list[str]]) -> None:
        if set(params) - {"q", "budget"}:
            self.send_error_json("Unsupported approved profile control parameter", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_control_surface_available():
            self.send_profile_control_unavailable()
            return
        query_text = (params.get("q") or [""])[0].strip()
        if not query_text:
            self.send_error_json("Missing approved profile candidate query", HTTPStatus.BAD_REQUEST)
            return
        try:
            review = build_production_approved_profile_review(
                RetrievalQuery(query_text),
                budget_profile=(params.get("budget") or ["Medium"])[0],
            )
            review["mutation_available"] = bool(
                review["mutation_available"] and self.profile_mutation_surface_available()
            )
            review["write_mode"] = "profile_admin" if self.app.profile_admin else "readonly_preview"
            self.send_json(review)
        except (
            RetrievalValidationError,
            MemoryPackageValidationError,
            EvidenceSelectionValidationError,
            TemporalCandidateValidationError,
            ApprovedProfileControlError,
            ValueError,
        ):
            self.send_error_json("Invalid approved profile candidate query", HTTPStatus.BAD_REQUEST)

    def api_approved_profile_active(self, params: dict[str, list[str]]) -> None:
        if params:
            self.send_error_json("Approved profile projection accepts no parameters", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_control_surface_available():
            self.send_profile_control_unavailable()
            return
        try:
            self.send_json(active_approved_profile_projection())
        except ApprovedProfileSidecarUnavailable:
            self.send_json({
                "protocol_version": APPROVED_PROFILE_CONTROL_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "sidecar_invalid",
            }, HTTPStatus.SERVICE_UNAVAILABLE)

    def send_profile_enablement_unavailable(self) -> None:
        self.send_json({
            "protocol_version": PROFILE_ENABLEMENT_PROTOCOL_VERSION,
            "status": "unavailable",
            "reason_code": "production_readonly_required",
            "execution": {
                "source_readonly": True,
                "source_persisted": False,
                "external_model_calls": False,
                "sent_to_model": False,
            },
        }, HTTPStatus.SERVICE_UNAVAILABLE)

    def api_profile_enablement_control(self, params: dict[str, list[str]]) -> None:
        if set(params) - {"q", "budget"}:
            self.send_error_json("Unsupported profile enablement parameter", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_control_surface_available():
            self.send_profile_enablement_unavailable()
            return
        query_text = (params.get("q") or [""])[0].strip()
        if not query_text:
            self.send_error_json("Missing approved profile candidate query", HTTPStatus.BAD_REQUEST)
            return
        try:
            review = build_production_profile_enablement_review(
                RetrievalQuery(query_text),
                budget_profile=(params.get("budget") or ["Medium"])[0],
            )
            review["mutation_available"] = bool(
                review["mutation_available"] and self.profile_mutation_surface_available()
            )
            review["write_mode"] = "profile_admin" if self.app.profile_admin else "readonly_preview"
            self.send_json(review)
        except (
            RetrievalValidationError,
            MemoryPackageValidationError,
            EvidenceSelectionValidationError,
            TemporalCandidateValidationError,
            ProfileEnablementError,
            ValueError,
        ):
            self.send_error_json("Invalid profile enablement query", HTTPStatus.BAD_REQUEST)

    def api_profile_enablement_active(self, params: dict[str, list[str]]) -> None:
        if params:
            self.send_error_json("Profile enablement projection accepts no parameters", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_control_surface_available():
            self.send_profile_enablement_unavailable()
            return
        try:
            self.send_json(active_profile_enablement_projection())
        except ProfileEnablementSidecarUnavailable:
            self.send_json({
                "protocol_version": PROFILE_ENABLEMENT_PROTOCOL_VERSION,
                "status": "unavailable",
                "reason_code": "sidecar_invalid",
            }, HTTPStatus.SERVICE_UNAVAILABLE)

    def api_health(self) -> None:
        with self.app.connect() as conn:
            conversations_count = self.count_table(conn, "conversations")
            messages_count = self.count_table(conn, "messages")
            marks_count = self.count_table(conn, "conversation_marks")
            tags_count = self.count_table(conn, "conversation_tags")
        self.send_json(
            {
                "ok": True,
                "db_mode": self.app.db_mode,
                "readonly": self.app.readonly,
                "db_path_label": self.app.db_path_label,
                "conversations_count": conversations_count,
                "messages_count": messages_count,
                "marks_count": marks_count,
                "tags_count": tags_count,
                "source_trace_enabled": True,
                "project_hub_enabled": True,
                "timeline_enabled": True,
                "source_platforms_enabled": True,
                "available_source_platforms": [
                    {"value": value, "label": label}
                    for value, label in SOURCE_PLATFORM_LABELS.items()
                ],
                "long_mock_scenarios_enabled": True,
                "ui_language": "zh-CN",
            }
        )

    def count_table(self, conn: sqlite3.Connection, table_name: str) -> int:
        if not self.table_exists(conn, table_name):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])

    def table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return exists is not None

    def table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        if not self.table_exists(conn, table_name):
            return set()
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}

    def source_expr(self, conn: sqlite3.Connection, table_name: str, alias: str) -> str:
        if "source_platform" in self.table_columns(conn, table_name):
            return f"COALESCE(NULLIF({alias}.source_platform, ''), 'chatgpt')"
        return "'chatgpt'"

    def parse_source_platform(self, params: dict[str, list[str]]) -> str | None:
        value = (params.get("source_platform") or [""])[0].strip().lower()
        if not value:
            return ""
        if value not in SOURCE_PLATFORM_LABELS:
            self.send_error_json("Invalid source platform", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def parse_memory_space(self, params: dict[str, list[str]]) -> str | None:
        value = (params.get("memory_space") or ["whale"])[0].strip().lower() or "whale"
        if value not in MEMORY_SPACE_LABELS:
            self.send_error_json("Invalid memory space", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def parse_summary_type(self, params: dict[str, list[str]]) -> str | None:
        value = (params.get("summary_type") or [""])[0].strip()
        if not value:
            return ""
        if value not in {"conversation_summary", "memory_space_overview"}:
            self.send_error_json("Invalid summary type", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def parse_bounded_int_param(
        self,
        params: dict[str, list[str]],
        name: str,
        default: int,
        maximum: int,
    ) -> int | None:
        raw_value = (params.get(name) or [""])[0].strip()
        if not raw_value:
            return default
        try:
            value = int(raw_value)
        except ValueError:
            self.send_error_json(f"Invalid {name}", HTTPStatus.BAD_REQUEST)
            return None
        if value < 1 or value > maximum:
            self.send_error_json(f"Invalid {name}: must be between 1 and {maximum}", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def parse_bool_param(self, params: dict[str, list[str]], name: str, default: bool = False) -> bool | None:
        raw_value = (params.get(name) or [""])[0].strip().lower()
        if not raw_value:
            return default
        if raw_value in {"1", "true", "yes", "on"}:
            return True
        if raw_value in {"0", "false", "no", "off"}:
            return False
        self.send_error_json(f"Invalid {name}", HTTPStatus.BAD_REQUEST)
        return None

    def parse_optional_float_param(
        self,
        params: dict[str, list[str]],
        name: str,
        minimum: float,
        maximum: float,
    ) -> float | None | bool:
        raw_value = (params.get(name) or [""])[0].strip()
        if not raw_value:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            self.send_error_json(f"Invalid {name}", HTTPStatus.BAD_REQUEST)
            return False
        if value < minimum or value > maximum:
            self.send_error_json(f"Invalid {name}: must be between {minimum:g} and {maximum:g}", HTTPStatus.BAD_REQUEST)
            return False
        return value

    def source_filter_clause(self, source_expr: str, source_platform: str) -> tuple[str, list[str]]:
        if not source_platform:
            return "", []
        return f"{source_expr} = ?", [source_platform]

    def api_memory_summaries(self, params: dict[str, list[str]]) -> None:
        memory_space = self.parse_memory_space(params)
        if memory_space is None:
            return
        summary_type = self.parse_summary_type(params)
        if summary_type is None:
            return
        conversation_id = unquote((params.get("conversation_id") or [""])[0]).strip()
        limit = self.parse_limit(params)
        rows = list_summaries(
            self.app.db_path,
            memory_space=memory_space,
            summary_type=summary_type or None,
            conversation_id=conversation_id or None,
            limit=limit,
        )
        self.send_json(
            {
                "items": rows,
                "count": len(rows),
                "readonly": True,
                "db_mode": self.app.db_mode,
                "memory_space": memory_space,
                "summary_type": summary_type,
            }
        )

    def api_memory_summary_health(self, params: dict[str, list[str]]) -> None:
        memory_space = self.parse_memory_space(params)
        if memory_space is None:
            return
        summary_type = self.parse_summary_type(params)
        if summary_type is None:
            return
        conversation_id = unquote((params.get("conversation_id") or [""])[0]).strip()
        limit = self.parse_limit(params)
        rows = list_summaries(
            self.app.db_path,
            memory_space=memory_space,
            summary_type=summary_type or None,
            conversation_id=conversation_id or None,
            limit=limit,
        )
        self.send_json(
            {
                "items": [diagnose_summary(row) for row in rows],
                "overall": diagnose_summaries(rows),
                "readonly": True,
                "db_mode": self.app.db_mode,
                "memory_space": memory_space,
                "summary_type": summary_type,
            }
        )

    def api_whale_prompt_preview(self, params: dict[str, list[str]]) -> None:
        user_input = ((params.get("q") or params.get("input") or [""])[0]).strip()
        if not user_input:
            self.send_error_json("Missing q or input", HTTPStatus.BAD_REQUEST)
            return

        summary_limit = self.parse_bounded_int_param(params, "summary_limit", 5, 10)
        if summary_limit is None:
            return
        raw_memory_top_k = self.parse_bounded_int_param(params, "raw_memory_top_k", 5, 10)
        if raw_memory_top_k is None:
            return
        preview_chars = self.parse_bounded_int_param(params, "preview_chars", 300, 1000)
        if preview_chars is None:
            return
        include_general = self.parse_bool_param(params, "include_general", False)
        if include_general is None:
            return
        show_full_prompt = self.parse_bool_param(params, "show_full_prompt", False)
        if show_full_prompt is None:
            return

        report = build_whale_prompt_context(
            user_input,
            db_path=self.app.db_path,
            summary_limit=summary_limit,
            raw_memory_top_k=raw_memory_top_k,
            include_general=include_general,
            preview_chars=preview_chars,
        )
        report["readonly"] = True
        report["sent_to_model"] = False
        if not show_full_prompt:
            report.pop("prompt", None)
        self.send_json(report)

    def api_semantic_summary_search_preview(self, params: dict[str, list[str]]) -> None:
        query = ((params.get("q") or params.get("query") or [""])[0]).strip()
        if not query:
            self.send_error_json("Missing q or query", HTTPStatus.BAD_REQUEST)
            return
        raw_top_k = (params.get("top_k") or ["5"])[0].strip() or "5"
        try:
            top_k = max(1, min(int(raw_top_k), 20))
        except ValueError:
            self.send_error_json("Invalid top_k", HTTPStatus.BAD_REQUEST)
            return
        min_score = self.parse_optional_float_param(params, "min_score", -1.0, 1.0)
        if min_score is False:
            return
        memory_space = (params.get("memory_space") or [""])[0].strip() or None

        result = semantic_summary_search_preview(
            query,
            SEMANTIC_SUMMARY_DB_PATH,
            memory_space=memory_space,
            top_k=top_k,
            min_score=min_score,
        )
        result.update(
            {
                "readonly": True,
                "sent_to_model": False,
                "provider_is_test_only": True,
                "warning": (
                    "deterministic_test provider only validates the pipeline and does not represent "
                    "production semantic quality."
                ),
            }
        )
        self.send_json(result)

    def api_whale_context_optimizer(self, params: dict[str, list[str]]) -> None:
        user_input = ((params.get("q") or params.get("input") or [""])[0]).strip()
        if not user_input:
            self.send_error_json("Missing q or input", HTTPStatus.BAD_REQUEST)
            return
        test_general = self.parse_bool_param(params, "test_general", False)
        if test_general is None:
            return
        limit = self.parse_bounded_int_param(params, "limit", 5, 10)
        if limit is None:
            return
        max_tokens = self.parse_bounded_int_param(params, "max_tokens", 0, 100000)
        if max_tokens is None:
            return

        result = optimize_context(
            user_input,
            db_path=self.app.db_path,
            test_general=test_general,
        )
        payload = context_optimizer_payload(result, limit=limit, max_tokens=max_tokens or None)
        payload.update(
            {
                "readonly": True,
                "applied": False,
                "sent_to_model": False,
            }
        )
        self.send_json(payload)

    def add_source_fields(self, item: dict, value: str | None = None) -> dict:
        platform = (value or item.get("source_platform") or "chatgpt").strip() or "chatgpt"
        item["source_platform"] = platform
        item["source_platform_label"] = source_platform_label(platform)
        return item

    def conversation_marker(self, conn: sqlite3.Connection, conversation_id: str) -> dict:
        if not conversation_id:
            return {"status": "", "tags": ""}
        marker = self.marker_sql(conn)
        row = conn.execute(
            f"""
            SELECT
                {marker["status"]} AS status,
                {marker["tags"]} AS tags
            FROM conversations c
            {marker["conversation_joins"]}
            WHERE c.conversation_id = ?
            GROUP BY c.conversation_id
            """,
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else {"status": "", "tags": ""}

    def source_counts_for_where(
        self,
        conn: sqlite3.Connection,
        where_clause: str = "",
        params: list[str] | None = None,
    ) -> dict[str, int]:
        params = params or []
        source_expr = self.source_expr(conn, "conversations", "c")
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        rows = conn.execute(
            f"""
            SELECT {source_expr} AS source_platform, COUNT(DISTINCT c.conversation_id) AS count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.conversation_id
            {where_sql}
            GROUP BY 1
            """,
            params,
        ).fetchall()
        return {row["source_platform"] or "chatgpt": int(row["count"] or 0) for row in rows}

    def marker_sql(self, conn: sqlite3.Connection) -> dict[str, str]:
        marks_exists = self.table_exists(conn, "conversation_marks")
        tags_exists = self.table_exists(conn, "conversation_tags")
        joins = []
        if marks_exists:
            joins.append("LEFT JOIN conversation_marks cm ON cm.conversation_id = c.conversation_id")
        if tags_exists:
            joins.append("LEFT JOIN conversation_tags ct ON ct.conversation_id = c.conversation_id")
        return {
            "conversation_joins": "\n".join(joins),
            "message_joins": "\n".join(join.replace("c.conversation_id", "m.conversation_id") for join in joins),
            "status": "COALESCE(cm.status, '')" if marks_exists else "''",
            "note": "COALESCE(cm.note, '')" if marks_exists else "''",
            "tags": "COALESCE(group_concat(DISTINCT ct.tag), '')" if tags_exists else "''",
        }

    def handle_runtime_exception(
        self, exc: Exception, *, invalid_message: str = "Invalid runtime request",
    ) -> None:
        # Execution-only types are imported only after an explicit admin route
        # has raised.  Runtime-review startup, preview, and rejection never load
        # the production execution facade or provider adapter module.
        from memory.production_runtime_execution import (
            ProductionRuntimeExecutionError,
            RuntimeIdempotencyConflict,
            RuntimePreviewMismatch,
            RuntimeSidecarUnavailable,
        )

        if isinstance(exc, RuntimeIdempotencyConflict):
            self.send_json({
                "error": "request_id or mutation_id conflict",
                "reason_code": "idempotency_conflict",
            }, HTTPStatus.CONFLICT)
        elif isinstance(exc, RuntimeSidecarUnavailable):
            self.send_json({
                "error": "runtime sidecar is not initialized",
                "reason_code": "sidecar_not_initialized",
            }, HTTPStatus.SERVICE_UNAVAILABLE)
        elif isinstance(exc, RuntimePreviewMismatch):
            self.send_json({
                "error": "runtime preview no longer matches the execution request",
                "reason_code": "runtime_preview_binding_mismatch",
            }, HTTPStatus.CONFLICT)
        elif isinstance(exc, ProductionRuntimeExecutionError):
            self.send_error_json(invalid_message, HTTPStatus.BAD_REQUEST)
        elif isinstance(exc, sqlite3.Error):
            print(f"[local-web] database error: {exc}")
            self.send_error_json("Database connection failed", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            print(f"[local-web] unexpected API error: {exc.__class__.__name__}")
            self.send_error_json("Unexpected server error", HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_api_post(self, path: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            self.send_error_json("Invalid JSON")
            return
        if path == "/api/runtime-execute":
            try:
                self.api_runtime_execute(payload)
            except Exception as exc:
                self.handle_runtime_exception(exc)
            return
        try:
            if path == "/api/conversation/tag":
                self.api_add_tag(payload)
            elif path == "/api/conversation/mark":
                self.api_mark(payload)
            elif path == "/api/approved-profile-decision":
                self.api_approved_profile_decision(payload)
            elif path == "/api/profile-enablement-proposal":
                self.api_profile_enablement_proposal(payload)
            elif path == "/api/profile-enablement-decision":
                self.api_profile_enablement_decision(payload)
            else:
                self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
        except (ApprovedProfileRevisionConflict, ProfileEnablementRevisionConflict) as exc:
            self.send_json({
                "error": "stale approved profile page",
                "reason_code": "expected_revision_conflict",
                "current_revision": exc.current_revision,
            }, HTTPStatus.CONFLICT)
        except (ApprovedProfileIdempotencyConflict, ProfileEnablementIdempotencyConflict):
            self.send_json({
                "error": "request_id or mutation_id conflict",
                "reason_code": "idempotency_conflict",
            }, HTTPStatus.CONFLICT)
        except (ApprovedProfileSidecarUnavailable, ProfileEnablementSidecarUnavailable):
            self.send_json({
                "error": "approved profile sidecar is not initialized",
                "reason_code": "sidecar_not_initialized",
            }, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ApprovedProfileControlError, ProfileEnablementError):
            self.send_error_json("Invalid approved profile decision", HTTPStatus.BAD_REQUEST)
        except (
            RetrievalValidationError,
            MemoryPackageValidationError,
            EvidenceSelectionValidationError,
            TemporalCandidateValidationError,
            ValueError,
        ):
            self.send_error_json("Invalid approved profile decision source", HTTPStatus.BAD_REQUEST)
        except sqlite3.Error as exc:
            print(f"[local-web] database error: {exc}")
            self.send_error_json("Database connection failed", HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            print(f"[local-web] unexpected API error: {exc.__class__.__name__}")
            self.send_error_json("Unexpected server error", HTTPStatus.INTERNAL_SERVER_ERROR)

    def api_runtime_execute(self, payload: dict[str, Any]) -> None:
        if self.app.runtime_review is True:
            self.send_json({
                "error": "Review-only mode: execution authority is absent. Sending is unavailable.",
                "reason_code": "runtime_review_execution_forbidden",
                "runtime_review": True,
                "execution_authority_present": False,
                "can_execute": False,
            }, HTTPStatus.FORBIDDEN)
            return
        required = {
            "q", "budget", "request_id", "mutation_id", "expected_bindings", "confirmed",
        }
        if set(payload) != required:
            self.send_error_json("Runtime execution fields mismatch", HTTPStatus.BAD_REQUEST)
            return
        if not self.runtime_mutation_surface_available():
            self.send_json({
                "error": "runtime execution requires explicit runtime-admin mode",
                "reason_code": "runtime_admin_required",
            }, HTTPStatus.FORBIDDEN)
            return
        result = self.app.runtime_execution_service.execute(
            payload.get("q"),
            request_id=payload.get("request_id"),
            mutation_id=payload.get("mutation_id"),
            budget_profile=payload.get("budget"),
            expected_bindings=payload.get("expected_bindings"),
            confirmed=payload.get("confirmed"),
            authority=self.app.runtime_execution_session,
        )
        self.send_json(result)

    def api_approved_profile_decision(self, payload: dict[str, Any]) -> None:
        allowed = {
            "q", "budget", "package_identity", "candidate_id", "action",
            "expected_revision", "request_id", "mutation_id", "edited_statement", "reason",
        }
        required = {
            "q", "package_identity", "candidate_id", "action", "expected_revision",
            "request_id", "mutation_id",
        }
        if set(payload) - allowed or not required <= set(payload):
            self.send_error_json("Approved profile decision fields mismatch", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_mutation_surface_available():
            self.send_json({
                "error": "approved profile mutations require explicit profile-admin mode",
                "reason_code": "profile_admin_required",
            }, HTTPStatus.FORBIDDEN)
            return
        query_text = payload.get("q")
        if not isinstance(query_text, str) or not query_text.strip():
            self.send_error_json("Missing approved profile candidate query", HTTPStatus.BAD_REQUEST)
            return
        result = record_production_approved_profile_decision(
            RetrievalQuery(query_text.strip()),
            profile_admin=self.app.profile_admin_session,
            budget_profile=payload.get("budget", "Medium"),
            package_identity=payload.get("package_identity"),
            candidate_id=payload.get("candidate_id"),
            action=payload.get("action"),
            expected_revision=payload.get("expected_revision"),
            request_id=payload.get("request_id"),
            mutation_id=payload.get("mutation_id"),
            edited_statement=payload.get("edited_statement", ""),
            reason=payload.get("reason", ""),
        )
        self.send_json(result)

    def api_profile_enablement_proposal(self, payload: dict[str, Any]) -> None:
        allowed = {
            "original_text", "proposal_type", "source_anchors", "expected_revision",
            "request_id", "mutation_id",
        }
        required = allowed - {"source_anchors"}
        if set(payload) - allowed or not required <= set(payload):
            self.send_error_json("Direct profile proposal fields mismatch", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_mutation_surface_available():
            self.send_json({
                "error": "direct proposals require explicit profile-admin mode",
                "reason_code": "profile_admin_required",
            }, HTTPStatus.FORBIDDEN)
            return
        proposal = {
            "original_text": payload.get("original_text"),
            "proposal_type": payload.get("proposal_type"),
            "source_anchors": payload.get("source_anchors", []),
            "expected_revision": payload.get("expected_revision"),
            "request_id": payload.get("request_id"),
            "mutation_id": payload.get("mutation_id"),
        }
        authority = open_production_gate_b1(
            profile_admin=self.app.profile_admin_session, **proposal,
        )
        self.send_json(ProfileEnablementService().propose(
            authority=authority, **proposal,
        ))

    def api_profile_enablement_decision(self, payload: dict[str, Any]) -> None:
        common = {
            "subject_origin", "subject_id", "action", "expected_revision",
            "request_id", "mutation_id", "edited_statement", "reason",
        }
        origin = payload.get("subject_origin")
        if origin == "v7.2_candidate":
            allowed = common | {"q", "budget", "package_identity"}
            required = common - {"edited_statement", "reason"} | {"q", "package_identity"}
        elif origin == "direct_user_proposal":
            allowed = common
            required = common - {"edited_statement", "reason"}
        else:
            self.send_error_json("Invalid profile decision origin", HTTPStatus.BAD_REQUEST)
            return
        if set(payload) - allowed or not required <= set(payload):
            self.send_error_json("Profile enablement decision fields mismatch", HTTPStatus.BAD_REQUEST)
            return
        if not self.profile_mutation_surface_available():
            self.send_json({
                "error": "profile decisions require explicit profile-admin mode",
                "reason_code": "profile_admin_required",
            }, HTTPStatus.FORBIDDEN)
            return
        service = ProfileEnablementService()
        common_args = {
            "subject_origin": origin,
            "subject_id": payload.get("subject_id"),
            "action": payload.get("action"),
            "expected_revision": payload.get("expected_revision"),
            "request_id": payload.get("request_id"),
            "mutation_id": payload.get("mutation_id"),
            "edited_statement": payload.get("edited_statement", ""),
            "reason": payload.get("reason", ""),
        }
        if origin == "v7.2_candidate":
            query_text = payload.get("q")
            if not isinstance(query_text, str) or not query_text.strip():
                self.send_error_json("Missing approved profile candidate query", HTTPStatus.BAD_REQUEST)
                return
            common_args.update(
                query_text=query_text.strip(),
                budget_profile=payload.get("budget", "Medium"),
                package_identity=payload.get("package_identity"),
            )
        else:
            common_args.update(query_text="", budget_profile="Medium", package_identity=None)
        authority = open_production_gate_b2(
            profile_admin=self.app.profile_admin_session, **common_args,
        )
        result = service.decide(authority=authority, **common_args)
        self.send_json(result)

    def api_recent(self, params: dict[str, list[str]]) -> None:
        source_platform = self.parse_source_platform(params)
        if source_platform is None:
            return
        with self.app.connect() as conn:
            marker = self.marker_sql(conn)
            source_expr = self.source_expr(conn, "conversations", "c")
            source_filter, source_params = self.source_filter_clause(source_expr, source_platform)
            where_sql = f"WHERE {source_filter}" if source_filter else ""
            rows = conn.execute(
                f"""
                SELECT
                    c.conversation_id,
                    c.title,
                    c.update_time_iso,
                    c.message_count,
                    {source_expr} AS source_platform,
                    {marker["status"]} AS status,
                    {marker["tags"]} AS tags
                FROM conversations c
                {marker["conversation_joins"]}
                {where_sql}
                GROUP BY c.conversation_id
                ORDER BY c.update_time DESC
                LIMIT 20
                """
                ,
                source_params,
            ).fetchall()
        self.send_json({"items": [self.add_source_fields(dict(row)) for row in rows]})

    def api_search(self, params: dict[str, list[str]]) -> None:
        query = (params.get("q") or [""])[0].strip()
        if not query:
            self.send_error_json("请输入搜索关键词", HTTPStatus.BAD_REQUEST)
            return
        source_platform = self.parse_source_platform(params)
        if source_platform is None:
            return
        scope = (params.get("scope") or [""])[0].strip().lower()
        aggregate = (params.get("aggregate") or [""])[0].strip().lower()
        if scope == "all" and aggregate == "conversation":
            sort = (params.get("sort") or ["relevance"])[0].strip().lower() or "relevance"
            if sort not in {"relevance", "latest"}:
                self.send_error_json("Invalid search sort", HTTPStatus.BAD_REQUEST)
                return
            offset = self.parse_mobile_search_int(params, "offset", 0, 0, None)
            if offset is None:
                return
            limit = self.parse_mobile_search_int(params, "limit", 20, 1, 20)
            if limit is None:
                return
            result = search_all_memory_conversations(
                query,
                sort=sort,
                offset=offset,
                limit=limit,
                source_platform=source_platform or None,
                db_path=self.app.db_path,
            )
            with self.app.connect() as conn:
                markers = {
                    group.get("conversation_id") or "": self.conversation_marker(
                        conn, group.get("conversation_id") or ""
                    )
                    for group in result["items"]
                    if group.get("conversation_id")
                }
            for group in result["items"]:
                marker = markers.get(group.get("conversation_id") or "", {})
                group["best_hit"] = self.search_item_payload(group["best_hit"], marker)
                group["other_hits"] = [
                    self.search_item_payload(hit, marker) for hit in group["other_hits"]
                ]
            self.send_json(result)
            return
        memory_space = self.parse_memory_space(params)
        if memory_space is None:
            return
        limit = self.parse_limit(params)
        rows = retrieve(query, top_k=limit, memory_space=memory_space, db_path=self.app.db_path)
        if source_platform:
            rows = [row for row in rows if row.get("source_platform") == source_platform]
        with self.app.connect() as conn:
            markers = {
                row.get("conversation_id") or "": self.conversation_marker(conn, row.get("conversation_id") or "")
                for row in rows
                if row.get("conversation_id")
            }
        items = []
        for row in rows:
            marker = markers.get(row.get("conversation_id") or "", {})
            items.append(self.search_item_payload(row, marker))
        self.send_json({"items": items, "memory_space": memory_space})

    def search_item_payload(self, row: dict, marker: dict | None = None) -> dict:
        item = dict(row)
        marker = marker or {}
        item["source"] = item.pop("hit_source", item.get("source") or "")
        item["title"] = item.get("title") or ""
        item["role"] = item.get("role") or ""
        item["role_label"] = role_label(item.get("role")) if item.get("role") else ""
        item["update_time_iso"] = item.get("update_time_iso") or ""
        item["conversation_id"] = item.get("conversation_id") or ""
        item["message_id"] = item.get("message_id") or ""
        item["reference_uri"] = reference_uri(item["conversation_id"], item["message_id"])
        item["status"] = marker.get("status") or item.get("status") or ""
        item["tags"] = marker.get("tags") or item.get("tags") or ""
        self.add_source_fields(item)
        return item

    def parse_mobile_search_int(
        self,
        params: dict[str, list[str]],
        name: str,
        default: int,
        minimum: int,
        maximum: int | None,
    ) -> int | None:
        raw_value = (params.get(name) or [str(default)])[0]
        try:
            value = int(raw_value)
        except ValueError:
            self.send_error_json(f"Invalid search {name}", HTTPStatus.BAD_REQUEST)
            return None
        if value < minimum or (maximum is not None and value > maximum):
            self.send_error_json(f"Invalid search {name}", HTTPStatus.BAD_REQUEST)
            return None
        return value

    def parse_limit(self, params: dict[str, list[str]]) -> int:
        raw_limit = (params.get("limit") or ["20"])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 20
        return max(1, min(limit, 100))

    def parse_timeline_limit(self, params: dict[str, list[str]]) -> int:
        raw_limit = (params.get("limit") or ["12"])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 12
        return max(1, min(limit, 60))

    def term_filter_clause(self, terms: list[str], conv_alias: str = "c") -> tuple[str, list[str]]:
        parts: list[str] = []
        params: list[str] = []
        for term in terms:
            pattern = f"%{term}%"
            parts.append(
                f"""(
                    {conv_alias}.title LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM messages mt
                        WHERE mt.conversation_id = {conv_alias}.conversation_id
                          AND mt.text LIKE ?
                    )
                )"""
            )
            params.extend([pattern, pattern])
        return " OR ".join(parts), params

    def parse_window(self, params: dict[str, list[str]]) -> int | None:
        raw_window = (params.get("window") or ["3"])[0]
        try:
            window = int(raw_window)
        except ValueError:
            return None
        if window < 0 or window > 10:
            return None
        return window

    def message_payload(self, row: sqlite3.Row, focus_message_id: str = "") -> dict:
        message_id = row["message_id"] or ""
        conversation_id = row["conversation_id"] or ""
        item = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": row["role"] or "",
            "role_label": role_label(row["role"]),
            "create_time_iso": row["create_time_iso"] or "",
            "text": row["text"] or "",
            "is_focus": bool(focus_message_id and message_id == focus_message_id),
            "reference_uri": reference_uri(conversation_id, message_id),
        }
        if "position" in row.keys():
            item["position"] = int(row["position"])
        if "source_platform" in row.keys():
            self.add_source_fields(item, row["source_platform"])
        else:
            self.add_source_fields(item)
        return item

    def api_message_context(self, params: dict[str, list[str]]) -> None:
        conversation_id = (params.get("conversation_id") or [""])[0].strip()
        if not conversation_id:
            self.send_error_json("Missing conversation id", HTTPStatus.BAD_REQUEST)
            return
        message_id = (params.get("message_id") or [""])[0].strip()
        if not message_id:
            self.send_error_json("Missing message id", HTTPStatus.BAD_REQUEST)
            return
        window = self.parse_window(params)
        if window is None:
            self.send_error_json("Invalid window", HTTPStatus.BAD_REQUEST)
            return
        with self.app.connect() as conn:
            message_source_expr = self.source_expr(conn, "messages", "messages")
            focus_rows = conn.execute(
                f"""
                SELECT rowid AS message_rowid, message_id, conversation_id,
                       conversation_title, ordinal, create_time,
                       {message_source_expr} AS source_platform
                FROM messages
                WHERE conversation_id = ? AND message_id = ?
                LIMIT 2
                """,
                (conversation_id, message_id),
            ).fetchall()
            if not focus_rows:
                self.send_error_json("Message anchor not found", HTTPStatus.NOT_FOUND)
                return
            if len(focus_rows) != 1:
                self.send_error_json("Ambiguous message anchor", HTTPStatus.CONFLICT)
                return
            focus = focus_rows[0]
            messages = conn.execute(
                f"""
                WITH ordered AS (
                    SELECT
                        rowid AS message_rowid,
                        conversation_id,
                        message_id,
                        role,
                        create_time_iso,
                        text,
                        {message_source_expr} AS source_platform,
                        ROW_NUMBER() OVER (
                            ORDER BY ordinal IS NULL, ordinal,
                                     create_time IS NULL, create_time, rowid
                        ) AS position
                    FROM messages
                    WHERE conversation_id = ?
                ), focus_position AS (
                    SELECT position
                    FROM ordered
                    WHERE message_rowid = ?
                )
                SELECT
                    ordered.conversation_id,
                    ordered.message_id,
                    ordered.role,
                    ordered.create_time_iso,
                    ordered.text,
                    ordered.source_platform,
                    ordered.position
                FROM ordered, focus_position
                WHERE ordered.position BETWEEN focus_position.position - ?
                                           AND focus_position.position + ?
                ORDER BY ordered.position ASC
                """,
                (
                    focus["conversation_id"],
                    int(focus["message_rowid"]),
                    window,
                    window,
                ),
            ).fetchall()
        self.send_json(
            {
                "conversation_id": focus["conversation_id"],
                "conversation_title": focus["conversation_title"] or "",
                "source_platform": focus["source_platform"] or "chatgpt",
                "source_platform_label": source_platform_label(focus["source_platform"]),
                "focus_message_id": message_id,
                "window": window,
                "messages": [self.message_payload(row, message_id) for row in messages],
            }
        )

    def api_conversation(self, params: dict[str, list[str]]) -> None:
        conversation_id = (params.get("id") or [""])[0].strip()
        if not conversation_id:
            self.send_error_json("Missing conversation id")
            return
        focus_message_id = (params.get("focus_message_id") or [""])[0].strip()
        with self.app.connect() as conn:
            marker = self.marker_sql(conn)
            conversation_source_expr = self.source_expr(conn, "conversations", "c")
            message_source_expr = self.source_expr(conn, "messages", "messages")
            conv = conn.execute(
                f"""
                SELECT
                    c.conversation_id,
                    c.title,
                    c.update_time_iso,
                    c.message_count,
                    {conversation_source_expr} AS source_platform,
                    {marker["status"]} AS status,
                    {marker["note"]} AS note,
                    {marker["tags"]} AS tags
                FROM conversations c
                {marker["conversation_joins"]}
                WHERE c.conversation_id = ?
                GROUP BY c.conversation_id
                """,
                (conversation_id,),
            ).fetchone()
            if not conv:
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            messages = conn.execute(
                f"""
                SELECT conversation_id, message_id, role, create_time_iso, text, {message_source_expr} AS source_platform
                FROM messages
                WHERE conversation_id = ?
                ORDER BY ordinal ASC, create_time ASC
                """,
                (conversation_id,),
            ).fetchall()
        conversation = dict(conv)
        conversation["focus_message_id"] = focus_message_id
        self.add_source_fields(conversation)
        self.send_json(
            {
                "conversation": conversation,
                "focus_message_id": focus_message_id,
                "messages": [self.message_payload(row, focus_message_id) for row in messages],
            }
        )

    def api_conversation_messages(self, params: dict[str, list[str]]) -> None:
        allowed = {"conversation_id", "anchor_message_id", "before", "after", "limit"}
        if set(params) - allowed:
            self.send_error_json(
                "Unsupported conversation reader parameter", HTTPStatus.BAD_REQUEST,
            )
            return
        if any(len(values) != 1 for values in params.values()):
            self.send_error_json(
                "Repeated conversation reader parameter", HTTPStatus.BAD_REQUEST,
            )
            return
        conversation_id = (params.get("conversation_id") or [""])[0].strip()
        anchor_values = [name for name in ("anchor_message_id", "before", "after") if name in params]
        if len(anchor_values) != 1:
            self.send_error_json(
                "Exactly one conversation anchor is required", HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            limit = int((params.get("limit") or ["20"])[0])
        except ValueError:
            self.send_error_json("Invalid conversation limit", HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = read_conversation_messages(
                self.app.db_path,
                conversation_id=conversation_id,
                anchor_message_id=(params.get("anchor_message_id") or [None])[0],
                before=(params.get("before") or [None])[0],
                after=(params.get("after") or [None])[0],
                limit=limit,
            )
        except ConversationReaderBadRequest as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
            return
        except ConversationReaderNotFound as exc:
            self.send_error_json(str(exc), HTTPStatus.NOT_FOUND)
            return
        except ConversationReaderConflict as exc:
            self.send_error_json(str(exc), HTTPStatus.CONFLICT)
            return
        for message in payload["messages"]:
            message["role_label"] = role_label(message.get("role"))
            message["source_platform_label"] = source_platform_label(
                message.get("source_platform")
            )
            message["reference_uri"] = reference_uri(
                message.get("conversation_id"), message.get("message_id")
            )
        self.add_source_fields(payload["conversation"])
        self.send_json(payload)

    def project_by_name(self, name: str) -> dict | None:
        for project in self.app.load_projects():
            if project["name"] == name:
                return project
        return None

    def project_where_clause(self, project: dict, aliases: tuple[str, str] = ("c", "m")) -> tuple[str, list[str]]:
        patterns = [f"%{term}%" for term in project["match_terms"]]
        conv_alias, msg_alias = aliases
        parts = []
        params = []
        for pattern in patterns:
            parts.append(f"{conv_alias}.title LIKE ?")
            params.append(pattern)
            parts.append(f"{msg_alias}.text LIKE ?")
            params.append(pattern)
        return " OR ".join(parts), params

    def project_stats(self, conn: sqlite3.Connection, project: dict, source_platform: str = "") -> dict:
        where_clause, params = self.project_where_clause(project)
        source_expr = self.source_expr(conn, "conversations", "c")
        source_filter, source_params = self.source_filter_clause(source_expr, source_platform)
        if source_filter:
            where_clause = f"({where_clause}) AND {source_filter}"
            params.extend(source_params)
        row = conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT c.conversation_id) AS match_count,
                MAX(c.update_time_iso) AS latest_update_time_iso
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.conversation_id
            WHERE {where_clause}
            """,
            params,
        ).fetchone()
        return {
            "match_count": int(row["match_count"] or 0),
            "latest_update_time_iso": row["latest_update_time_iso"] or "",
            "source_counts": self.source_counts_for_where(conn, where_clause, params),
        }

    def conversation_matches_project(self, conn: sqlite3.Connection, conversation_id: str, title: str, project: dict) -> bool:
        for term in project["match_terms"]:
            if term and term in title:
                return True
        clause, params = self.term_filter_clause(project["match_terms"], "c")
        if not clause:
            return False
        row = conn.execute(
            f"""
            SELECT 1
            FROM conversations c
            WHERE c.conversation_id = ?
              AND ({clause})
            LIMIT 1
            """,
            [conversation_id, *params],
        ).fetchone()
        return row is not None

    def api_timeline(self, params: dict[str, list[str]]) -> None:
        group_by = (params.get("group_by") or ["month"])[0].strip().lower()
        if group_by not in {"month", "day"}:
            group_by = "month"
        limit = self.parse_timeline_limit(params)
        project_name = unquote((params.get("project") or [""])[0]).strip()
        query = (params.get("q") or [""])[0].strip()
        source_platform = self.parse_source_platform(params)
        if source_platform is None:
            return

        projects = self.app.load_projects()
        selected_project = None
        where_parts: list[str] = []
        query_params: list[str] = []
        if project_name:
            selected_project = self.project_by_name(project_name)
            if not selected_project:
                self.send_error_json("Project not found", HTTPStatus.NOT_FOUND)
                return
            clause, values = self.term_filter_clause(selected_project["match_terms"], "c")
            where_parts.append(f"({clause})")
            query_params.extend(values)
        if query:
            clause, values = self.term_filter_clause([query], "c")
            where_parts.append(f"({clause})")
            query_params.extend(values)

        with self.app.connect() as conn:
            source_expr = self.source_expr(conn, "conversations", "c")
            source_filter, source_params = self.source_filter_clause(source_expr, source_platform)
            if source_filter:
                where_parts.append(source_filter)
                query_params.extend(source_params)
            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            rows = conn.execute(
                f"""
                SELECT
                    c.conversation_id,
                    c.title,
                    COALESCE(NULLIF(c.update_time_iso, ''), c.create_time_iso, '') AS timeline_time_iso,
                    c.update_time_iso,
                    c.create_time_iso,
                    c.message_count,
                    {source_expr} AS source_platform,
                    COALESCE(c.update_time, c.create_time, 0) AS sort_time
                FROM conversations c
                {where_sql}
                ORDER BY sort_time DESC
                LIMIT 1200
                """,
                query_params,
            ).fetchall()

            periods: dict[str, dict] = {}
            for row in rows:
                period = period_from_iso(row["timeline_time_iso"], group_by)
                bucket = periods.setdefault(
                    period,
                    {
                        "period": period,
                        "conversation_count": 0,
                        "message_count": 0,
                        "top_project_counts": {},
                        "source_counts": {},
                        "conversations": [],
                    },
                )
                row_source = row["source_platform"] or "chatgpt"
                bucket["conversation_count"] += 1
                bucket["message_count"] += int(row["message_count"] or 0)
                bucket["source_counts"][row_source] = bucket["source_counts"].get(row_source, 0) + 1
                project_matches = [
                    project["name"]
                    for project in projects
                    if self.conversation_matches_project(
                        conn,
                        row["conversation_id"] or "",
                        row["title"] or "",
                        project,
                    )
                ]
                for name in project_matches:
                    bucket["top_project_counts"][name] = bucket["top_project_counts"].get(name, 0) + 1
                if len(bucket["conversations"]) < 20:
                    bucket["conversations"].append(
                        {
                            "conversation_id": row["conversation_id"] or "",
                            "title": row["title"] or "",
                            "update_time_iso": row["timeline_time_iso"] or row["update_time_iso"] or row["create_time_iso"] or "",
                            "message_count": int(row["message_count"] or 0),
                            "source_platform": row_source,
                            "source_platform_label": source_platform_label(row_source),
                            "project_matches": project_matches,
                        }
                    )

        items = []
        for bucket in list(periods.values())[:limit]:
            top_projects = [
                {"name": name, "match_count": count}
                for name, count in sorted(
                    bucket["top_project_counts"].items(),
                    key=lambda item: (-item[1], item[0]),
                )[:5]
            ]
            items.append(
                {
                    "period": bucket["period"],
                    "conversation_count": bucket["conversation_count"],
                    "message_count": bucket["message_count"],
                    "source_counts": bucket["source_counts"],
                    "top_projects": top_projects,
                    "conversations": bucket["conversations"],
                }
            )

        self.send_json(
            {
                "db_mode": self.app.db_mode,
                "readonly": self.app.readonly,
                "group_by": group_by,
                "project": selected_project["name"] if selected_project else "",
                "q": query,
                "source_platform": source_platform,
                "items": items,
            }
        )

    def api_projects(self, params: dict[str, list[str]]) -> None:
        source_platform = self.parse_source_platform(params)
        if source_platform is None:
            return
        projects = self.app.load_projects()
        with self.app.connect() as conn:
            items = []
            for project in projects:
                stats = self.project_stats(conn, project, source_platform)
                items.append(
                    {
                        "name": project["name"],
                        "description": project["description"],
                        "status": project["status"],
                        "priority": project["priority"],
                        "keywords": project["keywords"],
                        "aliases": project["aliases"],
                        "match_count": stats["match_count"],
                        "latest_update_time_iso": stats["latest_update_time_iso"],
                        "source_counts": stats["source_counts"],
                    }
                )
        self.send_json(
            {
                "items": items,
                "db_mode": self.app.db_mode,
                "readonly": self.app.readonly,
                "source_platform": source_platform,
            }
        )

    def api_project(self, params: dict[str, list[str]]) -> None:
        name = unquote((params.get("name") or [""])[0]).strip()
        if not name:
            self.send_error_json("Missing project name")
            return
        project = self.project_by_name(name)
        if not project:
            self.send_error_json("Project not found", HTTPStatus.NOT_FOUND)
            return
        source_platform = self.parse_source_platform(params)
        if source_platform is None:
            return
        where_clause, query_params = self.project_where_clause(project)
        row_limit = 50 if self.app.db_mode == "real" and self.app.readonly else 20
        with self.app.connect() as conn:
            marker = self.marker_sql(conn)
            stats = self.project_stats(conn, project, source_platform)
            conversation_source_expr = self.source_expr(conn, "conversations", "c")
            message_source_expr = self.source_expr(conn, "messages", "m")
            conversation_source_filter, conversation_source_params = self.source_filter_clause(
                conversation_source_expr,
                source_platform,
            )
            message_source_filter, message_source_params = self.source_filter_clause(message_source_expr, source_platform)
            conversation_where = where_clause
            conversation_params = list(query_params)
            if conversation_source_filter:
                conversation_where = f"({conversation_where}) AND {conversation_source_filter}"
                conversation_params.extend(conversation_source_params)
            conversations = conn.execute(
                f"""
                SELECT DISTINCT
                    c.conversation_id,
                    c.title,
                    c.update_time_iso,
                    c.message_count,
                    {conversation_source_expr} AS source_platform,
                    {marker["status"]} AS status,
                    {marker["tags"]} AS tags
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.conversation_id
                {marker["conversation_joins"]}
                WHERE {conversation_where}
                GROUP BY c.conversation_id
                ORDER BY c.update_time DESC
                LIMIT ?
                """,
                [*conversation_params, row_limit],
            ).fetchall()

            message_parts = []
            message_params = []
            for keyword in project["match_terms"]:
                message_parts.append("m.text LIKE ?")
                message_params.append(f"%{keyword}%")
            message_where = " OR ".join(message_parts)
            if message_source_filter:
                message_where = f"({message_where}) AND {message_source_filter}"
                message_params.extend(message_source_params)
            messages = conn.execute(
                f"""
                SELECT
                    m.message_id,
                    m.conversation_id,
                    m.conversation_title AS title,
                    m.role,
                    m.create_time_iso,
                    m.create_time_iso AS update_time_iso,
                    m.text AS snippet,
                    {message_source_expr} AS source_platform,
                    {marker["status"]} AS status,
                    {marker["tags"]} AS tags
                FROM messages m
                {marker["message_joins"]}
                WHERE {message_where}
                GROUP BY m.message_id
                ORDER BY m.create_time DESC
                LIMIT ?
                """,
                [*message_params, row_limit],
            ).fetchall()
        matched_messages = []
        for row in messages:
            item = dict(row)
            item["role_label"] = role_label(item.get("role"))
            item["snippet"] = compact(item.get("snippet"), 240)
            item["reference_uri"] = reference_uri(item.get("conversation_id"), item.get("message_id"))
            self.add_source_fields(item)
            matched_messages.append(item)
        matched_conversations = [self.add_source_fields(dict(row)) for row in conversations]
        self.send_json(
            {
                "project": {
                    "name": project["name"],
                    "description": project["description"],
                    "status": project["status"],
                    "priority": project["priority"],
                    "keywords": project["keywords"],
                    "aliases": project["aliases"],
                    "match_count": stats["match_count"],
                    "latest_update_time_iso": stats["latest_update_time_iso"],
                    "source_counts": stats["source_counts"],
                },
                "matched_conversations": matched_conversations,
                "matched_messages": matched_messages,
                "db_mode": self.app.db_mode,
                "readonly": self.app.readonly,
                "source_platform": source_platform,
            }
        )

    def api_marks(self) -> None:
        with self.app.connect() as conn:
            if not self.table_exists(conn, "conversation_marks"):
                self.send_json({"items": []})
                return
            rows = conn.execute(
                """
                SELECT cm.status, c.title, COALESCE(cm.note, '') AS note, cm.updated_at, cm.conversation_id
                FROM conversation_marks cm
                JOIN conversations c ON c.conversation_id = cm.conversation_id
                ORDER BY cm.updated_at DESC
                LIMIT 100
                """
            ).fetchall()
        self.send_json({"items": rows_to_dicts(rows)})

    def api_tags(self) -> None:
        with self.app.connect() as conn:
            if not self.table_exists(conn, "conversation_tags"):
                self.send_json({"items": []})
                return
            rows = conn.execute(
                """
                SELECT tag, COUNT(DISTINCT conversation_id) AS conversation_count
                FROM conversation_tags
                GROUP BY tag
                ORDER BY tag COLLATE NOCASE
                """
            ).fetchall()
        self.send_json({"items": rows_to_dicts(rows)})

    def api_tag(self, params: dict[str, list[str]]) -> None:
        tag = unquote((params.get("name") or [""])[0]).strip()
        if not tag:
            self.send_error_json("Missing tag name")
            return
        with self.app.connect() as conn:
            if not self.table_exists(conn, "conversation_tags"):
                self.send_json({"items": []})
                return
            rows = conn.execute(
                """
                SELECT DISTINCT c.conversation_id, c.title, c.update_time_iso, c.message_count
                FROM conversation_tags ct
                JOIN conversations c ON c.conversation_id = ct.conversation_id
                WHERE ct.tag = ?
                ORDER BY c.update_time DESC
                LIMIT 100
                """,
                (tag,),
            ).fetchall()
        self.send_json({"items": rows_to_dicts(rows)})

    def api_add_tag(self, payload: dict) -> None:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        raw_tags = payload.get("tags", payload.get("tag", ""))
        if isinstance(raw_tags, list):
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        else:
            tags = [tag.strip() for tag in str(raw_tags).replace("，", ",").split(",") if tag.strip()]
        if not conversation_id:
            self.send_error_json("Conversation id is required")
            return
        if not tags:
            self.send_error_json("Tag is required")
            return
        with self.app.connect() as conn:
            # Serialize the existence check and inserts across local writers.
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not exists:
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            created_at = now_iso()
            conn.executemany(
                "INSERT INTO conversation_tags (conversation_id, tag, created_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM conversation_tags WHERE conversation_id = ? AND tag = ?)",
                [(conversation_id, tag, created_at, conversation_id, tag) for tag in dict.fromkeys(tags)],
            )
            conn.commit()
        self.send_json({"ok": True, "tags": list(dict.fromkeys(tags)), "db_mode": self.app.db_mode})

    def api_mark(self, payload: dict) -> None:
        conversation_id = str(payload.get("conversation_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        note = str(payload.get("note") or "").strip()
        if not conversation_id:
            self.send_error_json("Conversation id is required")
            return
        if status not in ALLOWED_STATUSES:
            self.send_error_json("Invalid status")
            return
        with self.app.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not exists:
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            conn.execute(
                """
                INSERT INTO conversation_marks (conversation_id, status, note, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, status, note or None, now_iso()),
            )
            conn.commit()
        self.send_json({"ok": True, "status": status, "db_mode": self.app.db_mode})


def _service_mode(args: argparse.Namespace) -> str:
    if args.runtime_admin:
        return "runtime-admin"
    if args.runtime_admin_mock:
        return "runtime-admin-mock"
    if args.runtime_review:
        return "runtime-review"
    if args.profile_admin:
        return "profile-admin"
    if args.real and args.readonly:
        return "real-readonly"
    if args.real:
        return "real"
    return "web"


def main() -> int:
    args = parse_args()
    if args.host == "0.0.0.0":
        print("Refusing to bind 0.0.0.0. AyunOS local web UI must stay local-only.")
        return 2
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if not WEB_ROOT.exists():
        raise FileNotFoundError(f"Web root not found: {WEB_ROOT}")

    if args.profile_admin and not (
        args.host in {"127.0.0.1", "localhost", "::1"}
        and args.real and args.readonly and db_path == REAL_DB_PATH.resolve()
    ):
        print("Refusing profile-admin mode without --real --readonly and the fixed real database.")
        return 2

    runtime_modes = sum(bool(value) for value in (
        args.runtime_review, args.runtime_admin, args.runtime_admin_mock,
    ))
    if runtime_modes > 1:
        print("Refusing simultaneous Runtime review/admin modes.")
        return 2

    if args.runtime_review and args.profile_admin:
        print("Refusing simultaneous runtime-review and profile-admin modes.")
        return 2

    if args.runtime_review and not (
        args.host in {"127.0.0.1", "localhost", "::1"}
        and args.real and args.readonly and db_path == REAL_DB_PATH.resolve()
        and args.allow_external_model_calls is False
        and args.confirm_deepseek_send is False
        and args.runtime_test_sidecar is None
    ):
        print(
            "Refusing runtime-review mode without the fixed local real-readonly source and zero execution flags."
        )
        return 2

    if args.runtime_admin and not (
        args.host in {"127.0.0.1", "localhost", "::1"}
        and args.real and args.readonly and db_path == REAL_DB_PATH.resolve()
        and args.allow_external_model_calls is True
        and args.confirm_deepseek_send is True
    ):
        print(
            "Refusing runtime-admin mode without fixed local real-readonly source and both external-send confirmations."
        )
        return 2

    if args.runtime_admin_mock and not (
        args.host in {"127.0.0.1", "localhost", "::1"}
        and args.real and args.readonly and db_path == REAL_DB_PATH.resolve()
        and args.runtime_test_sidecar
        and args.allow_external_model_calls is False
        and args.confirm_deepseek_send is False
    ):
        print(
            "Refusing mock runtime-admin mode without fixed local real-readonly source, "
            "an initialized temporary sidecar, and zero external-send flags."
        )
        return 2

    mode = _service_mode(args)
    try:
        server = ThreadingHTTPServer((args.host, args.port), AyunOSHandler)
    except OSError as exc:
        reason = f"errno={exc.errno}" if exc.errno is not None else exc.__class__.__name__
        print(
            f"Failed to bind {mode} listener at {args.host}:{args.port} ({reason}). "
            "Execution authority was not created."
        )
        return 2

    try:
        bound_host, bound_port = server.server_address[:2]
        bound_port = int(bound_port)
        if bound_port != args.port:
            print(
                f"Refusing {mode} listener because requested port {args.port} "
                f"does not match bound port {bound_port}. Execution authority was not created."
            )
            return 2

        server_instance_id = "srv_" + uuid.uuid4().hex
        profile_admin_session = open_production_profile_admin() if args.profile_admin else None
        runtime_execution_service: Any = (
            ProductionRuntimeReviewService()
            if args.real and args.readonly and db_path == REAL_DB_PATH.resolve()
            else None
        )
        runtime_execution_session = None
        runtime_mock = False
        if args.runtime_admin:
            from memory.production_runtime_execution import (
                ProductionRuntimeExecutionService,
                open_production_runtime_execution,
            )

            runtime_execution_service = ProductionRuntimeExecutionService()
            runtime_execution_session = open_production_runtime_execution(
                execute=True,
                allow_external_model_call=args.allow_external_model_calls,
                confirm_deepseek_send=args.confirm_deepseek_send,
            )
        elif args.runtime_admin_mock:
            from memory.production_runtime_execution import create_mock_runtime_execution_service

            runtime_execution_service = create_mock_runtime_execution_service(
                Path(args.runtime_test_sidecar).expanduser().resolve()
            )
            runtime_execution_session = runtime_execution_service.execution_session
            runtime_mock = True
        app = App(
            db_path, real=args.real, readonly=args.readonly,
            profile_admin_session=profile_admin_session,
            runtime_execution_session=runtime_execution_session,
            runtime_execution_service=runtime_execution_service,
            runtime_mock=runtime_mock,
            runtime_review=args.runtime_review,
            service_mode=mode,
            bound_host=str(bound_host),
            bound_port=bound_port,
            server_instance_id=server_instance_id,
            operational_state_path=(
                Path(getattr(args, "operational_state_db", None))
                .expanduser()
                .resolve()
                if getattr(args, "operational_state_db", None)
                else None
            ),
        )
        if not app.readonly:
            app.ensure_marks_schema()
        AyunOSHandler.app = app

        print(
            f"AyunOS {mode} UI running at {app.bound_endpoint} "
            f"(pid={app.process_id}, instance={app.server_instance_id})"
        )
        server.serve_forever()
        return 0
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
