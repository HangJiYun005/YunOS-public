from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.cognitive_state_store import CognitiveStateStore
from memory.operational_state_models import OperationalStateError
from memory.operational_state_store import (
    PRODUCTION_WRITE_ENV,
    OperationalStateStore,
    initialize_operational_database,
)


class V10FProductionStoreGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v10f-store-gate-")
        self.database = Path(self.temp.name) / "operational.db"
        initialize_operational_database(
            self.database,
            created_at="2026-08-01T00:00:00.000000Z",
        )
        apply_cognitive_state_migration(
            self.database,
            created_at="2026-08-01T00:01:00.000000Z",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_operational_store_requires_exact_true_and_environment_gate(self) -> None:
        with patch(
            "memory.operational_state_store.is_production_operational_path",
            return_value=True,
        ):
            default = OperationalStateStore(self.database)
            numeric = OperationalStateStore(
                self.database, allow_production_write=1
            )
            explicit = OperationalStateStore(
                self.database, allow_production_write=True
            )
            with patch.dict(os.environ, {PRODUCTION_WRITE_ENV: "1"}):
                for store in (default, numeric):
                    with self.subTest(store=store.allow_production_write):
                        with self.assertRaisesRegex(
                            OperationalStateError, "production_writer_disabled"
                        ):
                            store._connect(write=True)
                with closing(explicit._connect(write=True)) as connection:
                    self.assertEqual(
                        9001,
                        connection.execute(
                            "SELECT schema_version FROM operational_schema"
                        ).fetchone()[0],
                    )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    OperationalStateError, "production_writer_disabled"
                ):
                    explicit._connect(write=True)

    def test_all_cognitive_stores_propagate_one_explicit_flag(self) -> None:
        constructors = (
            CognitiveStateStore,
            CognitivePlanningStore,
            CognitiveProviderStore,
            CognitiveExecutionStore,
        )
        with patch(
            "memory.operational_state_store.is_production_operational_path",
            return_value=True,
        ):
            for constructor in constructors:
                with self.subTest(store=constructor.__name__):
                    default = constructor(self.database)
                    enabled = constructor(
                        self.database, allow_production_write=True
                    )
                    if isinstance(default, CognitiveExecutionStore):
                        default_operational = default.operational
                        enabled_operational = enabled.operational
                    else:
                        default_operational = default.operational_store
                        enabled_operational = enabled.operational_store
                    self.assertFalse(default_operational.allow_production_write)
                    self.assertTrue(enabled_operational.allow_production_write)
                    if isinstance(enabled, CognitivePlanningStore):
                        self.assertTrue(
                            enabled.objective_store.operational_store.allow_production_write
                        )


if __name__ == "__main__":
    unittest.main()
