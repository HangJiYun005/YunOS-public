from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools import v10_f_isolated_restore_web_acceptance as acceptance


class _FakeProcess:
    def __init__(
        self, pid: int = 43210, *, owned_pids=(), cleanup_overrides=None
    ) -> None:
        self.pid = pid
        self.owned_pids = {pid, *owned_pids}
        self.cleanup_overrides = dict(cleanup_overrides or {})
        self.closed = False

    def poll(self):
        return None

    def owns_pid(self, pid):
        return pid in self.owned_pids

    def close(self):
        self.closed = True
        result = {
            "pid": self.pid,
            "tree_termination_requested": True,
            "job_terminated": True,
            "job_closed": True,
            "job_close_error": 0,
            "job_query_verified": True,
            "job_active_processes_after_termination": 0,
            "process_group_termination_requested": True,
            "process_group_identity_held": True,
            "process_group_identity_lost": False,
            "process_group_sigkill_sent": False,
            "process_group_residual": False,
            "exited": True,
            "returncode": 1,
            "stdout_bytes": 0,
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stdout_truncated": False,
            "stderr_bytes": 0,
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "stderr_truncated": False,
        }
        result.update(self.cleanup_overrides)
        return result


class IsolatedRestoreWebAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="yunos-v10-f-isolated-web-test-"
        )
        self.base = Path(self.temporary.name)
        self.output = self.base / "output"
        self.output.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _restore_fixture(self, *, port: int = 18765) -> Path:
        root = self.output / f"restored-final-{port}"
        root.mkdir()
        database_results = {}
        for index, (label, relative) in enumerate(
            acceptance._FIXED_DATABASES.items(), start=1
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"sqlite-fixture-{label}-{index}".encode("ascii"))
            database_results[label] = {
                "backup_audit": {"file_gate": acceptance._file_gate(path)}
            }
        nonce = "0123456789abcdef0123456789abcdef"
        ownership_identity = acceptance._canonical_digest(
            {
                "marker_schema_version": acceptance._OWNERSHIP_MARKER_VERSION,
                "contract_version": acceptance.RECOVERY_CONTRACT_VERSION,
                "purpose": "restore",
                "nonce": nonce,
            }
        )
        package_identity = acceptance._canonical_digest(
            {
                "marker_schema_version": acceptance._OWNERSHIP_MARKER_VERSION,
                "contract_version": acceptance.RECOVERY_CONTRACT_VERSION,
                "purpose": "package",
                "nonce": "fedcba9876543210fedcba9876543210",
            }
        )
        manifest = {
            "contract_version": acceptance.RECOVERY_CONTRACT_VERSION,
            "release_name": "YunOS v10.0 — Bounded Cognitive Agency",
            "phase": "final",
            "created_at": "2026-08-02T00:00:00Z",
            "method": "sqlite_online_backup_plus_verified_asset_copy",
            "database_count": 5,
            "production_databases_opened_read_only": True,
            "production_write_performed": False,
            "database_results": database_results,
            "assets": {},
            "release_reports": {},
            "source_provenance": {},
            "service_quiescence": {},
            "schema_manifest_relative_path": "schema_manifest.json",
            "logical_summaries_relative_path": "logical_summaries.json",
            "provider_template_relative_path": "provider_configuration_template.json",
            "recovery_guide_relative_path": "RECOVERY.md",
            "artifact_inventory": [],
            "restore_rehearsal": {},
            "ownership_marker_identity_digest": package_identity,
            "backup_set_digest": "b" * 64,
            "elapsed_seconds": 0.1,
        }
        manifest["manifest_digest"] = acceptance._canonical_digest(manifest)
        self._write_json(
            root / "recovery_metadata" / "release_manifest.json", manifest
        )
        provider_template = {
            "secret_material_included": False,
            "secret_references_included": False,
            "requires_local_secret_configuration": True,
            "profiles": [],
        }
        provider_template["template_digest"] = acceptance._canonical_digest(
            provider_template
        )
        self._write_json(
            root
            / "recovery_metadata"
            / "provider_configuration_template.json",
            provider_template,
        )
        runtime = {
            "contract_version": acceptance.RECOVERY_CONTRACT_VERSION,
            "phase": "final",
            "mode": "real-readonly",
            "requested_loopback_origin": f"http://127.0.0.1:{port}",
            "production_database_connected": False,
            "write_routes_enabled": False,
            "restored_database_paths": {
                label: relative.as_posix()
                for label, relative in acceptance._FIXED_DATABASES.items()
            },
            "asset_path": "data/ingestion_assets",
            "provider_template_path": (
                "recovery_metadata/provider_configuration_template.json"
            ),
            "release_reports_path": "recovery_metadata/release_reports",
            "ready_for_isolated_readonly_start": True,
            "ownership_marker_identity_digest": ownership_identity,
            "source_service_quiescence_digest": "c" * 64,
        }
        runtime["runtime_digest"] = acceptance._canonical_digest(runtime)
        self._write_json(root / "restore_runtime.json", runtime)
        marker = {
            "marker_schema_version": acceptance._OWNERSHIP_MARKER_VERSION,
            "contract_version": acceptance.RECOVERY_CONTRACT_VERSION,
            "purpose": "restore",
            "state": "sealed",
            "nonce": nonce,
            "phase": "final",
            "manifest_digest": manifest["manifest_digest"],
            "backup_set_digest": manifest["backup_set_digest"],
            "ownership_identity_digest": ownership_identity,
            "runtime_digest": runtime["runtime_digest"],
        }
        marker["marker_digest"] = acceptance._canonical_digest(marker)
        self._write_json(root / acceptance._OWNER_MARKER, marker)
        return root

    def _contract(self, *, port: int = 18765):
        root = self._restore_fixture(port=port)
        return acceptance._validate_restore_contract(
            root, port, output_root=self.output
        )

    def _source_fixture(self) -> tuple[Path, tuple[str, ...]]:
        source = self.base / "source"
        source.mkdir()
        paths = sorted(acceptance._REQUIRED_TRACKED)
        paths.append("memory/production_runtime_status.py")
        for raw in paths:
            path = source / Path(*raw.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# tracked: {raw}\n", encoding="utf-8")
        return source, tuple(sorted(paths))

    def test_valid_restore_contract_binds_exact_five_databases(self) -> None:
        contract = self._contract()
        self.assertFalse(contract.runtime["production_database_connected"])
        self.assertEqual(contract.ownership_marker["purpose"], "restore")
        self.assertEqual(contract.ownership_marker["state"], "sealed")
        self.assertEqual(contract.ownership_marker["phase"], "final")
        self.assertEqual(
            contract.ownership_marker["ownership_identity_digest"],
            contract.runtime["ownership_marker_identity_digest"],
        )
        self.assertNotEqual(
            contract.ownership_marker["ownership_identity_digest"],
            contract.manifest["ownership_marker_identity_digest"],
        )
        self.assertEqual(set(contract.database_paths), set(acceptance._FIXED_DATABASES))
        for path in contract.database_paths.values():
            self.assertTrue(acceptance._is_within(path, contract.root))

    def test_real_restore_keeps_package_and_restore_owner_identities_distinct(self) -> None:
        root = self._restore_fixture()
        manifest_path = root / "recovery_metadata" / "release_manifest.json"
        marker_path = root / acceptance._OWNER_MARKER
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        marker = json.loads(marker_path.read_text(encoding="utf-8"))

        package_identity = "a" * 64
        self.assertNotEqual(package_identity, marker["ownership_identity_digest"])
        manifest["ownership_marker_identity_digest"] = package_identity
        manifest["manifest_digest"] = acceptance._canonical_digest(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_digest"
            }
        )
        self._write_json(manifest_path, manifest)
        marker["manifest_digest"] = manifest["manifest_digest"]
        marker["marker_digest"] = acceptance._canonical_digest(
            {
                key: value
                for key, value in marker.items()
                if key != "marker_digest"
            }
        )
        self._write_json(marker_path, marker)

        contract = acceptance._validate_restore_contract(
            root, 18765, output_root=self.output
        )
        self.assertEqual(
            package_identity,
            contract.manifest["ownership_marker_identity_digest"],
        )
        self.assertEqual(
            contract.runtime["ownership_marker_identity_digest"],
            contract.ownership_marker["ownership_identity_digest"],
        )

        manifest["ownership_marker_identity_digest"] = marker[
            "ownership_identity_digest"
        ]
        manifest["manifest_digest"] = acceptance._canonical_digest(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_digest"
            }
        )
        self._write_json(manifest_path, manifest)
        marker["manifest_digest"] = manifest["manifest_digest"]
        marker["marker_digest"] = acceptance._canonical_digest(
            {
                key: value
                for key, value in marker.items()
                if key != "marker_digest"
            }
        )
        self._write_json(marker_path, marker)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "recovery_manifest_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

    def test_production_ports_and_out_of_output_root_are_refused(self) -> None:
        for port in (0, 1023, 8765, 8766, 65536):
            with self.subTest(port=port), self.assertRaises(
                acceptance.IsolatedRestoreAcceptanceError
            ):
                acceptance._validate_port(port)
        outside = self.base / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "restore_root_must_be_below_output",
        ):
            acceptance._validate_restore_contract(
                outside, 18765, output_root=self.output
            )
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "unsafe_path_shape",
        ):
            acceptance._validate_restore_contract(
                Path("output/restored-final"),
                18765,
                output_root=self.output,
            )

    def test_restore_runtime_production_connection_tamper_is_refused(self) -> None:
        root = self._restore_fixture()
        runtime_path = root / "restore_runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["production_database_connected"] = True
        runtime["runtime_digest"] = acceptance._canonical_digest(
            acceptance._without(runtime, "runtime_digest")
        )
        self._write_json(runtime_path, runtime)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "restore_runtime_boundary_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

    def test_provider_template_secret_material_is_refused(self) -> None:
        root = self._restore_fixture()
        template = root / "recovery_metadata" / "provider_configuration_template.json"
        self._write_json(
            template,
            {
                "secret_material_included": False,
                "secret_references_included": False,
                "profiles": [{"api_key": "sk-proj-this-must-never-be-copied"}],
            },
        )
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "provider_template_secret_boundary_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

    def test_manifest_and_database_tamper_are_refused(self) -> None:
        root = self._restore_fixture()
        manifest_path = root / "recovery_metadata" / "release_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["backup_set_digest"] = "d" * 64
        self._write_json(manifest_path, manifest)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "recovery_manifest_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

        root = self._restore_fixture(port=18766)
        main = root / acceptance._FIXED_DATABASES["main"]
        main.write_bytes(b"changed-after-restore")
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "restored_database_manifest_mismatch",
        ):
            acceptance._validate_restore_contract(
                root, 18766, output_root=self.output
            )

        root = self._restore_fixture(port=18767)
        (root / acceptance._OWNER_MARKER).unlink()
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "required_path_missing",
        ):
            acceptance._validate_restore_contract(
                root, 18767, output_root=self.output
            )

        root = self._restore_fixture(port=18768)
        marker_path = root / acceptance._OWNER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["unexpected"] = False
        marker["marker_digest"] = acceptance._canonical_digest(
            acceptance._without(marker, "marker_digest")
        )
        self._write_json(marker_path, marker)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "ownership_marker_shape_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18768, output_root=self.output
            )

        root = self._restore_fixture(port=18769)
        marker_path = root / acceptance._OWNER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["marker_digest"] = "0" * 64
        self._write_json(marker_path, marker)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "ownership_marker_invalid",
        ):
            acceptance._validate_restore_contract(
                root, 18769, output_root=self.output
            )

        root = self._restore_fixture(port=18770)
        marker_path = root / acceptance._OWNER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["nonce"] = "sk-proj-secret-material-forbidden"
        marker["marker_digest"] = acceptance._canonical_digest(
            acceptance._without(marker, "marker_digest")
        )
        self._write_json(marker_path, marker)
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "ownership_marker_secret_detected",
        ):
            acceptance._validate_restore_contract(
                root, 18770, output_root=self.output
            )

        for offset, field in enumerate(
            ("manifest_digest", "backup_set_digest", "runtime_digest"),
            start=18771,
        ):
            root = self._restore_fixture(port=offset)
            marker_path = root / acceptance._OWNER_MARKER
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker[field] = "e" * 64
            marker["marker_digest"] = acceptance._canonical_digest(
                acceptance._without(marker, "marker_digest")
            )
            self._write_json(marker_path, marker)
            with self.subTest(field=field), self.assertRaisesRegex(
                acceptance.IsolatedRestoreAcceptanceError,
                "ownership_marker_binding_mismatch",
            ):
                acceptance._validate_restore_contract(
                    root, offset, output_root=self.output
                )

    def test_symlinked_restore_marker_is_refused_when_supported(self) -> None:
        root = self._restore_fixture()
        target = root / acceptance._OWNER_MARKER
        holding = root / "real-owner-marker.json"
        target.replace(holding)
        try:
            target.symlink_to(holding)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable in this environment")
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "reparse_path_forbidden",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

    def test_hardlinked_restore_marker_is_refused_when_supported(self) -> None:
        root = self._restore_fixture()
        target = root / acceptance._OWNER_MARKER
        holding = root / "hardlink-owner-marker.json"
        target.replace(holding)
        try:
            target.hardlink_to(holding)
        except (OSError, NotImplementedError):
            self.skipTest("hardlink creation is unavailable in this environment")
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "hardlink_path_forbidden",
        ):
            acceptance._validate_restore_contract(
                root, 18765, output_root=self.output
            )

    def test_clean_head_snapshot_copies_only_allowlisted_tracked_files(self) -> None:
        contract = self._contract()
        source, paths = self._source_fixture()
        clean = acceptance.GitState(head="a" * 40, status=b"", tracked_paths=paths)
        result = acceptance._copy_clean_head_snapshot(
            contract,
            source_root=source,
            state_provider=lambda _root: clean,
        )
        self.assertEqual(result["source_binding"], "fixed_clean_head")
        self.assertEqual(result["file_count"], len(paths))
        acceptance._verify_copied_snapshot(contract, result)
        self.assertTrue((contract.root / "tools" / "local_web.py").is_file())
        self.assertFalse((contract.root / "data" / ".env").exists())
        self.assertFalse(acceptance._allowed_tracked_path("config_private/secret.json"))
        self.assertFalse(acceptance._allowed_tracked_path(".env"))
        (contract.root / "tools" / "local_web.py").write_text(
            "# changed\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "runtime_snapshot_changed",
        ):
            acceptance._verify_copied_snapshot(contract, result)

    def test_dirty_git_and_source_drift_fail_closed(self) -> None:
        contract = self._contract()
        source, paths = self._source_fixture()
        dirty = acceptance.GitState(
            head="a" * 40, status=b" M tools/local_web.py\0", tracked_paths=paths
        )
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "source_worktree_not_clean",
        ):
            acceptance._copy_clean_head_snapshot(
                contract,
                source_root=source,
                state_provider=lambda _root: dirty,
            )

        contract = acceptance._validate_restore_contract(
            self._restore_fixture(port=18767), 18767, output_root=self.output
        )
        states = iter(
            (
                acceptance.GitState("a" * 40, b"", paths),
                acceptance.GitState("b" * 40, b"", paths),
            )
        )
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "runtime_source_drift_detected",
        ):
            acceptance._copy_clean_head_snapshot(
                contract,
                source_root=source,
                state_provider=lambda _root: next(states),
            )

    def _prepare_fake_live_tree(self, contract) -> None:
        for relative in (
            Path("tools/local_web.py"),
            Path("memory/production_runtime_status.py"),
            Path("web/index.html"),
        ):
            path = contract.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")

    def _probe(self, contract) -> dict:
        bindings = {
            "project_root": str(contract.root),
            "web_root": str(contract.root / "web"),
            "main": str(contract.database_paths["main"]),
            "default_main": str(contract.database_paths["main"]),
            "profile": str(contract.database_paths["profile"]),
            "semantic": str(contract.database_paths["semantic"]),
            "runtime": str(contract.database_paths["runtime"]),
            "operational": str(contract.database_paths["operational"]),
            "local_web_module": str(contract.root / "tools" / "local_web.py"),
            "runtime_status_module": str(
                contract.root / "memory" / "production_runtime_status.py"
            ),
        }
        search = {
            "items": [
                {
                    "conversation_id": "fixture",
                    "match_count": 1,
                    "other_match_count": 0,
                    "best_hit": {
                        "conversation_id": "fixture",
                        "message_id": "message-fixture",
                    },
                    "other_hits": [],
                }
            ],
            "total_conversations": 1,
            "has_more": False,
            "next_offset": None,
            "sort": "relevance",
            "limit": 5,
            "offset": 0,
        }
        return {
            "bindings": bindings,
            "query": "YunOS",
            "query_sha256": hashlib.sha256(b"YunOS").hexdigest(),
            "expected_search_digest": acceptance._canonical_digest(
                acceptance._search_identity(search)
            ),
            "expected_search_count": 1,
            "operational_argument": str(contract.database_paths["operational"]),
            "local_module_count": 20,
            "local_module_digest": "d" * 64,
        }

    @staticmethod
    def _api(payload: dict, status: int = 200) -> acceptance.HttpResult:
        return acceptance.HttpResult(
            status=status,
            headers={
                "cache-control": "no-store",
                "content-type": "application/json; charset=utf-8",
            },
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _fake_http(self, contract, process):
        cognitive = {
            "readonly": True,
            "cache_control": "no-store",
            "items": [{}],
            "count": 1,
        }

        def call(origin, path, *, method="GET", body=None):
            self.assertEqual(origin, "http://127.0.0.1:18765")
            if method == "POST":
                return self._api(
                    {"error": "Readonly mode: writes are disabled"}, status=403
                )
            if path == "/api/status":
                return self._api(
                    {
                        "db_mode": "real",
                        "service_mode": "real-readonly",
                        "readonly": True,
                        "runtime_admin": False,
                        "profile_admin": False,
                        "execution_authority_present": False,
                        "can_execute": False,
                        "bound_host": "127.0.0.1",
                        "bound_port": 18765,
                        "process_id": process.pid,
                        "runtime_sidecar": {
                            "status": "ready",
                            "ready": True,
                            "readonly": True,
                            "execution": {"credential_read": False},
                            "sidecar": {
                                "path": str(contract.database_paths["runtime"])
                            }
                        },
                    }
                )
            if path.startswith("/api/search?"):
                return self._api(
                    {
                        "items": [
                            {
                                "conversation_id": "fixture",
                                "match_count": 1,
                                "other_match_count": 0,
                                "best_hit": {
                                    "conversation_id": "fixture",
                                    "message_id": "message-fixture",
                                    "role_label": "用户",
                                },
                                "other_hits": [],
                            }
                        ],
                        "total_conversations": 1,
                        "has_more": False,
                        "next_offset": None,
                        "sort": "relevance",
                        "limit": 5,
                        "offset": 0,
                    }
                )
            if path.startswith("/api/cognitive/runs"):
                return self._api(
                    {**cognitive, "items": [{"run_id": "run:fixture"}]}
                )
            if path.startswith("/api/cognitive/objectives") or path.startswith(
                "/api/cognitive/plans"
            ):
                return self._api(cognitive)
            if path.startswith("/api/cognitive/workspace?"):
                surface = "mobile" if "surface=mobile" in path else "desktop"
                return self._api(
                    {
                        "readonly": True,
                        "cache_control": "no-store",
                        "surface": surface,
                        "boundaries": {
                            "readonly": True,
                            "write_api_available": False,
                            "mobile_write_allowed": False,
                            "tailnet_write_allowed": False,
                            "acceptance_write_allowed": False,
                            "authority_expansion_allowed": False,
                        },
                    }
                )
            static = {
                "/": "认知任务工作区",
                "/app.js": "/api/cognitive/workspace",
                "/service-worker.js": "const CACHE = 'fixture';",
                "/manifest.webmanifest": '{"name":"YunOS"}',
            }
            if path in static:
                return acceptance.HttpResult(
                    status=200,
                    headers={"content-type": "text/plain; charset=utf-8"},
                    body=static[path].encode("utf-8"),
                )
            raise AssertionError(path)

        return call

    def test_server_acceptance_cleans_owned_process_and_preserves_five_databases(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        process = _FakeProcess()
        report = acceptance._exercise_server(
            contract,
            18765,
            Path("fixture-python"),
            probe_runner=lambda _contract, _python: self._probe(contract),
            launcher=lambda _contract, _port, _python: process,
            http=self._fake_http(contract, process),
            startup_seconds=0.1,
        )
        self.assertTrue(process.closed)
        self.assertTrue(report["database_content_unchanged"])
        self.assertTrue(report["process_cleanup"]["exited"])
        self.assertTrue(
            report["responses"]["search"]["matches_isolated_direct_query"]
        )

    def test_http_failure_still_cleans_owned_process(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        process = _FakeProcess()

        def refused(*_args, **_kwargs):
            raise acceptance.IsolatedRestoreAcceptanceError("fixture_http_failure")

        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "isolated_web_start_timeout",
        ):
            acceptance._exercise_server(
                contract,
                18765,
                Path("fixture-python"),
                probe_runner=lambda _contract, _python: self._probe(contract),
                launcher=lambda _contract, _port, _python: process,
                http=refused,
                startup_seconds=0.01,
            )
        self.assertTrue(process.closed)

    def test_http_failure_cannot_hide_invalid_cleanup_receipt(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        overrides = (
            {"job_query_verified": False}
            if os.name == "nt"
            else {
                "process_group_identity_held": False,
                "process_group_identity_lost": True,
                "process_group_residual": True,
            }
        )
        process = _FakeProcess(cleanup_overrides=overrides)

        def refused(*_args, **_kwargs):
            raise acceptance.IsolatedRestoreAcceptanceError("fixture_http_failure")

        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "isolated_cleanup_unverified_after_failure",
        ):
            acceptance._exercise_server(
                contract,
                18765,
                Path("fixture-python"),
                probe_runner=lambda _contract, _python: self._probe(contract),
                launcher=lambda _contract, _port, _python: process,
                http=refused,
                startup_seconds=0.01,
            )
        self.assertTrue(process.closed)

    def test_status_pid_outside_owned_process_is_refused(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        process = _FakeProcess()
        normal_http = self._fake_http(contract, process)

        def unrelated_pid(origin, path, *, method="GET", body=None):
            result = normal_http(origin, path, method=method, body=body)
            if path != "/api/status":
                return result
            payload = json.loads(result.body.decode("utf-8"))
            payload["process_id"] = process.pid + 1
            return self._api(payload)

        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "isolated_status_boundary_invalid",
        ):
            acceptance._exercise_server(
                contract,
                18765,
                Path("fixture-python"),
                probe_runner=lambda _contract, _python: self._probe(contract),
                launcher=lambda _contract, _port, _python: process,
                http=unrelated_pid,
                startup_seconds=0.1,
            )
        self.assertTrue(process.closed)

    def test_owned_child_status_pid_is_accepted_and_recorded(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        child_pid = 43211
        process = _FakeProcess(owned_pids=(child_pid,))
        normal_http = self._fake_http(contract, process)

        def child_status(origin, path, *, method="GET", body=None):
            result = normal_http(origin, path, method=method, body=body)
            if path != "/api/status":
                return result
            payload = json.loads(result.body.decode("utf-8"))
            payload["process_id"] = child_pid
            return self._api(payload)

        report = acceptance._exercise_server(
            contract,
            18765,
            Path("fixture-python"),
            probe_runner=lambda _contract, _python: self._probe(contract),
            launcher=lambda _contract, _port, _python: process,
            http=child_status,
            startup_seconds=0.1,
        )
        self.assertTrue(process.closed)
        self.assertEqual(report["launch_boundary"]["launcher_pid"], process.pid)
        self.assertEqual(report["launch_boundary"]["service_pid"], child_pid)
        self.assertTrue(report["launch_boundary"]["service_pid_owned"])
        self.assertFalse(report["launch_boundary"]["service_pid_matches_launcher"])
        self.assertIn(
            report["launch_boundary"]["ownership_method"],
            {"windows_job_object", "posix_process_group"},
        )

    def test_owned_process_wrapper_terminates_real_process(self) -> None:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
        owned = None
        try:
            job = acceptance._windows_job_for(process) if os.name == "nt" else None
            owned = acceptance.OwnedServerProcess(process, job)
            self.assertTrue(owned.owns_pid(process.pid))
            self.assertFalse(owned.owns_pid(os.getpid()))
            self.assertFalse(owned.owns_pid(True))
            self.assertFalse(owned.owns_pid(0))
            self.assertFalse(owned.owns_pid(-1))
            self.assertFalse(owned.owns_pid("1"))
            self.assertFalse(owned.owns_pid(process.pid + (1 << 32)))
            result = owned.close()
            self.assertTrue(result["exited"])
            self.assertIsNotNone(process.poll())
            self.assertFalse(owned.owns_pid(process.pid))
            if os.name == "nt":
                self.assertTrue(result["job_terminated"])
                self.assertTrue(result["job_query_verified"])
                self.assertEqual(result["job_active_processes_after_termination"], 0)
                self.assertTrue(result["job_closed"])
            else:
                self.assertTrue(result["process_group_termination_requested"])
                self.assertFalse(result["process_group_residual"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object test")
    def test_suspended_job_owns_real_descendant_and_leaves_no_residual(self) -> None:
        import ctypes
        from ctypes import wintypes

        marker = self.base / "owned-processes.json"
        code = (
            "import json, os, pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'service_pid': os.getpid(), 'child_pid': child.pid}), "
            "encoding='utf-8')\n"
            "time.sleep(30)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004
            ),
        )
        owned = None
        child_pid = None
        try:
            job = acceptance._windows_job_for(process)
            owned = acceptance.OwnedServerProcess(process, job)
            acceptance._windows_resume_suspended_process(process)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            pids = json.loads(marker.read_text(encoding="utf-8"))
            service_pid = pids["service_pid"]
            child_pid = pids["child_pid"]
            self.assertNotEqual(service_pid, process.pid)
            self.assertTrue(owned.owns_pid(process.pid))
            self.assertTrue(owned.owns_pid(service_pid))
            self.assertTrue(owned.owns_pid(child_pid))
            self.assertFalse(owned.owns_pid(os.getpid()))
            self.assertFalse(owned.owns_pid(service_pid + (1 << 32)))
            result = owned.close()
            self.assertTrue(result["job_terminated"])
            self.assertTrue(result["job_query_verified"])
            self.assertEqual(result["job_active_processes_after_termination"], 0)
            self.assertTrue(result["job_closed"])
            owned = None

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            def is_running(pid):
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                try:
                    exit_code = wintypes.DWORD(0)
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return int(exit_code.value) == 259
                finally:
                    self.assertTrue(kernel32.CloseHandle(handle))

            deadline = time.monotonic() + 5
            while is_running(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(is_running(child_pid))
        finally:
            if owned is not None:
                owned.close()
            elif process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows launch cleanup test")
    def test_resume_failure_cannot_hide_invalid_launch_cleanup(self) -> None:
        contract = self._contract()
        fake_raw_process = mock.Mock()
        fake_raw_process.pid = 43210
        with (
            mock.patch.object(
                acceptance.subprocess, "Popen", return_value=fake_raw_process
            ),
            mock.patch.object(acceptance, "_windows_job_for", return_value=12345),
            mock.patch.object(
                acceptance,
                "_windows_resume_suspended_process",
                side_effect=acceptance.IsolatedRestoreAcceptanceError(
                    "fixture_resume_failure"
                ),
            ),
            mock.patch.object(
                acceptance,
                "_close_and_validate_owned_process",
                side_effect=acceptance.IsolatedRestoreAcceptanceError(
                    "fixture_cleanup_failure"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                acceptance.IsolatedRestoreAcceptanceError,
                "isolated_launch_cleanup_unverified",
            ):
                acceptance._launch_server(contract, 18765, Path("fixture-python"))

    @unittest.skipUnless(os.name == "nt", "Windows launch cleanup test")
    def test_job_setup_failure_classifies_suspended_cleanup_failure(self) -> None:
        contract = self._contract()
        fake_raw_process = mock.Mock()
        fake_raw_process.pid = 43210
        fake_raw_process.poll.return_value = None
        fake_raw_process.terminate.side_effect = OSError("fixture terminate failure")
        with (
            mock.patch.object(
                acceptance.subprocess, "Popen", return_value=fake_raw_process
            ),
            mock.patch.object(
                acceptance,
                "_windows_job_for",
                side_effect=acceptance.IsolatedRestoreAcceptanceError(
                    "fixture_job_setup_failure"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                acceptance.IsolatedRestoreAcceptanceError,
                "isolated_suspended_launcher_cleanup_unverified",
            ):
                acceptance._launch_server(contract, 18765, Path("fixture-python"))

    @unittest.skipIf(os.name == "nt", "POSIX process-group test")
    def test_close_fails_closed_when_posix_group_identity_is_lost(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        owned = acceptance.OwnedServerProcess(process, None)
        process.communicate(timeout=10)
        self.assertIsNotNone(process.poll())
        result = owned.close()
        self.assertFalse(result["process_group_identity_held"])
        self.assertTrue(result["process_group_identity_lost"])
        self.assertFalse(result["process_group_termination_requested"])
        self.assertTrue(result["process_group_residual"])
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "owned_process_group_cleanup_unverified",
        ):
            acceptance._validate_process_cleanup(result)

    @unittest.skipIf(os.name == "nt", "POSIX process-group test")
    def test_posix_broker_holds_identity_through_child_termination(self) -> None:
        marker = self.base / "posix-broker-child-started.txt"
        term_marker = self.base / "posix-broker-child-terminated.txt"
        child_code = (
            "import pathlib, signal, sys, time\n"
            "started = pathlib.Path(sys.argv[1])\n"
            "terminated = pathlib.Path(sys.argv[2])\n"
            "def handle_term(_signum, _frame):\n"
            "    terminated.write_text('term', encoding='utf-8')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, handle_term)\n"
            "started.write_text('started', encoding='utf-8')\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        command = [
            sys.executable,
            "-I",
            "-c",
            acceptance._POSIX_SERVER_BROKER,
            sys.executable,
            "-c",
            child_code,
            str(marker),
            str(term_marker),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        owned = acceptance.OwnedServerProcess(process, None)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            self.assertIsNone(process.poll())
            result = owned.close()
            acceptance._validate_process_cleanup(result)
            self.assertTrue(term_marker.exists())
            self.assertTrue(result["process_group_identity_held"])
            self.assertFalse(result["process_group_identity_lost"])
            self.assertTrue(result["process_group_sigkill_sent"])
            self.assertFalse(result["process_group_residual"])
        finally:
            if process.poll() is None:
                owned.close()

    def test_path_probe_rejects_any_production_binding(self) -> None:
        contract = self._contract()
        self._prepare_fake_live_tree(contract)
        probe = self._probe(contract)
        probe["bindings"]["runtime"] = str(
            acceptance.SOURCE_ROOT / "data" / "runtime" / "ayunos_runtime.db"
        )
        with self.assertRaises(
            acceptance.IsolatedRestoreAcceptanceError
        ):
            acceptance._validate_probe(contract, probe)

    def test_evidence_is_one_shot_and_canonical_digest_is_stable(self) -> None:
        contract = self._contract()
        report = {"status": "accepted", "production_database_connected": False}
        report["evidence_digest"] = acceptance._canonical_digest(report)
        path, digest = acceptance._write_evidence(contract.root, report)
        self.assertTrue(path.is_file())
        self.assertEqual(digest, acceptance._hash_file(path))
        with self.assertRaisesRegex(
            acceptance.IsolatedRestoreAcceptanceError,
            "isolated_acceptance_already_recorded",
        ):
            acceptance._write_evidence(contract.root, report)


if __name__ == "__main__":
    unittest.main()
