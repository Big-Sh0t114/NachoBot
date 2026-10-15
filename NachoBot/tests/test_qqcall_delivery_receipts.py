import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ncnk_message import Seg
from plugins.qqcall_plugin import delivery as receipts
from src.chat.message_receive.uni_message_sender import _relay_tts_text, _send_message
from src.multimodal.contracts import TTSStreamChunk, TTSStreamSpec


def message(kind="qqcall_group", segment=None):
    return SimpleNamespace(
        message_info=SimpleNamespace(
            platform="qq", message_id="parent", time=1, group_info=None, user_info=None,
            additional_config={"delivery_target": {"kind": kind, "turn_id": "turn"}},
        ),
        message_segment=segment or Seg(type="text", data="reply"),
        processed_plain_text="reply",
    )


class Api:
    def __init__(self, reject=None, respond=True):
        self.handler = None
        self.sent = []
        self.reject = reject
        self.respond = respond
        self.transport = True
        self.submitted = asyncio.Event()

    def register_custom_message_handler(self, name, handler):
        assert name == "qqcall_delivery_response"
        self.handler = handler

    async def send_message(self, packet):
        self.sent.append(packet)
        self.submitted.set()
        event = packet.message_segment.data.get("event") if packet.message_segment.type == "voice_stream" else None
        if self.respond:
            code = "display_text_acknowledged" if packet.message_segment.type == "text" else "local_endpoint_queued"
            envelope = {
                "version": 1, "platform": "qq", "status": "accepted",
                "request_id": packet.message_info.additional_config["delivery_target"]["request_id"],
                "result": {"code": code, "drain_confirmed": False},
            }
            if event == self.reject and self.reject is not None:
                envelope.pop("result")
                envelope.update(status="error", error={"code": "stale_group_scope"})
            await self.handler({"platform": "qq", "content": envelope})
        return self.transport


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.plugin_system.apis import delivery_api

        self.providers = dict(delivery_api._providers)
        receipts.register(lambda: True)

    async def asyncTearDown(self):
        from src.plugin_system.apis import delivery_api

        receipts.unregister()
        delivery_api._providers.clear()
        delivery_api._providers.update(self.providers)
        self.assertEqual(receipts._pending, {})

    async def test_private_and_group_receipts_preserve_parent_target_and_are_unique(self):
        api = Api()
        for kind in ("qqcall", "qqcall_group"):
            parent = message(kind)
            original = copy.deepcopy(parent.message_info.additional_config)
            self.assertTrue(await receipts.send_with_qqcall_receipt(api, parent))
            self.assertEqual(parent.message_info.additional_config, original)
            self.assertIsNot(api.sent[-1].message_info, parent.message_info)
        ids = [p.message_info.additional_config["delivery_target"]["request_id"] for p in api.sent]
        self.assertEqual(len(set(ids)), 2)

    async def test_wrong_platform_version_id_and_forged_success_do_not_succeed(self):
        for change in ("platform", "version", "request_id", "code", "drain_confirmed", "conflicting_error"):
            with self.subTest(change=change):
                api = Api(respond=False)
                task = asyncio.create_task(receipts.send_with_qqcall_receipt(api, message()))
                await api.submitted.wait()
                rid = api.sent[-1].message_info.additional_config["delivery_target"]["request_id"]
                envelope = {"version": 1, "platform": "qq", "request_id": rid,
                            "status": "accepted", "result": {"code": "display_text_acknowledged", "drain_confirmed": False}}
                if change in {"platform", "version", "request_id"}:
                    envelope[change] = {"platform": "discord", "version": True, "request_id": "unknown"}[change]
                elif change == "conflicting_error":
                    envelope["error"] = {"code": "stale_group_scope"}
                else:
                    envelope["result"][change] = "unknown" if change == "code" else True
                await api.handler({"platform": "qq", "content": envelope})
                if change in {"platform", "version", "request_id"}:
                    self.assertFalse(task.done())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(receipts.QQCallDeliveryRejected):
                        await task

    async def test_timeout_cancellation_and_false_transport_clean_up_waiters(self):
        api = Api(respond=False)
        with patch.object(receipts, "_ACK_TIMEOUT", 0.01):
            with self.assertRaisesRegex(receipts.QQCallDeliveryRejected, "timeout"):
                await receipts.send_with_qqcall_receipt(api, message())
        api.respond = True
        api.transport = False
        with self.assertRaises(receipts.QQCallDeliveryRejected):
            await receipts.send_with_qqcall_receipt(api, message())
        self.assertEqual(receipts._pending, {})

    async def test_rejected_display_does_not_claim_transport_success(self):
        api = Api()
        api.transport = False
        with patch("src.chat.message_receive.uni_message_sender.get_global_api", return_value=api):
            self.assertFalse(await _send_message(message()))

    async def test_plugin_unload_releases_waiter_and_rejects_late_receipt(self):
        from plugins.qqcall_plugin.plugin import QQCallPlugin
        from src.plugin_system.apis import delivery_api

        api = Api(respond=False)
        pending = asyncio.create_task(delivery_api.send_message(api, message()))
        await api.submitted.wait()
        request_id = api.sent[0].message_info.additional_config["delivery_target"]["request_id"]
        QQCallPlugin.on_plugin_unload(SimpleNamespace(
            _unregister_realtime_provider=QQCallPlugin._unregister_realtime_provider,
        ))
        with self.assertRaises(delivery_api.DeliveryRejected):
            await pending
        with self.assertRaises(delivery_api.DeliveryRejected):
            await delivery_api.send_message(api, message())
        await api.handler({"platform": "qq", "content": {
            "version": 1, "platform": "qq", "status": "accepted", "request_id": request_id,
            "result": {"code": "display_text_acknowledged", "drain_confirmed": False},
        }})
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(receipts._pending, {})

    async def test_stale_start_or_chunk_closes_tts_without_consuming_more_or_buffering(self):
        for reject in ("start", "chunk"):
            with self.subTest(reject=reject):
                api = Api(reject=reject)
                state = {"closed": False, "yielded": 0, "buffered": 0}

                async def stream(*args, **kwargs):
                    try:
                        for _ in range(20):
                            state["yielded"] += 1
                            yield TTSStreamChunk(pcm_s16le=b"\x01\x00" * 1600,
                                                 spec=TTSStreamSpec(sample_rate=16000, channels=1, sample_width=2))
                    finally:
                        state["closed"] = True

                async def buffered(*args, **kwargs):
                    state["buffered"] += 1

                router = SimpleNamespace(synthesize_tts_stream=stream, synthesize_tts=buffered)
                with patch("src.chat.message_receive.uni_message_sender.get_global_api", return_value=api):
                    self.assertFalse(await _relay_tts_text(message(), router, "reply", "zh"))
                self.assertEqual(state, {"closed": True, "yielded": 1, "buffered": 0})
                events = [p.message_segment.data["event"] for p in api.sent]
                self.assertEqual(events, ["start", "abort"] if reject == "start" else ["start", "chunk", "abort"])

    async def test_ordinary_messages_keep_transport_behavior_and_need_no_handler(self):
        api = SimpleNamespace(send_message=lambda _: None)
        async def send(_):
            return None
        api.send_message = send
        self.assertTrue(await receipts.send_with_qqcall_receipt(api, message("ordinary")))
