from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins.qqcall_plugin import realtime_provider
from plugins.qqcall_plugin.plugin import QQCallPlugin
from src.chat.focus.models import StoredMessageRef
from src.chat.realtime_ingress import (
    RealtimeIngressManager,
    RealtimeProvider,
    RealtimeSession,
    realtime_ingress_manager,
)
from src.common.data_models.database_data_model import DatabaseMessages


_SCOPE = {
    "serviceInstance": "01234567-89ab-4cde-8fab-0123456789ab",
    "hostGeneration": 2,
    "connectionGeneration": 3,
    "callId": "call-42",
    "callGeneration": 4,
}
_CHAT_ID = "qq-private-42"
_PEER = "42"
_TURN_ID = "turn-42"
_SESSION_ID = hashlib.sha256(json.dumps(_SCOPE, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _voice_message(*, row_id: int = 200, chat_id: str = _CHAT_ID, platform: str = "qq"):
    config = {
        "qqcall_scope": dict(_SCOPE),
        "peer_user_id": _PEER,
        "delivery_target": {
            "kind": "qqcall",
            **_SCOPE,
            "peer_user_id": _PEER,
            "turn_id": _TURN_ID,
        },
        "realtime_session": {
            "version": 1,
            "provider": "qqcall",
            "session_id": _SESSION_ID,
            "turn_id": _TURN_ID,
        },
        "runtime_capabilities": {
            "schema_version": 1,
            "reply_delivery": "tts_text",
            "voice_stream": True,
            "planner_bypass": True,
        },
    }
    user = SimpleNamespace(user_id=_PEER, user_nickname="Caller")
    message_id = f"qqcall:voice:{_SCOPE['callId']}:{_SCOPE['callGeneration']}:{_TURN_ID}"
    return SimpleNamespace(
        message_id=message_id,
        time=100.0,
        processed_plain_text="hello from the call",
        display_message="hello from the call",
        user_info=user,
        message_info=SimpleNamespace(
            platform=platform,
            message_id=message_id,
            user_info=user,
            group_info=None,
            additional_config=config,
        ),
        additional_config=config,
        group_info=None,
        chat_info_group_id=None,
        chat_info_stream_id=chat_id,
        chat_info_platform=platform,
        focus_row_id=row_id,
    )


def _database_voice_message(
    *,
    row_id: int = 200,
    chat_id: str = _CHAT_ID,
    platform: str = "qq",
    message_id: str | None = None,
    config: dict | None = None,
):
    live = _voice_message(row_id=row_id, chat_id=chat_id)
    message_config = dict(live.additional_config if config is None else config)
    message_id = live.message_id if message_id is None else message_id
    stored = DatabaseMessages(
        message_id=message_id,
        time=live.time,
        chat_id=chat_id,
        processed_plain_text=live.processed_plain_text,
        display_message=live.display_message,
        additional_config=json.dumps(message_config),
        user_id=_PEER,
        user_nickname="Caller",
        user_platform=platform,
        chat_info_stream_id=chat_id,
        chat_info_platform=platform,
    )
    stored.focus_row_id = row_id
    return stored


def _status(
    *,
    peer: str = _PEER,
    scope=None,
    call_id: str | None = None,
    audio_turn_id: str | None = _TURN_ID,
):
    return {
        "peer_user_id": peer,
        "scope": dict(_SCOPE if scope is None else scope),
        "call": {
            "phase": "connected",
            "uncertain": False,
            "callId": _SCOPE["callId"] if call_id is None else call_id,
            "generation": _SCOPE["callGeneration"],
            "audioTurnId": audio_turn_id,
        },
    }


class QQCallRealtimeProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_enabled_plugin_load_registers_provider_and_disabled_state_removes_it(self):
        from plugins.qqcall_plugin import plugin as plugin_module

        config = {
            "enabled": True,
            "core": {
                "allow_planner_accept": False,
                "group_intent_model_task": "default",
                "group_intent_timeout_seconds": 5,
            },
        }
        plugin_dir = "plugins/qqcall_plugin"
        try:
            with (
                patch.object(plugin_module, "read_qqcall_public_config", return_value=config),
                patch.object(plugin_module, "_core_qqcall_enabled", return_value=True),
                patch.object(realtime_provider, "qqcall_enabled", return_value=True),
            ):
                plugin = QQCallPlugin(plugin_dir)
                plugin.get_plugin_components()
                self.assertIn("qqcall", realtime_ingress_manager._providers)
                provider = realtime_ingress_manager._providers["qqcall"]
                self.assertIs(provider.verify, realtime_provider.verify_qqcall_realtime_message)
                self.assertIs(provider.lifecycle, realtime_provider.verify_qqcall_lifecycle)
                self.assertIs(provider.enabled, realtime_provider.qqcall_enabled)

            disabled_config = {**config, "enabled": False}
            with (
                patch.object(plugin_module, "read_qqcall_public_config", return_value=disabled_config),
                patch.object(plugin_module, "_core_qqcall_enabled", return_value=False),
            ):
                QQCallPlugin(plugin_dir).get_plugin_components()
            self.assertNotIn("qqcall", realtime_ingress_manager._providers)
        finally:
            realtime_ingress_manager.unregister_provider("qqcall")

    async def test_voice_declaration_matches_qq_scope_peer_and_current_call(self):
        message = _voice_message()
        status = AsyncMock(return_value=_status())
        with (
            patch.object(realtime_provider, "qqcall_enabled", return_value=True),
            patch.object(realtime_provider, "request_qqcall", new=status),
            patch(
                "src.chat.message_receive.chat_stream.get_chat_manager",
                return_value=SimpleNamespace(
                    get_stream_id=Mock(return_value=_CHAT_ID)
                ),
            ),
        ):
            self.assertTrue(
                await realtime_provider.verify_qqcall_realtime_message(
                    message,
                    _CHAT_ID,
                    RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
                )
            )
        status.assert_awaited_once_with(
            "status",
            platform="qq",
            peer_user_id=_PEER,
            scope=_SCOPE,
            timeout=2.5,
        )

    async def test_failed_or_stale_current_call_status_is_rejected(self):
        message = _voice_message()
        cases = (
            _status(peer="43"),
            _status(scope={**_SCOPE, "callGeneration": 5}),
            _status(call_id="stale-call"),
            _status(audio_turn_id="old-service-turn"),
            _status(audio_turn_id=None),
            {
                "peer_user_id": _PEER,
                "scope": dict(_SCOPE),
                "call": {"phase": "invite_ready", "uncertain": False},
            },
        )
        for result in cases:
            with (
                self.subTest(result=result),
                patch.object(realtime_provider, "qqcall_enabled", return_value=True),
                patch.object(realtime_provider, "request_qqcall", new=AsyncMock(return_value=result)),
                patch(
                    "src.chat.message_receive.chat_stream.get_chat_manager",
                    return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
                ),
            ):
                self.assertFalse(
                    await realtime_provider.verify_qqcall_realtime_message(
                        message,
                        _CHAT_ID,
                        RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
                    )
                )

    async def test_unavailable_scoped_status_quarantines_declared_row(self):
        manager = RealtimeIngressManager()
        manager.register_provider(
            RealtimeProvider(
                "qqcall",
                realtime_provider.verify_qqcall_realtime_message,
                enabled=lambda: True,
            )
        )
        request = AsyncMock(side_effect=RuntimeError("status unavailable"))
        voice = _voice_message()
        with (
            patch.object(realtime_provider, "qqcall_enabled", return_value=True),
            patch.object(realtime_provider, "request_qqcall", new=request),
            patch(
                "src.chat.message_receive.chat_stream.get_chat_manager",
                return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
            ),
        ):
            decision = await manager.inspect_message(voice, _CHAT_ID)
            self.assertTrue(decision.declared)
            self.assertFalse(decision.authorized)
            self.assertFalse(
                manager.record_message(
                    decision,
                    StoredMessageRef(200, _CHAT_ID, voice.message_id, voice.time),
                )
            )

        request.assert_awaited_once_with(
            "status",
            platform="qq",
            peer_user_id=_PEER,
            scope=_SCOPE,
            timeout=2.5,
        )
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())
        self.assertIn(200, manager.excluded_row_ids_for_chat(_CHAT_ID))
        self.assertEqual(manager.filter_ordinary_messages(_CHAT_ID, [voice]), [])

    async def test_audio_onset_rejects_queued_old_turn_without_ordinary_fallback(self):
        voice = _voice_message()
        runtime_modules = (
            "src.chat.brain_chat.brain_chat",
            "src.chat.heart_flow.heartFC_chat",
        )
        for module_name in runtime_modules:
            module = __import__(module_name, fromlist=["realtime_ingress_manager"])
            runtime_type = module.BrainChatting if module_name.endswith("brain_chat") else module.HeartFChatting
            manager = RealtimeIngressManager()
            manager.register_provider(
                RealtimeProvider(
                    "qqcall",
                    realtime_provider.verify_qqcall_realtime_message,
                    enabled=lambda: True,
                )
            )
            request = AsyncMock(
                side_effect=(
                    _status(audio_turn_id=_TURN_ID),
                    _status(audio_turn_id="replacement-turn"),
                )
            )
            offline_manager = SimpleNamespace(
                include_deferred_live_messages=lambda _chat, messages: messages,
                filter_pending_recovery_messages=lambda _chat, messages: messages,
                filter_recovered_nonfocus=lambda _chat, messages: messages,
                commit_nonfocus_read_cursor=Mock(),
                commit_deferred_live_rows=Mock(),
            )
            loader = Mock(return_value=[voice])
            runtime = object.__new__(runtime_type)
            runtime.stream_id = _CHAT_ID
            runtime.last_read_time = 0.0
            runtime._offline_recovery_batch = None
            runtime._ensure_offline_recovery_state = Mock()
            runtime._observe = AsyncMock(return_value=True)
            runtime.blocked_users = {}
            runtime._filter_blocked_users = Mock(side_effect=lambda rows, **_kwargs: rows)

            with (
                self.subTest(runtime=runtime_type.__name__),
                patch.object(realtime_provider, "qqcall_enabled", return_value=True),
                patch.object(realtime_provider, "request_qqcall", new=request),
                patch(
                    "src.chat.message_receive.chat_stream.get_chat_manager",
                    return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
                ),
                patch.object(module, "realtime_ingress_manager", manager),
                patch.object(module, "offline_recovery_manager", offline_manager),
                patch.object(module, "load_message_rows_by_ids", loader),
                patch.object(module.message_api, "get_messages_by_time_in_chat", return_value=[voice]),
                patch.object(module.asyncio, "sleep", new=AsyncMock()),
            ):
                decision = await manager.inspect_message(voice, _CHAT_ID)
                self.assertTrue(decision.authorized)
                self.assertTrue(
                    manager.record_message(
                        decision,
                        StoredMessageRef(200, _CHAT_ID, voice.message_id, voice.time),
                    )
                )
                self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))

                # The adapter's current turn changes at speech onset. No replacement
                # transcript is submitted before the old row is revalidated.
                await runtime._loopbody()

                self.assertEqual(request.await_count, 2)
                loader.assert_called_once_with(_CHAT_ID, (200,))
                runtime._observe.assert_not_awaited()
                self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())
                self.assertIn(200, manager.excluded_row_ids_for_chat(_CHAT_ID))
                self.assertEqual(manager.filter_ordinary_messages(_CHAT_ID, [voice]), [])

    async def test_persisted_database_voice_rows_revalidate_and_dispatch_by_id(self):
        live_voice = _voice_message()
        database_voice = _database_voice_message()
        altered_message_id = _database_voice_message(
            message_id=f"qqcall:voice:{_SCOPE['callId']}:{_SCOPE['callGeneration']}:old-turn"
        )
        altered_platform = _database_voice_message(platform="discord")
        annotation = RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID)

        runtime_modules = (
            "src.chat.brain_chat.brain_chat",
            "src.chat.heart_flow.heartFC_chat",
        )
        for module_name in runtime_modules:
            module = __import__(module_name, fromlist=["realtime_ingress_manager"])
            runtime_type = module.BrainChatting if module_name.endswith("brain_chat") else module.HeartFChatting
            manager = RealtimeIngressManager()
            manager.register_provider(
                RealtimeProvider(
                    "qqcall",
                    realtime_provider.verify_qqcall_realtime_message,
                    enabled=lambda: True,
                )
            )
            request = AsyncMock(return_value=_status())
            runtime = object.__new__(runtime_type)
            runtime.stream_id = _CHAT_ID
            runtime.last_read_time = 0.0
            runtime._offline_recovery_batch = None
            runtime._ensure_offline_recovery_state = Mock()
            runtime._observe = AsyncMock(return_value=True)
            runtime.blocked_users = {}
            runtime._filter_blocked_users = Mock(side_effect=lambda rows, **_kwargs: rows)
            loader = Mock(return_value=[database_voice])

            with (
                self.subTest(runtime=runtime_type.__name__),
                patch.object(realtime_provider, "qqcall_enabled", return_value=True),
                patch.object(realtime_provider, "request_qqcall", new=request),
                patch(
                    "src.chat.message_receive.chat_stream.get_chat_manager",
                    return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
                ),
                patch.object(module, "realtime_ingress_manager", manager),
                patch.object(module, "load_message_rows_by_ids", loader),
            ):
                decision = await manager.inspect_message(live_voice, _CHAT_ID)
                self.assertTrue(decision.authorized)
                self.assertTrue(
                    manager.record_message(
                        decision,
                        StoredMessageRef(200, _CHAT_ID, live_voice.message_id, live_voice.time),
                    )
                )
                self.assertTrue(
                    await realtime_provider.verify_qqcall_realtime_message(
                        database_voice,
                        _CHAT_ID,
                        annotation,
                    )
                )
                self.assertFalse(
                    await realtime_provider.verify_qqcall_realtime_message(
                        altered_message_id,
                        _CHAT_ID,
                        annotation,
                    )
                )
                self.assertFalse(
                    await realtime_provider.verify_qqcall_realtime_message(
                        altered_platform,
                        _CHAT_ID,
                        annotation,
                    )
                )
                await runtime._loopbody()

                loader.assert_called_once_with(_CHAT_ID, (200,))
                runtime._observe.assert_awaited_once()
                self.assertEqual(runtime._observe.await_args.kwargs["recent_messages_list"], [database_voice])
                self.assertEqual(runtime.last_read_time, 0.0)
                self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())
                self.assertEqual(request.await_count, 3)

    async def test_disabled_plugin_never_queries_adapter(self):
        with (
            patch.object(realtime_provider, "qqcall_enabled", return_value=False),
            patch.object(realtime_provider, "request_qqcall", new=AsyncMock()) as request,
            patch(
                "src.chat.message_receive.chat_stream.get_chat_manager",
                return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
            ),
        ):
            accepted = await realtime_provider.verify_qqcall_realtime_message(
                _voice_message(),
                _CHAT_ID,
                RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
            )
        self.assertFalse(accepted)
        request.assert_not_awaited()

    async def test_plugin_disable_during_scoped_status_invalidates_proof(self):
        message = _voice_message()
        enabled = Mock(side_effect=(True, True, False))
        with (
            patch.object(realtime_provider, "qqcall_enabled", enabled),
            patch.object(realtime_provider, "request_qqcall", new=AsyncMock(return_value=_status())),
            patch(
                "src.chat.message_receive.chat_stream.get_chat_manager",
                return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
            ),
        ):
            self.assertFalse(
                await realtime_provider.verify_qqcall_realtime_message(
                    message,
                    _CHAT_ID,
                    RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
                )
            )
        self.assertEqual(enabled.call_count, 3)

    async def test_scope_annotation_turn_route_and_platform_mismatches_fail_closed(self):
        message = _voice_message()
        self.assertFalse(
            await realtime_provider.verify_qqcall_realtime_message(
                message,
                "another-chat",
                RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
            )
        )
        self.assertFalse(
            await realtime_provider.verify_qqcall_realtime_message(
                message,
                _CHAT_ID,
                RealtimeSession(1, "qqcall", "wrong-binding", _TURN_ID),
            )
        )
        self.assertFalse(
            await realtime_provider.verify_qqcall_realtime_message(
                message,
                _CHAT_ID,
                RealtimeSession(1, "qqcall", _SESSION_ID, "stale-turn"),
            )
        )
        wrong_platform = _voice_message(platform="discord")
        with patch.object(realtime_provider, "qqcall_enabled", return_value=True), patch.object(
            realtime_provider,
            "request_qqcall",
            new=AsyncMock(return_value=_status()),
        ) as request:
            self.assertFalse(
                await realtime_provider.verify_qqcall_realtime_message(
                    wrong_platform,
                    _CHAT_ID,
                    RealtimeSession(1, "qqcall", _SESSION_ID, _TURN_ID),
                )
            )
        request.assert_not_awaited()

    async def test_senderless_lifecycle_event_stays_a_standard_context_event(self):
        data = {
            "call_id": _SCOPE["callId"],
            "call_generation": _SCOPE["callGeneration"],
            "phase": "connected",
            "uncertain": False,
        }
        config = {
            "system_event": {
                "version": 1,
                "type": "qq.call.call_connected",
                "actor": None,
                "target": None,
                "data": data,
            },
            "system_event_route": {
                "platform": "qq",
                "kind": "private",
                "peer": {"user_id": _PEER},
            },
            "qqcall_scope": dict(_SCOPE),
            "peer_user_id": _PEER,
            "delivery_target": {"kind": "qqcall", **_SCOPE, "peer_user_id": _PEER},
        }
        message = SimpleNamespace(
            message_id="qqcall:lifecycle:connected",
            additional_config=config,
            user_info=None,
            group_info=None,
            chat_info_group_id=None,
            message_info=SimpleNamespace(
                platform="qq",
                message_id="qqcall:lifecycle:connected",
                user_info=None,
                group_info=None,
                additional_config=config,
            ),
        )
        with (
            patch.object(realtime_provider, "qqcall_enabled", return_value=True),
            patch.object(realtime_provider, "request_qqcall", new=AsyncMock(return_value=_status())),
            patch(
                "src.chat.message_receive.chat_stream.get_chat_manager",
                return_value=SimpleNamespace(get_stream_id=Mock(return_value=_CHAT_ID)),
            ),
        ):
            change = await realtime_provider.verify_qqcall_lifecycle(message, _CHAT_ID)
        self.assertIsNotNone(change)
        self.assertEqual(change.provider, "qqcall")
        self.assertEqual(change.session_id, _SESSION_ID)
        self.assertEqual(change.chat_id, _CHAT_ID)
        self.assertTrue(change.active)
        self.assertEqual(change.ttl_seconds, 120.0)


if __name__ == "__main__":
    unittest.main()
