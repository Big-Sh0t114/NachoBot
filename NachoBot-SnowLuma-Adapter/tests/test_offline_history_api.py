from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-SnowLuma-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

from ncnk_message.offline_history import HistoryPageError, HistoryTarget, OfflineHistoryStore, scoped_delivery_key  # noqa: E402
from src.config import global_config  # noqa: E402
from src.offline_history import BACKEND, SnowLumaOfflineHistory, normalize_snowluma_messages  # noqa: E402


ACCOUNT = "123456"
PLATFORM = "snowluma-test"
BASELINE = 900.0
CUTOFF = 1000.0
GROUP = HistoryTarget("group", "77")
PRIVATE = HistoryTarget("private", "456")


def _settings(**overrides):
    values = {"enabled": True, "page_size": 10, "max_pages": 5, "retry_delay_sec": 0.1, "receipt_timeout_sec": 0.2}
    values.update(overrides)
    return SimpleNamespace(**values)


def _group_message(message_id: int = -100, timestamp: float = BASELINE + 1):
    return {
        "message_id": message_id, "time": timestamp, "self_id": int(ACCOUNT),
        "message_type": "group", "sub_type": "normal", "group_id": 77,
        "message_seq": 123, "sender": {"user_id": 42},
        "message": [{"type": "text", "data": {"text": "group"}}],
    }


def _private_message(message_id: int, timestamp: float, *, sender_id: int, target_id: int | None = None):
    message = {
        "message_id": message_id, "time": timestamp, "self_id": int(ACCOUNT),
        "message_type": "private", "sender": {"user_id": sender_id},
        "message": [{"type": "text", "data": {"text": str(message_id)}}],
    }
    if target_id is not None:
        message["target_id"] = target_id
    return message


def _runtime(tmp_path: Path, *, call_action=None):
    store = OfflineHistoryStore(tmp_path / "history.sqlite3")
    return SnowLumaOfflineHistory(platform=PLATFORM, settings=_settings(), store=store, call_action=call_action)


@pytest.mark.parametrize(
    ("filter_enabled", "private_type", "private_list", "friends", "banned", "known_private", "expected", "expected_actions"),
    [
        (True, "whitelist", {456, 789}, [], {789}, set(), {"456"}, []),
        (True, "blacklist", {999}, [456, 789, 999], {789}, set(), {"456"}, ["get_friend_list"]),
        (False, "whitelist", {456}, [456, 999], {999}, {888}, {"456", "888"}, ["get_friend_list"]),
    ],
)
def test_discovery_uses_private_policy_and_never_discovers_groups(
    tmp_path, monkeypatch, filter_enabled, private_type, private_list, friends, banned, known_private, expected, expected_actions
):
    calls = []

    async def call_action(action, params, *, expected_connection=None):
        calls.append(action)
        assert action == "get_friend_list"
        return {"status": "ok", "data": [{"user_id": item} for item in friends]}

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        runtime.store.initialize_scope(BACKEND, ACCOUNT, PLATFORM, now=BASELINE)
        runtime.store.register_target(BACKEND, ACCOUNT, PLATFORM, GROUP)
        for friend_id in known_private:
            runtime.store.register_target(BACKEND, ACCOUNT, PLATFORM, HistoryTarget("private", str(friend_id)))
        monkeypatch.setattr(global_config, "chat", SimpleNamespace(
            enable_chat_list_filter=filter_enabled, group_list_type="blacklist", group_list={77},
            private_list_type=private_type, private_list=private_list, ban_user_id=banned,
        ))
        targets = await runtime.discover_targets(ACCOUNT, connection=object())
        assert targets == tuple(HistoryTarget("private", item) for item in sorted(expected))
        assert calls == expected_actions
        runtime.store.close()

    asyncio.run(scenario())


