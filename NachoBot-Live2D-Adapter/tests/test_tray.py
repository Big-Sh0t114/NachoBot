from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live2d_adapter.tray import DesktopPetTray


class TrayMenuTests(unittest.TestCase):
    def test_pet_posts_to_the_existing_tray_thread_and_menu(self) -> None:
        import pystray
        from pystray._util import win32

        commands = []
        icon = SimpleNamespace(
            _hwnd=123, _message_handlers={}, run=Mock(), update_menu=Mock(), _on_notify=Mock()
        )
        tray = DesktopPetTray("pet", lambda *args: commands.append(args), lambda _: True, Mock())
        with (
            patch.object(pystray, "Icon", return_value=icon) as factory,
            patch("live2d_adapter.tray.threading.Thread"),
        ):
            tray.start(visible=False)
        menu = factory.call_args.args[3]
        menu.items[0](icon)
        self.assertEqual(commands, [("desktop_toggle_visibility", None)])
        post = Mock()
        with patch.dict(sys.modules, {"win32gui": SimpleNamespace(PostMessage=post)}):
            self.assertTrue(tray.show_menu())
        post.assert_called_once_with(123, tray._menu_message, 0, 0)
        icon._message_handlers[tray._menu_message](0, 0)
        icon.update_menu.assert_called_once()
        icon._on_notify.assert_called_once_with(0, win32.WM_RBUTTONUP)
