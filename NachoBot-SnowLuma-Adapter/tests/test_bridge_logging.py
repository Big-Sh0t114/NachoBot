from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_NACHOBOT_PATH = Path(__file__).resolve().parents[2] / "NachoBot"
if str(_NACHOBOT_PATH) not in sys.path:
    sys.path.append(str(_NACHOBOT_PATH))

from ncnk_message import BaseMessageInfo, FormatInfo, GroupInfo, MessageBase, Seg, UserInfo  # noqa: E402
from src.bridge import SnowLumaBridge  # noqa: E402
from src.config import global_config  # noqa: E402
from src.logger import logger  # noqa: E402


@pytest.fixture
def bridge() -> SnowLumaBridge:
    return SnowLumaBridge()


def test_admission_rejection_logs_stable_policy_reason(bridge: SnowLumaBridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(global_config.chat, "enable_chat_list_filter", True)
    monkeypatch.setattr(global_config.chat, "group_list_type", "whitelist")
    monkeypatch.setattr(global_config.chat, "group_list", [1001])
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{level}|{message}")
    try:
        assert asyncio.run(bridge._allowed(2002, 3003)) is False
    finally:
        logger.remove(sink)
    assert any("reason=group_whitelist" in record for record in records)


def test_allowed_message_and_senderless_notice_handoffs_are_visible(
    bridge: SnowLumaBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(global_config.chat, "enable_chat_list_filter", True)
    monkeypatch.setattr(global_config.chat, "group_list_type", "whitelist")
    monkeypatch.setattr(global_config.chat, "group_list", [1001])
    monkeypatch.setattr(global_config.chat, "private_list_type", "whitelist")
    monkeypatch.setattr(global_config.chat, "private_list", [2002])
    monkeypatch.setattr(global_config.chat, "ban_user_id", [])
    monkeypatch.setattr(bridge, "_get_group_name", AsyncMock(return_value="synthetic-group"))
    monkeypatch.setattr(bridge, "_get_member_info", AsyncMock(return_value={}))
    bridge.router.send_message = AsyncMock(return_value=True)
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{level}|{message}")
    try:
        async def scenario() -> None:
            await bridge.handle_snowluma_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 11,
                    "group_id": 1001,
                    "sender": {"user_id": 2002, "nickname": "synthetic-user"},
                    "message": [{"type": "text", "data": {"text": "synthetic message"}}],
                }
            )
            await bridge.handle_snowluma_event(
                {
                    "post_type": "notice",
                    "notice_type": "group_admin",
                    "sub_type": "set",
                    "group_id": 1001,
                    "user_id": 2002,
                    "operator_id": 2002,
                }
            )

        asyncio.run(scenario())
    finally:
        logger.remove(sink)

    assert any("ordinary-message handoff success" in record for record in records)
    assert any("system_event handoff success" in record for record in records)
    calls = bridge.router.send_message.await_args_list
    assert len(calls) == 2
    system_message = calls[1].args[0]
    assert system_message.message_info.user_info is None
    assert "system_event" in system_message.message_info.additional_config


def test_outbound_summary_does_not_log_message_body(bridge: SnowLumaBridge, monkeypatch: pytest.MonkeyPatch) -> None:
    message = MessageBase(
        message_info=BaseMessageInfo(
            platform="qq",
            message_id="synthetic-source",
            group_info=GroupInfo(platform="qq", group_id="1001", group_name="synthetic-group"),
            user_info=UserInfo(platform="qq", user_id="2002"),
            format_info=FormatInfo(content_format=["text"], accept_format=["text"]),
        ),
        message_segment=Seg(type="seglist", data=[Seg(type="text", data="synthetic-secret-body")]),
    )
    monkeypatch.setattr(bridge.client, "call_action", AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 12}}))
    bridge.router.send_custom_message = AsyncMock(return_value=True)
    records: list[str] = []
    sink = logger.add(records.append, level="DEBUG", format="{message}")
    try:
        asyncio.run(bridge._send_normal(message))
    finally:
        logger.remove(sink)
    assert not any("synthetic-secret-body" in record for record in records)
