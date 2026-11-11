"""Small, bounded renderers for untrusted prompt evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


_TRUNCATED = "...[truncated]"
_ITEMS_OMITTED = "...[items omitted]"
_DEPTH_LIMIT = "[depth limit]"
_NODE_LIMIT = "...[items omitted]"
_CYCLE = "[cycle]"
_UNSUPPORTED = "[unsupported value]"
_MAX_LABEL_CHARS = 120
_MAX_SECTIONS = 64


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _marker_for_budget(marker: str, budget: int) -> str:
    if budget >= len(marker):
        return marker
    if budget:
        return "…"[:budget]
    return ""


def _truncate_text_with_status(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    marker = _marker_for_budget(_TRUNCATED, max_chars)
    return value[: max_chars - len(marker)] + marker, True


def _truncate_text(value: str, max_chars: int) -> str:
    return _truncate_text_with_status(value, max_chars)[0]


def _escaped_char(char: str) -> str:
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    escaped = escapes.get(char)
    if escaped is not None:
        return escaped
    if ord(char) < 0x20:
        return f"\\u{ord(char):04x}"
    return char


def _quoted_string(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", len(value) > 0
    if max_chars == 1:
        return ("…", True) if value else ('"', False)

    chunks: list[str] = []
    escaped_length = 0
    for char in value:
        escaped = _escaped_char(char)
        if escaped_length + len(escaped) + 2 <= max_chars:
            chunks.append(escaped)
            escaped_length += len(escaped)
            continue

        marker = _marker_for_budget(_TRUNCATED, max_chars - 2)
        available = max_chars - 2 - len(marker)
        while chunks and escaped_length > available:
            escaped_length -= len(chunks.pop())
        if len(marker) > max_chars - 2:
            return marker, True
        return '"' + "".join(chunks) + marker + '"', True

    if len(value) <= len(chunks):
        return '"' + "".join(chunks) + '"', False
    # The loop may stop early only after exhausting the output budget.
    marker = _marker_for_budget(_TRUNCATED, max_chars - 2)
    available = max_chars - 2 - len(marker)
    while chunks and escaped_length > available:
        escaped_length -= len(chunks.pop())
    if len(marker) > max_chars - 2:
        return marker, True
    return '"' + "".join(chunks) + marker + '"', True


class _RenderState:
    __slots__ = ("max_depth", "max_nodes", "nodes", "active")

    def __init__(self, max_depth: int, max_nodes: int) -> None:
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.nodes = 0
        self.active: set[int] = set()


def _container(
    value: dict[Any, Any] | list[Any] | tuple[Any, ...],
    budget: int,
    state: _RenderState,
    depth: int,
) -> tuple[str, bool]:
    value_type = type(value)
    is_mapping = value_type is dict
    opening, closing = ("{", "}") if is_mapping else ("[", "]")
    if depth >= state.max_depth:
        return _truncate_text_with_status(_DEPTH_LIMIT, budget)
    if budget < 2:
        return _marker_for_budget(_TRUNCATED, budget), True

    identity = id(value)
    if identity in state.active:
        return _truncate_text_with_status(_CYCLE, budget)

    state.active.add(identity)
    entries: list[str] = []
    used = 1
    was_truncated = False
    index = 0
    total = len(value)
    iterator = iter(value.items()) if is_mapping else iter(value)
    try:
        while index < total and state.nodes < state.max_nodes:
            separator_length = 2 if entries else 0
            child_budget = budget - used - separator_length - 1
            if child_budget <= 0:
                was_truncated = True
                break
            try:
                item = next(iterator)
            except StopIteration:
                break

            if is_mapping:
                key, child = item
                key_budget = min(256, max(1, child_budget - 3))
                key_text, key_truncated = _value(key, key_budget, state, depth + 1, True)
                pair_prefix = key_text + ": "
                child_value_budget = max(0, child_budget - len(pair_prefix))
                child_text, child_truncated = _value(child, child_value_budget, state, depth + 1, True)
                rendered = pair_prefix + child_text
                child_truncated = key_truncated or child_truncated or child_value_budget <= 0
            else:
                rendered, child_truncated = _value(item, child_budget, state, depth + 1, True)

            if len(rendered) > child_budget:
                rendered, _ = _truncate_text_with_status(rendered, child_budget)
                child_truncated = True
            if not rendered:
                was_truncated = True
                break
            entries.append(rendered)
            used += separator_length + len(rendered)
            index += 1
            was_truncated = was_truncated or child_truncated

        if index >= total:
            return opening + ", ".join(entries) + closing, was_truncated
        was_truncated = True

        # Only now reserve room for the omission marker. If the existing bounded
        # prefix consumed that room, shorten its final element to make space.
        while True:
            separator_length = 2 if entries else 0
            marker = _marker_for_budget(_ITEMS_OMITTED, max(0, budget - 2 - separator_length))
            content_limit = budget - 2 - len(marker) - separator_length
            if not entries:
                break
            content_used = sum(map(len, entries)) + 2 * (len(entries) - 1)
            if content_used <= content_limit:
                break

            prior_used = sum(map(len, entries[:-1])) + 2 * max(0, len(entries) - 2)
            last_allowance = max(0, content_limit - prior_used - (2 if len(entries) > 1 else 0))
            if last_allowance > 0:
                entries[-1], _ = _truncate_text_with_status(entries[-1], last_allowance)
                break
            entries.pop()

        separator = ", " if entries else ""
        result = opening + separator.join(entries + ([marker] if marker else [])) + closing
        if len(result) > budget:
            result, _ = _truncate_text_with_status(result, budget)
        return result, True
    finally:
        state.active.remove(identity)


def _value(value: Any, budget: int, state: _RenderState, depth: int, nested: bool) -> tuple[str, bool]:
    if budget <= 0:
        return "", True
    if state.nodes >= state.max_nodes:
        return _marker_for_budget(_NODE_LIMIT, budget), True
    state.nodes += 1

    value_type = type(value)
    if value_type is str:
        return _quoted_string(value, budget) if nested else _truncate_text_with_status(value, budget)
    if value is None:
        return _truncate_text_with_status("null", budget)
    if value_type is bool:
        return _truncate_text_with_status("true" if value else "false", budget)
    if value_type is int:
        bits = value.bit_length()
        estimated_digits = (bits * 30103) // 100000 + 1
        if estimated_digits + (1 if value < 0 else 0) > budget + 1:
            return _marker_for_budget("...[integer omitted]", budget), True
        try:
            return _truncate_text_with_status(str(value), budget)
        except (ValueError, OverflowError):
            return _marker_for_budget("...[integer omitted]", budget), True
    if value_type is float:
        return _truncate_text_with_status(repr(value), budget)

    if value_type not in (dict, list, tuple):
        return _truncate_text_with_status(_UNSUPPORTED, budget)
    return _container(value, budget, state, depth)


def render_bounded_value(
    value: Any,
    max_chars: int = 8000,
    *,
    max_depth: int = 8,
    max_nodes: int = 512,
) -> str:
    """Render a builtin JSON-like value without exceeding ``max_chars``.

    Top-level strings stay byte-for-byte unchanged when they fit. Unknown objects
    are represented by a fixed marker; their ``str`` and ``repr`` methods are
    never called.
    """

    return _render_bounded_value(value, max_chars, max_depth=max_depth, max_nodes=max_nodes)[0]


def _render_bounded_value(
    value: Any,
    max_chars: int,
    *,
    max_depth: int = 8,
    max_nodes: int = 512,
) -> tuple[str, bool]:
    max_chars = _positive_int(max_chars, "max_chars")
    max_depth = _positive_int(max_depth, "max_depth")
    max_nodes = _positive_int(max_nodes, "max_nodes")
    return _value(value, max_chars, _RenderState(max_depth, max_nodes), 0, False)


def _safe_label(label: Any) -> str:
    if type(label) is not str:
        label = _UNSUPPORTED
    return _truncate_text(label, _MAX_LABEL_CHARS)


def _bounded_wrappers(prefix: str, suffix: str, max_chars: int) -> tuple[str, str]:
    if len(prefix) + len(suffix) <= max_chars:
        return prefix, suffix
    if len(suffix) >= max_chars:
        return "", suffix[-max_chars:]
    return prefix[: max_chars - len(suffix)], suffix


def render_bounded_sections(
    sections: Iterable[tuple[str, Any]],
    *,
    prefix: str = "",
    suffix: str = "",
    max_chars: int,
    section_max_chars: int = 8000,
    label_separator: str = "\n",
    section_separator: str = "\n",
) -> str:
    """Join bounded, fairly allocated evidence sections between trusted wrappers."""

    max_chars = _positive_int(max_chars, "max_chars")
    section_max_chars = _positive_int(section_max_chars, "section_max_chars")
    if type(prefix) is not str or type(suffix) is not str:
        raise TypeError("prefix and suffix must be strings")
    if type(label_separator) is not str or type(section_separator) is not str:
        raise TypeError("section separators must be strings")

    prefix, suffix = _bounded_wrappers(prefix, suffix, max_chars)
    room = max_chars - len(prefix) - len(suffix)
    if room <= 0:
        return prefix + suffix

    candidates: list[tuple[str, Any]] = []
    iterator = iter(sections)
    for _index in range(_MAX_SECTIONS):
        try:
            pair = next(iterator)
        except StopIteration:
            break
        try:
            label, value = pair
        except (TypeError, ValueError):
            label, value = _UNSUPPORTED, pair
        candidates.append((_safe_label(label), value))

    has_more = False
    if len(candidates) == _MAX_SECTIONS:
        try:
            next(iterator)
            has_more = True
        except StopIteration:
            pass
    if has_more:
        candidates = candidates[: _MAX_SECTIONS - 1]
        candidates.append(("Additional sections", "...[sections omitted]"))
    if not candidates:
        return prefix + suffix

    # If headings alone do not fit, retain a bounded prefix of sections and add
    # an omission note when space permits.
    accepted: list[tuple[str, Any]] = []
    fixed_cost = 0
    omitted = False
    for candidate in candidates:
        label, _value_item = candidate
        needed = len(label) + len(label_separator) + len(section_separator) + 1
        if fixed_cost + needed > room:
            omitted = True
            break
        accepted.append(candidate)
        fixed_cost += needed
    if omitted:
        marker_section = ("Additional sections", "...[sections omitted]")
        marker_cost = len(marker_section[0]) + len(label_separator) + len(section_separator) + 1
        while accepted and fixed_cost + marker_cost > room:
            removed_label, _removed_value = accepted.pop()
            fixed_cost -= len(removed_label) + len(label_separator) + len(section_separator) + 1
        if fixed_cost + marker_cost <= room:
            accepted.append(marker_section)
            fixed_cost += marker_cost
    if not accepted:
        return prefix + suffix

    extra_content_room = room - fixed_cost
    extra_per_section = min(section_max_chars - 1, extra_content_room // len(accepted))
    allocations = [1 + extra_per_section for _ in accepted]
    remainder = extra_content_room - extra_per_section * len(accepted)
    for index in range(len(allocations)):
        extra = min(remainder, section_max_chars - allocations[index])
        allocations[index] += extra
        remainder -= extra
        if remainder <= 0:
            break

    rendered_and_truncated = [
        _render_bounded_value(value, allocation)
        for (_label, value), allocation in zip(accepted, allocations, strict=True)
    ]
    rendered = [text for text, _truncated in rendered_and_truncated]

    # Share space left by short sections among sections that filled their first
    # fair share. Recompute spare from actual output after each bounded rerender,
    # since a section may finish before using all of its new allowance.
    available_content = len(accepted) + extra_content_room
    spare = available_content - sum(map(len, rendered))
    eligible = [
        index
        for index, (allocation, (_text, truncated)) in enumerate(
            zip(allocations, rendered_and_truncated, strict=True)
        )
        if truncated and allocation < section_max_chars
    ]
    for index in tuple(eligible):
        if spare <= 0:
            break
        previous_length = len(rendered[index])
        new_budget = min(section_max_chars, previous_length + spare)
        if new_budget <= allocations[index]:
            continue
        allocations[index] = new_budget
        _label, value = accepted[index]
        rendered[index], _truncated = _render_bounded_value(value, allocations[index])
        spare = available_content - sum(map(len, rendered))

    body = "".join(
        label + label_separator + content + section_separator
        for (label, _value_item), content in zip(accepted, rendered, strict=True)
    )
    return prefix + body + suffix
