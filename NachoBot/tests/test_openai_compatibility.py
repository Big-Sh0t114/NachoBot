from __future__ import annotations

import ast
import json
import logging
import sys
import types
import traceback
import unittest
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI


TEST_ROOT = Path(__file__).parents[1]
SRC_ROOT = TEST_ROOT / "src"
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))


def _stub_package(name: str, path: Path) -> types.ModuleType:
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    package.__package__ = name
    spec = ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    package.__spec__ = spec
    sys.modules[name] = package
    return package


def _load_source_modules_safely() -> None:
    """Load source classes without initializing live config, logging, or database state."""
    previous_src_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "src" or name.startswith("src.")
    }

    _stub_package("src", SRC_ROOT)
    _stub_package("src.common", SRC_ROOT / "common")
    _stub_package("src.config", SRC_ROOT / "config")
    _stub_package("src.llm_models", SRC_ROOT / "llm_models")
    _stub_package("src.llm_models.model_client", SRC_ROOT / "llm_models" / "model_client")
    _stub_package("src.llm_models.payload_content", SRC_ROOT / "llm_models" / "payload_content")
    _stub_package("src.manager", SRC_ROOT / "manager")

    logger_module = types.ModuleType("src.common.logger")

    def get_logger(name: str) -> logging.Logger:
        logger = logging.getLogger(f"openai-compat-test.{name}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return logger

    logger_module.get_logger = get_logger
    sys.modules[logger_module.__name__] = logger_module

    config_module = types.ModuleType("src.config.config")
    config_module.global_config = SimpleNamespace(debug=SimpleNamespace(enable_llm_cache_stats=False))
    config_module.model_config = SimpleNamespace(model_task_config=SimpleNamespace())
    sys.modules[config_module.__name__] = config_module

    utils_module = types.ModuleType("src.llm_models.utils")
    utils_module.compress_messages = lambda messages: messages
    utils_module.llm_usage_recorder = SimpleNamespace(record_usage_to_database=lambda **_kwargs: None)
    sys.modules[utils_module.__name__] = utils_module

    try:
        global ModelInfo, APIProvider
        global RespNotOkException, ModelAttemptFailed
        global APIResponse, OpenaiClient, LLMRequest, RequestType
        global Message, RoleType, ToolOption, _openai_module, _is_candidate_specific_compatibility_error
        from src.config.api_ada_configs import APIProvider, ModelInfo
        from src.llm_models.exceptions import ModelAttemptFailed, RespNotOkException
        from src.llm_models.model_client.base_client import APIResponse
        from src.llm_models.model_client import openai_client as _openai_module
        from src.llm_models.model_client.openai_client import OpenaiClient
        from src.llm_models.payload_content.message import Message, RoleType
        from src.llm_models.payload_content.tool_option import ToolOption
        from src.llm_models.utils_model import (
            LLMRequest,
            RequestType,
            _is_candidate_specific_compatibility_error,
        )
    finally:
        for name in tuple(sys.modules):
            if name == "src" or name.startswith("src."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_src_modules)


_load_source_modules_safely()


def _model(identifier: str, *, extra_params: dict | None = None, name: str | None = None) -> ModelInfo:
    return ModelInfo(
        model_identifier=identifier,
        name=name or identifier,
        api_provider="OpenAI-test",
        force_stream_mode=False,
        extra_params=extra_params or {},
    )


def _completion_response(model: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class OpenAICompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._sdk_clients: list[AsyncOpenAI] = []

    async def asyncTearDown(self) -> None:
        for client in self._sdk_clients:
            await client.close()

    def _client(self, handler, *, api_key: str = "test-provider-key-123456"):
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk_client = AsyncOpenAI(
            base_url="https://api.example.test/v1",
            api_key=api_key,
            http_client=http_client,
            max_retries=0,
        )
        self._sdk_clients.append(sdk_client)
        client = object.__new__(OpenaiClient)
        client.client = sdk_client
        client.api_provider = APIProvider(
            name="OpenAI-test",
            base_url="https://api.example.test/v1",
            api_key=api_key,
        )
        return client

    @staticmethod
    def _message() -> list[Message]:
        return [Message(RoleType.User, "compatibility probe")]

    def test_candidate_fallback_requires_supported_model_parameter_error(self) -> None:
        for error in (
            RespNotOkException(
                400,
                "unsupported parameter",
                error_code="unsupported_parameter",
                param="max_tokens",
            ),
            RespNotOkException(
                400,
                "unsupported parameter",
                error_code="unsupported_parameter",
                param="max_completion_tokens",
            ),
            RespNotOkException(
                400,
                "unsupported value",
                error_code="unsupported_value",
                param="temperature",
            ),
            RespNotOkException(
                400,
                "unknown provider model id",
                error_code="model_not_found",
                param="model",
            ),
        ):
            self.assertTrue(_is_candidate_specific_compatibility_error(error))

        for error in (
            RespNotOkException(400, "unsupported tool", error_code="unsupported_parameter", param="tools"),
            RespNotOkException(400, "model not found", error_code="model_not_found", param="messages"),
            RespNotOkException(400, "bad request", error_code="invalid_request_error", param="messages"),
            RespNotOkException(400, "unknown bad request"),
            RespNotOkException(500, "server error", error_code="unsupported_parameter", param="max_tokens"),
        ):
            self.assertFalse(_is_candidate_specific_compatibility_error(error))

    def test_planner_raw_diagnostics_are_debug_gated(self) -> None:
        planner_path = SRC_ROOT / "chat" / "brain_chat" / "brain_planner.py"
        planner_module = ast.parse(planner_path.read_text(encoding="utf-8"))
        planner_function = next(
            node
            for node in ast.walk(planner_module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_execute_main_planner"
        )
        show_prompt_guard = next(
            node
            for node in ast.walk(planner_function)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "global_config.debug.show_prompt"
        )

        content_names = {"prompt", "llm_content", "reasoning_content"}
        content_log_calls = []
        for node in ast.walk(planner_function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
                continue
            has_content = any(
                isinstance(child, ast.Name) and child.id in content_names
                for arg in node.args
                for child in ast.walk(arg)
            )
            if node.func.attr == "info" and has_content:
                content_log_calls.append(node)
                continue
            if has_content and node.lineno <= show_prompt_guard.end_lineno:
                content_log_calls.append(node)
        self.assertTrue(content_log_calls)
        self.assertFalse(any(call.func.attr == "info" for call in content_log_calls))
        for call in content_log_calls:
            self.assertGreater(call.lineno, show_prompt_guard.lineno)
            self.assertLessEqual(call.end_lineno, show_prompt_guard.end_lineno)

    async def test_legacy_deepseek_and_qwen_extra_parameters_remain_in_body(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=_completion_response(body["model"]))

        client = self._client(handler)
        for identifier, extra in (
            ("deepseek-chat", {"enable_thinking": False, "custom_mode": "deepseek"}),
            ("Qwen/Qwen3-8B", {"enable_thinking": False, "custom_mode": "qwen"}),
        ):
            await client.get_response(
                _model(identifier),
                self._message(),
                max_tokens=512,
                temperature=0.3,
                extra_params=extra,
            )
        self.assertEqual(len(captured), 2)
        for body in captured:
            self.assertEqual(body["max_tokens"], 512)
            self.assertEqual(body["temperature"], 0.3)
            self.assertFalse(body["enable_thinking"])
        self.assertEqual(captured[0]["custom_mode"], "deepseek")
        self.assertEqual(captured[1]["custom_mode"], "qwen")

    async def test_nested_api_error_is_useful_and_redacts_credentials(self) -> None:
        api_key = "configured-provider-secret-123456"
        message = (
            "Unsupported parameter; configured=" + api_key
            + '; json={"api_key":"non-sk-other-secret"}'
            + " Authorization: Bearer bearer-secret-value"
            + " sk-proj-very-secret-token"
            + " https://user:password-value@api.example.test/path?api_key=query-secret-value"
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "param": "max_tokens",
                        "code": "unsupported_parameter",
                    }
                },
            )

        client = self._client(handler, api_key=api_key)
        with self.assertRaises(RespNotOkException) as caught:
            await client.get_response(_model("gpt-5.6-terra"), self._message())

        rendered = str(caught.exception)
        rendered_traceback = "".join(traceback.format_exception(caught.exception))
        self.assertIn("HTTP 400", rendered)
        self.assertIn("Unsupported parameter", rendered)
        self.assertIn("unsupported_parameter", rendered)
        self.assertIn("param=max_tokens", rendered)
        self.assertIn("invalid_request_error", rendered)
        for secret in (
            api_key,
            "non-sk-other-secret",
            "bearer-secret-value",
            "sk-proj-very-secret-token",
            "password-value",
            "query-secret-value",
        ):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, rendered_traceback)

    async def test_failure_log_contains_safe_metadata_and_request_id_only(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter",
                        "type": "invalid_request_error",
                        "param": "max_tokens",
                        "code": "unsupported_parameter",
                    }
                },
                headers={"x-request-id": "req_safe_123"},
            )

        client = self._client(handler)
        client.api_provider.base_url = "https://url-user:url-password@api.example.test/v1?api_key=query-secret"
        private_prompt = "private prompt that must not enter failure diagnostics"
        private_tool_name = "private_tool_name"
        private_schema_value = "private-schema-value"
        private_extra_value = "private-extra-value"
        model = _model("gpt-5.6-terra")
        tool = ToolOption(
            private_tool_name,
            "private description",
            input_schema={
                "type": "object",
                "properties": {"private": {"type": "string", "description": private_schema_value}},
            },
        )
        with patch.object(_openai_module.logger, "warning") as warning:
            with self.assertRaises(RespNotOkException) as caught:
                await client.get_response(
                    model,
                    [Message(RoleType.User, private_prompt)],
                    tool_options=[tool],
                    max_tokens=128,
                    temperature=0.1,
                    extra_params={"private_option": private_extra_value},
                )

        diagnostics = caught.exception.request_diagnostics
        self.assertIsNotNone(diagnostics)
        self.assertEqual(diagnostics["endpoint"], "https://api.example.test/v1/chat/completions")
        self.assertEqual(diagnostics["model"], "gpt-5.6-terra")
        self.assertTrue(diagnostics["has_tools"])
        self.assertEqual(diagnostics["tool_count"], 1)
        self.assertEqual(diagnostics["messages"], [{"role": "user", "content_length": len(private_prompt)}])
        self.assertEqual(diagnostics["parameters"]["max_tokens"], {"type": "int", "value": 128})
        self.assertEqual(diagnostics["parameters"]["private_option"], {"type": "str"})
        self.assertEqual(caught.exception.request_id, "req_safe_123")
        self.assertIn("request_id=req_safe_123", str(caught.exception))

        warning.assert_called_once()
        log_payload = warning.call_args.args[1]
        for private_value in (
            private_prompt,
            private_tool_name,
            private_schema_value,
            private_extra_value,
            "url-user",
            "url-password",
            "query-secret",
        ):
            self.assertNotIn(private_value, log_payload)
        self.assertIn("content_length", log_payload)
        self.assertIn("has_tools", log_payload)

    async def test_flat_and_non_json_api_errors_keep_useful_details(self) -> None:
        for response, expected in (
            (
                httpx.Response(
                    400,
                    json={
                        "message": "Temperature is unsupported",
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_value",
                    },
                ),
                ("Temperature is unsupported", "unsupported_value", "temperature"),
            ),
            (
                httpx.Response(400, text="upstream rejected this request"),
                ("upstream rejected this request", "HTTP 400"),
            ),
        ):
            def handler(_request: httpx.Request, _response=response) -> httpx.Response:
                return _response

            client = self._client(handler)
            with self.assertRaises(RespNotOkException) as caught:
                await client.get_response(_model("gpt-5.6-terra"), self._message())
            rendered = str(caught.exception)
            for expected_part in expected:
                self.assertIn(expected_part, rendered)

    async def test_echoed_prompt_and_structured_error_body_are_not_exposed(self) -> None:
        prompt = "private prompt echoed by upstream"
        authorization_value = "Bearer upstream-secret-value"

        def echo_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": f"Rejected input: {prompt}", "code": "invalid_request_error"}},
            )

        client = self._client(echo_handler)
        with self.assertRaises(RespNotOkException) as echoed:
            await client.get_response(_model("legacy-model"), [Message(RoleType.User, prompt)])
        self.assertNotIn(prompt, str(echoed.exception))

        def structured_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "request_prompt": prompt,
                        "authorization": authorization_value,
                        "request_headers": {"Authorization": authorization_value},
                    }
                },
            )

        client = self._client(structured_handler)
        with self.assertRaises(RespNotOkException) as structured:
            await client.get_response(_model("legacy-model"), [Message(RoleType.User, prompt)])
        rendered = "".join(traceback.format_exception(structured.exception))
        self.assertIn("without an error message", str(structured.exception))
        self.assertNotIn(prompt, rendered)
        self.assertNotIn(authorization_value, rendered)

    async def test_400_falls_back_to_remaining_candidates_without_retrying_same_model(self) -> None:
        models = [
            _model("provider-a", name="candidate-a"),
            _model("provider-b", name="candidate-b"),
        ]
        providers = {model.name: APIProvider(name=model.name, base_url="https://api.test", api_key="test") for model in models}
        request = object.__new__(LLMRequest)
        request.model_for_task = SimpleNamespace(model_list=[model.name for model in models])
        request.model_usage = {model.name: (0, 0, 0, 0.0) for model in models}
        request.request_type = "test"
        request._model_config = SimpleNamespace(model_task_config=SimpleNamespace())
        def select_model(*, exclude_models=None, eligible_models=None):
            del eligible_models
            selected = next(model for model in models if model.name not in (exclude_models or set()))
            return selected, providers[selected.name], object()

        request._select_model = select_model
        attempted: list[str] = []

        async def candidate_executor(model_info, _provider, default_attempt):
            del default_attempt
            attempted.append(model_info.name)
            if model_info.name == "candidate-a":
                raise ModelAttemptFailed(
                    "candidate returned HTTP 400",
                    RespNotOkException(
                        400,
                        "unsupported_parameter max_tokens",
                        error_code="unsupported_parameter",
                        param="max_tokens",
                    ),
                )
            return APIResponse(content="ok")

        response, selected = await request._execute_request(
            request_type=RequestType.RESPONSE,
            candidate_executor=candidate_executor,
        )
        self.assertEqual(response.content, "ok")
        self.assertEqual(selected.name, "candidate-b")
        self.assertEqual(attempted, ["candidate-a", "candidate-b"])
        self.assertEqual(len(attempted), len(set(attempted)))

    async def test_provider_model_not_found_falls_back_without_identifier_rewriting(self) -> None:
        models = [
            _model("gpt-5.6-terra/openai", name="gpt-5.6-terra/openai"),
            _model("gpt-5.6-terra", name="gpt-5.6-terra"),
        ]
        providers = {model.name: APIProvider(name=model.name, base_url="https://api.test", api_key="test") for model in models}
        request = object.__new__(LLMRequest)
        request.model_for_task = SimpleNamespace(model_list=[model.name for model in models])
        request.model_usage = {model.name: (0, 0, 0, 0.0) for model in models}
        request.request_type = "test"
        request._model_config = SimpleNamespace(model_task_config=SimpleNamespace())

        def select_model(*, exclude_models=None, eligible_models=None):
            del eligible_models
            selected = next(model for model in models if model.name not in (exclude_models or set()))
            return selected, providers[selected.name], object()

        request._select_model = select_model
        attempted_identifiers: list[str] = []

        async def candidate_executor(model_info, _provider, default_attempt):
            del default_attempt
            attempted_identifiers.append(model_info.model_identifier)
            if model_info.model_identifier.endswith("/openai"):
                raise ModelAttemptFailed(
                    "provider rejected model identifier",
                    RespNotOkException(
                        400,
                        "unknown provider for model gpt-5.6-terra/openai",
                        error_code="model_not_found",
                        param="model",
                        error_type="invalid_request_error",
                    ),
                )
            return APIResponse(content="REMOTE_TERRA_OK")

        response, chosen = await request._execute_request(
            request_type=RequestType.RESPONSE,
            candidate_executor=candidate_executor,
        )
        self.assertEqual(attempted_identifiers, ["gpt-5.6-terra/openai", "gpt-5.6-terra"])
        self.assertEqual(chosen.name, "gpt-5.6-terra")
        self.assertEqual(response.content, "REMOTE_TERRA_OK")

    async def test_all_400_candidates_raise_last_useful_error_without_same_candidate_retry(self) -> None:
        models = [
            _model("provider-a", name="candidate-a"),
            _model("provider-b", name="candidate-b"),
        ]
        providers = {model.name: APIProvider(name=model.name, base_url="https://api.test", api_key="test") for model in models}
        request = object.__new__(LLMRequest)
        request.model_for_task = SimpleNamespace(model_list=[model.name for model in models])
        request.model_usage = {model.name: (0, 0, 0, 0.0) for model in models}
        request.request_type = "test"
        request._model_config = SimpleNamespace(model_task_config=SimpleNamespace())

        def select_model(*, exclude_models=None, eligible_models=None):
            del eligible_models
            selected = next(model for model in models if model.name not in (exclude_models or set()))
            return selected, providers[selected.name], object()

        request._select_model = select_model
        attempted: list[str] = []

        async def candidate_executor(model_info, _provider, default_attempt):
            del default_attempt
            attempted.append(model_info.name)
            raise ModelAttemptFailed(
                "candidate returned HTTP 400",
                RespNotOkException(
                    400,
                    f"unsupported parameter for {model_info.name}",
                    error_code="unsupported_parameter",
                    param="max_tokens",
                ),
            )

        with self.assertRaises(RespNotOkException) as caught:
            await request._execute_request(
                request_type=RequestType.RESPONSE,
                candidate_executor=candidate_executor,
            )
        self.assertEqual(attempted, ["candidate-a", "candidate-b"])
        self.assertIn("candidate-b", str(caught.exception))

    async def test_request_wide_or_unknown_400_aborts_without_trying_another_model(self) -> None:
        models = [
            _model("provider-a", name="candidate-a"),
            _model("provider-b", name="candidate-b"),
        ]
        providers = {model.name: APIProvider(name=model.name, base_url="https://api.test", api_key="test") for model in models}
        request = object.__new__(LLMRequest)
        request.model_for_task = SimpleNamespace(model_list=[model.name for model in models])
        request.model_usage = {model.name: (0, 0, 0, 0.0) for model in models}
        request.request_type = "test"
        request._model_config = SimpleNamespace(model_task_config=SimpleNamespace())

        def select_model(*, exclude_models=None, eligible_models=None):
            del eligible_models
            selected = next(model for model in models if model.name not in (exclude_models or set()))
            return selected, providers[selected.name], object()

        request._select_model = select_model

        for error in (
            RespNotOkException(400, "invalid message", error_code="invalid_request_error", param="messages"),
            RespNotOkException(400, "unknown bad request"),
            RespNotOkException(
                400,
                "unsupported tool schema field",
                error_code="unsupported_parameter",
                param="tools",
            ),
        ):
            attempted: list[str] = []

            async def candidate_executor(model_info, _provider, default_attempt, _error=error):
                del default_attempt
                attempted.append(model_info.name)
                raise ModelAttemptFailed("candidate returned HTTP 400", _error)

            with self.subTest(error=str(error)), self.assertRaises(RespNotOkException):
                await request._execute_request(
                    request_type=RequestType.RESPONSE,
                    candidate_executor=candidate_executor,
                )
            self.assertEqual(attempted, ["candidate-a"])


if __name__ == "__main__":
    unittest.main()
