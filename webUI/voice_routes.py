"""Authenticated HTTP routes for browser-owned WebUI voice calls."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
import wave
from typing import Any
from urllib.request import Request, urlopen

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictInt

try:
    from tts_manager import TTSGenerationError, TTSManager, TTSUnavailableError, _get_core_auth_token, _get_core_base_url
    from chat_backend import ChatBackendError
    from voice_calls import (
        CALL_LEASE_SECONDS,
        MAX_MESSAGE_CHARS,
        VoiceCallError,
        VoiceCallStore,
        canonical_core_user_id,
        validate_conversation_id,
    )
except ImportError:  # pragma: no cover - package import context
    from .tts_manager import TTSGenerationError, TTSManager, TTSUnavailableError, _get_core_auth_token, _get_core_base_url
    from .chat_backend import ChatBackendError
    from .voice_calls import (
        CALL_LEASE_SECONDS,
        MAX_MESSAGE_CHARS,
        VoiceCallError,
        VoiceCallStore,
        canonical_core_user_id,
        validate_conversation_id,
    )


MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BASE64_CHARS = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_AUDIO_SECONDS = 45
CORE_REQUEST_TIMEOUT_SECONDS = 35.0
ASR_STREAM_SAMPLE_RATE = 16_000
ASR_STREAM_CHANNELS = 1
ASR_STREAM_CHUNK_MAX_BYTES = 64 * 1024
ASR_STREAM_CHUNK_MAX_BASE64_CHARS = 4 * ((ASR_STREAM_CHUNK_MAX_BYTES + 2) // 3)
ASR_STREAM_MAX_DURATION_SECONDS = 60
ASR_STREAM_MAX_PCM_BYTES = ASR_STREAM_SAMPLE_RATE * ASR_STREAM_CHANNELS * 2 * ASR_STREAM_MAX_DURATION_SECONDS
ASR_STREAM_MAX_CHUNKS = 4096
ASR_STREAM_MAX_CONCURRENCY = 8
ASR_STREAM_IDLE_TIMEOUT_SECONDS = 15
ASR_STREAM_REQUEST_TIMEOUT_SECONDS = 10.0
ASR_STREAM_MAX_RESPONSE_BYTES = 64 * 1024
TTS_STREAM_MAX_PCM_BYTES = 16 * 1024 * 1024
_CONTROL_EMOTIONS = ("normal", "shy", "disgust", "angry")
_CONTROL_ACTIONS = (
    "待机/放松",
    "点头/同意",
    "摇头/否定",
    "转身向左/看左边",
    "转身向右/看右边",
    "眨眼/卖萌/Wink",
    "身体晃动/开心/兴奋",
    "歪头/疑惑/思考",
    "害羞/移开视线/不好意思",
    "一般",
)


class CreateCallRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=96)
    user_name: str = Field(default="WebUI", max_length=128)
    model_id: str | None = Field(default=None, max_length=128)


class VoiceMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    request_message_id: str = Field(min_length=1, max_length=128)
    user_name: str = Field(default="WebUI", max_length=128)
    generation: int = Field(ge=0, le=2**31 - 1)


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BASE64_CHARS)
    generation: int = Field(ge=0, le=2**31 - 1)


class GenerationRequest(BaseModel):
    generation: int = Field(ge=0, le=2**31 - 1)


class AudioStreamStartRequest(BaseModel):
    generation: StrictInt = Field(ge=0, le=2**31 - 1)


class AudioStreamChunkRequest(BaseModel):
    generation: StrictInt = Field(ge=0, le=2**31 - 1)
    seq: StrictInt = Field(ge=0, lt=ASR_STREAM_MAX_CHUNKS)
    pcm_base64: str = Field(min_length=1, max_length=ASR_STREAM_CHUNK_MAX_BASE64_CHARS)


class AudioStreamFinishRequest(BaseModel):
    generation: StrictInt = Field(ge=0, le=2**31 - 1)


class AudioStreamAbortRequest(BaseModel):
    generation: StrictInt = Field(ge=0, le=2**31 - 1)


class TTSRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31 - 1)


class ControlRequest(BaseModel):
    control_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31 - 1)


class PlaybackRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31 - 1)
    status: str = Field(pattern="^(played|interrupted)$")


@dataclass
class _AudioStreamSession:
    call_id: str
    generation: int
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    stream_id: str | None = None
    next_seq: int = 0
    bytes_received: int = 0
    chunk_digests: dict[int, bytes] = field(default_factory=dict)
    cancelled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def expired(self, now: float) -> bool:
        return (
            now - self.created_at >= ASR_STREAM_MAX_DURATION_SECONDS
            or now - self.last_activity >= ASR_STREAM_IDLE_TIMEOUT_SECONDS
        )


class AudioStreamSequenceError(VoiceCallError):
    """A retriable stream sequence conflict that must preserve the session."""


class CoreAudioStreamProxy:
    """Bounded per-call broker for Core's authenticated streaming ASR API."""

    def __init__(self, client_factory: Any = httpx.AsyncClient):
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending_starts = 0
        self._pending_starts_drained = asyncio.Event()
        self._pending_starts_drained.set()
        self._streams: dict[str, _AudioStreamSession] = {}
        self._streams_lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        """Create the persistent Core transport; safe to call more than once."""
        async with self._lifecycle_lock:
            async with self._client_lock:
                self._closing = False
                if self._client is None:
                    self._client = self._client_factory(
                        timeout=httpx.Timeout(ASR_STREAM_REQUEST_TIMEOUT_SECONDS, connect=3.0),
                        limits=httpx.Limits(
                            max_connections=ASR_STREAM_MAX_CONCURRENCY,
                            max_keepalive_connections=ASR_STREAM_MAX_CONCURRENCY,
                        ),
                        follow_redirects=False,
                    )

    async def aclose(self) -> None:
        """Abort active Core streams and close the persistent client."""
        async with self._lifecycle_lock:
            async with self._client_lock:
                self._closing = True
                client = self._client
                self._client = None
            async with self._streams_lock:
                sessions = list(self._streams.values())
                self._streams.clear()
                for session in sessions:
                    session.cancelled = True
            # A Core start may already be in flight but not yet have returned
            # its stream_id. Keep the client open until that request observes
            # cancellation and aborts any stream it created with this client.
            await self._pending_starts_drained.wait()
            if client is not None:
                await asyncio.gather(
                    *(self._best_effort_abort(session.stream_id, client=client) for session in sessions),
                    return_exceptions=True,
                )
                await client.aclose()

    async def close(self) -> None:
        """Compatibility alias for application lifespan owners."""
        await self.aclose()

    async def start_stream(self, call_id: str, generation: int) -> dict[str, Any]:
        await self._expire_idle_streams()
        session = _AudioStreamSession(call_id=call_id, generation=generation)
        client = await self._acquire_start_client()
        reserved = False
        try:
            async with self._streams_lock:
                if self._closing:
                    raise HTTPException(503, "ASR stream proxy is shutting down")
                if call_id in self._streams:
                    raise VoiceCallError("此通话已有进行中的语音识别", 409)
                if len(self._streams) >= ASR_STREAM_MAX_CONCURRENCY:
                    raise VoiceCallError("语音识别并发已满，请稍后重试", 429)
                # Reserve a slot before awaiting Core so concurrent starts cannot
                # exceed the broker bound or create two streams for one call.
                self._streams[call_id] = session
                reserved = True

            result = await self._core_post(
                "start",
                {
                    "sample_rate": ASR_STREAM_SAMPLE_RATE,
                    "channels": ASR_STREAM_CHANNELS,
                    "platform": "webui",
                },
                client=client,
            )
            stream_id = result.get("stream_id")
            if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 128:
                raise HTTPException(502, "Core returned an invalid ASR stream")
            async with self._streams_lock:
                if self._streams.get(call_id) is not session or session.cancelled:
                    stale = True
                else:
                    session.stream_id = stream_id
                    session.last_activity = time.monotonic()
                    stale = False
            if stale:
                await self._best_effort_abort(stream_id, client=client)
                raise VoiceCallError("语音请求已过期", 409)
            # Core stream IDs stay server-side; the browser addresses the stream
            # through its call ID and generation only.
            return {"streaming": True, "generation": generation}
        except BaseException:
            if reserved:
                await self._remove_session(session, abort_core=True)
            raise
        finally:
            await self._release_start_client()

    async def _acquire_start_client(self) -> httpx.AsyncClient:
        """Pin a live transport and register an in-flight Core start atomically."""
        async with self._client_lock:
            if self._closing:
                raise HTTPException(503, "ASR stream proxy is shutting down")
            if self._client is None:
                self._client = self._client_factory(
                    timeout=httpx.Timeout(ASR_STREAM_REQUEST_TIMEOUT_SECONDS, connect=3.0),
                    limits=httpx.Limits(
                        max_connections=ASR_STREAM_MAX_CONCURRENCY,
                        max_keepalive_connections=ASR_STREAM_MAX_CONCURRENCY,
                    ),
                    follow_redirects=False,
                )
            self._pending_starts += 1
            self._pending_starts_drained.clear()
            return self._client

    async def _release_start_client(self) -> None:
        async with self._client_lock:
            self._pending_starts -= 1
            if self._pending_starts == 0:
                self._pending_starts_drained.set()

    async def send_chunk(
        self,
        call_id: str,
        generation: int,
        seq: int,
        pcm_base64: str,
        pcm: bytes,
    ) -> dict[str, Any]:
        session = await self._get_session(call_id, generation)
        failure: BaseException | None = None
        try:
            async with session.lock:
                await self._assert_session_current(session)
                now = time.monotonic()
                if session.expired(now):
                    raise VoiceCallError("ASR 流已超时", 409)
                if seq < session.next_seq:
                    digest = hashlib.sha256(pcm).digest()
                    if session.chunk_digests.get(seq) != digest:
                        raise AudioStreamSequenceError("重复的 ASR 音频块内容不匹配", 409)
                    session.last_activity = now
                    return {"seq": seq, "generation": generation}
                if seq > session.next_seq:
                    raise AudioStreamSequenceError("ASR 音频块顺序无效", 409)
                if (
                    session.next_seq >= ASR_STREAM_MAX_CHUNKS
                    or session.bytes_received + len(pcm) > ASR_STREAM_MAX_PCM_BYTES
                    or now - session.created_at >= ASR_STREAM_MAX_DURATION_SECONDS
                ):
                    raise VoiceCallError("ASR 音频超过 60 秒限制", 413)
                if session.stream_id is None:
                    raise VoiceCallError("ASR 流尚未就绪", 409)
                await self._core_post(
                    "chunk",
                    {
                        "stream_id": session.stream_id,
                        "seq": seq,
                        "pcm_base64": pcm_base64,
                    },
                )
                await self._assert_session_current(session)
                session.chunk_digests[seq] = hashlib.sha256(pcm).digest()
                session.next_seq += 1
                session.bytes_received += len(pcm)
                session.last_activity = time.monotonic()
        except AudioStreamSequenceError:
            raise
        except BaseException as exc:
            failure = exc
        if failure is not None:
            await self._remove_session(session, abort_core=True)
            raise failure
        return {"seq": seq, "generation": generation}

    async def finish_stream(self, call_id: str, generation: int) -> dict[str, Any]:
        session = await self._get_session(call_id, generation)
        failure: BaseException | None = None
        result: dict[str, Any] | None = None
        async with session.lock:
            try:
                await self._assert_session_current(session)
                if session.expired(time.monotonic()):
                    raise VoiceCallError("ASR 流已超时", 409)
                if session.stream_id is None:
                    raise VoiceCallError("ASR 流尚未就绪", 409)
                result = await self._core_post("finish", {"stream_id": session.stream_id})
                text = result.get("text")
                if not isinstance(text, str) or result.get("degraded") is True:
                    raise HTTPException(502, "Core returned an invalid ASR result")
                text = text.strip()
                if len(text) > MAX_MESSAGE_CHARS:
                    text = text[:MAX_MESSAGE_CHARS]
                await self._assert_session_current(session)
                result = {"text": text, "generation": generation}
            except BaseException as exc:
                failure = exc
        await self._remove_session(session, abort_core=failure is not None)
        if failure is not None:
            raise failure
        return result or {"text": "", "generation": generation}

    async def abort_stream(self, call_id: str, generation: int) -> dict[str, Any]:
        async with self._streams_lock:
            session = self._streams.get(call_id)
            if session is None:
                return {"aborted": True, "generation": generation}
            if session.generation != generation:
                raise VoiceCallError("语音请求已过期", 409)
        await self._remove_session(session, abort_core=True)
        return {"aborted": True, "generation": generation}

    async def abort_call(self, call_id: str) -> None:
        async with self._streams_lock:
            session = self._streams.get(call_id)
        if session is not None:
            await self._remove_session(session, abort_core=True)

    async def _get_session(self, call_id: str, generation: int) -> _AudioStreamSession:
        async with self._streams_lock:
            session = self._streams.get(call_id)
        if session is None or session.generation != generation or session.cancelled:
            raise VoiceCallError("ASR 流不存在或已过期", 409)
        if session.expired(time.monotonic()):
            await self._remove_session(session, abort_core=True)
            raise VoiceCallError("ASR 流已超时", 409)
        return session

    async def _assert_session_current(self, session: _AudioStreamSession) -> None:
        async with self._streams_lock:
            if self._streams.get(session.call_id) is not session or session.cancelled:
                raise VoiceCallError("语音请求已过期", 409)

    async def _expire_idle_streams(self) -> None:
        now = time.monotonic()
        async with self._streams_lock:
            expired = [session for session in self._streams.values() if session.expired(now)]
        for session in expired:
            await self._remove_session(session, abort_core=True)

    async def _remove_session(self, session: _AudioStreamSession, *, abort_core: bool) -> None:
        async with self._streams_lock:
            if self._streams.get(session.call_id) is session:
                self._streams.pop(session.call_id, None)
            session.cancelled = True
            stream_id = session.stream_id
        if abort_core and stream_id:
            await self._best_effort_abort(stream_id)

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._closing:
                raise HTTPException(503, "ASR stream proxy is shutting down")
            if self._client is None:
                self._client = self._client_factory(
                    timeout=httpx.Timeout(ASR_STREAM_REQUEST_TIMEOUT_SECONDS, connect=3.0),
                    limits=httpx.Limits(
                        max_connections=ASR_STREAM_MAX_CONCURRENCY,
                        max_keepalive_connections=ASR_STREAM_MAX_CONCURRENCY,
                    ),
                    follow_redirects=False,
                )
            return self._client

    async def _core_post(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if client is None:
            client = await self._ensure_client()
        token = _get_core_auth_token()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{_get_core_base_url().rstrip('/')}/api/multimodal/audio/stream/{operation}"
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise HTTPException(502, "Core ASR stream redirects are not allowed")
                if response.status_code >= 400:
                    raise HTTPException(502, "Core ASR stream request failed")
                body = bytearray()
                async for block in response.aiter_bytes():
                    if len(body) + len(block) > ASR_STREAM_MAX_RESPONSE_BYTES:
                        raise HTTPException(502, "Core ASR stream response exceeded the size limit")
                    body.extend(block)
        except asyncio.CancelledError:
            raise
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, "Core ASR stream request failed") from exc
        try:
            result = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(502, "Core returned invalid ASR stream data") from exc
        if not isinstance(result, dict):
            raise HTTPException(502, "Core returned invalid ASR stream data")
        return result

    async def _best_effort_abort(
        self,
        stream_id: str | None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not stream_id:
            return
        try:
            await self._core_post("abort", {"stream_id": stream_id}, client=client)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _http_error(exc: VoiceCallError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


def _validate_pcm16_wav(encoded: str) -> bytes:
    if len(encoded) > MAX_AUDIO_BASE64_CHARS:
        raise VoiceCallError("音频超过 4 MB 限制", 413)
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceCallError("音频编码无效", 400) from exc
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise VoiceCallError("音频为空或超过 4 MB 限制", 413)
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise VoiceCallError("音频必须是 PCM16 单声道 WAV", 400)
            rate = wav.getframerate()
            frames = wav.getnframes()
            if rate < 8_000 or rate > 48_000 or frames <= 0 or frames > rate * MAX_AUDIO_SECONDS:
                raise VoiceCallError("音频采样率或时长超出限制", 413)
            if len(wav.readframes(frames)) != frames * 2:
                raise VoiceCallError("音频内容为空或不完整", 400)
    except (wave.Error, EOFError) as exc:
        raise VoiceCallError("音频 WAV 文件无效", 400) from exc
    return audio


def _core_json_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{_get_core_base_url().rstrip('/')}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {_get_core_auth_token()}"} if _get_core_auth_token() else {}),
        },
        method="POST",
    )
    with urlopen(request, timeout=CORE_REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("Core response exceeded limit")
        parsed = json.loads(body.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}


async def _asr_readiness(tts_mgr: TTSManager) -> bool:
    try:
        core_url = _get_core_base_url()
        token = _get_core_auth_token()
        health = await asyncio.to_thread(
            tts_mgr._request_json,
            f"{core_url.rstrip('/')}/api/multimodal/health",
            4.0,
            token,
        )
    except Exception:
        return False
    if not isinstance(health, dict):
        return False

    # This is the browser's permission to capture a mic segment, not a promise
    # that Core has a local :9874 streaming runtime. A reachable full/lite Core
    # can route ordinary voice ASR to its configured remote/default provider;
    # the browser path keeps full-WAV ASR available when stream start fails.
    profile = str(health.get("desired_profile") or "").strip().casefold()
    capabilities = health.get("capabilities")
    operations = capabilities.get("perception") if isinstance(capabilities, dict) else None
    if profile in {"full", "lite"} and isinstance(operations, (list, tuple, set)):
        return "audio.transcribe.v1" in operations

    # Compatibility with older Core health payloads that only exposed local
    # operation readiness.
    observed = health.get("observed_local")
    perception = observed.get("perception") if isinstance(observed, dict) else None
    runtime = perception.get("observed") if isinstance(perception, dict) else None
    operations = runtime.get("operations") if isinstance(runtime, dict) else None
    return bool(
        health.get("desired_profile") == "full"
        and isinstance(perception, dict)
        and perception.get("required") is True
        and perception.get("ready") is True
        and isinstance(operations, (list, tuple, set))
        and "audio.transcribe.v1" in operations
    )


def create_voice_router(
    store: VoiceCallStore,
    chat_backend: Any,
    tts_mgr: TTSManager,
    *,
    audio_stream_proxy: CoreAudioStreamProxy | None = None,
) -> APIRouter:
    router = APIRouter()
    stream_proxy = audio_stream_proxy or CoreAudioStreamProxy()
    # The owning application lifespan can call .start() on startup and .aclose()
    # on shutdown without making route construction open network connections.
    router.audio_stream_proxy = stream_proxy

    def require_active_call_generation(call_id: str, generation: int) -> dict[str, Any]:
        call = store.get_call(call_id)
        if call is None or call.get("status") != "active" or call.get("generation") != generation:
            raise VoiceCallError("语音请求已过期", 409)
        return call

    async def abort_matching_stream(call_id: str, generation: int) -> None:
        try:
            await stream_proxy.abort_stream(call_id, generation)
        except VoiceCallError:
            # A request for an old generation must never cancel a newer stream.
            pass

    @router.get("/api/chat/calls/status")
    async def calls_status() -> dict[str, Any]:
        store.expire_stale_calls()
        try:
            tts_status = await tts_mgr.status(strict=True)
        except Exception:
            tts_status = {"ready": False, "error": "TTS 状态检查失败"}
        tts_ready = tts_status.get("ready") is True
        asr_ready = await _asr_readiness(tts_mgr)
        if not tts_ready:
            reason = str(tts_status.get("error") or "TTS 服务未就绪")[:200]
        elif not asr_ready:
            reason = "ASR 不可用，可直接输入文字"
        else:
            reason = ""
        return {"tts_ready": tts_ready, "asr_ready": asr_ready, "reason": reason}

    @router.post("/api/chat/calls")
    async def create_call(body: CreateCallRequest) -> dict[str, Any]:
        try:
            conversation_id = validate_conversation_id(body.conversation_id)
            tts_status = await tts_mgr.status(strict=True)
            if tts_status.get("ready") is not True:
                raise HTTPException(503, str(tts_status.get("error") or "TTS 服务未就绪")[:200])
            model_id = str(body.model_id or "").strip() or None
            if model_id:
                try:
                    from live2d_web import live2d_web_manager

                    live2d_web_manager.get_model(model_id)
                except Exception as exc:
                    raise HTTPException(400, "Live2D 模型不可用") from exc
            return store.create_call(conversation_id, body.user_name, model_id)
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.get("/api/chat/calls")
    async def list_calls(conversation_id: str = Query(min_length=1, max_length=96)) -> dict[str, Any]:
        try:
            return {"calls": store.list_calls(conversation_id)}
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.get("/api/chat/calls/{call_id}")
    async def get_call(call_id: str) -> dict[str, Any]:
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(404, "语音通话不存在")
        return call

    @router.post("/api/chat/calls/{call_id}/message")
    async def send_voice_message(call_id: str, body: VoiceMessageRequest) -> dict[str, Any]:
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(404, "语音通话不存在")
        try:
            generation = int(body.generation)
            if call["status"] != "active" or call["generation"] != generation:
                raise VoiceCallError("语音请求已过期", 409)
            content = body.message.strip()
            if not content:
                raise VoiceCallError("消息内容不能为空", 400)
            user_message = store.add_message(
                call_id,
                role="user",
                content=content,
                generation=generation,
                request_message_id=body.request_message_id,
                delivery_status="pending",
            )
            feedback = store.peek_interrupt_feedback(call_id)
            model_id = str(call.get("model_id") or "")
            control_emotions = _CONTROL_EMOTIONS
            control_actions = _CONTROL_ACTIONS
            if model_id:
                try:
                    from live2d_web import live2d_web_manager

                    entry = live2d_web_manager.get_model(model_id)
                    control_emotions = tuple(entry.get("expressions", {}).keys()) or _CONTROL_EMOTIONS
                    control_actions = tuple(_CONTROL_ACTIONS)
                except Exception:
                    model_id = ""
            try:
                result = await asyncio.wait_for(chat_backend.send_message(
                    conversation_id=call["conversation_id"],
                    text=content,
                    user_id=canonical_core_user_id(call["conversation_id"]),
                    user_name=body.user_name,
                    request_message_id=body.request_message_id,
                    channel="voice",
                    call_id=call_id,
                    generation=generation,
                    voice_reply_controls=bool(model_id),
                    control_emotions=control_emotions,
                    control_actions=control_actions,
                    interrupt_feedback=feedback,
                ), timeout=8.0)
            except asyncio.TimeoutError as exc:
                store.set_message_status(user_message["id"], call_id, "failed")
                raise HTTPException(503, "连接 Core 聊天通道超时") from exc
            except ChatBackendError as exc:
                store.set_message_status(user_message["id"], call_id, "failed")
                raise HTTPException(exc.status_code, str(exc)) from exc
            store.set_message_status(user_message["id"], call_id, "accepted")
            if feedback:
                store.consume_interrupt_feedback(call_id, str(feedback["token"]))
            return result
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/transcribe")
    async def transcribe(call_id: str, body: TranscribeRequest) -> dict[str, Any]:
        try:
            call = store.get_call(call_id)
            if call is None or call["status"] != "active" or call["generation"] != body.generation:
                raise VoiceCallError("语音请求已过期", 409)
            audio = _validate_pcm16_wav(body.audio_base64)
            payload = {
                "operation": "audio.transcribe.v1",
                "task": "voice",
                "data": base64.b64encode(audio).decode("ascii"),
                "media_format": "base64",
                "mime_type": "audio/wav",
            }
            result = await asyncio.wait_for(
                asyncio.to_thread(_core_json_request, "/api/multimodal/audio/transcribe/v1", payload),
                timeout=CORE_REQUEST_TIMEOUT_SECONDS + 2,
            )
            current = store.get_call(call_id)
            if current is None or current["status"] != "active" or current["generation"] != body.generation:
                raise VoiceCallError("语音请求已过期", 409)
            transcript = str(result.get("text") or "").strip()
            if result.get("degraded") is True or not transcript:
                raise HTTPException(503, "ASR 不可用，可直接输入文字")
            if len(transcript) > MAX_MESSAGE_CHARS:
                transcript = transcript[:MAX_MESSAGE_CHARS]
            return {"text": transcript, "generation": body.generation}
        except VoiceCallError as exc:
            raise _http_error(exc) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, "ASR 请求失败，可直接输入文字") from exc

    @router.post(
        "/api/chat/calls/{call_id}/audio/stream/start",
        description=(
            "Optional low-latency ASR path. If stream start, chunk delivery, or finish fails, "
            "clients should send the retained WAV to the call's existing /transcribe endpoint."
        ),
    )
    async def start_audio_stream(call_id: str, body: AudioStreamStartRequest) -> dict[str, Any]:
        try:
            require_active_call_generation(call_id, body.generation)
            result = await stream_proxy.start_stream(call_id, body.generation)
            require_active_call_generation(call_id, body.generation)
            return result
        except VoiceCallError as exc:
            call = store.get_call(call_id)
            if call is None or call.get("status") != "active" or call.get("generation") != body.generation:
                await abort_matching_stream(call_id, body.generation)
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/audio/stream/chunk")
    async def append_audio_stream_chunk(call_id: str, body: AudioStreamChunkRequest) -> dict[str, Any]:
        try:
            require_active_call_generation(call_id, body.generation)
            try:
                pcm = base64.b64decode(body.pcm_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise VoiceCallError("音频块编码无效", 400) from exc
            if not pcm or len(pcm) % 2:
                raise VoiceCallError("音频块必须是完整的 PCM16 单声道数据", 400)
            if len(pcm) > ASR_STREAM_CHUNK_MAX_BYTES:
                raise VoiceCallError("音频块超过 64 KB 限制", 413)
            result = await stream_proxy.send_chunk(
                call_id,
                body.generation,
                body.seq,
                body.pcm_base64,
                pcm,
            )
            require_active_call_generation(call_id, body.generation)
            return result
        except AudioStreamSequenceError as exc:
            raise _http_error(exc) from exc
        except VoiceCallError as exc:
            await abort_matching_stream(call_id, body.generation)
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/audio/stream/finish")
    async def finish_audio_stream(call_id: str, body: AudioStreamFinishRequest) -> dict[str, Any]:
        try:
            require_active_call_generation(call_id, body.generation)
            result = await stream_proxy.finish_stream(call_id, body.generation)
            require_active_call_generation(call_id, body.generation)
            return result
        except VoiceCallError as exc:
            await abort_matching_stream(call_id, body.generation)
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/audio/stream/abort")
    async def abort_audio_stream(call_id: str, body: AudioStreamAbortRequest) -> dict[str, Any]:
        try:
            return await stream_proxy.abort_stream(call_id, body.generation)
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/interrupt")
    async def interrupt(call_id: str, body: GenerationRequest) -> dict[str, Any]:
        try:
            generation = store.interrupt(call_id, body.generation)
            await abort_matching_stream(call_id, body.generation)
            return {"generation": generation}
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/end")
    async def end_call(call_id: str) -> dict[str, Any]:
        try:
            result = store.end_call(call_id)
            await stream_proxy.abort_call(call_id)
            try:
                from live2d_web import live2d_web_manager

                live2d_web_manager.discard(call_id)
            except Exception:
                pass
            return result
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/heartbeat")
    async def heartbeat(call_id: str) -> dict[str, Any]:
        try:
            return store.heartbeat(call_id)
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    @router.post("/api/chat/calls/{call_id}/tts")
    async def synthesize(call_id: str, body: TTSRequest):
        try:
            voice_message = store.message_for_tts(call_id, body.message_id, body.generation)
            tts_status = await tts_mgr.status(strict=True)
            if tts_status.get("ready") is not True:
                raise HTTPException(503, str(tts_status.get("error") or "TTS 服务未就绪")[:200])
            audio_path, cache_hit = await tts_mgr.generate(voice_message["message"]["content"], strict=True)
            current = store.get_call(call_id)
            if current is None or current["status"] != "active" or current["generation"] != body.generation:
                raise VoiceCallError("语音回复已过期", 409)
            return FileResponse(
                audio_path,
                media_type="audio/wav",
                headers={"Cache-Control": "private, max-age=86400", "X-TTS-Cache": "HIT" if cache_hit else "MISS"},
            )
        except VoiceCallError as exc:
            raise _http_error(exc) from exc
        except TTSUnavailableError as exc:
            raise HTTPException(503, str(exc)[:200]) from exc
        except TTSGenerationError as exc:
            raise HTTPException(502, str(exc)[:200]) from exc

    @router.post("/api/chat/calls/{call_id}/tts/stream")
    async def synthesize_stream(call_id: str, body: TTSRequest) -> StreamingResponse:
        """Proxy Core's PCM stream for a future browser voice-call player."""

        try:
            voice_message = store.message_for_tts(call_id, body.message_id, body.generation)
            status = await tts_mgr.status(strict=True)
            if status.get("ready") is not True:
                raise HTTPException(503, str(status.get("error") or "TTS 服务未就绪")[:200])
        except VoiceCallError as exc:
            raise _http_error(exc) from exc
        except TTSUnavailableError as exc:
            raise HTTPException(503, str(exc)[:200]) from exc

        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0), follow_redirects=False)
        upstream: httpx.Response | None = None
        try:
            core_token = _get_core_auth_token()
            headers = {"Authorization": f"Bearer {core_token}"} if core_token else {}
            request = client.build_request(
                "POST",
                f"{_get_core_base_url().rstrip('/')}/api/multimodal/tts/stream",
                headers=headers,
                json={"text": voice_message["message"]["content"], "platform": "webui"},
            )
            upstream = await client.send(request, stream=True)
            if upstream.status_code != 200:
                raise HTTPException(503 if upstream.status_code in {404, 405, 501} else 502, "Core TTS stream unavailable")
            if (
                upstream.headers.get("x-tts-stream-version") != "1"
                or upstream.headers.get("x-audio-codec") != "pcm_s16le"
                or upstream.headers.get("x-audio-sample-width") != "2"
            ):
                raise HTTPException(502, "Core TTS stream format invalid")
            sample_rate = int(upstream.headers.get("x-audio-sample-rate", "0"))
            channels = int(upstream.headers.get("x-audio-channels", "0"))
            if not 8_000 <= sample_rate <= 96_000 or channels not in {1, 2}:
                raise HTTPException(502, "Core TTS stream format invalid")
            current = store.get_call(call_id)
            if current is None or current["status"] != "active" or current["generation"] != body.generation:
                raise VoiceCallError("语音回复已过期", 409)
            pieces = upstream.aiter_bytes()
            first = await anext(pieces)
            if not first:
                raise HTTPException(502, "Core TTS stream empty")
        except (httpx.HTTPError, OSError, ValueError, StopAsyncIteration) as exc:
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            raise HTTPException(503, "Core TTS stream unavailable") from exc
        except VoiceCallError as exc:
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            raise _http_error(exc) from exc
        except BaseException:
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()
            raise

        async def audio():
            total = 0
            try:
                for part in (first,):
                    total += len(part)
                    if total > TTS_STREAM_MAX_PCM_BYTES:
                        raise RuntimeError("Core TTS stream exceeded WebUI audio limit")
                    yield part
                async for part in pieces:
                    current = store.get_call(call_id)
                    if current is None or current["status"] != "active" or current["generation"] != body.generation:
                        raise RuntimeError("WebUI voice call ended during TTS stream")
                    total += len(part)
                    if total > TTS_STREAM_MAX_PCM_BYTES:
                        raise RuntimeError("Core TTS stream exceeded WebUI audio limit")
                    yield part
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            audio(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-TTS-Stream-Version": "1",
                "X-Audio-Sample-Rate": str(sample_rate),
                "X-Audio-Channels": str(channels),
                "X-Audio-Sample-Width": "2",
                "X-Audio-Codec": "pcm_s16le",
            },
        )

    @router.post("/api/chat/calls/{call_id}/control")
    async def apply_control(call_id: str, body: ControlRequest) -> dict[str, Any]:
        try:
            call = store.get_call(call_id)
            if call is None or call["status"] != "active" or call["generation"] != body.generation:
                raise VoiceCallError("语音控制已过期", 409)
            if not call.get("model_id"):
                return {"generation": body.generation, "commands": []}
            if not store.claim_control(call_id, body.control_id, body.generation):
                return {"generation": body.generation, "commands": []}
            from live2d_web import live2d_web_manager

            commands = live2d_web_manager.apply_control(call_id, body.control_id)
            current = store.get_call(call_id)
            if current is None or current["status"] != "active" or current["generation"] != body.generation:
                return {"generation": body.generation, "commands": []}
            return {"generation": body.generation, "commands": commands}
        except VoiceCallError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            raise HTTPException(503, "Live2D 控制不可用") from exc

    @router.post("/api/chat/calls/{call_id}/playback")
    async def playback(call_id: str, body: PlaybackRequest) -> dict[str, Any]:
        try:
            row = store.settle_playback(call_id, body.message_id, body.generation, body.status)
            return {"message_id": body.message_id, "status": row.get("delivery_status")}
        except VoiceCallError as exc:
            raise _http_error(exc) from exc

    return router


__all__ = ["CoreAudioStreamProxy", "create_voice_router"]
