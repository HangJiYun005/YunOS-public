from __future__ import annotations

import json
from typing import Any


SHORT_SUMMARY_LENGTH = 30


def diagnose_summary(summary: dict) -> dict:
    """
    Diagnose one memory summary without reading or writing database content.
    """
    warnings: list[str] = []
    score = 100

    summary_id = summary.get("id")
    memory_space = str(summary.get("memory_space") or "").strip()
    summary_type = str(summary.get("summary_type") or "").strip()
    summary_text = str(summary.get("summary_text") or "").strip()
    conversation_id = str(summary.get("conversation_id") or "").strip()
    source_message_count = safe_int(summary.get("source_message_count"))
    source_conversation_count = safe_int(summary.get("source_conversation_count"))
    method = str(summary.get("method") or "").strip()

    has_summary_text = bool(summary_text)
    summary_length = len(summary_text)
    if not has_summary_text:
        score -= 60
        warnings.append("summary_text is empty")
    elif summary_length < SHORT_SUMMARY_LENGTH:
        score -= 20
        warnings.append("summary_text is too short")

    has_memory_space = bool(memory_space)
    if not has_memory_space:
        score -= 30
        warnings.append("memory_space is missing")

    has_summary_type = bool(summary_type)
    if not has_summary_type:
        score -= 30
        warnings.append("summary_type is missing")

    has_required_conversation_id = summary_type != "conversation_summary" or bool(conversation_id)
    if not has_required_conversation_id:
        score -= 30
        warnings.append("conversation_summary is missing conversation_id")

    has_source_counts = source_message_count > 0 or source_conversation_count > 0
    if not has_source_counts:
        score -= 15
        warnings.append("source counts are empty")

    keywords_count, keywords_ok = parse_keywords(summary.get("keywords_json"))
    if not keywords_ok:
        score -= 5
        warnings.append("keywords_json is empty or invalid")

    has_method = bool(method)
    if not has_method:
        warnings.append("method is missing")

    return {
        "summary_id": summary_id,
        "memory_space": memory_space,
        "summary_type": summary_type,
        "health_score": max(0, min(100, score)),
        "warnings": warnings,
        "summary_length": summary_length,
        "keywords_count": keywords_count,
        "has_summary_text": has_summary_text,
        "has_memory_space": has_memory_space,
        "has_summary_type": has_summary_type,
        "has_required_conversation_id": has_required_conversation_id,
        "has_source_counts": has_source_counts,
        "has_method": has_method,
    }


def diagnose_summaries(summaries: list[dict]) -> dict:
    """
    Diagnose a list of summaries without reading or writing database content.
    """
    items = [diagnose_summary(summary) for summary in summaries]
    total_count = len(items)
    by_memory_space: dict[str, int] = {}
    by_summary_type: dict[str, int] = {}
    total_source_message_count = 0
    warning_count = 0
    score_total = 0

    for summary, item in zip(summaries, items):
        memory_space = item["memory_space"] or "missing"
        summary_type = item["summary_type"] or "missing"
        by_memory_space[memory_space] = by_memory_space.get(memory_space, 0) + 1
        by_summary_type[summary_type] = by_summary_type.get(summary_type, 0) + 1
        total_source_message_count += safe_int(summary.get("source_message_count"))
        warning_count += len(item["warnings"])
        score_total += int(item["health_score"])

    average_health_score = round(score_total / total_count, 2) if total_count else 0
    warnings: list[str] = []
    if warning_count:
        warnings.append(f"{warning_count} summary health warning(s) found")

    return {
        "total_count": total_count,
        "by_memory_space": by_memory_space,
        "by_summary_type": by_summary_type,
        "total_source_message_count": total_source_message_count,
        "warning_count": warning_count,
        "average_health_score": average_health_score,
        "warnings": warnings,
    }


def parse_keywords(value: Any) -> tuple[int, bool]:
    if value in (None, ""):
        return 0, False
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0, False
    if not isinstance(parsed, list) or not parsed:
        return 0, False
    return len(parsed), True


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
