"""Text processing utilities for Bilibili Adapter."""

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

# Lazy import Seg to avoid circular dependency
Seg = None


def _get_seg_class():
    """Lazy import Seg class."""
    global Seg
    if Seg is None:
        from ncnk_message import Seg as _Seg

        Seg = _Seg
    return Seg


_EMOJI_CODEPOINT_RANGES = (
    (0x1F100, 0x1F1FF),
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x2702, 0x27B0),
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_IMAGE_PREFIX_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COMMAND_GUARD_PREFIX = "\u200b"
_KAOMOJI_PLACEHOLDER_RE = re.compile(r"__KAOMOJI_\d+__")
BILIBILI_DANMU_MAX_LENGTH = 40
BILIBILI_DANMU_SEND_DELAY_SECONDS = 0.8


def _strip_emoji(text: str) -> str:
    if not text:
        return ""
    return "".join(
        ch
        for ch in text
        if not any(start <= ord(ch) <= end for start, end in _EMOJI_CODEPOINT_RANGES)
    )


def _mask_urls(text: str) -> str:
    if not text:
        return ""
    return _URL_RE.sub("[link]", text)


# Regex to match kaomoji and special emoticons
_KAOMOJI_RE = re.compile(
    r"[\(\（\[\]\{\}\u208d\u208e]"  # Opening bracket: ( （ [ ] { } ₍ ₎
    r"[^\(\)\（\）\[\]\{\}\u208d\u208e]{1,20}"  # Content (1-20 chars)
    r"[\)\）\]\]\}\}\u208e]"  # Closing bracket
    r"|"
    r"[｡ﾟ✧♪♡☆★●○◎◇◆□■△▲▽▼※→←↑↓\u25de\u0311]+"  # Special symbols (including ◞ ̑)
)


def _clean_text_for_tts(text: str) -> str:
    """Clean text for TTS: remove kaomoji, emoticons, and special characters."""
    if not text:
        return ""
    # Remove kaomoji like (๑•́ ₃ •̀๑), (=^･ω･^=), etc.
    cleaned = _KAOMOJI_RE.sub("", text)
    # Remove standalone special chars that might cause issues
    cleaned = re.sub(r"[～〜♪♡☆★]", "", cleaned)
    # Normalize multiple spaces/punctuation
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。、！？]{2,}", "。", cleaned)
    return cleaned.strip()


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    if "\\u" in cleaned:
        try:
            cleaned = cleaned.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned


def _guess_image_format(image_bytes: bytes) -> str:
    if not image_bytes:
        return ""
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "gif"
    return ""


def _decode_image_base64(data: Any) -> Tuple[Optional[bytes], str]:
    if not data or not isinstance(data, str):
        return None, ""
    raw = data.strip()
    if raw.startswith("base64://"):
        raw = raw[len("base64://") :]
    fmt = ""
    match = _IMAGE_PREFIX_RE.match(raw)
    if match:
        fmt = match.group(1).lower()
        raw = raw[match.end() :]
    try:
        image_bytes = base64.b64decode(raw)
    except Exception:
        return None, ""
    if not fmt:
        fmt = _guess_image_format(image_bytes)
    if fmt == "jpg":
        fmt = "jpeg"
    return image_bytes, fmt


def _extract_plain_text(seg) -> str:
    if seg.type == "seglist" and isinstance(seg.data, list):
        parts = [_extract_plain_text(child) for child in seg.data]
        return "".join(parts)
    if seg.type == "text" or seg.type == "tts_text":
        return _strip_emoji(str(seg.data or ""))
    return ""


def _extract_image_base64(seg) -> str:
    if seg.type in ("image", "emoji"):
        return str(seg.data or "")
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            image_data = _extract_image_base64(child)
            if image_data:
                return image_data
    return ""


def _guard_command_segment(seg) -> None:
    SegClass = _get_seg_class()
    if seg.type == "text":
        text = str(seg.data or "")
        if text and not text.startswith(_COMMAND_GUARD_PREFIX):
            seg.data = f"{_COMMAND_GUARD_PREFIX}{text}"
        return
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            if child.type == "text":
                child_text = str(child.data or "")
                if child_text and not child_text.startswith(_COMMAND_GUARD_PREFIX):
                    child.data = f"{_COMMAND_GUARD_PREFIX}{child_text}"
                elif not child_text:
                    child.data = _COMMAND_GUARD_PREFIX
                return
        seg.data.insert(0, SegClass(type="text", data=_COMMAND_GUARD_PREFIX))


