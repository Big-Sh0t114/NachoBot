from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import replace

import pytest

from test_qqcall_group_wire import ACCOUNT, GROUP, META, PEER, _CoreLoopback, _runtime
from qqcall_client.qqcall import QQCallError

UID = "u_" + "P" * 22
INVITE_ID = "123e4567-e89b-42d3-a456-426614174000"
SCOPE = {"kind": "qqcall_group", **META, "groupId": GROUP, "roomId": GROUP, "roomGeneration": 1}


def invitation_poll(age_ms=0):
    return {
        "version": 1, "accountId": ACCOUNT, **META, "cursor": 1, "currentCall": None,
        "events": [{"sequence": 1, "kind": "group_invite", "call": None, "groupInvite": {
            "inviteId": INVITE_ID, "groupId": GROUP, "callerUid": UID,
            "observedAtMs": int(time.time() * 1000) - age_ms,
        }}],
    }


@pytest.mark.parametrize("case", ["allowed", "denied", "expired", "unmapped", "occupied", "core_unready"])
def test_invitation_poll_uses_real_group_admission_without_fabricated_command(case):
    async def scenario():
        core = _CoreLoopback()
        paths, activations = [], []
        poll = invitation_poll(30_000 if case == "expired" else 0)

        async def transport(path, body, _token):
            paths.append(path)
            if path == "/v1/adapter/poll":
                return copy.deepcopy(poll)
            if path == "/v1/group/status":
                assert body["groupId"] == GROUP
                return {"version": 1, "accountId": ACCOUNT, **META, "groupId": GROUP,
                        "occupied": case == "occupied", "nativeJoinVerified": True,
                        "room": {"groupId": GROUP, "roomId": GROUP, "roomGeneration": 1,
                                 "live": True, "observedAtMs": int(time.time() * 1000)}}
            if path == "/v1/group/join":
                assert body["triggerKind"] == "invite"
                assert body["inviteId"] == INVITE_ID
                assert body["requesterUserId"] == PEER
                assert "trigger_text" not in body
                return {"version": 1, "accountId": ACCOUNT, **META, "joined": True,
                        "scope": dict(SCOPE), "source": "native_scene3_relation", "requesterUserId": PEER}
            assert path == "/v1/group/current"
            return {"version": 1, "accountId": ACCOUNT, **META, "active": True,
                    "scope": dict(SCOPE), "requesterUserId": PEER}

        runtime = _runtime(core, transport=transport, admit_group=lambda _peer, _group: case != "denied")
        runtime.config = replace(runtime.config, peer_map={} if case == "unmapped" else {UID: PEER})
        runtime._bootstrapped = True

        async def activate(scope, peer, *_args):
            activations.append((scope, peer))

        runtime._activate_group_audio = activate
        identity = await runtime._read_platform_state()
        if case == "core_unready":
            identity["core_ready"] = False
        assert await runtime._poll_once(identity)
        assert runtime._cursor == 1
        assert bool(activations) == (case == "allowed")
        assert ("/v1/group/join" in paths) == (case == "allowed")
        await runtime._poll_once(identity)
        assert len(activations) == (1 if case == "allowed" else 0), "a repeated poll never retries the invitation"
        assert core.requests == [] and core.responses == [], "invitation is not a forged user/Core command"

    asyncio.run(scenario())


def test_malformed_invitation_and_public_forged_invite_id_are_rejected():
    runtime = _runtime(_CoreLoopback())
    malformed = invitation_poll()
    malformed["events"][0]["groupInvite"]["observedAtMs"] = True
    with pytest.raises(QQCallError, match="service_event_invalid"):
        runtime._validate_poll(malformed)
    from test_qqcall_group_wire import _group_request
    forged = _group_request()
    forged["content"]["inviteId"] = INVITE_ID
    assert runtime._request_envelope(forged, "qq") is None
