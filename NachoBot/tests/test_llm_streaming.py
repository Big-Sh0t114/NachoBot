from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from src.config.config import model_config
from src.llm_models.model_client.base_client import APIResponse, UsageRecord, client_registry
from src.llm_models.model_client.openai_client import _default_stream_response_handler
from src.llm_models.utils_model import (
    LLMRequest,
    stream_delta_is_meaningful,
)


def _openai_chunk(*, content=None, reasoning_content=None, tool_calls=None, usage=None, choices=True):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=None,
        tool_calls=tool_calls or [],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)] if choices else [],
        usage=usage,
    )


class StreamDeltaMeaningfulnessTests(unittest.TestCase):
    def test_openai_like_empty_usage_and_bookkeeping_frames_are_not_meaningful(self):
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=0, total_tokens=1)
        empty = _openai_chunk()
        usage_only = _openai_chunk(usage=usage, choices=False)
        bookkeeping = _openai_chunk(
            tool_calls=[SimpleNamespace(index=0, id="", function=SimpleNamespace(name="", arguments=""))]
        )

        self.assertFalse(stream_delta_is_meaningful(empty))
        self.assertFalse(stream_delta_is_meaningful(usage_only))
        self.assertFalse(stream_delta_is_meaningful(bookkeeping))

    def test_openai_like_content_reasoning_and_tool_deltas_are_meaningful(self):
        tool_name = _openai_chunk(
            tool_calls=[SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(name="write_text", arguments=""))]
        )
        tool_arguments = _openai_chunk(
            tool_calls=[SimpleNamespace(index=0, id="", function=SimpleNamespace(name="", arguments='{"path":'))]
        )

        self.assertTrue(stream_delta_is_meaningful(_openai_chunk(content="hello")))
        self.assertTrue(stream_delta_is_meaningful(_openai_chunk(reasoning_content="thinking")))
        self.assertTrue(stream_delta_is_meaningful(tool_name))
        self.assertTrue(stream_delta_is_meaningful(tool_arguments))

    def test_gemini_like_text_thought_and_function_call_deltas_are_meaningful(self):
        usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=0, total_token_count=1)
        empty = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="", thought=False)]))],
            usage_metadata=None,
        )
        usage_only = SimpleNamespace(candidates=[], usage_metadata=usage)
        text = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="hello", thought=False)]))],
            usage_metadata=None,
        )
        thought = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="think", thought=True)]))],
            usage_metadata=None,
        )
        function_call = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                thought=False,
                                function_call=SimpleNamespace(name="write_text", args={"path": "out.txt"}),
                            )
                        ]
                    )
                )
            ],
            usage_metadata=None,
        )
        empty_function_call = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text=None, function_call={})]
                    )
                )
            ],
            usage_metadata=None,
        )
        empty_nested_function_call = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                function_call={"name": "", "args": {"metadata": {}}},
                            )
                        ]
                    )
                )
            ],
            usage_metadata=None,
        )
        empty_function_call_object = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                function_call=SimpleNamespace(name="", args={}),
                            )
                        ]
                    )
                )
            ],
            usage_metadata=None,
        )

        self.assertFalse(stream_delta_is_meaningful(empty))
        self.assertFalse(stream_delta_is_meaningful(usage_only))
        self.assertFalse(stream_delta_is_meaningful(empty_function_call))
        self.assertFalse(stream_delta_is_meaningful(empty_nested_function_call))
        self.assertFalse(stream_delta_is_meaningful(empty_function_call_object))
        self.assertTrue(stream_delta_is_meaningful(text))
        self.assertTrue(stream_delta_is_meaningful(thought))
        self.assertTrue(stream_delta_is_meaningful(function_call))


class LLMRequestStreamingEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_entry_point_forces_provider_handler_and_preserves_output_and_usage(self):
        usage = UsageRecord(
            model_name="fake-model",
            provider_name="fake-provider",
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        )
        tool_delta = _openai_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(
                        name="write_text",
                        arguments='{"path":"out.txt","content":"done"}',
                    ),
                )
            ]
        )
        events = [
            _openai_chunk(content="hello"),
            tool_delta,
            _openai_chunk(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7), choices=False),
        ]

        class FakeStream:
            def __init__(self, values):
                self._values = iter(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._values)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        class FakeClient:
            def __init__(self):
                self.force_stream_values = []
                self.handlers = []
                self.observed_stream = None

            async def get_response(
                self,
                *,
                model_info,
                stream_response_handler,
                interrupt_flag,
                **_kwargs,
            ):
                self.force_stream_values.append(model_info.force_stream_mode)
                self.handlers.append(stream_response_handler)
                self.observed_stream = FakeStream(events)
                response, _usage = await stream_response_handler(self.observed_stream, interrupt_flag)
                response.usage = usage
                return response

        fake_client = FakeClient()
        model_info = ModelInfo("fake-id", "fake-model", "fake-provider")
        provider = APIProvider(
            name="fake-provider",
            base_url="http://fake-provider",
            api_key="test-key",
            max_retry=1,
            retry_interval=0,
        )
        request = LLMRequest(TaskConfig(model_list=["fake-model"], max_tokens=77, temperature=0.2), "file_edit")
        observed = []

        with (
            patch.object(model_config, "get_model_info", return_value=model_info),
            patch.object(model_config, "get_provider", return_value=provider),
            patch.object(client_registry, "get_client_class_instance", return_value=fake_client),
            patch(
                "src.llm_models.utils_model._provider_stream_handler",
                return_value=_default_stream_response_handler,
            ),
            patch("src.llm_models.utils_model.llm_usage_recorder.record_usage_to_database") as record_usage,
        ):
            content, detail = await request.generate_response_stream_async(
                "create the file",
                tools=[
                    {
                        "name": "write_text",
                        "description": "write text",
                        "input_schema": {"type": "object"},
                    }
                ],
                on_delta=observed.append,
            )

        self.assertEqual(content, "hello")
        self.assertEqual(detail[1], "fake-model")
        self.assertEqual(len(detail[2] or []), 1)
        self.assertEqual(detail[2][0].func_name, "write_text")
        self.assertEqual(detail[2][0].args, {"path": "out.txt", "content": "done"})
        self.assertEqual(fake_client.force_stream_values, [True])
        self.assertIsNotNone(fake_client.handlers[0])
        self.assertEqual(len(observed), 3)
        self.assertEqual([stream_delta_is_meaningful(item) for item in observed], [True, True, False])
        record_usage.assert_called_once()
        self.assertIs(record_usage.call_args.kwargs["model_usage"], usage)

    async def test_non_stream_entry_point_remains_non_stream(self):
        usage = UsageRecord("fake-model", "fake-provider", 1, 1, 2)

        class FakeClient:
            def __init__(self):
                self.force_stream_values = []

            async def get_response(self, *, model_info, stream_response_handler, **_kwargs):
                self.force_stream_values.append(model_info.force_stream_mode)
                self.assertIsNone(stream_response_handler)
                return APIResponse(content="normal", tool_calls=[], usage=usage)

            def assertIsNone(self, value):
                if value is not None:
                    raise AssertionError("ordinary request unexpectedly received stream handler")

        fake_client = FakeClient()
        model_info = ModelInfo("fake-id", "fake-model", "fake-provider")
        provider = APIProvider(
            name="fake-provider",
            base_url="http://fake-provider",
            api_key="test-key",
            max_retry=1,
            retry_interval=0,
        )
        request = LLMRequest(TaskConfig(model_list=["fake-model"]), "file_edit")
        with (
            patch.object(model_config, "get_model_info", return_value=model_info),
            patch.object(model_config, "get_provider", return_value=provider),
            patch.object(client_registry, "get_client_class_instance", return_value=fake_client),
            patch("src.llm_models.utils_model.llm_usage_recorder.record_usage_to_database") as record_usage,
        ):
            content, detail = await request.generate_response_async("ordinary response")

        self.assertEqual(content, "normal")
        self.assertEqual(detail[1], "fake-model")
        self.assertEqual(fake_client.force_stream_values, [False])
        record_usage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
