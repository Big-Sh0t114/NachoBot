"""
TTS 文本清洗工具

清除颜文字（kaomoji）、Unicode emoji、特殊符号等对语音合成有害的字符。
在文本送入 TTS 引擎之前统一调用 clean_text_for_tts() 即可。
"""

import re
import unicodedata

# ============================================================
# 1. 颜文字（Kaomoji）模式
#    采用列表匹配 + 括号型模式，避免过度匹配正常文本
# ============================================================

# 常见颜文字列表（精确匹配）
_COMMON_KAOMOJI = [
    # 哭泣类
    "(╥_╥)", "(T_T)", "(;_;)", "(ﾉД`)", "(´;ω;`)", "(ノ_<。)",
    "(T▽T)", "(ಥ_ಥ)", "(TдT)", "(T⌓T)",
    # 开心类
    "(≧∇≦)", "(＾▽＾)", "(^_^)", "(＾o＾)", "(✿◡‿◡)",
    "(◕ᴗ◕✿)", "(✧ω✧)", "(◕‿◕)", "(｡◕‿◕｡)", "(★ω★)",
    "(≧◡≦)", "(＾∀＾)", "(*≧ω≦)", "(*^▽^*)", "(●'◡'●)",
    # 生气/翻桌
    "(╯°□°)╯︵ ┻━┻", "(ノಠ益ಠ)ノ", "(╬ Ò﹏Ó)", "(╬▔皿▔)╯",
    "┬─┬ノ( º _ ºノ)",
    # 卖萌类
    "(・ω・)", "(・∀・)", "(=^・ω・^=)", "(●ˇ∀ˇ●)",
    "(｡♥‿♥｡)", "(灬ºωº灬)", "(*´▽`*)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)",
    "(✪ω✪)", "(*≧∀≦*)", "ヾ(≧▽≦*)o", "o(*≧▽≦)ツ",
    # 无语/困惑
    "(⊙_⊙)", "(°ー°〃)", "( ̄▽ ̄)", "(￣▽￣)", "┌(・。・)┘",
    "(ー_ー)!!", "(¬_¬)", "(-_-;)", "(；一_一)",
    # 其他
    "╮(╯▽╰)╭", "╰(*°▽°*)╯", "～(￣▽￣～)", "(～￣▽￣)～",
    "ヽ(✿ﾟ▽ﾟ)ノ", "ヾ(◍°∇°◍)ﾉﾞ", "(つ﹏⊂)", "(╥﹏╥)",
    "( ˘ω˘ )", "(｡ŏ﹏ŏ)", "(◎_◎;)", "Σ(°△°|||)",
    "( •̀ ω •́ )✧", "(✿ヘᴥヘ)", "(ง •_•)ง", "(づ ●─● )づ",
]

# 转义列表中的特殊字符用于正则匹配
_KAOMOJI_EXACT = re.compile(
    "|".join(re.escape(k) for k in sorted(_COMMON_KAOMOJI, key=len, reverse=True)),
    re.UNICODE,
)

# 括号型颜文字通用模式：括号内只包含非文字字符（非CJK、非字母、非数字）
# 中间的字符必须是颜文字特征字符（符号、标点等），不能是正常文字
_KAOMOJI_BRACKET_CHARS = (
    "\u0020-\u002f"   # 空格和ASCII标点 !"#$%&'()*+,-./
    "\u003a-\u0040"   # :;<=>?@
    "\u005b-\u0060"   # [\]^_`
    "\u007b-\u007e"   # {|}~
    "\u00a0-\u00bf"   # 拉丁补充标点
    "\u2010-\u2bff"   # 通用标点、符号、箭头、数学运算、方框字符等
    "\u3000-\u303f"   # CJK符号和标点（不包含假名和汉字）
    "\uff00-\uffef"   # 全角ASCII、半角片假名、全角符号
    "\u0300-\u036f"   # 组合变音符号
)

_KAOMOJI_BRACKET_PATTERN = re.compile(
    r"[╮╭┌┘ヽ～〜]?"                        # 可选前缀装饰
    r"[(\[{<（「『【〔]"                      # 开括号
    rf"[{_KAOMOJI_BRACKET_CHARS}]"           # 至少一个颜文字特征字符
    rf"[{_KAOMOJI_BRACKET_CHARS}]*"          # 更多可选字符（{0,14}总计1~15）
    r"[)\]}>）」』】〕]"                      # 闭括号
    r"[/\\ノ丿)>╮╭┌┘～〜]*"                  # 可选后缀
    r"[︵︶┻━┬─ノ ]*",                       # 掀桌等动作后缀（table flip debris）
    re.UNICODE,
)

