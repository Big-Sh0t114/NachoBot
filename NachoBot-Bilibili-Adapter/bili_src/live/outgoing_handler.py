import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import re
import time
import unicodedata
from typing import Any, Dict, Optional

from ncnk_message import MessageBase
from bili_src.core.utils import (
    _extract_image_base64,
    _extract_plain_text,
    _extract_voice_base64,
    _extract_voice_stream_payload,
    _has_voice_stream_segment,
    _find_reply_id,
    _strip_emoji,
    _split_bilibili_text,
    BILIBILI_DANMU_MAX_LENGTH,
    BILIBILI_DANMU_SEND_DELAY_SECONDS,
)
from bili_src.live.two_phase_search import BilibiliLiveSearchOrchestrator, SearchFollowupResult
from bili_src.live2d.remote_controller import PreparedReplyResult


@dataclass
class _PendingLiveReply:
    message_id: str
    room_id: int
    reply_mid: str
    reply_dmid: str
    first_pass_tts_language: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prepared: PreparedReplyResult | None = None
    timer: asyncio.Task | None = None
    status: str = "preparing"
    active_stream_id: str = ""
    active_stream_metadata: dict[str, Any] | None = None
    stream_states: OrderedDict[str, str] = field(default_factory=OrderedDict)
    audio_succeeded: bool = False
    final_search_delivered: bool = False
    control_applied: bool = False
    buffer_claimed: bool = False
    buffered_voice_data: str = ""
    finished_at: float = 0.0
    evicted: bool = False


