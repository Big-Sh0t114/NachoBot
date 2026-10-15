"""Shared projection for adapter-declared JSON reply envelopes."""

from __future__ import annotations

import json
import re


def prepare_json_envelope_delivery(
    full_text: str,
    *,
    tts_language: str = "",
) -> tuple[str, dict[str, str] | None]:
    """Return human-facing reply text and an optional explicit TTS sidecar.

    The original text remains available to the caller for adapter transport.
    Only an explicit JSON object with a string ``reply`` is projected. Plain
    text remains unchanged; malformed JSON-shaped output fails closed so it
    cannot enter assistant history as a control envelope.
    """

    original_text = str(full_text or "")
    candidate = original_text.strip()
    start = candidate.find("{")
    if start < 0:
        return original_text, None

    prefix = candidate[:start].strip()
    explicit_json = not prefix or re.fullmatch(r"```(?:json)?\s*", prefix, flags=re.IGNORECASE) is not None
    end = candidate.rfind("}")
    if end <= start:
        return ("", None) if explicit_json else (original_text, None)

    try:
        envelope = json.loads(candidate[start : end + 1], strict=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ("", None) if explicit_json else (original_text, None)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("reply"), str):
        return ("", None) if explicit_json else (original_text, None)

    reply_text = envelope["reply"].strip()
    display_text = reply_text
    language = str(tts_language or "").strip().lower()
    if language not in {"ja", "zh"}:
        language = ""

    raw_tts = envelope.get("tts_text")
    segment_language = language
    if isinstance(raw_tts, dict):
        tts_text = str(raw_tts.get("text") or "").strip()
        requested_language = str(raw_tts.get("lang") or "").strip().lower()
        if requested_language in {"ja", "zh"}:
            segment_language = requested_language
    elif isinstance(raw_tts, str):
        tts_text = raw_tts.strip()
    else:
        tts_text = ""

    transport_text = original_text
    if not tts_text and language:
        normalized = reply_text.replace("＜", "<").replace("＞", ">").replace("／", "/")
        jp_parts = re.findall(r"<JP>(.*?)</JP>", normalized, flags=re.IGNORECASE | re.DOTALL)
        zh_parts = re.findall(r"<ZH>(.*?)</ZH>", normalized, flags=re.IGNORECASE | re.DOTALL)
        japanese = "".join(part.strip() for part in jp_parts if part.strip())
        chinese = "".join(part.strip() for part in zh_parts if part.strip())
        if japanese or chinese:
            display_text = chinese or japanese
            tts_text = (japanese or chinese) if language == "ja" else (chinese or japanese)
            normalized_envelope = dict(envelope)
            normalized_envelope["reply"] = display_text
            transport_text = json.dumps(normalized_envelope, ensure_ascii=False)

    if not tts_text:
        return display_text, None

    payload = {
        "text": tts_text,
        "display_text": transport_text,
    }
    if segment_language:
        payload["lang"] = segment_language
    return display_text, payload
