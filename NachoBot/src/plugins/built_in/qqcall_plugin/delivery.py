"""Await the adapter's receipt for each scoped QQCall packet."""

import asyncio
import copy
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from src.chat.runtime_capabilities import additional_config_from_message
from src.plugin_system.apis.delivery_api import (
    DeliveryRejected,
    register_delivery_provider,
    unregister_delivery_provider,
)


class QQCallDeliveryRejected(DeliveryRejected):
    """A rejected/unknown delivery must never trigger a fresh audio fallback."""

    fallback_allowed = False


@dataclass
class _Pending:
    future: asyncio.Future
    codes: frozenset[str]


_pending: dict[str, _Pending] = {}
_registered_api = None
_MAX_PENDING = 64
_ACK_TIMEOUT = 20.0


def register(enabled) -> None:
    for kind in ("qqcall", "qqcall_group"):
        register_delivery_provider("qq", kind, send_with_qqcall_receipt, enabled=enabled)


def unregister() -> None:
    for kind in ("qqcall", "qqcall_group"):
        unregister_delivery_provider("qq", kind)
    # Release receipt waiters when the plugin unloads or its config is disabled.
    for pending in tuple(_pending.values()):
        if not pending.future.done():
            pending.future.set_result(False)


def is_qqcall_delivery(message) -> bool:
    target = additional_config_from_message(message).get("delivery_target")
    return (
        getattr(message.message_info, "platform", None) == "qq"
        and isinstance(target, Mapping)
        and target.get("kind") in {"qqcall", "qqcall_group"}
    )


def _receipt_codes(segment, kind: str) -> tuple[frozenset[str], bool]:
    codes = frozenset({"display_text_acknowledged"})
    buffered = False

    def visit(item):
        nonlocal codes, buffered
        if item.type == "seglist" and isinstance(item.data, list):
            for child in item.data:
                visit(child)
        elif item.type in {"voice", "voice_stream"}:
            completed_audio = item.type == "voice" or (
                isinstance(item.data, Mapping) and item.data.get("event") == "end"
            )
            # Queue admission is sufficient for intermediate packets, but it
            # cannot confirm a buffered clip or the final stream drain.
            codes = frozenset({"local_endpoint_drained"}) if completed_audio else frozenset({
                "local_endpoint_queued", "local_endpoint_drained",
            })
            buffered |= item.type == "voice"
        elif item.type not in {"text", "tts_text", "reply"}:
            suffix = "group" if kind == "qqcall_group" else "call"
            codes = frozenset({f"unsupported_{suffix}_segment_consumed"})

    visit(segment)
    return codes, buffered


async def handle_qqcall_delivery_response(raw) -> None:
    if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict):
        return
    envelope = raw["content"]
    request_id = envelope.get("request_id")
    pending = _pending.get(request_id) if isinstance(request_id, str) else None
    if pending is None or pending.future.done():
        return
    if (
        raw.get("platform") != "qq" or envelope.get("platform") != "qq"
        or type(envelope.get("version")) is not int or envelope["version"] != 1
    ):
        return
    result = envelope.get("result")
    accepted = (
        envelope.get("status") == "accepted"
        and set(envelope) == {"version", "platform", "status", "request_id", "result"}
        and isinstance(result, dict)
        and set(result) == {"code", "drain_confirmed"}
        and isinstance(result.get("code"), str)
        and result["code"] in pending.codes
        and type(result.get("drain_confirmed")) is bool
        and result["drain_confirmed"] == (result["code"] == "local_endpoint_drained")
    )
    # No upstream payload/error text is logged or surfaced to users.
    pending.future.set_result(accepted)


async def send_with_qqcall_receipt(api, message) -> bool:
    global _registered_api
    if not is_qqcall_delivery(message):
        return (await api.send_message(message)) is not False
    if len(_pending) >= _MAX_PENDING:
        raise QQCallDeliveryRejected("qqcall_delivery_capacity")
    if api is not _registered_api:
        try:
            api.register_custom_message_handler(
                "qqcall_delivery_response", handle_qqcall_delivery_response
            )
        except Exception:
            raise QQCallDeliveryRejected("qqcall_delivery_handler_unavailable") from None
        _registered_api = api
    additional = copy.deepcopy(additional_config_from_message(message))
    target = additional["delivery_target"]
    request_id = secrets.token_urlsafe(24)
    while request_id in _pending:
        request_id = secrets.token_urlsafe(24)
    target["request_id"] = request_id
    packet = copy.copy(message)
    packet.message_info = copy.copy(message.message_info)
    packet.message_info.additional_config = additional
    codes, buffered = _receipt_codes(message.message_segment, target["kind"])
    future = asyncio.get_running_loop().create_future()
    _pending[request_id] = _Pending(future, codes)

    async def send_and_wait():
        try:
            sent = await api.send_message(packet)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QQCallDeliveryRejected("qqcall_delivery_send_failed") from None
        if sent is False:
            return False
        return await future

    try:
        # Buffered legacy playback can cover the full bounded 300-second audio;
        # streaming packets have short receipts and retain their own deadline.
        accepted = await asyncio.wait_for(send_and_wait(), 320.0 if buffered else _ACK_TIMEOUT)
        if not accepted:
            raise QQCallDeliveryRejected("qqcall_delivery_rejected")
        return True
    except asyncio.TimeoutError:
        raise QQCallDeliveryRejected("qqcall_delivery_timeout") from None
    finally:
        _pending.pop(request_id, None)
        if not future.done():
            future.cancel()
