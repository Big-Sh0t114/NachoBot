"""Shared floating UI geometry; model zoom never changes persisted preferences."""

from __future__ import annotations

import math
from typing import Any

COLOR_KEY = "#ff00ff"
PALETTE = {
    "page": "#f4f6fa",
    "card": "#ffffff",
    "history": "#f7f9fc",
    "text": "#182230",
    "secondary": "#667085",
    "muted": "#98a2b3",
    "accent": "#0f9d8a",
    "accent_light": "#19b5a0",
    "accent_dark": "#087f70",
    "accent_bg": "#e9f8f4",
    "border": "#e4e9f0",
    "border_light": "#eef2f6",
    "user_bubble": "#e8f6f2",
    "assistant_bubble": "#ffffff",
    "shadow": "#dce2eb",
}


def ui_scale(value: float) -> float:
    number = float(value)
    return max(0.55, min(1.8, number)) if math.isfinite(number) else 1.0


def px(value: float, scale: float) -> int:
    return max(1, round(value * scale)) if value else 0


def popup_position(pet: Any, area: Any, size: Any, scale: float) -> tuple[int, int]:
    left, top, right, bottom = area
    width, height = size
    x = pet[0] - px(64, scale) - width
    if x < left:
        x = pet[2] + px(12, scale)
    return (
        max(left, min(right - width, x)),
        max(top, min(bottom - height, pet[1] + px(24, scale))),
    )


def rounded_surface(tk: Any, parent: Any, color: str, background: str, *, stretch: bool = False):
    """A rounded content surface that follows text height and model zoom."""
    canvas = tk.Canvas(
        parent, height=76, background=background, borderwidth=0, highlightthickness=0
    )
    canvas._pet_dynamic_canvas = True
    shape = canvas.create_polygon(0, 0, 1, 1, fill=color, outline="", smooth=True, splinesteps=28)
    frame = tk.Frame(canvas, background=color)
    item = canvas.create_window(12, 12, anchor="nw", window=frame, width=1)

    def layout(event: Any) -> None:
        factor = getattr(canvas, "_pet_ui_scale", 1.0)
        w, h = max(1, event.width), max(1, event.height)
        r, inset = min(px(14, factor), w / 2, h / 2), px(12, factor)
        canvas.coords(
            shape,
            r,
            0,
            w - r,
            0,
            w,
            0,
            w,
            r,
            w,
            h - r,
            w,
            h,
            w - r,
            h,
            r,
            h,
            0,
            h,
            0,
            h - r,
            0,
            r,
            0,
            0,
        )
        canvas.coords(item, inset, inset)
        canvas.itemconfigure(item, width=max(1, w - inset * 2))
        if stretch:
            canvas.itemconfigure(item, height=max(1, h - inset * 2))

    def fit(event: Any) -> None:
        if not stretch:
            canvas.configure(height=event.height + px(24, getattr(canvas, "_pet_ui_scale", 1.0)))

    canvas.bind("<Configure>", layout)
    frame.bind("<Configure>", fit)
    return canvas, frame


def action_button(tk: Any, parent: Any, text: str, command: Any, background: str) -> Any:
    canvas = tk.Canvas(
        parent,
        width=1,
        height=38,
        background=background,
        borderwidth=0,
        highlightthickness=0,
        cursor="hand2",
        takefocus=True,
    )
    canvas._pet_dynamic_canvas = True
    shape = canvas.create_polygon(0, 0, 1, 1, fill=PALETTE["accent_bg"], outline="", smooth=True)
    label = canvas.create_text(0, 0, text=text, fill=PALETTE["accent_dark"])
    armed = False

    def draw(_event: Any = None) -> None:
        w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        scale = getattr(canvas, "_pet_ui_scale", 1.0)
        radius = min(px(10, scale), h / 2)
        canvas.coords(
            shape,
            radius,
            0,
            w - radius,
            0,
            w,
            0,
            w,
            radius,
            w,
            h - radius,
            w,
            h,
            w - radius,
            h,
            radius,
            h,
            0,
            h,
            0,
            h - radius,
            0,
            radius,
            0,
            0,
        )
        canvas.coords(label, w / 2, h / 2)
        canvas.itemconfigure(label, font=("Microsoft YaHei UI", max(8, px(9, scale))))

    def hover(active: bool) -> None:
        canvas.itemconfigure(shape, fill=PALETTE["accent"] if active else PALETTE["accent_bg"])
        canvas.itemconfigure(label, fill="#ffffff" if active else PALETTE["accent_dark"])

    def press(_event: Any) -> None:
        nonlocal armed
        armed = True
        canvas.focus_set()

    def release(event: Any) -> None:
        nonlocal armed
        activate = (
            armed and 0 <= event.x < canvas.winfo_width() and 0 <= event.y < canvas.winfo_height()
        )
        armed = False
        if activate:
            command()

    canvas.bind("<Configure>", draw)
    canvas.bind("<Enter>", lambda _e: hover(True))
    canvas.bind("<Leave>", lambda _e: hover(False))
    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<ButtonRelease-1>", release)
    canvas.bind("<Return>", lambda _e: command())
    canvas.bind("<space>", lambda _e: command())
    return canvas


