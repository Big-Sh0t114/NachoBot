from __future__ import annotations

import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path

from starlette.requests import Request

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from security import (  # noqa: E402
    TOKEN_ENV,
    TOKEN_HEADER,
    WS_AUTH_PREFIX,
    WebUISecurity,
    is_loopback_bind,
    validate_server_security,
    validate_webui_config_raw,
)


class DummyConfig:
    host = "127.0.0.1"
    port = 8088
    auth_token = ""
    allowed_origins: list[str] = []


def make_request(*, origin: str | None = None, token: str = "") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin:
        headers.append((b"origin", origin.encode()))
    if token:
        headers.append((TOKEN_HEADER.encode(), token.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/status",
            "raw_path": b"/api/status",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8088),
        }
    )


class FakeWebSocket:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self.closed: tuple[int, str] | None = None
        self.accepted: str | None = None

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = subprotocol


class WebUISecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_token = os.environ.pop(TOKEN_ENV, None)

    def tearDown(self) -> None:
        if self.previous_token is not None:
            os.environ[TOKEN_ENV] = self.previous_token

    def test_loopback_detection_and_remote_fail_closed(self) -> None:
        for host in ("localhost", "127.0.0.1", "127.42.1.5", "::1", "[::1]"):
            self.assertTrue(is_loopback_bind(host), host)
            validate_server_security(host, "")

        for host in ("0.0.0.0", "::", "192.168.1.10", "panel.example.com"):
            self.assertFalse(is_loopback_bind(host), host)
            with self.assertRaises(RuntimeError):
                validate_server_security(host, "")
            validate_server_security(host, "configured-token")

    def test_http_requires_token_when_configured(self) -> None:
        config = DummyConfig()
        os.environ[TOKEN_ENV] = "expected"
        security = WebUISecurity(config)

        self.assertEqual(security.authorize_http(make_request()).status_code, 401)
        self.assertIsNone(security.authorize_http(make_request(token="expected")))
        self.assertEqual(
            security.authorize_http(
                make_request(origin="https://hostile.example", token="expected")
            ).status_code,
            403,
        )

    def test_effective_config_rejects_remote_bind_without_token(self) -> None:
        raw = '[server]\nhost = "0.0.0.0"\n[security]\nauth_token = ""\n'
        with self.assertRaises(RuntimeError):
            validate_webui_config_raw(
                raw,
            )

        os.environ[TOKEN_ENV] = "configured"
        validate_webui_config_raw(raw)
        with self.assertRaisesRegex(RuntimeError, "不得写入"):
            validate_webui_config_raw(
                raw.replace('auth_token = ""', 'auth_token = "configured"'),
            )

    def test_loopback_config_still_rejects_legacy_persisted_token(self) -> None:
        raw = '[server]\nhost = "127.0.0.1"\n[security]\nauth_token = "legacy-secret"\n'
        with self.assertRaisesRegex(RuntimeError, "不得写入"):
            validate_webui_config_raw(raw)

    def test_hot_reload_cannot_remove_auth_from_existing_remote_socket(self) -> None:
        target_loopback_without_token = (
            '[server]\nhost = "127.0.0.1"\n[security]\nauth_token = ""\n'
        )
        with self.assertRaises(RuntimeError):
            validate_webui_config_raw(
                target_loopback_without_token,
                runtime_bind_host="0.0.0.0",
            )
        without_security_section = '[server]\nhost = "127.0.0.1"\n'
        with self.assertRaises(RuntimeError):
            validate_webui_config_raw(
                without_security_section,
                runtime_bind_host="0.0.0.0",
            )

    def test_runtime_origin_keeps_the_actual_bound_port_after_reload(self) -> None:
        config = DummyConfig()
        security = WebUISecurity(config)
        config.port = 9999

        self.assertIn("http://127.0.0.1:8088", security.allowed_origins())
        self.assertNotIn("http://127.0.0.1:9999", security.allowed_origins())

    def test_configured_exact_origin_is_allowed(self) -> None:
        config = DummyConfig()
        os.environ[TOKEN_ENV] = "expected"
        config.allowed_origins = ["https://panel.example.com"]
        security = WebUISecurity(config)
        self.assertIsNone(
            security.authorize_http(
                make_request(origin="https://panel.example.com", token="expected")
            )
        )
        rejection = security.authorize_http(
            make_request(origin="https://panel.example.com.evil", token="expected")
        )
        self.assertEqual(rejection.status_code, 403)
        self.assertEqual(
            security.authorize_http(
                make_request(origin="https://panel.example.com/path", token="expected")
            ).status_code,
            403,
        )

    def test_websocket_checks_origin_and_protocol_token_before_accept(self) -> None:
        config = DummyConfig()
        os.environ[TOKEN_ENV] = "expected"
        security = WebUISecurity(config)

        encoded = base64.urlsafe_b64encode(b"expected").decode().rstrip("=")
        accepted = FakeWebSocket(
            {
                "origin": "http://127.0.0.1:8088",
                "sec-websocket-protocol": f"nachobot, {WS_AUTH_PREFIX}{encoded}",
            }
        )
        self.assertTrue(asyncio.run(security.authorize_websocket(accepted)))
        self.assertEqual(accepted.accepted, "nachobot")

        rejected = FakeWebSocket(
            {
                "origin": "https://hostile.example",
                "sec-websocket-protocol": f"nachobot, {WS_AUTH_PREFIX}{encoded}",
            }
        )
        self.assertFalse(asyncio.run(security.authorize_websocket(rejected)))
        self.assertEqual(rejected.closed[0], 1008)


if __name__ == "__main__":
    unittest.main()
