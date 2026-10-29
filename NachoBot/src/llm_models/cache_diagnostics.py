"""Bounded, opt-in prompt-prefix diagnostics.

The diagnostic records describe the ``Message`` objects immediately before the
provider SDK transforms them. They never persist prompt text, binary payloads,
API keys, endpoints, or provider configuration values.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_PROMPT_TEXT_CHARS = 16_384
MAX_PROMPT_FIELDS = 256
MAX_BINARY_HASH_BYTES = 2 * 1024 * 1024
MAX_ENTRIES_PER_KEY = 128
MAX_KEYS = 64
MAX_GLOBAL_BYTES = 8 * 1024 * 1024
MAX_QUEUE_ENTRIES = 128
MAX_OUTPUT_FILE_BYTES = 2 * 1024 * 1024
MAX_HTML_ROWS = 2048
_WRITER_POLL_SECONDS = 0.2
_TRUNCATION_MARKER = "<truncated>"


@dataclass(frozen=True)
class _PromptField:
    path: str
    kind: str
    value: str
    digest: str
    length: int
    sampled_chars: int = 0
    truncated: bool = False

    @property
    def memory_bytes(self) -> int:
        return (
            sys.getsizeof(self)
            + sys.getsizeof(self.__dict__)
            + sys.getsizeof(self.path)
            + sys.getsizeof(self.kind)
            + sys.getsizeof(self.value)
            + sys.getsizeof(self.digest)
            + 96
        )


@dataclass(frozen=True)
class _PromptSnapshot:
    fields: tuple[_PromptField, ...]
    fingerprint: str
    sampled_chars: int
    truncated: bool
    memory_bytes: int


_history: OrderedDict[tuple[str, str, str, str, str], deque[_PromptSnapshot]] = OrderedDict()
_history_bytes = 0
_dropped_records = 0
_state_lock = threading.RLock()

_write_queue: queue.Queue[tuple[Path, dict[str, Any]] | None] = queue.Queue(maxsize=MAX_QUEUE_ENTRIES)
_writer_guard = threading.Lock()
_writer_thread: threading.Thread | None = None


def _safe_name(value: Any, limit: int = 120) -> str:
    text = str(value or "unknown")
    cleaned = "".join(ch if ch.isalnum() or ch in "._-/" else "_" for ch in text)
    return cleaned[:limit] or "unknown"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _binary_digest(value: Any, max_bytes: int) -> tuple[str, int, bool, int]:
    """Hash a bounded prefix of a binary field without retaining its payload."""
    if isinstance(value, memoryview):
        prefix = value[:max_bytes]
        return hashlib.sha256(prefix).hexdigest(), len(value), len(value) > len(prefix), len(prefix)
    if isinstance(value, (bytes, bytearray)):
        prefix = memoryview(value)[:max_bytes]
        return hashlib.sha256(prefix).hexdigest(), len(value), len(value) > len(prefix), len(prefix)

    if isinstance(value, str):
        # Image/video message parts are base64 strings. Decode only a bounded
        # prefix; if malformed, hash a bounded encoded prefix and mark it.
        encoded_limit = ((max_bytes + 2) // 3) * 4
        encoded = value[:encoded_limit].encode("ascii", errors="ignore")
        padded = encoded + b"=" * ((-len(encoded)) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False)[:max_bytes]
            length = max(0, (len(value) * 3) // 4 - (2 if value.endswith("==") else 1 if value.endswith("=") else 0))
            return hashlib.sha256(decoded).hexdigest(), length, len(value) > encoded_limit, len(decoded)
        except (ValueError, TypeError):
            return hashlib.sha256(encoded).hexdigest(), len(value), True, min(len(encoded), max_bytes)

    fallback = repr(type(value).__name__).encode("utf-8")
    return hashlib.sha256(fallback).hexdigest(), 0, True, 0


def _fit_text(value: str, budget: int) -> tuple[str, bool]:
    if len(value) <= budget:
        return value, False
    if budget <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:budget], True
    return value[: budget - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER, True


def _bounded_value(value: Any, budget: int, depth: int = 0) -> tuple[str, bool]:
    """Make a character- and depth-bounded signature and flag every omission."""
    if budget <= 0:
        return "", True
    if depth >= 8:
        return _TRUNCATION_MARKER[:budget], True
    if isinstance(value, str):
        return _fit_text(value, budget)
    if value is None or isinstance(value, (bool, int, float)):
        return _fit_text(str(value), budget)
    if isinstance(value, dict):
        parts: list[str] = []
        used = 0
        truncated = False
        for index, (key, child) in enumerate(value.items()):
            if index >= 24:
                truncated = True
                break
            remaining = budget - 2 - used - (1 if parts else 0)
            if remaining <= 0:
                truncated = True
                break
            key_text, key_truncated = _bounded_value(key, min(128, remaining), depth + 1)
            child_budget = max(0, min(512, remaining - len(key_text) - 1))
            child_text, child_truncated = _bounded_value(child, child_budget, depth + 1)
            piece, piece_truncated = _fit_text(f"{key_text}:{child_text}", remaining)
            parts.append(piece)
            used += len(piece) + (1 if len(parts) > 1 else 0)
            truncated = truncated or key_truncated or child_truncated or piece_truncated
            if piece_truncated:
                break
        return "{" + ",".join(parts) + "}", truncated
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        used = 0
        truncated = False
        for index, child in enumerate(value):
            if index >= 24:
                truncated = True
                break
            remaining = budget - 2 - used - (1 if parts else 0)
            if remaining <= 0:
                truncated = True
                break
            child_text, child_truncated = _bounded_value(child, min(512, remaining), depth + 1)
            piece, piece_truncated = _fit_text(child_text, remaining)
            parts.append(piece)
            used += len(piece) + (1 if len(parts) > 1 else 0)
            truncated = truncated or child_truncated or piece_truncated
            if piece_truncated:
                break
        return "[" + ",".join(parts) + "]", truncated
    option_values: dict[str, Any] = {}
    for name in (
        "name", "description", "params", "input_schema", "format_type", "schema",
        "required", "enum_values", "param_type", "strict",
    ):
        try:
            option_value = getattr(value, name)
        except Exception:
            continue
        if getattr(option_value, "value", None) is not None:
            option_value = option_value.value
        option_values[name] = option_value
    if option_values:
        return _bounded_value(option_values, budget, depth + 1)
    return _fit_text(f"<{type(value).__name__}>", budget)


def _append_field(
    fields: list[_PromptField],
    *,
    path: str,
    kind: str,
    value: str,
    length: int,
    sampled_chars: int = 0,
    truncated: bool = False,
) -> None:
    if len(fields) >= MAX_PROMPT_FIELDS:
        return
    fields.append(
        _PromptField(
            path=path,
            kind=kind,
            value=value,
            digest=_sha256_text(value),
            length=max(0, length),
            sampled_chars=max(0, sampled_chars),
            truncated=truncated,
        )
    )


def _message_parts(
    messages: Iterable[Any],
    tools: Any = None,
    response_format: Any = None,
) -> list[_PromptField]:
    fields: list[_PromptField] = []
    remaining_text_chars = MAX_PROMPT_TEXT_CHARS
    remaining_binary_bytes = MAX_BINARY_HASH_BYTES
    try:
        for message_index, message in enumerate(messages):
            if len(fields) >= MAX_PROMPT_FIELDS:
                break
            role_obj = getattr(message, "role", "unknown")
            role = getattr(role_obj, "value", role_obj)
            role_text = _safe_name(role, 32)
            _append_field(
                fields,
                path=f"messages[{message_index}].role",
                kind="role",
                value=role_text,
                length=len(role_text),
            )

            content = getattr(message, "content", "")
            parts = content if isinstance(content, list) else [content]
            for part_index, part in enumerate(parts):
                if len(fields) >= MAX_PROMPT_FIELDS:
                    break
                path = f"messages[{message_index}].content[{part_index}]"
                if isinstance(part, str):
                    text = part[:remaining_text_chars]
                    remaining_text_chars -= len(text)
                    _append_field(
                        fields,
                        path=path,
                        kind="text",
                        value=text,
                        length=len(part),
                        sampled_chars=len(text),
                        truncated=len(text) < len(part),
                    )
                    continue

                if isinstance(part, (bytes, bytearray, memoryview)):
                    digest, length, truncated, hashed_bytes = _binary_digest(part, remaining_binary_bytes)
                    remaining_binary_bytes = max(0, remaining_binary_bytes - hashed_bytes)
                    _append_field(
                        fields,
                        path=path,
                        kind="binary",
                        value=digest,
                        length=length,
                        truncated=truncated,
                    )
                    continue

                if isinstance(part, (tuple, list)):
                    if len(part) == 2:
                        payload = part[1]
                    elif len(part) >= 3:
                        payload = part[-1]
                    else:
                        payload = ""
                    digest, length, truncated, hashed_bytes = _binary_digest(payload, remaining_binary_bytes)
                    remaining_binary_bytes = max(0, remaining_binary_bytes - hashed_bytes)
                    _append_field(
                        fields,
                        path=path,
                        kind="binary",
                        value=digest,
                        length=length,
                        truncated=truncated,
                    )
                    continue

                _append_field(
                    fields,
                    path=path,
                    kind="other",
                    value=_sha256_text(type(part).__name__),
                    length=0,
                    truncated=True,
                )

            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                # Tool arguments may contain prompt text. Keep only a digest.
                encoded, encoded_truncated = _bounded_value(tool_calls, 4096)
                _append_field(
                    fields,
                    path=f"messages[{message_index}].tool_calls",
                    kind="tool_calls",
                    value=_sha256_text(encoded),
                    length=len(encoded),
                    truncated=encoded_truncated,
                )
    except Exception:
        # Invalid custom message objects should still produce a bounded diagnostic.
        if not fields:
            _append_field(fields, path="messages", kind="unknown", value="", length=0)

    for path, value in (("request.tools", tools), ("request.response_format", response_format)):
        if len(fields) >= MAX_PROMPT_FIELDS:
            break
        signature, signature_truncated = _bounded_value(value, 4096)
        _append_field(
            fields,
            path=path,
            kind="request_option",
            value=_sha256_text(signature),
            length=len(signature),
            truncated=signature_truncated,
        )
    return fields


def _snapshot(
    messages: Iterable[Any],
    tools: Any = None,
    response_format: Any = None,
    key_memory_bytes: int = 0,
) -> _PromptSnapshot:
    fields = _message_parts(messages, tools=tools, response_format=response_format)
    fingerprint_source = "\n".join(f"{f.path}\0{f.kind}\0{f.digest}\0{f.length}" for f in fields)
    fingerprint = _sha256_text(fingerprint_source)
    sampled_chars = sum(field.sampled_chars for field in fields)
    truncated = any(field.truncated for field in fields) or len(fields) >= MAX_PROMPT_FIELDS
    memory_bytes = (
        sum(field.memory_bytes for field in fields)
        + sys.getsizeof(tuple(fields))
        + sys.getsizeof(fingerprint_source)
        + sys.getsizeof(fingerprint)
        + key_memory_bytes
        + 128
    )
    return _PromptSnapshot(
        fields=tuple(fields),
        fingerprint=fingerprint,
        sampled_chars=sampled_chars,
        truncated=truncated,
        memory_bytes=memory_bytes,
    )


def _common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _field_signature(field: _PromptField) -> tuple[str, str, int]:
    return field.kind, field.digest, field.length


def _sliding_match(
    previous: _PromptSnapshot,
    current: _PromptSnapshot,
) -> tuple[bool, int | None, int | None, int]:
    best = (0, 0, 0)
    previous_fields = previous.fields
    current_fields = current.fields
    for current_index, current_field in enumerate(current_fields):
        for previous_index, previous_field in enumerate(previous_fields):
            if _field_signature(current_field) != _field_signature(previous_field):
                continue
            run = 1
            while (
                current_index + run < len(current_fields)
                and previous_index + run < len(previous_fields)
                and _field_signature(current_fields[current_index + run])
                == _field_signature(previous_fields[previous_index + run])
            ):
                run += 1
            substantial = run >= 2 or min(current_field.length, previous_field.length) >= 32
            if (current_index or previous_index) and substantial and run > best[0]:
                best = (run, current_index, previous_index)
    if not best[0]:
        return False, None, None, 0
    return True, best[1], best[2], best[0]


def _compare(
    previous: _PromptSnapshot | None,
    current: _PromptSnapshot,
    *,
    include_sliding: bool = True,
) -> dict[str, Any]:
    if previous is None:
        return {
            "estimated_prefix_chars": None,
            "first_changed_field": None,
            "changed_offset_chars": None,
            "sliding_detected": False,
            "sliding_current_offset_fields": None,
            "sliding_previous_offset_fields": None,
            "sliding_match_fields": 0,
            "comparison_reference_age": None,
        }

    prefix_chars = 0
    changed_field = None
    changed_offset = None
    max_fields = max(len(previous.fields), len(current.fields))
    for index in range(max_fields):
        old = previous.fields[index] if index < len(previous.fields) else None
        new = current.fields[index] if index < len(current.fields) else None
        if old is None or new is None:
            changed_field = new.path if new else old.path
            changed_offset = 0
            break
        if old.path == new.path and old.kind == new.kind and old.digest == new.digest and old.length == new.length:
            if old.kind == "text":
                prefix_chars += min(old.sampled_chars, new.sampled_chars)
            continue

        changed_field = new.path
        changed_offset = 0
        if old.path == new.path and old.kind == new.kind == "text":
            changed_offset = _common_prefix_chars(old.value, new.value)
            prefix_chars += changed_offset
        break
    else:
        prefix_chars = min(previous.sampled_chars, current.sampled_chars)

    if include_sliding:
        sliding, current_offset, previous_offset, match_fields = _sliding_match(previous, current)
    else:
        sliding, current_offset, previous_offset, match_fields = False, None, None, 0
    return {
        "estimated_prefix_chars": prefix_chars,
        "first_changed_field": changed_field,
        "changed_offset_chars": changed_offset,
        "sliding_detected": sliding,
        "sliding_current_offset_fields": current_offset,
        "sliding_previous_offset_fields": previous_offset,
        "sliding_match_fields": match_fields,
    }


def _best_comparison(previous: Iterable[_PromptSnapshot], current: _PromptSnapshot) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_snapshot: _PromptSnapshot | None = None
    best_score = -1
    best_age: int | None = None
    previous_items = list(previous)
    for age, snapshot in enumerate(reversed(previous_items), start=1):
        candidate = _compare(snapshot, current, include_sliding=False)
        score = candidate["estimated_prefix_chars"]
        score = -1 if score is None else score
        if score > best_score:
            best = candidate
            best_snapshot = snapshot
            best_score = score
            best_age = age
    if best is None:
        return _compare(None, current)
    sliding, current_offset, previous_offset, match_fields = _sliding_match(best_snapshot, current)
    best["sliding_detected"] = sliding
    best["sliding_current_offset_fields"] = current_offset
    best["sliding_previous_offset_fields"] = previous_offset
    best["sliding_match_fields"] = match_fields
    best["comparison_reference_age"] = best_age
    return best


def _session_scope(session_id: str | None) -> str:
    if not isinstance(session_id, str) or not session_id:
        return "callsite"
    # Session identifiers remain useful for stable grouping without writing IDs.
    return "session:" + hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def _key_for(model_info: Any, request_type: str, session_id: str | None) -> tuple[str, str, str, str, str]:
    provider = _safe_name(getattr(model_info, "api_provider", "unknown"))
    model = _safe_name(getattr(model_info, "name", "unknown"))
    model_identifier = _safe_name(getattr(model_info, "model_identifier", "unknown"))
    return provider, model, model_identifier, _safe_name(request_type), _session_scope(session_id)


def _usage_values(usage: Any) -> tuple[bool, int | None, int | None]:
    if usage is None or getattr(usage, "prompt_cache_reported", False) is not True:
        return False, None, None
    try:
        hit = max(0, int(getattr(usage, "prompt_cache_hit_tokens", 0)))
        miss = max(0, int(getattr(usage, "prompt_cache_miss_tokens", 0)))
    except (TypeError, ValueError, OverflowError):
        return False, None, None
    prompt = max(0, int(getattr(usage, "prompt_tokens", 0) or 0))
    hit = min(hit, prompt)
    miss = prompt - hit
    return True, hit, miss


def _default_output_dir() -> Path:
    return Path(__file__).parents[2] / "logs" / "llm_cache_stats"


def _ensure_writer() -> None:
    global _writer_thread
    with _writer_guard:
        if _writer_thread is not None and _writer_thread.is_alive():
            return
        _writer_thread = threading.Thread(target=_writer_loop, name="llm-cache-stats-writer", daemon=True)
        _writer_thread.start()


def record_request(
    *,
    messages: Iterable[Any],
    model_info: Any,
    request_type: str,
    session_id: str | None = None,
    usage: Any = None,
    tools: Any = None,
    response_format: Any = None,
    enabled: bool = False,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Record one successful prompt attempt; queue all disk writes off-thread.

    The returned dictionary is safe for tests/logging: it contains only hashes,
    field paths, counts, and bounded names. Prompt text and binary content stay
    in bounded in-memory comparison state and are never emitted to disk.
    """
    global _history_bytes, _dropped_records
    if not enabled:
        return None
    try:
        key = _key_for(model_info, request_type, session_id)
        key_memory_bytes = sys.getsizeof(key) + sum(sys.getsizeof(part) for part in key)
        snapshot = _snapshot(
            messages,
            tools=tools,
            response_format=response_format,
            key_memory_bytes=key_memory_bytes,
        )
        with _state_lock:
            state = _history.get(key)
            comparison = _best_comparison(state or (), snapshot)
            if state is None:
                while len(_history) >= MAX_KEYS:
                    _, expired = _history.popitem(last=False)
                    _history_bytes -= sum(item.memory_bytes for item in expired)
                state = deque()
                _history[key] = state
            else:
                _history.move_to_end(key)

            state.append(snapshot)
            _history_bytes += snapshot.memory_bytes
            while len(state) > MAX_ENTRIES_PER_KEY:
                _history_bytes -= state.popleft().memory_bytes
            while _history_bytes > MAX_GLOBAL_BYTES and _history:
                oldest_key = next(iter(_history))
                oldest_state = _history[oldest_key]
                _history_bytes -= oldest_state.popleft().memory_bytes
                if not oldest_state:
                    del _history[oldest_key]

            history_depth = len(state)

        actual_reported, actual_hit, actual_miss = _usage_values(usage)
        provider, model, model_identifier, req_type, session_scope = key
        record = {
            "schema_version": 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "model_identifier": model_identifier,
            "request_type": req_type,
            "session_scope": session_scope,
            "diagnostic_scope_kind": "session" if session_scope.startswith("session:") else "callsite",
            "estimate_basis": "pre-provider Message character-prefix estimate",
            "estimated_prefix_chars": comparison["estimated_prefix_chars"],
            "first_changed_field": comparison["first_changed_field"],
            "changed_offset_chars": comparison["changed_offset_chars"],
            "comparison_reference_age": comparison["comparison_reference_age"],
            "sliding_detected": comparison["sliding_detected"],
            "sliding_current_offset_fields": comparison["sliding_current_offset_fields"],
            "sliding_previous_offset_fields": comparison["sliding_previous_offset_fields"],
            "sliding_match_fields": comparison["sliding_match_fields"],
            "actual_provider_counts_reported": actual_reported,
            "actual_prompt_cache_hit_tokens": actual_hit,
            "actual_prompt_cache_miss_tokens": actual_miss,
            "prompt_fingerprint": snapshot.fingerprint,
            "prompt_text_chars_sampled": snapshot.sampled_chars,
            "prompt_snapshot_truncated": snapshot.truncated,
            "estimate_complete": not snapshot.truncated,
            "history_depth": history_depth,
        }
        target_dir = Path(output_dir) if output_dir is not None else _default_output_dir()
        _ensure_writer()
        try:
            _write_queue.put_nowait((target_dir, record))
        except queue.Full:
            with _state_lock:
                _dropped_records += 1
        return record
    except Exception:
        # This instrumentation is never allowed to change inference success.
        return None


