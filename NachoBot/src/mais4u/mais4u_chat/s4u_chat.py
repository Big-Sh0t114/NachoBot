import asyncio
import traceback
import time
import random
import re
from typing import Dict, Optional, Tuple, List  # 导入类型提示
from ncnk_message import UserInfo, Seg
from src.common.logger import get_logger
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from .s4u_stream_generator import S4UStreamGenerator
from src.chat.message_receive.message import MessageSending, MessageRecv, MessageRecvS4U
from src.config.config import global_config
from src.common.message.api import get_global_api
from src.chat.message_receive.storage import MessageStorage
from .s4u_watching_manager import watching_manager
import json
from .s4u_mood_manager import mood_manager
from src.mais4u.s4u_config import s4u_config, s4u_config_main
from src.person_info.person_info import get_person_id
from .yes_or_no import yes_or_no_head
from src.chat.utils.bilingual_splitter import bilingual_splitter

logger = get_logger("S4U_chat")


def clean_tts_text(text: str) -> str:
    """
    清洗TTS文本，移除颜文字、特殊符号，仅保留日文、中文、英文、数字及标准标点。
    """
    # 定义允许的字符范围：
    # \u3040-\u309f: 平假名
    # \u30a0-\u30ff: 片假名
    # \u4e00-\u9faf: CJK统一汉字
    # \u3000-\u303f: CJK标点符号 (如 、 。)
    # \uff00-\uffef: 全角字符 (包括全角标点)
    # \w: 字母数字下划线
    # \s: 空白
    # .,?!: 基本英文标点
    # 排除其他所有符号 (如 star *, Greek chars like ω, etc. often used in kaomoji)

    # 使用反向逻辑：替换掉 不在白名单 的字符
    # 注意：为了保留长音符号 'ー' (\u30fc) 和片假名中点 '・' (\u30fb - 有时TTS能读，有时不能，保险起见先保留，如果TTS报错再去掉)
    # 实测 '・' (30fb) 在某些TTS可能导致停顿或错误，颜文字常用 -> 还是允许吧，它也是人名间隔符。
    # 颜文字 `´・ω・`` : `´` (B4) is distinct. `ω` (03C9) is distinct.

    cleaned = re.sub(
        r"[^\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\u3000-\u303f\uff01-\uff9f\w\s,.?!，。？！、~～]", "", text
    )
    return cleaned.strip()


def parse_bilingual_content(text: str) -> Tuple[str, Optional[str]]:
    """
    解析双语内容
    期望格式：中文内容 (日文翻译)
    返回：(显示文本, TTS文本)
    """
    # 匹配所有 "中文 (日文)" 格式中的日文部分
    pattern = r"[(（](.*?)[)）]"
    parts = re.findall(pattern, text, re.DOTALL)

    if parts:
        # 如果匹配到至少一个
        # tts_文本 = 所有括号内内容清洗后拼接 (用 <JP> 包裹)
        # Apply cleaning to each part
        cleaned_parts = [clean_tts_text(p) for p in parts]
        # Filter out empty strings after cleaning
        cleaned_parts = [p for p in cleaned_parts if p]

        if cleaned_parts:
            tts_text = "".join([f"<JP>{p}</JP>" for p in cleaned_parts])
        else:
            tts_text = None

        # 显示文本 = 移除所有括号及内容 (保持原样显示，不清洗)
        chat_text = re.sub(pattern, "", text, flags=re.DOTALL)
        # 清理因移除产生的多余空白
        chat_text = re.sub(r"\s+", " ", chat_text).strip()

        return chat_text, tts_text

    # 如果未匹配到格式，回退到默认逻辑
    chat_text = text
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
        cleaned_text = clean_tts_text(text)
        if cleaned_text:
            tts_text = f"<JP>{cleaned_text}</JP>"
        else:
            tts_text = None
    else:
        # Fallback: Treat as Chinese. Use enclosed tags.
        # Avoid sending tts_text if it is same as chat_text (to prevent duplication in adapter buffer)
        # But adapter needs tags to know language?
        # Actually, if we send ONLY text segment, adapter treats as raw.
        # If we send tts_text segment, adapter buffers it.
        # To avoid duplication, we return None for tts_text here if we don't want TTS.
        # But if user wants TTS for Chinese? Bot only speaks Japanese (as per adapter verification).
        # So returning None is correct.
        tts_text = None

    return chat_text, tts_text


def is_english_letter(char: str) -> bool:
    """Check if character is an english letter (case insensitive)"""
    return "a" <= char.lower() <= "z"


