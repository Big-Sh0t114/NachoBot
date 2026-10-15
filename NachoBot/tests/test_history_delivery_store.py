from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


def _load_store_module():
    source_path = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "history_delivery.py"
    spec = importlib.util.spec_from_file_location("history_delivery_store_source_test", source_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Core delivery store from {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history_delivery = _load_store_module()
HistoryDeliveryStore = history_delivery.HistoryDeliveryStore


def _load_receipt_helper():
    source_path = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "bot.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(
        node
        for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_history_delivery_receipt"
    )

    class _ImportSeam(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "src.common.message" and any(alias.name == "get_global_api" for alias in node.names):
                return None
            return self.generic_visit(node)

    function = _ImportSeam().visit(function)
    ast.fix_missing_locations(function)
    module = ast.Module(body=[function], type_ignores=[])
    return compile(module, str(source_path), "exec")


_RECEIPT_HELPER_CODE = _load_receipt_helper()


def _begin(run_id: str, token: str, targets: list[tuple[str, str]], *, source: str = "adapter-a") -> dict:
    return {
        "version": 1,
        "backend": "napcat",
        "account_id": "bot-123",
        "platform": "qq",
        "source_instance": source,
        "run_id": run_id,
        "run_token": token,
        "cutoff_at": 1_780_000_200.0,
        "phase": "begin",
        "targets": [{"session_kind": kind, "session_id": session_id} for kind, session_id in targets],
    }


def _delivery(session_id: str, run_id: str = "run-old") -> dict:
    return {
        "backend": "napcat",
        "account_id": "bot-123",
        "platform": "qq",
        "session_kind": "group",
        "session_id": session_id,
        "message_id": f"message-{session_id}",
        "delivery_key": f"delivery-{session_id}",
        "receipt_token": "receipt-token",
        "recovered": True,
        "original_timestamp": 1_780_000_100.25,
        "history_run": {"run_id": run_id, "cutoff_at": 1_780_000_200.0},
    }


class HistoryDeliveryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = HistoryDeliveryStore(self.path, processing_lease_sec=1)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_same_core_owner_cannot_reclaim_slow_processing_row_after_lease(self):
        delivery = _delivery("slow")
        self.assertEqual(self.store.claim(delivery, now=10), "new")
        self.assertEqual(self.store.claim(delivery, now=10_000), "busy")

        restarted_store = HistoryDeliveryStore(self.path, processing_lease_sec=1)
        try:
            self.assertEqual(restarted_store.claim(delivery, now=10_001), "new")
        finally:
            restarted_store.close()

    def test_successful_seal_releases_focus_hold_but_keeps_durable_row_pending(self):
        begin = _begin("run-current", "token-current", [("group", "A")])
        self.store.begin_history_run(begin)
        delivery = _delivery("A", run_id="run-current")
        self.assertEqual(self.store.claim(delivery), "new")
        self.store.mark_pending(delivery["delivery_key"], chat_id="qq:group:A", row_id=31, original_timestamp=1_780_000_100.25)

        seal = {**begin, "phase": "seal", "targets": [{"session_kind": "group", "session_id": "A"}]}
        self.assertEqual(self.store.seal_history_run(seal), (("run-current", "group", "A"),))
        self.assertFalse(self.store.history_target_blocked("run-current", "group", "A"))
        self.assertEqual(self.store.pending_rows()[0]["row_id"], 31)
        self.assertEqual(self.store.state(delivery["delivery_key"]), "pending")

        self.assertEqual(self.store.complete_rows([31]), 1)
        self.assertEqual(self.store.state(delivery["delivery_key"]), "complete")
        self.assertEqual(self.store.pending_rows(), ())

    def test_failed_route_with_old_pending_row_stays_open_until_later_full_scan_seals(self):
        old_begin = _begin("run-old", "token-old", [("group", "A"), ("group", "B")])
        self.store.begin_history_run(old_begin)
        old_delivery = _delivery("A", run_id="run-old")
        self.assertEqual(self.store.claim(old_delivery), "new")
        self.store.mark_pending(old_delivery["delivery_key"], chat_id="qq:group:A", row_id=41, original_timestamp=1_780_000_100.25)

        newer = _begin("run-new", "token-new", [("group", "A"), ("group", "B")], source="adapter-b")
        self.store.begin_history_run(newer)
        # B failed with no pending rows and can be released. A failed while
        # carrying an earlier durable row and must retain both run holds.
        retired = self.store.release_empty_open_targets_from_older_instances(
            backend="napcat", account_id="bot-123", platform="qq", source_instance="adapter-b"
        )
        self.assertIn(("run-old", "group", "B"), retired)
        self.assertTrue(self.store.history_target_blocked("run-old", "group", "A"))
        self.assertFalse(self.store.history_target_blocked("run-old", "group", "B"))

        partial_seal = {
            **newer,
            "phase": "seal",
            "targets": [{"session_kind": "group", "session_id": "B"}],
        }
        closed = self.store.seal_history_run(partial_seal)
        self.assertNotIn(("run-new", "group", "A"), closed)
        self.assertTrue(self.store.history_target_blocked("run-new", "group", "A"))
        self.assertFalse(self.store.history_target_blocked("run-new", "group", "B"))

        full_seal = {**newer, "phase": "seal", "targets": newer["targets"]}
        closed = self.store.seal_history_run(full_seal)
        self.assertIn(("run-old", "group", "A"), closed)
        self.assertIn(("run-new", "group", "A"), closed)
        self.assertFalse(self.store.history_target_blocked("run-old", "group", "A"))
        self.assertFalse(self.store.history_target_blocked("run-new", "group", "A"))
        self.assertEqual(len(self.store.pending_rows()), 1, "sealing the scan must not mark the stored row replied")

    def test_offline_collector_pending_live_transport_row_keeps_failed_route_open(self):
        # The adapter can deliver a live envelope before Core's offline
        # collector classifies it as historical. Its transport metadata says
        # recovered=false, but once persisted in the recovery collection it
        # must protect an omitted/failed route from an empty seal.
        delivery = _delivery("collector-live", run_id="")
        delivery["recovered"] = False
        delivery.pop("history_run")
        self.assertEqual(self.store.claim(delivery), "new")
        self.store.mark_pending(
            delivery["delivery_key"],
            chat_id="qq:group:collector-live",
            row_id=61,
            original_timestamp=1_780_000_100.25,
        )

        begin = _begin("run-collector", "token-collector", [("group", "collector-live")])
        self.store.begin_history_run(begin)
        seal = {**begin, "phase": "seal", "targets": []}
        closed = self.store.seal_history_run(seal)

        self.assertNotIn(("run-collector", "group", "collector-live"), closed)
        self.assertTrue(self.store.history_target_blocked("run-collector", "group", "collector-live"))
        self.assertEqual(self.store.pending_rows()[0]["row_id"], 61)

    def test_reopen_repairs_legacy_pending_live_row_before_omitted_route_seal(self):
        begin = _begin("run-before-upgrade", "token-before-upgrade", [("group", "legacy")])
        self.store.begin_history_run(begin)
        delivery = _delivery("legacy", run_id="")
        delivery["recovered"] = False
        delivery.pop("history_run")
        self.assertEqual(self.store.claim(delivery), "new")
        self.store._connection().execute(
            "UPDATE history_deliveries SET state='pending', recovered=0, chat_id=?, row_id=?, original_timestamp=? WHERE delivery_key=?",
            ("qq:group:legacy", 71, 1_780_000_100.25, delivery["delivery_key"]),
        )
        self.store.close()

        reopened = HistoryDeliveryStore(self.path)
        try:
            recovered = reopened._connection().execute(
                "SELECT recovered FROM history_deliveries WHERE delivery_key=?",
                (delivery["delivery_key"],),
            ).fetchone()[0]
            self.assertEqual(recovered, 1)
            newer = _begin("run-after-upgrade", "token-after-upgrade", [("group", "legacy")], source="adapter-b")
            reopened.begin_history_run(newer)
            closed = reopened.seal_history_run({**newer, "phase": "seal", "targets": []})
            self.assertNotIn(("run-after-upgrade", "group", "legacy"), closed)
            self.assertTrue(reopened.history_target_blocked("run-before-upgrade", "group", "legacy"))
            self.assertTrue(reopened.history_target_blocked("run-after-upgrade", "group", "legacy"))
        finally:
            reopened.close()

    def test_restart_lists_every_open_route_before_pending_delivery_restore(self):
        begin = _begin("run-open", "token-open", [("group", "A"), ("group", "B")])
        self.store.begin_history_run(begin)
        delivery = _delivery("A", run_id="run-open")
        self.assertEqual(self.store.claim(delivery), "new")
        self.store.mark_pending(delivery["delivery_key"], chat_id="qq:group:A", row_id=51, original_timestamp=1_780_000_100.25)

        restarted_store = HistoryDeliveryStore(self.path)
        try:
            runs = restarted_store.open_history_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(
                {(target["session_kind"], target["session_id"]) for target in runs[0]["targets"]},
                {("group", "A"), ("group", "B")},
            )
            self.assertEqual(restarted_store.pending_rows()[0]["row_id"], 51)
            self.assertTrue(restarted_store.history_target_blocked("run-open", "group", "A"))
            self.assertTrue(restarted_store.history_target_blocked("run-open", "group", "B"))
        finally:
            restarted_store.close()

    def test_seal_rejects_wrong_token_and_wrong_scope(self):
        begin = _begin("run-scope", "token-scope", [("group", "A")])
        self.store.begin_history_run(begin)
        base_seal = {**begin, "phase": "seal", "targets": begin["targets"]}
        with self.assertRaisesRegex(ValueError, "token"):
            self.store.seal_history_run({**base_seal, "run_token": "wrong-token"})
        with self.assertRaisesRegex(ValueError, "scope"):
            self.store.seal_history_run({**base_seal, "account_id": "other-bot"})

    def test_filtered_is_terminal_but_processing_error_remains_retryable(self):
        filtered = _delivery("filtered")
        self.assertEqual(self.store.claim(filtered), "new")
        self.store.mark_filtered(filtered["delivery_key"])
        self.assertEqual(self.store.state(filtered["delivery_key"]), "filtered")
        self.assertEqual(self.store.claim(filtered), "filtered")

        errored = _delivery("errored")
        self.assertEqual(self.store.claim(errored), "new")
        # Core removes a nonterminal processing claim on exception and sends
        # no receipt, so the adapter's durable outbox can retry the message.
        self.store.release(errored["delivery_key"])
        self.assertIsNone(self.store.state(errored["delivery_key"]))
        self.assertEqual(self.store.claim(errored), "new")

    def test_delivery_envelope_requires_exact_scoped_identity_and_run_cutoff(self):
        identity = ("napcat", "bot-123", "qq", "group", "A", "message-A")
        delivery = {
            "version": 1,
            "backend": identity[0],
            "account_id": identity[1],
            "platform": identity[2],
            "session_kind": identity[3],
            "session_id": identity[4],
            "message_id": identity[5],
            "delivery_key": hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "receipt_token": "secret-token",
            "recovered": True,
        }
        envelope = {
            "message_info": {
                "platform": "qq",
                "message_id": "message-A",
                "group_info": {"group_id": "A"},
                "additional_config": {
                    "offline_recovery": {
                        "delivery": delivery,
                        "history_run": {"run_id": "run-1", "cutoff_at": 200.0},
                        "original_timestamp": 199.5,
                    }
                },
            }
        }
        parsed = history_delivery.delivery_from_envelope(envelope)
        self.assertEqual(parsed["delivery_key"], delivery["delivery_key"])
        self.assertEqual(parsed["original_timestamp"], 199.5)

        wrong_route = json.loads(json.dumps(envelope))
        wrong_route["message_info"]["group_info"]["group_id"] = "another-group"
        self.assertIsNone(history_delivery.delivery_from_envelope(wrong_route))
        after_cutoff = json.loads(json.dumps(envelope))
        after_cutoff["message_info"]["additional_config"]["offline_recovery"]["original_timestamp"] = 201.0
        self.assertIsNone(history_delivery.delivery_from_envelope(after_cutoff))

    def test_core_emits_scoped_terminal_receipt_and_tolerates_transport_failure(self):
        sent = []

        class _Api:
            fail = False

            async def send_custom_message(self, platform, message_type, payload):
                if self.fail:
                    raise OSError("fake websocket disconnected")
                sent.append((platform, message_type, payload))

        class _Logger:
            def warning(self, *_args, **_kwargs):
                return None

        api = _Api()
        namespace = {
            "Any": Any,
            "get_global_api": lambda: api,
            "logger": _Logger(),
        }
        exec(_RECEIPT_HELPER_CODE, namespace)
        send_receipt = namespace["_send_history_delivery_receipt"]
        delivery = _delivery("A")

        async def scenario():
            await send_receipt(delivery, "filtered")
            api.fail = True
            await send_receipt(delivery, "accepted")

        asyncio.run(scenario())
        self.assertEqual(len(sent), 1)
        platform, message_type, payload = sent[0]
        self.assertEqual((platform, message_type), ("qq", "offline_history_receipt"))
        self.assertEqual(payload["delivery_key"], delivery["delivery_key"])
        self.assertEqual(payload["receipt_token"], delivery["receipt_token"])
        self.assertEqual(payload["outcome"], "filtered")


if __name__ == "__main__":
    unittest.main()
