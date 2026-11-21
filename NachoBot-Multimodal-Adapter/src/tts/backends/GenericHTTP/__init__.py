"""Dependency-light external HTTP text-to-speech backend.

The configured HTTP service returns a complete WAV or PCM16 payload.  This
backend buffers each response before exposing audio to the shared runtime; it
does not claim native streaming support.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from copy import deepcopy
import json
import math
import re
import socket
import time
import tomllib
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import wave

from nachobot_multimodal.tts.base import BaseTTSModel, MAX_TTS_AUDIO_BYTES, MAX_TTS_AUDIO_SECONDS, PCMChunk
from nachobot_multimodal.utils.text_cleaner import clean_text_for_tts


MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_WAV_BYTES = MAX_TTS_AUDIO_BYTES
MAX_JSON_BYTES = 44 * 1024 * 1024
_BODY_MAX_DEPTH = 32
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PLACEHOLDER = re.compile(r"\{\{(text|language|voice)\}\}")


class GenericHTTPError(ValueError):
    """A safe, user-facing error with no request URL or credential details."""


def validate_profile(profile: dict) -> dict:
    """Validate and return a normalized HTTP TTS profile.

    Supported response formats are uncompressed PCM16 WAV and raw signed
    little-endian PCM16.  Arbitrary request headers and JSON bodies are
    supported, while response size, wall-clock time, and audio duration are
    bounded.
    """

    if not isinstance(profile, dict):
        raise ValueError("GenericHTTP profile must be an object")
    allowed = {
        "name", "url", "method", "headers", "body", "response_type",
        "body_json", "response_path", "timeout_seconds", "audio_format", "sample_rate", "channels",
    }
    unknown = set(profile) - allowed
    if unknown:
        raise ValueError("GenericHTTP profile contains unsupported fields")

    name = profile.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 128 or "\x00" in name:
        raise ValueError("GenericHTTP profile name must be a non-empty string up to 128 characters")

    url = profile.get("url")
    if not isinstance(url, str) or len(url) > 8192 or any(char in url for char in "\r\n\x00"):
        raise ValueError("GenericHTTP URL must be a valid HTTP or HTTPS URL")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("GenericHTTP URL must be a valid HTTP or HTTPS URL") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("GenericHTTP URL must use HTTP or HTTPS without embedded credentials or fragments")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("GenericHTTP URL has an invalid port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("GenericHTTP URL has an invalid port")

    method = profile.get("method", "POST")
    if not isinstance(method, str) or method.upper() not in {"GET", "POST"}:
        raise ValueError("GenericHTTP method must be GET or POST")
    method = method.upper()

    headers = profile.get("headers", {})
    if not isinstance(headers, dict):
        raise ValueError("GenericHTTP headers must be a string-to-string object")
    normalized_headers: dict[str, str] = {}
    for key, value in headers.items():
        if (
            not isinstance(key, str)
            or not _HEADER_NAME.fullmatch(key)
            or key.lower() in {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
            or not isinstance(value, str)
            or any(char in value for char in "\r\n\x00")
        ):
            raise ValueError("GenericHTTP headers must contain safe string names and values")
        normalized_headers[key] = value

    body = profile.get("body", {})
    body_json = profile.get("body_json")
    if body_json is not None:
        if "body" in profile or not isinstance(body_json, str):
            raise ValueError("GenericHTTP body configuration is invalid")
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError:
            raise ValueError("GenericHTTP body_json is invalid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("GenericHTTP body must be a JSON object")
    _validate_json_value(body, depth=0)

    response_type = profile.get("response_type", "audio")
    if not isinstance(response_type, str) or response_type not in {"audio", "base64", "url"}:
        raise ValueError("GenericHTTP response_type must be audio, base64, or url")
    response_path = profile.get("response_path", "")
    if not isinstance(response_path, str) or len(response_path) > 512:
        raise ValueError("GenericHTTP response_path must be a dotted JSON field path")
    if response_type in {"base64", "url"} and (
        not response_path or any(not part for part in response_path.split("."))
    ):
        raise ValueError("GenericHTTP base64 and url responses require response_path")

    timeout = profile.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)):
        raise ValueError("GenericHTTP timeout_seconds must be a finite number from 0.1 to 120")
    timeout = float(timeout)
    if not 0.1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("GenericHTTP timeout_seconds must be from 0.1 to 120")

    audio_format = profile.get("audio_format", "wav")
    if not isinstance(audio_format, str) or audio_format not in {"wav", "pcm"}:
        raise ValueError("GenericHTTP audio_format must be wav or pcm; compressed formats are unsupported")
    sample_rate = profile.get("sample_rate")
    channels = profile.get("channels")
    if audio_format == "pcm":
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 1 <= sample_rate <= 384_000:
            raise ValueError("GenericHTTP PCM audio requires a sample_rate from 1 to 384000")
        if isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= 8:
            raise ValueError("GenericHTTP PCM audio requires channels from 1 to 8")
    else:
        sample_rate = None
        channels = None

    return {
        "name": name.strip(),
        "url": url,
        "method": method,
        "headers": dict(normalized_headers),
        "body": deepcopy(body),
        "response_type": response_type,
        "response_path": response_path,
        "timeout_seconds": timeout,
        "audio_format": audio_format,
        **({"sample_rate": sample_rate, "channels": channels} if audio_format == "pcm" else {}),
    }


def _validate_json_value(value: Any, *, depth: int) -> None:
    if depth > _BODY_MAX_DEPTH:
        raise ValueError("GenericHTTP body is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("GenericHTTP body numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("GenericHTTP body object keys must be strings")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("GenericHTTP body must contain only JSON-compatible values")


def load_active_profile(config_path: str | Path) -> dict:
    """Read and validate one complete, stable profile snapshot from TOML."""

    try:
        with Path(config_path).open("rb") as handle:
            config = tomllib.load(handle)
    except Exception:
        raise GenericHTTPError("GenericHTTP configuration could not be read") from None
    active_id = config.get("active_profile") if isinstance(config, dict) else None
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(active_id, str) or not active_id or not isinstance(profiles, dict):
        raise GenericHTTPError("GenericHTTP configuration has no active profile")
    profile = profiles.get(active_id)
    if not isinstance(profile, dict):
        raise GenericHTTPError("GenericHTTP active profile was not found")
    # TOML has no null value. WebUI persists a JSON-compatible body as
    # compact JSON text when users need JSON null values; validation decodes
    # this storage-only field back into the public body object.
    try:
        return validate_profile(profile)
    except ValueError as exc:
        # Validation messages contain field names only, never profile values.
        raise GenericHTTPError(str(exc)) from None


def _interpolate(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: variables[match.group(1)], value)
    if isinstance(value, list):
        return [_interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item, variables) for key, item in value.items()}
    return value


def _query_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _build_request(profile: dict, payload: dict, text: str, language: str, voice: str) -> Request:
    variables = {"text": text, "language": language, "voice": voice}
    mapped = _interpolate(payload, variables)
    headers = dict(profile["headers"])
    if profile["method"] == "GET":
        parsed = urlsplit(profile["url"])
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((key, _query_value(value)) for key, value in mapped.items())
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        return Request(url, headers=headers, method="GET")
    body = json.dumps(mapped, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not any(key.lower() == "content-type" for key in headers):
        headers["Content-Type"] = "application/json"
    return Request(profile["url"], data=body, headers=headers, method="POST")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so configured credentials are never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response_socket(response: Any) -> Any | None:
    current = getattr(response, "fp", None)
    raw = getattr(current, "raw", None)
    return getattr(raw, "_sock", None) or getattr(current, "_sock", None)


def _read_bounded(response: Any, *, limit: int, deadline: float) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            content_length_value = int(content_length)
        except (TypeError, ValueError):
            raise GenericHTTPError("GenericHTTP response has an invalid content length") from None
        if content_length_value > limit:
            raise GenericHTTPError("GenericHTTP response exceeded the byte limit")

    output = bytearray()
    reader = getattr(response, "read1", response.read)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GenericHTTPError("GenericHTTP request exceeded its time limit")
        sock = _response_socket(response)
        if sock is not None:
            sock.settimeout(remaining)
        try:
            chunk = reader(min(64 * 1024, limit + 1 - len(output)))
        except (TimeoutError, socket.timeout):
            raise GenericHTTPError("GenericHTTP request exceeded its time limit") from None
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > limit:
            raise GenericHTTPError("GenericHTTP response exceeded the byte limit")
    if time.monotonic() > deadline:
        raise GenericHTTPError("GenericHTTP request exceeded its time limit")
    return bytes(output)


def _open_read(request: Request, *, limit: int, deadline: float) -> tuple[bytes, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GenericHTTPError("GenericHTTP request exceeded its time limit")
    opener = build_opener(_NoRedirectHandler())
    try:
        response = opener.open(request, timeout=remaining)
    except HTTPError as exc:
        status = int(exc.code)
        exc.close()
        if 300 <= status < 400:
            raise GenericHTTPError("GenericHTTP redirects are not supported") from None
        raise GenericHTTPError(f"GenericHTTP endpoint returned HTTP status {status}") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise GenericHTTPError("GenericHTTP request failed") from None
    try:
        status = int(getattr(response, "status", 200))
        if 300 <= status < 400:
            raise GenericHTTPError("GenericHTTP redirects are not supported")
        if status < 200 or status >= 300:
            raise GenericHTTPError(f"GenericHTTP endpoint returned HTTP status {status}")
        content_type = str(response.headers.get("Content-Type", "")).lower()
        return _read_bounded(response, limit=limit, deadline=deadline), content_type
    except GenericHTTPError:
        raise
    except Exception:
        raise GenericHTTPError("GenericHTTP request failed") from None
    finally:
        response.close()


def _json_field(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise GenericHTTPError("GenericHTTP response is missing the configured JSON field")
        value = value[part]
    return value


def _json_response(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GenericHTTPError("GenericHTTP endpoint returned invalid JSON") from None


def _extract_audio(profile: dict, raw: bytes, deadline: float) -> bytes:
    response_type = profile["response_type"]
    if response_type == "base64":
        document = _json_response(raw)
        value = _json_field(document, profile["response_path"])
        if not isinstance(value, str) or len(value) > MAX_JSON_BYTES:
            raise GenericHTTPError("GenericHTTP base64 field must contain a bounded string")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise GenericHTTPError("GenericHTTP response contains invalid base64 audio") from None
        if len(raw) > MAX_WAV_BYTES:
            raise GenericHTTPError("GenericHTTP audio exceeded the byte limit")
    elif response_type == "url":
        document = _json_response(raw)
        value = _json_field(document, profile["response_path"])
        if not isinstance(value, str):
            raise GenericHTTPError("GenericHTTP URL field must contain a string")
        if len(value) > 8192 or any(char in value for char in "\r\n\x00"):
            raise GenericHTTPError("GenericHTTP audio URL is invalid")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise GenericHTTPError("GenericHTTP audio URL is invalid") from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise GenericHTTPError("GenericHTTP audio URL must use HTTP or HTTPS without embedded credentials")
        try:
            port = parsed.port
        except ValueError:
            raise GenericHTTPError("GenericHTTP audio URL has an invalid port") from None
        if port is not None and not 1 <= port <= 65535:
            raise GenericHTTPError("GenericHTTP audio URL has an invalid port")
        # Deliberately make this request without the profile's Authorization
        # or other headers. Redirects are disabled by _NoRedirectHandler.
        raw, _content_type = _open_read(Request(value, method="GET"), limit=MAX_WAV_BYTES, deadline=deadline)
    return _normalize_audio(profile, raw)


def _normalize_audio(profile: dict, raw: bytes) -> bytes:
    if not raw:
        raise GenericHTTPError("GenericHTTP endpoint returned empty audio")
    if len(raw) > MAX_WAV_BYTES:
        raise GenericHTTPError("GenericHTTP audio exceeded the byte limit")
    if profile["audio_format"] == "pcm":
        channels = profile["channels"]
        sample_rate = profile["sample_rate"]
        frame_width = channels * 2
        if len(raw) % frame_width:
            raise GenericHTTPError("GenericHTTP PCM audio ended with an incomplete frame")
        if len(raw) / frame_width / sample_rate > MAX_TTS_AUDIO_SECONDS:
            raise GenericHTTPError("GenericHTTP audio exceeded the duration limit")
        buffer = BytesIO()
        try:
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(raw)
        except (wave.Error, ValueError):
            raise GenericHTTPError("GenericHTTP PCM audio could not be converted to WAV") from None
        return buffer.getvalue()

    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise GenericHTTPError("GenericHTTP must return uncompressed PCM16 WAV; compressed audio is unsupported")
    try:
        with wave.open(BytesIO(raw), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE" or wav_file.getsampwidth() != 2:
                raise GenericHTTPError("GenericHTTP supports uncompressed PCM16 WAV only; compressed audio is unsupported")
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            frames = wav_file.getnframes()
            if not 1 <= channels <= 8 or not 1 <= sample_rate <= 384_000 or frames <= 0:
                raise GenericHTTPError("GenericHTTP WAV audio has invalid format metadata")
            if frames / sample_rate > MAX_TTS_AUDIO_SECONDS:
                raise GenericHTTPError("GenericHTTP audio exceeded the duration limit")
            pcm = wav_file.readframes(frames)
            if len(pcm) != frames * channels * 2:
                raise GenericHTTPError("GenericHTTP WAV audio is truncated")
    except GenericHTTPError:
        raise
    except (wave.Error, EOFError, OSError, ValueError):
        raise GenericHTTPError("GenericHTTP returned an invalid WAV response") from None
    return raw


def _synthesize_sync(profile: dict, text: str, language: str, voice: str, deadline: float) -> bytes:
    if time.monotonic() >= deadline:
        raise GenericHTTPError("GenericHTTP request exceeded its time limit")
    request = _build_request(profile, profile["body"], text, language, voice)
    limit = MAX_JSON_BYTES if profile["response_type"] in {"base64", "url"} else MAX_WAV_BYTES
    raw, _content_type = _open_read(request, limit=limit, deadline=deadline)
    audio = _extract_audio(profile, raw, deadline)
    if time.monotonic() > deadline:
        raise GenericHTTPError("GenericHTTP request exceeded its time limit")
    return audio


async def _drain_cancelled_worker(worker: asyncio.Task[bytes]) -> None:
    """Keep the caller's runtime lock until its blocking HTTP worker exits."""

    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
            # Repeated cancellation must not detach the synchronous socket
            # operation from the shared TTS serialization boundary.
            continue
        except Exception:
            # The worker is complete; its exception is retrieved below and
            # cancellation remains the caller-visible result.
            break
    if worker.cancelled():
        raise asyncio.CancelledError
    try:
        worker.result()
    except Exception:
        # The waiting request was cancelled; only drain it, do not replace the
        # cancellation with an error that completed afterward in the worker.
        pass
    raise asyncio.CancelledError


