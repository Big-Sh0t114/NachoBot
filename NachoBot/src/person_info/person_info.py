import hashlib
import json
import time
import random
import math
import re

from json_repair import repair_json
from typing import Union, Optional

from src.common.logger import get_logger
from src.common.database.database import db
from src.common.database.database_model import PersonInfo, PersonBinding
from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config


logger = get_logger("person_info")

relation_selection_model = LLMRequest(
    model_set=model_config.model_task_config.utils_small, request_type="relation_selection"
)


def get_person_id(platform: str, user_id: Union[int, str]) -> str:
    """获取唯一id (支持多平台绑定)"""
    if "-" in platform:
        platform = platform.split("-")[1]
    user_id_str = str(user_id)

    try:
        # 1. 优先查询绑定表
        binding = PersonBinding.get_or_none(
            PersonBinding.platform == platform, PersonBinding.platform_user_id == user_id_str
        )
        if binding:
            return binding.person_id
    except Exception as e:
        logger.warning(f"查询绑定表时出错，可能表还未初始化: {e}")

    # 2. 如果没有查到，使用传统的 MD5 方式生成默认的 person_id
    components = [platform, user_id_str]
    key = "_".join(components)
    return hashlib.md5(key.encode()).hexdigest()


def get_person_id_by_person_name(person_name: str) -> str:
    """根据用户名获取用户ID（支持多平台绑定：若用户属于聚合组，返回组ID）"""
    try:
        record = PersonInfo.get_or_none(PersonInfo.person_name == person_name)
        if not record:
            return ""
        person_id = record.person_id
        try:
            from src.person_info.bind_manager import bind_manager

            merged = bind_manager._get_merged_row_for_person(person_id)
            if merged:
                return merged.person_id
        except Exception:
            pass
        return person_id
    except Exception as e:
        logger.error(f"根据用户名 {person_name} 获取用户ID时出错 (Peewee): {e}")
        return ""


def is_person_known(person_id: str = None, user_id: str = None, platform: str = None, person_name: str = None) -> bool:  # type: ignore
    if person_id:
        person = PersonInfo.get_or_none(PersonInfo.person_id == person_id)
        if person:
            return person.is_known
        # ── 多平台绑定：person_id 可能是聚合组 ID ──
        try:
            from src.person_info.bind_manager import bind_manager

            merged_row = bind_manager._get_merged_row_for_person(person_id)
            if merged_row and merged_row.merged_ids:
                primary_info = bind_manager._get_primary_info([x.strip() for x in merged_row.merged_ids.split(",")])
                if primary_info:
                    return primary_info.is_known
        except Exception:
            pass
        return False
    elif user_id and platform:
        person_id = get_person_id(platform, user_id)
        return is_person_known(person_id=person_id)
    elif person_name:
        person_id = get_person_id_by_person_name(person_name)
        person = PersonInfo.get_or_none(PersonInfo.person_id == person_id)
        return person.is_known if person else False
    else:
        return False


def get_category_from_memory(memory_point: str) -> Optional[str]:
    """从记忆点中获取分类"""
    # 按照最左边的:符号进行分割，返回分割后的第一个部分作为分类
    if not isinstance(memory_point, str):
        return None
    parts = memory_point.split(":", 1)
    return parts[0].strip() if len(parts) > 1 else None


def get_weight_from_memory(memory_point: str) -> float:
    """从记忆点中获取权重"""
    # 按照最右边的:符号进行分割，返回分割后的最后一个部分作为权重
    if not isinstance(memory_point, str):
        return -math.inf
    parts = memory_point.rsplit(":", 1)
    if len(parts) <= 1:
        return -math.inf
    try:
        return float(parts[-1].strip())
    except Exception:
        return -math.inf


def get_memory_content_from_memory(memory_point: str) -> str:
    """从记忆点中获取记忆内容"""
    # 按:进行分割，去掉第一段和最后一段，返回中间部分作为记忆内容
    if not isinstance(memory_point, str):
        return ""
    parts = memory_point.split(":")
    return ":".join(parts[1:-1]).strip() if len(parts) > 2 else ""


def extract_categories_from_response(response: str) -> list[str]:
    """从response中提取所有<>包裹的内容"""
    if not isinstance(response, str):
        return []

    import re

    pattern = r"<([^<>]+)>"
    matches = re.findall(pattern, response)
    return matches


def calculate_string_similarity(s1: str, s2: str) -> float:
    """
    计算两个字符串的相似度

    Args:
        s1: 第一个字符串
        s2: 第二个字符串

    Returns:
        float: 相似度，范围0-1，1表示完全相同
    """
    if s1 == s2:
        return 1.0

    if not s1 or not s2:
        return 0.0

    # 计算Levenshtein距离

    distance = levenshtein_distance(s1, s2)
    max_len = max(len(s1), len(s2))

    # 计算相似度：1 - (编辑距离 / 最大长度)
    similarity = 1 - (distance / max_len if max_len > 0 else 0)
    return similarity


def tokenize_for_overlap(text: str) -> list[str]:
    """简单分词，用于计算文本重叠度"""
    if not isinstance(text, str):
        return []
    text = text.strip().lower()
    if not text:
        return []
    if re.search(r"[\u4e00-\u9fff]", text):
        compact = re.sub(r"\s+", "", text)
        return [ch for ch in compact if ch.strip()]
    return re.findall(r"[a-z0-9_]+", text)


