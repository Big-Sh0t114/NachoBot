from __future__ import annotations

import gc
import math
import subprocess
import sys
import tkinter as tk
import time
import weakref
import unittest
from types import SimpleNamespace

from live2d_adapter.parameter_controls import ParameterController, ParameterPanel


class FakeParameter:
    def __init__(
        self,
        parameter_id: str,
        minimum: float,
        maximum: float,
        default: float,
        value: float,
    ) -> None:
        self.id = parameter_id
        self.min = minimum
        self.max = maximum
        self.default = default
        self.value = value


class FakeNativeModel:
    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.writes: list[tuple[str, float]] = []

    def SetParameterValueById(self, parameter_id: str, value: float) -> None:
        self.writes.append((parameter_id, value))
        for parameter in self.model.parameters:
            if parameter.id == parameter_id:
                parameter.value = value
                return


class FakeModel:
    def __init__(self, parameters: list[FakeParameter]) -> None:
        self.parameters = parameters
        self._model = FakeNativeModel(self)

    def GetParameterCount(self) -> int:
        return len(self.parameters)

    def GetParameter(self, index: int) -> FakeParameter:
        return self.parameters[index]

    def GetParameterValue(self, index: int) -> float:
        return self.parameters[index].value

    def Update(self) -> None:
        for index, parameter in enumerate(self.parameters):
            parameter.value = float(index) + 0.25


class FallbackModel:
    def __init__(self, parameters: list[FakeParameter]) -> None:
        self.parameters = parameters
        self.writes: list[tuple[str, float]] = []

    def GetParameterCount(self) -> int:
        return len(self.parameters)

    def GetParameter(self, index: int) -> FakeParameter:
        return self.parameters[index]

    def GetParameterValue(self, index: int) -> float:
        return self.parameters[index].value

    def SetParameterValue(self, parameter_id: str, value: float) -> None:
        self.writes.append((parameter_id, value))
        for parameter in self.parameters:
            if parameter.id == parameter_id:
                parameter.value = value
                return


