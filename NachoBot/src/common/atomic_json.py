"""Atomic JSON file replacement for small and large Core persistence files."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | str | None = None,
) -> None:
    """Stream JSON to a sibling temporary file, then atomically replace *path*.

    Serialization, encoding, flush, fsync, close, and replace errors propagate.
    Until ``os.replace`` succeeds, the previous destination remains untouched.
    """
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing_mode = None
    if os.name == "posix":
        try:
            existing_stat = target.stat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(existing_stat.st_mode):
                existing_mode = stat.S_IMODE(existing_stat.st_mode)

    fd: int | None = None
    temp_path: str | None = None
    stream = None
    committed = False
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp_path = temp_name

        # Keep descriptor ownership here even if fdopen fails; explicit closefd=False
        # avoids ambiguous ownership transfer on construction errors.
        stream = os.fdopen(fd, "w", encoding="utf-8", closefd=False)

        try:
            json.dump(value, stream, ensure_ascii=ensure_ascii, indent=indent)
            stream.flush()
            if existing_mode is not None:
                os.fchmod(fd, existing_mode)
            os.fsync(stream.fileno())
        except BaseException:
            try:
                stream.close()
            except BaseException:
                pass
            raise

        # A close failure is a failed pre-commit write and must prevent replace.
        stream.close()
        stream = None
        descriptor = fd
        fd = None
        os.close(descriptor)
        os.replace(temp_path, target)
        committed = True
    finally:
        if stream is not None:
            try:
                stream.close()
            except BaseException:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except BaseException:
                pass
        if temp_path is not None and not committed:
            try:
                os.unlink(temp_path)
            except BaseException:
                # Cleanup is best-effort and must not hide the write failure.
                pass