def test_legacy_group_outbox_is_never_pulled_marked_or_emitted(tmp_path, monkeypatch):
    actions, custom_frames, emitted, runtime_ref = [], [], [], {}

    async def call_action(action, params, *, expected_connection=None):
        actions.append((action, params))
        assert action == "get_friend_msg_history"
        return {"status": "ok", "data": []}

    async def send_custom(*, platform, message_type_name, message):
        custom_frames.append((message_type_name, dict(message)))
        if message_type_name == "offline_history_run":
            runtime_ref["runtime"].handle_run_receipt({"content": {
                "version": 1, "backend": message["backend"], "account_id": message["account_id"],
                "platform": platform, "run_id": message["run_id"], "run_token": message["run_token"],
                "phase": message["phase"], "status": "ok",
            }})
        return True

    async def emit_message(raw, context):
        emitted.append((raw, context))
        return "sent"

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        runtime_ref["runtime"] = runtime
        runtime.store.initialize_scope(BACKEND, ACCOUNT, PLATFORM, now=BASELINE)
        runtime.store.stage_page(BACKEND, ACCOUNT, PLATFORM, GROUP, [_group_message()], run_id="legacy-group-run", cutoff_at=CUTOFF)
        runtime.store._db().execute(
            """UPDATE sessions SET checkpoint_id=?, checkpoint_at=?, checkpoint_ids=?
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?""",
            ("-99", BASELINE - 1, json.dumps(["-99"]), BACKEND, ACCOUNT, PLATFORM, "group", GROUP.session_id),
        )
        runtime.store._db().execute(
            """CREATE TABLE snowluma_group_anchors (
                 backend TEXT NOT NULL, account_id TEXT NOT NULL, platform TEXT NOT NULL,
                 session_id TEXT NOT NULL, latest_id TEXT NOT NULL, latest_at REAL NOT NULL,
                 latest_seq INTEGER, latest_observed_at REAL NOT NULL, bootstrap_id TEXT,
                 bootstrap_at REAL, bootstrap_seq INTEGER, checkpoint_seq INTEGER,
                 PRIMARY KEY(backend, account_id, platform, session_id)
               )"""
        )
        legacy_anchor = (BACKEND, ACCOUNT, PLATFORM, GROUP.session_id, "-90", BASELINE - 2, 17,
                         BASELINE - 1, "-91", BASELINE - 3, 16, 15)
        runtime.store._db().execute(
            "INSERT INTO snowluma_group_anchors VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            legacy_anchor,
        )
        runtime.bind(call_action=call_action, send_custom=send_custom, emit_message=emit_message, admission_check=lambda *_args: True)
        monkeypatch.setattr(global_config, "chat", SimpleNamespace(
            enable_chat_list_filter=True, group_list_type="whitelist", group_list={77},
            private_list_type="whitelist", private_list={456}, ban_user_id=set(),
        ))
        runtime._generation_cutoff = CUTOFF
        result = await runtime.run_generation(
            account_id=ACCOUNT, connection=("snow-socket", 1, ACCOUNT),
            connection_epoch_started_at=BASELINE, account_is_current=lambda: True,
        )
        history_runs = [message for kind, message in custom_frames if kind == "offline_history_run"]
        progress = [message for kind, message in custom_frames if kind == "offline_recovery_progress"]
        assert [message["phase"] for message in history_runs] == ["begin", "seal"]
        assert all(target == {"session_kind": "private", "session_id": "456"}
                   for frame in history_runs for target in frame["targets"])
        assert progress and all(message["group_id"] is None and message["user_id"] == 456 for message in progress)
        assert actions == [("get_friend_msg_history", {"user_id": 456, "count": 10, "reverse_order": True})]
        assert emitted == []
        pending = runtime.store.pending_for_target(BACKEND, ACCOUNT, PLATFORM, GROUP)
        assert len(pending) == 1 and pending[0].message_id == "-100"
        assert pending[0].delivery_key not in runtime._receipt_expectations
        assert runtime.store.checkpoint(BACKEND, ACCOUNT, PLATFORM, GROUP) == (
            "-99", BASELINE - 1, frozenset({"-99"})
        )
        assert tuple(runtime.store._db().execute(
            "SELECT * FROM snowluma_group_anchors WHERE session_id=?", (GROUP.session_id,)
        ).fetchone()) == legacy_anchor
        assert result["complete_sessions"] == 1
        runtime.store.close()

    asyncio.run(scenario())


