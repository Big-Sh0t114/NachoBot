from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.qqcall_plugin import control_api as qqcall_api  # noqa: E402


PEER = "123456789"
OTHER_PEER = "987654321"
SCOPE = {
    "serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001",
    "hostGeneration": 7,
    "connectionGeneration": 9,
    "callId": "call-abc123",
    "callGeneration": 11,
}


class _FakeCoreAPI:
    def __init__(self):
        self.handler = None
        self.registration_count = 0
        self.requests = []
        self.send_started = asyncio.Event()
        self.block_send = False
        self.release_send = asyncio.Event()
        self.send_result = True
        self.on_send = None

    def register_custom_message_handler(self, message_type_name, handler):
        assert message_type_name == "qqcall_response"
        self.registration_count += 1
        self.handler = handler

    async def send_custom_message(self, *, platform, message_type_name, message):
        self.requests.append((platform, message_type_name, message))
        self.send_started.set()
        if self.on_send is not None:
            await self.on_send(message)
        if self.block_send:
            await self.release_send.wait()
        return self.send_result


def _run(coro):
    return asyncio.run(coro)


def _status_data(peer_user_id=PEER, scope=SCOPE):
    return {
        "scope": dict(scope),
        "call": {
            "callId": scope["callId"],
            "generation": scope["callGeneration"],
            "phase": "invite_ready",
            "uncertain": False,
            "inviteCallbackReady": True,
            "inviteCorrelation": "kernel_from_uid_matched",
            "acceptState": "not_requested",
            "audioTurnId": None,
        },
        "peer_user_id": peer_user_id,
    }


def _mutation_data(scope=SCOPE):
    return {
        "scope": dict(scope),
        "result": {
            "posted": True,
            "id": 55,
            "callId": scope["callId"],
            "callGeneration": scope["callGeneration"],
        },
    }


def _group_status_data(**changes):
    result = {
        "occupied": True,
        "nativeJoinVerified": False,
        "roomObserved": False,
        "diagnosticOnly": True,
        "membership": {
            "source": "group_audio_member_count",
            "onlineCount": 3,
            "memberCount": None,
            "businessType": None,
            "nativeTimeout": False,
            "ttlSecondsRemaining": 27,
        },
        "invitation": {
            "source": "group_video_invite_member_update",
            "totalInvitedMembers": 2,
            "joinCount": 1,
            "quitCount": 0,
            "ttlSecondsRemaining": 15,
        },
        "roomEndObservation": None,
    }
    result.update(changes)
    return result


def _group_join_data(**changes):
    result = {
        "version": 1,
        "accountId": "99887766",
        "serviceInstance": SCOPE["serviceInstance"],
        "hostGeneration": 7,
        "connectionGeneration": 9,
        "joined": True,
        "scope": {
            "kind": "qqcall_group",
            "serviceInstance": SCOPE["serviceInstance"],
            "hostGeneration": 7,
            "connectionGeneration": 9,
            "groupId": "445566",
            "roomId": "room-abc",
            "roomGeneration": 4,
        },
        "source": "native_scene3_relation",
        "requesterUserId": PEER,
    }
    result.update(changes)
    return result


def _response(request, *, data=None, status="ok", peer_user_id=None, operation=None, platform=None, outer_platform=None, scope=None, error=None):
    content = {
        "version": 1,
        "request_id": request["request_id"],
        "platform": platform if platform is not None else request["platform"],
        "operation": operation if operation is not None else request["operation"],
        "peer_user_id": peer_user_id if peer_user_id is not None else request["peer_user_id"],
        "status": status,
    }
    if status == "error":
        content["error"] = error or {"code": "call_scope_stale"}
    else:
        content["data"] = data
    if "group_id" in request:
        content["group_id"] = request["group_id"]
    if "requester_user_id" in request:
        content["requester_user_id"] = request["requester_user_id"]
    return {
        "platform": outer_platform if outer_platform is not None else request["platform"],
        "content": content,
    }


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    qqcall_api._pending_requests.clear()
    qqcall_api._registered_api = None
    yield
    assert qqcall_api.pending_request_count() == 0
    qqcall_api._pending_requests.clear()
    qqcall_api._registered_api = None


