from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message import Seg  # noqa: E402
from ncnk_message.offline_history import HistoryTarget  # noqa: E402
from src.offline_history import BACKEND, NapcatOfflineHistory  # noqa: E402
from src.recv_handler.message_handler import message_handler  # noqa: E402
from src.recv_handler.message_sending import message_send_instance  # noqa: E402


ACCOUNT_ID = "90001"
PLATFORM = "qq"
PRIVATE = HistoryTarget("private", "84")


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


def _row(message_id: int, sequence: int, timestamp: float, *, account_id: str = ACCOUNT_ID, peer_id: str = "84"):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": int(account_id),
        "message_id": message_id,
        "message_seq": sequence,
        "time": timestamp,
        "sender": {"user_id": int(peer_id), "nickname": "peer"},
        "message": [{"type": "text", "data": {"text": f"canonical-{message_id}"}}],
        "raw_message": f"canonical-{message_id}",
    }


def _recent_row(
    raw_message_id: str,
    timestamp: float,
    *,
    peer_id: str = "84",
    chat_type: int = 1,
    latest: dict | None = None,
):
    row = {
        "peerUin": peer_id,
        "chatType": chat_type,
        "msgId": raw_message_id,
        "msgTime": timestamp,
    }
    if latest is not None:
        row["lastestMsg"] = latest
    return row


def _exact_run_receipt(runtime: NapcatOfflineHistory, message: dict) -> None:
    runtime.handle_run_receipt(
        {
            "content": {
                "version": 1,
                "backend": message["backend"],
                "account_id": message["account_id"],
                "platform": message["platform"],
                "run_id": message["run_id"],
                "run_token": message["run_token"],
                "phase": message["phase"],
                "status": "ok",
            }
        }
    )


@pytest.mark.parametrize("first_page", ["raised", "empty", "stale", "same_second"])
def test_latest_raw_anchor_recovers_through_history_and_actual_core_serialization(
    tmp_path, monkeypatch, first_page
):
    async def scenario():
        import importlib

        handler_module = importlib.import_module("src.recv_handler.message_handler")
        baseline = time.time() - 30
        anchor_time = baseline + 3
        anchor_raw_id = "1234567890123456789"
        anchor_row = _row(303, 403, anchor_time)
        previous_row = _row(302, 402, baseline + 2)
        older_row = _row(301, 401, baseline - 1)
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / f"anchor-{first_page}.sqlite3",
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)

        actions: list[tuple[str, dict]] = []
        serialized: list[dict] = []
        phases: list[str] = []
        history_call = 0

        async def call_action(action, params, *, expected_connection=None):
            nonlocal history_call
            assert expected_connection is connection
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                assert params == {"count": 100}
                return {
                    "status": "ok",
                    "data": [
                        _recent_row(
                            anchor_raw_id,
                            anchor_time,
                            latest={
                                "message_type": "private",
                                "sub_type": "friend",
                                "self_id": int(ACCOUNT_ID),
                                "sender": {"user_id": 84},
                                "time": anchor_time,
                            },
                        )
                    ],
                }
            if action == "get_friend_msg_history":
                history_call += 1
                if history_call == 1:
                    if first_page == "raised":
                        raise RuntimeError("消息undefined不存在")
                    if first_page == "empty":
                        return {"status": "ok", "data": {"messages": []}}
                    if first_page == "stale":
                        return {"status": "ok", "data": {"messages": [_row(299, 399, baseline + 1)]}}
                    return {
                        "status": "ok",
                        "data": {"messages": [_row(298, 398, anchor_time)]},
                    }
                if params.get("message_seq") == anchor_raw_id:
                    return {"status": "ok", "data": {"messages": [previous_row, anchor_row]}}
                assert params.get("message_seq") == "402"
                return {"status": "ok", "data": {"messages": [older_row]}}
            if action == "get_msg":
                assert params == {"message_id": anchor_raw_id}
                return {"status": "ok", "data": dict(anchor_row)}
            raise AssertionError(f"unexpected NapCat action {action}")

        async def send_custom(*, platform, message_type_name, message):
            assert platform == PLATFORM
            if message_type_name == "offline_history_run":
                phases.append(message["phase"])
                if message["phase"] == "seal":
                    assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
                _exact_run_receipt(runtime, message)
            return True

        async def send_message_to_core(message_base):
            wire = message_base.to_dict()
            serialized.append(wire)
            info = wire["message_info"]
            recovery = info["additional_config"]["offline_recovery"]
            delivery = recovery["delivery"]
            assert info["time"] == recovery["original_timestamp"]
            runtime.handle_delivery_receipt(
                {"content": {**delivery, "version": 1, "outcome": "accepted"}}
            )
            return True

        monkeypatch.setattr(handler_module, "get_offline_history_runtime", lambda: runtime)
        monkeypatch.setattr(message_handler, "verified_account_id", ACCOUNT_ID)
        monkeypatch.setattr(message_handler, "connection_epoch_started_at", baseline)
        monkeypatch.setattr(message_handler, "check_allow_to_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(
            message_handler,
            "handle_real_message",
            AsyncMock(return_value=([Seg(type="text", data="recovered")], {})),
        )
        monkeypatch.setattr(message_send_instance, "message_send", send_message_to_core)
        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=lambda raw, context: message_handler.handle_raw_message(
                raw, context, manage_progress=False
            ),
            admission_check=AsyncMock(return_value=True),
        )
        connection = object()
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=connection,
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )

        assert result["complete_sessions"] == 1
        assert result["failed_sessions"] == {}
        assert phases == ["begin", "seal"]
        expected_ids = ["302", "303", "298"] if first_page == "same_second" else ["302", "303"]
        expected_times = [baseline + 2, anchor_time, anchor_time] if first_page == "same_second" else [baseline + 2, anchor_time]
        assert [row["message_info"]["message_id"] for row in serialized] == expected_ids
        assert [row["message_info"]["time"] for row in serialized] == expected_times
        assert all(
            row["message_info"]["additional_config"]["offline_recovery"]["delivery"]["recovered"]
            for row in serialized
        )
        history_actions = [params for action, params in actions if action == "get_friend_msg_history"]
        assert history_actions == [
            {"count": 2, "reverse_order": False, "user_id": 84},
            {"count": 2, "reverse_order": True, "user_id": 84, "message_seq": anchor_raw_id},
            {"count": 2, "reverse_order": True, "user_id": 84, "message_seq": "402"},
        ]
        assert actions.count(("get_recent_contact", {"count": 100})) == 1
        assert [action for action, _params in actions].count("get_msg") == 1
        checkpoint_id, checkpoint_at, checkpoint_ids = runtime.store.checkpoint(
            BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE
        )
        if first_page == "same_second":
            assert checkpoint_id in {"298", "303"}
            assert checkpoint_at == anchor_time
            assert checkpoint_ids == frozenset({"298", "303"})
        else:
            assert (checkpoint_id, checkpoint_at) == ("303", anchor_time)
        runtime.store.close()

    asyncio.run(scenario())


