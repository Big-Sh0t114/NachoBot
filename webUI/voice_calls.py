"""Durable WebUI voice-call transcripts and per-request delivery routing."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


WEBUI_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = WEBUI_DIR / "data" / "voice_calls.sqlite3"
MAX_CONVERSATION_ID = 96
MAX_CALL_MESSAGES = 500
MAX_MESSAGE_CHARS = 10_000
MAX_REQUEST_ROUTES = 20_000
MAX_TOTAL_CALLS = 5_000
CALL_LEASE_SECONDS = 30.0
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")


class VoiceCallError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def validate_conversation_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > MAX_CONVERSATION_ID or not _ID_RE.fullmatch(candidate):
        raise VoiceCallError("conversation_id 无效", 400)
    return candidate


def canonical_core_user_id(conversation_id: str) -> str:
    """Injectively map accepted IDs to Core identities without lossy cleanup."""
    conversation_id = validate_conversation_id(conversation_id)
    return f"webui_{conversation_id}"


class VoiceCallStore:
    """SQLite-backed call state; audio recordings are never persisted."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._interruptions: dict[str, dict[str, Any]] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','ended')),
                    generation INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    last_activity REAL NOT NULL,
                    model_id TEXT,
                    live2d_model_id TEXT,
                    UNIQUE(id, conversation_id)
                );
                CREATE INDEX IF NOT EXISTS calls_conversation_idx
                    ON calls(conversation_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    request_message_id TEXT,
                    generation INTEGER NOT NULL,
                    interrupted INTEGER NOT NULL DEFAULT 0,
                    delivery_status TEXT,
                    core_message_id TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS messages_call_idx
                    ON messages(call_id, created_at, id);
                CREATE TABLE IF NOT EXISTS request_routes (
                    request_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    core_user_id TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('text','voice')),
                    call_id TEXT,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    send_state TEXT NOT NULL DEFAULT 'registered'
                        CHECK(send_state IN ('registered','sending','accepted','failed')),
                    FOREIGN KEY(call_id, conversation_id)
                        REFERENCES calls(id, conversation_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS request_routes_conversation_idx
                    ON request_routes(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS used_controls (
                    call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                    control_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(call_id, control_id)
                );
                CREATE TABLE IF NOT EXISTS seen_core_messages (
                    message_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(request_routes)")}
            if "send_state" not in columns:
                db.execute("ALTER TABLE request_routes ADD COLUMN send_state TEXT NOT NULL DEFAULT 'registered'")

    def recover_after_restart(self) -> int:
        self._interruptions.clear()
        now = time.time()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE calls SET status='ended', ended_at=?, last_activity=? WHERE status='active'",
                (now, now),
            )
            db.execute("UPDATE messages SET interrupted=1,delivery_status='cancelled' "
                       "WHERE role='assistant' AND delivery_status='pending' "
                       "AND call_id IN (SELECT id FROM calls WHERE status='ended')")
            return max(0, int(cursor.rowcount))

    def expire_stale_calls(self) -> int:
        now = time.time()
        with self._lock, self._connect() as db:
            return self._expire_stale(db, now)

    def _expire_stale(self, db: sqlite3.Connection, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cursor = db.execute(
            "UPDATE calls SET status='ended', ended_at=?, last_activity=? "
            "WHERE status='active' AND last_activity < ?",
            (now, now, now - CALL_LEASE_SECONDS),
        )
        if cursor.rowcount:
            db.execute("UPDATE messages SET interrupted=1,delivery_status='cancelled' "
                       "WHERE role='assistant' AND delivery_status='pending' "
                       "AND call_id IN (SELECT id FROM calls WHERE status='ended')")
        for call_id in list(self._interruptions):
            active = db.execute("SELECT 1 FROM calls WHERE id=? AND status='active'", (call_id,)).fetchone()
            if not active:
                self._interruptions.pop(call_id, None)
        return max(0, int(cursor.rowcount))

    @staticmethod
    def _decode_call(db: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        messages = db.execute(
            "SELECT id, role, content, created_at, request_message_id, generation, interrupted, delivery_status "
            "FROM messages WHERE call_id=? ORDER BY created_at, rowid",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "status": row["status"],
            "generation": int(row["generation"]),
            "started_at": float(row["started_at"]),
            "ended_at": float(row["ended_at"]) if row["ended_at"] is not None else None,
            "model_id": row["model_id"],
            "messages": [
                {
                    "id": item["id"],
                    "role": item["role"],
                    "content": item["content"],
                    "created_at": float(item["created_at"]),
                    **({"request_message_id": item["request_message_id"]} if item["request_message_id"] else {}),
                    "generation": int(item["generation"]),
                    "interrupted": bool(item["interrupted"]),
                    **({"delivery_status": item["delivery_status"]} if item["delivery_status"] else {}),
                }
                for item in messages
            ],
        }

    def create_call(self, conversation_id: str, owner: str, model_id: str | None = None) -> dict[str, Any]:
        conversation_id = validate_conversation_id(conversation_id)
        owner = str(owner or "WebUI").strip()[:128] or "WebUI"
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire_stale(db, now)
            active = db.execute(
                "SELECT * FROM calls WHERE conversation_id=? AND status='active' ORDER BY started_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise VoiceCallError("此会话已有活动语音通话", 409)
            total_calls = int(db.execute("SELECT count(*) FROM calls").fetchone()[0])
            if total_calls >= MAX_TOTAL_CALLS:
                raise VoiceCallError("语音通话记录已达到存储上限，请先删除旧会话", 507)
            call_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO calls(id,conversation_id,owner,status,generation,started_at,last_activity,model_id,live2d_model_id) "
                "VALUES(?,?,?,'active',0,?,?,?,?)",
                (call_id, conversation_id, owner, now, now, model_id, model_id),
            )
            return self._decode_call(db, db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()) or {}

    def get_call(self, call_id: str, *, touch: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connect() as db:
            self._expire_stale(db, now)
            if touch:
                db.execute(
                    "UPDATE calls SET last_activity=? WHERE id=? AND status='active'",
                    (now, call_id),
                )
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            return self._decode_call(db, row)

    def list_calls(self, conversation_id: str) -> list[dict[str, Any]]:
        conversation_id = validate_conversation_id(conversation_id)
        with self._lock, self._connect() as db:
            self._expire_stale(db)
            rows = db.execute(
                "SELECT * FROM calls WHERE conversation_id=? ORDER BY started_at DESC LIMIT 100",
                (conversation_id,),
            ).fetchall()
            return [self._decode_call(db, row) or {} for row in rows]

    def _require_active(self, db: sqlite3.Connection, call_id: str, generation: int | None = None) -> sqlite3.Row:
        self._expire_stale(db)
        row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise VoiceCallError("语音通话不存在", 404)
        if row["status"] != "active":
            raise VoiceCallError("语音通话已结束", 409)
        if generation is not None and int(row["generation"]) != int(generation):
            raise VoiceCallError("语音请求已过期", 409)
        return row

    def heartbeat(self, call_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = self._require_active(db, call_id)
            now = time.time()
            db.execute("UPDATE calls SET last_activity=? WHERE id=?", (now, call_id))
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            return self._decode_call(db, row) or {}

    def interrupt(self, call_id: str, generation: int) -> int:
        with self._lock, self._connect() as db:
            row = self._require_active(db, call_id)
            current = int(row["generation"])
            # Optimistic idempotence: retries with an older generation observe the winner.
            if int(generation) < current:
                return current
            if int(generation) > current:
                raise VoiceCallError("语音请求已过期", 409)
            next_generation = current + 1
            now = time.time()
            # Count only a server-observed pending reply. Ordinary listening
            # turns still advance the generation but are not interruptions.
            pending = db.execute(
                "SELECT 1 FROM messages WHERE call_id=? AND generation=? AND role='assistant' "
                "AND delivery_status IN ('pending','interrupted') LIMIT 1", (call_id, current),
            ).fetchone()
            waiting = db.execute(
                "SELECT 1 FROM request_routes r WHERE r.call_id=? AND r.generation=? "
                "AND r.send_state IN ('sending','accepted') AND NOT EXISTS "
                "(SELECT 1 FROM messages m WHERE m.request_message_id=r.request_message_id "
                "AND m.role='assistant') LIMIT 1", (call_id, current),
            ).fetchone()
            if pending or waiting:
                state = self._interruptions.setdefault(call_id, {"times": [], "last_feedback": 0, "pending": None})
                state["times"] = [t for t in state["times"] if now - t < 60][-7:] + [now]
                if len(state["times"]) >= 3 and now - state["last_feedback"] >= 60 and not state["pending"]:
                    state["pending"] = {"token": uuid.uuid4().hex, "count": len(state["times"]), "created_at": now}
            db.execute(
                "UPDATE calls SET generation=?,last_activity=? WHERE id=? AND generation=? AND status='active'",
                (next_generation, now, call_id, current),
            )
            db.execute(
                "UPDATE messages SET interrupted=1,delivery_status='cancelled' "
                "WHERE call_id=? AND generation=? AND role='assistant' AND (delivery_status IS NULL OR delivery_status='pending')",
                (call_id, current),
            )
            return next_generation

    def peek_interrupt_feedback(self, call_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            self._require_active(db, call_id)
            state = self._interruptions.get(call_id)
            pending = state and state.get("pending")
            if pending and time.time() - pending["created_at"] < 60:
                return dict(pending)
            if state:
                state["pending"] = None
            return None

    def consume_interrupt_feedback(self, call_id: str, token: str) -> None:
        with self._lock:
            state = self._interruptions.get(call_id)
            if state and (state.get("pending") or {}).get("token") == token:
                state["pending"] = None
                state["last_feedback"] = time.time()

    def end_call(self, call_id: str) -> dict[str, Any]:
        self._interruptions.pop(call_id, None)
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise VoiceCallError("语音通话不存在", 404)
            if row["status"] == "active":
                db.execute("UPDATE calls SET status='ended',ended_at=?,last_activity=? WHERE id=?", (now, now, call_id))
                db.execute("UPDATE messages SET interrupted=1,delivery_status='cancelled' "
                           "WHERE call_id=? AND role='assistant' AND delivery_status='pending'", (call_id,))
            return self._decode_call(db, db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()) or {}

    def register_request(
        self,
        request_message_id: str,
        conversation_id: str,
        core_user_id: str,
        channel: str,
        call_id: str | None = None,
        generation: int = 0,
    ) -> dict[str, Any]:
        request_message_id = str(request_message_id or "").strip()
        if not request_message_id or len(request_message_id) > 128:
            raise VoiceCallError("request_message_id 无效", 400)
        conversation_id = validate_conversation_id(conversation_id)
        if channel not in {"text", "voice"}:
            raise VoiceCallError("消息通道无效", 400)
        if channel == "voice" and not call_id:
            raise VoiceCallError("语音通话标识缺失", 400)
        if core_user_id != canonical_core_user_id(conversation_id):
            raise VoiceCallError("Core 会话身份不匹配", 409)
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM request_routes WHERE request_message_id=?", (request_message_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["conversation_id"] == conversation_id
                    and existing["core_user_id"] == core_user_id
                    and existing["channel"] == channel
                    and existing["call_id"] == call_id
                    and int(existing["generation"]) == int(generation)
                )
                if not same:
                    raise VoiceCallError("request_message_id 已被其他请求使用", 409)
                return dict(existing)
            if channel == "voice":
                call = self._require_active(db, str(call_id), int(generation))
                if call["conversation_id"] != conversation_id:
                    raise VoiceCallError("语音通话与会话不匹配", 409)
            count = int(db.execute("SELECT count(*) FROM request_routes").fetchone()[0])
            if count >= MAX_REQUEST_ROUTES:
                db.execute("DELETE FROM request_routes WHERE created_at < ? AND "
                           "(call_id IS NULL OR call_id IN (SELECT id FROM calls WHERE status='ended'))",
                           (now - 86400 * 7,))
                if int(db.execute("SELECT count(*) FROM request_routes").fetchone()[0]) >= MAX_REQUEST_ROUTES:
                    raise VoiceCallError("请求过多，请稍后再试", 429)
            db.execute(
                "INSERT INTO request_routes(request_message_id,conversation_id,core_user_id,channel,call_id,generation,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (request_message_id, conversation_id, core_user_id, channel, call_id, int(generation), now),
            )
            if call_id:
                db.execute("UPDATE calls SET last_activity=? WHERE id=?", (now, call_id))
            return {
                "request_message_id": request_message_id,
                "conversation_id": conversation_id,
                "core_user_id": core_user_id,
                "channel": channel,
                "call_id": call_id,
                "generation": int(generation),
                "created_at": now,
                "send_state": "registered",
            }

    def begin_request(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        route = self.register_request(*args, **kwargs)
        request_id = route["request_message_id"]
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE request_routes SET send_state='sending' WHERE request_message_id=? AND send_state='registered'",
                (request_id,),
            )
            row = db.execute("SELECT * FROM request_routes WHERE request_message_id=?", (request_id,)).fetchone()
            result = dict(row) if row is not None else route
            return result, cursor.rowcount > 0

    def finish_request(self, request_message_id: str, send_state: str) -> None:
        if send_state not in {"accepted", "failed"}:
            raise ValueError("invalid request send state")
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE request_routes SET send_state=? WHERE request_message_id=? AND send_state='sending'",
                (send_state, str(request_message_id)),
            )

    def resolve_request(self, request_message_id: str, core_user_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            self._expire_stale(db)
            row = db.execute(
                "SELECT * FROM request_routes WHERE request_message_id=? AND core_user_id=?",
                (str(request_message_id or ""), str(core_user_id or "")),
            ).fetchone()
            return dict(row) if row is not None else None

    def conversation_for_core_user(self, core_user_id: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT conversation_id FROM request_routes WHERE core_user_id=? ORDER BY created_at DESC LIMIT 1",
                (str(core_user_id or ""),),
            ).fetchone()
            return str(row["conversation_id"]) if row is not None else None

    def claim_core_message(self, message_id: str) -> bool:
        message_id = str(message_id or "").strip()
        if not message_id:
            return True
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM seen_core_messages WHERE created_at < ?", (time.time() - 86400 * 7,))
            db.execute("DELETE FROM seen_core_messages WHERE message_id IN "
                       "(SELECT message_id FROM seen_core_messages ORDER BY created_at DESC LIMIT -1 OFFSET 49999)")
            cursor = db.execute(
                "INSERT OR IGNORE INTO seen_core_messages(message_id,created_at) VALUES(?,?)",
                (message_id, time.time()),
            )
            return cursor.rowcount > 0

    def add_message(
        self,
        call_id: str,
        *,
        role: str,
        content: str,
        generation: int,
        request_message_id: str | None = None,
        core_message_id: str | None = None,
        interrupted: bool = False,
        delivery_status: str | None = None,
        require_active: bool = True,
    ) -> dict[str, Any]:
        content = str(content or "")
        if role not in {"user", "assistant"} or len(content) > MAX_MESSAGE_CHARS:
            raise VoiceCallError("消息内容无效或超过限制", 400)
        now = time.time()
        with self._lock, self._connect() as db:
            self._expire_stale(db, now)
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise VoiceCallError("语音通话不存在", 404)
            if require_active:
                row = self._require_active(db, call_id, generation)
            if core_message_id:
                existing = db.execute(
                    "SELECT id,role,content,created_at,request_message_id,generation,interrupted,delivery_status "
                    "FROM messages WHERE core_message_id=?",
                    (core_message_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content"] != content or int(existing["generation"]) != int(generation):
                        raise VoiceCallError("消息请求标识已被使用", 409)
                    return {**dict(existing), "duplicate": True}
            if role == "user" and request_message_id:
                existing = db.execute(
                    "SELECT id,role,content,created_at,request_message_id,generation,interrupted,delivery_status "
                    "FROM messages WHERE call_id=? AND request_message_id=? AND role='user'",
                    (call_id, request_message_id),
                ).fetchone()
                if existing is not None:
                    if existing["content"] != content or int(existing["generation"]) != int(generation):
                        raise VoiceCallError("消息请求标识已被使用", 409)
                    return {**dict(existing), "duplicate": True}
            count = int(db.execute("SELECT count(*) FROM messages WHERE call_id=?", (call_id,)).fetchone()[0])
            if count >= MAX_CALL_MESSAGES:
                raise VoiceCallError("通话记录已达到单通话存储上限", 507)
            message_id = uuid.uuid4().hex
            stale = int(row["generation"]) != int(generation) or row["status"] != "active"
            effective_interrupted = bool(interrupted or stale)
            effective_status = delivery_status or ("cancelled" if effective_interrupted else None)
            db.execute(
                "INSERT INTO messages(id,call_id,role,content,created_at,request_message_id,generation,interrupted,delivery_status,core_message_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id, call_id, role, content, now, request_message_id, int(generation),
                    int(effective_interrupted), effective_status or ("pending" if role == "assistant" and not effective_interrupted else None), core_message_id,
                ),
            )
            if role == "user" and require_active:
                db.execute("UPDATE calls SET last_activity=? WHERE id=? AND status='active'", (now, call_id))
            return {
                "id": message_id,
                "role": role,
                "content": content,
                "created_at": now,
                **({"request_message_id": request_message_id} if request_message_id else {}),
                "generation": int(generation),
                "interrupted": effective_interrupted,
                "duplicate": False,
                **({"delivery_status": effective_status or "pending"} if role == "assistant" and not effective_interrupted else ({"delivery_status": effective_status} if effective_status else {})),
            }

    def message_for_tts(self, call_id: str, message_id: str, generation: int) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            call = self._require_active(db, call_id, generation)
            row = db.execute(
                "SELECT * FROM messages WHERE id=? AND call_id=? AND role='assistant'",
                (message_id, call_id),
            ).fetchone()
            if row is None or int(row["generation"]) != int(generation) or row["interrupted"]:
                raise VoiceCallError("语音回复已过期", 409)
            return {"message": dict(row), "live2d_model_id": call["live2d_model_id"]}

    def claim_control(self, call_id: str, control_id: str, generation: int) -> bool:
        if not control_id or len(control_id) > 128:
            raise VoiceCallError("控制标识无效", 400)
        with self._lock, self._connect() as db:
            self._require_active(db, call_id, generation)
            cursor = db.execute(
                "INSERT OR IGNORE INTO used_controls(call_id,control_id,generation,created_at) VALUES(?,?,?,?)",
                (call_id, control_id, int(generation), time.time()),
            )
            return cursor.rowcount > 0

    def settle_playback(self, call_id: str, message_id: str, generation: int, status: str) -> dict[str, Any]:
        if status not in {"played", "interrupted"}:
            raise VoiceCallError("播放状态无效", 400)
        with self._lock, self._connect() as db:
            self._expire_stale(db)
            call = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                raise VoiceCallError("语音通话不存在", 404)
            row = db.execute(
                "SELECT * FROM messages WHERE id=? AND call_id=? AND role='assistant' AND generation=?",
                (message_id, call_id, int(generation)),
            ).fetchone()
            if row is None:
                raise VoiceCallError("语音回复不存在或已过期", 409)
            stale = call["status"] != "active" or int(call["generation"]) != int(generation)
            final_status = row["delivery_status"] if row["delivery_status"] != "pending" else ("interrupted" if stale else status)
            db.execute(
                "UPDATE messages SET delivery_status=?,interrupted=CASE WHEN ?='interrupted' THEN 1 ELSE interrupted END WHERE id=?",
                (final_status, final_status, message_id),
            )
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return dict(row)

    def set_message_status(self, message_id: str, call_id: str, status: str) -> None:
        if status not in {"accepted", "failed"}:
            raise VoiceCallError("消息状态无效", 400)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE messages SET delivery_status=? WHERE id=? AND call_id=? AND role='user' "
                "AND delivery_status IN ('pending','accepted','failed')",
                (status, message_id, call_id),
            )

    def delete_conversation(self, conversation_id: str) -> int:
        conversation_id = validate_conversation_id(conversation_id)
        with self._lock, self._connect() as db:
            call_count = int(db.execute("SELECT count(*) FROM calls WHERE conversation_id=?", (conversation_id,)).fetchone()[0])
            db.execute("DELETE FROM request_routes WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM calls WHERE conversation_id=?", (conversation_id,))
            return call_count

    def call_ids_for_conversation(self, conversation_id: str) -> list[str]:
        conversation_id = validate_conversation_id(conversation_id)
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id FROM calls WHERE conversation_id=?", (conversation_id,)).fetchall()
            return [str(row["id"]) for row in rows]


voice_call_store = VoiceCallStore()
