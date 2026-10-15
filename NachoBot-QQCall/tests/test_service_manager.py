from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import service.manager as manager_module
from service.config import DEFAULT_CONFIG, load_config
from service.manager import (
    ActiveGeneration,
    BridgeProcess,
    Evaluation,
    ServiceError,
    Supervisor,
    main,
)
from service.private_files import atomic_private_json, read_private_json
from service.rpc import OneBotClient, OneBotError
from service.windows import HostLaunch, TargetBinding

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NACHOBOT_ROOT = REPOSITORY_ROOT / "NachoBot"
WEBUI_ROOT = REPOSITORY_ROOT / "WebUI"
if str(NACHOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(NACHOBOT_ROOT))
if str(WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(WEBUI_ROOT))

import process_manager as webui_process_manager
from qqcall_client import qqcall_service as shared_qqcall_service
from qqcall_client.qqcall import QQCallConfig, QQCallRuntime
from qqcall_client.qqcall_service import load_qqcall_adapter_settings

SELF_UID = "u_" + ("a" * 22)
PEER_UID = "u_" + ("b" * 22)
CAPTURE_ENDPOINT_ID = "virtual-capture-endpoint"
RENDER_ENDPOINT_ID = "virtual-render-endpoint"


def _endpoint_inventory() -> list[dict[str, str]]:
    return [
        {
            "id": CAPTURE_ENDPOINT_ID,
            "name": "Playback (VB-Audio Virtual Cable)",
            "flow": "capture",
        },
        {
            "id": RENDER_ENDPOINT_ID,
            "name": "Capture (VB-Audio Virtual Cable)",
            "flow": "render",
        },
        {
            "id": "virtual-playback-render-endpoint",
            "name": "Playback (VB-Audio Virtual Cable)",
            "flow": "render",
        },
    ]


def _audio_guard_fixture(root: Path) -> Path:
    node_path = root / "QQCallAudioRouteGuard-v2.node"
    dll_path = root / "QQCallAudioRouteGuard-v2.dll"
    node_bytes = b"test audio route guard node"
    dll_bytes = b"test audio route guard dll"
    node_path.write_bytes(node_bytes)
    dll_path.write_bytes(dll_bytes)
    guard_path = root / "audio-guard.json"
    guard_path.write_text(json.dumps({
        "version": 1,
        "qqBuild": "9.9.26-44343",
        "hostGeneration": 1,
        "nodeArtifact": str(node_path),
        "guardDllPath": str(dll_path),
        "nodeSha256": hashlib.sha256(node_bytes).hexdigest(),
        "dllSha256": hashlib.sha256(dll_bytes).hexdigest(),
        "captureEndpointId": CAPTURE_ENDPOINT_ID,
        "renderEndpointId": RENDER_ENDPOINT_ID,
    }), encoding="utf-8")
    return guard_path


def _toml_value(value: Any) -> str:
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }"
    raise TypeError(type(value).__name__)


