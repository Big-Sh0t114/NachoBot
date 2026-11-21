"""Durable ACK-gated sandbox task and artifact outbox records.

The store is deliberately lazy: importing this module does not create files.
Production callers acquire the single-writer owner before recovery or ingress.
The handoff is persisted as trusted server data, revalidated when read, and is
never reconstructed from model output.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from src.chat.sandbox.sandbox_handoff import SandboxEditHandoff, acknowledgement_fingerprint
from src.chat.sandbox.sandbox_manager import SandboxManager, validate_relative_path


class SandboxTaskStoreError(RuntimeError):
    """A durable sandbox record could not be safely read or updated."""


class SandboxTaskStoreOwnershipError(SandboxTaskStoreError):
    """This process does not own the sandbox task/outbox store."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_OWNER_LOCK = threading.Lock()
_PROCESS_OWNERS: dict[str, str] = {}
MAX_AUTOMATIC_ARTIFACT_ATTEMPTS = 5
OFFLINE_ARTIFACT_DEFER_SECONDS = 5.0


class SandboxTaskStore:
    """SQLite-backed task claims, results, and one-row-per-artifact receipts."""

    def __init__(
        self,
        *,
        manager: Optional[SandboxManager] = None,
        storage_root: Optional[str | Path] = None,
        clock: Any = time.time,
    ) -> None:
        if manager is None and storage_root is None:
            raise ValueError("manager or storage_root is required")
        self.manager = manager
        self.storage_root = Path(storage_root if storage_root is not None else manager.storage_root)
        self.tasks_root = self.storage_root / ".tasks"
        self.outbox_root = self.storage_root / ".outbox"
        self.database_path = self.tasks_root / "sandbox_tasks.sqlite3"
        self._clock = clock
        self._owner_token = uuid.uuid4().hex
        self._owner_pid = os.getpid()
        self._owner_host = socket.gethostname()
        self._owner_lock_path = self.tasks_root / "owner.lock"
        self._owner_lock_handle: Any = None
        self._initialized = False
        self._init_lock = threading.RLock()

    def _check_storage_path(self, path: Path) -> None:
        if self.manager is not None:
            self.manager._check_storage_path(path)
            return
        root = self.storage_root.resolve(strict=False)
        resolved = path.resolve(strict=False)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise SandboxTaskStoreError("task path escapes trusted storage")

    def _initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            self._check_storage_path(self.storage_root)
            self.tasks_root.mkdir(parents=True, exist_ok=True)
            self.outbox_root.mkdir(parents=True, exist_ok=True)
            for path in (self.tasks_root, self.outbox_root, self.database_path):
                self._check_storage_path(path)
            connection = sqlite3.connect(str(self.database_path), timeout=15.0)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS owners (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        host TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        token TEXT NOT NULL,
                        acquired_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tasks (
                        handoff_id TEXT PRIMARY KEY,
                        handoff_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        ack_stream_id TEXT NOT NULL,
                        ack_message_id TEXT NOT NULL,
                        ack_fingerprint TEXT NOT NULL,
                        transaction_id TEXT,
                        outcome TEXT,
                        rounds INTEGER NOT NULL DEFAULT 0,
                        tool_calls INTEGER NOT NULL DEFAULT 0,
                        changed_paths_json TEXT NOT NULL DEFAULT '[]',
                        error_code TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS artifacts (
                        handoff_id TEXT NOT NULL REFERENCES tasks(handoff_id) ON DELETE CASCADE,
                        relative_path TEXT NOT NULL,
                        snapshot_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        receipt_sequence INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0,
                        receipt_message_id TEXT,
                        receipt_status TEXT,
                        detail_code TEXT,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (handoff_id, relative_path)
                    );
                    CREATE INDEX IF NOT EXISTS artifacts_pending_idx
                        ON artifacts(status, next_attempt_at);
                    CREATE TABLE IF NOT EXISTS artifact_receipts (
                        handoff_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        message_id TEXT,
                        detail_code TEXT,
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY (handoff_id, relative_path, attempt),
                        FOREIGN KEY (handoff_id, relative_path)
                            REFERENCES artifacts(handoff_id, relative_path) ON DELETE CASCADE
                    );
                    """
                )
                artifact_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
                }
                if "receipt_sequence" not in artifact_columns:
                    connection.execute(
                        "ALTER TABLE artifacts ADD COLUMN receipt_sequence INTEGER NOT NULL DEFAULT 0"
                    )
                # Reconcile on every startup. ALTER TABLE may survive a crash
                # before the historical receipt backfill, so column presence
                # alone cannot prove this data has been migrated. MAX keeps
                # the lifetime sequence monotonic across retry cycles.
                connection.execute(
                    "UPDATE artifacts SET receipt_sequence = MAX(receipt_sequence, attempts, COALESCE(("
                    "SELECT MAX(attempt) FROM artifact_receipts WHERE "
                    "artifact_receipts.handoff_id = artifacts.handoff_id AND "
                    "artifact_receipts.relative_path = artifacts.relative_path), 0))"
                )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self._initialize()
        self._check_storage_path(self.database_path)
        connection = sqlite3.connect(str(self.database_path), timeout=15.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_owner(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT token, host, pid FROM owners WHERE singleton = 1"
        ).fetchone()
        if (
            row is None
            or row["token"] != self._owner_token
            or row["host"] != self._owner_host
            or row["pid"] != self._owner_pid
        ):
            raise SandboxTaskStoreOwnershipError("sandbox task store is not owned by this instance")

    def _lock_key(self) -> str:
        return os.path.normcase(str(self._owner_lock_path.resolve(strict=False)))

    def _try_acquire_owner_lock(self) -> bool:
        key = self._lock_key()
        with _PROCESS_OWNER_LOCK:
            current = _PROCESS_OWNERS.get(key)
            if current is not None:
                return current == self._owner_token
            self._check_storage_path(self.storage_root)
            self.tasks_root.mkdir(parents=True, exist_ok=True)
            self.outbox_root.mkdir(parents=True, exist_ok=True)
            self._check_storage_path(self.tasks_root)
            self._check_storage_path(self.outbox_root)
            self._check_storage_path(self._owner_lock_path)
            handle = self._owner_lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                handle.close()
                return False
            self._owner_lock_handle = handle
            _PROCESS_OWNERS[key] = self._owner_token
            return True

    def _release_owner_lock(self) -> None:
        handle = self._owner_lock_handle
        if handle is None:
            return
        key = self._lock_key()
        with _PROCESS_OWNER_LOCK:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self._owner_lock_handle = None
                if _PROCESS_OWNERS.get(key) == self._owner_token:
                    _PROCESS_OWNERS.pop(key, None)

    def claim_owner(self) -> bool:
        """Acquire an OS-backed single-owner lock for this storage root."""

        if self._owner_lock_handle is not None:
            return True
        if not self._try_acquire_owner_lock():
            return False
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO owners(singleton, host, pid, token, acquired_at) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET host=excluded.host, pid=excluded.pid, "
                "token=excluded.token, acquired_at=excluded.acquired_at",
                (self._owner_host, self._owner_pid, self._owner_token, float(self._clock())),
            )
            connection.commit()
            return True
        except BaseException:
            if connection is not None:
                connection.rollback()
            self._release_owner_lock()
            raise
        finally:
            if connection is not None:
                connection.close()

    def release_owner(self) -> None:
        """Release only this instance's owner row, for graceful shutdown."""

        if self._owner_lock_handle is None:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM owners WHERE singleton = 1 AND token = ? AND host = ? AND pid = ?",
                (self._owner_token, self._owner_host, self._owner_pid),
            )
            connection.commit()
        finally:
            connection.close()
            self._release_owner_lock()

    @staticmethod
    def _handoff_from_json(raw: str, expected_id: str) -> SandboxEditHandoff:
        try:
            payload = json.loads(raw)
            expected_fields = set(SandboxEditHandoff.__dataclass_fields__)
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                raise ValueError("invalid handoff fields")
            handoff = SandboxEditHandoff(**payload)
            if handoff.handoff_id != expected_id or not handoff.binding_is_valid():
                raise ValueError("invalid handoff binding")
            if not handoff.acknowledgement_fingerprint:
                raise ValueError("missing acknowledgement binding")
            return handoff
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxTaskStoreError("stored handoff failed validation") from exc

    @classmethod
    def _validated_task_handoff(cls, row: sqlite3.Row) -> SandboxEditHandoff:
        handoff = cls._handoff_from_json(row["handoff_json"], str(row["handoff_id"]))
        if (
            row["ack_stream_id"] != handoff.stream_id
            or not row["ack_message_id"]
            or row["ack_fingerprint"] != handoff.acknowledgement_fingerprint
        ):
            raise SandboxTaskStoreError("stored task lacks a valid delivered acknowledgement binding")
        return handoff

    @staticmethod
    def _handoff_json(handoff: SandboxEditHandoff) -> str:
        if not isinstance(handoff, SandboxEditHandoff) or not handoff.binding_is_valid():
            raise SandboxTaskStoreError("handoff failed validation")
        if not handoff.acknowledgement_fingerprint:
            raise SandboxTaskStoreError("handoff lacks acknowledgement binding")
        return json.dumps(asdict(handoff), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def enqueue_after_ack(self, handoff: SandboxEditHandoff, receipt: Any, delivered_content: Any) -> bool:
        """Durably claim a task only from a real matching DELIVERED receipt."""

        from src.plugin_system.apis.send_api import SendStatus

        if not isinstance(handoff, SandboxEditHandoff) or not handoff.binding_is_valid():
            return False
        if getattr(receipt, "status", None) is not SendStatus.DELIVERED:
            return False
        receipt_stream = str(getattr(receipt, "stream_id", "") or "")
        message_id = str(getattr(receipt, "message_id", "") or "")
        if receipt_stream != handoff.stream_id or not message_id:
            return False
        if not handoff.matches_acknowledgement(delivered_content):
            return False
        content_fingerprint = acknowledgement_fingerprint(delivered_content)
        if content_fingerprint != handoff.acknowledgement_fingerprint:
            return False
        handoff_json = self._handoff_json(handoff)
        now = float(self._clock())
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO tasks(handoff_id, handoff_json, status, ack_stream_id, "
                "ack_message_id, ack_fingerprint, created_at, updated_at) "
                "VALUES (?, ?, 'QUEUED', ?, ?, ?, ?, ?)",
                (
                    handoff.handoff_id,
                    handoff_json,
                    receipt_stream,
                    message_id,
                    content_fingerprint,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def _task_row(self, handoff_id: str) -> Optional[sqlite3.Row]:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT * FROM tasks WHERE handoff_id = ?", (str(handoff_id),)
            ).fetchone()
        finally:
            connection.close()

    def get_handoff(self, handoff_id: str) -> Optional[SandboxEditHandoff]:
        row = self._task_row(handoff_id)
        return None if row is None else self._validated_task_handoff(row)

    def get_task(self, handoff_id: str) -> Optional[dict[str, Any]]:
        row = self._task_row(handoff_id)
        if row is None:
            return None
        handoff = self._validated_task_handoff(row)
        connection = self._connect()
        try:
            artifacts = connection.execute(
                "SELECT relative_path, sha256, status, attempts, receipt_message_id, receipt_status, detail_code "
                "FROM artifacts WHERE handoff_id = ? ORDER BY relative_path",
                (str(handoff_id),),
            ).fetchall()
            receipts = connection.execute(
                "SELECT relative_path, attempt, status, message_id, detail_code, recorded_at "
                "FROM artifact_receipts WHERE handoff_id = ? ORDER BY relative_path, attempt",
                (str(handoff_id),),
            ).fetchall()
        finally:
            connection.close()
        receipts_by_path: dict[str, list[dict[str, Any]]] = {}
        for receipt in receipts:
            receipts_by_path.setdefault(receipt["relative_path"], []).append(
                {
                    "attempt": receipt["attempt"],
                    "status": receipt["status"],
                    "message_id": receipt["message_id"],
                    "detail_code": receipt["detail_code"],
                    "recorded_at": receipt["recorded_at"],
                }
            )
        return {
            "handoff_id": row["handoff_id"],
            "handoff": handoff,
            "status": row["status"],
            "ack": {
                "stream_id": row["ack_stream_id"],
                "message_id": row["ack_message_id"],
                "status": "delivered",
                "content_fingerprint": row["ack_fingerprint"],
            },
            "transaction_id": row["transaction_id"],
            "outcome": row["outcome"],
            "rounds": row["rounds"],
            "tool_calls": row["tool_calls"],
            "changed_paths": json.loads(row["changed_paths_json"]),
            "error_code": row["error_code"],
            "artifacts": [
                {**dict(artifact), "receipts": receipts_by_path.get(artifact["relative_path"], [])}
                for artifact in artifacts
            ],
        }

    def queued_handoffs(self) -> tuple[SandboxEditHandoff, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT handoff_id, handoff_json, ack_stream_id, ack_message_id, ack_fingerprint "
                "FROM tasks WHERE status = 'QUEUED' ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._validated_task_handoff(row) for row in rows)

    def mark_running(self, handoff_id: str) -> bool:
        now = float(self._clock())
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='RUNNING', updated_at=?, error_code=NULL "
                "WHERE handoff_id=? AND status='QUEUED'",
                (now, str(handoff_id)),
            )
            return cursor.rowcount == 1

    def mark_interrupted(self, handoff_id: str, *, error_code: str = "interrupted") -> None:
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE tasks SET status='INTERRUPTED', outcome='INTERRUPTED', error_code=?, updated_at=? "
                "WHERE handoff_id=? AND status='RUNNING'",
                (str(error_code)[:80], now, str(handoff_id)),
            )

    def mark_denied(self, handoff_id: str) -> None:
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE tasks SET status='CANCELLED', outcome='CANCELLED', error_code='authorization_denied', "
                "updated_at=? WHERE handoff_id=? AND status IN ('QUEUED','RUNNING')",
                (now, str(handoff_id)),
            )

    def mark_failed(self, handoff_id: str, *, error_code: str) -> None:
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE tasks SET status='FAILED', outcome='MODEL_ERROR', error_code=?, updated_at=? "
                "WHERE handoff_id=? AND status='RUNNING'",
                (str(error_code)[:80], now, str(handoff_id)),
            )

    def record_result(self, handoff_id: str, result: Any) -> None:
        raw_outcome = getattr(getattr(result, "outcome", None), "value", None) or str(
            getattr(result, "outcome", "UNKNOWN")
        )
        rounds = max(0, int(getattr(result, "rounds", 0) or 0))
        tool_calls = max(0, int(getattr(result, "tool_calls", 0) or 0))
        paths = []
        for path in getattr(result, "changed_paths", ()) or ():
            paths.append(validate_relative_path(path))
        now = float(self._clock())
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, transaction_id FROM tasks WHERE handoff_id=?", (str(handoff_id),)
            ).fetchone()
            if row is None:
                return
            transaction_id = row["transaction_id"]
            status = row["status"]
            if transaction_id:
                # Preserve the journal-derived commit/delivery state.
                connection.execute(
                    "UPDATE tasks SET outcome=?, rounds=?, tool_calls=?, changed_paths_json=?, updated_at=? "
                    "WHERE handoff_id=?",
                    (raw_outcome, rounds, tool_calls, json.dumps(paths), now, str(handoff_id)),
                )
                return
            if raw_outcome == "FINALIZED" and paths:
                raw_outcome = "PUBLICATION_FAILED"
                status = "FAILED"
                error_code = "committed_journal_missing"
            elif raw_outcome == "FINALIZED":
                status = "COMPLETED"
                error_code = None
            elif raw_outcome == "CANCELLED":
                status = "CANCELLED"
                error_code = "cancelled"
            else:
                status = "FAILED"
                error_code = str(raw_outcome).lower()[:80]
            connection.execute(
                "UPDATE tasks SET status=?, outcome=?, rounds=?, tool_calls=?, changed_paths_json=?, "
                "error_code=?, updated_at=? WHERE handoff_id=?",
                (status, raw_outcome, rounds, tool_calls, json.dumps(paths), error_code, now, str(handoff_id)),
            )

    def record_commit(self, handoff_id: str, record: dict[str, Any]) -> None:
        """Rebuild the outbox from the manager's validated committed journal."""

        if not isinstance(record, dict) or record.get("status") != "committed":
            raise SandboxTaskStoreError("transaction journal is not committed")
        transaction_id = str(record.get("transaction_id") or "")
        journal_handoff = record.get("handoff")
        if not transaction_id or not isinstance(journal_handoff, dict):
            raise SandboxTaskStoreError("transaction journal is incomplete")
        if str(journal_handoff.get("handoff_id") or "") != str(handoff_id):
            raise SandboxTaskStoreError("transaction journal handoff mismatch")
        outputs = record.get("outputs")
        if not isinstance(outputs, list):
            raise SandboxTaskStoreError("transaction output manifest is invalid")
        validated_outputs: list[tuple[str, str, str]] = []
        raw_transaction_root = self.storage_root / ".transactions" / str(handoff_id)
        self._check_storage_path(raw_transaction_root)
        transaction_root = raw_transaction_root.resolve(strict=True)
        import hashlib

        for output in outputs:
            if not isinstance(output, dict):
                raise SandboxTaskStoreError("transaction output is invalid")
            relative = validate_relative_path(output.get("path"))
            digest = str(output.get("new_sha256") or "")
            if not _SHA256.fullmatch(digest):
                raise SandboxTaskStoreError("transaction output digest is invalid")
            raw_snapshot = Path(str(output.get("postimage_path") or ""))
            snapshot = raw_snapshot if raw_snapshot.is_absolute() else self.storage_root / raw_snapshot
            self._check_storage_path(snapshot)
            resolved_snapshot = snapshot.resolve(strict=True)
            if os.path.commonpath((str(transaction_root), str(resolved_snapshot))) != str(transaction_root):
                raise SandboxTaskStoreError("transaction postimage escapes its journal")
            if not resolved_snapshot.is_file():
                raise SandboxTaskStoreError("transaction postimage is not a file")
            hasher = hashlib.sha256()
            with resolved_snapshot.open("rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise SandboxTaskStoreError("transaction postimage digest mismatch")
            # The transaction library's internal postimage names are not user
            # filenames. Materialize a private immutable delivery copy under
            # .outbox/<handoff>/<original-relative-path> so adapters derive the
            # correct name and extension without reading mutable sandbox paths.
            outbox_root = self.outbox_root / str(handoff_id)
            self._check_storage_path(outbox_root)
            destination = outbox_root / Path(relative)
            self._check_storage_path(destination.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._check_storage_path(destination)
            if destination.exists():
                if not destination.is_file():
                    raise SandboxTaskStoreError("outbox artifact path is not a file")
                copy_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                if copy_hash != digest:
                    raise SandboxTaskStoreError("stored outbox artifact digest mismatch")
            else:
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".sandbox-outbox-", dir=str(destination.parent)
                )
                try:
                    with os.fdopen(file_descriptor, "wb") as target, resolved_snapshot.open("rb") as source:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    temporary_path = Path(temporary_name)
                    self._check_storage_path(temporary_path)
                    os.replace(temporary_path, destination)
                except BaseException:
                    Path(temporary_name).unlink(missing_ok=True)
                    raise
                self._check_storage_path(destination)
                copy_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                if copy_hash != digest:
                    raise SandboxTaskStoreError("materialized outbox artifact digest mismatch")
            validated_outputs.append((relative, str(destination.resolve(strict=True)), digest))
        if len({path for path, _, _ in validated_outputs}) != len(validated_outputs):
            raise SandboxTaskStoreError("transaction output paths are duplicated")

        now = float(self._clock())
        with self._write() as connection:
            task = connection.execute(
                "SELECT handoff_json, ack_stream_id, ack_message_id, ack_fingerprint, transaction_id "
                "FROM tasks WHERE handoff_id=?",
                (str(handoff_id),),
            ).fetchone()
            if task is None:
                raise SandboxTaskStoreError("committed transaction has no ACK-gated task claim")
            stored_handoff = self._validated_task_handoff(
                {
                    "handoff_id": str(handoff_id),
                    "handoff_json": task["handoff_json"],
                    "ack_stream_id": task["ack_stream_id"],
                    "ack_message_id": task["ack_message_id"],
                    "ack_fingerprint": task["ack_fingerprint"],
                }
            )
            if asdict(stored_handoff) != journal_handoff:
                raise SandboxTaskStoreError("transaction handoff does not match ACK-gated task")
            previous_id = task["transaction_id"]
            if previous_id and previous_id != transaction_id:
                raise SandboxTaskStoreError("handoff is linked to a different transaction")
            connection.execute(
                "UPDATE tasks SET status='COMMITTED', transaction_id=?, changed_paths_json=?, "
                "error_code=NULL, updated_at=? WHERE handoff_id=?",
                (transaction_id, json.dumps([path for path, _, _ in validated_outputs]), now, str(handoff_id)),
            )
            expected = {path: (snapshot, digest) for path, snapshot, digest in validated_outputs}
            existing_rows = connection.execute(
                "SELECT relative_path, snapshot_path, sha256, status FROM artifacts WHERE handoff_id=?",
                (str(handoff_id),),
            ).fetchall()
            existing = {row["relative_path"]: row for row in existing_rows}
            if set(existing) - set(expected):
                raise SandboxTaskStoreError("transaction output manifest changed")
            for relative, (snapshot, digest) in expected.items():
                row = existing.get(relative)
                if row is not None and (row["snapshot_path"] != snapshot or row["sha256"] != digest):
                    raise SandboxTaskStoreError("stored outbox manifest changed")
                connection.execute(
                    "INSERT OR IGNORE INTO artifacts(handoff_id, relative_path, snapshot_path, sha256, "
                    "status, updated_at) VALUES (?, ?, ?, ?, 'PENDING', ?)",
                    (str(handoff_id), relative, snapshot, digest, now),
                )
            self._refresh_task_status(connection, str(handoff_id), now)

    def _refresh_task_status(self, connection: sqlite3.Connection, handoff_id: str, now: float) -> None:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM artifacts WHERE handoff_id=?", (str(handoff_id),)
            ).fetchall()
        ]
        if not statuses:
            task_status = "COMPLETED"
        elif all(status == "DELIVERED" for status in statuses):
            task_status = "DELIVERED"
        elif any(status == "DELIVERY_UNKNOWN" for status in statuses):
            task_status = "DELIVERY_UNKNOWN"
        elif any(status in {"RETRY_EXHAUSTED", "INVALID_SNAPSHOT", "BLOCKED_AUTHORIZATION"} for status in statuses):
            task_status = "PUBLICATION_BLOCKED"
        else:
            task_status = "COMMITTED"
        connection.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE handoff_id=? AND transaction_id IS NOT NULL",
            (task_status, now, str(handoff_id)),
        )

    def recover(self, manager: Optional[SandboxManager] = None) -> None:
        """Reconcile interrupted model runs and transaction/outbox crash windows."""

        transaction_manager = manager or self.manager
        if transaction_manager is None:
            raise SandboxTaskStoreError("transaction manager is required for recovery")
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE tasks SET status='INTERRUPTED', outcome='INTERRUPTED', error_code='process_restart', "
                "updated_at=? WHERE status='RUNNING'",
                (now,),
            )
            sending = connection.execute(
                "SELECT handoff_id, relative_path, receipt_sequence FROM artifacts WHERE status='SENDING'"
            ).fetchall()
            for row in sending:
                connection.execute(
                    "UPDATE artifacts SET status='DELIVERY_UNKNOWN', detail_code='process_restart_in_flight', "
                    "updated_at=? WHERE handoff_id=? AND relative_path=? AND status='SENDING'",
                    (now, row["handoff_id"], row["relative_path"]),
                )
                if int(row["receipt_sequence"]) > 0:
                    connection.execute(
                        "INSERT OR IGNORE INTO artifact_receipts(handoff_id, relative_path, attempt, status, "
                        "detail_code, recorded_at) VALUES (?, ?, ?, 'DELIVERY_UNKNOWN', 'process_restart_in_flight', ?)",
                        (row["handoff_id"], row["relative_path"], int(row["receipt_sequence"]), now),
                    )
            for handoff_id, in connection.execute("SELECT handoff_id FROM tasks").fetchall():
                task_row = connection.execute(
                    "SELECT handoff_id, handoff_json, ack_stream_id, ack_message_id, ack_fingerprint "
                    "FROM tasks WHERE handoff_id=?",
                    (handoff_id,),
                ).fetchone()
                self._validated_task_handoff(task_row)
                self._refresh_task_status(connection, handoff_id, now)
        connection = self._connect()
        try:
            handoff_ids = [row[0] for row in connection.execute("SELECT handoff_id FROM tasks").fetchall()]
        finally:
            connection.close()
        for handoff_id in handoff_ids:
            record = transaction_manager.transaction_record(handoff_id)
            if isinstance(record, dict) and record.get("status") == "committed":
                self.record_commit(handoff_id, record)

    def pending_artifacts(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        now = float(self._clock())
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT handoff_id, relative_path, snapshot_path, sha256, status, attempts, next_attempt_at "
                "FROM artifacts WHERE status IN ('PENDING','RETRYABLE','BLOCKED_AUTHORIZATION') "
                "AND next_attempt_at <= ? ORDER BY updated_at LIMIT ?",
                (now, max(1, int(limit))),
            ).fetchall()
        finally:
            connection.close()
        return tuple(dict(row) for row in rows)

    def defer_artifact_until_transport_ready(
        self,
        handoff_id: str,
        relative_path: str,
        *,
        delay_seconds: float = OFFLINE_ARTIFACT_DEFER_SECONDS,
    ) -> bool:
        """Move an offline artifact behind other pending work without claiming a send."""

        now = float(self._clock())
        delay = min(60.0, max(1.0, float(delay_seconds)))
        path = validate_relative_path(relative_path)
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE artifacts SET next_attempt_at=?, updated_at=? "
                "WHERE handoff_id=? AND relative_path=? AND status IN ('PENDING','RETRYABLE')",
                (now + delay, now, str(handoff_id), path),
            )
            return cursor.rowcount == 1

    def claim_artifact_send(self, handoff_id: str, relative_path: str) -> bool:
        path = validate_relative_path(relative_path)
        now = float(self._clock())
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE artifacts SET status='SENDING', attempts=attempts+1, "
                "receipt_sequence=receipt_sequence+1, detail_code=NULL, updated_at=? "
                "WHERE handoff_id=? AND relative_path=? AND status IN ('PENDING','RETRYABLE') "
                "AND next_attempt_at <= ?",
                (now, str(handoff_id), path, now),
            )
            return cursor.rowcount == 1

    def mark_artifact_delivered(self, handoff_id: str, relative_path: str, message_id: str) -> None:
        if not str(message_id or "").strip():
            raise SandboxTaskStoreError("delivered artifact receipt needs a message id")
        now = float(self._clock())
        with self._write() as connection:
            path = validate_relative_path(relative_path)
            cursor = connection.execute(
                "UPDATE artifacts SET status='DELIVERED', receipt_message_id=?, receipt_status='delivered', "
                "detail_code=NULL, updated_at=? WHERE handoff_id=? AND relative_path=? AND status='SENDING'",
                (str(message_id), now, str(handoff_id), path),
            )
            if cursor.rowcount == 1:
                attempt = connection.execute(
                    "SELECT receipt_sequence FROM artifacts WHERE handoff_id=? AND relative_path=?",
                    (str(handoff_id), path),
                ).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_receipts(handoff_id, relative_path, attempt, status, "
                    "message_id, recorded_at) VALUES (?, ?, ?, 'DELIVERED', ?, ?)",
                    (str(handoff_id), path, int(attempt), str(message_id), now),
                )
            self._refresh_task_status(connection, handoff_id, now)

    def mark_artifact_unknown(self, handoff_id: str, relative_path: str, *, detail_code: str) -> None:
        now = float(self._clock())
        with self._write() as connection:
            path = validate_relative_path(relative_path)
            cursor = connection.execute(
                "UPDATE artifacts SET status='DELIVERY_UNKNOWN', receipt_status='unknown', detail_code=?, "
                "updated_at=? WHERE handoff_id=? AND relative_path=? AND status='SENDING'",
                (str(detail_code)[:80], now, str(handoff_id), path),
            )
            if cursor.rowcount == 1:
                attempt = connection.execute(
                    "SELECT receipt_sequence FROM artifacts WHERE handoff_id=? AND relative_path=?",
                    (str(handoff_id), path),
                ).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_receipts(handoff_id, relative_path, attempt, status, "
                    "detail_code, recorded_at) VALUES (?, ?, ?, 'DELIVERY_UNKNOWN', ?, ?)",
                    (str(handoff_id), path, int(attempt), str(detail_code)[:80], now),
                )
            self._refresh_task_status(connection, handoff_id, now)

    def mark_artifact_invalid_snapshot(self, handoff_id: str, relative_path: str) -> None:
        now = float(self._clock())
        path = validate_relative_path(relative_path)
        with self._write() as connection:
            connection.execute(
                "UPDATE artifacts SET status='INVALID_SNAPSHOT', receipt_status='not_sent', "
                "detail_code='snapshot_validation_failed', updated_at=? WHERE handoff_id=? "
                "AND relative_path=? AND status IN ('PENDING','RETRYABLE','SENDING')",
                (now, str(handoff_id), path),
            )
            self._refresh_task_status(connection, handoff_id, now)

    def mark_artifact_retryable(self, handoff_id: str, relative_path: str, *, detail_code: str) -> None:
        now = float(self._clock())
        path = validate_relative_path(relative_path)
        with self._write() as connection:
            row = connection.execute(
                "SELECT attempts, receipt_sequence FROM artifacts WHERE handoff_id=? AND relative_path=? "
                "AND status='SENDING'",
                (str(handoff_id), path),
            ).fetchone()
            if row is None:
                return
            delay = min(60.0, 2.0 ** max(0, min(int(row["attempts"]) - 1, 6)))
            next_status = (
                "RETRY_EXHAUSTED"
                if int(row["attempts"]) >= MAX_AUTOMATIC_ARTIFACT_ATTEMPTS
                else "RETRYABLE"
            )
            connection.execute(
                "UPDATE artifacts SET status=?, receipt_status='pre_send_rejection', detail_code=?, "
                "next_attempt_at=?, updated_at=? WHERE handoff_id=? AND relative_path=? AND status='SENDING'",
                (next_status, str(detail_code)[:80], now + delay, now, str(handoff_id), path),
            )
            connection.execute(
                "INSERT OR IGNORE INTO artifact_receipts(handoff_id, relative_path, attempt, status, "
                "detail_code, recorded_at) VALUES (?, ?, ?, 'PRE_SEND_REJECTION', ?, ?)",
                (str(handoff_id), path, int(row["receipt_sequence"]), str(detail_code)[:80], now),
            )
            self._refresh_task_status(connection, handoff_id, now)

    def retry_artifact(
        self,
        handoff_id: str,
        relative_path: str,
        *,
        allow_possible_duplicate: bool = False,
    ) -> bool:
        """Explicit operator retry; uncertain receipts require a duplicate-risk override."""

        now = float(self._clock())
        path = validate_relative_path(relative_path)
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM artifacts WHERE handoff_id=? AND relative_path=?",
                (str(handoff_id), path),
            ).fetchone()
            if row is None:
                return False
            status = str(row["status"])
            if status == "RETRY_EXHAUSTED":
                pass
            elif status == "DELIVERY_UNKNOWN" and allow_possible_duplicate is True:
                pass
            else:
                return False
            connection.execute(
                "UPDATE artifacts SET status='PENDING', attempts=0, next_attempt_at=0, "
                "receipt_message_id=NULL, receipt_status=NULL, detail_code='operator_retry', updated_at=? "
                "WHERE handoff_id=? AND relative_path=?",
                (now, str(handoff_id), path),
            )
            self._refresh_task_status(connection, handoff_id, now)
            return True

    def unblock_authorized_artifact(self, handoff_id: str, relative_path: str) -> None:
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE artifacts SET status='PENDING', detail_code=NULL, next_attempt_at=0, updated_at=? "
                "WHERE handoff_id=? AND relative_path=? AND status='BLOCKED_AUTHORIZATION'",
                (now, str(handoff_id), validate_relative_path(relative_path)),
            )
            self._refresh_task_status(connection, handoff_id, now)

    def block_artifact_unauthorized(self, handoff_id: str, relative_path: str) -> None:
        now = float(self._clock())
        with self._write() as connection:
            connection.execute(
                "UPDATE artifacts SET status='BLOCKED_AUTHORIZATION', detail_code='authorization_denied', "
                "updated_at=? WHERE handoff_id=? AND relative_path=? "
                "AND status IN ('PENDING','RETRYABLE','BLOCKED_AUTHORIZATION')",
                (now, str(handoff_id), validate_relative_path(relative_path)),
            )
            self._refresh_task_status(connection, handoff_id, now)

    def committed_handoffs(self) -> tuple[str, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT handoff_id FROM tasks WHERE transaction_id IS NOT NULL ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
        return tuple(row[0] for row in rows)


__all__ = ["SandboxTaskStore", "SandboxTaskStoreError", "SandboxTaskStoreOwnershipError"]
