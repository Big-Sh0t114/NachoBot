from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from test_qqcall_group_wire import ACCOUNT, GROUP, PEER, _CoreLoopback, _runtime
from qqcall_client import qqcall_dependencies as deps

CORE_HEALTH = {"capabilities": {"tts": True}, "observed_local": {
    "tts": {"required": True, "ready": True}, "perception": {"required": True, "ready": True},
}}
UVC_HEALTH = {"protocol_version": 1, "service_instance_id": "synthetic-instance", "ready": True, "capabilities": {
    key: {"available": True} for key in ("pipeline", "vad", "speaker")
}}


@pytest.mark.parametrize("case,expected", [
    ("ready", ()), ("tts", ("tts",)), ("both", ("tts", "universal")),
    ("perception", ("perception",)), ("lite", ()), ("potato", ("tts",)),
    ("core_down", ("core",)), ("bad_protocol", ("universal",)), ("speaker", ("universal",)), ("warmup", ("tts",)),
])
def test_probes_real_contracts_and_collects_all_missing_links(monkeypatch, case, expected):
    core, uvc = copy.deepcopy(CORE_HEALTH), copy.deepcopy(UVC_HEALTH)
    if case in {"tts", "both"}:
        core["observed_local"]["tts"]["ready"] = False
    if case == "both":
        uvc["ready"] = False
    if case == "perception":
        core["observed_local"]["perception"]["ready"] = False
    if case in {"lite", "potato"}:
        core["observed_local"]["perception"]["required"] = False
    if case == "potato":
        core["capabilities"]["tts"] = False
    if case == "bad_protocol":
        uvc["protocol_version"] = True
    if case == "speaker":
        uvc["capabilities"]["speaker"]["available"] = False
    if case == "warmup":
        core["observed_local"]["tts"]["observed"] = {"model_loaded": True, "synthesis_ready": False}
    observed = []

    async def get(url, token):
        observed.append((url, token))
        if "/api/multimodal/health" in url:
            if case == "core_down":
                raise OSError("synthetic failure")
            return core
        return uvc

    monkeypatch.setattr(deps, "_health_json", get)
    router = SimpleNamespace(config=SimpleNamespace(route_config={"qq": SimpleNamespace(url="ws://127.0.0.1:8123/ws", token="synthetic-secret")}))
    check = deps.make_dependency_check(router, "qq", {"url": "http://127.0.0.1:8016", "token_file": ""})
    assert asyncio.run(check()) == expected
    assert ("http://127.0.0.1:8123/api/multimodal/health", "synthetic-secret") in observed
    assert len(observed) == 2


@pytest.mark.parametrize("instance,valid", [
    (None, False), ("", False), (True, False), (1, False), ({}, False),
    ("x" * 129, False), ("x" * 128, True), ("synthetic-instance", True),
])
def test_universal_instance_contract_is_required_even_when_health_claims_ready(monkeypatch, instance, valid):
    core, uvc = copy.deepcopy(CORE_HEALTH), copy.deepcopy(UVC_HEALTH)
    core["observed_local"]["tts"]["ready"] = False
    if instance is None:
        uvc.pop("service_instance_id")
    else:
        uvc["service_instance_id"] = instance

    async def get(url, token):
        return core if "/api/multimodal/health" in url else uvc

    monkeypatch.setattr(deps, "_health_json", get)
    router = SimpleNamespace(config=SimpleNamespace(route_config={
        "qq": SimpleNamespace(url="ws://127.0.0.1:8123/ws", token="synthetic-secret"),
    }))
    check = deps.make_dependency_check(router, "qq", {"url": "http://127.0.0.1:8016", "token_file": ""})
    assert asyncio.run(check()) == (("tts",) if valid else ("tts", "universal"))


@pytest.mark.parametrize("missing", [("tts",), ("universal",), ("tts", "universal"), ("perception", "tts", "universal")])
def test_group_refusal_occurs_before_native_room_query_and_lists_missing_services(missing):
    async def scenario():
        calls = []
        core = _CoreLoopback()

        async def transport(path, *_args):
            calls.append(path)
            raise AssertionError("dependencies must be ready before native room queries")

        runtime = _runtime(core, transport=transport)
        runtime._dependency_check = lambda: missing
        identity = await runtime._read_platform_state()
        response = await runtime._process_group_join_request({
            "request_id": "synthetic_dependency_request", "peer_user_id": PEER,
            "group_id": GROUP, "trigger_text": "#join_vc_qq",
        }, identity)
        code = response["error"]["code"]
        assert calls == []
        notice = deps.dependency_notice(code)
        assert notice and all(deps.DEPENDENCY_NAMES[name] in notice for name in missing)
    asyncio.run(scenario())


def test_no_probe_configuration_fails_closed_and_invalid_labels_cannot_reach_notice():
    async def scenario():
        runtime = _runtime(_CoreLoopback())
        runtime._dependency_check = None
        assert await runtime._dependency_failure() == "call_missing_core_tts_universal"
        runtime._dependency_check = lambda: ("untrusted_exception_text",)
        assert await runtime._dependency_failure() == "call_missing_core_tts_universal"
    asyncio.run(scenario())
    assert deps.dependency_notice("call_missing_untrusted_exception_text") is None
    assert deps.dependency_notice("call_missing_tts_tts") is None


