import asyncio
import json
from loguru import logger
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Add NachoBot path for ncnk_message module
_root_dir = Path(__file__).resolve().parents[1]
_nachobot_path = _root_dir / "NachoBot"
if _nachobot_path.exists() and str(_nachobot_path) not in sys.path:
    sys.path.insert(0, str(_nachobot_path))


from ncnk_message import (  # noqa: E402
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    Router,
    RouteConfig,
    Seg,
    TargetConfig,
    TemplateInfo,
    UserInfo,
)

from bili_src.core.config import (  # noqa: E402
    AdapterConfig,
    AsrModelConfig,
    PrivateSessionConfig,
    _resolve_asr_model_config,
    _resolve_vlm_model_config_list,
)
from bili_src.core.utils import (  # noqa: E402
    _guard_command_segment,
    _mask_urls,
    _strip_emoji,
)
from bili_src.api.api import BilibiliApi  # noqa: E402
from bili_src.live.live_worker import LiveRoomWorker  # noqa: E402
from bili_src.live.screen_monitor import ScreenMonitor  # noqa: E402
from bili_src.audio.mic_capture import MicCaptureWorker, MicConfig  # noqa: E402
from bili_src.audio.audio_player import AudioPlayer  # noqa: E402
# from live_streamer import LiveStreamerController, PriorityEvent  # noqa: E402

# Try to import TTS model
# 修复：动态获取相对路径，替换硬编码的绝对路径
tts_adapter_path = _root_dir / "NachoBot-Multimodal-Adapter"
TTSModel = None
_tts_import_error = None

if tts_adapter_path.exists():
    if str(tts_adapter_path) not in sys.path:
        sys.path.insert(0, str(tts_adapter_path))
    try:
        from nachobot_multimodal.utils.tts_resolver import resolve_tts_model_class
        TTSModel, _tts_import_error = resolve_tts_model_class()
    except ImportError as e:
        _tts_import_error = str(e)
    except Exception as e:
        _tts_import_error = f"Unexpected error: {e}"
else:
    _tts_import_error = f"TTS adapter path does not exist: {tts_adapter_path}"

ACCEPT_FORMAT = ["text", "reply", "command"]
ACCEPT_FORMAT_PRIVATE = ["text", "image", "emoji", "reply", "command"]

BILIBILI_DANMU_SEND_DELAY_SECONDS = 0.8


