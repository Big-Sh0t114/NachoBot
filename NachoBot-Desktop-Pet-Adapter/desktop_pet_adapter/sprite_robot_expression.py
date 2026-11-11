"""Deterministic pixel LED expressions for the sprite robot face."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

_CYCLE_SECONDS = 10.0
_NORMAL_EXPRESSION_HOLDS: tuple[tuple[float, str], ...] = (
    (2.0, "neutral"),
    (2.0, "smile"),
    (2.0, "curious"),
    (2.0, "sleepy"),
    (2.0, "focused"),
)
_BLINK_WINDOWS: tuple[tuple[float, float], ...] = (
    (1.50, 1.65),
    (3.50, 3.65),
    (5.50, 5.65),
    (7.50, 7.65),
    (9.50, 9.65),
)
_STATE_EXPRESSIONS = {
    "drag": "clenched",
    "fall": "surprised",
    "react": "excited",
}
_PATTERNS: dict[str, tuple[str, ...]] = {
    "neutral": (
        ".........",
        "###...###",
        "#.#...#.#",
        "###...###",
        "..#####..",
    ),
    "smile": (
        "..#...#..",
        ".#.#.#.#.",
        "#.......#",
        "..#...#..",
        "...###...",
    ),
    "curious": (
        ".#....###",
        "#.#...#.#",
        ".#....###",
        ".........",
        "..#####..",
    ),
    "sleepy": (
        ".........",
        ".###.###.",
        "..#...#..",
        ".........",
        "...###...",
    ),
    "focused": (
        "###...###",
        ".#.....#.",
        ".........",
        "...###...",
        "..#####..",
    ),
    "clenched": (
        ".##...##..",
        "###...###.",
        ".##...##..",
        ".........",
        ".#######..",
    ),
    "surprised": (
        "..#...#..",
        ".###.###.",
        "..#...#..",
        "....#....",
        "...###...",
    ),
    "excited": (
        ".#.....#.",
        "###...###",
        ".#.....#.",
        "..#.#.#..",
        "...###...",
    ),
    "blink": (
        ".........",
        "###...###",
        ".........",
        ".........",
        "..#####..",
    ),
}
_BACKGROUND = (8, 14, 27, 255)
_LED_RED = (255, 61, 57, 255)


def select_robot_expression(state: str, elapsed_seconds: float) -> str:
    """Select a stable expression for an animation state and elapsed face time."""

    fixed_expression = _STATE_EXPRESSIONS.get(str(state).casefold())
    if fixed_expression is not None:
        return fixed_expression
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        elapsed = 0.0
    if not math.isfinite(elapsed) or elapsed < 0:
        elapsed = 0.0
    phase = elapsed % _CYCLE_SECONDS
    if any(start <= phase < end for start, end in _BLINK_WINDOWS):
        return "blink"
    for hold, expression in _NORMAL_EXPRESSION_HOLDS:
        if phase < hold:
            return expression
        phase -= hold
    return _NORMAL_EXPRESSION_HOLDS[0][1]


def draw_robot_expression(
    surface: Any,
    face_rect: Sequence[int],
    expression: str,
) -> None:
    """Draw a crop-safe, integer-pixel LED expression inside a face rectangle."""

    if len(face_rect) != 4:
        return
    try:
        left, top, right, bottom = (int(value) for value in face_rect)
        surface_width, surface_height = surface.get_size()
    except (TypeError, ValueError, AttributeError):
        return
    left = max(0, min(int(surface_width), left))
    top = max(0, min(int(surface_height), top))
    right = max(left, min(int(surface_width), right))
    bottom = max(top, min(int(surface_height), bottom))
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return
    surface.fill(_BACKGROUND, (left, top, width, height))
    pattern = _PATTERNS.get(str(expression).casefold(), _PATTERNS["neutral"])
    pattern_height = len(pattern)
    pattern_width = max((len(row) for row in pattern), default=0)
    if pattern_width <= 0 or pattern_height <= 0:
        return
    if width >= pattern_width and height >= pattern_height:
        scale = max(1, min(3, width // pattern_width, height // pattern_height))
        origin_x = left + (width - pattern_width * scale) // 2
        origin_y = top + (height - pattern_height * scale) // 2
        for y, row in enumerate(pattern):
            for x, pixel in enumerate(row):
                if pixel == "#":
                    surface.fill(
                        _LED_RED,
                        (origin_x + x * scale, origin_y + y * scale, scale, scale),
                    )
        return
    # Tiny/legacy face crops still get a compact, clipped pixel expression.
    for y, row in enumerate(pattern):
        pixel_y = top + (y * max(0, height - 1) + (pattern_height - 1) // 2) // max(1, pattern_height - 1)
        for x, pixel in enumerate(row):
            if pixel == "#":
                pixel_x = left + (x * max(0, width - 1) + (pattern_width - 1) // 2) // max(1, pattern_width - 1)
                surface.fill(_LED_RED, (pixel_x, pixel_y, 1, 1))
