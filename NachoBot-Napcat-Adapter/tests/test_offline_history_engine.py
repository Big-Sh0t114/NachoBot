from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[2] / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.append(str(CORE_ROOT))

from ncnk_message.offline_history import (  # noqa: E402
    HistoryPage,
    HistoryTarget,
    OfflineHistoryStore,
    OfflineHistorySynchronizer,
    ScanPlan,
)


def _message(message_id: str, timestamp: float, *, target: HistoryTarget, outgoing: bool = False):
    if target.kind == "group":
        return {
            "message_type": "group",
            "group_id": target.session_id,
            "message_id": message_id,
            "time": timestamp,
            "sender": {"user_id": "bot" if outgoing else "user"},
            "_offline_history_skip": outgoing,
        }
    return {
        "message_type": "private",
        "message_id": message_id,
        "time": timestamp,
        "sender": {"user_id": "bot" if outgoing else target.session_id},
        **({"target_id": target.session_id, "_offline_history_outgoing": True, "_offline_history_skip": True} if outgoing else {}),
    }


def _checkpoint(store: OfflineHistoryStore, target: HistoryTarget, *, backend="test", platform="QQ"):
    store.initialize_scope(backend, "account", platform, now=100)
    store.register_target(backend, "account", platform, target, discovered_at=100)
    store.complete_target(
        backend,
        "account",
        platform,
        target,
        checkpoint_id="A",
        checkpoint_timestamp=100,
        boundary_ids={"A"},
    )


def _synchronizer(path: Path, *, clock, page_size=2, max_pages=10, backend="test"):
    return OfflineHistorySynchronizer(
        OfflineHistoryStore(path),
        backend=backend,
        platform="QQ",
        page_size=page_size,
        max_pages=max_pages,
        retry_delay=0.01,
        marker_refresh_sec=10,
        clock=lambda: clock[0],
    )


def _run(sync, target, pages, *, plan_target=None, targets=None, hold=None, order=None, run_callback=None):
    emitted = []
    page_calls = []
    page_iter = iter(pages)

    async def fetch(_target, cursor, _size, direction):
        page_calls.append((cursor, direction))
        page = next(page_iter)
        if order is not None:
            order.append(("fetch", _target.session_id))
        return page

    async def send_hold(route, progress_id, cutoff, active, _run_id, _route_total):
        if hold is not None:
            await hold(route, progress_id, cutoff, active)

    async def emit(item, _run_id, _cutoff):
        emitted.append(item.message_id)
        if order is not None:
            order.append(("emit", item.target.session_id))
        return True

    result = asyncio.run(
        sync.run(
            account_id="account",
            targets=targets or (target,),
            fetch_page=fetch,
            send_hold=send_hold,
            emit=emit,
            plan_target=plan_target,
            send_run=run_callback,
        )
    )
    return result, emitted, page_calls


def test_first_enable_baseline_replays_only_after_enable_and_normalizes_ms(tmp_path):
    clock = [1_700_000_100]
    target = HistoryTarget("private", "peer")
    sync = _synchronizer(tmp_path / "state.sqlite3", clock=clock)
    sync.store.initialize_scope("test", "account", "QQ", now=1_700_000_000)
    messages = (
        _message("old", 1_699_999_999, target=target),
        _message("baseline", 1_700_000_000_000, target=target),
        _message("new", 1_700_000_050_000, target=target),
    )
    result, emitted, _ = _run(
        sync,
        target,
        [HistoryPage(messages, next_cursor="oldest", end_of_history=True)],
    )
    assert result["baseline_at"] == 1_700_000_000
    assert emitted == ["new"]
    assert sync.store.checkpoint("test", "account", "QQ", target)[:2] == ("new", 1_700_000_050)


