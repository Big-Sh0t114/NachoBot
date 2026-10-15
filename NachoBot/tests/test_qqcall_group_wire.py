from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qqcall_client.qqcall as qqcall_runtime  # noqa: E402
from qqcall_client.qqcall import QQCallConfig, QQCallRuntime  # noqa: E402


def _load_core_qqcall_api():
    """Load the production Core API module while replacing only its transport import."""
    missing = object()
    previous_message = sys.modules.get("src.common.message", missing)
    previous_api = sys.modules.get("src.common.message.api", missing)
    if previous_api is missing:
        transport_module = types.ModuleType("src.common.message.api")
        transport_module.get_global_api = lambda: None
        sys.modules["src.common.message.api"] = transport_module
    spec = importlib.util.spec_from_file_location(
        "_qqcall_api_group_wire_test",
        PROJECT_ROOT / "plugins" / "qqcall_plugin" / "control_api.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_api is missing:
            sys.modules.pop("src.common.message.api", None)
        else:
            sys.modules["src.common.message.api"] = previous_api
        if previous_message is missing:
            sys.modules.pop("src.common.message", None)
        else:
            sys.modules["src.common.message"] = previous_message
    return module


qqcall_api = _load_core_qqcall_api()


ACCOUNT = "123456789"
OTHER_ACCOUNT = "987654321"
PEER = "1122334455"
GROUP = "246813579"
OTHER_GROUP = "246813580"
META = {"serviceInstance": "service-instance-1", "hostGeneration": 7, "connectionGeneration": 9}


class _CoreLoopback:
    """Run the real Core pending API and handler through a fake message transport."""

    def __init__(self) -> None:
        self.handler = None
        self.runtime: QQCallRuntime | None = None
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self.forward_request = None

    def register_custom_message_handler(self, message_type_name, handler) -> None:
        assert message_type_name == "qqcall_response"
        self.handler = handler

    async def send_custom_message(self, *, platform, message_type_name, message) -> bool:
        assert platform == "qq"
        assert message_type_name == "qqcall_request"
        assert self.runtime is not None
        request = copy.deepcopy(message)
        self.requests.append(request)
        raw = {
            "platform": platform,
            "message_type_name": message_type_name,
            "content": request,
        }
        if self.forward_request is None:
            await self.runtime.handle_request(raw)
        else:
            await self.forward_request(raw)
        return True

    async def send_adapter_response(self, message_type_name, response) -> bool:
        assert message_type_name == "qqcall_response"
        assert self.handler is not None
        wire_response = copy.deepcopy(response)
        self.responses.append(wire_response)
        await self.handler({
            "platform": "qq",
            "message_type_name": message_type_name,
            "content": wire_response,
        })
        return True


@pytest.fixture(autouse=True)
def _reset_core_qqcall_state():
    qqcall_api._pending_requests.clear()
    qqcall_api._registered_api = None
    yield
    assert qqcall_api.pending_request_count() == 0
    qqcall_api._pending_requests.clear()
    qqcall_api._registered_api = None


def _runtime(
    core: _CoreLoopback,
    *,
    enabled: bool = True,
    account_id: str = ACCOUNT,
    state_account: str = ACCOUNT,
    core_ready: bool = True,
    admit_group=None,
    transport=None,
) -> QQCallRuntime:
    identity = (object(), 4, state_account)
    state = {
        "connected": True,
        "account_id": state_account,
        "identity": identity,
        "core_ready": core_ready,
    }

    async def emit(_message):
        return True

    async def read_state():
        return state

    async def admit_peer(_peer):
        return True

    async def status_transport(path, payload, _token):
        if transport is not None:
            return await transport(path, payload, _token)
        assert path == "/v1/group/status"
        assert payload["groupId"] == GROUP
        return {
            "version": 1,
            "accountId": ACCOUNT,
            **META,
            "groupId": GROUP,
            "occupied": False,
            "nativeJoinVerified": False,
            "room": None,
        }

    config = QQCallConfig.from_mapping({
        "enabled": enabled,
        "endpoint": "http://127.0.0.1:6112",
        "token_file": "C:\\test\\qqcall.token",
        "account_id": account_id,
        "peer_map": {},
    })
    runtime = QQCallRuntime(
        config,
        "qq",
        emit,
        core.send_adapter_response,
        read_state,
        admit_peer,
        admit_group=admit_group or (lambda _peer, _group: True),
        dependency_check=lambda: (),
        transport=status_transport,
    )
    runtime._token = "synthetic-test-token"
    runtime._client_instance = "test-client"
    runtime._identity = identity
    runtime._identity_account = state_account
    runtime._service_meta = dict(META)
    runtime._lease_claimed = True
    core.runtime = runtime
    return runtime


def _group_request(*, group_id: str = GROUP, request_id: str = "group_wire_request_00000001") -> dict:
    return {
        "platform": "qq",
        "message_type_name": "qqcall_request",
        "content": {
            "version": 1,
            "request_id": request_id,
            "platform": "qq",
            "operation": "join_group",
            "peer_user_id": PEER,
            "scope": None,
            "group_id": group_id,
            "trigger_text": "#join_vc_qq",
        },
    }


def _group_status_request(*, group_id: str = GROUP, request_id: str = "group_status_wire_request_0001") -> dict:
    return {
        "platform": "qq",
        "message_type_name": "qqcall_request",
        "content": {
            "version": 1,
            "request_id": request_id,
            "platform": "qq",
            "operation": "group_status",
            "peer_user_id": PEER,
            "scope": None,
            "group_id": group_id,
        },
    }


def _diagnostic_result(*, group_id: str = GROUP, **changes) -> dict:
    now_ms = int(time.time() * 1000)
    result = {
        "version": 1,
        "accountId": ACCOUNT,
        **META,
        "groupId": group_id,
        "occupied": True,
        "nativeJoinVerified": False,
        "room": None,
        "diagnosticOnly": True,
        "diagnostic": {
            "groupId": group_id,
            "membership": {
                "source": "group_audio_member_count",
                "onlineCount": 4,
                "memberCount": 6,
                "businessType": 2,
                "nativeTimeout": False,
                "observedAtMs": now_ms - 2_100,
            },
            "invitation": {
                "source": "group_video_invite_member_update",
                "roomId": "room-secret-99887766",
                "totalInvitedMembers": 3,
                "seq": "12345678901234567890",
                "joinCount": 1,
                "quitCount": 0,
                "observedAtMs": now_ms - 5_100,
            },
            "roomEndObservation": None,
            "diagnosticOnly": True,
            "nativeJoinVerified": False,
        },
    }
    result.update(changes)
    return result


async def _read_group_status(*, timeout: float = 0.25) -> dict:
    return await qqcall_api.request_qqcall(
        "group_status", peer_user_id=PEER, group_id=GROUP, timeout=timeout
    )


async def _group_status_error(*, timeout: float = 0.25) -> str:
    try:
        await _read_group_status(timeout=timeout)
    except qqcall_api.QQCallAPIError as exc:
        return exc.code
    raise AssertionError("group_status failure unexpectedly returned success")


def test_group_status_adapter_envelope_has_group_and_no_join_intent_or_call_scope():
    request = _group_status_request()
    content = request["content"]
    assert QQCallRuntime._request_envelope(request, "qq") == content

    for changes in (
        {"trigger_text": "#stat_vc_qq"},
        {"scope": {"serviceInstance": "forged"}},
        {"group_id": "not-a-group-id"},
    ):
        forged = copy.deepcopy(request)
        forged["content"].update(changes)
        assert QQCallRuntime._request_envelope(forged, "qq") is None


async def _join_error(*, timeout: float = 0.25) -> str:
    try:
        await qqcall_api.request_qqcall(
            "join_group",
            peer_user_id=PEER,
            group_id=GROUP,
            trigger_text="#join_vc_qq",
            timeout=timeout,
        )
    except qqcall_api.QQCallAPIError as exc:
        return exc.code
    raise AssertionError("join_group refusal unexpectedly returned success")


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("unverified_room", "unsupported_unverified_group_join"),
        ("group_denied", "group_not_admitted"),
        ("disabled", "qqcall_disabled"),
        ("account_mismatch", "account_mismatch"),
    ("core_busy", "call_missing_core"),
        ("runtime_stopped", "runtime_stopped"),
        ("inflight_limit", "runtime_busy"),
    ],
)
def test_real_core_group_pending_resolves_correlated_adapter_refusal(monkeypatch, case, expected_error):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            return {
                "version": 1,
                "accountId": ACCOUNT,
                **META,
                "groupId": GROUP,
                "occupied": False,
                "nativeJoinVerified": False,
                "room": None,
            }

        def admit_group(_peer, _group):
            return case != "group_denied"

        core = _CoreLoopback()
        runtime = _runtime(
            core,
            enabled=case != "disabled",
            state_account=OTHER_ACCOUNT if case == "account_mismatch" else ACCOUNT,
            core_ready=case != "core_busy",
            admit_group=admit_group,
            transport=transport,
        )
        if case == "runtime_stopped":
            runtime._stopped = True
        if case == "inflight_limit":
            for index in range(16):
                runtime._request_inflight[f"reserved-{index}"] = (
                    "reserved-fingerprint",
                    {},
                    asyncio.get_running_loop().create_future(),
                )
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        started = time.monotonic()
        error = await _join_error()
        elapsed = time.monotonic() - started

        assert error == expected_error
        assert elapsed < 1.0
        assert qqcall_api.pending_request_count() == 0
        assert len(core.requests) == 1
        assert len(core.responses) == 1
        request = core.requests[0]
        assert core.responses[0] == {
            "version": 1,
            "request_id": request["request_id"],
            "platform": "qq",
            "operation": "join_group",
            "peer_user_id": PEER,
            "status": "error",
            "error": {"code": expected_error},
            "group_id": GROUP,
        }
        assert "/v1/group/join" not in paths
        if case == "unverified_room":
            assert paths == ["/v1/group/status"]
        elif case in {"group_denied", "disabled", "account_mismatch", "core_busy", "runtime_stopped", "inflight_limit"}:
            assert paths == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "replay_kind",
    ["same_cached_request", "different_group_cached_request", "different_private_peer_cached_request"],
)
def test_cached_duplicate_responses_preserve_received_envelope_fields(monkeypatch, replay_kind):
    async def scenario():
        core = _CoreLoopback()
        runtime = _runtime(core)

        async def forward_with_cached_replay(raw):
            await runtime.handle_request(raw)
            replay = copy.deepcopy(raw)
            if replay_kind == "different_group_cached_request":
                replay["content"]["group_id"] = OTHER_GROUP
            elif replay_kind == "different_private_peer_cached_request":
                replay["content"] = {
                    "version": 1,
                    "request_id": raw["content"]["request_id"],
                    "platform": "qq",
                    "operation": "status",
                    "peer_user_id": OTHER_ACCOUNT,
                    "scope": None,
                }
            await runtime.handle_request(replay)

        core.forward_request = forward_with_cached_replay
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        error = await _join_error()

        assert error == "unsupported_unverified_group_join"
        assert len(core.responses) == 2
        assert core.responses[0]["group_id"] == GROUP
        assert core.responses[1]["request_id"] == core.requests[0]["request_id"]
        expected_second_error = "unsupported_unverified_group_join"
        if replay_kind == "different_group_cached_request":
            expected_second_error = "duplicate_request_id"
            assert core.responses[1]["group_id"] == OTHER_GROUP
            assert core.responses[1]["operation"] == "join_group"
            assert core.responses[1]["peer_user_id"] == PEER
        elif replay_kind == "different_private_peer_cached_request":
            expected_second_error = "duplicate_request_id"
            assert "group_id" not in core.responses[1]
            assert core.responses[1]["operation"] == "status"
            assert core.responses[1]["peer_user_id"] == OTHER_ACCOUNT
        else:
            assert core.responses[1]["group_id"] == GROUP
            assert core.responses[1]["operation"] == "join_group"
            assert core.responses[1]["peer_user_id"] == PEER
        assert core.responses[1]["error"]["code"] == expected_second_error
        assert qqcall_api.pending_request_count() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "collision_kind",
    ["same_group", "different_group", "private_operation_and_peer"],
)
def test_inflight_collision_does_not_poison_original_core_waiter(monkeypatch, collision_kind):
    async def scenario():
        entered_admission = asyncio.Event()
        release_admission = asyncio.Event()
        core = _CoreLoopback()

        async def admit_group(_peer, _group):
            entered_admission.set()
            await release_admission.wait()
            return True

        runtime = _runtime(core, admit_group=admit_group)

        async def forward_with_inflight_collision(raw):
            original_task = asyncio.create_task(runtime.handle_request(raw))
            await entered_admission.wait()
            conflict = copy.deepcopy(raw)
            if collision_kind == "different_group":
                conflict["content"]["group_id"] = OTHER_GROUP
            elif collision_kind == "private_operation_and_peer":
                conflict["content"] = {
                    "version": 1,
                    "request_id": raw["content"]["request_id"],
                    "platform": "qq",
                    "operation": "status",
                    "peer_user_id": OTHER_ACCOUNT,
                    "scope": None,
                }
            conflict_task = asyncio.create_task(runtime.handle_request(conflict))
            await asyncio.sleep(0)
            request_id = raw["content"]["request_id"]
            pending = qqcall_api._pending_requests[request_id]
            assert pending.future.done() is False
            assert qqcall_api.pending_request_count() == 1
            if collision_kind != "same_group":
                await conflict_task
                assert pending.future.done() is False
                assert len(core.responses) == 1
                collision_response = core.responses[0]
                assert collision_response["error"]["code"] == "duplicate_request_id"
                if collision_kind == "different_group":
                    assert collision_response["group_id"] == OTHER_GROUP
                    assert collision_response["operation"] == "join_group"
                    assert collision_response["peer_user_id"] == PEER
                else:
                    assert "group_id" not in collision_response
                    assert collision_response["operation"] == "status"
                    assert collision_response["peer_user_id"] == OTHER_ACCOUNT
            release_admission.set()
            await asyncio.gather(original_task, conflict_task)

        core.forward_request = forward_with_inflight_collision
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        error = await _join_error()

        assert error == "unsupported_unverified_group_join"
        assert len(core.responses) == 2
        assert core.responses[-1]["group_id"] == GROUP
        assert core.responses[-1]["error"]["code"] == "unsupported_unverified_group_join"
        if collision_kind == "same_group":
            assert all(response["error"]["code"] == "unsupported_unverified_group_join" for response in core.responses)
            assert [response["group_id"] for response in core.responses] == [GROUP, GROUP]
        elif collision_kind == "different_group":
            assert core.responses[0]["error"]["code"] == "duplicate_request_id"
            assert core.responses[0]["group_id"] == OTHER_GROUP
        else:
            assert core.responses[0]["error"]["code"] == "duplicate_request_id"
            assert "group_id" not in core.responses[0]
            assert core.responses[0]["operation"] == "status"
            assert core.responses[0]["peer_user_id"] == OTHER_ACCOUNT
        assert all(response["request_id"] == core.requests[0]["request_id"] for response in core.responses)
        assert qqcall_api.pending_request_count() == 0

    asyncio.run(scenario())


