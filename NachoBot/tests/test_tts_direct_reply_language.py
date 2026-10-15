import unittest
from unittest.mock import patch

from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401 - prime Core imports
from src.plugins.built_in.tts_plugin.plugin import TTSAction


class DirectReplyLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_reply_uses_current_preference_without_action_only_exemption(self):
        key = TTSAction.build_language_key('group', 'chat')
        for language, label in (('ja', '日文'), ('zh', '中文')):
            with self.subTest(language=language), patch.object(TTSAction, '_language_preferences', {key:language}):
                prompt = await TTSAction.get_language_prompt_for_chat('chat', 'group', direct_reply=True)
                self.assertIn(label, prompt)
                self.assertIn('reply正文会直接合成为语音', prompt)
                self.assertNotIn('普通 reply、其他 action', prompt)
                ordinary = await TTSAction.get_language_prompt_for_chat('chat', 'group')
                self.assertIn('此约束仅适用于 tts_action', ordinary)

    async def test_explicit_one_reply_override_does_not_replace_chat_preference(self):
        key = TTSAction.build_language_key('group', 'chat')
        with patch.object(TTSAction, '_language_preferences', {key:'ja'}):
            temporary = await TTSAction.get_language_prompt_for_chat('chat', 'group', direct_reply=True, text_lang='zh')
            self.assertIn('只使用中文', temporary)
            self.assertEqual(TTSAction._language_preferences, {key:'ja'})
            following = await TTSAction.get_language_prompt_for_chat('chat', 'group', direct_reply=True)
            self.assertIn('只使用日文', following)

    async def test_unsupported_override_preserves_configured_language(self):
        key = TTSAction.build_language_key('group', 'chat')
        with patch.object(TTSAction, '_language_preferences', {key:'ja'}):
            prompt = await TTSAction.get_language_prompt_for_chat('chat', 'group', direct_reply=True, text_lang='fr')
            self.assertIn('只使用日文', prompt)
            self.assertEqual(TTSAction._language_preferences, {key:'ja'})
