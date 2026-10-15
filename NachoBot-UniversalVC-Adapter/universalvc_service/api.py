"""Authenticated loopback HTTP and WebSocket API for UniversalVC sessions."""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import WSMsgType, web

from .engine import (
    SessionRequestError,
    SessionUnavailableError,
    UniversalVCSessionEngine,
    validate_session_options,
)
from .service_config import ServiceSettings


PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 8192
MAX_PCM_FRAME_BYTES = 65536
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_VOICE_BYTES = 16 * 1024 * 1024
MAX_PENDING_REQUESTS = 8
MAX_PENDING_AUDIO_BYTES = 16 * 1024 * 1024
logger = logging.getLogger("universalvc.service.api")


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _common(session: "ManagedSession") -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "service_instance_id": session.service_instance_id,
        "session_id": session.session_id,
        "session_generation": session.session_generation,
    }


def _bearer(request: web.Request) -> str:
    value = request.headers.get("Authorization", "")
    prefix = "Bearer "
    return value[len(prefix):] if value.startswith(prefix) else ""


@dataclass
class PendingAudio:
    request_id: str
    source_id: str
    op: str
    extra: dict[str, Any]
    chunked: bool = False
    chunks: bytearray = field(default_factory=bytearray)
    next_seq: int = 0
    declared_size: int | None = None
    base_sequence: int | None = None


@dataclass
class ManagedSession:
    engine: UniversalVCSessionEngine
    service_instance_id: str
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    websocket: web.WebSocketResponse | None = None
    writer_task: asyncio.Task | None = None
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, PendingAudio] = field(default_factory=dict)
    pending_audio_bytes: int = 0
    active_audio_bytes: int = 0
    operations: set[asyncio.Task] = field(default_factory=set)
    active_request_ids: set[str] = field(default_factory=set)
    closing: bool = False
    event_overflow: bool = False

    @property
    def session_id(self) -> str:
        return self.engine.session_id

    @property
    def session_generation(self) -> str:
        return self.engine.session_generation


