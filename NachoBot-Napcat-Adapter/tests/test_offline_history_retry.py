import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CORE_ROOT = PROJECT_ROOT.parent / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message.offline_history import HistoryPageError, HistoryTarget  # noqa: E402
from src.offline_history import (  # noqa: E402
    BACKEND,
    NapcatOfflineHistory,
    _RecentContactAnchor,
    _RecentNavigationCache,
    _ScopedLiveAnchor,
    _next_retry_delay,
    core_connection_identity,
)


ACCOUNT_ID = "90001"
PLATFORM = "qq"
PEER_84 = HistoryTarget("private", "84")
PEER_85 = HistoryTarget("private", "85")


def _settings(**overrides):
    values = {
        "enabled": True,
        "page_size": 2,
        "max_pages": 8,
        "retry_delay_sec": 0.1,
        "receipt_timeout_sec": 0.1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _private_row(message_id: int, sequence: int, timestamp: float, peer_id: int):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": int(ACCOUNT_ID),
        "message_id": message_id,
        "message_seq": sequence,
        "time": timestamp,
        "sender": {"user_id": peer_id, "nickname": f"peer-{peer_id}"},
        "message": [{"type": "text", "data": {"text": f"message-{message_id}"}}],
        "raw_message": f"message-{message_id}",
    }


def _core_router():
    socket = object()
    connection = SimpleNamespace(
        _connection_uuid="core-socket",
        _connected_count=1,
        network_driver=SimpleNamespace(active_connections={"core-socket": socket}),
    )

    class CoreClient:
        def __init__(self):
            self.connection = connection

        def is_connected(self):
            return True

    router = SimpleNamespace(clients={PLATFORM: CoreClient()})
    return router, core_connection_identity(router, PLATFORM)[1]


def _bind_receipt_callbacks(runtime, *, emissions, markers, holds, call_action=None):
    async def send_custom(*, platform, message_type_name, message):
        if message_type_name == "offline_history_run":
            markers.append(dict(message))
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
        elif message_type_name == "offline_recovery_progress":
            holds.append((message["user_id"], message["active"]))
        return True

    async def emit_message(raw, context):
        emissions.append((dict(raw), context))
        delivery = context["metadata"]["delivery"]
        runtime.handle_delivery_receipt(
            {"content": {**delivery, "version": 1, "outcome": "accepted"}}
        )
        return "sent"

    runtime.bind(
        call_action=call_action,
        send_custom=send_custom,
        emit_message=emit_message,
        admission_check=lambda *_args, **_kwargs: True,
    )


def _begin_target_sets(markers):
    return [
        [target["session_id"] for target in marker["targets"]]
        for marker in markers
        if marker["phase"] == "begin"
    ]


def test_recovery_retries_only_failed_private_peer_and_keeps_missing_anchor_incomplete(tmp_path):
    async def scenario():
        baseline = time.time() - 120
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-integration.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-socket-1",)
        router, core_identity = _core_router()
        platform_state = {
            "connected": True,
            "account_id": ACCOUNT_ID,
            "identity": platform_identity,
        }
        admitted = (PEER_84, PEER_85)
        discovery_calls = []
        history_calls = {"84": 0, "85": 0}
        recent_calls = 0
        markers = []
        holds = []
        emissions = []
        run_results = []
        generation_count = 0
        wait_count = 0

        async def discover_targets(account_id, expected_connection):
            assert account_id == ACCOUNT_ID
            assert expected_connection is connection
            discovery_calls.append(True)
            return admitted

        runtime.discover_targets = discover_targets

        async def call_action(action, params, *, expected_connection=None):
            nonlocal recent_calls
            assert expected_connection is connection
            if action == "get_recent_contact":
                recent_calls += 1
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            peer_id = str(params["user_id"])
            history_calls[peer_id] += 1
            if peer_id == "84":
                return {
                    "status": "ok",
                    "data": {
                        "messages": [
                            _private_row(8401, 18401, baseline + 1, 84),
                            _private_row(8400, 18400, baseline - 1, 84),
                        ]
                    },
                }
            return {"status": "error", "retcode": 1200, "message": "消息undefined不存在"}

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def counted_real_run_generation(**kwargs):
            nonlocal generation_count
            result = await real_run_generation(**kwargs)
            run_results.append(result)
            generation_count += 1
            return result

        runtime.run_generation = counted_real_run_generation

        async def deterministic_wait(_timeout):
            nonlocal generation_count, wait_count
            wait_count += 1
            if generation_count < 3:
                return  # Let the first two bounded failure retries proceed.
            if generation_count == 3 and wait_count == 4:
                await runtime.decorate_live_message(
                    _private_row(8401, 18401, baseline + 1, 84),
                    {"metadata": {"original_timestamp": baseline + 1}},
                    account_id=ACCOUNT_ID,
                )
                return
            if generation_count == 3:
                assert wait_count == 3  # One idle timeout did not start another generation.
                return
            platform_state["connected"] = False

        runtime._wait_for_retry_event = deterministic_wait

        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert len(discovery_calls) == 4
        assert wait_count == 5
        assert history_calls == {"84": 2, "85": 3}
        assert recent_calls == 1  # A live anchor leaves the shared snapshot and its TTL alone.
        assert _begin_target_sets(markers) == [["84", "85"], ["85"], ["85"], ["84"]]
        assert run_results[0]["complete_sessions"] == 1
        assert run_results[0]["failed_sessions"].keys() == {"private:85"}
        assert all(result["complete_sessions"] == 0 for result in run_results[1:3])
        assert all(set(result["failed_sessions"]) == {"private:85"} for result in run_results[1:3])
        assert run_results[2]["_attempted_target_ids"] == frozenset({"85"})
        assert run_results[3]["_attempted_target_ids"] == frozenset({"84"})
        assert run_results[3]["failed_sessions"] == {}
        assert len(emissions) == 1 and emissions[0][0]["message_id"] == "8401"
        assert emissions[0][1]["metadata"]["original_timestamp"] == baseline + 1
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)[:2] == (
            "8401",
            baseline + 1,
        )
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_85)[:2] == (None, None)
        assert runtime.store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PEER_85) == ()
        assert [entry for entry in holds if entry[1] is True] == [
            (84, True),
            (85, True),
            (85, True),
            (85, True),
            (84, True),
        ]
        runtime.store.close()

    asyncio.run(scenario())


