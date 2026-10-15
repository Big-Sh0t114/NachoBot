from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import service.universal_service as universal_service_module
from service.universal_service import (
    UniversalServiceError,
    UniversalServiceSupervisor,
    _ProbeFailure,
    _HEALTH_TIMEOUT_SECONDS,
    _health_document,
)


_INSTANCE = "8b02bb6423e04815a961d54196a9bc42"


def _health(*, pipeline: bool = True, version: int = 1, ready: bool = True) -> dict[str, Any]:
    return {
        "protocol_version": version,
        "ready": ready,
        "service_instance_id": _INSTANCE,
        "capabilities": {"pipeline": {"available": pipeline}},
    }


@dataclass
class _Record:
    pid: int
    parent_pid: int
    image_path: str
    creation_time_100ns: int


class _FakeProcess:
    def __init__(self, pid: int = 501):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None):
        del timeout
        self.returncode = 0
        return 0


class _EarlyExitProcess(_FakeProcess):
    def __init__(self, pid: int = 501, *, exit_on_poll: int = 4):
        super().__init__(pid)
        self.poll_count = 0
        self.exit_on_poll = exit_on_poll

    def poll(self):
        self.poll_count += 1
        if self.poll_count >= self.exit_on_poll:
            self.returncode = 0
        return self.returncode


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class _FakeSystem:
    def __init__(self, root_image: str = "", process: _FakeProcess | None = None):
        self.root_image = root_image
        self.process = process
        self.records: dict[int, _Record] = {}
        self.killed: list[dict[str, Any]] = []
        self.listener_pids: list[int] = []

    def process_identity(self, pid: int):
        record = self.records.get(pid)
        if record is None:
            return None
        return record.creation_time_100ns, record.image_path

    def process_table(self):
        return dict(self.records)

    def process_is_current(self, pid: int, creation_time_100ns: int):
        record = self.records.get(pid)
        return record is not None and record.creation_time_100ns == creation_time_100ns

    def listener_owners(self, _port: int):
        return list(self.listener_pids)

    def terminate_owned(self, records: list[dict[str, Any]]) -> bool:
        self.killed.extend(records)
        if self.process is not None:
            self.process.terminate()
        self.records.clear()
        return True


class _FalseCleanupSystem(_FakeSystem):
    def __init__(self, mode: str, root_image: str, process: _FakeProcess):
        super().__init__(root_image, process)
        self.mode = mode
        self.queryable = True

    def process_identity(self, pid: int):
        if not self.queryable:
            return None
        return super().process_identity(pid)

    def process_is_current(self, pid: int, creation_time_100ns: int):
        if not self.queryable:
            return None
        return super().process_is_current(pid, creation_time_100ns)

    def terminate_owned(self, records: list[dict[str, Any]]) -> bool:
        self.killed.extend(records)
        if self.mode == "gone":
            self.records.clear()
            if self.process is not None:
                self.process.terminate()
        elif self.mode == "unknown":
            self.queryable = False
            if self.process is not None:
                self.process.terminate()
        elif self.mode == "live":
            # The direct Popen handle will stop the root, but an exact child
            # identity remains live and must keep cleanup unconfirmed.
            self.records.pop(501, None)
        return False


