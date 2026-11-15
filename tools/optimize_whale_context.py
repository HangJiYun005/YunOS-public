from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import whale_chat


TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
SUMMARY_LIMITS = (1, 2, 3, 5)
RAW_MEMORY_TOP_KS = (2, 3, 5, 8)
TOP_CONFIG_COUNT = 5
TOKEN_BUDGET = 3000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare whale prompt context parameter costs without calling an API.")
    parser.add_argument("message", help="User input to inspect.")
    parser.add_argument("--db", default=str(TEST_DB_PATH), help="SQLite database path. Defaults to the test DB.")
    parser.add_argument("--real-readonly", action="store_true", help="Required when reading the real database.")
    parser.add_argument("--test-general", action="store_true", help="Also test include_general=true candidates.")
    parser.add_argument("--json", action="store_true", help="Output JSON diagnostics.")
    return parser.parse_args()


def is_real_db_path(db_path: Path) -> bool:
    try:
        return db_path.expanduser().resolve() == REAL_DB_PATH.resolve()
    except OSError:
        return False


def include_general_values(test_general: bool) -> list[bool]:
    values = [False]
    if test_general:
        values.append(True)
    return values


def score_candidate(candidate: dict[str, Any]) -> float:
    score = 1000.0
    score -= candidate["estimated_total_tokens"] * 0.1
    if candidate["summary_count"] > 0:
        score += 120.0
    if candidate["raw_memory_count"] > 0:
        score += 100.0
    if candidate["general_context_count"] == 0:
        score += 30.0
    if candidate["summary_before_raw_memory"]:
        score += 60.0
    if candidate["estimated_total_tokens"] > TOKEN_BUDGET:
        score -= 250.0
    return round(score, 2)


def candidate_from_report(
    report: dict[str, Any],
    summary_limit: int,
    raw_memory_top_k: int,
    include_general: bool,
) -> dict[str, Any]:
    totals = report["totals"]
    ordering = report["ordering"]
    enabled_section_names = [section["name"] for section in report["sections"] if section["enabled"]]
    candidate = {
        "summary_limit": summary_limit,
        "raw_memory_top_k": raw_memory_top_k,
        "include_general": include_general,
        "total_chars": totals["total_chars"],
        "estimated_total_tokens": totals["estimated_total_tokens"],
        "summary_count": totals["summary_count"],
        "raw_memory_count": totals["raw_memory_count"],
        "general_context_count": totals["general_context_count"],
        "summary_before_raw_memory": ordering["summary_before_raw_memory"],
        "enabled_sections": totals["enabled_sections"],
        "enabled_section_names": enabled_section_names,
        "section_order": ordering["section_order"],
    }
    candidate["score"] = score_candidate(candidate)
    return candidate


def optimize_context(message: str, db_path: str | Path, test_general: bool = False) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for summary_limit in SUMMARY_LIMITS:
        for raw_memory_top_k in RAW_MEMORY_TOP_KS:
            for include_general in include_general_values(test_general):
                report = whale_chat.build_whale_prompt_context(
                    message,
                    db_path=db_path,
                    summary_limit=summary_limit,
                    raw_memory_top_k=raw_memory_top_k,
                    include_general=include_general,
                    preview_chars=0,
                )
                candidates.append(
                    candidate_from_report(
                        report,
                        summary_limit=summary_limit,
                        raw_memory_top_k=raw_memory_top_k,
                        include_general=include_general,
                    )
                )

    ranked = sorted(
        candidates,
        key=lambda item: (
            -item["score"],
            item["estimated_total_tokens"],
            -item["summary_count"],
            -item["raw_memory_count"],
            item["include_general"],
            item["summary_limit"],
            item["raw_memory_top_k"],
        ),
    )
    top_configs = ranked[:TOP_CONFIG_COUNT]
    recommended_config = {
        "summary_limit": top_configs[0]["summary_limit"],
        "raw_memory_top_k": top_configs[0]["raw_memory_top_k"],
        "include_general": top_configs[0]["include_general"],
    }
    return {
        "query": message,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "top_configs": top_configs,
        "recommended_config": recommended_config,
        "reason": recommendation_reason(top_configs[0]),
    }


def recommendation_reason(candidate: dict[str, Any]) -> str:
    parts = [
        f"score={candidate['score']}",
        f"estimated_total_tokens={candidate['estimated_total_tokens']}",
        f"summary_count={candidate['summary_count']}",
        f"raw_memory_count={candidate['raw_memory_count']}",
    ]
    if candidate["general_context_count"] == 0:
        parts.append("general_context_count=0")
    if candidate["summary_before_raw_memory"]:
        parts.append("summary_before_raw_memory=true")
    return "Recommended because it balances lower token cost with available whale summaries/raw memory: " + ", ".join(parts) + "."


def format_text_report(result: dict[str, Any]) -> str:
    lines = [
        "Whale Context Optimizer",
        f"candidate_count: {result['candidate_count']}",
        "",
        "Top 5 Configurations",
    ]
    for index, candidate in enumerate(result["top_configs"], start=1):
        lines.extend(
            [
                f"{index}. summary_limit={candidate['summary_limit']} raw_memory_top_k={candidate['raw_memory_top_k']} include_general={str(candidate['include_general']).lower()}",
                f"   total_chars={candidate['total_chars']} estimated_total_tokens={candidate['estimated_total_tokens']} score={candidate['score']}",
                f"   summary_count={candidate['summary_count']} raw_memory_count={candidate['raw_memory_count']} general_context_count={candidate['general_context_count']}",
                f"   summary_before_raw_memory={str(candidate['summary_before_raw_memory']).lower()} enabled_sections={candidate['enabled_sections']}",
                f"   enabled_section_names={', '.join(candidate['enabled_section_names'])}",
            ]
        )
    config = result["recommended_config"]
    lines.extend(
        [
            "",
            "recommended_config:",
            f"  summary_limit: {config['summary_limit']}",
            f"  raw_memory_top_k: {config['raw_memory_top_k']}",
            f"  include_general: {str(config['include_general']).lower()}",
            "reason:",
            f"  {result['reason']}",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    db_path = Path(args.db)
    if is_real_db_path(db_path) and not args.real_readonly:
        print("Reading the real database requires --real-readonly.", file=sys.stderr)
        return 2

    result = optimize_context(args.message, db_path=db_path, test_general=args.test_general)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
