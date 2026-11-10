"""Bounded floor roaming for the sprite renderer."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol


class RandomSource(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True, slots=True)
class RoamingStep:
    position: tuple[float, float]
    state: str
    direction: int


class SpriteRoamingPolicy:
    """Move along the work-area floor, pause at turns, and fall after a drag."""

    def __init__(
        self,
        *,
        speed_px_per_second: float,
        idle_pause_min_seconds: float,
        idle_pause_max_seconds: float,
        margin: int = 0,
        random_source: RandomSource | None = None,
    ) -> None:
        self.speed = max(1.0, float(speed_px_per_second))
        self.idle_pause_min = max(0.0, float(idle_pause_min_seconds))
        self.idle_pause_max = max(self.idle_pause_min, float(idle_pause_max_seconds))
        self.margin = max(0, int(margin))
        self._random = random_source or random.Random()
        self.direction = 1
        self._pause_remaining = self._pause_duration()

    def advance(
        self,
        position: tuple[float, float],
        *,
        viewport: tuple[int, int],
        work_area: tuple[int, int, int, int],
        delta_time: float,
        allowed: bool,
    ) -> RoamingStep:
        """Advance one frame; disallowed movement leaves the window untouched."""

        x, y = float(position[0]), float(position[1])
        if not allowed:
            return RoamingStep((x, y), "idle", self.direction)

        dt = max(0.0, min(float(delta_time), 0.25))
        width, height = viewport
        left, top, right, bottom = work_area
        minimum_x = float(left + self.margin)
        maximum_x = float(max(minimum_x, right - width - self.margin))
        floor_y = float(max(top, bottom - height - self.margin))

        # The pet can be dropped anywhere after a manual drag. Let the fall row
        # animate a bounded return to the work-area floor before walking.
        if y < floor_y:
            y = min(floor_y, y + self.speed * dt)
            return RoamingStep((x, y), "fall", self.direction)
        y = floor_y

        if self._pause_remaining > 0:
            self._pause_remaining = max(0.0, self._pause_remaining - dt)
            return RoamingStep((x, y), "idle", self.direction)

        next_x = x + self.direction * self.speed * dt
        if next_x < minimum_x or next_x > maximum_x:
            next_x = min(maximum_x, max(minimum_x, next_x))
            self.direction *= -1
            self._pause_remaining = self._pause_duration()
            return RoamingStep((next_x, y), "idle", self.direction)
        return RoamingStep((next_x, y), "walk", self.direction)

    def note_user_input(self) -> None:
        """Start a fresh quiet pause after pointer or keyboard activity."""

        self._pause_remaining = self._pause_duration()

    def _pause_duration(self) -> float:
        return self._random.uniform(self.idle_pause_min, self.idle_pause_max)
