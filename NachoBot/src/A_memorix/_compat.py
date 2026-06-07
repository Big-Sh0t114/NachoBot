"""A_Memorix 兼容层 - 桥接上游依赖到 NachoBot 基础设施。

本文件为 A_Memorix 子系统提供所有外部依赖的垫片实现，
避免直接修改 A_Memorix 内部代码，便于后续同步上游更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
_NACHOBOT_ROOT = Path(__file__).resolve().parent.parent.parent  # NachoBot/
BOT_CONFIG_PATH = _NACHOBOT_ROOT / "config" / "bot_config.toml"


# ---------------------------------------------------------------------------
# AMemorixConfig (Pydantic 替代品 - 使用 dataclass)
# ---------------------------------------------------------------------------
@dataclass
class _PluginConfig:
    enabled: bool = True

@dataclass
class _EmbeddingConfig:
    api_url: str = ""
    model: str = ""
    dimension: int = 1024
    batch_size: int = 32
    api_key: str = ""

@dataclass
class _StorageConfig:
    data_dir: str = ""
    auto_save: bool = True
    auto_save_interval_seconds: int = 300

@dataclass
class _SearchConfig:
    default_limit: int = 5
    score_threshold: float = 0.3
    enable_sparse: bool = True
    sparse_tokenizer: str = "mixed"
    rrf_k: int = 60

@dataclass
class _V5LifecycleConfig:
    enabled: bool = True
    half_life_hours: float = 24.0
    inactive_threshold: float = 0.1
    prune_threshold: float = 0.01

@dataclass
class _WebConfig:
    enabled: bool = False
    import_config: dict = field(default_factory=dict)

@dataclass
class _SummarizationConfig:
    model_name: List[str] = field(default_factory=lambda: ["auto"])

@dataclass
class _EpisodeConfig:
    segmentation_model: str = "auto"

@dataclass
class _SharedMemoryGroupConfig:
    targets: List[Any] = field(default_factory=list)

@dataclass
class _IntegrationConfig:
    heuristic_memory_cross_chat_enabled: bool = False
    heuristic_memory_group_to_private_enabled: bool = False
    heuristic_memory_private_to_group_enabled: bool = False

@dataclass
class _CrossChatMemoryConfig:
    enabled: bool = False
    group_list_type: str = "blacklist"
    group_list: List[str] = field(default_factory=list)
    private_list_type: str = "whitelist"
    private_list: List[str] = field(default_factory=list)

@dataclass
class AMemorixConfig:
    """A_Memorix 配置模型，模拟上游 Pydantic 模型的 dataclass 版本。"""
    plugin: _PluginConfig = field(default_factory=_PluginConfig)
    embedding: _EmbeddingConfig = field(default_factory=_EmbeddingConfig)
    storage: _StorageConfig = field(default_factory=_StorageConfig)
    search: _SearchConfig = field(default_factory=_SearchConfig)
    v5_lifecycle: _V5LifecycleConfig = field(default_factory=_V5LifecycleConfig)
    web: _WebConfig = field(default_factory=_WebConfig)
    summarization: _SummarizationConfig = field(default_factory=_SummarizationConfig)
    episode: _EpisodeConfig = field(default_factory=_EpisodeConfig)
    shared_memory_groups: List[_SharedMemoryGroupConfig] = field(default_factory=list)
    integration: _IntegrationConfig = field(default_factory=_IntegrationConfig)
    cross_chat_memory: _CrossChatMemoryConfig = field(default_factory=_CrossChatMemoryConfig)

    def model_dump(self, mode: str = "json") -> Dict[str, Any]:
        """模拟 Pydantic model_dump()"""
        import dataclasses
        def _to_dict(obj):
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
            if isinstance(obj, list):
                return [_to_dict(v) for v in obj]
            if isinstance(obj, dict):
                return {k: _to_dict(v) for k, v in obj.items()}
            return obj
        return _to_dict(self)


# ---------------------------------------------------------------------------
# AMemorixConfigUtils (上游 src.common.utils.utils_config)
# ---------------------------------------------------------------------------
class AMemorixConfigUtils:
    """上游 AMemorixConfigUtils 的最小垫片。"""

    @staticmethod
    def get_shared_memory_session_ids(chat_id: str) -> List[str]:
        """获取与当前聊天流共享长期记忆检索范围的真实聊天流 ID。"""
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            return []

        config = getattr(config_manager.a_memorix, "cross_chat_memory", _CrossChatMemoryConfig())
        if not bool(getattr(config, "enabled", False)):
            return []

        current_access = AMemorixConfigUtils._get_stream_access(clean_chat_id, config)
        if "read" not in current_access:
            return []

        resolved_session_ids: List[str] = []
        for session_id in AMemorixConfigUtils._iter_existing_session_ids():
            if session_id == clean_chat_id:
                continue
            source_access = AMemorixConfigUtils._get_stream_access(session_id, config)
            if "write" in source_access:
                resolved_session_ids.append(session_id)
        return sorted(set(resolved_session_ids))

    @staticmethod
    def _iter_existing_session_ids() -> List[str]:
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager

            return list(get_chat_manager().streams.keys())
        except Exception:
            return []

    @staticmethod
    def _get_stream_access(session_id: str, config: _CrossChatMemoryConfig) -> set[str]:
        stream_type = AMemorixConfigUtils._get_stream_type(session_id)
        if stream_type == "group":
            list_type = str(config.group_list_type or "blacklist").strip().lower()
            target_list = config.group_list or []
        elif stream_type == "private":
            list_type = str(config.private_list_type or "whitelist").strip().lower()
            target_list = config.private_list or []
        else:
            return set()

        access = {"read", "write"} if list_type == "blacklist" else set()
        matched_access = AMemorixConfigUtils._find_configured_access(session_id, target_list, stream_type)
        if not matched_access:
            return access
        if list_type == "blacklist":
            return access - matched_access
        return access | matched_access

    @staticmethod
    def _get_stream_type(session_id: str) -> str:
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager

            stream = get_chat_manager().get_stream(session_id)
        except Exception:
            return ""
        if stream is None:
            return ""
        return "group" if getattr(stream, "group_info", None) else "private"

    @staticmethod
    def _find_configured_access(session_id: str, target_list: List[str], stream_type: str) -> set[str]:
        for item in target_list:
            resolved_session_id, access = AMemorixConfigUtils._parse_access_target(str(item or ""), stream_type)
            if resolved_session_id == session_id:
                return access
        return set()

    @staticmethod
    def _parse_access_target(target: str, stream_type: str) -> tuple[str, set[str]]:
        parts = [part.strip() for part in target.split(":")]
        if len(parts) != 3:
            return "", set()
        platform, target_id, access_token = parts
        if not platform or not target_id:
            return "", set()
        access = AMemorixConfigUtils._parse_access_token(access_token)
        if not access:
            return "", set()
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager

            session_id = get_chat_manager().get_stream_id(platform, target_id, is_group=stream_type == "group")
        except Exception:
            return "", set()
        return session_id, access

    @staticmethod
    def _parse_access_token(access_token: str) -> set[str]:
        token = str(access_token or "").strip().lower()
        if token == "read":
            return {"read"}
        if token == "write":
            return {"write"}
        if token == "full":
            return {"read", "write"}
        return set()


# ---------------------------------------------------------------------------
# _update_toml_doc (上游 src.webui.utils.toml_utils)
# ---------------------------------------------------------------------------
def _update_toml_doc(target: dict, source: dict) -> None:
    """递归合并 source 到 target（就地修改）。"""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _update_toml_doc(target[key], value)
        else:
            target[key] = value


# ---------------------------------------------------------------------------
# config_manager 垫片
# ---------------------------------------------------------------------------
class _ConfigManagerShim:
    """模拟上游 config_manager 的最小接口。"""

    _reload_callbacks: List[Any] = []

    def get_global_config(self):
        """返回一个具有 .a_memorix 属性的对象。"""
        return self

    def get_model_config(self):
        """返回具有 .models_dict 的模型配置对象。"""
        from src.config.config import model_config
        return model_config

    @property
    def a_memorix(self) -> AMemorixConfig:
        """从 TOML 配置读取 a_memorix 节，不存在则返回默认值。"""
        import tomlkit
        config_path = BOT_CONFIG_PATH
        if config_path.exists():
            try:
                with config_path.open("r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
                a_memorix_data = doc.get("a_memorix", {})
                if isinstance(a_memorix_data, dict) and a_memorix_data:
                    return self._dict_to_config(a_memorix_data)
            except Exception:
                pass
        return AMemorixConfig()

    @staticmethod
    def _dict_to_config(data: dict) -> AMemorixConfig:
        """从字典构造 AMemorixConfig。"""
        config = AMemorixConfig()
        if "plugin" in data and isinstance(data["plugin"], dict):
            config.plugin = _PluginConfig(**{k: v for k, v in data["plugin"].items() if k in _PluginConfig.__dataclass_fields__})
        if "embedding" in data and isinstance(data["embedding"], dict):
            config.embedding = _EmbeddingConfig(**{k: v for k, v in data["embedding"].items() if k in _EmbeddingConfig.__dataclass_fields__})
        if "storage" in data and isinstance(data["storage"], dict):
            config.storage = _StorageConfig(**{k: v for k, v in data["storage"].items() if k in _StorageConfig.__dataclass_fields__})
        if "search" in data and isinstance(data["search"], dict):
            config.search = _SearchConfig(**{k: v for k, v in data["search"].items() if k in _SearchConfig.__dataclass_fields__})
        if "v5_lifecycle" in data and isinstance(data["v5_lifecycle"], dict):
            config.v5_lifecycle = _V5LifecycleConfig(**{k: v for k, v in data["v5_lifecycle"].items() if k in _V5LifecycleConfig.__dataclass_fields__})
        if "summarization" in data and isinstance(data["summarization"], dict):
            config.summarization = _SummarizationConfig(**{k: v for k, v in data["summarization"].items() if k in _SummarizationConfig.__dataclass_fields__})
        if "episode" in data and isinstance(data["episode"], dict):
            config.episode = _EpisodeConfig(**{k: v for k, v in data["episode"].items() if k in _EpisodeConfig.__dataclass_fields__})
        if "shared_memory_groups" in data and isinstance(data["shared_memory_groups"], list):
            groups: List[_SharedMemoryGroupConfig] = []
            for item in data["shared_memory_groups"]:
                if isinstance(item, dict):
                    groups.append(
                        _SharedMemoryGroupConfig(
                            targets=list(item.get("targets", []) or []),
                        )
                    )
            config.shared_memory_groups = groups
        if "integration" in data and isinstance(data["integration"], dict):
            config.integration = _IntegrationConfig(
                **{k: v for k, v in data["integration"].items() if k in _IntegrationConfig.__dataclass_fields__}
            )
        if "cross_chat_memory" in data and isinstance(data["cross_chat_memory"], dict):
            config.cross_chat_memory = _CrossChatMemoryConfig(
                **{
                    k: v
                    for k, v in data["cross_chat_memory"].items()
                    if k in _CrossChatMemoryConfig.__dataclass_fields__
                }
            )
        return config

    def register_reload_callback(self, callback):
        self._reload_callbacks.append(callback)

    async def reload_config(self, changed_scopes=None):
        for cb in self._reload_callbacks:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(cb):
                    await cb(changed_scopes)
                else:
                    cb(changed_scopes)
            except Exception:
                pass


config_manager = _ConfigManagerShim()


# ---------------------------------------------------------------------------
# LLM Service 垫片 (src.services.llm_service)
# ---------------------------------------------------------------------------
@dataclass
class LLMServiceResult:
    """上游 LLMServiceResult 的最小垫片。"""
    content: str = ""
    reasoning: str = ""
    model_name: str = ""
    success: bool = True
    error: str = ""

    @classmethod
    def from_error(cls, message: str, detail: str = "") -> "LLMServiceResult":
        return cls(content="", success=False, error=f"{message}: {detail}")

    @classmethod
    def from_response_result(cls, result: Any) -> "LLMServiceResult":
        if isinstance(result, cls):
            return result
        return cls(content=str(result), success=True)


@dataclass
class LLMServiceRequest:
    task_name: str = ""
    request_type: str = ""
    prompt: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass
class LLMGenerationOptions:
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tool_options: Any = None
    response_format: Any = None
    interrupt_flag: Any = None


class LLMServiceClient:
    """上游 LLMServiceClient 的最小垫片，内部委托到 NachoBot 的 LLMRequest。"""

    def __init__(self, task_name: str = "", request_type: str = "", session_id: str = ""):
        from src.config.config import model_config
        self.task_name = task_name
        self.request_type = request_type

        # 查找对应的 TaskConfig
        task_config = getattr(model_config.model_task_config, task_name, None)
        if task_config is None:
            # 回退到 memory / utils / planner
            for fallback in ("memory", "utils", "planner"):
                task_config = getattr(model_config.model_task_config, fallback, None)
                if task_config is not None:
                    break
        if task_config is None:
            raise RuntimeError(f"找不到模型任务配置: {task_name}")

        from src.llm_models.utils_model import LLMRequest
        self._orchestrator = LLMRequest(model_set=task_config, request_type=request_type or task_name)

    async def generate_response(self, prompt: str, options: Optional[LLMGenerationOptions] = None) -> LLMServiceResult:
        temp = options.temperature if options else None
        max_tok = options.max_tokens if options else None
        content, (reasoning, model_name, _) = await self._orchestrator.generate_response_async(
            prompt=prompt, temperature=temp, max_tokens=max_tok
        )
        return LLMServiceResult(content=content, reasoning=reasoning, model_name=model_name, success=True)


async def generate(request: LLMServiceRequest) -> LLMServiceResult:
    """上游 llm_service.generate() 的垫片。"""
    client = LLMServiceClient(task_name=request.task_name, request_type=request.request_type)
    return await client.generate_response(
        prompt=request.prompt,
        options=LLMGenerationOptions(temperature=request.temperature, max_tokens=request.max_tokens),
    )


def get_available_models() -> Dict[str, Any]:
    """返回 model_task_config 中所有可用的模型任务。"""
    from src.config.config import model_config
    result = {}
    task_config = model_config.model_task_config
    for attr_name in dir(task_config):
        if attr_name.startswith("_"):
            continue
        attr_val = getattr(task_config, attr_name, None)
        if attr_val is not None and hasattr(attr_val, "model_list"):
            result[attr_name] = attr_val
    return result
