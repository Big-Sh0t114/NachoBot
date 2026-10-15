from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QQCALL_ROOT = PROJECT_ROOT.parent / "NachoBot-QQCall"
for _path in (PROJECT_ROOT, QQCALL_ROOT, PROJECT_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_qqcall_group_wire as group_wire_tests  # noqa: E402
import test_qqcall_group_universal_api as binding_tests  # noqa: E402
from qqcall_client.qqcall_group_wire import (  # noqa: E402
    group_audio_session_id,
    validate_group_audio_claim,
    validate_group_audio_scope,
)
from qqcall_pcm.group_protocol import (  # noqa: E402
    group_session_id,
    validate_group_scope,
)


qqcall_api = group_wire_tests.qqcall_api
PEER = group_wire_tests.PEER
OTHER_PEER = "9988776655"
GROUP = group_wire_tests.GROUP
SCOPE_GROUP = {
    "kind": "qqcall_group",
    "serviceInstance": "service-instance-1",
    "hostGeneration": 7,
    "connectionGeneration": 9,
    "groupId": GROUP,
    "roomId": "room-reference-1",
    "roomGeneration": 4,
}
CLAIM = {
    "turn_id": "turn-accepted-1",
    "uvc_session": {
        "service_instance_id": "uvc-service-1",
        "session_id": "session-1",
        "session_generation": "1",
        "source_id": "main",
    },
    "speaker_ref": "qqg-0123456789abcdef",
}


class _ResponseCoreAPI:
    def __init__(self) -> None:
        self.handler = None
        self.request = None
        self.sent = asyncio.Event()

    def register_custom_message_handler(self, _message_type: str, handler) -> None:
        self.handler = handler

    async def send_custom_message(self, *, platform, message_type_name, message) -> bool:
        assert platform == "qq"
        assert message_type_name == "qqcall_request"
        self.request = copy.deepcopy(message)
        self.sent.set()
        return True


def _response(request: dict, *, scope_group=None, audio_claim=None, data=None) -> dict:
    content = {
        "version": 1,
        "request_id": request["request_id"],
        "platform": "qq",
        "operation": "group_audio_status",
        "peer_user_id": request["peer_user_id"],
        "status": "ok",
        "group_id": request["group_id"],
        "scope_group": copy.deepcopy(SCOPE_GROUP if scope_group is None else scope_group),
        "audio_claim": copy.deepcopy(CLAIM if audio_claim is None else audio_claim),
        "data": {"active": True} if data is None else data,
    }
    return {"platform": "qq", "content": content}


def _run(coro):
    return asyncio.run(coro)


def test_wire_scope_matches_pcm_contract_and_hash() -> None:
    scope = {**SCOPE_GROUP, "serviceInstance": "service-雪", "roomId": "房间-1"}
    assert validate_group_audio_scope(scope) == validate_group_scope(scope)
    assert group_audio_session_id(scope) == group_session_id(scope)
    normalized = validate_group_audio_scope(scope)
    normalized["roomId"] = "mutated-copy"
    assert SCOPE_GROUP["roomId"] == "room-reference-1"
    assert validate_group_audio_scope(scope)["roomId"] == "房间-1"


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {**SCOPE_GROUP, "unexpected": True},
        {key: value for key, value in SCOPE_GROUP.items() if key != "roomId"},
        {**SCOPE_GROUP, "kind": "private_call"},
        {**SCOPE_GROUP, "serviceInstance": "has whitespace"},
        {**SCOPE_GROUP, "serviceInstance": "control\u200bchar"},
        {**SCOPE_GROUP, "hostGeneration": True},
        {**SCOPE_GROUP, "connectionGeneration": 2**53},
        {**SCOPE_GROUP, "groupId": "012345"},
        {**SCOPE_GROUP, "roomId": "bad\nroom"},
    ],
)
def test_wire_scope_and_pcm_contract_reject_same_invalid_values(scope) -> None:
    with pytest.raises(ValueError):
        validate_group_audio_scope(scope)
    with pytest.raises(ValueError):
        validate_group_scope(scope)


def test_wire_claim_is_exact_bounded_and_copied() -> None:
    normalized = validate_group_audio_claim(CLAIM)
    assert normalized == CLAIM
    normalized["uvc_session"]["session_id"] = "changed"
    assert CLAIM["uvc_session"]["session_id"] == "session-1"


