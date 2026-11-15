from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PROJECTS_PATH = PROJECT_ROOT / "config" / "projects_public.json"
PRIVATE_PROJECTS_PATH = PROJECT_ROOT / "config_private" / "projects_private.json"
TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")

ALLOWED_STATUSES = {"active", "paused", "archived", "mock"}
GENERIC_TERMS = {"AI", "GPT", "test", "项目", "学习", "技术", "记录", "聊天", "系统"}
GENERIC_TERM_KEYS = {item.casefold() for item in GENERIC_TERMS}


@dataclass
class ConfigSummary:
    label: str
    path: Path
    project_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    projects: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.error_count += 1
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warning_count += 1
        self.warnings.append(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate AyunOS project config files without writing data.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--public", action="store_true", help="Validate only config/projects_public.json.")
    group.add_argument("--private", action="store_true", help="Validate only config_private/projects_private.json.")
    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument("--db", help="Database path used for read-only match counts.")
    db_group.add_argument("--real", action="store_true", help="Use the real local memory database in read-only mode.")
    db_group.add_argument("--test", action="store_true", help="Use the sanitized test database in read-only mode.")
    return parser.parse_args()


def database_path(args: argparse.Namespace) -> tuple[Path, str]:
    if args.real:
        return REAL_DB_PATH, "real database"
    if args.db:
        return Path(args.db).expanduser(), "custom database"
    return TEST_DB_PATH, "test database"


def config_targets(args: argparse.Namespace) -> list[tuple[str, Path, bool]]:
    if args.public:
        return [("public", PUBLIC_PROJECTS_PATH, True)]
    if args.private:
        return [("private", PRIVATE_PROJECTS_PATH, False)]
    return [("public", PUBLIC_PROJECTS_PATH, True), ("private", PRIVATE_PROJECTS_PATH, False)]


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def count_conversations(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "conversations"):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])


