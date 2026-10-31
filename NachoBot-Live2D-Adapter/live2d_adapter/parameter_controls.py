"""Runtime-backed manual Live2D parameter controls for the desktop pet."""

from __future__ import annotations

from .ui_theme import PALETTE, ScrollThumb, action_button, popup_shell, rounded_surface, px, scale_widgets, ui_scale

import math
import threading
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import tkinter as tk


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


class _ParameterSlider:
    """Canvas slider with a visible teal track and draggable circular thumb."""

    def __init__(
        self,
        tk_module: Any,
        parent: tk.Misc,
        *,
        minimum: float,
        maximum: float,
        value: float,
        resolution: float,
        on_change: Callable[[float], None],
        colors: Mapping[str, str],
    ) -> None:
        self._tk = tk_module
        self._minimum = minimum
        self._maximum = maximum
        self._resolution = resolution
        self._value = _clamp(value, minimum, maximum)
        self._on_change = on_change
        self._colors = colors
        self._ui_scale = 1.0
        self._hover = False
        self._dragging = False
        self.canvas = tk_module.Canvas(
            parent,
            height=34,
            bg=colors["surface"],
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
        )
        self.canvas._pet_dynamic_canvas = True
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self._draw()

    def pack(self, **options: Any) -> Any:
        return self.canvas.pack(**options)

    def bind(self, sequence: str, callback: Callable[..., Any], add: str | None = None) -> Any:
        return self.canvas.bind(sequence, callback, add=add)

    def get(self) -> float:
        return self._value

    def set(self, value: float) -> None:
        number = _finite_number(value)
        if number is None:
            return
        self._value = _clamp(number, self._minimum, self._maximum)
        self._draw()

    def _on_resize(self, _event: tk.Event) -> None:
        self._draw()

    def _on_enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()

    def _on_press(self, event: tk.Event) -> None:
        self._dragging = True
        self.canvas.focus_set()
        self._set_from_x(float(event.x))

    def _on_drag(self, event: tk.Event) -> None:
        if self._dragging:
            self._set_from_x(float(event.x))

    def _on_release(self, event: tk.Event) -> None:
        if self._dragging:
            self._set_from_x(float(event.x))
        self._dragging = False

    def _set_from_x(self, x: float) -> None:
        width = max(1, self.canvas.winfo_width())
        start = float(px(12, self._ui_scale))
        end = max(start + 1.0, float(width) - start)
        ratio = min(1.0, max(0.0, (x - start) / (end - start)))
        span = self._maximum - self._minimum
        value = self._minimum + ratio * span
        if self._resolution > 0.0:
            steps = round((value - self._minimum) / self._resolution)
            value = self._minimum + steps * self._resolution
        value = _clamp(value, self._minimum, self._maximum)
        if math.isclose(value, self._value, abs_tol=1e-12):
            return
        self._value = value
        self._draw()
        self._on_change(value)

    def _draw(self) -> None:
        try:
            width = max(1, self.canvas.winfo_width())
            height = max(1, self.canvas.winfo_height())
            start = float(px(12, self._ui_scale))
            end = max(start + 1.0, float(width) - start)
            center_y = float(height) / 2.0
            span = self._maximum - self._minimum
            ratio = 0.5 if span <= 0.0 else (self._value - self._minimum) / span
            thumb_x = start + ratio * (end - start)
            accent = self._colors["accent_hover"] if self._hover else self._colors["accent"]
            radius = float(px(7 if self._hover else 6, self._ui_scale))
            self.canvas.delete("all")
            self.canvas.create_line(
                start,
                center_y,
                end,
                center_y,
                fill=PALETTE["border"],
                width=px(3, self._ui_scale),
                capstyle=self._tk.ROUND,
            )
            if thumb_x > start + 0.5:
                self.canvas.create_line(
                    start,
                    center_y,
                    thumb_x,
                    center_y,
                    fill=accent,
                    width=px(3, self._ui_scale),
                    capstyle=self._tk.ROUND,
                )
            self.canvas.create_oval(
                thumb_x - radius,
                center_y - radius,
                thumb_x + radius,
                center_y + radius,
                fill=accent if self._hover or self._dragging else "#ffffff",
                outline=self._colors["accent_hover"] if self._hover else accent,
                width=px(2, self._ui_scale),
            )
            self.canvas.create_oval(
                thumb_x - 2.5,
                center_y - 2.5,
                thumb_x + 2.5,
                center_y + 2.5,
                fill=accent,
                outline=accent,
                width=1,
            )
        except self._tk.TclError:
            return


