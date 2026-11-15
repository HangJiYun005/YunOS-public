from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory import summarizer


DEFAULT_TEST_DB = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
REAL_GATE_ERROR = "Refusing to write to the real database without --allow-real-write and --confirm-real-write."


def migrate(
    db_path: str | Path,
    allow_real_write: bool = False,
    confirm_real_write: bool = False,
    backup_dir: str | Path | None = None,
) -> dict:
    path = Path(db_path).expanduser().resolve()
    is_real_db = summarizer.is_real_db_path(path)
    result = {
        "db_path": str(path),
        "is_real_db": is_real_db,
        "backup_path": None,
        "migrated": False,
        "created_or_exists": False,
        "table_exists_after": False,
        "table": "memory_summaries",
    }
    if is_real_db:
        gate_error = summarizer.real_write_gate_error(allow_real_write, confirm_real_write)
        if gate_error:
            result["error"] = gate_error
            return result
        result["backup_path"] = str(summarizer.backup_database(path, backup_dir=backup_dir))
    elif not summarizer.is_allowed_summary_write_path(path):
        result["error"] = "v3-B only migrates memory summaries in test databases"
        return result

    with sqlite3.connect(path) as conn:
        existed = summarizer.table_exists(conn, "memory_summaries")
        summarizer.ensure_summary_schema(conn)
        conn.commit()
        result["migrated"] = not existed
        result["created_or_exists"] = True
        result["table_exists_after"] = summarizer.table_exists(conn, "memory_summaries")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create v3-C memory_summaries table with real database gates.")
    parser.add_argument("--db", default=str(DEFAULT_TEST_DB), help="Test SQLite database path.")
    parser.add_argument("--allow-real-write", action="store_true", help="Enable real database writes.")
    parser.add_argument("--confirm-real-write", action="store_true", help="Confirm real database writes.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    result = migrate(
        args.db,
        allow_real_write=args.allow_real_write,
        confirm_real_write=args.confirm_real_write,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
