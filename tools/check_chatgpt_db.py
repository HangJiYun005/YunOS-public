from pathlib import Path
import argparse
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "chatgpt_export" / "chatgpt_memory.sqlite"


def scalar(cur, sql):
    return cur.execute(sql).fetchone()[0]


def parse_args():
    parser = argparse.ArgumentParser(description="Read-only check for a local ChatGPT memory database.")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the local SQLite database to inspect.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    db_uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        cur = conn.cursor()

        conversations_count = scalar(cur, "SELECT COUNT(*) FROM conversations")
        messages_count = scalar(cur, "SELECT COUNT(*) FROM messages")

        print(f"Database: {db_path}")
        print(f"conversations count: {conversations_count}")
        print(f"messages count: {messages_count}")
        print()
        print("Recent 10 conversations:")
        print("title\tupdate_time_iso\tmessage_count")

        rows = cur.execute(
            """
            SELECT
                COALESCE(NULLIF(title, ''), '(untitled)') AS title,
                COALESCE(update_time_iso, '') AS update_time_iso,
                COALESCE(message_count, 0) AS message_count
            FROM conversations
            ORDER BY update_time DESC
            LIMIT 10
            """
        ).fetchall()

        for title, update_time_iso, message_count in rows:
            print(f"{title}\t{update_time_iso}\t{message_count}")


if __name__ == "__main__":
    main()
