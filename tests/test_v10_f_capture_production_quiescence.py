from __future__ import annotations

import json
import io
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from memory.cognitive_production_migration import (
    COGNITIVE_CAPTURE_ARTIFACT_VERSION,
    COGNITIVE_CAPTURE_CONTROLLER_DIGEST,
    COGNITIVE_CAPTURE_ISSUER_ID,
    COGNITIVE_CAPTURE_ISSUER_VERSION,
    COGNITIVE_CAPTURE_TAILSCALE_COMMANDS,
    COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256,
)
from memory.operational_state_models import canonical_digest
from memory.secure_control_files import pinned_control_directory
from tools.v10_f_capture_production_quiescence import (
    CAPTURE_ROOT,
    EXPECTED_PROXY,
    QuiescenceCaptureError,
    _FIXED_PRODUCTION_CAPTURE_EXECUTOR,
    _run_boundary_after_exact_source_match,
    build_parser,
    capture_fixed_claim_boundary_and_source_identities,
    capture_fixed_production_quiescence,
    capture_production_quiescence,
    capture_tailscale_boundary,
    main,
    write_capture,
)
from tools.v10_f_prepare_production_migration import (
    PRODUCTION_MIGRATION_CONTROL_ROOT,
    build_service_quiescence_evidence,
)


