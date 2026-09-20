from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_ROOT = _ROOT / "NachoBot-SnowLuma-Adapter"
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(_ROOT / "NachoBot"))

from src.bridge import SnowLumaBridge  # noqa: E402


def test_platform_api_request_maps_qzone_like_action(monkeypatch):
    bridge = SnowLumaBridge()
    bridge.client.call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": None})
    bridge.router.send_custom_message = AsyncMock(return_value=True)

    asyncio.run(bridge.handle_platform_api_request({
        "content": {
            "version": 1,
            "request_id": "A" * 32,
            "operation": "like_qzone",
            "platform": "qq",
            "params": {"tid": "tid-1", "target_uin": 12345, "abstime": 0},
        }
    }))

    bridge.client.call_action.assert_awaited_once_with(
        "like_qzone", {"tid": "tid-1", "target_uin": 12345, "abstime": 0}
    )
    sent = bridge.router.send_custom_message.await_args.kwargs["message"]
    assert sent["status"] == "ok"
    assert sent["data"] == {"success": True}


def test_platform_api_request_maps_qzone_comment_action(monkeypatch):
    bridge = SnowLumaBridge()
    bridge.client.call_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": None})
    bridge.router.send_custom_message = AsyncMock(return_value=True)

    asyncio.run(bridge.handle_platform_api_request({
        "platform": "qq",
        "content": {
            "version": 1,
            "request_id": "B" * 32,
            "operation": "comment_qzone",
            "platform": "qq",
            "params": {"tid": "tid-2", "target_uin": 12345, "content": "hello"},
        },
    }))

    bridge.client.call_action.assert_awaited_once_with(
        "comment_qzone", {"tid": "tid-2", "target_uin": 12345, "content": "hello"}
    )
    sent = bridge.router.send_custom_message.await_args.kwargs["message"]
    assert sent["status"] == "ok"
    assert sent["data"] == {"success": True}


def test_invalid_known_request_returns_error_instead_of_timing_out(monkeypatch):
    bridge = SnowLumaBridge()
    bridge.client.call_action = AsyncMock()
    bridge.router.send_custom_message = AsyncMock(return_value=True)

    asyncio.run(bridge.handle_platform_api_request({
        "platform": "qq",
        "content": {
            "version": 1,
            "request_id": "C" * 32,
            "operation": "comment_qzone",
            "platform": "qq",
            "params": {"tid": "tid-3", "target_uin": "12345", "content": "hello"},
        },
    }))

    bridge.client.call_action.assert_not_awaited()
    sent = bridge.router.send_custom_message.await_args.kwargs["message"]
    assert sent["status"] == "error"
    assert sent["error"] == {"code": "invalid_request"}