def test_recent_navigation_cache_filters_admission_expires_and_is_socket_scoped():
    now = [100.0]
    cache = _RecentNavigationCache(clock=lambda: now[0])
    anchor = _RecentContactAnchor("84", "raw-84", 1234567890.0, "840", "1840")
    admitted = frozenset({"84", "85"})
    scope = (BACKEND, ACCOUNT_ID, PLATFORM, "socket-a")
    cache.remember(
        scope=scope,
        admitted_target_ids=admitted,
        anchors={"84": anchor},
        invalid_targets={"85": "bad anchor"},
    )

    assert cache.get(scope=scope, admitted_target_ids=frozenset({"84"})) == (
        {"84": anchor},
        {},
    )
    assert cache.get(scope=scope, admitted_target_ids=frozenset({"85"})) == (
        {},
        {"85": "bad anchor"},
    )
    assert cache.get(
        scope=(BACKEND, ACCOUNT_ID, PLATFORM, "socket-b"),
        admitted_target_ids=admitted,
    ) is None
    assert cache.get(
        scope=(BACKEND, "90002", PLATFORM, "socket-a"),
        admitted_target_ids=admitted,
    ) is None
    assert cache.get(
        scope=scope,
        admitted_target_ids=frozenset({"84", "85", "86"}),
    ) is None
    now[0] += 299.9
    assert cache.get(scope=scope, admitted_target_ids=admitted) is not None
    now[0] = 400.0
    assert cache.get(scope=scope, admitted_target_ids=admitted) is None