@pytest.mark.parametrize(
    "claim",
    [
        None,
        {**CLAIM, "extra": "not-allowed"},
        {key: value for key, value in CLAIM.items() if key != "turn_id"},
        {**CLAIM, "turn_id": ""},
        {**CLAIM, "turn_id": "x" * 129},
        {**CLAIM, "speaker_ref": "qqg-ABCDEF0123456789"},
        {**CLAIM, "uvc_session": {**CLAIM["uvc_session"], "source_id": "other"}},
        {**CLAIM, "uvc_session": {**CLAIM["uvc_session"], "extra": "x"}},
    ],
)
def test_wire_claim_rejects_malformed_or_untrusted_identity_fields(claim) -> None:
    with pytest.raises(ValueError):
        validate_group_audio_claim(claim)


def test_core_api_group_audio_request_is_fully_correlated(monkeypatch) -> None:
    async def scenario() -> None:
        fake = _ResponseCoreAPI()
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: fake)
        task = asyncio.create_task(
            qqcall_api.request_qqcall(
                "group_audio_status",
                peer_user_id=PEER,
                group_id=GROUP,
                scope_group=SCOPE_GROUP,
                audio_claim=CLAIM,
                timeout=1,
            )
        )
        await asyncio.wait_for(fake.sent.wait(), timeout=1)
        request = fake.request
        assert request == {
            "version": 1,
            "request_id": request["request_id"],
            "platform": "qq",
            "operation": "group_audio_status",
            "peer_user_id": PEER,
            "scope": None,
            "group_id": GROUP,
            "scope_group": SCOPE_GROUP,
            "audio_claim": CLAIM,
        }
        pending = qqcall_api._pending_requests[request["request_id"]]
        assert pending.scope is None
        assert pending.peer_user_id == PEER

        mismatch_scope = {**SCOPE_GROUP, "roomGeneration": 5}
        await fake.handler(_response(request, scope_group=mismatch_scope))
        assert not pending.future.done()
        mismatch_claim = {**CLAIM, "turn_id": "other-turn"}
        await fake.handler(_response(request, audio_claim=mismatch_claim))
        assert not pending.future.done()

        await fake.handler(_response(request, data={"active": True, "extra": 1}))
        with pytest.raises(qqcall_api.QQCallAPIError, match="group_audio_status_invalid"):
            await task

    _run(scenario())


@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [
        ("status", {"audio_claim": CLAIM}),
        ("status", {"scope_group": SCOPE_GROUP}),
        ("hangup", {"scope": {"serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001", "hostGeneration": 7, "connectionGeneration": 9, "callId": "call-1", "callGeneration": 1}, "audio_claim": CLAIM}),
        ("hangup", {"scope": {"serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001", "hostGeneration": 7, "connectionGeneration": 9, "callId": "call-1", "callGeneration": 1}, "scope_group": SCOPE_GROUP}),
        ("join_group", {"group_id": GROUP, "trigger_text": "#join_vc_qq", "audio_claim": CLAIM}),
        ("group_status", {"group_id": GROUP, "audio_claim": CLAIM}),
    ],
)
def test_non_group_audio_operations_reject_audio_claim(monkeypatch, operation, kwargs) -> None:
    async def scenario() -> None:
        with pytest.raises(qqcall_api.QQCallAPIError, match="invalid_group_request"):
            await qqcall_api.request_qqcall(operation, peer_user_id=PEER, **kwargs)

    _run(scenario())


def test_adapter_private_mutation_envelope_cannot_carry_group_scope() -> None:
    private_scope = {
        "serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001",
        "hostGeneration": 7,
        "connectionGeneration": 9,
        "callId": "call-1",
        "callGeneration": 1,
    }
    request = {
        "platform": "qq",
        "message_type_name": "qqcall_request",
        "content": {
            "version": 1,
            "request_id": "group_scope_mutation_rejected_01",
            "platform": "qq",
            "operation": "hangup",
            "peer_user_id": PEER,
            "scope": private_scope,
            "scope_group": SCOPE_GROUP,
        },
    }
    assert group_wire_tests.QQCallRuntime._request_envelope(request, "qq") is None


class _FakeBinding:
    def __init__(self, *, accept: bool = True, on_verify=None) -> None:
        self.accept = accept
        self.on_verify = on_verify
        self.calls: list[tuple[dict, dict]] = []

    async def verify_result(self, scope, claim) -> bool:
        self.calls.append((copy.deepcopy(scope), copy.deepcopy(claim)))
        if self.on_verify is not None:
            result = self.on_verify()
            if asyncio.iscoroutine(result):
                await result
        return self.accept and scope == SCOPE_GROUP and claim == CLAIM