def test_private_response_has_no_group_scope_and_malformed_group_is_not_echoed(monkeypatch):
    async def scenario():
        core = _CoreLoopback()
        runtime = _runtime(core)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.raises(qqcall_api.QQCallAPIError) as error:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=0.25)
        assert error.value.code == "no_active_call"
        assert len(core.responses) == 1
        assert "group_id" not in core.responses[0]

        malformed = _group_request(group_id="not-a-group-id")
        assert await runtime.handle_request(malformed) is None
        assert len(core.responses) == 1
        assert runtime.status()["counts"]["cachedRequests"] == 1

    asyncio.run(scenario())


def test_cancelled_group_request_caches_correlated_unknown_outcome(monkeypatch):
    async def scenario():
        entered_admission = asyncio.Event()
        release_admission = asyncio.Event()
        core = _CoreLoopback()

        async def admit_group(_peer, _group):
            entered_admission.set()
            await release_admission.wait()
            return True

        runtime = _runtime(core, admit_group=admit_group)
        request_id = "group_wire_cancelled_request_0001"
        raw = _group_request(request_id=request_id)
        owner = asyncio.create_task(runtime.handle_request(raw))
        await entered_admission.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        cached = runtime._request_cache[request_id]
        assert cached[1]["error"]["code"] == "request_outcome_unknown"
        assert cached[1]["group_id"] == GROUP
        monkeypatch.setattr(qqcall_api.secrets, "token_urlsafe", lambda _size: request_id)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        error = await _join_error(timeout=0.25)

        assert error == "request_outcome_unknown"
        assert len(core.responses) == 1
        assert core.responses[0]["request_id"] == request_id
        assert core.responses[0]["group_id"] == GROUP
        assert qqcall_api.pending_request_count() == 0

    asyncio.run(scenario())