def test_forward_inclusive_anchor_paginates_all_pages_and_preserves_ties(tmp_path):
    target = HistoryTarget("group", "77")
    store = OfflineHistoryStore(tmp_path / "forward.sqlite3")
    _checkpoint(store, target)
    clock = [200]
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", page_size=3, retry_delay=0.01, clock=lambda: clock[0]
    )
    pages = [
        HistoryPage(
            (_message("A", 100, target=target), _message("B", 110, target=target), _message("C", 120, target=target)),
            next_cursor="C",
        ),
        HistoryPage(
            (_message("C", 120, target=target), _message("D", 130, target=target), _message("E", 140, target=target)),
            next_cursor="E",
            end_of_history=True,
        ),
    ]

    def plan(route, checkpoint_id, checkpoint_at, boundary_ids, live_id, live_at, baseline):
        return ScanPlan("forward", checkpoint_id or live_id, checkpoint_id or live_id, boundary_ids | frozenset({checkpoint_id or live_id}), checkpoint_at or live_at or baseline, True)

    result, emitted, calls = _run(sync, target, pages, plan_target=plan)
    assert result["failed_sessions"] == {}
    assert emitted == ["B", "C", "D", "E"]
    assert calls == [("A", "forward"), ("C", "forward")]
    assert store.checkpoint("test", "account", "QQ", target) == ("E", 140, frozenset({"E"}))


def test_backward_checkpoint_ties_keep_new_ids_at_same_second(tmp_path):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "ties.sqlite3")
    _checkpoint(store, target)
    sync = _synchronizer(tmp_path / "ties.sqlite3", clock=[120])
    messages = (
        _message("A", 100, target=target),
        _message("new-tie", 100, target=target),
        _message("later", 110, target=target),
    )
    _result, emitted, _ = _run(sync, target, [HistoryPage(messages, "A")])
    assert emitted == ["new-tie", "later"]
    assert sync.store.checkpoint("test", "account", "QQ", target) == (
        "later",
        110,
        frozenset({"later"}),
    )


def test_incomplete_or_capped_scan_never_emits_staged_prefix_or_advances(tmp_path):
    target = HistoryTarget("private", "peer")
    state = tmp_path / "cap.sqlite3"
    store = OfflineHistoryStore(state)
    _checkpoint(store, target)
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", page_size=1, max_pages=1, retry_delay=0.01, clock=lambda: 200
    )
    page = HistoryPage((_message("B", 110, target=target),), next_cursor="B")
    result, emitted, _ = _run(sync, target, [page])
    assert result["complete_sessions"] == 0
    assert "private:peer" in result["failed_sessions"]
    assert emitted == []
    assert store.checkpoint("test", "account", "QQ", target) == ("A", 100, frozenset({"A"}))
    assert [item.message_id for item in store.pending_for_target("test", "account", "QQ", target)] == ["B"]


@pytest.mark.parametrize(
    "page",
    [
        HistoryPage((), next_cursor=None),
        HistoryPage((_message("B", 110, target=HistoryTarget("private", "peer")),), "A"),
    ],
)
def test_empty_or_repeated_page_is_incomplete(tmp_path, page):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / f"invalid-{id(page)}.sqlite3")
    _checkpoint(store, target)
    sync = OfflineHistorySynchronizer(store, backend="test", platform="QQ", clock=lambda: 200)
    result, emitted, _ = _run(sync, target, [page])
    assert result["complete_sessions"] == 0
    assert emitted == []


def test_fetch_all_sessions_before_any_emit_and_isolate_failed_session(tmp_path):
    first = HistoryTarget("private", "first")
    second = HistoryTarget("private", "second")
    store = OfflineHistoryStore(tmp_path / "sessions.sqlite3")
    store.initialize_scope("test", "account", "QQ", now=100)
    sync = OfflineHistorySynchronizer(store, backend="test", platform="QQ", clock=lambda: 120)
    order = []

    async def fetch(target, _cursor, _size, _direction):
        order.append(("fetch", target.session_id))
        if target == first:
            raise RuntimeError("temporary API failure")
        return HistoryPage((_message("B", 110, target=second),), "B", end_of_history=True)

    async def hold(*_args):
        return None

    async def emit(item, _run_id, _cutoff):
        order.append(("emit", item.target.session_id))
        return True

    result = asyncio.run(
        sync.run(
            account_id="account",
            targets=(first, second),
            fetch_page=fetch,
            send_hold=hold,
            emit=emit,
        )
    )
    assert result["complete_sessions"] == 1
    assert result["failed_sessions"] == {"private:first": "temporary API failure"}
    assert order == [("fetch", "first"), ("fetch", "second"), ("emit", "second")]