class ParameterPanel:
    """Modern floating controls for a copied model parameter snapshot."""

    _COLORS = {
        "background": PALETTE["card"],
        "surface": PALETTE["history"],
        "text": PALETTE["text"],
        "secondary": PALETTE["secondary"],
        "accent": PALETTE["accent"],
        "accent_hover": PALETTE["accent_dark"],
        "border": PALETTE["border"],
        "manual": PALETTE["accent_dark"],
    }

    def __init__(
        self,
        root: tk.Misc,
        on_change: Callable[[str, float], None],
        on_reset: Callable[[str | None], None],
        logger: Any = None,
    ) -> None:
        import tkinter as tk_module

        self._tk = tk_module
        self._ui_scale = 1.0
        self._work_area_override = None
        self.root = root
        self.on_change = on_change
        self.on_reset = on_reset
        self.logger = logger
        self.window = self._tk.Toplevel(root)
        self.window.withdraw()
        self.window.title("Live2D 参数微调")
        self.window.overrideredirect(True)
        self.window.resizable(False, False)
        try:
            self.window.attributes("-toolwindow", True)
        except self._tk.TclError:
            pass
        try:
            self.window.attributes("-topmost", True)
        except self._tk.TclError:
            pass
        self.window.geometry(self._initial_geometry())
        self.window.configure(bg=self._COLORS["background"])
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self._items: dict[str, dict[str, Any]] = {}
        self._specs: list[dict[str, Any]] = []
        self._signature: tuple[tuple[str, str], ...] = ()
        self._dragging: set[str] = set()
        self._window_drag_origin: tuple[int, int, int, int] | None = None
        self._updating = False
        self._destroyed = False
        self._build()

    def show(self, specs: Sequence[Mapping[str, Any]]) -> None:
        """Display the panel, creating controls from real model parameter specs."""

        if self._destroyed:
            return
        self.update_specs(specs)
        try:
            self.window.deiconify()
            self.window.lift()
            self._keep_inside_work_area()
        except self._tk.TclError:
            return

    def update_specs(self, specs: Sequence[Mapping[str, Any]]) -> None:
        """Refresh values without replacing the control currently being dragged."""

        if self._destroyed:
            return
        self._specs = [dict(spec) for spec in specs]
        signature = tuple((str(spec.get("id", "")), str(spec.get("group", "其他"))) for spec in self._specs)
        if signature != self._signature:
            self._rebuild_controls()
        else:
            self._refresh_values()

    def hide(self) -> None:
        """Hide the floating panel without releasing any manual parameter locks."""

        if self._destroyed:
            return
        self._dragging.clear()
        self._window_drag_origin = None
        try:
            self.window.withdraw()
        except self._tk.TclError:
            pass

    def is_interacting(self) -> bool:
        """Return whether a slider or the floating-panel header is being dragged.

        Call this on the Tk owner thread when deciding whether to auto-hide a
        hover group containing the parameter panel.
        """

        return not self._destroyed and bool(
            self._dragging or self._window_drag_origin is not None
        )

    def destroy(self) -> None:
        """Destroy the panel and its Tk widgets."""

        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.window.destroy()
        except self._tk.TclError:
            pass

    close = destroy

    def winfo_id(self) -> int:
        """Return the native window id for desktop hover-surface tracking."""

        return int(self.window.winfo_id())

    def _build(self) -> None:
        colors = self._COLORS
        shell = popup_shell(self._tk, self.window, colors["background"], 400, 620)

        header = self._tk.Frame(shell, bg=colors["background"])
        header.pack(fill="x", padx=6, pady=(9, 18))
        self._header = header
        title_stack = self._tk.Frame(header, bg=colors["background"])
        title_stack.pack(side="left", fill="x", expand=True)
        title = self._tk.Label(
            title_stack,
            text="模型参数",
            bg=colors["background"],
            fg=colors["text"],
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
        )
        title.pack(fill="x")
        subtitle = self._tk.Label(
            title_stack,
            text="调整表情与姿态",
            bg=colors["background"],
            fg=colors["secondary"],
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        subtitle.pack(fill="x", pady=(2, 0))
        close_button = self._tk.Button(
            header,
            text="×",
            command=self.hide,
            bg=colors["background"],
            fg=colors["secondary"],
            activebackground=colors["border"],
            activeforeground=colors["text"],
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=8,
            pady=1,
            font=("Microsoft YaHei UI", 14, "bold"),
            takefocus=True,
        )
        close_button.pack(side="right", padx=(5, 0))
        self._close_button = close_button

        for widget in (header, title_stack, title, subtitle):
            self._bind_header_drag(widget)

        divider = self._tk.Frame(shell, bg=colors["border"], height=1)
        divider.pack(fill="x", padx=6, pady=(0, 13))

        footer = self._tk.Frame(shell, bg=colors["background"])
        footer.pack(side="bottom", fill="x", padx=6, pady=(14, 4))
        reset = action_button(self._tk, footer, "重置全部参数", lambda: self.on_reset(None), colors["background"])
        reset.pack(fill="x")
        content = self._tk.Frame(shell, bg=colors["background"])
        content.pack(fill="both", expand=True)
        self._canvas = self._tk.Canvas(
            content,
            bg=colors["background"],
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ScrollThumb(self._tk, content, self._canvas.yview, colors["background"])
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._body = self._tk.Frame(self._canvas, bg=colors["background"])
        self._body_window = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw"
        )
        self._body.bind("<Configure>", self._update_scroll_region)
        self._canvas.bind("<Configure>", self._resize_body)
        self._bind_wheel(self._canvas)
        self._bind_wheel(self._body)

    def _rebuild_controls(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self._items.clear()
        self._signature = tuple(
            (str(spec.get("id", "")), str(spec.get("group", "其他")))
            for spec in self._specs
        )

        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in self._specs:
            group = str(spec.get("group", "其他")) or "其他"
            groups.setdefault(group, []).append(spec)
        group_order = sorted(
            groups,
            key=lambda group: (
                _GROUP_ORDER.index(group) if group in _GROUP_ORDER else len(_GROUP_ORDER),
                group,
            ),
        )

        for group in group_order:
            group_header = self._tk.Frame(self._body, bg=self._COLORS["background"])
            group_header.pack(fill="x", padx=7, pady=(14, 9))
            self._tk.Label(
                group_header,
                text=group,
                bg=self._COLORS["background"],
                fg=self._COLORS["text"],
                font=("Microsoft YaHei UI", 10, "bold"),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            self._button(
                group_header,
                "重置",
                lambda value=group: self.on_reset(value),
                compact=True,
            ).pack(side="right")

            for spec in groups[group]:
                self._add_parameter_card(spec)

        self._refresh_values()
        self._scale_controls()
        self._update_scroll_region()

    def _add_parameter_card(self, spec: dict[str, Any]) -> None:
        colors = self._COLORS
        surface, card = rounded_surface(self._tk, self._body, colors["surface"], colors["background"])
        surface.pack(fill="x", padx=4, pady=(0, 7))
        self._bind_wheel(surface)
        heading = self._tk.Frame(card, bg=colors["surface"])
        heading.pack(fill="x")
        left = self._tk.Frame(heading, bg=colors["surface"])
        left.pack(side="left", fill="x", expand=True)
        label = self._tk.Label(
            left,
            text=str(spec.get("label", spec.get("id", ""))),
            bg=colors["surface"],
            fg=colors["text"],
            font=("Microsoft YaHei UI", 10),
            anchor="w",
        )
        label.pack(side="left")
        manual_label = self._tk.Label(
            left,
            text="",
            bg=colors["surface"],
            fg=colors["manual"],
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        manual_label.pack(side="left", padx=(6, 0))
        value_label = self._tk.Label(
            heading,
            text="0.000",
            bg=colors["surface"],
            fg=colors["accent_hover"],
            font=("Consolas", 9, "bold"),
            padx=6, pady=2,
            anchor="e",
        )
        value_label.pack(side="right")

        minimum = _finite_number(spec.get("minimum"))
        maximum = _finite_number(spec.get("maximum"))
        if minimum is None or maximum is None or minimum > maximum:
            minimum, maximum = 0.0, 1.0
        resolution = max((maximum - minimum) / 1000.0, 0.001)
        parameter_id = str(spec.get("id", ""))
        value = _finite_number(spec.get("value"))
        if value is None:
            value = _finite_number(spec.get("default")) or minimum
        scale = _ParameterSlider(
            self._tk,
            card,
            minimum=minimum,
            maximum=maximum,
            value=value,
            resolution=resolution,
            on_change=lambda number, current_id=parameter_id: self._slider_changed(
                current_id, number
            ),
            colors=colors,
        )
        scale.pack(fill="x", pady=(0, 0))
        scale.bind(
            "<ButtonPress-1>",
            lambda _event, current_id=parameter_id: self._begin_drag(
                current_id
            ),
            add="+",
        )
        scale.bind(
            "<ButtonRelease-1>",
            lambda _event, current_id=parameter_id: self._dragging.discard(
                current_id
            ),
            add="+",
        )
        self._items[parameter_id] = {
            "scale": scale,
            "value_label": value_label,
            "manual_label": manual_label,
            "minimum": minimum,
            "maximum": maximum,
        }
        self._bind_wheel(card)
        self._bind_wheel(heading)
        self._bind_wheel(left)
        self._bind_wheel(label)
        self._bind_wheel(manual_label)
        self._bind_wheel(value_label)

    def _refresh_values(self) -> None:
        if not self._items:
            return
        self._updating = True
        try:
            for spec in self._specs:
                parameter_id = str(spec.get("id", ""))
                widgets = self._items.get(parameter_id)
                if widgets is None or parameter_id in self._dragging:
                    continue
                value = _finite_number(spec.get("value"))
                if value is None:
                    continue
                value = _clamp(value, widgets["minimum"], widgets["maximum"])
                widgets["scale"].set(value)
                widgets["value_label"].configure(text=f"{value:.3f}")
                widgets["manual_label"].configure(
                    text="手动" if bool(spec.get("manual")) else "",
                )
        except self._tk.TclError as exc:
            self._log_once(f"[Live2D] Parameter panel refresh failed: {exc}")
        finally:
            self._updating = False

    def _slider_changed(self, parameter_id: str, raw_value: str | float) -> None:
        if self._updating:
            return
        value = _finite_number(raw_value)
        if value is None:
            return
        widgets = self._items.get(parameter_id)
        if widgets is not None:
            widgets["value_label"].configure(text=f"{value:.3f}")
            widgets["manual_label"].configure(text="手动")
        self.on_change(parameter_id, value)

    def _begin_drag(self, parameter_id: str) -> None:
        self._dragging.add(parameter_id)

    def _resize_body(self, event: tk.Event) -> None:
        try:
            self._canvas.itemconfigure(self._body_window, width=max(1, event.width))
        except self._tk.TclError:
            pass

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except self._tk.TclError:
            pass

    def _bind_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", lambda _event: self._canvas.yview_scroll(-1, "units"), add="+")
        widget.bind("<Button-5>", lambda _event: self._canvas.yview_scroll(1, "units"), add="+")

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self._canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        compact: bool = False,
    ) -> tk.Button:
        colors = self._COLORS
        return self._tk.Button(
            parent,
            text=text,
            command=command,
            bg=colors["background"],
            fg=colors["accent_hover"],
            activebackground=colors["accent"],
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=8 if compact else 10,
            pady=3 if compact else 5,
            font=("Microsoft YaHei UI", 8 if compact else 9),
        )

    def _scale_controls(self) -> None:
        self.window._pet_scale = self._ui_scale
        scale_widgets(self.window, self._ui_scale)
        for widgets in self._items.values():
            slider = widgets["scale"]
            slider._ui_scale = self._ui_scale
            slider._draw()

    def set_scale(self, value: float, work_area: Any = None) -> None:
        factor = ui_scale(value)
        if work_area is not None:
            self._work_area_override = tuple(work_area)
        if abs(factor - self._ui_scale) < 0.001:
            return
        self._ui_scale = factor
        self._scale_controls()
        left, top, right, bottom = self._work_area()
        width, height = min(px(400, factor), right - left), min(px(620, factor), bottom - top)
        x, y = int(self.window.winfo_x()), int(self.window.winfo_y())
        x, y = max(left, min(right - width, x)), max(top, min(bottom - height, y))
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _initial_geometry(self) -> str:
        left, top, right, bottom = self._work_area()
        work_width = max(1, right - left)
        work_height = max(1, bottom - top)
        width = min(px(400, self._ui_scale), work_width)
        height = min(px(620, self._ui_scale), work_height)
        x = left + max(0, (work_width - width) // 2)
        y = top + max(0, (work_height - height) // 2)
        return f"{width}x{height}+{x}+{y}"

    def _work_area(self) -> tuple[int, int, int, int]:
        """Return the primary screen work area, excluding the taskbar on Windows."""

        if self._work_area_override is not None:
            return self._work_area_override
        try:
            import ctypes
            from ctypes import wintypes

            class Rect(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            rect = Rect()
            spi_get_work_area = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                spi_get_work_area,
                0,
                ctypes.byref(rect),
                0,
            ):
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except (AttributeError, OSError, TypeError):
            pass

        width = int(self.window.winfo_screenwidth())
        height = int(self.window.winfo_screenheight())
        return 0, 0, width, height

    def _keep_inside_work_area(self) -> None:
        left, top, right, bottom = self._work_area()
        width = min(max(1, int(self.window.winfo_width())), max(1, right - left))
        height = min(max(1, int(self.window.winfo_height())), max(1, bottom - top))
        current_x = int(self.window.winfo_x())
        current_y = int(self.window.winfo_y())
        x = min(max(current_x, left), max(left, right - width))
        y = min(max(current_y, top), max(top, bottom - height))
        if (width, height, x, y) != (
            int(self.window.winfo_width()),
            int(self.window.winfo_height()),
            current_x,
            current_y,
        ):
            self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _bind_header_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._start_window_drag, add="+")
        widget.bind("<B1-Motion>", self._drag_window, add="+")
        widget.bind("<ButtonRelease-1>", self._end_window_drag, add="+")

    def _start_window_drag(self, event: tk.Event) -> None:
        self._window_drag_origin = (
            int(event.x_root),
            int(event.y_root),
            int(self.window.winfo_x()),
            int(self.window.winfo_y()),
        )

    def _drag_window(self, event: tk.Event) -> None:
        origin = self._window_drag_origin
        if origin is None:
            return
        start_x, start_y, window_x, window_y = origin
        left, top, right, bottom = self._work_area()
        width = min(max(1, int(self.window.winfo_width())), max(1, right - left))
        height = min(max(1, int(self.window.winfo_height())), max(1, bottom - top))
        x = min(max(window_x + int(event.x_root) - start_x, left), max(left, right - width))
        y = min(max(window_y + int(event.y_root) - start_y, top), max(top, bottom - height))
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _end_window_drag(self, _event: tk.Event) -> None:
        self._window_drag_origin = None

    def _log_once(self, message: str) -> None:
        method = getattr(self.logger, "warning", None)
        if callable(method):
            method(message)
