from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import socket
import stat
import sys
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from service import windows as windows_module
from service.windows import (
    HostLaunch,
    ProcessRecord,
    TargetBinding,
    WindowsServiceError,
    WindowsSystem,
    _run_process_with_file_stdout,
)


@contextmanager
def capture_popen_processes():
    processes: list[subprocess.Popen] = []
    original_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    with patch("service.windows.subprocess.Popen", side_effect=capture):
        try:
            yield processes
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5.0)


class FixtureSystem(WindowsSystem):
    def __init__(self, owners: list[int], processes: dict[int, ProcessRecord]):
        super().__init__()
        self.owners = owners
        self.processes = processes
        self.module_paths: dict[int, list[str]] = {}
        self.module_errors: dict[int, WindowsServiceError] = {}
        self.module_queries: list[int] = []
        self.identity_overrides: dict[int, tuple[int, str] | None] = {}
        self.after_module_query = None

    def listener_owners(self, port: int) -> list[int]:
        return list(self.owners)

    def process_table(self) -> dict[int, ProcessRecord]:
        return dict(self.processes)

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        if pid in self.identity_overrides:
            return self.identity_overrides[pid]
        process = self.processes.get(pid)
        if process is None:
            return None
        return process.creation_time_100ns, process.image_path

    def _query_qqnt_modules(self, pid: int) -> list[str]:
        self.module_queries.append(pid)
        if self.after_module_query is not None:
            self.after_module_query(self, pid)
        if pid in self.module_errors:
            raise self.module_errors[pid]
        return list(self.module_paths.get(pid, []))


class PublicCommandLineFixtureSystem(FixtureSystem):
    def __init__(self, owners: list[int], processes: dict[int, ProcessRecord], original: dict[int, ProcessRecord]):
        super().__init__(owners, processes)
        self.identities = {
            pid: (record.creation_time_100ns, record.image_path)
            for pid, record in original.items()
        }
        self.command_lines: dict[int, object] = {}
        self.command_line_errors: dict[int, BaseException] = {}
        self.command_line_queries: list[int] = []
        self.parent_ids: dict[int, int] = {
            pid: record.parent_pid for pid, record in original.items()
        }
        self.parent_id_responses: dict[int, list[int]] = {}
        self.command_line_effect = None

    def _require_windows(self) -> None:
        # This fixture tests the Windows-specific query protocol on any CI OS.
        return None

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        if pid in self.identity_overrides:
            return self.identity_overrides[pid]
        return self.identities.get(pid)

    def public_process(self, pid: int):
        system = self

        class _PublicProcess:
            def __init__(self) -> None:
                self.ppid_calls = 0

            def ppid(self) -> int:
                responses = system.parent_id_responses.get(pid)
                if responses:
                    index = min(self.ppid_calls, len(responses) - 1)
                    self.ppid_calls += 1
                    return responses[index]
                return system.parent_ids.get(pid, 0)

            def cmdline(self):
                system.command_line_queries.append(pid)
                if system.command_line_effect is not None:
                    system.command_line_effect(system, pid)
                error = system.command_line_errors.get(pid)
                if error is not None:
                    raise error
                return system.command_lines.get(pid)

        return _PublicProcess()


class LaunchFixtureSystem(WindowsSystem):
    def __init__(self, launcher_pid: int, launcher_image: str):
        super().__init__()
        self.launcher_pid = launcher_pid
        self.launcher_image = launcher_image
        self.identity_queries: list[int] = []
        self.cleanup_launches: list[HostLaunch] = []
        self.terminated_records: list[list[dict[str, object]]] = []

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        self.identity_queries.append(pid)
        if pid != self.launcher_pid:
            return None
        return 123456789, self.launcher_image

    def owned_host_processes(self, launch: HostLaunch, _config: dict[str, object]) -> list[dict[str, object]]:
        self.cleanup_launches.append(launch)
        return [{
            "role": "launcher",
            "pid": launch.launcher_pid,
            "creationTime100ns": str(launch.launcher_creation_time_100ns),
            "imagePath": launch.launcher_image_path,
            "profilePath": launch.profile_path,
        }]

    def terminate_owned(self, records: list[dict[str, object]]) -> bool:
        self.terminated_records.append(records)
        return True


class _FakeApiCall:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakePipeFindKernel32:
    def __init__(
        self,
        names: list[str],
        *,
        end_error: int = 18,
        first_error: int | None = None,
        close_result: bool = True,
    ):
        self.names = list(names)
        self.end_error = end_error
        self.first_error = first_error
        self.close_result = close_result
        self.index = -1
        self.closed = False
        self.patterns: list[str] = []
        self.FindFirstFileW = _FakeApiCall(self._first)
        self.FindNextFileW = _FakeApiCall(self._next)
        self.FindClose = _FakeApiCall(self._close)

    @staticmethod
    def _set_name(pointer, name: str) -> None:
        pointer._obj.cFileName = name

    def _first(self, pattern: str, data):
        self.patterns.append(pattern)
        if self.first_error is not None:
            ctypes.set_last_error(self.first_error)
            return ctypes.c_void_p(-1).value
        if not self.names:
            ctypes.set_last_error(2)
            return ctypes.c_void_p(-1).value
        self.index = 0
        self._set_name(data, self.names[self.index])
        return 31337

    def _next(self, handle, data):
        if handle != 31337:
            ctypes.set_last_error(6)
            return 0
        next_index = self.index + 1
        if next_index >= len(self.names):
            ctypes.set_last_error(self.end_error)
            return 0
        self.index = next_index
        self._set_name(data, self.names[self.index])
        return 1

    def _close(self, handle):
        self.closed = handle == 31337
        if not self.close_result:
            ctypes.set_last_error(6)
            return 0
        return 1


class _FakeKernel32:
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x00000102

    def __init__(
        self,
        identities: dict[int, tuple[int, str]],
        *,
        handle_identities: dict[int, tuple[int, str]] | None = None,
        open_failures: set[int] | None = None,
        time_query_failures: set[int] | None = None,
        terminate_results: dict[int, bool] | None = None,
        wait_results: dict[int, list[int]] | None = None,
    ):
        self.identities = identities
        self.handle_identities = dict(handle_identities or identities)
        self.open_failures = set(open_failures or ())
        self.time_query_failures = set(time_query_failures or ())
        self.terminate_results = dict(terminate_results or {})
        self.wait_results = {pid: list(values) for pid, values in (wait_results or {}).items()}
        self.handles: dict[int, int] = {}
        self.events: list[tuple[str, int]] = []
        self.next_handle = 100_000
        self.OpenProcess = _FakeApiCall(self._open_process)
        self.GetProcessTimes = _FakeApiCall(self._get_process_times)
        self.QueryFullProcessImageNameW = _FakeApiCall(self._query_image)
        self.WaitForSingleObject = _FakeApiCall(self._wait)
        self.CloseHandle = _FakeApiCall(self._close)
        self.TerminateProcess = _FakeApiCall(self._terminate)

    def _open_process(self, _access, _inherit, pid):
        self.events.append(("open", pid))
        if pid in self.open_failures:
            return 0
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = pid
        return handle

    def _get_process_times(self, handle, created, _exited, _kernel, _user):
        pid = self.handles[handle]
        self.events.append(("times", pid))
        if pid in self.time_query_failures or pid not in self.handle_identities:
            return 0
        creation, _image = self.handle_identities[pid]
        output = created._obj
        output.low = creation & 0xFFFFFFFF
        output.high = creation >> 32
        return 1

    def _query_image(self, handle, _flags, image_buffer, size):
        pid = self.handles[handle]
        self.events.append(("image", pid))
        identity = self.handle_identities.get(pid)
        if identity is None:
            return 0
        image_buffer.value = identity[1]
        size._obj.value = len(identity[1])
        return 1

    def _wait(self, handle, _timeout):
        pid = self.handles[handle]
        self.events.append(("wait", pid))
        results = self.wait_results.get(pid, [])
        return results.pop(0) if results else self.WAIT_OBJECT_0

    def _close(self, handle):
        pid = self.handles.pop(handle)
        self.events.append(("close", pid))
        return 1

    def _terminate(self, handle, _exit_code):
        pid = self.handles[handle]
        self.events.append(("terminate", pid))
        return int(self.terminate_results.get(pid, True))


class _TerminationFixtureSystem(WindowsSystem):
    def __init__(
        self,
        identities: dict[int, tuple[int, str]],
        *,
        current_states: dict[int, bool | None] | None = None,
        handle_identities: dict[int, tuple[int, str]] | None = None,
        open_failures: set[int] | None = None,
        time_query_failures: set[int] | None = None,
        terminate_results: dict[int, bool] | None = None,
        wait_results: dict[int, list[int]] | None = None,
    ):
        super().__init__()
        self.identities = identities
        self.current_states = dict(current_states or {})
        self.kernel32 = _FakeKernel32(
            identities,
            handle_identities=handle_identities,
            open_failures=open_failures,
            time_query_failures=time_query_failures,
            terminate_results=terminate_results,
            wait_results=wait_results,
        )

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        return self.identities.get(pid)

    def process_is_current(self, pid: int, _creation: int) -> bool | None:
        if pid in self.current_states:
            return self.current_states[pid]
        return super().process_is_current(pid, _creation)

    def _kernel32(self):
        return self.kernel32