def test_marker_setup_failure_closes_every_attempted_route(tmp_path):
    first = HistoryTarget("private", "first")
    second = HistoryTarget("private", "second")
    sync = _synchronizer(tmp_path / "holds.sqlite3", clock=[120])
    markers = []

    async def hold(target, progress_id, _cutoff, active, _run_id, _route_total):
        markers.append((target.session_id, active))
        if target == second and active:
            raise RuntimeError("core disconnected")

    async def fetch(*_args):
        raise AssertionError("history must not fetch when hold setup failed")

    async def emit(*_args):
        raise AssertionError("nothing may emit")

    with pytest.raises(RuntimeError, match="core disconnected"):
        asyncio.run(
            sync.run(
                account_id="account",
                targets=(first, second),
                fetch_page=fetch,
                send_hold=hold,
                emit=emit,
            )
        )
    assert markers == [("first", True), ("second", True), ("first", False), ("second", False)]


def test_bidirectional_private_rows_advance_cursor_but_only_incoming_emits(tmp_path):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "bidirectional.sqlite3")
    _checkpoint(store, target)
    sync = _synchronizer(tmp_path / "bidirectional.sqlite3", clock=[120])
    page = HistoryPage(
        (
            _message("A", 100, target=target),
            _message("incoming", 110, target=target),
            _message("bot-reply", 115, target=target, outgoing=True),
        ),
        "A",
    )
    _result, emitted, _ = _run(sync, target, [page])
    assert emitted == ["incoming"]
    assert sync.store.checkpoint("test", "account", "QQ", target) == (
        "bot-reply",
        115,
        frozenset({"bot-reply"}),
    )


def test_begin_ack_failure_does_not_start_api_fetch_or_emit(tmp_path):
    target = HistoryTarget("private", "peer")
    sync = _synchronizer(tmp_path / "begin-ack.sqlite3", clock=[120])
    calls = []

    async def fetch(*_args):
        calls.append("fetch")
        return HistoryPage((_message("new", 110, target=target),), None, end_of_history=True)

    async def hold(*_args):
        calls.append("hold")

    async def emit(*_args):
        calls.append("emit")
        return True

    async def run_callback(_run_id, _token, _cutoff, _targets, phase):
        calls.append(phase)
        if phase == "begin":
            raise ConnectionError("Core did not durably register the run")

    with pytest.raises(ConnectionError, match="durably register"):
        asyncio.run(
            sync.run(
                account_id="account",
                targets=(target,),
                fetch_page=fetch,
                send_hold=hold,
                emit=emit,
                send_run=run_callback,
            )
        )
    assert calls == ["begin"]
    assert sync.store.checkpoint("test", "account", "QQ", target)[0] is None
    assert sync.store.pending_for_target("test", "account", "QQ", target) == ()


