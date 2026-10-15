from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from service.rpc import OneBotClient, OneBotError


def _server_frame(value: object, *, opcode: int = 1) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8") if opcode == 1 else bytes(value)
    size = len(payload)
    if size < 126:
        return bytes((0x80 | opcode, size)) + payload
    if size <= 0xFFFF:
        return bytes((0x80 | opcode, 126)) + struct.pack("!H", size) + payload
    return bytes((0x80 | opcode, 127)) + struct.pack("!Q", size) + payload


def _read_client_frame(connection: socket.socket) -> dict[str, object]:
    first, second = connection.recv(2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", connection.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", connection.recv(8))[0]
    mask = connection.recv(4)
    payload = bytearray()
    while len(payload) < length:
        chunk = connection.recv(length - len(payload))
        if not chunk:
            raise RuntimeError("client closed")
        payload.extend(chunk)
    decoded = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    return json.loads(decoded.decode("utf-8"))


class WebSocketFixture:
    def __init__(self, handler):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error: BaseException | None = None

        def serve() -> None:
            try:
                connection, _ = self.listener.accept()
                with connection:
                    connection.settimeout(2)
                    raw = bytearray()
                    while b"\r\n\r\n" not in raw:
                        raw.extend(connection.recv(2048))
                    header = bytes(raw).split(b"\r\n\r\n", 1)[0].decode("ascii")
                    key = next(line.split(":", 1)[1].strip() for line in header.split("\r\n") if line.lower().startswith("sec-websocket-key:"))
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
                    handler(connection, accept)
            except BaseException as exc:  # report fixture failures to the test thread
                self.error = exc
            finally:
                self.listener.close()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            self.listener.close()
            raise AssertionError("fixture server did not stop")
        if self.error is not None:
            raise self.error


