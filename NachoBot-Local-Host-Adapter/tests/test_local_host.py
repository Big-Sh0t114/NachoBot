from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ADAPTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_DIR))

from adapter import LocalHostAdapter
from config import (
    AppConfig,
    AutoAnnouncementConfig,
    BrowserTTSConfig,
    Live2DConfig,
    NachoBotConfig,
    NeuralTTSConfig,
    OutputConfig,
    ServerConfig,
    TTSConfig,
    VRMConfig,
)
from outputs import ReplyOutput


def make_config(subtitle_file: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=8789, log_level="INFO"),
        nachobot=NachoBotConfig(
            host="127.0.0.1", port=8000, platform="local.host", studio_id="studio",
            host_user_id="host", host_nickname="主播", reply_prompt="简短回答", 
            network_search_enabled=False, person_profile_enabled=False,
        ),
        output=OutputConfig(
            subtitle_file=subtitle_file,
            speech_file=subtitle_file.with_suffix(".mp3"),
            console=False,
        ),
        tts=TTSConfig(enabled=False, url="http://127.0.0.1:8070/api/tts", play_local=False, timeout_seconds=5),
        neural_tts=NeuralTTSConfig(
            enabled=False,
            voice="zh-CN-XiaoxiaoNeural",
            rate="+0%",
            pitch="+2Hz",
            volume="+0%",
        ),
        browser_tts=BrowserTTSConfig(
            enabled=True,
            language="zh-CN",
            preferred_voice="Microsoft Yaoyao",
            rate=1.0,
            pitch=1.0,
            volume=1.0,
        ),
        auto_announcements=AutoAnnouncementConfig(
            enabled=False, first_delay_seconds=30, interval_seconds=30, messages=()
        ),
        live2d=Live2DConfig(enabled=False, url="ws://127.0.0.1:8766", token=""),
        vrm=VRMConfig(enabled=False, model_file=subtitle_file.parent / "xingyu-host-v1.vrm"),
    )