def test_recent_snapshot_cache_requeries_after_ttl_and_for_a_new_socket(tmp_path):
    async def scenario():
        baseline = time.time() - 60
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-cache-ttl.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        runtime.discover_targets = lambda *_args: asyncio.sleep(0, result=(PEER_84,))
        now = [10.0]
        cache = _RecentNavigationCache(clock=lambda: now[0])
        recent_calls = 0
        connection = object()
        markers = []
        holds = []
        emissions = []

        async def call_action(action, _params, *, expected_connection=None):
            nonlocal recent_calls
            if action == "get_recent_contact":
                recent_calls += 1
                return {"status": "ok", "data": []}
            return {
                "status": "ok",
                "data": {"messages": [_private_row(8400, 18400, baseline - 1, 84)]},
            }

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        base_kwargs = {
            "account_id": ACCOUNT_ID,
            "connection": connection,
            "connection_epoch_started_at": baseline,
            "account_is_current": lambda: True,
            "_recent_navigation_cache": cache,
        }
        for now[0], scope in (
            (10.0, (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-a")),
            (309.9, (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-a")),
            (310.0, (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-a")),
            (310.1, (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-b")),
        ):
            await runtime.run_generation(
                **base_kwargs,
                _recent_navigation_scope=scope,
            )

        assert recent_calls == 3
        runtime.store.close()

    asyncio.run(scenario())


def test_failed_or_malformed_recent_snapshot_is_not_cached(tmp_path):
    async def scenario():
        baseline = time.time() - 60
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-cache-failed-snapshot.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        runtime.discover_targets = lambda *_args: asyncio.sleep(0, result=(PEER_84,))
        cache = _RecentNavigationCache()
        recent_calls = 0
        connection = object()
        markers = []
        holds = []
        emissions = []

        async def call_action(action, _params, *, expected_connection=None):
            nonlocal recent_calls
            if action == "get_recent_contact":
                recent_calls += 1
                if recent_calls == 1:
                    return {"status": "ok", "data": None}
                return {"status": "ok", "data": []}
            return {
                "status": "ok",
                "data": {"messages": [_private_row(8400, 18400, baseline - 1, 84)]},
            }

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        kwargs = {
            "account_id": ACCOUNT_ID,
            "connection": connection,
            "connection_epoch_started_at": baseline,
            "account_is_current": lambda: True,
            "_recent_navigation_cache": cache,
            "_recent_navigation_scope": (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-a"),
        }
        for _ in range(3):
            result = await runtime.run_generation(**kwargs)
            assert result["failed_sessions"] == {}

        assert recent_calls == 2
        runtime.store.close()

    asyncio.run(scenario())


def test_retry_backoff_doubles_to_cap_and_progress_resets():
    delay = 15.0
    observed = [delay]
    for _ in range(7):
        delay = _next_retry_delay(delay, 15.0, progress=False)
        observed.append(delay)
    assert observed == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
    assert _next_retry_delay(240.0, 15.0, progress=True) == 15.0
    assert _next_retry_delay(900.0, 400.0, progress=False) == 400.0


def test_recovery_three_strikes_are_per_target_and_live_signals_do_not_revive_retired_targets(tmp_path):
    async def scenario():
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(retry_delay_sec=15.0),
            state_path=tmp_path / "retry-scheduler.sqlite3",
        )
        platform_identity = ("napcat-scheduler-socket",)
        router, core_identity = _core_router()
        platform_state = {"connected": True, "account_id": ACCOUNT_ID, "identity": platform_identity}
        outcomes = [
            ({"84", "85"}, {"private:84": "unavailable", "private:85": "unavailable"}),
            ({"84", "85"}, {"private:84": "unavailable"}),
            ({"84", "85"}, {"private:84": "unavailable", "private:85": "unavailable"}),
            ({"85"}, {"private:85": "unavailable"}),
            ({"85"}, {}),
        ]
        waits = []
        retry_subsets = []
        excluded_subsets = []
        overlay_ids = []
        run_count = 0

        async def fake_run_generation(**kwargs):
            nonlocal run_count
            run_count += 1
            attempted, result_failures = outcomes.pop(0)
            retry_ids = kwargs["_retry_target_ids"]
            retry_subsets.append(None if retry_ids is None else tuple(sorted(retry_ids)))
            excluded_subsets.append(tuple(sorted(kwargs["_excluded_target_ids"])))
            anchors = kwargs["_live_anchors"]
            overlay_ids.append(
                {target_id: scoped.anchor.message_id for target_id, scoped in anchors.items()}
            )
            if run_count == 2:
                # Arrives during the second scan. The next candidate must
                # retain failed peer 84 and add signaled peer 85.
                live_at = time.time()
                await runtime.decorate_live_message(
                    _private_row(8501, 18501, live_at, 85),
                    {"metadata": {"original_timestamp": live_at}},
                    account_id=ACCOUNT_ID,
                )
            return {
                "complete_sessions": len(attempted) - len(result_failures),
                "failed_sessions": result_failures,
                "_admitted_target_ids": frozenset({"84", "85"}),
                "_attempted_target_ids": frozenset(attempted),
            }

        async def deterministic_wait(timeout):
            waits.append(timeout)
            if len(waits) == 4:
                # Peer 84 is retired and peer 85 has succeeded, so this cue
                # must be discarded without another API/history run.
                live_at = time.time()
                await runtime.decorate_live_message(
                    _private_row(8401, 18401, live_at, 84),
                    {"metadata": {"original_timestamp": live_at}},
                    account_id=ACCOUNT_ID,
                )
            elif len(waits) == 5:
                platform_state["connected"] = False

        runtime.run_generation = fake_run_generation
        runtime._wait_for_retry_event = deterministic_wait
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=object(),
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=time.time(),
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert waits == [15.0, 15.0, 30.0, 30.0, 30.0]
        assert retry_subsets == [
            None,
            ("84", "85"),
            ("84", "85"),
            ("85",),
            ("85",),
        ]
        assert excluded_subsets == [(), (), (), ("84",), ("84",)]
        assert overlay_ids == [{}, {}, {"85": "8501"}, {"85": "8501"}, {"85": "8501"}]
        assert outcomes == []
        runtime.store.close()

    asyncio.run(scenario())


def test_three_failures_stop_after_the_15_and_30_second_retry_gaps(tmp_path):
    async def scenario():
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(retry_delay_sec=15.0),
            state_path=tmp_path / "retry-three-strikes.sqlite3",
        )
        platform_identity = ("napcat-three-strikes-socket",)
        router, core_identity = _core_router()
        platform_state = {"connected": True, "account_id": ACCOUNT_ID, "identity": platform_identity}
        attempts = []
        waits = []

        async def fail_peer_84(**kwargs):
            attempts.append(kwargs["_excluded_target_ids"])
            return {
                "complete_sessions": 0,
                "failed_sessions": {"private:84": "unavailable"},
                "_admitted_target_ids": frozenset({"84"}),
                "_attempted_target_ids": frozenset({"84"}),
            }

        async def deterministic_wait(timeout):
            waits.append(timeout)
            if len(waits) == 3:
                platform_state["connected"] = False

        runtime.run_generation = fail_peer_84
        runtime._wait_for_retry_event = deterministic_wait
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=object(),
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=time.time(),
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert attempts == [frozenset(), frozenset(), frozenset()]
        assert waits == [15.0, 30.0, 30.0]
        runtime.store.close()

    asyncio.run(scenario())


def test_new_connection_generation_resets_three_strike_retirement(tmp_path):
    async def scenario():
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(retry_delay_sec=15.0),
            state_path=tmp_path / "retry-generation-reset.sqlite3",
        )
        router, core_identity = _core_router()
        observed_retry_subsets = []
        observed_excluded_subsets = []
        run_count = 0
        wait_count = 0

        for generation in (1, 2):
            platform_identity = (f"napcat-generation-{generation}",)
            platform_state = {"connected": True, "account_id": ACCOUNT_ID, "identity": platform_identity}
            generation_waits = 0

            async def fail_three_times(**kwargs):
                nonlocal run_count
                retry_ids = kwargs["_retry_target_ids"]
                observed_retry_subsets.append(
                    None if retry_ids is None else tuple(sorted(retry_ids))
                )
                observed_excluded_subsets.append(kwargs["_excluded_target_ids"])
                run_count += 1
                return {
                    "complete_sessions": 0,
                    "failed_sessions": {"private:84": "unavailable"},
                    "_admitted_target_ids": frozenset({"84"}),
                    "_attempted_target_ids": frozenset({"84"}),
                }

            async def disconnect_after_wait(_timeout, state=platform_state):
                nonlocal wait_count, generation_waits
                wait_count += 1
                generation_waits += 1
                if generation_waits == 4:
                    # The third failure retires the peer; two idle timeouts must not rerun it.
                    state["connected"] = False

            runtime.run_generation = fail_three_times
            runtime._wait_for_retry_event = disconnect_after_wait
            await runtime._recover_until_complete(
                account_id=ACCOUNT_ID,
                connection=object(),
                platform_identity=platform_identity,
                core_identity=core_identity,
                connection_epoch_started_at=time.time(),
                get_platform_state=lambda state=platform_state: dict(state),
                router=router,
            )

        assert run_count == 6
        assert observed_retry_subsets == [None, ("84",), ("84",), None, ("84",), ("84",)]
        assert observed_excluded_subsets == [frozenset()] * 6
        assert wait_count == 8
        runtime.store.close()

    asyncio.run(scenario())


def test_fresh_admission_removing_failed_target_does_not_pull_or_hold_it_again(tmp_path):
    async def scenario():
        baseline = time.time() - 60
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-admission.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-socket-admission",)
        router, core_identity = _core_router()
        platform_state = {"connected": True, "account_id": ACCOUNT_ID, "identity": platform_identity}
        admitted = [PEER_85]
        discovery_count = 0
        history_count = 0
        active_holds = []
        markers = []
        emissions = []
        run_count = 0
        recent_count = 0

        async def discover_targets(_account_id, _connection):
            nonlocal discovery_count
            discovery_count += 1
            return tuple(admitted)

        runtime.discover_targets = discover_targets

        async def call_action(action, params, *, expected_connection=None):
            nonlocal history_count, recent_count
            if action == "get_recent_contact":
                recent_count += 1
                return {"status": "ok", "data": []}
            history_count += 1
            return {"status": "error", "retcode": 1200, "message": "history unavailable"}

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=active_holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def update_admission_after_retry(**kwargs):
            nonlocal run_count
            result = await real_run_generation(**kwargs)
            run_count += 1
            if run_count == 1:
                admitted.clear()
            else:
                platform_state["connected"] = False
            return result

        runtime.run_generation = update_admission_after_retry
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert discovery_count == 2
        assert history_count == 1 and recent_count == 1
        assert _begin_target_sets(markers) == [["85"]]
        assert [entry for entry in active_holds if entry[1] is True] == [(85, True)]
        assert emissions == []
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_85)[:2] == (None, None)
        runtime.store.close()

    asyncio.run(scenario())


def test_idle_timeout_does_not_rescan_and_live_signal_checks_only_its_peer(tmp_path):
    async def scenario():
        baseline = time.time() - 60
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-live-signal.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-socket-live-signal",)
        router, core_identity = _core_router()
        platform_state = {"connected": True, "account_id": ACCOUNT_ID, "identity": platform_identity}
        discovery_count = 0
        history_count = 0
        recent_count = 0
        generation_count = 0
        wait_count = 0
        markers = []
        holds = []
        emissions = []
        history_requests = []
        live_anchor = []

        async def discover_targets(_account_id, _connection):
            nonlocal discovery_count
            discovery_count += 1
            return (PEER_84,)

        runtime.discover_targets = discover_targets

        async def call_action(action, params, *, expected_connection=None):
            nonlocal history_count, recent_count
            if action == "get_recent_contact":
                recent_count += 1
                return {"status": "ok", "data": []}
            history_count += 1
            history_requests.append(dict(params))
            if params.get("message_seq") and live_anchor:
                return {
                    "status": "ok",
                    "data": {"messages": [live_anchor[0]]},
                }
            if live_anchor:
                return {
                    "status": "ok",
                    "data": {
                        "messages": [
                            _private_row(8498, 18498, live_anchor[0]["time"], 84),
                            _private_row(8400, 18400, baseline - 1, 84),
                        ]
                    },
                }
            return {
                "status": "ok",
                "data": {"messages": [_private_row(8400, 18400, baseline - 1, 84)]},
            }

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def counted_real_run_generation(**kwargs):
            nonlocal generation_count
            result = await real_run_generation(**kwargs)
            generation_count += 1
            if generation_count == 2:
                platform_state["connected"] = False
            return result

        runtime.run_generation = counted_real_run_generation

        async def deterministic_idle_wait(_timeout):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 1:
                assert generation_count == 1
                return  # Simulate an idle timeout; no history scan should follow.
            assert generation_count == 1
            live_at = time.time()
            live_message = _private_row(8499, 18499, live_at, 84)
            live_anchor.append(live_message)
            await runtime.decorate_live_message(
                live_message,
                {"metadata": {"original_timestamp": live_at}},
                account_id=ACCOUNT_ID,
            )

        runtime._wait_for_retry_event = deterministic_idle_wait
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert wait_count == 2
        assert generation_count == 2
        assert discovery_count == 2
        assert history_count == 3
        assert recent_count == 1
        assert history_requests[-1]["message_seq"] == "8499"
        assert _begin_target_sets(markers) == [["84"], ["84"]]
        runtime.store.close()

    asyncio.run(scenario())


def test_failed_live_anchor_is_reused_for_each_retry_without_recent_refresh(tmp_path):
    async def scenario():
        baseline = time.time() - 120
        live_at = baseline + 2
        live_message = _private_row(-8501, 18501, live_at, 85)
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(retry_delay_sec=0.1),
            state_path=tmp_path / "retry-live-anchor-failure.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-live-anchor-failure-socket",)
        router, core_identity = _core_router()
        platform_state = {
            "connected": True,
            "account_id": ACCOUNT_ID,
            "identity": platform_identity,
        }
        markers = []
        holds = []
        emissions = []
        recent_count = 0
        history_requests = []
        run_kwargs = []
        run_results = []

        async def discover_targets(_account_id, expected_connection):
            assert expected_connection is connection
            return (PEER_84, PEER_85)

        runtime.discover_targets = discover_targets

        async def call_action(action, params, *, expected_connection=None):
            nonlocal recent_count
            assert expected_connection is connection
            if action == "get_recent_contact":
                recent_count += 1
                # A real live event can arrive while the initial full scan is
                # suspended in the snapshot request. The full scan stays full;
                # its cue is consumed by the next targeted pass.
                await runtime.decorate_live_message(
                    live_message,
                    {"metadata": {"original_timestamp": live_at}},
                    account_id=ACCOUNT_ID,
                )
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            peer_id = str(params["user_id"])
            history_requests.append((peer_id, dict(params)))
            if peer_id == "84":
                return {
                    "status": "ok",
                    "data": {
                        "messages": [
                            _private_row(8401, 18401, baseline + 1, 84),
                            _private_row(8400, 18400, baseline - 1, 84),
                        ]
                    },
                }
            return {"status": "error", "retcode": 1200, "message": "history unavailable"}

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def counted_run_generation(**kwargs):
            run_kwargs.append(dict(kwargs))
            result = await real_run_generation(**kwargs)
            run_results.append(result)
            return result

        runtime.run_generation = counted_run_generation
        wait_count = 0

        async def deterministic_wait(_timeout):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                platform_state["connected"] = False

        runtime._wait_for_retry_event = deterministic_wait
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert len(run_kwargs) == 3
        assert run_kwargs[0]["_retry_target_ids"] is None
        assert run_kwargs[0]["_live_anchors"] == {}
        for kwargs in run_kwargs[1:]:
            assert kwargs["_retry_target_ids"] == frozenset({"85"})
            assert kwargs["_live_anchors"]["85"].anchor.message_id == "-8501"
            assert kwargs["_live_anchors"]["85"].anchor.timestamp == live_at
        assert recent_count == 1
        assert [peer for peer, _params in history_requests].count("84") == 1
        assert [peer for peer, _params in history_requests].count("85") == 5
        assert [params.get("message_seq") for peer, params in history_requests if peer == "85"] == [
            None,
            None,
            "-8501",
            None,
            "-8501",
        ]
        assert _begin_target_sets(markers) == [["84", "85"], ["85"], ["85"]]
        assert all(set(result["failed_sessions"]) == {"private:85"} for result in run_results)
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_85)[:2] == (None, None)
        runtime.store.close()

    asyncio.run(scenario())


def test_expired_cache_live_anchor_targets_new_peer_and_preserves_same_second_ids(
    tmp_path, monkeypatch
):
    async def scenario():
        baseline = time.time() - 60
        cache_clock = [10.0]
        monkeypatch.setattr(
            "src.offline_history._RecentNavigationCache",
            lambda: _RecentNavigationCache(clock=lambda: cache_clock[0]),
        )
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-live-new-peer.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-live-new-peer-socket",)
        router, core_identity = _core_router()
        platform_state = {
            "connected": True,
            "account_id": ACCOUNT_ID,
            "identity": platform_identity,
        }
        admitted = [PEER_84]
        markers = []
        holds = []
        emissions = []
        history_requests = []
        recent_count = 0
        run_count = 0
        overlays = []
        live_at = baseline + 2
        live_message = _private_row(-8601, 18601, live_at, 86)

        async def discover_targets(_account_id, expected_connection):
            assert expected_connection is connection
            return tuple(admitted)

        runtime.discover_targets = discover_targets

        async def call_action(action, params, *, expected_connection=None):
            nonlocal recent_count
            assert expected_connection is connection
            if action == "get_recent_contact":
                recent_count += 1
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            peer_id = str(params["user_id"])
            history_requests.append((peer_id, dict(params)))
            if peer_id == "84":
                return {
                    "status": "ok",
                    "data": {
                        "messages": [
                            _private_row(8401, 18401, baseline + 1, 84),
                            _private_row(8400, 18400, baseline - 1, 84),
                        ]
                    },
                }
            assert peer_id == "86"
            if params.get("message_seq") == "-8601":
                return {
                    "status": "ok",
                    "data": {"messages": [live_message]},
                }
            return {
                "status": "ok",
                "data": {
                    "messages": [
                        _private_row(-8602, 18602, live_at, 86),
                        _private_row(8600, 18600, baseline - 1, 86),
                    ]
                },
            }

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def counted_run_generation(**kwargs):
            nonlocal run_count
            overlays.append(dict(kwargs["_live_anchors"]))
            result = await real_run_generation(**kwargs)
            run_count += 1
            if run_count == 2:
                platform_state["connected"] = False
            return result

        runtime.run_generation = counted_run_generation
        wait_count = 0

        async def signal_new_peer(_timeout):
            nonlocal wait_count
            wait_count += 1
            assert wait_count == 1 and run_count == 1
            # Expire the startup snapshot and admit a peer after baseline.
            # Its validated negative short ID remains a direct history cursor.
            cache_clock[0] = 311.0
            admitted.append(HistoryTarget("private", "86"))
            same_second_message = _private_row(-8602, 18602, live_at, 86)
            await runtime.decorate_live_message(
                same_second_message,
                {"metadata": {"original_timestamp": live_at}},
                account_id=ACCOUNT_ID,
            )
            await runtime.decorate_live_message(
                live_message,
                {"metadata": {"original_timestamp": live_at}},
                account_id=ACCOUNT_ID,
            )

        runtime._wait_for_retry_event = signal_new_peer
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert run_count == 2
        assert recent_count == 1
        assert overlays[0] == {}
        assert overlays[1]["86"].anchor.message_id == "-8601"
        assert [peer for peer, _params in history_requests].count("84") == 1
        assert [peer for peer, _params in history_requests].count("86") == 2
        assert history_requests[-1][1]["message_seq"] == "-8601"
        assert _begin_target_sets(markers) == [["84"], ["86"]]
        assert [entry for entry in holds if entry[1] is True] == [(84, True), (86, True)]
        emitted_ids = {str(raw["message_id"]) for raw, _context in emissions}
        assert {"-8601", "-8602"}.issubset(emitted_ids)
        for raw, context in emissions:
            if str(raw["message_id"]) in {"-8601", "-8602"}:
                assert context["metadata"]["original_timestamp"] == live_at
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)[:2] == (
            "8401",
            baseline + 1,
        )
        peer_86_checkpoint = runtime.store.checkpoint(
            BACKEND, ACCOUNT_ID, PLATFORM, HistoryTarget("private", "86")
        )
        assert peer_86_checkpoint[1] == live_at
        assert peer_86_checkpoint[2] == frozenset({"-8601", "-8602"})
        runtime.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message_id", "timestamp"),
    [(None, 1234.0), ("8401", 0.0), ("8401", float("nan"))],
)
def test_malformed_live_anchor_overlay_fails_before_upstream_or_checkpoint_changes(
    tmp_path, message_id, timestamp
):
    async def scenario():
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-malformed-live-anchor.sqlite3",
        )
        connection = object()
        generation_scope = (BACKEND, ACCOUNT_ID, PLATFORM, id(connection), "socket-a")
        api_calls = []
        discovery_calls = []

        async def discover_targets(*_args):
            discovery_calls.append(True)
            return (PEER_84,)

        async def call_action(action, _params, *, expected_connection=None):
            api_calls.append(action)
            return {"status": "ok", "data": []}

        runtime.discover_targets = discover_targets
        runtime.bind(
            call_action=call_action,
            send_custom=lambda **_kwargs: asyncio.sleep(0, result=True),
            emit_message=lambda *_args: asyncio.sleep(0, result="sent"),
            admission_check=lambda *_args, **_kwargs: True,
        )
        overlay = _ScopedLiveAnchor(
            account_id=ACCOUNT_ID,
            generation_scope=generation_scope,
            anchor=_RecentContactAnchor(
                target_id="84",
                raw_message_id=message_id or "8401",
                timestamp=timestamp,
                message_id=message_id,
            ),
        )

        with pytest.raises(HistoryPageError):
            await runtime.run_generation(
                account_id=ACCOUNT_ID,
                connection=connection,
                connection_epoch_started_at=time.time() - 10,
                account_is_current=lambda: True,
                _recent_navigation_scope=generation_scope,
                _live_anchors={"84": overlay},
            )

        assert discovery_calls == []
        assert api_calls == []
        malformed_checkpoint = runtime.store.checkpoint(
            BACKEND, ACCOUNT_ID, PLATFORM, PEER_84
        )
        assert malformed_checkpoint[0] is None
        assert malformed_checkpoint[2] == frozenset()
        runtime.store.close()

    asyncio.run(scenario())


