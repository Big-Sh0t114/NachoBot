"""Small durable store for scoped direct-dialogue corrections and recall evidence."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "dialogue_memory.sqlite3"
MAX_RECALL_BATCHES_PER_SCOPE = 25
MAX_CORRECTIONS_PER_SCOPE = 128
RECALL_RETENTION_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class DialogueMemoryScope:
    chat_id: str
    platform: str
    user_id: str

    def valid(self) -> bool:
        return bool(self.chat_id.strip() and self.platform.strip() and self.user_id.strip())


@dataclass(frozen=True, slots=True)
class RecallEvidence:
    evidence_ref: str
    text: str
    source: str = ""
    source_chat_id: str = ""


@dataclass(frozen=True, slots=True)
class DialogueCorrection:
    message_id: str
    occurred_at: float
    user_text: str
    status: str
    field: str
    old_span: str
    new_span: str
    evidence_refs: tuple[str, ...]
    suppressed_spans: tuple[str, ...]


class DialogueMemoryStore:
    """SQLite-backed correction overlays; the database is opened lazily."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_STORE_PATH
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 500")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            return
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dialogue_scope_state (
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                user_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, platform, user_id)
            );
            CREATE TABLE IF NOT EXISTS dialogue_recall_batches (
                batch_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                user_id TEXT NOT NULL,
                query_text TEXT NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dialogue_recall_scope
                ON dialogue_recall_batches(chat_id, platform, user_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS dialogue_recall_evidence (
                batch_id TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (batch_id, evidence_ref),
                FOREIGN KEY (batch_id) REFERENCES dialogue_recall_batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS dialogue_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                user_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                occurred_at REAL NOT NULL,
                user_text TEXT NOT NULL,
                status TEXT NOT NULL,
                field TEXT NOT NULL DEFAULT '',
                old_span TEXT NOT NULL DEFAULT '',
                new_span TEXT NOT NULL DEFAULT '',
                evidence_refs TEXT NOT NULL DEFAULT '[]',
                suppressed_spans TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE (chat_id, platform, user_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_dialogue_corrections_scope
                ON dialogue_corrections(chat_id, platform, user_id, active, occurred_at);
            """
        )
        connection.commit()
        self._initialized = True

    @staticmethod
    def _scope_values(scope: DialogueMemoryScope) -> tuple[str, str, str]:
        return scope.chat_id, scope.platform, scope.user_id

    def get_revision(self, scope: DialogueMemoryScope) -> int:
        if not scope.valid():
            return 0
        with self._lock, self._connection() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT revision FROM dialogue_scope_state WHERE chat_id=? AND platform=? AND user_id=?",
                self._scope_values(scope),
            ).fetchone()
            return int(row["revision"]) if row else 0

    def get_latest_evidence(
        self,
        scope: DialogueMemoryScope,
        *,
        max_age_seconds: float = 3600.0,
        now: Optional[float] = None,
        limit: int = 40,
    ) -> list[RecallEvidence]:
        if not scope.valid():
            return []
        now = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            self._ensure_schema(connection)
            batch = connection.execute(
                """SELECT batch_id FROM dialogue_recall_batches
                   WHERE chat_id=? AND platform=? AND user_id=? AND observed_at>=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (*self._scope_values(scope), now - max(0.0, max_age_seconds)),
            ).fetchone()
            if not batch:
                return []
            rows = connection.execute(
                """SELECT evidence_ref, text, source FROM dialogue_recall_evidence
                   WHERE batch_id=? ORDER BY rowid LIMIT ?""",
                (batch["batch_id"], max(1, min(100, limit))),
            ).fetchall()
            return [RecallEvidence(row["evidence_ref"], row["text"], row["source"]) for row in rows]

    def record_recall(
        self,
        scope: DialogueMemoryScope,
        *,
        query: str,
        evidence: Iterable[RecallEvidence],
        expected_revision: int,
        occurred_at: Optional[float] = None,
    ) -> bool:
        if not scope.valid():
            return False
        occurred_at = time.time() if occurred_at is None else float(occurred_at)
        batch_id = uuid.uuid4().hex
        clean_evidence = list(evidence)[:20]
        with self._lock, self._connection() as connection:
            self._ensure_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO dialogue_scope_state(chat_id, platform, user_id, revision) VALUES(?,?,?,0)",
                self._scope_values(scope),
            )
            current = connection.execute(
                "SELECT revision FROM dialogue_scope_state WHERE chat_id=? AND platform=? AND user_id=?",
                self._scope_values(scope),
            ).fetchone()
            if int(current["revision"]) != int(expected_revision):
                return False
            connection.execute(
                """INSERT INTO dialogue_recall_batches
                   (batch_id, chat_id, platform, user_id, query_text, observed_at)
                   VALUES(?,?,?,?,?,?)""",
                (batch_id, *self._scope_values(scope), query[:2400], occurred_at),
            )
            connection.executemany(
                "INSERT INTO dialogue_recall_evidence(batch_id, evidence_ref, text, source) VALUES(?,?,?,?)",
                [(batch_id, item.evidence_ref[:300], item.text[:1200], item.source[:300]) for item in clean_evidence],
            )
            connection.execute(
                """DELETE FROM dialogue_recall_batches
                   WHERE chat_id=? AND platform=? AND user_id=? AND batch_id NOT IN (
                     SELECT batch_id FROM dialogue_recall_batches
                     WHERE chat_id=? AND platform=? AND user_id=?
                     ORDER BY observed_at DESC LIMIT ?
                   )""",
                (*self._scope_values(scope), *self._scope_values(scope), MAX_RECALL_BATCHES_PER_SCOPE),
            )
            connection.execute(
                "DELETE FROM dialogue_recall_batches WHERE observed_at < ?",
                (occurred_at - RECALL_RETENTION_SECONDS,),
            )
            connection.commit()
            return True

    def record_correction(
        self,
        scope: DialogueMemoryScope,
        *,
        correction: DialogueCorrection,
    ) -> bool:
        if not scope.valid() or not correction.message_id.strip():
            return False
        evidence_refs = tuple(dict.fromkeys(ref for ref in correction.evidence_refs if ref))[:40]
        suppressed_spans = tuple(dict.fromkeys(span for span in correction.suppressed_spans if span))[:20]
        with self._lock, self._connection() as connection:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                """SELECT 1 FROM dialogue_corrections
                   WHERE chat_id=? AND platform=? AND user_id=? AND message_id=?""",
                (*self._scope_values(scope), correction.message_id),
            ).fetchone()
            if duplicate:
                connection.rollback()
                return False

            active = 1
            if correction.status == "exact":
                current_refs = set(evidence_refs)
                current_old = _normalize_span(correction.old_span)
                related_rows = connection.execute(
                    """SELECT id, occurred_at, field, old_span, new_span, evidence_refs, suppressed_spans
                       FROM dialogue_corrections WHERE chat_id=? AND platform=? AND user_id=?
                       AND active=1 AND status='exact' ORDER BY occurred_at, id""",
                    self._scope_values(scope),
                ).fetchall()
                related = []
                for row in related_rows:
                    previous_refs = set(_decode_json_list(row["evidence_refs"]))
                    previous_old = _normalize_span(row["old_span"])
                    previous_new = _normalize_span(row["new_span"])
                    same_field = bool(correction.field and row["field"] == correction.field)
                    value_chain = bool(same_field and current_old and previous_new and current_old == previous_new)
                    same_fact = bool(same_field and current_old and previous_old == current_old)
                    # A shared source paragraph can contain several independent
                    # claims of the same broad field (for example, preferences
                    # for coffee and cats). Evidence references establish
                    # provenance, not fact identity; only a matching old value
                    # or an exact new-to-old chain supersedes an earlier fact.
                    if value_chain or same_fact:
                        related.append(row)
                        suppressed_spans = tuple(
                            dict.fromkeys(
                                (*suppressed_spans, row["old_span"], row["new_span"], *_decode_json_list(row["suppressed_spans"]))
                            )
                        )[:20]
                        evidence_refs = tuple(dict.fromkeys((*evidence_refs, *previous_refs)))[:40]

                related_disputes = connection.execute(
                    """SELECT id, occurred_at, evidence_refs FROM dialogue_corrections
                       WHERE chat_id=? AND platform=? AND user_id=? AND active=1 AND status='disputed'
                       ORDER BY occurred_at, id""",
                    self._scope_values(scope),
                ).fetchall()
                linked_disputes = [
                    row for row in related_disputes
                    if current_refs.intersection(_decode_json_list(row["evidence_refs"]))
                ]
                newer_related = [*related, *linked_disputes]
                if any(float(row["occurred_at"]) > correction.occurred_at for row in newer_related):
                    # Arrival order cannot let an older source message replace a
                    # correction that is newer in the conversation timeline.
                    active = 0
                else:
                    for row in related:
                        connection.execute("UPDATE dialogue_corrections SET active=0 WHERE id=?", (row["id"],))
                    for row in linked_disputes:
                        connection.execute("UPDATE dialogue_corrections SET active=0 WHERE id=?", (row["id"],))
            elif correction.status == "disputed":
                current_refs = set(evidence_refs)
                linked = connection.execute(
                    """SELECT id, occurred_at, evidence_refs, status FROM dialogue_corrections
                       WHERE chat_id=? AND platform=? AND user_id=? AND active=1
                       ORDER BY occurred_at, id""",
                    self._scope_values(scope),
                ).fetchall()
                linked = [
                    row for row in linked
                    if current_refs.intersection(_decode_json_list(row["evidence_refs"]))
                ]
                if any(float(row["occurred_at"]) > correction.occurred_at for row in linked):
                    active = 0
                else:
                    for row in linked:
                        if row["status"] == "disputed":
                            connection.execute("UPDATE dialogue_corrections SET active=0 WHERE id=?", (row["id"],))

            connection.execute(
                "INSERT OR IGNORE INTO dialogue_scope_state(chat_id, platform, user_id, revision) VALUES(?,?,?,0)",
                self._scope_values(scope),
            )
            if active:
                connection.execute(
                    "UPDATE dialogue_scope_state SET revision=revision+1 WHERE chat_id=? AND platform=? AND user_id=?",
                    self._scope_values(scope),
                )
            connection.execute(
                """INSERT INTO dialogue_corrections
                   (chat_id, platform, user_id, message_id, occurred_at, user_text, status, field,
                    old_span, new_span, evidence_refs, suppressed_spans, active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    *self._scope_values(scope), correction.message_id, float(correction.occurred_at),
                    correction.user_text[:1200], correction.status, correction.field[:80],
                    correction.old_span[:160], correction.new_span[:160],
                    json.dumps(evidence_refs, ensure_ascii=False),
                    json.dumps(suppressed_spans, ensure_ascii=False), active,
                ),
            )
            connection.execute(
                """DELETE FROM dialogue_corrections WHERE chat_id=? AND platform=? AND user_id=?
                   AND id NOT IN (SELECT id FROM dialogue_corrections WHERE chat_id=? AND platform=? AND user_id=?
                     ORDER BY occurred_at DESC, id DESC LIMIT ?)""",
                (*self._scope_values(scope), *self._scope_values(scope), MAX_CORRECTIONS_PER_SCOPE),
            )
            connection.commit()
            return True

    def get_corrections(self, scope: DialogueMemoryScope) -> list[DialogueCorrection]:
        if not scope.valid():
            return []
        with self._lock, self._connection() as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                """SELECT message_id, occurred_at, user_text, status, field, old_span, new_span,
                          evidence_refs, suppressed_spans
                   FROM dialogue_corrections WHERE chat_id=? AND platform=? AND user_id=? AND active=1
                   ORDER BY occurred_at, id""",
                self._scope_values(scope),
            ).fetchall()
            return [
                DialogueCorrection(
                    message_id=row["message_id"], occurred_at=float(row["occurred_at"]),
                    user_text=row["user_text"], status=row["status"], field=row["field"],
                    old_span=row["old_span"], new_span=row["new_span"],
                    evidence_refs=tuple(_decode_json_list(row["evidence_refs"])),
                    suppressed_spans=tuple(_decode_json_list(row["suppressed_spans"])),
                )
                for row in rows
            ]


def _decode_json_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [item for item in decoded if isinstance(item, str)] if isinstance(decoded, list) else []


def _normalize_span(value: str) -> str:
    return " ".join((value or "").casefold().split()).strip(" ，,。；;.!?！？\"'“”‘’")


_default_store: Optional[DialogueMemoryStore] = None
_default_store_lock = threading.Lock()


def get_dialogue_memory_store() -> DialogueMemoryStore:
    """Return the process-wide store without opening its database until first use."""
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = DialogueMemoryStore()
    return _default_store
