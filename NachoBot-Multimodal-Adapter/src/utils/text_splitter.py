"""文本切句工具

从 vox_api_server.py 提取的文本切句逻辑，供 TTS Adapter 在 Adapter 层进行句级切分。
用于分段流式输出场景：将长文本按句拆分，逐句生成并发送语音。
支持动态读取配置文件中的切句方案（如 cut0 ~ cut5）。
"""

import re
import toml
from pathlib import Path
from typing import List, Tuple


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


def get_split_config() -> Tuple[str, int]:
    """读取配置文件获取当前启用的切句方案及最大长度"""
    try:
        # text_splitter.py 位于 NachoBot-Multimodal-Adapter/src/utils/text_splitter.py
        # configs 目录在 text_splitter.py 向上两级
        configs_dir = Path(__file__).resolve().parents[2] / "configs"
        base_toml_path = configs_dir / "base.toml"
        
        if base_toml_path.exists():
            with open(base_toml_path, "r", encoding="utf-8") as f:
                base_data = toml.load(f)
            
            enabled_tts = base_data.get("enabled_tts", {}).get("enabled", [])
            if enabled_tts:
                active_plugin = enabled_tts[0]
                if active_plugin == "Vox":
                    vox_toml_path = configs_dir / "vox.toml"
                    if vox_toml_path.exists():
                        with open(vox_toml_path, "r", encoding="utf-8") as f:
                            vox_data = toml.load(f)
                        split_method = vox_data.get("tts", {}).get("split_method", "cut3")
                        max_split_length = vox_data.get("tts", {}).get("max_split_length", 80)
                        return split_method, max_split_length
                elif active_plugin == "GPT_Sovits":
                    gpt_toml_path = configs_dir / "gpt-sovits.toml"
                    if gpt_toml_path.exists():
                        with open(gpt_toml_path, "r", encoding="utf-8") as f:
                            gpt_data = toml.load(f)
                        split_method = gpt_data.get("tts", {}).get("text_split_method", "cut5")
                        max_split_length = gpt_data.get("tts", {}).get("max_split_length", 80)
                        return split_method, max_split_length
    except Exception:
        pass
    
    # 默认兜底
    return "cut3", 80


def split_text_for_streaming(text: str, min_segment_length: int = 10) -> List[str]:
    """为分段流式输出切分文本，切分方案动态读取配置文件中的设置。

    Args:
        text: 要切分的文本
        min_segment_length: 最小段长度，过短的段会被合并

    Returns:
        切分后的文本片段列表
    """
    text = text.strip()
    if not text:
        return []

    method, max_length = get_split_config()

    if method == "cut0":
        return [text]

    # 定义各级标点
    punct_level1 = "。！？!?."  # 句号、感叹号、问号
    punct_level2 = punct_level1 + "，,、"  # 增加逗号、顿号
    punct_level3 = punct_level2 + "；;：:…—"  # 增加分号、冒号、省略号、破折号

    if method == "cut1":
        segments = _split_by_punctuation(text, punct_level1)
    elif method == "cut2":
        segments = _split_by_punctuation(text, punct_level2)
    elif method == "cut3":
        segments = _split_by_punctuation(text, punct_level3)
    elif method == "cut4":
        # 纯按字符数切分
        segments = [text[i:i + max_length] for i in range(0, len(text), max_length)]
    elif method == "cut5":
        # 先按标点切，再对超长段二次切分
        segments = _split_by_punctuation(text, punct_level3)
        final = []
        for seg in segments:
            if len(seg) > max_length:
                final.extend(seg[i:i + max_length] for i in range(0, len(seg), max_length))
            else:
                final.append(seg)
        segments = final
    else:
        segments = _split_by_punctuation(text, punct_level3)

    # 合并过短片段
    segments = _merge_short_segments(segments, min_length=min_segment_length)
    return segments if segments else [text]
