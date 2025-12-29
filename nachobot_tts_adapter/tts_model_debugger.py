import asyncio
import importlib
from pathlib import Path
from src.plugins.base_tts_model import BaseTTSModel
from typing import List
import soundfile as sf
import numpy as np


class TTSModelDebugger:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.tts_list: List[BaseTTSModel] = []

    def import_module(self):
        """动态导入TTS适配"""
        from src.config import Config

        config = Config(self.config_path)
        for tts in config.enabled_plugin.enabled:
            module_name = f"src.plugins.{tts}"
            try:
                module = importlib.import_module(module_name)
                tts_class: BaseTTSModel = module.TTSModel()
                self.tts_list.append(tts_class)
            except ImportError as e:
                print(f"Error importing {module_name}: {e}")
            except AttributeError as e:
                print(f"Error accessing TTSModel in {module_name}: {e}")
            except Exception as e:
                print(f"Unexpected error importing {module_name}: {e}")

    async def test_tts(self, text: str, platform: str):
        """测试TTS模型"""
        if not self.tts_list:
            print("没有启用任何TTS模型")
            return

        for tts_class in self.tts_list:
            print(f"测试模型: {tts_class.__class__.__name__}")
            try:
                audio_data = await tts_class.tts(text=text, platform=platform)
                audio_np = np.frombuffer(audio_data, dtype=np.int16)
                print(f"模型 {tts_class.__class__.__name__} 生成了音频数据，长度: {len(audio_data)} bytes")

                # 将音频数据写入WAV文件
                # output_file = f"{tts_class.__class__.__name__}_output.wav"
                # sf.write(output_file, audio_np, samplerate=48000, format='WAV')
                # print(f"音频已保存到 {output_file}")
            except Exception as e:
                print(f"模型 {tts_class.__class__.__name__} 处理失败: {e}")


if __name__ == "__main__":
    config_path = Path(__file__).parent / "configs" / "base.toml"
    debugger = TTSModelDebugger(str(config_path))
    debugger.import_module()

    text_to_test = "你好，这是一段测试文本。"
    platform_to_test = "qq"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(debugger.test_tts(text_to_test, platform_to_test))
