from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import toml
from ncnk_message import BaseMessageInfo, GroupInfo, Seg, UserInfo
from qqcall_client import qqcall_service
from src.config.config import global_config
from src.chat.message_receive.message import MessageRecv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.qqcall_plugin import plugin as qqcall_plugin  # noqa: E402
from plugins.qqcall_plugin.control_api import QQCallAPIError  # noqa: E402


PEER = "123456789"


def test_dependency_failure_reply_names_all_missing_links_without_claiming_acceptance():
    text = qqcall_plugin._error_reply(QQCallAPIError("call_missing_tts_universal"))
    assert "TTS" in text and "UniversalVC" in text and "暂不能接听或加入通话" in text
    assert "已接听" not in text and "已加入" not in text


def test_recent_empty_room_notice_describes_observation_without_claiming_join():
    text = qqcall_plugin._error_reply(QQCallAPIError("group_room_observed_empty"))
    assert "QQ刚报告" in text and "无人在线" in text and "未执行加入" in text
    assert "已加入" not in text

OTHER_PEER = "987654321"
SCOPE = {
    "serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001",
    "hostGeneration": 7,
    "connectionGeneration": 9,
    "callId": "call-abc123",
    "callGeneration": 11,
}


def _run(coro):
    return asyncio.run(coro)


def _write_canonical_toml(monkeypatch, tmp_path, *, enabled, core=None):
    service_root = tmp_path / "NachoBot"
    canonical_path = service_root / "plugins" / "qqcall_plugin" / "config.toml"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(qqcall_service, "_SERVICE_PROJECT_ROOT", service_root)
    monkeypatch.setattr(qqcall_service, "canonical_qqcall_config_path", lambda: canonical_path)

    raw = copy.deepcopy(qqcall_service._fixed_validator().DEFAULT_CONFIG)
    raw["plugin"]["enabled"] = enabled
    raw["qqcall"].update(
        {
            "runtime_dir": str(service_root / ".runtime" / "service"),
            "allow_planner_accept": False,
            "group_intent_model_task": "utils",
            "group_intent_timeout_seconds": 10,
        }
    )
    if enabled:
        # These synthetic values satisfy schema validation only; the tests
        # never start the service, connect to OneBot, or open audio devices.
        raw["qqcall"]["account_id"] = PEER
        raw["qqcall"]["onebot"]["url"] = "ws://127.0.0.1:3001"
        raw["qqcall"]["paths"]["qq_exe"] = "C:/fixture/QQ.exe"
        raw["qqcall"]["audio"]["capture_device"] = "fixture-capture"
        raw["qqcall"]["audio"]["playback_device"] = "fixture-playback"
    if core:
        raw["qqcall"].update(core)
    canonical_path.write_text(toml.dumps(raw), encoding="utf-8")
    return service_root, canonical_path


@pytest.fixture(autouse=True)
def _canonical_qqcall_enabled_by_default(monkeypatch):
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: True)


def _plugin_config(allow_planner=True):
    return {
        "plugin": {"enabled": True},
        "qqcall": {
            "allow_planner_accept": allow_planner,
            "group_intent_model_task": "utils",
            "group_intent_timeout_seconds": 10,
        },
    }


def _status(peer_user_id=PEER, scope=SCOPE, **changes):
    call = {
        "callId": scope["callId"],
        "generation": scope["callGeneration"],
        "phase": "invite_ready",
        "uncertain": False,
        "inviteCallbackReady": True,
        "inviteCorrelation": "kernel_from_uid_matched",
        "acceptState": "not_requested",
    }
    call.update(changes)
    return {"scope": dict(scope), "call": call, "peer_user_id": peer_user_id}


def _mutation(scope=SCOPE):
    return {
        "scope": dict(scope),
        "result": {
            "posted": True,
            "id": 55,
            "callId": scope["callId"],
            "callGeneration": scope["callGeneration"],
        },
    }


def _private_message(*, peer=PEER, platform="qq", group=None, sender=True, stream_peer=PEER):
    user_info = SimpleNamespace(platform=platform, user_id=peer) if sender else None
    stream_user = SimpleNamespace(platform=platform, user_id=stream_peer) if stream_peer else None
    stream = SimpleNamespace(
        stream_id="private-stream",
        platform=platform,
        user_info=stream_user,
        group_info=group,
    )
    info = SimpleNamespace(platform=platform, user_info=user_info, group_info=group, additional_config={})
    return SimpleNamespace(message_info=info, chat_stream=stream, processed_plain_text="#stat_vc_qq")


def _live_recovery_metadata(*, recovered=False, replayed=False, received_age=0.0, received_future=0.0):
    now = time.time()
    return {
        "version": 1,
        "connection_epoch_started_at": now - 60,
        "received_at": now - received_age + received_future,
        "original_timestamp": now - 7200 if replayed else now,
        "timestamp_source": "platform_seconds",
        "progress_id": "fixture-progress",
        "delivery": {"recovered": recovered},
    }


def _group_message(
    text,
    *,
    peer=PEER,
    group="445566",
    stream_peer=PEER,
    stream_group=None,
    platform="qq",
    sender=True,
    recovered=False,
    replayed=False,
    received_age=0.0,
    received_future=0.0,
    system_event=False,
    segment_kind="text",
    extra_segments=None,
):
    group_value = SimpleNamespace(platform=platform, group_id=group)
    stream_group_value = SimpleNamespace(platform=platform, group_id=group if stream_group is None else stream_group)
    user_info = SimpleNamespace(platform=platform, user_id=peer) if sender else None
    stream_user = SimpleNamespace(platform=platform, user_id=stream_peer) if stream_peer else None
    additional = {
        "offline_recovery": _live_recovery_metadata(
            recovered=recovered,
            replayed=replayed,
            received_age=received_age,
            received_future=received_future,
        )
    }
    if system_event:
        additional["system_event"] = {"version": 1, "type": "qq.call.fake", "data": {}}
    if segment_kind == "mixed":
        segment = Seg("seglist", [Seg("text", text), Seg("image", "opaque")])
    elif segment_kind in {"reply", "forward"}:
        segment = Seg(segment_kind, extra_segments or [])
    else:
        segment = Seg(segment_kind, "fixture-audio" if segment_kind == "voice" else text)
    message_info = BaseMessageInfo(
        platform=platform,
        message_id="message-1",
        time=time.time(),
        group_info=group_value,
        user_info=user_info,
        additional_config=additional,
    )
    stream = SimpleNamespace(
        stream_id="group-stream",
        platform=platform,
        user_info=stream_user,
        group_info=stream_group_value,
    )
    return SimpleNamespace(
        message_info=message_info,
        chat_stream=stream,
        message_segment=segment,
        processed_plain_text=text,
        raw_message=text,
        reply=None,
    )


