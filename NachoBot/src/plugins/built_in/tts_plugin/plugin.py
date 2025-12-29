from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.component_types import ComponentInfo
from src.common.logger import get_logger
import json
import random
from src.plugin_system.apis import database_api, generator_api, send_api, message_api
from src.config.config import model_config
from src.llm_models.utils_model import LLMRequest
import time
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.config_types import ConfigField
from typing import Tuple, List, Type, Dict, Optional
from src.chat.advanced.advanced_manager import advanced_manager

logger = get_logger("tts")


class TTSAction(BaseAction):
    """TTS语音转换动作处理类"""

    _language_preferences: Dict[str, str] = {}
    _last_tts_time: Dict[str, float] = {}  # chat_id -> timestamp
    MIN_TTS_INTERVAL = 60  # 秒级间隔，避免短时间内连续TTS
    DEFAULT_LANGUAGE = "ja"
    LANGUAGE_INFO: Dict[str, Dict[str, str]] = {
        "zh": {"display": "中文", "confirmation": "让我们说中文喵！"},
        "ja": {"display": "日文", "confirmation": "日本語を話しましょうにゃ！"},
    }
    FALLBACK_ZH: List[str] = [
        "好的，我马上发语音喵！",
        "收到，这就语音跟你说～",
        "好呀，我用语音告诉你。",
        "明白，稍后用语音回复你。",
        "嘿嘿，我录一段语音给你~",
        "收到啦，语音这就来。",
        "了解，我用语音说得更清楚喵。",
        "行的，我用语音回你。",
        "收到，我发个语音过去。",
        "好嘞，语音版马上奉上！",
    ]
    FALLBACK_JA: List[str] = [
        "了解だよ、すぐに送るね～。",
        "わかった、今すぐボイスで返すね。",
        "うん、音声で伝えるよ。",
        "了解、後でボイス送るね。",
        "任せて、声で話すね！",
        "いいよ、音声でお返しするね。",
        "オーケー、ボイスメッセージ届けるね。",
        "うん、音声の方が伝わりやすいしね。",
        "了解、少し待ってて、ボイス録るね。",
        "わかったよ、にゃんこボイスで答えるね。",
    ]

    # 激活设置
    activation_type = ActionActivationType.LLM_JUDGE
    parallel_action = False

    # 动作基本信息
    action_name = "tts_action"
    action_description = "将文本转换为语音进行播放，适用于需要语音输出的场景"

    # 动作参数定义
    action_parameters = {
        "voice_text": "需要朗读的内容，严格按当前会话设定的TTS语种来生成文本，默认使用日语",
    }

    # 动作使用场景
    action_require = [
        "当需要发送语音信息时使用",
        "当表达内容更适合用语音而不是文字传达时使用",
        "当用户想听到语音回答而非阅读文本时使用",
        "当用户让你说点什么时使用",
        "当用户对你表达了亲密时使用",
    ]

    # 关联类型
    associated_types = ["tts_text"]

    async def execute(self) -> Tuple[bool, str]:
        """处理TTS文本转语音动作"""
        logger.info(f"{self.log_prefix} 执行TTS动作: {self.reasoning}")

        text = self.action_data.get("voice_text")

        # 高级模式下直接禁用TTS，回退为文本发送
        if advanced_manager.should_block_tts(self.chat_stream):
            fallback_text = text or self.reasoning or "高级模式禁用TTS，已改为文本回复。"
            try:
                stream_id = self.chat_stream.stream_id if self.chat_stream else ""
                if stream_id:
                    await send_api.text_to_stream(fallback_text, stream_id)
                    logger.info(f"{self.log_prefix} 高级模式禁用TTS，已改为文本发送")
                    return True, "高级模式禁用TTS，已文本发送"
            except Exception as e:
                logger.warning(f"{self.log_prefix} 高级模式TTS回退文本发送失败: {e}")
            return False, "高级模式禁用TTS"

        if not text:
            logger.error(f"{self.log_prefix} 执行TTS动作时未提供文本内容")
            await self._ensure_language_loaded()
            current_language = self._get_current_language()
            # 尝试用回复器模型自动生成朗读文本
            text = await self._generate_voice_text_with_replyer(current_language)
            if text:
                logger.info(f"{self.log_prefix} 自动生成了TTS朗读文本")
            else:
                text = self._fallback_text(current_language)
                logger.warning(f"{self.log_prefix} 无法生成朗读文本，使用默认语种短句")
        else:
            await self._ensure_language_loaded()
            current_language = self._get_current_language()

        stripped_text = text.strip()
        if self._is_lang_switch_command(stripped_text):
            return await self._handle_language_switch()

        # 确保文本适合TTS使用
        processed_text = self._process_text_for_tts(text, current_language)
        processed_text = await self._ensure_language_consistency(processed_text, current_language)
        record_data = {"tts_language": current_language}

        try:
            # 发送TTS消息，携带语种元数据
            await self.send_custom(message_type="tts_text", content={"text": processed_text, "lang": current_language})

            # 记录动作信息
            await database_api.store_action_info(
                chat_stream=self.chat_stream,
                action_build_into_prompt=False,
                action_prompt_display="",
                action_done=True,
                thinking_id=self.thinking_id,
                action_data=record_data,
                action_name=self.action_name,
            )

            logger.info(f"{self.log_prefix} TTS动作执行成功，文本长度: {len(processed_text)}")
            return True, "TTS动作执行成功"

        except Exception as e:
            logger.error(f"{self.log_prefix} 执行TTS动作时出错: {e}")
            return False, f"执行TTS动作时出错: {e}"

    def _process_text_for_tts(self, text: str, language: str) -> str:
        """
        处理文本使其更适合TTS使用
        - 移除不必要的特殊字符和表情符号
        - 修正标点符号以提高语音质量
        - 优化文本结构使语音更流畅
        """
        # 这里可以添加文本处理逻辑
        # 例如：移除多余的标点、表情符号，优化语句结构等

        # 简单示例实现
        processed_text = text.strip()

        # 移除多余的标点符号
        import re

        # 移除emoji/非常用符号，保留中日文、数字、常见标点
        processed_text = re.sub(r"[^\w\s\u3040-\u30ff\u4e00-\u9fff\u3000-\u303f。，！？、；：,.!?~\-（）()…]", "", processed_text)

        processed_text = re.sub(r"([!?,.;:。！？，、；：])\1+", r"\1", processed_text)

        # 确保句子结尾有合适的标点
        preferred_period = "。"
        if language == "ja":
            preferred_period = "。"
        elif language == "zh":
            preferred_period = "。"

        if not any(processed_text.endswith(end) for end in [".", "?", "!", "。", "！", "？"]):
            processed_text = f"{processed_text}{preferred_period}"

        # 防止清洗后为空
        if not processed_text.strip():
            processed_text = self._fallback_text(language)

        return processed_text

    async def _ensure_language_consistency(self, text: str, language: str) -> str:
        """
        确保输出文本与目标语种一致：
        - 检测不匹配时，优先调用回复器改写到目标语种
        - 改写失败再回退到短句
        """
        if not text:
            return text

        if not self._needs_language_rewrite(text, language):
            return text

        rewritten = await self._rewrite_text_with_replyer(text, language)
        if rewritten:
            processed = self._process_text_for_tts(rewritten, language)
            if processed.strip():
                return processed

        return self._fallback_text(language)

    def _needs_language_rewrite(self, text: str, language: str) -> bool:
        import re

        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
        has_kana = bool(re.search(r"[ぁ-ゟ゠-ヿ]", text))

        if language == "ja":
            return has_cjk and not has_kana
        if language == "zh":
            return has_kana and not has_cjk
        return False

    async def _generate_voice_text_with_replyer(self, language: str) -> Optional[str]:
        """
        使用回复器模型生成语音朗读文本，适用于planner未提供voice_text的情况。
        """
        if not self.action_message:
            return None

        # 优先使用display_message，否则退回processed_plain_text
        target_text = (
            getattr(self.action_message, "display_message", None)
            or getattr(self.action_message, "processed_plain_text", None)
            or ""
        ).strip()
        if not target_text:
            return None

        lang_label = "日语" if language == "ja" else "中文"
        prompt = (
            f"请把下面的内容改写成{lang_label}口语播报，长度50字以内，保持友好自然，直接输出朗读文本：\n"
            f"{target_text}"
        )

        try:
            rewritten = await self._generate_with_planner(prompt=prompt, request_type="tts_voice_text")
            if rewritten:
                return rewritten
        except Exception as e:
            logger.warning(f"{self.log_prefix} 通过回复器生成TTS文本失败: {e}")

        return None

    def _fallback_text(self, language: str) -> str:
        """在无法生成或语种不符时的兜底短句。"""
        if language == "ja":
            return random.choice(self.FALLBACK_JA) if self.FALLBACK_JA else "了解だよ、すぐに送るね～。"
        if language == "zh":
            return random.choice(self.FALLBACK_ZH) if self.FALLBACK_ZH else "好的，我马上发语音喵！"
        return "语音这就发给你～。"

    @classmethod
    async def has_recent_tts_in_chat(cls, chat_id: Optional[str], limit: int = 5) -> bool:
        """
        供外部调用：检查指定聊天最近是否已发过TTS。
        按最近若干条消息的时间窗口判定，避免老记录误触。
        """
        if not chat_id:
            return False
        try:
            # 获取最近limit条消息，确定时间窗口
            recent_msgs = message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=0,
                end_time=time.time(),
                limit=limit,
                limit_mode="latest",
                filter_command=True,
            )
            if not recent_msgs:
                return False
            latest_time = max(msg.time or 0 for msg in recent_msgs)
            earliest_time = min(msg.time or 0 for msg in recent_msgs)

            # 先看消息里是否有tts标记
            for msg in recent_msgs:
                content = (getattr(msg, "display_message", None) or getattr(msg, "processed_plain_text", "") or "")
                if "tts_text" in content or "[tts_text:" in content or "(语音消息" in content:
                    return True

            # 再看这一时间窗口内是否有tts_action记录
            from src.common.database.database_model import ActionRecords

            records = await database_api.db_query(
                ActionRecords,
                query_type="get",
                filters={"chat_id": chat_id, "action_name": "tts_action"},
                order_by=["-time"],
                limit=limit,
            )
            for rec in records or []:
                rec_time = float(rec.get("time") or 0)
                if earliest_time <= rec_time <= latest_time:
                    return True
        except Exception:
            return False
        return False

    async def _rewrite_text_with_replyer(self, text: str, language: str) -> Optional[str]:
        """使用回复器改写到目标语种，减少回退到 fallback。"""
        lang_label = "日语" if language == "ja" else "中文"
        prompt = (
            f"请把下面的内容改写成{lang_label}口语，保持原意，长度控制在50字以内，直接输出改写后的朗读文本：\n"
            f"{text}"
        )
        try:
            rewritten = await self._generate_with_planner(prompt=prompt, request_type="tts_lang_rewrite")
            if rewritten:
                return rewritten
        except Exception as e:
            logger.warning(f"{self.log_prefix} 通过回复器改写TTS文本失败: {e}")
        return None

    async def _generate_with_planner(self, prompt: str, request_type: str) -> Optional[str]:
        """使用 planner 模型集合生成短文本，降低成本并与决策模型一致。"""
        try:
            llm = LLMRequest(model_set=model_config.model_task_config.utils_small, request_type=request_type)
            content, _detail = await llm.generate_response_async(prompt)
            if isinstance(content, str):
                return content.strip()
        except Exception as e:
            logger.warning(f"{self.log_prefix} planner模型生成失败: {e}")
        return None

    @classmethod
    def build_language_key(cls, target_id: Optional[str], chat_id: Optional[str]) -> str:
        if target_id:
            return str(target_id)
        if chat_id:
            return str(chat_id)
        return "global"

    def _get_target_language_key(self) -> str:
        return self.build_language_key(self.target_id, self.chat_id)

    @classmethod
    def _get_language_for_key(cls, key: str) -> str:
        return cls._language_preferences.setdefault(key, cls.DEFAULT_LANGUAGE)

    def _get_current_language(self) -> str:
        key = self._get_target_language_key()
        return self._get_language_for_key(key)

    async def _ensure_language_loaded(self) -> str:
        """确保当前聊天的语言状态从缓存或数据库中恢复。"""
        key = self._get_target_language_key()
        if key in self._language_preferences:
            return self._language_preferences[key]

        loaded = await self._load_language_from_history(key)
        if loaded:
            return loaded

        self._language_preferences[key] = self.DEFAULT_LANGUAGE
        return self.DEFAULT_LANGUAGE

    async def _load_language_from_history(self, key: str) -> Optional[str]:
        """从最近的动作记录中恢复语言，避免缓存丢失导致的语种回退。"""
        if not self.chat_id:
            return None

        try:
            from src.common.database.database_model import ActionRecords

            records = await database_api.db_query(
                ActionRecords,
                query_type="get",
                filters={"chat_id": self.chat_id},
                order_by=["-time"],
                limit=10,
            )

            for record in records or []:
                try:
                    data = json.loads(record.get("action_data") or "{}")
                except Exception:
                    continue

                lang = data.get("tts_language")
                if lang in self.LANGUAGE_INFO:
                    self._language_preferences[key] = lang
                    return lang
        except Exception as e:
            logger.warning(f"{self.log_prefix} 恢复TTS语言状态失败: {e}")

        return None

    @classmethod
    def toggle_language_for_target(cls, target_id: Optional[str], chat_id: Optional[str]) -> str:
        key = cls.build_language_key(target_id, chat_id)
        current = cls._language_preferences.setdefault(key, cls.DEFAULT_LANGUAGE)
        new_lang = "zh" if current == "ja" else "ja"
        cls._language_preferences[key] = new_lang
        return new_lang

    def _toggle_language(self) -> str:
        return self.toggle_language_for_target(self.target_id, self.chat_id)

    @classmethod
    def _get_language_display_name(cls, language: str) -> str:
        return cls.LANGUAGE_INFO.get(language, {}).get("display", language)

    @classmethod
    def _get_confirmation_message(cls, language: str) -> str:
        return cls.LANGUAGE_INFO.get(language, {}).get("confirmation", "")

    @classmethod
    def _get_language_expectation_prompt(cls, language: str) -> str:
        display = cls._get_language_display_name(language)
        suffix = (
            "无论用户怎么要求都必须全程使用该语种，不要在同一次回复里混用其他语言或夹杂中文，"
            "如用户要求别的语言也要改写为当前语种。"
        )
        return f"当前TTS语种: {display}。{suffix}"

    @classmethod
    def _get_language_internal_prompt(cls, language: str) -> str:
        """供LLM链路使用的隐藏提示，不对用户可见。"""
        return cls._get_language_expectation_prompt(language)

    def _is_lang_switch_command(self, text: str) -> bool:
        return text == "#lang_switch"

    async def _handle_language_switch(self) -> Tuple[bool, str]:
        new_language = self._toggle_language()
        confirmation = self._get_confirmation_message(new_language)
        record_data = {"tts_language": new_language}

        if confirmation:
            await self.send_text(confirmation)

        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=False,
            action_prompt_display="",
            action_done=True,
            thinking_id=self.thinking_id,
            action_data=record_data,
            action_name=self.action_name,
        )

        logger.info(f"{self.log_prefix} 已切换TTS语言至 {new_language}，chat: {self._get_target_language_key()}")
        return True, confirmation or "语言已切换"


