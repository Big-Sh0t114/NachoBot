"""预约提醒的 LLM 结构化解析与防误触发闸口。

借鉴 A_chatter 的 parser 设计，在真正创建预约之前做三道判断：

1. 类型归一化纠偏：判断这条消息是否真的是「让 bot 在某时间提醒」的请求
   （is_reminder）。像「今天有人提醒我做核酸真烦」这种只是提到「提醒」二字、
   并非要求 bot 设定提醒的句子，会被纠正为 is_reminder=False，不再误建预约。

2. confidence 卡口：LLM 对解析结果的置信度低于阈值时，不直接创建，转为追问。

3. ambiguities 卡口：时间表达缺日期/具体钟点（如「晚上」「明天提醒我」）时，
   把缺失项写入 ambiguities，不自行默认补全，转为追问用户。

解析失败或 LLM 不可用时，降级回退到原有的正则时间解析，保证可用性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api

from src.chat.heart_flow.appointment_scheduler import parse_remind_time

logger = get_logger("appointment_parser")

# confidence 低于该阈值视为不够明确，需要追问
CONFIDENCE_THRESHOLD = 0.7
_DEFAULT_TZ = "Asia/Shanghai"


@dataclass
class AppointmentParseResult:
    """预约解析结果。"""

    is_reminder: bool
    remind_datetime: Optional[datetime]
    remind_content: str
    confidence: float
    ambiguities: List[str] = field(default_factory=list)
    # 解析是否真正经过 LLM（False 表示走了正则回退）
    parsed_by_llm: bool = False

    @property
    def needs_clarification(self) -> bool:
        """是否需要追问用户（双卡口）。"""
        return self.confidence < CONFIDENCE_THRESHOLD or bool(self.ambiguities)


def _build_prompt(text: str, remind_content_hint: str, tz_name: str) -> str:
    now = datetime.now(ZoneInfo(tz_name))
    return f"""你是一个定时提醒解析器。请判断用户消息是否真的是在要求机器人「到某个时间提醒/叫/通知」他，并解析出结构化信息。
最终回复必须只包含一个 JSON 对象，不要输出推理过程、解释文字、Markdown 代码块或 JSON 之外的任何字符。

当前时间：{now.isoformat()}
默认时区：{tz_name}

用户最新消息：
{text}

planner 提取的提醒内容（仅供参考，可能不准）：{remind_content_hint}

输出 JSON 格式：
{{
  "is_reminder": true,
  "remind_at": "带时区 ISO 时间，例如 {now.year}-{now.month:02d}-{now.day:02d}T20:00:00+08:00，无法确定则留空",
  "remind_content": "到点要提醒用户的事项，去掉时间和指令词，例如「交报告」「起床」",
  "confidence": 0.0,
  "ambiguities": []
}}

判断规则：
1. is_reminder 只有在用户明确要求机器人「在某个时间点/一段时间后提醒、叫、通知、喊」他时才为 true。
2. 下列情况 is_reminder 必须为 false：
   - 只是叙述、吐槽、转述里出现「提醒」「叫我」等词，并非要求机器人设定提醒（如「今天有人提醒我做核酸真烦」）。
   - 在问机器人问题、聊天、表达情绪，没有「请你到时提醒我」的真实意图。
   - 要求的是查询、取消已有提醒，而不是新建提醒。
