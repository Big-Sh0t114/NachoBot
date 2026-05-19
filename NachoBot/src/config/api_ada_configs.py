from dataclasses import dataclass, field
from typing import Optional

from src.common.logger import get_logger
from .config_base import ConfigBase

logger = get_logger("api_ada_configs")


@dataclass
class APIProvider(ConfigBase):
    """API提供商配置类"""

    name: str
    """API提供商名称"""

    base_url: str
    """API基础URL"""

    api_key: str = field(default_factory=str, repr=False)
    """API密钥列表"""

    client_type: str = field(default="openai")
    """客户端类型（如openai/google等，默认为openai）"""

    max_retry: int = 2
    """最大重试次数（单个模型API调用失败，最多重试的次数）"""

    timeout: int = 10
    """API调用的超时时长（超过这个时长，本次请求将被视为“请求超时”，单位：秒）"""

    retry_interval: int = 10
    """重试间隔（如果API调用失败，重试的间隔时间，单位：秒）"""

    def get_api_key(self) -> str:
        return self.api_key

    def __post_init__(self):
        """确保api_key在repr中不被显示"""
        if not self.api_key:
            raise ValueError("API密钥不能为空，请在配置中设置有效的API密钥。")
        if not self.base_url and self.client_type != "gemini":
            raise ValueError("API基础URL不能为空，请在配置中设置有效的基础URL。")
        if not self.name:
            raise ValueError("API提供商名称不能为空，请在配置中设置有效的名称。")


@dataclass
class ModelInfo(ConfigBase):
    """单个模型信息配置类"""

    model_identifier: str
    """模型标识符（用于URL调用）"""

    name: str
    """模型名称（用于模块调用）"""

    api_provider: str
    """API提供商（如OpenAI、Azure等）"""

    price_in: float = field(default=0.0)
    """每M token输入价格"""

    price_out: float = field(default=0.0)
    """每M token输出价格"""

    force_stream_mode: bool = field(default=False)
    """是否强制使用流式输出模式"""

    extra_params: dict = field(default_factory=dict)
    """额外参数（用于API调用时的额外配置）"""

    def __post_init__(self):
        if not self.model_identifier:
            raise ValueError("模型标识符不能为空，请在配置中设置有效的模型标识符。")
        if not self.name:
            raise ValueError("模型名称不能为空，请在配置中设置有效的模型名称。")
        if not self.api_provider:
            raise ValueError("API提供商不能为空，请在配置中设置有效的API提供商。")


@dataclass
class TaskConfig(ConfigBase):
    """任务配置类"""

    model_list: list[str] = field(default_factory=list)
    """任务使用的模型列表"""

    max_tokens: int = 1024
    """任务最大输出token数"""

    temperature: float = 0.3
    """模型温度"""

    timeout: Optional[int] = None
    """任务超时时长（秒），如果设置，将覆盖API提供商的默认超时时长"""