def test_raw_recent_id_is_navigation_only_and_requires_exact_get_msg_coverage(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        anchor_time = baseline + 3
        anchor_raw_id = "9876543210987654321"
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(), state_path=tmp_path / "proof-miss.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        emitted = []

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {"status": "ok", "data": [_recent_row(anchor_raw_id, anchor_time)]}
            if action == "get_friend_msg_history" and params.get("message_seq") is None:
                raise RuntimeError("empty first page")
            if action == "get_friend_msg_history":
                assert params["message_seq"] == anchor_raw_id
                return {"status": "ok", "data": {"messages": [_row(303, 403, anchor_time)]}}
            if action == "get_msg":
                nested_user_payload = _row(303, 403, anchor_time)
                return {"status": "ok", "data": {"message": nested_user_payload}}
            raise AssertionError(action)

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=lambda raw, _context: emitted.append(raw) or "sent",
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        assert result["complete_sessions"] == 0
        assert result["failed_sessions"]
        assert emitted == []
        assert [action for action, _params in actions] == [
            "get_recent_contact",
            "get_friend_msg_history",
            "get_friend_msg_history",
            "get_msg",
        ]
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
        assert runtime.store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE) == ()
        runtime.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "recent_rows",
    [
        [],
        [_recent_row("1234567890123456789", 1_800_000_000, chat_type=2)],
        [
            _recent_row(
                "1234567890123456789",
                1_800_000_000,
                latest={"message_type": "private", "self_id": 99999, "time": 1_800_000_000},
            )
        ],
        [
            _recent_row(
                "1234567890123456789",
                1_800_000_000,
                latest={
                    "message_type": "private",
                    "sub_type": "group",
                    "self_id": int(ACCOUNT_ID),
                    "sender": {"user_id": 84},
                    "group_id": 77002,
                    "time": 1_800_000_000,
                },
            )
        ],
        [
            _recent_row(
                "1234567890123456789",
                1_800_000_000,
                latest={
                    "message_type": "private",
                    "self_id": int(ACCOUNT_ID),
                    "sender": {"user_id": 99999},
                    "time": 1_800_000_000,
                },
            )
        ],
    ],
)
def test_missing_group_or_cross_account_snapshot_cannot_rescue_failed_first_page(tmp_path, recent_rows):
    async def scenario():
        baseline = time.time() - 30
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(), state_path=tmp_path / "no-anchor.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        emitted = []

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {"status": "ok", "data": recent_rows}
            assert action == "get_friend_msg_history"
            raise RuntimeError("initial history unavailable")

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=lambda raw, _context: emitted.append(raw) or "sent",
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        history_calls = [params for action, params in actions if action == "get_friend_msg_history"]
        assert result["complete_sessions"] == 0
        assert result["failed_sessions"]
        assert len(history_calls) == 1 and "message_seq" not in history_calls[0]
        assert not any(action in {"get_group_msg_history", "get_group_msg_history_by_seq"} for action, _ in actions)
        assert emitted == []
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
        assert runtime.store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE) == ()
        runtime.store.close()

    asyncio.run(scenario())


