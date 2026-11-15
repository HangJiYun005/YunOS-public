from __future__ import annotations

import argparse
import ast
import fnmatch
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
PUBLIC_PROJECTS_PATH = PROJECT_ROOT / "config" / "projects_public.json"
PRIVATE_PROJECTS_PATH = PROJECT_ROOT / "config_private" / "projects_private.json"
# Formal post-incremental-import baseline. DeepSeek rows remain counted
# separately below, so these values represent the non-DeepSeek slice of the
# current 409-conversation / 16324-message production database.
EXPECTED_REAL_CONVERSATIONS = 408
EXPECTED_REAL_MESSAGES = 15636

BLOCKED_TRACKED_PREFIXES = (
    "data/",
    "imports/",
    "backups/",
    "logs/",
    "docs_private/",
    "config_private/",
    ".venv/",
)

BLOCKED_TRACKED_PATTERNS = ("*.db", "*.sqlite", "*.jsonl", "*.zip")

REQUIRED_IGNORE_PROBES = {
    "data/": "data/probe.txt",
    "imports/": "imports/probe.txt",
    "imports/deepseek/": "imports/deepseek/probe.txt",
    "backups/": "backups/probe.txt",
    "logs/": "logs/probe.txt",
    "docs_private/": "docs_private/probe.md",
    "config_private/": "config_private/probe.json",
    ".venv/": ".venv/probe.txt",
    "*.db": "probe.db",
    "*.jsonl": "probe.jsonl",
    "*.zip": "probe.zip",
}

PUBLIC_DOC_TARGETS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs",
    PROJECT_ROOT / "public_demo",
)

MEDIUM_RISK_DOC_TERMS = (
    "身份证",
    "手机号",
    "银行卡",
    "家庭住址",
    "真实姓名",
    "微信号",
    "password",
    "secret",
    "api_key",
)

EXPECTED_CONVERSATION_COLUMNS = {
    "conversation_id",
    "title",
    "create_time",
    "create_time_iso",
    "update_time",
    "update_time_iso",
    "source_file",
    "default_model_slug",
    "is_archived",
    "is_starred",
    "message_count",
    "user_message_count",
    "assistant_message_count",
    "char_count",
    "tags",
    "importance_score",
    "metadata_json",
    "memory_space",
    "source_platform",
}

EXPECTED_MESSAGE_COLUMNS = {
    "message_id",
    "node_id",
    "conversation_id",
    "conversation_title",
    "ordinal",
    "parent_id",
    "children_json",
    "role",
    "author_name",
    "create_time",
    "create_time_iso",
    "update_time",
    "update_time_iso",
    "content_type",
    "text",
    "status",
    "weight",
    "metadata_json",
    "memory_space",
    "source_platform",
}

MARK_TABLES = ("conversation_tags", "conversation_marks", "message_marks")


class Audit:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.warning_codes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"FAILED: {message}")

    def warn(self, message: str, code: str = "general") -> None:
        self.warnings.append(message)
        self.warning_codes.append(code)
        print(f"WARNING: {message}")

    def pass_line(self, message: str) -> None:
        print(f"PASS: {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only AyunOS project audit.")
    parser.add_argument("--db", default=str(TEST_DB_PATH), help="Database to inspect as the default working database.")
    return parser.parse_args()


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def git_tracked_files(audit: Audit) -> list[str]:
    result = run_git(["ls-files"])
    if result.returncode != 0:
        audit.warn(f"git ls-files failed: {result.stderr.strip()}")
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    print("Tracked files:")
    for path in files:
        print(f"- {path}")
    return files


def check_git(audit: Audit) -> None:
    print("\n## Git Check")
    files = git_tracked_files(audit)
    blocked_prefix_hits = [
        path
        for path in files
        if path.startswith(BLOCKED_TRACKED_PREFIXES) and not path.endswith("/.gitkeep")
    ]
    blocked_pattern_hits = [
        path
        for path in files
        if any(fnmatch.fnmatch(path.lower(), pattern) for pattern in BLOCKED_TRACKED_PATTERNS)
    ]

    if blocked_prefix_hits:
        audit.fail("tracked files exist under local/private data directories")
        for path in blocked_prefix_hits:
            print(f"  - {path}")
    else:
        audit.pass_line("no tracked files under local/private data directories")

    if blocked_pattern_hits:
        audit.fail("tracked files include blocked database/export file patterns")
        for path in blocked_pattern_hits:
            print(f"  - {path}")
    else:
        audit.pass_line("no tracked files match blocked database/export patterns")

    if "config_private/projects_private.json" in files:
        audit.fail("config_private/projects_private.json is tracked")
    else:
        audit.pass_line("config_private/projects_private.json is not tracked")

    deepseek_tracked = [
        path
        for path in files
        if path.startswith("imports/deepseek/") or (path.startswith("imports/") and "deepseek" in path.lower())
    ]
    if deepseek_tracked:
        audit.fail("tracked DeepSeek import files exist")
        for path in deepseek_tracked:
            print(f"  - {path}")
    else:
        audit.pass_line("no tracked DeepSeek import files")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def table_count(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def source_count(conn: sqlite3.Connection, table_name: str, source_platform: str) -> int:
    if "source_platform" not in table_columns(conn, table_name):
        return 0
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE source_platform = ?",
            (source_platform,),
        ).fetchone()[0]
    )


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def check_database(audit: Audit, db_path: Path) -> None:
    print("\n## Default Database Check")
    if not db_path.exists():
        audit.warn(f"default database is missing: {db_path}")
        return
    if db_path != TEST_DB_PATH.resolve():
        audit.warn("default audit database is not the test database")
    else:
        audit.pass_line("default audit database is the test database")

    with connect_ro(db_path) as conn:
        conversations_exists = table_exists(conn, "conversations")
        messages_exists = table_exists(conn, "messages")
        if conversations_exists and messages_exists:
            audit.pass_line("conversations and messages tables exist")
            print(f"conversations rows: {table_count(conn, 'conversations')}")
            print(f"messages rows: {table_count(conn, 'messages')}")
        else:
            audit.warn("conversations or messages table is missing")

        for table in MARK_TABLES:
            if table_exists(conn, table):
                audit.pass_line(f"{table} exists")
                print(f"{table} rows: {table_count(conn, table)}")
            else:
                audit.warn(f"{table} is missing")

        check_original_tables(audit, conn)
        if db_path == TEST_DB_PATH.resolve():
            check_test_database_source_platforms(audit, conn)
            check_test_database_sanitized(audit, conn)
        else:
            audit.pass_line("skipped test-database sanitization scan for explicit real database audit")


def check_original_tables(audit: Audit, conn: sqlite3.Connection) -> None:
    print("\n## Original Table Protection Check")
    if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
        audit.warn("cannot verify source table columns because a source table is missing")
        return

    conversation_columns = table_columns(conn, "conversations")
    message_columns = table_columns(conn, "messages")
    extra_conversation_columns = conversation_columns - EXPECTED_CONVERSATION_COLUMNS
    extra_message_columns = message_columns - EXPECTED_MESSAGE_COLUMNS
    missing_conversation_columns = EXPECTED_CONVERSATION_COLUMNS - conversation_columns
    missing_message_columns = EXPECTED_MESSAGE_COLUMNS - message_columns

    if extra_conversation_columns or extra_message_columns:
        audit.warn("source tables contain unexpected columns")
        for column in sorted(extra_conversation_columns):
            print(f"  - conversations.{column}")
        for column in sorted(extra_message_columns):
            print(f"  - messages.{column}")
    else:
        audit.pass_line("no unexpected columns in source tables")

    if missing_conversation_columns or missing_message_columns:
        audit.warn("source tables are missing expected columns")
        for column in sorted(missing_conversation_columns):
            print(f"  - conversations.{column}")
        for column in sorted(missing_message_columns):
            print(f"  - messages.{column}")
    else:
        audit.pass_line("source table expected columns are present")


def sensitive_terms() -> list[str]:
    return [
        "ExampleUser",
        "Wei " + "Qihang",
        "wei" + "qihang",
        "wei" + "qihang826",
        "ExampleUser",
        "ExampleUser",
        "C:" + "\\Users\\YUN",
        "messages" + ".jsonl",
        "ayunos" + "_chatgpt" + ".db",
        "conversations" + "_index.csv",
        "assets" + "_index.csv",
        "ayunos" + "_chatgpt" + "_import_pack.zip",
        "\u79c1\u4eba\u804a\u5929",
        "\u5b8c\u6574\u804a\u5929\u8bb0\u5f55",
        "\u4eb2\u5b50\u5173\u7cfb",
        "\u604b\u7231",
        "\u4f53\u91cd",
        "\u8102\u80aa\u809d",
        "\u751f\u6d3b\u8d39",
    ]


def check_test_database_sanitized(audit: Audit, conn: sqlite3.Connection) -> None:
    print("\n## Test Database Sanitization Check")
    if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
        audit.warn("cannot inspect test database sanitization because source tables are missing")
        return
    terms = sensitive_terms()
    hits = []
    for term in terms:
        title_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE title LIKE ?",
                (f"%{term}%",),
            ).fetchone()[0]
        )
        text_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE text LIKE ?",
                (f"%{term}%",),
            ).fetchone()[0]
        )
        if title_count or text_count:
            hits.append((term, title_count, text_count))
    if hits:
        audit.warn("test database contains sensitive-looking terms")
        for _, title_count, text_count in hits:
            print(f"  - title hits={title_count}, text hits={text_count}")
    else:
        audit.pass_line("test database titles/text pass sensitive-term check")


