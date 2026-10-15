from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client.qqcall import (  # noqa: E402
    QQCallConfig,
    QQCallRuntime,
    is_qqcall_delivery_target,
)


ACCOUNT = "123456789"
REQUESTER = "987654321"
REQUEST_ID = "group_output_request_00000001"
GROUP_SCOPE = {
    "kind": "qqcall_group",
    "serviceInstance": "service-instance-group-1",
    "hostGeneration": 7,
    "connectionGeneration": 9,
    "groupId": "246813579",
    "roomId": "room-opaque-1",
    "roomGeneration": 3,
}


def _target(**updates):
    value = {
        **GROUP_SCOPE,
        "requester_user_id": REQUESTER,
        "turn_id": "turn-group-1",
    }
    value.update(updates)
    return value


def _raw(target=None, segment=None):
    result = {"additional_config": {"delivery_target": target}}
    if segment is not None:
        result["message_segment"] = segment
    return result


def _runtime(*, enabled: bool = True):
    seen = {"custom": [], "status": []}
    identity = (object(), 4, ACCOUNT)

    async def send_message(_message):
        return True

    async def send_custom(kind, payload):
        seen["custom"].append((kind, dict(payload)))
        return True

    async def read_state():
        return {"connected": True, "account_id": ACCOUNT, "identity": identity}

    async def admit_peer(_peer):
        return True

    config = QQCallConfig.from_mapping({
        "enabled": enabled,
        "endpoint": "http://127.0.0.1:6112",
        "token_file": "C:\\test\\qqcall.token",
        "account_id": ACCOUNT,
        "peer_map": {},
    })
    runtime = QQCallRuntime(
        config,
        "qq",
        send_message,
        send_custom,
        read_state,
        admit_peer,
        on_status=lambda event: seen["status"].append(event),
    )
    return runtime, seen


def test_delivery_target_helper_consumes_declared_group_kinds_even_when_malformed():
    assert is_qqcall_delivery_target(_raw(_target()))
    assert is_qqcall_delivery_target(_raw({"kind": "qqcall_group"}))
    assert is_qqcall_delivery_target(_raw({"kind": "qqcall"}))
    assert not is_qqcall_delivery_target(_raw({"kind": "ordinary"}))
    assert not is_qqcall_delivery_target({"additional_config": {}})


def test_group_targets_are_consumed_as_sanitized_unavailable_or_stale_responses():
    cases = (
        (False, _target(request_id=REQUEST_ID), "group_audio_unavailable", REQUEST_ID),
        (True, {"kind": "qqcall_group"}, "stale_group_scope", None),
        (True, _target(serviceInstance="stale-service-instance", request_id=REQUEST_ID), "group_audio_unavailable", REQUEST_ID),
        (True, _target(request_id="invalid"), "stale_group_scope", None),
    )

    for enabled, target, code, request_id in cases:
        runtime, seen = _runtime(enabled=enabled)

        async def forbidden_private_voice(*_args):
            raise AssertionError("group delivery must not use the private voice path")

        with patch.object(runtime._voice, "intercept_outbound", side_effect=forbidden_private_voice) as voice_send:
            assert asyncio.run(runtime.intercept_outbound(_raw(target))) is True

        voice_send.assert_not_called()
        assert len(seen["custom"]) == 1
        kind, response = seen["custom"][0]
        assert kind == "qqcall_delivery_response"
        assert response["version"] == 1
        assert response["platform"] == "qq"
        assert response["status"] == "error"
        assert response["error"] == {"code": code}
        assert "result" not in response
        assert (response.get("request_id") == request_id)
        assert set(response) == ({"version", "platform", "status", "error"} | ({"request_id"} if request_id else set()))
        assert runtime.status()["lastFailureCode"] == code
        assert seen["status"][-1]["kind"] == "delivery_rejected"


class _GroupAudioOwner:
    def __init__(self, *, current=True):
        self.current = current
        self.state = "active"
        self.played = []

    def status(self):
        return {"ready": True, "state": "active"}

    async def verify_current_turn(self, scope, requester, turn_id):
        return self.current and scope == GROUP_SCOPE and requester == REQUESTER and turn_id == "turn-group-1"

    async def play_core_segment(self, scope, requester, turn_id, kind, data):
        self.played.append((kind, data))
        return {"render_drained": True, "drain_confirmed": True}


