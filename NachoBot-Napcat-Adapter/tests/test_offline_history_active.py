from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from websockets.protocol import State

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message import Seg  # noqa: E402
from ncnk_message.offline_history import (  # noqa: E402
    HistoryPageError,
    HistoryTarget,
    OfflineHistoryStore,
    PendingHistoryMessage,
    build_delivery_metadata,
    scoped_delivery_key,
)
from src.config import global_config  # noqa: E402
from src.offline_history import (  # noqa: E402
    BACKEND,
    NapcatOfflineHistory,
    core_connection_identity,
    normalize_napcat_messages,
)
from src.recv_handler.message_handler import MessageHandler, message_handler  # noqa: E402
from src.recv_handler.message_sending import message_send_instance  # noqa: E402


ACCOUNT_ID = "90001"
PLATFORM = "qq"
GROUP = HistoryTarget("group", "77001")
PRIVATE = HistoryTarget("private", "84")


def _history_row(message_id: int, sequence: int, timestamp: float, *, group_id: int = 77001):
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": int(ACCOUNT_ID),
        "message_id": message_id,
        "message_seq": sequence,
        "time": timestamp,
        "group_id": group_id,
        "sender": {"user_id": 84, "nickname": "sender"},
        "message": [{"type": "text", "data": {"text": f"message-{message_id}"}}],
        "raw_message": f"message-{message_id}",
    }


def _private_history_row(message_id: int, sequence: int, timestamp: float, *, sender_id: int = 84):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": int(ACCOUNT_ID),
        "message_id": message_id,
        "message_seq": sequence,
        "time": timestamp,
        "sender": {"user_id": sender_id, "nickname": "sender"},
        "message": [{"type": "text", "data": {"text": f"message-{message_id}"}}],
        "raw_message": f"message-{message_id}",
    }


def _temporary_private_history_row(
    message_id: int,
    sequence: int,
    timestamp: float,
    *,
    sender_id: int,
    group_id: int = 77002,
    target_id: int | None = None,
):
    row = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "group",
        "self_id": int(ACCOUNT_ID),
        "message_id": message_id,
        "message_seq": sequence,
        "time": timestamp,
        "group_id": group_id,
        "sender": {"user_id": sender_id, "nickname": "temporary-peer"},
        "message": [{"type": "text", "data": {"text": f"message-{message_id}"}}],
        "raw_message": f"message-{message_id}",
    }
    if target_id is not None:
        row["target_id"] = target_id
    return row


def _settings(**overrides):
    values = {
        "enabled": True,
        "page_size": 2,
        "max_pages": 8,
        "retry_delay_sec": 0.01,
        "receipt_timeout_sec": 0.2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normalizer_keeps_more_than_twenty_rows_ties_and_rejects_bad_identity():
    now = time.time()
    source = [_private_history_row(100 + index, 200 + index, now + index // 2) for index in range(24)]
    messages, oldest = normalize_napcat_messages(
        source,
        target=PRIVATE,
        account_id=ACCOUNT_ID,
        page_size=24,
    )
    assert len(messages) == 24
    assert oldest == "200"
    assert messages[0]["time"] == messages[1]["time"]
    assert messages[-1]["message_id"] == "123"

    with pytest.raises(Exception, match="account"):
        normalize_napcat_messages(
            [{**source[0], "self_id": "different-account"}],
            target=PRIVATE,
            account_id=ACCOUNT_ID,
            page_size=2,
        )
    with pytest.raises(Exception, match="time"):
        normalize_napcat_messages(
            [{**source[0], "time": float("nan")}],
            target=PRIVATE,
            account_id=ACCOUNT_ID,
            page_size=2,
        )
    with pytest.raises(Exception, match="private targets only"):
        normalize_napcat_messages(
            [_history_row(100, 200, now)],
            target=GROUP,
            account_id=ACCOUNT_ID,
            page_size=2,
        )


def test_normalizer_keeps_temporary_private_rows_navigation_only_without_mutating_sources():
    source = [
        _private_history_row(104, 204, 103.0),
        _temporary_private_history_row(102, 202, 101.0, sender_id=int(ACCOUNT_ID), target_id=84),
        _temporary_private_history_row(101, 201, 100.0, sender_id=84),
        _private_history_row(103, 203, 102.0),
    ]
    original = [dict(row, sender=dict(row["sender"])) for row in source]
    normalized, oldest_cursor = normalize_napcat_messages(
        source,
        target=PRIVATE,
        account_id=ACCOUNT_ID,
        page_size=4,
    )

    assert oldest_cursor == "201"  # Cursor may point at a skipped temporary row.
    assert [message["message_id"] for message in normalized] == ["101", "102", "103", "104"]
    incoming_temporary, outgoing_temporary = normalized[:2]
    assert incoming_temporary["sub_type"] == "group"
    assert incoming_temporary["_offline_history_skip"] is True
    assert "_offline_history_outgoing" not in incoming_temporary
    assert "group_id" not in incoming_temporary
    assert outgoing_temporary["sub_type"] == "group"
    assert outgoing_temporary["_offline_history_skip"] is True
    assert outgoing_temporary["_offline_history_outgoing"] is True
    assert "group_id" not in outgoing_temporary
    assert not normalized[2].get("_offline_history_skip")
    assert not normalized[3].get("_offline_history_skip")
    assert source == original


@pytest.mark.parametrize(
    "bad_row",
    [
        {"message_type": "group", "sub_type": "normal", "group_id": 77002, "sender": {"user_id": 84}},
        {"message_type": "private", "sub_type": "friend", "group_id": 77002, "sender": {"user_id": 84}},
        {"message_type": "private", "sub_type": "stranger", "sender": {"user_id": 84}},
        {"message_type": "private", "sub_type": "group", "group_id": 0, "sender": {"user_id": 84}},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": 999}},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": int(ACCOUNT_ID)}, "target_id": 85},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": int(ACCOUNT_ID)}, "target_id": 0},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": int(ACCOUNT_ID)}, "target_id": ""},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": 0}, "user_id": 84},
        {"message_type": None, "sub_type": "group", "group_id": 77002, "sender": {"user_id": 84}},
        {"message_type": "private", "sub_type": "group", "group_id": 77002, "sender": {"user_id": 84}, "self_id": "80002"},
    ],
)
def test_normalizer_rejects_true_group_mismatched_temporary_scope_and_unsupported_subtypes(bad_row):
    row = {
        "message_id": "bad-row",
        "message_seq": 500,
        "time": 123.0,
        "self_id": int(ACCOUNT_ID),
        "message_type": "private",
        "sub_type": "group",
        "sender": {"user_id": 84},
        **bad_row,
    }
    with pytest.raises(HistoryPageError):
        normalize_napcat_messages([row], target=PRIVATE, account_id=ACCOUNT_ID, page_size=1)


