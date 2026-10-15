"""Normalize adapter prompts for Core's ``str.format`` based Prompt class."""

from __future__ import annotations

import re


_SIMPLE_FIELD = re.compile(r"\{([A-Za-z_]\w*)\}")


def _field_end(template: str, start: int) -> int | None:
    """Return the end of a Core-style field with a simple variable name."""

    match = _SIMPLE_FIELD.match(template, start)
    return match.end() if match else None


def _inject_variables(template: str, variables: dict[str, str]) -> str:
    """Replace adapter-owned variables without touching escaped braces/fields."""

    result: list[str] = []
    index = 0
    while index < len(template):
        if template.startswith((r"\{", r"\}"), index):
            result.append(template[index : index + 2])
            index += 2
            continue
        if template.startswith("{{", index):
            end = _escaped_group_end(template, index)
            result.append(template[index:end])
            index = end
            continue
        if template[index] == "{":
            end = _field_end(template, index)
            if end is not None:
                name = template[index + 1 : end - 1]
                result.append(str(variables.get(name, template[index:end])))
                index = end
                continue
        result.append(template[index])
        index += 1
    return "".join(result)


def _json_object_start(template: str, start: int) -> bool:
    """Whether a brace starts a JSON-like object with a quoted key."""

    if start >= len(template) or template[start] != "{":
        return False
    index = start + 1
    while index < len(template) and template[index].isspace():
        index += 1
    if index >= len(template) or template[index] != '"':
        return False
    index += 1
    escaped = False
    while index < len(template):
        char = template[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            index += 1
            while index < len(template) and template[index].isspace():
                index += 1
            return index < len(template) and template[index] == ":"
        index += 1
    return False


def _json_object_end(template: str, start: int) -> int | None:
    """Find the matching close brace while respecting JSON strings."""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(template)):
        char = template[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _escaped_group_end(template: str, start: int) -> int:
    """Skip an existing ``{{...}}`` format escape, including nested braces."""

    depth = 2
    in_string = False
    escaped = False
    index = start + 2
    while index < len(template):
        char = template[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    # Leave malformed legacy text intact. Core will report the original issue.
    return len(template)


def _escape_json_object(
    template: str,
    start: int,
    end: int,
    *,
    outer_braces_escaped: bool = False,
) -> str:
    """Escape object delimiters while preserving fields inside JSON strings."""

    result: list[str] = []
    index = start + 1 if outer_braces_escaped else start
    content_end = end - 1 if outer_braces_escaped else end
    in_string = False
    while index <= content_end:
        char = template[index]
        if char == "\\" and index < content_end and template[index + 1] in "{}":
            result.append(template[index : index + 2])
            index += 2
            continue
        if in_string:
            if char == "\\" and index < content_end:
                result.append(template[index : index + 2])
                index += 2
                continue
            if char == '"':
                in_string = False
                result.append(char)
                index += 1
                continue
            if template.startswith("{{", index):
                result.append("{{")
                index += 2
                continue
            if char == "{":
                field_end = _field_end(template, index)
                if field_end is not None and field_end - 1 <= end:
                    result.append(template[index:field_end])
                    index = field_end
                    continue
                result.append("{{")
                index += 1
                continue
            if char == "}":
                if template.startswith("}}", index):
                    result.append("}}")
                    index += 2
                else:
                    result.append("}}")
                    index += 1
                continue
            result.append(char)
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
        elif char in "{}":
            if (
                outer_braces_escaped
                and template.startswith(char * 2, index)
                and index + 1 <= content_end
            ):
                result.append(char * 2)
                index += 2
                continue
            result.append(char * 2)
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _escape_json_objects(template: str) -> str:
    """Escape literal JSON objects but keep Core fields and prior escapes."""

    result: list[str] = []
    index = 0
    while index < len(template):
        if template.startswith((r"\{", r"\}"), index):
            result.append(template[index : index + 2])
            index += 2
            continue
        if template.startswith("{{", index):
            wrapped_json_start = index + 1
            if _json_object_start(template, wrapped_json_start):
                wrapped_json_end = _json_object_end(template, wrapped_json_start)
                if (
                    wrapped_json_end is not None
                    and wrapped_json_end + 1 < len(template)
                    and template[wrapped_json_end + 1] == "}"
                ):
                    result.append(
                        "{{"
                        + _escape_json_object(
                            template,
                            wrapped_json_start,
                            wrapped_json_end,
                            outer_braces_escaped=True,
                        )
                        + "}}"
                    )
                    index = wrapped_json_end + 2
                    continue
            end = _escaped_group_end(template, index)
            result.append(template[index:end])
            index = end
            continue
        if _json_object_start(template, index):
            end = _json_object_end(template, index)
            if end is not None:
                result.append(_escape_json_object(template, index, end))
                index = end + 1
                continue
        result.append(template[index])
        index += 1
    return "".join(result)


def normalize_prompt_template(template: str, variables: dict[str, str] | None = None) -> str:
    """Substitute adapter variables and make raw JSON safe for Core Prompt.format.

    Core uses Python ``str.format`` when it renders ``TemplateInfo`` prompts.
    Literal JSON braces therefore need doubling, while single-name fields such
    as ``{identity}`` must remain available to Core. Well-formed doubled
    braces and Core's backslash-escaped brace convention remain supported;
    partially escaped JSON is repaired. The result is idempotent.
    """

    if not template:
        return template
    if variables:
        template = _inject_variables(template, variables)
    return _escape_json_objects(template)
