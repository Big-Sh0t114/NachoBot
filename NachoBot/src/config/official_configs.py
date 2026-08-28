import re

from dataclasses import dataclass, field
from typing import Literal, Optional

from src.config.config_base import ConfigBase

"""
须知：
1. 本文件中记录了所有的配置项
2. 所有新增的class都需要继承自ConfigBase
3. 所有新增的class都应在config.py中的Config类中添加字段
4. 对于新增的字段，若为可选项，则应在其后添加field()并设置default_factory或default
"""


@dataclass
class BotConfig(ConfigBase):
    """QQ机器人配置类"""

    platform: str
    """平台"""

    qq_account: str
    """QQ账号"""

    nickname: str
    """昵称"""

    owner_name: str = ""
    """主人名，可用于 {owner_name} 动态模板字段"""

    alias_names: list[str] = field(default_factory=lambda: [])
    """别名列表"""

    integrated_plan: bool = True
    """集成规划开关（设为false则回退至分离的planner/replyer模式）"""

    sandbox_whitelist: list[str] = field(default_factory=lambda: [])
    """沙盒白名单列表"""

    llm_block: bool = True
    """是否启用LLM自主屏蔽用户功能（群聊中屏蔽垃圾/骚扰信息发送者）"""

    bot_ban: bool = True
    """是否启用LLM自主禁言用户功能（群聊中禁言违规用户，通过平台API真正禁言）"""

    ban_rules: str = ""
    """禁言规则文本，由Replyer在决策时参考。留空则使用内置默认规则"""

    ban_whitelist: list[str] = field(default_factory=lambda: [])
    """禁言白名单，列表中的QQ号/用户ID不会被bot禁言"""


@dataclass
class PersonalityConfig(ConfigBase):
    """人格配置类"""

    personality: str
    """人格"""

    emotion_style: str
    """情感特征"""

    reply_style: str = ""
    """表达风格"""

    interest: str = ""
    """兴趣"""

    plan_style: str = ""
    """说话规则，行为风格"""

    private_plan_style: str = ""
    """私聊说话规则，行为风格"""

    gift_reaction_prompt: str = ""
    """礼物反应提示词"""


@dataclass
class RelationshipConfig(ConfigBase):
    """关系配置类"""

    enable_relationship: bool = True
    """是否启用关系系统"""


