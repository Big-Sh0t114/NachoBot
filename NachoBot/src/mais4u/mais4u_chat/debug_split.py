import re
from typing import List


def split_bilingual_text(text: str) -> List[str]:
    """
    Split text into sentences based on bilingual format or punctuation.
    Further split long sentences by commas to ensure subtitles are short (~10-15 chars).
    """
    sentences = []

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
        # Fallback: Split by sentence punctuation
        # Added . for dot support
        parts = re.split(r"([。！？!?.])", text)
        current = ""
        for p in parts:
            current += p
            if re.match(r"[。！？!?.]", p):
                primary_sentences.append(current.strip())
                current = ""
        if current.strip():
            primary_sentences.append(current.strip())

    # 2. Second Pass: Sub-split long sentences by commas
    final_sentences = []

    # Threshold for splitting (User asked for ~10 chars, set soft limit)
    LENGTH_THRESHOLD = 10

    for sentence in primary_sentences:
        # Check if sentence has JP part
        jp_match = re.search(r"[(（](.*?)[)）]", sentence, re.DOTALL)

        if jp_match:
            # Has JP: Split content before JP
            jp_full_content = jp_match.group(1)  # Content inside brackets
            jp_start = jp_match.start()
            jp_end = jp_match.end()

            content_part = sentence[:jp_start]
            tail_part = sentence[jp_end:]  # Punctuation after JP

            # Only split if content is long enough
            if len(content_part) > LENGTH_THRESHOLD:
                # Split content (Chinese) by commas
                cn_parts_raw = re.split(r"([，,、\s]+)", content_part)
                cn_parts = []
                current_cn = ""
                for p in cn_parts_raw:
                    current_cn += p
                    if re.match(r"[，,、\s]+", p):
                        if current_cn.strip():
                            cn_parts.append(current_cn.strip())
                        current_cn = ""
                if current_cn.strip():
                    cn_parts.append(current_cn.strip())

                # Split Japanese
                jp_parts_raw = re.split(r"([、，,]+)", jp_full_content)
                jp_parts = []
                current_jp = ""
                for p in jp_parts_raw:
                    current_jp += p
                    if re.match(r"[、，,]+", p):
                        if current_jp.strip():
                            jp_parts.append(current_jp.strip())
                        current_jp = ""
                if current_jp.strip():
                    jp_parts.append(current_jp.strip())

                if len(cn_parts) > 1:
                    combined_chunks = []
                    limit = min(len(cn_parts), len(jp_parts))
                    if limit > 0:
                        for i in range(limit - 1):
                            chunk_cn = cn_parts[i]
                            chunk_jp = jp_parts[i]
                            combined_chunks.append(f"{chunk_cn} ({chunk_jp})")

                        rest_cn = "".join(cn_parts[limit - 1 :])
                        rest_jp = "".join(jp_parts[limit - 1 :])

                        combined_chunks.append(f"{rest_cn} ({rest_jp}){tail_part}")
                        final_sentences.extend(combined_chunks)
                    else:
                        final_sentences.append(sentence)
                else:
                    final_sentences.append(sentence)
            else:
                final_sentences.append(sentence)
        else:
            # Text only (Remainder or non-bilingual)
            if len(sentence) > LENGTH_THRESHOLD:
                # Added . to delimiters
                sub_parts_raw = re.split(r"([，,、\s.]+)", sentence)
                current = ""
                for p in sub_parts_raw:
                    current += p
                    if re.match(r"[，,、\s.]+", p):
                        if current.strip():
                            final_sentences.append(current.strip())
                        current = ""
                if current.strip():
                    final_sentences.append(current.strip())
            else:
                final_sentences.append(sentence)

    return [s for s in final_sentences if s]


# Sample text from image (approximate)
text = "哈啊,主人的屏幕上好像什么都没有显示出来呢, 就是一片空白的样子是电脑在加载什么东西吗, 还说是只是单纯地打开了一个空白的窗口呀这种安静的氛围让我更困了呢.眼皮好重主人是在准备做什么吗, 要不要跟大家说说接下来打算干什么呀不然直播间一直这么安静的话, 观众们也会觉得无聊跑掉的吧虽然我也挺享受这种安静的感觉就是了.但还是要活跃一下气氛比较好吧"

result = split_bilingual_text(text)
print("Original Length:", len(text))
print("Split Sentences Count:", len(result))
for i, s in enumerate(result):
    print(f"{i}: {s}")