def calculate_overlap_score(text_a: str, text_b: str) -> float:
    """计算两个文本的重叠度"""
    terms_a = tokenize_for_overlap(text_a)
    terms_b = tokenize_for_overlap(text_b)
    if not terms_a or not terms_b:
        return 0.0
    set_a = set(terms_a)
    set_b = set(terms_b)
    overlap = len(set_a & set_b)
    denom = min(len(set_a), len(set_b))
    if denom == 0:
        return 0.0
    return overlap / denom


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    计算两个字符串的编辑距离

    Args:
        s1: 第一个字符串
        s2: 第二个字符串

    Returns:
        int: 编辑距离
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


class Person:
    @classmethod
    def register_person(
        cls,
        platform: str,
        user_id: str,
        nickname: str,
        group_id: Optional[str] = None,
        group_cardname: Optional[str] = None,
    ):
        """
        注册新用户，或用最新的平台资料刷新已有用户。
        必须输入 platform、user_id 和 nickname 参数

        Args:
            platform: 平台名称
            user_id: 用户ID
            nickname: 用户的平台昵称
            group_id: 群 ID（私聊时为 None）
            group_cardname: 用户在该群的当前名片

        Returns:
            Person: 新注册的Person实例
        """
        if not platform or not user_id or not nickname:
            logger.error("注册用户失败：platform、user_id 和 nickname 都是必需参数")
            return None

        # 生成唯一的person_id
        person_id = get_person_id(platform, user_id)

        if is_person_known(person_id=person_id):
            logger.debug(f"用户 {nickname} 已存在")
            person = Person(person_id=person_id)
            cls._refresh_platform_profile(
                person=person,
                platform=platform,
                user_id=user_id,
                user_nickname=nickname,
                group_id=group_id,
                group_cardname=group_cardname,
            )
            return person

        # 创建Person实例
        person = cls.__new__(cls)

        # 设置基本属性
        person.person_id = person_id
        person.platform = platform
        person.user_id = user_id
        person.nickname = nickname
        person.user_nickname = nickname

        # 初始化默认值
        person.is_known = True  # 注册后立即标记为已认识
        person.person_name = nickname  # 使用nickname作为初始person_name
        person.name_reason = "用户注册时设置的昵称"
        person.know_times = 1
        person.know_since = time.time()
        person.last_know = time.time()
        person.memory_points = []
        person.group_cardname = []
        person.vip_expire_time = None

        if group_id is not None:
            person.update_group_cardname(group_id, group_cardname, sync=False)

        # 同步到数据库
        person.sync_to_database()

        logger.info(f"成功注册新用户：{person_id}，平台：{platform}，昵称：{nickname}")

        return person

    @staticmethod
    def _original_person_id(platform: str, user_id: str) -> str:
        """返回平台账号在跨平台绑定前的原始 person_id。"""
        normalized_platform = platform.split("-", 1)[1] if "-" in platform else platform
        return hashlib.md5(f"{normalized_platform}_{user_id}".encode()).hexdigest()

    @classmethod
    def _refresh_platform_profile(
        cls,
        person: "Person",
        platform: str,
        user_id: str,
        user_nickname: str,
        group_id: Optional[str],
        group_cardname: Optional[str],
    ) -> None:
        """仅刷新平台资料，不覆盖 bot 已经学会的称呼或人物名。"""
        original_person_id = cls._original_person_id(platform, str(user_id))
        record = PersonInfo.get_or_none(PersonInfo.person_id == original_person_id)
        if record is None:
            record = PersonInfo.get_or_none(PersonInfo.person_id == person.person_id)
        if record is None:
            logger.warning(f"无法刷新用户 {platform}:{user_id} 的资料：未找到 PersonInfo")
            return

        changed = False
        if record.user_nickname != user_nickname:
            record.user_nickname = user_nickname
            changed = True

        cards = cls._load_group_cardnames(record.group_cardname)
        if group_id is not None:
            changed = cls._update_group_cardname_list(cards, group_id, group_cardname) or changed

        if changed:
            record.group_cardname = json.dumps(cards, ensure_ascii=False)
            record.save()
            logger.debug(f"已刷新用户 {platform}:{user_id} 的平台昵称/群名片")

        # 当前消息链路也立即使用新资料。
        person.user_nickname = user_nickname
        person.group_cardname = cards

    @staticmethod
    def _load_group_cardnames(raw_value) -> list[dict[str, str]]:
        if not raw_value:
            return []
        if isinstance(raw_value, list):
            items = raw_value
        else:
            try:
                items = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("group_id") is not None]

    @staticmethod
    def _update_group_cardname_list(
        cards: list[dict[str, str]], group_id: str, group_cardname: Optional[str]
    ) -> bool:
        """原地更新某个群的名片；明确的空名片会移除旧值。"""
        group_id = str(group_id)
        cardname = None if group_cardname is None else str(group_cardname).strip()
        for index, item in enumerate(cards):
            if str(item.get("group_id")) != group_id:
                continue
            if group_cardname is None:
                return False
            if not cardname:
                cards.pop(index)
                return True
            if item.get("group_cardname") == cardname:
                return False
            item["group_cardname"] = cardname
            return True

        if cardname:
            cards.append({"group_id": group_id, "group_cardname": cardname})
            return True
        return False

    def update_group_cardname(
        self, group_id: str, group_cardname: Optional[str], *, sync: bool = True
    ) -> bool:
        """更新当前用户在指定群的名片，返回资料是否变化。"""
        changed = self._update_group_cardname_list(self.group_cardname, group_id, group_cardname)
        if changed and sync:
            self.sync_to_database()
        return changed

    def __init__(self, platform: str = "", user_id: str = "", person_id: str = "", person_name: str = ""):
        if platform == global_config.bot.platform and user_id == global_config.bot.qq_account:
            self.is_known = True
            self.person_id = get_person_id(platform, user_id)
            self.user_id = user_id
            self.platform = platform
            self.nickname = global_config.bot.nickname
            self.user_nickname = global_config.bot.nickname
            self.person_name = global_config.bot.nickname
            self.name_reason = "bot self"
            self.know_times = 0
            self.know_since = None
            self.last_know = None
            self.memory_points = []
            self.group_cardname = []
            self.vip_expire_time = None
            return

        self.user_id = ""
        self.platform = ""

        if person_id:
            self.person_id = person_id
        elif person_name:
            self.person_id = get_person_id_by_person_name(person_name)
            if not self.person_id:
                self.is_known = False
                logger.warning(f"根据用户名 {person_name} 获取用户ID时，不存在用户{person_name}")
                return
        elif platform and user_id:
            self.person_id = get_person_id(platform, user_id)
            self.user_id = user_id
            self.platform = platform
        else:
            logger.error("Person 初始化失败，缺少必要参数")
            raise ValueError("Person 初始化失败，缺少必要参数")

        if not is_person_known(person_id=self.person_id):
            self.is_known = False
            logger.debug(f"用户 {platform}:{user_id}:{person_name}:{person_id} 尚未认识")
            self.person_name = f"未知用户{self.person_id[:4]}"
            return
            # raise ValueError(f"用户 {platform}:{user_id}:{person_name}:{person_id} 尚未认识")

        self.is_known = False

        # 初始化默认值
        self.nickname = ""
        self.user_nickname = ""
        self.person_name: Optional[str] = None
        self.name_reason: Optional[str] = None
        self.know_times = 0
        self.know_since = None
        self.last_know: Optional[float] = None
        self.memory_points = []
        self.group_cardname = []
        self.vip_expire_time = None

        # 从数据库加载数据
        self.load_from_database()

    def del_memory(self, category: str, memory_content: str, similarity_threshold: float = 0.95):
        """
        删除指定分类和记忆内容的记忆点

        Args:
            category: 记忆分类
            memory_content: 要删除的记忆内容
            similarity_threshold: 相似度阈值，默认0.95（95%）

        Returns:
            int: 删除的记忆点数量
        """
        if not self.memory_points:
            return 0

        deleted_count = 0
        memory_points_to_keep = []

        for memory_point in self.memory_points:
            # 跳过None值
            if memory_point is None:
                continue
            # 解析记忆点
            parts = memory_point.split(":", 2)  # 最多分割2次，保留记忆内容中的冒号
            if len(parts) < 3:
                # 格式不正确，保留原样
                memory_points_to_keep.append(memory_point)
                continue

            memory_category = parts[0].strip()
            memory_text = parts[1].strip()
            _memory_weight = parts[2].strip()

            # 检查分类是否匹配
            if memory_category != category:
                memory_points_to_keep.append(memory_point)
                continue

            # 计算记忆内容的相似度
            similarity = calculate_string_similarity(memory_content, memory_text)

            # 如果相似度达到阈值，则删除（不添加到保留列表）
            if similarity >= similarity_threshold:
                deleted_count += 1
                logger.debug(f"删除记忆点: {memory_point} (相似度: {similarity:.4f})")
            else:
                memory_points_to_keep.append(memory_point)

        # 更新memory_points
        self.memory_points = memory_points_to_keep

        # 同步到数据库
        if deleted_count > 0:
            self.sync_to_database()
            logger.info(f"成功删除 {deleted_count} 个记忆点，分类: {category}")

        return deleted_count

    def get_all_category(self):
        category_list = []
        for memory in self.memory_points:
            if memory is None:
                continue
            category = get_category_from_memory(memory)
            if category and category not in category_list:
                category_list.append(category)
        return category_list

    def get_memory_list_by_category(self, category: str):
        memory_list = []
        for memory in self.memory_points:
            if memory is None:
                continue
            if get_category_from_memory(memory) == category:
                memory_list.append(memory)
        return memory_list

    def get_random_memory_by_category(self, category: str, num: int = 1):
        memory_list = self.get_memory_list_by_category(category)
        if len(memory_list) < num:
            return memory_list
        return random.sample(memory_list, num)

    def get_top_memories_by_category(self, category: str, num: int = 1):
        memory_list = self.get_memory_list_by_category(category)
        if not memory_list:
            return []

        def _memory_weight(memory: str) -> float:
            weight = get_weight_from_memory(memory)
            return weight if math.isfinite(weight) else 0.0

        return sorted(memory_list, key=_memory_weight, reverse=True)[:num]

    def get_relevant_memories(self, query: str, max_num: int = 3, min_score: float = 0.2) -> list[str]:
        if not query or not self.memory_points:
            return []
        query = query.strip()
        if not query:
            return []

        scored = []
        for memory_point in self.memory_points:
            if memory_point is None:
                continue
            content = get_memory_content_from_memory(memory_point)

    def load_from_database(self):
        """从数据库加载个人信息数据（支持多平台绑定聚合行）"""
        try:
            # 查询数据库中的记录
            record = PersonInfo.get_or_none(PersonInfo.person_id == self.person_id)

            # ── 多平台绑定：如果 person_id 是聚合组 ID，没有直接的 PersonInfo ──
            # 则从聚合行的成员中找到最优的基底 PersonInfo 来加载
            if not record:
                try:
                    from src.person_info.bind_manager import bind_manager, MERGED_PLATFORM
                    from src.common.database.database_model import PersonBinding

                    merged_row = PersonBinding.get_or_none(
                        PersonBinding.platform == MERGED_PLATFORM, PersonBinding.person_id == self.person_id
                    )
                    if merged_row and merged_row.merged_ids:
                        # 从成员中找最优的基底信息
                        record = bind_manager._get_primary_info([x.strip() for x in merged_row.merged_ids.split(",")])
                except Exception as e:
                    logger.debug(f"查找聚合组基底信息时出错: {e}")

            if record:
                self.user_id = record.user_id or ""
                self.platform = record.platform or ""
                self.is_known = record.is_known or False
                self.nickname = record.nickname or ""
                self.user_nickname = record.user_nickname or record.nickname or ""
                self.person_name = record.person_name or self.nickname
                self.name_reason = record.name_reason or None
                self.know_times = record.know_times or 0
                self.vip_expire_time = record.vip_expire_time or None

                # ── 多平台绑定：修正 user_id/platform ──
                # 当基底记录来自非当前运行平台（如 Bilibili）时，
                # self.user_id 会被设为错误平台的 ID。
                # 这里优先从绑定表中查找当前 bot 运行平台的 user_id。
                try:
                    from src.common.database.database_model import PersonBinding
                    bot_platform = global_config.bot.platform or ""
                    if bot_platform and self.platform != bot_platform:
                        # 当前基底记录不是 bot 运行平台，尝试从绑定表查找
                        bot_binding = PersonBinding.get_or_none(
                            PersonBinding.person_id == self.person_id,
                            PersonBinding.platform == bot_platform,
                        )
                        if bot_binding and bot_binding.platform_user_id:
                            logger.debug(
                                f"用户 {self.person_id} 的 user_id 从 {self.platform}:{self.user_id} "
                                f"修正为 {bot_platform}:{bot_binding.platform_user_id}"
                            )
                            self.user_id = bot_binding.platform_user_id
                            self.platform = bot_platform
                except Exception as e:
                    logger.debug(f"修正绑定用户 user_id 时出错（不影响正常加载）: {e}")

                # 处理points字段（JSON格式的列表）
                if record.memory_points:
                    try:
                        loaded_points = json.loads(record.memory_points)
                        # 过滤掉None值，确保数据质量
                        if isinstance(loaded_points, list):
                            self.memory_points = [point for point in loaded_points if point is not None]
                        else:
                            self.memory_points = []
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"解析用户 {self.person_id} 的points字段失败，使用默认值")
                        self.memory_points = []
                else:
                    self.memory_points = []

                self.group_cardname = self._load_group_cardnames(record.group_cardname)

                # ── 多平台绑定：优先使用聚合行的记忆 ──
                try:
                    from src.person_info.bind_manager import bind_manager

                    merged_memory = bind_manager.get_merged_memory_for_person(self.person_id)
                    if merged_memory is not None:
                        self.memory_points = merged_memory
                        logger.debug(f"用户 {self.person_id} 使用聚合绑定记忆 ({len(merged_memory)} 条)")
                except Exception as e:
                    logger.debug(f"检查聚合记忆时出错（不影响正常加载）: {e}")

                logger.debug(f"已从数据库加载用户 {self.person_id} 的信息")
            else:
                self.sync_to_database()
                logger.info(f"用户 {self.person_id} 在数据库中不存在，使用默认值并创建")

        except Exception as e:
            logger.error(f"从数据库加载用户 {self.person_id} 信息时出错: {e}")
            # 出错时保持默认值

    def sync_to_database(self):
        """将所有属性同步回数据库（支持多平台绑定聚合行）"""
        if not self.is_known:
            return
        try:
            # ── 多平台绑定：如果属于绑定组，记忆写入聚合行 ──
            memory_written_to_merged = False
            try:
                from src.person_info.bind_manager import bind_manager

                memory_written_to_merged = bind_manager.update_merged_memory(self.person_id, self.memory_points)
                if memory_written_to_merged:
                    logger.debug(f"用户 {self.person_id} 的记忆已写入聚合行")
            except Exception as e:
                logger.debug(f"更新聚合记忆时出错（不影响正常保存）: {e}")

            # 准备数据
            memory_json = (
                json.dumps([point for point in self.memory_points if point is not None], ensure_ascii=False)
                if self.memory_points
                else json.dumps([], ensure_ascii=False)
            )

            data = {
                "person_id": self.person_id,
                "is_known": self.is_known,
                "platform": self.platform,
                "user_id": self.user_id,
                "nickname": self.nickname,
                "user_nickname": self.user_nickname,
                "group_cardname": json.dumps(self.group_cardname, ensure_ascii=False),
                "person_name": self.person_name,
                "name_reason": self.name_reason,
                "know_times": self.know_times,
                "know_since": self.know_since,
                "last_know": self.last_know,
                "vip_expire_time": self.vip_expire_time,
            }

            # 如果记忆没有写入聚合行，则正常写入 PersonInfo
            # 如果已写入聚合行，PersonInfo 中保留原始记忆不更新
            if not memory_written_to_merged:
                data["memory_points"] = memory_json

            # 检查记录是否存在
            record = PersonInfo.get_or_none(PersonInfo.person_id == self.person_id)

            if record:
                # 更新现有记录
                for field, value in data.items():
                    if hasattr(record, field):
                        setattr(record, field, value)
                record.save()
                logger.debug(f"已同步用户 {self.person_id} 的信息到数据库")
            else:
                # 如果记忆已写入聚合行（说明 person_id 是聚合组 ID），
                # 不需要创建 PersonInfo 空记录
                if memory_written_to_merged:
                    logger.debug(f"用户 {self.person_id} 是聚合组ID，跳过创建 PersonInfo")
                else:
                    # 创建新记录时总是写入记忆
                    data["memory_points"] = memory_json
                    PersonInfo.create(**data)
                    logger.debug(f"已创建用户 {self.person_id} 的信息到数据库")

            # ── 多平台绑定：同步 VIP 状态到绑定组的所有成员 ──
            try:
                from src.person_info.bind_manager import bind_manager

                merged_row = bind_manager._get_merged_row_for_person(self.person_id)
                if merged_row and merged_row.merged_ids:
                    member_ids = [x.strip() for x in merged_row.merged_ids.split(",")]
                    for mid in member_ids:
                        if mid == self.person_id:
                            continue
                        member_record = PersonInfo.get_or_none(PersonInfo.person_id == mid)
                        if member_record and member_record.vip_expire_time != self.vip_expire_time:
                            member_record.vip_expire_time = self.vip_expire_time
                            member_record.save()
                    logger.debug(f"已同步 VIP 状态到绑定组 ({len(member_ids)} 个成员)")
            except Exception as e:
                logger.debug(f"同步 VIP 状态到绑定组时出错（不影响正常保存）: {e}")

        except Exception as e:
            logger.error(f"同步用户 {self.person_id} 信息到数据库时出错: {e}")

    def is_vip(self) -> bool:
        """检查用户是否为有效VIP（大航海未过期）"""
        if not self.vip_expire_time:
            return False
        return time.time() < self.vip_expire_time

    def set_vip(self, duration_days: int = 31) -> None:
        """设置用户为VIP，开始倒计时。如果已是VIP则重置倒计时。

        Args:
            duration_days: VIP有效期天数，默认30天
        """
        self.vip_expire_time = time.time() + duration_days * 24 * 3600
        self.sync_to_database()
        logger.info(
            f"用户 {self.person_name}({self.person_id}) 已设置VIP，到期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.vip_expire_time))}"
        )

    def clear_expired_vip(self) -> bool:
        """清除已过期的VIP标识。返回是否清除了VIP。"""
        if self.vip_expire_time and time.time() >= self.vip_expire_time:
            self.vip_expire_time = None
            self.sync_to_database()
            logger.info(f"用户 {self.person_name}({self.person_id}) 的VIP已过期，已清除")
            return True
        return False

    def get_platform_user_ids(self) -> dict:
        """获取该用户所有已绑定平台的 user_id 映射。

        通过查询 PersonBinding 表获取当前 person_id 关联的所有平台账号。
        适用于多平台绑定场景，当 self.user_id 可能不是目标平台的 ID 时使用。

        Returns:
            dict: {platform: user_id} 映射，如 {"qq": "12345", "bilibili": "67890"}
                  若无绑定信息，返回当前记录自身的 {self.platform: self.user_id}
        """
        result = {}
        try:
            from src.common.database.database_model import PersonBinding
            from src.person_info.bind_manager import MERGED_PLATFORM

            # 查找该 person_id 关联的所有非聚合映射行
            binding_rows = PersonBinding.select().where(
                PersonBinding.person_id == self.person_id,
                PersonBinding.platform != MERGED_PLATFORM,
            )
            for row in binding_rows:
                result[row.platform] = row.platform_user_id
        except Exception as e:
            logger.debug(f"查询绑定平台映射失败: {e}")

        # 兜底：如果绑定表查不到，返回当前记录自身的信息
        if not result and self.platform and self.user_id:
            result[self.platform] = self.user_id

        return result

    def get_user_id_for_platform(self, target_platform: str) -> str:
        """获取指定平台的 user_id。

        当用户绑定了多个平台时，self.user_id 可能是任意平台的 ID。
        使用此方法可精确获取目标平台的 user_id。

        Args:
            target_platform: 目标平台名称，如 "qq", "bilibili"
                            支持前缀匹配（如 "bilibili" 可匹配 "bilibili.live"）

        Returns:
            str: 该平台的 user_id，未找到返回空字符串
        """
        try:
            from src.common.database.database_model import PersonBinding
            from src.person_info.bind_manager import MERGED_PLATFORM

            target_platform = target_platform.lower()

            # 精确匹配
            row = PersonBinding.get_or_none(
                PersonBinding.person_id == self.person_id,
                PersonBinding.platform == target_platform,
            )
            if row:
                return row.platform_user_id

            # 前缀匹配（如 "bilibili" 匹配 "bilibili.live"）
            row = PersonBinding.get_or_none(
                PersonBinding.person_id == self.person_id,
                PersonBinding.platform.startswith(target_platform),
            )
            if row:
                return row.platform_user_id

        except Exception as e:
            logger.debug(f"查询平台 {target_platform} 的 user_id 失败: {e}")

        # 兜底：如果当前记录的 platform 匹配，直接返回
        if self.platform and self.platform.lower().startswith(target_platform.lower()) and self.user_id:
            return self.user_id

        return ""

    def update_gift_value(self, amount_cny: float) -> None:
        """更新用户的直播打赏累计记忆点（实时更新，非追加）。

        记忆点格式: "直播打赏:累计送出直播打赏价值约{X}元人民币:{weight}"

        Args:
            amount_cny: 本次打赏金额（人民币元）
        """
        if amount_cny <= 0:
            return

        CATEGORY = "直播打赏"

        # 查找现有的直播打赏记忆点
        existing_memory = None
        existing_value = 0.0
        existing_weight = 1.0

        for memory_point in self.memory_points:
            if memory_point is None:
                continue
            cat = get_category_from_memory(memory_point)
            if cat == CATEGORY:
                existing_memory = memory_point
                existing_weight = get_weight_from_memory(memory_point)
                # 从记忆内容中解析已有金额
                content = get_memory_content_from_memory(memory_point)
                # content 格式: "累计送出直播打赏价值约{X}元人民币"
                try:
                    match = re.search(r"约([\d.]+)元", content)
                    if match:
                        existing_value = float(match.group(1))
                except (ValueError, AttributeError):
                    existing_value = 0.0
                break

        # 计算新的累计金额
        new_value = existing_value + amount_cny
        # 保留两位小数
        new_value = round(new_value, 2)

        # 删除旧记忆点
        if existing_memory:
            try:
                self.memory_points.remove(existing_memory)
            except ValueError:
                pass
            new_weight = existing_weight + 1.0
        else:
            new_weight = 1.0

        # 创建新记忆点
        new_memory = f"{CATEGORY}:累计送出直播打赏价值约{new_value}元人民币:{new_weight}"
        self.memory_points.append(new_memory)

        # 同步到数据库
        self.sync_to_database()
        logger.info(f"用户 {self.person_name}({self.person_id}) 直播打赏累计更新: {existing_value} -> {new_value} 元")

    async def build_relationship(self, chat_content: str = "", info_type="", skip_llm: bool = False):
        if not self.is_known:
            return ""
        # 构建points文本

        identity_details = []
        if self.user_nickname and self.person_name != self.user_nickname:
            identity_details.append(f"ta在{self.platform}上的昵称是{self.user_nickname}")
        if self.nickname and self.nickname not in {self.person_name, self.user_nickname}:
            identity_details.append(f"你平时称呼ta为{self.nickname}")
        cardnames = list(
            dict.fromkeys(
                str(item.get("group_cardname", "")).strip()
                for item in self.group_cardname
                if item.get("group_cardname")
            )
        )
        if cardnames:
            identity_details.append(f"ta当前使用的群名片有{'、'.join(cardnames)}")
        nickname_str = f"（{'；'.join(identity_details)}）" if identity_details else ""

        relation_info = ""

        points_text = ""
        category_list = self.get_all_category()

        if chat_content:
            relevant_points = self.get_relevant_memories(chat_content, max_num=2)
            if relevant_points:
                points_text = ";\n".join(relevant_points)
            elif not skip_llm:
                prompt = f"""当前聊天内容：
{chat_content}

分类列表：
{category_list}
**要求**：请你根据当前聊天内容，从以下分类中选择一个与聊天内容相关的分类，并用<>包裹输出，不要输出其他内容，不要输出引号或[]，严格用<>包裹：
例如:
<分类1><分类2><分类3>......
如果没有相关的分类，请输出<none>"""

                response, _ = await relation_selection_model.generate_response_async(prompt)
                # print(prompt)
                # print(response)
                category_list = [c for c in extract_categories_from_response(response) if c.lower() != "none"]
                if category_list:
                    for category in category_list:
                        top_memories = self.get_top_memories_by_category(category, 2)
                        if top_memories:
                            random_memory_str = ";\n".join(
                                [get_memory_content_from_memory(memory) for memory in top_memories]
                            )
                            points_text = f"有关 {category} 的内容：{random_memory_str}"
                            break
        elif info_type:
            relevant_points = self.get_relevant_memories(info_type, max_num=3)
            if relevant_points:
                points_text = ";\n".join(relevant_points)
            elif not skip_llm:
                prompt = f"""你需要获取用户{self.person_name}的 **{info_type}** 信息。

现有信息类别列表：
{category_list}
**要求**：请你根据**{info_type}**，从以下分类中选择一个与**{info_type}**相关的分类，并用<>包裹输出，不要输出其他内容，不要输出引号或[]，严格用<>包裹：
例如:
<分类1><分类2><分类3>......
如果没有相关的分类，请输出<none>"""
                response, _ = await relation_selection_model.generate_response_async(prompt)
                print(prompt)
                print(response)
                category_list = [c for c in extract_categories_from_response(response) if c.lower() != "none"]
                if category_list:
                    for category in category_list:
                        top_memories = self.get_top_memories_by_category(category, 3)
                        if top_memories:
                            random_memory_str = ";\n".join(
                                [get_memory_content_from_memory(memory) for memory in top_memories]
                            )
                            points_text = f"有关 {category} 的内容：{random_memory_str}"
                            break
        else:
            for category in category_list:
                top_memories = self.get_top_memories_by_category(category, 1)
                if top_memories:
                    memory_content = get_memory_content_from_memory(top_memories[0])
                    if memory_content:
                        points_text = f"有关 {category} 的内容：{memory_content}"
                        break

        points_info = ""
        if points_text:
            points_info = f"你还记得有关{self.person_name}的内容：{points_text}"

        vip_tag = "（该用户是你的大航海成员/VIP）" if self.is_vip() else ""

        if not (nickname_str or points_info or vip_tag):
            return ""
        relation_info = f"{self.person_name}:{nickname_str}{vip_tag}{points_info}"

        return relation_info


