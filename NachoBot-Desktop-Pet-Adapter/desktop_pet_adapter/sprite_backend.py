"""Built-in sprite renderer backend with no Live2D model dependency."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backend import BackendContext
from .config import AdapterConfig
from .sprite_renderer import SpriteAtlasManifest, SpriteRenderer


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent / "assets" / "nachobot_chibi" / "manifest.json"
)

SPRITE_ACTIONS = {
    "NOD": "react",
    "SHAKE_HEAD": "react",
    "TURN_LEFT": "react",
    "TURN_RIGHT": "react",
    "WINK": "react",
    "HAPPY": "react",
    "TILT_HEAD": "react",
    "LOOK_AWAY": "react",
}


class SpriteBackend:
    name = "sprite"
    model_adapter = None

    def __init__(self, config: AdapterConfig, logger: Any) -> None:
        self.config = config
        self.logger = logger
        self.manifest: SpriteAtlasManifest | None = None

    def prepare(self) -> dict[str, Any]:
        manifest_path = self.config.sprite.atlas_path or DEFAULT_MANIFEST
        if not manifest_path.is_file():
            raise FileNotFoundError(
                "sprite atlas manifest not found: "
                f"{manifest_path}. Generate the NachoBot chibi sprite atlas or set [sprite].atlas_path."
            )
        self.manifest = SpriteAtlasManifest.load(manifest_path)
        self.logger.info(
            "[Sprite] atlas validated: image={} frame={}x{} columns={} states={}",
            self.manifest.atlas_path,
            self.manifest.frame_width,
            self.manifest.frame_height,
            self.manifest.columns,
            tuple(self.manifest.states),
        )
        return {
            "atlas": str(self.manifest.atlas_path),
            "frame_width": self.manifest.frame_width,
            "frame_height": self.manifest.frame_height,
            "states": tuple(self.manifest.states),
        }

    def resolve_action(self, action_id: str) -> str | None:
        normalized = str(action_id).strip().upper()
        return self.config.action_mappings.get(normalized) or SPRITE_ACTIONS.get(normalized)

    def create_renderer(self, context: BackendContext) -> SpriteRenderer:
        if self.manifest is None:
            raise RuntimeError("Sprite backend must be prepared before creating its renderer")
        return SpriteRenderer(context, self.manifest)

    def capabilities(self, renderer: Any) -> dict[str, Any]:
        # The WAV commands are backed by their own reserved mixer channel.
        # PCM stream playback is advertised separately so a failed WAV channel
        # cannot make the websocket accept audio that the renderer will drop.
        audio_playback = bool(
            getattr(renderer, "audio_ready", False)
            and getattr(renderer, "_wav_channel", None) is not None
        )
        pcm_ready = bool(getattr(renderer, "pcm_audio_ready", False))
        commands = [
            "prepare_reply",
            "apply_control",
            "state",
            "speaking",
            "emotion",
            "action",
            "motion",
            "random_motion",
            "stop_audio",
        ]
        if audio_playback:
            commands.extend(("play_audio", "queue_audio"))
        if pcm_ready:
            commands.append("voice_stream")
        return {
            "sprite_animation": True,
            "autonomous_movement": bool(self.config.desktop_pet.enabled),
            "audio_playback": audio_playback,
            "voice_stream_pcm": pcm_ready,
            "gaze_control": False,
            "parameter_tween": False,
            "commands": commands,
        }


def create_backend(config: AdapterConfig, logger: Any) -> SpriteBackend:
    return SpriteBackend(config, logger)
