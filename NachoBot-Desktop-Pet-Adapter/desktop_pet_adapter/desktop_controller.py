"""Shared desktop-pet interaction, chat, tray, and command lifecycle."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .chat import DesktopPetChat
from .config import DesktopPetConfig
from .desktop_interactions import DesktopInteractionSession
from .desktop_movement import MovementGeometry
from .desktop_pet import clamp_window_position
from .tray import DesktopPetTray


@dataclass(frozen=True, slots=True)
class DesktopPetFrame:
    position: tuple[float, float]
    geometry: MovementGeometry
    work_area: tuple[int, int, int, int]
    visible: bool
    pet_dragging: bool
    motion_busy: bool = False


@dataclass(frozen=True, slots=True)
class DesktopPetControllerHost:
    """Explicit renderer callbacks used by the backend-neutral controller."""

    enqueue_command: Callable[[str, Any], None]
    sample_frame: Callable[[], DesktopPetFrame | None]
    move_window: Callable[[int, int], None]
    present_movement: Callable[[str], bool]
    start_motion: Callable[[str, int], bool]
    read_window_flag: Callable[[str], bool]
    sync_chat_anchor: Callable[[DesktopPetChat, bool], None]
    open_parameters: Callable[[], None]
    set_parameter: Callable[[dict[str, Any]], None]
    reset_parameters: Callable[[str | None], None]
    toggle_visibility: Callable[[], None]
    toggle_click_through: Callable[[], None]
    toggle_topmost: Callable[[], None]
    reset_position: Callable[[], None]
    quit: Callable[[], None]
    report_click: Callable[[int], None]
    parameter_panel_factory: Callable[..., Any] | None = None


class DesktopPetController:
    """Own the reusable desktop-pet behavior independently of a renderer."""

    def __init__(
        self,
        config: DesktopPetConfig,
        logger: Any,
        host: DesktopPetControllerHost,
        *,
        own_hwnd: int | None = None,
        own_pid: int | None = None,
        chat: DesktopPetChat | None = None,
        tray: DesktopPetTray | None = None,
        interactions: DesktopInteractionSession | None = None,
        clock: Callable[[], float] = time.monotonic,
        choice: Callable[[list[Any]], Any] = random.choice,
        autonomous_displacement: bool = False,
    ) -> None:
        self.config = config
        self.logger = logger
        self.host = host
        self._clock = clock
        self._choice = choice
        # Window movement is a backend capability, never a user-controlled
        # interaction setting. A renderer must opt in explicitly (sprite mode);
        # toggles and protocol commands cannot grant it to Live2D.
        self.autonomous_displacement = bool(autonomous_displacement)
        self._movement_status = "idle"
        self.interactions = interactions
        if config.enabled and self.interactions is None:
            self.interactions = DesktopInteractionSession(
                config.interactions,
                own_hwnd=own_hwnd,
                own_pid=own_pid,
                logger=logger,
                allow_pet_displacement=self.autonomous_displacement,
            )
        elif self.interactions is not None and hasattr(
            self.interactions, "allow_pet_displacement"
        ):
            self.interactions.allow_pet_displacement = self.autonomous_displacement
        self.chat = chat or (
            DesktopPetChat(
                config.chat,
                config.title,
                host.enqueue_command,
                logger,
                character_name=config.character_name,
                chat_header=config.chat_header,
                parameter_panel_factory=host.parameter_panel_factory,
            )
            if config.enabled
            else None
        )
        self.tray = tray
        self._started = False
        self._last_pet_click_at = 0.0
        self._last_interaction_at = clock()
        self._next_idle_motion_at = 0.0
        self._last_chat_sync_at = 0.0

    @property
    def desktop_interactions(self) -> DesktopInteractionSession | None:
        return self.interactions

    @property
    def interaction_movement_active(self) -> bool:
        return bool(
            self._movement_status in {
                "approaching",
                "climbing",
                "following",
                "falling",
                "recovering",
                "perched",
            }
            or (self.interactions is not None and self.interactions.movement_active)
        )

    def start(self) -> None:
        if self._started or not self.config.enabled:
            return
        self._started = True
        if self.interactions is not None:
            self.interactions.start()
        self._schedule_next_idle_motion()
        if self.tray is None:
            self.tray = DesktopPetTray(
                self.config.title,
                self.host.enqueue_command,
                self.menu_flag,
                self.logger,
                character_name=self.config.character_name,
            )
        self.tray.start(visible=self.config.tray_icon)
        self._refresh_tray_targets()

    def start_chat_host(self) -> None:
        if self.chat is not None:
            self.chat.start()

    def shutdown(self) -> None:
        if self.interactions is not None:
            self.interactions.shutdown()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.chat is not None:
            self.chat.stop()
        self._started = False

    def note_user_input(self, *, pet_dragging: bool = False) -> None:
        self._last_interaction_at = self._clock()
        if self.interactions is not None:
            self.interactions.note_user_input(pet_dragging=pet_dragging)
        self._schedule_next_idle_motion()

    def cancel_click_sequence(self) -> None:
        self._last_pet_click_at = 0.0

    def handle_pet_click(self, button: int) -> None:
        if button == 3:
            if self.tray is not None:
                self.tray.show_menu()
            return
        now = self._clock()
        self._last_interaction_at = now
        self.host.report_click(button)
        if button == 1:
            is_double_click = now - self._last_pet_click_at <= 0.45
            motion = (
                self.config.double_click_motion
                if is_double_click
                else self.config.left_click_motion
            )
            self._last_pet_click_at = now
            if is_double_click and self.chat is not None and self.config.chat.enabled:
                self.logger.info("[DesktopPet] double-click detected; opening chat")
                self.chat.open(from_pet=True)
        else:
            motion = self.config.right_click_motion
        if motion:
            self.host.start_motion(motion, 3)
        self._schedule_next_idle_motion()

    def tick(self, delta_time: float) -> None:
        try:
            frame = self.host.sample_frame()
            if frame is not None and self.interactions is not None:
                update = self.interactions.update(
                    position=frame.position,
                    viewport=frame.geometry.viewport,
                    work_area=frame.work_area,
                    delta_time=delta_time,
                    visible=frame.visible,
                    pet_dragging=frame.pet_dragging,
                    foot_anchor=frame.geometry.foot_anchor,
                )
                self._movement_status = update.status
                if update.presentation_intent and self.autonomous_displacement:
                    self.host.present_movement(update.presentation_intent)
                if update.position is not None and self.autonomous_displacement:
                    target_x, target_y = clamp_window_position(
                        round(update.position[0]),
                        round(update.position[1]),
                        frame.geometry.viewport[0],
                        frame.geometry.viewport[1],
                        frame.work_area,
                        0,
                    )
                    if (target_x, target_y) != tuple(round(value) for value in frame.position):
                        self.host.move_window(target_x, target_y)
                if self.tray is not None and self.interactions.consume_menu_changed():
                    self._refresh_tray_targets()

            if self.chat is not None:
                now = self._clock()
                if now - self._last_chat_sync_at >= 1 / 120:
                    self._last_chat_sync_at = now
                    self.host.sync_chat_anchor(self.chat, False)

            self._maybe_play_idle_motion(frame)
        except Exception as exc:
            self.logger.warning("[DesktopPet] controller update failed: {}", exc)

    def sync_chat(self, *, force: bool = False) -> None:
        if self.chat is None:
            return
        now = self._clock()
        if not force and now - self._last_chat_sync_at < 1 / 120:
            return
        self._last_chat_sync_at = now
        self.host.sync_chat_anchor(self.chat, force)

    def menu_flag(self, name: str) -> bool:
        if not self.autonomous_displacement and name in {
            "window_approach",
            "icon_approach",
            "follow_foreground_window",
        }:
            return False
        if self.interactions is not None and (
            name in {
                "interaction_enabled",
                "window_approach",
                "icon_approach",
                "follow_foreground_window",
                "move_windows",
                "move_icons",
                "gui_inspection",
                "gui_actions",
            }
            or name.startswith("gui_target:")
        ):
            return self.interactions.get_state(name)
        return self.host.read_window_flag(name)

    def handle_command(self, command: str, value: Any = None) -> bool:
        session = self.interactions
        if not self.autonomous_displacement and command in {
            "desktop_toggle_window_approach",
            "desktop_toggle_icon_approach",
            "desktop_toggle_follow_window",
        }:
            self.logger.debug(
                "[DesktopPet] ignored autonomous movement toggle for a backend without displacement capability: {}",
                command,
            )
            return True
        if session is not None and session.handle_command(command, value):
            self._refresh_tray_targets()
            if self.tray is not None:
                self.tray.refresh()
            return True
        if command == "desktop_open_chat":
            if self.chat is not None and self.config.chat.enabled:
                self.chat.open()
            return True
        if command == "desktop_open_parameters":
            if self.chat is not None:
                self.chat.open_parameters()
            else:
                self.host.open_parameters()
            return True
        if command == "desktop_parameter_set":
            if isinstance(value, dict):
                self.host.set_parameter(value)
            return True
        if command == "desktop_parameters_reset":
            self.host.reset_parameters(str(value) if value is not None else None)
            return True
        callbacks = {
            "desktop_toggle_visibility": self.host.toggle_visibility,
            "desktop_toggle_click_through": self.host.toggle_click_through,
            "desktop_toggle_topmost": self.host.toggle_topmost,
            "desktop_reset_position": self.host.reset_position,
            "desktop_quit": self.host.quit,
        }
        callback = callbacks.get(command)
        if callback is not None:
            callback()
            return True
        if command == "desktop_motion":
            self.host.start_motion(str(value or "Tap"), 3)
            return True
        return False

    def _refresh_tray_targets(self) -> None:
        if self.tray is not None and self.interactions is not None:
            self.tray.set_gui_targets(
                self.interactions.allowed_gui_targets,
                self.interactions.inspected_gui_targets,
            )

    def _schedule_next_idle_motion(self) -> None:
        minimum = self.config.idle_motion_min_seconds
        maximum = self.config.idle_motion_max_seconds
        self._next_idle_motion_at = self._clock() + random.uniform(minimum, maximum)

    def schedule_next_idle_motion(self) -> None:
        """Reset the idle-presentation timer after visible user activity."""

        self._schedule_next_idle_motion()

    def _maybe_play_idle_motion(self, frame: DesktopPetFrame | None) -> None:
        if (
            not self._started
            or not self.config.idle_motion_groups
            or self._clock() < self._next_idle_motion_at
            or (
                frame is not None
                and (not frame.visible or frame.pet_dragging or frame.motion_busy)
            )
        ):
            return
        if self._clock() - self._last_interaction_at < self.config.idle_motion_min_seconds:
            self._schedule_next_idle_motion()
            return
        group = self._choice(list(self.config.idle_motion_groups))
        self.logger.info("[DesktopPet] idle interaction: {}", group)
        self.host.start_motion(group, 1)
        self._schedule_next_idle_motion()

    def maybe_play_idle_motion(self, frame: DesktopPetFrame | None = None) -> None:
        """Compatibility and host-test seam for one idle tick."""

        self._maybe_play_idle_motion(frame)