class WindowsLaunchIoTests(unittest.TestCase):
    def test_wait_returns_when_independent_child_keeps_file_stdout_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout_path = root / "launch.receipt"
            child_pid_path = root / "child.pid"
            python = getattr(sys, "_base_executable", sys.executable)
            child_code = "import time; time.sleep(10)"
            parent_code = (
                "import subprocess,sys; "
                "child=subprocess.Popen([sys.argv[3],'-c',sys.argv[1]]); "
                "open(sys.argv[2],'w',encoding='ascii').write(str(child.pid)); "
                "print('receipt',flush=True)"
            )
            command = [python, "-c", parent_code, child_code, str(child_pid_path), python]
            child_pid: int | None = None
            started = time.monotonic()
            try:
                with capture_popen_processes() as processes:
                    with stdout_path.open("wb") as stdout_file:
                        returncode = _run_process_with_file_stdout(
                            command,
                            stdout_file,
                            timeout_seconds=5.0,
                        )
                elapsed = time.monotonic() - started
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                self.assertEqual(returncode, 0)
                self.assertEqual(len(processes), 1)
                self.assertEqual(processes[0].poll(), 0)
                self.assertEqual(stdout_path.read_text(encoding="utf-8").strip(), "receipt")
                self.assertLess(elapsed, 3.0)
                if os.name == "nt":
                    self.assertIsNotNone(WindowsSystem().process_identity(child_pid))
                else:
                    os.kill(child_pid, 0)
            finally:
                if child_pid is None and child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="ascii"))
                if child_pid is not None:
                    if os.name == "nt":
                        system = WindowsSystem()
                        identity = system.process_identity(child_pid)
                        if identity is not None:
                            cleaned = system.terminate_owned([{
                                "role": "host_child",
                                "pid": child_pid,
                                "creationTime100ns": str(identity[0]),
                                "imagePath": identity[1],
                                "profilePath": str(root),
                            }])
                            self.assertTrue(cleaned)
                            deadline = time.monotonic() + 3.0
                            while time.monotonic() < deadline and system.process_identity(child_pid) is not None:
                                time.sleep(0.02)
                            self.assertIsNone(system.process_identity(child_pid))
                    else:
                        try:
                            os.kill(child_pid, signal.SIGTERM)
                        except OSError:
                            pass
                        deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline:
                            try:
                                os.kill(child_pid, 0)
                            except OSError:
                                break
                            time.sleep(0.02)

    def test_wait_timeout_kills_and_reaps_immediate_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout_path = root / "launch.receipt"
            command = [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]
            started = time.monotonic()
            with capture_popen_processes() as processes:
                with stdout_path.open("wb") as stdout_file:
                    with self.assertRaisesRegex(WindowsServiceError, "host_launch_failed"):
                        _run_process_with_file_stdout(command, stdout_file, timeout_seconds=0.3)
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
            self.assertLess(time.monotonic() - started, 3.0)

    def test_callback_exception_kills_and_reaps_immediate_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "launch.receipt"
            command = [sys.executable, "-c", "import time; time.sleep(30)"]

            def fail_progress() -> None:
                raise RuntimeError("progress_failed")

            started = time.monotonic()
            with capture_popen_processes() as processes:
                with stdout_path.open("wb") as stdout_file:
                    with self.assertRaisesRegex(RuntimeError, "progress_failed"):
                        _run_process_with_file_stdout(
                            command,
                            stdout_file,
                            timeout_seconds=10.0,
                            on_progress=fail_progress,
                        )
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
            self.assertLess(time.monotonic() - started, 3.0)

    def test_stop_callback_kills_process_and_preserves_stop_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stdout_path = Path(temporary) / "launch.receipt"
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
            stop_checks = 0
            progress_updates = 0

            def should_stop() -> bool:
                nonlocal stop_checks
                stop_checks += 1
                return stop_checks >= 3

            def on_progress() -> None:
                nonlocal progress_updates
                progress_updates += 1

            started = time.monotonic()
            with capture_popen_processes() as processes:
                with stdout_path.open("wb") as stdout_file:
                    with self.assertRaisesRegex(WindowsServiceError, "service_stop_requested"):
                        _run_process_with_file_stdout(
                            command,
                            stdout_file,
                            timeout_seconds=10.0,
                            should_stop=should_stop,
                            on_progress=on_progress,
                        )
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
            self.assertEqual(stop_checks, 3)
            self.assertGreaterEqual(progress_updates, 1)
            self.assertLess(time.monotonic() - started, 3.0)

    @staticmethod
    def _config(root: Path) -> tuple[dict[str, object], Path, int]:
        runtime = root / ".runtime" / "service"
        loader = root / "NapCat.Shell"
        loader.mkdir(parents=True)
        generation = 41
        return ({
            "runtime_dir": str(runtime),
            "paths": {"qq_exe": str(root / "QQ.exe"), "loader_root": str(loader)},
            "ports": {"host": 6111, "callback": 6110},
        }, loader / "NapCatWinBootMain.exe", generation)

    @staticmethod
    def _receipt(root: Path, generation: int, launcher_pid: object = 1234) -> dict[str, object]:
        run_directory = root / ".runtime" / "service" / "hosts" / f"host-{generation}"
        return {
            "ok": True,
            "dryRun": False,
            "hostGeneration": generation,
            "listenPort": 6111,
            "outputPort": 6110,
            "runDirectory": str(run_directory),
            "profilePath": str(run_directory / "profile"),
            "launchPlanPath": str(run_directory / "launch-plan.json"),
            "processLaunched": True,
            "launcherPid": launcher_pid,
        }

    def _launch_with_stdout(self, system: LaunchFixtureSystem, config: dict[str, object], generation: int, stdout: bytes):
        def write_receipt(_command: list[str], stream, **_kwargs: object) -> int:
            stream.write(stdout)
            stream.flush()
            return 0

        with patch("service.windows._run_process_with_file_stdout", side_effect=write_receipt):
            return system.launch_host(
                config,
                generation,
                TargetBinding(10, 100, 20, 200, "C:\\QQ\\resources\\app"),
                None,
            )

    def test_launch_receipt_pins_process_and_rejects_invalid_pid_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, launcher_image, generation = self._config(root)
            good = self._receipt(root, generation)
            system = LaunchFixtureSystem(1234, str(launcher_image))
            launch = self._launch_with_stdout(system, config, generation, json.dumps(good).encode("utf-8"))
            self.assertEqual(launch.launcher_pid, 1234)
            self.assertEqual(launch.launcher_creation_time_100ns, 123456789)
            self.assertEqual(launch.launcher_image_path, str(launcher_image))

            for bad_pid in (True, 0, -1, 0x80000000, "1234"):
                with self.subTest(pid=bad_pid):
                    value = dict(good)
                    value["launcherPid"] = bad_pid
                    invalid_system = LaunchFixtureSystem(1234, str(launcher_image))
                    with self.assertRaisesRegex(WindowsServiceError, "host_launch_failed"):
                        self._launch_with_stdout(invalid_system, config, generation, json.dumps(value).encode("utf-8"))
                    self.assertEqual(invalid_system.identity_queries, [])

    def test_launch_receipt_rejects_malformed_or_oversized_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, launcher_image, generation = self._config(root)
            system = LaunchFixtureSystem(1234, str(launcher_image))
            for output in (b"{", b"[]", b'{"ok":true,"ok":true}', b'{"ok":NaN}'):
                with self.subTest(output=output[:24]), self.assertRaisesRegex(WindowsServiceError, "host_launch_failed"):
                    self._launch_with_stdout(system, config, generation, output)
            with self.assertRaisesRegex(WindowsServiceError, "host_launch_failed"):
                self._launch_with_stdout(system, config, generation, b" " * (64 * 1024 + 1))

    def test_callback_failure_cleans_only_host_from_complete_pinned_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, launcher_image, generation = self._config(root)
            system = LaunchFixtureSystem(1234, str(launcher_image))
            output = json.dumps(self._receipt(root, generation)).encode("utf-8")

            def fail_progress() -> None:
                raise RuntimeError("progress_failed")

            def write_receipt_then_fail(_command: list[str], stream, **kwargs: object) -> int:
                stream.write(output)
                stream.flush()
                progress = kwargs.get("on_progress")
                self.assertTrue(callable(progress))
                progress()
                return 0

            with patch("service.windows._run_process_with_file_stdout", side_effect=write_receipt_then_fail):
                with self.assertRaisesRegex(RuntimeError, "progress_failed"):
                    system.launch_host(
                        config,
                        generation,
                        TargetBinding(10, 100, 20, 200, "C:\\QQ\\resources\\app"),
                        None,
                        on_progress=fail_progress,
                    )
            self.assertEqual(len(system.cleanup_launches), 1)
            cleanup_launch = system.cleanup_launches[0]
            self.assertEqual(cleanup_launch.launcher_pid, 1234)
            self.assertEqual(cleanup_launch.launcher_creation_time_100ns, 123456789)
            self.assertEqual(cleanup_launch.launcher_image_path, str(launcher_image))
            self.assertEqual(len(system.terminated_records), 1)
            self.assertEqual(system.terminated_records[0][0]["pid"], 1234)


