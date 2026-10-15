from __future__ import annotations

import asyncio
import copy
import io
import logging
from functools import wraps
import os
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QQCALL_ROOT = PROJECT_ROOT.parent / "NachoBot-QQCall"
for _path in (PROJECT_ROOT, QQCALL_ROOT, PROJECT_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_qqcall_group_universal_api as binding_tests  # noqa: E402
import qqcall_pcm.group_runtime as group_runtime_module  # noqa: E402
from qqcall_client.qqcall_group_wire import validate_group_audio_claim  # noqa: E402
from qqcall_pcm.group_runtime import GroupAudioRuntime, QQCallGroupAudioRuntimeError  # noqa: E402


SCOPE = dict(binding_tests.SCOPE)
REQUESTER = "1122334455"


class _FakeBackend:
    def __init__(self) -> None:
        self.state = "stopped"
        self.send_pcm = None
        self.capture_fence = None
        self.failure_callback = None
        self.scope = None
        self.played = []
        self.cancel_count = 0
        self.stop_count = 0
        self.start_count = 0
        self.start_entered: asyncio.Event | None = None
        self.start_release: asyncio.Event | None = None

    async def start(self, capture_callback, on_failure, scope, *, send_pcm=None, capture_fence=None):
        assert capture_callback is None
        assert callable(send_pcm)
        assert callable(capture_fence)
        self.start_count += 1
        self.failure_callback = on_failure
        self.scope = copy.deepcopy(scope)
        self.send_pcm = send_pcm
        self.capture_fence = capture_fence
        self.state = "starting"
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        self.state = "ready"

    async def play_core_segment(self, kind, data, fence):
        if not fence():
            raise QQCallGroupAudioRuntimeError("stale_group_scope")
        self.played.append((kind, copy.deepcopy(data)))
        drained = kind == "voice" or kind == "voice_stream" and data.get("event") == "end"
        return {"render_drained": drained, "drain_confirmed": drained}

    async def cancel_playback(self):
        self.cancel_count += 1

    async def stop(self):
        self.stop_count += 1
        self.state = "stopped"


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class _OwnedFakeClient(binding_tests.FakeClient):
    """Count only the resources this runtime receives and owns."""

    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0
        self.create_cancelled = False

    async def create_session(self, options):
        try:
            return await super().create_session(options)
        except asyncio.CancelledError:
            self.create_cancelled = True
            raise

    async def close(self) -> None:
        self.close_count += 1
        await super().close()


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _runtime(client, backend, messages, *, fresh_lease=None, local_fence=None, client_factory=None):
    checks = []
    failures = []

    async def verify(scope):
        checks.append(copy.deepcopy(scope))
        return True if fresh_lease is None else await fresh_lease(scope)

    async def send(message):
        messages.append(message)
        return True

    return GroupAudioRuntime(
        SCOPE,
        REQUESTER,
        backend,
        (lambda: client) if client_factory is None else client_factory,
        verify,
        send,
        (lambda: True) if local_fence is None else local_fence,
        failures.append,
        group_name="测试群",
    ), checks, failures


_SLOW_STARTUP_SECONDS = group_runtime_module._PROOF_TTL_SECONDS + 0.15


@pytest.mark.parametrize("case,expected", [
    ("timeout", "lease_refresh_timeout"), ("exception", "lease_refresh_error"),
    ("false", "lease_refresh_rejected"), ("context", "lease_refresh_context_lost"),
])
@_async_test
async def test_refresh_failure_diagnostics_keep_proof_revoked_and_hide_exception_text(case, expected):
    async def refresh(_scope):
        if case == "timeout":
            await asyncio.sleep(1)
        if case == "exception":
            raise RuntimeError("private-secret-endpoint-token")
        return case == "context"

    fence_calls = []

    def local_fence():
        fence_calls.append(True)
        return case != "context"

    runtime, _, _ = _runtime(_OwnedFakeClient(), _FakeBackend(), [], fresh_lease=refresh, local_fence=local_fence)
    runtime._state = "active"
    runtime._proof_at = group_runtime_module.time.monotonic()
    with patch.object(group_runtime_module, "_PROOF_TTL_SECONDS", 0.02):
        with TestCase().assertLogs(group_runtime_module._LOGGER, level="WARNING") as captured:
            assert await runtime._refresh_lease() is False
    assert runtime._proof_at is None
    assert runtime.is_current() is False
    assert len(fence_calls) == (1 if case == "context" else 0)
    text = "\n".join(captured.output)
    assert expected in text
    assert "private-secret-endpoint-token" not in text


@_async_test
async def test_wav_fallback_stays_in_original_senderless_group_and_capture_continues():
    client = _OwnedFakeClient()
    backend = _FakeBackend()
    messages = []
    runtime, _checks, failures = _runtime(client, backend, messages)
    await runtime.start()
    await client.session.push(binding_tests.make_event(
        client.session, text=None, precomputed_asr_result_id=None,
    ))
    await _wait_until(lambda: len(messages) == 1)
    message = messages[0]
    config = message.message_info.additional_config
    assert message.message_info.group_info.group_id == SCOPE["groupId"]
    assert message.message_info.user_info is None
    assert message.message_segment.data == binding_tests.SYNTHETIC_VOICE_BASE64
    assert "precomputed_asr_result_id" not in config
    assert config["precomputed_asr_scope"] == binding_tests.SYNTHETIC_ASR_SCOPE
    assert config["realtime_session"]["provider"] == "qqcall_group"
    assert await backend.send_pcm(b"\0\0", source_id="main") == {"accepted": True}
    assert runtime.status()["ready"] is True
    assert failures == []
    await runtime.stop()


@pytest.mark.parametrize("code,logged", [("capture_queue_overrun", "capture_queue_overrun"),
                                       ("u_private_scope_1234", "unclassified")])
@_async_test
async def test_backend_failure_logs_only_fixed_code_and_closes_owned_audio(code, logged):
    client = _OwnedFakeClient()
    backend = _FakeBackend()
    runtime, _checks, failures = _runtime(client, backend, [])
    await runtime.start()
    with patch.object(group_runtime_module._LOGGER, "warning") as warning:
        await backend.failure_callback(code)
        assert warning.call_args.args[1] == logged
        if logged == "unclassified":
            assert code not in repr(warning.call_args)
    assert failures == [code]
    assert runtime.status()["ready"] is False
    assert backend.stop_count == 1
    assert client.close_count == 1


@pytest.mark.parametrize("slow_stage", ["client", "session", "backend"])
@_async_test
async def test_startup_lease_watch_keeps_proof_fresh_across_slow_stages(slow_stage):
    """The existing two-second proof cache is renewed while startup awaits are held."""
    client = _OwnedFakeClient()
    backend = _FakeBackend()
    messages = []
    entered = asyncio.Event()
    release = asyncio.Event()

    if slow_stage == "client":
        async def client_factory():
            entered.set()
            await release.wait()
            return client

        runtime, lease_checks, failures = _runtime(
            client, backend, messages, client_factory=client_factory,
        )
    elif slow_stage == "session":
        client.create_entered = entered
        client.create_release = release
        runtime, lease_checks, failures = _runtime(client, backend, messages)
    else:
        backend.start_entered = entered
        backend.start_release = release
        runtime, lease_checks, failures = _runtime(client, backend, messages)

    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        start_task = asyncio.create_task(runtime.start())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(_SLOW_STARTUP_SECONDS)
        assert runtime.is_current()
        assert len(lease_checks) >= 2
        release.set()
        await asyncio.wait_for(start_task, timeout=3.0)

        assert runtime.status()["ready"] is True
        assert failures == []
        assert backend.start_count == 1
        await runtime.stop()

    assert client.closed is True
    assert backend.stop_count == 1


@_async_test
async def test_slow_client_factory_reproduces_pre_watch_proof_expiry():
    """A long refresh interval reproduces the old startup behavior before the fix."""
    client = _OwnedFakeClient()
    backend = _FakeBackend()
    messages = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def client_factory():
        entered.set()
        await release.wait()
        return client

    runtime, lease_checks, failures = _runtime(
        client, backend, messages, client_factory=client_factory,
    )
    with patch.object(
        group_runtime_module,
        "_WATCH_INTERVAL_SECONDS",
        _SLOW_STARTUP_SECONDS + 1.0,
    ):
        start_task = asyncio.create_task(runtime.start())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(_SLOW_STARTUP_SECONDS)
        release.set()
        with pytest.raises(QQCallGroupAudioRuntimeError, match="lease_not_current"):
            await asyncio.wait_for(start_task, timeout=3.0)

    assert len(lease_checks) == 1
    assert client.close_count == 1
    assert backend.start_count == 0
    assert backend.stop_count == 1
    assert failures == []


@_async_test
async def test_route_proof_revocation_during_session_creation_closes_owned_session_only():
    client = _OwnedFakeClient()
    client.create_entered = asyncio.Event()
    client.create_release = asyncio.Event()
    backend = _FakeBackend()
    unowned_client = binding_tests.FakeClient()
    messages = []
    lease_current = True

    async def fresh_lease(_scope):
        return lease_current

    runtime, _lease_checks, failures = _runtime(
        client, backend, messages, fresh_lease=fresh_lease,
    )
    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        start_task = asyncio.create_task(runtime.start())
        await asyncio.wait_for(client.create_entered.wait(), timeout=1.0)
        lease_current = False
        await _wait_until(lambda: runtime.state == "stopping", timeout=2.0)
        client.create_release.set()
        with pytest.raises(QQCallGroupAudioRuntimeError):
            await asyncio.wait_for(start_task, timeout=6.0)
        await _wait_until(lambda: failures == ["group_audio_lease_lost"])

    assert runtime.status()["ready"] is False
    assert client.session.close_count == 1
    assert client.close_count == 1
    assert backend.start_count == 0
    assert backend.stop_count == 1
    assert unowned_client.closed is False
    assert unowned_client.session.close_count == 0


@_async_test
async def test_core_context_revocation_during_backend_start_prevents_late_activation():
    client = _OwnedFakeClient()
    backend = _FakeBackend()
    backend.start_entered = asyncio.Event()
    backend.start_release = asyncio.Event()
    unowned_client = binding_tests.FakeClient()
    messages = []
    core_context_current = True

    runtime, _lease_checks, failures = _runtime(
        client,
        backend,
        messages,
        local_fence=lambda: core_context_current,
    )
    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        start_task = asyncio.create_task(runtime.start())
        await asyncio.wait_for(backend.start_entered.wait(), timeout=2.0)
        core_context_current = False
        await _wait_until(lambda: runtime.state == "stopping", timeout=2.0)
        backend.start_release.set()
        with pytest.raises(QQCallGroupAudioRuntimeError):
            await asyncio.wait_for(start_task, timeout=6.0)
        await _wait_until(lambda: failures == ["group_audio_lease_lost"])

    assert runtime.status()["ready"] is False
    assert backend.start_count == 1
    assert backend.state == "stopped"
    assert backend.stop_count == 1
    assert client.session.close_count == 1
    assert client.close_count == 1
    assert unowned_client.closed is False
    assert unowned_client.session.close_count == 0


@_async_test
async def test_cancelled_session_startup_closes_owned_resources_promptly():
    client = _OwnedFakeClient()
    client.create_entered = asyncio.Event()
    client.create_release = asyncio.Event()
    backend = _FakeBackend()
    messages = []
    runtime, _lease_checks, _failures = _runtime(client, backend, messages)

    start_task = asyncio.create_task(runtime.start())
    await asyncio.wait_for(client.create_entered.wait(), timeout=1.0)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start_task, timeout=5.0)

    assert runtime.state == "stopped"
    assert client.create_cancelled is True
    assert client.session.close_count == 0
    assert client.close_count == 1
    assert backend.start_count == 0
    assert backend.stop_count == 1


