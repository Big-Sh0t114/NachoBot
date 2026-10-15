"""Small bounded OneBot v11 HTTP and WebSocket API client for local adapters."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import secrets
import socket
import ssl
import struct
import threading
import time
from typing import Any
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024
_TIMEOUT_SECONDS = 3.0
_MAX_CONCURRENT_HTTP_EXCHANGES = 4


class OneBotError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OneBotError("onebot_response_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise OneBotError("onebot_response_invalid")


def _parse_json(raw: bytes, expected_data_type: type[dict | list]) -> dict[str, Any] | list[Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OneBotError("onebot_response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise OneBotError("onebot_response_invalid") from None
    if type(value) is not dict or value.get("status") != "ok":
        raise OneBotError("onebot_action_failed")
    retcode = value.get("retcode")
    if type(retcode) is not int or retcode != 0:
        raise OneBotError("onebot_action_failed")
    data = value.get("data")
    if type(data) is not expected_data_type:
        raise OneBotError("onebot_response_invalid")
    if expected_data_type is list and any(type(item) is not dict for item in data):
        raise OneBotError("onebot_response_invalid")
    return data


class OneBotClient:
    """Perform only explicit read actions against the configured loopback API."""

    def __init__(self, url: str, token: str | None = None, *, timeout: float = _TIMEOUT_SECONDS):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https", "ws", "wss") or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise OneBotError("onebot_url_invalid")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise OneBotError("onebot_url_invalid")
        if token is not None and (type(token) is not str or not token or len(token) > 4096 or any(ch in token for ch in "\r\n\x00")):
            raise OneBotError("onebot_token_invalid")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or timeout > 30:
            raise OneBotError("onebot_timeout_invalid")
        self._parsed = parsed
        self._token = token
        self._timeout = float(timeout)
        self._http_transport_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_HTTP_EXCHANGES)

    def set_http_transport_limiter(self, limiter: threading.BoundedSemaphore) -> None:
        """Bind a caller-owned cap shared by HTTP exchanges across client instances."""
        if not isinstance(limiter, threading.BoundedSemaphore):
            raise OneBotError("onebot_transport_limiter_invalid")
        self._http_transport_slots = limiter

    def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if type(action) is not str or action not in ("get_status", "get_login_info", "get_stranger_info"):
            raise OneBotError("onebot_action_unsupported")
        if params is not None and type(params) is not dict:
            raise OneBotError("onebot_request_invalid")
        return self._request(action, params or {}, dict, timeout)

    def get_friend_list(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        """Return the OneBot friend list through its list-typed read contract."""
        return self._request("get_friend_list", {}, list, timeout)

    def _request(
        self,
        action: str,
        params: dict[str, Any],
        expected_data_type: type[dict | list],
        timeout: float | None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if action not in ("get_status", "get_login_info", "get_stranger_info", "get_friend_list"):
            raise OneBotError("onebot_action_unsupported")
        if type(params) is not dict:
            raise OneBotError("onebot_request_invalid")
        if timeout is None:
            effective_timeout = self._timeout
        else:
            if (type(timeout) not in (int, float) or not math.isfinite(timeout) or
                    timeout <= 0 or timeout > 30):
                raise OneBotError("onebot_timeout_invalid")
            effective_timeout = min(self._timeout, float(timeout))
        deadline = time.monotonic() + effective_timeout
        body = json.dumps({"action": action, "params": params, "echo": secrets.token_urlsafe(18)},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._parsed.scheme in ("http", "https"):
            return self._http(action, params, expected_data_type, deadline)
        return self._websocket(body, expected_data_type, deadline)

    def _http(
        self,
        action: str,
        params: dict[str, Any],
        expected_data_type: type[dict | list],
        deadline: float,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        secure = self._parsed.scheme == "https"
        host = self._parsed.hostname or ""
        port = self._parsed.port or (443 if secure else 80)
        connection_type = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        transport_slots = self._http_transport_slots
        if not transport_slots.acquire(timeout=_remaining(deadline)):
            raise OneBotError("onebot_timeout")
        worker_started = False
        connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            timeout = _remaining(deadline)
            connection = connection_type(host, port, timeout=timeout)
            base_path = self._parsed.path.rstrip("/")
            path = f"{base_path}/{action}"
            payload = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
                "Content-Length": str(len(payload)),
            }
            if self._token is not None:
                headers["Authorization"] = f"Bearer {self._token}"
            cancelled = threading.Event()
            outcome: dict[str, Any] = {}

            def exchange() -> None:
                response: http.client.HTTPResponse | None = None
                try:
                    if cancelled.is_set():
                        return
                    connection.timeout = _remaining(deadline)
                    connection.request("POST", path, body=payload, headers=headers)
                    if connection.sock is not None:
                        connection.sock.settimeout(_remaining(deadline))
                    response = connection.getresponse()
                    if response.status != 200:
                        raise OneBotError("onebot_http_failed")
                    raw = bytearray()
                    while len(raw) <= MAX_RESPONSE_BYTES:
                        remaining_capacity = MAX_RESPONSE_BYTES + 1 - len(raw)
                        if remaining_capacity <= 0:
                            break
                        response_socket = _http_response_socket(response)
                        if response_socket is not None:
                            response_socket.settimeout(_remaining(deadline))
                        chunk = response.read1(min(64 * 1024, remaining_capacity))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    outcome["data"] = _parse_json(bytes(raw), expected_data_type)
                except (OneBotError, OSError, TimeoutError, http.client.HTTPException, ValueError, struct.error) as exc:
                    outcome["error"] = exc
                finally:
                    try:
                        if response is not None:
                            response.close()
                    finally:
                        try:
                            connection.close()
                        finally:
                            # The exchange thread owns this slot after start;
                            # caller timeout does not free capacity while the
                            # underlying HTTP operation is still alive.
                            transport_slots.release()

            worker = threading.Thread(target=exchange, name="qqcall-onebot-http", daemon=True)
            try:
                worker.start()
            except (OSError, RuntimeError):
                connection.close()
                raise OneBotError("onebot_unavailable") from None
            except BaseException:
                connection.close()
                raise
            worker_started = True
            try:
                worker.join(_remaining(deadline))
            except OneBotError:
                cancelled.set()
                connection.close()
                raise
            if worker.is_alive():
                cancelled.set()
                connection.close()
                worker.join(0.05)
                raise OneBotError("onebot_timeout")
            error = outcome.get("error")
            if isinstance(error, OneBotError):
                raise error
            if isinstance(error, (socket.timeout, TimeoutError)):
                raise OneBotError("onebot_timeout") from None
            if isinstance(error, (OSError, http.client.HTTPException, ValueError, struct.error)):
                raise OneBotError("onebot_unavailable") from None
            if error is not None:
                raise OneBotError("onebot_unavailable") from None
            return outcome["data"]
        except BaseException:
            if not worker_started:
                if connection is not None:
                    connection.close()
                transport_slots.release()
            raise

    def _websocket(
        self,
        request_body: bytes,
        expected_data_type: type[dict | list],
        deadline: float,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        secure = self._parsed.scheme == "wss"
        host = self._parsed.hostname or ""
        port = self._parsed.port or (443 if secure else 80)
        path = self._parsed.path or "/"
        if self._parsed.path and not path.startswith("/"):
            path = "/" + path
        sock: socket.socket | None = None
        try:
            request = json.loads(request_body.decode("utf-8"))
            expected_echo = request["echo"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise OneBotError("onebot_request_invalid") from None
        try:
            remaining = _remaining(deadline)
            sock = socket.create_connection((host, port), timeout=remaining)
            sock.settimeout(_remaining(deadline))
            if secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = f"[{host}]" if ":" in host else host
            default_port = 443 if secure else 80
            if port != default_port:
                host_header = f"{host_header}:{port}"
            lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if self._token is not None:
                lines.append(f"Authorization: Bearer {self._token}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            reader = _SocketReader(sock, deadline)
            headers = reader.read_headers()
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if headers.get(":status") != "101" or headers.get("sec-websocket-accept") != accept or headers.get("upgrade", "").lower() != "websocket":
                raise OneBotError("onebot_websocket_handshake_failed")
            sock.settimeout(_remaining(deadline))
            _send_frame(sock, request_body, opcode=1)
            for _ in range(64):
                response = reader.receive_message()
                try:
                    value = json.loads(response.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                    raise OneBotError("onebot_response_invalid") from None
                if not isinstance(value, dict) or value.get("echo") != expected_echo:
                    continue
                return _parse_json(response, expected_data_type)
            raise OneBotError("onebot_event_flood")
        except OneBotError:
            raise
        except (OSError, TimeoutError, ssl.SSLError, ValueError, struct.error):
            raise OneBotError("onebot_unavailable") from None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise OneBotError("onebot_timeout")
    return value


def _http_response_socket(response: http.client.HTTPResponse) -> socket.socket | None:
    file_pointer = getattr(response, "fp", None)
    raw = getattr(file_pointer, "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


class _SocketReader:
    """Buffered socket reader preserving bytes coalesced with HTTP headers."""

    def __init__(self, sock: socket.socket, deadline: float):
        self.sock = sock
        self.deadline = deadline
        self.buffer = bytearray()

    def _receive(self, count: int) -> None:
        self.sock.settimeout(_remaining(self.deadline))
        try:
            chunk = self.sock.recv(count)
        except TimeoutError:
            raise OneBotError("onebot_timeout") from None
        if not chunk:
            raise OneBotError("onebot_websocket_closed")
        self.buffer.extend(chunk)

    def read_exact(self, size: int) -> bytes:
        if size < 0 or size > MAX_RESPONSE_BYTES:
            raise OneBotError("onebot_websocket_frame_invalid")
        while len(self.buffer) < size:
            self._receive(max(1, min(16 * 1024, size - len(self.buffer))))
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read_headers(self) -> dict[str, str]:
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) >= MAX_HANDSHAKE_BYTES:
                raise OneBotError("onebot_websocket_handshake_invalid")
            self._receive(min(2048, MAX_HANDSHAKE_BYTES - len(self.buffer)))
        split_at = self.buffer.index(b"\r\n\r\n")
        head = bytes(self.buffer[:split_at])
        del self.buffer[:split_at + 4]
        try:
            lines = head.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            raise OneBotError("onebot_websocket_handshake_invalid") from None
        if not lines or not lines[0].startswith("HTTP/1.1 "):
            raise OneBotError("onebot_websocket_handshake_invalid")
        status = lines[0].split(" ", 2)
        result = {":status": status[1] if len(status) > 1 else ""}
        for line in lines[1:]:
            if ":" not in line:
                raise OneBotError("onebot_websocket_handshake_invalid")
            name, value = line.split(":", 1)
            lower = name.strip().lower()
            if lower in result:
                raise OneBotError("onebot_websocket_handshake_invalid")
            result[lower] = value.strip()
        return result

    def receive_message(self) -> bytes:
        fragments = bytearray()
        active_opcode: int | None = None
        while True:
            first, second = self.read_exact(2)
            fin = bool(first & 0x80)
            if first & 0x70 or second & 0x80:
                raise OneBotError("onebot_websocket_frame_invalid")
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_exact(8))[0]
            if length > MAX_RESPONSE_BYTES or ((opcode & 0x08) and (not fin or length > 125)):
                raise OneBotError("onebot_websocket_frame_invalid")
            payload = self.read_exact(length)
            if opcode == 0x8:
                raise OneBotError("onebot_websocket_closed")
            if opcode == 0x9:
                _send_frame(self.sock, payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if active_opcode is not None:
                    raise OneBotError("onebot_websocket_frame_invalid")
                active_opcode = opcode
            elif opcode == 0x0:
                if active_opcode is None:
                    raise OneBotError("onebot_websocket_frame_invalid")
            else:
                raise OneBotError("onebot_websocket_frame_invalid")
            fragments.extend(payload)
            if len(fragments) > MAX_RESPONSE_BYTES:
                raise OneBotError("onebot_response_too_large")
            if fin:
                if active_opcode != 0x1:
                    raise OneBotError("onebot_response_invalid")
                return bytes(fragments)


def _send_frame(sock: socket.socket, payload: bytes, *, opcode: int) -> None:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise OneBotError("onebot_request_too_large")
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        head = bytes((0x80 | opcode, 0x80 | length))
    elif length <= 0xFFFF:
        head = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
    else:
        head = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    sock.sendall(head + mask + masked)


__all__ = ["OneBotClient", "OneBotError"]