def test_status_request_registers_once_and_accepts_ack_before_send_returns(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def ack_first(request):
            assert qqcall_api.pending_request_count() == 1
            await fake.handler(_response(request, data=_status_data()))

        fake.on_send = ack_first
        data = await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert data == _status_data()
        assert fake.registration_count == 1
        assert len(fake.requests) == 1
        platform, message_type, request = fake.requests[0]
        assert (platform, message_type) == ("qq", "qqcall_request")
        assert request == {
            "version": 1,
            "request_id": request["request_id"],
            "platform": "qq",
            "operation": "status",
            "peer_user_id": PEER,
            "scope": None,
        }
        assert len(request["request_id"]) >= 20

    _run(scenario())


def test_accept_mutation_is_bound_to_full_scope_and_exact_result(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def ack_first(request):
            assert request["scope"] == SCOPE
            await fake.handler(_response(request, data=_mutation_data()))

        fake.on_send = ack_first
        result = await qqcall_api.request_qqcall("accept", peer_user_id=PEER, scope=dict(SCOPE))
        assert result == _mutation_data()
        assert fake.registration_count == 1

    _run(scenario())


@pytest.mark.parametrize(
    "error_code",
    [
        "call_occupied",
        "unsupported_unverified_group_join",
        "group_room_unverified",
        "core_not_ready",
        "group_admission_failed",
        "group_not_admitted",
        "group_status_failed",
        "service_timeout",
        "group_room_query_timeout",
        "group_room_observed_empty",
        "group_room_query_quarantined",
        "group_room_query_busy",
        "group_room_query_post_failed",
        "group_room_metadata_invalid",
        "group_status_invalid",
        "group_status_stale",
        "group_id_invalid",
        "group_join_failed",
        "group_join_response_invalid",
        "group_trigger_invalid",
    ],
)
def test_group_join_request_is_explicit_and_binds_busy_or_unverified_error_to_group_and_peer(monkeypatch, error_code):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            assert request == {
                "version": 1,
                "request_id": request["request_id"],
                "platform": "qq",
                "operation": "join_group",
                "peer_user_id": PEER,
                "scope": None,
                "group_id": "445566",
                "trigger_text": "请加入群语音",
            }
            response = _response(
                request,
                status="error",
                error={"code": error_code},
            )
            response["content"]["group_id"] = "445567"
            await fake.handler(response)
            await fake.handler(_response(request, status="error", error={"code": error_code}))

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(
                "join_group",
                peer_user_id=PEER,
                group_id="445566",
                trigger_text="请加入群语音",
                timeout=1,
            )
        assert exc_info.value.code == error_code
        assert len(fake.requests) == 1

    _run(scenario())


@pytest.mark.parametrize(
    ("operation", "kwargs", "expected_code"),
    [
        ("join_group", {"group_id": "0", "trigger_text": "#join_vc_qq"}, "invalid_group_id"),
        ("join_group", {"group_id": "445566", "trigger_text": "\n#join_vc_qq"}, "invalid_group_request"),
        ("join_group", {"group_id": "445566", "trigger_text": "x" * 257}, "invalid_group_request"),
        ("join_group", {"group_id": "445566", "trigger_text": "please join"}, "invalid_group_request"),
        ("join_group", {"group_id": "445566", "trigger_text": "#join_vc_qq", "scope": dict(SCOPE)}, "invalid_scope"),
        ("status", {"group_id": "445566", "trigger_text": "请加入群语音"}, "invalid_group_request"),
    ],
)
def test_group_join_rejects_invalid_fields_and_legacy_operations_forbid_group_context(monkeypatch, operation, kwargs, expected_code):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(operation, peer_user_id=PEER, **kwargs)
        assert exc_info.value.code == expected_code
        assert fake.requests == []
        assert fake.registration_count == 0

    _run(scenario())


def test_group_join_accepts_only_correlated_native_join_result(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            wrong_group = _response(request, data=_group_join_data())
            wrong_group["content"]["group_id"] = "445567"
            await fake.handler(wrong_group)
            await fake.handler(_response(request, data=_group_join_data()))

        fake.on_send = respond
        result = await qqcall_api.request_qqcall(
            "join_group", peer_user_id=PEER, group_id="445566", trigger_text="#join_vc_qq"
        )
        assert result == _group_join_data()
        assert fake.requests[0][2]["scope"] is None

    _run(scenario())


def test_group_join_default_request_deadline_is_bounded_without_changing_status_deadline(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)
    observed_timeouts = []
    real_wait_for = asyncio.wait_for

    async def record_timeout(awaitable, timeout):
        observed_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(qqcall_api.asyncio, "wait_for", record_timeout)

    async def scenario():
        async def respond(request):
            if request["operation"] == "join_group":
                await fake.handler(_response(request, data=_group_join_data()))
            else:
                await fake.handler(_response(request, data=_status_data()))

        fake.on_send = respond
        await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        await qqcall_api.request_qqcall(
            "join_group", peer_user_id=PEER, group_id="445566", trigger_text="#join_vc_qq"
        )
        assert observed_timeouts == [8.0, 45.0]

    _run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"joined": False},
        {"source": "group_status"},
        {"requesterUserId": OTHER_PEER},
        {"accountId": "0"},
        {"hostGeneration": True},
        {"scope": {"kind": "qqcall_group"}},
        {"extra": "caller_claimed"},
    ],
)
def test_group_join_rejects_unverified_or_mismatched_success_fields(monkeypatch, changes):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            await fake.handler(_response(request, data=_group_join_data(**changes)))

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(
                "join_group", peer_user_id=PEER, group_id="445566", trigger_text="#join_vc_qq"
            )
        assert exc_info.value.code == "group_join_response_invalid"

    _run(scenario())


