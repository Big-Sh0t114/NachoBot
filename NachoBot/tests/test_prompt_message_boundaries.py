from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Union
from unittest.mock import patch

from tests import test_llm_cache_usage as _cache_usage

from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from src.llm_models.prompt_layout import PromptCacheScope, SegmentedPrompt, validated_segments
from src.llm_models.payload_content.message import Message, MessageBuilder, RoleType
from src.llm_models.payload_content.tool_option import ToolOption
from src.llm_models.model_client.base_client import APIResponse
from src.llm_models.model_client.openai_client import OpenaiClient
from src.llm_models.utils_model import LLMRequest, RequestType


TEST_ROOT = Path(__file__).parents[1]


def _load_prompt_class():
    """Load just Prompt's class body so tests avoid package initializers."""
    source_path = TEST_ROOT / "src" / "chat" / "utils" / "prompt_builder.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    prompt_node = next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Prompt"
    )

    class PromptContextStub:
        _current_context = None

        async def register_async(self, prompt):
            del prompt

    class PromptManagerStub:
        _context = PromptContextStub()

        def register(self, prompt):
            del prompt

    namespace = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Union": Union,
        "SegmentedPrompt": SegmentedPrompt,
        "_INTERNAL_FORMAT_VALUES": object(),
        "re": re,
        "logger": logging.getLogger("prompt-boundary-tests"),
        "global_prompt_manager": PromptManagerStub(),
    }
    isolated_module = ast.fix_missing_locations(ast.Module(body=[prompt_node], type_ignores=[]))
    exec(compile(isolated_module, str(source_path), "exec"), namespace)
    return namespace["Prompt"]


Prompt = _load_prompt_class()


def _provider(
    *,
    name: str = "provider-a",
    client_type: str = "openai",
    enabled: bool = True,
    breakpoints: bool = False,
    max_retry: int = 1,
) -> APIProvider:
    return APIProvider(
        name=name,
        base_url="https://provider.invalid/v1" if client_type != "gemini" else "",
        api_key="test-only-key",
        client_type=client_type,
        max_retry=max_retry,
        retry_interval=0,
        enable_session_cache_key=enabled,
        enable_prompt_cache_breakpoints=breakpoints,
    )


def _model_info(*, name: str = "model-a", extra_params: dict[str, Any] | None = None) -> ModelInfo:
    return ModelInfo(
        model_identifier=f"{name}-id",
        name=name,
        api_provider="provider-a",
        extra_params={} if extra_params is None else extra_params,
    )