@dataclass
class ChatConfig(ConfigBase):
    """聊天配置类"""

    max_context_size: int = 18
    """上下文长度"""

    max_context_size_group: Optional[int] = None
    """群聊上下文长度，未设置时使用 max_context_size"""

    max_context_size_private: Optional[int] = None
    """私聊上下文长度，未设置时使用 max_context_size"""

    def get_max_context_size(self, is_group_chat: Optional[bool] = None) -> int:
        """
        获取当前聊天的上下文长度；若未设置专项值则回落到全局默认
        """
        if is_group_chat is True and self.max_context_size_group is not None:
            return self.max_context_size_group
        if is_group_chat is False and self.max_context_size_private is not None:
            return self.max_context_size_private
        return self.max_context_size

    interest_rate_mode: Literal["fast", "accurate"] = "fast"
    """兴趣值计算模式，fast为快速计算，accurate为精确计算"""

    planner_size: float = 1.5
    """副规划器大小，越小，动作执行能力越精细，但是消耗更多token，调大可以缓解429类错误"""

    mentioned_bot_reply: bool = True
    """是否启用提及必回复"""

    title_enabled_groups: list[str] = field(default_factory=lambda: [])
    """启用头衔设置功能的群号列表，仅列表中的群可使用 set_group_title 动作"""

    at_bot_inevitable_reply: float = 1
    """@bot 必然回复，1为100%回复，0为不额外增幅"""

    planner_interrupt_enabled: bool = True
    """Planner 打断总开关，关闭后新消息不会打断正在执行的 LLM 请求"""

    planner_interrupt_max_consecutive_count: int = 3
    """Planner 连续打断上限，超过此次数后即使有新消息也不再打断当前 LLM 请求"""

    person_profile_injection_enabled: bool = True
    """人物画像注入开关，开启后在 Planner 决策前自动注入对话参与者的画像信息"""

    person_profile_injection_max_profiles: int = 3
    """每轮 Planner 最多注入几个对话参与者的画像"""

    talk_value: float = 1
    """思考频率"""

    talk_value_list: list[list] = field(default_factory=list)
    """
    按聊天流配置思考频率
    格式: [["chat_stream_id", talk_value], ...]
    chat_stream_id 形如 "platform:id:type"，空字符串表示全局配置
    talk_value 为浮点数，表示该会话的思考频率
    """

    def _parse_stream_config_to_chat_id(self, stream_config_str: str) -> Optional[str]:
        """解析流配置字符串并生成对应的 chat_id"""
        try:
            parts = stream_config_str.split(":")
            if len(parts) != 3:
                return None

            platform = parts[0]
            id_str = parts[1]
            stream_type = parts[2]

            is_group = stream_type == "group"

            import hashlib

            components = [platform, str(id_str)] if is_group else [platform, str(id_str), "private"]
            key = "_".join(components)
            return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
        except (ValueError, IndexError):
            return None

    def get_talk_value_for_chat(self, chat_stream_id: Optional[str] = None) -> float:
        """
        根据聊天流ID获取思考频率；若未配置则返回全局默认值
        """
        if self.talk_value_list:
            if chat_stream_id:
                specific_talk_value = self._get_stream_specific_talk_value(chat_stream_id)
                if specific_talk_value is not None:
                    return specific_talk_value

            global_talk_value = self._get_global_talk_value()
            if global_talk_value is not None:
                return global_talk_value

        return self.talk_value

    def _get_stream_specific_talk_value(self, chat_stream_id: str) -> Optional[float]:
        """获取特定聊天流的思考频率"""
        for config_item in self.talk_value_list:
            if not config_item or len(config_item) < 2:
                continue

            stream_config_str = config_item[0]
            if stream_config_str == "":
                continue

            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None or config_chat_id != chat_stream_id:
                continue

            try:
                return float(config_item[1])
            except (ValueError, IndexError):
                continue

        return None

    def _get_global_talk_value(self) -> Optional[float]:
        """获取全局配置的思考频率"""
        for config_item in self.talk_value_list:
            if not config_item or len(config_item) < 2:
                continue

            if config_item[0] == "":
                try:
                    return float(config_item[1])
                except (ValueError, IndexError):
                    continue

        return None


@dataclass
class MessageReceiveConfig(ConfigBase):
    """消息接收配置类"""

    ban_words: set[str] = field(default_factory=lambda: set())
    """过滤词列表"""

    ban_msgs_regex: set[str] = field(default_factory=lambda: set())
    """过滤正则表达式列表"""


@dataclass
class MemoryConfig(ConfigBase):
    """记忆配置类"""

    max_agent_iterations: int = 5
    """Agent最多迭代轮数（最低为1）"""

    agent_timeout_seconds: float = 120.0
    """Agent超时时间（秒）"""

    global_memory: bool = False
    """是否允许记忆检索在聊天记录中进行全局查询（忽略当前chat_id，仅对 search_chat_history 等工具生效）"""

    global_memory_blacklist: list[str] = field(default_factory=lambda: [])
    """
    全局记忆黑名单，当启用全局记忆时，不将特定聊天流纳入检索
    格式: ["platform:id:type", ...]
    
    示例:
    [
        "qq:1919810:private",  # 排除特定私聊
        "qq:114514:group",     # 排除特定群聊
    ]
    
    说明:
    - 当启用全局记忆时，黑名单中的聊天流不会被检索
    - 当在黑名单中的聊天流进行查询时，仅使用该聊天流的本地记忆
    """

    planner_question: bool = True
    """
    是否使用 Planner 提供的 question 作为记忆检索问题
    - True: 当 Planner 在 reply 动作中提供了 question 时，直接使用该问题进行记忆检索，跳过 LLM 生成问题的步骤
    - False: 沿用旧模式，使用 LLM 生成问题
    """

    def __post_init__(self):
        """验证配置值"""
        if self.max_agent_iterations < 1:
            raise ValueError(f"max_agent_iterations 必须至少为1，当前值: {self.max_agent_iterations}")
        if self.agent_timeout_seconds <= 0:
            raise ValueError(f"agent_timeout_seconds 必须大于0，当前值: {self.agent_timeout_seconds}")


