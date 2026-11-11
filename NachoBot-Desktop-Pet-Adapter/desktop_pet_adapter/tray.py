"""System-tray controls for the Windows desktop pet."""

from __future__ import annotations

import json
import hashlib
import threading
from collections.abc import Callable
from typing import Any

TrayCommand = Callable[[str, Any], None]
StateGetter = Callable[[str], bool]


class DesktopPetTray:
    def __init__(
        self,
        title: str,
        on_command: TrayCommand,
        get_state: StateGetter,
        logger: Any,
        character_name: str = "日和",
    ) -> None:
        self.title = title
        self.character_name = character_name
        self.on_command = on_command
        self.get_state = get_state
        self.logger = logger
        self._icon = None
        self._thread: threading.Thread | None = None
        self._menu_message = 0x8000 + 63
        self._inspected_targets: tuple[Any, ...] = ()
        self._allowed_targets: tuple[Any, ...] = ()

    @staticmethod
    def _create_image():
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill=(255, 116, 169, 255))
        draw.ellipse((18, 22, 24, 30), fill=(255, 255, 255, 255))
        draw.ellipse((40, 22, 46, 30), fill=(255, 255, 255, 255))
        draw.arc((19, 22, 45, 48), 20, 160, fill=(255, 255, 255, 255), width=4)
        return image

    def start(self, *, visible: bool = True) -> None:
        try:
            import pystray
        except ImportError:
            self.logger.warning("pystray is unavailable; desktop pet tray disabled")
            return

        def send(command: str, value: Any = None):
            def callback(_icon=None, _item=None) -> None:
                self.on_command(command, value)

            return callback

        self._build_menu(pystray, send)
        self._icon = pystray.Icon(
            "nachobot-hiyori-desktop-pet",
            self._create_image(),
            self.title,
            self._menu,
        )
        # Post onto pystray's own message loop: the exact same native menu and
        # callbacks are used for the pet and the notification-area icon.
        from pystray._util import win32

        icon = self._icon

        def open_menu(_wparam: Any, _lparam: Any) -> None:
            icon.update_menu()
            icon._on_notify(0, win32.WM_RBUTTONUP)

        icon._message_handlers[self._menu_message] = open_menu
        self._thread = threading.Thread(
            target=self._icon.run,
            kwargs={"setup": lambda current: setattr(current, "visible", visible)},
            name="nachobot-live2d-tray",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("Desktop pet tray started")

    def _build_menu(self, pystray, send) -> None:
        controls = []
        targets = self._dedupe_targets((*self._allowed_targets, *self._inspected_targets))
        for target in targets[:20]:
            state_key = self._target_state_key(target)
            controls.append(
                pystray.MenuItem(
                    self._target_label(target),
                    send("desktop_toggle_gui_target", target),
                    checked=lambda _item, key=state_key: self.get_state(key),
                )
            )
        controls_menu = (
            pystray.Menu(*controls)
            if controls
            else pystray.Menu(
                pystray.MenuItem("尚无候选控件；先扫描前台窗口", lambda: None, enabled=False)
            )
        )

        def game_input_label(_item=None) -> str:
            if self.get_state("game_input_failed"):
                return "游戏计数兼容：失败，可重试"
            if self.get_state("game_input_stopping"):
                return "游戏计数兼容：正在停止"
            if self.get_state("game_input_active"):
                return "游戏计数兼容：已启用"
            if self.get_state("game_input_pending"):
                return "游戏计数兼容：正在请求管理员权限"
            return "游戏计数兼容：普通模式（启用时需管理员授权）"

        self._menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏", send("desktop_toggle_visibility")),
            pystray.MenuItem(
                "鼠标穿透",
                send("desktop_toggle_click_through"),
                checked=lambda _item: self.get_state("click_through"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打个招呼", send("desktop_motion", "Tap")),
            pystray.MenuItem("回到屏幕右下角", send("desktop_reset_position")),
            pystray.MenuItem(
                game_input_label,
                send("desktop_toggle_game_input"),
                checked=lambda _item: self.get_state("game_input_pending")
                or self.get_state("game_input_active"),
                visible=lambda _item: self.get_state("game_input_available"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "自主互动",
                pystray.Menu(
                    pystray.MenuItem(
                        "自动互动总开关",
                        send("desktop_toggle_interactions"),
                        checked=lambda _item: self.get_state("interaction_enabled"),
                    ),
                    pystray.MenuItem(
                        "攀爬 / 跟随窗口",
                        send("desktop_toggle_window_approach"),
                        checked=lambda _item: self.get_state("window_approach"),
                    ),
                    pystray.MenuItem(
                        "跟随前台窗口移动",
                        send("desktop_toggle_follow_window"),
                        checked=lambda _item: self.get_state("follow_foreground_window"),
                    ),
                    pystray.MenuItem(
                        "靠近桌面图标",
                        send("desktop_toggle_icon_approach"),
                        checked=lambda _item: self.get_state("icon_approach"),
                    ),
                    pystray.MenuItem(
                        "轻推普通窗口",
                        send("desktop_toggle_window_moves"),
                        checked=lambda _item: self.get_state("move_windows"),
                    ),
                    pystray.MenuItem(
                        "移动桌面图标",
                        send("desktop_toggle_icon_moves"),
                        checked=lambda _item: self.get_state("move_icons"),
                    ),
                    pystray.MenuItem(
                        "UIA 控件检查",
                        send("desktop_toggle_gui_inspection"),
                        checked=lambda _item: self.get_state("gui_inspection"),
                    ),
                    pystray.MenuItem(
                        "UIA 允许控件动作",
                        send("desktop_toggle_gui_actions"),
                        checked=lambda _item: self.get_state("gui_actions"),
                    ),
                    pystray.MenuItem("扫描前台控件", send("desktop_gui_inspect")),
                    pystray.MenuItem("允许的控件", controls_menu),
                    pystray.MenuItem(
                        "恢复窗口与图标位置",
                        send("desktop_restore_interactions"),
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出桌宠", send("desktop_quit")),
        )

    @staticmethod
    def _dedupe_targets(targets: tuple[Any, ...]) -> tuple[Any, ...]:
        values: dict[str, Any] = {}
        for target in targets:
            values[DesktopPetTray._target_state_key(target)] = target
        return tuple(values.values())

    @staticmethod
    def _target_state_key(target: Any) -> str:
        data = {
            key: getattr(target, key, "")
            for key in (
                "process_name",
                "window_title",
                "control_name",
                "control_type",
                "action",
                "automation_id",
            )
        }
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return "gui_target:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _target_label(target: Any) -> str:
        label = f"{target.process_name}: {target.control_name} ({target.action})"
        return label[:100]

    def set_gui_targets(self, allowed: tuple[Any, ...], inspected: tuple[Any, ...]) -> None:
        self._allowed_targets = tuple(allowed)
        self._inspected_targets = tuple(inspected)
        icon = self._icon
        if icon is None:
            return
        try:
            import pystray

            def send(command: str, value: Any = None):
                def callback(_icon=None, _item=None) -> None:
                    self.on_command(command, value)

                return callback

            self._build_menu(pystray, send)
            icon.menu = self._menu
        except Exception as exc:
            self.logger.debug("Desktop UIA target menu refresh failed: {}", exc)

    def show_menu(self) -> bool:
        icon = self._icon
        hwnd = getattr(icon, "_hwnd", None)
        if not hwnd:
            return False
        try:
            import win32gui

            win32gui.PostMessage(hwnd, self._menu_message, 0, 0)
            return True
        except Exception as exc:
            self.logger.warning("Could not open desktop pet menu: {}", exc)
            return False

    def refresh(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception as exc:
                self.logger.debug("Desktop pet tray refresh failed: {}", exc)

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:
                self.logger.debug("Desktop pet tray stop failed: {}", exc)
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
