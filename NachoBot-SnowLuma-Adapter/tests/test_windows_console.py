from __future__ import annotations

from collections.abc import Callable

import pytest

from src import windows_console


class _FakeKernel32:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.callback: Callable[[int], bool] | None = None

    def SetConsoleCtrlHandler(self, callback: Callable[[int], bool], _add: bool) -> bool:
        self.callback = callback
        return self.result


@pytest.fixture(autouse=True)
def reset_registered_handler() -> None:
    previous = windows_console._registered_handler
    windows_console._registered_handler = None
    try:
        yield
    finally:
        windows_console._registered_handler = previous


def test_close_event_terminates_immediately_with_injected_terminator() -> None:
    terminated: list[int] = []
    kernel32 = _FakeKernel32(result=True)

    assert windows_console.register_console_close_handler(
        platform="win32",
        kernel32=kernel32,
        terminator=terminated.append,
    ) is True
    assert kernel32.callback is not None

    assert bool(kernel32.callback(windows_console.CTRL_CLOSE_EVENT)) is True
    assert terminated == [0]


@pytest.mark.parametrize("control_type", (0, 1, 5, 6))
def test_non_close_events_are_not_claimed(control_type: int) -> None:
    terminated: list[int] = []
    kernel32 = _FakeKernel32(result=True)

    windows_console.register_console_close_handler(
        platform="win32",
        kernel32=kernel32,
        terminator=terminated.append,
    )
    assert kernel32.callback is not None

    assert bool(kernel32.callback(control_type)) is False
    assert terminated == []


def test_successful_registration_retains_callback_reference() -> None:
    kernel32 = _FakeKernel32(result=True)

    assert windows_console.register_console_close_handler(
        platform="win32", kernel32=kernel32
    ) is True
    assert kernel32.callback is windows_console._registered_handler


def test_registration_failure_is_non_fatal() -> None:
    kernel32 = _FakeKernel32(result=False)

    assert windows_console.register_console_close_handler(
        platform="win32", kernel32=kernel32
    ) is False
    assert windows_console._registered_handler is None


def test_non_windows_is_a_no_op() -> None:
    class _UnexpectedKernel32:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"native API called unexpectedly: {name}")

    assert windows_console.register_console_close_handler(
        platform="linux", kernel32=_UnexpectedKernel32()
    ) is False
    assert windows_console._registered_handler is None

