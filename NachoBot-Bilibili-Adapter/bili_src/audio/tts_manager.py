import asyncio
import json
import re
import time
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

_multimodal_adapter_path = Path(__file__).resolve().parents[3] / "NachoBot-Multimodal-Adapter"
if _multimodal_adapter_path.exists() and str(_multimodal_adapter_path) not in sys.path:
    sys.path.insert(0, str(_multimodal_adapter_path))

try:
    from nachobot_multimodal.utils.emotion_resolver import resolve_emotion_preset_remote
except ImportError:
    resolve_emotion_preset_remote = None

try:
    from nachobot_multimodal.utils.tts_runtime import TTSRuntime
except ImportError:
    TTSRuntime = None

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
        tts_model_class: Any = None,
        tts_import_error: Optional[str] = None,
        tts_config_dir: Optional[Path] = None,
    ):
        self.config = config
        self.logger = logger
        self.config_path = config_path
        self.audio_player = audio_player
        
        self.send_danmu = send_danmu_callback
        self.on_start_replying = live2d_start_reply_callback
        self.on_reply_finished = live2d_finish_reply_callback
        self.apply_live2d_control = live2d_apply_control_callback
        self.prepare_reply = prepare_reply_callback
        
        self.tts_model_class = tts_model_class
        self.tts_import_error = tts_import_error
        self.tts_config_dir = Path(tts_config_dir) if tts_config_dir else None
        self._tts_runtime = (
            TTSRuntime(
                self.tts_config_dir,
                model_class=tts_model_class,
                import_error=tts_import_error,
            )
            if TTSRuntime is not None
            else None
        )
        self.tts_model = None
        self.tts_enable = False
        self.subtitle_path = "subtitles.txt"

        self._tts_buffer: Dict[int, List[str]] = {}
        self._tts_timer: Dict[int, asyncio.Task] = {}
        self._tts_metadata: Dict[int, Dict[str, Any]] = {}
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

        if self.tts_enable:
            self.ensure_tts_model()

    def _get_next_idle_interval(self) -> float:
        min_sec = max(10, self.config.idle_tts_min_seconds)
        max_sec = max(min_sec, self.config.idle_tts_max_seconds)
        return random.uniform(min_sec, max_sec)

    def reset_idle_timer(self) -> None:
        self._last_active_time = time.time()
        self._next_idle_target = self._get_next_idle_interval()

    def ensure_tts_model(self) -> bool:
        """Refresh the shared runtime and mirror its actual model."""

        if self._tts_runtime is None:
            self.logger.error(f"TTS enabled but TTSModel not available: {self.tts_import_error}")
            self.tts_model = None
            return False
        ready = self._tts_runtime.ensure_tts_model()
        self.tts_model = self._tts_runtime.model
        if not ready:
            self.logger.error(f"TTS configuration unavailable: {self._tts_runtime.error}")
        return ready

    async def _synthesize_tts_segment(self, text: str, **kwargs) -> Any:
        """Refresh and synthesize one segment under the shared async boundary."""

        if self._tts_runtime is None:
            raise RuntimeError("TTS runtime unavailable")
        try:
            async with self._tts_runtime.model_context() as model:
                self.tts_model = model
                return await model.tts(text=text, **kwargs)
        except Exception:
            self.tts_model = self._tts_runtime.model
            raise

    async def _resolve_remote_emotion_preset(self, text: str) -> Optional[str]:
        """Resolve emotion through the same base config selected for TTS."""

        if resolve_emotion_preset_remote is None:
            return None
        base_path = None
        if self.tts_config_dir:
            base_path = (
                self.tts_config_dir
                if self.tts_config_dir.suffix.lower() == ".toml"
                else self.tts_config_dir / "base.toml"
            )
        return await resolve_emotion_preset_remote(text, base_config_path=base_path)

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

    def repair_unbalanced_tags(self, text: str, open_zh: int, close_zh: int, open_jp: int, close_jp: int) -> str:
        repaired = text

        if open_zh == 0 and close_zh > 0:
            self.logger.warning(f"Removing {close_zh} orphaned </ZH> closing tag(s) without opening tags")
            repaired = repaired.replace("</ZH>", "")
        elif close_zh == 0 and open_zh > 0:
            self.logger.warning(f"Adding {open_zh} missing </ZH> closing tag(s)")
            repaired = repaired + "</ZH>" * open_zh

        if open_jp == 0 and close_jp > 0:
            self.logger.warning(f"Removing {close_jp} orphaned </JP> closing tag(s) without opening tags")
            repaired = repaired.replace("</JP>", "")
        elif close_jp == 0 and open_jp > 0:
            self.logger.warning(f"Adding {open_jp} missing </JP> closing tag(s)")
            repaired = repaired + "</JP>" * open_jp

        if repaired != text:
            self.logger.info(
                "Tag repair applied: original_chars={} repaired_chars={}",
                len(text),
                len(repaired),
            )

        return repaired

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
                    from nachobot_multimodal.utils.text_splitter import split_text_for_streaming
                    segments = split_text_for_streaming(cleaned_tts_text)
                    self.logger.info(f"Idle TTS 分段流式: {len(segments)} 个分段")

                    preset_name = None
                    if resolve_emotion_preset_remote is not None:
                        try:
                            preset_name = await self._resolve_remote_emotion_preset(cleaned_tts_text)
                        except Exception as exc:
                            self.logger.error(
                                "Failed to resolve emotion preset: error_type={}",
                                type(exc).__name__,
                            )

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

    async def wait_and_process_tts(self, room_id: int, delay: float = 0.5) -> None:
        try:
            await asyncio.sleep(delay)
            await self.process_buffered_live_reply(room_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.logger.error(
                "TTS timer error: error_type={}",
                type(exc).__name__,
            )

    def buffer_tts_reply(
        self,
        room_id: int,
        text: str,
        reply_mid: str,
        reply_dmid: str,
        control_id: Optional[str] = None,
    ):
        # Normalize full-width symbols and brackets to standard uppercase tags
        text = text.replace("＜", "<").replace("＞", ">")
        text = text.replace("／", "/")
        text = text.replace("Ｚ", "Z").replace("Ｈ", "H").replace("ｚ", "Z").replace("ｈ", "H")
        text = text.replace("Ｊ", "J").replace("Ｐ", "P").replace("ｊ", "J").replace("ｐ", "P")
        text = re.sub(r"[<\[【［](/?)(ZH|JP)[>\]】］]", lambda m: f"<{m.group(1)}{m.group(2).upper()}>", text, flags=re.IGNORECASE)
        
        buffer = self._tts_buffer.setdefault(room_id, [])
        buffer.append(text)

        if room_id not in self._tts_metadata:
            self._tts_metadata[room_id] = {
                "reply_mid": reply_mid,
                "reply_dmid": reply_dmid,
                "start_time": time.time(),
                "control_id": control_id,
            }
        elif control_id:
            self._tts_metadata[room_id]["control_id"] = control_id

        if room_id in self._tts_timer:
            self._tts_timer[room_id].cancel()

        self._tts_timer[room_id] = asyncio.create_task(self.wait_and_process_tts(room_id))

    async def process_buffered_live_reply(self, room_id: int) -> None:
        try:
            buffer = self._tts_buffer.get(room_id)
            if not buffer:
                return

            full_text = "".join(buffer)

            open_zh = full_text.count("<ZH>")
            close_zh = full_text.count("</ZH>")
            open_jp = full_text.count("<JP>")
            close_jp = full_text.count("</JP>")

            is_balanced = (open_zh == close_zh) and (open_jp == close_jp)

            self.logger.info(
                "SmartBuffering Check: balanced={} (ZH:{}/{} JP:{}/{}) chars={}",
                is_balanced,
                open_zh,
                close_zh,
                open_jp,
                close_jp,
                len(full_text),
            )

            metadata = self._tts_metadata.get(room_id, {})
            start_time = metadata.get("start_time", 0)
            elapsed = time.time() - start_time

            if not is_balanced and elapsed < 8.0:
                self.logger.info(f"Buffered TTS text unbalanced, extending wait... (elapsed={elapsed:.1f}s)")
                self._tts_timer[room_id] = asyncio.create_task(self.wait_and_process_tts(room_id, delay=1.0))
                return

            if not is_balanced:
                self.logger.warning("TTS buffer timeout with unbalanced tags. Attempting repair...")
                full_text = self.repair_unbalanced_tags(full_text, open_zh, close_zh, open_jp, close_jp)

            self._tts_buffer[room_id] = []
            self._tts_timer.pop(room_id, None)
            self._tts_metadata.pop(room_id, None)

            reply_mid = metadata.get("reply_mid")
            reply_dmid = metadata.get("reply_dmid")
            control_id = metadata.get("control_id")
            reply_started = False
            control_apply_attempted = False

            async def activate_reply() -> None:
                nonlocal reply_started, control_apply_attempted
                if not reply_started and self.on_start_replying:
                    reply_started = True
                    await self.on_start_replying()
                if not control_apply_attempted and self.apply_live2d_control and control_id:
                    control_apply_attempted = True
                    await self.apply_live2d_control(control_id)

            self.logger.info(
                "Processing buffered TTS reply for room {}: chars={} buffer_segments={}",
                room_id,
                len(full_text),
                len(buffer),
            )

            room_config = self.config.live_room_prompts.get(room_id, {})
            tts_config = room_config.get("tts", {})

            # Determine TTS language mode for this room
            room_lang = self.get_room_language(room_id)

            if room_lang == "zh":
                # Chinese-only mode: strip any residual bilingual tags and TTS the Chinese text directly
                display_text = re.sub(r"</?[A-Z]{2}>", "", full_text).strip()
                msg_to_send = display_text
                tts_text = display_text
            else:
                # Default bilingual mode: parse <JP> and <ZH> tags
                text_jp, text_zh = self.parse_bilingual_response(full_text)
                display_text = text_zh if text_zh else full_text
                msg_to_send = text_zh if text_zh else full_text
                tts_text = text_jp if text_jp else ""

            # Enter the TTS path when an already-created client exists or this
            # room is configured for TTS. Each segment below performs the live
            # refresh; the cached object must not bypass configuration checks.
            if self.tts_model or (tts_config and tts_config.get("enable")):
                subtitle_path = str(tts_config.get("subtitle_path") or "subtitles.txt")
                self.update_subtitle(display_text, subtitle_path=subtitle_path)

                if tts_text:
                    cleaned_tts_text = _clean_text_for_tts(tts_text)
                    self.logger.info(
                        "TTS Generating for room {} (lang={}): chars={}",
                        room_id,
                        room_lang,
                        len(cleaned_tts_text),
                    )
                    try:
                        # 分段流式：按句切分文本，逐句生成并立即送入播放队列
                        from nachobot_multimodal.utils.text_splitter import split_text_for_streaming
                        segments = split_text_for_streaming(cleaned_tts_text)
                        self.logger.info(f"TTS 分段流式: {len(segments)} 个分段")

                        preset_name = None
                        if resolve_emotion_preset_remote is not None:
                            try:
                                preset_name = await self._resolve_remote_emotion_preset(cleaned_tts_text)
                            except Exception as exc:
                                self.logger.error(
                                    "Failed to resolve emotion preset: error_type={}",
                                    type(exc).__name__,
                                )

                        first_segment = True
                        for idx, seg_text in enumerate(segments):
                            self.logger.info(
                                "TTS segment {}/{}: chars={}",
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
                                # 首段音频就绪后触发 Live2D 控制，最多一次
                                try:
                                    await activate_reply()
                                except Exception as exc:
                                    self.logger.error(
                                        "Live2D reply hook error: error_type={}",
                                        type(exc).__name__,
                                    )
                                self.audio_player.interrupt_idle()

                            if audio_data:
                                self.audio_player.play(audio_data)

                        self.logger.info(f"TTS Played successfully for room {room_id}")
                        return
                    except Exception as exc:
                        self.logger.error(
                            "TTS generation failed: error_type={}",
                            type(exc).__name__,
                        )
                        self.logger.info("Fallback to sending danmu due to TTS error")
                else:
                    self.logger.warning(
                        "TTS enabled for room {} but no parseable text for TTS. Sending danmu.",
                        room_id,
                    )

            # Fallback
            if self.on_start_replying or self.apply_live2d_control:
                try:
                    await activate_reply()
                except Exception as exc:
                    self.logger.error(
                        "Live2D reply hook error: error_type={}",
                        type(exc).__name__,
                    )

            safe_danmu_text = msg_to_send
            if len(safe_danmu_text) > 30:
                safe_danmu_text = safe_danmu_text[:30] + "..."

            await self.send_danmu(room_id, safe_danmu_text, reply_mid, reply_dmid)

            if self.on_reply_finished:
                try:
                    await self.on_reply_finished()
                except Exception as exc:
                    self.logger.error(
                        "Live2D reply hook error: error_type={}",
                        type(exc).__name__,
                    )

        except Exception as exc:
            self.logger.error(
                "Error processing buffered TTS reply: error_type={}",
                type(exc).__name__,
            )
            self._tts_buffer.pop(room_id, None)
            self._tts_metadata.pop(room_id, None)
