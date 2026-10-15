"""One-time, fail-closed migration from the legacy Discord adapters.

This module is intentionally independent of discord.py and the Core runtime.
It reads the Koishi SQLite database in SQLite read-only mode, writes a private
identity snapshot once, and never reads Koishi during normal adapter runtime.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import tomlkit

from identity_map import IDENTITY_SCHEMA_VERSION, IdentityMap
from config_validation import ConfigValidationError, validate_config_mapping


CONFIG_VERSION = 3
# Native v2 adapter configs remain authoritative and must not be remigrated.
_MIGRATION_CUTOFF_VERSION = 2
_PLACEHOLDER_TOKENS = {"", "your_discord_bot_token", "your_token", "token"}
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")


class MigrationError(ValueError):
    """A sanitized, actionable migration failure with no source values."""


@dataclass(frozen=True)
class MigrationResult:
    success: bool
    migrated: bool
    message: str
    error: str | None = None
    user_count: int = 0
    channel_count: int = 0
    private_channel_count: int = 0


def needs_migration(config_path: Path | str) -> bool:
    """Return whether a target is absent or predates schema version 2.

    Malformed live TOML raises a sanitized error so setup can fail closed.
    """

    path = Path(config_path)
    if not path.exists():
        return True
    data = _load_toml(path, role="Discord adapter config")
    raw_version = data.get("config_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
        raise MigrationError("Discord adapter config has an invalid schema version")
    return raw_version < _MIGRATION_CUTOFF_VERSION


def _load_toml(path: Path, *, role: str) -> tomlkit.TOMLDocument:
    try:
        if not path.is_file():
            raise MigrationError(f"{role} file is missing or not a regular file")
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except MigrationError:
        raise
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError) as exc:
        raise MigrationError(f"{role} is unreadable or malformed TOML") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _snowflake(value: Any, *, role: str) -> str:
    if isinstance(value, bool) or value is None:
        raise MigrationError(f"Koishi database contains an invalid {role}")
    text = str(value).strip()
    if not _SNOWFLAKE_RE.fullmatch(text) or not 0 < int(text) < 2**64:
        raise MigrationError(f"Koishi database contains an invalid {role}")
    return text


def _logical_id(value: Any, *, role: str) -> str:
    if isinstance(value, bool) or value is None:
        raise MigrationError(f"Koishi database contains an invalid {role}")
    text = str(value).strip()
    if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
        raise MigrationError(f"Koishi database contains an invalid {role}")
    return text


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]).lower() for row in connection.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error as exc:
        raise MigrationError("Koishi database schema could not be read") from exc


def export_identity_map(database_path: Path | str) -> IdentityMap:
    """Read and validate the legacy Discord identity tables without writes."""

    path = Path(database_path)
    if not path.is_file():
        raise MigrationError("Koishi SQLite database is missing")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise MigrationError("Koishi SQLite database failed its read-only integrity check")

        required = {
            "binding": {"aid", "bid", "pid", "platform", "botselfid"},
            "bindingchannel": {"aid", "channelid"},
            "channelprivate": {"userid", "channelid", "botselfid", "platform"},
        }
        tables = {
            str(row[0]).lower()
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in required.items():
            if table not in tables or not columns.issubset(_table_columns(connection, table)):
                raise MigrationError("Koishi SQLite database is missing required identity fields")

        users: dict[str, str] = {}
        logical_users: dict[str, str] = {}
        bot_ids: set[str] = set()
        for aid, _bid, pid, _platform, botselfid in connection.execute(
            "SELECT aid, bid, pid, platform, botselfid FROM binding WHERE lower(platform)='discord'"
        ):
            native = _snowflake(pid, role="Discord user ID")
            logical = _logical_id(aid, role="logical user ID")
            if native in users and users[native] != logical:
                raise MigrationError("Koishi database contains conflicting Discord user bindings")
            if logical in logical_users and logical_users[logical] != native:
                raise MigrationError("Koishi database contains non-bijective Discord user bindings")
            users[native] = logical
            logical_users[logical] = native
            if botselfid not in (None, ""):
                bot_ids.add(_snowflake(botselfid, role="Discord bot ID"))

        channels: dict[str, str] = {}
        logical_channels: dict[str, str] = {}
        for aid, channel_id in connection.execute(
            "SELECT aid, channelId FROM bindingchannel"
        ):
            native = _snowflake(channel_id, role="Discord channel ID")
            logical = _logical_id(aid, role="logical channel ID")
            if native in channels and channels[native] != logical:
                raise MigrationError("Koishi database contains conflicting Discord channel bindings")
            if logical in logical_channels and logical_channels[logical] != native:
                raise MigrationError("Koishi database contains non-bijective Discord channel bindings")
            channels[native] = logical
            logical_channels[logical] = native

        private_channels: list[dict[str, str]] = []
        for user_id, channel_id, botselfid in connection.execute(
            "SELECT userId, channelId, botSelfId FROM channelprivate WHERE lower(platform)='discord'"
        ):
            private_user = _snowflake(user_id, role="Discord private user ID")
            private_channel = _snowflake(channel_id, role="Discord private channel ID")
            bot_id = "" if botselfid in (None, "") else _snowflake(botselfid, role="Discord bot ID")
            private_channels.append(
                {"user_id": private_user, "channel_id": private_channel, "bot_self_id": bot_id}
            )
            if bot_id:
                bot_ids.add(bot_id)
        connection.close()
    except MigrationError:
        try:
            connection.close()
        except Exception:
            pass
        raise
    except (sqlite3.Error, OSError) as exc:
        raise MigrationError("Koishi SQLite identity data could not be read") from exc

    if not users or not channels:
        raise MigrationError("Koishi SQLite database has no Discord identity mappings")
    return IdentityMap(users, channels, tuple(private_channels), frozenset(bot_ids))


def _yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
            return str(parsed) if parsed is not None else ""
        except json.JSONDecodeError as exc:
            raise MigrationError("Koishi Discord network config has malformed YAML scalar data") from exc
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise MigrationError("Koishi Discord network config has malformed YAML scalar data")
        return value[1:-1].replace("''", "'")
    # Only these scalar values are consumed. Strip a trailing YAML comment when
    # it is separated by whitespace; URLs may contain '#' without whitespace.
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value.lower() in {"null", "~"}:
        return ""
    return value


def _yaml_key(line: str) -> tuple[int, str, str] | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    if ":" not in stripped:
        return None
    key, raw_value = stripped.split(":", 1)
    if not key or key.startswith("!"):
        return None
    return len(line) - len(stripped), key.strip(), raw_value.strip()


def _koishi_discord_network(path: Path) -> dict[str, str]:
    """Extract only old Discord token/proxy scalars without logging their values."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError("Koishi Discord network config is unreadable") from exc

    plugin_indent: int | None = None
    proxy_indent: int | None = None
    token_candidates: list[str] = []
    proxy_candidates: list[str] = []
    for line in lines:
        parsed = _yaml_key(line)
        if parsed is None:
            continue
        indent, key, raw = parsed
        if key == "adapter-discord":
            plugin_indent = indent
            continue
        if plugin_indent is not None:
            if indent <= plugin_indent:
                plugin_indent = None
            elif indent > plugin_indent and key == "token":
                token_candidates.append(_yaml_scalar(raw))
        if key == "proxy-agent":
            proxy_indent = indent
            continue
        if proxy_indent is not None:
            if indent <= proxy_indent:
                proxy_indent = None
            elif indent > proxy_indent and key == "proxyAgent":
                proxy_candidates.append(_yaml_scalar(raw))

    tokens = {value for value in token_candidates if value}
    proxies = {value for value in proxy_candidates if value}
    if len(tokens) > 1 or len(proxies) > 1:
        raise MigrationError("Koishi Discord network config has conflicting settings")
    return {
        "token": next(iter(tokens), ""),
        "proxy_url": next(iter(proxies), ""),
    }


