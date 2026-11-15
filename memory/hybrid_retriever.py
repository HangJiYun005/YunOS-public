from __future__ import annotations

from pathlib import Path

from memory.read_only_retriever import retrieve
from memory.semantic_memory import ensure_test_database_path, semantic_summary_search_preview


RRF_K = 60


def hybrid_search_preview(
    query: str,
    source_db: str | Path,
    semantic_db: str | Path,
    memory_space: str = "whale",
    keyword_top_k: int = 5,
    semantic_top_k: int = 5,
    combined_top_k: int = 8,
    provider_name: str = "local_sentence_transformer",
) -> dict:
    if provider_name != "local_sentence_transformer":
        raise ValueError("Hybrid preview requires provider_name=local_sentence_transformer")
    semantic_path = ensure_test_database_path(semantic_db)
    clean_space = (memory_space or "").strip()
    if not clean_space or clean_space in {"*", "all"}:
        raise ValueError("memory_space must name one explicit memory space")

    keyword_rows = retrieve(
        query,
        top_k=keyword_top_k,
        memory_space=clean_space,
        db_path=source_db,
    )
    base = {
        "query": query,
        "memory_space": clean_space,
        "provider": provider_name,
        "keyword_results": [_keyword_result(row, rank) for rank, row in enumerate(keyword_rows, start=1)],
        "semantic_results": [],
        "combined_results": [],
        "keyword_count": len(keyword_rows),
        "semantic_count": 0,
        "combined_count": 0,
    }
    semantic_payload = semantic_summary_search_preview(
        query,
        semantic_path,
        memory_space=clean_space,
        top_k=semantic_top_k,
        provider_name=provider_name,
    )

    if "readonly_error" in semantic_payload:
        return {**base, "semantic_error": semantic_payload["readonly_error"]}

    semantic_results = [_semantic_result(row) for row in semantic_payload["results"]]
    keyword_results = base["keyword_results"]
    combined = _combine_rrf(keyword_results, semantic_results, combined_top_k)
    return {
        **base,
        "semantic_results": semantic_results,
        "combined_results": combined,
        "semantic_count": len(semantic_results),
        "combined_count": len(combined),
    }


def _keyword_result(row: dict, rank: int) -> dict:
    return {
        "source_kind": "keyword_message",
        "source_id": row.get("message_id") or row.get("conversation_id") or "",
        "memory_space": row.get("memory_space") or "",
        "rank": rank,
        "original_score": None,
        "content_preview": row.get("snippet") or "",
        "source_platform": row.get("source_platform") or "",
        "summary_type": None,
        "conversation_id": row.get("conversation_id") or "",
    }


def _semantic_result(row: dict) -> dict:
    return {
        "source_kind": "semantic_summary",
        "source_id": row["source_id"],
        "memory_space": row["memory_space"],
        "rank": row["rank"],
        "original_score": row["score"],
        "content_preview": row.get("content_preview") or "",
        "source_platform": None,
        "summary_type": row.get("summary_type"),
        "conversation_id": row.get("conversation_id"),
    }


def _combine_rrf(keyword_results: list[dict], semantic_results: list[dict], combined_top_k: int) -> list[dict]:
    merged = []
    for item in [*keyword_results, *semantic_results]:
        combined = dict(item)
        combined["rrf_score"] = 1.0 / (RRF_K + int(item["rank"]))
        merged.append(combined)
    merged.sort(key=lambda item: (-item["rrf_score"], item["source_kind"], item["source_id"]))
    for rank, item in enumerate(merged[: max(1, int(combined_top_k))], start=1):
        item["rank"] = rank
    return merged[: max(1, int(combined_top_k))]
