import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, Mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

NACHOBOT_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(NACHOBOT_ROOT) not in sys.path:
    # Napcat 自身的 src 包必须保持更高优先级；NachoBot 这里只用于提供 ncnk_message。
    sys.path.append(str(NACHOBOT_ROOT))

from src.listen_address import resolve_listen_address  # noqa: E402
from src.recv_handler import ACCEPT_FORMAT, NoticeType  # noqa: E402
from src.recv_handler.notice_handler import NoticeHandler  # noqa: E402
import src.recv_handler.notice_handler as notice_handler_module  # noqa: E402
import src.send_handler.main_send_handler as main_send_handler_module  # noqa: E402
from src.config import global_config  # noqa: E402


class ContainerBindTests(unittest.TestCase):
    def test_adapter_connects_to_core_and_advertises_tts_field(self) -> None:
        self.assertEqual(global_config.nachobot_server.port, 8000)
        self.assertIn("tts_text", ACCEPT_FORMAT)

    def test_core_port_is_read_directly_without_rewriting_file(self) -> None:
        from src.config.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            original = (
                "[nickname]\nnickname = 'test'\n\n"
                "[napcat_server]\nport = 8095\n\n"
                "[nachobot_server]\nhost = '127.0.0.1'\nport = 8000\n\n"
                "[chat]\n"
                "group_list_type = 'whitelist'\n"
                "group_list = []\n"
                "private_list_type = 'blacklist'\n"
                "private_list = []\n"
                "ban_user_id = []\n"
                "ban_qq_bot = false\n"
                "enable_poke = true\n\n"
                "[voice]\nuse_tts = false\n\n"
                "[debug]\nlevel = 'INFO'\n"
            )
            config_path.write_text(original, encoding="utf-8")
            loaded = load_config(str(config_path))
            self.assertEqual(loaded.nachobot_server.port, 8000)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_listen_address_import_does_not_require_runtime_config(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if part
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", "from src.listen_address import resolve_listen_address"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse((Path(directory) / "config.toml").exists())

    def test_environment_overrides_local_config_for_container(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "NACHOBOT_NAPCAT_LISTEN_HOST": "0.0.0.0",
                "NACHOBOT_NAPCAT_LISTEN_PORT": "8095",
            },
        ):
            self.assertEqual(
                resolve_listen_address("127.0.0.1", 9000),
                ("0.0.0.0", 8095),
            )


