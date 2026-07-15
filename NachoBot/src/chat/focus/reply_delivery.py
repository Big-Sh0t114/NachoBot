"""Settle reserved Focus reply context from real adapter delivery receipts."""

from __future__ import annotations

from typing import Iterable, Sequence, TYPE_CHECKING

from src.common.logger import get_logger

from .reply_context import ReplyContextRef, acknowledge_reply_context, release_reply_context

if TYPE_CHECKING:
    from src.plugin_system.apis.send_api import SendReceipt


logger = get_logger("focus_reply_delivery")


async def settle_reply_context_delivery(
    refs: Iterable[ReplyContextRef],
    receipts: Sequence["SendReceipt"],
) -> bool:
    """ACK once on partial/full delivery; otherwise release the reservation."""

    context_refs = tuple(refs)
    if not context_refs:
        return False
    delivered = [receipt for receipt in receipts if receipt.delivered]
    if not delivered:
        await release_reply_context(context_refs, "not_delivered")
        return False

    # One logical reply cycle consumes once even when the reply was split.
    delivery_id = delivered[0].message_id or f"{delivered[0].stream_id}:{context_refs[0].cycle_id}"
    acknowledged = await acknowledge_reply_context(context_refs, delivery_id)
    if not acknowledged:
        logger.warning(
            f"Focus ReplyContext 投递成功但 ACK 未完整写入: cycle={context_refs[0].cycle_id}, delivery={delivery_id}"
        )
    return acknowledged
