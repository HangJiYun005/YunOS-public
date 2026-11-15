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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview whale prompt context without calling an API.")
    parser.add_argument("message", help="User input to inspect.")
    parser.add_argument("--db", default=str(TEST_DB_PATH), help="SQLite database path. Defaults to the test DB.")
    parser.add_argument("--real-readonly", action="store_true", help="Required when reading the real database.")
    parser.add_argument("--summary-limit", type=int, default=5, help="Maximum whale summary rows.")
    parser.add_argument("--raw-memory-top-k", type=int, default=5, help="Maximum whale raw memory hits.")
    parser.add_argument("--include-general", action="store_true", help="Include Optional Context - general.")
    parser.add_argument("--preview-chars", type=int, default=300, help="Maximum preview characters per section.")
    parser.add_argument("--json", action="store_true", help="Output JSON diagnostics.")
    parser.add_argument("--show-full-prompt", action="store_true", help="Include the full assembled prompt.")
    parser.add_argument("--retrieval-mode", choices=("legacy", "hybrid"), default="legacy")
    parser.add_argument("--semantic-db")
    parser.add_argument("--combined-top-k", type=int, default=5)
    return parser.parse_args()


def is_real_db_path(db_path: Path) -> bool:
    try:
        return db_path.expanduser().resolve() == REAL_DB_PATH.resolve()
    except OSError:
        return False


def report_for_cli(report: dict[str, Any], show_full_prompt: bool) -> dict[str, Any]:
    payload = dict(report)
    if not show_full_prompt:
        payload.pop("prompt", None)
    return payload


def format_text_report(report: dict[str, Any], show_full_prompt: bool = False) -> str:
    lines = ["Prompt Preview", ""]
    for section in report["sections"]:
        lines.extend(
            [
                f"{section['name']}:",
                f"  enabled: {'yes' if section['enabled'] else 'no'}",
                f"  item_count: {section['item_count']}",
                f"  chars: {section['chars']}",
                f"  estimated_tokens: {section['estimated_tokens']}",
            ]
        )
        if section["preview"]:
            lines.extend(["  preview:", indent_preview(section["preview"], "    ")])
        lines.append("")

    totals = report["totals"]
    lines.extend(
        [
            "Total:",
            f"  chars: {totals['total_chars']}",
            f"  estimated_tokens: {totals['estimated_total_tokens']}",
            f"  enabled_sections: {totals['enabled_sections']}",
            f"  summary_count: {totals['summary_count']}",
            f"  raw_memory_count: {totals['raw_memory_count']}",
            f"  general_context_count: {totals['general_context_count']}",
            "",
            "Ordering:",
            f"  section_order: {' > '.join(report['ordering']['section_order'])}",
            f"  summary_before_raw_memory: {'yes' if report['ordering']['summary_before_raw_memory'] else 'no'}",
        ]
    )
    if show_full_prompt:
        lines.extend(["", "Full Prompt:", report["prompt"]])
    return "\n".join(lines)


def indent_preview(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


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

    report = whale_chat.build_whale_prompt_context(
        args.message,
        db_path=db_path,
        summary_limit=args.summary_limit,
        raw_memory_top_k=args.raw_memory_top_k,
        include_general=args.include_general,
        preview_chars=args.preview_chars,
        retrieval_mode=args.retrieval_mode,
        semantic_db_path=args.semantic_db,
        combined_top_k=args.combined_top_k,
    )
    payload = report_for_cli(report, show_full_prompt=args.show_full_prompt)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report, show_full_prompt=args.show_full_prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
