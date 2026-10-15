from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import service.snowluma_identity as identity_module
from service.rpc import OneBotClient, OneBotError
from service.snowluma_identity import SnowLumaIdentityError, SnowLumaOneBotClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from webUI.snowluma_locator import resolve_snowluma_runtime

ACCOUNT = "2955610491"
UID = "u_" + "a" * 22
OTHER_UID = "u_" + "b" * 22
PEER_UIN_1 = "214"
PEER_UIN_2 = "10003"
PEER_UID_1 = "u_" + "c" * 22
PEER_UID_2 = "u_" + "d" * 22
DATALINE_VIRTUAL_CONTACTS = (
    (2113929217, "我的电脑"),
    (2113929218, "我的手机"),
    (2113929219, "我的Pad"),
)


def _fixture_uid(index: int) -> str:
    return "u_" + f"{index:022d}"


def _create_db(path: Path, rows: list[tuple[object, object]], *, wal: bool = True, unique: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode = WAL")
        uid_constraint = " UNIQUE" if unique else ""
        uin_constraint = " UNIQUE" if unique else ""
        connection.execute(
            "CREATE TABLE users (uid TEXT" + uid_constraint + ", uin INTEGER" + uin_constraint + ", "
            "nickname TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '', "
            "is_friend INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO users (uid, uin, updated_at) VALUES (?, ?, ?)",
            [(uid, uin, 1) for uid, uin in rows],
        )
        connection.commit()
    finally:
        connection.close()


class SnowLumaIdentityClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "SnowLuma-v1.14.17-win-x64"
        self.runtime.mkdir()
        (self.runtime / "package.json").write_text(json.dumps({"version": "1.14.17"}), encoding="utf-8")
        self.database = self.runtime / "data" / ACCOUNT / "snowluma_identity.db"
        _create_db(self.database, [(UID, int(ACCOUNT))])
        self.client = self._client()

    def _client(self, *, account_id: str = ACCOUNT, timeout: float = 3.0) -> SnowLumaOneBotClient:
        client = SnowLumaOneBotClient(
            "ws://127.0.0.1:3001/",
            "private-token-fixture",
            account_id=account_id,
            project_root=self.root,
            timeout=timeout,
        )
        # Use the repository's existing resolver against each isolated runtime
        # fixture, without importing modules from a second workspace root.
        client._trusted_resolver = lambda _root, _deadline: resolve_snowluma_runtime
        return client

    def _rpc(self, data: dict[str, object]):
        return patch.object(OneBotClient, "call", return_value=data)

    @staticmethod
    def _dataline_rows() -> list[dict[str, object]]:
        return [
            {
                "user_id": uin if index == 0 else str(uin),
                "nickname": name,
                "remark": name,
            }
            for index, (uin, name) in enumerate(DATALINE_VIRTUAL_CONTACTS)
        ]

    @staticmethod
    def _discovery_supervisor():
        from service.manager import Supervisor, _MAX_CONCURRENT_PEER_IDENTITY_RPCS

        supervisor = Supervisor.__new__(Supervisor)
        supervisor.config = {"account_id": ACCOUNT}
        supervisor.monotonic_clock = time.monotonic
        supervisor.identity_discovery_timeout_seconds = 5.0
        supervisor._peer_rpc_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_PEER_IDENTITY_RPCS)
        supervisor._peer_rpc_futures_lock = threading.Lock()
        supervisor._peer_rpc_futures = set()
        return supervisor

    def test_successful_exact_uin_response_gets_cached_uid_without_freshness_claim(self) -> None:
        before = self.database.read_bytes()
        with self._rpc({"user_id": ACCOUNT}) as rpc:
            result = self.client.call("get_stranger_info", {"user_id": ACCOUNT}, timeout=1.0)

        self.assertEqual(result, {"user_id": ACCOUNT, "uid": UID})
        self.assertEqual(rpc.call_args.args[:2], ("get_stranger_info", {"user_id": ACCOUNT}))
        self.assertLessEqual(rpc.call_args.kwargs["timeout"], 1.0)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.client._database_path, self.database)

    def test_resolve_self_uid_reads_only_the_configured_account_cache_without_rpc(self) -> None:
        with patch.object(
            OneBotClient,
            "call",
            side_effect=AssertionError("self UID cache lookup must not issue a OneBot RPC"),
        ) as rpc:
            result = self.client.resolve_self_uid(timeout=0.5)

        self.assertEqual(result, UID)
        rpc.assert_not_called()
        self.assertEqual(self.client._database_path, self.database)

    def test_resolve_self_uid_rejects_invalid_timeouts_before_runtime_or_cache_access(self) -> None:
        invalid_timeouts = (True, 0, -1, 31, float("nan"), float("inf"))
        with (
            patch.object(self.client, "_current_runtime", side_effect=AssertionError("must not resolve runtime")),
            patch.object(self.client, "_cached_uid", side_effect=AssertionError("must not read cache")),
        ):
            for timeout in invalid_timeouts:
                with self.subTest(timeout=timeout), self.assertRaises(OneBotError) as caught:
                    self.client.resolve_self_uid(timeout=timeout)
                self.assertEqual(caught.exception.code, "onebot_timeout_invalid")

    def test_resolve_self_uid_propagates_cache_timeout_without_rpc_fallback(self) -> None:
        with (
            patch.object(self.client, "_current_runtime", return_value=self.runtime),
            patch.object(
                self.client,
                "_cached_uid",
                side_effect=SnowLumaIdentityError("snowluma_identity_timeout"),
            ),
            patch.object(
                OneBotClient,
                "call",
                side_effect=AssertionError("self UID cache failure must not fall back to profile RPC"),
            ) as rpc,
            self.assertRaises(SnowLumaIdentityError) as caught,
        ):
            self.client.resolve_self_uid(timeout=0.5)

        self.assertEqual(caught.exception.code, "snowluma_identity_timeout")
        rpc.assert_not_called()

    def test_stale_timestamp_and_source_are_ignored_because_the_row_is_cached_identity(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE users SET updated_at = 1, source = 'profile' WHERE uin = ?", (int(ACCOUNT),))
            connection.commit()
        finally:
            connection.close()
        with self._rpc({"user_id": ACCOUNT}) as _rpc:
            result = self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(result["uid"], UID)

    def test_self_and_peer_uids_all_come_from_the_authenticated_bot_account_cache(self) -> None:
        _create_db(
            self.database,
            [
                (UID, int(ACCOUNT)),
                (PEER_UID_1, int(PEER_UIN_1)),
                (PEER_UID_2, int(PEER_UIN_2)),
            ],
        )
        # Conflicting per-peer databases must be irrelevant: SnowLuma's
        # account cache is selected by the authenticated bot account, while
        # each requested UIN is only a lookup key within that one database.
        foreign_databases = []
        for peer_uin in (PEER_UIN_1, PEER_UIN_2):
            foreign_database = self.runtime / "data" / peer_uin / "snowluma_identity.db"
            _create_db(foreign_database, [(OTHER_UID, int(peer_uin))])
            foreign_databases.append(foreign_database)

        original_connect = sqlite3.connect
        opened_databases: list[str] = []

        def recording_connect(database, *args, **kwargs):
            opened_databases.append(str(database))
            return original_connect(database, *args, **kwargs)

        def rpc_response(action, params=None, **_kwargs):
            self.assertEqual(action, "get_stranger_info")
            return {"user_id": params["user_id"]}

        with (
            patch.object(OneBotClient, "call", side_effect=rpc_response),
            patch.object(identity_module.sqlite3, "connect", side_effect=recording_connect),
        ):
            self.assertEqual(
                self.client.call("get_stranger_info", {"user_id": ACCOUNT})["uid"],
                UID,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                peer_futures = [
                    executor.submit(
                        self.client.call,
                        "get_stranger_info",
                        {"user_id": peer_uin},
                    )
                    for peer_uin in (PEER_UIN_1, PEER_UIN_2)
                ]
                peer_results = [future.result(timeout=2.0) for future in peer_futures]

        self.assertEqual([result["uid"] for result in peer_results], [PEER_UID_1, PEER_UID_2])
        self.assertEqual(len(opened_databases), 3)
        expected_uri = self.database.as_uri() + "?mode=ro"
        self.assertEqual(opened_databases, [expected_uri] * 3)
        self.assertTrue(all(str(path) not in "\n".join(opened_databases) for path in foreign_databases))
        self.assertEqual(self.client._database_path, self.database)

    def test_wal_reader_uses_existing_uncheckpointed_cache_rows(self) -> None:
        writer = sqlite3.connect(self.database)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("DELETE FROM users")
        writer.execute("INSERT INTO users (uid, uin, updated_at) VALUES (?, ?, 1)", (UID, int(ACCOUNT)))
        writer.commit()
        self.assertTrue(Path(str(self.database) + "-wal").exists())
        try:
            with self._rpc({"user_id": ACCOUNT}):
                result = self.client.call("get_stranger_info", {"user_id": ACCOUNT})
            self.assertEqual(result["uid"], UID)
        finally:
            writer.close()

    def test_non_identity_actions_and_friend_list_keep_base_client_behavior(self) -> None:
        with patch.object(OneBotClient, "call", return_value={"online": True}) as call:
            self.assertEqual(self.client.call("get_status"), {"online": True})
            call.assert_called_once_with("get_status", None, timeout=None)

        limiter = threading.BoundedSemaphore(4)
        self.client.set_http_transport_limiter(limiter)
        self.assertIs(self.client._http_transport_slots, limiter)
        with patch.object(OneBotClient, "get_friend_list", return_value=[]) as friends:
            self.assertEqual(self.client.get_friend_list(timeout=0.5), [])
            friends.assert_called_once_with(timeout=0.5)
        with self.assertRaises(OneBotError):
            self.client.call("unsupported")

    def test_friend_list_filters_only_exact_dataline_virtual_rows(self) -> None:
        expected_markers = {str(uin): name for uin, name in DATALINE_VIRTUAL_CONTACTS}
        self.assertTrue(identity_module._DATALINE_VIRTUAL_FRIENDS == expected_markers)
        device_rows = self._dataline_rows()
        lookalikes = [
            {"user_id": "70001", "nickname": "我的电脑", "remark": "我的电脑"},
            {"user_id": device_rows[0]["user_id"], "nickname": "我的电脑", "remark": "renamed"},
            {**device_rows[0], "uid": "fixture-uid"},
            {**device_rows[1], "uid": "fixture-uid-a", "userUid": "fixture-uid-b"},
            {**device_rows[2], "account_id": device_rows[2]["user_id"]},
            {**device_rows[0], "user_id": "0" + str(DATALINE_VIRTUAL_CONTACTS[0][0])},
            {"user_id": "2113929220", "nickname": "我的Pad", "remark": "我的Pad"},
            {"user_id": None, "nickname": "我的手机", "remark": "我的手机"},
        ]
        rows = [
            lookalikes[0],
            device_rows[0],
            lookalikes[1],
            device_rows[1],
            lookalikes[2],
            device_rows[2],
            *lookalikes[3:],
        ]
        original_rows = [dict(row) for row in rows]
        expected = [row for row in rows if all(row is not device for device in device_rows)]

        with patch.object(OneBotClient, "get_friend_list", return_value=rows) as friend_rpc:
            filtered = self.client.get_friend_list(timeout=0.75)

        friend_rpc.assert_called_once_with(timeout=0.75)
        self.assertEqual(len(filtered), len(rows) - len(device_rows))
        self.assertTrue(all(actual is wanted for actual, wanted in zip(filtered, expected)))
        self.assertTrue(all(row == original for row, original in zip(rows, original_rows)))

    def test_manager_discovery_looks_up_only_real_peers_after_filter(self) -> None:
        supervisor = self._discovery_supervisor()
        real_uins = [str(700000 + index) for index in range(32)]
        uid_by_uin = {uin: _fixture_uid(index + 1) for index, uin in enumerate(real_uins)}
        rows = [
            {"user_id": uin, "nickname": f"fixture-{index}", "remark": ""}
            for index, uin in enumerate(real_uins)
        ] + self._dataline_rows()

        def profile_rpc(_client, action, params=None, *, timeout=None):
            if action != "get_stranger_info" or type(params) is not dict:
                raise AssertionError("unexpected identity action")
            uin = params.get("user_id")
            if uin not in uid_by_uin:
                raise AssertionError("virtual or unknown contact reached UID lookup")
            return {"user_id": uin, "uid": uid_by_uin[uin]}

        with (
            patch.object(OneBotClient, "get_friend_list", return_value=rows) as friend_rpc,
            patch.object(OneBotClient, "call", autospec=True, side_effect=profile_rpc) as profile_rpcs,
        ):
            peer_map = supervisor._discover_peer_map(
                self.client,
                UID,
                time.monotonic() + supervisor.identity_discovery_timeout_seconds,
            )

        friend_rpc.assert_called_once()
        self.assertEqual(profile_rpcs.call_count, len(real_uins))
        self.assertTrue(all(call.args[2]["user_id"] in uid_by_uin for call in profile_rpcs.call_args_list))
        self.assertEqual(len(peer_map), len(real_uins))
        self.assertTrue(all(peer_map.get(uid_by_uin[uin]) == uin for uin in real_uins))

    def test_manager_still_fails_closed_for_real_reserved_id_lookalike_without_uid(self) -> None:
        from service.manager import ServiceError

        supervisor = self._discovery_supervisor()
        real_row = {
            "user_id": str(DATALINE_VIRTUAL_CONTACTS[0][0]),
            "nickname": "fixture contact",
            "remark": "different metadata",
        }
        rows = [real_row, *self._dataline_rows()]
        with (
            patch.object(OneBotClient, "get_friend_list", return_value=rows),
            patch.object(self.client, "call", return_value={"user_id": real_row["user_id"]}),
            self.assertRaises(ServiceError) as caught,
        ):
            supervisor._discover_peer_map(
                self.client,
                UID,
                time.monotonic() + supervisor.identity_discovery_timeout_seconds,
            )
        self.assertEqual(caught.exception.code, "friend_uid_unavailable")

    def test_mismatched_or_nonnumeric_rpc_uin_never_uses_the_cache(self) -> None:
        with (
            self._rpc({"user_id": "2955610492"}),
            patch.object(self.client, "_current_runtime", side_effect=AssertionError("must not read cache")),
        ):
            result = self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(result, {"user_id": "2955610492"})

        with (
            self._rpc({"user_id": "not-a-uin"}),
            patch.object(self.client, "_current_runtime", side_effect=AssertionError("must not read cache")),
        ):
            result = self.client.call("get_stranger_info", {"user_id": "not-a-uin"})
        self.assertEqual(result, {"user_id": "not-a-uin"})

    def test_existing_valid_uid_is_preserved_without_cache_lookup(self) -> None:
        rpc_data = {"user_id": ACCOUNT, "user_uid": OTHER_UID}
        with (
            self._rpc(rpc_data),
            patch.object(self.client, "_current_runtime", side_effect=AssertionError("must not read cache")),
        ):
            result = self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertIs(result, rpc_data)

    def test_existing_malformed_or_conflicting_uids_are_rejected_not_overwritten(self) -> None:
        cases = (
            ({"user_id": ACCOUNT, "uid": "malformed"}, "snowluma_identity_uid_invalid"),
            ({"user_id": ACCOUNT, "uid": UID, "user_uid": OTHER_UID}, "snowluma_identity_uid_conflict"),
            ({"user_id": ACCOUNT, "uid": 123}, "snowluma_identity_uid_invalid"),
        )
        for data, expected in cases:
            with self.subTest(expected=expected), self._rpc(data), self.assertRaises(SnowLumaIdentityError) as caught:
                self.client.call("get_stranger_info", {"user_id": ACCOUNT})
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(str(caught.exception), expected)

    def test_missing_database_and_unknown_uin_fail_with_safe_codes(self) -> None:
        self.database.unlink()
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as missing:
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(missing.exception.code, "snowluma_identity_cache_unavailable")

        _create_db(self.database, [])
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as changed:
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(changed.exception.code, "snowluma_identity_cache_changed")

        client = self._client()
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as unknown:
            client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(unknown.exception.code, "snowluma_identity_uid_unavailable")

    def test_malformed_cached_uid_and_ambiguous_mappings_fail_closed(self) -> None:
        _create_db(self.database, [("bad-uid", int(ACCOUNT))])
        client = self._client()
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as malformed:
            client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(malformed.exception.code, "snowluma_identity_uid_invalid")

        _create_db(self.database, [(UID, int(ACCOUNT)), (OTHER_UID, int(ACCOUNT))], unique=False)
        client = self._client()
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as duplicate_uin:
            client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(duplicate_uin.exception.code, "snowluma_identity_ambiguous")

        _create_db(self.database, [(UID, int(ACCOUNT)), (UID, int(ACCOUNT) + 1)], unique=False)
        client = self._client()
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as duplicate_uid:
            client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(duplicate_uid.exception.code, "snowluma_identity_ambiguous")

    def test_account_and_cached_uin_must_fit_canonical_bounds(self) -> None:
        with self.assertRaises(SnowLumaIdentityError) as invalid_account:
            self._client(account_id="../outside")
        self.assertEqual(invalid_account.exception.code, "snowluma_identity_uin_invalid")

        self.client._account_id = "9223372036854775808"
        with self._rpc({"user_id": "9223372036854775808"}), self.assertRaises(SnowLumaIdentityError) as too_large:
            self.client.call("get_stranger_info", {"user_id": "9223372036854775808"})
        self.assertEqual(too_large.exception.code, "snowluma_identity_uin_invalid")

    def test_cache_file_replacement_is_detected_after_it_was_pinned(self) -> None:
        with self._rpc({"user_id": ACCOUNT}):
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        saved = self.database.with_name("snowluma_identity.saved.db")
        self.database.rename(saved)
        _create_db(self.database, [(OTHER_UID, int(ACCOUNT))])

        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as caught:
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(caught.exception.code, "snowluma_identity_cache_changed")

    def test_database_is_opened_read_only_and_runtime_change_fails_closed(self) -> None:
        original_connect = sqlite3.connect
        calls: list[tuple[str, bool]] = []

        def recording_connect(database, *args, **kwargs):
            calls.append((str(database), kwargs.get("uri", False)))
            return original_connect(database, *args, **kwargs)

        with patch.object(identity_module.sqlite3, "connect", side_effect=recording_connect), self._rpc({"user_id": ACCOUNT}):
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(len(calls), 1)
        self.assertIn("mode=ro", calls[0][0])
        self.assertTrue(calls[0][1])

        readonly = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(readonly.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("UPDATE users SET updated_at = 2 WHERE uin = ?", (int(ACCOUNT),))
        finally:
            readonly.close()

        second = self.root / "SnowLuma-v1.14.17-win-x64-copy"
        second.mkdir()
        (second / "package.json").write_text(json.dumps({"version": "1.14.17"}), encoding="utf-8")
        with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as changed:
            self.client.call("get_stranger_info", {"user_id": ACCOUNT})
        self.assertEqual(changed.exception.code, "snowluma_identity_runtime_unavailable")

    def test_database_lock_wait_obeys_the_caller_deadline(self) -> None:
        writer = sqlite3.connect(self.database)
        try:
            writer.execute("PRAGMA journal_mode = DELETE")
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with self._rpc({"user_id": ACCOUNT}), self.assertRaises(SnowLumaIdentityError) as caught:
                self.client.call("get_stranger_info", {"user_id": ACCOUNT}, timeout=0.1)
            elapsed = time.monotonic() - started
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(caught.exception.code, "snowluma_identity_timeout")
        self.assertLess(elapsed, 1.0)

    def test_sqlite_progress_handler_interrupts_a_query_past_the_deadline(self) -> None:
        original_connect = sqlite3.connect
        clock = [100.0]

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def set_progress_handler(self, callback, operations):
                return self.connection.set_progress_handler(callback, operations)

            def execute(self, sql, parameters=()):
                if sql.startswith("SELECT uid, uin"):
                    clock[0] += 2.0
                return self.connection.execute(sql, parameters)

            def close(self):
                return self.connection.close()

        def wrapped_connect(*args, **kwargs):
            return ConnectionProxy(original_connect(*args, **kwargs))

        with (
            patch.object(identity_module.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(identity_module.sqlite3, "connect", side_effect=wrapped_connect),
            patch.object(identity_module, "_PROGRESS_OPS", 1),
            self._rpc({"user_id": ACCOUNT}),
            self.assertRaises(SnowLumaIdentityError) as caught,
        ):
            self.client.call("get_stranger_info", {"user_id": ACCOUNT}, timeout=1.0)
        self.assertEqual(caught.exception.code, "snowluma_identity_timeout")


if __name__ == "__main__":
    unittest.main()
