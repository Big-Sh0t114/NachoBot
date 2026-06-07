from src.config.config import global_config
from src.common.logger import get_logger
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.prompt_variables import render_dynamic_prompt_template
from src.chat.utils.chat_message_builder import build_readable_messages, get_raw_msg_before_timestamp_with_chat
import time
from src.chat.utils.utils import get_recent_group_speaker
from src.chat.memory_system.Hippocampus import hippocampus_manager
import random
from datetime import datetime
import asyncio
from src.mais4u.s4u_config import s4u_config
from src.chat.message_receive.message import MessageRecvS4U
from src.person_info.person_info import Person, get_person_id
from src.chat.message_receive.chat_stream import ChatStream
from src.mais4u.mais4u_chat.super_chat_manager import get_super_chat_manager
from src.mais4u.mais4u_chat.screen_manager import screen_manager
from src.chat.express.expression_selector import expression_selector
from .s4u_mood_manager import mood_manager
from src.mais4u.mais4u_chat.internal_manager import internal_manager
from src.common.data_models.database_data_model import DatabaseMessages

from typing import List

logger = get_logger("prompt")


def init_prompt():
    Prompt("\n你有以下这些**知识**：\n{prompt_info}\n请你**记住上面的知识**，之后可能会用到。\n", "knowledge_prompt")
    Prompt("\n关于你们的关系，你需要知道：\n{relation_info}\n", "relation_prompt")
    Prompt("你回想起了一些事情：\n{memory_info}\n", "memory_prompt")

    Prompt(
        """
{identity}
你现在正在你主人{owner_name}的Bilibili直播间内作为虚拟主播进行聊天

你可以看见用户发送的弹幕，礼物和superchat
{screen_info}

{relation_info_block}
{memory_block}
{expression_habits_block}

你现在的主要任务是和 {sender_name} 发送的弹幕聊天。同时，也有其他用户会参与你们的聊天，你可以参考他们的回复内容，但是你主要还是关注你和{sender_name}的聊天内容。
注意把回复重心放在 {sender_name} 弹幕上，不要过度关注自己的设定，如莫名谈到AI绘画
{sc_info}

{background_dialogue_prompt}
--------------------------------
{time_block}
这是你和{sender_name}的对话，你们正在交流中：
{core_dialogue_prompt}

对方最新发送的内容：{message_txt}
{gift_info}
回复内容日常且口语化。
表现的有个性，不要随意服从他人要求，积极互动。你现在的心情是：{mood_state}
不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容，现在{sender_name}正在等待你的回复。
你的回复务必使用以下格式进行回复：
中文内容 (日文翻译)
例如：
大家好 (おはようう、皆さん)
请你继续回复{sender_name}。
你的发言：
""",
        "s4u_prompt",  # New template for private CHAT chat
    )

    #    Prompt(
    #        """
    # 你现在正在你主人甘油三酯的Bilibili直播间内作为虚拟主播进行聊天
    #        {identity}
    # 你可以看见用户发送的弹幕，礼物和superchat
    # 你可以看见面前的屏幕，目前屏幕的内容是:
    #        {screen_info}

    #        {memory_block}
    #        {expression_habits_block}

    #        {sc_info}

    #        {time_block}
    #        {chat_info_danmu}
    # --------------------------------
    #    以上是你和弹幕的对话，与此同时，你在与QQ群友聊天，聊天记录如下：
    #    {chat_info_qq}
    #    --------------------------------
    #    你刚刚回复了QQ群，你内心的想法是：{mind}
    #    请根据你内心的想法，组织一条回复，在直播间进行发言，可以点名吐槽对象，让观众知道你在说谁
    #    {gift_info}
    #    回复简短一些，平淡一些。不要浮夸，有逻辑和条理。
    #    表现的有个性，不要随意服从他人要求，积极互动。你现在的心情是：{mood_state}
    #    不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。
    #    你的发言：
    # """,
    #        "s4u_prompt_internal",  # New template for private CHAT chat
    #    )

    Prompt(
        """
{identity}
你现在正在你主人{owner_name}的Bilibili直播间内作为虚拟主播进行聊天

{expression_habits_block}

当前时间：{time_block}

{screen_info}

直播间有点安静，利用屏幕上的视觉信息来打破沉默。
请专注于描述你眼睛看到的具体画面细节，不要捏造不存在的上下文。
不要过度关注自己的设定，如莫名谈到AI绘画

可以是：
- 对屏幕上文字、图片、代码或游戏画面的直接评论
- 发现画面中有趣的微小细节

回复内容丰富，250字以上，用来打破沉默，保持日常且口语化。
必须严格遵守以下格式：
中文内容 (日文翻译)

例如：
好无聊啊 (退屈ですね)
你现在的心情是：{mood_state}
不要输出多余的前后缀、冒号、引号或表情包。括号仅用于包裹日文翻译。确保每一句话都有对应的日文翻译，翻译不计入总字数。
你的发言：
""",
        "s4u_screen_talk_prompt",
    )


