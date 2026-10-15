from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import toml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client import qqcall as qqcall_module  # noqa: E402
from qqcall_client import qqcall_service as qqcall_service_module  # noqa: E402
from qqcall_client.qqcall import QQCallConfig, QQCallError, QQCallRuntime  # noqa: E402
from qqcall_client.qqcall_universal_api import (  # noqa: E402
    QQCallRouteRejection,
    QQCallRouteRetired,
    QQCallVoiceError,
)


ACCOUNT = "123456789"
PEER_UIN = "987654321"
UID = "kernel-uid-opaque"
TOKEN = "A" * 43
META = {"serviceInstance": "service-instance-1", "hostGeneration": 7, "connectionGeneration": 9}
SCOPE = {**META, "callId": "call-abc123", "callGeneration": 11}
DYNAMIC_META = {
    "serviceInstance": "723e4567-e89b-42d3-a456-426614174000",
    "hostGeneration": 7,
    "connectionGeneration": 9,
}
DYNAMIC_SCOPE = {**DYNAMIC_META, "callId": "call-abc123", "callGeneration": 11}


def _call(*, peer_uid: str | None = UID, correlation: str = "kernel_from_uid_matched", **updates: Any) -> dict[str, Any]:
    value = {
        "callId": SCOPE["callId"],
        "generation": SCOPE["callGeneration"],
        "peerUid": peer_uid,
        "phase": "invite_ready",
        "uncertain": False,
        "inviteCallbackReady": True,
        "inviteCorrelation": correlation,
        "acceptState": "not_requested",
    }
    value.update(updates)
    return value


def _private_scope_proof(
    *,
    meta: dict[str, Any] = META,
    scope_kind: str = "call",
    relation_id: str = "private-relation",
    from_uid: str = UID,
    call_id: str | None = None,
    call_generation: int | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "profile": qqcall_module._PRIVATE_SCOPE_PROFILE,
        "wrapperSha256": qqcall_module._PRIVATE_SCOPE_WRAPPER_SHA256,
        "callbackSequence": 1,
        "inviteType": 1,
        "relationId": relation_id,
        "fromUid": from_uid,
        "targetPid": 4455,
        "kernelCreationTime100ns": "134000000000000000",
        "connectionGeneration": meta["connectionGeneration"],
        "listenerMutationEpoch": 2,
        "ipcSequence": 6,
        "accountId": ACCOUNT,
        "serviceInstance": meta["serviceInstance"],
        "hostGeneration": meta["hostGeneration"],
        "scopeKind": scope_kind,
        "callId": call_id if scope_kind == "call" else None,
        "callGeneration": call_generation if scope_kind == "call" else None,
        "inviteEventId": "123e4567-e89b-42d3-a456-426614174000",
    }


def _bind_test_kernel(runtime: QQCallRuntime) -> None:
    runtime._peer_manifest_kernel = (4455, 134000000000000000)


def _poll(*, call: dict[str, Any] | None = None, cursor: int = 0, events=None, **updates: Any) -> dict[str, Any]:
    value = {
        "version": 1,
        "accountId": ACCOUNT,
        **META,
        "cursor": cursor,
        "events": [] if events is None else events,
        "currentCall": call,
    }
    value.update(updates)
    return value


def _identity(account: str = ACCOUNT, connection: object | None = None) -> dict[str, Any]:
    return {"connected": True, "account_id": account, "identity": (connection or object(), 4, account)}


def _enabled_config(token_file: str = "C:\\private\\qqcall.token", **updates: Any) -> QQCallConfig:
    value = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:6112",
        "token_file": token_file,
        "account_id": ACCOUNT,
        "peer_map": {UID: PEER_UIN},
        "poll_interval_sec": 1.0,
    }
    value.update(updates)
    return QQCallConfig.from_mapping(value)


def _runtime(
    *,
    transport=None,
    state=None,
    admit=None,
    admit_group=None,
    send_busy_private=None,
    config: QQCallConfig | None = None,
):
    state = state or _identity()
    seen: dict[str, Any] = {"events": [], "custom": [], "admissions": []}

    async def emit(message):
        seen["events"].append(message)
        return True

    async def custom(kind, message):
        seen["custom"].append((kind, message))
        return True

    async def read_state():
        return state

    async def check(peer):
        seen["admissions"].append(peer)
        return True if admit is None else await _maybe(admit(peer))

    runtime = QQCallRuntime(
        config or _enabled_config(),
        "qq",
        emit,
        custom,
        read_state,
        check,
        send_busy_private=send_busy_private,
        admit_group=admit_group,
        dependency_check=lambda: (),
        transport=transport,
    )
    runtime._token = TOKEN
    runtime._client_instance = "qtestclient"
    runtime._identity = tuple(state["identity"])
    runtime._identity_account = state["account_id"]
    return runtime, seen


