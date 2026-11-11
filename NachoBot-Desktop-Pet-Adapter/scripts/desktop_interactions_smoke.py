"""Isolated native smoke for scene enumeration, fixture window movement, and UIA.

Run from the adapter with:
    .venv/Scripts/python.exe scripts/desktop_interactions_smoke.py

The parent launches this script in a child Python process with a hard timeout.
Only a disposable, non-activating fixture window is moved or invoked. Desktop
icons are enumerated through the Shell view and are never repositioned.
"""

from __future__ import annotations

import subprocess
import sys
import time


def _fixture_window():
    import win32api
    import win32con
    import win32gui

    clicks = {"count": 0}
    class_name = f"NachoBotInteractionFixture-{win32api.GetCurrentProcessId()}"
    title = "NachoBot Desktop Interaction Fixture"

    def window_proc(hwnd, message, wparam, lparam):
        if message == win32con.WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            notification = (int(wparam) >> 16) & 0xFFFF
            if command_id == 1001 and notification == win32con.BN_CLICKED:
                clicks["count"] += 1
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    window_class = win32gui.WNDCLASS()
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpszClassName = class_name
    window_class.lpfnWndProc = window_proc
    window_class.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    window_class.hbrBackground = win32gui.GetStockObject(win32con.WHITE_BRUSH)
    win32gui.RegisterClass(window_class)
    hwnd = 0
    button = 0
    try:
        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_NOACTIVATE,
            class_name,
            title,
            win32con.WS_OVERLAPPEDWINDOW,
            180,
            160,
            560,
            340,
            0,
            0,
            window_class.hInstance,
            None,
        )
        button = win32gui.CreateWindowEx(
            0,
            "BUTTON",
            "Fixture Safe Button",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            32,
            48,
            220,
            36,
            hwnd,
            1001,
            window_class.hInstance,
            None,
        )
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.UpdateWindow(hwnd)
        yield hwnd, button, clicks, title
    finally:
        if button and win32gui.IsWindow(button):
            win32gui.DestroyWindow(button)
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(class_name, window_class.hInstance)


def _run_probe() -> int:
    if sys.platform != "win32":
        print("SKIP: native desktop interaction smoke requires Windows")
        return 0

    import pythoncom
    import win32gui

    from desktop_pet_adapter.desktop_scene import DesktopSceneProvider, SceneUnavailable
    from desktop_pet_adapter.desktop_uia import DesktopUIA

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    provider = None
    fixture = _fixture_window()
    try:
        hwnd, _button, clicks, title = next(fixture)
        provider = DesktopSceneProvider(own_hwnd=None, own_pid=-1, include_icons=True)
        snapshot = provider.snapshot()
        if isinstance(snapshot, SceneUnavailable):
            raise RuntimeError(f"scene snapshot unavailable: {snapshot.reason}: {snapshot.detail}")
        target = next((window for window in snapshot.windows if window.hwnd == hwnd), None)
        if target is None or not target.movable:
            raise RuntimeError("disposable fixture was missing or not a movable ordinary window")

        before_rect = target.rect
        receipt = provider.move_window(
            hwnd=target.hwnd,
            pid=target.pid,
            process_name=target.process_name,
            title=title,
            expected_rect=target.rect,
            dx=5,
            dy=3,
            max_delta_px=8,
        )
        if receipt is None or (receipt.moved_dx, receipt.moved_dy) != (5, 3):
            raise RuntimeError(f"fixture-only move failed: {receipt!r}")
        moved_snapshot = provider.snapshot()
        moved = next(
            (window for window in getattr(moved_snapshot, "windows", ()) if window.hwnd == hwnd),
            None,
        )
        if moved is None or moved.rect == before_rect:
            raise RuntimeError("fixture did not move according to the read-only scene snapshot")
        restored = provider.restore_windows()
        restored_snapshot = provider.snapshot()
        restored_window = next(
            (window for window in getattr(restored_snapshot, "windows", ()) if window.hwnd == hwnd),
            None,
        )
        if restored < 1 or restored_window is None or restored_window.rect != before_rect:
            raise RuntimeError("fixture window did not restore to its original rectangle")

        uia = DesktopUIA(max_controls=20)
        targets = uia.inspect_window(hwnd)
        button_target = next(
            (
                item
                for item in targets
                if item.control_name == "Fixture Safe Button"
                and item.control_type.casefold() == "button"
                and item.action == "invoke"
            ),
            None,
        )
        if button_target is None:
            raise RuntimeError("UIA could not inspect the disposable fixture button")
        if not uia.execute_window(hwnd, button_target):
            raise RuntimeError("UIA Invoke failed on the disposable fixture button")
        deadline = time.monotonic() + 1.0
        while clicks["count"] == 0 and time.monotonic() < deadline:
            win32gui.PumpWaitingMessages()
            time.sleep(0.01)
        if clicks["count"] != 1:
            raise RuntimeError("fixture did not receive exactly one UIA button invocation")

        print(
            "PASS: "
            f"snapshot_windows={len(snapshot.windows)} "
            f"desktop_icons={len(snapshot.icons)} "
            f"fixture_move_delta={receipt.moved_dx},{receipt.moved_dy} "
            f"restore_count={restored} "
            f"uia_targets={len(targets)} invoked={clicks['count']}"
        )
        return 0
    finally:
        if provider is not None:
            provider.close()
        try:
            fixture.close()
        except Exception:
            pass
        pythoncom.CoUninitialize()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-child":
        return _run_probe()
    command = [sys.executable, __file__, "--probe-child"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        print("FAIL: native desktop probe exceeded its 20 second limit", file=sys.stderr)
        return 124
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(f"probe_exit={result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
