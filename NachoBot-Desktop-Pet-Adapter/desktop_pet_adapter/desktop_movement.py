"""Pure screen-space planning for approaching, perching, and recovery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

Point = tuple[float, float]
Rect = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class MovementPlan:
    target_kind: str
    target_identity: str
    waypoints: tuple[Point, ...]
    goal: Point
    attached: bool
    motion: str

    @property
    def presentation_intent(self) -> str:
        """Semantic intent; ``motion`` remains as a legacy field name."""

        return self.motion


@dataclass(frozen=True, slots=True)
class MovementStep:
    position: Point
    state: str
    arrived: bool
    attached: bool
    target_kind: str | None
    target_identity: str | None


@dataclass(frozen=True, slots=True)
class MovementGeometry:
    """Conservative placement geometry used by the path planner.

    The current Live2D backend supplies its full viewport because it has no
    measured silhouette bounds. The foot anchor is a normalized viewport point.
    """

    viewport: tuple[int, int]
    foot_anchor: tuple[float, float] = (0.5, 0.9)
    bounds_source: str = "viewport_fallback"


def resolve_presentation_intent(
    intent: str,
    mappings: dict[str, str],
    resolve_group: Any,
) -> str | None:
    """Return a model group only when that intent has an explicit valid mapping."""

    group = str(mappings.get(intent, "")).strip()
    if not group:
        return None
    return resolve_group(group)


def _clamp_origin(point: Point, viewport: tuple[int, int], work_area: Rect) -> Point:
    left, top, right, bottom = work_area
    width, height = viewport
    return (
        max(float(left), min(float(right - width), point[0])),
        max(float(top), min(float(bottom - height), point[1])),
    )


def _subtract_occlusion(
    left: float,
    right: float,
    y: float,
    occluders: tuple[Any, ...],
) -> list[tuple[float, float]]:
    segments = [(left, right)]
    for window in occluders:
        r_left, r_top, r_right, r_bottom = window.rect
        if r_top <= y < r_bottom:
            cut_left, cut_right = max(left, r_left), min(right, r_right)
            if cut_left >= cut_right:
                continue
            next_segments: list[tuple[float, float]] = []
            for segment_left, segment_right in segments:
                if cut_right <= segment_left or cut_left >= segment_right:
                    next_segments.append((segment_left, segment_right))
                else:
                    if cut_left > segment_left:
                        next_segments.append((segment_left, cut_left))
                    if cut_right < segment_right:
                        next_segments.append((cut_right, segment_right))
            segments = next_segments
    return segments


def _subtract_vertical_occlusion(
    top: float,
    bottom: float,
    x: float,
    occluders: tuple[Any, ...],
) -> list[tuple[float, float]]:
    segments = [(top, bottom)]
    for window in occluders:
        left, _top, right, _bottom = window.rect
        if left <= x < right:
            cut_top, cut_bottom = max(top, window.rect[1]), min(bottom, window.rect[3])
            if cut_top >= cut_bottom:
                continue
            next_segments: list[tuple[float, float]] = []
            for segment_top, segment_bottom in segments:
                if cut_bottom <= segment_top or cut_top >= segment_bottom:
                    next_segments.append((segment_top, segment_bottom))
                else:
                    if cut_top > segment_top:
                        next_segments.append((segment_top, cut_top))
                    if cut_bottom < segment_bottom:
                        next_segments.append((cut_bottom, segment_bottom))
            segments = next_segments
    return segments


def _window_lookup(snapshot: Any, identity: str) -> Any | None:
    try:
        hwnd = int(identity)
    except (TypeError, ValueError):
        return None
    return next((window for window in snapshot.windows if int(window.hwnd) == hwnd), None)


def _icon_lookup(snapshot: Any, identity: str) -> Any | None:
    return next((icon for icon in snapshot.icons if icon.identity == identity), None)


def _window_plan(
    window: Any,
    snapshot: Any,
    current: Point,
    viewport: tuple[int, int],
    work_area: Rect,
    foot_anchor: tuple[float, float],
) -> MovementPlan | None:
    left, top, right, bottom = window.rect
    width, height = viewport
    anchor_x, anchor_y = foot_anchor
    if right - left < width or bottom - top < max(120, height // 3):
        return None
    occluders = tuple(
        candidate
        for candidate in snapshot.windows
        if candidate.z_order < window.z_order
    )
    area_left, area_top, area_right, area_bottom = work_area
    reachable_x = (
        area_left + anchor_x * width,
        area_right - (1.0 - anchor_x) * width,
    )
    reachable_y = (
        area_top + anchor_y * height,
        area_bottom - (1.0 - anchor_y) * height,
    )
    top_segments = _subtract_occlusion(left, right, top, occluders)
    perch_segments = [
        segment
        for segment in top_segments
        if segment[1] - segment[0] >= width
        and reachable_y[0] - 2 <= top <= reachable_y[1] + 2
    ]
    top_goal: Point | None = None
    if perch_segments:
        # Favor the broadest top segment and clamp the foot inside the portion
        # where the full viewport fits both on screen and above the window.
        segment = max(perch_segments, key=lambda item: item[1] - item[0])
        fit_left = max(segment[0] + width / 2, reachable_x[0])
        fit_right = min(segment[1] - width / 2, reachable_x[1])
        if fit_left <= fit_right:
            foot_x = (fit_left + fit_right) / 2
            raw_goal = (foot_x - anchor_x * width, top - anchor_y * height)
            top_goal = _clamp_origin(raw_goal, viewport, work_area)
            actual_foot = (
                top_goal[0] + anchor_x * width,
                top_goal[1] + anchor_y * height,
            )
            if abs(actual_foot[0] - foot_x) > 2.0 or abs(actual_foot[1] - top) > 2.0:
                top_goal = None

    # Choose a side edge that is visible continuously from the window top.
    # If the top is too high for the complete viewport to fit, stop at the
    # highest screen-reachable point on that side and perch there instead.
    # A lower segment disconnected by an occluding window is not a climb route.
    side_options: list[tuple[float, Point, Point]] = []
    for side_x in (float(left), float(right)):
        if not reachable_x[0] - 2 <= side_x <= reachable_x[1] + 2:
            continue
        segments = _subtract_vertical_occlusion(top, bottom, side_x, occluders)
        top_connected = [
            segment for segment in segments if segment[0] <= top + 2
        ]
        if not top_connected:
            continue
        visible_top, visible_bottom = max(
            top_connected, key=lambda item: item[1] - item[0]
        )
        segment_top = max(visible_top, reachable_y[0])
        segment_bottom = min(visible_bottom, reachable_y[1])
        if segment_bottom - segment_top < 100:
            continue
        entry_foot = (side_x, segment_bottom)
        entry_origin = _clamp_origin(
            (side_x - anchor_x * width, segment_bottom - anchor_y * height),
            viewport,
            work_area,
        )
        entry_actual = (
            entry_origin[0] + anchor_x * width,
            entry_origin[1] + anchor_y * height,
        )
        if (
            abs(entry_actual[0] - entry_foot[0]) > 2
            or abs(entry_actual[1] - entry_foot[1]) > 2
        ):
            continue
        side_foot_y = top if top_goal is not None else segment_top
        side_origin = _clamp_origin(
            (side_x - anchor_x * width, side_foot_y - anchor_y * height),
            viewport,
            work_area,
        )
        side_actual = (
            side_origin[0] + anchor_x * width,
            side_origin[1] + anchor_y * height,
        )
        if (
            abs(side_actual[0] - side_x) > 2
            or abs(side_actual[1] - side_foot_y) > 2
        ):
            continue
        cost = math.dist(current, entry_origin)
        side_options.append((cost, entry_origin, side_origin))
    if side_options:
        _, entry, side_top = min(side_options, key=lambda item: item[0])
        path = (entry, side_top) + ((top_goal,) if top_goal is not None else ())
        goal = top_goal or side_top
        return MovementPlan(
            target_kind="window",
            target_identity=str(int(window.hwnd)),
            waypoints=path,
            goal=goal,
            attached=True,
            motion="approach_window",
        )
    if top_goal is None:
        return None
    return MovementPlan(
        target_kind="window",
        target_identity=str(int(window.hwnd)),
        waypoints=(top_goal,),
        goal=top_goal,
        attached=True,
        motion="approach_window",
    )


def plan_journey(
    snapshot: Any,
    current: Point,
    viewport: tuple[int, int],
    work_area: Rect,
    foot_anchor: tuple[float, float] = (0.5, 0.9),
    *,
    allow_windows: bool = True,
    allow_icons: bool = True,
    follow_foreground: bool = True,
    previous_target: str | None = None,
) -> MovementPlan | None:
    """Plan a path to a visible window top edge or a safe desktop icon."""

    if allow_windows:
        candidates = list(snapshot.windows)
        if follow_foreground and snapshot.foreground_hwnd:
            candidates.sort(
                key=lambda window: (
                    window.hwnd != snapshot.foreground_hwnd,
                    window.z_order,
                )
            )
        else:
            candidates.sort(key=lambda window: window.z_order)
        if previous_target and len(candidates) > 1:
            candidates.sort(key=lambda window: str(window.hwnd) == previous_target)
        for window in candidates:
            plan = _window_plan(
                window,
                snapshot,
                current,
                viewport,
                work_area,
                foot_anchor,
            )
            if plan is not None:
                return plan
    if allow_icons:
        for icon in snapshot.icons:
            if not icon.name or icon.name.casefold() in {"recycle bin", "回收站"}:
                continue
            goal = _clamp_origin(
                (
                    float(icon.x) - foot_anchor[0] * viewport[0],
                    float(icon.y + 38) - foot_anchor[1] * viewport[1],
                ),
                viewport,
                work_area,
            )
            return MovementPlan(
                target_kind="icon",
                target_identity=icon.identity,
                waypoints=(goal,),
                goal=goal,
                attached=True,
                motion="approach_icon",
            )
    return None


def revalidate_plan(
    plan: MovementPlan,
    snapshot: Any,
    current: Point,
    viewport: tuple[int, int],
    work_area: Rect,
    foot_anchor: tuple[float, float],
) -> MovementPlan | None:
    """Refresh a moving target or return None when the target disappeared."""

    if plan.target_kind == "window":
        window = _window_lookup(snapshot, plan.target_identity)
        return (
            _window_plan(window, snapshot, current, viewport, work_area, foot_anchor)
            if window is not None
            else None
        )
    if plan.target_kind == "icon":
        icon = _icon_lookup(snapshot, plan.target_identity)
        if icon is None:
            return None
        goal = _clamp_origin(
            (
                float(icon.x) - foot_anchor[0] * viewport[0],
                float(icon.y + 38) - foot_anchor[1] * viewport[1],
            ),
            viewport,
            work_area,
        )
        return MovementPlan(
            plan.target_kind,
            plan.target_identity,
            (goal,),
            goal,
            True,
            plan.presentation_intent,
        )
    return None


def step_journey(
    plan: MovementPlan,
    current: Point,
    delta_time: float,
    *,
    speed_px_per_second: float = 240.0,
    waypoint_index: int = 0,
) -> tuple[Point, int, bool]:
    """Advance toward the next waypoint without overshoot."""

    if waypoint_index >= len(plan.waypoints):
        return current, waypoint_index, True
    target = plan.waypoints[waypoint_index]
    distance = math.dist(current, target)
    maximum_step = max(0.0, min(float(speed_px_per_second), 900.0)) * max(
        0.0, min(float(delta_time), 0.1)
    )
    if distance <= max(1.0, maximum_step):
        next_index = waypoint_index + 1
        return target, next_index, next_index >= len(plan.waypoints)
    ratio = maximum_step / distance
    return (
        current[0] + (target[0] - current[0]) * ratio,
        current[1] + (target[1] - current[1]) * ratio,
    ), waypoint_index, False


def fall_to_work_area(
    current: Point,
    viewport: tuple[int, int],
    work_area: Rect,
) -> MovementPlan:
    goal = _clamp_origin((current[0], float(work_area[3] - viewport[1])), viewport, work_area)
    return MovementPlan("recovery", "", (goal,), goal, False, "recover")
