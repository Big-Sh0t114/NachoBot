"""Plugin-owned control client for the independent QQCall runtime.

This module transports explicit private call commands, bounded group status,
group-join intent, and an owner-bound group leave through the configured adapter
connection. It is not a general RPC API.
"""

from __future__ import annotations

import asyncio
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any

from qqcall_client.qqcall_policy import match_group_call_trigger
from qqcall_client.qqcall_group_wire import (
    validate_group_audio_claim,
    validate_group_audio_scope,
)
from src.common.message.api import get_global_api


QQCALL_REQUEST_TYPE = "qqcall_request"
QQCALL_RESPONSE_TYPE = "qqcall_response"
_PROTOCOL_VERSION = 1
_MAX_PENDING = 64
_GROUP_JOIN_REQUEST_TIMEOUT_SEC = 45.0
_DEFAULT_REQUEST_TIMEOUT_SEC = 8.0
_QQ_UIN_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_MAX_GROUP_TRIGGER_TEXT_CHARS = 128
_SERVICE_INSTANCE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_ENUM_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
_SAFE_ERROR_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
_SCOPE_KEYS = frozenset(
    {"serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration"}
)
_STATUS_CALL_KEYS = frozenset(
    {
        "callId",
        "generation",
        "phase",
        "uncertain",
        "inviteCallbackReady",
        "inviteCorrelation",
        "acceptState",
        "audioTurnId",
    }
)
_MUTATION_RESULT_KEYS = frozenset({"posted", "id", "callId", "callGeneration"})
_GROUP_STATUS_KEYS = frozenset(
    {"occupied", "nativeJoinVerified", "roomObserved", "diagnosticOnly", "membership", "invitation", "roomEndObservation"}
)
_GROUP_MEMBERSHIP_KEYS = frozenset(
    {"source", "onlineCount", "memberCount", "businessType", "nativeTimeout", "ttlSecondsRemaining"}
)
_GROUP_INVITATION_KEYS = frozenset(
    {"source", "totalInvitedMembers", "joinCount", "quitCount", "ttlSecondsRemaining"}
)
_GROUP_ROOM_END_KEYS = frozenset({"source", "nativeTimeout", "onlineCount", "ttlSecondsRemaining"})
_GROUP_JOIN_RESULT_KEYS = frozenset(
    {
        "version",
        "accountId",
        "serviceInstance",
        "hostGeneration",
        "connectionGeneration",
        "joined",
        "scope",
        "source",
        "requesterUserId",
    }
)
_SUPPORTED_OPERATIONS = frozenset(
    {"status", "accept", "hangup", "reject", "join_group", "leave_group", "group_status", "group_audio_status"}
)
_KNOWN_RESPONSE_ERRORS = frozenset(
    {
        "unsupported_operation",
        "unsupported_unverified_command_hangup",
        "unsupported_unverified_command_reject",
        "adapter_owned",
        "adapter_lease_required",
        "adapter_command_in_flight",
        "call_scope_stale",
        "call_not_current",
        "call_not_accepting",
        "invite_callback_not_ready",
        "accept_already_requested",
        "accept_post_not_confirmed",
        "login_not_ready",
        "service_busy",
        "service_stopped",
        "service_not_started",
        "command_failed",
        "bridge_status_unavailable",
        "invalid_operation",
        "invalid_call_scope",
        "unauthorized",
        "qqcall_disabled",
        "account_mismatch",
        "platform_identity_changed",
        "no_active_call",
        "peer_not_admitted",
        "peer_admission_failed",
        "call_uncertain",
        "runtime_not_ready",
        "runtime_busy",
        "runtime_stopped",
        "service_unavailable",
        "service_timeout",
        "group_room_query_timeout",
        "group_room_observed_empty",
        "group_room_query_quarantined",
        "group_room_query_busy",
        "group_room_query_post_failed",
        "group_room_metadata_invalid",
        "stale_generation",
        "invalid_peer_user_id",
        "invalid_platform",
        "invite_correlation_unavailable",
        "service_command_invalid",
        "duplicate_request_id",
        "request_outcome_unknown",
        "invalid_request",
        "platform_state_unavailable",
        "platform_state_invalid",
        "call_occupied",
        "unsupported_unverified_group_join",
        "group_room_unverified",
        "group_identity_mismatch",
        "group_join_unavailable",
        "group_not_admitted",
        "core_not_ready",
        "group_admission_failed",
        "group_status_failed",
        "group_status_invalid",
        "group_status_stale",
        "group_audio_status_invalid",
        "group_audio_status_stale",
        "invalid_group_audio_scope",
        "invalid_group_audio_claim",
        "group_join_failed",
        "group_join_response_invalid",
        "group_leave_unconfirmed",
        "group_not_joined",
        "group_owner_mismatch",
        "group_requester_mismatch",
        "group_scope_stale",
        "group_audio_unavailable",
        "group_audio_busy",
        "group_audio_route_unavailable",
        "group_audio_lease_lost",
        "audio_plan_unavailable",
        "audio_plan_replaced",
        "host_input_not_active",
        "host_output_not_active",
        "group_trigger_invalid",
        "group_id_invalid",
        "invalid_group_id",
        "invalid_group_request",
    }
)