def check_test_database_source_platforms(audit: Audit, conn: sqlite3.Connection) -> None:
    print("\n## Test Database Source Platform Check")
    conversation_columns = table_columns(conn, "conversations")
    message_columns = table_columns(conn, "messages")
    if "source_platform" in conversation_columns and "source_platform" in message_columns:
        audit.pass_line("test database conversations/messages include source_platform")
    else:
        audit.fail("test database source tables must include source_platform")
        return

    values = {
        row[0] or ""
        for row in conn.execute(
            """
            SELECT source_platform FROM conversations
            UNION
            SELECT source_platform FROM messages
            """
        )
    }
    allowed = {"chatgpt", "deepseek", "claude", "gemini", "manual", "markdown", "txt", "unknown"}
    invalid = values - allowed
    if invalid:
        audit.fail("test database contains invalid source_platform values")
        for value in sorted(invalid):
            print(f"  - {value}")
    else:
        audit.pass_line("test database source_platform values are valid")

    expected_mock_sources = {"chatgpt", "deepseek", "manual"}
    if expected_mock_sources.issubset(values):
        audit.pass_line("test database includes chatgpt, deepseek, and manual mock source platforms")
    else:
        audit.fail("test database must include chatgpt, deepseek, and manual mock source platforms")


def check_real_database(audit: Audit) -> None:
    print("\n## Real Database Check")
    if not REAL_DB_PATH.exists():
        audit.warn("real local database is missing")
        return
    audit.pass_line("real local database exists")
    with connect_ro(REAL_DB_PATH) as conn:
        if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
            audit.fail("real database source tables are missing")
            return
        conversation_count = table_count(conn, "conversations")
        message_count = table_count(conn, "messages")
        deepseek_conversation_count = source_count(conn, "conversations", "deepseek")
        deepseek_message_count = source_count(conn, "messages", "deepseek")
        baseline_conversation_count = conversation_count - deepseek_conversation_count
        baseline_message_count = message_count - deepseek_message_count
        print(f"real conversations rows: {conversation_count}")
        print(f"real messages rows: {message_count}")
        print(f"real deepseek conversations rows: {deepseek_conversation_count}")
        print(f"real deepseek messages rows: {deepseek_message_count}")
        if (
            baseline_conversation_count != EXPECTED_REAL_CONVERSATIONS
            or baseline_message_count != EXPECTED_REAL_MESSAGES
        ):
            audit.fail(
                "real database non-DeepSeek row counts differ from the expected readonly baseline"
            )
        else:
            audit.pass_line("real database non-DeepSeek row counts match the expected readonly baseline")
            if deepseek_conversation_count or deepseek_message_count:
                audit.pass_line("real database DeepSeek import rows are counted separately")

        real_extra_conversation_columns = table_columns(conn, "conversations") - EXPECTED_CONVERSATION_COLUMNS
        real_extra_message_columns = table_columns(conn, "messages") - EXPECTED_MESSAGE_COLUMNS
        optional_real_columns = {"source_platform"}
        real_missing_conversation_columns = (
            EXPECTED_CONVERSATION_COLUMNS - optional_real_columns - table_columns(conn, "conversations")
        )
        real_missing_message_columns = EXPECTED_MESSAGE_COLUMNS - optional_real_columns - table_columns(conn, "messages")
        if (
            real_extra_conversation_columns
            or real_extra_message_columns
            or real_missing_conversation_columns
            or real_missing_message_columns
        ):
            audit.fail("real database source table schema differs from the expected baseline")

        tag_count = 0
        note_count = 0
        if table_exists(conn, "conversation_tags"):
            tag_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM conversation_tags WHERE tag = ?",
                    ("test",),
                ).fetchone()[0]
            )
        for table in ("conversation_marks", "message_marks"):
            if table_exists(conn, table):
                note_count += int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE note LIKE ?",
                        ("%smoke test%",),
                    ).fetchone()[0]
                )
        if tag_count or note_count:
            audit.warn(
                "real database contains historical test markers",
                code="historical_test_markers",
            )
            print(f"  - tag=test rows={tag_count}")
            print(f"  - smoke-test notes={note_count}")
        else:
            audit.pass_line("no test markers found in the real mark/tag tables")


def is_ignored(path: str) -> bool:
    return run_git(["check-ignore", "-q", "--", path]).returncode == 0


def check_files(audit: Audit) -> None:
    print("\n## File Safety Check")
    required_files = [
        ".gitignore",
        "tools/privacy_scan.py",
        "tools/validate_projects.py",
        "README.md",
        "tools/create_test_db.py",
        "docs/security_audit.md",
        "docs/timeline_view.md",
        "docs/source_platforms.md",
        "docs/evidence_selection_v7_1.md",
        "memory/evidence_selection.py",
        "tools/evidence_selection_preview.py",
        "docs/context_assembly_v7_3.md",
        "memory/context_assembly.py",
        "tools/context_assembly_preview.py",
    ]
    for rel_path in required_files:
        if (PROJECT_ROOT / rel_path).is_file():
            audit.pass_line(f"{rel_path} exists")
        else:
            audit.fail(f"{rel_path} is missing")

    for label, probe in REQUIRED_IGNORE_PROBES.items():
        if is_ignored(probe):
            audit.pass_line(f"{label} is ignored")
        else:
            audit.fail(f"{label} is not ignored")


def check_default_entrypoints(audit: Audit) -> None:
    print("\n## Default Entrypoint Check")
    checks = {
        "tools/search_memory.py": "data\" / \"test\" / \"ayunos_memory_test.db",
        "tools/memory_browser.py": "data\" / \"test\" / \"ayunos_memory_test.db",
        "tools/local_web.py": "DEFAULT_DB_PATH = TEST_DB_PATH",
        "tools/migrate_memory_marks.py": "data\" / \"test\" / \"ayunos_memory_test.db",
        "tools/backup_memory_db.py": "data\" / \"test\" / \"ayunos_memory_test.db",
        "tools/validate_projects.py": "return TEST_DB_PATH, \"test database\"",
        "tools/audit_project.py": "default=str(TEST_DB_PATH)",
    }
    for rel_path, needle in checks.items():
        text = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        if needle in text:
            audit.pass_line(f"{rel_path} defaults to test database")
        else:
            audit.warn(f"{rel_path} may not default to the test database")

    run_text = (PROJECT_ROOT / "run.ps1").read_text(encoding="utf-8")
    for command in ("search", "browser", "web", "backup", "validate-projects", "audit"):
        block = command_block(run_text, command)
        if not block:
            audit.fail(f"run.ps1 is missing default {command} command")
        elif "$RealDb" in block or "--real" in block:
            audit.fail(f"default {command} command must not target the real database")
        else:
            audit.pass_line(f"default {command} command does not target the real database")

    for command in ("search-real", "browser-real", "web-real", "backup-real", "validate-projects-real", "audit-real"):
        block = command_block(run_text, command)
        if not block:
            audit.fail(f"run.ps1 is missing explicit real command: {command}")
        elif command == "validate-projects-real" and "--real" in block and "No message text or titles will be printed." in block:
            audit.pass_line(f"{command} explicitly targets the real database and prints a warning")
        elif "$RealDb" in block and "Warn-RealDb" in block:
            audit.pass_line(f"{command} explicitly targets the real database and prints a warning")
        else:
            audit.fail(f"{command} must explicitly target the real database and print a warning")


def command_block(text: str, command: str) -> str:
    marker = f'    "{command}" {{'
    start = text.find(marker)
    if start == -1:
        return ""
    next_marker = text.find('\n    "', start + len(marker))
    if next_marker == -1:
        next_marker = text.find("\n    default", start + len(marker))
    if next_marker == -1:
        return text[start:]
    return text[start:next_marker]


def function_source(text: str, name: str, indent: str = "    ") -> str:
    marker = f"\n{indent}def {name}("
    start = text.find(marker)
    if start == -1:
        marker = f"\ndef {name}("
        start = text.find(marker)
    if start == -1:
        return ""
    next_marker = text.find(f"\n{indent}def ", start + len(marker))
    if next_marker == -1:
        next_marker = text.find("\ndef ", start + len(marker))
    if next_marker == -1:
        return text[start:]
    return text[start:next_marker]


