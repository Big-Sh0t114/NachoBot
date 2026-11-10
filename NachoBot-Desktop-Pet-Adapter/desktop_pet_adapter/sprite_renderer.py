"""Transparent frame-atlas desktop renderer, independent of Live2D."""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
import queue
import random
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .backend import BackendContext
from .desktop_controller import (
    DesktopPetController,
    DesktopPetControllerHost,
    DesktopPetFrame,
)
from .desktop_movement import MovementGeometry
from .desktop_pet import (
    DesktopPetState,
    DesktopPetStateStore,
    clamp_window_position,
    initial_window_position,
)
from .input_counter import InputCounter
from .sprite_counter_display import CounterFeedbackState, draw_counter_overlay
from .sprite_movement import SpriteRoamingPolicy
from .sprite_robot_expression import draw_robot_expression, select_robot_expression
from .windows_dpi import per_monitor_dpi_context

REQUIRED_STATES = ("idle", "walk", "climb", "fall", "drag", "react")
REACT_DURATION_SECONDS = 1.25
USER_ACTIVITY_PAUSE_SECONDS = 3.0
COUNTER_IDLE_SECONDS = 5.0
MAX_QUEUED_WAV_ITEMS = 8
MAX_QUEUED_WAV_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SpriteStateRow:
    row: int | None
    frames: int
    fps: float
    anchor_x: float | None = None
    anchor_y: float | None = None
    frame_rects: tuple[tuple[int, int, int, int], ...] | None = None
    face_rects: tuple[tuple[int, int, int, int], ...] | None = None
    robot_rects: tuple[tuple[int, int, int, int], ...] | None = None


@dataclass(frozen=True, slots=True)
class SpriteAtlasManifest:
    manifest_path: Path
    atlas_path: Path
    frame_width: int
    frame_height: int
    columns: int
    alpha_margin: int
    states: dict[str, SpriteStateRow]
    robot_scale: float = 1.0
    left_atlas_path: Path | None = None
    left_states: dict[str, SpriteStateRow] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> SpriteAtlasManifest:
        manifest_path = path.expanduser().resolve()
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"sprite manifest could not be read: {manifest_path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("sprite manifest version must be 1")
        atlas_name = payload.get("atlas", "sprites.png")
        if not isinstance(atlas_name, str) or not atlas_name.strip():
            raise ValueError("sprite manifest atlas must be a non-empty relative filename")
        atlas_path = (manifest_path.parent / atlas_name).resolve()
        if not atlas_path.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("sprite manifest atlas must remain inside its asset directory")
        try:
            columns = int(payload.get("columns", 4))
            frame_width = int(payload.get("frame_width", 0))
            frame_height = int(payload.get("frame_height", 0))
            alpha_margin = int(payload.get("alpha_margin", 5))
        except (TypeError, ValueError) as exc:
            raise ValueError("sprite manifest dimensions and columns must be integers") from exc
        if columns <= 0 or alpha_margin < 1:
            raise ValueError("sprite manifest columns must be positive and alpha_margin at least 1")
        raw_states = payload.get("states")
        if not isinstance(raw_states, dict):
            raise ValueError("sprite manifest states must be an object")
        states: dict[str, SpriteStateRow] = {}
        robot_scale = float(payload.get("robot_scale", 1.0))
        if not math.isfinite(robot_scale) or not 1 <= robot_scale <= 3:
            raise ValueError("sprite robot_scale must be between 1 and 3")
        for state in REQUIRED_STATES:
            raw = raw_states.get(state)
            if not isinstance(raw, Mapping):
                raise ValueError(f"sprite manifest is missing state {state!r}")
            try:
                raw_rects = raw.get("frame_rects")
                frame_rects = None
                if raw_rects is not None:
                    if not isinstance(raw_rects, list) or not raw_rects:
                        raise ValueError("frame_rects must be a non-empty array")
                    parsed_rects = []
                    for rect in raw_rects:
                        if (
                            not isinstance(rect, list)
                            or len(rect) != 4
                            or any(type(value) is not int for value in rect)
                        ):
                            raise ValueError("each frame_rect must be [x, y, width, height]")
                        parsed_rects.append(tuple(rect))
                    frame_rects = tuple(parsed_rects)
                raw_face_rects = raw.get("face_rects")
                face_rects = None
                if raw_face_rects is not None:
                    if not isinstance(raw_face_rects, list) or not raw_face_rects:
                        raise ValueError("face_rects must be a non-empty array")
                    parsed_face_rects = []
                    for rect in raw_face_rects:
                        if (
                            not isinstance(rect, list)
                            or len(rect) != 4
                            or any(type(value) is not int for value in rect)
                        ):
                            raise ValueError("each face_rect must be [x, y, width, height]")
                        parsed_face_rects.append(tuple(rect))
                    face_rects = tuple(parsed_face_rects)
                raw_row = raw.get("row")
                raw_robots = raw.get("robot_rects")
                robot_rects = None
                if raw_robots is not None:
                    if not isinstance(raw_robots, list) or not raw_robots:
                        raise ValueError("robot_rects must be a non-empty array")
                    if any(not isinstance(rect, list) or len(rect) != 4
                           or any(type(v) is not int for v in rect) for rect in raw_robots):
                        raise ValueError("each robot_rect must be [x, y, width, height]")
                    robot_rects = tuple(tuple(rect) for rect in raw_robots)
                row = None if raw_row is None else int(raw_row)
                frames = int(raw.get("frames", len(frame_rects) if frame_rects else columns))
                fps = float(raw["fps"])
                anchor_x = raw.get("anchor_x")
                anchor_y = raw.get("anchor_y")
                anchor_x = None if anchor_x is None else float(anchor_x)
                anchor_y = None if anchor_y is None else float(anchor_y)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"sprite manifest state {state!r} has invalid row metadata") from exc
            if (
                (row is None and frame_rects is None)
                or (row is not None and row < 0)
                or not 1 <= frames <= (len(frame_rects) if frame_rects else columns)
                or not math.isfinite(fps)
                or not 0.1 <= fps <= 60
                or (frame_rects is not None and len(frame_rects) != frames)
                or (face_rects is not None and len(face_rects) != frames)
                or (robot_rects is not None and (len(robot_rects) != frames or face_rects is None))
            ):
                raise ValueError(f"sprite manifest state {state!r} has invalid row, frame count, or fps")
            if anchor_x is not None and (
                not math.isfinite(anchor_x) or not 0 <= anchor_x <= 1
            ):
                raise ValueError(f"sprite manifest state {state!r} anchor_x must be between 0 and 1")
            if anchor_y is not None and (
                not math.isfinite(anchor_y) or not 0 <= anchor_y <= 1
            ):
                raise ValueError(f"sprite manifest state {state!r} anchor_y must be between 0 and 1")
            states[state] = SpriteStateRow(
                row,
                frames,
                fps,
                anchor_x,
                anchor_y,
                frame_rects,
                face_rects,
                robot_rects,
            )
        if not atlas_path.is_file():
            raise FileNotFoundError(f"sprite atlas image not found: {atlas_path}")
        try:
            with Image.open(atlas_path) as image:
                if image.format != "PNG" or "A" not in image.getbands():
                    raise ValueError("sprite atlas must be a transparent PNG with an alpha channel")
                image = image.convert("RGBA")
                has_grid_rows = any(row.frame_rects is None for row in states.values())
                if has_grid_rows and min(frame_width, frame_height) <= 0:
                    raise ValueError("grid-sliced sprite states require frame_width and frame_height")
                if has_grid_rows and (
                    image.size[0] % frame_width
                    or image.size[1] % frame_height
                    or image.width != columns * frame_width
                ):
                    raise ValueError("sprite atlas dimensions do not match the declared grid")
                alpha = image.getchannel("A")
                if alpha.getextrema() == (255, 255):
                    raise ValueError("sprite atlas has no transparent pixels")
                thresholded_alpha = alpha.point(lambda value: 255 if value > 64 else 0)
                for state, row in states.items():
                    if row.frame_rects is not None:
                        rects = row.frame_rects
                    else:
                        assert row.row is not None
                        if (row.row + 1) * frame_height > image.height:
                            raise ValueError(f"sprite atlas row for {state!r} is outside the image")
                        rects = tuple(
                            (
                                column * frame_width,
                                row.row * frame_height,
                                frame_width,
                                frame_height,
                            )
                            for column in range(row.frames)
                        )
                    for column, (x, y, width, height) in enumerate(rects):
                        if (
                            x < 0
                            or y < 0
                            or width <= 0
                            or height <= 0
                            or x + width > image.width
                            or y + height > image.height
                        ):
                            raise ValueError(f"sprite atlas {state!r} frame {column} is outside the image")
                        if row.face_rects is not None:
                            face_x, face_y, face_width, face_height = row.face_rects[column]
                            if (
                                face_x < 0
                                or face_y < 0
                                or face_width <= 0
                                or face_height <= 0
                                or face_x + face_width > width
                                or face_y + face_height > height
                            ):
                                raise ValueError(
                                    f"sprite atlas {state!r} face rectangle {column} is outside its frame"
                                )
                        if row.robot_rects is not None:
                            rx, ry, rw, rh = row.robot_rects[column]
                            fx, fy, fw, fh = row.face_rects[column]
                            if (rx < 0 or ry < 0 or rw <= 0 or rh <= 0
                                    or rx + rw > width or ry + rh > height
                                    or fx < rx or fy < ry or fx + fw > rx + rw or fy + fh > ry + rh):
                                raise ValueError("robot rectangle must fit its frame and contain its face")
                        tile = thresholded_alpha.crop((x, y, x + width, y + height))
                        bounds = tile.getbbox()
                        if bounds is None:
                            raise ValueError(f"sprite atlas contains an empty {state!r} frame")
                        left, top, right, bottom = bounds
                        if min(left, top, width - right, height - bottom) < alpha_margin:
                            raise ValueError(
                                f"sprite atlas {state!r} frame {column} needs at least "
                                f"{alpha_margin}px transparent margin"
                            )
        except OSError as exc:
            raise ValueError(f"sprite atlas image could not be inspected: {exc}") from exc
        left_atlas_path = None
        left_states: dict[str, SpriteStateRow] = {}
        if "left_atlas" in payload:
            left_atlas_name = payload.get("left_atlas")
            if not isinstance(left_atlas_name, str) or not left_atlas_name.strip():
                raise ValueError("sprite manifest left_atlas must be a non-empty relative filename")
            left_atlas_path = (manifest_path.parent / left_atlas_name).resolve()
            if not left_atlas_path.is_relative_to(manifest_path.parent.resolve()):
                raise ValueError("sprite manifest left_atlas must remain inside its asset directory")
            if not left_atlas_path.is_file():
                raise FileNotFoundError(f"left sprite atlas image not found: {left_atlas_path}")
            raw_left_states = payload.get("left_states", {})
            if not isinstance(raw_left_states, Mapping):
                raise ValueError("sprite manifest left_states must be an object")
            if any(state not in REQUIRED_STATES for state in raw_left_states):
                raise ValueError("sprite manifest left_states contains an unknown state")
            left_states = {
                state: states[state]
                for state in ("walk", "climb")
            }
            for state, raw in raw_left_states.items():
                left_states[state] = _parse_left_state_row(state, raw, states[state])
            _validate_sprite_atlas(
                left_atlas_path,
                left_states,
                columns=columns,
                frame_width=frame_width,
                frame_height=frame_height,
                alpha_margin=alpha_margin,
                atlas_label="left sprite atlas",
            )
        elif "left_states" in payload:
            raise ValueError("sprite manifest left_states requires left_atlas")
        return cls(
            manifest_path=manifest_path,
            atlas_path=atlas_path,
            frame_width=frame_width,
            frame_height=frame_height,
            columns=columns,
            alpha_margin=alpha_margin,
            states=states,
            robot_scale=robot_scale,
            left_atlas_path=left_atlas_path,
            left_states=left_states,
        )


