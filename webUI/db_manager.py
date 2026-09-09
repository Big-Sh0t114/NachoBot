"""
NachoBot WebUI — Database Manager
Provides read/write access to the NachoBot SQLite database for the WebUI.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

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
MAX_PAGE_SIZE = 200
MAX_PAGE_NUMBER = 1_000_000

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
SQLITE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier after validation against existing schema."""
    text = str(identifier)
    if not SQLITE_IDENTIFIER_RE.fullmatch(text):
        raise ValueError("Invalid SQLite identifier")
    return f'"{text}"'


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


def _execute_schema_sql(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
) -> sqlite3.Cursor:
    """Execute SQL assembled only from validated schema identifiers."""
    # codeql[py/sql-injection]
    return conn.execute(sql, params)


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
                t_ident = self._table_identifier(conn, t)
                count = _execute_schema_sql(conn, f"SELECT COUNT(*) FROM {t_ident}").fetchone()[0]
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
                t_ident = self._table_identifier(conn, t)
                count = _execute_schema_sql(conn, f"SELECT COUNT(*) FROM {t_ident}").fetchone()[0]
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
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Query a table with pagination, column filters, and sorting."""
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= MAX_PAGE_NUMBER:
            raise ValueError(f"page must be between 1 and {MAX_PAGE_NUMBER}")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            if table not in tables:
                raise ValueError(f"Table not found: {table}")

            table_ident = self._table_identifier(conn, table)
            cols = self._get_columns(conn, table)
            col_names = [c["name"] for c in cols]

            # Validate sort column
            if sort_by not in col_names:
                sort_by = "id" if "id" in col_names else col_names[0]
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"

            # Build query
            conditions: list[str] = []
            params: list[Any] = []

            # Per-column exact-match filters
            if filters:
                for col, val in filters.items():
                    if col in col_names and val != "":
                        col_ident = _quote_identifier(col)
                        conditions.append(f"CAST({col_ident} AS TEXT) = ?")
                        params.append(val)

            # Legacy global search (fallback, searches across all text columns)
            if search:
                text_cols = [c["name"] for c in cols if c["type"] in ("TEXT", "")]
                if text_cols:
                    or_parts = [f"CAST({_quote_identifier(c)} AS TEXT) LIKE ?" for c in text_cols]
                    conditions.append("(" + " OR ".join(or_parts) + ")")
                    params.extend([f"%{search}%"] * len(text_cols))

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            # Get total count
            count_sql = f"SELECT COUNT(*) FROM {table_ident} {where_clause}"
            total = _execute_schema_sql(conn, count_sql, params).fetchone()[0]

            # Get page data
            offset = (page - 1) * page_size
            sort_ident = _quote_identifier(sort_by)
            data_sql = f"SELECT * FROM {table_ident} {where_clause} ORDER BY {sort_ident} {sort_order} LIMIT ? OFFSET ?"
            rows = _execute_schema_sql(conn, data_sql, params + [page_size, offset]).fetchall()

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

    def get_column_values(
        self,
        table: str,
        column: str,
        limit: int = 200,
    ) -> list[str]:
        """Get distinct non-null values for a column (for filter dropdowns)."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            if table not in tables:
                raise ValueError(f"Table not found: {table}")
            table_ident = self._table_identifier(conn, table)
            cols = self._get_columns(conn, table)
            col_names = [c["name"] for c in cols]
            if column not in col_names:
                raise ValueError(f"Column not found: {column}")

            column_ident = _quote_identifier(column)
            sql = f"SELECT DISTINCT CAST({column_ident} AS TEXT) AS val FROM {table_ident} WHERE {column_ident} IS NOT NULL ORDER BY val LIMIT ?"
            rows = _execute_schema_sql(conn, sql, (limit,)).fetchall()
            return [r["val"] for r in rows]
        finally:
            conn.close()

    def get_row(self, table: str, row_id: int) -> dict[str, Any]:
        """Get a single row by ID (full data, no truncation)."""
        conn = _get_conn()
        try:
            tables = self._get_table_names(conn)
            if table not in tables:
                raise ValueError(f"Table not found: {table}")

            table_ident = self._table_identifier(conn, table)
            row = _execute_schema_sql(conn, f"SELECT * FROM {table_ident} WHERE id = ?", (row_id,)).fetchone()
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
            table_ident = self._editable_table_identifier(conn, table)
            # Verify row exists
            existing = _execute_schema_sql(conn, f"SELECT id FROM {table_ident} WHERE id = ?", (row_id,)).fetchone()
            if not existing:
                raise ValueError(f"Row not found: {table}.{row_id}")

            # Build UPDATE statement
            cols = list(data.keys())
            # Never allow updating 'id'
            cols = [c for c in cols if c != "id"]
            if not cols:
                return
            valid_columns = {c["name"] for c in self._get_columns(conn, table)}
            invalid_columns = [c for c in cols if c not in valid_columns]
            if invalid_columns:
                raise ValueError(f"Column not found: {', '.join(invalid_columns)}")

            set_clause = ", ".join(f"{_quote_identifier(c)} = ?" for c in cols)
            values = [data[c] for c in cols]
            values.append(row_id)

            _execute_schema_sql(conn, f"UPDATE {table_ident} SET {set_clause} WHERE id = ?", values)
            conn.commit()
        finally:
            conn.close()

    def delete_row(self, table: str, row_id: int) -> None:
        """Delete a row (only for editable tables)."""
        if table not in EDITABLE_TABLES:
            raise PermissionError(f"Table '{table}' is not editable")

        conn = _get_rw_conn()
        try:
            table_ident = self._editable_table_identifier(conn, table)
            existing = _execute_schema_sql(conn, f"SELECT id FROM {table_ident} WHERE id = ?", (row_id,)).fetchone()
            if not existing:
                raise ValueError(f"Row not found: {table}.{row_id}")

            _execute_schema_sql(conn, f"DELETE FROM {table_ident} WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_webui_conversation(self, conversation_id: str, backend_user_id: str) -> dict[str, Any]:
        """Delete all SQLite records owned exclusively by one WebUI chat session.

        WebUI private chats use platform ``local`` and derive their Core user ID
        from the browser conversation ID. Core then derives both the private
        stream ID and person ID from that user ID. This method reproduces those
        stable IDs and removes the corresponding records in one transaction.

        A local identity that has been merged with another platform is rejected
        deliberately: deleting it as an ordinary session could otherwise erase
        or corrupt shared cross-platform memories.
        """
        conversation_id = str(conversation_id or "").strip()
        backend_user_id = str(backend_user_id or "").strip()
        if not conversation_id or not backend_user_id:
            raise ValueError("conversation_id 和 backend_user_id 不能为空")

        platform = "local"
        stream_key = f"{platform}_{backend_user_id}_private"
        # Must match legacy Core stream IDs; not used for security.
        # codeql[py/weak-sensitive-data-hashing]
        stream_id = hashlib.md5(stream_key.encode(), usedforsecurity=False).hexdigest()
        person_payload = json.dumps(
            [platform, backend_user_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        derived_person_id = hashlib.sha256(
            b"nachobot:person-id:v2\0" + person_payload
        ).hexdigest()

        conn = _get_rw_conn()
        deleted: dict[str, int] = {}

        def table_exists(table: str) -> bool:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone()
            return row is not None

        def table_columns(table: str) -> set[str]:
            if not table_exists(table):
                return set()
            return {row["name"] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()}

        def delete_any(table: str, matches: list[tuple[str, tuple[Any, ...]]]) -> int:
            columns = table_columns(table)
            clauses: list[str] = []
            params: list[Any] = []
            for column, values in matches:
                if column not in columns or not values:
                    continue
                placeholders = ", ".join("?" for _ in values)
                clauses.append(f"{_quote_identifier(column)} IN ({placeholders})")
                params.extend(values)
            if not clauses:
                return 0
            table_ident = _quote_identifier(table)
            cursor = _execute_schema_sql(
                conn,
                f"DELETE FROM {table_ident} WHERE " + " OR ".join(clauses),
                params,
            )
            count = max(0, cursor.rowcount)
            if count:
                deleted[table] = deleted.get(table, 0) + count
            return count

        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")

            # Preserve legacy person IDs when this account was registered before
            # the current SHA-256 identity derivation was introduced.
            person_id = derived_person_id
            if table_exists("person_info"):
                row = conn.execute(
                    "SELECT person_id FROM person_info "
                    "WHERE platform = ? AND user_id = ? ORDER BY id LIMIT 1",
                    (platform, backend_user_id),
                ).fetchone()
                if row and row["person_id"]:
                    person_id = str(row["person_id"])

            # Refuse destructive cleanup when this ephemeral WebUI identity has
            # been merged into a persistent cross-platform person group.
            if table_exists("person_bindings"):
                binding = conn.execute(
                    "SELECT person_id FROM person_bindings "
                    "WHERE platform = ? AND platform_user_id = ? LIMIT 1",
                    (platform, backend_user_id),
                ).fetchone()
                if binding and str(binding["person_id"]) != person_id:
                    merged = conn.execute(
                        "SELECT 1 FROM person_bindings "
                        "WHERE platform = '__merged__' AND person_id = ? LIMIT 1",
                        (str(binding["person_id"]),),
                    ).fetchone()
                    if merged:
                        raise ValueError(
                            "该 WebUI 会话身份已与其他平台账号绑定。请先解绑账号，再删除会话。"
                        )

            # Focus runtime tables are optional and may vary by schema version.
            # Delete children before their parent rows to satisfy foreign keys.
            delete_any("focus_handoff_reservation", [("target_chat_id", (stream_id,))])
            delete_any(
                "focus_handoff",
                [("target_chat_id", (stream_id,)), ("source_chat_id", (stream_id,))],
            )
            delete_any("focus_event", [("chat_id", (stream_id,))])
            delete_any("focus_chat_cursor", [("chat_id", (stream_id,))])

            delete_any(
                "messages",
                [
                    ("chat_id", (stream_id,)),
                    ("chat_info_stream_id", (stream_id,)),
                    ("chat_info_user_id", (backend_user_id,)),
                    ("user_id", (backend_user_id,)),
                ],
            )
            delete_any(
                "action_records",
                [("chat_id", (stream_id,)), ("chat_info_stream_id", (stream_id,))],
            )
            delete_any("expression", [("chat_id", (stream_id,))])
            delete_any("chat_history", [("chat_id", (stream_id,))])
            delete_any("thinking_back", [("chat_id", (stream_id,))])
            delete_any("statistics_message_hourly", [("chat_id", (stream_id,))])
            delete_any("llm_usage", [("user_id", (backend_user_id, person_id))])

            if table_exists("chat_streams"):
                cursor = conn.execute(
                    "DELETE FROM chat_streams WHERE stream_id = ? "
                    "OR (platform = ? AND user_id = ?)",
                    (stream_id, platform, backend_user_id),
                )
                if cursor.rowcount > 0:
                    deleted["chat_streams"] = cursor.rowcount

            if table_exists("bind_requests"):
                cursor = conn.execute(
                    "DELETE FROM bind_requests WHERE req_person_id = ? "
                    "OR (target_platform = ? AND target_user_id = ?)",
                    (person_id, platform, backend_user_id),
                )
                if cursor.rowcount > 0:
                    deleted["bind_requests"] = cursor.rowcount

            if table_exists("person_bindings"):
                cursor = conn.execute(
                    "DELETE FROM person_bindings WHERE platform = ? AND platform_user_id = ?",
                    (platform, backend_user_id),
                )
                if cursor.rowcount > 0:
                    deleted["person_bindings"] = cursor.rowcount

            if table_exists("person_info"):
                cursor = conn.execute(
                    "DELETE FROM person_info WHERE person_id = ? "
                    "OR (platform = ? AND user_id = ?)",
                    (person_id, platform, backend_user_id),
                )
                if cursor.rowcount > 0:
                    deleted["person_info"] = cursor.rowcount

            conn.commit()
            return {
                "status": "ok",
                "conversation_id": conversation_id,
                "backend_user_id": backend_user_id,
                "person_id": person_id,
                "stream_id": stream_id,
                "deleted": deleted,
                "deleted_rows": sum(deleted.values()),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- internal helpers ----

    def _get_table_names(self, conn: sqlite3.Connection) -> list[str]:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [
            r[0]
            for r in cursor.fetchall()
            if r[0] not in HIDDEN_TABLES and SQLITE_IDENTIFIER_RE.fullmatch(r[0])
        ]

    def _get_columns(self, conn: sqlite3.Connection, table: str) -> list[dict[str, str]]:
        if table not in self._get_table_names(conn):
            raise ValueError(f"Table not found: {table}")
        cursor = conn.execute("SELECT name, type, [notnull], pk FROM pragma_table_info(?)", (table,))
        return [
            {"name": r["name"], "type": r["type"], "notnull": bool(r["notnull"]), "pk": bool(r["pk"])}
            for r in cursor.fetchall()
            if SQLITE_IDENTIFIER_RE.fullmatch(r["name"])
        ]

    def _table_identifier(self, conn: sqlite3.Connection, table: str) -> str:
        if table not in self._get_table_names(conn):
            raise ValueError(f"Table not found: {table}")
        return _quote_identifier(table)

    def _editable_table_identifier(self, conn: sqlite3.Connection, table: str) -> str:
        if table not in EDITABLE_TABLES:
            raise PermissionError(f"Table '{table}' is not editable")
        return self._table_identifier(conn, table)