def check_readonly_entrypoints(audit: Audit) -> None:
    print("\n## Real Readonly Entrypoint Check")
    run_text = (PROJECT_ROOT / "run.ps1").read_text(encoding="utf-8")
    local_web_text = (PROJECT_ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")

    web_block = command_block(run_text, "web")
    readonly_block = command_block(run_text, "web-real-readonly")
    profile_admin_block = command_block(run_text, "profile-admin")
    runtime_review_block = command_block(run_text, "runtime-review")
    runtime_admin_block = command_block(run_text, "runtime-admin")

    if readonly_block:
        audit.pass_line("run.ps1 contains web-real-readonly command")
    else:
        audit.fail("run.ps1 is missing web-real-readonly command")

    if readonly_block and "--readonly" in readonly_block and "--real" in readonly_block:
        audit.pass_line("web-real-readonly uses --real and --readonly")
    else:
        audit.fail("web-real-readonly must use --real and --readonly")

    if readonly_block and "tools\\local_web.py" in readonly_block and "$RealDb" in readonly_block:
        audit.pass_line("web-real-readonly targets the explicit real database path")
    else:
        audit.fail("web-real-readonly must target the real database, not the test database")

    if (
        profile_admin_block
        and "tools\\local_web.py" in profile_admin_block
        and "$RealDb" in profile_admin_block
        and "--real" in profile_admin_block
        and "--readonly" in profile_admin_block
        and "--profile-admin" in profile_admin_block
    ):
        audit.pass_line("profile-admin is an explicit fixed-real-source mode with readonly source access")
    else:
        audit.fail("profile-admin must explicitly use the fixed real source with --real --readonly")

    if (
        runtime_review_block
        and "tools\\local_web.py" in runtime_review_block
        and "$RealDb" in runtime_review_block
        and "--real" in runtime_review_block
        and "--readonly" in runtime_review_block
        and "--runtime-review" in runtime_review_block
        and "--allow-external-model-calls" not in runtime_review_block
        and "--confirm-deepseek-send" not in runtime_review_block
    ):
        audit.pass_line("runtime-review is explicit, fixed-source, readonly, and has zero execution flags")
    else:
        audit.fail("runtime-review must use the fixed real-readonly source with no execution flags")

    if (
        runtime_admin_block
        and "tools\\local_web.py" in runtime_admin_block
        and "$RealDb" in runtime_admin_block
        and "--real" in runtime_admin_block
        and "--readonly" in runtime_admin_block
        and "--runtime-admin" in runtime_admin_block
        and "--allow-external-model-calls" in runtime_admin_block
        and "--confirm-deepseek-send" in runtime_admin_block
    ):
        audit.pass_line("runtime-admin is explicit, fixed-source, readonly, and external-send confirmed")
    else:
        audit.fail("runtime-admin must require fixed real-readonly source and both send confirmations")

    if web_block and "--db" not in web_block and "--real" not in web_block and "--readonly" not in web_block:
        audit.pass_line("default web command still uses local_web.py default test database")
    else:
        audit.fail("default web command must continue using the test database")

    if "Readonly mode: writes are disabled" in local_web_text and "HTTPStatus.FORBIDDEN" in local_web_text:
        audit.pass_line("local_web.py rejects readonly POST writes with HTTP 403 JSON")
    else:
        audit.fail("local_web.py must reject readonly POST writes with HTTP 403 JSON")

    if "?mode=ro" in local_web_text and "sqlite3.connect(uri, uri=True)" in local_web_text:
        audit.pass_line("local_web.py opens readonly database connections with SQLite mode=ro")
    else:
        audit.fail("readonly database connections must use SQLite mode=ro")

    if "if not app.readonly:" in local_web_text and "app.ensure_marks_schema()" in local_web_text:
        audit.pass_line("readonly mode skips mark/tag schema migration")
    else:
        audit.fail("readonly mode must skip mark/tag schema migration")

    selection_source = function_source(local_web_text, "api_evidence_selection_preview")
    if (
        selection_source
        and "self.app.readonly" in selection_source
        and 'self.app.db_mode == "real"' in selection_source
        and "REAL_DB_PATH.resolve()" in selection_source
        and "build_production_evidence_selection" in selection_source
        and '"db"' not in selection_source
    ):
        audit.pass_line("v7.1 GET preview is fixed-path production-readonly")
    else:
        audit.fail("v7.1 GET preview must be fixed-path production-readonly and reject database paths")

    temporal_source = function_source(local_web_text, "api_temporal_memory_candidates_preview")
    if (
        temporal_source
        and "self.app.readonly" in temporal_source
        and 'self.app.db_mode == "real"' in temporal_source
        and "REAL_DB_PATH.resolve()" in temporal_source
        and "build_production_temporal_candidates" in temporal_source
        and '"db"' not in temporal_source
        and '"provider"' not in temporal_source
    ):
        audit.pass_line("v7.2 GET preview is fixed-path production-readonly and local-only")
    else:
        audit.fail("v7.2 GET preview must reject database/provider inputs and require production-readonly")

    temporal_core = (PROJECT_ROOT / "memory" / "temporal_memory_candidates.py").read_text(encoding="utf-8")
    if (
        '"candidate_only": True' in temporal_core
        and '"persisted": False' in temporal_core
        and '"external_calls": False' in temporal_core
        and "DEFAULT_TIMEZONE = \"Asia/Shanghai\"" in temporal_core
        and "DEFAULT_COUNTRY_OR_REGION = \"CN\"" in temporal_core
        and "STATEMENT_MAX_CHARS = 120" in temporal_core
        and "REASON_MAX_CHARS = 100" in temporal_core
        and "safe_placeholder_and_needs_review" in temporal_core
        and "def _attribution_gate" in temporal_core
        and "def _scan_scoped_segments" in temporal_core
        and "inherited_scopes" in temporal_core
        and "role != \"user\"" in temporal_core
        and "quoted_scope" in temporal_core
        and "hypothetical_scope" in temporal_core
        and "direct_question_scope" in temporal_core
        and "def _primary_polarity" in temporal_core
        and "excluded_target_keys" in temporal_core
        and "class _TemporalCandidateCore" in temporal_core
        and "class TemporalCandidateService" in temporal_core
        and "trusted_predecessor_revision_chain_v1" in temporal_core
        and "def _validate_trusted_timezone_predecessor" in temporal_core
        and "def _is_reserved_identity" in temporal_core
        and "_RESERVED_IDENTITY_PAYLOADS" in temporal_core
        and '"config": deepcopy(trusted_previous)' in temporal_core
        and "source_memory_projection" in temporal_core
    ):
        audit.pass_line("v7.2 candidate-only, local-provider, timezone, and character boundaries are explicit")
    else:
        audit.fail("v7.2 core boundaries are incomplete")

    assembly_source = function_source(local_web_text, "api_context_assembly_preview")
    if (
        assembly_source
        and "self.app.readonly" in assembly_source
        and 'self.app.db_mode == "real"' in assembly_source
        and "REAL_DB_PATH.resolve()" in assembly_source
        and "build_production_context_assembly" in assembly_source
        and '"db"' not in assembly_source
        and '"profile"' not in assembly_source
        and '"provider"' not in assembly_source
    ):
        audit.pass_line("v7.3 GET preview is fixed-path production-readonly and accepts no trust injection")
    else:
        audit.fail("v7.3 GET preview must reject path/profile/provider inputs and require production-readonly")

    assembly_core = (PROJECT_ROOT / "memory" / "context_assembly.py").read_text(encoding="utf-8")
    if (
        'PROTOCOL_VERSION = "v7.3-context-assembly-2"' in assembly_core
        and 'SELECTION_POLICY_VERSION = "v7.3-context-selection-policy-2"' in assembly_core
        and 'ASSEMBLY_IDENTITY_VERSION = "v7.3-context-assembly-identity-2"' in assembly_core
        and 'RELEVANCE_PROFILE = "latin-exact-token-han-conservative-v2"' in assembly_core
        and "_LATIN_TOKEN_RE" in assembly_core
        and "_HAN_SEGMENT_RE" in assembly_core
        and "_v70_redacted_text" in assembly_core
        and "_v70_reject_absolute_paths" in assembly_core
        and "def _contains_absolute_path" not in assembly_core
        and 'for field in ("capabilities", "degradation", "missing_capabilities", "warnings")' in assembly_core
        and '"candidate_promotion_allowed": False' in assembly_core
        and '"approval_status": "not_approved"' in assembly_core
        and '"persisted": False' in assembly_core
        and '"external_model_calls": False' in assembly_core
        and "def validate_context_assembly_source_backed" in assembly_core
        and "class ContextAssemblyService" in assembly_core
    ):
        audit.pass_line("v7.3 propagation, relevance, v7.0 privacy, candidate, identity, source-backed, and zero-write boundaries are explicit")
    else:
        audit.fail("v7.3 context assembly boundaries are incomplete")

    delivery_source = function_source(local_web_text, "api_context_delivery_preview")
    if (
        delivery_source
        and "self.app.readonly" in delivery_source
        and 'self.app.db_mode == "real"' in delivery_source
        and "REAL_DB_PATH.resolve()" in delivery_source
        and "build_production_context_delivery" in delivery_source
        and '"db"' not in delivery_source
        and '"profile"' not in delivery_source
        and '"provider"' not in delivery_source
        and '"exclude"' in delivery_source
        and '"sent_to_model": False' in delivery_source
        and '"persisted": False' in delivery_source
    ):
        audit.pass_line("v7.4 GET delivery is fixed-path real-readonly, adjustable, non-persisted, and non-sent")
    else:
        audit.fail("v7.4 GET delivery must reject trust/path injection and remain non-persisted/non-sent")

    delivery_core = (PROJECT_ROOT / "memory" / "context_delivery.py").read_text(encoding="utf-8")
    web_index = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    web_app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    if (
        'PROTOCOL_VERSION = "v7.4-transparent-context-delivery-1"' in delivery_core
        and 'DELIVERY_IDENTITY_VERSION = "v7.4-context-delivery-identity-1"' in delivery_core
        and 'TEMPORARY_EXCLUSION_POLICY = "v7.4-current-delivery-exclusion-1"' in delivery_core
        and '"persisted": False' in delivery_core
        and '"sent_to_model": False' in delivery_core
        and '"candidate_promotion_allowed": False' in delivery_core
        and 'trust_status="candidate/not_approved"' in delivery_core
        and 'trust_status="historical_evidence"' in delivery_core
        and "def _resolve_exclusion_closure" in delivery_core
        and '"model_payload_provenance"' in delivery_core
        and '"expanded_excerpt_provenance"' in delivery_core
        and '"excluded_provenance"' in delivery_core
        and "validate_context_assembly_standalone" in delivery_core
        and "def validate_context_delivery_against_assembly" in delivery_core
        and 'view: "contextReview"' in web_app
        and "function loadContextReview" in web_app
        and "function renderContextReviewResults" in web_app
        and "/api/context-delivery-preview" in web_app
    ):
        audit.pass_line("v7.4 delivery identity, trust separation, temporary exclusion, validation, and review UI are explicit")
    else:
        audit.fail("v7.4 transparent context review boundaries are incomplete")

    profile_core = (PROJECT_ROOT / "memory" / "approved_profile_control.py").read_text(encoding="utf-8")
    profile_get = function_source(local_web_text, "api_approved_profile_control")
    profile_post = function_source(local_web_text, "api_approved_profile_decision")
    profile_init = (PROJECT_ROOT / "tools" / "init_approved_profile_sidecar.py").read_text(encoding="utf-8")
    if (
        'PROTOCOL_VERSION = "v8.0-a-lite-approved-profile-control-2"' in profile_core
        and 'SIDECAR_SCHEMA_VERSION = "v8.0-a-lite-approved-profile-sidecar-2"' in profile_core
        and "validate_temporal_candidates_standalone" in profile_core
        and "build_production_temporal_candidates" in profile_core
        and "approved_profile_decisions_no_update" in profile_core
        and "approved_profile_decisions_no_delete" in profile_core
        and "approved_profile_receipts_no_update" in profile_core
        and "approved_profile_receipts_no_delete" in profile_core
        and "decision_source TEXT NOT NULL CHECK(decision_source = 'manual')" in profile_core
        and 'item["model_visible"] = True' in profile_core
        and '"decision_history": history' in profile_core
        and "def _make_receipt" in profile_core
        and "def _validate_receipts" in profile_core
        and "def _append_approved_profile_decision" in profile_core
        and "def _record_test_approved_profile_decision" in profile_core
        and "def open_production_profile_admin" in profile_core
        and "is_trusted_profile_admin_session" in profile_core
        and "def _validate_decision_rows" in profile_core
        and "BEGIN IMMEDIATE" in profile_core
        and "expected_revision" in profile_core
        and "request_id TEXT NOT NULL UNIQUE" in profile_core
        and "mutation_id TEXT NOT NULL UNIQUE" in profile_core
        and "def active_approved_profile_projection" in profile_core
        and "external_model_calls" in profile_core
        and "sent_to_model" in profile_core
    ):
        audit.pass_line("v8.0-A Lite manual source-backed decisions, append-only receipts/history, model visibility, and no-model boundaries are explicit")
    else:
        audit.fail("v8.0-A Lite approved profile control boundaries are incomplete")

    if (
        profile_get
        and "profile_control_surface_available" in profile_get
        and "build_production_approved_profile_review" in profile_get
        and 'set(params) - {"q", "budget"}' in profile_get
        and '"db"' not in profile_get
        and profile_post
        and "profile_mutation_surface_available" in profile_post
        and "record_production_approved_profile_decision" in profile_post
        and "profile_admin=self.app.profile_admin_session" in profile_post
        and "expected_revision" in profile_post
        and "request_id" in profile_post
        and "mutation_id" in profile_post
        and '"sidecar_path"' not in profile_post
    ):
        audit.pass_line("v8.0-A Lite preview is real-readonly while POST requires explicit profile-admin and fixed production services")
    else:
        audit.fail("v8.0-A Lite API surfaces must reject source/sidecar path injection")

    if (
        'parser.add_argument("--execute", action="store_true"' in profile_init
        and "--allow-production-profile-sidecar" in profile_init
        and "--confirm-production-profile-sidecar-init" in profile_init
        and "test_mode=args.test_sidecar is not None" in profile_init
        and "confirmation_type_invalid" in profile_core
        and "allow_production_profile_sidecar is True" in profile_core
        and "confirm_production_profile_sidecar_init is True" in profile_core
    ):
        audit.pass_line("v8.0-A Lite production sidecar initialization is explicit and confirmation-gated")
    else:
        audit.fail("v8.0-A Lite production sidecar must remain uninitialized by default")

    production_delivery_core = (PROJECT_ROOT / "memory" / "production_delivery.py").read_text(encoding="utf-8")
    if (
        'PROTOCOL_VERSION = "v8.0-e-production-delivery-1"' in production_delivery_core
        and 'PROFILE_POLICY_VERSION = "v8.0-e-active-model-visible-all-1"' in production_delivery_core
        and 'LEXICAL_FALLBACK_VERSION = "v8.0-e-readonly-fts-fallback-1"' in production_delivery_core
        and "build_sealed_v74_delivery" in production_delivery_core
        and "active_profile_enablement_projection" in production_delivery_core
        and "mode=ro&immutable=1" in production_delivery_core
        and "messages_fts MATCH" in production_delivery_core
        and '"direct_user_first"' in production_delivery_core
        and '"credential_read": False' in production_delivery_core
        and "def validate_production_delivery_source_backed" in production_delivery_core
        and "def production_profile_binding" in production_delivery_core
        and "build_production_context_delivery" in local_web_text
    ):
        audit.pass_line("v8.0-E UI/runtime production Delivery is one fixed readonly builder with all-active profile policy and source-backed lexical fallback")
    else:
        audit.fail("v8.0-E production Delivery builder, profile policy, lexical fallback, or UI integration is incomplete")

    runtime_core = (PROJECT_ROOT / "memory" / "runtime_thin_slice.py").read_text(encoding="utf-8")
    adapter_core = (PROJECT_ROOT / "memory" / "deepseek_adapter.py").read_text(encoding="utf-8")
    runtime_init = (PROJECT_ROOT / "tools" / "init_runtime_sidecar.py").read_text(encoding="utf-8")
    runtime_cli = (PROJECT_ROOT / "tools" / "runtime_thin_slice.py").read_text(encoding="utf-8")
    runtime_preview = function_source(local_web_text, "api_runtime_preview")
    runtime_execute = function_source(local_web_text, "api_runtime_execute")
    if (
        'PROTOCOL_VERSION = "v8.0-bc-deepseek-runtime-thin-slice-1"' in runtime_core
        and 'SIDECAR_SCHEMA_VERSION = "v8.0-bc-runtime-sidecar-2"' in runtime_core
        and 'REQUEST_ARTIFACT_VERSION = "v8.0-bc-runtime-request-artifact-2"' in runtime_core
        and 'PROVIDER_ID = "deepseek"' in adapter_core
        and 'MODEL_ID = "deepseek-chat"' in adapter_core
        and '"stream": False' in runtime_core
        and "def _production_runtime_delivery" in runtime_core
        and "build_production_delivery" in runtime_core
        and "production_profile_binding" in runtime_core
        and "validate_trusted_delivery_standalone" in runtime_core
        and '"model_context_sha256"' in runtime_core
        and '"wire_body_sha256"' in runtime_core
        and '"wire_body_base64"' in runtime_core
        and '"system_instruction_version"' in runtime_core
        and '"approved_profile_binding"' in runtime_core
        and "def validate_runtime_request_artifact" in runtime_core
    ):
        audit.pass_line("v8.0-B/C request artifact binds the shared v8.0-E production Delivery, actual approved-profile contents, system, model, and payload")
    else:
        audit.fail("v8.0-B/C request artifact or source-backed authority binding is incomplete")

    if (
        all(f"CREATE TABLE {table}" in runtime_core for table in (
            "runtime_conversations", "runtime_request_artifacts", "runtime_model_runs",
            "runtime_messages", "runtime_errors", "runtime_run_events", "runtime_receipts",
        ))
        and 'RUN_STATUSES = ("prepared", "running", "succeeded", "failed")' in runtime_core
        and "runtime ledger is append-only" in runtime_core
        and "BEGIN IMMEDIATE" in runtime_core
        and "request_id TEXT NOT NULL UNIQUE" in runtime_core
        and "mutation_id TEXT NOT NULL UNIQUE" in runtime_core
        and "def _rebuild_terminal_receipt" in runtime_core
        and "def recover_running" in runtime_core
        and '"status": "reserved", "invoked": False' in runtime_core
        and "test runtime primitive cannot target the production sidecar" in runtime_core
        and "def _build_production_runtime_core_factory" in runtime_core
        and '"send_attempt_started"' in runtime_core
        and '"provider_response_received"' in runtime_core
    ):
        audit.pass_line("v8.0-B/C runtime ledger, state machine, idempotent receipt, recovery, and append-only gates are explicit")
    else:
        audit.fail("v8.0-B/C runtime ledger or failure-recovery boundary is incomplete")

    if (
        "import sqlite3" not in adapter_core
        and "from memory." not in adapter_core
        and "import memory." not in adapter_core
        and "urlopen" in adapter_core
        and "parse_deepseek_response" in adapter_core
        and "DEEPSEEK_API_KEY" in adapter_core
        and "config_private" in adapter_core
        and "DeepSeekProviderError" in adapter_core
    ):
        audit.pass_line("DeepSeek adapter is transport-only with local credential loading and sanitized errors")
    else:
        audit.fail("DeepSeek adapter must not access databases, retrieval, profiles, or candidates")

    if (
        runtime_preview
        and "runtime_surface_available" in runtime_preview
        and '"request_id", "mutation_id"' in runtime_preview
        and "self.app.runtime_execution_service.preview" in runtime_preview
        and '"db"' not in runtime_preview
        and '"provider"' not in runtime_preview
        and runtime_execute
        and "runtime_mutation_surface_available" in runtime_execute
        and "self.app.runtime_execution_service.execute" in runtime_execute
        and '"expected_bindings"' in runtime_execute
        and '"confirmed"' in runtime_execute
        and "authority=self.app.runtime_execution_session" in runtime_execute
        and '"sidecar_path"' not in runtime_execute
        and '"provider"' not in runtime_execute
        and '"delivery"' not in runtime_execute
        and '"artifact"' not in runtime_execute
        and "memory.runtime_thin_slice" not in local_web_text
        and "RuntimeThinSliceService" not in local_web_text
        and '"/api/runtime-recover"' not in local_web_text
    ):
        audit.pass_line("v8.0-G production API uses the fixed D1 service, exact reviewed bindings, and no B/C fallback")
    else:
        audit.fail("v8.0-G production API must reject injection and remain isolated from B/C execution/recovery")

    if (
        'parser.add_argument("--execute", action="store_true"' in runtime_init
        and "--allow-production-runtime-sidecar" in runtime_init
        and "--confirm-production-runtime-sidecar-init" in runtime_init
        and "test_mode=args.test_sidecar is not None" in runtime_init
        and "confirmation_type_invalid" in runtime_core
        and "allow_production_runtime_sidecar is True" in runtime_core
        and "confirm_production_runtime_sidecar_init is True" in runtime_core
        and "--allow-external-model-call" in runtime_cli
        and "--confirm-deepseek-send" in runtime_cli
        and 'commands.add_parser("preview"' in runtime_cli
        and 'commands.add_parser("status"' in runtime_cli
    ):
        audit.pass_line("v8.0-B/C initialization and execution are dry-run/default-off with separate strict confirmations")
    else:
        audit.fail("v8.0-B/C production initialization or external execution gate is incomplete")

    d1_profile = (PROJECT_ROOT / "memory" / "profile_enablement.py").read_text(encoding="utf-8")
    d1_profile_init = (PROJECT_ROOT / "tools" / "init_profile_enablement_sidecar.py").read_text(encoding="utf-8")
    d1_profile_get = function_source(local_web_text, "api_profile_enablement_control")
    d1_profile_proposal = function_source(local_web_text, "api_profile_enablement_proposal")
    d1_profile_decision = function_source(local_web_text, "api_profile_enablement_decision")
    if (
        'PROTOCOL_VERSION = "v8.0-d1-profile-enablement-control-2"' in d1_profile
        and 'PROPOSAL_VERSION = "v8.0-d1-direct-user-profile-proposal-2"' in d1_profile
        and 'SIDECAR_SCHEMA_VERSION = "v8.0-d1-approved-profile-sidecar-2"' in d1_profile
        and "GateB1ProposalCapability" in d1_profile
        and "GateB2DecisionCapability" in d1_profile
        and "_consume_profile_gate_authority" in d1_profile
        and "direct_profile_proposals" in d1_profile
        and "profile_enablement_decisions" in d1_profile
        and "profile_enablement_receipts" in d1_profile
        and "subject_origin TEXT NOT NULL" in d1_profile
        and "decision_source TEXT NOT NULL CHECK(decision_source = 'manual')" in d1_profile
        and '"v7.2_candidate", "direct_user_proposal"' in d1_profile
        and "build_production_temporal_candidates" in d1_profile
        and "test profile core cannot target production" in d1_profile
        and "BEGIN IMMEDIATE" in d1_profile
        and 'item["model_visible"] = True' in d1_profile
    ):
        audit.pass_line("v8.0-D1 keeps v7.2 source-backed and adds distinct manual direct proposals on one append-only revision chain")
    else:
        audit.fail("v8.0-D1 direct proposal origin, manual decision, or append-only boundary is incomplete")

    if (
        d1_profile_get
        and "profile_control_surface_available" in d1_profile_get
        and "build_production_profile_enablement_review" in d1_profile_get
        and 'set(params) - {"q", "budget"}' in d1_profile_get
        and d1_profile_proposal
        and "profile_mutation_surface_available" in d1_profile_proposal
        and "open_production_gate_b1" in d1_profile_proposal
        and "ProfileEnablementService().propose" in d1_profile_proposal
        and '"sidecar_path"' not in d1_profile_proposal
        and '"proposed_by"' not in d1_profile_proposal
        and d1_profile_decision
        and "profile_mutation_surface_available" in d1_profile_decision
        and 'origin == "v7.2_candidate"' in d1_profile_decision
        and 'origin == "direct_user_proposal"' in d1_profile_decision
        and "open_production_gate_b2" in d1_profile_decision
        and '"sidecar_path"' not in d1_profile_decision
    ):
        audit.pass_line("v8.0-D1 API separates readonly review from fixed profile-admin proposal and origin-specific decisions")
    else:
        audit.fail("v8.0-D1 API must reject source, user, and sidecar injection outside profile-admin")

    if (
        'parser.add_argument("--execute", action="store_true"' in d1_profile_init
        and "--allow-production-profile-sidecar" in d1_profile_init
        and "--confirm-production-profile-sidecar-init" in d1_profile_init
        and "test_mode=args.test_sidecar is not None" in d1_profile_init
        and "confirmation_type_invalid" in d1_profile
        and "allow_production_profile_sidecar is True" in d1_profile
        and "confirm_production_profile_sidecar_init is True" in d1_profile
    ):
        audit.pass_line("v8.0-D1 profile sidecar remains dry-run/default-off and strictly production-gated")
    else:
        audit.fail("v8.0-D1 profile sidecar initialization gate is incomplete")

    d1_runtime = (PROJECT_ROOT / "memory" / "runtime_enablement.py").read_text(encoding="utf-8")
    d1_adapter = (PROJECT_ROOT / "memory" / "deepseek_v4_adapter.py").read_text(encoding="utf-8")
    d1_contract = (PROJECT_ROOT / "memory" / "runtime_protocol_contracts.py").read_text(encoding="utf-8")
    d1_status = (PROJECT_ROOT / "memory" / "production_runtime_status.py").read_text(encoding="utf-8")
    d1_review = (PROJECT_ROOT / "memory" / "production_runtime_review.py").read_text(encoding="utf-8")
    d1_execution = (PROJECT_ROOT / "memory" / "production_runtime_execution.py").read_text(encoding="utf-8")
    d1_runtime_init = (PROJECT_ROOT / "tools" / "init_runtime_enablement_sidecar.py").read_text(encoding="utf-8")
    d1_readiness = (PROJECT_ROOT / "tools" / "deepseek_readiness.py").read_text(encoding="utf-8")
    if (
        'RUNTIME_PROTOCOL_VERSION = "v8.0-d1-deepseek-production-runtime-1"' in d1_contract
        and 'RUNTIME_SCHEMA_VERSION = "v8.0-d1-runtime-sidecar-1"' in d1_contract
        and 'MODEL_ID = "deepseek-v4-flash"' in d1_contract
        and 'BASE_URL = "https://api.deepseek.com"' in d1_contract
        and 'RUNTIME_PROFILE_VERSION = "v8.0-d1-deepseek-production-profile-1"' in d1_contract
        and 'RENDERER_VERSION = "v8.0-d1-deepseek-wire-renderer-1"' in d1_contract
        and 'ARTIFACT_VERSION = "v8.0-d1-runtime-request-artifact-1"' in d1_contract
        and '"stream": False' in d1_contract
        and '"thinking": {"type": "disabled"}' in d1_contract
        and "from memory.runtime_protocol_contracts import" in d1_runtime
        and "from memory.runtime_protocol_contracts import" in d1_adapter
        and "build_runtime_artifact" in d1_runtime
        and "prepare_runtime_request" in d1_adapter
        and "create_production_v4_flash_runtime" in d1_runtime
        and "build_production_delivery" in d1_runtime
        and "production_profile_binding" in d1_runtime
        and "validate_trusted_delivery_standalone" in d1_runtime
        and "client_overrides_allowed" in d1_runtime
        and "test runtime primitive cannot target production" in d1_runtime
    ):
        audit.pass_line("v8.0-D1 fixes v4-flash profile, versioned artifact/renderer, exact wire bytes, and production factory dependencies")
    else:
        audit.fail("v8.0-D1 runtime profile, wire binding, or production factory is incomplete")

    if (
        "class ProductionRuntimeReviewService" in d1_review
        and "def preview(" in d1_review
        and "def execute(" not in d1_review
        and "def get_run(" not in d1_review
        and "build_production_delivery" in d1_review
        and "build_reviewed_execution_binding" in d1_review
        and "production_runtime_status()" in d1_review
        and "production_runtime_execution" not in d1_review
        and "ProductionRuntimeExecutionSession" not in d1_review
        and "DeepSeekV4FlashHTTPAdapter" not in d1_review
        and "load_production_deepseek_api_key" not in d1_review
        and "config_private" not in d1_review
        and "urlopen" not in d1_review
        and '"runtime_review": self.app.runtime_review' in local_web_text
        and '"execution_authority_present": self.app.execution_authority_present' in local_web_text
        and '"can_execute": self.app.can_execute' in local_web_text
        and "runtime_review_execution_forbidden" in local_web_text
    ):
        audit.pass_line("v8.0-H review-only Runtime is builder/status-only and server-gated without execution imports")
    else:
        audit.fail("v8.0-H runtime-review must remain authority-free, adapter-free, credential-free, and server-gated")

    if (
        "class ProductionRuntimeExecutionService" in d1_execution
        and "PRODUCTION_RUNTIME_SIDECAR" in d1_execution
        and "DeepSeekV4FlashHTTPAdapter()" not in d1_execution
        and "adapter_factory=DeepSeekV4FlashHTTPAdapter" in d1_execution
        and "adapter = self._construct_adapter()" in d1_execution
        and "with _open_validated_sidecar(self.sidecar_path, write=False)" in d1_execution
        and "with _open_validated_sidecar(self.sidecar_path, write=True)" in d1_execution
        and "build_production_delivery" in d1_execution
        and "production_profile_binding" in d1_execution
        and 'REVIEWED_EXECUTION_BINDING_VERSION = "v8.0-g-reviewed-execution-binding-1"' in d1_contract
        and "build_reviewed_execution_binding" in d1_execution
        and "validate_reviewed_execution_binding" in d1_execution
        and '"diagnostic_bindings"' in d1_execution
        and '"reviewed_execution_binding"' in d1_execution
        and "build_runtime_artifact" in d1_execution
        and "validate_runtime_artifact_binding" in d1_execution
        and "request_fingerprint" in d1_execution
        and "model_run_identity" in d1_execution
        and "build_runtime_receipt" in d1_execution
        and "validate_runtime_ledger" in d1_execution
        and "runtime_preview_binding_mismatch" in d1_execution
        and "automatic_retry" in d1_execution
        and "memory.runtime_thin_slice" not in d1_execution
        and "deepseek-chat" not in d1_execution
        and "load_production_deepseek_api_key" in d1_adapter
        and "memory.deepseek_adapter" not in d1_adapter
        and "os.environ" not in d1_adapter
    ):
        audit.pass_line("v8.0-G separates diagnostics from reviewed authority and delays the fixed D1 adapter until pure preflight passes")
    else:
        audit.fail("v8.0-G production execution must use reviewed authority, delayed fixed D1 adapter construction, fail-closed no-retry, B/C isolation, and private-file-only credentials")

    if (
        "from memory.runtime_protocol_contracts import" in d1_status
        and "validate_runtime_ledger" in d1_status
        and "mode=ro&immutable=1" in d1_status
        and "PRAGMA query_only=ON" in d1_status
        and "memory.deepseek_v4_adapter" not in d1_status
        and "memory.runtime_enablement" not in d1_status
        and "memory.runtime_thin_slice" not in d1_status
        and "urllib" not in d1_status
        and "urlopen" not in d1_status
    ):
        audit.pass_line("v8.0-F production status reader uses only the pure D1 contract and immutable readonly ledger validation")
    else:
        audit.fail("v8.0-F production status reader must remain transport-free, fixed-path, immutable, and fail-closed")

    if (
        "import sqlite3" not in d1_adapter
        and "graph_enhanced_retrieval" not in d1_adapter
        and "profile_enablement" not in d1_adapter
        and "temporal_memory_candidates" not in d1_adapter
        and "context_assembly" not in d1_adapter
        and "urlopen" in d1_adapter
        and "DeepSeekV4ProviderError" in d1_adapter
        and "PreparedDeepSeekV4Request" in d1_adapter
    ):
        audit.pass_line("v8.0-D1 DeepSeek adapter remains transport-only and accepts a full frozen artifact request")
    else:
        audit.fail("v8.0-D1 adapter must not import database, retrieval, profile, candidate, or assembly modules")

    if (
        'parser.add_argument("--execute", action="store_true"' in d1_runtime_init
        and "--allow-production-runtime-sidecar" in d1_runtime_init
        and "--confirm-production-runtime-sidecar-init" in d1_runtime_init
        and "confirmation_type_invalid" in d1_runtime
        and "allow_production_runtime_sidecar is True" in d1_runtime
        and "confirm_production_runtime_sidecar_init is True" in d1_runtime
        and "GET /models" in d1_readiness
        and "GET /user/balance" in d1_readiness
        and "--allow-official-readiness-gets" in d1_readiness
        and "--confirm-no-chat-completion" in d1_readiness
        and "deepseek_readiness_dry_run" in d1_readiness
        and "_consume_readiness_authority(authority)" in d1_runtime
        and "_run_production_readiness_transport(credential)" in d1_runtime
        and "DeepSeekReadinessHTTPAdapter" not in d1_adapter
        and "_run_test_readiness_transport" in d1_adapter
        and 'READINESS_PROTOCOL_VERSION = "v8.0-d1-deepseek-readiness-2"' in d1_adapter
    ):
        audit.pass_line("v8.0-D1 runtime init and sealed one-shot two-GET readiness remain separately authorized and default-off")
    else:
        audit.fail("v8.0-D1 runtime initialization or readiness authorization is incomplete")

    d2_fidelity = (PROJECT_ROOT / "memory" / "profile_authorization_fidelity.py").read_text(encoding="utf-8")
    d2_builder = (PROJECT_ROOT / "tools" / "build_profile_authorization_manifest.py").read_text(encoding="utf-8")
    d2_runbook = (PROJECT_ROOT / "docs" / "production_authorization_fidelity_recovery_v8_0_d2.md").read_text(encoding="utf-8")
    if (
        'CONTROL_VERSION = "v8.0-d2-authorization-fidelity-control-1"' in d2_fidelity
        and 'MANIFEST_VERSION = "v8.0-d2-profile-authorization-manifest-1"' in d2_fidelity
        and 'OVERLAY_SCHEMA_VERSION = "v8.0-d2-authorization-fidelity-overlay-1"' in d2_fidelity
        and '"batch_policy": "all_or_nothing"' in d2_fidelity
        and '"utf8_sha256": _sha256_bytes(raw)' in d2_fidelity
        and '"codepoints": [ord(char) for char in value]' in d2_fidelity
        and "runtime proposal text differs from authorized manifest" in d2_fidelity
        and "proposal read-back fidelity check failed" in d2_fidelity
        and "decision read-back fidelity check failed" in d2_fidelity
        and "BEGIN IMMEDIATE" in d2_fidelity
        and "_validate_overlay_records(conn)" in d2_fidelity
        and "test core cannot target production" in d2_fidelity
        and "Fixed production D2 executor" in d2_fidelity
    ):
        audit.pass_line("v8.0-D2 binds canonical ASCII manifests, exact UTF-8 bytes/code points, atomic batches, and append-only receipts")
    else:
        audit.fail("v8.0-D2 authorization fidelity or transaction boundary is incomplete")

    if (
        "read_bytes()" in d2_builder
        and 'decode("utf-8", errors="strict")' in d2_builder
        and 'encode("ascii", errors="strict")' in d2_builder
        and "initialize_profile_enablement_sidecar" not in d2_builder
        and "ProfileAuthorizationFidelityService" not in d2_builder
        and "R1 / Gate B2" in d2_runbook
        and "R2 / Gate B1" in d2_runbook
        and "R3 / Gate B2" in d2_runbook
        and "does not authorize or execute R1, R2, or R3" in d2_runbook
        and "Do not enter R2" in d2_runbook
        and "Do not enter R3" in d2_runbook
    ):
        audit.pass_line("v8.0-D2 manifest builder is offline-only and recovery stages require independent stop-and-authorize boundaries")
    else:
        audit.fail("v8.0-D2 offline builder or R1/R2/R3 recovery isolation is incomplete")


def check_fixed_production_authority_boundary(audit: Audit) -> None:
    print("\n## Fixed Production Authority Boundary Check")
    policy_path = PROJECT_ROOT / "memory" / "db_path_policy.py"
    policy_text = policy_path.read_text(encoding="utf-8")
    threat_model = (
        PROJECT_ROOT / "docs" / "fixed_production_authority_boundary_v8_0_d2_1.md"
    ).read_text(encoding="utf-8")
    profile_modules = tuple(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "memory/approved_profile_control.py",
            "memory/profile_enablement.py",
            "memory/profile_authorization_fidelity.py",
        )
    )
    tree = ast.parse(policy_text)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    formal_names = (
        "authorize_alias_safe_test_database_target",
        "validate_knowledge_test_db_target",
        "connect_alias_safe_test_database",
        "create_alias_safe_test_database_connection",
    )
    blocked_parameters = {"policy", "protected_targets", "production_root", "resolver"}
    formal_parameters = {
        name: {
            argument.arg
            for argument in (
                functions[name].args.posonlyargs
                + functions[name].args.args
                + functions[name].args.kwonlyargs
            )
        }
        for name in formal_names
        if name in functions
    }
    fixed_targets_source = function_source(policy_text, "_fixed_production_database_targets", indent="")
    authorize_source = function_source(
        policy_text, "authorize_alias_safe_test_database_target", indent=""
    )
    target_needles = (
        'root / "data" / "' + "ayunos" + '_chatgpt.db"',
        'root / "data" / "semantic" / "ayunos_semantic.db"',
        'root / "data" / "profile" / "approved_profile.db"',
        'root / "data" / "runtime" / "ayunos_runtime.db"',
        'root / "data" / "memory" / "ayunos_memory.db"',
        'root / "data" / "knowledge" / "ayunos_knowledge.db"',
        'root / "data" / "review" / "ayunos_review_authority.db"',
        'root / "data" / "review" / "ayunos_review_authority_registry.db"',
    )
    if (
        'FIXED_AUTHORITY_BOUNDARY_VERSION = "v8.0-d2.1-fixed-production-authority-boundary-1"'
        in policy_text
        and all(needle in fixed_targets_source for needle in target_needles)
        and len(formal_parameters) == len(formal_names)
        and all(
            blocked_parameters.isdisjoint(parameters)
            for parameters in formal_parameters.values()
        )
        and "_fixed_production_database_targets()" in authorize_source
        and "PROTECTED_PRODUCTION_DATABASE_TARGETS" not in authorize_source
        and "_PRODUCTION_TARGET_POLICY" not in policy_text
        and "_AliasSafeTargetPolicy" not in policy_text
        and "os.environ" not in policy_text
    ):
        audit.pass_line(
            "v8.0-D2.1 formal target validators use eight fixed code identities with no supported policy/root/resolver override"
        )
    else:
        audit.fail("v8.0-D2.1 fixed production target authority is incomplete")

    if all(
        "tests." not in source
        and "_PRODUCTION_TARGET_POLICY" not in source
        and "_AliasSafeTargetPolicy" not in source
        for source in profile_modules
    ):
        audit.pass_line(
            "v8.0-A, D1, and D2 profile modules do not depend on a tests-only policy seam"
        )
    else:
        audit.fail("production profile modules must not import or install tests-only policy state")

    if (
        "arbitrary monkey patching or arbitrary code execution inside the same Python" in threat_model
        and "direct database modification by another program running as the same Windows" in threat_model
        and "source or data tampering by a local administrator" in threat_model
        and "a dedicated production writer process" in threat_model
        and "a dedicated OS identity" in threat_model
        and "NTFS ACLs" in threat_model
        and "test account with read-only or no access" in threat_model
        and "unreplaceable" not in threat_model
    ):
        audit.pass_line(
            "v8.0-D2.1 documents supported-path guarantees, same-process exclusions, and v9 OS/process candidates"
        )
    else:
        audit.fail("v8.0-D2.1 threat model is incomplete or overclaims Python object authority")