def test_live_timestamp_mismatch_keeps_delivery_identity_without_waking_history(tmp_path):
    async def scenario():
        baseline = time.time() - 60
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(),
            state_path=tmp_path / "retry-live-time-mismatch.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-live-time-mismatch-socket",)
        router, core_identity = _core_router()
        platform_state = {
            "connected": True,
            "account_id": ACCOUNT_ID,
            "identity": platform_identity,
        }
        markers = []
        holds = []
        emissions = []
        recent_count = 0
        history_count = 0
        run_count = 0

        async def discover_targets(*_args):
            return (PEER_84,)

        runtime.discover_targets = discover_targets

        async def call_action(action, _params, *, expected_connection=None):
            nonlocal recent_count, history_count
            if action == "get_recent_contact":
                recent_count += 1
                return {"status": "ok", "data": []}
            history_count += 1
            return {
                "status": "ok",
                "data": {"messages": [_private_row(8400, 18400, baseline - 1, 84)]},
            }

        _bind_receipt_callbacks(
            runtime,
            emissions=emissions,
            markers=markers,
            holds=holds,
            call_action=call_action,
        )
        real_run_generation = runtime.run_generation

        async def counted_run_generation(**kwargs):
            nonlocal run_count
            result = await real_run_generation(**kwargs)
            run_count += 1
            return result

        runtime.run_generation = counted_run_generation
        waits = 0
        decorated_contexts = []

        async def signal_mismatched_time(_timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                raw_time = baseline + 3
                context_time = baseline + 2
                decorated_contexts.append(
                    await runtime.decorate_live_message(
                        _private_row(8499, 18499, raw_time, 84),
                        {"metadata": {"original_timestamp": context_time}},
                        account_id=ACCOUNT_ID,
                    )
                )
                assert not runtime._rerun_event.is_set()
                assert runtime._pending_live_cues == {}
            else:
                platform_state["connected"] = False

        runtime._wait_for_retry_event = signal_mismatched_time
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        assert run_count == 1
        assert recent_count == 1 and history_count == 1
        assert decorated_contexts[0]["metadata"]["delivery"]["message_id"] == "8499"
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)[:2] == (
            None,
            None,
        )
        runtime.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_status", ["failed", "sent"])
