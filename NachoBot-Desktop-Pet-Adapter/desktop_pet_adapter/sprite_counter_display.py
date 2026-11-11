"""Small pixel counter overlay for Sprite robot face rectangles."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "B": ("110", "101", "110", "101", "110"),
    ".": ("000", "000", "000", "000", "010"),
    "+": ("000", "010", "111", "010", "000"),
    ":": ("000", "010", "000", "010", "000"),
    "A": ("010", "101", "111", "101", "101"),
    "C": ("111", "100", "100", "100", "111"),
    "E": ("111", "100", "110", "100", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "K": ("101", "101", "110", "101", "101"),
    "M": ("101", "111", "111", "101", "101"),
    "R": ("110", "101", "110", "101", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "Y": ("101", "101", "010", "010", "010"),
    "L": ("100", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
}


@dataclass(slots=True)
class CounterFeedbackState:
    """Bounded pulse and burst state derived from aggregate count deltas."""

    last_total: int = 0
    burst_count: int = 0
    burst_until: float = 0.0
    pulse_until: float = 0.0

    def prime(self, total: int) -> None:
        """Set a starting aggregate without treating persisted counts as new input."""

        self.last_total = max(0, int(total))
        self.burst_count = 0
        self.burst_until = 0.0
        self.pulse_until = 0.0

    def observe(self, total: int, *, now: float | None = None) -> int:
        current = max(0, int(total))
        current_time = time.monotonic() if now is None else float(now)
        delta = max(0, current - self.last_total)
        self.last_total = current
        if delta:
            if current_time >= self.burst_until:
                self.burst_count = 0
            self.burst_count += delta
            self.burst_until = current_time + 0.4
            self.pulse_until = current_time + 0.15
        return delta

    def burst_active(self, *, now: float | None = None) -> bool:
        current_time = time.monotonic() if now is None else float(now)
        return self.burst_count > 0 and current_time < self.burst_until

    def pulse_active(self, *, now: float | None = None) -> bool:
        current_time = time.monotonic() if now is None else float(now)
        return current_time < self.pulse_until


def _compact_total(value: int, character_budget: int) -> str:
    """Choose a rounded, unit-marked value that fits without dropping digits."""

    for unit, suffix in ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B")):
        rounded_tenths = (value * 10 + unit // 2) // unit
        if rounded_tenths >= 10_000:
            continue
        if rounded_tenths % 10:
            whole, fraction = divmod(rounded_tenths, 10)
            decimal_candidate = f"{whole}.{fraction}{suffix}"
            if len(decimal_candidate) <= character_budget:
                return decimal_candidate
        rounded = (value + unit // 2) // unit
        if rounded <= 0:
            continue
        candidate = f"{rounded}{suffix}"
        if len(candidate) <= character_budget:
            return candidate
    return "+"


def format_total(total: int, *, character_budget: int = 5) -> str:
    """Return a compact display string while leaving persisted counts exact."""

    value = max(0, int(total))
    budget = max(1, int(character_budget))
    digits = str(value)
    if value < 10_000 and len(digits) <= budget:
        return digits
    return _compact_total(value, budget)


def _draw_text(
    surface: Any,
    text: str,
    x: int,
    y: int,
    color: tuple[int, int, int, int],
    *,
    scale: int = 1,
) -> None:
    fill = surface.fill
    for character in text.upper():
        glyph = _GLYPHS.get(character)
        if glyph is not None:
            for row_index, row in enumerate(glyph):
                for column_index, pixel in enumerate(row):
                    if pixel == "1":
                        fill(
                            color,
                            (
                                x + column_index * scale,
                                y + row_index * scale,
                                scale,
                                scale,
                            ),
                        )
        x += 4 * scale


def _detail_lines(
    *,
    total: int,
    keyboard_presses: int,
    mouse_clicks: int,
    character_capacity: int,
) -> tuple[str, ...]:
    lines: list[str] = []
    for label, count in (
        ("TOTAL", total),
        ("KEYBOARD", keyboard_presses),
        ("MOUSE", mouse_clicks),
    ):
        prefix = f"{label} "
        digits = str(max(0, int(count)))
        first_capacity = max(1, character_capacity - len(prefix))
        lines.append(prefix + digits[:first_capacity])
        remaining = digits[first_capacity:]
        while remaining:
            lines.append(remaining[:character_capacity])
            remaining = remaining[character_capacity:]
    return tuple(lines)


def _draw_detail_panel(
    surface: Any,
    face_rect: tuple[int, int, int, int],
    *,
    total: int,
    keyboard_presses: int,
    mouse_clicks: int,
) -> None:
    surface_width, surface_height = surface.get_size()
    text_scale = 2 if surface_width >= 160 and surface_height >= 120 else 1
    maximum_width = surface_width - 8
    character_capacity = max(
        1,
        (maximum_width - 8 + text_scale) // (4 * text_scale),
    )
    lines = _detail_lines(
        total=total,
        keyboard_presses=keyboard_presses,
        mouse_clicks=mouse_clicks,
        character_capacity=character_capacity,
    )
    content_width = max((4 * len(line) - 1) * text_scale for line in lines)
    panel_width = min(maximum_width, content_width + 8)
    line_pitch = 7 * text_scale
    panel_height = len(lines) * 5 * text_scale + max(0, len(lines) - 1) * 2 * text_scale + 8
    left, top, right, bottom = face_rect
    panel_x = max(4, min(left, surface_width - panel_width - 4))
    panel_y = bottom + 3
    if panel_y + panel_height > surface_height - 4:
        panel_y = top - panel_height - 3
    panel_y = max(4, min(panel_y, surface_height - panel_height - 4))

    fill = surface.fill
    fill((6, 15, 28, 244), (panel_x, panel_y, panel_width, panel_height))
    for index, line in enumerate(lines):
        _draw_text(
            surface,
            line,
            panel_x + 4,
            panel_y + 4 + index * line_pitch,
            (205, 229, 244, 255),
            scale=text_scale,
        )


def _draw_burst(
    surface: Any,
    face_rect: tuple[int, int, int, int],
    burst_count: int,
) -> None:
    surface_width, surface_height = surface.get_size()
    burst_value = format_total(burst_count, character_budget=5)
    text = f"+{burst_value}"
    text_width = 4 * len(text) - 1
    bubble_width = text_width + 4
    bubble_height = 9
    left, top, right, bottom = face_rect
    bubble_x = max(2, min(right - bubble_width, surface_width - bubble_width - 2))
    bubble_y = top - bubble_height - 2
    if bubble_y < 2:
        bubble_y = bottom + 2
    bubble_y = max(2, min(bubble_y, surface_height - bubble_height - 2))
    fill = surface.fill
    fill((7, 22, 36, 255), (bubble_x, bubble_y, bubble_width, bubble_height))
    _draw_text(surface, text, bubble_x + 2, bubble_y + 2, (117, 236, 255, 255))


def draw_counter_overlay(
    surface: Any,
    rect: Sequence[int],
    total: int,
    *,
    keyboard_presses: int = 0,
    mouse_clicks: int = 0,
    hovered: bool = False,
    pulse: bool = False,
    burst_count: int = 0,
) -> str:
    """Draw an opaque dark face inset and centered 3x5 red pixel digits.

    ``rect`` uses left/top/right/bottom screen coordinates. Digits are drawn
    after atlas scaling so they remain crisp at the default 176px pet size.
    """

    if len(rect) != 4:
        raise ValueError("counter face rectangle must be left, top, right, bottom")
    left, top, right, bottom = (int(value) for value in rect)
    width = max(0, right - left)
    height = max(0, bottom - top)
    if width <= 0 or height <= 0:
        return ""

    fill = surface.fill
    fill((22, 76, 112, 255) if pulse else (8, 14, 27, 255), (left, top, width, height))
    if height < 5 or width < 3:
        return ""
    character_budget = max(1, (width + 1) // 4)
    text = format_total(total, character_budget=character_budget)
    base_glyph_width = len(text) * 3 + max(0, len(text) - 1)
    pixel_scale = min(
        3,
        height // 5,
        width // max(1, base_glyph_width),
    )
    if pixel_scale < 1:
        return ""
    glyph_width = base_glyph_width * pixel_scale
    glyph_height = 5 * pixel_scale
    x = left + (width - glyph_width) // 2
    y = top + (height - glyph_height) // 2
    for character in text:
        glyph = _GLYPHS.get(character)
        if glyph is None:
            x += 4
            continue
        for row_index, row in enumerate(glyph):
            for column_index, pixel in enumerate(row):
                if pixel == "1":
                    pixel_x = x + column_index * pixel_scale
                    pixel_y = y + row_index * pixel_scale
                    if left <= pixel_x < right and top <= pixel_y < bottom:
                        fill(
                            (255, 125, 83, 255) if pulse else (255, 61, 57, 255),
                            (pixel_x, pixel_y, pixel_scale, pixel_scale),
                        )
        x += 4 * pixel_scale
    if hovered:
        _draw_detail_panel(
            surface,
            (left, top, right, bottom),
            total=total,
            keyboard_presses=keyboard_presses,
            mouse_clicks=mouse_clicks,
        )
    if burst_count > 0:
        _draw_burst(surface, (left, top, right, bottom), burst_count)
    return text
