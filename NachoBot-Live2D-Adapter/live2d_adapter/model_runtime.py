"""Cubism model runtime that composes into a host-owned OpenGL render loop.

This module owns only Live2D SDK/model state. It does not create a pygame window,
read host configuration, process UI events, or know about chat/business state.
Call ``initialize`` and ``dispose`` on the render thread while the host's GL
context is current.
"""

from __future__ import annotations

import gc
import importlib
import logging
import math
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_adapter import Live2DModelAdapter
from .parameter_runtime import ParameterController

_CWD_LOCK = threading.RLock()


class _CompatibleLogger:
    """Format logging arguments before calling stdlib or Loguru loggers."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._logger, name)
        if name not in {"debug", "info", "warning", "error", "critical", "exception"}:
            return method

        def emit(message: Any, *args: Any, **kwargs: Any) -> Any:
            if args:
                message = str(message) % args
            return method(message, **kwargs)

        return emit


@contextmanager
def _model_directory(path: Path) -> Iterator[None]:
    """Temporarily use the model directory for SDK-relative asset loading."""

    with _CWD_LOCK:
        original = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(original)


def _damped_step(
    position: float,
    velocity: float,
    target: float,
    delta_time: float,
    parameter_range: float,
) -> tuple[float, float]:
    """Track a physics target while limiting visible acceleration."""

    delta_time = max(1.0 / 240.0, min(1.0 / 15.0, delta_time))
    parameter_range = max(abs(parameter_range), 0.01)
    max_acceleration = parameter_range * 0.5
    max_velocity = parameter_range * 1.5
    remaining = target - position
    stopping_velocity = (2.0 * max_acceleration * abs(remaining)) ** 0.5
    desired_velocity = min(max_velocity, stopping_velocity)
    if remaining < 0.0:
        desired_velocity = -desired_velocity
    max_velocity_change = max_acceleration * delta_time
    velocity_change = max(
        -max_velocity_change,
        min(max_velocity_change, desired_velocity - velocity),
    )
    velocity += velocity_change
    position += velocity * delta_time
    return position, velocity


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Value-only model loading and viewport settings supplied by the host."""

    width: int = 800
    height: int = 600
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    adaptation_enabled: bool = True
    parameter_mappings: Mapping[str, tuple[str, ...] | str] = field(default_factory=dict)
    expression_mappings: Mapping[str, str] = field(default_factory=dict)
    action_mappings: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeFrame:
    """Copied frame inputs; now_seconds uses the process monotonic time base."""

    now_seconds: float
    delta: float
    gaze_x: float | None = None
    gaze_y: float | None = None
    interaction_recent: bool = False
    dragging: bool = False
    speaking: bool = False


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Immutable metadata returned after a model has loaded successfully."""

    model_path: str
    parameter_ids: tuple[str, ...]
    expression_ids: tuple[str, ...]
    motion_groups: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class Live2DModelRuntime:
    """Own one loaded Cubism model without owning its window or GL context."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self.logger = _CompatibleLogger(logger or logging.getLogger("nachobot.live2d.provider"))
        self._live2d: Any = None
        self._model: Any = None
        self._model_adapter: Live2DModelAdapter | None = None
        self._parameter_controller = ParameterController(logger=self.logger)
        self._options = RuntimeOptions()
        self._model_path: Path | None = None
        self._width = self._options.width
        self._height = self._options.height
        self._scale = self._options.scale
        self._offset_x = self._options.offset_x
        self._offset_y = self._options.offset_y
        self._initialize_started = False
        self._initialized = False
        self._disposed = False
        self._sdk_init_attempted = False
        self._gl_init_attempted = False
        self._parameter_indexes: dict[str, int] = {}
        self._lip_sync_param_ids: tuple[str, ...] = ()
        self._failed_parameter_writes: set[str] = set()
        self._smooth_idle_enabled = False
        self._standby_smoothing_active = False
        self._motion_in_progress = False
        self._motion_is_initial_idle = False
        self._physics_output_param_ids: tuple[str, ...] = ()
        self._physics_parameter_ranges: dict[str, float] = {}
        self._physics_filter_states: dict[str, tuple[float, float]] = {}
        self._physics_filter_updated_at: float | None = None
        self._active_tweens: list[dict[str, Any]] = []
        self._last_now_seconds: float | None = None
        self._target_x = 0.0
        self._target_y = 0.0
        self._current_gaze_x = 0.0
        self._current_gaze_y = 0.0
        self._mouth_phase = 0.0

    @property
    def model_info(self) -> ModelInfo | None:
        """Return model metadata only; native model handles stay private."""

        if self._model_adapter is None or self._model_path is None:
            return None
        metadata = self._model_adapter.metadata
        return ModelInfo(
            model_path=str(self._model_path),
            parameter_ids=tuple(self._model_adapter.available_parameter_ids),
            expression_ids=tuple(self._model_adapter.available_expression_ids),
            motion_groups=tuple(self._model_adapter.available_motion_groups),
            warnings=tuple(metadata.warnings),
        )

    def initialize(
        self,
        model_path: str | Path,
        *,
        options: RuntimeOptions | None = None,
    ) -> ModelInfo:
        """Initialize the SDK and load a model on the caller's current GL context."""

        if self._initialize_started:
            raise RuntimeError("Live2DModelRuntime can only be initialized once")
        self._initialize_started = True
        self._options = options or RuntimeOptions()
        self._width = max(1, int(self._options.width))
        self._height = max(1, int(self._options.height))
        self._scale = float(self._options.scale)
        self._offset_x = float(self._options.offset_x)
        self._offset_y = float(self._options.offset_y)
        path = Path(model_path).expanduser().resolve()
        self._model_path = path

        if not path.is_file():
            raise FileNotFoundError(f"Live2D model descriptor not found: {path}")
        adapter = Live2DModelAdapter.from_model_path(
            path,
            enabled=self._options.adaptation_enabled,
            parameter_mappings=self._options.parameter_mappings,
            expression_mappings=self._options.expression_mappings,
            action_mappings=self._options.action_mappings,
            logger=self.logger,
        )
        moc_path = adapter.metadata.moc_path
        if moc_path is None or not moc_path.is_file():
            raise FileNotFoundError(f"Live2D model .moc3 file not found: {moc_path}")
        self._model_adapter = adapter

        try:
            with _model_directory(path.parent):
                self._live2d = importlib.import_module("live2d.v3")
                self.logger.info("[Live2D] Initializing SDK on the host GL context")
                self._sdk_init_attempted = True
                self._live2d.init()
                self._gl_init_attempted = True
                self._live2d.glInit()

                self._model = self._live2d.LAppModel()
                self._model.LoadModelJson(f"./{path.name}")
                self._model.Resize(self._width, self._height)
                self._bind_runtime_metadata()
                self._disable_culling_and_depth()
                self.set_transform(self._scale, self._offset_x, self._offset_y)
                self._configure_smooth_idle()
                self._start_idle_motion()
                self._initialized = True
        except Exception:
            self._cleanup_native_resources()
            raise

        info = self.model_info
        assert info is not None
        self.logger.info("[Live2D] Model loaded: %s", path)
        return info

    def load(
        self,
        model_path: str | Path,
        *,
        options: RuntimeOptions | None = None,
    ) -> ModelInfo:
        """Alias for ``initialize`` for hosts that use load terminology."""

        return self.initialize(model_path, options=options)

    def resize(self, width: int, height: int) -> None:
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        if self._model is not None:
            self._model.Resize(self._width, self._height)

    def set_transform(self, scale: float, offset_x: float, offset_y: float) -> None:
        self._scale = float(scale)
        self._offset_x = float(offset_x)
        self._offset_y = float(offset_y)
        if self._model is not None:
            self._model.SetScale(self._scale)
            self._model.SetOffset(self._offset_x, self._offset_y)

    def command(self, kind: str, payload: Any) -> bool:
        """Apply one model-only command on the render thread.

        Supported commands are ``gaze``, ``auto_gaze``, ``tween`` /
        ``param_tween``, ``motion`` / ``body_action``, ``random_motion``,
        ``desktop_motion``, ``canonical_action``, and ``emotion``. Business
        states and audio commands intentionally remain in the host.
        """

        if self._model is None or self._model_adapter is None:
            return False
        command = str(kind or "").strip().casefold()

        if command == "gaze":
            if not isinstance(payload, (tuple, list)) or len(payload) < 2:
                return False
            coordinates = self._finite_pair(payload[0], payload[1])
            if coordinates is None:
                return False
            self._target_x, self._target_y = coordinates
            return True

        if command == "auto_gaze":
            if not isinstance(payload, Mapping):
                return False
            coordinates = self._finite_pair(payload.get("x", 0.0), payload.get("y", 0.0))
            if coordinates is None:
                return False
            self._target_x, self._target_y = coordinates
            return True

        if command in {"tween", "param_tween"}:
            if not isinstance(payload, Mapping):
                return False
            parameter_id = str(payload.get("param") or "").strip()
            value = self._finite_number(payload.get("value"))
            duration = self._finite_number(payload.get("duration", 1.0))
            if not parameter_id or value is None or duration is None:
                return False
            return self._queue_parameter_tweens({parameter_id: value}, duration=duration)

        if command in {"motion", "body_action"}:
            if isinstance(payload, Mapping):
                group = payload.get("group", "")
                priority = payload.get("priority", 3)
            else:
                group, priority = payload, 3
            priority_value = self._finite_number(priority)
            if priority_value is None:
                return False
            return self._start_motion(group, priority=int(priority_value))

        if command == "random_motion":
            if not isinstance(payload, Mapping):
                return False
            group = payload.get("group", "Idle")
            priority = self._finite_number(payload.get("priority", 3))
            if priority is None:
                return False
            return self._start_motion(group, priority=int(priority), random_motion=True)

        if command == "desktop_motion":
            if isinstance(payload, Mapping):
                requested_group = str(payload.get("group") or "Tap")
                priority = self._finite_number(payload.get("priority", 3))
                if priority is None:
                    return False
            else:
                requested_group = str(payload or "Tap")
                priority = 3.0
            return self._start_random_motion(requested_group, int(priority))

        if command == "canonical_action":
            action_id = str(payload or "").strip().upper()
            motion_group = self._model_adapter.resolve_action(action_id)
            if not motion_group:
                self.logger.warning("[Live2D] Canonical action is unavailable: %s", action_id)
                return False
            return self._start_motion(motion_group)

        if command == "emotion":
            emotion_name: str | None = None
            if isinstance(payload, Mapping):
                try:
                    strongest = max(payload, key=lambda key: float(payload[key]))
                    if float(payload[strongest]) >= 3:
                        emotion_name = str(strongest)
                except (TypeError, ValueError, OverflowError):
                    self.logger.warning("[Live2D] Ignored invalid emotion weights")
            elif isinstance(payload, str):
                emotion_name = payload
            if not emotion_name:
                return False
            expression_id = self._model_adapter.resolve_expression(emotion_name)
            if expression_id:
                self._model.SetExpression(expression_id)
                return True
            if emotion_name.casefold() in {"normal", "default", "neutral"}:
                reset = getattr(self._model, "ResetExpressions", None)
                if callable(reset):
                    reset()
                    self._apply_emotion_parameter_pose(emotion_name)
                    return True
                return self._apply_emotion_parameter_pose(emotion_name)
            return self._apply_emotion_parameter_pose(emotion_name)

        return False

    def update_draw(self, frame: RuntimeFrame) -> None:
        """Advance gaze, lipsync, tweens, motion, smoothing, and model drawing."""

        model = self._model
        if model is None or not isinstance(frame, RuntimeFrame):
            return
        now = self._finite_number(frame.now_seconds)
        if now is None:
            return
        self._last_now_seconds = now
        model.SetScale(self._scale)
        model.SetOffset(self._offset_x, self._offset_y)
        self._prepare_draw_state()
        self._update_gaze(frame, now)
        self._update_lip_sync(bool(frame.speaking))
        self._update_tweens(now, bool(frame.interaction_recent))
        self._return_to_standby_if_finished()

        # Keep the original algorithm order: Cubism Update, smoothed idle physics,
        # user-authored parameter values, then Draw.
        model.Update()
        self._smooth_physics_outputs()
        self._parameter_controller.apply(model)
        model.Draw()

    def parameter_snapshot(self) -> list[dict[str, Any]]:
        """Return fresh UI-safe dictionaries without exposing native objects."""

        return self._parameter_controller.snapshot()

    def set_parameter(self, parameter_id: str, value: Any) -> bool:
        """Stage a manual value; the next render frame applies it before Draw."""

        return self._parameter_controller.set_value(parameter_id, value)

    def reset_parameters(self, group: str | None = None) -> None:
        """Release manual overrides, optionally for one UI group."""

        self._parameter_controller.reset(group)

    def dispose(self) -> None:
        """Release Cubism objects while the host GL context is still current."""

        if self._disposed:
            return
        self._cleanup_native_resources()
        self._disposed = True
        self._initialized = False

    def _cleanup_native_resources(self) -> None:
        self._parameter_controller.unbind_model()
        model = self._model
        self._model = None
        if model is not None:
            try:
                model.DestroyRenderer()
            except Exception as exc:
                self.logger.warning("[Live2D] DestroyRenderer cleanup failed: %s", exc)
        del model
        gc.collect()

        live2d = self._live2d
        if live2d is not None and self._gl_init_attempted:
            try:
                live2d.glRelease()
            except Exception as exc:
                self.logger.warning("[Live2D] glRelease cleanup failed: %s", exc)
        if live2d is not None and self._sdk_init_attempted:
            try:
                live2d.dispose()
            except Exception as exc:
                self.logger.warning("[Live2D] SDK dispose cleanup failed: %s", exc)
        self._live2d = None
        self._sdk_init_attempted = False
        self._gl_init_attempted = False

    def _bind_runtime_metadata(self) -> None:
        assert self._model is not None
        assert self._model_adapter is not None
        try:
            parameter_ids = list(self._model.GetParamIds() or ())
        except Exception as exc:
            parameter_ids = []
            self.logger.warning("[Live2D] Failed to enumerate parameters: %s", exc)
        try:
            expression_ids = list(self._model.GetExpressionIds() or ())
        except Exception as exc:
            expression_ids = []
            self.logger.warning("[Live2D] Failed to enumerate expressions: %s", exc)
        try:
            motion_groups = self._model.GetMotionGroups() or ()
        except Exception as exc:
            motion_groups = ()
            self.logger.warning("[Live2D] Failed to enumerate motion groups: %s", exc)
        if isinstance(motion_groups, Mapping):
            motion_groups = list(motion_groups)
        self._model_adapter.bind_runtime(
            parameter_ids=parameter_ids,
            expression_ids=expression_ids,
            motion_groups=motion_groups,
        )
        self._parameter_indexes = {
            parameter_id: index for index, parameter_id in enumerate(parameter_ids)
        }
        self._lip_sync_param_ids = self._model_adapter.resolve_parameter("MOUTH_OPEN")
        self._parameter_controller.bind_model(self._model, self._model_adapter)

    def _disable_culling_and_depth(self) -> None:
        try:
            from OpenGL.GL import GL_CULL_FACE, GL_DEPTH_TEST, glDisable

            glDisable(GL_CULL_FACE)
            glDisable(GL_DEPTH_TEST)
        except ImportError:
            self.logger.warning("[Live2D] PyOpenGL unavailable for model GL setup")
        except Exception as exc:
            self.logger.warning("[Live2D] OpenGL state setup failed: %s", exc)

    def _prepare_draw_state(self) -> None:
        """Restore the blend/texture state required by the original model draw."""

        try:
            from OpenGL.GL import (
                GL_BLEND,
                GL_ONE_MINUS_SRC_ALPHA,
                GL_SRC_ALPHA,
                GL_TEXTURE_2D,
                glBlendFunc,
                glEnable,
            )

            glEnable(GL_BLEND)
            glEnable(GL_TEXTURE_2D)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        except ImportError:
            self.logger.warning("[Live2D] PyOpenGL unavailable for model draw state")
        except Exception as exc:
            self.logger.debug("[Live2D] Could not restore model draw state: %s", exc)

    def _update_gaze(self, frame: RuntimeFrame, now: float) -> None:
        if frame.gaze_x is not None and frame.gaze_y is not None:
            coordinates = self._finite_pair(frame.gaze_x, frame.gaze_y)
            if coordinates is not None and not frame.dragging:
                gaze_x, gaze_y = coordinates
                screen_x = (gaze_x + 1.0) * 0.5 * self._width
                screen_y = (1.0 - gaze_y) * 0.5 * self._height
                self._model.Drag(screen_x, screen_y)
            return
        if frame.dragging or frame.interaction_recent or self._active_tweens:
            return
        self._current_gaze_x += (self._target_x - self._current_gaze_x) * 0.01
        self._current_gaze_y += (self._target_y - self._current_gaze_y) * 0.01
        screen_x = (self._current_gaze_x + 1.0) * 0.5 * self._width
        screen_y = (1.0 - self._current_gaze_y) * 0.5 * self._height
        try:
            self._model.Drag(screen_x, screen_y)
        except Exception as exc:
            self.logger.debug("[Live2D] Gaze update failed at %.3f: %s", now, exc)

    def _update_lip_sync(self, speaking: bool) -> None:
        if speaking:
            self._mouth_phase += 0.12
            mouth_value = (
                0.4 * math.sin(self._mouth_phase * 2.5)
                + 0.3 * math.sin(self._mouth_phase * 1.8)
                + 0.3 * math.sin(self._mouth_phase * 3.3)
            )
            mouth_value = max(0.0, min(1.0, (mouth_value + 1.0) * 0.5))
            self._set_lip_sync_value(mouth_value)
        elif self._mouth_phase > 0:
            self._mouth_phase = 0.0
            self._set_lip_sync_value(0.0)

    def _update_tweens(self, now: float, interaction_recent: bool) -> None:
        if interaction_recent:
            return
        active_next: list[dict[str, Any]] = []
        for tween in self._active_tweens:
            try:
                if tween["start_time"] is None:
                    tween["start_time"] = now
                elapsed = now - float(tween["start_time"])
                t = max(0.0, min(1.0, elapsed / float(tween["duration"])))
                eased_t = 1.0 - (1.0 - t) * (1.0 - t)
                current_value = (
                    float(tween["start_val"])
                    + (float(tween["end_val"]) - float(tween["start_val"])) * eased_t
                )
                parameter_id = str(tween["param"])
                if parameter_id in self._model_adapter.available_parameter_ids:
                    try:
                        self._model.SetParameterValue(parameter_id, current_value, 1.0)
                    except Exception:
                        pass
                if t < 1.0:
                    active_next.append(tween)
            except Exception as exc:
                self.logger.error("[Live2D] Error in tween processing: %s", exc)
        self._active_tweens = active_next

    def _set_lip_sync_value(self, value: float) -> None:
        if not self._lip_sync_param_ids and self._model_adapter is not None:
            self._lip_sync_param_ids = self._model_adapter.resolve_parameter("MOUTH_OPEN")
        if not self._lip_sync_param_ids:
            if "<unresolved-lip-sync>" not in self._failed_parameter_writes:
                self._failed_parameter_writes.add("<unresolved-lip-sync>")
                self.logger.warning("[Live2D] No lip-sync parameter could be resolved")
            return
        for parameter_id in self._lip_sync_param_ids:
            try:
                self._model.SetParameterValue(parameter_id, value, 1.0)
            except Exception as exc:
                if parameter_id not in self._failed_parameter_writes:
                    self._failed_parameter_writes.add(parameter_id)
                    self.logger.warning(f"[Live2D] Lip-sync write failed for {parameter_id}: {exc}")

    def _configure_smooth_idle(self) -> bool:
        if self._model is None:
            return False
        try:
            self._model.SetAutoBreathEnable(True)
        except Exception as exc:
            self.logger.warning("[Live2D] Failed to restore SDK auto-breath: %s", exc)
        self._smooth_idle_enabled = True
        self._configure_physics_smoothing()
        return True

    def _configure_physics_smoothing(self) -> None:
        self._physics_output_param_ids = ()
        self._physics_parameter_ranges.clear()
        self._physics_filter_states.clear()
        self._physics_filter_updated_at = time.perf_counter()
        native_model = getattr(self._model, "_model", None)
        if not callable(getattr(native_model, "SetParameterValueById", None)):
            self.logger.warning(
                "[Live2D] Physics smoothing unavailable; transient writes are unsupported"
            )
            return
        resolved_ids = tuple(
            dict.fromkeys(
                parameter_id
                for canonical in ("ANGLE_X", "ANGLE_Z", "BODY_ANGLE_X", "BODY_ANGLE_Z")
                for parameter_id in self._model_adapter.resolve_parameter(canonical)
                if parameter_id in self._parameter_indexes
            )
        )
        for parameter_id in resolved_ids:
            index = self._parameter_indexes[parameter_id]
            try:
                parameter = self._model.GetParameter(index)
                parameter_range = float(parameter.max) - float(parameter.min)
            except Exception:
                parameter_range = 1.0
            self._physics_parameter_ranges[parameter_id] = max(abs(parameter_range), 0.01)
        self._physics_output_param_ids = resolved_ids

    def _smooth_physics_outputs(self) -> None:
        if (
            not self._smooth_idle_enabled
            or self._model is None
            or not self._physics_output_param_ids
        ):
            return
        if not self._standby_smoothing_active:
            self._physics_filter_states.clear()
            self._physics_filter_updated_at = time.perf_counter()
            return
        now = time.perf_counter()
        previous = self._physics_filter_updated_at
        self._physics_filter_updated_at = now
        delta_time = 1.0 / 60.0 if previous is None else now - previous
        tweened = {
            str(tween.get("param")) for tween in self._active_tweens if isinstance(tween, dict)
        }
        for parameter_id in self._physics_output_param_ids:
            raw_value = self._get_parameter_value(parameter_id)
            if parameter_id in tweened:
                self._physics_filter_states.pop(parameter_id, None)
                continue
            state = self._physics_filter_states.get(parameter_id)
            if state is None:
                self._physics_filter_states[parameter_id] = (raw_value, 0.0)
                continue
            value, velocity = _damped_step(
                state[0],
                state[1],
                raw_value,
                delta_time,
                self._physics_parameter_ranges.get(parameter_id, 1.0),
            )
            self._physics_filter_states[parameter_id] = (value, velocity)
            try:
                self._model._model.SetParameterValueById(parameter_id, value, 1.0)
            except Exception as exc:
                if parameter_id not in self._failed_parameter_writes:
                    self._failed_parameter_writes.add(parameter_id)
                    self.logger.warning(
                        f"[Live2D] Physics smoothing failed for {parameter_id}: {exc}"
                    )

    def _get_parameter_value(self, parameter_id: str) -> float:
        if self._model is None:
            return 0.0
        index = self._parameter_indexes.get(parameter_id)
        if index is None:
            return 0.0
        try:
            return float(self._model.GetParameterValue(index))
        except Exception:
            return 0.0

    def _resolve_motion_request(self, requested: Any) -> tuple[str | None, int]:
        requested_text = str(requested or "").strip()
        group = self._model_adapter.resolve_motion_group(requested_text)
        if group is not None:
            return group, 0
        base, separator, suffix = requested_text.rpartition("_")
        if separator and suffix.isdigit():
            group = self._model_adapter.resolve_motion_group(base)
            if group is not None:
                return group, int(suffix)
        return None, 0

    def _start_idle_motion(self) -> bool:
        if self._model is None:
            return False
        idle_group = self._model_adapter.resolve_motion_group("Idle")
        if not idle_group:
            self.logger.info("[Live2D] No Idle motion group found; skipping")
            return False
        try:
            self._model.StartMotion(idle_group, 0, 3)
        except Exception as exc:
            self._enter_standby()
            self.logger.error("[Live2D] Failed to start initial Idle[0]: %s", exc)
            return False
        self._motion_in_progress = True
        self._motion_is_initial_idle = True
        self._standby_smoothing_active = True
        self._physics_filter_states.clear()
        self._physics_filter_updated_at = time.perf_counter()
        return True

    def _enter_standby(self, *, preserve_filter: bool = False) -> None:
        self._motion_in_progress = False
        self._motion_is_initial_idle = False
        self._standby_smoothing_active = True
        if not preserve_filter:
            self._physics_filter_states.clear()
            self._physics_filter_updated_at = time.perf_counter()

    def _return_to_standby_if_finished(self) -> None:
        if self._model is None or not self._motion_in_progress:
            return
        try:
            finished = self._model.IsMotionFinished()
        except Exception as exc:
            self.logger.warning("[Live2D] Failed to inspect motion state: %s", exc)
            return
        if finished:
            self._enter_standby(preserve_filter=self._motion_is_initial_idle)

    def _start_motion(
        self,
        requested: Any,
        *,
        priority: int = 3,
        random_motion: bool = False,
    ) -> bool:
        if self._model is None:
            return False
        group, index = self._resolve_motion_request(requested)
        if group is None:
            self.logger.warning("[Live2D] Motion group does not exist: %s", requested)
            return False
        idle_group = self._model_adapter.resolve_motion_group("Idle")
        if group == idle_group:
            try:
                self._model.StopAllMotions()
            except Exception as exc:
                self.logger.error("[Live2D] Failed to stop motion for standby: %s", exc)
            self._enter_standby()
            return True
        try:
            self._motion_in_progress = True
            self._motion_is_initial_idle = False
            self._standby_smoothing_active = False
            if random_motion:
                self._model.StartRandomMotion(group, priority)
            else:
                self._model.StartMotion(group, index, priority)
            return True
        except Exception as exc:
            self._enter_standby()
            self.logger.error("[Live2D] Failed to start motion %s: %s", group, exc)
            return False

    def _start_random_motion(self, requested_group: str, priority: int = 3) -> bool:
        if self._model is None:
            return False
        group = self._model_adapter.resolve_motion_group(requested_group)
        if group is None:
            self.logger.warning("[Live2D] Motion group does not exist: %s", requested_group)
            return False
        try:
            self._motion_in_progress = True
            self._motion_is_initial_idle = False
            self._standby_smoothing_active = False
            self._model.StartRandomMotion(group, priority)
            return True
        except Exception as exc:
            self._enter_standby()
            self.logger.error("[Live2D] Failed to start random motion %s: %s", group, exc)
            return False

    def _queue_parameter_tweens(self, values: Mapping[str, Any], *, duration: float = 0.35) -> bool:
        queued = False
        # Match the original renderer: tweens start when the command is received,
        # even when a recent-interaction cooldown delays writes for a later frame.
        start_time = time.monotonic()
        for requested_parameter, target in values.items():
            parameter_ids = self._model_adapter.resolve_parameter(requested_parameter)
            if not parameter_ids:
                self.logger.debug("[Live2D] Emotion parameter unavailable: %s", requested_parameter)
                continue
            target_value = self._finite_number(target)
            if target_value is None:
                continue
            for parameter_id in parameter_ids:
                self._active_tweens = [
                    tween for tween in self._active_tweens if tween.get("param") != parameter_id
                ]
                self._active_tweens.append(
                    {
                        "param": parameter_id,
                        "start_val": self._get_parameter_value(parameter_id),
                        "end_val": target_value,
                        "start_time": start_time,
                        "duration": max(0.05, float(duration)),
                    }
                )
                queued = True
        return queued

    def _apply_emotion_parameter_pose(self, emotion_name: str) -> bool:
        """Provide model feedback when no matching expression file exists."""

        normalized = emotion_name.strip().casefold()
        aliases = {
            "default": "normal",
            "neutral": "normal",
            "普通": "normal",
            "默认": "normal",
            "happy": "joy",
            "smile": "joy",
            "开心": "joy",
            "高兴": "joy",
            "sad": "sorrow",
            "悲伤": "sorrow",
            "难过": "sorrow",
            "anger": "angry",
            "mad": "angry",
            "生气": "angry",
            "愤怒": "angry",
            "surprise": "fear",
            "surprised": "fear",
            "惊讶": "fear",
            "害羞": "shy",
            "脸红": "shy",
        }
        normalized = aliases.get(normalized, normalized)
        neutral = {
            "ParamCheek": 0.0,
            "MOUTH_OPEN": 0.0,
            "MOUTH_FORM": 0.0,
            "ParamEyeLSmile": 0.0,
            "ParamEyeRSmile": 0.0,
            "BROW_L_Y": 0.0,
            "BROW_R_Y": 0.0,
            "ANGLE_Y": 0.0,
            "ANGLE_Z": 0.0,
        }
        poses: dict[str, dict[str, float]] = {
            "normal": neutral,
            "joy": {
                **neutral,
                "ParamCheek": 0.75,
                "MOUTH_FORM": 0.85,
                "ParamEyeLSmile": 0.8,
                "ParamEyeRSmile": 0.8,
            },
            "shy": {**neutral, "ParamCheek": 1.0, "MOUTH_FORM": 0.25, "ANGLE_Z": 6.0},
            "angry": {**neutral, "MOUTH_FORM": -0.75, "BROW_L_Y": -0.65, "BROW_R_Y": -0.65},
            "disgust": {**neutral, "MOUTH_FORM": -0.9, "BROW_L_Y": -0.25, "BROW_R_Y": 0.2},
            "fear": {**neutral, "MOUTH_OPEN": 0.55, "BROW_L_Y": 0.65, "BROW_R_Y": 0.65},
            "sorrow": {
                **neutral,
                "MOUTH_FORM": -0.55,
                "BROW_L_Y": 0.35,
                "BROW_R_Y": 0.35,
                "ANGLE_Y": -5.0,
            },
        }
        pose = poses.get(normalized)
        return self._queue_parameter_tweens(pose) if pose is not None else False

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _finite_pair(cls, x: Any, y: Any) -> tuple[float, float] | None:
        x_number = cls._finite_number(x)
        y_number = cls._finite_number(y)
        if x_number is None or y_number is None:
            return None
        return x_number, y_number
