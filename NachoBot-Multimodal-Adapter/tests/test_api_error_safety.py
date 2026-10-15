from __future__ import annotations

import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal import api_server  # noqa: E402


class ApiErrorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_perception_masks_exception_and_escapes_untrusted_log_fields(self):
        class FailingRuntime:
            async def perceive(self, *_args, **_kwargs):
                raise RuntimeError("private model path\nforged log line")

        body = api_server.PerceptionBody(operation="image.describe.v1\nforged", data="YQ==")
        with patch.object(api_server, "_runtime", FailingRuntime()):
            with self.assertLogs(api_server.logger.name, level=logging.ERROR) as captured:
                response = await api_server.perception(body)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["error"]["message"], "local perception operation failed")
        self.assertNotIn("private model path", response.body.decode("utf-8"))
        self.assertEqual(len(captured.records), 1)
        self.assertIn(r"image.describe.v1\nforged", captured.output[0])
        self.assertIn(r"private model path\nforged log line", captured.output[0])
        self.assertNotIn("\nforged", captured.output[0])

    async def test_invalid_request_masks_raw_value_error_details(self):
        class FailingRuntime:
            async def perceive(self, *_args, **_kwargs):
                raise ValueError("private file C:\\secret\\model.bin")

        body = api_server.PerceptionBody(operation="image.describe.v1", data="YQ==")
        with patch.object(api_server, "_runtime", FailingRuntime()):
            response = await api_server.perception(body)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "invalid perception request")
        self.assertNotIn("model.bin", response.body.decode("utf-8"))
