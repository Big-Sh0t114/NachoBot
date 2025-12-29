import aiohttp
import uuid
from pathlib import Path
from src.plugins.base_tts_model import BaseTTSModel
from .tts_config import DoubaoTTSBaseConfig
import base64


class TTSModel(BaseTTSModel):
    def __init__(self):
        """初始化TTS模型"""
        self.config = self.load_config()

    def load_config(self) -> "DoubaoTTSBaseConfig":
        """加载配置文件"""
        config_path = Path(__file__).parent.parent.parent.parent / "configs" / "Doubao_tts.toml"
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        return DoubaoTTSBaseConfig(str(config_path))

    async def tts(self, text: str, **kwargs) -> bytes:
        # sourcery skip: inline-immediately-returned-variable, reintroduce-else, swap-if-else-branches, use-named-expression
        """
        文本转语音
        Args:
            text (str): 合成内容
        Returns:
            音频二进制数据流(wav格式)
        """
        print(f"开始调用豆包TTS API生成音频，文本: {text[:30]}{'...' if len(text) > 30 else ''}")
        headers = {"Authorization": f"Bearer;{self.config.app.token}", "Content-Type": "application/json"}
        request_id = str(uuid.uuid4())
        payload = {
            "app": {
                "appid": self.config.app.appid,
                "token": self.config.app.token,
                "cluster": self.config.app.cluster,
            },
            "user": {"uid": request_id},
            "audio": {
                "voice_type": self.config.audio.voice_type,
                "encoding": "wav",
                "speed_ratio": self.config.audio.speed_ratio,
                "loudness_ratio": self.config.audio.loudness_ratio,
            },
            "request": {
                "reqid": request_id,
                "text": text,
                "operation": "query",
            },
        }
        if self.config.audio.explicit_language:
            payload["audio"]["explicit_language"] = self.config.audio.explicit_language
        if self.config.audio.context_language:
            payload["audio"]["context_language"] = self.config.audio.context_language
        if self.config.request.silence_duration > 0 and self.config.request.silence_duration < 30000:
            payload["request"]["silence_duration"] = self.config.request.silence_duration
            payload["request"]["enable_trailing_silence_audio"] = True
        audio_base64 = ""
        async with aiohttp.ClientSession() as session:
            async with session.post(self.config.app.base_url, headers=headers, json=payload) as response:
                if response.status != 200:
                    raise RuntimeError(f"豆包TTS API请求失败，状态码: {response.status}")
                resp_json = await response.json()
                if resp_json.get("code") != 3000:
                    raise RuntimeError(f"TTS请求失败: {resp_json.get('message')}")
                audio_base64 = resp_json.get("data")
                if not audio_base64:
                    raise RuntimeError("豆包TTS API未返回音频数据")
                audio_bytes = base64.b64decode(audio_base64)
                return audio_bytes

    async def tts_stream(self, text: str, **kwargs):
        """
        文本转语音，流式方式（豆包不支持）
        """
        raise RuntimeError("豆包API不支持HTTP流式方式")
