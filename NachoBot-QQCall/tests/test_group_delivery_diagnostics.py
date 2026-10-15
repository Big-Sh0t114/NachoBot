"""Distinguish API recognition, transport handoff, and current-turn acceptance."""
import asyncio
import logging
from pathlib import Path
import sys

import pytest

TASK_ROOT = Path(__file__).resolve().parents[1]
for directory in (TASK_ROOT, TASK_ROOT.parent / "NachoBot", TASK_ROOT.parent / "NachoBot" / "tests"):
    sys.path.insert(0, str(directory))

from test_qqcall_group_audio_runtime import _runtime, _FakeBackend, _OwnedFakeClient, _async_test
from test_qqcall_group_universal_api import make_event, wait_until


@pytest.mark.parametrize("mode,outcome,forwarded,accepted", [
    ("success", "forwarded_current", 1, 1),
    ("reject", "send_rejected", 0, 0),
    ("truthy", "send_rejected", 0, 0),
    ("exception", "send_failed", 0, 0),
    ("timeout", "send_timeout", 0, 0),
    ("stale", "forwarded_then_stale", 1, 0),
])
@_async_test
async def test_delivery_outcome_does_not_confuse_recognition_with_handoff(
    mode, outcome, forwarded, accepted, caplog,
):
    client = _OwnedFakeClient()
    current = True
    messages = []
    runtime, _, _ = _runtime(client, _FakeBackend(), messages, local_fence=lambda: current)

    async def send(message):
        nonlocal current
        messages.append(message)
        if mode == "exception":
            raise RuntimeError("private-error-content")
        if mode == "timeout":
            raise asyncio.TimeoutError("private-error-content")
        if mode == "stale":
            current = False
        if mode == "truthy":
            return {"sent": True}
        return mode != "reject"

    runtime._send_message = send
    caplog.set_level(logging.INFO, logger="qqcall_pcm.group_runtime")
    await runtime.start()
    try:
        await client.session.push(make_event(client.session, text="private-transcript-content"))
        await wait_until(lambda: runtime.status()["lastSpeechOutcome"] is not None)
        status = runtime.status()
        assert status["speechResults"] == 1
        assert status["forwardedResults"] == forwarded
        assert status["acceptedTurns"] == accepted
        assert status["lastSpeechOutcome"] == outcome
        assert len(messages) == 1
        logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "qqcall_pcm.group_runtime")
        assert f"outcome={outcome}" in logs
        assert "private-transcript-content" not in logs
        assert "private-error-content" not in logs
    finally:
        await runtime.stop()


@_async_test
async def test_expired_scope_is_reported_without_sending():
    messages = []
    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), messages)
    assert await runtime._on_speech_result({}) is False
    assert messages == []
    assert runtime.status()["lastSpeechOutcome"] == "scope_stale_before_delivery"
    assert runtime.status()["forwardedResults"] == 0
