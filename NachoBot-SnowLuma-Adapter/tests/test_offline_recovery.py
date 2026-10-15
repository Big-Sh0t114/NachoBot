from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-SnowLuma-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

from ncnk_message import Seg  # noqa: E402
import src.bridge as bridge_module  # noqa: E402
from src.bridge import SnowLumaBridge  # noqa: E402
from src.config import global_config  # noqa: E402
from src.snowluma_client import SnowLumaClient  # noqa: E402


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

    monkeypatch.setattr(bridge_module, "time", SimpleNamespace(time=lambda: now[0]))
    bridge = SnowLumaBridge()
    monkeypatch.setattr(bridge, "_notice_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(bridge, "_get_member_info", AsyncMock(side_effect=member_info))
    monkeypatch.setattr(bridge, "_get_group_name", AsyncMock(return_value="test-group"))
    bridge.router.send_message = AsyncMock(return_value=True)

    asyncio.run(
        bridge._handle_notice(
            {
                "notice_type": "group_admin",
                "sub_type": "set",
                "group_id": 77,
                "user_id": 42,
                "time": value,
            }
        )
    )

    bridge.router.send_message.assert_awaited_once()
    message = bridge.router.send_message.await_args.args[0]
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
    async def unused_event(_payload):
        return None

    async def unused_status(_online, _account_id):
        return None

    client = SnowLumaClient(unused_event, unused_status)
    client.connection_epoch_started_at = RECEIVED_AT - 5
    raw = {"message_type": "private", "sender": {"user_id": 42}}
    if value is not None:
        raw["time"] = value

    context = client.capture_offline_recovery_context(raw, received_at=RECEIVED_AT)

    assert context["metadata"]["original_timestamp"] == expected
    assert context["metadata"]["timestamp_source"] == source
    assert context["metadata"]["received_at"] == RECEIVED_AT
    assert context["metadata"]["connection_epoch_started_at"] == RECEIVED_AT - 5
    assert context["metadata"]["progress_id"]
    assert context["user_id"] == "42"
    assert context["group_id"] is None


def test_progress_route_ids_match_bridge_integer_normalization():
    async def unused_event(_payload):
        return None

    async def unused_status(_online, _account_id):
        return None

    client = SnowLumaClient(unused_event, unused_status)
    context = client.capture_offline_recovery_context(
        {
            "message_type": "group",
            "group_id": "00077",
            "sender": {"user_id": "00042"},
        },
        received_at=RECEIVED_AT,
    )

    assert context["group_id"] == "77"
    assert context["user_id"] == "42"


def _onebot_message(timestamp=RECEIVED_AT, *, group=False):
    payload = {
        "post_type": "message",
        "message_type": "group" if group else "private",
        "message_id": "message-1",
        "time": timestamp,
        "sender": {"user_id": 42, "nickname": "tester", "card": ""},
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
        # The adapter must use its own capture, never this untrusted value.
        "additional_config": {"offline_recovery": {"original_timestamp": 1}},
    }
    if group:
        payload["group_id"] = 77
    return payload


def test_transport_progress_brackets_parallel_conversion_and_core_send():
    order = []
    release_conversion = asyncio.Event()

    async def callback(payload):
        order.append("conversion_started")
        await release_conversion.wait()
        order.append("core_send")
        assert payload["raw_message"] == "hello"

    async def status(_online, _account_id):
        return None

    async def progress(context, active):
        order.append(("progress", active, context["metadata"]["progress_id"]))

    client = SnowLumaClient(callback, status, progress)
    client.connection_epoch_started_at = RECEIVED_AT - 5

    async def scenario():
        payload = _onebot_message(1_700_000_000.125)
        original = dict(payload)
        await client._handle_text_payload(json.dumps(payload))
        assert order[0][0:2] == ("progress", True)
        assert len(client._event_tasks) == 1
        task = next(iter(client._event_tasks))
        await asyncio.sleep(0)
        assert order[1] == "conversion_started"
        assert order[-1][0:2] != ("progress", False)
        release_conversion.set()
        await asyncio.gather(task)
        await asyncio.sleep(0)
        assert order[-1][0:2] == ("progress", False)
        assert order.index("core_send") < next(i for i, item in enumerate(order) if isinstance(item, tuple) and item[0:2] == ("progress", False))
        assert payload == original
        assert client.take_offline_recovery_context(payload) is None
        assert not client._recovery_contexts

    asyncio.run(scenario())


def test_listener_boundary_is_set_before_first_listener_task(monkeypatch):
    async def unused_event(_payload):
        return None

    async def unused_status(_online, _account_id):
        return None

    client = SnowLumaClient(unused_event, unused_status)
    observed = []

    async def connect():
        observed.append("connected")

    async def listen():
        observed.append(("listener", client.connection_epoch_started_at))
        client._stop_event.set()

    async def verify(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(client, "_listen", listen)
    monkeypatch.setattr(client, "call_action", verify)

    asyncio.run(client.run())

    assert observed[0] == "connected"
    assert observed[1][0] == "listener"
    assert isinstance(observed[1][1], float)


def test_cancelled_callback_before_first_step_still_closes_progress():
    markers = []

    async def callback(_payload):
        await asyncio.Event().wait()

    async def status(_online, _account_id):
        return None

    async def progress(context, active):
        markers.append((active, context["metadata"]["progress_id"]))

    client = SnowLumaClient(callback, status, progress)

    async def scenario():
        await client._handle_text_payload(json.dumps(_onebot_message()))
        task = next(iter(client._event_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client._drain_event_tasks()

    asyncio.run(scenario())

    assert [active for active, _progress_id in markers] == [True, False]
    assert markers[0][1] == markers[1][1]


def test_cancelled_pending_media_conversion_closes_progress_without_core_send(monkeypatch):
    order = []
    release_media = asyncio.Event()
    bridge = SnowLumaBridge()
    monkeypatch.setattr(bridge, "_allowed", AsyncMock(return_value=True))

    async def convert_media(_item, _raw, _additional):
        order.append("media_conversion_started")
        await release_media.wait()
        return Seg(type="text", data="converted")

    async def progress(context, active):
        order.append(("progress", active, context["metadata"]["progress_id"]))
        await bridge._send_offline_recovery_progress(context, active)

    monkeypatch.setattr(bridge, "_convert_inbound_segment", convert_media)
    bridge.client.progress_callback = progress
    bridge.router.send_message = AsyncMock(return_value=True)
    bridge.router.send_custom_message = AsyncMock(return_value=True)
    payload = _onebot_message()
    payload["message"] = [{"type": "image", "data": {"url": "https://example.invalid/image"}}]

    async def scenario():
        await bridge.client._handle_text_payload(json.dumps(payload))
        task = next(iter(bridge.client._event_tasks))
        await asyncio.sleep(0)
        assert order[0][0:2] == ("progress", True)
        assert order[1] == "media_conversion_started"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.client._drain_event_tasks()

    asyncio.run(scenario())

    assert [item[1] for item in order if isinstance(item, tuple) and item[0] == "progress"] == [True, False]
    bridge.router.send_message.assert_not_awaited()
    markers = [call.kwargs["message"] for call in bridge.router.send_custom_message.await_args_list]
    assert [marker["active"] for marker in markers] == [True, False]


def test_callback_exception_still_closes_progress():
    markers = []

    async def callback(_payload):
        raise RuntimeError("conversion failed")

    async def status(_online, _account_id):
        return None

    async def progress(context, active):
        markers.append((active, context["metadata"]["progress_id"]))

    client = SnowLumaClient(callback, status, progress)

    async def scenario():
        await client._handle_text_payload(json.dumps(_onebot_message()))
        await asyncio.gather(*tuple(client._event_tasks), return_exceptions=True)

    asyncio.run(scenario())

    assert [active for active, _progress_id in markers] == [True, False]
    assert markers[0][1] == markers[1][1]


def test_direct_bridge_conversion_preserves_timestamp_and_emits_metadata(monkeypatch):
    bridge = SnowLumaBridge()
    monkeypatch.setattr(bridge, "_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(bridge, "_convert_inbound_segment", AsyncMock(return_value=Seg(type="text", data="hello")))
    bridge.router.send_message = AsyncMock(return_value=True)
    bridge.router.send_custom_message = AsyncMock(return_value=True)

    asyncio.run(bridge._handle_inbound_message(_onebot_message(1_700_000_000.125)))

    sent = bridge.router.send_message.await_args.args[0]
    assert sent.message_info.time == 1_700_000_000.125
    assert sent.message_info.additional_config["offline_recovery"]["original_timestamp"] == 1_700_000_000.125
    assert sent.raw_message == "hello"
    markers = [call.kwargs["message"] for call in bridge.router.send_custom_message.await_args_list]
    assert [marker["active"] for marker in markers] == [True, False]
    assert markers[0]["progress_id"] == markers[1]["progress_id"]


def test_transport_bridge_uses_captured_context_and_finishes_once(monkeypatch):
    bridge = SnowLumaBridge()
    monkeypatch.setattr(bridge, "_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(bridge, "_convert_inbound_segment", AsyncMock(return_value=Seg(type="text", data="hello")))
    bridge.client.connection_epoch_started_at = RECEIVED_AT - 5
    bridge.router.send_message = AsyncMock(return_value=True)
    bridge.router.send_custom_message = AsyncMock(return_value=True)

    async def scenario():
        await bridge.client._handle_text_payload(
            json.dumps(_onebot_message(1_700_000_000.125)),
            received_at=RECEIVED_AT,
            progress_id="transport-progress",
        )
        await asyncio.gather(*tuple(bridge.client._event_tasks))

    asyncio.run(scenario())

    sent = bridge.router.send_message.await_args.args[0]
    assert sent.message_info.time == 1_700_000_000.125
    assert sent.message_info.additional_config["offline_recovery"]["received_at"] == RECEIVED_AT
    assert sent.message_info.additional_config["offline_recovery"]["progress_id"] == "transport-progress"
    markers = [call.kwargs["message"] for call in bridge.router.send_custom_message.await_args_list]
    assert [marker["active"] for marker in markers] == [True, False]
    assert markers[0]["progress_id"] == markers[1]["progress_id"] == "transport-progress"
    assert markers[0]["source_instance"] == markers[1]["source_instance"]
    assert markers[0]["user_id"] == str(sent.message_info.user_info.user_id) == "42"


@pytest.mark.parametrize(
    ("route", "policy"),
    [
        ({"message_type": "group", "group_id": 77}, {"group_list_type": "whitelist", "group_list": {88}}),
        ({"message_type": "group", "group_id": 77}, {"group_list_type": "blacklist", "group_list": {77}}),
        ({"message_type": "private"}, {"private_list_type": "whitelist", "private_list": {88}}),
        ({"message_type": "private"}, {"private_list_type": "blacklist", "private_list": {42}}),
        ({"message_type": "private"}, {"ban_user_id": {42}}),
    ],
)
def test_existing_admission_rejections_emit_no_ordinary_core_message(monkeypatch, route, policy):
    chat = {
        "enable_chat_list_filter": True,
        "group_list_type": "blacklist",
        "group_list": set(),
        "private_list_type": "blacklist",
        "private_list": set(),
        "ban_user_id": set(),
        "ban_qq_bot": False,
        "enable_poke": False,
    }
    chat.update(policy)
    monkeypatch.setattr(global_config, "chat", SimpleNamespace(**chat))
    bridge = SnowLumaBridge()
    bridge.router.send_message = AsyncMock(return_value=True)
    bridge.router.send_custom_message = AsyncMock(return_value=True)
    payload = _onebot_message(group=route["message_type"] == "group")
    if route["message_type"] == "private":
        payload["message_type"] = "private"

    asyncio.run(bridge._handle_inbound_message(payload))

    bridge.router.send_message.assert_not_awaited()
    markers = [call.kwargs["message"] for call in bridge.router.send_custom_message.await_args_list]
    assert [marker["active"] for marker in markers] == [True, False]
    assert markers[0]["progress_id"] == markers[1]["progress_id"]


@pytest.mark.parametrize("rejection", ["self_muted", "qq_bot"])
def test_mute_and_qqbot_admission_rejections_emit_no_ordinary_core_message(monkeypatch, rejection):
    chat = SimpleNamespace(
        enable_chat_list_filter=False,
        group_list_type="blacklist",
        group_list=set(),
        private_list_type="blacklist",
        private_list=set(),
        ban_user_id=set(),
        ban_qq_bot=rejection == "qq_bot",
        enable_poke=False,
    )
    monkeypatch.setattr(global_config, "chat", chat)
    bridge = SnowLumaBridge()
    bridge.router.send_message = AsyncMock(return_value=True)
    bridge.router.send_custom_message = AsyncMock(return_value=True)
    monkeypatch.setattr(bridge, "_convert_inbound_segment", AsyncMock(return_value=Seg(type="text", data="hello")))
    if rejection == "self_muted":
        bridge._self_muted_groups[77] = time.time() + 300
    else:
        monkeypatch.setattr(bridge, "_get_member_info", AsyncMock(return_value={"is_robot": True}))

    asyncio.run(bridge._handle_inbound_message(_onebot_message(group=True)))

    bridge.router.send_message.assert_not_awaited()
    markers = [call.kwargs["message"] for call in bridge.router.send_custom_message.await_args_list]
    assert [marker["active"] for marker in markers] == [True, False]
