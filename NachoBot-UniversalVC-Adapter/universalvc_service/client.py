"""Lightweight loopback client for the UniversalVC audio service.

This module deliberately imports only the standard library and aiohttp so Core
and platform adapters do not load UniversalVC's native audio dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import struct
import uuid
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp


DEFAULT_BASE_URL = "http://127.0.0.1:8016"
PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 8192
MAX_PCM_FRAME_BYTES = 65536
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_WAV_BYTES = 16 * 1024 * 1024
_END = object()


class UniversalVCClientError(RuntimeError):
    """Base class for service and protocol failures."""


class UniversalVCDisconnected(UniversalVCClientError):
    """The service disconnected or the session lease ended."""


class UniversalVCProtocolError(UniversalVCClientError):
    """The service returned stale, malformed, or unknown protocol data."""


class UniversalVCServiceError(UniversalVCClientError):
    """The service rejected a well-formed request."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"UniversalVC service error {code}: {message}")


def _loopback_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("UniversalVC base URL must be a non-empty URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("UniversalVC URL must use HTTP(S) and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("UniversalVC URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("UniversalVC base URL must not contain a query or fragment")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise ValueError("UniversalVC client only accepts literal loopback hosts")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _json_object(raw: bytes | str, *, maximum: int) -> dict[str, Any]:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    else:
        encoded = raw
    if len(encoded) > maximum:
        raise UniversalVCProtocolError("UniversalVC response exceeded its size limit")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UniversalVCProtocolError("UniversalVC returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UniversalVCProtocolError("UniversalVC returned a non-object response")
    return value


def _voice_payload(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Core voice segment omitted audio_base64")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("Core voice segment has invalid base64") from exc
    if not raw or len(raw) > MAX_WAV_BYTES:
        raise ValueError("Core voice segment is empty or exceeds the WAV limit")
    return raw


class UniversalVCClient:
    """Authenticated HTTP/WebSocket client for a local UniversalVC service."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        selected_url = base_url or os.environ.get("NACHOBOT_UNIVERSALVC_URL") or DEFAULT_BASE_URL
        self.base_url = _loopback_base_url(selected_url)
        self.token = (token if token is not None else os.environ.get("NACHOBOT_UNIVERSALVC_TOKEN", "")).strip()
        self._http: aiohttp.ClientSession | None = None
        self._sessions: set[UniversalVCSession] = set()
        self._closed = False

    def _authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._closed:
            raise UniversalVCDisconnected("UniversalVC client is closed")
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=35.0, connect=3.0, sock_read=31.0),
                trust_env=False,
                connector=aiohttp.TCPConnector(limit=8, ssl=False if self.base_url.startswith("http://") else None),
            )
        return self._http

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        session = await self._ensure_http()
        url = self.base_url + path
        async with session.request(
            method,
            url,
            json=payload,
            headers=self._authorization(),
            allow_redirects=False,
        ) as response:
            if allow_missing and response.status == 404:
                return {}
            body = await response.content.read(MAX_EVENT_BYTES + 1)
            result = _json_object(body, maximum=MAX_EVENT_BYTES) if body else {}
            if 300 <= response.status < 400:
                raise UniversalVCProtocolError("UniversalVC endpoint attempted a redirect")
            if response.status < 200 or response.status >= 300:
                error = result.get("error") if isinstance(result.get("error"), dict) else {}
                code = error.get("code") if isinstance(error.get("code"), str) else f"http_{response.status}"
                message = error.get("message") if isinstance(error.get("message"), str) else "request rejected"
                raise UniversalVCServiceError(code, message)
            return result

    async def create_session(self, options: dict[str, Any]) -> "UniversalVCSession":
        if not isinstance(options, dict):
            raise TypeError("session options must be a dictionary")
        raw_sources = options.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("session options must declare at least one source")
        source_ids: set[str] = set()
        for source in raw_sources:
            source_id = source.get("id") if isinstance(source, dict) else None
            if not isinstance(source_id, str) or not source_id or len(source_id) > 64:
                raise ValueError("session options contain an invalid source ID")
            source_ids.add(source_id)
        if len(source_ids) != len(raw_sources):
            raise ValueError("session options contain duplicate source IDs")
        result = await self._request_json("POST", "/v1/sessions", payload=options)
        required = ("session_id", "session_generation", "service_instance_id", "websocket_path")
        if any(not isinstance(result.get(key), str) or not result[key] for key in required):
            raise UniversalVCProtocolError("UniversalVC create response omitted session identity")
        session = UniversalVCSession(
            self,
            session_id=result["session_id"],
            session_generation=result["session_generation"],
            service_instance_id=result["service_instance_id"],
            websocket_path=result["websocket_path"],
            source_ids=source_ids,
        )
        self._sessions.add(session)
        try:
            await session._connect()
        except BaseException:
            await session.close()
            raise
        return session

    async def close(self) -> None:
        if self._closed:
            return
        sessions = list(self._sessions)
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        self._closed = True
        if self._http is not None and not self._http.closed:
            await self._http.close()


class UniversalVCSession:
    """One server-owned audio session and its single bound WebSocket."""

    def __init__(
        self,
        client: UniversalVCClient,
        *,
        session_id: str,
        session_generation: str,
        service_instance_id: str,
        websocket_path: str,
        source_ids: set[str],
    ):
        self._client = client
        self.session_id = session_id
        self.session_generation = session_generation
        self.service_instance_id = service_instance_id
        self._websocket_path = websocket_path
        self._source_ids = frozenset(source_ids)
        self._default_source_id = sorted(source_ids)[0]
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._sequence_locks: dict[str, asyncio.Lock] = {
            source_id: asyncio.Lock() for source_id in source_ids
        }
        self._sequences: dict[tuple[str, str], int] = {}
        self._events: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._ready = asyncio.Event()
        self._terminal_error: BaseException | None = None
        self._closing = False

    def _common_header(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "service_instance_id": self.service_instance_id,
            "session_id": self.session_id,
            "session_generation": self.session_generation,
        }

    def _ws_url(self) -> str:
        if self._websocket_path.startswith("/"):
            path = self._websocket_path
        else:
            path = "/" + self._websocket_path
        parsed = urlsplit(self._client.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, parsed.path + path, "", ""))

    async def _connect(self) -> None:
        http = await self._client._ensure_http()
        try:
            self._ws = await http.ws_connect(
                self._ws_url(),
                headers=self._client._authorization(),
                autoping=True,
                autoclose=True,
                heartbeat=None,
                max_msg_size=MAX_EVENT_BYTES,
                compress=0,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise UniversalVCDisconnected("could not connect to UniversalVC stream") from exc
        self._receive_task = asyncio.create_task(self._receive_loop(), name=f"universalvc-recv-{self.session_id[:12]}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            await self._fail(UniversalVCProtocolError("UniversalVC stream handshake timed out"))
            raise UniversalVCProtocolError("UniversalVC stream handshake timed out") from exc
        if self._terminal_error is not None:
            raise self._terminal_error
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"universalvc-heartbeat-{self.session_id[:12]}"
        )

    async def _fail(self, error: BaseException) -> None:
        if self._terminal_error is None:
            self._terminal_error = error
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(self._terminal_error)
        self._pending.clear()
        self._ready.set()
        if self._events.full():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._events.put_nowait(_END)
        except asyncio.QueueFull:
            pass

    def _check_identity(self, message: dict[str, Any]) -> None:
        expected = self._common_header()
        for key, value in expected.items():
            if message.get(key) != value:
                raise UniversalVCProtocolError(f"UniversalVC returned stale {key}")

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    value = _json_object(message.data, maximum=MAX_EVENT_BYTES)
                    self._check_identity(value)
                    kind = value.get("type")
                    if kind == "ready":
                        if value.get("protocol_version") != PROTOCOL_VERSION:
                            raise UniversalVCProtocolError("UniversalVC protocol version mismatch")
                        self._ready.set()
                        continue
                    if kind == "response":
                        request_id = value.get("request_id")
                        if not isinstance(request_id, str) or request_id not in self._pending:
                            raise UniversalVCProtocolError("UniversalVC returned an unknown request ID")
                        future = self._pending.pop(request_id)
                        if value.get("ok") is True:
                            if not future.done():
                                future.set_result(value.get("result", {}))
                        else:
                            error = value.get("error") if isinstance(value.get("error"), dict) else {}
                            code = error.get("code") if isinstance(error.get("code"), str) else "rejected"
                            reason = error.get("message") if isinstance(error.get("message"), str) else "request rejected"
                            if not future.done():
                                future.set_exception(UniversalVCServiceError(code, reason))
                        continue
                    if kind == "event":
                        event_name = value.get("event")
                        if event_name not in {"speech_start", "speech_end", "speech_result", "error", "playback"}:
                            raise UniversalVCProtocolError("UniversalVC returned an unknown event")
                        try:
                            self._events.put_nowait(value)
                        except asyncio.QueueFull as exc:
                            raise UniversalVCProtocolError("UniversalVC event queue overflowed") from exc
                        continue
                    raise UniversalVCProtocolError("UniversalVC returned an unknown message type")
                if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    raise UniversalVCDisconnected("UniversalVC stream closed")
                if message.type == aiohttp.WSMsgType.ERROR:
                    raise UniversalVCDisconnected("UniversalVC stream failed")
                if message.type == aiohttp.WSMsgType.BINARY:
                    raise UniversalVCProtocolError("UniversalVC sent an unexpected binary frame")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc if isinstance(exc, UniversalVCClientError) else UniversalVCDisconnected("UniversalVC stream disconnected")
            await self._fail(error)
        finally:
            if self._terminal_error is None and not self._closing:
                await self._fail(UniversalVCDisconnected("UniversalVC stream ended"))

    async def _write_json(self, value: dict[str, Any]) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._ws is None or self._ws.closed:
            raise UniversalVCDisconnected("UniversalVC stream is not connected")
        message = {**self._common_header(), **value}
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("UniversalVC request event exceeds size limit")
        async with self._send_lock:
            try:
                await self._ws.send_str(encoded)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise UniversalVCDisconnected("UniversalVC stream send failed") from exc

    async def _write_binary(self, header: dict[str, Any], pcm: bytes) -> None:
        if not isinstance(pcm, bytes):
            pcm = bytes(pcm)
        if len(pcm) > MAX_PCM_FRAME_BYTES:
            raise ValueError("UniversalVC PCM frame exceeds 65536 bytes")
        complete = {**self._common_header(), **header}
        encoded = json.dumps(complete, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_HEADER_BYTES:
            raise ValueError("UniversalVC frame header exceeds 8192 bytes")
        frame = struct.pack(">I", len(encoded)) + encoded + pcm
        if self._ws is None or self._ws.closed:
            raise UniversalVCDisconnected("UniversalVC stream is not connected")
        async with self._send_lock:
            try:
                await self._ws.send_bytes(frame)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise UniversalVCDisconnected("UniversalVC stream send failed") from exc

    async def _request(self, op: str, *, source_id: str = "main", extra: dict[str, Any] | None = None) -> tuple[str, asyncio.Future]:
        if self._terminal_error is not None:
            raise self._terminal_error
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_json({
                "type": "request",
                "op": op,
                "request_id": request_id,
                "source_id": source_id,
                **(extra or {}),
            })
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        return request_id, future

    async def _await_response(self, future: asyncio.Future) -> dict[str, Any]:
        try:
            # A Core playback drain can legitimately take longer than a small
            # request timeout; session heartbeats continue while it completes.
            result = await asyncio.wait_for(future, timeout=90.0)
        except asyncio.CancelledError:
            self._forget_future(future)
            raise
        except asyncio.TimeoutError as exc:
            await self._fail(UniversalVCDisconnected("UniversalVC request timed out"))
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
            raise UniversalVCDisconnected("UniversalVC request timed out") from exc
        if not isinstance(result, dict):
            raise UniversalVCProtocolError("UniversalVC response result was not an object")
        return result

    def _next_seq(self, source_id: str, namespace: str = "input", count: int = 1) -> int:
        key = (source_id, namespace)
        seq = self._sequences.get(key, 0)
        self._sequences[key] = seq + max(1, count)
        return seq

    def _validate_source_id(self, source_id: str) -> str:
        if not isinstance(source_id, str) or source_id not in self._source_ids:
            raise ValueError("source_id was not declared for this UniversalVC session")
        return source_id

    def _forget_future(self, future: asyncio.Future) -> None:
        for request_id, pending in tuple(self._pending.items()):
            if pending is future:
                self._pending.pop(request_id, None)
                break
        if not future.done():
            future.cancel()

    async def _send_audio_request(
        self,
        op: str,
        pcm: bytes,
        *,
        source_id: str,
        extra: dict[str, Any] | None = None,
        max_bytes: int = 60 * 16_000 * 2,
    ) -> dict[str, Any]:
        source_id = self._validate_source_id(source_id)
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError("PCM must be bytes-like")
        raw = bytes(pcm)
        if len(raw) > max_bytes:
            raise ValueError("UniversalVC audio payload exceeds its size limit")
        sequence_namespace = "input" if op in {"send_pcm", "feed_vad"} else op
        frame_count = max(1, (len(raw) + MAX_PCM_FRAME_BYTES - 1) // MAX_PCM_FRAME_BYTES)
        async with self._sequence_locks[source_id]:
            first_sequence = self._next_seq(source_id, sequence_namespace, frame_count)
            request_op = f"{op}_begin" if len(raw) > MAX_PCM_FRAME_BYTES else op
            request_id, future = await self._request(request_op, source_id=source_id, extra=extra)
            try:
                if len(raw) <= MAX_PCM_FRAME_BYTES:
                    await self._write_binary({
                        "type": "audio",
                        "op": op,
                        "request_id": request_id,
                        "source_id": source_id,
                        "seq": first_sequence,
                    }, raw)
                else:
                    for offset in range(0, len(raw), MAX_PCM_FRAME_BYTES):
                        await self._write_binary({
                            "type": "audio",
                            "op": f"{op}_chunk",
                            "request_id": request_id,
                            "source_id": source_id,
                            "seq": first_sequence + offset // MAX_PCM_FRAME_BYTES,
                        }, raw[offset:offset + MAX_PCM_FRAME_BYTES])
                    await self._write_json({
                        "type": "request",
                        "op": f"{op}_end",
                        "request_id": request_id,
                        "source_id": source_id,
                    })
                return await self._await_response(future)
            except BaseException:
                self._forget_future(future)
                raise

    async def send_pcm(self, pcm: bytes, source_id: str = "main") -> dict[str, Any]:
        return await self._send_audio_request("send_pcm", pcm, source_id=source_id, max_bytes=MAX_PCM_FRAME_BYTES)

    async def feed_vad(self, pcm: bytes, source_id: str = "main") -> dict[str, Any]:
        result = await self._send_audio_request("feed_vad", pcm, source_id=source_id, max_bytes=MAX_PCM_FRAME_BYTES)
        speaking = result.get("speaking")
        completed = result.get("completed_pcm", "")
        if type(speaking) is not bool or not isinstance(completed, str):
            raise UniversalVCProtocolError("UniversalVC VAD response is malformed")
        try:
            completed_pcm = base64.b64decode(completed.encode("ascii"), validate=True) if completed else b""
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise UniversalVCProtocolError("UniversalVC VAD audio was invalid base64") from exc
        if len(completed_pcm) > 60 * 16_000 * 2:
            raise UniversalVCProtocolError("UniversalVC VAD result exceeded utterance limit")
        segments_value = result.get("completed_segments")
        completed_segments: tuple[bytes, ...] = ()
        if isinstance(segments_value, list):
            decoded = []
            for item in segments_value:
                if not isinstance(item, str):
                    raise UniversalVCProtocolError("UniversalVC VAD segments are malformed")
                try:
                    data = base64.b64decode(item.encode("ascii"), validate=True)
                except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                    raise UniversalVCProtocolError("UniversalVC VAD segment was invalid base64") from exc
                if len(data) > 60 * 16_000 * 2:
                    raise UniversalVCProtocolError("UniversalVC VAD segment exceeded utterance limit")
                decoded.append(data)
            completed_segments = tuple(decoded)
        joined_segments = b"".join(completed_segments)
        if len(joined_segments) > 60 * 16_000 * 2:
            raise UniversalVCProtocolError("UniversalVC completed segments exceeded utterance limit")
        if completed_pcm and completed_segments and completed_pcm != joined_segments:
            raise UniversalVCProtocolError("UniversalVC VAD segment representations disagree")
        if not completed_pcm and completed_segments:
            completed_pcm = joined_segments
        if not completed_segments and completed_pcm:
            completed_segments = (completed_pcm,)
        return {"speaking": speaking, "completed_pcm": completed_pcm, "completed_segments": completed_segments}

    async def encode_wav(self, pcm: bytes, max_duration_seconds: float = 60) -> str:
        duration = float(max_duration_seconds)
        if not 0 < duration <= 60:
            raise ValueError("max_duration_seconds must be in (0, 60]")
        result = await self._send_audio_request(
            "encode_wav",
            pcm,
            source_id=self._default_source_id,
            extra={"max_duration_seconds": duration},
            max_bytes=int(duration * 16_000 * 2),
        )
        encoded = result.get("wav_base64")
        if not isinstance(encoded, str):
            raise UniversalVCProtocolError("UniversalVC WAV response omitted wav_base64")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise UniversalVCProtocolError("UniversalVC returned invalid WAV base64") from exc
        if not raw or len(raw) > MAX_WAV_BYTES:
            raise UniversalVCProtocolError("UniversalVC WAV response exceeded size limit")
        return encoded

    async def play_audio(self, segment: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(segment, dict):
            raise TypeError("Core audio segment must be a dictionary")
        kind = segment.get("type")
        data = segment.get("data", segment)
        if kind == "voice":
            encoded = data if isinstance(data, str) else data.get("audio_base64") or data.get("audio") if isinstance(data, dict) else None
            raw = _voice_payload(encoded)
            source_id = self._default_source_id
            request_id, future = await self._request(
                "play_voice_begin", source_id=source_id, extra={"audio_bytes": len(raw)}
            )
            try:
                for offset in range(0, len(raw), MAX_PCM_FRAME_BYTES):
                    await self._write_binary({
                        "type": "audio",
                        "op": "play_voice_chunk",
                        "request_id": request_id,
                        "source_id": source_id,
                        "seq": offset // MAX_PCM_FRAME_BYTES,
                    }, raw[offset:offset + MAX_PCM_FRAME_BYTES])
                await self._write_json({"type": "request", "op": "play_voice_end", "request_id": request_id, "source_id": source_id})
                return await self._await_response(future)
            except BaseException:
                self._forget_future(future)
                raise
        if kind == "voice_stream":
            if not isinstance(data, dict):
                raise ValueError("Core voice_stream segment must contain an object")
            event = data.get("event")
            if event not in {"start", "chunk", "end", "abort"}:
                raise ValueError("Core voice_stream event is unknown")
            payload = dict(data)
            encoded = payload.pop("audio_base64", None)
            pcm = b""
            if encoded is not None:
                if not isinstance(encoded, str):
                    raise ValueError("Core voice_stream audio_base64 is malformed")
                try:
                    pcm = base64.b64decode(encoded.encode("ascii"), validate=True)
                except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                    raise ValueError("Core voice_stream audio_base64 is invalid") from exc
            if len(pcm) > MAX_PCM_FRAME_BYTES:
                raise ValueError("Core voice_stream PCM event exceeds transport frame limit")
            source_id = self._default_source_id
            request_id, future = await self._request(
                "play_voice_stream", source_id=source_id, extra={"segment": payload}
            )
            try:
                await self._write_binary({
                    "type": "audio",
                    "op": "play_voice_stream",
                    "request_id": request_id,
                    "source_id": source_id,
                    "seq": int(data.get("seq", 0)) if isinstance(data.get("seq", 0), int) and not isinstance(data.get("seq", 0), bool) else 0,
                    "segment": payload,
                }, pcm)
                return await self._await_response(future)
            except BaseException:
                self._forget_future(future)
                raise
        raise ValueError("Core segment type must be voice or voice_stream")

    async def interrupt(self) -> dict[str, Any]:
        path = f"/v1/sessions/{quote(self.session_id, safe='')}/interrupt"
        result = await self._client._request_json("POST", path, payload={
            "session_generation": self.session_generation,
            "service_instance_id": self.service_instance_id,
        })
        return result

    async def pause_audio(self) -> dict[str, Any]:
        request_id, future = await self._request("pause_audio", source_id=self._default_source_id)
        del request_id
        return await self._await_response(future)

    async def resume_audio(self) -> dict[str, Any]:
        request_id, future = await self._request("resume_audio", source_id=self._default_source_id)
        del request_id
        return await self._await_response(future)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            value = await self._events.get()
            if value is _END:
                if self._terminal_error is not None:
                    raise self._terminal_error
                return
            yield value

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closing and self._terminal_error is None:
                await asyncio.sleep(10.0)
                await self._write_json({"type": "heartbeat", "op": "heartbeat"})
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc if isinstance(exc, UniversalVCClientError) else UniversalVCDisconnected("UniversalVC heartbeat failed")
            await self._fail(error)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        path = f"/v1/sessions/{quote(self.session_id, safe='')}"
        try:
            await self._client._request_json("DELETE", path, allow_missing=True)
        except Exception:
            pass
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        task = self._receive_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._fail(UniversalVCDisconnected("UniversalVC session closed"))
        self._client._sessions.discard(self)
