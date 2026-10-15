"""Small, platform-neutral cleanup for replies explicitly routed to TTS."""

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
_ASCII_FACE_BODY = (
    r"(?:(?:[:;=][-^']?[)(DPp/\\|]|[)(][-^']?[:;=])|"
    r"T_T|T\.T|Q_Q|Q\.Q|\^[_oO-]\^|>_<|;_;|0_0|o_O|O_o|o_o|O_O|"
    r"u_u|v_v|-_-|\._\.|=_=|\*_\*|xD|XD|:3)"
)
_ASCII_EMOTICON = re.compile(
    rf"(?<![A-Za-z0-9]){_ASCII_FACE_BODY}(?![A-Za-z0-9])"
)
_SHRUG_EMOTICON = re.compile(r"¯\s*\\?_?\(\s*ツ\s*\)_?/\s*¯")
_EMPTY_PARENS = re.compile(r"\(\s*\)|（\s*）")
_ARM_GLYPHS = "٩۶งლوﾉノ╰╯╮╭づ"
_ARMS_FACE_GROUP = re.compile(
    rf"(?P<left>[{_ARM_GLYPHS}]*)"
    r"(?P<group>\([^()\n]{1,48}\)|（[^（）\n]{1,48}）)"
    rf"(?P<right>[{_ARM_GLYPHS}]*)"
)
_FACE_GLYPHS = set("ωΩдДツᴗᵕ益皿∀ಥ╥◕◡⊙ʖ‿︿▽△⌒ㅅㅜㅠ・･＾^°º╹◕◔◉ಠ•;；´｀`*｡づ")
_STRONG_FACE_GLYPHS = set("ωΩдДツᴗᵕ益皿∀ಥ╥◕◡⊙ʖ‿︿▽△⌒ㅅㅜㅠ╹◔◉ಠづ")
_STANDALONE_FACE_RUN = re.compile(r"[ωΩдДツᴗᵕ益皿∀ಥ╥◕◡⊙ʖ‿︿▽△⌒ㅅㅜㅠ・･＾^°º╹◔◉ಠづ｡´｀`*]{2,}")
_FACE_HAN_GLYPHS = {"益", "皿"}
_DECORATIVE_GLYPHS = set("©®™℠‼⁉•※◕◡◔◉♡♥")


def clean_tts_reply_text(text: str) -> str:
    """Remove non-spoken markup and common faces while preserving the reply."""
    if not isinstance(text, str):
        return ""

    cleaned = _CONTROL_BLOCK.sub(" ", text)
    cleaned = _CONTROL_TOKEN_BLOCK.sub(" ", cleaned)
    cleaned = _CONTROL_TAG.sub(" ", cleaned)
    cleaned = _CONTROL_TOKEN.sub(" ", cleaned)
    cleaned = _SHRUG_EMOTICON.sub(" ", cleaned)

    def remove_parenthetical_face(match: re.Match[str]) -> str:
        if _is_face_body(match.group("group")):
            return " "
        return match.group(0)

    def remove_standalone_face(match: re.Match[str]) -> str:
        if sum(char in _STRONG_FACE_GLYPHS for char in match.group(0)) >= 2:
            return " "
        return match.group(0)

    cleaned = _ARMS_FACE_GROUP.sub(remove_parenthetical_face, cleaned)
    cleaned = _STANDALONE_FACE_RUN.sub(remove_standalone_face, cleaned)
    cleaned = _ASCII_EMOTICON.sub(" ", cleaned)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_PREFIX.sub("", cleaned)
    cleaned = _EMPTY_PARENS.sub(" ", cleaned)
    cleaned = _MARKDOWN_FENCE.sub("", cleaned).replace("`", "")
    for wrapper, replacement in _MARKDOWN_WRAPPERS:
        cleaned = wrapper.sub(replacement, cleaned)

    visible: list[str] = []
    for char in cleaned:
        category = unicodedata.category(char)
        if char in "\n\r\t":
            visible.append(" ")
        elif category.startswith("C") or _is_emoji(char) or char in _DECORATIVE_GLYPHS:
            continue
        elif char == "•" or "\u2500" <= char <= "\u259f":
            continue
        elif (
            "\ufe00" <= char <= "\ufe0f"
            or "\U0001f3fb" <= char <= "\U0001f3ff"
            or char == "\u20e3"
        ):
            continue
        else:
            visible.append(char)

    cleaned = re.sub(r"\s+", " ", "".join(visible)).strip()
    cleaned = _EMPTY_PARENS.sub(" ", cleaned)
    cleaned = re.sub(r"\s+([，。！？、；：,.!?;:])", r"\1", cleaned)
    if not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


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
    signals = sum(char in _FACE_GLYPHS for char in body)
    return signals >= 2 and (not han_chars or signals >= 2)


def _is_emoji(char: str) -> bool:
    value = ord(char)
    return (
        0x1F000 <= value <= 0x1FAFF
        or 0x1FC00 <= value <= 0x1FFFD
        or 0x2600 <= value <= 0x27BF
    )