def _invite_message(*, peer=PEER, stream_peer=PEER, group=None, sender=False, event_type="qq.call.invite_callback_ready", scope=SCOPE, event_data=None):
    route = {"platform": "qq", "kind": "private", "peer": {"user_id": peer}}
    data = event_data or {
        "call_id": scope["callId"],
        "call_generation": scope["callGeneration"],
        "phase": "invite_ready",
        "uncertain": False,
        "invite_callback_ready": True,
        "invite_correlation": "kernel_from_uid_matched",
        "accept_state": "not_requested",
    }
    additional_config = {
        "system_event": {"version": 1, "type": event_type, "actor": None, "target": None, "data": data},
        "system_event_route": route,
        "qqcall_scope": dict(scope),
    }
    message_info = SimpleNamespace(
        platform="qq",
        user_info=SimpleNamespace(user_id=peer) if sender else None,
        group_info=group,
        additional_config=additional_config,
    )
    chat_info = SimpleNamespace(platform="qq", group_info=group)
    action_message = SimpleNamespace(
        message_info=message_info,
        chat_info=chat_info,
        additional_config=additional_config,
        user_info=message_info.user_info,
        sender_info=None,
    )
    stream = SimpleNamespace(
        stream_id="private-stream",
        platform="qq",
        user_info=SimpleNamespace(user_id=stream_peer) if stream_peer else None,
        group_info=group,
    )
    return action_message, stream


def _new_command(message, config=None, operation="status"):
    command = qqcall_plugin.QQCallCommand(message, config or _plugin_config())
    command.set_matched_groups({"operation": operation} if operation is not None else {"operation": ""})
    command.replies = []

    async def send_text(content, **kwargs):
        command.replies.append(content)
        return True

    command.send_text = send_text
    return command


async def _approve_group_intent(*args, **kwargs):
    return qqcall_plugin._GroupIntentReviewResult(qqcall_plugin._GroupIntentReviewOutcome.APPROVED)


def _intent_plugin_config(**overrides):
    config = _plugin_config()
    config["qqcall"].update(overrides)
    return config


def _stub_intent_model(monkeypatch, response='{"join":true}', *, success=True, error=None, wait=None, models=None):
    calls = []
    model_config = object()
    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        qqcall_plugin.llm_api,
        "get_available_models",
        lambda: {"utils": model_config} if models is None else models,
    )

    async def generate(prompt, selected_model, **kwargs):
        calls.append((prompt, selected_model, kwargs))
        if wait is not None:
            await wait()
        if error is not None:
            raise error
        return success, response, "unlogged reasoning", "test-model"

    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", generate)
    return calls, model_config


def _new_action(action_message, stream, config=None, action_data=None):
    return qqcall_plugin.QQCallAcceptAction(
        action_data=action_data or {},
        reasoning="",
        cycle_timers={},
        thinking_id="thinking-1",
        chat_stream=stream,
        plugin_config=config or _plugin_config(),
        action_message=action_message,
    )


def test_plugin_reads_canonical_toml_switch_and_ignores_local_or_legacy_json(
    monkeypatch, tmp_path
):
    service_root, _ = _write_canonical_toml(
        monkeypatch,
        tmp_path,
        enabled=False,
        core={"allow_planner_accept": False},
    )
    (service_root / "config.json").write_text('{"schema": 2, "enabled": true}', encoding="utf-8")
    plugin_dir = tmp_path / "qqcall_plugin"
    plugin_dir.mkdir()
    shutil.copyfile(PROJECT_ROOT / "plugins" / "qqcall_plugin" / "_manifest.json", plugin_dir / "_manifest.json")
    local_path = plugin_dir / "config.toml"
    local_path.write_text(
        '[plugin]\nenabled = true\n\n'
        '[qqcall]\nallow_planner_accept = true\n',
        encoding="utf-8",
    )
    local_contents = local_path.read_text(encoding="utf-8")
    plugin = qqcall_plugin.QQCallPlugin(str(plugin_dir))
    assert plugin.enable_plugin is False
    assert plugin.plugin_info.enabled is False
    assert plugin.get_config("plugin.enabled") is False
    assert [info.name for info, _ in plugin.get_plugin_components()] == ["qqcall_control"]
    assert local_path.read_text(encoding="utf-8") == local_contents


def test_plugin_roundtrips_all_core_toml_settings_and_does_not_generate_local_config(
    monkeypatch, tmp_path
):
    core = {
        "allow_planner_accept": True,
        "group_intent_model_task": "utils",
        "group_intent_timeout_seconds": 17,
    }
    _write_canonical_toml(monkeypatch, tmp_path, enabled=True, core=core)
    public_config = qqcall_service.read_qqcall_public_config()
    assert public_config["enabled"] is True
    assert isinstance(public_config["core"], dict)
    plugin_dir = tmp_path / "alternate-plugin-dir"
    plugin_dir.mkdir()
    shutil.copyfile(PROJECT_ROOT / "plugins" / "qqcall_plugin" / "_manifest.json", plugin_dir / "_manifest.json")

    plugin = qqcall_plugin.QQCallPlugin(str(plugin_dir))
    assert plugin.enable_plugin is True
    assert plugin.plugin_info.enabled is True
    assert plugin.config == {
        "plugin": {"enabled": True},
        "qqcall": {
            "allow_planner_accept": True,
            "group_intent_model_task": "utils",
            "group_intent_timeout_seconds": 17,
        },
    }
    assert [info.name for info, _ in plugin.get_plugin_components()] == [
        "qqcall_control",
        "qqcall_accept",
    ]
    assert not (plugin_dir / "config.toml").exists()


def test_status_command_requires_qq_private_sender_but_adapter_owns_admission(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return _status(peer_user_id=kwargs["peer_user_id"])

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_private_message())
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "来电已准备好" in reply
        assert "未确认是否接通" in reply
        assert calls == [("status", {"platform": "qq", "peer_user_id": PEER})]
        assert command.replies == [reply]
        assert "call-abc123" not in reply
        assert PEER not in reply

        for message in (
            _private_message(group=SimpleNamespace(group_id="g")),
            _private_message(sender=False),
            _private_message(platform="discord"),
            _private_message(stream_peer=OTHER_PEER),
        ):
            denied = _new_command(message)
            result = await denied.execute()
            if message.message_info.group_info is not None:
                assert result[0] and result[2] and "未能验证" in result[1]
            else:
                assert result == (True, None, True)
        assert len(calls) == 1

        # The adapter's allow decision remains authoritative for every peer.
        other_peer = _new_command(_private_message(peer=OTHER_PEER, stream_peer=OTHER_PEER))
        success, reply, intercept = await other_peer.execute()
        assert success and intercept and "QQCall 状态" in reply
        assert calls[-1] == ("status", {"platform": "qq", "peer_user_id": OTHER_PEER})

    _run(scenario())