def test_group_status_uses_current_lease_and_projects_bounded_diagnostics(monkeypatch):
    async def scenario():
        paths = []
        admission = []

        async def transport(path, payload, token):
            paths.append(path)
            assert token == "synthetic-test-token"
            assert payload == {
                "accountId": ACCOUNT,
                "clientInstance": "test-client",
                "groupId": GROUP,
            }
            return _diagnostic_result()

        def admit_group(peer, group):
            admission.append((peer, group))
            return True

        core = _CoreLoopback()
        runtime = _runtime(core, transport=transport, admit_group=admit_group)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        data = await _read_group_status()

        assert data == {
            "occupied": True,
            "nativeJoinVerified": False,
            "roomObserved": False,
            "diagnosticOnly": True,
            "membership": {
                "source": "group_audio_member_count",
                "onlineCount": 4,
                "memberCount": 6,
                "businessType": 2,
                "nativeTimeout": False,
                "ttlSecondsRemaining": 28,
            },
            "invitation": {
                "source": "group_video_invite_member_update",
                "totalInvitedMembers": 3,
                "joinCount": 1,
                "quitCount": 0,
                "ttlSecondsRemaining": 25,
            },
            "roomEndObservation": None,
        }
        assert admission == [(PEER, GROUP), (PEER, GROUP)]
        assert paths == ["/v1/group/diagnostics"]
        assert runtime._active_call is None
        assert core.requests[0] == {
            "version": 1,
            "request_id": core.requests[0]["request_id"],
            "platform": "qq",
            "operation": "group_status",
            "peer_user_id": PEER,
            "scope": None,
            "group_id": GROUP,
        }
        response = core.responses[0]
        assert response["group_id"] == GROUP
        assert response["status"] == "ok"
        assert "room-secret-99887766" not in repr(response)
        assert PEER not in repr(response["data"])
        assert "groupId" not in response["data"]
        assert "roomId" not in repr(response["data"])
        assert "/v1/group/join" not in paths

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("group_denied", "group_not_admitted"),
        ("disabled", "qqcall_disabled"),
        ("account_mismatch", "account_mismatch"),
        ("core_busy", "core_not_ready"),
        ("runtime_stopped", "runtime_stopped"),
        ("lease_lost", "adapter_lease_required"),
    ],
)
def test_group_status_enforces_adapter_admission_and_runtime_fences(monkeypatch, case, expected_error):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result()

        core = _CoreLoopback()
        runtime = _runtime(
            core,
            enabled=case != "disabled",
            state_account=OTHER_ACCOUNT if case == "account_mismatch" else ACCOUNT,
            core_ready=case != "core_busy",
            admit_group=lambda _peer, _group: case != "group_denied",
            transport=transport,
        )
        if case == "runtime_stopped":
            runtime._stopped = True
        if case == "lease_lost":
            runtime._lease_claimed = False
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _group_status_error() == expected_error
        assert len(core.responses) == 1
        assert core.responses[0]["error"]["code"] == expected_error
        assert core.responses[0]["group_id"] == GROUP
        if case in {"group_denied", "disabled", "account_mismatch", "core_busy", "runtime_stopped", "lease_lost"}:
            assert paths == []

    asyncio.run(scenario())