def test_group_history_entrypoints_reject_without_calling_api_or_emitting(tmp_path):
    calls, emitted = [], []

    async def call_action(action, params, *, expected_connection=None):
        calls.append(action)
        raise AssertionError("group history API must be unreachable")

    async def emit_message(raw, context):
        emitted.append(raw)
        return "sent"

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        runtime._emit_message = emit_message
        with pytest.raises(HistoryPageError, match="private targets only"):
            await runtime.fetch_page(target=GROUP, cursor="-100", page_size=10, connection=object(), account_id=ACCOUNT)
        with pytest.raises(HistoryPageError, match="private targets only"):
            runtime.plan_target(GROUP, None, None, frozenset(), None, None, BASELINE)
        assert await runtime.emit_history_item(
            SimpleNamespace(target=GROUP), run_id="legacy", cutoff=CUTOFF, account_id=ACCOUNT, connection=object(),
            connection_epoch_started_at=BASELINE, account_is_current=lambda: True,
        ) is False
        with pytest.raises(HistoryPageError, match="private targets only"):
            await runtime.send_run(run_id="run", run_token="token", cutoff=CUTOFF, targets=(GROUP,), phase="begin",
                                   account_id=ACCOUNT, account_is_current=lambda: True)
        assert calls == [] and emitted == []
        runtime.store.close()

    asyncio.run(scenario())


def test_group_live_decoration_keeps_identity_without_recovery_state(tmp_path):
    async def scenario():
        runtime = _runtime(tmp_path)
        original_at = 987.25
        decorated = await runtime.decorate_live_message(_group_message(-123, original_at),
                                                        {"metadata": {"original_timestamp": original_at}},
                                                        account_id=ACCOUNT)
        delivery = decorated["metadata"]["delivery"]
        assert delivery["recovered"] is False
        assert delivery["session_kind"] == "group"
        assert delivery["delivery_key"] == scoped_delivery_key(BACKEND, ACCOUNT, PLATFORM, GROUP, "-123")
        assert decorated["metadata"]["original_timestamp"] == original_at
        assert runtime.store.known_targets(BACKEND, ACCOUNT, PLATFORM) == ()
        assert runtime.store.live_anchor(BACKEND, ACCOUNT, PLATFORM, GROUP) == (None, None)
        assert not runtime._rerun_event.is_set()
        assert not runtime.store._db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='snowluma_group_anchors'"
        ).fetchone()
        runtime.store.close()

    asyncio.run(scenario())


def test_group_normalizer_is_explicitly_disabled():
    with pytest.raises(HistoryPageError, match="private targets only"):
        normalize_snowluma_messages([_group_message()], target=GROUP, account_id=ACCOUNT, page_size=10)