def test_core_offline_group_refusal_reports_all_links_and_keeps_notice_acl():
    async def scenario():
        native, notices = [], []

        async def transport(path, *_args):
            native.append(path)
            raise AssertionError("offline Core must never permit a native room operation")

        runtime = _runtime(_CoreLoopback(), transport=transport)
        runtime._dependency_check = lambda: ("tts", "universal")
        identity = await runtime._read_platform_state()
        identity["core_ready"] = False

        async def notify(peer, group, text, preflight):
            assert await preflight()
            notices.append(text)

        runtime._send_call_notice = notify
        result = await runtime._process_group_join_request({
            "request_id": "offline_core_group_request", "peer_user_id": PEER,
            "group_id": GROUP, "trigger_text": "#join_vc_qq",
        }, identity)
        assert result["error"]["code"] == "call_missing_core_tts_universal"
        await runtime._notify_dependency_refusal(PEER, GROUP, result["error"]["code"], identity)
        assert len(notices) == 1 and all(label in notices[0] for label in ("Core", "TTS", "UniversalVC"))
        runtime._admit_group = lambda *_args: False
        await runtime._notify_dependency_refusal(PEER, GROUP, result["error"]["code"], identity)
        assert len(notices) == 1 and native == []
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["allowed", "missing_scope", "acl_denied", "also_universal"])
def test_offline_core_private_callback_rejects_only_proven_allowed_invites(tmp_path, case):
    from unittest.mock import patch
    from test_qqcall_runtime import _auto_accept_runtime, qqcall_service_module

    async def scenario():
        commands, notices = [], []

        async def transport(path, body, _token):
            assert path == "/v1/call/command"
            assert body["operation"] == "reject"
            commands.append(body)
            return {}

        with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", tmp_path / "QQCallProject"):
            runtime, seen, state, *_files = _auto_accept_runtime(
                tmp_path, transport=transport, with_proof=case != "missing_scope",
                admit=lambda _peer: case != "acl_denied",
            )
            state["core_ready"] = False
            runtime._dependency_check = lambda: ("universal",) if case == "also_universal" else ()

            async def notify(peer, group, text, preflight):
                assert group is None and await preflight()
                notices.append(text)

            runtime._send_call_notice = notify
            call = runtime._active_call
            await runtime._emit_lifecycle("invite_callback_ready", call, state)
            await runtime._emit_lifecycle("invite_callback_ready", call, state)
            permitted = case in {"allowed", "also_universal"}
            assert len(commands) == len(notices) == int(permitted)
            if permitted:
                assert commands[0]["scope"] == call["scope"]
                assert "Core" in notices[0]
                assert ("UniversalVC" in notices[0]) == (case == "also_universal")
            assert seen["events"] == seen["custom"] == [], "offline lifecycle remains deferred, never an ordinary user message"
    asyncio.run(scenario())


def test_invitation_refusal_notice_keeps_group_acl_and_has_no_core_message_side_effect():
    async def scenario():
        core, sent = _CoreLoopback(), []
        runtime = _runtime(core)
        identity = await runtime._read_platform_state()

        async def send(peer, group, text, preflight):
            assert await preflight()
            sent.append((peer, group, text))

        runtime._send_call_notice = send
        await runtime._notify_dependency_refusal(PEER, GROUP, "call_missing_tts_universal", identity)
        assert len(sent) == 1 and sent[0][:2] == (PEER, GROUP)
        runtime._admit_group = lambda _peer, _group: False
        await runtime._notify_dependency_refusal(PEER, GROUP, "call_missing_tts", identity)
        assert len(sent) == 1
        assert core.requests == core.responses == []
    asyncio.run(scenario())


def test_private_missing_dependencies_posts_reject_instead_of_accept():
    from test_qqcall_runtime import _runtime as private_runtime, _prime_call, _request, SCOPE

    async def scenario():
        commands = []

        async def transport(path, body, _token):
            assert path == "/v1/call/command"
            commands.append(body)
            return {}

        runtime, _seen = private_runtime(transport=transport)
        _prime_call(runtime)
        runtime._dependency_check = lambda: ("tts", "universal")
        response = await runtime.handle_request(_request("accept", dict(SCOPE)))
        assert response["error"]["code"] == "call_missing_tts_universal"
        assert len(commands) == 1 and commands[0]["operation"] == "reject"
        assert commands[0]["scope"] == SCOPE
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["ready", "redirect", "oversize", "unavailable"])
def test_health_http_authentication_bounds_and_no_redirect(case):
    from aiohttp import web

    async def scenario():
        redirected = []

        async def handler(request):
            assert request.headers.get("Authorization") == "Bearer synthetic-health-secret"
            if case == "redirect":
                return web.Response(status=302, headers={"Location": "/destination"})
            if case == "oversize":
                return web.Response(body=b" " * 65537)
            if case == "unavailable":
                return web.Response(status=503)
            return web.json_response({"ready": True})

        async def destination(_request):
            redirected.append(True)
            return web.json_response({"ready": True})

        app = web.Application()
        app.router.add_get("/health", handler)
        app.router.add_get("/destination", destination)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            url = f"http://127.0.0.1:{runner.addresses[0][1]}/health"
            if case == "ready":
                assert await deps._health_json(url, "synthetic-health-secret") == {"ready": True}
            else:
                with pytest.raises(ValueError):
                    await deps._health_json(url, "synthetic-health-secret")
            assert redirected == []
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_core_error_normalization_preserves_only_known_missing_dependency_labels():
    from test_qqcall_group_wire import qqcall_api
    assert qqcall_api._error_code("call_missing_tts_universal") == "call_missing_tts_universal"
    assert qqcall_api._error_code("call_missing_token_or_user_identity") == "upstream_error"