class QQCallAPIError(RuntimeError):
    """A safe, typed failure from the QQCall Core-to-adapter boundary."""

    def __init__(self, code: str) -> None:
        safe_code = code if isinstance(code, str) and _SAFE_ERROR_RE.fullmatch(code) else "upstream_error"
        super().__init__(safe_code)
        self.code = safe_code


@dataclass(frozen=True)
class _PendingRequest:
    platform: str
    operation: str
    peer_user_id: str
    group_id: str | None
    scope: dict[str, Any] | None
    scope_group: dict[str, Any] | None
    audio_claim: dict[str, Any] | None
    future: asyncio.Future


_pending_requests: dict[str, _PendingRequest] = {}
_registered_api: Any = None


def _positive_safe_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**53 - 1


def validate_qq_peer_user_id(value: Any) -> str:
    """Validate and return a canonical QQ UIN string."""

    if not isinstance(value, str) or not _QQ_UIN_RE.fullmatch(value):
        raise QQCallAPIError("invalid_peer_user_id")
    return value


def validate_qq_group_id(value: Any) -> str:
    """Validate the current QQ group identifier as a canonical UIN string."""

    if not isinstance(value, str) or not _QQ_UIN_RE.fullmatch(value):
        raise QQCallAPIError("invalid_group_id")
    return value


# Public plugin-facing aliases for the shared group-audio wire contract.
validate_qqcall_group_scope = validate_group_audio_scope
validate_qqcall_group_audio_claim = validate_group_audio_claim


def validate_qqcall_scope(value: Any) -> dict[str, Any]:
    """Validate the exact opaque call scope required for a mutation."""

    if not isinstance(value, dict) or set(value) != _SCOPE_KEYS:
        raise QQCallAPIError("invalid_scope")
    service_instance = value.get("serviceInstance")
    call_id = value.get("callId")
    if (
        not isinstance(service_instance, str)
        or not _SERVICE_INSTANCE_RE.fullmatch(service_instance)
        or not _positive_safe_integer(value.get("hostGeneration"))
        or not _positive_safe_integer(value.get("connectionGeneration"))
        or not isinstance(call_id, str)
        or not _CALL_ID_RE.fullmatch(call_id)
        or not _positive_safe_integer(value.get("callGeneration"))
    ):
        raise QQCallAPIError("invalid_scope")
    return {
        "serviceInstance": service_instance,
        "hostGeneration": value["hostGeneration"],
        "connectionGeneration": value["connectionGeneration"],
        "callId": call_id,
        "callGeneration": value["callGeneration"],
    }


def _copy_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if scope is None else dict(scope)


def _copy_group_audio_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if scope is None else validate_qqcall_group_scope(scope)


