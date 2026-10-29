"""Small immutable string wrapper for explicit user-message boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptCacheScope:
    """Per-request cache accounting identity carried only as in-memory metadata."""

    session_id: str
    request_type: str

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("cache scope requires a non-empty session ID")
        if type(self.request_type) is not str:
            raise TypeError("cache scope request type must be a string")


class SegmentedPrompt(str):
    """A flattened string that also retains up to three semantic text chunks.

    The chunks are all intended to be sent as separate user messages.  The
    wrapper deliberately carries no role information and ``str(prompt)`` is
    always the exact concatenation of its chunks.
    """

    __slots__ = ("_segments",)

    def __new__(cls, segments: tuple[str, ...] | str, *additional: str) -> "SegmentedPrompt":
        if additional:
            if type(segments) is not str:
                raise TypeError("positional prompt chunks must be strings")
            normalized = (segments, *additional)
        elif isinstance(segments, tuple):
            normalized = tuple(segments)
        elif type(segments) is str:
            normalized = (segments,)
        else:
            raise TypeError("segments must be a tuple of strings or positional strings")

        if not 1 <= len(normalized) <= 3:
            raise ValueError("a segmented prompt must contain between one and three chunks")
        if any(type(segment) is not str for segment in normalized):
            raise TypeError("every prompt chunk must be a string")

        immutable_segments = tuple(normalized)
        instance = super().__new__(cls, "".join(immutable_segments))
        object.__setattr__(instance, "_segments", immutable_segments)
        return instance

    @property
    def segments(self) -> tuple[str, ...]:
        """The immutable chunks whose exact concatenation is this string."""
        return self._segments

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SegmentedPrompt is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("SegmentedPrompt is immutable")

    def __add__(self, other: object) -> "SegmentedPrompt":
        if not isinstance(other, str):
            return NotImplemented

        segments = validated_segments(self)
        if segments is None:
            return type(self)((str(self) + str(other),))
        return type(self)((*segments[:-1], segments[-1] + str(other)))

    def __radd__(self, other: object) -> "SegmentedPrompt":
        if not isinstance(other, str):
            return NotImplemented

        segments = validated_segments(self)
        if segments is None:
            return type(self)((str(other) + str(self),))
        return type(self)((str(other) + segments[0], *segments[1:]))


def validated_segments(value: object) -> tuple[str, ...] | None:
    """Return exact-class chunks only when their flattened text is consistent.

    Request construction uses this check instead of trusting a duck-typed
    ``segments`` attribute or a subclass that could override the text view.
    """
    if type(value) is not SegmentedPrompt:
        return None

    segments = value.segments
    if type(segments) is not tuple or not 1 <= len(segments) <= 3:
        return None
    if any(type(segment) is not str for segment in segments):
        return None
    if "".join(segments) != str(value):
        return None
    return segments
