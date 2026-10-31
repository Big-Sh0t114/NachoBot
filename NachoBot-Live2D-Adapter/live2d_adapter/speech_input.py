"""Bounded PCM speech segmentation for continuous microphone input."""

from __future__ import annotations

import math
from array import array
from collections import deque
import sys


class SpeechSegmenter:
    """Energy gate with pre-roll and pause detection; ASR remains in perception."""

    def __init__(self, rate: int):
        self.rate = rate
        self._noise = 80.0
        self.reset()

    def reset(self) -> None:
        self._prefix: deque[bytes] = deque()
        self._prefix_size = 0
        self._blocks: list[bytes] = []
        self._size = 0
        self._speech = self._silence = 0.0

    @property
    def speaking(self) -> bool:
        return self._speech >= 0.12

    def feed(self, pcm: bytes) -> bytes | None:
        if not pcm:
            return None
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        energy = math.sqrt(sum(value * value for value in samples) / len(samples))
        voiced = energy >= max(350, self._noise * 3)
        duration = len(pcm) / (self.rate * 2)
        if not voiced:
            self._noise = min(250, self._noise * 0.98 + energy * 0.02)
        if not self._blocks:
            if not voiced:
                self._prefix.append(pcm)
                self._prefix_size += len(pcm)
                while self._prefix and self._prefix_size > self.rate // 2:
                    self._prefix_size -= len(self._prefix.popleft())
                return None
            self._blocks.extend(self._prefix)
            self._size = self._prefix_size
            self._prefix.clear()
            self._prefix_size = 0
        self._blocks.append(pcm)
        self._size += len(pcm)
        self._speech += duration if voiced else 0
        self._silence = 0 if voiced else self._silence + duration
        if self._silence < 0.8 and self._size < self.rate * 2 * 15:
            return None
        result = b"".join(self._blocks) if self._speech >= 0.2 else None
        self.reset()
        return result
