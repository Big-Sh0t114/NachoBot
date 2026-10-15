from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.focus.models import StoredMessageRef
from src.chat.realtime_ingress import RealtimeIngressManager, RealtimeProvider
from src.chat.replyer.group_generator import DefaultReplyer
from src.chat.runtime_capabilities import RuntimeCapabilities
from src.plugins.built_in.tts_plugin.plugin import TTSAction
from src.chat.utils.capability_router import CapabilityDecision
from src.common.data_models.database_data_model import DatabaseMessages
from src.mcp.types import MCPAccessContext
import src.chat.replyer.group_generator as group_generator_module


CHAT_ID = "actorless-group-chat"
GROUP_ID = "actorless-group"


async def _verified_speaker_row() -> DatabaseMessages:
    additional_config = {
        "realtime_session": {
            "version": 1,
            "provider": "synthetic",
            "session_id": "opaque-session",
            "turn_id": "turn-1",
        },
        "runtime_capabilities": {
            "schema_version": 1,
            "planner_bypass": True,
            "reply_delivery": "tts_text",
            "voice_stream": True,
        },
        "realtime_speaker": {
            "version": 1,
            "speaker_ref": "voice-cluster-a",
            "label": "匿名说话人甲",
        },
    }
    group_info = SimpleNamespace(group_id=GROUP_ID, group_name="Synthetic group")
    message_info = SimpleNamespace(
        platform="synthetic",
        message_id="wire-actorless-1",
        user_info=None,
        group_info=group_info,
        additional_config=additional_config,
    )
    incoming = SimpleNamespace(
        message_id="wire-actorless-1",
        message_info=message_info,
        additional_config=additional_config,
        message_segment=SimpleNamespace(type="voice"),
        is_voice=True,
        user_info=None,
        group_info=group_info,
        processed_plain_text="请加入群语音",
        time=100.0,
    )
    manager = RealtimeIngressManager()
    manager.register_provider(RealtimeProvider("synthetic", lambda _message, _chat, _session: True))

    decision = await manager.inspect_message(incoming, CHAT_ID)
    if not decision.authorized:
        raise AssertionError("test provider must authorize the synthetic voice row")
    if not manager.record_message(
        decision,
        StoredMessageRef(501, CHAT_ID, incoming.message_id, incoming.time),
    ):
        raise AssertionError("test row must be recorded in the pending ledger")
    row = DatabaseMessages(
        message_id=incoming.message_id,
        time=incoming.time,
        chat_id=CHAT_ID,
        processed_plain_text=incoming.processed_plain_text,
        display_message=incoming.processed_plain_text,
        additional_config=json.dumps(incoming.additional_config, ensure_ascii=False),
        chat_info_stream_id=CHAT_ID,
        chat_info_platform="synthetic",
        chat_info_group_id=GROUP_ID,
        chat_info_group_name="Synthetic group",
        chat_info_group_platform="synthetic",
    )
    row.id = 501
    if not await manager.revalidate_message(row, CHAT_ID):
        raise AssertionError("test row must pass the immediate provider revalidation")
    if not manager.is_authorized_anonymous_speaker_message(row):
        raise AssertionError("test row must have a current exact speaker proof")
    return row


