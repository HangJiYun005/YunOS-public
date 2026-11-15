"""YunOS v8.0-E production Delivery integration.

This versioned layer composes, but does not modify, the sealed v7.4 Delivery.
It is the single production builder used by Gate C UI and runtime previews.
All source reads are fixed-path, immutable SQLite reads; no model or credential
surface is reachable from this module.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable

from memory.context_assembly import estimate_tokens, redact_context_text
from memory.context_delivery import (
    build_production_context_delivery as build_sealed_v74_delivery,
    validate_context_delivery_standalone,
)
from memory.db_path_policy import DEFAULT_REAL_DB_PATH
from memory.graph_enhanced_retrieval import RetrievalQuery
from memory.profile_enablement import active_profile_enablement_projection
from memory.runtime_protocol_contracts import (
    canonical_digest,
    canonical_json,
    production_assembly_identity,
    production_delivery_identity,
    production_evidence_identity,
    text_sha256,
)


PROTOCOL_VERSION = "v8.0-e-production-delivery-1"
DELIVERY_IDENTITY_VERSION = "v8.0-e-production-delivery-identity-1"
ASSEMBLY_PROTOCOL_VERSION = "v8.0-e-production-context-assembly-1"
ASSEMBLY_IDENTITY_VERSION = "v8.0-e-production-context-assembly-identity-1"
PROFILE_POLICY_VERSION = "v8.0-e-active-model-visible-all-1"
LEXICAL_FALLBACK_VERSION = "v8.0-e-readonly-fts-fallback-1"
SOURCE_DB = DEFAULT_REAL_DB_PATH
MAX_LEXICAL_POOL = 512
DEFAULT_FALLBACK_EVIDENCE = 10

_PROFILE_ID_RE = re.compile(r"upi_[0-9a-f]{64}")
_PROJECTION_ID_RE = re.compile(r"app_[0-9a-f]{64}")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{1,63}")
_HAN_RE = re.compile(r"[\u3400-\u9fff]{2,12}")
_PRICE_RE = re.compile(
    r"(?:\b\d{3,6}(?:\.\d+)?\s*(?:rmb|cny|元|块|日元)\b|"
    r"(?:价格|优惠|补贴|国补|折扣|促销|券|满减|免息|成交价|原价|低于|以下|以内))",
    re.IGNORECASE,
)

# Control-plane words should not become content seeds.  This is query-shape
# filtering, not a product/message allowlist.
_CONTROL_TERMS = {
    "about", "actually", "and", "anchors", "approved", "based", "choosing",
    "cite", "conversation", "distinguish", "evidence", "exclusions", "facts",
    "from", "historical", "history", "message", "points", "preferences",
    "prior", "profile", "relevant", "remain", "supported", "uncertain", "what",
    "which", "with", "yunos", "the", "this", "that", "user", "current",
    "are", "was", "were", "has", "have", "had", "can", "could", "should",
}

_THEME_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("long_term_use", ("长期", "很多年", "永远", "一直", "高频", "随身携带", "long-term")),
    ("experience_priority", ("体验", "舒服", "质感", "安静", "屏幕", "刷新率", "风扇", "experience", "comfort")),
    ("display_condition", ("60hz", "120hz", "60 hz", "120 hz", "无法接受", "受不了", "刷新率")),
    ("mobility_battery", ("续航", "电池", "随身", "出门", "包里", "不用充电", "插座", "重量", "飞机", "图书馆", "battery", "portable")),
    ("target_configuration", ("目标配置", "配置", "内存", "ssd", "24g", "32g", "1tb", "1t", "m5", "m6")),
    ("price_threshold", ("预算", "价格", "国补", "优惠", "低于", "以下", "以内", "below", "14999", "15000", "15,000", "14500", "京东", "rmb")),
    ("purchase_plan", ("计划", "优先", "下一步", "等到", "到时候", "双十一", "九月", "春节", "购买", "买", "monitor", "after september")),
    ("uncertain_or_changed", ("可能", "看情况", "再说", "如果", "但是", "不过", "虽然", "不一定", "不重要", "买不起", "uncertain", "maybe")),
)


class ProductionDeliveryValidationError(ValueError):
    """The v8.0-E Delivery is malformed or not source-backed."""


def _dump(value: Any) -> str:
    return canonical_json(value)


def _digest(value: Any) -> str:
    return canonical_digest(value)


def _text_sha256(value: str) -> str:
    return text_sha256(value)


def _reference_uri(conversation_id: str, message_id: str) -> str:
    return f"ayunos://conversation/{conversation_id}?message={message_id}"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    target = path.expanduser().resolve()
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _profile_item_is_includable(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    approval = item.get("approval")
    return bool(
        _PROFILE_ID_RE.fullmatch(str(item.get("profile_item_id", "")))
        and item.get("model_visible") is True
        and isinstance(approval, dict)
        and approval.get("status") == "approved"
        and approval.get("authority") == "user"
        and isinstance(item.get("statement"), str)
        and item["statement"].strip()
        and isinstance(item.get("topics"), list)
    )


def _project_profiles(projection: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(projection, dict):
        raise ProductionDeliveryValidationError("approved profile projection is invalid")
    if not _PROJECTION_ID_RE.fullmatch(str(projection.get("projection_identity", ""))):
        raise ProductionDeliveryValidationError("approved profile projection identity is invalid")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for index, source in enumerate(projection.get("items", ())):
        if not _profile_item_is_includable(source):
            excluded.append({
                "item_id": str(source.get("profile_item_id", f"malformed-{index}")) if isinstance(source, dict) else f"malformed-{index}",
                "reason": "not_active_approved_model_visible_or_malformed",
            })
            continue
        included.append(deepcopy(source))
    included.sort(key=lambda item: item["profile_item_id"])
    if len({item["profile_item_id"] for item in included}) != len(included):
        raise ProductionDeliveryValidationError("approved profile projection contains duplicate identities")
    return included, excluded


def _query_terms(text: str) -> list[str]:
    candidates: list[str] = []
    for token in _LATIN_RE.findall(text):
        clean = token.strip("._+-")
        if len(clean) < 3 or clean.casefold() in _CONTROL_TERMS:
            continue
        candidates.append(clean)
    for token in _HAN_RE.findall(text):
        if len(token) >= 2:
            candidates.append(token)
    return list(dict.fromkeys(candidates))


def _fts_literal(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _theme_tags(text: str) -> list[str]:
    folded = text.casefold()
    return [name for name, cues in _THEME_CUES if any(cue.casefold() in folded for cue in cues)]


def _theme_strength(text: str, theme: str) -> int:
    folded = text.casefold()
    cues = next(cues for name, cues in _THEME_CUES if name == theme)
    strength = sum(folded.count(cue.casefold()) for cue in cues)
    if theme == "target_configuration" and ("目标配置" in folded or ("target" in folded and "configuration" in folded)):
        strength += 6
    if theme == "price_threshold" and re.search(r"(?:低于|以下|以内|below).{0,16}\d", folded):
        strength += 6
    return strength


def _normalized_text(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.casefold())


def _excerpt(text: str, limit: int = 190) -> str:
    clean = redact_context_text(" ".join(str(text or "").split()))
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _seed_terms(conn: sqlite3.Connection, query_text: str) -> tuple[list[str], dict[str, int]]:
    terms = _query_terms(query_text)
    if not terms:
        raise ProductionDeliveryValidationError("lexical fallback has no safe query term")
    counts: dict[str, int] = {}
    for term in terms[:24]:
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                (_fts_literal(term),),
            ).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ProductionDeliveryValidationError("messages FTS is unavailable") from exc
        if count:
            counts[term] = int(count)
    if not counts:
        raise ProductionDeliveryValidationError("lexical fallback found no query-bound seed")
    entity = [term for term in counts if any(char.isupper() for char in term[1:]) or any(char.isdigit() for char in term)]
    pool = entity or list(counts)
    pool.sort(key=lambda term: (counts[term], -len(term), term.casefold()))
    return pool[:1], counts


def _fetch_lexical_candidates(
    conn: sqlite3.Connection,
    query: RetrievalQuery,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized = query.normalized()
    seeds, term_counts = _seed_terms(conn, normalized.query)
    match = " OR ".join(_fts_literal(term) for term in seeds)
    where = ["messages_fts MATCH ?"]
    values: list[Any] = [match]
    if normalized.start_epoch is not None:
        where.append("m.create_time >= ?")
        values.append(normalized.start_epoch)
    if normalized.end_epoch is not None:
        where.append("m.create_time <= ?")
        values.append(normalized.end_epoch)
    if normalized.memory_space is not None:
        where.append("m.memory_space = ?")
        values.append(normalized.memory_space)
    predicate = " AND ".join(where)
    fts_count = int(conn.execute(
        "SELECT COUNT(*) FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid WHERE " + predicate,
        values,
    ).fetchone()[0])
    rows = conn.execute(
        """
        SELECT m.rowid, m.conversation_id, m.message_id, m.conversation_title,
               m.ordinal, m.role, m.create_time, m.create_time_iso, m.text,
               bm25(messages_fts) AS fts_rank
        FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
        WHERE """ + predicate + " ORDER BY bm25(messages_fts), m.create_time DESC LIMIT ?",
        (*values, MAX_LEXICAL_POOL),
    ).fetchall()
    direct_by_rowid = {row["rowid"]: dict(row) | {"direct_seed": True} for row in rows}
    literal_where = ["lower(coalesce(m.text,'')) LIKE lower(?)"]
    literal_values: list[Any] = ["%" + seeds[0] + "%"]
    if normalized.start_epoch is not None:
        literal_where.append("m.create_time >= ?")
        literal_values.append(normalized.start_epoch)
    if normalized.end_epoch is not None:
        literal_where.append("m.create_time <= ?")
        literal_values.append(normalized.end_epoch)
    if normalized.memory_space is not None:
        literal_where.append("m.memory_space = ?")
        literal_values.append(normalized.memory_space)
    literal_predicate = " AND ".join(literal_where)
    literal_count = int(conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE " + literal_predicate,
        literal_values,
    ).fetchone()[0])
    literal_user_count = int(conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE " + literal_predicate + " AND m.role = 'user'",
        literal_values,
    ).fetchone()[0])
    literal_rows = conn.execute(
        """
        SELECT m.rowid, m.conversation_id, m.message_id, m.conversation_title,
               m.ordinal, m.role, m.create_time, m.create_time_iso, m.text,
               NULL AS fts_rank
        FROM messages m WHERE """ + literal_predicate + " ORDER BY m.create_time DESC LIMIT ?",
        (*literal_values, MAX_LEXICAL_POOL),
    ).fetchall()
    for row in literal_rows:
        direct_by_rowid.setdefault(row["rowid"], dict(row) | {"direct_seed": True})
    direct = list(direct_by_rowid.values())
    user_direct = [row for row in direct if row["role"] == "user"]

    # Context expansion is query-bound: only neighboring direct-user messages
    # in the same conversation, and only when they carry decision/theme cues.
    candidates: dict[int, dict[str, Any]] = {row["rowid"]: row for row in user_direct}
    for seed in user_direct:
        neighbors = conn.execute(
            """
            SELECT rowid, conversation_id, message_id, conversation_title, ordinal,
                   role, create_time, create_time_iso, text, NULL AS fts_rank
            FROM messages
            WHERE conversation_id = ? AND role = 'user' AND ordinal BETWEEN ? AND ?
            ORDER BY ordinal
            """,
            (seed["conversation_id"], max(0, int(seed["ordinal"] or 0) - 10), int(seed["ordinal"] or 0) + 10),
        ).fetchall()
        for raw in neighbors:
            item = dict(raw) | {"direct_seed": False}
            if _theme_tags(str(item.get("text") or "")):
                candidates.setdefault(item["rowid"], item)
    return list(candidates.values()), {
        "seed_terms": seeds,
        "term_document_counts": {term: term_counts[term] for term in sorted(term_counts)},
        "raw_match_count": literal_count,
        "fts_match_count": fts_count,
        "direct_user_match_count": literal_user_count,
        "fts_direct_user_seed_count": len(user_direct),
        "candidate_pool_count": len(candidates),
        "fts_query": match,
    }


def _candidate_score(item: dict[str, Any], seeds: list[str]) -> tuple[float, list[str]]:
    text = str(item.get("text") or "")
    folded = text.casefold()
    themes = _theme_tags(text)
    score = 100.0 if item.get("role") == "user" else 0.0
    score += 30.0 if item.get("direct_seed") else 8.0
    score += 24.0 * sum(term.casefold() in folded for term in seeds)
    score += 12.0 * len(themes)
    score += min(len(text), 600) / 100.0
    if _PRICE_RE.search(text):
        score += 8.0
    return score, themes


def _select_evidence(
    candidates: list[dict[str, Any]], seeds: list[str], limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    scored: list[dict[str, Any]] = []
    for item in candidates:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        signature = _normalized_text(text)
        if len(signature) < 12 or signature in deduped:
            continue
        score, themes = _candidate_score(item, seeds)
        projected = deepcopy(item)
        projected["score"] = score
        projected["themes"] = themes
        deduped[signature] = projected
        scored.append(projected)
    scored.sort(key=lambda item: (-item["score"], -float(item.get("create_time") or 0), str(item["message_id"])))

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for theme, _cues in _THEME_CUES:
        matches = [item for item in scored if theme in item["themes"] and item["message_id"] not in selected_ids]
        match = max(
            matches,
            key=lambda item: (_theme_strength(str(item.get("text") or ""), theme), item["score"], float(item.get("create_time") or 0)),
            default=None,
        )
        if match is not None and len(selected) < limit:
            match.setdefault("selection_reasons", []).append(theme)
            selected.append(match)
            selected_ids.add(match["message_id"])
    for item in scored:
        if len(selected) >= limit:
            break
        if item["message_id"] not in selected_ids:
            item.setdefault("selection_reasons", []).append("score_fill")
            selected.append(item)
            selected_ids.add(item["message_id"])
    selected.sort(key=lambda item: (float(item.get("create_time") or 0), str(item["message_id"])))

    evidence: list[dict[str, Any]] = []
    for item in selected:
        anchor = {
            "conversation_id": str(item["conversation_id"]),
            "message_id": str(item["message_id"]),
            "role": str(item["role"]),
            "time": str(item.get("create_time_iso") or ""),
            "conversation_title": redact_context_text(str(item.get("conversation_title") or "")),
            "reference_uri": _reference_uri(str(item["conversation_id"]), str(item["message_id"])),
        }
        excerpt = _excerpt(str(item["text"]))
        evidence_item = {
            "evidence_reference_id": "",
            "source_anchor": anchor,
            "excerpt": excerpt,
            "retrieval": {
                "method": "sqlite_fts5_lexical_fallback",
                "version": LEXICAL_FALLBACK_VERSION,
                "direct_user_message": True,
                "direct_seed": bool(item.get("direct_seed")),
                "score": round(float(item["score"]), 6),
                "themes": list(item["themes"]),
                "selection_reasons": list(item.get("selection_reasons", ())),
            },
            "freshness": {
                "time_sensitive": bool(_PRICE_RE.search(str(item["text"]))),
                "status": "potentially_stale" if _PRICE_RE.search(str(item["text"])) else "historical_statement",
                "reason_code": "volatile_price_or_promotion" if _PRICE_RE.search(str(item["text"])) else "not_time_sensitive_market_data",
            },
            "temporal": {
                "position": "historical",
                "potentially_conflicting": False,
                "related_evidence_ids": [],
            },
            "provenance": {
                "source_ids": [f"{anchor['conversation_id']}:{anchor['message_id']}"],
                "evidence_ids": [],
                "event_ids": [],
            },
        }
        evidence_item["evidence_reference_id"] = production_evidence_identity(evidence_item)
        evidence.append(evidence_item)

    # Mark possible evolution/conflict conservatively; never resolve it into a
    # durable preference.  Related items must share a theme and differ in time.
    for index, item in enumerate(evidence):
        related = [
            other["evidence_reference_id"] for other in evidence
            if other is not item
            and set(other["retrieval"]["themes"]) & set(item["retrieval"]["themes"])
            and other["source_anchor"]["time"] != item["source_anchor"]["time"]
        ]
        if related and "uncertain_or_changed" in item["retrieval"]["themes"]:
            item["temporal"] = {
                "position": "changed_or_conditional_historical_view",
                "potentially_conflicting": True,
                "related_evidence_ids": sorted(related),
            }
    return evidence, {
        "deduplicated_count": len(scored),
        "selected_count": len(evidence),
        "compression_applied": len(scored) > len(evidence),
    }


def _lexical_fallback(
    query: RetrievalQuery, *, source_db: Path, limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with _connect_readonly(source_db) as conn:
        candidates, retrieval = _fetch_lexical_candidates(conn, query)
    evidence, selection = _select_evidence(candidates, retrieval["seed_terms"], limit)
    manifest = {
        "version": LEXICAL_FALLBACK_VERSION,
        "status": "active" if evidence else "empty",
        "reason_code": "semantic_or_graph_unavailable",
        "source": "fixed_production_main_db_readonly_fts5",
        "role_priority": "direct_user_first",
        "query_binding": _text_sha256(query.normalized().query),
        **retrieval,
        **selection,
    }
    return evidence, manifest


def _profile_binding(projection: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "authority": "v8.0-d1-approved-profile-projection",
        "projection_protocol_version": projection["protocol_version"],
        "projection_identity": projection["projection_identity"],
        "revision": projection["revision"],
        "decision_count": projection["decision_count"],
        "model_visible": bool(items),
        "profile_item_ids": [item["profile_item_id"] for item in items],
    }


def _review_item(
    *, layer: str, item_type: str, item_id: str, trust_status: str,
    content: str, source_anchor: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None, excludable: bool = False,
    included: bool = True, omitted_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "layer": layer,
        "item_type": item_type,
        "item_id": item_id,
        "trust_status": trust_status,
        "included": included,
        "excludable": excludable,
        "content": content,
        "characters": len(content),
        "estimated_tokens": estimate_tokens(content),
        "source_anchor": deepcopy(source_anchor),
        "provenance": deepcopy(provenance or {"source_ids": [], "evidence_ids": [], "event_ids": []}),
        "exclusion": {"state": "none", "caused_by_item_ids": []},
        "omitted_reasons": sorted(set(omitted_reasons)),
    }


def _render_model_context(
    question: str, profiles: list[dict[str, Any]], evidence: list[dict[str, Any]],
    uncertainties: list[dict[str, str]], capability: dict[str, Any],
) -> str:
    lines = [
        f"YunOS Production Model Context {ASSEMBLY_PROTOCOL_VERSION}",
        "[A CURRENT TASK]",
        f"user_question: {question}",
        "[B APPROVED PROFILE - ALL ACTIVE + MODEL_VISIBLE; NO RELEVANCE PRUNING]",
    ]
    lines.extend(
        f"- [approved/{item['kind']}/{item['profile_item_id']}] {item['statement']}"
        for item in profiles
    )
    if not profiles:
        lines.append("- none")
    lines.extend((
        "[C HISTORICAL EVIDENCE - SOURCE BACKED; HISTORICAL, NOT CURRENT FACT]",
        f"retrieval_degradation: {capability['degradation']['status']}; lexical_fallback={capability['lexical_fallback']['status']}",
    ))
    for item in evidence:
        anchor = item["source_anchor"]
        freshness = item["freshness"]
        temporal = item["temporal"]
        lines.append(
            f"- [{item['evidence_reference_id']}] {anchor['conversation_id']}/{anchor['message_id']} "
            f"{anchor['role']} {anchor['time']} | {anchor['reference_uri']} | "
            f"freshness={freshness['status']} | temporal={temporal['position']}"
        )
        lines.append(f"  excerpt: {item['excerpt']}")
    if not evidence:
        lines.append("- none")
    lines.append("[D OMITTED / UNCERTAIN INFORMATION]")
    lines.extend(f"- [{item['reason_code']}] {item['statement']}" for item in uncertainties)
    return "\n".join(lines) + "\n"


def _uncertainties(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = [{
        "reason_code": "historical_evidence_not_durable_profile",
        "statement": "Historical messages support only the cited past statements; they are not automatically promoted into approved profile facts.",
    }, {
        "reason_code": "unsupported_product_conclusion",
        "statement": "Product performance, the best product choice, and the user's final purchase decision remain uncertain unless directly established by approved profile or current external evidence.",
    }]
    if any(item["freshness"]["time_sensitive"] for item in evidence):
        result.append({
            "reason_code": "volatile_price_or_promotion",
            "statement": "Historical prices, discounts, subsidies, and promotions may be stale and must not be treated as current offers.",
        })
    if any(item["temporal"]["potentially_conflicting"] for item in evidence):
        result.append({
            "reason_code": "position_changed_or_conflicted",
            "statement": "The cited history contains conditional or changing viewpoints; no single timeless preference is asserted.",
        })
    return result


class _ProductionDeliveryCore:
    """Injectable test seam; production uses fixed dependencies below."""

    def __init__(
        self, *, source_db: Path, projection_loader: Callable[[], dict[str, Any]],
        sealed_builder: Callable[..., dict[str, Any]],
    ) -> None:
        self.source_db = source_db.expanduser().resolve()
        self.projection_loader = projection_loader
        self.sealed_builder = sealed_builder

    def build(
        self, query: RetrievalQuery, *, budget_profile: str = "Medium",
        total_characters: int | None = None, estimated_tokens: int | None = None,
        max_evidence: int | None = None, max_candidates: int | None = None,
        max_history_expansions: int | None = None,
        excluded_item_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        normalized = query.normalized()
        excluded = sorted(set(str(value).strip() for value in excluded_item_ids if str(value).strip()))
        projection = self.projection_loader()
        profiles, profile_excluded = _project_profiles(projection)
        profile_ids = {item["profile_item_id"] for item in profiles}
        if profile_ids & set(excluded):
            raise ProductionDeliveryValidationError("v8.0-E active approved profiles cannot be excluded")

        sealed = self.sealed_builder(
            query, budget_profile=budget_profile,
            total_characters=total_characters, estimated_tokens=estimated_tokens,
            max_evidence=max_evidence, max_candidates=max_candidates,
            max_history_expansions=max_history_expansions,
            excluded_item_ids=(),
        )
        validate_context_delivery_standalone(sealed)
        missing = set(sealed["capability_state"]["missing_capabilities"])
        fallback_required = bool({"semantic", "graph"} & missing)
        fallback_limit = max_evidence or DEFAULT_FALLBACK_EVIDENCE
        fallback_limit = max(1, min(int(fallback_limit), 24))
        lexical, lexical_manifest = (
            _lexical_fallback(query, source_db=self.source_db, limit=fallback_limit)
            if fallback_required else ([], {
                "version": LEXICAL_FALLBACK_VERSION, "status": "not_needed",
                "reason_code": "semantic_and_graph_available", "source": "none",
                "role_priority": "direct_user_first", "query_binding": _text_sha256(normalized.query),
                "seed_terms": [], "term_document_counts": {}, "raw_match_count": 0,
                "fts_match_count": 0,
                "direct_user_match_count": 0, "fts_direct_user_seed_count": 0,
                "candidate_pool_count": 0,
                "fts_query": "", "deduplicated_count": 0, "selected_count": 0,
                "compression_applied": False,
            })
        )
        evidence = [item for item in lexical if item["evidence_reference_id"] not in set(excluded)]
        unknown_exclusions = set(excluded) - {item["evidence_reference_id"] for item in lexical}
        if unknown_exclusions:
            raise ProductionDeliveryValidationError("excluded item is not present in production Delivery")
        uncertainties = _uncertainties(evidence)

        capability = deepcopy(sealed["capability_state"])
        capability["lexical_fallback"] = {
            "status": lexical_manifest["status"],
            "reason_code": lexical_manifest["reason_code"],
            "version": LEXICAL_FALLBACK_VERSION,
        }
        if fallback_required and evidence:
            capability["status"] = "partial"
            capability["reason_code"] = "source_backed_lexical_fallback"
            capability["degradation"] = deepcopy(capability["degradation"])
            capability["degradation"]["status"] = "degraded"
            warnings = list(capability["warnings"])
            warnings.append({
                "code": "lexical_fallback_active",
                "message": "Semantic or graph retrieval is unavailable; fixed readonly main-DB FTS evidence is shown.",
            })
            capability["warnings"] = sorted(warnings, key=lambda item: (item["code"], item["message"]))

        model_context = _render_model_context(normalized.query, profiles, evidence, uncertainties, capability)
        char_limit = int(sealed["budget"]["total_characters_limit"])
        token_limit = int(sealed["budget"]["estimated_token_limit"])
        while evidence and (len(model_context) > char_limit or estimate_tokens(model_context) > token_limit):
            theme_counts = {
                theme: sum(theme in item["retrieval"]["themes"] for item in evidence)
                for theme, _cues in _THEME_CUES
            }
            removable = [
                index for index, item in enumerate(evidence)
                if "score_fill" in item["retrieval"]["selection_reasons"]
                or (
                    all(theme_counts[theme] > 1 for theme in item["retrieval"]["themes"])
                    and not any(
                        reason in {"display_condition", "target_configuration", "price_threshold"}
                        for reason in item["retrieval"]["selection_reasons"]
                    )
                )
            ]
            pool = removable or list(range(len(evidence)))
            remove_index = min(
                pool,
                key=lambda index: (
                    float(evidence[index]["retrieval"]["score"]),
                    evidence[index]["source_anchor"]["time"],
                ),
            )
            evidence.pop(remove_index)
            lexical_manifest["selected_count"] = len(evidence)
            lexical_manifest["compression_applied"] = True
            uncertainties = _uncertainties(evidence)
            model_context = _render_model_context(normalized.query, profiles, evidence, uncertainties, capability)
        if len(model_context) > char_limit or estimate_tokens(model_context) > token_limit:
            raise ProductionDeliveryValidationError("approved profile and transparency context exceed the selected hard budget")

        binding = _profile_binding(projection, profiles)
        assembly = {
            "protocol_version": ASSEMBLY_PROTOCOL_VERSION,
            "assembly_identity_version": ASSEMBLY_IDENTITY_VERSION,
            "assembly_identity": "",
            "sealed_v7_4_binding": {
                "protocol_version": sealed["protocol_version"],
                "delivery_identity": sealed["delivery_identity"],
                "assembly_identity": sealed["assembly_binding"]["assembly_identity"],
            },
            "profile_policy": {
                "version": PROFILE_POLICY_VERSION,
                "rule": "include_all_active_and_model_visible_without_relevance_pruning",
                "source_item_count": len(projection.get("items", ())),
                "included_item_ids": binding["profile_item_ids"],
                "excluded": profile_excluded,
            },
            "approved_profile_binding": binding,
            "lexical_fallback": deepcopy(lexical_manifest),
            "historical_evidence_bindings": [{
                "evidence_reference_id": item["evidence_reference_id"],
                "conversation_id": item["source_anchor"]["conversation_id"],
                "message_id": item["source_anchor"]["message_id"],
                "excerpt_sha256": _text_sha256(item["excerpt"]),
            } for item in evidence],
            "execution": {
                "readonly": True, "persisted": False, "sent_to_model": False,
                "external_model_calls": False, "credential_read": False,
            },
        }
        assembly["assembly_identity"] = production_assembly_identity(assembly)

        review_items = [_review_item(
            layer="current_task", item_type="current_task", item_id="current_task",
            trust_status="current_request", content=normalized.query,
        )]
        review_items.extend(_review_item(
            layer="approved_profile", item_type="approved_profile",
            item_id=item["profile_item_id"], trust_status="active_approved_model_visible",
            content=item["statement"],
        ) for item in profiles)
        review_items.extend(_review_item(
            layer="historical_evidence", item_type="lexical_evidence",
            item_id=item["evidence_reference_id"], trust_status="source_backed_historical",
            content=item["excerpt"], source_anchor=item["source_anchor"],
            provenance=item["provenance"], excludable=True,
        ) for item in evidence)
        omissions = [{
            "layer": "historical_evidence", "item_type": "lexical_evidence",
            "item_id": item_id, "reason": "user_excluded_current_delivery",
            "priority": 4, "origin": "v8.0-e-production-delivery",
        } for item_id in excluded]
        delivery = {
            "protocol_version": PROTOCOL_VERSION,
            "delivery_identity_version": DELIVERY_IDENTITY_VERSION,
            "delivery_identity": "",
            "assembly_binding": {
                "protocol_version": ASSEMBLY_PROTOCOL_VERSION,
                "assembly_identity_version": ASSEMBLY_IDENTITY_VERSION,
                "assembly_identity": assembly["assembly_identity"],
                "selection_policy_identity": PROFILE_POLICY_VERSION + "+" + LEXICAL_FALLBACK_VERSION,
            },
            "profile_binding": binding,
            "controls": {
                "query": normalized.query,
                "time_range": {
                    "start_time": normalized.start.isoformat() if normalized.start else None,
                    "end_time": normalized.end.isoformat() if normalized.end else None,
                },
                "budget_profile": sealed["controls"]["budget_profile"],
                "budget_limits": deepcopy(sealed["controls"]["budget_limits"]),
                "profile_inclusion_policy": PROFILE_POLICY_VERSION,
                "lexical_fallback_policy": LEXICAL_FALLBACK_VERSION,
                "excluded_item_ids": excluded,
            },
            "status": "ok" if evidence else "partial",
            "reason_code": "production_delivery_ready" if evidence else "historical_evidence_unavailable",
            "review_state": "customized" if excluded else "unmodified",
            "execution": {
                "readonly": True, "persisted": False, "sent_to_model": False,
                "external_model_calls": False, "credential_read": False,
                "temporary_adjustments_only": True,
            },
            "capability_state": capability,
            "layers": {
                "current_task": {"status": "included", "excludable": False, "content": {
                    "current_user_question": normalized.query,
                    "time_range": deepcopy(sealed["controls"]["time_range"]),
                }},
                "approved_profile": {
                    "status": "included" if profiles else "empty",
                    "empty_message": "No active approved model-visible profile items are available." if not profiles else "",
                    "authority": binding["authority"],
                    "candidate_promotion_allowed": False,
                    "items": profiles,
                },
                "candidate_memory": {
                    "candidate_only": True, "may_be_used_as_auxiliary_reference": True,
                    "items": [], "relations": [],
                },
                "historical_evidence": {
                    "default_representation": "source_anchor_plus_excerpt",
                    "items": evidence,
                    "expanded_count": len(evidence),
                    "retrieval_manifest": lexical_manifest,
                },
            },
            "uncertainties": uncertainties,
            "omitted": omissions,
            "review_items": review_items,
            "counts": {
                "approved_profile_available": len(projection.get("items", ())),
                "approved_profile_included": len(profiles),
                "approved_profile_excluded": len(profile_excluded),
                "candidates_included": 0,
                "candidate_relations_included": 0,
                "historical_evidence_included": len(evidence),
                "lexical_raw_matches": lexical_manifest["raw_match_count"],
                "lexical_fts_matches": lexical_manifest["fts_match_count"],
                "lexical_direct_user_matches": lexical_manifest["direct_user_match_count"],
                "lexical_deduplicated": lexical_manifest["deduplicated_count"],
                "temporary_exclusion_requested": len(excluded),
                "temporary_excluded": len(excluded),
                "provenance_cascade_excluded": 0,
                "relations_cascade_excluded": 0,
                "omitted_manifest_entries": len(omissions),
                "uncertainties": len(uncertainties),
                "review_items": len(review_items),
            },
            "budget": {
                "total_characters_limit": char_limit,
                "used_characters": len(model_context),
                "remaining_characters": char_limit - len(model_context),
                "character_hard_limit_satisfied": len(model_context) <= char_limit,
                "estimated_token_limit": token_limit,
                "estimated_tokens_used": estimate_tokens(model_context),
                "estimated_tokens_remaining": token_limit - estimate_tokens(model_context),
                "estimator_profile": sealed["budget"]["estimator_profile"],
                "exact_model_tokenizer": False,
            },
            "model_payload_provenance": {
                "source_ids": sorted({value for item in evidence for value in item["provenance"]["source_ids"]}),
                "evidence_ids": [item["evidence_reference_id"] for item in evidence],
                "event_ids": [],
            },
            "model_context": model_context,
            "source_assembly": assembly,
        }
        delivery["delivery_identity"] = production_delivery_identity(delivery)
        validate_production_delivery_standalone(delivery)
        return delivery


class ProductionDeliveryService:
    """Fixed production facade; no DB/profile/provider injection."""

    __slots__ = ()

    def build(self, query: RetrievalQuery, **kwargs: Any) -> dict[str, Any]:
        core = _ProductionDeliveryCore(
            source_db=SOURCE_DB,
            projection_loader=active_profile_enablement_projection,
            sealed_builder=build_sealed_v74_delivery,
        )
        delivery = core.build(query, **kwargs)
        validate_production_delivery_source_backed(delivery)
        return delivery


def build_production_delivery(query: RetrievalQuery, **kwargs: Any) -> dict[str, Any]:
    return ProductionDeliveryService().build(query, **kwargs)


def production_profile_binding(delivery: dict[str, Any]) -> dict[str, Any]:
    validate_production_delivery_standalone(delivery)
    binding = deepcopy(delivery["profile_binding"])
    actual = [item["profile_item_id"] for item in delivery["layers"]["approved_profile"]["items"]]
    if binding["profile_item_ids"] != actual:
        raise ProductionDeliveryValidationError("profile binding does not match Delivery contents")
    return binding


def validate_production_delivery_standalone(delivery: Any) -> None:
    if not isinstance(delivery, dict):
        raise ProductionDeliveryValidationError("production Delivery must be an object")
    required = {
        "protocol_version", "delivery_identity_version", "delivery_identity",
        "assembly_binding", "profile_binding", "controls", "status", "reason_code",
        "review_state", "execution", "capability_state", "layers", "uncertainties",
        "omitted", "review_items", "counts", "budget", "model_payload_provenance",
        "model_context", "source_assembly",
    }
    if set(delivery) != required:
        raise ProductionDeliveryValidationError("production Delivery fields mismatch")
    if delivery["protocol_version"] != PROTOCOL_VERSION or delivery["delivery_identity_version"] != DELIVERY_IDENTITY_VERSION:
        raise ProductionDeliveryValidationError("production Delivery version mismatch")
    expected_identity = production_delivery_identity(delivery)
    if delivery["delivery_identity"] != expected_identity:
        raise ProductionDeliveryValidationError("production Delivery identity mismatch")
    execution = delivery["execution"]
    if execution != {
        "readonly": True, "persisted": False, "sent_to_model": False,
        "external_model_calls": False, "credential_read": False,
        "temporary_adjustments_only": True,
    }:
        raise ProductionDeliveryValidationError("production Delivery execution boundary mismatch")
    assembly = delivery["source_assembly"]
    if not isinstance(assembly, dict) or assembly.get("protocol_version") != ASSEMBLY_PROTOCOL_VERSION:
        raise ProductionDeliveryValidationError("production assembly version mismatch")
    expected_assembly = production_assembly_identity(assembly)
    if assembly.get("assembly_identity") != expected_assembly:
        raise ProductionDeliveryValidationError("production assembly identity mismatch")
    if delivery["assembly_binding"].get("assembly_identity") != assembly["assembly_identity"]:
        raise ProductionDeliveryValidationError("production assembly binding mismatch")
    profiles = delivery["layers"]["approved_profile"]["items"]
    if any(not _profile_item_is_includable(item) for item in profiles):
        raise ProductionDeliveryValidationError("non-approved or malformed profile reached Delivery")
    actual_profile_ids = [item["profile_item_id"] for item in profiles]
    if delivery["profile_binding"].get("profile_item_ids") != actual_profile_ids:
        raise ProductionDeliveryValidationError("profile binding does not match actual Delivery")
    if (
        assembly.get("approved_profile_binding") != delivery["profile_binding"]
        or assembly.get("profile_policy", {}).get("included_item_ids") != actual_profile_ids
    ):
        raise ProductionDeliveryValidationError("assembly profile binding does not match actual Delivery")
    evidence = delivery["layers"]["historical_evidence"]["items"]
    if any(
        not item.get("source_anchor", {}).get("conversation_id")
        or not item.get("source_anchor", {}).get("message_id")
        or item["source_anchor"].get("role") != "user"
        or item["source_anchor"].get("reference_uri") != _reference_uri(
            item["source_anchor"]["conversation_id"], item["source_anchor"]["message_id"]
        )
        for item in evidence
    ):
        raise ProductionDeliveryValidationError("historical evidence source anchor is invalid")
    for item in evidence:
        expected_evidence_identity = production_evidence_identity(item)
        if (
            item.get("evidence_reference_id") != expected_evidence_identity
            or item.get("retrieval", {}).get("version") != LEXICAL_FALLBACK_VERSION
        ):
            raise ProductionDeliveryValidationError("historical evidence identity mismatch")
    evidence_ids = [item["evidence_reference_id"] for item in evidence]
    if delivery["model_payload_provenance"].get("evidence_ids") != evidence_ids:
        raise ProductionDeliveryValidationError("evidence binding does not match actual Delivery")
    expected_evidence_bindings = [{
        "evidence_reference_id": item["evidence_reference_id"],
        "conversation_id": item["source_anchor"]["conversation_id"],
        "message_id": item["source_anchor"]["message_id"],
        "excerpt_sha256": _text_sha256(item["excerpt"]),
    } for item in evidence]
    if assembly.get("historical_evidence_bindings") != expected_evidence_bindings:
        raise ProductionDeliveryValidationError("assembly evidence binding does not match actual Delivery")
    counts = delivery["counts"]
    if (
        counts.get("approved_profile_included") != len(profiles)
        or counts.get("historical_evidence_included") != len(evidence)
    ):
        raise ProductionDeliveryValidationError("production Delivery item counts mismatch")
    budget = delivery["budget"]
    if (
        budget["used_characters"] != len(delivery["model_context"])
        or budget["estimated_tokens_used"] != estimate_tokens(delivery["model_context"])
        or not budget["character_hard_limit_satisfied"]
    ):
        raise ProductionDeliveryValidationError("model context budget mismatch")


def validate_production_delivery_source_backed(delivery: dict[str, Any]) -> None:
    validate_production_delivery_standalone(delivery)
    projection = active_profile_enablement_projection()
    expected_profiles, _excluded = _project_profiles(projection)
    if delivery["profile_binding"] != _profile_binding(projection, expected_profiles):
        raise ProductionDeliveryValidationError("Delivery profile authority is stale or mismatched")
    with _connect_readonly(SOURCE_DB) as conn:
        for item in delivery["layers"]["historical_evidence"]["items"]:
            anchor = item["source_anchor"]
            rows = conn.execute(
                """
                SELECT conversation_id, message_id, conversation_title, role,
                       create_time_iso, text
                FROM messages WHERE conversation_id = ? AND message_id = ? LIMIT 2
                """,
                (anchor["conversation_id"], anchor["message_id"]),
            ).fetchall()
            if len(rows) != 1:
                raise ProductionDeliveryValidationError("historical evidence anchor is not unique")
            row = rows[0]
            if (
                row["role"] != "user"
                or _excerpt(row["text"]) != item["excerpt"]
                or str(row["create_time_iso"] or "") != anchor["time"]
                or redact_context_text(str(row["conversation_title"] or "")) != anchor["conversation_title"]
            ):
                raise ProductionDeliveryValidationError("historical evidence does not match source")


def validate_trusted_delivery_standalone(delivery: dict[str, Any]) -> None:
    if isinstance(delivery, dict) and delivery.get("protocol_version") == PROTOCOL_VERSION:
        validate_production_delivery_standalone(delivery)
    else:
        validate_context_delivery_standalone(delivery)


def canonical_production_delivery_json(delivery: dict[str, Any]) -> str:
    validate_production_delivery_standalone(delivery)
    return _dump(delivery)


def render_production_delivery(delivery: dict[str, Any]) -> str:
    validate_production_delivery_standalone(delivery)
    return "\n".join((
        f"YunOS Production Delivery {delivery['protocol_version']}",
        f"delivery_identity: {delivery['delivery_identity']}",
        f"assembly_identity: {delivery['assembly_binding']['assembly_identity']}",
        f"approved_profile: {delivery['counts']['approved_profile_included']}/{delivery['counts']['approved_profile_available']}",
        f"historical_evidence: {delivery['counts']['historical_evidence_included']}",
        f"lexical_raw_matches: {delivery['counts']['lexical_raw_matches']}",
        "persisted: false",
        "sent_to_model: false",
        "external_model_calls: false",
        "credential_read: false",
        "MODEL_CONTEXT_BEGIN",
        delivery["model_context"].rstrip(),
        "MODEL_CONTEXT_END",
        "",
    ))