def _advanced_split(text: str) -> List[str]:
    """
    Advanced sentence splitter referenced from src/chat/utils/utils.py
    Splits by punctuation but merges short segments probabilistically.
    """
    # Preprocessing
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"\n\s*([，,。;\s])", r"\1", text)
    text = re.sub(r"([，,。;\s])\s*\n", r"\1", text)
    text = re.sub(r"([\u4e00-\u9fff])\n([\u4e00-\u9fff])", r"\1。\2", text)

    len_text = len(text)
    if len_text < 3:
        # For very short text, rarely split chars
        return list(text) if random.random() < 0.01 else [text]

    separators = {"，", ",", " ", "。", ";", ".", "!", "?", "！", "？"}
    segments = []
    current_segment = ""

    # 1. Split into (content, separator) tuples
    i = 0
    while i < len(text):
        char = text[i]
        if char in separators:
            can_split = True
            # Don't split if surrounded by English letters (e.g. Mr.Smith)
            if 0 < i < len(text) - 1:
                prev_char = text[i - 1]
                next_char = text[i + 1]
                if is_english_letter(prev_char) and is_english_letter(next_char):
                    can_split = False

            if can_split:
                if current_segment:
                    segments.append((current_segment, char))
                elif char == " ":
                    segments.append(("", char))
                current_segment = ""
            else:
                current_segment += char
        else:
            current_segment += char
        i += 1

    if current_segment:
        segments.append((current_segment, ""))

    segments = [(content, sep) for content, sep in segments if content or sep]

    if not segments:
        return [text] if text else []

    # 2. Merge logic (Probabilistic)
    # We want granularity ~10 chars.
    if len_text < 10:
        split_strength = 0.3  # Mostly merge
    else:
        split_strength = 0.9  # Aggressive split

    merge_probability = 1.0 - split_strength

    merged_segments = []
    idx = 0
    while idx < len(segments):
        current_content, current_sep = segments[idx]

        # Check merge
        if idx + 1 < len(segments) and random.random() < merge_probability and current_content:
            next_content, next_sep = segments[idx + 1]
            if next_content:
                merged_content = current_content + current_sep + next_content
                merged_segments.append((merged_content, next_sep))
            else:
                merged_segments.append((current_content, next_sep))
            idx += 2
        else:
            merged_segments.append((current_content, current_sep))
            idx += 1

    final_sentences = []
    for content, sep in merged_segments:
        if content:
            # Reattach separator for subtitle flow logic
            final_sentences.append(content + sep)

    return [s for s in final_sentences if s.strip()]


async def split_bilingual_text(text: str) -> List[str]:
    """
    Split text using LLM (BilingualSplitter).
    Fallback to regex-based _split_bilingual_text_fallback if LLM fails.
    """
    try:
        # Attempt LLM Split
        sentences = await bilingual_splitter.split_text(text)
        if sentences:
            return sentences
        else:
            logger.warning("LLM Split returned empty, using fallback.")
            return _split_bilingual_text_fallback(text)
    except Exception as e:
        logger.error(f"LLM Split error: {e}, using fallback.")
        return _split_bilingual_text_fallback(text)


