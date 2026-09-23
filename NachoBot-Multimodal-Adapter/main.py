"""Uniform public TTS API used by the 9880 runtime supervisor.

The supervisor starts one backend-specific raw service on a loopback port,
waits for its readiness contract, then constructs exactly one fixed client
here before exposing this app. This module deliberately contains no config
fingerprint refresh on request boundaries and no platform transport relay.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field


# Keep all Hugging Face models in the adapter-owned cache directory. These
# environment defaults are inherited by the selected child and classifier.
ADAPTER_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ADAPTER_ROOT / "models" / "hf_cache"))
if os.getenv("NACHOBOT_HF_ENDPOINT", "").strip():
    os.environ["HF_ENDPOINT"] = os.environ["NACHOBOT_HF_ENDPOINT"].strip()
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

# Direct file-based imports (including ``python -I`` probes) do not put the
# adapter root on sys.path. Add the stable package root before importing the
# adapter namespace; do not depend on the caller's cwd.
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

SRC_PATH = ADAPTER_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from nachobot_multimodal.config import Config  # noqa: E402
from nachobot_multimodal.logger import logger  # noqa: E402
from nachobot_multimodal.utils import post_process  # noqa: E402
from nachobot_multimodal.utils.tts_resolver import resolve_tts_model_snapshot  # noqa: E402


PUBLIC_PORT = 9880
PRIVATE_PORT = 9881

_BACKEND_ALIASES = {
    "vox": "Vox",
    "voxcpm": "Vox",
    "gpt_sovits": "GPT_Sovits",
    "gpt-sovits": "GPT_Sovits",
    "gptsovits": "GPT_Sovits",
}


def normalize_backend(value: object) -> str:
    """Return the canonical backend plugin name or raise a clear error."""

    normalized = str(value or "").strip().lower()
    try:
        return _BACKEND_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("TTS backend must be Vox or GPT_Sovits") from exc


class WebUITTSRequest(BaseModel):
    """Stable Core-facing synthesis request."""

    text: str = Field(min_length=1, max_length=10_000)
    platform: str = Field(default="webui", max_length=64)
    text_lang: str | None = Field(default=None, max_length=32)


class TTSPipeline:
    """One fixed, already-initialized backend client.

    ``model`` is normally supplied by :class:`TTSRuntimeSupervisor` after the
    raw child is ready. The injectable path keeps unit tests lightweight and
    makes the startup/readiness boundary explicit.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        backend: str | None = None,
        model: Any | None = None,
        engine_host: str | None = None,
        engine_port: int | None = None,
        backend_alive: bool = True,
        public_host: str | None = None,
        public_port: int | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = Config(str(self.config_path))
        self.selected_backend = normalize_backend(backend) if backend else None
        self.model = model
        if self.model is None:
            self.model = self._construct_model(
                self.selected_backend,
                engine_host=engine_host,
                engine_port=engine_port,
            )
        if self.selected_backend is None:
            self.selected_backend = self._backend_from_model(self.model)

        self.tts_list = [self.model]
        self._backend_alive = bool(backend_alive)
        self._engine_ready = bool(getattr(self.model, "_initialized", True))
        self._webui_tts_lock = asyncio.Lock()
        self._stopped = False
        self.public_host = public_host or self.config.server.host
        self.public_port = int(public_port or self.config.server.port or PUBLIC_PORT)
        self.app = FastAPI(
            title="NachoBot TTS Runtime",
            docs_url=None,
            redoc_url=None,
        )
        self._register_http_endpoints()

    @staticmethod
    def _backend_from_model(model: Any) -> str:
        module = type(model).__module__.lower()
        if ".vox." in module or module.endswith("vox.tts_model"):
            return "Vox"
        if ".gpt_sovits." in module or "gpt_sovits" in module:
            return "GPT_Sovits"
        return type(model).__name__

    def _construct_model(
        self,
        backend: str | None,
        *,
        engine_host: str | None,
        engine_port: int | None,
    ) -> Any:
        snapshot = resolve_tts_model_snapshot(
            self.config_path,
            fixed_backend=backend,
        )
        if snapshot.model_class is None or snapshot.error:
            raise RuntimeError(snapshot.error or "TTS backend is unavailable")

        kwargs: dict[str, Any] = {"config_path": snapshot.backend_config_path}
        if snapshot.plugin == "Vox":
            kwargs["base_config_path"] = snapshot.base_config_path
        if engine_host is not None:
            kwargs["engine_host"] = engine_host
        if engine_port is not None:
            kwargs["engine_port"] = engine_port
        try:
            return snapshot.model_class(**kwargs)
        except TypeError:
            # Narrow compatibility path for injected/legacy model classes that
            # only accept the config path. Real selected backends accept the
            # explicit private endpoint arguments above.
            kwargs.pop("engine_host", None)
            kwargs.pop("engine_port", None)
            return snapshot.model_class(**kwargs)

    @property
    def model_ready(self) -> bool:
        """Whether the fixed client and its optional local classifier are ready."""

        model = getattr(self, "model", None)
        if model is None or not getattr(self, "_engine_ready", True):
            return False
        emotion_ready = getattr(model, "emotion_ready", True)
        return bool(emotion_ready)

    @property
    def ready(self) -> bool:
        return bool(
            getattr(self, "_backend_alive", True)
            and self.model_ready
            and not getattr(self, "_stopped", False)
        )

    @property
    def backend_alive(self) -> bool:
        return bool(getattr(self, "_backend_alive", True))

    def set_backend_alive(self, alive: bool) -> None:
        """Publish child liveness without replacing the fixed client."""

        self._backend_alive = bool(alive)

    def stop(self) -> None:
        self._stopped = True
        self._backend_alive = False

    def _register_http_endpoints(self) -> None:
        """Register only the uniform public routes."""

        @self.app.get("/api/health")
        async def health_endpoint() -> dict[str, Any]:
            return self._health_payload()

        @self.app.post("/api/tts")
        async def tts_endpoint(body: WebUITTSRequest) -> Response:
            if not self.ready:
                raise HTTPException(status_code=503, detail="TTS runtime is not ready")
            text = body.text.strip()
            if not text:
                raise HTTPException(status_code=400, detail="TTS text cannot be empty")
            try:
                async with self._webui_tts_lock:
                    # The child may exit while a request is waiting for the
                    # single-client lock. Recheck before using the client.
                    if not self.ready:
                        raise HTTPException(status_code=503, detail="TTS runtime is not ready")
                    audio_data = await self.model.tts(
                        text=text,
                        platform=body.platform,
                        text_lang=body.text_lang,
                    )
                if not audio_data:
                    raise HTTPException(status_code=502, detail="TTS returned empty audio")
                if getattr(self.config.tts_base_config, "post_process", False):
                    audio_data = post_process.simulate_telephone_voice(audio_data)
                return Response(
                    content=audio_data,
                    media_type="audio/wav",
                    headers={"Cache-Control": "no-store"},
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("TTS synthesis failed")
                raise HTTPException(status_code=502, detail=f"TTS generation failed: {exc}") from exc

    def _health_payload(self) -> dict[str, Any]:
        ready = self.ready
        return {
            "status": "ok" if ready else "unhealthy",
            "ready": ready,
            # Core's readiness adapter treats this as the canonical model
            # boundary.  Keep it independent from child liveness: Core also
            # combines it with ``ready``/``backend_alive`` below.
            "model_loaded": bool(self.model_ready),
            "backend": getattr(self, "selected_backend", None),
            "backend_alive": self.backend_alive,
            "engine_ready": self.model_ready,
            "emotion_ready": bool(getattr(getattr(self, "model", None), "emotion_ready", True)),
        }

    async def get_voice_no_stream(
        self,
        text: str,
        platform: str,
        text_lang: str | None = None,
    ) -> bytes | None:
        """Compatibility helper for local callers; uses the fixed client."""

        if not self.ready:
            return None
        try:
            async with self._webui_tts_lock:
                if not self.ready:
                    return None
                audio_data = await self.model.tts(
                    text=text,
                    platform=platform,
                    text_lang=text_lang,
                )
            if audio_data and getattr(self.config.tts_base_config, "post_process", False):
                audio_data = post_process.simulate_telephone_voice(audio_data)
            return audio_data or None
        except Exception as exc:
            logger.exception("TTS synthesis failed: {}", exc)
            return None


def create_app(
    config_path: str | Path,
    *,
    backend: str | None = None,
    model: Any | None = None,
    engine_host: str | None = None,
    engine_port: int | None = None,
    backend_alive: bool = True,
) -> FastAPI:
    """Build the fixed public app; startup callers retain the pipeline object."""

    return TTSPipeline(
        config_path,
        backend=backend,
        model=model,
        engine_host=engine_host,
        engine_port=engine_port,
        backend_alive=backend_alive,
    ).app


def main() -> None:
    """Delegate process ownership to the runtime manager."""

    from scripts.tts_runtime_manager import main as runtime_main

    raise SystemExit(
        runtime_main(
            [
                "serve",
                "--config",
                str(ADAPTER_ROOT / "configs" / "base.toml"),
            ]
        )
    )


if __name__ == "__main__":
    main()