@dataclass
class ExpressionConfig(ConfigBase):
    """表达配置类"""

    learning_list: list[list] = field(default_factory=lambda: [])
    """
    表达学习配置列表，支持按聊天流配置
    格式: [["chat_stream_id", "use_expression", "enable_learning", learning_intensity], ...]
    
    示例:
    [
        ["", "enable", "enable", 1.0],  # 全局配置：使用表达，启用学习，学习强度1.0
        ["qq:1919810:private", "enable", "enable", 1.5],  # 特定私聊配置：使用表达，启用学习，学习强度1.5
        ["qq:114514:private", "enable", "disable", 0.5],  # 特定私聊配置：使用表达，禁用学习，学习强度0.5
    ]
    
    说明:
    - 第一位: chat_stream_id，空字符串表示全局配置
    - 第二位: 是否使用学到的表达 ("enable"/"disable")
    - 第三位: 是否学习表达 ("enable"/"disable") 
    - 第四位: 学习强度（浮点数），影响学习频率，最短学习时间间隔 = 300/学习强度（秒）
    """

    expression_groups: list[list[str]] = field(default_factory=list)
    """
    表达学习互通组
    格式: [["qq:12345:group", "qq:67890:private"]]
    """

    def _parse_stream_config_to_chat_id(self, stream_config_str: str) -> Optional[str]:
        """
        解析流配置字符串并生成对应的 chat_id

        Args:
            stream_config_str: 格式为 "platform:id:type" 的字符串

        Returns:
            str: 生成的 chat_id，如果解析失败则返回 None
        """
        try:
            parts = stream_config_str.split(":")
            if len(parts) != 3:
                return None

            platform = parts[0]
            id_str = parts[1]
            stream_type = parts[2]

            # 判断是否为群聊
            is_group = stream_type == "group"

            # 使用与 ChatStream.get_stream_id 相同的逻辑生成 chat_id
            import hashlib

            if is_group:
                components = [platform, str(id_str)]
            else:
                components = [platform, str(id_str), "private"]
            key = "_".join(components)
            return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()

        except (ValueError, IndexError):
            return None

    def get_expression_config_for_chat(self, chat_stream_id: Optional[str] = None) -> tuple[bool, bool, int]:
        """
        根据聊天流ID获取表达配置

        Args:
            chat_stream_id: 聊天流ID，格式为哈希值

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)
        """
        if not self.learning_list:
            # 如果没有配置，使用默认值：启用表达，启用学习，300秒间隔
            return True, True, 300

        # 优先检查聊天流特定的配置
        if chat_stream_id:
            specific_expression_config = self._get_stream_specific_config(chat_stream_id)
            if specific_expression_config is not None:
                return specific_expression_config

        # 检查全局配置（第一个元素为空字符串的配置）
        global_expression_config = self._get_global_config()
        if global_expression_config is not None:
            return global_expression_config

        # 如果都没有匹配，返回默认值
        return True, True, 300

    def _get_stream_specific_config(self, chat_stream_id: str) -> Optional[tuple[bool, bool, int]]:
        """
        获取特定聊天流的表达配置

        Args:
            chat_stream_id: 聊天流ID（哈希值）

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)，如果没有配置则返回 None
        """
        for config_item in self.learning_list:
            if not config_item or len(config_item) < 4:
                continue

            stream_config_str = config_item[0]  # 例如 "qq:1026294844:group"

            # 如果是空字符串，跳过（这是全局配置）
            if stream_config_str == "":
                continue

            # 解析配置字符串并生成对应的 chat_id
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None:
                continue

            # 比较生成的 chat_id
            if config_chat_id != chat_stream_id:
                continue

            # 解析配置
            try:
                use_expression: bool = config_item[1].lower() == "enable"
                enable_learning: bool = config_item[2].lower() == "enable"
                learning_intensity: float = float(config_item[3])
                return use_expression, enable_learning, learning_intensity  # type: ignore
            except (ValueError, IndexError):
                continue

        return None

    def _get_global_config(self) -> Optional[tuple[bool, bool, int]]:
        """
        获取全局表达配置

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)，如果没有配置则返回 None
        """
        for config_item in self.learning_list:
            if not config_item or len(config_item) < 4:
                continue

            # 检查是否为全局配置（第一个元素为空字符串）
            if config_item[0] == "":
                try:
                    use_expression: bool = config_item[1].lower() == "enable"
                    enable_learning: bool = config_item[2].lower() == "enable"
                    learning_intensity = float(config_item[3])
                    return use_expression, enable_learning, learning_intensity  # type: ignore
                except (ValueError, IndexError):
                    continue

        return None