def _write_toml(path: Path, value: dict[str, Any]) -> None:
    lines: list[str] = []

    def table(current: dict[str, Any], prefix: str) -> None:
        if prefix:
            if lines:
                lines.append("")
            lines.append(f"[{prefix}]")
        for key, item in current.items():
            if not isinstance(item, dict):
                lines.append(f"{key} = {_toml_value(item)}")
        for key, item in current.items():
            if isinstance(item, dict):
                table(item, f"{prefix}.{key}" if prefix else key)

    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{key} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            table(item, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _config(
    root: Path,
    *,
    enabled: bool = True,
    adapter: str = "napcat",
    peer_map: dict[str, str] | None = None,
) -> dict:
    value = copy.deepcopy(DEFAULT_CONFIG)
    qqcall = value["qqcall"]
    value["plugin"]["enabled"] = enabled
    qqcall["adapter"] = adapter
    qqcall["account_id"] = "12345" if enabled else ""
    qqcall["runtime_dir"] = str(root / ".runtime" / "service")
    qqcall["onebot"]["url"] = "http://127.0.0.1:8999" if enabled else ""
    qqcall["paths"]["qq_exe"] = str(root / "QQ.exe") if enabled else ""
    if enabled:
        qqcall["audio"]["capture_device"] = "Capture (VB-Audio Virtual Cable) (loopback)"
        qqcall["audio"]["playback_device"] = "Playback (VB-Audio Virtual Cable)"
        qqcall["native"]["guard_config_file"] = str(_audio_guard_fixture(root))
    qqcall["peer_map"] = peer_map or {}
    path = root / "config.toml"
    _write_toml(path, value)
    return load_config(path)


class FakeSystem:
    supported = True

    def __init__(self, manifest: dict | None = None, binding: TargetBinding | None = None):
        self.manifest = manifest or {
            "schema": 1,
            "adapter": "napcat",
            "accountId": "12345",
            "connected": True,
            "adapterPid": 8095,
            "adapterCreationTime100ns": "809500",
            "connectionGeneration": "a1b2c3d4",
            "updatedAtMs": 1_700_000_000_000,
        }
        self.binding = binding or TargetBinding(8095, 809500, 51200, 512000, "C:\\QQ\\Current")
        self.live: dict[int, int] = {self.binding.kernel_pid: self.binding.kernel_creation_time_100ns}
        self.launch_count = 0
        self.terminate_calls: list[list[dict]] = []
        self.port_checks: list[int] = []
        self.revalidate_hook = None

    def current_process_identity(self) -> tuple[int, int]:
        return 700, 700000

    def process_identity(self, pid: int):
        if pid == 701:
            return 701000, "C:\\node.exe"
        return None

    def process_is_current(self, pid: int, creation: int) -> bool | None:
        if pid not in self.live:
            return False
        return self.live[pid] == creation

    def read_manifest(self, _config: dict) -> dict | None:
        return self.manifest

    def bind_adapter(self, _config: dict, _manifest: dict) -> TargetBinding:
        return self.binding

    def revalidate_binding(self, _config: dict, _manifest: dict, binding: TargetBinding) -> bool:
        if self.revalidate_hook is not None:
            self.revalidate_hook()
        return self.binding == binding

    def listener_owners(self, _port: int) -> list[int]:
        self.port_checks.append(_port)
        return []

    def launch_host(self, *_args: object, **_kwargs: object) -> HostLaunch:
        self.launch_count += 1
        raise AssertionError("plan/check must never launch a Host")

    def terminate_owned(self, records: list[dict]) -> bool:
        self.terminate_calls.append(records)
        return True


class FakeRpc:
    def __init__(
        self,
        *,
        online: bool = True,
        include_peer_uid: bool = True,
        peer_response_uin: str | None = None,
        peer_uid: str | None = None,
        fail_peer_lookup: bool = False,
        friends: list[dict[str, Any]] | None = None,
        friend_uids: dict[str, str | None] | None = None,
        friend_response_fields: dict[str, dict[str, Any]] | None = None,
        fail_friend_list: bool = False,
        fail_peer_uins: set[str] | None = None,
        account_uin: int | str = 12345,
        self_uid: str = SELF_UID,
    ):
        self.online = online
        self.include_peer_uid = include_peer_uid
        self.peer_response_uin = peer_response_uin
        self.peer_uid = peer_uid
        self.fail_peer_lookup = fail_peer_lookup
        self.friends = copy.deepcopy(friends or [])
        self.friend_uids = dict(friend_uids or {})
        self.friend_response_fields = copy.deepcopy(friend_response_fields or {})
        self.fail_friend_list = fail_friend_list
        self.fail_peer_uins = set(fail_peer_uins or ())
        self.account_uin = account_uin
        self.self_uid = self_uid
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[float | None] = []
        self.after_call = None
        self.status_data: dict[str, Any] | None = None

    def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        params = params or {}
        self.calls.append((action, params))
        self.timeouts.append(timeout)
        if action == "get_status":
            result = copy.deepcopy(self.status_data) if self.status_data is not None else {"online": self.online}
        elif action == "get_login_info":
            result = {"user_id": self.account_uin}
        elif action == "get_stranger_info":
            user = str(params["user_id"])
            if (user == "10002" and self.fail_peer_lookup) or user in self.fail_peer_uins:
                raise OneBotError("rpc_error")
            result = {"user_id": user}
            if user == str(self.account_uin) or user == "12345":
                result["uid"] = self.self_uid
            elif user in self.friend_uids:
                if self.friend_uids[user] is not None:
                    result["uid"] = self.friend_uids[user]
            elif user == "10002":
                if self.peer_response_uin is not None:
                    result["user_id"] = self.peer_response_uin
                if self.include_peer_uid:
                    result["uid"] = self.peer_uid or PEER_UID
            if user in self.friend_response_fields:
                result.update(copy.deepcopy(self.friend_response_fields[user]))
        else:
            raise AssertionError(action)
        if self.after_call is not None:
            self.after_call(action, params)
        return result

    def get_friend_list(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        self.calls.append(("get_friend_list", {}))
        self.timeouts.append(timeout)
        if self.fail_friend_list:
            raise OneBotError("rpc_error")
        result = copy.deepcopy(self.friends)
        if self.after_call is not None:
            self.after_call("get_friend_list", {})
        return result


class FakeBridge:
    def __init__(self, detached: bool):
        self.detached = detached
        self.process = SimpleNamespace(pid=701, poll=lambda: None)
        self.events: list[dict] = []
        self.commands: list[str] = []

    def send(self, command: str) -> bool:
        self.commands.append(command)
        self.events.append({"command": command, "result": {"adapterAvailable": command == "adapter-resume"}})
        return True

    def stop(self, timeout: float = 12.0) -> bool:
        return self.detached

    def poll_event(self):
        return self.events.pop(0) if self.events else None

    def wait_event(self, _timeout: float):
        return self.poll_event()


class _ControlledStdout:
    _EOF = object()

    def __init__(self):
        self._lines: queue.Queue[str | object] = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self._lines.get()
        if line is self._EOF:
            raise StopIteration
        return line  # type: ignore[return-value]

    def feed(self, line: str) -> None:
        self._lines.put(line)

    def close(self) -> None:
        self._lines.put(self._EOF)


class _BridgeStopChild:
    def __init__(self, stdout: _ControlledStdout):
        self.stdin = self
        self.stdout = stdout
        self.stderr = io.StringIO()
        self.returncode: int | None = None
        self.quit_sent = threading.Event()
        self.exit_observed = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    def write(self, value: str) -> int:
        if value == "quit\n":
            self.quit_sent.set()
        return len(value)

    def flush(self) -> None:
        return None

    def poll(self) -> int | None:
        if self.returncode is not None:
            self.exit_observed.set()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = 1


class FakeUniversalService:
    def __init__(self, result: dict[str, Any] | None = None):
        self.result = result or {"state": "ready", "code": "universal_api_service_ready", "reused": True}
        self.calls: list[dict[str, Any]] = []

    def ensure_ready(self, *, checkpoint=None):
        self.calls.append({"checkpoint": checkpoint})
        return dict(self.result)


class BridgeProcessStopTests(unittest.TestCase):
    _ACK = json.dumps({"command": "quit", "result": {"detachedConfirmed": True}}) + "\n"

    def _stop_after_exit(self, lines: list[str], *, close_stdout: bool = True, timeout: float = 1.0):
        stdout = _ControlledStdout()
        process = _BridgeStopChild(stdout)
        bridge = BridgeProcess(process)
        outcome: dict[str, Any] = {}
        completed = threading.Event()

        def stop() -> None:
            outcome["detached"] = bridge.stop(timeout=timeout)
            completed.set()

        worker = threading.Thread(target=stop)
        worker.start()
        self.assertTrue(process.quit_sent.wait(1.0), "stop sent its explicit quit request")
        process.returncode = 0
        self.assertTrue(process.exit_observed.wait(1.0), "stop observed process exit before stdout release")
        for line in lines:
            stdout.feed(line)
        if close_stdout:
            stdout.close()
        self.assertTrue(completed.wait(timeout + 1.0), "stop remained within its original deadline")
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        return bridge, process, outcome["detached"], stdout

    def test_final_qualified_ack_after_process_exit_is_drained_before_return(self):
        bridge, process, detached, stdout = self._stop_after_exit([self._ACK])
        self.addCleanup(stdout.close)
        bridge._reader.join(1.0)
        self.assertTrue(detached)
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(process.kill_calls, 0)

    def test_ack_before_process_exit_remains_accepted(self):
        stdout = _ControlledStdout()
        stdout.feed(self._ACK)
        stdout.close()
        process = _BridgeStopChild(stdout)
        bridge = BridgeProcess(process)
        self.assertTrue(bridge.stop(timeout=1.0))
        bridge._reader.join(1.0)
        self.assertEqual(process.terminate_calls, 0)

    def test_missing_negative_and_malformed_ack_after_exit_remain_unconfirmed(self):
        negative = json.dumps({"command": "quit", "result": {"detachedConfirmed": False}}) + "\n"
        for lines in ([], [negative], ["not-json\n"]):
            with self.subTest(lines=lines):
                bridge, _process, detached, stdout = self._stop_after_exit(lines)
                stdout.close()
                bridge._reader.join(1.0)
                self.assertFalse(detached)

    def test_blocked_stdout_reader_does_not_extend_stop_past_deadline(self):
        stdout = _ControlledStdout()
        process = _BridgeStopChild(stdout)
        bridge = BridgeProcess(process)
        outcome: dict[str, Any] = {}
        completed = threading.Event()
        started = time.monotonic()

        def stop() -> None:
            outcome["detached"] = bridge.stop(timeout=0.08)
            completed.set()

        worker = threading.Thread(target=stop)
        worker.start()
        self.assertTrue(process.quit_sent.wait(1.0))
        process.returncode = 0
        self.assertTrue(process.exit_observed.wait(1.0))
        self.assertTrue(completed.wait(0.5), "the original stop deadline bounds an EOF-blocked reader")
        worker.join(timeout=0.5)
        elapsed = time.monotonic() - started
        stdout.close()
        bridge._reader.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(outcome["detached"])
        self.assertLess(elapsed, 0.5)

    def test_full_stdout_event_queue_retains_only_the_qualified_quit_ack(self):
        stdout = _ControlledStdout()
        for index in range(512):
            stdout.feed(json.dumps({"kind": "status", "index": index}) + "\n")
        stdout.feed(self._ACK)
        stdout.close()
        process = _BridgeStopChild(stdout)
        bridge = BridgeProcess(process)
        self.assertTrue(bridge._stdout_eof.wait(1.0))
        self.assertEqual(bridge.events.qsize(), 512)
        self.assertTrue(bridge.stop(timeout=1.0))
        bridge._reader.join(1.0)
        self.assertEqual(process.terminate_calls, 0)


class ServiceManagerTests(unittest.TestCase):
    def test_console_logs_state_changes_without_polling_noise_or_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = self._supervisor(Path(temp))
            supervisor._console_logging = True
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                supervisor._status("starting", "service_starting", persist=False)
                supervisor._status("starting", "service_starting", persist=False)
                supervisor._status("waiting_adapter", "adapter_not_connected", persist=False)
                supervisor._status("ready", "", persist=False)
                supervisor._status("ready", "", persist=False)
                supervisor._status("stopping", "stop_requested", persist=False)
                supervisor._status("stopped", "service_stopped", persist=False)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(len(errors.getvalue().splitlines()), 5)
            self.assertIn("通话控制链已就绪", errors.getvalue())
            self.assertIn("服务已停止", errors.getvalue())

    def test_console_logs_component_changes_and_only_normalized_public_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = self._supervisor(Path(temp))
            supervisor._console_logging = True
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                supervisor._status("starting", "service_starting", persist=False)
                supervisor.universal_service_status = {
                    "state": "ready", "code": "universal_api_service_ready", "reused": True,
                    "token": "DO_NOT_ECHO",
                }
                supervisor._status("starting", "service_starting", persist=False)
                supervisor._status("error", "DO_NOT_ECHO\nforged log", persist=False)
                supervisor._status("waiting_kernel_restart", "fixed_shim_kernel_consumed", persist=False)
            self.assertEqual(len(output.getvalue().splitlines()), 4)
            self.assertIn("已就绪（复用）", output.getvalue())
            self.assertIn("[WARN]", output.getvalue())
            self.assertIn("code=service_error", output.getvalue())
            self.assertNotIn("DO_NOT_ECHO", output.getvalue())
            self.assertNotIn("forged log", output.getvalue())

    def test_console_failure_does_not_prevent_status_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = self._supervisor(Path(temp))
            supervisor._console_logging = True
            with patch("builtins.print", side_effect=OSError("closed console")):
                status = supervisor._status("waiting_adapter", "adapter_not_connected")
            self.assertEqual(read_private_json(supervisor.status_path), status)

    def test_read_only_status_has_no_console_side_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = self._supervisor(Path(temp))
            with contextlib.redirect_stderr(io.StringIO()) as output:
                supervisor._result_status("ready", "")
            self.assertEqual(output.getvalue(), "")

    def _recoverable_active(self, root: Path):
        supervisor = self._supervisor(root)
        evaluation = supervisor.evaluate()
        directory = supervisor.runtime_dir / "generations" / "connection-42"
        directory.mkdir(parents=True)
        bridge = FakeBridge(True)
        active = ActiveGeneration(
            evaluation, 41, 42, directory,
            HostLaunch(7000, str(root), str(root / "profile"), str(root / "report")),
            [], 8100, "810000", "sidecar", bridge,
            service_instance=str(uuid.uuid4()), shim_consumed=True,
            account_path_verified=True, login_ready=True, host_runtime_ready=True,
        )
        supervisor.system.live[8100] = 810000
        supervisor.active = active
        return supervisor, active

    def test_adapter_restart_preserves_native_generation_and_republishes_after_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, active = self._recoverable_active(Path(temp))
            supervisor._write_generation_identity(active)
            supervisor._suspend_adapter(active)
            self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())
            self.assertEqual(supervisor._monitor_active(active), "ready")
            supervisor.system.manifest = {**supervisor.system.manifest, "adapterPid": 9001,
                                          "adapterCreationTime100ns": "900100", "connectionGeneration": "reconnected"}
            supervisor.system.binding = replace(supervisor.system.binding, adapter_pid=9001,
                                                 adapter_creation_time_100ns=900100)
            resumed = supervisor.evaluate()
            supervisor._resume_adapter(active, resumed)
            self.assertIs(supervisor.active, active)
            self.assertEqual(active.bridge.commands, ["adapter-pause", "adapter-resume"])
            self.assertEqual((active.host_generation, active.connection_generation, active.host_pid), (41, 42, 8100))
            self.assertFalse(active.adapter_suspended)
            snapshot = read_private_json(supervisor.runtime_dir / "adapter-runtime.private.json")
            self.assertEqual(snapshot["serviceInstance"], active.service_instance)
            self.assertEqual(snapshot["connectionGeneration"], 42)
            self.assertEqual(supervisor.system.terminate_calls, [])
            self.assertEqual(supervisor.system.launch_count, 0)

    def test_recovery_refuses_changed_native_identity_peers_routes_or_process(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, active = self._recoverable_active(Path(temp))
            original = active.evaluation
            candidates = [
                replace(original, identity={**original.identity, "selfUid": "u_" + "x" * 22}),
                replace(original, peer_map={PEER_UID: "10002"}),
                replace(original, guard_source={"guard": dict.fromkeys(
                    ["nodeArtifact", "nodeSha256", "guardDllPath", "dllSha256", "captureEndpointId", "renderEndpointId"],
                    "changed"), "captureRenderName": "capture", "playbackRenderName": "playback",
                    "playbackRenderEndpointId": "changed"}),
                replace(original, binding=replace(original.binding, kernel_creation_time_100ns=512001)),
                replace(original, binding=replace(original.binding, app_path="C:\\OtherQQ")),
                replace(original, manifest={**original.manifest, "accountId": "99999"}),
            ]
            for candidate in candidates:
                with self.subTest(candidate=candidate.native_fingerprint), self.assertRaisesRegex(
                    ServiceError, "adapter_recovery_identity_mismatch",
                ):
                    supervisor._resume_adapter(active, candidate)
            self.assertEqual(active.bridge.commands, [])
            supervisor._suspend_adapter(active)
            for pid in (original.kernel_pid, active.host_pid):
                supervisor.system.live[pid] += 1
                with self.assertRaisesRegex(ServiceError, "adapter_recovery_native_unavailable"):
                    supervisor._resume_adapter(active, original)
                supervisor.system.live[pid] -= 1
            self.assertNotIn("adapter-resume", active.bridge.commands)

    def test_outage_run_loop_keeps_binding_until_explicit_stop(self):
        class FakeLock:
            def __init__(self, _path): pass
            def acquire(self): return True
            def release(self): pass
        for code in ("adapter_not_connected", "adapter_process_unavailable", "adapter_manifest_stale",
                     "adapter_listener_unavailable", "onebot_unavailable", "onebot_timeout", "onebot_offline"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                supervisor, active = self._recoverable_active(Path(temp))
                checks = []
                supervisor._consume_stop_request = lambda: len(checks) >= 2
                def tick(_seconds):
                    checks.append(supervisor.last_status.copy())
                    self.assertIs(supervisor.active, active)
                    self.assertEqual(supervisor.system.terminate_calls, [])
                supervisor.sleeper = tick
                with patch.object(supervisor, "_validate_static_dependencies"), patch.object(
                    manager_module, "ProcessLock", FakeLock,
                ), patch.object(supervisor, "evaluate", side_effect=[ServiceError(code, "waiting_adapter"), active.evaluation]):
                    result, exit_code = supervisor.run()
                self.assertEqual([item["phase"] for item in checks], ["waiting_adapter", "ready"])
                self.assertEqual(active.bridge.commands, ["adapter-pause", "adapter-resume"])
                self.assertEqual(result["phase"], "stopped")
                self.assertEqual(exit_code, 0)
                self.assertEqual(len(supervisor.system.terminate_calls), 1)

    def test_gate_failure_and_native_failure_cannot_publish_recovery_snapshot(self):
        for failure in ("send", "ack", "controller", "host"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                supervisor, active = self._recoverable_active(Path(temp))
                if failure == "send":
                    active.bridge.send = lambda _command: False
                elif failure == "ack":
                    active.bridge.events.append({"command": "adapter-pause", "code": "operation_failed"})
                elif failure == "controller":
                    active.bridge.process.poll = lambda: 1
                else:
                    active.host_runtime_failure_code = "host_disconnected"
                with self.assertRaises(ServiceError):
                    supervisor._resume_adapter(active, active.evaluation)
                self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())
                self.assertNotIn("adapter-resume", active.bridge.commands)

    def test_stop_retries_exact_owned_cleanup_without_repeating_detach(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, active = self._recoverable_active(Path(temp))
            with patch.object(active.bridge, "stop", return_value=True) as detach, patch.object(
                supervisor.system, "terminate_owned", side_effect=[False, True],
            ) as terminate:
                self.assertTrue(supervisor._stop_active(trigger_code="explicit_stop"))
            self.assertEqual(detach.call_count, 1)
            self.assertEqual(terminate.call_count, 2)
            self.assertIsNone(supervisor.active)
            self.assertFalse((active.directory / "host-cleanup-unconfirmed.private.json").exists())

    def test_suspended_binding_cannot_resume_after_native_invalidation(self):
        for flag in ("providerInvalidated", "controllerInvalidated", "hostInvalidated", "stopped"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp:
                supervisor, active = self._recoverable_active(Path(temp))
                active.bridge.events.append({"kind": "bridge_status", "status": {flag: True}})
                with self.assertRaisesRegex(ServiceError, "native_binding_invalidated"):
                    supervisor._resume_adapter(active, active.evaluation)
                self.assertNotIn("adapter-resume", active.bridge.commands)

    def test_identity_errors_stop_instead_of_entering_recoverable_wait(self):
        class FakeLock:
            def __init__(self, _path): pass
            def acquire(self): return True
            def release(self): pass
        for code in ("adapter_account_mismatch", "onebot_account_mismatch", "adapter_process_mismatch",
                     "adapter_manifest_invalid", "target_ambiguous", "identity_uid_mismatch"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                supervisor, active = self._recoverable_active(Path(temp))
                with patch.object(supervisor, "_validate_static_dependencies"), patch.object(
                    manager_module, "ProcessLock", FakeLock,
                ), patch.object(supervisor, "evaluate", side_effect=ServiceError(code, "quarantined")):
                    status, _ = supervisor.run(once=True)
                self.assertEqual(status["code"], code)
                self.assertIsNone(supervisor.active)
                self.assertEqual(active.bridge.commands, [])
                self.assertEqual(len(supervisor.system.terminate_calls), 1)

    def test_recovery_publication_failure_never_enables_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor, active = self._recoverable_active(Path(temp))
            with patch.object(supervisor, "_write_generation_identity", side_effect=ServiceError("audio_route_changed")):
                with self.assertRaisesRegex(ServiceError, "audio_route_changed"):
                    supervisor._resume_adapter(active, active.evaluation)
            self.assertTrue(active.adapter_suspended)
            self.assertEqual(active.bridge.commands, ["adapter-pause"])
            self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())

    def test_adapter_change_between_evaluation_and_monitor_suspends_instead_of_detaching(self):
        class FakeLock:
            def __init__(self, _path): pass
            def acquire(self): return True
            def release(self): pass
        with tempfile.TemporaryDirectory() as temp:
            supervisor, active = self._recoverable_active(Path(temp))
            ticks = []
            supervisor._consume_stop_request = lambda: bool(ticks)
            def tick(_seconds):
                ticks.append(supervisor.last_status.copy())
                self.assertIs(supervisor.active, active)
                self.assertTrue(active.adapter_suspended)
                self.assertEqual(supervisor.system.terminate_calls, [])
            supervisor.sleeper = tick
            with patch.object(supervisor, "_validate_static_dependencies"), patch.object(
                manager_module, "ProcessLock", FakeLock,
            ), patch.object(supervisor, "evaluate", return_value=active.evaluation), patch.object(
                supervisor, "_binding_still_current", return_value=False,
            ):
                status, _ = supervisor.run()
            self.assertEqual(ticks[0]["phase"], "waiting_adapter")
            self.assertEqual(active.bridge.commands, ["adapter-pause"])
            self.assertEqual(status["phase"], "stopped")

    def test_duplicate_start_preserves_live_owner_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary))
            supervisor._ensure_private_runtime()
            owner = {"phase": "ready", "owner_fixture": "do_not_replace"}
            atomic_private_json(supervisor.status_path, owner)
            before = supervisor.status_path.read_bytes()
            with manager_module.ProcessLock(supervisor.runtime_dir / "service.lock"):
                with patch.object(supervisor, "_validate_static_dependencies"):
                    result, code = supervisor.run(once=True)
            self.assertEqual(code, 2)
            self.assertEqual(result["code"], "service_already_running")
            self.assertEqual(supervisor.status_path.read_bytes(), before)

    def test_group_state_diagnostic_is_generation_bound_and_reflects_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            supervisor = object.__new__(Supervisor)
            active = SimpleNamespace(directory=directory, host_generation=41, connection_generation=42)
            group = {"state": "joined", "roomGeneration": 2, "hostGeneration": 41, "connectionGeneration": 42, "secret": "DO_NOT_ECHO"}
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "group_joined", "status": {"groupCall": group}})
            path = directory / "group-state.safe.json"
            saved = read_private_json(path)
            self.assertEqual(saved["roomGeneration"], 2)
            self.assertEqual(saved["state"], "joined")
            self.assertNotIn("DO_NOT_ECHO", path.read_text())
            group["hostGeneration"] = 40
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "group_idle", "status": {"groupCall": group}})
            self.assertEqual(read_private_json(path), saved)
            group.update(hostGeneration=41, state="idle")
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "group_idle", "status": {"groupCall": group}})
            self.assertEqual(read_private_json(path)["state"], "idle")

    def test_group_invite_diagnostic_persists_only_fixed_boolean_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            supervisor = object.__new__(Supervisor)
            active = SimpleNamespace(directory=directory, host_generation=41)
            fields = ("kernelInvitePresent", "kernelCallerPresent", "callerCorrelated", "groupCorrelated", "addressedToSelf", "ticketPublished")
            observation = {key: True for key in fields}
            observation["secret"] = "DO_NOT_ECHO"
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "group_invite_classified", "status": {"groupInvitationObservation": observation}})
            path = directory / "group-invite-classification.safe.json"
            saved = read_private_json(path)
            self.assertEqual(set(saved), set(fields) | {"hostGeneration", "observedAtMs"})
            self.assertNotIn("DO_NOT_ECHO", path.read_text())
            observation["addressedToSelf"] = "malformed"
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "group_invite_classified", "status": {"groupInvitationObservation": observation}})
            self.assertEqual(read_private_json(path), saved)

    def _supervisor(
        self,
        root: Path,
        *,
        config: dict | None = None,
        system: FakeSystem | None = None,
        rpc: FakeRpc | None = None,
        endpoint_inventory=None,
        monotonic_clock=None,
        identity_discovery_timeout_seconds: float = 15.0,
        universal_service_factory=None,
    ) -> Supervisor:
        root.mkdir(parents=True, exist_ok=True)
        config = config or _config(root)
        system = system or FakeSystem()
        rpc = rpc or FakeRpc()
        if universal_service_factory is None:
            # Every FakeSystem manager is explicitly isolated from production
            # UniversalVC process startup.
            universal_service_factory = lambda **_kwargs: FakeUniversalService()
        return Supervisor(
            config,
            system=system,
            rpc_factory=lambda _url, _token: rpc,
            endpoint_inventory=endpoint_inventory or _endpoint_inventory,
            project_root=root,
            clock_ms=lambda: 1_700_000_000_000,
            monotonic_clock=monotonic_clock,
            identity_discovery_timeout_seconds=identity_discovery_timeout_seconds,
            sleeper=lambda _seconds: None,
            universal_service_factory=universal_service_factory,
        )

    def test_snowluma_cached_identity_factory_is_limited_to_production_client(self) -> None:
        production_client = manager_module.OneBotClient
        snow_client = FakeRpc()
        snow_configured: list[tuple[str, str | None, dict[str, Any]]] = []

        def make_snow_client(url: str, token: str | None, **kwargs: Any) -> FakeRpc:
            snow_configured.append((url, token, kwargs))
            return snow_client

        production_responses = {
            "get_status": {"online": True},
            "get_login_info": {"user_id": "12345"},
            "get_stranger_info": {"user_id": "12345", "uid": SELF_UID},
        }

        def production_call(action, _params=None, *, timeout=None):
            return dict(production_responses[action])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snow_config = _config(root, adapter="snowluma")
            snow_supervisor = Supervisor(
                snow_config,
                system=FakeSystem(),
                rpc_factory=production_client,
                endpoint_inventory=_endpoint_inventory,
                project_root=root,
                clock_ms=lambda: 1_700_000_000_000,
                universal_service_factory=lambda **_kwargs: FakeUniversalService(),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", side_effect=make_snow_client) as snow_factory,
            ):
                result = snow_supervisor._onebot_identity()
            snow_factory.assert_called_once()
            self.assertEqual(result[2], SELF_UID)
            self.assertEqual(snow_configured[0][0], snow_config["onebot"]["url"])
            self.assertEqual(snow_configured[0][1], "token-fixture")
            self.assertEqual(snow_configured[0][2]["account_id"], "12345")
            self.assertEqual(snow_configured[0][2]["project_root"], Path(manager_module.__file__).resolve().parents[2])

            custom_calls: list[tuple[str, str | None]] = []
            custom_client = FakeRpc()

            def custom_factory(url: str, token: str | None) -> FakeRpc:
                custom_calls.append((url, token))
                return custom_client

            custom_supervisor = Supervisor(
                snow_config,
                system=FakeSystem(),
                rpc_factory=custom_factory,
                endpoint_inventory=_endpoint_inventory,
                project_root=root,
                clock_ms=lambda: 1_700_000_000_000,
                universal_service_factory=lambda **_kwargs: FakeUniversalService(),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="custom-token"),
                patch.object(manager_module, "SnowLumaOneBotClient", side_effect=AssertionError("custom factory must be preserved")),
            ):
                custom_supervisor._onebot_identity()
            self.assertEqual(custom_calls, [(snow_config["onebot"]["url"], "custom-token")])

            napcat_root = root / "napcat"
            napcat_root.mkdir()
            napcat_config = _config(napcat_root, adapter="napcat")
            napcat_supervisor = Supervisor(
                napcat_config,
                system=FakeSystem(),
                rpc_factory=production_client,
                endpoint_inventory=_endpoint_inventory,
                project_root=root,
                clock_ms=lambda: 1_700_000_000_000,
                universal_service_factory=lambda **_kwargs: FakeUniversalService(),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="napcat-token"),
                patch.object(manager_module, "SnowLumaOneBotClient", side_effect=AssertionError("NapCat must keep OneBotClient")),
                patch.object(production_client, "call", side_effect=production_call),
            ):
                self.assertEqual(napcat_supervisor._onebot_identity()[2], SELF_UID)

    def test_snow_self_identity_uses_cached_accessor_after_online_and_exact_login(self) -> None:
        class CachedSnowClient(manager_module._SNOWLUMA_ONEBOT_CLIENT_TYPE):
            def __init__(
                self,
                root: Path,
                *,
                online: bool = True,
                account: str = "12345",
                status_data: dict[str, Any] | None = None,
                login_error: Any = None,
                resolve_error: Any = None,
            ):
                super().__init__(
                    "ws://127.0.0.1:3001/",
                    "token-fixture",
                    account_id="12345",
                    project_root=root,
                )
                self.online = online
                self.account = account
                self.status_data = status_data
                self.login_error = login_error
                self.resolve_error = resolve_error
                self.actions: list[str] = []
                self.resolve_count = 0

            def call(self, action: str, params=None, *, timeout=None):
                self.actions.append(action)
                if action == "get_status":
                    return copy.deepcopy(self.status_data) if self.status_data is not None else {"online": self.online}
                if action == "get_login_info":
                    if self.login_error is not None:
                        raise self.login_error
                    return {"user_id": self.account}
                raise AssertionError("Snow self lookup must not request a profile")

            def resolve_self_uid(self, *, timeout=None):
                self.resolve_count += 1
                if self.resolve_error is not None:
                    raise self.resolve_error
                return SELF_UID

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, adapter="snowluma")
            supervisor = self._supervisor(root, config=config)
            supervisor.rpc_factory = manager_module.OneBotClient
            client = CachedSnowClient(root)
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=client),
            ):
                result = supervisor._onebot_identity()

            self.assertIs(result[0], client)
            self.assertEqual(result[1], {"user_id": "12345", "uid": SELF_UID})
            self.assertEqual(result[2], SELF_UID)
            self.assertEqual(client.actions, ["get_status", "get_login_info"])
            self.assertEqual(client.resolve_count, 1)

            for status_data in (
                {"online": True, "good": False},
                {"online": True, "good": True},
                {"online": True},
            ):
                accepted = CachedSnowClient(root, status_data=status_data)
                with (
                    patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                    patch.object(manager_module, "SnowLumaOneBotClient", return_value=accepted),
                ):
                    result = supervisor._onebot_identity()
                self.assertEqual(result[2], SELF_UID)
                self.assertEqual(accepted.actions, ["get_status", "get_login_info"])
                self.assertEqual(accepted.resolve_count, 1)

            for online, account, expected in (
                (False, "12345", "onebot_offline"),
                (True, "12346", "onebot_account_mismatch"),
                (True, "not-a-valid-uin", "onebot_identity_unavailable"),
            ):
                rejected = CachedSnowClient(root, online=online, account=account)
                with (
                    patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                    patch.object(manager_module, "SnowLumaOneBotClient", return_value=rejected),
                    self.assertRaises(ServiceError) as caught,
                ):
                    supervisor._onebot_identity()
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(rejected.resolve_count, 0)
                self.assertNotIn("get_stranger_info", rejected.actions)

            for status_data in (
                {"online": False, "good": True},
                {"good": True},
                {"online": 1, "good": True},
                {"online": True, "good": 0},
            ):
                rejected = CachedSnowClient(root, status_data=status_data)
                with (
                    patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                    patch.object(manager_module, "SnowLumaOneBotClient", return_value=rejected),
                    self.assertRaises(ServiceError) as caught,
                ):
                    supervisor._onebot_identity()
                self.assertEqual((caught.exception.code, caught.exception.phase), ("onebot_offline", "waiting_login"))
                self.assertEqual(rejected.actions, ["get_status"])
                self.assertEqual(rejected.resolve_count, 0)

            unhealthy_path_wrong_account = CachedSnowClient(
                root, status_data={"online": True, "good": False}, account="12346",
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=unhealthy_path_wrong_account),
                self.assertRaises(ServiceError) as caught,
            ):
                supervisor._onebot_identity()
            self.assertEqual(caught.exception.code, "onebot_account_mismatch")
            self.assertEqual(unhealthy_path_wrong_account.actions, ["get_status", "get_login_info"])
            self.assertEqual(unhealthy_path_wrong_account.resolve_count, 0)

            unhealthy_path_timeout = CachedSnowClient(
                root,
                status_data={"online": True, "good": False},
                login_error=OneBotError("onebot_timeout"),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=unhealthy_path_timeout),
                self.assertRaises(ServiceError) as caught,
            ):
                supervisor._onebot_identity()
            self.assertEqual((caught.exception.code, caught.exception.phase), ("onebot_timeout", "waiting_login"))
            self.assertEqual(caught.exception._diagnostic_stage, "onebot_login_rpc_wait_timeout")
            self.assertEqual(unhealthy_path_timeout.actions, ["get_status", "get_login_info"])
            self.assertEqual(unhealthy_path_timeout.resolve_count, 0)

            cache_failure = CachedSnowClient(
                root,
                resolve_error=manager_module.SnowLumaIdentityError("snowluma_identity_timeout"),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=cache_failure),
                self.assertRaises(ServiceError) as caught,
            ):
                supervisor._onebot_identity()
            self.assertEqual((caught.exception.code, caught.exception.phase), ("self_uid_unavailable", "waiting_identity"))
            self.assertEqual(cache_failure.resolve_count, 1)
            self.assertNotIn("get_stranger_info", cache_failure.actions)

    def test_snow_auto_discovery_identity_recheck_rereads_cached_self_uid(self) -> None:
        class CachedSnowClient(manager_module._SNOWLUMA_ONEBOT_CLIENT_TYPE):
            def __init__(self, root: Path, cached_uid: str):
                super().__init__(
                    "ws://127.0.0.1:3001/",
                    "token-fixture",
                    account_id="12345",
                    project_root=root,
                )
                self.cached_uid = cached_uid
                self.actions: list[str] = []
                self.resolve_timeouts: list[float | None] = []

            def call(self, action: str, params=None, *, timeout=None):
                self.actions.append(action)
                if action == "get_login_info":
                    return {"user_id": "12345"}
                raise AssertionError("Snow self recheck must not request a profile")

            def resolve_self_uid(self, *, timeout=None):
                self.resolve_timeouts.append(timeout)
                return self.cached_uid

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root, config=_config(root, adapter="snowluma"))
            matching = CachedSnowClient(root, SELF_UID)
            supervisor._verify_onebot_identity(matching, SELF_UID, time.monotonic() + 2.0)
            self.assertEqual(matching.actions, ["get_login_info"])
            self.assertEqual(len(matching.resolve_timeouts), 1)
            self.assertGreater(matching.resolve_timeouts[0], 0)
            self.assertLessEqual(matching.resolve_timeouts[0], 2.0)

            changed = CachedSnowClient(root, "u_" + ("e" * 22))
            with self.assertRaises(ServiceError) as caught:
                supervisor._verify_onebot_identity(changed, SELF_UID, time.monotonic() + 2.0)
            self.assertEqual(caught.exception.code, "self_uid_changed")
            self.assertEqual(changed.actions, ["get_login_info"])
            self.assertEqual(len(changed.resolve_timeouts), 1)

    def test_snow_cached_self_uid_change_rejects_and_evicts_discovered_peer_map(self) -> None:
        changed_uid = "u_" + ("e" * 22)

        class CachedSnowClient(manager_module._SNOWLUMA_ONEBOT_CLIENT_TYPE):
            def __init__(self, root: Path):
                super().__init__(
                    "ws://127.0.0.1:3001/",
                    "token-fixture",
                    account_id="12345",
                    project_root=root,
                )
                self.cached_uids = [SELF_UID, SELF_UID, SELF_UID, changed_uid]
                self.self_profile_requests = 0
                self.peer_profile_requests = 0

            def call(self, action: str, params=None, *, timeout=None):
                params = params or {}
                if action == "get_status":
                    return {"online": True}
                if action == "get_login_info":
                    return {"user_id": "12345"}
                if action == "get_stranger_info" and str(params.get("user_id")) == "12345":
                    self.self_profile_requests += 1
                    raise AssertionError("Snow self identity must not use profile RPC")
                if action == "get_stranger_info" and str(params.get("user_id")) == "20001":
                    self.peer_profile_requests += 1
                    return {"user_id": "20001", "uid": PEER_UID}
                raise AssertionError("unexpected OneBot action")

            def get_friend_list(self, *, timeout=None):
                return [{"user_id": "20001"}]

            def resolve_self_uid(self, *, timeout=None):
                return self.cached_uids.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, adapter="snowluma")
            system = FakeSystem(manifest={
                "schema": 1,
                "adapter": "snowluma",
                "accountId": "12345",
                "connected": True,
                "adapterPid": 8095,
                "adapterCreationTime100ns": "809500",
                "connectionGeneration": "a1b2c3d4",
                "updatedAtMs": 1_700_000_000_000,
            })
            supervisor = self._supervisor(root, config=config, system=system)
            supervisor.rpc_factory = manager_module.OneBotClient
            client = CachedSnowClient(root)

            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=client),
            ):
                first = supervisor.evaluate()
                self.assertEqual(first.peer_map, {PEER_UID: "20001"})
                self.assertTrue(supervisor._resolved_peers)

                with self.assertRaises(ServiceError) as caught:
                    supervisor.evaluate()

            self.assertEqual(caught.exception.code, "self_uid_changed")
            self.assertFalse(supervisor._resolved_peers)
            self.assertEqual(client.self_profile_requests, 0)
            self.assertEqual(client.peer_profile_requests, 1)

    def test_snowluma_cached_uid_failure_waits_for_identity_not_login(self) -> None:
        class CacheFailureRpc(FakeRpc):
            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                if action == "get_stranger_info":
                    raise manager_module.SnowLumaIdentityError("snowluma_identity_cache_unavailable")
                return super().call(action, params, timeout=timeout)

        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary), adapter="snowluma")
            supervisor = Supervisor(
                config,
                system=FakeSystem(),
                rpc_factory=manager_module.OneBotClient,
                endpoint_inventory=_endpoint_inventory,
                universal_service_factory=lambda **_kwargs: FakeUniversalService(),
            )
            with (
                patch.object(manager_module, "_read_token_file", return_value="token-fixture"),
                patch.object(manager_module, "SnowLumaOneBotClient", return_value=CacheFailureRpc()),
                self.assertRaises(ServiceError) as caught,
            ):
                supervisor._onebot_identity()
            self.assertEqual((caught.exception.code, caught.exception.phase), ("self_uid_unavailable", "waiting_identity"))
            self.assertIsNone(caught.exception._diagnostic_stage)

    def test_onebot_timeout_stage_identifies_only_the_active_rpc_action(self) -> None:
        expected_stages = {
            "get_status": "onebot_status_rpc_wait_timeout",
            "get_login_info": "onebot_login_rpc_wait_timeout",
            "get_stranger_info": "onebot_self_rpc_wait_timeout",
        }

        class ActionTimeoutRpc(FakeRpc):
            def __init__(self, timeout_action: str):
                super().__init__()
                self.timeout_action = timeout_action

            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                if action == self.timeout_action:
                    self.calls.append((action, params or {}))
                    self.timeouts.append(timeout)
                    raise OneBotError("onebot_timeout")
                return super().call(action, params, timeout=timeout)

        for action, expected_stage in expected_stages.items():
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = _config(root)
                rpc = ActionTimeoutRpc(action)
                supervisor = self._supervisor(root, config=config, rpc=rpc)
                with patch.object(manager_module, "_read_token_file", return_value="token-fixture"):
                    with self.assertRaises(ServiceError) as caught:
                        supervisor._onebot_identity()

                self.assertEqual((caught.exception.code, caught.exception.phase), ("onebot_timeout", "waiting_login"))
                self.assertEqual(caught.exception._diagnostic_stage, expected_stage)
                self.assertEqual(rpc.calls[-1][0], action)

    def test_enabled_run_attempts_universal_service_before_adapter_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            supervisor = self._supervisor(root, config=config)
            events: list[str] = []

            def ensure_universal():
                events.append("universal")
                supervisor.universal_service_status = {
                    "state": "ready",
                    "code": "universal_api_service_ready",
                    "reused": True,
                }

            def evaluate():
                events.append("evaluate")
                return SimpleNamespace(fingerprint=(51200, 512000))

            with (
                patch.object(supervisor, "_validate_static_dependencies", return_value=None),
                patch.object(supervisor, "_ensure_universal_service", side_effect=ensure_universal),
                patch.object(supervisor, "evaluate", side_effect=evaluate),
                patch.object(supervisor, "_kernel_quarantined", return_value=True),
            ):
                status, code = supervisor.run(once=True)

            self.assertEqual(events, ["universal", "evaluate"])
            self.assertEqual(code, 0)
            self.assertEqual(status["universalService"]["state"], "ready")

    def test_host_runtime_wait_requires_matching_health_report_and_listener_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            generation_dir = root / "host-generation"
            generation_dir.mkdir()
            report_base = root / "runtime-host"
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(report_base),
                                host_generation=41, launcher_creation_time_100ns=70000,
                                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            host_image = supervisor.config["paths"]["qq_exe"]
            owned = [{
                "role": "host", "pid": 8100, "creationTime100ns": "810000",
                "imagePath": host_image, "profilePath": str(root / "profile"),
            }]
            active = ActiveGeneration(
                evaluation, 41, 42, generation_dir, launch, owned, 8100, "810000",
                "sidecar", FakeBridge(True),
            )
            active.bridge.events.extend([
                {"kind": "bridge_status", "event": "host_availability", "status": {
                    "hostGeneration": 41, "hostPid": 8100, "hostAvailable": False,
                }},
                {"kind": "bridge_status", "event": "host_availability", "status": {
                    "hostGeneration": 41, "hostPid": 8100, "hostAvailable": True,
                }},
            ])
            active.host_runtime_ready = False
            report_path = Path(str(report_base) + ".host.status.json")
            report_path.write_text(json.dumps({
                "phase": "ready", "hostGeneration": 41, "targetPid": 8100,
                "lastFailureCode": None, "ready": True, "listening": True, "stopped": False,
            }), encoding="utf-8")
            supervisor.system.listener_owners = lambda port: [8100]
            supervisor.system.process_identity = lambda pid: (810000, host_image) if pid == 8100 else None
            supervisor._await_host_runtime_ready(active, timeout_seconds=0.1)
            self.assertTrue(active.host_runtime_ready)

            supervisor.system.listener_owners = lambda _port: [8200]
            with self.assertRaisesRegex(ServiceError, "host_listener_owner_mismatch"):
                supervisor._await_host_runtime_ready(active, timeout_seconds=0.1)

    def test_host_runtime_failure_surfaces_exact_callback_gate_code_and_binding_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "host-generation"
            directory.mkdir()
            report_base = root / "runtime-host"
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(report_base),
                                host_generation=41, launcher_creation_time_100ns=70000,
                                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000",
                "sidecar", FakeBridge(True),
            )
            report_path = Path(str(report_base) + ".host.status.json")
            report_path.write_text(json.dumps({
                "phase": "failed", "hostGeneration": 41, "targetPid": 8100,
                "lastFailureCode": "output_callback_not_ready", "ready": False,
                "listening": False, "stopped": True,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ServiceError, "output_callback_not_ready"):
                supervisor._await_host_runtime_ready(active, timeout_seconds=0.1)

            report_path.write_text(json.dumps({
                "phase": "initializing", "hostGeneration": 41, "targetPid": 8101,
                "lastFailureCode": None, "ready": False, "listening": True, "stopped": False,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ServiceError, "host_status_binding_mismatch"):
                supervisor._await_host_runtime_ready(active, timeout_seconds=0.1)

    def test_bridge_startup_stderr_is_sanitized_and_keeps_the_specific_failure_code(self) -> None:
        emitted: list[dict[str, str]] = []
        parser = BridgeProcess.__new__(BridgeProcess)
        parser.events = SimpleNamespace(put_nowait=emitted.append)
        parser._read_safe_failure_line("QQCall Bridge CLI failed: output_listener_failed")
        self.assertEqual(emitted, [{"kind": "bridge_startup_error", "code": "output_listener_failed"}])
        self.assertEqual(parser.last_failure_code, "output_listener_failed", "safe code is latched before the event is queued")
        parser._read_safe_failure_line("QQCall Bridge CLI failed: NOT_A_SAFE_CODE")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(parser.last_failure_code, "output_listener_failed")

        agent_parser = BridgeProcess.__new__(BridgeProcess)
        agent_events: list[dict[str, str]] = []
        agent_parser.events = SimpleNamespace(put_nowait=agent_events.append)
        agent_parser._read_safe_failure_line('{"kind":"auto_bootstrap_failed","code":"login_ack_timeout"}')
        self.assertEqual(agent_parser.last_failure_code, "login_ack_timeout")
        self.assertEqual(agent_events, [{"kind": "auto_bootstrap_failed", "code": "login_ack_timeout"}])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "host-generation"
            directory.mkdir()
            active = ActiveGeneration(
                evaluation, 41, 42, directory,
                HostLaunch(7000, str(root), str(root / "profile"), str(root / "report")),
                [], 8100, "810000", "sidecar", FakeBridge(True),
            )
            active.bridge.events.append(emitted[0])
            with self.assertRaisesRegex(ServiceError, "output_listener_failed"):
                supervisor._await_bridge_ready(active, timeout_seconds=0.1)

    def test_steady_state_host_failure_cannot_be_hidden_by_previous_login_or_later_ready_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "host-generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(root / "host-report"),
                                host_generation=41, launcher_creation_time_100ns=70000,
                                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            active = ActiveGeneration(evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", FakeBridge(True))
            active.account_path_verified = True
            active.login_ready = True
            # Connection refusal before first Host readiness is normal startup waiting.
            supervisor._consume_bridge_event(active, {"kind": "host_failure", "code": "host_disconnected"})
            self.assertFalse(active.host_runtime_failure_code)
            active.host_runtime_ready = True
            active.bridge.events.extend([
                {"kind": "host_failure", "code": "output_forward_failed"},
                {"kind": "bridge_status", "event": "host_availability", "status": {
                    "hostGeneration": 41, "hostPid": 8100, "hostAvailable": False, "lastFailureCode": "host_disconnected"}},
                {"kind": "bridge_status", "event": "host_availability", "status": {
                    "hostGeneration": 41, "hostPid": 8100, "hostAvailable": True}},
                {"kind": "bridge_status", "event": "host_availability", "status": None},
                {"kind": "bridge_status", "event": "host_availability", "status": {
                    "hostGeneration": 40, "hostPid": 8100, "hostAvailable": "true"}},
            ])
            with self.assertRaisesRegex(ServiceError, "output_forward_failed"):
                supervisor._monitor_active(active)
            self.assertFalse(active.host_runtime_ready)
            self.assertEqual(active.host_runtime_failure_code, "output_forward_failed")
            self.assertTrue(active.account_path_verified)
            self.assertTrue(active.login_ready)

    def test_steady_state_host_report_failure_is_scoped_and_never_echoes_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "host-generation"
            directory.mkdir()
            report_base = root / "host-report"
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(report_base), host_generation=41,
                                launcher_creation_time_100ns=70000, launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            active = ActiveGeneration(evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", FakeBridge(True))
            active.account_path_verified = True
            active.login_ready = True
            active.host_runtime_ready = True
            path = Path(str(report_base) + ".host.status.json")
            for failure, expected in (("output_forward_failed", "output_forward_failed"),
                                      ({"secret": "DO_NOT_ECHO"}, "host_failed")):
                with self.subTest(failure=expected):
                    active.host_runtime_failure_code = ""
                    path.write_text(json.dumps({"phase": "output_failure", "hostGeneration": 41,
                                                "targetPid": 8100, "lastFailureCode": failure}), encoding="utf-8")
                    with self.assertRaises(ServiceError) as caught:
                        supervisor._monitor_active(active)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn("DO_NOT_ECHO", str(caught.exception))
            path.write_text(json.dumps({"phase": "failed", "hostGeneration": 40, "targetPid": 8100}), encoding="utf-8")
            with self.assertRaisesRegex(ServiceError, "host_status_binding_mismatch"):
                supervisor._host_report_failure(active)

    def test_host_availability_requires_boolean_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            directory = root / "host-generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(root / "host-report"), host_generation=41,
                                launcher_creation_time_100ns=70000, launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            active = ActiveGeneration(supervisor.evaluate(), 41, 42, directory, launch, [], 8100, "810000", "sidecar", FakeBridge(True))
            active.host_runtime_ready = True
            supervisor._consume_bridge_event(active, {"kind": "bridge_status", "event": "host_availability", "status": {
                "hostGeneration": 41, "hostPid": 8100, "hostAvailable": "true"}})
            self.assertFalse(active.host_runtime_ready)
            self.assertEqual(active.host_runtime_failure_code, "host_status_invalid")

    def test_business_rejections_do_not_terminate_a_healthy_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            directory = root / "host-generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(root / "host-report"), host_generation=41)
            active = ActiveGeneration(supervisor.evaluate(), 41, 42, directory, launch, [], 8100, "810000", "sidecar", FakeBridge(True))
            active.account_path_verified = True
            active.login_ready = True
            active.host_runtime_ready = True
            for code in ("group_start_refused", "group_quit_refused", "group_room_query_failed",
                         "audio_device_selection_failed", "generation_mismatch", {"private": "DO_NOT_ECHO"}):
                with self.subTest(code=type(code).__name__ if type(code) is not str else code):
                    active.bridge.events.append({"kind": "host_failure", "code": code})
                    self.assertEqual(supervisor._monitor_active(active), "ready")
                    self.assertTrue(active.host_runtime_ready)
                    self.assertEqual(active.host_runtime_failure_code, "")
            active.bridge.events.append({"kind": "bridge_status", "event": "host_availability", "status": {
                "hostGeneration": 41, "hostPid": 8100, "hostAvailable": False}})
            with self.assertRaisesRegex(ServiceError, "host_runtime_unavailable"):
                supervisor._monitor_active(active)

    def test_business_timeout_keeps_host_but_health_timeout_terminates_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            directory = root / "host-generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(root / "host-report"), host_generation=41)
            active = ActiveGeneration(supervisor.evaluate(), 41, 42, directory, launch, [], 8100, "810000", "sidecar", FakeBridge(True))
            active.account_path_verified = True
            active.login_ready = True
            active.host_runtime_ready = True
            for operation in ("group_room_query", "group_start", "group_quit", "audio_device_selection", "invoke"):
                active.bridge.events.append({"kind": "host_failure", "code": "host_request_timeout", "operation": operation})
                self.assertEqual(supervisor._monitor_active(active), "ready")
                self.assertEqual(active.host_runtime_failure_code, "")
            active.bridge.events.append({"kind": "host_failure", "code": "host_request_timeout", "operation": "health"})
            with self.assertRaisesRegex(ServiceError, "host_request_timeout") as caught:
                supervisor._monitor_active(active)
            self.assertFalse(active.host_runtime_ready)
            self.assertEqual(caught.exception._diagnostic_stage, "host_health")

    def test_run_cleans_up_failed_host_without_restarting_consumed_kernel(self) -> None:
        for detached in (True, False):
            with self.subTest(detached=detached), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                supervisor = self._supervisor(root)
                evaluation = supervisor.evaluate()
                directory = supervisor.runtime_dir / "generations" / "connection-42"
                directory.mkdir(parents=True)
                launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"),
                                    host_generation=41, launcher_creation_time_100ns=70000,
                                    launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
                bridge = FakeBridge(detached)
                bridge.events.append({"kind": "host_failure", "code": "output_forward_failed"})
                active = ActiveGeneration(evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge)
                active.account_path_verified = True
                active.login_ready = True
                active.host_runtime_ready = True
                atomic_private_json(directory / "attach-attempt.private.json", {"attempted": True})
                supervisor.active = active
                supervisor._validate_static_dependencies = lambda: None
                supervisor._ensure_universal_service = lambda: None
                supervisor._recover_prior_attach_attempt = lambda: None
                supervisor._current_supervisor_identity = lambda: (700, 700000)
                supervisor._write_run_identity = lambda: None
                with patch.object(supervisor, "_start_generation", side_effect=AssertionError("must not restart QQ")):
                    status, exit_code = supervisor.run(once=True)
                self.assertFalse(status["ready"])
                self.assertEqual((status["phase"], status["code"], exit_code), ("error", "output_forward_failed", 2))
                self.assertTrue(supervisor._kernel_quarantined(evaluation))
                self.assertEqual(len(supervisor.system.terminate_calls), 1)
                self.assertIsNone(supervisor.active)
                record = read_private_json(directory / "supervisor-termination.private.json")
                self.assertEqual(record["supervisorFailureCode"], "output_forward_failed")
                self.assertEqual(record["supervisorFailureStage"], "host_transport")

    def test_first_rejected_startup_output_is_strictly_bound_and_saved_privately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation-42"
            directory.mkdir()
            active = ActiveGeneration(
                evaluation, 41, 42, directory,
                HostLaunch(7000, str(root), str(root / "profile"), str(root / "report")),
                [], 8100, "810000", "sidecar", FakeBridge(True),
            )
            safe_metadata = {
                "command": 1,
                "id": 1,
                "hostGeneration": 41,
                "connectionGeneration": 42,
                "sequence": 3,
                "reasonCode": "kernel_listener_not_attached",
            }
            private_path = directory / "startup-failure.private.json"
            invalid_fixtures = [
                (dict(safe_metadata, command=True), 41, 42),
                (dict(safe_metadata, sequence=0), 41, 42),
                (dict(safe_metadata, hostGeneration=99), 41, 42),
                (dict(safe_metadata, reasonCode="renderer_crashed"), 41, 42),
                (dict(safe_metadata, rawValue="LEAK_UID_TOKEN_STDOUT_MARKER"), 41, 42),
                (dict(safe_metadata), 99, 42),
            ]
            for candidate, host_generation, connection_generation in invalid_fixtures:
                supervisor._consume_bridge_event(active, {
                    "kind": "bridge_status",
                    "event": "output_rejected",
                    "status": {
                        "attached": False,
                        "hostGeneration": host_generation,
                        "connectionGeneration": connection_generation,
                        "firstRejectedOutput": candidate,
                    },
                })
                self.assertFalse(private_path.exists(), "invalid metadata must not be persisted")

            supervisor._consume_bridge_event(active, {
                "kind": "bridge_status",
                "event": "output_rejected",
                "status": {
                    "attached": False,
                    "hostGeneration": 41,
                    "connectionGeneration": 42,
                    "firstRejectedOutput": safe_metadata,
                },
            })
            first_record = {
                "schema": 1,
                "hostGeneration": 41,
                "connectionGeneration": 42,
                "firstRejectedOutput": safe_metadata,
            }
            self.assertEqual(json.loads(private_path.read_text(encoding="utf-8")), first_record)

            later_metadata = dict(safe_metadata, command=5, id=88, sequence=4)
            supervisor._consume_bridge_event(active, {
                "kind": "bridge_status",
                "event": "output_rejected",
                "status": {
                    "attached": False,
                    "hostGeneration": 41,
                    "connectionGeneration": 42,
                    "firstRejectedOutput": later_metadata,
                },
            })
            supervisor._consume_bridge_event(active, {
                "kind": "bridge_status",
                "event": "host_availability",
                "status": {
                    "hostGeneration": 41,
                    "hostPid": 8100,
                    "hostAvailable": False,
                    "lastFailureCode": "renderer_crashed",
                },
            })
            private_text = private_path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(private_text), first_record, "cleanup failure must not replace the first output cause")
            self.assertNotIn("LEAK_UID_TOKEN_STDOUT_MARKER", private_text)
            self.assertNotIn("firstRejectedOutput", supervisor.last_status, "public service status schema stays unchanged")

    def test_runtime_cleanup_refreshes_only_the_previously_pinned_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            report = str(root / "host-report")
            launch = HostLaunch(7000, str(root), str(root / "profile"), report,
                                host_generation=41, launcher_creation_time_100ns=70000,
                                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe")
            image = "C:\\QQ\\QQ.exe"
            pinned = [
                {"role": "launcher", "pid": 7000, "creationTime100ns": "70000", "imagePath": "C:\\loader\\NapCatWinBootMain.exe"},
                {"role": "host", "pid": 8100, "creationTime100ns": "810000", "imagePath": image},
                {"role": "host_child", "pid": 8101, "creationTime100ns": "810100", "imagePath": image},
            ]
            refreshed = pinned[:2] + [
                {"role": "host_child", "pid": 8102, "creationTime100ns": "810200", "imagePath": image},
            ]
            supervisor.system.owned_host_processes = lambda _launch, _config: refreshed
            cleanup = supervisor._refresh_owned_hosts(launch, pinned)
            supervisor.system.terminate_owned(cleanup)
            self.assertEqual({entry["pid"] for entry in cleanup}, {7000, 8100, 8101, 8102})
            self.assertEqual(
                {entry["pid"] for entry in supervisor.system.terminate_calls[-1]},
                {7000, 8100, 8101, 8102},
                "orphaned exact children remain owned and newly verified children are added",
            )
            self.assertNotIn(8200, {entry["pid"] for entry in cleanup}, "a separate profile tree is never discovered")

            reused_root = dict(refreshed[1], creationTime100ns="819999")
            supervisor.system.owned_host_processes = lambda _launch, _config: [refreshed[0], reused_root, *refreshed[2:]]
            rejected = supervisor._refresh_owned_hosts(launch, pinned)
            self.assertEqual(rejected, pinned, "a sidecar root PID reuse cannot replace the pinned root")
            supervisor.system.terminate_owned(rejected)
            self.assertNotIn(81999, {entry["pid"] for entry in supervisor.system.terminate_calls[-1]})

            changed_pid_root = dict(refreshed[1], pid=8201)
            supervisor.system.owned_host_processes = lambda _launch, _config: [refreshed[0], changed_pid_root, *refreshed[2:]]
            self.assertEqual(
                supervisor._refresh_owned_hosts(launch, pinned),
                pinned,
                "a changed sidecar root PID cannot replace the first pinned root",
            )

            changed_image_root = dict(refreshed[1], imagePath="C:\\personal\\QQ.exe")
            supervisor.system.owned_host_processes = lambda _launch, _config: [refreshed[0], changed_image_root, *refreshed[2:]]
            self.assertEqual(
                supervisor._refresh_owned_hosts(launch, pinned),
                pinned,
                "a changed sidecar root image cannot replace the first pinned root",
            )

    def test_stop_active_captures_owned_tree_before_detach_and_unions_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            launch = HostLaunch(
                7000, str(root), str(root / "profile"), str(root / "host-report"),
                host_generation=41, launcher_creation_time_100ns=70000,
                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe",
            )
            image = "C:\\QQ\\QQ.exe"
            initial = [
                {"role": "launcher", "pid": 7000, "creationTime100ns": "70000", "imagePath": launch.launcher_image_path, "profilePath": launch.profile_path},
                {"role": "host", "pid": 8100, "creationTime100ns": "810000", "imagePath": image, "profilePath": launch.profile_path},
            ]
            snapshots = [
                [*initial, {"role": "host_child", "pid": 8101, "creationTime100ns": "810100", "imagePath": image, "profilePath": launch.profile_path}],
                [*initial, {"role": "host_child", "pid": 8102, "creationTime100ns": "810200", "imagePath": image, "profilePath": launch.profile_path}],
            ]
            events: list[str] = []

            def refresh(_launch, _config):
                events.append("refresh")
                return snapshots.pop(0)

            supervisor.system.owned_host_processes = refresh

            def terminate(records):
                events.append("terminate")
                supervisor.system.terminate_calls.append(records)
                return True

            supervisor.system.terminate_owned = terminate
            bridge = FakeBridge(True)
            bridge.stop = lambda timeout=12.0: (events.append("bridge_stop") or True)
            active = ActiveGeneration(
                evaluation, 41, 42, root / "generation", launch, initial,
                8100, "810000", "sidecar", bridge,
            )
            supervisor.active = active

            self.assertTrue(supervisor._stop_active())
            self.assertEqual(events, ["refresh", "bridge_stop", "refresh", "terminate"])
            self.assertEqual(
                {item["pid"] for item in supervisor.system.terminate_calls[-1]},
                {7000, 8100, 8101, 8102},
            )

    def test_stop_active_persists_first_controller_and_host_cause_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(
                7000, str(root), str(root / "profile"), str(directory / "host-report"),
                host_generation=41, launcher_creation_time_100ns=70000,
                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe",
            )
            atomic_private_json(directory / "agent-report.controller-exit.private.json", {
                "schema": 1,
                "errorClass": "uncaught_exception",
                "failureCode": "controller_uncaught_exception",
                "origin": "uncaughtException",
                "observedAtMs": 1_700_000_000_001,
                "exitedAtMs": 1_700_000_000_002,
                "exitCode": 1,
            })
            atomic_private_json(Path(launch.report_path + ".host.status.json"), {
                "hostGeneration": 41,
                "targetPid": 8100,
                "phase": "renderer_failure",
                "lastFailureCode": "renderer_crashed",
                "ready": False,
            })
            bridge = FakeBridge(True)
            bridge.process.poll = lambda: -15
            active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )
            observed_before_bridge_stop: list[dict[str, Any]] = []

            def stop_bridge(timeout: float = 12.0) -> bool:
                record = read_private_json(directory / "supervisor-termination.private.json")
                observed_before_bridge_stop.append(record)
                return True

            bridge.stop = stop_bridge
            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="controller_exit", phase="waiting_kernel_restart",
            ))
            self.assertEqual(len(observed_before_bridge_stop), 1)
            record = observed_before_bridge_stop[0]
            self.assertEqual(record["observedAtMs"], 1_700_000_000_000)
            self.assertEqual(record["triggerCode"], "controller_exit")
            self.assertEqual(record["phase"], "waiting_kernel_restart")
            self.assertEqual(record["controllerPollReturnCode"], -15)
            self.assertEqual(record["controllerFailureCode"], "controller_uncaught_exception")
            self.assertEqual(record["controllerExceptionOrigin"], "uncaughtException")
            self.assertEqual(record["controllerExitAtMs"], 1_700_000_000_002)
            self.assertEqual(record["controllerObserverExitCode"], 1)
            self.assertEqual(record["hostPhase"], "renderer_failure")
            self.assertEqual(record["hostFailureCode"], "renderer_crashed")

            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="explicit_stop", phase="stopping", failure_code="later_reason",
            ))
            self.assertEqual(
                read_private_json(directory / "supervisor-termination.private.json"),
                record,
                "the first termination cause is preserved across repeated cleanup attempts",
            )

    def test_peer_identity_stage_is_allowlisted_first_writer_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"))
            bridge = FakeBridge(True)
            active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )
            observed_before_cleanup: list[dict[str, Any]] = []

            def stop_bridge(timeout: float = 12.0) -> bool:
                observed_before_cleanup.append(read_private_json(directory / "supervisor-termination.private.json"))
                return True

            bridge.stop = stop_bridge
            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="service_error",
                phase="waiting_identity",
                failure_code="peer_identity_timeout",
                failure_stage="final_binding_fence_wait_timeout",
            ))
            first_record = observed_before_cleanup[0]
            self.assertEqual(first_record["supervisorFailureCode"], "peer_identity_timeout")
            self.assertEqual(first_record["supervisorFailureStage"], "final_binding_fence_wait_timeout")

            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="service_error",
                phase="waiting_identity",
                failure_code="peer_identity_timeout",
                failure_stage="untrusted raw detail\\nUID/token",
            ))
            self.assertEqual(
                read_private_json(directory / "supervisor-termination.private.json"),
                first_record,
                "later cleanup must not replace the first stage",
            )

            invalid_error = ServiceError(
                "peer_identity_timeout", "waiting_identity", diagnostic_stage="untrusted raw detail\\nUID/token",
            )
            self.assertIsNone(invalid_error._diagnostic_stage)

            unsafe_directory = root / "unsafe-stage"
            unsafe_directory.mkdir()
            self.assertTrue(supervisor._persist_controller_termination(
                directory=unsafe_directory,
                bridge=None,
                host_generation=42,
                host_pid=None,
                host_report_path=None,
                trigger_code="service_error",
                phase="waiting_identity",
                failure_code="peer_identity_timeout",
                failure_stage={"stage": "final_binding_fence_wait_timeout", "token": "redacted"},
            ))
            unsafe_record = read_private_json(unsafe_directory / "supervisor-termination.private.json")
            self.assertIsNone(unsafe_record["supervisorFailureStage"])

    def test_onebot_timeout_stage_is_code_keyed_private_and_first_writer(self) -> None:
        class _StageSubclass(str):
            pass

        invalid_cases = (
            ("onebot_timeout", "unrecognized_onebot_stage"),
            ("onebot_timeout", _StageSubclass("onebot_status_rpc_wait_timeout")),
            ("peer_identity_timeout", "onebot_status_rpc_wait_timeout"),
            ("onebot_offline", "onebot_login_rpc_wait_timeout"),
        )
        for code, stage in invalid_cases:
            with self.subTest(code=code, stage_type=type(stage).__name__):
                self.assertIsNone(ServiceError(code, "waiting_login", diagnostic_stage=stage)._diagnostic_stage)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"))
            bridge = FakeBridge(True)
            active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )
            reports_seen_before_cleanup: list[dict[str, Any]] = []

            def stop_bridge(timeout: float = 12.0) -> bool:
                reports_seen_before_cleanup.append(
                    read_private_json(directory / "supervisor-termination.private.json")
                )
                return True

            bridge.stop = stop_bridge
            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="service_error",
                phase="waiting_login",
                failure_code="onebot_timeout",
                failure_stage="onebot_login_rpc_wait_timeout",
            ))
            first_record = reports_seen_before_cleanup[0]
            self.assertEqual(first_record["supervisorFailureCode"], "onebot_timeout")
            self.assertEqual(first_record["supervisorFailureStage"], "onebot_login_rpc_wait_timeout")

            supervisor.active = active
            self.assertTrue(supervisor._stop_active(
                trigger_code="service_error",
                phase="waiting_login",
                failure_code="onebot_timeout",
                failure_stage="onebot_self_rpc_wait_timeout",
            ))
            self.assertEqual(
                read_private_json(directory / "supervisor-termination.private.json"),
                first_record,
                "a later OneBot action stage must not replace the original termination cause",
            )

            unsafe_directory = root / "unsafe-onebot-stage"
            unsafe_directory.mkdir()
            self.assertTrue(supervisor._persist_controller_termination(
                directory=unsafe_directory,
                bridge=None,
                host_generation=42,
                host_pid=None,
                host_report_path=None,
                trigger_code="service_error",
                phase="waiting_login",
                failure_code="onebot_timeout",
                failure_stage=_StageSubclass("onebot_status_rpc_wait_timeout"),
            ))
            unsafe_record = read_private_json(unsafe_directory / "supervisor-termination.private.json")
            self.assertIsNone(unsafe_record["supervisorFailureStage"])

    def test_peer_identity_stage_write_failure_keeps_cleanup_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"))
            events: list[str] = []
            bridge = FakeBridge(True)
            bridge.stop = lambda timeout=12.0: (events.append("bridge_stop") or True)
            supervisor.system.terminate_owned = lambda records: (events.append("host_cleanup") or True)
            supervisor.active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )
            with patch.object(manager_module, "atomic_private_json_if_absent", side_effect=OSError("private detail")):
                self.assertTrue(supervisor._stop_active(
                    trigger_code="service_error",
                    phase="waiting_identity",
                    failure_code="peer_identity_timeout",
                    failure_stage="cached_binding_fence_busy",
                ))
            self.assertEqual(events, ["bridge_stop", "host_cleanup"])

    def test_run_carries_peer_identity_stage_into_first_termination_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"))
            bridge = FakeBridge(True)
            supervisor.active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )

            def fail_evaluation() -> Evaluation:
                raise ServiceError(
                    "peer_identity_timeout", "waiting_identity",
                    diagnostic_stage="cached_binding_fence_busy",
                )

            supervisor._validate_static_dependencies = lambda: None
            supervisor._ensure_universal_service = lambda: None
            supervisor._recover_prior_attach_attempt = lambda: None
            supervisor._current_supervisor_identity = lambda: (700, 700000)
            supervisor._write_run_identity = lambda: None
            supervisor.evaluate = fail_evaluation

            status, exit_code = supervisor.run(once=True)

            self.assertEqual((status["phase"], status["code"], exit_code), ("waiting_identity", "peer_identity_timeout", 0))
            record = read_private_json(directory / "supervisor-termination.private.json")
            self.assertEqual(record["supervisorFailureCode"], "peer_identity_timeout")
            self.assertEqual(record["supervisorFailureStage"], "cached_binding_fence_busy")

    def test_termination_diagnostics_failure_does_not_change_cleanup_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            directory = root / "generation"
            directory.mkdir()
            launch = HostLaunch(7000, str(root), str(root / "profile"), str(directory / "host-report"))
            events: list[str] = []
            bridge = FakeBridge(True)
            bridge.stop = lambda timeout=12.0: (events.append("bridge_stop") or True)
            supervisor.system.terminate_owned = lambda records: (events.append("host_cleanup") or True)
            supervisor.active = ActiveGeneration(
                evaluation, 41, 42, directory, launch, [], 8100, "810000", "sidecar", bridge,
            )
            with patch.object(manager_module, "atomic_private_json_if_absent", side_effect=OSError("private detail")):
                self.assertTrue(supervisor._stop_active(
                    trigger_code="explicit_stop", phase="stopping",
                ))
            self.assertEqual(events, ["bridge_stop", "host_cleanup"])

    def test_start_generation_exception_captures_owned_tree_before_bridge_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            launch = HostLaunch(
                7000, str(root), str(root / "profile"), str(root / "host-report"),
                host_generation=41, launcher_creation_time_100ns=70000,
                launcher_image_path="C:\\loader\\NapCatWinBootMain.exe",
            )
            image = "C:\\QQ\\QQ.exe"
            initial = [
                {"role": "launcher", "pid": 7000, "creationTime100ns": "70000", "imagePath": launch.launcher_image_path, "profilePath": launch.profile_path},
                {"role": "host", "pid": 8100, "creationTime100ns": "810000", "imagePath": image, "profilePath": launch.profile_path},
                {"role": "host_child", "pid": 8101, "creationTime100ns": "810100", "imagePath": image, "profilePath": launch.profile_path},
            ]
            snapshots = [
                [initial[0], initial[1], {"role": "host_child", "pid": 8102, "creationTime100ns": "810200", "imagePath": image, "profilePath": launch.profile_path}],
                [initial[0], initial[1], {"role": "host_child", "pid": 8103, "creationTime100ns": "810300", "imagePath": image, "profilePath": launch.profile_path}],
            ]
            events: list[str] = []

            def refresh(_launch, _config):
                events.append("refresh")
                return snapshots.pop(0)

            supervisor.system.owned_host_processes = refresh
            supervisor.system.launch_host = lambda *_args, **_kwargs: launch
            supervisor.system.wait_host_ready = lambda *_args, **_kwargs: (initial, 8100, "sidecar", "810000")

            def terminate(records):
                events.append("terminate")
                supervisor.system.terminate_calls.append(records)
                return False

            supervisor.system.terminate_owned = terminate
            bridge = FakeBridge(True)
            bridge.stop = lambda timeout=12.0: (events.append("bridge_stop") or True)

            with (
                patch.object(supervisor, "_validate_static_dependencies"),
                patch.object(supervisor, "_startup_checkpoint"),
                patch.object(supervisor, "_make_guard_for_generation", return_value=None),
                patch.object(supervisor, "_revalidate_audio_route"),
                patch.object(supervisor, "_binding_still_current", return_value=True),
                patch.object(supervisor, "_build_audio_route_config", return_value=None),
                patch.object(supervisor, "_start_bridge_process", return_value=bridge),
                patch.object(supervisor, "_await_bridge_ready", return_value=False),
                patch.object(supervisor, "_allocate_generation", side_effect=[41, 42]),
                self.assertRaisesRegex(ServiceError, "bridge_startup_unconfirmed"),
            ):
                supervisor._start_generation(evaluation)

            self.assertEqual(events, ["refresh", "bridge_stop", "refresh", "terminate", "refresh", "terminate"])
            self.assertEqual(
                {item["pid"] for item in supervisor.system.terminate_calls[-1]},
                {7000, 8100, 8101, 8102, 8103},
            )
            self.assertTrue(supervisor._kernel_quarantined(evaluation))
            generation_directories = list((supervisor.runtime_dir / "generations").glob("connection-*"))
            self.assertEqual(len(generation_directories), 1)
            cleanup_record = read_private_json(
                generation_directories[0] / "host-cleanup-unconfirmed.private.json",
            )
            self.assertEqual(cleanup_record["hostGeneration"], 41)
            self.assertEqual(
                {item["pid"] for item in cleanup_record["ownedHosts"]},
                {7000, 8100, 8101, 8102, 8103},
            )

    def test_stop_active_quarantines_exact_kernel_when_owned_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            generation_directory = root / "generation"
            generation_directory.mkdir()
            records = [
                {"role": "host", "pid": 8100, "creationTime100ns": "810000", "imagePath": "C:\\QQ\\QQ.exe", "profilePath": "C:\\profile"},
                {"role": "host_child", "pid": 8101, "creationTime100ns": "810100", "imagePath": "C:\\QQ\\QQ.exe", "profilePath": "C:\\profile"},
                {"role": "launcher", "pid": 7000, "creationTime100ns": "70000", "imagePath": "C:\\loader\\NapCatWinBootMain.exe", "profilePath": "C:\\profile"},
            ]
            snapshots = [
                [records[2], records[0], {"role": "host_child", "pid": 8102, "creationTime100ns": "810200", "imagePath": "C:\\QQ\\QQ.exe", "profilePath": "C:\\profile"}],
                [records[2], records[0], {"role": "host_child", "pid": 8103, "creationTime100ns": "810300", "imagePath": "C:\\QQ\\QQ.exe", "profilePath": "C:\\profile"}],
            ]
            supervisor.system.owned_host_processes = lambda _launch, _config: snapshots.pop(0)
            cleanup_calls: list[list[dict]] = []

            def fail_cleanup(current_records):
                cleanup_calls.append(current_records)
                return False

            supervisor.system.terminate_owned = fail_cleanup
            active = ActiveGeneration(
                evaluation, 10, 11, generation_directory,
                HostLaunch(9, "", "", ""), records, 8100, "810000", "sidecar", FakeBridge(True),
            )
            supervisor.active = active

            self.assertFalse(supervisor._stop_active())
            self.assertTrue(supervisor._kernel_quarantined(evaluation))
            self.assertEqual(
                {item["pid"] for item in cleanup_calls[-1]},
                {7000, 8100, 8101, 8102, 8103},
            )
            cleanup_record = read_private_json(
                generation_directory / "host-cleanup-unconfirmed.private.json",
            )
            self.assertEqual(cleanup_record["hostGeneration"], 10)
            self.assertEqual(
                {item["pid"] for item in cleanup_record["ownedHosts"]},
                {7000, 8100, 8101, 8102, 8103},
            )

    def test_startup_checkpoint_cancels_on_matching_scoped_stop_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            supervisor._ensure_private_runtime()
            supervisor.run_identity = supervisor._manager_identity(42)
            manager = supervisor.run_identity
            atomic_private_json(supervisor._stop_request_path(), {
                "schema": manager["schema"], "requestId": str(uuid.uuid4()),
                "pid": manager["pid"], "creationTime100ns": manager["creationTime100ns"],
                "runGeneration": manager["runGeneration"], "requestedAtMs": 1_700_000_000_000,
            })
            with self.assertRaisesRegex(ServiceError, "service_stop_requested"):
                supervisor._startup_checkpoint("bridge_callback_wait")

    def test_disabled_guard_validation_never_enumerates_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary), enabled=False)

            def forbidden_inventory():
                raise AssertionError("disabled audio must not enumerate endpoints")

            self.assertIsNone(manager_module._validate_guard(config, forbidden_inventory))

    def test_endpoint_replacement_immediately_before_host_launch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stable = _endpoint_inventory()
            replaced = copy.deepcopy(stable)
            replaced[2]["id"] = "new-playback-endpoint-id"
            inventories = iter((stable, replaced))
            supervisor = self._supervisor(root, endpoint_inventory=lambda: next(inventories))
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            atomic_private_json(supervisor.audio_plan_path, {"schema": 1})
            atomic_private_json(supervisor.runtime_dir / "adapter-runtime.private.json", {"schema": 1})
            with patch.object(supervisor, "_validate_static_dependencies"):
                with self.assertRaisesRegex(ServiceError, "audio_route_changed"):
                    supervisor._start_generation(evaluation)
            self.assertEqual(supervisor.system.launch_count, 0)
            self.assertFalse(supervisor.audio_plan_path.exists())
            self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())

    def test_evaluation_fingerprint_changes_when_same_name_render_endpoint_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            original = supervisor.evaluate()
            replaced = copy.deepcopy(_endpoint_inventory())
            replaced[2]["id"] = "replacement-with-the-same-name"
            changed_guard_source = manager_module._validate_guard(
                supervisor.config,
                endpoint_inventory=lambda: replaced,
            )
            changed = Evaluation(
                original.manifest,
                original.binding,
                original.identity,
                original.identity_source,
                original.full_identity_path,
                original.peer_map,
                changed_guard_source,
            )
            self.assertNotEqual(original.fingerprint, changed.fingerprint)

    def test_audio_plan_is_published_with_exact_current_peer_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            directory = supervisor.runtime_dir / "generations" / "connection-22"
            directory.mkdir(parents=True)
            active = ActiveGeneration(
                evaluation, 21, 22, directory, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", FakeBridge(True),
                service_instance="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
            )
            supervisor._write_generation_identity(active)

            plan = read_private_json(supervisor.audio_plan_path)
            self.assertEqual(set(plan), {
                "schema", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
                "kernelPid", "kernelCreationTime100ns", "captureEndpointId", "renderEndpointId", "audio",
            })
            self.assertEqual(plan["schema"], 1)
            self.assertEqual(plan["serviceInstance"], active.service_instance)
            self.assertEqual(plan["hostGeneration"], active.host_generation)
            self.assertEqual(plan["connectionGeneration"], active.connection_generation)
            self.assertEqual(plan["kernelPid"], evaluation.kernel_pid)
            self.assertEqual(plan["kernelCreationTime100ns"], str(evaluation.kernel_filetime))
            self.assertEqual(set(plan["audio"]), {
                "enabled", "capture_device", "playback_device", "capture_mode", "device_scope",
            })
            self.assertEqual(plan["audio"], {
                "enabled": True,
                "capture_device": "Capture (VB-Audio Virtual Cable) [loopback]",
                "playback_device": "Playback (VB-Audio Virtual Cable)",
                "capture_mode": "loopback",
                "device_scope": {
                    "capture_render_name": "Capture (VB-Audio Virtual Cable)",
                    "playback_render_name": "Playback (VB-Audio Virtual Cable)",
                },
            })
            peer_snapshot = read_private_json(supervisor.runtime_dir / "adapter-runtime.private.json")
            self.assertEqual(peer_snapshot["serviceInstance"], plan["serviceInstance"])
            self.assertEqual(peer_snapshot["hostGeneration"], plan["hostGeneration"])
            self.assertEqual(peer_snapshot["connectionGeneration"], plan["connectionGeneration"])
            self.assertTrue(active.peer_snapshot_written)

    def test_stale_route_before_plan_publication_clears_plan_and_peer_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stable = _endpoint_inventory()
            replaced = copy.deepcopy(stable)
            replaced[2]["id"] = "replacement-with-the-same-friendly-name"
            inventories = iter((stable, stable, replaced))
            supervisor = self._supervisor(root, endpoint_inventory=lambda: next(inventories))
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            directory = supervisor.runtime_dir / "generations" / "connection-22"
            directory.mkdir(parents=True)
            active = ActiveGeneration(
                evaluation, 21, 22, directory, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", FakeBridge(True),
                service_instance="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
            )
            with self.assertRaisesRegex(ServiceError, "audio_route_changed"):
                supervisor._write_generation_identity(active)
            self.assertFalse(supervisor.audio_plan_path.exists())
            self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())

    def test_stop_active_clears_published_audio_and_peer_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            directory = supervisor.runtime_dir / "generations" / "connection-22"
            directory.mkdir(parents=True)
            active = ActiveGeneration(
                evaluation, 21, 22, directory, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", FakeBridge(True),
                service_instance="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
            )
            supervisor.active = active
            atomic_private_json(supervisor.audio_plan_path, {"schema": 1})
            atomic_private_json(supervisor.runtime_dir / "adapter-runtime.private.json", {"schema": 1})

            self.assertTrue(supervisor._stop_active())
            self.assertFalse(supervisor.audio_plan_path.exists())
            self.assertFalse((supervisor.runtime_dir / "adapter-runtime.private.json").exists())

    def test_offline_onebot_is_a_waiting_login_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root, rpc=FakeRpc(online=False))
            status = supervisor.plan()
            self.assertEqual(status["phase"], "waiting_login")
            self.assertFalse(status["ready"])

    def test_generic_onebot_good_false_keeps_legacy_offline_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = FakeRpc()
            rpc.status_data = {"online": True, "good": False}
            supervisor = self._supervisor(root, rpc=rpc)

            with self.assertRaises(ServiceError) as caught:
                supervisor._onebot_identity()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("onebot_offline", "waiting_login"))
            self.assertEqual(rpc.calls, [("get_status", {})])

    def test_snow_health_false_does_not_stop_or_replace_consumed_active_generation(self) -> None:
        class FakeLock:
            def __init__(self, _path: Path):
                pass

            def acquire(self) -> bool:
                return True

            def release(self) -> None:
                return None

        scenarios = (
            ("recovers", [{"online": True, "good": False}, {"online": True, "good": True}]),
            ("remains_degraded", [{"online": True, "good": False}]),
        )
        for label, status_sequence in scenarios:
            with self.subTest(scenario=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = _config(root, adapter="snowluma")
                system = FakeSystem(manifest={
                    "schema": 1,
                    "adapter": "snowluma",
                    "accountId": "12345",
                    "connected": True,
                    "adapterPid": 8095,
                    "adapterCreationTime100ns": "809500",
                    "connectionGeneration": "a1b2c3d4",
                    "updatedAtMs": 1_700_000_000_000,
                })
                rpc = FakeRpc()
                rpc.status_data = {"online": True, "good": True}
                supervisor = self._supervisor(root, config=config, system=system, rpc=rpc)
                evaluation = supervisor.evaluate()

                # The existing generation was attached to this exact Kernel incarnation.
                supervisor._ensure_private_runtime()
                supervisor._quarantine_kernel(evaluation, "shim_consumed")
                generation_dir = supervisor.runtime_dir / "generations" / "connection-41"
                generation_dir.mkdir(parents=True)
                bridge = FakeBridge(True)
                cleanup_events: list[str] = []
                bridge.stop = lambda timeout=12.0, events=cleanup_events: (events.append("bridge_stop") or True)
                system.terminate_owned = lambda _records, events=cleanup_events: (events.append("host_cleanup") or True)
                active = ActiveGeneration(
                    evaluation,
                    40,
                    41,
                    generation_dir,
                    HostLaunch(7000, str(root), str(root / "profile"), str(root / "report")),
                    [],
                    8100,
                    "810000",
                    "sidecar",
                    bridge,
                    shim_consumed=True,
                    account_path_verified=True,
                    login_ready=True,
                )
                supervisor.active = active

                rpc.status_data = copy.deepcopy(status_sequence[0])
                observed_statuses: list[dict[str, Any]] = []
                status_reads = [0]

                def advance_status(
                    action: str,
                    _params: dict,
                    *,
                    current_rpc: FakeRpc = rpc,
                    observed: list[dict[str, Any]] = observed_statuses,
                    sequence: list[dict[str, Any]] = status_sequence,
                    reads: list[int] = status_reads,
                ) -> None:
                    if action != "get_status":
                        return
                    reads[0] += 1
                    observed.append(copy.deepcopy(current_rpc.status_data or {}))
                    next_index = min(reads[0], len(sequence) - 1)
                    current_rpc.status_data = copy.deepcopy(sequence[next_index])

                rpc.after_call = advance_status
                monitored_active_ids: list[int] = []
                original_monitor = supervisor._monitor_active

                def record_monitor(
                    current: ActiveGeneration,
                    active_ids: list[int] = monitored_active_ids,
                    monitor=original_monitor,
                ) -> str:
                    active_ids.append(id(current))
                    return monitor(current)

                supervisor._monitor_active = record_monitor
                starts: list[tuple[Any, ...]] = []

                def unexpected_start(
                    current: Evaluation,
                    recorded: list[tuple[Any, ...]] = starts,
                ) -> ActiveGeneration:
                    recorded.append(current.fingerprint)
                    raise AssertionError("the active SnowLuma generation must not be replaced")

                supervisor._start_generation = unexpected_start
                status_history: list[tuple[str, str]] = []
                original_status = supervisor._status

                def capture_status(
                    phase: str,
                    code: str,
                    *,
                    persist: bool = True,
                    status_call=original_status,
                    history: list[tuple[str, str]] = status_history,
                ) -> dict[str, Any]:
                    result = status_call(phase, code, persist=persist)
                    history.append((phase, code))
                    return result

                supervisor._status = capture_status
                stop_checks = [0]

                def request_stop_after_two_ticks(checks: list[int] = stop_checks) -> bool:
                    checks[0] += 1
                    return checks[0] > 3

                supervisor._consume_stop_request = request_stop_after_two_ticks

                with (
                    patch.object(supervisor, "_validate_static_dependencies"),
                    patch.object(manager_module, "ProcessLock", FakeLock),
                ):
                    final_status, exit_code = supervisor.run()

                self.assertEqual(exit_code, 0)
                self.assertEqual((final_status["phase"], final_status["code"]), ("stopped", "service_stopped"))
                expected_observed = [status_sequence[min(index, len(status_sequence) - 1)] for index in range(2)]
                self.assertEqual(observed_statuses, expected_observed)
                self.assertEqual(monitored_active_ids, [id(active), id(active)])
                self.assertEqual(starts, [])
                self.assertEqual(cleanup_events, ["bridge_stop", "host_cleanup"])
                self.assertNotIn(("waiting_login", "onebot_offline"), status_history)
                termination = read_private_json(generation_dir / "supervisor-termination.private.json")
                self.assertEqual(termination["triggerCode"], "explicit_stop")
                self.assertEqual(termination["phase"], "stopping")

    def test_stale_adapter_heartbeat_fails_closed_before_host_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            system.manifest["updatedAtMs"] = 1_699_999_980_000
            supervisor = self._supervisor(root, system=system)
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual(caught.exception.code, "adapter_manifest_stale")
            self.assertEqual(caught.exception.phase, "waiting_adapter")
            self.assertEqual(system.launch_count, 0)

    def test_plan_uses_read_only_rpc_and_never_launches_or_stops_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            rpc = FakeRpc()
            supervisor = self._supervisor(root, system=system, rpc=rpc)
            status = supervisor.plan()
            self.assertEqual(status["phase"], "needs_attachment")
            self.assertEqual(system.launch_count, 0)
            self.assertEqual(system.terminate_calls, [])
            self.assertEqual(
                [action for action, _ in rpc.calls],
                ["get_status", "get_login_info", "get_stranger_info", "get_friend_list", "get_login_info", "get_stranger_info"],
            )
            self.assertFalse((root / ".runtime").exists())

    def test_run_keeps_consumed_kernel_active_until_ready_then_restart_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            evaluation = supervisor.evaluate()
            generation_dir = Path(supervisor.config["runtime_dir"]) / "generations" / "connection-22"
            generation_dir.mkdir(parents=True)
            bridge = FakeBridge(True)
            bridge.events = [
                {"kind": "agent_event", "event": {
                    "kind": "bootstrap_status", "processPid": evaluation.kernel_pid,
                    "connectionGeneration": 22, "sessionFound": False, "serviceAvailable": False,
                }},
                {"kind": "auto_login_identity_verified", "result": {"accountPathVerified": True}},
                {"kind": "auto_login_ready", "result": {"posted": False, "loginState": "ready"}},
            ]
            active = ActiveGeneration(
                evaluation, 21, 22, generation_dir, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", bridge,
            )
            starts: list[int] = []
            supervisor._start_generation = lambda current: (starts.append(current.kernel_pid) or active)
            stop_checks = 0

            def consume_stop() -> bool:
                nonlocal stop_checks
                stop_checks += 1
                return stop_checks > 3

            supervisor._consume_stop_request = consume_stop
            history: list[dict] = []
            original_status = supervisor._status

            def capture_status(phase: str, code: str, *, persist: bool = True):
                value = original_status(phase, code, persist=persist)
                history.append(dict(value))
                return value

            supervisor._status = capture_status

            class FakeLock:
                def __init__(self, _path: Path):
                    pass

                def acquire(self) -> bool:
                    return True

                def release(self) -> None:
                    return None

            with patch.object(supervisor, "_validate_static_dependencies"), patch.object(manager_module, "ProcessLock", FakeLock):
                final, code = supervisor.run()
            self.assertEqual(code, 0)
            self.assertEqual(final["phase"], "stopped")
            self.assertEqual(starts, [evaluation.kernel_pid])
            self.assertEqual(system.port_checks, [6111, 6110, 6120])
            ready = [item for item in history if item["phase"] == "ready"]
            self.assertGreaterEqual(len(ready), 2)
            self.assertTrue(all(item["ready"] and "code" not in item for item in ready))
            self.assertEqual(bridge.events, [])
            self.assertTrue(supervisor._kernel_quarantined(evaluation))

            restarted = self._supervisor(root, system=system)
            with patch.object(restarted, "_validate_static_dependencies"), patch.object(manager_module, "ProcessLock", FakeLock), patch.object(
                restarted, "_start_generation", side_effect=AssertionError("quarantined Kernel must not get a new controller"),
            ):
                restart_status, restart_code = restarted.run(once=True)
            self.assertEqual(restart_code, 0)
            self.assertEqual(restart_status["phase"], "waiting_kernel_restart")

    def test_early_controller_exit_with_unconfirmed_detach_quarantines_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            evaluation = supervisor.evaluate()
            generation_dir = supervisor.runtime_dir / "generations" / "connection-22"
            bridge = FakeBridge(False)
            bridge.process.poll = lambda: 1
            active = ActiveGeneration(
                evaluation, 21, 22, generation_dir, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", bridge,
            )
            starts: list[int] = []
            supervisor._start_generation = lambda current: (starts.append(current.kernel_pid) or active)

            class FakeLock:
                def __init__(self, _path: Path):
                    pass

                def acquire(self) -> bool:
                    return True

                def release(self) -> None:
                    return None

            with patch.object(supervisor, "_validate_static_dependencies"), patch.object(manager_module, "ProcessLock", FakeLock):
                status, code = supervisor.run(once=True)
            self.assertEqual(code, 0)
            self.assertEqual(status["phase"], "waiting_kernel_restart")
            self.assertEqual(starts, [evaluation.kernel_pid])
            self.assertTrue(supervisor._kernel_quarantined(evaluation))

    def test_confirmed_controller_exit_counts_toward_generation_failure_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            config = _config(root)
            config["supervision"]["max_failures_per_generation"] = 3
            supervisor = self._supervisor(root, config=config, system=system)
            evaluation = supervisor.evaluate()
            starts: list[int] = []
            history: list[dict] = []
            original_status = supervisor._status

            def capture_status(phase: str, code: str, *, persist: bool = True):
                value = original_status(phase, code, persist=persist)
                history.append(dict(value))
                return value

            supervisor._status = capture_status

            def start_generation(current: Evaluation) -> ActiveGeneration:
                starts.append(current.kernel_pid)
                bridge = FakeBridge(True)  # explicitly confirmed clean detach on stop
                bridge.process.poll = lambda: 1
                return ActiveGeneration(
                    current, 21, 22, supervisor.runtime_dir / "generations" / f"connection-{len(starts)}",
                    HostLaunch(9, "", "", ""), [], 8100, "810000", "host-sidecar", bridge,
                )

            supervisor._start_generation = start_generation
            supervisor._wait_before_retry = lambda *_args: True
            supervisor._consume_stop_request = lambda: supervisor.last_status.get("code") == "generation_failure_limit"

            class FakeLock:
                def __init__(self, _path: Path):
                    pass

                def acquire(self) -> bool:
                    return True

                def release(self) -> None:
                    return None

            with patch.object(supervisor, "_validate_static_dependencies"), patch.object(manager_module, "ProcessLock", FakeLock):
                final, code = supervisor.run()
            self.assertEqual(code, 0)
            self.assertEqual(final["phase"], "stopped")
            self.assertEqual(starts, [evaluation.kernel_pid] * 3)
            self.assertTrue(any(item.get("code") == "generation_failure_limit" for item in history))
            self.assertFalse(supervisor._kernel_quarantined(evaluation))

    def test_long_retry_backoff_checks_scoped_stop_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            clock = [0.0]
            sleeps: list[float] = []
            checks = [0]

            def sleeper(seconds: float) -> None:
                sleeps.append(seconds)
                clock[0] += seconds

            def consume_stop() -> bool:
                checks[0] += 1
                return checks[0] == 2

            supervisor.sleeper = sleeper
            supervisor._consume_stop_request = consume_stop
            with patch.object(manager_module.time, "monotonic", side_effect=lambda: clock[0]):
                should_continue = supervisor._wait_before_retry(
                    30.0, supervisor._result_status("waiting_target", "controller_exited"), 2.0,
                )
            self.assertFalse(should_continue)
            self.assertEqual(sleeps, [2.0])

    def test_explicit_peer_map_drives_verified_onebot_lookup_without_policy_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, peer_map={PEER_UID: "10002"})
            rpc = FakeRpc(include_peer_uid=False)
            supervisor = self._supervisor(root, config=config, rpc=rpc)
            evaluation = supervisor.evaluate()
            self.assertEqual(evaluation.peer_map, {PEER_UID: "10002"})
            stranger_queries = [params["user_id"] for action, params in rpc.calls if action == "get_stranger_info"]
            self.assertEqual(stranger_queries, ["12345", "10002"])
            self.assertNotIn("get_friend_list", [action for action, _ in rpc.calls])

    def test_empty_peer_map_discovers_full_verified_friend_identity_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            friends = [{"user_id": str(20_000 + index), "nickname": f"ignored-{index}"} for index in range(32)]
            friend_uids = {str(20_000 + index): f"u_{index:022d}" for index in range(32)}
            config = _config(root)
            rpc = FakeRpc(friends=friends, friend_uids=friend_uids)
            clock = [0.0]
            clock_lock = threading.Lock()

            def advance_fake_clock(_action, _params) -> None:
                with clock_lock:
                    clock[0] += 0.1

            rpc.after_call = advance_fake_clock
            system = FakeSystem()
            system.revalidate_hook = lambda: clock.__setitem__(0, clock[0] + 5.37)
            supervisor = self._supervisor(
                root, config=config, system=system, rpc=rpc,
                monotonic_clock=lambda: clock[0], identity_discovery_timeout_seconds=15.0,
            )

            evaluation = supervisor.evaluate()

            expected = {uid: uin for uin, uid in friend_uids.items()}
            self.assertEqual(evaluation.peer_map, expected)
            self.assertLess(clock[0], 15.0)
            self.assertEqual(config["peer_map"], {})
            self.assertEqual(sum(action == "get_friend_list" for action, _ in rpc.calls), 1)
            cache_key = next(iter(supervisor._resolved_peers))
            self.assertEqual(cache_key[:9], (
                "napcat", "12345", 8095, 809500, "a1b2c3d4",
                51200, 512000, os.path.normcase(os.path.realpath("C:\\QQ\\Current")).rstrip("\\/"), SELF_UID,
            ))
            stranger_queries = [params["user_id"] for action, params in rpc.calls if action == "get_stranger_info"]
            self.assertEqual(stranger_queries[0], "12345")
            self.assertEqual(stranger_queries[-1], "12345")
            self.assertCountEqual(stranger_queries[1:-1], [str(20_000 + index) for index in range(32)])

            supervisor._ensure_private_runtime()
            generation_dir = supervisor.runtime_dir / "generations" / "connection-22"
            generation_dir.mkdir(parents=True)
            active = ActiveGeneration(
                evaluation, 21, 22, generation_dir, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", FakeBridge(True),
                service_instance=str(uuid.uuid4()),
            )
            supervisor._write_generation_identity(active)
            private_peer_file = read_private_json(generation_dir / "kernel-peer-identities.private.json")
            self.assertEqual(private_peer_file["peerUidsByUin"], friend_uids)
            self.assertTrue(active.peer_snapshot_written)
            evaluation.peer_map.clear()
            supervisor._write_generation_identity(active)
            self.assertEqual(
                read_private_json(generation_dir / "kernel-peer-identities.private.json")["peerUidsByUin"],
                friend_uids,
            )

    def test_friend_discovery_skips_single_verified_self_row(self) -> None:
        peer_uins = [str(20_100 + index) for index in range(31)]
        friend_uids = {uin: f"u_{index + 1:022d}" for index, uin in enumerate(peer_uins)}
        friends = [{"user_id": "12345", "nickname": "ignored-self-name"}]
        friends.extend({"user_id": uin, "nickname": f"ignored-peer-{index}"} for index, uin in enumerate(peer_uins))
        rpc = FakeRpc(friends=friends, friend_uids=friend_uids)
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary), rpc=rpc)

            evaluation = supervisor.evaluate()

            expected = {uid: uin for uin, uid in friend_uids.items()}
            self.assertEqual(len(friends), 32)
            self.assertEqual(evaluation.peer_map, expected)
            self.assertNotIn(SELF_UID, evaluation.peer_map)
            self.assertNotIn("12345", evaluation.peer_map.values())
            self.assertEqual(supervisor._resolved_peers[next(iter(supervisor._resolved_peers))], expected)
            stranger_queries = [params["user_id"] for action, params in rpc.calls if action == "get_stranger_info"]
            self.assertEqual(stranger_queries[0], "12345")
            self.assertEqual(stranger_queries[-1], "12345")
            self.assertCountEqual(stranger_queries[1:-1], peer_uins)

    def test_delayed_friend_resolution_uses_four_workers_and_finishes_under_deadline(self) -> None:
        friends = [{"user_id": str(40_000 + index)} for index in range(32)]
        friend_uins = {str(40_000 + index) for index in range(32)}
        friend_uids = {uin: f"u_{index:022d}" for index, uin in enumerate(sorted(friend_uins))}

        class DelayedRpc(FakeRpc):
            def __init__(self):
                super().__init__(friends=friends, friend_uids=friend_uids)
                self.peer_lock = threading.Lock()
                self.active_peer_rpcs = 0
                self.max_peer_rpcs = 0

            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                user = str((params or {}).get("user_id", ""))
                if action != "get_stranger_info" or user not in friend_uins:
                    return super().call(action, params, timeout=timeout)
                with self.peer_lock:
                    self.active_peer_rpcs += 1
                    self.max_peer_rpcs = max(self.max_peer_rpcs, self.active_peer_rpcs)
                try:
                    time.sleep(0.08)
                    return super().call(action, params, timeout=timeout)
                finally:
                    with self.peer_lock:
                        self.active_peer_rpcs -= 1

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = DelayedRpc()
            supervisor = self._supervisor(root, rpc=rpc, identity_discovery_timeout_seconds=15.0)
            started = time.monotonic()

            evaluation = supervisor.evaluate()

            elapsed = time.monotonic() - started
            self.assertEqual(evaluation.peer_map, {uid: uin for uin, uid in friend_uids.items()})
            self.assertLess(elapsed, 15.0)
            self.assertEqual(rpc.max_peer_rpcs, 4)
            self.assertEqual(rpc.active_peer_rpcs, 0)
            self.assertEqual(len(supervisor._resolved_peers), 1)

    def test_timeout_retries_do_not_accumulate_peer_rpc_workers(self) -> None:
        friends = [{"user_id": str(50_000 + index)} for index in range(8)]
        friend_uins = {str(50_000 + index) for index in range(8)}
        friend_uids = {uin: f"u_{index:022d}" for index, uin in enumerate(sorted(friend_uins))}
        release = threading.Event()
        first_wave_started = threading.Event()
        all_idle = threading.Event()
        all_idle.set()

        class GatedRpc(FakeRpc):
            def __init__(self):
                super().__init__(friends=friends, friend_uids=friend_uids)
                self.peer_lock = threading.Lock()
                self.active_peer_rpcs = 0
                self.max_peer_rpcs = 0
                self.started_peer_uins: list[str] = []

            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                user = str((params or {}).get("user_id", ""))
                if action != "get_stranger_info" or user not in friend_uins:
                    return super().call(action, params, timeout=timeout)
                with self.peer_lock:
                    self.active_peer_rpcs += 1
                    self.max_peer_rpcs = max(self.max_peer_rpcs, self.active_peer_rpcs)
                    self.started_peer_uins.append(user)
                    all_idle.clear()
                    if self.active_peer_rpcs == 4:
                        first_wave_started.set()
                try:
                    # Model an RPC that returns late despite its timeout so
                    # repeated evaluations must retain ownership of its slot.
                    release.wait(2.0)
                    return super().call(action, params, timeout=timeout)
                finally:
                    with self.peer_lock:
                        self.active_peer_rpcs -= 1
                        if self.active_peer_rpcs == 0:
                            all_idle.set()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = GatedRpc()
            supervisor = self._supervisor(root, rpc=rpc, identity_discovery_timeout_seconds=0.15)
            try:
                with self.assertRaises(ServiceError) as first:
                    supervisor.evaluate()
                self.assertEqual(first.exception.code, "peer_identity_timeout")
                self.assertEqual(first.exception._diagnostic_stage, "peer_identity_rpc_wait_timeout")
                self.assertTrue(first_wave_started.wait(1.0))
                self.assertEqual(len(rpc.started_peer_uins), 4)
                self.assertEqual(rpc.max_peer_rpcs, 4)
                self.assertFalse(supervisor._resolved_peers)

                for _ in range(2):
                    with self.assertRaises(ServiceError) as retry:
                        supervisor.evaluate()
                    self.assertEqual(retry.exception.code, "peer_identity_timeout")
                self.assertEqual(len(rpc.started_peer_uins), 4)
                self.assertEqual(rpc.max_peer_rpcs, 4)
                self.assertFalse(supervisor._resolved_peers)
            finally:
                release.set()
                self.assertTrue(all_idle.wait(2.0))

            drain_deadline = time.monotonic() + 1.0
            while time.monotonic() < drain_deadline:
                with supervisor._peer_rpc_futures_lock:
                    if not supervisor._peer_rpc_futures:
                        break
                time.sleep(0.005)
            with supervisor._peer_rpc_futures_lock:
                self.assertFalse(supervisor._peer_rpc_futures)
            self.assertFalse(supervisor._resolved_peers, "late timed-out RPCs must not publish the peer cache")

            supervisor.identity_discovery_timeout_seconds = 15.0
            evaluation = supervisor.evaluate()
            self.assertEqual(evaluation.peer_map, {uid: uin for uin, uid in friend_uids.items()})
            self.assertEqual(len(supervisor._resolved_peers), 1)
            self.assertEqual(rpc.max_peer_rpcs, 4)

    def test_real_http_transport_workers_remain_bounded_across_discovery_retries(self) -> None:
        friends = [{"user_id": str(60_000 + index)} for index in range(4)]
        peer_uids = {str(60_000 + index): f"u_{chr(99 + index) * 22}" for index in range(4)}
        release_headers = threading.Event()
        first_wave_started = threading.Event()
        counts_lock = threading.Lock()
        active_slow_handlers = 0
        max_slow_handlers = 0
        total_slow_requests = 0

        class SlowHeadersHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                nonlocal active_slow_handlers, max_slow_handlers, total_slow_requests
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                action = self.path.rstrip("/").rsplit("/", 1)[-1]
                params = request
                data: dict[str, Any] | list[dict[str, Any]]
                slow_peer = False
                if action == "get_status":
                    data = {"online": True}
                elif action == "get_login_info":
                    data = {"user_id": "12345"}
                elif action == "get_friend_list":
                    data = friends
                elif action == "get_stranger_info":
                    user = str(params.get("user_id", ""))
                    if user == "12345":
                        data = {"user_id": user, "uid": SELF_UID}
                    else:
                        if not release_headers.is_set():
                            slow_peer = True
                            with counts_lock:
                                active_slow_handlers += 1
                                total_slow_requests += 1
                                max_slow_handlers = max(max_slow_handlers, active_slow_handlers)
                                if active_slow_handlers == 4:
                                    first_wave_started.set()
                        data = {"user_id": user, "uid": peer_uids[user]}
                else:
                    self.send_error(404)
                    return
                body = json.dumps({"status": "ok", "retcode": 0, "data": data}).encode("utf-8")
                try:
                    if slow_peer:
                        response_headers = (
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                        for index, byte in enumerate(response_headers):
                            if release_headers.wait(0.05):
                                self.wfile.write(response_headers[index:])
                                break
                            self.wfile.write(bytes((byte,)))
                            self.wfile.flush()
                        self.wfile.write(body)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                except OSError:
                    return
                finally:
                    if slow_peer:
                        with counts_lock:
                            active_slow_handlers -= 1

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeadersHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        created_clients: list[OneBotClient] = []
        timeout_seconds = [0.15]

        def real_rpc_factory(url: str, token: str | None) -> OneBotClient:
            client = OneBotClient(url, token, timeout=timeout_seconds[0])
            created_clients.append(client)
            return client

        def live_http_workers() -> int:
            return sum(
                thread.is_alive() and thread.name == "qqcall-onebot-http"
                for thread in threading.enumerate()
            )

        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = _config(root)
                config["onebot"]["url"] = f"http://127.0.0.1:{server.server_address[1]}/"
                supervisor = self._supervisor(
                    root,
                    config=config,
                    identity_discovery_timeout_seconds=0.5,
                )
                supervisor.rpc_factory = real_rpc_factory

                with self.assertRaises(ServiceError) as first:
                    supervisor.evaluate()
                self.assertEqual(first.exception.code, "peer_identity_timeout")
                self.assertTrue(first_wave_started.wait(1.0))
                self.assertEqual(live_http_workers(), 4)

                for _ in range(2):
                    with self.assertRaises(ServiceError) as retry:
                        supervisor.evaluate()
                    self.assertEqual(retry.exception.code, "onebot_timeout")
                    self.assertEqual(live_http_workers(), 4)
                self.assertEqual(total_slow_requests, 4)
                self.assertLessEqual(max_slow_handlers, 4)
                self.assertEqual(len(created_clients), 3)
                self.assertTrue(all(
                    client._http_transport_slots is supervisor._onebot_http_slots
                    for client in created_clients
                ))
                self.assertFalse(supervisor._resolved_peers)

                release_headers.set()
                drain_deadline = time.monotonic() + 2.0
                while live_http_workers() and time.monotonic() < drain_deadline:
                    time.sleep(0.005)
                self.assertEqual(live_http_workers(), 0)

                timeout_seconds[0] = 0.5
                supervisor.identity_discovery_timeout_seconds = 3.0
                evaluation = supervisor.evaluate()
                self.assertEqual(evaluation.peer_map, {uid: uin for uin, uid in peer_uids.items()})
                self.assertEqual(len(supervisor._resolved_peers), 1)
        finally:
            release_headers.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)

    def test_friend_discovery_accepts_64_and_rejects_65_without_truncation(self) -> None:
        for count in (64, 65):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                friends = [{"user_id": str(30_000 + index)} for index in range(count)]
                friend_uids = {str(30_000 + index): f"u_{index:022d}" for index in range(count)}
                rpc = FakeRpc(friends=friends, friend_uids=friend_uids)
                supervisor = self._supervisor(root, rpc=rpc)

                if count == 64:
                    evaluation = supervisor.evaluate()
                    self.assertEqual(len(evaluation.peer_map), 64)
                    self.assertEqual(
                        len([1 for action, _ in rpc.calls if action == "get_stranger_info"]) - 2,
                        64,
                    )
                else:
                    with self.assertRaises(ServiceError) as caught:
                        supervisor.evaluate()
                    self.assertEqual((caught.exception.code, caught.exception.phase), ("friend_list_too_large", "waiting_identity"))
                    self.assertEqual(len([1 for action, _ in rpc.calls if action == "get_stranger_info"]), 1)
                    self.assertFalse(supervisor._resolved_peers)

    def test_discovery_failure_missing_uid_and_identity_conflicts_never_cache_partial_maps(self) -> None:
        cases = (
            (FakeRpc(fail_friend_list=True), "friend_list_unavailable", "waiting_identity"),
            (FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": None}), "friend_uid_unavailable", "waiting_identity"),
            (FakeRpc(friends=[{"user_id": "20001"}], fail_peer_uins={"20001"}), "peer_identity_unavailable", "waiting_identity"),
            (FakeRpc(friends=[{"user_id": "20001"}, {"user_id": "20001"}], friend_uids={"20001": PEER_UID}), "friend_identity_ambiguous", "quarantined"),
            (FakeRpc(friends=[{"user_id": "12345"}, {"user_id": "12345"}], friend_uids={"12345": SELF_UID}), "friend_identity_ambiguous", "quarantined"),
            (FakeRpc(friends=[{"user_id": "20001"}, {"user_id": "20002"}], friend_uids={"20001": PEER_UID, "20002": PEER_UID}), "friend_identity_ambiguous", "quarantined"),
            (FakeRpc(friends=[{"user_id": "20001", "self_id": "20002"}], friend_uids={"20001": PEER_UID}), "peer_identity_conflict", "quarantined"),
            (FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID}, friend_response_fields={"20001": {"user_id": "20002"}}), "friend_identity_conflict", "quarantined"),
            (FakeRpc(friends=[{"user_id": "invalid"}]), "friend_list_invalid", "waiting_identity"),
        )
        for rpc, expected_code, expected_phase in cases:
            with self.subTest(code=expected_code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                supervisor = self._supervisor(root, rpc=rpc)
                with self.assertRaises(ServiceError) as caught:
                    supervisor.evaluate()
                self.assertEqual((caught.exception.code, caught.exception.phase), (expected_code, expected_phase))
                self.assertFalse(supervisor._resolved_peers)

    def test_invalid_friend_list_row_is_not_omitted_or_looked_up(self) -> None:
        rpc = FakeRpc(
            friends=[{"user_id": "20001"}, {"user_id": "not-a-uin"}],
            friend_uids={"20001": PEER_UID},
        )
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary), rpc=rpc)

            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("friend_list_invalid", "waiting_identity"))
            stranger_queries = [
                params["user_id"] for action, params in rpc.calls if action == "get_stranger_info"
            ]
            self.assertEqual(stranger_queries, ["12345"], "invalid snapshots fail before per-peer lookups")
            self.assertFalse(supervisor._resolved_peers)

    def test_concurrent_friend_errors_keep_friend_list_order_precedence(self) -> None:
        class OutOfOrderFailureRpc(FakeRpc):
            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                if action == "get_stranger_info" and (params or {}).get("user_id") == "20001":
                    time.sleep(0.05)
                return super().call(action, params, timeout=timeout)

        rpc = OutOfOrderFailureRpc(
            friends=[{"user_id": "20001"}, {"user_id": "20002"}],
            friend_uids={"20001": None, "20002": PEER_UID},
            friend_response_fields={"20002": {"user_id": "20003"}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary), rpc=rpc)

            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("friend_uid_unavailable", "waiting_identity"))
            self.assertFalse(supervisor._resolved_peers)

    def test_discovery_cache_is_bound_to_account_uid_adapter_and_kernel_generation(self) -> None:
        class ChangeIdentityAfterLookup(FakeRpc):
            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                result = super().call(action, params, timeout=timeout)
                if action == "get_stranger_info" and params and params.get("user_id") == "20001":
                    self.account_uin = "99999"
                    self.self_uid = "u_" + ("c" * 22)
                return result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = ChangeIdentityAfterLookup(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
            supervisor = self._supervisor(root, rpc=rpc)
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual(caught.exception.code, "onebot_account_mismatch")
            self.assertFalse(supervisor._resolved_peers)

        for changed_target in ("connection", "kernel"):
            with self.subTest(target=changed_target), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                system = FakeSystem()
                class ChangeTargetAfterList(FakeRpc):
                    def __init__(self, *args, target_system: FakeSystem, target_mode: str, **kwargs):
                        self.target_system = target_system
                        self.target_mode = target_mode
                        super().__init__(*args, **kwargs)

                    def get_friend_list(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
                        result = super().get_friend_list(timeout=timeout)
                        if self.target_mode == "connection":
                            self.target_system.manifest = {
                                **self.target_system.manifest,
                                "connectionGeneration": "new-generation",
                            }
                        else:
                            current = self.target_system.binding
                            self.target_system.binding = TargetBinding(
                                current.adapter_pid, current.adapter_creation_time_100ns,
                                current.kernel_pid + 1, current.kernel_creation_time_100ns + 1,
                                current.app_path,
                            )
                        return result

                rpc = ChangeTargetAfterList(
                    target_system=system,
                    target_mode=changed_target,
                    friends=[{"user_id": "20001"}],
                    friend_uids={"20001": PEER_UID},
                )
                supervisor = self._supervisor(root, system=system, rpc=rpc)
                with self.assertRaises(ServiceError) as caught:
                    supervisor.evaluate()
                self.assertEqual(caught.exception.code, "target_binding_changed")
                self.assertFalse(supervisor._resolved_peers)

                if changed_target == "connection":
                    next_rpc = FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
                    # A new supervisor cycle supplies the new bound manifest and cannot reuse the failed cycle.
                    supervisor.system.manifest["updatedAtMs"] = 1_700_000_000_000
                    supervisor.rpc_factory = lambda _url, _token, client=next_rpc: client
                    evaluation = supervisor.evaluate()
                    self.assertEqual(evaluation.peer_map, {PEER_UID: "20001"})
                    self.assertEqual(len(supervisor._resolved_peers), 1)
                    self.assertEqual(next(iter(supervisor._resolved_peers))[4], "new-generation")

    def test_discovery_deadline_covers_rpc_work_and_final_binding_recheck(self) -> None:
        clock = [0.0]
        system = FakeSystem()
        rpc = FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
        rpc.after_call = lambda _action, _params: clock.__setitem__(0, clock[0] + 1.0)
        system.revalidate_hook = lambda: clock.__setitem__(0, clock[0] + 14.0)
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(
                Path(temporary), system=system, rpc=rpc,
                monotonic_clock=lambda: clock[0], identity_discovery_timeout_seconds=15.0,
            )
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "final_binding_deadline_exhausted")
            self.assertFalse(supervisor._resolved_peers)
            self.assertEqual(rpc.timeouts[-4:], [15.0, 14.0, 13.0, 12.0])

    def test_cached_binding_fence_busy_has_distinct_peer_timeout_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
            supervisor = self._supervisor(root, rpc=rpc)
            supervisor.evaluate()
            self.assertEqual(len(supervisor._resolved_peers), 1)

            self.assertTrue(supervisor._binding_fence_lock.acquire(blocking=False))
            try:
                with self.assertRaises(ServiceError) as caught:
                    supervisor.evaluate()
            finally:
                supervisor._binding_fence_lock.release()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "cached_binding_fence_busy")
            self.assertEqual(len(supervisor._resolved_peers), 1, "the early cached-lock failure keeps existing cache behavior")

    def test_cached_login_rpc_timeout_has_operation_specific_stage(self) -> None:
        class LoginTimeoutRpc(FakeRpc):
            def __init__(self):
                super().__init__(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
                self.login_call_count = 0
                self.timeout_login_call: int | None = None

            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                if action == "get_login_info":
                    self.login_call_count += 1
                    if self.login_call_count == self.timeout_login_call:
                        raise OneBotError("onebot_timeout")
                return super().call(action, params, timeout=timeout)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = LoginTimeoutRpc()
            supervisor = self._supervisor(root, rpc=rpc)
            supervisor.evaluate()
            self.assertEqual(len(supervisor._resolved_peers), 1)
            # Call 3 is the next evaluate's initial login check; call 4 is the
            # cached-map identity recheck inside the existing 15-second fence.
            rpc.timeout_login_call = 4

            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "onebot_login_rpc_wait_timeout")
            self.assertFalse(supervisor._resolved_peers, "the existing timeout-driven cache eviction remains unchanged")

    def test_cached_self_rpc_timeout_has_operation_specific_stage(self) -> None:
        class SelfTimeoutRpc(FakeRpc):
            def __init__(self):
                super().__init__(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
                self.self_call_count = 0
                self.timeout_self_call: int | None = None

            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                if action == "get_stranger_info" and str((params or {}).get("user_id")) == "12345":
                    self.self_call_count += 1
                    if self.self_call_count == self.timeout_self_call:
                        raise OneBotError("onebot_timeout")
                return super().call(action, params, timeout=timeout)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = SelfTimeoutRpc()
            supervisor = self._supervisor(root, rpc=rpc)
            supervisor.evaluate()
            self.assertEqual(len(supervisor._resolved_peers), 1)
            # The first two account lookups seed the map; the next two are the
            # subsequent evaluate and its cached-map identity recheck.
            rpc.timeout_self_call = 4

            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "onebot_self_rpc_wait_timeout")
            self.assertFalse(supervisor._resolved_peers, "the existing timeout-driven cache eviction remains unchanged")

    def test_identity_deadline_exhaustion_has_fixed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary), monotonic_clock=lambda: 5.0)
            with self.assertRaises(ServiceError) as caught:
                supervisor._identity_time_remaining(4.0)

        self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
        self.assertEqual(caught.exception._diagnostic_stage, "identity_deadline_exhausted")

    def test_final_binding_fence_busy_has_distinct_peer_timeout_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()

            self.assertTrue(supervisor._binding_fence_lock.acquire(blocking=False))
            try:
                with self.assertRaises(ServiceError) as caught:
                    supervisor._binding_still_current_until(
                        evaluation.manifest,
                        evaluation.binding,
                        time.monotonic() + 1.0,
                    )
            finally:
                supervisor._binding_fence_lock.release()

            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "final_binding_fence_busy")

    def test_slow_final_binding_fence_times_out_without_late_cache_or_worker_pileup(self) -> None:
        system = FakeSystem()
        fence_started = threading.Event()
        fence_finished = threading.Event()
        revalidation_calls: list[bool] = []

        def slow_revalidation() -> None:
            revalidation_calls.append(True)
            fence_started.set()
            time.sleep(0.45)
            fence_finished.set()

        system.revalidate_hook = slow_revalidation
        rpc = FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(
                Path(temporary), system=system, rpc=rpc,
                identity_discovery_timeout_seconds=0.2,
            )
            started = time.monotonic()
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            elapsed = time.monotonic() - started
            self.assertEqual((caught.exception.code, caught.exception.phase), ("peer_identity_timeout", "waiting_identity"))
            self.assertEqual(caught.exception._diagnostic_stage, "final_binding_fence_wait_timeout")
            self.assertTrue(fence_started.is_set())
            self.assertLess(elapsed, 0.35)
            self.assertFalse(supervisor._resolved_peers)
            self.assertTrue(supervisor._binding_fence_lock.locked())

            with self.assertRaises(ServiceError) as retry:
                supervisor.evaluate()
            self.assertEqual(retry.exception.code, "peer_identity_timeout")
            self.assertEqual(retry.exception._diagnostic_stage, "peer_discovery_fence_busy")
            self.assertEqual(caught.exception._diagnostic_stage, "final_binding_fence_wait_timeout")
            self.assertEqual(len(revalidation_calls), 1, "diagnostic propagation must not add a binding revalidation")
            self.assertEqual(sum(action == "get_friend_list" for action, _ in rpc.calls), 1)
            self.assertFalse(supervisor._resolved_peers)
            self.assertTrue(fence_finished.wait(1))
            release_deadline = time.monotonic() + 1.0
            while supervisor._binding_fence_lock.locked() and time.monotonic() < release_deadline:
                time.sleep(0.005)
            self.assertFalse(supervisor._binding_fence_lock.locked())
            self.assertEqual(caught.exception._diagnostic_stage, "final_binding_fence_wait_timeout")
            self.assertFalse(supervisor._resolved_peers)

    def test_manifest_switch_during_final_binding_recheck_rejects_old_cycle_cache(self) -> None:
        system = FakeSystem()

        def switch_connection_generation() -> None:
            system.manifest = {**system.manifest, "connectionGeneration": "new-generation"}

        system.revalidate_hook = switch_connection_generation
        rpc = FakeRpc(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = self._supervisor(Path(temporary), system=system, rpc=rpc)
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual((caught.exception.code, caught.exception.phase), ("target_binding_changed", "waiting_target"))
            self.assertFalse(supervisor._resolved_peers)

    def test_auto_discovery_cache_hit_rechecks_onebot_identity(self) -> None:
        class ChangeSelfUidAfterInitialIdentity(FakeRpc):
            def call(self, action: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
                result = super().call(action, params, timeout=timeout)
                if action == "get_stranger_info" and params and params.get("user_id") == "12345":
                    self.self_queries += 1
                    if self.self_queries == 3:
                        self.self_uid = "u_" + ("c" * 22)
                return result

            def __init__(self, **kwargs: Any):
                super().__init__(**kwargs)
                self.self_queries = 0

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rpc = ChangeSelfUidAfterInitialIdentity(friends=[{"user_id": "20001"}], friend_uids={"20001": PEER_UID})
            supervisor = self._supervisor(root, rpc=rpc)
            supervisor.evaluate()
            self.assertEqual(sum(action == "get_friend_list" for action, _ in rpc.calls), 1)
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual(caught.exception.code, "self_uid_changed")
            self.assertFalse(supervisor._resolved_peers)

    def test_peer_map_requires_matching_onebot_uin_and_never_falls_back_after_rpc_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, peer_map={PEER_UID: "10002"})
            supervisor = self._supervisor(
                root, config=config, rpc=FakeRpc(include_peer_uid=False, peer_response_uin="10003"),
            )
            evaluation = supervisor.evaluate()
            self.assertEqual(evaluation.peer_map, {})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, peer_map={PEER_UID: "10002"})
            supervisor = self._supervisor(root, config=config, rpc=FakeRpc(fail_peer_lookup=True))
            evaluation = supervisor.evaluate()
            self.assertEqual(evaluation.peer_map, {})

    def test_peer_map_uid_mismatch_quarantines_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, peer_map={PEER_UID: "10002"})
            supervisor = self._supervisor(
                root, config=config, rpc=FakeRpc(peer_uid="u_" + ("c" * 22)),
            )
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual(caught.exception.code, "peer_identity_mismatch")
            self.assertEqual(caught.exception.phase, "quarantined")

    def test_audio_guard_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            config["native"]["guard_config_file"] = ""
            supervisor = self._supervisor(root, config=config)
            with self.assertRaises(ServiceError) as caught:
                supervisor.evaluate()
            self.assertEqual(caught.exception.code, "audio_guard_config_missing")
            self.assertEqual(caught.exception.phase, "quarantined")

    def test_detach_unconfirmed_quarantines_kernel_and_stops_only_owned_host_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            evaluation = SimpleNamespace(kernel_pid=51200, kernel_filetime=512000)
            owned_hosts = [
                {"role": "host", "pid": 8100, "creationTime100ns": "810000", "imagePath": "C:\\host.exe", "profilePath": "C:\\profile"},
                {"role": "launcher", "pid": 8101, "creationTime100ns": "810100", "imagePath": "C:\\launcher.exe", "profilePath": "C:\\profile"},
            ]
            active = ActiveGeneration(
                evaluation, 10, 11, root / ".runtime" / "service" / "generations" / "connection-11",
                HostLaunch(9, "", "", ""), owned_hosts, 8100, "810000", "sidecar", FakeBridge(False),
            )
            supervisor.active = active
            self.assertFalse(supervisor._stop_active())
            self.assertEqual(system.terminate_calls, [owned_hosts])
            self.assertTrue(supervisor._kernel_quarantined(evaluation))
            quarantine = read_private_json(supervisor.quarantine_path)
            self.assertEqual(quarantine["kernels"], [{"pid": 51200, "creationTime100ns": "512000", "reason": "detach_unconfirmed"}])

    def test_pid_reuse_with_new_filetime_releases_old_kernel_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            supervisor._quarantine_process(51200, 512000, "shim_consumed")
            old = SimpleNamespace(kernel_pid=51200, kernel_filetime=512000)
            self.assertTrue(supervisor._kernel_quarantined(old))
            system.live[51200] = 512001
            new = SimpleNamespace(kernel_pid=51200, kernel_filetime=512001)
            self.assertFalse(supervisor._kernel_quarantined(new))

    def test_unknown_quarantined_kernel_liveness_blocks_only_exact_incarnation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            evaluation = supervisor.evaluate()
            supervisor._quarantine_kernel(evaluation, "shim_consumed")
            system.process_is_current = lambda _pid, _creation: None

            self.assertTrue(supervisor._kernel_quarantined(evaluation))
            different_incarnation = SimpleNamespace(
                kernel_pid=evaluation.kernel_pid,
                kernel_filetime=evaluation.kernel_filetime + 1,
            )
            self.assertFalse(supervisor._kernel_quarantined(different_incarnation))
            self.assertEqual(read_private_json(supervisor.quarantine_path)["kernels"], [{
                "pid": evaluation.kernel_pid,
                "creationTime100ns": str(evaluation.kernel_filetime),
                "reason": "shim_consumed",
            }])

    def test_missing_kernel_liveness_accessor_blocks_exact_quarantined_incarnation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            evaluation = supervisor.evaluate()
            supervisor._quarantine_kernel(evaluation, "shim_consumed")
            supervisor.system = SimpleNamespace()

            self.assertTrue(supervisor._kernel_quarantined(evaluation))

    def test_run_once_does_not_start_generation_when_kernel_liveness_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            supervisor = self._supervisor(root, system=system)
            evaluation = supervisor.evaluate()
            supervisor._quarantine_kernel(evaluation, "shim_consumed")
            system.process_is_current = lambda _pid, _creation: None

            class FakeLock:
                def __init__(self, _path: Path):
                    pass

                def acquire(self) -> bool:
                    return True

                def release(self) -> None:
                    return None

            with patch.object(supervisor, "_validate_static_dependencies"), patch.object(
                manager_module, "ProcessLock", FakeLock,
            ), patch.object(
                supervisor, "_start_generation", side_effect=AssertionError("unknown Kernel liveness must block startup"),
            ):
                status, code = supervisor.run(once=True)

            self.assertEqual(code, 0)
            self.assertEqual(status["phase"], "waiting_kernel_restart")

    def test_attach_attempt_marker_survives_manager_restart_and_quarantines_exact_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            system = supervisor.system
            generation_dir = supervisor.runtime_dir / "generations" / "connection-22"
            marker = generation_dir / "attach-attempt.private.json"
            atomic_private_json(marker, {
                "schema": 1,
                "supervisorPid": 700,
                "supervisorCreationTime100ns": "700000",
                "runGeneration": 20,
                "controllerGeneration": 22,
                "hostGeneration": 21,
                "kernelPid": 51200,
                "kernelCreationTime100ns": "512000",
                "attemptedAtMs": 1_700_000_000_000,
            })
            atomic_private_json(supervisor.manager_path, {
                "schema": 1,
                "pid": 700,
                "creationTime100ns": "700000",
                "runGeneration": 20,
                "controllerGeneration": 22,
                "hostGeneration": 21,
                "kernelPid": 51200,
                "kernelCreationTime100ns": "512000",
                "attachAttemptPath": str(marker),
            })
            supervisor._recover_prior_attach_attempt()
            self.assertTrue(supervisor._kernel_quarantined(SimpleNamespace(kernel_pid=51200, kernel_filetime=512000)))
            system.live[51200] = 512001
            self.assertFalse(supervisor._kernel_quarantined(SimpleNamespace(kernel_pid=51200, kernel_filetime=512001)))

    def test_malformed_stop_request_is_rejected_without_unhandled_type_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            system.live[700] = 700000
            supervisor = self._supervisor(root, system=system)
            supervisor._ensure_private_runtime()
            atomic_private_json(supervisor.manager_path, {
                "schema": 1, "pid": 700, "creationTime100ns": "700000",
                "runGeneration": 20, "startedAtMs": 100,
            })
            atomic_private_json(supervisor._stop_request_path(), {
                "schema": 1, "requestId": str(uuid.uuid4()), "pid": 700,
                "creationTime100ns": "700000", "runGeneration": 20,
                "requestedAtMs": "not-a-number",
            })
            result, code = supervisor.request_stop()
            self.assertEqual(code, 2)
            self.assertEqual(result["code"], "stop_request_binding_mismatch")

    def test_stop_request_uses_current_manager_scope_and_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = FakeSystem()
            system.live[700] = 700000
            supervisor = self._supervisor(root, system=system)
            supervisor._ensure_private_runtime()
            atomic_private_json(supervisor.manager_path, {
                "schema": 1, "pid": 700, "creationTime100ns": "700000",
                "runGeneration": 20, "startedAtMs": 100,
            })
            first, first_code = supervisor.request_stop()
            second, second_code = supervisor.request_stop()
            self.assertEqual((first_code, second_code), (0, 0))
            self.assertEqual(first, {"ok": True, "phase": "stop_requested"})
            self.assertEqual(second, first)
            self.assertEqual(read_private_json(supervisor._stop_request_path())["runGeneration"], 20)

    def test_status_schema_is_stable_and_does_not_include_account_identity_or_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = self._supervisor(root)
            status = supervisor._status("needs_attachment", "admin_attach_required")
            on_disk = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(set(on_disk), {
                "schema", "phase", "ready", "updatedAtMs", "supervisorPid", "supervisorCreationTime100ns", "code",
                "universalService",
            })
            self.assertEqual(on_disk["universalService"], {"state": "not_checked", "code": "", "reused": False})
            self.assertEqual(status, on_disk)
            encoded = json.dumps(on_disk)
            self.assertNotIn("12345", encoded)
            self.assertNotIn(SELF_UID, encoded)
            self.assertNotIn("token", encoded.lower())

    def test_ready_status_is_accepted_by_the_actual_webui_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            public_config = copy.deepcopy(DEFAULT_CONFIG)
            public_config["plugin"]["enabled"] = True
            public_config["qqcall"].update({"account_id": "12345"})
            public_config["qqcall"]["onebot"]["url"] = "http://127.0.0.1:8999"
            public_config["qqcall"]["paths"]["qq_exe"] = str(root / "QQ.exe")
            public_config["qqcall"]["runtime_dir"] = config["runtime_dir"]
            public_config["qqcall"]["audio"].update({
                "capture_device": "Capture (VB-Audio Virtual Cable) [loopback]",
                "playback_device": "Playback (VB-Audio Virtual Cable)",
            })
            _write_toml(root / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml", public_config)
            system = FakeSystem()
            supervisor = self._supervisor(root, config=config, system=system)
            supervisor.clock_ms = lambda: int(time.time() * 1000)
            supervisor.supervisor_pid = 700
            supervisor.supervisor_creation_time_100ns = "700000"
            status = supervisor._status("ready", "identity_and_login_verified")
            self.assertTrue(status["ready"])
            self.assertNotIn("code", status)

            webui = webui_process_manager.ProcessManager(root)
            webui.states["qqcall"] = webui_process_manager.ServiceState(
                status=webui_process_manager.ServiceStatus.RUNNING,
                process=SimpleNamespace(returncode=None),
                pid=700,
            )
            webui._qqcall_process_creation_time_100ns = lambda _pid: "700000"
            self.assertEqual(webui._read_qqcall_phase(), {
                "phase": "ready", "ready": True, "detail": "通话控制链就绪",
                "universalService": {"state": "not_checked", "code": "", "reused": False},
            })

            supervisor.universal_service_status = {
                "state": "unavailable", "code": "universal_api_auth_failed", "reused": False,
            }
            status = supervisor._status("ready", "identity_and_login_verified")
            self.assertTrue(status["ready"])
            phase = webui._read_qqcall_phase()
            self.assertTrue(phase["ready"])
            self.assertIn("UniversalVC 认证失败", phase["detail"])
            self.assertEqual(phase["universalService"]["code"], "universal_api_auth_failed")

    def test_core_consumes_manager_snapshot_and_uses_same_control_token_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixed_root_patch = patch.object(shared_qqcall_service, "_SERVICE_PROJECT_ROOT", root)
            fixed_root_patch.start()
            self.addCleanup(fixed_root_patch.stop)
            canonical_path_patch = patch.object(
                shared_qqcall_service,
                "canonical_qqcall_config_path",
                return_value=root / "config.toml",
            )
            canonical_path_patch.start()
            self.addCleanup(canonical_path_patch.stop)
            config = _config(root, peer_map={PEER_UID: "10002"})
            supervisor = self._supervisor(root, config=config, rpc=FakeRpc(include_peer_uid=False))
            evaluation = supervisor.evaluate()
            supervisor._ensure_private_runtime()
            runtime_dir = Path(config["runtime_dir"])
            expected_token = runtime_dir / "control.token.private"
            manager_module.read_or_create_private_token(expected_token)

            generation_dir = runtime_dir / "generations" / "connection-22"
            generation_dir.mkdir(parents=True)
            active = ActiveGeneration(
                evaluation, 21, 22, generation_dir, HostLaunch(9, "", "", ""),
                [], 8100, "810000", "host-sidecar", FakeBridge(True),
                service_instance=str(uuid.uuid4()),
            )
            supervisor._write_generation_identity(active)

            adapter_toml = root / "napcat.toml"
            adapter_toml.write_text(
                "[qqcall]\nservice_config_file = " + json.dumps(str(root / "retired-config.json")) + "\n",
                encoding="utf-8",
            )
            # The real Core intentionally anchors runtime files beneath its
            # fixed project root. Point that fixed boundary at this isolated
            # fixture so the cross-module contract test never touches the live
            # checkout's .runtime directory.
            settings = load_qqcall_adapter_settings("napcat", adapter_toml, QQCallConfig())
            self.assertIsNone(settings.error_code)
            self.assertEqual(Path(settings.config.token_file).resolve(), expected_token.resolve())

            fake_process = SimpleNamespace(
                pid=701, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(), poll=lambda: None,
            )
            with patch("service.manager.subprocess.Popen", return_value=fake_process) as popen:
                supervisor._start_bridge_process(
                    evaluation, 21, 22, 8100, "host-sidecar", generation_dir,
                    generation_dir / "kernel-peer-identities.private.json", None,
                    generation_dir / "identity-source.private.json",
                    generation_dir / "login-identity.private.json",
                )
            command = popen.call_args.args[0]
            token_argument = command[command.index("--control-token-file") + 1]
            self.assertEqual(Path(token_argument).resolve(), Path(settings.config.token_file).resolve())
            self.assertEqual(
                command[command.index("--kernel-creation-time-100ns") + 1],
                str(evaluation.kernel_filetime),
                "the supervisor supplies the current validated Kernel FILETIME to the Bridge CLI",
            )

            runtime = QQCallRuntime(
                settings.config, "napcat", lambda _message: None, lambda _name, _data: None,
                lambda: {"connected": True}, lambda _user: True,
            )
            snapshot = read_private_json(runtime_dir / "adapter-runtime.private.json")
            meta = {
                "serviceInstance": snapshot["serviceInstance"],
                "hostGeneration": snapshot["hostGeneration"],
                "connectionGeneration": snapshot["connectionGeneration"],
            }
            self.assertEqual(runtime._read_peer_map(meta), evaluation.peer_map)
            self.assertEqual(snapshot["kernelCreationTime100ns"], "512000")
            self.assertEqual(snapshot["peerMap"], {PEER_UID: "10002"})

    def test_stale_or_outside_runtime_location_is_rejected_before_filesystem_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            config["runtime_dir"] = str(root / "outside")
            supervisor = self._supervisor(root, config=config)
            with self.assertRaisesRegex(ServiceError, "runtime_must_be_private_project_path"):
                supervisor._ensure_private_runtime()
            self.assertFalse((root / "outside").exists())

    def test_generated_admin_helper_is_scoped_but_never_executed_by_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            # Include shell metacharacters in a harmless path to test PS literal quoting.
            config["paths"]["qq_exe"] = str(root / "QQ$Folder'Quoted.exe")
            system = FakeSystem()
            supervisor = self._supervisor(root, config=config, system=system)
            supervisor.run_identity = {
                "schema": 1, "pid": 700, "creationTime100ns": "700000", "runGeneration": 20, "startedAtMs": 10,
            }
            generation = root / ".runtime" / "service" / "generations" / "connection-22"
            generation.mkdir(parents=True)
            for path in (
                root / "scripts" / "qqcall_injector.py",
                root / "service" / "process_identity.py",
                root / "native" / "build" / "x64" / "QQCallRecoveryShim2.dll",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            evaluation = SimpleNamespace(kernel_pid=51200, kernel_filetime=512000)
            with patch.object(manager_module, "_sha256_file", return_value=manager_module._RECOVERY_SHIM_SHA256.lower()):
                helper = supervisor._write_attach_helper(
                    evaluation, generation, generation / "broker-config.private.json", 21, 22, 701, "701000",
                )
            script = helper.read_text(encoding="utf-8")
            self.assertIn("$ExpectedImage = '" + config["paths"]["qq_exe"].replace("'", "''") + "'", script)
            self.assertIn("$manager.attachAttemptPath -ne $MarkerFile", script)
            self.assertIn(f"$AttachStatusMaxAgeMs = {manager_module._ATTACH_STATUS_MAX_AGE_MS}", script)
            self.assertIn("($now - [long]$status.updatedAtMs) -gt $AttachStatusMaxAgeMs", script)
            for identity_gate in (
                "$manager.pid -ne $ExpectedSupervisorPid",
                "$manager.creationTime100ns -ne $ExpectedSupervisorFileTime",
                "$manager.runGeneration -ne $ExpectedRunGeneration",
                "$manager.controllerGeneration -ne $ExpectedControllerGeneration",
                "$manager.hostGeneration -ne $ExpectedHostGeneration",
                "$manager.controllerPid -ne $ExpectedControllerPid",
                "$manager.controllerCreationTime100ns -ne $ExpectedControllerFileTime",
                "$manager.kernelPid -ne $ExpectedKernelPid",
                "$manager.kernelCreationTime100ns -ne $ExpectedKernelFileTime",
            ):
                self.assertIn(identity_gate, script)
            self.assertIn("$manager.controllerPid -ne $ExpectedControllerPid", script)
            self.assertIn("$manager.controllerCreationTime100ns -ne $ExpectedControllerFileTime", script)
            self.assertIn("$controllerIdentity.creationTime100ns -ne $ExpectedControllerFileTime", script)
            self.assertIn("function ConvertFrom-InventoryJson", script)
            self.assertIn('Get-Command ConvertFrom-Json -ErrorAction Stop', script)
            self.assertIn('Parameters.ContainsKey("DateKind")', script)
            self.assertIn("-DateKind String", script)
            self.assertIn("$_.createdUtc -is [string]", script)
            self.assertIn("$_.createdUtc -ceq $ExpectedStart", script)
            self.assertIn("[IO.File]::Move($markerTemp, $MarkerFile)", script)
            self.assertIn("[IO.FileMode]::CreateNew", script)
            self.assertIn("--recovery-shim --recovery-attempt 2 --inject", script)
            self.assertNotIn("Start-Process", script)
            self.assertEqual(system.launch_count, 0)
            self.assertEqual(system.terminate_calls, [])
            self.assertFalse((generation / "attach-attempt.private.json").exists())

            parser_start = script.index("function ConvertFrom-InventoryJson {")
            parser_end = script.index("\n}\n$identity =", parser_start) + 2
            parser_function = script[parser_start:parser_end]
            expected_start = "2026-10-08T02:07:22.888423Z"
            fixture = {
                "processes": [{
                    "pid": 30024,
                    "startFileTime": "134358988428884232",
                    "createdUtc": expected_start,
                    "imagePath": config["paths"]["qq_exe"],
                }],
            }
            encoded_fixture = base64.b64encode(json.dumps(fixture).encode("utf-8")).decode("ascii")
            command_template = parser_function + f'''\n$ErrorActionPreference = "Stop"
$converter = Get-Command ConvertFrom-Json -ErrorAction Stop
if ("{{MODE}}" -eq "modern" -and -not $converter.Parameters.ContainsKey("DateKind")) {{ throw "DateKind unavailable" }}
if ("{{MODE}}" -eq "legacy" -and $converter.Parameters.ContainsKey("DateKind")) {{ throw "DateKind unexpectedly available" }}
$raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_fixture}'))
$expectedStart = '{expected_start}'
$expectedFileTime = '134358988428884232'
$expectedImage = '{config["paths"]["qq_exe"].replace("'", "''")}'
$parsed = ConvertFrom-InventoryJson -Json $raw
$item = @($parsed.processes)[0]
if ($item.createdUtc -isnot [string] -or $item.createdUtc -cne $expectedStart) {{ throw "createdUtc was not retained as exact text" }}
$matches = @($parsed.processes | Where-Object {{ $_.pid -eq 30024 -and $_.startFileTime -eq $expectedFileTime -and ($_.createdUtc -is [string]) -and ($_.createdUtc -ceq $expectedStart) -and [string]::Equals($_.imagePath, $expectedImage, [StringComparison]::OrdinalIgnoreCase) }})
if ($matches.Count -ne 1) {{ throw "exact inventory row did not match" }}
$wrongIso = $raw.Replace($expectedStart, '2026-10-08T02:07:22.888424Z')
$wrongIsoItem = @( (ConvertFrom-InventoryJson -Json $wrongIso).processes )
if (@($wrongIsoItem | Where-Object {{ $_.createdUtc -is [string] -and $_.createdUtc -ceq $expectedStart }}).Count -ne 0) {{ throw "wrong ISO timestamp matched" }}
$wrongFileTime = $raw.Replace($expectedFileTime, '134358988428884233')
$wrongFileTimeItem = @( (ConvertFrom-InventoryJson -Json $wrongFileTime).processes )
if (@($wrongFileTimeItem | Where-Object {{ $_.startFileTime -eq $expectedFileTime }}).Count -ne 0) {{ throw "wrong FILETIME matched" }}
Write-Output "inventory-json-ok"
'''
            powershells = (
                ("modern", shutil.which("pwsh")),
                ("legacy", shutil.which("powershell")),
            )
            for mode, executable in powershells:
                with self.subTest(powershell=mode):
                    if executable is None:
                        self.skipTest(f"{mode} PowerShell executable is unavailable for generated-helper JSON integration test")
                    completed = subprocess.run(
                        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command_template.replace("{MODE}", mode)],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), "inventory-json-ok")

            powershell7 = shutil.which("pwsh")
            if powershell7 is None:
                self.skipTest("PowerShell 7 is unavailable for generated-helper freshness-gate integration test")
            freshness_command = r'''$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:ATTACH_HELPER_PATH, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw "generated helper parse failed" }
$gates = @($ast.FindAll({
    param($node)
    if ($node -isnot [System.Management.Automation.Language.IfStatementAst]) { return $false }
    $statement = $node.Extent.Text
    return $statement.Contains('$status.phase') -and $statement.Contains('$AttachStatusMaxAgeMs')
}, $true))
if ($gates.Count -ne 1) { throw "status gate AST was ambiguous" }
$gateText = $gates[0].Extent.Text
if ($gateText -match '(?i)inject|marker|FileMode|PythonExe') { throw "status gate crossed the pre-injection boundary" }
$gateBlock = [scriptblock]::Create($gateText)
$now = [long]1700000000000
$AttachStatusMaxAgeMs = 60000
$cases = @(
    @{ name = 'age_14s'; updatedAtMs = $now - 14000; phase = 'needs_attachment'; ready = $false; expected = 'accepted' },
    @{ name = 'age_60s'; updatedAtMs = $now - 60000; phase = 'needs_attachment'; ready = $false; expected = 'accepted' },
    @{ name = 'age_61s'; updatedAtMs = $now - 61000; phase = 'needs_attachment'; ready = $false; expected = 'rejected' },
    @{ name = 'future'; updatedAtMs = $now + 1; phase = 'needs_attachment'; ready = $false; expected = 'rejected' },
    @{ name = 'wrong_phase'; updatedAtMs = $now - 1000; phase = 'ready'; ready = $false; expected = 'rejected' },
    @{ name = 'ready_true'; updatedAtMs = $now - 1000; phase = 'needs_attachment'; ready = $true; expected = 'rejected' }
)
foreach ($case in $cases) {
    $status = [pscustomobject]@{ phase = $case.phase; ready = $case.ready; updatedAtMs = $case.updatedAtMs }
    $outcome = 'accepted'
    try { & $gateBlock } catch { $outcome = 'rejected' }
    if ($outcome -ne $case.expected) { throw "freshness gate fixture mismatch" }
}
Write-Output "attach-freshness-gate-ok"
'''
            completed = subprocess.run(
                [powershell7, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", freshness_command],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env={**os.environ, "ATTACH_HELPER_PATH": str(helper)},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "attach-freshness-gate-ok")


class ServiceCliTests(unittest.TestCase):
    def test_check_plan_and_disabled_run_never_autostart_or_create_api_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enabled_root = root / "enabled"
            enabled_root.mkdir()
            enabled_config = _config(enabled_root)
            factory_calls: list[dict[str, Any]] = []

            def unexpected_factory(**kwargs):
                factory_calls.append(kwargs)
                raise AssertionError("read-only commands must not construct the UniversalVC supervisor")

            enabled = ServiceManagerTests()._supervisor(
                enabled_root, config=enabled_config,
                universal_service_factory=unexpected_factory,
            )
            with (
                patch.object(enabled, "_validate_private_runtime_location", return_value=None),
                patch.object(enabled, "_validate_static_dependencies", return_value=None),
                patch.object(enabled, "evaluate", return_value=object()),
                patch.object(enabled, "_validate_ports", return_value=None),
            ):
                self.assertEqual(enabled.check()[0]["phase"], "waiting_adapter")
                self.assertEqual(enabled.plan()["phase"], "needs_attachment")

            disabled_root = root / "disabled"
            disabled_root.mkdir()
            disabled_config = _config(disabled_root, enabled=False)
            disabled = ServiceManagerTests()._supervisor(
                disabled_root, config=disabled_config,
                universal_service_factory=unexpected_factory,
            )
            self.assertEqual(disabled.check()[0]["phase"], "disabled")
            self.assertEqual(disabled.plan()["phase"], "disabled")
            self.assertEqual(disabled.run(once=True)[0]["phase"], "disabled")
            self.assertEqual(factory_calls, [])
            self.assertFalse((root / "enabled" / ".runtime").exists())
            self.assertFalse((disabled_root / ".runtime").exists())

    def test_check_disabled_succeeds_without_runtime_mutation_and_invalid_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "disabled.toml"
            _write_toml(config_path, copy.deepcopy(DEFAULT_CONFIG))
            output = io.StringIO()
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path), contextlib.redirect_stdout(output):
                self.assertEqual(main(["check", "--config", str(config_path)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["phase"], "disabled")
            self.assertFalse(result["ready"])
            self.assertFalse((root / ".runtime").exists())

            invalid = copy.deepcopy(DEFAULT_CONFIG)
            invalid["extra"] = "no"
            _write_toml(config_path, invalid)
            output = io.StringIO()
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path), contextlib.redirect_stdout(output):
                self.assertEqual(main(["check", "--config", str(config_path)]), 2)
            self.assertEqual(json.loads(output.getvalue())["code"], "config_fields_invalid")

            alternate = root / "alternate.toml"
            _write_toml(alternate, copy.deepcopy(DEFAULT_CONFIG))
            output = io.StringIO()
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path), contextlib.redirect_stdout(output):
                self.assertEqual(main(["check", "--config", str(alternate)]), 2)
            self.assertEqual(json.loads(output.getvalue())["code"], "config_path_noncanonical")

    def test_disabled_plan_run_and_status_do_not_create_runtime_or_start_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "disabled.toml"
            _write_toml(config_path, copy.deepcopy(DEFAULT_CONFIG))
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path):
                for command in ("plan", "run", "status"):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main([command, "--config", str(config_path)]), 0)
                    payload = json.loads(output.getvalue())
                    self.assertFalse(payload["ready"])
                    self.assertEqual(payload["phase"], "disabled")
                    self.assertFalse((root / ".runtime").exists())

    def test_status_cli_reads_only_safe_public_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "disabled.toml"
            raw_config = copy.deepcopy(DEFAULT_CONFIG)
            raw_config["qqcall"]["runtime_dir"] = str(root / ".runtime" / "service")
            _write_toml(config_path, raw_config)
            runtime = root / ".runtime" / "service"
            status_path = runtime / "status.json"
            atomic_private_json(status_path, {
                "schema": 1,
                "phase": "ready",
                "ready": True,
                # An old timestamp alone must not hide a supervisor whose
                # exact process identity is still live.
                "updatedAtMs": 1_700_000_000_000,
                "supervisorPid": 700,
                "supervisorCreationTime100ns": "700000",
            })
            output = io.StringIO()
            with (
                patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path),
                patch.object(manager_module.WindowsSystem, "process_is_current", return_value=True) as process_is_current,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["status", "--config", str(config_path)]), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["phase"], "ready")
            self.assertTrue(value["ready"])
            self.assertNotIn("enabled", value)
            self.assertNotIn("adapter", value)
            process_is_current.assert_called_once_with(700, 700000)

    def test_status_cli_fails_closed_for_missing_or_unverified_supervisor_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "enabled.toml"
            raw_config = copy.deepcopy(DEFAULT_CONFIG)
            raw_config["qqcall"]["runtime_dir"] = str(root / ".runtime" / "service")
            _write_toml(config_path, raw_config)
            status_path = root / ".runtime" / "service" / "status.json"
            base = {
                "schema": 1,
                "phase": "ready",
                "ready": True,
                "updatedAtMs": 1_700_000_000_000,
                "supervisorPid": 700,
                "supervisorCreationTime100ns": "700000",
            }

            cases = (
                ("missing_pid", {key: value for key, value in base.items() if key != "supervisorPid"}, "unavailable"),
                ("zero_pid", {**base, "supervisorPid": 0}, "unavailable"),
                ("reused_pid", base, "reused"),
                ("identity_mismatch", {**base, "supervisorCreationTime100ns": "700001"}, "mismatch"),
                ("identity_query_unavailable", base, "unavailable"),
            )
            for name, status, outcome in cases:
                with self.subTest(name=name):
                    atomic_private_json(status_path, status)
                    output = io.StringIO()
                    with patch.object(manager_module, "DEFAULT_CONFIG_PATH", config_path), contextlib.redirect_stdout(output):
                        if outcome == "reused":
                            # The PID still exists, but it now denotes another process incarnation.
                            identity_patch = patch.object(
                                manager_module.WindowsSystem,
                                "process_identity",
                                return_value=(700001, "manager.exe"),
                            )
                        elif outcome == "mismatch":
                            identity_patch = patch.object(
                                manager_module.WindowsSystem,
                                "process_is_current",
                                return_value=False,
                            )
                        elif name == "identity_query_unavailable":
                            identity_patch = patch.object(
                                manager_module.WindowsSystem,
                                "process_is_current",
                                return_value=None,
                            )
                        else:
                            identity_patch = patch.object(
                                manager_module.WindowsSystem,
                                "process_is_current",
                                return_value=True,
                            )
                        with identity_patch as identity_check:
                            self.assertEqual(main(["status", "--config", str(config_path)]), 0)
                    value = json.loads(output.getvalue())
                    self.assertFalse(value["ready"])
                    self.assertEqual(value["phase"], "error")
                    self.assertEqual(value["code"], "status_unavailable")
                    if name in ("missing_pid", "zero_pid"):
                        identity_check.assert_not_called()
                    elif outcome == "reused":
                        self.assertTrue(any(call.args == (700,) for call in identity_check.call_args_list))
                    else:
                        identity_check.assert_called_once()

    def test_status_cli_preserves_no_manager_and_disabled_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enabled_root = root / "enabled"
            enabled_root.mkdir()
            enabled_path = enabled_root / "config.toml"
            _config(enabled_root)

            output = io.StringIO()
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", enabled_path), contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--config", str(enabled_path)]), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["phase"], "waiting_adapter")
            self.assertEqual(value["code"], "status_unavailable")
            self.assertFalse(value["ready"])
            self.assertFalse((enabled_root / ".runtime").exists())

            disabled_root = root / "disabled"
            disabled_root.mkdir()
            disabled_path = disabled_root / "config.toml"
            _config(disabled_root, enabled=False)
            output = io.StringIO()
            with patch.object(manager_module, "DEFAULT_CONFIG_PATH", disabled_path), contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--config", str(disabled_path)]), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["phase"], "disabled")
            self.assertEqual(value["code"], "status_unavailable")
            self.assertFalse(value["ready"])
            self.assertFalse((disabled_root / ".runtime").exists())


if __name__ == "__main__":
    unittest.main()