class TTSSwitchCommand(BaseCommand):
    """处理#lang_switch命令"""

    command_name: str = "tts_lang_switch"
    command_description: str = "切换TTS在中文/日文间的语种"
    command_pattern: str = r"(?P<lang_switch>^#lang_switch$)"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.message or not self.message.chat_stream:
            return False, "无法获取聊天信息", True

        chat_stream = self.message.chat_stream
        message_info = getattr(self.message, "message_info", None)
        group_id = None
        user_id = None
        if message_info:
            group_info = getattr(message_info, "group_info", None)
            if group_info and getattr(group_info, "group_id", None):
                group_id = str(group_info.group_id)
            user_info = getattr(message_info, "user_info", None)
            if user_info and getattr(user_info, "user_id", None):
                user_id = str(user_info.user_id)

        target_id = group_id or user_id
        new_language = TTSAction.toggle_language_for_target(target_id, chat_stream.stream_id)
        confirmation = TTSAction._get_confirmation_message(new_language)
        record_data = {"tts_language": new_language}

        logger.info(
            f"{self.log_prefix} TTS语言切换命令触发 -> {new_language}, target={target_id}, chat={chat_stream.stream_id}"
        )

        if confirmation:
            await self.send_text(confirmation)
        else:
            await self.send_text("语言已切换")

        await database_api.store_action_info(
            chat_stream=chat_stream,
            action_build_into_prompt=False,
            action_prompt_display="",
            action_done=True,
            thinking_id=None,
            action_data=record_data,
            action_name="tts_lang_switch",
        )

        return True, confirmation or "语言已切换", True