@dataclass
class ToolConfig(ConfigBase):
    """工具配置类"""

    enable_tool: bool = False
    """是否在聊天中启用工具"""

    web_search_engines: list[str] = field(default_factory=lambda: ["bing", "duckduckgo"])
    """浏览器联网搜索引擎，按顺序失败回退"""

    web_search_timeout_seconds: int = 20
    """单个搜索引擎页面的加载超时时间（秒）"""



@dataclass
class MCPSettings(ConfigBase):
    """核心 MCP 运行时配置"""

    enabled: bool = True
    """是否启用核心 MCP 运行时"""

    auto_detect: bool = True
    """是否通过能力路由器自动触发 MCP 独立工具链"""

    servers_json: str = ""
    """Claude Desktop 风格的 mcpServers JSON"""

    tool_prefix: str = "mcp"
    """暴露给模型的 MCP 工具名前缀"""

    disabled_tools: list[str] = field(default_factory=list)
    """核心 MCP 运行时禁用的工具名"""

    connect_timeout_seconds: int = 20
    """单个 MCP 服务器连接和工具发现超时"""

    call_timeout_seconds: int = 60
    """单次 MCP 工具调用超时"""

    reconnect_interval_seconds: int = 30
    """断开服务器的后台重连间隔；设为 0 禁用"""

    permissions_enabled: bool = True
    """是否启用核心 MCP 权限策略"""

    permission_default_mode: str = "deny_all"
    """未匹配权限规则时的策略：allow_all 或 deny_all"""

    quick_allow_users: list[str] = field(default_factory=list)
    """始终允许使用 MCP 的用户 ID"""

    quick_deny_groups: list[str] = field(default_factory=list)
    """始终禁止使用 MCP 的群 ID"""

    permission_rules_json: str = "[]"
    """按工具名和会话 ID 匹配的高级权限规则 JSON"""

    max_rounds: int = 3
    """单次 MCP 独立工具链的最大决策轮数"""

    max_calls: int = 5
    """单次 MCP 独立工具链允许的最大工具调用数"""

    max_candidate_tools: int = 32
    """单次 MCP 决策最多暴露给模型的候选工具数"""

    observation_max_chars: int = 12000
    """回注 MCP 工具观察结果的最大字符数"""


@dataclass
class MCPConfig(ConfigBase):
    """独立 mcp_config.toml 配置文件"""

    mcp: MCPSettings = field(default_factory=MCPSettings)


@dataclass
class VoiceConfig(ConfigBase):
    """语音识别配置类"""

    enable_asr: bool = False
    """是否启用语音识别"""


