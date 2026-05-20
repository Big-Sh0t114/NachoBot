"""文本切句工具

从 vox_api_server.py 提取的文本切句逻辑，供 TTS Adapter 在 Adapter 层进行句级切分。
用于分段流式输出场景：将长文本按句拆分，逐句生成并发送语音。
"""

import re
from typing import List


def _split_by_punctuation(text: str, punctuation_set: str) -> List[str]:
    """按指定标点切分文本，标点保留在前一段末尾"""
    pattern = "([" + re.escape(punctuation_set) + "]+)"
    parts = re.split(pattern, text)
    segments = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        # 如果下一个 part 是标点，拼接到当前段
        if i + 1 < len(parts) and re.fullmatch(pattern, parts[i + 1]):
            seg += parts[i + 1]
            i += 2
        else:
            i += 1
        seg = seg.strip()
        if seg:
            segments.append(seg)
    return segments


def _merge_short_segments(segments: List[str], min_length: int = 10) -> List[str]:
    """将过短的片段合并到相邻片段，避免碎片化"""
    if not segments:
        return segments
    merged = [segments[0]]
    for seg in segments[1:]:
        if len(merged[-1]) < min_length:
            merged[-1] += seg
        else:
            merged.append(seg)
    # 如果最后一段太短，合并到倒数第二段
    if len(merged) > 1 and len(merged[-1]) < min_length:
        merged[-2] += merged[-1]
        merged.pop()
    return merged


def split_text_for_streaming(text: str, min_segment_length: int = 10) -> List[str]:
    """为分段流式输出切分文本

    使用句号/感叹号/问号作为切分点（句级切分），适合 Adapter 层的分段流式发送。
    API Server 内部仍可对每段做更细粒度的子句切分以保证音色质量。

    Args:
        text: 要切分的文本
        min_segment_length: 最小段长度，过短的段会被合并

    Returns:
        切分后的文本片段列表
    """
    text = text.strip()
    if not text:
        return []

    # 仅按句级标点切分（句号、感叹号、问号）
    punct_sentence = "。！？!?."
    segments = _split_by_punctuation(text, punct_sentence)

    # 合并过短片段
    segments = _merge_short_segments(segments, min_length=min_segment_length)

    return segments if segments else [text]
