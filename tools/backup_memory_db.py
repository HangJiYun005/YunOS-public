from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
DEFAULT_OUT_DIR = PROJECT_ROOT / "backups"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy the local memory database to a timestamped backup file.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path to back up.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Directory for backup files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if not db_path.is_file():
        raise ValueError(f"Database path is not a file: {db_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}_{timestamp}{db_path.suffix}"
    backup_path = out_dir / backup_name

    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")
    print(f"Source left unchanged: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