def test_group_status_uses_exact_group_envelope_and_sanitized_correlated_response(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            assert request == {
                "version": 1,
                "request_id": request["request_id"],
                "platform": "qq",
                "operation": "group_status",
                "peer_user_id": PEER,
                "scope": None,
                "group_id": "445566",
            }
            wrong_group = _response(request, data=_group_status_data())
            wrong_group["content"]["group_id"] = "445567"
            await fake.handler(wrong_group)
            await fake.handler(_response(request, data=_group_status_data()))

        fake.on_send = respond
        data = await qqcall_api.request_qqcall(
            "group_status", peer_user_id=PEER, group_id="445566", timeout=1
        )
        assert data == _group_status_data()
        assert fake.requests[0][2]["scope"] is None
        assert "trigger_text" not in fake.requests[0][2]
        assert "peer_user_id" not in data and "roomId" not in repr(data)

    _run(scenario())


def test_group_leave_uses_typed_group_envelope_and_correlated_verified_result(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            assert request == {
                "version": 1,
                "request_id": request["request_id"],
                "platform": "qq",
                "operation": "leave_group",
                "peer_user_id": PEER,
                "scope": None,
                "group_id": "445566",
                "requester_user_id": PEER,
            }
            wrong_group = _response(request, data={"left": True})
            wrong_group["content"]["group_id"] = "445567"
            await fake.handler(wrong_group)
            wrong_requester = _response(request, data={"left": True})
            wrong_requester["content"]["requester_user_id"] = OTHER_PEER
            await fake.handler(wrong_requester)
            await fake.handler(_response(request, data={"left": True}))

        fake.on_send = respond
        data = await qqcall_api.request_qqcall(
            "leave_group",
            peer_user_id=PEER,
            requester_user_id=PEER,
            group_id="445566",
            timeout=1,
        )
        assert data == {"left": True}
        assert len(fake.requests) == 1

    _run(scenario())


def test_group_leave_rejects_unconfirmed_success_data(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            await fake.handler(_response(request, data={"left": False}))

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(
                "leave_group",
                peer_user_id=PEER,
                requester_user_id=PEER,
                group_id="445566",
                timeout=1,
            )
        assert exc_info.value.code == "group_leave_unconfirmed"

    _run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"group_id": "445566"},
        {"group_id": "445566", "requester_user_id": OTHER_PEER},
        {"group_id": "445566", "requester_user_id": PEER, "scope": dict(SCOPE)},
        {"group_id": "445566", "requester_user_id": PEER, "trigger_text": "#leave_vc_qq"},
    ],
)
def test_group_leave_rejects_missing_or_synthetic_scope_inputs(monkeypatch, kwargs):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("leave_group", peer_user_id=PEER, **kwargs)
        assert exc_info.value.code in {"invalid_group_request", "invalid_scope"}
        assert fake.requests == []

    _run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"unexpected": "uid"},
        {"nativeJoinVerified": "true"},
        {"roomObserved": True},
        {"membership": {"source": "group_audio_member_count", "onlineCount": 1, "memberCount": None,
                         "businessType": None, "nativeTimeout": False, "ttlSecondsRemaining": 31}},
        {"invitation": {"source": "group_video_invite_member_update", "totalInvitedMembers": 1,
                         "joinCount": 8193, "quitCount": 0, "ttlSecondsRemaining": 10}},
    ],
)
def test_group_status_rejects_unbounded_or_unverified_metadata(monkeypatch, changes):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            data = _group_status_data(**changes)
            await fake.handler(_response(request, data=data))

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(
                "group_status", peer_user_id=PEER, group_id="445566", timeout=1
            )
        assert exc_info.value.code == "group_status_invalid"

    _run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"group_id": "0"}, "invalid_group_id"),
        ({"group_id": "445566", "trigger_text": "#stat_vc_qq"}, "invalid_group_request"),
        ({"group_id": "445566", "scope": dict(SCOPE)}, "invalid_scope"),
    ],
)
def test_group_status_rejects_invalid_scope_and_join_only_fields(monkeypatch, kwargs, expected_code):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("group_status", peer_user_id=PEER, **kwargs)
        assert exc_info.value.code == expected_code
        assert fake.requests == []

    _run(scenario())