@pytest.mark.parametrize(
    ("phase", "uncertain", "expected_label", "occupied"),
    [
        ("ending", False, "正在请求挂断", True),
        ("ending_unconfirmed", True, "挂断结果尚未确认", True),
        ("reject_posting", False, "正在请求拒接", True),
        ("reject_posted", False, "已请求拒接", True),
        ("reject_unconfirmed", True, "拒接结果尚未确认", True),
        ("future_phase", False, "暂时还看不清这通电话的状态", False),
    ],
)
def test_status_command_keeps_private_end_phases_occupied_and_preserves_unknown_fallback(
    monkeypatch, phase, uncertain, expected_label, occupied
):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return _status(phase=phase, uncertain=uncertain)

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_private_message())
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert expected_label in reply
        assert "通话已结束" not in reply
        assert "没有活动通话" not in reply
        if occupied:
            assert "还没确认通话结束" in reply or "通话位置仍被占用" in reply
        assert calls == [("status", {"platform": "qq", "peer_user_id": PEER})]
        assert command.replies == [reply]

    _run(scenario())


def test_group_status_uses_verified_live_requester_and_returns_only_read_only_diagnostics(monkeypatch):
    calls = []
    diagnostics = {
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

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return diagnostics

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_group_message("#stat_vc_qq"), operation="join")
        command.set_matched_groups({"operation": "join", "peer_user_id": OTHER_PEER, "group_id": "999999"})
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "通话位置：占用中" in reply
        assert "在线数：4" in reply and "约 28 秒后过期" in reply
        assert "邀请 3 人" in reply and "邀请不代表已加入" in reply
        assert "未验证" in reply
        assert calls == [(
            "group_status",
            {"platform": "qq", "peer_user_id": PEER, "group_id": "445566"},
        )]
        assert command.replies == [reply]
        assert PEER not in reply and "445566" not in reply and "998877" not in reply
        assert "room" not in reply.lower()

    _run(scenario())


def test_group_leave_uses_verified_group_requester_without_private_hangup_or_planner(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return {"left": True}

    async def forbidden_review(*_args, **_kwargs):
        raise AssertionError("explicit leave must not invoke Planner or LLM review")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", forbidden_review)

    async def scenario():
        assert re.fullmatch(qqcall_plugin.QQCallCommand.command_pattern, "#leave_vc_qq")
        command = _new_command(_group_message("#leave_vc_qq"), operation="leave")
        command.set_matched_groups({"operation": "leave", "peer_user_id": OTHER_PEER, "group_id": "999999"})
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert reply == "已经离开群语音啦，需要时再叫我喵~"
        assert calls == [(
            "leave_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "requester_user_id": PEER,
            },
        )]
        assert command.replies == [reply]

    _run(scenario())


@pytest.mark.parametrize(
    ("message_kind", "message_kwargs"),
    [
        ("private", {}),
        ("group", {"stream_group": "445567"}),
        ("group", {"recovered": True}),
        ("group", {"replayed": True}),
        ("group", {"segment_kind": "mixed"}),
    ],
    ids=["private-command", "group-route-mismatch", "recovered", "replayed", "mixed-segments"],
)
def test_group_leave_refuses_unverified_routes_before_adapter_call(monkeypatch, message_kind, message_kwargs):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unverified leave route reached the adapter API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message = (
            _private_message(peer=PEER, group=None)
            if message_kind == "private"
            else _group_message("#leave_vc_qq", **message_kwargs)
        )
        if message_kind == "private":
            message.processed_plain_text = "#leave_vc_qq"
        command = _new_command(message, operation="leave")
        command.set_matched_groups({"operation": "leave", "peer_user_id": PEER, "group_id": "445566"})
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert reply is not None and "未执行离开操作" in reply
        assert command.replies == [reply]

    _run(scenario())


@pytest.mark.parametrize(
    "message_kwargs",
    [
        pytest.param({"stream_peer": OTHER_PEER}, id="forged-requester"),
        pytest.param({"stream_group": "445567"}, id="forged-group"),
        pytest.param({"sender": False, "stream_peer": None}, id="senderless"),
        pytest.param({"recovered": True}, id="recovered"),
        pytest.param({"replayed": True}, id="replayed"),
        pytest.param({"segment_kind": "mixed"}, id="mixed-segments"),
    ],
)
def test_group_status_rejects_forged_or_replayed_route_before_adapter_call(monkeypatch, message_kwargs):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unverified group status reached the adapter API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message = _group_message("#stat_vc_qq", **message_kwargs)
        command = _new_command(message, operation=None)
        command.set_matched_groups({"operation": "status", "peer_user_id": PEER, "group_id": "445566"})
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "未能验证" in reply
        assert command.replies == [reply]

    _run(scenario())


def test_group_status_adapter_denial_is_silent_and_private_status_stays_private(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "group_status":
            raise QQCallAPIError("group_not_admitted")
        return _status(peer_user_id=kwargs["peer_user_id"])

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        denied = _new_command(_group_message("#stat_vc_qq"), operation=None)
        assert await denied.execute() == (True, None, True)
        assert denied.replies == []

        private_message = _private_message()
        private_message.processed_plain_text = "#stat_vc_qq"
        private_message.message_segment = Seg("text", "#stat_vc_qq")
        private = _new_command(private_message, operation="status")
        success, reply, intercept = await private.execute()
        assert success and intercept and "QQCall 状态" in reply
        assert [operation for operation, _kwargs in calls] == ["group_status", "status"]
        assert calls[1] == ("status", {"platform": "qq", "peer_user_id": PEER})

    _run(scenario())


def test_group_status_empty_diagnostics_are_explicitly_unverified(monkeypatch):
    async def request(operation, **kwargs):
        assert operation == "group_status"
        assert kwargs == {"platform": "qq", "peer_user_id": PEER, "group_id": "445566"}
        return {
            "occupied": False,
            "nativeJoinVerified": False,
            "roomObserved": False,
            "diagnosticOnly": True,
            "membership": None,
            "invitation": None,
            "roomEndObservation": None,
        }

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_group_message("#stat_vc_qq"), operation=None)
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "通话位置：空闲" in reply
        assert "群语音接入能力：未验证" in reply
        assert "当前群房间：未验证" in reply
        assert reply.count("还没有可靠记录") == 3
        assert "邀请不代表已加入" not in reply

    _run(scenario())


@pytest.mark.parametrize(
    ("trigger_text", "expected_kind"),
    [
        ("#join_vc_qq", "command"),
        ("#join_vc_qq", "command"),
        ("请加入群语音", "natural"),
        ("请加入群语音。", "natural"),
        ("加入当前群通话", "natural"),
        ("加入当前群通话！", "natural"),
        ("加入群语音", "natural"),
    ],
)
def test_group_join_trigger_sends_one_exact_live_intent_and_intercepts(monkeypatch, trigger_text, expected_kind):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("unsupported_unverified_group_join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    if expected_kind == "natural":
        monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", _approve_group_intent)
    assert qqcall_plugin.match_group_call_trigger(trigger_text) == expected_kind

    async def scenario():
        command = _new_command(_group_message(trigger_text), operation=None)
        command.set_matched_groups({"operation": "", "peer_user_id": OTHER_PEER, "group_id": "999999"})
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "尚未验证" in reply and "未执行加入操作" in reply
        assert calls == [(
            "join_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "trigger_text": trigger_text,
            },
        )]
        assert command.replies == [reply]
        assert PEER not in reply and "445566" not in reply and trigger_text not in reply

    _run(scenario())


