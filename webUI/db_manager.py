"""
NachoBot WebUI — Database Manager
Provides read/write access to the NachoBot SQLite database for the WebUI.
"""

import sqlite3
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "NachoBot" / "data" / "NachoBot.db"

# Tables that allow editing/deleting via the WebUI
EDITABLE_TABLES = {"person_info", "group_info", "expression"}

# Fields that should be truncated in list views (long text)
TRUNCATE_FIELDS = {"memory_points", "member_list", "group_impression", "topic",
                   "memory_items", "original_text", "summary", "key_point",
                   "thinking_steps", "action_data", "action_prompt_display",
                   "processed_plain_text", "display_message", "priority_info",
                   "selected_expressions", "additional_config", "description",
                   "key_words", "key_words_lite", "context", "answer"}
TRUNCATE_LENGTH = 80

# Human-readable table name mapping
TABLE_LABELS = {
    "person_info": "用户信息",
    "group_info": "群组信息",
    "chat_streams": "会话流",
    "messages": "消息记录",
    "chat_history": "聊天历史概括",
    "llm_usage": "LLM 调用记录",
    "emoji": "表情包",
    "expression": "表达风格",
    "images": "图像信息",
    "image_descriptions": "图像描述",
    "graph_nodes": "记忆图节点",
    "graph_edges": "记忆图边",
    "action_records": "动作记录",
    "online_time": "在线时间",
    "person_bindings": "跨平台绑定",
    "bind_requests": "绑定请求",
    "thinking_back": "记忆检索",
}

# Tables to exclude from browsing
HIDDEN_TABLES = {"sqlite_stat1", "sqlite_stat4"}


def _get_conn() -> sqlite3.Connection:
    """Get a read-only connection to the database."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=wal")
    return conn


def _get_rw_conn() -> sqlite3.Connection:
    """Get a read-write connection (for editable tables only)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class DatabaseManager:
    """Manages read/write access to NachoBot.db for the WebUI."""

    def db_exists(self) -> bool:
        return DB_PATH.exists()

    def get_stats(self) -> dict[str, Any]:
        """Return database overview statistics."""
        if not self.db_exists():
            return {"exists": False}

        conn = _get_conn()
        try:
            size_bytes = DB_PATH.stat().st_size
            tables = self._get_table_names(conn)
            table_stats = []
            for t in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                table_stats.append({
                    "name": t,
                    "label": TABLE_LABELS.get(t, t),
                    "rows": count,
                    "editable": t in EDITABLE_TABLES,
                })
            return {
                "exists": True,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / 1024 / 1024, 1),
                "tables": table_stats,
            }
        finally:
            conn.close()

    def list_tables(self) -> list[dict[str, Any]]:
        """List all browsable tables with column info."""
        if not self.db_exists():
            return []

        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            result = []
            for t in tables:
                cols = self._get_columns(conn, t)
                count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                result.append({
                    "name": t,
                    "label": TABLE_LABELS.get(t, t),
                    "columns": cols,
                    "rows": count,
                    "editable": t in EDITABLE_TABLES,
                })
            return result
        finally:
            conn.close()

    def query_table(
        self,
        table: str,
        page: int = 1,
        page_size: int = 50,
        search: str = "",
        sort_by: str = "id",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """Query a table with pagination, search, and sorting."""
        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            if table not in tables:
                raise ValueError(f"Table not found: {table}")

            cols = self._get_columns(conn, table)
            col_names = [c["name"] for c in cols]

            # Validate sort column
            if sort_by not in col_names:
                sort_by = "id" if "id" in col_names else col_names[0]
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"

            # Build query
            where_clause = ""
            params: list[Any] = []
            if search:
                # Search across all text columns
                text_cols = [c["name"] for c in cols if c["type"] in ("TEXT", "")]
                if text_cols:
                    conditions = [f"CAST([{c}] AS TEXT) LIKE ?" for c in text_cols]
                    where_clause = "WHERE " + " OR ".join(conditions)
                    params = [f"%{search}%"] * len(text_cols)

            # Get total count
            count_sql = f"SELECT COUNT(*) FROM [{table}] {where_clause}"
            total = conn.execute(count_sql, params).fetchone()[0]

            # Get page data
            offset = (page - 1) * page_size
            data_sql = f"SELECT * FROM [{table}] {where_clause} ORDER BY [{sort_by}] {sort_order} LIMIT ? OFFSET ?"
            rows = conn.execute(data_sql, params + [page_size, offset]).fetchall()

            # Convert to dicts, truncating long fields
            data = []
            for row in rows:
                d = dict(row)
                for key, val in d.items():
                    if key in TRUNCATE_FIELDS and isinstance(val, str) and len(val) > TRUNCATE_LENGTH:
                        d[key] = val[:TRUNCATE_LENGTH] + "..."
                data.append(d)

            return {
                "table": table,
                "label": TABLE_LABELS.get(table, table),
                "columns": cols,
                "data": data,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "editable": table in EDITABLE_TABLES,
            }
        finally:
            conn.close()

    def get_row(self, table: str, row_id: int) -> dict[str, Any]:
        """Get a single row by ID (full data, no truncation)."""
        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            if table not in tables:
                raise ValueError(f"Table not found: {table}")

            row = conn.execute(f"SELECT * FROM [{table}] WHERE id = ?", (row_id,)).fetchone()
            if not row:
                raise ValueError(f"Row not found: {table}.{row_id}")

            cols = self._get_columns(conn, table)
            return {
                "table": table,
                "columns": cols,
                "data": dict(row),
                "editable": table in EDITABLE_TABLES,
            }
        finally:
            conn.close()

    def update_row(self, table: str, row_id: int, data: dict[str, Any]) -> None:
        """Update a row (only for editable tables)."""
        if table not in EDITABLE_TABLES:
            raise PermissionError(f"Table '{table}' is not editable")

        conn = _get_rw_conn()
        try:
            # Verify row exists
            existing = conn.execute(f"SELECT id FROM [{table}] WHERE id = ?", (row_id,)).fetchone()
            if not existing:
                raise ValueError(f"Row not found: {table}.{row_id}")

            # Build UPDATE statement
            cols = list(data.keys())
            # Never allow updating 'id'
            cols = [c for c in cols if c != "id"]
            if not cols:
                return

            set_clause = ", ".join(f"[{c}] = ?" for c in cols)
            values = [data[c] for c in cols]
            values.append(row_id)

            conn.execute(f"UPDATE [{table}] SET {set_clause} WHERE id = ?", values)
            conn.commit()
        finally:
            conn.close()

    def delete_row(self, table: str, row_id: int) -> None:
        """Delete a row (only for editable tables)."""
        if table not in EDITABLE_TABLES:
            raise PermissionError(f"Table '{table}' is not editable")

        conn = _get_rw_conn()
        try:
            existing = conn.execute(f"SELECT id FROM [{table}] WHERE id = ?", (row_id,)).fetchone()
            if not existing:
                raise ValueError(f"Row not found: {table}.{row_id}")

            conn.execute(f"DELETE FROM [{table}] WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    # ---- internal helpers ----

    def _get_table_names(self, conn: sqlite3.Connection) -> list[str]:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r[0] for r in cursor.fetchall() if r[0] not in HIDDEN_TABLES]

    def _get_columns(self, conn: sqlite3.Connection, table: str) -> list[dict[str, str]]:
        cursor = conn.execute(f"PRAGMA table_info([{table}])")
        return [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])} for r in cursor.fetchall()]