def test_napcat_run_pages_older_messages_and_requires_exact_row_and_run_receipts(tmp_path, monkeypatch):
    async def scenario():
        baseline = time.time() - 30
        store = OfflineHistoryStore(tmp_path / "adapter-state.sqlite3")
        store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            store=store,
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        calls: list[tuple[str, dict]] = []
        pages = [
            [_private_history_row(102, 202, baseline + 2), _private_history_row(103, 203, baseline + 3)],
            [_private_history_row(100, 200, baseline - 2)],
        ]

        async def call_action(action, params, *, expected_connection=None, **_kwargs):
            assert expected_connection is connection
            assert custom_frames[0][0] == "offline_history_run"
            assert custom_frames[0][1]["phase"] == "begin"
            calls.append((action, dict(params)))
            if action == "get_recent_contact":
                return {"status": "ok", "data": []}
            if action == "get_friend_msg_history":
                return {"status": "ok", "data": {"messages": pages.pop(0)}}
            raise AssertionError(f"unexpected action {action}")

        custom_frames: list[tuple[str, dict]] = []

        async def send_custom(*, platform, message_type_name, message):
            assert platform == PLATFORM
            custom_frames.append((message_type_name, dict(message)))
            if message_type_name == "offline_history_run":
                if message["phase"] == "seal":
                    assert store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
                wrong = {
                    "version": 1,
                    "backend": message["backend"],
                    "account_id": "wrong-account",
                    "platform": message["platform"],
                    "run_id": message["run_id"],
                    "run_token": message["run_token"],
                    "phase": message["phase"],
                    "status": "ok",
                }
                runtime.handle_run_receipt({"content": wrong})
                unsupported_version = {
                    key: message[key]
                    for key in ("backend", "account_id", "platform", "run_id", "run_token", "phase")
                }
                unsupported_version.update(version=2, status="ok")
                runtime.handle_run_receipt({"content": unsupported_version})
                exact = {
                    key: message[key]
                    for key in ("version", "backend", "account_id", "platform", "run_id", "run_token", "phase")
                }
                exact["status"] = "ok"
                runtime.handle_run_receipt({"content": exact})
            return True

        async def emit_message(raw, context):
            assert len(calls) == 3  # Snapshot lookup and both history pages finish before delivery.
            metadata = context["metadata"]
            delivery = metadata["delivery"]
            assert delivery["recovered"] is True
            assert delivery["receipt_token"]
            assert metadata["history_run"]["run_id"]
            invalid = {**delivery, "version": 1, "outcome": "accepted", "receipt_token": "wrong"}
            runtime.handle_delivery_receipt({"content": invalid})
            runtime.handle_delivery_receipt(
                {"content": {**delivery, "version": 2, "outcome": "accepted"}}
            )
            await asyncio.sleep(0)
            assert not runtime._delivery_waiters[delivery["delivery_key"]].done()
            runtime.handle_delivery_receipt({"content": {**delivery, "version": 1, "outcome": "accepted"}})
            return "sent"

        connection = object()
        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit_message,
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=connection,
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )

        assert result["complete_sessions"] == 1
        assert not result["failed_sessions"]
        assert [(action, params.get("message_seq"), params.get("reverse_order")) for action, params in calls] == [
            ("get_recent_contact", None, None),
            ("get_friend_msg_history", None, False),
            ("get_friend_msg_history", "202", True),
        ]
        assert calls[0][1] == {"count": 100}
        assert calls[1][1]["count"] == 2
        assert store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[:2] == ("103", baseline + 3)
        run_phases = [frame["phase"] for kind, frame in custom_frames if kind == "offline_history_run"]
        assert run_phases == ["begin", "seal"]
        hold = next(frame for kind, frame in custom_frames if kind == "offline_recovery_progress" and frame["active"])
        assert hold["history_run"]["run_id"]
        store.close()

    # Discovery uses current adapter allowlists, so constrain it without touching
    # any on-disk configuration.
    monkeypatch.setattr(
        global_config,
        "chat",
        SimpleNamespace(
            group_list_type="whitelist",
            group_list=[77001],
            private_list_type="whitelist",
            private_list=[84],
            ban_user_id=[],
        ),
    )
    asyncio.run(scenario())


