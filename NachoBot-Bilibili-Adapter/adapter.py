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
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

# Try to import TTS model
tts_adapter_path = Path(r"C:\Users\BigSh0t\Nacho-with-u\nachobot_tts_adapter")
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


class BilibiliAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                )
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
            tasks.append(self.mic_worker.start())
            tasks.append(self._mic_control_loop())

        await asyncio.gather(*tasks)

    async def _mic_control_loop(self) -> None:
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return

        room_id = self.mic_worker.config.room_id

        while True:
            try:
                should_pause = True

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

        jp_match = re.search(r"<JP>(.*?)</JP>", text, re.DOTALL)
        zh_match = re.search(r"<ZH>(.*?)</ZH>", text, re.DOTALL)

        text_jp = jp_match.group(1).strip() if jp_match else ""
        text_zh = zh_match.group(1).strip() if zh_match else ""

        if not text_jp and not text_zh:
            cleaned = re.sub(r"</?[A-Z]{2}>", "", text).strip()
            return "", cleaned

        return text_jp, text_zh

    async def _play_audio(self, audio_data: bytes) -> None:
        if not audio_data:
            return

        try:
            temp_path = os.path.join(tempfile.gettempdir(), "nachobot_tts_temp.wav")
            with open(temp_path, "wb") as f:
                f.write(audio_data)
            winsound.PlaySound(temp_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self.logger.error(f"Failed to play audio: {e}")

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
        if manual_state is not True:
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
        self._screen_manual_until = time.time() + self._screen_manual_duration_seconds
        action = "enabled" if enable else "disabled"
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

    # ========== Incoming Message Handlers ==========

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
    ) -> None:
        if not text:
            return
        if self._handle_mic_manual_command(room_id, user_id, text, user_name):
            return
        if self._handle_screen_manual_command(room_id, user_id, text, user_name):
            return
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
            template_items: Dict[str, str] = {}
            if reply_prompt:
                template_items["replyer_prompt"] = reply_prompt
            if planner_prompt:
                template_items["planner_prompt"] = planner_prompt
            template_info = TemplateInfo(
                template_items=template_items,
                template_name=f"bilibili_live_{room_id}",
                template_default=False,
            )
        additional_config = {
            "room_id": room_id,
            "reply_mid": reply_mid,
            "reply_dmid": reply_dmid,
        }
        if is_mentioned:
            additional_config["is_mentioned"] = 1.0

        processed_text = text
        if self.config.live_disable_network_search:
            processed_text = _mask_urls(processed_text)
            additional_config["disable_tools"] = True

        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=str(message_id),
            time=float(timestamp),
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
        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=processed_text),
            raw_message=None,
        )
        await self._send_to_nachobot(message)

    async def handle_incoming_gift(
        self,
        room_id: int,
        gift_name: str,
        num: int,
        user_id: str,
        user_name: str,
        timestamp: float,
    ) -> None:
        self.logger.info(
            f"Gift: [{room_id}] {user_name}({user_id}) sent {gift_name} x{num}"
        )
        # gift_data = f"{gift_name}:{num}"
        # TODO: Implement gift handling if needed
        pass

    async def _handle_mic_recognition(self, text: str) -> None:
        if not text:
            return
        if not self.mic_worker or not self.mic_worker.config.room_id:
            return
        room_id = self.mic_worker.config.room_id
        await self.handle_mic_message(room_id, text)

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
            platform=self.config.platform,
            message_id=f"mic_{int(time.time() * 1000)}",
            time=time.time(),
            user_info=UserInfo(
                platform=self.config.platform,
                user_id="2146014839",
                user_nickname="主人",
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
        await self._send_to_nachobot(message)

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
            f"SuperChat: [{room_id}] {user_name}({user_id}): [￥{price}] {message_text}"
        )

        sc_data = f"{price}:{message_text}"
        segments = [Seg(type="superchat", data=sc_data)]

        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=f"sc_{room_id}_{user_id}_{int(timestamp * 1000)}_{uuid.uuid4().hex[:8]}",
            time=float(timestamp),
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
            additional_config={"room_id": room_id},
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=segments),
            raw_message=f"[SC￥{price}] {message_text}",
            processed_plain_text=f"[SC￥{price}] {message_text}",
        )

        await self._send_to_nachobot(message)

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
            f"Guard: [{room_id}] {user_name}({user_id}) bought {guard_name} x{num}"
        )

        gift_data = f"{guard_name}:{num}"
        priority_data = {
            "message_type": "vip",
            "message_priority": 1000.0,
            "guard_level": guard_level,
        }

        segments = [
            Seg(type="gift", data=gift_data),
            Seg(type="priority_info", data=json.dumps(priority_data)),
        ]

        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=f"guard_{room_id}_{user_id}_{int(timestamp * 1000)}_{uuid.uuid4().hex[:8]}",
            time=float(timestamp),
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
            additional_config={"room_id": room_id},
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=segments),
            raw_message=f"开通了 {guard_name} x{num}",
            processed_plain_text=f"开通了 {guard_name} x{num}",
        )

        await self._send_to_nachobot(message)

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
        if room_prompts:
            tts_enable = bool(room_prompts.get("tts", {}).get("enable", False))

        if tts_enable:
            tts_instruction = (
                "\n\n非常重要：请必须同时输出中文回复和对应的日文翻译（用于语音播放），格式严格如下：\n"
                "<JP>日文翻译内容</JP><ZH>中文原本意思</ZH>\n"
                "例如：\n"
                "<JP>こんにちは、ご飯を食べましたか？</JP><ZH>你好呀，吃过饭了吗？</ZH>\n"
                "只输出上述XML格式，不要输出其他多余内容(包括前后缀，冒号和引号，括号，表情包等)。"
            )
            reply_prompt += tts_instruction

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
            try:
                at_items = await self.api.get_at_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.warning(f"At notice fetch error: {exc}")
            if reply_items or at_items:
                self.logger.info(
                    "Comment notices: reply=%s at=%s",
                    len(reply_items),
                    len(at_items),
                )
            else:
                self.logger.debug("Comment notices: 0")
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
        segments = _split_bilibili_text(text, max_length=BILIBILI_DANMU_MAX_LENGTH)
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
        if not text:
            return

        if len(text) <= 4 and all("\u4e00" <= c <= "\u9fff" for c in text):
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

        if tts_enable and self.tts_model:
            text_jp, text_zh = self._parse_bilingual_response(text)

            display_text = text_zh if text_zh else text
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
                msg_to_send = text_zh if text_zh else text
                await self._send_danmu(
                    room_id, msg_to_send, reply_mid or None, reply_dmid or None
                )
                return

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
