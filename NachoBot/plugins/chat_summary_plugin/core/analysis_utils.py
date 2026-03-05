"""
聊天分析工具类

提取公共的聊天记录分析方法，避免代码重复
"""

import re
import json
from datetime import datetime
from typing import List, Dict, Optional, Callable, Any
from collections import Counter

from src.config.config import model_config
from src.plugin_system import llm_api, get_logger
from .constants import AnalysisConfig

logger = get_logger("chat_analysis_utils")


class ChatAnalysisUtils:
    """聊天记录分析工具类"""

    # Emoji 正则表达式（完整Unicode范围）
    EMOJI_PATTERN = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "\U0001f900-\U0001f9ff"  # supplemental symbols
        "]+",
        flags=re.UNICODE,
    )

    @staticmethod
    def format_messages(messages: List[dict]) -> str:
        """格式化聊天记录为文本

        Args:
            messages: 聊天记录列表

        Returns:
            格式化的聊天记录文本
        """
        formatted = []
        for msg in messages:
            timestamp = msg.get("time", 0)
            time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
            nickname = msg.get("user_nickname", "未知用户")
            cardname = msg.get("user_cardname", "")
            display_name = cardname if cardname else nickname
            text = msg.get("processed_plain_text", "")

            if text:
                formatted.append(f"[{time_str}] {display_name}: {text}")

        return "\n".join(formatted)

    @staticmethod
    def count_emojis(text: str) -> int:
        """统计文本中的 emoji 数量（使用正则表达式）

        Args:
            text: 待统计的文本

        Returns:
            emoji 数量
        """
        matches = ChatAnalysisUtils.EMOJI_PATTERN.findall(text)
        return len(matches)

    @staticmethod
    def analyze_user_stats(messages: List[dict]) -> Dict[str, Dict]:
        """分析用户统计数据

        Args:
            messages: 聊天记录列表

        Returns:
            用户统计字典，格式: {user_id: {nickname, message_count, char_count, emoji_count, hours}}
        """
        user_stats = {}

        for msg in messages:
            user_id = str(msg.get("user_id", ""))
            if not user_id:
                continue

            nickname = msg.get("user_nickname", "未知用户")
            text = msg.get("processed_plain_text", "")

            if user_id not in user_stats:
                user_stats[user_id] = {
                    "user_id": user_id,  # 保存 user_id
                    "nickname": nickname,
                    "message_count": 0,
                    "char_count": 0,
                    "emoji_count": 0,
                    "hours": Counter(),  # 各小时发言次数
                }

            stats = user_stats[user_id]
            stats["message_count"] += 1
            stats["char_count"] += len(text)

            # 使用改进的 emoji 统计
            stats["emoji_count"] += ChatAnalysisUtils.count_emojis(text)

            # 统计发言时间
            timestamp = msg.get("time", 0)
            hour = datetime.fromtimestamp(timestamp).hour
            stats["hours"][hour] += 1

        return user_stats

    @staticmethod
    async def analyze_user_titles(
        messages: List[dict], user_stats: Dict, get_config: Callable = None
    ) -> Optional[List[Dict]]:
        """使用 LLM 分析群友称号

        Args:
            messages: 聊天记录列表（用于上下文）
            user_stats: 用户统计数据
            get_config: 配置获取函数（可选）

        Returns:
            称号列表，格式: [{name, title, reason}, ...]
        """
        try:
            # 只分析发言 >= 配置的最小发言数的用户
            active_users = {
                uid: stats
                for uid, stats in user_stats.items()
                if stats["message_count"] >= AnalysisConfig.MIN_MESSAGES_FOR_TITLE
            }

            if not active_users:
                return []

            # 构建用户数据文本
            users_text = []
            for user_id, stats in sorted(active_users.items(), key=lambda x: x[1]["message_count"], reverse=True)[
                : AnalysisConfig.MAX_USERS_FOR_TITLE
            ]:  # 使用配置的最大用户数
                night_messages = sum(stats["hours"][h] for h in range(0, 6))
                avg_chars = stats["char_count"] / stats["message_count"] if stats["message_count"] > 0 else 0
                emoji_ratio = stats["emoji_count"] / stats["message_count"] if stats["message_count"] > 0 else 0
                night_ratio = night_messages / stats["message_count"] if stats["message_count"] > 0 else 0

                users_text.append(
                    f"- {stats['nickname']}: "
                    f"发言{stats['message_count']}条, 平均{avg_chars:.1f}字, "
                    f"表情比例{emoji_ratio:.2f}, 夜间发言比例{night_ratio:.2f}"
                )

            users_info = "\n".join(users_text)

            # 构建 prompt
            prompt = f"""根据群友数据创造有趣的称号。

用户数据：
{users_info}

要求：
1. 称号2-4个汉字
2. 基于真实数据，不要编造
3. 避免重复类型（不要多个"龙王""话痨"）
4. 有创意，避免陈词滥调
5. 理由严格控制在50-70字，引用数据说明为什么

参考分类：活跃度（龙王、潜水员）、时间特征（夜猫子）、内容风格（段子手）、表情/情绪（表情帝）、互动特征（接梗高手）

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "name": "用户名",
    "title": "称号（2-4字）",
    "reason": "获得理由,引用数据（50-70字）"
  }}
]"""

            # 使用 LLM 生成
            model_task_config = model_config.model_task_config.replyer
            success, result, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_task_config,
                request_type="plugin.chat_summary.titles",
            )

            if not success:
                logger.error(f"LLM生成称号失败: {result}")
                return []

            # 解析并验证 JSON
            data = ChatAnalysisUtils._parse_llm_json(result)
            return ChatAnalysisUtils._validate_titles(data, user_stats)

        except Exception as e:
            logger.error(f"分析群友称号失败: {e}", exc_info=True)
            return []

    @staticmethod
    async def analyze_golden_quotes(messages: List[dict], get_config: Callable = None) -> Optional[List[Dict]]:
        """使用 LLM 提取群聊金句（群圣经）

        Args:
            messages: 聊天记录列表
            get_config: 配置获取函数（可选）

        Returns:
            金句列表，格式: [{content, sender, reason}, ...]
        """
        try:
            # 提取适合的消息（使用配置的长度范围）
            interesting_messages = []
            for msg in messages:
                nickname = msg.get("user_nickname", "未知用户")
                cardname = msg.get("user_cardname", "")
                display_name = cardname if cardname else nickname
                text = msg.get("processed_plain_text", "")
                timestamp = msg.get("time", 0)
                time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")

                # 清理 @ 提及格式（如 @理理<123456> → 去掉整个提及部分）
                # 使用正则移除 @用户名<数字> 格式
                text = re.sub(r"@[^<\s]+<\d+>\s*", "", text)
                text = text.strip()

                if AnalysisConfig.MIN_QUOTE_LENGTH <= len(
                    text
                ) <= AnalysisConfig.MAX_QUOTE_LENGTH and not text.startswith(("http", "www", "/")):
                    interesting_messages.append({"sender": display_name, "time": time_str, "content": text})

            if not interesting_messages:
                return []

            # 构建消息文本
            messages_text = "\n".join(
                [f"[{msg['time']}] {msg['sender']}: {msg['content']}" for msg in interesting_messages]
            )

            # 构建 prompt
            prompt = f"""从群聊记录中挑选3-5句最有趣的金句。

优先级（从高到低）：
1. 神回复、接梗高手（优先选择回复的那句，不是发起的）
2. 有上下文才有笑点的梗
3. 精彩吐槽或离谱观点
4. 高/低情商发言

要求：
- 每个金句来自不同发言人
- 避免平淡陈述句、问候语
- 内容水可以只返回2-3个
- 理由严格控制在50-70字，说明为什么有趣、回应了什么

群聊记录：
{messages_text}

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "content": "金句原文",
    "sender": "发言人",
    "reason": "选择理由（50-70字）"
  }}
]"""

            # 使用 LLM 生成
            model_task_config = model_config.model_task_config.replyer
            success, result, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_task_config,
                request_type="plugin.chat_summary.quotes",
            )

            if not success:
                logger.error(f"LLM生成金句失败: {result}")
                return []

            # 解析并验证 JSON
            data = ChatAnalysisUtils._parse_llm_json(result)
            return ChatAnalysisUtils._validate_quotes(data)

        except Exception as e:
            logger.error(f"分析金句失败: {e}", exc_info=True)
            return []

    @staticmethod
    async def analyze_depression_index(
        messages: List[dict], user_stats: Dict, get_config: Callable = None
    ) -> Optional[List[Dict]]:
        """使用 LLM 分析群友炫压抑指数

        Args:
            messages: 聊天记录列表
            user_stats: 用户统计数据
            get_config: 配置获取函数（可选）

        Returns:
            炫压抑指数列表，格式: [{name, user_id, rank, comment}, ...]
        """
        try:
            # 只分析发言 >= 配置的最小发言数的用户
            active_users = {
                uid: stats
                for uid, stats in user_stats.items()
                if stats["message_count"] >= AnalysisConfig.MIN_MESSAGES_FOR_TITLE
            }

            if not active_users:
                return []

            # 提取用户发言内容样本
            user_messages = {}
            for msg in messages:
                user_id = str(msg.get("user_id", ""))
                if user_id not in active_users:
                    continue

                text = msg.get("processed_plain_text", "")
                if len(text) < 5:  # 过滤太短的消息
                    continue

                if user_id not in user_messages:
                    user_messages[user_id] = []

                # 每人最多收集20条有效发言
                if len(user_messages[user_id]) < 20:
                    user_messages[user_id].append(text)

            if not user_messages:
                return []

            # 构建用户发言样本文本
            users_sample = []
            for user_id in sorted(
                user_messages.keys(), key=lambda uid: active_users[uid]["message_count"], reverse=True
            ):
                nickname = active_users[user_id]["nickname"]
                sample_texts = user_messages[user_id][:10]  # 提供10条样本
                users_sample.append(f"【{nickname}】\n" + "\n".join(f"  - {text[:60]}" for text in sample_texts))

            users_info = "\n\n".join(users_sample)

            # 构建 prompt
            prompt = f"""分析群友的"炫压抑"指数（娱乐向）。炫压抑=性欲望强烈但表达受抑制的失衡状态。

用户发言样本：
{users_info}

评级标准：
- S级：想色色但欲言又止,或疯狂发涩图/开黄腔(过度补偿)
- A级：经常想开车但克制扭捏
- B级：偶尔开车,表达自然
- C级：很少提及或表达健康
- D级：完全回避性话题

要求：只返回前4名,评价25-30字,采用文言文风格,文雅而有趣。

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "name": "用户名",
    "rank": "S/A/B/C/D",
    "comment": "简短评价"
  }}
]"""

            # 使用 LLM 生成
            model_task_config = model_config.model_task_config.replyer
            success, result, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_task_config,
                request_type="plugin.chat_summary.depression",
            )

            if not success:
                logger.error(f"LLM生成炫压抑指数失败: {result}")
                return []

            # 解析并验证 JSON
            data = ChatAnalysisUtils._parse_llm_json(result)
            return ChatAnalysisUtils._validate_depression_index(data, user_stats)

        except Exception as e:
            logger.error(f"分析炫压抑指数失败: {e}", exc_info=True)
            return []

    @staticmethod
    def _validate_titles(data: List[Dict[str, Any]], user_stats: Dict[str, Dict] = None) -> List[Dict[str, Any]]:
        """验证并清理群友称号数据

        Args:
            data: 原始数据列表
            user_stats: 用户统计数据（用于匹配 user_id）

        Returns:
            验证后的数据列表
        """
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # 必需字段
            if not all(key in item for key in ["name", "title", "reason"]):
                logger.warning(f"群友称号数据缺少必需字段: {item}")
                continue

            # 验证数据类型和长度
            name = str(item["name"])[:50]  # 限制长度
            title = str(item["title"])[: AnalysisConfig.MAX_TITLE_LENGTH]
            reason = str(item["reason"])[: AnalysisConfig.MAX_REASON_LENGTH]

            if not name or not title or not reason:
                continue

            # 尝试从 user_stats 中查找匹配的 user_id
            user_id = ""
            if user_stats:
                for uid, stats in user_stats.items():
                    if stats.get("nickname") == name:
                        user_id = uid
                        break

            validated.append(
                {
                    "name": name,
                    "title": title,
                    "reason": reason,
                    "user_id": user_id,  # 添加 user_id 字段
                }
            )

        return validated

    @staticmethod
    def _validate_quotes(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """验证并清理金句数据

        Args:
            data: 原始数据列表

        Returns:
            验证后的数据列表
        """
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # 必需字段
            if not all(key in item for key in ["content", "sender", "reason"]):
                logger.warning(f"金句数据缺少必需字段: {item}")
                continue

            # 验证数据类型和长度
            content = str(item["content"])[:200]  # 限制长度
            sender = str(item["sender"])[:50]
            reason = str(item["reason"])[: AnalysisConfig.MAX_REASON_LENGTH]

            # 清理 @ 提及格式（如 @理理<123456> → 去掉整个提及部分）
            content = re.sub(r"@[^<\s]+<\d+>\s*", "", content)
            content = content.strip()

            if not content or not sender or not reason:
                continue

            validated.append({"content": content, "sender": sender, "reason": reason})

        return validated

    @staticmethod
    def _validate_depression_index(
        data: List[Dict[str, Any]], user_stats: Dict[str, Dict] = None
    ) -> List[Dict[str, Any]]:
        """验证并清理炫压抑指数数据

        Args:
            data: 原始数据列表
            user_stats: 用户统计数据（用于匹配 user_id）

        Returns:
            验证后的数据列表
        """
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # 必需字段
            if not all(key in item for key in ["name", "rank", "comment"]):
                logger.warning(f"炫压抑指数数据缺少必需字段: {item}")
                continue

            # 验证数据类型和长度
            name = str(item["name"])[:50]
            rank = str(item["rank"]).upper().strip()
            comment = str(item["comment"])[:60]  # 限制评价长度（30字约60字符）

            # 验证rank是否在S/A/B/C/D中
            if rank not in ["S", "A", "B", "C", "D"]:
                logger.warning(f"无效的rank值: {rank}")
                continue

            if not name or not rank or not comment:
                continue

            # 尝试从 user_stats 中查找匹配的 user_id
            user_id = ""
            if user_stats:
                for uid, stats in user_stats.items():
                    if stats.get("nickname") == name:
                        user_id = uid
                        break

            validated.append({"name": name, "rank": rank, "comment": comment, "user_id": user_id})

        return validated[:4]  # 只返回前4个

    @staticmethod
    async def analyze_user_profile(
        messages: List[dict], user_name: str, get_config: Callable = None
    ) -> Optional[Dict[str, Any]]:
        """分析单个用户的个人画像

        Args:
            messages: 用户的聊天记录列表
            user_name: 用户昵称
            get_config: 配置获取函数（可选）

        Returns:
            用户画像数据，格式: {
                user_id: "QQ号",  # 用户ID
                tags: [标签1, 标签2],  # 1-2个个性标签
                active_hours: "18:00-23:00",  # 活跃时段描述
                fun_score: 85,  # 整活质量评分 0-100
                fun_comment: "整活评价",  # 简短评价
                topic_leadership: 75,  # 话题引导力 0-100
                topic_comment: "话题引导评价",
                rank_title: "黄金话痨III",  # 段位称号
                rank_desc: "段位描述",
                mood: "积极/中性/消极",
                mood_score: 0-100  # 心情分数
            }
        """
        try:
            if not messages:
                return None

            # 获取用户ID（从第一条消息中提取）
            user_id = str(messages[0].get("user_id", "")) if messages else ""

            # 统计基础数据
            hours_counter = Counter()
            all_texts = []

            for msg in messages:
                timestamp = msg.get("time", 0)
                hour = datetime.fromtimestamp(timestamp).hour
                hours_counter[hour] += 1

                text = msg.get("processed_plain_text", "")
                if len(text) >= 5:  # 收集有效发言
                    all_texts.append(text)

            # 找出最活跃的时段
            if hours_counter:
                most_active_hours = hours_counter.most_common(3)
                active_hours_list = sorted([h for h, _ in most_active_hours])
            else:
                active_hours_list = []

            # 构建聊天记录样本（最多15条）
            sample_texts = all_texts[:15] if len(all_texts) > 15 else all_texts
            chat_sample = "\n".join([f"- {text[:80]}" for text in sample_texts])

            # 构建统计信息
            total_chars = sum(len(text) for text in all_texts)
            avg_chars = total_chars / len(all_texts) if all_texts else 0
            emoji_count = sum(ChatAnalysisUtils.count_emojis(text) for text in all_texts)
            emoji_ratio = emoji_count / len(all_texts) if all_texts else 0

            # 统计时段分布
            morning = sum(hours_counter[h] for h in range(6, 12))  # 6-12点
            afternoon = sum(hours_counter[h] for h in range(12, 18))  # 12-18点
            evening = sum(hours_counter[h] for h in range(18, 24))  # 18-24点
            night = sum(hours_counter[h] for h in range(0, 6))  # 0-6点

            time_distribution = f"早{morning}条/午{afternoon}条/晚{evening}条/夜{night}条"

            # 计算更多维度的统计
            total_messages = len(messages)
            # 话题关键词（简单统计）
            words_freq = Counter()
            for text in all_texts:
                # 简单分词（按空格和标点）
                words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", text)
                words_freq.update([w for w in words if len(w) >= 2])
            top_words = ", ".join([w for w, _ in words_freq.most_common(5)])

            # 互动特征（简单判断）
            question_count = sum(1 for text in all_texts if "?" in text or "？" in text)
            question_ratio = question_count / total_messages if total_messages > 0 else 0

            # 构建 prompt
            prompt = f"""分析这个用户的聊天画像（娱乐向，有趣但不失真实）。

用户基础数据：
- 用户名：{user_name}
- 发言数：{total_messages}条
- 平均长度：{avg_chars:.1f}字/条
- 表情使用：{emoji_ratio:.2f}个/条
- 提问比例：{question_ratio:.2f}

时间特征：
- 时段分布：{time_distribution}
- 最活跃时段：{", ".join([f"{h}点" for h in active_hours_list[:3]])}

内容特征：
- 高频词：{top_words}

发言样本（最近{len(sample_texts)}条）：
{chat_sample}

要求：
1. tags: 1-3个有趣的个性标签（3-6字），基于真实数据，避免陈词滥调
   - 参考维度：时间特征（夜猫子/早起鸟）、表达风格（表情包选手/文字工匠）、互动特征（好奇宝宝/话题终结者）、情绪倾向（开心果/emo精）
   - 例如："深夜哲学家"、"表情包达人"、"提问小能手"、"简洁派发言"

2. active_time: 描述活跃时段特征（12-15字），有趣且准确
   - 例如："深夜冲浪型选手"、"朝九晚五社畜"、"全天候在线人"

3. fun_score: 整活质量评分（0-100）
   - 0-30: 发言平淡无趣，无亮点
   - 31-60: 偶尔有梗，但不够出彩
   - 61-85: 经常有有趣发言，能活跃气氛
   - 86-100: 群聊灵魂，笑点制造机

4. fun_comment: 整活质量评价（18-25字），幽默点评当天的有趣发言
   - 例如："3次神回复让群友笑喷，段子手潜质显现"
   - 或："发言质朴无华，建议多学习群友整活"

5. topic_leadership: 话题引导力（0-100）
   - 基于发言是否引发他人回复、讨论热度等
   - 0-30: 话题终结者，发言无人接
   - 31-60: 偶尔能引起讨论
   - 61-85: 经常引发话题，有带动力
   - 86-100: 群聊节奏大师，一呼百应

6. topic_comment: 话题引导力评价（18-25字）
   - 例如："发起2个热门话题，群友积极响应"
   - 或："发言自说自话，缺乏互动吸引力"

7. rank_title: 段位称号（6-10字），根据综合表现评定
   - 参考维度：发言数、质量、互动、活跃时段等
   - 例如："黄金话痨III"、"钻石夜猫I"、"青铜潜水员V"、"白银表情包II"
   - 段位等级：青铜 < 白银 < 黄金 < 铂金 < 钻石 < 大师 < 王者
   - 每个等级有I到V五个小段位

8. rank_desc: 段位描述（20-28字），说明为什么是这个段位
   - 例如："今日发言50条且质量上乘，晋升在即"
   - 或："潜水选手，发言稀少，建议多冒泡"

9. mood: 今日整体心情（积极/中性/消极）

10. mood_score: 心情分数（0-100，综合表情、用词、语气判断）
   - 0-30: 明显消极/emo
   - 31-60: 平淡/中性
   - 61-85: 积极/活跃
   - 86-100: 非常开心/兴奋

11. mood_reason: 心情评估依据（15-22字），简洁引用具体数据或特征

返回JSON（不要markdown代码块）：
{{
  "tags": ["标签1", "标签2", "标签3"],
  "active_time": "活跃时段描述",
  "fun_score": 75,
  "fun_comment": "整活质量评价",
  "topic_leadership": 68,
  "topic_comment": "话题引导力评价",
  "rank_title": "黄金话痨III",
  "rank_desc": "段位描述",
  "mood": "积极/中性/消极",
  "mood_score": 75,
  "mood_reason": "基于表情使用、用词倾向等的评估理由"
}}"""

            # 使用 LLM 生成
            model_task_config = model_config.model_task_config.replyer
            success, result, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_task_config,
                request_type="plugin.chat_summary.user_profile",
            )

            if not success:
                logger.error(f"LLM生成用户画像失败: {result}")
                return None

            # 解析 JSON
            data = ChatAnalysisUtils._parse_llm_json_object(result)
            if not data:
                return None

            # 验证并返回（添加 user_id）
            validated_data = ChatAnalysisUtils._validate_user_profile(data)
            if validated_data:
                validated_data["user_id"] = user_id
            return validated_data

        except Exception as e:
            logger.error(f"分析用户画像失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _validate_user_profile(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """验证用户画像数据

        Args:
            data: 原始数据字典

        Returns:
            验证后的数据，失败返回 None
        """
        try:
            if not isinstance(data, dict):
                return None

            # 验证必需字段
            required_fields = [
                "tags",
                "active_time",
                "fun_score",
                "fun_comment",
                "topic_leadership",
                "topic_comment",
                "rank_title",
                "rank_desc",
                "mood",
                "mood_score",
                "mood_reason",
            ]
            if not all(key in data for key in required_fields):
                logger.warning(f"用户画像数据缺少必需字段: {data}")
                return None

            # 验证并清理 tags
            tags = data.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            # 最多3个标签，每个3-6个汉字（9-18字节）
            tags = [str(tag)[:18] for tag in tags[:3] if len(str(tag).strip()) >= 3]

            # 验证其他字段
            active_time = str(data.get("active_time", ""))[:30]

            # 验证整活质量评分
            try:
                fun_score = int(data.get("fun_score", 50))
                fun_score = max(0, min(100, fun_score))
            except (ValueError, TypeError):
                fun_score = 50

            fun_comment = str(data.get("fun_comment", ""))[:60]  # 约30字

            # 验证话题引导力
            try:
                topic_leadership = int(data.get("topic_leadership", 50))
                topic_leadership = max(0, min(100, topic_leadership))
            except (ValueError, TypeError):
                topic_leadership = 50

            topic_comment = str(data.get("topic_comment", ""))[:60]  # 约30字

            # 验证段位信息
            rank_title = str(data.get("rank_title", ""))[:30]
            rank_desc = str(data.get("rank_desc", ""))[:70]  # 约35字

            mood = str(data.get("mood", "中性"))

            # 验证 mood 值
            if mood not in ["积极", "中性", "消极"]:
                mood = "中性"

            # 验证 mood_score
            try:
                mood_score = int(data.get("mood_score", 50))
                mood_score = max(0, min(100, mood_score))  # 限制在 0-100
            except (ValueError, TypeError):
                mood_score = 50

            mood_reason = str(data.get("mood_reason", ""))[:50]  # 限制为50字符（约25汉字）

            if (
                not tags
                or not active_time
                or not fun_comment
                or not topic_comment
                or not rank_title
                or not rank_desc
                or not mood_reason
            ):
                logger.warning("用户画像数据字段为空")
                return None

            return {
                "tags": tags,
                "active_time": active_time,
                "fun_score": fun_score,
                "fun_comment": fun_comment,
                "topic_leadership": topic_leadership,
                "topic_comment": topic_comment,
                "rank_title": rank_title,
                "rank_desc": rank_desc,
                "mood": mood,
                "mood_score": mood_score,
                "mood_reason": mood_reason,
            }

        except Exception as e:
            logger.error(f"验证用户画像数据失败: {e}")
            return None

    @staticmethod
    def _parse_llm_json_object(result: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 对象（非数组）"""
        try:
            import json_repair

            data = json_repair.loads(result)
            if isinstance(data, dict):
                return data
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                return data[0]
        except Exception as e:
            logger.warning(f"json_repair 解析对象失败: {e}")

        # Fallback to standard json parsing
        try:
            start_idx = result.find("{")
            end_idx = result.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                result_cleaned = result[start_idx : end_idx + 1]
            else:
                result_cleaned = result
            data = json.loads(result_cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as e:
            logger.error(f"解析 JSON 对象失败: {e}")
            logger.debug(f"原始LLM输出: {result[:500]}")
        return None

    @staticmethod
    def _parse_llm_json(result: str) -> List[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 列表"""
        try:
            import json_repair

            data = json_repair.loads(result)
            if isinstance(data, list) and all(isinstance(item, dict) for item in data):
                return data
            elif isinstance(data, dict):
                return [data]
        except Exception as e:
            logger.warning(f"json_repair 解析列表失败: {e}")

        # Fallback to standard json parsing
        try:
            start_idx = result.find("[")
            end_idx = result.rfind("]")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                result_cleaned = result[start_idx : end_idx + 1]
            else:
                result_cleaned = result
            data = json.loads(result_cleaned)
            if isinstance(data, list) and all(isinstance(item, dict) for item in data):
                return data
        except json.JSONDecodeError as e:
            logger.error(f"解析 JSON 列表失败: {e}")
            logger.debug(f"原始LLM输出: {result[:500]}")
        return []
