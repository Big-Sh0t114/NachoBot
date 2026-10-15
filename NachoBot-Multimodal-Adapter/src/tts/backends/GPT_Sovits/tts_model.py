import requests
import aiohttp
import os
import struct
from typing import AsyncIterable, AsyncIterator, Dict, Any, List
from pathlib import Path
from nachobot_multimodal.tts.base import (
    BaseTTSModel,
    PCMChunk,
    close_async_iterator,
    collect_pcm_stream_to_wav,
)
try:
    from tts_config import TTSBaseConfig, TTSPreset
except ImportError:
    from .tts_config import TTSBaseConfig, TTSPreset

response_error_status_list = [
    400,  # Bad Request
]


class _AsyncByteReader:
    """Read exact RIFF fields from an arbitrarily chunked async byte stream."""

    def __init__(self, source: AsyncIterable[bytes]):
        self._source = source.__aiter__()
        self._buffer = bytearray()
        self._eof = False

    async def _fill(self, size: int) -> None:
        while len(self._buffer) < size and not self._eof:
            try:
                chunk = await anext(self._source)
            except StopAsyncIteration:
                self._eof = True
                break
            if chunk:
                self._buffer.extend(chunk)

    async def read_exactly(self, size: int) -> bytes:
        await self._fill(size)
        if len(self._buffer) < size:
            raise ValueError("GPT-SoVITS returned a truncated WAV header")
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    async def skip(self, size: int) -> None:
        remaining = size
        while remaining:
            if self._buffer:
                amount = min(remaining, len(self._buffer))
                del self._buffer[:amount]
                remaining -= amount
                continue
            await self._fill(1)
            if not self._buffer:
                raise ValueError("GPT-SoVITS returned a truncated WAV chunk")

    async def read_some(self, size: int) -> bytes:
        await self._fill(1)
        if not self._buffer:
            return b""
        amount = min(size, len(self._buffer))
        result = bytes(self._buffer[:amount])
        del self._buffer[:amount]
        return result

    async def aclose(self) -> None:
        await close_async_iterator(self._source)


