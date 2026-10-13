"""Durable adapter-side state and bounded history synchronization primitives.

The module depends only on the Python standard library so adapters can use it
without importing Core configuration, database, or logging code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .qq_history_identity import validate_qq_private_identity


class HistoryRecoveryError(RuntimeError):
    """Base error for incomplete or invalid history recovery."""


class HistoryPageError(HistoryRecoveryError):
    """An API response failed validation or could not be used as a page."""


class HistoryStateError(HistoryRecoveryError):
    """Persistent state is unreadable or inconsistent."""


@dataclass(frozen=True, slots=True)
class HistoryTarget:
    kind: str
    session_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"group", "private"}:
            raise ValueError("history target kind must be group or private")
        if not self.session_id.strip():
            raise ValueError("history target session_id must not be empty")


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One normalized page, ordered from oldest to newest.

    ``next_cursor`` is the cursor to use for the next page in the configured
    backend direction.  A short or empty page is not an end signal by itself;
    the backend must set ``end_of_history`` only when it has authoritative
    evidence that the requested boundary was reached.
    """

    messages: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    end_of_history: bool = False
    boundary_reached: bool = False
    authoritative: bool = True


@dataclass(frozen=True, slots=True)
class ScanPlan:
    """Immutable checkpoint boundary plus a mutable API pagination start.

    ``backward`` scans begin at the latest page and walk toward a stored
    checkpoint (or the first-enable baseline). ``forward`` scans are used by
    SnowLuma groups, whose API can only authoritatively walk newer from a
    validated saved message ID.
    """

    direction: Literal["backward", "forward"]
    initial_cursor: str | None
    boundary_id: str | None
    boundary_ids: frozenset[str]
    boundary_at: float
    require_exact_id: bool = False

    def __post_init__(self) -> None:
        if self.direction not in {"backward", "forward"}:
            raise ValueError("history scan direction must be backward or forward")
        if not math.isfinite(self.boundary_at) or self.boundary_at <= 0:
            raise ValueError("history scan boundary must be finite and positive")
        if self.require_exact_id and not self.boundary_id:
            raise ValueError("history scan requires a boundary ID")


@dataclass(frozen=True, slots=True)
class PendingHistoryMessage:
    target: HistoryTarget
    message_id: str
    original_timestamp: float
    raw_message: dict[str, Any]
    delivery_key: str
    receipt_token: str
    run_id: str
    cutoff_at: float