def _is_placeholder_token(value: Any) -> bool:
    return str(value or "").strip().lower() in _PLACEHOLDER_TOKENS


def _filter_list(source: Any, *, role: str) -> list[str]:
    if not isinstance(source, (list, tuple)):
        raise MigrationError(f"Legacy {role} filter must be a list")
    result: list[str] = []
    for item in source:
        if isinstance(item, bool) or item is None:
            raise MigrationError(f"Legacy {role} filter contains an invalid entry")
        value = str(item).strip()
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise MigrationError(f"Legacy {role} filter contains an invalid entry")
        result.append(value)
    return result


def _convert_filter_ids(
    values: list[str],
    native_to_logical: Mapping[str, str],
    *,
    role: str,
    list_type: str,
) -> list[str]:
    logical_values = set(native_to_logical.values())
    result: list[str] = []
    for value in values:
        if value in native_to_logical:
            result.append(native_to_logical[value])
        elif value in logical_values:
            result.append(value)
        elif list_type == "whitelist":
            raise MigrationError(
                f"Legacy {role} whitelist has an unmappable entry; resolve it before migration"
            )
        else:
            # Keeping an unknown blacklist/ban entry preserves a deny rule. New
            # unmapped entities use their native ID as the logical ID.
            result.append(value)
    return list(dict.fromkeys(result))


def _read_mapping(table: Mapping[str, Any], key: str, role: str) -> dict[str, Any]:
    value = table.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MigrationError(f"Legacy {role} section must be a table")
    return _plain(value)


