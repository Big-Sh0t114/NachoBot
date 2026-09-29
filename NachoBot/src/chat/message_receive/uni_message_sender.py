import asyncio
import base64
import time
import traceback
import uuid
from collections.abc import Mapping

from rich.traceback import install
from ncnk_message import BaseMessageInfo, MessageBase, Seg

from src.common.message.api import get_global_api
from src.common.logger import get_logger
from src.chat.message_receive.message import MessageSending
from src.chat.message_receive.storage import MessageStorage
from src.chat.utils.utils import truncate_message
from src.chat.utils.utils import calculate_typing_time
from src.config.config import global_config
from src.chat.runtime_capabilities import (
    additional_config_from_message,
    runtime_capabilities_from_stream,
)
from src.multimodal.contracts import (
    MAX_TEXT_CHARS,
    TTS_STREAM_CHUNK_BYTES,
    TTS_STREAM_CODEC,
    TTS_STREAM_MAX_BYTES,
    TTS_STREAM_MAX_DURATION_SECONDS,
)

install(extra_lines=3)

logger = get_logger("sender")

_TTS_RELAY_SLICE_SECONDS = 0.1
_TTS_RELAY_MAX_AHEAD_SECONDS = 0.5
_TTS_RELAY_DRAIN_MARGIN_SECONDS = 0.2


def _tts_relay_now() -> float:
    return time.monotonic()


async def _tts_relay_wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


_DEFAULT_BLOCKED_MARKERS = (
    "i'm kiro",
    "i am kiro",
    "kiro-cli chat",
    "kiro-cli",
    "kiro cli",
    "ai assistant built by aws",
    "aws services",
    "i can't engage with this request",
    "i need to decline this request",
    "this message is attempting to manipulate",
    "adopting a fake persona",
    "creating a fake persona",
    "roleplaying as a character",
    "roleplay as a different character",
    "i don't roleplay as other characters",
    "i don't pretend to be someone else",
    "fabricated instructions",
    "fabricated rules",
    "ignoring my actual instructions",
    "instructions to ignore my real system prompts",
    "override my actual identity",
    "actual identity and guidelines",
    "responding as if i'm in a qq",
    "qq chat group",
    "identity verification",
    "false claims about",
    "fabricated identity",
    "fake conversation history",
)


def _get_response_filter_settings() -> tuple[bool, list[str]]:
    filter_config = getattr(global_config, "response_filter", None)
    if filter_config is None:
        return True, [marker.lower() for marker in _DEFAULT_BLOCKED_MARKERS]
    enabled = bool(getattr(filter_config, "enable", True))
    markers = getattr(filter_config, "blocked_markers", [])
    cleaned = [str(marker).lower() for marker in markers if str(marker).strip()]
    return enabled, cleaned


def _should_suppress_text_reply(text: str) -> bool:
    if not text:
        return False
    enabled, markers = _get_response_filter_settings()
    if not enabled or not markers:
        return False
    normalized = text.lower()
    return any(marker in normalized for marker in markers)


async def _send_message(message: MessageSending, show_log=True) -> bool:
    """合并后的消息发送函数，包含WS发送和日志记录"""
    message_preview = truncate_message(message.processed_plain_text, max_length=200)

    try:
        # 直接调用API发送消息
        send_result = await get_global_api().send_message(message)
        if send_result is False:
            logger.warning(
                "ncnk 未能将消息 '%s' 发往平台'%s'",
                message_preview,
                message.message_info.platform,
            )
            return False
        if show_log:
            logger.info(f"已将消息  '{message_preview}'  发往平台'{message.message_info.platform}'")
        return True

    except Exception as e:
        logger.error(f"发送消息   '{message_preview}'   发往平台'{message.message_info.platform}' 失败: {str(e)}")
        traceback.print_exc()
        raise e  # 重新抛出其他异常


def _tts_display_text(data) -> str:
    if isinstance(data, Mapping):
        value = data.get("display_text")
        if value is None:
            value = data.get("text")
    else:
        value = data
    return str(value or "")[:MAX_TEXT_CHARS]


