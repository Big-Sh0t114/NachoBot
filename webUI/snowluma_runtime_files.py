"""Standard-library validation for the SnowLuma runtime file graph.

This is a small lexical scanner for generated JavaScript bundles, not a full
JavaScript parser. It recognizes ordinary static imports, re-exports, and
literal ``import("./chunk.js")`` calls. Comments, quoted strings, templates,
and common regular-expression literals are skipped so their text cannot invent
dependencies. Imports assembled from expressions and code inside template
interpolations are intentionally outside this checker's scope.
"""

from __future__ import annotations

import os
import posixpath
import stat
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator
from urllib.parse import unquote


REQUIRED_RUNTIME_FILES: tuple[str, ...] = (
    "package.json",
    "index.mjs",
    "launcher.bat",
    "client/index.html",
    "native/snowluma-win32-x64.dll",
    "native/snowluma-win32-x64.node",
    "native/websocket-win32-x64.node",
)

# Current bundled index.mjs is about 7.4 MB. These limits leave ample room for
# larger bundles while ensuring a malformed runtime cannot consume
# unbounded memory or scan time during setup.
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MODULE_COUNT = 4096
MAX_SUCCESS_CACHE_ROOTS = 8

@dataclass(frozen=True)
class _Token:
    kind: str
    value: str | None


class _TokenStream:
    def __init__(self, tokens: Iterator[_Token]) -> None:
        self._tokens = tokens
        self._buffer: deque[_Token | None] = deque()

    def peek(self, offset: int = 0) -> _Token | None:
        while len(self._buffer) <= offset:
            try:
                self._buffer.append(next(self._tokens))
            except StopIteration:
                self._buffer.append(None)
        return self._buffer[offset]

    def pop(self) -> _Token | None:
        self.peek()
        return self._buffer.popleft()


_SUCCESS_CACHE: OrderedDict[
    tuple[str, tuple[str, ...]], tuple[tuple[Path, tuple[object, ...]], ...]
] = OrderedDict()
_SUCCESS_CACHE_LOCK = threading.RLock()


def _file_signature(path: Path, runtime_root: Path) -> tuple[object, ...] | None:
    """Fingerprint a file and its symlink target without reading its contents."""

    try:
        link_stat = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(runtime_root)
        target_stat = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        return None

    def identity(result: os.stat_result) -> tuple[object, ...]:
        return (
            result.st_dev,
            result.st_ino,
            result.st_mode,
            result.st_size,
            result.st_mtime_ns,
            result.st_ctime_ns,
            getattr(result, "st_file_attributes", None),
            getattr(result, "st_reparse_tag", None),
        )

    return (
        os.path.normcase(str(resolved)),
        identity(link_stat),
        identity(target_stat),
    )


def _cache_key(runtime_root: Path, stable_files: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    return os.path.normcase(str(runtime_root)), stable_files


def _cached_success(
    runtime_root: Path, stable_files: tuple[str, ...]
) -> bool:
    key = _cache_key(runtime_root, stable_files)
    with _SUCCESS_CACHE_LOCK:
        snapshot = _SUCCESS_CACHE.get(key)
        if snapshot is None:
            return False
        if any(_file_signature(path, runtime_root) != signature for path, signature in snapshot):
            del _SUCCESS_CACHE[key]
            return False
        _SUCCESS_CACHE.move_to_end(key)
        return True


def _remember_success(
    runtime_root: Path,
    stable_files: tuple[str, ...],
    snapshot: dict[str, tuple[Path, tuple[object, ...]]],
) -> None:
    key = _cache_key(runtime_root, stable_files)
    value = tuple(snapshot.values())
    with _SUCCESS_CACHE_LOCK:
        _SUCCESS_CACHE[key] = value
        _SUCCESS_CACHE.move_to_end(key)
        while len(_SUCCESS_CACHE) > MAX_SUCCESS_CACHE_ROOTS:
            _SUCCESS_CACHE.popitem(last=False)


def _can_start_regular_expression(previous: _Token | None) -> bool:
    if previous is None:
        return True
    if previous.kind == "identifier":
        return previous.value in {
            "await",
            "case",
            "delete",
            "else",
            "in",
            "instanceof",
            "return",
            "throw",
            "typeof",
            "void",
            "yield",
        }
    return previous.value in {
        "(",
        "[",
        "{",
        "=",
        ":",
        ",",
        ";",
        "!",
        "?",
        "&&",
        "||",
        "??",
        "=>",
    }


def _skip_quoted(source: str, index: int, quote: str) -> tuple[int, str | None]:
    """Skip a JS single/double quoted literal and return its decoded value."""

    output: list[str] = []
    cursor = index + 1
    while cursor < len(source):
        char = source[cursor]
        if char == quote:
            return cursor + 1, "".join(output)
        if char in "\r\n":
            return cursor, None
        if char != "\\":
            output.append(char)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(source):
            break
        escaped = source[cursor]
        if escaped in "\r\n":
            if escaped == "\r" and cursor + 1 < len(source) and source[cursor + 1] == "\n":
                cursor += 1
            cursor += 1
            continue
        if escaped == "u":
            if cursor + 1 < len(source) and source[cursor + 1] == "{":
                close = source.find("}", cursor + 2)
                digits = source[cursor + 2 : close] if close >= 0 else ""
                try:
                    output.append(chr(int(digits, 16)))
                    cursor = close + 1
                    continue
                except (ValueError, OverflowError):
                    return cursor + 1, None
            digits = source[cursor + 1 : cursor + 5]
            try:
                output.append(chr(int(digits, 16)))
                cursor += 5
                continue
            except (ValueError, OverflowError):
                return cursor + 1, None
        if escaped == "x":
            digits = source[cursor + 1 : cursor + 3]
            try:
                output.append(chr(int(digits, 16)))
                cursor += 3
                continue
            except (ValueError, OverflowError):
                return cursor + 1, None
        output.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}.get(escaped, escaped))
        cursor += 1
    return cursor, None


