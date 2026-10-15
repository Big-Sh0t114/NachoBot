"""Voice lifecycle, isolation, interruption feedback and HTTP contract tests."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_calls import VoiceCallStore, VoiceCallError, canonical_core_user_id
from chat_backend import LocalChatBackend
from voice_routes import create_voice_router


class VoiceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'calls.sqlite3'
        self.store = VoiceCallStore(self.path)
        self.call = self.store.create_call('conversation-a', 'Tester')

    def tearDown(self):
        self.directory.cleanup()

    def pending(self, generation):
        request_id = f'request-{generation}'
        self.store.begin_request(request_id, 'conversation-a', canonical_core_user_id('conversation-a'),
                                 'voice', self.call['id'], generation)
        self.store.finish_request(request_id, 'accepted')
        return request_id

    def test_idle_speech_and_retries_do_not_count(self):
        for generation in range(5):
            self.store.interrupt(self.call['id'], generation)
        self.assertIsNone(self.store.peek_interrupt_feedback(self.call['id']))
        self.pending(5)
        self.assertEqual(self.store.interrupt(self.call['id'], 5), 6)
        for _ in range(5):
            self.assertEqual(self.store.interrupt(self.call['id'], 5), 6)
        self.assertIsNone(self.store.peek_interrupt_feedback(self.call['id']))

    def test_feedback_is_temporary_scoped_and_consumed_once(self):
        for generation in range(3):
            self.pending(generation)
            self.store.interrupt(self.call['id'], generation)
        feedback = self.store.peek_interrupt_feedback(self.call['id'])
        self.assertEqual(feedback['count'], 3)
        self.assertEqual(self.store.peek_interrupt_feedback(self.call['id']), feedback)
        second = self.store.create_call('conversation-b', 'Tester')
        self.assertIsNone(self.store.peek_interrupt_feedback(second['id']))
        self.store.consume_interrupt_feedback(self.call['id'], feedback['token'])
        self.store.consume_interrupt_feedback(self.call['id'], feedback['token'])
        self.assertIsNone(self.store.peek_interrupt_feedback(self.call['id']))
        self.pending(3)
        self.store.interrupt(self.call['id'], 3)
        self.assertIsNone(self.store.peek_interrupt_feedback(self.call['id']))
        self.store.end_call(self.call['id'])
        self.assertNotIn(self.call['id'], self.store._interruptions)

    def test_played_reply_not_counted_late_reply_retained(self):
        request_id = self.pending(0)
        row = self.store.add_message(self.call['id'], role='assistant', content='hello', generation=0,
                                     request_message_id=request_id, core_message_id='core-1')
        self.store.settle_playback(self.call['id'], row['id'], 0, 'played')
        self.store.interrupt(self.call['id'], 0)
        self.assertNotIn(self.call['id'], self.store._interruptions)
        late = self.store.add_message(self.call['id'], role='assistant', content='late', generation=0,
                                     request_message_id=request_id, core_message_id='core-2', require_active=False)
        self.assertTrue(late['interrupted'])
        self.store.recover_after_restart()
        reopened = VoiceCallStore(self.path).get_call(self.call['id'])
        self.assertEqual(reopened['status'], 'ended')
        self.assertEqual(len(reopened['messages']), 2)
        self.assertFalse(reopened['messages'][0]['interrupted'])

    def test_durable_route_isolates_channels_and_late_replies(self):
        backend = LocalChatBackend()
        backend.voice_store = self.store
        self.pending(0)
        def reply(user='webui_conversation-a', core_id='core-a'):
            return {'message_info': {'user_info': {'user_id': user}, 'message_id': core_id,
                                    'additional_config': {'reply_to_message_id': 'request-0'}},
                    'message_segment': {'type': 'text', 'data': 'hello'}}
        self.assertIsNone(backend._make_reply_event(reply('webui_conversation-b')))
        event = backend._make_reply_event(reply())
        self.assertEqual(event['channel'], 'voice')
        self.assertEqual(event['call_id'], self.call['id'])
        self.assertIsNone(backend._make_reply_event(reply()))
        self.store.end_call(self.call['id'])
        self.assertIsNone(backend._make_reply_event(reply(core_id='core-late')))
        self.assertEqual(len(self.store.get_call(self.call['id'])['messages']), 2)

    def test_canonical_identity_does_not_collapse_ids(self):
        self.assertNotEqual(canonical_core_user_id('a-b'), canonical_core_user_id('ab'))
        with self.assertRaises(VoiceCallError):
            canonical_core_user_id('a/b')
        with self.assertRaises(VoiceCallError):
            self.store.create_call('conversation-a', 'Tester')


class WebUIRoutingPayloadTests(unittest.TestCase):
    def _payload(self, *, channel, voice_reply_controls=False, interrupt_feedback=None):
        return LocalChatBackend._build_incoming_message(
            message_id='request-1',
            conversation_id='conversation-a',
            text='hello',
            user_id='webui_conversation-a',
            user_name='Tester',
            channel=channel,
            call_id='call-1' if channel == 'voice' else None,
            generation=3 if channel == 'voice' else 0,
            voice_reply_controls=voice_reply_controls,
            control_emotions=('normal', 'happy'),
            control_actions=('待机', '放松'),
            interrupt_feedback=interrupt_feedback,
        )

    def test_voice_bypasses_planner_with_live2d_controls_disabled_or_enabled(self):
        plain_voice = self._payload(channel='voice')
        controlled_voice = self._payload(channel='voice', voice_reply_controls=True)

        for payload in (plain_voice, controlled_voice):
            additional = payload['message_info']['additional_config']
            self.assertEqual(additional['webui_channel'], 'voice')
            self.assertTrue(additional['runtime_capabilities']['planner_bypass'])
            self.assertEqual(additional['webui_call_id'], 'call-1')
            self.assertEqual(additional['webui_generation'], 3)
        self.assertNotIn('reply_controls', plain_voice['message_info']['additional_config']['runtime_capabilities'])
        self.assertTrue(
            controlled_voice['message_info']['additional_config']['runtime_capabilities']['reply_controls']
        )
        self.assertEqual(
            controlled_voice['message_info']['additional_config']['runtime_capabilities']['control_emotions'],
            ['normal', 'happy'],
        )
        self.assertEqual(
            controlled_voice['message_info']['additional_config']['runtime_capabilities']['control_actions'],
            ['待机', '放松'],
        )

    def test_interrupt_feedback_is_carried_as_a_generic_capability(self):
        payload = self._payload(channel='voice', interrupt_feedback={'count': 5})
        runtime = payload['message_info']['additional_config']['runtime_capabilities']
        self.assertEqual(runtime['interruption_feedback'], {'count': 5})
        self.assertNotIn('webui_interrupt_feedback', payload['message_info']['additional_config'])

    def test_text_stays_on_planner_route_in_same_conversation(self):
        voice = self._payload(channel='voice')
        text = self._payload(channel='text')
        voice_config = voice['message_info']['additional_config']
        text_config = text['message_info']['additional_config']

        self.assertFalse(text_config['runtime_capabilities']['planner_bypass'])
        self.assertEqual(text_config['webui_channel'], 'text')
        self.assertEqual(text_config['conversation_id'], voice_config['conversation_id'])
        self.assertEqual(text['message_info']['user_info']['user_id'], voice['message_info']['user_info']['user_id'])


class VoiceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = VoiceCallStore(Path(self.directory.name) / 'calls.sqlite3')
        self.tts = SimpleNamespace(status=AsyncMock(return_value={'ready': True}))
        self.backend = SimpleNamespace(send_message=AsyncMock(return_value={'status': 'accepted'}))
        self.proxy = SimpleNamespace(abort_call=AsyncMock(), abort_stream=AsyncMock())
        app = FastAPI()
        app.include_router(create_voice_router(self.store, self.backend, self.tts, audio_stream_proxy=self.proxy))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.directory.cleanup()

    async def test_tts_gate_and_typed_call(self):
        self.tts.status.return_value = {'ready': False}
        response = await self.client.post('/api/chat/calls', json={'conversation_id': 'a'})
        self.assertEqual(response.status_code, 503)
        self.tts.status.return_value = {'ready': True}
        response = await self.client.post('/api/chat/calls', json={'conversation_id': 'a'})
        self.assertEqual(response.status_code, 200)
        call = response.json()
        result = await self.client.post(f"/api/chat/calls/{call['id']}/message", json={
            'message': 'typed voice input', 'request_message_id': 'r1', 'generation': 0})
        self.assertEqual(result.status_code, 200, result.text)
        kwargs = self.backend.send_message.call_args.kwargs
        self.assertEqual(kwargs['user_id'], 'webui_a')
        self.assertEqual(kwargs['channel'], 'voice')
        self.tts.status.assert_called_with(strict=True)
        self.assertEqual(len(self.store.get_call(call['id'])['messages']), 1)
        await self.client.post(f"/api/chat/calls/{call['id']}/interrupt", json={'generation': 0})
        stale = await self.client.post(f"/api/chat/calls/{call['id']}/message", json={
            'message': 'old', 'request_message_id': 'r2', 'generation': 0})
        self.assertEqual(stale.status_code, 409)
        isolated = await self.client.get('/api/chat/calls?conversation_id=b')
        self.assertEqual(isolated.json()['calls'], [])


if __name__ == '__main__':
    unittest.main()