class ActorlessReplyerTests(unittest.IsolatedAsyncioTestCase):
    def _replyer_with_cached_admin_context(self) -> DefaultReplyer:
        admin = SimpleNamespace(user_id="cached-admin", user_nickname="Cached Admin", platform="qq")
        prior_message_info = SimpleNamespace(
            message_id="prior-admin-message",
            sender_info=admin,
            user_info=admin,
            additional_config={"disable_tools": True},
        )
        stream = SimpleNamespace(
            stream_id=CHAT_ID,
            platform="qq",
            user_info=admin,
            group_info=SimpleNamespace(group_id=GROUP_ID, group_name="Synthetic group"),
            context=SimpleNamespace(message=SimpleNamespace(message_info=prior_message_info)),
        )
        replyer = object.__new__(DefaultReplyer)
        replyer.chat_stream = stream
        replyer.request_type = "replyer"
        return replyer

    async def test_verified_speaker_prompt_stays_actorless_despite_cached_admin_stream(self):
        row = await _verified_speaker_row()
        replyer = self._replyer_with_cached_admin_context()
        replyer.build_expression_habits = AsyncMock(return_value=("", []))
        replyer.build_relation_info = AsyncMock(return_value="")
        replyer.build_tool_info = AsyncMock(return_value="")
        replyer.get_prompt_info = AsyncMock(return_value="")
        replyer.build_actions_prompt = AsyncMock(return_value="")
        replyer.build_personality_prompt = AsyncMock(return_value="")
        replyer._build_mid_term_memory_block = AsyncMock(return_value="")
        replyer.build_keywords_reaction_prompt = AsyncMock(return_value="")
        replyer.build_split_chat_history_prompts = Mock(return_value=("", ""))
        person_probe = Mock()

        async def exercise() -> None:
            with (
                patch.object(group_generator_module.global_config.chat, "get_max_context_size", return_value=30),
                patch.object(group_generator_module.global_config.mood, "enable_mood", False),
                patch.object(group_generator_module.global_config.bot, "qq_account", "bot-account"),
                patch.object(group_generator_module.global_config.bot, "platform", "qq"),
                patch.object(group_generator_module.global_config.bot, "nickname", "NachoBot"),
                patch.object(group_generator_module.global_config.personality, "reply_style", "自然"),
                patch.object(group_generator_module.global_config.personality, "gift_reaction_prompt", ""),
                patch.object(group_generator_module, "get_latest_session_name", return_value="session"),
                patch.object(group_generator_module, "get_stepped_limit", return_value=10),
                patch.object(group_generator_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]),
                patch.object(group_generator_module, "build_readable_messages", return_value=""),
                patch.object(group_generator_module, "build_memory_retrieval_prompt", new=AsyncMock(return_value="")),
                patch.object(group_generator_module, "replace_user_references", side_effect=lambda text, *_a, **_k: text),
                patch.object(group_generator_module, "guard_user_content", side_effect=lambda text, *_a: (text, False, [])),
                patch.object(group_generator_module, "Person", new=person_probe),
                patch.object(group_generator_module.global_prompt_manager, "format_prompt", new=AsyncMock(return_value="prompt")) as formatter,
                patch.object(group_generator_module, "runtime_capabilities_from_stream",
                             return_value=RuntimeCapabilities(reply_delivery="tts_text")),
                patch.object(TTSAction, "get_language_prompt_for_chat",
                             new=AsyncMock(return_value="VOICE_LANGUAGE_FROM_CURRENT_CONFIG")) as language,
                patch("src.chat.offline_recovery.offline_recovery_manager") as recovery_manager,
            ):
                recovery_manager.filter_pending_recovery_messages.side_effect = lambda _chat_id, messages: list(messages)
                await replyer.build_prompt_reply_context(reply_message=row)
                language.assert_awaited_once_with(chat_id=CHAT_ID, target_id=GROUP_ID,
                                                direct_reply=True, text_lang=None)
                self.assertIn("VOICE_LANGUAGE_FROM_CURRENT_CONFIG", formatter.await_args.kwargs["extra_info_block"])

        await exercise()

        replyer.build_tool_info.assert_awaited_once()
        tool_call = replyer.build_tool_info.await_args
        self.assertEqual(tool_call.args[1], "")
        self.assertEqual(tool_call.kwargs["user_id"], "")
        self.assertTrue(tool_call.kwargs["actorless_anonymous"])
        self.assertEqual(tool_call.kwargs["sandbox_source_message_id"], row.message_id)
        replyer.build_relation_info.assert_awaited_once()
        self.assertEqual(replyer.build_relation_info.await_args.args[1], "")
        person_probe.assert_not_called()

    async def test_actorless_tool_context_skips_legacy_admin_stream_but_keeps_neutral_mcp_and_web(self):
        replyer = self._replyer_with_cached_admin_context()
        replyer.url_fetcher = SimpleNamespace(build_url_info=AsyncMock(return_value=""))
        replyer.web_search_manager = SimpleNamespace(is_available=True)
        legacy_actor_reads = []

        async def legacy_tool_call(**_kwargs):
            # The production ToolExecutor retains and injects this stream into
            # legacy plugin tools. Calling it would silently select the prior
            # admin even though the current row has no platform identity.
            legacy_actor_reads.append(replyer.tool_executor.chat_stream.user_info.user_id)
            return ([{"content": "legacy tool result"}], [], "")

        replyer.tool_executor = SimpleNamespace(
            chat_stream=replyer.chat_stream,
            execute_from_chat_message=AsyncMock(side_effect=legacy_tool_call),
        )
        seen_access_contexts = []

        def tool_catalog(*, access_context):
            seen_access_contexts.append(access_context)
            if access_context.is_admin:
                return "admin-only tool"
            if access_context.is_group and not access_context.user_id:
                return "group-neutral tool"
            return ""

        replyer.mcp_executor = SimpleNamespace(get_tool_catalog_summary=Mock(side_effect=tool_catalog))
        replyer.capability_router = SimpleNamespace(decide=AsyncMock(return_value=CapabilityDecision()))
        admin_access = MCPAccessContext(
            user_id="cached-admin",
            chat_id=GROUP_ID,
            is_group=True,
            is_admin=True,
        )
        runtime_caps = RuntimeCapabilities()
        edit_config = SimpleNamespace(model_list=["test-model"])
        mcp_executor_call = AsyncMock(return_value=([{"content": "group-safe MCP result"}], [], ""))

        async def exercise() -> None:
            with (
                patch.object(group_generator_module, "runtime_capabilities_from_stream", return_value=runtime_caps),
                patch.object(group_generator_module, "model_config", SimpleNamespace(model_task_config=SimpleNamespace(file_edit=edit_config))),
                patch.object(group_generator_module, "access_context_from_stream", return_value=admin_access) as access_context,
                patch.object(group_generator_module, "sandbox_user_allowed", return_value=True) as sandbox_allowed,
                patch.object(group_generator_module, "build_search_after_decision", new=AsyncMock(return_value=None)),
                patch.object(group_generator_module, "execute_mcp_after_decision", new=mcp_executor_call),
            ):
                anon_result = await replyer.build_tool_info(
                    chat_history="",
                    sender="",
                    target="请加入群语音",
                    user_id="",
                    sandbox_source_message_id="wire-actorless-1",
                    sandbox_group_id=GROUP_ID,
                    actorless_anonymous=True,
                )
                access_context.assert_not_called()
                sandbox_allowed.assert_not_called()
                self.assertTrue(replyer.capability_router.decide.await_args.kwargs["allow_mcp"])
                self.assertFalse(replyer.capability_router.decide.await_args.kwargs["allow_sandbox_edit"])
                self.assertFalse(replyer.capability_router.decide.await_args.kwargs["sandbox_edit_available"])
                self.assertEqual(replyer.capability_router.decide.await_args.kwargs["sandbox_actor_id"], "")
                self.assertTrue(replyer.capability_router.decide.await_args.kwargs["allow_web_search"])
                self.assertIn("group-safe MCP result", str(anon_result))
                self.assertEqual(
                    seen_access_contexts[-1],
                    MCPAccessContext(user_id="", chat_id=GROUP_ID, is_group=True, is_admin=False),
                )
                self.assertEqual(replyer.tool_executor.execute_from_chat_message.await_count, 0)
                self.assertEqual(legacy_actor_reads, [])
                self.assertEqual(mcp_executor_call.await_count, 1)
                self.assertEqual(
                    mcp_executor_call.await_args.kwargs["access_context"],
                    MCPAccessContext(user_id="", chat_id=GROUP_ID, is_group=True, is_admin=False),
                )

                replyer.chat_stream.context.message.message_info.additional_config = {}
                replyer.capability_router.decide.reset_mock()
                await replyer.build_tool_info(
                    chat_history="",
                    sender="Cached Admin",
                    target="请加入群语音",
                    user_id="",
                    actorless_anonymous=False,
                )
                access_context.assert_called_once_with(replyer.chat_stream, "")
                sandbox_allowed.assert_called_once_with("cached-admin")
                self.assertEqual(seen_access_contexts[-1], admin_access)
                self.assertTrue(replyer.capability_router.decide.await_args.kwargs["allow_mcp"])
                self.assertTrue(replyer.capability_router.decide.await_args.kwargs["allow_sandbox_edit"])
                self.assertEqual(replyer.capability_router.decide.await_args.kwargs["sandbox_actor_id"], "cached-admin")
                self.assertTrue(replyer.capability_router.decide.await_args.kwargs["allow_web_search"])
                self.assertEqual(replyer.tool_executor.execute_from_chat_message.await_count, 1)
                self.assertEqual(legacy_actor_reads, ["cached-admin"])
                self.assertEqual(mcp_executor_call.await_count, 2)

        await exercise()


if __name__ == "__main__":
    unittest.main()