def test_group_join_reports_only_validated_native_success(monkeypatch):
    calls = []
    result = {
        "version": 1,
        "accountId": "99887766",
        "serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001",
        "hostGeneration": 7,
        "connectionGeneration": 9,
        "joined": True,
        "scope": {
            "kind": "qqcall_group",
            "serviceInstance": "8cc43d5a-4dce-4e64-9d8f-713d65000001",
            "hostGeneration": 7,
            "connectionGeneration": 9,
            "groupId": "445566",
            "roomId": "room-abc",
            "roomGeneration": 4,
        },
        "source": "native_scene3_relation",
        "requesterUserId": PEER,
    }

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return result

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", _approve_group_intent)

    async def scenario():
        command = _new_command(_group_message("加入当前群通话"), operation=None)
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert reply == "来啦，已经加入群语音了喵~"
        assert calls == [(
            "join_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "trigger_text": "加入当前群通话",
            },
        )]
        assert PEER not in reply and "445566" not in reply

    _run(scenario())


@pytest.mark.parametrize(
    "error_code",
    [
        "call_occupied",
        "unsupported_unverified_group_join",
        "group_room_unverified",
        "group_identity_mismatch",
        "service_timeout",
        "group_room_query_timeout",
        "group_room_observed_empty",
        "group_room_query_quarantined",
        "group_room_query_busy",
        "group_room_query_post_failed",
        "group_room_metadata_invalid",
        "group_not_admitted",
        "group_admission_failed",
        "peer_not_admitted",
        "peer_admission_failed",
    ],
)
def test_group_join_typed_errors_are_safe_and_never_claim_success(monkeypatch, error_code):
    async def request(operation, **kwargs):
        assert operation == "join_group"
        raise QQCallAPIError(error_code)

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", _approve_group_intent)

    async def scenario():
        command = _new_command(_group_message("加入当前群通话"), operation=None)
        success, reply, intercept = await command.execute()
        assert success and intercept
        if error_code in {
            "group_not_admitted",
            "group_admission_failed",
            "peer_not_admitted",
            "peer_admission_failed",
        }:
            assert reply is None
            assert command.replies == []
            return
        assert "加入成功" not in reply and PEER not in reply and "445566" not in reply
        if error_code == "call_occupied":
            assert "已有通话" in reply
        elif error_code in {"unsupported_unverified_group_join", "group_room_unverified"}:
            assert "尚未验证" in reply and "未执行" in reply
        elif error_code == "group_not_admitted":
            assert "适配器设置还不允许" in reply

    _run(scenario())


def test_adapter_denial_after_private_ingress_silences_command(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("peer_not_admitted")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_private_message())
        assert await command.execute() == (True, None, True)
        assert calls == [("status", {"platform": "qq", "peer_user_id": PEER})]
        assert command.replies == []

    _run(scenario())