def test_status_with_explicit_scope_requires_exact_same_scope(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def ack_first(request):
            assert request["scope"] == SCOPE
            await fake.handler(_response(request, data=_status_data()))

        fake.on_send = ack_first
        result = await qqcall_api.request_qqcall("status", peer_user_id=PEER, scope=dict(SCOPE))
        assert result["scope"] == SCOPE

    _run(scenario())


@pytest.mark.parametrize("audio_turn_id", [None, "opaque/turn:token"])
def test_status_preserves_nullable_opaque_audio_turn_id(audio_turn_id):
    data = _status_data()
    data["call"]["audioTurnId"] = audio_turn_id
    validated = qqcall_api._validate_status_data(data, PEER)
    assert validated["call"]["audioTurnId"] == audio_turn_id


@pytest.mark.parametrize("audio_turn_id", [True, 42, "", " turn ", "turn\nnext", "x" * 257])
def test_status_rejects_malformed_audio_turn_id(audio_turn_id):
    data = _status_data()
    data["call"]["audioTurnId"] = audio_turn_id
    with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
        qqcall_api._validate_status_data(data, PEER)
    assert exc_info.value.code == "malformed_response"


def test_status_requires_audio_turn_id_field():
    data = _status_data()
    del data["call"]["audioTurnId"]
    with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
        qqcall_api._validate_status_data(data, PEER)
    assert exc_info.value.code == "malformed_response"


def test_wrong_outer_inner_platform_peer_and_operation_do_not_resolve(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=1))
        await fake.send_started.wait()
        request = fake.requests[0][2]
        invalid = [
            _response(request, data=_status_data(), outer_platform="other"),
            _response(request, data=_status_data(), platform="other"),
            _response(request, data=_status_data(), peer_user_id=OTHER_PEER),
            _response(request, data=_status_data(), operation="accept"),
        ]
        for message in invalid:
            await fake.handler(message)
            assert not task.done()
        await fake.handler(_response(request, data=_status_data()))
        assert await task == _status_data()

    _run(scenario())


def test_mutation_response_with_late_wrong_scope_is_ignored(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def wrong_scope(request):
            mismatched = dict(SCOPE, callGeneration=SCOPE["callGeneration"] + 1)
            await fake.handler(_response(request, data=_mutation_data(mismatched)))

        fake.on_send = wrong_scope
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("accept", peer_user_id=PEER, scope=dict(SCOPE), timeout=0.01)
        assert exc_info.value.code == "request_outcome_unknown"
        request = fake.requests[0][2]
        await fake.handler(_response(request, data=_mutation_data()))
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_blocked_send_is_covered_by_status_deadline_and_cleans_pending(monkeypatch):
    fake = _FakeCoreAPI()
    fake.block_send = True
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(
            qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=0.02)
        )
        await fake.send_started.wait()
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await task
        assert exc_info.value.code == "timeout"
        assert len(fake.requests) == 1
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_blocked_mutation_send_deadline_is_unknown_without_retry_and_late_ack_is_ignored(monkeypatch):
    fake = _FakeCoreAPI()
    fake.block_send = True
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        task = asyncio.create_task(
            qqcall_api.request_qqcall(
                "accept", peer_user_id=PEER, scope=dict(SCOPE), timeout=0.02
            )
        )
        await fake.send_started.wait()
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await task
        assert exc_info.value.code == "request_outcome_unknown"
        assert len(fake.requests) == 1, "a mutation is never retried after an ambiguous deadline"
        request = fake.requests[0][2]
        assert qqcall_api.pending_request_count() == 0
        await fake.handler(_response(request, data=_mutation_data()))
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_send_duration_consumes_receipt_deadline_and_late_ack_does_not_resolve(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        late_response = None

        async def delayed_send(request):
            nonlocal late_response
            loop = asyncio.get_running_loop()

            def deliver_late_ack():
                nonlocal late_response
                late_response = asyncio.create_task(
                    fake.handler(_response(request, data=_status_data()))
                )

            loop.call_later(0.13, deliver_late_ack)
            await asyncio.sleep(0.07)

        fake.on_send = delayed_send
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=0.1)
        assert exc_info.value.code == "timeout"
        assert qqcall_api.pending_request_count() == 0
        await asyncio.sleep(0.06)
        assert late_response is not None
        await late_response
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_false_send_after_ack_is_send_failed_and_cleans_pending(monkeypatch):
    fake = _FakeCoreAPI()
    fake.send_result = False
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def ack_first(request):
            await fake.handler(_response(request, data=_status_data()))

        fake.on_send = ack_first
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert exc_info.value.code == "send_failed"
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_timeout_and_cancellation_remove_pending_entries(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=0.01)
        assert exc_info.value.code == "timeout"
        task = asyncio.create_task(qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=10))
        for _ in range(100):
            if len(fake.requests) >= 2:
                break
            await asyncio.sleep(0)
        assert len(fake.requests) >= 2
        cancelled_request = fake.requests[-1][2]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await fake.handler(_response(cancelled_request, data=_status_data()))
        assert qqcall_api.pending_request_count() == 0

    _run(scenario())


