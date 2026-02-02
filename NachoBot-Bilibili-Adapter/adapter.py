"""Main BilibiliAdapter class for Bilibili live streaming integration."""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
import winsound
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Deque
import queue
import wave
import io
import itertools

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

from config import (  # noqa: E402
    AdapterConfig,
    AsrModelConfig,
    PrivateSessionConfig,
    VlmModelConfig,
    _resolve_asr_model_config,
    _resolve_vlm_model_config,
)
from utils import (  # noqa: E402
    BILIBILI_DANMU_MAX_LENGTH,
    _clean_text_for_tts,
    _decode_image_base64,
    _extract_image_base64,
    _extract_plain_text,
    _find_reply_id,
    _guard_command_segment,
    _mask_urls,
    _normalize_text,
    _split_bilibili_text,
    _strip_emoji,
    _URL_RE,
)
from api import BilibiliApi  # noqa: E402
from live_worker import LiveRoomWorker  # noqa: E402
from screen_monitor import ScreenMonitor  # noqa: E402
from mic_capture import MicCaptureWorker, MicConfig  # noqa: E402
# [DEPRECATED] Live Streamer mode moved to mais4u
# from live_streamer import LiveStreamerController, PriorityEvent  # noqa: E402

# Try to import TTS model
tts_adapter_path = Path(r"C:\Users\BigSh0t\Nacho-with-u\NachoBot-TTS-Adapter")
TTSModel = None
_tts_import_error = None

if tts_adapter_path.exists():
    if str(tts_adapter_path) not in sys.path:
        sys.path.insert(0, str(tts_adapter_path))
    try:
        from src.plugins.GPT_Sovits.tts_model import TTSModel
    except ImportError as e:
        _tts_import_error = str(e)
    except Exception as e:
        _tts_import_error = f"Unexpected error: {e}"
else:
    _tts_import_error = f"TTS adapter path does not exist: {tts_adapter_path}"

ACCEPT_FORMAT = ["text", "reply", "command"]
ACCEPT_FORMAT_PRIVATE = ["text", "image", "emoji", "reply", "command"]
COMMENT_REPLY_LIMIT = 10
COMMENT_LIMIT_FALLBACK_TEXT = "NachoBot有点口渴了哦，先休息一下啦~"
BILIBILI_DANMU_SEND_DELAY_SECONDS = 0.8


