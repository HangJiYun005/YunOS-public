import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import audit_project
from tools.deepseek_importer import ensure_schema


class AuditProjectTest(unittest.TestCase):
    def test_real_database_baseline_matches_post_incremental_import(self):
        self.assertEqual(408, audit_project.EXPECTED_REAL_CONVERSATIONS)
        self.assertEqual(15636, audit_project.EXPECTED_REAL_MESSAGES)

    def test_git_check_allows_tracked_gitkeep_placeholders_under_data(self):
        audit = audit_project.Audit()
        with patch.object(
            audit_project,
            "git_tracked_files",
            return_value=["data/.gitkeep", "data/test/.gitkeep", "tools/audit_project.py"],
        ):
            audit_project.check_git(audit)

        self.assertFalse(audit.failures)

    def test_real_database_count_check_allows_deepseek_import_delta(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "real.db"
            with sqlite3.connect(db_path) as conn:
                ensure_schema(conn)
                conn.executemany(
                    """
                    INSERT INTO conversations (
                        conversation_id, title, message_count, user_message_count,
                        assistant_message_count, source_platform
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            f"chatgpt-baseline-{index}",
                            "ChatGPT baseline",
                            5229 if index == 0 else 0,
                            2600 if index == 0 else 0,
                            2629 if index == 0 else 0,
                            "chatgpt",
                        )
                        for index in range(audit_project.EXPECTED_REAL_CONVERSATIONS)
                    ]
                    + [("deepseek-import", "DeepSeek import", 688, 344, 344, "deepseek")],
                )
                conn.executemany(
                    """
                    INSERT INTO messages (
                        message_id, conversation_id, conversation_title, ordinal,
                        role, text, source_platform
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            f"chatgpt-{index}",
                            "chatgpt-baseline-0",
                            "ChatGPT baseline",
                            index,
                            "assistant",
                            "baseline",
                            "chatgpt",
                        )
                        for index in range(audit_project.EXPECTED_REAL_MESSAGES)
                    ]
                    + [
                        (
                            f"deepseek-{index}",
                            "deepseek-import",
                            "DeepSeek import",
                            index,
                            "user" if index % 2 == 0 else "assistant",
                            "deepseek",
                            "deepseek",
                        )
                        for index in range(688)
                    ],
                )
                conn.commit()

            original_path = audit_project.REAL_DB_PATH
            try:
                audit_project.REAL_DB_PATH = db_path
                audit = audit_project.Audit()
                audit_project.check_real_database(audit)
            finally:
                audit_project.REAL_DB_PATH = original_path

            self.assertNotIn(
                "real database source table row counts differ from the expected readonly baseline",
                audit.failures,
            )
            self.assertFalse(audit.failures)

    def test_fixed_production_authority_boundary_audit_passes(self):
        audit = audit_project.Audit()
        audit_project.check_fixed_production_authority_boundary(audit)
        self.assertFalse(audit.failures)

    def test_deterministic_sqlite_snapshot_audit_passes(self):
        audit = audit_project.Audit()
        audit_project.check_deterministic_sqlite_snapshot(audit)
        self.assertFalse(audit.failures)

    def test_runtime_execution_readonly_entrypoint_audit_accepts_reviewed_binding_and_delayed_adapter(self):
        audit = audit_project.Audit()
        audit_project.check_readonly_entrypoints(audit)
        self.assertNotIn(
            "v8.0-G production execution must use reviewed authority, delayed fixed D1 "
            "adapter construction, fail-closed no-retry, B/C isolation, and private-file-only "
            "credentials",
            audit.failures,
        )
        self.assertNotIn(
            "v8.0-H runtime-review must remain authority-free, adapter-free, "
            "credential-free, and server-gated",
            audit.failures,
        )
        self.assertNotIn(
            "runtime-review must use the fixed real-readonly source with no execution flags",
            audit.failures,
        )


if __name__ == "__main__":
    unittest.main()