def test_seal_ack_failure_keeps_checkpoint_until_restart_retry_seal(tmp_path):
    target = HistoryTarget("private", "peer")
    state_path = tmp_path / "seal-ack.sqlite3"
    first_store = OfflineHistoryStore(state_path)
    _checkpoint(first_store, target)
    first = OfflineHistorySynchronizer(
        first_store, backend="test", platform="QQ", page_size=2, retry_delay=0.01, clock=lambda: 120
    )
    page = HistoryPage(
        (_message("A", 100, target=target), _message("B", 110, target=target)),
        "A",
    )
    emitted = []
    phases = []

    async def fetch(*_args):
        return page

    async def hold(*_args):
        return None

    async def emit(item, _run_id, _cutoff):
        emitted.append(item.message_id)
        return True

    async def lost_seal(_run_id, _token, _cutoff, _targets, phase):
        phases.append(phase)
        if phase == "seal":
            raise ConnectionError("Core did not confirm durable run seal")

    with pytest.raises(ConnectionError, match="durable run seal"):
        asyncio.run(
            first.run(
                account_id="account",
                targets=(target,),
                fetch_page=fetch,
                send_hold=hold,
                emit=emit,
                send_run=lost_seal,
            )
        )
    assert phases == ["begin", "seal"]
    assert emitted == ["B"]
    assert first_store.checkpoint("test", "account", "QQ", target) == ("A", 100, frozenset({"A"}))
    outbox_after_disconnect = first_store._db().execute(
        "SELECT message_id, state FROM outbox WHERE session_id=?", (target.session_id,)
    ).fetchall()
    assert [(row[0], row[1]) for row in outbox_after_disconnect] == [("B", "acked")]
    first_store.close()

    # A fresh engine retries from disk. The Core row was durably accepted, so
    # it must not be emitted twice; checkpoint advances only after seal ACK.
    restarted_store = OfflineHistoryStore(state_path)
    restarted = OfflineHistorySynchronizer(
        restarted_store, backend="test", platform="QQ", page_size=2, retry_delay=0.01, clock=lambda: 130
    )
    restarted_phases = []

    async def successful_seal(_run_id, _token, _cutoff, _targets, phase):
        restarted_phases.append(phase)

    _result, retried_messages, _calls = _run(
        restarted,
        target,
        [page],
        run_callback=successful_seal,
    )
    assert restarted_phases == ["begin", "seal"]
    assert retried_messages == []
    assert restarted_store.checkpoint("test", "account", "QQ", target) == (
        "B",
        110,
        frozenset({"B"}),
    )
    assert restarted_store._db().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_live_anchor_keeps_only_latest_original_boundary_per_session(tmp_path):
    target = HistoryTarget("group", "77")
    store = OfflineHistoryStore(tmp_path / "live-anchor.sqlite3")
    store.record_live_seen("test", "account", "QQ", target, "newest", 110, observed_at=10)
    store.record_live_seen("test", "account", "QQ", target, "delayed-old", 105, observed_at=20)
    assert store.live_anchor("test", "account", "QQ", target) == ("newest", 110)
    store.record_live_seen("test", "account", "QQ", target, "newest-tie", 110, observed_at=30)
    assert store.live_anchor("test", "account", "QQ", target) == ("newest-tie", 110)
    rows = store._db().execute("SELECT COUNT(*) FROM live_seen WHERE session_id=?", (target.session_id,)).fetchone()[0]
    assert rows == 1


def test_pending_outbox_query_can_stream_bounded_chunks(tmp_path):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "bounded-outbox.sqlite3")
    store.initialize_scope("test", "account", "QQ", now=100)
    store.register_target("test", "account", "QQ", target, discovered_at=100)
    messages = tuple(_message(f"m{index}", 100 + index, target=target) for index in range(5))
    store.stage_page(
        "test",
        "account",
        "QQ",
        target,
        messages,
        run_id="run",
        cutoff_at=200,
    )
    first = store.pending_for_target("test", "account", "QQ", target, limit=2)
    assert [item.message_id for item in first] == ["m0", "m1"]


def test_opt_out_delivery_failure_attempts_each_row_once_and_continues_peers(tmp_path):
    first = HistoryTarget("private", "first")
    second = HistoryTarget("private", "second")
    store = OfflineHistoryStore(tmp_path / "delivery-failure.sqlite3")
    _checkpoint(store, first)
    _checkpoint(store, second)
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", page_size=10, retry_delay=0.01, clock=lambda: 130
    )
    pages = {
        first: HistoryPage(
            (
                _message("A", 100, target=first),
                _message("B", 110, target=first),
                _message("C", 120, target=first),
            ),
            "A",
        ),
        second: HistoryPage(
            (_message("A", 100, target=second), _message("D", 110, target=second)),
            "A",
        ),
    }
    attempts = []
    markers = []
    holds = []
    session_errors = []

    async def fetch(target, _cursor, _size, _direction):
        return pages[target]

    async def hold(target, progress_id, _cutoff, active, _run_id, _route_total):
        holds.append((target, progress_id, active))

    async def emit(item, _run_id, _cutoff):
        attempts.append((item.target.session_id, item.message_id))
        return item.message_id != "C"

    async def run_marker(_run_id, _token, _cutoff, targets, phase):
        markers.append((phase, targets))

    result = asyncio.run(
        sync.run(
            account_id="account",
            targets=(first, second),
            fetch_page=fetch,
            send_hold=hold,
            emit=emit,
            send_run=run_marker,
            on_session_error=lambda target, error: session_errors.append((target, str(error))),
            retry_delivery_failures=False,
        )
    )

    assert attempts == [("first", "B"), ("first", "C"), ("second", "D")]
    assert result["complete_sessions"] == 1
    assert result["failed_sessions"] == {"private:first": "history delivery was not acknowledged"}
    assert session_errors == [(first, "history delivery was not acknowledged")]
    assert markers[0][0] == "begin"
    assert markers[1] == ("seal", (second,))
    assert [active for _target, _progress_id, active in holds] == [True, True, False, False]
    assert store.checkpoint("test", "account", "QQ", first) == ("A", 100, frozenset({"A"}))
    assert store.checkpoint("test", "account", "QQ", second) == ("D", 110, frozenset({"D"}))
    assert [item.message_id for item in store.pending_for_target("test", "account", "QQ", first)] == ["C"]