def _merge_config(
    voice_document: tomlkit.TOMLDocument,
    text_data: Mapping[str, Any],
    identity: IdentityMap,
    koishi_network: Mapping[str, str],
) -> bytes:
    merged = copy.deepcopy(voice_document)
    voice_config = _read_mapping(voice_document, "voice", "voice config")
    discord = _read_mapping(voice_document, "discord", "Discord config")
    old_discord = _read_mapping(text_data, "discord", "Discord config")
    core_source = _read_mapping(text_data, "nachobot_server", "Core config")
    if str(core_source.get("platform", "discord")).lower() != "discord":
        raise MigrationError("Legacy text adapter config is not for Discord")

    for key in ("token", "app_id", "proxy_enabled", "proxy_url"):
        if key not in discord and key in old_discord:
            discord[key] = _plain(old_discord[key])
    voice_token = str(discord.get("token", "") or "").strip()
    koishi_token = str(koishi_network.get("token", "") or "").strip()
    if not _is_placeholder_token(voice_token) and not _is_placeholder_token(koishi_token):
        if voice_token != koishi_token:
            raise MigrationError("Legacy Discord credential sources conflict; select the active token first")
    elif _is_placeholder_token(voice_token) and not _is_placeholder_token(koishi_token):
        discord["token"] = koishi_token
    if not str(discord.get("proxy_url", "") or "").strip():
        discord["proxy_url"] = str(koishi_network.get("proxy_url", "") or "")
    discord.setdefault("proxy_enabled", bool(discord.get("proxy_url")))
    discord.setdefault("app_id", "")
    merged["discord"] = discord

    core = _read_mapping(voice_document, "nachobot", "Core config")
    if not core.get("host"):
        core["host"] = str(core_source.get("host", "localhost"))
    if not core.get("port"):
        core["port"] = int(core_source.get("port", 8000))
    merged["nachobot"] = core

    chat_source = _read_mapping(text_data, "chat", "chat config")
    group_type = str(chat_source.get("group_list_type", "whitelist")).lower()
    private_type = str(chat_source.get("private_list_type", "blacklist")).lower()
    if group_type not in {"whitelist", "blacklist"} or private_type not in {"whitelist", "blacklist"}:
        raise MigrationError("Legacy chat filter type must be whitelist or blacklist")
    group_list = _convert_filter_ids(
        _filter_list(chat_source.get("group_list", []), role="group"),
        identity.channel_native_to_logical,
        role="group",
        list_type=group_type,
    )
    private_list = _convert_filter_ids(
        _filter_list(chat_source.get("private_list", []), role="private user"),
        identity.user_native_to_logical,
        role="private user",
        list_type=private_type,
    )
    banned_users = _convert_filter_ids(
        _filter_list(chat_source.get("ban_user_id", []), role="banned user"),
        identity.user_native_to_logical,
        role="banned user",
        list_type="blacklist",
    )
    merged["chat"] = {
        "group_list_type": group_type,
        "group_list": group_list,
        "private_list_type": private_type,
        "private_list": private_list,
        "ban_user_id": banned_users,
    }

    voice = dict(voice_config)
    voice["use_tts"] = bool((text_data.get("voice", {}) or {}).get("use_tts", True))
    voice_filter_type = str(voice.get("allowed_channel_list_type", "blacklist")).lower()
    if voice_filter_type not in {"whitelist", "blacklist"}:
        raise MigrationError("Legacy voice channel filter type must be whitelist or blacklist")
    voice["allowed_channel_list_type"] = voice_filter_type
    voice["allowed_channel_list"] = _convert_filter_ids(
        _filter_list(voice.get("allowed_channel_list", []), role="voice channel"),
        identity.channel_native_to_logical,
        role="voice channel",
        list_type=voice_filter_type,
    )
    visual_source = _read_mapping(text_data, "visual", "visual config")
    visual_image = _read_mapping(visual_source, "image", "visual image config")
    merged["voice"] = voice
    merged["visual"] = {"image": visual_image}
    network_source = _read_mapping(text_data, "network", "network config")
    merged["network"] = {"proxy": str(network_source.get("proxy", "") or "")}
    merged.setdefault("media", {"max_attachment_bytes": 20 * 1024 * 1024})
    merged["identity"] = {"required": True}
    merged["config_version"] = CONFIG_VERSION
    try:
        validate_config_mapping(merged, require_token=False)
    except ConfigValidationError as exc:
        raise MigrationError(str(exc)) from exc
    return tomlkit.dumps(merged).encode("utf-8")


