from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import request

from memory.read_only_retriever import DEFAULT_DB_PATH, retrieve
from memory.summary_reader import list_summaries
from memory.hybrid_retriever import hybrid_search_preview
from memory.semantic_memory import SemanticRetrievalError


PROJECT_ROOT = Path(__file__).resolve().parent
PERSONA_PATH = PROJECT_ROOT / "config" / "persona_whale.md"
WHALE_SUMMARY_HEADER = "[High-level Memory Summaries - whale]"
WHALE_MEMORY_HEADER = "[Relevant Memory - whale]"
HYBRID_MEMORY_HEADER = "[Hybrid Relevant Memory - whale]"
GENERAL_CONTEXT_HEADER = "[Optional Context - general]"
WHALE_SUMMARY_INTRO = "Existing high-level summaries from YunOS memory_summaries. Read-only context; do not treat this as newly generated memory."
WHALE_MEMORY_INTRO = "以下内容来自 YunOS 中用户与鲸鱼的历史对话检索结果，优先级高于通用资料。"
GENERAL_CONTEXT_INTRO = "以下内容来自 general 记忆空间，只作为知识补充，不覆盖鲸鱼人格记忆。"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
QUERY_STOP_WORDS = {
    "about",
    "before",
    "discuss",
    "talk",
    "talked",
    "remember",
    "previously",
    "were",
}
QUERY_STOP_PHRASES = (
    "我们",
    "之前",
    "以前",
    "聊过",
    "聊到",
    "聊",
    "记得",
    "有没有",
    "是否",
    "关于",
    "和",
    "吗",
    "呢",
    "了",
    "的",
)


def load_persona(path: Path = PERSONA_PATH) -> str:
    return path.read_text(encoding="utf-8")


def estimate_tokens(chars: int) -> int:
    return (max(0, int(chars)) + 3) // 4


def preview_text(text: str, limit: int = 300) -> str:
    clean_limit = max(0, int(limit or 0))
    if clean_limit == 0:
        return ""
    if len(text) <= clean_limit:
        return text
    if clean_limit <= 3:
        return text[:clean_limit]
    return text[: clean_limit - 3] + "..."


def section_diagnostics(name: str, text: str, item_count: int, preview_chars: int) -> dict[str, Any]:
    chars = len(text)
    return {
        "name": name,
        "enabled": bool(text),
        "item_count": item_count if text else 0,
        "chars": chars,
        "estimated_tokens": estimate_tokens(chars),
        "preview": preview_text(text, preview_chars) if text else "",
    }


