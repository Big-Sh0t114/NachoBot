"""Conservative, shared cleanup for text sent to speech synthesis."""

from __future__ import annotations

import re
import unicodedata


_CONTROL_BLOCK = re.compile(
    r"<(think|analysis|reasoning|scratchpad|internal|control)\b[^>]*>.*?(?:</\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_CONTROL_TOKEN_BLOCK = re.compile(
    r"<\|\s*(?P<tag>think|analysis|reasoning|scratchpad|internal|control)\s*\|>"
    r".*?(?:<\|\s*/(?P=tag)\s*\|>|(?=<\|\s*final\s*\|>)|$)",
    re.IGNORECASE | re.DOTALL,
)
_CONTROL_TAG = re.compile(
    r"</?(?:think|analysis|reasoning|scratchpad|internal|control|final)\b[^>]*>",
    re.IGNORECASE,
)
_CONTROL_TOKEN = re.compile(
    r"<\|\s*/?\s*(?:think|analysis|reasoning|scratchpad|internal|control|final)\s*\|>",
    re.IGNORECASE,
)

# Exact forms are checked before emoji codepoints so emoji inside a face cannot
# leave spoken punctuation or fragments behind.
_COMMON_KAOMOJI = (
    # Keep the original exact forms supported by this public utility.
    "(╥_╥)", "(T_T)", "(;_;)", "(ﾉД`)", "(´;ω;`)", "(ノ_<。)",
    "(T▽T)", "(ಥ_ಥ)", "(TдT)", "(T⌓T)",
    "(≧∇≦)", "(＾▽＾)", "(^_^)", "(＾o＾)", "(✿◡‿◡)", "(◕ᴗ◕✿)",
    "(✧ω✧)", "(◕‿◕)", "(｡◕‿◕｡)", "(★ω★)", "(≧◡≦)", "(＾∀＾)",
    "(*≧ω≦)", "(*^▽^*)", "(●'◡'●)",
    "(╯°□°)╯︵ ┻━┻", "(ノಠ益ಠ)ノ", "(╬ Ò﹏Ó)", "(╬▔皿▔)╯", "┬─┬ノ( º _ ºノ)",
    "(・ω・)", "(・∀・)", "(=^・ω・^=)", "(●ˇ∀ˇ●)", "(｡♥‿♥｡)",
    "(灬ºωº灬)", "(*´▽`*)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)", "(✪ω✪)", "(*≧∀≦*)",
    "ヾ(≧▽≦*)o", "o(*≧▽≦)ツ",
    "(⊙_⊙)", "(°ー°〃)", "( ̄▽ ̄)", "(￣▽￣)", "┌(・。・)┘",
    "(ー_ー)!!", "(¬_¬)", "(-_-;)", "(；一_一)",
    "╮(╯▽╰)╭", "╰(*°▽°*)╯", "～(￣▽￣～)", "(～￣▽￣)～",
    "ヽ(✿ﾟ▽ﾟ)ノ", "ヾ(◍°∇°◍)ﾉﾞ", "(つ﹏⊂)", "(╥﹏╥)", "( ˘ω˘ )",
    "(｡ŏ﹏ŏ)", "(◎_◎;)", "Σ(°△°|||)", "( •̀ ω •́ )✧", "(✿ヘᴥヘ)",
    "(ง •_•)ง", "(づ ●─● )づ",
    # Common additions for spaced, waving-arm, and ASCII-style faces.
    "(≧▽≦)", "(^.^)", "(^o^)", "(^-^)", "(T.T)", "(Q_Q)", "(TдT)",
    "*^▽^*",
    "＼(^o^)／", "ヾ(≧▽≦)ノ", "ヽ(≧▽≦)ノ", "٩(≧▽≦)۶",
    "( ͡° ͜ʖ ͡°)", "(づ￣ ³￣)づ", "(づ｡◕‿‿◕｡)づ",
)
_KAOMOJI_EXACT = re.compile(
    "|".join(re.escape(face) for face in sorted(_COMMON_KAOMOJI, key=len, reverse=True)),
    re.UNICODE,
)

