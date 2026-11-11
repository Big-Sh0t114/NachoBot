from __future__ import annotations

import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desktop_pet_adapter.config import (
    ConfigError,
    DesktopGuiTarget,
    DesktopInteractionConfig,
    load_config,
)
from desktop_pet_adapter.desktop_interactions import DesktopInteractionSession
from desktop_pet_adapter.desktop_movement import (
    MovementPlan,
    fall_to_work_area,
    plan_journey,
    revalidate_plan,
    step_journey,
)
from desktop_pet_adapter.desktop_scene import (
    DesktopIcon,
    DesktopSceneProvider,
    SceneSnapshot,
    SceneWindow,
    _IconCache,
)
from desktop_pet_adapter.desktop_uia import DesktopUIA, target_is_safe
from desktop_pet_adapter.desktop_worker import DesktopInteractionWorker, DesktopWorkResult

WORK_AREA = (0, 0, 1920, 1080)
VIEWPORT = (520, 760)
ANCHOR = (0.5, 0.9)


def _window(
    hwnd: int = 101,
    rect: tuple[int, int, int, int] = (600, 100, 1500, 950),
    *,
    z_order: int = 0,
    foreground: bool = False,
    movable: bool = False,
    title: str = "Fixture window",
) -> SceneWindow:
    return SceneWindow(
        hwnd=hwnd,
        pid=2000 + hwnd,
        process_name="fixture.exe",
        title=title,
        rect=rect,
        z_order=z_order,
        foreground=foreground,
        movable=movable,
    )


def _scene(
    windows: tuple[SceneWindow, ...] = (),
    icons: tuple[DesktopIcon, ...] = (),
    *,
    captured_at: float = 0.0,
    foreground_hwnd: int | None = None,
    system_idle_seconds: float = 100.0,
    fullscreen: bool = False,
) -> SceneSnapshot:
    return SceneSnapshot(
        captured_at=captured_at,
        windows=windows,
        icons=icons,
        foreground_hwnd=foreground_hwnd,
        cursor=(900, 800),
        work_area=WORK_AREA,
        system_idle_seconds=system_idle_seconds,
        fullscreen=fullscreen,
    )


