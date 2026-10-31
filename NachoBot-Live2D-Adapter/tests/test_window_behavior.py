from __future__ import annotations

import os
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == "nt", "native Windows behavior")
class PassiveWindowTests(unittest.TestCase):
    def test_real_sdl_window_receives_click_without_activation_and_stays_topmost(self):
        # An isolated software SDL window exercises its real WndProc without
        # loading a model, changing the cursor, or injecting global input.
        script = r'''
import pygame
import win32con
import win32gui
from live2d_adapter.chat import _suppress_thread_window_activation
from live2d_adapter.window_behavior import PassiveWindow

pygame.display.init()
before = win32gui.GetForegroundWindow()
with _suppress_thread_window_activation():
    pygame.display.set_mode((120, 160), pygame.NOFRAME | pygame.HIDDEN)
hwnd = pygame.display.get_wm_info()["window"]
passive = PassiveWindow(hwnd)
owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
try:
    assert win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0x08000000
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    pygame.event.pump()
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    pygame.event.pump()
    assert win32gui.GetForegroundWindow() == before, "show took foreground"
    assert win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
    capture = win32gui.GetCapture()
    for button in (win32con.WM_LBUTTONDOWN, win32con.WM_RBUTTONDOWN, win32con.WM_LBUTTONDBLCLK):
        result = win32gui.SendMessage(hwnd, win32con.WM_MOUSEACTIVATE, hwnd, (button << 16) | win32con.HTCLIENT)
        assert result == win32con.MA_NOACTIVATE, result
    pygame.event.clear()
    win32gui.SendMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, (40 << 16) | 40)
    win32gui.SendMessage(hwnd, win32con.WM_LBUTTONUP, 0, (40 << 16) | 40)
    events = pygame.event.get()
    assert any(event.type == pygame.MOUSEBUTTONDOWN for event in events), events
    assert any(event.type == pygame.MOUSEBUTTONUP for event in events), events
    assert win32gui.GetForegroundWindow() == before, "click took foreground"
    assert win32gui.GetCapture() == capture, "click captured the mouse"
    assert owner and not win32gui.IsWindowVisible(owner)
    for _ in range(3):
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        pygame.event.pump()
        assert win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
        assert win32gui.GetForegroundWindow() == before
finally:
    passive.close()
    assert not win32gui.IsWindow(owner)
    assert win32gui.IsWindow(hwnd)
    passive.close()
    from unittest.mock import patch
    with patch("live2d_adapter.window_behavior._keep_sdl_window_topmost", side_effect=RuntimeError("setup failed")):
        try:
            PassiveWindow(hwnd)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected setup failure")
    assert win32gui.IsWindow(hwnd)
    assert not win32gui.GetWindow(hwnd, win32con.GW_OWNER), "failed setup leaked an owner"
    pygame.quit()
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