class ScrollThumb:
    """A quiet scrollbar without native arrow buttons or sunken chrome."""

    def __init__(self, tk: Any, parent: Any, command: Any, background: str) -> None:
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.drag_y, self.drag_first = 0.0, 0.0
        self.canvas = tk.Canvas(
            parent,
            width=6,
            background=background,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.canvas._pet_dynamic_canvas = True
        self.thumb = self.canvas.create_line(3, 0, 3, 1, fill="#cbd3df", width=3, capstyle="round")
        self.canvas.bind("<Configure>", lambda _e: self._draw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind(
            "<Enter>", lambda _e: self.canvas.itemconfigure(self.thumb, fill=PALETTE["accent"])
        )
        self.canvas.bind(
            "<Leave>", lambda _e: self.canvas.itemconfigure(self.thumb, fill="#cbd3df")
        )

    def pack(self, **options: Any) -> None:
        self.canvas.pack(**options)

    def set(self, first: Any, last: Any) -> None:
        self.first, self.last = float(first), float(last)
        self._draw()

    def _draw(self) -> None:
        height, width = max(1, self.canvas.winfo_height()), max(1, self.canvas.winfo_width())
        size = max(min(height, 24), height * (self.last - self.first))
        top = min(height - size, height * self.first)
        self.canvas.coords(
            self.thumb,
            width / 2,
            top + width / 2,
            width / 2,
            max(top + width / 2, top + size - width / 2),
        )
        self.canvas.itemconfigure(
            self.thumb,
            width=max(2, width - 2),
            state="hidden" if self.last - self.first >= 0.999 else "normal",
        )

    def _press(self, event: Any) -> None:
        height = max(1, self.canvas.winfo_height())
        if not self.first * height <= event.y <= self.last * height:
            span = self.last - self.first
            self.first = min(1 - span, max(0, event.y / height - span / 2))
            self.command("moveto", self.first)
        self.drag_y, self.drag_first = event.y, self.first

    def _drag(self, event: Any) -> None:
        fraction = self.drag_first + (event.y - self.drag_y) / max(1, self.canvas.winfo_height())
        self.command("moveto", max(0, min(1, fraction)))


def scale_widgets(widget: Any, scale: float) -> None:
    """Scale from original metrics, preserving widgets, drafts and slider drags."""
    import tkinter.font as fonts
    from tkinter import TclError

    options = widget.keys() or []
    if not options:
        return
    base = getattr(widget, "_pet_ui_metrics", None)
    if not isinstance(base, dict):
        base = {"options": {}, "pack": {}, "items": {}}
        for name in ("padx", "pady", "borderwidth", "highlightthickness", "wraplength"):
            if name in options:
                base["options"][name] = float(str(widget.cget(name)))
        if widget.winfo_class() in {"Canvas", "Frame", "Scrollbar"}:
            for name in ("width", "height"):
                if name in options:
                    base["options"][name] = float(str(widget.cget(name)))
        if "font" in options:
            base["font"] = fonts.Font(root=widget, font=widget.cget("font")).actual()
        if widget.winfo_manager() == "pack":
            info = widget.pack_info()
            for name in ("padx", "pady", "ipadx", "ipady"):
                raw = info[name]
                values = raw if isinstance(raw, (tuple, list)) else widget.tk.splitlist(str(raw))
                base["pack"][name] = tuple(float(str(v)) for v in values)
        widget._pet_ui_metrics = base
    if getattr(widget, "_pet_ui_scale", None) != scale:
        for name, value in base["options"].items():
            widget.configure(**{name: px(value, scale)})
        if "font" in base:
            font = base["font"]
            size = int(font["size"])
            widget.configure(
                font=(
                    font["family"],
                    max(8, px(abs(size), scale)) * (-1 if size < 0 else 1),
                    font["weight"],
                    font["slant"],
                )
            )
        if base["pack"]:
            widget.pack_configure(
                **{
                    name: tuple(px(v, scale) for v in values)
                    for name, values in base["pack"].items()
                }
            )
        if widget.winfo_class() == "Canvas" and not getattr(widget, "_pet_dynamic_canvas", False):
            for item in widget.find_all():
                if item not in base["items"]:
                    kind = widget.type(item)
                    attributes = {}
                    for key in ("width", "height") if kind == "window" else ("width",):
                        try:
                            attributes[key] = float(str(widget.itemcget(item, key)))
                        except (ValueError, TclError):
                            pass
                    if kind == "text":
                        font = fonts.Font(root=widget, font=widget.itemcget(item, "font")).actual()
                        attributes["font"] = font
                    base["items"][item] = (widget.coords(item), attributes)
                coords, attributes = base["items"][item]
                widget.coords(item, *(float(v) * scale for v in coords))
                for key, value in attributes.items():
                    if key == "font":
                        widget.itemconfigure(
                            item,
                            font=(
                                value["family"],
                                max(8, px(abs(value["size"]), scale)),
                                value["weight"],
                                value["slant"],
                            ),
                        )
                    else:
                        widget.itemconfigure(item, **{key: px(value, scale)})
        widget._pet_ui_scale = scale
    for child in widget.winfo_children():
        scale_widgets(child, scale)


def popup_shell(tk: Any, window: Any, color: str, width: int, height: int) -> Any:
    """A rounded, borderless surface with no native title bar or nested card frame."""
    window.configure(background=COLOR_KEY)
    try:
        window.attributes("-transparentcolor", COLOR_KEY)
        window.attributes("-alpha", 1.0)
    except tk.TclError:
        pass
    canvas = tk.Canvas(
        window,
        width=width,
        height=height,
        background=COLOR_KEY,
        borderwidth=0,
        highlightthickness=0,
    )
    canvas._pet_dynamic_canvas = True
    canvas.pack(fill="both", expand=True)
    shell = tk.Frame(canvas, background=color)
    shadow = canvas.create_polygon(
        0, 0, 1, 1, fill=PALETTE["shadow"], outline="", smooth=True, splinesteps=24
    )
    shape = canvas.create_polygon(0, 0, 1, 1, fill=color, outline="", smooth=True, splinesteps=24)
    content = canvas.create_window(
        12, 12, anchor="nw", window=shell, width=width - 24, height=height - 24
    )

    def layout(event: Any) -> None:
        factor = getattr(window, "_pet_scale", 1.0)
        radius = px(20, factor)
        w, h = event.width - 1, event.height - 1
        coords = (
            radius,
            0,
            w - radius,
            0,
            w,
            0,
            w,
            radius,
            w,
            h - radius,
            w,
            h,
            w - radius,
            h,
            radius,
            h,
            0,
            h,
            0,
            h - radius,
            0,
            radius,
            0,
            0,
        )
        canvas.coords(shadow, *coords)
        surface = list(coords)
        for index in range(1, len(surface), 2):
            if surface[index] > h / 2:
                surface[index] -= px(4, factor)
        canvas.coords(shape, *surface)
        inset = px(14, factor)
        canvas.coords(content, inset, inset)
        canvas.itemconfigure(content, width=max(1, w - inset * 2), height=max(1, h - inset * 2))

    canvas.bind("<Configure>", layout)
    return shell
