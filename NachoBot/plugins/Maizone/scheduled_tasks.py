import asyncio
import time
import random
import datetime
import traceback
import os
import json
from typing import List, Dict
from pathlib import Path

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api, config_api, person_api

from .qzone_api import QzoneAuthError, create_qzone_api
from .cookie_manager import renew_cookies
from .utils import monitor_read_feed, reply_feed, comment_feed, like_feed, send_feed

logger = get_logger("Maizone.定时任务")


# ===== 定时任务功能 =====
def _save_processed_list(processed_list: Dict[str, List[str]]):
    """保存已处理说说及评论字典到文件"""
    try:
        file_path = str(Path(__file__).parent.resolve() / "processed_list.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(processed_list, f, ensure_ascii=False, indent=2)
            logger.debug("已保存已处理说说列表")
    except Exception as e:
        logger.error(f"保存已处理评论失败: {str(e)}")


def _load_processed_list() -> Dict[str, List[str]]:
    """从文件加载已处理说说及评论字典"""
    file_path = str(Path(__file__).parent.resolve() / "processed_list.json")

    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                logger.debug("正在加载已处理说说列表")
                return json.load(f)
        except Exception as e:
            logger.error(f"加载已处理评论失败: {str(e)}")
            return {}
    logger.warning("未找到已处理评论列表，将创建新列表")
    return {}


class FeedMonitor:
    """定时监控好友说说的类"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.last_check_time = 0

    async def start(self):
        """启动监控任务"""
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._monitor_loop())
        logger.info("说说监控任务已启动")

    async def stop(self):
        """停止监控任务"""
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("说说监控任务已停止")

    async def _monitor_loop(self):
        """监控循环"""
        # 获取配置
        interval = self.plugin.get_config("monitor.interval_minutes", 5)
        # 记录已处理评论，说说id映射已处理评论列表
        processed_list = _load_processed_list()
        while self.is_running:
            try:
                # 等待指定时间
                await asyncio.sleep(interval * 60)
                # 执行监控任务
                await self.check_feeds(processed_list)
                # 保存已处理评论到文件
                _save_processed_list(processed_list)
            except asyncio.CancelledError:
                _save_processed_list(processed_list)
                break
            except Exception as e:
                logger.error(f"监控任务出错: {str(e)}")
                _save_processed_list(processed_list)
                traceback.print_exc()
                # 出错后等待一段时间再重试
                await asyncio.sleep(300)

    async def check_feeds(self, processed_comments: Dict[str, List[str]]):
        """检查空间说说并回复未读说说和评论"""

        qq_account = config_api.get_global_config("bot.qq_account", "")
        show_prompt = self.plugin.get_config("models.show_prompt", False)
        # 模型配置
        models = llm_api.get_available_models()
        text_model = self.plugin.get_config("models.text_model", "replyer")
        model_config = models[text_model]
        if not model_config:
            return False, "未配置LLM模型"

        bot_personality = config_api.get_global_config("personality.personality", "一个机器人")
        bot_expression = config_api.get_global_config("personality.reply_style", "内容积极向上")

        # 更新cookies
        try:
            await renew_cookies()
        except Exception as e:
            logger.error(f"更新cookies失败: {str(e)}")
            return False, "更新cookies失败"

        try:
            logger.info("监控任务: 正在获取说说列表")
            feeds_list = await monitor_read_feed()
        except QzoneAuthError as e:
            logger.error(f"QQ空间登录态失效: {str(e)}")
            return False, "QQ空间登录态失效"
        except Exception as e:
            logger.error(f"获取说说列表失败: {str(e)}")
            return False, "获取说说列表失败"
            # 逐条点赞回复
        try:
            if len(feeds_list) == 0:
                logger.info("未读取到新说说")
                return True, "success"
            for feed in feeds_list:
                await asyncio.sleep(3 + random.random())
                content = feed["content"]
                if feed["images"]:
                    for image in feed["images"]:
                        content = content + image
                fid = feed["tid"]
                target_qq = feed["target_qq"]
                rt_con = feed.get("rt_con", "")
                comments_list = feed["comments"]
                # 回复自己的说说评论
                if target_qq == qq_account:
                    enable_auto_reply = self.plugin.get_config("monitor.enable_auto_reply", False)
                    if not enable_auto_reply:
                        continue
                    # 获取未回复的评论
                    list_to_reply = []  # 待回复的评论
                    if comments_list:
                        for comment in comments_list:
                            comment_qq = comment.get("qq_account", "")
                            if int(comment_qq) != int(qq_account):  # 只考虑不是自己的评论
                                if comment["comment_tid"] not in processed_comments.get(
                                    fid, []
                                ):  # 只考虑未处理过的评论
                                    list_to_reply.append(comment)  # 添加到待回复列表
                                    processed_comments.setdefault(fid, []).append(
                                        comment["comment_tid"]
                                    )  # 记录到已处理评论
                                    while len(processed_comments) > 100:
                                        # 为防止字典无限增长，限制字典大小
                                        oldest_fid = next(iter(processed_comments))
                                        processed_comments.pop(oldest_fid)

                    if len(list_to_reply) == 0:
                        continue
                    for comment in list_to_reply:
                        # 逐条回复评论
                        user_id = comment["qq_account"]
                        person_id = person_api.get_person_id("qq", user_id)
                        impression = await person_api.get_person_value(person_id, "memory_points", ["无"])
                        prompt = f"""
                        你是'{bot_personality}'，你的好友'{comment["nickname"]}'评论了你QQ空间上的一条内容为“{content}”说说，
                        你的好友对该说说的评论为:“{comment["content"]}”，你想要对此评论进行回复，
                        你对该好友的印象是:{impression}，若与你的印象点相关，可以适当回复相关内容，无关则忽略此印象，
                        {bot_expression}，回复的平淡一些，简短一些，说中文，
                        不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容
                        """
                        logger.info(f"正在回复{comment['nickname']}的评论：{comment['content']}...")

                        if show_prompt:
                            logger.info(f"回复评论prompt内容：{prompt}")

                        from src.plugin_system.apis.send_api import should_filter_text

                        success, reply, reasoning, model_name = await llm_api.generate_with_filter_retry(
                            prompt=prompt,
                            model_config=model_config,
                            filter_func=should_filter_text,
                            retry_count=3,
                            request_type="story.generate",
                            temperature=0.3,
                            max_tokens=4096,
                        )

                        if not success:
                            return False, "生成回复内容失败"

                        logger.info(f"正在回复{comment['nickname']}的评论：{comment['content']}...")

                        await renew_cookies()
                        success = await reply_feed(fid, target_qq, comment["nickname"], reply, comment["comment_tid"])
                        if not success:
                            logger.error(f"回复评论{comment['content']}失败")
                            return False, "回复评论失败"
                        logger.info(f"发送回复'{reply}'成功")
                        await asyncio.sleep(5 + random.random() * 5)
                    continue
                # 评论他人说说
                if fid in processed_comments:
                    # 该说说已处理过，跳过
                    continue
                person_id = person_api.get_person_id("qq", target_qq)
                impression = await person_api.get_person_value(person_id, "memory_points", ["无"])
                if not rt_con:
                    prompt = f"""
                    你是'{bot_personality}'，你正在浏览你好友'{target_qq}'的QQ空间，
                    你看到了你的好友'{target_qq}'qq空间上内容是'{content}'的说说，你想要发表你的一条评论，
                    你对该好友的印象是:{impression}，若与你的印象点相关，可以适当回复相关内容，无关则忽略此印象，
                    {bot_expression}，回复的平淡一些，简短一些，说中文，
                    不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容
                    """
                else:
                    prompt = f"""
                    你是'{bot_personality}'，你正在浏览你好友'{target_qq}'的QQ空间，
                    你看到了你的好友'{target_qq}'在qq空间上转发了一条内容为'{rt_con}'的说说，你的好友的评论为'{content}'，
                    你对该好友的印象是:{impression}，若与你的印象点相关，可以适当回复相关内容，无关则忽略此印象，
                    你想要发表你的一条评论，{bot_expression}，回复的平淡一些，简短一些，说中文，
                    不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容
                    """
                logger.info(f"正在评论'{target_qq}'的说说：{content[:30]}...")

                if show_prompt:
                    logger.info(f"评论说说prompt内容：{prompt}")

                from src.plugin_system.apis.send_api import should_filter_text

                success, comment, reasoning, model_name = await llm_api.generate_with_filter_retry(
                    prompt=prompt,
                    model_config=model_config,
                    filter_func=should_filter_text,
                    retry_count=3,
                    request_type="story.generate",
                    temperature=0.3,
                    max_tokens=4096,
                )

                if not success:
                    return False, "生成评论内容失败"

                logger.info(f"成功生成评论内容：'{comment}'，即将发送")

                success = await comment_feed(target_qq, fid, comment)
                if not success:
                    logger.error(f"评论说说{content}失败")
                    return False, "评论说说失败"
                logger.info(f"发送评论'{comment}'成功")
                # 点赞说说
                success = await like_feed(target_qq, fid)
                if not success:
                    logger.error(f"点赞说说{content}失败")
                    return False, "点赞说说失败"
                logger.info(f"点赞说说{content[:10]}..成功")
                # 记录该说说已处理
                processed_comments[fid] = []
                while len(processed_comments) > 100:
                    # 为防止字典无限增长，限制字典大小
                    oldest_fid = next(iter(processed_comments))
                    processed_comments.pop(oldest_fid)
            return True, "success"
        except Exception as e:
            logger.error(f"点赞评论失败: {str(e)}")
            return False, "点赞评论失败"


class ScheduleSender:
    """定时发送说说的类"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.last_send_time = 0
        self.fluctuate_table = []  # 记录波动后的发送时间表
        self.last_reset_date = None  # 记录上次重置发送时间表日期

    async def start(self):
        """启动定时发送任务"""
        if self.is_running:
            return
        self.is_running = True
        self.last_reset_date = datetime.datetime.now().date()
        self._generate_fluctuate_table()  # 生成时间表
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("定时发送说说任务已启动")

    async def stop(self):
        """停止定时发送任务"""
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("定时发送说说任务已停止")

    def _generate_fluctuate_table(self):
        """生成随机波动时间表"""
        schedule_times = self.plugin.get_config("schedule.schedule_times", ["08:00", "20:00"])
        fluctuate_minutes = self.plugin.get_config("schedule.fluctuation_minutes", 0)

        # 清空当前波动表
        self.fluctuate_table = []

        # 如果波动分钟数为0，直接使用计划时间
        if fluctuate_minutes == 0:
            self.fluctuate_table = schedule_times.copy()
            self.fluctuate_table.sort()
            logger.info(f"无波动，使用计划时间: {self.fluctuate_table}")
            return

        # 为每个计划时间生成一个随机波动时间
        for base_time in schedule_times:
            # 解析基础时间
            base_hour, base_minute = map(int, base_time.split(":"))
            base_total_minutes = base_hour * 60 + base_minute
            # 生成随机偏移量
            offset = random.randint(-fluctuate_minutes, fluctuate_minutes)
            # 处理溢出
            total_minutes = base_total_minutes + offset
            if total_minutes < 0:
                total_minutes += 24 * 60
            elif total_minutes >= 24 * 60:
                total_minutes -= 24 * 60
            # 转换回时间格式
            h = total_minutes // 60
            m = total_minutes % 60
            fluctuate_time = f"{h:02d}:{m:02d}"
            # 添加到波动表
            if fluctuate_time not in self.fluctuate_table:
                self.fluctuate_table.append(fluctuate_time)

        # 按时间排序波动表
        self.fluctuate_table.sort()
        logger.info(f"波动后的发送时间表: {self.fluctuate_table}")

    def _should_reset_schedule(self):
        """检查是否需要重置时间表（每天0点）"""
        current_date = datetime.datetime.now().date()
        return current_date != self.last_reset_date

    async def _schedule_loop(self):
        """定时发送循环"""
        while self.is_running:
            try:
                # 检查是否需要重置时间表（每天0点）
                if self._should_reset_schedule():
                    logger.info("检测到日期变化，重置发送时间表")
                    self.last_reset_date = datetime.datetime.now().date()
                    self._generate_fluctuate_table()
                # 获取当前时间
                current_time = datetime.datetime.now().strftime("%H:%M")
                # 检查是否到达发送时间
                if current_time in self.fluctuate_table:
                    # 避免同一分钟内重复发送
                    if time.time() - self.last_send_time > 60:
                        logger.info("正在发送定时说说...")
                        self.last_send_time = time.time()
                        await self.send_scheduled_feed()
                        self.fluctuate_table.remove(current_time)
                        logger.info(f"剩余发送时间点: {self.fluctuate_table}")

                # 每分钟检查一次
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时发送任务出错: {str(e)}")
                await asyncio.sleep(60)  # 出错后等待一分钟再继续

    async def send_scheduled_feed(self):
        """发送定时说说"""
        # 模型配置
        models = llm_api.get_available_models()
        text_model = self.plugin.get_config("models.text_model", "replyer")
        model_config = models[text_model]
        if not model_config:
            logger.error("未配置LLM模型")
            return

        # 获取主题设置
        random_topic = self.plugin.get_config("schedule.random_topic", True)
        fixed_topics = self.plugin.get_config("schedule.fixed_topics", ["日常生活", "心情分享", "有趣见闻"])
        # 人格配置
        bot_personality = config_api.get_global_config("personality.personality", "一个蓝发猫娘")
        bot_expression = config_api.get_global_config("personality.reply_style", "内容积极向上")
        # 核心配置
        # 生成图片相关配置
        image_dir = str(Path(__file__).parent.resolve() / "images")
        enable_image = self.plugin.get_config("send.enable_image", True)
        apikey = self.plugin.get_config("models.api_key", "")
        image_mode = self.plugin.get_config("send.image_mode", "random").lower()
        ai_probability = self.plugin.get_config("send.ai_probability", 0.5)
        image_number = self.plugin.get_config("send.image_number", 1)
        # 说说生成相关配置
        history_number = self.plugin.get_config("send.history_number", 5)
        # 更新cookies
        try:
            await renew_cookies()
        except Exception as e:
            logger.error(f"更新cookies失败: {str(e)}")
            return
        qzone = create_qzone_api()
        # 生成说说内容
        if random_topic:
            prompt_pre = self.plugin.get_config("send.prompt", "")
            data = {"bot_personality": bot_personality, "bot_expression": bot_expression, "topic": "随机"}
            prompt = prompt_pre.format(**data)
        else:
            fixed_topic = random.choice(fixed_topics)
            prompt_pre = self.plugin.get_config("send.prompt", "")
            data = {"bot_personality": bot_personality, "bot_expression": bot_expression, "topic": fixed_topic}
            prompt = prompt_pre.format(**data)

        prompt += "\n以下是你最近发过的说说，写新说说时注意不要在相隔不长的时间发送相似内容的说说\n"
        prompt += await qzone.get_send_history(history_number)
        prompt += "\n只输出一条说说正文的内容，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )"

        show_prompt = self.plugin.get_config("models.show_prompt", False)
        if show_prompt:
            logger.info(f"生成说说prompt内容：{prompt}")

        from src.plugin_system.apis.send_api import should_filter_text

        success, story, reasoning, model_name = await llm_api.generate_with_filter_retry(
            prompt=prompt,
            model_config=model_config,
            filter_func=should_filter_text,
            retry_count=3,
            request_type="story.generate",
            temperature=0.7,
            max_tokens=2048,
        )

        if not success:
            return False, "生成说说内容失败", True

        logger.info(f"成功生成说说内容：'{story}'")
        # 检查apikey
        if image_mode != "only_emoji" and not apikey:
            logger.warning("未配置API密钥，无法使用AI生成图片，将改为only_emoji模式")
            image_mode = "only_emoji"  # 如果没有apikey，则只使用表情包

        # 发送说说
        success = await send_feed(story, image_dir, enable_image, image_mode, ai_probability, image_number)
        if success:
            logger.info(f"定时任务成功发送说说: {story}")
        else:
            logger.error("定时任务发送说说失败")
