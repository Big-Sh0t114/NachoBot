from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-SnowLuma-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

import src.qqcall_runtime as qqcall_runtime  # noqa: E402
import src.config as adapter_config_module  # noqa: E402
from src.qqcall_runtime import create_snowluma_qqcall_runtime  # noqa: E402
from qqcall_client.qqcall import QQCallConfig, QQCallRuntime  # noqa: E402


class TestSnowLumaQQCallBinding(unittest.TestCase):
    def test_private_profile_uses_and_rechecks_current_snowluma_connection(self):
        async def scenario():
            snapshot = (object(), 7, "123456789")
            current = [True]
            async def lookup(action, params, **kwargs):
                self.assertEqual(action, "get_stranger_info")
                self.assertEqual(params, {"user_id": 34567})
                self.assertIs(kwargs["expected_connection"], snapshot)
                self.assertTrue(kwargs["pre_send_check"]())
                if change[0]:
                    current[0] = False
                return {"status": "ok", "retcode": 0, "data": {"user_id": 34567, "nickname": "真实昵称"}}
            change = [False]
            client = SimpleNamespace(connection_snapshot=lambda: snapshot,
                connection_is_current=lambda observed: observed is snapshot and current[0], call_action=lookup)
            runtime = create_snowluma_qqcall_runtime({"account_id": "123456789"}, router=SimpleNamespace(),
                client=client, admission_check=AsyncMock(return_value=(True, "allowed")))
            self.assertEqual((await runtime._voice._resolve_peer_info("34567"))["nickname"], "真实昵称")
            change[0] = True
            self.assertIsNone(await runtime._voice._resolve_peer_info("34567"))
        asyncio.run(scenario())

    def test_retired_local_qqcall_table_is_ignored_by_config_loader(self):
        template = (ADAPTER_ROOT / "template_config.toml").read_text(encoding="utf-8")
        legacy = template + '\n[qqcall]\nretired = "ignored"\n'
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(legacy, encoding="utf-8")
            runtime = QQCallConfig()
            with patch.object(adapter_config_module, "CONFIG_PATH", config_path), patch.object(
                adapter_config_module,
                "load_qqcall_adapter_settings",
                return_value=SimpleNamespace(config=runtime),
            ) as load_settings:
                loaded = adapter_config_module.load_config()
            self.assertIs(loaded.qqcall, runtime)
            load_settings.assert_called_once_with("snowluma", config_path, {})

    def test_admission_callbacks_use_current_snowluma_policy_for_private_and_group(self):
        async def scenario():
            class ExistingSnowLumaPolicy:
                def __init__(self):
                    self.allowed_private = {123456789}
                    self.allowed_groups = {(123456789, 222333444)}
                    self.calls = []

                async def check(self, peer_uin, group_uin):
                    self.calls.append((peer_uin, group_uin))
                    if group_uin is None:
                        allowed = peer_uin in self.allowed_private
                    else:
                        allowed = (peer_uin, group_uin) in self.allowed_groups
                    return allowed, "allowed" if allowed else "denied"

            policy = ExistingSnowLumaPolicy()
            runtime = create_snowluma_qqcall_runtime(
                {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:6112",
                    "token_file": r"C:\private\control.token",
                    "account_id": "123456789",
                },
                router=SimpleNamespace(check_connection=lambda _platform: False),
                client=SimpleNamespace(),
                admission_check=policy.check,
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

    def test_busy_dm_uses_current_socket_private_policy_while_core_is_down(self):
        async def scenario():
            websocket = object()
            action_calls = []

            class Client:
                def connection_snapshot(self):
                    return (websocket, 17, "123456789")

                def connection_is_current(self, snapshot):
                    return snapshot == self.connection_snapshot()

                async def call_action(self, action, params, **kwargs):
                    action_calls.append((action, params, kwargs))
                    self.assertion = await kwargs["pre_send_check"]()
                    return {"status": "ok", "retcode": 0}

            class Router:
                def check_connection(self, platform):
                    return False

                async def send_message(self, _message):
                    return True

                async def send_custom_message(self, **_kwargs):
                    return True

            admissions = []

            async def admission(peer, group):
                admissions.append((peer, group))
                return True, "allowed"

            config = {
                "enabled": True,
                "endpoint": "http://127.0.0.1:6112",
                "token_file": "C:\\private\\control.token",
                "account_id": "123456789",
                "peer_map": {"opaque-kernel-uid": "987654321"},
            }
            client = Client()
            runtime = create_snowluma_qqcall_runtime(
                config,
                router=Router(),
                client=client,
                admission_check=admission,
            )
            state = await runtime._read_platform_state()
            self.assertTrue(state["connected"])
            self.assertFalse(state["core_ready"])
            self.assertTrue(await runtime._send_busy_private(
                "987654321",
                "当前正在通话，暂时无法接听，请稍后再试。",
                AsyncMock(return_value=True),
            ))
            self.assertTrue(client.assertion)
            self.assertEqual(len(action_calls), 1)
            self.assertEqual(action_calls[0][0], "send_private_msg")
            self.assertEqual(action_calls[0][1], {
                "user_id": 987654321,
                "message": [{"type": "text", "data": {"text": "当前正在通话，暂时无法接听，请稍后再试。"}}],
            })
            self.assertGreaterEqual(len(admissions), 3)
            self.assertTrue(all(group is None for _peer, group in admissions))

        asyncio.run(scenario())

    def test_uses_verified_socket_generation_core_route_and_existing_admission(self):
        async def scenario():
            websocket = object()

            class Client:
                connected = True
                connection_generation = 17
                connected_account_id = "123456789"

                def connection_snapshot(self):
                    return (websocket, self.connection_generation, self.connected_account_id)

                def connection_is_current(self, snapshot):
                    return snapshot == self.connection_snapshot()

            class Router:
                def check_connection(self, platform):
                    return platform == "qq"

                async def send_message(self, message):
                    return True

                async def send_custom_message(self, **kwargs):
                    captured.append(kwargs)
                    return True

            admission_calls = []
            captured = []

            async def admission(peer, group):
                admission_calls.append((peer, group))
                return True, "allowed"

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
            client = Client()
            diagnostic_logs = []
            forwarded_status = []
            diagnostic_logger = SimpleNamespace(
                info=lambda template, *args: diagnostic_logs.append(template.format(*args))
            )
            with patch.object(qqcall_runtime, "logger", diagnostic_logger):
                router = Router()
                with patch.dict(sys.modules, {"qqcall_pcm": None}):
                    runtime = create_snowluma_qqcall_runtime(
                        config,
                        router=router,
                        client=client,
                        admission_check=admission,
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
            self.assertEqual(state["identity"], (id(websocket), 17, "123456789"))
            await runtime._admitted("987654321", state)
            self.assertEqual(admission_calls, [(987654321, None)])
            self.assertTrue(await runtime._call(runtime._send_custom, "qqcall_response", {"ok": True}))
            self.assertEqual(captured, [{
                "platform": "qq",
                "message_type_name": "qqcall_response",
                "message": {"ok": True},
            }])
            status_event = {"kind": "test_status", "status": runtime.status()}
            runtime._on_status(status_event)
            runtime._on_status(status_event)
            self.assertEqual(len(diagnostic_logs), 1)
            self.assertEqual(len(forwarded_status), 2)
            self.assertIs(forwarded_status[0], status_event)
            self.assertNotIn("123456789", diagnostic_logs[0])

            client.connection_generation = 18
            changed = await runtime._read_platform_state()
            self.assertNotEqual(state["identity"], changed["identity"])

            client.connection_snapshot = lambda: None
            disconnected = await runtime._read_platform_state()
            self.assertFalse(disconnected["connected"])

        asyncio.run(scenario())

    def test_real_bridge_outbound_guard_consumes_call_target_but_keeps_ordinary_route(self):
        async def scenario():
            state = {"connected": True, "account_id": "123456789", "identity": ("socket", 1, "123456789")}
            rejected = []

            async def send_custom(kind, message):
                rejected.append((kind, message))
                return True

            runtime = QQCallRuntime(
                QQCallConfig(),
                "qq",
                AsyncMock(return_value=True),
                send_custom,
                lambda: state,
                AsyncMock(return_value=True),
            )

            def fake_global_config():
                config = SimpleNamespace(
                    voice=SimpleNamespace(use_tts=False),
                    debug=SimpleNamespace(level="ERROR", raw_payload=False, raw_outbound=False),
                    nachobot=SimpleNamespace(host="127.0.0.1", port=8000, platform_name="qq"),
                    snowluma=SimpleNamespace(host="127.0.0.1", port=3001, path="", token="", scheme="ws"),
                    qqcall=QQCallConfig(),
                    chat=SimpleNamespace(),
                    visual=SimpleNamespace(),
                    send=SimpleNamespace(min_interval_sec=0),
                )
                return config

            fake_config = ModuleType("src.config")
            fake_config.global_config = fake_global_config()
            fake_logger = ModuleType("src.logger")

            def noop_log(*_args, **_kwargs):
                return None

            fake_logger.logger = SimpleNamespace(debug=noop_log, info=noop_log, warning=noop_log, error=noop_log)
            fake_logger.custom_logger = object()
            fake_client = ModuleType("src.snowluma_client")
            fake_client.SnowLumaClient = object
            fake_modules = {
                "src.config": fake_config,
                "src.logger": fake_logger,
                "src.snowluma_client": fake_client,
            }
            module_name = "src._qqcall_bridge_runtime_test"
            before = set(sys.modules)
            module_path = ADAPTER_ROOT / "src" / "bridge.py"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            self.assertIsNotNone(spec)
            bridge_module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                sys.modules[module_name] = bridge_module
                try:
                    assert spec.loader is not None
                    spec.loader.exec_module(bridge_module)
                    bridge = bridge_module.SnowLumaBridge.__new__(bridge_module.SnowLumaBridge)
                    bridge.qqcall_runtime = runtime
                    bridge._send_normal = AsyncMock(return_value=None)
                    bridge._handle_command = AsyncMock(return_value=None)
                    bridge.client = SimpleNamespace(call_action=AsyncMock())

                    for target in (
                        {"kind": "qqcall"},
                        {"kind": "qqcall", "callId": "stale", "peer_user_id": "987654321"},
                    ):
                        await bridge.handle_core_message({
                            "message_info": {"platform": "qq", "additional_config": {"delivery_target": target}},
                            "message_segment": {"type": "text", "data": "must-not-be-sent"},
                        })
                    self.assertEqual(bridge._send_normal.await_count, 0)
                    self.assertEqual(bridge._handle_command.await_count, 0)
                    self.assertEqual(bridge.client.call_action.await_count, 0)
                    self.assertEqual(len(rejected), 2)
                    self.assertTrue(all(kind == "qqcall_delivery_response" for kind, _ in rejected))
                    self.assertTrue(all(msg["error"]["code"] in {"audio_unavailable", "stale_call_scope"} for _, msg in rejected))

                    await bridge.handle_core_message({
                        "message_info": {
                            "platform": "qq",
                            "user_info": {"platform": "qq", "user_id": "987654321"},
                            "additional_config": {},
                        },
                        "message_segment": {"type": "text", "data": "ordinary"},
                    })
                    bridge._send_normal.assert_awaited_once()
                    bridge.client.call_action.assert_not_awaited()
                finally:
                    sys.modules.pop(module_name, None)
                    for imported in set(sys.modules) - before:
                        if imported.startswith("src.") and imported not in {"src.qqcall_runtime"}:
                            sys.modules.pop(imported, None)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