class BilibiliAdapter:
    def __init__(
        self,
        config: AdapterConfig,
        logger,
        config_path: Optional[Path] = None,
    ):
        self.config = config
        self.logger = logger
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                ),
                # Live messages use the standard HeartFlow pipeline.
                "bilibili.live": TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                ),
            }
        )
        self.router = Router(route_config, custom_logger=logger)
        self.router.register_class_handler(self.handle_from_nachobot)
        self.api = BilibiliApi(config, logger)
        self._screen_host_room_id = config.live_host_room_id
        self._screen_monitor: Optional[ScreenMonitor] = None
        self._screen_manual_enable = config.screen_manual_enable
        self._screen_manual_duration_seconds = config.screen_manual_duration_seconds
        self._screen_manual_user_ids = {
            str(user_id) for user_id in config.screen_manual_user_ids if str(user_id)
        }
        self._screen_manual_state: Optional[bool] = None
        self._screen_manual_until: float = 0.0
        self._danmu_cache: Dict[int, Dict[str, str]] = {}

        self._dm_last_seqno: Dict[Tuple[int, int], int] = {}
        self._last_private_session: Optional[PrivateSessionConfig] = None
        self._private_session_by_group: Dict[str, PrivateSessionConfig] = {}
        self._auto_private_sessions: List[PrivateSessionConfig] = []
        self._auto_private_sessions_ts: float = 0.0
        self._user_name_cache: Dict[str, Tuple[str, float]] = {}
        self._user_name_cache_seconds = 3600

        # Store config path for persistence
        self.config_path = config_path
        self._self_danmu_ids: Dict[int, Dict[str, float]] = {}

        # Event Serialization & Aggregation
        from bili_src.live.event_manager import EventManager

        self.event_manager = EventManager(self.config, self.logger, self)

        # Outgoing Message Dispatch
        from bili_src.live.outgoing_handler import OutgoingHandler

        self.outgoing_handler = OutgoingHandler(self.config, self.logger, self)

        # Comment Handler
        from bili_src.chat.comment_handler import CommentHandler

        self.comment_handler = CommentHandler(self.config, self.logger, self)

        # Private Message Handler
        from bili_src.chat.private_handler import PrivateHandler

        self.private_handler = PrivateHandler(self.config, self.logger, self)

        # Initialize Mic Capture Worker
        self.mic_worker: Optional[MicCaptureWorker] = None
        self._mic_manual_state: Optional[bool] = None
        self.logger.info(
            f"Mic Config Check: enable={config.mic_asr_enable}, room_id={config.mic_asr_room_id}"
        )
        if config.mic_asr_enable and config.mic_asr_room_id:
            mic_config = MicConfig(
                enable=config.mic_asr_enable,
                room_id=config.mic_asr_room_id,
                subtitle_path=config.mic_asr_subtitle_path,
                silence_threshold=config.mic_asr_silence_threshold,
                silence_duration=config.mic_asr_silence_duration,
                sample_rate=config.mic_asr_sample_rate,
                push_to_talk=config.mic_asr_push_to_talk,
                ptt_key=config.mic_asr_ptt_key,
            )
            # Define callback alias for thread-safe calling if needed,
            # though MicCaptureWorker handles thread-safety via asyncio.run_coroutine_threadsafe now
            mic_config.on_speech_start = self._on_speech_start

            self.mic_worker = MicCaptureWorker(
                mic_config, self._handle_mic_recognition, logger
            )
            self.mic_worker.set_asr_callback(self._call_asr_api)
        self._self_danmu_texts: Dict[int, List[Tuple[str, float]]] = {}
        self._live_status_cache: Dict[int, Tuple[int, float]] = {}
        self._live_status_cache_seconds = 20
        if self._screen_host_room_id is not None:
            monitor_configs = self._load_vlm_model_configs()
            if monitor_configs:
                self._screen_monitor = ScreenMonitor(
                    monitor_configs,
                    logger,
                    min_interval_seconds=config.screen_capture_interval_seconds,
                    capture_active_window=config.screen_capture_active_window,
                    excluded_exes=config.screen_capture_excluded_exes,
                )
            else:
                self.logger.warning("Screen monitor disabled: VLM config unavailable")
        else:
            self.logger.info("Screen monitor disabled: host room not configured")

        # Initialize AudioPlayer
        self.audio_player = AudioPlayer(logger)

        # self._live_streamer_controllers: Dict[int, LiveStreamerController] = {}
        # for room_id, streamer_config in config.live_streamer_configs.items():
        #     if streamer_config.enable:
        #         self._live_streamer_controllers[room_id] = LiveStreamerController(
        #             config=streamer_config,
        #             room_id=room_id,
        #             adapter=self,
        #             logger=logger,
        #         )
        #         self.logger.info(f"Live Streamer mode enabled for room {room_id}")

        from bili_src.live2d.live2d_manager import Live2DManager

        self.live2d_manager = Live2DManager(self.config, self.logger, self)

        if self.live2d_manager.controller:

            def _on_audio_start():
                self.live2d_manager.controller.set_speaking(True)

            def _on_audio_stop():
                self.live2d_manager.controller.set_speaking(False)
                self.live2d_manager.controller.notify_reply_finished()

            self.audio_player.on_start = _on_audio_start
            self.audio_player.on_stop = _on_audio_stop

        from bili_src.audio.tts_manager import TTSManager

        self.tts_manager = TTSManager(
            config=self.config,
            logger=self.logger,
            config_path=self.config_path,
            audio_player=self.audio_player,
            send_danmu_callback=self.outgoing_handler._send_danmu,
            live2d_start_reply_callback=self.live2d_manager.controller.on_start_replying
            if self.live2d_manager.controller
            else None,
            live2d_finish_reply_callback=self.live2d_manager.controller.on_reply_finished
            if self.live2d_manager.controller
            else None,
            live2d_execute_action_callback=self.live2d_manager.execute_extracted_live2d_action,
            extract_json_emotion_callback=self.live2d_manager.extract_json_emotion_from_text,
            tts_model_class=TTSModel,
            tts_import_error=_tts_import_error,
        )

    async def _resolve_user_nickname(self, user_id: str) -> str:
        """Resolve a user's nickname from cache or Bilibili API."""
        now = time.time()
        if user_id in self._user_name_cache:
            name, t = self._user_name_cache[user_id]
            # Use 3600 for successful names, and 300s for fallback user_ids
            cache_duration = self._user_name_cache_seconds if name != user_id else 150
            if now - t < cache_duration:
                return name

        try:
            uid_int = int(user_id)
            info = await self.api.get_user_info(uid_int)
            data = info.get("data", {})
            name = data.get("name")
            if name:
                self._user_name_cache[user_id] = (name, now)
                return name
        except Exception as e:
            self.logger.debug(f"Failed to resolve user_id {user_id}: {e}")

        # Cache the failure to prevent rate limit spam
        self._user_name_cache[user_id] = (user_id, now)
        return user_id

    # ========== Run and Control Methods ==========

    async def run(self) -> None:
        await self.api.start()
        if self.live2d_manager:
            await self.live2d_manager.start()
        tasks = [self.router.run()]
        if self.config.live_enable:
            for room_id in self.config.room_ids:
                worker = LiveRoomWorker(
                    room_id, self.config, self.api, self, self.logger
                )
                tasks.append(worker.run())
        else:
            self.logger.info("Live adapter disabled by config")
        if self.config.enable_reply_notice:
            tasks.append(self.comment_handler.comment_notice_loop())
        if self.config.private_enable and (
            self.config.private_sessions or self.config.private_auto_sessions
        ):
            tasks.append(self.private_handler.private_message_loop())

        if self.mic_worker:
            tasks.append(self._run_mic_worker_forever())
            tasks.append(self._mic_control_loop())

        # Event Queue and Gift Aggregation
        tasks.append(self.event_manager.event_consumer_loop())
        tasks.append(self.event_manager.gift_flush_loop())
        tasks.append(self.tts_manager.idle_tts_loop())

        # for controller in self._live_streamer_controllers.values():
        #     tasks.append(controller.start())

        self.audio_player.start()  # Start audio player loop

        try:
            await asyncio.gather(*tasks)
        finally:
            if self.live2d_manager:
                await self.live2d_manager.stop()
            await self.api.close()

    async def _run_mic_worker_forever(self) -> None:
        """Keep mic worker running, restarting on failure"""
        if not self.mic_worker:
            return

        while True:
            try:
                self.logger.info("Starting MicCaptureWorker...")
                await self.mic_worker.start()
                self.logger.warning("MicCaptureWorker exited. Restarting in 3s...")
            except asyncio.CancelledError:
                self.logger.info("MicCaptureWorker task cancelled")
                break
            except Exception as e:
                self.logger.error(f"MicCaptureWorker crashed: {e}. Restarting in 5s...")
                await asyncio.sleep(5)

            await asyncio.sleep(3)

    async def _mic_control_loop(self) -> None:
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return

        room_id = self.mic_worker.config.room_id

        while True:
            try:
                should_pause = True

                # if room_id in self._live_streamer_controllers:
                #     should_pause = True
                #     self.logger.debug(
                #         "[LiveStreamer] ASR paused - Live Streamer mode active"
                #     )
                if self._mic_manual_state is not None:
                    should_pause = not self._mic_manual_state
                else:
                    status = await self._get_live_status(room_id)
                    if status == 1:
                        should_pause = False
                    else:
                        should_pause = True

                if should_pause:
                    if not self.mic_worker.is_paused():
                        self.mic_worker.pause()
                else:
                    if self.mic_worker.is_paused():
                        self.mic_worker.resume()

            except Exception as e:
                self.logger.error(f"Error in mic control loop: {e}")

            await asyncio.sleep(5)

    def _load_vlm_model_configs(self) -> list:
        root_dir = Path(__file__).resolve().parents[1]
        model_config_path = root_dir / "NachoBot" / "config" / "model_config.toml"
        return _resolve_vlm_model_config_list(model_config_path, self.logger)

    def _load_asr_model_config(self) -> Optional[AsrModelConfig]:
        root_dir = Path(__file__).resolve().parents[1]
        model_config_path = root_dir / "NachoBot" / "config" / "model_config.toml"
        return _resolve_asr_model_config(model_config_path, self.logger)

    async def _on_speech_start(self):
        """Callback when user starts speaking."""
        # Stop audio player immediately
        self.audio_player.stop_and_pause()

    # ========== Danmu Cache and Filter Methods ==========

    def remember_danmu(self, room_id: int, message_id: str, user_id: str) -> None:
        cache = self._danmu_cache.setdefault(room_id, {})
        cache[message_id] = user_id
        if len(cache) > 2000:
            for key in list(cache.keys())[:500]:
                cache.pop(key, None)

    def _filter_outgoing_text(self, text: str) -> str:
        if not text:
            return text
        if not self.config.response_filter_enable:
            return text
        markers = self.config.response_filter_blocked_markers
        if not markers:
            return text
        normalized = text.lower()
        for marker in markers:
            if marker and marker in normalized:
                self.logger.error(
                    "[BilibiliAdapter] Detected blocked marker in outgoing text, replaced with Filtered"
                )
                return "Filtered"
        return text

    @staticmethod
    def _sanitize_user_text(text: str) -> str:
        """
        Sanitize user text to prevent spoofing system events.
        If a user message looks like a system gift notification, prefix it to clarify source.
        """
        if not text:
            return ""

        # Patterns that look like system events
        # "送出了 xxx xN (价值...)"
        # "开通了 xxx xN"
        suspicious_prefixes = ("送出了", "开通了")

        # Check if it starts with suspicious prefix AND contains "x" followed by a number
        if text.startswith(suspicious_prefixes) and re.search(r" x\d+", text):
            return f"[用户发言] {text}"

        # Check for SuperChat spoofing
        if "（注意：这是一条超级弹幕信息，价值" in text:
            return f"[用户发言] {text}"

        return text

    # ========== Live Status and Screen Methods ==========

    async def _get_live_status(self, room_id: int) -> Optional[int]:
        now = time.time()
        cached = self._live_status_cache.get(room_id)
        if cached and (now - cached[1]) < self._live_status_cache_seconds:
            return cached[0]
        status = await self.api.get_live_status(room_id)
        if status is None:
            self.logger.warning("Live status check failed: room_id=%s", room_id)
            return None
        self._live_status_cache[room_id] = (status, now)
        return status

    async def _get_screen_summary(
        self, room_id: int, user_id: str, message_text: str
    ) -> Optional[str]:
        if not self._screen_monitor:
            return None
        if not self._screen_host_room_id or room_id != self._screen_host_room_id:
            return None
        manual_state = self._get_screen_manual_state()
        if manual_state is False:
            return None

        if manual_state is True:
            pass  # Force enabled, skip live check
        else:
            # None (auto mode)
            live_status = await self._get_live_status(room_id)
            if live_status != 1:
                return None
        if (
            self.config.dede_user_id
            and user_id
            and user_id == str(self.config.dede_user_id)
        ):
            return None
        return await self._screen_monitor.maybe_analyze(message_text)

    async def refresh_screen_summary(self, room_id: int) -> Optional[str]:
        """Refresh the local screen-summary cache used by live prompt templates."""
        return await self._get_screen_summary(
            room_id,
            user_id="",
            message_text="Periodic screen refresh",
        )

    def _get_cached_screen_summary(self, room_id: int) -> Optional[str]:
        """Return cached screen summary without triggering VLM. Non-blocking."""
        if not self._screen_monitor:
            return None
        if not self._screen_host_room_id or room_id != self._screen_host_room_id:
            return None
        manual_state = self._get_screen_manual_state()
        if manual_state is False:
            return None
        return self._screen_monitor.get_cached_summary()

    @staticmethod
    def _inject_screen_summary(prompt: str, summary: str) -> str:
        if not prompt or not summary:
            return prompt
        screen_block = f"【直播画面】{summary}"
        placeholder = "{extra_info_block}"
        if placeholder in prompt:
            return prompt.replace(placeholder, f"{placeholder}\n{screen_block}")
        return f"{screen_block}\n{prompt}"

    def _get_screen_manual_state(self) -> Optional[bool]:
        if self._screen_manual_state is None:
            return None
        if time.time() >= self._screen_manual_until:
            self._screen_manual_state = None
            self._screen_manual_until = 0.0
            return None
        return self._screen_manual_state

    def _handle_screen_manual_command(
        self,
        room_id: int,
        user_id: str,
        text: str,
        user_name: str,
    ) -> bool:
        if not self._screen_manual_enable:
            return False
        if not self._screen_host_room_id or room_id != self._screen_host_room_id:
            return False
        command = text.strip().lower()
        if command not in ("#screen_on", "#screen_off"):
            return False
        if self._screen_manual_user_ids and user_id not in self._screen_manual_user_ids:
            self.logger.warning(
                "Screen monitor manual command rejected: room_id=%s user_id=%s user_name=%s",
                room_id,
                user_id,
                user_name,
            )
            return True
        enable = command == "#screen_on"
        self._screen_manual_state = enable
        if enable:
            self._screen_manual_until = (
                time.time() + self._screen_manual_duration_seconds
            )
        else:
            # Permanent off until next command or restart
            self._screen_manual_until = float("inf")

            # Clear cached screen data so prompt builders stop injecting stale content
            if self._screen_monitor:
                self._screen_monitor._last_summary = None


        action = "enabled" if enable else "permanently disabled"
        self.logger.info(
            "Screen monitor manual %s for %s seconds by user_id=%s",
            action,
            self._screen_manual_duration_seconds,
            user_id,
        )
        return True

    def _handle_mic_manual_command(
        self,
        room_id: int,
        user_id: str,
        text: str,
        user_name: str,
    ) -> bool:
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return False

        if room_id != self.mic_worker.config.room_id:
            return False

        command = text.strip().lower()
        if command not in ("#asr_on", "#asr_off"):
            return False

        if self._screen_manual_user_ids and user_id not in self._screen_manual_user_ids:
            self.logger.warning(
                "Mic manual command rejected: room_id=%s user_id=%s user_name=%s",
                room_id,
                user_id,
                user_name,
            )
            return True

        enable = command == "#asr_on"
        self._mic_manual_state = enable

        action = "force enabled" if enable else "force disabled"
        self.logger.info("Mic capture %s by user_id=%s", action, user_id)
        return True

    async def _get_template_info(
        self, room_id: int, user_id: str, prompt_text: str
    ) -> Optional[TemplateInfo]:
        """
        Helper to resolve template info for live events (Gift/SC/Guard).
        """
        screen_summary = self._get_cached_screen_summary(room_id)
        reply_prompt, _ = self._resolve_live_prompts(room_id)

        if screen_summary:
            reply_prompt = self._inject_screen_summary(reply_prompt, screen_summary)

        if not reply_prompt:
            return None

        template_items: Dict[str, str] = {}
        if reply_prompt:
            template_items["replyer_prompt"] = reply_prompt
            template_items["reply_prompt"] = reply_prompt

        # Check if TTS is enabled to generate a distinct template name
        # This prevents prompt caching issues when hot-switching
        template_suffix = ""
        if self.tts_manager.is_tts_enabled(room_id):
            room_lang = self.tts_manager.get_room_language(room_id)
            template_suffix = f"_tts_{room_lang}"

        return TemplateInfo(
            template_items=template_items,
            template_name=f"bilibili_live_{room_id}{template_suffix}",
            template_default=False,
        )

    # ========== Incoming Message Handlers ==========

    async def _handle_test_command(
        self,
        room_id: int,
        user_id: str,
        text: str,
        user_name: str,
    ) -> bool:
        """
        Handle test commands for simulating live events.
        Only allows owner (dede_user_id) to trigger.
        Commands:
        - #test_gift: Simulate sending a gift
        - #test_sc <msg>: Simulate sending a superchat
        - #test_guard: Simulate opening a guard
        - #test_clear: Clear any temporary test state (placeholder)
        """
        if not (text.startswith("#test_") or text.startswith("#guard_")):
            return False

        # Security check: allow dede_user_id or manual control users
        allowed_ids = {str(self.config.dede_user_id)}
        if self.config.screen_manual_user_ids:
            allowed_ids.update(str(uid) for uid in self.config.screen_manual_user_ids)

        if str(user_id) not in allowed_ids:
            # Optional: Log attempt?
            # self.logger.warning(f"Unauthorized test command from {user_id}: {text}")
            return False

        if str(user_id) not in allowed_ids:
            return False

        # Robust parsing for commands that might be contiguous like #guard_enable=[...]
        cmd = text.split(" ")[0].split("=")[0].split("[")[0].strip()
        arg = ""
        # Try to extract argument part based on cmd length
        if len(text) > len(cmd):
            # The rest of the string is potential reference, but be careful of delimiters
            # e.g. "#guard_enable=[...]" -> cmd="#guard_enable"
            # We want arg="[...]"
            # Find closest delimiter index after cmd
            candidate_arg = text[len(cmd) :].strip()
            if (
                candidate_arg.startswith("=")
                or candidate_arg.startswith("[")
                or candidate_arg.startswith(" ")
            ):
                # Strip leading delimiters if it's just a separator, but [ might be part of JSON-like structure
                # Actually for #guard_enable=[...], the arg is [level:...]
                # For #test_sc msg, the arg is msg
                # Let's just strip leading space/equals, but preserve brackets for structure
                arg = candidate_arg.lstrip(" =")

        self.logger.info(f"Test command triggered: {cmd} args={arg} by {user_name}")

        now_ts = time.time()

        try:
            if cmd == "#test_gift":
                await self.handle_incoming_gift(
                    room_id=room_id,
                    gift_name="测试礼物(TestGift)",
                    num=1,
                    user_id=user_id,
                    user_name=user_name,
                    timestamp=now_ts,
                    price=100,  # Simulate a paid gift
                )
                await self._send_danmu(
                    room_id, "【测试】已触发模拟礼物事件", None, None
                )
                return True

            elif cmd == "#test_sc":
                msg = arg if arg else "这是测试SC内容(Test SC Message)"
                await self.handle_incoming_superchat(
                    room_id=room_id,
                    message_text=msg,
                    price=30,
                    user_id=user_id,
                    user_name=user_name,
                    timestamp=now_ts,
                )
                await self._send_danmu(room_id, "【测试】已触发模拟SC事件", None, None)
                return True

                return True

            elif cmd == "#guard_enable":
                # Parse args format: [level:<G/A/C>,message:<text>]
                # Simplified parsing: looking for pattern or just simplistic split
                # Expected arg: "[level:G,message:Hello]" or similar

                target_level = 0
                target_msg = "Test VIP Message"

                if arg:
                    # Remove brackets
                    clean_arg = arg.strip("[]")
                    parts = clean_arg.split(",")
                    for part in parts:
                        if ":" in part:
                            key, val = part.split(":", 1)
                            key = key.strip().lower()
                            val = val.strip()
                            if key == "level":
                                if val.upper() == "G":
                                    target_level = 1
                                elif val.upper() == "A":
                                    target_level = 2
                                elif val.upper() == "C":
                                    target_level = 3
                            elif key == "message":
                                target_msg = val

                if target_level > 0:
                    await self.handle_incoming_danmu(
                        room_id=room_id,
                        message_id=str(uuid.uuid4()),
                        text=target_msg,
                        user_id=user_id,
                        user_name=user_name,
                        timestamp=now_ts,
                        guard_level=target_level,
                    )
                    await self._send_danmu(
                        room_id,
                        f"【测试】模拟身份发言: Lv{target_level} - {target_msg}",
                        None,
                        None,
                    )
                else:
                    await self._send_danmu(
                        room_id,
                        "【测试】参数错误，用法: #guard_enable=[level:G/A/C,message:内容]",
                        None,
                        None,
                    )
                return True

            elif cmd == "#test_guard":
                await self.handle_incoming_guard(
                    room_id=room_id,
                    guard_name="舰长(Captain)",
                    num=1,
                    user_id=user_id,
                    user_name=user_name,
                    timestamp=now_ts,
                    guard_level=3,
                )
                await self._send_danmu(
                    room_id, "【测试】已触发模拟上舰事件", None, None
                )
                return True

        except Exception as e:
            self.logger.error(f"Test command execution failed: {e}")
            await self._send_danmu(room_id, f"【测试】执行失败: {e}", None, None)
            return True  # Still consume the message so it doesn't loop as normal chat

        return False

    async def handle_incoming_danmu(
        self,
        room_id: int,
        message_id: str,
        text: str,
        user_id: str,
        user_name: str,
        timestamp: float,
        reply_mid: str = "",
        reply_dmid: str = "",
        is_mentioned: bool = False,
        guard_level: int = 0,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        if not text:
            return
        if self._handle_mic_manual_command(room_id, user_id, text, user_name):
            return
        if self.tts_manager.handle_tts_manual_command(
            room_id,
            user_id,
            text,
            user_name,
            allowed_user_ids=set(
                str(uid) for uid in getattr(self.config, "screen_manual_user_ids", [])
            ),
        ):
            return
        if self._handle_screen_manual_command(room_id, user_id, text, user_name):
            return
        if await self._handle_test_command(room_id, user_id, text, user_name):
            return

        # Fallback: if bot danmu leaks past is_self_danmu, drop it based on bot_account config
        if self.config.bot_account and str(user_id) == str(self.config.bot_account):
            if self.config.live_log_danmu:
                self.logger.info(
                    "Danmu ignored (bot_account match): room_id=%s user_id=%s message_id=%s",
                    room_id,
                    user_id,
                    message_id,
                )
            return

        # if room_id in self._live_streamer_controllers:
        #     controller = self._live_streamer_controllers[room_id]
        #     controller.add_danmu_from_params(
        #         message_id=message_id,
        #         text=text,
        #         user_id=user_id,
        #         user_name=user_name,
        #         timestamp=timestamp,
        #     )
        #     self.logger.debug(
        #         f"[LiveStreamer] Danmu routed to buffer: {user_name}: {text[:30]}..."
        #     )
        #     return  # Don't process through normal path
        # Use cached screen summary (non-blocking); the worker refreshes it periodically.
        template_info = await self._get_template_info(room_id, user_id, text)
        if template_info:
            # Info logged inside _get_template_info
            pass
        additional_config = {
            "room_id": room_id,
            "reply_mid": reply_mid,
            "reply_dmid": reply_dmid,
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }
        if is_mentioned:
            additional_config["is_mentioned"] = 1.0

        if is_mentioned:
            additional_config["is_mentioned"] = 1.0

        # Sanitize text to prevent spoofing
        processed_text = self._sanitize_user_text(text)

        if not self.config.live_network_search_enabled:
            processed_text = _mask_urls(processed_text)
            additional_config["disable_tools"] = True

        # If has guard level, force mention to ensure processing if desired, or at least give priority
        # Guard Levels: 1=Governor (Zongdu), 2=Admiral (Tidu), 3=Captain (Jianzhang)
        # We assign higher priority to higher ranks
        priority_segment = None
        if guard_level > 0:
            # VIP Logic
            priority_score = 1000.0  # Default Captain
            if guard_level == 1:
                priority_score = 2000.0  # Governor (Highest)
            elif guard_level == 2:
                priority_score = 1500.0  # Admiral

            # Ensure VIPs are treated as mentioned so bot pays attention
            additional_config["is_mentioned"] = 1.0

            priority_segment = Seg(
                type="priority_info",
                data={
                    "message_type": "vip",
                    "message_priority": priority_score,
                },
            )

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=str(message_id),
            time=float(timestamp),
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        # Notify the standalone avatar without constructing NachoBot chat objects.
        if self.live2d_manager.controller:
            try:
                await self.live2d_manager.controller.on_message_received()
            except Exception as e:
                self.logger.error(f"Live2D hook error: {e}")

        # Construct message content
        text_segment = Seg(type="text", data=processed_text)

        final_segment = text_segment
        if priority_segment:
            final_segment = Seg(type="seglist", data=[priority_segment, text_segment])

        message = MessageBase(
            message_info=message_info,
            message_segment=final_segment,
            raw_message=None,
        )

        # Priority selection:
        # 10: High (SC, Guard, Admin)
        # 20: VIP / Mention / High Value Gift
        # 30: Aggregated Gift
        # 40: Normal Danmu
        priority = 40
        if guard_level > 0 or is_mentioned:
            priority = 20

        self.event_manager.push_to_event_queue(priority, message)

    async def handle_incoming_gift(
        self,
        room_id: int,
        gift_name: str,
        num: int,
        user_id: str,
        user_name: str,
        timestamp: float,
        price: int = 0,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        self.logger.info(
            f"Gift: [{room_id}] {user_name}({user_id}) sent {gift_name} x{num} (Price: {price})"
        )

        # Aggregation Logic
        # Buffer small gifts (< 20 CNY total value) to prevent spam
        if price * num < 20:
            key = (room_id, user_id, gift_name)
            if key not in self.event_manager.gift_buffer:
                self.event_manager.gift_buffer[key] = {
                    "count": 0,
                    "price": price,  # Unit price
                    "timestamp": timestamp,
                    "user_name": user_name,
                }
            self.event_manager.gift_buffer[key]["count"] += num
            self.event_manager.gift_buffer[key]["price"] = price
            self.event_manager.gift_buffer[key]["timestamp"] = (
                timestamp  # Update to latest
            )
            self.event_manager.last_gift_time[key] = time.time()  # Update act time
            return

        # Build prompt using helper
        prompt_text = f"送出了 {gift_name} x{num}"
        template_info = await self._get_template_info(room_id, user_id, prompt_text)

        # Prepare additional config for high value gifts logic if needed
        # Ensuring mention logic is consistent
        additional_config = {
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }
        # if template_info:
        #    additional_config = template_info.additional_config or {}

        # Force mention for gifts to ensure reaction
        additional_config["is_mentioned"] = 1.0

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=str(uuid.uuid4()),
            time=timestamp,
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        # Use seglist to include both gift info and text prompt
        # Include gift metadata alongside readable HeartFlow text.
        gift_segment = Seg(type="gift", data=f"{gift_name}:{num}")
        text_segment = Seg(type="text", data=prompt_text)

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=[gift_segment, text_segment]),
            raw_message=json.dumps(
                {
                    "type": "gift",
                    "gift_name": gift_name,
                    "num": num,
                    "price": price,
                    "room_id": room_id,
                },
                ensure_ascii=True,
            ),
        )

        # Push to Queue (Priority 20 for High Value Gifts)
        self.event_manager.push_to_event_queue(20, message)

    async def _handle_mic_recognition(self, text: str) -> None:
        if not text:
            return
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return
        room_id = self.mic_worker.config.room_id
        await self.handle_mic_message(room_id, text)

        # Resume Audio Player after speech is acknowledged
        self.audio_player.resume()

    async def handle_incoming_poke(
        self,
        room_id: int,
        user_id: str,
        user_name: str,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        """
        Handle a poke event (simulated or real).
        """
        self.logger.info(f"Poke event received from {user_name} ({user_id})")

        timestamp = time.time()

        # Standard format for poke/notice
        text = f"{user_name}用鼠标戳了戳你"

        # Resolve template info to ensuring correct persona/TTS settings
        template_info = await self._get_template_info(room_id, user_id, text)

        additional_config = {
            "room_id": room_id,
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            # Special ID for notice messages as seen in bot.py logic
            message_id="notice",
            time=timestamp,
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=text),
            raw_message=None,
        )

        # High priority to ensure immediate reaction
        self.event_manager.push_to_event_queue(20, message)

    async def _call_asr_api(self, wav_data: bytes) -> Optional[str]:
        import aiohttp

        asr_config = self._load_asr_model_config()
        if not asr_config:
            self.logger.warning("ASR config not available, cannot process speech")
            return None

        try:
            data = aiohttp.FormData()
            data.add_field(
                "file", wav_data, filename="audio.wav", content_type="audio/wav"
            )
            data.add_field("model", asr_config.model)
            data.add_field("language", "zh")
            # Use a proper Chinese context prompt to guide the model, which reduces hallucinations
            # and improves recognition of Chinese over strange languages.
            data.add_field("prompt", "这是一段中文普通话日常对话录音。")

            headers = {"Authorization": f"Bearer {asr_config.api_key}"}
            url = f"{asr_config.base_url}/audio/transcriptions"

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=data, headers=headers, timeout=asr_config.timeout
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.logger.error(
                            f"ASR API error: {resp.status} - {error_text}"
                        )
                        return None

                    result = await resp.json()
                    text = str(result.get("text", "")).strip()
                    if text:
                        # First strip emojis properly
                        text = _strip_emoji(text).strip()
                        # Sanitize text to remove control characters (except newlines/tabs)
                        text = "".join(
                            ch for ch in text if ch.isprintable() or ch in "\n\r\t"
                        )
                        # Then remove trailing punctuation
                        text = text.rstrip("。?.，,！!？ ")
                        
                        # Filter out common Whisper hallucinations for silence/noise
                        lower_text = text.lower()
                        hallucinations = [
                            "그", "thank you", "you", "amara.org", "subtitles", 
                            "啊", "嗯", "那", "这", "好的"
                        ]
                        
                        # Also check if it contains Korean characters (common whisper hallucination)
                        import re
                        if re.search(r'[\uac00-\ud7a3]', text):
                            self.logger.debug(f"Filtered out ASR hallucination (Korean): {text}")
                            return None
                            
                        # Check exact matches or substrings for English hallucinations
                        if any(h == lower_text for h in hallucinations) or "amara.org" in lower_text:
                            self.logger.debug(f"Filtered out ASR hallucination: {text}")
                            return None

                    return text

        except Exception as e:
            self.logger.error(f"ASR API call failed: {e}")
            return None

    async def handle_mic_message(self, room_id: int, text: str) -> None:
        self.tts_manager.reset_idle_timer()
        additional_config = {
            "room_id": room_id,
            "is_mentioned": 2.0,
            "source": "mic_asr",
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }

        # 修复：从配置动态读取主人的 ID 和名字，消除硬编码
        master_user_id = str(getattr(self.config, "live_master_user_id", "2146014839"))
        master_user_name = str(getattr(self.config, "live_master_user_name", "主人"))

        # [FIX] Include template_info so mic messages don't clobber
        # last_messages with a template-less entry.
        template_info = await self._get_template_info(room_id, master_user_id, text)

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=f"mic_{int(time.time() * 1000)}",
            time=time.time(),
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=master_user_id,
                user_nickname=master_user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        processed_text = text
        if not self.config.live_network_search_enabled:
            processed_text = _mask_urls(processed_text)

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=processed_text),
            raw_message=None,
        )
        # Bypass queue for immediate core processing
        asyncio.create_task(self._send_to_nachobot(message))

    async def handle_incoming_superchat(
        self,
        room_id: int,
        message_text: str,
        price: int,
        user_id: str,
        user_name: str,
        timestamp: float,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        self.logger.info(
            f"SuperChat: [{room_id}] {user_name}({user_id}): {message_text} (Price: {price} CNY)"
        )

        # if room_id in self._live_streamer_controllers:
        #     controller = self._live_streamer_controllers[room_id]
        #     await controller.inject_priority_event(
        #         PriorityEvent(
        #             event_type="superchat",
        #             user_name=user_name,
        #             user_id=user_id,
        #             timestamp=timestamp,
        #             sc_message=message_text,
        #             sc_price=price,
        #         )
        #     )
        #     self.logger.info("[LiveStreamer] SC injected as priority event")
        #     return  # Don't process through normal path

        # Build prompt using helper
        prompt_text = f"发送了超级弹幕(SC)：{message_text} (价值 {price} 元)"
        template_info = await self._get_template_info(room_id, user_id, prompt_text)

        additional_config = {
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }
        # if template_info:
        #    additional_config = template_info.additional_config or {}

        # Force mention for SC to ensure reaction
        additional_config["is_mentioned"] = 1.0

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=str(uuid.uuid4()),
            time=timestamp,
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        # Include structured SC metadata alongside readable HeartFlow text.
        # Superchat segment format: "price:text"
        sc_segment = Seg(type="superchat", data=f"{price}:{message_text}")
        text_segment = Seg(type="text", data=prompt_text)

        final_segment = Seg(type="seglist", data=[sc_segment, text_segment])

        message = MessageBase(
            message_info=message_info,
            message_segment=final_segment,
            raw_message=json.dumps(
                {
                    "type": "superchat",
                    "text": message_text,
                    "price": price,
                    "room_id": room_id,
                },
                ensure_ascii=True,
            ),
        )

        # Bypass queue for immediate core processing
        asyncio.create_task(self._send_to_nachobot(message))

    async def handle_incoming_guard(
        self,
        room_id: int,
        guard_name: str,
        num: int,
        user_id: str,
        user_name: str,
        timestamp: float,
        guard_level: int = 3,
        price: int = 0,
        **kwargs,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        self.logger.info(
            f"Guard: [{room_id}] {user_name}({user_id}) became {guard_name} (Level: {guard_level}) - PATCHED_VERIFIED"
        )

        # if room_id in self._live_streamer_controllers:
        #     controller = self._live_streamer_controllers[room_id]
        #     await controller.inject_priority_event(
        #         PriorityEvent(
        #             event_type="guard",
        #             user_name=user_name,
        #             user_id=user_id,
        #             timestamp=timestamp,
        #             guard_name=guard_name,
        #             guard_level=guard_level,
        #         )
        #     )
        #     self.logger.info("[LiveStreamer] Guard injected as priority event")
        #     return  # Don't process through normal path

        prompt_text = f"开通了 {guard_name} ({num} 个月)"
        template_info = await self._get_template_info(room_id, user_id, prompt_text)

        additional_config = {
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }
        # if template_info:
        #    additional_config = template_info.additional_config or {}

        # Force mention for Guardian to ensure reaction
        additional_config["is_mentioned"] = 1.0

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=str(uuid.uuid4()),
            time=timestamp,
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        # Priority Info for VIP
        priority_segment = Seg(
            type="priority_info",
            data={
                "message_type": "vip",
                "message_priority": 1000.0,
            },
        )
        text_segment = Seg(type="text", data=prompt_text)

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=[priority_segment, text_segment]),
            raw_message=json.dumps(
                {
                    "type": "guard",
                    "guard_name": guard_name,
                    "num": num,
                    "level": guard_level,
                    "room_id": room_id,
                    "price": price,
                },
                ensure_ascii=True,
            ),
        )

        # Bypass queue for immediate core processing
        asyncio.create_task(self._send_to_nachobot(message))

    async def handle_incoming_guard_entry(
        self,
        room_id: int,
        user_id: str,
        user_name: str,
        guard_level: int,
        timestamp: float,
    ) -> None:
        self.tts_manager.reset_idle_timer()
        """Handle a guard-level (大航海) user entering the live room."""
        guard_label = {1: "总督", 2: "提督", 3: "舰长"}.get(guard_level, "舰长")
        self.logger.info(
            f"GuardEntry: [{room_id}] {user_name}({user_id}) entered as {guard_label}"
        )

        prompt_text = f"{guard_label} {user_name} 进入了直播间"
        template_info = await self._get_template_info(room_id, user_id, prompt_text)

        additional_config = {
            "room_id": room_id,
            "is_mentioned": 1.0,
            "live_person_profile_enabled": self.config.live_person_profile_enabled,
        }

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id="notice",
            time=timestamp,
            user_info=UserInfo(
                platform="bilibili.live",
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform="bilibili.live",
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=prompt_text),
            raw_message=None,
        )

        # Priority 20: same as VIP/Mention to ensure bot notices and greets
        self.event_manager.push_to_event_queue(20, message)

    # ========== Prompt Resolution ==========

    @staticmethod
    def _build_live_plan_block(room_prompts: Optional[Dict[str, str]]) -> str:
        if not room_prompts:
            return ""
        sections = (
            ("直播分类", room_prompts.get("live_category", "")),
            ("直播标题", room_prompts.get("live_title", "")),
            ("直播内容", room_prompts.get("live_content", "")),
            ("直播细节", room_prompts.get("live_detail", "")),
        )
        lines: List[str] = []
        for label, value in sections:
            content = str(value or "").strip()
            if content:
                lines.append(f"{label}：{content}")
        if not lines:
            return ""
        return "以下是本场直播计划，请在回复时参考：\n" + "\n".join(lines)

    @staticmethod
    def _inject_live_plan_into_prompt(reply_prompt: str, live_plan_block: str) -> str:
        if not reply_prompt or not live_plan_block:
            return reply_prompt
        placeholder = "{extra_info_block}"
        if placeholder in reply_prompt:
            return reply_prompt.replace(
                placeholder, f"{placeholder}\n{live_plan_block}"
            )
        return f"{live_plan_block}\n{reply_prompt}"

    def _resolve_live_prompts(self, room_id: int) -> Tuple[str, str]:
        reply_prompt = self.config.live_reply_prompt
        planner_prompt = self.config.live_planner_prompt

        room_prompts = self.config.live_room_prompts.get(room_id)
        live_plan_block = self._build_live_plan_block(room_prompts)
        if room_prompts is not None:
            room_reply = str(room_prompts.get("reply_prompt", "") or "")
            room_planner = str(room_prompts.get("planner_prompt", "") or "")
            if room_reply:
                reply_prompt = room_reply
            if room_planner:
                planner_prompt = room_planner
        if reply_prompt and live_plan_block:
            reply_prompt = self._inject_live_plan_into_prompt(
                reply_prompt, live_plan_block
            )

        tts_enable = self.tts_manager.is_tts_enabled(room_id)
        tts_config = {}
        if room_prompts:
            tts_config = room_prompts.get("tts", {})

        if tts_enable:
            # Check for TTS-specific prompts in config
            tts_reply = str(tts_config.get("reply_prompt", "") or "")
            tts_planner = str(tts_config.get("planner_prompt", "") or "")

            # If specified, override the prompts
            if tts_reply:
                reply_prompt = tts_reply
            if tts_planner:
                planner_prompt = tts_planner

            # Determine language mode for this room
            room_lang = self.tts_manager.get_room_language(room_id)

            if room_lang == "zh":
                # Chinese-only TTS mode: tell model to output plain Chinese, no bilingual tags
                zh_tts_instruction = (
                    "\n\n语音模式已切换为中文。请只用中文回复，禁止使用<JP><ZH>标签，不要进行日语翻译。"
                    "直接输出中文回复内容，语音系统会直接朗读你的中文回复。"
                )
                # Strip any pre-existing bilingual instructions from the prompt
                reply_prompt = re.sub(
                    r"非常重要：请必须同时输出中文回复和对应的日文翻译.*?(?=\n\n|\Z)",
                    "",
                    reply_prompt,
                    flags=re.DOTALL,
                )
                reply_prompt += zh_tts_instruction
                self.logger.debug(
                    f"Appending Chinese-only TTS instruction to prompt for room {room_id}"
                )
            else:
                # Default: Bilingual JP+ZH mode
                # Fallback: append generic instructions if no specific reply prompt was found
                # (Or if the custom one is missing the required XML instructions)
                if "<JP>" not in reply_prompt and "<ZH>" not in reply_prompt:
                    tts_instruction = (
                        "\n\n非常重要：请必须同时输出中文回复和对应的日文翻译（用于语音播放），格式严格如下：\n"
                        "<JP>日本語翻訳</JP><ZH>中文原本意思</ZH>\n"
                        "例如：\n"
                        "<JP>こんにちは、ご飯を食べましたか？</JP><ZH>你好呀，吃过饭了吗？</ZH>\n"
                    )
                    self.logger.debug(
                        f"Appending TTS instruction to prompt for room {room_id}"
                    )
                    reply_prompt += tts_instruction
        else:
            # Force disable XML if TTS is off (Circuit Breaker for Context Pollution)
            anti_tts_instruction = (
                "\n语音已关闭，禁止使用<JP><ZH>标签，不要进行日语翻译，只输出中文。"
            )
            reply_prompt += anti_tts_instruction

        if reply_prompt and "{person_profile_block}" not in reply_prompt:
            injection_point = "{extra_info_block}"
            if injection_point in reply_prompt:
                reply_prompt = reply_prompt.replace(
                    injection_point, f"{injection_point}\n{{person_profile_block}}", 1
                )
            else:
                reply_prompt += "\n{person_profile_block}"
        if reply_prompt:
            reply_prompt += "\n{moderation_prompt}"
        if planner_prompt:
            planner_prompt += "\n{moderation_prompt}"

        self.logger.debug(
            f"Resolved prompts for room {room_id}: tts_enable={tts_enable}"
        )
        return reply_prompt, planner_prompt

    # ========== Handle From NachoBot ==========

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        await self.outgoing_handler.handle_from_nachobot(raw_message_base_dict)

    async def _send_danmu(
        self,
        room_id: int,
        text: str,
        reply_mid: Optional[str],
        reply_dmid: Optional[str],
    ) -> None:
        await self.outgoing_handler._send_danmu(room_id, text, reply_mid, reply_dmid)

    def is_self_danmu(
        self,
        room_id: int,
        user_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        return self.outgoing_handler.is_self_danmu(room_id, user_id, message_id, text)

    # ========== Command Handlers ==========

    async def _send_to_nachobot(self, message: MessageBase) -> None:
        self.logger.info(
            "Forward to NachoBot: platform=%s group_id=%s message_id=%s",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            message.message_info.message_id,
        )
        if self.config.disable_command_trigger:
            _guard_command_segment(message.message_segment)
        if self.config.disable_video_sender_plugin and message.raw_message:
            message.raw_message = None
        client = self.router.clients.get(self.config.platform)
        if client is not None:
            await client.send_message(message.to_dict())
            return
        await self.router.send_message(message)

    # ========== Private Message Loop ==========

    @staticmethod
    def _render_gift_text(gift_name: str, num: int, price: int = 0) -> str:
        """Render gift event to human-readable text."""
        verb = "开通了" if gift_name in ["舰长", "提督", "总督"] else "送出了"
        price_suffix = f"（价值{price}元）" if price > 0 else ""
        return f"{verb} {gift_name} x{num}{price_suffix}"

    async def _create_live_message_info(
        self,
        message_id: str,
        timestamp: float,
        room_id: int,
        user_id: str,
        user_name: str,
        additional_config: Optional[Dict[str, Any]] = None,
    ) -> BaseMessageInfo:
        # 直播消息使用 bilibili 平台
        live_platform = "bilibili"

        if additional_config is None:
            additional_config = {}
        additional_config.setdefault(
            "live_person_profile_enabled",
            self.config.live_person_profile_enabled,
        )

        return BaseMessageInfo(
            platform=live_platform,
            message_id=message_id,
            time=float(timestamp),
            user_info=UserInfo(
                platform=live_platform,
                user_id=user_id,
                user_nickname=user_name,
            ),
            group_info=GroupInfo(
                platform=live_platform,
                group_id=str(room_id),
                group_name=str(room_id),
            ),
            format_info=FormatInfo(
                content_format=["text"],
                accept_format=ACCEPT_FORMAT,
            ),
            additional_config=additional_config,
        )

    # ========== Event Serialization & Gift Aggregation ==========