def test_delivery_receipt_failures_reach_three_strike_scheduler_without_inner_resends(
    tmp_path, failure_status
):
    async def scenario():
        baseline = time.time() - 120
        runtime = NapcatOfflineHistory(
            platform=PLATFORM,
            settings=_settings(receipt_timeout_sec=0.1, retry_delay_sec=0.1),
            state_path=tmp_path / "delivery-three-strikes.sqlite3",
        )
        runtime.store.initialize_scope(BACKEND, ACCOUNT_ID, PLATFORM, now=baseline)
        connection = object()
        platform_identity = ("napcat-delivery-socket",)
        router, core_identity = _core_router()
        platform_state = {
            "connected": True,
            "account_id": ACCOUNT_ID,
            "identity": platform_identity,
        }
        admitted = (PEER_84, PEER_85)
        runtime.discover_targets = lambda *_args: asyncio.sleep(0, result=admitted)
        history_calls = {"84": 0, "85": 0}
        markers = []
        holds = []
        emissions = []
        run_results = []

        async def call_action(action, params, *, expected_connection=None):
            assert expected_connection is connection
            if action == "get_recent_contact":
                return {"status": "ok", "data": []}
            assert action == "get_friend_msg_history"
            peer_id = str(params["user_id"])
            history_calls[peer_id] += 1
            peer_number = int(peer_id)
            return {
                "status": "ok",
                "data": {
                    "messages": [
                        _private_row(peer_number * 100 + 1, peer_number * 1000 + 1, baseline + 1, peer_number),
                        _private_row(peer_number * 100, peer_number * 1000, baseline - 1, peer_number),
                    ]
                },
            }

        async def send_custom(*, platform, message_type_name, message):
            if message_type_name == "offline_history_run":
                markers.append(dict(message))
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
            elif message_type_name == "offline_recovery_progress":
                holds.append((message["user_id"], message["active"]))
            return True

        async def emit_message(raw, context):
            delivery = context["metadata"]["delivery"]
            session_id = delivery["session_id"]
            emissions.append((session_id, str(raw["message_id"])))
            if session_id == "84" and failure_status == "failed":
                return "failed"
            if session_id == "85":
                runtime.handle_delivery_receipt(
                    {"content": {**delivery, "version": 1, "outcome": "accepted"}}
                )
            return "sent"

        runtime.bind(
            call_action=call_action,
            send_custom=send_custom,
            emit_message=emit_message,
            admission_check=lambda *_args, **_kwargs: True,
        )
        initial_failed_checkpoint = runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)
        real_run_generation = runtime.run_generation

        async def counted_run_generation(**kwargs):
            result = await real_run_generation(**kwargs)
            run_results.append(result)
            return result

        runtime.run_generation = counted_run_generation
        wait_count = 0

        async def deterministic_wait(_timeout):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 3:
                live_at = time.time()
                await runtime.decorate_live_message(
                    _private_row(8401, 18401, live_at, 84),
                    {"metadata": {"original_timestamp": live_at}},
                    account_id=ACCOUNT_ID,
                )
            elif wait_count == 4:
                platform_state["connected"] = False

        runtime._wait_for_retry_event = deterministic_wait
        await runtime._recover_until_complete(
            account_id=ACCOUNT_ID,
            connection=connection,
            platform_identity=platform_identity,
            core_identity=core_identity,
            connection_epoch_started_at=baseline,
            get_platform_state=lambda: dict(platform_state),
            router=router,
        )

        failed_emissions = [entry for entry in emissions if entry[0] == "84"]
        successful_emissions = [entry for entry in emissions if entry[0] == "85"]
        assert failed_emissions == [("84", "8401")] * 3
        assert successful_emissions == [("85", "8501")]
        assert history_calls == {"84": 3, "85": 1}
        assert len(run_results) == 3
        assert run_results[0]["complete_sessions"] == 1
        assert run_results[0]["failed_sessions"] == {"private:84": "history delivery was not acknowledged"}
        assert all(result["complete_sessions"] == 0 for result in run_results[1:3])
        assert all(set(result["failed_sessions"]) == {"private:84"} for result in run_results[1:3])
        failed_checkpoint_after = runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)
        assert failed_checkpoint_after[0] is None
        assert failed_checkpoint_after[2] == initial_failed_checkpoint[2] == frozenset()
        assert runtime.store.checkpoint(BACKEND, ACCOUNT_ID, PLATFORM, PEER_85)[:2] == (
            "8501",
            baseline + 1,
        )
        assert [item.message_id for item in runtime.store.pending_for_target(BACKEND, ACCOUNT_ID, PLATFORM, PEER_84)] == [
            "8401"
        ]
        begin_targets = _begin_target_sets(markers)
        assert begin_targets == [["84", "85"], ["84"], ["84"]]
        assert len([entry for entry in holds if entry[1] is True]) == 4
        assert len([entry for entry in holds if entry[1] is False]) == 4
        runtime.store.close()

    asyncio.run(scenario())