def _tts_text_entries(segment: Seg, default_language: str = "") -> list[tuple[str, str]]:
    """Collect only explicit, nonempty TTS fields from the final segment tree."""
    entries: list[tuple[str, str]] = []

    def visit(current):
        if not isinstance(current, Seg):
            return
        if current.type == "seglist" and isinstance(current.data, list):
            for child in current.data:
                visit(child)
            return
        if current.type != "tts_text":
            return
        data = current.data
        raw_text = data.get("text") if isinstance(data, Mapping) else data
        text = str(raw_text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            return
        raw_language = data.get("lang") if isinstance(data, Mapping) else None
        language = str(raw_language or default_language or "").strip()[:32]
        entries.append((text, language))

    visit(segment)
    return entries


def _tts_fields_as_display_text(segment: Seg) -> Seg:
    """Project explicit TTS fields to their visible text for the platform."""
    if segment.type == "seglist" and isinstance(segment.data, list):
        return Seg(
            type="seglist",
            data=[
                _tts_fields_as_display_text(child) if isinstance(child, Seg) else child
                for child in segment.data
            ],
        )
    if segment.type == "tts_text":
        return Seg(type="text", data=_tts_display_text(segment.data))
    return segment


def _router_allows_tts(router) -> bool:
    profile = getattr(router, "profile", None)
    allows_tts = getattr(profile, "allows_tts", None)
    return allows_tts is not False


def _tts_default_language(message: MessageSending, capabilities) -> str:
    trigger_message = getattr(
        getattr(message.chat_stream, "context", None),
        "message",
        None,
    )
    for candidate in (message, trigger_message):
        value = additional_config_from_message(candidate).get("tts_language")
        if isinstance(value, str) and value.strip():
            return value.strip()[:32]
    language = getattr(capabilities, "tts_language", "")
    return str(language or "").strip()[:32]


def _stream_chunk_parts(chunk):
    """Read the agreed TTSStreamChunk contract and validate bounded PCM data."""
    pcm = getattr(chunk, "pcm_s16le", None)
    if pcm is None:
        # Keep the sender tolerant of equivalent typed chunk wrappers that
        # expose the public ``data`` and format fields directly.
        pcm = getattr(chunk, "data", None)
    if not isinstance(pcm, bytes) or not pcm:
        raise ValueError("TTS stream chunk must contain nonempty PCM bytes")

    spec = getattr(chunk, "spec", chunk)
    sample_rate = getattr(spec, "sample_rate", None)
    channels = getattr(spec, "channels", None)
    sample_width = getattr(spec, "sample_width", None)
    codec = getattr(spec, "codec", None)
    numeric_fields = (sample_rate, channels, sample_width)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric_fields):
        raise ValueError("TTS stream format fields must be integers")
    if not 8_000 <= sample_rate <= 192_000 or not 1 <= channels <= 2:
        raise ValueError("TTS stream sample rate or channel count is out of range")
    if sample_width != 2 or str(codec or "").strip().lower() != TTS_STREAM_CODEC:
        raise ValueError("TTS stream codec must be PCM signed 16-bit little-endian")
    if len(pcm) > TTS_STREAM_CHUNK_BYTES:
        raise ValueError("TTS stream chunk exceeds the byte limit")
    frame_bytes = channels * sample_width
    if len(pcm) % frame_bytes:
        raise ValueError("TTS stream chunk does not end on a complete PCM frame")
    spec_tuple = (sample_rate, channels, sample_width, TTS_STREAM_CODEC)
    return pcm, spec_tuple


def _stream_event_data(
    event: str,
    stream_id: str,
    parent_message_id: str,
    spec: tuple[int, int, int, str],
    *,
    seq: int | None = None,
    pcm: bytes | None = None,
) -> dict:
    sample_rate, channels, sample_width, codec = spec
    data = {
        "event": event,
        "stream_id": stream_id,
        "parent_message_id": parent_message_id,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "codec": codec,
    }
    if seq is not None:
        data["seq"] = seq
    if pcm is not None:
        data["audio_base64"] = base64.b64encode(pcm).decode("ascii")
    return data