class OutgoingHandler:
    PENDING_REPLY_TIMEOUT_SECONDS = 120.0
    STREAM_INACTIVITY_TIMEOUT_SECONDS = 120.0
    COMPLETED_REPLY_TTL_SECONDS = 600.0
    MAX_REPLY_STATES = 256
    MAX_STREAMS_PER_REPLY = 64

    def __init__(self, config: Any, logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        self.live_search = BilibiliLiveSearchOrchestrator(adapter_ref, logger)
        self._reply_states: OrderedDict[str, _PendingLiveReply] = OrderedDict()
        self._timer_tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    @staticmethod
    def _explicit_tts_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip()) and len(value) <= 16_384
        if isinstance(value, dict):
            text = value.get("text")
            return isinstance(text, str) and bool(text.strip()) and len(text) <= 16_384
        return False

    @classmethod
    def _reply_envelope(cls, text: Any) -> dict[str, Any] | None:
        if not isinstance(text, str) or not text.strip() or len(text) > 65_536:
            return None
        candidate = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```", candidate, re.IGNORECASE | re.DOTALL
        )
        if fenced:
            candidate = fenced.group(1).strip()
        try:
            parsed = json.loads(candidate, strict=False)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("reply"), str):
            return None
        return parsed

    @classmethod
    def _has_explicit_tts(cls, segment: Any) -> bool:
        segment_type = getattr(segment, "type", None)
        data = getattr(segment, "data", None)
        if segment_type == "tts_text":
            if cls._explicit_tts_value(data):
                return True
            if isinstance(data, dict) and cls._explicit_tts_value(data.get("tts_text")):
                return True
        if segment_type == "text":
            envelope = cls._reply_envelope(data)
            if envelope is not None and cls._explicit_tts_value(envelope.get("tts_text")):
                return True
        if segment_type == "seglist" and isinstance(data, list):
            return any(cls._has_explicit_tts(child) for child in data)
        return False

    @classmethod
    def _explicit_tts_language(cls, raw_message: Any) -> str | None:
        envelope = cls._reply_envelope(raw_message)
        if envelope is None:
            return None
        value = envelope.get("tts_text")
        if not isinstance(value, dict):
            return None
        text = value.get("text")
        language = value.get("lang")
        if (
            isinstance(text, str)
            and text.strip()
            and len(text) <= 16_384
            and isinstance(language, str)
            and language in {"zh", "ja"}
        ):
            return language
        return None

    def _clean_display_text(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.replace("\\u200b", "").replace("\\u200B", "")

        # The Live2D boundary normally returns only reply. Keep the fallback
        # conservative if a legacy implementation ever returns the envelope.
        if text.startswith(("{", "[", "```")):
            envelope = self._reply_envelope(text)
            if envelope is None:
                return ""
            text = envelope["reply"]


        text = re.sub(r"</?[A-Za-z][^>]{0,100}>", "", text)
        text = re.sub(r"<\|[^>]{0,100}\|>", "", text)
        text = "".join(
            char
            for char in text
            if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
        )
        text = _strip_emoji(text).strip()
        return text if any(not char.isspace() for char in text) else ""

    def _prepared_with_clean_reply(self, prepared: Any) -> PreparedReplyResult:
        reply = self._clean_display_text(
            getattr(prepared, "reply", "")
            if not isinstance(prepared, dict)
            else prepared.get("reply", "")
        )
        web_search = bool(
            getattr(prepared, "web_search", False)
            if not isinstance(prepared, dict)
            else prepared.get("web_search", False)
        )
        search_query = str(
            (
                getattr(prepared, "search_query", "")
                if not isinstance(prepared, dict)
                else prepared.get("search_query", "")
            )
            or ""
        )
        control_id = (
            getattr(prepared, "control_id", None)
            if not isinstance(prepared, dict)
            else prepared.get("control_id")
        )
        return PreparedReplyResult(reply, web_search, search_query, control_id)

    def _subtitle_path(self, room_id: int) -> str:
        room_prompts = getattr(self.config, "live_room_prompts", {}) or {}
        room_config = room_prompts.get(room_id, {})
        tts_config = room_config.get("tts", {}) if isinstance(room_config, dict) else {}
        return str(tts_config.get("subtitle_path") or "subtitles.txt")

    def _room_tts_enabled(self, room_id: int) -> bool:
        manager = getattr(self.adapter, "tts_manager", None)
        is_enabled = getattr(manager, "is_tts_enabled", None)
        if not callable(is_enabled):
            return False
        try:
            return bool(is_enabled(room_id))
        except Exception as exc:
            self.logger.warning("Bilibili TTS route check failed: error_type={}", type(exc).__name__)
            return False

    def _write_live_subtitle(self, room_id: int, text: str) -> bool:
        if not text:
            return False
        tts_manager = getattr(self.adapter, "tts_manager", None)
        if tts_manager is None:
            return False
        try:
            tts_manager.update_subtitle(text, subtitle_path=self._subtitle_path(room_id))
        except Exception as exc:
            self.logger.warning("Bilibili subtitle update failed: error_type={}", type(exc).__name__)
            return False
        return True

    def _prune_reply_states(self) -> None:
        now = time.monotonic()
        for message_id, state in list(self._reply_states.items()):
            if (
                state.status in {"success", "fallback", "failed", "superseded"}
                and state.finished_at
                and now - state.finished_at > self.COMPLETED_REPLY_TTL_SECONDS
            ):
                self._forget_reply_state(message_id, state)
        while len(self._reply_states) >= self.MAX_REPLY_STATES:
            message_id, state = next(iter(self._reply_states.items()))
            self._forget_reply_state(message_id, state)

    def _forget_reply_state(self, message_id: str, state: _PendingLiveReply) -> None:
        self._reply_states.pop(message_id, None)
        state.evicted = True
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if not state.ready.is_set():
            state.ready.set()

    def _reserve_reply_state(
        self,
        message_id: str,
        room_id: int,
        reply_mid: str,
        reply_dmid: str,
    ) -> _PendingLiveReply | None:
        self._prune_reply_states()
        if self._shutting_down or message_id in self._reply_states:
            return None
        state = _PendingLiveReply(message_id, room_id, reply_mid, reply_dmid)
        self._reply_states[message_id] = state
        return state

    def _cancel_reply_timer(self, state: _PendingLiveReply) -> None:
        timer = state.timer
        state.timer = None
        if (
            timer is not None
            and timer is not asyncio.current_task()
            and not timer.done()
        ):
            timer.cancel()

    def _arm_reply_timer(self, state: _PendingLiveReply, delay: float) -> None:
        self._cancel_reply_timer(state)
        if self._shutting_down or state.evicted:
            return
        timer = asyncio.create_task(self._reply_timeout(state, delay))
        state.timer = timer
        self._timer_tasks.add(timer)
        timer.add_done_callback(self._timer_tasks.discard)

    async def _reply_timeout(self, state: _PendingLiveReply, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._expire_pending_reply(state)
        except asyncio.CancelledError:
            raise

    async def _expire_pending_reply(self, state: _PendingLiveReply) -> None:
        async with state.event_lock:
            if (
                self._shutting_down
                or state.evicted
                or state.status in {"fallback", "failed", "superseded"}
            ):
                return
            if state.active_stream_metadata is not None:
                await self._stop_active_stream(state)
            if state.audio_succeeded:
                return
            if state.buffer_claimed:
                return
            await self._fallback_pending_reply(state)

    async def _stop_active_stream(self, state: _PendingLiveReply) -> None:
        metadata = state.active_stream_metadata
        if not metadata:
            return
        payload = dict(metadata)
        payload["event"] = "abort"
        player = self.adapter.audio_player
        try:
            if (
                hasattr(player, "active_voice_stream_id")
                and player.active_voice_stream_id != metadata.get("stream_id")
            ):
                return
            await player.handle_voice_stream_event(payload)
        except Exception as exc:
            self.logger.warning(
                "Core voice stream cleanup failed: error_type={}", type(exc).__name__
            )
        finally:
            if state.active_stream_metadata is metadata:
                state.active_stream_metadata = None
                state.active_stream_id = ""

    def _supersede_other_active_replies(
        self, active_state: _PendingLiveReply | None
    ) -> None:
        now = time.monotonic()
        for state in self._reply_states.values():
            if state is active_state or not state.active_stream_id:
                continue
            state.stream_states[state.active_stream_id] = "superseded"
            state.active_stream_id = ""
            state.active_stream_metadata = None
            state.status = "superseded"
            state.finished_at = now
            self._cancel_reply_timer(state)

    async def _start_pending_search(self, state: _PendingLiveReply) -> None:
        prepared = state.prepared
        if prepared is None:
            return
        first_delivery = True

        async def deliver_search_result(
            result: PreparedReplyResult,
            room_id: int,
            reply_mid: str,
            reply_dmid: str,
        ) -> None:
            if self._shutting_down or state.evicted:
                return
            nonlocal first_delivery
            if first_delivery:
                first_delivery = False
                return
            async with state.event_lock:
                if self._shutting_down or state.evicted:
                    return
                delivered = await self._deliver_live_reply(
                    result, room_id, reply_mid, reply_dmid
                )
                if delivered and not self._shutting_down and not state.evicted:
                    state.final_search_delivered = True

        await self.live_search.handle(
            prepared,
            room_id=state.room_id,
            reply_mid=state.reply_mid,
            reply_dmid=state.reply_dmid,
            deliver=deliver_search_result,
            first_pass_tts_language=state.first_pass_tts_language,
        )

    async def _fallback_pending_reply(self, state: _PendingLiveReply) -> None:
        if (
            self._shutting_down
            or state.evicted
            or state.audio_succeeded
            or state.status in {"fallback", "failed", "superseded"}
        ):
            return
        self._cancel_reply_timer(state)
        await self._stop_active_stream(state)
        if state.final_search_delivered:
            state.status = "success"
            state.finished_at = time.monotonic()
            return
        state.status = "fallback"
        state.finished_at = time.monotonic()
        if state.prepared is not None:
            await self._deliver_live_reply(
                state.prepared,
                state.room_id,
                state.reply_mid,
                state.reply_dmid,
                control_already_applied=state.control_applied,
                update_subtitle=False,
            )

    async def _apply_stream_control(self, state: _PendingLiveReply) -> None:
        if state.control_applied or state.prepared is None:
            return
        state.control_applied = True
        manager = getattr(self.adapter, "live2d_manager", None)
        controller = getattr(manager, "controller", None)
        if controller is not None:
            try:
                await controller.on_start_replying()
            except Exception as exc:
                self.logger.warning("Live2D reply hook failed: error_type={}", type(exc).__name__)
        try:
            await manager.apply_control(state.prepared.control_id)
        except Exception as exc:
            self.logger.warning("Live2D control apply failed: error_type={}", type(exc).__name__)

    async def _handle_paired_buffered_voice(self, state: _PendingLiveReply) -> None:
        async with state.event_lock:
            await state.ready.wait()
            if (
                self._shutting_down
                or state.evicted
                or state.audio_succeeded
                or state.status in {"fallback", "failed"}
                or not state.prepared
            ):
                return
            self._cancel_reply_timer(state)
            await self._apply_stream_control(state)
            try:
                accepted = await self._play_core_voice(
                    state.buffered_voice_data,
                    idle=False,
                    control_id=state.prepared.control_id,
                    control_already_applied=True,
                    start_already_notified=True,
                )
            except Exception as exc:
                self.logger.warning("Buffered Core voice playback failed: error_type={}", type(exc).__name__)
                accepted = False
            if accepted:
                state.audio_succeeded = True
                state.status = "success"
                state.finished_at = time.monotonic()
                return
            await self._fallback_pending_reply(state)

    async def shutdown(self) -> None:
        self._shutting_down = True
        await self.live_search.shutdown()
        timers = tuple(self._timer_tasks)
        for state in tuple(self._reply_states.values()):
            self._forget_reply_state(state.message_id, state)
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        self._timer_tasks.clear()

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        message = MessageBase.from_dict(raw_message_base_dict)
        self.logger.info(
            "Incoming from NachoBot: platform={} group_id={} user_id={}",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            getattr(message.message_info.user_info, "user_id", None),
        )
        seg = message.message_segment
        if seg.type == "command":
            await self._handle_command(message)
            return

        # Core's PCM lifecycle is a sequence of audio-control messages, not a
        # complete WAV artifact or a second display-text reply.
        if _has_voice_stream_segment(seg):
            await self._handle_voice_stream_event(
                _extract_voice_stream_payload(seg),
                room_id=self._resolve_room_id(message),
            )
            return

        voice_data = _extract_voice_base64(seg)
        image_data = _extract_image_base64(seg)
        if image_data:
            private_target = self.adapter.private_handler.resolve_private_target(
                message
            )
            if private_target:
                await self.adapter.private_handler.send_private_image(
                    private_target, image_data
                )
                text = _extract_plain_text(seg).strip()
                if text:
                    await self.adapter.private_handler.send_private_message(
                        private_target, text
                    )
            else:
                self.logger.warning("Image message unsupported for non-private target")
            return

        text = _extract_plain_text(seg).strip()
        if not text and not voice_data:
            return

        room_id = self._resolve_room_id(message)
        comment_target = self.adapter.comment_handler.resolve_comment_target(message)
        private_target = (
            self.adapter.private_handler.resolve_private_target(message)
            if room_id is None and comment_target is None
            else None
        )

        message_id = str(message.message_info.message_id or "").strip()
        if (
            room_id is not None
            and comment_target is None
            and private_target is None
            and not voice_data
            and self._has_explicit_tts(seg)
            and message_id
        ):
            existing = self._reply_states.get(message_id)
            if existing is not None:
                return
            reply_dmid = _find_reply_id(seg)
            reply_mid = self._lookup_reply_mid(room_id, reply_dmid) if reply_dmid else ""
            state = self._reserve_reply_state(
                message_id, room_id, reply_mid or "", reply_dmid or ""
            )
            if state is None:
                return
            try:
                raw_reply = text
                state.first_pass_tts_language = self._explicit_tts_language(raw_reply)
                prepared = await self.adapter.live2d_manager.prepare_reply(raw_reply)
                if self._shutting_down or state.evicted:
                    return
                state.prepared = self._prepared_with_clean_reply(prepared)
                self._write_live_subtitle(room_id, state.prepared.reply)
                state.status = "waiting"
                self._arm_reply_timer(state, self.PENDING_REPLY_TIMEOUT_SECONDS)
                await self._start_pending_search(state)
            except asyncio.CancelledError:
                self._forget_reply_state(message_id, state)
                raise
            except Exception as exc:
                state.status = "failed"
                state.finished_at = time.monotonic()
                self._cancel_reply_timer(state)
                self.logger.warning(
                    "Explicit TTS reply preparation failed: error_type={}",
                    type(exc).__name__,
                )
            finally:
                state.ready.set()
            if state.buffer_claimed and not state.audio_succeeded:
                await self._handle_paired_buffered_voice(state)
            return

        if room_id is not None and not text and voice_data:
            candidates = [
                state
                for state in self._reply_states.values()
                if state.room_id == room_id
                and not state.evicted
                and not state.audio_succeeded
                and state.status in {"preparing", "waiting"}
                and not state.buffer_claimed
            ]
            if len(candidates) == 1:
                state = candidates[0]
                state.buffer_claimed = True
                state.buffered_voice_data = voice_data
                await self._handle_paired_buffered_voice(state)
                return

        original_text = text
        prepared = (
            await self.adapter.live2d_manager.prepare_reply(original_text)
            if original_text
            else PreparedReplyResult("", False, "", None)
        )
        text = prepared.reply
        if not text and not voice_data:
            self.logger.warning("Outgoing reply suppressed after Live2D preparation")
            return

        if comment_target:
            await self.adapter.live2d_manager.apply_control(prepared.control_id)
            if voice_data:
                await self._play_core_voice(
                    voice_data,
                    idle=False,
                    control_id=prepared.control_id,
                )
            if text:
                await self.adapter.comment_handler.send_comment_reply_from_context(
                    comment_target, text
                )
            return

        if room_id is not None:
            reply_dmid = _find_reply_id(seg)
            reply_mid = ""
            if reply_dmid:
                reply_mid = self._lookup_reply_mid(room_id, reply_dmid)
            await self._handle_live_reply(
                {
                    "message": original_text,
                    "room_id": room_id,
                    "reply_mid": reply_mid or "",
                    "reply_dmid": reply_dmid or "",
                    "voice": voice_data,
                },
                prepared=prepared,
            )
            return

        if private_target:
            await self.adapter.live2d_manager.apply_control(prepared.control_id)
            if voice_data:
                await self._play_core_voice(voice_data, idle=False)
            if text:
                await self.adapter.private_handler.send_private_message(
                    private_target, text
                )
            return

        self.logger.warning("Missing room_id for outgoing danmu")

    async def _handle_voice_stream_event(
        self, payload: Any, *, room_id: int | None = None
    ) -> None:
        player = getattr(self.adapter, "audio_player", None)
        if player is None:
            self.logger.warning("Core voice_stream has no Bilibili audio player")
            return
        event = payload.get("event") if isinstance(payload, dict) else None
        parent_message_id = (
            payload.get("parent_message_id")
            if isinstance(payload, dict)
            else None
        )
        stream_id = payload.get("stream_id") if isinstance(payload, dict) else None
        state = (
            self._reply_states.get(parent_message_id)
            if isinstance(parent_message_id, str) and parent_message_id
            else None
        )
        if state is not None:
            if room_id is None or room_id != state.room_id:
                return
            async with state.event_lock:
                await state.ready.wait()
                if self._shutting_down or state.evicted:
                    return
                await self._process_correlated_stream_event(
                    state, payload, event, stream_id, player
                )
            return

        controller = getattr(
            getattr(self.adapter, "live2d_manager", None),
            "controller",
            None,
        )
        accepted = await player.handle_voice_stream_event(payload)
        if not accepted:
            self.logger.warning("Bilibili could not play Core voice_stream event")
        if event == "start":
            if accepted:
                self._supersede_other_active_replies(None)
            if accepted and controller is not None:
                await controller.on_start_replying()
            elif not accepted and controller is not None:
                await controller.on_reply_finished()

    async def _process_correlated_stream_event(
        self,
        state: _PendingLiveReply,
        payload: Any,
        event: Any,
        stream_id: Any,
        player: Any,
    ) -> None:
        if state.status in {"fallback", "failed", "superseded"}:
            return
        if not isinstance(payload, dict) or not isinstance(stream_id, str) or not stream_id:
            accepted = await player.handle_voice_stream_event(payload)
            if not accepted:
                self.logger.warning("Bilibili could not play Core voice_stream event")
            return

        stream_id = stream_id.strip()
        if event == "start":
            if stream_id in state.stream_states:
                return
            while len(state.stream_states) >= self.MAX_STREAMS_PER_REPLY:
                state.stream_states.popitem(last=False)
            state.stream_states[stream_id] = "starting"
        elif state.stream_states.get(stream_id) not in {"starting", "active"}:
            # Late/replayed chunks must not abort AudioPlayer's unrelated stream.
            return

        accepted = await player.handle_voice_stream_event(payload)
        if not accepted:
            self.logger.warning("Bilibili could not play Core voice_stream event")

        if event == "start":
            if accepted:
                self._supersede_other_active_replies(state)
                previous = state.active_stream_id
                if previous and previous != stream_id:
                    state.stream_states[previous] = "superseded"
                state.active_stream_id = stream_id
                state.active_stream_metadata = {
                    key: payload[key]
                    for key in (
                        "stream_id",
                        "parent_message_id",
                        "sample_rate",
                        "channels",
                        "sample_width",
                        "codec",
                    )
                    if key in payload
                }
                state.stream_states[stream_id] = "active"
                if not state.audio_succeeded:
                    state.status = "streaming"
                self._arm_reply_timer(
                    state, self.STREAM_INACTIVITY_TIMEOUT_SECONDS
                )
                await self._apply_stream_control(state)
            else:
                state.stream_states[stream_id] = "rejected"
                if state.active_stream_id == stream_id:
                    state.active_stream_id = ""
                    state.active_stream_metadata = None
            return

        if event == "chunk":
            was_active = state.active_stream_id == stream_id
            if not accepted:
                state.stream_states[stream_id] = "failed"
                if was_active:
                    await self._stop_active_stream(state)
                if not state.audio_succeeded and not state.buffer_claimed:
                    await self._fallback_pending_reply(state)
                elif state.audio_succeeded and was_active:
                    self._cancel_reply_timer(state)
            elif was_active:
                self._arm_reply_timer(state, self.STREAM_INACTIVITY_TIMEOUT_SECONDS)
            return

        if event == "end":
            state.stream_states[stream_id] = "ended" if accepted else "failed"
            was_active = state.active_stream_id == stream_id
            if was_active:
                if accepted:
                    state.active_stream_id = ""
                    state.active_stream_metadata = None
                else:
                    await self._stop_active_stream(state)
            if accepted:
                state.audio_succeeded = True
                state.status = "success"
                state.finished_at = time.monotonic()
                self._cancel_reply_timer(state)
            elif not state.audio_succeeded and not state.buffer_claimed:
                await self._fallback_pending_reply(state)
            elif state.audio_succeeded and was_active:
                self._cancel_reply_timer(state)
            return

        if event == "abort":
            was_active = state.active_stream_id == stream_id
            if was_active:
                state.stream_states[stream_id] = "aborted" if accepted else "failed"
                if accepted:
                    state.active_stream_id = ""
                    state.active_stream_metadata = None
                else:
                    await self._stop_active_stream(state)
            # A failed start can be followed by Core's existing buffered-voice
            # fallback. Wait for that same-room segment or the bounded timeout.
            if was_active and not state.audio_succeeded and not state.buffer_claimed:
                await self._fallback_pending_reply(state)
            elif was_active and state.audio_succeeded:
                self._cancel_reply_timer(state)

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
        cache = self.adapter._danmu_cache.get(room_id) or {}
        return str(cache.get(reply_dmid) or "")

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
            await self.adapter.private_handler.handle_private_send(args, message)
            return
        self.logger.warning(f"Unknown command: {command_name}")

    async def _handle_comment_reply(self, args: Dict[str, Any]) -> None:
        raw_msg = str(args.get("message") or "")
        prepared = await self.adapter.live2d_manager.prepare_reply(raw_msg)
        text = prepared.reply
        text = _strip_emoji(text).strip()

        await self.adapter.live2d_manager.apply_control(prepared.control_id)

        if not text:
            self.logger.warning("Empty comment reply text")
            return
        text = self.adapter._filter_outgoing_text(text)
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
        target = (comment_type, oid, root_id, parent_id)
        await self.adapter.comment_handler.send_comment_reply_from_context(target, text)

    async def _handle_live_reply(
        self,
        args: Dict[str, Any],
        *,
        prepared: PreparedReplyResult | None = None,
    ) -> None:
        raw_message = str(args.get("message") or "")
        first_pass_tts_language = self._explicit_tts_language(raw_message)
        try:
            room_id = int(args.get("room_id"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid room_id for live reply")
            return
        reply_mid = str(args.get("reply_mid") or "")
        reply_dmid = str(args.get("reply_dmid") or "")
        if prepared is None:
            prepared = await self.adapter.live2d_manager.prepare_reply(raw_message)

        voice_data = str(args.get("voice") or "").strip()

        # A Core-produced voice segment is already the final reply artifact;
        # do not let the text-only live-search orchestrator discard it.
        if voice_data:
            await self._deliver_live_reply(
                prepared,
                room_id,
                reply_mid,
                reply_dmid,
                voice_data=voice_data,
            )
            return

        handled = await self.live_search.handle(
            prepared,
            room_id=room_id,
            reply_mid=reply_mid,
            reply_dmid=reply_dmid,
            deliver=self._deliver_live_reply,
            first_pass_tts_language=first_pass_tts_language,
        )
        if handled:
            return

        await self._deliver_live_reply(
            prepared,
            room_id,
            reply_mid,
            reply_dmid,
            voice_data=voice_data,
        )

    async def _deliver_live_reply(
        self,
        prepared: PreparedReplyResult,
        room_id: int,
        reply_mid: str,
        reply_dmid: str,
        voice_data: str = "",
        *,
        control_already_applied: bool = False,
        update_subtitle: bool = True,
    ) -> bool:
        """Deliver one already-orchestrated live reply through TTS/Live2D/danmu."""

        if getattr(self, "_shutting_down", False):
            return False
        text = self._clean_display_text(prepared.reply)
        if isinstance(prepared, SearchFollowupResult) and not voice_data:
            if prepared.voice_data and self._room_tts_enabled(room_id):
                voice_data = prepared.voice_data
        if text and update_subtitle:
            self._write_live_subtitle(room_id, text)

        delivery_state = {
            "start_notified": control_already_applied,
            "control_applied": control_already_applied,
        }

        # Prebuilt audio (for example a requested song or another platform
        # media result) is independently deliverable.  It must not be gated
        # on an adjacent text/tts_text field, especially in potato mode where
        # Core deliberately preserves existing media without synthesizing.
        if voice_data:
            try:
                played = await self._play_core_voice(
                    voice_data,
                    idle=False,
                    control_id=prepared.control_id,
                    lifecycle_state=delivery_state,
                )
            except Exception as exc:
                self.logger.warning(
                    "Core voice playback failed: error_type={}", type(exc).__name__
                )
                played = False
            if played:
                return True
            self.logger.warning("Core voice payload was invalid; falling back to text")

        text = self.adapter._filter_outgoing_text(text)
        if not text:
            return False

        if self.adapter.live2d_manager.controller:
            try:
                if not delivery_state["start_notified"]:
                    delivery_state["start_notified"] = True
                    await self.adapter.live2d_manager.controller.on_start_replying()
                if not delivery_state["control_applied"]:
                    delivery_state["control_applied"] = True
                    await self.adapter.live2d_manager.apply_control(prepared.control_id)
            except Exception as exc:
                self.logger.error(
                    "Live2D reply hook error: error_type={}",
                    type(exc).__name__,
                )

        delivered = await self._send_danmu(
            room_id, text, reply_mid or None, reply_dmid or None
        )

        if self.adapter.live2d_manager.controller:
            try:
                await self.adapter.live2d_manager.controller.on_reply_finished()
            except Exception as exc:
                self.logger.error(
                    "Live2D reply hook error: error_type={}",
                    type(exc).__name__,
                )
        return delivered

    async def _play_core_voice(
        self,
        voice_data: str,
        *,
        idle: bool,
        control_id: str | None = None,
        control_already_applied: bool = False,
        start_already_notified: bool = False,
        lifecycle_state: dict[str, bool] | None = None,
    ) -> bool:
        """Decode Core audio and hand final playback to the platform layer."""
        raw = str(voice_data or "").strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            audio_data = base64.b64decode(raw, validate=True)
        except Exception:
            return False
        if not audio_data or len(audio_data) > 16 * 1024 * 1024:
            return False
        self.adapter.audio_player.interrupt_idle()
        controller = self.adapter.live2d_manager.controller
        if controller:
            try:
                if not start_already_notified and not (
                    lifecycle_state and lifecycle_state.get("start_notified")
                ):
                    if lifecycle_state is not None:
                        lifecycle_state["start_notified"] = True
                    await controller.on_start_replying()
                if not control_already_applied and not (
                    lifecycle_state and lifecycle_state.get("control_applied")
                ):
                    if lifecycle_state is not None:
                        lifecycle_state["control_applied"] = True
                    await self.adapter.live2d_manager.apply_control(control_id)
            except Exception as exc:
                self.logger.error(
                    "Live2D reply hook error: error_type={}",
                    type(exc).__name__,
                )
        if idle:
            await asyncio.to_thread(self.adapter.audio_player.play_idle, audio_data)
        else:
            self.adapter.audio_player.play(audio_data)
        return True

    async def _send_danmu(
        self,
        room_id: int,
        text: str,
        reply_mid: Optional[str],
        reply_dmid: Optional[str],
    ) -> bool:
        text = self.adapter._filter_outgoing_text(text)

        # An adapter-side TTS toggle no longer changes the delivery type.
        # Ordinary Core text must always obey Bilibili's danmu limit; only an
        # actual returned voice field is played as audio.
        segments = _split_bilibili_text(
            text,
            max_length=BILIBILI_DANMU_MAX_LENGTH,
        )
        if not segments:
            self.logger.warning("Empty danmu after splitting")
            return False
        self.logger.info(
            "Send danmu: room_id={} reply_mid={} reply_dmid={} chars={} segments={}",
            room_id,
            reply_mid or "",
            reply_dmid or "",
            len(text),
            len(segments),
        )
        sent = False
        for idx, segment in enumerate(segments):
            segment_reply_mid = reply_mid if idx == 0 else None
            segment_reply_dmid = reply_dmid if idx == 0 else None
            try:
                resp = await self.adapter.api.send_danmu(
                    room_id=room_id,
                    message=segment,
                    reply_mid=segment_reply_mid or None,
                    reply_dmid=segment_reply_dmid or None,
                )
                response_code = resp.get("code") if isinstance(resp, dict) else None
                if response_code != 0:
                    self.logger.warning("Danmu send failed: code={}", response_code)
                else:
                    sent = True
                    dmid = None
                    data = (resp or {}).get("data", {})
                    if isinstance(data, dict):
                        dmid = data.get("dmid") or data.get("dmid_str")
                    self._remember_self_danmu(
                        room_id, str(dmid) if dmid else "", segment
                    )
                    self.logger.info("Danmu send ok")
            except Exception as exc:
                self.logger.error(
                    "Danmu send error: error_type={}",
                    type(exc).__name__,
                )
            if idx < len(segments) - 1:
                await asyncio.sleep(BILIBILI_DANMU_SEND_DELAY_SECONDS)
        return sent

    def _remember_self_danmu(self, room_id: int, message_id: str, text: str) -> None:
        now = time.time()
        if message_id:
            room_cache = self.adapter._self_danmu_ids.setdefault(room_id, {})
            room_cache[message_id] = now
            if len(room_cache) > 500:
                for msg_id, ts in list(room_cache.items()):
                    if now - ts > 30:
                        room_cache.pop(msg_id, None)
        room_texts = self.adapter._self_danmu_texts.setdefault(room_id, [])
        if text:
            room_texts.append((text, now))
        if len(room_texts) > 200:
            self.adapter._self_danmu_texts[room_id] = [
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
        room_cache = self.adapter._self_danmu_ids.get(room_id, {})
        if message_id and message_id in room_cache:
            return True
        room_texts = self.adapter._self_danmu_texts.get(room_id, [])
        if text:
            window = 2.5 if len(text.strip()) <= 2 else 6.0
            for sent_text, ts in list(room_texts):
                if now - ts > 30:
                    continue
                if sent_text == text and (now - ts) <= window:
                    return True
        return False