def _split_bilingual_text_fallback(text: str) -> List[str]:
    """
    [Fallback Strategy]
    Split text into sentences based on bilingual format or punctuation.
    Further split long sentences by commas to ensure subtitles are short (~10-15 chars).
    Uses Proportional Alignment Logic (CN Master).
    """

    # 1. First Pass: Split by bilingual pattern "Content (JP) Punctuation"
    pattern = re.compile(r"(.*?[(（].*?[)）][\s\u3002\uff1f\uff01!?;]*)", re.DOTALL)
    last_end = 0
    has_brackets = False

    primary_sentences = []
    for match in pattern.finditer(text):
        has_brackets = True
        chunk = match.group(1).strip()
        if chunk:
            primary_sentences.append(chunk)
        last_end = match.end()

    if has_brackets:
        tail = text[last_end:].strip()
        if tail:
            primary_sentences.append(tail)
    else:
        # Fallback for non-bilingual text
        return _advanced_split(text)

    # 1.1 Post-process: Merge orphaned component chunks
    # (e.g. if "Text (Kaomoji) (Transition)" got split into "Text (Kaomoji)" and "(Transition)" because of multiple brackets)
    merged_sentences = []
    for s in primary_sentences:
        # Check if s starts with a bracket and looks like an orphan (no content before bracket)
        # Regex: Start with optional space, then bracket.
        if re.match(r"^\s*[(（]", s) and merged_sentences:
            # Merge with previous
            merged_sentences[-1] += " " + s
        else:
            merged_sentences.append(s)
    primary_sentences = merged_sentences

    # 2. Second Pass: Sub-split content parts
    final_sentences = []

    for sentence in primary_sentences:
        # Check if sentence has JP part
        # Use finditer to find ALL bracket groups, pick the LAST one as JP candidate
        matches = list(re.finditer(r"[(（](.*?)[)）]", sentence, re.DOTALL))

        if matches:
            # Assume last bracket is the translation
            jp_match = matches[-1]
            jp_full_content = jp_match.group(1)
            jp_start = jp_match.start()
            jp_end = jp_match.end()

            # Content is everything before the LAST bracket
            content_part = sentence[:jp_start]
            tail_part = sentence[jp_end:]

            # Check for leaked Japanese in content_part
            jp_leak_match = re.search(r"[\u3040-\u309f\u30a0-\u30ff]", content_part)
            if jp_leak_match:
                leak_start = jp_leak_match.start()
                leaked_jp = content_part[leak_start:]
                real_content = content_part[:leak_start]
                content_part = real_content
                jp_full_content = leaked_jp + jp_full_content

            # 1. Master Split: Split Chinese Content for Visual Pacing
            cn_parts = _advanced_split(content_part)

            target_count = len(cn_parts)

            # 2. Slave Split: Force Split Japanese into 'target_count' parts
            # This ensures 1:1 alignment so that each subtitle chunk has corresponding audio
            jp_parts = []

            if target_count <= 1:
                jp_parts = [jp_full_content]
            else:
                # Proportional Split Algorithm
                # Attempt to split JP text into 'target_count' chunks of roughly equal length
                # respecting punctuation if possible.

                # First, try to split by punctuation to see if we have enough "natural" chunks
                natural_jp_parts = _advanced_split(jp_full_content)

                if len(natural_jp_parts) == target_count:
                    jp_parts = natural_jp_parts
                else:
                    # Hard Split / Re-grouping needed
                    # Just force split by length for now to guarantee alignment
                    total_len = len(jp_full_content)
                    chunk_len = max(1, total_len // target_count)

                    start_idx = 0
                    for i in range(target_count):
                        if i == target_count - 1:
                            # Last chunk takes rest
                            jp_parts.append(jp_full_content[start_idx:])
                        else:
                            end_idx = start_idx + chunk_len
                            # Try to find a punctuation/space near end_idx to snap to
                            # (Simple scan forward/back 5 chars)
                            snap_point = end_idx
                            found_snap = False
                            for offset in range(-5, 6):
                                check_idx = end_idx + offset
                                if 0 < check_idx < total_len and jp_full_content[check_idx] in " 、，,。. ":
                                    snap_point = check_idx + 1  # Include punct
                                    found_snap = True
                                    break

                            jp_parts.append(jp_full_content[start_idx:snap_point])
                            start_idx = snap_point

            # 3. Combine
            combined_chunks = []
            limit = min(len(cn_parts), len(jp_parts))
            for i in range(limit - 1):
                chunk_cn = cn_parts[i]
                chunk_jp = jp_parts[i]
                combined_chunks.append(f"{chunk_cn} ({chunk_jp})")

            # Last part gets tail punctuation and remaining JP (if any mismatch)
            rest_cn = "".join(cn_parts[limit - 1 :])
            rest_jp = "".join(jp_parts[limit - 1 :])
            logger.info(f"Bilingual Split: CN={len(cn_parts)} parts, JP={len(jp_parts)} parts => Aligned.")

            combined_chunks.append(f"{rest_cn} ({rest_jp}){tail_part}")
            final_sentences.extend(combined_chunks)

        else:
            split_chunks = _advanced_split(sentence)
            final_sentences.extend(split_chunks)

    return [s for s in final_sentences if s]


class MessageSenderContainer:
    """一个简单的容器，用于按顺序发送消息并模拟打字效果。"""

    def __init__(self, chat_stream: ChatStream, original_message: MessageRecv):
        self.chat_stream = chat_stream
        self.original_message = original_message
        self.queue = asyncio.Queue()
        self.storage = MessageStorage()
        self._task: Optional[asyncio.Task] = None
        self._paused_event = asyncio.Event()
        self._paused_event.set()  # 默认设置为非暂停状态

        self.msg_id = ""

        self.last_msg_id = ""

        self.voice_done = ""

    async def add_message(self, chunk: str):
        """向队列中添加一个消息块。"""
        await self.queue.put(chunk)

    async def close(self):
        """表示没有更多消息了，关闭队列。"""
        await self.queue.put(None)  # Sentinel

    def pause(self):
        """暂停发送。"""
        self._paused_event.clear()

    def resume(self):
        """恢复发送。"""
        self._paused_event.set()

    def _calculate_typing_delay(self, text: str) -> float:
        """根据文本长度计算模拟打字延迟。"""
        chars_per_second = s4u_config.chars_per_second
        min_delay = s4u_config.min_typing_delay
        max_delay = s4u_config.max_typing_delay

        delay = len(text) / chars_per_second
        return max(min_delay, min(delay, max_delay))

    async def _send_worker(self):
        """从队列中取出消息并发送。"""
        while True:
            try:
                # This structure ensures that task_done() is called for every item retrieved,
                # even if the worker is cancelled while processing the item.
                chunk = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                if chunk is None:
                    break

                # Check for pause signal *after* getting an item.
                await self._paused_event.wait()
                if not chunk:
                    continue

                if not chunk:
                    continue

                # Smart Sentence Splitting
                # Even if we receive a large chunk (e.g. non-streaming), we split it.
                # Split bilingual text into semantic sentences using LLM (with fallback)
                sentences = await split_bilingual_text(chunk)

                for sentence in sentences:
                    # Parse bilingual content first
                    chat_text, tts_text = parse_bilingual_content(sentence)

                    # 发送TTS Segment (如果有日文)
                    if tts_text:
                        message_segment = Seg(type="tts_text", data=tts_text)
                        bot_message = MessageSending(
                            message_id=self.msg_id,
                            chat_stream=self.chat_stream,
                            bot_user_info=UserInfo(
                                user_id=global_config.bot.qq_account,
                                user_nickname=global_config.bot.nickname,
                                platform=self.original_message.message_info.platform,
                            ),
                            sender_info=self.original_message.message_info.user_info,
                            message_segment=message_segment,
                            reply=self.original_message,
                            is_emoji=False,
                            apply_set_reply_logic=True,
                            reply_to=f"{self.original_message.message_info.user_info.platform}:{self.original_message.message_info.user_info.user_id}",
                        )

                        await bot_message.process()

                        # Send TTS segment first
                        await get_global_api().send_message(bot_message)
                        logger.info(
                            f"已将消息 '{self.msg_id}:{sentence[:20]}...' (TTS: {tts_text}) 发往平台 '{bot_message.message_info.platform}'"
                        )

                    # 发送Text Segment (使用中文/显示文本)
                    if chat_text:
                        message_segment = Seg(type="text", data=chat_text)
                        bot_message = MessageSending(
                            message_id=self.msg_id,
                            chat_stream=self.chat_stream,
                            bot_user_info=UserInfo(
                                user_id=global_config.bot.qq_account,
                                user_nickname=global_config.bot.nickname,
                                platform=self.original_message.message_info.platform,
                            ),
                            sender_info=self.original_message.message_info.user_info,
                            message_segment=message_segment,
                            reply=self.original_message,
                            is_emoji=False,
                            apply_set_reply_logic=True,
                            reply_to=f"{self.original_message.message_info.user_info.platform}:{self.original_message.message_info.user_info.user_id}",
                        )
                        await bot_message.process()

                        await get_global_api().send_message(bot_message)
                        logger.info(
                            f"已将消息 '{self.msg_id}:{sentence[:20]}...' (Text: {chat_text}) 发往平台 '{bot_message.message_info.platform}'"
                        )

                        await self.storage.store_message(bot_message, self.chat_stream)

                    # Calculate Wait Duration based on JP TTS length
                    # JP TTS speed approx 5 chars/sec.
                    jp_len = len(tts_text) if tts_text else 0
                    audio_duration = (jp_len / 5.0) + 0.5

                    delay = max(1.5, min(audio_duration, 8.0))

                    # Sleep AFTER sending to keep subtitle on screen
                    await asyncio.sleep(delay)

            except Exception as e:
                logger.error(f"[消息流: {self.chat_stream.stream_id}] 消息发送或存储时出现错误: {e}", exc_info=True)

            finally:
                # CRUCIAL: Always call task_done() for any item that was successfully retrieved.
                self.queue.task_done()

    def start(self):
        """启动发送任务。"""
        if self._task is None:
            self._task = asyncio.create_task(self._send_worker())

    async def join(self):
        """等待所有消息发送完毕。"""
        if self._task:
            await self._task


class S4UChatManager:
    def __init__(self):
        self.s4u_chats: Dict[str, "S4UChat"] = {}

    def get_or_create_chat(self, chat_stream: ChatStream) -> "S4UChat":
        if chat_stream.stream_id not in self.s4u_chats:
            stream_name = get_chat_manager().get_stream_name(chat_stream.stream_id) or chat_stream.stream_id
            logger.info(f"Creating new S4UChat for stream: {stream_name}")
            self.s4u_chats[chat_stream.stream_id] = S4UChat(chat_stream)
        return self.s4u_chats[chat_stream.stream_id]


if not s4u_config.enable_s4u:
    s4u_chat_manager = None
else:
    s4u_chat_manager = S4UChatManager()


def get_s4u_chat_manager() -> S4UChatManager:
    return s4u_chat_manager


class S4UChat:
    def __init__(self, chat_stream: ChatStream):
        """初始化 S4UChat 实例。"""

        self.chat_stream = chat_stream
        self.stream_id = chat_stream.stream_id
        self.stream_name = get_chat_manager().get_stream_name(self.stream_id) or self.stream_id

        # 两个消息队列
        self._vip_queue = asyncio.PriorityQueue()
        self._normal_queue = asyncio.PriorityQueue()

        self._entry_counter = 0  # 保证FIFO的全局计数器
        self._new_message_event = asyncio.Event()  # 用于唤醒处理器

        self._processing_task = asyncio.create_task(self._message_processor())
        self._current_generation_task: Optional[asyncio.Task] = None
        # 当前消息的元数据：(队列类型, 优先级分数, 计数器, 消息对象)
        self._current_message_being_replied: Optional[Tuple[str, float, int, MessageRecv]] = None

        self._is_replying = False
        self.gpt = S4UStreamGenerator()
        self.gpt.chat_stream = self.chat_stream
        self.interest_dict: Dict[str, float] = {}  # 用户兴趣分

        self.internal_message: List[MessageRecvS4U] = []

        self.msg_id = ""
        self.voice_done = ""

        self._paused = False  # 是否暂停回应（由#stop_react触发）

        # Streamer Mode 主播模式状态
        self._last_valid_danmu_time = time.time()
        self._screen_talk_count = 0
        self._streamer_mode_config = s4u_config_main.streamer_mode
        # 计算当前会话是否启用主播模式（需在 room_ids 列表中）
        self._streamer_mode_active = self._check_streamer_mode_enabled()

        logger.info(
            f"[{self.stream_name}] S4UChat initialized. stream_id={self.stream_id}, StreamerMode={'ACTIVE' if self._streamer_mode_active else 'OFF'}, room_ids={self._streamer_mode_config.room_ids}"
        )

    def _check_streamer_mode_enabled(self) -> bool:
        """检查当前会话是否启用主播模式"""
        if not self._streamer_mode_config.enable:
            return False
        # 如果 room_ids 为空，则禁用（安全默认）
        if not self._streamer_mode_config.room_ids:
            return False
        # 检查当前 stream_id 是否在允许列表中
        return self.stream_id in self._streamer_mode_config.room_ids

    def stop_react(self):
        """停止回应并清空所有待处理消息"""
        logger.warning(f"[{self.stream_name}] 🛑 Streamer Mode PAUSED by command.")
        self._paused = True

        # 1. 清空队列
        while not self._vip_queue.empty():
            try:
                self._vip_queue.get_nowait()
                self._vip_queue.task_done()
            except asyncio.QueueEmpty:
                break

        while not self._normal_queue.empty():
            try:
                self._normal_queue.get_nowait()
                self._normal_queue.task_done()
            except asyncio.QueueEmpty:
                break

        self.internal_message.clear()

        # 2. 取消当前正在生成的任务
        if self._current_generation_task and not self._current_generation_task.done():
            logger.info(f"[{self.stream_name}] Cancelling current generation task...")
            self._current_generation_task.cancel()

        # 3. 如果正在进行屏幕自言自语，也应该尝试打断（通过cancel message sender）
        # 目前 MessageSenderContainer 没有直接暴露，但在 generate_and_send 中如果 task 被 verify_cancel，应该能停止

    def start_react(self):
        """恢复回应"""
        logger.warning(f"[{self.stream_name}] ▶️ Streamer Mode RESUMED by command.")
        self._paused = False
        # 唤醒处理循环
        self._new_message_event.set()

    def _get_priority_info(self, message: MessageRecv) -> dict:
        """安全地从消息中提取和解析 priority_info"""
        priority_info_raw = message.priority_info
        priority_info = {}
        if isinstance(priority_info_raw, str):
            try:
                priority_info = json.loads(priority_info_raw)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse priority_info JSON: {priority_info_raw}")
        elif isinstance(priority_info_raw, dict):
            priority_info = priority_info_raw
        return priority_info

    def _is_vip(self, priority_info: dict) -> bool:
        """检查消息是否来自VIP用户。"""
        return priority_info.get("message_type") == "vip"

    def _get_interest_score(self, user_id: str) -> float:
        """获取用户的兴趣分，默认为1.0"""
        return self.interest_dict.get(user_id, 1.0)

    def go_processing(self):
        if self.voice_done == self.last_msg_id:
            return True
        return False

    def _calculate_base_priority_score(self, message: MessageRecv, priority_info: dict) -> float:
        """
        为消息计算基础优先级分数。分数越高，优先级越高。
        """
        score = 0.0

        # 加上消息自带的优先级
        score += priority_info.get("message_priority", 0.0)

        # 加上用户的固有兴趣分
        score += self._get_interest_score(message.message_info.user_info.user_id)
        return score

    def decay_interest_score(self):
        for person_id, score in self.interest_dict.items():
            if score > 0:
                self.interest_dict[person_id] = score * 0.95
            else:
                self.interest_dict[person_id] = 0

    async def add_message(self, message: MessageRecvS4U | MessageRecv) -> None:
        self.decay_interest_score()

        """根据VIP状态和中断逻辑将消息放入相应队列。"""
        user_id = message.message_info.user_info.user_id
        platform = message.message_info.platform
        _person_id = get_person_id(platform, user_id)

        # try:
        #     is_gift = message.is_gift
        #     is_superchat = message.is_superchat
        #     # print(is_gift)
        #     # print(is_superchat)
        #     if is_gift:
        #         await self.relationship_builder.build_relation(immediate_build=person_id)
        #         # 安全地增加兴趣分，如果person_id不存在则先初始化为1.0
        #         current_score = self.interest_dict.get(person_id, 1.0)
        #         self.interest_dict[person_id] = current_score + 0.1 * message.gift_count
        #     elif is_superchat:
        #         await self.relationship_builder.build_relation(immediate_build=person_id)
        #         # 安全地增加兴趣分，如果person_id不存在则先初始化为1.0
        #         current_score = self.interest_dict.get(person_id, 1.0)
        #         self.interest_dict[person_id] = current_score + 0.1 * float(message.superchat_price)

        #         # 添加SuperChat到管理器
        #         super_chat_manager = get_super_chat_manager()
        #         await super_chat_manager.add_superchat(message)
        #     else:
        #         await self.relationship_builder.build_relation(20)
        # except Exception:
        #     traceback.print_exc()

        logger.info(f"[{self.stream_name}] 消息处理完毕，消息内容：{message.processed_plain_text}")

        # 主播模式：使用 DanmuScorer 过滤低价值弹幕
        if self._streamer_mode_active:
            try:
                from .danmu_scorer import get_danmu_scorer

                scorer = get_danmu_scorer()
                user_name = message.message_info.user_info.user_nickname or str(user_id)
                score = await scorer.score_single(message.processed_plain_text, user_name)

                if not scorer.is_valid(score):
                    logger.info(
                        f"[{self.stream_name}] 弹幕被过滤 (score={score:.2f}): {message.processed_plain_text[:30]}..."
                    )
                    return  # 不入队，直接丢弃
                else:
                    logger.debug(f"[{self.stream_name}] 弹幕有效 (score={score:.2f})")
                    # 重置空闲计时和自言自语计数
                    self._last_valid_danmu_time = time.time()
                    self._screen_talk_count = 0
            except Exception as e:
                logger.warning(f"[{self.stream_name}] DanmuScorer 调用失败，跳过过滤: {e}")

        priority_info = self._get_priority_info(message)
        is_vip = self._is_vip(priority_info)
        new_priority_score = self._calculate_base_priority_score(message, priority_info)

        should_interrupt = False
        if (
            s4u_config.enable_message_interruption
            and self._current_generation_task
            and not self._current_generation_task.done()
        ):
            if self._current_message_being_replied:
                current_queue, current_priority, _, current_msg = self._current_message_being_replied

                # 规则：VIP从不被打断
                if current_queue == "vip":
                    pass  # Do nothing

                # 规则：普通消息可以被打断
                elif current_queue == "normal":
                    # VIP消息可以打断普通消息
                    if is_vip:
                        should_interrupt = True
                        logger.info(f"[{self.stream_name}] VIP message received, interrupting current normal task.")
                    # 普通消息的内部打断逻辑
                    else:
                        new_sender_id = message.message_info.user_info.user_id
                        current_sender_id = current_msg.message_info.user_info.user_id
                        # 新消息优先级更高
                        if new_priority_score > current_priority:
                            should_interrupt = True
                            logger.info(f"[{self.stream_name}] New normal message has higher priority, interrupting.")
                        # 同用户，新消息的优先级不能更低
                        elif new_sender_id == current_sender_id and new_priority_score >= current_priority:
                            should_interrupt = True
                            logger.info(f"[{self.stream_name}] Same user sent new message, interrupting.")

        if should_interrupt:
            if self.gpt.partial_response:
                logger.warning(
                    f"[{self.stream_name}] Interrupting reply. Already generated: '{self.gpt.partial_response}'"
                )
            self._current_generation_task.cancel()

        # asyncio.PriorityQueue 是最小堆，所以我们存入分数的相反数
        # 这样，原始分数越高的消息，在队列中的优先级数字越小，越靠前
        item = (-new_priority_score, self._entry_counter, time.time(), message)

        if is_vip and s4u_config.vip_queue_priority:
            await self._vip_queue.put(item)
            logger.info(f"[{self.stream_name}] VIP message added to queue.")
        else:
            await self._normal_queue.put(item)

        self._entry_counter += 1
        self._new_message_event.set()  # 唤醒处理器

    def _cleanup_old_normal_messages(self):
        """清理普通队列中不在最近N条消息范围内的消息"""
        if not s4u_config.enable_old_message_cleanup or self._normal_queue.empty():
            return

        # 计算阈值：保留最近 recent_message_keep_count 条消息
        cutoff_counter = max(0, self._entry_counter - s4u_config.recent_message_keep_count)

        # 临时存储需要保留的消息
        temp_messages = []
        removed_count = 0

        # 取出所有普通队列中的消息
        while not self._normal_queue.empty():
            try:
                item = self._normal_queue.get_nowait()
                neg_priority, entry_count, timestamp, message = item

                # 如果消息在最近N条消息范围内，保留它
                logger.info(
                    f"检查消息:{message.processed_plain_text},entry_count:{entry_count} cutoff_counter:{cutoff_counter}"
                )

                if entry_count >= cutoff_counter:
                    temp_messages.append(item)
                else:
                    removed_count += 1
                    self._normal_queue.task_done()  # 标记被移除的任务为完成

            except asyncio.QueueEmpty:
                break

        # 将保留的消息重新放入队列
        for item in temp_messages:
            self._normal_queue.put_nowait(item)

        if removed_count > 0:
            logger.info(
                f"消息{message.processed_plain_text}超过{s4u_config.recent_message_keep_count}条，现在counter:{self._entry_counter}被移除"
            )
            logger.info(
                f"[{self.stream_name}] Cleaned up {removed_count} old normal messages outside recent {s4u_config.recent_message_keep_count} range."
            )

    async def _message_processor(self):
        """调度器：优先处理VIP队列，然后处理普通队列。支持主播模式空闲自言自语。"""
        while True:
            try:
                # PAUSE CHECK: 检查暂停状态，放在循环最开始
                if self._paused:
                    logger.debug(f"[{self.stream_name}] Paused. Waiting for resume...")
                    # 如果暂停，直接等待被唤醒（通过 start_react）
                    await self._new_message_event.wait()
                    self._new_message_event.clear()
                    # 唤醒后再次检查，如果还是暂停（比如被stop_react唤醒），继续等待
                    if self._paused:
                        continue

                # 主播模式：带超时的等待，超时后触发屏幕自言自语
                if self._streamer_mode_active:
                    try:
                        # Randomize idle timeout (20s - 40s)
                        idle_timeout = random.randint(20, 40)
                        await asyncio.wait_for(self._new_message_event.wait(), timeout=idle_timeout)
                    except asyncio.TimeoutError:
                        # 再次检查暂停状态，防止在等待期间被暂停但尚未处理
                        if self._paused:
                            continue

                        # 超时 → 尝试触发屏幕自言自语
                        await self._try_screen_self_talk()
                        continue
                else:
                    # 普通模式：无限等待
                    await self._new_message_event.wait()

                self._new_message_event.clear()

                # 再次检查暂停（双重保险）
                if self._paused:
                    continue

                # 清理普通队列中的过旧消息
                self._cleanup_old_normal_messages()

                # 优先处理VIP队列
                if not self._vip_queue.empty():
                    neg_priority, entry_count, _, message = self._vip_queue.get_nowait()
                    priority = -neg_priority
                    queue_name = "vip"
                # 其次处理普通队列
                elif not self._normal_queue.empty():
                    neg_priority, entry_count, timestamp, message = self._normal_queue.get_nowait()
                    priority = -neg_priority
                    # 检查普通消息是否超时
                    if time.time() - timestamp > s4u_config.message_timeout_seconds:
                        logger.info(
                            f"[{self.stream_name}] Discarding stale normal message: {message.processed_plain_text[:20]}..."
                        )
                        self._normal_queue.task_done()
                        continue  # 处理下一条
                    queue_name = "normal"
                else:
                    if self.internal_message:
                        message = self.internal_message[-1]
                        self.internal_message = []

                        priority = 0
                        neg_priority = 0
                        entry_count = 0
                        queue_name = "internal"

                        logger.info(
                            f"[{self.stream_name}] normal/vip 队列都空，触发 internal_message 回复: {getattr(message, 'processed_plain_text', str(message))[:20]}..."
                        )
                    else:
                        continue  # 没有消息了，回去等事件

                self._current_message_being_replied = (queue_name, priority, entry_count, message)
                self._current_generation_task = asyncio.create_task(self._generate_and_send(message))

                try:
                    await self._current_generation_task
                except asyncio.CancelledError:
                    logger.info(
                        f"[{self.stream_name}] Reply generation was interrupted externally for {queue_name} message. The message will be discarded."
                    )
                    # 被中断的消息应该被丢弃，而不是重新排队，以响应最新的用户输入。
                    # 旧的重新入队逻辑会导致所有中断的消息最终都被回复。

                except Exception as e:
                    logger.error(f"[{self.stream_name}] _generate_and_send task error: {e}", exc_info=True)
                finally:
                    self._current_generation_task = None
                    self._current_message_being_replied = None
                    # 标记任务完成
                    if queue_name == "vip":
                        self._vip_queue.task_done()
                    elif queue_name == "internal":
                        # 如果使用 internal_message 生成回复，则不从 normal 队列中移除
                        pass
                    else:
                        self._normal_queue.task_done()

                    # 检查是否还有任务，有则立即再次触发事件
                    if not self._vip_queue.empty() or not self._normal_queue.empty():
                        self._new_message_event.set()

            except asyncio.CancelledError:
                logger.info(f"[{self.stream_name}] Message processor is shutting down.")
                break
            except Exception as e:
                logger.error(f"[{self.stream_name}] Message processor main loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

    def get_processing_message_id(self):
        self.last_msg_id = self.msg_id
        self.msg_id = f"{time.time()}_{random.randint(1000, 9999)}"

    async def _try_screen_self_talk(self):
        """
        主播模式：尝试触发屏幕自言自语
        当无有效弹幕且未超过最大自言自语次数时触发
        """
        # 检查是否超过最大自言自语次数
        if self._screen_talk_count >= self._streamer_mode_config.max_screen_talk_count:
            logger.debug(f"[{self.stream_name}] 已达到最大屏幕自言自语次数 ({self._screen_talk_count})")
            return

        # 导入 prompt_builder 和 screen_manager
        from .s4u_prompt import prompt_builder
        from .screen_manager import screen_manager

        # 检查是否有屏幕内容可用（使用 get_screen 而非 get_screen_str，后者在屏幕关闭时仍返回非空回退文本）
        screen_info = screen_manager.get_screen()
        if not screen_info:
            logger.debug(f"[{self.stream_name}] 无屏幕内容可用，跳过自言自语")
            return

        logger.info(f"[{self.stream_name}] 触发屏幕自言自语 (第 {self._screen_talk_count + 1} 次)")

        # 构建屏幕自言自语 prompt
        prompt = await prompt_builder.build_screen_talk_prompt(self.chat_stream)

        # 使用 LLM 生成
        try:
            response = ""
            async for chunk in self.gpt._generate_response_with_llm_request(prompt):
                response += chunk

            if response.strip():
                # Send sentences sequentially for better subtitle sync
                sentences = await split_bilingual_text(response.strip())
                for i, sentence in enumerate(sentences):
                    # Parse bilingual content first to get JP length
                    chat_text, tts_text = parse_bilingual_content(sentence)

                    # Send FIRST
                    await self._send_screen_talk_response(chat_text, tts_text)
                    logger.info(
                        f"[{self.stream_name}] Screen talk sentence {i + 1}/{len(sentences)} sent: {chat_text[:10]}..."
                    )

                    # Calculate Wait Duration based on JP TTS length
                    # JP TTS speed approx 5 chars/sec.
                    # Add base buffer of 1.0s
                    jp_len = len(tts_text)
                    audio_duration = (jp_len / 5.0) + 0.5

                    # Ensure min/max
                    delay = max(2.0, min(audio_duration, 10.0))

                    # Sleep AFTER sending to let audio play before next subtitle
                    await asyncio.sleep(delay)

                self._screen_talk_count += 1
                self._last_valid_danmu_time = time.time()
                logger.info(f"[{self.stream_name}] Screen talk complete provided full response: {response[:50]}...")
        except Exception as e:
            logger.error(f"[{self.stream_name}] 屏幕自言自语生成失败: {e}")

    async def _send_screen_talk_response(self, chat_text: str, tts_text: str):
        """发送屏幕自言自语的响应"""
        self.get_processing_message_id()

        # Already parsed by caller
        # chat_text, tts_text = parse_bilingual_content(response)

        # 1. 发送 TTS (使用日文)
        message_segment = Seg(type="tts_text", data=tts_text)
        bot_message = MessageSending(
            message_id=self.msg_id,
            chat_stream=self.chat_stream,
            bot_user_info=UserInfo(
                user_id=global_config.bot.qq_account,
                user_nickname=global_config.bot.nickname,
                platform="bilibili",  # 假设是 bilibili
            ),
            sender_info=UserInfo(
                user_id="system",
                user_nickname="系统",
                platform="bilibili",
            ),
            message_segment=message_segment,
            reply=None,
            is_emoji=False,
            apply_set_reply_logic=False,
            reply_to=None,
        )

        await bot_message.process()
        await get_global_api().send_message(bot_message)
        logger.info(f"[{self.stream_name}] 已发送屏幕自言自语 (TTS): {tts_text}")

        # 2. 发送字幕 (使用中文)
        message_segment = Seg(type="text", data=chat_text)
        bot_message = MessageSending(
            message_id=self.msg_id,
            chat_stream=self.chat_stream,
            bot_user_info=UserInfo(
                user_id=global_config.bot.qq_account,
                user_nickname=global_config.bot.nickname,
                platform="bilibili",
            ),
            sender_info=UserInfo(
                user_id="system",
                user_nickname="系统",
                platform="bilibili",
            ),
            message_segment=message_segment,
            reply=None,
            is_emoji=False,
            apply_set_reply_logic=False,
            reply_to=None,
        )
        await bot_message.process()

        # 自言自语也需要存储吗？暂时不需要或者存入特殊的？
        # 这里仅发送
        await get_global_api().send_message(bot_message)
        logger.info(f"[{self.stream_name}] 已发送屏幕自言自语 (Text): {chat_text}")

    async def _generate_and_send(self, message: MessageRecv):
        """为单个消息生成文本回复。整个过程可以被中断。"""
        self._is_replying = True
        total_chars_sent = 0  # 跟踪发送的总字符数

        self.get_processing_message_id()

        # 视线管理：开始生成回复时切换视线状态
        chat_watching = watching_manager.get_watching_by_chat_id(self.stream_id)

        if message.is_internal:
            await chat_watching.on_internal_message_start()
        else:
            await chat_watching.on_reply_start()

        sender_container = MessageSenderContainer(self.chat_stream, message)
        sender_container.start()

        async def generate_and_send_inner():
            nonlocal total_chars_sent
            logger.info(f"[S4U] 开始为消息生成文本和音频流: '{message.processed_plain_text[:30]}...'")

            # 主播模式：使用动态长度和段间等待
            if self._streamer_mode_active:
                logger.info("[S4U] 主播模式：使用动态长度生成")
                # 获取当前队列中的有效弹幕数量作为参考
                valid_danmu_count = self._normal_queue.qsize() + self._vip_queue.qsize()
                gen = self.gpt.generate_response_with_dynamic_length(message, valid_danmu_count, "")
                async for segment in gen:
                    sender_container.msg_id = self.msg_id
                    await sender_container.add_message(segment)
                    total_chars_sent += len(segment)
            elif s4u_config.enable_streaming_output:
                logger.info("[S4U] 开始流式输出")
                # 流式输出，边生成边发送
                gen = self.gpt.generate_response(message, "")
                async for chunk in gen:
                    sender_container.msg_id = self.msg_id
                    await sender_container.add_message(chunk)
                    total_chars_sent += len(chunk)
            else:
                logger.info("[S4U] 开始一次性输出")
                # 一次性输出，先收集所有chunk
                all_chunks = []
                gen = self.gpt.generate_response(message, "")
                async for chunk in gen:
                    all_chunks.append(chunk)
                    total_chars_sent += len(chunk)
                # 一次性发送
                sender_container.msg_id = self.msg_id
                await sender_container.add_message("".join(all_chunks))

        try:
            try:
                # 移除超时限制
                # await asyncio.wait_for(generate_and_send_inner(), timeout=10)
                await generate_and_send_inner()
            except Exception as e:
                logger.error(f"[{self.stream_name}] 回复生成过程中出现错误: {e}", exc_info=True)
                # sender_container.msg_id = self.msg_id
                # await sender_container.add_message("NachoBot不知道哦")
                # total_chars_sent = len("NachoBot不知道哦")

            mood = mood_manager.get_mood_by_chat_id(self.stream_id)
            await yes_or_no_head(
                text=total_chars_sent,
                emotion=mood.mood_state,
                chat_history=message.processed_plain_text,
                chat_id=self.stream_id,
            )

            # 等待所有文本消息发送完成
            await sender_container.close()
            await sender_container.join()

            await chat_watching.on_thinking_finished()

            start_time = time.time()
            logged = False
            while not self.go_processing():
                if time.time() - start_time > 60:
                    logger.warning(f"[{self.stream_name}] 等待消息发送超时（60秒），强制跳出循环。")
                    break
                if not logged:
                    logger.info(f"[{self.stream_name}] 等待消息发送完成...")
                    logged = True
                await asyncio.sleep(0.2)

            logger.info(f"[{self.stream_name}] 所有文本块处理完毕。")

        except asyncio.CancelledError:
            logger.info(f"[{self.stream_name}] 回复流程（文本）被中断。")
            raise  # 将取消异常向上传播
        except Exception as e:
            traceback.print_exc()
            logger.error(f"[{self.stream_name}] 回复生成过程中出现错误: {e}", exc_info=True)
            # 回复生成实时展示：清空内容（出错时）
        finally:
            self._is_replying = False

            # 视线管理：回复结束时切换视线状态
            chat_watching = watching_manager.get_watching_by_chat_id(self.stream_id)
            await chat_watching.on_reply_finished()

            # 确保发送器被妥善关闭（即使已关闭，再次调用也是安全的）
            sender_container.resume()
            if not sender_container._task.done():
                await sender_container.close()
                await sender_container.join()
            logger.info(f"[{self.stream_name}] _generate_and_send 任务结束，资源已清理。")

    async def shutdown(self):
        """平滑关闭处理任务。"""
        logger.info(f"正在关闭 S4UChat: {self.stream_name}")

        # 取消正在运行的任务
        if self._current_generation_task and not self._current_generation_task.done():
            self._current_generation_task.cancel()

        if self._processing_task and not self._processing_task.done():
            self._processing_task.cancel()

        # 等待任务响应取消
        try:
            await self._processing_task
        except asyncio.CancelledError:
            logger.info(f"处理任务已成功取消: {self.stream_name}")