def check_deterministic_sqlite_snapshot(audit: Audit) -> None:
    print("\n## Deterministic SQLite Snapshot Check")
    snapshot = (PROJECT_ROOT / "memory" / "sqlite_snapshot.py").read_text(encoding="utf-8")
    tool = (
        PROJECT_ROOT / "tools" / "profile_authorization_snapshot.py"
    ).read_text(encoding="utf-8")
    runbook = (
        PROJECT_ROOT / "docs" / "production_authorization_fidelity_recovery_v8_0_d2.md"
    ).read_text(encoding="utf-8")
    if (
        'SNAPSHOT_PROTOCOL_VERSION = "v8.0-d2.2-deterministic-sqlite-snapshot-1"'
        in snapshot
        and "def canonical_sqlite_value(" in snapshot
        and '"sqlite_type": inferred' in snapshot
        and '"utf8_base64"' in snapshot
        and '"code_points"' in snapshot
        and '"ieee754_binary64_hex"' in snapshot
        and '"base64"' in snapshot
        and '"sha256"' in snapshot
        and 'f"typeof({quoted})"' in snapshot
        and "rows.sort(key=lambda row: canonical_snapshot_bytes(row[\"order_key\"]))"
        in snapshot
        and "def verify_preexisting_rows_unchanged(" in snapshot
        and '"?mode=ro&immutable=1"' in snapshot
        and "def snapshot_production_profile_authorization_database()" in snapshot
        and "profile_admin" not in snapshot
        and "open_production_manifest_capability" not in snapshot
    ):
        audit.pass_line(
            "v8.0-D2.2 snapshots all SQLite storage classes with deterministic row/table/database identities"
        )
    else:
        audit.fail("v8.0-D2.2 deterministic SQLite snapshot encoding is incomplete")

    if (
        "snapshot_production_profile_authorization_database()" in tool
        and "argparse" not in tool
        and "db_path" not in tool
        and "snapshot_summary(snapshot)" in tool
        and "deterministic base64 plus byte length and SHA-256" in runbook
        and "verify_preexisting_rows_unchanged(before, after)" in runbook
        and "paf_b8acf79160eaabcb04902eb1ddbd1e577d5f1a59aa4970df098d0739ce6e77a0"
        in runbook
    ):
        audit.pass_line(
            "v8.0-D2.2 production snapshot preflight is fixed-path, read-only, privacy-safe, and preserves the reviewed R2 identity"
        )
    else:
        audit.fail("v8.0-D2.2 production snapshot or R2 runbook boundary is incomplete")


