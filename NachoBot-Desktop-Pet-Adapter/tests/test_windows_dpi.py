from __future__ import annotations

import ctypes
import unittest
from unittest.mock import Mock, patch

from desktop_pet_adapter.windows_dpi import per_monitor_dpi_context


class WindowsDpiContextTests(unittest.TestCase):
    def test_prefers_per_monitor_v2_and_restores_previous_thread_context(self) -> None:
        set_context = Mock(side_effect=[0x1234, None])
        with patch(
            "desktop_pet_adapter.windows_dpi._set_thread_context_function",
            return_value=set_context,
        ):
            with per_monitor_dpi_context() as active:
                self.assertTrue(active)

        self.assertEqual(
            [call.args[0].value for call in set_context.call_args_list],
            [ctypes.c_void_p(-4).value, 0x1234],
        )

    def test_falls_back_to_per_monitor_v1_and_restores_on_exception(self) -> None:
        set_context = Mock(side_effect=[0, 0x4321, None])
        with patch(
            "desktop_pet_adapter.windows_dpi._set_thread_context_function",
            return_value=set_context,
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with per_monitor_dpi_context() as active:
                    self.assertTrue(active)
                    raise RuntimeError("fixture")

        self.assertEqual(
            [call.args[0].value for call in set_context.call_args_list],
            [ctypes.c_void_p(-4).value, ctypes.c_void_p(-3).value, 0x4321],
        )

    def test_unsupported_platform_keeps_caller_running(self) -> None:
        with patch(
            "desktop_pet_adapter.windows_dpi._set_thread_context_function",
            return_value=None,
        ):
            with per_monitor_dpi_context() as active:
                self.assertFalse(active)


if __name__ == "__main__":
    unittest.main()
