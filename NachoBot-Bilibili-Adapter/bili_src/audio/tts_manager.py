import asyncio
import json
import re
import time
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

from bili_src.core.multimodal_client import CoreMultimodalClient


def _split_text_for_streaming(text: str, max_length: int = 80) -> List[str]:
    """Split playback-sized chunks without importing the local model runtime."""

    text = str(text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?.，,、；;：:])", text)
    segments: List[str] = []
    for part in (item.strip() for item in parts):
        if not part:
            continue
        for offset in range(0, len(part), max_length):
            chunk = part[offset : offset + max_length]
            if segments and len(segments[-1]) < 10:
                segments[-1] += chunk
            else:
                segments.append(chunk)
    return segments or [text]

def _clean_text_for_tts(text: str) -> str:
    """Helper to clean text for TTS, similar to how utils did it. 
       We will keep this here if it's specific to TTS formatting."""
    # Assuming this exists in utils, but if we need a proxy:
    # We will import it from `bili_src.core.utils` later.
    from bili_src.core.utils import _clean_text_for_tts as util_clean
    return util_clean(text)

class TTSManager:
    def __init__(
        self,
        config: Any,
        logger,
        config_path: Optional[Path],
        audio_player: Any,
        send_danmu_callback: Callable,
        live2d_start_reply_callback: Optional[Callable] = None,
        live2d_finish_reply_callback: Optional[Callable] = None,
        live2d_apply_control_callback: Optional[Callable] = None,
        prepare_reply_callback: Optional[Callable] = None,
        multimodal_client: Optional[CoreMultimodalClient] = None,
    ):
        self.config = config
        self.logger = logger
        self.config_path = config_path
        self.audio_player = audio_player
        
        # Ordinary chat replies are delivered by OutgoingHandler.  This
        # manager keeps only the explicitly non-chat idle-speech path.
        del send_danmu_callback, live2d_finish_reply_callback
        self.on_start_replying = live2d_start_reply_callback
        self.apply_live2d_control = live2d_apply_control_callback
        self.prepare_reply = prepare_reply_callback

        self.multimodal_client = multimodal_client or CoreMultimodalClient(
            getattr(config, "nachobot_host", "127.0.0.1"),
            getattr(config, "nachobot_port", 8000),
        )
        self.tts_enable = False
        self.subtitle_path = "subtitles.txt"

        self._tts_manual_overrides: Dict[int, bool] = {}

        # Per-room language preference: "ja" (default, bilingual JP+ZH) or "zh" (Chinese-only)
        self._lang_overrides: Dict[int, str] = {}

        self._last_active_time = time.time()
        self._next_idle_target = self._get_next_idle_interval()

        self._init_tts_state()

    def _init_tts_state(self) -> None:
        if self.config.live_room_prompts:
            for room_cfg in self.config.live_room_prompts.values():
                if room_cfg.get("tts", {}).get("enable"):
                    self.tts_enable = True
                    self.subtitle_path = str(room_cfg.get("tts", {}).get("subtitle_path", "subtitles.txt"))
                    break

    def _get_next_idle_interval(self) -> float:
        min_sec = max(10, self.config.idle_tts_min_seconds)
        max_sec = max(min_sec, self.config.idle_tts_max_seconds)
        return random.uniform(min_sec, max_sec)

    def reset_idle_timer(self) -> None:
        self._last_active_time = time.time()
        self._next_idle_target = self._get_next_idle_interval()

    async def _synthesize_tts_segment(self, text: str, **kwargs) -> Any:
        """Synthesize explicit non-chat idle speech through Core."""

        response = await self.multimodal_client.synthesize_tts(
            text,
            platform=str(kwargs.get("platform") or self.config.platform),
            text_lang=kwargs.get("text_lang"),
        )
        import base64

        audio = response.get("audio_base64") or response.get("audio")
        if not isinstance(audio, str) or not audio.strip():
            raise RuntimeError("Core TTS returned no audio")
        try:
            return base64.b64decode(audio, validate=True)
        except Exception as exc:
            raise RuntimeError("Core TTS returned invalid audio") from exc

    async def _prepare_idle_reply(self, idle_item: Any) -> Any:
        """Normalize idle strings/dicts through the Live2D owner when present."""

        if self.prepare_reply is None:
            if isinstance(idle_item, dict):
                return idle_item.get("reply", str(idle_item))
            return str(idle_item)

        if isinstance(idle_item, dict):
            raw_item = json.dumps(idle_item, ensure_ascii=False)
        else:
            raw_item = str(idle_item)
        return await self.prepare_reply(raw_item)

    @staticmethod
    def _prepared_value(prepared: Any, key: str, default: Any = None) -> Any:
        if isinstance(prepared, dict):
            return prepared.get(key, default)
        return getattr(prepared, key, default)

    def is_tts_enabled(self, room_id: int) -> bool:
        if room_id in self._tts_manual_overrides:
            return self._tts_manual_overrides[room_id]

        if self.config.live_room_prompts:
            room_pts = self.config.live_room_prompts.get(room_id, {})
            return bool(room_pts.get("tts", {}).get("enable", False))

        return False

    def save_tts_config(self, room_id: int, enable: bool) -> None:
        try:
            import tomlkit
        except ImportError:
            self.logger.error("tomlkit not installed, cannot persist config")
            return

        if not self.config_path or not self.config_path.exists():
            self.logger.warning("Config path not set or file missing, skip persist")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)

            live_sec = doc.get("live")
            if not live_sec:
                self.logger.warning("Config missing [live] section, skip persist")
                return

            prompts = live_sec.get("room_prompts")
            if not prompts:
                self.logger.warning("Config missing [live.room_prompts], skip persist")
                return

            str_room_id = str(room_id)
            room_conf = prompts.get(str_room_id)
            if not room_conf:
                self.logger.warning(f"Room {room_id} not in config room_prompts, skip persist")
                return

            if "tts" not in room_conf:
                room_conf["tts"] = tomlkit.table()

            room_conf["tts"]["enable"] = enable

            with open(self.config_path, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)

            self.logger.info(f"Persisted TTS config for room {room_id}: enable={enable}")
        except Exception as exc:
            self.logger.error(
                "Error persisting TTS config: error_type={}",
                type(exc).__name__,
            )
            raise

    def save_idle_tts_config(self, enable: bool) -> None:
        try:
            import tomlkit
        except ImportError:
            self.logger.error("tomlkit not installed, cannot persist idle config")
            return

        if not self.config_path or not self.config_path.exists():
            self.logger.warning("Config path not set or file missing, skip persist idle")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)

            live_sec = doc.get("live")
            if not live_sec:
                self.logger.warning("Config missing [live] section, skip persist idle")
                return

            idle_tts = live_sec.get("idle_tts")
            if not idle_tts:
                self.logger.warning("Config missing [live.idle_tts], skip persist idle")
                return

            idle_tts["enable"] = enable

            with open(self.config_path, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)

            self.logger.info(f"Persisted idle_tts config: enable={enable}")
        except Exception as exc:
            self.logger.error(
                "Error persisting idle_tts config: error_type={}",
                type(exc).__name__,
            )
            raise

    def handle_tts_manual_command(self, room_id: int, user_id: str, text: str, user_name: str, allowed_user_ids: set) -> bool:
        command = text.strip().lower()
        if command not in ("#tts_on", "#tts_off", "#lang_switch", "#idle_on", "#idle_off"):
            return False

        allowed = False
        if str(user_id) == str(self.config.dede_user_id):
            allowed = True
        elif allowed_user_ids and str(user_id) in allowed_user_ids:
            allowed = True

        if not allowed:
            self.logger.warning(
                "TTS manual command rejected: room_id={} user_id={} user_name={}",
                room_id, user_id, user_name,
            )
            return True

        if command == "#lang_switch":
            return self._handle_lang_switch(room_id, user_id)

        if command in ("#idle_on", "#idle_off"):
            enable = command == "#idle_on"
            self.config.idle_tts_enable = enable
            action = "Enabled" if enable else "Disabled"
            self.logger.info("Idle TTS {} manually by user_id={}", action, user_id)
            if self.config_path:
                try:
                    self.save_idle_tts_config(enable)
                except Exception as exc:
                    self.logger.error(
                        "Failed to persist Idle TTS config: error_type={}",
                        type(exc).__name__,
                    )
            return True

        enable = command == "#tts_on"
        self._tts_manual_overrides[room_id] = enable

        if self.config.live_room_prompts and room_id in self.config.live_room_prompts:
            room_config = self.config.live_room_prompts[room_id]
            if "tts" not in room_config:
                room_config["tts"] = {}
            room_config["tts"]["enable"] = enable

        action = "Enabled" if enable else "Disabled"
        self.logger.info("TTS {} manually by user_id={} (Room: {})", action, user_id, room_id)

        if self.config_path:
            try:
                self.save_tts_config(room_id, enable)
            except Exception as exc:
                self.logger.error(
                    "Failed to persist TTS config: error_type={}",
                    type(exc).__name__,
                )

        return True

    def _handle_lang_switch(self, room_id: int, user_id: str) -> bool:
        """Toggle TTS language between Japanese (bilingual) and Chinese-only for a room."""
        current = self.get_room_language(room_id)
        new_lang = "zh" if current == "ja" else "ja"
        self._lang_overrides[room_id] = new_lang

        lang_display = {"ja": "日本語 (bilingual)", "zh": "中文 (Chinese-only)"}
        self.logger.info(
            "TTS language switched to {} by user_id={} (Room: {})",
            lang_display.get(new_lang, new_lang), user_id, room_id,
        )
        return True

    def get_room_language(self, room_id: int) -> str:
        """Get the current TTS language for a room. Default is 'ja' (bilingual)."""
        return self._lang_overrides.get(room_id, "ja")

    def parse_bilingual_response(self, text: str) -> Tuple[str, str]:
        if not text:
            return "", ""

        # Normalize full-width symbols and brackets to standard uppercase tags
        text = text.replace("＜", "<").replace("＞", ">")
        text = text.replace("／", "/")
        text = text.replace("Ｚ", "Z").replace("Ｈ", "H").replace("ｚ", "Z").replace("ｈ", "H")
        text = text.replace("Ｊ", "J").replace("Ｐ", "P").replace("ｊ", "J").replace("ｐ", "P")
        text = re.sub(r"[<\[【［](/?)(ZH|JP)[>\]】］]", lambda m: f"<{m.group(1)}{m.group(2).upper()}>", text, flags=re.IGNORECASE)

        jp_matches = re.findall(r"<JP>(.*?)</JP>", text, re.DOTALL)
        zh_matches = re.findall(r"<ZH>(.*?)</ZH>", text, re.DOTALL)

        text_jp = "".join(m.strip() for m in jp_matches if m.strip())
        text_zh = "".join(m.strip() for m in zh_matches if m.strip())

        if not text_jp and not text_zh:
            self.logger.warning(
                "Failed to parse bilingual tags: text_chars={}",
                len(text),
            )
            cleaned = re.sub(r"</?[A-Z]{2}>", "", text).strip()
            return "", cleaned

        return text_jp, text_zh

    def update_subtitle(self, text: str, subtitle_path: str = None) -> None:
        if not text:
            return

        target_path = subtitle_path or self.subtitle_path
        try:
            with open(target_path, "w", encoding="utf-8-sig") as f:
                f.write(text)
            self.logger.info(f"Subtitle updated: {target_path}")
        except Exception as exc:
            self.logger.error(
                "Failed to update subtitle: error_type={}",
                type(exc).__name__,
            )

    async def idle_tts_loop(self) -> None:
        if not self.config.idle_tts_texts:
            self.logger.warning("Idle TTS loop aborted: idle_tts_texts list is empty (failed to load json?).")
            return
        self.logger.info(f"Idle TTS loop started. Min: {self.config.idle_tts_min_seconds}s, Max: {self.config.idle_tts_max_seconds}s")
        while True:
            await asyncio.sleep(2.0)
            if not getattr(self.config, "idle_tts_enable", False):
                self.reset_idle_timer()
                continue
            if self.audio_player.is_playing:
                self.reset_idle_timer()
                continue
            idle_duration = time.time() - self._last_active_time
            if idle_duration > self._next_idle_target:
                idle_item = random.choice(self.config.idle_tts_texts)
                self.logger.info(f"Idle time ({idle_duration:.1f}s) reached target ({self._next_idle_target:.1f}s). Triggering preset TTS.")
                self.reset_idle_timer()
                try:
                    prepared = await self._prepare_idle_reply(idle_item)
                    if isinstance(prepared, str):
                        parsed_text = prepared
                    else:
                        parsed_text = str(self._prepared_value(prepared, "reply", "") or "")
                    control_id = self._prepared_value(prepared, "control_id")
                    
                    text_jp, text_zh = self.parse_bilingual_response(parsed_text)
                    display_text = text_zh if text_zh else parsed_text
                    tts_text = text_jp if text_jp else parsed_text
                    
                    self.update_subtitle(display_text)
                    cleaned_tts_text = _clean_text_for_tts(tts_text)
                    
                    # 分段流式：按句切分，逐句生成并立即送入空闲播放队列
                    segments = _split_text_for_streaming(cleaned_tts_text)
                    self.logger.info(f"Idle TTS 分段流式: {len(segments)} 个分段")

                    preset_name = None

                    first_segment = True
                    for idx, seg_text in enumerate(segments):
                        self.logger.info(
                            "Idle TTS segment {}/{}: chars={}",
                            idx + 1,
                            len(segments),
                            len(seg_text),
                        )
                        audio_data = await self._synthesize_tts_segment(
                            seg_text,
                            platform=self.config.platform,
                            preset_name=preset_name,
                            split_method="cut0",
                        )

                        if first_segment and audio_data:
                            first_segment = False
                            if self.on_start_replying:
                                await self.on_start_replying()
                            if self.apply_live2d_control and control_id:
                                await self.apply_live2d_control(control_id)

                        if audio_data:
                            await asyncio.to_thread(self.audio_player.play_idle, audio_data)
                except Exception as exc:
                    self.logger.error(
                        "Failed to generate/play idle TTS: error_type={}",
                        type(exc).__name__,
                    )