def _copy_group_audio_claim(claim: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if claim is None else validate_qqcall_group_audio_claim(claim)


def _safe_enum(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if isinstance(value, str) and _ENUM_RE.fullmatch(value):
        return value
    raise QQCallAPIError("malformed_response")


def _safe_audio_turn_id(value: Any) -> str | None:
    """Validate the nullable opaque audio turn exposed by scoped status."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise QQCallAPIError("malformed_response")
    return value


def _validate_status_data(data: Any, peer_user_id: str) -> dict[str, Any] | None:
    if not isinstance(data, dict) or set(data) != {"scope", "call", "peer_user_id"}:
        raise QQCallAPIError("malformed_response")
    if data.get("peer_user_id") != peer_user_id:
        return None

    scope = validate_qqcall_scope(data.get("scope"))
    call = data.get("call")
    if not isinstance(call, dict) or set(call) != _STATUS_CALL_KEYS:
        raise QQCallAPIError("malformed_response")
    if (
        call.get("callId") != scope["callId"]
        or not _positive_safe_integer(call.get("generation"))
        or call.get("generation") != scope["callGeneration"]
        or not isinstance(call.get("uncertain"), bool)
        or not isinstance(call.get("inviteCallbackReady"), bool)
    ):
        raise QQCallAPIError("malformed_response")

    safe_call = {
        "callId": scope["callId"],
        "generation": scope["callGeneration"],
        "phase": _safe_enum(call.get("phase"), nullable=True),
        "uncertain": call["uncertain"],
        "inviteCallbackReady": call["inviteCallbackReady"],
        "inviteCorrelation": _safe_enum(call.get("inviteCorrelation"), nullable=True),
        "acceptState": _safe_enum(call.get("acceptState"), nullable=True),
        "audioTurnId": _safe_audio_turn_id(call.get("audioTurnId")),
    }
    return {"scope": scope, "call": safe_call, "peer_user_id": peer_user_id}


def _validate_mutation_data(data: Any, expected_scope: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict) or set(data) != {"scope", "result"}:
        raise QQCallAPIError("malformed_response")
    scope = validate_qqcall_scope(data.get("scope"))
    if scope != expected_scope:
        return None
    result = data.get("result")
    if not isinstance(result, dict) or set(result) != _MUTATION_RESULT_KEYS:
        raise QQCallAPIError("malformed_response")
    if (
        result.get("posted") is not True
        or not _positive_safe_integer(result.get("id"))
        or result.get("callId") != scope["callId"]
        or not _positive_safe_integer(result.get("callGeneration"))
        or result.get("callGeneration") != scope["callGeneration"]
    ):
        raise QQCallAPIError("malformed_response")
    return {
        "scope": scope,
        "result": {
            "posted": True,
            "id": result["id"],
            "callId": scope["callId"],
            "callGeneration": scope["callGeneration"],
        },
    }


def _nonnegative_bounded_integer(value: Any, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _valid_group_ttl(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 30


def _validate_group_status_item(value: Any, keys: frozenset[str], source: str) -> bool:
    return type(value) is dict and set(value) == keys and value.get("source") == source and _valid_group_ttl(
        value.get("ttlSecondsRemaining")
    )


def _validate_group_status_data(data: Any) -> dict[str, Any]:
    """Validate and retain only bounded group diagnostics safe for a chat reply."""

    if type(data) is not dict or set(data) != _GROUP_STATUS_KEYS:
        raise QQCallAPIError("group_status_invalid")
    if (
        type(data.get("occupied")) is not bool
        or type(data.get("nativeJoinVerified")) is not bool
        or type(data.get("roomObserved")) is not bool
        or data.get("roomObserved") is True and data.get("nativeJoinVerified") is not True
        or data.get("diagnosticOnly") is not True
    ):
        raise QQCallAPIError("group_status_invalid")

    membership = data.get("membership")
    if membership is not None:
        if (
            not _validate_group_status_item(membership, _GROUP_MEMBERSHIP_KEYS, "group_audio_member_count")
            and not _validate_group_status_item(membership, _GROUP_MEMBERSHIP_KEYS, "group_video_member_count")
        ):
            raise QQCallAPIError("group_status_invalid")
        if (
            not _nonnegative_bounded_integer(membership.get("onlineCount"), 1_000_000)
            or membership.get("memberCount") is not None
            and not _nonnegative_bounded_integer(membership.get("memberCount"), 1_000_000)
            or membership.get("businessType") is not None
            and not _nonnegative_bounded_integer(membership.get("businessType"), 0x7FFFFFFF)
            or membership.get("nativeTimeout") is not None
            and type(membership.get("nativeTimeout")) is not bool
        ):
            raise QQCallAPIError("group_status_invalid")

    invitation = data.get("invitation")
    if invitation is not None:
        if not _validate_group_status_item(invitation, _GROUP_INVITATION_KEYS, "group_video_invite_member_update") or (
            not _nonnegative_bounded_integer(invitation.get("totalInvitedMembers"), 1_000_000)
            or not _nonnegative_bounded_integer(invitation.get("joinCount"), 8192)
            or not _nonnegative_bounded_integer(invitation.get("quitCount"), 8192)
        ):
            raise QQCallAPIError("group_status_invalid")

    room_end = data.get("roomEndObservation")
    if room_end is not None:
        if (
            not _validate_group_status_item(room_end, _GROUP_ROOM_END_KEYS, "native_timeout_zero_members")
            or room_end.get("nativeTimeout") is not True
            or room_end.get("onlineCount") != 0
            or type(room_end.get("onlineCount")) is not int
        ):
            raise QQCallAPIError("group_status_invalid")

    return {
        "occupied": data["occupied"],
        "nativeJoinVerified": data["nativeJoinVerified"],
        "roomObserved": data["roomObserved"],
        "diagnosticOnly": True,
        "membership": dict(membership) if membership is not None else None,
        "invitation": dict(invitation) if invitation is not None else None,
        "roomEndObservation": dict(room_end) if room_end is not None else None,
    }


def _validate_group_audio_status_data(data: Any) -> dict[str, bool]:
    if type(data) is not dict or set(data) != {"active"} or type(data.get("active")) is not bool:
        raise QQCallAPIError("group_audio_status_invalid")
    return {"active": data["active"]}


def _validate_group_leave_data(data: Any) -> dict[str, bool]:
    if type(data) is not dict or set(data) != {"left"} or data.get("left") is not True:
        raise QQCallAPIError("group_leave_unconfirmed")
    return {"left": True}


def _validate_group_join_data(data: Any, pending: _PendingRequest) -> dict[str, Any]:
    """Validate affirmative evidence that the native group join completed."""

    if type(data) is not dict or set(data) != _GROUP_JOIN_RESULT_KEYS:
        raise QQCallAPIError("group_join_response_invalid")
    try:
        scope = validate_qqcall_group_scope(data.get("scope"))
    except ValueError:
        raise QQCallAPIError("group_join_response_invalid") from None
    service_instance = data.get("serviceInstance")
    host_generation = data.get("hostGeneration")
    connection_generation = data.get("connectionGeneration")
    if (
        type(data.get("version")) is not int
        or data["version"] != _PROTOCOL_VERSION
        or type(data.get("joined")) is not bool
        or data["joined"] is not True
        or data.get("source") != "native_scene3_relation"
        or not isinstance(data.get("accountId"), str)
        or not _QQ_UIN_RE.fullmatch(data["accountId"])
        or not isinstance(data.get("requesterUserId"), str)
        or data["requesterUserId"] != pending.peer_user_id
        or not isinstance(service_instance, str)
        or not _SERVICE_INSTANCE_RE.fullmatch(service_instance)
        or not _positive_safe_integer(host_generation)
        or not _positive_safe_integer(connection_generation)
        or scope["groupId"] != pending.group_id
        or scope["serviceInstance"] != service_instance
        or scope["hostGeneration"] != host_generation
        or scope["connectionGeneration"] != connection_generation
    ):
        raise QQCallAPIError("group_join_response_invalid")
    return {
        "version": _PROTOCOL_VERSION,
        "accountId": data["accountId"],
        "serviceInstance": service_instance,
        "hostGeneration": host_generation,
        "connectionGeneration": connection_generation,
        "joined": True,
        "scope": scope,
        "source": "native_scene3_relation",
        "requesterUserId": pending.peer_user_id,
    }


def _error_code(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ERROR_RE.fullmatch(value):
        return "upstream_error"
    from qqcall_client.qqcall_dependencies import dependency_notice
    return value if value in _KNOWN_RESPONSE_ERRORS or dependency_notice(value) is not None else "upstream_error"


def _ensure_registered(api: Any) -> None:
    global _registered_api
    if api is _registered_api:
        return
    try:
        api.register_custom_message_handler(QQCALL_RESPONSE_TYPE, handle_qqcall_response)
    except Exception:
        raise QQCallAPIError("handler_registration_failed") from None
    _registered_api = api


async def handle_qqcall_response(raw_message: Any) -> None:
    """Resolve a waiter only when the complete response is request-bound."""

    if not isinstance(raw_message, dict):
        return
    outer_platform = raw_message.get("platform")
    envelope = raw_message.get("content")
    if not isinstance(envelope, dict):
        return
    request_id = envelope.get("request_id")
    if not isinstance(request_id, str):
        return
    pending = _pending_requests.get(request_id)
    if pending is None or outer_platform != pending.platform:
        return
    # Both copies are required. A missing field is not an acceptable default.
    version = envelope.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _PROTOCOL_VERSION
        or envelope.get("platform") != pending.platform
    ):
        return
    if envelope.get("operation") != pending.operation or envelope.get("peer_user_id") != pending.peer_user_id:
        return
    if pending.operation in {"join_group", "leave_group", "group_status", "group_audio_status"}:
        if envelope.get("group_id") != pending.group_id:
            return
    elif "group_id" in envelope:
        return
    if pending.operation == "leave_group":
        if envelope.get("requester_user_id") != pending.peer_user_id:
            return
    elif "requester_user_id" in envelope:
        return
    if pending.operation == "group_audio_status":
        try:
            echoed_scope = validate_qqcall_group_scope(envelope.get("scope_group"))
            echoed_claim = validate_qqcall_group_audio_claim(envelope.get("audio_claim"))
        except ValueError:
            return
        if echoed_scope != pending.scope_group or echoed_claim != pending.audio_claim:
            return
    elif "scope_group" in envelope or "audio_claim" in envelope:
        return
    if pending.future.done():
        return

    status = envelope.get("status")
    if status == "error":
        error = envelope.get("error")
        code = _error_code(error.get("code") if isinstance(error, dict) else None)
        _pending_requests.pop(request_id, None)
        pending.future.set_exception(QQCallAPIError(code))
        return
    if status != "ok":
        return

    try:
        if pending.operation == "join_group":
            result = _validate_group_join_data(envelope.get("data"), pending)
        elif pending.operation == "group_status":
            result = _validate_group_status_data(envelope.get("data"))
        elif pending.operation == "group_audio_status":
            result = _validate_group_audio_status_data(envelope.get("data"))
        elif pending.operation == "leave_group":
            result = _validate_group_leave_data(envelope.get("data"))
        elif pending.operation == "status":
            result = _validate_status_data(envelope.get("data"), pending.peer_user_id)
            # A response for a different peer remains uncorrelated until timeout.
            if result is None:
                return
            if pending.scope is not None and result["scope"] != pending.scope:
                return
        else:
            if pending.scope is None:
                return
            result = _validate_mutation_data(envelope.get("data"), pending.scope)
            if result is None:
                return
    except QQCallAPIError as exc:
        _pending_requests.pop(request_id, None)
        pending.future.set_exception(exc)
        return

    _pending_requests.pop(request_id, None)
    pending.future.set_result(result)


async def request_qqcall(
    operation: str,
    *,
    platform: str = "qq",
    peer_user_id: str,
    group_id: str | None = None,
    requester_user_id: str | None = None,
    trigger_text: str | None = None,
    scope: dict[str, Any] | None = None,
    scope_group: dict[str, Any] | None = None,
    audio_claim: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Request private controls, group diagnostics, group join, or owned group leave."""

    if not isinstance(operation, str) or operation not in _SUPPORTED_OPERATIONS:
        raise QQCallAPIError("invalid_operation")
    if platform != "qq":
        raise QQCallAPIError("invalid_platform")
    peer_user_id = validate_qq_peer_user_id(peer_user_id)
    scope_group_value: dict[str, Any] | None = None
    audio_claim_value: dict[str, Any] | None = None
    if operation in {"join_group", "leave_group", "group_status", "group_audio_status"}:
        group_id = validate_qq_group_id(group_id)
        if scope is not None:
            raise QQCallAPIError("invalid_scope")
        if operation == "leave_group":
            if (
                requester_user_id is None
                or validate_qq_peer_user_id(requester_user_id) != peer_user_id
                or trigger_text is not None
                or scope_group is not None
                or audio_claim is not None
            ):
                raise QQCallAPIError("invalid_group_request")
        elif requester_user_id is not None:
            raise QQCallAPIError("invalid_group_request")
        if operation == "join_group":
            if scope_group is not None or audio_claim is not None:
                raise QQCallAPIError("invalid_group_request")
            if (
                not isinstance(trigger_text, str)
                or not trigger_text.strip()
                or len(trigger_text) > _MAX_GROUP_TRIGGER_TEXT_CHARS
                or "\x00" in trigger_text
                or "\r" in trigger_text
                or "\n" in trigger_text
                or match_group_call_trigger(trigger_text) is None
            ):
                raise QQCallAPIError("invalid_group_request")
        elif operation == "group_status":
            if trigger_text is not None or scope_group is not None or audio_claim is not None:
                raise QQCallAPIError("invalid_group_request")
        elif operation == "group_audio_status":
            if trigger_text is not None:
                raise QQCallAPIError("invalid_group_request")
            try:
                scope_group_value = validate_qqcall_group_scope(scope_group)
            except ValueError:
                raise QQCallAPIError("invalid_group_audio_scope") from None
            try:
                audio_claim_value = validate_qqcall_group_audio_claim(audio_claim)
            except ValueError:
                raise QQCallAPIError("invalid_group_audio_claim") from None
            if scope_group_value["groupId"] != group_id:
                raise QQCallAPIError("invalid_group_audio_scope")
    else:
        if (
            group_id is not None
            or trigger_text is not None
            or scope_group is not None
            or audio_claim is not None
            or requester_user_id is not None
        ):
            raise QQCallAPIError("invalid_group_request")
    if operation == "status":
        if scope is not None:
            scope = validate_qqcall_scope(scope)
    elif operation in {"accept", "hangup", "reject"}:
        if scope is None:
            raise QQCallAPIError("invalid_scope")
        scope = validate_qqcall_scope(scope)
    else:
        # join_group is an intent request, not a private call mutation, and is
        # deliberately unscoped until a native room identity is verifiable.
        scope = None
    if timeout is None:
        timeout = _GROUP_JOIN_REQUEST_TIMEOUT_SEC if operation == "join_group" else _DEFAULT_REQUEST_TIMEOUT_SEC
    if isinstance(timeout, bool):
        raise QQCallAPIError("invalid_timeout")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        raise QQCallAPIError("invalid_timeout") from None
    if not math.isfinite(timeout_value) or timeout_value <= 0 or timeout_value > 60:
        raise QQCallAPIError("invalid_timeout")
    if len(_pending_requests) >= _MAX_PENDING:
        raise QQCallAPIError("too_many_pending_requests")

    try:
        api = get_global_api()
    except Exception:
        raise QQCallAPIError("runtime_not_ready") from None
    if api is None:
        raise QQCallAPIError("runtime_not_ready")
    _ensure_registered(api)
    request_id = secrets.token_urlsafe(24)
    while request_id in _pending_requests:
        request_id = secrets.token_urlsafe(24)
    future = asyncio.get_running_loop().create_future()
    pending = _PendingRequest(
        platform,
        operation,
        peer_user_id,
        group_id,
        _copy_scope(scope),
        _copy_group_audio_scope(scope_group_value),
        _copy_group_audio_claim(audio_claim_value),
        future,
    )
    _pending_requests[request_id] = pending
    request = {
        "version": _PROTOCOL_VERSION,
        "request_id": request_id,
        "platform": platform,
        "operation": operation,
        "peer_user_id": peer_user_id,
        "scope": _copy_scope(scope),
    }
    if operation in {"join_group", "leave_group", "group_status", "group_audio_status"}:
        request["group_id"] = group_id
        if operation == "join_group":
            request["trigger_text"] = trigger_text
        elif operation == "leave_group":
            request["requester_user_id"] = requester_user_id
        elif operation == "group_audio_status":
            request["scope_group"] = _copy_group_audio_scope(scope_group_value)
            request["audio_claim"] = _copy_group_audio_claim(audio_claim_value)
    async def send_and_wait() -> dict[str, Any]:
        try:
            sent = await api.send_custom_message(
                platform=platform,
                message_type_name=QQCALL_REQUEST_TYPE,
                message=request,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QQCallAPIError("send_failed") from None
        if sent is not True:
            raise QQCallAPIError("send_failed")
        return await future

    try:
        try:
            # One deadline covers transport submission and the correlated reply.
            return await asyncio.wait_for(send_and_wait(), timeout_value)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            code = "timeout" if operation in {"status", "group_status", "group_audio_status"} else "request_outcome_unknown"
            raise QQCallAPIError(code) from None
    except asyncio.CancelledError:
        if not future.done():
            future.cancel()
        raise
    finally:
        _pending_requests.pop(request_id, None)
        # Drain a response exception if a false/failed send won an ack race.
        if future.done() and not future.cancelled():
            try:
                future.exception()
            except (asyncio.CancelledError, Exception):
                pass


def pending_request_count() -> int:
    """Return the active waiter count for diagnostics and focused tests."""

    return len(_pending_requests)


__all__ = [
    "QQCALL_REQUEST_TYPE",
    "QQCALL_RESPONSE_TYPE",
    "QQCallAPIError",
    "handle_qqcall_response",
    "pending_request_count",
    "request_qqcall",
    "validate_qq_group_id",
    "validate_qq_peer_user_id",
    "validate_qqcall_group_audio_claim",
    "validate_qqcall_group_scope",
    "validate_qqcall_scope",
]
