"""Initialize or migrate the built-in plugin configuration without overwrites."""

import os
from pathlib import Path
import tempfile

import tomlkit


_PATH_FIELDS = (
    ("identity_file",), ("runtime_dir",), ("onebot", "token_file"),
    ("paths", "qq_exe"), ("paths", "loader_root"), ("paths", "python_exe"),
    ("paths", "node_exe"), ("universal_api", "token_file"), ("native", "guard_config_file"),
)


def migrated_config(source: str, old_directory: Path, new_directory: Path) -> str:
    """Retain settings/comments and keep relative paths pointing at the same files."""
    document = tomlkit.parse(source)
    settings = document["qqcall"]
    for keys in _PATH_FIELDS:
        table = settings
        for key in keys[:-1]:
            table = table.get(key, {})
        value = table.get(keys[-1], "")
        if not isinstance(value, str):
            raise ValueError("config_path_invalid")
        if not value or Path(value).is_absolute():
            continue
        if keys == ("paths", "node_exe") and "/" not in value and "\\" not in value:
            continue
        target = (old_directory / value).resolve()
        table[keys[-1]] = os.path.relpath(target, new_directory).replace("\\", "/")
    document.pop("plugin", None)
    return tomlkit.dumps(document)


def ensure_default_config(config_path: Path) -> bool:
    """Publish complete settings once; any existing destination always wins."""
    if os.path.lexists(config_path):
        return False
    legacy = config_path.parents[4] / "plugins/qqcall_plugin/config.toml"
    if legacy.is_file():
        source = migrated_config(legacy.read_text(encoding="utf-8"), legacy.parent, config_path.parent)
    else:
        source = "# QQCall settings. Enable with [qqcall].enabled in config/bot_config.toml.\n"
        from qqcall_client.qqcall_service import default_qqcall_config

        source += tomlkit.dumps(default_qqcall_config())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=config_path.parent, prefix=".qqcall-config-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(source.encode("utf-8"))
        try:
            os.link(temporary, config_path)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
