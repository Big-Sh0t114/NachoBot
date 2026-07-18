import asyncio
import json
from typing import Dict, Optional

from src.chat.message_receive.message import MessageRecv
from src.chat.utils.chat_message_builder import (
    build_readable_messages,
    get_raw_msg_by_timestamp_with_chat_inclusive,
)
from src.config.config import global_config
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager


class ChatMood:
    def __init__(self, chat_id: str, manager):
        self.chat_id: str = chat_id
        self.manager = manager
        self.mood_state: str = "感觉很平静"
        self.mood_values: Dict[str, int] = {
            "joy": 5,
            "anger": 1,
            "sorrow": 1,
            "fear": 1,
            "shy": 1,
            "disgust": 1,
        }
        self.regression_count: int = 0
        self.last_change_time: float = 0

        # Initial send
        asyncio.create_task(self.send_emotion_update(self.mood_values))

    async def _call_llm(
        self, prompt: str, request_type: str = "mood_text"
    ) -> Optional[str]:
        # Helper to call LLM via Controller -> Adapter -> ModelClient
        model_client = self.manager.controller.adapter.model_client
        if not model_client:
            self.manager.logger.warning("ModelClient not available")
            return None

        # Use 'utils_small' or similar if available, else 'replyer'
        # In model_client.py, we have call_planner and call_replyer.
        # We might need to use call_replyer as a fallback or add a generic call.
        # Assuming we can use call_replyer for now or the generic _call_task_model if exposed.
        # But _call_task_model is private.
        # We will use call_replyer for now, or assume adapter exposes a generic way.

        # Use planner (small model) for mood detection
        return await model_client.call_planner(prompt)

    def _parse_numerical_mood(self, response: str) -> Optional[Dict[str, int]]:
        try:
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                response = response.split("```")[1].split("```")[0]

            data = json.loads(response)
            required_keys = {"joy", "anger", "sorrow", "fear", "shy", "disgust"}
            if not required_keys.issubset(data.keys()):
                return None

            return {
                key: int(data[key])
                for key in required_keys
                if 1 <= int(data[key]) <= 10
            }

        except Exception as e:
            self.manager.logger.error(f"Error parsing numerical mood: {e}")
            return None

    async def update_mood_by_message(self, message: MessageRecv):
        self.regression_count = 0
        message_time: float = message.message_info.time

        # Get history
        timestamp_start = self.last_change_time
        timestamp_end = message_time

        # Use global_config from NachoBot (imported)
        # Note: logic for 'get_raw_msg...' relies on DB connection which should be active

        try:
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

        async def _update_text_mood():
            prompt = await global_prompt_manager.format_prompt(
                "change_mood_prompt_vtb",
                chat_talking_prompt=chat_talking_prompt,
                indentify_block=indentify_block,
                mood_state=self.mood_state,
            )
            return await self._call_llm(prompt)

        async def _update_numerical_mood():
            prompt = await global_prompt_manager.format_prompt(
                "change_mood_numerical_prompt",
                chat_talking_prompt=chat_talking_prompt,
                indentify_block=indentify_block,
                mood_state=self.mood_state,
                joy=self.mood_values["joy"],
                anger=self.mood_values["anger"],
                sorrow=self.mood_values["sorrow"],
                fear=self.mood_values["fear"],
                shy=self.mood_values["shy"],
                disgust=self.mood_values["disgust"],
            )
            resp = await self._call_llm(prompt)
            if resp:
                return self._parse_numerical_mood(resp)
            return None

        results = await asyncio.gather(_update_text_mood(), _update_numerical_mood())
        text_mood, num_mood = results

        if text_mood:
            self.mood_state = text_mood
        if num_mood:
            self.mood_values = num_mood
            await self.send_emotion_update(self.mood_values)

        self.last_change_time = message_time

    async def regress_mood(self):
        # ... Similar logic for regress ...
        # Simplified for brevity, logic allows regression
        pass

    async def send_emotion_update(self, mood_values: Dict[str, int]):
        emotion_data = {
            "joy": mood_values.get("joy", 5),
            "anger": mood_values.get("anger", 1),
            "sorrow": mood_values.get("sorrow", 1),
            "fear": mood_values.get("fear", 1),
            "shy": mood_values.get("shy", 1),
            "disgust": mood_values.get("disgust", 1),
        }
        await self.manager.controller.send_live2d_event("emotion", emotion_data)


class MoodManager:
    def __init__(self, controller):
        self.controller = controller
        self.logger = controller.logger
        self.mood_list: list[ChatMood] = []
        self.task_started = False
        self._init_prompts()

    def _init_prompts(self):
        # Register prompts ported from the legacy live mood manager.
        # We assume Prompt class is available
        Prompt(
            """{chat_talking_prompt}\n以上是直播间里正在进行的对话\n\n{indentify_block}\n你刚刚的情绪状态是：{mood_state}\n\n现在，发送了消息，引起了你的注意，你对其进行了阅读和思考，请你输出一句话描述你新的情绪状态，不要输出任何其他内容\n请只输出情绪状态，不要输出其他内容：\n""",
            "change_mood_prompt_vtb",
        )
        Prompt(
            """{chat_talking_prompt}\n以上是直播间里正在进行的对话\n\n{indentify_block}\n你刚刚的情绪状态是：{mood_state}\n具体来说，从1-10分，你的情绪状态是：\n喜(Joy): {joy}\n怒(Anger): {anger}\n哀(Sorrow): {sorrow}\n惧(Fear): {fear}\n害羞(Shy): {shy}\n厌恶(disgust): {disgust}\n\n现在，发送了消息，引起了你的注意，你对其进行了阅读和思考。请基于对话内容，评估你新的情绪状态。\n请以JSON格式输出你新的情绪状态，包含"喜、怒、哀、惧、害羞、厌恶"六个维度，每个维度的取值范围为1-10。\n键值请使用英文: "joy", "anger", "sorrow", "fear", "shy", "disgust".\n例如: {{"joy": 5, "anger": 1, "sorrow": 1, "fear": 1, "shy": 1, "disgust": 1}}\n不要输出任何其他内容，只输出JSON。\n""",
            "change_mood_numerical_prompt",
        )
        # Add others as needed

    async def start(self):
        self.task_started = True
        # Start regression loop (simplified)
        asyncio.create_task(self._regression_loop())

    async def _regression_loop(self):
        while True:
            await asyncio.sleep(30)
            # implement regression logic loop
            pass

    def get_mood_by_chat_id(self, chat_id: str) -> ChatMood:
        for mood in self.mood_list:
            if mood.chat_id == chat_id:
                return mood
        new_mood = ChatMood(chat_id, self)
        self.mood_list.append(new_mood)
        return new_mood

    async def update_mood_by_message(self, message: MessageRecv):
        # Get chat_id from message
        # Note: MessageRecv structure in adapter might be slightly different or same (imported from ncnk_message)
        # In adapter.py we use ncnk_message.MessageBase, but here we expect MessageRecv
        # Actually MessageRecv is from src.chat.message_receive.message

        # Assuming logic to extract chat_id
        chat_id = "live_room"  # Simplified for single room context usually
        if message.chat_stream:
            chat_id = message.chat_stream.stream_id

        mood = self.get_mood_by_chat_id(chat_id)
        await mood.update_mood_by_message(message)
