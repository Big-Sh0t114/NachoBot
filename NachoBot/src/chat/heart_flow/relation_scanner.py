"""
关系扫描器
周期性扫描聊天消息，自动提取用户印象并写入 memory_points
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple
from json_repair import repair_json

from src.common.logger import get_logger
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.plugin_system.apis import message_api
from src.chat.utils.chat_message_builder import build_readable_messages
from src.person_info.person_info import (
    Person,
    get_memory_content_from_memory,
    get_weight_from_memory,
    person_info_manager,
)
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager

logger = get_logger("relation_scanner")


def _is_bot_self(platform: str, user_id: str) -> bool:
    """判断是否是 bot 自身"""
    try:
        if str(user_id) == str(global_config.bot.qq_account):
            return True
        if hasattr(global_config, "bilibili") and hasattr(global_config.bilibili, "bilibili_bot_account"):
            if str(user_id) == str(global_config.bilibili.bilibili_bot_account):
                return True
    except Exception:
        pass
    return False


def init_prompt():
    """初始化关系扫描器的提示词模板"""

    Prompt(
        """\
以下是用户 "{person_name}" 的近期发言：
----------------------
{user_messages}
----------------------

请分析上述发言，提取其中包含的用户个人信息、偏好、习惯、身份、经历、态度或其他可记忆的印象点。

**提取规则**：
- 只提取**明确表达**的信息，不要推测
- 每条印象用**一句简短的话**概括
- 如果没有可提取的信息，返回空数组

请严格用 JSON 格式输出，不要输出任何其他内容：
{{
    "impressions": [
        "印象1",
        "印象2"
    ]
}}

如果没有可提取的印象，输出：
{{
    "impressions": []
}}
""",
        "relation_scanner_extract",
    )

    Prompt(
        """\
以下是一些记忆条目的分类：
----------------------
{category_list}
----------------------
每一个分类条目类型代表了你对用户："{person_name}"的印象的一个类别

现在，你有一条对 {person_name} 的新记忆内容：
{memory_point}

请判断该记忆内容是否属于上述分类，请给出分类的名称。
如果不属于上述分类，请输出一个合适的分类名称，对新记忆内容进行概括。要求分类名具有概括性。
注意分类数一般不超过5个
请严格用json格式输出，不要输出任何其他内容：
{{
    "category": "分类名称"
}}""",
        "relation_scanner_category",
    )

    Prompt(
        """\
以下是有关{category}的现有记忆：
----------------------
{memory_list}
----------------------

现在，你有一条对 {person_name} 的新记忆内容：
{memory_point}

请判断该新记忆内容是否已经存在于现有记忆中，你可以对现有进行进行以下修改：
注意，一般来说记忆内容不超过5个，且记忆文本不应太长

1.新增：当记忆内容不存在于现有记忆，且不存在矛盾，请用json格式输出：
{{
    "new_memory": "需要新增的记忆内容"
}}
2.加深印象：如果这个新记忆已经存在于现有记忆中，在内容上与现有记忆类似，请用json格式输出：
{{
    "memory_id": 1,
    "integrate_memory": "加深后的记忆内容，合并内容类似的新记忆和旧记忆"
}}
3.整合：如果这个新记忆与现有记忆产生矛盾，请你结合其他记忆进行整合，用json格式输出：
{{
    "memory_id": 1,
    "integrate_memory": "整合后的记忆内容，合并内容矛盾的新记忆和旧记忆"
}}