async def _maybe(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


def _request(operation: str, scope=None, request_id: str = "request_id_123456789012") -> dict[str, Any]:
    return {
        "platform": "qq",
        "message_type_name": "qqcall_request",
        "content": {
            "version": 1,
            "request_id": request_id,
            "platform": "qq",
            "operation": operation,
            "peer_user_id": PEER_UIN,
            "scope": scope,
        },
    }


def _group_request(
    *,
    peer_user_id: str = PEER_UIN,
    group_id: str = "246813579",
    trigger_text: str = "#join_vc_qq",
    request_id: str = "request_id_123456789012",
) -> dict[str, Any]:
    request = _request("join_group", None, request_id)
    request["content"].update({"group_id": group_id, "trigger_text": trigger_text, "peer_user_id": peer_user_id})
    return request


def _prime_call(runtime: QQCallRuntime, call: dict[str, Any] | None = None) -> None:
    runtime._service_meta = dict(META)
    runtime._active_call = runtime._normalize_call(call or _call(), META)
    runtime._bootstrapped = True
    runtime._lease_claimed = True


def _prime_idle(runtime: QQCallRuntime) -> None:
    runtime._service_meta = dict(META)
    runtime._active_call = None
    runtime._bootstrapped = True
    runtime._lease_claimed = True


def _shared_auto_accept_files(
    directory: Path,
    meta: dict[str, Any] = DYNAMIC_META,
    peer_map: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    service_config_path = REPO_ROOT / "NachoBot-QQCall" / "service" / "config.py"
    spec = importlib.util.spec_from_file_location("_qqcall_runtime_test_service_config", service_config_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = copy.deepcopy(module.DEFAULT_CONFIG)
    value["plugin"]["enabled"] = True
    qqcall = value["qqcall"]
    qqcall.update({"adapter": "napcat", "account_id": ACCOUNT})
    qqcall["onebot"]["url"] = "ws://127.0.0.1:3001/ws"
    qqcall["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
    qqcall["audio"].update({
        "capture_device": "Test Virtual Capture",
        "playback_device": "Test Virtual Playback",
    })
    service_root = directory / "QQCallProject"
    qqcall["runtime_dir"] = str((service_root / ".runtime" / "runtime").resolve())
    qqcall["policy"] = {"auto_accept": True}
    service_root.mkdir(parents=True, exist_ok=True)
    public_path = service_root.parent / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(toml.dumps(value), encoding="utf-8")
    manifest_path = Path(qqcall["runtime_dir"]) / "adapter-runtime.private.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "accountId": ACCOUNT,
        "serviceInstance": meta["serviceInstance"],
        "hostGeneration": meta["hostGeneration"],
        "connectionGeneration": meta["connectionGeneration"],
        "kernelPid": 4455,
        "kernelCreationTime100ns": "134000000000000000",
        "peerMap": peer_map if peer_map is not None else {UID: PEER_UIN},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return public_path, manifest_path


def _auto_accept_runtime(
    directory: Path,
    *,
    transport,
    admit=None,
    with_proof: bool = False,
    meta: dict[str, Any] = DYNAMIC_META,
    call_value: dict[str, Any] | None = None,
    peer_map: dict[str, str] | None = None,
    correlation: str = "kernel_from_uid_matched",
    native_from_uid: str = UID,
    relation_id: str = "private-relation",
):
    public_path, manifest_path = _shared_auto_accept_files(directory, meta, peer_map)
    state = _identity()
    config = _enabled_config(
        peer_map={},
        peer_map_file=str(manifest_path),
        shared_config_file=str(public_path),
        adapter_name="napcat",
        auto_accept=True,
    )
    runtime, seen = _runtime(transport=transport, state=state, admit=admit, config=config)
    runtime._service_meta = dict(meta)
    dynamic_map = runtime._read_peer_map(meta)
    call = copy.deepcopy(call_value) if call_value is not None else _call(correlation=correlation)
    if with_proof:
        call["privateScopeProof"] = _private_scope_proof(
            meta=meta,
            scope_kind="call",
            relation_id=relation_id,
            from_uid=native_from_uid,
            call_id=call["callId"],
            call_generation=call["generation"],
        )
    runtime._active_call = runtime._normalize_call(call, meta, dynamic_map)
    runtime._bootstrapped = True
    runtime._lease_claimed = True
    return runtime, seen, state, public_path, manifest_path


def _start_real_control_service() -> tuple[subprocess.Popen[str], int]:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node.js is unavailable for cross-language control-service verification")
    service_path = REPO_ROOT / "NachoBot-QQCall" / "bridge" / "control-service.cjs"
    script = r"""
const { createControlService } = require(process.argv[1]);
const token = process.env.QQCALL_TEST_TOKEN;
const accountId = '123456789';
const call = {
  callId: 'call-abc123', generation: 11, peerUid: 'kernel-uid-opaque',
  phase: 'invite_ready', uncertain: false, inviteCallbackReady: true,
  inviteCorrelation: 'kernel_from_uid_matched', acceptState: 'not_requested',
};
const bridge = {
  status: () => ({ hostGeneration: 7, connectionGeneration: 9 }),
  subscribeStatus: () => () => {},
  subscribeControlEvents: () => () => {},
  getIntegrationSnapshot: () => ({ status: { hostGeneration: 7, connectionGeneration: 9 }, call }),
  accept: (callId) => ({ posted: true, id: 55, callId, callGeneration: 11 }),
  hangup: () => { throw Object.assign(new Error('unsupported'), { code: 'unsupported_unverified_command:hangup' }); },
  reject: () => { throw Object.assign(new Error('unsupported'), { code: 'unsupported_unverified_command:reject' }); },
};
const service = createControlService({ bridge, token, accountId, port: 0 });
service.start().then((status) => {
  process.stdout.write(JSON.stringify({ port: status.port }) + '\n');
  process.stdin.once('data', async () => { await service.stop(); process.exit(0); });
}).catch(() => process.exit(2));
"""
    env = dict(os.environ)
    env["QQCALL_TEST_TOKEN"] = TOKEN
    process = subprocess.Popen(
        [node, "-e", script, str(service_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    if not line:
        stderr = process.stderr.read() if process.stderr else ""
        process.kill()
        raise AssertionError(f"Node control-service did not start (exit={process.poll()}, stderr={stderr[:200]})")
    return process, json.loads(line)["port"]


class TestQQCallRuntime(unittest.TestCase):
    def test_audio_route_transport_error_keeps_bounded_status_and_fixed_code(self):
        async def verify_failure(error, *, replace_post=False, expect_retired=False):
            runtime, _seen = _runtime(transport=lambda _path, _payload, _token: None)
            runtime._lease_claimed = True
            runtime._audio_plan_required = False
            runtime._voice._backend = SimpleNamespace(config=SimpleNamespace(
                capture_mode="loopback",
                device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
            ))
            connected_call = {
                "scope": dict(SCOPE), "peer_user_id": PEER_UIN, "peer_uid": UID,
                "phase": "connected", "uncertain": False, "inviteCallbackReady": True,
                "inviteCorrelation": "kernel_from_uid_matched",
            }
            runtime._voice.update_call(connected_call, runtime._audio_identity())

            async def current_context(_scope, _client, _identity, *, failure_sink=None):
                return True

            runtime._audio_route_context_is_current = current_context

            async def fail_post(_path, _payload):
                raise error

            if replace_post:
                runtime._post = fail_post
            else:
                async def transport(_path, _payload, _token):
                    raise error

                runtime._transport = transport

            outcome = await runtime._verify_audio_route(
                SCOPE,
                SimpleNamespace(
                    capture_mode="loopback",
                    device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
                ),
            )
            if expect_retired:
                self.assertIsInstance(outcome, QQCallRouteRetired)
            else:
                self.assertIsInstance(outcome, QQCallRouteRejection)
                self.assertEqual(outcome.reason, "route_helper_transport_error")
                with self.assertRaises(QQCallVoiceError):
                    await runtime._voice._verify_route(runtime._voice._desired)
            return outcome, runtime._voice.status()["routeDiagnostics"], runtime

        rejection, diagnostics, runtime = asyncio.run(verify_failure(QQCallError("service_stopped", 503)))
        self.assertEqual(rejection._helper_http_status, 503)
        self.assertEqual(rejection._helper_error_code, "service_stopped")
        self.assertEqual(diagnostics["firstFailure"], "route_helper_transport_error")
        self.assertEqual(diagnostics["routeHelperHttpStatus"], 503)
        self.assertEqual(diagnostics["routeHelperErrorCode"], "service_stopped")

        async def later_transport(_path, _payload, _token):
            raise QQCallError("service_stopped", 503)

        runtime._transport = later_transport

        async def preserve_first_failure():
            with self.assertRaises(QQCallVoiceError):
                await runtime._voice._verify_route(runtime._voice._desired)
            return runtime._voice.status()["routeDiagnostics"]

        preserved = asyncio.run(preserve_first_failure())
        self.assertEqual(preserved["firstFailure"], "route_helper_transport_error")
        self.assertEqual(preserved["routeHelperHttpStatus"], 503)
        self.assertEqual(preserved["routeHelperErrorCode"], "service_stopped")
        self.assertEqual(preserved["failureCount"], 1)

        retired, retired_diagnostics, _retired_runtime = asyncio.run(
            verify_failure(QQCallError("call_scope_stale", 409), expect_retired=True)
        )
        self.assertIsInstance(retired, QQCallRouteRetired)
        self.assertIsNone(retired_diagnostics["firstFailure"])
        self.assertEqual(retired_diagnostics["failureCount"], 0)
        self.assertIsNone(retired_diagnostics["routeHelperHttpStatus"])
        self.assertIsNone(retired_diagnostics["routeHelperErrorCode"])

        class DerivedQQCallError(QQCallError):
            pass

        for error in (
            QQCallError("call_scope_stale", True),
            QQCallError("call_scope_stale", 503),
            QQCallError("call_not_connected", 409),
            DerivedQQCallError("call_scope_stale", 409),
        ):
            with self.subTest(error_type=type(error).__name__, status=error.status_code, code=error.code):
                nonexact, _nonexact_diagnostics, _nonexact_runtime = asyncio.run(verify_failure(error))
                self.assertIsInstance(nonexact, QQCallRouteRejection)
                self.assertEqual(nonexact.reason, "route_helper_transport_error")
        bool_status, _bool_diagnostics, _bool_runtime = asyncio.run(
            verify_failure(QQCallError("call_scope_stale", True))
        )
        self.assertIsNone(bool_status._helper_http_status)

        next_call = {
            "scope": {**SCOPE, "callGeneration": SCOPE["callGeneration"] + 1},
            "peer_user_id": PEER_UIN,
            "peer_uid": UID,
            "phase": "connected",
            "uncertain": False,
            "inviteCallbackReady": True,
            "inviteCorrelation": "kernel_from_uid_matched",
        }
        runtime._voice.update_call(next_call, runtime._audio_identity())
        reset = runtime._voice.status()["routeDiagnostics"]
        self.assertIsNone(reset["firstFailure"])
        self.assertIsNone(reset["routeHelperHttpStatus"])
        self.assertIsNone(reset["routeHelperErrorCode"])
        self.assertEqual(reset["failureCount"], 0)

        generic_error = RuntimeError("private response body and authorization header")
        generic_rejection, generic_diagnostics, _generic_runtime = asyncio.run(
            verify_failure(generic_error, replace_post=True)
        )
        self.assertIsNone(generic_rejection._helper_http_status)
        self.assertIsNone(generic_rejection._helper_error_code)
        self.assertIsNone(generic_diagnostics["routeHelperHttpStatus"])
        self.assertIsNone(generic_diagnostics["routeHelperErrorCode"])
        self.assertNotIn("private response", repr(generic_rejection))
        self.assertNotIn("authorization", repr(generic_diagnostics))

    def test_audio_route_phase_timeout_is_scope_bound_and_keeps_first_stage(self):
        audio_config = SimpleNamespace(
            capture_mode="loopback",
            device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
        )
        connected_call = {
            "scope": dict(SCOPE),
            "peer_user_id": PEER_UIN,
            "peer_uid": UID,
            "phase": "connected",
            "uncertain": False,
            "inviteCallbackReady": True,
            "inviteCorrelation": "kernel_from_uid_matched",
        }

        async def run_timeout(blocked_phase):
            context_calls = 0
            transport_calls = 0
            runtime, _seen = _runtime(transport=None)
            runtime._lease_claimed = True
            runtime._audio_plan_required = False
            runtime._voice._backend = SimpleNamespace(config=audio_config)
            runtime._voice.update_call(connected_call, runtime._audio_identity())

            async def context(_scope, _client, _identity, *, failure_sink=None):
                nonlocal context_calls
                context_calls += 1
                should_stall = (
                    blocked_phase == "pre_context" and context_calls == 1
                    or blocked_phase == "post_context" and context_calls == 2
                )
                if should_stall:
                    await asyncio.Event().wait()
                return True

            async def transport(_path, _payload, _token):
                nonlocal transport_calls
                transport_calls += 1
                if blocked_phase == "helper_request":
                    await asyncio.Event().wait()
                return {}

            runtime._audio_route_context_is_current = context
            runtime._transport = transport
            voice = runtime._voice
            with patch("qqcall_client.qqcall_universal_api._ROUTE_VERIFIER_TIMEOUT", 0.01):
                with self.assertRaises(QQCallVoiceError) as raised:
                    await voice._verify_route(voice._desired)
            self.assertEqual(raised.exception.code, "universal_api_route_unverified")
            diagnostics = voice.status()["routeDiagnostics"]
            self.assertEqual(diagnostics["firstFailure"], "route_check_timeout")
            self.assertEqual(diagnostics["routeCheckStage"], blocked_phase)
            self.assertEqual(diagnostics["firstFailureStage"], blocked_phase)
            if blocked_phase == "pre_context":
                self.assertEqual(transport_calls, 0)
            if blocked_phase in {"helper_request", "post_context"}:
                self.assertEqual(transport_calls, 1)

            if blocked_phase == "helper_request":
                # A later failure and verifier invocation may update the current
                # stage, but cannot replace the first timeout's captured phase.
                async def immediate_transport(_path, _payload, _token):
                    return {}

                runtime._transport = immediate_transport
                with self.assertRaises(QQCallVoiceError):
                    await voice._verify_route(voice._desired)
                diagnostics = voice.status()["routeDiagnostics"]
                self.assertEqual(diagnostics["firstFailure"], "route_check_timeout")
                self.assertEqual(diagnostics["firstFailureStage"], "helper_request")
                self.assertEqual(diagnostics["routeCheckStage"], "proof_validation")

        async def scenario():
            for phase in ("pre_context", "helper_request", "post_context"):
                with self.subTest(phase=phase):
                    await run_timeout(phase)

        asyncio.run(scenario())

    def test_audio_route_success_records_completed_stage(self):
        async def scenario():
            now_ms = time.time_ns() // 1_000_000
            proof = {
                "version": 1,
                "accountId": ACCOUNT,
                "scope": dict(SCOPE),
                "verified": True,
                "source": "windows_active_sessions",
                "hostPid": 4455,
                "observedAtMs": now_ms,
                "hostInput": {
                    "deviceName": "Test Playback", "flow": "capture", "state": "active",
                    "endpointIdHash": "a" * 64, "processId": 4455,
                },
                "hostOutput": {
                    "deviceName": "Test Capture", "flow": "render", "state": "active",
                    "endpointIdHash": "b" * 64, "processId": 4455,
                },
            }

            async def transport(_path, _payload, _token):
                return proof

            runtime, _seen = _runtime(transport=transport)
            runtime._lease_claimed = True
            runtime._audio_plan_required = False
            runtime._voice._backend = SimpleNamespace(config=SimpleNamespace(
                capture_mode="loopback",
                device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
            ))
            connected_call = {
                "scope": dict(SCOPE), "peer_user_id": PEER_UIN, "peer_uid": UID,
                "phase": "connected", "uncertain": False, "inviteCallbackReady": True,
                "inviteCorrelation": "kernel_from_uid_matched",
            }
            runtime._voice.update_call(connected_call, runtime._audio_identity())

            async def context(_scope, _client, _identity, *, failure_sink=None):
                return True

            runtime._audio_route_context_is_current = context
            proof_time = await runtime._voice._verify_route(runtime._voice._desired)
            self.assertGreater(proof_time, 0)
            diagnostics = runtime._voice.status()["routeDiagnostics"]
            self.assertEqual(diagnostics["routeCheckStage"], "completed")
            self.assertIsNone(diagnostics["firstFailureStage"])

        asyncio.run(scenario())

    def test_audio_route_negative_helper_code_is_bounded_and_falsey(self):
        async def verify(
            response,
            *,
            requested_scope=SCOPE,
            plan_required=False,
            plan_error=None,
            context_current=True,
        ):
            async def transport(path, payload, token):
                self.assertEqual(path, "/v1/audio/route")
                self.assertEqual(payload["scope"], SCOPE)
                self.assertEqual(token, TOKEN)
                return response

            runtime, _seen = _runtime(transport=transport)
            runtime._lease_claimed = True
            runtime._audio_plan_required = plan_required

            if plan_error is not None:
                def read_plan():
                    raise QQCallError(plan_error)

                runtime._read_current_audio_plan = read_plan

            async def current_context(_scope, _client_instance, _identity, *, failure_sink=None):
                if not context_current and failure_sink is not None:
                    failure_sink("route_identity_mismatch")
                return context_current

            runtime._audio_route_context_is_current = current_context
            audio_config = SimpleNamespace(
                capture_mode="loopback",
                device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
            )
            return await runtime._verify_audio_route(requested_scope, audio_config)

        valid_negative = {
            "version": 1,
            "accountId": ACCOUNT,
            "scope": dict(SCOPE),
            "verified": False,
            "code": "host_input_not_active",
        }
        result = asyncio.run(verify(valid_negative))
        self.assertIsInstance(result, QQCallRouteRejection)
        self.assertEqual(result.reason, "host_input_not_active")
        self.assertFalse(result)

        invalid_scope = asyncio.run(verify(valid_negative, requested_scope={}))
        self.assertEqual(invalid_scope.reason, "route_scope_invalid")
        changed_plan = asyncio.run(verify(valid_negative, plan_required=True, plan_error="audio_plan_replaced"))
        self.assertEqual(changed_plan.reason, "route_plan_changed")
        stale_context = asyncio.run(verify(valid_negative, context_current=False))
        self.assertEqual(stale_context.reason, "route_identity_mismatch")

        wrong_scope = {**valid_negative, "scope": {**SCOPE, "callGeneration": 12}}
        scope_rejection = asyncio.run(verify(wrong_scope))
        self.assertEqual(scope_rejection.reason, "route_scope_mismatch")

        for code in ("private-caller-token-998877", "route_check_timeout"):
            with self.subTest(code=code):
                untrusted_negative = {**valid_negative, "code": code}
                untrusted = asyncio.run(verify(untrusted_negative))
                self.assertIsInstance(untrusted, QQCallRouteRejection)
                self.assertEqual(untrusted.reason, "route_helper_rejected_unclassified")
                self.assertNotIn(code, repr(untrusted))

        async def startup_retry_contract():
            async def transport(_path, _payload, _token):
                return valid_negative

            runtime, _seen = _runtime(transport=transport)
            runtime._lease_claimed = True
            runtime._audio_plan_required = False
            runtime._voice._backend = SimpleNamespace(config=SimpleNamespace(
                capture_mode="loopback",
                device_scope={"capture_render_name": "Test Capture", "playback_render_name": "Test Playback"},
            ))
            connected_call = {
                "scope": dict(SCOPE), "peer_user_id": PEER_UIN, "peer_uid": UID,
                "phase": "connected", "uncertain": False, "inviteCallbackReady": True,
                "inviteCorrelation": "kernel_from_uid_matched",
            }
            runtime._voice.update_call(connected_call, runtime._audio_identity())

            async def current_context(_scope, _client, _identity, *, failure_sink=None):
                return True

            runtime._audio_route_context_is_current = current_context
            voice = runtime._voice
            with self.assertRaises(Exception) as pending:
                await voice._verify_route(
                    voice._desired,
                    startup_pending=True,
                    deadline=time.monotonic() + 1.0,
                )
            self.assertEqual(type(pending.exception).__name__, "_StartupRoutePending")
            self.assertIsNone(voice.status()["routeDiagnostics"]["firstFailure"])

            with self.assertRaises(QQCallVoiceError):
                await voice._verify_route(voice._desired)
            self.assertEqual(
                voice.status()["routeDiagnostics"]["firstFailure"],
                "host_input_not_active",
            )

        asyncio.run(startup_retry_contract())

    def test_audio_route_context_reports_fixed_identity_and_scope_categories(self):
        async def classify(*, scope=SCOPE, identity_change=False, lease_claimed=True):
            runtime, _seen = _runtime()
            runtime._lease_claimed = lease_claimed
            runtime._service_meta = dict(META)
            runtime._active_call = {
                "scope": dict(SCOPE),
                "phase": "connected",
                "uncertain": False,
                "inviteCallbackReady": True,
                "inviteCorrelation": "kernel_from_uid_matched",
                "peer_user_id": PEER_UIN,
            }
            identity = {
                "identity": runtime._identity if not identity_change else ("other",),
                "account_id": runtime._identity_account,
                "connected": True,
                "core_ready": True,
            }
            failures = []
            result = await runtime._audio_route_context_is_current(
                scope, "qtestclient", identity, failure_sink=failures.append,
            )
            return result, failures

        valid, valid_failures = asyncio.run(classify())
        self.assertTrue(valid)
        self.assertEqual(valid_failures, [])

        identity_valid, identity_failures = asyncio.run(classify(identity_change=True))
        self.assertFalse(identity_valid)
        self.assertEqual(identity_failures, ["route_identity_mismatch"])

        scope_valid, scope_failures = asyncio.run(
            classify(scope={**SCOPE, "callGeneration": SCOPE["callGeneration"] + 1})
        )
        self.assertFalse(scope_valid)
        self.assertEqual(scope_failures, ["route_scope_unverified"])

        unavailable, unavailable_failures = asyncio.run(classify(lease_claimed=False))
        self.assertFalse(unavailable)
        self.assertEqual(unavailable_failures, ["route_identity_unavailable"])

    def test_config_is_strict_default_disabled_and_never_reads_token_when_disabled(self):
        config = QQCallConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(dict(config.peer_map), {})
        invalid_configs = (
            {"enabled": 1},
            {"account_id": " 123"},
            {"endpoint": "http://example.com:6112"},
            {"endpoint": "http://127.0.0.1:6112/path"},
            {"peer_map": {"uid-a": "123", "uid-b": "123"}},
            {"allowed_users": (PEER_UIN,)},
            {"poll_interval_sec": True},
            {"unknown": True},
        )
        for value in invalid_configs:
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    QQCallConfig.from_mapping(value)

        calls = []

        async def transport(*args):
            calls.append(args)
            raise AssertionError("disabled runtime must not touch control service")

        runtime, _ = _runtime(config=QQCallConfig(), transport=transport)
        runtime._token = None
        asyncio.run(runtime.run())
        self.assertEqual(calls, [])

    def test_senderless_resync_maps_uid_and_drops_uncorrelated_or_unknown_peer(self):
        async def scenario(correlation="kernel_from_uid_matched", uid=UID):
            sent = []

            async def transport(path, payload, token):
                self.assertEqual(path, "/v1/adapter/poll")
                return _poll(call=_call(peer_uid=uid, correlation=correlation), cursor=1)

            runtime, _ = _runtime(transport=transport)
            runtime._send_message = lambda message: sent.append(message) or True
            self.assertTrue(await runtime._poll_once(await runtime._read_platform_state()))
            self.assertEqual(runtime.status()["cursor"], 1)
            should_send = correlation in {"kernel_from_uid_matched", "kernel_relation_uid_matched"} and uid == UID
            self.assertEqual(len(sent), int(should_send))
            if sent:
                message = sent[0]
                self.assertIsNone(message.message_info.user_info)
                self.assertIsNone(message.message_info.group_info)
                self.assertIsNone(message.message_info.sender_info)
                additional = message.message_info.additional_config
                self.assertEqual(additional["system_event"]["type"], "qq.call.invite_callback_ready")
                self.assertEqual(additional["system_event"]["data"]["invite_correlation"], correlation)
                self.assertEqual(additional["system_event_route"]["peer"]["user_id"], PEER_UIN)
                self.assertEqual(additional["qqcall_scope"], SCOPE)
                self.assertNotIn(UID, json.dumps(additional))

        asyncio.run(scenario())
        asyncio.run(scenario(correlation="kernel_relation_uid_matched"))
        asyncio.run(scenario(correlation="awaiting_20006"))
        asyncio.run(scenario(uid="unmapped-uid"))
        asyncio.run(scenario(correlation="kernel_relation_uid_matched", uid="unmapped-uid"))

    def test_resync_never_reannounces_terminal_ready_call(self):
        self.assertEqual(QQCallRuntime._resync_kind(_call(phase="expired")), "call_terminal")
        self.assertEqual(QQCallRuntime._resync_kind(_call(phase="end_observed", uncertain=True)), "call_terminal")
        self.assertIsNone(QQCallRuntime._resync_kind(_call(phase="invite_ready", uncertain=True)))

    def test_accept_is_full_scope_admitted_posted_only_and_request_id_deduped(self):
        calls = []

        async def transport(path, payload, token):
            calls.append((path, payload))
            return {
                "version": 1,
                "accountId": ACCOUNT,
                "serviceInstance": META["serviceInstance"],
                "operation": "accept",
                "scope": dict(SCOPE),
                "result": {"posted": True, "id": 55, "callId": SCOPE["callId"], "callGeneration": 11},
            }

        runtime, seen = _runtime(transport=transport)
        _prime_call(runtime)
        raw = _request("accept", dict(SCOPE))
        response = asyncio.run(runtime.handle_request(raw))
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["data"]["result"]["posted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("/v1/call/command", {
            "accountId": ACCOUNT,
            "clientInstance": "qtestclient",
            "operation": "accept",
            "scope": SCOPE,
        }))
        self.assertEqual(asyncio.run(runtime.handle_request(raw)), response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["custom"][-1][0], "qqcall_response")

    def test_relation_uid_matched_uses_existing_peer_map_and_command_guards(self):
        calls = []

        async def transport(path, payload, _token):
            calls.append((path, payload))
            return {
                "version": 1,
                "accountId": ACCOUNT,
                "serviceInstance": META["serviceInstance"],
                "operation": "accept",
                "scope": dict(SCOPE),
                "result": {"posted": True, "id": 56, "callId": SCOPE["callId"], "callGeneration": 11},
            }

        runtime, seen = _runtime(transport=transport)
        _prime_call(runtime, _call(correlation="kernel_relation_uid_matched"))
        accepted = asyncio.run(runtime.handle_request(
            _request("accept", dict(SCOPE), "request_id_relation_uid_123456")
        ))
        self.assertEqual(accepted["status"], "ok")
        self.assertEqual(seen["admissions"], [PEER_UIN])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["scope"], SCOPE)

        unmapped, unmapped_seen = _runtime(
            transport=transport,
            config=_enabled_config(peer_map={}),
        )
        _prime_call(unmapped, _call(correlation="kernel_relation_uid_matched"))
        rejected = asyncio.run(unmapped.handle_request(
            _request("accept", dict(SCOPE), "request_id_relation_uid_654321")
        ))
        self.assertEqual(rejected["error"]["code"], "no_active_call")
        self.assertEqual(unmapped_seen["admissions"], [])
        self.assertEqual(len(calls), 1)

        with self.assertRaises(QQCallError) as error:
            runtime._normalize_call(_call(correlation="u_private_uid_998877"), META)
        self.assertEqual(error.exception.code, "service_call_invalid")

    def test_generic_invite_auto_accept_fails_closed_without_private_scope_proof(self):
        async def scenario():
            commands = []

            async def transport(path, payload, _token):
                commands.append((path, payload))
                return {
                    "version": 1,
                    "accountId": ACCOUNT,
                    "serviceInstance": DYNAMIC_META["serviceInstance"],
                    "operation": "accept",
                    "scope": dict(DYNAMIC_SCOPE),
                    "result": {
                        "posted": True,
                        "id": 77,
                        "callId": DYNAMIC_SCOPE["callId"],
                        "callGeneration": DYNAMIC_SCOPE["callGeneration"],
                    },
                }

            with tempfile.TemporaryDirectory() as directory:
                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, seen, state, _public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport,
                    )
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    await runtime._maybe_auto_accept(runtime._active_call, state)

            self.assertEqual(commands, [])
            self.assertEqual(seen["admissions"], [PEER_UIN])
            self.assertEqual(seen["custom"], [])
            self.assertEqual(runtime._last_failure_code, "private_scope_unverified")

        asyncio.run(scenario())

    def test_node_contract_fixture_allows_exactly_one_empty_uid_private_accept(self):
        fixture_path = REPO_ROOT / "NachoBot-QQCall" / ".runtime" / "private-scope-repair" / "after" / "private-scope-contract.fixture.json"
        try:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.skipTest("run the Node private-scope contract suite first to generate its poll fixture")
        generated_at_ms = fixture.get("generatedAtMs") if isinstance(fixture, dict) else None
        if (
            isinstance(generated_at_ms, bool)
            or not isinstance(generated_at_ms, int)
            or not 0 <= time.time_ns() // 1_000_000 - generated_at_ms <= 5 * 60 * 1000
        ):
            self.skipTest("Node private-scope contract fixture is stale; rerun the Node contract suite")

        expected_sources = (
            "agent/bootstrap.cjs",
            "agent/kernel-agent.cjs",
            "probe/broker.cjs",
            "bridge/host-client.cjs",
            "bridge/qqcall-bridge.cjs",
            "bridge/control-service.cjs",
            "shared/private-invite-scope.cjs",
            "tests/private-invite-scope-contract.test.cjs",
        )
        source_hashes = fixture.get("sourceSha256") if isinstance(fixture, dict) else None
        self.assertIsInstance(source_hashes, dict, "fresh Node fixture must include the producer source seal")
        self.assertEqual(set(source_hashes), set(expected_sources))
        for relative_path in expected_sources:
            source_path = REPO_ROOT / "NachoBot-QQCall" / relative_path
            actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest().upper()
            self.assertEqual(source_hashes[relative_path], actual_hash, f"stale Node fixture source: {relative_path}")

        poll = fixture.get("poll")
        call = poll.get("currentCall") if isinstance(poll, dict) else None
        proof = call.get("privateScopeProof") if isinstance(call, dict) else None
        self.assertEqual(poll.get("accountId"), ACCOUNT)
        self.assertIsInstance(call, dict)
        self.assertIsInstance(proof, dict)
        self.assertEqual(call.get("inviteCorrelation"), "kernel_relation_uid_matched")
        self.assertEqual(proof.get("inviteType"), 1)
        self.assertEqual(proof.get("fromUid"), "")
        self.assertEqual(proof.get("relationId"), PEER_UIN)
        self.assertEqual(proof.get("scopeKind"), "call")

        meta = {
            "serviceInstance": poll["serviceInstance"],
            "hostGeneration": poll["hostGeneration"],
            "connectionGeneration": poll["connectionGeneration"],
        }
        peer_map = {call["peerUid"]: proof["relationId"]}
        commands = []

        async def transport(path, payload, token):
            self.assertEqual(token, TOKEN)
            self.assertEqual(path, "/v1/call/command")
            self.assertEqual(payload["operation"], "accept")
            commands.append((path, payload))
            scope = dict(payload["scope"])
            return {
                "version": 1,
                "accountId": ACCOUNT,
                "serviceInstance": meta["serviceInstance"],
                "operation": "accept",
                "scope": scope,
                "result": {
                    "posted": True,
                    "id": 77,
                    "callId": scope["callId"],
                    "callGeneration": scope["callGeneration"],
                },
            }

        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, seen, state, _public, _manifest = _auto_accept_runtime(
                        Path(directory),
                        transport=transport,
                        meta=meta,
                        call_value=call,
                        peer_map=peer_map,
                    )
                    self.assertEqual(runtime._active_call["private_scope_proof"]["fromUid"], "")
                    self.assertEqual(runtime._active_call["inviteCorrelation"], "kernel_relation_uid_matched")
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    await runtime._maybe_auto_accept(runtime._active_call, state)

            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][1]["scope"], runtime._active_call["scope"])
            self.assertGreaterEqual(len(seen["admissions"]), 1)
            self.assertTrue(all(peer == proof["relationId"] for peer in seen["admissions"]))

        asyncio.run(scenario())

    def test_empty_native_uid_private_proof_requires_exact_current_relation_binding(self):
        async def scenario():
            cases = (
                ("wrong correlation", {"correlation": "kernel_from_uid_matched"}, {UID: PEER_UIN}),
                ("wrong relation map", {"correlation": "kernel_relation_uid_matched", "relation_id": PEER_UIN}, {UID: "111222333"}),
                ("explicit bad native uid", {"correlation": "kernel_relation_uid_matched", "native_from_uid": None}, {UID: PEER_UIN}),
            )
            for index, (label, options, peer_map) in enumerate(cases):
                with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                    calls = []

                    async def transport(path, payload, _token):
                        calls.append((path, payload))
                        return {}

                    with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                        runtime, _seen, state, _public, _manifest = _auto_accept_runtime(
                            Path(directory),
                            transport=transport,
                            with_proof=True,
                            correlation=options.get("correlation", "kernel_relation_uid_matched"),
                            native_from_uid=options.get("native_from_uid", ""),
                            relation_id=options.get("relation_id", PEER_UIN),
                            peer_map=peer_map,
                            call_value=_call(correlation=options.get("correlation", "kernel_relation_uid_matched")),
                        )
                        await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(calls, [])
                    self.assertIn(runtime._last_failure_code, {"private_scope_unverified", "auto_accept_refused"})

        asyncio.run(scenario())

    def test_private_scope_proof_is_bound_to_current_kernel_account_and_single_call(self):
        async def scenario():
            invalidations = (
                ("kernel pid", lambda proof: proof.update(targetPid=proof["targetPid"] + 1)),
                ("kernel FILETIME", lambda proof: proof.update(kernelCreationTime100ns="134000000000000001")),
                ("connection generation", lambda proof: proof.update(connectionGeneration=10)),
                ("Host generation", lambda proof: proof.update(hostGeneration=8)),
                ("account", lambda proof: proof.update(accountId="999999999")),
                ("service instance", lambda proof: proof.update(serviceInstance="other-service-instance")),
                ("invite type", lambda proof: proof.update(inviteType=2)),
                ("another call ID", lambda proof: proof.update(callId="call-other")),
                ("another call generation", lambda proof: proof.update(callGeneration=12)),
                ("malformed shape", lambda proof: proof.pop("ipcSequence")),
            )
            for label, invalidate in invalidations:
                with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                    commands = []

                    async def transport(path, payload, _token):
                        commands.append((path, payload))
                        return {}

                    call = _call(correlation="kernel_from_uid_matched")
                    proof = _private_scope_proof(
                        meta=DYNAMIC_META,
                        relation_id="private-relation",
                        from_uid=UID,
                        call_id=call["callId"],
                        call_generation=call["generation"],
                    )
                    invalidate(proof)
                    call["privateScopeProof"] = proof
                    with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                        runtime, _seen, state, _public, _manifest = _auto_accept_runtime(
                            Path(directory),
                            transport=transport,
                            meta=DYNAMIC_META,
                            call_value=call,
                            peer_map={UID: PEER_UIN},
                        )
                        self.assertIsNone(runtime._active_call["private_scope_proof"])
                        await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(commands, [], f"invalid {label} must never reach accept")
                    self.assertIn(runtime._last_failure_code, {"private_scope_unverified", "auto_accept_refused"})

        asyncio.run(scenario())

    def test_unclassified_auto_accept_reports_policy_denial_before_scope_gap(self):
        async def scenario():
            commands = []

            async def transport(path, payload, _token):
                commands.append((path, payload))
                return {}

            with tempfile.TemporaryDirectory() as directory:
                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, seen, state, _public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport, admit=lambda _peer: False,
                    )
                    await runtime._maybe_auto_accept(runtime._active_call, state)

            self.assertEqual(commands, [])
            self.assertEqual(seen["admissions"], [PEER_UIN])
            self.assertEqual(runtime._last_failure_code, "auto_accept_refused")

        asyncio.run(scenario())

    def test_dynamic_peer_map_is_authoritative_and_rejects_unknown_or_aliased_uids(self):
        with tempfile.TemporaryDirectory() as directory:
            _public_path, manifest_path = _shared_auto_accept_files(Path(directory))
            runtime, _seen = _runtime(config=_enabled_config(
                peer_map={"legacy-alias": "111222333"},
                peer_map_file=str(manifest_path),
            ))
            peer_map = runtime._read_peer_map(DYNAMIC_META)
            self.assertEqual(peer_map, {UID: PEER_UIN})
            unknown = runtime._normalize_call(_call(peer_uid="legacy-alias"), DYNAMIC_META, peer_map)
            self.assertIsNone(unknown["peer_user_id"])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["kernelCreationTime100ns"] = "134000000000000001"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(QQCallError) as reused_pid:
                runtime._read_peer_map(DYNAMIC_META)
            self.assertEqual(reused_pid.exception.code, "peer_manifest_replaced")

            manifest["kernelCreationTime100ns"] = "134000000000000000"
            manifest["peerMap"] = {UID: PEER_UIN, "second-kernel-uid": PEER_UIN}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(QQCallError) as duplicate:
                runtime._read_peer_map(DYNAMIC_META)
            self.assertEqual(duplicate.exception.code, "peer_manifest_invalid")

            manifest["peerMap"] = {UID: PEER_UIN}
            manifest["schema"] = 1.0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(QQCallError) as schema:
                runtime._read_peer_map(DYNAMIC_META)
            self.assertEqual(schema.exception.code, "peer_manifest_stale")

    def test_busy_invite_targets_incoming_mapped_caller_while_core_is_down(self):
        async def scenario():
            state = _identity()
            state["core_ready"] = False
            sends = []
            polls = []

            async def transport(path, payload, _token):
                polls.append((path, payload))
                self.assertEqual(path, "/v1/adapter/poll")
                if payload["after"] == 0:
                    return _poll(
                        call=_call(),
                        cursor=1,
                        events=[{
                            "sequence": 1,
                            "kind": "invite_busy",
                            "call": None,
                            "busyInvite": {
                                "callerUid": "busy-caller",
                                "identityCorrelation": "kernel_from_uid_matched",
                            },
                        }],
                    )
                return _poll(call=_call(), cursor=1)

            async def send_busy(peer, text, preflight):
                self.assertTrue(await preflight())
                sends.append((peer, text))
                return True

            runtime, seen = _runtime(
                state=state,
                transport=transport,
                send_busy_private=send_busy,
                config=_enabled_config(peer_map={UID: PEER_UIN, "busy-caller": "111222333"}),
            )
            _prime_call(runtime)
            _bind_test_kernel(runtime)
            runtime._core_ready = False
            identity = await runtime._read_platform_state()
            event = {"busy_invite": {
                "caller_uid": "busy-caller",
                "peer_user_id": "111222333",
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(
                    scope_kind="busy", relation_id="busy-relation", from_uid="busy-caller",
                ),
            }}
            await runtime._poll_once(identity)
            await runtime._process_busy_event(event, runtime._active_call, identity, 1)
            await runtime._process_busy_event(event, runtime._active_call, identity, 2)

            self.assertEqual(sends, [("111222333", "当前正在通话，暂时无法接听，请稍后再试。")])
            self.assertEqual(len(polls), 2)
            self.assertEqual(polls[0][1]["after"], 0)
            self.assertEqual(polls[1][1]["after"], 1)
            self.assertEqual(seen["events"], [])
            self.assertEqual(seen["admissions"], ["111222333"] * 4)

        asyncio.run(scenario())

    def test_busy_private_blacklist_refusal_suppresses_control_and_dm(self):
        async def scenario():
            sends = []
            polls = []

            async def deny(_peer):
                return False

            async def transport(path, _payload, _token):
                polls.append(path)
                return {}

            async def send_busy(*args):
                sends.append(args)
                return True

            runtime, seen = _runtime(admit=deny, transport=transport, send_busy_private=send_busy)
            _prime_call(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            await runtime._process_busy_event({"busy_invite": {
                "caller_uid": UID,
                "peer_user_id": PEER_UIN,
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(scope_kind="busy"),
            }}, runtime._active_call, identity, 4)
            self.assertEqual(sends, [])
            self.assertEqual(polls, [])
            self.assertEqual(seen["admissions"], [PEER_UIN])

        asyncio.run(scenario())

    def test_core_down_keeps_adapter_identity_but_rejects_accept_and_voice_readiness(self):
        async def scenario():
            state = _identity()
            state["core_ready"] = False

            async def transport(path, _payload, _token):
                self.fail(f"native mutation must not reach Control while Core is down: {path}")

            runtime, seen = _runtime(state=state, transport=transport)
            _prime_call(runtime)
            runtime._core_ready = False
            identity = await runtime._read_platform_state()
            request = QQCallRuntime._request_envelope(_request("accept", dict(SCOPE)), "qq")
            response = await runtime._process_request(request, identity)

            self.assertEqual(response["error"]["code"], "core_not_ready")
            self.assertTrue(runtime.status()["accountVerified"])
            self.assertFalse(runtime.status()["coreReady"])
            self.assertFalse(runtime.status()["audio"]["ready"])
            self.assertEqual(seen["admissions"], [])

        asyncio.run(scenario())

    def test_busy_invite_suppresses_if_slot_is_cleared_before_send(self):
        async def scenario():
            state = _identity()
            send_attempts = []
            admission_count = 0

            async def transport(path, _payload, _token):
                if path == "/v1/adapter/poll":
                    return _poll(call=None, cursor=1)
                self.fail(f"unexpected endpoint: {path}")

            async def admit(_peer):
                nonlocal admission_count
                admission_count += 1
                return admission_count < 3

            async def send_busy(_peer, _text, preflight):
                if await preflight():
                    send_attempts.append(True)
                    return True
                return False

            runtime, _seen = _runtime(
                state=state,
                transport=transport,
                admit=admit,
                send_busy_private=send_busy,
            )
            _prime_call(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            event = {"busy_invite": {
                "caller_uid": UID,
                "peer_user_id": PEER_UIN,
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(scope_kind="busy"),
            }}
            await runtime._process_busy_event(event, runtime._active_call, identity, 1)
            self.assertEqual(send_attempts, [])
            self.assertEqual(admission_count, 3)

        asyncio.run(scenario())

    def test_busy_invite_rechecks_private_admission_after_fresh_control_poll(self):
        async def scenario():
            admission_count = 0
            send_attempts = []

            async def admit(_peer):
                nonlocal admission_count
                admission_count += 1
                return admission_count < 3

            async def transport(path, _payload, _token):
                self.assertEqual(path, "/v1/adapter/poll")
                return _poll(call=_call(), cursor=1)

            async def send_busy(_peer, _text, preflight):
                if await preflight():
                    send_attempts.append(True)
                    return True
                return False

            runtime, _ = _runtime(
                transport=transport,
                admit=admit,
                send_busy_private=send_busy,
            )
            _prime_call(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            await runtime._process_busy_event({"busy_invite": {
                "caller_uid": UID,
                "peer_user_id": PEER_UIN,
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(scope_kind="busy"),
            }}, runtime._active_call, identity, 1)
            self.assertEqual(admission_count, 4)
            self.assertEqual(send_attempts, [])

        asyncio.run(scenario())

    def test_group_slot_busy_invite_sends_one_private_busy_notice_under_lease(self):
        async def scenario():
            sends = []
            occupancy_requests = []

            async def transport(path, payload, _token):
                occupancy_requests.append((path, dict(payload)))
                self.assertEqual(path, "/v1/call/occupancy")
                self.assertEqual(payload, {"accountId": ACCOUNT, "clientInstance": "qtestclient"})
                return {"version": 1, "accountId": ACCOUNT, **META, "occupied": True, "kind": "group"}

            async def send_busy(peer, text, preflight):
                if await preflight():
                    sends.append((peer, text))
                    return True
                return False

            config = _enabled_config(peer_map={UID: PEER_UIN, "busy-caller": "111222333"})
            runtime, _seen = _runtime(transport=transport, send_busy_private=send_busy, config=config)
            _prime_idle(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            event = {"busy_invite": {
                "caller_uid": "busy-caller",
                "peer_user_id": "111222333",
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(
                    scope_kind="busy", relation_id="busy-relation", from_uid="busy-caller",
                ),
            }}
            await runtime._process_busy_event(event, None, identity, 1)
            await runtime._process_busy_event(event, None, identity, 1)

            self.assertEqual(sends, [("111222333", "当前正在通话，暂时无法接听，请稍后再试。")])
            self.assertEqual(len(occupancy_requests), 3)

        asyncio.run(scenario())

    def test_group_slot_busy_notice_fails_closed_on_occupancy_acl_or_proof_loss(self):
        async def scenario():
            for response in (
                {"version": 1, "accountId": ACCOUNT, **META, "occupied": False, "kind": None},
                {"version": 1, "accountId": ACCOUNT, **META, "occupied": True, "kind": "private"},
                {"version": 1, "accountId": ACCOUNT, **META, "occupied": True, "kind": "group", "extra": True},
                {"version": 1, "accountId": ACCOUNT, **{**META, "hostGeneration": 8}, "occupied": True, "kind": "group"},
            ):
                sends = []

                async def transport(path, _payload, _token):
                    self.assertEqual(path, "/v1/call/occupancy")
                    return response

                async def send_busy(peer, text, preflight):
                    if await preflight():
                        sends.append((peer, text))
                        return True
                    return False

                runtime, _seen = _runtime(
                    transport=transport,
                    send_busy_private=send_busy,
                    config=_enabled_config(peer_map={UID: PEER_UIN, "busy-caller": "111222333"}),
                )
                _prime_idle(runtime)
                _bind_test_kernel(runtime)
                identity = await runtime._read_platform_state()
                await runtime._process_busy_event({"busy_invite": {
                    "caller_uid": "busy-caller",
                    "peer_user_id": "111222333",
                    "identity_correlation": "kernel_from_uid_matched",
                    "private_scope_proof": _private_scope_proof(
                        scope_kind="busy", relation_id="busy-relation", from_uid="busy-caller",
                    ),
                }}, None, identity, 1)
                self.assertEqual(sends, [])

            sends = []
            admission_count = 0

            async def admit(_peer):
                nonlocal admission_count
                admission_count += 1
                return admission_count < 2

            async def occupied_transport(path, _payload, _token):
                self.assertEqual(path, "/v1/call/occupancy")
                return {"version": 1, "accountId": ACCOUNT, **META, "occupied": True, "kind": "group"}

            async def send_busy(peer, text, preflight):
                if await preflight():
                    sends.append((peer, text))
                    return True
                return False

            runtime, _seen = _runtime(
                transport=occupied_transport,
                admit=admit,
                send_busy_private=send_busy,
                config=_enabled_config(peer_map={UID: PEER_UIN, "busy-caller": "111222333"}),
            )
            _prime_idle(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            await runtime._process_busy_event({"busy_invite": {
                "caller_uid": "busy-caller",
                "peer_user_id": "111222333",
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": _private_scope_proof(
                    scope_kind="busy", relation_id="busy-relation", from_uid="busy-caller",
                ),
            }}, None, identity, 1)
            self.assertEqual(sends, [])

            sends = []
            runtime, _seen = _runtime(
                transport=occupied_transport,
                send_busy_private=send_busy,
                config=_enabled_config(peer_map={UID: PEER_UIN, "busy-caller": "111222333"}),
            )
            _prime_idle(runtime)
            _bind_test_kernel(runtime)
            identity = await runtime._read_platform_state()
            invalid = _private_scope_proof(
                scope_kind="busy", relation_id="busy-relation", from_uid="wrong-caller",
            )
            await runtime._process_busy_event({"busy_invite": {
                "caller_uid": "busy-caller",
                "peer_user_id": "111222333",
                "identity_correlation": "kernel_from_uid_matched",
                "private_scope_proof": invalid,
            }}, None, identity, 1)
            self.assertEqual(sends, [])

        asyncio.run(scenario())

    def test_busy_invite_rejects_unknown_uid_bad_proof_and_replaced_socket(self):
        runtime, _seen = _runtime()
        raw = _poll(
            call=_call(),
            cursor=1,
            events=[{
                "sequence": 1,
                "kind": "invite_busy",
                "call": None,
                "busyInvite": {"callerUid": "unmapped", "identityCorrelation": "kernel_from_uid_matched"},
            }],
        )
        _, events = runtime._validate_poll(raw)
        self.assertIsNone(events[0]["busy_invite"]["peer_user_id"])
        raw["events"][0]["busyInvite"]["identityCorrelation"] = "awaiting_20006"
        with self.assertRaises(QQCallError):
            runtime._validate_poll(raw)

        async def scenario():
            state = _identity()
            sent = []

            async def replace_identity(_peer):
                state["identity"] = (object(), 5, ACCOUNT)
                return True

            async def transport(path, _payload, _token):
                return _poll(call=_call(), cursor=1)

            async def send_busy(*args):
                sent.append(args)
                return True

            replaced, _seen = _runtime(
                state=state,
                admit=replace_identity,
                transport=transport,
                send_busy_private=send_busy,
            )
            _prime_call(replaced)
            _bind_test_kernel(replaced)
            identity = await replaced._read_platform_state()
            await replaced._process_busy_event({"busy_invite": {
                "caller_uid": UID,
                "peer_user_id": PEER_UIN,
                "identity_correlation": "kernel_relation_uid_matched",
                "private_scope_proof": _private_scope_proof(
                    scope_kind="busy", relation_id=PEER_UIN, from_uid="",
                ),
            }}, replaced._active_call, identity, 1)
            self.assertEqual(sent, [])

        asyncio.run(scenario())

    def test_group_join_requires_explicit_trigger_admission_and_unverified_room_refuses(self):
        async def scenario():
            group_admissions = []
            endpoints = []

            async def admit_group(peer, group):
                group_admissions.append((peer, group))
                return peer == PEER_UIN and group == "246813579"

            async def transport(path, payload, _token):
                endpoints.append((path, payload))
                if path == "/v1/group/status":
                    return {
                        "version": 1,
                        "accountId": ACCOUNT,
                        **META,
                        "groupId": "246813579",
                        "occupied": False,
                        "nativeJoinVerified": False,
                        "room": None,
                    }
                self.fail(f"unexpected endpoint: {path}")

            runtime, _seen = _runtime(
                transport=transport,
                admit_group=admit_group,
            )
            _prime_idle(runtime)
            state = await runtime._read_platform_state()
            valid = QQCallRuntime._request_envelope(_group_request(trigger_text="请加入群语音！"), "qq")
            self.assertIsNotNone(valid)
            malformed = _group_request(trigger_text="请不要加入群语音")
            self.assertIsNone(QQCallRuntime._request_envelope(malformed, "qq"))
            response = await runtime._process_request(valid, state)
            self.assertEqual(response["error"]["code"], "unsupported_unverified_group_join")
            self.assertEqual(group_admissions, [(PEER_UIN, "246813579")])
            self.assertEqual(endpoints[0][0], "/v1/group/status")
            self.assertEqual(endpoints[0][1], {
                "accountId": ACCOUNT,
                "clientInstance": runtime._client_instance,
                "groupId": "246813579",
            })

        asyncio.run(scenario())

    def test_group_join_refuses_denied_member_or_global_call_slot(self):
        async def scenario():
            status_calls = []

            async def transport(path, _payload, _token):
                status_calls.append(path)
                return {
                    "version": 1,
                    "accountId": ACCOUNT,
                    **META,
                    "groupId": "246813579",
                    "occupied": True,
                    "nativeJoinVerified": False,
                    "room": None,
                }

            denied, _ = _runtime(admit_group=lambda _peer, _group: False, transport=transport)
            _prime_idle(denied)
            state = await denied._read_platform_state()
            parsed = QQCallRuntime._request_envelope(_group_request(), "qq")
            response = await denied._process_request(parsed, state)
            self.assertEqual(response["error"]["code"], "group_not_admitted")
            self.assertEqual(status_calls, [])

            allowed, _ = _runtime(admit_group=lambda _peer, _group: True, transport=transport)
            _prime_idle(allowed)
            state = await allowed._read_platform_state()
            parsed = QQCallRuntime._request_envelope(_group_request(), "qq")
            response = await allowed._process_request(parsed, state)
            self.assertEqual(response["error"]["code"], "call_occupied")
            self.assertEqual(status_calls, ["/v1/group/status"])

        asyncio.run(scenario())

    def test_core_recovery_emits_deferred_old_terminal_before_new_current_call(self):
        async def scenario():
            state = _identity()
            state["core_ready"] = False
            terminal = _call(
                callId="call-old",
                generation=10,
                phase="terminal",
                inviteCallbackReady=False,
                acceptState="accepted",
            )
            current = _call(
                callId="call-new",
                generation=12,
                phase="connected",
                inviteCallbackReady=False,
                acceptState="accepted",
            )

            async def transport(path, payload, _token):
                self.assertEqual(path, "/v1/adapter/poll")
                if payload["after"] == 0:
                    return _poll(
                        call=current,
                        cursor=2,
                        events=[
                            {"sequence": 1, "kind": "call_terminal", "call": terminal},
                            {"sequence": 2, "kind": "call_connected", "call": current},
                        ],
                    )
                return _poll(call=current, cursor=2)

            runtime, seen = _runtime(transport=transport, state=state)
            _prime_idle(runtime)
            offline_identity = await runtime._read_platform_state()
            await runtime._poll_once(offline_identity)
            self.assertEqual(seen["events"], [])
            self.assertEqual(len(runtime._deferred_terminal_events), 1)
            self.assertEqual(runtime._active_call["callId"], "call-new")

            state["core_ready"] = True
            online_identity = await runtime._read_platform_state()
            await runtime._poll_once(online_identity)

            self.assertEqual(
                [message.message_info.message_id for message in seen["events"]],
                [
                    "qqcall:call-old:10:call_terminal",
                    "qqcall:call-new:12:call_connected",
                ],
            )
            self.assertEqual(len(runtime._deferred_terminal_events), 0)
            self.assertEqual(runtime._active_call["callId"], "call-new")

        asyncio.run(scenario())

    def test_unclassified_auto_accept_never_reaches_config_or_native_command(self):
        async def scenario():
            commands = []

            async def transport(path, payload, _token):
                commands.append((path, payload))
                scope = dict(payload.get("scope", {}))
                return {
                    "version": 1,
                    "accountId": ACCOUNT,
                    "serviceInstance": DYNAMIC_META["serviceInstance"],
                    "operation": "accept",
                    "scope": scope,
                    "result": {
                        "posted": True,
                        "id": 77,
                        "callId": scope.get("callId"),
                        "callGeneration": scope.get("callGeneration"),
                    },
                }

            # Shared policy enablement is not enough to prove a private invite.
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, _seen, state, _public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport,
                    )
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(runtime._last_failure_code, "private_scope_unverified")

            # Re-read shared policy after async admission even for a proved invite.
            with tempfile.TemporaryDirectory() as directory:
                public_path: Path | None = None

                def disable_plugin(_peer):
                    value = toml.load(public_path)
                    value["plugin"]["enabled"] = False
                    public_path.write_text(toml.dumps(value), encoding="utf-8")
                    return True

                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, _seen, state, public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport, admit=disable_plugin, with_proof=True,
                    )
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(runtime._last_failure_code, "auto_accept_refused")

            # A same-generation mapping replacement is not allowed to inherit
            # the old caller binding after the admission callback yields.
            with tempfile.TemporaryDirectory() as directory:
                manifest_path: Path | None = None

                def replace_mapping(_peer):
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    value["peerMap"] = {UID: "111222333"}
                    manifest_path.write_text(json.dumps(value), encoding="utf-8")
                    return True

                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, _seen, state, _public_path, manifest_path = _auto_accept_runtime(
                        Path(directory), transport=transport, admit=replace_mapping, with_proof=True,
                    )
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(runtime._last_failure_code, "auto_accept_refused")

            # A replacement OneBot socket/account is checked by the existing
            # identity fence before any command reaches the supervisor.
            with tempfile.TemporaryDirectory() as directory:
                state_ref: dict[str, Any] | None = None

                def replace_socket(_peer):
                    state_ref["identity"] = (object(), 5, "111222333")
                    state_ref["account_id"] = "111222333"
                    return True

                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, _seen, state, _public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport, admit=replace_socket, with_proof=True,
                    )
                    state_ref = state
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(runtime._last_failure_code, "auto_accept_refused")

            # A same-scope replacement cannot inherit the old private proof.
            with tempfile.TemporaryDirectory() as directory:
                runtime_ref: dict[str, QQCallRuntime] = {}

                def replace_proof(_peer):
                    current_call = dict(runtime_ref["runtime"]._active_call)
                    current_call["private_scope_proof"] = None
                    runtime_ref["runtime"]._active_call = current_call
                    return True

                with patch.object(qqcall_service_module, "_SERVICE_PROJECT_ROOT", Path(directory) / "QQCallProject"):
                    runtime, _seen, state, _public_path, _manifest = _auto_accept_runtime(
                        Path(directory), transport=transport, admit=replace_proof, with_proof=True,
                    )
                    runtime_ref["runtime"] = runtime
                    original_scope = dict(runtime._active_call["scope"])
                    await runtime._maybe_auto_accept(runtime._active_call, state)
                    self.assertEqual(runtime._active_call["scope"], original_scope)
                    self.assertIsNone(runtime._active_call["private_scope_proof"])
                    self.assertEqual(runtime._last_failure_code, "auto_accept_refused")
                    self.assertEqual(commands, [])

            self.assertEqual(commands, [])

        asyncio.run(scenario())

    def test_call_target_gate_accepts_only_trusted_correlations(self):
        for correlation, expected_error in (
            ("kernel_from_uid_matched", "audio_disabled"),
            ("kernel_relation_uid_matched", "audio_disabled"),
            ("single_active_invite_uncorrelated", "stale_call_scope"),
        ):
            with self.subTest(correlation=correlation):
                runtime, seen = _runtime()
                _prime_call(runtime, _call(correlation=correlation, phase="connected"))
                raw = {
                    "additional_config": {
                        "delivery_target": {
                            "kind": "qqcall",
                            **SCOPE,
                            "peer_user_id": PEER_UIN,
                        }
                    }
                }
                self.assertTrue(asyncio.run(runtime.intercept_outbound(raw)))
                self.assertEqual(seen["custom"][-1][1]["error"]["code"], expected_error)

    def test_scoped_status_identity_and_admission_fail_closed(self):
        bool_scope = {**SCOPE, "hostGeneration": True}
        int_scope = {**SCOPE, "hostGeneration": 1}
        self.assertFalse(QQCallRuntime._scope_matches(bool_scope, int_scope))
        commands = []

        async def transport(path, payload, token):
            commands.append(path)
            return {"version": 1}

        runtime, _ = _runtime(transport=transport, admit=lambda _peer: False)
        _prime_call(runtime)
        stale = asyncio.run(runtime.handle_request(_request("status", {**SCOPE, "callGeneration": 99})))
        self.assertEqual(stale["error"]["code"], "call_scope_stale")
        denied = asyncio.run(runtime.handle_request(_request("accept", dict(SCOPE), "request_id_abcdefghijkl")))
        self.assertEqual(denied["error"]["code"], "peer_not_admitted")
        self.assertEqual(commands, [])

        wrong_state = _identity(account="111111111")
        runtime2, _ = _runtime(transport=transport, state=wrong_state)
        _prime_call(runtime2)
        mismatch = asyncio.run(runtime2.handle_request(_request("accept", dict(SCOPE), "request_id_abcdefghijklm")))
        self.assertEqual(mismatch["error"]["code"], "account_mismatch")

    def test_stop_during_admission_prevents_command_dispatch(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = []

            async def admission(_peer):
                entered.set()
                await release.wait()
                return True

            async def transport(path, payload, token):
                calls.append(path)
                return {}

            runtime, _ = _runtime(transport=transport, admit=admission)
            _prime_call(runtime)
            runtime._lease_claimed = False
            request_task = asyncio.create_task(runtime.handle_request(_request("accept", dict(SCOPE))))
            await entered.wait()
            stopped = await runtime.stop()
            self.assertTrue(stopped["closing"])
            release.set()
            response = await request_task
            self.assertEqual(response["error"]["code"], "runtime_stopped")
            self.assertEqual(calls, [])

        asyncio.run(scenario())

    def test_stop_during_poll_does_not_emit_late_event_and_releases_lease(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                poll_started = asyncio.Event()
                release_poll = asyncio.Event()
                paths = []

                async def transport(path, payload, token):
                    paths.append(path)
                    if path == "/v1/adapter/poll":
                        poll_started.set()
                        await release_poll.wait()
                        return _poll(call=_call(), cursor=1)
                    if path == "/v1/adapter/release":
                        return {
                            "version": 1,
                            "accountId": ACCOUNT,
                            "serviceInstance": META["serviceInstance"],
                            "released": True,
                        }
                    raise AssertionError(path)

                token_file = Path(temp_dir) / "runtime.token"
                token_file.write_text(TOKEN, encoding="utf-8")
                runtime, seen = _runtime(transport=transport, config=_enabled_config(str(token_file), poll_interval_sec=0.1))
                runtime._token = None
                run_task = asyncio.create_task(runtime.run())
                await poll_started.wait()
                stop_task = asyncio.create_task(runtime.stop())
                await asyncio.sleep(0)
                self.assertTrue(runtime.status()["closing"])
                release_poll.set()
                await stop_task
                await run_task
                self.assertEqual(seen["events"], [])
                self.assertEqual(paths, ["/v1/adapter/poll", "/v1/adapter/release"])
                self.assertFalse(runtime.status()["leaseClaimed"])

        asyncio.run(scenario())

    def test_call_targeted_outbound_is_consumed_and_reports_no_audio_path(self):
        runtime, seen = _runtime()
        raw = {"message_info": {"additional_config": {"delivery_target": {"kind": "qqcall", **SCOPE, "peer_user_id": PEER_UIN}}}}
        self.assertTrue(asyncio.run(runtime.intercept_outbound(raw)))
        self.assertEqual(runtime.status()["lastFailureCode"], "stale_call_scope")
        self.assertEqual(seen["custom"][-1][0], "qqcall_delivery_response")
        self.assertEqual(seen["custom"][-1][1]["error"]["code"], "stale_call_scope")
        self.assertTrue(asyncio.run(runtime.intercept_outbound({"additional_config": {"delivery_target": {"kind": "qqcall"}}})))
        self.assertFalse(asyncio.run(runtime.intercept_outbound({"additional_config": {}})))

    def test_custom_response_timeout_releases_request_and_reports_safe_failure(self):
        async def scenario():
            entered = asyncio.Event()
            cancelled = asyncio.Event()
            runtime, _ = _runtime()
            _prime_call(runtime)

            async def blocked_custom_send(_kind, _message):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            runtime._send_custom = blocked_custom_send
            raw = _request("status", dict(SCOPE))
            with patch.object(qqcall_module, "_CORE_SEND_TIMEOUT_SEC", 0.02):
                response = await asyncio.wait_for(runtime.handle_request(raw), timeout=0.5)

            self.assertIsNone(response)
            self.assertTrue(entered.is_set())
            self.assertTrue(cancelled.is_set())
            self.assertEqual(runtime.status()["counts"]["inflightRequests"], 0)
            self.assertEqual(runtime.status()["counts"]["cachedRequests"], 1)
            self.assertEqual(runtime.status()["lastFailureCode"], "core_response_send_failed")

        asyncio.run(scenario())

    def test_call_target_diagnostic_timeout_stays_consumed_with_original_failure(self):
        async def scenario():
            entered = asyncio.Event()
            cancelled = asyncio.Event()
            runtime, _ = _runtime(config=QQCallConfig())

            async def blocked_custom_send(_kind, _message):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            runtime._send_custom = blocked_custom_send
            raw = {"additional_config": {"delivery_target": {"kind": "qqcall"}}}
            with patch.object(qqcall_module, "_CORE_SEND_TIMEOUT_SEC", 0.02):
                consumed = await asyncio.wait_for(runtime.intercept_outbound(raw), timeout=0.5)

            self.assertTrue(consumed)
            self.assertTrue(entered.is_set())
            self.assertTrue(cancelled.is_set())
            self.assertEqual(runtime.status()["lastFailureCode"], "audio_unavailable")

        asyncio.run(scenario())

    def test_http_redirect_never_forwards_bearer_to_location(self):
        target_received = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                target_received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            async def send():
                await qqcall_module._http_post(
                    f"http://127.0.0.1:{redirect.server_port}", "/v1/adapter/poll", TOKEN, {"x": 1}
                )

            with self.assertRaises(QQCallError) as error:
                asyncio.run(send())
            self.assertEqual(error.exception.code, "service_redirect_rejected")
            self.assertEqual(target_received, [])
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join(timeout=2)
            target_thread.join(timeout=2)

    def test_http_total_timeout_covers_slow_drip_response_headers(self):
        class SlowHeaderHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.connection.sendall(b"HTTP/1.1 200 OK\r\n")
                time.sleep(0.08)
                self.connection.sendall(b"Content-Type: application/json\r\n")
                time.sleep(0.08)
                self.connection.sendall(b"Content-Length: 11\r\n")
                time.sleep(0.08)
                self.connection.sendall(b"Connection: close\r\n\r\n{\"ok\":true}")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaderHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        original_timeout = qqcall_module._HTTP_TOTAL_TIMEOUT_SEC
        qqcall_module._HTTP_TOTAL_TIMEOUT_SEC = 0.18
        started = time.monotonic()
        try:
            async def send():
                await qqcall_module._http_post(f"http://127.0.0.1:{server.server_port}", "/poll", TOKEN, {})

            with self.assertRaises(QQCallError) as error:
                asyncio.run(send())
            self.assertEqual(error.exception.code, "service_timeout")
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            qqcall_module._HTTP_TOTAL_TIMEOUT_SEC = original_timeout
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_group_status_http_waits_for_typed_native_timeout_but_poll_deadline_stays_short(self):
        class DelayedErrorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(0.03)
                body = b'{"ok":false,"code":"group_room_query_timeout"}'
                try:
                    self.send_response(504)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), DelayedErrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            with (patch.object(qqcall_module, "_GROUP_STATUS_HTTP_TIMEOUT_SEC", 0.2),
                  patch.object(qqcall_module, "_HTTP_TOTAL_TIMEOUT_SEC", 0.005)):
                with self.assertRaises(QQCallError) as native:
                    asyncio.run(qqcall_module._http_post(endpoint, "/v1/group/status", TOKEN, {}))
                self.assertEqual(native.exception.code, "group_room_query_timeout")
                self.assertEqual(native.exception.status_code, 504)
                with self.assertRaises(QQCallError) as poll:
                    asyncio.run(qqcall_module._http_post(endpoint, "/v1/adapter/poll", TOKEN, {}))
                self.assertEqual(poll.exception.code, "service_timeout")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_collects_delayed_chunks_and_rejects_oversized_aggregate(self):
        body = json.dumps({"ok": True, "message": "split response"}).encode("utf-8")

        def start_body_server(response_body: bytes, split: bool):
            class BodyHandler(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    try:
                        if split:
                            midpoint = len(response_body) // 2
                            self.wfile.write(response_body[:midpoint])
                            self.wfile.flush()
                            time.sleep(0.04)
                            self.wfile.write(response_body[midpoint:])
                        else:
                            self.wfile.write(response_body)
                    except OSError:
                        pass

                def log_message(self, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), BodyHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, thread

        normal_server, normal_thread = start_body_server(body, True)
        try:
            result = asyncio.run(qqcall_module._http_post(
                f"http://127.0.0.1:{normal_server.server_port}", "/poll", TOKEN, {}
            ))
            self.assertEqual(result, {"ok": True, "message": "split response"})
        finally:
            normal_server.shutdown()
            normal_server.server_close()
            normal_thread.join(timeout=2)

        too_large = b"x" * (qqcall_module._MAX_HTTP_BYTES + 1)
        large_server, large_thread = start_body_server(too_large, False)
        try:
            with self.assertRaises(QQCallError) as error:
                asyncio.run(qqcall_module._http_post(
                    f"http://127.0.0.1:{large_server.server_port}", "/poll", TOKEN, {}
                ))
            self.assertEqual(error.exception.code, "service_response_too_large")
        finally:
            large_server.shutdown()
            large_server.server_close()
            large_thread.join(timeout=2)

    def test_runtime_roundtrips_with_real_node_control_service(self):
        process, port = _start_real_control_service()

        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                events = []
                state = _identity()
                token_file = Path(temp_dir) / "control.token"
                token_file.write_text(TOKEN, encoding="utf-8")
                runtime = QQCallRuntime(
                    _enabled_config(str(token_file), endpoint=f"http://127.0.0.1:{port}", poll_interval_sec=0.1),
                    "qq",
                    lambda message: events.append(message) or True,
                    lambda _kind, _message: True,
                    lambda: state,
                    lambda _peer: True,
                    dependency_check=lambda: (),
                )
                run_task = asyncio.create_task(runtime.run())
                deadline = time.monotonic() + 5
                while not runtime.status()["activeCall"] and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertEqual(runtime.status()["activeCall"]["phase"], "invite_ready")
                self.assertEqual(len(events), 1)
                scope = dict(runtime._active_call["scope"])
                response = await runtime.handle_request(_request("accept", scope))
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["data"]["result"], {
                    "posted": True,
                    "id": 55,
                    "callId": scope["callId"],
                    "callGeneration": scope["callGeneration"],
                })
                await runtime.stop()
                await run_task
                self.assertFalse(runtime.status()["leaseClaimed"])

        try:
            asyncio.run(scenario())
        finally:
            if process.stdin:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                except OSError:
                    pass
                process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