def _parse_left_state_row(
    state: str,
    raw: Any,
    fallback: SpriteStateRow,
) -> SpriteStateRow:
    if not isinstance(raw, Mapping):
        raise ValueError(f"sprite manifest left state {state!r} must be an object")
    merged: dict[str, Any] = {
        "row": fallback.row,
        "frames": fallback.frames,
        "fps": fallback.fps,
        "anchor_x": fallback.anchor_x,
        "anchor_y": fallback.anchor_y,
        "frame_rects": fallback.frame_rects,
        "face_rects": fallback.face_rects,
        "robot_rects": fallback.robot_rects,
    }
    merged.update(raw)
    try:
        parsed_rects: dict[str, tuple[tuple[int, int, int, int], ...] | None] = {}
        for name in ("frame_rects", "face_rects", "robot_rects"):
            values = merged.get(name)
            if values is None:
                parsed_rects[name] = None
                continue
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(f"{name} must be a non-empty array")
            parsed = []
            for rect in values:
                if (
                    not isinstance(rect, (list, tuple))
                    or len(rect) != 4
                    or any(type(value) is not int for value in rect)
                ):
                    raise ValueError(f"each {name[:-1]} must be [x, y, width, height]")
                parsed.append(tuple(rect))
            parsed_rects[name] = tuple(parsed)
        row = None if merged.get("row") is None else int(merged["row"])
        frame_rects = parsed_rects["frame_rects"]
        frames = int(merged.get("frames", len(frame_rects) if frame_rects else fallback.frames))
        fps = float(merged["fps"])
        anchor_x = None if merged.get("anchor_x") is None else float(merged["anchor_x"])
        anchor_y = None if merged.get("anchor_y") is None else float(merged["anchor_y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"sprite manifest left state {state!r} has invalid row metadata") from exc
    face_rects = parsed_rects["face_rects"]
    robot_rects = parsed_rects["robot_rects"]
    if (
        (row is None and frame_rects is None)
        or (row is not None and row < 0)
        or not 1 <= frames <= (len(frame_rects) if frame_rects else max(fallback.frames, 1))
        or frames != fallback.frames
        or not math.isfinite(fps)
        or not 0.1 <= fps <= 60
        or (frame_rects is not None and len(frame_rects) != frames)
        or (face_rects is not None and len(face_rects) != frames)
        or (robot_rects is not None and (len(robot_rects) != frames or face_rects is None))
    ):
        raise ValueError(f"sprite manifest left state {state!r} has invalid row, frame count, or fps")
    if anchor_x is not None and (not math.isfinite(anchor_x) or not 0 <= anchor_x <= 1):
        raise ValueError(f"sprite manifest left state {state!r} anchor_x must be between 0 and 1")
    if anchor_y is not None and (not math.isfinite(anchor_y) or not 0 <= anchor_y <= 1):
        raise ValueError(f"sprite manifest left state {state!r} anchor_y must be between 0 and 1")
    return SpriteStateRow(
        row,
        frames,
        fps,
        anchor_x,
        anchor_y,
        frame_rects,
        face_rects,
        robot_rects,
    )


def _validate_sprite_atlas(
    atlas_path: Path,
    states: Mapping[str, SpriteStateRow],
    *,
    columns: int,
    frame_width: int,
    frame_height: int,
    alpha_margin: int,
    atlas_label: str,
) -> None:
    try:
        with Image.open(atlas_path) as image:
            if image.format != "PNG" or "A" not in image.getbands():
                raise ValueError(f"{atlas_label} must be a transparent PNG with an alpha channel")
            image = image.convert("RGBA")
            has_grid_rows = any(row.frame_rects is None for row in states.values())
            if has_grid_rows and min(frame_width, frame_height) <= 0:
                raise ValueError("grid-sliced sprite states require frame_width and frame_height")
            if has_grid_rows and (
                image.width % frame_width
                or image.height % frame_height
                or image.width != columns * frame_width
            ):
                raise ValueError(f"{atlas_label} dimensions do not match the declared grid")
            alpha = image.getchannel("A")
            if alpha.getextrema() == (255, 255):
                raise ValueError(f"{atlas_label} has no transparent pixels")
            thresholded_alpha = alpha.point(lambda value: 255 if value > 64 else 0)
            for state, row in states.items():
                if row.frame_rects is not None:
                    rects = row.frame_rects
                else:
                    assert row.row is not None
                    if (row.row + 1) * frame_height > image.height:
                        raise ValueError(f"{atlas_label} row for {state!r} is outside the image")
                    rects = tuple(
                        (column * frame_width, row.row * frame_height, frame_width, frame_height)
                        for column in range(row.frames)
                    )
                for index, (x, y, width, height) in enumerate(rects):
                    if (
                        x < 0
                        or y < 0
                        or width <= 0
                        or height <= 0
                        or x + width > image.width
                        or y + height > image.height
                    ):
                        raise ValueError(f"{atlas_label} {state!r} frame {index} is outside the image")
                    if row.face_rects is not None:
                        fx, fy, fw, fh = row.face_rects[index]
                        if fx < 0 or fy < 0 or fw <= 0 or fh <= 0 or fx + fw > width or fy + fh > height:
                            raise ValueError(
                                f"{atlas_label} {state!r} face rectangle {index} is outside its frame"
                            )
                    if row.robot_rects is not None:
                        rx, ry, rw, rh = row.robot_rects[index]
                        fx, fy, fw, fh = row.face_rects[index]
                        if (
                            rx < 0 or ry < 0 or rw <= 0 or rh <= 0
                            or rx + rw > width or ry + rh > height
                            or fx < rx or fy < ry or fx + fw > rx + rw or fy + fh > ry + rh
                        ):
                            raise ValueError(f"{atlas_label} robot rectangle must fit its frame and contain its face")
                    bounds = thresholded_alpha.crop((x, y, x + width, y + height)).getbbox()
                    if bounds is None:
                        raise ValueError(f"{atlas_label} contains an empty {state!r} frame")
                    left, top, right, bottom = bounds
                    if min(left, top, width - right, height - bottom) < alpha_margin:
                        raise ValueError(
                            f"{atlas_label} {state!r} frame {index} needs at least "
                            f"{alpha_margin}px transparent margin"
                        )
    except OSError as exc:
        raise ValueError(f"{atlas_label} image could not be inspected: {exc}") from exc


class _LayeredAlphaWindow:
    """Update a pygame HWND with a top-down premultiplied-alpha DIB."""

    def __init__(self, hwnd: int, width: int, height: int, logger: Any) -> None:
        if os.name != "nt":
            raise RuntimeError("sprite desktop windows require Windows")
        import win32con
        import win32gui

        self.hwnd = int(hwnd)
        self.width = int(width)
        self.height = int(height)
        self.logger = logger
        self._win32con = win32con
        self._win32gui = win32gui
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        self._gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        self._gdi32.CreateDIBSection.restype = ctypes.c_void_p
        self._gdi32.CreateDIBSection.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._gdi32.SelectObject.restype = ctypes.c_void_p
        self._gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        self._gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        self._screen_dc = win32gui.GetDC(0)
        self._memory_dc = self._gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._memory_dc:
            raise ctypes.WinError(ctypes.get_last_error())

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        class RgbQuad(ctypes.Structure):
            _fields_ = [
                ("rgbBlue", ctypes.c_uint8),
                ("rgbGreen", ctypes.c_uint8),
                ("rgbRed", ctypes.c_uint8),
                ("rgbReserved", ctypes.c_uint8),
            ]

        class BitmapInfo(ctypes.Structure):
            _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", RgbQuad * 1)]

        self._bitmap_info = BitmapInfo()
        self._bitmap_info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        self._bitmap_info.bmiHeader.biWidth = self.width
        self._bitmap_info.bmiHeader.biHeight = -self.height
        self._bitmap_info.bmiHeader.biPlanes = 1
        self._bitmap_info.bmiHeader.biBitCount = 32
        self._bitmap_info.bmiHeader.biCompression = 0
        self._bitmap_info.bmiHeader.biSizeImage = self.width * self.height * 4
        self._bits = ctypes.c_void_p()
        self._bitmap = self._gdi32.CreateDIBSection(
            self._screen_dc,
            ctypes.byref(self._bitmap_info),
            0,
            ctypes.byref(self._bits),
            None,
            0,
        )
        if not self._bitmap or not self._bits:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self._previous_bitmap = self._gdi32.SelectObject(self._memory_dc, self._bitmap)
        self._user32.UpdateLayeredWindow.restype = ctypes.c_int
        self._user32.UpdateLayeredWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]

    def present(self, surface: Any, position: tuple[int, int]) -> None:
        raw = bytearray(self._pygame_bytes(surface))
        # AC_SRC_ALPHA expects premultiplied RGB channels in its DIB.
        for offset in range(0, len(raw), 4):
            alpha = raw[offset + 3]
            raw[offset] = (raw[offset] * alpha + 127) // 255
            raw[offset + 1] = (raw[offset + 1] * alpha + 127) // 255
            raw[offset + 2] = (raw[offset + 2] * alpha + 127) // 255
        ctypes.memmove(self._bits, bytes(raw), self.width * self.height * 4)
        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]

        class Size(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_int32), ("cy", ctypes.c_int32)]

        class BlendFunction(ctypes.Structure):
            _fields_ = [
                ("BlendOp", ctypes.c_uint8),
                ("BlendFlags", ctypes.c_uint8),
                ("SourceConstantAlpha", ctypes.c_uint8),
                ("AlphaFormat", ctypes.c_uint8),
            ]

        destination = Point(int(position[0]), int(position[1]))
        source = Point(0, 0)
        size = Size(self.width, self.height)
        blend = BlendFunction(0, 0, 255, 1)
        success = self._user32.UpdateLayeredWindow(
            ctypes.c_void_p(self.hwnd),
            ctypes.c_void_p(self._screen_dc),
            ctypes.byref(destination),
            ctypes.byref(size),
            ctypes.c_void_p(self._memory_dc),
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            2,
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _pygame_bytes(surface: Any) -> bytes:
        import pygame

        return pygame.image.tobytes(surface, "BGRA", False)

    def close(self) -> None:
        if getattr(self, "_memory_dc", None):
            if getattr(self, "_previous_bitmap", None):
                self._gdi32.SelectObject(self._memory_dc, self._previous_bitmap)
                self._previous_bitmap = None
            self._gdi32.DeleteDC(self._memory_dc)
            self._memory_dc = None
        if getattr(self, "_bitmap", None):
            self._gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if getattr(self, "_screen_dc", None):
            self._win32gui.ReleaseDC(0, self._screen_dc)
            self._screen_dc = None


class SpriteRenderer:
    """Render frame-atlas animations and host the shared desktop-pet UI."""

    def __init__(
        self,
        context: BackendContext,
        manifest: SpriteAtlasManifest,
        *,
        enable_desktop_controller: bool = True,
    ) -> None:
        self.context = context
        self.config = context.config
        self.logger = context.logger
        self.command_queue = context.command_queue
        self.width = self.config.renderer.width
        self._character_width = self.width
        all_rows = (*manifest.states.values(), *manifest.left_states.values())
        if manifest.robot_scale > 1 and any(row.robot_rects for row in all_rows):
            self.width += math.ceil(self.width * 0.28 * (manifest.robot_scale - 1))
        self.height = self.config.renderer.height
        self.manifest = manifest
        self.running = False
        self.pcm_audio_ready = False
        self.audio_ready = False
        self._pygame = None
        self._screen = None
        self._surface = None
        self._native_window: _LayeredAlphaWindow | None = None
        self._hwnd: int | None = None
        self._position = (0.0, 0.0)
        self._work_area = (0, 0, 1920, 1080)
        self._state_store = DesktopPetStateStore(
            self.config.desktop_pet.state_path
            if self.config.desktop_pet.remember_position
            else None,
            self.logger,
        )
        self._saved_state = (
            self._state_store.load() if self.config.desktop_pet.enabled else DesktopPetState()
        )
        self._visible = self._saved_state.visible
        self._always_on_top = (
            self._saved_state.always_on_top
            if self._saved_state.always_on_top is not None
            else self.config.desktop_pet.always_on_top
        )
        self._click_through = (
            self._saved_state.click_through
            if self._saved_state.click_through is not None
            else self.config.desktop_pet.click_through
        )
        self._frames: dict[str, list[Any]] = {}
        self._frame_bounds: dict[str, list[tuple[int, int, int, int]]] = {}
        self._face_bounds: dict[str, list[tuple[int, int, int, int] | None]] = {}
        self._left_frames: dict[str, list[Any]] = {}
        self._left_frame_bounds: dict[str, list[tuple[int, int, int, int]]] = {}
        self._left_face_bounds: dict[str, list[tuple[int, int, int, int] | None]] = {}
        self._input_counter: InputCounter | None = None
        self._input_feedback: CounterFeedbackState | None = None
        self._last_observed_counter_total: int | None = None
        self._last_counter_input_at: float | None = None
        self._ordinary_face_epoch: float | None = None
        self._last_game_input_menu_state: tuple[bool, str] | None = None
        self._face_hover_error_logged = False
        self._state = "idle"
        self._frame_index = 0
        self._frame_elapsed = 0.0
        self._state_deadline = 0.0
        self._command_state_until = 0.0
        self._speech_reaction_active = False
        self._direction = 1
        self._speaking = False
        self._dragging = False
        self._drag_start_global = (0, 0)
        self._drag_last_global = (0, 0)
        self._drag_distance = 0
        self._last_position_save_at = 0.0
        self._last_user_input_at = time.monotonic()
        self._last_position_before_update: tuple[float, float] | None = None
        self._wav_channel = None
        self._audio_queue: deque[bytes] = deque()
        self._audio_queue_bytes = 0
        self._external_speaking = False
        self._pcm_player = None
        self._desktop_controller: DesktopPetController | None = None
        self._enable_desktop_controller = enable_desktop_controller
        self._roaming = SpriteRoamingPolicy(
            speed_px_per_second=self.config.sprite.movement_speed_px_per_second,
            idle_pause_min_seconds=self.config.sprite.idle_pause_min_seconds,
            idle_pause_max_seconds=self.config.sprite.idle_pause_max_seconds,
            margin=self.config.desktop_pet.margin,
        )
        if enable_desktop_controller and self.config.desktop_pet.enabled:
            self._desktop_controller = DesktopPetController(
                self.config.desktop_pet,
                self.logger,
                DesktopPetControllerHost(
                    enqueue_command=self._enqueue_desktop_command,
                    sample_frame=self._desktop_frame,
                    move_window=self._move_pet_window,
                    present_movement=self._present_movement_intent,
                    start_motion=self._start_motion,
                    read_window_flag=self._read_window_flag,
                    sync_chat_anchor=self._sync_chat_anchor,
                    open_parameters=lambda: None,
                    set_parameter=lambda _value: None,
                    reset_parameters=lambda _parameter: None,
                    toggle_visibility=self._toggle_visibility,
                    toggle_click_through=self._toggle_click_through,
                    toggle_topmost=self._toggle_topmost,
                    reset_position=self._reset_position,
                    quit=self._quit,
                    report_click=self.context.on_click,
                ),
                own_pid=os.getpid(),
                autonomous_displacement=True,
            )

    def run(self) -> None:
        """Run the renderer with thread-local physical-pixel coordinates."""

        with per_monitor_dpi_context():
            self._run_with_dpi_context()

    def _run_with_dpi_context(self) -> None:
        """Create the alpha window and run until the host asks for shutdown."""

        import pygame

        self._pygame = pygame
        self.running = True
        try:
            pygame.init()
            try:
                pygame.mixer.init()
                self.audio_ready = bool(pygame.mixer.get_init())
                from .sprite_audio import PygamePCMPlayer

                # The PCM helper owns channel 0. Reserve it from pygame's
                # automatic Sound.play allocator, and use channel 1 explicitly
                # for bounded WAV playback.
                pygame.mixer.set_reserved(1)
                self._pcm_player = PygamePCMPlayer(pygame, self.logger)
                self.pcm_audio_ready = self._pcm_player.ready
                try:
                    self._wav_channel = pygame.mixer.Channel(1)
                except Exception:
                    self._wav_channel = None
            except pygame.error as exc:
                self.logger.warning("[Sprite] audio playback unavailable: {}", exc)
            flags = pygame.NOFRAME
            self._screen = pygame.display.set_mode((self.width, self.height), flags)
            pygame.display.set_caption(self.config.desktop_pet.title)
            self._surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA, 32)
            self._load_frames()
            self._work_area = self._get_work_area()
            self._position = self._initial_position()
            if os.name == "nt":
                self._attach_native_window()
            elif self.config.desktop_pet.enabled:
                raise RuntimeError("sprite desktop-pet windows require Windows")
            if self.config.desktop_pet.enabled:
                try:
                    self._input_counter = InputCounter(
                        self.config.desktop_pet.state_path,
                        self.logger,
                    )
                    self._input_counter.start()
                    self._input_feedback = CounterFeedbackState()
                    self._input_feedback.prime(self._input_counter.baseline_total)
                    self._last_observed_counter_total = self._input_counter.baseline_total
                    self._last_counter_input_at = None
                except Exception as exc:
                    if self._input_counter is not None:
                        try:
                            self._input_counter.stop()
                        except Exception as stop_exc:
                            self.logger.warning(
                                "[Sprite] input counter cleanup failed: {}",
                                stop_exc,
                            )
                    self._input_counter = None
                    self._input_feedback = None
                    self.logger.warning("[Sprite] input counter could not start: {}", exc)
            self._draw_frame()
            self._present_frame()
            if self._desktop_controller is not None:
                if not self.pcm_audio_ready and self._desktop_controller.chat is not None:
                    # Local chat emits PCM voice_stream events. Keep its UI
                    # from claiming audio is ready if this backend cannot play
                    # those events on the current device.
                    self._desktop_controller.chat._set_voice_enabled(False)
                self._desktop_controller.start_chat_host()
                self._desktop_controller.start()
            self.logger.info(
                "[Sprite] renderer ready: atlas={} size={}x{} states={}",
                self.manifest.atlas_path,
                self.width,
                self.height,
                tuple(self.manifest.states),
            )
            self.context.on_ready()
            clock = pygame.time.Clock()
            while self.running:
                # Keep up with display/DPI changes while using physical pixels.
                self._refresh_work_area()
                delta_time = clock.tick(60) / 1000.0
                self._handle_events(pygame.event.get())
                self._poll_drag()
                self._process_commands()
                if self._desktop_controller is not None:
                    self._last_position_before_update = self._position
                    self._desktop_controller.tick(delta_time)
                    self._update_interaction_animation()
                    self._advance_roaming(delta_time)
                self._pump_voice_streams()
                self._pump_audio_queue()
                self._update_reaction_deadline()
                self._advance_animation(delta_time)
                self._draw_frame()
                self._present_frame()
                self._save_state()
                if self._input_counter is not None:
                    self._input_counter.pump_save()
                    self._refresh_game_input_menu()
        finally:
            self._shutdown()

    def run_preview_smoke(
        self,
        output_path: Path,
        *,
        frame_count: int = 120,
        movement_pause_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Render a bounded, off-screen smoke preview without tray or OS writes."""

        with per_monitor_dpi_context():
            return self._run_preview_smoke_with_dpi_context(
                output_path,
                frame_count=frame_count,
                movement_pause_seconds=movement_pause_seconds,
            )

    def _run_preview_smoke_with_dpi_context(
        self,
        output_path: Path,
        *,
        frame_count: int = 120,
        movement_pause_seconds: float = 0.0,
    ) -> dict[str, Any]:
        import pygame

        pygame.init()
        pygame.display.set_mode((1, 1), pygame.HIDDEN)
        self._pygame = pygame
        self._surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA, 32)
        self._ordinary_face_epoch = None
        self._load_frames()
        self._work_area = (0, 0, max(800, self.width * 3), max(600, self.height * 3))
        self._position = (
            80.0,
            float(self._work_area[3] - self.height - self.config.desktop_pet.margin),
        )
        # An explicit pause value keeps this reproducible while regular runtime
        # roaming continues to use the configured idle interval.
        self._roaming = SpriteRoamingPolicy(
            speed_px_per_second=self.config.sprite.movement_speed_px_per_second,
            idle_pause_min_seconds=max(0.0, movement_pause_seconds),
            idle_pause_max_seconds=max(0.0, movement_pause_seconds),
            margin=self.config.desktop_pet.margin,
            random_source=random.Random(0),
        )
        self._state = "walk"
        self._direction = 1
        observed_frames: set[int] = set()
        start_position = self._position
        try:
            self.running = True
            for _ in range(max(1, int(frame_count))):
                step = self._roaming.advance(
                    self._position,
                    viewport=(self.width, self.height),
                    work_area=self._work_area,
                    delta_time=1 / 60,
                    allowed=True,
                )
                self._position = step.position
                self._set_animation(step.state, step.direction)
                self._advance_animation(1 / 60)
                self._draw_frame()
                observed_frames.add(self._frame_index)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pygame.image.save(self._surface, str(output_path))
            result = {
                "preview": str(output_path.resolve()),
                "frames_seen": sorted(observed_frames),
                "start_position": start_position,
                "end_position": self._position,
                "movement_pixels": abs(self._position[0] - start_position[0]),
            }
            return result
        finally:
            self.running = False
            self._clear_frame_caches()
            self._ordinary_face_epoch = None
            pygame.quit()
            self._pygame = None
            self._surface = None

    def run_native_smoke(
        self,
        output_path: Path,
        *,
        duration_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Exercise the real layered HWND briefly without tray UI or services."""

        with per_monitor_dpi_context():
            return self._run_native_smoke_with_dpi_context(
                output_path,
                duration_seconds=duration_seconds,
            )

    def _run_native_smoke_with_dpi_context(
        self,
        output_path: Path,
        *,
        duration_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Exercise the real layered HWND briefly without services or tray UI."""

        if os.name != "nt":
            raise RuntimeError("native layered-window smoke requires Windows")
        import pygame
        import win32con
        import win32gui

        duration = max(0.25, min(float(duration_seconds), 10.0))
        hwnd = None
        result: dict[str, Any] = {}
        frames_seen: set[int] = set()
        frame_count = 0
        start_position = (0.0, 0.0)
        pygame.display.init()
        self._pygame = pygame
        try:
            self._screen = pygame.display.set_mode((self.width, self.height), pygame.NOFRAME)
            pygame.display.set_caption("NachoBot sprite smoke")
            self._surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA, 32)
            self._ordinary_face_epoch = None
            self._load_frames()
            self._work_area = self._get_work_area()
            self._position = tuple(
                float(value)
                for value in initial_window_position(
                    "bottom_left",
                    self.width,
                    self.height,
                    self._work_area,
                    self.config.desktop_pet.margin,
                )
            )
            self._always_on_top = False
            self._click_through = True
            self._visible = True
            self._attach_native_window()
            hwnd = self._hwnd
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if not ex_style & win32con.WS_EX_LAYERED:
                raise RuntimeError("sprite HWND did not receive WS_EX_LAYERED")
            self._visible = False
            self._apply_window_flags()
            hidden_transition_verified = not bool(win32gui.IsWindowVisible(hwnd))
            self._present_frame()
            self._visible = True
            self._apply_window_flags()
            visible_transition_verified = bool(win32gui.IsWindowVisible(hwnd))
            if not (hidden_transition_verified and visible_transition_verified):
                raise RuntimeError("sprite HWND hide/show transition did not apply")
            self._roaming = SpriteRoamingPolicy(
                speed_px_per_second=self.config.sprite.movement_speed_px_per_second,
                idle_pause_min_seconds=0,
                idle_pause_max_seconds=0,
                margin=self.config.desktop_pet.margin,
                random_source=random.Random(0),
            )
            self._state = "walk"
            start_position = self._position
            clock = pygame.time.Clock()
            started = time.monotonic()
            bounds_verified = True
            self.running = True
            while time.monotonic() - started < duration:
                self._refresh_work_area()
                delta = max(1 / 240, min(1 / 15, clock.tick(60) / 1000.0 or 1 / 60))
                step = self._roaming.advance(
                    self._position,
                    viewport=(self.width, self.height),
                    work_area=self._work_area,
                    delta_time=delta,
                    allowed=True,
                )
                self._move_pet_window(step.position[0], step.position[1])
                self._set_animation(step.state, step.direction)
                self._advance_animation(delta)
                self._draw_frame()
                self._present_frame()
                try:
                    import win32gui

                    left, top, right, bottom = win32gui.GetWindowRect(self._hwnd)
                    work_left, work_top, work_right, work_bottom = self._work_area
                    margin = self.config.desktop_pet.margin
                    bounds_verified = bounds_verified and bool(
                        left >= work_left + margin
                        and top >= work_top + margin
                        and right <= work_right - margin
                        and bottom <= work_bottom - margin
                    )
                except Exception:
                    bounds_verified = False
                frames_seen.add(self._frame_index)
                frame_count += 1
                pygame.event.pump()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pygame.image.save(self._surface, str(output_path))
            result = {
                "preview": str(output_path.resolve()),
                "frames_seen": sorted(frames_seen),
                "frame_count": frame_count,
                "start_position": start_position,
                "end_position": self._position,
                "movement_pixels": abs(self._position[0] - start_position[0]),
                "layered_style": bool(ex_style & win32con.WS_EX_LAYERED),
                "visibility_cycle_verified": (
                    hidden_transition_verified and visible_transition_verified
                ),
                "update_layered_window_calls": frame_count,
                "work_area": self._work_area,
                "native_bounds_verified": bounds_verified,
            }
        finally:
            self.running = False
            try:
                if hwnd and win32gui.IsWindow(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            if self._native_window is not None:
                self._native_window.close()
                self._native_window = None
            pygame.quit()
            try:
                result["window_destroyed"] = not bool(hwnd and win32gui.IsWindow(hwnd))
            except Exception:
                result["window_destroyed"] = False
            result["pygame_cleaned"] = not pygame.get_init() and not pygame.display.get_init()
            self._pygame = None
            self._screen = None
            self._surface = None
            self._clear_frame_caches()
            self._ordinary_face_epoch = None
        return result

    def _clear_frame_caches(self) -> None:
        for cache in (
            self._frames,
            self._frame_bounds,
            self._face_bounds,
            self._left_frames,
            self._left_frame_bounds,
            self._left_face_bounds,
        ):
            cache.clear()

    def _selected_frame_caches(
        self,
    ) -> tuple[
        dict[str, list[Any]],
        dict[str, list[tuple[int, int, int, int]]],
        dict[str, list[tuple[int, int, int, int] | None]],
    ]:
        if self._direction < 0 and self._state in self._left_frames:
            return self._left_frames, self._left_frame_bounds, self._left_face_bounds
        return self._frames, self._frame_bounds, self._face_bounds

    def _selected_state_row(self) -> SpriteStateRow:
        if self._direction < 0 and self._state in self.manifest.left_states:
            return self.manifest.left_states[self._state]
        return self.manifest.states[self._state]

    def _load_frames(self) -> None:
        pygame = self._pygame
        image = pygame.image.load(str(self.manifest.atlas_path))
        self._clear_frame_caches()
        image_sources: list[tuple[str, Any, Mapping[str, SpriteStateRow]]] = [
            ("right", image, self.manifest.states)
        ]
        if self.manifest.left_atlas_path is not None and self.manifest.left_states:
            left_image = pygame.image.load(str(self.manifest.left_atlas_path))
            image_sources.append(("left", left_image, self.manifest.left_states))
        robot_template = None
        idle = self.manifest.states["idle"]
        if idle.robot_rects is not None:
            rx, ry, rw, rh = idle.robot_rects[0]
            ix, iy = (
                idle.frame_rects[0][:2]
                if idle.frame_rects
                else (0, idle.row * self.manifest.frame_height)
            )
            robot_template = image.subsurface(pygame.Rect(ix + rx, iy + ry, rw, rh)).copy()
        prepared_sources: list[tuple[str, str, SpriteStateRow, list[Any]]] = []
        maximum_visible_height = 0
        maximum_visible_width = 0
        for direction, source_image, source_states in image_sources:
            for state, row in source_states.items():
                if row.frame_rects is not None:
                    rects = row.frame_rects
                else:
                    assert row.row is not None
                    rects = tuple(
                        (
                            column * self.manifest.frame_width,
                            row.row * self.manifest.frame_height,
                            self.manifest.frame_width,
                            self.manifest.frame_height,
                        )
                        for column in range(row.frames)
                    )
                prepared = []
                for index, (x, y, width, height) in enumerate(rects):
                    tile = pygame.Surface((width, height), pygame.SRCALPHA, 32)
                    tile.blit(source_image, (0, 0), pygame.Rect(x, y, width, height))
                    visible_bounds = tile.get_bounding_rect(min_alpha=64)
                    pixel_bounds = tile.get_bounding_rect(min_alpha=1)
                    cropped = tile.subsurface(pixel_bounds).copy()
                    visible_local = (
                        visible_bounds.left - pixel_bounds.left,
                        visible_bounds.top - pixel_bounds.top,
                        visible_bounds.right - pixel_bounds.left,
                        visible_bounds.bottom - pixel_bounds.top,
                    )
                    maximum_visible_height = max(maximum_visible_height, visible_bounds.height)
                    maximum_visible_width = max(maximum_visible_width, visible_bounds.width)
                    face_rect = row.face_rects[index] if row.face_rects is not None else None
                    robot_rect = row.robot_rects[index] if row.robot_rects is not None else None
                    # Use the isolated right-facing idle shell in both directional assets.
                    robot_image = None
                    if robot_rect is not None:
                        robot_image = (
                            pygame.transform.smoothscale(robot_template, robot_rect[2:])
                            if robot_template is not None
                            else tile.subsurface(pygame.Rect(robot_rect)).copy()
                        )
                    prepared.append(
                        (cropped, visible_local, tuple(pixel_bounds), face_rect, robot_rect, robot_image)
                    )
                prepared_sources.append((direction, state, row, prepared))

        # One scale across both directional assets prevents size pumping on turns.
        target_visible_height = max(1, round(self.height * 0.80))
        target_visible_width = max(1, round(self._character_width * 0.92))
        scale = min(
            target_visible_height / max(1, maximum_visible_height),
            target_visible_width / max(1, maximum_visible_width),
        )
        for direction, state, row, prepared in prepared_sources:
            if direction == "left":
                frames_cache = self._left_frames
                bounds_cache = self._left_frame_bounds
                faces_cache = self._left_face_bounds
            else:
                frames_cache = self._frames
                bounds_cache = self._frame_bounds
                faces_cache = self._face_bounds
            frames_cache[state] = []
            bounds_cache[state] = []
            faces_cache[state] = []
            row_anchor_bounds = prepared[0][1]
            for cropped, visible_local, pixel_bounds, face_rect, robot_rect, robot_image in prepared:
                width = max(1, round(cropped.get_width() * scale))
                height = max(1, round(cropped.get_height() * scale))
                scaled = pygame.transform.smoothscale(cropped, (width, height))
                content = pygame.Surface((self.width, self.height), pygame.SRCALPHA, 32)
                visible_left, visible_top, visible_right, visible_bottom = visible_local
                scaled_visible = pygame.Rect(
                    round(visible_left * scale),
                    round(visible_top * scale),
                    max(1, round((visible_right - visible_left) * scale)),
                    max(1, round((visible_bottom - visible_top) * scale)),
                )
                anchor_bounds = visible_local if state in {"idle", "walk"} else row_anchor_bounds
                anchor_left, anchor_top, anchor_right, anchor_bottom = anchor_bounds
                if state in {"idle", "walk"}:
                    source_anchor_x = round((scaled_visible.left + scaled_visible.right) / 2)
                    source_anchor_y = scaled_visible.bottom
                else:
                    scaled_anchor_left = round(anchor_left * scale)
                    scaled_anchor_top = round(anchor_top * scale)
                    scaled_anchor_right = round(anchor_right * scale)
                    scaled_anchor_bottom = scaled_anchor_top + max(
                        1, round((anchor_bottom - anchor_top) * scale)
                    )
                    source_anchor_x = round((scaled_anchor_left + scaled_anchor_right) / 2)
                    source_anchor_y = scaled_anchor_bottom
                interactions = self.config.desktop_pet.interactions
                target_anchor_x = round(
                    (row.anchor_x if row.anchor_x is not None else interactions.foot_anchor_x)
                    * self._character_width
                )
                target_anchor_y = round(
                    (row.anchor_y if row.anchor_y is not None else interactions.foot_anchor_y)
                    * self.height
                )
                dest_x = target_anchor_x - source_anchor_x
                dest_y = target_anchor_y - source_anchor_y
                dest_x = max(-scaled_visible.left, min(self._character_width - scaled_visible.right, dest_x))
                dest_y = max(-scaled_visible.top, min(self.height - scaled_visible.bottom, dest_y))
                content.blit(scaled, (dest_x, dest_y))
                frames_cache[state].append(content)
                bounds_cache[state].append(
                    (
                        dest_x + scaled_visible.left,
                        dest_y + scaled_visible.top,
                        dest_x + scaled_visible.right,
                        dest_y + scaled_visible.bottom,
                    )
                )
                if face_rect is None:
                    faces_cache[state].append(None)
                else:
                    face_x, face_y, face_width, face_height = face_rect
                    crop_left, crop_top, _crop_right, _crop_bottom = pixel_bounds
                    faces_cache[state].append(
                        (
                            dest_x + round((face_x - crop_left) * scale),
                            dest_y + round((face_y - crop_top) * scale),
                            dest_x + round((face_x + face_width - crop_left) * scale),
                            dest_y + round((face_y + face_height - crop_top) * scale),
                        )
                    )
                    if robot_rect is not None and self.manifest.robot_scale > 1:
                        rx, ry, rw, rh = robot_rect
                        factor = scale * self.manifest.robot_scale
                        robot_width = min(self.width, max(1, round(rw * factor)))
                        robot_height = min(self.height, max(1, round(rh * factor)))
                        sx, sy = robot_width / rw, robot_height / rh
                        old_left = dest_x + (rx - crop_left) * scale
                        old_top = dest_y + (ry - crop_top) * scale
                        robot_left = min(self.width - robot_width, max(0, round(old_left)))
                        robot_top = min(
                            self.height - robot_height,
                            max(0, round(old_top - (robot_height - rh * scale) / 2)),
                        )
                        enlarged = pygame.transform.smoothscale(robot_image, (robot_width, robot_height))
                        erase = pygame.mask.from_surface(robot_image, 128).to_surface(
                            setcolor=(255, 255, 255, 0), unsetcolor=(255, 255, 255, 255)
                        )
                        erase = pygame.transform.smoothscale(
                            erase, (max(1, round(rw * scale)), max(1, round(rh * scale)))
                        )
                        content.blit(
                            erase,
                            (round(old_left), round(old_top)),
                            special_flags=pygame.BLEND_RGBA_MULT,
                        )
                        content.blit(enlarged, (robot_left, robot_top))
                        faces_cache[state][-1] = (
                            robot_left + round((face_x - rx) * sx),
                            robot_top + round((face_y - ry) * sy),
                            robot_left + round((face_x + face_width - rx) * sx),
                            robot_top + round((face_y + face_height - ry) * sy),
                        )
                        left, top, right, bottom = bounds_cache[state][-1]
                        bounds_cache[state][-1] = (
                            min(left, robot_left),
                            min(top, robot_top),
                            max(right, robot_left + robot_width),
                            max(bottom, robot_top + robot_height),
                        )

    def _draw_frame(self) -> None:
        pygame = self._pygame
        if self._surface is None or pygame is None:
            return
        self._surface.fill((0, 0, 0, 0))
        frames, _frame_bounds, face_bounds = self._selected_frame_caches()
        state_frames = frames[self._state]
        index = self._frame_index % len(state_frames)
        frame = state_frames[index]
        self._surface.blit(frame, (0, 0))
        counts = None
        now = time.monotonic()
        if self._input_counter is not None:
            counts = self._input_counter.snapshot()
            current_total = max(0, int(counts.total))
            previous_total = self._last_observed_counter_total
            delta = 0 if previous_total is None else max(0, current_total - previous_total)
            self._last_observed_counter_total = current_total
            feedback = self._input_feedback
            if feedback is not None:
                if previous_total is None:
                    feedback.prime(current_total)
                else:
                    feedback.observe(current_total, now=now)
            if delta:
                self._last_counter_input_at = now
        counter_rects = face_bounds.get(self._state)
        face_rect = counter_rects[index] if counter_rects else None
        if face_rect is None:
            self._ordinary_face_epoch = None
            return
        if counts is not None and self._counter_overlay_visible(now=now):
            self._ordinary_face_epoch = None
            feedback = self._input_feedback
            hovered = self._cursor_over_robot_face(face_rect)
            draw_counter_overlay(
                self._surface,
                face_rect,
                counts.total,
                keyboard_presses=counts.keyboard_presses,
                mouse_clicks=counts.mouse_clicks,
                hovered=hovered,
                pulse=feedback.pulse_active(now=now) if feedback is not None else False,
                burst_count=(
                    feedback.burst_count
                    if feedback is not None and feedback.burst_active(now=now)
                    else 0
                ),
            )
            return
        if self._ordinary_face_epoch is None:
            self._ordinary_face_epoch = now
        expression = select_robot_expression(self._state, now - self._ordinary_face_epoch)
        draw_robot_expression(self._surface, face_rect, expression)

    def _counter_overlay_visible(self, *, now: float) -> bool:
        last_input_at = self._last_counter_input_at
        if last_input_at is None:
            return False
        elapsed = now - last_input_at
        return 0 <= elapsed < COUNTER_IDLE_SECONDS

    def _present_frame(self) -> None:
        # UpdateLayeredWindow rejects updates while the HWND is explicitly
        # hidden on some Windows builds. The window has no visible pixels to
        # refresh in that state; the next visible frame will be uploaded once
        # the visibility toggle shows it again.
        if self._native_window is not None and self._surface is not None and self._visible:
            self._native_window.present(self._surface, (round(self._position[0]), round(self._position[1])))

    def _attach_native_window(self) -> None:
        import pygame
        import win32con
        import win32gui

        info = pygame.display.get_wm_info()
        hwnd = info.get("window")
        if not hwnd:
            raise RuntimeError("pygame did not provide its Win32 window handle")
        self._hwnd = int(hwnd)
        style = win32gui.GetWindowLong(self._hwnd, win32con.GWL_EXSTYLE)
        style |= win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE
        if self.config.desktop_pet.hide_from_taskbar:
            style |= win32con.WS_EX_TOOLWINDOW
        if self._click_through:
            style |= win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(self._hwnd, win32con.GWL_EXSTYLE, style)
        self._native_window = _LayeredAlphaWindow(self._hwnd, self.width, self.height, self.logger)
        self._apply_window_flags(initial=True)

    def _apply_window_flags(self, *, initial: bool = False) -> None:
        if not self._hwnd:
            return
        import win32con
        import win32gui

        was_visible = bool(win32gui.IsWindowVisible(self._hwnd))
        style = win32gui.GetWindowLong(self._hwnd, win32con.GWL_EXSTYLE)
        if self._click_through:
            style |= win32con.WS_EX_TRANSPARENT
        else:
            style &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(self._hwnd, win32con.GWL_EXSTYLE, style)
        z_order = win32con.HWND_TOPMOST if self._always_on_top else win32con.HWND_NOTOPMOST
        flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOSIZE
        if not initial:
            flags |= win32con.SWP_NOMOVE
        else:
            flags &= ~win32con.SWP_NOMOVE
        win32gui.SetWindowPos(
            self._hwnd,
            z_order,
            round(self._position[0]) if initial else 0,
            round(self._position[1]) if initial else 0,
            self.width,
            self.height,
            flags,
        )
        if self._visible and not was_visible:
            # SDL and some Windows builds cache layered attributes across a
            # hide/show transition. Refresh the layered bit before the next
            # UpdateLayeredWindow call (SetLayeredWindowAttributes users must
            # do this before switching back to UpdateLayeredWindow).
            style = win32gui.GetWindowLong(self._hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                self._hwnd,
                win32con.GWL_EXSTYLE,
                style & ~win32con.WS_EX_LAYERED,
            )
            win32gui.SetWindowLong(
                self._hwnd,
                win32con.GWL_EXSTYLE,
                style | win32con.WS_EX_LAYERED,
            )
        win32gui.ShowWindow(
            self._hwnd,
            win32con.SW_SHOWNOACTIVATE if self._visible else win32con.SW_HIDE,
        )

    def _handle_events(self, events: list[Any]) -> None:
        pygame = self._pygame
        for event in events:
            if event.type == pygame.QUIT:
                self.running = False
                continue
            if event.type in (
                pygame.MOUSEBUTTONDOWN,
                pygame.MOUSEBUTTONUP,
                pygame.MOUSEMOTION,
                pygame.MOUSEWHEEL,
                pygame.KEYDOWN,
                pygame.KEYUP,
            ):
                self._last_user_input_at = time.monotonic()
                self._roaming.note_user_input()
                if self._desktop_controller is not None:
                    self._desktop_controller.note_user_input(pet_dragging=self._dragging)
            if event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1 and self.config.desktop_pet.enabled and not self._click_through:
                    self._dragging = True
                    self._drag_distance = 0
                    self._set_animation("drag", self._direction)
                    self._drag_start_global = self._cursor_position()
                    self._drag_last_global = self._drag_start_global
                    if self._desktop_controller is not None:
                        self._desktop_controller.note_user_input(pet_dragging=True)
                elif event.button == 3 and self._desktop_controller is not None:
                    self._desktop_controller.handle_pet_click(3)
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1 and self._dragging:
                    self._finish_drag()
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                self.running = False

    def _poll_drag(self) -> None:
        if not self._dragging or os.name != "nt" or not self._hwnd:
            return
        try:
            import win32api

            current = tuple(int(value) for value in win32api.GetCursorPos())
            dx = current[0] - self._drag_last_global[0]
            dy = current[1] - self._drag_last_global[1]
            if dx or dy:
                self._move_pet_window(
                    self._position[0] + dx,
                    self._position[1] + dy,
                    drag_point=current,
                )
                self._drag_distance += abs(dx) + abs(dy)
                self._drag_last_global = current
            if not win32api.GetAsyncKeyState(0x01) & 0x8000:
                self._finish_drag()
        except Exception as exc:
            self.logger.debug("[Sprite] manual drag polling failed: {}", exc)

    def _finish_drag(self) -> None:
        self._dragging = False
        self._set_animation("idle", self._direction)
        self._roaming.note_user_input()
        if self._desktop_controller is not None:
            if self._drag_distance <= 14:
                self._desktop_controller.handle_pet_click(1)
            else:
                self._desktop_controller.cancel_click_sequence()
            self._desktop_controller.note_user_input(pet_dragging=False)
        self._drag_distance = 0
        self._save_state(force=True)

    def _cursor_position(self) -> tuple[int, int]:
        if os.name == "nt":
            try:
                import win32api

                return tuple(int(value) for value in win32api.GetCursorPos())
            except Exception:
                pass
        return (round(self._position[0]), round(self._position[1]))

    def _process_commands(self) -> None:
        while True:
            try:
                command, value = self.command_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if command == "desktop_toggle_game_input":
                    self._toggle_game_input()
                    continue
                if str(command).startswith("desktop_"):
                    if self._desktop_controller is not None:
                        self._desktop_controller.handle_command(str(command), value)
                    continue
                if command in {"motion", "body_action"}:
                    self._set_command_animation(self._state_for_label(str(value)))
                elif command == "random_motion":
                    group = value.get("group", "react") if isinstance(value, Mapping) else value
                    self._set_command_animation(self._state_for_label(str(group)))
                elif command == "state":
                    self._set_command_animation(self._state_for_label(str(value)))
                elif command == "emotion":
                    self._set_command_animation("react")
                elif command == "speaking":
                    self._external_speaking = bool(value)
                    self._sync_audio_speaking()
                    if self._speaking:
                        self._speech_reaction_active = True
                        # Audio output itself pauses roaming. Keep the visual
                        # reaction finite so interaction/motion states may
                        # replace it without inheriting an infinite hold.
                        self._set_command_animation("react")
                    else:
                        self._release_speech_reaction()
                elif command == "voice_stream":
                    self._handle_voice_stream(value)
                elif command == "play_audio":
                    self._play_audio(bytes(value), reset=True)
                elif command == "queue_audio":
                    if isinstance(value, Mapping):
                        self._play_audio(bytes(value.get("audio", b"")), reset=bool(value.get("reset")))
                elif command == "stop_audio":
                    self._stop_audio()
                elif command in {"auto_gaze", "param_tween"}:
                    self.logger.debug("[Sprite] ignored unsupported model-specific command: {}", command)
            except Exception as exc:
                self.logger.warning("[Sprite] command {} was ignored: {}", command, exc)

    def _play_audio(self, data: bytes, *, reset: bool) -> None:
        pygame = self._pygame
        if not data or pygame is None or not self.audio_ready:
            return
        if reset:
            self._audio_queue.clear()
            self._audio_queue_bytes = 0
            self._external_speaking = False
            if self._wav_channel is not None:
                self._wav_channel.stop()
            if self._pcm_player is not None:
                self._pcm_player.stop()
            self._sync_audio_speaking()
        if self._wav_channel is None:
            self.logger.warning("[Sprite] WAV playback channel is unavailable")
            return
        if self._wav_channel.get_busy() or (
            self._pcm_player is not None and self._pcm_player.active
        ):
            if (
                len(self._audio_queue) >= MAX_QUEUED_WAV_ITEMS
                or self._audio_queue_bytes + len(data) > MAX_QUEUED_WAV_BYTES
            ):
                self.logger.warning("[Sprite] WAV playback queue is full; audio was dropped")
                return
            self._audio_queue.append(data)
            self._audio_queue_bytes += len(data)
            return
        try:
            sound = pygame.mixer.Sound(file=io.BytesIO(data))
        except pygame.error as exc:
            self.logger.warning("[Sprite] WAV audio could not be loaded: {}", exc)
            return
        self._wav_channel.play(sound)

    def _stop_audio(self) -> None:
        self._audio_queue.clear()
        self._audio_queue_bytes = 0
        if self._wav_channel is not None:
            self._wav_channel.stop()
        if self._pcm_player is not None:
            self._pcm_player.stop()
        self._external_speaking = False
        self._sync_audio_speaking()
        self._release_speech_reaction()

    def _pump_audio_queue(self) -> None:
        channel = self._wav_channel
        if (
            channel is None
            or channel.get_busy()
            or not self._audio_queue
            or not self.audio_ready
            or (self._wav_channel is not None and self._wav_channel.get_busy())
            or (self._pcm_player is not None and self._pcm_player.active)
        ):
            return
        data = self._audio_queue.popleft()
        self._audio_queue_bytes -= len(data)
        try:
            sound = self._pygame.mixer.Sound(file=io.BytesIO(data))
            channel.play(sound)
        except self._pygame.error as exc:
            self.logger.warning("[Sprite] queued WAV audio could not be loaded: {}", exc)

    def _handle_voice_stream(self, event: Any) -> None:
        if self._pcm_player is None:
            self.logger.warning("[Sprite] PCM playback is unavailable; voice chunk ignored")
            return
        if isinstance(event, Mapping) and event.get("event") == "start":
            self._audio_queue.clear()
            self._audio_queue_bytes = 0
            self._external_speaking = False
            if self._wav_channel is not None:
                self._wav_channel.stop()
        try:
            self._pcm_player.handle_event(event)
        except (ValueError, BufferError, RuntimeError) as exc:
            self.logger.warning("[Sprite] PCM voice stream rejected: {}", exc)
        self._sync_audio_speaking()

    def _pump_voice_streams(self) -> None:
        while True:
            try:
                event = self.context.voice_stream_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_voice_stream(event)
        if self._pcm_player is not None:
            self._pcm_player.pump()
        self._sync_audio_speaking()

    def _sync_audio_speaking(self) -> None:
        was_speaking = self._speaking
        self._speaking = bool(
            self._external_speaking
            or (self._pcm_player is not None and self._pcm_player.active)
        )
        if was_speaking and not self._speaking:
            self._release_speech_reaction()

    def _release_speech_reaction(self) -> None:
        """Release only the speech-owned reaction, preserving other poses."""

        if not self._speech_reaction_active:
            return
        self._speech_reaction_active = False
        if self._state == "react":
            self._command_state_until = 0.0
            self._set_animation("idle", self._direction)

    def _advance_roaming(self, delta_time: float) -> None:
        if not self.config.desktop_pet.enabled or self.config.renderer.backend != "sprite":
            return
        self._refresh_work_area()
        interaction_active = bool(
            self._desktop_controller is not None
            and self._desktop_controller.interaction_movement_active
        )
        visible = self._visible
        user_idle = self._system_idle_seconds()
        user_active = user_idle is not None and user_idle < USER_ACTIVITY_PAUSE_SECONDS
        can_roam = visible and not self._dragging and not interaction_active and not user_active
        if interaction_active:
            return
        if self._speaking or (
            self._state == "react" and time.monotonic() < self._state_deadline
        ) or time.monotonic() < self._command_state_until:
            return
        step = self._roaming.advance(
            self._position,
            viewport=(self.width, self.height),
            work_area=self._work_area,
            delta_time=delta_time,
            allowed=can_roam,
        )
        self._direction = step.direction
        if step.position != self._position:
            self._move_pet_window(step.position[0], step.position[1])
        self._set_animation("drag" if self._dragging else step.state, self._direction)

    @staticmethod
    def _system_idle_seconds() -> float | None:
        if os.name != "nt":
            return None
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]

        info = LastInputInfo(ctypes.sizeof(LastInputInfo), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        current = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((current - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def _update_interaction_animation(self) -> None:
        controller = self._desktop_controller
        if controller is None or not controller.interaction_movement_active:
            return
        status = controller._movement_status
        state = {
            "climbing": "climb",
            "falling": "fall",
            "recovering": "fall",
            "approaching": "walk",
            "following": "walk",
            "perched": "idle",
        }.get(status, "idle")
        previous = self._last_position_before_update
        if previous is not None:
            dx = self._position[0] - previous[0]
            if abs(dx) > 0.5:
                self._direction = 1 if dx > 0 else -1
        self._set_animation(state, self._direction)

    def _set_animation(self, state: str, direction: int) -> None:
        if state not in self.manifest.states:
            state = "react"
        direction = 1 if direction >= 0 else -1
        if state != self._state or direction != self._direction:
            self._frame_index = 0
            self._frame_elapsed = 0.0
            if state == "react" and not self._speaking:
                self._state_deadline = time.monotonic() + REACT_DURATION_SECONDS
        self._state = state
        self._direction = direction

    def _set_command_animation(self, state: str, *, hold: bool = False) -> None:
        self._set_animation(state, self._direction)
        if state == "idle":
            self._command_state_until = 0.0
        else:
            self._command_state_until = (
                float("inf") if hold else time.monotonic() + REACT_DURATION_SECONDS
            )

    def _advance_animation(self, delta_time: float) -> None:
        row = self._selected_state_row()
        self._frame_elapsed += max(0.0, min(0.25, delta_time))
        self._frame_index = int(self._frame_elapsed * row.fps) % row.frames

    def _update_reaction_deadline(self) -> None:
        if self._state == "react" and not self._speaking and time.monotonic() >= self._state_deadline:
            self._set_animation("idle", self._direction)

    @staticmethod
    def _state_for_label(label: str) -> str:
        normalized = label.strip().casefold().replace("_", " ").replace("-", " ")
        if any(word in normalized for word in ("climb", "approach window", "follow window")):
            return "climb" if "climb" in normalized else "walk"
        if any(word in normalized for word in ("fall", "drop", "recover")):
            return "fall"
        if "walk" in normalized or "follow" in normalized:
            return "walk"
        if "drag" in normalized:
            return "drag"
        if "idle" in normalized or "standby" in normalized or "normal" in normalized:
            return "idle"
        return "react"

    def _present_movement_intent(self, intent: str) -> bool:
        normalized = str(intent).casefold()
        self._set_animation("climb" if "climb" in normalized else "walk", self._direction)
        return True

    def _start_motion(self, group: str, priority: int = 3) -> bool:
        self.command_queue.put(("motion", group))
        return True

    def _enqueue_desktop_command(self, command: str, value: Any = None) -> None:
        self.command_queue.put((command, value))

    def _desktop_frame(self) -> DesktopPetFrame | None:
        if not self.config.desktop_pet.enabled:
            return None
        self._refresh_work_area()
        visible = self._visible
        if self._hwnd and os.name == "nt":
            try:
                import win32gui

                rect = win32gui.GetWindowRect(self._hwnd)
                if (
                    round(self._position[0]) != int(rect[0])
                    or round(self._position[1]) != int(rect[1])
                ):
                    self._position = (float(rect[0]), float(rect[1]))
                visible = visible and bool(win32gui.IsWindowVisible(self._hwnd))
            except Exception:
                pass
        interactions = self.config.desktop_pet.interactions
        row = self._selected_state_row()
        return DesktopPetFrame(
            position=self._position,
            geometry=MovementGeometry(
                viewport=(self.width, self.height),
                foot_anchor=(
                    self._character_width
                    * (row.anchor_x if row.anchor_x is not None else interactions.foot_anchor_x)
                    / self.width,
                    row.anchor_y if row.anchor_y is not None else interactions.foot_anchor_y,
                ),
            ),
            work_area=self._work_area,
            visible=visible,
            pet_dragging=self._dragging,
            motion_busy=self._state in {"react", "drag"},
        )

    def _get_work_area(
        self,
        *,
        drag_point: tuple[int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        if os.name == "nt":
            try:
                import win32api
                import win32con

                if drag_point is None and self._dragging:
                    drag_point = tuple(int(value) for value in win32api.GetCursorPos())
                if drag_point is not None:
                    monitor = win32api.MonitorFromPoint(
                        tuple(int(value) for value in drag_point),
                        win32con.MONITOR_DEFAULTTONEAREST,
                    )
                elif self._hwnd:
                    monitor = win32api.MonitorFromWindow(
                        self._hwnd,
                        win32con.MONITOR_DEFAULTTONEAREST,
                    )
                else:
                    monitor = win32api.MonitorFromPoint(
                        tuple(int(value) for value in win32api.GetCursorPos()),
                        win32con.MONITOR_DEFAULTTONEAREST,
                    )
                info = win32api.GetMonitorInfo(monitor)
                return tuple(int(value) for value in info["Work"])
            except Exception as exc:
                self.logger.debug("[Sprite] work area query failed: {}", exc)
        return (0, 0, 1920, 1080)

    def _refresh_work_area(
        self,
        *,
        drag_point: tuple[int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        """Refresh work-area bounds for the current physical monitor."""

        self._work_area = self._get_work_area(drag_point=drag_point)
        return self._work_area

    def _initial_position(self) -> tuple[float, float]:
        margin = self.config.desktop_pet.margin
        if self._saved_state.x is not None and self._saved_state.y is not None:
            x, y = self._saved_state.x, self._saved_state.y
        else:
            x, y = initial_window_position(
                self.config.desktop_pet.start_position,
                self.width,
                self.height,
                self._work_area,
                margin,
            )
        x, y = clamp_window_position(x, y, self.width, self.height, self._work_area, margin)
        return float(x), float(y)

    def _move_pet_window(
        self,
        x: int | float,
        y: int | float,
        *,
        drag_point: tuple[int, int] | None = None,
    ) -> None:
        self._refresh_work_area(drag_point=drag_point)
        left, top, right, bottom = self._work_area
        margin = self.config.desktop_pet.margin
        minimum_x = float(left + margin)
        maximum_x = float(max(left + margin, right - self.width - margin))
        minimum_y = float(top + margin)
        maximum_y = float(max(top + margin, bottom - self.height - margin))
        logical_x = max(minimum_x, min(maximum_x, float(x)))
        logical_y = max(minimum_y, min(maximum_y, float(y)))
        self._position = (logical_x, logical_y)
        if self._hwnd and os.name == "nt":
            import win32con
            import win32gui

            win32gui.SetWindowPos(
                self._hwnd,
                0,
                round(logical_x),
                round(logical_y),
                0,
                0,
                win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )

    def _read_window_flag(self, name: str) -> bool:
        counter = self._input_counter
        if name == "game_input_available":
            return bool(counter is not None and counter.game_input_available)
        if name in {
            "game_input_pending",
            "game_input_active",
            "game_input_stopping",
            "game_input_failed",
        }:
            state = counter.game_mode_state if counter is not None else "off"
            return {
                "game_input_pending": state == "pending",
                "game_input_active": state == "active",
                "game_input_stopping": state == "stopping",
                "game_input_failed": state == "failed",
            }[name]
        return {
            "click_through": self._click_through,
            "always_on_top": self._always_on_top,
            "visible": self._visible,
        }.get(name, False)

    def _toggle_game_input(self) -> None:
        counter = self._input_counter
        if counter is None or not counter.game_input_available:
            return
        state = counter.game_mode_state
        if state in {"pending", "active"}:
            counter.disable_game_mode()
        elif state != "stopping":
            counter.enable_game_mode()
        self._refresh_game_input_menu()

    def _refresh_game_input_menu(self) -> None:
        counter = self._input_counter
        state = (
            bool(counter is not None and counter.game_input_available),
            counter.game_mode_state if counter is not None else "off",
        )
        if state == self._last_game_input_menu_state:
            return
        self._last_game_input_menu_state = state
        controller = self._desktop_controller
        tray = getattr(controller, "tray", None) if controller is not None else None
        if tray is not None:
            tray.refresh()

    def _toggle_visibility(self) -> None:
        self._visible = not self._visible
        self._apply_window_flags()
        self._save_state(force=True)

    def _toggle_click_through(self) -> None:
        self._click_through = not self._click_through
        self._apply_window_flags()
        self._save_state(force=True)

    def _toggle_topmost(self) -> None:
        self._always_on_top = not self._always_on_top
        self._apply_window_flags()
        self._save_state(force=True)

    def _reset_position(self) -> None:
        self._position = tuple(
            float(value)
            for value in initial_window_position(
                "bottom_right",
                self.width,
                self.height,
                self._get_work_area(),
                self.config.desktop_pet.margin,
            )
        )
        self._move_pet_window(round(self._position[0]), round(self._position[1]))
        self._save_state(force=True)

    def _quit(self) -> None:
        self.running = False

    def _sync_chat_anchor(self, chat: Any, force: bool) -> None:
        try:
            left, top = round(self._position[0]), round(self._position[1])
            pet_rect = (left, top, left + self.width, top + self.height)
            chat.sync_anchor(
                pet_rect,
                self._work_area,
                visible=self._visible,
                topmost=self._always_on_top,
                click_through=self._click_through,
                hovered=self._cursor_over_pet_window(),
                model_scale=1.0,
            )
            sync_output = getattr(chat, "set_output_speaking", None)
            if callable(sync_output):
                sync_output(self._audio_output_active())
        except Exception as exc:
            self.logger.debug("[Sprite] chat anchor sync failed: {}", exc)

    def _cursor_over_pet_window(self) -> bool:
        if not self._hwnd or not self._visible or self._click_through or os.name != "nt":
            return False
        try:
            import win32api
            import win32con
            import win32gui

            point = tuple(win32api.GetCursorPos())
            hit = int(win32gui.WindowFromPoint(point))
            root_hwnd = int(win32gui.GetAncestor(self._hwnd, win32con.GA_ROOT) or self._hwnd)
            hit_root = int(win32gui.GetAncestor(hit, win32con.GA_ROOT) or hit)
            return hit_root == root_hwnd or bool(win32gui.IsChild(root_hwnd, hit))
        except Exception as exc:
            self.logger.debug("[Sprite] chat hover hit-test unavailable: {}", exc)
            return False

    def _cursor_over_robot_face(self, face_rect: tuple[int, int, int, int]) -> bool:
        if not self._hwnd or not self._visible or os.name != "nt":
            return False
        try:
            import win32api

            cursor_x, cursor_y = win32api.GetCursorPos()
            window_left, window_top = round(self._position[0]), round(self._position[1])
            left, top, right, bottom = face_rect
            margin = max(4, round(self.width * 4 / 176))
            return (
                window_left + left - margin
                <= cursor_x
                < window_left + right + margin
                and window_top + top - margin
                <= cursor_y
                < window_top + bottom + margin
            )
        except Exception as exc:
            if not self._face_hover_error_logged:
                self.logger.debug("[Sprite] robot face hover check unavailable: {}", exc)
                self._face_hover_error_logged = True
            return False

    def _audio_output_active(self) -> bool:
        return bool(
            self._speaking
            or (self._wav_channel is not None and self._wav_channel.get_busy())
            or (self._pcm_player is not None and self._pcm_player.active)
        )

    def _save_state(self, *, force: bool = False) -> None:
        if not self.config.desktop_pet.enabled or not self.config.desktop_pet.remember_position:
            return
        now = time.monotonic()
        if not force and now - self._last_position_save_at < 1.0:
            return
        self._state_store.save(
            DesktopPetState(
                x=round(self._position[0]),
                y=round(self._position[1]),
                always_on_top=self._always_on_top,
                click_through=self._click_through,
                visible=self._visible,
            )
        )
        self._last_position_save_at = now

    def _shutdown(self) -> None:
        self.running = False
        if self._input_counter is not None:
            try:
                self._input_counter.stop()
            except Exception as exc:
                self.logger.warning("[Sprite] input counter shutdown failed: {}", exc)
            self._input_counter = None
        self._input_feedback = None
        self._last_observed_counter_total = None
        self._last_counter_input_at = None
        self._ordinary_face_epoch = None
        self._stop_audio()
        if self._pcm_player is not None:
            self._pcm_player.close()
            self._pcm_player = None
        if self._desktop_controller is not None:
            self._desktop_controller.shutdown()
            self._desktop_controller = None
        self._save_state(force=True)
        if self._native_window is not None:
            self._native_window.close()
            self._native_window = None
        if self._pygame is not None:
            self._pygame.quit()
            self._pygame = None
        self._clear_frame_caches()
        self._screen = None
        self._surface = None
        if self.context.on_exit is not None:
            self.context.on_exit()