现在，请你根据情况选出合适的修改方式，并输出json，不要输出其他内容：
""",
        "relation_scanner_update",
    )


class RelationScanner:
    """关系扫描器：周期性扫描聊天消息，自动提取用户印象"""

    def __init__(self, chat_id: str, check_interval: int = 1800, message_threshold: int = 50):
        self.chat_id = chat_id
        self._chat_display_name = self._get_chat_display_name()
        self.log_prefix = f"[{self._chat_display_name}]"

        self.check_interval = check_interval
        self.message_threshold = message_threshold

        # 时间记录
        self.last_scan_time = time.time()

        # 防重复：记录每个用户最后扫描时间
        self._recently_scanned_users: Dict[str, float] = {}
        self._user_cooldown = 600  # 同一用户 10 分钟内不重复扫描

        # LLM
        self.scanner_llm = LLMRequest(
            model_set=model_config.model_task_config.utils_small, request_type="relation_scanner"
        )

        # 后台循环
        self._periodic_task: Optional[asyncio.Task] = None
        self._running = False

    def _get_chat_display_name(self) -> str:
        try:
            chat_name = get_chat_manager().get_stream_name(self.chat_id)
            if chat_name:
                return chat_name
            return self.chat_id[:8] + "..." if len(self.chat_id) > 20 else self.chat_id
        except Exception:
            return self.chat_id[:8] + "..." if len(self.chat_id) > 20 else self.chat_id

    async def start(self):
        """启动后台定期扫描循环"""
        if not global_config.relationship.enable_relationship:
            logger.info(f"{self.log_prefix} 关系系统未启用，跳过关系扫描器")
            return

        if self._running:
            logger.warning(f"{self.log_prefix} 关系扫描器已在运行")
            return

        self._running = True
        self._periodic_task = asyncio.create_task(self._periodic_scan_loop())
        logger.info(
            f"{self.log_prefix} 关系扫描器已启动 | 检查间隔: {self.check_interval}s | 消息阈值: {self.message_threshold}"
        )

    async def stop(self):
        """停止后台扫描循环"""
        self._running = False
        if self._periodic_task:
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
            self._periodic_task = None
        logger.info(f"{self.log_prefix} 关系扫描器已停止")

    async def _periodic_scan_loop(self):
        """后台定期扫描循环"""
        try:
            while self._running:
                await asyncio.sleep(self.check_interval)
                try:
                    await self._scan()
                except Exception as e:
                    logger.error(f"{self.log_prefix} 关系扫描出错: {e}", exc_info=True)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 关系扫描循环被取消")
            raise

    async def _scan(self):
        """执行一次扫描"""
        current_time = time.time()

        # 获取自上次扫描以来的新消息
        new_messages = message_api.get_messages_by_time_in_chat(
            chat_id=self.chat_id,
            start_time=self.last_scan_time,
            end_time=current_time,
            limit=0,
            limit_mode="latest",
            filter_mai=False,
            filter_command=False,
        )

        if not new_messages or len(new_messages) < self.message_threshold:
            logger.info(
                f"{self.log_prefix} 关系扫描器检查 | 累积消息: {len(new_messages) if new_messages else 0} 条 | "
                f"未达阈值 {self.message_threshold}，跳过"
            )
            return

        logger.info(
            f"{self.log_prefix} 对满 {self.message_threshold} 条对话开启记忆扫描与更新 | 累积消息: {len(new_messages)} 条 | "
            f"时间窗口: {self.last_scan_time:.0f} -> {current_time:.0f}"
        )

        # 更新扫描时间
        self.last_scan_time = current_time

        # 按用户分组
        user_messages: Dict[Tuple[str, str], List] = {}  # (platform, user_id) -> messages
        for msg in new_messages:
            try:
                platform = msg.user_info.platform
                user_id = str(msg.user_info.user_id)

                # 过滤 bot 自身
                if _is_bot_self(platform, user_id):
                    continue

                key = (platform, user_id)
                if key not in user_messages:
                    user_messages[key] = []
                user_messages[key].append(msg)
            except Exception:
                continue

        # 对每个用户独立处理
        processed_count = 0
        for (platform, user_id), messages in user_messages.items():
            # 少于等于 2 条发言，跳过
            if len(messages) <= 2:
                continue

            # 检查冷却时间
            person_key = f"{platform}_{user_id}"
            last_scanned = self._recently_scanned_users.get(person_key, 0)
            if current_time - last_scanned < self._user_cooldown:
                continue

            # 处理该用户
            try:
                await self._process_user_messages(platform, user_id, messages, new_messages)
                self._recently_scanned_users[person_key] = current_time
                processed_count += 1
            except Exception as e:
                logger.error(f"{self.log_prefix} 处理用户 {platform}:{user_id} 失败: {e}", exc_info=True)

        if processed_count > 0:
            logger.info(f"{self.log_prefix} 关系扫描完成 | 处理了 {processed_count} 个用户")

    async def _process_user_messages(self, platform: str, user_id: str, messages: list, all_messages: list = None):
        """处理单个用户的消息，提取印象并写入记忆"""

        # 获取 Person
        person = Person(platform=platform, user_id=user_id)
        if not person.is_known:
            logger.debug(f"{self.log_prefix} 用户 {platform}:{user_id} 未注册，跳过")
            return

        person_name = person.person_name or f"用户{user_id}"

        # 构建用户发言文本
        user_text = build_readable_messages(
            messages,
            replace_bot_name=True,
            timestamp_mode="normal_no_YMD",
            read_mark=0.0,
            truncate=True,
            show_actions=False,
        )

        if not user_text.strip():
            return

        # Step 1: 调用 LLM 提取印象
        prompt = await global_prompt_manager.format_prompt(
            "relation_scanner_extract",
            person_name=person_name,
            user_messages=user_text,
        )

        response, _ = await self.scanner_llm.generate_response_async(prompt=prompt)
        if not response or not response.strip():
            return

        # 解析印象列表
        try:
            data = json.loads(repair_json(response))
            impressions = data.get("impressions", [])
        except Exception as e:
            logger.warning(f"{self.log_prefix} 解析印象提取结果失败: {e}, raw={response[:200]}")
            return

        if not impressions:
            logger.debug(f"{self.log_prefix} 用户 {person_name} 近期发言无可提取印象")
            return

        logger.info(f"{self.log_prefix} 用户 {person_name} 提取到 {len(impressions)} 条印象")

        # Step 2: 对每条印象执行分类 + 更新
        for impression in impressions[:5]:  # 最多处理 5 条
            if not isinstance(impression, str) or not impression.strip():
                continue
            try:
                await self._categorize_and_store(person, impression.strip())
            except Exception as e:
                logger.error(f"{self.log_prefix} 存储印象失败: {e}", exc_info=True)

        # 用完整对话（含 bot 回复）更新 nickname
        if all_messages:
            try:
                full_conversation = build_readable_messages(
                    all_messages,
                    replace_bot_name=True,
                    timestamp_mode="normal_no_YMD",
                    read_mark=0.0,
                    truncate=True,
                    show_actions=False,
                )
                if full_conversation.strip():
                    # 从消息列表获取用户最新的平台昵称和群名片
                    latest_nickname = ""
                    cardname = ""
                    for msg in reversed(messages):
                        try:
                            latest_nickname = msg.user_info.user_nickname or ""
                            cardname = getattr(msg.user_info, "user_cardname", "") or ""
                            if latest_nickname:
                                break
                        except Exception:
                            pass

                    asyncio.create_task(
                        person_info_manager.qv_person_name(
                            person_id=person.person_id,
                            user_nickname=latest_nickname,
                            user_cardname=cardname,
                            user_avatar="",
                            request=full_conversation[:1000],
                        )
                    )
            except Exception as e:
                logger.error(f"{self.log_prefix} 更新用户 {person_name} 昵称失败: {e}", exc_info=True)

    async def _categorize_and_store(self, person: Person, impression: str):
        """对一条印象进行分类并存储（复用 BuildRelationAction 的逻辑）"""

        person_name = person.person_name or "未知"

        # Step A: 分类
        category_list = person.get_all_category()
        category_list_str = "\n".join(category_list) if category_list else "无分类"

        prompt = await global_prompt_manager.format_prompt(
            "relation_scanner_category",
            category_list=category_list_str,
            memory_point=impression,
            person_name=person_name,
        )

        response, _ = await self.scanner_llm.generate_response_async(prompt=prompt)

        category = "其他"
        if response and response.strip():
            try:
                category_data = json.loads(repair_json(response))
                category = category_data.get("category", "其他") or "其他"
            except Exception:
                pass

        # Step B: 更新记忆
        memory_list = person.get_memory_list_by_category(category)
        if not memory_list:
            # 直接新增
            person.memory_points.append(f"{category}:{impression}:1.0")
            person.sync_to_database()
            logger.info(f"{self.log_prefix} {person_name} 的记忆：[新增] {impression} ({category})")
            return

        # 构建现有记忆列表让 LLM 判断
        memory_list_str = ""
        memory_list_id = {}
        for idx, memory in enumerate(memory_list, start=1):
            memory_content = get_memory_content_from_memory(memory)
            memory_list_str += f"{idx}. {memory_content}\n"
            memory_list_id[idx] = memory

        prompt = await global_prompt_manager.format_prompt(
            "relation_scanner_update",
            category=category,
            memory_list=memory_list_str,
            memory_point=impression,
            person_name=person_name,
        )

        response, _ = await self.scanner_llm.generate_response_async(prompt=prompt)

        if not response or not response.strip():
            # LLM 无响应，直接新增
            person.memory_points.append(f"{category}:{impression}:1.0")
            person.sync_to_database()
            return

        try:
            update_data = json.loads(repair_json(response))
        except Exception:
            person.memory_points.append(f"{category}:{impression}:1.0")
            person.sync_to_database()
            return

        new_memory = update_data.get("new_memory", "")
        memory_id = update_data.get("memory_id", "")
        integrate_memory = update_data.get("integrate_memory", "")

        if new_memory:
            person.memory_points.append(f"{category}:{new_memory}:1.0")
            person.sync_to_database()
            logger.info(f"{self.log_prefix} {person_name} 的记忆：[新增] {new_memory} ({category})")
        elif memory_id and integrate_memory:
            old_memory = memory_list_id.get(memory_id)
            if old_memory:
                old_content = get_memory_content_from_memory(old_memory)
                del_count = person.del_memory(category, old_content)
                if del_count > 0:
                    old_weight = get_weight_from_memory(old_memory)
                    person.memory_points.append(f"{category}:{integrate_memory}:{old_weight + 1.0}")
                    person.sync_to_database()
            logger.info(f"{self.log_prefix} {person_name} 的记忆：[更新] {old_content} -> {integrate_memory}")


init_prompt()