def test_group_status_rejects_stale_generation_and_never_treats_room_as_joined(monkeypatch):
    async def scenario():
        async def stale_transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            return _diagnostic_result(hostGeneration=META["hostGeneration"] + 1)

        core = _CoreLoopback()
        runtime = _runtime(core, transport=stale_transport)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)
        assert await _group_status_error() == "stale_generation"
        assert core.responses[0]["group_id"] == GROUP

        valid_room = {
            "groupId": GROUP,
            "roomId": "private-room-id",
            "roomGeneration": 4,
            "live": True,
            "observedAtMs": int(time.time() * 1000) - 100,
        }

        async def current_transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            result = _diagnostic_result(nativeJoinVerified=True, room=valid_room)
            return result

        runtime._transport = current_transport
        data = await _read_group_status()
        assert data["nativeJoinVerified"] is True
        assert data["roomObserved"] is True
        assert "private-room-id" not in repr(data)
        assert "joined" not in repr(data).lower()

        async def capability_only_transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            return _diagnostic_result(nativeJoinVerified=True, room=None, diagnostic=None)

        runtime._transport = capability_only_transport
        capability_only = await _read_group_status()
        assert capability_only["nativeJoinVerified"] is True
        assert capability_only["roomObserved"] is False
        assert capability_only["membership"] is None
        assert "joined" not in repr(capability_only).lower()

    asyncio.run(scenario())


