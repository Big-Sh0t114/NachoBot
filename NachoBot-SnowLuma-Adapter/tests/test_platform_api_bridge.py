from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(_ROOT / "NachoBot"))

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
