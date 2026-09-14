"""System-tray controls for the Windows desktop pet."""

from __future__ import annotations

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
    ) -> None:
        self.title = title
        self.on_command = on_command
        self.get_state = get_state
        self.logger = logger
        self._icon = None
        self._thread: threading.Thread | None = None

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

    def start(self) -> None:
        try:
            import pystray
        except ImportError:
            self.logger.warning("pystray is unavailable; desktop pet tray disabled")
            return

        def send(command: str, value: Any = None):
            def callback(_icon=None, _item=None) -> None:
                self.on_command(command, value)

            return callback

        menu = pystray.Menu(
            pystray.MenuItem("和日和对话", send("desktop_open_chat"), default=True),
            pystray.MenuItem("显示 / 隐藏", send("desktop_toggle_visibility")),
            pystray.MenuItem(
                "鼠标穿透",
                send("desktop_toggle_click_through"),
                checked=lambda _item: self.get_state("click_through"),
            ),
            pystray.MenuItem(
                "始终置顶",
                send("desktop_toggle_topmost"),
                checked=lambda _item: self.get_state("always_on_top"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打个招呼", send("desktop_motion", "Tap")),
            pystray.MenuItem("回到屏幕右下角", send("desktop_reset_position")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出桌宠", send("desktop_quit")),
        )
        self._icon = pystray.Icon(
            "nachobot-hiyori-desktop-pet",
            self._create_image(),
            self.title,
            menu,
        )
        self._thread = threading.Thread(
            target=self._icon.run,
            name="nachobot-live2d-tray",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("Desktop pet tray started")

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