def _atomic_temp(path: Path, content: bytes, *, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_path, stat.S_IMODE(mode))
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _identity_bytes(identity: IdentityMap) -> bytes:
    return (json.dumps(identity.to_mapping(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def migrate_legacy_config(
    *,
    target_config_path: Path | str,
    legacy_discordvc_config_path: Path | str,
    legacy_koishi_adapter_config_path: Path | str,
    koishi_config_path: Path | str,
    identity_path: Path | str,
    koishi_db_path: Path | str,
) -> MigrationResult:
    """Validate both legacy configs and SQLite, then atomically publish v2.

    All source parsing, policy conversion, ID-bijection checks, and existing-map
    conflict checks finish before any destination is created or replaced.
    """

    target = Path(target_config_path)
    voice_source = Path(legacy_discordvc_config_path)
    text_source = Path(legacy_koishi_adapter_config_path)
    koishi_source = Path(koishi_config_path)
    map_path = Path(identity_path)
    database = Path(koishi_db_path)
    try:
        if target.exists() and not needs_migration(target):
            return MigrationResult(True, False, "Discord adapter config is already current")
        voice_document = _load_toml(voice_source, role="Legacy DiscordVC config")
        text_document = _load_toml(text_source, role="Legacy Koishi adapter config")
        identity = export_identity_map(database)
        koishi_network = _koishi_discord_network(koishi_source)
        merged_bytes = _merge_config(voice_document, text_document, identity, koishi_network)
        map_bytes = _identity_bytes(identity)

        if map_path.exists():
            current_identity = IdentityMap.load(map_path)
            if current_identity.to_mapping() != identity.to_mapping():
                raise MigrationError("Existing Discord identity map conflicts with the validated legacy export")

        # Preserve the old credential-bearing DiscordVC source if migration is
        # in-place.  The backup is private and ignored by Git.
        backup_path: Path | None = None
        if voice_source.resolve() == target.resolve() and target.exists():
            backup_path = target.with_name("config.toml.legacy-v1.bak")
            old_bytes = target.read_bytes()
            if backup_path.exists() and backup_path.read_bytes() != old_bytes:
                raise MigrationError("Existing private legacy config backup conflicts with the source")

        map_temp: Path | None = None
        backup_temp: Path | None = None
        config_temp: Path | None = None
        try:
            if not map_path.exists():
                map_temp = _atomic_temp(map_path, map_bytes, mode=0o600)
            if backup_path is not None and not backup_path.exists():
                backup_temp = _atomic_temp(backup_path, old_bytes, mode=0o600)
            target_mode = target.stat().st_mode if target.exists() else None
            config_temp = _atomic_temp(target, merged_bytes, mode=target_mode or 0o600)

            # Publish the identity file first.  If config replacement fails,
            # rerun is safe because an identical map is accepted idempotently.
            if map_temp is not None:
                os.replace(map_temp, map_path)
                map_temp = None
            if backup_temp is not None:
                os.replace(backup_temp, backup_path)
                backup_temp = None
            os.replace(config_temp, target)
            config_temp = None
        finally:
            for temp_path in (map_temp, backup_temp, config_temp):
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        return MigrationResult(
            True,
            True,
            "Legacy Discord config and identity map migrated",
            user_count=len(identity.user_native_to_logical),
            channel_count=len(identity.channel_native_to_logical),
            private_channel_count=len(identity.private_channels),
        )
    except MigrationError as exc:
        return MigrationResult(False, False, "Discord migration was not applied", str(exc))
    except (OSError, sqlite3.Error, tomlkit.exceptions.TOMLKitError) as exc:
        # Do not include exception text: it can contain a path or source bytes.
        return MigrationResult(False, False, "Discord migration was not applied", "A validated migration file could not be written")


def _default_paths(root: Path) -> dict[str, Path]:
    adapter = root / "NachoBot-Discord-Adapter"
    target = adapter / "config.toml"
    old_voice = root / "NachoBot-DiscordVC-Adapter" / "config.toml"
    return {
        "target_config_path": target,
        "legacy_discordvc_config_path": target if target.exists() else old_voice,
        "legacy_koishi_adapter_config_path": root / "NachoBot-Koishi-Adapter" / "config.toml",
        "koishi_config_path": root / "koishi-app" / "koishi.yml",
        "identity_path": adapter / "data" / "identity_map.json",
        "koishi_db_path": root / "koishi-app" / "data" / "koishi.db",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Discord adapter settings once")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    result = migrate_legacy_config(**_default_paths(args.root.resolve()))
    print(result.message)
    if result.error:
        print(f"Migration error: {result.error}")
        return 1
    if result.migrated:
        print(
            "Exported validated identity counts: "
            f"users={result.user_count}, channels={result.channel_count}, "
            f"private_channels={result.private_channel_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