class _HealthServer:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None, *, redirect: str = ""):
        self.authorization: list[str | None] = []
        self.hits = 0
        selected_body = body or _health()
        selected_status = status
        selected_redirect = redirect
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.hits += 1
                owner.authorization.append(self.headers.get("Authorization"))
                if selected_redirect:
                    self.send_response(302)
                    self.send_header("Location", selected_redirect)
                    self.end_headers()
                    return
                self.send_response(selected_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(selected_body).encode("utf-8"))

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class UniversalServiceTests(unittest.TestCase):
    def _helper(
        self,
        root: Path,
        *,
        url: str,
        token_file: Path,
        token_file_defaulted: bool = False,
        environ: dict[str, str] | None = None,
        system: _FakeSystem | None = None,
        popen=None,
    ) -> UniversalServiceSupervisor:
        uvc = root / "NachoBot-UniversalVC-Adapter"
        (uvc / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (uvc / ".venv" / "Scripts" / "python.exe").write_bytes(b"fixture")
        (uvc / "service_main.py").write_text("# fixture entry\n", encoding="utf-8")
        runtime = root / "runtime"
        return UniversalServiceSupervisor(
            url=url,
            token_file=token_file,
            token_file_defaulted=token_file_defaulted,
            runtime_dir=runtime,
            universal_root=uvc,
            system=system or _FakeSystem(),
            environ=environ or {},
            popen=popen or (lambda *_args, **_kwargs: _FakeProcess()),
            monotonic=time.monotonic,
            sleep=time.sleep,
        )

    def test_healthy_loopback_is_reused_and_default_token_is_published(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "runtime" / "universal-api.token.private"
                helper = self._helper(
                    root,
                    url=server.url,
                    token_file=token_file,
                    token_file_defaulted=True,
                    environ={"NACHOBOT_UNIVERSALVC_TOKEN": "shared-token"},
                    popen=lambda *_args, **_kwargs: self.fail("healthy service must not spawn"),
                )
                previous_family = [{
                    "pid": 501,
                    "parentPid": 0,
                    "imagePath": r"C:\\UniversalVC\\python.exe",
                    "creationTime100ns": "1000",
                }]
                helper._store(
                    "ready", "universal_api_service_ready", reused=False,
                    instance=_INSTANCE, family=previous_family,
                )
                status = helper.ensure_ready()
                self.assertEqual(status, {"state": "ready", "code": "universal_api_service_ready", "reused": True})
                self.assertEqual(server.authorization, ["Bearer shared-token"])
                self.assertEqual(token_file.read_text(encoding="ascii").strip(), "shared-token")
                receipt = json.loads(helper.receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["serviceInstanceId"], _INSTANCE)
                self.assertEqual(receipt["processFamily"], previous_family)
                self.assertEqual(helper._owned_family, [])
                self.assertNotIn("shared-token", helper.receipt_path.read_text(encoding="utf-8"))
        finally:
            server.close()

    def test_healthy_reuse_uses_matching_explicit_token_file(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "configured-token.private"
                token_file.write_text("explicit-token\n", encoding="ascii")
                helper = self._helper(
                    root,
                    url=server.url,
                    token_file=token_file,
                    environ={"NACHOBOT_UNIVERSALVC_TOKEN": "explicit-token"},
                    popen=lambda *_args, **_kwargs: self.fail("healthy service must not spawn"),
                )

                status = helper.ensure_ready()

                self.assertEqual(status, {
                    "state": "ready", "code": "universal_api_service_ready", "reused": True,
                })
                self.assertEqual(server.authorization, ["Bearer explicit-token"])
                self.assertEqual(token_file.read_text(encoding="ascii"), "explicit-token\n")
        finally:
            server.close()

    def test_explicit_token_mismatch_fails_before_healthy_probe_or_spawn(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "configured-token.private"
                token_file.write_text("explicit-token\n", encoding="ascii")
                helper = self._helper(
                    root,
                    url=server.url,
                    token_file=token_file,
                    environ={"NACHOBOT_UNIVERSALVC_TOKEN": "different-env-token"},
                    popen=lambda *_args, **_kwargs: self.fail("credential mismatch must not spawn"),
                )

                status = helper.ensure_ready()

                self.assertEqual(status["code"], "universal_api_token_mismatch")
                self.assertEqual(server.hits, 0)
                self.assertEqual(token_file.read_text(encoding="ascii"), "explicit-token\n")
        finally:
            server.close()

    def test_missing_explicit_token_fails_before_healthy_probe_or_creation(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "missing-token.private"
                helper = self._helper(
                    root,
                    url=server.url,
                    token_file=token_file,
                    environ={"NACHOBOT_UNIVERSALVC_TOKEN": "environment-token"},
                    popen=lambda *_args, **_kwargs: self.fail("missing explicit token must not spawn"),
                )

                status = helper.ensure_ready()

                self.assertEqual(status["code"], "universal_api_token_invalid")
                self.assertEqual(server.hits, 0)
                self.assertFalse(token_file.exists())
        finally:
            server.close()

    def test_explicit_token_change_during_health_probe_prevents_ready_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "configured-token.private"
            token_file.write_text("initial-token\n", encoding="ascii")
            helper = self._helper(
                root,
                url="http://127.0.0.1:8016",
                token_file=token_file,
                popen=lambda *_args, **_kwargs: self.fail("healthy endpoint must not spawn"),
            )

            def change_file_during_probe(token: str) -> dict[str, str]:
                self.assertEqual(token, "initial-token")
                token_file.write_text("replacement-token\n", encoding="ascii")
                return {"service_instance_id": _INSTANCE}

            helper._probe = change_file_during_probe

            status = helper.ensure_ready()

            self.assertEqual(status["code"], "universal_api_token_mismatch")
            self.assertEqual(token_file.read_text(encoding="ascii"), "replacement-token\n")

    def test_reuse_does_not_carry_receipt_family_from_a_different_instance(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "token.private"
                token_file.write_text("valid-token\n", encoding="ascii")
                helper = self._helper(root, url=server.url, token_file=token_file)
                helper._store(
                    "ready", "universal_api_service_ready", reused=False,
                    instance="previous-service-instance",
                    family=[{
                        "pid": 501,
                        "parentPid": 0,
                        "imagePath": r"C:\\Previous\\python.exe",
                        "creationTime100ns": "1000",
                    }],
                )

                status = helper.ensure_ready()

                self.assertEqual(status["state"], "ready")
                receipt = json.loads(helper.receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["serviceInstanceId"], _INSTANCE)
                self.assertEqual(receipt["processFamily"], [])
                self.assertEqual(helper._owned_family, [])
        finally:
            server.close()

    def test_unauthorized_listener_is_never_competed_with(self) -> None:
        server = _HealthServer(status=401)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "token.private"
                token_file.write_text("valid-token\n", encoding="ascii")
                helper = self._helper(
                    root, url=server.url, token_file=token_file,
                    popen=lambda *_args, **_kwargs: self.fail("401 must not start a competing listener"),
                )
                status = helper.ensure_ready()
                self.assertEqual(status["code"], "universal_api_auth_failed")
                self.assertEqual(server.hits, 1)
        finally:
            server.close()

    def test_health_requires_protocol_readiness_and_pipeline_capability(self) -> None:
        for document in (_health(version=2), _health(ready=False), _health(pipeline=False)):
            server = _HealthServer(body=document)
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    token_file = root / "token.private"
                    token_file.write_text("valid-token\n", encoding="ascii")
                    helper = self._helper(
                        root, url=server.url, token_file=token_file,
                        popen=lambda *_args, **_kwargs: self.fail("an incompatible listener must not be competed with"),
                    )
                    self.assertEqual(helper.ensure_ready()["code"], "universal_api_health_invalid")
            finally:
                server.close()

    def test_redirect_does_not_forward_bearer_or_follow_to_another_listener(self) -> None:
        destination = _HealthServer()
        redirect = _HealthServer(redirect=destination.url + "/v1/health")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "token.private"
                token_file.write_text("valid-token\n", encoding="ascii")
                helper = self._helper(
                    root, url=redirect.url, token_file=token_file,
                    popen=lambda *_args, **_kwargs: self.fail("redirect is an occupied listener"),
                )
                self.assertEqual(helper.ensure_ready()["state"], "unavailable")
                self.assertEqual(redirect.hits, 1)
                self.assertEqual(destination.hits, 0)
        finally:
            redirect.close()
            destination.close()

    def test_connection_refusal_spawns_exact_cli_with_private_env_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            expected_python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FakeSystem(expected_python, process)
            system.records = {
                501: _Record(501, 0, expected_python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            system.listener_pids = [502]
            calls = 0
            captured: dict[str, Any] = {}

            def probe(_token: str):
                nonlocal calls
                calls += 1
                if calls <= 2:
                    raise _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
                return {"service_instance_id": _INSTANCE}

            def popen(command, **kwargs):
                captured["command"] = list(command)
                captured.update(kwargs)
                return process

            token_file = root / "runtime" / "universal-api.token.private"
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                token_file_defaulted=True,
                environ={"NACHOBOT_CORE_HOST": "127.0.0.1", "NACHOBOT_CORE_PORT": "8000"},
                system=system, popen=popen,
            )
            helper._probe = probe
            status = helper.ensure_ready()
            self.assertEqual(status, {"state": "ready", "code": "universal_api_service_ready", "reused": False})
            command = captured["command"]
            self.assertEqual(command[0], expected_python)
            self.assertEqual(command[1], str((uvc / "service_main.py").resolve()))
            self.assertEqual(command[2], "--config")
            self.assertEqual(Path(command[3]).name, "universal-service.config.private.toml")
            self.assertNotIn(token_file.read_text(encoding="ascii").strip(), " ".join(command))
            self.assertIsNone(captured["stdin"])
            self.assertIsNone(captured["stdout"])
            self.assertIsNone(captured["stderr"])
            self.assertFalse(captured["shell"])
            self.assertEqual(captured["env"]["NACHOBOT_CORE_HOST"], "127.0.0.1")
            self.assertEqual(captured["env"]["NACHOBOT_CORE_PORT"], "8000")
            self.assertEqual(captured["env"]["NACHOBOT_UNIVERSALVC_PORT"], "8016")
            self.assertEqual(captured["env"]["NACHOBOT_UNIVERSALVC_TOKEN"], token_file.read_text(encoding="ascii").strip())
            self.assertEqual([item["pid"] for item in helper.last_receipt["processFamily"]], [502, 501])

    def test_windows_spawn_uses_breakaway_flag_and_never_retries_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected_flags = 0x00000010 | 0x01000000
            captured: list[int] = []

            def popen_ok(_command, **kwargs):
                captured.append(kwargs["creationflags"])
                return _FakeProcess()

            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=root / "token.private",
                popen=popen_ok,
            )
            windows_subprocess = SimpleNamespace(
                CREATE_NEW_CONSOLE=0x00000010,
                CREATE_BREAKAWAY_FROM_JOB=0x01000000,
                DEVNULL=subprocess.DEVNULL,
                SubprocessError=subprocess.SubprocessError,
            )
            with (
                patch.object(universal_service_module, "os", SimpleNamespace(name="nt")),
                patch.object(universal_service_module, "subprocess", windows_subprocess),
            ):
                helper._spawn(["python"], {})
            self.assertEqual(captured, [expected_flags])

            failed_calls: list[int] = []

            def reject_breakaway(_command, **kwargs):
                failed_calls.append(kwargs["creationflags"])
                raise OSError("breakaway is prohibited by an ancestor job")

            rejected = self._helper(
                root / "rejected", url="http://127.0.0.1:8016",
                token_file=root / "rejected" / "token.private", popen=reject_breakaway,
            )
            with (
                patch.object(universal_service_module, "os", SimpleNamespace(name="nt")),
                patch.object(universal_service_module, "subprocess", windows_subprocess),
            ):
                with self.assertRaisesRegex(UniversalServiceError, "universal_api_service_cli_unavailable"):
                    rejected._spawn(["python"], {})
            self.assertEqual(failed_calls, [expected_flags])

    def test_existing_invalid_service_token_fails_closed_but_valid_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            uvc.mkdir(parents=True, exist_ok=True)
            (uvc / "service.toml").write_text(
                '[server]\nhost="127.0.0.1"\nport=8016\ntoken="REPLACE_WITH_A_RANDOM_SECRET"\n',
                encoding="utf-8",
            )
            token_file = root / "runtime" / "universal-api.token.private"
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                token_file_defaulted=True, environ={"NACHOBOT_UNIVERSALVC_TOKEN": "override-token"},
            )
            helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            command, launch_env = helper._command()
            self.assertEqual(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"], "override-token")
            self.assertEqual(token_file.read_text(encoding="ascii").strip(), "override-token")
            self.assertEqual(command[1], str((uvc / "service_main.py").resolve()))

            no_override = self._helper(
                root / "other", url="http://127.0.0.1:8016",
                token_file=root / "other" / "runtime" / "universal-api.token.private",
                token_file_defaulted=True,
            )
            (no_override.universal_root / "service.toml").write_text(
                '[server]\nhost="127.0.0.1"\nport=8016\ntoken="CHANGE_ME"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(UniversalServiceError, "universal_api_token_invalid"):
                no_override._command()

    def test_route_mismatch_and_https_refusal_do_not_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            uvc.mkdir(parents=True, exist_ok=True)
            (uvc / "service.toml").write_text(
                '[server]\nhost="127.0.0.1"\nport=8017\ntoken="valid-token"\n', encoding="utf-8"
            )
            token_file = root / "runtime" / "universal-api.token.private"
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                token_file_defaulted=True,
                popen=lambda *_args, **_kwargs: self.fail("mismatched route must not launch"),
            )
            helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            self.assertEqual(helper.ensure_ready()["code"], "universal_api_route_mismatch")

            https_token_file = root / "https" / "https-token"
            https_token_file.parent.mkdir(parents=True)
            https_token_file.write_text("valid-token\n", encoding="ascii")
            https_helper = self._helper(
                root / "https", url="https://127.0.0.1:8016", token_file=https_token_file,
                environ={"NACHOBOT_UNIVERSALVC_TOKEN": "valid-token"},
                popen=lambda *_args, **_kwargs: self.fail("HTTPS refusal must not launch"),
            )
            https_helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            self.assertEqual(https_helper.ensure_ready()["code"], "universal_api_https_autostart_unsupported")

    def test_pid_reuse_with_same_image_cannot_rebind_the_spawned_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            expected_python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FakeSystem(expected_python, process)
            system.records = {501: _Record(501, 0, expected_python, 1000)}
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=root / "token.private",
                system=system,
            )
            helper._pin_root(process)
            self.assertEqual(helper._root_binding["creationTime100ns"], "1000")
            system.records = {501: _Record(501, 0, expected_python, 2000)}
            self.assertEqual(helper._capture_family(process), [])
            self.assertEqual(helper._root_binding["creationTime100ns"], "1000")

    def test_timeout_cleans_pinned_redirector_and_child_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FakeSystem(python, process)
            system.records = {
                501: _Record(501, 0, python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            token_file = root / "runtime" / "token.private"
            token_file.parent.mkdir(parents=True)
            token_file.write_text("valid-token\n", encoding="ascii")
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                system=system, popen=lambda *_args, **_kwargs: process,
            )
            helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            clock = _Clock()
            helper.monotonic = clock.monotonic
            helper.sleep = clock.sleep

            status = helper.ensure_ready()

            self.assertEqual(status["code"], "universal_api_start_timeout")
            self.assertTrue(helper.last_receipt["cleanupConfirmed"])
            self.assertEqual({item["pid"] for item in system.killed}, {501, 502})
            self.assertTrue(process.terminated)

    def test_cleanup_verifies_exact_family_gone_when_bounded_terminator_reports_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FalseCleanupSystem("gone", python, process)
            system.records = {
                501: _Record(501, 0, python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=root / "token.private",
                system=system,
            )
            helper._pin_root(process)

            confirmed, family = helper._cleanup_failed_start(process)

            self.assertTrue(confirmed)
            self.assertEqual({item["pid"] for item in family}, {501, 502})
            self.assertIsNone(system.process_identity(501))
            self.assertIsNone(system.process_identity(502))

    def test_cleanup_stays_unconfirmed_for_unknown_or_live_exact_identity(self) -> None:
        for mode in ("unknown", "live"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                uvc = root / "NachoBot-UniversalVC-Adapter"
                python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
                process = _FakeProcess()
                system = _FalseCleanupSystem(mode, python, process)
                system.records = {
                    501: _Record(501, 0, python, 1000),
                    502: _Record(502, 501, r"C:\Python\python.exe", 1001),
                }
                helper = self._helper(
                    root, url="http://127.0.0.1:8016", token_file=root / "token.private",
                    system=system,
                )
                helper._pin_root(process)

                confirmed, family = helper._cleanup_failed_start(process)

                self.assertFalse(confirmed)
                self.assertEqual({item["pid"] for item in family}, {501, 502})

    def test_early_redirector_exit_cleans_owned_child_before_reporting_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _EarlyExitProcess(exit_on_poll=4)
            system = _FakeSystem(python, process)
            system.records = {
                501: _Record(501, 0, python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            token_file = root / "token.private"
            token_file.write_text("valid-token\n", encoding="ascii")
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                system=system, popen=lambda *_args, **_kwargs: process,
            )
            helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            clock = _Clock()
            helper.monotonic = clock.monotonic
            helper.sleep = clock.sleep

            status = helper.ensure_ready()

            self.assertEqual(status["code"], "universal_api_start_process_exited")
            self.assertTrue(helper.last_receipt["cleanupConfirmed"])
            self.assertEqual({item["pid"] for item in system.killed}, {501, 502})

    def test_stop_checkpoint_during_launch_reaps_owned_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FakeSystem(python, process)
            system.records = {
                501: _Record(501, 0, python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            token_file = root / "token.private"
            token_file.write_text("valid-token\n", encoding="ascii")
            helper = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                system=system, popen=lambda *_args, **_kwargs: process,
            )
            helper._probe = lambda _token: (_ for _ in ()).throw(
                _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
            )
            clock = _Clock()
            helper.monotonic = clock.monotonic
            helper.sleep = clock.sleep
            checkpoints = 0

            def stop_during_launch():
                nonlocal checkpoints
                checkpoints += 1
                if checkpoints == 2:
                    raise RuntimeError("stop requested")

            status = helper.ensure_ready(checkpoint=stop_during_launch)

            self.assertEqual(status["code"], "universal_api_start_cancelled")
            self.assertTrue(helper.last_receipt["cleanupConfirmed"])
            self.assertEqual({item["pid"] for item in system.killed}, {501, 502})

    def test_concurrent_ensures_spawn_once_and_second_reuses_ready_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uvc = root / "NachoBot-UniversalVC-Adapter"
            python = str((uvc / ".venv" / "Scripts" / "python.exe").resolve())
            process = _FakeProcess()
            system = _FakeSystem(python, process)
            system.records = {
                501: _Record(501, 0, python, 1000),
                502: _Record(502, 501, r"C:\Python\python.exe", 1001),
            }
            system.listener_pids = [502]
            token_file = root / "token.private"
            token_file.write_text("valid-token\n", encoding="ascii")
            spawned = threading.Event()
            second_probe = threading.Event()
            spawn_calls: list[int] = []

            def probe_first(_token: str):
                if spawned.is_set():
                    return {"service_instance_id": _INSTANCE}
                raise _ProbeFailure("universal_api_service_unavailable", connection_refused=True)

            def popen_first(_command, **_kwargs):
                spawn_calls.append(1)
                spawned.set()
                if not second_probe.wait(2.0):
                    raise AssertionError("competing ensure did not recheck under the startup lock")
                return process

            second_calls = 0

            def probe_second(_token: str):
                nonlocal second_calls
                second_calls += 1
                if second_calls == 1:
                    if not spawned.wait(2.0):
                        raise AssertionError("first ensure did not acquire the startup lock")
                    raise _ProbeFailure("universal_api_service_unavailable", connection_refused=True)
                second_probe.set()
                return {"service_instance_id": _INSTANCE}

            first = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                system=system, popen=popen_first,
            )
            second = self._helper(
                root, url="http://127.0.0.1:8016", token_file=token_file,
                system=system,
            )
            first._probe = probe_first
            second._probe = probe_second
            outputs: dict[str, dict[str, Any]] = {}
            failures: list[BaseException] = []

            def run(name: str, helper: UniversalServiceSupervisor):
                try:
                    outputs[name] = helper.ensure_ready()
                except BaseException as exc:
                    failures.append(exc)

            worker1 = threading.Thread(target=run, args=("first", first), daemon=True)
            worker2 = threading.Thread(target=run, args=("second", second), daemon=True)
            worker1.start()
            worker2.start()
            worker1.join(timeout=4.0)
            worker2.join(timeout=4.0)

            self.assertFalse(worker1.is_alive())
            self.assertFalse(worker2.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(spawn_calls), 1)
            self.assertEqual(outputs["first"]["state"], "ready")
            self.assertEqual(outputs["second"], {
                "state": "ready", "code": "universal_api_service_ready", "reused": True,
            })

    def test_default_port_is_supported_for_loopback_root_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / "token.private"
            token_file.write_text("valid-token\n", encoding="ascii")
            helper = self._helper(root, url="http://127.0.0.1", token_file=token_file)

            command, launch_env = helper._command()

            self.assertEqual(launch_env["NACHOBOT_UNIVERSALVC_PORT"], "80")
            self.assertEqual(command[2], "--config")
            observed_ports: list[int] = []
            helper.system.listener_owners = lambda port: observed_ports.append(port) or [88]
            self.assertTrue(helper._listener_belongs_to([{"pid": 88}]))
            self.assertEqual(observed_ports, [80])

    def test_real_loopback_probe_is_bounded_and_uses_bearer_header(self) -> None:
        server = _HealthServer()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                token_file = root / "token.private"
                token_file.write_text("valid-token\n", encoding="ascii")
                helper = self._helper(root, url=server.url, token_file=token_file)
                start = time.monotonic()
                status = helper.ensure_ready()
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertEqual(status["state"], "ready")
                self.assertEqual(server.authorization, ["Bearer valid-token"])
        finally:
            server.close()

    def test_delayed_local_connection_refusal_is_still_recognized_as_refusal(self) -> None:
        timeouts: list[float] = []

        class DelayedRefusalOpener:
            def open(self, _request, *, timeout: float):
                timeouts.append(timeout)
                # Model Windows loopback behavior where the OS does not
                # resolve an unused-port connect until roughly two seconds.
                if timeout < 2.1:
                    raise urllib.error.URLError(TimeoutError("connect still pending"))
                raise urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))

        with patch("service.universal_service.urllib.request.build_opener", return_value=DelayedRefusalOpener()):
            with self.assertRaises(_ProbeFailure) as caught:
                _health_document("http://127.0.0.1:65534", "valid-token")

        self.assertGreaterEqual(_HEALTH_TIMEOUT_SECONDS, 2.1)
        self.assertEqual(timeouts, [_HEALTH_TIMEOUT_SECONDS])
        self.assertTrue(caught.exception.connection_refused)


if __name__ == "__main__":
    unittest.main()
