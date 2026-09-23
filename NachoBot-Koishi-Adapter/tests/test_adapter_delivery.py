import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(_ROOT / "NachoBot"))
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))

from adapter import KoishiOneBotAdapter
from ncnk_message import BaseMessageInfo, MessageBase, Seg, UserInfo


class _FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.sent_event = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        self.sent_event.set()

    async def close(self):
        self.closed = True


def _adapter():
    instance = KoishiOneBotAdapter.__new__(KoishiOneBotAdapter)
    instance.config = SimpleNamespace(platform="discord", use_tts=False)
    instance.logger = Mock()
    instance.onebot_ws = _FakeWebSocket()
    instance.onebot_send_lock = asyncio.Lock()
    instance._onebot_response_waiters = {}
    instance._onebot_response_lock = asyncio.Lock()
    instance.router = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
    instance._ONEBOT_SEND_TIMEOUT = 0.2
    instance._ONEBOT_RESPONSE_TIMEOUT = 0.05
    return instance


def _outgoing_message():
    return MessageBase(
        message_info=BaseMessageInfo(
            platform="discord",
            message_id="core-message-1",
            user_info=UserInfo(
                platform="discord", user_id="42", user_nickname="receiver"
            ),
        ),
        message_segment=Seg(type="text", data="hello"),
    ).to_dict()


def test_successful_onebot_response_emits_one_message_id_echo():
    async def scenario():
        instance = _adapter()
        task = asyncio.create_task(instance.handle_from_nachobot(_outgoing_message()))
        await asyncio.wait_for(instance.onebot_ws.sent_event.wait(), timeout=0.2)
        echo = instance.onebot_ws.sent[0]["echo"]
        await instance._resolve_onebot_response(
            {
                "status": "ok",
                "retcode": 0,
                "echo": echo,
                "data": {"message_id": 9876},
            }
        )
        await asyncio.wait_for(task, timeout=0.2)

        instance.router.send_custom_message.assert_awaited_once_with(
            platform="discord",
            message_type_name="message_id_echo",
            message={
                "type": "echo",
                "echo": "core-message-1",
                "actual_id": "9876",
            },
        )
        assert instance._onebot_response_waiters == {}

    asyncio.run(scenario())


def test_onebot_error_response_never_emits_message_id_echo():
    async def scenario():
        instance = _adapter()
        task = asyncio.create_task(instance.handle_from_nachobot(_outgoing_message()))
        await asyncio.wait_for(instance.onebot_ws.sent_event.wait(), timeout=0.2)
        echo = instance.onebot_ws.sent[0]["echo"]
        await instance._resolve_onebot_response(
            {
                "status": "failed",
                "retcode": 100,
                "echo": echo,
                "message": "send failed",
            }
        )
        await asyncio.wait_for(task, timeout=0.2)

        instance.router.send_custom_message.assert_not_awaited()
        assert instance._onebot_response_waiters == {}

    asyncio.run(scenario())


def test_response_timeout_and_sender_cancellation_clean_waiters():
    async def scenario():
        instance = _adapter()
        timed_out = await instance._onebot_send("send_msg", {"message": []})
        assert timed_out is None
        assert instance._onebot_response_waiters == {}

        instance._ONEBOT_RESPONSE_TIMEOUT = 1.0
        instance.onebot_ws = _FakeWebSocket()
        task = asyncio.create_task(instance._onebot_send("send_msg", {"message": []}))
        await asyncio.wait_for(instance.onebot_ws.sent_event.wait(), timeout=0.2)
        assert len(instance._onebot_response_waiters) == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled OneBot sender unexpectedly completed")
        assert instance._onebot_response_waiters == {}

    asyncio.run(scenario())
