"""Danmu buffer with 8-minute sliding window for Live Streamer mode."""

import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class DanmuEntry:
    """Single danmu entry with metadata."""

    message_id: str
    text: str
    user_id: str
    user_name: str
    timestamp: float
    replied: bool = False

    def __hash__(self) -> int:
        return hash(self.message_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DanmuEntry):
            return False
        return self.message_id == other.message_id


class DanmuBuffer:
    """
    8-minute sliding window buffer for danmu.

    Maintains a pool of unreplied danmu entries and provides methods to:
    - Add new danmu
    - Mark danmu as replied (removes from active pool)
    - Get unreplied danmu within the window
    - Cleanup expired entries
    """

    def __init__(self, window_seconds: int = 480):
        self._entries: Dict[str, DanmuEntry] = {}
        self._window_seconds = window_seconds

    def add(self, entry: DanmuEntry) -> None:
        """Add danmu to buffer."""
        # Don't add if already exists
        if entry.message_id in self._entries:
            return
        self._entries[entry.message_id] = entry
        # Cleanup on add to prevent unbounded growth
        self.cleanup_expired()

    def add_from_params(
        self,
        message_id: str,
        text: str,
        user_id: str,
        user_name: str,
        timestamp: float,
    ) -> DanmuEntry:
        """Convenience method to create and add entry from parameters."""
        entry = DanmuEntry(
            message_id=message_id,
            text=text,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp,
        )
        self.add(entry)
        return entry

    def mark_replied(self, message_id: str) -> None:
        """Mark danmu as replied and remove from active pool."""
        if message_id in self._entries:
            # Mark as replied and remove
            self._entries[message_id].replied = True
            del self._entries[message_id]

    def get_unreplied(self) -> List[DanmuEntry]:
        """Get all unreplied danmu within window, sorted by timestamp (oldest first)."""
        self.cleanup_expired()
        entries = [e for e in self._entries.values() if not e.replied]
        return sorted(entries, key=lambda e: e.timestamp)

    def get_unreplied_count(self) -> int:
        """Get count of unreplied danmu for elastic logic."""
        self.cleanup_expired()
        return len([e for e in self._entries.values() if not e.replied])

    def get_entry(self, message_id: str) -> Optional[DanmuEntry]:
        """Get a specific entry by message_id."""
        return self._entries.get(message_id)

    def cleanup_expired(self) -> None:
        """Remove danmu older than window."""
        cutoff = time.time() - self._window_seconds
        expired = [
            msg_id
            for msg_id, entry in self._entries.items()
            if entry.timestamp < cutoff
        ]
        for msg_id in expired:
            del self._entries[msg_id]

    def format_pool(self) -> str:
        """
        Format unreplied danmu pool for prompt injection.

        Returns formatted string like:
        - [msg_id_1] 用户A: 弹幕内容1
        - [msg_id_2] 用户B: 弹幕内容2
        """
        entries = self.get_unreplied()
        if not entries:
            return "(弹幕池为空)"

        lines = []
        for entry in entries:
            # Truncate long messages
            text = entry.text[:50] + "..." if len(entry.text) > 50 else entry.text
            lines.append(f"- [{entry.message_id}] {entry.user_name}: {text}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()
