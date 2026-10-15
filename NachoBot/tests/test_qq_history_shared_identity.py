from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ncnk_message.offline_history import HistoryTarget, scoped_delivery_key
from ncnk_message.qq_history_identity import (
    build_qq_private_identity,
    native_nt_sequence,
    snow_message_id_for_native_sequence,
    validate_qq_private_identity,
)


def _load_history_delivery():
    source = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "history_delivery.py"
    spec = importlib.util.spec_from_file_location("qq_shared_history_source", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Core delivery store from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history_delivery = _load_history_delivery()
HistoryDeliveryStore = history_delivery.HistoryDeliveryStore


class _Condition:
    def __init__(self, predicate):
        self.predicate = predicate

    def __and__(self, other):
        return _Condition(lambda row: self.predicate(row) and other.predicate(row))


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return _Condition(lambda row: getattr(row, self.name, None) == value)

    def is_null(self, value):
        return _Condition(lambda row: (getattr(row, self.name, None) is None) is value)

    def desc(self):
        return self


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def where(self, condition):
        return _Rows(row for row in self.rows if condition.predicate(row))

    def order_by(self, *_fields):
        return self

    def __iter__(self):
        return iter(self.rows)


class _Messages:
    message_id = _Field("message_id")
    chat_info_platform = _Field("chat_info_platform")
    chat_info_group_id = _Field("chat_info_group_id")
    chat_info_user_id = _Field("chat_info_user_id")
    time = _Field("time")
    id = _Field("id")
    rows = []

    @classmethod
    def select(cls):
        return _Rows(cls.rows)


class _StoredMessageRef:
    def __init__(self, row_id, chat_id, message_id, message_time):
        self.row_id = row_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.message_time = message_time


def _load_storage_ingress(history_store):
    source = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "storage.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    helpers = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_positive_timestamp", "_legacy_qq_alias_row_matches", "_qq_alias_row_matches_scope"}
    ]
    storage_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MessageStorage")
    function = next(
        node for node in storage_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "find_existing_ingress"
    )
    function.decorator_list = []

    class _ImportSeam(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "src.chat.message_receive.history_delivery":
                return None
            return self.generic_visit(node)

    module = ast.Module(body=helpers + [_ImportSeam().visit(function)], type_ignores=[])
    ast.fix_missing_locations(module)
    from ncnk_message.qq_history_identity import positive_qq_id, validate_qq_private_identity

    namespace = {
        "json": json,
        "hashlib": hashlib,
        "math": __import__("math"),
        "Messages": _Messages,
        "StoredMessageRef": history_delivery.StoredMessageRef if hasattr(history_delivery, "StoredMessageRef") else _StoredMessageRef,
        "logger": SimpleNamespace(exception=lambda *_args, **_kwargs: None),
        "positive_qq_id": positive_qq_id,
        "validate_qq_private_identity": validate_qq_private_identity,
        "history_delivery_store": history_store,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["find_existing_ingress"]


def _load_bot_message_process(*, store, delivery, find_existing_ingress, receipts, restores):
    source = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "bot.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    bot_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatBot")
    function = next(
        node for node in bot_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "message_process"
    )

    class _ImportSeam(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module in {
                "src.chat.offline_recovery",
                "src.chat.message_receive.history_delivery",
            }:
                return None
            return self.generic_visit(node)

    function = _ImportSeam().visit(function)
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Dict": dict,
        "Any": Any,
        "delivery_from_envelope": lambda _envelope: delivery,
        "history_delivery_store": store,
        "offline_recovery_manager": SimpleNamespace(
            restore_pending_deliveries=lambda keys: restores.extend(keys),
            message_finished=lambda _key: None,
        ),
        "MessageStorage": SimpleNamespace(find_existing_ingress=find_existing_ingress),
        "_send_history_delivery_receipt": lambda delivery, outcome: _capture_receipt(receipts, delivery, outcome),
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["message_process"]


def _load_message_sending_to_dict(history_store):
    source = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "message.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    sending = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MessageSending")
    methods = [
        node for node in sending.body
        if isinstance(node, ast.FunctionDef) and node.name in {"to_dict", "_qq_reply_identity_hint"}
    ]

    class _ImportSeam(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "src.chat.message_receive.history_delivery":
                return None
            return self.generic_visit(node)

    methods = [_ImportSeam().visit(node) for node in methods]
    base = ast.ClassDef(
        name="MessageProcessBase",
        bases=[],
        keywords=[],
        body=[
            ast.FunctionDef(
                name="to_dict",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=[
                    ast.Return(
                        ast.Dict(
                            keys=[ast.Constant("message_info")],
                            values=[
                                ast.Dict(
                                    keys=[ast.Constant("additional_config")],
                                    values=[ast.Attribute(value=ast.Attribute(value=ast.Name("self", ast.Load()), attr="message_info", ctx=ast.Load()), attr="additional_config", ctx=ast.Load())],
                                )
                            ],
                        )
                    )
                ],
                decorator_list=[],
            )
        ],
        decorator_list=[],
    )
    cls = ast.ClassDef(name="MessageSending", bases=[ast.Name("MessageProcessBase", ast.Load())], keywords=[], body=methods, decorator_list=[])
    module = ast.Module(body=[base, cls], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "json": json,
        "sqlite3": __import__("sqlite3"),
        "math": __import__("math"),
        "hashlib": hashlib,
        "Any": Any,
        "history_delivery_store": history_store,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["MessageSending"]


async def _capture_receipt(receipts, delivery, outcome):
    receipts.append((delivery, outcome))


STAMP = 1_780_000_100.25
ACCOUNT = "2955610491"
PEER = "19563"


def _message_id(sequence: int) -> str:
    return snow_message_id_for_native_sequence(sequence, PEER)


def _delivery(
    backend: str,
    sequence: int = 19_563,
    *,
    account: str = ACCOUNT,
    peer: str = PEER,
    stamp: float = STAMP,
    proof: bool = True,
    message_id: str | None = None,
) -> dict:
    snow_id = snow_message_id_for_native_sequence(sequence, peer)
    if message_id is None:
        message_id = str(450_000 + sequence) if backend == "napcat" else snow_id
    raw = {
        "message_type": "private",
        "sub_type": "friend",
        "self_id": account,
        "user_id": peer,
        "time": stamp,
        "message_id": message_id,
        # Deliberately unrelated. SnowLuma never exposes canonical ntMsgSeq.
        "message_seq": 888_888_888,
    }
    if backend == "napcat":
        raw["real_seq"] = str(sequence)
    identity = build_qq_private_identity(
        backend=backend,
        account_id=account,
        platform="qq",
        session_kind="private",
        session_id=peer,
        raw=raw,
        original_timestamp=stamp,
    )
    if not proof:
        identity = None
    delivery_key = scoped_delivery_key(backend, account, "qq", HistoryTarget("private", peer), message_id)
    delivery = {
        "version": 1,
        "backend": backend,
        "account_id": account,
        "platform": "qq",
        "session_kind": "private",
        "session_id": peer,
        "message_id": message_id,
        "delivery_key": delivery_key,
        "receipt_token": f"receipt-{backend}-{sequence}",
        "recovered": False,
        "original_timestamp": stamp,
    }
    if identity is not None:
        delivery["qq_identity"] = identity
    return delivery


def _run(backend: str, run_id: str, token: str, source: str, peer: str = PEER) -> dict:
    return {
        "version": 1,
        "backend": backend,
        "account_id": ACCOUNT,
        "platform": "qq",
        "source_instance": source,
        "run_id": run_id,
        "run_token": token,
        "cutoff_at": STAMP + 300,
        "phase": "begin",
        "targets": [{"session_kind": "private", "session_id": peer}],
    }


def _identity_sync_record(delivery: dict, *, stamp: float | None = None, identity: dict | None = None) -> dict:
    return {
        "session_kind": "private",
        "session_id": delivery["session_id"],
        "message_id": delivery["message_id"],
        "delivery_key": delivery["delivery_key"],
        "original_timestamp": delivery["original_timestamp"] if stamp is None else stamp,
        "qq_identity": delivery.get("qq_identity") if identity is None else identity,
    }


class QQHistorySharedIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shared-history.sqlite3"
        self.store = HistoryDeliveryStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _delivery_count(self) -> int:
        return int(self.store._connection().execute("SELECT COUNT(*) FROM history_deliveries").fetchone()[0])

    @staticmethod
    def _stored_core_message(delivery: dict, row_id: int):
        return SimpleNamespace(
            id=row_id,
            chat_id="qq:private:19563",
            message_id=delivery["message_id"],
            time=delivery["original_timestamp"],
            chat_info_platform="qq",
            chat_info_group_id=None,
            chat_info_user_id=delivery["session_id"],
            chat_info_user_platform="qq",
            user_id=delivery["session_id"],
            user_platform="qq",
            additional_config=json.dumps(
                {
                    "offline_recovery": {
                        "original_timestamp": delivery["original_timestamp"],
                        "delivery": delivery,
                    }
                }
            ),
        )

    def test_napcat_real_seq_decimal_string_maps_to_snow_native_id(self):
        identity = _delivery("napcat")["qq_identity"]
        self.assertEqual(identity["native_nt_seq"], 19_563)
        self.assertEqual(identity["snow_message_id"], _message_id(19_563))
        self.assertEqual(native_nt_sequence("19563"), 19_563)

        for invalid in (True, False, 0, -1, 2**32, 1.0, float("nan"), "+1", " 1", "1.0", "１２"):
            self.assertIsNone(native_nt_sequence(invalid), repr(invalid))
        raw = {
            "message_type": "private",
            "sub_type": "friend",
            "self_id": ACCOUNT,
            "user_id": PEER,
            "time": STAMP,
            "message_id": "1001",
            "message_seq": 19_563,
        }
        self.assertIsNone(
            build_qq_private_identity(
                backend="napcat",
                account_id=ACCOUNT,
                platform="qq",
                session_kind="private",
                session_id=PEER,
                raw=raw,
                original_timestamp=STAMP,
            )
        )

    def test_bidirectional_complete_alias_is_durable_and_preserves_incoming_receipt_scope(self):
        napcat = _delivery("napcat")
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])

        self.assertEqual(self.store.claim(snow), "complete")
        self.assertEqual(self._delivery_count(), 1)
        self.assertEqual(self.store.root_delivery_key(snow["delivery_key"]), napcat["delivery_key"])
        self.assertEqual(snow["receipt_token"], "receipt-snowluma-19563")

        self.store.close()
        reopened = HistoryDeliveryStore(self.path)
        try:
            self.assertEqual(reopened.claim(snow), "complete")
            self.assertEqual(reopened.state(snow["delivery_key"]), "complete")
            self.assertEqual(reopened.root_delivery_key(snow["delivery_key"]), napcat["delivery_key"])
            self.assertEqual(int(reopened._connection().execute("SELECT COUNT(*) FROM history_deliveries").fetchone()[0]), 1)
        finally:
            reopened.close()

    def test_adapter_envelopes_are_revalidated_at_core_delivery_and_claim_seams(self):
        def envelope(delivery):
            return {
                "message_info": {
                    "platform": "qq",
                    "message_id": delivery["message_id"],
                    "time": STAMP,
                    "group_info": None,
                    "user_info": {"platform": "qq", "user_id": PEER},
                    "additional_config": {
                        "offline_recovery": {
                            "original_timestamp": STAMP,
                            "delivery": delivery,
                        }
                    },
                }
            }

        napcat_envelope = envelope(_delivery("napcat"))
        snow_envelope = envelope(_delivery("snowluma"))
        napcat = history_delivery.delivery_from_envelope(napcat_envelope)
        snow = history_delivery.delivery_from_envelope(snow_envelope)
        self.assertIsNotNone(napcat)
        self.assertIsNotNone(snow)
        self.assertEqual(napcat["qq_identity"]["native_nt_seq"], 19_563)
        self.assertNotIn("native_nt_seq", snow["qq_identity"])
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        self.assertEqual(self.store.claim(snow), "complete")
        self.assertEqual(self._delivery_count(), 1)

        malformed = envelope(_delivery("napcat"))
        malformed["message_info"]["additional_config"]["offline_recovery"]["delivery"]["qq_identity"]["snow_message_id"] = "123"
        parsed_malformed = history_delivery.delivery_from_envelope(malformed)
        self.assertIsNotNone(parsed_malformed)
        self.assertNotIn("qq_identity", parsed_malformed)

    def test_pending_alias_resumes_original_root_without_second_delivery_row(self):
        snow = _delivery("snowluma")
        napcat = _delivery("napcat")
        self.assertEqual(self.store.claim(snow), "new")
        self.store.mark_pending(snow["delivery_key"], chat_id="qq:private:19563", row_id=42, original_timestamp=STAMP)

        self.assertEqual(self.store.claim(napcat), "pending")
        self.assertEqual(self.store.root_delivery_key(napcat["delivery_key"]), snow["delivery_key"])
        self.assertEqual(self.store.pending_rows()[0]["delivery_key"], snow["delivery_key"])
        self.assertEqual(self.store.pending_rows()[0]["row_id"], 42)
        self.assertEqual(self._delivery_count(), 1)

    def test_pre_alias_legacy_snow_receipt_is_adopted_by_derived_napcat_id(self):
        snow = _delivery("snowluma", proof=False)
        napcat = _delivery("napcat")
        self.assertEqual(self.store.claim(snow), "new")
        self.store.mark_pending(snow["delivery_key"], chat_id="qq:private:19563", row_id=51, original_timestamp=STAMP)

        self.assertEqual(self.store.claim(napcat), "pending")
        self.assertEqual(self.store.root_delivery_key(napcat["delivery_key"]), snow["delivery_key"])
        self.assertEqual(self.store.pending_rows()[0]["row_id"], 51)
        self.assertEqual(self._delivery_count(), 1)
        scopes = self.store.qq_native_delivery_scopes(napcat)
        self.assertEqual({(item["backend"], item["message_id"]) for item in scopes}, {
            ("napcat", napcat["message_id"]),
            ("snowluma", snow["message_id"]),
        })

    def test_napcat_identity_sync_seeds_legacy_complete_receipt_without_changing_state(self):
        napcat = _delivery("napcat", proof=False)
        napcat["recovered"] = False
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        run = _run("napcat", "identity-seed", "identity-token", "np-seed-instance")
        self.store.begin_history_run(run)

        payload = {
            **run,
            "phase": "identity_sync",
            "identities": [_identity_sync_record(_delivery("napcat"))],
        }
        self.assertEqual(self.store.sync_qq_identities_from_run(payload), 1)
        self.assertEqual(self.store.state(napcat["delivery_key"]), "complete")
        self.assertEqual(self._delivery_count(), 1)
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(snow), "complete")
        self.assertEqual(self.store.root_delivery_key(snow["delivery_key"]), napcat["delivery_key"])
        self.assertEqual(self._delivery_count(), 1)

    def test_identity_sync_rejects_wrong_nonce_target_time_and_unknown_receipt(self):
        napcat = _delivery("napcat", proof=False)
        napcat["recovered"] = False
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        other_peer = _delivery("napcat", peer="19564", proof=False)
        other_peer["recovered"] = False
        self.assertEqual(self.store.claim(other_peer), "new")
        self.store.mark_complete(other_peer["delivery_key"])
        run = _run("napcat", "identity-invalid", "identity-valid-token", "np-seed-instance")
        self.store.begin_history_run(run)

        valid_record = _identity_sync_record(_delivery("napcat"))
        with self.assertRaises(ValueError):
            self.store.sync_qq_identities_from_run(
                {**run, "run_token": "wrong-token", "phase": "identity_sync", "identities": [valid_record]}
            )

        other_record = _identity_sync_record(_delivery("napcat", peer="19564"))
        wrong_identity = {**valid_record["qq_identity"], "peer_id": "19564"}
        wrong_scope_record = _identity_sync_record(_delivery("napcat"), identity=wrong_identity)
        wrong_time_delivery = _delivery("napcat", stamp=STAMP + 1)
        wrong_time_record = _identity_sync_record(wrong_time_delivery)
        unknown_record = _identity_sync_record(_delivery("napcat", message_id="990001"))
        payload = {
            **run,
            "phase": "identity_sync",
            "identities": [other_record, wrong_scope_record, wrong_time_record, unknown_record],
        }
        self.assertEqual(self.store.sync_qq_identities_from_run(payload), 0)
        self.assertEqual(
            int(self.store._connection().execute("SELECT COUNT(*) FROM qq_history_aliases").fetchone()[0]),
            0,
        )
        self.assertEqual(self._delivery_count(), 2)

    def test_exact_napcat_replay_lazily_binds_snow_alias(self):
        unproven = _delivery("napcat", proof=False)
        napcat = _delivery("napcat")
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(unproven), "new")
        self.store.mark_complete(unproven["delivery_key"])
        self.assertEqual(self.store.claim(napcat), "complete")
        self.assertEqual(self.store.claim(snow), "complete")
        self.assertEqual(self.store.root_delivery_key(snow["delivery_key"]), napcat["delivery_key"])
        self.assertEqual(self._delivery_count(), 1)

    def test_missing_or_wrong_timestamp_proof_detaches_virtual_key_before_pending(self):
        napcat = _delivery("napcat")
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        self.assertEqual(self.store.claim(snow), "complete")

        wrong_time = dict(snow)
        wrong_time["original_timestamp"] = STAMP + 1
        self.assertIsNone(
            validate_qq_private_identity(
                wrong_time["qq_identity"],
                backend=wrong_time["backend"],
                account_id=wrong_time["account_id"],
                platform=wrong_time["platform"],
                session_kind=wrong_time["session_kind"],
                session_id=wrong_time["session_id"],
                message_id=wrong_time["message_id"],
                original_timestamp=wrong_time["original_timestamp"],
            )
        )
        wrong_time.pop("qq_identity")
        self.assertEqual(self.store.claim(wrong_time), "new")
        self.assertEqual(self.store.root_delivery_key(snow["delivery_key"]), snow["delivery_key"])
        self.store.mark_pending(snow["delivery_key"], chat_id="qq:private:19563", row_id=62, original_timestamp=STAMP + 1)
        self.assertEqual(self.store.state(snow["delivery_key"]), "pending")
        self.assertEqual(self.store.pending_rows()[0]["delivery_key"], snow["delivery_key"])
        self.assertEqual(self._delivery_count(), 2)

    def test_unproven_virtual_snow_key_cannot_cross_ack_and_owns_new_pending_root(self):
        napcat = _delivery("napcat")
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        self.assertEqual(self.store.claim(snow), "complete")

        unproven_snow = _delivery("snowluma", proof=False)
        self.assertEqual(unproven_snow["delivery_key"], snow["delivery_key"])
        self.assertEqual(self.store.claim(unproven_snow), "new")
        self.assertEqual(self.store.root_delivery_key(unproven_snow["delivery_key"]), unproven_snow["delivery_key"])
        self.store.mark_pending(unproven_snow["delivery_key"], chat_id="qq:private:19563", row_id=63, original_timestamp=STAMP)
        self.assertEqual(self.store.state(unproven_snow["delivery_key"]), "pending")
        self.assertEqual(self._delivery_count(), 2)

    def test_processing_family_returns_busy_without_creating_another_row(self):
        napcat = _delivery("napcat")
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(napcat), "new")
        other_owner = HistoryDeliveryStore(self.path)
        try:
            self.assertEqual(other_owner.claim(snow), "busy")
            self.assertEqual(int(other_owner._connection().execute("SELECT COUNT(*) FROM history_deliveries").fetchone()[0]), 1)
        finally:
            other_owner.close()

    def test_same_text_and_second_but_distinct_native_sequence_remain_distinct(self):
        first = _delivery("napcat", 19_563)
        second = _delivery("napcat", 19_564)
        other_snow = _delivery("snowluma", 19_564)
        self.assertEqual(self.store.claim(first), "new")
        self.store.mark_complete(first["delivery_key"])
        self.assertEqual(self.store.claim(second), "new")
        self.store.mark_complete(second["delivery_key"])
        self.assertEqual(self.store.claim(other_snow), "complete")
        self.assertEqual(self._delivery_count(), 2)
        self.assertNotEqual(first["qq_identity"]["snow_message_id"], second["qq_identity"]["snow_message_id"])

    def test_account_peer_route_and_ambiguous_hash_are_isolated(self):
        napcat = _delivery("napcat")
        self.assertEqual(self.store.claim(napcat), "new")
        self.store.mark_complete(napcat["delivery_key"])
        self.assertEqual(self.store.claim(_delivery("snowluma", account="2955610492")), "new")
        self.assertEqual(self.store.claim(_delivery("snowluma", peer="19564")), "new")
        self.assertEqual(self._delivery_count(), 3)

        other = _delivery("snowluma", sequence=19_565)
        self.store._connection().execute(
            """INSERT INTO qq_history_aliases(alias_kind, alias_key, family_id, ambiguous)
               VALUES('snow', ?, NULL, 1) ON CONFLICT(alias_kind, alias_key)
               DO UPDATE SET family_id=NULL, ambiguous=1""",
            (json.dumps((ACCOUNT, "qq", PEER, other["qq_identity"]["snow_message_id"]), separators=(",", ":")),),
        )
        self.assertEqual(self.store.claim(other), "new")
        self.assertEqual(self.store.root_delivery_key(other["delivery_key"]), other["delivery_key"])

    def test_napcat_open_run_pending_hands_off_only_on_new_snow_seal(self):
        nap_run = _run("napcat", "run-nap", "token-nap", "nap-instance")
        self.store.begin_history_run(nap_run)
        nap = _delivery("napcat")
        nap["recovered"] = True
        nap["history_run"] = {"run_id": "run-nap", "cutoff_at": STAMP + 300}
        self.assertEqual(self.store.claim(nap), "new")
        self.store.mark_pending(nap["delivery_key"], chat_id="qq:private:19563", row_id=70, original_timestamp=STAMP)

        snow_run = _run("snowluma", "run-snow", "token-snow", "snow-instance")
        self.store.begin_history_run(snow_run)
        retired = self.store.release_empty_open_targets_from_older_instances(
            backend="snowluma",
            account_id=ACCOUNT,
            platform="qq",
            source_instance="snow-instance",
        )
        self.assertNotIn(("run-nap", "private", PEER), retired)
        self.assertTrue(self.store.history_target_blocked("run-nap", "private", PEER))

        sealed = {**snow_run, "phase": "seal"}
        closed = self.store.seal_history_run(sealed)
        self.assertIn(("run-nap", "private", PEER), closed)
        self.assertIn(("run-snow", "private", PEER), closed)
        self.assertFalse(self.store.history_target_blocked("run-nap", "private", PEER))
        self.assertFalse(self.store.history_target_blocked("run-snow", "private", PEER))
        self.assertEqual(self.store.pending_rows()[0]["delivery_key"], nap["delivery_key"])
        snow = _delivery("snowluma")
        self.assertEqual(self.store.claim(snow), "pending")
        self.assertEqual(self.store.root_delivery_key(snow["delivery_key"]), nap["delivery_key"])
        self.assertEqual(self._delivery_count(), 1)

    def test_storage_and_bot_repair_cross_adapter_message_insert_crash_window(self):
        napcat = _delivery("napcat")
        napcat["recovered"] = True
        self.assertEqual(self.store.claim(napcat, now=100), "new")
        _Messages.rows = [self._stored_core_message(napcat, row_id=81)]
        self.store.close()

        restarted = HistoryDeliveryStore(self.path, processing_lease_sec=1)
        snow = _delivery("snowluma")
        snow["recovered"] = True
        self.assertEqual(restarted.state(napcat["delivery_key"]), "processing")
        find_existing = _load_storage_ingress(restarted)
        found = find_existing(snow)
        self.assertIsNotNone(found)
        self.assertEqual(found.row_id, 81)

        receipts: list[tuple[dict, str]] = []
        restores: list[str] = []
        message_process = _load_bot_message_process(
            store=restarted,
            delivery=snow,
            find_existing_ingress=find_existing,
            receipts=receipts,
            restores=restores,
        )
        asyncio.run(message_process(SimpleNamespace(), {"test": "adapter-envelope"}))

        self.assertEqual(restarted.state(snow["delivery_key"]), "pending")
        self.assertEqual(restarted.root_delivery_key(snow["delivery_key"]), napcat["delivery_key"])
        self.assertEqual(restarted.pending_rows()[0]["row_id"], 81)
        self.assertEqual(int(restarted._connection().execute("SELECT COUNT(*) FROM history_deliveries").fetchone()[0]), 1)
        self.assertEqual(restores, [napcat["delivery_key"]])
        self.assertEqual(len(receipts), 1)
        self.assertIs(receipts[0][0], snow)
        self.assertEqual((receipts[0][0]["delivery_key"], receipts[0][0]["receipt_token"], receipts[0][1]),
                         (snow["delivery_key"], snow["receipt_token"], "accepted"))
        restarted.close()

    def test_message_sending_serializes_confirmed_scoped_reply_aliases(self):
        source = _delivery("napcat")
        aliases = {"napcat": source["message_id"], "snowluma": source["qq_identity"]["snow_message_id"]}

        class _ReplyStore:
            def qq_reply_identity(self, delivery_key, **scope):
                self.request = (delivery_key, scope)
                return {
                    "version": 1,
                    "scope": "qq_private_friend",
                    "account_id": ACCOUNT,
                    "platform": "qq",
                    "peer_id": PEER,
                    "original_reply_id": source["message_id"],
                    "native_ids": aliases,
                }

        fake_store = _ReplyStore()
        sending_type = _load_message_sending_to_dict(fake_store)
        reply = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id=source["message_id"],
                platform="qq",
                time=STAMP,
                user_info=SimpleNamespace(platform="qq", user_id=PEER),
                additional_config={
                    "offline_recovery": {
                        "original_timestamp": STAMP,
                        "delivery": source,
                    }
                },
            )
        )
        target = SimpleNamespace(user_id=PEER, to_dict=lambda: {"platform": "qq", "user_id": PEER})
        sending = sending_type.__new__(sending_type)
        sending.chat_stream = SimpleNamespace(platform="qq", group_info=None, user_info=target)
        sending.message_info = SimpleNamespace(additional_config={"sentinel": True})
        sending.reply = reply
        sending.reply_to_message_id = source["message_id"]

        result = sending.to_dict()
        config = result["message_info"]["additional_config"]
        self.assertEqual(config["sentinel"], True)
        self.assertEqual(config["reply_to_message_id"], source["message_id"])
        self.assertEqual(config["qq_reply_identity"]["native_ids"], aliases)
        self.assertEqual(fake_store.request[0], source["delivery_key"])
        self.assertEqual(fake_store.request[1]["original_reply_id"], source["message_id"])

    def test_legacy_snow_pending_source_uses_family_confirmed_napcat_reply_alias(self):
        source = _delivery("snowluma", proof=False)
        alias_id = str(450_563)
        alias_map = {"snowluma": source["message_id"], "napcat": alias_id}

        class _LegacyReplyStore:
            def qq_reply_identity(self, delivery_key, **scope):
                self.request = (delivery_key, scope)
                return {
                    "version": 1,
                    "scope": "qq_private_friend",
                    "account_id": ACCOUNT,
                    "platform": "qq",
                    "peer_id": PEER,
                    "original_reply_id": source["message_id"],
                    "native_ids": alias_map,
                }

        fake_store = _LegacyReplyStore()
        sending_type = _load_message_sending_to_dict(fake_store)
        reply = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id=source["message_id"],
                platform="qq",
                time=STAMP,
                user_info=SimpleNamespace(platform="qq", user_id=PEER),
                additional_config={
                    "offline_recovery": {
                        "original_timestamp": STAMP,
                        "delivery": source,
                    }
                },
            )
        )
        target = SimpleNamespace(user_id=PEER, to_dict=lambda: {"platform": "qq", "user_id": PEER})
        sending = sending_type.__new__(sending_type)
        sending.chat_stream = SimpleNamespace(platform="qq", group_info=None, user_info=target)
        sending.message_info = SimpleNamespace(additional_config=None)
        sending.reply = reply
        sending.reply_to_message_id = source["message_id"]

        result = sending.to_dict()
        translated = result["message_info"]["additional_config"]["qq_reply_identity"]
        self.assertEqual(translated["native_ids"], alias_map)
        self.assertEqual(translated["original_reply_id"], source["message_id"])


if __name__ == "__main__":
    unittest.main()
