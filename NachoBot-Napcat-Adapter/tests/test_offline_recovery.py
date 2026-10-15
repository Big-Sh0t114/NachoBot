from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message import Seg  # noqa: E402
import src.recv_handler.notice_handler as notice_handler_module  # noqa: E402
from src.recv_handler.message_handler import message_handler  # noqa: E402
from src.recv_handler.message_sending import message_send_instance  # noqa: E402
from src.recv_handler.notice_handler import notice_handler  # noqa: E402
from src.config import global_config  # noqa: E402


RECEIVED_AT = 1_700_000_000.75
LATER_TIME = RECEIVED_AT + 500


@pytest.mark.parametrize(
    ("value", "expected", "source"),
    [
        (1_700_000_000.125, 1_700_000_000.125, "platform_seconds"),
        (1_700_000_000_125, 1_700_000_000.125, "platform_milliseconds"),
        ("invalid", RECEIVED_AT, "receive_time_fallback"),
    ],
)
def test_notice_keeps_receive_time_and_preserves_platform_timestamp(monkeypatch, value, expected, source):
    now = [RECEIVED_AT]

    async def member_info(*_args):
        now[0] = LATER_TIME
        return {"nickname": "tester", "card": ""}

    monkeypatch.setattr(notice_handler_module, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(notice_handler_module, "get_member_info", AsyncMock(side_effect=member_info))
    monkeypatch.setattr(
        notice_handler_module,
        "get_group_info",
        AsyncMock(return_value={"group_name": "test-group"}),
    )
    monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(message_send_instance, "message_send", send)

    handler = notice_handler_module.NoticeHandler()
    handler.server_connection = object()
    asyncio.run(
        handler.handle_notice(
            {
                "notice_type": "group_admin",
                "sub_type": "set",
                "group_id": 77,
                "user_id": 42,
                "time": value,
            }
        )
    )

    send.assert_awaited_once()
    message = send.await_args.args[0]
    assert message.message_info.time == RECEIVED_AT
    assert message.message_info.user_info is None
    assert message.message_info.additional_config["notice_timestamp"] == {
        "original_timestamp": expected,
        "timestamp_source": source,
    }
    assert "offline_recovery" not in message.message_info.additional_config
    assert "progress_id" not in message.message_info.additional_config
    assert "system_event" in message.message_info.additional_config


@pytest.mark.parametrize(
    ("value", "expected", "source"),
    [
        (1_700_000_000.125, 1_700_000_000.125, "platform_seconds"),
        ("1700000000.125", 1_700_000_000.125, "platform_seconds"),
        (1_700_000_000_125, 1_700_000_000.125, "platform_milliseconds"),
        (True, RECEIVED_AT, "receive_time_fallback"),
        (float("nan"), RECEIVED_AT, "receive_time_fallback"),
        (float("inf"), RECEIVED_AT, "receive_time_fallback"),
        (None, RECEIVED_AT, "receive_time_fallback"),
        ("invalid", RECEIVED_AT, "receive_time_fallback"),
    ],
)
def test_capture_normalizes_original_onebot_timestamp(value, expected, source):
    raw = {"message_type": "private", "sender": {"user_id": 42}}
    if value is not None:
        raw["time"] = value

    context = message_handler.capture_offline_recovery_context(
        raw,
        connection_epoch_started_at=RECEIVED_AT - 5,
        received_at=RECEIVED_AT,
    )

    assert context["metadata"]["original_timestamp"] == expected
    assert context["metadata"]["timestamp_source"] == source
    assert context["metadata"]["received_at"] == RECEIVED_AT
    assert context["metadata"]["connection_epoch_started_at"] == RECEIVED_AT - 5
    assert context["metadata"]["progress_id"]
    assert context["metadata"]["source_instance"]
    assert context["group_id"] is None
    assert context["user_id"] == "42"


def _private_message(timestamp=RECEIVED_AT):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 123,
        "time": timestamp,
        "sender": {"user_id": 42, "nickname": "tester", "card": ""},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
        # A raw event cannot override adapter-owned metadata.
        "additional_config": {"offline_recovery": {"original_timestamp": 1}},
    }


def test_direct_message_conversion_preserves_captured_timestamp_and_closes_progress(monkeypatch):
    context = message_handler.capture_offline_recovery_context(
        _private_message(1_700_000_000.125),
        connection_epoch_started_at=RECEIVED_AT - 5,
        received_at=RECEIVED_AT,
    )
    monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
    monkeypatch.setattr(
        message_handler,
        "handle_real_message",
        AsyncMock(return_value=([Seg(type="text", data="hello")], {})),
    )
    message_send = AsyncMock(return_value=True)
    progress_send = AsyncMock(return_value=True)
    monkeypatch.setattr(message_send_instance, "message_send", message_send)
    monkeypatch.setattr(message_send_instance, "send_custom_message", progress_send)

    asyncio.run(message_handler.handle_raw_message(_private_message(1_700_000_000.125), context))

    sent = message_send.await_args.args[0]
    assert sent.message_info.time == 1_700_000_000.125
    assert sent.message_info.additional_config["offline_recovery"] == context["metadata"]
    assert sent.raw_message == "hello"
    assert [call.kwargs["custom_message"]["active"] for call in progress_send.await_args_list] == [True, False]
    assert all(call.kwargs["message_type"] == "offline_recovery_progress" for call in progress_send.await_args_list)
    assert all(call.kwargs["custom_message"]["progress_id"] == context["metadata"]["progress_id"] for call in progress_send.await_args_list)
    assert all(call.kwargs["custom_message"]["source_instance"] == context["metadata"]["source_instance"] for call in progress_send.await_args_list)