def check_api_safety(audit: Audit) -> None:
    print("\n## API Safety Check")
    local_web_text = (PROJECT_ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")
    web_text = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    path_leak_terms = (
        "str(self.app.db_path",
        "self.app.db_path.as_posix",
        "self.app.db_path)",
        "PROJECT_ROOT",
        "PUBLIC_PROJECTS_PATH",
        "PRIVATE_PROJECTS_PATH",
        "projects_path",
    )

    for method in ("api_status", "api_health", "api_projects", "api_project", "api_timeline"):
        source = function_source(local_web_text, method)
        if not source:
            audit.fail(f"{method} is missing")
            continue
        if any(term in source for term in path_leak_terms):
            audit.fail(f"{method} may expose a local path")
        else:
            audit.pass_line(f"{method} does not expose local path symbols")

    health_source = function_source(local_web_text, "api_health")
    if "title" in health_source or "text" in health_source:
        audit.fail("/api/health must not expose titles or message text")
    else:
        audit.pass_line("/api/health returns counts and flags only")

    if '"timeline_enabled": True' in health_source:
        audit.pass_line("/api/health reports timeline_enabled")
    else:
        audit.fail("/api/health must report timeline_enabled")

    if '"source_platforms_enabled": True' in health_source and "available_source_platforms" in health_source:
        audit.pass_line("/api/health reports source platform support")
    else:
        audit.fail("/api/health must report source platform support")

    if "def source_expr(" in local_web_text and "return \"'chatgpt'\"" in local_web_text:
        audit.pass_line("local_web.py supports source_platform fallback for legacy databases")
    else:
        audit.fail("local_web.py must fallback missing source_platform columns to chatgpt")

    if "Invalid source platform" in local_web_text and "HTTPStatus.BAD_REQUEST" in local_web_text:
        audit.pass_line("invalid source_platform returns controlled JSON 400")
    else:
        audit.fail("invalid source_platform must return controlled JSON 400")

    timeline_source = function_source(local_web_text, "api_timeline")
    if not timeline_source:
        audit.fail("/api/timeline handler is missing")
    elif '"text"' in timeline_source or '"snippet"' in timeline_source:
        audit.fail("/api/timeline must not return message text or snippets")
    else:
        audit.pass_line("/api/timeline does not return message text or snippets")

    if "self.app.load_projects()" in timeline_source and "group_by" in timeline_source and "LIMIT 1200" in timeline_source:
        audit.pass_line("/api/timeline supports grouping and project config matching")
    else:
        audit.fail("/api/timeline must support grouping and project config matching")

    if 'item["snippet"] = compact(item.get("snippet"), 240)' in local_web_text:
        audit.pass_line("/api/search returns compact snippets")
    else:
        audit.fail("/api/search must compact message snippets")

    if "return max(1, min(limit, 100))" in local_web_text:
        audit.pass_line("/api/search clamps result limit")
    else:
        audit.fail("/api/search must clamp result limit")

    if "if window < 0 or window > 10:" in local_web_text:
        audit.pass_line("/api/message-context clamps window to a maximum of 10")
    else:
        audit.fail("/api/message-context must cap window at 10")

    message_context_source = function_source(local_web_text, "api_message_context")
    if (
        'params.get("conversation_id")' in message_context_source
        and 'params.get("message_id")' in message_context_source
        and "WHERE conversation_id = ? AND message_id = ?" in message_context_source
        and "LIMIT 2" in message_context_source
        and "Ambiguous message anchor" in message_context_source
    ):
        audit.pass_line("/api/message-context uses a fail-closed composite conversation/message anchor")
    else:
        audit.fail("/api/message-context must bind conversation_id and message_id and reject ambiguity")

    do_post_source = function_source(local_web_text, "do_POST")
    if (
        "self.app.readonly" in do_post_source
        and "self.app.profile_admin" in do_post_source
        and "profile_admin_mutation" in do_post_source
        and 'parsed.path != "/api/approved-profile-decision"' not in do_post_source
        and '"/api/profile-enablement-proposal"' in do_post_source
        and '"/api/profile-enablement-decision"' in do_post_source
        and "HTTPStatus.FORBIDDEN" in do_post_source
    ):
        audit.pass_line("ordinary readonly mode rejects every POST; only explicit profile-admin can dispatch profile proposals and decisions")
    else:
        audit.fail("readonly mutation dispatch must require explicit profile-admin mode")

    if "except Exception as exc:" in local_web_text and "Unexpected server error" in local_web_text:
        audit.pass_line("API handlers return sanitized JSON for unexpected errors")
    else:
        audit.fail("API handlers must not expose unexpected exceptions to clients")

    if "traceback" in local_web_text.lower():
        audit.fail("local_web.py must not return or print tracebacks from API handlers")
    else:
        audit.pass_line("local_web.py contains no traceback exposure path")

    conversation_source = function_source(local_web_text, "api_conversation")
    if "text" in conversation_source and "打开完整对话" in web_text and "查看原文" in web_text:
        audit.pass_line("/api/conversation full text is reached through explicit original-source UI actions")
    else:
        audit.fail("/api/conversation full text access must stay explicit in the UI")


def check_frontend_safety(audit: Audit) -> None:
    print("\n## Frontend Safety Check")
    web_text = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    runtime_execution_text = (
        PROJECT_ROOT / "memory" / "production_runtime_execution.py"
    ).read_text(encoding="utf-8")

    readonly_needles = (
        "tagInput.disabled = state.readonly",
        "tagButton.disabled = state.readonly",
        "statusInput.disabled = state.readonly",
        "noteInput.disabled = state.readonly",
        "markButton.disabled = state.readonly",
        "真实库只读模式下禁止写入标签和状态",
    )
    missing_readonly = [needle for needle in readonly_needles if needle not in web_text]
    if missing_readonly:
        audit.fail("real readonly UI controls are not fully disabled")
        for needle in missing_readonly:
            print(f"  - missing: {needle}")
    else:
        audit.pass_line("real readonly UI disables tag/status write controls and shows a warning")

    if "function addTagFromCard" in web_text or "function markFromCard" in web_text:
        audit.fail("frontend contains hidden prompt-based write helpers")
    else:
        audit.pass_line("frontend contains no hidden prompt-based write helpers")

    post_count = web_text.count('method: "POST"')
    if (
        post_count == 6
        and 'fetch("/api/approved-profile-decision"' in web_text
        and 'fetch("/api/profile-enablement-proposal"' in web_text
        and 'fetch("/api/profile-enablement-decision"' in web_text
        and 'api("/api/runtime-execute"' in web_text
        and "explicit_runtime_confirmation_required" in runtime_execution_text
    ):
        audit.pass_line("frontend POST calls are limited to visible tag/status, profile-admin forms, and confirmed Runtime-admin execution")
    else:
        audit.fail(f"frontend POST call count changed unexpectedly: {post_count}")

    hardcoded_private_terms = (
        "C:" + "\\",
        "Users" + "\\YUN",
        "ayunos" + "_chatgpt" + ".db",
        "messages" + ".jsonl",
        "Wei " + "Qihang",
        "wei" + "qihang",
    )
    hits = [term for term in hardcoded_private_terms if term.lower() in web_text.lower()]
    if hits:
        audit.fail("frontend contains hardcoded private-looking terms")
        for term in hits:
            print(f"  - {term}")
    else:
        audit.pass_line("frontend contains no hardcoded private paths, usernames, or real export filenames")

    timeline_needles = (
        'view: "timeline"',
        "function renderTimeline",
        "查看该项目时间轴",
        "关键词时间轴",
        "返回时间轴",
    )
    index_text = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    timeline_text = f"{index_text}\n{web_text}"
    missing_timeline = [needle for needle in timeline_needles if needle not in timeline_text]
    if missing_timeline:
        audit.fail("frontend timeline entrypoints are incomplete")
        for needle in missing_timeline:
            print(f"  - missing: {needle}")
    else:
        audit.pass_line("frontend contains Timeline nav, filters, and return links")

    source_needles = (
        "sourcePlatformOptions",
        "search-source-platform",
        "project-source-platform",
        "timeline-source-platform",
        "sourceCountsText",
        "来源：",
    )
    missing_source = [needle for needle in source_needles if needle not in timeline_text]
    if missing_source:
        audit.fail("frontend source platform controls are incomplete")
        for needle in missing_source:
            print(f"  - missing: {needle}")
    else:
        audit.pass_line("frontend contains source platform filters and labels")


def iter_public_markdown_files():
    for target in PUBLIC_DOC_TARGETS:
        if target.is_file() and target.suffix.lower() == ".md":
            yield target
        elif target.is_dir():
            yield from sorted(path for path in target.rglob("*.md") if path.is_file())


def check_documentation_safety(audit: Audit) -> None:
    print("\n## Documentation Safety Check")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "privacy_scan.py")],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        audit.pass_line("privacy scan passed for committable public files")
    else:
        audit.fail("privacy scan failed for committable public files")
        for line in result.stdout.splitlines():
            print(f"  {line}")

    doc_hits: list[tuple[Path, int, str]] = []
    lowered_terms = [(term, term.lower()) for term in MEDIUM_RISK_DOC_TERMS]
    for path in iter_public_markdown_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        for line_number, line in enumerate(lines, start=1):
            lowered_line = line.lower()
            for term, lowered_term in lowered_terms:
                if lowered_term in lowered_line:
                    doc_hits.append((path.relative_to(PROJECT_ROOT), line_number, term))
    if doc_hits:
        audit.warn("public documentation contains medium-risk terms that need manual confirmation", code="doc_medium_risk")
        for rel_path, line_number, term in doc_hits:
            print(f"  - {rel_path}:{line_number}: {term}")
    else:
        audit.pass_line("public documentation has no medium-risk manual-review terms")


