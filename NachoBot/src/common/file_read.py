"""Small bounded readers for local files shared across media callers."""

from __future__ import annotations


class BoundedFileTooLargeError(ValueError):
    """Raised when a local file contains more than its byte budget."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        super().__init__(f"file exceeds the {max_bytes}-byte limit")


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")


def read_bounded_file(path, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes and reject overflow."""

    _validate_max_bytes(max_bytes)
    with open(path, "rb") as file:
        content = file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise BoundedFileTooLargeError(max_bytes)
    return content


def read_bounded_text_file(path, *, max_bytes: int, encoding: str = "utf-8") -> str:
    """Read bounded bytes, strictly decode, and normalize universal newlines."""

    content = read_bounded_file(path, max_bytes=max_bytes)
    text = content.decode(encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n")
