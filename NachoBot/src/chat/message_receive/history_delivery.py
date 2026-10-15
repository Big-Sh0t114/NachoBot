"""Durable identity receipts for adapter-initiated history delivery.

This deliberately uses a small SQLite file separate from Core's reply and
Focus databases.  Importing the module does not open the database; the first
delivery or startup-restore call creates it under Core's ignored ``data`` dir.
"""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ncnk_message.qq_history_identity import (
    qq_canonical_identity_key,
    qq_snow_alias_key,
    validate_qq_private_identity,
)


_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PATH = _ROOT / "data" / "offline_history_delivery.sqlite3"


def _positive_finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def delivery_from_envelope(envelope: Any) -> dict[str, Any] | None:
    """Extract and validate the adapter's additive message identity metadata."""

    if not isinstance(envelope, dict):
        return None
    info = envelope.get("message_info")
    if not isinstance(info, dict):
        return None
    additional = info.get("additional_config")
    if isinstance(additional, str):
        try:
            additional = json.loads(additional)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(additional, dict):
        return None
    recovery = additional.get("offline_recovery")
    if not isinstance(recovery, dict):
        return None
    delivery = recovery.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("version") != 1:
        return None
    backend = str(delivery.get("backend") or "").strip()
    account_id = str(delivery.get("account_id") or "").strip()
    platform = str(delivery.get("platform") or "").strip()
    kind = str(delivery.get("session_kind") or "").strip()
    session_id = str(delivery.get("session_id") or "").strip()
    message_id = str(delivery.get("message_id") or "").strip()
    key = str(delivery.get("delivery_key") or "").strip()
    if (
        not backend
        or not account_id
        or not platform
        or kind not in {"group", "private"}
        or not session_id
        or not message_id
        or not key
        or platform != str(info.get("platform") or "")
    ):
        return None
    route = info.get("group_info") if kind == "group" else info.get("user_info")
    route_id = route.get("group_id" if kind == "group" else "user_id") if isinstance(route, dict) else None
    if route_id is None or str(route_id).strip() != session_id:
        return None
    identity = json.dumps(
        (backend, account_id, platform, kind, session_id, message_id),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if hashlib.sha256(identity.encode("utf-8")).hexdigest() != key:
        return None
    if str(info.get("message_id") or "").strip() != message_id:
        return None
    history_run = recovery.get("history_run")
    original_timestamp = _positive_finite(recovery.get("original_timestamp"))
    if bool(delivery.get("recovered")):
        cutoff = _positive_finite(history_run.get("cutoff_at")) if isinstance(history_run, dict) else None
        run_id = history_run.get("run_id") if isinstance(history_run, dict) else None
        if (
            original_timestamp is None
            or cutoff is None
            or original_timestamp > cutoff
            or not isinstance(run_id, str)
            or not run_id.strip()
        ):
            return None
    result = {
        "backend": backend,
        "account_id": account_id,
        "platform": platform,
        "session_kind": kind,
        "session_id": session_id,
        "message_id": message_id,
        "delivery_key": key,
        "receipt_token": str(delivery.get("receipt_token") or "").strip(),
        "recovered": bool(delivery.get("recovered")),
        "original_timestamp": original_timestamp,
        "history_run": history_run if isinstance(history_run, dict) else None,
    }
    qq_identity = validate_qq_private_identity(
        delivery.get("qq_identity"),
        backend=backend,
        account_id=account_id,
        platform=platform,
        session_kind=kind,
        session_id=session_id,
        message_id=message_id,
        original_timestamp=original_timestamp,
    )
    if qq_identity is not None:
        result["qq_identity"] = qq_identity
    return result


class HistoryDeliveryStore:
    """Atomic Core identity state and durable pending recovery row index."""

    def __init__(self, path: str | Path = _DEFAULT_PATH, *, processing_lease_sec: float = 120.0):
        self.path = Path(path)
        self.processing_lease_sec = max(1.0, float(processing_lease_sec))
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.owner_id = __import__("uuid").uuid4().hex

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            try:
                db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                if existed:
                    check = db.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        db.close()
                        raise RuntimeError("Core offline history delivery DB failed integrity check")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS history_deliveries (
                        delivery_key TEXT PRIMARY KEY,
                        backend TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        session_kind TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        recovered INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        owner_id TEXT,
                        chat_id TEXT,
                        row_id INTEGER,
                        original_timestamp REAL,
                        history_run_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS history_deliveries_pending_rows
                      ON history_deliveries(state, chat_id, row_id);
                    CREATE TABLE IF NOT EXISTS history_runs (
                        run_id TEXT PRIMARY KEY,
                        run_token TEXT NOT NULL,
                        backend TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        source_instance TEXT NOT NULL,
                        cutoff_at REAL NOT NULL,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS qq_history_families (
                        family_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        root_delivery_key TEXT NOT NULL UNIQUE,
                        account_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        peer_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        original_timestamp REAL NOT NULL,
                        snow_message_id TEXT NOT NULL,
                        native_nt_seq INTEGER,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS qq_history_aliases (
                        alias_kind TEXT NOT NULL,
                        alias_key TEXT NOT NULL,
                        family_id INTEGER,
                        ambiguous INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (alias_kind, alias_key)
                    );
                    CREATE TABLE IF NOT EXISTS qq_history_delivery_keys (
                        delivery_key TEXT PRIMARY KEY,
                        family_id INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS qq_history_native_ids (
                        family_id INTEGER NOT NULL,
                        backend TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        PRIMARY KEY (family_id, backend)
                    );
                    CREATE TABLE IF NOT EXISTS history_run_targets (
                        run_id TEXT NOT NULL,
                        session_kind TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'open',
                        PRIMARY KEY (run_id, session_kind, session_id),
                        FOREIGN KEY (run_id) REFERENCES history_runs(run_id) ON DELETE CASCADE
                    );
                    """
                )
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(history_deliveries)")}
                if "owner_id" not in columns:
                    db.execute("ALTER TABLE history_deliveries ADD COLUMN owner_id TEXT")
                run_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(history_runs)")}
                if "run_token" not in run_columns:
                    db.execute("ALTER TABLE history_runs ADD COLUMN run_token TEXT NOT NULL DEFAULT ''")
                # A durable pending row is part of offline collection even if
                # an older version first received it as ordinary live traffic.
                db.execute("UPDATE history_deliveries SET recovered=1 WHERE state='pending'")
                db.execute(
                    "DELETE FROM qq_history_delivery_keys WHERE family_id NOT IN "
                    "(SELECT family_id FROM qq_history_families)"
                )
                db.execute(
                    "DELETE FROM qq_history_aliases WHERE family_id IS NOT NULL AND family_id NOT IN "
                    "(SELECT family_id FROM qq_history_families)"
                )
                db.execute(
                    "DELETE FROM qq_history_aliases WHERE family_id IN "
                    "(SELECT f.family_id FROM qq_history_families f JOIN history_deliveries d "
                    "ON d.delivery_key=f.root_delivery_key WHERE d.state='filtered')"
                )
                db.execute(
                    "DELETE FROM qq_history_delivery_keys WHERE family_id IN "
                    "(SELECT f.family_id FROM qq_history_families f JOIN history_deliveries d "
                    "ON d.delivery_key=f.root_delivery_key WHERE d.state='filtered')"
                )
                db.execute(
                    "DELETE FROM qq_history_families WHERE root_delivery_key IN "
                    "(SELECT delivery_key FROM history_deliveries WHERE state='filtered')"
                )
            except (sqlite3.DatabaseError, OSError) as exc:
                raise RuntimeError(f"cannot open Core offline history delivery DB: {exc}") from exc
            self._db = db
        return self._db

    @staticmethod
    def _qq_identity(delivery: dict[str, Any]) -> dict[str, Any] | None:
        return validate_qq_private_identity(
            delivery.get("qq_identity"),
            backend=delivery.get("backend"),
            account_id=delivery.get("account_id"),
            platform=delivery.get("platform"),
            session_kind=delivery.get("session_kind"),
            session_id=delivery.get("session_id"),
            message_id=delivery.get("message_id"),
            original_timestamp=delivery.get("original_timestamp"),
        )

    @staticmethod
    def _qq_family_for_delivery_key(db: sqlite3.Connection, delivery_key: str) -> sqlite3.Row | None:
        return db.execute(
            """SELECT f.family_id, f.root_delivery_key, f.account_id, f.platform, f.peer_id,
                      f.sender_id, f.original_timestamp, f.snow_message_id, f.native_nt_seq
               FROM qq_history_delivery_keys k JOIN qq_history_families f ON f.family_id=k.family_id
               WHERE k.delivery_key=?""",
            (delivery_key,),
        ).fetchone()

    @staticmethod
    def _qq_family(db: sqlite3.Connection, family_id: int) -> sqlite3.Row | None:
        return db.execute(
            """SELECT family_id, root_delivery_key, account_id, platform, peer_id,
                      sender_id, original_timestamp, snow_message_id, native_nt_seq
               FROM qq_history_families WHERE family_id=?""",
            (family_id,),
        ).fetchone()

    @staticmethod
    def _qq_alias_keys(identity: dict[str, Any]) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        canonical = qq_canonical_identity_key(identity)
        snow_alias = qq_snow_alias_key(identity)
        return (
            ("canonical", canonical) if canonical is not None else None,
            ("snow", snow_alias) if snow_alias is not None else None,
        )

    @classmethod
    def _qq_candidate_family_ids(
        cls, db: sqlite3.Connection, delivery_key: str, identity: dict[str, Any] | None
    ) -> set[int]:
        candidates: set[int] = set()
        if identity is None:
            return candidates
        key_family = cls._qq_family_for_delivery_key(db, delivery_key)
        if key_family is not None:
            candidates.add(int(key_family[0]))
        for alias in cls._qq_alias_keys(identity):
            if alias is None:
                continue
            row = db.execute(
                "SELECT family_id, ambiguous FROM qq_history_aliases WHERE alias_kind=? AND alias_key=?",
                alias,
            ).fetchone()
            if row is not None and not bool(row[1]) and row[0] is not None:
                candidates.add(int(row[0]))
        return candidates

    @classmethod
    def _detach_virtual_qq_delivery_key(cls, db: sqlite3.Connection, delivery_key: str) -> None:
        """Forget a proof-gated alias when the caller cannot prove this delivery."""

        family = cls._qq_family_for_delivery_key(db, delivery_key)
        if family is not None and str(family[1]) != delivery_key:
            db.execute("DELETE FROM qq_history_delivery_keys WHERE delivery_key=?", (delivery_key,))

    @staticmethod
    def _quarantine_qq_alias(db: sqlite3.Connection, alias: tuple[str, str] | None) -> None:
        if alias is None:
            return
        db.execute(
            """INSERT INTO qq_history_aliases(alias_kind, alias_key, family_id, ambiguous)
               VALUES(?, ?, NULL, 1)
               ON CONFLICT(alias_kind, alias_key) DO UPDATE SET family_id=NULL, ambiguous=1""",
            alias,
        )

    @classmethod
    def _quarantine_qq_identity(cls, db: sqlite3.Connection, identity: dict[str, Any]) -> None:
        for alias in cls._qq_alias_keys(identity):
            cls._quarantine_qq_alias(db, alias)

    @staticmethod
    def _qq_family_matches(family: sqlite3.Row, identity: dict[str, Any]) -> bool:
        if (
            str(family[2]) != identity["account_id"]
            or str(family[3]) != identity["platform"]
            or str(family[4]) != identity["peer_id"]
            or str(family[5]) != identity["sender_id"]
            or float(family[6]) != float(identity["original_timestamp"])
            or str(family[7]) != identity["snow_message_id"]
        ):
            return False
        stored_sequence = int(family[8]) if family[8] is not None else None
        incoming_sequence = identity.get("native_nt_seq")
        return stored_sequence is None or incoming_sequence is None or stored_sequence == incoming_sequence

    @classmethod
    def _attach_qq_identity(
        cls,
        db: sqlite3.Connection,
        *,
        root_delivery_key: str,
        delivery_key: str,
        identity: dict[str, Any],
        backend: str,
        message_id: str,
        now: float,
        expected_family_id: int | None = None,
    ) -> int | None:
        """Bind validated aliases to one root row, failing closed on collisions."""

        aliases = cls._qq_alias_keys(identity)
        candidates = cls._qq_candidate_family_ids(db, delivery_key, identity)
        alias_rows: dict[tuple[str, str], sqlite3.Row] = {}
        for alias in aliases:
            if alias is None:
                continue
            row = db.execute(
                "SELECT family_id, ambiguous FROM qq_history_aliases WHERE alias_kind=? AND alias_key=?",
                alias,
            ).fetchone()
            if row is not None:
                alias_rows[alias] = row

        canonical_alias, snow_alias = aliases
        canonical_tombstone = bool(alias_rows.get(canonical_alias, (None, 0))[1]) if canonical_alias else False
        snow_tombstone = bool(alias_rows.get(snow_alias, (None, 0))[1]) if snow_alias else False
        if len(candidates) > 1 or canonical_tombstone:
            cls._quarantine_qq_identity(db, identity)
            return None

        family_id: int | None = next(iter(candidates), None)
        if expected_family_id is not None:
            if family_id is not None and family_id != expected_family_id:
                cls._quarantine_qq_identity(db, identity)
                return None
            family_id = expected_family_id

        if family_id is None:
            # An ambiguous Snow hash cannot identify a Snow-only row. NapCat
            # can still keep an exact canonical mapping when it has QQNT seq.
            if snow_tombstone and identity.get("native_nt_seq") is None:
                return None
            existing_key_family = cls._qq_family_for_delivery_key(db, root_delivery_key)
            if existing_key_family is not None:
                family_id = int(existing_key_family[0])
            else:
                db.execute(
                    """INSERT INTO qq_history_families
                       (root_delivery_key, account_id, platform, peer_id, sender_id,
                        original_timestamp, snow_message_id, native_nt_seq, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        root_delivery_key,
                        identity["account_id"],
                        identity["platform"],
                        identity["peer_id"],
                        identity["sender_id"],
                        float(identity["original_timestamp"]),
                        identity["snow_message_id"],
                        identity.get("native_nt_seq"),
                        now,
                    ),
                )
                family_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

        family = cls._qq_family(db, family_id)
        if family is None:
            return None
        if str(family[1]) != root_delivery_key:
            cls._quarantine_qq_identity(db, identity)
            return None
        if not cls._qq_family_matches(family, identity):
            cls._quarantine_qq_identity(db, identity)
            return None
        known_native_id = db.execute(
            "SELECT message_id FROM qq_history_native_ids WHERE family_id=? AND backend=?",
            (family_id, backend),
        ).fetchone()
        if known_native_id is not None and str(known_native_id[0]) != message_id:
            cls._detach_virtual_qq_delivery_key(db, delivery_key)
            cls._quarantine_qq_identity(db, identity)
            return None

        # A Snow-only family learns its canonical sequence when a later
        # NapCat delivery proves the exact native sequence/hash relationship.
        if family[8] is None and identity.get("native_nt_seq") is not None:
            db.execute(
                "UPDATE qq_history_families SET native_nt_seq=? WHERE family_id=? AND native_nt_seq IS NULL",
                (identity["native_nt_seq"], family_id),
            )

        for alias in aliases:
            if alias is None:
                continue
            row = alias_rows.get(alias)
            if row is not None and bool(row[1]):
                if alias[0] == "snow" and identity.get("native_nt_seq") is not None:
                    continue
                cls._quarantine_qq_identity(db, identity)
                return None
            if row is not None and row[0] is not None and int(row[0]) != family_id:
                cls._quarantine_qq_identity(db, identity)
                return None
            if row is None:
                db.execute(
                    "INSERT INTO qq_history_aliases(alias_kind, alias_key, family_id, ambiguous) VALUES(?, ?, ?, 0)",
                    (*alias, family_id),
                )

        prior_key_family = cls._qq_family_for_delivery_key(db, delivery_key)
        if prior_key_family is not None and int(prior_key_family[0]) != family_id:
            cls._detach_virtual_qq_delivery_key(db, delivery_key)
            cls._quarantine_qq_identity(db, identity)
            return None
        db.execute(
            "INSERT OR IGNORE INTO qq_history_delivery_keys(delivery_key, family_id) VALUES(?, ?)",
            (delivery_key, family_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO qq_history_native_ids(family_id, backend, message_id) VALUES(?, ?, ?)",
            (family_id, backend, message_id),
        )
        return family_id

    @classmethod
    def _legacy_snow_family_id(
        cls, db: sqlite3.Connection, identity: dict[str, Any], now: float
    ) -> int | None:
        """Adopt an exact pre-alias SnowLuma receipt using NapCat's native proof."""

        sequence = identity.get("native_nt_seq")
        if sequence is None:
            return None
        fields = (
            "snowluma",
            identity["account_id"],
            "qq",
            "private",
            identity["peer_id"],
            identity["snow_message_id"],
        )
        legacy_key = hashlib.sha256(
            json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        row = db.execute(
            """SELECT backend, account_id, platform, session_kind, session_id, message_id,
                      state, original_timestamp
               FROM history_deliveries WHERE delivery_key=?""",
            (legacy_key,),
        ).fetchone()
        if (
            row is None
            or tuple(str(row[index]) for index in range(6)) != fields
            or str(row[6]) not in {"processing", "pending", "complete"}
            or _positive_finite(row[7]) != float(identity["original_timestamp"])
        ):
            return None
        existing = cls._qq_family_for_delivery_key(db, legacy_key)
        if existing is not None:
            family_id = int(existing[0])
            family = cls._qq_family(db, family_id)
            if family is None or not cls._qq_family_matches(family, identity):
                return None
        else:
            db.execute(
                """INSERT INTO qq_history_families
                   (root_delivery_key, account_id, platform, peer_id, sender_id,
                    original_timestamp, snow_message_id, native_nt_seq, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    legacy_key,
                    identity["account_id"],
                    identity["platform"],
                    identity["peer_id"],
                    identity["sender_id"],
                    float(identity["original_timestamp"]),
                    identity["snow_message_id"],
                    sequence,
                    now,
                ),
            )
            family_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            for alias in cls._qq_alias_keys(identity):
                if alias is None:
                    continue
                prior = db.execute(
                    "SELECT family_id, ambiguous FROM qq_history_aliases WHERE alias_kind=? AND alias_key=?",
                    alias,
                ).fetchone()
                if prior is not None and (bool(prior[1]) or prior[0] is not None):
                    cls._delete_qq_family(db, family_id)
                    return None
                db.execute(
                    "INSERT INTO qq_history_aliases(alias_kind, alias_key, family_id, ambiguous) VALUES(?, ?, ?, 0)",
                    (*alias, family_id),
                )
            db.execute(
                "INSERT INTO qq_history_delivery_keys(delivery_key, family_id) VALUES(?, ?)",
                (legacy_key, family_id),
            )
            db.execute(
                "INSERT INTO qq_history_native_ids(family_id, backend, message_id) VALUES(?, 'snowluma', ?)",
                (family_id, identity["snow_message_id"]),
            )
        return family_id

    @classmethod
    def _delete_qq_family(cls, db: sqlite3.Connection, family_id: int) -> None:
        # Keep ambiguity tombstones; they prevent a later hash collision from
        # turning into a false merge after the processing row is removed.
        db.execute("DELETE FROM qq_history_aliases WHERE family_id=?", (family_id,))
        db.execute("DELETE FROM qq_history_delivery_keys WHERE family_id=?", (family_id,))
        db.execute("DELETE FROM qq_history_native_ids WHERE family_id=?", (family_id,))
        db.execute("DELETE FROM qq_history_families WHERE family_id=?", (family_id,))

    @classmethod
    def _root_key(cls, db: sqlite3.Connection, delivery_key: str) -> str:
        family = cls._qq_family_for_delivery_key(db, delivery_key)
        return str(family[1]) if family is not None else delivery_key

    def root_delivery_key(self, delivery_key: str) -> str:
        """Return the durable root key for pending restoration of an alias."""

        with self._lock:
            return self._root_key(self._connection(), str(delivery_key or "").strip())

    def qq_reply_identity(
        self,
        delivery_key: str,
        *,
        backend: str,
        account_id: str,
        platform: str,
        peer_id: str,
        original_reply_id: str,
    ) -> dict[str, Any] | None:
        """Return confirmed family-native IDs for an exact incoming reply target."""

        key = str(delivery_key or "").strip()
        backend = str(backend or "").strip()
        account = str(account_id or "").strip()
        peer = str(peer_id or "").strip()
        original = str(original_reply_id or "").strip()
        if not key or backend not in {"napcat", "snowluma"} or not account or platform != "qq" or not peer or not original:
            return None
        with self._lock:
            db = self._connection()
            family = self._qq_family_for_delivery_key(db, key)
            if family is None:
                return None
            if (
                str(family[2]) != account
                or str(family[3]) != "qq"
                or str(family[4]) != peer
            ):
                return None
            native_ids = {
                str(row[0]): str(row[1])
                for row in db.execute(
                    "SELECT backend, message_id FROM qq_history_native_ids WHERE family_id=?",
                    (int(family[0]),),
                )
            }
        if native_ids.get(backend) != original:
            return None
        return {
            "version": 1,
            "scope": "qq_private_friend",
            "account_id": account,
            "platform": "qq",
            "peer_id": peer,
            "original_reply_id": original,
            "native_ids": native_ids,
        }

    def qq_native_delivery_scopes(self, delivery: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        """List confirmed family delivery identities for exact recovery-row lookup."""

        identity = self._qq_identity(delivery)
        if identity is None:
            return ()
        key = str(delivery.get("delivery_key") or "").strip()
        if not key:
            return ()
        with self._lock:
            db = self._connection()
            candidates = self._qq_candidate_family_ids(db, key, identity)
            if len(candidates) != 1:
                return ()
            family = self._qq_family(db, next(iter(candidates)))
            if family is None or not self._qq_family_matches(family, identity):
                return ()
            root = db.execute(
                "SELECT state FROM history_deliveries WHERE delivery_key=?",
                (str(family[1]),),
            ).fetchone()
            if root is None or str(root[0]) == "filtered":
                return ()
            native_ids = tuple(
                (str(row[0]), str(row[1]))
                for row in db.execute(
                    "SELECT backend, message_id FROM qq_history_native_ids WHERE family_id=?",
                    (int(family[0]),),
                )
            )
        scopes: list[dict[str, Any]] = []
        for backend, message_id in native_ids:
            if backend not in {"napcat", "snowluma"} or not message_id:
                continue
            fields = (backend, identity["account_id"], "qq", "private", identity["peer_id"], message_id)
            scoped_key = hashlib.sha256(
                json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            scopes.append(
                {
                    "backend": backend,
                    "account_id": identity["account_id"],
                    "platform": "qq",
                    "session_kind": "private",
                    "session_id": identity["peer_id"],
                    "message_id": message_id,
                    "delivery_key": scoped_key,
                    "original_timestamp": float(identity["original_timestamp"]),
                }
            )
        return tuple(scopes)

    def claim(self, delivery: dict[str, Any], *, now: float | None = None) -> str:
        """Return new, busy, pending, complete, filtered, or invalid."""

        key = str(delivery.get("delivery_key") or "").strip()
        required = ("backend", "account_id", "platform", "session_kind", "session_id", "message_id")
        if not key or any(not str(delivery.get(field) or "").strip() for field in required):
            return "invalid"
        stamp = time.time() if now is None else float(now)
        if _positive_finite(delivery.get("original_timestamp")) is None:
            return "invalid"
        identity = self._qq_identity(delivery)
        history_run = delivery.get("history_run")
        run_id = str(history_run.get("run_id") or "") if isinstance(history_run, dict) else ""
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state, updated_at, owner_id FROM history_deliveries WHERE delivery_key=?",
                    (key,),
                ).fetchone()
                if row is not None:
                    state = str(row[0])
                    family = self._qq_family_for_delivery_key(db, key)
                    if state == "filtered":
                        if family is not None:
                            self._delete_qq_family(db, int(family[0]))
                        db.execute("COMMIT")
                        return "filtered"
                    # A delivery-key alias is only cross-backend authority while
                    # this exact envelope carries valid proof. Keep a direct root
                    # family, but detach any foreign virtual key before falling
                    # back to this backend's durable row.
                    if identity is None:
                        self._detach_virtual_qq_delivery_key(db, key)
                    if identity is not None:
                        candidates = self._qq_candidate_family_ids(db, key, identity)
                        if len(candidates) > 1:
                            self._detach_virtual_qq_delivery_key(db, key)
                            self._quarantine_qq_identity(db, identity)
                        elif candidates:
                            candidate_id = next(iter(candidates))
                            candidate = self._qq_family(db, candidate_id)
                            if candidate is not None and str(candidate[1]) == key:
                                self._attach_qq_identity(
                                    db,
                                    root_delivery_key=key,
                                    delivery_key=key,
                                    identity=identity,
                                    backend=str(delivery["backend"]),
                                    message_id=str(delivery["message_id"]),
                                    now=stamp,
                                    expected_family_id=candidate_id,
                                )
                            elif candidate is not None and self._qq_family_matches(candidate, identity):
                                # The exact delivery row was already persisted
                                # before alias support existed. It is safe to
                                # adopt this key only with current validated proof.
                                attached = self._attach_qq_identity(
                                    db,
                                    root_delivery_key=str(candidate[1]),
                                    delivery_key=key,
                                    identity=identity,
                                    backend=str(delivery["backend"]),
                                    message_id=str(delivery["message_id"]),
                                    now=stamp,
                                    expected_family_id=candidate_id,
                                )
                                if attached is None:
                                    self._detach_virtual_qq_delivery_key(db, key)
                            else:
                                self._detach_virtual_qq_delivery_key(db, key)
                                self._quarantine_qq_identity(db, identity)
                        else:
                            self._detach_virtual_qq_delivery_key(db, key)
                            self._attach_qq_identity(
                                db,
                                root_delivery_key=key,
                                delivery_key=key,
                                identity=identity,
                                backend=str(delivery["backend"]),
                                message_id=str(delivery["message_id"]),
                                now=stamp,
                            )
                    if state in {"pending", "complete"}:
                        db.execute("COMMIT")
                        return state
                    same_owner = str(row[2] or "") == self.owner_id
                    if state == "processing" and same_owner:
                        db.execute("COMMIT")
                        return "busy"
                    db.execute(
                        "UPDATE history_deliveries SET updated_at=?, owner_id=? WHERE delivery_key=?",
                        (stamp, self.owner_id, key),
                    )
                    db.execute("COMMIT")
                    return "new"

                # Missing/malformed proof must not inherit a delivery key that
                # was previously only an alias to another adapter's root.
                if identity is None:
                    self._detach_virtual_qq_delivery_key(db, key)
                candidates = self._qq_candidate_family_ids(db, key, identity)
                if not candidates and identity is not None:
                    legacy_family = self._legacy_snow_family_id(db, identity, stamp)
                    if legacy_family is not None:
                        candidates.add(legacy_family)
                if len(candidates) > 1:
                    if identity is not None:
                        self._detach_virtual_qq_delivery_key(db, key)
                        self._quarantine_qq_identity(db, identity)
                    candidates.clear()
                family = self._qq_family(db, next(iter(candidates))) if candidates else None
                if family is not None and identity is not None and not self._qq_family_matches(family, identity):
                    self._detach_virtual_qq_delivery_key(db, key)
                    self._quarantine_qq_identity(db, identity)
                    family = None
                if family is not None:
                    root_key = str(family[1])
                    root_row = db.execute(
                        "SELECT state, owner_id, updated_at FROM history_deliveries WHERE delivery_key=?",
                        (root_key,),
                    ).fetchone()
                    if root_row is None or str(root_row[0]) == "filtered":
                        self._delete_qq_family(db, int(family[0]))
                        family = None
                    else:
                        linked_family_id = int(family[0])
                        if identity is not None and self._attach_qq_identity(
                            db,
                            root_delivery_key=root_key,
                            delivery_key=key,
                            identity=identity,
                            backend=str(delivery["backend"]),
                            message_id=str(delivery["message_id"]),
                            now=stamp,
                            expected_family_id=linked_family_id,
                        ) is None:
                            self._detach_virtual_qq_delivery_key(db, key)
                            family = None
                        else:
                            root_state = str(root_row[0])
                            if root_state in {"pending", "complete"}:
                                db.execute("COMMIT")
                                return root_state
                            same_owner = str(root_row[1] or "") == self.owner_id
                            active_lease = (
                                root_state == "processing"
                                and stamp - float(root_row[2]) < self.processing_lease_sec
                            )
                            if root_state == "processing" and (same_owner or active_lease):
                                db.execute("COMMIT")
                                return "busy"
                            db.execute(
                                "UPDATE history_deliveries SET updated_at=?, owner_id=? WHERE delivery_key=?",
                                (stamp, self.owner_id, root_key),
                            )
                            db.execute("COMMIT")
                            return "new"

                db.execute(
                    """INSERT INTO history_deliveries
                        (delivery_key, backend, account_id, platform, session_kind, session_id,
                        message_id, recovered, state, updated_at, owner_id, original_timestamp, history_run_id)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)""",
                    (
                        key,
                        str(delivery["backend"]),
                        str(delivery["account_id"]),
                        str(delivery["platform"]),
                        str(delivery["session_kind"]),
                        str(delivery["session_id"]),
                        str(delivery["message_id"]),
                        int(bool(delivery.get("recovered"))),
                        stamp,
                        self.owner_id,
                        float(delivery["original_timestamp"]),
                        run_id,
                    ),
                )
                if identity is not None:
                    self._attach_qq_identity(
                        db,
                        root_delivery_key=key,
                        delivery_key=key,
                        identity=identity,
                        backend=str(delivery["backend"]),
                        message_id=str(delivery["message_id"]),
                        now=stamp,
                    )
                db.execute("COMMIT")
                return "new"
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def mark_pending(self, delivery_key: str, *, chat_id: str, row_id: int, original_timestamp: float) -> None:
        if not delivery_key or not chat_id or int(row_id) <= 0 or _positive_finite(original_timestamp) is None:
            raise ValueError("pending history row requires identity, chat, row ID, and timestamp")
        with self._lock:
            db = self._connection()
            root_key = self._root_key(db, delivery_key)
            cursor = db.execute(
                """UPDATE history_deliveries SET state='pending', recovered=1, updated_at=?, chat_id=?, row_id=?, original_timestamp=?
                   WHERE delivery_key=? AND state IN ('processing', 'pending')""",
                (time.time(), chat_id, int(row_id), float(original_timestamp), root_key),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Core history delivery claim disappeared before durable pending insert")

    def restore_existing_row(
        self,
        delivery: dict[str, Any],
        *,
        chat_id: str,
        row_id: int,
        original_timestamp: float,
    ) -> str:
        """Repair the crash window after message-row insert and before receipt insert."""

        key = str(delivery.get("delivery_key") or "").strip()
        if not key or not chat_id or int(row_id) <= 0:
            raise ValueError("existing history row requires a valid identity and stored row")
        state = "pending" if bool(delivery.get("recovered")) else "complete"
        history_run = delivery.get("history_run")
        run_id = str(history_run.get("run_id") or "") if isinstance(history_run, dict) else ""
        root_values = (
            str(delivery["backend"]),
            str(delivery["account_id"]),
            str(delivery["platform"]),
            str(delivery["session_kind"]),
            str(delivery["session_id"]),
            str(delivery["message_id"]),
            int(bool(delivery.get("recovered"))),
            state,
            time.time(),
            chat_id,
            int(row_id),
            float(original_timestamp),
            run_id,
        )
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                root_key = self._root_key(db, key)
                existing = db.execute(
                    "SELECT 1 FROM history_deliveries WHERE delivery_key=?",
                    (root_key,),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO history_deliveries
                           (delivery_key, backend, account_id, platform, session_kind, session_id, message_id,
                            recovered, state, updated_at, chat_id, row_id, original_timestamp, history_run_id)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (root_key, *root_values),
                    )
                else:
                    db.execute(
                        """UPDATE history_deliveries SET state=?, recovered=?, updated_at=?, chat_id=?, row_id=?,
                                  original_timestamp=?
                           WHERE delivery_key=? AND state IN ('processing', 'pending')""",
                        (
                            state,
                            int(bool(delivery.get("recovered"))),
                            time.time(),
                            chat_id,
                            int(row_id),
                            float(original_timestamp),
                            root_key,
                        ),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return state

    def mark_complete(self, delivery_key: str) -> None:
        with self._lock:
            db = self._connection()
            root_key = self._root_key(db, delivery_key)
            cursor = db.execute(
                "UPDATE history_deliveries SET state='complete', updated_at=? WHERE delivery_key=? AND state='processing'",
                (time.time(), root_key),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Core history delivery claim disappeared before completion")

    def mark_filtered(self, delivery_key: str) -> None:
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                root_key = self._root_key(db, delivery_key)
                cursor = db.execute(
                "UPDATE history_deliveries SET state='filtered', updated_at=? WHERE delivery_key=? AND state='processing'",
                    (time.time(), root_key),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Core history delivery claim disappeared before terminal filtering")
                family = db.execute(
                    "SELECT family_id FROM qq_history_families WHERE root_delivery_key=?",
                    (root_key,),
                ).fetchone()
                if family is not None:
                    self._delete_qq_family(db, int(family[0]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def release(self, delivery_key: str) -> None:
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                root_key = self._root_key(db, delivery_key)
                cursor = db.execute(
                    "DELETE FROM history_deliveries WHERE delivery_key=? AND state='processing'", (root_key,)
                )
                family = db.execute(
                    "SELECT family_id FROM qq_history_families WHERE root_delivery_key=?",
                    (root_key,),
                ).fetchone()
                if cursor.rowcount and family is not None:
                    self._delete_qq_family(db, int(family[0]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def pending_rows(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection().execute(
                """SELECT delivery_key, backend, account_id, platform, session_kind, session_id,
                          message_id, chat_id, row_id, original_timestamp, history_run_id
                   FROM history_deliveries WHERE state='pending' AND row_id IS NOT NULL AND chat_id IS NOT NULL
                   ORDER BY original_timestamp, row_id"""
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def open_history_runs(self) -> tuple[dict[str, Any], ...]:
        """Return unsealed runs and their still-open route holds for restart."""

        with self._lock:
            rows = self._connection().execute(
                """SELECT r.run_id, r.run_token, r.backend, r.account_id, r.platform,
                          r.source_instance, r.cutoff_at, t.session_kind, t.session_id
                   FROM history_runs r
                   JOIN history_run_targets t ON t.run_id=r.run_id
                   WHERE r.state='open' AND t.state='open'
                   ORDER BY r.created_at, r.run_id, t.session_kind, t.session_id"""
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            run_id = str(row[0])
            record = grouped.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "run_token": str(row[1]),
                    "backend": str(row[2]),
                    "account_id": str(row[3]),
                    "platform": str(row[4]),
                    "source_instance": str(row[5]),
                    "cutoff_at": float(row[6]),
                    "targets": [],
                },
            )
            record["targets"].append({"session_kind": str(row[7]), "session_id": str(row[8])})
        return tuple(grouped.values())

    def begin_history_run(self, payload: dict[str, Any]) -> None:
        """Durably register every route before the adapter starts API paging."""

        run_id = str(payload.get("run_id") or "").strip()
        backend = str(payload.get("backend") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        source_instance = str(payload.get("source_instance") or "").strip()
        run_token = str(payload.get("run_token") or "").strip()
        cutoff = _positive_finite(payload.get("cutoff_at"))
        targets = payload.get("targets")
        if (
            payload.get("version") != 1
            or not run_id
            or not backend
            or not account_id
            or not platform
            or not source_instance
            or not run_token
            or cutoff is None
            or not isinstance(targets, list)
            or not targets
        ):
            raise ValueError("offline history run begin payload is invalid")
        normalized_targets: set[tuple[str, str]] = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("offline history run target is malformed")
            kind = str(target.get("session_kind") or "").strip()
            session_id = str(target.get("session_id") or "").strip()
            if kind not in {"group", "private"} or not session_id:
                raise ValueError("offline history run target route is invalid")
            normalized_targets.add((kind, session_id))
        if len(normalized_targets) != len(targets):
            raise ValueError("offline history run has duplicate targets")

        now = time.time()
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT backend, account_id, platform, source_instance, cutoff_at, state, run_token FROM history_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                identity = (backend, account_id, platform, source_instance)
                if existing is not None:
                    if (
                        tuple(str(existing[index]) for index in range(4)) != identity
                        or float(existing[4]) != cutoff
                        or str(existing[6]) != run_token
                    ):
                        raise ValueError("offline history run ID was reused for a different identity")
                    existing_targets = {
                        (str(row[0]), str(row[1]))
                        for row in db.execute(
                            "SELECT session_kind, session_id FROM history_run_targets WHERE run_id=?",
                            (run_id,),
                        )
                    }
                    if existing_targets != normalized_targets:
                        raise ValueError("offline history run retry changed its target set")
                    db.execute("COMMIT")
                    return
                db.execute(
                    """INSERT INTO history_runs
                       (run_id, run_token, backend, account_id, platform, source_instance, cutoff_at, state, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (run_id, run_token, backend, account_id, platform, source_instance, cutoff, now, now),
                )
                db.executemany(
                    "INSERT INTO history_run_targets(run_id, session_kind, session_id, state) VALUES(?, ?, ?, 'open')",
                    ((run_id, kind, session_id) for kind, session_id in sorted(normalized_targets)),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def sync_qq_identities_from_run(self, payload: dict[str, Any]) -> int:
        """Bind identity-only NapCat scan records to exact existing receipts.

        This migration phase cannot create a delivery receipt or message row.
        Every alias is grounded in a registered run, an admitted private
        target, and an already-pending or completed NapCat receipt with the
        same scoped key and original timestamp.
        """

        run_id = str(payload.get("run_id") or "").strip()
        run_token = str(payload.get("run_token") or "").strip()
        backend = str(payload.get("backend") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        source_instance = str(payload.get("source_instance") or "").strip()
        cutoff = _positive_finite(payload.get("cutoff_at"))
        records = payload.get("identities")
        if (
            type(payload.get("version")) is not int
            or payload.get("version") != 1
            or payload.get("phase") != "identity_sync"
            or backend != "napcat"
            or not account_id
            or not platform
            or not source_instance
            or not run_id
            or not run_token
            or cutoff is None
            or platform != "qq"
            or not isinstance(records, list)
            or not records
            or len(records) > 100
        ):
            raise ValueError("offline history identity-sync payload is invalid")

        now = time.time()
        changed = 0
        seen_keys: set[str] = set()
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                run = db.execute(
                    """SELECT backend, account_id, platform, source_instance, cutoff_at, state, run_token
                       FROM history_runs WHERE run_id=?""",
                    (run_id,),
                ).fetchone()
                if (
                    run is None
                    or tuple(str(run[index]) for index in range(4))
                    != (backend, account_id, platform, source_instance)
                    or float(run[4]) != cutoff
                    or str(run[5]) not in {"open", "sealed"}
                    or str(run[6]) != run_token
                ):
                    raise ValueError("offline history identity-sync scope did not match registered begin")

                for record in records:
                    if not isinstance(record, dict) or set(record) != {
                        "session_kind",
                        "session_id",
                        "message_id",
                        "delivery_key",
                        "original_timestamp",
                        "qq_identity",
                    }:
                        continue
                    kind = str(record.get("session_kind") or "").strip()
                    peer = str(record.get("session_id") or "").strip()
                    message_id = str(record.get("message_id") or "").strip()
                    key = str(record.get("delivery_key") or "").strip()
                    original_timestamp = _positive_finite(record.get("original_timestamp"))
                    if (
                        kind != "private"
                        or not peer
                        or not message_id
                        or not key
                        or original_timestamp is None
                        or original_timestamp > cutoff
                        or key in seen_keys
                    ):
                        continue
                    seen_keys.add(key)

                    identity = validate_qq_private_identity(
                        record.get("qq_identity"),
                        backend=backend,
                        account_id=account_id,
                        platform=platform,
                        session_kind=kind,
                        session_id=peer,
                        message_id=message_id,
                        original_timestamp=original_timestamp,
                    )
                    if identity is None:
                        continue
                    fields = (backend, account_id, platform, kind, peer, message_id)
                    scoped_key = hashlib.sha256(
                        json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    if scoped_key != key:
                        continue
                    admitted = db.execute(
                        """SELECT 1 FROM history_run_targets
                           WHERE run_id=? AND session_kind='private' AND session_id=?""",
                        (run_id, peer),
                    ).fetchone()
                    if admitted is None:
                        continue

                    receipt = db.execute(
                        """SELECT backend, account_id, platform, session_kind, session_id, message_id,
                                  recovered, state, original_timestamp
                           FROM history_deliveries WHERE delivery_key=?""",
                        (key,),
                    ).fetchone()
                    if (
                        receipt is None
                        or tuple(str(receipt[index]) for index in range(6)) != fields
                        or str(receipt[7]) not in {"pending", "complete"}
                        or _positive_finite(receipt[8]) != original_timestamp
                    ):
                        continue

                    candidates = self._qq_candidate_family_ids(db, key, identity)
                    if len(candidates) > 1:
                        continue
                    family_id = next(iter(candidates), None)
                    family = self._qq_family(db, family_id) if family_id is not None else None
                    if family_id is not None and (
                        family is None or not self._qq_family_matches(family, identity)
                    ):
                        continue
                    canonical_key, _snow_key = self._qq_alias_keys(identity)
                    canonical_alias = (
                        db.execute(
                            "SELECT family_id, ambiguous FROM qq_history_aliases WHERE alias_kind=? AND alias_key=?",
                            canonical_key,
                        ).fetchone()
                        if canonical_key is not None
                        else None
                    )
                    if canonical_alias is not None and bool(canonical_alias[1]):
                        continue
                    known_native = (
                        db.execute(
                            "SELECT message_id FROM qq_history_native_ids WHERE family_id=? AND backend=?",
                            (family_id, backend),
                        ).fetchone()
                        if family_id is not None
                        else None
                    )
                    if known_native is not None and str(known_native[0]) != message_id:
                        continue

                    root_key = str(family[1]) if family is not None else key
                    attached = self._attach_qq_identity(
                        db,
                        root_delivery_key=root_key,
                        delivery_key=key,
                        identity=identity,
                        backend=backend,
                        message_id=message_id,
                        now=now,
                        expected_family_id=family_id,
                    )
                    if attached is not None:
                        changed += 1
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return changed

    def release_empty_open_targets_from_older_instances(
        self,
        *,
        backend: str,
        account_id: str,
        platform: str,
        source_instance: str,
    ) -> tuple[tuple[str, str, str], ...]:
        """Retire empty holds left by an adapter process that has restarted."""

        scope = tuple(str(value).strip() for value in (backend, account_id, platform, source_instance))
        if not all(scope):
            raise ValueError("adapter generation retirement requires a complete scope")
        closed: set[tuple[str, str, str]] = set()
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                routes = db.execute(
                    """SELECT r.run_id, t.session_kind, t.session_id
                       FROM history_runs r JOIN history_run_targets t ON t.run_id=r.run_id
                       WHERE r.backend=? AND r.account_id=? AND r.platform=?
                         AND r.source_instance<>? AND r.state='open' AND t.state='open'""",
                    scope,
                ).fetchall()
                for run_id_value, kind_value, session_value in routes:
                    run_id, kind, session_id = str(run_id_value), str(kind_value), str(session_value)
                    pending = db.execute(
                        """SELECT 1 FROM history_deliveries
                           WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?
                             AND recovered=1 AND state='pending' LIMIT 1""",
                        (*scope[:3], kind, session_id),
                    ).fetchone()
                    if pending is not None:
                        continue
                    db.execute(
                        """UPDATE history_run_targets SET state='complete'
                           WHERE run_id=? AND session_kind=? AND session_id=? AND state='open'""",
                        (run_id, kind, session_id),
                    )
                    closed.add((run_id, kind, session_id))
                if platform == "qq" and backend in {"napcat", "snowluma"}:
                    current = db.execute(
                        """SELECT run_id, created_at FROM history_runs
                           WHERE backend=? AND account_id=? AND platform=? AND source_instance=? AND state='open'
                           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                        scope,
                    ).fetchone()
                    if current is not None:
                        admitted_peers = {
                            str(row[0])
                            for row in db.execute(
                                "SELECT session_id FROM history_run_targets WHERE run_id=? AND session_kind='private'",
                                (str(current[0]),),
                            )
                        }
                        compatible_backends = {"napcat", "snowluma"} - {backend}
                        if admitted_peers and compatible_backends:
                            placeholders = ",".join("?" for _ in compatible_backends)
                            old_routes = db.execute(
                                f"""SELECT r.run_id, r.backend, t.session_id FROM history_runs r
                                    JOIN history_run_targets t ON t.run_id=r.run_id
                                    WHERE r.backend IN ({placeholders}) AND r.account_id=? AND r.platform='qq'
                                      AND r.created_at<=? AND r.source_instance<>? AND r.state='open'
                                      AND t.session_kind='private' AND t.state='open'""",
                                (*sorted(compatible_backends), account_id, float(current[1]), source_instance),
                            ).fetchall()
                            for old_run, old_backend, old_peer in old_routes:
                                peer = str(old_peer)
                                if peer not in admitted_peers:
                                    continue
                                pending = db.execute(
                                    """SELECT 1 FROM history_deliveries
                                       WHERE backend=? AND account_id=? AND platform='qq'
                                         AND session_kind='private' AND session_id=?
                                         AND recovered=1 AND state='pending' LIMIT 1""",
                                    (str(old_backend), account_id, peer),
                                ).fetchone()
                                if pending is not None:
                                    continue
                                db.execute(
                                    """UPDATE history_run_targets SET state='complete'
                                       WHERE run_id=? AND session_kind='private' AND session_id=? AND state='open'""",
                                    (str(old_run), peer),
                                )
                                closed.add((str(old_run), "private", peer))
                            for old_backend in compatible_backends:
                                db.execute(
                                    """UPDATE history_runs SET state='sealed', updated_at=?
                                       WHERE backend=? AND account_id=? AND platform='qq' AND state='open'
                                         AND NOT EXISTS (SELECT 1 FROM history_run_targets t
                                           WHERE t.run_id=history_runs.run_id AND t.state='open')""",
                                    (time.time(), old_backend, account_id),
                                )
                db.execute(
                    """UPDATE history_runs SET state='sealed', updated_at=?
                       WHERE backend=? AND account_id=? AND platform=? AND state='open' AND NOT EXISTS (
                         SELECT 1 FROM history_run_targets t WHERE t.run_id=history_runs.run_id AND t.state='open'
                       )""",
                    (time.time(), *scope[:3]),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return tuple(sorted(closed))

    def seal_history_run(self, payload: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
        """Release holds only for fully fetched routes with durable deliveries.

        Older open runs for the same account are superseded per completed
        target. Failed routes remain held only when an earlier run has a
        stored, unanswered row; those rows are released when a later complete
        scan seals their route.
        """

        run_id = str(payload.get("run_id") or "").strip()
        run_token = str(payload.get("run_token") or "").strip()
        backend = str(payload.get("backend") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        source_instance = str(payload.get("source_instance") or "").strip()
        cutoff = _positive_finite(payload.get("cutoff_at"))
        targets = payload.get("targets")
        if (
            payload.get("version") != 1
            or not run_id
            or not run_token
            or not backend
            or not account_id
            or not platform
            or not source_instance
            or cutoff is None
            or not isinstance(targets, list)
        ):
            raise ValueError("offline history run seal payload is invalid")
        normalized_targets: set[tuple[str, str]] = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("offline history run target is malformed")
            kind = str(target.get("session_kind") or "").strip()
            session_id = str(target.get("session_id") or "").strip()
            if kind not in {"group", "private"} or not session_id:
                raise ValueError("offline history run target route is invalid")
            normalized_targets.add((kind, session_id))

        closed: set[tuple[str, str, str]] = set()
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                current = db.execute(
                    """SELECT backend, account_id, platform, source_instance, cutoff_at,
                              created_at, state, run_token FROM history_runs WHERE run_id=?""",
                    (run_id,),
                ).fetchone()
                if current is None:
                    raise ValueError("offline history run seal has no registered begin")
                if (
                    str(current[0]) != backend
                    or str(current[1]) != account_id
                    or str(current[2]) != platform
                    or str(current[3]) != source_instance
                    or float(current[4]) != cutoff
                ):
                    raise ValueError("offline history run seal scope did not match registered begin")
                if str(current[7]) != run_token:
                    raise ValueError("offline history run token did not match registered begin")
                declared = {
                    (str(row[0]), str(row[1]))
                    for row in db.execute(
                        "SELECT session_kind, session_id FROM history_run_targets WHERE run_id=?",
                        (run_id,),
                    )
                }
                if not normalized_targets.issubset(declared):
                    raise ValueError("offline history run seal named an unregistered target")
                if str(current[6]) == "sealed":
                    closed.update(
                        (run_id, kind, session_id)
                        for kind, session_id in declared
                        if (kind, session_id) in normalized_targets
                    )
                    db.execute("COMMIT")
                    return tuple(sorted(closed))

                # A successfully fetched target can be handed to Focus after
                # every row is durably stored, even while replies are pending.
                # Failed targets remain open only when an earlier durable row
                # still needs the full-session run to finish.
                for kind, session_id in declared:
                    pending = db.execute(
                        """SELECT 1 FROM history_deliveries
                           WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?
                             AND recovered=1 AND state='pending' LIMIT 1""",
                        (backend, account_id, platform, kind, session_id),
                    ).fetchone()
                    if (kind, session_id) in normalized_targets:
                        state = "sealed" if pending is not None else "complete"
                    elif pending is None:
                        state = "complete"
                    else:
                        continue
                    db.execute(
                        "UPDATE history_run_targets SET state=? WHERE run_id=? AND session_kind=? AND session_id=? AND state='open'",
                        (state, run_id, kind, session_id),
                    )
                    old_runs = db.execute(
                        """SELECT r.run_id FROM history_runs r
                           JOIN history_run_targets t ON t.run_id=r.run_id
                           WHERE r.run_id<>? AND r.backend=? AND r.account_id=? AND r.platform=?
                            AND r.created_at<=? AND r.state='open'
                             AND t.session_kind=? AND t.session_id=? AND t.state='open'""",
                        (run_id, backend, account_id, platform, float(current[5]), kind, session_id),
                    ).fetchall()
                    for old in old_runs:
                        old_id = str(old[0])
                        db.execute(
                            "UPDATE history_run_targets SET state=? WHERE run_id=? AND session_kind=? AND session_id=?",
                            (state, old_id, kind, session_id),
                        )
                        closed.add((old_id, kind, session_id))
                    if (
                        platform == "qq"
                        and backend in {"napcat", "snowluma"}
                        and kind == "private"
                        and (kind, session_id) in normalized_targets
                    ):
                        compatible_backends = {"napcat", "snowluma"} - {backend}
                        placeholders = ",".join("?" for _ in compatible_backends)
                        cross_backend_runs = db.execute(
                            f"""SELECT r.run_id FROM history_runs r
                                JOIN history_run_targets t ON t.run_id=r.run_id
                                WHERE r.run_id<>? AND r.backend IN ({placeholders})
                                  AND r.account_id=? AND r.platform='qq' AND r.created_at<=?
                                  AND r.state='open' AND t.session_kind='private'
                                  AND t.session_id=? AND t.state='open'""",
                            (
                                run_id,
                                *sorted(compatible_backends),
                                account_id,
                                float(current[5]),
                                session_id,
                            ),
                        ).fetchall()
                        for old in cross_backend_runs:
                            old_id = str(old[0])
                            db.execute(
                                """UPDATE history_run_targets SET state='complete'
                                   WHERE run_id=? AND session_kind='private' AND session_id=? AND state='open'""",
                                (old_id, session_id),
                            )
                            closed.add((old_id, "private", session_id))
                        for old_backend in compatible_backends:
                            db.execute(
                                """UPDATE history_runs SET state='sealed', updated_at=?
                                   WHERE backend=? AND account_id=? AND platform='qq' AND state='open'
                                     AND NOT EXISTS (SELECT 1 FROM history_run_targets t
                                       WHERE t.run_id=history_runs.run_id AND t.state='open')""",
                                (time.time(), old_backend, account_id),
                            )
                    closed.add((run_id, kind, session_id))
                db.execute(
                    """UPDATE history_runs SET state='sealed', updated_at=?
                       WHERE run_id=? AND NOT EXISTS (
                         SELECT 1 FROM history_run_targets t WHERE t.run_id=history_runs.run_id AND t.state='open'
                       )""",
                    (time.time(), run_id),
                )
                db.execute(
                    """UPDATE history_runs SET state='sealed', updated_at=?
                       WHERE backend=? AND account_id=? AND platform=? AND state='open' AND NOT EXISTS (
                         SELECT 1 FROM history_run_targets t WHERE t.run_id=history_runs.run_id AND t.state='open'
                       )""",
                    (time.time(), backend, account_id, platform),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return tuple(sorted(closed))

    def history_target_blocked(self, run_id: str, session_kind: str, session_id: str) -> bool:
        with self._lock:
            row = self._connection().execute(
                """SELECT t.state, r.state FROM history_run_targets t
                   JOIN history_runs r ON r.run_id=t.run_id
                   WHERE t.run_id=? AND t.session_kind=? AND t.session_id=?""",
                (run_id, session_kind, session_id),
            ).fetchone()
        return bool(row and str(row[0]) == "open" and str(row[1]) == "open")

    def complete_rows(self, row_ids: tuple[int, ...] | list[int]) -> int:
        ids = tuple(dict.fromkeys(int(row_id) for row_id in row_ids if int(row_id) > 0))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            db = self._connection()
            db.execute("BEGIN IMMEDIATE")
            try:
                routes = db.execute(
                    f"""SELECT DISTINCT backend, account_id, platform, session_kind, session_id
                         FROM history_deliveries WHERE state='pending' AND row_id IN ({placeholders})""",
                    ids,
                ).fetchall()
                cursor = db.execute(
                f"UPDATE history_deliveries SET state='complete', updated_at=? WHERE state='pending' AND row_id IN ({placeholders})",
                (time.time(), *ids),
                )
                for route in routes:
                    scope = tuple(str(value) for value in route)
                    still_pending = db.execute(
                        """SELECT 1 FROM history_deliveries WHERE backend=? AND account_id=? AND platform=?
                             AND session_kind=? AND session_id=? AND recovered=1 AND state='pending' LIMIT 1""",
                        scope,
                    ).fetchone()
                    if still_pending is None:
                        db.execute(
                            """UPDATE history_run_targets SET state='complete'
                               WHERE session_kind=? AND session_id=? AND state='sealed'
                                 AND run_id IN (SELECT run_id FROM history_runs
                                                WHERE backend=? AND account_id=? AND platform=?)""",
                            (scope[3], scope[4], scope[0], scope[1], scope[2]),
                        )
                db.execute(
                    """UPDATE history_runs SET state='sealed', updated_at=?
                       WHERE state='open' AND NOT EXISTS (
                         SELECT 1 FROM history_run_targets t WHERE t.run_id=history_runs.run_id AND t.state='open'
                       )""",
                    (time.time(),),
                )
                db.execute("COMMIT")
                return int(cursor.rowcount)
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def state(self, delivery_key: str) -> str | None:
        with self._lock:
            db = self._connection()
            root_key = self._root_key(db, str(delivery_key or "").strip())
            row = db.execute(
                "SELECT state FROM history_deliveries WHERE delivery_key=?", (root_key,)
            ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


history_delivery_store = HistoryDeliveryStore()
