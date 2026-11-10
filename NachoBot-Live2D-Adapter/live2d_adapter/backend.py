"""Fixed Live2D renderer composition for the standalone product."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .config import StandaloneConfig
from .model_adapter import Live2DModelAdapter, ModelMetadata, inspect_model
from .model_runtime import Live2DModelRuntime, RuntimeOptions


class Live2DRendererAPI(Protocol):
    running: bool
    pcm_audio_ready: bool

    def run(self) -> None: ...


class Live2DBackend:
    """Prepare static metadata, then own the one independent Live2D renderer."""

    name = "live2d"

    def __init__(self, config: StandaloneConfig, logger: Any) -> None:
        self.config = config
        self.logger = logger
        self.metadata: ModelMetadata | None = None
        self.model_adapter: Live2DModelAdapter | None = None
        self.runtime_options: RuntimeOptions | None = None

    def prepare(self) -> dict[str, Any]:
        renderer = self.config.renderer
        if not renderer.model_path.is_file():
            raise FileNotFoundError(f"Live2D model file not found: {renderer.model_path}")
        metadata = inspect_model(renderer.model_path)
        moc_path = metadata.moc_path
        if moc_path is None or not moc_path.is_file():
            raise FileNotFoundError(f"Live2D model .moc3 file not found: {moc_path}")
        adaptation = self.config.adaptation
        self.model_adapter = Live2DModelAdapter(
            metadata,
            enabled=adaptation.enabled,
            parameter_mappings=adaptation.parameter_mappings,
            expression_mappings=adaptation.expression_mappings,
            action_mappings=self.config.action_mappings,
            logger=self.logger,
        )
        self.runtime_options = RuntimeOptions(
            width=renderer.width,
            height=renderer.height,
            scale=renderer.scale,
            adaptation_enabled=adaptation.enabled,
            parameter_mappings=adaptation.parameter_mappings,
            expression_mappings=adaptation.expression_mappings,
            action_mappings=self.config.action_mappings,
        )
        self.metadata = metadata
        report = self.model_adapter.describe()
        for warning in metadata.warnings:
            self.logger.warning("Live2D model metadata: {}", warning)
        self.logger.info("Live2D automatic adaptation: {}", report)
        return {"adaptation": report}

    def create_renderer(
        self,
        *,
        command_queue,
        voice_stream_queue,
        on_click,
        on_ready,
        on_exit,
    ) -> Live2DRendererAPI:
        if self.runtime_options is None or self.model_adapter is None:
            raise RuntimeError("Live2D backend must be prepared before creating a renderer")
        from .renderer import Live2DRenderer

        renderer = self.config.renderer
        return Live2DRenderer(
            model_path=renderer.model_path,
            logger=self.logger,
            command_queue=command_queue,
            transparent=renderer.transparent,
            hidden=renderer.hidden,
            antialiasing=renderer.antialiasing,
            width=renderer.width,
            height=renderer.height,
            scale=renderer.scale,
            track_mouse=renderer.track_mouse,
            on_click=on_click,
            on_ready=on_ready,
            on_exit=on_exit,
            voice_stream_queue=voice_stream_queue,
            model_adapter=self.model_adapter,
            runtime_options=self.runtime_options,
            runtime_factory=lambda: Live2DModelRuntime(logger=self.logger),
        )

    def resolve_action(self, action_id: str) -> str | None:
        if self.model_adapter is None:
            return None
        return self.model_adapter.resolve_action(action_id)

    def capabilities(self, renderer: Live2DRendererAPI | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "audio_playback": bool(renderer and getattr(renderer, "audio_ready", False)),
            "voice_stream_pcm": bool(renderer and renderer.pcm_audio_ready),
        }
        info = getattr(renderer, "model_info", None) if renderer is not None else None
        if info is not None:
            result.update(
                {
                    "model_parameter_count": len(info.parameter_ids),
                    "model_expression_count": len(info.expression_ids),
                    "model_motion_group_count": len(info.motion_groups),
                }
            )
        return result

    @property
    def model_path(self) -> Path:
        return self.config.renderer.model_path

    @staticmethod
    def normalize_mappings(values: Mapping[str, str]) -> dict[str, str]:
        """Compatibility helper for hosts that need a plain mapping copy."""

        return {str(key): str(value) for key, value in values.items()}
