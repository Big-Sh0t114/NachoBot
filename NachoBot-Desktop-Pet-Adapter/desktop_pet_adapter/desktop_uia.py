"""Read-only UIA inspection and exact-pattern actions for configured controls."""

from __future__ import annotations

import ctypes
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from .config import DesktopGuiTarget

_UNSAFE_NAME_PARTS = (
    "delete",
    "remove",
    "send",
    "submit",
    "purchase",
    "buy",
    "pay",
    "order",
    "transfer",
    "withdraw",
    "erase",
    "uninstall",
    "close",
)
_INVOKE_TYPES = {"button", "hyperlink", "menuitem", "splitbutton"}
_TOGGLE_TYPES = {"checkbox", "radiobutton", "togglebutton"}
_EXPAND_TYPES = {"treeitem", "button", "combobox"}


def target_is_safe(target: DesktopGuiTarget) -> bool:
    """Reject destructive or ambiguous-looking controls even when allowlisted."""

    name = target.control_name.casefold()
    return not any(part in name for part in _UNSAFE_NAME_PARTS)


def _pattern_is_available(wrapper, action: str) -> bool:
    try:
        if action == "invoke":
            return wrapper.iface_invoke is not None
        if action == "toggle":
            return wrapper.iface_toggle is not None
        if action == "expand":
            return wrapper.iface_expand_collapse is not None
    except Exception:
        return False
    return False


def _action_for_control(wrapper) -> str | None:
    """Return one safe, supported UIA pattern without invoking it."""

    control_type = str(wrapper.element_info.control_type or "").casefold()
    if control_type.replace(" ", "") in _TOGGLE_TYPES and _pattern_is_available(
        wrapper, "toggle"
    ):
        return "toggle"
    if control_type.replace(" ", "") in _EXPAND_TYPES and _pattern_is_available(
        wrapper, "expand"
    ):
        return "expand"
    if control_type.replace(" ", "") in _INVOKE_TYPES and _pattern_is_available(
        wrapper, "invoke"
    ):
        return "invoke"
    return None


