"""Render regular-expression command patterns as bounded, readable triggers.

This module deliberately parses patterns without compiling or executing them.
Patterns outside the small supported subset fall back to their registered
command name instead of exposing regular-expression syntax in user-facing help.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

try:
    from re import _constants as _constants
    from re import _parser as _parser
except ImportError:  # pragma: no cover - supported Python versions provide these modules
    _constants = None
    _parser = None


_MAX_PATTERN_LENGTH = 512
_MAX_NODES = 256
_MAX_DEPTH = 20
_MAX_EXPANSIONS = 16
_MAX_DISPLAY_LENGTH = 320
_WHITESPACE = " \t\r\n\v\f\u3000"
_COMMAND_MARKERS = {"#", "/", "!", "！"}


class _UnsupportedPattern(Exception):
    """Raised when a pattern cannot be represented safely and clearly."""


def _fallback_name(name: Optional[str]) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        return "命令"
    if candidate.startswith(("#", "/", "!", "！")):
        return candidate[:_MAX_DISPLAY_LENGTH]
    safe_name = re.sub(r"[^\w-]+", "_", candidate, flags=re.UNICODE).strip("_")
    return f"#{safe_name or '命令'}"[:_MAX_DISPLAY_LENGTH]


def _known_display(pattern: str) -> Optional[str]:
    """Preserve established hand-tuned display conventions."""
    if "lang_switch" in pattern:
        return "#lang_switch"
    if "mus_rand" in pattern:
        return "#mus_rand"
    if "点歌|播放|来首" in pattern:
        return "点歌/播放/来首 + 关键词"
    if "diary_" in pattern:
        tag_match = re.search(r"#diary_\w+", pattern)
        if tag_match:
            return tag_match.group(0)
    if "advanced_enable" in pattern or "#adv_on" in pattern:
        return "#adv_on"
    if "advanced_disable" in pattern or "#adv_off" in pattern:
        return "#adv_off"
    if "advanced_check" in pattern or "#adv_check" in pattern:
        return "#adv_check"
    return None


def _is_manual_placeholder_display(pattern: str) -> bool:
    if "<" not in pattern or ">" not in pattern or "\\" in pattern:
        return False
    if any(character in pattern for character in "^$()[]?*+|{}."):
        return False
    inside_placeholder = False
    saw_placeholder = False
    for character in pattern:
        if character == "<":
            if inside_placeholder:
                return False
            inside_placeholder = True
            saw_placeholder = True
        elif character == ">":
            if not inside_placeholder:
                return False
            inside_placeholder = False
    return saw_placeholder and not inside_placeholder


def format_pattern_for_display(pattern: str, fallback_name: Optional[str] = None) -> str:
    """Convert a regex trigger to help text, falling back to a safe command name."""
    if not pattern:
        return _fallback_name(fallback_name)

    known_display = _known_display(pattern)
    if known_display is not None:
        return known_display

    # Manual entries use angle-bracket placeholders as display text, not regex.
    if _is_manual_placeholder_display(pattern):
        return pattern

    try:
        rendered = _render_pattern(pattern)
    except (AttributeError, IndexError, RecursionError, TypeError, ValueError, _UnsupportedPattern, re.error):
        return _fallback_name(fallback_name)

    if not rendered or len(rendered) > _MAX_DISPLAY_LENGTH:
        return _fallback_name(fallback_name)
    return rendered


def _render_pattern(pattern: str) -> str:
    if _parser is None or _constants is None or len(pattern) > _MAX_PATTERN_LENGTH:
        raise _UnsupportedPattern

    parsed = _parser.parse(pattern, 0)
    group_names = {index: name for name, index in parsed.state.groupdict.items()}
    tokens = list(parsed)
    _count_nodes(tokens)
    tokens = _unwrap_root_capture(tokens, group_names)
    tokens = _strip_leading_prefix(tokens)
    if not _contains_visible_literal(tokens):
        raise _UnsupportedPattern
    variants = _render_tokens(tokens, group_names, depth=0)
    if not variants:
        raise _UnsupportedPattern

    normalized = []
    for value in variants:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in normalized:
            normalized.append(value)
    if not normalized or len(normalized) > _MAX_EXPANSIONS:
        raise _UnsupportedPattern
    return "/".join(normalized)


def _count_nodes(tokens: Sequence[tuple], depth: int = 0) -> int:
    if depth > _MAX_DEPTH:
        raise _UnsupportedPattern
    total = len(tokens)
    if total > _MAX_NODES:
        raise _UnsupportedPattern
    for operation, argument in tokens:
        nested: Optional[Sequence[tuple]] = None
        if operation is _constants.SUBPATTERN:
            nested = argument[3]
        elif operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT}:
            nested = argument[2]
        elif operation is _constants.BRANCH:
            nested = None
            for branch in argument[1]:
                total += _count_nodes(branch, depth + 1)
        if nested is not None:
            total += _count_nodes(nested, depth + 1)
        if total > _MAX_NODES:
            raise _UnsupportedPattern
    return total


def _unwrap_root_capture(tokens: list[tuple], group_names: dict[int, str]) -> list[tuple]:
    meaningful = [
        token
        for token in tokens
        if not (token[0] is _constants.AT and token[1] in _anchor_constants())
    ]
    if len(meaningful) != 1 or meaningful[0][0] is not _constants.SUBPATTERN:
        return tokens
    group_number, _add_flags, _delete_flags, child = meaningful[0][1]
    if group_number in group_names:
        return list(child)
    return tokens


def _anchor_constants() -> set:
    return {
        _constants.AT_BEGINNING,
        _constants.AT_BEGINNING_STRING,
        _constants.AT_END,
        _constants.AT_END_STRING,
    }


def _strip_leading_prefix(tokens: list[tuple]) -> list[tuple]:
    """Drop optional leading whitespace and optional non-command prefixes.

    Chat adapters often prepend optional mention tokens before a fixed command.
    They are transport syntax, so show the trigger beginning at its literal
    command text. The rule is structural and applies to any leading optional
    group without a command marker, rather than mapping particular plugins.
    """
    result = list(tokens)
    while result:
        operation, argument = result[0]
        if operation is _constants.AT and argument in _anchor_constants():
            result.pop(0)
            continue
        if operation is _constants.MAX_REPEAT and _is_whitespace_tokens(argument[2]):
            result.pop(0)
            continue
        if operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT}:
            minimum, _maximum, child = argument
            if minimum == 0 and not _contains_command_marker(child) and _has_fixed_trigger(result[1:]):
                result.pop(0)
                continue
        break
    while result and result[-1][0] is _constants.AT and result[-1][1] in _anchor_constants():
        result.pop()
    return result


def _has_fixed_trigger(tokens: Sequence[tuple]) -> bool:
    for operation, argument in tokens:
        if _is_whitespace_token(operation, argument):
            continue
        if operation is _constants.LITERAL:
            return chr(argument) in _COMMAND_MARKERS or bool(chr(argument).strip())
        if operation is _constants.BRANCH:
            branches = argument[1]
            return bool(branches) and all(_branch_starts_with_literal(branch) for branch in branches)
        if operation is _constants.SUBPATTERN:
            return _has_fixed_trigger(argument[3])
        return False
    return False


def _branch_starts_with_literal(tokens: Sequence[tuple]) -> bool:
    for operation, argument in tokens:
        if _is_whitespace_token(operation, argument):
            continue
        if operation is _constants.LITERAL:
            return True
        if operation is _constants.SUBPATTERN:
            return _branch_starts_with_literal(argument[3])
        return False
    return False


def _contains_command_marker(tokens: Sequence[tuple]) -> bool:
    for operation, argument in tokens:
        if operation is _constants.LITERAL and chr(argument) in _COMMAND_MARKERS:
            return True
        if operation is _constants.SUBPATTERN and _contains_command_marker(argument[3]):
            return True
        if operation is _constants.BRANCH and any(_contains_command_marker(branch) for branch in argument[1]):
            return True
        if operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT} and _contains_command_marker(argument[2]):
            return True
    return False


def _contains_visible_literal(tokens: Sequence[tuple]) -> bool:
    for operation, argument in tokens:
        if operation is _constants.LITERAL and chr(argument) not in _WHITESPACE:
            return True
        if operation is _constants.SUBPATTERN and _contains_visible_literal(argument[3]):
            return True
        if operation is _constants.BRANCH and any(_contains_visible_literal(branch) for branch in argument[1]):
            return True
        if operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT} and _contains_visible_literal(argument[2]):
            return True
    return False


def _render_tokens(tokens: Sequence[tuple], group_names: dict[int, str], depth: int) -> list[str]:
    if depth > _MAX_DEPTH:
        raise _UnsupportedPattern
    rendered = [""]
    for operation, argument in tokens:
        if operation is _constants.AT and argument in _anchor_constants():
            part = [""]
        elif operation is _constants.LITERAL:
            part = [chr(argument)]
        elif operation is _constants.SUBPATTERN:
            part = _render_subpattern(argument, group_names, depth + 1)
        elif operation is _constants.BRANCH:
            part = []
            for branch in argument[1]:
                part.extend(_render_tokens(branch, group_names, depth + 1))
                _check_expansion_count(part)
        elif operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT}:
            part = _render_repeat(argument, group_names, depth + 1)
        elif operation is _constants.IN:
            part = _render_character_class(argument)
        else:
            raise _UnsupportedPattern
        rendered = _combine(rendered, part)
    return rendered


def _render_subpattern(argument: tuple, group_names: dict[int, str], depth: int) -> list[str]:
    group_number, _add_flags, _delete_flags, child = argument
    group_name = group_names.get(group_number)
    if group_name:
        literal_variants = _try_render_literal_tokens(child, depth + 1)
        if literal_variants is not None:
            return literal_variants
        return [f"[{group_name}]"]
    return _render_tokens(child, group_names, depth + 1)


def _try_render_literal_tokens(tokens: Sequence[tuple], depth: int) -> Optional[list[str]]:
    try:
        values = _render_tokens(tokens, {}, depth)
    except _UnsupportedPattern:
        return None
    if any("[" in value and "]" in value for value in values):
        return None
    return values


def _render_repeat(argument: tuple, group_names: dict[int, str], depth: int) -> list[str]:
    minimum, maximum, child = argument
    if _is_whitespace_tokens(child):
        return [" " if minimum > 0 else ""]

    if minimum == maximum == 1:
        return _render_tokens(child, group_names, depth + 1)

    optional = minimum == 0 and maximum == 1
    repeated = maximum == _constants.MAXREPEAT

    if repeated and _is_argument_suffix(child):
        placeholder = " [参数]"
        return [_mark_optional(placeholder) if minimum == 0 else placeholder]

    if repeated and len(child) == 1 and child[0][0] is _constants.ANY:
        placeholder = "[文本]"
        return [_mark_optional(placeholder) if minimum == 0 else placeholder]

    if repeated and len(child) == 1 and child[0][0] is _constants.IN:
        placeholder = _character_class_placeholder(child[0][1])
        if placeholder == "[字符]":
            raise _UnsupportedPattern
        return [_mark_optional(placeholder) if minimum == 0 else placeholder]

    if optional:
        values = _render_tokens(child, group_names, depth + 1)
        if values == [""]:
            return values
        return [_mark_optional(value) for value in values]

    raise _UnsupportedPattern


def _mark_optional(value: str) -> str:
    if not value.strip():
        return ""
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    content = value.strip()
    marker = "?" if content.endswith("]") else "（可选）"
    return f"{leading}{content}{marker}{trailing}"


def _is_argument_suffix(tokens: Sequence[tuple]) -> bool:
    saw_whitespace = False
    meaningful = []
    for operation, argument in tokens:
        if _is_whitespace_token(operation, argument):
            saw_whitespace = True
            continue
        if operation is _constants.SUBPATTERN and not saw_whitespace:
            return _is_argument_suffix(argument[3])
        meaningful.append((operation, argument))
    has_variable = any(
        operation in {_constants.IN, _constants.ANY, _constants.MAX_REPEAT, _constants.MIN_REPEAT}
        for operation, _argument in meaningful
    )
    return saw_whitespace and has_variable


def _render_character_class(items: Sequence[tuple]) -> list[str]:
    if not items:
        raise _UnsupportedPattern
    if all(operation is _constants.LITERAL for operation, _argument in items):
        return [chr(argument) for _operation, argument in items]
    if _is_whitespace_class(items):
        return [" "]
    raise _UnsupportedPattern


def _character_class_placeholder(items: Sequence[tuple]) -> str:
    if _is_whitespace_class(items):
        return " "
    if any(argument is _constants.CATEGORY_DIGIT for operation, argument in items if operation is _constants.CATEGORY):
        return "[数字]"
    if any(argument is _constants.CATEGORY_NOT_SPACE for operation, argument in items if operation is _constants.CATEGORY):
        return "[文本]"
    return "[字符]"


def _is_whitespace_tokens(tokens: Sequence[tuple]) -> bool:
    return bool(tokens) and all(_is_whitespace_token(operation, argument) for operation, argument in tokens)


def _is_whitespace_token(operation: object, argument: object) -> bool:
    if operation is _constants.LITERAL:
        return chr(argument) in _WHITESPACE
    if operation is _constants.IN:
        return _is_whitespace_class(argument)
    return False


def _is_whitespace_class(items: Iterable[tuple]) -> bool:
    values = list(items)
    if not values:
        return False
    for operation, argument in values:
        if operation is _constants.LITERAL and chr(argument) in _WHITESPACE:
            continue
        if operation is _constants.CATEGORY and argument is _constants.CATEGORY_SPACE:
            continue
        return False
    return True


def _combine(left: list[str], right: list[str]) -> list[str]:
    combined = []
    for prefix in left:
        for suffix in right:
            combined.append(prefix + suffix)
            _check_expansion_count(combined)
    return combined


def _check_expansion_count(values: Sequence[str]) -> None:
    if len(values) > _MAX_EXPANSIONS:
        raise _UnsupportedPattern