class NoticeSystemEventTests(unittest.TestCase):
    def test_poke_fast_path_is_independent_and_core_always_receives_event(self) -> None:
        async def scenario():
            async def run_case(random_value, poke_result=None, poke_error=None):
                handler = NoticeHandler()
                handler.server_connection = object()
                raw_message = {
                    "notice_type": NoticeType.notify,
                    "sub_type": NoticeType.Notify.poke,
                    "group_id": 100,
                    "user_id": 123,
                    "target_id": 999,
                    "self_id": 999,
                    "raw_info": [],
                }
                poke_mock = AsyncMock(return_value=poke_result)
                if poke_error is not None:
                    poke_mock.side_effect = poke_error
                with (
                    mock.patch.object(notice_handler_module.global_config.chat, "enable_poke", True),
                    mock.patch.object(
                        notice_handler_module.message_handler,
                        "check_allow_to_chat",
                        new=AsyncMock(return_value=True),
                    ),
                    mock.patch.object(
                        notice_handler_module,
                        "get_self_info",
                        new=AsyncMock(return_value={"user_id": 999, "nickname": "NachoBot"}),
                    ),
                    mock.patch.object(
                        notice_handler_module,
                        "get_member_info",
                        new=AsyncMock(return_value={"nickname": "测试用户", "card": "群名片"}),
                    ),
                    mock.patch.object(
                        notice_handler_module,
                        "get_group_info",
                        new=AsyncMock(return_value={"group_name": "测试群"}),
                    ),
                    mock.patch.object(notice_handler_module.random, "random", return_value=random_value),
                    mock.patch.object(
                        notice_handler_module.nc_message_sender,
                        "send_message_to_napcat",
                        new=poke_mock,
                    ),
                    mock.patch.object(
                        notice_handler_module.message_send_instance,
                        "message_send",
                        new=AsyncMock(),
                    ) as core_mock,
                ):
                    await handler.handle_notice(raw_message)
                core_mock.assert_awaited_once()
                event_data = core_mock.await_args.args[0].message_info.additional_config["system_event"]["data"]
                if random_value < 0.5:
                    poke_mock.assert_awaited_once_with("send_poke", {"user_id": 123, "group_id": 100})
                    expected_result = "error" if poke_error is not None else (
                        "success" if isinstance(poke_result, dict) and poke_result.get("status") == "ok" else "failed"
                    )
                    self.assertEqual(
                        event_data["fast_poke"],
                        {"triggered": True, "result": expected_result},
                    )
                else:
                    poke_mock.assert_not_awaited()
                    self.assertNotIn("fast_poke", event_data)

            await run_case(0.1, {"status": "ok"})
            await run_case(0.9, {"status": "ok"})
            await run_case(0.1, poke_error=RuntimeError("Napcat unavailable"))

        asyncio.run(scenario())

    def test_poke_uses_sender_none_and_preserves_actor_metadata(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()

            raw_message = {
                "notice_type": NoticeType.notify,
                "sub_type": NoticeType.Notify.poke,
                "group_id": 100,
                "user_id": 123,
                "target_id": 999,
                "self_id": 999,
                "raw_info": [],
            }

            with (
                mock.patch.object(notice_handler_module.global_config.chat, "enable_poke", True),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_self_info",
                    new=AsyncMock(return_value={"user_id": 999, "nickname": "NachoBot"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_member_info",
                    new=AsyncMock(return_value={"nickname": "测试用户", "card": "群名片"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(notice_handler_module.random, "random", return_value=0.9),
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as send_mock,
            ):
                await handler.handle_notice(raw_message)

            send_mock.assert_awaited_once()
            message = send_mock.await_args.args[0]
            self.assertIsNone(message.message_info.user_info)

            additional_config = message.message_info.additional_config
            self.assertIsInstance(additional_config, dict)
            event = additional_config["system_event"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["type"], "qq.poke")
            self.assertEqual(event["actor"], {"user_id": "123", "name": "群名片"})
            self.assertEqual(event["target"], {"user_id": "999", "name": "NachoBot"})
            self.assertEqual(event["data"]["group_id"], "100")

        asyncio.run(scenario())

    def test_private_poke_has_explicit_route_and_no_sender_or_group(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.notify,
                "sub_type": NoticeType.Notify.poke,
                "user_id": 123,
                "target_id": 999,
                "self_id": 999,
                "raw_info": [],
            }

            with (
                mock.patch.object(notice_handler_module.global_config.chat, "enable_poke", True),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_self_info",
                    new=AsyncMock(return_value={"user_id": 999, "nickname": "NachoBot"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_stranger_info",
                    new=AsyncMock(return_value={"nickname": "私聊用户", "card": "私聊备注"}),
                ),
                mock.patch.object(notice_handler_module.random, "random", return_value=0.9),
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(return_value=True),
                ) as send_mock,
            ):
                await handler.handle_notice(raw_message)

            send_mock.assert_awaited_once()
            message = send_mock.await_args.args[0]
            self.assertIsNone(message.message_info.user_info)
            self.assertIsNone(message.message_info.sender_info)
            self.assertIsNone(message.message_info.group_info)
            additional_config = message.message_info.additional_config
            self.assertEqual(
                additional_config["system_event_route"],
                {
                    "platform": "qq",
                    "kind": "private",
                    "peer": {
                        "user_id": "123",
                        "nickname": "私聊用户",
                        "cardname": "私聊备注",
                    },
                },
            )

        asyncio.run(scenario())

    def test_fast_poke_timeout_logs_and_still_forwards_once(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.notify,
                "sub_type": NoticeType.Notify.poke,
                "group_id": 100,
                "user_id": 123,
                "target_id": 999,
                "self_id": 999,
                "raw_info": [],
            }
            poke_mock = AsyncMock(side_effect=asyncio.TimeoutError())
            with (
                mock.patch.object(notice_handler_module.global_config.chat, "enable_poke", True),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_self_info",
                    new=AsyncMock(return_value={"user_id": 999, "nickname": "NachoBot"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_member_info",
                    new=AsyncMock(return_value={"nickname": "测试用户", "card": "群名片"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(notice_handler_module.random, "random", return_value=0.1),
                mock.patch.object(
                    notice_handler_module.nc_message_sender,
                    "send_message_to_napcat",
                    new=poke_mock,
                ),
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(return_value=True),
                ) as core_mock,
                mock.patch.object(notice_handler_module.logger, "warning") as warning_mock,
            ):
                await handler.handle_notice(raw_message)

            poke_mock.assert_awaited_once_with("send_poke", {"user_id": 123, "group_id": 100})
            core_mock.assert_awaited_once()
            warning_messages = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
            self.assertIn("快速回戳超时", warning_messages)

        asyncio.run(scenario())


    def test_natural_lift_uses_actorless_structured_system_event(self) -> None:
        class StopAfterCapture(Exception):
            pass

        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            handler.lifted_list.append(SimpleNamespace(group_id=100, user_id=123))

            captured = []

            async def capture_notice(message):
                captured.append(message)
                raise StopAfterCapture()

            handler.natural_lift = AsyncMock(
                return_value=SimpleNamespace(
                    data={
                        "sub_type": "lift_ban",
                        "lifted_user_info": {
                            "user_id": 123,
                            "user_nickname": "测试用户",
                            "user_cardname": "群名片",
                        },
                    }
                )
            )
            handler.put_notice = AsyncMock(side_effect=capture_notice)

            with (
                mock.patch.object(
                    notice_handler_module.db_manager,
                    "delete_ban_record",
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ) as admission_mock,
            ):
                with self.assertRaises(StopAfterCapture):
                    await handler.handle_natural_lift()

            admission_mock.assert_awaited_once_with(123, 100, False, False, ignore_self_muted=False)
            self.assertEqual(len(captured), 1)
            message = captured[0]
            self.assertIsNone(message.message_info.user_info)

            additional_config = message.message_info.additional_config
            self.assertIsInstance(additional_config, dict)
            event = additional_config["system_event"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["type"], "qq.group_lift_ban")
            self.assertIsNone(event["actor"])
            self.assertEqual(event["target"], {"user_id": "123", "name": "群名片"})
            self.assertEqual(event["data"]["group_id"], "100")
            self.assertTrue(event["data"]["natural"])

        asyncio.run(scenario())

    def test_natural_lift_is_dropped_by_admission_before_enrichment(self) -> None:
        class StopAfterDrop(Exception):
            pass

        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            lift_record = SimpleNamespace(group_id=100, user_id=123)
            handler.lifted_list.append(lift_record)
            handler.natural_lift = AsyncMock()
            handler.put_notice = AsyncMock()

            async def stop_after_drop(_delay):
                raise StopAfterDrop()

            with (
                mock.patch.object(
                    notice_handler_module.db_manager,
                    "delete_ban_record",
                ) as delete_record_mock,
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=False),
                ) as admission_mock,
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(),
                ) as group_info_mock,
                mock.patch.object(notice_handler_module.logger, "warning") as warning_mock,
                mock.patch.object(notice_handler_module.asyncio, "sleep", new=stop_after_drop),
            ):
                with self.assertRaises(StopAfterDrop):
                    await handler.handle_natural_lift()

            delete_record_mock.assert_called_once_with(lift_record)
            admission_mock.assert_awaited_once_with(123, 100, False, False, ignore_self_muted=False)
            handler.natural_lift.assert_not_awaited()
            handler.put_notice.assert_not_awaited()
            group_info_mock.assert_not_awaited()
            self.assertEqual(handler.lifted_list, [])
            warning_messages = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
            self.assertIn("自然解除禁言 notice/system event 因聊天准入策略被丢弃", warning_messages)

        asyncio.run(scenario())


    def test_generic_group_notice_is_forwarded_as_structured_system_event(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()

            raw_message = {
                "notice_type": "group_admin",
                "sub_type": "set",
                "group_id": 100,
                "user_id": 123,
            }

            with (
                mock.patch.object(
                    notice_handler_module,
                    "get_member_info",
                    new=AsyncMock(return_value={"nickname": "测试用户", "card": "群名片"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ) as admission_mock,
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as send_mock,
            ):
                await handler.handle_notice(raw_message)

            send_mock.assert_awaited_once()
            admission_mock.assert_awaited_once()
            message = send_mock.await_args.args[0]
            self.assertIsNone(message.message_info.user_info)
            self.assertEqual(message.message_segment.data, "群名片被设为群管理员")

            additional_config = message.message_info.additional_config
            self.assertIsInstance(additional_config, dict)
            event = additional_config["system_event"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["type"], "qq.group_admin.set")
            self.assertIsNone(event["actor"])
            self.assertEqual(event["target"], {"user_id": "123", "name": "群名片"})
            self.assertEqual(event["data"]["group_id"], "100")
            self.assertEqual(event["data"]["raw_event"], raw_message)

        asyncio.run(scenario())

    def test_generic_group_notice_is_dropped_by_group_admission_before_enrichment(self) -> None:
        async def scenario():
            for list_type, configured_groups in (
                ("whitelist", [999]),
                ("blacklist", [100]),
            ):
                handler = NoticeHandler()
                handler.server_connection = object()
                raw_message = {
                    "notice_type": "group_admin",
                    "sub_type": "set",
                    "group_id": 100,
                    "user_id": 123,
                }
                generic_handler = AsyncMock()
                with (
                    mock.patch.object(notice_handler_module.global_config.chat, "group_list_type", list_type),
                    mock.patch.object(notice_handler_module.global_config.chat, "group_list", configured_groups),
                    mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", []),
                    mock.patch.object(notice_handler_module.global_config.chat, "ban_qq_bot", False),
                    mock.patch.object(
                        notice_handler_module.notice_handler,
                        "self_muted_groups",
                        {},
                    ),
                    mock.patch.object(handler, "_handle_generic_group_notice", new=generic_handler),
                    mock.patch.object(
                        notice_handler_module,
                        "get_member_info",
                        new=AsyncMock(),
                    ) as member_info_mock,
                    mock.patch.object(
                        notice_handler_module,
                        "get_group_info",
                        new=AsyncMock(),
                    ) as group_info_mock,
                    mock.patch.object(
                        notice_handler_module.message_send_instance,
                        "message_send",
                        new=AsyncMock(),
                    ) as core_mock,
                    mock.patch.object(
                        notice_handler_module.nc_message_sender,
                        "send_message_to_napcat",
                        new=AsyncMock(),
                    ) as action_mock,
                ):
                    await handler.handle_notice(raw_message)

                generic_handler.assert_not_awaited()
                member_info_mock.assert_not_awaited()
                group_info_mock.assert_not_awaited()
                core_mock.assert_not_awaited()
                action_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_private_poke_admission_blocks_action_and_forward(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.notify,
                "sub_type": NoticeType.Notify.poke,
                "user_id": 123,
                "target_id": 999,
                "self_id": 999,
                "raw_info": [],
            }
            with (
                mock.patch.object(notice_handler_module.global_config.chat, "enable_poke", True),
                mock.patch.object(notice_handler_module.global_config.chat, "private_list_type", "whitelist"),
                mock.patch.object(notice_handler_module.global_config.chat, "private_list", []),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", []),
                mock.patch.object(
                    notice_handler_module,
                    "get_stranger_info",
                    new=AsyncMock(),
                ) as stranger_info_mock,
                mock.patch.object(handler, "handle_poke_notify", new=AsyncMock()) as poke_handler_mock,
                mock.patch.object(
                    notice_handler_module.nc_message_sender,
                    "send_message_to_napcat",
                    new=AsyncMock(),
                ) as action_mock,
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as core_mock,
                mock.patch.object(
                    notice_handler_module.logger,
                    "warning",
                ) as warning_mock,
            ):
                await handler.handle_notice(raw_message)

            stranger_info_mock.assert_not_awaited()
            poke_handler_mock.assert_not_awaited()
            action_mock.assert_not_awaited()
            core_mock.assert_not_awaited()
            warning_messages = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
            self.assertIn("notice/system event 因聊天准入策略被丢弃", warning_messages)

        asyncio.run(scenario())

    def test_group_ban_notice_checks_operator_against_global_user_ban(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.ban,
                "group_id": 100,
                "user_id": 456,
                "operator_id": 123,
                "duration": 60,
            }

            with (
                mock.patch.object(notice_handler_module.global_config.chat, "group_list_type", "whitelist"),
                mock.patch.object(notice_handler_module.global_config.chat, "group_list", [100]),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", [123]),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_qq_bot", False),
                mock.patch.object(
                    notice_handler_module.notice_handler,
                    "self_muted_groups",
                    {},
                ),
                mock.patch.object(handler, "handle_ban_notify", new=AsyncMock()) as ban_handler_mock,
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as core_mock,
            ):
                await handler.handle_notice(raw_message)

            ban_handler_mock.assert_not_awaited()
            core_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_malformed_own_lift_ban_keeps_mute_fence_and_is_not_forwarded(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.lift_ban,
                "group_id": 100,
                "user_id": 999,
                "self_id": 999,
                # operator_id intentionally omitted: the dedicated handler
                # must reject the malformed self-unmute event.
            }
            muted_groups = {100: 9999999999.0}

            with (
                mock.patch.object(
                    handler,
                    "self_muted_groups",
                    muted_groups,
                ),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ) as admission_mock,
                mock.patch.object(
                    notice_handler_module,
                    "get_member_info",
                    new=AsyncMock(),
                ) as member_info_mock,
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(),
                ) as group_info_mock,
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as core_mock,
            ):
                await handler.handle_notice(raw_message)

            self.assertIn(100, muted_groups)
            admission_mock.assert_awaited_once_with(999, 100, False, False, ignore_self_muted=True)
            member_info_mock.assert_not_awaited()
            group_info_mock.assert_not_awaited()
            core_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_group_ban_keeps_qq_bot_actor_policy_enabled(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.ban,
                "group_id": 100,
                "user_id": 456,
                "operator_id": 123,
                "duration": 60,
            }

            with (
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=False),
                ) as admission_mock,
                mock.patch.object(handler, "handle_ban_notify", new=AsyncMock()) as ban_handler_mock,
            ):
                await handler.handle_notice(raw_message)

            admission_mock.assert_awaited_once_with(
                123,
                100,
                False,
                False,
                ignore_self_muted=False,
            )
            ban_handler_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_own_lift_ban_build_failure_keeps_mute_fence(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.lift_ban,
                "group_id": 100,
                "user_id": 999,
                "operator_id": 123,
                "self_id": 999,
            }
            muted_groups = {100: 9999999999.0}
            event_meta = {
                "type": "qq.group_lift_ban",
                "actor": {"user_id": "123", "name": "管理员"},
                "target": {"user_id": "999", "name": "NachoBot"},
                "data": {"group_id": "100"},
            }

            with (
                mock.patch.object(handler, "self_muted_groups", muted_groups),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    handler,
                    "handle_lift_ban_notify",
                    new=AsyncMock(
                        return_value=(
                            notice_handler_module.Seg(type="text", data="管理员解除了NachoBot的禁言"),
                            event_meta,
                        )
                    ),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "build_system_event",
                    side_effect=ValueError("invalid event"),
                ),
                mock.patch.object(handler, "put_notice", new=AsyncMock()) as put_notice_mock,
            ):
                await handler.handle_notice(raw_message)

            self.assertIn(100, muted_groups)
            put_notice_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_own_lift_ban_serialization_failure_keeps_mute_fence(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.lift_ban,
                "group_id": 100,
                "user_id": 999,
                "operator_id": 123,
                "self_id": 999,
                "non_serializable": object(),
            }
            muted_groups = {100: 9999999999.0}
            event_meta = {
                "type": "qq.group_lift_ban",
                "actor": {"user_id": "123", "name": "管理员"},
                "target": {"user_id": "999", "name": "NachoBot"},
                "data": {"group_id": "100"},
            }

            with (
                mock.patch.object(handler, "self_muted_groups", muted_groups),
                mock.patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    handler,
                    "handle_lift_ban_notify",
                    new=AsyncMock(
                        return_value=(
                            notice_handler_module.Seg(type="text", data="管理员解除了NachoBot的禁言"),
                            event_meta,
                        )
                    ),
                ),
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(handler, "put_notice", new=AsyncMock()) as put_notice_mock,
            ):
                await handler.handle_notice(raw_message)

            self.assertIn(100, muted_groups)
            put_notice_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_actorless_group_notice_uses_group_admission_not_private_list(self) -> None:
        async def scenario():
            raw_message = {
                "notice_type": "group_admin",
                "sub_type": "set",
                "group_id": 100,
            }

            for configured_groups, private_groups, should_forward in (
                ([100], [], True),
                ([999], [100], False),
            ):
                handler = NoticeHandler()
                handler.server_connection = object()
                with (
                    mock.patch.object(notice_handler_module.global_config.chat, "group_list_type", "whitelist"),
                    mock.patch.object(notice_handler_module.global_config.chat, "group_list", configured_groups),
                    mock.patch.object(notice_handler_module.global_config.chat, "private_list_type", "whitelist"),
                    mock.patch.object(notice_handler_module.global_config.chat, "private_list", private_groups),
                    mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", []),
                    mock.patch.object(notice_handler_module.global_config.chat, "ban_qq_bot", True),
                    mock.patch.object(
                        notice_handler_module.notice_handler,
                        "self_muted_groups",
                        {},
                    ),
                    mock.patch.object(
                        notice_handler_module,
                        "get_member_info",
                        new=AsyncMock(),
                    ) as member_info_mock,
                    mock.patch.object(
                        notice_handler_module,
                        "get_group_info",
                        new=AsyncMock(return_value={"group_name": "测试群"}),
                    ) as group_info_mock,
                    mock.patch.object(
                        notice_handler_module.message_send_instance,
                        "message_send",
                        new=AsyncMock(return_value=True),
                    ) as core_mock,
                ):
                    await handler.handle_notice(raw_message)

                member_info_mock.assert_not_awaited()
                if should_forward:
                    core_mock.assert_awaited_once()
                    message = core_mock.await_args.args[0]
                    self.assertIsNone(message.message_info.user_info)
                    event = message.message_info.additional_config["system_event"]
                    self.assertEqual(event["data"]["group_id"], "100")
                    group_info_mock.assert_awaited_once()
                else:
                    core_mock.assert_not_awaited()
                    group_info_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_own_lift_ban_admission_keeps_mute_state_when_group_is_denied(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.lift_ban,
                "group_id": 100,
                "user_id": 999,
                "operator_id": 123,
                "self_id": 999,
            }
            muted_groups = {100: 9999999999.0}

            with (
                mock.patch.object(notice_handler_module.global_config.chat, "group_list_type", "whitelist"),
                mock.patch.object(notice_handler_module.global_config.chat, "group_list", [999]),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", []),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_qq_bot", False),
                mock.patch.object(
                    notice_handler_module.notice_handler,
                    "self_muted_groups",
                    muted_groups,
                ),
                mock.patch.object(handler, "self_muted_groups", muted_groups),
                mock.patch.object(
                    handler,
                    "handle_lift_ban_notify",
                    new=AsyncMock(),
                ) as lift_handler_mock,
                mock.patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as core_mock,
            ):
                await handler.handle_notice(raw_message)

            self.assertIn(100, muted_groups)
            lift_handler_mock.assert_not_awaited()
            core_mock.assert_not_awaited()

        asyncio.run(scenario())

    def test_own_lift_ban_can_pass_admission_while_bot_mute_fence_is_active(self) -> None:
        async def scenario():
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": NoticeType.group_ban,
                "sub_type": NoticeType.GroupBan.lift_ban,
                "group_id": 100,
                "user_id": 999,
                "operator_id": 123,
                "self_id": 999,
            }
            muted_groups = {100: 9999999999.0}
            event_meta = {
                "type": "qq.group_lift_ban",
                "actor": {"user_id": "123", "name": "管理员"},
                "target": {"user_id": "999", "name": "NachoBot"},
                "data": {"group_id": "100"},
            }

            with (
                mock.patch.object(notice_handler_module.global_config.chat, "group_list_type", "whitelist"),
                mock.patch.object(notice_handler_module.global_config.chat, "group_list", [100]),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_user_id", []),
                mock.patch.object(notice_handler_module.global_config.chat, "ban_qq_bot", False),
                mock.patch.object(
                    notice_handler_module.notice_handler,
                    "self_muted_groups",
                    muted_groups,
                ),
                mock.patch.object(handler, "self_muted_groups", muted_groups),
                mock.patch.object(
                    handler,
                    "handle_lift_ban_notify",
                    new=AsyncMock(
                        return_value=(notice_handler_module.Seg(type="text", data="管理员解除了NachoBot的禁言"), event_meta)
                    ),
                ) as lift_handler_mock,
                mock.patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                mock.patch.object(handler, "put_notice", new=AsyncMock()) as put_notice_mock,
            ):
                await handler.handle_notice(raw_message)

            self.assertNotIn(100, muted_groups)
            lift_handler_mock.assert_awaited_once_with(raw_message, 100)
            put_notice_mock.assert_awaited_once()

        asyncio.run(scenario())

    def test_send_normal_message_routes_senderless_group_message_by_group_info(self) -> None:
        async def scenario():
            handler = main_send_handler_module.SendHandler()
            raw_message_base = SimpleNamespace(
                message_info=SimpleNamespace(
                    group_info=SimpleNamespace(group_id=100),
                    user_info=None,
                ),
                message_segment=SimpleNamespace(type="text", data="回复系统事件"),
            )

            processed_message = [{"type": "text", "data": {"text": "回复系统事件"}}]
            with (
                mock.patch.object(
                    main_send_handler_module.SendMessageHandleClass,
                    "process_seg_recursive",
                    return_value=processed_message,
                ),
                mock.patch.object(
                    main_send_handler_module.nc_message_sender,
                    "send_message_to_napcat",
                    new=AsyncMock(return_value={"status": "ok", "data": {"message_id": 321}}),
                ) as send_mock,
                mock.patch.object(
                    main_send_handler_module.nc_message_sender,
                    "message_sent_back",
                    new=AsyncMock(),
                ) as sent_back_mock,
            ):
                await handler.send_normal_message(raw_message_base)

            send_mock.assert_awaited_once_with(
                "send_group_msg",
                {"group_id": 100, "message": processed_message},
            )
            sent_back_mock.assert_awaited_once_with(raw_message_base, 321)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