def test_napcat_private_recovery_scans_temp_rows_but_only_emits_supported_friend_rows(tmp_path, monkeypatch):
    async def scenario():
        import importlib

        from ncnk_message.router import Router
        from src.recv_handler import message_handler as handler_module

        baseline = time.time() - 30
        source_page = [
            _private_history_row(104, 204, baseline + 4),
            _temporary_private_history_row(102, 202, baseline + 2, sender_id=int(ACCOUNT_ID), target_id=84),
            _temporary_private_history_row(101, 201, baseline + 1, sender_id=84),
            _private_history_row(103, 203, baseline + 3),
        ]
        source_copy = deepcopy(source_page)
        older_page = [_private_history_row(99, 199, baseline - 2)]
        pages = [source_page, older_page]
        actions = []
        frames = []
        serialized_messages = []
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(page_size=4), state_path=tmp_path / "temp-history.sqlite3")
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))

        async def call_action(action, params, *, expected_connection=None):
            if action == "get_recent_contact":
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            actions.append(dict(params))
            return {"status": "ok", "data": {"messages": pages.pop(0)}}

        class FakeCoreClient:
            async def send_custom_message(self, message_type_name, message):
                frames.append((message_type_name, dict(message)))
                assert message_type_name == "offline_history_run"
                if message["phase"] == "seal":
                    assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
                runtime.handle_run_receipt({"content": {
                    "version": 1,
                    "backend": message["backend"],
                    "account_id": message["account_id"],
                    "platform": message["platform"],
                    "run_id": message["run_id"],
                    "run_token": message["run_token"],
                    "phase": message["phase"],
                    "status": "ok",
                }})
                return True

            async def send_message(self, payload):
                serialized_messages.append(payload)
                info = payload["message_info"]
                recovery = info["additional_config"]["offline_recovery"]
                delivery = recovery["delivery"]
                assert delivery["recovered"] is True
                assert recovery["history_run"]["run_id"]
                runtime.handle_delivery_receipt({
                    "content": {**delivery, "version": 1, "outcome": "accepted"}
                })
                return True

        main = importlib.import_module("main")
        monkeypatch.setattr(main, "get_offline_history_runtime", lambda: runtime)
        monkeypatch.setattr(main.nc_message_sender, "send_message_to_napcat", call_action)
        monkeypatch.setattr(message_handler, "verified_account_id", ACCOUNT_ID)
        monkeypatch.setattr(message_handler, "connection_epoch_started_at", baseline)
        monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
        conversion = AsyncMock(return_value=([Seg(type="text", data="recovered")], {}))
        monkeypatch.setattr(
            message_handler,
            "handle_real_message",
            conversion,
        )
        monkeypatch.setattr(handler_module, "get_offline_history_runtime", lambda: runtime)
        fake_router = object.__new__(Router)
        fake_router.config = SimpleNamespace(
            route_config={PLATFORM: SimpleNamespace(url="ws://core.test")}
        )
        fake_router.clients = {PLATFORM: FakeCoreClient()}
        monkeypatch.setattr(message_send_instance, "nachobot_router", fake_router)
        assert main.configure_offline_history_runtime() is runtime

        store = runtime.store
        store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )

        assert source_page == source_copy
        assert actions == [
            {"count": 4, "reverse_order": False, "user_id": 84},
            {"count": 4, "reverse_order": True, "user_id": 84, "message_seq": "201"},
        ]
        delivery_rows = [
            message["message_info"]["additional_config"]["offline_recovery"]["delivery"]
            for message in serialized_messages
        ]
        assert [row["delivery_key"] for row in delivery_rows] == [
            scoped_delivery_key(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE, message_id)
            for message_id in ("103", "104")
        ]
        assert [row["message_id"] for row in delivery_rows] == ["103", "104"]
        assert conversion.await_count == 2
        assert [message["message_info"]["time"] for message in serialized_messages] == [
            baseline + 3,
            baseline + 4,
        ]
        assert all(row["recovered"] is True for row in delivery_rows)
        run_messages = [message for kind, message in frames if kind == "offline_history_run"]
        assert [message["phase"] for message in run_messages] == ["begin", "seal"]
        assert all(
            target == {"session_kind": "private", "session_id": "84"}
            for message in run_messages
            for target in message["targets"]
        )
        assert result["complete_sessions"] == 1 and result["failed_sessions"] == {}
        assert store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[:2] == ("104", baseline + 4)
        assert store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE) == ()
        store.close()

    asyncio.run(scenario())