@pytest.mark.parametrize(
    ("route", "chat_config"),
    [
        (
            {"message_type": "group", "group_id": 77, "sender": {"user_id": 42}},
            {"group_list_type": "whitelist", "group_list": {88}},
        ),
        (
            {"message_type": "group", "group_id": 77, "sender": {"user_id": 42}},
            {"group_list_type": "blacklist", "group_list": {77}},
        ),
        (
            {"message_type": "private", "sender": {"user_id": 42}},
            {"private_list_type": "whitelist", "private_list": {88}},
        ),
        (
            {"message_type": "private", "sender": {"user_id": 42}},
            {"private_list_type": "blacklist", "private_list": {42}},
        ),
        (
            {"message_type": "private", "sender": {"user_id": 42}},
            {"ban_user_id": {42}},
        ),
    ],
)
def test_admission_rejection_sends_progress_end_without_core_message(monkeypatch, route, chat_config):
    defaults = {
        "group_list_type": "blacklist",
        "group_list": set(),
        "private_list_type": "blacklist",
        "private_list": set(),
        "ban_user_id": set(),
        "ban_qq_bot": False,
    }
    defaults.update(chat_config)
    monkeypatch.setattr(global_config, "chat", SimpleNamespace(**defaults))
    message_send = AsyncMock(return_value=True)
    progress_send = AsyncMock(return_value=True)
    convert = AsyncMock(return_value=([Seg(type="text", data="hello")], {}))
    monkeypatch.setattr(message_send_instance, "message_send", message_send)
    monkeypatch.setattr(message_send_instance, "send_custom_message", progress_send)
    monkeypatch.setattr(message_handler, "handle_real_message", convert)

    raw = {
        "post_type": "message",
        "message_type": route["message_type"],
        "sub_type": "normal" if route["message_type"] == "group" else "friend",
        "message_id": 124,
        "time": RECEIVED_AT,
        "sender": {"user_id": route["sender"]["user_id"], "nickname": "tester"},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
        **({"group_id": route["group_id"]} if "group_id" in route else {}),
    }
    asyncio.run(message_handler.handle_raw_message(raw))

    message_send.assert_not_awaited()
    convert.assert_not_awaited()
    assert [call.kwargs["custom_message"]["active"] for call in progress_send.await_args_list] == [True, False]


@pytest.mark.parametrize("rejection", ["self_muted", "qq_bot"])
def test_mute_and_qqbot_admission_rejections_send_no_core_message(monkeypatch, rejection):
    chat = SimpleNamespace(
        group_list_type="blacklist",
        group_list=set(),
        private_list_type="blacklist",
        private_list=set(),
        ban_user_id=set(),
        ban_qq_bot=rejection == "qq_bot",
    )
    monkeypatch.setattr(global_config, "chat", chat)
    if rejection == "self_muted":
        monkeypatch.setitem(notice_handler.self_muted_groups, 77, time.time() + 300)
    else:
        monkeypatch.setattr(
            "src.recv_handler.message_handler.get_member_info",
            AsyncMock(return_value={"is_robot": True}),
        )
    message_send = AsyncMock(return_value=True)
    progress_send = AsyncMock(return_value=True)
    monkeypatch.setattr(message_send_instance, "message_send", message_send)
    monkeypatch.setattr(message_send_instance, "send_custom_message", progress_send)

    raw = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 125,
        "time": RECEIVED_AT,
        "group_id": 77,
        "sender": {"user_id": 42, "nickname": "tester"},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
    }
    asyncio.run(message_handler.handle_raw_message(raw))

    message_send.assert_not_awaited()
    assert [call.kwargs["custom_message"]["active"] for call in progress_send.await_args_list] == [True, False]


def test_napcat_receive_starts_progress_before_queueing(monkeypatch):
    import main as adapter_main

    order = []
    raw = _private_message()

    class OneFrameConnection:
        def __init__(self):
            second = {**raw, "message_id": 124}
            self.frames = iter([json.dumps(raw), json.dumps(second)])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration from None

    class RecordingQueue:
        async def put(self, item):
            message_id = item[0]["message_id"]
            order.append(("queued", message_id))

    async def start_marker(_context, *, active):
        assert active is True
        order.append(("progress_started", _context["metadata"].get("progress_id")))

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(adapter_main, "message_queue", RecordingQueue())
    monkeypatch.setattr(adapter_main.message_handler, "set_server_connection", no_op)
    monkeypatch.setattr(adapter_main.notice_handler, "set_server_connection", no_op)
    monkeypatch.setattr(adapter_main.nc_message_sender, "set_server_connection", no_op)
    monkeypatch.setattr(adapter_main, "connection_watchdog", no_op)
    monkeypatch.setattr(
        adapter_main.message_handler,
        "capture_offline_recovery_context",
        lambda event, **kwargs: {"metadata": {"progress_id": str(event["message_id"])}},
    )
    monkeypatch.setattr(adapter_main.message_handler, "send_offline_recovery_progress", start_marker)

    asyncio.run(adapter_main.message_recv(OneFrameConnection()))

    assert order == [
        ("progress_started", "123"),
        ("queued", 123),
        ("progress_started", "124"),
        ("queued", 124),
    ]
