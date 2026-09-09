"""
聊天内容概括器
用于累积、打包和压缩聊天记录
"""

import asyncio
import hashlib
import json
import time
import re
import difflib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from json_repair import repair_json

from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.config.config import model_config
from src.llm_models.utils_model import LLMRequest
from src.plugin_system.apis import message_api
from src.chat.utils.chat_message_builder import build_readable_messages
from src.person_info.person_info import Person
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager

logger = get_logger("chat_history_summarizer")

HIPPO_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "hippo_memorizer"
MAX_PENDING_MESSAGES = 500
TOPIC_CHECK_MESSAGE_LIMIT = 80
TIME_TRIGGER_MIN_MESSAGES = 20
TOPIC_RETRY_COOLDOWN_SECONDS = 300


def is_bot_self(platform: str, user_id: str) -> bool:
    try:
        from src.config.config import global_config

        return (
            str(platform or "").strip().lower()
            == str(global_config.bot.platform or "").strip().lower()
            and str(user_id) == str(global_config.bot.qq_account)
        )
    except Exception:
        pass
    return False


def init_prompt():
    """初始化提示词模板"""

    topic_analysis_prompt = """【历史话题标题列表】（仅标题，不含具体内容）：
{history_topics_block}
【历史话题标题列表结束】

【本次聊天记录】（每条消息前有编号，用于后续引用）：
{messages_block}
【本次聊天记录结束】

请完成以下任务：
**识别话题**
1. 识别【本次聊天记录】中正在进行的一个或多个话题；
2. 【本次聊天记录】的中的消息可能与历史话题有关，也可能毫无关联。
2. 判断【历史话题标题列表】中的话题是否在【本次聊天记录】中出现，如果出现，则直接使用该历史话题标题字符串；

**选取消息**
1. 对于每个话题（新话题或历史话题），从上述带编号的消息中选出与该话题强相关的消息编号列表；
2. 每个话题用一句话清晰地描述正在发生的事件，必须包含时间（大致即可）、人物、主要事件和主题，保证精准且有区分度； 

请先输出一段简短思考，说明有什么话题，哪些是不包含在历史话题中的，哪些是包含在历史话题中的，并说明为什么；
然后严格以 JSON 格式输出【本次聊天记录】中涉及的话题，格式如下：
[
  {{
    "topic": "话题",
    "message_indices": [1, 2, 5]
  }},
  ...
]
"""
    Prompt(topic_analysis_prompt, "hippo_topic_analysis_prompt")

    topic_summary_prompt = """
请基于以下话题，对聊天记录片段进行概括，提取以下信息：

**话题**：{topic}

**要求**：
1. 关键词：提取与话题相关的关键词，用列表形式返回（3-10个关键词）
2. 概括：对这段话的平文本概括（50-200字），要求：
   - 仔细地转述发生的事件和聊天内容；
   - 可以适当摘取聊天记录中的原文；
   - 重点突出事件的发展过程和结果；
   - 围绕话题这个中心进行概括。
3. 关键信息：提取话题中的关键信息点，用列表形式返回（3-8个关键信息点），每个关键信息点应该简洁明了。

请以JSON格式返回，格式如下：
{{
    "keywords": ["关键词1", "关键词2", ...],
    "summary": "概括内容",
    "key_point": ["关键信息1", "关键信息2", ...]
}}

聊天记录：
{original_text}

请直接返回JSON，不要包含其他内容。
"""
    Prompt(topic_summary_prompt, "hippo_topic_summary_prompt")


@dataclass
class MessageBatch:
    """消息批次（用于触发话题检查的原始消息累积）"""

    messages: List[DatabaseMessages]
    start_time: float
    end_time: float
    start_cursor_time: float = 0.0
    start_cursor_message_id: str = ""


@dataclass
class TopicCacheItem:
    """
    话题缓存项

    Attributes:
        topic: 话题标题（一句话描述时间、人物、事件和主题）
        messages: 与该话题相关的消息字符串列表（已经通过 build 函数转成可读文本）
        participants: 涉及到的发言人昵称集合
        no_update_checks: 连续多少次“检查”没有新增内容
    """

    topic: str
    messages: List[str] = field(default_factory=list)
    participants: Set[str] = field(default_factory=set)
    no_update_checks: int = 0
    # Immutable source coordinates for every message selected for this topic.
    # These are persisted separately from the rendered message text so a
    # retry/restart can derive the same id without depending on an LLM summary.
    source_message_keys: List[Tuple[float, str]] = field(default_factory=list)
    source_start_time: Optional[float] = None
    source_end_time: Optional[float] = None
    source_identity: str = ""


