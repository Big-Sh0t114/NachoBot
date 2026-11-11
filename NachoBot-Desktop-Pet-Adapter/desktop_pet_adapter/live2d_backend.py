"""Composition adapter for the optional independent Live2D provider."""

from __future__ import annotations

from typing import Any

from .backend import BackendContext, BackendSelectionError
from .config import AdapterConfig


class Live2DBackend:
    name = "live2d"

    def __init__(
        self,
        config: AdapterConfig,
        logger: Any,
        *,
        runtime_factory: Any,
        runtime_options: Any,
        runtime_frame_factory: Any,
        model_adapter_factory: Any,
    ) -> None:
        self.config = config
        self.logger = logger
        self._runtime_factory = runtime_factory
        self._runtime_options = runtime_options
        self._runtime_frame_factory = runtime_frame_factory
        self._model_adapter_factory = model_adapter_factory
        # This is provider-owned, pure descriptor metadata. The host never
        # receives it and never sees a native model handle.
        self.model_adapter = None

    def prepare(self) -> dict[str, Any]:
        renderer = self.config.renderer
        model_path = renderer.model_path
        if model_path is None:
            raise ValueError("[renderer].model_path is required by the Live2D backend")
        if not model_path.is_file():
            raise FileNotFoundError(f"Live2D model file not found: {model_path}")

        adaptation = self.config.adaptation
        self.model_adapter = self._model_adapter_factory.from_model_path(
            model_path,
            enabled=adaptation.enabled,
            parameter_mappings=adaptation.parameter_mappings,
            expression_mappings=adaptation.expression_mappings,
            action_mappings=self.config.action_mappings,
            logger=self.logger,
        )
        for warning in self.model_adapter.metadata.warnings:
            self.logger.warning("[Live2D] model metadata warning: {}", warning)
        report = self.model_adapter.describe()
        self.logger.info("Live2D automatic adaptation: {}", report)
        return {"adaptation": report}

    def resolve_action(self, action_id: str) -> str | None:
        """Resolve a canonical action through provider-owned model metadata."""

        if self.model_adapter is None:
            return None
        return self.model_adapter.resolve_action(action_id)

    def create_renderer(self, context: BackendContext) -> Any:
        from .live2d_host import Live2DHost

        model_path = context.config.renderer.model_path
        if model_path is None:
            raise ValueError("[renderer].model_path is required by the Live2D backend")
        renderer = context.config.renderer
        return Live2DHost(
            model_path=str(model_path),
            logger=context.logger,
            command_queue=context.command_queue,
            transparent=renderer.transparent,
            antialiasing=renderer.antialiasing,
            width=renderer.width,
            height=renderer.height,
            scale=renderer.scale,
            track_mouse=renderer.track_mouse,
            on_click=context.on_click,
            on_ready=context.on_ready,
            on_exit=context.on_exit,
            desktop_pet_config=context.config.desktop_pet,
            voice_stream_queue=context.voice_stream_queue,
            runtime_factory=self._runtime_factory,
            runtime_options=self._runtime_options,
            runtime_frame_factory=self._runtime_frame_factory,
        )

    def capabilities(self, renderer: Any) -> dict[str, Any]:
        info = getattr(renderer, "model_info", None)
        if info is None:
            return {}
        return {
            "model_parameter_count": len(info.parameter_ids),
            "model_expression_count": len(info.expression_ids),
            "model_motion_group_count": len(info.motion_groups),
        }


def create_backend(config: AdapterConfig, logger: Any) -> Live2DBackend:
    if config.renderer.model_path is None:
        raise ValueError("[renderer].model_path is required by the Live2D backend")
    try:
        from live2d_adapter import Live2DModelRuntime, RuntimeFrame, RuntimeOptions
        from live2d_adapter.model_adapter import Live2DModelAdapter
    except ImportError as exc:
        raise BackendSelectionError(
            "Live2D provider is not installed. Select backend = 'live2d' and start "
            "through launch_desktop_pet.bat, or run `uv run --project renderers/live2d "
            "--locked python -m desktop_pet_adapter --config <config.toml> --mode <mode>` "
            "from the Desktop Pet Adapter directory."
        ) from exc

    renderer = config.renderer
    adaptation = config.adaptation
    options = RuntimeOptions(
        width=renderer.width,
        height=renderer.height,
        scale=renderer.scale,
        adaptation_enabled=adaptation.enabled,
        parameter_mappings=adaptation.parameter_mappings,
        expression_mappings=adaptation.expression_mappings,
        action_mappings=config.action_mappings,
    )
    return Live2DBackend(
        config,
        logger,
        runtime_factory=lambda: Live2DModelRuntime(logger=logger),
        runtime_options=options,
        runtime_frame_factory=RuntimeFrame,
        model_adapter_factory=Live2DModelAdapter,
    )