3. 所有时间必须转为带时区的绝对 ISO 时间，禁止输出「明天」「晚上」等相对表达。
4. 如果时间表达缺少日期或具体钟点（如「晚上」「晚点」「明天提醒我」没说几点），把缺失项写进 ambiguities，并且 remind_at 留空，不要自行默认补全。
5. confidence 表示你对「这确实是一条提醒请求且时间内容都解析正确」的整体置信度，范围 0 到 1。不确定就给低分。
6. is_reminder 为 false 时，confidence 表示你对「这不是提醒请求」的置信度。
"""


def _extract_json(response_text: str) -> Optional[dict]:
    text = str(response_text or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"[预约解析] JSON 解析失败: {exc}")
        return None
    return payload if isinstance(payload, dict) else None


def _parse_remind_at(value: object, tz_name: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        # LLM 可能仍输出相对表达，退回正则解析
        return parse_remind_time(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def _regex_fallback(remind_time_raw: str, remind_content: str) -> AppointmentParseResult:
    """LLM 不可用时的降级：沿用正则解析，置信度保守。"""
    remind_datetime = parse_remind_time(remind_time_raw)
    return AppointmentParseResult(
        is_reminder=True,
        remind_datetime=remind_datetime,
        remind_content=remind_content,
        # 没经过类型纠偏，给中等偏上置信度，让原有时间校验继续把关
        confidence=0.75 if remind_datetime else 0.5,
        ambiguities=[] if remind_datetime else ["时间无法识别"],
        parsed_by_llm=False,
    )


async def parse_appointment_request(
    *,
    message_text: str,
    remind_time_raw: str,
    remind_content_hint: str,
    tz_name: str = _DEFAULT_TZ,
) -> AppointmentParseResult:
    """对一条疑似预约请求做结构化解析与防误触发判断。

    Args:
        message_text: 用户最新消息原文，用于类型判断。
        remind_time_raw: planner 提取的时间字符串，用于正则回退。
        remind_content_hint: planner 提取的提醒内容，供 LLM 参考。
        tz_name: 时区名。

    Returns:
        AppointmentParseResult，调用方据此决定创建预约还是追问/回复。
    """
    text = str(message_text or "").strip()

    models = llm_api.get_available_models()
    model_config = models.get("utils") or models.get("utils_small")
    if not text or model_config is None:
        logger.debug("[预约解析] 缺少文本或可用模型，回退正则解析")
        return _regex_fallback(remind_time_raw, remind_content_hint)

    prompt = _build_prompt(text, remind_content_hint, tz_name)
    try:
        success, response, _reasoning, _model_name = await llm_api.generate_with_model(
            prompt=prompt,
            model_config=model_config,
            request_type="appointment.parse",
            temperature=0.1,
            max_tokens=600,
        )
    except Exception as exc:
        logger.error(f"[预约解析] LLM 调用异常: {exc}")
        return _regex_fallback(remind_time_raw, remind_content_hint)

    if not success:
        logger.warning(f"[预约解析] LLM 生成失败: {response}")
        return _regex_fallback(remind_time_raw, remind_content_hint)

    payload = _extract_json(response)
    if payload is None:
        logger.warning("[预约解析] LLM 未返回有效 JSON，回退正则解析")
        return _regex_fallback(remind_time_raw, remind_content_hint)

    is_reminder = bool(payload.get("is_reminder", False))
    confidence = 0.0
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (ValueError, TypeError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    ambiguities = [str(item).strip() for item in payload.get("ambiguities", []) if str(item).strip()]
    remind_content = str(payload.get("remind_content") or remind_content_hint or "提醒").strip()
    remind_datetime = _parse_remind_at(payload.get("remind_at"), tz_name)

    # 类型归一化纠偏：不是真正的提醒请求时，清空时间，避免误建
    if not is_reminder:
        remind_datetime = None

    # 缺时间但 LLM 没标注歧义时，补一条，确保进入追问而非静默失败
    if is_reminder and remind_datetime is None and not ambiguities:
        ambiguities.append("提醒时间不明确")

    logger.info(
        f"[预约解析] is_reminder={is_reminder}, confidence={confidence:.2f}, "
        f"ambiguities={ambiguities}, content={remind_content}"
    )

    return AppointmentParseResult(
        is_reminder=is_reminder,
        remind_datetime=remind_datetime,
        remind_content=remind_content,
        confidence=confidence,
        ambiguities=ambiguities,
        parsed_by_llm=True,
    )
