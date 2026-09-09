from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(message.message_segment.data, "介绍一下今天的主题")
        self.assertEqual(message.message_info.template_info.template_name, "local_host_reply")

    def test_direct_announcement_writes_obs_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subtitle.txt"
            config = make_config(subtitle)
            output = ReplyOutput(config.output, config.tts, config.neural_tts, config.live2d)
            result = asyncio.run(output.deliver("<ZH>晚上好</ZH>"))
            self.assertEqual(result, "晚上好")
            self.assertEqual(subtitle.read_text(encoding="utf-8"), "晚上好")

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
