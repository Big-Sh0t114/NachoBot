"""Portable non-blocking singleton lock scoped to one Discord bot token."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class SingletonProcessLock:
    def __init__(self, directory: Path, token: str):
        if not token or not token.strip():
            raise ValueError("Discord token is required before acquiring the process lock")
        fingerprint = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:24]
        self.path = directory / ".locks" / f"discord-{fingerprint}.lock"
        self._stream = None
        self._locked = False

    def acquire(self) -> None:
        if self._locked:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise RuntimeError("Another Discord adapter process already holds this bot identity") from exc
        self._stream = stream
        self._locked = True

    def release(self) -> None:
        if not self._locked or self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
            self._locked = False

    def __enter__(self) -> "SingletonProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
