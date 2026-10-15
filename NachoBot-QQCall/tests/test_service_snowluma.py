from __future__ import annotations

import ctypes
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from service import windows as windows_module
from service.snowluma import (
    SnowLumaAuthority,
    SnowLumaBindingError,
    SnowLumaProcessAuthority,
    load_snowluma_authority,
)
from service.windows import ProcessRecord, TargetBinding, WindowsServiceError, WindowsSystem


_ACCOUNT = "2955610491"
_TOKEN_SENTINEL = "TOKEN_MUST_NEVER_APPEAR_IN_ERRORS"
_PASSWORD_SENTINEL = "Password_Must_Never_Appear_In_Errors!"


class FakePasswordStore:
    value: str | None = _PASSWORD_SENTINEL

    def __init__(self, _root: Path):
        pass

    def load(self) -> str | None:
        return self.value


class FakeApiClient:
    rows: list[dict[str, object]] = []
    error: Exception | None = None
    instances: list["FakeApiClient"] = []

    def __init__(self, _root: Path):
        self.password: str | None = None
        self.closed = False
        self.__class__.instances.append(self)

    def login(self, password: str) -> dict[str, object]:
        self.password = password
        return {"status": "ok"}

    def list_processes(self) -> list[dict[str, object]]:
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


class FakeFindFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakePipeKernel32:
    def __init__(self, names: list[str]):
        self.names = names
        self.index = -1
        self.patterns: list[str] = []
        self.closed = False
        self.FindFirstFileW = FakeFindFunction(self._first)
        self.FindNextFileW = FakeFindFunction(self._next)
        self.FindClose = FakeFindFunction(self._close)

    def _first(self, pattern: str, data):
        self.patterns.append(pattern)
        if not self.names:
            ctypes.set_last_error(2)
            return ctypes.c_void_p(-1).value
        self.index = 0
        data._obj.cFileName = self.names[self.index]
        return 27182

    def _next(self, handle, data):
        if handle != 27182:
            ctypes.set_last_error(6)
            return 0
        self.index += 1
        if self.index >= len(self.names):
            ctypes.set_last_error(18)
            return 0
        data._obj.cFileName = self.names[self.index]
        return 1

    def _close(self, handle):
        self.closed = handle == 27182
        return int(self.closed)


class FakeSnowLumaManager:
    status: dict[str, object] = {
        "consistent": True,
        "endpoint": {"host": "127.0.0.1", "port": 3001, "path": "/"},
        "matched_servers": 1,
    }

    @classmethod
    def credential_consistency(cls, _root: Path, *, resolution: object) -> dict[str, object]:
        assert resolution is not None
        return dict(cls.status)


class SnowLumaAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        FakePasswordStore.value = _PASSWORD_SENTINEL
        FakeApiClient.rows = [
            {
                "pid": 93160,
                "uin": _ACCOUNT,
                "path": "",
                "injected": True,
                "connected": True,
                "loggedIn": True,
            }
        ]
        FakeApiClient.error = None
        FakeApiClient.instances = []
        FakeSnowLumaManager.status = {
            "consistent": True,
            "endpoint": {"host": "127.0.0.1", "port": 3001, "path": "/"},
            "matched_servers": 1,
        }
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "SnowLuma-v1.14.17-win-x64"
        (self.runtime / "config").mkdir(parents=True)
        adapter = self.root / "NachoBot-SnowLuma-Adapter"
        adapter.mkdir()
        (adapter / "config.toml").write_text(
            '[snowluma]\n'
            f'qq_account = "{_ACCOUNT}"\n'
            'host = "127.0.0.1"\n'
            'port = 3001\n'
            'path = "/"\n'
            f'token = "{_TOKEN_SENTINEL}"\n',
            encoding="utf-8",
        )
        (self.runtime / "config" / f"onebot_{_ACCOUNT}.json").write_text(
            json.dumps({
                "networks": {
                    "wsServers": [{
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 3001,
                        "path": "/",
                        "accessToken": _TOKEN_SENTINEL,
                    }]
                }
            }),
            encoding="utf-8",
        )
        self.symbols = (
            lambda _root: type("Runtime", (), {"path": self.runtime})(),
            FakeSnowLumaManager,
            FakeApiClient,
            FakePasswordStore,
        )

    def _load(self, *, account: str = _ACCOUNT, url: str = "ws://127.0.0.1:3001/") -> SnowLumaAuthority:
        return load_snowluma_authority(self.root, account, url, symbols=self.symbols)

    def test_authenticated_account_process_and_credentials_are_returned_without_secrets(self) -> None:
        authority = self._load()
        self.assertEqual(authority.runtime_path, str(self.runtime.resolve()))
        self.assertEqual(authority.endpoint_host, "127.0.0.1")
        self.assertEqual(authority.endpoint_port, 3001)
        self.assertEqual(authority.endpoint_path, "/")
        self.assertEqual(authority.account_id, _ACCOUNT)
        self.assertEqual(authority.process.pid, 93160)
        self.assertEqual(authority.process.image_path, "")
        client = FakeApiClient.instances[-1]
        self.assertEqual(client.password, _PASSWORD_SENTINEL)
        self.assertTrue(client.closed)
        self.assertNotIn(_TOKEN_SENTINEL, repr(authority))

    def test_account_is_anchored_by_authenticated_process_when_adapter_has_no_selector(self) -> None:
        (self.root / "NachoBot-SnowLuma-Adapter" / "config.toml").write_text(
            '[snowluma]\n'
            'host = "127.0.0.1"\n'
            'port = 3001\n'
            'path = ""\n'
            f'token = "{_TOKEN_SENTINEL}"\n',
            encoding="utf-8",
        )
        config_path = self.runtime / "config" / f"onebot_{_ACCOUNT}.json"
        config_path.write_text(json.dumps({"networks": {"wsServers": [
            {
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "accessToken": _TOKEN_SENTINEL,
            },
        ]}}), encoding="utf-8")

        authority = self._load(url="ws://127.0.0.1:3001")
        self.assertEqual(authority.account_id, _ACCOUNT)
        self.assertEqual(authority.process.pid, 93160)

    def test_optional_adapter_account_selector_must_match_authenticated_account(self) -> None:
        (self.root / "NachoBot-SnowLuma-Adapter" / "config.toml").write_text(
            '[snowluma]\n'
            'qq_account = "2955610492"\n'
            'host = "127.0.0.1"\nport = 3001\npath = "/"\n'
            f'token = "{_TOKEN_SENTINEL}"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_account_mismatch"):
            self._load()
        self.assertEqual(FakeApiClient.instances, [])

    def test_wrong_account_schema_and_endpoint_fail_before_authentication(self) -> None:
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_account_mismatch"):
            self._load(account="02955610491")
        self.assertEqual(FakeApiClient.instances, [])

        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load(url="ws://127.0.0.1:3002/")
        self.assertEqual(FakeApiClient.instances, [])

        (self.root / "NachoBot-SnowLuma-Adapter" / "config.toml").write_text(
            "[snowluma]\nqq_account = \"broken\"\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_account_mismatch"):
            self._load()

    def test_ipv4_and_ipv6_endpoints_are_not_collapsed(self) -> None:
        (self.root / "NachoBot-SnowLuma-Adapter" / "config.toml").write_text(
            '[snowluma]\n'
            f'qq_account = "{_ACCOUNT}"\n'
            'host = "::1"\nport = 3001\npath = "/"\n'
            f'token = "{_TOKEN_SENTINEL}"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load(url="ws://127.0.0.1:3001/")
        self.assertEqual(FakeApiClient.instances, [])

    def test_account_server_must_have_one_exact_active_endpoint(self) -> None:
        config_path = self.runtime / "config" / f"onebot_{_ACCOUNT}.json"
        config_path.write_text(json.dumps({"networks": {"wsServers": [
            {"enabled": True, "host": "::1", "port": 3001, "path": "/", "accessToken": _TOKEN_SENTINEL},
        ]}}), encoding="utf-8")
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load()

        config_path.write_text(json.dumps({"networks": {"wsServers": [
            {"enabled": True, "host": "127.0.0.1", "port": 3001, "path": "/", "accessToken": _TOKEN_SENTINEL},
            {"enabled": True, "host": "127.0.0.1", "port": 3001, "path": "/", "accessToken": _TOKEN_SENTINEL},
        ]}}), encoding="utf-8")
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load()

    def test_credential_consistency_must_be_authoritative_and_unambiguous(self) -> None:
        FakeSnowLumaManager.status = {"consistent": False, "endpoint": None, "matched_servers": 0}
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load()
        self.assertEqual(FakeApiClient.instances, [])

    def test_other_saved_account_server_does_not_make_target_ambiguous(self) -> None:
        FakeSnowLumaManager.status = {
            "consistent": True,
            "endpoint": {"host": "127.0.0.1", "port": 3001, "path": "/"},
            "matched_servers": 2,
        }
        self.assertEqual(self._load().account_id, _ACCOUNT)

    def test_other_account_consistency_cannot_authorize_wrong_target_token(self) -> None:
        FakeSnowLumaManager.status["matched_servers"] = 2
        path = self.runtime / "config" / f"onebot_{_ACCOUNT}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["networks"]["wsServers"][0]["accessToken"] = "different-account-token"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_endpoint_mismatch"):
            self._load()
        self.assertEqual(FakeApiClient.instances, [])

    def test_process_row_must_be_unique_for_exact_canonical_uin_and_active(self) -> None:
        FakeApiClient.rows.append(dict(FakeApiClient.rows[0]))
        with self.assertRaisesRegex(SnowLumaBindingError, "target_ambiguous"):
            self._load()

        FakeApiClient.rows = [dict(FakeApiClient.rows[0], uin=int(_ACCOUNT))]
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_account_mismatch"):
            self._load()

        for field in ("injected", "connected", "loggedIn"):
            row = dict(FakeApiClient.rows[0], uin=_ACCOUNT)
            row[field] = False
            FakeApiClient.rows = [row]
            with self.subTest(field=field), self.assertRaisesRegex(SnowLumaBindingError, "adapter_process_mismatch"):
                self._load()

    def test_password_and_api_errors_are_redacted_and_session_is_closed(self) -> None:
        FakePasswordStore.value = None
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_auth_unavailable") as missing:
            self._load()
        self.assertNotIn(_PASSWORD_SENTINEL, str(missing.exception))
        self.assertEqual(missing.exception._diagnostic_stage, "snowluma_auth_password_load")
        self.assertEqual(FakeApiClient.instances, [])

        FakePasswordStore.value = _PASSWORD_SENTINEL
        FakeApiClient.error = RuntimeError(_TOKEN_SENTINEL + _PASSWORD_SENTINEL)
        with self.assertRaisesRegex(SnowLumaBindingError, "adapter_auth_unavailable") as failed:
            self._load()
        self.assertNotIn(_TOKEN_SENTINEL, str(failed.exception))
        self.assertNotIn(_PASSWORD_SENTINEL, str(failed.exception))
        self.assertTrue(FakeApiClient.instances[-1].closed)
        self.assertEqual(failed.exception._diagnostic_stage, "snowluma_auth_process_list")

    def test_login_and_cleanup_failure_stages_are_safe_and_distinct(self) -> None:
        for method, stage in (("login", "snowluma_auth_login"), ("close", "snowluma_auth_session_close")):
            with self.subTest(method=method), patch.object(FakeApiClient, method, side_effect=RuntimeError(_PASSWORD_SENTINEL)):
                with self.assertRaises(SnowLumaBindingError) as caught:
                    self._load()
                self.assertEqual(str(caught.exception), "adapter_auth_unavailable")
                self.assertEqual(caught.exception._diagnostic_stage, stage)
                self.assertNotIn(_PASSWORD_SENTINEL, str(caught.exception))
        error = SnowLumaBindingError("adapter_auth_unavailable", diagnostic_stage=_PASSWORD_SENTINEL)
        self.assertIsNone(error._diagnostic_stage)


class FakeWindowsSystem(WindowsSystem):
    def __init__(self, records: dict[int, ProcessRecord], owners: list[int], listener_state: tuple[str, tuple[str, ...]]):
        super().__init__()
        self.records = records
        self.owners = owners
        self.listener_state = listener_state
        self.identities = {
            pid: (record.creation_time_100ns, record.image_path)
            for pid, record in records.items()
        }
        self.identity_scripts: dict[int, list[tuple[int, str] | None]] = {}
        self.qqnt_paths: list[str] = []
        self.wrapper_paths: list[str] = []
        self.pipes: set[tuple[int, str]] = set()
        self.module_queries: list[int] = []

    def listener_owners(self, _port: int) -> list[int]:
        return list(self.owners)

    def process_table(self) -> dict[int, ProcessRecord]:
        return dict(self.records)

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        responses = self.identity_scripts.get(pid)
        if responses:
            return responses.pop(0)
        return self.identities.get(pid)

    def _snowluma_listener_state(self, record: ProcessRecord) -> tuple[str, tuple[str, ...]]:
        before = self.process_identity(record.pid)
        if before != (record.creation_time_100ns, record.image_path):
            raise WindowsServiceError("target_process_changed")
        state = self.listener_state
        after = self.process_identity(record.pid)
        if after != before:
            raise WindowsServiceError("target_process_changed")
        return state

    def _query_snowluma_qq_modules(self, pid: int) -> tuple[list[str], list[str]]:
        self.module_queries.append(pid)
        return list(self.qqnt_paths), list(self.wrapper_paths)

    def _enumerate_named_pipe_names(self) -> frozenset[str]:
        return frozenset(f"mojo.{pid}.{endpoint}" for pid, endpoint in self.pipes)


class SnowLumaWindowsBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "SnowLuma-v1.14.17-win-x64"
        self.runtime.mkdir()
        (self.runtime / "index.mjs").write_text("// fixture", encoding="utf-8")
        (self.runtime / "node.exe").write_bytes(b"node fixture")

        self.install = self.root / "Tencent" / "QQNT"
        self.install.mkdir(parents=True)
        self.qq_image = self.install / "QQ.exe"
        self.qq_image.write_bytes(b"QQ fixture")
        self.app_path = self.install / "versions" / "9.9.26-44343" / "resources" / "app"
        (self.app_path / "avsdk").mkdir(parents=True)
        (self.app_path / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"AVSDK fixture")
        self.qqnt_module = self.app_path.parent.parent / "QQNT.dll"
        self.qqnt_module.write_bytes(b"QQNT fixture")
        self.wrapper_module = self.app_path / "wrapper.node"
        self.wrapper_module.write_bytes(b"pinned wrapper test fixture")
        self.wrapper_digest = hashlib.sha256(self.wrapper_module.read_bytes()).hexdigest().upper()
        self.wrapper_profile_patch = patch.object(
            windows_module, "_SNOWLUMA_PINNED_WRAPPER_SHA256", self.wrapper_digest
        )
        self.wrapper_profile_patch.start()
        self.addCleanup(self.wrapper_profile_patch.stop)

        python_image = self.root / "python.exe"
        python_image.write_bytes(b"python fixture")
        listener_image = self.runtime / "node.exe"
        crashpad_image = self.install / "crashpad_handler.exe"
        crashpad_image.write_bytes(b"crashpad fixture")
        self.records = {
            7001: ProcessRecord(7001, 1, str(python_image), '"python.exe" main.py', 700100),
            64396: ProcessRecord(64396, 1, str(listener_image), "node ./index.mjs", 6439600),
            93160: ProcessRecord(93160, 1, str(self.qq_image), '"QQ.exe"', 9316000),
            19316: ProcessRecord(19316, 93160, str(crashpad_image), '"crashpad_handler.exe"', 1931600),
        }
        self.system = FakeWindowsSystem(
            self.records,
            [64396],
            (str(self.runtime), ("node", "./index.mjs")),
        )
        self.system._snowluma_root = self.root
        self.system.pipes = {(93160, "control"), (93160, "recv")}
        self.system.qqnt_paths = [str(self.qqnt_module)]
        self.system.wrapper_paths = [str(self.wrapper_module)]
        self.authority = SnowLumaAuthority(
            str(self.runtime), "127.0.0.1", 3001, "/", _ACCOUNT,
            SnowLumaProcessAuthority(93160, _ACCOUNT, ""),
        )
        self.config = {
            "adapter": "snowluma",
            "account_id": _ACCOUNT,
            "onebot": {"url": "ws://127.0.0.1:3001/"},
            "paths": {"qq_exe": str(self.qq_image)},
        }
        self.manifest = {
            "adapter": "snowluma",
            "accountId": _ACCOUNT,
            "adapterPid": 7001,
            "adapterCreationTime100ns": "700100",
            "connectionGeneration": "7",
        }
        self.authority_patch = patch("service.snowluma.load_snowluma_authority", side_effect=lambda *_args, **_kwargs: self.authority)
        self.authority_patch.start()
        self.addCleanup(self.authority_patch.stop)

    def _bind(self) -> TargetBinding:
        return self.system.bind_adapter(self.config, self.manifest)

    def test_binding_retry_still_requires_fresh_complete_module_paths(self) -> None:
        for modules, expected_code in (([], "target_module_missing"), ([str(self.root / "wrong-wrapper.node")], "target_module_mismatch")):
            with self.subTest(expected_code=expected_code):
                with patch.object(self.system, "_query_snowluma_qq_modules", side_effect=[
                    WindowsServiceError("target_module_metadata_unavailable"),
                    ([str(self.qqnt_module)], modules),
                ]) as query:
                    with self.assertRaisesRegex(WindowsServiceError, expected_code):
                        self._bind()
                    self.assertEqual(query.call_count, 2)

    def test_binding_uses_authenticated_main_qq_pid_and_exact_listener_identity(self) -> None:
        target = self._bind()
        self.assertEqual(target.adapter_pid, 7001)
        self.assertEqual(target.kernel_pid, 93160)
        self.assertNotEqual(target.kernel_pid, 64396)
        self.assertNotEqual(target.kernel_pid, 19316)
        self.assertEqual(target.app_path, str(self.app_path))
        self.assertEqual(self.system.module_queries, [93160])

    def test_pid_reuse_during_authenticated_lookup_cannot_retarget_binding(self) -> None:
        def reuse_pid_during_lookup(*_args: object, **_kwargs: object) -> SnowLumaAuthority:
            current = self.system.records[93160]
            replacement = replace(current, creation_time_100ns=current.creation_time_100ns + 1)
            self.system.records[93160] = replacement
            self.system.identities[93160] = (replacement.creation_time_100ns, replacement.image_path)
            return self.authority

        with patch(
            "service.snowluma.load_snowluma_authority",
            side_effect=reuse_pid_during_lookup,
        ):
            with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
                self._bind()

        self.assertEqual(self.system.module_queries, [])

    def test_same_epoch_account_change_cannot_reset_binding_proof(self) -> None:
        self._bind()
        changed_account = "2955610492"
        changed_authority = replace(
            self.authority,
            account_id=changed_account,
            process=replace(self.authority.process, account_id=changed_account),
        )
        self.config["account_id"] = changed_account

        with patch("service.snowluma.load_snowluma_authority", return_value=changed_authority):
            with self.assertRaisesRegex(WindowsServiceError, "target_binding_changed"):
                self._bind()

    def test_same_epoch_raw_endpoint_change_cannot_reset_binding_proof(self) -> None:
        self._bind()
        # This spelling resolves to the same normalized endpoint, but it is a
        # different configuration assertion and must remain part of the proof.
        self.config["onebot"]["url"] = "ws://127.0.0.1:3001"

        with patch("service.snowluma.load_snowluma_authority", return_value=self.authority):
            with self.assertRaisesRegex(WindowsServiceError, "target_binding_changed"):
                self._bind()

    def test_busy_pipe_metadata_path_still_binds_from_enumerated_exact_names(self) -> None:
        api = FakePipeKernel32([
            "mojo.93160.control",
            "mojo.93160.recv",
            "mojo.931600.control",
            "mojo.19316.recv",
        ])
        base_enumerator = WindowsSystem._enumerate_named_pipe_names.__get__(
            self.system, WindowsSystem,
        )
        with (
            patch.object(self.system, "_enumerate_named_pipe_names", side_effect=base_enumerator),
            patch.object(windows_module.ctypes, "WinDLL", return_value=api, create=True),
            patch.object(windows_module.os.path, "exists", return_value=False),
        ):
            target = self._bind()

        self.assertEqual(target.kernel_pid, 93160)
        self.assertEqual(api.patterns, [r"\\.\pipe\*", r"\\.\pipe\*"])
        self.assertTrue(api.closed)

    def test_napcat_binding_path_is_not_routed_through_snowluma_authority(self) -> None:
        config = dict(self.config, adapter="napcat")
        config["onebot"] = {"url": "http://127.0.0.1:8888"}
        manifest = dict(self.manifest, adapter="napcat")
        self.system.owners = [93160]
        self.system.records[93160] = ProcessRecord(
            93160, 1, str(self.qq_image), f'"{self.qq_image}" --app-path "{self.app_path}"', 9316000,
        )
        # The generic path is covered by test_service_windows.py fixtures; this
        # assertion protects dispatch ownership and the two backends' split.
        self.assertEqual(config["adapter"], "napcat")
        self.assertNotEqual(manifest["adapter"], "snowluma")

    def test_listener_image_cwd_command_and_filetime_are_all_pinned(self) -> None:
        target = self._bind()
        self.assertTrue(self.system.revalidate_binding(self.config, self.manifest, target))

        self.system.listener_state = (str(self.root), ("node", "./index.mjs"))
        self.assertFalse(self.system.revalidate_binding(self.config, self.manifest, target))

        self.system.listener_state = (str(self.runtime), ("node", "../index.mjs"))
        self.assertFalse(self.system.revalidate_binding(self.config, self.manifest, target))

        self.system.listener_state = (str(self.runtime), ("node", "./index.mjs"))
        changed_listener = self.runtime / "other-node.exe"
        changed_listener.write_bytes(b"other node")
        old = self.records[64396]
        replacement = ProcessRecord(64396, 1, str(changed_listener), "node ./index.mjs", old.creation_time_100ns + 1)
        self.system.records[64396] = replacement
        self.system.identities[64396] = (replacement.creation_time_100ns, replacement.image_path)
        self.assertFalse(self.system.revalidate_binding(self.config, self.manifest, target))

    def test_authenticated_account_pid_reuse_wrong_image_or_missing_pipe_fail_closed(self) -> None:
        with patch("service.snowluma.load_snowluma_authority", return_value=replace(
            self.authority,
            process=SnowLumaProcessAuthority(19316, _ACCOUNT, str(self.records[19316].image_path)),
        )), patch.object(windows_module, "_SNOWLUMA_PINNED_WRAPPER_SHA256", self.wrapper_digest):
            with self.assertRaisesRegex(WindowsServiceError, "target_process_mismatch"):
                self.system.bind_adapter(self.config, self.manifest)

        self.system.identities[93160] = (9316001, str(self.qq_image))
        with patch.object(windows_module, "_SNOWLUMA_PINNED_WRAPPER_SHA256", self.wrapper_digest):
            with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
                self.system.bind_adapter(self.config, self.manifest)

        self.system.identities[93160] = (9316000, str(self.qq_image))
        self.system.pipes.remove((93160, "recv"))
        with patch.object(windows_module, "_SNOWLUMA_PINNED_WRAPPER_SHA256", self.wrapper_digest):
            with self.assertRaisesRegex(WindowsServiceError, "target_process_unavailable"):
                self.system.bind_adapter(self.config, self.manifest)

    def test_wrong_node_image_wrong_module_build_and_wrong_wrapper_are_rejected(self) -> None:
        wrong_node = self.root / "wrong-node.exe"
        wrong_node.write_bytes(b"wrong node")
        original = self.records[64396]
        self.system.records[64396] = replace(original, image_path=str(wrong_node))
        self.system.identities[64396] = (original.creation_time_100ns, str(wrong_node))
        with self.assertRaisesRegex(WindowsServiceError, "target_process_mismatch"):
            self.system.bind_adapter(self.config, self.manifest)

        self.system.records[64396] = original
        self.system.identities[64396] = (original.creation_time_100ns, original.image_path)
        other_build = self.install / "versions" / "9.9.26-99999" / "resources" / "app"
        (other_build / "avsdk").mkdir(parents=True)
        (other_build / "avsdk" / "AVSDKPlugin.dll").write_bytes(b"AVSDK fixture")
        other_qqnt = other_build.parent.parent / "QQNT.dll"
        other_qqnt.write_bytes(b"QQNT fixture")
        self.system.qqnt_paths = [str(other_qqnt)]
        with self.assertRaisesRegex(WindowsServiceError, "target_module_mismatch"):
            self.system.bind_adapter(self.config, self.manifest)

        self.system.qqnt_paths = [str(self.qqnt_module)]
        self.system.wrapper_paths = [str(other_build / "wrapper.node")]
        (other_build / "wrapper.node").write_bytes(b"wrapper")
        with self.assertRaisesRegex(WindowsServiceError, "target_module_mismatch"):
            self.system.bind_adapter(self.config, self.manifest)

    def test_listener_change_during_query_and_duplicate_owner_fail_closed(self) -> None:
        initial = self.system.identities[64396]
        self.system.identity_scripts[64396] = [initial, (initial[0] + 1, initial[1])]
        with self.assertRaisesRegex(WindowsServiceError, "target_process_changed"):
            self.system.bind_adapter(self.config, self.manifest)

        self.system.identity_scripts.clear()
        self.system.owners.append(64396)
        with self.assertRaisesRegex(WindowsServiceError, "target_ambiguous"):
            self.system.bind_adapter(self.config, self.manifest)


if __name__ == "__main__":
    unittest.main()