async def _send_direct_segment(message: MessageSending, segment: Seg) -> bool:
    """Send a relay segment through ncnk without rerunning reply hooks/storage."""
    info = message.message_info
    direct_message = MessageBase(
        message_info=BaseMessageInfo(
            platform=info.platform,
            message_id=uuid.uuid4().hex,
            time=info.time,
            group_info=info.group_info,
            user_info=info.user_info,
        ),
        message_segment=segment,
    )
    return (await get_global_api().send_message(direct_message)) is not False


async def _close_tts_iterator(iterator) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Closing TTS stream iterator failed (%s)", type(exc).__name__)


async def _send_buffered_tts_fallback(
    message: MessageSending,
    router,
    text: str,
    text_lang: str | None,
) -> bool:
    """Use the existing buffered router path only before streaming audio starts."""
    try:
        result = await router.synthesize_tts(
            text,
            platform=str(getattr(message.message_info, "platform", "core") or "core"),
            text_lang=text_lang,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Buffered TTS fallback failed (%s)", type(exc).__name__)
        return False
    audio_base64 = getattr(result, "audio_base64", None)
    if not isinstance(audio_base64, str) or not audio_base64.strip():
        return True
    return await _send_direct_segment(message, Seg(type="voice", data=audio_base64))


async def _best_effort_abort(
    message: MessageSending,
    stream_id: str,
    parent_message_id: str,
    spec: tuple[int, int, int, str],
) -> None:
    try:
        await _send_direct_segment(
            message,
            Seg(
                type="voice_stream",
                data=_stream_event_data("abort", stream_id, parent_message_id, spec),
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Best-effort voice stream abort failed (%s)", type(exc).__name__)


async def _relay_tts_text(
    message: MessageSending,
    router,
    text: str,
    text_lang: str | None,
) -> bool:
    """Relay one explicit TTS field, with a buffered fallback before audio."""
    iterator = None
    stream_id = uuid.uuid4().hex
    parent_message_id = str(getattr(message.message_info, "message_id", "") or "")
    started = False
    start_attempted = False
    emitted_chunk = False
    chosen_spec: tuple[int, int, int, str] | None = None
    next_seq = 0
    playback_until: float | None = None
    pending_pcm = bytearray()

    try:
        stream = router.synthesize_tts_stream(
            text,
            platform=str(getattr(message.message_info, "platform", "core") or "core"),
            text_lang=text_lang,
        )
        iterator = stream.__aiter__()
        try:
            first_chunk = await iterator.__anext__()
            first_pcm, chosen_spec = _stream_chunk_parts(first_chunk)
        except StopAsyncIteration:
            await _close_tts_iterator(iterator)
            return await _send_buffered_tts_fallback(message, router, text, text_lang)
        except asyncio.CancelledError:
            await _close_tts_iterator(iterator)
            raise
        except Exception as exc:
            await _close_tts_iterator(iterator)
            if getattr(exc, "fallback_allowed", True):
                logger.info(
                    "TTS streaming unavailable before audio (%s); using buffered TTS",
                    type(exc).__name__,
                )
                return await _send_buffered_tts_fallback(message, router, text, text_lang)
            else:
                logger.warning("TTS stream failed before relay (%s)", type(exc).__name__)
                return False

        first_duration = len(first_pcm) / (chosen_spec[0] * chosen_spec[1] * chosen_spec[2])
        if len(first_pcm) > TTS_STREAM_MAX_BYTES or first_duration > TTS_STREAM_MAX_DURATION_SECONDS:
            await _close_tts_iterator(iterator)
            return await _send_buffered_tts_fallback(message, router, text, text_lang)

        try:
            start_attempted = True
            start_sent = await _send_direct_segment(
                message,
                Seg(
                    type="voice_stream",
                    data=_stream_event_data(
                        "start", stream_id, parent_message_id, chosen_spec
                    ),
                ),
            )
        except asyncio.CancelledError:
            await _close_tts_iterator(iterator)
            raise
        except Exception as exc:
            await _close_tts_iterator(iterator)
            logger.info("TTS stream start send failed (%s); using buffered TTS", type(exc).__name__)
            await _best_effort_abort(message, stream_id, parent_message_id, chosen_spec)
            return await _send_buffered_tts_fallback(message, router, text, text_lang)
        if not start_sent:
            await _close_tts_iterator(iterator)
            return await _send_buffered_tts_fallback(message, router, text, text_lang)
        started = True

        async def send_chunk(
            pcm: bytes,
            spec: tuple[int, int, int, str],
            *,
            flush: bool = False,
        ) -> bool:
            nonlocal next_seq, emitted_chunk, playback_until
            sample_rate, channels, sample_width, _ = spec
            frame_bytes = channels * sample_width
            slice_bytes = min(
                TTS_STREAM_CHUNK_BYTES // frame_bytes,
                max(1, int(sample_rate * _TTS_RELAY_SLICE_SECONDS)),
            ) * frame_bytes
            pending_pcm.extend(pcm)
            while len(pending_pcm) >= slice_bytes or (flush and pending_pcm):
                piece = bytes(pending_pcm[:slice_bytes])
                del pending_pcm[: len(piece)]
                duration = len(piece) / (sample_rate * frame_bytes)
                now = _tts_relay_now()
                if playback_until is not None:
                    excess = playback_until - now + duration - _TTS_RELAY_MAX_AHEAD_SECONDS
                    if excess > 0:
                        await _tts_relay_wait(excess)
                sent = await _send_direct_segment(
                    message,
                    Seg(
                        type="voice_stream",
                        data=_stream_event_data(
                            "chunk",
                            stream_id,
                            parent_message_id,
                            spec,
                            seq=next_seq,
                            pcm=piece,
                        ),
                    ),
                )
                if not sent:
                    return False
                now = _tts_relay_now()
                playback_until = max(playback_until or now, now) + duration
                emitted_chunk = True
                next_seq += 1
            return True

        if not await send_chunk(first_pcm, chosen_spec):
            raise RuntimeError("ncnk rejected a voice stream chunk")

        total_bytes = len(first_pcm)
        chunk_count = 1
        async for chunk in iterator:
            pcm, spec = _stream_chunk_parts(chunk)
            if spec != chosen_spec:
                raise ValueError("TTS stream format changed during the response")
            total_bytes += len(pcm)
            chunk_count += 1
            duration = total_bytes / (spec[0] * spec[1] * spec[2])
            if (
                len(pcm) > TTS_STREAM_CHUNK_BYTES
                or total_bytes > TTS_STREAM_MAX_BYTES
                or duration > TTS_STREAM_MAX_DURATION_SECONDS
                or chunk_count > 4096
            ):
                raise ValueError("TTS stream exceeded its configured bounds")
            if not await send_chunk(pcm, spec):
                raise RuntimeError("ncnk rejected a voice stream chunk")

        if not await send_chunk(b"", chosen_spec, flush=True):
            raise RuntimeError("ncnk rejected the final voice stream chunk")

        end_sent = await _send_direct_segment(
            message,
            Seg(
                type="voice_stream",
                data=_stream_event_data(
                    "end", stream_id, parent_message_id, chosen_spec
                ),
            ),
        )
        if not end_sent:
            raise RuntimeError("ncnk rejected the voice stream end event")
        # End marks the producer finished; a consumer may still have PCM in its
        # playback queue. Keep consecutive TTS fields in audible order.
        if playback_until is not None:
            remaining = playback_until - _tts_relay_now()
            await _tts_relay_wait(max(0.0, remaining) + _TTS_RELAY_DRAIN_MARGIN_SECONDS)
        started = False
        return True
    except asyncio.CancelledError:
        if iterator is not None:
            try:
                await _close_tts_iterator(iterator)
            except asyncio.CancelledError:
                pass
        if (started or start_attempted) and chosen_spec is not None:
            try:
                await asyncio.shield(
                    _best_effort_abort(message, stream_id, parent_message_id, chosen_spec)
                )
            except asyncio.CancelledError:
                pass
        raise
    except Exception as exc:
        if iterator is not None:
            await _close_tts_iterator(iterator)
        if started and chosen_spec is not None:
            logger.warning("TTS stream relay failed after start (%s)", type(exc).__name__)
            await _best_effort_abort(message, stream_id, parent_message_id, chosen_spec)
            return False
        elif not emitted_chunk:
            logger.info("TTS stream unavailable before audio (%s)", type(exc).__name__)
            return await _send_buffered_tts_fallback(message, router, text, text_lang)
        return False


class UniversalMessageSender:
    """管理消息的注册、即时处理、发送和存储，并跟踪思考状态。"""

    def __init__(self):
        self.storage = MessageStorage()

    async def send_message(
        self, message: MessageSending, typing=False, set_reply=False, storage_message=True, show_log=True
    ):
        """
        处理、发送并存储一条消息。

        参数：
            message: MessageSending 对象，待发送的消息。
            typing: 是否模拟打字等待。

        用法：
            - typing=True 时，发送前会有打字等待。
        """
        if not message.chat_stream:
            logger.error("消息缺少 chat_stream，无法发送")
            raise ValueError("消息缺少 chat_stream，无法发送")
        if not message.message_info or not message.message_info.message_id:
            logger.error("消息缺少 message_info 或 message_id，无法发送")
            raise ValueError("消息缺少 message_info 或 message_id，无法发送")

        chat_id = message.chat_stream.stream_id
        message_id = message.message_info.message_id

        try:
            if set_reply:
                message.build_reply()
                logger.debug(f"[{chat_id}] 选择回复引用消息: {message.processed_plain_text[:20]}...")

            from src.plugin_system.core.events_manager import events_manager
            from src.plugin_system.base.component_types import EventType

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.POST_SEND_PRE_PROCESS, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送被插件取消: {str(message.message_segment)[:100]}...")
                return False
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    logger.warning(f"[{chat_id}] 插件修改了消息的纯文本内容，可能导致此内容被覆盖。")
                    message.processed_plain_text = modified_message.plain_text

            await message.process()

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.POST_SEND, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送被插件取消: {str(message.message_segment)[:100]}...")
                return False
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    message.processed_plain_text = modified_message.plain_text

            if _should_suppress_text_reply(message.processed_plain_text or ""):
                logger.warning(f"[{chat_id}] 过滤前信息: {message.processed_plain_text}")
                logger.error(f"[{chat_id}] 检测到可疑回复模板，已替换为 Filtered")
                filtered_segment = Seg(type="text", data="Filtered")
                if message.reply and getattr(message.reply.message_info, "message_id", None):
                    message.message_segment = Seg(
                        type="seglist",
                        data=[Seg(type="reply", data=message.reply.message_info.message_id), filtered_segment],  # type: ignore
                    )
                else:
                    message.message_segment = filtered_segment
                message.processed_plain_text = "Filtered"
                message.display_message = "Filtered"

            if typing:
                typing_time = calculate_typing_time(
                    input_string=message.processed_plain_text,
                    thinking_start_time=message.thinking_start_time,
                    is_emoji=message.is_emoji,
                )
                await asyncio.sleep(typing_time)

            capabilities = runtime_capabilities_from_stream(message.chat_stream)
            voice_stream = capabilities.voice_stream
            tts_entries = (
                _tts_text_entries(
                    message.message_segment,
                    default_language=_tts_default_language(message, capabilities),
                )
                if voice_stream
                else []
            )

            # Plugins and storage retain the original coherent reply, including
            # explicit TTS fields. Adapters receive the corresponding display
            # text in the one logical message before any audio relay begins.
            original_segment = message.message_segment
            if voice_stream:
                message.message_segment = _tts_fields_as_display_text(original_segment)
            try:
                sent_msg = await _send_message(message, show_log=show_log)
            finally:
                message.message_segment = original_segment
            if not sent_msg:
                return False

            if voice_stream and tts_entries:
                from src.multimodal import get_multimodal_router

                router = get_multimodal_router()
                if _router_allows_tts(router):
                    for text, text_lang in tts_entries:
                        if not await _relay_tts_text(
                            message,
                            router,
                            text,
                            text_lang or None,
                        ):
                            break

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.AFTER_SEND, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送后续处理被插件取消: {str(message.message_segment)[:100]}...")
                return True
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    message.processed_plain_text = modified_message.plain_text

            if storage_message:
                await self.storage.store_message(message, message.chat_stream)

            return sent_msg

        except Exception as e:
            logger.error(f"[{chat_id}] 处理或存储消息 {message_id} 时出错: {e}")
            raise e