def test_napcat_malformed_temporary_private_page_emits_nothing_or_advances_checkpoint(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        malformed_page = [
            _private_history_row(104, 204, baseline + 4),
            _temporary_private_history_row(102, 202, baseline + 2, sender_id=999),
        ]
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(), state_path=tmp_path / "bad-temp-history.sqlite3")
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        emitted = []
        frames = []

        async def call_action(action, params, *, expected_connection=None):
            if action == "get_recent_contact":
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            return {"status": "ok", "data": {"messages": malformed_page}}

        async def send_custom(*, platform, message_type_name, message):
            frames.append((message_type_name, dict(message)))
            if message_type_name == "offline_history_run":
                runtime.handle_run_receipt({"content": {
                    "version": 1,
                    "backend": message["backend"],
                    "account_id": message["account_id"],
                    "platform": platform,
                    "run_id": message["run_id"],
                    "run_token": message["run_token"],
                    "phase": message["phase"],
                    "status": "ok",
                }})
            return True

        async def emit_message(raw, context):
            emitted.append(raw)
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit_message,
            admission_check=AsyncMock(return_value=True),
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        assert result["complete_sessions"] == 0
        assert result["failed_sessions"]
        assert emitted == []
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
        assert runtime.store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE) == ()
        assert [message["phase"] for kind, message in frames if kind == "offline_history_run"] == ["begin", "seal"]
        runtime.store.close()

    asyncio.run(scenario())


def test_legacy_group_session_and_outbox_are_never_pulled_marked_or_emitted(tmp_path, monkeypatch):
    async def scenario():
        baseline = time.time() - 20
        store = OfflineHistoryStore(tmp_path / "private-only-state.sqlite3")
        store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        store.register_target(BACKEND, ACCOUNT_ID, PLATFORM, GROUP)
        old_group = _history_row(444, 544, baseline + 2)
        store.stage_page(BACKEND, ACCOUNT_ID, PLATFORM, GROUP, [old_group], run_id="legacy-group", cutoff_at=baseline + 5)
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(), store=store)
        actions = []
        custom_frames = []
        emitted = []

        async def call_action(action, params, *, expected_connection=None):
            if action == "get_recent_contact":
                return {"status": "ok", "data": []}
            actions.append((action, dict(params)))
            assert action == "get_friend_msg_history"
            assert params["user_id"] == 84
            row = _private_history_row(901, 901, baseline + 5, sender_id=int(ACCOUNT_ID))
            row["target_id"] = 84
            return {"status": "ok", "data": {"messages": [row]}}

        async def send_custom(*, platform, message_type_name, message):
            custom_frames.append((message_type_name, dict(message)))
            if message_type_name == "offline_history_run":
                runtime.handle_run_receipt(
                    {
                        "content": {
                            "version": 1,
                            "backend": message["backend"],
                            "account_id": message["account_id"],
                            "platform": platform,
                            "run_id": message["run_id"],
                            "run_token": message["run_token"],
                            "phase": message["phase"],
                            "status": "ok",
                        }
                    }
                )
            return True

        async def emit_message(raw, context):
            emitted.append((raw, context))
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit_message,
            admission_check=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            global_config,
            "chat",
            SimpleNamespace(
                group_list_type="blacklist",
                group_list=[77001],
                private_list_type="whitelist",
                private_list=[84],
                ban_user_id=[],
            ),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )

        run_frames = [frame for kind, frame in custom_frames if kind == "offline_history_run"]
        progress_frames = [frame for kind, frame in custom_frames if kind == "offline_recovery_progress"]
        assert [frame["phase"] for frame in run_frames] == ["begin", "seal"]
        assert all(
            target == {"session_kind": "private", "session_id": "84"}
            for frame in run_frames
            for target in frame["targets"]
        )
        assert progress_frames and all(frame["group_id"] is None and frame["user_id"] == 84 for frame in progress_frames)
        assert [action for action, _params in actions] == ["get_friend_msg_history"]
        assert emitted == []
        assert any(target == GROUP for target in store.known_targets(BACKEND, ACCOUNT_ID, PLATFORM))
        pending_group = store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, GROUP)
        assert len(pending_group) == 1 and pending_group[0].message_id == "444"
        assert pending_group[0].delivery_key not in runtime._receipt_expectations
        assert store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, GROUP)[0] is None
        assert result["complete_sessions"] == 1
        store.close()

    asyncio.run(scenario())


