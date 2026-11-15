from __future__ import annotations

import argparse
import builtins
from contextlib import redirect_stdout
from http import HTTPStatus
import io
from pathlib import Path
import socket
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

import tools.local_web as web


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "run.ps1"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def web_args(**overrides) -> argparse.Namespace:
    values = {
        "host": "127.0.0.1",
        "port": 8781,
        "db": str(web.REAL_DB_PATH),
        "real": True,
        "readonly": True,
        "profile_admin": False,
        "runtime_review": False,
        "runtime_admin": False,
        "allow_external_model_calls": False,
        "confirm_deepseek_send": False,
        "runtime_admin_mock": False,
        "runtime_test_sidecar": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeBoundServer:
    def __init__(self, host: str, port: int, events: list[str]) -> None:
        self.server_address = (host, port)
        self.events = events
        self.serve_forever = Mock(side_effect=lambda: events.append("serve"))
        self.server_close = Mock(side_effect=lambda: events.append("close"))


def fake_execution_module(events: list[str]) -> types.ModuleType:
    module = types.ModuleType("memory.production_runtime_execution")

    class Service:
        def __init__(self) -> None:
            events.append("service")

    def issue(**_kwargs):
        events.append("authority")
        return object()

    module.ProductionRuntimeExecutionService = Service
    module.open_production_runtime_execution = issue
    return module


class RuntimeLaunchIsolationTest(unittest.TestCase):
    def test_runner_forwards_each_web_branch_once(self):
        source = RUNNER.read_text(encoding="utf-8")
        for marker in (
            '& $Python "tools\\local_web.py" @ForwardedArgs',
            '& $Python "tools\\local_web.py" "--db" $RealDb @ForwardedArgs',
            '& $Python "tools\\local_web.py" "--db" $RealDb "--real" "--readonly" @ForwardedArgs',
            '"--profile-admin" @ForwardedArgs',
            '"--runtime-review" @ForwardedArgs',
            '"--confirm-deepseek-send" @ForwardedArgs',
            '"--runtime-test-sidecar" $RuntimeMockSidecar @RuntimeMockArgs',
        ):
            self.assertIn(marker, source)
        self.assertEqual(1, source.count('"--runtime-review" @ForwardedArgs'))
        self.assertEqual(1, source.count('"--confirm-deepseek-send" @ForwardedArgs'))

    @unittest.skipUnless(POWERSHELL.is_file(), "PowerShell launcher test requires Windows")
    def test_runner_preserves_invalid_port_argument(self):
        result = subprocess.run(
            [
                str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(RUNNER), "runtime-admin", "--port", "70000",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode)
        self.assertIn("port must be an integer from 1 through 65535", output)
        self.assertNotIn("requires an explicit --port", output)

    @unittest.skipUnless(POWERSHELL.is_file(), "PowerShell launcher test requires Windows")
    def test_runner_preserves_one_argument_with_spaces_and_special_characters(self):
        missing = PROJECT_ROOT / "missing path & bracket [one]" / "source.db"
        result = subprocess.run(
            [
                str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(RUNNER), "web", "--db", str(missing),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing path & bracket [one]", output)
        self.assertNotIn("unrecognized arguments", output)

    def test_port_parser_is_strict_and_runtime_admin_requires_explicit_port(self):
        with patch.object(sys, "argv", ["local_web.py", "--runtime-admin"]):
            with self.assertRaises(SystemExit) as missing:
                web.parse_args()
        self.assertEqual(2, missing.exception.code)

        for value in ("0", "65536", "not-a-port", "1.5"):
            with self.subTest(value=value), patch.object(
                sys, "argv", ["local_web.py", "--runtime-admin", "--port", value]
            ):
                with self.assertRaises(SystemExit) as invalid:
                    web.parse_args()
                self.assertEqual(2, invalid.exception.code)

        with patch.object(
            sys, "argv", ["local_web.py", "--runtime-review", "--port", "8780"]
        ):
            self.assertEqual(8780, web.parse_args().port)
        with patch.object(sys, "argv", ["local_web.py", "--readonly"]):
            self.assertEqual(8765, web.parse_args().port)
        with patch.object(
            sys, "argv", ["local_web.py", "--runtime-admin", "--port", "8781"]
        ):
            self.assertEqual(8781, web.parse_args().port)
        with patch.object(sys, "argv", ["local_web.py", "--unknown-launch-argument"]):
            with self.assertRaises(SystemExit) as unknown:
                web.parse_args()
        self.assertEqual(2, unknown.exception.code)

    def test_duplicate_port_is_rejected_before_bind_or_execution_import(self):
        for values in (("8781", "8781"), ("8781", "8782")):
            with self.subTest(values=values):
                events: list[str] = []
                fake_module = fake_execution_module(events)
                original_import = builtins.__import__

                def tracked_import(name, *args, **kwargs):
                    if name == "memory.production_runtime_execution":
                        events.append("facade_import")
                    return original_import(name, *args, **kwargs)

                argv = [
                    "local_web.py", "--runtime-admin",
                    "--port", values[0], "--port", values[1],
                ]
                with patch.object(sys, "argv", argv), patch(
                    "tools.local_web.ThreadingHTTPServer",
                    side_effect=AssertionError("listener must not be constructed"),
                ) as server_factory, patch.dict(
                    sys.modules,
                    {"memory.production_runtime_execution": fake_module},
                ), patch("builtins.__import__", side_effect=tracked_import):
                    with self.assertRaises(SystemExit) as duplicate:
                        web.main()

                self.assertEqual(2, duplicate.exception.code)
                server_factory.assert_not_called()
                self.assertEqual([], events)

    def test_bind_happens_before_service_and_authority(self):
        events: list[str] = []
        server = FakeBoundServer("127.0.0.1", 8781, events)

        def bind(address, _handler):
            self.assertEqual(("127.0.0.1", 8781), address)
            events.append("bind")
            return server

        args = web_args(
            runtime_admin=True,
            allow_external_model_calls=True,
            confirm_deepseek_send=True,
        )
        fake_module = fake_execution_module(events)
        with patch("tools.local_web.parse_args", return_value=args), patch(
            "tools.local_web.ThreadingHTTPServer", side_effect=bind,
        ), patch.dict(sys.modules, {"memory.production_runtime_execution": fake_module}):
            result = web.main()

        self.assertEqual(0, result)
        self.assertEqual(["bind", "service", "authority", "serve", "close"], events)
        app = web.AyunOSHandler.app
        self.assertEqual("runtime-admin", app.service_mode)
        self.assertEqual("127.0.0.1", app.bound_host)
        self.assertEqual(8781, app.bound_port)
        self.assertFalse(app.service_readonly)
        self.assertTrue(app.readonly)
        self.assertTrue(app.runtime_admin)
        self.assertTrue(app.execution_authority_present)
        self.assertTrue(app.can_execute)
        self.assertRegex(app.server_instance_id, r"^srv_[0-9a-f]{32}$")

    def test_review_and_real_readonly_bind_their_exact_custom_ports(self):
        cases = (
            (web_args(port=8780, runtime_review=True), "runtime-review", 8780),
            (web_args(port=8790), "real-readonly", 8790),
        )
        for args, expected_mode, expected_port in cases:
            with self.subTest(mode=expected_mode):
                events: list[str] = []
                server = FakeBoundServer("127.0.0.1", expected_port, events)

                def bind(address, _handler):
                    self.assertEqual(("127.0.0.1", expected_port), address)
                    events.append("bind")
                    return server

                with patch("tools.local_web.parse_args", return_value=args), patch(
                    "tools.local_web.ThreadingHTTPServer", side_effect=bind,
                ):
                    self.assertEqual(0, web.main())
                app = web.AyunOSHandler.app
                self.assertEqual(expected_mode, app.service_mode)
                self.assertEqual(expected_port, app.bound_port)
                self.assertFalse(app.runtime_admin)
                self.assertFalse(app.execution_authority_present)
                self.assertFalse(app.can_execute)

    def test_real_occupied_port_fails_before_authority_creation(self):
        events: list[str] = []
        fake_module = fake_execution_module(events)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = int(occupied.getsockname()[1])
            args = web_args(
                port=port,
                runtime_admin=True,
                allow_external_model_calls=True,
                confirm_deepseek_send=True,
            )
            output = io.StringIO()
            with patch("tools.local_web.parse_args", return_value=args), patch.dict(
                sys.modules, {"memory.production_runtime_execution": fake_module}
            ), redirect_stdout(output):
                result = web.main()

        self.assertEqual(2, result)
        self.assertEqual([], events)
        self.assertIn(f"127.0.0.1:{port}", output.getvalue())
        self.assertIn("Execution authority was not created", output.getvalue())

    def test_two_web_servers_cannot_own_the_same_port(self):
        first = web.ThreadingHTTPServer(("127.0.0.1", 0), web.AyunOSHandler)
        port = int(first.server_address[1])
        try:
            with self.assertRaises(OSError):
                web.ThreadingHTTPServer(("127.0.0.1", port), web.AyunOSHandler)
        finally:
            first.server_close()

    def test_readonly_and_admin_modes_can_bind_distinct_ports(self):
        readonly_events: list[str] = []
        readonly_server = FakeBoundServer("127.0.0.1", 8765, readonly_events)
        readonly_args = web_args(port=8765)
        with patch("tools.local_web.parse_args", return_value=readonly_args), patch(
            "tools.local_web.ThreadingHTTPServer", return_value=readonly_server,
        ):
            self.assertEqual(0, web.main())
        readonly_app = web.AyunOSHandler.app

        admin_events: list[str] = []
        admin_server = FakeBoundServer("127.0.0.1", 8781, admin_events)
        admin_args = web_args(
            runtime_admin=True,
            allow_external_model_calls=True,
            confirm_deepseek_send=True,
        )
        with patch("tools.local_web.parse_args", return_value=admin_args), patch(
            "tools.local_web.ThreadingHTTPServer", return_value=admin_server,
        ), patch.dict(
            sys.modules,
            {"memory.production_runtime_execution": fake_execution_module(admin_events)},
        ):
            self.assertEqual(0, web.main())
        admin_app = web.AyunOSHandler.app

        self.assertEqual(("real-readonly", 8765, False, False), (
            readonly_app.service_mode, readonly_app.bound_port,
            readonly_app.runtime_admin, readonly_app.can_execute,
        ))
        self.assertEqual(("runtime-admin", 8781, True, True), (
            admin_app.service_mode, admin_app.bound_port,
            admin_app.runtime_admin, admin_app.can_execute,
        ))

    def test_status_reports_safe_mode_endpoint_and_instance_metadata(self):
        app = web.App(
            web.REAL_DB_PATH,
            real=True,
            readonly=True,
            runtime_execution_session=object(),
            runtime_execution_service=object(),
            service_mode="runtime-admin",
            bound_host="127.0.0.1",
            bound_port=8781,
            server_instance_id="srv_0123456789abcdef0123456789abcdef",
        )
        handler = object.__new__(web.AyunOSHandler)
        handler.app = app
        handler.send_json = Mock()
        with patch("tools.local_web.production_runtime_status", return_value={"ready": True}):
            handler.api_status()
        payload = handler.send_json.call_args.args[0]
        self.assertEqual("runtime-admin", payload["service_mode"])
        self.assertTrue(payload["runtime_admin"])
        self.assertFalse(payload["runtime_review"])
        self.assertFalse(payload["readonly"])
        self.assertTrue(payload["readonly_source_tables"])
        self.assertTrue(payload["execution_authority_present"])
        self.assertTrue(payload["can_execute"])
        self.assertEqual("127.0.0.1", payload["bound_host"])
        self.assertEqual(8781, payload["bound_port"])
        self.assertEqual("http://127.0.0.1:8781", payload["bound_endpoint"])
        self.assertIsInstance(payload["process_id"], int)
        self.assertEqual("srv_0123456789abcdef0123456789abcdef", payload["server_instance_id"])

    def test_readonly_execute_post_is_rejected_before_body_read(self):
        handler = object.__new__(web.AyunOSHandler)
        handler.path = "/api/runtime-execute"
        handler.app = Mock(readonly=True, runtime_review=False, profile_admin=False, runtime_admin=False)
        handler.send_error_json = Mock()
        handler.rfile = Mock()
        handler.rfile.read.side_effect = AssertionError("body must not be read")
        handler.do_POST()
        handler.send_error_json.assert_called_once_with(
            "Readonly mode: writes are disabled", HTTPStatus.FORBIDDEN,
        )
        handler.rfile.read.assert_not_called()

    def test_ui_displays_actual_endpoint_and_safe_runtime_flags(self):
        source = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for marker in (
            't("runtime.boundEndpoint"',
            'data.service_mode',
            'data.bound_host',
            'data.bound_port',
            'data.server_instance_id',
            't("legacy.tools.runtime.adminMode"',
            "runtimeBoolean(state.runtimeAdmin)",
            't("legacy.tools.runtime.executionAuthority"',
            "runtimeBoolean(state.executionAuthorityPresent)",
            't("legacy.tools.runtime.canExecute"',
            "runtimeBoolean(state.canExecute)",
        ):
            self.assertIn(marker, source)

    def test_favicon_is_inline_and_requires_no_external_resource(self):
        source = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('rel="icon"', source)
        self.assertIn('href="data:image/svg+xml,', source)
        self.assertNotIn('rel="icon" href="http', source)


if __name__ == "__main__":
    unittest.main()