def test_group_status_empty_and_expired_observations_are_not_verified(monkeypatch):
    async def scenario():
        async def transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            result = _diagnostic_result()
            result["diagnostic"]["membership"]["observedAtMs"] = int(time.time() * 1000) - 30_000
            result["diagnostic"]["invitation"] = None
            result["diagnostic"]["roomEndObservation"] = None
            return result

        core = _CoreLoopback()
        _runtime(core, transport=transport)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        data = await _read_group_status()

        assert data["membership"] is None
        assert data["invitation"] is None
        assert data["roomEndObservation"] is None
        assert data["nativeJoinVerified"] is False
        assert data["roomObserved"] is False

    asyncio.run(scenario())


def test_group_status_result_is_dropped_when_current_client_lease_changes_mid_read(monkeypatch):
    async def scenario():
        core = _CoreLoopback()
        runtime = None

        async def transport(path, payload, _token):
            assert path == "/v1/group/diagnostics"
            assert payload["clientInstance"] == "test-client"
            runtime._client_instance = "replacement-adapter-client"
            return _diagnostic_result()

        runtime = _runtime(core, transport=transport)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _group_status_error() == "platform_identity_changed"
        assert core.responses[0]["error"]["code"] == "platform_identity_changed"
        assert core.responses[0]["group_id"] == GROUP

    asyncio.run(scenario())


