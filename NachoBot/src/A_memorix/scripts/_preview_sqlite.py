"""Small read-only SQLite helpers for command-line migration previews."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing database in SQLite read-only mode, without a fallback."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()}
