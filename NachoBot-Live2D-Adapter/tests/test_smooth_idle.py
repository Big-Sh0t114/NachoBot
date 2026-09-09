from __future__ import annotations

import queue
import unittest
from pathlib import Path
from unittest import mock

from live2d_adapter.model_adapter import Live2DModelAdapter
from live2d_adapter.renderer import Live2DRenderer, _damped_step


class StubLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class StubNativeModel:
    def __init__(self) -> None:
        self.transient_writes: list[tuple[str, float, float]] = []

    def SetParameterValueById(
        self, parameter_id: str, value: float, weight: float
    ) -> None:
        self.transient_writes.append((parameter_id, value, weight))


class StubModel:
    def __init__(self) -> None:
        self._model = StubNativeModel()
        self.auto_breath: list[bool] = []
        self.finished = True
        self.values = [0.0] * 6
        self.parameter_writes: list[tuple[str, float, float]] = []
        self.started_motions: list[tuple[str, int, int]] = []
        self.stop_count = 0

    def SetAutoBreathEnable(self, enabled: bool) -> None:
        self.auto_breath.append(enabled)

    def IsMotionFinished(self) -> bool:
        return self.finished

    def GetParameterValue(self, index: int) -> float:
        return self.values[index]

    def SetParameterValue(self, parameter_id: str, value: float, weight: float) -> None:
        self.parameter_writes.append((parameter_id, value, weight))

    def StartMotion(self, group: str, index: int, priority: int) -> None:
        self.started_motions.append((group, index, priority))

    def StopAllMotions(self) -> None:
        self.stop_count += 1


class SmoothIdleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapter_root = Path(__file__).resolve().parents[1]
        cls.model_path = (
            adapter_root / "resources" / "NachoBot" / "Nachobot.model3.json"
        )

    def make_renderer(self) -> tuple[Live2DRenderer, StubModel]:
        adapter = Live2DModelAdapter.from_model_path(self.model_path)
        parameter_ids = (
            "ParamAngleX",
            "ParamAngleY",
            "ParamAngleZ",
            "ParamBodyAngleX",
            "ParamBodyAngleZ",
            "ParamBreath",
        )
        adapter.bind_runtime(parameter_ids=parameter_ids)
        renderer = Live2DRenderer(
            str(self.model_path),
            StubLogger(),
            queue.Queue(),
            model_adapter=adapter,
        )
        model = StubModel()
        renderer.model = model
        renderer._parameter_indexes = {
            parameter_id: index for index, parameter_id in enumerate(parameter_ids)
        }
        return renderer, model

    def test_physics_filter_limits_acceleration_after_a_target_jump(self) -> None:
        position = 0.0
        velocity = 0.0
        velocities = []
        for _ in range(60):
            previous_position = position
            position, velocity = _damped_step(
                position,
                velocity,
                1.0,
                1.0 / 60.0,
                2.0,
            )
            velocities.append((position - previous_position) * 60.0)

        accelerations = [
            (velocities[index] - velocities[index - 1]) * 60.0
            for index in range(1, len(velocities))
        ]
        self.assertLessEqual(max(abs(value) for value in accelerations), 1.000001)
        self.assertGreater(position, 0.5)

    def test_body_physics_smoothing_uses_a_transient_write(self) -> None:
        renderer, model = self.make_renderer()
        with mock.patch(
            "live2d_adapter.renderer.time.perf_counter", return_value=100.0
        ):
            renderer._configure_smooth_idle()
        renderer._standby_smoothing_active = True
        body_index = renderer._parameter_indexes["ParamBodyAngleZ"]
        model.values[body_index] = 1.0
        renderer._physics_filter_states["ParamBodyAngleZ"] = (0.0, 0.0)

        with mock.patch(
            "live2d_adapter.renderer.time.perf_counter", return_value=100.1
        ):
            renderer._smooth_physics_outputs()

        self.assertEqual(model.parameter_writes, [])
        self.assertEqual(len(model._model.transient_writes), 1)
        self.assertEqual(model._model.transient_writes[0][0], "ParamBodyAngleZ")

    def test_smoothing_restores_auto_breath_without_replacing_idle_parameters(self) -> None:
        renderer, model = self.make_renderer()
        with mock.patch(
            "live2d_adapter.renderer.time.perf_counter", return_value=100.0
        ):
            self.assertTrue(renderer._configure_smooth_idle())

        self.assertEqual(model.auto_breath, [True])
        self.assertEqual(model.parameter_writes, [])
        self.assertEqual(
            set(renderer._physics_output_param_ids),
            {
                "ParamAngleX",
                "ParamAngleZ",
                "ParamBodyAngleX",
                "ParamBodyAngleZ",
            },
        )

    def test_initial_idle_plays_once_then_enters_standby(self) -> None:
        renderer, model = self.make_renderer()
        renderer._start_idle_motion()
        renderer._return_to_standby_if_finished()
        renderer._return_to_standby_if_finished()

        self.assertEqual(model.started_motions, [("Idle", 0, 3)])
        self.assertFalse(renderer._motion_in_progress)
        self.assertTrue(renderer._standby_smoothing_active)

    def test_initial_idle_finish_preserves_filter_state(self) -> None:
        renderer, model = self.make_renderer()
        renderer._start_idle_motion()
        renderer._physics_filter_states["ParamAngleX"] = (2.0, 0.5)

        renderer._return_to_standby_if_finished()

        self.assertEqual(model.started_motions, [("Idle", 0, 3)])
        self.assertEqual(
            renderer._physics_filter_states["ParamAngleX"],
            (2.0, 0.5),
        )

    def test_finished_action_returns_to_standby_without_idle_zero(self) -> None:
        renderer, model = self.make_renderer()
        model.finished = False
        renderer._start_motion("Nod")
        self.assertFalse(renderer._standby_smoothing_active)

        model.finished = True
        renderer._return_to_standby_if_finished()

        self.assertEqual(model.started_motions, [("Nod", 0, 3)])
        self.assertFalse(renderer._motion_in_progress)
        self.assertTrue(renderer._standby_smoothing_active)

    def test_active_non_idle_motion_is_not_smoothed(self) -> None:
        renderer, model = self.make_renderer()
        renderer._configure_smooth_idle()
        renderer._standby_smoothing_active = False
        renderer._physics_filter_states["ParamAngleX"] = (0.0, 0.0)
        model.values[renderer._parameter_indexes["ParamAngleX"]] = 1.0

        renderer._smooth_physics_outputs()

        self.assertEqual(model._model.transient_writes, [])
        self.assertEqual(renderer._physics_filter_states, {})

    def test_explicit_idle_request_enters_standby_without_replaying_idle_zero(self) -> None:
        renderer, model = self.make_renderer()
        with mock.patch(
            "live2d_adapter.renderer.time.perf_counter", return_value=100.0
        ):
            renderer._configure_smooth_idle()

        renderer._start_motion("Idle")

        self.assertEqual(model.stop_count, 1)
        self.assertEqual(model.started_motions, [])
        self.assertTrue(renderer._standby_smoothing_active)


if __name__ == "__main__":
    unittest.main()