class _FakeGroupAudioOwner:
    def __init__(self, binding) -> None:
        self.binding = binding
        self.state = "active"
        self.stop_calls: list[str] = []

    def status(self) -> dict:
        return {"state": self.state, "ready": self.state == "active"}

    async def verify_result(self, scope, claim) -> bool:
        return await self.binding.verify_result(scope, claim)

    async def stop(self, reason: str) -> None:
        self.stop_calls.append(reason)
        self.state = "stopped"


def _install_group_owner(runtime, binding, *, fake_native_current=True):
    owner = _FakeGroupAudioOwner(binding)
    runtime._group_audio_binding = binding
    runtime._group_audio_runtime = owner
    runtime._group_audio_requester_user_id = PEER
    runtime._group_join_scope = dict(SCOPE_GROUP)
    runtime._group_join_meta = dict(runtime._service_meta)
    runtime._group_join_client_instance = runtime._client_instance
    runtime._group_join_identity = tuple(runtime._identity)

    if fake_native_current:
        async def current(scope, requester, service_meta, client_instance):
            return bool(
                scope == SCOPE_GROUP
                and requester == PEER
                and service_meta == runtime._service_meta
                and client_instance == runtime._client_instance
            )

        runtime._read_native_group_current = current
    return owner


def _native_join_result(requester: str = PEER) -> dict:
    return {
        "version": 1,
        "accountId": group_wire_tests.ACCOUNT,
        **group_wire_tests.META,
        "joined": True,
        "scope": dict(SCOPE_GROUP),
        "source": "native_scene3_relation",
        "requesterUserId": requester,
    }


async def _group_audio_request(*, peer: str = PEER, scope=None, claim=None) -> dict:
    return await qqcall_api.request_qqcall(
        "group_audio_status",
        peer_user_id=peer,
        group_id=GROUP,
        scope_group=copy.deepcopy(SCOPE_GROUP if scope is None else scope),
        audio_claim=copy.deepcopy(CLAIM if claim is None else claim),
        timeout=1,
    )


def test_adapter_group_audio_status_requires_binding_and_echoes_proof(monkeypatch) -> None:
    async def scenario() -> None:
        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        binding = _FakeBinding()
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        result = await _group_audio_request()
        assert result == {"active": True}
        assert binding.calls
        assert binding.calls[0] == (SCOPE_GROUP, CLAIM)
        assert core.requests[0]["peer_user_id"] == PEER
        assert core.requests[0]["scope"] is None
        response = core.responses[0]
        assert response["data"] == {"active": True}
        assert response["group_id"] == GROUP
        assert response["scope_group"] == SCOPE_GROUP
        assert response["audio_claim"] == CLAIM
        assert runtime._active_call is None

    _run(scenario())


def test_join_success_guard_rechecks_current_native_scope_and_audio_owner(monkeypatch) -> None:
    async def scenario() -> None:
        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        binding = _FakeBinding()
        _install_group_owner(runtime, binding)
        request = {
            "operation": "join_group",
            "request_id": "join_success_guard_request_01",
            "peer_user_id": PEER,
            "group_id": GROUP,
        }
        identity = await runtime._read_platform_state()
        response = runtime._make_response(
            request["request_id"],
            "join_group",
            PEER,
            data=_native_join_result(),
        )

        guarded = await runtime._guard_group_join_response(request, identity, response)
        assert guarded["status"] == "ok"
        assert guarded["data"] == _native_join_result()

        stopped = []

        async def stop(reason, *, quit_native=False):
            stopped.append((reason, quit_native))

        runtime._stop_group_audio = stop

        async def no_longer_current(*_args):
            return False

        runtime._read_native_group_current = no_longer_current
        guarded = await runtime._guard_group_join_response(request, identity, response)
        assert guarded["status"] == "error"
        assert guarded["error"] == {"code": "group_join_response_invalid"}
        assert stopped == [("group_join_response_invalid", True)]

    _run(scenario())