def test_group_live_decoration_keeps_identity_and_original_time_without_recovery_anchor(tmp_path):
    async def scenario():
        store = OfflineHistoryStore(tmp_path / "group-live-only.sqlite3")
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(), store=store)
        original_at = 1234567890.25
        context = {"metadata": {"original_timestamp": original_at}}
        result = await runtime.decorate_live_message(
            _history_row(555, 655, original_at), context, account_id=ACCOUNT_ID
        )
        delivery = result["metadata"]["delivery"]
        assert delivery["recovered"] is False
        assert delivery["delivery_key"] == scoped_delivery_key(BACKEND, ACCOUNT_ID, PLATFORM, GROUP, "555")
        assert result["metadata"]["original_timestamp"] == original_at
        assert store.known_targets(BACKEND, ACCOUNT_ID, PLATFORM) == ()
        assert store.live_anchor(BACKEND, ACCOUNT_ID, PLATFORM, GROUP) == (None, None)
        assert not runtime._rerun_event.is_set()
        store.close()

    asyncio.run(scenario())


def test_group_history_entrypoints_reject_without_calling_api_or_emitting(tmp_path):
    async def scenario():
        calls = []

        async def call_action(action, params, *, expected_connection=None):
            calls.append(action)
            raise AssertionError("group history API must be unreachable")

        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "group-rejected.sqlite3",
            call_action=call_action,
        )
        with pytest.raises(HistoryPageError, match="private targets only"):
            await runtime.fetch_page(
                target=GROUP, cursor=None, page_size=10, connection=object(), account_id=ACCOUNT_ID
            )
        with pytest.raises(HistoryPageError, match="private targets only"):
            runtime.plan_target(GROUP, None, None, frozenset(), None, None, time.time())
        assert await runtime.emit_history_item(
            SimpleNamespace(target=GROUP), run_id="legacy", cutoff=time.time(), account_id=ACCOUNT_ID,
            connection_epoch_started_at=time.time(), account_is_current=lambda: True,
        ) is False
        with pytest.raises(HistoryPageError, match="group targets"):
            await runtime.send_hold(
                target=GROUP, progress_id="progress", cutoff=time.time(), active=True,
                run_id="run", route_total=1, account_id=ACCOUNT_ID, connection_epoch_started_at=time.time(),
            )
        with pytest.raises(HistoryPageError, match="private targets only"):
            await runtime.send_run(
                run_id="run", run_token="token", cutoff=time.time(), targets=(GROUP,), phase="begin",
                account_id=ACCOUNT_ID, account_is_current=lambda: True,
            )
        assert calls == []
        runtime.store.close()

    asyncio.run(scenario())


def test_recovered_napcat_row_uses_actual_handler_and_preserves_exact_receipt_metadata(tmp_path, monkeypatch):
    async def scenario():
        import importlib

        main = importlib.import_module("main")
        epoch = time.time() - 100
        store = OfflineHistoryStore(tmp_path / "handler-state.sqlite3")
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(), store=store)
        raw = _private_history_row(555, 700, epoch - 20)
        target = PRIVATE
        key = scoped_delivery_key(BACKEND, ACCOUNT_ID, PLATFORM, target, "555")
        item = PendingHistoryMessage(
            target=target,
            message_id="555",
            original_timestamp=epoch - 20,
            raw_message=raw,
            delivery_key=key,
            receipt_token="test-receipt-token",
            run_id="test-run-id",
            cutoff_at=epoch,
        )
        monkeypatch.setattr(main, "get_offline_history_runtime", lambda: runtime)
        assert main.configure_offline_history_runtime() is runtime
        assert runtime._call_action == main.nc_message_sender.send_message_to_napcat
        assert runtime._send_custom is not None
        assert runtime._admission_check is not None
        monkeypatch.setattr(message_handler, "verified_account_id", ACCOUNT_ID)
        monkeypatch.setattr(message_handler, "connection_epoch_started_at", epoch)
        monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "src.recv_handler.message_handler.get_group_info",
            AsyncMock(return_value={"group_name": "test"}),
        )
        monkeypatch.setattr(
            message_handler,
            "handle_real_message",
            AsyncMock(return_value=([Seg(type="text", data="recovered")], {})),
        )

        async def send_to_core(message_base):
            envelope = message_base.message_info.additional_config["offline_recovery"]
            delivery = envelope["delivery"]
            assert delivery["recovered"] is True
            assert delivery["receipt_token"] == "test-receipt-token"
            assert delivery["delivery_key"] == key
            assert envelope["history_run"] == {"run_id": "test-run-id", "cutoff_at": epoch, "version": 1}
            assert message_base.message_info.time == epoch - 20
            runtime.handle_delivery_receipt(
                {"content": {**delivery, "version": 1, "outcome": "accepted"}}
            )
            return True

        monkeypatch.setattr(message_send_instance, "message_send", send_to_core)
        result = await runtime.emit_history_item(
            item,
            run_id="new-run-id",
            cutoff=epoch,
            account_id=ACCOUNT_ID,
            connection_epoch_started_at=epoch,
            account_is_current=lambda: True,
        )
        assert result is True
        store.close()

    asyncio.run(scenario())