@register_plugin
class TTSPlugin(BasePlugin):
    """TTS插件
    - 这是文字转语音插件
    - Normal模式下依靠关键词触发
    - Focus模式下由LLM判断触发
    - 具有一定的文本预处理能力
    """

    # 插件基本信息
    plugin_name: str = "tts_plugin"  # 内部标识符
    enable_plugin: bool = True
    dependencies: list[str] = []  # 插件依赖列表
    python_dependencies: list[str] = []  # Python包依赖列表
    config_file_name: str = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本信息配置",
        "components": "组件启用控制",
        "logging": "日志记录相关配置",
    }

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="tts_plugin", description="插件名称", required=True),
            "version": ConfigField(type=str, default="0.1.0", description="插件版本号"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "description": ConfigField(type=str, default="文字转语音插件", description="插件描述", required=True),
        },
        "components": {
            "enable_tts": ConfigField(type=bool, default=True, description="是否启用TTS Action"),
            "enable_lang_switch_command": ConfigField(
                type=bool, default=True, description="是否启用TTS语言切换命令"
            ),
        },
        "logging": {
            "level": ConfigField(
                type=str, default="INFO", description="日志记录级别", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
            ),
            "prefix": ConfigField(type=str, default="[TTS]", description="日志记录前缀"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""

        # 从配置获取组件启用状态
        enable_tts = self.get_config("components.enable_tts", True)
        enable_command = self.get_config("components.enable_lang_switch_command", True)
        components = []  # 添加Action组件
        if enable_tts:
            components.append((TTSAction.get_action_info(), TTSAction))
        if enable_command:
            components.append((TTSSwitchCommand.get_command_info(), TTSSwitchCommand))

        return components