class PersonInfoManager:
    def __init__(self):
        self.person_name_list = {}
        self.person_nickname_list = {}  # person_id -> nickname (平台昵称)
        self.person_platform_list = {}  # person_id -> platform
        self.qv_name_llm = LLMRequest(model_set=model_config.model_task_config.utils, request_type="relation.qv_name")
        try:
            db.connect(reuse_if_open=True)
            # 设置连接池参数
            if hasattr(db, "execute_sql"):
                # 设置SQLite优化参数
                db.execute_sql("PRAGMA cache_size = -64000")  # 64MB缓存
                db.execute_sql("PRAGMA temp_store = memory")  # 临时存储在内存中
                db.execute_sql("PRAGMA mmap_size = 268435456")  # 256MB内存映射
            db.create_tables([PersonInfo, PersonBinding], safe=True)

            # create_tables(safe=True) 不会为旧 SQLite 表补列。
            # 启动时做幂等热迁移，保留已有的人物和记忆数据。
            self._ensure_profile_columns()

            # --- 热迁移逻辑：将老用户的单平台账号写入绑定表 ---
            self._migrate_old_bindings()

        except Exception as e:
            logger.error(f"数据库连接或 PersonInfo/PersonBinding 表创建失败: {e}")

    @staticmethod
    def _ensure_profile_columns() -> None:
        columns = {row[1] for row in db.execute_sql("PRAGMA table_info(person_info)").fetchall()}
        if "user_nickname" not in columns:
            db.execute_sql("ALTER TABLE person_info ADD COLUMN user_nickname TEXT")
        if "group_cardname" not in columns:
            db.execute_sql("ALTER TABLE person_info ADD COLUMN group_cardname TEXT")

        db.execute_sql(
            "UPDATE person_info SET user_nickname = nickname "
            "WHERE user_nickname IS NULL OR user_nickname = ''"
        )

    def _migrate_old_bindings(self):
        """将老的 PersonInfo 中的 platform 和 user_id 迁移到 PersonBinding 中"""
        try:
            # 检查是否已经迁移过（如果绑定表是空的，说明可能需要迁移）
            if PersonBinding.select().count() == 0:
                logger.info("检测到 PersonBinding 表为空，开始执行旧用户数据热迁移...")
                migrated_count = 0
                for person in PersonInfo.select().where(
                    PersonInfo.platform.is_null(False), PersonInfo.user_id.is_null(False)
                ):
                    # 忽略无效数据
                    if not person.platform or not person.user_id:
                        continue

                    # 如果带有 '-'，处理一下 platform 逻辑以保持与 get_person_id 一致
                    plat = person.platform.split("-")[1] if "-" in person.platform else person.platform
                    uid_str = str(person.user_id)

                    # 插入绑定表
                    try:
                        PersonBinding.create(person_id=person.person_id, platform=plat, platform_user_id=uid_str)
                        migrated_count += 1
                    except Exception as e:
                        # 可能是重复数据导致 unique 约束报错，忽略即可
                        logger.debug(f"迁移用户 {person.person_id} 时跳过: {e}")

                logger.info(f"旧用户数据热迁移完成，共迁移 {migrated_count} 条记录。")
        except Exception as e:
            logger.error(f"执行旧用户数据热迁移时出错: {e}")

        # 初始化时读取所有person_name和nickname
        try:
            for record in PersonInfo.select(
                PersonInfo.person_id, PersonInfo.person_name, PersonInfo.nickname, PersonInfo.platform
            ).where(PersonInfo.person_name.is_null(False)):
                if record.person_name:
                    self.person_name_list[record.person_id] = record.person_name
                if record.nickname:
                    self.person_nickname_list[record.person_id] = record.nickname
                if record.platform:
                    self.person_platform_list[record.person_id] = record.platform
            logger.debug(
                f"已加载 {len(self.person_name_list)} 个用户名称, {len(self.person_nickname_list)} 个昵称 (Peewee)"
            )
        except Exception as e:
            logger.error(f"从 Peewee 加载 person_name_list 失败: {e}")

    @staticmethod
    def _extract_json_from_text(text: str) -> dict:
        """从文本中提取JSON数据的高容错方法"""
        try:
            fixed_json = repair_json(text)
            if isinstance(fixed_json, str):
                parsed_json = json.loads(fixed_json)
            else:
                parsed_json = fixed_json

            if isinstance(parsed_json, list) and parsed_json:
                parsed_json = parsed_json[0]

            if isinstance(parsed_json, dict):
                return parsed_json

        except Exception as e:
            logger.warning(f"JSON提取失败: {e}")

        logger.warning(f"无法从文本中提取有效的JSON字典: {text}")
        logger.info(f"文本: {text}")
        return {"nickname": "", "reason": ""}

    async def _generate_unique_person_name(self, base_name: str) -> str:
        """生成唯一的 person_name，如果存在重复则添加数字后缀"""
        # 处理空昵称的情况
        if not base_name or base_name.isspace():
            base_name = "空格"

        # 检查基础名称是否已存在
        if base_name not in self.person_name_list.values():
            return base_name

        # 如果存在，添加数字后缀
        counter = 1
        while True:
            new_name = f"{base_name}[{counter}]"
            if new_name not in self.person_name_list.values():
                return new_name
            counter += 1

    async def qv_person_name(
        self, person_id: str, user_nickname: str, user_cardname: str, user_avatar: str, request: str = ""
    ):
        """从对话内容中提取 bot 对用户的称呼，更新 nickname"""
        if not person_id:
            logger.debug("更新昵称失败：person_id不能为空")
            return None

        person = Person(person_id=person_id)
        if not person.is_known:
            logger.debug(f"更新昵称失败：用户 {person_id} 未注册")
            return None

        old_nickname = person.nickname
        bot_name = global_config.bot.nickname

        qv_name_prompt = f"你是{bot_name}。"
        qv_name_prompt += f"\n用户的平台昵称是「{user_nickname}」"
        if user_cardname:
            qv_name_prompt += f"，群名片是「{user_cardname}」"
        if old_nickname and old_nickname != user_nickname:
            qv_name_prompt += f"，你之前叫ta「{old_nickname}」"
        qv_name_prompt += "。"

        if request:
            qv_name_prompt += f"\n\n以下是你和这个用户的近期对话记录：\n{request}\n"

        qv_name_prompt += f"\n请根据对话记录，判断你（{bot_name}）在对话中是怎么称呼这个用户的。"
        qv_name_prompt += "\n**【严格提取规则】**："
        qv_name_prompt += "\n1. 只能提取**你（bot）对该用户的称呼**。"
        qv_name_prompt += (
            "\n2. **绝对不能**提取用户对你的称呼（例如用户叫你“猫猫”、“小笨猫”等，这些是你的名字，不是用户的！）。"
        )
        qv_name_prompt += f"\n3. **不要**直接把用户的平台昵称「{user_nickname}」作为提取结果返回，除非你在对话中确实就是这么连名带姓叫ta的。"
        qv_name_prompt += "\n4. 如果你在对话中使用了特定的爱称、尊称或简称（如“姐姐大人”、“欧尼酱”、“主人”、“宝宝”等），请优先提取这些作为昵称。"
        qv_name_prompt += f"\n5. 如果对话中你完全没有使用任何明确的称呼指代该用户，请直接返回原昵称「{old_nickname or user_nickname}」。"

        qv_name_prompt += "\n请用json格式输出，不要输出其他内容："
        qv_name_prompt += """
{
    "nickname": "你对该用户的有效称呼",
    "reason": "提取依据（说明是谁在哪句话里称呼谁）"
}"""

        try:
            response, _ = await self.qv_name_llm.generate_response_async(qv_name_prompt)
            result = self._extract_json_from_text(response)

            if not result or not result.get("nickname"):
                logger.debug(f"提取用户 {person_id} 昵称失败：结果为空")
                return None

            new_nickname = result["nickname"]

            # 如果昵称没有变化，跳过更新
            if new_nickname == old_nickname:
                logger.debug(f"用户 {person_id} 昵称未变化：{new_nickname}")
                return result

            # 更新 nickname
            person.nickname = new_nickname
            person.sync_to_database()

            logger.info(
                f"更新用户 {user_nickname}({person_id}) 的昵称：{old_nickname} -> {new_nickname}，"
                f"理由：{result.get('reason', '未提供')}"
            )

            # 更新内存缓存
            self.person_nickname_list[person_id] = new_nickname
            return result

        except Exception as e:
            logger.error(f"更新用户 {person_id} 昵称时出错: {e}")
            return None


person_info_manager = PersonInfoManager()