def _find_reply_id(seg) -> Optional[str]:
    if seg.type == "reply":
        return str(seg.data)
    if seg.type == "seglist" and isinstance(seg.data, list):
        for child in seg.data:
            reply_id = _find_reply_id(child)
            if reply_id:
                return reply_id
    return None


def _protect_kaomoji(sentence: str) -> Tuple[str, Dict[str, str]]:
    kaomoji_pattern = re.compile(
        r"("
        r"[(\[（【]"
        r"[^()\[\]（）【】]*?"
        r"[^一-龥a-zA-Z0-9\s]"
        r"[^()\[\]（）【】]*?"
        r"[)\]）】]"
        r")"
        r"|"
        r"([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
    )
    kaomoji_matches = kaomoji_pattern.findall(sentence)
    placeholder_to_kaomoji: Dict[str, str] = {}
    for idx, match in enumerate(kaomoji_matches):
        kaomoji = match[0] or match[1]
        placeholder = f"__KAOMOJI_{idx}__"
        sentence = sentence.replace(kaomoji, placeholder, 1)
        placeholder_to_kaomoji[placeholder] = kaomoji
    return sentence, placeholder_to_kaomoji


def _recover_kaomoji(
    sentences: List[str], placeholder_to_kaomoji: Dict[str, str]
) -> List[str]:
    recovered: List[str] = []
    for sentence in sentences:
        for placeholder, kaomoji in placeholder_to_kaomoji.items():
            sentence = sentence.replace(placeholder, kaomoji)
        recovered.append(sentence)
    return recovered


def _is_split_break_token(token: str) -> bool:
    if not token or _KAOMOJI_PLACEHOLDER_RE.fullmatch(token):
        return False
    return token.isspace() or token in "，。！？；：、,.!?;:"


def _tokenize_with_kaomoji(text: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    while i < len(text):
        match = _KAOMOJI_PLACEHOLDER_RE.match(text, i)
        if match:
            tokens.append(match.group(0))
            i = match.end()
            continue
        tokens.append(text[i])
        i += 1
    return tokens


def _split_bilibili_text(
    text: str, max_length: int = BILIBILI_DANMU_MAX_LENGTH
) -> List[str]:
    if not text:
        return []
    protected_text, kaomoji_mapping = _protect_kaomoji(text)
    tokens = _tokenize_with_kaomoji(protected_text)
    segments: List[str] = []
    current_tokens: List[str] = []
    current_len = 0
    last_break_index: Optional[int] = None

    def token_length(token: str) -> int:
        if token in kaomoji_mapping:
            return len(kaomoji_mapping[token])
        return len(token)

    def recompute_state() -> None:
        nonlocal current_len, last_break_index
        current_len = 0
        last_break_index = None
        for idx, tok in enumerate(current_tokens):
            current_len += token_length(tok)
            if _is_split_break_token(tok):
                last_break_index = idx

    def flush(count: int) -> None:
        nonlocal current_tokens
        if count <= 0:
            return
        segment = "".join(current_tokens[:count]).strip()
        if segment:
            segments.append(segment)
        current_tokens = current_tokens[count:]
        recompute_state()

    for token in tokens:
        if token == "\n":
            flush(len(current_tokens))
            continue
        tok_len = token_length(token)
        if current_len + tok_len > max_length and current_tokens:
            if last_break_index is not None:
                flush(last_break_index + 1)
            else:
                flush(len(current_tokens))
        if not current_tokens and tok_len > max_length:
            current_tokens.append(token)
            flush(len(current_tokens))
            continue
        current_tokens.append(token)
        current_len += tok_len
        if _is_split_break_token(token):
            last_break_index = len(current_tokens) - 1

    if current_tokens:
        flush(len(current_tokens))

    return _recover_kaomoji(segments, kaomoji_mapping)
