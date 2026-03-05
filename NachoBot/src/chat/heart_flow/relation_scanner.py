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
以下是一段近期包含你（bot）和多个用户的聊天记录：
----------------------
[CHAT_CONTEXT_PLACEHOLDER]
----------------------

你需要关注以下目标用户列表（按平台与ID）：
[TARGET_USERS_PLACEHOLDER]

请分析上述聊天记录，针对列出的每个目标用户，提取其中包含的个人信息、偏好、习惯、身份、经历、态度或其他可记忆的印象点，并判断你在对话中是怎么称呼该用户的。

**【印象提取规则】**：
1. 只提取**明确表达**的信息，不要推测。
2. 每条印象用**一句简短的话**概括。
3. 如果针对某个用户没有可提取的印象，该用户的 impressions 数组为空。

**【称呼/昵称提取规则】**：
1. 只能提取**你（bot）对该用户的称呼**。
2. **绝对不能**提取用户对你的称呼（这些是你的名字，不是用户的！）。
3. **不要**直接把用户的原平台昵称作为提取结果返回，除非你确实就是这么连名带姓叫ta的。
4. 如果你在对话中使用了特定的爱称、尊称或简称（如“姐姐大人”、“欧尼酱”、“主人”、“宝宝”等），请优先提取这些作为昵称。
5. 如果对话中你完全没有对该用户使用任何称呼，或者没有你的回复，请将 nickname 字段留空 ("")。

请严格用 JSON 格式输出，返回一个包含所有目标用户提取结果的列表，不要输出任何额外解释：
[
    {
        "platform": "平台名",
        "user_id": "用户ID",
        "nickname": "你对该用户的有效称呼（如果没有明确称呼请留空）",
        "impressions": [
            "印象1",
            "印象2"
        ]
    }
]
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
        self.scanner_llm = LLMRequest(model_set=model_config.model_task_config.replyer, request_type="relation_scanner")

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

        # 对每个用户独立处理计数并筛选目标用户
        target_users = []
        for (platform, user_id), messages in user_messages.items():
            # 少于等于 2 条发言，跳过
            if len(messages) <= 2:
                continue

            # 检查冷却时间
            person_key = f"{platform}_{user_id}"
            last_scanned = self._recently_scanned_users.get(person_key, 0)
            if current_time - last_scanned < self._user_cooldown:
                continue

            # 加入处理目标
            person = Person(platform=platform, user_id=user_id)
            if person.is_known:
                target_users.append(
                    {
                        "platform": platform,
                        "user_id": user_id,
                        "person_id": person.person_id,
                        "orig_nickname": messages[-1].user_info.user_nickname if messages else f"用户{user_id}",
                    }
                )
                self._recently_scanned_users[person_key] = current_time

        if not target_users:
            return

        try:
            await self._process_full_context(target_users, new_messages)
            logger.info(f"{self.log_prefix} 关系扫描完成 | 处理了 {len(target_users)} 个用户")
        except Exception as e:
            logger.error(f"{self.log_prefix} 处理上下文失败: {e}", exc_info=True)

    async def _process_full_context(self, target_users: list, all_messages: list):
        """处理完整的对话上下文，为指定的多个目标用户提取印象和称呼"""

        # 构建完整聊天文本
        chat_text = build_readable_messages(
            all_messages,
            replace_bot_name=True,
            timestamp_mode="normal_no_YMD",
            read_mark=0.0,
            truncate=True,
            show_actions=False,
        )

        if not chat_text.strip():
            return

        # 构建目标用户列表说明
        target_users_desc = ""
        for user in target_users:
            target_users_desc += f"- 平台: {user['platform']}, ID: {user['user_id']}, 原昵称: {user['orig_nickname']}\n"

        # Step 1: 调用 LLM 批量提取印象和称呼
        prompt_template = await global_prompt_manager.get_prompt_async("relation_scanner_extract")
        prompt = prompt_template.template
        prompt = prompt.replace("[CHAT_CONTEXT_PLACEHOLDER]", chat_text)
        prompt = prompt.replace("[TARGET_USERS_PLACEHOLDER]", target_users_desc)

        response, _ = await self.scanner_llm.generate_response_async(prompt=prompt)
        if not response or not response.strip():
            return

        # 解析用户提取结果列表
        try:
            results = json.loads(repair_json(response))
            if not isinstance(results, list):
                logger.warning(f"{self.log_prefix} 提取结果不是列表格式: raw={response[:200]}")
                return
        except Exception as e:
            logger.warning(f"{self.log_prefix} 解析印象提取结果失败: {e}, raw={response[:200]}")
            return

        # Step 2: 遍历每个提取的目标用户执行更新
        for user_data in results:
            if not isinstance(user_data, dict):
                continue

            platform = str(user_data.get("platform", ""))
            user_id = str(user_data.get("user_id", ""))
            impressions = user_data.get("impressions", [])
            extracted_nickname = user_data.get("nickname", "")

            if not platform or not user_id:
                continue

            # 使用 Person 对象，如果不存在会自动关联或返回
            person = Person(platform=platform, user_id=user_id)
            if not person.is_known:
                continue

            person_name = person.person_name or f"用户{user_id}"

            # 更新记忆点
            if impressions and isinstance(impressions, list):
                logger.info(f"{self.log_prefix} 用户 {person_name}({user_id}) 提取到 {len(impressions)} 条印象")
                for impression in impressions[:5]:  # 最多处理 5 条
                    if not isinstance(impression, str) or not impression.strip():
                        continue
                    try:
                        await self._categorize_and_store(person, impression.strip())
                    except Exception as e:
                        logger.error(f"{self.log_prefix} 存储印象失败: {e}", exc_info=True)
            else:
                logger.debug(f"{self.log_prefix} 用户 {person_name}({user_id}) 无可提取印象")

            # 更新昵称（如果有提取出明确的新的对话昵称）
            if extracted_nickname and isinstance(extracted_nickname, str):
                extracted_nickname = extracted_nickname.strip()
                old_nickname = person.nickname or person.person_name
                # 与现有昵称不同，且不是空串时才更新
                if extracted_nickname and extracted_nickname != old_nickname:
                    logger.info(
                        f"{self.log_prefix} 更新用户 {person_name}({user_id}) 对话昵称：{old_nickname} -> {extracted_nickname}"
                    )
                    person.nickname = extracted_nickname
                    person.sync_to_database()
                    person_info_manager.person_nickname_list[person.person_id] = extracted_nickname

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