def test_opt_out_delivery_exception_isolated_to_target(tmp_path):
    failed = HistoryTarget("private", "failed")
    peer = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "delivery-exception.sqlite3")
    _checkpoint(store, failed)
    _checkpoint(store, peer)
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", retry_delay=0.01, clock=lambda: 120
    )
    pages = {
        failed: HistoryPage((_message("A", 100, target=failed), _message("B", 110, target=failed)), "A"),
        peer: HistoryPage((_message("A", 100, target=peer), _message("C", 110, target=peer)), "A"),
    }
    attempts = []

    async def emit(item, _run_id, _cutoff):
        attempts.append(item.message_id)
        if item.target == failed:
            raise RuntimeError("Core ingress failed")
        return True

    result = asyncio.run(
        sync.run(
            account_id="account",
            targets=(failed, peer),
            fetch_page=lambda target, *_args: asyncio.sleep(0, result=pages[target]),
            send_hold=lambda *_args: asyncio.sleep(0),
            emit=emit,
            retry_delivery_failures=False,
        )
    )
    assert attempts == ["B", "C"]
    assert result["complete_sessions"] == 1
    assert result["failed_sessions"] == {"private:failed": "Core ingress failed"}
    assert [item.message_id for item in store.pending_for_target("test", "account", "QQ", failed)] == ["B"]


def test_delivery_cancellation_propagates_and_releases_holds(tmp_path):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "delivery-cancel.sqlite3")
    _checkpoint(store, target)
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", retry_delay=0.01, clock=lambda: 120
    )
    holds = []

    async def hold(_target, progress_id, _cutoff, active, _run_id, _route_total):
        holds.append((progress_id, active))

    async def emit(*_args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            sync.run(
                account_id="account",
                targets=(target,),
                fetch_page=lambda *_args: asyncio.sleep(
                    0,
                    result=HistoryPage(
                        (_message("A", 100, target=target), _message("B", 110, target=target)),
                        "A",
                    ),
                ),
                send_hold=hold,
                emit=emit,
                retry_delivery_failures=False,
            )
        )
    assert [active for _progress_id, active in holds] == [True, False]
    assert store.checkpoint("test", "account", "QQ", target) == ("A", 100, frozenset({"A"}))


def test_default_delivery_retry_still_recovers_after_a_missing_ack(tmp_path):
    target = HistoryTarget("private", "peer")
    store = OfflineHistoryStore(tmp_path / "delivery-default-retry.sqlite3")
    _checkpoint(store, target)
    sync = OfflineHistorySynchronizer(
        store, backend="test", platform="QQ", retry_delay=0.01, clock=lambda: 120
    )
    attempts = []
    waits = []

    async def emit(item, _run_id, _cutoff):
        attempts.append(item.message_id)
        return len(attempts) > 1

    async def wait_retry():
        waits.append(True)

    result = asyncio.run(
        sync.run(
            account_id="account",
            targets=(target,),
            fetch_page=lambda *_args: asyncio.sleep(
                0,
                result=HistoryPage(
                    (_message("A", 100, target=target), _message("B", 110, target=target)),
                    "A",
                ),
            ),
            send_hold=lambda *_args: asyncio.sleep(0),
            emit=emit,
            wait_retry=wait_retry,
        )
    )
    assert attempts == ["B", "B"]
    assert waits == [True]
    assert result["complete_sessions"] == 1
    assert result["failed_sessions"] == {}
    assert store.checkpoint("test", "account", "QQ", target) == ("B", 110, frozenset({"B"}))