def _install_group_owner(runtime, owner):
    runtime._group_audio_runtime = owner
    runtime._group_audio_binding = object()
    runtime._group_audio_requester_user_id = REQUESTER
    runtime._group_join_scope = dict(GROUP_SCOPE)
    runtime._group_join_meta = {
        key: GROUP_SCOPE[key]
        for key in ("serviceInstance", "hostGeneration", "connectionGeneration")
    }
    runtime._group_join_client_instance = "qtestclient"
    runtime._group_join_identity = (object(), 4, ACCOUNT)
    runtime._service_meta = dict(runtime._group_join_meta)
    runtime._client_instance = "qtestclient"
    runtime._identity = tuple(runtime._group_join_identity)
    runtime._identity_account = ACCOUNT
    runtime._lease_claimed = True
    runtime._active_call = None


def test_adapter_routing_simulation_consumes_runtime_and_absent_runtime_group_targets():
    group_message = _raw(_target())
    ordinary_message = _raw({"kind": "ordinary"})
    runtime, _seen = _runtime(enabled=False)

    async def napcat_route(raw, active_runtime):
        if active_runtime is not None:
            if await active_runtime.intercept_outbound(raw):
                return "consumed"
        elif is_qqcall_delivery_target(raw):
            return "consumed"
        return "ordinary_send"

    async def snowluma_route(raw, active_runtime):
        if await active_runtime.intercept_outbound(raw):
            return "consumed"
        return "ordinary_send"

    assert asyncio.run(napcat_route(group_message, runtime)) == "consumed"
    assert asyncio.run(snowluma_route(group_message, runtime)) == "consumed"
    assert asyncio.run(napcat_route(group_message, None)) == "consumed"
    assert asyncio.run(napcat_route(ordinary_message, None)) == "ordinary_send"
    assert asyncio.run(snowluma_route(ordinary_message, runtime)) == "ordinary_send"
    assert asyncio.run(runtime.intercept_outbound(ordinary_message)) is False
    assert is_qqcall_delivery_target(ordinary_message) is False


def test_group_voice_and_stream_replies_use_only_owned_local_audio_and_text_is_acknowledged():
    runtime, seen = _runtime()
    owner = _GroupAudioOwner()
    _install_group_owner(runtime, owner)
    target = _target(request_id=REQUEST_ID)
    message = _raw(target, {
        "type": "seglist",
        "data": [
            {"type": "tts_text", "data": "display acknowledgement"},
            {"type": "voice", "data": "core-wav"},
            {
                "type": "voice_stream",
                "data": {"event": "end", "stream_id": "stream-1"},
            },
        ],
    })

    async def forbidden_private_voice(*_args):
        raise AssertionError("group speech must stay off the private voice pipeline")

    with patch.object(runtime._voice, "intercept_outbound", side_effect=forbidden_private_voice) as voice_send:
        assert asyncio.run(runtime.intercept_outbound(message)) is True

    voice_send.assert_not_called()
    assert owner.played == [
        ("voice", "core-wav"),
        ("voice_stream", {"event": "end", "stream_id": "stream-1"}),
    ]
    kind, response = seen["custom"][0]
    assert kind == "qqcall_delivery_response"
    assert response == {
        "version": 1,
        "platform": "qq",
        "status": "accepted",
        "result": {"code": "local_endpoint_drained", "drain_confirmed": True},
        "request_id": REQUEST_ID,
    }


def test_group_stale_turn_is_consumed_without_private_or_ordinary_send_fallback():
    runtime, seen = _runtime()
    owner = _GroupAudioOwner(current=False)
    _install_group_owner(runtime, owner)
    message = _raw(_target(request_id=REQUEST_ID), {"type": "voice", "data": "stale-core-wav"})

    async def forbidden_private_voice(*_args):
        raise AssertionError("group speech must stay off the private voice pipeline")

    with patch.object(runtime._voice, "intercept_outbound", side_effect=forbidden_private_voice) as voice_send:
        assert asyncio.run(runtime.intercept_outbound(message)) is True

    voice_send.assert_not_called()
    assert owner.played == []
    assert seen["custom"][0][1]["status"] == "error"
    assert seen["custom"][0][1]["error"] == {"code": "stale_group_scope"}
