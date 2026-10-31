from __future__ import annotations

import queue
import unittest

from live2d_adapter.chat import DesktopPetChat, pet_side_button_positions
from live2d_adapter.config import DesktopChatConfig
from live2d_adapter.ui_theme import scale_widgets, ui_scale


class ZoomTests(unittest.TestCase):
    def test_native_send_button_works_after_zoom_and_does_not_send_drag_out(self) -> None:
        import tkinter as tk
        from unittest.mock import Mock

        root = tk.Tk()
        root.withdraw()
        try:
            chat = DesktopPetChat(
                DesktopChatConfig(enabled=True), "pet", lambda *_: None, Mock(), backend=Mock()
            )
            chat._tk = tk
            window = tk.Toplevel(root)
            chat._build_composer_surface(window)
            canvas = window.winfo_children()[0]
            chat._submit = Mock()
            for factor in (1.0, 0.7, 1.25, 1.0):
                window.geometry(f"{round(400 * factor)}x{round(60 * factor)}")
                scale_widgets(window, factor)
                root.update()
                bounds = canvas.coords(canvas.find_withtag("send")[0])
                x, y = round((bounds[0] + bounds[2]) / 2), round((bounds[1] + bounds[3]) / 2)
                canvas.event_generate("<Motion>", x=x, y=y)
                canvas.event_generate("<ButtonPress-1>", x=x, y=y)
                canvas.event_generate("<ButtonRelease-1>", x=x, y=y)
                root.update()
                chat._submit.assert_called_once()
                chat._submit.reset_mock()
                canvas.event_generate("<ButtonPress-1>", x=x, y=y)
                canvas.event_generate("<ButtonRelease-1>", x=5, y=5)
                root.update()
                chat._submit.assert_not_called()
        finally:
            root.update_idletasks()
            root.destroy()

    def test_zoom_only_anchor_change_is_not_deduplicated(self) -> None:
        chat = DesktopPetChat(DesktopChatConfig(), "pet", lambda *_: None, None)
        options = dict(visible=True, topmost=False, click_through=False, hovered=True)
        for zoom in (1.0, 0.7, 0.7, 1.3):
            chat.sync_anchor((500, 100, 700, 600), (0, 0, 1920, 1080), model_scale=zoom, **options)
        anchors = []
        while True:
            try:
                anchors.append(chat._ui_queue.get_nowait()[1]["ui_scale"])
            except queue.Empty:
                break
        self.assertEqual(anchors, [1.0, 0.7, 1.3])
        self.assertEqual(ui_scale(float("nan")), 1.0)
        small = pet_side_button_positions((500, 100, 700, 600), (0, 0, 1920, 1080), 0.7)
        large = pet_side_button_positions((500, 100, 700, 600), (0, 0, 1920, 1080), 1.3)
        self.assertLess(large["settings"][0], small["settings"][0])

    def test_placeholder_never_becomes_a_submitted_message(self) -> None:
        import tkinter as tk
        from unittest.mock import Mock

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            backend = Mock()
            chat = DesktopPetChat(
                DesktopChatConfig(enabled=True), "pet", lambda *_: None, Mock(), backend=backend
            )
            chat._tk = tk
            window = tk.Toplevel(root)
            window.withdraw()
            chat._build_composer_surface(window)
            self.assertTrue(chat._placeholder_visible)
            chat._submit()
            backend.ask.assert_not_called()
            chat._input_variable.set("真正的草稿")
            self.assertFalse(chat._placeholder_visible)
        finally:
            root.update_idletasks()
            root.destroy()

    def test_real_tk_zoom_preserves_draft_and_restores_original_metrics(self) -> None:
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            canvas = tk.Canvas(root, width=380, height=54, highlightthickness=0)
            canvas.pack(padx=10)
            entry = tk.Entry(canvas, font=("Segoe UI", 10))
            entry.insert(0, "保留我的草稿")
            item = canvas.create_window(20, 27, window=entry, width=340, height=30)
            for factor in (1.0, 0.7, 1.3, 1.0):
                scale_widgets(canvas, factor)
                self.assertEqual(float(canvas.itemcget(item, "width")), round(340 * factor))
                self.assertEqual(entry.get(), "保留我的草稿")
                self.assertEqual(canvas.coords(item), [20 * factor, 27 * factor])
            self.assertEqual(int(canvas.cget("width")), 380)
        finally:
            root.update_idletasks()
            root.destroy()