def test_pending_limit_is_64_and_cancellation_drains_every_waiter(monkeypatch):
    fake = _FakeCoreAPI()
    fake.block_send = True
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        tasks = [
            asyncio.create_task(qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=30))
            for _ in range(64)
        ]
        for _ in range(100):
            if qqcall_api.pending_request_count() == 64:
                break
            await asyncio.sleep(0)
        assert qqcall_api.pending_request_count() == 64
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert exc_info.value.code == "too_many_pending_requests"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert qqcall_api.pending_request_count() == 0
        assert fake.registration_count == 1

    _run(scenario())


@pytest.mark.parametrize(
    ("operation", "peer_user_id", "scope", "expected_code"),
    [
        ("anything", PEER, None, "invalid_operation"),
        ("status", "000123", None, "invalid_peer_user_id"),
        ("accept", PEER, {"callId": "partial"}, "invalid_scope"),
        ("hangup", PEER, None, "invalid_scope"),
    ],
)
def test_invalid_requests_fail_before_register_or_send(monkeypatch, operation, peer_user_id, scope, expected_code):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(operation, peer_user_id=peer_user_id, scope=scope)
        assert exc_info.value.code == expected_code
        assert fake.requests == []
        assert fake.registration_count == 0

    _run(scenario())


def test_adapter_error_code_is_safe_and_unknown_values_are_not_reflected(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            code = {
                1: "call_scope_stale",
                2: "peer_admission_failed",
                3: "raw-token-should-not-escape-123456789",
            }[len(fake.requests)]
            await fake.handler(_response(request, status="error", error={"code": code}))

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert exc_info.value.code == "call_scope_stale"
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert exc_info.value.code == "peer_admission_failed"
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER)
        assert exc_info.value.code == "upstream_error"

    _run(scenario())


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_response_version_must_be_an_integer_not_bool_or_float(monkeypatch, bad_version):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)

    async def scenario():
        async def respond(request):
            message = _response(request, data=_status_data())
            message["content"]["version"] = bad_version
            await fake.handler(message)

        fake.on_send = respond
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=0.01)
        assert exc_info.value.code == "timeout"

    _run(scenario())


def test_bool_generation_values_are_rejected_even_when_equal_to_one(monkeypatch):
    fake = _FakeCoreAPI()
    monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)
    scope_generation_one = dict(SCOPE, callGeneration=1)

    async def scenario():
        async def respond_status(request):
            data = _status_data(scope=scope_generation_one)
            data["call"]["generation"] = True
            await fake.handler(_response(request, data=data))

        fake.on_send = respond_status
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall("status", peer_user_id=PEER, timeout=1)
        assert exc_info.value.code == "malformed_response"

        async def respond_mutation(request):
            data = _mutation_data(scope_generation_one)
            data["result"]["callGeneration"] = True
            await fake.handler(_response(request, data=data))

        fake.on_send = respond_mutation
        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await qqcall_api.request_qqcall(
                "accept", peer_user_id=PEER, scope=scope_generation_one, timeout=1
            )
        assert exc_info.value.code == "malformed_response"

    _run(scenario())
