from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-SnowLuma-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

from src.snowluma_client import SnowLumaClient  # noqa: E402


class FakeWebSocket:
    """Small queue-backed socket fake that exercises the production reader loop."""

    def __init__(self):
        self.closed = False
        self.close_code = None
        self.sent: list[dict] = []
        self.sent_event = asyncio.Event()
        self.incoming: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
        self.processed_event = asyncio.Event()

    async def __aiter__(self):
        while True:
            frame = await self.incoming.get()
            if frame.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR}:
                self.closed = True
                self.close_code = 1000
            try:
                yield frame
            finally:
                self.incoming.task_done()
                self.processed_event.set()
            if frame.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR}:
                return

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))
        self.sent_event.set()

    async def feed_json(self, payload: dict) -> None:
        self.processed_event.clear()
        await self.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(payload)))
        await asyncio.wait_for(self.processed_event.wait(), timeout=1.0)

    async def feed_closed(self) -> None:
        await self.incoming.put(SimpleNamespace(type=WSMsgType.CLOSED, data=""))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True


async def _noop_status(_online: bool, _account_id: str) -> None:
    return None


async def _noop_progress(_context: dict, _active: bool) -> None:
    return None


def test_reader_resolves_login_echo_while_prelogin_message_waits_off_reader(monkeypatch):
    async def scenario():
        socket = FakeWebSocket()
        callbacks: list[tuple[str, str]] = []
        callback_started = asyncio.Event()
        callback_finished = asyncio.Event()
        client: SnowLumaClient

        async def handle_event(payload: dict) -> None:
            context = client.take_offline_recovery_context(payload)
            assert context is not None
            callback_started.set()
            account_id = await client.wait_for_verified_connection(context["connection"])
            if account_id is not None:
                event_account = str(payload.get("self_id") or "").strip()
                if event_account and event_account == account_id:
                    callbacks.append((event_account, account_id))
            callback_finished.set()

        client = SnowLumaClient(handle_event, _noop_status, _noop_progress)

        async def connect_fake_socket() -> None:
            client._ws = socket
            client._connection_generation += 1
            client._signal_identity_change()

        monkeypatch.setattr(client, "_connect", connect_fake_socket)
        run_task = asyncio.create_task(client.run())
        await asyncio.wait_for(socket.sent_event.wait(), timeout=1.0)
        login_frame = socket.sent[0]
        assert login_frame["action"] == "get_login_info"

        await socket.feed_json(
            {
                "post_type": "message",
                "message_type": "private",
                "self_id": "90001",
                "message_id": "prelogin-message",
                "sender": {"user_id": 77},
                "message": [{"type": "text", "data": {"text": "wait for login"}}],
            }
        )
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)
        assert client.connected_account_id == ""
        assert not callback_finished.is_set()

        # The same reader that dispatched the ordinary event must still read
        # the login action echo; the event callback waits in its own task.
        await socket.feed_json(
            {
                "echo": login_frame["echo"],
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": "90001"},
            }
        )
        generation = client.connection_generation
        verified = await asyncio.wait_for(
            client.wait_for_verified_connection((socket, generation)),
            timeout=1.0,
        )
        await asyncio.wait_for(callback_finished.wait(), timeout=1.0)
        assert verified == "90001"
        assert client.connected_account_id == "90001"
        assert client._identity_verified is True
        assert callbacks == [("90001", "90001")]

        # A raw event cannot replace the account that get_login_info verified.
        await socket.feed_json(
            {
                "post_type": "message",
                "message_type": "private",
                "self_id": "80002",
                "message_id": "wrong-account",
                "sender": {"user_id": 77},
                "message": [{"type": "text", "data": {"text": "reject"}}],
            }
        )
        assert client.connected_account_id == "90001"
        assert callbacks == [("90001", "90001")]

        client._stop_event.set()
        await socket.feed_closed()
        await asyncio.wait_for(run_task, timeout=1.0)
        await client._drain_event_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["closed", "replaced"])
