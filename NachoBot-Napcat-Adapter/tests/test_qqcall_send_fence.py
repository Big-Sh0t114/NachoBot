from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-Napcat-Adapter"
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
if str(ROOT / "NachoBot") not in sys.path:
    sys.path.append(str(ROOT / "NachoBot"))

from src.send_handler.nc_sending import NCMessageSender  # noqa: E402


class TestNapCatQQCallSendFence(unittest.TestCase):
    def test_queue_checks_policy_immediately_before_send_and_rechecks_connection(self):
        async def scenario():
            sender = NCMessageSender()
            sent = []

            class Connection:
                async def send(self, payload):
                    sent.append(payload)

            original = Connection()
            sender.server_connection = original
            worker = asyncio.create_task(sender._queue_worker())
            try:
                loop = asyncio.get_running_loop()
                failed = loop.create_future()

                async def deny():
                    return False

                await sender._send_queue.put(("private-action", "busy-1", failed, original, deny, None))
                result = await asyncio.wait_for(failed, timeout=1.0)
                self.assertEqual(result, {"status": "error", "message": "pre_send_check_failed"})
                self.assertEqual(sent, [])

                stale = loop.create_future()

                async def replace_connection():
                    sender.server_connection = Connection()
                    return True

                await sender._send_queue.put(("private-action", "busy-2", stale, original, replace_connection, None))
                result = await asyncio.wait_for(stale, timeout=1.0)
                self.assertEqual(result, {"status": "error", "message": "stale_connection"})
                self.assertEqual(sent, [])

                identity_stale = loop.create_future()
                account_current = {"value": True}

                async def replace_account():
                    account_current["value"] = False
                    return True

                sender.server_connection = original
                await sender._send_queue.put((
                    "private-action",
                    "busy-3",
                    identity_stale,
                    original,
                    replace_account,
                    lambda: account_current["value"],
                ))
                result = await asyncio.wait_for(identity_stale, timeout=1.0)
                self.assertEqual(result, {"status": "error", "message": "stale_connection"})
                self.assertEqual(sent, [])

                cancelled = loop.create_future()
                preflight_started = asyncio.Event()
                release_preflight = asyncio.Event()

                async def delayed_preflight():
                    preflight_started.set()
                    await release_preflight.wait()
                    return True

                await sender._send_queue.put((
                    "private-action",
                    "busy-4",
                    cancelled,
                    original,
                    delayed_preflight,
                    lambda: True,
                ))
                await asyncio.wait_for(preflight_started.wait(), timeout=1.0)
                cancelled.cancel()
                release_preflight.set()
                await asyncio.wait_for(sender._send_queue.join(), timeout=1.0)
                self.assertEqual(sent, [])
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