@pytest.mark.parametrize(
    ("post_current_inactive", "occupancy_changes", "expected"),
    [
        (True, {}, True),
        (True, {"occupied": True, "kind": "group"}, False),
        (True, {"occupied": True, "kind": "private"}, False),
        (True, {"occupied": False, "kind": "group"}, False),
        (True, {"occupied": False, "kind": "private"}, False),
        (True, {"hostGeneration": 8}, False),
        (True, {"connectionGeneration": 10}, False),
        (True, {"extra": True}, False),
        (False, {}, False),
    ],
)
def test_native_group_quit_requires_current_and_leased_global_absence(
    post_current_inactive, occupancy_changes, expected,
) -> None:
    async def scenario() -> None:
        current_reads = 0
        occupancy_reads = 0
        quit_requests = []

        async def transport(path, payload, _token):
            nonlocal current_reads, occupancy_reads
            if path == "/v1/group/current":
                current_reads += 1
                assert payload == {
                    "accountId": group_wire_tests.ACCOUNT,
                    "clientInstance": "test-client",
                    "scope": SCOPE_GROUP,
                }
                if current_reads == 1 or not post_current_inactive:
                    return {
                        "version": 1,
                        "accountId": group_wire_tests.ACCOUNT,
                        **group_wire_tests.META,
                        "active": True,
                        "scope": dict(SCOPE_GROUP),
                        "requesterUserId": PEER,
                    }
                return {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    **group_wire_tests.META,
                    "active": False,
                    "scope": None,
                    "requesterUserId": None,
                }
            if path == "/v1/call/occupancy":
                occupancy_reads += 1
                assert payload == {
                    "accountId": group_wire_tests.ACCOUNT,
                    "clientInstance": "test-client",
                }
                result = {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    **group_wire_tests.META,
                    "occupied": False,
                    "kind": None,
                }
                result.update(occupancy_changes)
                return result
            assert path == "/v1/group/quit"
            quit_requests.append(copy.deepcopy(payload))
            return {
                "version": 1,
                "accountId": group_wire_tests.ACCOUNT,
                "serviceInstance": group_wire_tests.META["serviceInstance"],
                "operation": "group_quit",
                "result": {"driverAccepted": True},
            }

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport)
        identity = await runtime._read_platform_state()
        runtime._group_join_scope = dict(SCOPE_GROUP)
        runtime._group_join_meta = dict(group_wire_tests.META)
        runtime._group_join_client_instance = "test-client"
        runtime._group_join_identity = tuple(identity["identity"])
        runtime._group_audio_requester_user_id = PEER
        result = await runtime._quit_owned_native_group(
            SCOPE_GROUP,
            PEER,
            identity,
            group_wire_tests.META,
            "test-client",
        )
        assert result is expected
        assert current_reads == 2
        assert occupancy_reads == (1 if post_current_inactive else 0)
        assert quit_requests == [{
            "accountId": group_wire_tests.ACCOUNT,
            "clientInstance": "test-client",
            "scope": SCOPE_GROUP,
        }]

    _run(scenario())


def _leave_request(*, peer=PEER, group=GROUP, requester=None, request_id="group_leave_request_00000001"):
    requester = peer if requester is None else requester
    return {
        "platform": "qq",
        "message_type_name": "qqcall_request",
        "content": {
            "version": 1,
            "request_id": request_id,
            "platform": "qq",
            "operation": "leave_group",
            "peer_user_id": peer,
            "scope": None,
            "group_id": group,
            "requester_user_id": requester,
        },
    }


def test_adapter_leave_envelope_is_exact_and_cannot_supply_a_native_scope():
    request = _leave_request()
    assert group_wire_tests.QQCallRuntime._request_envelope(request, "qq") == request["content"]
    for changes in (
        {"scope": dict(SCOPE_GROUP)},
        {"requester_user_id": OTHER_PEER},
        {"trigger_text": "#leave_vc_qq"},
        {"group_id": "not-a-qq-group"},
    ):
        forged = copy.deepcopy(request)
        forged["content"].update(changes)
        assert group_wire_tests.QQCallRuntime._request_envelope(forged, "qq") is None


async def _leave_group_request(*, peer=PEER, group=GROUP, requester=None):
    return await qqcall_api.request_qqcall(
        "leave_group",
        peer_user_id=peer,
        requester_user_id=peer if requester is None else requester,
        group_id=group,
        timeout=1,
    )


def _native_current(active: bool, scope=None, requester=None):
    return {
        "version": 1,
        "accountId": group_wire_tests.ACCOUNT,
        **group_wire_tests.META,
        "active": active,
        "scope": copy.deepcopy(SCOPE_GROUP if active and scope is None else scope),
        "requesterUserId": requester if active else None,
    }


