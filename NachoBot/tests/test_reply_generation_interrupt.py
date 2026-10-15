import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_realtime_ingress import _message, _provider
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.realtime_ingress import RealtimeIngressManager
from src.chat.focus.models import StoredMessageRef
from src.chat.reply_generation_interrupt import reply_generation_interrupt
from src.chat.reply_generation_interrupt import run_reply_generation
from src.plugin_system.apis import generator_api


class ReplyGenerationInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = RealtimeIngressManager()
        self.manager.register_provider(_provider())
        self.manager_patch = patch('src.chat.reply_generation_interrupt.realtime_ingress_manager', self.manager)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)

    async def authorize(self, source):
        decision = await self.manager.inspect_message(source, source.chat_info_stream_id)
        stored = StoredMessageRef(source.focus_row_id, source.chat_info_stream_id, source.message_id, source.time)
        self.assertTrue(self.manager.record_message(decision, stored))

    def runtime(self, cls, chat_id):
        runtime = object.__new__(cls)
        runtime.stream_id = chat_id
        runtime.chat_stream = SimpleNamespace(stream_id=chat_id)
        runtime.log_prefix = '[synthetic]'
        runtime._planner_interrupt_flag = None
        runtime._planner_interrupt_requested = False
        runtime._planner_interrupt_consecutive_count = 0
        runtime._focus_reply_context = lambda _: None
        runtime._build_low_latency_person_profile_block = AsyncMock(return_value='')
        runtime._send_and_store_reply = AsyncMock()
        return runtime

    async def execute(self, runtime, source):
        action = SimpleNamespace(action_type='reply', action_message=source, reasoning='', action_data={}, reply_text=None)
        return await runtime._execute_action(action, [action], 'synthetic', {}, {})

    async def test_group_and_private_realtime_abort_ongoing_replyer_and_next_flag_is_fresh(self):
        for cls in (HeartFChatting, BrainChatting):
            with self.subTest(runtime=cls.__name__):
                source = _message(turn_id=cls.__name__, row_id=200, chat_id=cls.__name__)
                await self.authorize(source)
                runtime = self.runtime(cls, cls.__name__)
                started = asyncio.Event()
                flags = []

                async def generate(**kwargs):
                    flag = kwargs['interrupt_flag']
                    self.assertIsInstance(flag, asyncio.Event)
                    flags.append(flag)
                    started.set()
                    await flag.wait()
                    return False, None

                with (
                    patch.object(generator_api, 'generate_reply', side_effect=generate),
                    patch('src.chat.brain_chat.brain_chat.advanced_manager.is_on', return_value=False),
                    patch('src.chat.heart_flow.heartFC_chat.injection_manager.build_injection_text', return_value=''),
                    patch('src.chat.heart_flow.heartFC_chat.global_config.chat.planner_interrupt_enabled', False),
                ):
                    task = asyncio.create_task(self.execute(runtime, source))
                    await asyncio.wait_for(started.wait(), 1)
                    runtime.signal_new_message(skip_interrupt=False)
                    self.assertFalse(flags[0].is_set(), 'ordinary messages changed direct Replyer behavior')
                    newer = _message(turn_id=f'{cls.__name__}-new', row_id=201, chat_id=cls.__name__)
                    await self.authorize(newer)
                    runtime.signal_new_message(priority_realtime=True)
                    result = await asyncio.wait_for(task, 1)
                    self.assertFalse(result['success'])
                    runtime._send_and_store_reply.assert_not_awaited()
                    self.assertEqual(runtime._reply_generation_interrupt_flags, [])
                    with reply_generation_interrupt(runtime, newer) as fresh:
                        self.assertFalse(fresh.is_set())
                        self.assertIsNot(fresh, flags[0])

    async def test_stale_turn_is_aborted_before_provider_submission(self):
        source = _message(turn_id='older')
        await self.authorize(source)
        await self.authorize(_message(turn_id='newer', row_id=201))
        runtime = self.runtime(HeartFChatting, source.chat_info_stream_id)
        with reply_generation_interrupt(runtime, source) as flag:
            self.assertTrue(flag.is_set())
        self.assertEqual(runtime._reply_generation_interrupt_flags, [])

    async def test_existing_ordinary_planner_flag_is_preserved_and_realtime_can_preempt_it(self):
        for cls in (HeartFChatting, BrainChatting):
            runtime = self.runtime(cls, 'ordinary')
            original = asyncio.Event()
            ordinary = SimpleNamespace(additional_config={})
            with reply_generation_interrupt(runtime, ordinary, original) as flag:
                self.assertIs(flag, original)
                runtime.signal_new_message(priority_realtime=True)
                self.assertTrue(original.is_set())
            self.assertEqual(runtime._reply_generation_interrupt_flags, [])

    async def test_cancelled_request_does_not_leave_interrupt_flags_for_later_requests(self):
        runtime = self.runtime(HeartFChatting, 'ordinary')
        started = asyncio.Event()

        async def request():
            with reply_generation_interrupt(runtime, SimpleNamespace(additional_config={})):
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(request())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(runtime._reply_generation_interrupt_flags, [])

    async def test_realtime_cancels_prompt_and_auxiliary_tasks_that_ignore_llm_flag(self):
        for cls in (HeartFChatting, BrainChatting):
            source = _message(turn_id=f'prompt-{cls.__name__}', chat_id=cls.__name__)
            await self.authorize(source)
            runtime = self.runtime(cls, cls.__name__)
            preparing = asyncio.Event()
            cleaned = asyncio.Event()

            async def auxiliary():
                try:
                    preparing.set()
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            async def generate(**kwargs):
                await asyncio.gather(auxiliary())
                raise AssertionError('stale prompt reached main model')

            with (
                patch.object(generator_api, 'generate_reply', side_effect=generate),
                patch('src.chat.brain_chat.brain_chat.advanced_manager.is_on', return_value=False),
                patch('src.chat.heart_flow.heartFC_chat.injection_manager.build_injection_text', return_value=''),
            ):
                task = asyncio.create_task(self.execute(runtime, source))
                await asyncio.wait_for(preparing.wait(), 1)
                runtime.signal_new_message(priority_realtime=True)
                self.assertFalse((await asyncio.wait_for(task, 1))['success'])
                self.assertTrue(cleaned.is_set())
                runtime._send_and_store_reply.assert_not_awaited()
                self.assertEqual(runtime._reply_generation_interrupt_flags, [])
                self.assertEqual(runtime._reply_generation_abort_events, [])

    async def test_ordinary_planner_interrupt_does_not_cancel_prompt_preparation(self):
        runtime = self.runtime(HeartFChatting, 'ordinary')
        flag = asyncio.Event()
        runtime._planner_interrupt_flag = flag
        started, finish = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            self.assertIs(kwargs['interrupt_flag'], flag)
            started.set()
            await finish.wait()
            return False, None

        with patch('src.chat.heart_flow.heartFC_chat.global_config.chat.planner_interrupt_enabled', True):
            task = asyncio.create_task(run_reply_generation(
                runtime, SimpleNamespace(additional_config={}), flag, generate))
            await started.wait()
            runtime.signal_new_message()
            self.assertTrue(flag.is_set())
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            self.assertEqual(await task, (False, None))
        self.assertEqual(runtime._reply_generation_abort_events, [])

    async def test_resistant_late_success_is_bounded_and_context_is_released_once(self):
        runtime = self.runtime(HeartFChatting, 'ordinary')
        started, finished, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.35)
                finished.set()
                return True, SimpleNamespace(context_refs=['opaque-ref'])

        async def release(*args):
            released.set()

        with patch('src.chat.reply_generation_interrupt.release_reply_context', side_effect=release) as cleanup:
            task = asyncio.create_task(run_reply_generation(
                runtime, SimpleNamespace(additional_config={}), None, generate))
            await started.wait()
            runtime.signal_new_message(priority_realtime=True)
            self.assertEqual(await asyncio.wait_for(task, 0.32), (False, None))
            self.assertFalse(finished.is_set())
            await asyncio.wait_for(released.wait(), 1)
            cleanup.assert_awaited_once_with(['opaque-ref'], 'generation_cancelled')
        self.assertEqual(runtime._reply_generation_interrupt_flags, [])
        self.assertEqual(runtime._reply_generation_abort_events, [])