async def synthesize_profile(
    profile: dict,
    text: str,
    language: str = "",
    voice: str = "",
) -> bytes:
    """Synthesize one bounded HTTP profile and return a complete WAV."""

    if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
        raise ValueError("GenericHTTP text must contain 1 to 10000 characters")
    text = clean_text_for_tts(text)
    if not text:
        raise ValueError("清洗后的 TTS 文本为空")

    normalized = validate_profile(profile)
    if not text.strip() or len(text) > 10_000:
        raise ValueError("GenericHTTP text must contain 1 to 10000 characters")
    if not isinstance(language, str) or len(language) > 128 or not isinstance(voice, str) or len(voice) > 256:
        raise ValueError("GenericHTTP language or voice value is too long")
    deadline = time.monotonic() + normalized["timeout_seconds"]
    worker = asyncio.create_task(
        asyncio.to_thread(_synthesize_sync, normalized, text, language, voice, deadline)
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=normalized["timeout_seconds"],
        )
    except asyncio.CancelledError:
        await _drain_cancelled_worker(worker)
        raise
    except asyncio.TimeoutError:
        def consume_late_result(future: asyncio.Future[bytes]) -> None:
            if not future.cancelled():
                future.exception()

        worker.add_done_callback(consume_late_result)
        raise GenericHTTPError("GenericHTTP request exceeded its time limit") from None
    except GenericHTTPError:
        raise
    except Exception:
        # Keep URL query tokens, Authorization values, and transport details
        # out of callers, logs, and public HTTP error responses.
        raise GenericHTTPError("GenericHTTP request failed") from None


