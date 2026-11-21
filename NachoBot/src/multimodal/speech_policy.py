"""Small immutable snapshot of the active local speech runtime policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SpeechPolicy:
    """Observed speech capabilities reported by the selected runtime."""

    backend: str | None
    ready: bool
    fixed_language: str | None
    language_switch_allowed: bool

    def to_dict(self) -> dict[str, object]:
        """Return the stable public API shape, with no derived extra keys."""

        return asdict(self)