class WindowsListenerEnumerationTests(unittest.TestCase):
    @staticmethod
    def _connection(
        *,
        family: int = socket.AF_INET,
        socktype: int = socket.SOCK_STREAM,
        status: str = windows_module.psutil.CONN_LISTEN,
        local_address: object = ("127.0.0.1", 8016),
        pid: object = 1234,
    ) -> SimpleNamespace:
        return SimpleNamespace(family=family, type=socktype, status=status, laddr=local_address, pid=pid)

    @staticmethod
    def _system() -> WindowsSystem:
        system = WindowsSystem()
        system._require_windows = lambda: None
        return system

    def test_ipv4_ipv6_rows_deduplicate_same_owner_and_return_sorted_unique_pids(self) -> None:
        rows = [
            self._connection(family=socket.AF_INET6, local_address=("::1", 8016, 0, 0), pid=42),
            self._connection(local_address=("127.0.0.1", 8016), pid=42),
            self._connection(local_address=("0.0.0.0", 8016), pid=17),
        ]
        system = self._system()
        with patch.object(windows_module.psutil, "net_connections", return_value=rows) as query:
            with patch.object(system, "_run_powershell", side_effect=AssertionError("PowerShell must not be used")):
                self.assertEqual(system.listener_owners(8016), [17, 42])
        query.assert_called_once_with(kind="tcp")

    def test_other_ports_and_nonlisten_rows_do_not_contribute_owners(self) -> None:
        rows = [
            self._connection(local_address=("127.0.0.1", 8017), pid=17),
            self._connection(
                status=windows_module.psutil.CONN_ESTABLISHED,
                local_address=None,
                pid=None,
            ),
        ]
        system = self._system()
        with patch.object(windows_module.psutil, "net_connections", return_value=rows):
            self.assertEqual(system.listener_owners(8016), [])

    def test_distinct_matching_owners_are_returned_for_binder_ambiguity_check(self) -> None:
        rows = [
            self._connection(local_address=("127.0.0.1", 8016), pid=9002),
            self._connection(family=socket.AF_INET6, local_address=("::", 8016, 0, 0), pid=9001),
        ]
        system = self._system()
        with patch.object(windows_module.psutil, "net_connections", return_value=rows):
            self.assertEqual(system.listener_owners(8016), [9001, 9002])

    def test_invalid_matching_owner_pids_fail_closed(self) -> None:
        for pid in (None, True, False, 0, -1, "1234"):
            with self.subTest(pid=pid):
                system = self._system()
                with patch.object(
                    windows_module.psutil,
                    "net_connections",
                    return_value=[self._connection(pid=pid)],
                ):
                    with self.assertRaisesRegex(WindowsServiceError, "listener_snapshot_invalid"):
                        system.listener_owners(8016)

    def test_malformed_tcp_rows_and_snapshot_fail_closed(self) -> None:
        malformed_rows = (
            self._connection(family=999),
            self._connection(socktype=socket.SOCK_DGRAM),
            self._connection(local_address=None),
            self._connection(local_address=("not-an-ip", 8016)),
            self._connection(local_address=("127.0.0.1", True)),
            self._connection(local_address=("127.0.0.1", 0)),
        )
        for row in malformed_rows:
            with self.subTest(row=row):
                system = self._system()
                with patch.object(windows_module.psutil, "net_connections", return_value=[row]):
                    with self.assertRaisesRegex(WindowsServiceError, "listener_snapshot_invalid"):
                        system.listener_owners(8016)
        for snapshot in (None, "invalid", [self._connection()] * 65_537):
            with self.subTest(snapshot_type=type(snapshot).__name__):
                system = self._system()
                with patch.object(windows_module.psutil, "net_connections", return_value=snapshot):
                    with self.assertRaisesRegex(WindowsServiceError, "listener_snapshot_invalid"):
                        system.listener_owners(8016)

    def test_api_failure_uses_fixed_unavailable_code(self) -> None:
        system = self._system()
        with patch.object(windows_module.psutil, "net_connections", side_effect=RuntimeError("private detail")):
            with self.assertRaisesRegex(WindowsServiceError, "windows_query_unavailable") as raised:
                system.listener_owners(8016)
        self.assertEqual(str(raised.exception), "windows_query_unavailable")

    def test_more_than_32_matching_rows_fail_closed_even_when_pids_duplicate(self) -> None:
        system = self._system()
        rows = [self._connection(pid=1234)] * 33
        with patch.object(windows_module.psutil, "net_connections", return_value=rows):
            with self.assertRaisesRegex(WindowsServiceError, "listener_snapshot_invalid"):
                system.listener_owners(8016)

    def test_invalid_port_is_rejected_before_windows_or_snapshot_api(self) -> None:
        system = WindowsSystem()
        with patch.object(system, "_require_windows", side_effect=AssertionError("unexpected platform check")):
            with patch.object(windows_module.psutil, "net_connections", side_effect=AssertionError("unexpected query")):
                for port in (True, 0, 65536, "8016"):
                    with self.subTest(port=port):
                        with self.assertRaisesRegex(WindowsServiceError, "listener_port_invalid"):
                            system.listener_owners(port)  # type: ignore[arg-type]

    def test_each_call_uses_a_fresh_snapshot(self) -> None:
        system = self._system()
        with patch.object(
            windows_module.psutil,
            "net_connections",
            side_effect=([
                self._connection(pid=1001),
            ], [
                self._connection(pid=1002),
            ]),
        ) as query:
            self.assertEqual(system.listener_owners(8016), [1001])
            self.assertEqual(system.listener_owners(8016), [1002])
        self.assertEqual(query.call_args_list, [call(kind="tcp"), call(kind="tcp")])

    def test_windows_requirement_is_preserved(self) -> None:
        system = WindowsSystem()
        with patch.object(system, "_require_windows", side_effect=WindowsServiceError("platform_unsupported")) as require:
            with patch.object(windows_module.psutil, "net_connections", side_effect=AssertionError("unexpected query")):
                with self.assertRaisesRegex(WindowsServiceError, "platform_unsupported"):
                    system.listener_owners(8016)
        require.assert_called_once_with()

    def test_owned_ipv4_and_ipv6_listeners_are_visible_when_available(self) -> None:
        system = self._system()
        sockets: list[socket.socket] = []
        try:
            ipv4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(ipv4)
            ipv4.bind(("127.0.0.1", 0))
            ipv4.listen()
            ports = [ipv4.getsockname()[1]]

            if socket.has_ipv6:
                ipv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                try:
                    ipv6.bind(("::1", 0))
                    ipv6.listen()
                except OSError:
                    ipv6.close()
                else:
                    sockets.append(ipv6)
                    ports.append(ipv6.getsockname()[1])

            for index, port in enumerate(ports):
                with self.subTest(family="ipv4" if index == 0 else "ipv6"):
                    self.assertEqual(system.listener_owners(port), [os.getpid()])
        finally:
            for listening_socket in sockets:
                listening_socket.close()


class WindowsProcessTableTests(unittest.TestCase):
    def test_unavailable_snowluma_module_read_retries_once_with_fresh_identity_and_short_budget(self) -> None:
        system = WindowsSystem()
        identity = (123456, r"C:\QQ\QQ.exe")
        modules = ([r"C:\QQ\QQNT.dll"], [r"C:\QQ\wrapper.node"])
        with (patch.object(system, "process_identity", return_value=identity) as identity_read,
              patch.object(system, "_query_snowluma_qq_modules", side_effect=[
                  WindowsServiceError("target_module_metadata_unavailable"), modules,
              ]) as query):
            self.assertEqual(system._query_current_snowluma_qq_modules(123, identity), modules)
            self.assertEqual(query.call_args_list, [call(123), call(123, timeout=1.0)])
            self.assertEqual(identity_read.call_count, 2)

    def test_denied_invalid_or_missing_module_reads_are_not_retried(self) -> None:
        for code in ("target_module_access_denied", "target_module_metadata_invalid", "target_module_missing"):
            with self.subTest(code=code):
                system = WindowsSystem()
                failure = WindowsServiceError(code)
                with (patch.object(system, "process_identity") as identity_read,
                      patch.object(system, "_query_snowluma_qq_modules", side_effect=failure) as query):
                    with self.assertRaises(WindowsServiceError) as raised:
                        system._query_current_snowluma_qq_modules(123, (123456, r"C:\QQ\QQ.exe"))
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(query.call_count, 1)
                    identity_read.assert_not_called()

    def test_repeated_module_unavailability_never_uses_cached_modules(self) -> None:
        system = WindowsSystem()
        identity = (123456, r"C:\QQ\QQ.exe")
        with (patch.object(system, "process_identity", return_value=identity),
              patch.object(system, "_query_snowluma_qq_modules", side_effect=WindowsServiceError(
                  "target_module_metadata_unavailable")) as query):
            with self.assertRaisesRegex(WindowsServiceError, "target_module_metadata_unavailable"):
                system._query_current_snowluma_qq_modules(123, identity)
            self.assertEqual(query.call_count, 2)

    def test_identity_change_before_or_after_module_retry_cannot_authorize_result(self) -> None:
        identity = (123456, r"C:\QQ\QQ.exe")
        for changed in (None, (123457, identity[1]), (identity[0], r"C:\Other\QQ.exe")):
            for after in (False, True):
                with self.subTest(changed=changed, after=after):
                    system = WindowsSystem()
                    readings = [identity, changed] if after else [changed]
                    with (patch.object(system, "process_identity", side_effect=readings),
                          patch.object(system, "_query_snowluma_qq_modules", side_effect=[
                              WindowsServiceError("target_module_metadata_unavailable"), (["qqnt"], ["wrapper"]),
                          ]) as query):
                        with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
                            system._query_current_snowluma_qq_modules(123, identity)
                        self.assertEqual(query.call_count, 2 if after else 1)

    def test_process_table_recovers_image_but_defers_missing_commandline(self) -> None:
        qq_image = r"C:\Program Files\Tencent\QQ\QQ.exe"
        python_image = r"C:\NachoBot\python.exe"
        rows = [
            {
                "ProcessId": 31820,
                "ParentProcessId": 51996,
                "ExecutablePath": None,
                "CommandLine": '"QQ.exe" --stale-row-value',
            },
            {
                "ProcessId": 51996,
                "ParentProcessId": 1000,
                "ExecutablePath": qq_image,
                "CommandLine": None,
            },
            {
                "ProcessId": 8095,
                "ParentProcessId": 1000,
                "ExecutablePath": python_image,
                "CommandLine": '"C:\\NachoBot\\python.exe" main.py',
            },
            {
                "ProcessId": 8100,
                "ParentProcessId": 1000,
                "ExecutablePath": r"C:\Other\QQ.exe",
                "CommandLine": None,
            },
        ]
        identities = {
            31820: (318200, qq_image),
            51996: (519600, qq_image),
            8095: (809500, python_image),
            8100: (810000, qq_image),
        }
        system = WindowsSystem()
        system._run_powershell = lambda *_args, **_kwargs: json.dumps(rows)
        with (
            patch.object(system, "process_identity", side_effect=identities.get),
            patch.object(windows_module.psutil, "Process", side_effect=AssertionError("process table must not query cmdline")),
        ):
            processes = system.process_table()

        self.assertEqual(processes[31820].image_path, qq_image)
        self.assertEqual(processes[31820].creation_time_100ns, 318200)
        self.assertEqual(processes[31820].command_line, "")
        self.assertEqual(processes[51996].command_line, "")
        self.assertEqual(processes[8095].command_line, '"C:\\NachoBot\\python.exe" main.py')
        self.assertNotIn(8100, processes)

    def test_public_argv_serialization_roundtrips_app_path_with_spaces(self) -> None:
        argv = [
            r"C:\Program Files\Tencent\QQ\QQ.exe",
            "--app-path",
            r"C:\QQ User Data\resources\app",
            "--type=utility",
            "--utility-sub-type=node.mojom.nodeservice",
        ]
        command_line = WindowsSystem._serialize_public_command_line(argv)
        present, values, invalid = windows_module._app_path_arguments(command_line)
        self.assertTrue(present)
        self.assertEqual(values, (argv[2],))
        self.assertFalse(invalid)

    def test_module_query_distinguishes_null_collection_from_missing_qqnt(self) -> None:
        system = WindowsSystem()
        responses = iter((
            '{"status":"unavailable"}',
            '{"status":"missing","paths":[]}',
        ))
        commands: list[str] = []

        def run_powershell(command: str, **_kwargs: object) -> str:
            commands.append(command)
            return next(responses)

        system._run_powershell = run_powershell
        with self.assertRaisesRegex(WindowsServiceError, "target_module_metadata_unavailable"):
            system._query_qqnt_modules(31820)
        self.assertEqual(system._query_qqnt_modules(31820), [])

        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], commands[1])
        lowered = commands[0].casefold()
        self.assertIn("$modulecollection=$p.modules", lowered)
        self.assertIn("if ($null -eq $modulecollection)", lowered)
        self.assertIn("status='unavailable'", lowered)
        self.assertIn("status='missing'", lowered)


