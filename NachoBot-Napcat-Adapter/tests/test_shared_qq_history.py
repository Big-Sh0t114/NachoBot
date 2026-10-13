from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_ROOT = Path(__file__).resolve().parents[2] / "NachoBot"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from ncnk_message import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo  # noqa: E402
from ncnk_message.offline_history import HistoryTarget, OfflineHistoryStore, build_delivery_metadata  # noqa: E402
from ncnk_message.qq_history_identity import (  # noqa: E402
    build_qq_private_identity,
    snow_message_id_for_native_sequence,
)


ACCOUNT = "2955610491"
PEER = "19563"
STAMP = 1_780_000_100.25


def _load_offline_history():
    source = Path(__file__).resolve().parents[1] / "src" / "offline_history.py"
    spec = importlib.util.spec_from_file_location("napcat_offline_history_identity_test", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load NapCat history integration from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_send_method():
    source = Path(__file__).resolve().parents[1] / "src" / "send_handler" / "main_send_handler.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    send_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendHandler")
    function = next(node for node in send_class.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_normal_message")

    class _ImportSeam(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "src.recv_handler.message_handler":
                return None
            return self.generic_visit(node)

    function = _ImportSeam().visit(function)
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
        critical=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    sender = _FakeNapcatSender()
    namespace = {
        "BaseMessageInfo": BaseMessageInfo,
        "GroupInfo": GroupInfo,
        "MessageBase": MessageBase,
        "Seg": Seg,
        "UserInfo": UserInfo,
        "logger": logger,
        "global_config": SimpleNamespace(napcat_server=SimpleNamespace(media_action_timeout_sec=1)),
        "SendMessageHandleClass": SimpleNamespace(process_seg_recursive=lambda value: list(value)),
        "nc_message_sender": sender,
        "message_handler": SimpleNamespace(verified_account_id=ACCOUNT),
    }
    from ncnk_message.qq_history_identity import resolve_qq_reply_segments

    namespace["resolve_qq_reply_segments"] = resolve_qq_reply_segments
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["send_normal_message"], sender


def _load_core_delivery_store():
    source = CORE_ROOT / "src" / "chat" / "message_receive" / "history_delivery.py"
    spec = importlib.util.spec_from_file_location("napcat_shared_identity_core_store_test", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Core delivery store from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HistoryDeliveryStore


class _FakeNapcatSender:
    def __init__(self):
        self.calls = []
        self.echoes = []

    async def send_message_to_napcat(self, action, params, **kwargs):
        self.calls.append((action, params, kwargs))
        return {"status": "ok", "data": {"message_id": "outgoing-napcat-id"}}

    async def message_sent_back(self, message, message_id):
        self.echoes.append((message, message_id))


def _serialized_hint(*, account=ACCOUNT, peer=PEER, napcat_id="450563", snow_id=None):
    return {
        "version": 1,
        "scope": "qq_private_friend",
        "account_id": account,
        "platform": "qq",
        "peer_id": peer,
        "original_reply_id": napcat_id,
        "native_ids": {
            "napcat": napcat_id,
            "snowluma": snow_id or snow_message_id_for_native_sequence(19_563, peer),
        },
    }


class NapcatSharedQQHistoryTests(unittest.TestCase):
    def test_live_normalizer_registers_verified_decimal_real_seq_identity(self):
        module = _load_offline_history()
        runtime = module.NapcatOfflineHistory(
            platform="qq",
            settings=SimpleNamespace(enabled=False, page_size=5, max_pages=2, retry_delay_sec=0.1),
            source_instance="test-napcat",
        )
        raw = {
            "message_type": "private",
            "sub_type": "friend",
            "self_id": ACCOUNT,
            "user_id": PEER,
            "message_id": "450563",
            "message_seq": 777,
            "real_seq": "19563",
            "time": STAMP,
        }
        context = {"metadata": {"original_timestamp": STAMP}}
        result = asyncio.run(runtime.decorate_live_message(raw, context, account_id=ACCOUNT))
        delivery = result["metadata"]["delivery"]
        identity = delivery["qq_identity"]
        self.assertEqual(identity["native_nt_seq"], 19_563)
        self.assertEqual(identity["snow_message_id"], snow_message_id_for_native_sequence(19_563, PEER))
        self.assertEqual(delivery["backend"], "napcat")

    def test_identity_sync_batches_only_bounded_metadata_without_waiting_for_receipts(self):
        module = _load_offline_history()
        sent = []

        async def capture(**payload):
            sent.append(payload)

        runtime = module.NapcatOfflineHistory(
            platform="qq",
            settings=SimpleNamespace(enabled=True, page_size=5, max_pages=2, retry_delay_sec=0.1),
            source_instance="identity-batch-test",
            send_custom=capture,
        )
        records = tuple(
            {
                "session_kind": "private",
                "session_id": PEER,
                "message_id": str(450_000 + index),
                "delivery_key": f"key-{index}",
                "original_timestamp": STAMP,
                "qq_identity": {"version": 1},
            }
            for index in range(201)
        )
        asyncio.run(
            runtime.send_identity_sync(
                run_id="registered-run",
                run_token="registered-token",
                cutoff=STAMP + 1,
                account_id=ACCOUNT,
                identities=records,
                account_is_current=lambda: True,
            )
        )
        self.assertEqual([len(item["message"]["identities"]) for item in sent], [100, 100, 1])
        self.assertTrue(all(item["message_type_name"] == "offline_history_run" for item in sent))
        self.assertTrue(all(item["message"]["phase"] == "identity_sync" for item in sent))
        self.assertTrue(all(set(record) == set(records[0]) for item in sent for record in item["message"]["identities"]))
        self.assertEqual(runtime._run_waiters, {})

    def test_latest_checkpoint_page_seeds_legacy_core_receipt_without_emitting(self):
        module = _load_offline_history()
        target = HistoryTarget("private", PEER)
        message_id = "450563"
        raw = {
            "message_type": "private",
            "sub_type": "friend",
            "self_id": ACCOUNT,
            "user_id": PEER,
            "sender": {"user_id": PEER},
            "message_id": message_id,
            "message_seq": 888_888,
            "real_seq": "19563",
            "time": STAMP,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            core_store = _load_core_delivery_store()(Path(temp_dir) / "core.sqlite3")
            napcat_key = build_delivery_metadata(
                backend="napcat",
                account_id=ACCOUNT,
                platform="qq",
                target=target,
                message_id=message_id,
                recovered=False,
            )["delivery_key"]
            self.assertEqual(
                core_store.claim(
                    {
                        "backend": "napcat",
                        "account_id": ACCOUNT,
                        "platform": "qq",
                        "session_kind": "private",
                        "session_id": PEER,
                        "message_id": message_id,
                        "delivery_key": napcat_key,
                        "recovered": False,
                        "original_timestamp": STAMP,
                    }
                ),
                "new",
            )
            core_store.mark_complete(napcat_key)

            adapter_store = OfflineHistoryStore(Path(temp_dir) / "adapter.sqlite3")
            adapter_store.initialize_scope("napcat", ACCOUNT, "qq", now=STAMP - 100)
            adapter_store.register_target("napcat", ACCOUNT, "qq", target, discovered_at=STAMP - 100)
            adapter_store.complete_target(
                "napcat",
                ACCOUNT,
                "qq",
                target,
                checkpoint_id=message_id,
                checkpoint_timestamp=STAMP,
                boundary_ids=(message_id,),
            )

            runtime_box = {}
            identity_payloads = []
            emitted = []

            async def transport(*, platform, message_type_name, message):
                if message_type_name == "offline_recovery_progress":
                    self.assertEqual(platform, "qq")
                    return
                self.assertEqual((platform, message_type_name), ("qq", "offline_history_run"))
                phase = message.get("phase")
                if phase == "begin":
                    core_store.begin_history_run(message)
                elif phase == "seal":
                    core_store.seal_history_run(message)
                elif phase == "identity_sync":
                    identity_payloads.append(message)
                    core_store.sync_qq_identities_from_run(message)
                if phase in {"begin", "seal"}:
                    runtime_box["runtime"].handle_run_receipt(
                        {
                            "content": {
                                "version": 1,
                                "backend": message["backend"],
                                "account_id": message["account_id"],
                                "platform": message["platform"],
                                "run_id": message["run_id"],
                                "run_token": message["run_token"],
                                "phase": phase,
                                "status": "ok",
                            }
                        }
                    )

            async def call_action(action, _params, *, expected_connection):
                self.assertIs(expected_connection, connection)
                self.assertEqual(action, "get_friend_msg_history")
                return {"status": "ok", "data": {"messages": [raw]}}

            runtime = module.NapcatOfflineHistory(
                platform="qq",
                settings=SimpleNamespace(
                    enabled=True,
                    page_size=10,
                    max_pages=5,
                    retry_delay_sec=0.1,
                    receipt_timeout_sec=1,
                ),
                state_path=Path(temp_dir) / "unused-state.sqlite3",
                source_instance="napcat-seed-test",
                store=adapter_store,
                call_action=call_action,
                send_custom=transport,
                emit_message=lambda *_args, **_kwargs: emitted.append(True),
            )
            runtime_box["runtime"] = runtime

            async def discover(_account_id, _connection):
                return (target,)

            runtime.discover_targets = discover
            connection = object()
            nav_scope = ("test-generation", id(connection))
            anchor = module._RecentContactAnchor(
                target_id=PEER,
                raw_message_id=message_id,
                timestamp=STAMP,
                message_id=message_id,
            )
            live_anchor = module._ScopedLiveAnchor(ACCOUNT, nav_scope, anchor)
            result = asyncio.run(
                runtime.run_generation(
                    account_id=ACCOUNT,
                    connection=connection,
                    connection_epoch_started_at=STAMP - 10,
                    account_is_current=lambda: True,
                    _live_anchors={PEER: live_anchor},
                    _recent_navigation_scope=nav_scope,
                )
            )

            self.assertEqual(result["complete_sessions"], 1)
            self.assertEqual(result["failed_sessions"], {})
            self.assertEqual(emitted, [])
            self.assertEqual(len(identity_payloads), 1)
            record = identity_payloads[0]["identities"][0]
            self.assertEqual(set(record), {
                "session_kind",
                "session_id",
                "message_id",
                "delivery_key",
                "original_timestamp",
                "qq_identity",
            })
            self.assertEqual(record["qq_identity"]["native_nt_seq"], 19_563)
            self.assertEqual(record["qq_identity"]["snow_message_id"], snow_message_id_for_native_sequence(19_563, PEER))

            snow_id = snow_message_id_for_native_sequence(19_563, PEER)
            snow_raw = {
                "message_type": "private",
                "sub_type": "friend",
                "self_id": ACCOUNT,
                "user_id": PEER,
                "message_id": snow_id,
                "time": STAMP,
            }
            snow_identity = build_qq_private_identity(
                backend="snowluma",
                account_id=ACCOUNT,
                platform="qq",
                session_kind="private",
                session_id=PEER,
                raw=snow_raw,
                original_timestamp=STAMP,
            )
            snow = build_delivery_metadata(
                backend="snowluma",
                account_id=ACCOUNT,
                platform="qq",
                target=target,
                message_id=snow_id,
                recovered=True,
                qq_identity=snow_identity,
            )
            snow["original_timestamp"] = STAMP
            self.assertEqual(core_store.claim(snow), "complete")
            self.assertEqual(int(core_store._connection().execute("SELECT COUNT(*) FROM history_deliveries").fetchone()[0]), 1)
            core_store.close()
            adapter_store.close()

    def test_actual_private_send_path_rewrites_only_confirmed_reply_and_preserves_body(self):
        send, sender = _load_send_method()
        hint = _serialized_hint()
        payload = [
            {"type": "reply", "data": {"id": "450563"}},
            {"type": "text", "data": {"text": "继续处理"}},
        ]
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                platform="qq",
                group_info=None,
                user_info=SimpleNamespace(user_id=PEER),
                additional_config={"qq_reply_identity": hint},
            ),
            message_segment=payload,
        )
        asyncio.run(send(SimpleNamespace(), message))
        self.assertEqual(sender.calls[0][0], "send_private_msg")
        self.assertEqual(sender.calls[0][1]["message"], [
            {"type": "reply", "data": {"id": "450563"}},
            {"type": "text", "data": {"text": "继续处理"}},
        ])
        self.assertEqual(sender.echoes[0][1], "outgoing-napcat-id")

    def test_actual_private_send_path_drops_wrong_scope_or_unknown_native_reply_only(self):
        for hint in (
            _serialized_hint(account="wrong-account"),
            {**_serialized_hint(), "native_ids": {"snowluma": "-123"}},
        ):
            with self.subTest(hint=hint):
                send, sender = _load_send_method()
                message = SimpleNamespace(
                    message_info=SimpleNamespace(
                        platform="qq",
                        group_info=None,
                        user_info=SimpleNamespace(user_id=PEER),
                        additional_config={"qq_reply_identity": hint},
                    ),
                    message_segment=[
                        {"type": "reply", "data": {"id": "450563"}},
                        {"type": "text", "data": {"text": "正文照常发送"}},
                    ],
                )
                asyncio.run(send(SimpleNamespace(), message))
                self.assertEqual(sender.calls[0][1]["message"], [
                    {"type": "text", "data": {"text": "正文照常发送"}},
                ])


if __name__ == "__main__":
    unittest.main()