class ChatHistorySummarizer:
    """聊天内容概括器"""

    def __init__(self, chat_id: str, check_interval: int = 60, writeback_drain_timeout: float = 10.0):
        """
        初始化聊天内容概括器

        Args:
            chat_id: 聊天ID
            check_interval: 定期检查间隔（秒），默认60秒
        """
        self.chat_id = chat_id
        self._chat_display_name = self._get_chat_display_name()
        self.log_prefix = f"[{self._chat_display_name}]"

        # 记录时间点，用于计算新消息
        self.last_check_time = time.time()
        self.last_check_message_id = ""

        # 记录上一次话题检查的时间，用于判断是否需要触发检查
        self.last_topic_check_time = time.time()

        # 当前累积的消息批次
        self.current_batch: Optional[MessageBatch] = None
        self._topic_retry_cooldown_until = 0.0

        # 话题缓存：topic_str -> TopicCacheItem
        # 在内存中维护，并通过本地文件实时持久化
        self.topic_cache: Dict[str, TopicCacheItem] = {}
        self._safe_chat_id = self._sanitize_chat_id(self.chat_id)
        self._topic_cache_file = HIPPO_CACHE_DIR / f"{self._safe_chat_id}.json"
        # 注意：批次加载需要异步查询消息，所以在 start() 中调用

        # LLM请求器，用于压缩聊天内容
        self.summarizer_llm = LLMRequest(
            model_set=model_config.model_task_config.utils, request_type="chat_history_summarizer"
        )

        # 后台循环相关
        self.check_interval = check_interval  # 检查间隔（秒）
        self._periodic_task: Optional[asyncio.Task] = None
        self._writeback_tasks: Set[asyncio.Task[None]] = set()
        self._writeback_drain_timeout = max(0.0, writeback_drain_timeout)
        self._stopping = False
        self._running = False
        # Serialize start/stop state transitions.  The lock is acquired
        # before any disk/network await so concurrent starts cannot both pass
        # the producer guard and publish two periodic tasks.
        self._lifecycle_lock = asyncio.Lock()

    def _get_chat_display_name(self) -> str:
        """获取聊天显示名称"""
        try:
            chat_name = get_chat_manager().get_stream_name(self.chat_id)
            if chat_name:
                return chat_name
            # 如果获取失败，使用简化的chat_id显示
            if len(self.chat_id) > 20:
                return f"{self.chat_id[:8]}..."
            return self.chat_id
        except Exception:
            # 如果获取失败，使用简化的chat_id显示
            if len(self.chat_id) > 20:
                return f"{self.chat_id[:8]}..."
            return self.chat_id

    def _sanitize_chat_id(self, chat_id: str) -> str:
        """用于生成可作为文件名的 chat_id"""
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", chat_id)

    def _load_topic_cache_from_disk(self):
        """在启动时加载本地话题缓存（同步部分），支持重启后继续"""
        try:
            if not self._topic_cache_file.exists():
                return

            with self._topic_cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)

            self.last_topic_check_time = data.get("last_topic_check_time", self.last_topic_check_time)
            cursor = data.get("last_check_cursor") or {}
            if isinstance(cursor, dict):
                self.last_check_time = float(cursor.get("time", data.get("last_check_time", self.last_check_time)))
                self.last_check_message_id = str(cursor.get("message_id", "") or "")
            else:
                self.last_check_time = float(data.get("last_check_time", self.last_check_time))
            topics_data = data.get("topics", {})
            loaded_count = 0
            for topic, payload in topics_data.items():
                if not isinstance(payload, dict):
                    continue
                source_message_keys: List[Tuple[float, str]] = []
                raw_source_keys = payload.get("source_message_keys", [])
                if isinstance(raw_source_keys, list):
                    for raw_key in raw_source_keys:
                        if not isinstance(raw_key, (list, tuple)) or len(raw_key) < 2:
                            continue
                        try:
                            source_message_keys.append((float(raw_key[0]), str(raw_key[1])))
                        except (TypeError, ValueError):
                            continue
                source_start_time = payload.get("source_start_time")
                source_end_time = payload.get("source_end_time")
                try:
                    source_start_time = (
                        float(source_start_time) if source_start_time is not None else None
                    )
                except (TypeError, ValueError):
                    source_start_time = None
                try:
                    source_end_time = float(source_end_time) if source_end_time is not None else None
                except (TypeError, ValueError):
                    source_end_time = None
                self.topic_cache[topic] = TopicCacheItem(
                    topic=topic,
                    messages=payload.get("messages", []),
                    participants=set(payload.get("participants", [])),
                    no_update_checks=payload.get("no_update_checks", 0),
                    source_message_keys=source_message_keys,
                    source_start_time=source_start_time,
                    source_end_time=source_end_time,
                    source_identity=str(payload.get("source_identity", "") or ""),
                )
                loaded_count += 1

            if loaded_count:
                logger.info(f"{self.log_prefix} 已加载 {loaded_count} 个话题缓存，继续追踪")
        except Exception as e:
            logger.error(f"{self.log_prefix} 加载话题缓存失败: {e}")

    async def _load_batch_from_disk(self):
        """在启动时加载聊天批次，支持重启后继续"""
        try:
            if not self._topic_cache_file.exists():
                return

            with self._topic_cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)

            batch_data = data.get("current_batch")
            if not batch_data:
                return

            start_time = batch_data.get("start_time")
            end_time = batch_data.get("end_time")
            if not start_time or not end_time:
                return

            start_cursor_time = batch_data.get("start_cursor_time")
            start_cursor_message_id = str(batch_data.get("start_cursor_message_id", "") or "")
            if start_cursor_time is not None:
                try:
                    from src.chat.utils.chat_message_builder import get_raw_msg_after_cursor_with_chat

                    messages = get_raw_msg_after_cursor_with_chat(
                        self.chat_id,
                        float(start_cursor_time),
                        start_cursor_message_id,
                        end_time=time.time(),
                        filter_bot=False,
                        filter_command=False,
                    )
                    checkpoint = (self.last_check_time, self.last_check_message_id)
                    messages = [
                        message for message in messages
                        if self._message_key(message) <= checkpoint
                    ]
                except (ImportError, AttributeError):
                    messages = message_api.get_messages_by_time_in_chat(
                        chat_id=self.chat_id,
                        start_time=start_time,
                        end_time=end_time,
                        limit=0,
                        limit_mode="latest",
                        filter_mai=False,
                        filter_command=False,
                    )
            else:
                # Backward-compatible restore for cache files written before
                # the compound cursor was introduced.
                messages = message_api.get_messages_by_time_in_chat(
                    chat_id=self.chat_id,
                    start_time=start_time,
                    end_time=end_time,
                    limit=0,
                    limit_mode="latest",
                    filter_mai=False,
                    filter_command=False,
                )

            if messages:
                self.current_batch = MessageBatch(
                    messages=sorted(messages, key=self._message_key),
                    start_time=start_time,
                    end_time=end_time,
                    start_cursor_time=float(start_cursor_time if start_cursor_time is not None else start_time),
                    start_cursor_message_id=start_cursor_message_id,
                )
                trimmed = self._trim_pending_batch()
                if trimmed:
                    self._persist_topic_cache()
                logger.info(
                    f"{self.log_prefix} 已恢复聊天批次，包含 {len(self.current_batch.messages)} 条消息"
                )
        except Exception as e:
            logger.error(f"{self.log_prefix} 加载聊天批次失败: {e}")

    def _persist_topic_cache(self):
        """实时持久化话题缓存和聊天批次，避免重启后丢失"""
        try:
            # Keep a cursor-only checkpoint even when a batch is discarded or
            # finalized.  Removing the file here would make a restart replay
            # the entire old window and could lose messages that arrived just
            # before eviction.
            self._topic_cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "chat_id": self.chat_id,
                "last_topic_check_time": self.last_topic_check_time,
                "last_check_time": self.last_check_time,
                "last_check_cursor": {
                    "time": self.last_check_time,
                    "message_id": self.last_check_message_id,
                },
                "topics": {
                    topic: {
                        "messages": item.messages,
                        "participants": sorted(item.participants),
                        "no_update_checks": item.no_update_checks,
                        "source_message_keys": [
                            [source_time, source_key]
                            for source_time, source_key in item.source_message_keys
                        ],
                        "source_start_time": item.source_start_time,
                        "source_end_time": item.source_end_time,
                        "source_identity": item.source_identity,
                    }
                    for topic, item in self.topic_cache.items()
                },
            }

            # 保存当前批次的时间范围（如果有）
            if self.current_batch:
                data["current_batch"] = {
                    "start_time": self.current_batch.start_time,
                    "end_time": self.current_batch.end_time,
                    "start_cursor_time": self.current_batch.start_cursor_time,
                    "start_cursor_message_id": self.current_batch.start_cursor_message_id,
                }

            with self._topic_cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"{self.log_prefix} 持久化话题缓存失败: {e}")

    async def process(self, current_time: Optional[float] = None):
        """
        处理聊天内容概括

        Args:
            current_time: 当前时间戳，如果为None则使用time.time()
        """
        if current_time is None:
            current_time = time.time()

        try:
            # Use a compound cursor so equal timestamps are deterministic and
            # restart does not need an overlapping (duplicate-prone) window.
            try:
                from src.chat.utils.chat_message_builder import get_raw_msg_after_cursor_with_chat

                new_messages = get_raw_msg_after_cursor_with_chat(
                    self.chat_id,
                    self.last_check_time,
                    self.last_check_message_id,
                    end_time=current_time,
                    filter_bot=False,
                    filter_command=False,
                )
            except (ImportError, AttributeError):
                new_messages = message_api.get_messages_by_time_in_chat(
                    chat_id=self.chat_id,
                    start_time=self.last_check_time,
                    end_time=current_time,
                    limit=0,
                    limit_mode="latest",
                    filter_mai=False,
                    filter_command=False,
                )

            if not new_messages:
                # 没有新消息，检查是否需要进行“话题检查”
                if self.current_batch and self.current_batch.messages:
                    await self._check_and_run_topic_check(current_time)
                return

            logger.debug(
                f"{self.log_prefix} 开始处理聊天概括，时间窗口: {self.last_check_time:.2f} -> {current_time:.2f}"
            )

            # Advance to the last message actually observed, not merely the
            # wall-clock end of the query.  This avoids losing late-arriving
            # rows and gives equal timestamps a stable message-id tie-break.
            previous_cursor = (self.last_check_time, self.last_check_message_id)
            new_messages.sort(key=self._message_key)
            last_message = max(new_messages, key=self._message_key)
            self.last_check_time, self.last_check_message_id = self._message_key(last_message)

            # 如果有当前批次，添加新消息
            if self.current_batch:
                before_count = len(self.current_batch.messages)
                self.current_batch.messages.extend(new_messages)
                self.current_batch.end_time = self.last_check_time
                self._trim_pending_batch()
                logger.info(
                    f"{self.log_prefix} 更新聊天检查批次: {before_count} -> {len(self.current_batch.messages)} 条消息"
                )
                # 更新批次后持久化
                self._persist_topic_cache()
            else:
                # 创建新批次
                self.current_batch = MessageBatch(
                    messages=new_messages,
                    start_time=new_messages[0].time if new_messages else current_time,
                    end_time=new_messages[-1].time if new_messages else current_time,
                    start_cursor_time=previous_cursor[0],
                    start_cursor_message_id=previous_cursor[1],
                )
                self._trim_pending_batch()
                logger.debug(f"{self.log_prefix} 新建聊天检查批次: {len(new_messages)} 条消息")
                # 创建批次后持久化
                self._persist_topic_cache()

            # Persist the exact cursor before any LLM work can fail or the
            # process can be restarted.
            self._persist_topic_cache()

            # 检查是否需要触发“话题检查”
            await self._check_and_run_topic_check(current_time)

        except Exception as e:
            logger.error(f"{self.log_prefix} 处理聊天内容概括时出错: {e}")
            import traceback

            traceback.print_exc()

    @staticmethod
    def _message_key(message: DatabaseMessages) -> tuple[float, str]:
        return (float(getattr(message, "time", 0.0)), str(getattr(message, "message_id", "") or ""))

    def _trim_pending_batch(self) -> bool:
        """Keep pending messages ordered and bounded, advancing the batch cursor on eviction."""
        if not self.current_batch or not self.current_batch.messages:
            return False

        ordered_messages = sorted(self.current_batch.messages, key=self._message_key)
        overflow_count = max(0, len(ordered_messages) - MAX_PENDING_MESSAGES)
        if not overflow_count:
            self.current_batch.messages = ordered_messages
            return False

        discarded = ordered_messages[:overflow_count]
        retained = ordered_messages[overflow_count:]
        last_discarded = discarded[-1]
        self.current_batch.messages = retained
        self.current_batch.start_cursor_time, self.current_batch.start_cursor_message_id = self._message_key(
            last_discarded
        )
        self.current_batch.start_time = float(getattr(retained[0], "time", self.current_batch.start_time))
        logger.warning(
            f"{self.log_prefix} 待处理批次超过 {MAX_PENDING_MESSAGES} 条，丢弃最旧的 {overflow_count} 条消息"
        )
        return True

    def _consume_pending_prefix(self, messages: List[DatabaseMessages]) -> None:
        """Consume only a successfully processed prefix of the pending batch."""
        if not self.current_batch or not messages:
            return

        consumed_count = min(len(messages), len(self.current_batch.messages))
        consumed = self.current_batch.messages[:consumed_count]
        remaining = self.current_batch.messages[consumed_count:]
        if not remaining:
            self.current_batch = None
            return

        last_consumed = consumed[-1]
        self.current_batch.messages = remaining
        self.current_batch.start_cursor_time, self.current_batch.start_cursor_message_id = self._message_key(
            last_consumed
        )
        self.current_batch.start_time = float(getattr(remaining[0], "time", self.current_batch.start_time))

    def _source_message_key(self, message: DatabaseMessages) -> Tuple[float, str]:
        """Return an immutable coordinate for a selected source message.

        Normal database rows have a stable ``message_id``.  A few adapters and
        isolated tests provide only a timestamp, so use a deterministic hash of
        source-row fields as a fallback; the rendered/LLM-generated summary is
        intentionally never part of this coordinate.
        """
        message_time = float(getattr(message, "time", 0.0))
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id:
            source_payload = {
                "chat_id": self.chat_id,
                "time": message_time,
                "processed_plain_text": getattr(message, "processed_plain_text", "") or "",
                "display_message": getattr(message, "display_message", "") or "",
                "user_id": getattr(message, "user_id", "") or "",
                "user_platform": getattr(message, "user_platform", "") or "",
                "chat_info_user_id": getattr(message, "chat_info_user_id", "") or "",
            }
            message_id = "content:" + hashlib.sha256(
                json.dumps(source_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
        return message_time, message_id

    def _refresh_topic_source_identity(
        self,
        topic: str,
        item: TopicCacheItem,
        fallback_start: Optional[float] = None,
        fallback_end: Optional[float] = None,
    ) -> None:
        """Normalize source coordinates and derive a restart-stable identity."""
        item.source_message_keys = sorted(set(item.source_message_keys), key=lambda key: (key[0], key[1]))
        if item.source_message_keys:
            item.source_start_time = min(key[0] for key in item.source_message_keys)
            item.source_end_time = max(key[0] for key in item.source_message_keys)
            identity_payload = {
                "chat_id": self.chat_id,
                "source_message_keys": [list(key) for key in item.source_message_keys],
            }
        else:
            # Legacy cache entries do not have source IDs.  Their persisted
            # rendered source chunks and range are still deterministic input;
            # do not use the LLM-generated topic/summary as identity material.
            if item.source_start_time is None and fallback_start is not None:
                item.source_start_time = float(fallback_start)
            if item.source_end_time is None and fallback_end is not None:
                item.source_end_time = float(fallback_end)
            identity_payload = {
                "chat_id": self.chat_id,
                "source_message_keys": [],
                "source_start_time": item.source_start_time,
                "source_end_time": item.source_end_time,
                "source_messages": list(item.messages),
            }
        item.source_identity = hashlib.sha256(
            json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _topic_source_range(
        item: TopicCacheItem, fallback_start: float, fallback_end: float
    ) -> Tuple[float, float]:
        start_time = item.source_start_time if item.source_start_time is not None else fallback_start
        end_time = item.source_end_time if item.source_end_time is not None else fallback_end
        return min(start_time, end_time), max(start_time, end_time)

    async def _check_and_run_topic_check(self, current_time: float):
        """
        检查是否需要进行一次“话题检查”

        触发条件：
        - 当前批次消息数 >= 80，或者
        - 距离上一次检查的时间 > 10800 秒（3小时）且消息数 >= 20
        """
        if not self.current_batch or not self.current_batch.messages:
            return

        if current_time < self._topic_retry_cooldown_until:
            logger.debug(
                f"{self.log_prefix} 话题检查处于冷却期，剩余 {self._topic_retry_cooldown_until - current_time:.1f}秒"
            )
            return False

        messages = self.current_batch.messages
        message_count = len(messages)
        time_since_last_check = current_time - self.last_topic_check_time

        # 格式化时间差显示
        if time_since_last_check < 60:
            time_str = f"{time_since_last_check:.1f}秒"
        elif time_since_last_check < 3600:
            time_str = f"{time_since_last_check / 60:.1f}分钟"
        else:
            time_str = f"{time_since_last_check / 3600:.1f}小时"

        logger.debug(f"{self.log_prefix} 批次状态检查 | 消息数: {message_count} | 距上次检查: {time_str}")

        # 检查“话题检查”触发条件
        should_check = False

        # 条件1: 消息数量达到有效检查批次上限，触发一次检查
        if message_count >= TOPIC_CHECK_MESSAGE_LIMIT:
            should_check = True
            logger.info(
                f"{self.log_prefix} 触发检查条件: 消息数量达到 {message_count} 条（阈值: {TOPIC_CHECK_MESSAGE_LIMIT}条）"
            )

        # 条件2: 距离上一次检查 > 3600 * 3 秒（3小时）且消息数量达到最小阈值，触发一次检查
        elif time_since_last_check > 3600 * 3 and message_count >= TIME_TRIGGER_MIN_MESSAGES:
            should_check = True
            logger.info(
                f"{self.log_prefix} 触发检查条件: 距上次检查 {time_str}（阈值: 3小时）且消息数量达到 {message_count} 条（阈值: {TIME_TRIGGER_MIN_MESSAGES}条）"
            )

        if should_check:
            messages_to_check = messages[:TOPIC_CHECK_MESSAGE_LIMIT]
            consumed = await self._run_topic_check_and_update_cache(messages_to_check)
            if consumed:
                # 只有成功处理的前缀可以被消费；LLM 失败时保留完整批次和游标。
                self._consume_pending_prefix(messages_to_check)
                self.last_topic_check_time = current_time
                self._topic_retry_cooldown_until = 0.0
            else:
                self._topic_retry_cooldown_until = current_time + TOPIC_RETRY_COOLDOWN_SECONDS
            self._persist_topic_cache()
            return consumed
        return False

    async def _run_topic_check_and_update_cache(self, messages: List[DatabaseMessages]) -> bool:
        """
        执行一次“话题检查”：
        1. 首先确认这段消息里是否有 Bot 发言，没有则直接丢弃本次批次；
        2. 将消息编号并转成字符串，构造 LLM Prompt；
        3. 把历史话题标题列表放入 Prompt，要求 LLM：
           - 识别当前聊天中的话题（1 个或多个）；
           - 为每个话题选出相关消息编号；
           - 若话题属于历史话题，则沿用原话题标题；
        4. LLM 返回 JSON：多个 {topic, message_indices}；
        5. 更新本地话题缓存，并根据规则触发“话题打包存储”。
        """
        if not messages:
            return True

        start_time = messages[0].time
        end_time = messages[-1].time

        logger.info(
            f"{self.log_prefix} 开始话题检查 | 消息数: {len(messages)} | 时间范围: {start_time:.2f} - {end_time:.2f}"
        )

        # 1. 检查当前批次内是否有 bot 发言（只检查当前批次，不往前推）
        # 原因：我们要记录的是 bot 参与过的对话片段，如果当前批次内 bot 没有发言，
        # 说明 bot 没有参与这段对话，不应该记录
        has_bot_message = False

        for msg in messages:
            # 使用统一的 is_bot_self 函数判断是否是机器人自己（支持多平台，包括 WebUI）
            if is_bot_self(msg.user_info.platform, msg.user_info.user_id):
                has_bot_message = True
                break

        if not has_bot_message:
            logger.info(
                f"{self.log_prefix} 当前批次内无 Bot 发言，丢弃本次检查 | 时间范围: {start_time:.2f} - {end_time:.2f}"
            )
            return True

        # 2. 构造编号后的消息字符串和参与者信息
        numbered_lines, index_to_msg_str, index_to_msg_text, index_to_participants = (
            self._build_numbered_messages_for_llm(messages)
        )

        # 3. 调用 LLM 识别话题，并得到 topic -> indices（失败时最多重试 3 次）
        existing_topics = list(self.topic_cache.keys())
        max_retries = 3
        attempt = 0
        success = False
        topic_to_indices: Dict[str, List[int]] = {}

        while attempt < max_retries:
            attempt += 1
            try:
                success, topic_to_indices = await self._analyze_topics_with_llm(
                    numbered_lines=numbered_lines,
                    existing_topics=existing_topics,
                )
            except Exception as e:
                success, topic_to_indices = False, {}
                logger.warning(f"{self.log_prefix} 话题识别第 {attempt} 次尝试抛出异常: {e}")

            if success and topic_to_indices:
                if attempt > 1:
                    logger.info(
                        f"{self.log_prefix} 话题识别在第 {attempt} 次重试后成功 | 话题数: {len(topic_to_indices)}"
                    )
                break

            logger.warning(
                f"{self.log_prefix} 话题识别失败或无有效话题，第 {attempt} 次尝试失败"
                + ("" if attempt >= max_retries else "，准备重试")
            )

        if not success or not topic_to_indices:
            logger.error(f"{self.log_prefix} 话题识别连续 {max_retries} 次失败或始终无有效话题，本次检查放弃")
            # LLM 失败不是消费成功；保留 current_batch 和游标等待后续重试。
            return False

        # 3.5. 检查新话题是否与历史话题相似（相似度>=90%则使用历史标题）
        topic_mapping = self._build_topic_mapping(topic_to_indices, similarity_threshold=0.9)

        # 应用话题映射：将相似的新话题标题替换为历史话题标题
        if topic_mapping:
            new_topic_to_indices: Dict[str, List[int]] = {}
            for new_topic, indices in topic_to_indices.items():
                # 如果这个新话题需要映射到历史话题
                if new_topic in topic_mapping:
                    historical_topic = topic_mapping[new_topic]
                    # 如果历史话题已经存在，合并消息索引
                    if historical_topic in new_topic_to_indices:
                        # 合并索引并去重
                        combined_indices = list(set(new_topic_to_indices[historical_topic] + indices))
                        new_topic_to_indices[historical_topic] = combined_indices
                    else:
                        new_topic_to_indices[historical_topic] = indices
                else:
                    # 不需要映射，保持原样
                    new_topic_to_indices[new_topic] = indices
            topic_to_indices = new_topic_to_indices

        # 4. 统计哪些话题在本次检查中有新增内容
        updated_topics: Set[str] = set()

        for topic, indices in topic_to_indices.items():
            if not indices:
                continue

            item = self.topic_cache.get(topic)
            created_item = False
            if not item:
                # 新话题
                item = TopicCacheItem(topic=topic)
                self.topic_cache[topic] = item
                created_item = True

            # 收集属于该话题的消息文本（不带编号）
            topic_msg_texts: List[str] = []
            new_participants: Set[str] = set()
            for idx in indices:
                if 1 <= idx <= len(messages):
                    item.source_message_keys.append(self._source_message_key(messages[idx - 1]))
                msg_text = index_to_msg_text.get(idx)
                if not msg_text:
                    continue
                topic_msg_texts.append(msg_text)
                new_participants.update(index_to_participants.get(idx, set()))

            if not topic_msg_texts:
                if created_item:
                    self.topic_cache.pop(topic, None)
                continue

            # 将本次检查中属于该话题的所有消息合并为一个字符串（不带编号）
            merged_text = "\n".join(topic_msg_texts)
            item.messages.append(merged_text)
            item.participants.update(new_participants)
            self._refresh_topic_source_identity(topic, item)
            # 本次检查中该话题有更新，重置计数
            item.no_update_checks = 0
            updated_topics.add(topic)

        # 5. 对于本次没有更新的历史话题，no_update_checks + 1
        for topic, item in list(self.topic_cache.items()):
            if topic not in updated_topics:
                item.no_update_checks += 1

        # 6. 检查是否有话题需要打包存储
        topics_to_finalize: List[str] = []
        for topic, item in self.topic_cache.items():
            if item.no_update_checks >= 3:
                logger.info(f"{self.log_prefix} 话题[{topic}] 连续 3 次检查无新增内容，触发打包存储")
                topics_to_finalize.append(topic)
                continue
            if len(item.messages) > 5:
                logger.info(f"{self.log_prefix} 话题[{topic}] 消息条数超过 4，触发打包存储")
                topics_to_finalize.append(topic)

        if not updated_topics and topic_to_indices:
            logger.warning(f"{self.log_prefix} 话题识别未选择任何有效消息，本次检查保留批次待重试")
            return False

        # Classification mutations (including a failed finalization's source
        # coordinates) must survive a process restart before any topic is
        # removed.
        self._persist_topic_cache()
        for topic in topics_to_finalize:
            item = self.topic_cache.get(topic)
            if not item:
                continue
            finalized = await self._finalize_and_store_topic(
                topic=topic,
                item=item,
                start_time=start_time,
                end_time=end_time,
            )
            if finalized:
                # 只有主 ChatHistory 写入得到确认后才消费缓存项。
                self.topic_cache.pop(topic, None)
                self._persist_topic_cache()
            else:
                # 压缩或主库写入失败时保留完整源坐标，供下一次重试。
                self._persist_topic_cache()

        return True

    def _find_most_similar_topic(
        self, new_topic: str, existing_topics: List[str], similarity_threshold: float = 0.9
    ) -> Optional[tuple[str, float]]:
        """
        查找与给定新话题最相似的历史话题

        Args:
            new_topic: 新话题标题
            existing_topics: 历史话题标题列表
            similarity_threshold: 相似度阈值，默认0.9（90%）

        Returns:
            Optional[tuple[str, float]]: 如果找到相似度>=阈值的历史话题，返回(历史话题标题, 相似度)，
                                         否则返回None
        """
        if not existing_topics:
            return None

        best_match = None
        best_similarity = 0.0

        for existing_topic in existing_topics:
            similarity = difflib.SequenceMatcher(None, new_topic, existing_topic).ratio()
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = existing_topic

        # 如果相似度达到阈值，返回匹配结果
        if best_match and best_similarity >= similarity_threshold:
            return (best_match, best_similarity)

        return None

    def _build_topic_mapping(
        self, topic_to_indices: Dict[str, List[int]], similarity_threshold: float = 0.9
    ) -> Dict[str, str]:
        """
        构建新话题到历史话题的映射（如果相似度>=阈值）

        Args:
            topic_to_indices: 新话题到消息索引的映射
            similarity_threshold: 相似度阈值，默认0.9（90%）

        Returns:
            Dict[str, str]: 新话题 -> 历史话题的映射字典
        """
        existing_topics_list = list(self.topic_cache.keys())
        topic_mapping: Dict[str, str] = {}

        for new_topic in topic_to_indices.keys():
            # 如果新话题已经在历史话题中，不需要检查
            if new_topic in existing_topics_list:
                continue

            # 查找最相似的历史话题
            result = self._find_most_similar_topic(new_topic, existing_topics_list, similarity_threshold)
            if result:
                historical_topic, similarity = result
                topic_mapping[new_topic] = historical_topic
                logger.info(
                    f"{self.log_prefix} 话题相似度检查: '{new_topic}' 与历史话题 '{historical_topic}' 相似度 {similarity:.2%}，使用历史标题"
                )

        return topic_mapping

    def _build_numbered_messages_for_llm(
        self, messages: List[DatabaseMessages]
    ) -> tuple[List[str], Dict[int, str], Dict[int, str], Dict[int, Set[str]]]:
        """
        将消息转为带编号的字符串，供 LLM 选择使用。

        返回:
            numbered_lines: ["1. xxx", "2. yyy", ...]  # 带编号，用于 LLM 选择
            index_to_msg_str: idx -> "idx. xxx"  # 带编号，用于 LLM 选择
            index_to_msg_text: idx -> "xxx"  # 不带编号，用于最终存储
            index_to_participants: idx -> {nickname1, nickname2, ...}
        """
        numbered_lines: List[str] = []
        index_to_msg_str: Dict[int, str] = {}
        index_to_msg_text: Dict[int, str] = {}  # 不带编号的消息文本
        index_to_participants: Dict[int, Set[str]] = {}

        for idx, msg in enumerate(messages, start=1):
            # 使用 build_readable_messages 生成可读文本
            try:
                text = build_readable_messages(
                    messages=[msg],
                    replace_bot_name=True,
                    timestamp_mode="normal_no_YMD",
                    read_mark=0.0,
                    truncate=False,
                    show_actions=False,
                ).strip()
            except Exception:
                # 回退到简单文本
                text = getattr(msg, "processed_plain_text", "") or ""

            # 获取发言人昵称
            participants: Set[str] = set()
            try:
                platform = (
                    getattr(msg, "user_platform", None)
                    or (msg.user_info.platform if msg.user_info else None)
                    or msg.chat_info.platform
                )
                user_id = msg.user_info.user_id if msg.user_info else None
                if platform and user_id:
                    person = Person(platform=platform, user_id=user_id)
                    if person.person_name:
                        participants.add(person.person_name)
            except Exception:
                pass

            # 带编号的字符串（用于 LLM 选择）
            line = f"{idx}. {text}"
            numbered_lines.append(line)
            index_to_msg_str[idx] = line
            # 不带编号的文本（用于最终存储）
            index_to_msg_text[idx] = text
            index_to_participants[idx] = participants

        return numbered_lines, index_to_msg_str, index_to_msg_text, index_to_participants

    async def _analyze_topics_with_llm(
        self,
        numbered_lines: List[str],
        existing_topics: List[str],
    ) -> tuple[bool, Dict[str, List[int]]]:
        """
        使用 LLM 识别本次检查中的话题，并为每个话题选择相关消息编号。

        要求：
        - 话题用一句话清晰描述正在发生的事件，包括时间、人物、主要事件和主题；
        - 可以有 1 个或多个话题；
        - 若某个话题与历史话题列表中的某个话题是同一件事，请直接使用历史话题的字符串；
        - 输出 JSON，格式：
          [
            {
              "topic": "话题标题字符串",
              "message_indices": [1, 2, 5]
            },
            ...
          ]
        """
        if not numbered_lines:
            return False, {}

        history_topics_block = "\n".join(f"- {t}" for t in existing_topics) if existing_topics else "（当前无历史话题）"
        messages_block = "\n".join(numbered_lines)

        prompt = await global_prompt_manager.format_prompt(
            "hippo_topic_analysis_prompt",
            history_topics_block=history_topics_block,
            messages_block=messages_block,
        )

        try:
            response, _ = await self.summarizer_llm.generate_response_async(
                prompt=prompt,
                temperature=0.3,
            )

            logger.info(f"{self.log_prefix} 话题识别LLM Prompt: {prompt}")
            logger.info(f"{self.log_prefix} 话题识别LLM Response: {response}")

            # 尝试从响应中提取JSON代码块
            json_str = None
            json_pattern = r"```json\s*(.*?)\s*```"
            matches = re.findall(json_pattern, response, re.DOTALL)

            if matches:
                # 找到JSON代码块，使用第一个匹配
                json_str = matches[0].strip()
            else:
                # 如果没有找到代码块，尝试查找JSON数组的开始和结束位置
                # 查找第一个 [ 和最后一个 ]
                start_idx = response.find("[")
                end_idx = response.rfind("]")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    json_str = response[start_idx : end_idx + 1].strip()
                else:
                    # 如果还是找不到，尝试直接使用整个响应（移除可能的markdown标记）
                    json_str = response.strip()
                    json_str = re.sub(r"^```json\s*", "", json_str, flags=re.MULTILINE)
                    json_str = re.sub(r"^```\s*", "", json_str, flags=re.MULTILINE)
                    json_str = json_str.strip()

            # 使用json_repair修复可能的JSON错误
            if json_str:
                try:
                    repaired_json = repair_json(json_str)
                    result = json.loads(repaired_json) if isinstance(repaired_json, str) else repaired_json
                except Exception as repair_error:
                    # 如果repair失败，尝试直接解析
                    logger.warning(f"{self.log_prefix} JSON修复失败，尝试直接解析: {repair_error}")
                    result = json.loads(json_str)
            else:
                raise ValueError("无法从响应中提取JSON内容")

            if not isinstance(result, list):
                logger.error(f"{self.log_prefix} 话题识别返回的 JSON 不是列表: {result}")
                return False, {}

            topic_to_indices: Dict[str, List[int]] = {}
            for item in result:
                if not isinstance(item, dict):
                    continue
                topic = item.get("topic")
                indices = item.get("message_indices") or item.get("messages") or []
                if not topic or not isinstance(topic, str):
                    continue
                if isinstance(indices, list):
                    valid_indices: List[int] = []
                    for v in indices:
                        try:
                            iv = int(v)
                            if iv > 0:
                                valid_indices.append(iv)
                        except (TypeError, ValueError):
                            continue
                    if valid_indices:
                        topic_to_indices[topic] = valid_indices

            return True, topic_to_indices

        except Exception as e:
            logger.error(f"{self.log_prefix} 话题识别 LLM 调用或解析失败: {e}")
            logger.error(f"{self.log_prefix} LLM响应: {response if 'response' in locals() else 'N/A'}")
            return False, {}

    async def _finalize_and_store_topic(
        self,
        topic: str,
        item: TopicCacheItem,
        start_time: float,
        end_time: float,
    ) -> bool:
        """
        对某个话题进行最终打包存储：
        1. 将 messages(list[str]) 拼接为 original_text；
        2. 使用 LLM 对 original_text 进行总结，得到 summary 和 keywords，theme 直接使用话题字符串；
        3. 写入数据库 ChatHistory；
        4. 完成后，调用方会从缓存中删除该话题。
        """
        if not item.messages:
            logger.info(f"{self.log_prefix} 话题[{topic}] 无消息内容，跳过打包")
            return False

        # Use the full accumulated source range, not only the latest batch.
        stored_start_time, stored_end_time = self._topic_source_range(item, start_time, end_time)
        self._refresh_topic_source_identity(
            topic,
            item,
            fallback_start=stored_start_time,
            fallback_end=stored_end_time,
        )

        original_text = "\n".join(item.messages)

        logger.info(
            f"{self.log_prefix} 开始打包话题[{topic}] | 消息数: {len(item.messages)} | 时间范围: {stored_start_time:.2f} - {stored_end_time:.2f}"
        )

        # 使用 LLM 进行总结（基于话题名）
        success, keywords, summary, key_point = await self._compress_with_llm(original_text, topic)
        if not success:
            logger.warning(f"{self.log_prefix} 话题[{topic}] LLM 概括失败，不写入数据库")
            return False

        participants = list(item.participants)

        stored = await self._store_to_database(
            start_time=stored_start_time,
            end_time=stored_end_time,
            original_text=original_text,
            participants=participants,
            theme=topic,  # 主题直接使用话题名
            keywords=keywords,
            summary=summary,
            key_point=key_point,
            source_identity=item.source_identity,
        )

        if not stored:
            logger.warning(f"{self.log_prefix} 话题[{topic}] 主数据库未确认保存，保留缓存待重试")
            return False

        logger.info(
            f"{self.log_prefix} 话题[{topic}] 成功打包并存储 | 消息数: {len(item.messages)} | 参与者数: {len(participants)}"
        )
        return True

    async def _compress_with_llm(self, original_text: str, topic: str) -> tuple[bool, List[str], str, List[str]]:
        """
        使用LLM压缩聊天内容（用于单个话题的最终总结）

        Args:
            original_text: 聊天记录原文
            topic: 话题名称

        Returns:
            tuple[bool, List[str], str, List[str]]: (是否成功, 关键词列表, 概括, 关键信息列表)
        """
        prompt = await global_prompt_manager.format_prompt(
            "hippo_topic_summary_prompt",
            topic=topic,
            original_text=original_text,
        )

        try:
            response, _ = await self.summarizer_llm.generate_response_async(prompt=prompt)

            # 解析JSON响应
            json_str = response.strip()
            json_str = re.sub(r"^```json\s*", "", json_str, flags=re.MULTILINE)
            json_str = re.sub(r"^```\s*", "", json_str, flags=re.MULTILINE)
            json_str = json_str.strip()

            # 查找JSON对象的开始与结束
            start_idx = json_str.find("{")
            if start_idx == -1:
                raise ValueError("未找到JSON对象开始标记")

            end_idx = json_str.rfind("}")
            if end_idx == -1 or end_idx <= start_idx:
                logger.warning(f"{self.log_prefix} JSON缺少结束标记，尝试自动修复")
                extracted_json = json_str[start_idx:]
            else:
                extracted_json = json_str[start_idx : end_idx + 1]

            def _parse_with_quote_fix(payload: str) -> Dict[str, Any]:
                fixed_chars: List[str] = []
                in_string = False
                escape_next = False
                i = 0
                while i < len(payload):
                    char = payload[i]
                    if escape_next:
                        fixed_chars.append(char)
                        escape_next = False
                    elif char == "\\":
                        fixed_chars.append(char)
                        escape_next = True
                    elif char == '"' and not escape_next:
                        fixed_chars.append(char)
                        in_string = not in_string
                    elif in_string and char in {"“", "”"}:
                        # 在字符串值内部，将中文引号替换为转义的英文引号
                        fixed_chars.append('\\"')
                    else:
                        fixed_chars.append(char)
                    i += 1

                repaired = "".join(fixed_chars)
                return json.loads(repaired)

            try:
                result = json.loads(extracted_json)
            except json.JSONDecodeError:
                try:
                    repaired_json = repair_json(extracted_json)
                    if isinstance(repaired_json, str):
                        result = json.loads(repaired_json)
                    else:
                        result = repaired_json
                except Exception as repair_error:
                    logger.warning(f"{self.log_prefix} repair_json 失败，使用引号修复: {repair_error}")
                    result = _parse_with_quote_fix(extracted_json)

            keywords = result.get("keywords", [])
            summary = result.get("summary", "")
            key_point = result.get("key_point", [])

            if not (keywords and summary) and key_point:
                logger.warning(f"{self.log_prefix} LLM返回的JSON中缺少字段，原文\n{response}")

            # 确保keywords和key_point是列表
            if isinstance(keywords, str):
                keywords = [keywords]
            if isinstance(key_point, str):
                key_point = [key_point]

            return True, keywords, summary, key_point

        except Exception as e:
            logger.error(f"{self.log_prefix} LLM压缩聊天内容时出错: {e}")
            logger.error(f"{self.log_prefix} LLM响应: {response if 'response' in locals() else 'N/A'}")
            # 返回失败标志和默认值
            return False, [], "压缩失败，无法生成概括", []

    async def _store_to_database(
        self,
        start_time: float,
        end_time: float,
        original_text: str,
        participants: List[str],
        theme: str,
        keywords: List[str],
        summary: str,
        key_point: Optional[List[str]] = None,
        source_identity: Optional[str] = None,
    ) -> bool:
        """存储到数据库"""
        try:
            from src.common.database.database_model import ChatHistory
            from src.plugin_system.apis import database_api

            # 准备数据
            data = {
                "chat_id": self.chat_id,
                "start_time": start_time,
                "end_time": end_time,
                "original_text": original_text,
                "participants": json.dumps(participants, ensure_ascii=False),
                "theme": theme,
                "keywords": json.dumps(keywords, ensure_ascii=False),
                "summary": summary,
                "count": 0,
            }

            # 存储 key_point（如果存在）
            if key_point is not None:
                data["key_point"] = json.dumps(key_point, ensure_ascii=False)

            # 使用db_save存储（使用start_time和chat_id作为唯一标识）
            # 由于可能有多条记录，我们使用组合键，但peewee不支持，所以使用start_time作为唯一标识
            # 但为了避免冲突，我们使用组合键：chat_id + start_time
            # 由于peewee不支持组合键，我们直接创建新记录（不提供key_field和key_value）
            saved_record = await database_api.db_save(
                ChatHistory,
                data=data,
            )

            if not saved_record:
                logger.warning(f"{self.log_prefix} 存储聊天历史记录到数据库失败")
                return False
            logger.debug(f"{self.log_prefix} 成功存储聊天历史记录到数据库")

        except Exception as e:
            logger.error(f"{self.log_prefix} 存储到数据库时出错: {e}")
            import traceback

            traceback.print_exc()
            return False

        # --- A_Memorix 双写：将摘要同步写入长期记忆 ---
        try:
            from src.memory_system.memory_service import memory_service

            if memory_service.is_enabled() and not self._stopping:
                task = asyncio.create_task(
                    self._writeback_to_a_memorix(
                        chat_id=self.chat_id,
                        start_time=start_time,
                        end_time=end_time,
                        theme=theme,
                        summary=summary,
                        keywords=keywords,
                        participants=participants,
                        key_point=key_point,
                        source_identity=source_identity,
                    ),
                    name=f"memory-writeback-{self._safe_chat_id}",
                )
                self._writeback_tasks.add(task)
                task.add_done_callback(self._writeback_tasks.discard)
        except Exception as e:
            logger.debug(f"{self.log_prefix} A_Memorix 双写启动失败（不影响主流程）: {e}")
        return True

    async def _writeback_to_a_memorix(
        self,
        chat_id: str,
        start_time: float,
        end_time: float,
        theme: str,
        summary: str,
        keywords: List[str],
        participants: List[str],
        key_point: Optional[List[str]] = None,
        source_identity: Optional[str] = None,
    ):
        """将聊天摘要异步写回 A_Memorix 长期记忆"""
        try:
            from src.memory_system.memory_service import memory_service
            # 拼接为富文本：theme + summary + key_point
            text_parts = [f"话题：{theme}", f"概括：{summary}"]
            if key_point:
                text_parts.append("关键信息：" + "；".join(key_point))

            if source_identity:
                identity = source_identity
            else:
                # Direct callers without a TopicCacheItem still get a stable
                # coordinate-based id.  Summary text is deliberately excluded.
                identity_payload = json.dumps(
                    {
                        "chat_id": chat_id,
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                identity = hashlib.sha256(identity_payload).hexdigest()
            result = await memory_service.ingest_summary(
                external_id=f"chat_summary:{identity}",
                chat_id=chat_id,
                text="\n".join(text_parts),
                participants=participants,
                time_start=start_time,
                time_end=end_time,
                tags=keywords,
                metadata={
                    "source": "chat_history_summarizer",
                    "theme": theme,
                    "key_point": key_point or [],
                },
            )
            if result.get("stored_ids"):
                logger.info(f"{self.log_prefix} A_Memorix 双写成功: {theme}")
            elif result.get("reason") == "exists":
                logger.debug(f"{self.log_prefix} A_Memorix 已存在相同摘要，跳过重复写入: {theme}")
            else:
                logger.warning(
                    f"{self.log_prefix} A_Memorix 未确认摘要写入: "
                    f"theme={theme}, reason={result.get('reason', 'unknown')}"
                )
        except Exception as e:
            logger.warning(f"{self.log_prefix} A_Memorix 双写失败: {e}")

    async def start(self):
        """启动后台定期检查循环"""
        async with self._lifecycle_lock:
            if self._periodic_task is not None:
                if not self._periodic_task.done():
                    logger.warning(f"{self.log_prefix} 后台循环仍在退出，拒绝创建第二个生产者")
                    return
                self._periodic_task = None
            if self._running:
                logger.warning(f"{self.log_prefix} 后台循环已在运行，无需重复启动")
                return

            # 恢复检查时钟与话题缓存，再恢复尚未分类的消息批次。
            self._load_topic_cache_from_disk()
            await self._load_batch_from_disk()

            self._stopping = False
            self._running = True
            periodic_task = asyncio.create_task(self._periodic_check_loop())
            self._periodic_task = periodic_task
            periodic_task.add_done_callback(self._periodic_task_done)
            logger.info(f"{self.log_prefix} 已启动后台定期检查循环 | 检查间隔: {self.check_interval}秒")

    async def stop(self):
        """先停止摘要生产者，再有界等待长期记忆双写。"""
        async with self._lifecycle_lock:
            self._stopping = True
            self._running = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._writeback_drain_timeout

            periodic_task = self._periodic_task
            if periodic_task and not periodic_task.done():
                periodic_task.cancel()
                # Never await the producer without the shared stop deadline: a
                # cancellation-resistant loop must remain tracked after timeout.
                await asyncio.wait(
                    (periodic_task,),
                    timeout=max(0.0, deadline - loop.time()),
                )
            elif periodic_task and periodic_task.done() and self._periodic_task is periodic_task:
                self._periodic_task = None

            current_task = asyncio.current_task()
            writeback_tasks = tuple(
                task for task in self._writeback_tasks if task is not current_task and not task.done()
            )
            if writeback_tasks:
                _, pending = await asyncio.wait(
                    writeback_tasks,
                    timeout=max(0.0, deadline - loop.time()),
                )
                if pending:
                    logger.warning(
                        f"{self.log_prefix} {len(pending)} 个 A_Memorix 双写任务在 "
                        f"{self._writeback_drain_timeout:.1f}秒内未完成，已取消"
                    )
                    for task in pending:
                        task.cancel()
                    # A cancellation-resistant writeback may suppress
                    # CancelledError.  Never gather it without a second deadline:
                    # stop() must remain bounded, while the task stays tracked by
                    # its done callback until it really exits.
                    await asyncio.wait(
                        pending,
                        timeout=max(0.0, deadline - loop.time()),
                    )
            self._writeback_tasks.difference_update(task for task in self._writeback_tasks if task.done())
            logger.info(f"{self.log_prefix} 已停止后台定期检查循环")

    def _periodic_task_done(self, completed: asyncio.Task) -> None:
        """Clear producer tracking only if this callback belongs to the live task."""
        if self._periodic_task is completed:
            self._periodic_task = None

    async def _periodic_check_loop(self):
        """后台定期检查循环"""
        try:
            while self._running:
                # 执行一次检查
                await self.process()

                # 等待指定间隔后再次检查
                await asyncio.sleep(self.check_interval)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 后台检查循环被取消")
            raise
        except Exception as e:
            logger.error(f"{self.log_prefix} 后台检查循环出错: {e}")
            import traceback

            traceback.print_exc()
            self._running = False


init_prompt()
