"""Browser-only Live2D model discovery, safe assets, and reply controls.

This module deliberately imports only the adapter's pure inspection and
control helpers. It never constructs the pygame renderer or desktop websocket
server. The browser renderer is served from pinned local vendor files.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import uuid
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

ROOT_DIR = Path(__file__).resolve().parent.parent
ADAPTER_DIR = ROOT_DIR / "NachoBot-Live2D-Adapter"
RESOURCES_DIR = ADAPTER_DIR / "resources"
CORE_FILE = ROOT_DIR / ".runtime" / "webui-live2d" / "live2dcubismcore.min.js"

_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "cache",
        "caches",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "site-packages",
    }
)
_MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
_MOTION_ACTIONS = (
    "NOD",
    "SHAKE_HEAD",
    "TURN_LEFT",
    "TURN_RIGHT",
    "WINK",
    "HAPPY",
    "TILT_HEAD",
    "LOOK_AWAY",
)


class Live2DModelError(ValueError):
    """A model is missing, malformed, unsafe, or not supported by the web UI."""


@dataclass(slots=True)
class _Model:
    model_id: str
    descriptor: Path
    directory: Path
    declared_assets: dict[str, Path]
    entry: dict[str, Any]
    adapter: Any
    fingerprint: tuple[tuple[str, int, int], ...]


@dataclass(slots=True)
class _CallState:
    model_id: str
    pipeline: Any
    model_adapter: Any
    last_access: float = field(default_factory=time.monotonic)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise Live2DModelError(f"{label} is too large")
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except Live2DModelError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Live2DModelError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise Live2DModelError(f"{label} must be a JSON object")
    return value


def _normalize_resource(value: Any) -> str:
    """Return a safe, descriptor-relative resource name or raise."""
    if not isinstance(value, str):
        raise Live2DModelError("resource reference must be a string")
    raw = value.strip()
    if not raw or len(raw) > 1024 or "\\" in raw or "%" in raw:
        raise Live2DModelError("resource reference is empty or unsafe")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise Live2DModelError("resource reference contains control characters")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise Live2DModelError("external or decorated resource references are not allowed")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise Live2DModelError("absolute resource references are not allowed")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise Live2DModelError("resource traversal is not allowed")
    return "/".join(parts)


def _descriptor_references(raw: dict[str, Any]) -> list[str]:
    file_refs = raw.get("FileReferences")
    if not isinstance(file_refs, dict):
        raise Live2DModelError("FileReferences must be an object")

    refs: list[str] = []

    def add(value: Any) -> None:
        if value is not None:
            refs.append(_normalize_resource(value))

    if not file_refs.get("Moc"):
        raise Live2DModelError("Moc is required")
    add(file_refs.get("Moc"))
    textures = file_refs.get("Textures", [])
    if not isinstance(textures, list) or not textures:
        raise Live2DModelError("at least one texture is required")
    for value in textures:
        add(value)
    for key in ("Physics", "Pose", "DisplayInfo", "UserData"):
        add(file_refs.get(key))

    expressions = file_refs.get("Expressions", [])
    if not isinstance(expressions, list):
        raise Live2DModelError("Expressions must be a list")
    for expression in expressions:
        if not isinstance(expression, dict):
            raise Live2DModelError("expression references must be objects")
        add(expression.get("File"))

    motions = file_refs.get("Motions", {})
    if not isinstance(motions, dict):
        raise Live2DModelError("Motions must be an object")
    for group in motions.values():
        if not isinstance(group, list):
            raise Live2DModelError("motion groups must be lists")
        for motion in group:
            if not isinstance(motion, dict):
                raise Live2DModelError("motion references must be objects")
            add(motion.get("File"))
            add(motion.get("Sound"))

    return list(dict.fromkeys(refs))


def _load_declared_assets(descriptor: Path, raw: dict[str, Any]) -> dict[str, Path]:
    model_dir = descriptor.parent.resolve(strict=True)
    refs = _descriptor_references(raw)
    assets: dict[str, Path] = {"": descriptor}
    for rel in refs:
        candidate = descriptor.parent.joinpath(*PurePosixPath(rel).parts)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise Live2DModelError("a declared model resource is missing") from exc
        if not _is_within(resolved, model_dir) or not resolved.is_file():
            raise Live2DModelError("a declared model resource escapes its model directory")
        assets[rel] = resolved
    # Discover Sound/SoundFile only in declared motion files. These are part of
    # the supported Cubism runtime graph, not arbitrary neighboring files.
    for rel in tuple(assets):
        if not rel.casefold().endswith(".motion3.json"):
            continue
        motion = _read_json(assets[rel], "motion descriptor")
        meta = motion.get("Meta")
        if not isinstance(meta, dict):
            continue
        for key in ("Sound", "SoundFile"):
            sound = meta.get(key)
            if sound is None:
                continue
            sound_rel = _normalize_resource(sound)
            candidate = descriptor.parent.joinpath(*PurePosixPath(sound_rel).parts)
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise Live2DModelError("a declared motion sound is missing") from exc
            if not _is_within(resolved, model_dir) or not resolved.is_file():
                raise Live2DModelError("a declared motion sound escapes its model directory")
            assets[sound_rel] = resolved
    return assets


def _descriptor_candidates(adapter_dir: Path) -> list[Path]:
    if not adapter_dir.is_dir():
        return []
    found: list[Path] = []
    for parent, dirs, files in os.walk(adapter_dir, followlinks=False):
        parent_path = Path(parent)
        dirs[:] = [
            name
            for name in dirs
            if name.casefold() not in _EXCLUDED_DIRS
            and not (parent_path / name).is_symlink()
        ]
        for name in files:
            if not name.casefold().endswith(".model3.json"):
                continue
            candidate = parent_path / name
            if candidate.is_symlink():
                continue
            found.append(candidate)
    return sorted(found, key=lambda path: path.relative_to(adapter_dir).as_posix().casefold())


def _display_name(descriptor: Path, adapter_dir: Path) -> str:
    relative_parent = descriptor.parent.relative_to(adapter_dir).as_posix()
    base = descriptor.name.removesuffix(".model3.json")
    return f"{base} ({relative_parent})" if relative_parent not in {"", "."} else base


class Live2DWebManager:
    """Own model discovery and staged controls independently of desktop UI."""

    def __init__(self, *, adapter_dir: Path | None = None, core_file: Path | None = None):
        self.adapter_dir = Path(adapter_dir or ADAPTER_DIR).resolve()
        self.core_file = Path(core_file or CORE_FILE)
        self._lock = threading.RLock()
        self._models: dict[str, _Model] = {}
        self._calls: dict[str, _CallState] = {}
        self._last_scan_at = 0.0
        self.scan_interval_seconds = 1.0
        self.call_ttl_seconds = 3600.0
        self.max_calls = 256
        self._scan()

    def _scan(self) -> None:
        adapter_path = str(self.adapter_dir)
        if adapter_path not in sys.path:
            sys.path.insert(0, adapter_path)
        try:
            from live2d_adapter.control_pipeline import ALLOWED_EMOTIONS, ControlPipeline
            from live2d_adapter.model_adapter import Live2DModelAdapter, inspect_model
        except ImportError:
            # Keep app startup safe when the optional adapter checkout is
            # absent. list_models() reports a stable, non-sensitive reason.
            self._models = {}
            self._control_pipeline_class = None
            self._model_adapter_class = None
            self._allowed_emotions = frozenset({"normal", "shy", "disgust", "angry"})
            self._last_scan_at = time.monotonic()
            return

        self._control_pipeline_class = ControlPipeline
        self._model_adapter_class = Live2DModelAdapter
        self._allowed_emotions = ALLOWED_EMOTIONS
        previous = self._models
        models: dict[str, _Model] = {}
        try:
            adapter_root = self.adapter_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            self._models = {}
            self._last_scan_at = time.monotonic()
            return
        for descriptor in _descriptor_candidates(self.adapter_dir):
            try:
                resolved_descriptor = descriptor.resolve(strict=True)
                if not _is_within(resolved_descriptor, adapter_root):
                    continue
                raw = _read_json(resolved_descriptor, "model descriptor")
                assets = _load_declared_assets(resolved_descriptor, raw)
                metadata = inspect_model(resolved_descriptor)
                if metadata.moc_path is None or not metadata.moc_path.is_file():
                    continue
                model_adapter = Live2DModelAdapter(metadata)
                description = model_adapter.describe()
                expressions = {
                    emotion: actual
                    for emotion in sorted(self._allowed_emotions)
                    if (actual := model_adapter.resolve_expression(emotion)) is not None
                }
                actions = {
                    action: actual
                    for action in _MOTION_ACTIONS
                    if (actual := model_adapter.resolve_action(action)) is not None
                }
                rel = resolved_descriptor.relative_to(adapter_root).as_posix()
                model_id = "l2d-" + hashlib.sha256(rel.casefold().encode("utf-8")).hexdigest()[:20]
                model_url = f"/api/chat/live2d/assets/{model_id}/{quote(resolved_descriptor.name)}"
                fingerprint_items: list[tuple[str, int, int]] = []
                for key, path in sorted(assets.items()):
                    stat = path.stat()
                    fingerprint_items.append((key, stat.st_mtime_ns, stat.st_size))
                fingerprint = tuple(fingerprint_items)
                existing = previous.get(model_id)
                if existing is not None and existing.fingerprint == fingerprint:
                    models[model_id] = existing
                    continue
                entry = {
                    "id": model_id,
                    "name": _display_name(resolved_descriptor, self.adapter_dir),
                    "model_url": model_url,
                    "lip_sync_parameters": list(description["lip_sync_parameters"]),
                    "expressions": expressions,
                    "actions": actions,
                }
                models[model_id] = _Model(
                    model_id=model_id,
                    descriptor=resolved_descriptor,
                    directory=resolved_descriptor.parent.resolve(),
                    declared_assets=assets,
                    entry=entry,
                    adapter=model_adapter,
                    fingerprint=fingerprint,
                )
            except (Live2DModelError, OSError, RuntimeError, ValueError, TypeError):
                continue
        self._models = models
        self._last_scan_at = time.monotonic()

    def _ensure_fresh(self) -> None:
        if time.monotonic() - self._last_scan_at >= self.scan_interval_seconds:
            self._scan()

    def _purge_calls(self) -> None:
        now = time.monotonic()
        expired = [
            call_id
            for call_id, state in self._calls.items()
            if now - state.last_access >= self.call_ttl_seconds
        ]
        for call_id in expired:
            state = self._calls.pop(call_id)
            state.pipeline.clear()
        while len(self._calls) > self.max_calls:
            oldest_id = min(self._calls, key=lambda key: self._calls[key].last_access)
            state = self._calls.pop(oldest_id)
            state.pipeline.clear()

    def refresh(self) -> None:
        """Rescan local resources; call explicitly after adding/removing models."""
        with self._lock:
            self._scan()

    def list_models(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_fresh()
            models = [dict(model.entry) for model in self._models.values()]
            if not self.core_ready:
                reason = "missing_core"
            elif not models:
                reason = "no_valid_models"
            else:
                reason = "ready"
            return {"available": reason == "ready", "reason": reason, "models": models}

    @property
    def core_ready(self) -> bool:
        try:
            return self.core_file.is_file() and not self.core_file.is_symlink()
        except OSError:
            return False

    def get_model(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_fresh()
            model = self._models.get(str(model_id))
            if model is None:
                raise Live2DModelError("unknown Live2D model")
            try:
                raw = _read_json(model.descriptor, "model descriptor")
                _load_declared_assets(model.descriptor, raw)
            except (Live2DModelError, OSError, RuntimeError) as exc:
                raise Live2DModelError("model resources are unavailable") from exc
            return dict(model.entry)

    def resolve_asset(self, model_id: str, asset_path: str) -> tuple[Path, str]:
        with self._lock:
            self._ensure_fresh()
            model = self._models.get(str(model_id))
            if model is None:
                raise Live2DModelError("unknown Live2D model")
            normalized = "" if asset_path in {"", model.descriptor.name} else _normalize_resource(asset_path)
            try:
                raw = _read_json(model.descriptor, "model descriptor")
                current_assets = _load_declared_assets(model.descriptor, raw)
            except (Live2DModelError, OSError, RuntimeError) as exc:
                raise Live2DModelError("model resources are unavailable") from exc
            if normalized == "":
                resolved = current_assets.get("")
                key = ""
            else:
                # The descriptor's URL includes the descriptor filename. All
                # resource paths resolve beside it, so allow only that exact
                # descriptor name or an exact declared relative resource.
                key = normalized
                resolved = current_assets.get(key)
            if resolved is None:
                raise Live2DModelError("asset is not declared by this model")
            try:
                current = resolved.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise Live2DModelError("asset is no longer available") from exc
            if not _is_within(current, model.directory) or not current.is_file():
                raise Live2DModelError("asset escaped the model directory")
            return current, mimetypes.guess_type(current.name)[0] or "application/octet-stream"

    def prepare_reply(self, call_id: str, model_id: str, raw_reply: Any) -> dict[str, Any]:
        call_key = str(call_id or "").strip()
        if not call_key or len(call_key) > 128:
            raise ValueError("call_id is required")
        with self._lock:
            self._ensure_fresh()
            self._purge_calls()
            model = self._models.get(str(model_id))
            if model is None:
                raise Live2DModelError("unknown Live2D model")
            state = self._calls.get(call_key)
            if state is None or state.model_id != model.model_id or state.model_adapter is not model.adapter:
                if state is not None:
                    state.pipeline.clear()
                state = _CallState(
                    model_id=model.model_id,
                    pipeline=self._control_pipeline_class(),
                    model_adapter=model.adapter,
                )
                self._calls[call_key] = state
            state.last_access = time.monotonic()
            control_id = uuid.uuid4().hex
            prepared = state.pipeline.prepare_reply(raw_reply, control_id, client_id="call")
            self._purge_calls()
            return {"reply": prepared.reply, "control_id": prepared.control_id}

    def apply_control(self, call_id: str, control_id: str) -> list[dict[str, str]]:
        with self._lock:
            self._ensure_fresh()
            self._purge_calls()
            state = self._calls.get(str(call_id or "").strip())
            model = self._models.get(state.model_id) if state else None
            if state is None or model is None or state.model_adapter is not model.adapter:
                return []
            state.last_access = time.monotonic()
            commands: list[dict[str, str]] = []

            def map_control(staged: Any) -> None:
                if staged.emotion:
                    actual = state.model_adapter.resolve_expression(staged.emotion)
                    if actual and actual in model.entry["expressions"].values():
                        commands.append({"type": "expression", "name": actual})
                if staged.action_id:
                    actual = state.model_adapter.resolve_action(staged.action_id)
                    if actual and actual in model.entry["actions"].values():
                        commands.append({"type": "motion", "group": actual})

            outcome = state.pipeline.apply(
                control_id,
                client_id="call",
                apply_callback=map_control,
            )
            if not outcome.applied:
                return []
            return commands

    def discard(self, call_id: str) -> None:
        with self._lock:
            state = self._calls.pop(str(call_id or "").strip(), None)
            if state is not None:
                state.pipeline.clear()


live2d_web_manager = Live2DWebManager()
router = APIRouter()


@router.get("/api/chat/live2d/models")
def list_live2d_models() -> dict[str, Any]:
    return live2d_web_manager.list_models()


@router.get("/api/chat/live2d/models/{model_id}")
def get_live2d_model(model_id: str) -> dict[str, Any]:
    try:
        return live2d_web_manager.get_model(model_id)
    except Live2DModelError as exc:
        raise HTTPException(status_code=404, detail="Live2D model not found") from exc


@router.get("/api/chat/live2d/assets/{model_id}/{asset_path:path}")
def get_live2d_asset(model_id: str, asset_path: str) -> FileResponse:
    try:
        path, media_type = live2d_web_manager.resolve_asset(model_id, asset_path)
    except Live2DModelError as exc:
        raise HTTPException(status_code=404, detail="Live2D asset not found") from exc
    return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


@router.get("/api/chat/live2d/runtime/core.js")
def get_live2d_core() -> FileResponse:
    if not live2d_web_manager.core_ready:
        raise HTTPException(status_code=503, detail="Live2D Cubism Core is unavailable")
    return FileResponse(
        live2d_web_manager.core_file,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


__all__ = [
    "Live2DModelError",
    "Live2DWebManager",
    "live2d_web_manager",
    "router",
]