_ASCII_FACE_BODY = (
    r"(?:(?:[:;=][-^']?[)(DPp/\\|]|[)(][-^']?[:;=])|"
    r"T_T|T\.T|Q_Q|Q\.Q|\^[_oO-]\^|>_<|;_;|0_0|o_O|O_o|o_o|O_O|"
    r"u_u|v_v|-_-|\._\.|=_=|\*_\*|xD|XD|:3)"
)
_ASCII_EMOTICON = re.compile(rf"(?<![A-Za-z0-9_]){_ASCII_FACE_BODY}(?![A-Za-z0-9_])")
_KAOMOJI_BARE_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9_])"
    r"(?:[OoQqTt][wWｗ_][OoQqTt]|[QqTt][AaBb][QqTt]|[Oo][Rr][Zz]|"
    r"[Oo][Tt][Zz]|[Xx][Dd]|≡ω≡|[>＞][_\.][<＜])"
    r"(?![a-zA-Z0-9_])",
    re.UNICODE,
)
_SHRUG_EMOTICON = re.compile(r"¯\s*\\?_?\(\s*ツ\s*\)_?/\s*¯")
_BARE_CAT_FACE = re.compile(r"(?<![A-Za-z0-9_])≡ω≡(?![A-Za-z0-9_])")
_EMPTY_PARENS = re.compile(r"\(\s*\)|（\s*）")
_KEYCAP_SEQUENCE = re.compile(r"[#*0-9]\ufe0f?\u20e3")

_ARM_GLYPHS = "٩۶งლوﾉノ╰╯╮╭づヽヾ"
_ARMS_FACE_GROUP = re.compile(
    rf"(?P<left>[{_ARM_GLYPHS}]*)"
    r"(?P<group>\([^()\n]{1,48}\)|（[^（）\n]{1,48}）)"
    rf"(?P<right>[{_ARM_GLYPHS}]*)"
)
_STRONG_FACE_GLYPHS = set("ωΩдДツᴗᵕ益皿∀ಥ╥◕◡⊙ʖ‿︿▽△≧≦⌒ㅅㅜㅠ╹◔◉ಠづ")
_STANDALONE_FACE_RUN = re.compile(r"[ωΩдДツᴗᵕ益皿∀ಥ╥◕◡⊙ʖ‿︿▽△≧≦⌒ㅅㅜㅠ・･＾^°º╹◔◉ಠづ｡´｀`*]{2,}")
_FACE_HAN_GLYPHS = {"益", "皿"}

_MARKDOWN_LINK = re.compile(r"!?\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)")
_MARKDOWN_PREFIX = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-+*][ \t]+|\d+[.)][ \t]+)")
_MARKDOWN_FENCE = re.compile(r"```[\w+-]*[ \t]*")
_MARKDOWN_WRAPPERS = (
    (re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.DOTALL), r"\1"),
    (re.compile(r"__(\S(?:.*?\S)?)__", re.DOTALL), r"\1"),
    (re.compile(r"~~(\S(?:.*?\S)?)~~", re.DOTALL), r"\1"),
    (re.compile(r"(?<!\w)\*(\S(?:.*?\S)?)\*(?!\w)", re.DOTALL), r"\1"),
    (re.compile(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)", re.DOTALL), r"\1"),
)
_EMPTY_MARKDOWN_WRAPPER = re.compile(
    r"\*\*\s*\*\*|__\s*__|~~\s*~~|(?<!\w)\*\s*\*(?!\w)|(?<!\w)_\s*_(?!\w)"
)

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, flags, modifiers, and emoji sequences
    (0x1FC00, 0x1FFFD),
    (0x2600, 0x27BF),    # emoji and dingbats; deliberately excludes math/box blocks
    (0x1F200, 0x1F251),
)
_EMOJI_SINGLE_CODEPOINTS = {0x203C, 0x2049, 0x2122, 0x2139, 0x2B50, 0x2B55}
_DECORATIVE_GLYPHS = set("©®™℠‼⁉•※◕◡◔◉♡♥")
_REPEATED_PUNCT = re.compile(r"([。！？!?~～,.，、;；：:])\1+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。！？、；：,.!?;:])")