class TTSModel(BaseTTSModel):
    """Shared runtime client with fresh profile selection on each request."""

    native_streaming = False
    stream_capability = "buffered_pcm"

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else (
            Path(__file__).resolve().parents[4] / "configs" / "generic-http.toml"
        )
        self.load_config()

    def load_config(self) -> dict:
        """Validate startup selection without sending a network request."""

        self._load_profile()
        return {"config_path": str(self.config_path)}

    def _load_profile(self) -> dict:
        return load_active_profile(self.config_path)

    @property
    def configuration_ready(self) -> bool:
        try:
            self._load_profile()
            return True
        except Exception:
            return False

    async def tts(self, text: str, **kwargs: Any) -> bytes:
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            raise ValueError("GenericHTTP text must contain 1 to 10000 characters")
        text = clean_text_for_tts(text)
        if not text:
            raise ValueError("清洗后的 TTS 文本为空")
        profile = self._load_profile()
        language = kwargs.get("text_lang") or kwargs.get("language") or ""
        voice = kwargs.get("voice") or kwargs.get("voice_id") or ""
        return await synthesize_profile(profile, text, str(language), str(voice))

    async def tts_stream(self, text: str, **kwargs: Any):
        """Compatibility byte iterator; synthesis still completes before yielding."""

        yield await self.tts(text, **kwargs)

    async def tts_pcm_stream(self, text: str, **kwargs: Any):
        """Yield buffered WAV samples in PCM chunks for legacy stream callers."""

        wav_data = await self.tts(text, **kwargs)
        try:
            with wave.open(BytesIO(wav_data), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                frames = wav_file.getnframes()
                pcm = wav_file.readframes(frames)
        except (wave.Error, EOFError, OSError):
            raise GenericHTTPError("GenericHTTP returned an invalid WAV response") from None
        frame_width = channels * 2
        chunk_size = max(frame_width, 64 * 1024 // frame_width * frame_width)
        for offset in range(0, len(pcm), chunk_size):
            yield PCMChunk(
                data=pcm[offset:offset + chunk_size],
                sample_rate=sample_rate,
                channels=channels,
                sample_width=2,
                codec="pcm_s16le",
            )


__all__ = ["GenericHTTPError", "TTSModel", "load_active_profile", "synthesize_profile", "validate_profile"]
