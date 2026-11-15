from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_store import (
    OperationalStateStore,
)
from tools.v9_production_adoption import (
    ENGLISH_PROJECT,
    YUNOS_PROJECT,
    apply_adoption,
    finalize_release,
    reconcile_adoption,
)


T0 = "2026-07-31T02:00:00Z"
AUTHORITY_DIGEST = hashlib.sha256(b"authorized-v9-task").hexdigest()


def evidence() -> dict:
    return {
        "contract_version": "yunos-v9-production-adoption-1",
        "observed_at": T0,
        "yunos_git": {
            "branch": "feature/v9-personal-operational-state",
            "head": "3d56896" + ("0" * 33),
            "tracked_change_count": 0,
            "staged_count": 0,
            "untracked_count": 0,
            "evidence_digest": hashlib.sha256(b"yunos-git").hexdigest(),
        },
        "english_git": {
            "branch": "main",
            "head": "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53",
            "tracked_change_count": 13,
            "staged_count": 0,
            "untracked_count": 25,
            "evidence_digest": hashlib.sha256(b"english-git").hexdigest(),
        },
        "main_database": {
            "size": 76_333_056,
            "sha256": hashlib.sha256(b"main-db").hexdigest(),
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "evidence_digest": hashlib.sha256(b"database").hexdigest(),
        },
        "external_repository_modified": False,
        "full_history_backfill": False,
    }


class ProductionAdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.database = self.root / "operational.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adopt(self) -> dict:
        return apply_adoption(
            self.database,
            evidence=evidence(),
            authority_evidence_digest=AUTHORITY_DIGEST,
            created_at=T0,
            production=False,
        )

    def test_two_real_projects_scenarios_and_writer_off(self) -> None:
        report = self.adopt()
        store = OperationalStateStore(self.database)
        context = OperationalStateContextService(self.database)

        self.assertFalse(report["writer_default_enabled"])
        self.assertFalse(report["active_writer_lease"])
        self.assertEqual(report["integrity"]["integrity_check"], "ok")
        self.assertEqual(report["integrity"]["foreign_key_violations"], 0)
        self.assertFalse(report["source_repository_modified"])
        self.assertFalse(report["full_history_backfill"])
        self.assertFalse(report["future_nodes_created"])

        projects = context.project_list()
        self.assertEqual(
            {item["entity_id"] for item in projects["items"]},
            {YUNOS_PROJECT, ENGLISH_PROJECT},
        )
        now = context.now_package(budget_profile="Large")
        self.assertEqual(
            now["current_focus"][0]["entity_id"], YUNOS_PROJECT
        )
        self.assertEqual(
            store.get_projection("goal:v9-short-lived-focus-check")[
                "freshness"
            ],
            "stale",
        )

        conflict_events = store.list_events(
            entity_id="work-item:v9-e-control-surface"
        )
        self.assertEqual(len(conflict_events), 2)
        self.assertEqual(
            conflict_events[1]["event"]["supersedes_event_id"],
            conflict_events[0]["event"]["event_id"],
        )
        self.assertEqual(
            store.get_projection("work-item:v9-e-control-surface")[
                "state"
            ],
            "completed",
        )
        self.assertEqual(
            store.get_projection("work-item:v9-0")["state"],
            "completed",
        )
        self.assertEqual(
            store.get_projection("work-item:english-v011-baseline")[
                "state"
            ],
            "completed",
        )

        self.assertEqual(
            store.get_projection("work-item:v9-production-readonly-audit")[
                "state"
            ],
            "completed",
        )
        outcomes = store.list_outcomes(
            delegation_id="delegation:v9-production-readonly-audit"
        )
        self.assertEqual(outcomes[0]["status"], "accepted")
        self.assertEqual(
            store.get_projection("delegation:v9-agent-overreach")["state"],
            "waiting",
        )
        self.assertTrue(
            report["delegation"]["overreach"][
                "project_projection_unchanged"
            ]
        )
        self.assertTrue(
            all(
                report["delegation"]["overreach"][
                    "simulated_attempts"
                ].values()
            )
        )

        yunos = context.resume_package(
            YUNOS_PROJECT, budget_profile="Large"
        )
        self.assertEqual(
            yunos["completed_nodes"],
            ["v9.0", "v9-A", "v9-B", "v9-C", "v9-D", "v9-E"],
        )
        self.assertEqual(
            yunos["next_approved_step"], "Complete v9-F final seal"
        )
        self.assertEqual(yunos["pending_conflicts"], 13)
        self.assertFalse(yunos["future_nodes_authorized"])
        self.assertTrue(yunos["source_anchors"])
        self.assertNotIn(
            "goal:v9-short-lived-focus-check",
            {item["entity_id"] for item in yunos["current_work"]},
        )

        english = context.resume_package(
            ENGLISH_PROJECT, budget_profile="Large"
        )
        self.assertEqual(english["current_version"], "v0.1.1")
        self.assertEqual(
            english["authoritative_baseline"],
            "5eb4a3c493de91b5ab9da1ceee00f5451eb62d53",
        )
        self.assertTrue(english["next_candidate_steps"])
        self.assertTrue(english["blockers"])
        self.assertTrue(english["source_anchors"])

        reconciliation = reconcile_adoption(
            self.database,
            evidence=evidence(),
            authority_evidence_digest=AUTHORITY_DIGEST,
            recorded_at="2026-07-31T03:30:00Z",
            production=False,
        )
        self.assertFalse(reconciliation["active_writer_lease"])
        self.assertTrue(
            all(
                item["status"] == "already_completed"
                for item in reconciliation["reconciled"]
            )
        )

    def test_backup_restore_rebuild_and_package_digests_match(self) -> None:
        adoption = self.adopt()
        source = OperationalStateStore(self.database)
        before = OperationalStateContextService(self.database)
        backup = self.root / "operational-backup.db"
        restore = self.root / "operational-restore.db"
        source.backup_to(backup)
        restored = OperationalStateStore.restore_from_backup(backup, restore)

        before_now = before.now_package(
            budget_profile="Large", as_of="2026-07-31T03:00:00Z"
        )
        after_context = OperationalStateContextService(restore)
        after_now = after_context.now_package(
            budget_profile="Large", as_of="2026-07-31T03:00:00Z"
        )
        self.assertEqual(before_now, after_now)
        self.assertEqual(
            before.resume_package(
                YUNOS_PROJECT,
                budget_profile="Large",
                as_of="2026-07-31T03:00:00Z",
            ),
            after_context.resume_package(
                YUNOS_PROJECT,
                budget_profile="Large",
                as_of="2026-07-31T03:00:00Z",
            ),
        )
        projections_before = source.list_projections()
        restored.rebuild_projections(
            snapshot_id=adoption["snapshot"]["snapshot_id"],
            recorded_at="2026-07-31T03:01:00Z",
        )
        self.assertEqual(projections_before, restored.list_projections())

    def test_final_release_observation_requires_and_records_annotated_tag(
        self,
    ) -> None:
        self.adopt()
        repository = self.root / "repo"
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "YunOS Test"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository,
            check=True,
        )
        (repository / "README.md").write_text(
            "release\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "release fixture"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "tag",
                "-a",
                "v9.0-personal-operational-state",
                "-m",
                "YunOS v9 final seal",
            ],
            cwd=repository,
            check=True,
        )
        result = finalize_release(
            self.database,
            yunos_repository=repository,
            tag_name="v9.0-personal-operational-state",
            authority_evidence_digest=AUTHORITY_DIGEST,
            recorded_at="2026-07-31T04:00:00Z",
            production=False,
        )
        self.assertEqual(result["head"], result["peeled_target"])
        self.assertFalse(result["active_writer_lease"])
        resume = OperationalStateContextService(
            self.database
        ).resume_package(YUNOS_PROJECT, budget_profile="Large")
        self.assertEqual(
            resume["current_phase"],
            "v9-F sealed",
        )
        self.assertEqual(
            resume["current_version"],
            "v9",
        )


if __name__ == "__main__":
    unittest.main()
