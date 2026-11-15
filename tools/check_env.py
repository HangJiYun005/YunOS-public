from __future__ import annotations

from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")


def main() -> int:
    checks = [
        ("import sqlite3", sqlite3 is not None),
        ("README.md exists", (PROJECT_ROOT / "README.md").is_file()),
        ("tools/privacy_scan.py exists", (PROJECT_ROOT / "tools" / "privacy_scan.py").is_file()),
        ("test memory database file exists", TEST_DB_PATH.is_file()),
        ("real memory database file exists", REAL_DB_PATH.is_file()),
        (".venv exists", (PROJECT_ROOT / ".venv").is_dir()),
    ]

    print(f"Python version: {sys.version.split()[0]}")
    print(f"Python path: {sys.executable}")
    print(f"Current working directory: {Path.cwd()}")
    print(f"Project root: {PROJECT_ROOT}")

    failures = [label for label, ok in checks if not ok]
    if failures:
        print("AYUNOS ENV CHECK WARNING")
        print("Issues:")
        for label in failures:
            print(f"- {label}")
        return 1

    print("AYUNOS ENV CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
