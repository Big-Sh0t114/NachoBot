"""
中期记忆配置模块
定义中期记忆系统的所有配置参数
支持从 bot_config.toml 的 [mid_term_memory] 段加载配置
"""

from dataclasses import dataclass, field
from typing import Optional

from src.common.logger import get_logger

logger = get_logger("mid_term_memory_config")


@dataclass
class MidTermMemoryConfig:
    """中期记忆配置"""

    # 功能开关
    enabled: bool = True

    # 最大保留的摘要数量
    max_summaries: int = 100

    # 摘要过期时间（秒）：内容结束时间距今超过此值的摘要不再召回，并会被清理
    # 设为 0 或负数表示不启用时间过期（仅靠数量上限淘汰）
    summary_ttl_seconds: float = 7 * 24 * 3600.0  # 7 天

    # ===== 召回相关配置（分级相关度机制）=====
    # 高相关度阈值：超过此值的摘要完整注入（含摘要全文）
    recall_threshold_high: float = 0.65
    # 低相关度阈值：介于 low~high 之间的摘要仅注入 recall_cues 关键词提示
    recall_threshold_low: float = 0.50
    # 兼容旧配置（如果外部代码引用了 recall_threshold，映射到 high）
    recall_threshold: float = 0.65

    # 单次摘要输入的最大字符数
    max_summary_input_chars: int = 16000

    # 单次摘要的最大消息数（避免超出上下文）
    max_messages_per_summary: int = 100

    # 生成摘要所需的最少消息数（避免对少量消息摘要）
    # 降低门槛以覆盖低频群聊场景
    min_messages_for_summary: int = 10

    # 摘要生成的时间间隔检查（秒）
    # 只有当前窗口滑过的消息时间跨度超过此值时才生成摘要
    min_time_gap_for_summary: float = 300.0  # 5分钟

    # 两次实际生成摘要之间的最小墙钟间隔（秒）
    # 降低冷却时间以提高摘要生成频率
    min_summary_interval_seconds: float = 900.0  # 15 分钟

    # 重启后批量追赶：一次性可连续处理的批次数上限
    # 避免重启后大量积压消息只能一批一批等冷却
    max_catchup_batches: int = 5

    # 存储路径（相对于项目根目录）
    storage_dir: str = "temp/mid_term_memory"

    # LLM 配置
    llm_model: str = "default"  # 使用系统默认模型
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2000
    # 摘要生成失败时的最大重试次数
    llm_max_retries: int = 2
    # 摘要质量校验：最小摘要长度（字符）
    min_summary_length: int = 30
    # 摘要质量校验：最少 recall_cues 数量
    min_recall_cues: int = 2

    # Phase 2: Embedding 向量化召回配置
    enable_vector_recall: bool = True  # 是否启用向量召回
    max_recalled_summaries: int = 5  # 最多召回的摘要数量（提高至5）
    fallback_to_full_injection: bool = False  # 降级时不再全量注入
    # 降级时按时间倒序取最近 N 条摘要
    fallback_recent_count: int = 3
    # 中期记忆注入的最大 token 预算（字符数近似）
    max_injection_chars: int = 3000

    # 时间衰减：摘要越新，相关度得分越高
    # 衰减公式：score * (decay_base ^ (-age_hours / decay_half_life_hours))
    enable_time_decay: bool = True
    time_decay_half_life_hours: float = 48.0  # 48小时后相关度减半

    # Query 构建：从最近消息中提取查询的消息数
    query_message_count: int = 8
    # 是否启用 LLM 辅助提取查询关键词（提升召回精度，但增加 ~1s 延迟）
    # 默认关闭以避免阻塞 planner 主流程，可在 bot_config.toml 中开启
    enable_llm_query_extraction: bool = False

    # Manager 缓存 LRU 上限
    manager_cache_max_size: int = 50


# 全局配置实例（启动时从 TOML 加载一次）
def _load_config_from_toml() -> MidTermMemoryConfig:
    """
    从 bot_config.toml 的 [mid_term_memory] 段加载配置
    如果配置段不存在或加载失败，返回默认配置
    """
    config = MidTermMemoryConfig()

    try:
        import os
        import tomlkit

        # 查找配置文件路径
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config",
        )
        config_path = os.path.join(config_dir, "bot_config.toml")

        if not os.path.exists(config_path):
            return config

        with open(config_path, "r", encoding="utf-8") as f:
            toml_data = tomlkit.load(f)

        mtm_section = toml_data.get("mid_term_memory")
        if not mtm_section or not isinstance(mtm_section, dict):
            return config

        # 映射 TOML 字段到 config 属性
        if "enabled" in mtm_section:
            config.enabled = bool(mtm_section["enabled"])
        if "max_summaries" in mtm_section:
            config.max_summaries = int(mtm_section["max_summaries"])
        if "summary_ttl_days" in mtm_section:
            config.summary_ttl_seconds = float(mtm_section["summary_ttl_days"]) * 24 * 3600.0
        if "recall_threshold_high" in mtm_section:
            config.recall_threshold_high = float(mtm_section["recall_threshold_high"])
            config.recall_threshold = config.recall_threshold_high
        if "recall_threshold_low" in mtm_section:
            config.recall_threshold_low = float(mtm_section["recall_threshold_low"])
        if "max_recalled_summaries" in mtm_section:
            config.max_recalled_summaries = int(mtm_section["max_recalled_summaries"])
        if "min_messages_for_summary" in mtm_section:
            config.min_messages_for_summary = int(mtm_section["min_messages_for_summary"])
        if "min_summary_interval_minutes" in mtm_section:
            config.min_summary_interval_seconds = float(mtm_section["min_summary_interval_minutes"]) * 60.0
        if "max_catchup_batches" in mtm_section:
            config.max_catchup_batches = int(mtm_section["max_catchup_batches"])
        if "max_injection_chars" in mtm_section:
            config.max_injection_chars = int(mtm_section["max_injection_chars"])
        if "enable_time_decay" in mtm_section:
            config.enable_time_decay = bool(mtm_section["enable_time_decay"])
        if "time_decay_half_life_hours" in mtm_section:
            config.time_decay_half_life_hours = float(mtm_section["time_decay_half_life_hours"])
        if "enable_llm_query_extraction" in mtm_section:
            config.enable_llm_query_extraction = bool(mtm_section["enable_llm_query_extraction"])
        if "query_message_count" in mtm_section:
            config.query_message_count = int(mtm_section["query_message_count"])
        if "fallback_recent_count" in mtm_section:
            config.fallback_recent_count = int(mtm_section["fallback_recent_count"])
        if "manager_cache_max_size" in mtm_section:
            config.manager_cache_max_size = int(mtm_section["manager_cache_max_size"])
        if "llm_max_retries" in mtm_section:
            config.llm_max_retries = int(mtm_section["llm_max_retries"])
        if "min_summary_length" in mtm_section:
            config.min_summary_length = int(mtm_section["min_summary_length"])
        if "min_recall_cues" in mtm_section:
            config.min_recall_cues = int(mtm_section["min_recall_cues"])

        logger.info("从 bot_config.toml [mid_term_memory] 加载中期记忆配置成功")

    except Exception as e:
        logger.warning(f"从 TOML 加载中期记忆配置失败，使用默认值: {e}")

    return config


default_config = _load_config_from_toml()


def get_mid_term_memory_config() -> MidTermMemoryConfig:
    """获取中期记忆配置"""
    return default_config
