"""
NachoBot WebUI — Plugin Manager
Scans and manages plugins from NachoBot/plugins/.
"""

import json
from pathlib import Path
from typing import Any

import tomlkit

try:
    from .secure_paths import ensure_within, resolve_named_dir
    from .config_manager import ConfigManager
except ImportError:
    from secure_paths import ensure_within, resolve_named_dir
    from config_manager import ConfigManager

ROOT_DIR = Path(__file__).resolve().parent.parent


class PluginManager:
    """Scans NachoBot/plugins/ and provides read/write access to plugin configs."""

    def __init__(self, root_dir: Path | None = None):
        self.root = root_dir or ROOT_DIR
        self.plugins_dir = self.root / "NachoBot" / "plugins"

    def list_plugins(self) -> list[dict[str, Any]]:
        """Scan all plugin directories and return metadata."""
        plugins = []
        if not self.plugins_dir.exists():
            return plugins

        for d in sorted(self.plugins_dir.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "__")):
                continue

            info: dict[str, Any] = {
                "id": d.name,
                "name": d.name,
                "description": "",
                "version": "",
                "has_config": False,
                "has_manifest": False,
            }

            # Read manifest
            manifest_path = d / "_manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    info["has_manifest"] = True
                    info["name"] = manifest.get("name", d.name)
                    info["description"] = manifest.get("description", "")
                    info["version"] = manifest.get("version", "")
                    info["manifest"] = manifest
                except Exception:
                    pass

            # Check for config
            config_path = d / "config.toml"
            if config_path.exists():
                info["has_config"] = True

            # Check for README
            readme_path = d / "Readme.md"
            if not readme_path.exists():
                readme_path = d / "README.md"
            if readme_path.exists():
                try:
                    info["readme"] = readme_path.read_text(encoding="utf-8")
                except Exception:
                    pass

            plugins.append(info)

        return plugins

    def read_plugin_config(self, plugin_id: str, mask_sensitive: bool = True) -> dict[str, Any]:
        """Read a plugin's config.toml as a dict."""
        config_path = self._resolve_config_path(plugin_id, must_exist=True)
        # codeql[py/path-injection]
        if not config_path.exists():
            raise FileNotFoundError(f"No config.toml for plugin: {plugin_id}")

        # codeql[py/path-injection]
        raw = config_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(raw)
        data = self._tomlkit_to_dict(doc)
        if mask_sensitive:
            ConfigManager._mask_dict(data)
        return data

    def read_plugin_config_raw(self, plugin_id: str) -> str:
        """Read raw plugin TOML for the local WebUI editor."""
        config_path = self._resolve_config_path(plugin_id, must_exist=True)
        # codeql[py/path-injection]
        if not config_path.exists():
            raise FileNotFoundError(f"No config.toml for plugin: {plugin_id}")
        # codeql[py/path-injection]
        return config_path.read_text(encoding="utf-8")

    def write_plugin_config(self, plugin_id: str, data: dict[str, Any]) -> None:
        """Write updated config to a plugin's config.toml."""
        config_path = self._resolve_config_path(plugin_id)

        # Read existing to preserve comments
        # codeql[py/path-injection]
        if config_path.exists():
            # codeql[py/path-injection]
            raw = config_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw)
        else:
            doc = tomlkit.document()

        self._update_tomlkit_doc(doc, data)
        # codeql[py/path-injection]
        config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    def write_plugin_config_raw(self, plugin_id: str, raw: str) -> None:
        """Write raw plugin TOML from the local WebUI editor."""
        config_path = self._resolve_config_path(plugin_id)
        # codeql[py/path-injection]
        config_path.write_text(raw, encoding="utf-8")

    def _resolve_config_path(self, plugin_id: str, *, must_exist: bool = False) -> Path:
        plugin_dir = resolve_named_dir(self.plugins_dir, plugin_id, must_exist=True)
        config_path = ensure_within(self.plugins_dir, plugin_dir / "config.toml", must_exist=must_exist)
        # codeql[py/path-injection]
        if must_exist and not config_path.is_file():
            raise FileNotFoundError(f"No config.toml for plugin: {plugin_id}")
        return config_path

    @staticmethod
    def _tomlkit_to_dict(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: PluginManager._tomlkit_to_dict(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [PluginManager._tomlkit_to_dict(v) for v in obj]
        if isinstance(obj, tomlkit.items.Integer):
            return int(obj)
        if isinstance(obj, tomlkit.items.Float):
            return float(obj)
        if isinstance(obj, tomlkit.items.Bool):
            return bool(obj)
        if isinstance(obj, tomlkit.items.String):
            return str(obj)
        return obj

    @staticmethod
    def _update_tomlkit_doc(doc: Any, data: dict[str, Any]) -> None:
        for key, value in data.items():
            if isinstance(value, dict) and key in doc and isinstance(doc[key], dict):
                PluginManager._update_tomlkit_doc(doc[key], value)
            else:
                doc[key] = value
