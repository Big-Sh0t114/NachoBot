"""Keyboard binding parsing and physical press/release edges, without hooks."""

from __future__ import annotations


_KEYS = {
    "ctrl": 0x11,
    "control": 0x11,
    "ctrl_l": 0xA2,
    "ctrl_r": 0xA3,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "alt": 0x12,
    "alt_l": 0xA4,
    "alt_r": 0xA5,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "shift": 0x10,
    "shift_l": 0xA0,
    "shift_r": 0xA1,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "win": 0x5B,
    "win_l": 0x5B,
    "win_r": 0x5C,
    "space": 0x20,
    "spacebar": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "caps_lock": 0x14,
    "capslock": 0x14,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
    **{str(number): 0x30 + number for number in range(10)},
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
}


def parse_ptt_key(value: str) -> tuple[int, ...]:
    """Empty disables keyboard PTT; Escape stays reserved for cancellation."""
    if not isinstance(value, str):
        raise ValueError("push_to_talk_key must be a string")
    if not value.strip():
        return ()
    parts = [part.strip().casefold() for part in value.split("+")]
    if len(parts) > 4 or any(part not in _KEYS for part in parts):
        raise ValueError(
            "invalid push_to_talk_key; use F8, ctrl+alt+v, space or ctrl_r; Escape is reserved"
        )
    keys = tuple(_KEYS[part] for part in parts)
    if len(set(keys)) != len(keys):
        raise ValueError("push_to_talk_key contains duplicate keys")
    return keys


class HoldToTalkKey:
    def __init__(self, value: str):
        self.keys = parse_ptt_key(value)
        self._was_down: bool | None = None

    def poll(self, key_state) -> str | None:
        if not self.keys:
            return None
        down = all(key_state(key) & 0x8000 for key in self.keys)
        previous, self._was_down = self._was_down, down
        # Starting while already held must never open the microphone.
        if previous is None or previous == down:
            return None
        return "press" if down else "release"