@dataclass
class ModelTaskConfig(ConfigBase):
    """模型配置类"""

    utils: TaskConfig
    """组件模型配置"""

    utils_small: TaskConfig
    """组件小模型配置"""

    replyer0: TaskConfig
    """默认回复模型组（原 replyer）"""

    vlm: TaskConfig
    """视觉语言模型配置"""

    voice: TaskConfig
    """语音识别模型配置"""

    tool_use: TaskConfig
    """专注工具使用模型配置"""

    planner: TaskConfig
    """规划模型配置"""

    embedding: TaskConfig
    """嵌入模型配置"""

    lpmm_entity_extract: TaskConfig
    """LPMM实体提取模型配置"""

    lpmm_rdf_build: TaskConfig
    """LPMM RDF构建模型配置"""

    lpmm_qa: TaskConfig
    """LPMM问答模型配置"""

    replyer1: TaskConfig = field(default_factory=TaskConfig)
    """备用回复模型组1（可选）"""

    replyer2: TaskConfig = field(default_factory=TaskConfig)
    """备用回复模型组2（可选）"""

    private_replyer0: TaskConfig = field(default_factory=TaskConfig)
    """私聊默认回复模型组（可选，缺省回退到默认参数）"""

    private_replyer1: TaskConfig = field(default_factory=TaskConfig)
    """私聊备用回复模型组1（可选）"""

    private_replyer2: TaskConfig = field(default_factory=TaskConfig)
    """私聊备用回复模型组2（可选）"""

    advanced_replyer: TaskConfig = field(default_factory=TaskConfig)
    """高级模式回复模型配置（可选，缺省回退到默认参数）"""

    web_search: TaskConfig = field(default_factory=TaskConfig)
    """联网搜索模型配置（可选，缺省回退到默认参数）"""

    mcp: TaskConfig = field(default_factory=TaskConfig)
    """MCP插件专用模型配置"""

    file_edit: TaskConfig = field(default_factory=TaskConfig)
    """文件编辑/代码生成专用模型配置"""

    video: TaskConfig = field(default_factory=TaskConfig)
    """视频解析专用模型配置"""

    bilibili_vlm: TaskConfig = field(default_factory=TaskConfig)
    """Bilibili 直播画面识别专用配置"""

    bilibili_replyer: TaskConfig = field(default_factory=TaskConfig)
    """Bilibili 直播间专属独立回复模型组（可选，缺省回退到默认参数）"""

    _active_replyer_group: int = field(default=0, repr=False, init=False)
    """当前激活的 replyer 组编号（0 或 1），运行时状态"""

    def __post_init__(self):
        # 尝试从本地存储加载已激活的全局回复模型组
        try:
            from src.manager.local_store_manager import local_storage
            saved_group = local_storage["global_active_replyer_group"]
            if isinstance(saved_group, int) and saved_group in [0, 1, 2]:
                success = self.switch_replyer_group(saved_group)
                if not success:
                    self.replyer = self.replyer0
            else:
                self.replyer = self.replyer0
        except Exception as e:
            logger.warning(f"加载全局模型组状态失败: {e}")
            self.replyer = self.replyer0

    def switch_replyer_group(self, group: int) -> bool:
        """切换全局默认回复模型组

        Args:
            group: 目标组编号（0 或 1）

        Returns:
            是否切换成功
        """
        from src.manager.local_store_manager import local_storage
        
        if group == 0:
            self.replyer = self.replyer0
            self._active_replyer_group = 0
            local_storage["global_active_replyer_group"] = 0
            logger.info("已切换默认回复模型组为 replyer0")
            return True
        elif group == 1:
            if not self.replyer1.model_list:
                logger.warning("replyer1 未配置模型列表，切换失败")
                return False
            self.replyer = self.replyer1
            self._active_replyer_group = 1
            local_storage["global_active_replyer_group"] = 1
            logger.info("已切换默认回复模型组为 replyer1")
            return True
        elif group == 2:
            if not self.replyer2.model_list:
                logger.warning("replyer2 未配置模型列表，切换失败")
                return False
            self.replyer = self.replyer2
            self._active_replyer_group = 2
            local_storage["global_active_replyer_group"] = 2
            logger.info("已切换默认回复模型组为 replyer2")
            return True
        else:
            logger.warning(f"无效的 replyer 组编号: {group}")
            return False

    def get_private_replyer(self, group: int = 0) -> TaskConfig:
        """获取指定组别的私聊回复模型组配置。如果未配置则回退。

        Args:
            group: 目标私聊组编号（0、1 或 2）

        Returns:
            TaskConfig: 最终应该使用的模型配置
        """
        if group == 0:
            return self.private_replyer0 if self.private_replyer0.model_list else self.replyer
        elif group == 1:
            return self.private_replyer1 if self.private_replyer1.model_list else self.replyer1
        elif group == 2:
            return self.private_replyer2 if self.private_replyer2.model_list else self.replyer2
        else:
            logger.warning(f"无效的私聊 replyer 组编号: {group}，回退到默认组")
            return self.replyer

    def get_task(self, task_name: str) -> TaskConfig:
        """获取指定任务的配置"""
        if hasattr(self, task_name):
            return getattr(self, task_name)
        raise ValueError(f"任务 '{task_name}' 未找到对应的配置")