def _occupancy(*, occupied=False, kind=None):
    return {
        "version": 1,
        "accountId": group_wire_tests.ACCOUNT,
        **group_wire_tests.META,
        "occupied": occupied,
        "kind": kind,
    }


def _install_native_owner(runtime, owner):
    runtime._group_audio_binding = owner.binding
    runtime._group_audio_runtime = owner
    runtime._group_audio_requester_user_id = PEER
    runtime._group_join_scope = dict(SCOPE_GROUP)
    runtime._group_join_meta = dict(runtime._service_meta)
    runtime._group_join_client_instance = runtime._client_instance
    runtime._group_join_identity = tuple(runtime._identity)


def test_leave_group_requires_native_current_and_global_occupancy_absence(monkeypatch):
    async def scenario():
        paths = []
        current_reads = 0
        quit_requests = []
        owner = None

        async def transport(path, payload, _token):
            nonlocal current_reads
            paths.append(path)
            if path == "/v1/group/current":
                current_reads += 1
                assert payload == {
                    "accountId": group_wire_tests.ACCOUNT,
                    "clientInstance": "test-client",
                    "scope": SCOPE_GROUP,
                }
                return _native_current(current_reads == 1, requester=PEER)
            if path == "/v1/group/quit":
                quit_requests.append(copy.deepcopy(payload))
                return {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    "serviceInstance": group_wire_tests.META["serviceInstance"],
                    "operation": "group_quit",
                    "result": {"driverAccepted": True},
                }
            if path == "/v1/call/occupancy":
                assert payload == {
                    "accountId": group_wire_tests.ACCOUNT,
                    "clientInstance": "test-client",
                }
                return _occupancy()
            raise AssertionError(f"unexpected native endpoint {path}")

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport)
        owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
        _install_native_owner(runtime, owner)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _leave_group_request() == {"left": True}
        assert core.requests[0] == {
            "version": 1,
            "request_id": core.requests[0]["request_id"],
            "platform": "qq",
            "operation": "leave_group",
            "peer_user_id": PEER,
            "scope": None,
            "group_id": GROUP,
            "requester_user_id": PEER,
        }
        assert quit_requests == [{
            "accountId": group_wire_tests.ACCOUNT,
            "clientInstance": "test-client",
            "scope": SCOPE_GROUP,
        }]
        assert current_reads == 3  # active check, post-quit absence, and send-bound replay proof
        assert paths.count("/v1/call/occupancy") == 2
        assert owner.stop_calls == ["stopped"]
        assert runtime._group_join_scope is None
        assert core.responses[-1]["data"] == {"left": True}
        assert core.responses[-1]["group_id"] == GROUP
        assert core.responses[-1]["requester_user_id"] == PEER

    _run(scenario())


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no_owner", "group_not_joined"),
        ("wrong_group", "group_scope_stale"),
        ("wrong_requester", "group_requester_mismatch"),
        ("acl", "group_not_admitted"),
        ("core", "core_not_ready"),
        ("lease", "adapter_lease_required"),
        ("stale_scope", "stale_generation"),
        ("stale_identity", "platform_identity_changed"),
    ],
)
def test_group_leave_denials_never_dispatch_native_quit(monkeypatch, case, expected):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            raise AssertionError("denied leave reached a native endpoint")

        def admitted(_peer, _group):
            return case != "acl"

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport, admit_group=admitted)
        if case != "no_owner":
            owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
            _install_native_owner(runtime, owner)
        peer = PEER
        group = GROUP
        if case == "wrong_group":
            group = group_wire_tests.OTHER_GROUP
        elif case == "wrong_requester":
            peer = OTHER_PEER
        elif case == "core":
            runtime._core_ready = False
        elif case == "lease":
            runtime._lease_claimed = False
        elif case == "stale_scope":
            runtime._group_join_meta = dict(runtime._group_join_meta, hostGeneration=8)
        elif case == "stale_identity":
            runtime._group_join_identity = (object(), 5, group_wire_tests.ACCOUNT)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await _leave_group_request(peer=peer, group=group)
        assert exc_info.value.code == expected
        assert paths == []
        assert not any(path == "/v1/group/quit" for path in paths)

    _run(scenario())


