from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.plugin_system.apis import platform_api  # noqa: E402


class _FakeCoreAPI:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.requests: list[tuple[str, str, dict]] = []
        self.block_send = False
        self.send_started = asyncio.Event()

    async def send_custom_message(self, platform: str, message_type: str, content: dict) -> bool:
        self.requests.append((platform, message_type, content))
        self.send_started.set()
        if self.block_send:
            await asyncio.Future()
        return self.result


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_pending_requests():
    platform_api._pending_requests.clear()
    yield
    assert platform_api.pending_request_count() == 0


def test_cookie_request_uses_correlated_platform_envelope_and_parses_equals_once(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(
            platform_api.get_platform_cookies(" User.QZone.QQ.com ", platform="qq", timeout=1)
        )
        await fake.send_started.wait()
        request = fake.requests[0]
        assert request[0] == "qq"
        assert request[1] == platform_api.PLATFORM_API_REQUEST_TYPE
        envelope = request[2]
        assert envelope["platform"] == "qq"
        assert envelope["operation"] == platform_api.GET_PLATFORM_COOKIES_OPERATION
        assert isinstance(envelope["request_id"], str)
        assert len(envelope["request_id"]) >= 20
        await platform_api.handle_platform_api_response(
            {
                "platform": "qq",
                "content": {
                    "version": 1,
                    "request_id": envelope["request_id"],
                    "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "a=one=two; b = three ; malformed"},
                },
            }
        )
        assert await task == {"a": "one=two", "b": "three"}

    _run(scenario())


def test_platform_none_uses_configured_platform_with_multiple_transports(monkeypatch):
    fake = _FakeCoreAPI()
    fake.connection = SimpleNamespace(platform_websockets={"qq": object(), "other": object()})
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)
    monkeypatch.setattr(
        platform_api,
        "global_config",
        SimpleNamespace(bot=SimpleNamespace(platform=" qq ")),
    )

    async def scenario():
        task = asyncio.create_task(platform_api.get_platform_cookies("example.com", timeout=1))
        await fake.send_started.wait()
        request = fake.requests[0]
        assert request[0] == "qq"
        envelope = request[2]
        await platform_api.handle_platform_api_response(
            {
                "platform": "qq",
                "content": {
                    "version": 1,
                    "request_id": envelope["request_id"],
                    "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "sid=value"},
                },
            }
        )
        assert await task == {"sid": "value"}

    _run(scenario())


def test_empty_configured_platform_fails_before_transport(monkeypatch):
    fake = _FakeCoreAPI()
    fake.connection = SimpleNamespace(platform_websockets={"qq": object()})
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)
    monkeypatch.setattr(
        platform_api,
        "global_config",
        SimpleNamespace(bot=SimpleNamespace(platform="  ")),
    )

    async def scenario():
        with pytest.raises(platform_api.PlatformAPIError, match="platform is not configured"):
            await platform_api.get_platform_cookies("example.com")
        assert fake.requests == []

    _run(scenario())


def test_spoofed_platform_response_does_not_resolve_waiter(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(platform_api.get_platform_cookies("example.com", platform="qq", timeout=1))
        await fake.send_started.wait()
        request_id = fake.requests[0][2]["request_id"]
        await platform_api.handle_platform_api_response(
            {
                "platform": "other",
                "content": {"request_id": request_id, "status": "ok", "data": {"cookies": "x=y"}},
            }
        )
        await asyncio.sleep(0)
        assert not task.done()
        await platform_api.handle_platform_api_response(
            {
                "platform": "qq",
                "content": {
                    "version": 1,
                    "request_id": request_id,
                    "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "x=y"},
                },
            }
        )
        assert await task == {"x": "y"}

    _run(scenario())


def test_omitted_response_version_is_ignored_until_valid_response(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(platform_api.get_platform_cookies("example.com", platform="qq", timeout=1))
        await fake.send_started.wait()
        request_id = fake.requests[0][2]["request_id"]
        omitted_version = {
            "platform": "qq",
            "content": {
                "request_id": request_id,
                "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                "platform": "qq",
                "status": "ok",
                "data": {"cookies": "x=spoof"},
            },
        }
        await platform_api.handle_platform_api_response(omitted_version)
        await asyncio.sleep(0)
        assert not task.done()
        assert platform_api.pending_request_count() == 1
        await platform_api.handle_platform_api_response(
            {
                "platform": "qq",
                "content": {
                    "version": 1,
                    "request_id": request_id,
                    "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "x=valid"},
                },
            }
        )
        assert await task == {"x": "valid"}

    _run(scenario())


def test_wrong_response_version_terminally_fails_matching_request(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(platform_api.get_platform_cookies("example.com", platform="qq", timeout=1))
        await fake.send_started.wait()
        request_id = fake.requests[0][2]["request_id"]
        await platform_api.handle_platform_api_response(
            {
                "platform": "qq",
                "content": {
                    "version": 2,
                    "request_id": request_id,
                    "operation": platform_api.GET_PLATFORM_COOKIES_OPERATION,
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "x=wrong"},
                },
            }
        )
        with pytest.raises(platform_api.PlatformAPIError, match="unsupported platform API response"):
            await task
        assert platform_api.pending_request_count() == 0

    _run(scenario())


@pytest.mark.parametrize("terminal", ["send_failure", "malformed", "timeout", "cancel"])
def test_pending_state_is_removed_on_every_terminal_path(monkeypatch, terminal):
    fake = _FakeCoreAPI(result=terminal != "send_failure")
    fake.block_send = terminal == "cancel"
    monkeypatch.setattr(platform_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(platform_api.get_platform_cookies("example.com", platform="qq", timeout=0.02))
        await fake.send_started.wait()
        if terminal == "malformed":
            request_id = fake.requests[0][2]["request_id"]
            await platform_api.handle_platform_api_response(
                {
                    "platform": "qq",
                    "content": {
                        "version": 1,
                        "request_id": request_id,
                        "status": "ok",
                        "data": {"cookies": []},
                    },
                }
            )
            assert platform_api.pending_request_count() == 0
            with pytest.raises(platform_api.PlatformAPIError):
                await task
        elif terminal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(platform_api.PlatformAPIError):
                await task

    _run(scenario())
