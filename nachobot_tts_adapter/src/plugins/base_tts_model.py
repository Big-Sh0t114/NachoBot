from abc import ABC, abstractmethod


class BaseTTSModel(ABC):
    @abstractmethod
    def __init__(self):
        """
        初始化插件

        Args:
            config (dict): 插件配置
        """
        self.config = self.load_config()

    @abstractmethod
    def load_config(self):
        """
        加载插件的自我配置文件

        Args:
            path (str): _description_
        """
        pass

    @abstractmethod
    async def tts(self, text: str, **kwargs) -> bytes:
        """
        非流式方式获取语音内容

        Args:
            text (str): 需要合成的语音内容
            **kwargs: 其他参数
        Returns:
            data (bytes): bytes格式的wav音频内容
        """
        pass

    @abstractmethod
    async def tts_stream(self, text: str, **kwargs):
        """
        流式方式获取语音内容

        Args:
            text (str): 需要合成的语音内容
            **kwargs: 其他参数
        """
        pass
