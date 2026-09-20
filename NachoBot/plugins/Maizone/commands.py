from typing import Optional
from pathlib import Path

from src.plugin_system import BaseCommand
from src.plugin_system.apis import llm_api, config_api
from src.common.logger import get_logger

from .qzone_api import create_qzone_api
from .cookie_manager import renew_cookies
from .utils import send_feed

logger = get_logger("Maizone.commands")


class SendFeedCommand(BaseCommand):
    """发说说Command - 响应#send_post命令"""

    command_name = "send_post"
    command_description = "发一条说说"

    command_pattern = r"^#send_post(?:\s+(?P<topic>\S[\s\S]*))?$"
    command_help = "发一条指定内容或随机的说说"
    command_examples = ["#send_post", "#send_post 写一份说说来祝大家新年快乐"]
    intercept_message = True

    def check_permission(self, qq_account: str) -> bool:
        """检查qq号为qq_account的用户是否拥有权限"""
        from src.chat.advanced.advanced_manager import advanced_manager

        return advanced_manager.is_allowed(qq_account)

    async def execute(self) -> tuple[bool, Optional[str], bool]:
        # 权限检查
        user_id = self.message.message_info.user_info.user_id
        if not self.is_force_command() and not self.check_permission(user_id):
            logger.info(f"{user_id}无{self.command_name}权限")
            await self.send_text("现在的关系还不能使用此指令哦~")
            return False, "现在的关系还不能使用此指令哦~", True
        else:
            logger.info(f"{user_id}拥有{self.command_name}权限")

        topic = self.matched_groups.get("topic")
        models = llm_api.get_available_models()
        text_model = self.get_config("models.text_model", "replyer_1")
        model_config = models[text_model]
        if not model_config:
            return False, "未配置LLM模型", True
        # 人格配置
        bot_personality = config_api.get_global_config("personality.personality", "一个机器人")
        bot_expression = config_api.get_global_config("personality.reply_style", "内容积极向上")
        # 生成图片相关配置
        enable_image = self.get_config("send.enable_image", "true")
        image_dir = str(Path(__file__).parent.resolve() / "images")
        apikey = self.get_config("models.api_key", "")
        image_mode = self.get_config("send.image_mode", "random").lower()
        ai_probability = self.get_config("send.ai_probability", 0.5)
        image_number = self.get_config("send.image_number", 1)
        # 说说生成相关配置
        history_number = self.get_config("send.history_number", 5)

        # 更新cookies
        try:
            await renew_cookies()
        except Exception as e:
            logger.error(f"更新cookies失败: {str(e)}")
            return False, "更新cookies失败", True
        qzone = create_qzone_api()
        prompt_pre = self.get_config("send.prompt", "")
        if topic:
            data = {"bot_personality": bot_personality, "topic": topic, "bot_expression": bot_expression}
            prompt = prompt_pre.format(**data)
        else:
            data = {"bot_personality": bot_personality, "bot_expression": bot_expression, "topic": "随机"}
            prompt = prompt_pre.format(**data)

        prompt += "\n以下是你以前发过的说说，写新说说时注意不要在相隔不长的时间发送相同主题的说说]\n"
        prompt += await qzone.get_send_history(history_number)
        prompt += "\n不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )"

        show_prompt = self.get_config("models.show_prompt", False)
        if show_prompt:
            logger.info(f"生成说说prompt内容：{prompt}")

        from src.plugin_system.apis.send_api import should_filter_text

        success, story, reasoning, model_name = await llm_api.generate_with_filter_retry(
            prompt=prompt,
            model_config=model_config,
            filter_func=should_filter_text,
            retry_count=3,
            request_type="story.generate",
            temperature=0.3,
            max_tokens=4096,
        )

        if not success:
            return False, story, True  # story here contains error message from llm_api

        logger.info(f"成功生成说说内容：'{story}'")

        if image_mode != "only_emoji" and not apikey:
            logger.warning("未配置apikey，无法生成图片，将只使用表情包")
            image_mode = "only_emoji"  # 如果没有apikey，则只使用表情包

        # 发送说说
        success = await send_feed(story, image_dir, enable_image, image_mode, ai_probability, image_number)
        if not success:
            return False, "发送说说失败", True
        await self.send_text(f"已发送说说：\n{story}")
        return True, "success", True