@pytest.mark.parametrize("occupancy", [{"occupied": True, "kind": "group"}, {"occupied": False, "kind": "group"}])
def test_group_leave_ack_without_terminal_absence_stays_unconfirmed_and_quarantined(monkeypatch, occupancy):
    async def scenario():
        paths = []
        current_reads = 0
        owner = None

        async def transport(path, payload, _token):
            nonlocal current_reads
            paths.append(path)
            if path == "/v1/group/current":
                current_reads += 1
                return _native_current(current_reads == 1, requester=PEER)
            if path == "/v1/group/quit":
                return {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    "serviceInstance": group_wire_tests.META["serviceInstance"],
                    "operation": "group_quit",
                    "result": {"driverAccepted": True},
                }
            if path == "/v1/call/occupancy":
                result = _occupancy()
                result.update(occupancy)
                return result
            raise AssertionError(f"unexpected native endpoint {path}")

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport)
        owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
        _install_native_owner(runtime, owner)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await _leave_group_request()
        assert exc_info.value.code == "group_leave_unconfirmed"
        assert owner.stop_calls == ["stopped"]
        assert runtime._group_join_scope == SCOPE_GROUP
        assert runtime._group_audio_native_quit_unconfirmed is True
        assert paths.count("/v1/group/quit") == 1
        assert paths.count("/v1/call/occupancy") == 2

    _run(scenario())


def test_group_leave_rechecks_acl_after_native_current_read_before_quit(monkeypatch):
    async def scenario():
        paths = []
        admission_calls = 0

        def admit(_peer, _group):
            nonlocal admission_calls
            admission_calls += 1
            return admission_calls == 1

        async def transport(path, _payload, _token):
            paths.append(path)
            if path == "/v1/group/current":
                return _native_current(True, requester=PEER)
            raise AssertionError(f"unexpected native endpoint {path}")

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, admit_group=admit, transport=transport)
        owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
        _install_native_owner(runtime, owner)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await _leave_group_request()
        assert exc_info.value.code == "group_not_admitted"
        assert admission_calls >= 2
        assert owner.stop_calls == ["stopped"]
        assert paths == ["/v1/group/current"]
        assert runtime._group_join_scope == SCOPE_GROUP

    _run(scenario())


def test_group_leave_preserves_audio_owner_if_local_stop_fails(monkeypatch):
    async def scenario():
        paths = []

        async def transport(path, _payload, _token):
            paths.append(path)
            raise AssertionError("native quit must wait for local audio shutdown")

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport)
        owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
        _install_native_owner(runtime, owner)

        async def failed_stop(reason):
            owner.stop_calls.append(reason)
            raise RuntimeError("fixture cleanup failure")

        owner.stop = failed_stop
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.raises(qqcall_api.QQCallAPIError) as exc_info:
            await _leave_group_request()
        assert exc_info.value.code == "group_leave_unconfirmed"
        assert owner.stop_calls == ["stopped"]
        assert runtime._group_audio_runtime is owner
        assert runtime._group_join_scope == SCOPE_GROUP
        assert paths == []

    _run(scenario())


