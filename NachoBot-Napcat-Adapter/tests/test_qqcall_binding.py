from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-Napcat-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

import src.qqcall_runtime as qqcall_runtime  # noqa: E402
from src.config.official_configs import QQCallAdapterConfig  # noqa: E402
from src.qqcall_runtime import create_napcat_qqcall_runtime  # noqa: E402
from src.qqcall_runtime import clear_qqcall_runtime  # noqa: E402
from qqcall_client.qqcall import QQCallConfig  # noqa: E402
from qqcall_client import qqcall_service  # noqa: E402


def _toml_value(value):
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"
    raise TypeError(type(value).__name__)


def _write_toml(path: Path, document: dict) -> None:
    lines = []

    def write_table(value, name):
        if name:
            if lines:
                lines.append("")
            lines.append(f"[{name}]")
        for key, item in value.items():
            if not isinstance(item, dict):
                lines.append(f"{key} = {_toml_value(item)}")
        for key, item in value.items():
            if isinstance(item, dict):
                write_table(item, f"{name}.{key}" if name else key)

    for key, item in document.items():
        if not isinstance(item, dict):
            lines.append(f"{key} = {_toml_value(item)}")
    for key, item in document.items():
        if isinstance(item, dict):
            write_table(item, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestNapcatQQCallBinding(unittest.TestCase):
    def test_private_profile_uses_and_rechecks_current_napcat_connection(self):
        async def scenario():
            connection = SimpleNamespace(closed=False)
            handler = SimpleNamespace(server_connection=connection, connection_generation=7, verified_account_id="123456789")
            change = [False]
            async def lookup(action, params, **kwargs):
                self.assertEqual(action, "get_stranger_info")
                self.assertEqual(params, {"user_id": 34567})
                self.assertIs(kwargs["expected_connection"], connection)
                self.assertTrue(await kwargs["pre_send_check"]())
                self.assertTrue(kwargs["identity_check"]())
                if change[0]:
                    handler.connection_generation += 1
                return {"status": "ok", "retcode": 0, "data": {"user_id": 34567, "nickname": "真实昵称"}}
            module = ModuleType("src.send_handler.nc_sending")
            module.nc_message_sender = SimpleNamespace(send_message_to_napcat=lookup)
            with patch.dict(sys.modules, {"src.send_handler.nc_sending": module}):
                runtime = create_napcat_qqcall_runtime({"account_id": "123456789"}, router=SimpleNamespace(), message_handler=handler)
                self.assertEqual((await runtime._voice._resolve_peer_info("34567"))["nickname"], "真实昵称")
                change[0] = True
                self.assertIsNone(await runtime._voice._resolve_peer_info("34567"))
        asyncio.run(scenario())

    def test_adapter_config_has_no_local_switch_or_path(self):
        config = QQCallAdapterConfig.from_dict({"retired": object()})
        runtime = QQCallConfig()
        self.assertFalse(hasattr(config, "enabled"))
        self.assertFalse(hasattr(config, "_config_path"))
        with patch(
            "src.config.official_configs.load_qqcall_adapter_settings",
            return_value=SimpleNamespace(config=runtime),
        ) as load_settings:
            self.assertIs(config.to_runtime_config(), runtime)
        load_settings.assert_called_once_with("napcat", "", {})

    def test_malformed_legacy_table_cannot_block_canonical_switch(self):
        config_module = importlib.import_module("src.config.config")
        template = (ADAPTER_ROOT / "template" / "template_config.toml").read_text(encoding="utf-8")
        example = copy.deepcopy(qqcall_service._fixed_validator().DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            service_root = Path(temporary) / "NachoBot-QQCall"
            service_root.mkdir()
            canonical_path = Path(temporary) / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
            with patch.object(qqcall_service, "_SERVICE_PROJECT_ROOT", service_root):
                for old_qqcall, enabled in (
                    ("qqcall = []", False),
                    ('qqcall = { retired = ["ignored"] }', True),
                ):
                    public = copy.deepcopy(example)
                    public["plugin"]["enabled"] = enabled
                    qqcall = public["qqcall"]
                    # Device names are optional overrides; the root switch
                    # also enables adapter-side audio/heartbeat state.
                    qqcall.pop("audio", None)
                    if enabled:
                        qqcall["account_id"] = "12345"
                        qqcall["onebot"]["url"] = "http://127.0.0.1:3001"
                        qqcall["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
                    _write_toml(canonical_path, public)
                    adapter_path = Path(temporary) / f"adapter-{enabled}.toml"
                    adapter_path.write_text(old_qqcall + "\n" + template, encoding="utf-8")

                    config = config_module.load_config(str(adapter_path))
                    runtime = config.qqcall.to_runtime_config()
                    self.assertIs(runtime.enabled, enabled)
                    self.assertIs(runtime.audio["enabled"], enabled)
                    if not enabled:
                        self.assertFalse((service_root / ".runtime").exists())

    def test_optional_canonical_audio_overrides_accept_omission_and_partial_values(self):
        config_module = importlib.import_module("src.config.config")
        template = (ADAPTER_ROOT / "template" / "template_config.toml").read_text(encoding="utf-8")
        example = copy.deepcopy(qqcall_service._fixed_validator().DEFAULT_CONFIG)
        cases = (
            None,
            {},
            {"capture_device": "Verified loopback override"},
            {"playback_device": "Verified virtual playback override"},
            {"capture_device": "", "playback_device": ""},
        )

        for audio_overrides in cases:
            with self.subTest(audio_overrides=audio_overrides), tempfile.TemporaryDirectory() as temporary:
                service_root = Path(temporary) / "NachoBot-QQCall"
                service_root.mkdir()
                canonical_path = Path(temporary) / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
                public = copy.deepcopy(example)
                public["plugin"]["enabled"] = True
                qqcall = public["qqcall"]
                qqcall["account_id"] = "12345"
                qqcall["onebot"]["url"] = "http://127.0.0.1:3001"
                qqcall["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
                if audio_overrides is None:
                    qqcall.pop("audio", None)
                else:
                    qqcall["audio"] = copy.deepcopy(audio_overrides)
                _write_toml(canonical_path, public)
                adapter_path = Path(temporary) / "adapter.toml"
                adapter_path.write_text(template, encoding="utf-8")

                with patch.object(qqcall_service, "_SERVICE_PROJECT_ROOT", service_root):
                    loaded = config_module.load_config(str(adapter_path))
                    runtime = loaded.qqcall.to_runtime_config()
                self.assertTrue(runtime.enabled)
                self.assertIs(runtime.audio["enabled"], True)

    def test_schema5_rejects_retired_and_unknown_public_audio_fields(self):
        example = copy.deepcopy(qqcall_service._fixed_validator().DEFAULT_CONFIG)
        invalid_audio_tables = (
            {"enabled": True},
            {"capture_mode": "loopback"},
            {"device_scope": {"capture": "virtual", "playback": "virtual"}},
            {"retired": "ignored"},
        )

        for audio in invalid_audio_tables:
            with self.subTest(audio=audio), tempfile.TemporaryDirectory() as temporary:
                service_root = Path(temporary) / "NachoBot-QQCall"
                service_root.mkdir()
                canonical_path = Path(temporary) / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
                public = copy.deepcopy(example)
                public["plugin"]["enabled"] = True
                qqcall = public["qqcall"]
                qqcall["account_id"] = "12345"
                qqcall["onebot"]["url"] = "http://127.0.0.1:3001"
                qqcall["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
                qqcall["audio"] = audio
                _write_toml(canonical_path, public)

                with patch.object(qqcall_service, "_SERVICE_PROJECT_ROOT", service_root):
                    with self.assertRaises(ValueError):
                        qqcall_service.read_qqcall_public_config()

    def test_admission_callbacks_use_current_napcat_policy_for_private_and_group(self):
        async def scenario():
            class ExistingNapcatPolicy:
                def __init__(self):
                    self.allowed_private = {123456789}
                    self.allowed_groups = {(123456789, 222333444)}
                    self.calls = []

                async def check_allow_to_chat(self, peer_uin, group_uin):
                    self.calls.append((peer_uin, group_uin))
                    if group_uin is None:
                        return peer_uin in self.allowed_private
                    return (peer_uin, group_uin) in self.allowed_groups

            policy = ExistingNapcatPolicy()
            runtime = create_napcat_qqcall_runtime(
                {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:6112",
                    "token_file": r"C:\private\control.token",
                    "account_id": "123456789",
                },
                router=SimpleNamespace(check_connection=lambda _platform: False),
                message_handler=SimpleNamespace(check_allow_to_chat=policy.check_allow_to_chat),
            )

            self.assertTrue(await runtime._admit_peer("123456789"))
            self.assertFalse(await runtime._admit_peer("987654321"))
            self.assertTrue(await runtime._admit_group("123456789", "222333444"))
            self.assertFalse(await runtime._admit_group("123456789", "999888777"))

            policy.allowed_private.clear()
            policy.allowed_private.add(987654321)
            policy.allowed_groups.clear()
            policy.allowed_groups.add((987654321, 999888777))
            self.assertFalse(await runtime._admit_peer("123456789"))
            self.assertTrue(await runtime._admit_peer("987654321"))
            self.assertFalse(await runtime._admit_group("123456789", "222333444"))
            self.assertTrue(await runtime._admit_group("987654321", "999888777"))

            self.assertEqual(policy.calls, [
                (123456789, None),
                (987654321, None),
                (123456789, 222333444),
                (123456789, 999888777),
                (123456789, None),
                (987654321, None),
                (123456789, 222333444),
                (987654321, 999888777),
            ])
            self.assertTrue(all(
                type(peer) is int and (group is None or type(group) is int)
                for peer, group in policy.calls
            ))

        asyncio.run(scenario())

    def test_busy_dm_uses_current_onebot_connection_private_policy_while_core_is_down(self):
        async def scenario():
            connection = SimpleNamespace(open=True)
            handler = SimpleNamespace(
                server_connection=connection,
                verified_account_id="123456789",
                connection_generation="nap-connection-7",
                admissions=[],
            )

            async def admission(peer, group):
                handler.admissions.append((peer, group))
                return True

            handler.check_allow_to_chat = admission
            actions = []

            async def send_action(action, params, **kwargs):
                actions.append((action, params, kwargs))
                self.assertTrue(await kwargs["pre_send_check"]())
                return {"status": "ok", "retcode": 0}

            fake_module = ModuleType("src.send_handler.nc_sending")
            fake_module.nc_message_sender = SimpleNamespace(send_message_to_napcat=send_action)

            class Router:
                def check_connection(self, _platform):
                    return False

                async def send_message(self, _message):
                    return True

                async def send_custom_message(self, **_kwargs):
                    return True

            runtime = create_napcat_qqcall_runtime(
                {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:6112",
                    "token_file": "C:\\private\\control.token",
                    "account_id": "123456789",
                    "peer_map": {"kernel-uid": "987654321"},
                },
                router=Router(),
                message_handler=handler,
            )
            state = await runtime._read_platform_state()
            self.assertTrue(state["connected"])
            self.assertFalse(state["core_ready"])
            with patch.dict(sys.modules, {"src.send_handler.nc_sending": fake_module}):
                self.assertTrue(await runtime._send_busy_private(
                    "987654321",
                    "当前正在通话，暂时无法接听，请稍后再试。",
                    AsyncMock(return_value=True),
                ))
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0][0], "send_private_msg")
            self.assertEqual(actions[0][1], {
                "user_id": 987654321,
                "message": [{"type": "text", "data": {"text": "当前正在通话，暂时无法接听，请稍后再试。"}}],
            })
            self.assertTrue(all(group is None for _peer, group in handler.admissions))

        asyncio.run(scenario())

    def test_uses_verified_existing_identity_router_and_private_admission(self):
        async def scenario():
            connection = SimpleNamespace(open=True)
            handler = SimpleNamespace(
                server_connection=connection,
                verified_account_id="123456789",
                connection_generation="nap-connection-4",
                admission_calls=[],
            )

            async def admission(peer, group):
                handler.admission_calls.append((peer, group))
                return True

            handler.check_allow_to_chat = admission
            sent = []
            custom = []

            class Router:
                def check_connection(self, platform):
                    return platform == "qq"

                async def send_message(self, message):
                    sent.append(message)
                    return True

                async def send_custom_message(self, **kwargs):
                    custom.append(kwargs)
                    return True

            config = {
                "enabled": True,
                "endpoint": "http://127.0.0.1:6112",
                "token_file": "C:\\private\\control.token",
                "account_id": "123456789",
                "peer_map": {"opaque-kernel-uid": "987654321"},
                "poll_interval_sec": 1.0,
                # Runtime enablement must not depend on device-name overrides.
                "audio": {"enabled": True},
            }
            diagnostic_logs = []
            forwarded_status = []
            diagnostic_logger = SimpleNamespace(
                info=lambda template, *args: diagnostic_logs.append(template.format(*args))
            )
            with patch.object(qqcall_runtime, "logger", diagnostic_logger):
                router = Router()
                with patch.dict(sys.modules, {"qqcall_pcm": None}):
                    runtime = create_napcat_qqcall_runtime(
                        config,
                        router=router,
                        message_handler=handler,
                        on_status=forwarded_status.append,
                    )
            self.assertIsNone(runtime._voice._client)
            self.assertIsNone(runtime._voice._session)
            self.assertIsNone(runtime._voice._backend)
            self.assertIsNotNone(runtime._voice._backend_factory)
            self.assertEqual(runtime.config.universal_api["url"], "http://127.0.0.1:8016")
            self.assertIs(runtime.config.audio["enabled"], True)
            state = await runtime._read_platform_state()
            self.assertTrue(state["connected"])
            self.assertEqual(state["account_id"], "123456789")
            self.assertEqual(state["identity"], (id(connection), "nap-connection-4", "123456789"))
            await runtime._admitted("987654321", state)
            self.assertEqual(handler.admission_calls, [(987654321, None)])
            self.assertTrue(await runtime._call(runtime._send_message, object()))
            self.assertTrue(await runtime._call(runtime._send_custom, "qqcall_response", {"ok": True}))
            self.assertEqual(custom, [{
                "platform": "qq",
                "message_type_name": "qqcall_response",
                "message": {"ok": True},
            }])
            self.assertEqual(len(sent), 1)
            self.assertEqual(runtime.config.enabled, True)
            status_event = {"kind": "test_status", "status": runtime.status()}
            runtime._on_status(status_event)
            runtime._on_status(status_event)
            self.assertEqual(len(diagnostic_logs), 1)
            self.assertEqual(len(forwarded_status), 2)
            self.assertIs(forwarded_status[0], status_event)
            self.assertNotIn("123456789", diagnostic_logs[0])

            replacement = SimpleNamespace(open=True)
            handler.server_connection = replacement
            handler.connection_generation = "nap-connection-5"
            changed = await runtime._read_platform_state()
            self.assertNotEqual(state["identity"], changed["identity"])

        asyncio.run(scenario())

    def test_real_send_handler_consumes_call_target_when_runtime_is_absent(self):
        sent = []
        fake_config = SimpleNamespace(napcat_server=SimpleNamespace(media_action_timeout_sec=240))
        fake_sender = SimpleNamespace(send_message_to_napcat=lambda *args, **kwargs: sent.append((args, kwargs)))
        fake_modules = {
            "src.config": SimpleNamespace(global_config=fake_config),
            "src.logger": SimpleNamespace(logger=SimpleNamespace(info=lambda *_: None, error=lambda *_: None)),
            "src.send_handler.send_command_handler": SimpleNamespace(SendCommandHandleClass=object),
            "src.send_handler.send_message_handler": SimpleNamespace(SendMessageHandleClass=object),
            "src.send_handler.nc_sending": SimpleNamespace(nc_message_sender=fake_sender),
        }
        clear_qqcall_runtime()
        with patch.dict(sys.modules, fake_modules):
            sys.modules.pop("src.send_handler.main_send_handler", None)
            module = importlib.import_module("src.send_handler.main_send_handler")
            raw = {
                "message_info": {
                    "platform": "qq",
                    "additional_config": {"delivery_target": {"kind": "qqcall", "malformed": True}},
                },
                "message_segment": {"type": "text", "data": "must-not-send"},
            }
            asyncio.run(module.SendHandler().handle_message(raw))
            sys.modules.pop("src.send_handler.main_send_handler", None)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