def test_verified_identity_wait_returns_none_when_socket_generation_ends(transition):
    async def scenario():
        client = SnowLumaClient(lambda _payload: _noop_status(False, ""), _noop_status)
        original = FakeWebSocket()
        client._ws = original
        client._connection_generation = 12
        snapshot = (original, 12)
        waiter = asyncio.create_task(client.wait_for_verified_connection(snapshot))
        await asyncio.sleep(0)
        assert not waiter.done()

        if transition == "closed":
            original.closed = True
        else:
            client._ws = FakeWebSocket()
            client._connection_generation = 13
            client._connected_account_id = "80002"
            client._identity_verified = True
        client._signal_identity_change()

        assert await asyncio.wait_for(waiter, timeout=1.0) is None

    asyncio.run(scenario())


def test_action_waiting_for_send_lock_rejects_replaced_socket_before_send():
    async def scenario():
        client = SnowLumaClient(lambda _payload: _noop_status(False, ""), _noop_status)
        original = FakeWebSocket()
        client._ws = original
        client._connection_generation = 5
        client._connected_account_id = "90001"
        await client._send_lock.acquire()
        action = asyncio.create_task(
            client.call_action(
                "get_group_msg_history",
                {"group_id": 77},
                expected_connection=(original, 5, "90001"),
                timeout_sec=1,
            )
        )
        await asyncio.sleep(0)
        assert original.sent == []

        client._ws = FakeWebSocket()
        client._connection_generation = 6
        client._send_lock.release()
        with pytest.raises(RuntimeError, match="connection changed before send"):
            await asyncio.wait_for(action, timeout=1.0)
        assert original.sent == []
        assert client._response_pool == {}

    asyncio.run(scenario())




@pytest.mark.parametrize("change", ["socket", "account"])
def test_action_rejects_response_after_socket_or_account_changes(change):
    async def scenario():
        client = SnowLumaClient(lambda _payload: _noop_status(False, ""), _noop_status)
        original = FakeWebSocket()
        client._ws = original
        client._connection_generation = 21
        client._connected_account_id = "90001"
        client._identity_verified = True
        action = asyncio.create_task(
            client.call_action(
                "get_friend_msg_history",
                {"user_id": 77},
                expected_connection=(original, 21, "90001"),
                timeout_sec=1,
            )
        )
        await asyncio.wait_for(original.sent_event.wait(), timeout=1.0)
        echo = original.sent[0]["echo"]
        await client._handle_text_payload(
            json.dumps({"echo": echo, "status": "ok", "retcode": 0, "data": {"messages": []}})
        )
        if change == "socket":
            client._ws = FakeWebSocket()
            client._connection_generation = 22
        else:
            client._connected_account_id = "80002"

        expected_reason = "connection changed before response" if change == "socket" else "account identity changed before response"
        with pytest.raises(RuntimeError, match=expected_reason):
            await asyncio.wait_for(action, timeout=1.0)
        assert client._response_pool == {}

    asyncio.run(scenario())


def test_login_response_from_replaced_generation_cannot_verify_old_socket(monkeypatch):
    async def scenario():
        original = FakeWebSocket()
        replacement = FakeWebSocket()
        client = SnowLumaClient(lambda _payload: _noop_status(False, ""), _noop_status)
        observed_at_disconnect: list[tuple[str, bool]] = []

        async def connect_fake_socket() -> None:
            client._ws = original
            client._connection_generation += 1
            client._signal_identity_change()

        disconnect = client._disconnect

        async def record_disconnect(**kwargs):
            observed_at_disconnect.append((client.connected_account_id, client._identity_verified))
            await disconnect(**kwargs)

        monkeypatch.setattr(client, "_connect", connect_fake_socket)
        monkeypatch.setattr(client, "_disconnect", record_disconnect)
        run_task = asyncio.create_task(client.run())
        await asyncio.wait_for(original.sent_event.wait(), timeout=1.0)
        login_echo = original.sent[0]["echo"]

        # Model a replacement generation that has already authenticated as a
        # different account while the prior socket's action response is late.
        client._ws = replacement
        client._connection_generation += 1
        client._connected_account_id = "80002"
        client._identity_verified = True
        client._signal_identity_change()
        client._stop_event.set()
        await original.feed_json(
            {
                "echo": login_echo,
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": "90001"},
            }
        )

        await asyncio.wait_for(run_task, timeout=1.0)
        assert observed_at_disconnect == [("80002", True)]
        assert client._identity_verified is False
        assert client.connected_account_id == ""
        await original.close()
        await client._drain_event_tasks()

    asyncio.run(scenario())