async def _iter_wav_pcm(source: AsyncIterable[bytes]) -> AsyncIterator[PCMChunk]:
    """Parse one streaming RIFF/WAVE header and yield only its PCM16 data."""

    reader = _AsyncByteReader(source)
    try:
        riff_header = await reader.read_exactly(12)
        if riff_header[:4] != b"RIFF" or riff_header[8:12] != b"WAVE":
            raise ValueError("GPT-SoVITS stream did not start with a RIFF/WAVE header")

        format_info: tuple[int, int, int] | None = None
        data_size: int | None = None
        while data_size is None:
            chunk_header = await reader.read_exactly(8)
            chunk_id, chunk_size = chunk_header[:4], struct.unpack("<I", chunk_header[4:])[0]
            if chunk_id == b"fmt ":
                if chunk_size < 16:
                    raise ValueError("GPT-SoVITS returned an invalid WAV fmt chunk")
                fmt = await reader.read_exactly(16)
                audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack(
                    "<HHIIHH", fmt
                )
                if audio_format != 1 or bits_per_sample != 16:
                    raise ValueError("GPT-SoVITS stream must contain uncompressed PCM16 audio")
                if channels <= 0 or sample_rate <= 0:
                    raise ValueError("GPT-SoVITS returned invalid WAV sample metadata")
                if block_align != channels * 2 or byte_rate != sample_rate * block_align:
                    raise ValueError("GPT-SoVITS returned inconsistent WAV PCM metadata")
                format_info = (channels, sample_rate, bits_per_sample // 8)
                await reader.skip(chunk_size - 16 + (chunk_size & 1))
            elif chunk_id == b"data":
                if format_info is None:
                    raise ValueError("GPT-SoVITS WAV data chunk appeared before its fmt chunk")
                data_size = chunk_size
            else:
                await reader.skip(chunk_size + (chunk_size & 1))

        if format_info is None:
            raise ValueError("GPT-SoVITS WAV stream omitted the fmt chunk")
        channels, sample_rate, sample_width = format_info
        # Streaming WAV writers commonly use 0 or 0xffffffff because the
        # final PCM size is not known when the header is emitted.
        remaining = None if data_size in (0, 0xFFFFFFFF) else data_size
        while remaining is None or remaining > 0:
            read_size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
            chunk = await reader.read_some(read_size)
            if not chunk:
                if remaining not in (None, 0):
                    raise ValueError("GPT-SoVITS WAV data ended before its declared length")
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield PCMChunk(
                data=chunk,
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )
    finally:
        await reader.aclose()


class TTSModel(BaseTTSModel):
    def __init__(
        self,
        config_path: str | Path | None = None,
        engine_host: str | None = None,
        engine_port: int | None = None,
    ):
        """初始化TTS模型"""
        self.config = self.load_config(config_path)
        if not self.config:
            raise ValueError("配置文件不存在或加载失败")
        # 记录配置文件所在目录，便于把相对路径转换为绝对路径
        self._config_dir = Path(self.config.config_path).parent.resolve()
        self.host = engine_host or os.environ.get("NACHOBOT_TTS_ENGINE_HOST") or self.config.tts.host
        self.port = int(engine_port or os.environ.get("NACHOBOT_TTS_ENGINE_PORT") or self.config.tts.port)

        self.base_url = f"http://{self.host}:{self.port}"
        self._ref_audio_path: str = None  # 存储当前使用的参考音频路径
        self._prompt_text: str = ""  # 存储当前使用的提示文本
        self._current_preset: str = ""  # 当前使用的角色预设名称
        self._initialized: bool = False  # 标记是否已完成初始化
        self._loaded_gpt_weights: str = ""  # 标记当前的gpt_weights名称
        self._loaded_sovits_weights: str = ""  # 标记当前的sovits_weights名称
        self.initialize()

    def load_config(self, config_path: str | Path | None = None) -> "TTSBaseConfig":
        """加载配置文件"""
        config_path = Path(config_path) if config_path else Path(__file__).resolve().parents[4] / "configs" / "gpt-sovits.toml"
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        return TTSBaseConfig(str(config_path))

    def initialize(self) -> None:
        """初始化模型和预设

        如果已经初始化过，则跳过
        """
        if self._initialized:
            return
        self._initialized = True
        # 设置默认角色预设
        if self.config:
            self.load_preset(self.config.pipeline.default_preset)
        else:
            raise RuntimeError("配置文件未加载或出现错误！")

    @property
    def ref_audio_path(self) -> str | None:
        """获取当前使用的参考音频路径"""
        return self._ref_audio_path

    @property
    def prompt_text(self) -> str | None:
        """获取当前使用的提示文本"""
        return self._prompt_text

    @property
    def current_preset(self) -> str | None:
        """获取当前使用的角色预设名称"""
        return self._current_preset

    def get_preset(self, preset_name: str) -> TTSPreset | None:
        """获取指定名称的角色预设配置

        Args:
            preset_name: 预设名称

        Returns:
            预设配置字典，如果不存在则返回None
        """
        if not self.config:
            return None

        presets = self.config.tts.models.presets
        return presets.get(preset_name)

    def load_preset(self, preset_name: str) -> None:
        """加载指定的角色预设

        Args:
            preset_name: 预设名称

        Raises:
            ValueError: 当预设不存在时抛出
        """
        if not self._initialized:
            self.initialize()
        preset = self.get_preset(preset_name)
        if not preset:
            raise ValueError(f"预设 {preset_name} 不存在")

        # 设置参考音频和提示文本
        self.set_refer_audio(self._resolve_path(preset.ref_audio_path), preset.prompt_text)

        # 如果预设指定了模型，则切换模型
        if preset.gpt_model:
            self.set_gpt_weights(self._resolve_path(preset.gpt_model))
        if preset.sovits_model:
            self.set_sovits_weights(self._resolve_path(preset.sovits_model))

        self._current_preset = preset_name

    def get_platform_preset(self, platform: str) -> str:
        """获取指定平台的角色预设配置

        Args:
            platform: 平台名称

        Returns:
            预设配置字典名称，如果不存在则返回None
        """
        preset = self.config.pipeline.platform_presets.get(platform)
        if not preset:
            print(f"平台 {platform} 没有指定预设，使用默认预设")
            return self.config.pipeline.default_preset
        return preset

    def set_refer_audio(self, audio_path: str, prompt_text: str) -> None:
        """设置参考音频和对应的提示文本

        Args:
            audio_path: 音频文件路径
            prompt_text: 对应的提示文本，必须提供

        Raises:
            ValueError: 当参数无效时抛出异常
        """
        if not audio_path:
            raise ValueError("audio_path不能为空")
        if not prompt_text:
            raise ValueError("prompt_text不能为空")

        self._ref_audio_path = audio_path
        self._prompt_text = prompt_text

    def set_gpt_weights(self, weights_path) -> None:
        """
        设置GPT权重

        Args:
            weights_path: 权重文件路径
        Raises:
            RuntimeError: 当设置gpt weights失败时抛出异常
        """
        if self._loaded_gpt_weights == weights_path:
            # 如果已经加载过相同的权重，则不需要重复设置
            return
        response = requests.get(f"{self.base_url}/set_gpt_weights", params={"weights_path": weights_path})
        if response.status_code != 200:
            raise RuntimeError(f"{response.json().get('message', '')}: {response.json().get('Exception', '')}")
        self._loaded_gpt_weights = weights_path

    def set_sovits_weights(self, weights_path):
        """
        设置SoVITS权重

        Args:
            weights_path: 权重文件路径
        Raises:
            RuntimeError: 当设置sovits weights失败时抛出异常
        """
        if self._loaded_sovits_weights == weights_path:
            # 如果已经加载过相同的权重，则不需要重复设置
            return
        response = requests.get(f"{self.base_url}/set_sovits_weights", params={"weights_path": weights_path})
        if response.status_code != 200:
            raise RuntimeError(f"{response.json().get('message', '')}: {response.json().get('Exception', '')}")
        self._loaded_sovits_weights = weights_path

    def build_parameters(
        self,
        text: str,
        ref_audio_path: str = None,
        aux_ref_audio_paths: List[str] = None,
        text_lang: str = None,
        prompt_text: str = None,
        prompt_lang: str = None,
        top_k: int = None,
        top_p: float = None,
        temperature: float = None,
        text_split_method: str = None,
        batch_size: int = None,
        batch_threshold: float = None,
        speed_factor: float = None,
        streaming_mode: bool = None,
        media_type: str = None,
        repetition_penalty: float = None,
        sample_steps: int = None,
        super_sampling: bool = None,
        preset_name: str = None,
    ) -> Dict[str, Any]:
        """构建请求参数"""
        if not self._initialized:
            self.initialize()

        # 优先使用传入的ref_audio_path和prompt_text,否则使用持久化的值
        ref_audio_path = ref_audio_path or self._ref_audio_path
        if not ref_audio_path:
            raise ValueError("未设置参考音频")

        prompt_text = prompt_text if prompt_text is not None else self._prompt_text
        preset_cfg = self.config.tts.models.presets.get(preset_name)
        global_cfg = self.config.tts

        final_text_lang = text_lang or preset_cfg.text_language or "auto"
        final_prompt_lang = prompt_lang or final_text_lang or preset_cfg.prompt_language or "zh"
        resolved_ref_audio_path = self._resolve_path(ref_audio_path)
        resolved_aux_ref_paths = [
            self._resolve_path(p) for p in (aux_ref_audio_paths or preset_cfg.aux_ref_audio_paths or [])
        ]

        params = {
            "text": text,
            "text_lang": final_text_lang,
            "ref_audio_path": resolved_ref_audio_path,
            "aux_ref_audio_paths": resolved_aux_ref_paths,
            "prompt_text": prompt_text or preset_cfg.prompt_text,
            "prompt_lang": final_prompt_lang,
            "top_k": top_k or global_cfg.top_k or 5,
            "top_p": top_p or global_cfg.top_p or 1.0,
            "temperature": temperature or global_cfg.temperature or 1.0,
            "text_split_method": text_split_method or global_cfg.text_split_method or "cut5",
            "batch_size": batch_size or global_cfg.batch_size or 1,
            "batch_threshold": batch_threshold or global_cfg.batch_threshold or 0.75,
            "speed_factor": speed_factor or preset_cfg.speed_factor or 1.0,
            "streaming_mode": str(streaming_mode if streaming_mode is not None else False),  # 缺省为False
            "media_type": media_type or global_cfg.media_type or "wav",
            "repetition_penalty": repetition_penalty or global_cfg.repetition_penalty or 1.35,
            "sample_steps": sample_steps or global_cfg.sample_steps or 32,
            "super_sampling": str(super_sampling or global_cfg.super_sampling),
        }
        params = {k: v for k, v in params.items() if v is not None}
        return params

    def _resolve_path(self, path: str) -> str:
        """将相对路径转换为绝对路径（相对配置文件目录）"""
        if not path:
            return path
        candidate = Path(path)
        if candidate.is_absolute():
            return str(candidate)
        return str((self._config_dir / candidate).resolve())

    async def tts(
        self,
        text: str,
        ref_audio_path: str = None,
        aux_ref_audio_paths: List[str] = None,
        text_lang: str = None,
        prompt_text: str = None,
        prompt_lang: str = None,
        top_k: int = None,
        top_p: float = None,
        temperature: float = None,
        text_split_method: str = None,
        batch_size: int = None,
        batch_threshold: float = None,
        speed_factor: float = None,
        media_type: str = None,
        repetition_penalty: float = None,
        sample_steps: int = None,
        super_sampling: bool = None,
        preset_name: str = None,
        **kwargs,
    ) -> bytes:
        """Collect the same streaming PCM inference path into a WAV file."""

        platform = kwargs.get("platform")
        if not platform:
            raise RuntimeError("未指定平台，请在kwargs中传入platform参数")

        return await collect_pcm_stream_to_wav(
            self.tts_pcm_stream(
                text,
                ref_audio_path=ref_audio_path,
                aux_ref_audio_paths=aux_ref_audio_paths,
                text_lang=text_lang,
                prompt_text=prompt_text,
                prompt_lang=prompt_lang,
                top_k=top_k,
                top_p=top_p,
                temperature=temperature,
                text_split_method=text_split_method,
                batch_size=batch_size,
                batch_threshold=batch_threshold,
                speed_factor=speed_factor,
                media_type=media_type,
                repetition_penalty=repetition_penalty,
                sample_steps=sample_steps,
                super_sampling=super_sampling,
                preset_name=preset_name,
                **kwargs,
            )
        )

    async def tts_pcm_stream(self, text: str, **kwargs) -> AsyncIterator[PCMChunk]:
        """Normalize GPT-SoVITS streaming WAV bytes to typed PCM16 chunks."""

        stream_kwargs = dict(kwargs)
        stream_kwargs["media_type"] = "wav"
        wav_stream = _iter_wav_pcm(self.tts_stream(text, **stream_kwargs))
        try:
            async for chunk in wav_stream:
                yield chunk
        finally:
            await close_async_iterator(wav_stream)

    def _resolve_preset_name(self, platform: str | None, preset_name: str | None) -> str:
        if not platform:
            print("未指定平台,使用默认平台")
            platform = "default"
        selected = preset_name or self.get_platform_preset(platform)
        if not self.get_preset(selected):
            raise ValueError(f"预设 {selected} 不存在")
        if self._current_preset != selected:
            self.load_preset(selected)
        return selected

    async def tts_stream(
        self,
        text,
        ref_audio_path=None,
        aux_ref_audio_paths=None,
        text_lang=None,
        prompt_text=None,
        prompt_lang=None,
        top_k=None,
        top_p=None,
        temperature=None,
        text_split_method=None,
        batch_size=None,
        batch_threshold=None,
        speed_factor=None,
        media_type=None,
        repetition_penalty=None,
        sample_steps=None,
        super_sampling=None,
        preset_name=None,
        **kwargs,
    ) -> AsyncIterator[bytes]:
        """Compatibility stream retaining the engine's original WAV bytes."""

        selected_preset = self._resolve_preset_name(kwargs.get("platform"), preset_name)
        params = self.build_parameters(
            text=text,
            ref_audio_path=ref_audio_path,
            aux_ref_audio_paths=aux_ref_audio_paths,
            text_lang=text_lang,
            prompt_text=prompt_text,
            prompt_lang=prompt_lang,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            text_split_method=text_split_method,
            batch_size=batch_size,
            batch_threshold=batch_threshold,
            speed_factor=speed_factor,
            streaming_mode=True,
            media_type=media_type,
            repetition_penalty=repetition_penalty,
            sample_steps=sample_steps,
            super_sampling=super_sampling,
            preset_name=selected_preset,
        )

        # Use an async-generator context so response and session resources are
        # closed by ``aclose()`` on normal completion, early exit, or cancel.
        timeout = aiohttp.ClientTimeout(total=None, connect=3.05, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.base_url}/tts",
                params=params,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    parsed_response = await response.json()
                    message = parsed_response.get("message", "未知错误")
                    exception_message = parsed_response.get("Exception", "")
                    raise aiohttp.ClientError(
                        f"请求失败: {response.status}, 错误信息: {message}"
                        + (f"，Exception: {exception_message}" if exception_message else "")
                    )

                response.raise_for_status()
                async for chunk in response.content.iter_chunked(4096):
                    if chunk:
                        yield chunk
