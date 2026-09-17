from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.chat.message_receive import bot as bot_module  # noqa: E402
from src.plugin_system.apis import send_api  # noqa: E402


class _Permit:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FocusCoordinator:
    def effect_permit(self, *args, **kwargs):
        return _Permit()


class _ExplodingFocusCoordinator:
    def effect_permit(self, *args, **kwargs):
        raise AssertionError("plugin media delivery must not acquire a Focus permit")


class _ChatManager:
    def __init__(self, stream):
        self.stream = stream

    def get_stream(self, stream_id):
        return self.stream if stream_id == self.stream.stream_id else None


class _Sender:
    def __init__(self, result=True):
        self.result = result
        self.sent = asyncio.Event()
        self.message = None

    async def send_message(self, message, **kwargs):
        self.message = message
        assert send_api.pending_ack_count() == 1
        self.sent.set()
        return self.result


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_waiters():
    send_api._pending_ack_waiters.clear()
    yield
    assert send_api.pending_ack_count() == 0


def _setup(monkeypatch, sender):
    stream = SimpleNamespace(
        stream_id="qq:user:1",
        platform="qq",
        group_info=None,
        user_info=SimpleNamespace(user_id="1"),
    )
    monkeypatch.setattr(send_api, "focus_coordinator", _FocusCoordinator())
    monkeypatch.setattr(send_api, "get_chat_manager", lambda: _ChatManager(stream))
    monkeypatch.setattr(send_api, "UniversalMessageSender", lambda: sender)
    monkeypatch.setattr(send_api.MessageStorage, "update_message", staticmethod(lambda *_args: False))
    monkeypatch.setattr(bot_module.MessageStorage, "update_message", staticmethod(lambda *_args: False))
    return stream


def test_media_receipt_waits_for_platform_echo_and_bot_updates_without_db_warning(monkeypatch):
    sender = _Sender()
    stream = _setup(monkeypatch, sender)
    updates = []
    monkeypatch.setattr(
        bot_module.MessageStorage,
        "update_message",
        staticmethod(lambda message_id, actual_id: updates.append((message_id, actual_id)) or False),
    )

    async def scenario():
        task = asyncio.create_task(
            send_api.local_media_to_stream_receipt(
                "videofile", "/tmp/video.mp4", stream.stream_id, show_log=False, ack_timeout=1
            )
        )
        await sender.sent.wait()
        core_id = sender.message.message_info.message_id
        assert not task.done()
        await bot_module.ChatBot.echo_message_process(
            object(),
            {
                "platform": "qq",
                "content": {"type": "echo", "echo": core_id, "actual_id": "987"},
            },
        )
        receipt = await task
        assert receipt.delivered
        assert receipt.message_id == "987"
        assert updates == [(core_id, "987")]

    _run(scenario())


def test_local_media_receipt_bypasses_focus_and_resolves_ack(monkeypatch):
    sender = _Sender()
    stream = _setup(monkeypatch, sender)
    monkeypatch.setattr(send_api, "focus_coordinator", _ExplodingFocusCoordinator())

    async def scenario():
        task = asyncio.create_task(
            send_api.local_media_to_stream_receipt(
                "videofile", "/tmp/video.mp4", stream.stream_id, show_log=False, ack_timeout=1
            )
        )
        await sender.sent.wait()
        core_id = sender.message.message_info.message_id
        await bot_module.ChatBot.echo_message_process(
            object(),
            {
                "platform": "qq",
                "content": {"type": "echo", "echo": core_id, "actual_id": "ack"},
            },
        )
        receipt = await task
        assert receipt.delivered
        assert receipt.message_id == "ack"

    _run(scenario())


def test_mismatched_platform_echo_cannot_satisfy_media_receipt(monkeypatch):
    sender = _Sender()
    stream = _setup(monkeypatch, sender)

    async def scenario():
        task = asyncio.create_task(
            send_api.local_media_to_stream_receipt(
                "voicefile", "/tmp/voice.silk", stream.stream_id, show_log=False, ack_timeout=1
            )
        )
        await sender.sent.wait()
        core_id = sender.message.message_info.message_id
        await bot_module.ChatBot.echo_message_process(
            object(),
            {
                "platform": "other",
                "content": {"type": "echo", "echo": core_id, "actual_id": "bad"},
            },
        )
        await asyncio.sleep(0)
        assert not task.done()
        await bot_module.ChatBot.echo_message_process(
            object(),
            {
                "platform": "qq",
                "content": {"type": "echo", "echo": core_id, "actual_id": "good"},
            },
        )
        assert (await task).message_id == "good"

    _run(scenario())


@pytest.mark.parametrize("terminal", ["failure", "timeout", "cancel"])
def test_media_receipt_cleans_waiter_on_failure_timeout_and_cancel(monkeypatch, terminal):
    sender = _Sender(result=terminal != "failure")
    stream = _setup(monkeypatch, sender)

    async def scenario():
        task = asyncio.create_task(
            send_api.local_media_to_stream_receipt(
                "voicefile", "/tmp/voice.silk", stream.stream_id, show_log=False, ack_timeout=0.01
            )
        )
        await sender.sent.wait()
        if terminal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            receipt = await task
            assert not receipt.delivered

    _run(scenario())