def test_core_control_import_does_not_load_group_pcm_runtime():
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT), str(QQCALL_ROOT)))
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; import qqcall_client.qqcall; "
            "assert 'qqcall_pcm.group_runtime' not in sys.modules",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@_async_test
async def test_runtime_binds_canonical_pcm_and_projects_anonymous_multi_speaker_context():
    client = binding_tests.FakeClient()
    backend = _FakeBackend()
    messages = []
    runtime, lease_checks, failures = _runtime(client, backend, messages)

    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        await runtime.start()
        assert runtime.status()["ready"] is True
        assert backend.scope == SCOPE
        assert backend.capture_fence() is True

        pcm = b"\x01\x00" * 320
        receipt = await backend.send_pcm(pcm, source_id="main")
        assert receipt == {"accepted": True}
        assert client.session.sent == [(pcm, "main")]

        with TestCase().assertLogs(binding_tests.group_api.__name__, level="INFO") as logs:
            await client.session.push(binding_tests.make_event(
                client.session,
                turn_id="group-turn-a",
                speaker_id="private-cluster-a",
                text="first private transcript",
            ))
            await client.session.push(binding_tests.make_event(
                client.session,
                turn_id="group-turn-b",
                speaker_id="private-cluster-b",
                text="second private transcript",
            ))
            await _wait_until(lambda: len(messages) == 2)
            await _wait_until(lambda: len(logs.records) == 2)

        annotations = [message.message_info.additional_config["realtime_speaker"] for message in messages]
        assert annotations[0]["speaker_ref"] != annotations[1]["speaker_ref"]
        claims = [
            message.message_info.additional_config["qqcall_group_audio_claim"]
            for message in messages
        ]
        assert [claim["turn_id"] for claim in claims] == ["group-turn-a", "group-turn-b"]
        assert all(validate_group_audio_claim(claim) == claim for claim in claims)
        assert not any(REQUESTER == annotation["speaker_ref"] for annotation in annotations)

        for message in messages:
            info = message.message_info
            additional = info.additional_config
            assert info.group_info.group_id == SCOPE["groupId"]
            assert info.group_info.group_name == "测试群"
            assert info.user_info is None and info.sender_info is None
            assert message.message_segment.type == "voice"
            assert additional["precomputed_asr_result_id"] == binding_tests.SYNTHETIC_ASR_RESULT_ID
            assert additional["precomputed_asr_scope"] == binding_tests.SYNTHETIC_ASR_SCOPE
            assert additional["qqcall_group_requester_user_id"] == REQUESTER
            assert additional["delivery_target"]["turn_id"] == additional["qqcall_group_audio_claim"]["turn_id"]

        log_text = "\n".join(record.getMessage() for record in logs.records)
        assert annotations[0]["speaker_ref"] in log_text
        assert annotations[1]["speaker_ref"] in log_text
        for private_value in (
            "private-cluster-a",
            "private-cluster-b",
            "first private transcript",
            "second private transcript",
            binding_tests.SYNTHETIC_ASR_RESULT_ID,
            binding_tests.SYNTHETIC_ASR_SCOPE,
        ):
            assert private_value not in log_text
        assert len(lease_checks) >= 2
        assert failures == []
        await runtime.stop()

    assert client.session.close_count == 1
    assert client.closed is True
    assert backend.stop_count == 1
    assert runtime.status()["ready"] is False


