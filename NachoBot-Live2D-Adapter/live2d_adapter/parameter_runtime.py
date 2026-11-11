"""Pure runtime controls for Live2D model parameters."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from typing import Any

_STANDARD_PARAMETERS: dict[str, tuple[str, str]] = {
    "ParamEyeLOpen": ("左眼睁开", "眼睛"),
    "ParamEyeROpen": ("右眼睁开", "眼睛"),
    "ParamEyeLSmile": ("左眼微笑", "眼睛"),
    "ParamEyeRSmile": ("右眼微笑", "眼睛"),
    "ParamEyeBallX": ("视线左右", "眼睛"),
    "ParamEyeBallY": ("视线上下", "眼睛"),
    "ParamEyeBallForm": ("眼球形状", "眼睛"),
    "ParamBrowLY": ("左眉上下", "眉毛"),
    "ParamBrowRY": ("右眉上下", "眉毛"),
    "ParamBrowLX": ("左眉左右", "眉毛"),
    "ParamBrowRX": ("右眉左右", "眉毛"),
    "ParamBrowLAngle": ("左眉角度", "眉毛"),
    "ParamBrowRAngle": ("右眉角度", "眉毛"),
    "ParamBrowLForm": ("左眉形状", "眉毛"),
    "ParamBrowRForm": ("右眉形状", "眉毛"),
    "ParamMouthOpenY": ("嘴巴开合", "嘴部"),
    "ParamMouthForm": ("嘴型", "嘴部"),
    "ParamAngleX": ("脸部左右", "脸部"),
    "ParamAngleY": ("脸部上下", "脸部"),
    "ParamAngleZ": ("脸部倾斜", "脸部"),
    "ParamBodyAngleX": ("身体左右", "身体"),
    "ParamBodyAngleY": ("身体上下", "身体"),
    "ParamBodyAngleZ": ("身体倾斜", "身体"),
    "ParamBreath": ("呼吸", "身体"),
    "ParamCheek": ("脸颊", "脸部"),
    "ParamArmLA": ("左臂 A", "身体"),
    "ParamArmRA": ("右臂 A", "身体"),
    "ParamArmLB": ("左臂 B", "身体"),
    "ParamArmRB": ("右臂 B", "身体"),
    "ParamHandL": ("左手", "身体"),
    "ParamHandR": ("右手", "身体"),
    "ParamHairFront": ("前发", "身体"),
    "ParamHairSide": ("侧发", "身体"),
    "ParamHairBack": ("后发", "身体"),
    "ParamHairFluffy": ("蓬松发型", "身体"),
    "ParamShoulderY": ("肩部上下", "身体"),
    "ParamBustX": ("胸部左右", "身体"),
    "ParamBustY": ("胸部上下", "身体"),
    "ParamBaseX": ("底座左右", "身体"),
    "ParamBaseY": ("底座上下", "身体"),
}

_GROUP_ORDER = ("眼睛", "眉毛", "嘴部", "脸部", "身体", "其他")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _parameter_presentation(parameter_id: str) -> tuple[str, str]:
    return _STANDARD_PARAMETERS.get(parameter_id, (parameter_id, "其他"))


class ParameterController:
    """Bind UI edits to one model and reapply manual values after each model update.

    Model reads happen only in bind_model(); all model writes happen in apply(),
    which the renderer must call on its own thread after Update() and before Draw().
    The other methods only copy or mutate ordinary Python data and are safe to call
    from the chat/Tk thread.
    """

    def __init__(self, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.RLock()
        self._model: Any = None
        self._parameters: dict[str, dict[str, Any]] = {}
        self._indexes: dict[str, int] = {}
        self._pending_resets: dict[str, float] = {}
        self._warned_messages: set[str] = set()
        self._last_value_refresh_at = 0.0

    def bind_model(
        self,
        model: Any,
        model_adapter: Any = None,
    ) -> list[dict[str, Any]]:
        """Read real runtime ranges/defaults and discard overrides from the old model."""

        parameters: dict[str, dict[str, Any]] = {}
        indexes: dict[str, int] = {}
        try:
            count = max(0, int(model.GetParameterCount()))
        except Exception as exc:
            self._log_once("warning", f"[Live2D] Cannot enumerate parameters: {exc}")
            count = 0

        display_names = _display_names(model_adapter)
        for index in range(count):
            try:
                parameter = model.GetParameter(index)
                parameter_id = str(getattr(parameter, "id", "") or "").strip()
                minimum = _finite_number(getattr(parameter, "min", None))
                maximum = _finite_number(getattr(parameter, "max", None))
                default = _finite_number(getattr(parameter, "default", None))
                value = _finite_number(getattr(parameter, "value", None))
            except Exception as exc:
                self._log_once(
                    "warning",
                    f"[Live2D] Cannot read parameter at index {index}: {exc}",
                )
                continue

            if (
                not parameter_id
                or minimum is None
                or maximum is None
                or default is None
                or minimum > maximum
            ):
                continue

            label, group = _parameter_presentation(parameter_id)
            display_name = display_names.get(parameter_id)
            if display_name:
                label = display_name
            default = _clamp(default, minimum, maximum)
            if value is None:
                value = default
            else:
                value = _clamp(value, minimum, maximum)

            parameters.setdefault(
                parameter_id,
                {
                    "id": parameter_id,
                    "label": label,
                    "group": group,
                    "minimum": minimum,
                    "maximum": maximum,
                    "default": default,
                    "value": value,
                    "manual": False,
                },
            )
            indexes.setdefault(parameter_id, index)

        with self._lock:
            self._model = model
            self._parameters = parameters
            self._indexes = indexes
            self._pending_resets.clear()
            self._last_value_refresh_at = 0.0
            return self._snapshot_locked()

    def unbind_model(self) -> None:
        """Drop all runtime model references and invalidate queued UI edits.

        This is intentionally model-free so shutdown can call it before native
        model destruction and framework disposal.
        """

        with self._lock:
            self._model = None
            self._parameters.clear()
            self._indexes.clear()
            self._pending_resets.clear()
            self._last_value_refresh_at = 0.0

    def set_value(self, parameter_id: str, value: Any) -> bool:
        """Stage one finite slider value, clamped to the actual model range."""

        number = _finite_number(value)
        if number is None:
            return False
        with self._lock:
            parameter = self._parameters.get(str(parameter_id))
            if parameter is None:
                return False
            parameter["value"] = _clamp(
                number,
                parameter["minimum"],
                parameter["maximum"],
            )
            parameter["manual"] = True
            self._pending_resets.pop(parameter["id"], None)
            return True

    def reset(self, group: str | None = None) -> None:
        """Release manual locks and queue one default write for touched parameters."""

        with self._lock:
            for parameter in self._parameters.values():
                if not parameter["manual"]:
                    continue
                if group is not None and parameter["group"] != group:
                    continue
                parameter_id = parameter["id"]
                self._pending_resets[parameter_id] = parameter["default"]
                parameter["value"] = parameter["default"]
                parameter["manual"] = False

    def apply(self, model: Any) -> None:
        """Apply staged resets and manual values after model Update, before Draw."""

        with self._lock:
            if model is not self._model:
                return
            resets = tuple(self._pending_resets.items())
            self._pending_resets.clear()
            manual_values = tuple(
                (parameter_id, parameter["value"])
                for parameter_id, parameter in self._parameters.items()
                if parameter["manual"]
            )

        for parameter_id, default in resets:
            self._write_value(model, parameter_id, default)
        for parameter_id, value in manual_values:
            self._write_value(model, parameter_id, value)

        now = time.monotonic()
        with self._lock:
            if model is not self._model or now - self._last_value_refresh_at < 0.2:
                return
            self._last_value_refresh_at = now
            values_to_read = tuple(
                (parameter_id, index)
                for parameter_id, index in self._indexes.items()
                if not self._parameters[parameter_id]["manual"]
            )
        for parameter_id, index in values_to_read:
            try:
                getter = getattr(model, "GetParameterValue", None)
                if callable(getter):
                    value = _finite_number(getter(index))
                else:
                    parameter = model.GetParameter(index)
                    value = _finite_number(getattr(parameter, "value", None))
            except Exception as exc:
                self._log_once(
                    "warning",
                    f"[Live2D] Cannot refresh parameter {parameter_id}: {exc}",
                )
                continue
            if value is None:
                continue
            with self._lock:
                parameter = self._parameters.get(parameter_id)
                if parameter is None or parameter["manual"]:
                    continue
                parameter["value"] = _clamp(
                    value,
                    parameter["minimum"],
                    parameter["maximum"],
                )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return UI-safe copies; no model calls are made from this method."""

        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> list[dict[str, Any]]:
        return [dict(parameter) for parameter in self._parameters.values()]

    def _write_value(self, model: Any, parameter_id: str, value: float) -> None:
        native_model = getattr(model, "_model", None)
        setter = getattr(native_model, "SetParameterValueById", None)
        if callable(setter):
            try:
                setter(parameter_id, value)
                return
            except Exception as exc:
                self._log_once(
                    "warning",
                    f"[Live2D] Transient parameter write failed; using wrapper fallback: {exc}",
                )
        else:
            self._log_once(
                "warning",
                "[Live2D] Native transient parameter setter is unavailable; "
                "using the model wrapper setter",
            )

        # LAppModel.SetParameterValue is the public live2d-py fallback. The native
        # SetParameterValueById path above is preferred because it does not save.
        fallback = getattr(model, "SetParameterValue", None)
        if callable(fallback):
            try:
                fallback(parameter_id, value)
            except Exception as exc:
                self._log_once(
                    "warning",
                    f"[Live2D] Could not write parameter {parameter_id}: {exc}",
                )

    def _log_once(self, level: str, message: str) -> None:
        if message in self._warned_messages:
            return
        self._warned_messages.add(message)
        method = getattr(self.logger, level, None)
        if callable(method):
            method(message)


def _display_names(model_adapter: Any) -> Mapping[str, str]:
    metadata = getattr(model_adapter, "metadata", None)
    names = getattr(metadata, "parameter_display_names", None)
    return names if isinstance(names, Mapping) else {}