class SessionManager:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        engine_factory: Callable[..., UniversalVCSessionEngine] = UniversalVCSessionEngine,
        service_instance_id: str | None = None,
    ):
        self.settings = settings
        self.engine_factory = engine_factory
        self.service_instance_id = service_instance_id or uuid.uuid4().hex
        self.sessions: dict[str, ManagedSession] = {}
        self.resource_owners: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None
        self._closed = False

    def start(self, app: web.Application) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep(), name="universalvc-session-lease-sweeper")

    async def create(self, options: dict[str, Any]) -> ManagedSession:
        normalized = validate_session_options(options, self.settings)
        async with self.lock:
            if self._closed:
                raise SessionUnavailableError("UniversalVC service is shutting down")
            if len(self.sessions) >= self.settings.max_sessions:
                raise SessionUnavailableError("maximum UniversalVC session count reached")
            total_sources = sum(len(item.engine.sources) or len(item.engine.options["sources"]) for item in self.sessions.values())
            if total_sources + len(normalized["sources"]) > self.settings.max_total_sources:
                raise SessionUnavailableError("maximum UniversalVC source count reached")
            engine = self.engine_factory(
                self.settings,
                normalized,
                service_instance_id=self.service_instance_id,
                emit=None,
            )
            session = ManagedSession(engine=engine, service_instance_id=self.service_instance_id)

            async def emit(event: str, source_id: str, **fields):
                if session.closing:
                    return
                body = {
                    **_common(session),
                    "type": "event",
                    "event": event,
                    "platform": session.engine.options["platform"],
                    "source_id": source_id,
                    **fields,
                }
                encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                if len(encoded) > MAX_EVENT_BYTES:
                    body = {
                        **_common(session), "type": "event", "event": "error", "source_id": source_id,
                        "code": "event_too_large", "message": "service event exceeded the protocol size limit",
                    }
                try:
                    session.events.put_nowait(body)
                except asyncio.QueueFull:
                    session.event_overflow = True
                    ws = session.websocket
                    if ws is not None and not ws.closed:
                        await ws.close(code=1013, message=b"event queue overflow")

            engine.emit = emit
            keys = engine.resource_keys
            conflicts = sorted(key for key in keys if key in self.resource_owners)
            if conflicts:
                await engine.close()
                raise SessionUnavailableError("one or more requested capture/output resources are already reserved")
            # Hold reservations across asynchronous device/model setup.
            for key in keys:
                self.resource_owners[key] = engine.session_id
            try:
                await engine.start()
            except BaseException:
                await engine.close()
                for key in keys:
                    if self.resource_owners.get(key) == engine.session_id:
                        self.resource_owners.pop(key, None)
                raise
            self.sessions[engine.session_id] = session
            return session

    async def delete(self, session_id: str) -> bool:
        async with self.lock:
            session = self.sessions.pop(session_id, None)
            if session is None:
                return False
            session.closing = True
        await self._close_session(session)
        async with self.lock:
            for key, owner in tuple(self.resource_owners.items()):
                if owner == session_id:
                    self.resource_owners.pop(key, None)
        return True

    async def _close_session(self, session: ManagedSession) -> None:
        session.closing = True
        operations = list(session.operations)
        for operation in operations:
            operation.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        session.operations.clear()
        session.pending.clear()
        session.pending_audio_bytes = 0
        session.active_audio_bytes = 0
        session.active_request_ids.clear()
        task = session.writer_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws = session.websocket
        if ws is not None and not ws.closed:
            await ws.close(code=1001, message=b"session closed")
        try:
            await session.engine.close()
        except Exception:
            logger.exception("UniversalVC session cleanup failed")

    async def close(self) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.resource_owners.clear()
        await asyncio.gather(*(self._close_session(session) for session in sessions), return_exceptions=True)

    async def _sweep(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(min(1.0, self.settings.heartbeat_interval_seconds))
                now = time.monotonic()
                expired = [
                    session_id
                    for session_id, session in tuple(self.sessions.items())
                    if now - session.last_activity > self.settings.session_idle_timeout_seconds
                ]
                for session_id in expired:
                    await self.delete(session_id)
        except asyncio.CancelledError:
            raise


MANAGER_APP_KEY = web.AppKey("universalvc_manager", SessionManager)


def _manager(request: web.Request) -> SessionManager:
    return request.app[MANAGER_APP_KEY]


def _validate_loopback_settings(settings: ServiceSettings) -> None:
    try:
        address = ipaddress.ip_address(settings.bind_host)
    except ValueError as exc:
        raise ValueError("UniversalVC service bind must be a loopback IP literal") from exc
    if not address.is_loopback:
        raise ValueError("UniversalVC service bind must be a loopback IP literal")
    if not isinstance(settings.token, str) or not settings.token.strip():
        raise ValueError("UniversalVC service bearer token must be configured")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    manager = _manager(request)
    if not hmac.compare_digest(_bearer(request), manager.settings.token):
        return _error("unauthorized", "valid bearer authentication is required", 401)
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    manager = _manager(request)
    settings = manager.settings
    try:
        from .vad_processor import _SHERPA_AVAILABLE
    except Exception:
        _SHERPA_AVAILABLE = False
    try:
        from .denoise import _RNNOISE_AVAILABLE
    except Exception:
        _RNNOISE_AVAILABLE = False
    try:
        from .speaker_tracker import _SHERPA_AVAILABLE as speaker_available
    except Exception:
        speaker_available = False
    capabilities = {
        "pipeline": {"available": bool(_SHERPA_AVAILABLE and settings.vad_model_path.is_file())},
        "vad": {"available": bool(_SHERPA_AVAILABLE and settings.vad_model_path.is_file())},
        "denoise": {"available": bool(_RNNOISE_AVAILABLE)},
        "speaker": {"available": bool(speaker_available and settings.speaker_model_path.is_file())},
        "device_capture": {"available": True, "selection": "explicit_only"},
        "device_playback": {"available": True, "selection": "explicit_only"},
    }
    return web.json_response({
        "protocol_version": PROTOCOL_VERSION,
        "service_instance_id": manager.service_instance_id,
        "ready": True,
        "capabilities": capabilities,
        "limits": {
            "max_sessions": settings.max_sessions,
            "max_sources_per_session": settings.max_sources_per_session,
            "max_total_sources": settings.max_total_sources,
            "source_queue_size": settings.source_queue_size,
            "max_pcm_frame_bytes": MAX_PCM_FRAME_BYTES,
            "max_utterance_seconds": settings.max_utterance_seconds,
            "session_idle_timeout_seconds": settings.session_idle_timeout_seconds,
            "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
        },
    })


async def create_session(request: web.Request) -> web.Response:
    try:
        options = await request.json(loads=json.loads)
        session = await _manager(request).create(options)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("invalid_json", "session request must be valid JSON", 400)
    except SessionRequestError as exc:
        return _error("invalid_session", str(exc), 400)
    except SessionUnavailableError as exc:
        return _error("unavailable", str(exc), 409)
    except Exception:
        logger.exception("UniversalVC session creation failed")
        return _error("session_start_failed", "requested UniversalVC session could not start", 503)
    return web.json_response({
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session.session_id,
        "session_generation": session.session_generation,
        "service_instance_id": session.service_instance_id,
        "websocket_path": f"/v1/sessions/{session.session_id}/stream",
    }, status=201)


async def interrupt_session(request: web.Request) -> web.Response:
    manager = _manager(request)
    session = manager.sessions.get(request.match_info["session_id"])
    if session is None:
        return _error("not_found", "session does not exist", 404)
    try:
        body = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("invalid_json", "interrupt request must be valid JSON", 400)
    if not isinstance(body, dict):
        return _error("invalid_request", "interrupt request must be an object", 400)
    if body.get("session_generation") != session.session_generation or body.get("service_instance_id") != session.service_instance_id:
        return _error("stale_session", "session identity is stale", 409)
    session.last_activity = time.monotonic()
    try:
        await session.engine.interrupt()
    except Exception:
        logger.exception("UniversalVC interrupt failed")
        return _error("interrupt_failed", "session interrupt failed", 500)
    return web.json_response({"status": "interrupted", "session_id": session.session_id})


async def delete_session(request: web.Request) -> web.Response:
    deleted = await _manager(request).delete(request.match_info["session_id"])
    if not deleted:
        return _error("not_found", "session does not exist", 404)
    return web.json_response({"status": "closed"})


async def _ws_send(session: ManagedSession, value: dict[str, Any]) -> None:
    ws = session.websocket
    if ws is None or ws.closed:
        return
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("WebSocket JSON message exceeded size limit")
    async with session.send_lock:
        await ws.send_str(raw.decode("utf-8"))


async def _event_writer(session: ManagedSession) -> None:
    try:
        while not session.closing:
            value = await session.events.get()
            await _ws_send(session, value)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("UniversalVC event writer ended", exc_info=True)
        ws = session.websocket
        if ws is not None and not ws.closed:
            await ws.close(code=1011, message=b"event writer failed")


def _check_identity(message: dict[str, Any], session: ManagedSession) -> None:
    expected = _common(session)
    for key in ("protocol_version", "service_instance_id", "session_id", "session_generation"):
        if message.get(key) != expected[key]:
            raise SessionRequestError(f"stale or invalid {key}")


def _request_id(message: dict[str, Any]) -> str:
    value = message.get("request_id")
    if not isinstance(value, str) or not value or len(value) > 128:
        raise SessionRequestError("request_id is invalid")
    return value


async def _reply(session: ManagedSession, request_id: str, result: dict | None = None, error: Exception | None = None):
    value = {**_common(session), "type": "response", "request_id": request_id}
    if error is None:
        value.update(ok=True, result=result or {})
    else:
        if isinstance(error, SessionRequestError):
            code = "invalid_request"
        elif isinstance(error, SessionUnavailableError):
            code = "unavailable"
        else:
            code = "operation_failed"
            logger.exception("UniversalVC stream operation failed", exc_info=error)
        value.update(ok=False, error={"code": code, "message": str(error)[:512]})
    await _ws_send(session, value)


def _begin_pending(session: ManagedSession, message: dict[str, Any], *, op: str, chunked: bool = False) -> PendingAudio:
    request_id = _request_id(message)
    if request_id in session.pending or request_id in session.active_request_ids:
        raise SessionRequestError("duplicate request_id")
    if len(session.pending) >= MAX_PENDING_REQUESTS:
        raise SessionUnavailableError("too many outstanding WebSocket operations")
    source_id = message.get("source_id", "main")
    if not isinstance(source_id, str) or source_id not in session.engine.sources:
        raise SessionRequestError("source_id is not declared")
    extra = {key: value for key, value in message.items() if key not in {
        "type", "protocol_version", "service_instance_id", "session_id", "session_generation",
        "request_id", "source_id", "op",
    }}
    pending = PendingAudio(request_id, source_id, op, extra, chunked=chunked)
    session.pending[request_id] = pending
    return pending


def _drop_pending(session: ManagedSession, request_id: str) -> PendingAudio | None:
    pending = session.pending.pop(request_id, None)
    if pending is not None:
        session.pending_audio_bytes = max(0, session.pending_audio_bytes - len(pending.chunks))
    return pending


def _track_operation(
    session: ManagedSession,
    coroutine,
    *,
    request_id: str,
    audio_bytes: int = 0,
) -> None:
    if len(session.operations) >= MAX_PENDING_REQUESTS:
        coroutine.close()
        raise SessionUnavailableError("too many active WebSocket operations")
    if session.pending_audio_bytes + session.active_audio_bytes + audio_bytes > MAX_PENDING_AUDIO_BYTES:
        coroutine.close()
        raise SessionUnavailableError("active audio operations exceeded the session memory limit")
    session.active_audio_bytes += audio_bytes
    session.active_request_ids.add(request_id)
    task = asyncio.create_task(coroutine)
    session.operations.add(task)

    def finished(completed: asyncio.Task) -> None:
        session.operations.discard(completed)
        session.active_audio_bytes = max(0, session.active_audio_bytes - audio_bytes)
        session.active_request_ids.discard(request_id)

    task.add_done_callback(finished)


def _dispatch_audio(session: ManagedSession, pending: PendingAudio, pcm: bytes) -> None:
    _drop_pending(session, pending.request_id)
    _track_operation(
        session,
        _complete_audio(session, pending, pcm),
        request_id=pending.request_id,
        audio_bytes=len(pcm),
    )


async def _complete_audio(session: ManagedSession, pending: PendingAudio, pcm: bytes) -> None:
    _drop_pending(session, pending.request_id)
    try:
        if pending.op in {"send_pcm", "feed_vad"}:
            sequence = pending.extra.get("sequence")
            result = await session.engine.process_audio(pending.op, pending.source_id, sequence, pcm)
        elif pending.op == "encode_wav":
            if pending.chunked:
                if pending.base_sequence is None:
                    raise SessionRequestError("WAV audio chunks are missing their sequence")
                for index in range(pending.next_seq):
                    session.engine.validate_sequence(
                        pending.source_id, "encode_wav", pending.base_sequence + index
                    )
            else:
                session.engine.validate_sequence(
                    pending.source_id, "encode_wav", pending.extra.get("sequence")
                )
            result = await session.engine.encode_wav(
                pending.source_id, pcm, pending.extra.get("max_duration_seconds", 60)
            )
        elif pending.op == "play_voice":
            result = await session.engine.play_voice(pending.source_id, pcm)
        elif pending.op == "play_voice_stream":
            segment = pending.extra.get("segment")
            if not isinstance(segment, dict):
                raise SessionRequestError("voice stream metadata is missing")
            result = await session.engine.play_voice_stream(pending.source_id, segment, pcm)
        else:
            raise SessionRequestError("unsupported binary audio operation")
    except Exception as exc:
        await _reply(session, pending.request_id, error=exc)
    else:
        await _reply(session, pending.request_id, result=result)


async def _request(session: ManagedSession, message: dict[str, Any]) -> None:
    request_id = _request_id(message)
    op = message.get("op")
    source_id = message.get("source_id", "main")
    if not isinstance(op, str):
        raise SessionRequestError("request op is invalid")
    if op in {"send_pcm", "feed_vad", "encode_wav", "play_voice_stream"}:
        pending = _begin_pending(session, message, op=op)
        pending.extra["sequence"] = message.get("seq")
        return
    if op in {"encode_wav_begin", "play_voice_begin"}:
        base_op = "encode_wav" if op == "encode_wav_begin" else "play_voice"
        pending = _begin_pending(session, message, op=base_op, chunked=True)
        if base_op == "play_voice":
            declared = message.get("audio_bytes")
            if isinstance(declared, bool) or not isinstance(declared, int) or not 1 <= declared <= MAX_VOICE_BYTES:
                session.pending.pop(request_id, None)
                raise SessionRequestError("audio_bytes is invalid")
            pending.declared_size = declared
        pending.extra["sequence"] = message.get("seq")
        return
    if op in {"encode_wav_end", "play_voice_end"}:
        pending = session.pending.get(request_id)
        expected_op = "encode_wav" if op == "encode_wav_end" else "play_voice"
        if pending is None or not pending.chunked or pending.op != expected_op:
            raise SessionRequestError("audio end has no matching begin")
        if expected_op == "play_voice" and pending.declared_size != len(pending.chunks):
            _drop_pending(session, request_id)
            raise SessionRequestError("Core voice payload length did not match declaration")
        _dispatch_audio(session, pending, bytes(pending.chunks))
        return
    if op in {"pause_audio", "resume_audio"}:
        result = await session.engine.command(source_id, op)
        await _reply(session, request_id, result=result)
        return
    if op == "play_voice_stream_abort":
        segment = message.get("segment")
        if not isinstance(segment, dict):
            raise SessionRequestError("voice stream metadata is missing")
        result = await session.engine.play_voice_stream(source_id, segment, b"")
        await _reply(session, request_id, result=result)
        return
    raise SessionRequestError("unknown session operation")


async def _binary(session: ManagedSession, raw: bytes) -> None:
    if len(raw) < 4:
        raise SessionRequestError("binary frame omitted header length")
    header_size = struct.unpack(">I", raw[:4])[0]
    if not 1 <= header_size <= MAX_HEADER_BYTES or len(raw) < 4 + header_size:
        raise SessionRequestError("binary frame header length is invalid")
    try:
        message = json.loads(raw[4:4 + header_size].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionRequestError("binary frame header is invalid JSON") from exc
    if not isinstance(message, dict):
        raise SessionRequestError("binary frame header must be an object")
    _check_identity(message, session)
    pcm = raw[4 + header_size:]
    allow_empty_stream_control = (
        message.get("op") == "play_voice_stream"
        and isinstance(message.get("segment"), dict)
        and message["segment"].get("event") in {"start", "end", "abort"}
    )
    if (not pcm and not allow_empty_stream_control) or len(pcm) > MAX_PCM_FRAME_BYTES:
        raise SessionRequestError("binary PCM frame is empty or exceeds size limit")
    request_id = _request_id(message)
    pending = session.pending.get(request_id)
    if pending is None:
        raise SessionRequestError("binary frame has no outstanding request")
    source_id = message.get("source_id")
    op = message.get("op")
    if source_id != pending.source_id:
        raise SessionRequestError("binary frame source_id does not match request")
    expected_binary_op = pending.op if not pending.chunked else f"{pending.op}_chunk"
    if op != expected_binary_op:
        raise SessionRequestError("binary frame op does not match request")
    sequence = message.get("seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise SessionRequestError("binary frame sequence is invalid")
    if pending.chunked:
        if pending.op == "encode_wav":
            if pending.base_sequence is None:
                pending.base_sequence = sequence
            expected_sequence = pending.base_sequence + pending.next_seq
        else:
            expected_sequence = pending.next_seq
        if sequence != expected_sequence:
            raise SessionRequestError("binary chunk sequence is duplicate or out of order")
        pending.next_seq += 1
        bound = MAX_VOICE_BYTES if pending.op == "play_voice" else 60 * 16_000 * 2
        if (
            len(pending.chunks) + len(pcm) > bound
            or session.pending_audio_bytes + session.active_audio_bytes + len(pcm) > MAX_PENDING_AUDIO_BYTES
        ):
            _drop_pending(session, request_id)
            raise SessionRequestError("audio request exceeded its bounded payload limit")
        pending.chunks.extend(pcm)
        session.pending_audio_bytes += len(pcm)
        return
    pending.extra["sequence"] = sequence
    _dispatch_audio(session, pending, pcm)


async def session_stream(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    session = manager.sessions.get(request.match_info["session_id"])
    if session is None:
        return _error("not_found", "session does not exist", 404)
    if session.closing:
        return _error("session_closed", "session is closing", 409)
    if session.websocket is not None and not session.websocket.closed:
        return _error("stream_bound", "session already has a WebSocket connection", 409)
    ws = web.WebSocketResponse(
        heartbeat=None,
        autoping=True,
        autoclose=True,
        max_msg_size=MAX_EVENT_BYTES,
        compress=False,
    )
    session.websocket = ws
    try:
        await ws.prepare(request)
        session.last_activity = time.monotonic()
        session.writer_task = asyncio.create_task(_event_writer(session), name=f"universalvc-events-{session.session_id[:12]}")
        await _ws_send(session, {**_common(session), "type": "ready", "protocol_version": PROTOCOL_VERSION})
        async for message in ws:
            session.last_activity = time.monotonic()
            if message.type == WSMsgType.TEXT:
                if len(message.data.encode("utf-8")) > MAX_EVENT_BYTES:
                    await ws.close(code=1009, message=b"event too large")
                    break
                try:
                    value = json.loads(message.data)
                    if not isinstance(value, dict):
                        raise SessionRequestError("WebSocket message must be an object")
                    _check_identity(value, session)
                    msg_type = value.get("type")
                    if msg_type == "heartbeat":
                        continue
                    if msg_type != "request":
                        raise SessionRequestError("unknown WebSocket message type")
                    await _request(session, value)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    await ws.close(code=1007, message=b"invalid JSON")
                    raise SessionRequestError("WebSocket message is invalid JSON") from exc
                except Exception as exc:
                    req_id = value.get("request_id") if isinstance(value, dict) else None
                    if isinstance(req_id, str) and req_id:
                        _drop_pending(session, req_id)
                        await _reply(session, req_id, error=exc)
                    else:
                        await ws.close(code=1008, message=b"invalid request")
                        break
            elif message.type == WSMsgType.BINARY:
                try:
                    await _binary(session, message.data)
                except Exception as exc:
                    await ws.close(code=1008, message=b"invalid audio frame")
                    raise SessionRequestError(str(exc)) from exc
            elif message.type == WSMsgType.ERROR:
                break
    except ConnectionError:
        pass
    except asyncio.CancelledError:
        # aiohttp may cancel a handler as its peer disconnects; the finally
        # block performs the lease and resource teardown without cancellation.
        pass
    except Exception:
        logger.debug("UniversalVC WebSocket ended with an error", exc_info=True)
    finally:
        if session.websocket is ws:
            session.websocket = None
        writer_task, session.writer_task = session.writer_task, None
        if writer_task is not None:
            writer_task.cancel()
            try:
                await asyncio.shield(asyncio.gather(writer_task, return_exceptions=True))
            except asyncio.CancelledError:
                pass
        # A stream disconnect ends the session; callers must create a new
        # generation instead of resuming buffered/captured audio.
        cleanup = asyncio.create_task(manager.delete(session.session_id))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            pass
    return ws


def create_app(
    settings: ServiceSettings,
    *,
    engine_factory: Callable[..., UniversalVCSessionEngine] = UniversalVCSessionEngine,
    service_instance_id: str | None = None,
) -> web.Application:
    """Create an injectable aiohttp application for production and loopback tests."""
    _validate_loopback_settings(settings)
    manager = SessionManager(settings, engine_factory=engine_factory, service_instance_id=service_instance_id)
    app = web.Application(middlewares=[auth_middleware], client_max_size=128 * 1024)
    app[MANAGER_APP_KEY] = manager
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/sessions", create_session)
    app.router.add_get("/v1/sessions/{session_id}/stream", session_stream)
    app.router.add_post("/v1/sessions/{session_id}/interrupt", interrupt_session)
    app.router.add_delete("/v1/sessions/{session_id}", delete_session)

    async def startup(app: web.Application):
        manager.start(app)

    async def cleanup(app: web.Application):
        await manager.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