@_async_test
async def test_barge_timing_reaches_adapter_library_handler_when_root_logging_is_warning():
    client = binding_tests.FakeClient()
    runtime, _checks, _failures = _runtime(client, _FakeBackend(), [])
    library_logger = logging.getLogger("ncnk_message")
    root_logger = logging.getLogger()
    prior_root_level, prior_library_level = root_logger.level, library_logger.level
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setLevel(logging.INFO)
    try:
        root_logger.setLevel(logging.WARNING)
        library_logger.setLevel(logging.INFO)
        with (patch.object(library_logger, "handlers", [handler]),
              patch.object(library_logger, "propagate", False)):
            await runtime.start()
            await runtime._on_speech_start()
            text = output.getvalue()
            assert "stage=claim_invalidated" in text
            assert "stage=cancel_settled" in text and "outcome=completed" in text
            assert root_logger.level == logging.WARNING
            for private in (REQUESTER, SCOPE["groupId"], client.session.session_id):
                assert private not in text
            await runtime.stop()
    finally:
        root_logger.setLevel(prior_root_level)
        library_logger.setLevel(prior_library_level)
        handler.close()


@_async_test
async def test_barge_timing_logs_order_and_fences_before_blocked_cancel_settles():
    client = binding_tests.FakeClient()
    backend = _FakeBackend()
    messages = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def cancel():
        entered.set()
        await release.wait()

    backend.cancel_playback = cancel
    runtime, _checks, _failures = _runtime(client, backend, messages)
    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        await runtime.start()
        await client.session.push(binding_tests.make_event(client.session, turn_id="private-old-turn"))
        await _wait_until(lambda: len(messages) == 1)
        timing = []

        def record(format_string, *args):
            if format_string.startswith("QQCall group barge stage="):
                timing.append(format_string % args)

        with (patch.object(binding_tests.group_api._LOGGER, "info", side_effect=record),
              patch.object(group_runtime_module._LOGGER, "info", side_effect=record)):
            await client.session.push(binding_tests.make_event(
                client.session, event="speech_start", turn_id="private-start-turn",
                speaker_id="private-speaker", text="private-transcript",
            ))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            assert not await runtime.verify_current_turn(SCOPE, REQUESTER, "private-old-turn")
            with pytest.raises(QQCallGroupAudioRuntimeError, match="stale_group_scope"):
                await runtime.play_core_segment(SCOPE, REQUESTER, "private-old-turn", "voice", "private-audio")
            assert backend.played == []
            release.set()
            await _wait_until(lambda: any("stage=cancel_settled" in line for line in timing))
        assert [line.split("stage=")[1].split()[0] for line in timing] == [
            "event_received", "lease_checked", "claim_invalidated", "cancel_settled",
        ]
        assert "outcome=completed" in timing[-1]
        text = "\n".join(timing)
        for private in ("private-", REQUESTER, SCOPE["groupId"], client.session.session_id,
                        binding_tests.SYNTHETIC_ASR_RESULT_ID, binding_tests.SYNTHETIC_VOICE_BASE64):
            assert private not in text
        await runtime.stop()