# 无括号型颜文字：OwO, QwQ, QAQ, TAT, orz, XD 等
# 使用 [a-zA-Z0-9_] 作为边界判断，因为 \w 在 UNICODE 模式下不匹配 CJK
_KAOMOJI_BARE_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9_])"
    r"(?:"
    r"[OoQqTt][wWｗ_][OoQqTt]"    # OwO, QwQ, TwT 等
    r"|[QqTt][AaBb][QqTt]"         # QAQ, TAT 等
    r"|[Oo][Rr][Zz]"               # orz
    r"|[Oo][Tt][Zz]"               # OTZ
    r"|[Xx][Dd]"                    # XD
    r"|[>＞][_\.][<＜]"              # >_< >.<
    r")"
    r"(?![a-zA-Z0-9_])",
    re.UNICODE,
)

# 装饰符号（独立出现时移除）
_DECORATION_PATTERN = re.compile(
    r"[☆★♪♫♬♩♡♥❤✿❀❁✾✽❃❋☀☁☂☃✨✧✦♠♣♦♤♧♢]+",
    re.UNICODE,
)

# ============================================================
# 2. Unicode Emoji 范围
#    精确划分子范围，避免覆盖 CJK 统一汉字 (U+4E00-U+9FFF)
# ============================================================

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"    # Emoticons (smileys)
    "\U0001F300-\U0001F5FF"    # Misc Symbols and Pictographs
    "\U0001F680-\U0001F6FF"    # Transport and Map
    "\U0001F1E0-\U0001F1FF"    # Regional Indicator Symbols
    "\U0001F900-\U0001F9FF"    # Supplemental Symbols
    "\U0001FA00-\U0001FA6F"    # Chess Symbols
    "\U0001FA70-\U0001FAFF"    # Symbols Extended-A
    "\U00002702-\U000027B0"    # Dingbats
    "\U00002600-\U000026FF"    # Misc symbols (☀☁☂ etc.)
    "\U0000FE00-\U0000FE0F"    # Variation Selectors
    "\U0000200D"               # Zero Width Joiner
    "\U00002B50"               # Star ⭐
    "\U000023F0-\U000023FA"    # Various technical symbols
    "\U0000203C"               # ‼
    "\U00002049"               # ⁉
    # Enclosed characters — 拆分为安全子范围，避开 CJK
    "\U000024C2-\U000024FF"    # Enclosed Alphanumerics (Ⓐ-ⓩ etc.)
    "\U00002500-\U00002BFF"    # Box drawing, block elements, misc symbols
    "\U0001F200-\U0001F251"    # Enclosed Ideographic Supplement
    "]+",
    re.UNICODE,
)

# ============================================================
# 3. 多余的标点/空白清理
# ============================================================

_MULTI_PUNCT = re.compile(r"([。！？!?~～…,.，、;；：:]{2,})")
_MULTI_SPACE = re.compile(r" {2,}")


def _collapse_punctuation(text: str) -> str:
    """将连续重复的标点符号缩减为一个"""
    def _pick_first(m: re.Match) -> str:
        return m.group(0)[0]
    return _MULTI_PUNCT.sub(_pick_first, text)


def clean_text_for_tts(text: str) -> str:
    """清洗文本，移除颜文字、emoji 等对 TTS 有害的内容

    处理流程:
      1. 移除 Unicode emoji
      2. 移除精确匹配的常见颜文字
      3. 移除括号型颜文字
      4. 移除无括号型颜文字 (OwO, QAQ 等)
      5. 移除装饰符号
      6. 移除不可见控制字符
      7. 合并连续标点
      8. 合并连续空格、去除首尾空白

    Args:
        text: 原始文本

    Returns:
        清洗后的纯文本，适合送入 TTS 引擎
    """
    if not text:
        return text

    # 1. 移除 Unicode emoji
    text = _EMOJI_PATTERN.sub("", text)

    # 2. 移除精确匹配的常见颜文字（优先处理，避免子串冲突）
    text = _KAOMOJI_EXACT.sub("", text)

    # 3. 移除括号型颜文字
    text = _KAOMOJI_BRACKET_PATTERN.sub("", text)

    # 4. 移除无括号型颜文字
    text = _KAOMOJI_BARE_PATTERN.sub("", text)

    # 5. 移除装饰符号
    text = _DECORATION_PATTERN.sub("", text)

    # 6. 移除不可见控制字符（保留换行和空格）
    cleaned_chars = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("C") and ch not in ("\n", "\r", "\t"):
            continue
        cleaned_chars.append(ch)
    text = "".join(cleaned_chars)

    # 7. 合并连续标点
    text = _collapse_punctuation(text)

    # 8. 合并连续空格、去除首尾空白
    text = _MULTI_SPACE.sub(" ", text)
    text = text.strip()

    return text