class DesktopUIA:
    """Inspect the current foreground window and invoke only exact target matches.

    Calls are expected to run on a dedicated daemon worker. This class does not
    bring a target window forward and never uses screen-coordinate input.
    """

    def __init__(self, *, max_controls: int = 40) -> None:
        self.max_controls = max(1, min(100, int(max_controls)))

    def inspect_foreground(self) -> tuple[DesktopGuiTarget, ...]:
        hwnd, process_name, title, pid = self._foreground_identity()
        if not hwnd or not title or pid == os.getpid():
            return ()
        return self._inspect_window(hwnd, process_name, title, pid, check_foreground=True)

    def inspect_window(self, hwnd: int) -> tuple[DesktopGuiTarget, ...]:
        """Inspect a specified window for disposable fixture validation."""

        identity = self._window_identity(hwnd)
        if identity is None:
            return ()
        process_name, title, pid = identity
        return self._inspect_window(int(hwnd), process_name, title, pid, check_foreground=False)

    def inspect_window_identity(
        self,
        hwnd: int,
        pid: int,
        process_name: str,
        title: str,
    ) -> tuple[DesktopGuiTarget, ...]:
        """Inspect a sampled external window without requiring current focus.

        The Windows tray menu itself takes foreground focus. A manual scan can
        therefore use the most recently sampled external foreground window,
        but only while its HWND, PID, process name, and title still identify the
        same window before and after the UIA tree read.
        """

        try:
            expected_pid = int(pid)
            hwnd = int(hwnd)
        except (TypeError, ValueError, OverflowError):
            return ()
        expected_process = str(process_name).strip()
        expected_title = str(title).strip()
        if not hwnd or not expected_process or not expected_title or expected_pid == os.getpid():
            return ()
        identity = self._window_identity(hwnd)
        if not self._identity_matches(
            identity,
            expected_pid,
            expected_process,
            expected_title,
        ):
            return ()
        targets = self._inspect_window(
            hwnd,
            expected_process,
            expected_title,
            expected_pid,
            check_foreground=False,
        )
        if not self._identity_matches(
            self._window_identity(hwnd),
            expected_pid,
            expected_process,
            expected_title,
        ):
            return ()
        return targets

    @staticmethod
    def _identity_matches(
        identity: tuple[str, str, int] | None,
        pid: int,
        process_name: str,
        title: str,
    ) -> bool:
        if identity is None:
            return False
        actual_process, actual_title, actual_pid = identity
        return (
            actual_pid == pid
            and actual_process.casefold() == process_name.casefold()
            and actual_title == title
        )

    def _inspect_window(
        self,
        hwnd: int,
        process_name: str,
        title: str,
        pid: int,
        *,
        check_foreground: bool,
    ) -> tuple[DesktopGuiTarget, ...]:

        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        targets: list[DesktopGuiTarget] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        for wrapper in window.descendants():
            try:
                info = wrapper.element_info
                if not wrapper.is_enabled() or not info.name:
                    continue
                action = _action_for_control(wrapper)
                if action is None:
                    continue
                control_name = str(info.name).strip()
                target = DesktopGuiTarget(
                    process_name=process_name,
                    window_title=title,
                    control_name=control_name,
                    control_type=str(info.control_type or "").strip(),
                    action=action,
                    automation_id=str(info.automation_id or "").strip(),
                )
                if not target_is_safe(target):
                    continue
                key = _target_key(target)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(target)
                if len(targets) >= self.max_controls:
                    break
            except Exception:
                # UIA trees can change while they are read; skip stale nodes.
                continue
        if check_foreground and self._foreground_identity() != (
            hwnd,
            process_name,
            title,
            pid,
        ):
            return ()
        return tuple(targets)

    def execute(
        self,
        target: DesktopGuiTarget,
        *,
        action_allowed: Callable[[], bool] | None = None,
    ) -> bool:
        """Re-find and revalidate the exact foreground target immediately before use."""

        hwnd, process_name, title, pid = self._foreground_identity()
        if (
            not hwnd
            or pid == os.getpid()
        ):
            return False
        return self._execute_exact(
            hwnd,
            process_name,
            title,
            pid,
            target,
            check_foreground=True,
            action_allowed=action_allowed,
        )

    def execute_window(
        self,
        hwnd: int,
        target: DesktopGuiTarget,
        *,
        action_allowed: Callable[[], bool] | None = None,
    ) -> bool:
        """Execute against a caller-specified disposable fixture window.

        Runtime actions should use :meth:`execute`, which requires the target to
        be foreground. This method exists for isolated native fixture probes and
        never activates the specified window.
        """

        identity = self._window_identity(hwnd)
        if identity is None:
            return False
        process_name, title, pid = identity
        return self._execute_exact(
            int(hwnd),
            process_name,
            title,
            pid,
            target,
            check_foreground=False,
            action_allowed=action_allowed,
        )

    def _execute_exact(
        self,
        hwnd: int,
        process_name: str,
        title: str,
        pid: int,
        target: DesktopGuiTarget,
        *,
        check_foreground: bool,
        action_allowed: Callable[[], bool] | None,
    ) -> bool:
        if (
            not target_is_safe(target)
            or process_name.casefold() != target.process_name.casefold()
            or title.casefold() != target.window_title.casefold()
        ):
            return False

        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        matches = []
        for wrapper in window.descendants():
            try:
                info = wrapper.element_info
                if (
                    str(info.name or "").strip().casefold()
                    == target.control_name.casefold()
                    and str(info.control_type or "").strip().casefold()
                    == target.control_type.casefold()
                    and str(info.automation_id or "").strip().casefold()
                    == target.automation_id.casefold()
                    and wrapper.is_enabled()
                    and _pattern_is_available(wrapper, target.action)
                ):
                    matches.append(wrapper)
            except Exception:
                continue
        # A duplicate accessible name is ambiguous. Never choose the first one.
        if len(matches) != 1:
            return False
        wrapper = matches[0]
        if _action_for_control(wrapper) != target.action or not wrapper.is_enabled():
            return False
        # The foreground may have changed while the provider built its UIA
        # tree. Re-read HWND/PID/title after lookup and directly before the
        # pattern call so stale inspection results cannot act on a new window.
        if check_foreground:
            current_hwnd, current_process, current_title, current_pid = self._foreground_identity()
            if (
                current_hwnd != hwnd
                or current_pid != pid
                or current_process.casefold() != process_name.casefold()
                or current_title.casefold() != title.casefold()
            ):
                return False
        else:
            current_identity = self._window_identity(hwnd)
            if current_identity != (process_name, title, pid):
                return False
        if action_allowed is not None and not action_allowed():
            return False
        if target.action == "invoke":
            wrapper.iface_invoke.Invoke()
        elif target.action == "toggle":
            wrapper.iface_toggle.Toggle()
        elif target.action == "expand":
            pattern = wrapper.iface_expand_collapse
            collapsed_state = 0  # ExpandCollapseState_Collapsed
            if int(pattern.CurrentExpandCollapseState) != collapsed_state:
                return False
            if action_allowed is not None and not action_allowed():
                return False
            pattern.Expand()
        else:
            return False
        return True

    @staticmethod
    def _foreground_identity() -> tuple[int, str, str, int]:
        try:
            import win32gui
            import win32process

            hwnd = int(win32gui.GetForegroundWindow() or 0)
            if not hwnd:
                return 0, "", "", 0
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            image_path = _process_image_path(pid)
            if not image_path:
                return 0, "", "", 0
            return (
                hwnd,
                os.path.basename(image_path).casefold(),
                str(win32gui.GetWindowText(hwnd) or "").strip(),
                int(pid),
            )
        except Exception:
            return 0, "", "", 0

    @staticmethod
    def _window_identity(hwnd: int) -> tuple[str, str, int] | None:
        try:
            import win32gui
            import win32process

            handle = int(hwnd)
            if not win32gui.IsWindow(handle):
                return None
            _thread_id, pid = win32process.GetWindowThreadProcessId(handle)
            image_path = _process_image_path(pid)
            if not image_path:
                return None
            title = str(win32gui.GetWindowText(handle) or "").strip()
            if not title:
                return None
            return os.path.basename(image_path).casefold(), title, int(pid)
        except Exception:
            return None