@pytest.mark.parametrize("failure", [RuntimeError("private-failure"), asyncio.CancelledError("private-cancellation")])
@_async_test
async def test_barge_cancel_failure_propagates_with_safe_incomplete_diagnostic(failure):
    client = binding_tests.FakeClient()
    backend = _FakeBackend()
    runtime, _checks, _failures = _runtime(client, backend, [])

    async def cancel():
        raise failure

    backend.cancel_playback = cancel
    await runtime.start()
    with TestCase().assertLogs(group_runtime_module._LOGGER, level="INFO") as logs:
        with pytest.raises(type(failure)) as raised:
            await runtime._on_speech_start()
    assert raised.value is failure
    assert "stage=claim_invalidated" in logs.output[0]
    assert "outcome=incomplete" in logs.output[-1]
    assert "private-" not in "\n".join(logs.output)
    backend.cancel_playback = _FakeBackend.cancel_playback.__get__(backend)
    await runtime.stop()


@_async_test
async def test_barge_in_revokes_old_turn_and_fences_voice_and_voice_stream_playback():
    client = binding_tests.FakeClient()
    backend = _FakeBackend()
    messages = []
    runtime, _checks, _failures = _runtime(client, backend, messages)

    with patch.object(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
        await runtime.start()
        await client.session.push(binding_tests.make_event(client.session, turn_id="play-turn-a"))
        await _wait_until(lambda: len(messages) == 1)
        assert await runtime.verify_current_turn(SCOPE, REQUESTER, "play-turn-a")

        await client.session.push(binding_tests.make_event(
            client.session,
            event="speech_start",
            turn_id="ignored-start-turn",
        ))
        await _wait_until(lambda: backend.cancel_count == 1)
        assert not await runtime.verify_current_turn(SCOPE, REQUESTER, "play-turn-a")
        with pytest.raises(QQCallGroupAudioRuntimeError, match="stale_group_scope"):
            await runtime.play_core_segment(SCOPE, REQUESTER, "play-turn-a", "voice", "stale")
        assert backend.played == []

        await client.session.push(binding_tests.make_event(client.session, turn_id="play-turn-b"))
        await _wait_until(lambda: len(messages) == 2)
        wav_receipt = await runtime.play_core_segment(
            SCOPE, REQUESTER, "play-turn-b", "voice", "core-wav-base64",
        )
        assert wav_receipt["render_drained"] is True

        stream_start = {
            "event": "start",
            "stream_id": "core-stream-1",
            "parent_message_id": "core-parent-1",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
        }
        stream_chunk = {
            "event": "chunk",
            "stream_id": "core-stream-1",
            "parent_message_id": "core-parent-1",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
            "seq": 0,
            "audio_base64": "AQAA",
        }
        stream_end = {key: value for key, value in stream_start.items() if key != "event"}
        stream_end["event"] = "end"
        for segment in (stream_start, stream_chunk, stream_end):
            await runtime.play_core_segment(
                SCOPE, REQUESTER, "play-turn-b", "voice_stream", segment,
            )
        assert [kind for kind, _data in backend.played] == [
            "voice", "voice_stream", "voice_stream", "voice_stream",
        ]
        assert runtime.status()["playbacks"] == 4
        await runtime.stop()

    assert client.session.close_count == 1
