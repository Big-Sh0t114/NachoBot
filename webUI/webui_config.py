from pathlib import Path
import tomlkit

ROOT_DIR = Path(__file__).resolve().parent.parent
WEBUI_DIR = Path(__file__).resolve().parent
CONFIG_PATH = WEBUI_DIR / "webui_config.toml"

DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8088
    },
    "paths": {
        "voxcpm_dir": "C:/Users/BigSh0t/VoxCPM-2.0.2",
        "sovits_dir": "C:/Users/BigSh0t/GPT-SoVITS/GPT-SoVITS-v2pro-20250604"
    },
    "proxy": {
        "http_proxy": "http://127.0.0.1:7897",
        "https_proxy": "http://127.0.0.1:7897"
    }
}

class WebUIConfig:
    def __init__(self):
        self.config = self._load()

    def reload(self):
        """Re-read config from disk. Call after webui_config.toml is updated."""
        self.config = self._load()

    def _load(self) -> dict:
        if not CONFIG_PATH.exists():
            self._save_default()
            return DEFAULT_CONFIG

        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw)
            merged = {}
            for section, keys in DEFAULT_CONFIG.items():
                merged[section] = {}
                section_doc = doc.get(section, {})
                for k, v in keys.items():
                    merged[section][k] = section_doc.get(k, v)
            return merged
        except Exception:
            return DEFAULT_CONFIG

    def _save_default(self):
        try:
            doc = tomlkit.document()
            
            # Server section
            server_table = tomlkit.table()
            server_table.add(tomlkit.comment("FastAPI/Uvicorn server hosting configuration"))
            server_table["host"] = DEFAULT_CONFIG["server"]["host"]
            server_table["port"] = DEFAULT_CONFIG["server"]["port"]
            doc["server"] = server_table

            # Paths section
            paths_table = tomlkit.table()
            paths_table.add(tomlkit.comment("Absolute paths to external model directories"))
            paths_table["voxcpm_dir"] = DEFAULT_CONFIG["paths"]["voxcpm_dir"]
            paths_table["sovits_dir"] = DEFAULT_CONFIG["paths"]["sovits_dir"]
            doc["paths"] = paths_table

            # Proxy section
            proxy_table = tomlkit.table()
            proxy_table.add(tomlkit.comment("Proxy configuration for adapters like Koishi/Discord"))
            proxy_table["http_proxy"] = DEFAULT_CONFIG["proxy"]["http_proxy"]
            proxy_table["https_proxy"] = DEFAULT_CONFIG["proxy"]["https_proxy"]
            doc["proxy"] = proxy_table

            CONFIG_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
        except Exception:
            pass

    @property
    def host(self) -> str:
        return self.config["server"]["host"]

    @property
    def port(self) -> int:
        return self.config["server"]["port"]

    @property
    def voxcpm_dir(self) -> Path:
        return Path(self.config["paths"]["voxcpm_dir"])

    @property
    def sovits_dir(self) -> Path:
        return Path(self.config["paths"]["sovits_dir"])

    @property
    def http_proxy(self) -> str:
        return self.config["proxy"]["http_proxy"]

    @property
    def https_proxy(self) -> str:
        return self.config["proxy"]["https_proxy"]

# Global instance
webui_config = WebUIConfig()