class ServiceRpcTests(unittest.TestCase):
    def test_websocket_preserves_coalesced_handshake_and_matches_echo_after_events(self) -> None:
        def handler(connection: socket.socket, accept: str) -> None:
            lifecycle = {"post_type": "meta_event", "meta_event_type": "lifecycle", "echo": None}
            headers = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            # Coalesce a legal handshake and unsolicited lifecycle frame; the
            # client must preserve bytes after the header terminator.
            connection.sendall(headers + _server_frame(lifecycle))
            request = _read_client_frame(connection)
            wrong = {"echo": "different", "status": "ok", "retcode": 0, "data": {"online": False}}
            response = {"echo": request["echo"], "status": "ok", "retcode": 0, "data": {"online": True}}
            connection.sendall(_server_frame(wrong) + _server_frame(response))

        server = WebSocketFixture(handler)
        try:
            value = OneBotClient(f"ws://127.0.0.1:{server.port}/api", timeout=1).call("get_status")
            self.assertEqual(value, {"online": True})
        finally:
            server.close()

    def test_websocket_partial_eof_fails_promptly(self) -> None:
        def handler(connection: socket.socket, _accept: str) -> None:
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n")

        server = WebSocketFixture(handler)
        result: list[str] = []

        def call() -> None:
            try:
                OneBotClient(f"ws://127.0.0.1:{server.port}/", timeout=1).call("get_status")
            except OneBotError as exc:
                result.append(exc.code)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(timeout=1.5)
        try:
            self.assertFalse(worker.is_alive(), "partial handshake EOF must not spin")
            self.assertEqual(result, ["onebot_websocket_closed"])
        finally:
            server.close()

    def test_websocket_unsolicited_frame_flood_is_bounded(self) -> None:
        def handler(connection: socket.socket, accept: str) -> None:
            headers = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            connection.sendall(headers)
            _read_client_frame(connection)
            frame = _server_frame({"echo": "unrelated", "status": "ok", "retcode": 0, "data": {}})
            connection.sendall(frame * 65)

        server = WebSocketFixture(handler)
        try:
            with self.assertRaisesRegex(OneBotError, "onebot_event_flood"):
                OneBotClient(f"ws://127.0.0.1:{server.port}/", timeout=1).call("get_status")
        finally:
            server.close()

    def test_websocket_uses_one_absolute_deadline_across_unsolicited_pings(self) -> None:
        started = threading.Event()

        def handler(connection: socket.socket, accept: str) -> None:
            headers = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            connection.sendall(headers)
            _read_client_frame(connection)
            started.set()
            for _ in range(20):
                try:
                    connection.sendall(_server_frame(b"x", opcode=0x9))
                except OSError:
                    return
                time.sleep(0.04)

        server = WebSocketFixture(handler)
        start = time.monotonic()
        try:
            with self.assertRaisesRegex(OneBotError, "onebot_timeout"):
                OneBotClient(f"ws://127.0.0.1:{server.port}/", timeout=0.25).call("get_status")
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            server.close()

    def test_http_transport_is_loopback_only_and_sends_optional_bearer(self) -> None:
        seen: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                seen["path"] = self.path
                seen["authorization"] = self.headers.get("Authorization", "")
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps({"status": "ok", "retcode": 0, "data": {"good": True}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_address[1]
            result = OneBotClient(f"http://127.0.0.1:{port}/api", "test-token").call("get_status")
            self.assertEqual(result, {"good": True})
            self.assertEqual(seen, {"path": "/api/get_status", "authorization": "Bearer test-token"})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

    def test_friend_list_is_a_dedicated_list_typed_http_action(self) -> None:
        seen: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                seen["path"] = self.path
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps({
                    "status": "ok", "retcode": 0, "data": [{"user_id": 10001}],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = OneBotClient(f"http://127.0.0.1:{server.server_address[1]}/api", timeout=1)
            with self.assertRaisesRegex(OneBotError, "onebot_action_unsupported"):
                client.call("get_friend_list")
            self.assertEqual(client.get_friend_list(), [{"user_id": 10001}])
            self.assertEqual(seen["path"], "/api/get_friend_list")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

    def test_http_action_specific_data_types_and_envelope_are_strict(self) -> None:
        def assert_rejected(body: bytes, action: str, expected: str) -> None:
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args: object) -> None:
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = OneBotClient(f"http://127.0.0.1:{server.server_address[1]}/", timeout=1)
                with self.assertRaisesRegex(OneBotError, expected):
                    if action == "get_friend_list":
                        client.get_friend_list()
                    else:
                        client.call(action)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=1)

        assert_rejected(
            b'{"status":"ok","retcode":0,"data":[{"user_id":10001}]}',
            "get_status", "onebot_response_invalid",
        )
        assert_rejected(
            b'{"status":"ok","retcode":0,"data":{"user_id":10001}}',
            "get_friend_list", "onebot_response_invalid",
        )
        assert_rejected(
            b'{"retcode":0,"data":{"online":true}}',
            "get_status", "onebot_action_failed",
        )

    def test_http_slow_body_is_interrupted_at_the_call_deadline(self) -> None:
        body = json.dumps({"status": "ok", "retcode": 0, "data": [{"user_id": 10001}]}).encode("utf-8")
        wrote_first = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body[:8])
                    self.wfile.flush()
                    wrote_first.set()
                    time.sleep(0.4)
                    self.wfile.write(body[8:])
                    self.wfile.flush()
                except OSError:
                    return

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(OneBotError, "onebot_timeout"):
                OneBotClient(f"http://127.0.0.1:{server.server_address[1]}/", timeout=1).get_friend_list(timeout=0.15)
            self.assertTrue(wrote_first.is_set())
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

    def test_http_transport_slot_is_released_when_exchange_thread_cannot_start(self) -> None:
        limiter = threading.BoundedSemaphore(1)
        client = OneBotClient("http://127.0.0.1:1/", timeout=0.2)
        client.set_http_transport_limiter(limiter)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(OneBotError, "onebot_unavailable"):
                client.call("get_status")
        self.assertTrue(limiter.acquire(blocking=False))
        limiter.release()

    def test_websocket_friend_list_accepts_only_list_data(self) -> None:
        def handler(connection: socket.socket, accept: str) -> None:
            headers = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            connection.sendall(headers)
            request = _read_client_frame(connection)
            response = {
                "echo": request["echo"], "status": "ok", "retcode": 0,
                "data": [{"user_id": 10001}],
            }
            connection.sendall(_server_frame(response))

        server = WebSocketFixture(handler)
        try:
            self.assertEqual(
                OneBotClient(f"ws://127.0.0.1:{server.port}/", timeout=1).get_friend_list(),
                [{"user_id": 10001}],
            )
        finally:
            server.close()

    def test_websocket_call_rejects_list_data_and_non_strict_envelope(self) -> None:
        for response_fields, expected in (
            ({"status": "ok", "retcode": 0, "data": [{"user_id": 10001}]}, "onebot_response_invalid"),
            ({"retcode": 0, "data": {"online": True}}, "onebot_action_failed"),
        ):
            def handler(connection: socket.socket, accept: str, fields=response_fields) -> None:
                headers = (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
                connection.sendall(headers)
                request = _read_client_frame(connection)
                connection.sendall(_server_frame({"echo": request["echo"], **fields}))

            server = WebSocketFixture(handler)
            try:
                with self.assertRaisesRegex(OneBotError, expected):
                    OneBotClient(f"ws://127.0.0.1:{server.port}/", timeout=1).call("get_status")
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