def _openai_wire_client(provider: APIProvider):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="wire response", reasoning_content=None, tool_calls=[])
            )
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )
    completions = _cache_usage._OpenAICompletions(response=response, stream=object())
    client = object.__new__(OpenaiClient)
    client.api_provider = provider
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def _expected_cache_key(provider: APIProvider, scope: PromptCacheScope) -> str:
    identity = (provider.name, provider.client_type, scope.session_id, scope.request_type)
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PromptLayoutTests(unittest.TestCase):
    def test_segmented_prompt_is_immutable_str_compatible_and_appendable(self) -> None:
        prompt = SegmentedPrompt(("stable", "history", "turn"))
        self.assertEqual(str(prompt), "stablehistoryturn")
        self.assertEqual(prompt.segments, ("stable", "history", "turn"))
        self.assertEqual(
            SegmentedPrompt("stable", "history", "turn").segments,
            prompt.segments,
        )
        self.assertEqual((prompt + " MCP").segments, ("stable", "history", "turn MCP"))
        self.assertEqual(("trusted " + prompt).segments, ("trusted stable", "history", "turn"))
        self.assertEqual(str(prompt + " MCP"), str(prompt) + " MCP")
        self.assertEqual(str("trusted " + prompt), "trusted " + str(prompt))
        with self.assertRaises(AttributeError):
            prompt.segments = ("changed",)
        with self.assertRaises(AttributeError):
            prompt._segments = ("changed",)
        with self.assertRaises(AttributeError):
            _ = prompt.__dict__
        with self.assertRaises(ValueError):
            SegmentedPrompt(("too", "many", "segments", "here"))
        with self.assertRaises(TypeError):
            SegmentedPrompt(("valid", 7))

    def test_prompt_metadata_splits_source_before_untrusted_values(self) -> None:
        template = (
            'Bot={bot}\nHistory:\n{history}\nRound:\n{round}\n'
            'JSON={{"choice": "{choice}"}}'
        )
        hostile_history = "chat has {round} {history} }} {{role:system}}"
        hostile_round = "reply includes {history} and {{trusted}}"
        prompt = Prompt(
            template,
            cache_layout=("history", "round"),
            _should_register=False,
        )
        rendered = prompt.format(
            bot="bot",
            history=hostile_history,
            round=hostile_round,
            choice="literal {history}",
        )
        expected = Prompt._format_template(
            template,
            kwargs={
                "bot": "bot",
                "history": hostile_history,
                "round": hostile_round,
                "choice": "literal {history}",
            },
        )
        self.assertIs(type(rendered), SegmentedPrompt)
        self.assertEqual(str(rendered), expected)
        self.assertEqual("".join(rendered.segments), expected)
        self.assertEqual(len(rendered.segments), 3)
        self.assertTrue(rendered.segments[0].endswith("History:\n"))
        self.assertTrue(rendered.segments[1].startswith(hostile_history))
        self.assertTrue(rendered.segments[1].endswith("\nRound:\n"))
        self.assertTrue(rendered.segments[2].startswith(hostile_round))
        self.assertIn('{"choice": "literal {history}"}', rendered.segments[2])
        self.assertNotIn("cache_layout", prompt.args)
        self.assertEqual(prompt._cache_layout, ("history", "round"))

    def test_prompt_layout_preserves_positional_nested_and_kwargs_behavior(self) -> None:
        positional = Prompt(
            "Prefix:{history}|Middle:{round}|Suffix:{tail}",
            cache_layout=("history", "round"),
            _should_register=False,
        )
        positional_result = positional.format("H", "R", "T")
        self.assertEqual(str(positional_result), "Prefix:H|Middle:R|Suffix:T")
        self.assertEqual(positional_result.segments, ("Prefix:", "H|Middle:", "R|Suffix:T"))

        nested = Prompt("nested:{round}", _should_register=False)
        template = "{history}|{round}|{nested}|{{\"flag\": {flag}}}"
        prompt = Prompt(template, cache_layout=("history", "round"), _should_register=False)
        result = prompt.format(history="H", round="R", nested=nested, flag="true")
        self.assertIs(type(result), SegmentedPrompt)
        self.assertEqual(str(result), "H|R|nested:R|{\"flag\": true}")

        initialized = Prompt(
            "{history}|{round}|{tail}",
            cache_layout=("history", "round"),
            _should_register=False,
            history="old-history",
            round="old-round",
            tail="old-tail",
        )
        overridden = initialized.format(history="new-history", round="new-round", tail="new-tail")
        self.assertEqual(str(overridden), "new-history|new-round|new-tail")
        self.assertEqual(overridden.segments, ("", "new-history|", "new-round|new-tail"))

    def test_invalid_layouts_fall_back_and_async_creation_keeps_metadata(self) -> None:
        for template in (
            "{history} but no round",
            "{history} then {history} then {round}",
            "{round} before {history}",
        ):
            prompt = Prompt(template, cache_layout=("history", "round"), _should_register=False)
            formatted = prompt.format(history="h", round="r")
            self.assertIs(type(formatted), str)

        legacy_cache_layout_field = Prompt("{cache_layout}", _should_register=False)
        self.assertEqual(legacy_cache_layout_field.format(cache_layout="plain field"), "plain field")

        class LegacyPrompt(Prompt):
            def __new__(cls, fstr, name=None, args=None):
                return super().__new__(cls, fstr, name, args)

        async def create_and_format():
            prompt = await Prompt.create_async(
                "{prefix}|{history}|{round}",
                cache_layout=("history", "round"),
                _should_register=False,
            )
            legacy = await LegacyPrompt.create_async("plain text", "legacy")
            return prompt, prompt.format(prefix="p", history="h", round="r"), legacy

        created, formatted, legacy = asyncio.run(create_and_format())
        self.assertEqual(created._cache_layout, ("history", "round"))
        self.assertIs(type(formatted), SegmentedPrompt)
        self.assertEqual(str(formatted), "p|h|r")
        self.assertEqual(legacy.name, "legacy")
        self.assertEqual(str(legacy), "plain text")

    def test_unannotated_prompt_keeps_plugin_subclass_format_path(self) -> None:
        class PluginPrompt(Prompt):
            constructions = 0

            def __new__(cls, *args, **kwargs):
                cls.constructions += 1
                return super().__new__(cls, *args, **kwargs)

        plugin_prompt = PluginPrompt("hello {person}", _should_register=False)
        result = plugin_prompt.format(person="plugin")
        self.assertIs(type(result), str)
        self.assertEqual(result, "hello plugin")
        self.assertEqual(PluginPrompt.constructions, 2)