class WindowsBindingTests(unittest.TestCase):
    def test_missing_onebot_listener_is_transport_absence_not_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            with self.assertRaisesRegex(WindowsServiceError, "adapter_listener_unavailable"):
                FixtureSystem([], processes).bind_adapter(fixture["config"], fixture["manifest"])

    def test_public_cmdline_fallback_is_limited_to_the_bound_qq_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            original = dict(processes)
            missing = {fixture["root"], fixture["kernel"], 30100, 30101}
            for pid in missing:
                record = processes[pid]
                processes[pid] = ProcessRecord(
                    record.pid,
                    record.parent_pid,
                    record.image_path,
                    "",
                    record.creation_time_100ns,
                )
            system = PublicCommandLineFixtureSystem([fixture["owner"]], processes, original)
            self._with_module_metadata(system, fixture, module)
            system.command_lines = {
                pid: original[pid].command_line.split()
                for pid in (fixture["root"], fixture["kernel"])
            }
            with patch.object(windows_module.psutil, "Process", side_effect=system.public_process):
                target = system.bind_adapter(fixture["config"], fixture["manifest"])

        self.assertEqual(target.kernel_pid, fixture["kernel"])
        self.assertEqual(target.adapter_pid, fixture["manifest"]["adapterPid"])
        self.assertEqual(system.command_line_queries, [fixture["root"], fixture["kernel"]])
        self.assertNotIn(30100, system.command_line_queries)
        self.assertNotIn(30101, system.command_line_queries)

    def test_complete_wmi_commandlines_do_not_call_psutil(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with patch.object(windows_module.psutil, "Process", side_effect=AssertionError("unexpected fallback")):
                target = system.bind_adapter(fixture["config"], fixture["manifest"])
        self.assertEqual(target.kernel_pid, fixture["kernel"])

    def test_missing_commandline_reuses_public_app_path_for_strict_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, app = self._tree(Path(temporary), "napcat", "kernel")
            original = dict(processes)
            missing = (50000, fixture["owner"], 51200)
            for pid in missing:
                record = processes[pid]
                processes[pid] = ProcessRecord(
                    record.pid,
                    record.parent_pid,
                    record.image_path,
                    "",
                    record.creation_time_100ns,
                )

            system = PublicCommandLineFixtureSystem([fixture["owner"]], processes, original)
            app_path = str(app)
            system.command_lines = {
                50000: [original[50000].image_path, "--app-path", app_path],
                fixture["owner"]: [original[fixture["owner"]].image_path, "--app-path", app_path, "--type=renderer"],
                51200: [original[51200].image_path, "--app-path", app_path, "--type=utility", "--utility-sub-type=node.mojom.nodeservice"],
            }

            with patch.object(windows_module.psutil, "Process", side_effect=system.public_process):
                target = system.bind_adapter(fixture["config"], fixture["manifest"])

        self.assertEqual(target.kernel_pid, 51200)
        self.assertEqual(Path(target.app_path), app)
        self.assertEqual(system.command_line_queries, [50000, fixture["owner"], 51200])
        self.assertEqual(system.module_queries, [])

    def test_invalid_nonqq_intermediary_lineage_rejects_before_public_cmdline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _module = self._flagless_tree(Path(temporary))
            root = processes[fixture["root"]]
            bridge_pid = 31000
            descendant_pid = 31001
            bridge_image = str(Path(temporary) / "bridge.exe")
            processes[bridge_pid] = ProcessRecord(
                bridge_pid,
                root.pid,
                bridge_image,
                '"bridge.exe"',
                root.creation_time_100ns - 50_000,
            )
            processes[descendant_pid] = ProcessRecord(
                descendant_pid,
                bridge_pid,
                root.image_path,
                "",
                root.creation_time_100ns + 100_000,
            )
            original = dict(processes)
            system = PublicCommandLineFixtureSystem([fixture["owner"]], processes, original)
            system.command_lines[descendant_pid] = [
                root.image_path,
                "--type=utility",
                "--utility-sub-type=node.mojom.nodeservice",
            ]

            with patch.object(windows_module.psutil, "Process", side_effect=system.public_process):
                with self.assertRaisesRegex(WindowsServiceError, "target_process_tree_invalid"):
                    system.bind_adapter(fixture["config"], fixture["manifest"])

        self.assertEqual(system.command_line_queries, [])

    def test_valid_nonqq_intermediary_lineage_allows_public_cmdline_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            root = processes[fixture["root"]]
            bridge_pid = 31000
            descendant_pid = 31001
            bridge_image = str(Path(temporary) / "bridge.exe")
            processes[bridge_pid] = ProcessRecord(
                bridge_pid,
                root.pid,
                bridge_image,
                '"bridge.exe"',
                root.creation_time_100ns + 50_000,
            )
            processes[descendant_pid] = ProcessRecord(
                descendant_pid,
                bridge_pid,
                root.image_path,
                "",
                root.creation_time_100ns + 100_000,
            )
            original = dict(processes)
            system = PublicCommandLineFixtureSystem([fixture["owner"]], processes, original)
            self._with_module_metadata(system, fixture, module)
            system.command_lines[descendant_pid] = [root.image_path, "--type=renderer"]

            with patch.object(windows_module.psutil, "Process", side_effect=system.public_process):
                target = system.bind_adapter(fixture["config"], fixture["manifest"])

        self.assertEqual(target.kernel_pid, fixture["kernel"])
        self.assertEqual(system.command_line_queries, [descendant_pid])

    def test_public_cmdline_failures_and_identity_changes_fail_closed(self) -> None:
        cases = (
            ("denied", "target_process_unavailable"),
            ("empty", "target_process_unavailable"),
            ("nonstring", "target_process_unavailable"),
            ("empty_arg", "target_process_unavailable"),
            ("nul", "target_process_unavailable"),
            ("oversize", "target_process_unavailable"),
            ("pid_reuse", "target_process_changed"),
            ("image_change", "target_process_changed"),
            ("parent_pid_change", "target_process_changed"),
            ("parent_identity_change", "target_process_changed"),
        )
        for failure, expected_code in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                processes, fixture, _module = self._flagless_tree(Path(temporary))
                original = dict(processes)
                kernel_pid = fixture["kernel"]
                kernel = processes[kernel_pid]
                processes[kernel_pid] = ProcessRecord(
                    kernel.pid,
                    kernel.parent_pid,
                    kernel.image_path,
                    "",
                    kernel.creation_time_100ns,
                )
                system = PublicCommandLineFixtureSystem([fixture["owner"]], processes, original)
                system.command_lines[kernel_pid] = original[kernel_pid].command_line.split()
                if failure == "denied":
                    system.command_line_errors[kernel_pid] = PermissionError(5, "denied")
                elif failure == "empty":
                    system.command_lines[kernel_pid] = []
                elif failure == "nonstring":
                    system.command_lines[kernel_pid] = ["QQ.exe", None]
                elif failure == "empty_arg":
                    system.command_lines[kernel_pid] = ["QQ.exe", ""]
                elif failure == "nul":
                    system.command_lines[kernel_pid] = ["QQ.exe", "bad\x00argument"]
                elif failure == "oversize":
                    system.command_lines[kernel_pid] = ["Q" * 32_768]
                elif failure == "pid_reuse":
                    def effect(current, pid):
                        if pid == kernel_pid:
                            current.identity_overrides[pid] = (
                                original[pid].creation_time_100ns + 1,
                                original[pid].image_path,
                            )
                    system.command_line_effect = effect
                elif failure == "image_change":
                    def effect(current, pid):
                        if pid == kernel_pid:
                            current.identity_overrides[pid] = (
                                original[pid].creation_time_100ns,
                                str(Path(temporary) / "other.exe"),
                            )
                    system.command_line_effect = effect
                elif failure == "parent_pid_change":
                    system.parent_id_responses[kernel_pid] = [kernel.parent_pid, kernel.parent_pid + 100]
                elif failure == "parent_identity_change":
                    parent_pid = kernel.parent_pid
                    def effect(current, pid):
                        if pid == kernel_pid:
                            parent = original[parent_pid]
                            current.identity_overrides[parent_pid] = (
                                parent.creation_time_100ns + 1,
                                parent.image_path,
                            )
                    system.command_line_effect = effect

                with patch.object(windows_module.psutil, "Process", side_effect=system.public_process):
                    with self.assertRaisesRegex(WindowsServiceError, expected_code):
                        system.bind_adapter(fixture["config"], fixture["manifest"])
                self.assertEqual(processes[kernel_pid].command_line, "")

    def _tree(self, root: Path, adapter: str, owner_kind: str) -> tuple[dict[int, ProcessRecord], dict[str, object], Path]:
        exe = root / "QQ.exe"
        exe.touch()
        app = root / "QQ App"
        (app / "avsdk").mkdir(parents=True)
        (app / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"fixture")
        python = root / "python.exe"
        python.touch()
        other_app = root / "Personal QQ"
        (other_app / "avsdk").mkdir(parents=True)
        (other_app / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"fixture")

        expected_exe = str(exe)
        app_path = str(app)
        other_path = str(other_app)
        processes = {
            1: ProcessRecord(1, 0, str(python), '"python.exe"', 1),
            8095: ProcessRecord(8095, 1, str(python), '"python.exe" main.py', 800000),
            9001: ProcessRecord(9001, 1, str(python), '"python.exe" main.py', 900000),
            50000: ProcessRecord(50000, 1, expected_exe, f'"{expected_exe}" --app-path "{app_path}"', 500000),
            60000: ProcessRecord(60000, 1, expected_exe, f'"{expected_exe}" --app-path "{other_path}"', 600000),
            60001: ProcessRecord(60001, 60000, expected_exe,
                                 f'"{expected_exe}" --app-path "{other_path}" --type=utility --utility-sub-type=node.mojom.nodeservice', 600100),
        }
        if owner_kind == "kernel":
            owner_pid = 51144
            processes[owner_pid] = ProcessRecord(owner_pid, 50000, expected_exe, f'"{expected_exe}" --app-path "{app_path}" --type=renderer', 511440)
            processes[51200] = ProcessRecord(51200, owner_pid, expected_exe,
                                             f'"{expected_exe}" --app-path "{app_path}" --type=utility --utility-sub-type=node.mojom.nodeservice', 512000)
        else:
            owner_pid = 50000
            processes[51200] = ProcessRecord(51200, 50000, expected_exe,
                                             f'"{expected_exe}" --app-path "{app_path}" --type=utility --utility-sub-type=node.mojom.nodeservice', 512000)
        adapter_pid = 8095 if adapter == "napcat" else 9001
        manifest = {
            "schema": 1,
            "adapter": adapter,
            "accountId": "12345",
            "connected": True,
            "adapterPid": adapter_pid,
            "adapterCreationTime100ns": str(processes[adapter_pid].creation_time_100ns),
            "connectionGeneration": "a1b2c3d4" if adapter == "napcat" else 7,
            "updatedAtMs": 100,
        }
        config = {
            "adapter": adapter,
            "onebot": {"url": "http://127.0.0.1:8888"},
            "paths": {"qq_exe": expected_exe},
        }
        return processes, {"config": config, "manifest": manifest, "owner": owner_pid}, app

    def _flagless_tree(self, root: Path, owner_kind: str = "utility") -> tuple[dict[int, ProcessRecord], dict[str, object], Path]:
        exe = root / "QQNT" / "QQ.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        python = root / "python.exe"
        python.touch()
        loader = root / "NapCatLoader.exe"
        loader.touch()
        version_root = exe.parent / "versions" / "fixture-version-a"
        app = version_root / "resources" / "app"
        (app / "avsdk").mkdir(parents=True)
        (app / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"fixture")
        module = version_root / "QQNT.dll"
        module.write_bytes(b"fixture")

        exe_path = str(exe)
        python_path = str(python)
        root_pid = 30000
        owner_pid = 30001 if owner_kind == "utility" else root_pid
        loader_pid = 30003
        processes = {
            1: ProcessRecord(1, 0, str(loader), '"NapCatLoader.exe"', 50),
            loader_pid: ProcessRecord(loader_pid, 1, str(loader), '"NapCatLoader.exe"', 300000),
            8095: ProcessRecord(8095, 1, python_path, '"python.exe" main.py', 800000),
            9001: ProcessRecord(9001, 1, python_path, '"python.exe" main.py', 900000),
            root_pid: ProcessRecord(root_pid, loader_pid, exe_path, f'"{exe_path}" --type=browser', 400000),
            30001: ProcessRecord(30001, root_pid, exe_path,
                                 f'"{exe_path}" --type=utility --utility-sub-type=node.mojom.nodeservice', 500000),
            30002: ProcessRecord(30002, root_pid, exe_path, f'"{exe_path}" --type=renderer', 500100),
            30100: ProcessRecord(30100, loader_pid, exe_path, f'"{exe_path}" --type=browser', 450000),
            30101: ProcessRecord(30101, 30100, exe_path,
                                 f'"{exe_path}" --type=utility --utility-sub-type=node.mojom.nodeservice', 460000),
        }
        if owner_kind == "main":
            owner_pid = root_pid
        adapter_pid = 8095
        manifest = {
            "schema": 1,
            "adapter": "napcat",
            "accountId": "12345",
            "connected": True,
            "adapterPid": adapter_pid,
            "adapterCreationTime100ns": str(processes[adapter_pid].creation_time_100ns),
            "connectionGeneration": "a1b2c3d4",
            "updatedAtMs": 100,
        }
        config = {
            "adapter": "napcat",
            "onebot": {"url": "http://127.0.0.1:9999"},
            "paths": {"qq_exe": exe_path},
        }
        fixture = {
            "config": config,
            "manifest": manifest,
            "owner": owner_pid,
            "root": root_pid,
            "kernel": 30001,
            "module": str(module),
            "app": str(app),
        }
        return processes, fixture, module

    @staticmethod
    def _with_module_metadata(system: FixtureSystem, fixture: dict[str, object], module: Path) -> None:
        module_path = str(module)
        for pid in {fixture["root"], fixture["owner"], fixture["kernel"]}:
            system.module_paths[pid] = [module_path]

    def test_napcat_kernel_listener_and_python_reader_are_separate_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            system = FixtureSystem([fixture["owner"]], processes)
            target = system.bind_adapter(fixture["config"], fixture["manifest"])
            self.assertEqual(target.adapter_pid, 8095)
            self.assertEqual(target.adapter_creation_time_100ns, 800000)
            self.assertEqual(target.kernel_pid, 51200)
            self.assertNotEqual(target.adapter_pid, fixture["owner"])
            self.assertEqual(system.module_queries, [])

    def test_snowluma_main_listener_and_python_reader_are_separate_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "snowluma", "main")
            system = FixtureSystem([fixture["owner"]], processes)
            target = TargetBinding(9001, 900000, 50000, 500000, str(temporary))
            with patch.object(system, "_bind_snowluma_adapter", return_value=target) as bind_snowluma:
                result = system.bind_adapter(fixture["config"], fixture["manifest"])
            self.assertEqual(result, target)
            bind_snowluma.assert_called_once_with(fixture["config"], fixture["manifest"])
            self.assertEqual(system.module_queries, [])

    def test_stale_manifest_python_filetime_and_ambiguous_socket_owners_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            wrong_time = dict(fixture["manifest"])
            wrong_time["adapterCreationTime100ns"] = "123"
            system = FixtureSystem([fixture["owner"]], processes)
            with self.assertRaisesRegex(WindowsServiceError, "adapter_process_mismatch"):
                system.bind_adapter(fixture["config"], wrong_time)
            with self.assertRaisesRegex(WindowsServiceError, "target_ambiguous"):
                FixtureSystem([fixture["owner"], 50000], processes).bind_adapter(fixture["config"], fixture["manifest"])

    def test_node_service_ambiguity_is_rejected_instead_of_selecting_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            exe = str(Path(temporary) / "QQ.exe")
            app = str(Path(temporary) / "QQ App")
            processes[51201] = ProcessRecord(51201, 51144, exe,
                                             f'"{exe}" --app-path "{app}" --type=utility --utility-sub-type=node.mojom.nodeservice', 512100)
            with self.assertRaisesRegex(WindowsServiceError, "target_ambiguous"):
                FixtureSystem([fixture["owner"]], processes).bind_adapter(fixture["config"], fixture["manifest"])

    def test_flagless_utility_listener_and_main_listener_bind_from_qqnt_metadata(self) -> None:
        for owner_kind in ("utility", "main"):
            with self.subTest(owner_kind=owner_kind), tempfile.TemporaryDirectory() as temporary:
                processes, fixture, module = self._flagless_tree(Path(temporary), owner_kind)
                system = FixtureSystem([fixture["owner"]], processes)
                self._with_module_metadata(system, fixture, module)
                target = system.bind_adapter(fixture["config"], fixture["manifest"])
                self.assertEqual(target.kernel_pid, fixture["kernel"])
                self.assertEqual(Path(target.app_path), Path(fixture["app"]))
                self.assertEqual(system.module_queries, [fixture["root"], fixture["kernel"]])
                self.assertNotIn(30100, system.module_queries)
                self.assertNotIn(30101, system.module_queries)

    def test_explicit_app_path_conflicts_and_malformed_flags_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, app = self._tree(Path(temporary), "napcat", "kernel")
            owner = processes[fixture["owner"]]
            other = Path(temporary) / "Personal QQ"
            processes[owner.pid] = ProcessRecord(
                owner.pid,
                owner.parent_pid,
                owner.image_path,
                f'"{owner.image_path}" --app-path "{other}" --type=renderer',
                owner.creation_time_100ns,
            )
            with self.assertRaisesRegex(WindowsServiceError, "target_ambiguous"):
                FixtureSystem([owner.pid], processes).bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            root = processes[50000]
            processes[50000] = ProcessRecord(root.pid, root.parent_pid, root.image_path,
                                             f'"{root.image_path}" --app-path', root.creation_time_100ns)
            with self.assertRaisesRegex(WindowsServiceError, "target_app_path_unverified"):
                FixtureSystem([fixture["owner"]], processes).bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, _ = self._tree(Path(temporary), "napcat", "kernel")
            root = processes[50000]
            missing_path = Path(temporary) / "missing-app-path"
            processes[50000] = ProcessRecord(root.pid, root.parent_pid, root.image_path,
                                             f'"{root.image_path}" --app-path "{missing_path}"', root.creation_time_100ns)
            with self.assertRaisesRegex(WindowsServiceError, "target_app_path_unverified"):
                FixtureSystem([fixture["owner"]], processes).bind_adapter(fixture["config"], fixture["manifest"])

    def test_fallback_rejects_multiple_nodeservices_before_module_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            sibling = processes[30002]
            processes[30002] = ProcessRecord(
                sibling.pid,
                sibling.parent_pid,
                sibling.image_path,
                f'"{sibling.image_path}" --type=utility --utility-sub-type=node.mojom.nodeservice',
                sibling.creation_time_100ns,
            )
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with self.assertRaisesRegex(WindowsServiceError, "target_ambiguous"):
                system.bind_adapter(fixture["config"], fixture["manifest"])
            self.assertEqual(system.module_queries, [])

    def test_fallback_rejects_missing_multiple_and_outside_module_paths(self) -> None:
        for failure in ("missing", "multiple", "outside"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                processes, fixture, module = self._flagless_tree(Path(temporary))
                system = FixtureSystem([fixture["owner"]], processes)
                self._with_module_metadata(system, fixture, module)
                if failure == "missing":
                    system.module_paths[fixture["root"]] = []
                    expected = "target_module_missing"
                elif failure == "multiple":
                    system.module_paths[fixture["root"]] = [str(module), str(module)]
                    expected = "target_module_ambiguous"
                else:
                    outside = Path(temporary) / "outside" / "QQNT.dll"
                    outside.parent.mkdir()
                    outside.write_bytes(b"fixture")
                    system.module_paths[fixture["root"]] = [str(outside)]
                    expected = "target_module_outside_install"
                with self.assertRaisesRegex(WindowsServiceError, expected):
                    system.bind_adapter(fixture["config"], fixture["manifest"])

    def test_fallback_rejects_inconsistent_qqnt_metadata_across_pinned_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            other_version = module.parent.parent / "fixture-version-b"
            other_app = other_version / "resources" / "app"
            (other_app / "avsdk").mkdir(parents=True)
            (other_app / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"fixture")
            other_module = other_version / "QQNT.dll"
            other_module.write_bytes(b"fixture")
            system.module_paths[fixture["kernel"]] = [str(other_module)]
            with self.assertRaisesRegex(WindowsServiceError, "target_module_mismatch"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

    def test_fallback_rejects_missing_avsdk_and_module_access_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            (Path(fixture["app"]) / "avsdk" / "AVSDKPlugin.dll").unlink()
            with self.assertRaisesRegex(WindowsServiceError, "target_app_path_unverified"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            system.module_errors[fixture["root"]] = WindowsServiceError("target_module_access_denied")
            with self.assertRaisesRegex(WindowsServiceError, "target_module_access_denied"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

    def test_fallback_rechecks_process_identity_image_and_listener_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            different_image = str(Path(temporary) / "other.exe")
            system.identity_overrides[fixture["kernel"]] = (
                processes[fixture["kernel"]].creation_time_100ns,
                different_image,
            )
            with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            record = processes[fixture["kernel"]]
            system.identity_overrides[fixture["kernel"]] = (
                record.creation_time_100ns + 1,
                record.image_path,
            )
            with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            system.after_module_query = lambda current, _pid: current.owners.__setitem__(slice(None), [30002])
            with self.assertRaisesRegex(WindowsServiceError, "adapter_listener_mismatch"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

    def test_fallback_rejects_bad_ancestry_and_ignores_unrelated_qq_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            root = processes[fixture["root"]]
            processes[root.pid] = ProcessRecord(root.pid, fixture["owner"], root.image_path,
                                                root.command_line, root.creation_time_100ns)
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with self.assertRaisesRegex(WindowsServiceError, "target_process_tree_invalid"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            owner = processes[fixture["owner"]]
            processes[owner.pid] = ProcessRecord(owner.pid, 999999, owner.image_path,
                                                 owner.command_line, owner.creation_time_100ns)
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with self.assertRaisesRegex(WindowsServiceError, "target_process_tree_invalid"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            parent_pid = fixture["root"]
            for index in range(65):
                pid = 10000 + index
                creation = processes[fixture["root"]].creation_time_100ns + 1000 * (index + 1)
                processes[pid] = ProcessRecord(pid, parent_pid, processes[fixture["root"]].image_path,
                                               '"QQ.exe" --type=renderer', creation)
                parent_pid = pid
            owner = processes[fixture["owner"]]
            processes[owner.pid] = ProcessRecord(owner.pid, parent_pid, owner.image_path,
                                                 owner.command_line, owner.creation_time_100ns)
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with self.assertRaisesRegex(WindowsServiceError, "target_process_tree_invalid"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            owner = processes[fixture["owner"]]
            processes[owner.pid] = ProcessRecord(owner.pid, owner.parent_pid, owner.image_path,
                                                 owner.command_line, processes[fixture["root"]].creation_time_100ns - 1)
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            with self.assertRaisesRegex(WindowsServiceError, "target_process_tree_invalid"):
                system.bind_adapter(fixture["config"], fixture["manifest"])

        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            system.module_paths[30100] = []
            target = system.bind_adapter(fixture["config"], fixture["manifest"])
            self.assertEqual(target.kernel_pid, fixture["kernel"])
            self.assertNotIn(30100, system.module_queries)
            self.assertNotIn(30101, system.module_queries)

    def test_revalidation_detects_a_qqnt_version_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            target = system.bind_adapter(fixture["config"], fixture["manifest"])

            newer = module.parent.parent / "fixture-version-b"
            newer_app = newer / "resources" / "app"
            (newer_app / "avsdk").mkdir(parents=True)
            (newer_app / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"fixture")
            newer_module = newer / "QQNT.dll"
            newer_module.write_bytes(b"fixture")
            self._with_module_metadata(system, fixture, newer_module)

            self.assertFalse(system.revalidate_binding(fixture["config"], fixture["manifest"], target))

    def test_revalidation_detects_an_adapter_pid_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            target = system.bind_adapter(fixture["config"], fixture["manifest"])
            new_manifest = dict(fixture["manifest"])
            new_manifest["adapterPid"] = 9001
            new_manifest["adapterCreationTime100ns"] = str(processes[9001].creation_time_100ns)
            self.assertFalse(system.revalidate_binding(fixture["config"], new_manifest, target))

    def test_revalidation_detects_a_kernel_pid_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            processes, fixture, module = self._flagless_tree(Path(temporary))
            system = FixtureSystem([fixture["owner"]], processes)
            self._with_module_metadata(system, fixture, module)
            target = system.bind_adapter(fixture["config"], fixture["manifest"])

            old_kernel = processes[fixture["kernel"]]
            processes[old_kernel.pid] = ProcessRecord(old_kernel.pid, old_kernel.parent_pid, old_kernel.image_path,
                                                      f'"{old_kernel.image_path}" --type=renderer',
                                                      old_kernel.creation_time_100ns)
            sibling = processes[30002]
            processes[sibling.pid] = ProcessRecord(sibling.pid, sibling.parent_pid, sibling.image_path,
                                                    f'"{sibling.image_path}" --type=utility --utility-sub-type=node.mojom.nodeservice',
                                                    sibling.creation_time_100ns)
            system.owners = [sibling.pid]
            system.module_paths[sibling.pid] = [str(module)]

            self.assertFalse(system.revalidate_binding(fixture["config"], fixture["manifest"], target))

    def _host_ownership_fixture(self, root: Path):
        exe = root / "QQ.exe"
        launcher_exe = root / "loader" / "NapCatWinBootMain.exe"
        launcher_exe.parent.mkdir(parents=True)
        exe.touch()
        launcher_exe.touch()
        profile = root / "runtime" / "host-41" / "profile"
        report = root / "runtime" / "host-41" / "host"
        profile.mkdir(parents=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        processes = {
            7000: ProcessRecord(7000, 1, str(launcher_exe), f'"{launcher_exe}"', 70000),
            # The authoritative sidecar PID has no --user-data-dir flag.
            8100: ProcessRecord(8100, 7000, str(exe), f'"{exe}" --no-sandbox', 81000),
            8101: ProcessRecord(8101, 8100, str(exe), f'"{exe}" --user-data-dir "{profile}" --type=renderer', 81010),
            8102: ProcessRecord(8102, 8100, str(exe), f'"{exe}" --user-data-dir "{profile}" --type=utility', 81020),
            8110: ProcessRecord(8110, 8102, str(exe), f'"{exe}" --user-data-dir "{profile}" --type=renderer', 81030),
            # Same and different profile paths outside the pinned launcher's tree are never owned.
            8200: ProcessRecord(8200, 1, str(exe), f'"{exe}" --user-data-dir "{profile}"', 82000),
            8300: ProcessRecord(8300, 1, str(exe), f'"{exe}" --user-data-dir "{root / "personal-profile"}"', 83000),
        }
        sidecar_path = Path(str(report) + ".host.config.json")
        sidecar_path.write_text(json.dumps({
            "targetPid": 8100,
            "hostGeneration": 41,
            "listenPort": 6111,
            "outputPort": 6110,
            "profilePath": str(profile),
        }), encoding="utf-8")
        config = {"paths": {"qq_exe": str(exe), "loader_root": str(launcher_exe.parent)},
                  "ports": {"host": 6111, "callback": 6110}}
        launch = HostLaunch(
            7000, str(report.parent), str(profile), str(report),
            host_generation=41,
            launcher_creation_time_100ns=70000,
            launcher_image_path=str(launcher_exe),
        )
        return FixtureSystem([], processes), launch, config, sidecar_path

    def test_owned_host_uses_sidecar_pinned_descendant_without_profile_flag_and_excludes_external_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system, launch, config, _sidecar = self._host_ownership_fixture(root)
            owned = system.owned_host_processes(launch, config)
            self.assertEqual({item["pid"] for item in owned}, {7000, 8100, 8101, 8102, 8110})
            self.assertEqual(next(item["role"] for item in owned if item["pid"] == 8100), "host")
            self.assertEqual(next(item["role"] for item in owned if item["pid"] == 8101), "host_child")

    def test_owned_host_fails_closed_for_launcher_pid_reuse_or_missing_pinned_lineage(self) -> None:
        for scenario in ("reused", "missing"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                system, launch, config, _sidecar = self._host_ownership_fixture(root)
                if scenario == "reused":
                    launcher = system.processes[7000]
                    system.processes[7000] = ProcessRecord(
                        launcher.pid, launcher.parent_pid, launcher.image_path, launcher.command_line, 70001,
                    )
                    expected = "host_launcher_identity_mismatch"
                else:
                    system.processes.pop(7000)
                    expected = "host_launcher_lineage_unavailable"
                with self.assertRaisesRegex(WindowsServiceError, expected):
                    system.owned_host_processes(launch, config)

    def test_owned_host_rejects_sidecar_target_generation_or_profile_conflict(self) -> None:
        for field, value, expected in (
            ("targetPid", 8101, "host_sidecar_binding_mismatch"),
            ("hostGeneration", 42, "host_sidecar_binding_mismatch"),
            ("profilePath", "C:\\foreign\\profile", "host_sidecar_binding_mismatch"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                system, launch, config, sidecar = self._host_ownership_fixture(root)
                value_doc = json.loads(sidecar.read_text(encoding="utf-8"))
                value_doc[field] = value
                sidecar.write_text(json.dumps(value_doc), encoding="utf-8")
                with self.assertRaisesRegex(WindowsServiceError, expected):
                    system.owned_host_processes(launch, config)

    def test_owned_host_rejects_unpinned_profile_root_and_explicit_profile_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system, launch, config, _sidecar = self._host_ownership_fixture(root)
            main = system.processes[8100]
            system.processes[8100] = ProcessRecord(
                main.pid, main.parent_pid, main.image_path,
                f'"{main.image_path}" --user-data-dir "{root / "conflicting-profile"}"', main.creation_time_100ns,
            )
            with self.assertRaisesRegex(WindowsServiceError, "host_profile_conflict"):
                system.owned_host_processes(launch, config)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system, launch, config, _sidecar = self._host_ownership_fixture(root)
            main = system.processes.pop(8100)
            system.processes[8100] = ProcessRecord(main.pid, 1, main.image_path,
                                                    f'"{main.image_path}" --user-data-dir "{launch.profile_path}"',
                                                    main.creation_time_100ns)
            with self.assertRaisesRegex(WindowsServiceError, "host_root_not_launcher_descendant"):
                system.owned_host_processes(launch, config)

    def test_owned_host_records_validated_depth_through_non_host_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system, launch, config, _sidecar = self._host_ownership_fixture(root)
            launcher = system.processes[7000]
            host = system.processes[8100]
            exe = config["paths"]["qq_exe"]
            helper_image = str(root / "helper.exe")
            system.processes = {
                7000: launcher,
                8100: host,
                9900: ProcessRecord(9900, 8100, exe, f'"{exe}" --type=ppapi', 90000),
                9001: ProcessRecord(9001, 8100, helper_image, f'"{helper_image}"', 91000),
                11: ProcessRecord(11, 9001, exe, f'"{exe}" --type=ppapi-broker', 92000),
            }

            owned = system.owned_host_processes(launch, config)
            by_pid = {item["pid"]: item for item in owned}
            self.assertEqual(by_pid[8100]["depth"], 1)
            self.assertEqual(by_pid[9900]["depth"], 2)
            self.assertEqual(by_pid[11]["depth"], 3)
            self.assertEqual(by_pid[11]["parentPid"], 9001)
            self.assertEqual(by_pid[11]["parentCreationTime100ns"], "91000")
            self.assertEqual(by_pid[9900]["processType"], "ppapi")
            self.assertEqual(by_pid[11]["processType"], "ppapi-broker")
            self.assertNotIn("commandLine", by_pid[11])
            self.assertNotIn("rawCommandLine", by_pid[11])

    def test_owned_host_rejects_invalid_intermediate_edge_and_omits_unlinked_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system, launch, config, _sidecar = self._host_ownership_fixture(root)
            launcher = system.processes[7000]
            host = system.processes[8100]
            exe = config["paths"]["qq_exe"]
            helper_image = str(root / "helper.exe")
            system.processes = {
                7000: launcher,
                8100: host,
                9001: ProcessRecord(9001, 8100, helper_image, f'"{helper_image}"', 91000),
                11: ProcessRecord(11, 9001, exe, f'"{exe}" --type=renderer', 90999),
            }
            with self.assertRaisesRegex(WindowsServiceError, "host_process_tree_invalid"):
                system.owned_host_processes(launch, config)

            system.processes.pop(11)
            system.processes[12] = ProcessRecord(12, 7777, exe, f'"{exe}" --type=renderer', 92000)
            owned = system.owned_host_processes(launch, config)
            self.assertNotIn(12, {item["pid"] for item in owned})


def _owned_cleanup_record(
    role: str,
    pid: int,
    creation: int,
    image: str,
    *,
    depth: int | None = None,
    process_type: str = "other",
    parent_pid: int = 1,
    parent_creation: int = 1,
) -> dict[str, object]:
    value: dict[str, object] = {
        "role": role,
        "pid": pid,
        "creationTime100ns": str(creation),
        "imagePath": image,
        "profilePath": "C:\\owned-host-profile",
    }
    if depth is not None:
        value.update({
            "depth": depth,
            "parentPid": parent_pid,
            "parentCreationTime100ns": str(parent_creation),
            "processType": process_type,
        })
    return value


class WindowsOwnedTerminationTests(unittest.TestCase):
    launcher_image = "C:\\loader\\NapCatWinBootMain.exe"
    host_image = "C:\\QQ\\QQ.exe"

    def _tree(self) -> tuple[list[dict[str, object]], dict[int, tuple[int, str]]]:
        records = [
            _owned_cleanup_record("launcher", 7000, 70000, self.launcher_image),
            _owned_cleanup_record("host", 8100, 81000, self.host_image, depth=1, parent_pid=7000, parent_creation=70000),
            _owned_cleanup_record("host_child", 9900, 90000, self.host_image, depth=2, process_type="ppapi", parent_pid=8100, parent_creation=81000),
        ]
        identities = {item["pid"]: (int(item["creationTime100ns"]), item["imagePath"]) for item in records}
        return records, identities

    def test_cleanup_orders_by_depth_then_ppapi_type_not_pid(self) -> None:
        records, identities = self._tree()
        records.extend([
            _owned_cleanup_record("host_child", 500, 90100, self.host_image, depth=3, process_type="renderer", parent_pid=9900, parent_creation=90000),
            _owned_cleanup_record("host_child", 600, 90200, self.host_image, depth=3, process_type="ppapi-broker", parent_pid=9900, parent_creation=90000),
            _owned_cleanup_record("host_child", 1000, 90300, self.host_image, depth=2, process_type="ppapi-broker", parent_pid=8100, parent_creation=81000),
            _owned_cleanup_record("host_child", 1200, 90400, self.host_image, depth=2, process_type="renderer", parent_pid=8100, parent_creation=81000),
        ])
        identities.update({item["pid"]: (int(item["creationTime100ns"]), item["imagePath"]) for item in records})
        system = _TerminationFixtureSystem(identities)

        self.assertTrue(system.terminate_owned(records))
        terminated = [pid for action, pid in system.kernel32.events if action == "terminate"]
        self.assertEqual(terminated, [600, 500, 9900, 1000, 1200, 8100, 7000])

    def test_unavailable_child_identity_blocks_root_and_launcher(self) -> None:
        records, identities = self._tree()
        child = next(item for item in records if item["role"] == "host_child")
        identities.pop(child["pid"])
        system = _TerminationFixtureSystem(identities, current_states={child["pid"]: None})

        self.assertFalse(system.terminate_owned(records))
        self.assertEqual([pid for action, pid in system.kernel32.events if action == "terminate"], [])

    def test_confirmed_gone_children_do_not_spend_one_powershell_timeout_each(self) -> None:
        records, identities = self._tree()
        records.extend([
            _owned_cleanup_record("host_child", 500, 90100, self.host_image, depth=3,
                                  process_type="renderer", parent_pid=9900, parent_creation=90000),
            _owned_cleanup_record("host_child", 600, 90200, self.host_image, depth=3,
                                  process_type="ppapi-broker", parent_pid=9900, parent_creation=90000),
            _owned_cleanup_record("host_child", 1000, 90300, self.host_image, depth=2,
                                  process_type="renderer", parent_pid=8100, parent_creation=81000),
        ])
        gone_pids = {item["pid"] for item in records if item["role"] == "host_child"}
        for pid in gone_pids:
            identities.pop(pid, None)
        # This unrelated live process is deliberately absent from the owned records.
        identities[4321] = (432100, self.host_image)
        system = _TerminationFixtureSystem(identities, open_failures=gone_pids)

        with patch.object(system, "_run_powershell", side_effect=AssertionError("unexpected process query")):
            with patch("service.windows.ctypes.get_last_error", return_value=87, create=True):
                self.assertTrue(system.terminate_owned(records))

        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [8100, 7000],
        )
        self.assertNotIn(4321, [pid for action, pid in system.kernel32.events if action == "terminate"])

    def test_access_denied_on_an_unqueryable_child_blocks_ancestors(self) -> None:
        records, identities = self._tree()
        child_pid = next(item["pid"] for item in records if item["role"] == "host_child")
        identities.pop(child_pid)
        system = _TerminationFixtureSystem(identities, open_failures={child_pid})

        with patch("service.windows.ctypes.get_last_error", return_value=5, create=True):
            self.assertFalse(system.terminate_owned(records))

        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [],
        )

    def test_fresh_handle_filetime_distinguishes_pid_reuse_from_live_child(self) -> None:
        records, identities = self._tree()
        child = next(item for item in records if item["role"] == "host_child")
        child_pid = child["pid"]
        expected_creation = int(child["creationTime100ns"])
        identities.pop(child_pid)
        reused = _TerminationFixtureSystem(
            identities,
            handle_identities={**identities, child_pid: (expected_creation + 1, self.host_image)},
        )
        self.assertTrue(reused.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in reused.kernel32.events if action == "terminate"],
            [8100, 7000],
        )

        records, identities = self._tree()
        identities.pop(child_pid)
        still_live = _TerminationFixtureSystem(
            identities,
            handle_identities={**identities, child_pid: (expected_creation, self.host_image)},
            wait_results={child_pid: [_FakeKernel32.WAIT_TIMEOUT]},
        )
        self.assertFalse(still_live.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in still_live.kernel32.events if action == "terminate"],
            [],
        )

    def test_child_termination_or_wait_failure_blocks_ancestors(self) -> None:
        for failure in ("terminate", "wait"):
            with self.subTest(failure=failure):
                records, identities = self._tree()
                child_pid = 9900
                if failure == "terminate":
                    system = _TerminationFixtureSystem(
                        identities, terminate_results={child_pid: False},
                        wait_results={child_pid: [_FakeKernel32.WAIT_TIMEOUT]},
                    )
                else:
                    system = _TerminationFixtureSystem(
                        identities, wait_results={child_pid: [_FakeKernel32.WAIT_TIMEOUT]},
                    )
                self.assertFalse(system.terminate_owned(records))
                self.assertEqual(
                    [pid for action, pid in system.kernel32.events if action == "terminate"],
                    [child_pid],
                )

    def test_root_failure_preserves_launcher(self) -> None:
        records, identities = self._tree()
        system = _TerminationFixtureSystem(
            identities, terminate_results={8100: False},
            wait_results={8100: [_FakeKernel32.WAIT_TIMEOUT]},
        )

        self.assertFalse(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [9900, 8100],
        )

    def test_all_waits_share_one_five_second_deadline(self) -> None:
        records, identities = self._tree()
        system = _TerminationFixtureSystem(identities)
        now = [100.0]
        wait_timeouts: list[int] = []
        wait_cost_seconds = {9900: 2.0, 8100: 2.0, 7000: 2.0}

        def monotonic() -> float:
            return now[0]

        def wait_for_process(handle: int, timeout_ms: int) -> int:
            pid = system.kernel32.handles[handle]
            system.kernel32.events.append(("wait", pid))
            wait_timeouts.append(timeout_ms)
            remaining = timeout_ms / 1000.0
            needed = wait_cost_seconds[pid]
            if needed > remaining:
                now[0] += remaining
                return _FakeKernel32.WAIT_TIMEOUT
            now[0] += needed
            return _FakeKernel32.WAIT_OBJECT_0

        system.kernel32.WaitForSingleObject.callback = wait_for_process
        with patch("service.windows.time.monotonic", side_effect=monotonic):
            self.assertFalse(system.terminate_owned(records))

        self.assertEqual(wait_timeouts, [5000, 3000, 1000])
        self.assertEqual(now[0], 105.0)
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [9900, 8100, 7000],
        )

    def test_absent_or_reused_child_is_skipped_and_root_reuse_never_kills_new_pid(self) -> None:
        records, identities = self._tree()
        child_pid = next(item["pid"] for item in records if item["role"] == "host_child")
        identities.pop(child_pid)
        system = _TerminationFixtureSystem(identities, current_states={child_pid: False})
        self.assertTrue(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [8100, 7000],
        )

        records, identities = self._tree()
        identities[9900] = (99900, self.host_image)
        system = _TerminationFixtureSystem(identities)
        self.assertTrue(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [8100, 7000],
        )

        records, identities = self._tree()
        identities[8100] = (99999, self.host_image)
        system = _TerminationFixtureSystem(identities)
        self.assertTrue(system.terminate_owned([records[0], records[1]]))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [7000],
        )

    def test_open_handle_filetime_and_image_must_match_before_termination(self) -> None:
        records, identities = self._tree()
        child = next(item for item in records if item["role"] == "host_child")
        reused_handle = dict(identities)
        reused_handle[child["pid"]] = (99900, self.host_image)
        system = _TerminationFixtureSystem(identities, handle_identities=reused_handle)
        self.assertTrue(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [8100, 7000],
        )

        records, identities = self._tree()
        child = next(item for item in records if item["role"] == "host_child")
        wrong_image = dict(identities)
        wrong_image[child["pid"]] = (identities[child["pid"]][0], "C:\\personal\\QQ.exe")
        system = _TerminationFixtureSystem(identities, handle_identities=wrong_image)
        self.assertFalse(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [],
        )

    def test_legacy_records_keep_children_before_root_and_launcher_without_depth(self) -> None:
        records, identities = self._tree()
        broker = _owned_cleanup_record(
            "host_child", 9901, 90100, self.host_image, process_type="ppapi-broker",
        )
        renderer = _owned_cleanup_record(
            "host_child", 9902, 90200, self.host_image, process_type="renderer",
        )
        broker["processType"] = "ppapi-broker"
        renderer["processType"] = "renderer"
        records.extend((broker, renderer))
        identities.update({9901: (90100, self.host_image), 9902: (90200, self.host_image)})
        for item in records:
            item.pop("depth", None)
            item.pop("parentPid", None)
            item.pop("parentCreationTime100ns", None)
        system = _TerminationFixtureSystem(identities)

        self.assertTrue(system.terminate_owned(records))
        self.assertEqual(
            [pid for action, pid in system.kernel32.events if action == "terminate"],
            [9900, 9901, 9902, 8100, 7000],
        )


class WindowsNamedPipeEnumerationTests(unittest.TestCase):
    def test_snapshot_uses_exact_enumerated_names_when_exists_reports_false(self) -> None:
        api = _FakePipeFindKernel32([
            "mojo.93160.control",
            "mojo.93160.recv",
            "mojo.931600.control",
            "mojo.93160.control-extra",
            "some-other-pipe",
        ])
        system = WindowsSystem()
        with (
            patch.object(windows_module.ctypes, "WinDLL", return_value=api, create=True),
            patch.object(windows_module.os.path, "exists", return_value=False),
        ):
            self.assertEqual(system._named_pipe_snapshot(93160), frozenset(("control", "recv")))
            self.assertFalse(system._named_pipe_exists(93161, "control"))
        self.assertEqual(api.patterns, [r"\\.\pipe\*", r"\\.\pipe\*"])
        self.assertTrue(api.closed)

    def test_empty_inventory_is_distinct_from_unexpected_enumeration_errors(self) -> None:
        system = WindowsSystem()
        empty = _FakePipeFindKernel32([], first_error=2)
        with patch.object(windows_module.ctypes, "WinDLL", return_value=empty, create=True):
            self.assertEqual(system._named_pipe_snapshot(93160), frozenset())

        for api in (
            _FakePipeFindKernel32(["mojo.93160.control"], end_error=0),
            _FakePipeFindKernel32(["mojo.93160.control"], end_error=5),
            _FakePipeFindKernel32(["mojo.93160.control"], close_result=False),
            _FakePipeFindKernel32(["mojo.93160.control"] * 2),
            _FakePipeFindKernel32([], first_error=5),
        ):
            with self.subTest(api=api), patch.object(
                windows_module.ctypes, "WinDLL", return_value=api, create=True,
            ), self.assertRaisesRegex(WindowsServiceError, "target_process_unavailable"):
                system._named_pipe_snapshot(93160)
            if api.index >= 0:
                self.assertTrue(api.closed)

    def test_inventory_limit_fails_closed_and_releases_enumerator(self) -> None:
        api = _FakePipeFindKernel32([
            f"unrelated-{index}" for index in range(windows_module._NAMED_PIPE_ENUMERATION_LIMIT + 1)
        ])
        with patch.object(windows_module.ctypes, "WinDLL", return_value=api, create=True):
            with self.assertRaisesRegex(WindowsServiceError, "target_process_unavailable"):
                WindowsSystem()._named_pipe_snapshot(93160)
        self.assertTrue(api.closed)


def _wrapper_stat(
    *,
    size: int,
    mtime: int = 300,
    ctime: int = 100,
    birthtime: int = 50,
    device: int = 11,
    inode: int = 22,
):
    return SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_size=size,
        st_mtime_ns=mtime,
        st_ctime_ns=ctime,
        st_birthtime_ns=birthtime,
        st_dev=device,
        st_ino=inode,
        st_file_attributes=0,
    )


class SnowLumaWrapperDigestTests(unittest.TestCase):
    def _run_digest(self, system, path: Path, path_stats, handle_stats) -> str:
        with (
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "lstat", side_effect=path_stats),
            patch.object(windows_module.os, "fstat", side_effect=handle_stats) as fstat,
        ):
            result = system._snowluma_wrapper_digest(path)
        self.assertEqual(fstat.call_count, len(handle_stats))
        return result

    def test_path_and_handle_ctime_semantics_may_differ_but_identity_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrapper.node"
            content = b"pinned wrapper test bytes"
            path.write_bytes(content)
            size = len(content)
            path_stat = _wrapper_stat(size=size, ctime=100, birthtime=50)
            handle_stat = _wrapper_stat(size=size, ctime=300, birthtime=50)
            system = WindowsSystem()

            digest = self._run_digest(
                system,
                path,
                [path_stat, path_stat],
                [handle_stat, handle_stat],
            )

        self.assertEqual(digest, hashlib.sha256(content).hexdigest().upper())

    def test_cache_key_uses_path_ctime_but_hash_still_checks_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrapper.node"
            content = b"pinned wrapper test bytes"
            path.write_bytes(content)
            size = len(content)
            system = WindowsSystem()
            handle_stat = _wrapper_stat(size=size, ctime=300, birthtime=50)

            first_digest = self._run_digest(
                system,
                path,
                [_wrapper_stat(size=size, ctime=100), _wrapper_stat(size=size, ctime=100)],
                [handle_stat, handle_stat],
            )
            second_digest = self._run_digest(
                system,
                path,
                [_wrapper_stat(size=size, ctime=101), _wrapper_stat(size=size, ctime=101)],
                [handle_stat, handle_stat],
            )

        self.assertEqual(first_digest, second_digest)

    def test_inode_must_be_available_and_path_identity_changes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrapper.node"
            path.write_bytes(b"pinned wrapper test bytes")
            size = path.stat().st_size
            system = WindowsSystem()
            unchanged = _wrapper_stat(size=size)

            with (
                patch.object(Path, "is_symlink", return_value=False),
                patch.object(Path, "lstat", side_effect=[_wrapper_stat(size=size, inode=0)]),
                self.assertRaisesRegex(WindowsServiceError, "target_module_missing"),
            ):
                system._snowluma_wrapper_digest(path)

            changed_path = _wrapper_stat(size=size, inode=23)
            with (
                patch.object(Path, "is_symlink", return_value=False),
                patch.object(Path, "lstat", side_effect=[unchanged, changed_path]),
                patch.object(windows_module.os, "fstat", side_effect=[unchanged, unchanged]),
                self.assertRaisesRegex(WindowsServiceError, "target_module_missing"),
            ):
                system._snowluma_wrapper_digest(path)

    def test_device_inode_size_mtime_and_birthtime_must_match_open_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrapper.node"
            content = b"pinned wrapper test bytes"
            path.write_bytes(content)
            base = _wrapper_stat(size=len(content))
            variants = {
                "device": _wrapper_stat(size=len(content), device=12),
                "inode": _wrapper_stat(size=len(content), inode=23),
                "size": _wrapper_stat(size=len(content) + 1),
                "mtime": _wrapper_stat(size=len(content), mtime=301),
                "birthtime": _wrapper_stat(size=len(content), birthtime=51),
            }
            for field, changed in variants.items():
                with (
                    self.subTest(field=field),
                    patch.object(Path, "is_symlink", return_value=False),
                    patch.object(Path, "lstat", side_effect=[base]),
                    patch.object(windows_module.os, "fstat", side_effect=[changed]),
                    self.assertRaisesRegex(WindowsServiceError, "target_module_missing"),
                ):
                    WindowsSystem()._snowluma_wrapper_digest(path)


if __name__ == "__main__":
    unittest.main()
