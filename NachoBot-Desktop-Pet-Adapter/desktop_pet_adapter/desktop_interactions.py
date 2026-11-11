"""Runtime policy for safe autonomous desktop-pet interactions."""

from __future__ import annotations

import ctypes
import hashlib
import json
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .config import DesktopGuiTarget, DesktopInteractionConfig
from .desktop_movement import (
    MovementPlan,
    fall_to_work_area,
    plan_journey,
    revalidate_plan,
    step_journey,
)
from .desktop_scene import SceneSnapshot, SceneUnavailable, SceneWindow
from .desktop_uia import load_saved_targets, merge_targets, save_targets
from .desktop_worker import DesktopInteractionWorker, DesktopWorkResult


@dataclass(frozen=True, slots=True)
class InteractionUpdate:
    position: tuple[float, float] | None = None
    motion: str | None = None
    attached: bool = False
    status: str = "idle"
    scene: SceneSnapshot | None = None

    @property
    def presentation_intent(self) -> str | None:
        """Semantic intent; ``motion`` remains as a legacy field alias."""

        return self.motion


class DesktopInteractionSession:
    """Autonomous movement policy and bounded native-action coordinator."""

    def __init__(
        self,
        config: DesktopInteractionConfig,
        *,
        own_hwnd: int | None,
        own_pid: int | None,
        logger: Any,
        worker: DesktopInteractionWorker | None = None,
        clock: Callable[[], float] = time.monotonic,
        choice: Callable[[list[Any]], Any] | None = None,
        allow_pet_displacement: bool = True,
    ) -> None:
        self.config = config
        self.logger = logger
        self._clock = clock
        self._choice = choice or random.choice
        self.allow_pet_displacement = bool(allow_pet_displacement)
        self.worker = worker or DesktopInteractionWorker(
            own_hwnd=own_hwnd,
            own_pid=own_pid,
            timeout_seconds=config.action_timeout_seconds,
        )
        self.enabled = config.enabled
        self.window_approach = config.window_approach
        self.icon_approach = config.icon_approach
        self.follow_foreground_window = config.follow_foreground_window
        self.move_windows = config.move_windows
        self.move_icons = config.move_icons
        self.gui_inspection = config.gui_inspection
        self.gui_actions = config.gui_actions
        self._targets_path = config.gui_targets_path
        self._gui_targets = merge_targets(
            config.gui_targets, load_saved_targets(config.gui_targets_path)
        )
        self._inspected_targets: tuple[DesktopGuiTarget, ...] = ()
        self._scene: SceneSnapshot | None = None
        self._last_external_foreground: SceneWindow | None = None
        self._plan: MovementPlan | None = None
        self._waypoint_index = 0
        self._plan_scene_signature: tuple[Any, ...] | None = None
        self._last_scene_at: float | None = None
        self._last_input_at = self._clock()
        self._last_update_at = self._last_input_at
        self._next_snapshot_at = 0.0
        self._next_decision_at = self._last_input_at + config.idle_interval_seconds
        self._target_cooldowns: dict[str, float] = {}
        self._reported_errors: set[tuple[str, str]] = set()
        self._last_presentation_intent: str | None = None
        self._started = False
        self._stopped = False
        self._last_position: tuple[float, float] | None = None
        self._attached = False
        self._menu_changed = False
        self._arrival_release_at: float | None = None
        self._user_active = False
        self._gate_state = "ready"
        self._manual_inspect_pending = False

    @property
    def scene(self) -> SceneSnapshot | None:
        return self._scene

    @property
    def allowed_gui_targets(self) -> tuple[DesktopGuiTarget, ...]:
        return self._gui_targets

    @property
    def movement_active(self) -> bool:
        """Whether an approach, follow, perch, or recovery plan owns movement."""

        return self.allow_pet_displacement and (self._plan is not None or self._attached)

    @property
    def inspected_gui_targets(self) -> tuple[DesktopGuiTarget, ...]:
        return self._inspected_targets

    @property
    def attached(self) -> bool:
        return self._attached

    def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        self.worker.start()

    def note_user_input(self, *, pet_dragging: bool = False) -> None:
        """Invalidate queued actions; retain a foreground-follow pose when safe."""

        now = self._clock()
        self._last_input_at = now
        self._user_active = True
        self._next_decision_at = max(
            self._next_decision_at, now + self.config.idle_interval_seconds
        )
        self.worker.invalidate()
        if pet_dragging:
            self._plan = None
            self._waypoint_index = 0
            self._plan_scene_signature = None
            self._attached = False

    def get_state(self, name: str) -> bool:
        values = {
            "interaction_enabled": self.enabled,
            "window_approach": self.allow_pet_displacement and self.window_approach,
            "icon_approach": self.allow_pet_displacement and self.icon_approach,
            "follow_foreground_window": self.allow_pet_displacement and self.follow_foreground_window,
            "move_windows": self.move_windows,
            "move_icons": self.move_icons,
            "gui_inspection": self.gui_inspection,
            "gui_actions": self.gui_actions,
        }
        if name in values:
            return bool(values[name])
        if name.startswith("gui_target:"):
            return any(self._target_state_key(target) == name for target in self._gui_targets)
        return False

    def handle_command(self, command: str, value: Any = None) -> bool:
        toggles = {
            "desktop_toggle_interactions": ("enabled", "interaction_enabled"),
            "desktop_toggle_window_approach": ("window_approach", "window_approach"),
            "desktop_toggle_icon_approach": ("icon_approach", "icon_approach"),
            "desktop_toggle_follow_window": (
                "follow_foreground_window",
                "follow_foreground_window",
            ),
            "desktop_toggle_window_moves": ("move_windows", "move_windows"),
            "desktop_toggle_icon_moves": ("move_icons", "move_icons"),
            "desktop_toggle_gui_inspection": ("gui_inspection", "gui_inspection"),
            "desktop_toggle_gui_actions": ("gui_actions", "gui_actions"),
        }
        toggle = toggles.get(command)
        if toggle is not None:
            if not self.allow_pet_displacement and command in {
                "desktop_toggle_window_approach",
                "desktop_toggle_icon_approach",
                "desktop_toggle_follow_window",
            }:
                return True
            attr, _state = toggle
            setattr(self, attr, not bool(getattr(self, attr)))
            self.worker.invalidate()
            if command == "desktop_toggle_window_approach" and not self.window_approach:
                if self._plan is not None and self._plan.target_kind == "window":
                    self._clear_plan()
            if command == "desktop_toggle_icon_approach" and not self.icon_approach:
                if self._plan is not None and self._plan.target_kind == "icon":
                    self._clear_plan()
            return True
        if command == "desktop_gui_inspect":
            target = self._last_external_foreground
            if self.gui_inspection and target is not None:
                job_id = self.worker.submit(
                    "inspect_gui",
                    payload=(
                        target.hwnd,
                        target.pid,
                        target.process_name,
                        target.title,
                    ),
                    timeout_seconds=self.config.action_timeout_seconds,
                    survive_invalidation=True,
                )
                self._manual_inspect_pending = job_id is not None
                return job_id is not None
            return False
        if command == "desktop_toggle_gui_target" and isinstance(value, DesktopGuiTarget):
            key = self._target_state_key(value)
            if any(self._target_state_key(target) == key for target in self._gui_targets):
                self._gui_targets = tuple(
                    target for target in self._gui_targets if self._target_state_key(target) != key
                )
            else:
                self._gui_targets = merge_targets(self._gui_targets, (value,))
            save_targets(self._targets_path, self._gui_targets)
            self._menu_changed = True
            return True
        if command == "desktop_restore_interactions":
            return self.worker.submit("restore") is not None
        return False

    def update(
        self,
        *,
        position: tuple[float, float],
        viewport: tuple[int, int],
        work_area: tuple[int, int, int, int],
        delta_time: float,
        visible: bool,
        pet_dragging: bool,
        foot_anchor: tuple[float, float] | None = None,
    ) -> InteractionUpdate:
        if self._stopped:
            return InteractionUpdate(position=position, status="stopped", scene=self._scene)
        self.start()
        effective_foot_anchor = foot_anchor or (
            self.config.foot_anchor_x,
            self.config.foot_anchor_y,
        )
        now = self._clock()
        allow_stale = (
            {"snapshot"}
            | (
                {"inspect_gui"}
                if self._manual_inspect_pending and visible and not pet_dragging
                else set()
            )
        )
        results = self.worker.poll(now=now, allow_stale_kinds=allow_stale)
        if not visible or pet_dragging:
            self._manual_inspect_pending = False
        for result in results:
            self._process_result(result, now)
        if now >= self._next_snapshot_at:
            self.worker.submit(
                "snapshot",
                timeout_seconds=self.config.action_timeout_seconds,
                survive_invalidation=True,
            )
            self._next_snapshot_at = now + self.config.scene_interval_seconds

        self._last_position = position
        if not self.allow_pet_displacement and self._plan is not None:
            self._clear_plan()
        if not visible:
            self._invalidate_gate("hidden")
            self._clear_plan()
            return InteractionUpdate(position=position, status="hidden", scene=self._scene)
        if pet_dragging:
            self._invalidate_gate("pet-drag")
            self._clear_plan()
            return InteractionUpdate(position=position, status="pet-drag", scene=self._scene)
        scene = self._scene
        if scene is None:
            return InteractionUpdate(position=position, status="sampling", scene=None)
        if scene.fullscreen:
            self._invalidate_gate("fullscreen")
            return InteractionUpdate(position=position, status="fullscreen", scene=scene)
        if self._scene_age(now) > self._max_scene_age():
            return InteractionUpdate(position=position, status="scene-stale", scene=scene)

        pet_input_idle = now - self._last_input_at
        system_input_idle = _system_idle_seconds()
        if system_input_idle is None:
            system_input_idle = max(0.0, scene.system_idle_seconds)
        user_active = min(pet_input_idle, system_input_idle) < self.config.idle_interval_seconds
        if user_active and not self._user_active:
            self.worker.invalidate()
        self._user_active = user_active
        if not user_active:
            self._gate_state = "ready"
        if not self.enabled:
            return InteractionUpdate(
                position=position,
                attached=self._attached,
                status="paused",
                scene=scene,
            )
        reactive_follow = self._is_reactive_follow(scene)

        if self._plan is not None:
            if self._scene_age(now) > self._max_scene_age():
                return InteractionUpdate(
                    position=position,
                    attached=self._attached,
                    status="scene-stale",
                    scene=scene,
                )
            # Existing pet-only journeys continue during desktop input. New
            # journeys and native actions remain behind the idle/cancellation
            # gates below and in the worker.
            self._refresh_plan_if_scene_changed(
                scene, position, viewport, work_area, effective_foot_anchor
            )
            plan = self._plan
            if plan is None:
                return InteractionUpdate(position=position, status="recovering", scene=scene)
            if plan.target_kind == "recovery":
                self._arrival_release_at = None
            if self._arrival_release_at is not None:
                if now < self._arrival_release_at or user_active:
                    if self._attached and plan.goal != position:
                        current = position
                        next_position, self._waypoint_index, arrived = step_journey(
                            plan,
                            current,
                            delta_time,
                            waypoint_index=self._waypoint_index,
                        )
                        status = (
                            "following" if plan.target_kind == "window" else "climbing"
                        )
                        if arrived:
                            status = "perched"
                        return InteractionUpdate(
                            position=next_position,
                            attached=True,
                            status=status,
                            scene=scene,
                        )
                    return InteractionUpdate(
                        position=position,
                        attached=self._attached,
                        status="perched",
                        scene=scene,
                    )
                self._clear_plan()
                self._next_decision_at = now
            if self._plan is not None:
                plan = self._plan
                current = position
                next_position, self._waypoint_index, arrived = step_journey(
                    plan,
                    current,
                    delta_time,
                    waypoint_index=self._waypoint_index,
                )
                status = "falling" if plan.target_kind == "recovery" else (
                    "following" if reactive_follow else "climbing"
                )
                motion = (
                    plan.presentation_intent
                    if plan.presentation_intent != self._last_presentation_intent
                    and next_position != current
                    else None
                )
                if motion is not None:
                    self._last_presentation_intent = motion
                if arrived:
                    if plan.target_kind == "recovery":
                        self._clear_plan()
                        self._next_decision_at = now + self.config.idle_interval_seconds
                        status = "recovered"
                    else:
                        self._attached = plan.attached
                        if self._arrival_release_at is None:
                            self._arrival_release_at = now + min(
                                20.0, max(5.0, self.config.idle_interval_seconds / 3)
                            )
                        self._next_decision_at = self._arrival_release_at
                        status = "perched"
                return InteractionUpdate(
                    position=next_position,
                    motion=motion,
                    attached=self._attached,
                    status=status,
                    scene=scene,
                )

        if user_active or now < self._next_decision_at:
            return InteractionUpdate(
                position=position,
                attached=False,
                status="paused" if not self.enabled else "idle",
                scene=scene,
            )
        self._choose_action(scene, position, viewport, work_area, now, effective_foot_anchor)
        if self._plan is not None:
            return InteractionUpdate(
                position=position,
                motion=self._plan.presentation_intent,
                status="approaching",
                scene=scene,
            )
        return InteractionUpdate(position=position, status="idle", scene=scene)

    def shutdown(self, *, restore_timeout: float = 0.65) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._started and self.worker.alive:
            self.worker.submit("restore", timeout_seconds=restore_timeout)
            deadline = self._clock() + max(0.0, restore_timeout)
            while self._clock() < deadline:
                results = self.worker.poll()
                if any(result.kind == "restore" for result in results):
                    break
                time.sleep(0.01)
        self.worker.stop(join_timeout=0.2)

    def consume_menu_changed(self) -> bool:
        changed = self._menu_changed
        self._menu_changed = False
        return changed

    def _process_result(self, result: DesktopWorkResult, now: float) -> None:
        if result.kind == "snapshot":
            if result.error:
                self._report_error(result.kind, result.error)
                return
            if isinstance(result.value, SceneUnavailable):
                self._report_error(result.kind, result.value.detail)
                return
            if isinstance(result.value, SceneSnapshot):
                self._remember_external_foreground(result.value)
                self._scene = result.value
                return
        elif result.kind == "inspect_gui":
            self._manual_inspect_pending = False
            if result.error:
                self._report_error(result.kind, result.error)
                return
            self._inspected_targets = tuple(result.value or ())
            self._menu_changed = True
            self.logger.info(
                "[DesktopPet] UIA scan found {} safe controls in the sampled external window",
                len(self._inspected_targets),
            )
        elif result.kind in {"move_window", "move_icon", "gui_action"}:
            if result.error:
                self._report_error(result.kind, result.error)
            if result.kind == "gui_action":
                # The queued payload is not returned by the worker, so cooldown
                # keys are added when scheduling the action.
                if result.value is True:
                    self.logger.info("[DesktopPet] UIA allowlisted control action completed")
            elif result.value is not None:
                self.logger.info("[DesktopPet] {} completed", result.kind)
        elif result.kind == "restore":
            if result.error:
                self._report_error(result.kind, result.error)
            elif isinstance(result.value, tuple):
                self.logger.info(
                    "[DesktopPet] restored {} windows and {} desktop icons",
                    result.value[0],
                    result.value[1],
                )

    def _remember_external_foreground(self, scene: SceneSnapshot) -> None:
        """Keep the latest sampled external foreground despite tray-menu focus."""

        if scene.foreground_hwnd is None:
            return
        target = next(
            (window for window in scene.windows if window.hwnd == scene.foreground_hwnd),
            None,
        )
        if target is None:
            # The tray can become foreground while its menu is open. Since the
            # provider excludes our PID/HWND from scene.windows, retain the last
            # external target instead of replacing it with our own tray window.
            return
        own_pid = getattr(self.worker, "own_pid", None)
        own_hwnd = getattr(self.worker, "own_hwnd", None)
        if (
            not target.title
            or not target.process_name
            or (own_pid is not None and target.pid == own_pid)
            or (own_hwnd is not None and target.hwnd == own_hwnd)
        ):
            return
        self._last_external_foreground = target

    def _report_error(self, kind: str, message: str) -> None:
        key = (kind, message[:160])
        if key not in self._reported_errors:
            self._reported_errors.add(key)
            self.logger.warning("[DesktopPet] {} unavailable: {}", kind, message[:240])

    def _clear_plan(self) -> None:
        self._plan = None
        self._waypoint_index = 0
        self._plan_scene_signature = None
        self._attached = False
        self._last_presentation_intent = None
        self._arrival_release_at = None

    def _invalidate_gate(self, state: str) -> None:
        if self._gate_state != state:
            self.worker.invalidate()
            self._gate_state = state

    def _scene_age(self, now: float) -> float:
        if self._scene is None:
            return float("inf")
        return max(0.0, now - self._scene.captured_at)

    def _max_scene_age(self) -> float:
        return max(
            3.0,
            self.config.scene_interval_seconds * 3 + self.config.action_timeout_seconds,
        )

    def _is_reactive_follow(self, scene: SceneSnapshot) -> bool:
        if not self.follow_foreground_window or self._plan is None:
            return False
        if self._plan.target_kind != "window":
            return False
        try:
            return int(self._plan.target_identity) == int(scene.foreground_hwnd or 0)
        except (TypeError, ValueError):
            return False

    def _refresh_plan_if_scene_changed(
        self,
        scene: SceneSnapshot,
        position: tuple[float, float],
        viewport: tuple[int, int],
        work_area: tuple[int, int, int, int],
        foot_anchor: tuple[float, float],
    ) -> None:
        plan = self._plan
        if plan is None or plan.target_kind == "recovery":
            return
        signature = self._target_signature(scene, plan)
        if signature is None:
            if self._last_scene_at != scene.captured_at:
                self._plan = fall_to_work_area(position, viewport, work_area)
                self._waypoint_index = 0
                self._attached = False
                self._plan_scene_signature = None
                self._arrival_release_at = None
                self.logger.info("[DesktopPet] perch target disappeared; recovering")
            self._last_scene_at = scene.captured_at
            return
        if signature == self._plan_scene_signature:
            self._last_scene_at = scene.captured_at
            return
        refreshed = revalidate_plan(
            plan,
            scene,
            position,
            viewport,
            work_area,
            foot_anchor,
        )
        self._last_scene_at = scene.captured_at
        self._plan_scene_signature = signature
        if refreshed is None:
            self._plan = fall_to_work_area(position, viewport, work_area)
            self._waypoint_index = 0
            self._attached = False
            self._arrival_release_at = None
            self.logger.info("[DesktopPet] perch is no longer visible; recovering")
            return
        if self._attached:
            refreshed = replace(refreshed, waypoints=(refreshed.goal,))
        self._plan = refreshed
        self._waypoint_index = 0

    def _target_signature(
        self, scene: SceneSnapshot, plan: MovementPlan
    ) -> tuple[Any, ...] | None:
        if plan.target_kind == "window":
            try:
                identity = int(plan.target_identity)
            except (TypeError, ValueError):
                return None
            target = next((window for window in scene.windows if window.hwnd == identity), None)
            if target is None:
                return None
            occluders = tuple(
                (window.hwnd, window.rect, window.z_order)
                for window in scene.windows
                if window.z_order < target.z_order
            )
            return target.rect, target.z_order, occluders
        if plan.target_kind == "icon":
            target = next(
                (icon for icon in scene.icons if icon.identity == plan.target_identity),
                None,
            )
            return (target.x, target.y) if target is not None else None
        return None

    def _choose_action(
        self,
        scene: SceneSnapshot,
        position: tuple[float, float],
        viewport: tuple[int, int],
        work_area: tuple[int, int, int, int],
        now: float,
        foot_anchor: tuple[float, float],
    ) -> None:
        choices: list[tuple[str, Any, str]] = []
        if self.allow_pet_displacement and (self.window_approach or self.icon_approach):
            plan = plan_journey(
                scene,
                position,
                viewport,
                work_area,
                foot_anchor,
                allow_windows=self.window_approach,
                allow_icons=self.icon_approach,
                follow_foreground=self.follow_foreground_window,
            )
            if plan is not None:
                choices.append(("approach", plan, plan.target_identity))
        if self.move_windows:
            for window in scene.windows:
                if not window.movable:
                    continue
                key = f"window:{window.hwnd}"
                if self._target_cooldowns.get(key, 0.0) <= now:
                    choices.append(("move_window", window, key))
        if self.move_icons:
            for icon in scene.icons:
                if icon.name.casefold() in {"recycle bin", "回收站"}:
                    continue
                key = f"icon:{icon.identity}"
                if self._target_cooldowns.get(key, 0.0) <= now:
                    choices.append(("move_icon", icon, key))
        if self.gui_actions and self._gui_targets:
            foreground = next(
                (window for window in scene.windows if window.hwnd == scene.foreground_hwnd),
                None,
            )
            if foreground is not None:
                for target in self._gui_targets:
                    key = self._target_state_key(target)
                    if (
                        target.process_name.casefold() == foreground.process_name.casefold()
                        and target.window_title.casefold() == foreground.title.casefold()
                        and self._target_cooldowns.get(key, 0.0) <= now
                    ):
                        choices.append(("gui_action", target, key))
        if not choices:
            self._next_decision_at = now + self.config.idle_interval_seconds
            return
        kind, target, key = self._choice(choices)
        if kind == "approach":
            self._plan = target
            self._waypoint_index = 0
            self._attached = False
            self._plan_scene_signature = self._target_signature(scene, target)
            self._last_scene_at = scene.captured_at
            self._next_decision_at = now + self.config.idle_interval_seconds
            self.logger.info(
                "[DesktopPet] starting {} path toward {}",
                target.target_kind,
                target.target_identity[:32],
            )
            return
        if kind == "move_window":
            delta = self._small_delta(self.config.max_window_shift_px)
            payload = {
                "hwnd": target.hwnd,
                "pid": target.pid,
                "process_name": target.process_name,
                "title": target.title,
                "expected_rect": target.rect,
                "dx": delta[0],
                "dy": delta[1],
                "max_delta_px": self.config.max_window_shift_px,
            }
        elif kind == "move_icon":
            delta = self._small_delta(self.config.max_icon_shift_px)
            payload = {
                "identity": target.identity,
                "expected_name": target.name,
                "x": target.x + delta[0],
                "y": target.y + delta[1],
                "max_delta_px": self.config.max_icon_shift_px,
            }
        else:
            payload = target
        job_id = self.worker.submit(
            kind,
            payload,
            timeout_seconds=self.config.action_timeout_seconds,
        )
        if job_id is not None:
            self._target_cooldowns[key] = now + self.config.target_cooldown_seconds
            self._next_decision_at = now + self.config.idle_interval_seconds
            self.logger.info("[DesktopPet] requested {}", kind)

    @staticmethod
    def _small_delta(maximum: int) -> tuple[int, int]:
        dx = random.randint(-maximum, maximum)
        dy = random.randint(-maximum, maximum)
        if dx == 0 and dy == 0:
            dx = maximum
        return dx, dy

    @staticmethod
    def _target_state_key(target: DesktopGuiTarget) -> str:
        payload = {
            "process_name": target.process_name,
            "window_title": target.window_title,
            "control_name": target.control_name,
            "control_type": target.control_type,
            "action": target.action,
            "automation_id": target.automation_id,
        }
        value = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return "gui_target:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _system_idle_seconds() -> float | None:
    """Read Windows last-input time so activity outside the pet pauses actions."""

    if sys.platform != "win32":
        return None
    try:
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_last_input = user32.GetLastInputInfo
        get_last_input.argtypes = [ctypes.POINTER(LastInputInfo)]
        get_last_input.restype = ctypes.c_int
        info = LastInputInfo(ctypes.sizeof(LastInputInfo), 0)
        if not get_last_input(ctypes.byref(info)):
            return None
        get_tick_count = kernel32.GetTickCount64
        get_tick_count.argtypes = []
        get_tick_count.restype = ctypes.c_uint64
        elapsed_ms = (int(get_tick_count()) - int(info.dwTime)) & 0xFFFFFFFF
        return max(0.0, elapsed_ms / 1000.0)
    except Exception:
        return None
