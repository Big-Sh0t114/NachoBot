import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_ROOT = _ROOT / "NachoBot-Napcat-Adapter"
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(_ROOT / "NachoBot"))

from src.listen_address import resolve_listen_address  # noqa: F401,E402
from src.recv_handler import NoticeType  # noqa: F401,E402
from src.recv_handler.notice_handler import NoticeHandler  # noqa: E402
import src.recv_handler.notice_handler as notice_handler_module  # noqa: E402
from src.config import global_config  # noqa: E402


class NoticeTtsCapabilityTests(unittest.TestCase):
    def test_senderless_notice_uses_live_tts_capability(self):
        async def scenario(use_tts: bool):
            handler = NoticeHandler()
            handler.server_connection = object()
            raw_message = {
                "notice_type": "group_admin",
                "sub_type": "set",
                "group_id": 100,
                "user_id": 123,
            }

            with (
                patch.object(global_config.voice, "use_tts", use_tts),
                patch.object(
                    notice_handler_module.message_handler,
                    "check_allow_to_chat",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    notice_handler_module,
                    "get_group_info",
                    new=AsyncMock(return_value={"group_name": "测试群"}),
                ),
                patch.object(
                    notice_handler_module,
                    "get_member_info",
                    new=AsyncMock(return_value={"nickname": "测试用户", "card": "群名片"}),
                ),
                patch.object(
                    notice_handler_module.message_send_instance,
                    "message_send",
                    new=AsyncMock(),
                ) as send_mock,
            ):
                await handler.handle_notice(raw_message)

            message = send_mock.await_args.args[0]
            return message.message_info.format_info.accept_format

        self.assertNotIn("tts_text", asyncio.run(scenario(False)))
        self.assertIn("tts_text", asyncio.run(scenario(True)))


if __name__ == "__main__":
    unittest.main()
