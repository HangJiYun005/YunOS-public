from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "data" / "test"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "hybrid_e2e_cases.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.embedding_provider import get_embedding_provider
from memory.hybrid_retriever import hybrid_search_preview
from memory.read_only_retriever import retrieve
from memory.semantic_memory import (
    build_embedding_records_from_summaries,
    save_embedding_record,
    semantic_summary_search_preview,
)
from tools.migrate_semantic_memory import migrate_semantic_memory


TOPIC_TEXT = {
    "whale": "鲸鱼 记忆 与历史摘要",
    "YunOS": "YunOS 摘要 保存 语义索引",
    "STM32": "STM32 温度 传感器 硬件",
    "科幻": "科幻 小说 未来社会",
    "图像生成": "图像 生成 工作流",
    "本地模型": "本地 模型 离线推理",
}
TOPIC_IDS = {
    "whale": "whale",
    "YunOS": "yunos",
    "STM32": "stm32",
    "科幻": "scifi",
    "图像生成": "image",
    "本地模型": "local",
}


def load_cases() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return list(json.load(handle)["cases"])


def create_fixture_databases(root: Path, cases: list[dict[str, Any]]) -> tuple[Path, Path]:
    source_db = root / "source.db"
    semantic_db = root / "semantic.db"
    with closing(sqlite3.connect(source_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time INTEGER DEFAULT 0,
                update_time_iso TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                conversation_title TEXT,
                create_time INTEGER DEFAULT 0,
                create_time_iso TEXT,
                role TEXT,
                text TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            """
        )
        for index, case in enumerate(cases, start=1):
            topic = case["topic"]
            conversation_id = f"conversation-{case['id']}"
            source_id = f"k-{TOPIC_IDS[topic]}"
            conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                (conversation_id, topic, index, f"2026-07-10T00:00:0{index}", "whale"),
            )
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    conversation_id,
                    topic,
                    index,
                    f"2026-07-10T00:00:0{index}",
                    "user",
                    f"{case['query']}；测试主题：{TOPIC_TEXT[topic]}",
                    "whale",
                ),
            )
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
            ("general-conversation", "general decoy", 0, "2026-07-09T00:00:00", "general"),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("k-general", "general-conversation", "general decoy", 0, "2026-07-09T00:00:00", "user", "鲸鱼 记忆", "general"),
        )
        conn.commit()

    migrate_semantic_memory(semantic_db, write=True)
    summary_rows = [
        {
            "id": f"s-{TOPIC_IDS[case['topic']]}",
            "memory_space": "whale",
            "summary_type": "conversation_summary",
            "conversation_id": f"conversation-{case['id']}",
            "summary_text": f"脱敏摘要：{TOPIC_TEXT[case['topic']]}。",
            "method": "fixture_e2e_v1",
            "source_message_count": 1,
        }
        for case in cases
    ]
    records, skipped = build_embedding_records_from_summaries(
        summary_rows, provider_name="local_sentence_transformer"
    )
    if skipped:
        raise RuntimeError("E2E fixture unexpectedly skipped summary rows")
    for record in records:
        save_embedding_record(record, semantic_db, dry_run=False)
    return source_db, semantic_db


def quality_gate(metrics: dict[str, Any]) -> str:
    if metrics["hybrid_hit_at_3"] < max(metrics["keyword_hit_at_3"], metrics["semantic_hit_at_3"]):
        return "failed"
    if metrics["hybrid_nonempty_rate"] < max(metrics["keyword_nonempty_rate"], metrics["semantic_nonempty_rate"]):
        return "failed"
    return "passed"


def evaluate_cases(source_db: Path, semantic_db: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    details = []
    keyword_hits = semantic_hits = hybrid_hits = 0
    keyword_nonempty = semantic_nonempty = hybrid_nonempty = 0
    mixed = 0
    natural_keyword = natural_total = short_keyword = short_total = 0

    for case in cases:
        keyword = retrieve(case["query"], top_k=3, memory_space="whale", db_path=source_db)
        semantic = semantic_summary_search_preview(
            case["query"], semantic_db, memory_space="whale", top_k=3, provider_name="local_sentence_transformer"
        )
        hybrid = hybrid_search_preview(
            case["query"], source_db, semantic_db, memory_space="whale",
            keyword_top_k=3, semantic_top_k=3, combined_top_k=3,
        )
        expected = set(case["expected_source_ids"])
        keyword_ids = [row.get("message_id") or row.get("conversation_id") for row in keyword[:3]]
        semantic_rows = semantic["results"][:3]
        semantic_ids = [row["source_id"] for row in semantic_rows]
        combined_rows = hybrid["combined_results"][:3]
        combined_ids = [row["source_id"] for row in combined_rows]
        keyword_hit = bool(expected.intersection(keyword_ids))
        semantic_hit = bool(expected.intersection(semantic_ids))
        hybrid_hit = bool(expected.intersection(combined_ids))
        keyword_hits += keyword_hit
        semantic_hits += semantic_hit
        hybrid_hits += hybrid_hit
        keyword_nonempty += bool(keyword_ids)
        semantic_nonempty += bool(semantic_ids)
        hybrid_nonempty += bool(combined_ids)
        kinds = {row["source_kind"] for row in combined_rows}
        mixed += {"keyword_message", "semantic_summary"}.issubset(kinds)
        if case["query_type"] == "natural":
            natural_total += 1
            natural_keyword += bool(keyword_ids)
        else:
            short_total += 1
            short_keyword += bool(keyword_ids)
        details.append(
            {
                "id": case["id"],
                "topic": case["topic"],
                "keyword_top3": [{"source_id": row.get("message_id") or row.get("conversation_id"), "source_kind": "keyword_message"} for row in keyword[:3]],
                "semantic_top3": [{"source_id": row["source_id"], "source_kind": "semantic_summary"} for row in semantic_rows],
                "hybrid_top3": [{"source_id": row["source_id"], "source_kind": row["source_kind"]} for row in combined_rows],
            }
        )

    count = len(cases)
    metrics = {
        "case_count": count,
        "keyword_hit_at_3": keyword_hits / count,
        "semantic_hit_at_3": semantic_hits / count,
        "hybrid_hit_at_3": hybrid_hits / count,
        "keyword_nonempty_rate": keyword_nonempty / count,
        "semantic_nonempty_rate": semantic_nonempty / count,
        "hybrid_nonempty_rate": hybrid_nonempty / count,
        "natural_query_keyword_nonempty_rate": natural_keyword / natural_total if natural_total else 0.0,
        "short_query_keyword_nonempty_rate": short_keyword / short_total if short_total else 0.0,
        "mixed_source_rate": mixed / count,
    }
    return {**metrics, "quality_gate": quality_gate(metrics), "cases": details}


def run_evaluation() -> dict[str, Any]:
    cases = load_cases()
    # Identity construction is local-only; the provider itself enforces offline model loading.
    get_embedding_provider("local_sentence_transformer").get_embedding_identity()
    with TemporaryDirectory(dir=TEST_ROOT) as tmp:
        temporary_root = Path(tmp)
        source_db, semantic_db = create_fixture_databases(temporary_root, cases)
        result = evaluate_cases(source_db, semantic_db, cases)
    result.update({
        "readonly_source": True,
        "sent_to_model": False,
        "provider": "local_sentence_transformer",
        "temporary_databases_cleaned": not temporary_root.exists(),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the real local hybrid retrieval chain on redacted fixtures.")
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    try:
        result = run_evaluation()
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"quality_gate": "failed", "status": "skipped_or_refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["quality_gate"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