def build_memory_section(header: str, intro: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = [
        header,
        intro,
        "",
    ]
    for idx, row in enumerate(rows, start=1):
        lines.extend(
            [
                f"{idx}. title: {row.get('title', '')}",
                f"   source_platform: {row.get('source_platform', '')}",
                f"   memory_space: {row.get('memory_space', '')}",
                f"   role: {row.get('role', '')}",
                f"   snippet: {row.get('snippet', '')}",
                f"   conversation_id: {row.get('conversation_id', '')}",
                f"   message_id: {row.get('message_id', '')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def build_relevant_memory_section(rows: list[dict[str, Any]]) -> str:
    return build_memory_section(WHALE_MEMORY_HEADER, WHALE_MEMORY_INTRO, rows)


def build_optional_general_section(rows: list[dict[str, Any]]) -> str:
    return build_memory_section(GENERAL_CONTEXT_HEADER, GENERAL_CONTEXT_INTRO, rows)


def retrieve_prompt_memory(
    user_input: str,
    top_k: int,
    memory_space: str,
    db_path: str | Path,
) -> list[dict[str, Any]]:
    rows = retrieve(user_input, top_k=top_k, memory_space=memory_space, db_path=db_path)
    if rows:
        return rows
    clean_top_k = max(1, min(int(top_k or 5), 20))
    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for query in fallback_queries(user_input):
        for row in retrieve(query, top_k=clean_top_k, memory_space=memory_space, db_path=db_path):
            key = (row.get("conversation_id", ""), row.get("message_id", ""), row.get("hit_source", ""))
            if key in seen:
                continue
            seen.add(key)
            collected.append(row)
            if len(collected) >= clean_top_k:
                return collected
    return collected


def fallback_queries(user_input: str) -> list[str]:
    terms = significant_query_terms(user_input)
    if not any(len(term) >= 3 for term in terms):
        return []
    queries: list[str] = []
    if len(terms) >= 2:
        queries.append(" ".join(terms[:4]))
    queries.extend(terms)
    return dedupe_strings(query for query in queries if query.strip() and query.strip() != user_input.strip())


def significant_query_terms(user_input: str) -> list[str]:
    text = user_input
    for phrase in QUERY_STOP_PHRASES:
        text = text.replace(phrase, " ")
    terms = re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", text)
    return dedupe_strings(term for term in terms if term.lower() not in QUERY_STOP_WORDS)


def dedupe_strings(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def load_whale_summary_rows(db_path: str | Path, limit: int = 5) -> list[dict[str, Any]]:
    """
    Read whale memory summaries for prompt context. Missing table or empty rows returns [].
    """
    clean_limit = max(1, min(int(limit or 5), 5))
    try:
        overview_rows = list_summaries(
            db_path,
            memory_space="whale",
            summary_type="memory_space_overview",
            limit=clean_limit,
        )
        remaining = max(0, clean_limit - len(overview_rows))
        conversation_rows = (
            list_summaries(
                db_path,
                memory_space="whale",
                summary_type="conversation_summary",
                limit=remaining,
            )
            if remaining
            else []
        )
    except Exception:
        return []
    return (overview_rows + conversation_rows)[:clean_limit]


def build_whale_summary_section(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = [WHALE_SUMMARY_HEADER, WHALE_SUMMARY_INTRO, ""]
    for idx, row in enumerate(rows, start=1):
        conversation_id = short_conversation_id(row.get("conversation_id") or "")
        lines.extend(
            [
                f"{idx}. summary_type: {row.get('summary_type', '')}",
                f"   conversation_id: {conversation_id or 'none'}",
                f"   source_message_count: {row.get('source_message_count') or 0}",
                f"   method: {row.get('method') or 'extractive_v1'}",
                f"   summary_text: {row.get('summary_text') or ''}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def load_whale_summary_context(db_path: str | Path, limit: int = 5) -> str:
    return build_whale_summary_section(load_whale_summary_rows(db_path, limit=limit))


def short_conversation_id(conversation_id: str) -> str:
    if len(conversation_id) <= 18:
        return conversation_id
    return f"{conversation_id[:8]}...{conversation_id[-6:]}"


def needs_general_context(user_input: str) -> bool:
    text = user_input.lower()
    expansion_terms = (
        "分析",
        "解释",
        "知识",
        "资料",
        "工具",
        "查询",
        "架构",
        "原理",
        "对比",
        "总结",
        "是什么",
        "为什么",
        "怎么实现",
        "how",
        "why",
        "what is",
        "compare",
        "architecture",
        "memory_space",
    )
    return any(term in text for term in expansion_terms)


def build_prompt(user_input: str, db_path: Path | str = DEFAULT_DB_PATH, top_k: int = 5, summary_limit: int = 5) -> str:
    context = build_whale_prompt_context(
        user_input,
        db_path=db_path,
        summary_limit=summary_limit,
        raw_memory_top_k=top_k,
        include_general=needs_general_context(user_input),
    )
    return context["prompt"]


def build_whale_prompt_context(
    user_input: str,
    db_path: str | Path | None = None,
    summary_limit: int = 5,
    raw_memory_top_k: int = 5,
    include_general: bool = False,
    preview_chars: int = 300,
    retrieval_mode: str = "legacy",
    semantic_db_path: str | Path | None = None,
    combined_top_k: int = 5,
) -> dict[str, Any]:
    """
    Assemble prompt context and diagnostics only.
    Does not call an API and does not write to the database.
    """
    if retrieval_mode not in {"legacy", "hybrid"}:
        raise ValueError("retrieval_mode must be legacy or hybrid")
    if retrieval_mode == "hybrid":
        if semantic_db_path is None:
            raise ValueError("semantic_db_path is required for hybrid retrieval")
        return build_hybrid_whale_prompt_context(
            user_input,
            db_path=db_path,
            summary_limit=summary_limit,
            raw_memory_top_k=raw_memory_top_k,
            include_general=include_general,
            preview_chars=preview_chars,
            semantic_db_path=semantic_db_path,
            combined_top_k=combined_top_k,
        )

    active_db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    persona = load_persona()
    summary_rows = load_whale_summary_rows(active_db_path, limit=summary_limit)
    summary_section = build_whale_summary_section(summary_rows)
    whale_rows = retrieve_prompt_memory(
        user_input,
        top_k=raw_memory_top_k,
        memory_space="whale",
        db_path=active_db_path,
    )
    memory_section = build_relevant_memory_section(whale_rows)
    general_rows: list[dict[str, Any]] = []
    general_section = ""
    if include_general:
        general_rows = retrieve_prompt_memory(
            user_input,
            top_k=raw_memory_top_k,
            memory_space="general",
            db_path=active_db_path,
        )
        general_section = build_optional_general_section(general_rows)

    section_texts = [
        ("System", f"[System]\n{persona.strip()}", 1),
        ("High-level Memory Summaries - whale", summary_section, len(summary_rows)),
        ("Relevant Memory - whale", memory_section, len(whale_rows)),
        ("Optional Context - general", general_section, len(general_rows)),
        ("User Input", f"[User Input]\n{user_input}", 1),
    ]
    sections = [
        section_diagnostics(name, text, item_count, preview_chars)
        for name, text, item_count in section_texts
    ]
    enabled_parts = [text for _, text, _ in section_texts if text]
    prompt = "\n\n".join(enabled_parts)
    section_order = [section["name"] for section in sections]
    summary_index = section_order.index("High-level Memory Summaries - whale")
    raw_memory_index = section_order.index("Relevant Memory - whale")
    total_chars = len(prompt)
    return {
        "sections": sections,
        "totals": {
            "total_chars": total_chars,
            "estimated_total_tokens": estimate_tokens(total_chars),
            "enabled_sections": sum(1 for section in sections if section["enabled"]),
            "summary_count": len(summary_rows),
            "raw_memory_count": len(whale_rows),
            "general_context_count": len(general_rows),
        },
        "ordering": {
            "section_order": section_order,
            "summary_before_raw_memory": summary_index < raw_memory_index,
        },
        "prompt": prompt,
        "retrieval_mode_requested": "legacy",
        "retrieval_mode_effective": "legacy",
        "hybrid_keyword_count": 0,
        "hybrid_semantic_count": 0,
        "hybrid_combined_count": 0,
        "hybrid_warning": "",
    }


def build_hybrid_whale_prompt_context(
    user_input: str,
    db_path: str | Path | None,
    summary_limit: int,
    raw_memory_top_k: int,
    include_general: bool,
    preview_chars: int,
    semantic_db_path: str | Path,
    combined_top_k: int,
) -> dict[str, Any]:
    active_db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    bounded_top_k = max(1, min(int(combined_top_k), 8))
    try:
        hybrid = hybrid_search_preview(
            user_input,
            active_db_path,
            semantic_db_path,
            memory_space="whale",
            keyword_top_k=raw_memory_top_k,
            semantic_top_k=summary_limit,
            combined_top_k=bounded_top_k,
        )
    except SemanticRetrievalError:
        return build_legacy_fallback(
            user_input,
            active_db_path,
            summary_limit,
            raw_memory_top_k,
            include_general,
            preview_chars,
            "semantic retrieval failed",
        )
    except (RuntimeError, ValueError) as exc:
        return build_legacy_fallback(
            user_input, active_db_path, summary_limit, raw_memory_top_k, include_general, preview_chars, str(exc)
        )
    if hybrid.get("semantic_error") or hybrid.get("status") == "refused":
        return build_legacy_fallback(
            user_input,
            active_db_path,
            summary_limit,
            raw_memory_top_k,
            include_general,
            preview_chars,
            str(hybrid.get("semantic_error") or "hybrid retrieval refused"),
        )

    combined_rows = list(hybrid.get("combined_results") or [])
    hybrid_section = build_hybrid_memory_section(combined_rows)
    persona = load_persona()
    general_rows: list[dict[str, Any]] = []
    general_section = ""
    if include_general:
        general_rows = retrieve_prompt_memory(user_input, raw_memory_top_k, "general", active_db_path)
        general_section = build_optional_general_section(general_rows)
    section_texts = [
        ("System", f"[System]\n{persona.strip()}", 1),
        ("Hybrid Relevant Memory - whale", hybrid_section, len(combined_rows)),
        ("Optional Context - general", general_section, len(general_rows)),
        ("User Input", f"[User Input]\n{user_input}", 1),
    ]
    sections = [section_diagnostics(name, text, item_count, preview_chars) for name, text, item_count in section_texts]
    prompt = "\n\n".join(text for _, text, _ in section_texts if text)
    total_chars = len(prompt)
    return {
        "sections": sections,
        "totals": {
            "total_chars": total_chars,
            "estimated_total_tokens": estimate_tokens(total_chars),
            "enabled_sections": sum(1 for section in sections if section["enabled"]),
            "summary_count": 0,
            "raw_memory_count": 0,
            "general_context_count": len(general_rows),
        },
        "ordering": {"section_order": [section["name"] for section in sections], "summary_before_raw_memory": None},
        "prompt": prompt,
        "retrieval_mode_requested": "hybrid",
        "retrieval_mode_effective": "hybrid",
        "hybrid_keyword_count": int(hybrid.get("keyword_count", 0)),
        "hybrid_semantic_count": int(hybrid.get("semantic_count", 0)),
        "hybrid_combined_count": len(combined_rows),
        "hybrid_warning": "",
    }


def build_legacy_fallback(
    user_input: str,
    db_path: str | Path,
    summary_limit: int,
    raw_memory_top_k: int,
    include_general: bool,
    preview_chars: int,
    warning: str,
) -> dict[str, Any]:
    report = build_whale_prompt_context(
        user_input,
        db_path=db_path,
        summary_limit=summary_limit,
        raw_memory_top_k=raw_memory_top_k,
        include_general=include_general,
        preview_chars=preview_chars,
        retrieval_mode="legacy",
    )
    report.update(
        {
            "retrieval_mode_requested": "hybrid",
            "retrieval_mode_effective": "legacy_fallback",
            "hybrid_warning": warning,
        }
    )
    return report


def build_hybrid_memory_section(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = [HYBRID_MEMORY_HEADER, "Read-only hybrid retrieval context.", ""]
    for index, row in enumerate(rows, start=1):
        conversation_id = short_conversation_id(str(row.get("conversation_id") or ""))
        original_score = row.get("original_score")
        score_text = f"{float(original_score):.4f}" if original_score is not None else "none"
        lines.extend(
            [
                f"{index}. source_kind: {row.get('source_kind', '')}",
                f"   rrf_score: {float(row.get('rrf_score', 0.0)):.4f}",
                f"   original_score: {score_text}",
                f"   source_platform: {row.get('source_platform') or 'none'}",
                f"   summary_type: {row.get('summary_type') or 'none'}",
                f"   conversation_id: {conversation_id or 'none'}",
                f"   content_preview: {row.get('content_preview') or ''}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def call_deepseek(prompt: str) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set; prompt was assembled but no API call was made.")
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        DEEPSEEK_CHAT_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble whale persona context from read-only YunOS memory.")
    parser.add_argument("message", help="Current user input.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum whale memory hits.")
    parser.add_argument("--print-prompt", action="store_true", help="Print the assembled prompt without calling an API.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt = build_prompt(args.message, db_path=args.db, top_k=args.top_k)
    if args.print_prompt:
        print(prompt)
        return 0
    print(call_deepseek(prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