def test_duplicate_group_leave_revalidates_old_scope_without_quitting_next_room(monkeypatch):
    async def scenario():
        paths = []
        current_scope = dict(SCOPE_GROUP)
        current_active = True
        quit_requests = []

        async def transport(path, payload, _token):
            nonlocal current_active
            paths.append(path)
            if path == "/v1/group/current":
                assert payload["scope"] == SCOPE_GROUP or payload["scope"] == current_scope
                return {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    **group_wire_tests.META,
                    "active": current_active,
                    "scope": dict(current_scope) if current_active else None,
                    "requesterUserId": PEER if current_active else None,
                }
            if path == "/v1/group/quit":
                quit_requests.append(copy.deepcopy(payload))
                current_active = False
                return {
                    "version": 1,
                    "accountId": group_wire_tests.ACCOUNT,
                    "serviceInstance": group_wire_tests.META["serviceInstance"],
                    "operation": "group_quit",
                    "result": {"driverAccepted": True},
                }
            if path == "/v1/call/occupancy":
                return _occupancy()
            raise AssertionError(f"unexpected native endpoint {path}")

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=transport)
        owner = _install_group_owner(runtime, _FakeBinding(), fake_native_current=False)
        _install_native_owner(runtime, owner)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _leave_group_request() == {"left": True}
        request_id = core.requests[0]["request_id"]
        old_scope = dict(SCOPE_GROUP)
        current_scope = {**SCOPE_GROUP, "roomId": "room-next", "roomGeneration": 5}
        current_active = True
        runtime._group_join_scope = dict(current_scope)
        runtime._group_join_meta = dict(group_wire_tests.META)
        runtime._group_join_client_instance = "test-client"
        runtime._group_join_identity = tuple(runtime._identity)
        runtime._group_audio_requester_user_id = PEER
        runtime._group_audio_binding = _FakeBinding()
        next_owner = _FakeGroupAudioOwner(runtime._group_audio_binding)
        runtime._group_audio_runtime = next_owner

        response = await runtime.handle_request({
            "platform": "qq",
            "message_type_name": "qqcall_request",
            "content": {
                "version": 1,
                "request_id": request_id,
                "platform": "qq",
                "operation": "leave_group",
                "peer_user_id": PEER,
                "scope": None,
                "group_id": GROUP,
                "requester_user_id": PEER,
            },
        })
        assert response is not None and response["status"] == "error"
        assert response["error"] == {"code": "group_leave_unconfirmed"}
        assert quit_requests == [{
            "accountId": group_wire_tests.ACCOUNT,
            "clientInstance": "test-client",
            "scope": old_scope,
        }]
        assert paths.count("/v1/group/quit") == 1
        assert runtime._group_join_scope == current_scope
        assert runtime._group_audio_runtime is next_owner

    _run(scenario())


def test_native_group_status_and_join_transport_wait_for_receipt_without_changing_other_paths(monkeypatch) -> None:
    async def scenario() -> None:
        async def transport(_path, _payload, _token):
            await asyncio.sleep(0.01)
            return {}

        runtime = group_wire_tests._runtime(group_wire_tests._CoreLoopback(), transport=transport)
        monkeypatch.setattr(group_wire_tests.qqcall_runtime, "_GROUP_JOIN_HTTP_TIMEOUT_SEC", 0.1)
        monkeypatch.setattr(group_wire_tests.qqcall_runtime, "_GROUP_STATUS_HTTP_TIMEOUT_SEC", 0.1)
        monkeypatch.setattr(group_wire_tests.qqcall_runtime, "_HTTP_TOTAL_TIMEOUT_SEC", 0.001)

        assert await runtime._post("/v1/group/join", {}) == {}
        assert await runtime._post("/v1/group/status", {}) == {}
        with pytest.raises(group_wire_tests.qqcall_runtime.QQCallError, match="service_unavailable"):
            await runtime._post("/v1/group/diagnostics", {})

    _run(scenario())


def test_adapter_proof_drops_after_owned_binding_lease_revocation(monkeypatch) -> None:
    async def scenario() -> None:
        lease_current = True
        accepted = []

        async def validator(_scope) -> bool:
            return lease_current

        async def on_result(envelope) -> None:
            accepted.append(envelope)

        client = binding_tests.FakeClient()
        binding = binding_tests.QQCallGroupUniversalBinding(client, validator, on_result)
        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        with pytest.MonkeyPatch.context() as scoped_patch:
            scoped_patch.setattr(binding_tests.group_api, "_LEASE_MONITOR_INTERVAL", 60.0)
            await binding.start(dict(SCOPE_GROUP))
            await client.session.push(binding_tests.make_event(client.session, turn_id="lease-proof-turn"))
            await binding_tests.wait_until(lambda: len(accepted) == 1)
            event = accepted[0]
            claim = {
                "turn_id": event["speech_result"]["turn_id"],
                "uvc_session": event["session_identity"],
                "speaker_ref": event["speaker_annotation"]["speaker_ref"],
            }

            assert await _group_audio_request(claim=claim) == {"active": True}
            lease_current = False
            assert await _group_audio_request(claim=claim) == {"active": False}
            assert core.responses[-1]["data"] == {"active": False}
            await binding.close()

        assert client.session.close_count == 1
        assert not client.closed

    _run(scenario())


def test_diagnostic_native_join_claim_never_substitutes_for_binding(monkeypatch) -> None:
    async def scenario() -> None:
        diagnostic_reads = []

        async def diagnostics(_path, _payload, _token):
            diagnostic_reads.append(True)
            return {"nativeJoinVerified": True, "roomObserved": True}

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, transport=diagnostics)
        runtime._group_audio_binding = None
        runtime._group_audio_requester_user_id = PEER
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        assert await _group_audio_request() == {"active": False}
        assert diagnostic_reads == []
        assert core.responses[0]["data"] == {"active": False}

    _run(scenario())