def test_group_status_result_is_dropped_if_core_readiness_changes_during_read(monkeypatch):
    async def scenario():
        core = _CoreLoopback()
        runtime = None

        async def transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            runtime._core_ready = False
            return _diagnostic_result()

        runtime = _runtime(core, transport=transport)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _group_status_error() == "core_not_ready"
        assert core.responses[0]["error"]["code"] == "core_not_ready"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "room",
    [
        {"groupId": OTHER_GROUP, "roomId": "r", "roomGeneration": 4, "live": True, "observedAtMs": int(time.time() * 1000)},
        {"groupId": GROUP, "roomId": "r", "roomGeneration": 4, "live": True, "observedAtMs": int(time.time() * 1000) - 5_001},
        {"groupId": GROUP, "roomId": "r", "roomGeneration": True, "live": True, "observedAtMs": int(time.time() * 1000)},
    ],
)
def test_group_status_rejects_room_assertions_without_fresh_group_binding(monkeypatch, room):
    async def scenario():
        async def transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            return _diagnostic_result(nativeJoinVerified=True, room=room)

        core = _CoreLoopback()
        _runtime(core, transport=transport)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)
        assert await _group_status_error() == "group_status_invalid"

    asyncio.run(scenario())


def test_group_status_exact_reuse_refreshes_data_and_recomputes_observation_ttl(monkeypatch):
    async def scenario():
        now_seconds = [1_800_000_000.0]
        monkeypatch.setattr(qqcall_runtime.time, "time", lambda: now_seconds[0])
        results = []
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            if not results:
                result = _diagnostic_result(occupied=True)
                results.append(copy.deepcopy(result))
                return result
            result = copy.deepcopy(results[0])
            result["occupied"] = False
            return result

        core = _CoreLoopback()
        runtime = _runtime(core, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        raw = _group_status_request(request_id="group_status_reuse_after_ttl_001")
        first = await runtime.handle_request(raw)
        now_seconds[0] += 31
        second = await runtime.handle_request(raw)

        assert first["data"]["occupied"] is True
        assert first["data"]["membership"] is not None
        assert second["status"] == "ok"
        assert second["data"]["occupied"] is False
        assert second["data"]["membership"] is None
        assert second["data"]["invitation"] is None
        assert paths == ["/v1/group/diagnostics", "/v1/group/diagnostics"]
        assert len(core.responses) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("revocation", "expected_error"),
    [
        ("admission", "group_not_admitted"),
        ("lease", "adapter_lease_required"),
    ],
)
def test_group_status_exact_reuse_rechecks_admission_and_lease(monkeypatch, revocation, expected_error):
    async def scenario():
        admitted = [True]
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result(occupied=not paths[:-1])

        core = _CoreLoopback()
        runtime = _runtime(
            core,
            admit_group=lambda _peer, _group: admitted[0],
            transport=transport,
        )

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        raw = _group_status_request(request_id="group_status_revoke_reuse_001")
        first = await runtime.handle_request(raw)
        assert first["status"] == "ok"
        if revocation == "admission":
            admitted[0] = False
        else:
            runtime._lease_claimed = False
        second = await runtime.handle_request(raw)

        assert second["status"] == "error"
        assert second["error"]["code"] == expected_error
        assert len(core.responses) == 2
        assert paths == ["/v1/group/diagnostics"]

    asyncio.run(scenario())


