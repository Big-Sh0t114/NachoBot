import json
import time
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from config import load_config
from main import create_app
from signature import build_callback_signature


class CallbackEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_comment_is_acked_and_queued(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        config = replace(
            config,
            douyin=replace(config.douyin, callback_secret="test-secret"),
        )
        app = create_app(config)
        body = json.dumps(
            [
                {
                    "msg_id": "callback-1",
                    "sec_openid": "user-1",
                    "nickname": "测试观众",
                    "content": "主播你好",
                    "timestamp": int(time.time() * 1000),
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        headers = {
            "x-msg-type": "live_comment",
            "x-roomid": "room-1",
            "x-nonce-str": "nonce-1",
            "x-timestamp": str(int(time.time() * 1000)),
        }
        headers["x-signature"] = build_callback_signature(headers, body, "test-secret")
        response = await self._invoke(app, body, headers)
        self.assertEqual(response, {"ok": True, "accepted": 1})
        self.assertEqual(app.state.adapter.queue.qsize(), 1)

    async def test_invalid_signature_is_rejected(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        config = replace(
            config,
            douyin=replace(config.douyin, callback_secret="test-secret"),
        )
        app = create_app(config)
        with self.assertRaises(HTTPException) as context:
            await self._invoke(
                app,
                b"[]",
                {
                    "x-msg-type": "live_comment",
                    "x-roomid": "room-1",
                    "x-nonce-str": "nonce-1",
                    "x-timestamp": str(int(time.time() * 1000)),
                    "x-signature": "invalid",
                },
            )
        self.assertEqual(context.exception.status_code, 401)

    @staticmethod
    async def _invoke(app, body: bytes, headers: dict[str, str]):
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/douyin/live/callback",
            "raw_path": b"/douyin/live/callback",
            "query_string": b"",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8788),
        }
        request = Request(scope, receive)
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", None) == "/douyin/live/callback"
        )
        return await endpoint(request)


if __name__ == "__main__":
    unittest.main()