class _CapturingLLMClient:
    def __init__(self, *, fail_first_with_413: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.fail_first_with_413 = fail_first_with_413

    async def get_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_with_413 and len(self.calls) == 1:
            raise _cache_usage.RespNotOkException(413, "payload too large")
        return APIResponse(content="done")

    async def get_embedding(self, *args, **kwargs):
        self.embedding_call = (args, kwargs)
        return APIResponse(embedding=[0.1])

    async def get_audio_transcriptions(self, **kwargs):
        self.audio_call = kwargs
        return APIResponse(content="recognized")


class MessageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _capture_public_messages(self, prompt, *, streaming: bool, system_prompt: str | None = None):
        request = LLMRequest(TaskConfig(), request_type="replyer", config=SimpleNamespace())
        model_info = _model_info()
        captured: dict[str, Any] = {}

        async def fake_execute(**kwargs):
            captured.update(kwargs)
            captured["messages"] = kwargs["message_factory"](object())
            response = SimpleNamespace(
                content="done",
                reasoning_content=None,
                tool_calls=[],
                usage=None,
            )
            return response, model_info

        request._execute_request = fake_execute
        if streaming:
            await request.generate_response_stream_async(prompt, on_delta=lambda _item: None)
        else:
            await request.generate_response_async(prompt, system_prompt=system_prompt)
        return captured

    async def test_response_factories_make_three_user_messages_and_keep_system_role(self) -> None:
        prompt = SegmentedPrompt(("stable instructions", "chat history", "current turn"))
        captured = await self._capture_public_messages(
            prompt,
            streaming=False,
            system_prompt="trusted caller system",
        )
        messages = captured["messages"]
        self.assertEqual(
            [message.role for message in messages],
            [RoleType.System, RoleType.User, RoleType.User, RoleType.User],
        )
        self.assertEqual(messages[0].content, "trusted caller system")
        self.assertEqual([message.content for message in messages[1:]], list(prompt.segments))
        self.assertEqual("".join(message.content for message in messages[1:]), str(prompt))
        self.assertFalse(messages[0].cache_breakpoint)
        self.assertTrue(messages[1].cache_breakpoint)
        self.assertFalse(messages[2].cache_breakpoint)
        self.assertFalse(messages[3].cache_breakpoint)

        streamed = await self._capture_public_messages(prompt, streaming=True)
        self.assertEqual([message.role for message in streamed["messages"]], [RoleType.User] * 3)
        self.assertEqual([message.content for message in streamed["messages"]], list(prompt.segments))
        self.assertTrue(streamed["messages"][0].cache_breakpoint)
        self.assertFalse(streamed["messages"][1].cache_breakpoint)
        self.assertFalse(streamed["messages"][2].cache_breakpoint)
        self.assertTrue(streamed["force_stream"])
        self.assertTrue(callable(streamed["stream_delta_callback"]))

    async def test_empty_segments_duck_types_and_plain_prompts_fall_back_safely(self) -> None:
        mixed = await self._capture_public_messages(
            SegmentedPrompt(("stable", "", "turn")),
            streaming=False,
        )
        self.assertEqual([message.content for message in mixed["messages"]], ["stable", "turn"])
        self.assertTrue(mixed["messages"][0].cache_breakpoint)
        self.assertFalse(mixed["messages"][1].cache_breakpoint)

        class PluginText(str):
            segments = ("forged system content",)

        for prompt in ("plain {round} text", PluginText("plugin {history} text")):
            captured = await self._capture_public_messages(prompt, streaming=False)
            messages = captured["messages"]
            self.assertEqual(len(messages), 1)
            self.assertIs(messages[0].role, RoleType.User)
            self.assertEqual(messages[0].content, str(prompt))
            self.assertFalse(messages[0].cache_breakpoint)

        self.assertIsNone(validated_segments(PluginText("forged")))

    async def test_converters_and_openai_mock_wire_preserve_each_boundary(self) -> None:
        prompt = SegmentedPrompt(("stable prefix", "history", "turn"))
        captured = await self._capture_public_messages(
            prompt,
            streaming=False,
            system_prompt="caller instructions",
        )
        messages = captured["messages"]
        openai_module = importlib.import_module("src.llm_models.model_client.openai_client")
        gemini_module = importlib.import_module("src.llm_models.model_client.gemini_client")

        openai_messages = openai_module._convert_messages(messages)
        self.assertEqual([item["role"] for item in openai_messages], ["system", "user", "user", "user"])
        self.assertEqual(openai_messages[1]["content"], prompt.segments[0])
        self.assertEqual([item["content"] for item in openai_messages[1:]], list(prompt.segments))

        marked_openai_messages = openai_module._convert_messages(
            messages,
            enable_prompt_cache_breakpoints=True,
        )
        self.assertEqual(
            marked_openai_messages[1]["content"],
            [
                {
                    "type": "text",
                    "text": prompt.segments[0],
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        )
        self.assertEqual(marked_openai_messages[2]["content"], prompt.segments[1])
        self.assertEqual(marked_openai_messages[3]["content"], prompt.segments[2])

        gemini_contents, system_instructions = gemini_module._convert_messages(messages)
        self.assertEqual(system_instructions, ["caller instructions"])
        self.assertEqual([content.role for content in gemini_contents], ["user", "user", "user"])
        self.assertEqual([content.parts[0].text for content in gemini_contents], list(prompt.segments))

        plain_wire = openai_module._convert_messages([Message(RoleType.User, "plain legacy text")])
        self.assertEqual(plain_wire, [{"role": "user", "content": "plain legacy text"}])

        multimodal = Message(
            RoleType.User,
            [("png", "image-one"), "text one", ("webp", "image-two"), "stable text"],
            cache_breakpoint=True,
        )
        multimodal_wire = openai_module._convert_messages(
            [multimodal],
            enable_prompt_cache_breakpoints=True,
        )[0]["content"]
        self.assertEqual([part["type"] for part in multimodal_wire], ["image_url", "text", "image_url", "text"])
        self.assertNotIn("prompt_cache_breakpoint", multimodal_wire[1])
        self.assertEqual(multimodal_wire[3]["prompt_cache_breakpoint"], {"mode": "explicit"})

        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4)
        fake_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="wire response", reasoning_content=None, tool_calls=[])
                )
            ],
            usage=usage,
        )
        completions = _cache_usage._OpenAICompletions(response=fake_response)
        client = object.__new__(OpenaiClient)
        client.api_provider = _provider(enabled=False, breakpoints=True)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        await client.get_response(
            model_info=_model_info(),
            message_list=messages,
            max_tokens=32,
            temperature=0.1,
            extra_params={"prompt_cache_key": "wire-cache-key"},
        )
        wire_call = completions.calls[0]
        self.assertEqual(
            [item["role"] for item in wire_call["messages"]],
            ["system", "user", "user", "user"],
        )
        self.assertEqual(
            wire_call["messages"][1]["content"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertEqual(wire_call["messages"][2]["content"], prompt.segments[1])
        self.assertEqual(wire_call["messages"][3]["content"], prompt.segments[2])
        self.assertEqual(wire_call["extra_body"]["prompt_cache_key"], "wire-cache-key")
        self.assertNotIn("prompt_cache_options", wire_call)

    async def test_breakpoint_flag_off_preserves_wire_content_even_when_message_is_marked(self) -> None:
        client, completions = _openai_wire_client(_provider(enabled=False, breakpoints=False))
        marked_message = MessageBuilder().add_text_content("stable text").set_cache_breakpoint().build()
        await client.get_response(
            model_info=_model_info(),
            message_list=[marked_message],
            max_tokens=32,
            temperature=0.1,
        )
        self.assertEqual(completions.calls[0]["messages"], [{"role": "user", "content": "stable text"}])
        self.assertIsNone(completions.calls[0]["extra_body"])

    async def test_execute_request_attaches_request_scope_to_call_local_messages(self) -> None:
        request = LLMRequest(
            TaskConfig(model_list=["model-a"]),
            request_type="planner",
            config=SimpleNamespace(),
            session_id="scoped-chat-id",
        )
        model_info = _model_info()
        provider = _provider(enabled=False)
        client = _CapturingLLMClient()
        request._select_model = lambda **_kwargs: (model_info, provider, client)
        request._cache_diagnostics_enabled = lambda: False
        original = [MessageBuilder().add_text_content("plain prompt").build()]

        await request._execute_request(
            request_type=RequestType.RESPONSE,
            message_factory=lambda _client: original,
        )

        delivered = client.calls[0]["message_list"]
        self.assertIsNot(delivered, original)
        self.assertIsNot(delivered[0], original[0])
        self.assertIsNone(original[0].cache_context)
        self.assertEqual(delivered[0].cache_context, PromptCacheScope("scoped-chat-id", "planner"))
        self.assertIsNotNone(delivered[0].cache_context)
        self.assertIs(request._attach_request_cache_scope(original, RequestType.EMBEDDING), original)

    async def test_stream_response_factory_carries_scope_and_only_static_prefix_marker(self) -> None:
        request = LLMRequest(
            TaskConfig(model_list=["model-a"]),
            request_type="replyer",
            config=SimpleNamespace(),
            session_id="stream-session",
        )
        model_info = _model_info()
        provider = _provider(enabled=False)
        client = _CapturingLLMClient()
        request._select_model = lambda **_kwargs: (model_info, provider, client)
        request._cache_diagnostics_enabled = lambda: False
        prompt = SegmentedPrompt(("stable", "history", "turn"))

        await request.generate_response_stream_async(prompt, on_delta=None)

        delivered = client.calls[0]["message_list"]
        self.assertEqual([message.role for message in delivered], [RoleType.User] * 3)
        self.assertEqual([message.content for message in delivered], list(prompt.segments))
        self.assertEqual(delivered[0].cache_context, PromptCacheScope("stream-session", "replyer"))
        self.assertEqual(delivered[1].cache_context, delivered[0].cache_context)
        self.assertTrue(delivered[0].cache_breakpoint)
        self.assertFalse(delivered[1].cache_breakpoint)
        self.assertFalse(delivered[2].cache_breakpoint)
        self.assertTrue(client.calls[0]["model_info"].force_stream_mode)

    async def test_413_retry_keeps_scope_breakpoint_tools_and_effective_messages(self) -> None:
        provider = _provider(max_retry=2, breakpoints=True)
        request = LLMRequest(
            TaskConfig(model_list=["model-a"]),
            request_type="replyer",
            config=SimpleNamespace(),
            session_id="chat-for-retry",
        )
        model_info = _model_info()
        client, completions = _openai_wire_client(provider)
        request._select_model = lambda **_kwargs: (model_info, provider, client)
        request._cache_diagnostics_enabled = lambda: False
        original_get_response = client.get_response
        intercepted_calls = []

        async def fail_once(**kwargs):
            intercepted_calls.append(kwargs)
            if len(intercepted_calls) == 1:
                raise _cache_usage.RespNotOkException(413, "payload too large")
            return await original_get_response(**kwargs)

        client.get_response = fail_once
        tools = [ToolOption("lookup", "lookup", input_schema={"type": "object", "properties": {}})]
        prompt = SegmentedPrompt(("stable prefix", "history", "current turn"))
        assistant_calls = [SimpleNamespace(id="call-1")]
        source_messages = [
            Message(RoleType.System, ["trusted", " system"]),
            *request._build_user_prompt_messages(prompt),
            Message(RoleType.Assistant, ["assistant", " continuation"], tool_calls=assistant_calls),
            Message(RoleType.Tool, ["tool", " result"], tool_call_id="call-1"),
        ]
        response, _ = await request._execute_request(
            request_type=RequestType.RESPONSE,
            message_factory=lambda _client: source_messages,
            tool_options=tools,
        )

        self.assertEqual(len(intercepted_calls), 2)
        self.assertIs(intercepted_calls[0]["tool_options"], tools)
        self.assertIs(intercepted_calls[1]["tool_options"], tools)
        first_scope = intercepted_calls[0]["message_list"][0].cache_context
        second_scope = intercepted_calls[1]["message_list"][0].cache_context
        self.assertIs(first_scope, second_scope)
        self.assertTrue(intercepted_calls[0]["message_list"][1].cache_breakpoint)
        self.assertTrue(intercepted_calls[1]["message_list"][1].cache_breakpoint)
        self.assertFalse(intercepted_calls[1]["message_list"][2].cache_breakpoint)
        self.assertEqual(
            [message.role for message in response._cache_diagnostic_messages],
            [RoleType.System, RoleType.User, RoleType.User, RoleType.User, RoleType.Assistant, RoleType.Tool],
        )
        self.assertEqual(response._cache_diagnostic_messages[0].content, ["trusted", " system"])
        self.assertEqual(
            [message.content for message in response._cache_diagnostic_messages[1:4]],
            list(prompt.segments),
        )
        self.assertEqual(response._cache_diagnostic_messages[4].tool_calls, assistant_calls)
        self.assertEqual(response._cache_diagnostic_messages[5].tool_call_id, "call-1")
        wire_call = completions.calls[0]
        self.assertEqual(wire_call["extra_body"]["prompt_cache_key"], _expected_cache_key(provider, first_scope))
        self.assertEqual(wire_call["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(
            [message["role"] for message in wire_call["messages"]],
            ["system", "user", "user", "user", "assistant", "tool"],
        )
        self.assertEqual(
            wire_call["messages"][1]["content"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertEqual(wire_call["messages"][2]["content"], prompt.segments[1])


class SessionCacheKeyTests(unittest.IsolatedAsyncioTestCase):
    async def _run_response(
        self,
        provider: APIProvider,
        scope: PromptCacheScope | None,
        model_info: ModelInfo | None = None,
        *,
        prompt: str = "prompt A",
        extra_params: dict[str, Any] | None = None,
        logical_time: float = 10.0,
        streaming: bool = False,
        cache_breakpoint: bool = False,
    ):
        model_info = model_info or _model_info()
        model_info.api_provider = provider.name
        if streaming:
            model_info.force_stream_mode = True
        client, completions = _openai_wire_client(provider)
        builder = MessageBuilder().add_text_content(prompt)
        if scope is not None:
            builder.set_cache_context(scope)
        if cache_breakpoint:
            builder.set_cache_breakpoint()
        messages = [builder.build()]

        async def stream_handler(_stream, _interrupt):
            return APIResponse(content="stream response"), None

        with patch.object(_cache_usage.utils_model.time, "time", return_value=logical_time):
            await client.get_response(
                model_info=model_info,
                message_list=messages,
                tool_options=None,
                temperature=0.2,
                max_tokens=32,
                extra_params=extra_params,
                stream_response_handler=stream_handler if streaming else None,
            )
        return completions.calls[-1], client

    async def test_key_is_stable_per_provider_session_and_task(self) -> None:
        provider = _provider()
        scope_a = PromptCacheScope("raw-chat-id-for-test", "replyer")
        call_a1, _ = await self._run_response(
            provider,
            scope_a,
            prompt="first changing prompt",
            logical_time=1,
        )
        key_a1 = call_a1["extra_body"]["prompt_cache_key"]
        call_b, _ = await self._run_response(
            provider,
            PromptCacheScope("other-chat-id", "replyer"),
            prompt="different prompt",
            logical_time=2,
        )
        call_a2, _ = await self._run_response(
            provider,
            scope_a,
            prompt="third prompt",
            logical_time=999999,
        )
        key_a2 = call_a2["extra_body"]["prompt_cache_key"]
        self.assertEqual(len(key_a1), 64)
        self.assertEqual(key_a1, key_a2)
        self.assertEqual(key_a1, _expected_cache_key(provider, scope_a))
        self.assertNotEqual(key_a1, call_b["extra_body"]["prompt_cache_key"])
        self.assertNotIn("raw-chat-id-for-test", key_a1)
        self.assertNotIn(provider.api_key, key_a1)
        self.assertNotIn("model-a-id", key_a1)
        self.assertNotIn("raw-chat-id-for-test", repr(call_a1))
        self.assertNotIn(provider.api_key, repr(call_a2))

        other_task = PromptCacheScope("raw-chat-id-for-test", "planner")
        other_task_call, _ = await self._run_response(provider, other_task)
        self.assertNotEqual(key_a1, other_task_call["extra_body"]["prompt_cache_key"])

        other_provider = _provider(name="provider-b")
        provider_call, _ = await self._run_response(other_provider, scope_a)
        self.assertNotEqual(key_a1, provider_call["extra_body"]["prompt_cache_key"])
        alternate_client_type = _provider(client_type="openai-compatible")
        client_type_call, _ = await self._run_response(alternate_client_type, scope_a)
        self.assertNotEqual(key_a1, client_type_call["extra_body"]["prompt_cache_key"])

        switched_model = _model_info(name="different-model")
        switched_model.api_provider = provider.name
        switched_model_call, _ = await self._run_response(provider, scope_a, switched_model)
        self.assertEqual(key_a1, switched_model_call["extra_body"]["prompt_cache_key"])

        stream_call, _ = await self._run_response(
            provider,
            scope_a,
            prompt="streamed request",
            streaming=True,
        )
        self.assertTrue(stream_call["stream"])
        self.assertEqual(stream_call["extra_body"]["prompt_cache_key"], key_a1)

    async def test_disabled_unsupported_sessionless_and_explicit_override(self) -> None:
        scope = PromptCacheScope("session", "replyer")
        disabled_call, _ = await self._run_response(_provider(enabled=False), scope)
        self.assertNotIn("prompt_cache_key", disabled_call["extra_body"] or {})
        sessionless_call, _ = await self._run_response(_provider(), None)
        self.assertIsNone(sessionless_call["extra_body"])

        provider = _provider()
        for existing in (None, "", "operator-supplied-key"):
            call, _ = await self._run_response(
                provider,
                scope,
                extra_params={"prompt_cache_key": existing},
            )
            self.assertIs(call["extra_body"]["prompt_cache_key"], existing)

        model_configured = _model_info(extra_params={"prompt_cache_key": "model-level-key"})
        model_configured.api_provider = provider.name
        client, completions = _openai_wire_client(provider)
        request = LLMRequest(TaskConfig(), request_type="replyer", config=SimpleNamespace())
        await request._attempt_request_on_model(
            model_info=model_configured,
            api_provider=provider,
            client=client,
            request_type=RequestType.RESPONSE,
            message_list=[
                MessageBuilder().add_text_content("override prompt").set_cache_context(scope).build()
            ],
            tool_options=None,
            response_format=None,
            stream_response_handler=None,
            async_response_parser=None,
            temperature=0.2,
            max_tokens=32,
            embedding_input=None,
            audio_base64=None,
            extra_params={"prompt_cache_key": None},
        )
        self.assertIsNone(completions.calls[0]["extra_body"]["prompt_cache_key"])
        self.assertEqual(model_configured.extra_params, {"prompt_cache_key": "model-level-key"})
        self.assertTrue(provider.enable_session_cache_key)

    async def test_embedding_and_audio_do_not_receive_cache_keys(self) -> None:
        provider = _provider()
        model_info = _model_info()
        request = LLMRequest(TaskConfig(), request_type="replyer", config=SimpleNamespace(), session_id="audio-chat")
        client = _CapturingLLMClient()
        await request._attempt_request_on_model(
            model_info=model_info,
            api_provider=provider,
            client=client,
            request_type=RequestType.EMBEDDING,
            message_list=[],
            tool_options=None,
            response_format=None,
            stream_response_handler=None,
            async_response_parser=None,
            temperature=None,
            max_tokens=None,
            embedding_input="embedding text",
            audio_base64=None,
        )
        self.assertNotIn("prompt_cache_key", client.embedding_call[1]["extra_params"])

        await request._attempt_request_on_model(
            model_info=model_info,
            api_provider=provider,
            client=client,
            request_type=RequestType.AUDIO,
            message_list=[],
            tool_options=None,
            response_format=None,
            stream_response_handler=None,
            async_response_parser=None,
            temperature=None,
            max_tokens=None,
            embedding_input=None,
            audio_base64="audio bytes",
        )
        self.assertNotIn("prompt_cache_key", client.audio_call["extra_params"])
        self.assertEqual(model_info.extra_params, {})

    def test_provider_flag_defaults_false_and_is_strictly_boolean(self) -> None:
        provider = APIProvider(name="default-provider", base_url="https://provider.invalid", api_key="key")
        self.assertFalse(provider.enable_session_cache_key)
        self.assertFalse(provider.enable_prompt_cache_breakpoints)
        valid = APIProvider.from_dict(
            {
                "name": "toml-provider",
                "base_url": "https://provider.invalid",
                "api_key": "key",
                "enable_session_cache_key": True,
                "enable_prompt_cache_breakpoints": True,
            }
        )
        self.assertTrue(valid.enable_session_cache_key)
        self.assertTrue(valid.enable_prompt_cache_breakpoints)
        with self.assertRaises(ValueError):
            APIProvider(
                name="bad-provider",
                base_url="https://provider.invalid",
                api_key="key",
                enable_session_cache_key="true",
            )
        with self.assertRaises(TypeError):
            APIProvider.from_dict(
                {
                    "name": "bad-provider",
                    "base_url": "https://provider.invalid",
                    "api_key": "key",
                    "enable_session_cache_key": "true",
                }
            )
        with self.assertRaises(TypeError):
            APIProvider.from_dict(
                {
                    "name": "bad-provider",
                    "base_url": "https://provider.invalid",
                    "api_key": "key",
                    "enable_prompt_cache_breakpoints": "true",
                }
            )


class PromptCacheUsageBoundaryTests(unittest.TestCase):
    def test_zero_cached_read_is_not_replaced_by_cache_write_or_creation_counts(self) -> None:
        provider_usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=2,
            total_tokens=102,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=0,
                cache_creation_tokens=10**9,
                cache_write_tokens=10**9,
            ),
            prompt_cache_write_tokens=10**9,
        )
        response = _cache_usage._openai_response(provider_usage)
        parsed, _ = _cache_usage._default_normal_response_parser(response)
        self.assertEqual(parsed.cache_usage, (0, 100, True))


if __name__ == "__main__":
    unittest.main()