def _skip_template(source: str, index: int) -> int:
    """Skip a template literal, including escaped backticks."""

    cursor = index + 1
    while cursor < len(source):
        if source[cursor] == "\\":
            cursor += 2
        elif source[cursor] == "`":
            return cursor + 1
        else:
            cursor += 1
    return cursor


def _skip_regular_expression(source: str, index: int) -> int:
    cursor = index + 1
    in_character_class = False
    while cursor < len(source):
        char = source[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char in "\r\n":
            return cursor
        if char == "[":
            in_character_class = True
        elif char == "]":
            in_character_class = False
        elif char == "/" and not in_character_class:
            cursor += 1
            while cursor < len(source) and _is_identifier_continue(source[cursor]):
                cursor += 1
            return cursor
        cursor += 1
    return cursor


def _is_identifier_start(char: str) -> bool:
    return char in "_$" or char.isalpha() or ord(char) >= 128


def _is_identifier_continue(char: str) -> bool:
    return _is_identifier_start(char) or "0" <= char <= "9"


def _lex(source: str) -> Iterator[_Token]:
    cursor = 0
    previous: _Token | None = None
    length = len(source)
    while cursor < length:
        char = source[cursor]
        if char.isspace() or char == "\ufeff":
            cursor += 1
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            cursor = length if newline < 0 else newline + 1
            continue
        if source.startswith("/*", cursor):
            close = source.find("*/", cursor + 2)
            cursor = length if close < 0 else close + 2
            continue
        if char in "'\"":
            cursor, value = _skip_quoted(source, cursor, char)
            token = _Token("string", value)
            previous = token
            yield token
            continue
        if char == "`":
            cursor = _skip_template(source, cursor)
            previous = _Token("template", None)
            continue
        if char == "/" and _can_start_regular_expression(previous):
            cursor = _skip_regular_expression(source, cursor)
            previous = _Token("regexp", None)
            continue
        if _is_identifier_start(char):
            end = cursor + 1
            while end < length and _is_identifier_continue(source[end]):
                end += 1
            token = _Token("identifier", source[cursor:end])
            previous = token
            cursor = end
            yield token
            continue
        if source.startswith("=>", cursor) or source.startswith("?.", cursor):
            token = _Token("punctuation", source[cursor : cursor + 2])
            cursor += 2
        elif source.startswith("...", cursor):
            token = _Token("punctuation", "...")
            cursor += 3
        else:
            token = _Token("punctuation", char)
            cursor += 1
        previous = token
        yield token


def _from_specifier(stream: _TokenStream) -> str | None:
    brace_depth = bracket_depth = paren_depth = 0
    while True:
        token = stream.peek()
        if token is None:
            return None
        value = token.value
        if brace_depth == bracket_depth == paren_depth == 0:
            if value == ";":
                stream.pop()
                return None
            if token.kind == "identifier" and value in {"import", "export"}:
                return None
            if token.kind == "identifier" and value == "from":
                stream.pop()
                specifier = stream.pop()
                return specifier.value if specifier and specifier.kind == "string" else None
        stream.pop()
        if value == "{":
            brace_depth += 1
        elif value == "}" and brace_depth:
            brace_depth -= 1
        elif value == "[":
            bracket_depth += 1
        elif value == "]" and bracket_depth:
            bracket_depth -= 1
        elif value == "(":
            paren_depth += 1
        elif value == ")" and paren_depth:
            paren_depth -= 1
    return None


def _module_specifiers(source: str) -> Iterator[str]:
    stream = _TokenStream(_lex(source))
    previous: _Token | None = None
    seen: set[str] = set()
    while (token := stream.pop()) is not None:
        if token.kind != "identifier" or token.value not in {"import", "export"}:
            previous = token
            continue
        if previous and previous.value in {".", "?."}:
            previous = token
            continue
        specifier: str | None = None
        following = stream.peek()
        if token.value == "import":
            if following and following.kind == "string":
                specifier = following.value
            elif following and following.value == "(" and (literal := stream.peek(1)) is not None:
                after_literal = stream.peek(2)
                if (
                    literal.kind == "string"
                    and after_literal is not None
                    and after_literal.value in {")", ","}
                ):
                    specifier = literal.value
            elif following and (
                following.kind == "identifier" or following.value in {"{", "*"}
            ):
                specifier = _from_specifier(stream)
        elif following and following.value in {"*", "{"}:
            specifier = _from_specifier(stream)
        if specifier and specifier not in seen:
            seen.add(specifier)
            yield specifier
        previous = token


def _relative_specifier(specifier: str) -> str | None:
    if not specifier.startswith(("./", "../")):
        return None
    path = specifier.split("?", 1)[0].split("#", 1)[0]
    decoded = unquote(path)
    if not decoded or "\0" in decoded:
        return "<invalid relative import>"
    return decoded


def _inside(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _display_relative(module_relative: PurePosixPath, specifier: str) -> str:
    candidate = PurePosixPath(posixpath.normpath(str(module_relative.parent / specifier)))
    return candidate.as_posix()


def required_runtime_components(
    runtime_path: Path | str,
    required_files: Iterable[str] = REQUIRED_RUNTIME_FILES,
) -> list[str]:
    """Return runtime-relative missing files, including actual JS dependencies."""

    try:
        runtime_root = Path(runtime_path).resolve()
    except (OSError, RuntimeError):
        return ["<runtime root> (unreadable)"]

    stable_files = tuple(required_files)
    if _cached_success(runtime_root, stable_files):
        return []

    missing: list[str] = []
    seen_missing: set[str] = set()
    snapshot: dict[str, tuple[Path, tuple[object, ...]]] = {}
    snapshot_changed = False

    def add(relative: str, reason: str = "") -> None:
        label = f"{relative} ({reason})" if reason else relative
        if label not in seen_missing:
            seen_missing.add(label)
            missing.append(label)

    def remember(path: Path, expected_target: Path | None = None) -> bool:
        nonlocal snapshot_changed
        signature = _file_signature(path, runtime_root)
        if signature is None:
            snapshot_changed = True
            return False
        if expected_target is not None and signature[0] != os.path.normcase(
            str(expected_target)
        ):
            snapshot_changed = True
            return False
        key = os.path.normcase(str(path))
        existing = snapshot.get(key)
        if existing is not None and existing[1] != signature:
            snapshot_changed = True
            return False
        snapshot[key] = (path, signature)
        return True

    for relative in stable_files:
        candidate = runtime_root.joinpath(*PurePosixPath(relative).parts)
        resolved = _inside(runtime_root, candidate)
        if resolved is None or not resolved.is_file():
            add(relative, "outside runtime" if candidate.exists() else "missing")
        else:
            remember(candidate, resolved)
            remember(resolved, resolved)

    entry = runtime_root / "index.mjs"
    resolved_entry = _inside(runtime_root, entry)
    if resolved_entry is None or not resolved_entry.is_file():
        return missing
    if not remember(entry, resolved_entry) or not remember(resolved_entry, resolved_entry):
        return ["dependency graph (changed during validation)"]

    pending: deque[tuple[Path, PurePosixPath, Path]] = deque(
        [(resolved_entry, PurePosixPath("index.mjs"), entry)]
    )
    visited: set[str] = set()
    total_source_bytes = 0

    while pending:
        module_path, module_relative, alias_path = pending.popleft()
        canonical_key = os.path.normcase(str(module_path))
        if canonical_key in visited:
            continue
        visited.add(canonical_key)
        if not remember(alias_path, module_path) or not remember(module_path, module_path):
            continue
        if len(visited) > MAX_MODULE_COUNT:
            add(module_relative.as_posix(), "dependency scan limit exceeded")
            break
        try:
            with module_path.open("rb") as source_file:
                raw = source_file.read(MAX_SOURCE_FILE_BYTES + 1)
        except OSError:
            add(module_relative.as_posix(), "unreadable")
            continue
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            add(module_relative.as_posix(), "file exceeds scan limit")
            continue
        total_source_bytes += len(raw)
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            add(module_relative.as_posix(), "dependency scan limit exceeded")
            break
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeError:
            add(module_relative.as_posix(), "unreadable")
            continue

        for specifier in _module_specifiers(source):
            relative_specifier = _relative_specifier(specifier)
            if relative_specifier is None:
                continue
            display = _display_relative(module_relative, relative_specifier)
            candidate = module_path.parent / Path(relative_specifier)
            resolved = _inside(runtime_root, candidate)
            if resolved is None:
                add(display, "outside runtime")
                continue
            try:
                is_file = resolved.is_file()
            except OSError:
                is_file = False
            if not is_file:
                add(display, "missing")
                continue
            if not remember(candidate, resolved) or not remember(resolved, resolved):
                continue
            if resolved.suffix.lower() in {".js", ".mjs", ".cjs"}:
                actual_relative = PurePosixPath(resolved.relative_to(runtime_root).as_posix())
                pending.append((resolved, actual_relative, candidate))

    if snapshot_changed:
        return ["dependency graph (changed during validation)"]
    if any(
        _file_signature(path, runtime_root) != signature
        for path, signature in snapshot.values()
    ):
        return ["dependency graph (changed during validation)"]
    if not missing:
        _remember_success(runtime_root, stable_files, snapshot)
    return missing


__all__ = ["REQUIRED_RUNTIME_FILES", "required_runtime_components"]
