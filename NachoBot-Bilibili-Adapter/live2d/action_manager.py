import asyncio
import json
from typing import Dict, Any, Optional
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.config.config import global_config
from src.chat.utils.chat_message_builder import (
    build_readable_messages,
    get_raw_msg_by_timestamp_with_chat_inclusive,
)
from src.chat.message_receive.message import MessageRecv

DEFAULT_BODY_CODE = {
    "双手背后向前弯腰": "010_0070",
    "歪头双手合十": "010_0100",
    "标准文静站立": "010_0101",
    "双手交叠腹部站立": "010_0150",
    "帅气的姿势": "010_0190",
    "另一个帅气的姿势": "010_0191",
    "手掌朝前可爱": "010_0210",
    "平静，双手后放": "平静，双手后放",
    "思考": "思考",
    "优雅，左手放在腰上": "优雅，左手放在腰上",
    "一般": "一般",
    "可爱，双手前放": "可爱，双手前放",
}


class ChatAction:
    def __init__(self, chat_id: str, manager):
        self.chat_id = chat_id
        self.manager = manager
        self.body_action = "一般"
        self.head_action = "注视摄像机"
        self.last_change_time = 0

    async def _call_llm(self, prompt: str) -> Optional[str]:
        # Helper to call LLM via Controller -> Adapter -> ModelClient
        model_client = self.manager.controller.adapter.model_client
        if not model_client:
            self.manager.logger.warning("ModelClient not available")
            return None
        return await model_client.call_planner(prompt)

    async def send_action_update(self):
        body_code = DEFAULT_BODY_CODE.get(self.body_action, "")
        if body_code:
            await self.manager.controller.send_live2d_event("body_action", body_code)

    async def update_action_by_message(self, message: MessageRecv):
        message_time = message.message_info.time
        timestamp_start = self.last_change_time
        timestamp_end = message_time

        try:
            # Logic similar to mood_manager to get history
            message_list = get_raw_msg_by_timestamp_with_chat_inclusive(
                chat_id=self.chat_id,
                timestamp_start=timestamp_start,
                timestamp_end=timestamp_end,
                limit=10,
                limit_mode="last",
            )
        except Exception as e:
            self.manager.logger.error(f"Failed to get messages: {e}")
            return

        chat_talking_prompt = build_readable_messages(
            message_list,
            replace_bot_name=True,
            timestamp_mode="normal_no_YMD",
            read_mark=0.0,
            truncate=True,
            show_actions=True,
        )

        bot_name = global_config.bot.nickname
        bot_nickname = (
            f",也有人叫你{','.join(global_config.bot.alias_names)}"
            if global_config.bot.alias_names
            else ""
        )
        prompt_personality = global_config.personality.personality
        indentify_block = (
            f"你的名字是{bot_name}{bot_nickname}，你{prompt_personality}："
        )

        all_actions = "\n".join([f"- {k}" for k in DEFAULT_BODY_CODE.keys()])

        prompt = await global_prompt_manager.format_prompt(
            "change_action_prompt",
            chat_talking_prompt=chat_talking_prompt,
            indentify_block=indentify_block,
            body_action=self.body_action,
            all_actions=all_actions,
        )

        response = await self._call_llm(prompt)

        if response:
            # Parse JSON
            try:
                if "```json" in response:
                    response = response.split("```json")[1].split("```")[0]
                elif "```" in response:
                    response = response.split("```")[1].split("```")[0]

                data = json.loads(response)
                new_action = data.get("body_action")
                if new_action and new_action in DEFAULT_BODY_CODE:
                    self.body_action = new_action
                    await self.send_action_update()
                    self.last_change_time = message_time
            except Exception as e:
                self.manager.logger.error(f"Failed to parse action response: {e}")


class ActionManager:
    def __init__(self, controller):
        self.controller = controller
        self.logger = controller.logger
        self.action_state_list = []
        self._init_prompts()

    def _init_prompts(self):
        Prompt(
            """
{chat_talking_prompt}
以上是群里正在进行的聊天记录

{indentify_block}
你现在的动作状态是：
- 身体动作：{body_action}

现在，因为你发送了消息，或者群里其他人发送了消息，引起了你的注意，你对其进行了阅读和思考，请你更新你的动作状态。
身体动作可选：
{all_actions}

请只按照以下json格式输出，描述你新的动作状态，确保每个字段都存在：
{{
  "body_action": "..."
}}
""",
            "change_action_prompt",
        )

    async def start(self):
        pass

    def get_action_state_by_chat_id(self, chat_id: str) -> ChatAction:
        for action in self.action_state_list:
            if action.chat_id == chat_id:
                return action
        new_action = ChatAction(chat_id, self)
        self.action_state_list.append(new_action)
        return new_action

    async def update_action_by_message(self, message: MessageRecv):
        # Handle missing stream_id if any
        chat_id = "live_room"
        if message.chat_stream:
            chat_id = message.chat_stream.stream_id

        action = self.get_action_state_by_chat_id(chat_id)
        await action.update_action_by_message(message)
