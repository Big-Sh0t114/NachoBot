"""Regression tests for SendAPI's legacy and receipt Focus boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

from ncnk_message import Seg

from src.chat.focus import bind_lease, current_context_lease
from src.chat.focus.models import EffectKind, FocusLease, StaleFocusLeaseError
from src.plugin_system.apis import send_api


class SendAPIFocusLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_bool_path_uses_permitted_sender_with_bound_lease(self):
        lease = FocusLease("focus-group", "source-stream", 7, "source-turn")
        permitted_sender = AsyncMock(
            return_value=send_api.SendReceipt(
                send_api.SendStatus.DELIVERED,
                "background-stream",
                message_id="background-message",
            )
        )
        fenced_sender = AsyncMock(side_effect=AssertionError("legacy bool path used fenced sender"))

        with (
            patch.object(send_api, "_send_to_target_receipt_permitted", permitted_sender),
            patch.object(send_api, "_send_to_target_receipt", fenced_sender),
            bind_lease(lease),
        ):
            self.assertIs(current_context_lease(), lease)
            delivered = await send_api._send_to_target(
                message_segment=Seg(type="text", data="background task"),
                stream_id="background-stream",
            )

        self.assertTrue(delivered)
        permitted_sender.assert_awaited_once()
        fenced_sender.assert_not_awaited()

    async def test_receipt_path_stays_fenced_and_returns_stale_lease(self):
        permitted_sender = AsyncMock(
            return_value=send_api.SendReceipt(send_api.SendStatus.DELIVERED, "generated-stream")
        )

        @asynccontextmanager
        async def reject_stale_lease():
            raise StaleFocusLeaseError("stale generated-reply lease")
            yield  # pragma: no cover

        with (
            patch.object(
                send_api.focus_coordinator,
                "effect_permit",
                return_value=reject_stale_lease(),
            ) as effect_permit,
            patch.object(send_api, "_send_to_target_receipt_permitted", permitted_sender),
            bind_lease(FocusLease("focus-group", "generated-stream", 8, "generated-turn")),
        ):
            receipt = await send_api.text_to_stream_receipt(
                text="generated reply",
                stream_id="generated-stream",
            )

        self.assertIs(receipt.status, send_api.SendStatus.STALE_LEASE)
        self.assertEqual(receipt.stream_id, "generated-stream")
        self.assertIn("stale generated-reply lease", receipt.detail)
        effect_permit.assert_called_once_with(
            None,
            EffectKind.SEND,
            target_chat_id="generated-stream",
        )
        permitted_sender.assert_not_awaited()

    async def test_background_receipt_bypasses_inherited_stale_lease(self):
        permitted_sender = AsyncMock(
            return_value=send_api.SendReceipt(
                send_api.SendStatus.DELIVERED,
                "background-stream",
                message_id="background-report",
            )
        )
        fenced_sender = AsyncMock(side_effect=AssertionError("background result used fenced sender"))

        with (
            patch.object(send_api, "_send_to_target_receipt_permitted", permitted_sender),
            patch.object(send_api, "_send_to_target_receipt", fenced_sender),
            bind_lease(FocusLease("focus-group", "source-stream", 7, "stale-turn")),
        ):
            receipt = await send_api.background_text_to_stream_receipt(
                text="Sandbox 已完成。",
                stream_id="background-stream",
            )

        self.assertIs(receipt.status, send_api.SendStatus.DELIVERED)
        permitted_sender.assert_awaited_once()
        fenced_sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
