from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from loguru import logger

from config import AppConfig
from outputs import ReplyOutput


DANCE_STYLES = {
    "idle": "停止舞蹈",
    "random": "自动串舞",
    "cute": "软萌甜舞",
    "energetic": "元气爵士",
    "kpop": "K-pop 编舞",
    "hiphop": "嘻哈 Groove",
    "shuffle": "曳步舞",
    "elegant": "优雅爵士",
    "gesture": "动作组合秀",
}

DEMO_EVENT_LABELS = {
    "comment": "评论触发",
    "like": "点赞聚合",
    "gift": "礼物致谢",
    "safety": "安全拦截",
    "pause": "主播接管",
    "resume": "AI 已恢复",
}

ROOT_DIR = Path(__file__).resolve().parents[1]
NACHOBOT_DIR = ROOT_DIR / "NachoBot"
if str(NACHOBOT_DIR) not in sys.path:
    sys.path.insert(0, str(NACHOBOT_DIR))
ACTION_ADAPTER_DIR = ROOT_DIR / "NachoBot-Live2D-Adapter" / "live2d_adapter"
if str(ACTION_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ACTION_ADAPTER_DIR))

from action_adapter import ActionAdapter  # noqa: E402

from ncnk_message import (  # noqa: E402
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    RouteConfig,
    Router,
    Seg,
    TargetConfig,
    UserInfo,
)


