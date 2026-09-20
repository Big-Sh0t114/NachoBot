import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TTSPipeline  # noqa: E402


class PlatformApiRelayTests(unittest.TestCase):
    def test_pipeline_registers_platform_api_relay_handlers(self) -> None:
        config = SimpleNamespace(
            config_path="unused",
            config_data={"debug": {"logging_level": "INFO"}},
            server=SimpleNamespace(host="127.0.0.1", port=8070),
            routes={"qq": "ws://127.0.0.1:8000/ws"},
        )
        server = Mock()
        router = Mock()

        with (
            patch("main.Config", return_value=config),
            patch("main.MessageServer", return_value=server),
            patch("main.Router", return_value=router),
        ):
            TTSPipeline("unused", no_local_models=True)

        self.assertEqual(
            [call.args[0] for call in router.register_custom_message_handler.call_args_list],
            ["platform_api_request"],
        )
        self.assertEqual(
            [call.args[0] for call in server.register_custom_message_handler.call_args_list],
            ["platform_api_response", "platform_status", "message_id_echo"],
        )

    def test_platform_api_request_is_forwarded_from_core_to_adapter(self) -> None:
        async def scenario() -> None:
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.server = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
            pipeline.router = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
            request = {
                "platform": "qq",
                "message_type_name": "platform_api_request",
                "content": {
                    "version": 1,
                    "request_id": "req-1",
                    "operation": "get_platform_cookies",
                    "platform": "qq",
                },
                "is_custom_message": True,
            }

            await pipeline._core_custom_handler("platform_api_request")(request)

            pipeline.server.send_custom_message.assert_awaited_once_with(
                "qq",
                "platform_api_request",
                request["content"],
            )
            pipeline.router.send_custom_message.assert_not_awaited()

        asyncio.run(scenario())

    def test_platform_api_response_is_forwarded_from_adapter_to_core(self) -> None:
        async def scenario() -> None:
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.server = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
            pipeline.router = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
            response = {
                "platform": "qq",
                "message_type_name": "platform_api_response",
                "content": {
                    "version": 1,
                    "request_id": "req-1",
                    "operation": "get_platform_cookies",
                    "platform": "qq",
                    "status": "ok",
                    "data": {"cookies": "redacted"},
                },
                "is_custom_message": True,
            }

            await pipeline._platform_custom_handler("platform_api_response")(response)

            pipeline.router.send_custom_message.assert_awaited_once_with(
                "qq",
                "platform_api_response",
                response["content"],
            )
            pipeline.server.send_custom_message.assert_not_awaited()

        asyncio.run(scenario())

    def test_malformed_custom_message_is_not_forwarded(self) -> None:
        async def scenario() -> None:
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.server = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))
            pipeline.router = SimpleNamespace(send_custom_message=AsyncMock(return_value=True))

            await pipeline._core_custom_handler("platform_api_request")(
                {"platform": "qq", "content": "invalid"}
            )

            pipeline.server.send_custom_message.assert_not_awaited()
            pipeline.router.send_custom_message.assert_not_awaited()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
