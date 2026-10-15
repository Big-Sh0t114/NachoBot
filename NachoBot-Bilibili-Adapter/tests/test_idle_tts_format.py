from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot"))

import bili_src.audio.tts_manager as tts_manager_module  # noqa: E402
from bili_src.audio.tts_manager import TTSManager  # noqa: E402


class _CoreClient:
    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events = events

    async def synthesize_tts(self, text: str, **kwargs):
        self.calls.append((text, kwargs))
        if self.events is not None:
            self.events.append(("synthesize", text))
        return {
            "audio_base64": base64.b64encode(text.encode("utf-8")).decode("ascii")
        }


class IdleTTSFormatTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one_idle_cycle(self, preset, prepare_reply=None):
        temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        subtitle_path = Path(temp_dir) / "subtitle.txt"
        events: list[tuple[str, str]] = []
        client = _CoreClient(events)

        def play_idle(audio):
            events.append(("play", audio.decode("utf-8")))

        player = SimpleNamespace(
            is_playing=False,
            play_idle=Mock(side_effect=play_idle),
        )
        manager = TTSManager.__new__(TTSManager)
        manager.config = SimpleNamespace(
            idle_tts_texts=[preset],
            idle_tts_enable=True,
            idle_tts_min_seconds=10,
            idle_tts_max_seconds=10,
            platform="bilibili.live",
        )
        manager.logger = SimpleNamespace(
            info=Mock(), warning=Mock(), error=Mock()
        )
        manager.audio_player = player
        manager.multimodal_client = client
        manager.prepare_reply = prepare_reply
        manager.subtitle_path = str(subtitle_path)
        manager.on_start_replying = AsyncMock(
            side_effect=lambda: events.append(("start", ""))
        )

        async def apply_control(control_id):
            events.append(("control", control_id))

        manager.apply_live2d_control = AsyncMock(side_effect=apply_control)
        manager._last_active_time = 0.0
        manager._next_idle_target = 0.0
        manager.reset_idle_timer = Mock()

        sleep_count = 0
        cycle_finished = asyncio.Event()

        async def controlled_sleep(_delay):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                return
            cycle_finished.set()
            await asyncio.Future()

        with (
            patch.object(tts_manager_module.asyncio, "sleep", controlled_sleep),
            patch.object(tts_manager_module.time, "time", return_value=10.0),
            patch.object(tts_manager_module, "_clean_text_for_tts", lambda text: text),
        ):
            task = asyncio.create_task(manager.idle_tts_loop())
            try:
                await asyncio.wait_for(cycle_finished.wait(), timeout=2.0)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        subtitle = subtitle_path.read_text(encoding="utf-8-sig") if subtitle_path.exists() else None
        return manager, client, events, subtitle

    async def test_dict_and_json_string_presets_use_explicit_tts_and_original_envelope(self):
        cases = (
            (
                {
                    "reply": "观众看到的日文测试甲。",
                    "tts_text": {"text": "これは最初の日本語テスト文です。こちらは二つ目の日本語テスト文です。", "lang": "ja"},
                    "emotion": "normal",
                    "action": "点头/同意",
                },
                "ja",
            ),
            (
                {
                    "reply": "观众看到的中文测试乙。",
                    "tts_text": {"text": "这是第一段中文测试文本。接下来这是第二段中文测试文本。", "lang": "zh"},
                    "emotion": "shy",
                    "action": "待机/放松",
                },
                "zh",
            ),
        )
        for index, (payload, lang) in enumerate(cases):
            with self.subTest(index=index):
                preset = payload if index == 0 else json.dumps(payload, ensure_ascii=False)
                received_envelopes = []

                async def prepare(raw):
                    received_envelopes.append(raw)
                    normalized = json.loads(raw)
                    return SimpleNamespace(
                        reply=normalized["reply"],
                        control_id="opaque-control-id",
                    )

                manager, client, events, subtitle = await self._run_one_idle_cycle(
                    preset, prepare
                )

                self.assertEqual(len(received_envelopes), 1)
                self.assertEqual(json.loads(received_envelopes[0]), payload)
                if isinstance(preset, str):
                    self.assertEqual(received_envelopes[0], preset)
                self.assertEqual(subtitle, payload["reply"])

                self.assertNotIn('"tts_text"', subtitle)
                self.assertEqual(
                    [call[1]["text_lang"] for call in client.calls],
                    [lang, lang],
                )
                self.assertEqual(
                    [call[1]["platform"] for call in client.calls],
                    ["bilibili.live", "bilibili.live"],
                )
                self.assertEqual(
                    "".join(call[0] for call in client.calls),
                    payload["tts_text"]["text"],
                )
                self.assertEqual(manager.on_start_replying.await_count, 1)
                manager.apply_live2d_control.assert_awaited_once_with(
                    "opaque-control-id"
                )
                self.assertEqual(
                    [event[0] for event in events],
                    ["synthesize", "start", "control", "play", "synthesize", "play"],
                )

    async def test_no_prepare_callback_decodes_dict_and_json_string_presets(self):
        cases = (
            {
                "reply": "无回调字典字幕。",
                "tts_text": "这是字典预设的显式语音。",
                "emotion": "normal",
                "action": "点头/同意",
            },
            json.dumps(
                {
                    "reply": "无回调JSON字幕。",
                    "tts_text": {"text": "これはJSONプリセットの音声です。", "lang": "ja"},
                    "emotion": "shy",
                    "action": "待机/放松",
                },
                ensure_ascii=False,
            ),
        )
        expected = (
            ("这是字典预设的显式语音。", None, "无回调字典字幕。"),
            ("これはJSONプリセットの音声です。", "ja", "无回调JSON字幕。"),
        )
        for preset, (speech, lang, display) in zip(cases, expected):
            with self.subTest(display=display):
                manager, client, _events, subtitle = await self._run_one_idle_cycle(
                    preset
                )

                self.assertEqual(subtitle, display)
                self.assertEqual([call[0] for call in client.calls], [speech])
                self.assertEqual(client.calls[0][1]["text_lang"], lang)
                manager.apply_live2d_control.assert_not_awaited()

    async def test_legacy_plain_and_json_reply_presets_keep_reply_text(self):
        cases = (
            ("旧版纯文本待机语音。", "旧版纯文本待机语音。", "旧版纯文本待机语音。"),
            (
                json.dumps(
                    {
                        "reply": "旧版JSON待机回复文本。",
                        "emotion": "normal",
                        "action": "点头/同意",
                    },
                    ensure_ascii=False,
                ),
                "旧版JSON待机回复文本。",
                "旧版JSON待机回复文本。",
            ),
        )
        for preset, speech, display in cases:
            with self.subTest(display=display):
                _manager, client, _events, subtitle = await self._run_one_idle_cycle(
                    preset
                )

                self.assertEqual(subtitle, display)
                self.assertEqual([call[0] for call in client.calls], [speech])
                self.assertIsNone(client.calls[0][1]["text_lang"])

    async def test_invalid_explicit_tts_text_is_skipped_without_stringifying(self):
        preset = {
            "reply": "不应写入字幕。",
            "tts_text": {"text": {"nested": "invalid"}, "lang": "ja"},
            "emotion": "normal",
            "action": "待机/放松",
        }
        manager, client, _events, subtitle = await self._run_one_idle_cycle(preset)

        self.assertEqual(client.calls, [])
        self.assertIsNone(subtitle)
        manager.audio_player.play_idle.assert_not_called()
        manager.logger.warning.assert_called_once_with(
            "Skipping idle preset with invalid tts_text"
        )

if __name__ == "__main__":
    unittest.main()