def check_project_config(audit: Audit) -> None:
    print("\n## Project Config Check")
    local_web_text = (PROJECT_ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")
    run_text = (PROJECT_ROOT / "run.ps1").read_text(encoding="utf-8")
    if PUBLIC_PROJECTS_PATH.is_file():
        audit.pass_line("config/projects_public.json exists")
    else:
        audit.fail("config/projects_public.json is missing")
        return

    try:
        projects = json.loads(PUBLIC_PROJECTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        audit.fail(f"config/projects_public.json is invalid JSON: {exc.msg}")
        return
    if isinstance(projects, list) and all(isinstance(item, dict) and item.get("name") for item in projects):
        audit.pass_line("config/projects_public.json is a valid project array")
    else:
        audit.fail("config/projects_public.json must be an array of project objects with names")

    if is_ignored("config_private/projects_private.json"):
        audit.pass_line("config_private/projects_private.json is ignored")
    else:
        audit.fail("config_private/projects_private.json must stay ignored")

    tracked_result = run_git(["ls-files"])
    tracked_files = [line.strip() for line in tracked_result.stdout.splitlines() if line.strip()]
    if "config_private/projects_private.json" in tracked_files:
        audit.fail("config_private/projects_private.json is tracked")
    else:
        audit.pass_line("config_private/projects_private.json is not tracked")

    if PRIVATE_PROJECTS_PATH.is_file():
        audit.pass_line("private project config exists locally and remains outside Git")
    else:
        audit.warn("private project config is missing for non-real audit runs", code="private_config_missing")

    if (PROJECT_ROOT / "tools" / "validate_projects.py").is_file():
        audit.pass_line("tools/validate_projects.py exists")
    else:
        audit.fail("tools/validate_projects.py is missing")

    if command_block(run_text, "validate-projects"):
        audit.pass_line("run.ps1 contains validate-projects command")
    else:
        audit.fail("run.ps1 is missing validate-projects command")

    validate_public = run_validate_projects(["--public", "--test"])
    if validate_public.returncode == 0:
        audit.pass_line("public project config validation is runnable")
    else:
        audit.fail("public project config validation failed")
    print_subprocess_output(validate_public)

    if PRIVATE_PROJECTS_PATH.is_file():
        validate_private = run_validate_projects(["--private", "--test"])
        if validate_private.returncode == 0:
            audit.pass_line("private project config validation is runnable")
        else:
            audit.fail("private project config validation failed")
        if "AYUNOS PROJECT CONFIG VALIDATION WARNING" in validate_private.stdout:
            print("NOTE: private project config validation returned warnings")
        print_subprocess_output(validate_private)

    if 'self.projects_path = PRIVATE_PROJECTS_PATH if self.db_mode == "real" else PUBLIC_PROJECTS_PATH' in local_web_text:
        audit.pass_line("real mode reads private project config and test mode reads public config")
    else:
        audit.fail("project config source must be private for real mode and public for test mode")

    if "Project config is invalid JSON" in local_web_text and "Project config must be a JSON array" in local_web_text:
        audit.pass_line("project config load errors return clear API errors")
    else:
        audit.fail("project config load failures must return clear errors")

    if "PROJECT_DEFINITIONS" not in local_web_text:
        audit.pass_line("local_web.py no longer uses hardcoded project definitions")
    else:
        audit.fail("local_web.py must not use PROJECT_DEFINITIONS")


def run_validate_projects(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "validate_projects.py"), *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def print_subprocess_output(result: subprocess.CompletedProcess[str]) -> None:
    for line in result.stdout.splitlines():
        print(f"  {line}")
    for line in result.stderr.splitlines():
        print(f"  {line}")


def main() -> int:
    args = parse_args()
    audit = Audit()
    db_path = Path(args.db).expanduser().resolve()
    print("AyunOS Project Audit")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Default audit database: {db_path}")

    check_git(audit)
    check_database(audit, db_path)
    check_real_database(audit)
    check_files(audit)
    check_default_entrypoints(audit)
    check_readonly_entrypoints(audit)
    check_fixed_production_authority_boundary(audit)
    check_deterministic_sqlite_snapshot(audit)
    check_api_safety(audit)
    check_frontend_safety(audit)
    check_project_config(audit)
    check_documentation_safety(audit)

    print("\n## Overall Result")
    if audit.failures:
        print("AYUNOS AUDIT FAILED")
        return 1
    if audit.warnings:
        print("AYUNOS AUDIT WARNING")
        if set(audit.warning_codes) == {"historical_test_markers"}:
            print("WARNING: real database contains historical test markers")
    else:
        print("AYUNOS AUDIT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
