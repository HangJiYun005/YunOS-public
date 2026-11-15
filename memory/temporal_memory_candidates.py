"""YunOS v7.2 deterministic temporal interpretation and memory candidates.

The layer consumes a validated v7.1 evidence selection, emits reviewable
candidate records, and never persists or approves them.  The built-in
interpreter is deliberately small and rule based.  A future provider may
implement the controlled interface, but v7.2 rejects every non-local provider.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from memory.db_path_policy import DEFAULT_REAL_DB_PATH
from memory.evidence_selection import (
    EvidenceSelectionService,
    EvidenceSelectionValidationError,
    PROTOCOL_VERSION as SOURCE_PROTOCOL_VERSION,
    _selection_status as _v71_selection_status,
    _validate_candidate_ledger as _validate_v71_candidate_ledger,
    _validate_source_projection as _validate_v71_source_projection,
    validate_evidence_selection_source_backed,
    validate_evidence_selection_standalone,
)
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.memory_package import MemoryPackageValidationError, _reject_absolute_paths


PROTOCOL_VERSION = "v7.2-temporal-memory-candidates-4"
INTERPRETER_VERSION = "deterministic-rules-4"
TIMEZONE_SCHEMA_VERSION = "v7.2-timezone-config-2"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_COUNTRY_OR_REGION = "CN"
STATEMENT_MAX_CHARS = 120
REASON_MAX_CHARS = 100
MAX_EVENTS_PER_EVIDENCE = 4
CONFLICT_MAX_DAYS = 366
SAFE_STATEMENT_PLACEHOLDER = "Source expression exceeds the safe statement limit; review anchored evidence."
CANDIDATE_TYPES = (
    "fact", "preference", "goal", "decision", "constraint",
    "acceptable_alternative", "open_question",
)
RELATION_TYPES = (
    "evolution", "coexistence", "conditional", "supersedes", "conflict", "unknown",
)
_RESERVED_IDENTITY_PAYLOADS = frozenset({"0" * 64, "f" * 64})
_SCOPE_PRECEDENCE = (
    "code", "quote", "blockquote", "third_party_report",
    "example", "hypothetical", "joke",
)
_SCOPE_REASON_CODES = {
    "code": "code_scope",
    "quote": "quoted_scope",
    "blockquote": "blockquote_scope",
    "third_party_report": "reported_speech_scope",
    "example": "example_scope",
    "hypothetical": "hypothetical_scope",
    "joke": "joke_scope",
}


class TemporalCandidateValidationError(ValueError):
    """The v7.2 package is malformed or cannot be reproduced."""


class _InterpretationProvider(Protocol):
    """Private future interface; v7.2 never invokes it."""

    provider_id: str

    def interpret(
        self, selected_evidence_envelope: tuple[dict[str, Any], ...],
        timezone_config: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _LocalDeterministicProvider:
    provider_id: str = "local-deterministic"


@dataclass(frozen=True)
class _ScopedSegment:
    text: str
    ordinal: int
    inherited_scopes: tuple[str, ...]
    attribution: dict[str, str]


def _selected_provider_envelope(selection: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return only selected anchors and text; omitted/source packages never cross this seam."""
    return tuple({
        "candidate_id": item["candidate_id"],
        "source_anchor": {
            "conversation_id": item["source"]["conversation"]["conversation_id"],
            "message_id": item["source"]["message_anchor"]["message_id"],
            "role": item["source"]["message_anchor"]["role"],
            "time": item["source"]["message_anchor"]["time"],
            "reference_uri": item["source"]["reference_uri"],
        },
        "excerpt": item["excerpt"],
        "evidence_ids": deepcopy(item["evidence_ids"]),
    } for item in selection["selected"])


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def default_timezone_config() -> dict[str, Any]:
    config = {
        "schema_version": TIMEZONE_SCHEMA_VERSION,
        "revision": 1,
        "country_or_region": DEFAULT_COUNTRY_OR_REGION,
        "timezone": DEFAULT_TIMEZONE,
        "source": "instance_default",
        "confirmation_status": "needs_user_confirmation",
        "predecessor": None,
        "change_reason": "first_use_default",
        "internal_time_standard": "UTC",
        "change_protocol": "trusted_predecessor_revision_chain_v1",
        "config_identity": "",
    }
    config["config_identity"] = "tzc_" + _digest({k: v for k, v in config.items() if k != "config_identity"})
    return config