@dataclass
class EmojiConfig(ConfigBase):
    """表情包配置类"""

    emoji_chance: float = 0.6
    """发送表情包的基础概率"""

    max_reg_num: int = 200
    """表情包最大注册数量"""

    do_replace: bool = True
    """达到最大注册数量时替换旧表情包"""

    check_interval: int = 120
    """表情包检查间隔（分钟）"""

    steal_emoji: bool = True
    """是否偷取表情包，可以发送保存的表情包"""

    content_filtration: bool = False
    """是否开启表情包过滤"""

    filtration_prompt: str = "符合公序良俗"
    """表情包过滤要求"""


@dataclass
class MoodConfig(ConfigBase):
    """情绪配置类"""

    enable_mood: bool = False
    """是否启用情绪系统"""

    mood_update_threshold: float = 1.0
    """情绪更新阈值,越高，更新越慢"""


@dataclass
class KeywordRuleConfig(ConfigBase):
    """关键词规则配置类"""

    keywords: list[str] = field(default_factory=lambda: [])
    """关键词列表"""

    regex: list[str] = field(default_factory=lambda: [])
    """正则表达式列表"""

    reaction: str = ""
    """关键词触发的反应"""

    def __post_init__(self):
        """验证配置"""
        if not self.keywords and not self.regex:
            raise ValueError("关键词规则必须至少包含keywords或regex中的一个")

        if not self.reaction:
            raise ValueError("关键词规则必须包含reaction")

        # 验证正则表达式
        for pattern in self.regex:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"无效的正则表达式 '{pattern}': {str(e)}") from e


@dataclass
class KeywordReactionConfig(ConfigBase):
    """关键词配置类"""

    keyword_rules: list[KeywordRuleConfig] = field(default_factory=lambda: [])
    """关键词规则列表"""

    regex_rules: list[KeywordRuleConfig] = field(default_factory=lambda: [])
    """正则表达式规则列表"""

    def __post_init__(self):
        """验证配置"""
        # 验证所有规则
        for rule in self.keyword_rules + self.regex_rules:
            if not isinstance(rule, KeywordRuleConfig):
                raise ValueError(f"规则必须是KeywordRuleConfig类型，而不是{type(rule).__name__}")


@dataclass
class InjectionPayloadConfig(ConfigBase):
    """单个注入条目的载荷"""

    system: str = ""
    """前置约束/系统提示"""

    few_shots: str = ""
    """示例或少样本"""

    note: str = ""
    """补充背景或知识"""


@dataclass
class InjectionTopicConfig(ConfigBase):
    """注入主题配置"""

    id: str
    """唯一标识"""

    title: str = ""
    """展示名称"""

    keywords: list[str] = field(default_factory=list)
    """触发关键词列表"""

    regex: list[str] = field(default_factory=list)
    """触发正则列表"""

    priority: int = 0
    """优先级，越大越靠前"""

    cooldown_turns: int = 0
    """冷却轮数，避免重复触发"""

    payload: InjectionPayloadConfig = field(default_factory=InjectionPayloadConfig)
    """注入内容"""


@dataclass
class InjectionConfig(ConfigBase):
    """注入系统配置"""

    enable: bool = True
    """总开关"""

    persistent_rounds: int = 10
    """单次触发后持续注入的轮次"""

    topics: list[InjectionTopicConfig] = field(default_factory=list)
    """可配置的注入主题列表"""

    def __post_init__(self):
        if self.persistent_rounds < 0:
            self.persistent_rounds = 0
        for topic in self.topics:
            if topic.cooldown_turns < 0:
                topic.cooldown_turns = 0


