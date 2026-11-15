from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.memory_sidecar import initialize_memory_sidecar, sidecar_initialized


class MemorySidecarTest(unittest.TestCase):
    def test_default_is_check_only_and_test_initialization_creates_schema(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sidecar.db"
            self.assertEqual("would_initialize", initialize_memory_sidecar(path)["status"])
            self.assertFalse(path.exists())
            self.assertEqual("initialized", initialize_memory_sidecar(path, execute=True)["status"])
            self.assertTrue(sidecar_initialized(path))
            self.assertEqual("already_initialized", initialize_memory_sidecar(path, execute=True)["status"])

    def test_existing_nonempty_database_is_never_overwritten(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sidecar.db"
            conn = sqlite3.connect(path); conn.execute("CREATE TABLE unrelated (value TEXT)"); conn.commit(); conn.close()
            self.assertEqual("refused", initialize_memory_sidecar(path, execute=True)["status"])
            conn = sqlite3.connect(path)
            try: self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name='unrelated'").fetchone())
            finally: conn.close()