def test_successful_cross_account_first_page_is_strict_and_does_not_fallback(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        anchor_time = baseline + 3
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(), state_path=tmp_path / "bad-page.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        emitted = []

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {"status": "ok", "data": [_recent_row("1234567890123456789", anchor_time)]}
            assert action == "get_friend_msg_history"
            return {"status": "ok", "data": {"messages": [_row(303, 403, anchor_time, account_id="99999")]}}

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=lambda raw, _context: emitted.append(raw) or "sent",
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        assert result["complete_sessions"] == 0
        assert result["failed_sessions"]
        assert [action for action, _params in actions] == [
            "get_recent_contact",
            "get_friend_msg_history",
        ]
        assert emitted == []
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[0] is None
        runtime.store.close()

    asyncio.run(scenario())


def test_known_parsed_short_id_proves_same_second_first_page_without_raw_id_lookup(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        anchor_time = baseline + 3
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(), state_path=tmp_path / "known-id.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        pages = [[_row(303, 403, anchor_time), _row(302, 402, baseline + 2)], [_row(301, 401, baseline - 1)]]

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {
                    "status": "ok",
                    "data": [
                        _recent_row(
                            "1234567890123456789",
                            anchor_time,
                            latest={
                                "message_type": "private",
                                "self_id": int(ACCOUNT_ID),
                                "sender": {"user_id": 84},
                                "message_id": 303,
                                "message_seq": 403,
                                "time": anchor_time,
                            },
                        )
                    ],
                }
            assert action == "get_friend_msg_history"
            return {"status": "ok", "data": {"messages": pages.pop(0)}}

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        async def emit(raw, context):
            delivery = context["metadata"]["delivery"]
            runtime.handle_delivery_receipt(
                {"content": {**delivery, "version": 1, "outcome": "accepted"}}
            )
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit,
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        assert result["complete_sessions"] == 1
        assert [action for action, _params in actions] == [
            "get_recent_contact",
            "get_friend_msg_history",
            "get_friend_msg_history",
        ]
        assert actions[1][1].get("message_seq") is None
        assert actions[2][1]["message_seq"] == "402"
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[:2] == (
            "303",
            anchor_time,
        )
        runtime.store.close()

    asyncio.run(scenario())


def test_newer_initial_page_keeps_its_cursor_and_walks_back_past_snapshot_time(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        snapshot_time = baseline + 2
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(page_size=3), state_path=tmp_path / "newer-page.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        emitted = []
        newest_page = [_row(304, 404, baseline + 3), _row(305, 405, baseline + 4)]
        older_page = [_row(201, 301, baseline - 1), _row(202, 302, snapshot_time)]

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {
                    "status": "ok",
                    "data": [
                        _recent_row(
                            "1234567890123456789",
                            snapshot_time,
                            latest={
                                "message_type": "private",
                                "self_id": int(ACCOUNT_ID),
                                "sender": {"user_id": 84},
                                "message_id": 202,
                                "message_seq": 302,
                                "time": snapshot_time,
                            },
                        )
                    ],
                }
            assert action == "get_friend_msg_history"
            if params.get("message_seq") is None:
                return {"status": "ok", "data": {"messages": newest_page}}
            assert params["message_seq"] == "404"
            return {"status": "ok", "data": {"messages": older_page}}

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        async def emit(raw, context):
            emitted.append(raw["message_id"])
            delivery = context["metadata"]["delivery"]
            runtime.handle_delivery_receipt(
                {"content": {**delivery, "version": 1, "outcome": "accepted"}}
            )
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit,
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )
        assert result["complete_sessions"] == 1
        history_calls = [params for action, params in actions if action == "get_friend_msg_history"]
        assert history_calls == [
            {"count": 3, "reverse_order": False, "user_id": 84},
            {"count": 3, "reverse_order": True, "user_id": 84, "message_seq": "404"},
        ]
        assert emitted == ["202", "304", "305"]
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[:2] == (
            "305",
            baseline + 4,
        )
        runtime.store.close()

    asyncio.run(scenario())


def test_newer_initial_page_cannot_cross_baseline_before_snapshot_is_proven(tmp_path):
    async def scenario():
        baseline = time.time() - 30
        snapshot_time = baseline + 2
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(page_size=3),
            state_path=tmp_path / "newer-boundary-crossing.sqlite3",
        )
        runtime.discover_targets = AsyncMock(return_value=(PRIVATE,))
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        actions = []
        emitted = []

        async def call_action(action, params, *, expected_connection=None):
            actions.append((action, dict(params)))
            if action == "get_recent_contact":
                return {
                    "status": "ok",
                    "data": [
                        _recent_row(
                            "1234567890123456789",
                            snapshot_time,
                            latest={
                                "message_type": "private",
                                "self_id": int(ACCOUNT_ID),
                                "sender": {"user_id": 84},
                                "time": snapshot_time,
                            },
                        )
                    ],
                }
            assert action == "get_friend_msg_history"
            assert params == {"count": 3, "reverse_order": False, "user_id": 84}
            # The latest row is newer than the snapshot, but the short page
            # also crosses the saved baseline without proving the anchor.
            return {
                "status": "ok",
                "data": {"messages": [_row(305, 405, baseline + 3), _row(201, 301, baseline - 1)]},
            }

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        async def emit(raw, _context):
            emitted.append(raw["message_id"])
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit,
            admission_check=AsyncMock(return_value=True),
        )
        result = await runtime.run_generation(
            account_id=ACCOUNT_ID,
            connection=object(),
            connection_epoch_started_at=baseline,
            account_is_current=lambda: True,
        )

        assert result["complete_sessions"] == 0
        assert set(result["failed_sessions"]) == {"private:84"}
        assert emitted == []
        assert [action for action, _params in actions].count("get_friend_msg_history") == 1
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PRIVATE)[:2] == (None, None)
        runtime.store.close()

    asyncio.run(scenario())