class ParameterControllerTests(unittest.TestCase):
    def test_module_import_keeps_tkinter_lazy(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import live2d_adapter.parameter_controls; "
                "assert 'tkinter' not in sys.modules",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def make_model(self) -> FakeModel:
        return FakeModel(
            [
                FakeParameter("ParamEyeLOpen", 0.0, 1.0, 0.82, 0.7),
                FakeParameter("ParamEyeBallX", -12.5, 17.5, 3.0, -2.0),
                FakeParameter("ParamMouthOpenY", 0.0, 2.0, 0.25, 1.0),
                FakeParameter("CustomTail", -4.0, 6.0, -1.5, 0.5),
            ]
        )

    def test_binding_uses_real_ranges_defaults_and_available_labels(self) -> None:
        model = self.make_model()
        adapter = SimpleNamespace(
            metadata=SimpleNamespace(
                parameter_display_names={"ParamEyeLOpen": "左眼开合（模型名称）"}
            )
        )
        controller = ParameterController()

        specs = controller.bind_model(model, adapter)
        by_id = {spec["id"]: spec for spec in specs}

        self.assertEqual(by_id["ParamEyeBallX"]["minimum"], -12.5)
        self.assertEqual(by_id["ParamEyeBallX"]["maximum"], 17.5)
        self.assertEqual(by_id["ParamEyeBallX"]["default"], 3.0)
        self.assertEqual(by_id["ParamEyeBallX"]["value"], -2.0)
        self.assertEqual(by_id["ParamEyeLOpen"]["label"], "左眼开合（模型名称）")
        self.assertEqual(by_id["ParamEyeLOpen"]["group"], "眼睛")
        self.assertEqual(by_id["CustomTail"]["group"], "其他")
        self.assertFalse(any(spec["manual"] for spec in specs))

    def test_set_value_clamps_and_rejects_invalid_or_unknown_values(self) -> None:
        controller = ParameterController()
        controller.bind_model(self.make_model())

        self.assertTrue(controller.set_value("ParamEyeBallX", 99))
        self.assertEqual(
            next(spec["value"] for spec in controller.snapshot() if spec["id"] == "ParamEyeBallX"),
            17.5,
        )
        self.assertTrue(controller.set_value("ParamEyeBallX", -100))
        self.assertFalse(controller.set_value("ParamEyeBallX", math.nan))
        self.assertFalse(controller.set_value("ParamEyeBallX", math.inf))
        self.assertFalse(controller.set_value("missing", 1.0))

    def test_manual_values_win_after_each_update_until_released(self) -> None:
        model = self.make_model()
        controller = ParameterController()
        controller.bind_model(model)
        self.assertTrue(controller.set_value("ParamEyeBallX", 13.25))

        for _ in range(3):
            model.Update()
            controller.apply(model)
            value = next(p.value for p in model.parameters if p.id == "ParamEyeBallX")
            self.assertEqual(value, 13.25)

        self.assertEqual(model._model.writes, [("ParamEyeBallX", 13.25)] * 3)

    def test_nonmanual_snapshot_refreshes_from_model_at_five_hz(self) -> None:
        model = self.make_model()
        controller = ParameterController()
        controller.bind_model(model)
        model.Update()
        controller.apply(model)
        current = {spec["id"]: spec["value"] for spec in controller.snapshot()}
        self.assertEqual(current["ParamEyeLOpen"], model.parameters[0].value)

        model.parameters[0].value = 0.91
        controller.apply(model)
        self.assertEqual(
            next(
                spec["value"]
                for spec in controller.snapshot()
                if spec["id"] == "ParamEyeLOpen"
            ),
            current["ParamEyeLOpen"],
        )

        controller._last_value_refresh_at = time.monotonic() - 0.21
        controller.apply(model)
        self.assertEqual(
            next(
                spec["value"]
                for spec in controller.snapshot()
                if spec["id"] == "ParamEyeLOpen"
            ),
            0.91,
        )

    def test_group_reset_writes_default_once_and_releases_manual_lock(self) -> None:
        model = self.make_model()
        controller = ParameterController()
        controller.bind_model(model)
        controller.set_value("ParamEyeLOpen", 0.2)
        controller.set_value("ParamEyeBallX", 10.0)
        controller.set_value("ParamMouthOpenY", 1.7)

        controller.reset("眼睛")
        specs = {spec["id"]: spec for spec in controller.snapshot()}
        self.assertFalse(specs["ParamEyeLOpen"]["manual"])
        self.assertFalse(specs["ParamEyeBallX"]["manual"])
        self.assertTrue(specs["ParamMouthOpenY"]["manual"])
        controller.apply(model)
        self.assertEqual(
            model._model.writes,
            [
                ("ParamEyeLOpen", 0.82),
                ("ParamEyeBallX", 3.0),
                ("ParamMouthOpenY", 1.7),
            ],
        )

        model.Update()
        controller.apply(model)
        self.assertEqual(
            model._model.writes[-1],
            ("ParamMouthOpenY", 1.7),
        )
        writes_after_group_reset = len(model._model.writes)

        controller.reset()
        controller.apply(model)
        self.assertEqual(
            model._model.writes[-1],
            ("ParamMouthOpenY", 0.25),
        )
        writes_after_all_reset = len(model._model.writes)
        model.Update()
        controller.apply(model)
        self.assertEqual(len(model._model.writes), writes_after_all_reset)
        self.assertGreater(writes_after_all_reset, writes_after_group_reset)

    def test_rebinding_discards_old_model_overrides(self) -> None:
        old_model = self.make_model()
        new_model = FakeModel([FakeParameter("NewParam", -3.0, 2.0, 0.5, 0.0)])
        controller = ParameterController()
        controller.bind_model(old_model)
        controller.set_value("ParamEyeBallX", 11.0)

        specs = controller.bind_model(new_model)
        self.assertEqual([spec["id"] for spec in specs], ["NewParam"])
        self.assertFalse(controller.set_value("ParamEyeBallX", 4.0))
        controller.apply(old_model)
        controller.apply(new_model)
        self.assertEqual(old_model._model.writes, [])
        self.assertEqual(new_model._model.writes, [])

    def test_unbind_releases_model_and_makes_late_operations_noop(self) -> None:
        model = self.make_model()
        controller = ParameterController()
        controller.bind_model(model)
        controller.set_value("ParamEyeBallX", 11.0)
        model_ref = weakref.ref(model)
        del model
        gc.collect()
        self.assertIsNotNone(model_ref())

        controller.unbind_model()
        gc.collect()

        self.assertIsNone(model_ref())
        self.assertEqual(controller.snapshot(), [])
        self.assertFalse(controller.set_value("ParamEyeBallX", 2.0))
        controller.reset()
        controller.reset("眼睛")
        controller.apply(None)
        self.assertEqual(controller.snapshot(), [])

    def test_public_wrapper_fallback_is_used_when_native_setter_is_absent(self) -> None:
        model = FallbackModel([FakeParameter("ParamMouthOpenY", 0, 1, 0.3, 0.0)])
        logger = SimpleNamespace(warnings=[], warning=None)
        logger.warning = logger.warnings.append
        controller = ParameterController(logger)
        controller.bind_model(model)
        controller.set_value("ParamMouthOpenY", 0.8)
        controller.apply(model)

        self.assertEqual(model.writes, [("ParamMouthOpenY", 0.8)])
        self.assertEqual(len(logger.warnings), 1)

    def test_wrapper_fallback_reset_restores_model_default_once(self) -> None:
        model = FallbackModel([FakeParameter("ParamMouthOpenY", 0, 1, 0.3, 0.0)])
        controller = ParameterController()
        controller.bind_model(model)
        controller.set_value("ParamMouthOpenY", 0.8)
        controller.apply(model)
        controller.reset()
        controller.apply(model)

        self.assertEqual(
            model.writes,
            [("ParamMouthOpenY", 0.8), ("ParamMouthOpenY", 0.3)],
        )
        controller.apply(model)
        self.assertEqual(len(model.writes), 2)


class ParameterPanelTests(unittest.TestCase):
    def test_tk_panel_builds_and_does_not_fight_active_slider(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        changes: list[tuple[str, float]] = []
        resets: list[str | None] = []
        panel = ParameterPanel(root, lambda pid, value: changes.append((pid, value)), resets.append)
        specs = [
            {
                "id": "ParamEyeLOpen",
                "label": "左眼睁开",
                "group": "眼睛",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.9,
                "value": 0.6,
                "manual": False,
            }
        ]
        try:
            panel.show(specs)
            root.update_idletasks()
            self.assertGreater(panel.winfo_id(), 0)
            self.assertTrue(panel.window.overrideredirect())
            self.assertEqual(panel._close_button.cget("text"), "×")
            work_left, work_top, work_right, work_bottom = panel._work_area()
            self.assertGreaterEqual(panel.window.winfo_rootx(), work_left)
            self.assertGreaterEqual(panel.window.winfo_rooty(), work_top)
            self.assertLessEqual(
                panel.window.winfo_rootx() + panel.window.winfo_width(),
                work_right,
            )
            self.assertLessEqual(
                panel.window.winfo_rooty() + panel.window.winfo_height(),
                work_bottom,
            )
            self.assertIn("ParamEyeLOpen", panel._items)
            self.assertEqual(
                panel._items["ParamEyeLOpen"]["manual_label"].cget("text"),
                "",
            )
            original_value = float(panel._items["ParamEyeLOpen"]["scale"].get())
            panel._dragging.add("ParamEyeLOpen")
            self.assertTrue(panel.is_interacting())
            changed_specs = [dict(specs[0], value=0.95)]
            panel.update_specs(changed_specs)
            self.assertEqual(
                float(panel._items["ParamEyeLOpen"]["scale"].get()),
                original_value,
            )
            panel._dragging.clear()
            self.assertFalse(panel.is_interacting())
            panel.update_specs(changed_specs)
            self.assertAlmostEqual(
                float(panel._items["ParamEyeLOpen"]["scale"].get()),
                0.95,
                places=2,
            )
            slider = panel._items["ParamEyeLOpen"]["scale"]
            self.assertGreaterEqual(len(slider.canvas.find_all()), 3)
            slider.canvas.event_generate("<ButtonPress-1>", x=280, y=17)
            root.update()
            self.assertIn("ParamEyeLOpen", panel._dragging)
            self.assertTrue(panel.is_interacting())
            slider.canvas.event_generate("<B1-Motion>", x=320, y=17)
            root.update()
            self.assertTrue(changes)
            slider.canvas.event_generate("<ButtonRelease-1>", x=320, y=17)
            root.update()
            self.assertNotIn("ParamEyeLOpen", panel._dragging)
            self.assertFalse(panel.is_interacting())

            original_x = panel.window.winfo_x()
            original_y = panel.window.winfo_y()
            panel._start_window_drag(
                SimpleNamespace(x_root=original_x + 20, y_root=original_y + 20)
            )
            self.assertTrue(panel.is_interacting())
            panel._drag_window(
                SimpleNamespace(x_root=original_x + 55, y_root=original_y + 45)
            )
            root.update_idletasks()
            self.assertEqual(
                panel.window.winfo_x(),
                min(max(original_x + 35, work_left), work_right - panel.window.winfo_width()),
            )
            self.assertEqual(
                panel.window.winfo_y(),
                min(max(original_y + 25, work_top), work_bottom - panel.window.winfo_height()),
            )
            panel._end_window_drag(SimpleNamespace())
            self.assertIsNone(panel._window_drag_origin)
            self.assertFalse(panel.is_interacting())

            self.assertEqual(panel.window.state(), "normal")
            panel._close_button.invoke()
            root.update_idletasks()
            self.assertEqual(panel.window.state(), "withdrawn")
            self.assertFalse(panel.is_interacting())
            panel.hide()
            panel.show(changed_specs)
            root.update_idletasks()
            self.assertEqual(panel.window.state(), "normal")
        finally:
            panel.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