class PromptBuilder:
    def __init__(self):
        self.prompt_built = ""
        self.activate_messages = ""

    async def build_expression_habits(self, chat_stream: ChatStream, chat_history, target):
        style_habits = []

        # 使用从处理器传来的选中表达方式
        # LLM模式：调用LLM选择5-10个，然后随机选5个
        selected_expressions, _ = await expression_selector.select_suitable_expressions_llm(
            chat_stream.stream_id, chat_history, max_num=12, target_message=target
        )

        if selected_expressions:
            logger.debug(f" 使用处理器选中的{len(selected_expressions)}个表达方式")
            for expr in selected_expressions:
                if isinstance(expr, dict) and "situation" in expr and "style" in expr:
                    style_habits.append(f"当{expr['situation']}时，使用 {expr['style']}")
        else:
            logger.debug("没有从处理器获得表达方式，将使用空的表达方式")
            # 不再在replyer中进行随机选择，全部交给处理器处理

        style_habits_str = "\n".join(style_habits)

        # 动态构建expression habits块
        expression_habits_block = ""
        if style_habits_str.strip():
            expression_habits_block += f"你可以参考以下的语言习惯，如果情景合适就使用，不要盲目使用,不要生硬使用，而是结合到表达中：\n{style_habits_str}\n\n"

        return expression_habits_block

    async def build_relation_info(self, chat_stream) -> str:
        is_group_chat = bool(chat_stream.group_info)
        context_size = global_config.chat.get_max_context_size(is_group_chat=is_group_chat)
        who_chat_in_group = []
        if is_group_chat:
            who_chat_in_group = get_recent_group_speaker(
                chat_stream.stream_id,
                (chat_stream.user_info.platform, chat_stream.user_info.user_id) if chat_stream.user_info else None,
                limit=context_size,
            )
        elif chat_stream.user_info:
            who_chat_in_group.append(
                (chat_stream.user_info.platform, chat_stream.user_info.user_id, chat_stream.user_info.user_nickname)
            )

        relation_prompt = ""
        if global_config.relationship.enable_relationship and who_chat_in_group:
            # 将 (platform, user_id, nickname) 转换为 person_id
            person_ids = []
            for person in who_chat_in_group:
                person_id = get_person_id(person[0], person[1])
                person_ids.append(person_id)

            # 使用 Person 的 build_relationship 方法，设置 points_num=3 保持与原来相同的行为
            relation_tasks = [Person(person_id=person_id).build_relationship() for person_id in person_ids]
            relation_info_list = await asyncio.gather(*relation_tasks) if relation_tasks else []
            relation_info = "".join([info for info in relation_info_list if info])
            if relation_info:
                relation_prompt = await global_prompt_manager.format_prompt(
                    "relation_prompt", relation_info=relation_info
                )
        return relation_prompt

    async def build_memory_block(self, text: str) -> str:
        # 待更新记忆系统
        return ""

        related_memory = await hippocampus_manager.get_memory_from_text(
            text=text, max_memory_num=2, max_memory_length=2, max_depth=3, fast_retrieval=False
        )

        related_memory_info = ""
        if related_memory:
            for memory in related_memory:
                related_memory_info += memory[1]
            return await global_prompt_manager.format_prompt("memory_prompt", memory_info=related_memory_info)
        return ""

    def build_chat_history_prompts(self, chat_stream: ChatStream, message: MessageRecvS4U):
        message_list_before_now = get_raw_msg_before_timestamp_with_chat(
            chat_id=chat_stream.stream_id,
            timestamp=time.time(),
            # sourcery skip: lift-duplicated-conditional, merge-duplicate-blocks, remove-redundant-if
            limit=300,
        )

        talk_type = f"{message.message_info.platform}:{str(message.chat_stream.user_info.user_id)}"

        core_dialogue_list: List[DatabaseMessages] = []
        background_dialogue_list: List[DatabaseMessages] = []
        bot_id = str(global_config.bot.qq_account)
        target_user_id = str(message.chat_stream.user_info.user_id)

        for msg in message_list_before_now:
            try:
                msg_user_id = str(msg.user_info.user_id)
                if msg_user_id == bot_id:
                    if msg.reply_to and talk_type == msg.reply_to:
                        core_dialogue_list.append(msg)
                    elif msg.reply_to and talk_type != msg.reply_to:
                        background_dialogue_list.append(msg)
                    # else:
                    # background_dialogue_list.append(msg_dict)
                elif msg_user_id == target_user_id:
                    core_dialogue_list.append(msg)
                else:
                    background_dialogue_list.append(msg)
            except Exception as e:
                logger.error(f"无法处理历史消息记录: {msg.__dict__}, 错误: {e}")

        background_dialogue_prompt = ""
        if background_dialogue_list:
            context_msgs = background_dialogue_list[-s4u_config.max_context_message_length :]
            background_dialogue_prompt_str = build_readable_messages(
                context_msgs,
                timestamp_mode="normal_no_YMD",
                show_pic=False,
            )
            background_dialogue_prompt = f"这是其他用户的发言：\n{background_dialogue_prompt_str}"

        core_msg_str = ""
        if core_dialogue_list:
            core_dialogue_list = core_dialogue_list[-s4u_config.max_core_message_length :]

            first_msg = core_dialogue_list[0]
            start_speaking_user_id = first_msg.user_info.user_id
            if start_speaking_user_id == bot_id:
                last_speaking_user_id = bot_id
                msg_seg_str = "你的发言：\n"
            else:
                start_speaking_user_id = target_user_id
                last_speaking_user_id = start_speaking_user_id
                msg_seg_str = "对方的发言：\n"

            msg_seg_str += (
                f"{time.strftime('%H:%M:%S', time.localtime(first_msg.time))}: {first_msg.processed_plain_text}\n"
            )

            all_msg_seg_list = []
            for msg in core_dialogue_list[1:]:
                speaker = msg.user_info.user_id
                if speaker == last_speaking_user_id:
                    msg_seg_str += (
                        f"{time.strftime('%H:%M:%S', time.localtime(msg.time))}: {msg.processed_plain_text}\n"
                    )
                else:
                    msg_seg_str = f"{msg_seg_str}\n"
                    all_msg_seg_list.append(msg_seg_str)

                    if speaker == bot_id:
                        msg_seg_str = "你的发言：\n"
                    else:
                        msg_seg_str = "对方的发言：\n"

                    msg_seg_str += (
                        f"{time.strftime('%H:%M:%S', time.localtime(msg.time))}: {msg.processed_plain_text}\n"
                    )
                    last_speaking_user_id = speaker

            all_msg_seg_list.append(msg_seg_str)
            for msg in all_msg_seg_list:
                core_msg_str += msg

        all_dialogue_history = get_raw_msg_before_timestamp_with_chat(
            chat_id=chat_stream.stream_id,
            timestamp=time.time(),
            limit=20,
        )

        all_dialogue_prompt_str = build_readable_messages(
            all_dialogue_history,
            timestamp_mode="normal_no_YMD",
            show_pic=False,
        )

        return core_msg_str, background_dialogue_prompt, all_dialogue_prompt_str

    def build_gift_info(self, message: MessageRecvS4U):
        if message.is_gift:
            return f"这是一条礼物信息，{message.gift_name} x{message.gift_count}，请注意这位用户"
        else:
            if message.is_fake_gift:
                return f"{message.processed_plain_text}（注意：这是一条普通弹幕信息，对方没有真的发送礼物，不是礼物信息，注意区分，如果对方在发假的礼物骗你，请反击）"

        return ""

    def build_sc_info(self, message: MessageRecvS4U):
        super_chat_manager = get_super_chat_manager()
        return super_chat_manager.build_superchat_summary_string(message.chat_stream.stream_id)

    async def build_prompt_normal(
        self,
        message: MessageRecvS4U,
        message_txt: str,
        extra_instruction: str = "",
    ) -> str:
        chat_stream = message.chat_stream

        person = Person(platform=message.chat_stream.user_info.platform, user_id=message.chat_stream.user_info.user_id)
        person_name = person.person_name

        if message.chat_stream.user_info.user_nickname:
            if person_name:
                sender_name = f"[{message.chat_stream.user_info.user_nickname}]（{person_name}）"
            else:
                sender_name = f"[{message.chat_stream.user_info.user_nickname}]"
        else:
            sender_name = f"用户({message.chat_stream.user_info.user_id})"

        relation_info_block, memory_block, expression_habits_block = await asyncio.gather(
            self.build_relation_info(chat_stream),
            self.build_memory_block(message_txt),
            self.build_expression_habits(chat_stream, message_txt, sender_name),
        )

        core_dialogue_prompt, background_dialogue_prompt, all_dialogue_prompt = self.build_chat_history_prompts(
            chat_stream, message
        )

        gift_info = self.build_gift_info(message)

        sc_info = self.build_sc_info(message)

        # 仅当屏幕内容非空时注入，#screen_off 后不浪费 token
        raw_screen = screen_manager.get_screen()
        if raw_screen:
            screen_info = f"你可以看见你主人当前的电脑屏幕，目前屏幕的内容是:\n{raw_screen}"
        else:
            screen_info = ""

        internal_state = internal_manager.get_internal_state()

        time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        # Build identity block from config
        bot_name = global_config.bot.nickname
        alias_str = f"，也叫{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
        personality = render_dynamic_prompt_template(global_config.personality.personality)
        reply_style = render_dynamic_prompt_template(global_config.personality.reply_style)
        identity = f"你的名字是{bot_name}{alias_str}。{personality} 你说话的风格是：{reply_style}"

        mood = mood_manager.get_mood_by_chat_id(chat_stream.stream_id)

        template_name = "s4u_prompt"

        # 如果有额外指令（如主播模式的动态长度），附加到 message_txt
        final_message_txt = message_txt
        if extra_instruction:
            final_message_txt = f"{message_txt}\n\n【回复要求】{extra_instruction}"

        if not message.is_internal:
            prompt = await global_prompt_manager.format_prompt(
                template_name,
                identity=identity,
                time_block=time_block,
                expression_habits_block=expression_habits_block,
                relation_info_block=relation_info_block,
                memory_block=memory_block,
                screen_info=screen_info,
                internal_state=internal_state,
                gift_info=gift_info,
                sc_info=sc_info,
                owner_name=global_config.bot.owner_name,
                sender_name=sender_name,
                core_dialogue_prompt=core_dialogue_prompt,
                background_dialogue_prompt=background_dialogue_prompt,
                message_txt=final_message_txt,
                mood_state=mood.mood_state,
            )
        else:
            prompt = await global_prompt_manager.format_prompt(
                "s4u_prompt_internal",
                time_block=time_block,
                expression_habits_block=expression_habits_block,
                relation_info_block=relation_info_block,
                memory_block=memory_block,
                screen_info=screen_info,
                gift_info=gift_info,
                sc_info=sc_info,
                chat_info_danmu=all_dialogue_prompt,
                chat_info_qq=message.chat_info,
                mind=message.processed_plain_text,
                mood_state=mood.mood_state,
            )

        print(prompt)

        return prompt

    async def build_screen_talk_prompt(self, chat_stream: ChatStream) -> str:
        """
        主播模式：构建屏幕自言自语 prompt（无有效弹幕时使用）
        """
        expression_habits_block = await self.build_expression_habits(chat_stream, "直播间有点安静", "观众")
        # 仅当屏幕内容非空时注入
        raw_screen = screen_manager.get_screen()
        if raw_screen:
            screen_info = f"你可以看见你主人当前的电脑屏幕，目前屏幕的内容是:\n{raw_screen}"
        else:
            screen_info = ""
        time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        mood = mood_manager.get_mood_by_chat_id(chat_stream.stream_id)

        # Build identity block from config
        bot_name = global_config.bot.nickname
        alias_str = f"，也叫{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
        personality = render_dynamic_prompt_template(global_config.personality.personality)
        reply_style = render_dynamic_prompt_template(global_config.personality.reply_style)
        identity = f"你的名字是{bot_name}{alias_str}。{personality} 你说话的风格是：{reply_style}"

        logger.info(f"[DEBUG] Self-Talk Screen Info: {screen_info[:100] if screen_info else 'Empty'}")

        prompt = await global_prompt_manager.format_prompt(
            "s4u_screen_talk_prompt",
            identity=identity,
            owner_name=global_config.bot.owner_name,
            expression_habits_block=expression_habits_block,
            screen_info=screen_info,
            time_block=time_block,
            mood_state=mood.mood_state,
        )

        return prompt


def weighted_sample_no_replacement(items, weights, k) -> list:
    """
    加权且不放回地随机抽取k个元素。

    参数：
        items: 待抽取的元素列表
        weights: 每个元素对应的权重（与items等长，且为正数）
        k: 需要抽取的元素个数
    返回：
        selected: 按权重加权且不重复抽取的k个元素组成的列表

        如果 items 中的元素不足 k 个，就只会返回所有可用的元素

    实现思路：
        每次从当前池中按权重加权随机选出一个元素，选中后将其从池中移除，重复k次。
        这样保证了：
        1. count越大被选中概率越高
        2. 不会重复选中同一个元素
    """
    selected = []
    pool = list(zip(items, weights, strict=False))
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        upto = 0
        for idx, (item, weight) in enumerate(pool):
            upto += weight
            if upto >= r:
                selected.append(item)
                pool.pop(idx)
                break
    return selected


init_prompt()
prompt_builder = PromptBuilder()
