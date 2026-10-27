"""SnowLuma saved-password reuse and sanitized login throttling regressions."""

import asyncio
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import server  # noqa: E402
from snowluma_credentials import SnowLumaPasswordStore  # noqa: E402
from snowluma_manager import SnowLumaAPIClient, SnowLumaApiError  # noqa: E402


class SnowLumaPasswordReuseTests(unittest.TestCase):
    def test_manual_password_is_saved_and_reused_by_fresh_requests(self):
        # An injectable protector avoids reading or replacing any live secret.
        with tempfile.TemporaryDirectory() as directory:
            store = SnowLumaPasswordStore(
                directory,
                protect=lambda data: b"test-envelope:" + data[::-1],
                unprotect=lambda data: data.removeprefix(b"test-envelope:")[::-1],
            )
            clients = []

            def make_client(_root):
                client = Mock()
                client.login.return_value = {"status": "ok"}
                client.get_agreements.return_value = {
                    "version": "test", "documents": [], "consentRequired": False,
                }
                client.list_processes.return_value = []
                clients.append(client)
                return client

            with patch.object(server, "SnowLumaPasswordStore", return_value=store), \
                 patch.object(server, "SnowLumaAPIClient", side_effect=make_client), \
                 patch.object(server, "_snowluma_check_agreement"):
                for password in ("synthetic-password", None, None):
                    self.assertEqual(server._snowluma_list_processes(password), [])
                self.assertEqual(len(clients), 3)
                for client in clients:
                    client.login.assert_called_once_with("synthetic-password")
                    client.close.assert_called_once()
                self.assertEqual(store.load(), "synthetic-password")

    def request_limited(self, message, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        error = HTTPError(
            "http://127.0.0.1:5099/api/login", 429, "Too Many Requests", headers,
            io.BytesIO(json.dumps({"message": message}).encode()),
        )
        client = SnowLumaAPIClient.__new__(SnowLumaAPIClient)
        client.port, client.timeout = 5099, 1
        client._token, client._cookie = "", ""
        client._opener = Mock()
        client._opener.open.side_effect = error
        with self.assertRaises(SnowLumaApiError) as caught:
            client.login("synthetic-password")
        return caught.exception

    def test_bundled_login_retry_message_maps_to_429(self):
        error = self.request_limited("登录尝试过多，请 897 秒后重试")
        self.assertEqual(error.code, "LOGIN_RATE_LIMITED")
        self.assertEqual(error.http_status, 429)
        response = server._snowluma_http_error(error)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.detail["retry_after"], 897)
        self.assertEqual(response.headers, {"Retry-After": "897"})
        self.assertIn("897 秒", response.detail["message"])

    def test_retry_header_is_bounded(self):
        error = self.request_limited("untrusted upstream body", "99999")
        self.assertEqual(error.details, {"retry_after": 86400})
        self.assertNotIn("untrusted", str(error))

    def test_untrusted_body_and_invalid_retry_are_never_exposed(self):
        for header in (None, "invalid", "9" * 100, "-1", "0"):
            with self.subTest(header=header):
                error = self.request_limited("synthetic-secret-body", header)
                response = server._snowluma_http_error(error)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(error.details, {})
                self.assertNotIn("synthetic-secret", str(response.detail))
                self.assertIsNone(response.headers)

    def test_process_endpoint_keeps_rate_limit_status(self):
        error = self.request_limited("登录尝试过多，请 20 秒后重试")
        with patch.object(server, "_snowluma_list_processes", side_effect=error):
            with self.assertRaises(server.HTTPException) as caught:
                asyncio.run(server.setup_snowluma_processes(server.SnowLumaProcessRequest()))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.detail["code"], "LOGIN_RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