def test_prelogin_slow_live_message_gets_same_identity_and_old_queued_account_is_not_relabelled(tmp_path, monkeypatch):
    async def scenario():
        from src.recv_handler import message_handler as handler_module

        epoch = time.time()
        temp_runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            store=OfflineHistoryStore(tmp_path / "live-state.sqlite3"),
        )
        monkeypatch.setattr(handler_module, "get_offline_history_runtime", lambda: temp_runtime)
        monkeypatch.setattr(message_handler, "verified_account_id", None)
        monkeypatch.setattr(message_handler, "connection_epoch_started_at", epoch)
        monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "src.recv_handler.message_handler.get_group_info",
            AsyncMock(return_value={"group_name": "test"}),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_conversion(_raw):
            started.set()
            await release.wait()
            return [Seg(type="text", data="live")], {}

        monkeypatch.setattr(message_handler, "handle_real_message", slow_conversion)
        sent_metadata = []

        async def send_to_core(message_base):
            sent_metadata.append(message_base.message_info.additional_config["offline_recovery"])
            return True

        monkeypatch.setattr(message_send_instance, "message_send", send_to_core)
        raw = _history_row(777, 900, epoch - 2)
        live_context = message_handler.capture_offline_recovery_context(
            raw,
            connection_epoch_started_at=epoch,
            received_at=epoch,
        )
        live_task = asyncio.create_task(
            message_handler.handle_raw_message(raw, live_context, manage_progress=False)
        )
        await started.wait()
        message_handler.verified_account_id = ACCOUNT_ID
        release.set()
        assert await live_task == "sent"
        live_delivery = sent_metadata[-1]["delivery"]
        history_delivery = build_delivery_metadata(
            backend=BACKEND,
            account_id=ACCOUNT_ID,
            platform=PLATFORM,
            target=GROUP,
            message_id="777",
            recovered=True,
            receipt_token="recovered-token",
        )
        assert live_delivery["delivery_key"] == history_delivery["delivery_key"]
        assert live_delivery["recovered"] is False

        sent_metadata.clear()
        old_connection_generation = message_handler.connection_generation
        old_raw = {**raw, "self_id": "80002"}
        old_context = message_handler.capture_offline_recovery_context(
            old_raw,
            connection_epoch_started_at=epoch - 1,
            received_at=epoch,
        )
        monkeypatch.setattr(message_handler, "connection_generation", "new-verified-connection")
        result = await message_handler.handle_raw_message(
            old_raw,
            old_context,
            manage_progress=False,
        )
        assert result == "filtered"
        assert sent_metadata == []
        assert old_context["metadata"]["transport_connection_generation"] == old_connection_generation
        temp_runtime.store.close()

    asyncio.run(scenario())


def test_prelogin_fast_transport_message_waits_for_identity_and_deduplicates_history(tmp_path, monkeypatch):
    async def scenario():
        from src.recv_handler import message_handler as handler_module

        epoch = time.time()
        handler = MessageHandler()
        connection = object()
        await handler.set_server_connection(connection, epoch_started_at=epoch)
        generation = handler.connection_generation
        temp_runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            store=OfflineHistoryStore(tmp_path / "prelogin-fast.sqlite3"),
        )
        monkeypatch.setattr(handler_module, "get_offline_history_runtime", lambda: temp_runtime)
        monkeypatch.setattr(handler, "check_allow_to_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "src.recv_handler.message_handler.get_group_info",
            AsyncMock(return_value={"group_name": "test"}),
        )
        converted = asyncio.Event()

        async def fast_conversion(_raw):
            converted.set()
            return [Seg(type="text", data="live")], {}

        monkeypatch.setattr(handler, "handle_real_message", fast_conversion)
        send_calls: list[dict[str, Any]] = []
        claimed: set[str] = set()
        core_hook_calls: list[str] = []
        claim_lock = asyncio.Lock()
        core_hook_entered = asyncio.Event()
        release_core_hook = asyncio.Event()

        async def fake_core_send(message_base):
            metadata = message_base.message_info.additional_config["offline_recovery"]
            delivery = metadata.get("delivery")
            assert delivery is not None
            key = delivery["delivery_key"]
            send_calls.append(delivery)
            async with claim_lock:
                first_claim = key not in claimed
                if first_claim:
                    claimed.add(key)
                    core_hook_calls.append(key)
            if first_claim:
                core_hook_entered.set()
                await release_core_hook.wait()
            return True

        monkeypatch.setattr(message_send_instance, "message_send", fake_core_send)
        raw_live = _history_row(777, 900, epoch - 2)
        live_context = handler.capture_offline_recovery_context(
            raw_live,
            connection_epoch_started_at=epoch,
            received_at=epoch,
            transport_owned=True,
        )
        live_task = asyncio.create_task(
            handler.handle_raw_message(raw_live, live_context, manage_progress=False)
        )
        await asyncio.wait_for(converted.wait(), timeout=1.0)
        assert send_calls == []  # Conversion finished while the login echo is still pending.

        await temp_runtime.prepare_account(ACCOUNT_ID)
        assert handler.publish_verified_account_id(
            ACCOUNT_ID,
            connection_generation=generation,
            expected_connection=connection,
        )
        await asyncio.wait_for(core_hook_entered.wait(), timeout=1.0)

        raw_history = dict(raw_live)
        history_context = handler.capture_offline_recovery_context(
            raw_history,
            connection_epoch_started_at=epoch,
            received_at=epoch,
        )
        history_context["metadata"]["delivery"] = build_delivery_metadata(
            backend=BACKEND,
            account_id=ACCOUNT_ID,
            platform=PLATFORM,
            target=GROUP,
            message_id="777",
            recovered=True,
            receipt_token="history-receipt",
        )
        history_task = asyncio.create_task(
            handler.handle_raw_message(raw_history, history_context, manage_progress=False)
        )
        assert await asyncio.wait_for(history_task, timeout=1.0) == "sent"
        assert not live_task.done()  # The first Core hook is still awaiting its durable claim path.
        assert len(send_calls) == 2
        assert send_calls[0]["delivery_key"] == send_calls[1]["delivery_key"]
        assert send_calls[0]["recovered"] is False
        assert send_calls[1]["recovered"] is True
        assert core_hook_calls == [send_calls[0]["delivery_key"]]

        release_core_hook.set()
        assert await asyncio.wait_for(live_task, timeout=1.0) == "sent"
        temp_runtime.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["disconnect", "reconnect"])