class LocalHostTests(unittest.TestCase):
    def test_manual_prompt_maps_to_local_host_message(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalHostAdapter(make_config(Path(directory) / "subtitle.txt"))
            message = adapter._build_message("介绍一下今天的主题")
        self.assertEqual(message.message_info.platform, "local.host")
        self.assertEqual(message.message_info.group_info.group_id, "studio")
        self.assertEqual(message.message_info.user_info.user_id, "host")
        self.assertIn("介绍一下今天的主题", message.message_segment.data)
        self.assertIn("回答风格要求", message.message_segment.data)
        self.assertIsNone(message.message_info.template_info)
        self.assertEqual(
            message.message_info.additional_config["runtime_capabilities"]["reply_delivery"],
            "json_envelope",
        )
        self.assertFalse(
            message.message_info.additional_config["runtime_capabilities"]["mid_term_memory"]
        )
        self.assertEqual(
            message.message_info.additional_config["runtime_capabilities"]["reply_model_group"],
            "realtime_replyer",
        )

    def test_direct_announcement_writes_obs_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(config.output, config.tts, config.neural_tts, config.live2d)
            result = asyncio.run(output.deliver("<ZH>晚上好</ZH>"))
            self.assertEqual(result, "晚上好")
            self.assertEqual(subtitle.read_text(encoding="utf-8"), "晚上好")

    def test_closed_mouth_delivery_writes_text_without_synthesizing(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(config.tts, enabled=True),
                config.neural_tts,
                config.live2d,
            )

            async def fail_if_called(*_args, **_kwargs):
                raise AssertionError("closed-mouth delivery must not synthesize audio")

            output._synthesize = fail_if_called
            result = asyncio.run(
                output.deliver("只显示文字", synthesize_audio=False)
            )

            self.assertEqual(result, "只显示文字")
            self.assertEqual(subtitle.read_text(encoding="utf-8"), "只显示文字")
            self.assertFalse(output.audio_ready)

    def test_voxcpm_is_preferred_and_uses_configured_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(config.tts, enabled=True, timeout_seconds=180),
                config.neural_tts,
                config.live2d,
            )
            observed_timeout = []
            original_wait_for = asyncio.wait_for

            async def synthesize_voxcpm(*_args, **_kwargs):
                return b"RIFF0000WAVEvoxcpm"

            async def capture_wait_for(awaitable, *, timeout):
                observed_timeout.append(timeout)
                return await original_wait_for(awaitable, timeout=timeout)

            def fail_sapi(_text):
                raise AssertionError("SAPI must not run when VoxCPM succeeds")

            output._synthesize = synthesize_voxcpm
            output._synthesize_sapi = fail_sapi
            with patch("outputs.asyncio.wait_for", capture_wait_for):
                asyncio.run(output.deliver("优先使用本地神经语音"))

            self.assertEqual(observed_timeout, [180.0])
            self.assertEqual(output.audio_source, "local_voxcpm")
            self.assertTrue(output.audio_ready)

    def test_sapi_is_used_only_after_voxcpm_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(config.tts, enabled=True),
                config.neural_tts,
                config.live2d,
            )

            async def unavailable_voxcpm(*_args, **_kwargs):
                return b""

            output._synthesize = unavailable_voxcpm
            output._synthesize_sapi = lambda _text: b"RIFF0000WAVEsapi"
            asyncio.run(output.deliver("神经语音失败后回退"))

            self.assertEqual(output.audio_source, "local_sapi")
            self.assertTrue(output.audio_ready)

    def test_live2d_prefers_streaming_voxcpm_path(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(config.tts, enabled=True),
                config.neural_tts,
                replace(config.live2d, enabled=True),
            )

            async def stream_voxcpm(*_args, **_kwargs):
                return b"RIFF0000WAVEstream", True

            async def fail_complete_wav(*_args, **_kwargs):
                raise AssertionError("complete WAV path must not run after streaming succeeds")

            output._synthesize_streaming = stream_voxcpm
            output._synthesize = fail_complete_wav
            audio, streamed = asyncio.run(output._synthesize_preferred("流式语音"))

            self.assertEqual(audio, b"RIFF0000WAVEstream")
            self.assertTrue(streamed)

    def test_speech_is_split_at_commas_without_tiny_fragments(self):
        segments = ReplyOutput._split_speech_segments(
            "好的，我们先读第一段，嗯，再继续读后面的内容。",
            min_chars=4,
        )

        self.assertEqual(
            segments,
            ["好的，我们先读第一段，", "嗯，再继续读后面的内容。"],
        )

    def test_later_comma_clauses_are_grouped_to_reduce_model_calls(self):
        segments = ReplyOutput._split_speech_segments(
            "我们先说第一段，后面这句话正在准备，如果还没准备好，"
            "我会稍微想一下，然后再自然地接着告诉你。",
            min_chars=4,
            target_chars=16,
        )

        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0], "我们先说第一段，")
        self.assertTrue(segments[-1].endswith("告诉你。"))

    def test_model_dead_air_is_trimmed_without_resampling_speech(self):
        sample_rate = 1000
        silence = b"\x00\x00"
        voiced = int(1200).to_bytes(2, "little", signed=True)
        pcm = silence * 300 + voiced * 200 + silence * 400

        trimmed = ReplyOutput._trim_pcm_silence(pcm, sample_rate)

        self.assertLess(len(trimmed), len(pcm))
        self.assertGreaterEqual(len(trimmed), 400)
        self.assertIn(voiced * 20, trimmed)

    def test_segmented_playback_prefetches_next_phrase_without_filler(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(
                    config.tts,
                    enabled=True,
                    segmented_playback=True,
                    segment_wait_seconds=0.03,
                    segment_min_chars=2,
                ),
                config.neural_tts,
                replace(config.live2d, enabled=True),
            )
            sent: list[tuple[int, bool]] = []
            sample_rate = 1000

            async def request(text, **_kwargs):
                if text.startswith("第二"):
                    await asyncio.sleep(0.01)
                return b"\x01\x00" * 250, sample_rate

            async def send(pcm, *, sample_rate, reset):
                sent.append((len(pcm), reset))
                return True

            output._request_stream_pcm = request
            output._send_stream_block = send
            result = asyncio.run(output._synthesize_streaming("第一句话，第二句话。"))

            self.assertIsNotNone(result)
            self.assertEqual(output.last_stream_segment_count, 2)
            self.assertEqual(output.last_stream_filler_count, 0)
            self.assertEqual(sent, [(500, True), (500, False)])

    def test_segmented_playback_adds_one_pause_and_same_voice_filler_when_late(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(
                config.output,
                replace(
                    config.tts,
                    enabled=True,
                    segmented_playback=True,
                    segment_wait_seconds=0.03,
                    segment_filler_enabled=True,
                    segment_min_chars=2,
                ),
                config.neural_tts,
                replace(config.live2d, enabled=True),
            )
            sample_rate = 1000
            output._filler_audio = (b"\x02\x00" * 50, sample_rate)
            sent: list[tuple[bytes, bool]] = []

            async def request(text, **_kwargs):
                if text.startswith("第二"):
                    await asyncio.sleep(0.16)
                return b"\x01\x00" * 80, sample_rate

            async def send(pcm, *, sample_rate, reset):
                sent.append((pcm, reset))
                return True

            output._request_stream_pcm = request
            output._send_stream_block = send
            result = asyncio.run(output._synthesize_streaming("第一句话，第二句话。"))

            self.assertIsNotNone(result)
            self.assertEqual(output.last_stream_segment_count, 2)
            self.assertEqual(output.last_stream_filler_count, 1)
            self.assertTrue(sent[0][1])
            self.assertTrue(all(not reset for _, reset in sent[1:]))
            self.assertIn(output._filler_audio[0], [pcm for pcm, _ in sent])

    def test_reply_metadata_is_parsed(self):
        text, emotion, action = LocalHostAdapter._parse_reply_metadata(
            '{"reply":"欢迎来到直播间","emotion":"happy","action":"wave"}'
        )
        self.assertEqual(text, "欢迎来到直播间")
        self.assertEqual(emotion, "happy")
        self.assertEqual(action, "wave")

    def test_delivery_advances_browser_speech_version(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalHostAdapter(make_config(Path(directory) / "subtitle.txt"))
            self.assertEqual(adapter.status()["speech_version"], 0)
            asyncio.run(adapter.announce("测试口播"))
            self.assertEqual(adapter.status()["speech_version"], 1)
            self.assertEqual(adapter.status()["latest_reply"], "测试口播")

    def test_demo_barrage_is_recorded_and_falls_back_without_core(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            adapter = LocalHostAdapter(make_config(subtitle))
            result = asyncio.run(adapter.ingest_demo_barrage("小星星", "主播你好"))
            self.assertEqual(result["mode"], "local_fallback")
            self.assertEqual(adapter.status()["latest_barrage"], "主播你好")
            self.assertEqual(adapter.status()["latest_barrage_user"], "小星星")
            self.assertEqual(subtitle.read_text(encoding="utf-8"), "小星星：主播你好")

    def test_demo_event_feedback_covers_gift_safety_and_host_takeover(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalHostAdapter(make_config(Path(directory) / "subtitle.txt"))
            result = asyncio.run(
                adapter.ingest_demo_event("gift", "小星星", "小花", amount=2)
            )
            self.assertEqual(result["event_type"], "gift")
            self.assertEqual(adapter.status()["demo_event_type"], "gift")
            self.assertEqual(adapter.status()["dance_style"], "cute")
            asyncio.run(adapter.ingest_demo_event("safety", detail="违规内容"))
            self.assertEqual(adapter.status()["demo_event_type"], "safety")
            asyncio.run(adapter.ingest_demo_event("pause"))
            self.assertTrue(adapter.status()["demo_ai_paused"])
            asyncio.run(adapter.ingest_demo_event("resume"))
            self.assertFalse(adapter.status()["demo_ai_paused"])

    def test_dance_and_voice_controls_change_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalHostAdapter(make_config(Path(directory) / "subtitle.txt"))
            self.assertEqual(asyncio.run(adapter.set_dance_style("cute")), "软萌甜舞")
            self.assertEqual(asyncio.run(adapter.set_dance_style("kpop")), "K-pop 编舞")
            self.assertEqual(asyncio.run(adapter.set_dance_style("random")), "自动串舞")
            self.assertEqual(adapter.status()["dance_style"], "random")
            self.assertIn("shuffle", adapter.status()["dance_styles"])
            self.assertEqual(asyncio.run(adapter.set_voice_profile("mature")), "御姐女声")
            self.assertEqual(adapter.status()["voice_profile"], "mature")
