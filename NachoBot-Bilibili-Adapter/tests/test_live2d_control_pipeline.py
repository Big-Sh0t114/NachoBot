from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot"))

import bili_src.live2d.remote_controller as remote_controller_module  # noqa: E402
from bili_src.audio.tts_manager import TTSManager  # noqa: E402
from bili_src.live.outgoing_handler import OutgoingHandler  # noqa: E402
from bili_src.live.two_phase_search import (  # noqa: E402
    BilibiliLiveSearchOrchestrator,
    PublicWebSearch,
)
from bili_src.live2d.remote_controller import (  # noqa: E402
    PreparedReplyResult,
    RemoteLive2DController,
)


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _CaptureLogger:
    def __init__(self) -> None:
        self.records: list[str] = []

    def __getattr__(self, _name):
        def capture(message, *args, **kwargs):
            del kwargs
            rendered = str(message)
            try:
                rendered = rendered.format(*args)
            except (IndexError, KeyError, ValueError):
                rendered = " ".join([rendered, *(str(arg) for arg in args)])
            self.records.append(rendered)

        return capture

    @property
    def text(self) -> str:
        return "\n".join(self.records)


class RemoteLive2DControlTests(unittest.IsolatedAsyncioTestCase):
    def _controller(self) -> RemoteLive2DController:
        adapter = SimpleNamespace(
            config=SimpleNamespace(
                live_live2d_url="ws://127.0.0.1:8766",
                live_live2d_token="",
                live_live2d_reconnect_seconds=1,
            )
        )
        return RemoteLive2DController(adapter, _Logger())

    def test_fallback_only_extracts_reply_and_disables_search(self) -> None:
        fallback = RemoteLive2DController._fallback_prepare_reply(
            '{"reply":"普通回复","emotion":"angry",'
            '"action":"点头/同意","web_search":true,"search_query":"secret"}'
        )
        self.assertEqual(
            fallback,
            PreparedReplyResult("普通回复", False, "", None),
        )
        self.assertEqual(
            RemoteLive2DController._fallback_prepare_reply("普通文本").reply,
            "普通文本",
        )
        self.assertEqual(
            RemoteLive2DController._fallback_prepare_reply('{"reply":').reply,
            "",
        )
        self.assertEqual(
            RemoteLive2DController._fallback_prepare_reply(
                "model prefix: {\"reply\":\"嵌入回复\",\"meta\":{\"safe\":true}} suffix"
            ).reply,
            "嵌入回复",
        )

    def test_fallback_rejects_non_reply_structured_outputs(self) -> None:
        rejected = (
            '[{"reply":"array leak"}]',
            '```json\n[{"reply":"fenced array leak"}]\n```',
            '42',
            '"scalar leak"',
            '```json\n42\n```',
            '{"reply":42}',
            '{"other":"missing reply"}',
            '{"reply":"unterminated"',
            '```plain\nnot JSON\n```',
        )
        for raw_reply in rejected:
            with self.subTest(raw_reply=raw_reply):
                self.assertEqual(
                    RemoteLive2DController._fallback_prepare_reply(raw_reply).reply,
                    "",
                )

    async def test_correlated_response_resolves_only_matching_future(self) -> None:
        controller = self._controller()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        controller._pending_requests["request-1"] = future

        await controller._handle_interaction(
            json.dumps(
                {
                    "type": "avatar.interaction",
                    "version": "1.1",
                    "event": "reply_prepared",
                    "request_id": "request-1",
                    "payload": {
                        "reply": "ok",
                        "web_search": False,
                        "search_query": "",
                        "control_id": "request-1",
                    },
                }
            )
        )
        self.assertEqual((await future)["reply"], "ok")
        self.assertEqual(controller._pending_requests, {"request-1": future})

        await controller._handle_interaction(
            json.dumps(
                {
                    "type": "avatar.interaction",
                    "version": "1.1",
                    "event": "reply_prepared",
                    "request_id": "late-request",
                    "payload": {"reply": "ignored"},
                }
            )
        )
        self.assertFalse(controller._pending_requests.get("late-request"))

    async def test_ready_capabilities_are_gated(self) -> None:
        controller = self._controller()
        await controller._handle_interaction(
            json.dumps(
                {
                    "type": "avatar.interaction",
                    "version": "1.1",
                    "event": "ready",
                    "payload": {
                        "running": True,
                        "capabilities": {"prepare_reply": True, "apply_control": False},
                    },
                }
            )
        )
        self.assertTrue(controller.has_capability("prepare_reply"))
        self.assertFalse(controller.has_capability("apply_control"))

    async def test_transport_runtime_exception_degrades_prepare_and_apply(self) -> None:
        controller = self._controller()
        controller._capabilities.update({"prepare_reply", "apply_control"})
        controller._request = AsyncMock(side_effect=RuntimeError("send failed"))

        prepared = await controller.prepare_reply(
            '{"reply":"普通回复","emotion":"angry","action":"点头/同意"}'
        )
        self.assertEqual(prepared, PreparedReplyResult("普通回复", False, "", None))
        self.assertFalse(await controller.apply_control("opaque-control-id"))

    async def test_request_timeout_cancels_private_future_and_cleans_pending(self) -> None:
        controller = self._controller()
        controller.is_running = True
        controller._connected.set()
        controller._ready.set()
        controller._capabilities.add("prepare_reply")
        controller._active_websocket = SimpleNamespace(send=AsyncMock())
        seen_futures = []

        async def immediate_timeout(awaitable, *, timeout):
            del timeout
            seen_futures.extend(controller._pending_requests.values())
            awaitable.cancel()
            raise asyncio.TimeoutError

        with patch.object(remote_controller_module.asyncio, "wait_for", immediate_timeout):
            with self.assertRaises(asyncio.TimeoutError):
                await controller._request("prepare_reply", {"reply": "test"})

        self.assertEqual(len(seen_futures), 1)
        self.assertTrue(seen_futures[0].cancelled())
        self.assertEqual(controller._pending_requests, {})

    async def test_request_cancellation_cancels_private_future_and_cleans_pending(self) -> None:
        controller = self._controller()
        controller.is_running = True
        controller._connected.set()
        controller._ready.set()
        controller._capabilities.add("prepare_reply")

        class BlockingWebSocket:
            def __init__(self):
                self.sent = asyncio.Event()
                self.block = asyncio.get_running_loop().create_future()

            async def send(self, _message):
                self.sent.set()
                await self.block

        websocket = BlockingWebSocket()
        controller._active_websocket = websocket
        request_task = asyncio.create_task(
            controller._request("prepare_reply", {"reply": "test"})
        )
        await websocket.sent.wait()
        private_future = next(iter(controller._pending_requests.values()))

        request_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request_task

        self.assertTrue(private_future.cancelled())
        self.assertEqual(controller._pending_requests, {})

    async def test_ready_timeout_cancels_event_wait(self) -> None:
        controller = self._controller()
        controller._connected.set()

        class TrackingEvent:
            def __init__(self) -> None:
                self.cancelled = asyncio.Event()

            def is_set(self) -> bool:
                return False

            async def wait(self) -> None:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        tracking = TrackingEvent()
        controller._ready = tracking

        with patch.object(remote_controller_module, "REQUEST_TIMEOUT_SECONDS", 0.001):
            prepared = await controller.prepare_reply("ordinary fallback")

        self.assertEqual(prepared.reply, "ordinary fallback")
        await asyncio.wait_for(tracking.cancelled.wait(), timeout=0.1)


class SearchFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_search_reply_preserves_control_id_and_disables_search_fields(self) -> None:
        adapter = SimpleNamespace(
            config=SimpleNamespace(live_network_search_enabled=True),
            tts_manager=SimpleNamespace(is_tts_enabled=lambda _room_id: False),
        )
        search_client = SimpleNamespace(search=AsyncMock(return_value=""))
        orchestrator = BilibiliLiveSearchOrchestrator(
            adapter,
            _Logger(),
            search_client=search_client,
        )
        delivered = []

        async def deliver(prepared, room_id, reply_mid, reply_dmid):
            delivered.append((prepared, room_id, reply_mid, reply_dmid))

        handled = await orchestrator.handle(
            PreparedReplyResult("", True, "天气", "opaque-control-id"),
            room_id=100,
            reply_mid="mid",
            reply_dmid="dmid",
            deliver=deliver,
        )
        await orchestrator.wait_for_pending()

        self.assertTrue(handled)
        self.assertEqual(len(delivered), 1)
        prepared = delivered[0][0]
        self.assertEqual(prepared.reply, orchestrator.FALLBACK_REPLY)
        self.assertFalse(prepared.web_search)
        self.assertEqual(prepared.search_query, "")
        self.assertEqual(prepared.control_id, "opaque-control-id")
        search_client.search.assert_awaited_once_with("天气")


class PrivacyLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_logs_query_metadata_without_query_content(self) -> None:
        logger = _CaptureLogger()
        search = PublicWebSearch(logger)
        search._search_duckduckgo_html = AsyncMock(return_value=[])
        search._search_bing_rss = AsyncMock(return_value=[])

        secret_query = "SECRET_QUERY_VALUE"
        await search.search(secret_query)

        self.assertNotIn(secret_query, logger.text)
        self.assertIn("query_chars=18", logger.text)

    async def test_tts_and_danmu_logs_omit_reply_content(self) -> None:
        logger = _CaptureLogger()
        manager = TTSManager.__new__(TTSManager)
        manager.logger = logger
        manager.parse_bilingual_response("SECRET_TTS_REPLY")

        adapter = SimpleNamespace(
            _filter_outgoing_text=lambda text: text,
            tts_manager=SimpleNamespace(is_tts_enabled=lambda _room_id: False),
            api=SimpleNamespace(
                send_danmu=AsyncMock(return_value={"code": 0, "data": {"dmid": "1"}})
            ),
            _self_danmu_ids={},
            _self_danmu_texts={},
        )
        handler = OutgoingHandler.__new__(OutgoingHandler)
        handler.logger = logger
        handler.adapter = adapter

        secret_danmu = "SECRET_DANMU_REPLY"
        await handler._send_danmu(100, secret_danmu, "mid", "dmid")

        self.assertNotIn("SECRET_TTS_REPLY", logger.text)
        self.assertNotIn(secret_danmu, logger.text)
        self.assertIn("chars=18", logger.text)


class TTSControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_dict_without_prepare_callback_returns_legacy_reply(self) -> None:
        manager = TTSManager.__new__(TTSManager)
        manager.prepare_reply = None

        prepared = await manager._prepare_idle_reply(
            {"reply": "旧版待机文本", "emotion": "angry", "action": "点头/同意"}
        )

        self.assertEqual(prepared, "旧版待机文本")

    async def test_buffered_tts_activates_once_before_late_failure_fallback(self) -> None:
        room_id = 100
        manager = TTSManager.__new__(TTSManager)
        manager.config = SimpleNamespace(
            live_room_prompts={room_id: {"tts": {"enable": True}}},
            platform="bilibili.live",
        )
        manager.logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        manager.audio_player = SimpleNamespace(
            interrupt_idle=Mock(),
            play=Mock(),
        )
        manager.tts_model = object()
        manager._lang_overrides = {}
        manager._tts_buffer = {room_id: ["<JP>第一句。</JP><ZH>第一句。</ZH>"]}
        manager._tts_timer = {}
        manager._tts_metadata = {
            room_id: {
                "reply_mid": "mid",
                "reply_dmid": "dmid",
                "start_time": 0.0,
                "control_id": "opaque-control-id",
            }
        }
        manager.update_subtitle = Mock()
        manager.send_danmu = AsyncMock()
        manager.on_start_replying = AsyncMock()
        manager.on_reply_finished = AsyncMock()
        manager.apply_live2d_control = AsyncMock(return_value=True)
        manager._synthesize_tts_segment = AsyncMock(
            side_effect=[b"audio-1", b"audio-2", RuntimeError("later synthesis failed")]
        )

        with (
            patch("bili_src.audio.tts_manager._clean_text_for_tts", side_effect=lambda text: text),
            patch("bili_src.audio.tts_manager.resolve_emotion_preset_remote", None),
            patch(
                "nachobot_multimodal.utils.text_splitter.split_text_for_streaming",
                return_value=["segment-1", "segment-2", "segment-3"],
            ),
        ):
            await manager.process_buffered_live_reply(room_id)

        manager.on_start_replying.assert_awaited_once_with()
        manager.apply_live2d_control.assert_awaited_once_with("opaque-control-id")
        self.assertEqual(manager.audio_player.play.call_count, 2)
        manager.send_danmu.assert_awaited_once_with(room_id, "第一句。", "mid", "dmid")
        manager.on_reply_finished.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
