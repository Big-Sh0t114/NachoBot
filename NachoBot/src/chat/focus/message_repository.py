"""Row-id based message loading for Focus turns.

Timestamp cursors can skip messages that share a timestamp or arrive while a
turn is running. Focus scans the immutable primary key and reports exactly how
far each batch was scanned, including filtered records.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import Messages
from src.config.config import global_config
from .models import StoredMessageRef


@dataclass(frozen=True, slots=True)
class FocusMessageBatch:
    messages: tuple[DatabaseMessages, ...]
    consumed_through_row_id: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class FocusMessageRangeSummary:
    first_message: StoredMessageRef
    last_message: StoredMessageRef
    unread_count: int
    has_mention: bool
    has_at: bool
    latest_preview: str


def load_message_batch(
    chat_id: str,
    after_row_id: int,
    through_row_id: int,
    *,
    limit: int = 20,
    filter_bot: bool = True,
    filter_command: bool = True,
) -> FocusMessageBatch:
    """Load the next bounded Focus batch in primary-key order.

    Filtering happens after the bounded scan so commands, notices and bot
    messages still advance the cursor instead of causing a busy loop. Returned
    messages expose focus_row_id as a diagnostic attribute.
    """

    if not chat_id:
        raise ValueError("chat_id cannot be empty")
    if after_row_id < 0 or through_row_id < 0:
        raise ValueError("Focus row cursors cannot be negative")
    if through_row_id < after_row_id:
        raise ValueError("through_row_id cannot precede after_row_id")
    if limit <= 0:
        raise ValueError("Focus message batch limit must be positive")
    if through_row_id == after_row_id:
        return FocusMessageBatch((), after_row_id, False)

    rows = list(
        Messages.select()
        .where((Messages.chat_id == chat_id) & (Messages.id > after_row_id) & (Messages.id <= through_row_id))
        .order_by(Messages.id.asc())
        .limit(limit)
    )
    if not rows:
        # Maintenance may remove rows after the coordinator snapshots a turn.
        return FocusMessageBatch((), through_row_id, False)

    messages: list[DatabaseMessages] = []
    bot_id = str(global_config.bot.qq_account)
    for row in rows:
        if row.message_id == "notice":
            continue
        if filter_bot and str(row.user_id or "") == bot_id:
            continue
        if filter_command and bool(row.is_command):
            continue
        message = DatabaseMessages(**row.__data__)
        message.focus_row_id = int(row.id)
        messages.append(message)

    consumed_through = int(rows[-1].id)
    return FocusMessageBatch(
        tuple(messages),
        consumed_through,
        consumed_through < through_row_id,
    )


def load_message_range_summary(
    chat_id: str,
    after_row_id: int,
    through_row_id: int,
) -> FocusMessageRangeSummary | None:
    """Summarize eligible stored messages for bootstrap crash recovery."""

    if not chat_id:
        raise ValueError("chat_id cannot be empty")
    if after_row_id < 0 or through_row_id < after_row_id:
        raise ValueError("Invalid Focus recovery row range")
    if through_row_id == after_row_id:
        return None

    query = (
        Messages.select()
        .where((Messages.chat_id == chat_id) & (Messages.id > after_row_id) & (Messages.id <= through_row_id))
        .order_by(Messages.id.asc())
    )
    bot_id = str(global_config.bot.qq_account)
    first: StoredMessageRef | None = None
    last: StoredMessageRef | None = None
    unread_count = 0
    has_mention = False
    has_at = False
    latest_preview = ""
    for row in query.iterator():
        if row.message_id == "notice":
            continue
        if str(row.user_id or "") == bot_id or bool(row.is_command):
            continue
        ref = StoredMessageRef(
            row_id=int(row.id),
            chat_id=chat_id,
            message_id=str(row.message_id or f"focus-row-{row.id}"),
            message_time=float(row.time or 0.0),
        )
        if first is None:
            first = ref
        last = ref
        unread_count += 1
        has_mention = has_mention or bool(row.is_mentioned)
        has_at = has_at or bool(row.is_at)
        value = row.processed_plain_text or row.display_message or ""
        latest_preview = " ".join(str(value).replace("\x00", "").split())[:200]

    if first is None or last is None:
        return None
    return FocusMessageRangeSummary(
        first_message=first,
        last_message=last,
        unread_count=unread_count,
        has_mention=has_mention,
        has_at=has_at,
        latest_preview=latest_preview,
    )


def latest_message_row_id(chat_id: str) -> int:
    """Return the latest stored message row for bootstrap baselines."""

    row = Messages.select(Messages.id).where(Messages.chat_id == chat_id).order_by(Messages.id.desc()).first()
    return int(row.id) if row is not None else 0
