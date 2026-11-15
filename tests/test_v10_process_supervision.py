from __future__ import annotations

import ctypes
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memory import cognitive_supervision as supervision_module
from memory.cognitive_command_policy import (
    ResolvedCommand,
    _RESOLVED_COMMAND_FACTORY_SEAL,
    _executable_identity,
    _minimal_environment,
)
from memory.cognitive_supervision import (
    CognitiveSupervisionError,
    ProcessSupervisor,
)
from memory.operational_state_models import canonical_digest


T1 = "2026-08-01T00:00:00.000000Z"


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class V10ProcessTreeSupervisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="v10-process-tree-")
        self.root = Path(self._temporary.name).resolve()
        self.artifacts = self.root / "artifacts"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _script_command(self, body: str, name: str) -> ResolvedCommand:
        script = self.root / name
        script.write_text(body, encoding="utf-8")
        executable = _executable_identity(sys.executable)
        return ResolvedCommand(
            template_id="git.diff.check.v1",
            capability_id="process.template.run",
            capability_version=1,
            capability_digest=canonical_digest({"test_capability": name}),
            executable_identity=executable,
            argv=(executable["absolute_path"], "-I", "-S", str(script)),
            working_directory=str(self.root),
            environment=_minimal_environment(),
            stdin_mode="disabled",
            shell=False,
            template_digest=canonical_digest({"test_template": name}),
            _factory_seal=_RESOLVED_COMMAND_FACTORY_SEAL,
        )

    @staticmethod
    def _assert_gone(test: unittest.TestCase, *pids: int) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(_process_alive(pid) for pid in pids):
            time.sleep(0.02)
        for pid in pids:
            test.assertFalse(_process_alive(pid), f"residual process {pid}")

    def test_parent_exit_cannot_leave_detached_child(self) -> None:
        command = self._script_command(
            "import subprocess, sys\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL, close_fds=True,\n"
            ")\n"
            "print(child.pid, flush=True)\n",
            "parent_exit.py",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=5_000,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )

        result = supervisor.run(
            command,
            timeout_ms=3_000,
            output_limit_bytes=16_384,
        )

        child_pid = int(result.stdout.strip().splitlines()[0])
        self.assertIn("owned_child_process_terminated", result.reason_codes)
        self.assertFalse(result.termination["residual_process"])
        self._assert_gone(self, result.pid, child_pid)

    def test_durable_cancel_callback_terminates_parent_and_child(self) -> None:
        command = self._script_command(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL, close_fds=True,\n"
            ")\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            "cancel_tree.py",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=5_000,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )
        cancel_after = time.monotonic() + 0.25

        result = supervisor.run(
            command,
            timeout_ms=3_000,
            output_limit_bytes=16_384,
            cancellation_requested=lambda: time.monotonic() >= cancel_after,
        )

        child_pid = int(result.stdout.strip().splitlines()[0])
        self.assertIn("tool_execution_cancelled", result.reason_codes)
        self.assertFalse(result.termination["residual_process"])
        self._assert_gone(self, result.pid, child_pid)

    def test_short_clock_stall_terminates_whole_process_group(self) -> None:
        command = self._script_command(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL, close_fds=True,\n"
            ")\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            "stall_tree.py",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=100,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )

        result = supervisor.run(
            command,
            timeout_ms=3_000,
            output_limit_bytes=16_384,
        )

        child_pid = int(result.stdout.strip().splitlines()[0])
        self.assertIn("suspected_stall", result.reason_codes)
        self.assertFalse(result.termination["residual_process"])
        self._assert_gone(self, result.pid, child_pid)

    def test_process_stream_queue_is_bounded_under_output_flood(self) -> None:
        command = self._script_command(
            "import sys\n"
            "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n"
            "sys.stdout.buffer.flush()\n",
            "output_flood.py",
        )
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            stall_check_ms=5_000,
            poll_ms=5,
            termination_grace_ms=100,
            trusted_clock=lambda: T1,
        )
        real_queue = queue.Queue
        observed_maxsizes: list[int] = []

        def bounded_queue(*args, **kwargs):
            maximum = kwargs.get("maxsize", args[0] if args else 0)
            observed_maxsizes.append(int(maximum))
            return real_queue(*args, **kwargs)

        with patch(
            "memory.cognitive_supervision.queue.Queue",
            side_effect=bounded_queue,
        ):
            result = supervisor.run(
                command,
                timeout_ms=3_000,
                output_limit_bytes=1_024,
            )
        self.assertEqual([64], observed_maxsizes)
        self.assertIn("tool_output_artifact_limit_exceeded", result.reason_codes)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 1_024)
        self.assertFalse(result.termination["residual_process"])

    def test_real_junction_or_symlink_artifact_root_is_denied(self) -> None:
        target = self.root / "artifact-target"
        target.mkdir()
        link = self.root / "artifact-link"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaises(CognitiveSupervisionError) as denied:
                ProcessSupervisor(artifact_root=link, trusted_clock=lambda: T1)
            self.assertEqual(
                "process_artifact_root_unavailable",
                denied.exception.reason_code,
            )
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_artifact_root_identity_drift_fails_before_process_spawn(self) -> None:
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            trusted_clock=lambda: T1,
        )
        moved = self.root / "artifacts-original"
        self.artifacts.rename(moved)
        self.artifacts.mkdir()
        marker = self.root / "spawned.txt"
        command = self._script_command(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('spawned')\n",
            "must_not_spawn.py",
        )

        with self.assertRaises(CognitiveSupervisionError) as changed:
            supervisor.run(
                command,
                timeout_ms=3_000,
                output_limit_bytes=1_024,
            )
        self.assertEqual("process_artifact_root_changed", changed.exception.reason_code)
        self.assertFalse(marker.exists())

    def test_artifact_write_window_blocks_root_swap_and_outside_write(self) -> None:
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            trusted_clock=lambda: T1,
        )
        command = self._script_command(
            "print('bounded artifact', flush=True)\n",
            "artifact_output.py",
        )
        moved = self.root / "artifacts-moved"
        outside = self.root / "artifact-outside"
        outside.mkdir()
        observation = {"swap_completed": False, "swap_blocked": False}

        def swap_artifact_root(_binding) -> None:
            try:
                self.artifacts.rename(moved)
            except OSError as error:
                observation["swap_blocked"] = True
                raise OSError("artifact root swap blocked") from error
            self.artifacts.symlink_to(outside, target_is_directory=True)
            observation["swap_completed"] = True

        try:
            with patch(
                "memory.cognitive_local_broker._artifact_write_race_observation_point",
                side_effect=swap_artifact_root,
            ):
                with self.assertRaises(CognitiveSupervisionError) as denied:
                    supervisor.run(
                        command,
                        timeout_ms=3_000,
                        output_limit_bytes=1_024,
                    )
            self.assertEqual("process_artifact_write_failed", denied.exception.reason_code)
            self.assertTrue(
                observation["swap_completed"] or observation["swap_blocked"]
            )
            self.assertEqual([], list(outside.iterdir()))
        finally:
            if self.artifacts.is_symlink():
                self.artifacts.unlink()
            if moved.exists():
                moved.rename(self.artifacts)

    def test_artifact_root_swap_between_precheck_and_binding_is_denied(self) -> None:
        supervisor = ProcessSupervisor(
            artifact_root=self.artifacts,
            trusted_clock=lambda: T1,
        )
        command = self._script_command(
            "print('binding race', flush=True)\n",
            "artifact_binding_race.py",
        )
        moved = self.root / "artifacts-before-binding"
        original_resolve = supervision_module.resolve_project_path
        swapped = False

        def replace_before_binding(**kwargs):
            nonlocal swapped
            if not swapped:
                self.artifacts.rename(moved)
                self.artifacts.mkdir()
                swapped = True
            return original_resolve(**kwargs)

        try:
            with patch(
                "memory.cognitive_supervision.resolve_project_path",
                side_effect=replace_before_binding,
            ):
                with self.assertRaises(CognitiveSupervisionError) as denied:
                    supervisor.run(
                        command,
                        timeout_ms=3_000,
                        output_limit_bytes=1_024,
                    )
            self.assertTrue(swapped)
            self.assertEqual("process_artifact_root_changed", denied.exception.reason_code)
            self.assertEqual([], list(self.artifacts.iterdir()))
        finally:
            if self.artifacts.exists():
                os.rmdir(self.artifacts)
            if moved.exists():
                moved.rename(self.artifacts)


if __name__ == "__main__":
    unittest.main()