def _process_image_path(pid: int) -> str:
    """Resolve a process image with query-limited rights, never VM_READ."""

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        query_name = kernel32.QueryFullProcessImageNameW
        query_name.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query_name.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        process = open_process(0x1000, 0, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(buffer))
            if not query_name(process, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            close_handle(process)
    except Exception:
        return ""


def _target_key(target: DesktopGuiTarget) -> tuple[str, str, str, str, str, str]:
    return (
        target.process_name.casefold(),
        target.window_title.casefold(),
        target.control_name.casefold(),
        target.control_type.casefold(),
        target.action,
        target.automation_id.casefold(),
    )


def merge_targets(*target_groups: Iterable[DesktopGuiTarget]) -> tuple[DesktopGuiTarget, ...]:
    merged: dict[tuple[str, str, str, str, str, str], DesktopGuiTarget] = {}
    for group in target_groups:
        for target in group:
            if target_is_safe(target):
                merged[_target_key(target)] = target
    return tuple(merged.values())


def load_saved_targets(path: Path | None) -> tuple[DesktopGuiTarget, ...]:
    if path is None:
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    targets: list[DesktopGuiTarget] = []
    for item in payload[:100]:
        if not isinstance(item, dict):
            continue
        try:
            target = DesktopGuiTarget(
                process_name=str(item["process_name"]).strip(),
                window_title=str(item["window_title"]).strip(),
                control_name=str(item["control_name"]).strip(),
                control_type=str(item["control_type"]).strip(),
                action=str(item["action"]).casefold().strip(),
                automation_id=str(item.get("automation_id", "")).strip(),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if (
            target.process_name
            and target.window_title
            and target.control_name
            and target.control_type
            and target.action in {"invoke", "toggle", "expand"}
            and target_is_safe(target)
        ):
            targets.append(target)
    return merge_targets(targets)


def save_targets(path: Path | None, targets: Iterable[DesktopGuiTarget]) -> bool:
    if path is None:
        return False
    safe_targets = merge_targets(targets)
    payload = [
        {
            "process_name": target.process_name,
            "window_title": target.window_title,
            "control_name": target.control_name,
            "control_type": target.control_type,
            "action": target.action,
            "automation_id": target.automation_id,
        }
        for target in safe_targets
    ]
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(path)
        return True
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
