import aiohttp
import os
from typing import AsyncIterator, Optional, Dict, Any
from pathlib import Path
from nachobot_multimodal.tts.base import (
    BaseTTSModel,
    PCMChunk,
    close_async_iterator,
    collect_pcm_stream_to_wav,
)
from nachobot_multimodal.logger import logger
from nachobot_multimodal.utils.text_cleaner import clean_text_for_tts

try:
    from tts_config import VoxBaseConfig, VoxPreset
except ImportError:
    from .tts_config import VoxBaseConfig, VoxPreset


class TTSModel(BaseTTSModel):
    """VoxCPM TTS 纯 HTTP 客户端

    仅负责：加载配置 → 构建参数 → 发送 HTTP GET 请求到 Vox API Server。
    When enabled, emotion classification is owned by this selected Vox client
    and is fully loaded during construction; no callback to another HTTP
    service is involved.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        base_config_path: str | Path | None = None,
        engine_host: str | None = None,
        engine_port: int | None = None,
    ):
        """初始化 VoxCPM TTS 模型"""
        self.config = self.load_config(config_path)
        if not self.config:
            raise ValueError("VoxCPM 配置文件不存在或加载失败")
        self._config_dir = Path(self.config.config_path).parent.resolve()
        # ``base_config_path`` remains accepted for compatibility with the
        # resolver, but is deliberately not read: emotion is local to Vox.
        self._base_config_path = Path(base_config_path) if base_config_path else None
        self.host = engine_host or os.environ.get("NACHOBOT_TTS_ENGINE_HOST") or self.config.vox.host
        self.port = int(engine_port or os.environ.get("NACHOBOT_TTS_ENGINE_PORT") or self.config.vox.port)
        self.base_url = f"http://{self.host}:{self.port}"
        self._current_preset: str = ""
        self._initialized: bool = False

        self._emotion_classifier = None
        if self.config.emotion.enabled:
            # Keep this import conditional: GPT startup must not import the
            # classifier module or its torch/transformers dependencies.
            from nachobot_multimodal.utils.emotion_classifier import EmotionClassifier

            self._emotion_classifier = EmotionClassifier(
                model_name=self.config.emotion.classifier_model,
                device=self.config.emotion.classifier_device,
                use_fp16=self.config.emotion.use_fp16,
            )
            self._emotion_classifier.load()
            if not getattr(self._emotion_classifier, "loaded", True):
                raise RuntimeError("Vox emotion classifier did not become ready")

        self.initialize()

    def load_config(self, config_path: str | Path | None = None) -> "VoxBaseConfig":
        """加载 VoxCPM 配置文件"""
        config_path = Path(config_path) if config_path else Path(__file__).resolve().parents[4] / "configs" / "vox.toml"
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        return VoxBaseConfig(str(config_path))

    def initialize(self) -> None:
        """初始化模型和预设"""
        if self._initialized:
            return
        self._initialized = True
        if self.config:
            self._current_preset = self.config.pipeline.default_preset
        else:
            raise RuntimeError("配置文件未加载或出现错误！")

    def get_preset(self, preset_name: str) -> Optional[VoxPreset]:
        """获取指定名称的角色预设配置"""
        if not self.config:
            return None
        return self.config.vox.presets.get(preset_name)

    def get_platform_preset(self, platform: str) -> str:
        """获取指定平台的角色预设名称"""
        preset = self.config.pipeline.platform_presets.get(platform)
        if not preset:
            logger.info(f"平台 {platform} 没有指定预设，使用默认预设")
            return self.config.pipeline.default_preset
        return preset

    def _resolve_path(self, path: str) -> str:
        """将相对路径转换为绝对路径（相对配置文件目录）"""
        if not path:
            return path
        candidate = Path(path)
        if candidate.is_absolute():
            return str(candidate)
        return str((self._config_dir / candidate).resolve())

    def build_parameters(
        self,
        text: str,
        preset: VoxPreset,
        text_lang: str = None,
        split_method: str = None,
    ) -> Dict[str, Any]:
        """构建请求参数（兼容 GPT-SoVITS 风格的 GET 请求）

        Args:
            split_method: 覆盖配置文件中的切句方式。适配器外部已切分文本时
                          可传入 "cut0" 禁止 API Server 重复切句。
        """
        cfg_value = preset.cfg_value or self.config.vox.cfg_value
        inference_timesteps = preset.inference_timesteps or self.config.vox.inference_timesteps
        normalize = preset.normalize if preset.normalize is not None else self.config.vox.normalize

        # 处理参考音频路径
        ref_wav_path = ""
        if preset.ref_audio_path:
            resolved = self._resolve_path(preset.ref_audio_path)
            if Path(resolved).exists():
                ref_wav_path = resolved

        # 控制指令（极致克隆模式下禁用）
        prompt_text = (preset.prompt_text or "").strip()
        control_instruction = "" if prompt_text else (preset.control_instruction or "")

        params = {
            "text": text,
            "text_lang": text_lang or "auto",
            "control_instruction": control_instruction,
            "reference_wav_path": ref_wav_path,
            "prompt_text": prompt_text,
            "cfg_value": cfg_value,
            "inference_timesteps": inference_timesteps,
            "denoise": "false",
            "normalize": str(normalize).lower(),
            "media_type": "wav",
            "streaming_mode": "false",
            # 切句参数（允许外部覆盖以禁用重复切句）
            "split_method": split_method or self.config.vox.split_method,
            "max_split_length": self.config.vox.max_split_length,
            "segment_gap_ms": self.config.vox.segment_gap_ms,
        }
        return params

    @property
    def emotion_ready(self) -> bool:
        """Whether enabled emotion classification is loaded."""

        return not self.config.emotion.enabled or bool(
            self._emotion_classifier is not None
            and getattr(self._emotion_classifier, "loaded", True)
        )

    def resolve_emotion_preset(self, text: str) -> Optional[str]:
        """Map a classifier label/confidence to a configured Vox preset."""

        classifier = self._emotion_classifier
        if classifier is None:
            return None
        try:
            tag, confidence = classifier.classify(text)
            emotion = self.config.emotion
            if confidence < emotion.confidence_threshold:
                logger.info(
                    "情感置信度不足 ({:.3f} < {:.3f})，回退默认预设: {}",
                    confidence,
                    emotion.confidence_threshold,
                    emotion.default_emotion,
                )
                return emotion.default_emotion
            preset_name = emotion.label_preset_map.get(tag)
            if preset_name:
                logger.info("情感分类选择预设: {}", preset_name)
                return preset_name
            logger.info(
                "情感标签 {!r} 未配置预设映射，回退默认预设: {}",
                tag,
                emotion.default_emotion,
            )
            return emotion.default_emotion
        except Exception as exc:
            logger.warning("情感分类失败，使用平台默认预设: {}", exc)
            return None

    async def tts(self, text: str, **kwargs) -> bytes:
        """Collect the shared streaming inference path into a WAV file."""

        return await collect_pcm_stream_to_wav(self.tts_pcm_stream(text, **kwargs))

    def _resolve_stream_request(self, text: str, kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Resolve the existing Vox preset and request parameters once."""

        platform = kwargs.get("platform")
        if not platform:
            raise RuntimeError("未指定平台，请在kwargs中传入platform参数")

        text_lang = kwargs.get("text_lang")

        # 优先使用外部传入的预设名（由 TTS Adapter 情感分类决定）
        preset_name = kwargs.get("preset_name")

        # If no explicit preset was supplied, classify locally.  The old
        # callback-to-facade path is intentionally gone.
        if not preset_name:
            preset_name = self.resolve_emotion_preset(text)

        # 最终回退到平台默认预设
        if not preset_name:
            preset_name = self.get_platform_preset(platform)

        preset = self.get_preset(preset_name)
        if not preset:
            # 回退到平台默认预设
            logger.warning(f"预设 '{preset_name}' 不存在，回退到平台默认预设")
            preset_name = self.get_platform_preset(platform)
            preset = self.get_preset(preset_name)
            if not preset:
                raise ValueError(f"预设 {preset_name} 不存在")

        cleaned_text = clean_text_for_tts(text)
        if cleaned_text != text:
            logger.info(f"文本清洗: '{text[:60]}' -> '{cleaned_text[:60]}'")
        if not cleaned_text:
            raise ValueError("清洗后的 TTS 文本为空")

        self._current_preset = preset_name
        split_method = kwargs.get("split_method")
        params = self.build_parameters(cleaned_text, preset, text_lang=text_lang, split_method=split_method)
        return params, preset_name

    async def tts_pcm_stream(self, text: str, **kwargs) -> AsyncIterator[PCMChunk]:
        """Yield typed PCM16 chunks from VoxCPM's native streaming endpoint."""

        params, _preset_name = self._resolve_stream_request(text, kwargs)
        timeout = aiohttp.ClientTimeout(total=120, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.base_url}/tts_stream",
                params=params,
            ) as response:
                if response.status == 400:
                    error_response = await response.json()
                    error_message = error_response.get("message", "未知错误")
                    raise aiohttp.ClientError(f"请求失败: {response.status}, 错误信息: {error_message}")
                if response.status == 503:
                    raise aiohttp.ClientError("VoxCPM 模型未加载，请检查 API 服务器状态")
                response.raise_for_status()

                headers = getattr(response, "headers", {}) or {}

                def header_value(name: str, default: str | None = None) -> str | None:
                    value = headers.get(name)
                    if value is None:
                        value = headers.get(name.lower())
                    return str(value) if value is not None else default

                sample_rate_text = header_value("X-Sample-Rate")
                if sample_rate_text is None:
                    raise RuntimeError("VoxCPM streaming response omitted X-Sample-Rate")
                sample_rate = int(sample_rate_text)
                channels = int(header_value("X-Channels", "1"))
                sample_width = int(header_value("X-Sample-Width", "2"))
                if sample_rate <= 0 or channels <= 0 or sample_width != 2:
                    raise RuntimeError("VoxCPM returned unsupported PCM metadata")

                async for chunk in response.content.iter_any():
                    if chunk:
                        yield PCMChunk(
                            data=bytes(chunk),
                            sample_rate=sample_rate,
                            channels=channels,
                            sample_width=sample_width,
                        )

    async def tts_stream(self, text: str, **kwargs) -> AsyncIterator[bytes]:
        """Compatibility byte stream backed by the typed Vox PCM stream."""

        iterator = self.tts_pcm_stream(text, **kwargs).__aiter__()
        try:
            async for chunk in iterator:
                yield chunk.data
        finally:
            await close_async_iterator(iterator)

    async def tts_stream_to_file(self, text: str, **kwargs) -> Optional[str]:
        """Collect the native PCM stream into a WAV file for legacy callers."""
        import os
        import tempfile

        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            audio_data = await self.tts(text, **kwargs)
            with open(path, "wb") as output:
                output.write(audio_data)
            return path
        except Exception as e:
            logger.error(f"tts_stream_to_file error: {e}")
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return None
