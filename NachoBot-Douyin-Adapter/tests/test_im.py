import json
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from config import load_config
from main import create_app
from signature import build_webhook_signature


class FakeIMClient:
    def __init__(self):
        self.calls = []

    async def send_text(self, context, text):
        self.calls.append((context, text))
        return {"data": {"error_code": 0}}


class IMWebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(
            config,
            douyin=replace(config.douyin, app_id="test-app"),
            im=replace(
                config.im,
                enabled=True,
                client_key="test-app",
                webhook_secret="webhook-secret",
                access_token="access-token",
                operator_open_id="operator-1",
            ),
        )
        self.app = create_app(self.config)

    async def test_verification_challenge(self):
        payload = {"event": "verify_webhook", "content": {"challenge": 12345}}
        response = await self._post(payload)
        self.assertEqual(response, {"challenge": 12345})

    async def test_text_private_message_is_queued_as_private_chat(self):
        response = await self._post(self._private_payload())
        self.assertEqual(response, {"ok": True, "accepted": 1})
        event, conversation, payload = self.app.state.adapter.queue.get_nowait()
        message = self.app.state.adapter._build_message(event, conversation, payload)
        self.assertEqual(message.message_info.platform, "douyin.private")
        self.assertIsNone(message.message_info.group_info)
        self.assertEqual(message.message_info.user_info.user_id, "viewer-1")
        self.assertEqual(message.message_segment.data, "你好")

    async def test_sent_event_is_acked_without_queueing(self):
        payload = self._private_payload()
        payload["event"] = "im_send_msg"
        response = await self._post(payload)
        self.assertEqual(response, {"ok": True, "accepted": 0})
        self.assertEqual(self.app.state.adapter.queue.qsize(), 0)

    async def test_invalid_signature_is_rejected(self):
        with self.assertRaises(HTTPException) as context:
            await self._post(self._private_payload(), signature="bad")
        self.assertEqual(context.exception.status_code, 401)

    async def test_core_private_reply_uses_im_api_not_live_output(self):
        payload = self._private_payload()
        adapter = self.app.state.adapter
        adapter.enqueue_private_payload(payload)
        event, conversation, queued = adapter.queue.get_nowait()
        message = adapter._build_message(event, conversation, queued)
        fake = FakeIMClient()
        adapter.im_client = fake
        await adapter.handle_from_nachobot(message.to_dict())
        await adapter.handle_from_nachobot(message.to_dict())
        self.assertEqual(len(fake.calls), 1)
        context, text = fake.calls[0]
        self.assertEqual(context.user_open_id, "viewer-1")
        self.assertEqual(context.message_id, "server-message-1")
        self.assertEqual(text, "你好")

    @staticmethod
    def _private_payload():
        return {
            "event": "im_receive_msg",
            "client_key": "test-app",
            "msg_id": "webhook-event-1",
            "from_user_id": "viewer-1",
            "to_user_id": "operator-1",
            "content": json.dumps(
                {
                    "conversation_short_id": "conversation-1",
                    "server_message_id": "server-message-1",
                    "conversation_type": 1,
                    "message_type": "text",
                    "text": "你好",
                    "create_time": 1710000000000,
                    "user_infos": [{"open_id": "viewer-1", "nick_name": "测试用户"}],
                },
                ensure_ascii=False,
            ),
        }

    async def _post(self, payload, signature=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        signature = signature or build_webhook_signature(body, "webhook-secret")
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
            "path": "/douyin/im/webhook",
            "raw_path": b"/douyin/im/webhook",
            "query_string": b"",
            "headers": [(b"x-douyin-signature", signature.encode("ascii"))],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8788),
        }
        request = Request(scope, receive)
        endpoint = next(
            route.endpoint
            for route in self.app.routes
            if getattr(route, "path", None) == "/douyin/im/webhook"
        )
        return await endpoint(request)


if __name__ == "__main__":
    unittest.main()