def scoped_delivery_key(
    backend: str,
    account_id: str,
    platform: str,
    target: HistoryTarget,
    message_id: str,
) -> str:
    """Return a deterministic identity shared by live ingress and history pulls."""

    fields = (backend, account_id, platform, target.kind, target.session_id, message_id)
    if any(not str(value).strip() for value in fields):
        raise ValueError("delivery identity fields must be non-empty")
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_delivery_metadata(
    *,
    backend: str,
    account_id: str,
    platform: str,
    target: HistoryTarget,
    message_id: str,
    recovered: bool,
    receipt_token: str | None = None,
    qq_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the additive message identity contract used by Core deduplication."""

    key = scoped_delivery_key(backend, account_id, platform, target, message_id)
    result: dict[str, Any] = {
        "version": 1,
        "backend": backend,
        "account_id": account_id,
        "platform": platform,
        "session_kind": target.kind,
        "session_id": target.session_id,
        "message_id": message_id,
        "delivery_key": key,
        "recovered": bool(recovered),
    }
    if receipt_token:
        result["receipt_token"] = receipt_token
    if isinstance(qq_identity, Mapping):
        validated_qq_identity = validate_qq_private_identity(
            qq_identity,
            backend=backend,
            account_id=account_id,
            platform=platform,
            session_kind=target.kind,
            session_id=target.session_id,
            message_id=message_id,
            original_timestamp=qq_identity.get("original_timestamp"),
        )
        if validated_qq_identity is not None:
            result["qq_identity"] = validated_qq_identity
    return result


def build_history_run_metadata(run_id: str, cutoff_at: float) -> dict[str, Any]:
    if not run_id.strip() or not math.isfinite(cutoff_at) or cutoff_at <= 0:
        raise ValueError("history run requires an ID and finite positive cutoff")
    return {"run_id": run_id, "cutoff_at": float(cutoff_at), "version": 1}


def _finite_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 100_000_000_000:
        number /= 1000.0
    return number if math.isfinite(number) and number > 0 else None


class OfflineHistoryStore:
    """SQLite adapter outbox/checkpoint store; opened only on first operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            try:
                connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                if existed:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        connection.close()
                        raise HistoryStateError("offline history state database failed integrity check")
                self._create_schema(connection)
            except (sqlite3.DatabaseError, OSError) as exc:
                raise HistoryStateError(f"cannot open offline history state: {exc}") from exc
            self._connection = connection
        return self._connection

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS scopes (
                backend TEXT NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                baseline_at REAL NOT NULL,
                PRIMARY KEY (backend, account_id, platform)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                backend TEXT NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                session_kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                checkpoint_id TEXT,
                checkpoint_at REAL,
                checkpoint_ids TEXT NOT NULL DEFAULT '[]',
                discovered_at REAL NOT NULL,
                PRIMARY KEY (backend, account_id, platform, session_kind, session_id)
            );
            CREATE TABLE IF NOT EXISTS live_seen (
                backend TEXT NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                session_kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                original_at REAL NOT NULL,
                observed_at REAL NOT NULL,
                PRIMARY KEY (backend, account_id, platform, session_kind, session_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                backend TEXT NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                session_kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                original_at REAL NOT NULL,
                sort_order INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                delivery_key TEXT NOT NULL,
                receipt_token TEXT NOT NULL,
                cutoff_at REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',
                run_id TEXT NOT NULL,
                PRIMARY KEY (backend, account_id, platform, session_kind, session_id, message_id),
                UNIQUE (delivery_key)
            );
            CREATE INDEX IF NOT EXISTS outbox_pending_order
              ON outbox (backend, account_id, platform, session_kind, session_id, state, original_at, sort_order);
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(outbox)")}
        if "cutoff_at" not in columns:
            db.execute("ALTER TABLE outbox ADD COLUMN cutoff_at REAL NOT NULL DEFAULT 0")

    @staticmethod
    def _scope_values(backend: str, account_id: str, platform: str) -> tuple[str, str, str]:
        values = tuple(str(value).strip() for value in (backend, account_id, platform))
        if any(not value for value in values):
            raise ValueError("backend, account_id, and platform are required")
        return values  # type: ignore[return-value]

    def initialize_scope(self, backend: str, account_id: str, platform: str, *, now: float | None = None) -> float:
        values = self._scope_values(backend, account_id, platform)
        baseline = time.time() if now is None else float(now)
        if not math.isfinite(baseline) or baseline <= 0:
            raise ValueError("scope baseline must be a finite positive timestamp")
        db = self._db()
        db.execute(
            "INSERT OR IGNORE INTO scopes(backend, account_id, platform, baseline_at) VALUES(?, ?, ?, ?)",
            (*values, baseline),
        )
        row = db.execute(
            "SELECT baseline_at FROM scopes WHERE backend=? AND account_id=? AND platform=?", values
        ).fetchone()
        if row is None:
            raise HistoryStateError("offline history scope baseline was not persisted")
        return float(row[0])

    def register_target(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
        *,
        discovered_at: float | None = None,
    ) -> tuple[str | None, float | None, frozenset[str]]:
        scope = self._scope_values(backend, account_id, platform)
        baseline = self.initialize_scope(*scope, now=discovered_at)
        discovered = time.time() if discovered_at is None else float(discovered_at)
        db = self._db()
        db.execute(
            """INSERT OR IGNORE INTO sessions
               (backend, account_id, platform, session_kind, session_id, discovered_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (*scope, target.kind, target.session_id, discovered),
        )
        row = db.execute(
            """SELECT checkpoint_id, checkpoint_at, checkpoint_ids FROM sessions
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?""",
            (*scope, target.kind, target.session_id),
        ).fetchone()
        if row is None:
            raise HistoryStateError("offline history session registration was not persisted")
        try:
            boundary_ids = frozenset(str(item) for item in json.loads(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoryStateError("offline history checkpoint identity set is corrupt") from exc
        # ``baseline`` is returned separately; it is not a fake session cursor.
        return row[0], (float(row[1]) if row[1] is not None else baseline), boundary_ids

    def known_targets(self, backend: str, account_id: str, platform: str) -> tuple[HistoryTarget, ...]:
        scope = self._scope_values(backend, account_id, platform)
        rows = self._db().execute(
            """SELECT session_kind, session_id FROM sessions
               WHERE backend=? AND account_id=? AND platform=? ORDER BY session_kind, session_id""",
            scope,
        ).fetchall()
        return tuple(HistoryTarget(str(row[0]), str(row[1])) for row in rows)

    def checkpoint(self, backend: str, account_id: str, platform: str, target: HistoryTarget) -> tuple[str | None, float | None, frozenset[str]]:
        scope = self._scope_values(backend, account_id, platform)
        row = self._db().execute(
            """SELECT checkpoint_id, checkpoint_at, checkpoint_ids FROM sessions
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?""",
            (*scope, target.kind, target.session_id),
        ).fetchone()
        if row is None:
            baseline = self.initialize_scope(*scope)
            return None, baseline, frozenset()
        try:
            ids = frozenset(str(item) for item in json.loads(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoryStateError("offline history checkpoint identity set is corrupt") from exc
        return row[0], (float(row[1]) if row[1] is not None else None), ids

    def record_live_seen(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
        message_id: str,
        original_timestamp: float,
        *,
        observed_at: float | None = None,
    ) -> None:
        scope = self._scope_values(backend, account_id, platform)
        timestamp = _finite_timestamp(original_timestamp)
        if timestamp is None or not str(message_id).strip():
            raise ValueError("live message identity requires a stable ID and timestamp")
        self.register_target(*scope, target)
        observed = time.time() if observed_at is None else float(observed_at)
        db = self._db()
        existing = db.execute(
            """SELECT message_id, original_at, observed_at FROM live_seen
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?
               ORDER BY original_at DESC, observed_at DESC LIMIT 1""",
            (*scope, target.kind, target.session_id),
        ).fetchone()
        if existing is not None and (timestamp, observed) <= (float(existing[1]), float(existing[2])):
            db.execute(
                """DELETE FROM live_seen WHERE backend=? AND account_id=? AND platform=?
                   AND session_kind=? AND session_id=? AND message_id<>?""",
                (*scope, target.kind, target.session_id, str(existing[0])),
            )
            return
        # Only the latest validated live event is needed as a pagination
        # anchor. Replacing it bounds this table to one ID per session; Core's
        # durable scoped delivery table handles message deduplication.
        db.execute(
            """DELETE FROM live_seen WHERE backend=? AND account_id=? AND platform=?
               AND session_kind=? AND session_id=?""",
            (*scope, target.kind, target.session_id),
        )
        self._db().execute(
            """INSERT OR IGNORE INTO live_seen
               (backend, account_id, platform, session_kind, session_id, message_id, original_at, observed_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (*scope, target.kind, target.session_id, str(message_id), timestamp, observed),
        )

    def live_anchor(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
    ) -> tuple[str | None, float | None]:
        """Return a live navigation anchor without advancing history progress."""

        scope = self._scope_values(backend, account_id, platform)
        row = self._db().execute(
            """SELECT message_id, original_at FROM live_seen
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?
               ORDER BY original_at DESC, observed_at DESC, message_id DESC LIMIT 1""",
            (*scope, target.kind, target.session_id),
        ).fetchone()
        return (str(row[0]), float(row[1])) if row else (None, None)

    def stage_page(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
        messages: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        cutoff_at: float,
        sort_order_start: int = 0,
    ) -> int:
        scope = self._scope_values(backend, account_id, platform)
        self.register_target(*scope, target)
        db = self._db()
        staged = 0
        db.execute("BEGIN IMMEDIATE")
        try:
            for offset, message in enumerate(messages):
                message_id = str(message.get("message_id") or "").strip()
                timestamp = _finite_timestamp(message.get("time"))
                if not message_id or timestamp is None:
                    raise HistoryPageError("history page contained a message without a stable ID or timestamp")
                key = scoped_delivery_key(backend, account_id, platform, target, message_id)
                raw_json = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
                existing = db.execute(
                    """SELECT delivery_key, raw_json FROM outbox WHERE backend=? AND account_id=? AND platform=?
                       AND session_kind=? AND session_id=? AND message_id=?""",
                    (*scope, target.kind, target.session_id, message_id),
                ).fetchone()
                if existing is not None:
                    if existing[0] != key:
                        raise HistoryStateError("offline history outbox key scope mismatch")
                    # Preserve the first payload/token to keep retries exact.
                    continue
                token = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO outbox
                       (backend, account_id, platform, session_kind, session_id, message_id,
                        original_at, sort_order, raw_json, delivery_key, receipt_token, cutoff_at, state, run_id)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        *scope,
                        target.kind,
                        target.session_id,
                        message_id,
                        timestamp,
                        sort_order_start + offset,
                        raw_json,
                        key,
                        token,
                        float(cutoff_at),
                        run_id,
                    ),
                )
                staged += 1
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return staged

    def pending_for_target(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
        *,
        limit: int | None = None,
    ) -> tuple[PendingHistoryMessage, ...]:
        scope = self._scope_values(backend, account_id, platform)
        query = (
            """SELECT message_id, original_at, raw_json, delivery_key, receipt_token, run_id, cutoff_at
               FROM outbox WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?
                 AND state='pending' ORDER BY original_at ASC, sort_order ASC, message_id ASC"""
        )
        parameters: tuple[Any, ...] = (*scope, target.kind, target.session_id)
        if limit is not None:
            if limit < 1:
                raise ValueError("pending history query limit must be positive")
            query += " LIMIT ?"
            parameters += (int(limit),)
        rows = self._db().execute(query, parameters).fetchall()
        result = []
        for row in rows:
            try:
                raw = json.loads(row[2])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HistoryStateError("offline history outbox payload is corrupt") from exc
            if not isinstance(raw, dict):
                raise HistoryStateError("offline history outbox payload is not an object")
            result.append(
                PendingHistoryMessage(
                    target,
                    str(row[0]),
                    float(row[1]),
                    raw,
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    float(row[6]),
                )
            )
        return tuple(result)

    def acknowledge(self, delivery_key: str, receipt_token: str, outcome: str) -> bool:
        if outcome not in {"accepted", "filtered", "pending"}:
            return False
        db = self._db()
        cursor = db.execute(
            "UPDATE outbox SET state=? WHERE delivery_key=? AND receipt_token=? AND state='pending'",
            ("acked", delivery_key, receipt_token),
        )
        if cursor.rowcount:
            return True
        row = db.execute("SELECT state, receipt_token FROM outbox WHERE delivery_key=?", (delivery_key,)).fetchone()
        return bool(row and row[0] == "acked" and row[1] == receipt_token)

    def complete_target(
        self,
        backend: str,
        account_id: str,
        platform: str,
        target: HistoryTarget,
        *,
        checkpoint_id: str | None,
        checkpoint_timestamp: float | None,
        boundary_ids: Iterable[str] = (),
    ) -> None:
        scope = self._scope_values(backend, account_id, platform)
        pending = self._db().execute(
            """SELECT COUNT(*) FROM outbox WHERE backend=? AND account_id=? AND platform=?
               AND session_kind=? AND session_id=? AND state='pending'""",
            (*scope, target.kind, target.session_id),
        ).fetchone()[0]
        if pending:
            raise HistoryStateError("cannot advance history checkpoint while outbox deliveries are pending")
        ids = sorted({str(item) for item in boundary_ids if str(item).strip()})
        encoded = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
        self._db().execute(
            """UPDATE sessions SET checkpoint_id=?, checkpoint_at=?, checkpoint_ids=?
               WHERE backend=? AND account_id=? AND platform=? AND session_kind=? AND session_id=?""",
            (checkpoint_id, checkpoint_timestamp, encoded, *scope, target.kind, target.session_id),
        )
        # Keep a short stable-ID overlap window; payload data for completed rows
        # is discarded only after the durable checkpoint has moved.
        self._db().execute(
            """DELETE FROM outbox WHERE backend=? AND account_id=? AND platform=? AND session_kind=?
               AND session_id=? AND state='acked'""",
            (*scope, target.kind, target.session_id),
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


FetchPage = Callable[[HistoryTarget, str | None, int, Literal["backward", "forward"]], Awaitable[HistoryPage]]
PlanTarget = Callable[
    [HistoryTarget, str | None, float | None, frozenset[str], str | None, float | None, float],
    ScanPlan,
]
SendHold = Callable[[HistoryTarget, str, float, bool, str, int], Awaitable[None]]
SendRun = Callable[
    [str, str, float, tuple[HistoryTarget, ...], Literal["begin", "seal"]], Awaitable[None]
]
EmitHistory = Callable[[PendingHistoryMessage, str, float], Awaitable[bool]]
AccountIsCurrent = Callable[[], bool]
ReadyToRetry = Callable[[], Awaitable[None]]


class OfflineHistorySynchronizer:
    """Serialize scans, stage complete pages durably, then emit acknowledged rows."""

    def __init__(
        self,
        store: OfflineHistoryStore,
        *,
        backend: str,
        platform: str,
        page_size: int = 50,
        max_pages: int = 200,
        retry_delay: float = 2.0,
        marker_refresh_sec: float = 120.0,
        clock: Callable[[], float] = time.time,
    ):
        if page_size < 1 or max_pages < 1:
            raise ValueError("history page_size and max_pages must be positive")
        self.store = store
        self.backend = backend
        self.platform = platform
        self.page_size = page_size
        self.max_pages = max_pages
        self.retry_delay = max(0.1, retry_delay)
        self.marker_refresh_sec = max(1.0, marker_refresh_sec)
        self.clock = clock
        self._lock = asyncio.Lock()
        self._generation = 0

    async def run(
        self,
        *,
        account_id: str,
        targets: Iterable[HistoryTarget],
        fetch_page: FetchPage,
        send_hold: SendHold,
        emit: EmitHistory,
        plan_target: PlanTarget | None = None,
        send_run: SendRun | None = None,
        account_is_current: AccountIsCurrent | None = None,
        wait_retry: ReadyToRetry | None = None,
        on_session_error: Callable[[HistoryTarget, Exception], None] | None = None,
        retry_delivery_failures: bool = True,
    ) -> dict[str, Any]:
        """Recover a single identity generation; failed sessions retain state for retry."""

        normalized_account = str(account_id).strip()
        if not normalized_account:
            raise ValueError("history recovery requires a verified account ID")
        async with self._lock:
            self._generation += 1
            generation = self._generation
            cutoff = float(self.clock())
            if not math.isfinite(cutoff) or cutoff <= 0:
                raise ValueError("history recovery clock must return a finite positive timestamp")
            baseline = self.store.initialize_scope(self.backend, normalized_account, self.platform, now=cutoff)
            run_id = uuid.uuid4().hex
            run_token = uuid.uuid4().hex
            unique_targets = tuple(dict.fromkeys(targets))
            holds: dict[HistoryTarget, str] = {}
            complete: dict[HistoryTarget, tuple[str | None, float | None, frozenset[str]]] = {}
            errors: dict[str, str] = {}
            refresh_task: asyncio.Task[None] | None = None
            try:
                if send_run is not None and unique_targets:
                    await send_run(run_id, run_token, cutoff, unique_targets, "begin")
                for target in unique_targets:
                    self.store.register_target(self.backend, normalized_account, self.platform, target)
                    progress_id = uuid.uuid4().hex
                    # Register before awaiting, so even a partly successful
                    # marker send is closed when setup fails or is cancelled.
                    holds[target] = progress_id
                    await send_hold(target, progress_id, cutoff, True, run_id, len(unique_targets))
                refresh_task = asyncio.create_task(
                    self._refresh_holds(holds, cutoff, send_hold, generation, run_id, len(unique_targets)),
                    name=f"offline-history-marker-refresh-{self.backend}-{run_id[:8]}",
                )
                # Fetch every candidate before emitting any row. Each page is
                # persisted immediately, bounding memory to one API page.
                for target in unique_targets:
                    try:
                        boundary = await self._fetch_target(
                            account_id=normalized_account,
                            target=target,
                            baseline=baseline,
                            cutoff=cutoff,
                            run_id=run_id,
                            fetch_page=fetch_page,
                            plan_target=plan_target,
                            account_is_current=account_is_current,
                            generation=generation,
                        )
                        complete[target] = boundary
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        errors[f"{target.kind}:{target.session_id}"] = str(exc)
                        if on_session_error is not None:
                            on_session_error(target, exc)

                # A failed target has no messages emitted from its incomplete
                # scan. Complete targets are delivered under all their holds.
                delivered_boundaries: dict[
                    HistoryTarget,
                    tuple[str | None, float | None, frozenset[str]],
                ] = {}
                for target in unique_targets:
                    boundary = complete.get(target)
                    if boundary is None:
                        continue
                    if retry_delivery_failures:
                        while True:
                            if account_is_current is not None and not account_is_current():
                                raise asyncio.CancelledError("account or connection generation changed")
                            pending = self.store.pending_for_target(
                                self.backend, normalized_account, self.platform, target, limit=self.page_size
                            )
                            if not pending:
                                delivered_boundaries[target] = boundary
                                break
                            sent_any = False
                            for item in pending:
                                if account_is_current is not None and not account_is_current():
                                    raise asyncio.CancelledError("account or connection generation changed")
                                accepted = await emit(item, item.run_id or run_id, item.cutoff_at or cutoff)
                                if accepted:
                                    self.store.acknowledge(item.delivery_key, item.receipt_token, "accepted")
                                    sent_any = True
                            if sent_any:
                                continue
                            # Keep the representative route holds alive while a
                            # receipt is missing; retries resume from disk outbox.
                            if wait_retry is not None:
                                await wait_retry()
                            else:
                                await asyncio.sleep(self.retry_delay)
                        continue

                    while True:
                        if account_is_current is not None and not account_is_current():
                            raise asyncio.CancelledError("account or connection generation changed")
                        pending = self.store.pending_for_target(
                            self.backend, normalized_account, self.platform, target, limit=self.page_size
                        )
                        if not pending:
                            delivered_boundaries[target] = boundary
                            break

                        delivery_error: Exception | None = None
                        for item in pending:
                            if account_is_current is not None and not account_is_current():
                                raise asyncio.CancelledError("account or connection generation changed")
                            try:
                                accepted = await emit(item, item.run_id or run_id, item.cutoff_at or cutoff)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                delivery_error = exc
                                break
                            if not accepted:
                                delivery_error = RuntimeError("history delivery was not acknowledged")
                                break
                            self.store.acknowledge(item.delivery_key, item.receipt_token, "accepted")

                        if delivery_error is None:
                            # The current page was accepted. Continue with the
                            # next pending page without resending acknowledged rows.
                            continue

                        errors[f"{target.kind}:{target.session_id}"] = str(delivery_error)
                        if on_session_error is not None:
                            try:
                                on_session_error(target, delivery_error)
                            except Exception:
                                # A reporting callback must not block the next
                                # independent target after this delivery failed.
                                pass
                        break
                if send_run is not None and unique_targets:
                    await send_run(run_id, run_token, cutoff, tuple(delivered_boundaries), "seal")
                for target, boundary in delivered_boundaries.items():
                    self.store.complete_target(
                        self.backend,
                        normalized_account,
                        self.platform,
                        target,
                        checkpoint_id=boundary[0],
                        checkpoint_timestamp=boundary[1],
                        boundary_ids=boundary[2],
                    )
                return {
                    "run_id": run_id,
                    "baseline_at": baseline,
                    "cutoff_at": cutoff,
                    "complete_sessions": len(complete) if retry_delivery_failures else len(delivered_boundaries),
                    "failed_sessions": errors,
                }
            finally:
                self._generation += 1
                if refresh_task is not None:
                    refresh_task.cancel()
                    await asyncio.gather(refresh_task, return_exceptions=True)
                for target, progress_id in holds.items():
                    try:
                        await send_hold(target, progress_id, cutoff, False, run_id, len(unique_targets))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass

    async def _fetch_target(
        self,
        *,
        account_id: str,
        target: HistoryTarget,
        baseline: float,
        cutoff: float,
        run_id: str,
        fetch_page: FetchPage,
        plan_target: PlanTarget | None,
        account_is_current: AccountIsCurrent | None,
        generation: int,
    ) -> tuple[str | None, float | None, frozenset[str]]:
        checkpoint_id, checkpoint_at, boundary_ids = self.store.checkpoint(
            self.backend, account_id, self.platform, target
        )
        live_anchor_id, live_anchor_at = self.store.live_anchor(
            self.backend, account_id, self.platform, target
        )
        boundary_at = checkpoint_at if checkpoint_at is not None else baseline
        if plan_target is None:
            plan = ScanPlan(
                direction="backward",
                initial_cursor=None,
                boundary_id=checkpoint_id,
                boundary_ids=boundary_ids,
                boundary_at=boundary_at,
            )
        else:
            plan = plan_target(
                target,
                checkpoint_id,
                checkpoint_at,
                boundary_ids,
                live_anchor_id,
                live_anchor_at,
                baseline,
            )
        if not isinstance(plan, ScanPlan):
            raise HistoryPageError("history scan plan was malformed")
        if plan.direction == "forward" and not plan.initial_cursor:
            raise HistoryPageError("forward group history has no validated message anchor; coverage is unavailable")
        if plan.require_exact_id and not plan.boundary_id:
            raise HistoryPageError("history scan requires a stable checkpoint ID")

        original_checkpoint_id = checkpoint_id
        original_checkpoint_at = checkpoint_at
        newest_id = checkpoint_id
        newest_at = checkpoint_at
        newest_ids = set(boundary_ids)
        cursor = plan.initial_cursor
        seen_cursors: set[str] = {cursor} if cursor else set()
        boundary_found = False
        reached_boundary_id = False
        if plan.direction == "forward" and not checkpoint_id and live_anchor_id:
            newest_id = live_anchor_id
            newest_at = live_anchor_at
            newest_ids = {live_anchor_id}
        sort_order = 0
        for _page_number in range(self.max_pages):
            if generation != self._generation:
                raise asyncio.CancelledError("history run generation changed")
            if account_is_current is not None and not account_is_current():
                raise asyncio.CancelledError("account or connection generation changed")
            page = await fetch_page(target, cursor, self.page_size, plan.direction)
            if not isinstance(page, HistoryPage) or not page.authoritative:
                raise HistoryPageError("history page was not authoritative")
            messages = tuple(page.messages)
            if not messages and not (page.end_of_history or page.boundary_reached):
                raise HistoryPageError("empty history page did not prove an end or checkpoint boundary")

            validated: list[tuple[float, str, Mapping[str, Any]]] = []
            for message in messages:
                if not isinstance(message, Mapping):
                    raise HistoryPageError("history page contained a malformed message")
                message_id = str(message.get("message_id") or "").strip()
                timestamp = _finite_timestamp(message.get("time"))
                if not message_id or timestamp is None:
                    raise HistoryPageError("history page contained a message without a stable ID or timestamp")
                self._validate_route(target, message)
                validated.append((timestamp, message_id, message))

            stage: list[Mapping[str, Any]] = []
            if plan.direction == "forward":
                for timestamp, message_id, message in validated:
                    if message_id in plan.boundary_ids or message_id == cursor:
                        continue
                    if timestamp < plan.boundary_at or timestamp > cutoff:
                        continue
                    if newest_at is None or timestamp > newest_at:
                        newest_at, newest_id, newest_ids = timestamp, message_id, {message_id}
                    elif timestamp == newest_at:
                        newest_id = message_id
                        newest_ids.add(message_id)
                    if not message.get("_offline_history_skip"):
                        stage.append(message)
            else:
                positions = [
                    index for index, (_timestamp, message_id, _message) in enumerate(validated)
                    if message_id in plan.boundary_ids
                ]
                boundary_position = max(positions) if positions else None
                if any(message_id == plan.boundary_id for _timestamp, message_id, _message in validated):
                    reached_boundary_id = True
                crossed_time_boundary = False
                for index, (timestamp, message_id, message) in enumerate(validated):
                    if timestamp > cutoff:
                        continue
                    if plan.boundary_id is None:
                        if timestamp <= plan.boundary_at:
                            crossed_time_boundary = True
                            continue
                    elif timestamp < plan.boundary_at:
                        crossed_time_boundary = True
                        continue
                    if message_id in plan.boundary_ids:
                        continue
                    if boundary_position is not None and index <= boundary_position and timestamp > plan.boundary_at:
                        continue
                    # Distinct IDs at the exact boundary second are retained;
                    # the persisted ID set suppresses only known old messages.
                    if newest_at is None or timestamp > newest_at:
                        newest_at, newest_id, newest_ids = timestamp, message_id, {message_id}
                    elif timestamp == newest_at:
                        newest_id = message_id
                        newest_ids.add(message_id)
                    if not message.get("_offline_history_skip"):
                        stage.append(message)
                if reached_boundary_id or crossed_time_boundary:
                    boundary_found = True

            if stage:
                self.store.stage_page(
                    self.backend,
                    account_id,
                    self.platform,
                    target,
                    stage,
                    run_id=run_id,
                    cutoff_at=cutoff,
                    sort_order_start=sort_order,
                )
                sort_order += len(stage)

            cursor_value = page.next_cursor
            if cursor_value is not None:
                cursor_value = str(cursor_value).strip()
                if not cursor_value:
                    cursor_value = None
            if cursor_value and cursor_value in seen_cursors:
                raise HistoryPageError("history pagination repeated a cursor")
            if cursor_value:
                seen_cursors.add(cursor_value)
            progressed = cursor_value != cursor
            if page.boundary_reached:
                boundary_found = True
                break
            if boundary_found:
                break
            if page.end_of_history:
                boundary_found = True
                break
            if not progressed:
                raise HistoryPageError("history pagination made no progress")
            if cursor_value is None:
                raise HistoryPageError("history page omitted a next cursor before reaching the boundary")
            cursor = cursor_value
        else:
            raise HistoryPageError("history page cap reached before a proven boundary")

        if not boundary_found:
            raise HistoryPageError("history scan did not prove its checkpoint boundary")
        if newest_id is None:
            # No new messages: retain the old checkpoint and its tie IDs.
            return original_checkpoint_id, original_checkpoint_at, boundary_ids
        return newest_id, newest_at, frozenset(newest_ids)

    @staticmethod
    def _validate_route(target: HistoryTarget, message: Mapping[str, Any]) -> None:
        kind = str(message.get("message_type") or "").strip().lower()
        if kind and kind != target.kind:
            raise HistoryPageError("history response message type did not match the requested route")
        if target.kind == "group":
            route = message.get("group_id")
            if route is None or str(route).strip() != target.session_id:
                raise HistoryPageError("history response group route did not match the requested group")
        else:
            if message.get("group_id") is not None:
                raise HistoryPageError("private history response unexpectedly contained a group route")
            sender = message.get("sender")
            sender_id = sender.get("user_id") if isinstance(sender, Mapping) else message.get("user_id")
            outgoing = bool(message.get("_offline_history_outgoing"))
            target_id = message.get("target_id")
            if sender_id is None or (
                str(sender_id).strip() != target.session_id
                and not (outgoing and str(target_id or "").strip() == target.session_id)
            ):
                raise HistoryPageError("history response private route did not match the requested user")

    async def _refresh_holds(
        self,
        holds: Mapping[HistoryTarget, str],
        cutoff: float,
        send_hold: SendHold,
        generation: int,
        run_id: str,
        route_total: int,
    ) -> None:
        while generation == self._generation:
            await asyncio.sleep(self.marker_refresh_sec)
            if generation != self._generation:
                return
            for target, progress_id in holds.items():
                await send_hold(target, progress_id, cutoff, True, run_id, route_total)
