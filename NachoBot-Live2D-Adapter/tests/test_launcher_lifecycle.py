"""Exercise launcher ownership with isolated local child processes."""

from __future__ import annotations

import json
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import ctypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live2d_adapter.config import ConfigError, load_config
from live2d_adapter.launcher import (
    CREATE_NO_WINDOW,
    LaunchIdentity,
    LaunchOutcome,
    LaunchSession,
    LauncherError,
    ManagedProcess,
    OwnedServices,
    SessionSupervisor,
    WindowsJob,
    _JobBasicAccountingInformation,
    follow_live_session,
    _parser,
    launch,
    listener_pid,
    main,
    _process_creation_ticks,
)


ROOT = Path(__file__).resolve().parents[1]


class ConsoleConfigTests(unittest.TestCase):
    def test_stop_waits_for_owned_tcp_table_entry_to_disappear(self) -> None:
        job = Mock(handle=1)
        job.wait_until_empty.return_value = True
        process = Mock()
        managed = ManagedProcess("core", process, job, Path("out"), Path("err"),
                                 listener_port=12345, listener_owner=55)
        with patch("live2d_adapter.launcher.listener_pid", side_effect=[55, 55, None]) as owner, \
             patch("live2d_adapter.launcher.time.sleep") as delay:
            managed.stop()
        self.assertEqual(owner.call_count, 3)
        self.assertEqual(delay.call_count, 2)
        job.close.assert_called_once()

    def test_console_defaults_and_independent_switches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            for extra, expected in (
                ("", (False, False)),
                ("show_desktop_console = true", (True, False)),
                ("show_core_console = true", (False, True)),
                ("show_desktop_console = true\nshow_core_console = true", (True, True)),
            ):
                config_path.write_text(
                    '[renderer]\nmodel_path = "avatar.model3.json"\n[desktop_pet]\n' + extra,
                    encoding="utf-8",
                )
                config = load_config(config_path)
                self.assertEqual(
                    (config.desktop_pet.show_desktop_console, config.desktop_pet.show_core_console),
                    expected,
                )
            config_path.write_text(
                '[renderer]\nmodel_path = "avatar.model3.json"\n'
                '[desktop_pet]\nshow_desktop_console = "false"',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config_path)


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_listener(port: int, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owner = listener_pid(port)
        if owner is not None:
            return owner
        time.sleep(0.05)
    raise AssertionError(f"No listener on port {port}")


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _write_fixture(directory: Path) -> Path:
    path = directory / "service_fixture.py"
    path.write_text(
        "import socket, subprocess, sys, time\n"
        "port, lifetime = int(sys.argv[1]), float(sys.argv[2])\n"
        "if port:\n"
        "    listener = socket.socket()\n"
        "    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    listener.bind(('127.0.0.1', port))\n"
        "    listener.listen()\n"
        "if len(sys.argv) > 3:\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    open(sys.argv[3], 'w', encoding='ascii').write(str(child.pid))\n"
        "time.sleep(lifetime)\n",
        encoding="utf-8",
    )
    return path


@unittest.skipUnless(os.name == "nt", "Windows Job Objects and TCP owner tables")
class LauncherLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="live2d launcher ")
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.fixture = _write_fixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start_service(
        self,
        services: OwnedServices,
        name: str,
        port: int,
        seconds: float = 20,
        *,
        child_pid_file: Path | None = None,
    ) -> ManagedProcess:
        command = [sys.executable, str(self.fixture), str(port), str(seconds)]
        if child_pid_file is not None:
            command.append(str(child_pid_file))
        process = services.start(
            name,
            command,
            port=port,
            cwd=self.root,
            timeout=3,
        )
        self.assertIsNotNone(process)
        return process  # type: ignore[return-value]

    def test_job_assignment_precedes_venv_python_execution_and_cleans_descendants(self) -> None:
        child_pid_file = self.root / "child.pid"
        managed = ManagedProcess.start(
            "redirector-fixture",
            [sys.executable, str(self.fixture), "0", "60", str(child_pid_file)],
            cwd=self.root,
            stdout_path=self.logs / "fixture.out",
            stderr_path=self.logs / "fixture.err",
        )
        try:
            deadline = time.monotonic() + 5
            child_pid_text = ""
            while not child_pid_text and time.monotonic() < deadline:
                try:
                    child_pid_text = child_pid_file.read_text(encoding="ascii").strip()
                except OSError:
                    pass
                time.sleep(0.05)
            self.assertTrue(child_pid_text, "suspended Python process did not resume")
            child_pid = int(child_pid_text)
            self.assertTrue(managed.job.contains(child_pid), "grandchild escaped its owned Job Object")
        finally:
            managed.stop()
        self.assertIsNotNone(managed.poll())
        deadline = time.monotonic() + 5
        while _process_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_process_is_running(child_pid), "owned descendant survived Job Object cleanup")

    def test_normal_pet_exit_cleans_only_launcher_owned_core(self) -> None:
        services = OwnedServices(self.logs)
        core_port, pet_port = _unused_port(), _unused_port()
        core = self.start_service(services, "core", core_port)
        pet = self.start_service(services, "pet", pet_port, seconds=1.5)
        self.assertTrue(core.job.contains(_wait_for_listener(core_port)))
        pet.process.wait(timeout=5)
        services.stop()
        self.assertIsNotNone(core.poll())
        self.assertIsNone(listener_pid(core_port))
        self.assertIsNone(listener_pid(pet_port))

    def test_closed_console_stops_pet_owned_core_and_preserves_external_core(self) -> None:
        services = OwnedServices(self.logs)
        external = OwnedServices(self.root / "external-logs")
        core_port, pet_port, external_port = _unused_port(), _unused_port(), _unused_port()
        try:
            core = self.start_service(services, "core", core_port)
            pet = self.start_service(services, "pet", pet_port)
            other = self.start_service(external, "existing-core", external_port)
            viewer = subprocess.Popen(
                [getattr(sys, "_base_executable", sys.executable), "-c", "pass"],
                creationflags=CREATE_NO_WINDOW,
            )
            viewer.wait(timeout=5)
            supervisor = SessionSupervisor(self.root)
            supervisor.session = LaunchSession(
                "desktop_pet", Mock(), pet, services, self.root / "config.toml",
                log_viewers=[Mock(poll=Mock(return_value=None)), viewer],
            )
            with patch("sys.stdout", io.StringIO()):
                self.assertTrue(supervisor._stop_for_closed_console())
            self.assertIsNone(supervisor.session)
            self.assertIsNotNone(pet.poll())
            self.assertIsNotNone(core.poll())
            self.assertIsNone(listener_pid(pet_port))
            self.assertIsNone(listener_pid(core_port))
            self.assertIsNone(other.poll())
            self.assertIsNotNone(listener_pid(external_port))
        finally:
            services.stop()
            external.stop()

    def test_closing_original_launcher_stops_its_background_session(self) -> None:
        services = OwnedServices(self.logs)
        client = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time;time.sleep(60)"],
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            pet = self.start_service(services, "pet", _unused_port())
            core = self.start_service(services, "core", _unused_port())
            supervisor = SessionSupervisor(self.root)
            supervisor.session = LaunchSession(
                "desktop_pet", Mock(), pet, services, self.root / "config.toml",
                console_owners=[(client.pid, _process_creation_ticks(client.pid))],
            )
            self.assertFalse(supervisor._stop_for_closed_console())
            client.terminate()
            client.wait(timeout=5)
            with patch("sys.stdout", io.StringIO()):
                self.assertTrue(supervisor._stop_for_closed_console())
            self.assertIsNotNone(pet.poll())
            self.assertIsNotNone(core.poll())
        finally:
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=5)
            services.stop()

    def test_forced_pet_exit_cleans_owned_core(self) -> None:
        services = OwnedServices(self.logs)
        core_port, pet_port = _unused_port(), _unused_port()
        core = self.start_service(services, "core", core_port)
        pet = self.start_service(services, "pet", pet_port)
        pet.stop()
        services.stop()
        self.assertIsNotNone(core.poll())
        self.assertIsNone(listener_pid(core_port))

    def test_launcher_exit_cancels_pending_startup_and_cleans_job(self) -> None:
        services = OwnedServices(self.logs, cancelled=lambda: True)
        port = _unused_port()
        with self.assertRaisesRegex(LauncherError, "console closed during startup"):
            self.start_service(services, "cancelled-pet", port)
        self.assertEqual(services.processes, [])
        self.assertIsNone(listener_pid(port))

    def test_failed_pet_startup_cleans_started_core(self) -> None:
        services = OwnedServices(self.logs)
        core_port = _unused_port()
        core = self.start_service(services, "core", core_port)
        with self.assertRaises(LauncherError):
            services.start(
                "pet",
                [sys.executable, str(self.fixture), "0", "0.1"],
                port=_unused_port(),
                cwd=self.root,
                timeout=1,
            )
        self.assertIsNotNone(core.poll())
        self.assertIsNone(listener_pid(core_port))

    def test_core_start_failure_is_cleaned_up(self) -> None:
        services = OwnedServices(self.logs)
        with self.assertRaises(LauncherError):
            services.start(
                "core",
                [sys.executable, str(self.fixture), "0", "0.1"],
                port=_unused_port(),
                cwd=self.root,
                timeout=1,
            )
        self.assertEqual(services.processes, [])

    def test_existing_core_is_reused_and_survives_pet_exit(self) -> None:
        core_port, pet_port = _unused_port(), _unused_port()
        existing_core = subprocess.Popen(
            [sys.executable, str(self.fixture), str(core_port), "30"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            initial_owner = _wait_for_listener(core_port)
            services = OwnedServices(self.logs)
            self.assertIsNone(
                services.start(
                    "core",
                    ["this-command-must-not-run"],
                    port=core_port,
                    cwd=self.root,
                    timeout=1,
                    reuse_existing=True,
                )
            )
            pet = self.start_service(services, "pet", pet_port, seconds=2.0)
            pet.process.wait(timeout=4)
            services.stop()
            self.assertIsNone(existing_core.poll(), "reused Core was stopped")
            self.assertEqual(listener_pid(core_port), initial_owner)
        finally:
            existing_core.terminate()
            existing_core.wait(timeout=5)

    def test_unrelated_renderer_listener_is_rejected_without_termination(self) -> None:
        port = _unused_port()
        external = subprocess.Popen(
            [sys.executable, str(self.fixture), str(port), "30"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            initial_owner = _wait_for_listener(port)
            with self.assertRaises(LauncherError):
                OwnedServices(self.logs).start(
                    "renderer",
                    ["this-command-must-not-run"],
                    port=port,
                    cwd=self.root,
                    timeout=1,
                )
            self.assertIsNone(external.poll(), "unrelated renderer listener was terminated")
            self.assertEqual(listener_pid(port), initial_owner)
        finally:
            external.terminate()
            external.wait(timeout=5)

    def test_duplicate_mode_reuses_session_and_switch_stops_owned_core_first(self) -> None:
        supervisor = SessionSupervisor(self.root)
        renderer_port, core_port = _unused_port(), _unused_port()

        class FixtureStarter:
            def __init__(self) -> None:
                self.starts: list[str] = []
                self.previous_services: list[OwnedServices] = []

            def start(inner_self, config_path: Path, mode: str) -> LaunchSession:
                if inner_self.starts:
                    self.assertIsNone(listener_pid(renderer_port), "old renderer was still listening")
                    if inner_self.starts[-1] == "desktop_pet":
                        self.assertIsNone(listener_pid(core_port), "owned Core was not released before mode switch")
                services = OwnedServices(self.logs)
                if mode == "desktop_pet":
                    self.start_service(services, "core", core_port)
                pet = self.start_service(services, "pet", renderer_port)
                inner_self.starts.append(mode)
                inner_self.previous_services.append(services)
                config = SimpleNamespace(desktop_pet=SimpleNamespace(show_desktop_console=False, show_core_console=False))
                return LaunchSession(
                    mode,
                    config,
                    pet,
                    services,
                    config_path,
                    pet_created=1000 + len(inner_self.starts),
                )  # type: ignore[arg-type]

        starter = FixtureStarter()
        supervisor.starter = starter  # type: ignore[assignment]
        requests_dir = self.logs / "launcher-requests"
        requests_dir.mkdir(parents=True)

        def request(request_id: str, mode: str) -> Path:
            path = requests_dir / f"{request_id}.request.json"
            path.write_text(
                json.dumps({"id": request_id, "config": str(self.root / "unused.toml"), "mode": mode}),
                encoding="utf-8",
            )
            return path

        supervisor._handle_request(request("first", "desktop_pet"))
        original_pet = supervisor.session.pet  # type: ignore[union-attr]
        original_identity = supervisor.session.identity  # type: ignore[union-attr]
        started = json.loads((requests_dir / "first.result.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (started["status"], started["mode"], started["pet_pid"], started["pet_created"]),
            ("started", "desktop_pet", original_identity.pet_pid, original_identity.pet_created),
        )
        supervisor._handle_request(request("duplicate", "desktop_pet"))
        self.assertIs(supervisor.session.pet, original_pet)  # type: ignore[union-attr]
        reused = json.loads((requests_dir / "duplicate.result.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (reused["status"], reused["mode"], reused["pet_pid"], reused["pet_created"]),
            ("reused", "desktop_pet", original_identity.pet_pid, original_identity.pet_created),
        )
        self.assertEqual(starter.starts, ["desktop_pet"])
        supervisor._handle_request(request("switch", "live"))
        try:
            self.assertEqual(starter.starts, ["desktop_pet", "live"])
            self.assertIsNotNone(original_pet.poll())
            self.assertEqual(supervisor.session.mode, "live")  # type: ignore[union-attr]
            live_identity = supervisor.session.identity  # type: ignore[union-attr]
            supervisor._handle_request(request("duplicate-live", "live"))
            reused_live = json.loads(
                (requests_dir / "duplicate-live.result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                (
                    reused_live["status"],
                    reused_live["mode"],
                    reused_live["pet_pid"],
                    reused_live["pet_created"],
                ),
                ("reused", "live", live_identity.pet_pid, live_identity.pet_created),
            )
            self.assertTrue(
                supervisor.session.pet.job.contains(_wait_for_listener(renderer_port))  # type: ignore[union-attr]
            )
        finally:
            if supervisor.session is not None:
                supervisor.session.stop()


class LauncherInterfaceTests(unittest.TestCase):
    def test_python_cli_mode_and_log_arguments(self) -> None:
        self.assertEqual(_parser().parse_args(["--mode", "live"]).mode, "live")
        self.assertEqual(_parser().parse_args(["--show-logs", "CORE"]).show_logs, "CORE")

    def test_live_mode_follows_output_and_ctrl_c_stops_the_exact_session(self) -> None:
        output = io.StringIO()
        identity = LaunchIdentity("live", 4312, 9876)
        with (
            patch("live2d_adapter.launcher._owner_alive", side_effect=[True, KeyboardInterrupt]),
            patch("live2d_adapter.launcher._enqueue_request", return_value=("stop-id", Path("requests"))) as enqueue,
            patch("live2d_adapter.launcher._wait_for_result", return_value={"status": "stopped"}),
            patch("live2d_adapter.launcher.LogTailer.read_available", side_effect=["live output\n", ""]),
            patch("live2d_adapter.launcher.time.sleep"),
            patch("sys.stdout", output),
        ):
            self.assertEqual(follow_live_session(ROOT, identity), 0)
        self.assertIn("live output", output.getvalue())
        self.assertIn("Live mode stopped", output.getvalue())
        self.assertEqual(enqueue.call_args.args[2], "live")
        self.assertEqual(enqueue.call_args.kwargs["action"], "stop")
        self.assertEqual(enqueue.call_args.kwargs["pet_pid"], 4312)
        self.assertEqual(enqueue.call_args.kwargs["pet_created"], 9876)

    def test_launch_returns_request_identity_for_reused_live_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live2d reused identity ") as directory:
            adapter_dir = Path(directory)
            response = {
                "status": "reused",
                "mode": "live",
                "pet_pid": 222,
                "pet_created": 333,
            }
            with (
                patch("live2d_adapter.launcher._adapter_root", return_value=adapter_dir),
                patch("live2d_adapter.launcher._wait_for_result", return_value=response),
            ):
                result = launch(adapter_dir / "config.toml", "live")
            self.assertEqual(result, LaunchOutcome(0, "reused", LaunchIdentity("live", 222, 333)))

    def test_core_console_is_created_before_services_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text('[runtime]\nmode="desktop_pet"\n[renderer]\nmodel_path="avatar.model3.json"\n[desktop_pet]\nshow_core_console=true\n[desktop_pet.chat]\nenabled=true\ntransport="core"\n', encoding="utf-8")
            supervisor = SessionSupervisor(root)
            order = []
            viewer = Mock(poll=Mock(return_value=None))
            def open_window(*args, **kwargs):
                order.append("console")
                self.assertEqual(kwargs["session_id"], "early")
                self.assertTrue(kwargs["start_at_end"])
                return viewer
            def start(*args):
                order.append("services")
                self.assertEqual(order, ["console", "services"])
                return LaunchSession("desktop_pet", load_config(config_path), Mock(pid=123), Mock(), config_path)
            request = root / "logs/launcher-requests/early.request.json"
            request.parent.mkdir(parents=True)
            request.write_text(json.dumps({"id":"early", "config":str(config_path), "mode":"desktop_pet"}), encoding="utf-8")
            with patch("live2d_adapter.launcher._open_log_viewer", side_effect=open_window), \
                 patch("live2d_adapter.launcher.listener_pid", return_value=None), \
                 patch.object(supervisor.starter, "start", side_effect=start):
                supervisor._handle_request(request)
            self.assertEqual(order, ["console", "services"])
            self.assertEqual(supervisor.session.log_viewers, [viewer])

    def test_startup_and_running_logs_share_the_same_cursor_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = Path(directory)
            logs = adapter / "logs"
            logs.mkdir()
            path = logs / "live2d.out.log"
            path.write_text("previous session\n", encoding="utf-8")
            def ready(*_args, **kwargs):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("\x1b[32m启动日志\x1b[0m\n")
                kwargs["on_poll"]()
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("运行日志\n")
                return {"status":"started", "mode":"desktop_pet", "pet_pid":123, "pet_created":456}
            output = io.StringIO()
            with patch("live2d_adapter.launcher._adapter_root", return_value=adapter), \
                 patch("live2d_adapter.launcher._wait_for_result", side_effect=ready), \
                 patch("sys.stdout", output):
                result = launch(adapter / "config.toml", "desktop_pet")
                with patch("live2d_adapter.launcher._owner_alive", side_effect=[True,False]), \
                     patch("live2d_adapter.launcher.time.sleep"):
                    follow_live_session(adapter, result.identity, tailers=result.log_tailers)
            self.assertNotIn("previous session", output.getvalue())
            self.assertEqual(output.getvalue().count("启动日志"),1)
            self.assertEqual(output.getvalue().count("运行日志"),1)
            self.assertIn("\x1b[32m", output.getvalue())

    def test_desktop_logs_stay_in_original_console_without_new_live2d_window(self) -> None:
        identity = LaunchIdentity("desktop_pet", 123, 456)
        with patch("live2d_adapter.launcher.load_config", return_value=SimpleNamespace(runtime=SimpleNamespace(mode="desktop_pet"))), \
             patch("live2d_adapter.launcher.launch", return_value=LaunchOutcome(0, "started", identity)), \
             patch("live2d_adapter.launcher.follow_live_session", return_value=0) as follow:
            self.assertEqual(main([]), 0)
        follow.assert_called_once_with(ROOT, identity)
        supervisor = SessionSupervisor(ROOT)
        config = SimpleNamespace(components=SimpleNamespace(core=True), desktop_pet=SimpleNamespace(show_desktop_console=True, show_core_console=True,
                    chat=SimpleNamespace(enabled=True, transport="core", core_url="ws://127.0.0.1:8000/ws")))
        with patch("live2d_adapter.launcher._open_log_viewer", return_value=Mock()) as open_window, \
             patch("live2d_adapter.launcher.listener_pid", return_value=None):
            viewers = supervisor._open_configured_log_windows(config, "startup-request")
        self.assertEqual(len(viewers), 1)
        self.assertEqual(open_window.call_args.args[:2], (ROOT, "CORE"))
        self.assertEqual(open_window.call_args.kwargs, {"session_id": "startup-request", "start_at_end": True})
        with patch("live2d_adapter.launcher._open_log_viewer") as open_window, \
             patch("live2d_adapter.launcher.listener_pid", return_value=777):
            self.assertEqual(supervisor._open_configured_log_windows(config, "reused-core"), [])
        open_window.assert_not_called()

    def test_main_uses_live_request_identity_after_desktop_replaces_shared_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live2d mode replacement ") as directory:
            adapter_dir = Path(directory)
            state_path = adapter_dir / "logs" / "launcher-state.json"
            live_identity = LaunchIdentity("live", 111, 999)

            def launch_then_replace_state(config_path: Path, mode: str) -> LaunchOutcome:
                self.assertEqual(mode, "live")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(
                    json.dumps({
                        "status": "ready",
                        "mode": "desktop_pet",
                        "pet_pid": 222,
                        "pet_created": 333,
                    }),
                    encoding="utf-8",
                )
                return LaunchOutcome(0, "started", live_identity)

            with (
                patch("live2d_adapter.launcher._adapter_root", return_value=adapter_dir),
                patch(
                    "live2d_adapter.launcher.load_config",
                    return_value=SimpleNamespace(runtime=SimpleNamespace(mode="live")),
                ),
                patch("live2d_adapter.launcher.launch", side_effect=launch_then_replace_state),
                patch("live2d_adapter.launcher.follow_live_session", return_value=0) as follow,
            ):
                self.assertEqual(main(["--mode", "live"]), 0)

            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["mode"],
                "desktop_pet",
            )
            follow.assert_called_once_with(adapter_dir, live_identity)

    def test_stale_live_stop_does_not_stop_replacement_desktop_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live2d stale stop ") as directory:
            adapter_dir = Path(directory)
            supervisor = SessionSupervisor(adapter_dir)
            pet = SimpleNamespace(pid=222, poll=lambda: None)
            services = Mock()
            session = LaunchSession(
                "desktop_pet",
                SimpleNamespace(),  # type: ignore[arg-type]
                pet,  # type: ignore[arg-type]
                services,  # type: ignore[arg-type]
                adapter_dir / "config.toml",
                pet_created=333,
            )
            supervisor.session = session
            requests_dir = adapter_dir / "logs" / "launcher-requests"
            request_path = requests_dir / "stale-live.request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text(
                json.dumps({
                    "id": "stale-live",
                    "action": "stop",
                    "mode": "live",
                    "pet_pid": 222,
                    "pet_created": 333,
                }),
                encoding="utf-8",
            )

            supervisor._handle_request(request_path)

            result = json.loads((requests_dir / "stale-live.result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "stale")
            self.assertIs(supervisor.session, session)
            services.stop.assert_not_called()

    def test_managed_stop_waits_for_job_descendants_after_root_exits(self) -> None:
        events: list[str] = []

        class FakeJobApi:
            active_counts = iter((2, 1, 0))

            def TerminateJobObject(self, handle: int, exit_code: int) -> int:
                events.append("terminate_job")
                return 1

            def QueryInformationJobObject(
                self,
                handle: int,
                info_class: int,
                info_buffer: int,
                buffer_size: int,
                returned_length: object,
            ) -> int:
                self_outer.assertEqual(info_class, 1)
                self_outer.assertEqual(buffer_size, ctypes.sizeof(_JobBasicAccountingInformation))
                info = ctypes.cast(
                    info_buffer,
                    ctypes.POINTER(_JobBasicAccountingInformation),
                ).contents
                info.ActiveProcesses = next(self.active_counts)
                events.append(f"active:{info.ActiveProcesses}")
                return 1

            def CloseHandle(self, handle: int) -> int:
                events.append("close_job")
                return 1

        class ExitedRoot:
            pid = 42

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float) -> int:
                events.append("wait_root")
                return 0

        self_outer = self
        job = WindowsJob.__new__(WindowsJob)
        job.handle = 123
        job._kernel32 = FakeJobApi()  # type: ignore[assignment]
        managed = ManagedProcess(
            "fixture",
            ExitedRoot(),  # type: ignore[arg-type]
            job,
            Path("fixture.out"),
            Path("fixture.err"),
        )

        with patch("live2d_adapter.launcher.time.sleep", side_effect=lambda _: events.append("poll")):
            managed.stop(timeout=1)

        self.assertEqual(
            events,
            [
                "terminate_job",
                "wait_root",
                "active:2",
                "poll",
                "active:1",
                "poll",
                "active:0",
                "wait_root",
                "close_job",
            ],
        )
        self.assertIsNone(job.handle)
        self.assertTrue(managed._closed)

    def test_managed_stop_reports_job_drain_timeout_and_closes_handle(self) -> None:
        events: list[str] = []

        class StuckJobApi:
            def TerminateJobObject(self, handle: int, exit_code: int) -> int:
                events.append("terminate_job")
                return 1

            def QueryInformationJobObject(
                self,
                handle: int,
                info_class: int,
                info_buffer: int,
                buffer_size: int,
                returned_length: object,
            ) -> int:
                info = ctypes.cast(
                    info_buffer,
                    ctypes.POINTER(_JobBasicAccountingInformation),
                ).contents
                info.ActiveProcesses = 1
                return 1

            def CloseHandle(self, handle: int) -> int:
                events.append("close_job")
                return 1

        class ExitedRoot:
            pid = 43

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float) -> int:
                return 0

        job = WindowsJob.__new__(WindowsJob)
        job.handle = 124
        job._kernel32 = StuckJobApi()  # type: ignore[assignment]
        managed = ManagedProcess(
            "stuck-fixture",
            ExitedRoot(),  # type: ignore[arg-type]
            job,
            Path("stuck.out"),
            Path("stuck.err"),
        )

        with self.assertRaisesRegex(LauncherError, "Timed out waiting for the stuck-fixture process tree"):
            managed.stop(timeout=0)

        self.assertEqual(events, ["terminate_job", "close_job"])
        self.assertIsNone(job.handle)
        self.assertTrue(managed._closed)

    def test_launch_recovers_if_idle_supervisor_exits_after_queue_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live2d supervisor handoff ") as directory:
            adapter_dir = Path(directory)
            state = {
                "mutex_owner": "existing-supervisor",
                "active_supervisors": 1,
                "peak_active_supervisors": 1,
                "spawn_count": 0,
                "existing_exited": False,
                "clock": 0.0,
            }

            class FakeMutex:
                def __init__(inner_self, name: str, *, initial_owner: bool = False) -> None:
                    if initial_owner and state["mutex_owner"] is None:
                        state["mutex_owner"] = "launch-client"
                        inner_self.owned = True
                    else:
                        inner_self.owned = False

                def release(inner_self) -> None:
                    if inner_self.owned:
                        self.assertEqual(state["mutex_owner"], "launch-client")
                        state["mutex_owner"] = "replacement-supervisor"
                        inner_self.owned = False

                def close(inner_self) -> None:
                    if inner_self.owned:
                        inner_self.release()

            def fake_spawn_supervisor(path: Path, mutex_name: str) -> SimpleNamespace:
                self.assertEqual(state["mutex_owner"], "launch-client")
                self.assertEqual(state["active_supervisors"], 0)
                state["spawn_count"] += 1
                state["active_supervisors"] += 1
                state["peak_active_supervisors"] = max(
                    state["peak_active_supervisors"], state["active_supervisors"]
                )
                request_path = next((path / "logs" / "launcher-requests").glob("*.request.json"))
                request = json.loads(request_path.read_text(encoding="utf-8"))
                result_path = request_path.with_name(f"{request['id']}.result.json")
                result_path.write_text(
                    json.dumps({
                        "id": request["id"],
                        "status": "started",
                        "mode": "desktop_pet",
                        "pet_pid": 42,
                        "pet_created": 43,
                    }),
                    encoding="utf-8",
                )
                return SimpleNamespace(poll=lambda: None)

            def advance_clock() -> float:
                state["clock"] += 1.0
                return state["clock"]

            def settle_supervisor_exit(_: float) -> None:
                if not state["existing_exited"]:
                    queued = list((adapter_dir / "logs" / "launcher-requests").glob("*.request.json"))
                    self.assertEqual(len(queued), 1, "the start request must be queued before handoff")
                    state["existing_exited"] = True
                    state["active_supervisors"] = 0
                    state["mutex_owner"] = None

            with (
                patch("live2d_adapter.launcher._adapter_root", return_value=adapter_dir),
                patch("live2d_adapter.launcher.WindowsMutex", FakeMutex),
                patch("live2d_adapter.launcher._spawn_supervisor", side_effect=fake_spawn_supervisor),
                patch("live2d_adapter.launcher.time.monotonic", side_effect=advance_clock),
                patch("live2d_adapter.launcher.time.sleep", side_effect=settle_supervisor_exit),
            ):
                result = launch(adapter_dir / "config.toml", "desktop_pet")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.identity, LaunchIdentity("desktop_pet", 42, 43))

            self.assertTrue(state["existing_exited"])
            self.assertEqual(state["spawn_count"], 1)
            self.assertEqual(state["peak_active_supervisors"], 1)
            self.assertEqual(state["mutex_owner"], "replacement-supervisor")

    @unittest.skipUnless(os.name == "nt" and shutil.which("cmd.exe"), "Windows batch launcher")
    def test_bat_invocation_uses_config_mode_override_and_logs_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="live2d bat fixture ") as directory:
            root = Path(directory)
            stub_dir = root / "stub tools"
            stub_dir.mkdir()
            launcher = root / "launch_live2d.bat"
            shutil.copyfile(ROOT / "launch_live2d.bat", launcher)
            (root / "config.toml").write_text("", encoding="utf-8")
            capture = root / "uv args.txt"
            uv_stub = stub_dir / "uv.bat"
            uv_stub.write_text(
                '@echo off\r\necho %*>>"%UV_CAPTURE%"\r\nexit /b 0\r\n',
                encoding="ascii",
            )
            base_env = os.environ.copy()
            base_env["PATH"] = str(stub_dir) + os.pathsep + base_env.get("PATH", "")
            base_env["UV_CAPTURE"] = str(capture)
            for args, expected in (
                ([], "--config"),
                (["desktop_pet"], "--mode desktop_pet"),
                (["live"], "--mode live"),
                (["logs", "ALL"], "--show-logs ALL"),
                (["logs", "LIVE2D"], "--show-logs LIVE2D"),
                (["logs", "CORE"], "--show-logs CORE"),
            ):
                capture.unlink(missing_ok=True)
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(launcher), *args],
                    cwd=root,
                    env=base_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                commands = capture.read_text(encoding="utf-8")
                self.assertIn("sync --locked", commands)
                self.assertIn(expected, commands)
                self.assertNotIn("powershell", commands.casefold())