def test_prelogin_transport_wait_is_released_without_sending_old_account_message(
    tmp_path, monkeypatch, transition
):
    async def scenario():
        from src.recv_handler import message_handler as handler_module

        epoch = time.time()
        handler = MessageHandler()
        old_connection = object()
        await handler.set_server_connection(old_connection, epoch_started_at=epoch)
        old_generation = handler.connection_generation
        temp_runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            store=OfflineHistoryStore(tmp_path / f"prelogin-{transition}.sqlite3"),
        )
        monkeypatch.setattr(handler_module, "get_offline_history_runtime", lambda: temp_runtime)
        monkeypatch.setattr(handler, "check_allow_to_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "src.recv_handler.message_handler.get_group_info",
            AsyncMock(return_value={"group_name": "test"}),
        )
        converted = asyncio.Event()

        async def fast_conversion(_raw):
            converted.set()
            return [Seg(type="text", data="old connection")], {}

        monkeypatch.setattr(handler, "handle_real_message", fast_conversion)
        sent: list[Any] = []
        monkeypatch.setattr(message_send_instance, "message_send", AsyncMock(side_effect=lambda item: sent.append(item) or True))
        raw = _history_row(778, 901, epoch - 1)
        context = handler.capture_offline_recovery_context(
            raw,
            connection_epoch_started_at=epoch,
            received_at=epoch,
            transport_owned=True,
        )
        task = asyncio.create_task(handler.handle_raw_message(raw, context, manage_progress=False))
        await asyncio.wait_for(converted.wait(), timeout=1.0)
        assert sent == []

        if transition == "disconnect":
            assert handler.invalidate_server_connection(
                old_connection,
                connection_generation=old_generation,
            )
        else:
            new_connection = object()
            await handler.set_server_connection(new_connection, epoch_started_at=epoch + 1)
            assert handler.publish_verified_account_id(
                "80002",
                connection_generation=handler.connection_generation,
                expected_connection=new_connection,
            )

        assert await asyncio.wait_for(task, timeout=1.0) == "filtered"
        assert sent == []
        temp_runtime.store.close()

    asyncio.run(scenario())


