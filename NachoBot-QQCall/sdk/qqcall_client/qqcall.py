"""Shared, fail-closed QQCall adapter runtime.

The runtime is intentionally an adapter-side client for the local Node control
service. It never attaches a Kernel listener, logs into QQ, or owns the Bridge
call state. Core lifecycle messages are structured senderless system events;
call-targeted outbound messages are consumed until a verified audio path exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import inspect
import json
import logging
import math
import os
import re
import secrets
import time
import aiohttp
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from ncnk_message.message_base import BaseMessageInfo, MessageBase, Seg
from .qqcall_group_wire import validate_group_audio_claim, validate_group_audio_scope
from .qqcall_policy import group_status_decision, match_group_call_trigger
from ncnk_message.system_event import build_system_event, build_system_event_route
from .qqcall_voice import (
    TRUSTED_INVITE_CORRELATIONS,
    QQCallRouteProof,
    QQCallVoiceRuntime,
    is_trusted_invite_correlation,
)
from .qqcall_universal_api import QQCallRouteRejection, QQCallRouteRetired


_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_LOGGER = logging.getLogger(__name__)
_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SERVICE_INSTANCE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_CLIENT_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
_ENDPOINT_HASH_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_PRIVATE_SCOPE_PROFILE = "qq-9.9.26-44343-c2c-invite-type-1"
_PRIVATE_SCOPE_WRAPPER_SHA256 = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2"
_PRIVATE_SCOPE_PROOF_KEYS = frozenset({
    "version", "profile", "wrapperSha256", "callbackSequence", "inviteType", "relationId", "fromUid",
    "targetPid", "kernelCreationTime100ns", "connectionGeneration", "listenerMutationEpoch", "ipcSequence",
    "accountId", "serviceInstance", "hostGeneration", "scopeKind", "callId", "callGeneration", "inviteEventId",
})
_INVITE_EVENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SCOPE_KEYS = frozenset({
    "serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration",
})
_GROUP_SCOPE_KEYS = frozenset({
    "kind", "serviceInstance", "hostGeneration", "connectionGeneration", "groupId", "roomId", "roomGeneration",
})
_GROUP_JOIN_RESULT_KEYS = frozenset({
    "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
    "joined", "scope", "source", "requesterUserId",
})
_GROUP_CURRENT_RESULT_KEYS = frozenset({
    "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
    "active", "scope", "requesterUserId",
})
_GROUP_QUIT_RESULT_KEYS = frozenset({
    "version", "accountId", "serviceInstance", "operation", "result",
})
_META_KEYS = frozenset({"serviceInstance", "hostGeneration", "connectionGeneration"})
_CALL_KEYS = frozenset({
    "callId", "generation", "peerUid", "phase", "uncertain", "inviteCallbackReady",
    "inviteCorrelation", "acceptState",
})
_CALL_PROOF_KEYS = _CALL_KEYS | frozenset({"privateScopeProof"})
_KNOWN_INVITE_CORRELATIONS = TRUSTED_INVITE_CORRELATIONS | frozenset({
    "awaiting_20006",
    "single_active_invite_uncorrelated",
    "room_scope_reused_requires_new_host",
    "room_scope_history_full_requires_new_host",
})
_POLL_KEYS = frozenset({
    "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
    "cursor", "events", "currentCall",
})
_PEER_MANIFEST_KEYS = frozenset({
    "schema", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
    "kernelPid", "kernelCreationTime100ns", "peerMap",
})
_EVENT_KEYS = frozenset({"sequence", "kind", "call"})
_BUSY_EVENT_KEYS = frozenset({"sequence", "kind", "call", "busyInvite"})
_GROUP_INVITE_EVENT_KEYS = frozenset({"sequence", "kind", "call", "groupInvite"})
_GROUP_INVITE_KEYS = frozenset({"inviteId", "groupId", "callerUid", "observedAtMs"})
_BUSY_INVITE_KEYS = frozenset({"callerUid", "identityCorrelation"})
_BUSY_INVITE_PROOF_KEYS = _BUSY_INVITE_KEYS | frozenset({"privateScopeProof"})
_SEMANTIC_KINDS = frozenset({
    "invite_callback_ready", "call_connected", "call_terminal", "controller_invalidated", "stopped",
})
_ALLOWED_OPERATIONS = frozenset(
    {"status", "accept", "hangup", "reject", "join_group", "leave_group", "group_status", "group_audio_status"}
)
_MAX_HTTP_BYTES = 1024 * 1024
_MAX_TOKEN_FILE_BYTES = 64 * 1024
_MAX_INFLIGHT_REQUESTS = 16
_REQUEST_CACHE_SIZE = 128
_HTTP_TIMEOUT_SEC = 4.0
_HTTP_TOTAL_TIMEOUT_SEC = 4.0
_GROUP_STATUS_HTTP_TIMEOUT_SEC = 12.0
_GROUP_JOIN_HTTP_TIMEOUT_SEC = 25.0
_CORE_SEND_TIMEOUT_SEC = 8.0
_MAX_PEER_MANIFEST_BYTES = 1024 * 1024
_AUDIO_PLAN_HASH_PREFIX = b"qqcall-mmdevice-id-v1\0"
_BUSY_NOTICE_TEXT = "当前正在通话，暂时无法接听，请稍后再试。"
_BUSY_NOTICE_COOLDOWN_SEC = 60.0
_BUSY_ATTEMPT_CACHE_SIZE = 256
_BUSY_SENDER_CACHE_SIZE = 128
_DEFERRED_TERMINAL_LIMIT = 16
# Private scope comes only from a per-call native proof bound to the current
# Kernel manifest and resolved invite identity; legacy provider events stay unknown.


class QQCallError(RuntimeError):
    """An error with a safe, public code; never includes request or identity data."""

    def __init__(self, code: str, status_code: int | None = None):
        super().__init__(code)
        self.code = code if _SAFE_CODE_RE.fullmatch(code) else "qqcall_failed"
        self.status_code = status_code


def _is_int(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= 2**53 - 1


def _audio_endpoint_hash(endpoint_id: str) -> str:
    return hashlib.sha256(_AUDIO_PLAN_HASH_PREFIX + endpoint_id.encode("utf-16-le")).hexdigest()


def _json_unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _endpoint(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 512:
        raise ValueError("invalid QQCall endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid QQCall endpoint") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or port is None
        or not 1 <= port <= 65535
    ):
        raise ValueError("invalid QQCall endpoint")
    host = parsed.hostname
    try:
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("QQCall endpoint must be loopback")
    authority = f"[{host}]" if host and ":" in host else host
    return f"http://{authority}:{port}"


def _universal_api_config(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"url", "token_file"}:
        raise ValueError("invalid QQCall UniversalVC API configuration")
    url = value.get("url")
    token_file = value.get("token_file")
    if not isinstance(url, str) or not isinstance(token_file, str) or "\x00" in token_file:
        raise ValueError("invalid QQCall UniversalVC API configuration")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid QQCall UniversalVC API URL") from exc
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc or host is None
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or not 1 <= (port or 0) <= 65535
        or parsed.path not in ("", "/")
    ):
        raise ValueError("invalid QQCall UniversalVC API URL")
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("QQCall UniversalVC API URL must be loopback")
    authority = f"[{host}]" if ":" in host else host
    return MappingProxyType({"url": f"{parsed.scheme}://{authority}:{port}", "token_file": token_file})


@dataclass(frozen=True, slots=True)
class QQCallConfig:
    """Configuration shared by NapCat and SnowLuma QQCall bindings."""

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:6112"
    token_file: str = ""
    account_id: str = ""
    peer_map: Mapping[str, str] = field(default_factory=dict)
    poll_interval_sec: float = 1.0
    audio: Mapping[str, Any] = field(default_factory=dict)
    universal_api: Mapping[str, Any] = field(default_factory=lambda: {
        "url": "http://127.0.0.1:8016", "token_file": "",
    })
    peer_map_file: str = ""
    shared_config_file: str = ""
    adapter_name: str = ""
    auto_select_adapter: bool = False
    auto_accept: bool = False
    platform_manifest_file: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("QQCall enabled must be a boolean")
        object.__setattr__(self, "endpoint", _endpoint(self.endpoint))
        if not isinstance(self.token_file, str) or "\x00" in self.token_file:
            raise ValueError("invalid QQCall token_file")
        if not isinstance(self.account_id, str):
            raise ValueError("invalid QQCall account_id")
        account_id = self.account_id
        if account_id and not _ID_RE.fullmatch(account_id):
            raise ValueError("invalid QQCall account_id")
        object.__setattr__(self, "account_id", account_id)
        if isinstance(self.poll_interval_sec, bool) or not isinstance(self.poll_interval_sec, (int, float)):
            raise ValueError("invalid QQCall poll interval")
        interval = float(self.poll_interval_sec)
        if not math.isfinite(interval) or not 0.1 <= interval <= 5.0:
            raise ValueError("invalid QQCall poll interval")
        object.__setattr__(self, "poll_interval_sec", interval)
        if not isinstance(self.peer_map, Mapping):
            raise ValueError("invalid QQCall peer_map")
        copied: dict[str, str] = {}
        seen_uin: set[str] = set()
        for uid, uin in self.peer_map.items():
            if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
                raise ValueError("invalid QQCall peer_map key")
            if not isinstance(uin, str) or not _ID_RE.fullmatch(uin):
                raise ValueError("invalid QQCall peer_map value")
            if uin in seen_uin:
                raise ValueError("duplicate QQCall peer_map value")
            copied[uid] = uin
            seen_uin.add(uin)
        object.__setattr__(self, "peer_map", MappingProxyType(copied))
        if not isinstance(self.audio, Mapping):
            raise ValueError("invalid QQCall audio configuration")
        if set(self.audio) - {
            "enabled", "capture_device", "capture_mode", "playback_device", "device_scope", "auto_resolve",
            "frame_ms", "queue_ms", "capture_queue_ms", "max_wav_seconds", "max_chunk_bytes",
            "drain_timeout_s", "cancel_timeout_s",
        }:
            raise ValueError("unknown QQCall audio config field")
        if "auto_resolve" in self.audio and type(self.audio["auto_resolve"]) is not bool:
            raise ValueError("invalid QQCall audio auto_resolve")
        object.__setattr__(self, "audio", MappingProxyType(dict(self.audio)))
        object.__setattr__(self, "universal_api", _universal_api_config(self.universal_api))
        for key in ("peer_map_file", "shared_config_file", "platform_manifest_file"):
            value = getattr(self, key)
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"invalid QQCall {key}")
        if self.adapter_name not in ("", "napcat", "snowluma"):
            raise ValueError("invalid QQCall adapter_name")
        if type(self.auto_select_adapter) is not bool or (self.auto_select_adapter and not self.adapter_name):
            raise ValueError("invalid QQCall auto_select_adapter")
        if type(self.auto_accept) is not bool:
            raise ValueError("invalid QQCall auto_accept")
        if self.auto_accept and not self.shared_config_file:
            raise ValueError("auto_accept requires shared policy")
        if self.enabled:
            if not account_id or not self.token_file.strip():
                raise ValueError("enabled QQCall requires account_id and token_file")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "QQCallConfig") -> "QQCallConfig":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("QQCall config must be a mapping")
        allowed = {
            "enabled", "endpoint", "token_file", "account_id", "peer_map", "poll_interval_sec",
            "audio", "universal_api", "peer_map_file", "shared_config_file", "adapter_name", "auto_accept",
            "platform_manifest_file", "auto_select_adapter",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("unknown QQCall config field")
        defaults = cls()
        return cls(
            enabled=value.get("enabled", defaults.enabled),
            endpoint=value.get("endpoint", defaults.endpoint),
            token_file=value.get("token_file", defaults.token_file),
            account_id=value.get("account_id", defaults.account_id),
            peer_map=value.get("peer_map", defaults.peer_map),
            poll_interval_sec=value.get("poll_interval_sec", defaults.poll_interval_sec),
            audio=value.get("audio", defaults.audio),
            universal_api=value.get("universal_api", defaults.universal_api),
            peer_map_file=value.get("peer_map_file", defaults.peer_map_file),
            shared_config_file=value.get("shared_config_file", defaults.shared_config_file),
            adapter_name=value.get("adapter_name", defaults.adapter_name),
            auto_select_adapter=value.get("auto_select_adapter", defaults.auto_select_adapter),
            auto_accept=value.get("auto_accept", defaults.auto_accept),
            platform_manifest_file=value.get("platform_manifest_file", defaults.platform_manifest_file),
        )


def _read_control_token(token_file: str) -> str:
    path = Path(token_file)
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_TOKEN_FILE_BYTES + 1)
    except OSError as exc:
        raise QQCallError("token_file_unavailable") from exc
    if len(raw) > _MAX_TOKEN_FILE_BYTES:
        raise QQCallError("token_file_too_large")
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise QQCallError("token_file_invalid") from exc
    token = text
    if text.startswith("{"):
        try:
            sidecar = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QQCallError("token_file_invalid") from exc
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("ipcToken"), str):
            raise QQCallError("token_file_invalid")
        token = sidecar["ipcToken"].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise QQCallError("token_file_invalid")
    return token


async def _http_post(endpoint: str, path: str, token: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > _MAX_HTTP_BYTES:
        raise QQCallError("request_too_large")
    try:
        total_timeout = (
            _GROUP_JOIN_HTTP_TIMEOUT_SEC
            if path == "/v1/group/join"
            else _GROUP_STATUS_HTTP_TIMEOUT_SEC if path == "/v1/group/status"
            else _HTTP_TOTAL_TIMEOUT_SEC
        )
        read_timeout = (
            _GROUP_JOIN_HTTP_TIMEOUT_SEC
            if path == "/v1/group/join"
            else _GROUP_STATUS_HTTP_TIMEOUT_SEC if path == "/v1/group/status"
            else _HTTP_TIMEOUT_SEC
        )
        timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=_HTTP_TIMEOUT_SEC,
            sock_connect=_HTTP_TIMEOUT_SEC,
            sock_read=read_timeout,
        )
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(
                endpoint + path,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
                allow_redirects=False,
            ) as response:
                status = int(response.status)
                # The local service never redirects. Never forward the bearer
                # through a Location response to another path, port, or host.
                if 300 <= status < 400:
                    raise QQCallError("service_redirect_rejected", status)
                raw_buffer = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    raw_buffer.extend(chunk)
                    if len(raw_buffer) > _MAX_HTTP_BYTES:
                        raise QQCallError("service_response_too_large", status)
                raw = bytes(raw_buffer)
    except QQCallError:
        raise
    except asyncio.TimeoutError as exc:
        raise QQCallError("service_timeout") from exc
    except aiohttp.ClientError as exc:
        raise QQCallError("service_unavailable") from exc
    if len(raw) > _MAX_HTTP_BYTES:
        raise QQCallError("service_response_too_large", status)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QQCallError("service_response_invalid", status) from exc
    if status < 200 or status >= 300:
        code = result.get("code") if isinstance(result, dict) else None
        safe = code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code) else "service_http_error"
        raise QQCallError(safe, status)
    if not isinstance(result, dict):
        raise QQCallError("service_response_invalid", status)
    return result


def _safe_callback_result(value: Any) -> bool:
    if isinstance(value, tuple) and value:
        return value[0] is True
    return value is True


def _decode_additional_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value.encode("utf-8", "ignore")) <= 64 * 1024:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_additional_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        info = raw.get("message_info")
        if isinstance(info, Mapping):
            return _decode_additional_config(info.get("additional_config"))
        return _decode_additional_config(raw.get("additional_config"))
    info = getattr(raw, "message_info", None)
    if info is not None:
        return _decode_additional_config(getattr(info, "additional_config", None))
    return {}


def _delivery_segments(raw: Any) -> list[tuple[str, Any]]:
    """Copy message segments in order without passing group output to QQ send."""
    if isinstance(raw, Mapping):
        info = raw.get("message_info")
        value = (
            info.get("message_segment")
            if isinstance(info, Mapping) and "message_segment" in info
            else raw.get("message_segment")
        )
    else:
        value = getattr(raw, "message_segment", None)
    pending = [value]
    result: list[tuple[str, Any]] = []
    while pending:
        current = pending.pop(0)
        if isinstance(current, Seg):
            kind, data = current.type, current.data
        elif isinstance(current, Mapping) and isinstance(current.get("type"), str):
            kind, data = current["type"], current.get("data")
        else:
            continue
        if kind == "seglist" and isinstance(data, list):
            pending[0:0] = data
        else:
            result.append((kind, data))
    return result


def is_qqcall_delivery_target(raw: Any) -> bool:
    target = _message_additional_config(raw).get("delivery_target")
    return isinstance(target, Mapping) and target.get("kind") in ("qqcall", "qqcall_group")


class QQCallRuntime:
    """One adapter's lease and typed Core request client for shared QQCall."""

    def __init__(
        self,
        config: QQCallConfig | Mapping[str, Any],
        platform: str,
        send_message: Callable[[MessageBase], Awaitable[Any]],
        send_custom: Callable[[str, dict[str, Any]], Awaitable[Any]],
        get_platform_state: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        admit_peer: Callable[[str], bool | Awaitable[bool]],
        *,
        send_busy_private: Callable[[str, str, Callable[[], Awaitable[bool]]], Any] | None = None,
        admit_group: Callable[[str, str], bool | Awaitable[bool]] | None = None,
        dependency_check: Callable[[], Any] | None = None,
        send_call_notice: Callable[[str, str | None, str, Callable[[], Awaitable[bool]]], Any] | None = None,
        transport: Callable[[str, dict[str, Any], str], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        on_status: Callable[[dict[str, Any]], Any] | None = None,
        audio_backend: Any | None = None,
        asr_client: Any | None = None,
        audio_route_verifier: Callable[[Mapping[str, Any], Any], Any] | None = None,
        resolve_peer_info: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = QQCallConfig.from_mapping(config)
        if not isinstance(platform, str) or not platform.strip():
            raise ValueError("invalid QQCall platform")
        if not callable(send_message) or not callable(send_custom) or not callable(get_platform_state) or not callable(admit_peer):
            raise TypeError("invalid QQCall runtime callback")
        self.platform = platform.strip()
        self._send_message = send_message
        self._send_custom = send_custom
        self._get_platform_state = get_platform_state
        self._admit_peer = admit_peer
        self._send_busy_private = send_busy_private
        self._admit_group = admit_group
        self._dependency_check = dependency_check
        self._send_call_notice = send_call_notice
        self._transport = transport
        self._on_status = on_status or (lambda _status: None)
        self._token: str | None = None
        self._client_instance: str | None = None
        self._cursor = 0
        self._bootstrapped = False
        self._lease_claimed = False
        self._identity: tuple[Any, ...] | None = None
        self._identity_account = ""
        self._core_ready = True
        self._deferred_terminal_events: OrderedDict[tuple[Any, ...], tuple[str, dict[str, Any]]] = OrderedDict()
        self._service_meta: dict[str, Any] | None = None
        self._peer_manifest_meta: tuple[str, int, int] | None = None
        self._peer_manifest_kernel: tuple[int, int] | None = None
        self._peer_manifest_map: tuple[tuple[str, str], ...] | None = None
        self._active_call: dict[str, Any] | None = None
        self._group_audio_binding: Any | None = None
        self._group_audio_requester_user_id: str | None = None
        self._group_audio_runtime: Any | None = None
        self._group_join_scope: dict[str, Any] | None = None
        self._group_join_meta: dict[str, Any] | None = None
        self._group_join_client_instance: str | None = None
        self._group_join_identity: tuple[Any, ...] | None = None
        self._group_audio_plan_fingerprint: str | None = None
        self._group_audio_last_failure_code: str | None = None
        self._group_audio_native_quit_unconfirmed = False
        self._group_audio_stop_depth = 0
        self._last_emitted_signature: tuple[Any, ...] | None = None
        self._stop_event = asyncio.Event()
        self._run_task: asyncio.Task[Any] | None = None
        self._running = False
        self._stopped = False
        self._closing = False
        self._blocked = False
        self._last_failure_code: str | None = None
        self._poll_count = 0
        self._event_count = 0
        self._sent_event_count = 0
        self._dropped_event_count = 0
        self._request_lock = asyncio.Lock()
        self._group_leave_lock = asyncio.Lock()
        self._request_cache: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._request_leave_targets: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._request_inflight: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._auto_accept_request_ids: set[str] = set()
        self._auto_accept_attempted_scope: tuple[Any, ...] | None = None
        self._busy_attempts: OrderedDict[tuple[Any, ...], float] = OrderedDict()
        self._busy_sender_times: OrderedDict[str, float] = OrderedDict()
        self._release_lock = asyncio.Lock()
        audio_enabled = self.config.audio.get("enabled") is True
        # A public config string cannot attest to a safe WASAPI endpoint. Every
        # production backend must be resolved from the current supervisor plan.
        self._audio_plan_required = bool(audio_backend is None and audio_enabled)
        self._audio_plan_binding: tuple[Any, ...] | None = None
        self._audio_plan_fingerprint: str | None = None
        audio_backend_factory = None
        if audio_backend is None and self.config.audio.get("enabled") is True:
            audio_backend_factory = self._create_audio_backend_for_turn
        self._voice = QQCallVoiceRuntime(
            platform=self.platform,
            send_message=self._send_message,
            admit_peer=self._admit_peer,
            identity_is_current=self._audio_identity_is_current,
            universal_api=self.config.universal_api,
            backend=audio_backend,
            backend_factory=audio_backend_factory,
            asr_client=asr_client,
            route_verifier=audio_route_verifier if audio_route_verifier is not None else self._verify_audio_route,
            on_status=self._on_audio_status,
            resolve_peer_info=resolve_peer_info,
        )

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any], **kwargs: Any) -> "QQCallRuntime":
        return cls(config=QQCallConfig.from_mapping(config), **kwargs)

    def status(self) -> dict[str, Any]:
        call = self._active_call
        audio_status = self._voice.status()
        audio_turn_id = None
        if isinstance(call, Mapping) and isinstance(call.get("scope"), Mapping):
            audio_turn_id = self._voice.current_turn_id(call["scope"])
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "stopped": self._stopped,
            "closing": self._closing,
            "blocked": self._blocked,
            "accountVerified": bool(self._identity and self._identity_account == self.config.account_id),
            "coreReady": self._core_ready,
            "leaseClaimed": self._lease_claimed,
            "autoAccept": self.config.auto_accept,
            "cursor": self._cursor,
            "lastFailureCode": self._last_failure_code,
            "audioTurnId": audio_turn_id,
            "groupAudio": self._group_audio_runtime.status() if self._group_audio_runtime is not None else {
                "state": "quarantined" if self._group_join_scope is not None else "stopped",
                "ready": False,
                "acceptedTurns": 0,
                "playbacks": 0,
                "lastPlaybackDrained": False,
                "lastFailureCode": self._group_audio_last_failure_code,
                "nativeQuitUnconfirmed": self._group_audio_native_quit_unconfirmed,
            },
            "counts": {
                "polls": self._poll_count,
                "events": self._event_count,
                "sentEvents": self._sent_event_count,
                "droppedEvents": self._dropped_event_count,
                "cachedRequests": len(self._request_cache),
                "inflightRequests": len(self._request_inflight),
            },
            "activeCall": self._call_public(call) if call and call.get("peer_user_id") else None,
            "audio": audio_status,
        }

    async def _audio_identity_is_current(self, identity: Mapping[str, Any]) -> bool:
        try:
            current = await self._read_platform_state()
            return bool(
                self._same_identity(identity, current)
                and identity.get("connected") is True
                and current.get("connected") is True
                and current.get("core_ready") is True
                and current.get("account_id") == self.config.account_id
            )
        except QQCallError:
            return False

    def _audio_identity(self) -> dict[str, Any] | None:
        if self._identity is None:
            return None
        return {
            "identity": self._identity,
            "account_id": self._identity_account,
            "connected": self._core_ready,
            "core_ready": self._core_ready,
        }

    def _sync_audio_call(self) -> None:
        if self._closing or self._stopped:
            self._voice.update_call(None, None)
        elif not self._core_ready:
            # The platform socket and lease remain live independently of Core.
            # Dropping the voice identity stops capture and drains in-flight
            # ASR/TTS work until the Core route is healthy again.
            self._voice.update_call(None, None)
        else:
            self._voice.update_call(self._active_call, self._audio_identity())

    async def _audio_route_context_is_current(
        self,
        scope: Mapping[str, Any],
        client_instance: str,
        identity: Mapping[str, Any],
        *,
        failure_sink: Callable[[str], Any] | None = None,
    ) -> bool:
        call = self._active_call
        if (
            self._stopped
            or not self.config.enabled
            or not self._lease_claimed
            or not self._service_meta
            or self._client_instance != client_instance
        ):
            if failure_sink is not None:
                failure_sink("route_identity_unavailable")
            return False
        if (
            not self._runtime_identity_matches(identity)
            or identity.get("connected") is not True
            or identity.get("core_ready") is False
            or identity.get("account_id") != self.config.account_id
        ):
            if failure_sink is not None:
                failure_sink("route_identity_mismatch")
            return False
        if (
            call is None
            or call.get("phase") != "connected"
            or call.get("uncertain") is not False
            or call.get("inviteCallbackReady") is not True
            or not is_trusted_invite_correlation(call.get("inviteCorrelation"))
            or not isinstance(call.get("peer_user_id"), str)
            or not _ID_RE.fullmatch(call["peer_user_id"])
            or not self._scope_matches(dict(scope), call.get("scope"))
            or any(self._service_meta.get(key) != scope.get(key) for key in _META_KEYS)
        ):
            if failure_sink is not None:
                failure_sink("route_scope_unverified")
            return False
        try:
            unchanged = await self._identity_unchanged(identity, require_verified=True)
        except Exception:
            if failure_sink is not None:
                failure_sink("route_identity_mismatch")
            return False
        if not unchanged and failure_sink is not None:
            failure_sink("route_identity_mismatch")
        return unchanged

    def _audio_turn_matches_runtime(self, turn: Any) -> bool:
        call = self._active_call
        identity = self._audio_identity()
        return bool(
            not self._closing
            and not self._stopped
            and self._core_ready
            and isinstance(call, Mapping)
            and self._scope_matches(dict(turn.scope), call.get("scope"))
            and isinstance(identity, Mapping)
            and dict(turn.identity) == dict(identity)
        )

    def _read_current_audio_plan(self) -> tuple[dict[str, Any], str]:
        if not self._audio_plan_required or not self.config.peer_map_file:
            raise QQCallError("audio_plan_unavailable")
        meta = self._service_meta
        if not isinstance(meta, Mapping):
            raise QQCallError("audio_plan_unavailable")
        try:
            from .qqcall_service import _validate_runtime_private_file

            _validate_runtime_private_file(
                Path(self.config.peer_map_file), "adapter-runtime.private.json"
            )
            self._read_peer_map(meta)
        except QQCallError:
            raise
        except ValueError as exc:
            code = str(exc)
            if not _SAFE_CODE_RE.fullmatch(code):
                code = "audio_plan_invalid"
            raise QQCallError(code) from None
        kernel_identity = self._peer_manifest_kernel
        if kernel_identity is None:
            raise QQCallError("audio_plan_unavailable")
        try:
            from .qqcall_service import read_qqcall_audio_plan

            plan = read_qqcall_audio_plan(
                self.config.peer_map_file,
                account_id=self.config.account_id,
                service_meta=meta,
                kernel_identity=kernel_identity,
            )
        except ValueError as exc:
            code = str(exc)
            if not _SAFE_CODE_RE.fullmatch(code):
                code = "audio_plan_invalid"
            raise QQCallError(code) from None
        except Exception as exc:
            raise QQCallError(self._error_code(exc, "audio_plan_unavailable")) from None

        binding = tuple(meta.get(key) for key in ("serviceInstance", "hostGeneration", "connectionGeneration"))
        fingerprint = hashlib.sha256(
            json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        if self._audio_plan_binding == binding and self._audio_plan_fingerprint not in (None, fingerprint):
            raise QQCallError("audio_plan_replaced")
        if self._audio_plan_binding != binding:
            self._audio_plan_binding = binding
            self._audio_plan_fingerprint = fingerprint
        elif self._audio_plan_fingerprint is None:
            self._audio_plan_fingerprint = fingerprint
        return plan, fingerprint

    async def _create_audio_backend_for_turn(self, turn: Any) -> Any:
        if not self._audio_turn_matches_runtime(turn):
            raise QQCallError("audio_scope_stale")
        client_instance = self._client_instance
        identity = dict(turn.identity)
        if not isinstance(client_instance, str) or not await self._audio_route_context_is_current(
            turn.scope, client_instance, identity
        ):
            raise QQCallError("audio_scope_stale")
        if not self._audio_turn_matches_runtime(turn):
            raise QQCallError("audio_scope_stale")
        plan, _fingerprint = self._read_current_audio_plan()
        if not self._audio_turn_matches_runtime(turn):
            raise QQCallError("audio_scope_stale")
        try:
            from .qqcall_audio import QQCallAudioBackend, QQCallAudioConfig

            resolved_config = QQCallAudioConfig.from_mapping(plan["audio"])
            return QQCallAudioBackend(resolved_config)
        except Exception as exc:
            raise QQCallError(self._error_code(exc, "audio_plan_invalid")) from None

    async def _verify_audio_route(
        self,
        scope: Mapping[str, Any],
        audio_config: Any,
    ) -> QQCallRouteProof | QQCallRouteRejection | QQCallRouteRetired | bool:
        """Validate one current Host route proof; no config value can attest it."""
        if isinstance(scope, Mapping) and set(scope) == _GROUP_SCOPE_KEYS:
            return await self._verify_group_audio_route(scope, audio_config)
        if not isinstance(scope, Mapping) or set(scope) != _SCOPE_KEYS:
            return QQCallRouteRejection("route_scope_invalid")

        route_stage_recorder = None
        voice_runtime = getattr(self, "_voice", None)
        recorder_factory = getattr(voice_runtime, "make_route_stage_recorder", None)
        if callable(recorder_factory):
            try:
                candidate = recorder_factory(scope)
            except Exception:
                candidate = None
            if callable(candidate):
                route_stage_recorder = candidate

        def record_route_stage(stage: str) -> None:
            if route_stage_recorder is None:
                return
            try:
                route_stage_recorder(stage)
            except Exception:
                # Instrumentation is deliberately inert with respect to route checks.
                return

        plan: dict[str, Any] | None = None
        plan_fingerprint: str | None = None
        expected_input_hash: str | None = None
        expected_output_hash: str | None = None
        if self._audio_plan_required:
            try:
                plan, plan_fingerprint = self._read_current_audio_plan()
            except Exception as exc:
                reason = "route_plan_changed" if isinstance(exc, QQCallError) and exc.code == "audio_plan_replaced" else "route_plan_unavailable"
                return QQCallRouteRejection(reason)
            try:
                plan_audio = plan["audio"]
                plan_scope = plan_audio["device_scope"]
                expected_input_hash = _audio_endpoint_hash(plan["captureEndpointId"])
                expected_output_hash = _audio_endpoint_hash(plan["renderEndpointId"])
            except (KeyError, TypeError):
                return QQCallRouteRejection("route_plan_unavailable")
            if (
                getattr(audio_config, "capture_device", None) != plan_audio["capture_device"]
                or getattr(audio_config, "playback_device", None) != plan_audio["playback_device"]
            ):
                return QQCallRouteRejection("route_plan_mismatch")
        capture_mode = getattr(audio_config, "capture_mode", None)
        if capture_mode != "loopback":
            # The current Host evidence proves active QQ endpoint sessions but
            # does not prove a configured physical/input-capture mapping.
            return QQCallRouteRejection("route_capture_mode_unverified")
        device_scope = getattr(audio_config, "device_scope", None)
        if isinstance(device_scope, Mapping):
            capture_render_name = device_scope.get("capture_render_name")
            playback_render_name = device_scope.get("playback_render_name")
        else:
            capture_render_name = getattr(device_scope, "capture_render_name", None)
            playback_render_name = getattr(device_scope, "playback_render_name", None)
        if (
            not isinstance(capture_render_name, str)
            or not capture_render_name
            or not isinstance(playback_render_name, str)
            or not playback_render_name
        ):
            return QQCallRouteRejection("route_device_scope_invalid")
        if self._audio_plan_required:
            plan_scope = plan["audio"]["device_scope"] if plan is not None else {}
            if (
                capture_render_name != plan_scope.get("capture_render_name")
                or playback_render_name != plan_scope.get("playback_render_name")
            ):
                return QQCallRouteRejection("route_plan_mismatch")

        call_scope = dict(scope)
        client_instance = self._client_instance
        if (
            not isinstance(client_instance, str)
            or not _CLIENT_INSTANCE_RE.fullmatch(client_instance)
            or not self._token
            or not self._lease_claimed
        ):
            return QQCallRouteRejection("route_identity_unavailable")
        identity = {
            "identity": self._identity,
            "account_id": self._identity_account,
            "connected": True,
        }
        context_failure: list[str] = []
        record_route_stage("pre_context")
        try:
            context_current = await self._audio_route_context_is_current(
                call_scope, client_instance, identity, failure_sink=context_failure.append,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            context_current = False
        if not context_current:
            return QQCallRouteRejection(context_failure[0] if context_failure else "route_context_unverified")
        if self._audio_plan_required:
            try:
                _current_plan, current_fingerprint = self._read_current_audio_plan()
            except Exception as exc:
                reason = "route_plan_changed" if isinstance(exc, QQCallError) and exc.code == "audio_plan_replaced" else "route_plan_unavailable"
                return QQCallRouteRejection(reason)
            if current_fingerprint != plan_fingerprint:
                return QQCallRouteRejection("route_plan_changed")
        payload = {
            "accountId": self.config.account_id,
            "clientInstance": client_instance,
            "scope": call_scope,
            "captureRenderName": capture_render_name,
            "playbackRenderName": playback_render_name,
        }
        record_route_stage("helper_request")
        try:
            response = await self._post("/v1/audio/route", payload)
        except asyncio.CancelledError:
            raise
        except QQCallError as exc:
            if (
                type(exc) is QQCallError
                and type(exc.status_code) is int
                and exc.status_code == 409
                and type(exc.code) is str
                and exc.code == "call_scope_stale"
            ):
                return QQCallRouteRetired()
            return QQCallRouteRejection(
                "route_helper_transport_error",
                _helper_http_status=exc.status_code,
                _helper_error_code=exc.code,
            )
        except Exception:
            return QQCallRouteRejection("route_helper_transport_error")
        context_failure = []
        record_route_stage("post_context")
        try:
            context_current = await self._audio_route_context_is_current(
                call_scope, client_instance, identity, failure_sink=context_failure.append,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            context_current = False
        if not context_current:
            return QQCallRouteRejection(context_failure[0] if context_failure else "route_context_unverified")
        if self._audio_plan_required:
            try:
                _current_plan, current_fingerprint = self._read_current_audio_plan()
            except Exception as exc:
                reason = "route_plan_changed" if isinstance(exc, QQCallError) and exc.code == "audio_plan_replaced" else "route_plan_unavailable"
                return QQCallRouteRejection(reason)
            if current_fingerprint != plan_fingerprint:
                return QQCallRouteRejection("route_plan_changed")
        record_route_stage("proof_validation")
        if not isinstance(response, dict):
            return QQCallRouteRejection("route_response_invalid")
        version = response.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            return QQCallRouteRejection("route_response_invalid")
        if response.get("accountId") != self.config.account_id:
            return QQCallRouteRejection("route_identity_mismatch")
        if not self._scope_matches(response.get("scope"), call_scope):
            return QQCallRouteRejection("route_scope_mismatch")
        if type(response.get("verified")) is not bool:
            return QQCallRouteRejection("route_response_invalid")
        if response["verified"] is False:
            # A negative proof is never promoted to readiness. Validate its
            # bounded public shape; the immutable rejection publishes only a
            # hardcoded, recognized helper code to route diagnostics.
            if set(response) != {"version", "accountId", "scope", "verified", "code"}:
                return QQCallRouteRejection("route_response_invalid")
            return QQCallRouteRejection.from_helper_code(response.get("code"))
        expected_fields = {
            "version", "accountId", "scope", "verified", "source", "hostPid", "observedAtMs",
            "hostInput", "hostOutput",
        }
        if set(response) != expected_fields or response.get("source") != "windows_active_sessions":
            return QQCallRouteRejection("route_proof_invalid")
        host_pid = response.get("hostPid")
        observed_at_ms = response.get("observedAtMs")
        if not _is_int(host_pid) or not _is_int(observed_at_ms):
            return QQCallRouteRejection("route_proof_invalid")

        def valid_session(
            value: Any,
            *,
            flow: str,
            name: str,
            expected_endpoint_hash: str | None = None,
        ) -> bool:
            return bool(
                isinstance(value, dict)
                and set(value) == {"deviceName", "flow", "state", "endpointIdHash", "processId"}
                and value.get("deviceName") == name
                and value.get("flow") == flow
                and value.get("state") == "active"
                and isinstance(value.get("endpointIdHash"), str)
                and _ENDPOINT_HASH_RE.fullmatch(value["endpointIdHash"])
                and (
                    expected_endpoint_hash is None
                    or value["endpointIdHash"].casefold() == expected_endpoint_hash.casefold()
                )
                and _is_int(value.get("processId"))
            )

        # QQ's active microphone is the selected playback endpoint, while its
        # speaker is the selected render endpoint captured through loopback.
        if not valid_session(
            response.get("hostInput"),
            flow="capture",
            name=playback_render_name,
            expected_endpoint_hash=expected_input_hash,
        ):
            return QQCallRouteRejection("route_proof_invalid")
        if not valid_session(
            response.get("hostOutput"),
            flow="render",
            name=capture_render_name,
            expected_endpoint_hash=expected_output_hash,
        ):
            return QQCallRouteRejection("route_proof_invalid")
        now_ms = time.time_ns() // 1_000_000
        age_ms = now_ms - observed_at_ms
        if age_ms < -250:
            return QQCallRouteRejection("route_proof_invalid")
        if age_ms > 2_500:
            return QQCallRouteRejection("route_proof_stale")
        observed_at_monotonic = time.monotonic() - max(0, age_ms) / 1_000
        record_route_stage("completed")
        return QQCallRouteProof(observed_at_monotonic=observed_at_monotonic)

    def _group_audio_local_context_current(
        self,
        scope: Mapping[str, Any],
        requester: str,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
    ) -> bool:
        runtime = self._group_audio_runtime
        return bool(
            not self._closing
            and not self._stopped
            and self.config.enabled
            and self._lease_claimed
            and self._client_instance == client_instance
            and self._group_join_client_instance == client_instance
            and self._service_meta == dict(service_meta)
            and self._group_join_meta == dict(service_meta)
            and self._group_join_scope == dict(scope)
            and self._group_audio_requester_user_id == requester
            and self._runtime_identity_matches(identity)
            and self._group_join_identity == tuple(identity.get("identity") or ())
            and self._core_ready
            and identity.get("core_ready") is not False
            and self._active_call is None
            and self._voice.status().get("ready") is not True
            and (runtime is None or runtime.state in {"starting", "active"})
        )

    async def _read_native_group_current(
        self,
        scope: Mapping[str, Any],
        requester: str,
        service_meta: Mapping[str, Any],
        client_instance: str,
    ) -> bool:
        """Independently confirm the driver-reported current joined room."""
        response = await self._post("/v1/group/current", {
            "accountId": self.config.account_id,
            "clientInstance": client_instance,
            "scope": dict(scope),
        })
        if (
            type(response) is not dict
            or set(response) != _GROUP_CURRENT_RESULT_KEYS
            or type(response.get("version")) is not int
            or response.get("version") != 1
            or response.get("accountId") != self.config.account_id
            or not _is_int(response.get("hostGeneration"))
            or not _is_int(response.get("connectionGeneration"))
            or any(response.get(key) != service_meta.get(key) for key in (
                "serviceInstance", "hostGeneration", "connectionGeneration",
            ))
            or type(response.get("active")) is not bool
        ):
            _LOGGER.warning("QQCall group current rejected stage=response_contract")
            return False
        if response["active"] is not True:
            _LOGGER.warning("QQCall group current rejected stage=native_inactive")
            return False
        try:
            current_scope = validate_group_audio_scope(response.get("scope"))
        except (TypeError, ValueError):
            _LOGGER.warning("QQCall group current rejected stage=scope_contract")
            return False
        matched = bool(current_scope == dict(scope) and response.get("requesterUserId") == requester)
        if not matched:
            _LOGGER.warning("QQCall group current rejected stage=owner_mismatch")
        return matched

    async def _native_group_is_absent(
        self,
        scope: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
    ) -> bool:
        if (
            not self._lease_claimed
            or self._client_instance != client_instance
            or self._service_meta != dict(service_meta)
        ):
            return False
        try:
            response = await self._post("/v1/group/current", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "scope": dict(scope),
            })
        except Exception:
            return False
        current_absent = bool(
            type(response) is dict
            and set(response) == _GROUP_CURRENT_RESULT_KEYS
            and type(response.get("version")) is int
            and response.get("version") == 1
            and response.get("accountId") == self.config.account_id
            and _is_int(response.get("hostGeneration"))
            and _is_int(response.get("connectionGeneration"))
            and all(response.get(key) == service_meta.get(key) for key in _META_KEYS)
            and response.get("active") is False
            and response.get("scope") is None
            and response.get("requesterUserId") is None
        )
        if not current_absent:
            return False
        if (
            not self._lease_claimed
            or self._client_instance != client_instance
            or self._service_meta != dict(service_meta)
        ):
            return False
        # current=false only fences this group's audio route. Ending,
        # quarantined, and suspended drivers may still own the global slot.
        # Release the local native owner only after the independent occupancy
        # authority confirms terminal global absence under the same lease.
        return await self._native_slot_is_absent(service_meta, client_instance)

    async def _native_slot_is_absent(
        self, service_meta: Mapping[str, Any], client_instance: str,
    ) -> bool:
        if (not self._lease_claimed or self._client_instance != client_instance
                or self._service_meta != dict(service_meta) or self._closing or self._stopped):
            return False
        try:
            occupancy = await self._post("/v1/call/occupancy", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
            })
        except Exception:
            return False
        return bool(
            type(occupancy) is dict
            and set(occupancy) == {
                "version", "accountId", "serviceInstance", "hostGeneration",
                "connectionGeneration", "occupied", "kind",
            }
            and type(occupancy.get("version")) is int
            and occupancy.get("version") == 1
            and occupancy.get("accountId") == self.config.account_id
            and _is_int(occupancy.get("hostGeneration"))
            and _is_int(occupancy.get("connectionGeneration"))
            and all(occupancy.get(key) == service_meta.get(key) for key in _META_KEYS)
            and occupancy.get("occupied") is False
            and occupancy.get("kind") is None
            and self._lease_claimed
            and self._client_instance == client_instance
            and self._service_meta == dict(service_meta)
            and not self._closing
            and not self._stopped
        )

    async def _reconcile_group_quarantine(self, identity: Mapping[str, Any]) -> bool:
        """Retire a stopped local owner only after fresh native absence proof."""
        if (not self._group_audio_native_quit_unconfirmed
                or self._group_audio_runtime is not None or self._group_audio_binding is not None
                or self._group_audio_stop_depth or self._closing or self._stopped
                or not isinstance(self._group_join_scope, Mapping)
                or not isinstance(self._group_join_meta, Mapping)
                or not isinstance(self._service_meta, Mapping)
                or not isinstance(self._client_instance, str) or not self._lease_claimed):
            return False
        scope = dict(self._group_join_scope)
        old_meta = dict(self._group_join_meta)
        meta = dict(self._service_meta)
        client = self._client_instance
        owner = (self._group_join_client_instance, self._group_join_identity, self._group_audio_requester_user_id)

        def unchanged() -> bool:
            return bool(
                self._group_audio_native_quit_unconfirmed and not self._group_audio_stop_depth
                and self._group_audio_runtime is None and self._group_audio_binding is None
                and self._group_join_scope == scope and self._group_join_meta == old_meta
                and (self._group_join_client_instance, self._group_join_identity, self._group_audio_requester_user_id) == owner
                and self._service_meta == meta and self._client_instance == client and self._lease_claimed
                and not self._closing and not self._stopped and self._runtime_identity_matches(identity)
            )

        try:
            if not unchanged() or not await self._identity_unchanged(identity, require_verified=True):
                return False
            if old_meta == meta:
                absent = await self._native_group_is_absent(scope, meta, client)
            else:
                # A verified supervisor manifest and strictly newer native
                # generations retire the old authority. A new lease alone,
                # an arbitrary UUID change, or a regressed generation cannot.
                newer = (self.config.peer_map_file and meta.get("serviceInstance") != old_meta.get("serviceInstance")
                         and all(_is_int(meta.get(k)) and _is_int(old_meta.get(k)) and meta[k] > old_meta[k]
                                 for k in ("hostGeneration", "connectionGeneration")))
                absent = bool(newer) and await self._native_slot_is_absent(meta, client)
            if not absent or not unchanged() or not await self._identity_unchanged(identity, require_verified=True) or not unchanged():
                return False
        except Exception:  # noqa: BLE001 - failed recovery probes must preserve the quarantined owner.
            return False
        old_failure = self._group_audio_last_failure_code
        self._group_join_scope = None
        self._group_join_meta = None
        self._group_join_client_instance = None
        self._group_join_identity = None
        self._group_audio_requester_user_id = None
        self._group_audio_plan_fingerprint = None
        self._group_audio_native_quit_unconfirmed = False
        self._group_audio_last_failure_code = None
        if self._last_failure_code in {old_failure, "group_audio_busy", "group_audio_unavailable"}:
            self._last_failure_code = None
        self._emit_status("group_terminal_reconciled")
        return True

    async def _group_audio_fresh_context_current(
        self,
        scope: Mapping[str, Any],
        requester: str,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
        expected_fingerprint: str,
    ) -> bool:
        # Keep the rejection stage local to this verification: overlapping status
        # reads must not overwrite the cause. Never log identities or payloads.
        stage = "local_before"

        def rejected() -> bool:
            _LOGGER.warning("QQCall group context rejected stage=%s", stage)
            return False

        if not self._group_audio_local_context_current(scope, requester, identity, service_meta, client_instance):
            return rejected()
        try:
            stage = "identity_before"
            if not await self._identity_unchanged(identity, require_verified=True):
                return rejected()
            stage = "core_before"
            if not await self._core_route_is_current(identity):
                return rejected()
            stage = "admission_before"
            await self._admitted_group(requester, str(scope["groupId"]), identity)
            stage = "local_after_admission"
            if not self._group_audio_local_context_current(scope, requester, identity, service_meta, client_instance):
                return rejected()
            stage = "native_current"
            if not await self._read_native_group_current(scope, requester, service_meta, client_instance):
                return rejected()
            stage = "admission_after"
            await self._admitted_group(requester, str(scope["groupId"]), identity)
            stage = "identity_after"
            if not await self._identity_unchanged(identity, require_verified=True):
                return rejected()
            stage = "core_after"
            if not await self._core_route_is_current(identity):
                return rejected()
            stage = "plan_read"
            _plan, current_fingerprint = self._read_current_audio_plan()
            stage = "plan_changed"
            if current_fingerprint != expected_fingerprint:
                return rejected()
            stage = "local_after"
            if not self._group_audio_local_context_current(scope, requester, identity, service_meta, client_instance):
                return rejected()
            return True
        except (asyncio.CancelledError,):
            raise
        except Exception as exc:
            # Fixed classes only; exception text/codes may contain external data.
            kind = "qqcall_error" if isinstance(exc, QQCallError) else "unexpected_error"
            _LOGGER.warning("QQCall group context exception stage=%s kind=%s", stage, kind)
            return False

    async def _verify_group_audio_route(
        self,
        scope_value: Mapping[str, Any],
        audio_config: Any,
    ) -> QQCallRouteProof | QQCallRouteRejection | QQCallRouteRetired | bool:
        """Verify the same Windows active-session evidence for one exact group scope."""
        if not isinstance(scope_value, Mapping) or set(scope_value) != _GROUP_SCOPE_KEYS:
            return QQCallRouteRejection("route_scope_invalid")
        try:
            scope = validate_group_audio_scope(dict(scope_value))
        except (TypeError, ValueError):
            return QQCallRouteRejection("route_scope_invalid")
        requester = self._group_audio_requester_user_id
        service_meta = self._group_join_meta
        client_instance = self._group_join_client_instance
        identity = {
            "identity": self._group_join_identity,
            "account_id": self.config.account_id,
            "connected": True,
            "core_ready": self._core_ready,
        }
        expected_fingerprint = self._group_audio_plan_fingerprint
        if (
            not isinstance(requester, str)
            or not isinstance(service_meta, Mapping)
            or not isinstance(client_instance, str)
            or not isinstance(expected_fingerprint, str)
            or not self._audio_plan_required
        ):
            return QQCallRouteRejection("route_identity_unavailable")
        try:
            plan, fingerprint = self._read_current_audio_plan()
            plan_audio = plan["audio"]
            plan_scope = plan_audio["device_scope"]
            expected_input_hash = _audio_endpoint_hash(plan["captureEndpointId"])
            expected_output_hash = _audio_endpoint_hash(plan["renderEndpointId"])
        except Exception:
            return QQCallRouteRejection("route_plan_unavailable")
        if fingerprint != expected_fingerprint:
            return QQCallRouteRejection("route_plan_changed")
        if (
            getattr(audio_config, "capture_device", None) != plan_audio.get("capture_device")
            or getattr(audio_config, "playback_device", None) != plan_audio.get("playback_device")
        ):
            return QQCallRouteRejection("route_plan_mismatch")
        if getattr(audio_config, "capture_mode", None) != "loopback":
            return QQCallRouteRejection("route_capture_mode_unverified")
        device_scope = getattr(audio_config, "device_scope", None)
        if isinstance(device_scope, Mapping):
            capture_render_name = device_scope.get("capture_render_name")
            playback_render_name = device_scope.get("playback_render_name")
        else:
            capture_render_name = getattr(device_scope, "capture_render_name", None)
            playback_render_name = getattr(device_scope, "playback_render_name", None)
        if (
            not isinstance(capture_render_name, str)
            or not capture_render_name
            or not isinstance(playback_render_name, str)
            or not playback_render_name
            or capture_render_name != plan_scope.get("capture_render_name")
            or playback_render_name != plan_scope.get("playback_render_name")
        ):
            return QQCallRouteRejection("route_plan_mismatch")
        if not _CLIENT_INSTANCE_RE.fullmatch(client_instance) or not self._token:
            return QQCallRouteRejection("route_identity_unavailable")
        if not await self._group_audio_fresh_context_current(
            scope, requester, identity, service_meta, client_instance, expected_fingerprint,
        ):
            return QQCallRouteRejection("route_context_unverified")
        payload = {
            "accountId": self.config.account_id,
            "clientInstance": client_instance,
            "scope": dict(scope),
            "captureRenderName": capture_render_name,
            "playbackRenderName": playback_render_name,
        }
        try:
            response = await self._post("/v1/audio/route", payload)
        except asyncio.CancelledError:
            raise
        except QQCallError as exc:
            if type(exc.status_code) is int and exc.status_code == 409 and exc.code == "call_scope_stale":
                return QQCallRouteRetired()
            return QQCallRouteRejection("route_helper_transport_error",
                                        _helper_http_status=exc.status_code, _helper_error_code=exc.code)
        except Exception:
            return QQCallRouteRejection("route_helper_transport_error")
        if not await self._group_audio_fresh_context_current(
            scope, requester, identity, service_meta, client_instance, expected_fingerprint,
        ):
            return QQCallRouteRejection("route_context_unverified")
        if (
            type(response) is not dict
            or type(response.get("version")) is not int
            or response.get("version") != 1
            or response.get("accountId") != self.config.account_id
            or response.get("scope") != scope
            or type(response.get("verified")) is not bool
        ):
            return QQCallRouteRejection("route_response_invalid")
        if response["verified"] is False:
            if set(response) != {"version", "accountId", "scope", "verified", "code"}:
                return QQCallRouteRejection("route_response_invalid")
            return QQCallRouteRejection.from_helper_code(response.get("code"))
        expected_fields = {
            "version", "accountId", "scope", "verified", "source", "hostPid", "observedAtMs",
            "hostInput", "hostOutput",
        }
        if (
            set(response) != expected_fields
            or response.get("source") != "windows_active_sessions"
            or not _is_int(response.get("hostPid"))
            or not _is_int(response.get("observedAtMs"))
        ):
            return QQCallRouteRejection("route_proof_invalid")

        def valid_session(value: Any, *, flow: str, name: str, endpoint_hash: str) -> bool:
            return bool(
                type(value) is dict
                and set(value) == {"deviceName", "flow", "state", "endpointIdHash", "processId"}
                and value.get("deviceName") == name
                and value.get("flow") == flow
                and value.get("state") == "active"
                and isinstance(value.get("endpointIdHash"), str)
                and _ENDPOINT_HASH_RE.fullmatch(value["endpointIdHash"])
                and value["endpointIdHash"].casefold() == endpoint_hash.casefold()
                and _is_int(value.get("processId"))
            )

        if not valid_session(
            response.get("hostInput"), flow="capture", name=playback_render_name, endpoint_hash=expected_input_hash,
        ) or not valid_session(
            response.get("hostOutput"), flow="render", name=capture_render_name, endpoint_hash=expected_output_hash,
        ):
            return QQCallRouteRejection("route_proof_invalid")
        age_ms = (time.time_ns() // 1_000_000) - response["observedAtMs"]
        if age_ms < -250:
            return QQCallRouteRejection("route_proof_invalid")
        if age_ms > 2_000:
            return QQCallRouteRejection("route_proof_stale")
        return QQCallRouteProof(observed_at_monotonic=time.monotonic() - max(0, age_ms) / 1_000)

    def _on_audio_status(self, event: dict[str, Any]) -> None:
        kind = event.get("kind") if isinstance(event, dict) else "audio_status"
        self._emit_status(str(kind or "audio_status"))

    def _call_public(self, call: Mapping[str, Any]) -> dict[str, Any]:
        scope = call.get("scope")
        return {
            "callId": call["callId"],
            "generation": call["generation"],
            "phase": call["phase"],
            "uncertain": call["uncertain"],
            "inviteCallbackReady": call["inviteCallbackReady"],
            "inviteCorrelation": call["inviteCorrelation"],
            "acceptState": call["acceptState"],
            "audioTurnId": self._voice.current_turn_id(scope) if isinstance(scope, Mapping) else None,
        }

    async def _call(self, callback: Callable[..., Any], *args: Any) -> Any:
        value = callback(*args)
        return await value if inspect.isawaitable(value) else value

    async def _read_platform_state(self) -> dict[str, Any]:
        try:
            value = await self._call(self._get_platform_state)
        except Exception as exc:
            raise QQCallError("platform_state_unavailable") from exc
        if not isinstance(value, Mapping):
            raise QQCallError("platform_state_invalid")
        identity_value = value.get("identity")
        if not isinstance(identity_value, (tuple, list)) or not identity_value:
            identity: tuple[Any, ...] = ()
        else:
            identity = tuple(identity_value)
        account_id = value.get("account_id")
        if not isinstance(account_id, str):
            account_id = ""
        return {
            "connected": value.get("connected") is True,
            "core_ready": value.get("core_ready") is True if "core_ready" in value else True,
            "account_id": account_id,
            "identity": identity,
        }

    @staticmethod
    def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return bool(left.get("identity")) and left.get("identity") == right.get("identity") and left.get("account_id") == right.get("account_id")

    async def _identity_unchanged(self, expected: Mapping[str, Any], *, require_verified: bool) -> bool:
        current = await self._read_platform_state()
        same = self._same_identity(expected, current)
        if require_verified:
            same = same and current["connected"] and current["account_id"] == self.config.account_id
        return same

    async def _capture_verified_identity(self) -> dict[str, Any] | None:
        state = await self._read_platform_state()
        if not state["connected"] or not state["identity"]:
            return None
        if not _ID_RE.fullmatch(state["account_id"]):
            return None
        if state["account_id"] != self.config.account_id:
            self._last_failure_code = "account_mismatch"
            return None
        return state

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.auto_select_adapter and path != "/v1/adapter/release":
            self._assert_selected_adapter()
        if self._token is None:
            raise QQCallError("runtime_not_ready")
        if self._transport is None:
            return await _http_post(self.config.endpoint, path, self._token, payload)
        try:
            value = self._transport(path, dict(payload), self._token)
            if inspect.isawaitable(value):
                timeout = (
                    _GROUP_JOIN_HTTP_TIMEOUT_SEC
                    if path == "/v1/group/join"
                    else _GROUP_STATUS_HTTP_TIMEOUT_SEC if path == "/v1/group/status"
                    else _HTTP_TOTAL_TIMEOUT_SEC
                )
                value = await asyncio.wait_for(value, timeout=timeout)
        except QQCallError:
            raise
        except Exception as exc:
            raise QQCallError("service_unavailable") from exc
        if not isinstance(value, dict):
            raise QQCallError("service_response_invalid")
        return value

    def _new_client_instance(self) -> str:
        return "q" + secrets.token_urlsafe(24)

    async def _release_lease(self, fence: Mapping[str, Any] | None = None) -> None:
        async with self._release_lock:
            if not self._lease_claimed or not self._client_instance or not self._token:
                return
            if fence is None:
                try:
                    fence = await self._read_platform_state()
                except QQCallError:
                    fence = None
            client_instance = self._client_instance
            payload = {"accountId": self.config.account_id, "clientInstance": client_instance}
            try:
                response = await self._post("/v1/adapter/release", payload)
                if fence is not None and not await self._identity_unchanged(fence, require_verified=False):
                    self._last_failure_code = "platform_identity_changed"
                    return
                if (
                    set(response) != {"version", "accountId", "serviceInstance", "released"}
                    or isinstance(response.get("version"), bool)
                    or not isinstance(response.get("version"), int)
                    or response.get("version") != 1
                    or response.get("accountId") != self.config.account_id
                    or not isinstance(response.get("serviceInstance"), str)
                    or not _UID_RE.fullmatch(response["serviceInstance"])
                    or (
                        self._service_meta is not None
                        and response.get("serviceInstance") != self._service_meta.get("serviceInstance")
                    )
                    or response.get("released") is not True
                ):
                    raise QQCallError("release_response_invalid")
                self._lease_claimed = False
            except Exception as exc:
                self._last_failure_code = self._error_code(exc, "lease_release_failed")

    async def _reset_for_identity(self, state: Mapping[str, Any] | None, reason: str) -> None:
        # Invalidate call and turn fences before any lease or device cleanup waits.
        await self._stop_group_audio(reason, quit_native=False)
        self._voice.update_call(None, None)
        await self._release_lease(fence=state)
        self._active_call = None
        self._last_emitted_signature = None
        self._service_meta = None
        self._peer_manifest_meta = None
        self._peer_manifest_kernel = None
        self._peer_manifest_map = None
        self._audio_plan_binding = None
        self._audio_plan_fingerprint = None
        self._auto_accept_attempted_scope = None
        self._busy_attempts.clear()
        self._busy_sender_times.clear()
        self._deferred_terminal_events.clear()
        self._cursor = 0
        self._bootstrapped = False
        self._lease_claimed = False
        connected = bool(state and state.get("connected") is True and state.get("identity"))
        self._identity = tuple(state["identity"]) if connected else None
        self._identity_account = str(state.get("account_id") or "") if connected and state else ""
        self._core_ready = bool(state and state.get("core_ready") is True)
        self._client_instance = self._new_client_instance() if self.config.enabled else None
        self._last_failure_code = reason
        self._emit_status("identity_reset")

    @staticmethod
    def _error_code(error: Any, fallback: str) -> str:
        code = getattr(error, "code", None)
        return code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code) else fallback

    def _assert_selected_adapter(self) -> None:
        """Only the supervisor-selected process may claim/control the lease."""
        try:
            from .qqcall_service import _validate_runtime_private_file, _windows_process_creation_time_100ns

            path = _validate_runtime_private_file(Path(self.config.peer_map_file), "adapter-runtime.private.json")
            with path.open("rb") as stream:
                raw = stream.read(_MAX_PEER_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_PEER_MANIFEST_BYTES:
                raise ValueError
            snapshot = json.loads(raw, object_pairs_hook=_json_unique_object)
            binding = snapshot.get("adapterBinding")
            if (
                set(snapshot) != _PEER_MANIFEST_KEYS | {"adapterBinding"}
                or snapshot.get("accountId") != self.config.account_id
                or type(binding) is not dict
                or set(binding) != {"adapter", "pid", "creationTime100ns"}
                or binding["adapter"] != self.config.adapter_name
                or type(binding["pid"]) is not int or binding["pid"] != os.getpid()
                or binding["creationTime100ns"] != str(_windows_process_creation_time_100ns(os.getpid()))
            ):
                raise ValueError
        except Exception:
            raise QQCallError("adapter_not_selected") from None

    def _read_peer_map(self, meta: Mapping[str, Any]) -> dict[str, str]:
        """Read the current-generation supervisor map or the frozen legacy map."""
        if not self.config.peer_map_file:
            return dict(self.config.peer_map)
        path = Path(self.config.peer_map_file)
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_PEER_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_PEER_MANIFEST_BYTES:
                raise ValueError("peer manifest too large")
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid number")),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise QQCallError("peer_manifest_invalid") from exc
        expected_keys = _PEER_MANIFEST_KEYS | ({"adapterBinding"} if self.config.auto_select_adapter else set())
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise QQCallError("peer_manifest_invalid")
        if self.config.auto_select_adapter:
            self._assert_selected_adapter()
        service_instance = payload.get("serviceInstance")
        host_generation = payload.get("hostGeneration")
        connection_generation = payload.get("connectionGeneration")
        kernel_pid = payload.get("kernelPid")
        kernel_creation = payload.get("kernelCreationTime100ns")
        raw_map = payload.get("peerMap")
        if (
            type(payload.get("schema")) is not int
            or payload.get("schema") != 1
            or payload.get("accountId") != self.config.account_id
            or not isinstance(service_instance, str)
            or not _SERVICE_INSTANCE_UUID_RE.fullmatch(service_instance)
            or not _is_int(host_generation)
            or not _is_int(connection_generation)
            or service_instance != meta.get("serviceInstance")
            or host_generation != meta.get("hostGeneration")
            or connection_generation != meta.get("connectionGeneration")
            or isinstance(kernel_pid, bool)
            or not isinstance(kernel_pid, int)
            or not 1 <= kernel_pid <= 2**31 - 1
            or not isinstance(kernel_creation, str)
            or not kernel_creation.isdecimal()
            or not 1 <= len(kernel_creation) <= 20
            or not 1 <= int(kernel_creation) <= 2**64 - 1
            or not isinstance(raw_map, dict)
            or len(raw_map) > 64
        ):
            raise QQCallError("peer_manifest_stale")

        peer_map: dict[str, str] = {}
        seen_uins: set[str] = set()
        for uid, uin in raw_map.items():
            if (
                not isinstance(uid, str)
                or not _UID_RE.fullmatch(uid)
                or not isinstance(uin, str)
                or not _ID_RE.fullmatch(uin)
                or uin in seen_uins
            ):
                raise QQCallError("peer_manifest_invalid")
            peer_map[uid] = uin
            seen_uins.add(uin)

        binding_meta = (service_instance, host_generation, connection_generation)
        kernel_identity = (kernel_pid, int(kernel_creation))
        map_identity = tuple(sorted(peer_map.items()))
        if self._peer_manifest_meta == binding_meta and (
            self._peer_manifest_kernel not in (None, kernel_identity)
            or self._peer_manifest_map not in (None, map_identity)
        ):
            raise QQCallError("peer_manifest_replaced")
        self._peer_manifest_meta = binding_meta
        self._peer_manifest_kernel = kernel_identity
        self._peer_manifest_map = map_identity
        return peer_map

    def _peer_binding_current(self, call: Mapping[str, Any], meta: Mapping[str, Any] | None) -> bool:
        if not isinstance(meta, Mapping):
            return False
        try:
            current_map = self._read_peer_map(meta)
        except QQCallError:
            return False
        uid = call.get("peer_uid")
        mapped_uin = call.get("peer_user_id")
        return (
            isinstance(uid, str)
            and isinstance(mapped_uin, str)
            and current_map.get(uid) == mapped_uin
        )

    def _normalize_private_scope_proof(
        self,
        value: Any,
        meta: Mapping[str, Any],
        *,
        scope_kind: str,
        peer_uid: str | None,
        peer_user_id: str | None,
        identity_correlation: str | None,
        call_id: str | None = None,
        call_generation: int | None = None,
        peer_map: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or set(value) != _PRIVATE_SCOPE_PROOF_KEYS:
            return None
        kernel_pid = value.get("targetPid")
        kernel_creation = value.get("kernelCreationTime100ns")
        relation_id = value.get("relationId")
        from_uid = value.get("fromUid")
        event_id = value.get("inviteEventId")
        proof_call_id = value.get("callId")
        proof_call_generation = value.get("callGeneration")
        if (
            type(value.get("version")) is not int or value.get("version") != 1
            or value.get("profile") != _PRIVATE_SCOPE_PROFILE
            or value.get("wrapperSha256") != _PRIVATE_SCOPE_WRAPPER_SHA256
            or not _is_int(value.get("callbackSequence"))
            or type(value.get("inviteType")) is not int or value.get("inviteType") != 1
            or not isinstance(relation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", relation_id)
            or not isinstance(from_uid, str)
            or (from_uid != "" and not _UID_RE.fullmatch(from_uid))
            or isinstance(kernel_pid, bool) or not isinstance(kernel_pid, int) or not 1 <= kernel_pid <= 2**31 - 1
            or not isinstance(kernel_creation, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", kernel_creation)
            or int(kernel_creation) > 2**64 - 1
            or not _is_int(value.get("connectionGeneration"))
            or value.get("connectionGeneration") != meta.get("connectionGeneration")
            or not _is_int(value.get("hostGeneration"))
            or value.get("hostGeneration") != meta.get("hostGeneration")
            or not _is_int(value.get("listenerMutationEpoch"))
            or not _is_int(value.get("ipcSequence"))
            or value.get("accountId") != self.config.account_id
            or value.get("serviceInstance") != meta.get("serviceInstance")
            or value.get("hostGeneration") != meta.get("hostGeneration")
            or value.get("scopeKind") != scope_kind
            or not isinstance(event_id, str) or not _INVITE_EVENT_ID_RE.fullmatch(event_id)
        ):
            return None
        if scope_kind == "call":
            if (
                not isinstance(call_id, str) or proof_call_id != call_id
                or not _is_int(call_generation) or proof_call_generation != call_generation
            ):
                return None
        elif scope_kind == "busy":
            if proof_call_id is not None or proof_call_generation is not None:
                return None
        else:
            return None
        active_map = self.config.peer_map if peer_map is None else peer_map
        if not isinstance(peer_uid, str) or active_map.get(peer_uid) != peer_user_id:
            return None
        if from_uid == "":
            # The pinned C2C producer initializes native from_uid to an actual
            # empty string. Accept that branch only when its canonical numeric
            # relation ID is the same UIN currently mapped to this Kernel UID.
            if identity_correlation != "kernel_relation_uid_matched" or relation_id != peer_user_id:
                return None
        elif identity_correlation != "kernel_from_uid_matched" or from_uid != peer_uid:
            return None
        kernel_identity = self._peer_manifest_kernel
        if kernel_identity != (kernel_pid, int(kernel_creation)):
            return None
        return dict(value)

    def _private_scope_proof_current(
        self,
        proof: Any,
        meta: Mapping[str, Any] | None,
        *,
        scope_kind: str,
        peer_uid: str | None,
        peer_user_id: str | None,
        identity_correlation: str | None,
        call_id: str | None = None,
        call_generation: int | None = None,
    ) -> bool:
        if not isinstance(meta, Mapping) or self._service_meta != dict(meta):
            return False
        try:
            current_map = self._read_peer_map(meta)
        except QQCallError:
            return False
        return self._normalize_private_scope_proof(
            proof,
            meta,
            scope_kind=scope_kind,
            peer_uid=peer_uid,
            peer_user_id=peer_user_id,
            identity_correlation=identity_correlation,
            call_id=call_id,
            call_generation=call_generation,
            peer_map=current_map,
        ) is not None

    def _normalize_call(
        self,
        value: Any,
        meta: Mapping[str, Any],
        peer_map: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) not in (_CALL_KEYS, _CALL_PROOF_KEYS):
            raise QQCallError("service_call_invalid")
        call_id = value.get("callId")
        generation = value.get("generation")
        peer_uid = value.get("peerUid")
        phase = value.get("phase")
        correlation = value.get("inviteCorrelation")
        accept_state = value.get("acceptState")
        if (
            not isinstance(call_id, str) or not _CALL_ID_RE.fullmatch(call_id)
            or not _is_int(generation)
            or (peer_uid is not None and (not isinstance(peer_uid, str) or not _UID_RE.fullmatch(peer_uid)))
            or not isinstance(phase, str) or not _SAFE_CODE_RE.fullmatch(phase)
            or type(value.get("uncertain")) is not bool
            or type(value.get("inviteCallbackReady")) is not bool
            or not isinstance(correlation, str)
            or not _SAFE_CODE_RE.fullmatch(correlation)
            or correlation not in _KNOWN_INVITE_CORRELATIONS
            or not isinstance(accept_state, str) or not _SAFE_CODE_RE.fullmatch(accept_state)
        ):
            raise QQCallError("service_call_invalid")
        if value["inviteCallbackReady"] and not peer_uid:
            raise QQCallError("service_call_invalid")
        scope = {
            "serviceInstance": meta["serviceInstance"],
            "hostGeneration": meta["hostGeneration"],
            "connectionGeneration": meta["connectionGeneration"],
            "callId": call_id,
            "callGeneration": generation,
        }
        active_map = self.config.peer_map if peer_map is None else peer_map
        mapped_uin = active_map.get(peer_uid) if peer_uid else None
        private_scope_proof = self._normalize_private_scope_proof(
            value.get("privateScopeProof"),
            meta,
            scope_kind="call",
            peer_uid=peer_uid,
            peer_user_id=mapped_uin,
            identity_correlation=correlation,
            call_id=call_id,
            call_generation=generation,
            peer_map=active_map,
        ) if "privateScopeProof" in value else None
        return {
            **{key: value[key] for key in ("callId", "generation", "phase", "uncertain", "inviteCallbackReady", "inviteCorrelation", "acceptState")},
            "peer_uid": peer_uid,
            "peer_user_id": mapped_uin,
            "scope": scope,
            "private_scope_proof": private_scope_proof,
        }

    def _validate_poll(
        self,
        value: Any,
        peer_map: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(value, dict) or set(value) != _POLL_KEYS:
            raise QQCallError("service_poll_invalid")
        if (
            isinstance(value.get("version"), bool)
            or not isinstance(value.get("version"), int)
            or value.get("version") != 1
            or value.get("accountId") != self.config.account_id
            or not isinstance(value.get("serviceInstance"), str)
            or not _UID_RE.fullmatch(value["serviceInstance"])
            or not _is_int(value.get("hostGeneration"))
            or not _is_int(value.get("connectionGeneration"))
            or not _is_int(value.get("cursor"), minimum=0)
            or not isinstance(value.get("events"), list)
            or len(value["events"]) > 256
        ):
            raise QQCallError("service_poll_invalid")
        meta = {
            "serviceInstance": value["serviceInstance"],
            "hostGeneration": value["hostGeneration"],
            "connectionGeneration": value["connectionGeneration"],
        }
        current = self._normalize_call(value["currentCall"], meta, peer_map)
        if current is not None:
            value = dict(value)
            value["currentCall"] = current
        events: list[dict[str, Any]] = []
        previous = -1
        for event in value["events"]:
            if not isinstance(event, dict):
                raise QQCallError("service_event_invalid")
            sequence = event.get("sequence")
            kind = event.get("kind")
            if (
                not _is_int(sequence, minimum=1)
                or sequence <= previous
                or sequence > value["cursor"]
                or not isinstance(kind, str) or not _SAFE_CODE_RE.fullmatch(kind)
            ):
                raise QQCallError("service_event_invalid")
            previous = sequence
            if kind == "group_invite":
                invite = event.get("groupInvite")
                if (
                    set(event) != _GROUP_INVITE_EVENT_KEYS or event.get("call") is not None
                    or not isinstance(invite, dict) or set(invite) != _GROUP_INVITE_KEYS
                    or not isinstance(invite.get("inviteId"), str)
                    or not _INVITE_EVENT_ID_RE.fullmatch(invite["inviteId"])
                    or not isinstance(invite.get("groupId"), str) or not _ID_RE.fullmatch(invite["groupId"])
                    or not isinstance(invite.get("callerUid"), str)
                    or not re.fullmatch(r"u_[A-Za-z0-9_-]{22}", invite["callerUid"])
                    or not _is_int(invite.get("observedAtMs"), minimum=0)
                ):
                    raise QQCallError("service_event_invalid")
                active_map = self.config.peer_map if peer_map is None else peer_map
                events.append({
                    "sequence": sequence, "kind": kind, "call": None,
                    "group_invite": {**invite, "peer_user_id": active_map.get(invite["callerUid"])},
                })
                continue
            if kind == "invite_busy":
                if set(event) != _BUSY_EVENT_KEYS or event.get("call") is not None:
                    raise QQCallError("service_event_invalid")
                busy_invite = event.get("busyInvite")
                if not isinstance(busy_invite, dict) or set(busy_invite) not in (_BUSY_INVITE_KEYS, _BUSY_INVITE_PROOF_KEYS):
                    raise QQCallError("service_event_invalid")
                caller_uid = busy_invite.get("callerUid")
                correlation = busy_invite.get("identityCorrelation")
                if (
                    not isinstance(caller_uid, str)
                    or not _UID_RE.fullmatch(caller_uid)
                    or not isinstance(correlation, str)
                    or not is_trusted_invite_correlation(correlation)
                ):
                    raise QQCallError("service_event_invalid")
                active_map = self.config.peer_map if peer_map is None else peer_map
                mapped_peer = active_map.get(caller_uid)
                private_scope_proof = self._normalize_private_scope_proof(
                    busy_invite.get("privateScopeProof"),
                    meta,
                    scope_kind="busy",
                    peer_uid=caller_uid,
                    peer_user_id=mapped_peer,
                    identity_correlation=correlation,
                    peer_map=active_map,
                ) if "privateScopeProof" in busy_invite else None
                events.append({
                    "sequence": sequence,
                    "kind": kind,
                    "call": None,
                    "busy_invite": {
                        "caller_uid": caller_uid,
                        "peer_user_id": mapped_peer,
                        "identity_correlation": correlation,
                        "private_scope_proof": private_scope_proof,
                    },
                })
                continue
            if set(event) != _EVENT_KEYS:
                raise QQCallError("service_event_invalid")
            call = self._normalize_call(event["call"], meta, peer_map)
            events.append({"sequence": sequence, "kind": kind, "call": call})
        return {**meta, "cursor": value["cursor"], "currentCall": current}, events

    async def _poll_once(self, identity: Mapping[str, Any]) -> bool:
        if self._closing or self._stopped:
            return False
        core_was_ready = self._core_ready
        self._core_ready = identity.get("core_ready") is True if "core_ready" in identity else True
        if not self._core_ready:
            if self._group_join_scope is not None or self._group_audio_runtime is not None:
                await self._stop_group_audio("core_not_ready", quit_native=True)
            if core_was_ready:
                self._last_emitted_signature = None
            self._voice.update_call(None, None)
        if not self._client_instance:
            self._client_instance = self._new_client_instance()
        payload = {
            "accountId": self.config.account_id,
            "clientInstance": self._client_instance,
            "after": self._cursor if self._bootstrapped else 0,
        }
        if not await self._identity_unchanged(identity, require_verified=True):
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        response = await self._post("/v1/adapter/poll", payload)
        # A successful poll claims or refreshes the service lease even if a
        # subsequent identity fence or response validation fails.
        self._lease_claimed = True
        if self._closing or self._stopped:
            return False
        if not await self._identity_unchanged(identity, require_verified=True):
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        try:
            # The adapter manifest is checked on every successful poll. In
            # reference mode it is authoritative for the current service and
            # Kernel generations; an absent/stale file never falls back to the
            # frozen legacy map.
            self._validate_poll(response, {})
            raw_meta = response if isinstance(response, Mapping) else {}
            peer_meta = {
                key: raw_meta.get(key)
                for key in ("serviceInstance", "hostGeneration", "connectionGeneration")
            }
            peer_map = self._read_peer_map(peer_meta)
            meta, events = self._validate_poll(response, peer_map)
        except QQCallError as exc:
            if self.config.peer_map_file:
                await self._reset_for_identity(identity, exc.code)
            raise
        self._poll_count += 1
        new_service_meta = {key: meta[key] for key in ("serviceInstance", "hostGeneration", "connectionGeneration")}
        if self._bootstrapped and self._service_meta != new_service_meta:
            # Drain the old generation's lease and audio state before claiming
            # a replacement. The current poll was made under the old client
            # instance, so it is discarded and a fresh poll claims the new one.
            await self._reset_for_identity(identity, "service_generation_changed")
            return False
        if not self._bootstrapped:
            self._service_meta = new_service_meta
            self._bootstrapped = True
            self._active_call = meta["currentCall"]
            async with self._request_lock:
                await self._reconcile_group_quarantine(identity)
            if self._active_call is not None and (
                self._group_join_scope is not None or self._group_audio_runtime is not None
            ):
                await self._stop_group_audio("private_call_active", quit_native=True)
            self._last_emitted_signature = None
            if self._active_call is not None:
                kind = self._resync_kind(self._active_call)
                if kind:
                    if not await self._emit_lifecycle(kind, self._active_call, identity):
                        return False
            if self._closing or self._stopped:
                return False
            self._cursor = meta["cursor"]
            self._sync_audio_call()
            self._emit_status("resynced")
            return True

        async with self._request_lock:
            await self._reconcile_group_quarantine(identity)
        last_sequence = self._cursor
        for event in events:
            if self._closing or self._stopped:
                return False
            self._event_count += 1
            sequence = event["sequence"]
            if sequence <= self._cursor:
                continue
            if sequence <= last_sequence:
                raise QQCallError("event_cursor_invalid")
            last_sequence = sequence
            if event["kind"] == "group_invite":
                # Advance before acting: a refused/expired invitation never
                # retries on the next poll. Commands and invitations share ACL,
                # native-room proof, occupancy and audio activation checks.
                self._cursor = sequence
                invite = event["group_invite"]
                # Scene-3 classification clears Bridge's temporary private
                # placeholder. Earlier journal entries must not keep it busy.
                self._active_call = meta["currentCall"]
                peer = invite.get("peer_user_id")
                age = int(time.time() * 1000) - invite["observedAtMs"]
                if (
                    isinstance(peer, str) and _ID_RE.fullmatch(peer) and 0 <= age < 30_000
                    and meta["currentCall"] is None and self._active_call is None
                ):
                    async with self._request_lock:
                        result = await self._process_group_join_request({
                            "request_id": secrets.token_urlsafe(24), "peer_user_id": peer,
                            "group_id": invite["groupId"],
                        }, identity, invitation=invite)
                    error = result.get("error")
                    if isinstance(error, dict):
                        await self._notify_dependency_refusal(peer, invite["groupId"], error.get("code"), identity)
                    self._last_failure_code = error.get("code") if isinstance(error, dict) else None
                    self._emit_status("group_auto_join_refused" if error else "group_auto_joined")
                else:
                    self._dropped_event_count += 1
                continue
            if event["kind"] == "invite_busy":
                await self._process_busy_event(event, meta["currentCall"], identity, sequence)
                self._cursor = sequence
                continue
            call = event["call"] if event["call"] is not None else self._active_call
            kind = event["kind"]
            if event["call"] is not None:
                self._active_call = event["call"]
                if self._active_call is not None and (
                    self._group_join_scope is not None or self._group_audio_runtime is not None
                ):
                    await self._stop_group_audio("private_call_active", quit_native=True)
            if kind in _SEMANTIC_KINDS and call is not None:
                if not await self._emit_lifecycle(kind, call, identity):
                    return False
            else:
                self._dropped_event_count += 1
            self._cursor = sequence
        current = meta["currentCall"]
        self._active_call = current
        if current is not None and (
            self._group_join_scope is not None or self._group_audio_runtime is not None
        ):
            await self._stop_group_audio("private_call_active", quit_native=True)
        self._cursor = max(self._cursor, meta["cursor"])
        self._sync_audio_call()
        if self._core_ready and not core_was_ready:
            await self._resync_after_core_return(identity)
        self._emit_status("polled")
        return True

    def _defer_lifecycle(self, kind: str, call: Mapping[str, Any]) -> None:
        if kind not in {"call_terminal", "controller_invalidated", "stopped"}:
            return
        saved = dict(call)
        scope = saved.get("scope")
        if not isinstance(scope, Mapping) or not self._service_meta:
            return
        if any(scope.get(key) != self._service_meta.get(key) for key in _META_KEYS):
            return
        scope_key = tuple(scope.get(key) for key in (
            "serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration",
        ))
        self._deferred_terminal_events[scope_key] = (kind, saved)
        self._deferred_terminal_events.move_to_end(scope_key)
        while len(self._deferred_terminal_events) > _DEFERRED_TERMINAL_LIMIT:
            self._deferred_terminal_events.popitem(last=False)

    async def _resync_after_core_return(self, identity: Mapping[str, Any]) -> None:
        if not self._core_ready or self._closing or self._stopped:
            return
        current = self._active_call
        current_scope = current.get("scope") if current is not None else None
        deferred = tuple(self._deferred_terminal_events.values())
        self._deferred_terminal_events.clear()
        for kind, call in deferred:
            if self._closing or self._stopped:
                return
            if current_scope is not None and current_scope == call.get("scope"):
                continue
            if not await self._emit_lifecycle(kind, call, identity, allow_terminal_without_active=True):
                return
        if current is not None:
            kind = self._resync_kind(current)
            if kind:
                await self._emit_lifecycle(kind, current, identity)

    async def _process_busy_event(
        self,
        event: Mapping[str, Any],
        occupied_call: Mapping[str, Any] | None,
        identity: Mapping[str, Any],
        sequence: int,
    ) -> None:
        invite = event.get("busy_invite")
        meta = self._service_meta
        callback = self._send_busy_private
        if (
            not isinstance(invite, Mapping)
            or occupied_call is not None
            and (
                not isinstance(occupied_call, Mapping)
                or not isinstance(occupied_call.get("scope"), Mapping)
            )
            or not isinstance(meta, Mapping)
            or callback is None
        ):
            return
        caller_uid = invite.get("caller_uid")
        peer_user_id = invite.get("peer_user_id")
        correlation = invite.get("identity_correlation")
        private_scope_proof = invite.get("private_scope_proof")
        if (
            not isinstance(caller_uid, str)
            or not _UID_RE.fullmatch(caller_uid)
            or not isinstance(peer_user_id, str)
            or not _ID_RE.fullmatch(peer_user_id)
            or not isinstance(correlation, str)
            or not is_trusted_invite_correlation(correlation)
            or not self._private_scope_proof_current(
                private_scope_proof,
                meta,
                scope_kind="busy",
                peer_uid=caller_uid,
                peer_user_id=peer_user_id,
                identity_correlation=correlation,
            )
        ):
            return
        group_occupied = occupied_call is None
        scope: dict[str, Any] | None = None
        if not group_occupied:
            assert isinstance(occupied_call, Mapping)
            scope = dict(occupied_call["scope"])
            if any(meta.get(key) != scope.get(key) for key in _META_KEYS):
                return
        try:
            current_map = self._read_peer_map(meta)
        except QQCallError:
            return
        if current_map.get(caller_uid) != peer_user_id:
            return

        now = time.monotonic()
        attempt_key = (meta["serviceInstance"], meta["hostGeneration"], meta["connectionGeneration"], sequence)
        for key, timestamp in tuple(self._busy_attempts.items()):
            if now - timestamp > _BUSY_NOTICE_COOLDOWN_SEC:
                self._busy_attempts.pop(key, None)
        for key, timestamp in tuple(self._busy_sender_times.items()):
            if now - timestamp > _BUSY_NOTICE_COOLDOWN_SEC:
                self._busy_sender_times.pop(key, None)
        if attempt_key in self._busy_attempts:
            return
        last_notice = self._busy_sender_times.get(peer_user_id)
        if last_notice is not None and now - last_notice < _BUSY_NOTICE_COOLDOWN_SEC:
            return
        # Claim before admission, Control polling, or the platform send. A
        # timeout or cancellation remains at-most-once for this invite.
        self._busy_attempts[attempt_key] = now
        self._busy_attempts.move_to_end(attempt_key)
        while len(self._busy_attempts) > _BUSY_ATTEMPT_CACHE_SIZE:
            self._busy_attempts.popitem(last=False)
        self._busy_sender_times[peer_user_id] = now
        self._busy_sender_times.move_to_end(peer_user_id)
        while len(self._busy_sender_times) > _BUSY_SENDER_CACHE_SIZE:
            self._busy_sender_times.popitem(last=False)

        async def admitted_now() -> bool:
            try:
                return _safe_callback_result(await self._call(self._admit_peer, peer_user_id))
            except Exception:
                return False

        try:
            if not await self._identity_unchanged(identity, require_verified=True):
                return
            if not self._peer_binding_uid_current(caller_uid, peer_user_id, meta):
                return
            if not await admitted_now():
                return
            if not self._private_scope_proof_current(
                private_scope_proof, meta, scope_kind="busy", peer_uid=caller_uid, peer_user_id=peer_user_id,
                identity_correlation=correlation,
            ):
                return
            if not await self._identity_unchanged(identity, require_verified=True):
                return
            if not self._peer_binding_uid_current(caller_uid, peer_user_id, meta):
                return

            if group_occupied:
                await self._send_group_occupied_busy_notice(
                    callback,
                    caller_uid=caller_uid,
                    peer_user_id=peer_user_id,
                    correlation=correlation,
                    private_scope_proof=private_scope_proof,
                    identity=identity,
                    meta=meta,
                    sequence=sequence,
                    admitted_now=admitted_now,
                )
                return

            async def pre_send_check() -> bool:
                if (
                    self._closing
                    or self._stopped
                    or self._service_meta != dict(meta)
                    or not await self._identity_unchanged(identity, require_verified=True)
                    or not self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
                    or not await admitted_now()
                    or not self._private_scope_proof_current(
                        private_scope_proof, meta, scope_kind="busy", peer_uid=caller_uid, peer_user_id=peer_user_id,
                        identity_correlation=correlation,
                    )
                ):
                    return False
                try:
                    live = await self._post("/v1/adapter/poll", {
                        "accountId": self.config.account_id,
                        "clientInstance": self._client_instance,
                        "after": sequence,
                    })
                    self._validate_poll(live, {})
                    live_meta = {key: live[key] for key in _META_KEYS}
                    live_map = self._read_peer_map(live_meta)
                    current, _events = self._validate_poll(live, live_map)
                except (QQCallError, KeyError, TypeError):
                    return False
                latest_call = current.get("currentCall")
                if (
                    live_meta != dict(meta)
                    or latest_call is None
                    or latest_call.get("scope") != scope
                    or live_map.get(caller_uid) != peer_user_id
                    or not self._private_scope_proof_current(
                        private_scope_proof, live_meta, scope_kind="busy", peer_uid=caller_uid, peer_user_id=peer_user_id,
                        identity_correlation=correlation,
                    )
                ):
                    return False
                return (
                    self._service_meta == dict(meta)
                    and await self._identity_unchanged(identity, require_verified=True)
                    and self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
                    and await admitted_now()
                    and self._private_scope_proof_current(
                        private_scope_proof, meta, scope_kind="busy", peer_uid=caller_uid, peer_user_id=peer_user_id,
                        identity_correlation=correlation,
                    )
                )

            try:
                result = await asyncio.wait_for(
                    self._call(callback, peer_user_id, _BUSY_NOTICE_TEXT, pre_send_check),
                    timeout=20.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            # Recheck policy and identity after the awaited platform action,
            # including timeouts where the send outcome is uncertain.
            current_policy = (
                await self._identity_unchanged(identity, require_verified=True)
                and self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
                and await admitted_now()
                and self._private_scope_proof_current(
                    private_scope_proof, meta, scope_kind="busy", peer_uid=caller_uid, peer_user_id=peer_user_id,
                    identity_correlation=correlation,
                )
            )
            if result is not True or not current_policy:
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Busy replies are best-effort; they must never block control poll
            # or be retried for the same invite.
            return

    async def _call_occupancy_is_group(self, meta: Mapping[str, Any]) -> bool:
        client_instance = self._client_instance
        if (
            not self._lease_claimed
            or not isinstance(client_instance, str)
            or self._closing
            or self._stopped
            or self._service_meta != dict(meta)
        ):
            return False
        try:
            response = await self._post("/v1/call/occupancy", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
            })
        except Exception:
            return False
        return bool(
            type(response) is dict
            and set(response) == {
                "version", "accountId", "serviceInstance", "hostGeneration",
                "connectionGeneration", "occupied", "kind",
            }
            and type(response.get("version")) is int
            and response.get("version") == 1
            and response.get("accountId") == self.config.account_id
            and all(response.get(key) == meta.get(key) for key in _META_KEYS)
            and type(response.get("occupied")) is bool
            and response.get("occupied") is True
            and response.get("kind") == "group"
            and self._lease_claimed
            and self._client_instance == client_instance
            and self._service_meta == dict(meta)
            and not self._closing
            and not self._stopped
        )

    async def _send_group_occupied_busy_notice(
        self,
        callback: Callable[..., Any],
        *,
        caller_uid: str,
        peer_user_id: str,
        correlation: str,
        private_scope_proof: Any,
        identity: Mapping[str, Any],
        meta: Mapping[str, Any],
        sequence: int,
        admitted_now: Callable[[], Awaitable[bool]],
    ) -> None:
        async def send_guard() -> bool:
            if (
                self._closing
                or self._stopped
                or self._service_meta != dict(meta)
                or not await self._identity_unchanged(identity, require_verified=True)
                or not await self._core_route_is_current(identity)
                or not self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
                or not await admitted_now()
                or not self._private_scope_proof_current(
                    private_scope_proof,
                    meta,
                    scope_kind="busy",
                    peer_uid=caller_uid,
                    peer_user_id=peer_user_id,
                    identity_correlation=correlation,
                )
            ):
                return False
            return bool(
                self._service_meta == dict(meta)
                and self._lease_claimed
                and await self._identity_unchanged(identity, require_verified=True)
                and await self._core_route_is_current(identity)
                and self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
                and await admitted_now()
                and self._private_scope_proof_current(
                    private_scope_proof,
                    meta,
                    scope_kind="busy",
                    peer_uid=caller_uid,
                    peer_user_id=peer_user_id,
                    identity_correlation=correlation,
                )
                # Occupancy is checked last so this exact positive lease proof
                # remains fresh through the callback's immediate send fence.
                and await self._call_occupancy_is_group(meta)
            )

        try:
            # Occupancy is read before and after the awaited DM as well as in
            # the adapter-provided immediate send fence.
            if not await send_guard():
                return
            result = await asyncio.wait_for(
                self._call(callback, peer_user_id, _BUSY_NOTICE_TEXT, send_guard),
                timeout=20.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = None
        if (
            result is not True
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
            or not self._peer_binding_uid_current(caller_uid, peer_user_id, meta)
            or not await admitted_now()
            or not self._private_scope_proof_current(
                private_scope_proof,
                meta,
                scope_kind="busy",
                peer_uid=caller_uid,
                peer_user_id=peer_user_id,
                identity_correlation=correlation,
            )
            or not await self._call_occupancy_is_group(meta)
        ):
            return

    def _peer_binding_uid_current(
        self,
        caller_uid: str,
        peer_user_id: str,
        meta: Mapping[str, Any],
    ) -> bool:
        try:
            current_map = self._read_peer_map(meta)
        except QQCallError:
            return False
        return current_map.get(caller_uid) == peer_user_id

    @staticmethod
    def _resync_kind(call: Mapping[str, Any]) -> str | None:
        if call.get("phase") in {"failed", "expired", "end_observed", "terminal"}:
            return "call_terminal"
        if call.get("uncertain") is True:
            return None
        if call.get("phase") == "connected":
            return "call_connected"
        if call.get("inviteCallbackReady") is True:
            return "invite_callback_ready"
        return None

    async def _core_route_is_current(self, identity: Mapping[str, Any]) -> bool:
        try:
            current = await self._read_platform_state()
        except QQCallError:
            return False
        return (
            self._same_identity(identity, current)
            and current.get("connected") is True
            and current.get("core_ready") is True
        )

    def _lifecycle_scope_current(
        self,
        kind: str,
        call: Mapping[str, Any],
        *,
        allow_terminal_without_active: bool = False,
    ) -> bool:
        if self._active_call is not None and self._active_call.get("scope") == call.get("scope"):
            return True
        scope = call.get("scope")
        return bool(
            allow_terminal_without_active
            and kind in {"call_terminal", "controller_invalidated", "stopped"}
            and (self._active_call is None or self._active_call.get("scope") != call.get("scope"))
            and isinstance(scope, Mapping)
            and isinstance(self._service_meta, Mapping)
            and all(scope.get(key) == self._service_meta.get(key) for key in _META_KEYS)
        )

    async def _emit_lifecycle(
        self,
        kind: str,
        call: Mapping[str, Any],
        identity: Mapping[str, Any],
        *,
        allow_terminal_without_active: bool = False,
    ) -> bool:
        if kind not in _SEMANTIC_KINDS:
            return True
        if self._closing or self._stopped:
            return False
        if not self._core_ready or identity.get("core_ready") is False:
            self._defer_lifecycle(kind, call)
            if kind == "invite_callback_ready":
                await self._maybe_auto_accept(call, identity)
            return True
        peer_user_id = call.get("peer_user_id")
        if (
            not isinstance(peer_user_id, str)
            or not _ID_RE.fullmatch(peer_user_id)
            or not is_trusted_invite_correlation(call.get("inviteCorrelation"))
        ):
            self._dropped_event_count += 1
            return True
        signature = (
            kind, call["callId"], call["generation"], call["phase"], call["uncertain"],
            call["inviteCallbackReady"], call["inviteCorrelation"], call["acceptState"],
        )
        if signature == self._last_emitted_signature:
            return True
        if not await self._identity_unchanged(identity, require_verified=True):
            self._last_failure_code = "platform_identity_changed"
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        if not await self._core_route_is_current(identity):
            self._defer_lifecycle(kind, call)
            return True
        if not self._peer_binding_current(call, self._service_meta):
            self._last_failure_code = "peer_manifest_stale"
            self._dropped_event_count += 1
            return True
        if self._closing or self._stopped:
            return False
        try:
            admitted = _safe_callback_result(await self._call(self._admit_peer, peer_user_id))
        except Exception:
            admitted = False
        if self._closing or self._stopped:
            return False
        if not await self._identity_unchanged(identity, require_verified=True):
            self._last_failure_code = "platform_identity_changed"
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        if not await self._core_route_is_current(identity):
            self._defer_lifecycle(kind, call)
            return True
        if not self._peer_binding_current(call, self._service_meta):
            self._last_failure_code = "peer_manifest_stale"
            self._dropped_event_count += 1
            return True
        if self._closing or self._stopped:
            return False
        if not admitted:
            self._dropped_event_count += 1
            self._last_emitted_signature = signature
            self._emit_status("peer_not_admitted")
            return True

        if self._closing or self._stopped:
            return False

        data = {
            "call_id": call["callId"],
            "call_generation": call["generation"],
            "phase": call["phase"],
            "uncertain": call["uncertain"],
            "invite_callback_ready": call["inviteCallbackReady"],
            "invite_correlation": call["inviteCorrelation"],
            "accept_state": call["acceptState"],
        }
        event = build_system_event(f"qq.call.{kind}", data=data)
        route = build_system_event_route(self.platform, user_id=peer_user_id)
        scope = dict(call["scope"])
        realtime_session_id = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        additional_config = {
            "system_event": event,
            "system_event_route": route,
            "qqcall_scope": scope,
            "peer_user_id": peer_user_id,
            "realtime_session": {
                "version": 1,
                "provider": "qqcall",
                "session_id": realtime_session_id,
            },
            "delivery_target": {"kind": "qqcall", **scope, "peer_user_id": peer_user_id},
        }
        text = self._lifecycle_text(kind, call)
        message = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.platform,
                message_id=f"qqcall:{call['callId']}:{call['generation']}:{kind}",
                time=time.time(),
                additional_config=additional_config,
                user_info=None,
                group_info=None,
                sender_info=None,
                receiver_info=None,
            ),
            message_segment=Seg(type="text", data=text),
            raw_message=None,
        )
        if not await self._identity_unchanged(identity, require_verified=True):
            self._last_failure_code = "platform_identity_changed"
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        if (
            not self._lifecycle_scope_current(
                kind,
                call,
                allow_terminal_without_active=allow_terminal_without_active,
            )
            or not self._peer_binding_current(call, self._service_meta)
        ):
            self._last_failure_code = "peer_manifest_stale"
            self._dropped_event_count += 1
            return True
        if self._closing or self._stopped:
            return False
        if not await self._core_route_is_current(identity):
            self._defer_lifecycle(kind, call)
            return True
        try:
            sent = await asyncio.wait_for(self._call(self._send_message, message), timeout=_CORE_SEND_TIMEOUT_SEC)
        except Exception as exc:
            if not await self._core_route_is_current(identity):
                self._defer_lifecycle(kind, call)
                return True
            self._last_failure_code = self._error_code(exc, "core_event_delivery_failed")
            self._blocked = True
            self._closing = True
            self._stop_event.set()
            self._emit_status("event_delivery_failed")
            return False
        if not await self._identity_unchanged(identity, require_verified=True):
            self._last_failure_code = "platform_identity_changed"
            await self._reset_for_identity(await self._read_platform_state(), "platform_identity_changed")
            return False
        if (
            not self._lifecycle_scope_current(
                kind,
                call,
                allow_terminal_without_active=allow_terminal_without_active,
            )
            or not self._peer_binding_current(call, self._service_meta)
        ):
            self._last_failure_code = "peer_manifest_stale"
            self._dropped_event_count += 1
            return True
        if sent is not True:
            if not await self._core_route_is_current(identity):
                self._defer_lifecycle(kind, call)
                return True
            self._last_failure_code = "core_event_delivery_failed"
            self._blocked = True
            self._closing = True
            self._stop_event.set()
            self._emit_status("event_delivery_failed")
            return False
        self._last_emitted_signature = signature
        self._sent_event_count += 1
        self._emit_status(kind)
        if kind == "invite_callback_ready":
            await self._maybe_auto_accept(call, identity)
        return True

    async def _auto_accept_policy_current(
        self,
        call: Mapping[str, Any],
        identity: Mapping[str, Any],
        *,
        require_private_scope: bool = True,
        require_core_route: bool = True,
    ) -> bool:
        peer_user_id = call.get("peer_user_id")
        def active_candidate_matches() -> bool:
            current_call = self._active_call
            return bool(
                isinstance(current_call, Mapping)
                and current_call.get("scope") == call.get("scope")
                and current_call.get("private_scope_proof") == call.get("private_scope_proof")
                and current_call.get("inviteCorrelation") == call.get("inviteCorrelation")
                and current_call.get("peer_uid") == call.get("peer_uid")
                and current_call.get("peer_user_id") == peer_user_id
                and current_call.get("phase") == "invite_ready"
                and current_call.get("uncertain") is False
                and current_call.get("inviteCallbackReady") is True
                and current_call.get("acceptState") == "not_requested"
            )

        if (
            not self.config.auto_accept
            or not self.config.shared_config_file
            or not isinstance(peer_user_id, str)
            or (require_private_scope and not self._private_scope_proof_current(
                call.get("private_scope_proof"),
                self._service_meta,
                scope_kind="call",
                peer_uid=call.get("peer_uid"),
                peer_user_id=peer_user_id,
                identity_correlation=call.get("inviteCorrelation"),
                call_id=call.get("callId"),
                call_generation=call.get("generation"),
            ))
            or self._closing
            or self._stopped
            or not self._lease_claimed
            or not self._client_instance
            or not self._service_meta
            or not active_candidate_matches()
            or not self._scope_matches(call.get("scope"), call.get("scope"))
            or not self._peer_binding_current(call, self._service_meta)
        ):
            return False
        try:
            from .qqcall_service import read_shared_qqcall_config

            current = read_shared_qqcall_config(self.config.shared_config_file, self.config.adapter_name)
        except Exception:
            return False
        policy = current.get("policy")
        if (
            current.get("enabled") is not True
            or current.get("account_id") != self.config.account_id
            or not isinstance(policy, Mapping)
            or policy.get("auto_accept") is not True
            or not await self._identity_unchanged(identity, require_verified=True)
        ):
            return False
        try:
            await self._admitted(peer_user_id, identity)
        except QQCallError:
            return False

        def config_allows_auto_accept() -> bool:
            try:
                current_config = read_shared_qqcall_config(
                    self.config.shared_config_file, self.config.adapter_name
                )
            except Exception:
                return False
            current_policy = current_config.get("policy")
            return bool(
                current_config.get("enabled") is True
                and current_config.get("account_id") == self.config.account_id
                and isinstance(current_policy, Mapping)
                and current_policy.get("auto_accept") is True
            )

        if not config_allows_auto_accept() or (require_core_route and not await self._core_route_is_current(identity)):
            return False
        # This synchronous fence runs after the final awaited core check and
        # immediately before the caller can dispatch an automatic command.
        current_meta = self._service_meta
        return bool(
            config_allows_auto_accept()
            and self._runtime_identity_matches(identity)
            and self._lease_claimed
            and self._client_instance
            and isinstance(current_meta, Mapping)
            and active_candidate_matches()
            and self._scope_matches(call.get("scope"), call.get("scope"))
            and self._peer_binding_current(call, current_meta)
            and (
                not require_private_scope
                or self._private_scope_proof_current(
                    call.get("private_scope_proof"),
                    current_meta,
                    scope_kind="call",
                    peer_uid=call.get("peer_uid"),
                    peer_user_id=peer_user_id,
                    identity_correlation=call.get("inviteCorrelation"),
                    call_id=call.get("callId"),
                    call_generation=call.get("generation"),
                )
            )
        )

    @staticmethod
    def _auto_accept_scope_key(call: Mapping[str, Any]) -> tuple[Any, ...]:
        scope = call.get("scope")
        if not isinstance(scope, Mapping):
            return ()
        return tuple(scope.get(key) for key in (
            "serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration",
        ))

    async def _maybe_auto_accept(
        self,
        call: Mapping[str, Any],
        identity: Mapping[str, Any],
    ) -> None:
        scope_key = self._auto_accept_scope_key(call)
        if (
            not self.config.auto_accept
            or not self.config.shared_config_file
            or not scope_key
            or scope_key == self._auto_accept_attempted_scope
        ):
            return
        # A scope receives one policy/capability evaluation. This makes the
        # manual-fallback refusal stable even when the same invite is replayed
        # by consecutive poll snapshots.
        self._auto_accept_attempted_scope = scope_key
        # Report the missing native private-call discriminator only when the
        # current account, peer binding, shared policy, and adapter admission
        # would otherwise permit this invite. Otherwise the ordinary policy
        # refusal is the useful and accurate diagnostic.
        if not await self._auto_accept_policy_current(
            call, identity, require_private_scope=False, require_core_route=False
        ):
            self._last_failure_code = "auto_accept_refused"
            self._emit_status("auto_accept_refused")
            return
        if not self._private_scope_proof_current(
            call.get("private_scope_proof"),
            self._service_meta,
            scope_kind="call",
            peer_uid=call.get("peer_uid"),
            peer_user_id=call.get("peer_user_id"),
            identity_correlation=call.get("inviteCorrelation"),
            call_id=call.get("callId"),
            call_generation=call.get("generation"),
        ):
            self._last_failure_code = "private_scope_unverified"
            self._emit_status("auto_accept_refused")
            return
        if not self._core_ready or identity.get("core_ready") is False or not await self._core_route_is_current(identity):
            # A trusted incoming invite can be rejected while Core is offline.
            # It still needs the live policy, ACL, peer binding and private proof.
            failure = await self._dependency_failure(identity) or "call_missing_core"
            if await self._auto_accept_policy_current(call, identity, require_core_route=False):
                try:
                    await self._post("/v1/call/command", {
                        "accountId": self.config.account_id, "clientInstance": self._client_instance,
                        "operation": "reject", "scope": dict(call["scope"]),
                    })
                except Exception:
                    self._emit_status("call_dependency_reject_unconfirmed")
                await self._notify_dependency_refusal(call["peer_user_id"], None, failure, identity)
            self._last_failure_code = failure
            self._emit_status("auto_accept_refused")
            return
        if not await self._auto_accept_policy_current(call, identity):
            self._last_failure_code = "auto_accept_refused"
            self._emit_status("auto_accept_refused")
            return
        # The scope was claimed before policy I/O. Even a timeout or a late
        # refusal cannot cause a second accept for the same call generation.
        request_id = secrets.token_urlsafe(32)
        request = {
            "platform": self.platform,
            "message_type_name": "qqcall_request",
            "content": {
                "version": 1,
                "request_id": request_id,
                "platform": self.platform,
                "operation": "accept",
                "peer_user_id": call["peer_user_id"],
                "scope": dict(call["scope"]),
            },
        }
        self._auto_accept_request_ids.add(request_id)
        try:
            response = await self.handle_request(request)
            if not isinstance(response, Mapping) or response.get("status") != "ok":
                from .qqcall_dependencies import dependency_notice
                code = response.get("error", {}).get("code") if isinstance(response, Mapping) else None
                self._last_failure_code = code if dependency_notice(code) is not None else "auto_accept_refused"
                self._emit_status("auto_accept_refused")
            else:
                self._emit_status("auto_accept_requested")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._last_failure_code = "auto_accept_refused"
            self._emit_status("auto_accept_refused")
        finally:
            self._auto_accept_request_ids.discard(request_id)

    @staticmethod
    def _lifecycle_text(kind: str, call: Mapping[str, Any]) -> str:
        if kind == "invite_callback_ready":
            return "QQ 来电邀请回调已就绪，等待显式接听；当前未自动接听。"
        if kind == "call_connected":
            return "QQCall 控制层观察到通话已连接；这不表示音频链路已验证。"
        if kind == "call_terminal":
            if call.get("uncertain"):
                return "QQCall 观察到通话结束相关状态，但远端结束尚未确认。"
            return "QQCall 报告当前通话进入终止状态。"
        if kind == "controller_invalidated":
            return "QQCall 控制器已失效；当前通话不能继续控制。"
        return "QQCall 控制器已停止。"

    def _emit_status(self, kind: str) -> None:
        try:
            result = self._on_status({"kind": kind, "status": self.status()})
            if inspect.isawaitable(result):
                asyncio.create_task(result).add_done_callback(lambda _task: None)
        except Exception:
            pass

    async def run(self) -> None:
        if not self.config.enabled or self._stopped or self._running:
            return
        self._running = True
        self._run_task = asyncio.current_task()
        self._voice.start_supervisor()
        try:
            self._token = _read_control_token(self.config.token_file)
            self._client_instance = self._new_client_instance()
            self._emit_status("started")
            while not self._stop_event.is_set() and not self._blocked:
                try:
                    state = await self._capture_verified_identity()
                    if state is None:
                        current = await self._read_platform_state()
                        if self._identity is not None and (
                            not current["connected"]
                            or not self._same_identity(
                                {"identity": self._identity, "account_id": self._identity_account}, current
                            )
                        ):
                            await self._reset_for_identity(current, "platform_identity_changed")
                        elif current.get("account_id") != self.config.account_id:
                            self._last_failure_code = "account_mismatch"
                        await self._wait_poll_interval()
                        continue
                    if self._identity is None or self._identity != state["identity"] or self._identity_account != state["account_id"]:
                        await self._reset_for_identity(state, "platform_identity_changed")
                    try:
                        polled = await self._poll_once(state)
                        if polled:
                            self._last_failure_code = None
                    except QQCallError as exc:
                        if self._group_join_scope is not None or self._group_audio_runtime is not None:
                            await self._stop_group_audio("poll_failed", quit_native=True)
                        self._last_failure_code = exc.code
                        if exc.code in {"adapter_owned", "account_mismatch", "event_cursor_invalid", "event_cursor_expired"}:
                            if exc.code in {"event_cursor_invalid", "event_cursor_expired"}:
                                await self._reset_for_identity(state, exc.code)
                                continue
                            self._blocked = True
                            self._stop_event.set()
                        elif exc.code.startswith(("service_poll_", "service_event_", "service_call_")):
                            self._blocked = True
                            self._stop_event.set()
                        self._emit_status("poll_failed")
                    except Exception as exc:
                        if self._group_join_scope is not None or self._group_audio_runtime is not None:
                            await self._stop_group_audio("poll_failed", quit_native=True)
                        self._last_failure_code = self._error_code(exc, "poll_failed")
                        self._emit_status("poll_failed")
                    if not self._stop_event.is_set():
                        await self._wait_poll_interval()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._group_join_scope is not None or self._group_audio_runtime is not None:
                        await self._stop_group_audio("poll_failed", quit_native=True)
                    self._last_failure_code = self._error_code(exc, "runtime_failed")
                    self._emit_status("runtime_failed")
                    await self._wait_poll_interval()
        finally:
            await self._stop_group_audio("stopped", quit_native=True)
            self._closing = True
            self._voice.update_call(None, None)
            await self._voice.stop()
            await self._release_lease()
            self._running = False
            self._stopped = True
            self._active_call = None
            self._emit_status("stopped")

    async def _wait_poll_interval(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.poll_interval_sec)
        except TimeoutError:
            pass

    async def stop(self) -> dict[str, Any]:
        if self._stopped:
            return self.status()
        # Close the ingress barrier before cleanup yields control to another
        # coroutine. Requests that have not crossed the mutation gate are now
        # rejected, and poll results cannot emit late lifecycle events.
        self._closing = True
        self._stop_event.set()
        await self._stop_group_audio("stopped", quit_native=True)
        self._voice.update_call(None, None)
        await self._voice.stop()
        task = self._run_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            await self._release_lease()
            self._stopped = True
            self._running = False
            self._active_call = None
            self._emit_status("stopped")
        return self.status()

    async def _admitted(self, peer_user_id: str, identity: Mapping[str, Any]) -> bool:
        if not await self._identity_unchanged(identity, require_verified=True):
            raise QQCallError("platform_identity_changed")
        try:
            allowed = _safe_callback_result(await self._call(self._admit_peer, peer_user_id))
        except Exception as exc:
            raise QQCallError("peer_admission_failed") from exc
        if not await self._identity_unchanged(identity, require_verified=True):
            raise QQCallError("platform_identity_changed")
        if not allowed:
            raise QQCallError("peer_not_admitted")
        return True

    async def _send_custom_response(
        self,
        response: dict[str, Any],
        fence: Mapping[str, Any] | None = None,
        *,
        before_send: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        try:
            identity = await self._read_platform_state()
        except QQCallError:
            return False
        if fence is not None and not self._same_identity(fence, identity):
            return False
        if before_send is not None:
            try:
                if not await before_send():
                    return False
            except Exception:
                return False
        try:
            sent = await asyncio.wait_for(
                self._call(self._send_custom, "qqcall_response", response),
                timeout=_CORE_SEND_TIMEOUT_SEC,
            )
        except Exception:
            return False
        try:
            current = await self._read_platform_state()
        except Exception:
            return False
        return self._same_identity(identity, current) and (fence is None or self._same_identity(fence, current)) and sent is True

    def _runtime_identity_matches(self, identity: Mapping[str, Any]) -> bool:
        return bool(
            self._identity
            and tuple(identity.get("identity") or ()) == self._identity
            and identity.get("account_id") == self._identity_account == self.config.account_id
        )

    @staticmethod
    def _request_envelope(raw: Any, platform: str) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping) or raw.get("platform") != platform:
            return None
        if raw.get("message_type_name") not in (None, "qqcall_request"):
            return None
        content = raw.get("content")
        base_keys = {"version", "request_id", "platform", "operation", "peer_user_id", "scope"}
        if not isinstance(content, dict):
            return None
        request_id = content.get("request_id")
        operation = content.get("operation")
        peer = content.get("peer_user_id")
        if (
            isinstance(content.get("version"), bool)
            or not isinstance(content.get("version"), int)
            or content.get("version") != 1
            or content.get("platform") != platform
            or not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id)
            or not isinstance(operation, str) or operation not in _ALLOWED_OPERATIONS
            or not isinstance(peer, str) or not _ID_RE.fullmatch(peer)
        ):
            return None
        scope = content.get("scope")
        if operation == "join_group":
            if set(content) != base_keys | {"group_id", "trigger_text"} or scope is not None:
                return None
            group_id = content.get("group_id")
            trigger_text = content.get("trigger_text")
            if (
                not isinstance(group_id, str)
                or not _ID_RE.fullmatch(group_id)
                or match_group_call_trigger(trigger_text) is None
            ):
                return None
        elif operation == "leave_group":
            if set(content) != base_keys | {"group_id", "requester_user_id"} or scope is not None:
                return None
            group_id = content.get("group_id")
            requester = content.get("requester_user_id")
            if (
                not isinstance(group_id, str)
                or not _ID_RE.fullmatch(group_id)
                or not isinstance(requester, str)
                or not _ID_RE.fullmatch(requester)
                or requester != peer
            ):
                return None
        elif operation == "group_status":
            if set(content) != base_keys | {"group_id"} or scope is not None:
                return None
            group_id = content.get("group_id")
            if not isinstance(group_id, str) or not _ID_RE.fullmatch(group_id):
                return None
        elif operation == "group_audio_status":
            if set(content) != base_keys | {"group_id", "scope_group", "audio_claim"} or scope is not None:
                return None
            group_id = content.get("group_id")
            try:
                normalized_scope = validate_group_audio_scope(content.get("scope_group"))
                normalized_claim = validate_group_audio_claim(content.get("audio_claim"))
            except (TypeError, ValueError):
                return None
            if not isinstance(group_id, str) or not _ID_RE.fullmatch(group_id):
                return None
            if normalized_scope["groupId"] != group_id:
                return None
            content = {
                **content,
                "scope_group": normalized_scope,
                "audio_claim": normalized_claim,
            }
        elif set(content) != base_keys:
            return None
        elif operation == "status":
            if scope is not None and (not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS):
                return None
        elif not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
            return None
        return dict(content)

    async def _admitted_group(
        self,
        peer_user_id: str,
        group_id: str,
        identity: Mapping[str, Any],
        *,
        require_core: bool = True,
    ) -> bool:
        if require_core and (not self._core_ready or identity.get("core_ready") is False):
            raise QQCallError("core_not_ready")
        if not await self._identity_unchanged(identity, require_verified=True):
            raise QQCallError("platform_identity_changed")
        callback = self._admit_group
        if callback is None:
            raise QQCallError("group_not_admitted")
        try:
            allowed = _safe_callback_result(await self._call(callback, peer_user_id, group_id))
        except Exception as exc:
            raise QQCallError("group_admission_failed") from exc
        if not await self._identity_unchanged(identity, require_verified=True):
            raise QQCallError("platform_identity_changed")
        if require_core and (not self._core_ready or not await self._core_route_is_current(identity)):
            raise QQCallError("core_not_ready")
        if not allowed:
            raise QQCallError("group_not_admitted")
        return True

    async def _dependency_failure(self, identity: Mapping[str, Any] | None = None) -> str | None:
        from .qqcall_dependencies import DEPENDENCY_NAMES

        try:
            if self._dependency_check is None:
                return "call_missing_core_tts_universal"
            missing = await asyncio.wait_for(self._call(self._dependency_check), timeout=4)
            if not isinstance(missing, (tuple, list)) or any(item not in DEPENDENCY_NAMES for item in missing):
                return "call_missing_core_tts_universal"
            if identity is not None and (
                not self._core_ready or identity.get("core_ready") is False
                or not await self._core_route_is_current(identity)
            ):
                missing = (*missing, "core")
            ordered = [name for name in DEPENDENCY_NAMES if name in missing]
            return "call_missing_" + "_".join(ordered) if ordered else None
        except Exception:
            return "call_missing_core_tts_universal"

    async def _notify_dependency_refusal(self, peer: str, group: str | None, code: str | None, identity: Mapping[str, Any]) -> None:
        from .qqcall_dependencies import dependency_notice

        text = dependency_notice(code)
        if text is None:
            return

        async def preflight():
            if self._closing or self._stopped or not await self._identity_unchanged(identity, require_verified=True):
                return False
            try:
                if group is None:
                    await self._admitted(peer, identity)
                else:
                    await self._admitted_group(peer, group, identity, require_core=False)
                return True
            except QQCallError:
                return False

        try:
            if self._send_call_notice and await preflight():
                await self._call(self._send_call_notice, peer, group, text, preflight)
            elif group is None and self._send_busy_private and await preflight():
                await self._call(self._send_busy_private, peer, text, preflight)
        except Exception:
            self._emit_status("call_dependency_notice_failed")

    @staticmethod
    def _group_observation_ttl(item: Mapping[str, Any], now_ms: int) -> int | None:
        observed_at = item.get("observedAtMs")
        if not _is_int(observed_at, minimum=0):
            raise QQCallError("group_status_invalid")
        age_ms = now_ms - observed_at
        if age_ms < 0 or age_ms >= 30_000:
            return None
        return min(30, max(1, math.ceil((30_000 - age_ms) / 1000)))

    def _project_group_observation(
        self,
        value: Any,
        category: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise QQCallError("group_status_invalid")
        ttl = self._group_observation_ttl(value, now_ms)
        if ttl is None:
            return None

        if category == "membership":
            required = {"source", "onlineCount", "observedAtMs"}
            optional = {"memberCount", "businessType", "nativeTimeout"}
            if (
                not required.issubset(value)
                or set(value) - required - optional
                or value.get("source") not in {"group_audio_member_count", "group_video_member_count"}
                or not _is_int(value.get("onlineCount"), minimum=0)
                or value["onlineCount"] > 1_000_000
            ):
                raise QQCallError("group_status_invalid")
            member_count = value.get("memberCount")
            business_type = value.get("businessType")
            native_timeout = value.get("nativeTimeout")
            if (
                member_count is not None and (not _is_int(member_count, minimum=0) or member_count > 1_000_000)
                or business_type is not None and (not _is_int(business_type, minimum=0) or business_type > 0x7FFFFFFF)
                or native_timeout is not None and type(native_timeout) is not bool
            ):
                raise QQCallError("group_status_invalid")
            return {
                "source": value["source"],
                "onlineCount": value["onlineCount"],
                "memberCount": member_count,
                "businessType": business_type,
                "nativeTimeout": native_timeout,
                "ttlSecondsRemaining": ttl,
            }

        if category == "invitation":
            if set(value) != {
                "source", "roomId", "totalInvitedMembers", "seq", "joinCount", "quitCount", "observedAtMs",
            }:
                raise QQCallError("group_status_invalid")
            room_id = value.get("roomId")
            sequence = value.get("seq")
            if (
                value.get("source") != "group_video_invite_member_update"
                or not isinstance(room_id, str)
                or not room_id
                or len(room_id) > 512
                or room_id.strip() != room_id
                or any(ord(char) < 32 or ord(char) == 127 for char in room_id)
                or not _is_int(value.get("totalInvitedMembers"), minimum=0)
                or value["totalInvitedMembers"] > 1_000_000
                or not _is_int(value.get("joinCount"), minimum=0)
                or value["joinCount"] > 8192
                or not _is_int(value.get("quitCount"), minimum=0)
                or value["quitCount"] > 8192
                or not (
                    _is_int(sequence, minimum=0)
                    or isinstance(sequence, str) and len(sequence) <= 20 and re.fullmatch(r"(?:0|[1-9][0-9]*)", sequence)
                )
            ):
                raise QQCallError("group_status_invalid")
            return {
                "source": "group_video_invite_member_update",
                "totalInvitedMembers": value["totalInvitedMembers"],
                "joinCount": value["joinCount"],
                "quitCount": value["quitCount"],
                "ttlSecondsRemaining": ttl,
            }

        if category == "roomEndObservation":
            if (
                set(value) != {"source", "nativeTimeout", "onlineCount", "observedAtMs"}
                or value.get("source") != "native_timeout_zero_members"
                or value.get("nativeTimeout") is not True
                or value.get("onlineCount") != 0
                or type(value.get("onlineCount")) is not int
            ):
                raise QQCallError("group_status_invalid")
            return {
                "source": "native_timeout_zero_members",
                "nativeTimeout": True,
                "onlineCount": 0,
                "ttlSecondsRemaining": ttl,
            }

        raise QQCallError("group_status_invalid")

    def _project_group_diagnostics(
        self,
        result: Any,
        *,
        group_id: str,
        service_meta: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        expected_keys = {
            "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
            "groupId", "occupied", "nativeJoinVerified", "room", "diagnosticOnly", "diagnostic",
        }
        if not isinstance(result, dict) or set(result) != expected_keys:
            raise QQCallError("group_status_invalid")
        if (
            isinstance(result.get("version"), bool)
            or result.get("version") != 1
            or not isinstance(result.get("version"), int)
            or result.get("accountId") != self.config.account_id
        ):
            raise QQCallError("group_status_invalid")
        if (
            result.get("serviceInstance") != service_meta.get("serviceInstance")
            or not _is_int(result.get("hostGeneration"))
            or result.get("hostGeneration") != service_meta.get("hostGeneration")
            or not _is_int(result.get("connectionGeneration"))
            or result.get("connectionGeneration") != service_meta.get("connectionGeneration")
        ):
            raise QQCallError("stale_generation")
        if result.get("groupId") != group_id:
            raise QQCallError("group_identity_mismatch")
        native_join_verified = result.get("nativeJoinVerified")
        room = result.get("room")
        if (
            type(result.get("occupied")) is not bool
            or type(native_join_verified) is not bool
            or result.get("diagnosticOnly") is not True
        ):
            raise QQCallError("group_status_invalid")

        room_observed = False
        if room is not None:
            room_keys = {"groupId", "roomId", "roomGeneration", "live", "observedAtMs"}
            room_id = room.get("roomId") if isinstance(room, dict) else None
            observed_at = room.get("observedAtMs") if isinstance(room, dict) else None
            age_ms = now_ms - observed_at if _is_int(observed_at, minimum=0) else -1
            if (
                native_join_verified is not True
                or not isinstance(room, dict)
                or set(room) != room_keys
                or room.get("groupId") != group_id
                or not isinstance(room_id, str)
                or not room_id
                or room_id.strip() != room_id
                or len(room_id) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in room_id)
                or not _is_int(room.get("roomGeneration"))
                or room.get("live") is not True
                or age_ms < 0
                or age_ms > 5_000
            ):
                raise QQCallError("group_status_invalid")
            room_observed = True

        diagnostic = result.get("diagnostic")
        if diagnostic is None:
            membership = invitation = room_end = None
        else:
            diagnostic_keys = {
                "groupId", "membership", "invitation", "roomEndObservation", "diagnosticOnly", "nativeJoinVerified",
            }
            if (
                not isinstance(diagnostic, dict)
                or set(diagnostic) != diagnostic_keys
                or diagnostic.get("groupId") != group_id
                or diagnostic.get("diagnosticOnly") is not True
                or diagnostic.get("nativeJoinVerified") is not False
            ):
                raise QQCallError("group_status_invalid")
            membership = self._project_group_observation(diagnostic.get("membership"), "membership", now_ms)
            invitation = self._project_group_observation(diagnostic.get("invitation"), "invitation", now_ms)
            room_end = self._project_group_observation(
                diagnostic.get("roomEndObservation"), "roomEndObservation", now_ms
            )

        return {
            "occupied": result["occupied"],
            "nativeJoinVerified": native_join_verified,
            "roomObserved": room_observed,
            "diagnosticOnly": True,
            "membership": membership,
            "invitation": invitation,
            "roomEndObservation": room_end,
        }

    async def _process_group_status_request(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any],
        validation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = str(request["request_id"])
        peer_user_id = str(request["peer_user_id"])
        group_id = str(request["group_id"])
        if not self._lease_claimed or not self._client_instance or not isinstance(self._service_meta, Mapping):
            return self._make_response(request_id, "group_status", peer_user_id, error_code="adapter_lease_required")
        if not self._core_ready or identity.get("core_ready") is False:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="core_not_ready")
        service_meta = dict(self._service_meta)
        client_instance = self._client_instance
        try:
            await self._admitted_group(peer_user_id, group_id, identity)
        except QQCallError as exc:
            return self._make_response(request_id, "group_status", peer_user_id, error_code=exc.code)
        if not self._core_ready or identity.get("core_ready") is False:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="core_not_ready")
        if (
            self._closing
            or self._stopped
            or self._service_meta != service_meta
            or not self._lease_claimed
            or self._client_instance != client_instance
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
        ):
            return self._make_response(request_id, "group_status", peer_user_id, error_code="platform_identity_changed")
        try:
            result = await self._post("/v1/group/diagnostics", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "groupId": group_id,
            })
        except Exception as exc:
            code = self._error_code(exc, "group_status_failed")
            return self._make_response(request_id, "group_status", peer_user_id, error_code=code)
        if not self._core_ready or identity.get("core_ready") is False:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="core_not_ready")
        if (
            self._closing
            or self._stopped
            or self._service_meta != service_meta
            or not self._lease_claimed
            or self._client_instance != client_instance
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
        ):
            return self._make_response(request_id, "group_status", peer_user_id, error_code="platform_identity_changed")
        try:
            data = self._project_group_diagnostics(
                result,
                group_id=group_id,
                service_meta=service_meta,
                now_ms=int(time.time() * 1000),
            )
        except QQCallError as exc:
            return self._make_response(request_id, "group_status", peer_user_id, error_code=exc.code)
        if validation_context is not None:
            validation_context.update({
                "result": result,
                "group_id": group_id,
                "service_meta": service_meta,
                "client_instance": client_instance,
            })
        return self._make_response(request_id, "group_status", peer_user_id, data=data)

    async def _group_audio_runtime_current(
        self,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
    ) -> bool:
        current_meta = self._service_meta
        if (
            self._closing
            or self._stopped
            or not self.config.enabled
            or not self._lease_claimed
            or self._client_instance != client_instance
            or not isinstance(current_meta, Mapping)
            or dict(current_meta) != dict(service_meta)
            or not self._core_ready
            or identity.get("core_ready") is False
            or not self._runtime_identity_matches(identity)
        ):
            return False
        try:
            if not await self._identity_unchanged(identity, require_verified=True):
                return False
            if not await self._core_route_is_current(identity):
                return False
        except Exception:
            return False
        return bool(
            self._core_ready
            and self._lease_claimed
            and not self._closing
            and not self._stopped
            and self._client_instance == client_instance
            and isinstance(self._service_meta, Mapping)
            and dict(self._service_meta) == dict(service_meta)
            and self._runtime_identity_matches(identity)
        )

    async def _group_audio_context_current(
        self,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
        requester: str,
        binding: Any,
        scope_group: Mapping[str, Any],
    ) -> bool:
        if not await self._group_audio_runtime_current(identity, service_meta, client_instance):
            return False
        if not bool(
            self._active_call is None
            and self._group_audio_requester_user_id == requester
            and self._group_audio_binding is binding
            and binding is not None
            and self._group_audio_runtime is not None
            and callable(getattr(binding, "verify_result", None))
            and scope_group.get("serviceInstance") == service_meta.get("serviceInstance")
            and scope_group.get("hostGeneration") == service_meta.get("hostGeneration")
            and scope_group.get("connectionGeneration") == service_meta.get("connectionGeneration")
        ):
            return False
        try:
            await self._admitted_group(requester, str(scope_group["groupId"]), identity)
            current = await self._read_native_group_current(
                scope_group, requester, service_meta, client_instance,
            )
        except Exception:
            return False
        return bool(
            current
            and await self._identity_unchanged(identity, require_verified=True)
            and await self._core_route_is_current(identity)
            and self._group_audio_local_context_current(
                scope_group, requester, identity, service_meta, client_instance,
            )
        )

    async def _new_group_universal_client(self) -> Any:
        """Create a distinct client only when a group audio binding is requested."""
        return await self._voice._new_client()

    @staticmethod
    def _make_group_audio_backend(plan: Mapping[str, Any]) -> Any:
        """Resolve WASAPI endpoints only from the supervisor's current plan."""
        from .qqcall_audio import QQCallAudioBackend, QQCallAudioConfig

        audio_value = plan.get("audio")
        if not isinstance(audio_value, Mapping) or audio_value.get("enabled") is not True:
            raise QQCallError("audio_disabled")
        return QQCallAudioBackend(QQCallAudioConfig.from_mapping(audio_value))

    async def _refresh_group_audio_route(self, scope: Mapping[str, Any], audio_config: Any) -> bool:
        # One timed-out metadata reader may be retired by the controller. Give
        # its replacement one fresh, complete verification within the existing
        # proof lifetime, never a new deadline or a cached success. The PCM
        # owner's independent expiry fence remains active throughout this await.
        async with asyncio.timeout(2.0):
            proof = await self._verify_group_audio_route(scope, audio_config)
            if type(proof) is QQCallRouteRejection and proof.reason == "observer_timeout":
                _LOGGER.warning("QQCall group audio route retry code=observer_timeout")
                await asyncio.sleep(0.05)
                proof = await self._verify_group_audio_route(scope, audio_config)
            if type(proof) is QQCallRouteRejection:
                _LOGGER.warning("QQCall group audio route rejected code=%s helper_status=%s helper_error=%s",
                                proof.reason, proof._helper_http_status, proof._helper_error_code)
            return bool(
                isinstance(proof, QQCallRouteProof)
                and 0 <= time.monotonic() - proof.observed_at_monotonic <= 2.0
            )

    async def _activate_group_audio(
        self,
        scope: Mapping[str, Any],
        requester: str,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
    ) -> bool:
        """Prove native membership and the virtual route before owning audio."""
        if self.config.audio.get("enabled") is not True or not self._audio_plan_required:
            raise QQCallError("group_audio_unavailable")
        try:
            plan, fingerprint = self._read_current_audio_plan()
            backend = self._make_group_audio_backend(plan)
        except Exception as exc:
            raise QQCallError(self._error_code(exc, "group_audio_unavailable")) from None
        self._group_audio_plan_fingerprint = fingerprint
        proof = await self._verify_group_audio_route(scope, backend.config)
        if (
            not isinstance(proof, QQCallRouteProof)
            or time.monotonic() - proof.observed_at_monotonic > 2.0
        ):
            if isinstance(proof, QQCallRouteRejection):
                raise QQCallError(proof.reason)
            raise QQCallError("group_audio_route_unavailable")

        # Imported here so text-only/control-only Core use does not import the
        # optional PCM runtime package.
        from qqcall_pcm.group_runtime import GroupAudioRuntime

        owner_ref: dict[str, Any] = {}

        def local_fence() -> bool:
            owner = owner_ref.get("runtime")
            return bool(
                self._group_audio_local_context_current(
                    scope, requester, identity, service_meta, client_instance,
                )
                and self._group_audio_runtime is owner
            )

        async def fresh_lease(current_scope: dict[str, Any]) -> bool:
            if current_scope != dict(scope):
                return False
            return await self._refresh_group_audio_route(current_scope, backend.config)

        async def owner_failed(code: str) -> None:
            owner = owner_ref.get("runtime")
            if owner is not None and self._group_audio_runtime is owner:
                self._last_failure_code = code
                await self._stop_group_audio(code, quit_native=True)

        runtime = GroupAudioRuntime(
            scope,
            requester,
            backend,
            self._new_group_universal_client,
            fresh_lease,
            self._send_message,
            local_fence,
            owner_failed,
        )
        owner_ref["runtime"] = runtime
        self._group_audio_runtime = runtime
        self._group_audio_binding = None

        def own_binding(binding: Any) -> None:
            if self._group_audio_runtime is runtime:
                self._group_audio_binding = binding

        try:
            await runtime.start(on_binding=own_binding)
        except Exception:
            if self._group_audio_runtime is runtime:
                await self._stop_group_audio("group_audio_start_failed", quit_native=True)
            raise
        if (
            self._group_audio_runtime is not runtime
            or self._group_audio_binding is None
            or runtime.status().get("ready") is not True
        ):
            await self._stop_group_audio("group_audio_start_failed", quit_native=True)
            raise QQCallError("group_audio_unavailable")
        return True

    async def _quit_owned_native_group(
        self,
        scope: Mapping[str, Any],
        requester: str,
        identity: Mapping[str, Any],
        service_meta: Mapping[str, Any],
        client_instance: str,
        *,
        before_dispatch: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        def owner_still_matches() -> bool:
            return bool(
                self._group_join_scope == dict(scope)
                and self._group_audio_requester_user_id == requester
                and self._group_join_meta == dict(service_meta)
                and self._group_join_client_instance == client_instance
                and self._group_join_identity == tuple(identity.get("identity") or ())
            )

        if (
            self._stopped
            or not self.config.enabled
            or not self._lease_claimed
            or self._client_instance != client_instance
            or self._service_meta != dict(service_meta)
            or not self._runtime_identity_matches(identity)
            or not owner_still_matches()
        ):
            return False
        try:
            if not await self._identity_unchanged(identity, require_verified=True):
                return False
            if not await self._read_native_group_current(scope, requester, service_meta, client_instance):
                absent = await self._native_group_is_absent(scope, service_meta, client_instance)
                return bool(
                    absent
                    and await self._identity_unchanged(identity, require_verified=True)
                    and self._lease_claimed
                    and self._client_instance == client_instance
                    and self._service_meta == dict(service_meta)
                )
            if (
                not await self._identity_unchanged(identity, require_verified=True)
                or not self._lease_claimed
                or self._client_instance != client_instance
                or self._service_meta != dict(service_meta)
                or not owner_still_matches()
            ):
                return False
            if before_dispatch is not None:
                try:
                    if await before_dispatch() is not True:
                        return False
                except Exception:
                    return False
            if (
                not self._lease_claimed
                or self._client_instance != client_instance
                or self._service_meta != dict(service_meta)
                or not owner_still_matches()
            ):
                return False
            response = await self._post("/v1/group/quit", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "scope": dict(scope),
            })
        except Exception:
            return False
        quit_acknowledged = bool(
            type(response) is dict
            and set(response) == _GROUP_QUIT_RESULT_KEYS
            and type(response.get("version")) is int
            and response.get("version") == 1
            and response.get("accountId") == self.config.account_id
            and response.get("serviceInstance") == service_meta.get("serviceInstance")
            and response.get("operation") == "group_quit"
        )
        if not quit_acknowledged:
            return False
        # A quit acknowledgement does not prove that the native slot has
        # actually been released. Keep the owner quarantined until a separate
        # driver-current read reports terminal absence for this generation.
        try:
            if (
                not await self._identity_unchanged(identity, require_verified=True)
                or not self._lease_claimed
                or self._client_instance != client_instance
                or self._service_meta != dict(service_meta)
                or not owner_still_matches()
                or not await self._native_group_is_absent(scope, service_meta, client_instance)
            ):
                return False
            return bool(
                await self._identity_unchanged(identity, require_verified=True)
                and self._lease_claimed
                and self._client_instance == client_instance
                and self._service_meta == dict(service_meta)
                and owner_still_matches()
            )
        except Exception:
            return False

    async def _stop_group_audio(
        self,
        reason: str = "stopped",
        *,
        quit_native: bool = False,
        before_native_quit: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        self._group_audio_stop_depth += 1
        try:
            return await self._stop_group_audio_impl(reason, quit_native=quit_native, before_native_quit=before_native_quit)
        finally:
            self._group_audio_stop_depth -= 1

    async def _stop_group_audio_impl(
        self, reason: str = "stopped", *, quit_native: bool = False,
        before_native_quit: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        runtime, self._group_audio_runtime = self._group_audio_runtime, None
        binding, self._group_audio_binding = self._group_audio_binding, None
        scope = dict(self._group_join_scope) if isinstance(self._group_join_scope, Mapping) else None
        requester = self._group_audio_requester_user_id
        service_meta = dict(self._group_join_meta) if isinstance(self._group_join_meta, Mapping) else None
        client_instance = self._group_join_client_instance
        identity = {
            "identity": self._group_join_identity,
            "account_id": self.config.account_id,
            "connected": True,
            "core_ready": self._core_ready,
        }
        safe_reason = (
            reason
            if isinstance(reason, str) and _SAFE_CODE_RE.fullmatch(reason) and reason != "stopped"
            else None
        )
        if safe_reason is not None:
            self._group_audio_last_failure_code = safe_reason
        if runtime is not None or binding is not None or scope is not None:
            # A concurrent poll/stop can invalidate an in-flight route read.
            # Record which cleanup started first without logging scope values.
            diagnostic_reason = reason if isinstance(reason, str) and reason in {
                "stopped", "core_not_ready", "private_call_active", "poll_failed",
                "platform_identity_changed", "service_generation_changed",
                "group_audio_lease_lost", "group_audio_watch_failed", "audio_scope_stale",
                "group_audio_start_failed", "group_join_response_invalid",
            } else "other"
            _LOGGER.warning("QQCall group cleanup started reason=%s quit_native=%s depth=%s",
                            diagnostic_reason, quit_native is True, self._group_audio_stop_depth)
        local_audio_stopped = True
        if runtime is not None:
            try:
                await runtime.stop(reason if safe_reason is not None else "stopped")
            except asyncio.CancelledError:
                self._group_audio_runtime = runtime
                self._group_audio_binding = binding
                raise
            except Exception:
                local_audio_stopped = False
        elif binding is not None:
            try:
                await binding.close()
            except asyncio.CancelledError:
                self._group_audio_runtime = runtime
                self._group_audio_binding = binding
                raise
            except Exception:
                local_audio_stopped = False
        if not local_audio_stopped:
            # Keep the owned handle so a later cleanup can retry. Do not quit
            # the native room while its local audio route may still be live.
            self._group_audio_runtime = runtime
            self._group_audio_binding = binding
        native_quit_confirmed = scope is None and local_audio_stopped
        if (
            local_audio_stopped
            and scope is not None
            and requester is not None
            and service_meta is not None
            and isinstance(client_instance, str)
            and quit_native
        ):
            native_quit_confirmed = await self._quit_owned_native_group(
                scope,
                requester,
                identity,
                service_meta,
                client_instance,
                before_dispatch=before_native_quit,
            )
        if scope is not None and not native_quit_confirmed:
            self._group_audio_native_quit_unconfirmed = True
            if self._group_audio_last_failure_code is None or reason == "stopped":
                self._group_audio_last_failure_code = "group_native_quit_unconfirmed"
            # Retain the exact owned native scope as a quarantine. Further
            # joins remain blocked until native ownership can be disproved.
            self._group_join_scope = scope
            self._group_join_meta = service_meta
            self._group_join_client_instance = client_instance
            self._group_join_identity = tuple(identity.get("identity") or ())
            self._group_audio_requester_user_id = requester
        else:
            self._group_audio_native_quit_unconfirmed = False
            self._group_audio_requester_user_id = None
            self._group_join_scope = None
            self._group_join_meta = None
            self._group_join_client_instance = None
            self._group_join_identity = None
        self._group_audio_plan_fingerprint = None
        return native_quit_confirmed

    async def _process_group_audio_status_request(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read a live accepted-result proof; never install or create a binding."""

        request_id = str(request["request_id"])
        requester = str(request["peer_user_id"])
        group_id = str(request["group_id"])
        scope_group = request["scope_group"]
        claim = request["audio_claim"]
        service_meta = self._service_meta
        client_instance = self._client_instance
        binding = self._group_audio_binding
        group_runtime = self._group_audio_runtime
        active = False

        if isinstance(service_meta, Mapping) and isinstance(client_instance, str):
            frozen_meta = dict(service_meta)
            if (
                isinstance(scope_group, Mapping)
                and scope_group.get("groupId") == group_id
                and await self._group_audio_runtime_current(identity, frozen_meta, client_instance)
            ):
                try:
                    await self._admitted_group(requester, group_id, identity)
                except QQCallError:
                    pass
                else:
                    if await self._group_audio_context_current(
                        identity, frozen_meta, client_instance, requester, binding, scope_group
                    ):
                        try:
                            verified = await group_runtime.verify_result(scope_group, claim)
                        except Exception:
                            verified = False
                        if verified is True:
                            try:
                                await self._admitted_group(requester, group_id, identity)
                            except QQCallError:
                                verified = False
                            if verified is True:
                                # Lease validation is the final awaited proof
                                # step; finish with only local state checks.
                                try:
                                    verified = await group_runtime.verify_result(scope_group, claim)
                                except Exception:
                                    verified = False
                                active = bool(
                                    verified is True
                                    and self._group_audio_binding is binding
                                    and self._group_audio_requester_user_id == requester
                                    and self._service_meta == frozen_meta
                                    and self._client_instance == client_instance
                                    and self._lease_claimed
                                    and self._runtime_identity_matches(identity)
                                    and self._active_call is None
                                    and scope_group.get("serviceInstance") == frozen_meta.get("serviceInstance")
                                    and scope_group.get("hostGeneration") == frozen_meta.get("hostGeneration")
                                    and scope_group.get("connectionGeneration") == frozen_meta.get("connectionGeneration")
                                    and self.config.enabled
                                    and not self._closing
                                    and not self._stopped
                                )

        return self._make_response(
            request_id,
            "group_audio_status",
            requester,
            data={"active": active},
        )

    async def _guard_group_audio_status_response(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            request.get("operation") != "group_audio_status"
            or response.get("status") != "ok"
            or not isinstance(response.get("data"), Mapping)
            or response["data"].get("active") is not True
        ):
            return response
        request_id = str(request["request_id"])
        requester = str(request["peer_user_id"])
        if identity is None:
            return self._make_response(
                request_id, "group_audio_status", requester, data={"active": False}
            )
        scope_group = request["scope_group"]
        claim = request["audio_claim"]
        group_id = str(request["group_id"])
        service_meta = self._service_meta
        client_instance = self._client_instance
        binding = self._group_audio_binding
        group_runtime = self._group_audio_runtime
        if (
            not isinstance(service_meta, Mapping)
            or not isinstance(client_instance, str)
            or binding is None
            or group_runtime is None
        ):
            return self._make_response(
                request_id, "group_audio_status", requester, data={"active": False}
            )
        frozen_meta = dict(service_meta)
        if not await self._group_audio_context_current(
            identity, frozen_meta, client_instance, requester, binding, scope_group
        ):
            return self._make_response(
                request_id, "group_audio_status", requester, data={"active": False}
            )
        try:
            await self._admitted_group(requester, group_id, identity)
            verified = await group_runtime.verify_result(scope_group, claim)
        except Exception:
            verified = False
        active = bool(
            verified is True
            and self._group_audio_binding is binding
            and self._group_audio_requester_user_id == requester
            and self._service_meta == frozen_meta
            and self._client_instance == client_instance
            and self._lease_claimed
            and self._runtime_identity_matches(identity)
            and self._active_call is None
            and scope_group.get("serviceInstance") == frozen_meta.get("serviceInstance")
            and scope_group.get("hostGeneration") == frozen_meta.get("hostGeneration")
            and scope_group.get("connectionGeneration") == frozen_meta.get("connectionGeneration")
            and self.config.enabled
            and not self._closing
            and not self._stopped
        )
        return self._make_response(
            request_id,
            "group_audio_status",
            requester,
            data={"active": active},
        )

    async def _guard_group_join_response(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Do not return cached or late native join success after room/audio loss."""
        if request.get("operation") != "join_group" or response.get("status") != "ok":
            return response
        request_id = str(request["request_id"])
        requester = str(request["peer_user_id"])
        group_id = str(request["group_id"])
        data = response.get("data")
        try:
            if (
                type(data) is not dict
                or set(data) != _GROUP_JOIN_RESULT_KEYS
                or type(data.get("version")) is not int
                or data.get("version") != 1
                or data.get("accountId") != self.config.account_id
                or not _is_int(data.get("hostGeneration"))
                or not _is_int(data.get("connectionGeneration"))
                or data.get("joined") is not True
                or data.get("source") != "native_scene3_relation"
                or data.get("requesterUserId") != requester
            ):
                raise ValueError("invalid joined result")
            scope = validate_group_audio_scope(data.get("scope"))
        except (TypeError, ValueError):
            return self._make_response(
                request_id, "join_group", requester, error_code="group_join_response_invalid"
            )

        service_meta = self._group_join_meta
        client_instance = self._group_join_client_instance
        runtime = self._group_audio_runtime
        binding = self._group_audio_binding
        if (
            identity is None
            or not isinstance(service_meta, Mapping)
            or not isinstance(client_instance, str)
            or runtime is None
            or binding is None
            or self._group_join_scope != scope
            or self._group_audio_requester_user_id != requester
            or scope.get("groupId") != group_id
            or any(data.get(key) != service_meta.get(key) for key in (
                "serviceInstance", "hostGeneration", "connectionGeneration",
            ))
            or any(scope.get(key) != service_meta.get(key) for key in (
                "serviceInstance", "hostGeneration", "connectionGeneration",
            ))
            or getattr(runtime, "binding", None) is not binding
            or runtime.status().get("ready") is not True
        ):
            await self._stop_group_audio("group_join_response_invalid", quit_native=True)
            return self._make_response(
                request_id, "join_group", requester, error_code="group_join_response_invalid"
            )

        frozen_meta = dict(service_meta)
        current = False
        try:
            await self._admitted_group(requester, group_id, identity)
            current = await self._read_native_group_current(
                scope, requester, frozen_meta, client_instance,
            )
        except Exception:
            current = False
        # The native current endpoint and admission callback are independent
        # awaited proofs. Finish on local ownership and a fresh Core/adapter
        # identity check so a stale result cannot be sent after teardown.
        owner_current = bool(
            current
            and self._group_audio_runtime is runtime
            and self._group_audio_binding is binding
            and self._group_audio_requester_user_id == requester
            and self._group_join_scope == scope
            and self._group_join_client_instance == client_instance
            and self._group_join_meta == frozen_meta
            and getattr(runtime, "binding", None) is binding
            and runtime.status().get("ready") is True
        )
        if owner_current:
            owner_current = await self._group_audio_runtime_current(
                identity, frozen_meta, client_instance,
            )
        if owner_current:
            owner_current = bool(
                self._group_audio_local_context_current(
                    scope, requester, identity, frozen_meta, client_instance,
                )
                and self._group_audio_runtime is runtime
                and self._group_audio_binding is binding
                and runtime.status().get("ready") is True
            )
        if not owner_current:
            await self._stop_group_audio("group_join_response_invalid", quit_native=True)
            return self._make_response(
                request_id, "join_group", requester, error_code="group_join_response_invalid"
            )
        return response

    async def _guard_group_status_response(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        response: dict[str, Any],
        validation_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if request.get("operation") != "group_status" or response.get("status") != "ok":
            return response
        request_id = str(request["request_id"])
        peer_user_id = str(request["peer_user_id"])
        group_id = str(request["group_id"])
        result = validation_context.get("result")
        service_meta = validation_context.get("service_meta")
        client_instance = validation_context.get("client_instance")
        if (
            identity is None
            or not isinstance(result, Mapping)
            or not isinstance(service_meta, Mapping)
            or not isinstance(client_instance, str)
        ):
            return self._make_response(request_id, "group_status", peer_user_id, error_code="group_status_invalid")
        if self._closing or self._stopped:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="runtime_stopped")
        if not self.config.enabled:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="qqcall_disabled")
        if not self._core_ready or identity.get("core_ready") is False:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="core_not_ready")
        if not self._lease_claimed or not self._client_instance:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="adapter_lease_required")
        if self._service_meta != service_meta or self._client_instance != client_instance:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="stale_generation")
        try:
            await self._admitted_group(peer_user_id, group_id, identity)
        except QQCallError as exc:
            return self._make_response(request_id, "group_status", peer_user_id, error_code=exc.code)
        # The admission callback and route checks above are asynchronous. Recheck
        # all adapter-owned bindings after they return, immediately before send.
        if not self._core_ready or identity.get("core_ready") is False:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="core_not_ready")
        if not self._lease_claimed or not self._client_instance:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="adapter_lease_required")
        if self._service_meta != service_meta or self._client_instance != client_instance:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="stale_generation")
        if self._closing or self._stopped:
            return self._make_response(request_id, "group_status", peer_user_id, error_code="runtime_stopped")
        try:
            data = self._project_group_diagnostics(
                result,
                group_id=group_id,
                service_meta=service_meta,
                now_ms=int(time.time() * 1000),
            )
        except QQCallError as exc:
            return self._make_response(request_id, "group_status", peer_user_id, error_code=exc.code)
        return self._make_response(request_id, "group_status", peer_user_id, data=data)

    async def _process_group_join_request(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any],
        *,
        invitation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = str(request["request_id"])
        peer_user_id = str(request["peer_user_id"])
        group_id = str(request["group_id"])
        trigger_kind = "invite" if invitation is not None else match_group_call_trigger(request["trigger_text"])
        if trigger_kind is None:
            return self._make_response(request_id, "join_group", peer_user_id, error_code="group_trigger_invalid")
        if self._group_join_scope is not None or self._group_audio_runtime is not None:
            return self._make_response(request_id, "join_group", peer_user_id, error_code="group_audio_busy")
        if not self._lease_claimed or not self._client_instance or not isinstance(self._service_meta, Mapping):
            return self._make_response(request_id, "join_group", peer_user_id, error_code="adapter_lease_required")
        service_meta = dict(self._service_meta)
        client_instance = self._client_instance
        if not isinstance(client_instance, str):
            return self._make_response(request_id, "join_group", peer_user_id, error_code="adapter_lease_required")
        try:
            await self._admitted_group(peer_user_id, group_id, identity, require_core=False)
        except QQCallError as exc:
            return self._make_response(request_id, "join_group", peer_user_id, error_code=exc.code)
        dependency_failure = await self._dependency_failure(identity)
        if dependency_failure:
            return self._make_response(request_id, "join_group", peer_user_id, error_code=dependency_failure)
        if (
            self._closing
            or self._stopped
            or self._service_meta != service_meta
            or not self._lease_claimed
            or self._client_instance != client_instance
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
        ):
            return self._make_response(request_id, "join_group", peer_user_id, error_code="platform_identity_changed")
        try:
            status = await self._post("/v1/group/status", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "groupId": group_id,
            })
        except Exception as exc:
            code = self._error_code(exc, "group_status_failed")
            return self._make_response(request_id, "join_group", peer_user_id, error_code=code)
        if (
            self._service_meta != service_meta
            or not self._lease_claimed
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
        ):
            return self._make_response(request_id, "join_group", peer_user_id, error_code="platform_identity_changed")
        decision = group_status_decision(
            status,
            account_id=self.config.account_id,
            service_meta=service_meta,
            group_id=group_id,
            local_call_scope=self._active_call.get("scope") if self._active_call else None,
            now_ms=int(time.time() * 1000),
        )
        if decision is not None:
            return self._make_response(request_id, "join_group", peer_user_id, error_code=decision)
        try:
            # Re-read the room proof after fresh admission. A live room that
            # changes between status checks is not a join target.
            await self._admitted_group(peer_user_id, group_id, identity)
            fresh_status = await self._post("/v1/group/status", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "groupId": group_id,
            })
            fresh_decision = group_status_decision(
                fresh_status,
                account_id=self.config.account_id,
                service_meta=service_meta,
                group_id=group_id,
                local_call_scope=self._active_call.get("scope") if self._active_call else None,
                now_ms=int(time.time() * 1000),
                expected_room=status.get("room") if isinstance(status, Mapping) else None,
            )
            if fresh_decision is not None:
                return self._make_response(
                    request_id, "join_group", peer_user_id, error_code=fresh_decision
                )
            if (
                self._service_meta != service_meta
                or not self._lease_claimed
                or self._client_instance != client_instance
                or not await self._identity_unchanged(identity, require_verified=True)
            ):
                return self._make_response(
                    request_id, "join_group", peer_user_id, error_code="platform_identity_changed"
                )
            dependency_failure = await self._dependency_failure()
            if dependency_failure:
                return self._make_response(request_id, "join_group", peer_user_id, error_code=dependency_failure)
            if not await self._identity_unchanged(identity, require_verified=True) or self._service_meta != service_meta:
                return self._make_response(request_id, "join_group", peer_user_id, error_code="platform_identity_changed")
            result = await self._post("/v1/group/join", {
                "accountId": self.config.account_id,
                "clientInstance": client_instance,
                "groupId": group_id,
                "requesterUserId": peer_user_id,
                "triggerKind": trigger_kind,
                **({"inviteId": invitation["inviteId"]} if invitation is not None else {}),
            })
        except Exception as exc:
            code = self._error_code(exc, "group_join_failed")
            if code not in {"call_occupied", "unsupported_unverified_group_join"}:
                code = "group_join_failed"
            return self._make_response(request_id, "join_group", peer_user_id, error_code=code)
        try:
            if (
                type(result) is not dict
                or set(result) != _GROUP_JOIN_RESULT_KEYS
                or type(result.get("version")) is not int
                or result.get("version") != 1
                or result.get("accountId") != self.config.account_id
                or not _is_int(result.get("hostGeneration"))
                or not _is_int(result.get("connectionGeneration"))
                or any(result.get(key) != service_meta.get(key) for key in (
                    "serviceInstance", "hostGeneration", "connectionGeneration",
                ))
                or result.get("joined") is not True
                or result.get("source") != "native_scene3_relation"
                or result.get("requesterUserId") != peer_user_id
            ):
                raise ValueError("invalid native join result")
            scope = validate_group_audio_scope(result.get("scope"))
            if (
                scope.get("groupId") != group_id
                or scope.get("serviceInstance") != service_meta.get("serviceInstance")
                or scope.get("hostGeneration") != service_meta.get("hostGeneration")
                or scope.get("connectionGeneration") != service_meta.get("connectionGeneration")
            ):
                raise ValueError("native join scope mismatch")
        except (TypeError, ValueError):
            return self._make_response(request_id, "join_group", peer_user_id, error_code="group_join_response_invalid")

        # Preserve this exact native room receipt as a temporary owner before
        # any fresh current-room, ACL, audio-plan, or route checks can fail.
        self._group_join_scope = dict(scope)
        self._group_join_meta = dict(service_meta)
        self._group_join_client_instance = client_instance
        self._group_join_identity = tuple(identity.get("identity") or ())
        self._group_audio_requester_user_id = peer_user_id
        self._group_audio_last_failure_code = None
        self._group_audio_native_quit_unconfirmed = False
        if (
            self._closing
            or self._stopped
            or self._client_instance != client_instance
            or self._service_meta != service_meta
            or not self._lease_claimed
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
        ):
            await self._stop_group_audio("platform_identity_changed", quit_native=True)
            return self._make_response(request_id, "join_group", peer_user_id, error_code="platform_identity_changed")
        current = False
        try:
            current = await self._read_native_group_current(scope, peer_user_id, service_meta, client_instance)
            await self._admitted_group(peer_user_id, group_id, identity)
        except QQCallError as exc:
            await self._stop_group_audio(exc.code, quit_native=True)
            return self._make_response(request_id, "join_group", peer_user_id, error_code=exc.code)
        except Exception:
            current = False
        if (
            not current
            or self._active_call is not None
            or not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
            or self._service_meta != service_meta
            or self._client_instance != client_instance
            or not self._lease_claimed
        ):
            await self._stop_group_audio("group_join_response_invalid", quit_native=True)
            return self._make_response(request_id, "join_group", peer_user_id, error_code="group_join_response_invalid")

        # Record only the exact room the native join response and independent
        # current-room endpoint agree on. No caller-provided scope can install
        # the later PCM binding.
        try:
            await self._admitted_group(peer_user_id, group_id, identity)
            if (
                not await self._identity_unchanged(identity, require_verified=True)
                or not await self._core_route_is_current(identity)
            ):
                raise QQCallError("platform_identity_changed")
            await self._activate_group_audio(
                scope, peer_user_id, identity, service_meta, client_instance,
            )
        except Exception as exc:
            code = self._error_code(exc, "group_audio_unavailable")
            await self._stop_group_audio(code, quit_native=True)
            if code not in {
                "group_not_admitted", "platform_identity_changed", "core_not_ready",
                "group_audio_busy", "audio_plan_unavailable", "audio_plan_replaced",
                "audio_disabled", "audio_config_invalid", "route_plan_unavailable",
                "route_plan_changed", "route_plan_mismatch", "route_capture_mode_unverified",
                "route_context_unverified", "route_proof_invalid", "route_proof_stale",
                "host_input_not_active", "host_output_not_active", "group_audio_route_unavailable",
            }:
                code = "group_audio_unavailable"
            return self._make_response(request_id, "join_group", peer_user_id, error_code=code)
        return self._make_response(
            request_id,
            "join_group",
            peer_user_id,
            data=dict(result),
        )

    async def _process_group_leave_request(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any],
        leave_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._group_leave_lock:
            request_id = str(request["request_id"])
            peer_user_id = str(request["peer_user_id"])
            group_id = str(request["group_id"])
            requester = str(request["requester_user_id"])

            if requester != peer_user_id:
                return self._make_response(
                    request_id, "leave_group", peer_user_id,
                    error_code="group_requester_mismatch",
                )
            if not self._core_ready or identity.get("core_ready") is False:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="core_not_ready",
                )
            if not self._lease_claimed or not self._client_instance:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="adapter_lease_required",
                )
            if self._group_join_scope is None:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="group_not_joined",
                )
            try:
                scope = validate_group_audio_scope(self._group_join_scope)
            except (TypeError, ValueError):
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="group_scope_stale",
                )
            service_meta = dict(self._group_join_meta) if isinstance(self._group_join_meta, Mapping) else None
            client_instance = self._group_join_client_instance
            owner_identity = self._group_join_identity
            owner_requester = self._group_audio_requester_user_id
            if scope["groupId"] != group_id:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="group_scope_stale",
                )
            if owner_requester != requester:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="group_requester_mismatch",
                )
            if (
                service_meta is None
                or not isinstance(client_instance, str)
                or owner_identity != tuple(identity.get("identity") or ())
                or not self._runtime_identity_matches(identity)
            ):
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="platform_identity_changed",
                )
            if self._active_call is not None or self._voice.status().get("ready") is True:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="group_audio_busy",
                )
            if not await self._group_audio_runtime_current(identity, service_meta, client_instance):
                code = "adapter_lease_required" if not self._lease_claimed else "stale_generation"
                return self._make_response(request_id, "leave_group", peer_user_id, error_code=code)
            try:
                await self._admitted_group(requester, group_id, identity)
            except QQCallError as exc:
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code=exc.code,
                )

            def owner_matches() -> bool:
                return bool(
                    self._group_join_scope == scope
                    and self._group_join_meta == service_meta
                    and self._group_join_client_instance == client_instance
                    and self._group_join_identity == owner_identity
                    and self._group_audio_requester_user_id == requester
                )

            if (
                not owner_matches()
                or not self._lease_claimed
                or self._client_instance != client_instance
                or self._service_meta != service_meta
                or not await self._identity_unchanged(identity, require_verified=True)
                or not await self._core_route_is_current(identity)
            ):
                return self._make_response(
                    request_id, "leave_group", peer_user_id, error_code="stale_generation",
                )

            target = {
                "group_id": group_id,
                "requester_user_id": requester,
                "scope": dict(scope),
                "service_meta": dict(service_meta),
                "client_instance": client_instance,
                "identity": tuple(identity.get("identity") or ()),
            }
            if leave_context is not None:
                leave_context["target"] = target

            async def revalidate_before_native_quit() -> bool:
                if (
                    not owner_matches()
                    or not self._lease_claimed
                    or self._client_instance != client_instance
                    or self._service_meta != service_meta
                    or not self._runtime_identity_matches(identity)
                    or self._active_call is not None
                    or self._voice.status().get("ready") is True
                    or not await self._identity_unchanged(identity, require_verified=True)
                    or not await self._core_route_is_current(identity)
                    or not await self._group_audio_runtime_current(identity, service_meta, client_instance)
                ):
                    return False
                try:
                    await self._admitted_group(requester, group_id, identity)
                except QQCallError:
                    return False
                return bool(
                    owner_matches()
                    and self._lease_claimed
                    and self._client_instance == client_instance
                    and self._service_meta == service_meta
                    and await self._identity_unchanged(identity, require_verified=True)
                    and await self._core_route_is_current(identity)
                )

            confirmed = await self._stop_group_audio(
                "stopped",
                quit_native=True,
                before_native_quit=revalidate_before_native_quit,
            )
            if leave_context is not None:
                target["audio_stop_completed"] = bool(
                    self._group_audio_runtime is None and self._group_audio_binding is None
                )
            if (
                confirmed
                and self._group_join_scope is None
                and self._group_audio_runtime is None
                and self._group_audio_binding is None
            ):
                return self._make_response(
                    request_id, "leave_group", peer_user_id, data={"left": True},
                )
            return self._make_response(
                request_id, "leave_group", peer_user_id,
                error_code="group_leave_unconfirmed",
            )

    @staticmethod
    def _scope_matches(received: Any, expected: Mapping[str, Any]) -> bool:
        if not isinstance(received, dict) or set(received) != _SCOPE_KEYS:
            return False
        if (
            not isinstance(received.get("serviceInstance"), str)
            or not _is_int(received.get("hostGeneration"))
            or not _is_int(received.get("connectionGeneration"))
            or not isinstance(received.get("callId"), str)
            or not _CALL_ID_RE.fullmatch(received["callId"])
            or not _is_int(received.get("callGeneration"))
        ):
            return False
        return received == dict(expected)

    def _make_response(
        self,
        request_id: str,
        operation: str,
        peer_user_id: str,
        *,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "version": 1,
            "request_id": request_id,
            "platform": self.platform,
            "operation": operation,
            "peer_user_id": peer_user_id,
            "status": "ok" if error_code is None else "error",
        }
        if error_code is None:
            response["data"] = data or {}
        else:
            response["error"] = {"code": error_code if _SAFE_CODE_RE.fullmatch(error_code) else "qqcall_failed"}
        return response

    @staticmethod
    def _stamp_response_request_scope(
        response: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind a response to the validated request scope used by Core correlation."""
        stamped = dict(response)
        stamped.pop("group_id", None)
        stamped.pop("scope_group", None)
        stamped.pop("audio_claim", None)
        if request.get("operation") in {"join_group", "leave_group", "group_status", "group_audio_status"}:
            group_id = request.get("group_id")
            if isinstance(group_id, str) and _ID_RE.fullmatch(group_id):
                stamped["group_id"] = group_id
        if request.get("operation") == "leave_group":
            requester = request.get("requester_user_id")
            if isinstance(requester, str) and _ID_RE.fullmatch(requester):
                stamped["requester_user_id"] = requester
        if request.get("operation") == "group_audio_status":
            try:
                stamped["scope_group"] = validate_group_audio_scope(request.get("scope_group"))
                stamped["audio_claim"] = validate_group_audio_claim(request.get("audio_claim"))
            except (TypeError, ValueError):
                stamped.pop("scope_group", None)
                stamped.pop("audio_claim", None)
        return stamped

    async def _process_request(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        *,
        group_status_context: dict[str, Any] | None = None,
        leave_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = request["request_id"]
        operation = request["operation"]
        peer_user_id = request["peer_user_id"]
        if self._closing or self._stopped:
            return self._make_response(request_id, operation, peer_user_id, error_code="runtime_stopped")
        if not self.config.enabled:
            return self._make_response(request_id, operation, peer_user_id, error_code="qqcall_disabled")
        if identity is None or not identity["connected"] or identity["account_id"] != self.config.account_id:
            return self._make_response(request_id, operation, peer_user_id, error_code="account_mismatch")
        if not self._runtime_identity_matches(identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="platform_identity_changed")
        if not await self._identity_unchanged(identity, require_verified=True):
            return self._make_response(request_id, operation, peer_user_id, error_code="platform_identity_changed")
        if operation == "group_status":
            return await self._process_group_status_request(request, identity, group_status_context)
        if operation == "leave_group":
            return await self._process_group_leave_request(request, identity, leave_context)
        if operation == "group_audio_status":
            return await self._process_group_audio_status_request(request, identity)
        if operation == "join_group":
            return await self._process_group_join_request(request, identity)
        if operation in {"accept", "hangup", "reject"} and (
            not self._core_ready or identity.get("core_ready") is False
        ):
            return self._make_response(request_id, operation, peer_user_id, error_code="core_not_ready")
        call = self._active_call
        if call is None or call.get("peer_user_id") != peer_user_id or not is_trusted_invite_correlation(call.get("inviteCorrelation")):
            return self._make_response(request_id, operation, peer_user_id, error_code="no_active_call")
        automatic_accept = request_id in self._auto_accept_request_ids
        if automatic_accept and operation != "accept":
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_invalid")
        if not self._peer_binding_current(call, self._service_meta):
            return self._make_response(request_id, operation, peer_user_id, error_code="peer_manifest_stale")
        if automatic_accept and not await self._auto_accept_policy_current(call, identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_policy_revoked")
        if operation == "status" and request.get("scope") is not None and not self._scope_matches(request["scope"], call["scope"]):
            return self._make_response(request_id, operation, peer_user_id, error_code="call_scope_stale")
        try:
            await self._admitted(peer_user_id, identity)
        except QQCallError as exc:
            return self._make_response(request_id, operation, peer_user_id, error_code=exc.code)
        if operation in {"accept", "hangup", "reject"} and not await self._core_route_is_current(identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="core_not_ready")
        if self._closing or self._stopped:
            return self._make_response(request_id, operation, peer_user_id, error_code="runtime_stopped")
        if self._active_call is None or self._active_call.get("scope") != call.get("scope"):
            return self._make_response(request_id, operation, peer_user_id, error_code="call_scope_stale")
        if not self._peer_binding_current(call, self._service_meta):
            return self._make_response(request_id, operation, peer_user_id, error_code="peer_manifest_stale")
        if automatic_accept and not await self._auto_accept_policy_current(call, identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_policy_revoked")
        if operation == "status":
            return self._make_response(request_id, operation, peer_user_id, data={
                "scope": dict(call["scope"]),
                "call": self._call_public(call),
                "peer_user_id": peer_user_id,
            })
        if not self._scope_matches(request["scope"], call["scope"]):
            return self._make_response(request_id, operation, peer_user_id, error_code="call_scope_stale")
        if not self._lease_claimed or not self._client_instance or not self._service_meta:
            return self._make_response(request_id, operation, peer_user_id, error_code="adapter_lease_required")
        if call["uncertain"]:
            return self._make_response(request_id, operation, peer_user_id, error_code="call_uncertain")
        if operation == "accept":
            if call["inviteCallbackReady"] is not True or call["phase"] != "invite_ready" or call["acceptState"] != "not_requested":
                return self._make_response(request_id, operation, peer_user_id, error_code="invite_callback_not_ready")
        if self._closing or self._stopped:
            return self._make_response(request_id, operation, peer_user_id, error_code="runtime_stopped")
        if not await self._identity_unchanged(identity, require_verified=True):
            return self._make_response(request_id, operation, peer_user_id, error_code="platform_identity_changed")
        if self._active_call is None or self._active_call.get("scope") != call.get("scope"):
            return self._make_response(request_id, operation, peer_user_id, error_code="call_scope_stale")
        if not self._peer_binding_current(call, self._service_meta):
            return self._make_response(request_id, operation, peer_user_id, error_code="peer_manifest_stale")
        if automatic_accept and not await self._auto_accept_policy_current(call, identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_policy_revoked")
        if operation in {"accept", "hangup", "reject"} and not await self._core_route_is_current(identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="core_not_ready")
        if automatic_accept and not await self._auto_accept_policy_current(call, identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_policy_revoked")
        payload = {
            "accountId": self.config.account_id,
            "clientInstance": self._client_instance,
            "operation": operation,
            "scope": dict(call["scope"]),
        }
        if operation == "accept":
            dependency_failure = await self._dependency_failure()
            if dependency_failure:
                if await self._identity_unchanged(identity, require_verified=True) and self._active_call is not None and self._active_call.get("scope") == call.get("scope"):
                    try:
                        await self._post("/v1/call/command", {**payload, "operation": "reject"})
                    except Exception:
                        self._emit_status("call_dependency_reject_unconfirmed")
                    if automatic_accept:
                        await self._notify_dependency_refusal(peer_user_id, None, dependency_failure, identity)
                return self._make_response(request_id, operation, peer_user_id, error_code=dependency_failure)
            if not await self._identity_unchanged(identity, require_verified=True) or self._active_call is None or self._active_call.get("scope") != call.get("scope"):
                return self._make_response(request_id, operation, peer_user_id, error_code="platform_identity_changed")
        try:
            result = await self._post("/v1/call/command", payload)
        except Exception as exc:
            code = self._error_code(exc, "command_failed")
            return self._make_response(request_id, operation, peer_user_id, error_code=code)
        if self._closing or self._stopped:
            return self._make_response(request_id, operation, peer_user_id, error_code="runtime_stopped")
        if not await self._identity_unchanged(identity, require_verified=True):
            return self._make_response(request_id, operation, peer_user_id, error_code="platform_identity_changed")
        if operation in {"accept", "hangup", "reject"} and not await self._core_route_is_current(identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="core_not_ready")
        if self._active_call is None or self._active_call.get("scope") != call.get("scope"):
            return self._make_response(request_id, operation, peer_user_id, error_code="call_scope_stale")
        if not self._peer_binding_current(call, self._service_meta):
            return self._make_response(request_id, operation, peer_user_id, error_code="peer_manifest_stale")
        if automatic_accept and not await self._auto_accept_policy_current(call, identity):
            return self._make_response(request_id, operation, peer_user_id, error_code="auto_accept_policy_revoked")
        if (
            set(result) != {"version", "accountId", "serviceInstance", "operation", "scope", "result"}
            or isinstance(result.get("version"), bool)
            or not isinstance(result.get("version"), int)
            or result.get("version") != 1
            or result.get("accountId") != self.config.account_id
            or result.get("serviceInstance") != call["scope"]["serviceInstance"]
            or result.get("operation") != operation
            or not self._scope_matches(result.get("scope"), call["scope"])
            or not isinstance(result.get("result"), dict)
        ):
            return self._make_response(request_id, operation, peer_user_id, error_code="service_command_invalid")
        command_result = result["result"]
        if (
            set(command_result) != {"posted", "id", "callId", "callGeneration"}
            or command_result.get("posted") is not True
            or not _is_int(command_result.get("id"))
            or command_result.get("callId") != call["callId"]
            or not _is_int(command_result.get("callGeneration"))
            or command_result.get("callGeneration") != call["generation"]
        ):
            return self._make_response(request_id, operation, peer_user_id, error_code="service_command_invalid")
        return self._make_response(request_id, operation, peer_user_id, data={
            "scope": dict(call["scope"]),
            "result": {
                "posted": True,
                "id": command_result["id"],
                "callId": call["callId"],
                "callGeneration": call["generation"],
            },
        })

    async def _refresh_group_status_duplicate(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request_id = str(request["request_id"])
        operation = str(request["operation"])
        while True:
            owner = False
            future: asyncio.Future[dict[str, Any]] | None = None
            response: dict[str, Any] | None = None
            async with self._request_lock:
                cached = self._request_cache.get(request_id)
                if cached is not None and cached[0] != fingerprint:
                    response = self._make_response(
                        request_id, operation, request["peer_user_id"],
                        error_code="duplicate_request_id",
                    )
                else:
                    inflight = self._request_inflight.get(request_id)
                    if inflight is not None:
                        if inflight[0] == fingerprint:
                            future = inflight[1]
                        else:
                            response = self._make_response(
                                request_id, operation, request["peer_user_id"],
                                error_code="duplicate_request_id",
                            )
                    elif len(self._request_inflight) >= _MAX_INFLIGHT_REQUESTS:
                        response = self._make_response(
                            request_id, operation, request["peer_user_id"],
                            error_code="runtime_busy",
                        )
                    else:
                        if cached is not None:
                            self._request_cache.move_to_end(request_id)
                        future = asyncio.get_running_loop().create_future()
                        self._request_inflight[request_id] = (fingerprint, future)
                        owner = True

            if response is not None:
                return response, {}
            if not owner:
                assert future is not None
                await asyncio.shield(future)
                continue

            assert future is not None
            validation_context: dict[str, Any] = {}
            try:
                response = await self._process_request(
                    request,
                    identity,
                    group_status_context=validation_context if operation == "group_status" else None,
                )
            except asyncio.CancelledError:
                response = self._make_response(
                    request_id, operation, request["peer_user_id"],
                    error_code="request_outcome_unknown",
                )
                await self._finish_request(request_id, fingerprint, request, response, future)
                raise
            except Exception as exc:
                response = self._make_response(
                    request_id, operation, request["peer_user_id"],
                    error_code=self._error_code(exc, "request_failed"),
                )
            await self._finish_request(request_id, fingerprint, request, response, future)
            return response, validation_context

    async def _refresh_group_leave_response(
        self,
        request: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
        fallback: Mapping[str, Any],
        fingerprint: str,
    ) -> dict[str, Any]:
        """Refresh a cached leave result without ever issuing another quit."""

        request_id = str(request["request_id"])
        async with self._request_lock:
            cached_target = self._request_leave_targets.get(request_id)
            if cached_target is None:
                if fallback.get("status") == "ok":
                    return self._make_response(
                        request_id, "leave_group", str(request["peer_user_id"]),
                        error_code="group_leave_unconfirmed",
                    )
                return dict(fallback)
            if cached_target[0] != fingerprint:
                return dict(fallback)
            target = dict(cached_target[1])

        def error(code: str) -> dict[str, Any]:
            return self._make_response(
                request_id, "leave_group", str(request["peer_user_id"]), error_code=code,
            )

        try:
            scope = validate_group_audio_scope(target.get("scope"))
        except (TypeError, ValueError):
            return error("group_scope_stale")
        group_id = request.get("group_id")
        requester = request.get("requester_user_id")
        peer_user_id = request.get("peer_user_id")
        service_meta = target.get("service_meta")
        client_instance = target.get("client_instance")
        owner_identity = target.get("identity")
        if (
            not isinstance(service_meta, Mapping)
            or not isinstance(client_instance, str)
            or not isinstance(owner_identity, tuple)
            or target.get("group_id") != group_id
            or scope.get("groupId") != group_id
        ):
            return error("group_scope_stale")
        if requester != peer_user_id or target.get("requester_user_id") != requester:
            return error("group_requester_mismatch")
        if identity is None or identity.get("connected") is not True:
            return error("platform_identity_changed")
        if tuple(identity.get("identity") or ()) != owner_identity or not self._runtime_identity_matches(identity):
            return error("platform_identity_changed")
        if not self._core_ready or identity.get("core_ready") is False:
            return error("core_not_ready")
        if not self._lease_claimed or self._client_instance != client_instance:
            return error("adapter_lease_required")
        if not isinstance(self._service_meta, Mapping) or dict(self._service_meta) != dict(service_meta):
            return error("stale_generation")
        if not await self._group_audio_runtime_current(identity, service_meta, client_instance):
            return error("stale_generation")
        if target.get("audio_stop_completed") is not True:
            return error("group_leave_unconfirmed")
        try:
            await self._admitted_group(str(requester), str(group_id), identity)
        except QQCallError as exc:
            return error(exc.code)
        if (
            not await self._native_group_is_absent(scope, service_meta, client_instance)
            or not await self._group_audio_runtime_current(identity, service_meta, client_instance)
        ):
            return error("group_leave_unconfirmed")
        try:
            await self._admitted_group(str(requester), str(group_id), identity)
        except QQCallError as exc:
            return error(exc.code)
        if (
            not await self._identity_unchanged(identity, require_verified=True)
            or not await self._core_route_is_current(identity)
            or not self._lease_claimed
            or self._client_instance != client_instance
            or self._service_meta != dict(service_meta)
        ):
            return error("group_leave_unconfirmed")

        if (
            self._group_join_scope == scope
            and self._group_join_meta == dict(service_meta)
            and self._group_join_client_instance == client_instance
            and self._group_join_identity == owner_identity
            and self._group_audio_requester_user_id == requester
        ):
            if self._group_audio_runtime is not None or self._group_audio_binding is not None:
                return error("group_leave_unconfirmed")
            self._group_join_scope = None
            self._group_join_meta = None
            self._group_join_client_instance = None
            self._group_join_identity = None
            self._group_audio_requester_user_id = None
            self._group_audio_native_quit_unconfirmed = False
            if self._group_audio_last_failure_code == "group_native_quit_unconfirmed":
                self._group_audio_last_failure_code = None
        return self._make_response(
            request_id, "leave_group", str(peer_user_id), data={"left": True},
        )

    async def handle_request(self, raw: Any) -> dict[str, Any] | None:
        request = self._request_envelope(raw, self.platform)
        if request is None:
            self._last_failure_code = "invalid_request"
            return None
        request_id = request["request_id"]
        try:
            identity = await self._read_platform_state()
        except QQCallError:
            identity = None
        try:
            fingerprint = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return None
        # Keep an owned snapshot of the normalized envelope. Duplicate handling,
        # processing, and cached responses must all refer to the same scope.
        request = json.loads(fingerprint)
        owner = False
        future: asyncio.Future[dict[str, Any]] | None = None
        response: dict[str, Any] | None = None
        group_status_context: dict[str, Any] = {}
        leave_context: dict[str, Any] = {}
        if request["operation"] in {"group_status", "group_audio_status"}:
            read_operation = request["operation"]
            async with self._request_lock:
                inflight = self._request_inflight.get(request_id)
                cached = self._request_cache.get(request_id)
                if inflight is not None:
                    if inflight[0] == fingerprint:
                        future = inflight[1]
                    else:
                        response = self._make_response(
                            request_id, read_operation, request["peer_user_id"],
                            error_code="duplicate_request_id",
                        )
                elif cached is not None and cached[0] != fingerprint:
                    self._request_cache.move_to_end(request_id)
                    response = self._make_response(
                        request_id, read_operation, request["peer_user_id"],
                        error_code="duplicate_request_id",
                    )
                elif len(self._request_inflight) >= _MAX_INFLIGHT_REQUESTS:
                    response = self._make_response(
                        request_id, read_operation, request["peer_user_id"],
                        error_code="runtime_busy",
                    )
                else:
                    if cached is not None:
                        self._request_cache.move_to_end(request_id)
                    future = asyncio.get_running_loop().create_future()
                    self._request_inflight[request_id] = (fingerprint, future)
                    owner = True
        else:
            async with self._request_lock:
                cached = self._request_cache.get(request_id)
                if cached is not None:
                    self._request_cache.move_to_end(request_id)
                    if cached[0] == fingerprint:
                        response = dict(cached[1])
                    else:
                        response = self._make_response(
                            request_id, request["operation"], request["peer_user_id"],
                            error_code="duplicate_request_id",
                        )
                else:
                    inflight = self._request_inflight.get(request_id)
                    if inflight is not None:
                        if inflight[0] == fingerprint:
                            future = inflight[1]
                        else:
                            response = self._make_response(
                                request_id, request["operation"], request["peer_user_id"],
                                error_code="duplicate_request_id",
                            )
                    elif len(self._request_inflight) >= _MAX_INFLIGHT_REQUESTS:
                        response = self._make_response(
                            request_id, request["operation"], request["peer_user_id"],
                            error_code="runtime_busy",
                        )
                    else:
                        future = asyncio.get_running_loop().create_future()
                        self._request_inflight[request_id] = (fingerprint, future)
                        owner = True

        if not owner and future is not None:
            response = await asyncio.shield(future)
            if request["operation"] in {"group_status", "group_audio_status"}:
                response, group_status_context = await self._refresh_group_status_duplicate(
                    request,
                    identity,
                    fingerprint,
                )
        elif owner:
            assert future is not None
            try:
                response = await self._process_request(
                    request,
                    identity,
                    group_status_context=group_status_context if request["operation"] == "group_status" else None,
                    leave_context=leave_context if request["operation"] == "leave_group" else None,
                )
            except asyncio.CancelledError:
                response = self._make_response(
                    request_id, request["operation"], request["peer_user_id"],
                    error_code="request_outcome_unknown",
                )
                await self._finish_request(
                    request_id, fingerprint, request, response, future,
                    leave_context=leave_context if request["operation"] == "leave_group" else None,
                )
                raise
            except Exception as exc:
                response = self._make_response(
                    request_id, request["operation"], request["peer_user_id"],
                    error_code=self._error_code(exc, "request_failed"),
                )
            await self._finish_request(
                request_id, fingerprint, request, response, future,
                leave_context=leave_context if request["operation"] == "leave_group" else None,
            )

        if response is None:
            return None
        if request_id in self._auto_accept_request_ids and response.get("status") == "ok":
            call = self._active_call
            if (
                call is None
                or call.get("scope") != request.get("scope")
                or not await self._auto_accept_policy_current(call, identity or {})
            ):
                response = self._make_response(
                    request_id,
                    request["operation"],
                    request["peer_user_id"],
                    error_code="auto_accept_policy_revoked",
                )
                async with self._request_lock:
                    cached = self._request_cache.get(request_id)
                    if cached is not None:
                        self._request_cache[request_id] = (cached[0], dict(response))
        response = self._stamp_response_request_scope(response, request)
        before_send: Callable[[], Awaitable[bool]] | None = None
        if request["operation"] == "join_group":
            async def validate_group_join_before_send() -> bool:
                try:
                    guarded = await self._guard_group_join_response(
                        request,
                        identity,
                        response,
                    )
                except Exception:
                    guarded = self._make_response(
                        request_id,
                        "join_group",
                        request["peer_user_id"],
                        error_code="group_join_response_invalid",
                    )
                guarded = dict(guarded)
                response.clear()
                response.update(self._stamp_response_request_scope(guarded, request))
                return True

            before_send = validate_group_join_before_send
        elif request["operation"] == "group_status":
            async def validate_group_status_before_send() -> bool:
                try:
                    guarded = await self._guard_group_status_response(
                        request,
                        identity,
                        response,
                        group_status_context,
                    )
                except Exception as exc:
                    guarded = self._make_response(
                        request_id,
                        "group_status",
                        request["peer_user_id"],
                        error_code=self._error_code(exc, "group_status_failed"),
                    )
                guarded = dict(guarded)
                response.clear()
                response.update(self._stamp_response_request_scope(guarded, request))
                return True

            before_send = validate_group_status_before_send
        elif request["operation"] == "group_audio_status":
            async def validate_group_audio_status_before_send() -> bool:
                try:
                    guarded = await self._guard_group_audio_status_response(
                        request,
                        identity,
                        response,
                    )
                except Exception:
                    guarded = self._make_response(
                        request_id,
                        "group_audio_status",
                        request["peer_user_id"],
                        data={"active": False},
                    )
                guarded = dict(guarded)
                response.clear()
                response.update(self._stamp_response_request_scope(guarded, request))
                return True

            before_send = validate_group_audio_status_before_send
        elif request["operation"] == "leave_group":
            async def validate_group_leave_before_send() -> bool:
                try:
                    guarded = await self._refresh_group_leave_response(
                        request, identity, response, fingerprint,
                    )
                except Exception:
                    guarded = self._make_response(
                        request_id, "leave_group", request["peer_user_id"],
                        error_code="group_leave_unconfirmed",
                    )
                guarded = self._stamp_response_request_scope(guarded, request)
                response.clear()
                response.update(guarded)
                async with self._request_lock:
                    cached = self._request_cache.get(request_id)
                    if cached is not None and cached[0] == fingerprint:
                        self._request_cache[request_id] = (cached[0], dict(response))
                return True

            before_send = validate_group_leave_before_send
        if not await self._send_custom_response(response, fence=identity, before_send=before_send):
            self._last_failure_code = "core_response_send_failed"
            return None
        return response

    async def _finish_request(
        self,
        request_id: str,
        fingerprint: str,
        request: Mapping[str, Any],
        response: dict[str, Any],
        future: asyncio.Future[dict[str, Any]],
        *,
        leave_context: Mapping[str, Any] | None = None,
    ) -> None:
        response = self._stamp_response_request_scope(response, request)
        async with self._request_lock:
            self._request_cache[request_id] = (fingerprint, dict(response))
            self._request_cache.move_to_end(request_id)
            target = leave_context.get("target") if isinstance(leave_context, Mapping) else None
            if request.get("operation") == "leave_group" and isinstance(target, Mapping):
                self._request_leave_targets[request_id] = (
                    fingerprint,
                    {
                        "group_id": target.get("group_id"),
                        "requester_user_id": target.get("requester_user_id"),
                        "scope": dict(target.get("scope", {})),
                        "service_meta": dict(target.get("service_meta", {})),
                        "client_instance": target.get("client_instance"),
                        "identity": tuple(target.get("identity", ())),
                        "audio_stop_completed": target.get("audio_stop_completed") is True,
                    },
                )
                self._request_leave_targets.move_to_end(request_id)
            while len(self._request_cache) > _REQUEST_CACHE_SIZE:
                expired_id, _expired_value = self._request_cache.popitem(last=False)
                self._request_leave_targets.pop(expired_id, None)
            while len(self._request_leave_targets) > _REQUEST_CACHE_SIZE:
                self._request_leave_targets.popitem(last=False)
            self._request_inflight.pop(request_id, None)
            if not future.done():
                future.set_result(dict(response))

    async def _reject_group_delivery_target(self, target: Mapping[str, Any], raw: Any = None) -> bool:
        scope_keys = _GROUP_SCOPE_KEYS
        required_target_keys = scope_keys | {"requester_user_id", "turn_id"}
        request_id_value = target.get("request_id")
        request_id = (
            request_id_value
            if isinstance(request_id_value, str) and _REQUEST_ID_RE.fullmatch(request_id_value)
            else None
        )

        scope: dict[str, Any] | None = None
        requester: str | None = None
        turn_id: str | None = None
        try:
            target_keys = set(target)
            if target_keys not in (required_target_keys, required_target_keys | {"request_id"}):
                raise ValueError("group delivery target is invalid")
            scope = validate_group_audio_scope({key: target.get(key) for key in scope_keys})
            requester = target.get("requester_user_id")
            turn_id = target.get("turn_id")
            if (
                type(requester) is not str
                or not _ID_RE.fullmatch(requester)
                or type(turn_id) is not str
                or not _UID_RE.fullmatch(turn_id)
                or ("request_id" in target and request_id is None)
            ):
                raise ValueError("group delivery target is invalid")
        except (TypeError, ValueError):
            scope = None
            requester = None
            turn_id = None

        code = "stale_group_scope" if scope is None else "group_audio_unavailable"
        delivery: dict[str, Any] | None = None
        runtime = self._group_audio_runtime
        meta = dict(self._group_join_meta) if isinstance(self._group_join_meta, Mapping) else None
        client_instance = self._group_join_client_instance
        identity = {
            "identity": self._group_join_identity,
            "account_id": self.config.account_id,
            "connected": True,
            "core_ready": self._core_ready,
        }
        owns_target = bool(
            scope is not None
            and requester is not None
            and turn_id is not None
            and self.config.enabled
            and runtime is not None
            and meta is not None
            and isinstance(client_instance, str)
            and self._group_join_scope == scope
            and self._group_audio_requester_user_id == requester
            and self._active_call is None
            and self._voice.status().get("ready") is not True
            and self._group_audio_local_context_current(
                scope, requester, identity, meta, client_instance,
            )
            and runtime.status().get("ready") is True
        )

        if owns_target and scope is not None and requester is not None and turn_id is not None:
            try:
                if not await runtime.verify_current_turn(scope, requester, turn_id):
                    code = "stale_group_scope"
                else:
                    receipt: dict[str, Any] = {
                        "accepted": True,
                        "code": "display_text_acknowledged",
                        "drain_confirmed": False,
                    }
                    for kind, data in _delivery_segments(raw):
                        if not await runtime.verify_current_turn(scope, requester, turn_id):
                            raise QQCallError("stale_group_scope")
                        if kind in {"text", "tts_text", "reply"}:
                            continue
                        if kind not in {"voice", "voice_stream"}:
                            receipt = {
                                "accepted": True,
                                "code": "unsupported_group_segment_consumed",
                                "drain_confirmed": False,
                            }
                            continue
                        played = await runtime.play_core_segment(
                            scope, requester, turn_id, kind, data,
                        )
                        drained = bool(
                            isinstance(played, Mapping)
                            and played.get("render_drained") is True
                            and played.get("drain_confirmed") is True
                        )
                        receipt = {
                            "accepted": True,
                            "code": "local_endpoint_drained" if drained else "local_endpoint_queued",
                            "drain_confirmed": drained,
                        }
                    if not await runtime.verify_current_turn(scope, requester, turn_id):
                        raise QQCallError("stale_group_scope")
                    delivery = receipt
                    code = receipt["code"]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                candidate = getattr(exc, "code", None)
                code = candidate if isinstance(candidate, str) and _SAFE_CODE_RE.fullmatch(candidate) else "audio_delivery_failed"
        elif scope is not None:
            if runtime is not None and runtime.status().get("ready") is True:
                code = "stale_group_scope"

        response: dict[str, Any] = {
            "version": 1,
            "platform": self.platform,
            "status": "accepted" if delivery and delivery.get("accepted") is True else "error",
        }
        if delivery and delivery.get("accepted") is True:
            response["result"] = {
                "code": code,
                "drain_confirmed": delivery.get("drain_confirmed") is True,
            }
        else:
            response["error"] = {"code": code}
        if request_id is not None:
            response["request_id"] = request_id
        self._last_failure_code = None if delivery and delivery.get("accepted") is True else code
        self._emit_status("delivery_accepted" if delivery and delivery.get("accepted") is True else "delivery_rejected")
        try:
            # A group stream may dispatch one Core message per small chunk, so
            # use the runtime's local turn fence instead of polling HTTP or the
            # native control endpoint on each playback callback.
            if delivery and delivery.get("accepted") is True and not (
                scope is not None
                and requester is not None
                and turn_id is not None
                and meta is not None
                and isinstance(client_instance, str)
                and self._group_audio_local_context_current(
                    scope, requester, identity, meta, client_instance,
                )
                and runtime is not None
                and await runtime.verify_current_turn(scope, requester, turn_id)
            ):
                response = {
                    "version": 1,
                    "platform": self.platform,
                    "status": "error",
                    "error": {"code": "stale_group_scope"},
                }
                if request_id is not None:
                    response["request_id"] = request_id
                self._last_failure_code = "stale_group_scope"
                self._emit_status("delivery_rejected")
            sent = await asyncio.wait_for(
                self._call(self._send_custom, "qqcall_delivery_response", response),
                timeout=_CORE_SEND_TIMEOUT_SEC,
            )
            if sent is not True or (
                delivery and delivery.get("accepted") is True
                and not (
                    scope is not None
                    and requester is not None
                    and turn_id is not None
                    and meta is not None
                    and isinstance(client_instance, str)
                    and self._group_audio_local_context_current(
                        scope, requester, identity, meta, client_instance,
                    )
                    and runtime is not None
                    and await runtime.verify_current_turn(scope, requester, turn_id)
                )
            ):
                self._emit_status("delivery_acceptance_unconfirmed" if delivery else "delivery_rejection_unconfirmed")
        except Exception:
            self._emit_status("delivery_acceptance_unconfirmed" if delivery else "delivery_rejection_unconfirmed")
        return True

    async def intercept_outbound(self, raw: Any) -> bool:
        additional = _message_additional_config(raw)
        target = additional.get("delivery_target")
        if isinstance(target, Mapping) and target.get("kind") == "qqcall_group":
            return await self._reject_group_delivery_target(target, raw)
        if not isinstance(target, Mapping) or target.get("kind") != "qqcall":
            return False
        scope = {key: target.get(key) for key in _SCOPE_KEYS}
        peer = target.get("peer_user_id")
        code = "audio_unavailable"
        delivery: dict[str, Any] | None = None
        if not self.config.enabled:
            code = "audio_unavailable"
        elif (
            not isinstance(scope["serviceInstance"], str)
            or not _is_int(scope["hostGeneration"])
            or not _is_int(scope["connectionGeneration"])
            or not isinstance(scope["callId"], str)
            or not _CALL_ID_RE.fullmatch(scope["callId"])
            or not _is_int(scope["callGeneration"])
            or not isinstance(peer, str)
            or not _ID_RE.fullmatch(peer)
        ):
            code = "stale_call_scope"
        else:
            call = self._active_call
            if (
                call is None
                or not self._scope_matches(scope, call["scope"])
                or call.get("peer_user_id") != peer
                or not is_trusted_invite_correlation(call.get("inviteCorrelation"))
            ):
                code = "stale_call_scope"
            elif self._voice.status().get("ready") is True:
                delivery = await self._voice.intercept_outbound(raw, target)
                code = str(delivery.get("code") or "audio_delivery_failed")
            else:
                code = str(self._voice.status().get("unavailableReason") or "audio_unavailable")
        request_id = target.get("request_id")
        response: dict[str, Any] = {
            "version": 1,
            "platform": self.platform,
            "status": "accepted" if delivery and delivery.get("accepted") is True else "error",
        }
        if delivery and delivery.get("accepted") is True:
            response["result"] = {
                "code": code,
                "drain_confirmed": delivery.get("drain_confirmed") is True,
            }
        else:
            response["error"] = {"code": code}
        if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id):
            response["request_id"] = request_id
        self._last_failure_code = None if delivery and delivery.get("accepted") is True else code
        self._emit_status("delivery_accepted" if delivery and delivery.get("accepted") is True else "delivery_rejected")
        try:
            identity = await self._read_platform_state()
            sent = await asyncio.wait_for(
                self._call(self._send_custom, "qqcall_delivery_response", response),
                timeout=_CORE_SEND_TIMEOUT_SEC,
            )
            current = await self._read_platform_state()
            if not self._same_identity(identity, current) or sent is not True:
                self._emit_status(
                    "delivery_acceptance_unconfirmed"
                    if delivery and delivery.get("accepted") is True
                    else "delivery_rejection_unconfirmed"
                )
        except Exception:
            self._emit_status(
                "delivery_acceptance_unconfirmed"
                if delivery and delivery.get("accepted") is True
                else "delivery_rejection_unconfirmed"
            )
        return True


__all__ = [
    "QQCallConfig",
    "QQCallError",
    "QQCallRuntime",
    "is_qqcall_delivery_target",
    "match_group_call_trigger",
]
