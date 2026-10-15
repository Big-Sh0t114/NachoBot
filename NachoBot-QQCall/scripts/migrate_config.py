"""Deterministically migrate canonical QQCall TOML schema 5 to schema 6.

The default is a validation-only dry run. Use ``--apply`` only after reviewing
the proposed fields; no values or comments are printed by this script.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from service.config import DEFAULT_CONFIG, _normalize  # noqa: E402


_SCHEMA_LINE = re.compile(r"(?m)^(?P<indent>[ \t]*)schema(?P<space>[ \t]*)=(?P<after>[ \t]*)5(?P<tail>[ \t]*(?:#[^\r\n]*)?)(?P<eol>\r?)$")
_VAD_HEADER = re.compile(r"(?m)^[ \t]*\[qqcall\.vad\][ \t]*(?:#.*)?\r?\n?")
_TABLE_HEADER = re.compile(r"(?m)^[ \t]*\[[^\]\r\n]+\][ \t]*(?:#.*)?\r?\n?")
_NEW_TABLE = '[qqcall.universal_api]\nurl = "http://127.0.0.1:8016"\ntoken_file = ""\n'
_REPARSE_POINT = 0x400


class MigrationError(ValueError):
    pass


def _without(values: dict[str, Any], key: str) -> dict[str, Any]:
    result = dict(values)
    result.pop(key, None)
    return result


def _validate_schema5_shape(value: dict[str, Any]) -> None:
    if set(value) != {"schema", "plugin", "qqcall"}:
        raise MigrationError("source_fields_invalid")
    plugin = value.get("plugin")
    if not isinstance(plugin, dict) or set(plugin) != {"enabled"}:
        raise MigrationError("source_fields_invalid")
    qqcall = value.get("qqcall")
    if not isinstance(qqcall, dict):
        raise MigrationError("source_fields_invalid")
    stable = set(DEFAULT_CONFIG["qqcall"]) - {"universal_api", "audio"}
    allowed = stable | {"vad", "audio"}
    if not stable.issubset(qqcall) or set(qqcall) - allowed or "vad" not in qqcall:
        raise MigrationError("source_fields_invalid")
    table_shapes = {
        "onebot": {"url", "token_file"},
        "paths": {"qq_exe", "loader_root", "python_exe", "node_exe"},
        "ports": {"host", "callback", "control"},
        "policy": {"auto_accept"},
        "supervision": {"poll_interval_sec", "retry_initial_sec", "retry_max_sec", "max_failures_per_generation"},
        "native": {"shim_variant", "guard_config_file"},
    }
    for name, fields in table_shapes.items():
        table = qqcall.get(name)
        if not isinstance(table, dict) or set(table) != fields:
            raise MigrationError("source_fields_invalid")
    vad = qqcall.get("vad")
    if not isinstance(vad, dict) or set(vad) != {"engine", "model_path"}:
        raise MigrationError("source_fields_invalid")
    if not isinstance(vad.get("engine"), str) or vad["engine"] not in {"universalvc", "rms"}:
        raise MigrationError("source_fields_invalid")
    if (
        not isinstance(vad.get("model_path"), str) or not vad["model_path"].strip()
        or "\x00" in vad["model_path"] or len(vad["model_path"]) > 4096
    ):
        raise MigrationError("source_fields_invalid")
    audio = qqcall.get("audio", {})
    if not isinstance(audio, dict) or set(audio) - {"capture_device", "playback_device"}:
        raise MigrationError("source_fields_invalid")
    if not isinstance(qqcall.get("peer_map"), dict):
        raise MigrationError("source_fields_invalid")


def migrate_text(source: str) -> str:
    try:
        old = tomllib.loads(source)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise MigrationError("source_config_invalid") from exc
    if type(old.get("schema")) is not int or old.get("schema") != 5:
        raise MigrationError("source_schema_not_5")
    _validate_schema5_shape(old)
    qqcall = old.get("qqcall")
    if not isinstance(qqcall, dict) or "vad" not in qqcall or "universal_api" in qqcall:
        raise MigrationError("source_qqcall_schema_invalid")

    matches = list(_SCHEMA_LINE.finditer(source))
    if len(matches) != 1:
        raise MigrationError("schema_assignment_ambiguous")
    replaced = _SCHEMA_LINE.sub(
        lambda match: f"{match.group('indent')}schema{match.group('space')}={match.group('after')}6{match.group('tail')}{match.group('eol')}",
        source,
        count=1,
    )
    headers = list(_VAD_HEADER.finditer(replaced))
    if len(headers) != 1:
        raise MigrationError("vad_table_ambiguous")
    header = headers[0]
    following = _TABLE_HEADER.search(replaced, header.end())
    section_end = following.start() if following else len(replaced)
    old_section = replaced[header.end():section_end]
    # Keep the old section's line ending style while preserving its comments.
    newline = "\r\n" if "\r\n" in source else "\n"
    new_table = _NEW_TABLE.replace("\n", newline)
    preserved_comments = _comments_as_lines(old_section, newline)
    table_head, table_body = new_table.split(newline, 1)
    replacement = table_head + newline + preserved_comments + table_body
    transformed = replaced[:header.start()] + replacement + replaced[section_end:]
    try:
        new = tomllib.loads(transformed)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise MigrationError("migration_result_invalid") from exc
    if new.get("schema") != 6:
        raise MigrationError("migration_schema_invalid")
    try:
        _normalize(new, Path.cwd())
    except Exception as exc:
        raise MigrationError("migration_result_invalid") from exc
    new_qqcall = new.get("qqcall")
    if not isinstance(new_qqcall, dict) or new_qqcall.get("universal_api") != {
        "url": "http://127.0.0.1:8016", "token_file": "",
    }:
        raise MigrationError("migration_fields_invalid")
    old_compare = dict(old)
    old_compare["schema"] = 6
    old_compare["qqcall"] = _without(qqcall, "vad")
    new_compare = dict(new)
    new_compare["qqcall"] = _without(new_qqcall, "universal_api")
    if old_compare != new_compare:
        raise MigrationError("migration_changed_unrelated_values")
    return transformed


def _comments_as_lines(section: str, newline: str) -> str:
    comments: list[str] = []
    quote: str | None = None
    triple = False
    escaped = False
    for line in section.splitlines():
        index = 0
        comment_at: int | None = None
        while index < len(line):
            if quote is not None:
                if quote == '"' and escaped:
                    escaped = False
                    index += 1
                    continue
                if quote == '"' and line[index] == "\\":
                    escaped = True
                    index += 1
                    continue
                closing = quote * (3 if triple else 1)
                if line.startswith(closing, index):
                    index += len(closing)
                    quote = None
                    triple = False
                    continue
                index += 1
                continue
            if line[index] == "#":
                comment_at = index
                break
            if line.startswith('"""', index) or line.startswith("'''", index):
                quote = line[index]
                triple = True
                index += 3
                continue
            if line[index] in {'"', "'"}:
                quote = line[index]
                triple = False
            index += 1
        if comment_at is not None:
            comments.append(line[comment_at:])
        escaped = False
    return "".join(comment + newline for comment in comments)


