"""Execute the real generation function with inert LLM/context dependencies."""
import ast
import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

CORE = Path(__file__).resolve().parents[2] / 'NachoBot'
spec = importlib.util.spec_from_file_location('_voice_generation_caps', CORE / 'src/chat/runtime_capabilities.py')
caps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = caps
spec.loader.exec_module(caps)


class ReplySet(list):
    def add_text_content(self, content):
        self.append(content)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_snapshot_json_and_serialized_feedback_reach_llm(self):
        tree = ast.parse((CORE / 'src/plugin_system/apis/generator_api.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'generate_reply')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
        ast.fix_missing_locations(module)
        stream = SimpleNamespace(stream_id='same-conversation', context=SimpleNamespace(message=None))
        received = []
        async def generate(**kwargs):
            received.append((kwargs, caps.runtime_capabilities_from_stream(stream)))
            await asyncio.sleep(0)
            return True, SimpleNamespace(content='{"reply":"先听你说","emotion":"normal","action":"待机/放松"}', sandbox_edit_handoff=None)
        replyer = SimpleNamespace(chat_stream=stream, generate_reply_with_context=generate)
        namespace = dict(asyncio=asyncio, json=json, logger=Mock(), traceback=Mock(),
                         get_replyer=lambda *a, **k: replyer,
                         assemble_reply_context=AsyncMock(return_value=SimpleNamespace(context_refs=())),
                         release_reply_context=AsyncMock(), ReplySetModel=ReplySet,
                         ReqAbortException=type('ReqAbortException', (Exception,), {}),
                         process_human_text=lambda *a: ReplySet([a[0]]),
                         additional_config_from_message=caps.additional_config_from_message,
                         runtime_capabilities_from_message=caps.runtime_capabilities_from_message,
                         runtime_capabilities_from_stream=caps.runtime_capabilities_from_stream,
                         scoped_runtime_capabilities=caps.scoped_runtime_capabilities)
        exec(compile(module, 'generator_api.py', 'exec'), namespace)
        voice = SimpleNamespace(chat_id=stream.stream_id, additional_config=json.dumps({
            'runtime_capabilities': {'reply_controls': True, 'reply_delivery': 'json_envelope',
                                     'control_emotions': ['normal'], 'control_actions': ['待机/放松'],
                                     'interruption_feedback': {'count': 3}}}))
        text = SimpleNamespace(chat_id=stream.stream_id, additional_config='{}')
        outcomes = await asyncio.gather(
            namespace['generate_reply'](chat_stream=stream, reply_message=voice),
            namespace['generate_reply'](chat_stream=stream, reply_message=text))
        self.assertTrue(all(success for success, _ in outcomes))
        self.assertIn('Allowed schema:', received[0][0]['extra_info'])
        self.assertIn('多次打断', received[0][0]['extra_info'])
        self.assertNotIn('For this WebUI', received[0][0]['extra_info'])
        self.assertNotIn('prefer normal emotion', received[0][0]['extra_info'])
        self.assertEqual(received[0][1].reply_delivery, 'json_envelope')
        self.assertEqual(received[1][1].reply_delivery, 'chunked')
        self.assertTrue(received[0][1].reply_controls)
        self.assertEqual(received[0][1].interruption_feedback_count, 3)
        self.assertFalse(received[1][1].reply_controls)
        self.assertEqual(received[1][1].interruption_feedback_count, 0)
        self.assertNotIn('多次打断', received[1][0]['extra_info'])
        self.assertFalse(caps.runtime_capabilities_from_stream(stream).reply_controls)
        self.assertEqual(len(outcomes[0][1].reply_set), 1)


if __name__ == '__main__':
    unittest.main()