@dataclass
class PromiseCacheConfig(ConfigBase):
    """约定/誓言缓存配置（独立于普通关键词系统）"""

    enable: bool = False
    """是否启用约定缓存"""

    keywords: list[str] = field(default_factory=lambda: ["约定", "誓言", "说好了"])
    """触发缓存的关键词列表"""

    context_size: int = 10
    """触发时向前回溯的消息条数"""

    post_context_size: int = 10
    """触发后继续追加的消息条数"""

    cache_dir: str = "data/promise_cache"
    """缓存存放目录（相对项目根路径）"""

    max_cache_per_keyword: int = 5
    """每个关键词保留的缓存文件数量"""

    case_sensitive: bool = False
    """是否区分大小写"""

    def __post_init__(self):
        if self.context_size < 0 or self.post_context_size < 0:
            raise ValueError("context_size 和 post_context_size 必须大于等于0")
        if self.max_cache_per_keyword < 1:
            raise ValueError("max_cache_per_keyword 必须至少为1")


@dataclass
class ResponsePostProcessConfig(ConfigBase):
    """回复后处理配置类"""

    enable_response_post_process: bool = True
    """是否启用回复后处理，包括错别字生成器，回复分割器"""


@dataclass
class ResponseFilterConfig(ConfigBase):
    """回复过滤配置类"""

    enable: bool = True
    """是否启用回复过滤"""

    blocked_markers: list[str] = field(
        default_factory=lambda: [
            "i'm kiro",
            "i am kiro",
            "kiro-cli chat",
            "kiro-cli",
            "kiro cli",
            "ai assistant built by aws",
            "aws services",
            "i can't engage with this request",
            "i need to decline this request",
            "this message is attempting to manipulate",
            "adopting a fake persona",
            "creating a fake persona",
            "roleplaying as a character",
            "roleplay as a different character",
            "i don't roleplay as other characters",
            "i don't pretend to be someone else",
            "fabricated instructions",
            "fabricated rules",
            "ignoring my actual instructions",
            "instructions to ignore my real system prompts",
            "override my actual identity",
            "actual identity and guidelines",
            "responding as if i'm in a qq",
            "qq chat group",
            "identity verification",
            "false claims about",
            "fabricated identity",
            "fake conversation history",
        ]
    )
    """可疑模板关键词（命中后会触发过滤）"""


@dataclass
class ChineseTypoConfig(ConfigBase):
    """中文错别字配置类"""

    enable: bool = True
    """是否启用中文错别字生成器"""

    error_rate: float = 0.01
    """单字替换概率"""

    min_freq: int = 9
    """最小字频阈值"""

    tone_error_rate: float = 0.1
    """声调错误概率"""

    word_replace_rate: float = 0.006
    """整词替换概率"""


@dataclass
class ResponseSplitterConfig(ConfigBase):
    """回复分割器配置类"""

    enable: bool = True
    """是否启用回复分割器"""

    max_length: int = 256
    """回复允许的最大长度"""

    max_sentence_num: int = 3
    """回复允许的最大句子数"""

    max_sentence_num_cap: int = 8
    """回复允许的最大句子数硬上限"""

    enable_kaomoji_protection: bool = False
    """是否启用颜文字保护"""


@dataclass
class TelemetryConfig(ConfigBase):
    """遥测配置类"""

    enable: bool = True
    """是否启用遥测"""


@dataclass
class DebugConfig(ConfigBase):
    """调试配置类"""

    show_prompt: bool = False
    """是否显示prompt"""


@dataclass
class ExperimentalConfig(ConfigBase):
    """实验功能配置类"""

    enable_friend_chat: bool = False
    """是否启用好友聊天"""


@dataclass
class FocusMemberConfig(ConfigBase):
    """Focus 组中的一个显式会话成员。"""

    key: str
    """组内稳定别名，供 initial_member 引用；不会暴露给模型作为实际目标ID。"""

    platform: str
    """适配器平台名，例如 qq。"""

    kind: Literal["group", "private"]
    """会话类型。v1 支持 group->group 和 group->private。"""

    external_id: str
    """平台侧群号或用户号，启动时解析成 ChatStream.stream_id。"""

    display_name: str = ""
    """Planner 中展示的安全名称；为空时由集成层生成。"""

    allow_import: bool = True
    """是否允许该会话接收短期交接内容。"""

    allow_export: bool = True
    """是否允许该会话导出短期交接内容。私聊源在 v1 中仍会被策略拒绝。"""

    planner_bypass: bool = False
    """该会话是否由适配器声明为直答会话；用于启动时尚无消息上下文的 Focus 排序。"""

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.platform.strip() or not self.external_id.strip():
            raise ValueError("Focus member key/platform/external_id 均不能为空")