def _validate_config_target(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    try:
        info = candidate.lstat()
    except OSError:
        raise MigrationError("config_io_failed") from None
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(info.st_mode)
        or attributes & _REPARSE_POINT
        or not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_nlink", 1) != 1
    ):
        raise MigrationError("config_target_unsupported")
    return candidate.resolve(strict=True)


def _atomic_write(path: Path, value: str) -> None:
    current_mode = stat.S_IMODE(path.stat().st_mode)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, current_mode)
        if os.name == "nt":
            replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                     ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
            replace_file.restype = ctypes.c_int
            if not replace_file(str(path), temp_name, None, 0, None, None):
                raise OSError(ctypes.get_last_error(), "ReplaceFileW failed")
            temp_name = None
        else:
            os.replace(temp_name, path)
            temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate QQCall canonical config schema 5 to 6")
    parser.add_argument("config", type=Path, help="canonical config TOML path")
    parser.add_argument("--apply", action="store_true", help="write the validated migration")
    args = parser.parse_args()
    try:
        path = _validate_config_target(args.config)
        source = path.read_text(encoding="utf-8")
        transformed = migrate_text(source)
        if args.apply:
            _atomic_write(path, transformed)
    except (OSError, UnicodeError, MigrationError) as exc:
        code = str(exc) if isinstance(exc, MigrationError) else "config_io_failed"
        print(f"migration rejected: {code}")
        return 2
    action = "applied" if args.apply else "dry-run valid"
    print(f"schema 5 -> 6; removed qqcall.vad; added qqcall.universal_api defaults; {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