def _rotate(path: Path) -> None:
    backup = path.with_name(path.name + ".1")
    try:
        if backup.exists():
            backup.unlink()
        if path.exists():
            path.replace(backup)
    except OSError:
        pass


def _append_jsonl(directory: Path, record: dict[str, Any]) -> None:
    path = directory / "llm_cache_stats.jsonl"
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_FILE_BYTES:
        return
    if path.exists() and path.stat().st_size + len(encoded) > MAX_OUTPUT_FILE_BYTES:
        _rotate(path)
    with path.open("a", encoding="utf-8") as output:
        output.write(line)


def _write_html(directory: Path) -> None:
    jsonl_path = directory / "llm_cache_stats.jsonl"
    html_path = directory / "llm_cache_stats.html"
    try:
        if not jsonl_path.exists():
            return
        with jsonl_path.open("r", encoding="utf-8") as source:
            rows = [line for line in source.readlines()[-MAX_HTML_ROWS:] if line.strip()]
        headings = (
            "UTC time", "Provider / model", "Type / scope", "Estimated prefix (chars)",
            "Actual cache hit / miss (tokens)", "First changed field / offset", "Sliding",
        )
        output = [
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>LLM prompt cache diagnostics</title>",
            "<style>body{font:14px sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}",
            "th,td{border:1px solid #bbb;padding:.45rem;text-align:left;vertical-align:top}",
            "th{background:#eee}</style></head><body><h1>LLM prompt cache diagnostics</h1><table><thead><tr>",
        ]
        output.extend(f"<th>{html.escape(label)}</th>" for label in headings)
        output.append("</tr></thead><tbody>")
        parsed_rows = []
        for line in rows:
            try:
                parsed_rows.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue

        row_window = parsed_rows
        while True:
            rendered_rows = []
            for record in row_window:
                estimate = record.get("estimated_prefix_chars")
                actual = (
                    f"{record.get('actual_prompt_cache_hit_tokens')} / {record.get('actual_prompt_cache_miss_tokens')}"
                    if record.get("actual_provider_counts_reported")
                    else "unreported"
                )
                changed = record.get("first_changed_field")
                if changed is not None:
                    changed = f"{changed} / {record.get('changed_offset_chars')}"
                values = (
                    record.get("timestamp_utc", ""),
                    f"{record.get('provider', '')} / {record.get('model', '')}",
                    f"{record.get('request_type', '')} / {record.get('session_scope', '')}",
                    "n/a" if estimate is None else str(estimate),
                    actual,
                    "n/a" if changed is None else str(changed),
                    "yes" if record.get("sliding_detected") else "no",
                )
                rendered_rows.append(
                    "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>"
                )
            window_note = (
                f"Showing the latest {len(row_window)} rows within the HTML size limit."
                if len(row_window) < len(parsed_rows)
                else f"Showing {len(row_window)} retained rows."
            )
            content = "\n".join(
                output
                + rendered_rows
                + [
                    "</tbody></table><p>Prefix lengths are pre-provider character estimates. "
                    "Actual counts come from provider usage metadata.</p>",
                    f"<p>{html.escape(window_note)}</p></body></html>",
                ]
            )
            encoded = content.encode("utf-8")
            if len(encoded) <= MAX_OUTPUT_FILE_BYTES or len(row_window) <= 1:
                break
            row_window = row_window[-max(1, len(row_window) // 2):]
        temp_path = html_path.with_name(html_path.name + ".tmp")
        with temp_path.open("w", encoding="utf-8") as output_file:
            output_file.write(content)
        temp_path.replace(html_path)
    except OSError:
        return


def _write_record_to_files(directory: Path, record: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _append_jsonl(directory, record)
    _write_html(directory)
    html_path = directory / "llm_cache_stats.html"
    if html_path.exists() and html_path.stat().st_size > MAX_OUTPUT_FILE_BYTES:
        _rotate(html_path)


def _writer_loop() -> None:
    while True:
        item = _write_queue.get()
        try:
            if item is None:
                return
            directory, record = item
            _write_record_to_files(directory, record)
        except Exception:
            # Storage is best-effort; the writer must remain available.
            pass
        finally:
            _write_queue.task_done()


def snapshot() -> dict[str, Any]:
    """Return bounded diagnostic state for tests and operational inspection."""
    with _state_lock:
        return {
            "keys": len(_history),
            "entries": sum(len(entries) for entries in _history.values()),
            "entries_by_key": {"max": MAX_ENTRIES_PER_KEY},
            "estimated_retained_bytes": _history_bytes,
            "max_estimated_retained_bytes": MAX_GLOBAL_BYTES,
            "max_keys": MAX_KEYS,
            "queue_depth": _write_queue.qsize(),
            "queue_capacity": MAX_QUEUE_ENTRIES,
            "dropped_records": _dropped_records,
        }


def reset(*, clear_pending: bool = True) -> None:
    """Clear in-memory comparison state; intended for tests and explicit resets."""
    global _history_bytes, _dropped_records
    with _state_lock:
        _history.clear()
        _history_bytes = 0
        _dropped_records = 0
    if clear_pending:
        while True:
            try:
                _write_queue.get_nowait()
            except queue.Empty:
                break
            else:
                _write_queue.task_done()


def flush(timeout: float = 5.0) -> bool:
    """Wait for queued reports to finish, primarily for isolated tests."""
    deadline = time.monotonic() + max(0.0, timeout)
    while _write_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(min(_WRITER_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return _write_queue.unfinished_tasks == 0
