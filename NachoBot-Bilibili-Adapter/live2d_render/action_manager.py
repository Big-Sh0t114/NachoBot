import json
from typing import Optional
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.config.config import global_config
from src.chat.utils.chat_message_builder import (
    build_readable_messages,
    get_raw_msg_by_timestamp_with_chat_inclusive,
)
from src.chat.message_receive.message import MessageRecv

# Motion Group mapping: LLM选择的动作名称 → Live2D Motion Group 名称
# 所有动作通过 StartRandomMotion(group, priority) 安全播放
# 每个 Motion Group 下有 2-3 个随机变体，自动随机抽取
DEFAULT_BODY_CODE = {
    "待机/放松": "Idle",
    "点头/同意": "Nod",
    "连续点头/非常赞同": "Nod",
    "摇头/否定": "Shake",
    "转身向左/看左边": "TurnLeft",
    "转身向右/看右边": "TurnRight",
    "身体前倾/好奇/仔细看": "LeanForward",
    "身体后仰/惊讶/吓一跳": "LeanBack",
    "身体晃动/开心/兴奋": "Sway",
    "歪头/疑惑/思考": "TiltHead",
    "害羞/移开视线/不好意思": "LookAway",
    "轻拍/打招呼": "Tap",
    "大动作/躲避/甩头": "Flick",
    "一般": "Idle",
}

# Hiyori Model Gaze Map (Live2D Unit Coordinates -1 to 1)
# X: -1 (Left) to 1 (Right)
# Y: -1 (Down) to 1 (Up)
HEAD_DIRECTION_MAP = {
    "Center": {"x": 0.0, "y": 0.0},
    "Left": {"x": -1.0, "y": 0.0},
    "Right": {"x": 1.0, "y": 0.0},
    "Up": {"x": 0.0, "y": 1.0},
    "Down": {"x": 0.0, "y": -1.0},
    "UpLeft": {"x": -0.8, "y": 0.8},
    "UpRight": {"x": 0.8, "y": 0.8},
    "DownLeft": {"x": -0.8, "y": -0.8},
    "DownRight": {"x": 0.8, "y": -0.8},
}


class ChatAction:
    def __init__(self, chat_id: str, manager):
        self.chat_id = chat_id
        self.manager = manager
        self.body_action = "一般"
        self.head_action = "Center"
        self.last_change_time = 0
        self.pending_action_update = False

    async def _call_llm(self, prompt: str) -> Optional[str]:
        # Helper to call LLM via Controller -> Adapter -> ModelClient
        model_client = self.manager.controller.adapter.model_client
        if not model_client:
            self.manager.logger.warning("ModelClient not available")
            return None
        return await model_client._call_task_model("utils_small", prompt)

    async def send_action_update(self):
        # 1. Send Body Action via StartRandomMotion (crash-safe)
        body_code = DEFAULT_BODY_CODE.get(self.body_action, "")
        if body_code and body_code != "Idle":
            # Use random_motion event type for StartRandomMotion
            await self.manager.controller.send_live2d_event(
                "random_motion", {"group": body_code, "priority": 3}
            )

        # 2. Send Head/Gaze Action
        gaze_data = HEAD_DIRECTION_MAP.get(self.head_action, None)
        if gaze_data:
            await self.manager.controller.send_live2d_event("auto_gaze", gaze_data)

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
        all_head_actions = ", ".join(HEAD_DIRECTION_MAP.keys())

        prompt = await global_prompt_manager.format_prompt(
            "change_action_prompt",
            chat_talking_prompt=chat_talking_prompt,
            indentify_block=indentify_block,
            body_action=self.body_action,
            head_action=self.head_action,
            all_actions=all_actions,
            all_head_actions=all_head_actions,
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

                new_body = data.get("body_action")
                new_head = data.get("head_action")

                updated = False
                if new_body and new_body in DEFAULT_BODY_CODE:
                    self.body_action = new_body
                    updated = True

                if new_head and new_head in HEAD_DIRECTION_MAP:
                    self.head_action = new_head
                    updated = True

                if updated:
                    self.pending_action_update = True
                    self.last_change_time = message_time
                    self.manager.logger.info(
                        f"[Action] Updated: Body={self.body_action}, Head={self.head_action} (Pending TTS Sync)"
                    )

            except Exception as e:
                self.manager.logger.error(
                    f"Failed to parse action response: {e}, resp: {response}"
                )

    async def on_start_replying(self):
        if self.pending_action_update:
            await self.send_action_update()
            self.pending_action_update = False
            self.manager.logger.info(
                "[Action] Dispatched pending action update on TTS playback start"
            )

    async def on_reply_finished(self):
        # Default back to Idle and Center after speaking
        self.body_action = "一般"
        self.head_action = "Center"
        
        # We also need to forcibly dispatch these to the renderer to break out of single-motion locks
        # Only dispatch gaze to Center. For body, Live2D v3 bindings may get stuck on the last frame of a non-looping Priority 3 motion
        # "Idle" is special cased to invoke the base idle group
        if self.manager.controller:
            await self.manager.controller.send_live2d_event(
                "random_motion", {"group": "Idle", "priority": 3}
            )
            gaze_data = HEAD_DIRECTION_MAP.get("Center", None)
            if gaze_data:
                await self.manager.controller.send_live2d_event("auto_gaze", gaze_data)
                
        self.manager.logger.info("[Action] Reply finished, reset to Body=Idle, Head=Center")


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
- 头部朝向：{head_action}

请分析最新的聊天内容，判断你是否需要改变动作或视线方向。
原则：
1. **多保持当前状态或归位**：如果没有明确的指令或强烈的情绪触发，请保持"待机/放松"和"Center"。
2. **不要频繁乱动**：动作应该自然且有意义，不要每一句话都触发大动作。
3. **视线控制**：只有在被要求"看左边"、"看右边"等，或者有明显空间指向性时才改变视线。否则保持"Center"。
4. **情绪匹配**：根据对话情绪选择合适动作。开心时可以晃动，疑惑时可以歪头，同意时可以点头。
5. **动作自然**：动作应该符合对话情境，比如被夸奖时"害羞/移开视线"，看到有趣的东西时"身体前倾/好奇"。

身体动作可选：
{all_actions}

头部朝向可选（Center为默认看镜头）：
{all_head_actions}

请只按照以下json格式输出：
{{
  "body_action": "...", 
  "head_action": "..."
}}
""",
            "change_action_prompt",
        )

    async def start(self):
        self.logger.info("ActionManager started")

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

    async def on_start_replying(self, chat_id="live_room"):
        action = self.get_action_state_by_chat_id(chat_id)
        await action.on_start_replying()

    async def on_reply_finished(self, chat_id="live_room"):
        action = self.get_action_state_by_chat_id(chat_id)
        await action.on_reply_finished()