def test_group_status_exact_reuse_reads_current_service_generation(monkeypatch):
    async def scenario():
        paths = []
        core = _CoreLoopback()
        runtime = None

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result(
                hostGeneration=runtime._service_meta["hostGeneration"],
                occupied=len(paths) == 1,
            )

        runtime = _runtime(core, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        raw = _group_status_request(request_id="group_status_new_generation_001")
        first = await runtime.handle_request(raw)
        runtime._service_meta = {**runtime._service_meta, "hostGeneration": META["hostGeneration"] + 1}
        second = await runtime.handle_request(raw)

        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert second["data"]["occupied"] is False
        assert paths == ["/v1/group/diagnostics", "/v1/group/diagnostics"]

    asyncio.run(scenario())


def test_group_status_inflight_duplicate_revalidates_after_admission_revocation(monkeypatch):
    async def scenario():
        entered_admission = asyncio.Event()
        release_admission = asyncio.Event()
        admitted = [True]
        admission_count = [0]
        paths = []

        async def admit_group(_peer, _group):
            admission_count[0] += 1
            if admission_count[0] == 1:
                entered_admission.set()
                await release_admission.wait()
                return True
            return admitted[0]

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result()

        core = _CoreLoopback()
        runtime = _runtime(core, admit_group=admit_group, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        raw = _group_status_request(request_id="group_status_inflight_revocation_01")
        owner = asyncio.create_task(runtime.handle_request(raw))
        await entered_admission.wait()
        follower = asyncio.create_task(runtime.handle_request(raw))
        await asyncio.sleep(0)
        collision = await runtime.handle_request(
            _group_status_request(
                group_id=OTHER_GROUP,
                request_id="group_status_inflight_revocation_01",
            )
        )
        admitted[0] = False
        release_admission.set()
        owner_response, follower_response = await asyncio.gather(owner, follower)

        assert collision["error"]["code"] == "duplicate_request_id"
        assert owner_response["status"] == "error"
        assert follower_response["status"] == "error"
        assert owner_response["error"]["code"] == "group_not_admitted"
        assert follower_response["error"]["code"] == "group_not_admitted"
        assert len(core.responses) == 3
        assert all(response["status"] != "ok" for response in core.responses)
        assert sum(response.get("error", {}).get("code") == "duplicate_request_id" for response in core.responses) == 1
        assert paths == ["/v1/group/diagnostics"]

    asyncio.run(scenario())


def test_group_status_conflicting_request_id_stays_duplicate_after_refresh(monkeypatch):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result()

        core = _CoreLoopback()
        runtime = _runtime(core, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        request_id = "group_status_fingerprint_conflict_01"
        first = await runtime.handle_request(_group_status_request(request_id=request_id))
        second = await runtime.handle_request(
            _group_status_request(group_id=OTHER_GROUP, request_id=request_id)
        )

        assert first["status"] == "ok"
        assert second["error"]["code"] == "duplicate_request_id"
        assert second["group_id"] == OTHER_GROUP
        assert paths == ["/v1/group/diagnostics"]

    asyncio.run(scenario())


def test_group_status_final_guard_rechecks_native_room_timestamp(monkeypatch):
    async def scenario():
        now_seconds = [1_800_000_000.0]
        monkeypatch.setattr(qqcall_runtime.time, "time", lambda: now_seconds[0])
        admission_count = [0]
        core = _CoreLoopback()

        def admit_group(_peer, _group):
            admission_count[0] += 1
            if admission_count[0] == 2:
                now_seconds[0] += 6
            return True

        room = {
            "groupId": GROUP,
            "roomId": "private-room-id",
            "roomGeneration": 3,
            "live": True,
            "observedAtMs": int(now_seconds[0] * 1000) - 100,
        }

        async def transport(path, _payload, _token):
            assert path == "/v1/group/diagnostics"
            return _diagnostic_result(
                nativeJoinVerified=True,
                room=room,
                diagnostic=None,
            )

        runtime = _runtime(core, admit_group=admit_group, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        request_id = "group_status_room_final_guard_001"
        response = await runtime.handle_request(_group_status_request(request_id=request_id))

        assert response["status"] == "error"
        assert response["error"]["code"] == "group_status_invalid"
        assert admission_count == [2]
        assert "private-room-id" not in repr(response)
        assert "private-room-id" not in repr(runtime._request_cache[request_id])

    asyncio.run(scenario())


def test_cached_group_status_refresh_respects_global_inflight_limit(monkeypatch):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            return _diagnostic_result()

        core = _CoreLoopback()
        runtime = _runtime(core, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        raw = _group_status_request(request_id="group_status_cached_inflight_limit_01")
        first = await runtime.handle_request(raw)
        assert first["status"] == "ok"
        reserved = []
        for index in range(16):
            future = asyncio.get_running_loop().create_future()
            reserved.append(future)
            runtime._request_inflight[f"reserved-{index}"] = ("reserved-fingerprint", future)

        second = await runtime.handle_request(raw)

        for future in reserved:
            if not future.done():
                future.set_result({})
        assert second["error"]["code"] == "runtime_busy"
        assert len(core.responses) == 2
        assert paths == ["/v1/group/diagnostics"]

    asyncio.run(scenario())


def test_delayed_group_status_follower_cannot_replace_evicted_id_mutation_cache(monkeypatch):
    async def scenario():
        diagnostics_entered = asyncio.Event()
        release_diagnostics = asyncio.Event()
        follower_waiting_to_refresh = asyncio.Event()
        release_follower = asyncio.Event()
        diagnostics_calls = []
        mutation_calls = []
        core = _CoreLoopback()

        async def transport(path, payload, _token):
            if path == "/v1/group/diagnostics":
                diagnostics_calls.append(path)
                diagnostics_entered.set()
                await release_diagnostics.wait()
                return _diagnostic_result()
            assert path == "/v1/call/command"
            mutation_calls.append((path, copy.deepcopy(payload)))
            scope = dict(payload["scope"])
            return {
                "version": 1,
                "accountId": ACCOUNT,
                "serviceInstance": META["serviceInstance"],
                "operation": "accept",
                "scope": scope,
                "result": {
                    "posted": True,
                    "id": 55,
                    "callId": scope["callId"],
                    "callGeneration": scope["callGeneration"],
                },
            }

        runtime = _runtime(core, transport=transport)

        async def ignore_response(_raw):
            return True

        core.handler = ignore_response
        call_scope = {
            **META,
            "callId": "call-idempotency-test",
            "callGeneration": 11,
        }
        runtime._active_call = {
            "peer_user_id": PEER,
            "inviteCorrelation": "kernel_from_uid_matched",
            "callId": call_scope["callId"],
            "generation": call_scope["callGeneration"],
            "scope": call_scope,
            "uncertain": False,
            "inviteCallbackReady": True,
            "phase": "invite_ready",
            "acceptState": "not_requested",
        }
        runtime._peer_binding_current = lambda _call, _meta: True

        original_refresh = runtime._refresh_group_status_duplicate

        async def delayed_refresh(_self, request, identity, fingerprint):
            follower_waiting_to_refresh.set()
            await release_follower.wait()
            return await original_refresh(request, identity, fingerprint)

        runtime._refresh_group_status_duplicate = types.MethodType(delayed_refresh, runtime)

        shared_request_id = "group_status_mutation_cache_eviction_01"
        status_request = _group_status_request(request_id=shared_request_id)
        owner = asyncio.create_task(runtime.handle_request(status_request))
        await diagnostics_entered.wait()
        follower = asyncio.create_task(runtime.handle_request(status_request))
        await asyncio.sleep(0)
        release_diagnostics.set()
        owner_response = await owner
        await follower_waiting_to_refresh.wait()

        for index in range(qqcall_runtime._REQUEST_CACHE_SIZE):
            await runtime.handle_request({
                "platform": "qq",
                "message_type_name": "qqcall_request",
                "content": {
                    "version": 1,
                    "request_id": f"status_eviction_request_{index:04d}",
                    "platform": "qq",
                    "operation": "status",
                    "peer_user_id": PEER,
                    "scope": None,
                },
            })
        assert shared_request_id not in runtime._request_cache

        mutation_request = {
            "platform": "qq",
            "message_type_name": "qqcall_request",
            "content": {
                "version": 1,
                "request_id": shared_request_id,
                "platform": "qq",
                "operation": "accept",
                "peer_user_id": PEER,
                "scope": call_scope,
            },
        }
        mutation_response = await runtime.handle_request(mutation_request)
        assert mutation_response["status"] == "ok", mutation_response.get("error")
        assert runtime._request_cache[shared_request_id][1]["operation"] == "accept"

        exact_mutation_replay = await runtime.handle_request(mutation_request)
        assert exact_mutation_replay == mutation_response
        assert len(mutation_calls) == 1

        release_follower.set()
        follower_response = await follower
        assert follower_response["error"]["code"] == "duplicate_request_id"
        assert runtime._request_cache[shared_request_id][1]["operation"] == "accept"
        final_mutation_replay = await runtime.handle_request(mutation_request)

        assert owner_response["status"] == "ok"
        assert final_mutation_replay == mutation_response
        assert len(mutation_calls) == 1
        assert diagnostics_calls == ["/v1/group/diagnostics"]

    asyncio.run(scenario())
