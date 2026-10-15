import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.plugin_system.apis import delivery_api as delivery


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    monkeypatch.setattr(delivery, "_providers", {})


def message(platform="example", kind="session"):
    return SimpleNamespace(message_info=SimpleNamespace(
        platform=platform, additional_config={"delivery_target": {"kind": kind}},
    ))


def test_platform_scoped_provider_and_ordinary_transport_are_independent():
    async def run():
        api = SimpleNamespace(send_message=AsyncMock(return_value=None))
        send = AsyncMock(return_value=True)
        delivery.register_delivery_provider("example", "session", send, enabled=lambda: True)
        assert await delivery.send_message(api, message())
        send.assert_awaited_once()
        api.send_message.assert_not_awaited()
        for ordinary in (message(kind="other"), message(platform="another")):
            assert await delivery.send_message(api, ordinary)
            assert not delivery.requires_audio_success(ordinary)
        assert api.send_message.await_count == 2
        assert delivery.requires_audio_success(message())
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["disabled", "exception", "false", "unloaded"])
def test_provider_failure_never_falls_back_to_transport(failure):
    async def run():
        api = SimpleNamespace(send_message=AsyncMock())
        send = AsyncMock(return_value=failure != "false")
        if failure == "exception":
            send.side_effect = RuntimeError("private upstream details")
        delivery.register_delivery_provider(
            "example", "session", send, enabled=lambda: failure != "disabled",
        )
        if failure == "unloaded":
            delivery.unregister_delivery_provider("example", "session")
        with pytest.raises(delivery.DeliveryRejected) as error:
            await delivery.send_message(api, message())
        assert error.value.fallback_allowed is False
        assert "private upstream" not in str(error.value)
        api.send_message.assert_not_awaited()
    asyncio.run(run())


def test_unload_during_send_rejects_late_success_and_reload_restores_route():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def send(api, packet):
            started.set()
            await release.wait()
            return True
        enabled = lambda: True
        api = SimpleNamespace(send_message=AsyncMock())
        delivery.register_delivery_provider("example", "session", send, enabled=enabled)
        pending = asyncio.create_task(delivery.send_message(api, message()))
        await started.wait()
        delivery.unregister_delivery_provider("example", "session")
        delivery.register_delivery_provider("example", "session", send, enabled=enabled)
        release.set()
        with pytest.raises(delivery.DeliveryRejected, match="changed"):
            await pending
        assert await delivery.send_message(api, message())
        api.send_message.assert_not_awaited()
    asyncio.run(run())


def test_idempotent_registration_preserves_inflight_send_and_cancellation():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def send(api, packet):
            started.set()
            await release.wait()
            return True
        enabled = lambda: True
        delivery.register_delivery_provider("example", "session", send, enabled=enabled)
        task = asyncio.create_task(delivery.send_message(None, message()))
        await started.wait()
        delivery.register_delivery_provider("example", "session", send, enabled=enabled)
        release.set()
        assert await task
        release.clear()
        task = asyncio.create_task(delivery.send_message(None, message()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_core_does_not_depend_on_qqcall_protocol_or_plugin():
    core = Path(__file__).resolve().parents[1] / "src"
    offenders = [str(p.relative_to(core)) for p in core.rglob("*.py")
                 if "qqcall" in p.read_text(encoding="utf-8").lower()]
    assert offenders == []