class AudioPlayer:
    """
    Manages audio playback with support for queuing, interruption, and resuming.
    Uses winsound for playback and calculates duration for timing.
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.queue: Deque[bytes] = queue.deque()
        self.current_audio: Optional[bytes] = None
        self.interrupted_audio: Optional[bytes] = None
        self.is_playing = False
        self.is_paused = False
        self.stop_event = asyncio.Event()  # Set when stopped/interrupted
        self.play_task: Optional[asyncio.Task] = None
        self._loop = None

    def start(self):
        """Start the playback loop."""
        if self.play_task and not self.play_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self.stop_event.clear()
        self.play_task = self._loop.create_task(self._playback_loop())
        self.logger.info("AudioPlayer started")

    async def _playback_loop(self):
        while True:
            try:
                if self.is_paused:
                    await asyncio.sleep(0.1)
                    continue

                if not self.queue:
                    await asyncio.sleep(0.1)
                    continue

                # Get next audio
                audio_data = self.queue.popleft()
                self.current_audio = audio_data
                self.is_playing = True

                # Calculate duration
                duration = self._get_wav_duration(audio_data)
                # self.logger.debug(f"Playing audio segment ({duration:.2f}s)")

                # Play (Async)
                self._play_sound(audio_data)

                # Wait for duration (or interruption)
                # We wait for duration, checking stop_event periodically or using wait_for
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration)
                    # If we got here, stop_event was set (Interrupted!)
                    self.logger.info("Audio playback interrupted!")
                    self._stop_sound()
                    self.interrupted_audio = self.current_audio  # Save current
                    self.current_audio = None
                except asyncio.TimeoutError:
                    # Finished playing naturally
                    pass

                self.current_audio = None
                self.is_playing = False
                self.stop_event.clear()  # Reset for next

            except Exception as e:
                self.logger.error(f"AudioPlayer loop error: {e}")
                await asyncio.sleep(1)

    def _play_sound(self, audio_data: bytes):
        try:
            # Save to temp
            temp_path = os.path.join(tempfile.gettempdir(), "nachobot_tts_player.wav")
            with open(temp_path, "wb") as f:
                f.write(audio_data)
            winsound.PlaySound(temp_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self.logger.error(f"Winsound play error: {e}")

    def _stop_sound(self):
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _get_wav_duration(self, audio_data: bytes) -> float:
        try:
            with io.BytesIO(audio_data) as f:
                with wave.open(f, "rb") as wav_file:
                    frames = wav_file.getnframes()
                    rate = wav_file.getframerate()
                    return frames / float(rate)
        except Exception:
            return 2.0  # Fallback

    def play(self, audio_data: bytes):
        """Add audio to queue."""
        self.queue.append(audio_data)

    def stop_and_pause(self):
        """Stop current playback immediately and pause."""
        self.is_paused = True
        self.stop_event.set()  # Signal loop to stop waiting
        self.logger.info("AudioPlayer stopped and paused.")

    def resume(self):
        """Resume playback, re-queueing interrupted audio."""
        if self.interrupted_audio:
            self.logger.info("Resuming interrupted audio...")
            self.queue.appendleft(self.interrupted_audio)
            self.interrupted_audio = None
        self.is_paused = False
        self.stop_event.clear()  # Ensure clear
        self.logger.info("AudioPlayer resumed.")


class BilibiliAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                ),
                # 直播消息使用 bilibili.live 平台，走 S4U 系统
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
        self._reply_seen: List[str] = []
        self._reply_seen_set: set[str] = set()
        self._dm_last_seqno: Dict[Tuple[int, int], int] = {}
        self._last_private_session: Optional[PrivateSessionConfig] = None
        self._private_session_by_group: Dict[str, PrivateSessionConfig] = {}
        self._auto_private_sessions: List[PrivateSessionConfig] = []
        self._auto_private_sessions_ts: float = 0.0
        self._user_name_cache: Dict[str, Tuple[str, float]] = {}
        self._user_name_cache_seconds = 3600
        self._comment_context: Dict[str, Dict[str, Any]] = {}
        self._comment_bootstrap_done = False
        self._comment_reply_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._self_danmu_ids: Dict[int, Dict[str, float]] = {}

        # TTS Buffering
        self._tts_buffer: Dict[int, List[str]] = {}
        self._tts_timer: Dict[int, asyncio.Task] = {}
        self._tts_metadata: Dict[int, Dict[str, Any]] = {}

        # Event Serialization & Aggregation
        self._event_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._core_ready_event: asyncio.Event = asyncio.Event()
        self._core_ready_event.set()  # Initially ready
        self._event_lock_timestamp: float = 0.0
        self._seq_counter = itertools.count()

        # Gift Aggregation: (room_id, user_id, gift_name) -> {'count': int, 'price': int, 'timestamp': float, 'user_name': str}
        self._gift_buffer: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
        self._last_gift_time: Dict[Tuple[int, str, str], float] = {}

        # Initialize Mic Capture Worker
        self.mic_worker: Optional[MicCaptureWorker] = None
        self._mic_manual_state: Optional[bool] = None
        if config.mic_asr_enable and config.mic_asr_room_id:
            mic_config = MicConfig(
                enable=config.mic_asr_enable,
                room_id=config.mic_asr_room_id,
                subtitle_path=config.mic_asr_subtitle_path,
                silence_threshold=config.mic_asr_silence_threshold,
                silence_duration=config.mic_asr_silence_duration,
                sample_rate=config.mic_asr_sample_rate,
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
            monitor_config = self._load_vlm_model_config()
            if monitor_config:
                self._screen_monitor = ScreenMonitor(monitor_config, logger)
            else:
                self.logger.warning("Screen monitor disabled: VLM config unavailable")
        else:
            self.logger.info("Screen monitor disabled: host room not configured")

        # Initialize TTS
        self.tts_model: Optional["TTSModel"] = None
        self.tts_enable = False
        self.subtitle_path = "subtitles.txt"

        if self.config.live_room_prompts:
            for room_cfg in self.config.live_room_prompts.values():
                if room_cfg.get("tts", {}).get("enable"):
                    self.tts_enable = True
                    self.subtitle_path = room_cfg.get("tts", {}).get(
                        "subtitle_path", "subtitles.txt"
                    )
                    break

        if self.tts_enable:
            if TTSModel:
                try:
                    self.tts_model = TTSModel()
                    self.logger.info("TTS Model initialized successfully")
                except Exception as e:
                    self.logger.error(f"Failed to initialize TTS Model: {e}")
            else:
                self.logger.error(
                    f"TTS enabled but TTSModel not available: {_tts_import_error}"
                )

        # Initialize AudioPlayer
        self.audio_player = AudioPlayer(logger)

        # [DEPRECATED] Live Streamer mode moved to mais4u
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

    # ========== Run and Control Methods ==========

    async def run(self) -> None:
        await self.api.start()
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
            tasks.append(self._comment_notice_loop())
        if self.config.private_enable and (
            self.config.private_sessions or self.config.private_auto_sessions
        ):
            tasks.append(self._private_message_loop())

        if self.mic_worker:
            tasks.append(self._run_mic_worker_forever())
            tasks.append(self._mic_control_loop())

        # Event Queue and Gift Aggregation
        tasks.append(self._process_event_queue())
        tasks.append(self._gift_flush_loop())

        # [DEPRECATED] Live Streamer mode moved to mais4u
        # for controller in self._live_streamer_controllers.values():
        #     tasks.append(controller.start())

        self.audio_player.start()  # Start audio player loop

        await asyncio.gather(*tasks)

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

                # [DEPRECATED] Live Streamer mode moved to mais4u
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

    def _load_vlm_model_config(self) -> Optional[VlmModelConfig]:
        root_dir = Path(__file__).resolve().parents[1]
        model_config_path = root_dir / "NachoBot" / "config" / "model_config.toml"
        return _resolve_vlm_model_config(model_config_path, self.logger)

    def _load_asr_model_config(self) -> Optional[AsrModelConfig]:
        root_dir = Path(__file__).resolve().parents[1]
        model_config_path = root_dir / "NachoBot" / "config" / "model_config.toml"
        return _resolve_asr_model_config(model_config_path, self.logger)

    # ========== TTS Methods ==========

    def _parse_bilingual_response(self, text: str) -> Tuple[str, str]:
        if not text:
            return "", ""

        # Use findall to capture all occurrences (handling recurrent tags in buffered text)
        jp_matches = re.findall(r"<JP>(.*?)</JP>", text, re.DOTALL)
        zh_matches = re.findall(r"<ZH>(.*?)</ZH>", text, re.DOTALL)

        text_jp = "".join(m.strip() for m in jp_matches if m.strip())
        text_zh = "".join(m.strip() for m in zh_matches if m.strip())

        if not text_jp and not text_zh:
            cleaned = re.sub(r"</?[A-Z]{2}>", "", text).strip()
            return "", cleaned

        return text_jp, text_zh

    async def _on_speech_start(self):
        """Callback when user starts speaking."""
        # Stop audio player immediately
        self.audio_player.stop_and_pause()

    async def _play_audio(self, audio_data: bytes) -> None:
        if not audio_data:
            return
        # Add to player queue
        self.audio_player.play(audio_data)

    def _update_subtitle(self, text: str, subtitle_path: str = None) -> None:
        if not text:
            return

        target_path = subtitle_path or self.subtitle_path

        try:
            with open(target_path, "w", encoding="utf-8-sig") as f:
                f.write(text)
            self.logger.info(f"Subtitle updated: {target_path}")
        except Exception as e:
            self.logger.error(f"Failed to update subtitle: {e}")

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

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def _extract_private_image_url(self, content: Any) -> str:
        image_keys = (
            "url",
            "image_url",
            "img_url",
            "image",
            "img",
            "src",
            "origin_url",
            "original_url",
            "preview",
            "cover",
            "thumb",
            "pic",
            "pic_url",
            "picture",
            "photo",
            "face",
            "raw_url",
        )

        def scan(value: Any) -> str:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith(("http://", "https://", "//")):
                    return candidate
                match = _URL_RE.search(candidate)
                return match.group(0) if match else ""
            if isinstance(value, dict):
                for key in image_keys:
                    if key in value:
                        found = scan(value.get(key))
                        if found:
                            return found
                for item in value.values():
                    found = scan(item)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = scan(item)
                    if found:
                        return found
            return ""

        content_value: Any = content
        if isinstance(content, str):
            trimmed = content.strip()
            if trimmed.startswith("{") or trimmed.startswith("["):
                try:
                    content_value = json.loads(trimmed)
                except json.JSONDecodeError:
                    content_value = content
        url = scan(content_value)
        return self._normalize_image_url(url)

    async def _download_private_image(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            return await self.api.fetch_base64(url)
        except Exception as exc:
            self.logger.warning(
                "Private image download failed: url=%s error=%s", url, exc
            )
            return None

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

    def _handle_tts_manual_command(
        self,
        room_id: int,
        user_id: str,
        text: str,
        user_name: str,
    ) -> bool:
        command = text.strip().lower()
        if command not in ("#tts_on", "#tts_off"):
            return False

        # Permission check: Owner or Manual Users
        allowed = False
        if str(user_id) == str(self.config.dede_user_id):
            allowed = True
        elif self._screen_manual_user_ids and str(user_id) in [
            str(uid) for uid in self._screen_manual_user_ids
        ]:
            allowed = True

        if not allowed:
            self.logger.warning(
                "TTS manual command rejected: room_id=%s user_id=%s user_name=%s",
                room_id,
                user_id,
                user_name,
            )
            return True

        enable = command == "#tts_on"

        # Update Config
        if self.config.live_room_prompts and room_id in self.config.live_room_prompts:
            room_config = self.config.live_room_prompts[room_id]
            if "tts" not in room_config:
                room_config["tts"] = {}
            room_config["tts"]["enable"] = enable

            action = "Enabled" if enable else "Disabled"
            self.logger.info("TTS %s manually by user_id=%s", action, user_id)

        return True

    def _get_template_info(
        self, room_id: int, user_id: str, prompt_text: str
    ) -> Optional[TemplateInfo]:
        """
        Helper to resolve template info for live events (Gift/SC/Guard).
        """
        reply_prompt, planner_prompt = self._resolve_live_prompts(room_id)
        if not reply_prompt and not planner_prompt:
            return None

        # Inject screen summary if available (optional for events, but good for context)
        # However, getting screen summary is async, and this helper is sync in usage pattern?
        # Usage: template_info = self._get_template_info(...) inside async methods.
        # But wait, the original usage didn't await it?
        # "template_info = self._get_template_info(room_id, user_id, prompt_text)"
        # If it's sync, we can't await _get_screen_summary.
        # Let's keep it simple for events: just resolve prompts.

        template_items: Dict[str, str] = {}
        if reply_prompt:
            template_items["replyer_prompt"] = reply_prompt
        if planner_prompt:
            template_items["brain_planner_prompt"] = planner_prompt
            template_items["planner_prompt"] = planner_prompt

        return TemplateInfo(
            template_items=template_items,
            template_name=f"bilibili_live_{room_id}",
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

    async def push_screen_update(
        self,
        room_id: int,
        user_id: str = "0",
        user_name: str = "System",
        timestamp: float = 0.0,
        existing_summary: Optional[str] = None,
    ):
        """
        Proactively push screen info to Core.
        If existing_summary is provided, use it. Otherwise, fetch new summary.
        """
        if timestamp == 0.0:
            timestamp = time.time()

        if existing_summary:
            summary = existing_summary
        else:
            # Trigger VLM analysis with generic prompt
            summary = await self._get_screen_summary(
                room_id, user_id, "Checking screen content"
            )

        if summary:
            try:
                screen_msg_info = BaseMessageInfo(
                    platform="bilibili.live",
                    message_id=f"screen_{uuid.uuid4().hex[:8]}",
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
                    additional_config={"room_id": room_id},
                )
                screen_message = MessageBase(
                    message_info=screen_msg_info,
                    # Send as 'screen' type so s4u_msg_processor recognized it and updates ScreenManager
                    message_segment=Seg(type="screen", data=summary),
                    raw_message=None,
                )
                # Priority 5 (Higher than Mic/SC) to ensure context update first
                self._push_to_event_queue(5, screen_message)
                self.logger.info(
                    f"Screen Info sent to Core for room {room_id} (Summary length: {len(summary)})"
                )
            except Exception as e:
                self.logger.error(f"Failed to push screen update: {e}")

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
        if not text:
            return
        if self._handle_mic_manual_command(room_id, user_id, text, user_name):
            return
        if self._handle_tts_manual_command(room_id, user_id, text, user_name):
            return
        if self._handle_screen_manual_command(room_id, user_id, text, user_name):
            return
        if await self._handle_test_command(room_id, user_id, text, user_name):
            return

        # [DEPRECATED] Live Streamer mode moved to mais4u
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
        template_info = None
        screen_summary = await self._get_screen_summary(room_id, user_id, text)
        reply_prompt, planner_prompt = self._resolve_live_prompts(room_id)
        if screen_summary:
            reply_prompt = self._inject_screen_summary(reply_prompt, screen_summary)
            if planner_prompt:
                planner_prompt = self._inject_screen_summary(
                    planner_prompt, screen_summary
                )
        if reply_prompt or planner_prompt:
            # [NEW] Send Screen Info to Core Key Update
            if screen_summary:
                try:
                    await self.push_screen_update(
                        room_id, user_id, user_name, timestamp, screen_summary
                    )
                except Exception as e:
                    self.logger.error(f"Failed to send screen info: {e}")

            template_items: Dict[str, str] = {}
            if reply_prompt:
                template_items["replyer_prompt"] = reply_prompt
            if planner_prompt:
                template_items["brain_planner_prompt"] = planner_prompt
                template_items["planner_prompt"] = planner_prompt
            # Check if TTS is enabled to generate a distinct template name
            # This prevents prompt caching issues when hot-switching
            template_suffix = ""
            if self.config.live_room_prompts:
                room_pts = self.config.live_room_prompts.get(room_id, {})
                if room_pts.get("tts", {}).get("enable", False):
                    template_suffix = "_tts"

            template_info = TemplateInfo(
                template_items=template_items,
                template_name=f"bilibili_live_{room_id}{template_suffix}",
                template_default=False,
            )
            self.logger.info(
                f"Using template: {template_info.template_name} (suffix='{template_suffix}')"
            )
        additional_config = {
            "room_id": room_id,
            "reply_mid": reply_mid,
            "reply_dmid": reply_dmid,
        }
        if is_mentioned:
            additional_config["is_mentioned"] = 1.0

        if is_mentioned:
            additional_config["is_mentioned"] = 1.0

        # Sanitize text to prevent spoofing
        processed_text = self._sanitize_user_text(text)

        if self.config.live_disable_network_search:
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

        self._push_to_event_queue(priority, message)

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
        self.logger.info(
            f"Gift: [{room_id}] {user_name}({user_id}) sent {gift_name} x{num} (Price: {price})"
        )

        # Aggregation Logic
        # Buffer small gifts (< 20 CNY total value) to prevent spam
        if price * num < 20:
            key = (room_id, user_id, gift_name)
            if key not in self._gift_buffer:
                self._gift_buffer[key] = {
                    "count": 0,
                    "price": price,  # Unit price
                    "timestamp": timestamp,
                    "user_name": user_name,
                }
            self._gift_buffer[key]["count"] += num
            self._gift_buffer[key]["price"] = price
            self._gift_buffer[key]["timestamp"] = timestamp  # Update to latest
            self._last_gift_time[key] = time.time()  # Update act time
            return

        # Build prompt using helper
        prompt_text = f"送出了 {gift_name} x{num}"
        template_info = self._get_template_info(room_id, user_id, prompt_text)

        # Prepare additional config for high value gifts logic if needed
        # Ensuring mention logic is consistent
        additional_config = {}
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
        # Gift segment format for S4U: "name:count"
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
        self._push_to_event_queue(20, message)

    async def _handle_mic_recognition(self, text: str) -> None:
        if not text:
            return
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return
        room_id = self.mic_worker.config.room_id
        await self.handle_mic_message(room_id, text)

        # Resume Audio Player after speech is acknowledged
        self.audio_player.resume()

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
            data.add_field("prompt", "ZH")

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
                        text = text.rstrip("。?.，,！!？")
                    return text

        except Exception as e:
            self.logger.error(f"ASR API call failed: {e}")
            return None

    async def handle_mic_message(self, room_id: int, text: str) -> None:
        additional_config = {
            "room_id": room_id,
            "is_mentioned": 2.0,
            "source": "mic_asr",
        }

        message_info = BaseMessageInfo(
            platform="bilibili.live",
            message_id=f"mic_{int(time.time() * 1000)}",
            time=time.time(),
            user_info=UserInfo(
                platform="bilibili.live",
                user_id="2146014839",
                user_nickname="主人",
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
            template_info=None,
            additional_config=additional_config,
        )

        processed_text = text
        if self.config.live_disable_network_search:
            processed_text = _mask_urls(processed_text)

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=processed_text),
            raw_message=None,
        )
        # Push to Queue (Priority 10 for Mic Command)
        self._push_to_event_queue(10, message)

    async def handle_incoming_superchat(
        self,
        room_id: int,
        message_text: str,
        price: int,
        user_id: str,
        user_name: str,
        timestamp: float,
    ) -> None:
        self.logger.info(
            f"SuperChat: [{room_id}] {user_name}({user_id}): {message_text} (Price: {price} CNY)"
        )

        # [DEPRECATED] Live Streamer mode moved to mais4u
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
        template_info = self._get_template_info(room_id, user_id, prompt_text)

        additional_config = {}
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

        # Use superchat segment so S4U recognizes it
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

        # Push to Queue (Priority 10 for SuperChat)
        self._push_to_event_queue(10, message)

    async def handle_incoming_guard(
        self,
        room_id: int,
        guard_name: str,
        num: int,
        user_id: str,
        user_name: str,
        timestamp: float,
        guard_level: int = 3,
    ) -> None:
        self.logger.info(
            f"Guard: [{room_id}] {user_name}({user_id}) became {guard_name} (Level: {guard_level}) - PATCHED_VERIFIED"
        )

        # [DEPRECATED] Live Streamer mode moved to mais4u
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
        template_info = self._get_template_info(room_id, user_id, prompt_text)

        additional_config = {}
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
                },
                ensure_ascii=True,
            ),
        )

        # Push to Queue (Priority 10 for Guard)
        self._push_to_event_queue(10, message)

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

        tts_enable = False
        tts_config = {}
        if room_prompts:
            tts_config = room_prompts.get("tts", {})
            tts_enable = bool(tts_config.get("enable", False))

        if tts_enable:
            # Check for TTS-specific prompts in config
            tts_reply = str(tts_config.get("reply_prompt", "") or "")
            tts_planner = str(tts_config.get("planner_prompt", "") or "")

            # If specified, override the prompts
            if tts_reply:
                reply_prompt = tts_reply
            if tts_planner:
                planner_prompt = tts_planner

            # Fallback: append generic instructions if no specific reply prompt was found
            # (Or if the custom one is missing the required XML instructions)
            if "<JP>" not in reply_prompt and "<ZH>" not in reply_prompt:
                tts_instruction = (
                    "\n\n非常重要：请必须同时输出中文回复和对应的日文翻译（用于语音播放），格式严格如下：\n"
                    "<JP>日文翻译内容</JP><ZH>中文原本意思</ZH>\n"
                    "例如：\n"
                    "<JP>こんにちは、ご飯を食べましたか？</JP><ZH>你好呀，吃过饭了吗？</ZH>\n"
                    "只输出上述XML格式，不要输出其他多余内容(包括前后缀，冒号和引号，括号，表情包等)。"
                )
                self.logger.debug(
                    f"Appending TTS instruction to prompt for room {room_id}"
                )
                reply_prompt += tts_instruction
        else:
            # Force disable XML if TTS is off (Circuit Breaker for Context Pollution)
            anti_tts_instruction = (
                "\n禁止使用<JP><ZH>标签，不要进行日语翻译，只输出中文。"
            )
            reply_prompt += anti_tts_instruction

        self.logger.debug(
            f"Resolved prompts for room {room_id}: tts_enable={tts_enable}"
        )
        return reply_prompt, planner_prompt

    # ========== Comment Notice Loop ==========

    async def _comment_notice_loop(self) -> None:
        while True:
            reply_items: List[Dict[str, Any]] = []
            at_items: List[Dict[str, Any]] = []
            try:
                reply_items = await self.api.get_reply_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.warning(f"Reply notice fetch error: {exc}")
                self.logger.debug(f"Reply notice fetch error: {exc}")
            try:
                at_items = await self.api.get_at_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.debug(f"At notice fetch error: {exc}")
            if reply_items or at_items:
                self.logger.info(
                    "Comment notices: reply=%s at=%s",
                    len(reply_items),
                    len(at_items),
                )
            else:
                self.logger.info("Comment notices: 0")
            if not self._comment_bootstrap_done:
                for item in reply_items:
                    self._track_notice_key(self._notice_key("reply", item))
                for item in at_items:
                    self._track_notice_key(self._notice_key("at", item))
                self._comment_bootstrap_done = True
            else:
                await self._handle_reply_notifications(reply_items, source="reply")
                await self._handle_reply_notifications(at_items, source="at")
            await asyncio.sleep(self.config.comment_poll_interval)

    def _notice_key(self, source: str, item: Dict[str, Any]) -> str:
        notify_id = str(item.get("id") or "")
        if not notify_id:
            return ""
        return f"{source}:{notify_id}"

    def _track_notice_key(self, notify_key: str) -> bool:
        if not notify_key or notify_key in self._reply_seen_set:
            return False
        self._reply_seen_set.add(notify_key)
        self._reply_seen.append(notify_key)
        if len(self._reply_seen) > 500:
            old = self._reply_seen.pop(0)
            self._reply_seen_set.discard(old)
        return True

    def _is_at_me(self, at_details: Any) -> bool:
        if not self.config.dede_user_id:
            return False
        if not isinstance(at_details, list):
            return False
        for detail in at_details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("mid") or "") == self.config.dede_user_id:
                return True
        return False

    @staticmethod
    def _build_comment_reply_target_from_item(
        comment_type: Any,
        comment_oid: Any,
        reply_item: Dict[str, Any],
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        try:
            comment_type_int = int(comment_type)
            comment_oid_int = int(comment_oid)
        except (TypeError, ValueError):
            return None

        root = None
        parent = None
        for value in (
            reply_item.get("root_id"),
            reply_item.get("source_id"),
            reply_item.get("target_id"),
        ):
            if value not in (None, "", 0):
                try:
                    root = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        for value in (
            reply_item.get("source_id"),
            reply_item.get("target_id"),
            reply_item.get("root_id"),
        ):
            if value not in (None, "", 0):
                try:
                    parent = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        return comment_type_int, comment_oid_int, root, parent

    async def _handle_reply_notifications(
        self, items: List[Dict[str, Any]], source: str
    ) -> None:
        if not items:
            return
        for item in items:
            notify_id = str(item.get("id") or "")
            if not notify_id:
                continue
            notify_key = self._notice_key(source, item)
            if not self._track_notice_key(notify_key):
                continue

            user = item.get("user") or {}
            reply_item = item.get("item") or {}
            user_id = str(user.get("mid") or "")
            user_name = str(user.get("nickname") or user_id)
            if (
                self.config.comment_resolve_user_nickname
                and user_id
                and (not user_name or user_name == user_id)
            ):
                user_name = await self._resolve_user_nickname(user_id)
            business_id = reply_item.get("business_id")
            subject_id = reply_item.get("subject_id")
            content = (
                reply_item.get("source_content")
                or reply_item.get("target_reply_content")
                or reply_item.get("root_reply_content")
                or reply_item.get("title")
                or ""
            )
            at_details = reply_item.get("at_details") or []
            is_at_me = self._is_at_me(at_details)
            if source == "at" and self.config.dede_user_id and not is_at_me:
                self.logger.debug("At notice without bot mention: id=%s", notify_id)
            group_id = f"comment:{business_id}:{subject_id}"
            self._remember_comment_context(
                group_id=group_id,
                comment_type=business_id,
                comment_oid=subject_id,
                root_id=reply_item.get("root_id"),
                source_id=reply_item.get("source_id"),
                target_id=reply_item.get("target_id"),
            )
            state_key = (group_id, user_id)
            state = self._comment_reply_state.get(
                state_key, {"count": 0, "silenced": False, "fallback_sent": False}
            )
            if state.get("silenced"):
                continue
            if state.get("count", 0) >= COMMENT_REPLY_LIMIT:
                if not state.get("fallback_sent"):
                    target = self._build_comment_reply_target_from_item(
                        business_id, subject_id, reply_item
                    )
                    if target:
                        await self._send_comment_reply_from_context(
                            target, COMMENT_LIMIT_FALLBACK_TEXT
                        )
                    else:
                        self.logger.warning(
                            "Comment fallback reply skipped: invalid target group_id=%s user_id=%s",
                            group_id,
                            user_id,
                        )
                    state["fallback_sent"] = True
                state["silenced"] = True
                self._comment_reply_state[state_key] = state
                continue
            now_ts = time.time()
            reply_time = float(item.get("reply_time") or now_ts)
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=notify_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=user_id,
                    user_nickname=user_name,
                ),
                group_info=GroupInfo(
                    platform=self.config.platform,
                    group_id=group_id,
                    group_name=group_id,
                ),
                format_info=FormatInfo(
                    content_format=["text"],
                    accept_format=ACCEPT_FORMAT,
                ),
                additional_config=self._build_comment_notice_config(
                    business_id=business_id,
                    subject_id=subject_id,
                    reply_item=reply_item,
                    source=source,
                    is_at_me=is_at_me,
                    reply_time=reply_time,
                ),
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=Seg(type="text", data=str(content)),
                raw_message=json.dumps(item, ensure_ascii=True),
            )
            await self._send_to_nachobot(message)
            state["count"] = int(state.get("count", 0)) + 1
            self._comment_reply_state[state_key] = state

    def _build_comment_notice_config(
        self,
        business_id: Any,
        subject_id: Any,
        reply_item: Dict[str, Any],
        source: str,
        is_at_me: bool,
        reply_time: float,
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "comment_type": business_id,
            "comment_oid": subject_id,
            "root_id": reply_item.get("root_id"),
            "source_id": reply_item.get("source_id"),
            "target_id": reply_item.get("target_id"),
            "uri": reply_item.get("uri"),
            "notice_source": source,
            "reply_time": reply_time,
        }
        if (
            self.config.comment_force_mention
            or source == "reply"
            or source == "at"
            or is_at_me
        ):
            config["is_mentioned"] = 1.0
        return config

    # ========== Handle From NachoBot ==========

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        message = MessageBase.from_dict(raw_message_base_dict)
        self.logger.info(
            "Incoming from NachoBot: platform=%s group_id=%s user_id=%s",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            getattr(message.message_info.user_info, "user_id", None),
        )
        seg = message.message_segment
        if seg.type == "command":
            await self._handle_command(message)
            return

        image_data = _extract_image_base64(seg)
        if image_data:
            private_target = self._resolve_private_target(message)
            if private_target:
                await self._send_private_image(private_target, image_data)
                text = _extract_plain_text(seg).strip()
                if text:
                    await self._send_private_message(private_target, text)
            else:
                self.logger.warning("Image message unsupported for non-private target")
            return

        text = _extract_plain_text(seg).strip()
        if not text:
            return
        comment_target = self._resolve_comment_target(message)
        if comment_target:
            await self._send_comment_reply_from_context(comment_target, text)
            return
        room_id = self._resolve_room_id(message)
        if room_id is not None:
            # Release lock to allow next event to be processed
            if not self._core_ready_event.is_set():
                self.logger.info("Core reply received, releasing event lock.")
                self._core_ready_event.set()

            reply_dmid = _find_reply_id(seg)
            reply_mid = ""
            if reply_dmid:
                reply_mid = self._lookup_reply_mid(room_id, reply_dmid)
            await self._handle_live_reply(
                {
                    "message": text,
                    "room_id": room_id,
                    "reply_mid": reply_mid or "",
                    "reply_dmid": reply_dmid or "",
                }
            )
            return
        private_target = self._resolve_private_target(message)
        if private_target:
            await self._send_private_message(private_target, text)
            return
        self.logger.warning("Missing room_id for outgoing danmu")

    def _resolve_room_id(self, message: MessageBase) -> Optional[int]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            try:
                return int(group_info.group_id)
            except ValueError:
                return None
        additional = message.message_info.additional_config or {}
        room_id = additional.get("room_id")
        if room_id is None:
            return None
        try:
            return int(room_id)
        except ValueError:
            return None

    def _lookup_reply_mid(self, room_id: int, reply_dmid: str) -> str:
        cache = self._danmu_cache.get(room_id) or {}
        return str(cache.get(reply_dmid) or "")

    # ========== Sending Danmu ==========

    async def _send_danmu(
        self,
        room_id: int,
        text: str,
        reply_mid: Optional[str],
        reply_dmid: Optional[str],
    ) -> None:
        text = self._filter_outgoing_text(text)

        # Check if TTS is enabled for this room
        max_len = BILIBILI_DANMU_MAX_LENGTH
        room_prompts = self.config.live_room_prompts.get(room_id, {})
        tts_config = room_prompts.get("tts", {})
        if tts_config.get("enable", False):
            max_len = 9999

        segments = _split_bilibili_text(text, max_length=max_len)
        if not segments:
            self.logger.warning("Empty danmu after splitting")
            return
        self.logger.info(
            "Send danmu: room_id=%s reply_mid=%s reply_dmid=%s text=%s",
            room_id,
            reply_mid or "",
            reply_dmid or "",
            text,
        )
        for idx, segment in enumerate(segments):
            segment_reply_mid = reply_mid if idx == 0 else None
            segment_reply_dmid = reply_dmid if idx == 0 else None
            try:
                resp = await self.api.send_danmu(
                    room_id=room_id,
                    message=segment,
                    reply_mid=segment_reply_mid or None,
                    reply_dmid=segment_reply_dmid or None,
                )
                if (resp or {}).get("code") != 0:
                    self.logger.warning(f"Danmu send failed: {resp}")
                else:
                    dmid = None
                    data = (resp or {}).get("data", {})
                    if isinstance(data, dict):
                        dmid = data.get("dmid") or data.get("dmid_str")
                    self._remember_self_danmu(
                        room_id, str(dmid) if dmid else "", segment
                    )
                    self.logger.info("Danmu send ok")
            except Exception as exc:
                self.logger.error(f"Danmu send error: {exc}")
            if idx < len(segments) - 1:
                await asyncio.sleep(BILIBILI_DANMU_SEND_DELAY_SECONDS)

    def _remember_self_danmu(self, room_id: int, message_id: str, text: str) -> None:
        now = time.time()
        if message_id:
            room_cache = self._self_danmu_ids.setdefault(room_id, {})
            room_cache[message_id] = now
            if len(room_cache) > 500:
                for msg_id, ts in list(room_cache.items()):
                    if now - ts > 30:
                        room_cache.pop(msg_id, None)
        room_texts = self._self_danmu_texts.setdefault(room_id, [])
        if text:
            room_texts.append((text, now))
        if len(room_texts) > 200:
            self._self_danmu_texts[room_id] = [
                item for item in room_texts if now - item[1] <= 30
            ]

    def is_self_danmu(
        self,
        room_id: int,
        user_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        if (
            (not self.config.live_allow_self_danmu)
            and self.config.dede_user_id
            and user_id
        ):
            if str(user_id) == str(self.config.dede_user_id):
                return True
        now = time.time()
        room_cache = self._self_danmu_ids.get(room_id, {})
        if message_id and message_id in room_cache:
            return True
        room_texts = self._self_danmu_texts.get(room_id, [])
        if text:
            window = 2.5 if len(text.strip()) <= 2 else 6.0
            for sent_text, ts in list(room_texts):
                if now - ts > 30:
                    continue
                if sent_text == text and (now - ts) <= window:
                    return True
        return False

    async def _wait_and_process_tts(self, room_id: int, delay: float = 0.5) -> None:
        """Helper to wait and trigger processing."""
        try:
            await asyncio.sleep(delay)
            await self._process_buffered_live_reply(room_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"TTS timer error: {e}")

    async def _process_buffered_live_reply(self, room_id: int) -> None:
        """Process buffered messages for a room after delay."""
        try:
            buffer = self._tts_buffer.get(room_id)
            if not buffer:
                return

            # Combine text parts
            # Use empty string join based on verification for bilingual text
            full_text = "".join(buffer)

            # Smart Buffering: Check for unbalanced tags
            open_zh = full_text.count("<ZH>")
            close_zh = full_text.count("</ZH>")
            open_jp = full_text.count("<JP>")
            close_jp = full_text.count("</JP>")

            is_balanced = (open_zh == close_zh) and (open_jp == close_jp)

            # Debug log for smart buffering
            self.logger.info(
                f"SmartBuffering Check: balanced={is_balanced} (ZH:{open_zh}/{close_zh} JP:{open_jp}/{close_jp}) "
                f"len={len(full_text)} content={repr(full_text[:100])}..."
            )

            metadata = self._tts_metadata.get(room_id, {})
            start_time = metadata.get("start_time", 0)
            elapsed = time.time() - start_time

            # If unbalanced data and we haven't waited too long (e.g., 8s), extend wait
            if not is_balanced and elapsed < 8.0:
                self.logger.info(
                    f"Buffered TTS text unbalanced (ZH:{open_zh}/{close_zh} JP:{open_jp}/{close_jp}), extending wait... (elapsed={elapsed:.1f}s)"
                )
                self._tts_timer[room_id] = asyncio.create_task(
                    self._wait_and_process_tts(room_id, delay=1.0)
                )
                return

            # Proceed to flush
            self._tts_buffer[room_id] = []

            # Clear timer reference
            self._tts_timer.pop(room_id, None)

            # Clear metadata
            self._tts_metadata.pop(room_id, None)

            reply_mid = metadata.get("reply_mid")
            reply_dmid = metadata.get("reply_dmid")

            self.logger.info(
                f"Processing buffered TTS reply for room {room_id}: {full_text[:50]}..."
            )

            room_config = self.config.live_room_prompts.get(room_id, {})
            tts_config = room_config.get("tts", {})

            if self.tts_model:
                text_jp, text_zh = self._parse_bilingual_response(full_text)
                display_text = text_zh if text_zh else full_text
                tts_text = text_jp if text_jp else ""

                subtitle_path = str(tts_config.get("subtitle_path") or "subtitles.txt")
                self._update_subtitle(display_text, subtitle_path=subtitle_path)

                if tts_text:
                    cleaned_tts_text = _clean_text_for_tts(tts_text)
                    self.logger.info(
                        f"TTS Generating for room {room_id}: {cleaned_tts_text}"
                    )
                    try:
                        audio_data = await self.tts_model.tts(
                            text=cleaned_tts_text, platform=self.config.platform
                        )
                        await self._play_audio(audio_data)
                        self.logger.info(f"TTS Played successfully for room {room_id}")
                        return
                    except Exception as e:
                        self.logger.error(f"TTS generation failed: {e}")
                        self.logger.info("Fallback to sending danmu due to TTS error")
                else:
                    self.logger.warning(
                        f"TTS enabled for room {room_id} but no Japanese text parsed. Sending raw text as danmu."
                    )
                    msg_to_send = text_zh if text_zh else full_text
                    await self._send_danmu(
                        room_id, msg_to_send, reply_mid or None, reply_dmid or None
                    )
                    return

            await self._send_danmu(
                room_id, full_text, reply_mid or None, reply_dmid or None
            )

        except Exception as e:
            self.logger.error(f"Error processing buffered TTS reply: {e}")
            self._tts_buffer.pop(room_id, None)
            self._tts_metadata.pop(room_id, None)

    # ========== Command Handlers ==========

    async def _handle_command(self, message: MessageBase) -> None:
        seg = message.message_segment
        command_data = seg.data if isinstance(seg.data, dict) else {}
        command_name = str(command_data.get("name") or "")
        args = command_data.get("args") or {}
        if command_name == "BILI_COMMENT_REPLY":
            await self._handle_comment_reply(args)
            return
        if command_name == "BILI_LIVE_REPLY":
            await self._handle_live_reply(args)
            return
        if command_name == "BILI_PRIVATE_SEND":
            await self._handle_private_send(args, message)
            return
        self.logger.warning(f"Unknown command: {command_name}")

    async def _handle_comment_reply(self, args: Dict[str, Any]) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        if not text:
            self.logger.warning("Empty comment reply text")
            return
        text = self._filter_outgoing_text(text)
        try:
            comment_type = int(args.get("type"))
            oid = int(args.get("oid"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid comment reply args")
            return
        root = args.get("root")
        parent = args.get("parent")
        root_id = int(root) if root not in (None, "", 0) else None
        parent_id = int(parent) if parent not in (None, "", 0) else None
        self.logger.info(
            "Send comment reply: type=%s oid=%s root=%s parent=%s",
            comment_type,
            oid,
            root_id,
            parent_id,
        )
        try:
            resp = await self.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                self.logger.info("Comment reply ok")
        except Exception as exc:
            self.logger.error(f"Comment reply error: {exc}")

    async def _handle_live_reply(self, args: Dict[str, Any]) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        text = self._filter_outgoing_text(text)
        if not text:
            return

        if len(text) <= 2 and all("\u4e00" <= c <= "\u9fff" for c in text):
            self.logger.debug(f"Skipping typo correction message: {text}")
            return
        try:
            room_id = int(args.get("room_id"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid room_id for live reply")
            return
        reply_mid = str(args.get("reply_mid") or "")
        reply_dmid = str(args.get("reply_dmid") or "")

        room_config = self.config.live_room_prompts.get(room_id, {})
        tts_config = room_config.get("tts", {})
        tts_enable = bool(tts_config.get("enable", False))

        self.logger.info(
            f"TTS Debug: room_id={room_id}, tts_enable={tts_enable}, tts_model={self.tts_model is not None}"
        )

        if tts_enable:
            # Buffer the text
            buffer = self._tts_buffer.setdefault(room_id, [])
            buffer.append(text)

            # Save metadata if this is the start of a buffer
            if room_id not in self._tts_metadata:
                self._tts_metadata[room_id] = {
                    "reply_mid": reply_mid,
                    "reply_dmid": reply_dmid,
                    "start_time": time.time(),
                }

            # Reset timer
            if room_id in self._tts_timer:
                self._tts_timer[room_id].cancel()

            # Start new timer (0.5s)
            self._tts_timer[room_id] = asyncio.create_task(
                self._wait_and_process_tts(room_id)
            )
            return

        # Original non-TTS logic below
        # (Actually, we can just execute the immediate logic here if not TTS)
        await self._send_danmu(room_id, text, reply_mid or None, reply_dmid or None)

    async def _handle_private_send(
        self, args: Dict[str, Any], message: Optional[MessageBase]
    ) -> None:
        text = _strip_emoji(str(args.get("message") or "")).strip()
        if not text:
            return
        talker_id = args.get("talker_id")
        session_type = args.get("session_type")
        target = None
        if talker_id not in (None, "", 0):
            try:
                target = PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                target = None
        if target is None and message is not None:
            target = self._resolve_private_target(message)
        if target is None:
            self.logger.warning("Missing private message target")
            return
        await self._send_private_message(target, text)

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
        # 直播消息使用 bilibili.live 平台，走 S4U 系统
        live_platform = "bilibili.live"
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

    async def _private_message_loop(self) -> None:
        while True:
            try:
                await self._poll_private_messages()
            except Exception as exc:
                self.logger.warning(f"Private message loop error: {exc}")
            await asyncio.sleep(self.config.private_poll_interval)

    async def _get_private_sessions(self) -> List[PrivateSessionConfig]:
        sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {
            (item.session_type, item.talker_id): item
            for item in self.config.private_sessions
        }
        if not self.config.private_auto_sessions:
            return list(sessions.values())

        now = time.time()
        refresh_seconds = max(5, self.config.private_auto_session_refresh_seconds)
        if (
            not self._auto_private_sessions
            or (now - self._auto_private_sessions_ts) >= refresh_seconds
        ):
            auto_sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {}
            for session_type in self.config.private_auto_session_types:
                resp = await self.api.get_sessions(
                    session_type=session_type,
                    size=self.config.private_auto_session_size,
                )
                if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                    self.logger.warning(
                        "Session list failed: session_type=%s code=%s message=%s",
                        session_type,
                        resp.get("code"),
                        resp.get("message") or resp.get("msg"),
                    )
                    continue
                data = (resp or {}).get("data", {})
                session_list = data.get("session_list") or []
                if not isinstance(session_list, list):
                    continue
                for item in session_list:
                    if not isinstance(item, dict):
                        continue
                    try:
                        talker_id = int(item.get("talker_id") or 0)
                        item_type = int(item.get("session_type") or session_type)
                    except (TypeError, ValueError):
                        continue
                    if item_type not in (1, 2):
                        continue
                    if not talker_id:
                        continue
                    auto_sessions[(item_type, talker_id)] = PrivateSessionConfig(
                        talker_id=talker_id,
                        session_type=item_type,
                    )
            self._auto_private_sessions = list(auto_sessions.values())
            self._auto_private_sessions_ts = now

        for item in self._auto_private_sessions:
            sessions.setdefault((item.session_type, item.talker_id), item)
        return list(sessions.values())

    async def _poll_private_messages(self) -> None:
        sessions = await self._get_private_sessions()
        if not sessions:
            self.logger.debug("Private polling: no sessions")
            return
        self.logger.debug("Private polling: %s sessions", len(sessions))
        for session in sessions:
            key = (session.session_type, session.talker_id)
            last_seqno = self._dm_last_seqno.get(key)
            resp = await self.api.fetch_session_msgs(
                talker_id=session.talker_id,
                session_type=session.session_type,
                size=20,
                begin_seqno=last_seqno,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(
                    "Private poll failed: talker_id=%s session_type=%s code=%s message=%s",
                    session.talker_id,
                    session.session_type,
                    resp.get("code"),
                    resp.get("message") or resp.get("msg"),
                )
            data = (resp or {}).get("data", {})
            messages = data.get("messages") or []
            max_seqno = data.get("max_seqno")
            if max_seqno is None:
                continue
            if last_seqno is None:
                self._dm_last_seqno[key] = int(max_seqno)
                continue
            if not messages:
                if int(max_seqno) > last_seqno:
                    self._dm_last_seqno[key] = int(max_seqno)
                continue
            await self._emit_private_messages(session=session, messages=messages)
            self._dm_last_seqno[key] = int(max_seqno)

    async def _emit_private_messages(
        self,
        session: PrivateSessionConfig,
        messages: Iterable[Dict[str, Any]],
    ) -> None:
        messages_list = list(messages)
        if messages_list:
            self.logger.info(
                "Private messages: talker_id=%s session_type=%s count=%s",
                session.talker_id,
                session.session_type,
                len(messages_list),
            )
        for msg in reversed(messages_list):
            sender_uid = str(msg.get("sender_uid") or "")
            if (
                sender_uid
                and self.config.dede_user_id
                and sender_uid == str(self.config.dede_user_id)
            ):
                continue
            msg_type = int(msg.get("msg_type") or 0)
            content = msg.get("content")
            content_text = ""
            segment: Optional[Seg] = None
            content_format = ["text"]
            image_url = ""
            if msg_type in (2, 6):
                image_url = self._extract_private_image_url(content)
                if image_url:
                    image_base64 = await self._download_private_image(image_url)
                    if image_base64:
                        segment = Seg(type="image", data=image_base64)
                        content_format = ["image"]
                if segment is None:
                    content_text = (
                        self._parse_private_content(msg_type, content) or "[image]"
                    )
            else:
                content_text = self._parse_private_content(msg_type, content)

            if segment is None:
                if not content_text:
                    continue
                segment = Seg(type="text", data=content_text)
            message_id = str(
                msg.get("msg_key") or msg.get("msg_seqno") or uuid.uuid4().hex
            )
            now_ts = time.time()
            msg_time = float(msg.get("timestamp") or now_ts)
            group_id = f"dm:{session.session_type}:{session.talker_id}"
            self._remember_private_session(group_id, session)
            sender_name = await self._resolve_user_nickname(
                sender_uid or str(session.talker_id)
            )
            additional_config = {
                "session_type": session.session_type,
                "talker_id": session.talker_id,
                "msg_type": msg_type,
                "msg_seqno": msg.get("msg_seqno"),
                "message_time": msg_time,
            }
            if image_url:
                additional_config["image_url"] = image_url
            if self.config.private_force_mention:
                additional_config["is_mentioned"] = 1.0
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=message_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=sender_uid or str(session.talker_id),
                    user_nickname=sender_name,
                ),
                group_info=None,
                format_info=FormatInfo(
                    content_format=content_format,
                    accept_format=ACCEPT_FORMAT_PRIVATE,
                ),
                additional_config=additional_config,
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=segment,
                raw_message=json.dumps(msg, ensure_ascii=False),
            )
            await self._send_to_nachobot(message)

    def _remember_private_session(
        self, group_id: str, session: PrivateSessionConfig
    ) -> None:
        self._private_session_by_group[group_id] = session
        self._last_private_session = session

    def _remember_comment_context(
        self,
        group_id: str,
        comment_type: Optional[int],
        comment_oid: Optional[int],
        root_id: Any,
        source_id: Any,
        target_id: Any,
    ) -> None:
        if not group_id:
            return
        self._comment_context[group_id] = {
            "comment_type": comment_type,
            "comment_oid": comment_oid,
            "root_id": root_id,
            "source_id": source_id,
            "target_id": target_id,
            "ts": time.time(),
        }

    @staticmethod
    def _parse_comment_group_id(group_id: str) -> Optional[Tuple[int, int]]:
        if not group_id or not group_id.startswith("comment:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def _resolve_comment_target(
        self, message: MessageBase
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        group_info = message.message_info.group_info
        group_id = group_info.group_id if group_info else None
        if not group_id:
            return None
        parsed = self._parse_comment_group_id(group_id)
        if not parsed:
            return None
        comment_type, comment_oid = parsed
        context = self._comment_context.get(group_id, {})
        root_id = context.get("root_id")
        source_id = context.get("source_id")
        target_id = context.get("target_id")
        root = None
        parent = None
        for value in (root_id, source_id, target_id):
            if value not in (None, "", 0):
                try:
                    root = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        for value in (source_id, target_id, root_id):
            if value not in (None, "", 0):
                try:
                    parent = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        return comment_type, comment_oid, root, parent

    async def _send_comment_reply_from_context(
        self,
        target: Tuple[int, int, Optional[int], Optional[int]],
        text: str,
    ) -> None:
        text = self._filter_outgoing_text(text)
        comment_type, oid, root_id, parent_id = target
        self.logger.info(
            "Send comment reply: type=%s oid=%s root=%s parent=%s",
            comment_type,
            oid,
            root_id,
            parent_id,
        )
        try:
            resp = await self.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                self.logger.info("Comment reply ok")
        except Exception as exc:
            self.logger.error(f"Comment reply error: {exc}")

    async def _resolve_user_nickname(self, user_id: str) -> str:
        if not user_id:
            return ""
        cached = self._user_name_cache.get(user_id)
        now = time.time()
        if cached and (now - cached[1]) < self._user_name_cache_seconds:
            return cached[0]
        try:
            mid = int(user_id)
        except ValueError:
            return user_id
        try:
            resp = await self.api.get_user_info(mid)
        except Exception as exc:
            self.logger.warning(f"User info fetch failed: mid={mid} error={exc}")
            return user_id
        name = (resp or {}).get("data", {}).get("name") or user_id
        self._user_name_cache[user_id] = (str(name), now)
        return str(name)

    def _resolve_private_target(
        self, message: MessageBase
    ) -> Optional[PrivateSessionConfig]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            parsed = self._parse_private_group_id(group_info.group_id)
            if parsed:
                return parsed
        additional = message.message_info.additional_config or {}
        talker_id = additional.get("talker_id")
        session_type = additional.get("session_type")
        if talker_id not in (None, "", 0):
            try:
                return PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        user_info = message.message_info.user_info
        if user_info and user_info.user_id:
            try:
                return PrivateSessionConfig(
                    talker_id=int(user_info.user_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        return self._last_private_session

    @staticmethod
    def _parse_private_group_id(group_id: str) -> Optional[PrivateSessionConfig]:
        if not group_id or not group_id.startswith("dm:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            session_type = int(parts[1])
            talker_id = int(parts[2])
        except ValueError:
            return None
        return PrivateSessionConfig(talker_id=talker_id, session_type=session_type)

    @staticmethod
    def _parse_private_content(msg_type: int, content: Any) -> str:
        if msg_type == 1 and content:
            text = ""
            if isinstance(content, dict):
                text = str(
                    content.get("content")
                    or content.get("text")
                    or content.get("title")
                    or ""
                )
            elif isinstance(content, str):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        text = str(
                            data.get("content")
                            or data.get("text")
                            or data.get("title")
                            or ""
                        )
                    elif isinstance(data, str):
                        text = data
                except Exception:
                    text = content
            text = _normalize_text(text)
            return _strip_emoji(text).strip()
        if msg_type in (2, 6):
            return "[image]"
        return ""

    async def _send_private_message(
        self, session: PrivateSessionConfig, text: str
    ) -> None:
        self.logger.info(
            "Send private message: talker_id=%s session_type=%s text=%s",
            session.talker_id,
            session.session_type,
            text,
        )
        try:
            safe_text = _normalize_text(text)
            safe_text = self._filter_outgoing_text(safe_text)
            if not safe_text:
                return
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await self.api.send_private_message(
                        talker_id=session.talker_id,
                        session_type=session.session_type,
                        message=safe_text,
                    )
                except Exception as exc:
                    if attempt < max_attempts:
                        self.logger.warning(
                            "Private message send failed (attempt %s/%s): %s",
                            attempt,
                            max_attempts,
                            exc,
                        )
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    raise
                if (resp or {}).get("code") != 0:
                    self.logger.warning(f"Private message failed: {resp}")
                    return
                self.logger.info("Private message ok")
                return
        except Exception as exc:
            self.logger.error(f"Private message error: {exc}")

    async def _send_private_image(
        self, session: PrivateSessionConfig, image_base64: str
    ) -> None:
        image_bytes, image_format = _decode_image_base64(image_base64)
        if not image_bytes:
            self.logger.warning("Private image send failed: invalid image data")
            return
        try:
            upload_resp = await self.api.upload_dynamic_image(
                image_bytes=image_bytes,
                image_format=image_format,
                category="daily",
            )
        except Exception as exc:
            self.logger.error("Private image upload error: %s", exc)
            return
        data = (upload_resp or {}).get("data", {})
        image_url = str(data.get("image_url") or "")
        if not image_url:
            self.logger.warning("Private image upload missing url: %s", upload_resp)
            return
        content: Dict[str, Any] = {"url": image_url}
        if data.get("image_height"):
            content["height"] = data.get("image_height")
        if data.get("image_width"):
            content["width"] = data.get("image_width")
        if data.get("img_size"):
            content["size"] = data.get("img_size")
        if image_format:
            content["imageType"] = image_format
        self.logger.info(
            "Send private image: talker_id=%s session_type=%s url=%s",
            session.talker_id,
            session.session_type,
            image_url,
        )
        try:
            resp = await self.api.send_private_image_message(
                talker_id=session.talker_id,
                session_type=session.session_type,
                content=content,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(f"Private image failed: {resp}")
            else:
                self.logger.info("Private image ok")
        except Exception as exc:
            self.logger.error(f"Private image error: {exc}")

    # ========== Event Serialization & Gift Aggregation ==========

    def _push_to_event_queue(self, priority: int, message: MessageBase) -> None:
        """
        Push message to event queue with priority.
        Priority: 10 (High) -> 40 (Low)
        """
        try:
            timestamp = time.time()
            # Queue item: (priority, timestamp, seq, message)
            # Tuple comparison sorts by priority asc, then timestamp asc, then seq (unique int)
            count = next(self._seq_counter)
            self._event_queue.put_nowait((priority, timestamp, count, message))
        except Exception as e:
            self.logger.error(f"Failed to push to event queue: {e}")

    async def _process_event_queue(self) -> None:
        """
        Background loop to consume events and send to Core sequentially.
        """
        self.logger.info("Event serialization queue started.")
        while True:
            try:
                # Wait for Core to be ready (Lock released)
                await self._core_ready_event.wait()

                # Get next highest priority event
                priority, timestamp, count, message = await self._event_queue.get()

                # Double check ready state (though wait() handles it)
                if not self._core_ready_event.is_set():
                    # Should not happen usually
                    self._event_queue.put_nowait((priority, timestamp, count, message))
                    await asyncio.sleep(0.1)
                    continue

                # Lock the core processing
                self._core_ready_event.clear()
                self._event_lock_timestamp = time.time()

                # Send to Core
                try:
                    await self._send_to_nachobot(message)

                    # Start watchdog to force release if Core hangs/doesn't reply
                    asyncio.create_task(self._event_lock_watchdog())
                except Exception as e:
                    self.logger.error(f"Failed to send event to core: {e}")
                    # If send failed, release lock immediately so we don't hang
                    self._core_ready_event.set()

                # Yield slightly to allow other tasks
                await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Event processing loop error: {e}")
                await asyncio.sleep(1)

    async def _event_lock_watchdog(self) -> None:
        """
        Force release core lock if no reply received within timeout.
        """
        timeout = 20.0  # seconds
        lock_ts = self._event_lock_timestamp
        await asyncio.sleep(timeout)

        # Check if lock is still held and timestamp matches (meaning same lock session)
        if (
            not self._core_ready_event.is_set()
            and self._event_lock_timestamp == lock_ts
        ):
            self.logger.warning(
                f"Core processing timed out ({timeout}s), forcing lock release."
            )
            self._core_ready_event.set()

    async def _gift_flush_loop(self) -> None:
        """
        Check gift buffer for idle streams and flush them.
        """
        self.logger.info("Gift aggregation loop started.")
        debounce_seconds = 2.0
        while True:
            try:
                await asyncio.sleep(1.0)
                now = time.time()
                keys_to_flush = []

                # Identify keys ready to flush
                for key, last_ts in list(self._last_gift_time.items()):
                    if now - last_ts >= debounce_seconds:
                        keys_to_flush.append(key)

                # Flush them
                for key in keys_to_flush:
                    if key in self._gift_buffer:
                        data = self._gift_buffer.pop(key)
                        del self._last_gift_time[key]

                        count = data["count"]
                        gift_name = key[2]
                        room_id = key[0]
                        user_id = key[1]
                        user_name = data["user_name"]
                        timestamp = data["timestamp"]
                        price = data["price"] * count  # Total price

                        # Build aggregated message logic
                        # We need to construct the message here.
                        # Ideally, reusing handle_incoming_gift logic but bypassing buffering.
                        # Or better, construct MessageBase here.

                        self.logger.info(
                            f"Flushing aggregated gift: {gift_name} x{count} from {user_name}"
                        )

                        prompt_text = f"送出了 {gift_name} x{count}"
                        template_info = self._get_template_info(
                            room_id, user_id, prompt_text
                        )

                        additional_config = {}
                        additional_config["is_mentioned"] = 1.0

                        message_info = BaseMessageInfo(
                            platform=self.config.platform,
                            message_id=str(uuid.uuid4()),
                            time=timestamp,
                            user_info=UserInfo(
                                platform=self.config.platform,
                                user_id=user_id,
                                user_nickname=user_name,
                            ),
                            group_info=GroupInfo(
                                platform=self.config.platform,
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

                        # Segments
                        # Adjust gift segment to reflect total count
                        gift_segment = Seg(type="gift", data=f"{gift_name}:{count}")
                        text_segment = Seg(type="text", data=prompt_text)

                        message = MessageBase(
                            message_info=message_info,
                            message_segment=Seg(
                                type="seglist", data=[gift_segment, text_segment]
                            ),
                            raw_message=json.dumps(
                                {
                                    "type": "gift",
                                    "gift_name": gift_name,
                                    "num": count,
                                    "price": price,
                                    "room_id": room_id,
                                },
                                ensure_ascii=True,
                            ),
                        )

                        # Push to Queue (Priority 30 for Gifts)
                        self._push_to_event_queue(30, message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Gift flush loop error: {e}")
                await asyncio.sleep(1)

    def _parse_bilingual_response(self, text: str) -> tuple[str, str]:
        """
        Parse bilingual text with tags.
        Input format examples:
        - <JP>JP</JP><ZH>ZH</ZH>
        - <ZH>CN</ZH><JP>JP</JP>
        - RawText (treated as ZH)
        """
        text_jp = []
        text_zh = []

        # Extract JP parts
        jp_parts = re.findall(r"<JP>(.*?)</JP>", text, re.DOTALL)
        if jp_parts:
            text_jp.extend(jp_parts)
            # Remove JP parts from text
            text = re.sub(r"<JP>.*?</JP>", "", text, flags=re.DOTALL)

        # Extract ZH parts
        zh_parts = re.findall(r"<ZH>(.*?)</ZH>", text, re.DOTALL)
        if zh_parts:
            text_zh.extend(zh_parts)
            text = re.sub(r"<ZH>.*?</ZH>", "", text, flags=re.DOTALL)

        # Treat remaining text as ZH (cleanup)
        remaining = text.strip()
        if remaining:
            text_zh.append(remaining)

        return "".join(text_jp), "".join(text_zh)
