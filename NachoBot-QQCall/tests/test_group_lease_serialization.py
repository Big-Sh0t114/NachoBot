"""Regression for startup and watcher sharing one native route observer."""
import asyncio
from pathlib import Path
import sys
import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
for directory in (TASK_ROOT, TASK_ROOT.parent / "NachoBot", TASK_ROOT.parent / "NachoBot" / "tests"):
    sys.path.insert(0, str(directory))

from test_qqcall_group_audio_runtime import _runtime, _FakeBackend, _OwnedFakeClient, _async_test


@_async_test
async def test_parallel_refreshes_do_not_overlap_a_single_native_observer():
    entered = asyncio.Event()
    release = asyncio.Event()
    busy = False
    overlaps = 0
    calls = 0

    async def single_observer(scope):
        nonlocal busy, overlaps, calls
        calls += 1
        if busy:
            overlaps += 1
            return False
        busy = True
        try:
            entered.set()
            await release.wait()
            return True
        finally:
            busy = False

    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [], fresh_lease=single_observer)
    runtime._state = "starting"
    runtime._stop_event = asyncio.Event()
    first = asyncio.create_task(runtime._refresh_lease())
    await entered.wait()
    second = asyncio.create_task(runtime._refresh_lease())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results == [True, True]
    assert overlaps == 0
    assert calls == 2, "each refresh must obtain its own proof"
    assert runtime.is_current()


@_async_test
async def test_lost_owner_during_refresh_never_leaves_cached_proof():
    entered = asyncio.Event()
    release = asyncio.Event()
    owned = True

    async def proof(scope):
        entered.set()
        await release.wait()
        return True

    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [], fresh_lease=proof, local_fence=lambda: owned)
    runtime._state = "starting"
    runtime._stop_event = asyncio.Event()
    first = asyncio.create_task(runtime._refresh_lease())
    await entered.wait()
    second = asyncio.create_task(runtime._refresh_lease())
    owned = False
    release.set()
    assert await asyncio.gather(first, second) == [False, False]
    assert runtime._proof_at is None
    assert not runtime.is_current()


@_async_test
async def test_pending_route_can_be_distinguished_from_stalled_loop_without_renewal():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def proof(scope):
        entered.set()
        await release.wait()
        return True

    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [], fresh_lease=proof)
    runtime._state = "starting"
    runtime._stop_event = asyncio.Event()
    probe = asyncio.create_task(runtime._probe_loop())
    refresh = asyncio.create_task(runtime._refresh_lease())
    try:
        await entered.wait()
        await asyncio.sleep(0.12)
        timing = runtime._timing_diagnostics()
        assert timing["refresh_inflight_ms"] >= 100
        assert timing["loop_tick_age_ms"] < timing["refresh_inflight_ms"]
        assert timing["max_loop_gap_ms"] > 0
        assert runtime._proof_at is None, "diagnostic ticks must never grant audio permission"
        assert not runtime.is_current()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)
        assert runtime._timing_diagnostics()["refresh_inflight_ms"] == -1
    finally:
        probe.cancel()
        refresh.cancel()
        await asyncio.gather(probe, refresh, return_exceptions=True)


@_async_test
async def test_stopping_runtime_drains_its_timing_probe():
    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [])
    await runtime.start()
    probe = runtime._loop_probe_task
    assert probe is not None
    await runtime.stop()
    assert probe.done()
    assert runtime._loop_probe_task is None


@_async_test
@pytest.mark.parametrize("route_delay", [1.2, 1.85])
async def test_route_latency_does_not_expire_capture_proof_between_refreshes(route_delay):
    async def native_route(scope):
        await asyncio.sleep(route_delay)
        return True

    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [], fresh_lease=native_route)
    try:
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            assert runtime.is_current(), "valid capture proof expired while its scheduled refresh was still running"
            await asyncio.sleep(0.02)
    finally:
        await runtime.stop()