def create_timezone_config(
    timezone_name: str, *, country_or_region: str, revision: int,
    confirmation_status: str, change_reason: str,
    previous_config: dict[str, Any] | None = None,
    previous_package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an in-memory config from a trusted predecessor; never persist it."""
    if previous_config is not None and previous_package is not None:
        raise TemporalCandidateValidationError("timezone revision accepts one trusted predecessor")
    predecessor = None
    trusted_previous = previous_config
    previous_package_identity = None
    if previous_package is not None:
        validate_temporal_candidates_standalone(previous_package)
        trusted_previous = previous_package["timezone_config"]
        previous_package_identity = previous_package["package_identity"]
    if revision == 1:
        if trusted_previous is not None:
            raise TemporalCandidateValidationError("first timezone revision cannot have a predecessor")
    else:
        if trusted_previous is None:
            raise TemporalCandidateValidationError("timezone revision requires a trusted predecessor")
        _validate_timezone_config(trusted_previous)
        if revision != trusted_previous["revision"] + 1:
            raise TemporalCandidateValidationError("timezone revision must increment the predecessor by one")
        predecessor = {
            "revision": trusted_previous["revision"],
            "config_identity": trusted_previous["config_identity"],
            "package_identity": previous_package_identity,
            "config": deepcopy(trusted_previous),
        }
    config = {
        "schema_version": TIMEZONE_SCHEMA_VERSION,
        "revision": revision,
        "country_or_region": country_or_region,
        "timezone": timezone_name,
        "source": "user_configured",
        "confirmation_status": confirmation_status,
        "predecessor": predecessor,
        "change_reason": change_reason,
        "internal_time_standard": "UTC",
        "change_protocol": "trusted_predecessor_revision_chain_v1",
        "config_identity": "",
    }
    _validate_timezone_config(config, allow_empty_identity=True)
    config["config_identity"] = "tzc_" + _digest({k: v for k, v in config.items() if k != "config_identity"})
    _validate_timezone_config(config)
    return config


class _TemporalCandidateCore:
    """Private dependency-injection seam for synthetic tests only."""

    def __init__(
        self, source_db: str | Path = DEFAULT_REAL_DB_PATH, *,
        selection_service: EvidenceSelectionService | None = None,
        provider: _InterpretationProvider | None = None,
    ) -> None:
        self.source_db = Path(source_db).expanduser().resolve()
        self.selection_service = selection_service or EvidenceSelectionService(self.source_db)
        self.provider = provider or _LocalDeterministicProvider()

    def build(
        self, query: RetrievalQuery, *, budget_profile: str = "Medium",
        timezone_config: dict[str, Any] | None = None,
        trusted_timezone_predecessor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.provider.provider_id != "local-deterministic" or not isinstance(
            self.provider, _LocalDeterministicProvider
        ):
            raise TemporalCandidateValidationError("v7.2 external interpretation providers are disabled")
        selection = self.selection_service.build(query, budget_profile=budget_profile)
        trusted_source_package = self.selection_service.memory_package_service.build(query)
        package = build_candidates_from_selection(
            selection, timezone_config=timezone_config or default_timezone_config(),
        )
        validate_temporal_candidates_source_backed(
            package, trusted_selection=selection, trusted_source_package=trusted_source_package,
            trusted_source_db=self.source_db,
            trusted_timezone_predecessor=trusted_timezone_predecessor,
        )
        return package


class TemporalCandidateService:
    """Fixed-production v7.2 service; accepts no source or provider injection."""

    def build(
        self, query: RetrievalQuery, *, budget_profile: str = "Medium",
        timezone_config: dict[str, Any] | None = None,
        trusted_timezone_predecessor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _TemporalCandidateCore().build(
            query, budget_profile=budget_profile, timezone_config=timezone_config,
            trusted_timezone_predecessor=trusted_timezone_predecessor,
        )


def build_production_temporal_candidates(
    query: RetrievalQuery, *, budget_profile: str = "Medium",
    timezone_config: dict[str, Any] | None = None,
    trusted_timezone_predecessor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return TemporalCandidateService().build(
        query, budget_profile=budget_profile, timezone_config=timezone_config,
        trusted_timezone_predecessor=trusted_timezone_predecessor,
    )


def build_candidates_from_selection(
    selection: dict[str, Any], *, timezone_config: dict[str, Any],
) -> dict[str, Any]:
    validate_evidence_selection_standalone(selection)
    tz_config = deepcopy(timezone_config)
    _validate_timezone_config(tz_config)
    events: list[dict[str, Any]] = []
    for selected in selection["selected"]:
        events.extend(_events_from_selected(selected, tz_config))
    events.sort(key=lambda item: (item["observed_at_utc"], item["event_id"]))
    candidates = [_candidate_from_event(event) for event in events]
    relations = _derive_relations(candidates, events)
    needs_review = sum(item["review_status"] == "needs_review" for item in candidates)
    source_projection = _source_projection(selection)
    status, reason = _status(source_projection, candidates, needs_review)
    package = {
        "protocol_version": PROTOCOL_VERSION,
        "package_identity": "",
        "status": status,
        "reason_code": reason,
        "readonly": True,
        "persisted": False,
        "candidate_only": True,
        "provider": {
            "provider_id": "local-deterministic",
            "interpreter_version": INTERPRETER_VERSION,
            "external_calls": False,
            "future_provider_interface_reserved": True,
        },
        "timezone_config": tz_config,
        "source_projection": source_projection,
        "limits": {
            "statement_max_chars": STATEMENT_MAX_CHARS,
            "reason_max_chars": REASON_MAX_CHARS,
            "max_events_per_evidence": MAX_EVENTS_PER_EVIDENCE,
            "overflow_policy": "safe_placeholder_and_needs_review",
        },
        "events": events,
        "candidates": candidates,
        "temporal_relations": relations,
        "counts": {
            "selected_evidence": len(selection["selected"]),
            "events": len(events),
            "candidates": len(candidates),
            "needs_review": needs_review,
            "relations": len(relations),
            "relation_types": {kind: sum(r["relation"] == kind for r in relations) for kind in RELATION_TYPES},
        },
    }
    package["package_identity"] = "tmc_" + _digest(_identity_payload(package))
    validate_temporal_candidates_standalone(package)
    return package


def canonical_temporal_candidates_json(package: dict[str, Any]) -> str:
    validate_temporal_candidates_standalone(package)
    return _dump(package)


def render_temporal_candidates(package: dict[str, Any]) -> str:
    validate_temporal_candidates_standalone(package)
    lines = [
        f"YunOS Temporal Memory Candidates {package['protocol_version']}",
        f"package_identity: {package['package_identity']}",
        f"source_selection_identity: {package['source_projection']['package_identity']}",
        f"status: {package['status']} ({package['reason_code']})",
        f"timezone: {package['timezone_config']['timezone']} country_or_region={package['timezone_config']['country_or_region']} revision={package['timezone_config']['revision']} confirmation={package['timezone_config']['confirmation_status']}",
        f"candidates: {package['counts']['candidates']}; needs_review: {package['counts']['needs_review']}; relations: {package['counts']['relations']}",
    ]
    for item in package["candidates"]:
        lines.append(
            f"- [{item['candidate_type']}/{item['review_status']}/{item['confidence']}] "
            f"{item['statement']} | {item['reason']} | {item['candidate_id']}"
        )
    for relation in package["temporal_relations"]:
        lines.append(
            f"- relation {relation['relation']}: {relation['newer_candidate_id']} -> "
            f"{relation['older_candidate_id']} | {relation['reason']}"
        )
    return "\n".join(lines) + "\n"


def validate_temporal_candidates_standalone(package: dict[str, Any]) -> None:
    if not isinstance(package, dict):
        raise TemporalCandidateValidationError("temporal candidate package must be an object")
    _exact_keys(package, {
        "protocol_version", "package_identity", "status", "reason_code", "readonly", "persisted",
        "candidate_only", "provider", "timezone_config", "source_projection", "limits", "events",
        "candidates", "temporal_relations", "counts",
    }, "temporal candidate package")
    if package["protocol_version"] != PROTOCOL_VERSION:
        raise TemporalCandidateValidationError("temporal candidate protocol version mismatch")
    if package["status"] not in {"ok", "partial", "empty", "unavailable"}:
        raise TemporalCandidateValidationError("invalid temporal candidate status")
    if package["readonly"] is not True or package["persisted"] is not False or package["candidate_only"] is not True:
        raise TemporalCandidateValidationError("v7.2 output must remain readonly candidate-only")
    if package["provider"] != {
        "provider_id": "local-deterministic", "interpreter_version": INTERPRETER_VERSION,
        "external_calls": False, "future_provider_interface_reserved": True,
    }:
        raise TemporalCandidateValidationError("unsupported interpretation provider state")
    _validate_timezone_config(package["timezone_config"])
    source = _validate_source_projection(package["source_projection"])
    if package["limits"] != {
        "statement_max_chars": STATEMENT_MAX_CHARS, "reason_max_chars": REASON_MAX_CHARS,
        "max_events_per_evidence": MAX_EVENTS_PER_EVIDENCE,
        "overflow_policy": "safe_placeholder_and_needs_review",
    }:
        raise TemporalCandidateValidationError("candidate character limits mismatch")
    events = [_validate_event(item, source, package["timezone_config"]) for item in package["events"]]
    if len({item["event_id"] for item in events}) != len(events):
        raise TemporalCandidateValidationError("duplicate event identity")
    if events != sorted(events, key=lambda item: (item["observed_at_utc"], item["event_id"])):
        raise TemporalCandidateValidationError("event ledger is not canonically ordered")
    expected_candidates = [_candidate_from_event(event) for event in events]
    if package["candidates"] != expected_candidates:
        raise TemporalCandidateValidationError("candidates do not match event ledger")
    expected_relations = _derive_relations(expected_candidates, events)
    if package["temporal_relations"] != expected_relations:
        raise TemporalCandidateValidationError("temporal relations are not reproducible")
    needs_review = sum(item["review_status"] == "needs_review" for item in expected_candidates)
    expected_counts = {
        "selected_evidence": source["selected_count"], "events": len(events),
        "candidates": len(expected_candidates), "needs_review": needs_review,
        "relations": len(expected_relations),
        "relation_types": {kind: sum(r["relation"] == kind for r in expected_relations) for kind in RELATION_TYPES},
    }
    if package["counts"] != expected_counts:
        raise TemporalCandidateValidationError("temporal candidate counts mismatch")
    expected_status = _status(source, expected_candidates, needs_review)
    if (package["status"], package["reason_code"]) != expected_status:
        raise TemporalCandidateValidationError("temporal candidate status mismatch")
    if package["package_identity"] != "tmc_" + _digest(_identity_payload(package)):
        raise TemporalCandidateValidationError("temporal candidate package identity mismatch")
    try:
        _reject_absolute_paths(package)
    except MemoryPackageValidationError as exc:
        raise TemporalCandidateValidationError("v7.2 package violates the v7.0 privacy boundary") from exc


def validate_temporal_candidates_source_backed(
    package: dict[str, Any], *, trusted_selection: dict[str, Any],
    trusted_source_package: dict[str, Any], trusted_source_db: str | Path,
    trusted_timezone_predecessor: dict[str, Any] | None = None,
) -> None:
    validate_temporal_candidates_standalone(package)
    _validate_trusted_timezone_predecessor(
        package["timezone_config"], trusted_timezone_predecessor,
    )
    validate_evidence_selection_source_backed(
        trusted_selection,
        trusted_source_package=trusted_source_package,
        trusted_source_db=trusted_source_db,
    )
    if package["source_projection"]["package_identity"] != trusted_selection["package_identity"]:
        raise TemporalCandidateValidationError("trusted selection identity mismatch")
    rebuilt = build_candidates_from_selection(
        trusted_selection, timezone_config=package["timezone_config"],
    )
    if package != rebuilt:
        raise TemporalCandidateValidationError("source-backed temporal reconstruction mismatch")

def validate_temporal_candidates_against_selection(
    package: dict[str, Any], *, trusted_selection: dict[str, Any],
    trusted_timezone_predecessor: dict[str, Any] | None = None,
) -> None:
    """Reproduce v7.2 from an already source-authenticated v7.1 package."""
    validate_temporal_candidates_standalone(package)
    _validate_trusted_timezone_predecessor(
        package["timezone_config"], trusted_timezone_predecessor,
    )
    validate_evidence_selection_standalone(trusted_selection)
    rebuilt = build_candidates_from_selection(trusted_selection, timezone_config=package["timezone_config"])
    if package != rebuilt:
        raise TemporalCandidateValidationError("trusted-selection temporal reconstruction mismatch")


def _identity_payload(package: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in package.items() if key != "package_identity"}


def _source_projection(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": selection["protocol_version"],
        "package_identity": selection["package_identity"],
        "status": selection["status"],
        "reason_code": selection["reason_code"],
        "readonly": selection["readonly"],
        "persisted": selection["persisted"],
        "request": deepcopy(selection["request"]),
        "capabilities": deepcopy(selection["capabilities"]),
        "degradation": deepcopy(selection["degradation"]),
        "warnings": deepcopy(selection["warnings"]),
        "missing_capabilities": deepcopy(selection["missing_capabilities"]),
        "source_memory_projection": deepcopy(selection["source_projection"]),
        "candidate_ledger": deepcopy(selection["candidate_ledger"]),
        "selected_count": len(selection["selected"]),
        "selected": [{
            "candidate_id": item["candidate_id"],
            "source": deepcopy(item["source"]),
            "evidence_ids": deepcopy(item["evidence_ids"]),
            "excerpt_commitment": next(
                ledger["excerpt_commitment"] for ledger in selection["candidate_ledger"]
                if ledger["candidate_id"] == item["candidate_id"]
            ),
        } for item in selection["selected"]],
    }


def _events_from_selected(selected: dict[str, Any], tz_config: dict[str, Any]) -> list[dict[str, Any]]:
    excerpt = unicodedata.normalize("NFC", selected["excerpt"].replace("\r\n", "\n").replace("\r", "\n"))
    events = []
    role = selected["source"]["message_anchor"]["role"]
    for segment in _scan_scoped_segments(excerpt, role=role)[:MAX_EVENTS_PER_EVIDENCE]:
        raw = segment.text
        ordinal = segment.ordinal
        attribution = segment.attribution
        if attribution["status"] == "rejected":
            continue
        interpreted = _interpret_clause(raw, role=role, attribution=attribution)
        if interpreted is None:
            continue
        statement, clipped = _bounded(raw, STATEMENT_MAX_CHARS)
        observed = _utc_iso(selected["source"]["message_anchor"]["time"])
        local_date = datetime.fromisoformat(observed.replace("Z", "+00:00")).astimezone(
            ZoneInfo(tz_config["timezone"])
        ).date().isoformat()
        event = {
            "event_id": "",
            "source_candidate_id": selected["candidate_id"],
            "evidence_ids": deepcopy(selected["evidence_ids"]),
            "source": deepcopy(selected["source"]),
            "source_excerpt_sha256": hashlib.sha256(selected["excerpt"].encode("utf-8")).hexdigest(),
            "clause_ordinal": ordinal,
            "clause_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "clause_unicode_codepoints": len(raw),
            "observed_at_utc": observed,
            "interpreted_local_date": local_date,
            "attribution": attribution,
            "candidate_type": interpreted["candidate_type"],
            "statement": statement,
            "explicitness": interpreted["explicitness"],
            "confidence": interpreted["confidence"],
            "overflowed": clipped,
            "review_status": "needs_review" if clipped or interpreted["explicitness"] != "explicit" else "ready_for_review",
            "reason": "",
            "subject_key": interpreted["subject_key"],
            "topic_key": interpreted["topic_key"],
            "target_keys": interpreted["target_keys"],
            "excluded_target_keys": interpreted["excluded_target_keys"],
            "polarity": interpreted["polarity"],
            "condition": interpreted["condition"],
            "markers": interpreted["markers"],
        }
        event["reason"] = _reason(event, clipped)
        event["event_id"] = "evt_" + _digest({k: v for k, v in event.items() if k != "event_id"})
        events.append(event)
    return events


def _scan_scoped_segments(text: str, *, role: str) -> list[_ScopedSegment]:
    """Scan one complete expression and carry conservative scopes across segments."""
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    persistent_scopes: set[str] = set()
    quote_stack: list[str] = []
    code_block = False
    buffer: list[str] = []
    segment_scopes: set[str] = set()
    segments: list[_ScopedSegment] = []
    quote_openers = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    quote_closers = set(quote_openers.values())

    def record_segment() -> None:
        nonlocal buffer, segment_scopes
        raw = "".join(buffer).strip()
        buffer = []
        if not raw:
            segment_scopes = set()
            return
        scopes = set(segment_scopes) | persistent_scopes
        activated = _persistent_scope_cues(raw)
        scopes.update(activated)
        persistent_scopes.update(activated)
        attribution = _segment_attribution(raw, role=role, inherited_scopes=scopes)
        segments.append(_ScopedSegment(
            text=raw,
            ordinal=len(segments) + 1,
            inherited_scopes=tuple(scope for scope in _SCOPE_PRECEDENCE if scope in scopes),
            attribution=attribution,
        ))
        segment_scopes = set()

    index = 0
    while index < len(normalized):
        segment_scopes.update(persistent_scopes)
        if code_block:
            segment_scopes.add("code")
        if quote_stack:
            segment_scopes.add("quote")
        if normalized.startswith("```", index):
            segment_scopes.add("code")
            buffer.append("```")
            code_block = not code_block
            index += 3
            continue
        character = normalized[index]
        if character == "\n":
            record_segment()
            index += 1
            continue
        if character == '"':
            segment_scopes.add("quote")
            if quote_stack and quote_stack[-1] == '"':
                quote_stack.pop()
            else:
                quote_stack.append('"')
        elif character in quote_openers:
            segment_scopes.add("quote")
            quote_stack.append(quote_openers[character])
        elif character in quote_closers:
            segment_scopes.add("quote")
            if quote_stack and quote_stack[-1] == character:
                quote_stack.pop()
        buffer.append(character)
        index += 1
        if character in "。！？!?":
            record_segment()
    record_segment()
    return segments


def _persistent_scope_cues(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    scopes: set[str] = set()
    if re.search(r"(?:^|\s)>\s", folded):
        scopes.add("blockquote")
    if re.search(
        r"(?:^|[\s，,。；;])(?:[^\s，,。！？!?：:]{1,16})(?:说|表示|称|提到|建议|认为)\s*[:：，,]?",
        folded,
    ) or re.search(
        r"\b(?:according to|(?:he|she|they|someone|friend|colleague|assistant|model|[a-z][a-z'-]{1,20})\s+(?:said|says|suggested|suggests|stated|thinks))\b",
        folded,
    ):
        scopes.add("third_party_report")
    if re.search(r"(?:示例文本如下|示例\s*[:：]|example\s*[:：])", folded):
        scopes.add("example")
    if re.search(
        r"(?:如果|假如|假设|假若|要是|倘若|设想|万一)|\b(?:if|hypothetically|suppose|imagine)\b",
        folded,
    ):
        scopes.add("hypothetical")
    if re.search(r"(?:开个玩笑|开玩笑|玩笑|反讽|哈哈|呵呵|just kidding|joking|sarcasm|\b(?:lol|lmao|jk)\b)", folded):
        scopes.add("joke")
    return scopes


def _segment_attribution(
    text: str, *, role: str, inherited_scopes: set[str],
) -> dict[str, str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    if role != "user":
        return {"status": "rejected", "reason_code": "non_user_role"}
    for scope in _SCOPE_PRECEDENCE:
        if scope in inherited_scopes:
            return {"status": "rejected", "reason_code": _SCOPE_REASON_CODES[scope]}
    if (
        re.search(r"(?<!\w)'[^'\n]{1,200}'(?!\w)", folded)
        or any(mark in folded for mark in ('"', "“", "”", "‘", "’", "「", "」", "『", "』"))
    ):
        return {"status": "rejected", "reason_code": "quoted_scope"}
    if (
        re.search(r"```|`[^`]*`|(?:^|[\s，,。；;])(?:代码|code)\s*[:：]", folded)
        or re.search(r"^\s*(?:>>>|#include\b|(?:def|class|function|const|let|var)\s+)", folded)
    ):
        return {"status": "rejected", "reason_code": "code_scope"}
    if re.fullmatch(r"\s*[（(【\[].*[）)】\]]\s*", folded, flags=re.DOTALL):
        return {"status": "rejected", "reason_code": "parenthetical_scope"}
    if re.search(r"(?:难道|岂不是|难不成|怎么可能|反问|isn't it|aren't i|wouldn't i|could it really)", folded):
        return {"status": "rejected", "reason_code": "rhetorical_scope"}
    if re.search(r"(?:这|那)?(?:不是|并非)(?:一个)?事实|not (?:a )?fact", folded):
        return {"status": "rejected", "reason_code": "negated_meta_assertion"}
    if re.search(
        r"[？?]\s*$|(?:吗|么|呢)\s*$|(?:是否|是不是|有没有|为何|为什么|怎么会)",
        folded,
    ):
        return {"status": "user_question", "reason_code": "direct_question_scope"}
    return {"status": "user_direct", "reason_code": "user_role_direct_expression"}


def _attribution_gate(text: str, *, role: str) -> dict[str, str]:
    """Compatibility wrapper over the complete-expression scope scanner."""
    segments = _scan_scoped_segments(text, role=role)
    if not segments:
        return {"status": "rejected", "reason_code": "empty_expression"}
    rejected = next((item.attribution for item in segments if item.attribution["status"] == "rejected"), None)
    if rejected is not None:
        return rejected
    question = next((item.attribution for item in segments if item.attribution["status"] == "user_question"), None)
    return question or {"status": "user_direct", "reason_code": "user_role_direct_expression"}


def _interpret_clause(
    text: str, *, role: str = "user", attribution: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    scope = attribution or _attribution_gate(text, role=role)
    if scope["status"] == "rejected":
        return None
    folded = unicodedata.normalize("NFKC", text).casefold()
    speculative = bool(re.search(r"(?:好像|似乎|也许|可能|大概|猜测|maybe|perhaps|might|probably|seems?)", folded))
    uncertain = bool(re.search(r"(?:不知道|不确定|尚不确定|待确认|unclear|unknown|not sure)", folded))
    non_exclusive = bool(re.search(r"(?:不是只|并非只|不只是|not only|not exclusively)", folded))
    rules = [
        ("acceptable_alternative", r"(?:也可以|可以接受|可接受|备选|acceptable alternative|alternatively|also acceptable|either .+ is fine)"),
        ("open_question", r"(?:不知道(?:是否|有没|买没)|不确定(?:是否|有没)|尚不确定|待确认|需要确认|open question|unclear whether|unknown whether|not sure whether)"),
        ("decision", r"(?:(?:我|我们)(?:已经)?决定|\b(?:i|we) decided\b|(?:^|\s)decision\s*[:：]|(?:我|我们).*(?:改用|替换为))"),
        ("goal", r"(?:我的目标|我们的目标|(?:^|\s)目标\s*[:：]|\bmy goal\b|\bour goal\b|(?:^|\s)goal\s*[:：])"),
        ("constraint", r"(?:约束\s*[:：]|constraint\s*[:：]|必须|不得|不能|禁止|required|must\b|must not|cannot)"),
        ("preference", r"(?:我(?:现在)?更想|我(?:更)?倾向|(?:早期|以前|过去).*考虑|后来更倾向|我偏好|我更喜欢|我喜欢|好像更适合我|似乎更适合我|preference\s*[:：]|\b(?:i|we) prefer\b|\bprefer\s+)"),
        ("fact", r"(?:事实\s*[:：]|(?:^|\s)(?:confirmed )?fact\s*[:：]|我(?:还没|尚未|没有)买|\bi (?:have not|haven't) bought\b)"),
    ]
    candidate_type = next((kind for kind, pattern in rules if re.search(pattern, folded)), None)
    if candidate_type is None:
        return None
    if scope["status"] == "user_question" and candidate_type != "open_question":
        return None
    if candidate_type == "decision" and re.search(r"(?:还没|没有|尚未|not)决定|not decided", folded):
        return None
    condition = _condition_code(folded)
    targets, excluded = _extract_targets(folded)
    explicit_positive = bool(re.search(
        r"(?:目标(?:是)?只有|目标(?:是)?仅限于|唯一(?:目标|选择)(?:是)?|only (?:goal|choice)|(?:决定|decision).*?(?:改用|replace).*?(?:with|为))",
        folded,
    ))
    explicit_exclusion = bool(re.search(
        r"(?:不再考虑|排除|不再选择|不要再|no longer consider|exclude|instead of|replace .+ with)",
        folded,
    ))
    exclusive = bool(
        explicit_positive and explicit_exclusion and not non_exclusive and targets and excluded
    )
    replacement = exclusive
    coexistence = bool(re.search(
        r"(?:并存|同时保留|都可以|也可以|也喜欢|both|coexist|also (?:prefer|like|accept)|also acceptable)",
        folded,
    ))
    evolution = bool(re.search(r"(?:早期|以前|过去|后来|现在|如今|变为|改用|previously|later|now|changed|from .+ to )", folded))
    uncertain = uncertain or scope["status"] == "user_question"
    polarity = _primary_polarity(candidate_type, folded, uncertain=uncertain)
    explicitness = "speculative" if speculative else "ambiguous" if uncertain else "explicit"
    confidence = "low" if speculative or uncertain else "high"
    topic_basis = _topic_basis(candidate_type, targets | excluded)
    topic_key = "topic_" + hashlib.sha256(topic_basis.encode("utf-8")).hexdigest()[:20]
    target_keys = sorted(_target_key(value) for value in targets)
    excluded_target_keys = sorted(_target_key(value) for value in excluded)
    return {
        "candidate_type": candidate_type,
        "explicitness": explicitness,
        "confidence": confidence,
        "subject_key": topic_key,
        "topic_key": topic_key,
        "target_keys": target_keys,
        "excluded_target_keys": excluded_target_keys,
        "polarity": polarity,
        "condition": condition,
        "markers": {
            "evolution": evolution,
            "coexistence": coexistence,
            "conditional": condition is not None,
            "exclusive_replacement": replacement,
            "exclusive": exclusive,
        },
    }


def _primary_polarity(candidate_type: str, folded: str, *, uncertain: bool) -> str:
    """Determine polarity for the primary semantic object, excluding excluded targets."""
    if uncertain:
        return "uncertain"
    negative_patterns = {
        "fact": r"(?:还没|尚未|没有)买|未购买|\b(?:have not|haven't) bought\b",
        "decision": r"(?:决定|decided)\s*(?:不买|不要|不再|not to|against)",
        "goal": r"(?:我的目标|我们的目标|\b(?:my|our) goal\b)\s*(?:不是|并非|not\b)",
        "constraint": r"(?:不得|不能|禁止|must not|cannot|do not)",
    }
    pattern = negative_patterns.get(candidate_type)
    if pattern and re.search(pattern, folded):
        return "negative"
    if candidate_type == "constraint" and re.search(r"(?:必须|required|must\b)", folded):
        return "required"
    return "positive"


def _condition_code(folded: str) -> str | None:
    if re.search(r"(?:便宜很多|价格低很多|明显更便宜|much cheaper|price is much lower)", folded):
        return "price_lower"
    match = re.search(r"(?:如果|当|在)(.{1,32}?)(?:时|，|,)|(?:if|when)\s+(.{1,32}?)(?:,| then )", folded)
    if not match:
        return None
    raw = next(group for group in match.groups() if group)
    normalized = " ".join(raw.split())
    return "context_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _extract_targets(folded: str) -> tuple[set[str], set[str]]:
    known = {
        token.casefold() for token in re.findall(
            r"(?i)\b(macbook|air|pro|tls|tea|coffee|water|bus|train|car)\b", folded,
        )
    }
    known.update(re.findall(r"(?:\buse|\breplace|\bwith)\s+([ab])\b", folded))
    excluded: set[str] = set()
    exclusion_patterns = (
        r"(?:不再考虑|排除|不再选择|不要再)\s*([a-z][a-z0-9_-]{0,31})",
        r"(?:no longer consider|exclude|instead of)\s+([a-z][a-z0-9_-]{0,31})",
        r"replace\s+([a-z][a-z0-9_-]{0,31})\s+with",
    )
    for pattern in exclusion_patterns:
        excluded.update(match.casefold() for match in re.findall(pattern, folded))
    targets = known - excluded
    return targets, excluded


def _topic_basis(candidate_type: str, targets: set[str]) -> str:
    if targets & {"air", "pro", "macbook"}:
        return "product_choice_macbook"
    if targets & {"tea", "coffee", "water"}:
        return "beverage_preference"
    if "tls" in targets:
        return "tls_usage"
    if targets & {"bus", "train", "car"}:
        return "transport_choice"
    if targets & {"a", "b"}:
        return "abstract_choice"
    return "unresolved_" + candidate_type


def _target_key(value: str) -> str:
    return "target_" + hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:20]


def _candidate_from_event(event: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "candidate_id": "",
        "state": "candidate",
        "candidate_type": event["candidate_type"],
        "statement": event["statement"],
        "review_status": event["review_status"],
        "explicitness": event["explicitness"],
        "confidence": event["confidence"],
        "reason": event["reason"],
        "event_id": event["event_id"],
        "evidence": {
            "evidence_ids": deepcopy(event["evidence_ids"]),
            "conversation_id": event["source"]["conversation"]["conversation_id"],
            "message_id": event["source"]["message_anchor"]["message_id"],
            "reference_uri": event["source"]["reference_uri"],
            "observed_at_utc": event["observed_at_utc"],
        },
    }
    candidate["candidate_id"] = "mc_" + _digest({k: v for k, v in candidate.items() if k != "candidate_id"})
    return candidate


def _derive_relations(candidates: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relations = []
    by_id = {candidate["event_id"]: candidate for candidate in candidates}
    history: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        prior = history.get(event["topic_key"], [])
        if prior:
            older = _relation_predecessor(event, prior)
            relation, reason, exclusive = _relation(event, older)
            newer_candidate = by_id[event["event_id"]]
            older_candidate = by_id[older["event_id"]]
            item = {
                "relation_id": "",
                "newer_candidate_id": newer_candidate["candidate_id"],
                "older_candidate_id": older_candidate["candidate_id"],
                "relation": relation,
                "exclusive": exclusive,
                "reason": reason,
                "evidence_event_ids": [older["event_id"], event["event_id"]],
            }
            item["relation_id"] = "tr_" + _digest({k: v for k, v in item.items() if k != "relation_id"})
            relations.append(item)
        history.setdefault(event["topic_key"], []).append(event)
    return relations


def _relation_predecessor(newer: dict[str, Any], prior: list[dict[str, Any]]) -> dict[str, Any]:
    if newer["markers"]["exclusive_replacement"]:
        excluded = set(newer["excluded_target_keys"])
        matches = [item for item in prior if excluded & set(item["target_keys"])]
        if matches:
            return matches[-1]
    if newer["candidate_type"] == "preference":
        preferences = [item for item in prior if item["candidate_type"] == "preference"]
        if preferences:
            return preferences[-1]
    if newer["candidate_type"] == "acceptable_alternative":
        primary = [item for item in prior if item["candidate_type"] in {"preference", "goal", "decision"}]
        if primary:
            return primary[-1]
    return prior[-1]


def _relation(newer: dict[str, Any], older: dict[str, Any]) -> tuple[str, str, bool]:
    markers = newer["markers"]
    newer_targets = set(newer["target_keys"])
    older_targets = set(older["target_keys"])
    if markers["exclusive_replacement"] and set(newer["excluded_target_keys"]) & older_targets:
        return "supersedes", "Explicit replacement language makes this candidate exclusive.", True
    if newer["candidate_type"] == "acceptable_alternative" and newer_targets != older_targets:
        return "coexistence", "A non-exclusive alternative preserves the earlier candidate.", False
    if newer["condition"] != older["condition"] and (newer["condition"] or older["condition"]):
        return "conditional", "The candidates apply under stated conditions.", False
    same_condition = newer["condition"] == older["condition"]
    same_target = bool(newer_targets and newer_targets == older_targets)
    same_state_kind = newer["candidate_type"] == older["candidate_type"]
    both_explicit = newer["explicitness"] == older["explicitness"] == "explicit"
    incompatible = {newer["polarity"], older["polarity"]} in (
        {"positive", "negative"}, {"required", "negative"},
    )
    observed_new = datetime.fromisoformat(newer["observed_at_utc"].replace("Z", "+00:00"))
    observed_old = datetime.fromisoformat(older["observed_at_utc"].replace("Z", "+00:00"))
    if (
        same_state_kind and both_explicit and same_target and same_condition and incompatible
        and (observed_new - observed_old).days <= CONFLICT_MAX_DAYS
    ):
        return "conflict", "Equivalent topic, target, condition and time have incompatible polarity.", False
    if markers["coexistence"] or older["markers"]["coexistence"]:
        return "coexistence", "Explicit coexistence language preserves both candidates.", False
    if newer["candidate_type"] == "preference" and older["candidate_type"] == "preference" and newer_targets != older_targets:
        return "evolution", "A later preference changes direction without excluding the earlier option.", False
    if markers["evolution"]:
        return "evolution", "Explicit time or change language shows evolution without replacement.", False
    return "unknown", "Evidence does not establish replacement, coexistence, condition, or conflict.", False


def _reason(event: dict[str, Any], clipped: bool) -> str:
    if clipped:
        return "Source clause exceeded the statement limit; review the anchored evidence."
    if event["explicitness"] == "speculative":
        return "Speculative wording is preserved as needs_review, not asserted as fact."
    if event["explicitness"] == "ambiguous":
        return "Ambiguous wording remains needs_review; no real-world state is asserted."
    return "Explicit local rule match; candidate remains unapproved."


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value, False
    return SAFE_STATEMENT_PLACEHOLDER, True


def _utc_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise TemporalCandidateValidationError("invalid evidence anchor time") from exc
    if parsed.tzinfo is None:
        raise TemporalCandidateValidationError("evidence anchor time must include an offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timezone_config(config: dict[str, Any], *, allow_empty_identity: bool = False) -> None:
    _exact_keys(config, {
        "schema_version", "revision", "country_or_region", "timezone", "source", "confirmation_status",
        "predecessor", "change_reason", "internal_time_standard",
        "change_protocol", "config_identity",
    }, "timezone config")
    if config["schema_version"] != TIMEZONE_SCHEMA_VERSION or config["internal_time_standard"] != "UTC":
        raise TemporalCandidateValidationError("invalid timezone config protocol")
    if config["change_protocol"] != "trusted_predecessor_revision_chain_v1":
        raise TemporalCandidateValidationError("invalid timezone change protocol")
    if isinstance(config["revision"], bool) or not isinstance(config["revision"], int) or config["revision"] < 1:
        raise TemporalCandidateValidationError("timezone revision must be positive")
    if config["source"] not in {"instance_default", "user_configured"}:
        raise TemporalCandidateValidationError("invalid timezone config source")
    if config["confirmation_status"] not in {"needs_user_confirmation", "confirmed"}:
        raise TemporalCandidateValidationError("invalid timezone confirmation state")
    if not isinstance(config["country_or_region"], str) or not re.fullmatch(
        r"(?:[A-Z]{2}|UNSPECIFIED)", config["country_or_region"]
    ):
        raise TemporalCandidateValidationError("invalid country or region")
    if config["revision"] == 1:
        if config["predecessor"] is not None:
            raise TemporalCandidateValidationError("first timezone revision cannot have a predecessor")
    else:
        _exact_keys(config["predecessor"], {
            "revision", "config_identity", "package_identity", "config",
        }, "timezone predecessor")
        predecessor = config["predecessor"]
        if predecessor["revision"] != config["revision"] - 1:
            raise TemporalCandidateValidationError("timezone predecessor revision is not contiguous")
        if not isinstance(predecessor["config_identity"], str) or not re.fullmatch(
            r"tzc_[0-9a-f]{64}", predecessor["config_identity"]
        ) or _is_reserved_identity(predecessor["config_identity"], "tzc_"):
            raise TemporalCandidateValidationError("invalid timezone predecessor identity")
        if predecessor["package_identity"] is not None and (
            not isinstance(predecessor["package_identity"], str)
            or not re.fullmatch(r"tmc_[0-9a-f]{64}", predecessor["package_identity"])
            or _is_reserved_identity(predecessor["package_identity"], "tmc_")
        ):
            raise TemporalCandidateValidationError("invalid timezone predecessor package identity")
        _validate_timezone_config(predecessor["config"])
        if (
            predecessor["config"]["revision"] != predecessor["revision"]
            or predecessor["config"]["config_identity"] != predecessor["config_identity"]
        ):
            raise TemporalCandidateValidationError("timezone predecessor content mismatch")
    if not isinstance(config["change_reason"], str) or not 1 <= len(config["change_reason"]) <= 80:
        raise TemporalCandidateValidationError("invalid timezone change reason")
    if config["source"] == "instance_default" and (
        config["revision"] != 1
        or config["country_or_region"] != DEFAULT_COUNTRY_OR_REGION
        or config["timezone"] != DEFAULT_TIMEZONE
        or config["confirmation_status"] != "needs_user_confirmation"
        or config["change_reason"] != "first_use_default"
    ):
        raise TemporalCandidateValidationError("instance-default timezone config is not canonical")
    try:
        ZoneInfo(config["timezone"])
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise TemporalCandidateValidationError("unknown IANA timezone") from exc
    if not allow_empty_identity and _is_reserved_identity(config["config_identity"], "tzc_"):
        raise TemporalCandidateValidationError("reserved timezone config identity")
    expected = "tzc_" + _digest({k: v for k, v in config.items() if k != "config_identity"})
    if not allow_empty_identity and config["config_identity"] != expected:
        raise TemporalCandidateValidationError("timezone config identity mismatch")


def _is_reserved_identity(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and value[len(prefix):] in _RESERVED_IDENTITY_PAYLOADS


def _validate_trusted_timezone_predecessor(
    config: dict[str, Any], trusted_predecessor: dict[str, Any] | None,
) -> None:
    """Bind revision >1 to caller-supplied trust, not the current package claim."""
    _validate_timezone_config(config)
    if config["revision"] == 1:
        if trusted_predecessor is not None:
            raise TemporalCandidateValidationError("first timezone revision cannot use trusted predecessor context")
        return
    if trusted_predecessor is None:
        raise TemporalCandidateValidationError("trusted timezone predecessor context is required")
    if "timezone_config" in trusted_predecessor or "package_identity" in trusted_predecessor:
        validate_temporal_candidates_standalone(trusted_predecessor)
        trusted_config = trusted_predecessor["timezone_config"]
        trusted_package_identity = trusted_predecessor["package_identity"]
    else:
        _validate_timezone_config(trusted_predecessor)
        trusted_config = trusted_predecessor
        trusted_package_identity = None
    predecessor = config["predecessor"]
    if (
        predecessor["config"] != trusted_config
        or predecessor["revision"] != trusted_config["revision"]
        or predecessor["config_identity"] != trusted_config["config_identity"]
        or predecessor["package_identity"] != trusted_package_identity
        or config["revision"] != trusted_config["revision"] + 1
    ):
        raise TemporalCandidateValidationError("trusted timezone predecessor binding mismatch")


def _validate_source_projection(source: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(source, {
        "protocol_version", "package_identity", "status", "reason_code", "readonly", "persisted",
        "request", "capabilities", "degradation", "warnings", "missing_capabilities",
        "source_memory_projection", "candidate_ledger", "selected_count", "selected",
    }, "source projection")
    if source["protocol_version"] != SOURCE_PROTOCOL_VERSION:
        raise TemporalCandidateValidationError("v7.1 source protocol mismatch")
    if not re.fullmatch(r"msp_[0-9a-f]{64}", source["package_identity"]):
        raise TemporalCandidateValidationError("invalid v7.1 source identity")
    if source["readonly"] is not True or source["persisted"] is not False:
        raise TemporalCandidateValidationError("v7.1 source must be readonly and non-persisted")
    try:
        ledger = _validate_v71_candidate_ledger(source["candidate_ledger"])
        memory_source = _validate_v71_source_projection(source["source_memory_projection"], ledger)
    except EvidenceSelectionValidationError as exc:
        raise TemporalCandidateValidationError("invalid v7.1 capability projection") from exc
    if source["request"] != memory_source["request"]:
        raise TemporalCandidateValidationError("v7.1 request projection mismatch")
    for field in ("capabilities", "degradation", "warnings", "missing_capabilities"):
        if source[field] != memory_source[field]:
            raise TemporalCandidateValidationError(f"v7.1 {field} projection mismatch")
    if not isinstance(source["selected"], list) or source["selected_count"] != len(source["selected"]):
        raise TemporalCandidateValidationError("invalid selected source projection")
    if (source["status"] in {"ok", "partial"}) != bool(source["selected"]):
        raise TemporalCandidateValidationError("v7.1 selected count contradicts status")
    ids = set()
    ledger_by_id = {item["candidate_id"]: item for item in ledger}
    for item in source["selected"]:
        _exact_keys(item, {"candidate_id", "source", "evidence_ids", "excerpt_commitment"}, "selected source")
        if not re.fullmatch(r"cand_[0-9a-f]{64}", item["candidate_id"]):
            raise TemporalCandidateValidationError("invalid selected candidate identity")
        if not isinstance(item["evidence_ids"], list) or not item["evidence_ids"] or not all(
            isinstance(value, str) and re.fullmatch(r"ev_[0-9a-f]{64}", value)
            for value in item["evidence_ids"]
        ):
            raise TemporalCandidateValidationError("invalid selected evidence identities")
        _exact_keys(item["source"], {"conversation", "message_anchor", "reference_uri"}, "selected source anchor")
        _exact_keys(item["source"]["conversation"], {"conversation_id", "title"}, "selected conversation")
        _exact_keys(item["source"]["message_anchor"], {"message_id", "role", "time", "timestamp"}, "selected message anchor")
        _exact_keys(item["excerpt_commitment"], {
            "profile", "sha256", "unicode_codepoints", "normalized_sha256",
            "normalized_unicode_codepoints",
        }, "selected excerpt commitment")
        if not re.fullmatch(r"[0-9a-f]{64}", item["excerpt_commitment"]["sha256"]):
            raise TemporalCandidateValidationError("invalid selected excerpt commitment")
        ledger_item = ledger_by_id.get(item["candidate_id"])
        if ledger_item is None or any(
            item[field] != ledger_item[field]
            for field in ("source", "evidence_ids", "excerpt_commitment")
        ):
            raise TemporalCandidateValidationError("selected projection does not match the v7.1 ledger")
        if item["candidate_id"] in ids:
            raise TemporalCandidateValidationError("duplicate source candidate identity")
        ids.add(item["candidate_id"])
    expected_status = _v71_selection_status(
        memory_source["status"], memory_source["reason_code"], bool(source["selected"]), bool(ledger),
    )
    if (source["status"], source["reason_code"]) != expected_status:
        raise TemporalCandidateValidationError("v7.1 status projection mismatch")
    return source


def _validate_event(
    event: dict[str, Any], source: dict[str, Any], timezone_config: dict[str, Any],
) -> dict[str, Any]:
    _exact_keys(event, {
        "event_id", "source_candidate_id", "evidence_ids", "source", "source_excerpt_sha256",
        "clause_ordinal", "clause_sha256", "clause_unicode_codepoints", "observed_at_utc",
        "interpreted_local_date", "attribution", "candidate_type",
        "statement", "explicitness", "confidence", "overflowed", "review_status", "reason", "subject_key",
        "topic_key", "target_keys", "excluded_target_keys", "polarity", "condition", "markers",
    }, "event")
    projected = next((item for item in source["selected"] if item["candidate_id"] == event["source_candidate_id"]), None)
    if projected is None or event["source"] != projected["source"] or event["evidence_ids"] != projected["evidence_ids"]:
        raise TemporalCandidateValidationError("event source anchor mismatch")
    if event["source_excerpt_sha256"] != projected["excerpt_commitment"].get("sha256"):
        raise TemporalCandidateValidationError("event excerpt commitment mismatch")
    _exact_keys(event["attribution"], {"status", "reason_code"}, "event attribution")
    valid_attribution = {
        ("user_direct", "user_role_direct_expression"),
        ("user_question", "direct_question_scope"),
    }
    if (
        (event["attribution"]["status"], event["attribution"]["reason_code"])
        not in valid_attribution
        or event["source"]["message_anchor"]["role"] != "user"
    ):
        raise TemporalCandidateValidationError("event is not directly attributable to the user")
    if event["candidate_type"] not in CANDIDATE_TYPES:
        raise TemporalCandidateValidationError("invalid candidate type")
    if event["explicitness"] not in {"explicit", "speculative", "ambiguous"} or event["confidence"] not in {"high", "low"}:
        raise TemporalCandidateValidationError("invalid candidate evidence assessment")
    if event["attribution"]["status"] == "user_question" and (
        event["candidate_type"] != "open_question" or event["explicitness"] != "ambiguous"
    ):
        raise TemporalCandidateValidationError("question scope can only produce an ambiguous open question")
    if event["confidence"] != ("high" if event["explicitness"] == "explicit" else "low"):
        raise TemporalCandidateValidationError("candidate confidence does not match explicitness")
    if not isinstance(event["overflowed"], bool):
        raise TemporalCandidateValidationError("invalid candidate overflow state")
    if event["review_status"] not in {"ready_for_review", "needs_review"}:
        raise TemporalCandidateValidationError("invalid candidate review status")
    expected_review = "needs_review" if event["overflowed"] or event["explicitness"] != "explicit" else "ready_for_review"
    if event["review_status"] != expected_review or event["reason"] != _reason(event, event["overflowed"]):
        raise TemporalCandidateValidationError("candidate review assessment mismatch")
    if not isinstance(event["statement"], str) or not event["statement"] or len(event["statement"]) > STATEMENT_MAX_CHARS:
        raise TemporalCandidateValidationError("candidate statement exceeds strict limit")
    if event["overflowed"] != (event["statement"] == SAFE_STATEMENT_PLACEHOLDER):
        raise TemporalCandidateValidationError("candidate safe-overflow representation mismatch")
    if not isinstance(event["reason"], str) or not event["reason"] or len(event["reason"]) > REASON_MAX_CHARS:
        raise TemporalCandidateValidationError("candidate reason exceeds strict limit")
    if not isinstance(event["clause_ordinal"], int) or not 1 <= event["clause_ordinal"] <= MAX_EVENTS_PER_EVIDENCE:
        raise TemporalCandidateValidationError("invalid event clause ordinal")
    if not isinstance(event["clause_unicode_codepoints"], int) or isinstance(
        event["clause_unicode_codepoints"], bool
    ) or event["clause_unicode_codepoints"] < 1:
        raise TemporalCandidateValidationError("invalid source clause length")
    if not isinstance(event["clause_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["clause_sha256"]):
        raise TemporalCandidateValidationError("invalid source clause commitment")
    if not re.fullmatch(r"topic_[0-9a-f]{20}", event["topic_key"]) or event["subject_key"] != event["topic_key"]:
        raise TemporalCandidateValidationError("invalid event topic identity")
    for field in ("target_keys", "excluded_target_keys"):
        values = event[field]
        if not isinstance(values, list) or values != sorted(set(values)) or any(
            not isinstance(value, str) or not re.fullmatch(r"target_[0-9a-f]{20}", value)
            for value in values
        ):
            raise TemporalCandidateValidationError(f"invalid event {field}")
    if set(event["target_keys"]) & set(event["excluded_target_keys"]):
        raise TemporalCandidateValidationError("event targets and exclusions overlap")
    if event["polarity"] not in {"positive", "negative", "required", "uncertain"}:
        raise TemporalCandidateValidationError("invalid event polarity")
    if event["condition"] is not None and (
        not isinstance(event["condition"], str)
        or not re.fullmatch(r"(?:price_lower|context_[0-9a-f]{20})", event["condition"])
    ):
        raise TemporalCandidateValidationError("invalid event condition")
    _exact_keys(event["markers"], {
        "evolution", "coexistence", "conditional", "exclusive_replacement", "exclusive",
    }, "event markers")
    if any(not isinstance(value, bool) for value in event["markers"].values()):
        raise TemporalCandidateValidationError("event markers must be booleans")
    if event["markers"]["conditional"] != (event["condition"] is not None):
        raise TemporalCandidateValidationError("event conditional marker mismatch")
    if event["markers"]["exclusive_replacement"] and (
        not event["markers"]["exclusive"] or not event["target_keys"]
        or not event["excluded_target_keys"] or event["polarity"] != "positive"
    ):
        raise TemporalCandidateValidationError("invalid exclusive replacement marker")
    if event["observed_at_utc"] != _utc_iso(event["observed_at_utc"]):
        raise TemporalCandidateValidationError("event internal time must be canonical UTC")
    if event["observed_at_utc"] != _utc_iso(event["source"]["message_anchor"]["time"]):
        raise TemporalCandidateValidationError("event time does not match the source anchor")
    expected_local_date = datetime.fromisoformat(
        event["observed_at_utc"].replace("Z", "+00:00")
    ).astimezone(ZoneInfo(timezone_config["timezone"])).date().isoformat()
    if event["interpreted_local_date"] != expected_local_date:
        raise TemporalCandidateValidationError("event local-date interpretation mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", event["source_excerpt_sha256"]):
        raise TemporalCandidateValidationError("invalid source excerpt commitment")
    if event["event_id"] != "evt_" + _digest({k: v for k, v in event.items() if k != "event_id"}):
        raise TemporalCandidateValidationError("event identity mismatch")
    return event


def _status(source: dict[str, Any], candidates: list[dict[str, Any]], needs_review: int) -> tuple[str, str]:
    if source["status"] == "unavailable":
        return "unavailable", source["reason_code"]
    if source["status"] == "partial":
        return "partial", (
            "partial_source_candidates" if candidates
            else "partial_source_no_interpretable_candidate"
        )
    if not candidates:
        return "empty", "no_interpretable_candidate"
    if needs_review:
        return "partial", "candidate_review_required"
    return "ok", "candidate_preview_complete"


def _exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise TemporalCandidateValidationError(f"{label} fields mismatch")