def test_private_normalizer_keeps_more_than_twenty_rows_and_tied_times():
    source = [_private_message(-index - 1, 950 + index // 2, sender_id=456) for index in range(24)]
    normalized, next_cursor = normalize_snowluma_messages(
        source, target=PRIVATE, account_id=ACCOUNT, page_size=24
    )
    assert len(normalized) == 24
    assert next_cursor == "-1"
    assert normalized[0]["time"] == normalized[1]["time"] == 950
    assert normalized[0]["message_id"] == "-1"
    assert normalized[-1]["message_id"] == "-24"


def test_private_recovery_retries_until_exact_core_receipt_then_seals(tmp_path, monkeypatch):
    actions, custom_frames, emitted, runtime_ref = [], [], [], {}

    async def call_action(action, params, *, expected_connection=None):
        actions.append((action, params))
        assert action == "get_friend_msg_history"
        return {"status": "ok", "data": [_private_message(-12, BASELINE + 5, sender_id=456)]}

    async def send_custom(*, platform, message_type_name, message):
        custom_frames.append((message_type_name, dict(message)))
        if message_type_name == "offline_history_run":
            runtime_ref["runtime"].handle_run_receipt({"content": {
                "version": 1, "backend": message["backend"], "account_id": message["account_id"],
                "platform": platform, "run_id": message["run_id"], "run_token": message["run_token"],
                "phase": message["phase"], "status": "ok",
            }})
        return True

    async def emit_message(raw, context):
        emitted.append((raw, context))
        if len(emitted) == 2:
            delivery = context["metadata"]["delivery"]
            runtime_ref["runtime"].handle_delivery_receipt({
                "content": {**delivery, "version": 1, "outcome": "accepted"}
            })
        return "sent"

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        runtime_ref["runtime"] = runtime
        runtime.settings.receipt_timeout_sec = 1.0
        runtime.store.initialize_scope(BACKEND, ACCOUNT, PLATFORM, now=BASELINE)
        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit_message,
            admission_check=lambda *_args: True,
        )
        monkeypatch.setattr(global_config, "chat", SimpleNamespace(
            enable_chat_list_filter=True,
            group_list_type="whitelist",
            group_list={77},
            private_list_type="whitelist",
            private_list={456},
            ban_user_id=set(),
        ))
        runtime._generation_cutoff = CUTOFF
        result = await runtime.run_generation(
            account_id=ACCOUNT,
            connection=("snow-socket", 2, ACCOUNT),
            connection_epoch_started_at=BASELINE,
            account_is_current=lambda: True,
        )
        deliveries = [context["metadata"]["delivery"] for _raw, context in emitted]
        assert len(emitted) == 2
        assert deliveries[0]["delivery_key"] == deliveries[1]["delivery_key"]
        assert deliveries[0]["receipt_token"] == deliveries[1]["receipt_token"]
        assert emitted[0][0]["time"] == BASELINE + 5
        assert [message["phase"] for kind, message in custom_frames if kind == "offline_history_run"] == ["begin", "seal"]
        assert result["complete_sessions"] == 1 and result["failed_sessions"] == {}
        assert runtime.store.checkpoint(BACKEND, ACCOUNT, PLATFORM, PRIVATE)[:2] == ("-12", BASELINE + 5)
        assert runtime.store.pending_for_target(BACKEND, ACCOUNT, PLATFORM, PRIVATE) == ()
        assert actions == [("get_friend_msg_history", {"user_id": 456, "count": 10, "reverse_order": True})]
        runtime.store.close()

    asyncio.run(scenario())


def test_private_empty_server_history_is_authoritative_end(tmp_path):
    calls = []

    async def call_action(action, params, *, expected_connection=None):
        calls.append((action, params, expected_connection))
        return {"status": "ok", "data": []}

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        page = await runtime.fetch_page(
            target=HistoryTarget("private", "456"),
            cursor=None,
            page_size=10,
            connection=("socket", 1, ACCOUNT),
            account_id=ACCOUNT,
        )
        assert page.messages == ()
        assert page.next_cursor is None
        assert page.end_of_history is True
        assert page.authoritative is True
        assert calls[0][0] == "get_friend_msg_history"
        assert calls[0][1] == {"user_id": 456, "count": 10, "reverse_order": True}
        runtime.store.close()

    asyncio.run(scenario())


def test_private_outgoing_rows_are_retained_as_navigation_identity():
    normalized, next_cursor = normalize_snowluma_messages(
        [
            _private_message(-7, BASELINE + 2, sender_id=123456, target_id=456),
            _private_message(8, BASELINE + 1, sender_id=456),
        ],
        target=HistoryTarget("private", "456"),
        account_id=ACCOUNT,
        page_size=10,
    )
    assert next_cursor == "8"
    assert [row["message_id"] for row in normalized] == ["8", "-7"]
    assert normalized[-1]["_offline_history_outgoing"] is True
    assert normalized[-1]["_offline_history_skip"] is True


def test_private_history_starts_at_server_latest_then_uses_signed_older_cursor(tmp_path):
    first_page = [
        _private_message(-7, BASELINE + 30, sender_id=int(ACCOUNT), target_id=456),
        _private_message(-8, BASELINE + 20, sender_id=456),
    ]
    second_page = [
        _private_message(-8, BASELINE + 20, sender_id=456),
        _private_message(-9, BASELINE + 10, sender_id=456),
    ]
    responses = [first_page, second_page]
    calls = []

    async def call_action(action, params, *, expected_connection=None):
        calls.append((action, params, expected_connection))
        return {"status": "ok", "data": responses[len(calls) - 1]}

    async def scenario():
        runtime = _runtime(tmp_path, call_action=call_action)
        target = HistoryTarget("private", "456")
        newest_scan = await runtime.fetch_page(
            target=target,
            cursor=None,
            page_size=2,
            connection=("socket", 7, ACCOUNT),
            account_id=ACCOUNT,
        )
        older_scan = await runtime.fetch_page(
            target=target,
            cursor=newest_scan.next_cursor,
            page_size=2,
            connection=("socket", 7, ACCOUNT),
            account_id=ACCOUNT,
        )
        assert newest_scan.next_cursor == "-8"
        assert older_scan.next_cursor == "-9"
        assert [message["message_id"] for message in newest_scan.messages] == ["-8", "-7"]
        assert newest_scan.messages[-1]["_offline_history_skip"] is True
        assert calls == [
            ("get_friend_msg_history", {"user_id": 456, "count": 2, "reverse_order": True}, ("socket", 7, ACCOUNT)),
            (
                "get_friend_msg_history",
                {"user_id": 456, "count": 2, "reverse_order": True, "message_id": -8},
                ("socket", 7, ACCOUNT),
            ),
        ]
        runtime.store.close()

    asyncio.run(scenario())