@pytest.mark.parametrize("failure", ["no_binding", "wrong_requester", "private_call", "acl_denied", "binding_mismatch"])
def test_adapter_group_audio_status_fails_closed(monkeypatch, failure) -> None:
    async def scenario() -> None:
        admitted = []

        def admit(peer, group):
            admitted.append((peer, group))
            return failure != "acl_denied"

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, admit_group=admit)
        binding = _FakeBinding(accept=failure != "binding_mismatch")
        if failure != "no_binding":
            _install_group_owner(runtime, binding)
        if failure == "private_call":
            runtime._active_call = {"scope": {"callId": "private"}}
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        result = await _group_audio_request(peer=OTHER_PEER if failure == "wrong_requester" else PEER)
        assert result == {"active": False}
        assert core.responses[0]["data"] == {"active": False}
        assert core.responses[0]["peer_user_id"] == (
            OTHER_PEER if failure == "wrong_requester" else PEER
        )
        assert core.responses[0]["scope_group"] == SCOPE_GROUP
        assert core.responses[0]["audio_claim"] == CLAIM
        if failure in {"no_binding", "wrong_requester", "private_call", "acl_denied"}:
            assert not binding.calls
        if failure == "acl_denied":
            assert admitted == [(PEER, GROUP)]

    _run(scenario())


def test_adapter_rechecks_group_acl_after_async_proof(monkeypatch) -> None:
    async def scenario() -> None:
        allowed = True
        admission_calls = []

        def admit(peer, group):
            nonlocal allowed
            admission_calls.append((peer, group))
            return allowed

        def revoke() -> None:
            nonlocal allowed
            allowed = False

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core, admit_group=admit)
        binding = _FakeBinding(on_verify=revoke)
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)

        result = await _group_audio_request()
        assert result == {"active": False}
        assert admission_calls == [(PEER, GROUP), (PEER, GROUP), (PEER, GROUP)]
        assert core.responses[0]["data"] == {"active": False}

    _run(scenario())


@pytest.mark.parametrize(
    ("which", "replacement"),
    [
        ("serviceInstance", "stale-service"),
        ("hostGeneration", 8),
        ("connectionGeneration", 10),
        ("roomGeneration", 5),
    ],
)
def test_adapter_rejects_stale_scope_or_binding_claim(monkeypatch, which, replacement) -> None:
    async def scenario() -> None:
        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        binding = _FakeBinding()
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)
        scope = {**SCOPE_GROUP, which: replacement}

        result = await _group_audio_request(scope=scope)
        assert result == {"active": False}
        assert core.responses[0]["data"] == {"active": False}

    _run(scenario())


@pytest.mark.parametrize(
    ("which", "replacement"),
    [
        ("session_id", "other-session"),
        ("session_generation", "2"),
        ("turn_id", "other-turn"),
        ("speaker_ref", "qqg-fedcba9876543210"),
    ],
)
def test_adapter_rejects_unmatched_uvc_turn_claim(monkeypatch, which, replacement) -> None:
    async def scenario() -> None:
        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        binding = _FakeBinding()
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)
        claim = copy.deepcopy(CLAIM)
        if which in claim["uvc_session"]:
            claim["uvc_session"][which] = replacement
        else:
            claim[which] = replacement

        result = await _group_audio_request(claim=claim)
        assert result == {"active": False}
        assert core.responses[0]["data"] == {"active": False}

    _run(scenario())


def test_audio_claim_source_must_be_main_and_requester_is_not_speaker(monkeypatch) -> None:
    async def scenario() -> None:
        bad_claim = copy.deepcopy(CLAIM)
        bad_claim["uvc_session"]["source_id"] = "other"
        with pytest.raises(qqcall_api.QQCallAPIError, match="invalid_group_audio_claim"):
            await _group_audio_request(claim=bad_claim)

        core = group_wire_tests._CoreLoopback()
        runtime = group_wire_tests._runtime(core)
        binding = _FakeBinding()
        _install_group_owner(runtime, binding)
        monkeypatch.setattr(qqcall_api, "get_global_api", lambda: core)
        assert await _group_audio_request() == {"active": True}
        assert binding.calls[0][1]["uvc_session"]["source_id"] == "main"
        assert PEER not in repr(binding.calls[0][1])

    _run(scenario())
