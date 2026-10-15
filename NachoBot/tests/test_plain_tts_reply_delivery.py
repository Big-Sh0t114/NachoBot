"""Offline regressions for adapter-declared plain-text TTS delivery.

The tests load platform-neutral helpers directly and AST-extract the production
methods under test. They do not import the Core package or initialize its DB.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import sys
import traceback
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


CORE_ROOT = Path(__file__).resolve().parents[1]


def _load_pure_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_callable(path: Path, name: str, namespace: dict, *, owner: str | None = None):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if owner is None:
        node = next(
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        )
    else:
        class_node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == owner)
        node = next(
            item
            for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        )
    node = copy.deepcopy(node)
    node.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class _ReplyType(Enum):
    TEXT = "text"
    IMAGE = "image"
    EMOJI = "emoji"
    HYBRID = "hybrid"


class _ReplySet:
    def __init__(self):
        self.reply_data = []

    def add_text_content(self, text: str):
        self.reply_data.append(SimpleNamespace(content_type=_ReplyType.TEXT, content=text))

    def __len__(self):
        return len(self.reply_data)


class _SendAPI:
    def __init__(self, *, delivered: bool = True, suppress: bool = False):
        self.delivered = delivered
        self.suppress = suppress
        self.text_calls = []
        self.tts_calls = []
        self.receipts = []

    def _receipt(self):
        receipt = SimpleNamespace(delivered=self.delivered)
        self.receipts.append(receipt)
        return receipt

    def _should_suppress_reply_set(self, _reply_set):
        return self.suppress

    async def text_to_stream_receipt(self, **kwargs):
        self.text_calls.append(kwargs)
        return self._receipt()

    async def tts_text_to_stream_receipt(self, **kwargs):
        self.tts_calls.append(kwargs)
        return self._receipt()


class PlainTTSReplyCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tts_module = _load_pure_module(
            "offline_tts_reply_delivery",
            CORE_ROOT / "src" / "chat" / "tts_reply_delivery.py",
        )

    def test_common_ascii_faces_and_kaomoji_are_removed(self):
        clean = self.tts_module.clean_tts_reply_text
        for face in ("(´･ω･`)", "(T_T)", "(^_^)"):
            with self.subTest(face=face):
                self.assertEqual(clean(face), "")
        for text in ("你好(´･ω･`)呀", "你好（´･ω･`）呀"):
            with self.subTest(text=text):
                self.assertEqual("".join(clean(text).split()), "你好呀")

    def test_complete_kaomoji_groups_are_removed_standalone_attached_and_fullwidth(self):
        clean = self.tts_module.clean_tts_reply_text
        cases = (
            ("(^o^)", ""),
            ("今天很开心(^o^)！", "今天很开心！"),
            ("今天很开心（^o^）！", "今天很开心！"),
            ("(*´▽｀*)", ""),
            ("明天见(*´▽｀*)。", "明天见。"),
            ("明天见（*´▽｀*）。", "明天见。"),
            ("(づ｡◕‿‿◕｡)づ", ""),
            ("你好(づ｡◕‿‿◕｡)づ", "你好"),
            ("你好（づ｡◕‿‿◕｡）づ", "你好"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual("".join(clean(text).split()), expected)

    def test_complete_shrug_is_removed_without_losing_adjacent_reply_text(self):
        clean = self.tts_module.clean_tts_reply_text
        shrug = r"¯\_(ツ)_/¯"
        self.assertEqual(clean(shrug), "")
        self.assertEqual(clean(f"谢谢{shrug}。"), "谢谢。")

    def test_emoji_and_punctuation_only_text_is_empty(self):
        clean = self.tts_module.clean_tts_reply_text
        for text in ("🤗❤️✨", "✨！！！", "**", "~~", "_ _", "(T_T)🤗"):
            with self.subTest(text=text):
                self.assertEqual(clean(text), "")

    def test_markdown_is_removed_without_losing_spoken_content(self):
        clean = self.tts_module.clean_tts_reply_text
        self.assertEqual(clean("**你好**，`test_file`。"), "你好，test_file。")

    def test_control_thinking_blocks_are_not_spoken(self):
        clean = self.tts_module.clean_tts_reply_text
        self.assertEqual(clean("你好<think>不要读这段</think>，继续。"), "你好，继续。")

    def test_analysis_control_tokens_keep_only_the_final_section(self):
        clean = self.tts_module.clean_tts_reply_text
        self.assertEqual(clean("<|analysis|>不要朗读这段推理<|final|>你好。"), "你好。")
        self.assertEqual(clean("<|analysis|>不要朗读这段推理"), "")

    def test_content_parentheses_and_units_are_preserved(self):
        clean = self.tts_module.clean_tts_reply_text
        for text in (
            "(明天上午十点)",
            "(test1)",
            "(C++)",
            "(3.14)",
            "(10:30:45)",
            "(v1.2.3)",
            "(十^2)",
            "还剩(8)个。",
            "范围是(0,8)。",
            "答案是8)。",
            "小于三可记为 <3。",
            "20℃",
        ):
            with self.subTest(text=text):
                self.assertEqual(clean(text), text)


class PlainTTSCoreSendTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = _load_pure_module(
            "offline_tts_runtime_capabilities",
            CORE_ROOT / "src" / "chat" / "runtime_capabilities.py",
        )
        cls.json_delivery = _load_pure_module(
            "offline_tts_json_delivery",
            CORE_ROOT / "src" / "chat" / "json_reply_delivery.py",
        )
        cls.clean = _load_pure_module(
            "offline_tts_send_cleaner",
            CORE_ROOT / "src" / "chat" / "tts_reply_delivery.py",
        ).clean_tts_reply_text
        cls.send_methods = {}
        paths = {
            "brain": CORE_ROOT / "src" / "chat" / "brain_chat" / "brain_chat.py",
            "heart": CORE_ROOT / "src" / "chat" / "heart_flow" / "heartFC_chat.py",
        }
        for name, path in paths.items():
            namespace = {
                "ReplyContentType": _ReplyType,
                "runtime_capabilities_from_message": cls.runtime.runtime_capabilities_from_message,
                "clean_tts_reply_text": cls.clean,
                "prepare_json_envelope_delivery": cls.json_delivery.prepare_json_envelope_delivery,
                "_prepare_json_envelope_delivery": cls.json_delivery.prepare_json_envelope_delivery,
                "send_api": None,
                "message_api": SimpleNamespace(count_new_messages=lambda **_kwargs: 0),
                "logger": Mock(),
                "random": SimpleNamespace(randint=lambda _low, _high: 2),
                "time": SimpleNamespace(time=lambda: 1.0),
            }
            cls.send_methods[name] = _extract_callable(
                path,
                "_send_response_permitted",
                namespace,
                owner="BrainChatting" if name == "brain" else "HeartFChatting",
            )
            cls.send_methods[name].__globals__["_test_namespace"] = namespace

    async def _send(self, adapter: str, chunks, *, mode="tts_text", language="zh", delivered=True):
        method = self.send_methods[adapter]
        namespace = method.__globals__["_test_namespace"]
        send_api = _SendAPI(delivered=delivered)
        namespace["send_api"] = send_api
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                additional_config={
                    "runtime_capabilities": {"reply_delivery": mode, "tts_language": language}
                }
            )
        )
        self_obj = SimpleNamespace(
            chat_stream=SimpleNamespace(stream_id="stream-1"),
            last_read_time=0,
            log_prefix="test",
        )
        reply_set = SimpleNamespace(reply_data=chunks)
        receipts = []
        result = await method(self_obj, reply_set, message, None, receipts)
        return result, receipts, send_api

    async def test_tts_mode_aggregates_only_text_and_sends_one_utterance(self):
        chunks = [
            SimpleNamespace(content_type=_ReplyType.TEXT, content="你好，"),
            SimpleNamespace(content_type=_ReplyType.IMAGE, content="ignored image"),
            SimpleNamespace(content_type=_ReplyType.TEXT, content="世界！"),
        ]
        for adapter in self.send_methods:
            with self.subTest(adapter=adapter):
                result, receipts, send_api = await self._send(adapter, chunks)
                self.assertEqual(result[0], "你好，世界！")
                self.assertEqual(len(receipts), 1)
                self.assertEqual(len(send_api.tts_calls), 1)
                self.assertEqual(send_api.text_calls, [])
                call = send_api.tts_calls[0]
                self.assertEqual(call["text"], "你好，世界！")
                self.assertEqual(call["display_message"], "你好，世界！")
                self.assertEqual(call["transport_text"], "你好，世界！")
                self.assertEqual(call["text_lang"], "zh")

    async def test_tts_mode_skips_receipts_when_cleanup_leaves_no_speech(self):
        empty_only_inputs = (
            "🤗❤️✨",
            "✨！！！",
            "**",
            "~~",
            "_ _",
            "(T_T)",
            r"¯\_(ツ)_/¯",
            "<|analysis|>不要朗读这段推理",
        )
        for adapter in self.send_methods:
            for text in empty_only_inputs:
                with self.subTest(adapter=adapter, text=text):
                    chunks = [SimpleNamespace(content_type=_ReplyType.TEXT, content=text)]
                    result, receipts, send_api = await self._send(adapter, chunks)
                    self.assertEqual(result[0], "")
                    self.assertEqual(receipts, [])
                    self.assertEqual(send_api.tts_calls, [])
                    self.assertEqual(send_api.text_calls, [])

    async def test_tts_sends_clean_final_text_and_preserves_numeric_content_for_both_senders(self):
        cases = (
            ("<|analysis|>不要朗读这段推理<|final|>你好。", "你好。"),
            ("还剩(8)个。", "还剩(8)个。"),
            ("范围是(0,8)。", "范围是(0,8)。"),
            ("答案是8)。", "答案是8)。"),
            ("小于三可记为 <3。", "小于三可记为 <3。"),
            (r"谢谢¯\_(ツ)_/¯。", "谢谢。"),
            (
                '{"reply":"显示字段","tts_text":"旁路字段"}',
                '{"reply":"显示字段","tts_text":"旁路字段"}',
            ),
        )
        for adapter in self.send_methods:
            for raw_text, expected in cases:
                with self.subTest(adapter=adapter, text=raw_text):
                    chunks = [SimpleNamespace(content_type=_ReplyType.TEXT, content=raw_text)]
                    result, receipts, send_api = await self._send(adapter, chunks)
                    self.assertEqual(result[0], expected)
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(len(send_api.tts_calls), 1)
                    self.assertEqual(send_api.text_calls, [])
                    call = send_api.tts_calls[0]
                    self.assertEqual(call["text"], expected)
                    self.assertEqual(call["display_message"], expected)
                    self.assertEqual(call["transport_text"], expected)

    async def test_undelivered_tts_receipt_is_recorded_but_not_returned_as_history(self):
        chunks = [SimpleNamespace(content_type=_ReplyType.TEXT, content="收到。")]
        for adapter in self.send_methods:
            with self.subTest(adapter=adapter):
                result, receipts, send_api = await self._send(adapter, chunks, delivered=False)
                self.assertEqual(result[0], "")
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].delivered, False)
                self.assertEqual(len(send_api.tts_calls), 1)

    async def test_chunked_mode_keeps_separate_text_sends(self):
        chunks = [
            SimpleNamespace(content_type=_ReplyType.TEXT, content="第一段。"),
            SimpleNamespace(content_type=_ReplyType.TEXT, content="第二段！"),
        ]
        for adapter in self.send_methods:
            with self.subTest(adapter=adapter):
                result, receipts, send_api = await self._send(adapter, chunks, mode="chunked", language="")
                self.assertEqual(result[0], "第一段。第二段！")
                self.assertEqual([call["text"] for call in send_api.text_calls], ["第一段。", "第二段！"])
                self.assertEqual(send_api.tts_calls, [])
                self.assertEqual(len(receipts), 2)

    async def test_json_envelope_mode_keeps_its_explicit_tts_projection(self):
        envelope = '{"reply":"显示内容","tts_text":"口语内容"}'
        chunks = [SimpleNamespace(content_type=_ReplyType.TEXT, content=envelope)]
        for adapter in self.send_methods:
            with self.subTest(adapter=adapter):
                result, receipts, send_api = await self._send(
                    adapter,
                    chunks,
                    mode="json_envelope",
                    language="zh",
                )
                self.assertEqual(result[0], "显示内容")
                self.assertEqual(len(receipts), 1)
                self.assertEqual(len(send_api.tts_calls), 1)
                self.assertEqual(send_api.tts_calls[0]["text"], "口语内容")
                self.assertEqual(send_api.tts_calls[0]["display_message"], "显示内容")
                self.assertEqual(send_api.tts_calls[0]["transport_text"], envelope)


class GeneratorPlainTTSModeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = _load_pure_module(
            "offline_tts_generator_runtime_capabilities",
            CORE_ROOT / "src" / "chat" / "runtime_capabilities.py",
        )
        generator_path = CORE_ROOT / "src" / "plugin_system" / "apis" / "generator_api.py"
        cls.namespace = {
            "asyncio": asyncio,
            "json": json,
            "traceback": traceback,
            "logger": Mock(),
            "ReplySetModel": _ReplySet,
            "runtime_capabilities_from_message": cls.runtime.runtime_capabilities_from_message,
            "runtime_capabilities_from_stream": cls.runtime.runtime_capabilities_from_stream,
            "scoped_runtime_capabilities": cls.runtime.scoped_runtime_capabilities,
            "ReqAbortException": type("ReqAbortException", (Exception,), {}),
            "release_reply_context": None,
            "assemble_reply_context": None,
            "get_replyer": None,
            "process_human_text": None,
        }
        cls.generate_reply = staticmethod(_extract_callable(generator_path, "generate_reply", cls.namespace))
        cls.rewrite_reply = staticmethod(_extract_callable(generator_path, "rewrite_reply", cls.namespace))

    def _message_and_stream(self, capabilities):
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                additional_config={"runtime_capabilities": capabilities}
            )
        )
        stream = SimpleNamespace(
            stream_id="stream-1",
            context=SimpleNamespace(message=message),
        )
        return message, stream

    def _set_generation_stubs(self, replyer, process_calls):
        async def assemble_reply_context(_reply_context, *, target_chat_id):
            self.assertEqual(target_chat_id, "stream-1")
            return SimpleNamespace(context_refs=())

        async def release_reply_context(_refs, _reason):
            return None

        def process_human_text(content, enable_splitter, enable_chinese_typo):
            process_calls.append((content, enable_splitter, enable_chinese_typo))
            reply_set = _ReplySet()
            reply_set.add_text_content(f"processed:{content}")
            return reply_set

        self.namespace["assemble_reply_context"] = assemble_reply_context
        self.namespace["release_reply_context"] = release_reply_context
        self.namespace["get_replyer"] = lambda *_args, **_kwargs: replyer
        self.namespace["process_human_text"] = process_human_text

    async def test_generate_tts_mode_preserves_punctuation_and_json_looking_literal(self):
        content = '{"reply":"literal","tts_text":"do not extract"}。'
        message, stream = self._message_and_stream({"reply_delivery": "tts_text", "tts_language": "zh"})
        process_calls = []

        async def generate_reply_with_context(**_kwargs):
            return True, SimpleNamespace(content=content, sandbox_edit_handoff=None)

        replyer = SimpleNamespace(chat_stream=stream, generate_reply_with_context=generate_reply_with_context)
        self._set_generation_stubs(replyer, process_calls)
        success, response = await self.generate_reply(chat_stream=stream, reply_message=message)

        self.assertTrue(success)
        self.assertEqual(process_calls, [])
        self.assertEqual(len(response.reply_set), 1)
        self.assertEqual(response.reply_set.reply_data[0].content, content)

    async def test_generate_default_mode_still_uses_human_text_processing(self):
        _message, stream = self._message_and_stream({"reply_delivery": "chunked"})
        process_calls = []
        content = "普通模式内容。"

        async def generate_reply_with_context(**_kwargs):
            return True, SimpleNamespace(content=content, sandbox_edit_handoff=None)

        replyer = SimpleNamespace(chat_stream=stream, generate_reply_with_context=generate_reply_with_context)
        self._set_generation_stubs(replyer, process_calls)
        success, response = await self.generate_reply(chat_stream=stream)

        self.assertTrue(success)
        self.assertEqual(process_calls, [(content, True, True)])
        self.assertEqual(response.reply_set.reply_data[0].content, f"processed:{content}")

    async def test_rewrite_tts_mode_preserves_json_looking_literal(self):
        content = '{"reply":"literal","tts_text":"not a sidecar"}'
        message, stream = self._message_and_stream({"reply_delivery": "tts_text"})
        process_calls = []

        async def rewrite_reply_with_context(**_kwargs):
            return True, SimpleNamespace(content=content)

        replyer = SimpleNamespace(chat_stream=stream, rewrite_reply_with_context=rewrite_reply_with_context)
        self._set_generation_stubs(replyer, process_calls)
        success, response = await self.rewrite_reply(chat_stream=stream)

        self.assertTrue(success)
        self.assertEqual(process_calls, [])
        self.assertEqual(len(response.reply_set), 1)
        self.assertEqual(response.reply_set.reply_data[0].content, content)

    async def test_rewrite_uses_pre_await_delivery_capability_snapshot(self):
        content = "保持这句。以及标点！"
        message, stream = self._message_and_stream({"reply_delivery": "tts_text"})
        process_calls = []

        async def rewrite_reply_with_context(**_kwargs):
            stream.context.message = SimpleNamespace(
                message_info=SimpleNamespace(
                    additional_config={"runtime_capabilities": {"reply_delivery": "chunked"}}
                )
            )
            return True, SimpleNamespace(content=content)

        replyer = SimpleNamespace(chat_stream=stream, rewrite_reply_with_context=rewrite_reply_with_context)
        self._set_generation_stubs(replyer, process_calls)
        success, response = await self.rewrite_reply(chat_stream=stream)

        self.assertTrue(success)
        self.assertEqual(process_calls, [])
        self.assertEqual(len(response.reply_set), 1)
        self.assertEqual(response.reply_set.reply_data[0].content, content)

    async def test_rewrite_default_mode_still_uses_human_text_processing(self):
        _message, stream = self._message_and_stream({"reply_delivery": "chunked"})
        process_calls = []
        content = "普通重写内容！"

        async def rewrite_reply_with_context(**_kwargs):
            return True, SimpleNamespace(content=content)

        replyer = SimpleNamespace(chat_stream=stream, rewrite_reply_with_context=rewrite_reply_with_context)
        self._set_generation_stubs(replyer, process_calls)
        success, response = await self.rewrite_reply(chat_stream=stream)

        self.assertTrue(success)
        self.assertEqual(process_calls, [(content, True, True)])
        self.assertEqual(response.reply_set.reply_data[0].content, f"processed:{content}")


if __name__ == "__main__":
    unittest.main()
