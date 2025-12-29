from __future__ import annotations

from src.chat.replyer.private_generator import PrivateReplyer
from src.config.config import model_config, global_config
from src.llm_models.utils_model import LLMRequest


class AdvancedPrivateReplyer(PrivateReplyer):
    """
    高级模式专用回复器。
    支持独立模型组（model_task_config.advanced_replyer），可通过配置开关。
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("request_type", "advanced_replyer")
        super().__init__(*args, **kwargs)
        if getattr(global_config.advanced, "use_advanced_replyer", True):
            advanced_model_set = getattr(model_config.model_task_config, "advanced_replyer", None)
            model_set = advanced_model_set or model_config.model_task_config.replyer
            # 使用高级模型组构造请求器
            self.express_model = LLMRequest(
                model_set=model_set, request_type=kwargs.get("request_type", "advanced_replyer")
            )