@pytest.mark.parametrize(
    "message_kwargs",
    [
        pytest.param({"stream_peer": OTHER_PEER}, id="stream-peer-mismatch"),
        pytest.param({"stream_group": "445567"}, id="stream-group-mismatch"),
        pytest.param({"sender": False, "stream_peer": None}, id="senderless"),
        pytest.param({"recovered": True}, id="recovered"),
        pytest.param({"replayed": True}, id="replayed"),
        pytest.param({"received_age": 180}, id="stale-180s"),
        pytest.param({"received_future": 10}, id="future-10s"),
        pytest.param({"system_event": True}, id="system-event"),
        pytest.param({"segment_kind": "reply"}, id="quoted-reply"),
        pytest.param({"segment_kind": "forward"}, id="quoted-forward"),
        pytest.param({"segment_kind": "mixed"}, id="mixed-segments"),
    ],
)
def test_group_join_rejects_forged_replayed_mismatched_or_quoted_provenance(monkeypatch, message_kwargs):
    # Build relative timestamps at test execution, not collection time, so the
    # exact freshness boundaries remain meaningful in a long full-suite run.
    message = _group_message("请加入群语音", **message_kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError("untrusted group trigger must not reach the adapter API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        command = _new_command(message, operation=None)
        success, reply, intercept = await command.execute()
        assert success and intercept
        assert "未执行加入操作" in reply

    _run(scenario())


def test_natural_group_phrase_on_non_qq_platform_is_not_a_qqcall_request(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("non-QQ group message must not reach QQCall")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        message = _group_message("请加入群语音", platform="discord")
        message.is_command = True
        command = _new_command(message, operation=None)
        assert await command.execute() == (True, None, False)
        assert message.is_command is False

    _run(scenario())


def test_private_natural_phrase_is_not_a_group_join_and_explicit_join_is_group_only(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("private chat cannot request a group join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        natural = _new_command(_private_message(), operation=None)
        natural.message.processed_plain_text = "请加入群语音"
        natural.message.is_command = True
        assert await natural.execute() == (True, None, False)
        assert natural.message.is_command is False

        explicit = _new_command(_private_message(), operation="join")
        explicit.message.processed_plain_text = "#join_vc_qq"
        success, reply, intercept = await explicit.execute()
        assert success and intercept
        assert "仅适用于当前 QQ 群" in reply and "未执行加入操作" in reply

    _run(scenario())


@pytest.mark.parametrize("phrase", ["请加入群语音", "加入群语音", "加入当前群通话"])
def test_fixed_join_phrases_are_not_registered_commands(phrase):
    for text in (phrase, f" {phrase}！ ", f"[语音：{phrase}。]"):
        assert re.fullmatch(qqcall_plugin.QQCallCommand.command_pattern, text) is None
    for text in ("#join_vc_qq", "#leave_vc_qq", "#stat_vc_qq", "[语音：#join_vc_qq]"):
        assert re.fullmatch(qqcall_plugin.QQCallCommand.command_pattern, text) is not None


def test_fixed_join_phrase_from_core_asr_is_ordinary_text_not_a_command(monkeypatch):
    calls = []

    class FakeMultimodalRouter:
        async def transcribe(self, voice_base64, **kwargs):
            assert voice_base64 == "fixture-audio"
            return SimpleNamespace(text="请加入群语音。", degraded=False)

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("call_occupied")

    monkeypatch.setattr(global_config.voice, "enable_asr", True)
    monkeypatch.setattr("src.multimodal.get_multimodal_router", lambda: FakeMultimodalRouter())
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", _approve_group_intent)

    async def scenario():
        now = time.time()
        message_info = BaseMessageInfo(
            platform="qq",
            message_id="voice-1",
            time=now,
            user_info=UserInfo(platform="qq", user_id=PEER),
            group_info=GroupInfo(platform="qq", group_id="445566"),
            additional_config={"offline_recovery": _live_recovery_metadata()},
        )
        message = MessageRecv(
            {
                "message_info": message_info.to_dict(),
                "message_segment": Seg("voice", "fixture-audio").to_dict(),
                "raw_message": "[voice]",
                "processed_plain_text": "",
            }
        )
        message.update_chat_stream(
            SimpleNamespace(
                stream_id="group-stream",
                platform="qq",
                user_info=UserInfo(platform="qq", user_id=PEER),
                group_info=GroupInfo(platform="qq", group_id="445566"),
            )
        )
        await message.process()
        assert message.processed_plain_text == "[语音：请加入群语音。]"
        assert re.fullmatch(
            qqcall_plugin.QQCallCommand.command_pattern,
            message.processed_plain_text,
            flags=re.IGNORECASE | re.DOTALL,
        ) is None
        assert calls == []

    _run(scenario())


def test_natural_group_join_requires_llm_approval_then_uses_original_route_and_text(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("unsupported_unverified_group_join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(global_config.bot, "nickname", "Nacho", raising=False)
    monkeypatch.setattr(global_config.bot, "alias_names", ["小纳", "NachoBot"], raising=False)
    llm_calls, selected_config = _stub_intent_model(monkeypatch)

    async def scenario():
        message = _group_message("请加入群语音。")
        message.is_command = True  # Core marks command-pattern matches before execute().
        command = _new_command(message, config=_intent_plugin_config())
        success, reply, intercept = await command.execute()
        assert success and intercept and "尚未验证" in reply
        assert calls == [(
            "join_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "trigger_text": "请加入群语音。",
            },
        )]
        assert len(llm_calls) == 1
        prompt, model_config, options = llm_calls[0]
        assert model_config is selected_config
        assert options == {
            "request_type": "qqcall.group_join_intent",
            "temperature": 0.0,
            "max_tokens": None,
        }
        assert "BOT_IDENTITIES_JSON=[\"Nacho\", \"小纳\", \"NachoBot\"]" in prompt
        assert 'CURRENT_CANDIDATE_JSON="请加入群语音。"' in prompt

    _run(scenario())


def test_natural_group_join_valid_negative_semantics_return_to_chat(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("semantic denial must not dispatch a group join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    llm_calls, _ = _stub_intent_model(monkeypatch, response='{"join":false}')

    async def scenario():
        message = _group_message("加入当前群通话")
        message.is_command = True
        command = _new_command(message, config=_intent_plugin_config())
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []
        assert len(llm_calls) == 1

    _run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        '{"join":true,"join":false}',
        '{"join":true,"scope":{}}',
        '{"join":"true"}',
        '{"join":0}',
        'prefix {"join":true}',
        '[{"join":true}]',
    ],
)
def test_natural_group_join_malformed_review_is_unavailable_and_consumed(monkeypatch, response):
    async def forbidden(*args, **kwargs):
        raise AssertionError("malformed review must not dispatch a group join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    _stub_intent_model(monkeypatch, response=response)
    error_logs = []
    monkeypatch.setattr(
        qqcall_plugin,
        "logger",
        SimpleNamespace(warning=lambda event, **fields: error_logs.append((event, fields))),
    )

    async def scenario():
        message = _group_message("加入当前群通话")
        message.is_command = True
        command = _new_command(message, config=_intent_plugin_config())
        result = await command.execute()
        assert result == (True, qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY, True)
        assert command.replies == [qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY]
        assert error_logs == [
            (
                "qqcall_group_intent_review_unavailable",
                {"error_code": "malformed_response"},
            )
        ]

    _run(scenario())


@pytest.mark.parametrize(
    ("failure", "expected_error_code"),
    [
        ("missing_utils", "model_task_unavailable"),
        ("api_error", "request_error"),
        ("api_false", "model_request_failed"),
        ("transport_error", "transport_error"),
        ("empty_response", "empty_response"),
        ("timeout", "timeout"),
        ("bad_timeout", "invalid_config"),
    ],
)
def test_natural_group_join_review_failures_consume_candidate_safely(monkeypatch, failure, expected_error_code):
    async def forbidden(*args, **kwargs):
        raise AssertionError("failed review must not dispatch a group join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    error_logs = []
    monkeypatch.setattr(
        qqcall_plugin,
        "logger",
        SimpleNamespace(warning=lambda event, **fields: error_logs.append((event, fields))),
    )
    config = _intent_plugin_config()
    if failure == "missing_utils":
        _stub_intent_model(monkeypatch, models={"utils_small": object()})
        monkeypatch.setattr(
            qqcall_plugin.llm_api,
            "generate_with_model",
            forbidden,
        )
    elif failure == "api_error":
        _stub_intent_model(monkeypatch, error=RuntimeError("provider failure"))
    elif failure == "api_false":
        _stub_intent_model(monkeypatch, success=False)
    elif failure == "transport_error":
        _stub_intent_model(monkeypatch, success=False, response="连接异常，请检查网络连接状态或URL是否正确")
    elif failure == "empty_response":
        _stub_intent_model(monkeypatch, success=False, response="响应内容为空，这可能是一个临时性问题")
    elif failure == "timeout":
        async def slow_review():
            await asyncio.sleep(2)

        _stub_intent_model(monkeypatch, wait=slow_review)
        config["qqcall"]["group_intent_timeout_seconds"] = 1
    else:
        _stub_intent_model(monkeypatch)
        config["qqcall"]["group_intent_timeout_seconds"] = 31

    async def scenario():
        message = _group_message("加入群语音")
        message.is_command = True
        command = _new_command(message, config=config)
        assert await command.execute() == (True, qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY, True)
        assert command.replies == [qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY]
        assert error_logs == [
            (
                "qqcall_group_intent_review_unavailable",
                {"error_code": expected_error_code},
            )
        ]

    _run(scenario())


@pytest.mark.parametrize(
    "mutation",
    ["sender", "group", "text", "freshness", "segment"],
)
@pytest.mark.parametrize("review_outcome", ["approved", "unavailable"])
def test_review_rechecks_exact_live_route_text_and_provenance(monkeypatch, mutation, review_outcome):
    async def forbidden(*args, **kwargs):
        raise AssertionError("a changed route or candidate must not dispatch")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", lambda *args, **kwargs: [])
    monkeypatch.setattr(qqcall_plugin.llm_api, "get_available_models", lambda: {"utils": object()})
    message = _group_message("请加入群语音")

    def mutate_candidate():
        if mutation == "sender":
            message.message_info.user_info.user_id = OTHER_PEER
        elif mutation == "group":
            message.message_info.group_info.group_id = "445567"
        elif mutation == "text":
            message.processed_plain_text = "加入群语音"
        elif mutation == "freshness":
            message.message_info.additional_config["offline_recovery"]["received_at"] = time.time() - 180
        else:
            message.message_segment = Seg("reply", [])

    async def generate(*args, **kwargs):
        mutate_candidate()
        if review_outcome == "approved":
            return True, '{"join":true}', "", "test-model"
        return False, "连接异常，请检查网络连接状态或URL是否正确", "", ""

    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", generate)

    async def scenario():
        message.is_command = True
        command = _new_command(message, config=_intent_plugin_config())
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []

    _run(scenario())


def test_explicit_group_join_command_stays_direct_without_model_review(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("unsupported_unverified_group_join")

    async def forbidden(*args, **kwargs):
        raise AssertionError("explicit #qqcall command must not invoke semantic review")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        command = _new_command(_group_message("#join_vc_qq"), operation="join")
        success, reply, intercept = await command.execute()
        assert success and intercept and "尚未验证" in reply
        assert calls == [(
            "join_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "trigger_text": "#join_vc_qq",
            },
        )]

    _run(scenario())


def test_natural_group_text_still_requires_review_when_matched_operation_says_join(monkeypatch):
    review_calls = []
    request_calls = []

    async def review(*args, **kwargs):
        review_calls.append((args, kwargs))
        return qqcall_plugin._GroupIntentReviewResult(qqcall_plugin._GroupIntentReviewOutcome.APPROVED)

    async def request(operation, **kwargs):
        request_calls.append((operation, kwargs))
        raise QQCallAPIError("unsupported_unverified_group_join")

    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", review)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_group_message("加入当前群通话"), operation="join")
        success, reply, intercept = await command.execute()
        assert success and intercept and "尚未验证" in reply
        assert len(review_calls) == 1
        assert request_calls == [(
            "join_group",
            {
                "platform": "qq",
                "peer_user_id": PEER,
                "group_id": "445566",
                "trigger_text": "加入当前群通话",
            },
        )]

    _run(scenario())


def test_private_natural_text_is_not_explicit_join_when_matched_operation_says_join(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("private natural text must not reach QQCall or model APIs")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        message = _private_message()
        message.processed_plain_text = "请加入群语音"
        message.is_command = True
        command = _new_command(message, operation="join")
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []

    _run(scenario())


@pytest.mark.parametrize("canonical_state", ["missing", "malformed"])
def test_plugin_disables_when_canonical_toml_is_unavailable_or_invalid(
    monkeypatch, tmp_path, canonical_state
):
    service_root, canonical_path = _write_canonical_toml(monkeypatch, tmp_path, enabled=True)
    if canonical_state == "missing":
        canonical_path.unlink()
    else:
        canonical_path.write_text('[plugin]\nenabled = true\n', encoding="utf-8")
    (service_root / "config.json").write_text('{"schema": 2, "enabled": true}', encoding="utf-8")

    plugin_dir = tmp_path / "alternate-plugin-dir"
    plugin_dir.mkdir()
    shutil.copyfile(PROJECT_ROOT / "plugins" / "qqcall_plugin" / "_manifest.json", plugin_dir / "_manifest.json")
    (plugin_dir / "config.toml").write_text('[plugin]\nenabled = true\n', encoding="utf-8")
    plugin = qqcall_plugin.QQCallPlugin(plugin_dir=str(plugin_dir))
    assert plugin.enable_plugin is False
    assert plugin.plugin_info.enabled is False


def test_canonical_switch_read_fails_closed_on_missing_invalid_or_error(monkeypatch):
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: None)
    assert qqcall_plugin._core_qqcall_enabled() is False
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: 1)
    assert qqcall_plugin._core_qqcall_enabled() is False

    def unavailable():
        raise OSError("unavailable")

    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", unavailable)
    assert qqcall_plugin._core_qqcall_enabled() is False


def test_disabled_natural_group_candidate_skips_llm_and_returns_to_conversation(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled natural candidate must not call the model")

    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: False)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message = _group_message("请加入群语音")
        message.is_command = True
        command = _new_command(message, operation=None)
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []

    _run(scenario())


@pytest.mark.parametrize(
    "message_kwargs",
    [
        pytest.param({"received_age": 180}, id="stale"),
        pytest.param({"sender": False, "stream_peer": None}, id="senderless"),
        pytest.param({"replayed": True}, id="replayed"),
        pytest.param({"segment_kind": "mixed"}, id="mixed-segments"),
    ],
)
@pytest.mark.parametrize("switch_state", ["disabled", "unavailable"])
def test_disabled_or_unavailable_switch_keeps_invalid_natural_text_ordinary(
    monkeypatch, message_kwargs, switch_state
):
    if switch_state == "disabled":
        monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: False)
    else:
        def unavailable():
            raise OSError("canonical config unavailable")

        monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", unavailable)

    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled natural candidate must not call model or adapter")

    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message = _group_message("请加入群语音", **message_kwargs)
        message.is_command = True
        command = _new_command(message, operation="")
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []

    _run(scenario())


def test_disabled_explicit_commands_reply_only_after_actual_route_verification(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled command must not dispatch")

    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: False)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", forbidden)

    async def scenario():
        group = _new_command(_group_message("#join_vc_qq"), operation="join")
        success, reply, intercept = await group.execute()
        assert success and intercept and reply == "通话功能还没开启喵~"
        assert group.replies == [reply]

        private = _new_command(_private_message(), operation="status")
        success, reply, intercept = await private.execute()
        assert success and intercept and reply == "通话功能还没开启喵~"
        assert private.replies == [reply]

        invalid_private = _new_command(_private_message(stream_peer=OTHER_PEER), operation="status")
        assert await invalid_private.execute() == (True, None, True)
        assert invalid_private.replies == []

    _run(scenario())


@pytest.mark.parametrize("review_success", [True, False])
def test_switch_turning_off_during_natural_review_prevents_group_join(monkeypatch, review_success):
    # Candidate gate, pre-review gate, then the switch flips during the await.
    enabled_values = iter([True, True, False])
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: next(enabled_values))
    response = '{"join":true}' if review_success else "连接异常，请检查网络连接状态或URL是否正确"
    llm_calls, _ = _stub_intent_model(monkeypatch, response=response, success=review_success)

    async def forbidden(*args, **kwargs):
        raise AssertionError("central shutdown during review must prevent join")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message = _group_message("请加入群语音")
        message.is_command = True
        command = _new_command(message, operation=None)
        assert await command.execute() == (True, None, False)
        assert message.is_command is False
        assert command.replies == []
        assert len(llm_calls) == 1

    _run(scenario())


def test_switch_turning_off_during_private_status_query_reports_disabled(monkeypatch):
    enabled = {"value": True}
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: enabled["value"])
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        assert operation == "status"
        enabled["value"] = False
        return _status()

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_private_message(), operation="status")
        success, reply, intercept = await command.execute()
        assert success and intercept and reply == "通话功能还没开启喵~"
        assert calls == [("status", {"platform": "qq", "peer_user_id": PEER})]
        assert command.replies == [reply]

    _run(scenario())


def test_private_status_preflight_peer_denial_stays_silent_if_switch_turns_off(monkeypatch):
    enabled = {"value": True}
    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: enabled["value"])
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        assert operation == "status"
        enabled["value"] = False
        raise QQCallAPIError("peer_not_admitted")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        command = _new_command(_private_message(), operation="status")
        assert await command.execute() == (True, None, True)
        assert calls == [("status", {"platform": "qq", "peer_user_id": PEER})]
        assert command.replies == []

    _run(scenario())


def test_planner_accept_requires_canonical_switch_after_event_validation(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled Planner action must not dispatch")

    monkeypatch.setattr(qqcall_plugin, "is_qqcall_enabled", lambda: False)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message, stream = _invite_message()
        action = _new_action(message, stream, config=_plugin_config(allow_planner=True))
        success, result = await action.execute()
        assert not success and result == "通话功能还没开启喵~"

    _run(scenario())


def test_group_intent_context_is_current_bounded_and_uses_code_assigned_roles(monkeypatch):
    now = time.time()
    monkeypatch.setattr(global_config.bot, "qq_account", "999000", raising=False)
    message = _group_message("加入群语音")
    captured = []

    def row(*, user_id, nickname, text, stream_id="group-stream", group_id="445566", message_id="old", timestamp=None, metadata=None):
        user = None if user_id is None else SimpleNamespace(
            platform="qq", user_id=user_id, user_nickname=nickname, user_cardname=None
        )
        return SimpleNamespace(
            chat_id=stream_id,
            message_id=message_id,
            time=now - 10 if timestamp is None else timestamp,
            user_info=user,
            group_info=SimpleNamespace(group_id=group_id, group_platform="qq"),
            chat_info=SimpleNamespace(stream_id=stream_id),
            processed_plain_text=text,
            display_message=text,
            additional_config=metadata,
            is_command=False,
        )

    rows = [
        row(user_id=None, nickname="", text="senderless event"),
        row(
            user_id=PEER,
            nickname="replayed",
            text="old event",
            metadata={"offline_recovery": _live_recovery_metadata(replayed=True)},
        ),
        row(user_id=PEER, nickname="other stream", text="other", stream_id="another-group"),
        row(user_id=PEER, nickname="wrong group", text="wrong", group_id="445567"),
        row(user_id=PEER, nickname="current", text="candidate", message_id="message-1"),
        row(user_id="999000", nickname="spoofed name", text="x" * 1024),
        row(user_id=PEER, nickname="claimed bot", text="x" * 1024),
        row(user_id=OTHER_PEER, nickname="member", text="x" * 1024),
    ]

    def fetch(chat_id, start_time, end_time, **kwargs):
        captured.append((chat_id, start_time, end_time, kwargs))
        return rows

    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", fetch)
    route = (PEER, "445566")
    rendered = qqcall_plugin._bounded_group_intent_context(message, route, now)
    entries = json.loads(rendered)
    assert len(rendered) <= 4096
    assert [entry["role"] for entry in entries] == ["bot", "requester", "member"]
    assert [entry["display_name"] for entry in entries] == ["spoofed name", "claimed bot", "member"]
    assert "999000" not in rendered and PEER not in rendered and OTHER_PEER not in rendered
    assert "senderless event" not in rendered and "old event" not in rendered
    assert "other" not in rendered and "wrong" not in rendered and "candidate" not in rendered
    assert captured[0][0] == "group-stream"
    assert captured[0][2] - captured[0][1] == 300
    assert captured[0][3] == {"limit": 8, "limit_mode": "latest"}


def test_group_intent_context_enforces_rendered_character_cap(monkeypatch):
    now = time.time()
    message = _group_message("加入群语音")
    rows = [
        SimpleNamespace(
            chat_id="group-stream",
            message_id=f"history-{index}",
            time=now - index - 1,
            user_info=SimpleNamespace(
                platform="qq", user_id=str(700000 + index), user_nickname="member", user_cardname=None
            ),
            group_info=SimpleNamespace(group_id="445566", group_platform="qq"),
            chat_info=SimpleNamespace(stream_id="group-stream"),
            processed_plain_text="z" * 1024,
            additional_config=None,
            is_command=False,
        )
        for index in range(8)
    ]
    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", lambda *args, **kwargs: rows)
    rendered = qqcall_plugin._bounded_group_intent_context(message, (PEER, "445566"), now)
    assert len(rendered) <= 4096
    assert len(json.loads(rendered)) < 8


def test_cancelled_group_intent_review_releases_capacity(monkeypatch):
    request_calls = []
    started = None
    keep_waiting = True

    async def request(operation, **kwargs):
        request_calls.append((operation, kwargs))
        raise QQCallAPIError("call_occupied")

    async def generate(*args, **kwargs):
        if keep_waiting:
            started.set()
            await asyncio.Event().wait()
        return True, '{"join":true}', "", "test-model"

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", lambda *args, **kwargs: [])
    monkeypatch.setattr(qqcall_plugin.llm_api, "get_available_models", lambda: {"utils": object()})
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", generate)

    async def scenario():
        nonlocal started, keep_waiting
        started = asyncio.Event()
        message = _group_message("请加入群语音")
        command = _new_command(message, config=_intent_plugin_config())
        task = asyncio.create_task(command.execute())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert command.replies == []
        keep_waiting = False
        next_command = _new_command(_group_message("请加入群语音"), config=_intent_plugin_config())
        success, reply, intercept = await next_command.execute()
        assert success and intercept and "已有通话" in reply
        assert len(request_calls) == 1

    _run(scenario())


def test_review_capacity_is_two_across_independent_event_loops(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    request_calls = []

    async def request(operation, **kwargs):
        request_calls.append((operation, kwargs))
        raise QQCallAPIError("call_occupied")

    async def generate(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
        await asyncio.to_thread(release.wait, 5)
        with lock:
            active -= 1
        return True, '{"join":true}', "", "test-model"

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    monkeypatch.setattr(qqcall_plugin.message_api, "get_messages_by_time_in_chat", lambda *args, **kwargs: [])
    monkeypatch.setattr(qqcall_plugin.llm_api, "get_available_models", lambda: {"utils": object()})
    monkeypatch.setattr(qqcall_plugin.llm_api, "generate_with_model", generate)
    outcomes = []

    def run_in_own_loop():
        outcomes.append(_run(_new_command(
            _group_message("请加入群语音"),
            config=_intent_plugin_config(),
            operation=None,
        ).execute()))

    first = threading.Thread(target=run_in_own_loop)
    second = threading.Thread(target=run_in_own_loop)
    first.start()
    second.start()
    assert started.wait(5)
    overloaded = _new_command(_group_message("请加入群语音"), config=_intent_plugin_config(), operation=None)
    assert _run(overloaded.execute()) == (True, qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY, True)
    assert overloaded.replies == [qqcall_plugin._GROUP_INTENT_UNAVAILABLE_REPLY]
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert peak == 2
    assert len(outcomes) == 2
    assert len(request_calls) == 2








def test_planner_action_uses_bound_event_scope_without_llm_scope_parameters(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return _mutation(kwargs["scope"])

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        message, stream = _invite_message()
        action = _new_action(
            message,
            stream,
            action_data={"target_message_id": "event-message", "loop_start_time": 8.5},
        )
        success, result = await action.execute()
        assert success
        assert "已提交" in result and "不代表通话已经接通" in result
        assert calls == [("accept", {"platform": "qq", "peer_user_id": PEER, "scope": SCOPE})]

    _run(scenario())


@pytest.mark.parametrize(
    "action_data",
    [
        {"scope": {"callId": "call-abc123"}},
        {"scope": dict(SCOPE, callGeneration=SCOPE["callGeneration"] + 1)},
        {"callId": "call-abc123"},
        {"serviceInstance": SCOPE["serviceInstance"], "hostGeneration": SCOPE["hostGeneration"]},
    ],
)
def test_planner_scope_inputs_must_be_complete_and_match_the_event(monkeypatch, action_data):
    async def forbidden(*args, **kwargs):
        raise AssertionError("mismatched Planner scope must not reach the API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message, stream = _invite_message()
        action = _new_action(message, stream, action_data=action_data)
        success, text = await action.execute()
        assert not success
        assert "范围" in text

    _run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"peer": OTHER_PEER},
        {"stream_peer": OTHER_PEER},
        {"group": SimpleNamespace(group_id="g", group_name="group")},
        {"sender": True},
        {"event_type": "qq.call.call_connected"},
    ],
)
def test_planner_action_requires_matching_senderless_private_invite_route(monkeypatch, kwargs):
    async def forbidden(*args, **kw):
        raise AssertionError("invalid event principal must not reach the API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message, stream = _invite_message(**kwargs)
        action = _new_action(message, stream)
        success, _ = await action.execute()
        assert not success

    _run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"uncertain": True},
        {"invite_callback_ready": False},
        {"invite_correlation": "single_active_invite_uncorrelated"},
        {"accept_state": "posted"},
        {"phase": "connected"},
    ],
)
def test_planner_action_requires_event_callback_correlation_and_certain_state(monkeypatch, changes):
    async def forbidden(*args, **kw):
        raise AssertionError("unqualified event must not reach the API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        data = {
            "call_id": SCOPE["callId"],
            "call_generation": SCOPE["callGeneration"],
            "phase": "invite_ready",
            "uncertain": False,
            "invite_callback_ready": True,
            "invite_correlation": "kernel_from_uid_matched",
            "accept_state": "not_requested",
        }
        data.update(changes)
        message, stream = _invite_message(event_data=data)
        action = _new_action(message, stream)
        success, _ = await action.execute()
        assert not success

    _run(scenario())


def test_planner_action_rejects_bool_generation_even_when_scope_generation_is_one(monkeypatch):
    async def forbidden(*args, **kw):
        raise AssertionError("bool call generation must not reach the API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        scope = dict(SCOPE, callGeneration=1)
        data = {
            "call_id": scope["callId"],
            "call_generation": True,
            "phase": "invite_ready",
            "uncertain": False,
            "invite_callback_ready": True,
            "invite_correlation": "kernel_from_uid_matched",
            "accept_state": "not_requested",
        }
        message, stream = _invite_message(scope=scope, event_data=data)
        action = _new_action(message, stream)
        success, _ = await action.execute()
        assert not success

    _run(scenario())


def test_planner_action_default_off_and_adapter_owns_peer_admission(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return _mutation(kwargs["scope"])

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        message, stream = _invite_message()
        disabled = _new_action(message, stream, config=_plugin_config(allow_planner=False))
        assert not (await disabled.execute())[0]
        admitted_message, admitted_stream = _invite_message(peer=OTHER_PEER, stream_peer=OTHER_PEER)
        adapter_admitted = _new_action(admitted_message, admitted_stream)
        assert (await adapter_admitted.execute())[0]
        assert calls == [(
            "accept",
            {"platform": "qq", "peer_user_id": OTHER_PEER, "scope": SCOPE},
        )]

    _run(scenario())


def test_planner_action_adapter_admission_denial_stops_action(monkeypatch):
    calls = []

    async def request(operation, **kwargs):
        calls.append((operation, kwargs))
        raise QQCallAPIError("peer_not_admitted")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)

    async def scenario():
        message, stream = _invite_message(peer=OTHER_PEER, stream_peer=OTHER_PEER)
        action = _new_action(message, stream)
        assert await action.execute() == (False, "")
        assert calls == [(
            "accept",
            {"platform": "qq", "peer_user_id": OTHER_PEER, "scope": SCOPE},
        )]

    _run(scenario())


def test_planner_action_rejects_missing_or_invalid_scope_metadata(monkeypatch):
    async def forbidden(*args, **kw):
        raise AssertionError("invalid event scope must not reach the API")

    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)

    async def scenario():
        message, stream = _invite_message()
        message.additional_config.pop("qqcall_scope", None)
        action = _new_action(message, stream)
        assert not (await action.execute())[0]

        message, stream = _invite_message()
        message.additional_config["qqcall_scope"] = {"callId": "partial"}
        message.message_info.additional_config["qqcall_scope"] = {"callId": "partial"}
        action = _new_action(message, stream)
        assert not (await action.execute())[0]

    _run(scenario())


@pytest.mark.parametrize("operation", ["accept", "hangup", "reject"])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("metadata", [{"operation": "status"}, {"private_operation": "accept"}, {"operation": "reject"}])
def test_removed_private_commands_are_unregistered_and_cannot_dispatch(monkeypatch, operation, group, metadata):
    async def forbidden(*args, **kwargs):
        raise AssertionError("removed command must not invoke a call operation or intent review")
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", forbidden)
    monkeypatch.setattr(qqcall_plugin, "_review_group_join_intent", forbidden)
    for text in (f"#qqcall {operation}", f"  #QQCALL {operation.upper()}  "):
        assert re.fullmatch(qqcall_plugin.QQCallCommand.command_pattern, text) is None
        message = _group_message(text) if group else _private_message()
        message.processed_plain_text = text
        command = _new_command(message)
        command.set_matched_groups(metadata)
        success, reply, _ = _run(command.execute())
        assert success and reply is None
        assert command.replies == []


def test_command_error_replies_keep_persona_and_do_not_echo_unknown_error_data():
    for code in ("group_join_failed", "group_leave_unconfirmed", "call_occupied", "call_missing_tts_universal"):
        reply = qqcall_plugin._error_reply(QQCallAPIError(code))
        assert "喵" in reply
        assert not reply.startswith(("来啦", "已经离开"))
    reply = qqcall_plugin._error_reply(QQCallAPIError("unexpected_private_detail"))
    assert "unexpected_private_detail" not in reply
    assert "#stat_vc_qq" in reply


@pytest.mark.parametrize("phase", ["connected", "terminal"])
def test_private_status_does_not_describe_connected_or_ended_call_as_waiting_to_accept(monkeypatch, phase):
    async def request(operation, **kwargs):
        assert operation == "status"
        return _status(phase=phase, inviteCallbackReady=False)
    monkeypatch.setattr(qqcall_plugin, "request_qqcall", request)
    command = _new_command(_private_message())
    success, reply, intercept = _run(command.execute())
    assert success and intercept and "喵" in reply
    assert "还在等 QQ 准备好接听" not in reply
    assert "未确认是否接通" not in reply
    assert ("QQ 报告通话已接通" if phase == "connected" else "通话已结束") in reply