@dataclass
class FocusGroupConfig(ConfigBase):
    """共享一个 active-chat lease 的 Focus 会话组。"""

    id: str
    members: list[FocusMemberConfig]
    initial_member: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Focus group id 不能为空")
        if len(self.members) < 2:
            raise ValueError(f"Focus group {self.id!r} 至少需要两个成员")
        keys = [member.key for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Focus group {self.id!r} 中存在重复 member key")
        if self.initial_member and self.initial_member not in keys:
            raise ValueError(f"Focus group {self.id!r} 的 initial_member 不在 members 中")


@dataclass
class FocusConfig(ConfigBase):
    """短期跨会话 Focus 编排配置。"""

    mode: Literal["off", "observe", "active"] = "off"
    """off 保持现有逻辑；observe 当前为安全 no-op；active 允许终止式切换和交接。"""

    allow_group_to_private: bool = True
    """允许显式 Focus 组内的群聊到私聊路径；私聊仅可无 handoff 安全返回群聊。"""

    unread_event_threshold: int = 5
    unviewed_event_seconds: int = 180
    max_events_per_prompt: int = 5
    max_unread_messages: int = 20
    switch_cooldown_seconds: int = 2
    handoff_ttl_seconds: int = 600
    handoff_successful_cycles: int = 3
    handoff_prompt_tokens: int = 512
    reservation_ttl_seconds: int = 120
    bypass_gate_enabled: bool = True
    """纯 Focus event turn 及 Planner bypass 会话使用仅允许 stay/switch 的轻量 Gate。"""
    bypass_gate_timeout_seconds: float = 8.0
    """轻量 Gate 的单次 LLM 超时。"""
    bypass_gate_max_tokens: int = 160
    """轻量 Gate 的最大输出 token。"""
    bypass_gate_retry_seconds: float = 3.0
    """轻量 Gate 在同一 turn 内重试前的等待时间。"""
    bypass_gate_max_attempts: int = 2
    """轻量 Gate 单轮最大尝试次数；耗尽后执行确定性安全降级。"""
    groups: list[FocusGroupConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        positive_fields = {
            "unread_event_threshold": self.unread_event_threshold,
            "unviewed_event_seconds": self.unviewed_event_seconds,
            "max_events_per_prompt": self.max_events_per_prompt,
            "max_unread_messages": self.max_unread_messages,
            "handoff_ttl_seconds": self.handoff_ttl_seconds,
            "handoff_successful_cycles": self.handoff_successful_cycles,
            "reservation_ttl_seconds": self.reservation_ttl_seconds,
            "bypass_gate_max_attempts": self.bypass_gate_max_attempts,
        }
        positive_float_fields = {
            "bypass_gate_timeout_seconds": self.bypass_gate_timeout_seconds,
            "bypass_gate_retry_seconds": self.bypass_gate_retry_seconds,
        }
        invalid = [name for name, value in positive_fields.items() if value <= 0]
        invalid.extend(name for name, value in positive_float_fields.items() if value <= 0)
        if invalid:
            raise ValueError(f"Focus 配置必须为正数: {', '.join(invalid)}")
        if self.switch_cooldown_seconds < 0:
            raise ValueError("Focus switch_cooldown_seconds 不能为负数")
        if not 128 <= self.handoff_prompt_tokens <= 768:
            raise ValueError("Focus handoff_prompt_tokens 必须在 128..768 范围内")

        if not 64 <= self.bypass_gate_max_tokens <= 512:
            raise ValueError("Focus bypass_gate_max_tokens 必须在 64..512 范围内")
        if self.bypass_gate_max_attempts > 5:
            raise ValueError("Focus bypass_gate_max_attempts 必须在 1..5 范围内")
        group_ids = [group.id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("Focus group id 不能重复")
        identities: dict[tuple[str, str, str], str] = {}
        for group in self.groups:
            if not any(member.kind == "group" for member in group.members):
                raise ValueError(f"Focus group {group.id!r} 至少需要一个群聊成员")
            for member in group.members:
                identity = (member.platform, member.kind, member.external_id)
                owner = identities.get(identity)
                if owner is not None:
                    raise ValueError(f"Focus 成员 {identity!r} 同时出现在 {owner!r} 和 {group.id!r}")
                identities[identity] = group.id
                if member.kind == "private" and not self.allow_group_to_private:
                    raise ValueError(f"Focus group {group.id!r} 包含私聊成员，但 allow_group_to_private=false")


@dataclass
class NcnkMessageConfig(ConfigBase):
    """ncnk_message配置类"""

    use_custom: bool = False
    """是否使用自定义的ncnk_message配置"""

    host: str = "127.0.0.1"
    """主机地址"""

    port: int = 8090
    """"端口号"""

    mode: Literal["ws", "tcp"] = "ws"
    """连接模式，支持ws和tcp"""

    use_wss: bool = False
    """是否使用WSS安全连接"""

    cert_file: str = ""
    """SSL证书文件路径，仅在use_wss=True时有效"""

    key_file: str = ""
    """SSL密钥文件路径，仅在use_wss=True时有效"""

    auth_token: list[str] = field(default_factory=lambda: [])
    """认证令牌，用于API验证，为空则不启用验证"""


@dataclass
class LPMMKnowledgeConfig(ConfigBase):
    """LPMM知识库配置类"""

    enable: bool = True
    """是否启用LPMM知识库"""

    rag_synonym_search_top_k: int = 10
    """RAG同义词搜索的Top K数量"""

    rag_synonym_threshold: float = 0.8
    """RAG同义词搜索的相似度阈值"""

    info_extraction_workers: int = 3
    """信息提取工作线程数"""

    qa_relation_search_top_k: int = 10
    """QA关系搜索的Top K数量"""

    qa_relation_threshold: float = 0.75
    """QA关系搜索的相似度阈值"""

    qa_paragraph_search_top_k: int = 1000
    """QA段落搜索的Top K数量"""

    qa_paragraph_node_weight: float = 0.05
    """QA段落节点权重"""

    qa_ent_filter_top_k: int = 10
    """QA实体过滤的Top K数量"""

    qa_ppr_damping: float = 0.8
    """QA PageRank阻尼系数"""

    qa_res_top_k: int = 10
    """QA最终结果的Top K数量"""

    embedding_dimension: int = 1024
    """嵌入向量维度，应该与模型的输出维度一致"""


@dataclass
class AdvancedConfig(ConfigBase):
    """高级模式配置"""

    whitelist: list[str] = field(default_factory=list)
    """允许开启高级模式的用户ID（QQ号）"""

    admins: list[str] = field(default_factory=list)
    """高级模式管理员ID（QQ号），可查看/管理高级用户"""

    default_enabled: bool = False
    """默认是否开启高级模式（仅白名单用户生效）"""

    block_tools_when_on: bool = True
    """高级模式是否禁用工具/联网"""

    block_tts_when_on: bool = True
    """高级模式是否禁用TTS输出"""

    prompt: str = ""
    """高级模式额外提示词（破甲提示词注入用）"""

    use_advanced_replyer: bool = True
    """高级模式是否启用独立的回复器模型组"""

    idle_timeout_minutes: int = 20
    """高级模式闲置超时时长（分钟），超过则自动关闭"""

    idle_check_interval_seconds: int = 60
    """闲置检测频率（秒）"""

    idle_notice: str = "20分钟未收到你的新消息，高级模式已自动关闭哦~"
    """闲置超时关闭时的提示文案"""
