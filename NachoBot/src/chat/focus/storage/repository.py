"""Async facade over Focus's versioned SQLite tables."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from ..models import (
    FocusEventSnapshot,
    FocusEventStatus,
    FocusHandoff,
    HandoffPayload,
    HandoffStatus,
    UntrustedExcerpt,
)
from .migrations import migrate_focus_database
from .models import FocusCursorRecord, FocusEventRecord, FocusGroupStateRecord, HandoffReservationRecord


T = TypeVar("T")


def default_focus_database_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "NachoBot.db"


class FocusSQLiteStorage:
    """Focus state repository and durable ``HandoffStore`` implementation.

    A connection is opened per operation.  This is important because methods
    run through ``asyncio.to_thread`` and sqlite connections are thread-bound.
    """

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path is not None else default_focus_database_path()

    async def migrate(self) -> int:
        return await asyncio.to_thread(migrate_focus_database, self.database_path)

    async def save_group_state(
        self,
        group_id: str,
        active_chat_id: str | None,
        epoch: int,
        membership_hash: str,
        *,
        updated_at: float | None = None,
    ) -> None:
        timestamp = time.time() if updated_at is None else updated_at

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO focus_group_state(group_id, active_chat_id, epoch, membership_hash, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    active_chat_id=excluded.active_chat_id,
                    epoch=excluded.epoch,
                    membership_hash=excluded.membership_hash,
                    updated_at=excluded.updated_at
                WHERE excluded.epoch >= focus_group_state.epoch
                """,
                (group_id, active_chat_id, epoch, membership_hash, timestamp),
            )

        await self._write(operation)

    async def load_group_states(self) -> tuple[FocusGroupStateRecord, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[FocusGroupStateRecord, ...]:
            rows = connection.execute(
                "SELECT group_id, active_chat_id, epoch, membership_hash, updated_at FROM focus_group_state"
            ).fetchall()
            return tuple(FocusGroupStateRecord(**dict(row)) for row in rows)

        return await self._read(operation)

    async def migrate_idle_group_membership(
        self,
        *,
        group_id: str,
        expected_epoch: int,
        expected_membership_hash: str,
        new_membership_hash: str,
        member_baselines: Mapping[str, int],
        fallback_active_chat_id: str | None,
        allow_removals: bool,
        updated_at: float | None = None,
    ) -> bool:
        """Atomically migrate an idle Focus group's membership.

        Existing member cursors are preserved, new members start at their latest
        stored row, and removed cursors are deleted only when ``allow_removals``
        is true.  If the active member was removed, the configured fallback is
        selected in the same transaction.  Live Focus work always fails closed.
        """

        timestamp = time.time() if updated_at is None else updated_at
        configured_chat_ids = set(member_baselines)
        if not configured_chat_ids or expected_membership_hash == new_membership_hash:
            return False
        if any(row_id < 0 for row_id in member_baselines.values()):
            raise ValueError("Focus member baselines cannot be negative")

        def operation(connection: sqlite3.Connection) -> bool:
            state = connection.execute(
                """
                SELECT active_chat_id, epoch, membership_hash
                FROM focus_group_state WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            if (
                state is None
                or int(state["epoch"]) != expected_epoch
                or state["membership_hash"] != expected_membership_hash
            ):
                return False

            existing_chat_ids = {
                str(row["chat_id"])
                for row in connection.execute(
                    "SELECT chat_id FROM focus_chat_cursor WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
            }
            added_chat_ids = configured_chat_ids - existing_chat_ids
            removed_chat_ids = existing_chat_ids - configured_chat_ids
            if not existing_chat_ids:
                return False
            if not allow_removals and (removed_chat_ids or not added_chat_ids):
                return False

            active_chat_id = state["active_chat_id"]
            selected_active_chat_id = active_chat_id
            if active_chat_id is not None and active_chat_id not in configured_chat_ids:
                if not allow_removals or fallback_active_chat_id not in configured_chat_ids:
                    return False
                selected_active_chat_id = fallback_active_chat_id

            pending_event = connection.execute(
                "SELECT 1 FROM focus_event WHERE group_id = ? AND status = 'pending' LIMIT 1",
                (group_id,),
            ).fetchone()
            active_handoff = connection.execute(
                "SELECT 1 FROM focus_handoff WHERE group_id = ? AND status = 'active' LIMIT 1",
                (group_id,),
            ).fetchone()
            reserved_delivery = connection.execute(
                """
                SELECT 1
                FROM focus_handoff_reservation AS reservation
                JOIN focus_handoff AS handoff ON handoff.handoff_id = reservation.handoff_id
                WHERE handoff.group_id = ? AND reservation.state = 'reserved'
                LIMIT 1
                """,
                (group_id,),
            ).fetchone()
            if pending_event is not None or active_handoff is not None or reserved_delivery is not None:
                return False

            state_cursor = connection.execute(
                """
                UPDATE focus_group_state
                SET active_chat_id = ?, epoch = epoch + 1, membership_hash = ?, updated_at = ?
                WHERE group_id = ? AND epoch = ? AND membership_hash = ?
                """,
                (
                    selected_active_chat_id,
                    new_membership_hash,
                    timestamp,
                    group_id,
                    expected_epoch,
                    expected_membership_hash,
                ),
            )
            if state_cursor.rowcount != 1:
                return False
            if removed_chat_ids:
                placeholders = ",".join("?" for _ in removed_chat_ids)
                connection.execute(
                    f"DELETE FROM focus_chat_cursor WHERE group_id = ? AND chat_id IN ({placeholders})",
                    (group_id, *sorted(removed_chat_ids)),
                )
            for chat_id in sorted(added_chat_ids):
                connection.execute(
                    """
                    INSERT INTO focus_chat_cursor(
                        group_id, chat_id, processed_row_id, last_viewed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, chat_id, member_baselines[chat_id], timestamp, timestamp),
                )
            return True

        return await self._write(operation)

    async def save_cursor(
        self,
        group_id: str,
        chat_id: str,
        processed_row_id: int,
        last_viewed_at: float,
        *,
        updated_at: float | None = None,
    ) -> None:
        timestamp = time.time() if updated_at is None else updated_at

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO focus_chat_cursor(group_id, chat_id, processed_row_id, last_viewed_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, chat_id) DO UPDATE SET
                    processed_row_id=MAX(focus_chat_cursor.processed_row_id, excluded.processed_row_id),
                    last_viewed_at=MAX(focus_chat_cursor.last_viewed_at, excluded.last_viewed_at),
                    updated_at=excluded.updated_at
                """,
                (group_id, chat_id, processed_row_id, last_viewed_at, timestamp),
            )

        await self._write(operation)

    async def load_cursors(self, group_id: str) -> tuple[FocusCursorRecord, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[FocusCursorRecord, ...]:
            rows = connection.execute(
                """
                SELECT group_id, chat_id, processed_row_id, last_viewed_at, updated_at
                FROM focus_chat_cursor WHERE group_id = ?
                """,
                (group_id,),
            ).fetchall()
            return tuple(FocusCursorRecord(**dict(row)) for row in rows)

        return await self._read(operation)

    async def upsert_event(
        self,
        group_id: str,
        event: FocusEventSnapshot,
        *,
        status: FocusEventStatus = FocusEventStatus.PENDING,
        last_delivered_revision: int = 0,
        visible: bool = False,
        expires_at: float | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO focus_event(
                    event_id, group_id, chat_id, revision, first_row_id, last_row_id,
                    unread_count, has_mention, has_at, latest_preview, status,
                    last_delivered_revision, visible, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    revision=MAX(focus_event.revision, excluded.revision),
                    last_row_id=MAX(focus_event.last_row_id, excluded.last_row_id),
                    unread_count=MAX(focus_event.unread_count, excluded.unread_count),
                    has_mention=MAX(focus_event.has_mention, excluded.has_mention),
                    has_at=MAX(focus_event.has_at, excluded.has_at),
                    latest_preview=CASE
                        WHEN excluded.revision >= focus_event.revision
                        THEN excluded.latest_preview
                        ELSE focus_event.latest_preview
                    END,
                    status=CASE
                        WHEN focus_event.status = 'pending' THEN excluded.status
                        ELSE focus_event.status
                    END,
                    last_delivered_revision=MAX(
                        focus_event.last_delivered_revision,
                        excluded.last_delivered_revision
                    ),
                    visible=MAX(focus_event.visible, excluded.visible),
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (
                    event.event_id,
                    group_id,
                    event.target_chat_id,
                    event.revision,
                    event.first_unread.row_id,
                    event.last_unread.row_id,
                    event.unread_count,
                    int(event.is_mentioned),
                    int(event.is_at),
                    event.latest_preview,
                    status.value,
                    last_delivered_revision,
                    int(visible),
                    timestamp,
                    timestamp,
                    expires_at,
                ),
            )

        await self._write(operation)

    async def load_pending_events(self, group_id: str | None = None) -> tuple[FocusEventRecord, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[FocusEventRecord, ...]:
            query = """
                SELECT event_id, group_id, chat_id, revision, first_row_id, last_row_id,
                       unread_count, has_mention, has_at, latest_preview, status,
                       last_delivered_revision, visible, created_at, updated_at, expires_at
                FROM focus_event WHERE status = 'pending'
            """
            parameters: tuple[Any, ...] = ()
            if group_id is not None:
                query += " AND group_id = ?"
                parameters = (group_id,)
            query += " ORDER BY created_at, event_id"
            rows = connection.execute(query, parameters).fetchall()
            return tuple(
                FocusEventRecord(
                    event_id=row["event_id"],
                    group_id=row["group_id"],
                    chat_id=row["chat_id"],
                    revision=row["revision"],
                    first_row_id=row["first_row_id"],
                    last_row_id=row["last_row_id"],
                    unread_count=row["unread_count"],
                    has_mention=bool(row["has_mention"]),
                    has_at=bool(row["has_at"]),
                    latest_preview=row["latest_preview"],
                    status=FocusEventStatus(row["status"]),
                    last_delivered_revision=row["last_delivered_revision"],
                    visible=bool(row["visible"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    expires_at=row["expires_at"],
                )
                for row in rows
            )

        return await self._read(operation)

    async def commit_turn(
        self,
        *,
        group_id: str,
        chat_id: str,
        processed_row_id: int,
        last_viewed_at: float,
        delivered_event_revisions: Mapping[str, int],
        updated_at: float | None = None,
    ) -> None:
        """Atomically persist a successful turn cursor and event deliveries."""

        timestamp = time.time() if updated_at is None else updated_at

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO focus_chat_cursor(group_id, chat_id, processed_row_id, last_viewed_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, chat_id) DO UPDATE SET
                    processed_row_id=MAX(focus_chat_cursor.processed_row_id, excluded.processed_row_id),
                    last_viewed_at=MAX(focus_chat_cursor.last_viewed_at, excluded.last_viewed_at),
                    updated_at=excluded.updated_at
                """,
                (group_id, chat_id, processed_row_id, last_viewed_at, timestamp),
            )
            for event_id, delivered_revision in delivered_event_revisions.items():
                connection.execute(
                    """
                    UPDATE focus_event
                    SET last_delivered_revision=MAX(
                            last_delivered_revision,
                            MIN(revision, ?)
                        ),
                        updated_at=?
                    WHERE event_id = ? AND group_id = ? AND status = 'pending'
                    """,
                    (max(0, int(delivered_revision)), timestamp, event_id, group_id),
                )

        await self._write(operation)

    async def resolve_event(self, event_id: str, *, status: FocusEventStatus = FocusEventStatus.RESOLVED) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE focus_event SET status = ?, updated_at = ? WHERE event_id = ? AND status = 'pending'",
                (status.value, time.time(), event_id),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def put(self, handoff: FocusHandoff) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._insert_handoff(connection, handoff)

        await self._write(operation)

    async def get(self, handoff_id: str) -> FocusHandoff | None:
        await self.expire()

        def operation(connection: sqlite3.Connection) -> FocusHandoff | None:
            row = connection.execute(
                "SELECT * FROM focus_handoff WHERE handoff_id = ? AND status = 'active'",
                (handoff_id,),
            ).fetchone()
            return self._handoff_from_row(row) if row is not None else None

        return await self._read(operation)

    async def get_active(self, group_id: str, target_chat_id: str, target_epoch: int) -> tuple[FocusHandoff, ...]:
        await self.expire()

        def operation(connection: sqlite3.Connection) -> tuple[FocusHandoff, ...]:
            rows = connection.execute(
                """
                SELECT * FROM focus_handoff
                WHERE group_id = ? AND target_chat_id = ? AND target_epoch = ? AND status = 'active'
                ORDER BY created_at, handoff_id
                """,
                (group_id, target_chat_id, target_epoch),
            ).fetchall()
            return tuple(self._handoff_from_row(row) for row in rows)

        return await self._read(operation)

    async def acknowledge(self, handoff_id: str, cycle_id: str, delivery_id: str) -> bool:
        if not cycle_id or not delivery_id:
            raise ValueError("cycle_id and delivery_id are required")

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT status, expires_at, max_cycles FROM focus_handoff WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            if row is None or row["status"] != HandoffStatus.ACTIVE.value or row["expires_at"] <= time.time():
                return False
            prior = connection.execute(
                """
                SELECT reservation_id, state, delivery_id FROM focus_handoff_reservation
                WHERE handoff_id = ? AND cycle_id = ?
                """,
                (handoff_id, cycle_id),
            ).fetchone()
            if prior is not None:
                if prior["delivery_id"] is not None:
                    return prior["state"] == "delivered" and prior["delivery_id"] == delivery_id
                if prior["state"] != "reserved":
                    return False
                connection.execute(
                    """
                    UPDATE focus_handoff_reservation
                    SET state = 'delivered', delivery_id = ?, lease_expires_at = ?
                    WHERE reservation_id = ? AND state = 'reserved'
                    """,
                    (delivery_id, time.time(), prior["reservation_id"]),
                )
            else:
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO focus_handoff_reservation(
                        reservation_id, handoff_id, cycle_id, target_chat_id, target_epoch,
                        state, created_at, lease_expires_at, delivery_id
                    )
                    SELECT ?, handoff_id, ?, target_chat_id, target_epoch, 'delivered', ?, ?, ?
                    FROM focus_handoff WHERE handoff_id = ?
                    """,
                    (uuid.uuid4().hex, cycle_id, now, now, delivery_id, handoff_id),
                )
            count_row = connection.execute(
                """
                SELECT COUNT(*) FROM focus_handoff_reservation
                WHERE handoff_id = ? AND state = 'delivered'
                """,
                (handoff_id,),
            ).fetchone()
            delivered = int(count_row[0]) if count_row else 0
            status = HandoffStatus.CONSUMED.value if delivered >= int(row["max_cycles"]) else HandoffStatus.ACTIVE.value
            connection.execute(
                "UPDATE focus_handoff SET acked_cycles = ?, status = ? WHERE handoff_id = ?",
                (delivered, status, handoff_id),
            )
            return True

        return await self._write(operation)

    async def supersede(self, handoff_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE focus_handoff SET status = ? WHERE handoff_id = ? AND status = ?",
                (HandoffStatus.SUPERSEDED.value, handoff_id, HandoffStatus.ACTIVE.value),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def expire(self, now: float | None = None) -> int:
        deadline = time.time() if now is None else now

        def operation(connection: sqlite3.Connection) -> int:
            connection.execute(
                """
                UPDATE focus_handoff_reservation SET state = 'released'
                WHERE state = 'reserved' AND lease_expires_at <= ?
                """,
                (deadline,),
            )
            cursor = connection.execute(
                "UPDATE focus_handoff SET status = ? WHERE status = ? AND expires_at <= ?",
                (HandoffStatus.EXPIRED.value, HandoffStatus.ACTIVE.value, deadline),
            )
            return cursor.rowcount

        return await self._write(operation)

    async def reserve(
        self,
        handoff_id: str,
        cycle_id: str,
        target_chat_id: str,
        target_epoch: int,
        *,
        ttl_seconds: float = 120,
    ) -> HandoffReservationRecord | None:
        now = time.time()
        reservation_id = uuid.uuid4().hex

        def operation(connection: sqlite3.Connection) -> HandoffReservationRecord | None:
            handoff = connection.execute(
                """
                SELECT handoff_id FROM focus_handoff
                WHERE handoff_id = ? AND target_chat_id = ? AND target_epoch = ?
                  AND status = 'active' AND expires_at > ?
                """,
                (handoff_id, target_chat_id, target_epoch, now),
            ).fetchone()
            if handoff is None:
                return None
            existing = connection.execute(
                "SELECT * FROM focus_handoff_reservation WHERE handoff_id = ? AND cycle_id = ?",
                (handoff_id, cycle_id),
            ).fetchone()
            if existing is not None:
                if existing["state"] == "delivered":
                    return self._reservation_from_row(existing)
                if existing["state"] == "reserved" and existing["lease_expires_at"] > now:
                    return self._reservation_from_row(existing)
                connection.execute(
                    """
                    UPDATE focus_handoff_reservation
                    SET state = 'reserved', created_at = ?, lease_expires_at = ?, delivery_id = NULL
                    WHERE reservation_id = ?
                    """,
                    (now, now + ttl_seconds, existing["reservation_id"]),
                )
                refreshed = connection.execute(
                    "SELECT * FROM focus_handoff_reservation WHERE reservation_id = ?",
                    (existing["reservation_id"],),
                ).fetchone()
                return self._reservation_from_row(refreshed)
            connection.execute(
                """
                INSERT INTO focus_handoff_reservation(
                    reservation_id, handoff_id, cycle_id, target_chat_id, target_epoch,
                    state, created_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (reservation_id, handoff_id, cycle_id, target_chat_id, target_epoch, now, now + ttl_seconds),
            )
            row = connection.execute(
                "SELECT * FROM focus_handoff_reservation WHERE reservation_id = ?", (reservation_id,)
            ).fetchone()
            return self._reservation_from_row(row)

        return await self._write(operation)

    async def release_reservation(self, reservation_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE focus_handoff_reservation SET state = 'released'
                WHERE reservation_id = ? AND state = 'reserved'
                """,
                (reservation_id,),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def compare_and_set_switch(
        self,
        *,
        group_id: str,
        source_chat_id: str,
        expected_epoch: int,
        target_chat_id: str,
        event_id: str,
        expected_event_revision: int,
        handoff: FocusHandoff | None,
        membership_hash: str,
        switched_at: float | None = None,
    ) -> bool:
        """Atomically switch active state, resolve the event, and store handoff."""

        timestamp = time.time() if switched_at is None else switched_at

        def operation(connection: sqlite3.Connection) -> bool:
            state_row = connection.execute(
                """
                SELECT 1 FROM focus_group_state
                WHERE group_id = ? AND active_chat_id = ? AND epoch = ?
                  AND membership_hash = ?
                """,
                (group_id, source_chat_id, expected_epoch, membership_hash),
            ).fetchone()
            event_row = connection.execute(
                """
                SELECT 1 FROM focus_event
                WHERE event_id = ? AND group_id = ? AND chat_id = ?
                  AND revision = ? AND status = 'pending' AND visible = 1
                """,
                (event_id, group_id, target_chat_id, expected_event_revision),
            ).fetchone()
            if state_row is None or event_row is None:
                return False

            state_cursor = connection.execute(
                """
                UPDATE focus_group_state
                SET active_chat_id = ?, epoch = ?, membership_hash = ?, updated_at = ?
                WHERE group_id = ? AND active_chat_id = ? AND epoch = ?
                  AND membership_hash = ?
                """,
                (
                    target_chat_id,
                    expected_epoch + 1,
                    membership_hash,
                    timestamp,
                    group_id,
                    source_chat_id,
                    expected_epoch,
                    membership_hash,
                ),
            )
            event_cursor = connection.execute(
                """
                UPDATE focus_event SET status = ?, updated_at = ?
                WHERE event_id = ? AND group_id = ? AND chat_id = ?
                  AND revision = ? AND status = 'pending'
                """,
                (
                    FocusEventStatus.RESOLVED.value,
                    timestamp,
                    event_id,
                    group_id,
                    target_chat_id,
                    expected_event_revision,
                ),
            )
            if state_cursor.rowcount != 1 or event_cursor.rowcount != 1:
                raise RuntimeError("Focus switch CAS invariants changed inside an immediate transaction")
            connection.execute(
                """
                INSERT INTO focus_chat_cursor(
                    group_id, chat_id, processed_row_id, last_viewed_at, updated_at
                ) VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(group_id, chat_id) DO UPDATE SET
                    last_viewed_at=MAX(focus_chat_cursor.last_viewed_at, excluded.last_viewed_at),
                    updated_at=excluded.updated_at
                """,
                (group_id, target_chat_id, timestamp, timestamp),
            )
            if handoff is not None:
                self._insert_handoff(connection, handoff)
            return True

        return await self._write(operation)

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run, operation, False)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run, operation, True)

    def _run(self, operation: Callable[[sqlite3.Connection], T], write: bool) -> T:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            if write:
                connection.commit()
            return result
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_handoff(connection: sqlite3.Connection, handoff: FocusHandoff) -> None:
        payload_json = json.dumps(
            {
                "task_summary": handoff.payload.task_summary,
                "source_display_name": handoff.payload.source_display_name,
                "target_display_name": handoff.payload.target_display_name,
                "known_facts": list(handoff.payload.known_facts),
                "pending_items": list(handoff.payload.pending_items),
                "recent_results": list(handoff.payload.recent_results),
                "excerpts": [
                    {
                        "speaker_label": excerpt.speaker_label,
                        "text": excerpt.text,
                        "source_message_id": excerpt.source_message_id,
                    }
                    for excerpt in handoff.payload.excerpts
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO focus_handoff(
                handoff_id, parent_id, group_id, source_chat_id, target_chat_id,
                source_epoch, target_epoch, payload_json, policy_version, revision,
                status, created_at, expires_at, max_cycles, acked_cycles
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(handoff_id) DO NOTHING
            """,
            (
                handoff.handoff_id,
                handoff.parent_id,
                handoff.group_id,
                handoff.source_chat_id,
                handoff.target_chat_id,
                handoff.source_epoch,
                handoff.target_epoch,
                payload_json,
                handoff.policy_version,
                handoff.revision,
                handoff.status.value,
                handoff.created_at,
                handoff.expires_at,
                handoff.max_successful_cycles,
            ),
        )

    @staticmethod
    def _handoff_from_row(row: sqlite3.Row) -> FocusHandoff:
        payload_data = json.loads(row["payload_json"])
        payload = HandoffPayload(
            task_summary=payload_data.get("task_summary", ""),
            source_display_name=payload_data.get("source_display_name", ""),
            target_display_name=payload_data.get("target_display_name", ""),
            known_facts=tuple(payload_data.get("known_facts", ())),
            pending_items=tuple(payload_data.get("pending_items", ())),
            recent_results=tuple(payload_data.get("recent_results", ())),
            excerpts=tuple(UntrustedExcerpt(**value) for value in payload_data.get("excerpts", ())),
        )
        return FocusHandoff(
            handoff_id=row["handoff_id"],
            parent_id=row["parent_id"],
            group_id=row["group_id"],
            source_chat_id=row["source_chat_id"],
            target_chat_id=row["target_chat_id"],
            source_epoch=row["source_epoch"],
            target_epoch=row["target_epoch"],
            payload=payload,
            policy_version=row["policy_version"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            max_successful_cycles=row["max_cycles"],
            revision=row["revision"],
            status=HandoffStatus(row["status"]),
        )

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> HandoffReservationRecord:
        return HandoffReservationRecord(
            reservation_id=row["reservation_id"],
            handoff_id=row["handoff_id"],
            cycle_id=row["cycle_id"],
            target_chat_id=row["target_chat_id"],
            target_epoch=row["target_epoch"],
            state=row["state"],
            created_at=row["created_at"],
            lease_expires_at=row["lease_expires_at"],
            delivery_id=row["delivery_id"],
        )