class MovementPlanningTests(unittest.TestCase):
    def test_reachable_top_edge_uses_full_viewport_clamping(self) -> None:
        target = _window(rect=(600, 700, 1500, 1000))
        plan = plan_journey(
            _scene((target,)),
            current=(800, 220),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.attached)
        self.assertAlmostEqual(plan.goal[1] + ANCHOR[1] * VIEWPORT[1], target.rect[1])
        self.assertGreaterEqual(plan.goal[1], WORK_AREA[1])
        self.assertLessEqual(plan.goal[1] + VIEWPORT[1], WORK_AREA[3])

    def test_high_window_climbs_only_the_reachable_connected_side(self) -> None:
        target = _window(rect=(600, 100, 1500, 950))
        plan = plan_journey(
            _scene((target,)),
            current=(800, 300),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.attached)
        self.assertGreaterEqual(len(plan.waypoints), 2)
        self.assertEqual(plan.goal[1], 0)
        self.assertNotAlmostEqual(plan.goal[1] + ANCHOR[1] * VIEWPORT[1], target.rect[1])
        position = (800.0, 300.0)
        index = 0
        arrived = False
        for _ in range(500):
            position, index, arrived = step_journey(
                plan, position, 0.1, speed_px_per_second=600, waypoint_index=index
            )
            if arrived:
                break
        self.assertTrue(arrived)
        self.assertEqual(position, plan.goal)

    def test_disconnected_visible_side_segment_is_not_a_climb_route(self) -> None:
        occluder = _window(
            hwnd=202,
            rect=(550, 90, 1000, 420),
            z_order=0,
            title="Occluder",
        )
        right_occluder = _window(
            hwnd=303,
            rect=(1200, 90, 1550, 420),
            z_order=0,
            title="Right occluder",
        )
        target = _window(
            hwnd=101,
            rect=(600, 100, 1500, 950),
            z_order=1,
        )
        plan = plan_journey(
            _scene((occluder, right_occluder, target)),
            current=(800, 300),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNone(plan)

    def test_target_loss_falls_back_to_a_reachable_work_area_position(self) -> None:
        target = _window(rect=(600, 700, 1500, 1000))
        original = plan_journey(
            _scene((target,)),
            current=(800, 300),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNotNone(original)
        self.assertIsNone(
            revalidate_plan(
                original,
                _scene((), captured_at=1.0),
                current=(900, 400),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                foot_anchor=ANCHOR,
            )
        )
        recovery = fall_to_work_area((900, 400), VIEWPORT, WORK_AREA)
        self.assertFalse(recovery.attached)
        self.assertEqual(recovery.goal[1], WORK_AREA[3] - VIEWPORT[1])

    def test_icon_revalidation_uses_viewport_then_work_area_order(self) -> None:
        icon = DesktopIcon("pidl:test", "Shortcut", 800, 760)
        scene = _scene(icons=(icon,))
        plan = plan_journey(
            scene,
            current=(600, 200),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            allow_windows=False,
            allow_icons=True,
        )
        self.assertIsNotNone(plan)
        refreshed = revalidate_plan(
            plan,
            scene,
            current=(600, 200),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
        )
        self.assertIsNotNone(refreshed)
        self.assertGreaterEqual(refreshed.goal[0], WORK_AREA[0])
        self.assertLessEqual(refreshed.goal[0] + VIEWPORT[0], WORK_AREA[2])


class _FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _MemoryWorker:
    def __init__(self) -> None:
        self.generation = 0
        self.submitted: list[tuple[str, object, dict[str, object]]] = []
        self.results = []
        self.poll_calls: list[dict[str, object]] = []

    def start(self) -> None:
        return None

    def invalidate(self) -> int:
        self.generation += 1
        return self.generation

    def submit(self, kind, payload=None, **kwargs):
        self.submitted.append((kind, payload, kwargs))
        return f"job-{len(self.submitted)}"

    def poll(self, **kwargs):
        self.poll_calls.append(kwargs)
        results, self.results = self.results, []
        return results

    def stop(self, **_kwargs):
        return None

    @property
    def alive(self):
        return False


class InteractionSessionTests(unittest.TestCase):
    def _config(self, **overrides) -> DesktopInteractionConfig:
        base = DesktopInteractionConfig(
            enabled=True,
            idle_interval_seconds=15,
            scene_interval_seconds=0.5,
            action_timeout_seconds=1,
            target_cooldown_seconds=30,
            icon_approach=False,
            move_windows=False,
            move_icons=False,
            gui_inspection=False,
            gui_actions=False,
        )
        return replace(base, **overrides)

    def test_pause_and_pet_drag_stop_movement_immediately(self) -> None:
        clock = _FakeClock(20)
        worker = _MemoryWorker()
        session = DesktopInteractionSession(
            self._config(),
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        session._scene = _scene((_window(rect=(600, 700, 1500, 1000)),), captured_at=20)
        session._next_decision_at = 0
        session._last_input_at = 0
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=None):
            first = session.update(
                position=(800, 220),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
            self.assertEqual(first.status, "approaching")
            self.assertIsNotNone(session._plan)
            before = (800.0, 220.0)
            drag = session.update(
                position=before,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=True,
            )
        self.assertEqual(drag.position, before)
        self.assertIsNone(session._plan)
        self.assertFalse(drag.attached)

    def test_unarrived_window_and_icon_paths_continue_during_user_input(self) -> None:
        clock = _FakeClock(30)
        window = _window(rect=(600, 100, 1500, 950))
        icon = DesktopIcon("pidl:shortcut", "Shortcut", 1500, 800)
        cases = (
            (
                _scene((window,), captured_at=clock.value),
                (100.0, 100.0),
                dict(allow_windows=True, allow_icons=False),
            ),
            (
                _scene(icons=(icon,), captured_at=clock.value),
                (100.0, 100.0),
                dict(allow_windows=False, allow_icons=True),
            ),
        )

        for scene, start, plan_options in cases:
            with self.subTest(target_kind="window" if scene.windows else "icon"):
                worker = _MemoryWorker()
                session = DesktopInteractionSession(
                    self._config(follow_foreground_window=False),
                    own_hwnd=None,
                    own_pid=1,
                    logger=Mock(),
                    worker=worker,
                    clock=clock,
                )
                plan = plan_journey(
                    scene,
                    current=start,
                    viewport=VIEWPORT,
                    work_area=WORK_AREA,
                    foot_anchor=ANCHOR,
                    **plan_options,
                )
                self.assertIsNotNone(plan)
                session._scene = scene
                session._plan = plan
                session._plan_scene_signature = session._target_signature(scene, plan)
                session._last_scene_at = scene.captured_at
                current = start
                for _ in range(3):
                    session.note_user_input(pet_dragging=False)
                    clock.value += 0.1
                    with patch(
                        "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
                        return_value=0,
                    ):
                        update = session.update(
                            position=current,
                            viewport=VIEWPORT,
                            work_area=WORK_AREA,
                            delta_time=0.1,
                            visible=True,
                            pet_dragging=False,
                        )
                    self.assertNotEqual(update.status, "user-active")
                    self.assertNotEqual(update.position, current)
                    current = update.position

    def test_recovery_path_continues_during_multiple_input_active_frames(self) -> None:
        clock = _FakeClock(30)
        worker = _MemoryWorker()
        session = DesktopInteractionSession(
            self._config(follow_foreground_window=False),
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        old_scene = _scene(
            (_window(rect=(600, 100, 1500, 950)),),
            captured_at=29,
        )
        session._scene = _scene((), captured_at=clock.value)
        session._plan = plan_journey(
            old_scene,
            current=(100.0, 100.0),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNotNone(session._plan)
        session._plan_scene_signature = session._target_signature(old_scene, session._plan)
        session._last_scene_at = old_scene.captured_at
        current = (100.0, 100.0)

        for _ in range(3):
            session.note_user_input(pet_dragging=False)
            clock.value += 0.1
            with patch(
                "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
                return_value=0,
            ):
                update = session.update(
                    position=current,
                    viewport=VIEWPORT,
                    work_area=WORK_AREA,
                    delta_time=0.1,
                    visible=True,
                    pet_dragging=False,
                )
            self.assertEqual(update.status, "falling")
            self.assertEqual(session._plan.target_kind, "recovery")
            self.assertNotEqual(update.position, current)
            current = update.position

    def test_global_activity_blocks_a_new_approach_plan(self) -> None:
        clock = _FakeClock(30)
        session = DesktopInteractionSession(
            self._config(),
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=_MemoryWorker(),
            clock=clock,
        )
        session._scene = _scene((_window(rect=(600, 100, 1500, 950)),), captured_at=30)
        session._next_decision_at = 0
        session.note_user_input(pet_dragging=False)
        with patch(
            "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
            return_value=0,
        ):
            update = session.update(
                position=(100.0, 100.0),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertIsNone(session._plan)
        self.assertEqual(update.position, (100.0, 100.0))

    def test_snapshots_survive_input_invalidation_and_keep_activity_polling(self) -> None:
        clock = _FakeClock(30)
        worker = _MemoryWorker()
        session = DesktopInteractionSession(
            self._config(),
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        session._scene = _scene((), captured_at=clock.value)
        session._next_snapshot_at = 0
        with patch(
            "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
            return_value=None,
        ):
            session.update(
                position=(100.0, 100.0),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        session.note_user_input(pet_dragging=False)
        clock.value += 0.1
        with patch(
            "desktop_pet_adapter.desktop_interactions._system_idle_seconds",
            return_value=0,
        ):
            session.update(
                position=(100.0, 100.0),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )

        snapshot = next(item for item in worker.submitted if item[0] == "snapshot")
        self.assertTrue(snapshot[2]["survive_invalidation"])
        self.assertIn("snapshot", worker.poll_calls[-1]["allow_stale_kinds"])
        self.assertGreater(worker.generation, 0)

    def test_global_input_suppresses_native_moves_but_keeps_foreground_follow(self) -> None:
        clock = _FakeClock(30)
        worker = _MemoryWorker()
        config = self._config(
            window_approach=False,
            move_windows=True,
        )
        session = DesktopInteractionSession(
            config,
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        foreground = _window(foreground=True, rect=(600, 700, 1500, 1000), movable=True)
        session._scene = _scene((foreground,), captured_at=30, foreground_hwnd=foreground.hwnd)
        session._last_input_at = 0
        session._next_decision_at = 0
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=0):
            session.update(
                position=(800, 220),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
        self.assertNotIn("move_window", [kind for kind, _payload, _kwargs in worker.submitted])

        plan = MovementPlan(
            "window",
            str(foreground.hwnd),
            ((900.0, 220.0),),
            (900.0, 220.0),
            True,
            "approach_window",
        )
        session._plan = plan
        session._attached = True
        session._plan_scene_signature = session._target_signature(session.scene, plan)
        session.note_user_input(pet_dragging=False)
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=0):
            followed = session.update(
                position=(800, 220),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertEqual(followed.status, "following")
        self.assertGreater(followed.position[0], 800)

    def test_perch_dwell_expires_and_allows_a_later_choice(self) -> None:
        clock = _FakeClock(20)
        worker = _MemoryWorker()
        session = DesktopInteractionSession(
            self._config(),
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
            choice=lambda choices: next(choice for choice in choices if choice[0] == "approach"),
        )
        scene = _scene((_window(rect=(600, 700, 1500, 1000)),), captured_at=20)
        session._scene = scene
        session._last_input_at = 0
        session._next_decision_at = 0
        session._plan = MovementPlan(
            "window",
            "101",
            ((800.0, 220.0),),
            (800.0, 220.0),
            True,
            "approach_window",
        )
        session._plan_scene_signature = session._target_signature(scene, session._plan)
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=None):
            arrived = session.update(
                position=(800, 220),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
            release_at = session._arrival_release_at
            clock.value = 24
            session._scene = replace(scene, captured_at=24)
            perched = session.update(
                position=arrived.position,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
            self.assertEqual(perched.status, "perched")
            self.assertEqual(session._arrival_release_at, release_at)
            clock.value = 26
            session._scene = replace(scene, captured_at=26)
            next_action = session.update(
                position=arrived.position,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
        self.assertIsNone(session._arrival_release_at)
        self.assertEqual(next_action.status, "approaching")

    def _arrived_window_session(self, *, clock: _FakeClock):
        worker = _MemoryWorker()
        config = self._config(follow_foreground_window=False)
        window = _window(
            rect=(600, 904, 1500, 1157),
            foreground=False,
        )
        scene = _scene((window,), captured_at=clock.value)
        plan = plan_journey(
            scene,
            current=(800, 300),
            viewport=VIEWPORT,
            work_area=WORK_AREA,
            foot_anchor=ANCHOR,
            allow_icons=False,
        )
        self.assertIsNotNone(plan)
        session = DesktopInteractionSession(
            config,
            own_hwnd=None,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        session._scene = scene
        session._plan = plan
        session._waypoint_index = len(plan.waypoints)
        session._plan_scene_signature = session._target_signature(scene, plan)
        session._last_scene_at = scene.captured_at
        session._arrival_release_at = clock.value + 70
        session._attached = True
        session._last_input_at = 0
        session._next_decision_at = session._arrival_release_at
        return session, worker, window, plan

    def test_arrived_window_tracks_user_moved_target_during_dwell(self) -> None:
        clock = _FakeClock(30)
        session, worker, window, plan = self._arrived_window_session(clock=clock)
        moved_window = replace(window, rect=(700, 904, 1600, 1157))
        session._scene = _scene((moved_window,), captured_at=31)
        old_position = plan.goal
        clock.value = 31
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=0):
            update = session.update(
                position=old_position,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertEqual(update.status, "following")
        self.assertTrue(update.attached)
        self.assertGreater(update.position[0], old_position[0])
        self.assertNotEqual(session._plan.goal, old_position)
        self.assertNotIn("move_window", [kind for kind, _payload, _kwargs in worker.submitted])

    def test_arrived_window_falls_when_target_disappears_or_is_minimized(self) -> None:
        clock = _FakeClock(30)
        session, _worker, _window_value, plan = self._arrived_window_session(clock=clock)
        session._scene = _scene((), captured_at=31)
        clock.value = 31
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=0):
            update = session.update(
                position=plan.goal,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertEqual(update.status, "falling")
        self.assertFalse(update.attached)
        self.assertEqual(session._plan.target_kind, "recovery")
        self.assertIsNone(session._arrival_release_at)

    def test_arrived_window_falls_when_occluded_during_dwell(self) -> None:
        clock = _FakeClock(30)
        session, _worker, target, plan = self._arrived_window_session(clock=clock)
        occluder = _window(
            hwnd=909,
            rect=(550, 850, 1550, 950),
            z_order=0,
            title="Occluder",
        )
        visible_target = replace(target, z_order=1)
        session._scene = _scene((occluder, visible_target), captured_at=31)
        clock.value = 31
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=None):
            update = session.update(
                position=plan.goal,
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=0.1,
                visible=True,
                pet_dragging=False,
            )
        self.assertEqual(update.status, "falling")
        self.assertFalse(update.attached)
        self.assertEqual(session._plan.target_kind, "recovery")
        self.assertIsNone(session._arrival_release_at)

    def test_tray_focus_keeps_manual_inspection_on_last_external_foreground(self) -> None:
        clock = _FakeClock(30)
        worker = _MemoryWorker()
        worker.own_pid = 1
        worker.own_hwnd = 500
        session = DesktopInteractionSession(
            self._config(gui_inspection=True),
            own_hwnd=500,
            own_pid=1,
            logger=Mock(),
            worker=worker,
            clock=clock,
        )
        external = _window(hwnd=101, foreground=True)
        foreground_scene = _scene(
            (external,), captured_at=30, foreground_hwnd=external.hwnd
        )
        worker.results.append(
            DesktopWorkResult(
                "external-scene",
                "snapshot",
                worker.generation,
                30,
                30,
                35,
                value=foreground_scene,
            )
        )
        with patch("desktop_pet_adapter.desktop_interactions._system_idle_seconds", return_value=None):
            session.update(
                position=(800, 200),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )

            # pystray makes our own tray HWND foreground while its menu is open.
            tray_scene = _scene((external,), captured_at=31, foreground_hwnd=500)
            worker.results.append(
                DesktopWorkResult(
                    "tray-scene",
                    "snapshot",
                    worker.generation,
                    31,
                    31,
                    36,
                    value=tray_scene,
                )
            )
            session.update(
                position=(800, 200),
                viewport=VIEWPORT,
                work_area=WORK_AREA,
                delta_time=1 / 60,
                visible=True,
                pet_dragging=False,
            )
        self.assertEqual(session._last_external_foreground.hwnd, external.hwnd)
        self.assertTrue(session.handle_command("desktop_gui_inspect"))
        kind, payload, kwargs = worker.submitted[-1]
        self.assertEqual(kind, "inspect_gui")
        self.assertEqual(
            payload,
            (external.hwnd, external.pid, external.process_name, external.title),
        )
        self.assertTrue(kwargs["survive_invalidation"])


class WorkerTests(unittest.TestCase):
    class Provider:
        def __init__(self, _hwnd, _pid, include_icons=True) -> None:
            self.include_icons = include_icons

        def snapshot(self):
            return _scene()

        def close(self):
            return None

    def _wait_result(self, worker, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            results = worker.poll()
            if results:
                return results
            time.sleep(0.005)
        return []

    def test_snapshot_result_survives_activity_invalidation(self) -> None:
        provider_started = threading.Event()
        initialize_provider = threading.Event()
        expected_scene = _scene(captured_at=time.monotonic())

        class SnapshotProvider(self.Provider):
            def snapshot(self):
                provider_started.set()
                return expected_scene

        def provider_factory(*_args, **_kwargs):
            provider_started.set()
            initialize_provider.wait(2)
            return SnapshotProvider(*_args, **_kwargs)

        # Hold provider initialization so invalidation lands before the queued
        # read-only snapshot can begin; its result is then admitted by the
        # session's snapshot-only stale-result allowance.
        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            timeout_seconds=3,
            scene_provider_factory=provider_factory,
            uia=SimpleNamespace(),
        )
        worker.submit("snapshot", survive_invalidation=True)
        self.assertTrue(provider_started.wait(0.5))
        worker.invalidate()
        initialize_provider.set()

        results = []
        deadline = time.monotonic() + 3
        try:
            while not results and time.monotonic() < deadline:
                results.extend(worker.poll(allow_stale_kinds={"snapshot"}))
                time.sleep(0.005)
        finally:
            initialize_provider.set()
            worker.stop()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "snapshot")
        self.assertIs(results[0].value, expected_scene)

    def test_expired_worker_operation_reports_timeout_without_blocking_caller(self) -> None:
        started = threading.Event()

        class SlowProvider(self.Provider):
            def snapshot(self):
                started.set()
                time.sleep(0.18)
                return _scene()

        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            timeout_seconds=0.03,
            scene_provider_factory=SlowProvider,
            uia=SimpleNamespace(),
        )
        worker.submit("snapshot", timeout_seconds=0.03)
        self.assertTrue(started.wait(0.5))
        time.sleep(0.04)
        result = self._wait_result(worker, timeout=0.25)
        self.assertTrue(result)
        self.assertTrue(result[0].timed_out)
        before = time.monotonic()
        worker.stop(join_timeout=0.01)
        self.assertLess(time.monotonic() - before, 0.1)
        time.sleep(0.15)

    def test_worker_failure_is_returned_as_a_result(self) -> None:
        class FailingProvider(self.Provider):
            def snapshot(self):
                raise RuntimeError("fixture failure")

        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            scene_provider_factory=FailingProvider,
            uia=SimpleNamespace(),
        )
        worker.submit("snapshot")
        results = self._wait_result(worker)
        worker.stop()
        self.assertEqual(len(results), 1)
        self.assertIn("fixture failure", results[0].error)

    def test_cancelled_delayed_uia_lookup_does_not_mutate(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class GuardedUIA:
            mutated = False

            def execute(self, _target, *, action_allowed=None):
                entered.set()
                release.wait(0.5)
                if action_allowed is not None and not action_allowed():
                    return False
                self.mutated = True
                return True

        uia = GuardedUIA()
        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            scene_provider_factory=self.Provider,
            uia=uia,
        )
        worker.submit("gui_action", object())
        self.assertTrue(entered.wait(0.5))
        worker.invalidate()
        release.set()
        time.sleep(0.05)
        self.assertFalse(uia.mutated)
        self.assertEqual(worker.poll(), [])
        worker.stop()

    def test_worker_initialization_failure_is_not_silently_stuck(self) -> None:
        def fail_factory(*_args, **_kwargs):
            raise RuntimeError("native provider failed to initialize")

        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            scene_provider_factory=fail_factory,
            uia=SimpleNamespace(),
        )
        worker.submit("snapshot")
        results = self._wait_result(worker)
        worker.stop()
        self.assertEqual(len(results), 1)
        self.assertIn("failed to initialize", results[0].error)

    def test_worker_inspects_the_sampled_external_window_identity(self) -> None:
        captured: list[tuple[object, ...]] = []

        class IdentityUIA:
            def inspect_window_identity(self, *identity):
                captured.append(identity)
                return ("safe-control",)

        worker = DesktopInteractionWorker(
            own_hwnd=None,
            own_pid=1,
            scene_provider_factory=self.Provider,
            uia=IdentityUIA(),
        )
        payload = (123, 456, "fixture.exe", "Fixture window")
        worker.submit("inspect_gui", payload=payload)
        results = self._wait_result(worker)
        worker.stop()
        self.assertEqual(captured, [payload])
        self.assertEqual(results[0].value, ("safe-control",))


class DesktopSceneRecoveryTests(unittest.TestCase):
    @staticmethod
    def _window_provider(initial_rect=(100, 200, 900, 800)):
        state = {
            "pid": 1234,
            "process_name": "fixture.exe",
            "title": "Fixture window",
            "visual": initial_rect,
            "native": initial_rect,
        }
        win32con = types.ModuleType("win32con")
        win32con.SWP_NOSIZE = 1
        win32con.SWP_NOZORDER = 2
        win32con.SWP_NOACTIVATE = 4
        win32con.SWP_NOOWNERZORDER = 8
        win32gui = types.ModuleType("win32gui")

        def set_window_pos(_hwnd, _insert_after, x, y, _width, _height, _flags):
            left, top, right, bottom = state["native"]
            width, height = right - left, bottom - top
            state["native"] = (int(x), int(y), int(x) + width, int(y) + height)
            state["visual"] = state["native"]

        win32gui.SetWindowPos = set_window_pos
        win32process = types.ModuleType("win32process")
        provider = DesktopSceneProvider(own_hwnd=None, own_pid=1, include_icons=False)

        def identity(_hwnd, _win32gui, _win32process):
            return (
                state["pid"],
                state["process_name"],
                state["title"],
                state["visual"],
                state["native"],
                True,
            )

        patches = (
            patch.dict(
                sys.modules,
                {
                    "win32con": win32con,
                    "win32gui": win32gui,
                    "win32process": win32process,
                },
            ),
            patch("desktop_pet_adapter.desktop_scene._is_windows", return_value=True),
            patch("desktop_pet_adapter.desktop_scene._physical_dpi_context", nullcontext),
            patch("desktop_pet_adapter.desktop_scene._window_identity", side_effect=identity),
            patch(
                "desktop_pet_adapter.desktop_scene._window_rect",
                side_effect=lambda _hwnd, _gui: state["visual"],
            ),
            patch(
                "desktop_pet_adapter.desktop_scene._window_native_rect",
                side_effect=lambda _hwnd, _gui: state["native"],
            ),
        )
        return provider, state, patches

    @staticmethod
    def _enter_patches(patches):
        stack = []
        for patcher in patches:
            stack.append(patcher)
            patcher.start()
        return stack

    @staticmethod
    def _exit_patches(stack):
        for patcher in reversed(stack):
            patcher.stop()

    def test_window_repeated_nudges_restore_after_user_reposition(self) -> None:
        provider, state, patches = self._window_provider()
        stack = self._enter_patches(patches)
        try:
            original = state["native"]
            first = provider.move_window(
                50, 1234, "fixture.exe", "Fixture window", original, 5, 0, 8
            )
            self.assertIsNotNone(first)
            second_origin = state["visual"]
            second = provider.move_window(
                50,
                1234,
                "fixture.exe",
                "Fixture window",
                second_origin,
                5,
                0,
                8,
            )
            self.assertIsNotNone(second)
            self.assertEqual(provider._window_recovery[50].original_rect, original)

            user_position = (300, 400, 1100, 1000)
            state["native"] = state["visual"] = user_position
            moved = provider.move_window(
                50,
                1234,
                "fixture.exe",
                "Fixture window",
                user_position,
                5,
                0,
                8,
            )
            self.assertIsNotNone(moved)
            self.assertEqual(provider._window_recovery[50].original_rect, user_position)
            self.assertEqual(provider.restore_windows(), 1)
            self.assertEqual(state["native"], user_position)
        finally:
            self._exit_patches(stack)

    def test_window_identity_change_starts_a_new_restore_origin(self) -> None:
        provider, state, patches = self._window_provider()
        stack = self._enter_patches(patches)
        try:
            original = state["native"]
            first = provider.move_window(
                50, 1234, "fixture.exe", "Fixture window", original, 5, 0, 8
            )
            self.assertIsNotNone(first)
            new_identity_origin = state["native"]
            state["pid"] = 4321
            state["process_name"] = "replacement.exe"
            state["title"] = "Replacement fixture"
            moved = provider.move_window(
                50,
                4321,
                "replacement.exe",
                "Replacement fixture",
                new_identity_origin,
                5,
                0,
                8,
            )
            self.assertIsNotNone(moved)
            self.assertEqual(
                provider._window_recovery[50].original_rect,
                new_identity_origin,
            )
            self.assertEqual(provider.restore_windows(), 1)
            self.assertEqual(state["native"], new_identity_origin)
        finally:
            self._exit_patches(stack)

    def test_icon_repeated_nudges_restore_to_user_repositioned_anchor(self) -> None:
        identity = "fixture-pidl"
        state = {"name": "Fixture shortcut", "anchor": (100, 100)}
        provider = DesktopSceneProvider(own_hwnd=None, own_pid=1, include_icons=True)
        provider._icon_cache[identity] = _IconCache(
            state["name"], state["anchor"], state["anchor"], True
        )

        def select_and_position(_view, _pidl, position):
            state["anchor"] = tuple(position)

        patches = (
            patch("desktop_pet_adapter.desktop_scene._is_windows", return_value=True),
            patch("desktop_pet_adapter.desktop_scene._physical_dpi_context", nullcontext),
            patch(
                "desktop_pet_adapter.desktop_scene._com_initialized",
                side_effect=lambda: nullcontext(),
            ),
            patch(
                "desktop_pet_adapter.desktop_scene._desktop_folder_view",
                side_effect=lambda: nullcontext((1, 2)),
            ),
            patch(
                "desktop_pet_adapter.desktop_scene._iter_folder_items",
                side_effect=lambda _view: iter((999,)),
            ),
            patch(
                "desktop_pet_adapter.desktop_scene._pidl_bytes",
                return_value=b"fixture-pidl-bytes",
            ),
            patch("desktop_pet_adapter.desktop_scene._icon_identity", return_value=identity),
            patch(
                "desktop_pet_adapter.desktop_scene._item_name",
                side_effect=lambda _folder, _pidl, _bytes: state["name"],
            ),
            patch(
                "desktop_pet_adapter.desktop_scene._item_position",
                side_effect=lambda _view, _pidl: state["anchor"],
            ),
            patch("desktop_pet_adapter.desktop_scene._icon_center", side_effect=lambda point: point),
            patch("desktop_pet_adapter.desktop_scene._is_recycle_bin", return_value=False),
            patch("desktop_pet_adapter.desktop_scene._auto_arrange_enabled", return_value=False),
            patch(
                "desktop_pet_adapter.desktop_scene._select_and_position",
                side_effect=select_and_position,
            ),
        )
        stack = self._enter_patches(patches)
        try:
            first = provider.move_icon(identity, state["name"], 105, 100, 8)
            self.assertIsNotNone(first)
            second = provider.move_icon(identity, state["name"], 110, 100, 8)
            self.assertIsNotNone(second)
            self.assertEqual(provider._icon_recovery[identity].original_anchor, (100, 100))

            user_anchor = (130, 100)
            state["anchor"] = user_anchor
            provider._icon_cache[identity] = _IconCache(
                state["name"], user_anchor, user_anchor, True
            )
            moved = provider.move_icon(identity, state["name"], 135, 100, 8)
            self.assertIsNotNone(moved)
            self.assertEqual(provider._icon_recovery[identity].original_anchor, user_anchor)
            self.assertEqual(provider.restore_icons(), 1)
            self.assertEqual(state["anchor"], user_anchor)
        finally:
            self._exit_patches(stack)


class UIATargetTests(unittest.TestCase):
    def test_destructive_names_are_never_eligible(self) -> None:
        for name in ("Delete", "Send message", "Purchase", "Close window"):
            target = DesktopGuiTarget(
                "fixture.exe", "Fixture", name, "Button", "invoke"
            )
            self.assertFalse(target_is_safe(target))
        safe = DesktopGuiTarget("fixture.exe", "Fixture", "Help", "Button", "invoke")
        self.assertTrue(target_is_safe(safe))

    def test_pause_or_stale_foreground_prevents_uia_invoke(self) -> None:
        import pywinauto

        invoke = Mock()
        wrapper = SimpleNamespace(
            element_info=SimpleNamespace(
                name="Fixture Safe Button",
                control_type="Button",
                automation_id="safe-button",
            ),
            is_enabled=Mock(return_value=True),
            iface_invoke=SimpleNamespace(Invoke=invoke),
        )
        window = SimpleNamespace(descendants=Mock(return_value=[wrapper]))
        desktop = Mock()
        desktop.window.return_value.wrapper_object.return_value = window
        target = DesktopGuiTarget(
            "fixture.exe",
            "Fixture",
            "Fixture Safe Button",
            "Button",
            "invoke",
            "safe-button",
        )
        identity = (123, "fixture.exe", "Fixture", 999)
        with (
            patch.object(DesktopUIA, "_foreground_identity", side_effect=[identity, identity]),
            patch.object(pywinauto, "Desktop", return_value=desktop),
        ):
            self.assertFalse(DesktopUIA().execute(target, action_allowed=lambda: False))
        invoke.assert_not_called()

    def test_foreground_change_after_lookup_prevents_invoke(self) -> None:
        import pywinauto

        invoke = Mock()
        wrapper = SimpleNamespace(
            element_info=SimpleNamespace(
                name="Fixture Safe Button",
                control_type="Button",
                automation_id="safe-button",
            ),
            is_enabled=Mock(return_value=True),
            iface_invoke=SimpleNamespace(Invoke=invoke),
        )
        window = SimpleNamespace(descendants=Mock(return_value=[wrapper]))
        desktop = Mock()
        desktop.window.return_value.wrapper_object.return_value = window
        original = (123, "fixture.exe", "Fixture", 999)
        changed = (456, "other.exe", "Other", 1000)
        target = DesktopGuiTarget(
            "fixture.exe", "Fixture", "Fixture Safe Button", "Button", "invoke", "safe-button"
        )
        with (
            patch.object(DesktopUIA, "_foreground_identity", side_effect=[original, changed]),
            patch.object(pywinauto, "Desktop", return_value=desktop),
        ):
            self.assertFalse(DesktopUIA().execute(target, action_allowed=lambda: True))
        invoke.assert_not_called()

    def test_sampled_window_inspection_rejects_stale_identity_before_lookup(self) -> None:
        uia = DesktopUIA()
        with (
            patch.object(
                uia,
                "_window_identity",
                return_value=("replacement.exe", "Replacement", 456),
            ),
            patch.object(uia, "_inspect_window") as inspect,
        ):
            targets = uia.inspect_window_identity(
                123,
                456,
                "fixture.exe",
                "Fixture",
            )
        self.assertEqual(targets, ())
        inspect.assert_not_called()

    def test_sampled_window_inspection_rejects_identity_change_during_lookup(self) -> None:
        uia = DesktopUIA()
        safe = DesktopGuiTarget("fixture.exe", "Fixture", "Help", "Button", "invoke")
        with (
            patch.object(
                uia,
                "_window_identity",
                side_effect=[
                    ("fixture.exe", "Fixture", 456),
                    ("replacement.exe", "Fixture", 456),
                ],
            ),
            patch.object(uia, "_inspect_window", return_value=(safe,)) as inspect,
            patch.object(
                uia,
                "_foreground_identity",
                return_value=(500, "pet.exe", "Tray", 1),
            ) as foreground,
        ):
            targets = uia.inspect_window_identity(123, 456, "fixture.exe", "Fixture")
        self.assertEqual(targets, ())
        inspect.assert_called_once_with(
            123,
            "fixture.exe",
            "Fixture",
            456,
            check_foreground=False,
        )
        foreground.assert_not_called()


class InteractionConfigTests(unittest.TestCase):
    def _config_path(self, root: Path, interaction_lines: str) -> Path:
        (root / "avatar.model3.json").write_text("{}", encoding="utf-8")
        path = root / "config.toml"
        path.write_text(
            "\n".join(
                (
                    "[renderer]",
                    'model_path = "avatar.model3.json"',
                    "[runtime]",
                    'mode = "desktop_pet"',
                    "[desktop_pet.interactions]",
                    interaction_lines,
                )
            ),
            encoding="utf-8",
        )
        return path

    def test_invalid_interaction_values_are_rejected(self) -> None:
        cases = (
            'enabled = "yes"',
            "max_icon_shift_px = 99",
            "idle_interval_seconds = 2",
            (
                'gui_targets = [{ process_name = "fixture.exe", '
                'window_title = "Fixture", control_name = "Help", '
                'control_type = "Button", action = "delete" }]'
            ),
        )
        for value in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                path = self._config_path(Path(temporary), value)
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_interactions_can_be_enabled_with_safe_gui_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._config_path(
                Path(temporary),
                "\n".join(
                    (
                        "enabled = true",
                        (
                            'gui_targets = [{ process_name = "fixture.exe", '
                            'window_title = "Fixture", control_name = "Help", '
                            'control_type = "Button", action = "invoke" }]'
                        ),
                    )
                ),
            )
            interactions = load_config(path).desktop_pet.interactions
        self.assertTrue(interactions.enabled)
        self.assertEqual(interactions.idle_interval_seconds, 90)
        self.assertEqual(interactions.gui_targets[0].control_name, "Help")


if __name__ == "__main__":
    unittest.main()