def test_recent_contact_result_is_cached_once_per_run_then_refreshed_for_new_identity(tmp_path):
    async def scenario():
        first_target = PRIVATE
        second_target = HistoryTarget("private", "85")
        runtime = NapcatOfflineHistory(
            platform=PLATFORM, settings=_settings(), state_path=tmp_path / "run-cache.sqlite3"
        )
        runtime.discover_targets = AsyncMock(return_value=(first_target, second_target))
        current_connection = None
        actions: list[tuple[str, str, Any, dict]] = []
        account_by_connection: dict[Any, str] = {}
        run_baselines: dict[str, float] = {}

        async def call_action(action, params, *, expected_connection=None):
            assert expected_connection is current_connection
            account_id = account_by_connection[current_connection]
            actions.append((account_id, action, expected_connection, dict(params)))
            if action == "get_recent_contact":
                return {"status": "error", "data": None}
            assert action == "get_friend_msg_history"
            peer_id = str(params["user_id"])
            baseline = run_baselines[account_id]
            row = _row(700 + int(peer_id), 800 + int(peer_id), baseline - 1, account_id=account_id, peer_id=peer_id)
            return {"status": "ok", "data": {"messages": [row]}}

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                _exact_run_receipt(runtime, message)
            return True

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=lambda _raw, _context: "sent",
            admission_check=AsyncMock(return_value=True),
        )

        for account_id, connection in (
            (ACCOUNT_ID, object()),
            (ACCOUNT_ID, object()),
            ("90002", object()),
        ):
            current_connection = connection
            account_by_connection[connection] = account_id
            baseline = time.time() - 20
            run_baselines[account_id] = baseline
            runtime.store.initialize_scope(BACKEND, account_id, PLATFORM, now=baseline)
            result = await runtime.run_generation(
                account_id=account_id,
                connection=connection,
                connection_epoch_started_at=baseline,
                account_is_current=lambda: True,
            )
            assert result["complete_sessions"] == 2
            assert result["failed_sessions"] == {}

        recent_rows = [row for row in actions if row[1] == "get_recent_contact"]
        assert len(recent_rows) == 3
        assert [row[0] for row in recent_rows] == [ACCOUNT_ID, ACCOUNT_ID, "90002"]
        assert len({row[2] for row in recent_rows}) == 3
        runtime.store.close()

    asyncio.run(scenario())
