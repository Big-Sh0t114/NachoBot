"""
日记服务模块（供 Action 与 Command 共同调用）

封装日记生成核心流程：
- 获取人设
- 构建时间线（含图片）
- Token 估算与截断（默认50k限制可选）
- 生成天气与日期串
- 选择模型生成内容（自定义/默认）
- 智能截断与保存
- QQ空间发布
"""

import datetime
import random
import time
from typing import Any, Dict, List, Tuple

from openai import AsyncOpenAI
from .utils import get_bot_personality,DiaryConstants
from src.plugin_system.apis import config_api, llm_api, get_logger

from .storage import DiaryStorage, DiaryQzoneAPI


logger = get_logger("diary_service")


class DiaryService:
    def __init__(self, plugin_config: Dict[str, Any] | None = None) -> None:
        self.plugin_config = plugin_config or {}
        self.storage = DiaryStorage()
        self.qzone_api = DiaryQzoneAPI()

    # ===================== 配置访问 =====================
    def get_config(self, key: str, default=None):
        if not self.plugin_config:
            return default
        keys = key.split(".")
        current = self.plugin_config
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    def build_chat_timeline(self, messages: List[Any]) -> str:
        if not messages:
            return "今天没有什么特别的对话。"
        timeline_parts: List[str] = []
        current_hour = -1
        bot_qq_account = str(config_api.get_global_config("bot.qq_account", ""))

        from .image_processor import ImageProcessor
        image_processor = ImageProcessor()

        bot_message_count = 0
        user_message_count = 0

        for msg in messages:
            msg_time = datetime.datetime.fromtimestamp(msg.time)
            hour = msg_time.hour
            if hour != current_hour:
                if 6 <= hour < 12:
                    time_period = f"上午{hour}点"
                elif 12 <= hour < 18:
                    time_period = f"下午{hour}点"
                else:
                    time_period = f"晚上{hour}点"
                timeline_parts.append(f"\n【{time_period}】")
                current_hour = hour

            nickname = msg.user_info.user_nickname or '某人'
            user_id = str(msg.user_info.user_id)

            if image_processor._is_image_message(msg):
                description = image_processor._get_image_description(msg)
                if user_id == bot_qq_account:
                    timeline_parts.append(f"我: [图片]{description}")
                    bot_message_count += 1
                else:
                    timeline_parts.append(f"{nickname}: [图片]{description}")
                    user_message_count += 1
            else:
                content = msg.processed_plain_text or ''
                if content and len(content) > 50:
                    content = content[:50] + "..."
                if user_id == bot_qq_account:
                    timeline_parts.append(f"我: {content}")
                    bot_message_count += 1
                else:
                    timeline_parts.append(f"{nickname}: {content}")
                    user_message_count += 1

        self._timeline_stats = {
            "total_messages": len(messages),
            "bot_messages": bot_message_count,
            "user_messages": user_message_count,
        }
        return "\n".join(timeline_parts)

    # ===================== Token估算与截断 =====================
    def _estimate_tokens(self, text: str) -> int:
        import re
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    def estimate_token_count(self, text: str) -> int:
        return self._estimate_tokens(text)

    def _truncate_messages(self, timeline: str, max_tokens: int) -> str:
        current_tokens = self._estimate_tokens(timeline)
        if current_tokens <= max_tokens:
            return timeline
        ratio = max_tokens / current_tokens
        target_length = int(len(timeline) * ratio * 0.95)
        truncated = timeline[:target_length]
        for i in range(len(truncated) - 1, len(truncated) // 2, -1):
            if truncated[i] in ['。', '！', '？', '\n']:
                truncated = truncated[: i + 1]
                break
        logger.info(f"时间线截断: {current_tokens}→{self._estimate_tokens(truncated)} tokens")
        return truncated + "\n\n[聊天记录过长,已截断]"

    def truncate_timeline_by_tokens(self, timeline: str, max_tokens: int) -> str:
        return self._truncate_messages(timeline, max_tokens)

    def smart_truncate(self, text: str, max_length: int = DiaryConstants.MAX_DIARY_LENGTH) -> str:
        if len(text) <= max_length:
            return text
        for i in range(max_length - 3, max_length // 2, -1):
            if text[i] in ['。', '！', '？', '~']:
                return text[: i + 1]
        return text[: max_length - 3] + "..."

    # ===================== 天气与日期 =====================
    def get_weather_by_emotion(self, messages: List[Any]) -> str:
        if not messages:
            return random.choice(["晴", "多云", "阴", "多云转晴"])
        all_content = " ".join([(msg.processed_plain_text or '') for msg in messages])
        happy_words = ["哈哈", "笑", "开心", "高兴", "棒", "好", "赞", "爱", "喜欢"]
        sad_words = ["难过", "伤心", "哭", "痛苦", "失望"]
        angry_words = ["无语", "醉了", "服了", "烦", "气", "怒"]
        calm_words = ["平静", "安静", "淡定", "还好", "一般"]
        happy_count = sum(1 for w in happy_words if w in all_content)
        sad_count = sum(1 for w in sad_words if w in all_content)
        angry_count = sum(1 for w in angry_words if w in all_content)
        calm_count = sum(1 for w in calm_words if w in all_content)
        if happy_count >= 2:
            return "晴"
        elif happy_count >= 1:
            return "多云转晴"
        elif sad_count >= 1:
            return "雨"
        elif angry_count >= 1:
            return "阴"
        elif calm_count >= 1:
            return "多云"
        else:
            return "多云"

    def get_date_with_weather(self, date: str, weather: str) -> str:
        try:
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            weekday = weekdays[date_obj.weekday()]
            return f"{date_obj.year}年{date_obj.month}月{date_obj.day}日,{weekday},{weather}。"
        except Exception:
            return f"{date},{weather}。"

    # ===================== 生成与发布 =====================
    async def _generate_with_custom_model(self, prompt: str) -> Tuple[bool, str]:
        try:
            api_key = self.get_config("custom_model.api_key", "")
            if not api_key or api_key == "sk-your-siliconflow-key-here":
                return False, "自定义模型API密钥未配置"
            client = AsyncOpenAI(
                base_url=self.get_config("custom_model.api_url", "https://api.siliconflow.cn/v1"),
                api_key=api_key,
            )
            api_timeout = self.get_config("custom_model.api_timeout", 300)
            if not (1 <= api_timeout <= 6000):
                api_timeout = 300
            completion = await client.chat.completions.create(
                model=self.get_config("custom_model.model_name", "Pro/deepseek-ai/DeepSeek-V3"),
                messages=[{"role": "user", "content": prompt}],
                temperature=self.get_config("custom_model.temperature", 0.7),
                timeout=api_timeout,
            )
            if completion.choices and len(completion.choices) > 0:
                content = completion.choices[0].message.content
            else:
                return False, "模型返回的响应为空"
            return True, content
        except Exception as e:
            logger.error(f"自定义模型调用失败: {e}")
            return False, f"自定义模型调用出错: {str(e)}"

    async def _generate_with_default_model(self, prompt: str, timeline: str) -> Tuple[bool, str]:
        try:
            max_tokens = DiaryConstants.TOKEN_LIMIT_50K
            current_tokens = self._estimate_tokens(timeline)
            if current_tokens > max_tokens:
                truncated = self._truncate_messages(timeline, max_tokens)
                prompt = prompt.replace(timeline, truncated)
            models = llm_api.get_available_models()
            model = models.get("replyer")
            if not model:
                return False, "未找到默认模型: replyer"
            success, diary_content, _, _ = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model,
                request_type="plugin.diary_generation",
            )
            if not success or not diary_content:
                return False, "默认模型生成日记失败"
            return True, diary_content
        except Exception as e:
            logger.error(f"默认模型调用失败: {e}")
            return False, f"默认模型调用出错: {str(e)}"

    async def generate_diary_from_messages(
        self,
        date: str,
        messages: List[Any],
        force_50k: bool = True,
    ) -> Tuple[bool, str]:
        try:
            personality = await get_bot_personality()
            timeline = self.build_chat_timeline(messages)

            if force_50k:
                max_tokens = DiaryConstants.TOKEN_LIMIT_50K
                current_tokens = self.estimate_token_count(timeline)
                if current_tokens > max_tokens:
                    timeline = self.truncate_timeline_by_tokens(timeline, max_tokens)

            weather = self.get_weather_by_emotion(messages)
            date_with_weather = self.get_date_with_weather(date, weather)

            # 读取字数配置：仅使用[min,max]随机
            min_wc = self.get_config("qzone_publishing.qzone_min_word_count", 250)
            max_wc = self.get_config("qzone_publishing.qzone_max_word_count", 350)
            # 规范范围并做上限保护
            if not isinstance(min_wc, int):
                min_wc = 250
            if not isinstance(max_wc, int):
                max_wc = 350
            if min_wc < 20:
                min_wc = 20
            if max_wc > DiaryConstants.MAX_DIARY_LENGTH:
                max_wc = DiaryConstants.MAX_DIARY_LENGTH
            if max_wc < min_wc:
                max_wc = min_wc
            target_length = random.randint(min_wc, max_wc)

            personality_desc = personality["core"]
            name = f"\n我的名字是{personality['nickname']}" if personality.get("nickname") else ""

            style = self.get_config("diary_generation.style", "diary")
            if style == "custom":
                template = self.get_config("diary_generation.custom_prompt", "") or ""
                context = {
                    "date": date,
                    "timeline": timeline,
                    "date_with_weather": date_with_weather,
                    "target_length": target_length,
                    "personality_desc": personality_desc,
                    "style": personality.get("style", ""),
                    "interest": personality.get("interest", ""),
                    "name": name,
                }
                try:
                    prompt = template.format(**context)
                    if not prompt.strip():
                        raise ValueError("empty custom prompt")
                except Exception:
                    style = "diary"
            if style == "qqzone":
                prompt = f"""{name}
我{personality_desc}
今天日期与天气是：{date_with_weather}
今天看到了一些聊天内容，其中也有我自己的发言：
{timeline}

阅读完这些记录，请用大约{target_length}字写一条适合QQ空间的说说：
随便挑一个喜欢的主题，围绕这个主题写。
你需要写的日常且口语化的文段，平淡一些，就像微博和贴吧的风格
遣词造句尽量简短一些。请注意把握聊天内容，不要书写的太有条理，可以有个性。
{personality['style']}
请注意不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只输出一段说说内容就好。
说说内容:"""
            elif style == "diary":
                prompt = f"""{name}
我{personality_desc}

今天是{date},回顾一下到现在为止的聊天记录:
{timeline}

现在我要写一篇{target_length}字左右的日记,记录到现在为止的感受:
1. 开头必须是日期和天气:{date_with_weather}
2. 像睡前随手写的感觉,轻松自然
3. 回忆到现在为止的对话,加入我的真实感受
4. 如果有有趣的事就重点写,平淡的一天就简单记录
5. 偶尔加一两句小总结或感想
6. 不要写成流水账,要有重点和感情色彩
7. 用第一人称"我"来写

书写风格：
你需要写的日常且口语化的文段，平淡一些
遣词造句尽量简短一些。请注意把握聊天内容，不要书写的太有条理，可以有个性。
{personality['style']}
请注意不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只输出一段日记内容就好。
不要输出多余内容(包括前后缀，冒号和引号，括号，表情包，at或 @等 )。
日记内容:"""

            use_custom_model = self.get_config("custom_model.use_custom_model", False)
            if use_custom_model:
                success, diary_content = await self._generate_with_custom_model(prompt)
            else:
                success, diary_content = await self._generate_with_default_model(prompt, timeline)

            if not success or not diary_content:
                return False, diary_content or "模型生成日记失败"

            # 截断上限：仅使用最大上限
            max_length = self.get_config("qzone_publishing.qzone_max_word_count", 350)
            if not isinstance(max_length, int):
                max_length = 350
            if max_length > DiaryConstants.MAX_DIARY_LENGTH:
                max_length = DiaryConstants.MAX_DIARY_LENGTH
            if len(diary_content) > max_length:
                diary_content = self.smart_truncate(diary_content, max_length)

            diary_record = {
                "date": date,
                "diary_content": diary_content,
                "word_count": len(diary_content),
                "generation_time": time.time(),
                "weather": weather,
                "bot_messages": getattr(self, "_timeline_stats", {}).get("bot_messages", 0),
                "user_messages": getattr(self, "_timeline_stats", {}).get("user_messages", 0),
                "is_published_qzone": False,
                "qzone_publish_time": None,
                "status": "生成成功",
                "error_message": "",
            }
            await self.storage.save_diary(diary_record)
            return True, diary_content
        except Exception as e:
            logger.error(f"生成日记失败: {e}")
            try:
                failed_record = {
                    "date": date,
                    "diary_content": "",
                    "word_count": 0,
                    "generation_time": time.time(),
                    "weather": "阴",
                    "bot_messages": 0,
                    "user_messages": 0,
                    "is_published_qzone": False,
                    "qzone_publish_time": None,
                    "status": "报错:生成失败",
                    "error_message": f"原因:{str(e)}",
                }
                await self.storage.save_diary(failed_record)
            except Exception:
                pass
            return False, f"生成日记时出错: {str(e)}"

    async def publish_to_qzone(self, date: str, diary_content: str) -> bool:
        try:
            napcat_host = self.get_config("qzone_publishing.napcat_host", "127.0.0.1")
            napcat_port = self.get_config("qzone_publishing.napcat_port", "9998")
            napcat_token = self.get_config("qzone_publishing.napcat_token", "")
            success = await self.qzone_api.publish_diary(diary_content, napcat_host, napcat_port, napcat_token)

            diary_data = await self.storage.get_diary(date)
            if diary_data:
                if success:
                    diary_data["is_published_qzone"] = True
                    diary_data["qzone_publish_time"] = time.time()
                    diary_data["status"] = "一切正常"
                    diary_data["error_message"] = ""
                else:
                    diary_data["is_published_qzone"] = False
                    diary_data["status"] = "报错:发说说失败"
                    diary_data["error_message"] = "原因:QQ空间发布失败,可能是cookie过期或网络问题"
                await self.storage.save_diary(diary_data)
            return success
        except Exception as e:
            logger.error(f"发布QQ空间失败: {e}")
            diary_data = await self.storage.get_diary(date)
            if diary_data:
                diary_data["is_published_qzone"] = False
                diary_data["status"] = "报错:发说说失败"
                diary_data["error_message"] = f"原因:发布异常 - {str(e)}"
                await self.storage.save_diary(diary_data)
            return False