def match_count(conn: sqlite3.Connection | None, terms: list[str]) -> int | None:
    if conn is None:
        return None
    if not terms or not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
        return None
    clauses: list[str] = []
    params: list[str] = []
    for term in terms:
        pattern = f"%{term}%"
        clauses.append("(c.title LIKE ? OR m.text LIKE ?)")
        params.extend([pattern, pattern])
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT c.conversation_id)
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.conversation_id
        WHERE {" OR ".join(clauses)}
        """,
        params,
    ).fetchone()
    return int(row[0] or 0)


def normalize_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            normalized.append(item.strip())
        else:
            normalized.append("")
    return normalized


def duplicate_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for term in terms:
        key = term.casefold()
        if key in seen:
            duplicates.add(term)
        seen.add(key)
    return sorted(duplicates, key=str.casefold)


def validate_project(
    summary: ConfigSummary,
    raw: Any,
    index: int,
    conn: sqlite3.Connection | None,
    conversation_count: int | None,
) -> dict[str, Any] | None:
    label = f"project[{index}]"
    if not isinstance(raw, dict):
        summary.error(f"{label}: project entry must be an object")
        return None

    for field_name in ("name", "description", "keywords"):
        if field_name not in raw:
            summary.error(f"{label}: missing required field {field_name}")

    name = raw.get("name")
    description = raw.get("description")
    keywords_raw = raw.get("keywords")
    aliases_raw = raw.get("aliases", [])
    status = raw.get("status", "")
    priority = raw.get("priority", "")

    project_name = name.strip() if isinstance(name, str) else f"<invalid {index}>"

    if not isinstance(name, str) or not name.strip():
        summary.error(f"{label}: name must be a non-empty string")
    if not isinstance(description, str):
        summary.error(f"{label}: description must be a string")
    if not isinstance(keywords_raw, list):
        summary.error(f"{project_name}: keywords must be an array")
        keywords: list[str] = []
    else:
        keywords = normalize_terms(keywords_raw)
        if not keywords:
            summary.error(f"{project_name}: keywords must not be empty")
    if "aliases" in raw and not isinstance(aliases_raw, list):
        summary.error(f"{project_name}: aliases must be an array")
        aliases: list[str] = []
    else:
        aliases = normalize_terms(aliases_raw)
    if "status" in raw and isinstance(status, str) and status and status not in ALLOWED_STATUSES:
        summary.warn(f"{project_name}: status is not one of active, paused, archived, mock")
    if "status" in raw and not isinstance(status, str):
        summary.warn(f"{project_name}: status should be a string")
    if "priority" in raw and (not isinstance(priority, (int, float)) or isinstance(priority, bool)):
        summary.error(f"{project_name}: priority must be a number")

    for term_type, terms in (("keyword", keywords), ("alias", aliases)):
        for term in terms:
            if not term:
                summary.warn(f"{project_name}: {term_type} should not be empty")
            elif len(term) < 2:
                summary.warn(f"{project_name}: {term_type} '{term}' is shorter than 2 characters")
            if term.casefold() in GENERIC_TERM_KEYS:
                summary.warn(f"{project_name}: {term_type} '{term}' may be too generic")

    for term in duplicate_terms(keywords):
        summary.error(f"{project_name}: duplicate keyword '{term}' in the same project")
    for term in duplicate_terms(aliases):
        summary.error(f"{project_name}: duplicate alias '{term}' in the same project")

    clean_terms = [term for term in [project_name, *keywords, *aliases] if term]
    count = match_count(conn, clean_terms)
    if count == 0:
        summary.warn(f"{project_name}: match_count is 0")
    if count is not None and conversation_count and count > conversation_count * 0.6:
        summary.warn(f"{project_name}: match_count is higher than 60% of conversations")

    return {
        "name": project_name,
        "status": status if isinstance(status, str) and status else "active",
        "priority": priority if isinstance(priority, (int, float)) and not isinstance(priority, bool) else "",
        "keyword_count": len(keywords),
        "alias_count": len(aliases),
        "match_count": count,
        "keywords": keywords,
        "aliases": aliases,
    }


def validate_config(
    label: str,
    path: Path,
    required: bool,
    conn: sqlite3.Connection | None,
    conversation_count: int | None,
) -> ConfigSummary:
    summary = ConfigSummary(label=label, path=path)
    if not path.is_file():
        if required:
            summary.error("config file is missing")
        else:
            summary.warn("private config is missing")
        return summary

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        summary.error(f"invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}")
        return summary

    if not isinstance(data, list):
        summary.error("top level must be an array")
        return summary

    summary.project_count = len(data)
    seen_names: set[str] = set()
    term_owners: dict[str, list[str]] = {}

    for index, raw in enumerate(data, start=1):
        project = validate_project(summary, raw, index, conn, conversation_count)
        if project is None:
            continue
        name_key = project["name"].casefold()
        if name_key in seen_names:
            summary.error(f"duplicate project name '{project['name']}'")
        seen_names.add(name_key)
        for term in [*project["keywords"], *project["aliases"]]:
            if term:
                term_owners.setdefault(term.casefold(), []).append(project["name"])
        summary.projects.append(project)

    for owners in term_owners.values():
        unique_owners = sorted(set(owners), key=str.casefold)
        if len(unique_owners) > 1:
            summary.warn(f"shared keyword or alias across projects: {', '.join(unique_owners)}")

    return summary


def print_summary(summary: ConfigSummary) -> None:
    print(f"\n## {summary.label} config")
    print(f"file path label: {summary.label}")
    print(f"project_count: {summary.project_count}")
    print(f"warning_count: {summary.warning_count}")
    print(f"error_count: {summary.error_count}")

    if summary.projects:
        print("projects:")
        for project in summary.projects:
            count = "unavailable" if project["match_count"] is None else str(project["match_count"])
            print(
                "- "
                f"name={project['name']}; "
                f"status={project['status']}; "
                f"priority={project['priority']}; "
                f"keyword_count={project['keyword_count']}; "
                f"alias_count={project['alias_count']}; "
                f"match_count={count}"
            )
    if summary.warnings:
        print("warnings:")
        for warning in summary.warnings:
            print(f"- {warning}")
    if summary.errors:
        print("errors:")
        for error in summary.errors:
            print(f"- {error}")


def main() -> int:
    args = parse_args()
    db_path, db_label = database_path(args)
    print("AyunOS Project Config Validation")
    print(f"database label: {db_label}")
    print("database access: read-only")
    print("content output: counts only; no message text or conversation titles")

    conn: sqlite3.Connection | None = None
    conversation_count: int | None = None
    warnings: list[str] = []
    if not db_path.is_file():
        warnings.append(f"{db_label} is missing; match counts are unavailable")
    else:
        try:
            conn = connect_readonly(db_path)
            conversation_count = count_conversations(conn)
            print(f"database_conversation_count: {conversation_count}")
        except sqlite3.Error as exc:
            warnings.append(f"could not open {db_label} read-only: {exc.__class__.__name__}")

    summaries = [
        validate_config(label, path, required, conn, conversation_count)
        for label, path, required in config_targets(args)
    ]
    if conn is not None:
        conn.close()

    for warning in warnings:
        if summaries:
            summaries[0].warn(warning)
    for summary in summaries:
        print_summary(summary)

    total_errors = sum(summary.error_count for summary in summaries)
    total_warnings = sum(summary.warning_count for summary in summaries)

    print("\n## Overall Result")
    if total_errors:
        print("AYUNOS PROJECT CONFIG VALIDATION FAILED")
        return 1
    if total_warnings:
        print("AYUNOS PROJECT CONFIG VALIDATION WARNING")
        return 0
    print("AYUNOS PROJECT CONFIG VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