def _is_emoji(char: str) -> bool:
    value = ord(char)
    return value in _EMOJI_SINGLE_CODEPOINTS or any(
        start <= value <= end for start, end in _EMOJI_RANGES
    )


def _is_han(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U0003134f"


def _is_face_body(group: str) -> bool:
    body = group[1:-1].strip()
    if re.fullmatch(_ASCII_FACE_BODY, body):
        return True
    if any(char.isascii() and char.isalnum() for char in body):
        return False
    han_chars = [char for char in body if _is_han(char)]
    if any(char not in _FACE_HAN_GLYPHS for char in han_chars):
        return False
    return sum(char in _STRONG_FACE_GLYPHS for char in body) >= 2


def _remove_faces(text: str) -> str:
    """Remove supported faces in one bounded pass."""
    text = _KAOMOJI_EXACT.sub(" ", text)
    text = _SHRUG_EMOTICON.sub(" ", text)
    text = _BARE_CAT_FACE.sub(" ", text)

    def remove_parenthetical_face(match: re.Match[str]) -> str:
        return " " if _is_face_body(match.group("group")) else match.group(0)

    def remove_standalone_face(match: re.Match[str]) -> str:
        return " " if sum(char in _STRONG_FACE_GLYPHS for char in match.group(0)) >= 2 else match.group(0)

    text = _ARMS_FACE_GROUP.sub(remove_parenthetical_face, text)
    text = _STANDALONE_FACE_RUN.sub(remove_standalone_face, text)
    text = _KAOMOJI_BARE_PATTERN.sub(" ", text)
    return _ASCII_EMOTICON.sub(" ", text)


def clean_text_for_tts(text: str) -> str:
    """Remove speech-irrelevant faces, emoji, and common display markup.

    Ordinary parentheticals, numeric notation, mathematical operators, paths,
    and visible Markdown link labels are retained. The returned text is only
    for synthesis; callers should keep the original reply for display/storage.
    """
    if not isinstance(text, str) or not text:
        return ""

    cleaned = _CONTROL_BLOCK.sub(" ", text)
    cleaned = _CONTROL_TOKEN_BLOCK.sub(" ", cleaned)
    cleaned = _CONTROL_TAG.sub(" ", cleaned)
    cleaned = _CONTROL_TOKEN.sub(" ", cleaned)
    cleaned = _remove_faces(cleaned)

    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_PREFIX.sub("", cleaned)
    cleaned = _EMPTY_PARENS.sub(" ", cleaned)
    cleaned = _MARKDOWN_FENCE.sub("", cleaned).replace("`", "")
    for wrapper, replacement in _MARKDOWN_WRAPPERS:
        cleaned = wrapper.sub(replacement, cleaned)
    cleaned = _EMPTY_MARKDOWN_WRAPPER.sub(" ", cleaned)
    # Markdown unwrapping can expose an identifier-bounded face (for example
    # `_QwQ_`). A second bounded pass keeps every backend's first-pass result
    # identical without recursively cleaning arbitrary transformations.
    cleaned = _remove_faces(cleaned)

    cleaned = _KEYCAP_SEQUENCE.sub("", cleaned)
    visible: list[str] = []
    for char in cleaned:
        category = unicodedata.category(char)
        value = ord(char)
        if char in "\n\r\t":
            visible.append(" ")
        elif (
            category.startswith("C")
            or _is_emoji(char)
            or char in _DECORATIVE_GLYPHS
            or 0xFE00 <= value <= 0xFE0F
            or 0xE0100 <= value <= 0xE01EF
            or 0x1F3FB <= value <= 0x1F3FF
            or value == 0x20E3
        ):
            continue
        else:
            visible.append(char)

    cleaned = _EMPTY_PARENS.sub(" ", "".join(visible))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _REPEATED_PUNCT.sub(r"\1", cleaned)
    cleaned = cleaned.strip()
    return cleaned if any(char.isalnum() for char in cleaned) else ""