class LocalHostAdapter:
    """Routes manual local prompts into NachoBot and sends replies to local outputs only."""

    def __init__(self, config: AppConfig):
        self.config = config
        target = TargetConfig(url=f"ws://{config.nachobot.host}:{config.nachobot.port}/ws", token=None)
        self.router = Router(RouteConfig(route_config={config.nachobot.platform: target}))
        self.router.register_class_handler(self.handle_from_nachobot)
        self.output = ReplyOutput(config.output, config.tts, config.neural_tts, config.live2d)
        self.action_adapter = ActionAdapter(logger)
        self._router_task: asyncio.Task | None = None
        self._request_count = 0
        self._reply_count = 0
        self._latest_reply = ""
        self._latest_barrage = ""
        self._latest_barrage_user = ""
        self._barrage_version = 0
        self._barrage_source = ""
        self._demo_event_type = ""
        self._demo_event_detail = ""
        self._demo_event_version = 0
        self._demo_ai_paused = False
        self._last_error = ""
        self._core_reachable = False
        self._last_core_probe = 0.0
        self._speech_version = 0
        self._tts_language = "auto"
        self._pending_tts_languages: deque[str] = deque()
        self._pending_speech_enabled: deque[bool] = deque()
        self._pending_prompts: deque[str] = deque()
        self._latest_emotion = "normal"
        self._latest_action = ""
        self._dance_style = "idle"
        self._auto_announcement_index = 0
        self._auto_announcement_task: asyncio.Task | None = None
        self._tts_filler_warmup_task: asyncio.Task | None = None
        self._desktop_pet_mode = os.getenv(
            "NACHOBOT_LOCAL_HOST_DESKTOP_MODE", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}

    async def start(self) -> None:
        self._router_task = asyncio.create_task(self._router_loop(), name="local-host-core-router")
        self._tts_filler_warmup_task = asyncio.create_task(
            self._warm_tts_filler_after_startup(),
            name="local-host-tts-filler-warmup",
        )
        if (
            not self._desktop_pet_mode
            and self.config.auto_announcements.enabled
            and self.config.auto_announcements.messages
        ):
            self._auto_announcement_task = asyncio.create_task(
                self._auto_announcement_loop(), name="local-host-auto-announcements"
            )

    async def stop(self) -> None:
        if self._router_task:
            self._router_task.cancel()
            await asyncio.gather(self._router_task, return_exceptions=True)
        if self._auto_announcement_task:
            self._auto_announcement_task.cancel()
            await asyncio.gather(self._auto_announcement_task, return_exceptions=True)
        if self._tts_filler_warmup_task:
            self._tts_filler_warmup_task.cancel()
            await asyncio.gather(self._tts_filler_warmup_task, return_exceptions=True)
        await self.router.stop()

    async def _warm_tts_filler_after_startup(self) -> None:
        # Let the HTTP server become responsive before the one-time local cache
        # generation occupies VoxCPM.  Subsequent starts load the cached WAV.
        await asyncio.sleep(3)
        await self.output.warm_segment_filler()

    async def _auto_announcement_loop(self) -> None:
        config = self.config.auto_announcements
        await asyncio.sleep(config.first_delay_seconds)
        while True:
            message = config.messages[self._auto_announcement_index % len(config.messages)]
            self._auto_announcement_index += 1
            try:
                await self._deliver_reply(message)
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Automatic live announcement failed: {}", exc)
            await asyncio.sleep(config.interval_seconds)

    async def _router_loop(self) -> None:
        while True:
            try:
                await self.router.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("NachoBot core connection failed: {}; retrying in 3s", exc)
                await asyncio.sleep(3)

    async def request_ai_reply(
        self,
        prompt: str,
        tts_language: str = "auto",
        *,
        speak: bool = True,
    ) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("请输入要让 AI 主播回应的话题或台词")
        if not await self._core_available():
            raise RuntimeError("尚未连接 NachoBot Core；请先启动 launchbot.bat")
        self._tts_language = tts_language
        message = self._build_message(prompt)
        self._pending_prompts.append(prompt)
        self._pending_tts_languages.append(tts_language)
        self._pending_speech_enabled.append(bool(speak))
        try:
            await self.router.send_message(message)
        except Exception:
            if self._pending_prompts and self._pending_prompts[-1] == prompt:
                self._pending_prompts.pop()
            if self._pending_tts_languages and self._pending_tts_languages[-1] == tts_language:
                self._pending_tts_languages.pop()
            if self._pending_speech_enabled and self._pending_speech_enabled[-1] == bool(speak):
                self._pending_speech_enabled.pop()
            raise
        self._request_count += 1
        return message.message_info.message_id

    async def _core_available(self) -> bool:
        """Check both the router map and the actual Core TCP listener.

        The router can retain a client entry briefly after a process exits; a
        short TCP probe prevents a false-positive AI handoff in the demo path.
        """
        now = time.monotonic()
        if now - self._last_core_probe < 1.0:
            return self._core_reachable
        self._last_core_probe = now
        if self.config.nachobot.platform not in self.router.clients:
            self._core_reachable = False
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.nachobot.host, self.config.nachobot.port),
                timeout=0.6,
            )
        except (OSError, asyncio.TimeoutError):
            self._core_reachable = False
            return False
        writer.close()
        await writer.wait_closed()
        self._core_reachable = True
        return True

    async def refresh_core_reachability(self) -> bool:
        """Refresh the cached connectivity flag used by the control-panel status."""
        return await self._core_available()

    async def announce(
        self,
        text: str,
        tts_language: str = "auto",
        *,
        speak: bool = True,
    ) -> str:
        text = text.strip()
        if not text:
            raise ValueError("请输入要直接播报的文字")
        self._tts_language = tts_language
        return await self._deliver_reply(
            text,
            question=text,
            tts_language=tts_language,
            synthesize_audio=speak,
        )

    async def ingest_demo_barrage(self, nickname: str, content: str) -> dict[str, str]:
        """Inject one clearly-labelled local replay of a Douyin comment event.

        Production comments still arrive through the official Douyin adapter. This
        endpoint is deliberately local-only and exists so a creator can verify the
        display/reply path before platform credentials and HTTPS callbacks are ready.
        """
        nickname = nickname.strip() or "演示观众"
        content = content.strip()
        if not content:
            raise ValueError("弹幕内容不能为空")
        self._latest_barrage = content
        self._latest_barrage_user = nickname
        self._barrage_version += 1
        self._barrage_source = "local_demo"
        self._set_demo_event("comment", f"{nickname}：{content}")
        prompt = f"直播弹幕（{nickname}）：{content}\n请像温柔自然的主播一样简短回应这条弹幕。"
        try:
            request_id = await self.request_ai_reply(prompt)
            return {"mode": "ai", "request_id": request_id}
        except RuntimeError:
            # Keep the demo useful when NachoBot Core is temporarily offline.
            await self._deliver_reply(f"{nickname}：{content}", question=content)
            return {"mode": "local_fallback", "request_id": ""}

    async def ingest_demo_event(
        self,
        event_type: str,
        nickname: str = "演示观众",
        detail: str = "",
        amount: int = 1,
        speak: bool = True,
    ) -> dict[str, str | int]:
        """Replay a local interaction for a review video without touching Douyin."""
        event_type = event_type.strip().lower()
        if event_type not in DEMO_EVENT_LABELS:
            raise ValueError("不支持的演示互动类型")
        nickname = nickname.strip() or "演示观众"
        detail = detail.strip()
        if event_type == "comment":
            if not detail:
                raise ValueError("评论互动需要提供内容")
            if speak:
                result = await self.ingest_demo_barrage(nickname, detail)
            else:
                result = {"mode": "local_demo", "request_id": ""}
                self._latest_barrage = detail
                self._latest_barrage_user = nickname
                self._barrage_version += 1
                self._barrage_source = "local_demo"
                self._set_demo_event("comment", f"{nickname}：{detail}")
                self._record_demo_reply("收到你的问题啦，马上跳一段软萌舞蹈！")
            return {**result, "event_type": event_type}
        if event_type == "like":
            count = max(1, amount)
            self._set_demo_event("like", f"累计收到 {count} 次点赞")
            if speak:
                await self._deliver_reply(
                    f"谢谢大家的点赞，爱心已累计 {count} 次！",
                    question="收到点赞",
                )
            else:
                self._record_demo_reply(f"谢谢大家的点赞，爱心已累计 {count} 次！")
        elif event_type == "gift":
            count = max(1, amount)
            gift_name = detail or "小星星"
            self._set_demo_event("gift", f"{nickname} 送出 {gift_name} × {count}")
            if speak:
                await self._deliver_reply(
                    f"谢谢 {nickname} 的 {gift_name}，送你一段软萌舞蹈！",
                    question=f"收到礼物 {gift_name}",
                )
            else:
                self._record_demo_reply(f"谢谢 {nickname} 的 {gift_name}，送你一段软萌舞蹈！")
            self._dance_style = "cute"
        elif event_type == "safety":
            self._set_demo_event("safety", "内容未通过安全检查，已静默拦截")
            if speak:
                await self._deliver_reply(
                    "这条内容暂时无法回应，我们换个轻松的话题吧。",
                    question="安全拦截",
                    emotion="disgust",
                    action="摇头/否定",
                )
            else:
                self._record_demo_reply("这条内容暂时无法回应，我们换个轻松的话题吧。")
        elif event_type == "pause":
            self._demo_ai_paused = True
            self._set_demo_event("pause", "主播已暂停 AI，当前由人工接管")
            if speak:
                await self._deliver_reply("AI 已暂停，主播接管中。", question="主播接管")
            else:
                self._record_demo_reply("AI 已暂停，主播接管中。")
        elif event_type == "resume":
            self._demo_ai_paused = False
            self._set_demo_event("resume", "AI 互动已恢复")
            if speak:
                await self._deliver_reply("AI 互动已恢复，欢迎继续聊天。", question="恢复互动")
            else:
                self._record_demo_reply("AI 互动已恢复，欢迎继续聊天。")
        return {"mode": "local_demo", "request_id": "", "event_type": event_type}

    def _set_demo_event(self, event_type: str, detail: str) -> None:
        self._demo_event_type = event_type
        self._demo_event_detail = detail
        self._demo_event_version += 1

    def _record_demo_reply(self, text: str) -> None:
        """Update visible demo subtitle without generating audio for screenshot capture."""
        self._latest_reply = text
        self._reply_count += 1
        self._speech_version += 1

    async def set_dance_style(self, style: str) -> str:
        if style not in DANCE_STYLES:
            raise ValueError("不支持的舞蹈类型")
        self._dance_style = style
        return DANCE_STYLES[style]

    async def set_voice_profile(self, profile: str) -> str:
        profile = self.output.set_voice_profile(profile)
        if self._tts_filler_warmup_task:
            self._tts_filler_warmup_task.cancel()
        self._tts_filler_warmup_task = asyncio.create_task(
            self.output.warm_segment_filler(),
            name=f"local-host-tts-filler-{profile}",
        )
        return {"cute": "软萌女声", "mature": "御姐女声"}[profile]

    async def _deliver_reply(
        self,
        text: str,
        *,
        question: str = "",
        emotion: str | None = None,
        action: str | None = None,
        tts_language: str = "auto",
        synthesize_audio: bool = True,
    ) -> str:
        decision = self.action_adapter.decide(
            question=question,
            reply=text,
            emotion=emotion,
            requested_action=action,
        )
        emotion = decision.emotion
        action = decision.action_id
        self._latest_emotion = emotion or "normal"
        self._latest_action = action or ""
        delivered = await self.output.deliver(
            text,
            emotion=emotion,
            action=action,
            tts_language=tts_language,
            synthesize_audio=synthesize_audio,
        )
        self._latest_reply = delivered
        self._reply_count += 1
        self._speech_version += 1
        return delivered

    def _build_message(self, prompt: str) -> MessageBase:
        additional = {
            "is_mentioned": 1.0,
            "disable_tools": not self.config.nachobot.network_search_enabled,
            "runtime_capabilities": {
                "schema_version": 1,
                "planner_bypass": True,
                "history_summarization": False,
                "notice_actions": False,
                "relation_inference": False,
                "expression_selection": False,
                "memory_retrieval": False,
                # Desktop chat already carries recent conversation context.
                # Skipping the extra memory query removes several seconds of
                # latency without changing the stored NachoBot memories.
                "mid_term_memory": False,
                "knowledge_retrieval": False,
                "reply_model_group": "realtime_replyer",
                "tool_mode": "disabled",
                "web_search_mode": (
                    "two_phase" if self.config.nachobot.network_search_enabled else "disabled"
                ),
                "reply_delivery": "json_envelope",
                "person_profile_mode": (
                    "low_latency" if self.config.nachobot.person_profile_enabled else "disabled"
                ),
                "typo_enabled": False,
            },
            "platform_event": {"type": "manual_host_prompt", "source": "local_control_panel"},
        }
        message_text = prompt
        if self.config.nachobot.reply_prompt:
            message_text = (
                f"{prompt}\n\n"
                f"回答风格要求：{self.config.nachobot.reply_prompt}"
            )
        info = BaseMessageInfo(
            platform=self.config.nachobot.platform,
            message_id=f"local-host-{time.time_ns()}",
            time=time.time(),
            user_info=UserInfo(
                platform=self.config.nachobot.platform,
                user_id=self.config.nachobot.host_user_id,
                user_nickname=self.config.nachobot.host_nickname,
            ),
            group_info=GroupInfo(
                platform=self.config.nachobot.platform,
                group_id=self.config.nachobot.studio_id,
                group_name="本机 AI 主播工作室",
            ),
            format_info=FormatInfo(content_format=["text"], accept_format=["text", "reply"]),
            template_info=None,
            additional_config=additional,
        )
        return MessageBase(
            message_info=info,
            message_segment=Seg(type="text", data=message_text),
        )

    async def handle_from_nachobot(self, raw_message: dict[str, Any]) -> None:
        try:
            message = MessageBase.from_dict(raw_message)
            if message.message_info.platform != self.config.nachobot.platform:
                return
            text = self._plain_text(message.message_segment).strip()
            if not text:
                return
            text, emotion, action = self._parse_reply_metadata(text)
            question = self._pending_prompts.popleft() if self._pending_prompts else ""
            tts_language = (
                self._pending_tts_languages.popleft()
                if self._pending_tts_languages
                else self._tts_language
            )
            synthesize_audio = (
                self._pending_speech_enabled.popleft()
                if self._pending_speech_enabled
                else True
            )
            await self._deliver_reply(
                text,
                question=question,
                emotion=emotion,
                action=action,
                tts_language=tts_language,
                synthesize_audio=synthesize_audio,
            )
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Failed to deliver local AI host reply: {}", exc)

    @staticmethod
    def _parse_reply_metadata(text: str) -> tuple[str, str | None, str | None]:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return text, None, None
        try:
            data = json.loads(text[start : end + 1], strict=False)
        except (json.JSONDecodeError, TypeError):
            return text, None, None
        if not isinstance(data, dict) or not data.get("reply"):
            return text, None, None
        emotion = str(data["emotion"]) if data.get("emotion") is not None else None
        action = str(data["action"]) if data.get("action") is not None else None
        return str(data["reply"]), emotion, action

    @classmethod
    def _plain_text(cls, segment: Seg) -> str:
        if segment.type == "seglist" and isinstance(segment.data, list):
            return "".join(cls._plain_text(child) for child in segment.data)
        if segment.type in {"text", "reply", "tts_text"}:
            if isinstance(segment.data, dict):
                return str(segment.data.get("text") or segment.data.get("content") or "")
            return str(segment.data or "")
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "local_only",
            "core_connected": self._core_reachable,
            "requests": self._request_count,
            "replies": self._reply_count,
            "latest_reply": self._latest_reply,
            "latest_emotion": self._latest_emotion,
            "latest_action": self._latest_action,
            "latest_barrage": self._latest_barrage,
            "latest_barrage_user": self._latest_barrage_user,
            "barrage_version": self._barrage_version,
            "barrage_source": self._barrage_source,
            "demo_event_type": self._demo_event_type,
            "demo_event_label": DEMO_EVENT_LABELS.get(self._demo_event_type, ""),
            "demo_event_detail": self._demo_event_detail,
            "demo_event_version": self._demo_event_version,
            "demo_ai_paused": self._demo_ai_paused,
            "last_error": self._last_error,
            "subtitle_file": str(self.config.output.subtitle_file),
            "tts_enabled": self.config.tts.enabled,
            "local_tts_enabled": self.config.tts.enabled,
            # Kept for the browser page's backward-compatible audio-player flag.
            "neural_tts_enabled": self.config.neural_tts.enabled or self.config.tts.enabled,
            "neural_tts_voice": self.output.neural_voice,
            "tts_language": self._tts_language,
            "neural_audio_ready": self.output.audio_ready,
            "neural_audio_version": self.output.audio_version,
            "live2d_streamed_audio_version": self.output.streamed_audio_version,
            "tts_stream_first_block_seconds": self.output.last_stream_first_block_seconds,
            "tts_stream_total_seconds": self.output.last_stream_total_seconds,
            "tts_stream_segment_count": self.output.last_stream_segment_count,
            "tts_stream_filler_count": self.output.last_stream_filler_count,
            "tts_segmented_playback": self.config.tts.segmented_playback,
            "tts_segment_wait_seconds": self.config.tts.segment_wait_seconds,
            "tts_segment_pause_step_seconds": self.config.tts.segment_pause_step_seconds,
            "tts_segment_target_chars": self.config.tts.segment_target_chars,
            "tts_segment_filler_ready": self.output.segment_filler_ready,
            "speech_audio_source": self.output.audio_source,
            "speech_audio_media_type": self.output.audio_media_type,
            "voice_profile": self.output.voice_profile,
            "browser_tts_enabled": self.config.browser_tts.enabled,
            "browser_tts": {
                "language": self.config.browser_tts.language,
                "preferred_voice": self.output.browser_preferred_voice,
                "rate": self.config.browser_tts.rate,
                "pitch": self.config.browser_tts.pitch,
                "volume": self.config.browser_tts.volume,
            },
            "speech_version": self._speech_version,
            "avatar_mode": (
                "vrm"
                if self.config.vrm.enabled and self.config.vrm.model_file.is_file()
                else "browser_animated"
            ),
            "vrm_enabled": self.config.vrm.enabled,
            "vrm_model_ready": self.config.vrm.model_file.is_file(),
            "vrm_model_file": str(self.config.vrm.model_file),
            "dance_style": self._dance_style,
            "dance_label": DANCE_STYLES[self._dance_style],
            "dance_styles": DANCE_STYLES,
            "auto_announcements_enabled": (
                self.config.auto_announcements.enabled and not self._desktop_pet_mode
            ),
            "auto_announcement_interval_seconds": self.config.auto_announcements.interval_seconds,
            "live2d_enabled": self.config.live2d.enabled,
        }