class V10FCaptureProductionQuiescenceTest(unittest.TestCase):
    @staticmethod
    def evidence():
        return build_service_quiescence_evidence(
            captured_at="2026-08-01T01:00:00.000000Z",
            tailscale_serve_config_digest="d" * 64,
            local_service_listener_count=0,
            writer_lease_count=0,
            operational_wal_size=0,
            production_write_env_enabled=False,
            tailscale_backend="Running",
            funnel_enabled=False,
        )

    @staticmethod
    def tailscale(arguments: tuple[str, ...]):
        if arguments == ("status", "--json"):
            return {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "node.example."},
            }
        return {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {"node.example:443": {"Handlers": {"/": {"Proxy": EXPECTED_PROXY}}}},
        }

    def test_capture_binds_live_gates_and_sanitized_tailscale_digest(self) -> None:
        with (
            patch(
                "tools.v10_f_capture_production_quiescence._run_tailscale_json",
                side_effect=self.tailscale,
            ),
            patch(
                "tools.v10_f_capture_production_quiescence._capture_live_quiescence",
                return_value={
                    "local_service_port": 8765,
                    "local_service_listener_count": 0,
                    "writer_lease_count": 0,
                    "operational_wal_size": 0,
                    "production_write_env_enabled": False,
                },
            ),
            patch(
                "tools.v10_f_capture_production_quiescence.utc_now",
                return_value="2026-08-01T01:00:00.000000Z",
            ),
        ):
            evidence = capture_production_quiescence()
        self.assertEqual("Running", evidence["tailscale_backend"])
        self.assertTrue(evidence["local_service_stopped"])
        self.assertFalse(evidence["funnel_enabled"])
        self.assertEqual(64, len(evidence["tailscale_serve_config_digest"]))
        self.assertNotIn("node.example", json.dumps(evidence))

    def test_tailscale_1_98_9_matching_tailnet_only_funnel_json_is_closed(
        self,
    ) -> None:
        with patch(
            "tools.v10_f_capture_production_quiescence._run_tailscale_json",
            side_effect=self.tailscale,
        ) as run_tailscale:
            boundary = capture_tailscale_boundary()
        self.assertEqual("Running", boundary["tailscale_backend"])
        self.assertFalse(boundary["funnel_enabled"])
        self.assertEqual(64, len(boundary["tailscale_serve_config_digest"]))
        self.assertEqual(
            list(COGNITIVE_CAPTURE_TAILSCALE_COMMANDS),
            [call.args[0] for call in run_tailscale.call_args_list],
        )

    def test_capture_controller_digest_binds_ordered_aba_command_sequence(
        self,
    ) -> None:
        expected_commands = (
            ("status", "--json"),
            ("serve", "status", "--json"),
            ("funnel", "status", "--json"),
            ("serve", "status", "--json"),
        )
        self.assertEqual(
            expected_commands,
            COGNITIVE_CAPTURE_TAILSCALE_COMMANDS,
        )
        self.assertEqual(
            canonical_digest(
                {
                    "artifact_version": COGNITIVE_CAPTURE_ARTIFACT_VERSION,
                    "issuer_id": COGNITIVE_CAPTURE_ISSUER_ID,
                    "issuer_version": COGNITIVE_CAPTURE_ISSUER_VERSION,
                    "operational_database": (
                        "data/operational/ayunos_operational_state.db"
                    ),
                    "tailscale_commands": [
                        list(arguments) for arguments in expected_commands
                    ],
                    "user_authorization_text_digest": (
                        COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
                    ),
                }
            ),
            COGNITIVE_CAPTURE_CONTROLLER_DIGEST,
        )

    def test_ordinary_capture_unpacks_the_shared_command_source(self) -> None:
        synthetic_commands = (
            ("probe-status",),
            ("probe-serve-before",),
            ("probe-funnel",),
            ("probe-serve-after",),
        )
        serve = self.tailscale(("serve", "status", "--json"))
        responses = (
            {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "node.example."},
            },
            serve,
            serve,
            serve,
        )
        with (
            patch(
                "tools.v10_f_capture_production_quiescence."
                "COGNITIVE_CAPTURE_TAILSCALE_COMMANDS",
                synthetic_commands,
            ),
            patch(
                "tools.v10_f_capture_production_quiescence."
                "_run_tailscale_json",
                side_effect=responses,
            ) as run_tailscale,
        ):
            boundary = capture_tailscale_boundary()
        self.assertFalse(boundary["funnel_enabled"])
        self.assertEqual(
            list(synthetic_commands),
            [call.args[0] for call in run_tailscale.call_args_list],
        )

    def test_general_status_public_keys_are_identity_not_funnel_authority(
        self,
    ) -> None:
        def response(arguments: tuple[str, ...]):
            if arguments == ("status", "--json"):
                return {
                    "BackendState": "Running",
                    "Self": {
                        "Online": True,
                        "DNSName": "node.example.",
                        "PublicKey": "nodekey:self",
                        "Capabilities": [
                            "https://tailscale.com/cap/funnel"
                        ],
                        "CapMap": {
                            "https://tailscale.com/cap/funnel": None
                        },
                    },
                    "Peer": {
                        "nodekey:peer": {"PublicKey": "nodekey:peer"}
                    },
                }
            return self.tailscale(arguments)

        with patch(
            "tools.v10_f_capture_production_quiescence._run_tailscale_json",
            side_effect=response,
        ):
            boundary = capture_tailscale_boundary()
        self.assertFalse(boundary["funnel_enabled"])

    def test_general_status_numeric_online_fails_closed(self) -> None:
        def response(arguments: tuple[str, ...]):
            if arguments == ("status", "--json"):
                return {
                    "BackendState": "Running",
                    "Self": {"Online": 1, "DNSName": "node.example."},
                }
            return self.tailscale(arguments)

        with patch(
            "tools.v10_f_capture_production_quiescence._run_tailscale_json",
            side_effect=response,
        ), self.assertRaisesRegex(
            QuiescenceCaptureError,
            "tailscale_not_running_online",
        ):
            capture_tailscale_boundary()

    def test_missing_and_non_string_dns_name_fail_closed(self) -> None:
        for dns_name in (None, 7, ["node.example."]):
            with self.subTest(dns_name=repr(dns_name)):
                def response(
                    arguments: tuple[str, ...], *, selected=dns_name
                ):
                    if arguments == ("status", "--json"):
                        self_state = {"Online": True}
                        if selected is not None:
                            self_state["DNSName"] = selected
                        return {
                            "BackendState": "Running",
                            "Self": self_state,
                        }
                    return self.tailscale(arguments)

                with patch(
                    "tools.v10_f_capture_production_quiescence."
                    "_run_tailscale_json",
                    side_effect=response,
                ), self.assertRaisesRegex(
                    QuiescenceCaptureError,
                    "tailscale_dns_name_invalid",
                ):
                    capture_tailscale_boundary()

    def test_identical_other_node_authority_fails_closed(self) -> None:
        serve = self.tailscale(("serve", "status", "--json"))
        authority = next(iter(serve["Web"]))
        serve["Web"]["other-node.example:443"] = serve["Web"].pop(
            authority
        )

        def response(arguments: tuple[str, ...]):
            if arguments == ("status", "--json"):
                return {
                    "BackendState": "Running",
                    "Self": {"Online": True, "DNSName": "node.example."},
                }
            return serve

        with patch(
            "tools.v10_f_capture_production_quiescence._run_tailscale_json",
            side_effect=response,
        ), self.assertRaisesRegex(
            QuiescenceCaptureError,
            "tailscale_serve_config_not_unique",
        ):
            capture_tailscale_boundary()

    def test_second_serve_snapshot_drift_fails_closed(self) -> None:
        serve = self.tailscale(("serve", "status", "--json"))
        drifted = json.loads(json.dumps(serve))
        authority = next(iter(drifted["Web"]))
        drifted["Web"]["other-node.example:443"] = drifted["Web"].pop(
            authority
        )
        serve_call_count = 0

        def response(arguments: tuple[str, ...]):
            nonlocal serve_call_count
            if arguments == ("status", "--json"):
                return {
                    "BackendState": "Running",
                    "Self": {"Online": True, "DNSName": "node.example."},
                }
            if arguments == ("funnel", "status", "--json"):
                return serve
            serve_call_count += 1
            return serve if serve_call_count == 1 else drifted

        with patch(
            "tools.v10_f_capture_production_quiescence._run_tailscale_json",
            side_effect=response,
        ), self.assertRaises(QuiescenceCaptureError):
            capture_tailscale_boundary()
        self.assertEqual(2, serve_call_count)

    def test_identical_numeric_https_values_fail_closed(self) -> None:
        for numeric_value in (1, 1.0):
            with self.subTest(numeric_value=repr(numeric_value)):
                serve = self.tailscale(("serve", "status", "--json"))
                serve["TCP"]["443"]["HTTPS"] = numeric_value

                def response(arguments: tuple[str, ...]):
                    if arguments == ("status", "--json"):
                        return {
                            "BackendState": "Running",
                            "Self": {
                                "Online": True,
                                "DNSName": "node.example.",
                            },
                        }
                    return serve

                with patch(
                    "tools.v10_f_capture_production_quiescence."
                    "_run_tailscale_json",
                    side_effect=response,
                ), self.assertRaisesRegex(
                    QuiescenceCaptureError,
                    "tailscale_serve_config_not_unique",
                ):
                    capture_tailscale_boundary()

    def test_capture_rejects_listener_funnel_offline_and_proxy_drift(self) -> None:
        live = {
            "local_service_port": 8765,
            "local_service_listener_count": 1,
            "writer_lease_count": 0,
            "operational_wal_size": 0,
            "production_write_env_enabled": False,
        }
        with (
            patch(
                "tools.v10_f_capture_production_quiescence._run_tailscale_json",
                side_effect=self.tailscale,
            ),
            patch(
                "tools.v10_f_capture_production_quiescence._capture_live_quiescence",
                return_value=live,
            ),
        ):
            with self.assertRaisesRegex(
                QuiescenceCaptureError, "service_quiescence_evidence_invalid"
            ):
                capture_production_quiescence()

        cases = (
            {"BackendState": "Stopped", "Self": {"Online": False}},
            {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "node.example."},
                "AllowFunnel": True,
            },
            {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "node.example."},
            },
        )
        for index, status in enumerate(cases):
            def response(arguments: tuple[str, ...], *, selected=status, number=index):
                if arguments == ("status", "--json"):
                    return selected
                if number == 1:
                    return {"AllowFunnel": True, "Proxy": EXPECTED_PROXY}
                if number == 2:
                    return {"Proxy": "http://127.0.0.1:9999"}
                return {"Proxy": EXPECTED_PROXY}

            with patch(
                "tools.v10_f_capture_production_quiescence._run_tailscale_json",
                side_effect=response,
            ):
                with self.assertRaises(QuiescenceCaptureError):
                    capture_production_quiescence()

    def test_write_is_exclusive_and_cli_has_no_target_override(self) -> None:
        parser = build_parser()
        namespace = parser.parse_args(["--capture", "--confirm-service-stopped"])
        self.assertTrue(namespace.capture)
        self.assertEqual(
            {"capture", "confirm_service_stopped", "help"},
            {action.dest for action in parser._actions},
        )
        evidence = self.evidence()
        self.assertIsNone(
            inspect.signature(write_capture).parameters["output_root"].default
        )
        with self.assertRaisesRegex(
            QuiescenceCaptureError, "shared_capture_output_root_required"
        ):
            write_capture(evidence)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary) / "captures"
            target = write_capture(evidence, output_root=root)
            self.assertTrue(target.is_file())
            with self.assertRaisesRegex(QuiescenceCaptureError, "quiescence_capture_exists"):
                write_capture(evidence, output_root=root)

    def test_shared_writer_rejects_every_formal_root_alias_before_validation(
        self,
    ) -> None:
        aliases = (
            CAPTURE_ROOT,
            CAPTURE_ROOT.parent / "." / CAPTURE_ROOT.name,
            CAPTURE_ROOT / ".." / CAPTURE_ROOT.name,
            PRODUCTION_MIGRATION_CONTROL_ROOT,
            PRODUCTION_MIGRATION_CONTROL_ROOT
            / "service_quiescence_evidence.json",
        )
        for alias in aliases:
            with self.subTest(path=str(alias)), patch(
                "tools.v10_f_capture_production_quiescence."
                "validate_cognitive_production_quiescence_evidence",
                side_effect=AssertionError("validation must not run"),
            ), self.assertRaisesRegex(
                QuiescenceCaptureError,
                "shared_capture_formal_path_forbidden",
            ):
                write_capture({}, output_root=alias)

    def test_fixed_executor_has_no_injectable_evidence_root_clock_or_probe(
        self,
    ) -> None:
        signature = inspect.signature(capture_fixed_production_quiescence)
        self.assertEqual([], list(signature.parameters))
        self.assertIsNone(capture_fixed_production_quiescence.__defaults__)
        self.assertIsNone(capture_fixed_production_quiescence.__kwdefaults__)
        forbidden = {
            "evidence",
            "output_root",
            "root",
            "clock",
            "current_time",
            "probe",
        }
        self.assertTrue(forbidden.isdisjoint(signature.parameters))
        executor_type = type(_FIXED_PRODUCTION_CAPTURE_EXECUTOR)
        executor_signature = inspect.signature(executor_type.run)
        self.assertEqual(["self"], list(executor_signature.parameters))
        claim_signature = inspect.signature(
            capture_fixed_claim_boundary_and_source_identities
        )
        self.assertEqual([], list(claim_signature.parameters))
        self.assertIsNone(
            capture_fixed_claim_boundary_and_source_identities.__defaults__
        )
        self.assertIsNone(
            capture_fixed_claim_boundary_and_source_identities.__kwdefaults__
        )
        claim_executor_signature = inspect.signature(
            executor_type.claim_snapshot
        )
        self.assertEqual(
            ["self"], list(claim_executor_signature.parameters)
        )
        closure = inspect.getclosurevars(executor_type.run).nonlocals
        self.assertTrue(forbidden.isdisjoint(closure))
        fixed_capture = closure["fixed_capture_under_pins"]
        fixed_capture_closure = inspect.getclosurevars(fixed_capture).nonlocals
        fixed_network_capture = fixed_capture_closure[
            "fixed_network_capture"
        ]
        fixed_network_closure = inspect.getclosurevars(
            fixed_network_capture
        ).nonlocals
        self.assertEqual(
            COGNITIVE_CAPTURE_TAILSCALE_COMMANDS,
            fixed_network_closure["fixed_tailscale_commands"],
        )
        fixed_run_tailscale = fixed_network_closure[
            "fixed_run_tailscale"
        ]
        runner_source = inspect.getsource(fixed_run_tailscale)
        self.assertIn("shell=False", runner_source)
        self.assertIn("stdin=fixed_devnull", runner_source)
        self.assertIn("stderr=fixed_devnull", runner_source)
        self.assertIn("close_fds=True", runner_source)
        self.assertNotIn("capture_output=True", runner_source)
        claim_closure = inspect.getclosurevars(
            executor_type.claim_snapshot
        ).nonlocals
        fixed_claim_snapshot = claim_closure[
            "fixed_claim_snapshot_under_pins"
        ]
        claim_source = inspect.getsource(fixed_claim_snapshot)
        ordered_calls = (
            "fixed_open_source_pins",
            "fixed_expected_claim_source_identities",
            "fixed_source_identities_from_pins",
            "real_run_boundary_after_source_match",
        )
        positions = [claim_source.index(name) for name in ordered_calls]
        self.assertEqual(sorted(positions), positions)
        self.assertGreaterEqual(
            claim_source.count("fixed_source_identities_from_pins"), 2
        )
        source = inspect.getsource(executor_type.run)
        self.assertNotIn("capture_production_quiescence", source)
        self.assertNotIn("write_capture", source)
        bound_live = closure["real_live_capture"]
        bound_clock = closure["real_clock"]
        with patch(
            "tools.v10_f_capture_production_quiescence."
            "_capture_live_quiescence",
            return_value={"forged": True},
        ), patch(
            "tools.v10_f_capture_production_quiescence.utc_now",
            return_value="2099-01-01T00:00:00.000000Z",
        ):
            patched_closure = inspect.getclosurevars(executor_type.run).nonlocals
            self.assertIs(bound_live, patched_closure["real_live_capture"])
            self.assertIs(bound_clock, patched_closure["real_clock"])
        with self.assertRaisesRegex(
            QuiescenceCaptureError, "exact_production_capture_executor_required"
        ):
            executor_type.run(object())
        with self.assertRaises(TypeError):
            capture_fixed_production_quiescence(  # type: ignore[call-arg]
                evidence=self.evidence(),
                output_root=Path(tempfile.gettempdir()),
            )
        with self.assertRaises(TypeError):
            capture_fixed_claim_boundary_and_source_identities(  # type: ignore[call-arg]
                expected_source_identities={}
            )

    def test_claim_source_mismatch_stops_before_boundary_subprocess(self) -> None:
        calls = 0

        def boundary() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"status": "unexpected"}

        expected = {"tailscale_executable": {"sha256": "a" * 64}}
        actual = {"tailscale_executable": {"sha256": "b" * 64}}
        with self.assertRaisesRegex(
            QuiescenceCaptureError,
            "capture_source_identity_changed",
        ):
            _run_boundary_after_exact_source_match(
                expected,
                actual,
                boundary,
            )
        self.assertEqual(0, calls)
        self.assertEqual(
            {"status": "unexpected"},
            _run_boundary_after_exact_source_match(
                expected,
                expected,
                boundary,
            ),
        )
        self.assertEqual(1, calls)

    def test_pinned_python_executable_can_launch_without_handle_release(
        self,
    ) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        with pinned_control_directory(
            anchor=Path(executable.anchor).resolve(strict=True),
            path=executable.parent,
            create_if_missing=False,
        ) as directory:
            pinned = directory.open_file(
                executable.name,
                maximum_bytes=128 * 1024 * 1024,
            )
            completed = subprocess.run(
                (
                    str(executable),
                    "-I",
                    "-c",
                    "print('pinned-launch-ok')",
                ),
                check=True,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                timeout=20,
            )
            pinned.verify()
        self.assertEqual(b"pinned-launch-ok\r\n", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows share-mode contract")
    def test_pinned_executable_blocks_deterministic_aba_replace(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary).resolve(strict=True)
            executable = root / "tailscale.exe"
            replacement = root / "tailscale-b.exe"
            restoration = root / "tailscale-a-restored.exe"
            executable.write_bytes(b"trusted-A")
            replacement.write_bytes(b"untrusted-B")
            restoration.write_bytes(b"trusted-A")
            with pinned_control_directory(
                anchor=root,
                path=root,
                create_if_missing=False,
            ) as directory:
                pinned = directory.open_file(executable.name)
                with self.assertRaises(OSError):
                    os.replace(replacement, executable)
                    os.replace(restoration, executable)
                pinned.verify()
                self.assertEqual(b"trusted-A", pinned.read())
            self.assertEqual(b"trusted-A", executable.read_bytes())
            self.assertEqual(b"untrusted-B", replacement.read_bytes())
            self.assertEqual(b"trusted-A", restoration.read_bytes())

    def test_shared_writer_rejects_reparse_and_existing_hardlink_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            base = Path(temporary)
            real_root = base / "real"
            real_root.mkdir()
            alias = base / "alias"
            try:
                alias.symlink_to(real_root, target_is_directory=True)
            except OSError:
                alias = None
            if alias is not None:
                with self.assertRaisesRegex(
                    QuiescenceCaptureError,
                    "shared_capture_formal_path_forbidden",
                ):
                    write_capture(self.evidence(), output_root=alias / "captures")

            first_root = base / "first"
            first = write_capture(self.evidence(), output_root=first_root)
            hardlink_root = base / "hardlink"
            hardlink_root.mkdir()
            hardlink = hardlink_root / first.name
            os.link(first, hardlink)
            self.assertGreaterEqual(first.stat().st_nlink, 2)
            with self.assertRaisesRegex(
                QuiescenceCaptureError, "quiescence_capture_exists"
            ):
                write_capture(self.evidence(), output_root=hardlink_root)

    def test_extra_serve_routes_and_every_funnel_marker_shape_fail_closed(
        self,
    ) -> None:
        base = self.tailscale(("serve", "status", "--json"))
        extra = json.loads(json.dumps(base))
        authority = next(iter(extra["Web"]))
        extra["Web"][authority]["Handlers"]["/admin"] = {
            "Proxy": "http://127.0.0.1:9999"
        }
        cases = [
            (extra, base),
            ({"Note": EXPECTED_PROXY}, base),
            (base, {"unexpected": {"enabled": False}}),
        ]
        for marker in (
            True,
            False,
            {"443": True},
            [443],
            "enabled",
            1,
        ):
            funnel = json.loads(json.dumps(base))
            funnel["AllowFunnel"] = marker
            cases.append((base, funnel))
            cases.append((funnel, funnel))
        for marker_name in (
            "Public",
            "AllowPublic",
            "public_authorized",
            "PubliclyAccessible",
        ):
            funnel = json.loads(json.dumps(base))
            funnel[marker_name] = False
            cases.append((base, funnel))
            cases.append((funnel, funnel))

        type_drift = json.loads(json.dumps(base))
        type_drift["TCP"]["443"]["HTTPS"] = 1
        cases.append((base, type_drift))

        live = {
            "local_service_port": 8765,
            "local_service_listener_count": 0,
            "writer_lease_count": 0,
            "operational_wal_size": 0,
            "production_write_env_enabled": False,
        }
        for index, (serve, funnel) in enumerate(cases):
            with self.subTest(index=index):
                def response(
                    arguments: tuple[str, ...],
                    *,
                    selected_serve=serve,
                    selected_funnel=funnel,
                ):
                    if arguments == ("status", "--json"):
                        return {
                            "BackendState": "Running",
                            "Self": {
                                "Online": True,
                                "DNSName": "node.example.",
                            },
                        }
                    if arguments == ("serve", "status", "--json"):
                        return selected_serve
                    return selected_funnel

                with patch(
                    "tools.v10_f_capture_production_quiescence._run_tailscale_json",
                    side_effect=response,
                ), patch(
                    "tools.v10_f_capture_production_quiescence._capture_live_quiescence",
                    return_value=live,
                ), self.assertRaises(QuiescenceCaptureError):
                    capture_production_quiescence()

    def test_cli_normalizes_capture_failure_without_traceback_or_artifact(self) -> None:
        stream = io.StringIO()
        with (
            patch(
                "tools.v10_f_capture_production_quiescence."
                "capture_fixed_production_quiescence",
                side_effect=QuiescenceCaptureError(
                    "service_quiescence_evidence_invalid"
                ),
            ),
            redirect_stdout(stream),
        ):
            code = main(["--capture", "--confirm-service-stopped"])
        self.assertEqual(1, code)
        self.assertEqual(
            {
                "status": "failed",
                "reason_code": "service_quiescence_evidence_invalid",
            },
            json.loads(stream.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
