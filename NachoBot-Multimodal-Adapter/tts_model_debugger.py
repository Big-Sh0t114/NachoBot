import asyncio
from pathlib import Path
from typing import List

import numpy as np
import soundfile as sf

from nachobot_multimodal.tts.base import BaseTTSModel
from nachobot_multimodal.utils.tts_runtime import TTSRuntime


class TTSModelDebugger:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.tts_list: List[BaseTTSModel] = []
        self.runtime = TTSRuntime(config_dir=Path(config_path))

    def import_module(self):
        """Resolve the latest configured model without loading a stale cache."""

        if self.runtime.ensure_tts_model():
            self.tts_list = [self.runtime.model]
        else:
            self.tts_list = []
            print(f"Could not resolve TTS Model: {self.runtime.error}")

    async def test_tts(self, text: str, platform: str):
        """测试TTS模型"""

        try:
            async with self.runtime.model_context() as tts_class:
                self.tts_list = [tts_class]
                print(f"测试模型: {tts_class.__class__.__name__}")
                audio_data = await tts_class.tts(text=text, platform=platform)
                _audio_np = np.frombuffer(audio_data, dtype=np.int16)
                print(f"模型 {tts_class.__class__.__name__} 生成了音频数据，长度: {len(audio_data)} bytes")

                # 将音频数据写入WAV文件
                # output_file = f"{tts_class.__class__.__name__}_output.wav"
                # sf.write(output_file, audio_np, samplerate=48000, format='WAV')
        except Exception as exc:
            self.tts_list = [self.runtime.model] if self.runtime.model is not None else []
            print(f"模型处理失败: {exc}")


if __name__ == "__main__":
    config_path = Path(__file__).parent / "configs" / "base.toml"
    debugger = TTSModelDebugger(str(config_path))
    debugger.import_module()

    text_to_test = "你好，这是一段测试文本。"
    platform_to_test = "qq"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(debugger.test_tts(text_to_test, platform_to_test))
