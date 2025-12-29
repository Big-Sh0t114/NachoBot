import base64
import io
import numpy as np
import soundfile as sf
from typing import AsyncIterator
from openai import OpenAI
from pathlib import Path
from src.plugins.base_tts_model import BaseTTSModel
from .tts_config import OmniTTSConfig


class TTSModel(BaseTTSModel):
    def __init__(self):
        """初始化TTS模型"""
        self.config = self.load_config()

    def load_config(self) -> "OmniTTSConfig":
        """加载配置文件"""
        config_path = Path(__file__).parent.parent.parent.parent / "configs" / "qwen_omni.toml"
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        return OmniTTSConfig(str(config_path))

    async def tts(self, text: str, **kwargs) -> bytes:
        """文本转语音"""
        audio_chunk_buffer: str = ""
        audio_buffer = io.BytesIO()
        async for chunk in self._tts_stream(text, **kwargs):
            audio_chunk_buffer += chunk
        wav_bytes = base64.b64decode(audio_chunk_buffer)
        audio_data = np.frombuffer(wav_bytes, dtype=np.int16)
        sf.write(audio_buffer, audio_data, samplerate=24000, format="WAV")
        audio_buffer.seek(0)
        return audio_buffer.read()

    async def tts_stream(self, text: str, **kwargs) -> AsyncIterator[bytes]:
        """
        文本转语音，返回音频数据的字节流

        Args:
            text: 需要转换为语音的文本

        Returns:
            音频数据的字节流
        """
        async for base64_chunk in self._tts_stream(text, **kwargs):
            audio_data = base64.b64decode(base64_chunk)
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            audio_buffer = io.BytesIO()
            sf.write(audio_buffer, audio_np, samplerate=24000, format="WAV")
            audio_buffer.seek(0)
            yield audio_buffer.read()

    async def _tts_stream(self, text: str, **kwargs) -> AsyncIterator[bytes]:
        """
        使用大模型流式生成音频数据

        Args:
            text: 需要转换为语音的文本

        Returns:
            音频数据的PCM字节流
        """
        print(f"开始调用大模型API生成音频，文本: {text[:30]}{'...' if len(text) > 30 else ''}")
        prompt = f"复述这句话，不要输出其他内容，只输出'{text}'就好，不要输出其他内容，不要输出前后缀，不要输出'{text}'以外的内容，不要说：如果还有类似的需求或者想聊聊别的"
        print(f"生成prompt: {prompt}")
        client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        completion = client.chat.completions.create(
            model=self.config.model_name,
            messages=[{"role": "user", "content": prompt}],
            modalities=["audio", "text"],
            audio={
                "voice": self.config.voice_character,
                "format": self.config.media_format,
            },
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in completion:
            if hasattr(chunk, "choices") and chunk.choices:
                delta = chunk.choices[0].delta
                # 检查是否有音频数据
                if hasattr(delta, "audio") and "data" in delta.audio:
                    yield delta.audio["data"]
            if hasattr(chunk, "usage") and chunk.usage:
                # 处理使用情况
                print(f"本次使用量: {chunk.usage}")
