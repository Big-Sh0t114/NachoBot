from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from plugins.owner_auth_plugin import plugin
from src.chat.replyer.prompt_build_result import ReplyPromptBuildResult


class OwnerAuthPromptPatchTests(unittest.TestCase):
    def setUp(self):
        self._original_method = plugin._original_build_prompt_reply_context
        self._patch_applied = plugin._patch_applied
        plugin._global_auth_cache.clear()

    def tearDown(self):
        plugin._global_auth_cache.clear()
        plugin._original_build_prompt_reply_context = self._original_method
        plugin._patch_applied = self._patch_applied

    def _apply_to(self, replyer_cls):
        importer = patch.object(plugin, "_import_default_replyer", return_value=replyer_cls)
        importer.start()
        self.addCleanup(importer.stop)
        plugin._original_build_prompt_reply_context = None
        plugin._patch_applied = False
        plugin.patch_build_prompt_reply_context()

    def test_typed_result_enhancement_preserves_metadata_identity(self):
        candidate = object()
        selected_expressions = [1, 3]
        base_result = ReplyPromptBuildResult("base prompt", selected_expressions, candidate)
        calls = []

        class FakeReplyer:
            async def build_prompt_reply_context(
                self,
                *,
                extra_info="",
                reply_reason="",
                available_actions=None,
                chosen_actions=None,
                enable_tool=True,
                reply_message=None,
                prompt_context=None,
            ):
                calls.append(chosen_actions)
                return base_result

        self._apply_to(FakeReplyer)
        plugin.store_auth_info("42", True, "已验证", "Alice")

        result = asyncio.run(
            FakeReplyer().build_prompt_reply_context(
                reply_reason="Alice: hello",
                chosen_actions=[{"action": "reply"}],
                reply_message={"user_id": "42"},
            )
        )

        self.assertIsInstance(result, ReplyPromptBuildResult)
        self.assertIn("【确认主人身份】", result.prompt)
        self.assertTrue(result.prompt.endswith("base prompt"))
        self.assertIs(result.selected_expressions, selected_expressions)
        self.assertIs(result.sandbox_candidate, candidate)
        self.assertEqual(calls, [[{"action": "reply"}]])

    def test_typed_result_without_auth_is_returned_unchanged(self):
        candidate = object()
        base_result = ReplyPromptBuildResult("base prompt", [2], candidate)

        class FakeReplyer:
            async def build_prompt_reply_context(self, **kwargs):
                return base_result

        self._apply_to(FakeReplyer)

        result = asyncio.run(FakeReplyer().build_prompt_reply_context(reply_message={"user_id": "42"}))

        self.assertIs(result, base_result)

    def test_legacy_tuple_is_enhanced_and_stays_tuple_shaped(self):
        selected_expressions = [5]

        class FakeReplyer:
            async def build_prompt_reply_context(
                self,
                *,
                extra_info="",
                reply_reason="",
                available_actions=None,
                choosen_actions=None,
                enable_tool=True,
                reply_message=None,
                prompt_context=None,
            ):
                return "base prompt", selected_expressions

        self._apply_to(FakeReplyer)
        plugin.store_auth_info("42", True, "已验证", "Alice")

        result = asyncio.run(
            FakeReplyer().build_prompt_reply_context(
                reply_message={"user_id": "42"},
                chosen_actions=[{"action": "reply"}],
            )
        )

        self.assertIs(type(result), tuple)
        self.assertIn("【确认主人身份】", result[0])
        self.assertIs(result[1], selected_expressions)

    def test_unsupported_result_shape_fails_closed(self):
        class FakeReplyer:
            async def build_prompt_reply_context(self, **kwargs):
                return {"prompt": "base prompt"}

        self._apply_to(FakeReplyer)

        with self.assertRaisesRegex(TypeError, r"unsupported result shape dict"):
            asyncio.run(FakeReplyer().build_prompt_reply_context())

    def test_patch_application_and_removal_are_idempotent(self):
        original_method = None

        class FakeReplyer:
            async def build_prompt_reply_context(self, **kwargs):
                return "base prompt", []

        original_method = FakeReplyer.build_prompt_reply_context
        self._apply_to(FakeReplyer)
        patched_method = FakeReplyer.build_prompt_reply_context

        plugin.patch_build_prompt_reply_context()
        self.assertIs(FakeReplyer.build_prompt_reply_context, patched_method)
        self.assertTrue(plugin.is_patch_applied())

        with patch.object(plugin, "_import_default_replyer", return_value=FakeReplyer):
            self.assertTrue(plugin.remove_owner_auth_patch())
            self.assertFalse(plugin.remove_owner_auth_patch())
        self.assertIs(FakeReplyer.build_prompt_reply_context, original_method)


if __name__ == "__main__":
    unittest.main()
