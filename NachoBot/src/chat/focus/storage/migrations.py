"""Focus-owned, monotonic SQLite schema migrations."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


FOCUS_SCHEMA_VERSION = 2


_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS focus_group_state (
                group_id TEXT PRIMARY KEY,
                active_chat_id TEXT,
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                membership_hash TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS focus_chat_cursor (
                group_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                processed_row_id INTEGER NOT NULL DEFAULT 0 CHECK (processed_row_id >= 0),
                last_viewed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (group_id, chat_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS focus_event (
                event_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                first_row_id INTEGER NOT NULL CHECK (first_row_id > 0),
                last_row_id INTEGER NOT NULL CHECK (last_row_id >= first_row_id),
                unread_count INTEGER NOT NULL CHECK (unread_count > 0),
                has_mention INTEGER NOT NULL DEFAULT 0,
                has_at INTEGER NOT NULL DEFAULT 0,
                latest_preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                last_delivered_revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS focus_event_pending_chat_idx
            ON focus_event(group_id, chat_id)
            WHERE status = 'pending'
            """,
            """
            CREATE INDEX IF NOT EXISTS focus_event_group_status_idx
            ON focus_event(group_id, status, updated_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS focus_handoff (
                handoff_id TEXT PRIMARY KEY,
                parent_id TEXT,
                group_id TEXT NOT NULL,
                source_chat_id TEXT NOT NULL,
                target_chat_id TEXT NOT NULL,
                source_epoch INTEGER NOT NULL CHECK (source_epoch >= 0),
                target_epoch INTEGER NOT NULL CHECK (target_epoch = source_epoch + 1),
                payload_json TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                max_cycles INTEGER NOT NULL CHECK (max_cycles > 0),
                acked_cycles INTEGER NOT NULL DEFAULT 0 CHECK (acked_cycles >= 0)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS focus_handoff_target_idx
            ON focus_handoff(group_id, target_chat_id, target_epoch, status, expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS focus_handoff_reservation (
                reservation_id TEXT PRIMARY KEY,
                handoff_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                target_chat_id TEXT NOT NULL,
                target_epoch INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                delivery_id TEXT,
                UNIQUE (handoff_id, cycle_id),
                FOREIGN KEY (handoff_id) REFERENCES focus_handoff(handoff_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS focus_reservation_expiry_idx
            ON focus_handoff_reservation(state, lease_expires_at)
            """,
        ),
    ),
    (
        2,
        (
            """
            ALTER TABLE focus_event
            ADD COLUMN visible INTEGER NOT NULL DEFAULT 0
            """,
        ),
    ),
)


def migrate_focus_database(database_path: str | Path) -> int:
    """Apply all Focus migrations in one process-safe immediate transaction."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS focus_schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            )
            """
        )
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM focus_schema_version").fetchone()
        current_version = int(row[0]) if row else 0
        if current_version > FOCUS_SCHEMA_VERSION:
            raise RuntimeError(
                f"Focus database schema {current_version} is newer than supported {FOCUS_SCHEMA_VERSION}"
            )
        for version, statements in _MIGRATIONS:
            if version <= current_version:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO focus_schema_version(version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            current_version = version
        connection.commit()
        return current_version
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