def test_main_connection_state_uses_real_websockets_enum_and_login_retry_is_generation_safe(monkeypatch):
    import importlib

    main = importlib.import_module("main")
    assert main._connection_is_open(SimpleNamespace(state=State.OPEN)) is True
    assert main._connection_is_open(SimpleNamespace(state=State.CLOSED)) is False

    async def scenario():
        connection = object()
        handler = MessageHandler()
        monkeypatch.setattr(main, "message_handler", handler)
        monkeypatch.setattr(main, "_active_server_connection", connection)
        monkeypatch.setattr(main, "_connection_epoch_started_at", 123.0)
        await handler.set_server_connection(connection, epoch_started_at=123.0)
        runtime = SimpleNamespace(enabled=True, prepare_account=AsyncMock())
        monkeypatch.setattr(main, "get_offline_history_runtime", lambda: runtime)
        attempts = 0

        async def action(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("first response lost")
            return {"status": "ok", "data": {"user_id": 90001}}

        monkeypatch.setattr(main.nc_message_sender, "send_message_to_napcat", action)
        monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(main, "notify_platform_status", lambda **_kwargs: None)
        await main._verify_napcat_login(connection, 123.0)
        assert attempts == 2
        assert handler.verified_account_id == ACCOUNT_ID
        runtime.prepare_account.assert_awaited_once_with(ACCOUNT_ID)

        stale_connection = object()
        new_connection = object()
        monkeypatch.setattr(main, "_active_server_connection", stale_connection)
        monkeypatch.setattr(main, "_connection_epoch_started_at", 456.0)
        await handler.set_server_connection(stale_connection, epoch_started_at=456.0)
        runtime.prepare_account.reset_mock()

        async def old_reply(*_args, **_kwargs):
            monkeypatch.setattr(main, "_active_server_connection", new_connection)
            monkeypatch.setattr(main, "_connection_epoch_started_at", 457.0)
            return {"status": "ok", "data": {"user_id": 80002}}

        monkeypatch.setattr(main.nc_message_sender, "send_message_to_napcat", old_reply)
        await main._verify_napcat_login(stale_connection, 456.0)
        assert handler.verified_account_id is None
        runtime.prepare_account.assert_not_awaited()

    asyncio.run(scenario())


def test_monitor_restarts_for_platform_and_core_socket_generations(tmp_path):
    async def scenario():
        runtime = NapcatOfflineHistory(platform=PLATFORM, settings=_settings(), state_path=tmp_path / "monitor.sqlite3")
        client_state = {"connected": True}
        socket_state = {"value": object()}
        connection = SimpleNamespace(
            _connection_uuid="core-connection",
            _connected_count=1,
            network_driver=SimpleNamespace(active_connections={"core-connection": socket_state["value"]}),
        )

        class FakeClient:
            def __init__(self):
                self.connection = connection

            def is_connected(self):
                return client_state["connected"]

        router = SimpleNamespace(clients={PLATFORM: FakeClient()})
        platform_state = {
            "connected": False,
            "account_id": ACCOUNT_ID,
            "identity": ("qq-ws-1",),
            "connection": object(),
            "connection_epoch_started_at": time.time(),
        }
        started: list[tuple[Any, ...]] = []
        cancelled: list[bool] = []
        cancel_event = asyncio.Event()
        changed = asyncio.Event()

        async def fake_run_generation(*, account_id, account_is_current, **_kwargs):
            started.append((account_id, tuple(platform_state["identity"]), core_connection_identity(router, PLATFORM)[1]))
            changed.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                cancel_event.set()
                raise

        runtime.run_generation = fake_run_generation
        monitor = asyncio.create_task(
            runtime.monitor(
                get_platform_state=lambda: dict(platform_state),
                router=router,
                poll_interval=0.1,
            )
        )
        await asyncio.sleep(0.03)
        assert not started

        platform_state["connected"] = True
        await asyncio.wait_for(changed.wait(), timeout=1.0)
        assert len(started) == 1

        client_state["connected"] = False
        await asyncio.wait_for(cancel_event.wait(), timeout=1.0)
        assert cancelled
        client_state["connected"] = True
        socket_state["value"] = object()
        connection.network_driver.active_connections["core-connection"] = socket_state["value"]
        changed.clear()
        cancel_event.clear()
        await asyncio.wait_for(changed.wait(), timeout=1.0)
        assert len(started) >= 2

        platform_state["identity"] = ("qq-ws-2",)
        platform_state["connection_epoch_started_at"] = time.time()
        changed.clear()
        cancel_event.clear()
        await asyncio.wait_for(changed.wait(), timeout=1.0)
        assert len(started) >= 3

        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        runtime.store.close()

    asyncio.run(scenario())


def test_main_reader_processes_echo_while_login_action_is_waiting(monkeypatch):
    import importlib

    main = importlib.import_module("main")

    class FakeConnection:
        async def __aiter__(self):
            await asyncio.sleep(0)
            yield json.dumps(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "message_id": 42,
                    "time": time.time(),
                    "self_id": 90001,
                    "sender": {"user_id": 84, "nickname": "sender"},
                    "message": [{"type": "text", "data": {"text": "queued"}}],
                    "raw_message": "queued",
                }
            )
            yield json.dumps({"echo": "reader-remains-independent", "status": "ok"})

    async def scenario():
        connection = FakeConnection()
        waiting = asyncio.Event()
        release = asyncio.Event()
        handled = asyncio.Event()

        async def blocked_login(*_args, **_kwargs):
            waiting.set()
            await release.wait()
            return {"status": "ok", "data": {"user_id": 90001}}

        async def record_response(payload):
            assert payload["echo"] == "reader-remains-independent"
            handled.set()

        monkeypatch.setattr(main, "_active_server_connection", None)
        monkeypatch.setattr(main, "_connection_epoch_started_at", 0.0)
        monkeypatch.setattr(main, "last_message_time", time.time())
        monkeypatch.setattr(main, "message_queue", asyncio.Queue())
        monkeypatch.setattr(main, "put_response", record_response)
        monkeypatch.setattr(main, "connection_watchdog", AsyncMock())
        monkeypatch.setattr(main.message_handler, "set_server_connection", AsyncMock())
        monkeypatch.setattr(main.notice_handler, "set_server_connection", AsyncMock())
        monkeypatch.setattr(main.nc_message_sender, "set_server_connection", AsyncMock())
        monkeypatch.setattr(main.nc_message_sender, "send_message_to_napcat", blocked_login)
        monkeypatch.setattr(message_send_instance, "send_custom_message", AsyncMock(return_value=True))
        monkeypatch.setattr(
            main,
            "get_offline_history_runtime",
            lambda: SimpleNamespace(enabled=True, prepare_account=AsyncMock()),
        )
        await main.message_recv(connection)
        assert handled.is_set()
        queued_message, queued_context = await main.message_queue.get()
        assert queued_message["message_id"] == 42
        assert queued_context["transport_owned"] is True
        assert queued_context["metadata"]["transport_connection_generation"]
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        release.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())
