"""Windows desktop-pet state and placement helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DesktopPetState:
    x: int | None = None
    y: int | None = None
    scale: float | None = None
    offset_x: float = 0.0
    offset_y: float = 0.0
    always_on_top: bool | None = None
    click_through: bool | None = None
    visible: bool = True

    @classmethod
    def from_mapping(cls, value: Any) -> DesktopPetState:
        if not isinstance(value, dict):
            return cls()

        def optional_int(key: str) -> int | None:
            raw = value.get(key)
            return int(raw) if isinstance(raw, (int, float)) else None

        def optional_float(key: str) -> float | None:
            raw = value.get(key)
            return float(raw) if isinstance(raw, (int, float)) else None

        def optional_bool(key: str) -> bool | None:
            raw = value.get(key)
            return raw if isinstance(raw, bool) else None

        return cls(
            x=optional_int("x"),
            y=optional_int("y"),
            scale=optional_float("scale"),
            offset_x=float(value.get("offset_x", 0.0) or 0.0),
            offset_y=float(value.get("offset_y", 0.0) or 0.0),
            always_on_top=optional_bool("always_on_top"),
            click_through=optional_bool("click_through"),
            visible=bool(value.get("visible", True)),
        )


class DesktopPetStateStore:
    """Persist only small window preferences; never writes model resources."""

    def __init__(self, path: Path | None, logger: Any = None) -> None:
        self.path = path
        self.logger = logger

    def load(self) -> DesktopPetState:
        if self.path is None or not self.path.is_file():
            return DesktopPetState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return DesktopPetState.from_mapping(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            if self.logger is not None:
                self.logger.warning("Desktop pet state ignored: {}", exc)
            return DesktopPetState()

    def save(self, state: DesktopPetState) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary_path.write_text(
                json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning("Desktop pet state could not be saved: {}", exc)


def clamp_window_position(
    x: int,
    y: int,
    width: int,
    height: int,
    work_area: tuple[int, int, int, int],
    margin: int = 0,
) -> tuple[int, int]:
    """Keep enough of the pet inside the current monitor's work area."""

    left, top, right, bottom = work_area
    margin = max(0, int(margin))
    minimum_x = left - max(0, width - margin)
    maximum_x = right - margin
    minimum_y = top - max(0, height - margin)
    maximum_y = bottom - margin
    return (
        max(minimum_x, min(maximum_x, int(x))),
        max(minimum_y, min(maximum_y, int(y))),
    )


def initial_window_position(
    placement: str,
    width: int,
    height: int,
    work_area: tuple[int, int, int, int],
    margin: int,
) -> tuple[int, int]:
    left, top, right, bottom = work_area
    if placement == "bottom_left":
        return left + margin, bottom - height - margin
    if placement == "center":
        return left + (right - left - width) // 2, top + (bottom - top - height) // 2
    return right - width - margin, bottom - height - margin
